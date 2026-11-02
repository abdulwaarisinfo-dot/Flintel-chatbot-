"""
tests/test_active_intent_workflow.py — the active-lead-intent workflow THROUGH the real answer paths.

* the real `routes.stream_answer` (SSE) with its real hooks, and
* the real `index._complete_message_answer_and_results` (the background path),

with only the outside world faked: saved-post retrieval (`get_evidence_with_topup`), the model (`analyze_with_claude*`), the
OpenAI HTTP call (a fake callable behind the real reddit_live.process) and the databases (in-memory fakes / mongomock).
No network, no real OpenAI request, no production data.

The point of these tests: historical search and the live branch are independent; each one's failure or emptiness leaves the
other's result intact; a normal search is untouched; the recent window is applied only where intended; and a live candidate is
never presented as verified, dated, or inside the historical window.
"""
import datetime
import json
import socket
import sys
import threading
import types

import pytest

mongomock = pytest.importorskip("mongomock")
import active_intent as ain  # noqa: E402
import intent_classifier as ic  # noqa: E402
import reddit_live as rl  # noqa: E402
import timerange  # noqa: E402
from tests import test_realtime_streaming as rs  # noqa: E402
from tests.test_active_intent import attach_classifier, set_classifier_caps  # noqa: E402
from tests.test_reddit_live import Env, message, on  # noqa: F401,E402
from tests.test_reliability_ux_perf import _Req, app_env, env  # noqa: F401,E402

EMAIL = "alice@example.com"
CRM = "Find people on Reddit who are actively looking for a CRM or asking for recommendations for a CRM for their business."
NOT_LEAD = "What is a CRM and how does it work?"
HIST_URL = "https://www.reddit.com/r/crm/comments/abc123/need_a_crm/"            # a realistic post id (the shared fixture POST has a 3-character one)
HIST_ROW = dict(rs.POST, post_url=HIST_URL)
LIVE1 = "https://www.reddit.com/r/smallbusiness/comments/aaa111/which_crm_should_i_use/"
LIVE2 = "https://www.reddit.com/r/startups/comments/bbb222/need_help_picking_software/"
NO_RESULTS = {"format": "no_results", "message": "No matching posts were found for this search."}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def _clean():
    assert rl.INFLIGHT.count == 0
    yield
    ain.DEPS_FACTORY = None
    ic.DEPS_FACTORY = None
    assert rl.INFLIGHT.count == 0


def resp(items, with_call=True):
    out = []
    if with_call:
        out.append({"type": "web_search_call", "id": "ws_1", "status": "completed",
                    "action": {"type": "search", "sources": [{"type": "url", "url": u} for u, _ in items]}})
    out.append({"type": "message", "content": [{"type": "output_text", "text": "PROSE", "annotations": [
        {"type": "url_citation", "url": u, "title": t, "start_index": 0, "end_index": 1} for u, t in items]}]})
    return {"id": "r", "status": "completed", "output": out, "usage": {"input_tokens": 10, "output_tokens": 5}}


class World:
    """One search's surroundings: the live backend, what retrieval was asked, what the model was given."""

    def __init__(self, chat_id, query=CRM, live_items=((LIVE1, "Which CRM should I use for my agency?"), (LIVE2, "Need help picking software"))):
        self.chat_id = chat_id
        self.live = Env([message(0, claude_answer=None, answered_at=None)], owner=EMAIL)
        self.live.chats.delete_many({"chat_id": "c1"})                       # Env's own default chat; this search's chat replaces it
        self.live.add_chat(EMAIL, chat_id, [message(0, claude_answer=None, answered_at=None, query=query, resolved_query=None,
                                                    request_id="req-1", credit_request_id="cr-1", evidence_required=15, keywords=["crm"])])
        self.live.response = resp(list(live_items))
        attach_classifier(self.live, decide=lambda t: "what is a crm" not in t.lower())     # a stand-in for the model's answer
        self.meta_ids = []
        subs = {rl.post_ref(u)[1]: rl.post_ref(u)[0] for u, _ in live_items if rl.post_ref(u)}
        titles = {rl.post_ref(u)[1]: t for u, t in live_items if rl.post_ref(u)}

        def info(ids, timeout):                       # Reddit's metadata endpoint, faked: every found post is 5 minutes old
            self.meta_ids.append(list(ids))
            now = datetime.datetime.now(datetime.timezone.utc)
            return {i: {"created_utc": (now - datetime.timedelta(minutes=5)).timestamp(), "subreddit": subs.get(i, "x"), "removed": False,
                        "title": titles.get(i, "A real post"), "body": f"{titles.get(i, 'A real post')}. Comparing options and asking for advice."}
                    for i in ids}
        self.live.deps.reddit_info = info
        self.retrieval_kwargs = []
        self.model_pools = []
        self.model_contexts = []


def enable(monkeypatch, minutes=30, wait=10):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", True, raising=False)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", minutes, raising=False)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_LIVE_WAIT_SECONDS", wait, raising=False)
    set_classifier_caps(monkeypatch)


def seed_stream_message(chats, chat_id, query=CRM, **over):
    now = datetime.datetime.utcnow()
    msg = {"query": query, "topic_key": "find crm buyers", "keywords": ["crm"], "match_phrases": None, "targeting_platform": "reddit",
           "time_window_days": None, "time_range": None, "evidence_required": 15, "unfiltered": False, "website_only": False,
           "website_context": None, "google_fallback_triggered": True, "search_progress_generated": True,
           "message_type": "search", "requested_at": now, "results": None, "claude_answer": None,
           "request_id": "req-1", "credit_request_id": "cr-1"}
    msg.update(over)
    chats.docs[(chat_id, EMAIL)] = {"chat_id": chat_id, "owner_key": EMAIL, "owner_type": "email", "title": "S", "messages": [msg],
                                    "summary": "", "created_at": now, "updated_at": now}
    return msg


def run_stream(app_env, monkeypatch, world, *, retrieval=None, nonblocking=True, query=CRM, msg_over=None, analysis=None):
    """Drive the real stream_answer. `retrieval(**kwargs)` replaces the saved-post search."""
    lg, idx, rt, chats, state = app_env
    saves = rs._common_stubs(app_env, monkeypatch)
    # the non-blocking stream also asks the (stubbed in this suite) flintel module for its progress-bar helper
    monkeypatch.setattr(sys.modules["flintel"], "calculate_search_progress_percent",
                        lambda elapsed_seconds, trigger_seconds=0, timeout_seconds=1: 1, raising=False)
    monkeypatch.setattr(rt, "get_owner", lambda request: (EMAIL, "email"))
    ain.DEPS_FACTORY = lambda: world.live.deps
    ic.DEPS_FACTORY = lambda: world.live.cls_deps

    def default_retrieval(**kw):
        return [dict(HIST_ROW)]

    def get_evidence(**kw):
        world.retrieval_kwargs.append(kw)
        return (retrieval or default_retrieval)(**kw)
    monkeypatch.setattr(rt, "get_evidence_with_topup", get_evidence)
    monkeypatch.setattr(rt, "get_matched_signals", lambda *a, **k: [])

    def analyze(q, pool, extra_context=None):
        world.model_pools.append(copy_rows(pool))
        world.model_contexts.append(extra_context)
        if analysis:
            return analysis(pool)
        return json.dumps(rs.ANSWER if pool else NO_RESULTS)

    monkeypatch.setattr(rt, "analyze_with_claude", analyze)
    seed_stream_message(chats, world.chat_id, query=query, **(msg_over or {}))
    monkeypatch.setattr(rt, "StreamingResponse", lambda gen, **kw: types.SimpleNamespace(body_iterator=gen))
    monkeypatch.setattr(rt, "STREAM_CHUNK_DELAY_SECONDS", 0, raising=False)            # typing-effect pacing off
    monkeypatch.setattr(rt, "RESPONSE_TIMEOUT", 4, raising=False)                      # an empty saved-post result is not waited on for 75-360 s
    monkeypatch.setattr(rt.config, "STRICT_WAIT_SECONDS", 2, raising=False)
    monkeypatch.setattr(rt.config, "NONBLOCKING_STREAM_ENABLED", nonblocking, raising=False)
    if nonblocking:
        # the real scan thread runs (acquire_scan); the stream's later polling loop would otherwise wait for a Google-fallback
        # attempt the stubs never resolve, so it is told the scan is complete (the strict-wait exit the stream already has)
        monkeypatch.setattr(rt, "strict_intent_mode", lambda: True)
        monkeypatch.setattr(rt, "get_scan_status", lambda *a, **k: {"complete": True})
    gen = iter(rt.stream_answer(_Req(), world.chat_id, "find crm buyers").body_iterator)
    events = []
    for line in gen:
        if isinstance(line, str) and line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    done = [e for e in events if e.get("done")]
    assert len(done) == 1, events[-3:]
    streamed = "".join(e["delta"] for e in events if isinstance(e.get("delta"), str))
    done[0]["answer"] = streamed                         # the answer the browser receives, in pieces, then renders
    return done[0], json.loads(streamed) if streamed else None, chats


def copy_rows(pool):
    return [dict(r) for r in pool or []]


def final_answer(done, chats, chat_id):
    return json.loads(chats.docs[(chat_id, EMAIL)]["messages"][0]["claude_answer"])


@pytest.fixture
def wf(app_env, on, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", 4, raising=False)
    return app_env


# ── 1/2/5: an active-lead prompt runs BOTH branches, concurrently, and gives one answer ─────────────────────────────────
def test_active_lead_prompt_triggers_both_branches_and_yields_one_combined_answer(wf, monkeypatch):
    world = World("s1")
    done, _unused, chats = run_stream(wf, monkeypatch, world)
    ans = final_answer(done, chats, "s1")
    # the historical branch ran through the existing range path with the SAME interval Live was given, and its analysis is the normal one
    (kw,) = world.retrieval_kwargs
    h = timerange.range_from_doc(kw["time_range"])
    assert kw["since_days"] is None and h is not None and h.end - h.start == datetime.timedelta(minutes=30)
    assert ans["format"] == "source_list" and ans["platforms"][0]["posts"][0]["link"] == HIST_URL
    # live branch ran once, with the web_search tool
    assert len(world.live.calls) == 1 and world.live.calls[0]["payload"]["tools"][0]["type"] == "web_search"
    # ONE answer: the live candidates are inside it, labelled, next to (not instead of) the historical analysis
    lc = ans["live_candidates"]
    assert {i["url"] for i in lc["items"]} == {LIVE1, LIVE2} and lc["live_results_limited_to_window"] is True
    assert json.loads(done["answer"]) == ans
    assert world.live.stored(0, "s1")["workflow"] == "active_intent"


def test_the_two_branches_run_concurrently_in_the_real_stream(wf, monkeypatch):
    """The saved-post search blocks until the live HTTP request has been made. If the live branch only started after the
    historical one finished, this would time out: it is the live request that lets the historical branch finish."""
    world = World("s1")
    live_called = threading.Event()
    first = world.live._post

    def post(url, headers, payload, timeout):
        live_called.set()
        return first(url, headers, payload, timeout)
    world.live.deps.post = post
    seen = {}

    def slow_retrieval(**kw):
        seen["live_started_before_historical_finished"] = live_called.wait(20)
        return [dict(HIST_ROW)]
    done, _u, chats = run_stream(wf, monkeypatch, world, retrieval=slow_retrieval, nonblocking=True)
    assert seen["live_started_before_historical_finished"] is True
    assert "live_candidates" in final_answer(done, chats, "s1")


def test_the_model_never_sees_live_links_and_cards_stay_historical(wf, monkeypatch):
    world = World("s1")
    done, _u, chats = run_stream(wf, monkeypatch, world)
    seen = json.dumps(world.model_pools) + json.dumps(world.model_contexts)
    assert LIVE1 not in seen and LIVE2 not in seen and "live" not in seen.lower().replace("deliver", "")
    assert [r["post_url"] for r in done["results"]] == [HIST_URL]                 # result cards: historical only
    ans = final_answer(done, chats, "s1")
    assert ans["counts"]["retrieved"] == 1 and ans["counts"]["unverified"] == 0   # integrity accounting never saw the live items


# ── 3/4: either branch empty or failing leaves the other intact ─────────────────────────────────────────────────────────
def test_historical_zero_still_runs_live_and_the_answer_is_built_on_live_evidence(wf, monkeypatch):
    world = World("s1")
    done, _u, chats = run_stream(wf, monkeypatch, world, retrieval=lambda **kw: [])
    ans = final_answer(done, chats, "s1")
    assert len(world.live.calls) == 1 and ans["format"] == "no_results"
    assert len(ans["live_candidates"]["items"]) == 2 and "No analysed saved-post evidence" in ans["live_candidates"]["statement"]
    assert "platforms" not in ans or not ans["platforms"]                      # nothing historical was invented


def test_live_zero_leaves_the_historical_answer_untouched(wf, monkeypatch):
    world = World("s1", live_items=())
    done, _u, chats = run_stream(wf, monkeypatch, world)
    ans = final_answer(done, chats, "s1")
    assert len(world.live.calls) == 1 and "live_candidates" not in ans
    assert ans["platforms"][0]["posts"][0]["link"] == HIST_URL


def test_both_branches_empty_reports_no_evidence_and_invents_nothing(wf, monkeypatch):
    world = World("s1", live_items=())
    done, _u, chats = run_stream(wf, monkeypatch, world, retrieval=lambda **kw: [])
    ans = final_answer(done, chats, "s1")
    assert ans["format"] == "no_results" and "live_candidates" not in ans and not ans.get("platforms")


def test_duplicate_urls_across_branches_are_removed(wf, monkeypatch):
    world = World("s1", live_items=((HIST_URL, "Need a CRM"), (LIVE1, "Which CRM should I use?"), (LIVE1, "dup")))
    done, _u, chats = run_stream(wf, monkeypatch, world)
    lc = final_answer(done, chats, "s1")["live_candidates"]
    assert [i["url"] for i in lc["items"]] == [LIVE1] and "duplicate" in lc["statement"]


@pytest.mark.parametrize("name,setup", [
    ("budget database down", lambda w: setattr(w.live.deps, "budget", type("B", (), {"find_one_and_update": lambda s, *a, **k: (_ for _ in ()).throw(RuntimeError("db"))})())),
    ("cooldown active", lambda w: w.live.budget.insert_one({"_id": "day:" + w.live.clock().strftime("%Y-%m-%d"), "count": 1,
                                                          "owners": {rl._owner_hash(EMAIL): 1}, "last": {rl._owner_hash(EMAIL): w.live.clock()}})),
    ("live timeout", lambda w: setattr(w.live, "raises", TimeoutError("slow"))),
    ("200 with citations but no web_search_call", lambda w: setattr(w.live, "response", resp([(LIVE1, "t")], with_call=False)))])
def test_live_failure_or_refusal_keeps_the_historical_answer(wf, monkeypatch, name, setup):
    world = World("s1")
    setup(world)
    done, _u, chats = run_stream(wf, monkeypatch, world)
    ans = final_answer(done, chats, "s1")
    assert "live_candidates" not in ans and ans["platforms"][0]["posts"][0]["link"] == HIST_URL
    assert ans["counts"]["retrieved"] == 1


def test_in_flight_limit_blocks_live_but_not_the_historical_answer(wf, monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", 1, raising=False)
    assert rl.INFLIGHT.try_enter(1)
    try:
        world = World("s1")
        done, _u, chats = run_stream(wf, monkeypatch, world)
    finally:
        rl.INFLIGHT.leave()
    ans = final_answer(done, chats, "s1")
    assert world.live.calls == [] and "live_candidates" not in ans and ans["platforms"][0]["posts"][0]["link"] == HIST_URL


def test_historical_failure_is_not_turned_into_fabricated_results_and_live_survives(wf, monkeypatch):
    world = World("s1")

    def broken(**kw):
        raise RuntimeError("saved-post search failed")
    done, _u, chats = run_stream(wf, monkeypatch, world, retrieval=broken, nonblocking=False)
    ans = final_answer(done, chats, "s1")
    assert ans["format"] == "no_results" and not ans.get("platforms")                  # no historical row was made up
    assert ans["live_candidates"]["items"][0]["verification_status"] == "date_verified_only"   # the live result was kept, labelled
    assert all(i["source"] == "live_web_search" for i in ans["live_candidates"]["items"])


# ── 6/7/8/12: provenance, unverified status, no window / date claims ─────────────────────────────────────────────────────
def test_live_candidates_are_labelled_date_verified_only_with_no_intent_claim(wf, monkeypatch):
    world = World("s1")
    done, _u, chats = run_stream(wf, monkeypatch, world)
    lc = final_answer(done, chats, "s1")["live_candidates"]
    assert lc["verification"] == "date_verified_title_and_url_only"
    for item in lc["items"]:
        assert item["verification_status"] == "date_verified_only" and item["evidence"] == "title_url_and_verified_date_only"
        assert item["source"] == "live_web_search" and item["date_verified"] is True and item["published_at"]
        assert item["intent"] == "demand" and not ({"score", "summary", "post_text"} & set(item))
    assert "has not read these posts" in lc["statement"] and "30-minute live search window" in lc["statement"]
    assert "not limited" not in lc["statement"]
    ranks = [i["title_relevance"] for i in lc["items"]]
    assert ranks == sorted(ranks, reverse=True)


# ── 9/10/11: the window is applied only where intended, and explicit ranges are preserved ──────────────────────────────
def test_a_normal_research_prompt_is_completely_unchanged(wf, monkeypatch):
    on_world, off_world = World("s1", query=NOT_LEAD), World("s1", query=NOT_LEAD)
    done_on, _u, chats_on = run_stream(wf, monkeypatch, on_world, query=NOT_LEAD)
    assert on_world.live.calls == [] and on_world.retrieval_kwargs[0].get("time_range") is None
    assert "live_candidates" not in final_answer(done_on, chats_on, "s1")
    # and byte-identical to the same prompt with the whole workflow switched off
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", False, raising=False)
    done_off, _u2, chats_off = run_stream(wf, monkeypatch, off_world, query=NOT_LEAD)
    assert final_answer(done_on, chats_on, "s1") == final_answer(done_off, chats_off, "s1")
    pick = lambda kw: {k: kw.get(k) for k in ("keywords", "evidence_required", "match_phrases", "targeting_platform", "since_days",
                                               "time_range", "unfiltered", "user_query", "topic_key", "chat_id")}
    assert pick(on_world.retrieval_kwargs[0]) == pick(off_world.retrieval_kwargs[0])


def test_workflow_off_means_even_an_active_lead_prompt_behaves_as_before(wf, monkeypatch):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", False, raising=False)
    world = World("s1")
    done, _u, chats = run_stream(wf, monkeypatch, world)
    ans = final_answer(done, chats, "s1")
    assert world.live.calls == [] and "live_candidates" not in ans and world.retrieval_kwargs[0].get("time_range") is None


def test_an_explicit_time_range_is_preserved_exactly_for_historical_and_live_runs_independently(wf, monkeypatch):
    explicit = {"start": datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc), "end": datetime.datetime(2026, 10, 8, tzinfo=datetime.timezone.utc),
                "tz": "UTC", "kind": "date_range", "phrase": "October 1 to October 7", "capped": False, "note": "",
                "reference": datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc), "display": "Oct 1 - Oct 7"}
    world = World("s1")
    done, _u, chats = run_stream(wf, monkeypatch, world, msg_over={"time_range": explicit})
    assert world.retrieval_kwargs[0]["time_range"] == explicit                       # Historical: the user's range, byte for byte
    assert len(world.live.calls) == 1 and "live_candidates" in final_answer(done, chats, "s1")      # Live: its own (configured) window
    wdoc = world.live.stored(0, "s1")["window"]
    assert timerange.to_utc(wdoc["end"]) - timerange.to_utc(wdoc["start"]) == datetime.timedelta(minutes=30)


def test_an_explicit_day_window_is_preserved_for_historical_and_live_runs_independently(wf, monkeypatch):
    world = World("s1")
    done, _u, chats = run_stream(wf, monkeypatch, world, msg_over={"time_window_days": 7})
    kw = world.retrieval_kwargs[0]
    assert kw["since_days"] == 7 and kw.get("time_range") is None            # Historical: the user's period, exactly
    assert len(world.live.calls) == 1 and "live_candidates" in final_answer(done, chats, "s1")           # Live: its own window


def test_the_window_override_is_never_persisted(wf, monkeypatch):
    world = World("s1")
    _d, _u, chats = run_stream(wf, monkeypatch, world)
    stored = chats.docs[("s1", EMAIL)]["messages"][0]
    assert stored["time_range"] is None and stored["time_window_days"] is None


@pytest.mark.parametrize("minutes", [30, 1500])
def test_live_and_historical_get_the_same_configured_interval_in_the_real_stream(wf, monkeypatch, minutes):
    enable(monkeypatch, minutes=minutes)
    world = World("s1")
    done, _u, chats = run_stream(wf, monkeypatch, world)
    kw = world.retrieval_kwargs[0]
    h = timerange.range_from_doc(kw["time_range"])                                         # Historical: the existing explicit-range path
    wdoc = world.live.stored(0, "s1")["window"]
    start, end = timerange.to_utc(wdoc["start"]), timerange.to_utc(wdoc["end"])
    assert (h.start, h.end) == (start, end) and end - start == datetime.timedelta(minutes=minutes)      # one UTC interval, no 40-minute cap
    payload = json.dumps(world.live.calls[0]["payload"])
    assert wdoc["start"] in payload and wdoc["end"] in payload                             # the exact boundaries given to the model
    lc = final_answer(done, chats, "s1")["live_candidates"]
    assert lc["window"]["minutes"] == minutes and timerange.to_utc(lc["window"]["start"]) == start
    stored = chats.docs[("s1", EMAIL)]["messages"][0]
    assert stored.get("time_range") is None                                                 # the shared interval is never persisted


def test_guests_and_inactive_live_get_no_window_and_no_live_call(wf, monkeypatch):
    world = World("s1")
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_LINKS_ENABLED", False, raising=False)
    done, _u, chats = run_stream(wf, monkeypatch, world)
    assert world.live.calls == [] and world.retrieval_kwargs[0].get("time_range") is None and "live_candidates" not in final_answer(done, chats, "s1")


def test_live_is_claimed_once_per_message_even_if_the_stream_runs_again(wf, monkeypatch):
    world = World("s1")
    same = {"requested_at": datetime.datetime.utcnow()}                    # a resumed stream reads the SAME stored message
    run_stream(wf, monkeypatch, world, msg_over=same)
    world2_calls = len(world.live.calls)
    done, _u, chats = run_stream(wf, monkeypatch, world, msg_over=same)                   # same message / same claim: a resumed or repeated run
    assert world2_calls == 1 and len(world.live.calls) == 1
    assert "live_candidates" in final_answer(done, chats, "s1")


# ── the background path ────────────────────────────────────────────────────────────────────────────────────────────────
def background(env, monkeypatch, world, matched, query=CRM, msg_over=None):
    lg, idx, chats = env
    ain.DEPS_FACTORY = lambda: world.live.deps
    ic.DEPS_FACTORY = lambda: world.live.cls_deps
    captured = {}

    def analyze(q, pool, extra_context=None):
        captured["pool"] = copy_rows(pool)
        return json.dumps(rs.ANSWER if pool else NO_RESULTS)
    monkeypatch.setattr(idx, "analyze_with_claude", analyze)
    chats.update_one({"chat_id": "c1", "owner_key": EMAIL}, {"$set": {"summary": ""}})
    with lg._usage_attribution(request_id="req-1"):
        idx.add_search_to_chat("c1", EMAIL, query, lg.normalize_topic_key(query), ["crm"], "reddit", evidence_required=15)
    msg = chats.find_one({"chat_id": "c1", "owner_key": EMAIL})["messages"][-1]
    msg["requested_at"] = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    msg["credit_request_id"] = "cr-1"
    msg.update(msg_over or {})
    idx._complete_message_answer_and_results("c1", EMAIL, msg, matched)
    return chats.find_one({"chat_id": "c1", "owner_key": EMAIL})["messages"][-1], captured


def bg_world(query=CRM, **kw):
    w = World("c1", query=query, **kw)
    return w


def recent(n, minutes_ago, url=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    return {"title": f"Need a CRM {n}", "post_text": "Looking for a CRM for my team, any recommendations?", "platform": "reddit",
            "post_url": url or f"https://www.reddit.com/r/crm/comments/b{n}b{n}b{n}/need_a_crm/",
            "created_utc": (now - datetime.timedelta(minutes=minutes_ago)).timestamp()}


@pytest.fixture
def bgenv(env, on, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", 4, raising=False)
    lg, idx, _old = env
    from tests.test_answer_persistence import FakeChats
    chats = FakeChats()
    monkeypatch.setattr(idx, "chats_collection", chats)
    return lg, idx, chats


def test_background_path_combines_both_branches_and_leaves_the_saved_posts_exactly_as_retrieved(bgenv, monkeypatch):
    world = bg_world()
    stored, seen = background(bgenv, monkeypatch, world, [recent(1, 5), recent(2, 25), recent(3, 90)])
    ans = json.loads(stored["claude_answer"])
    assert sorted(r["title"] for r in seen["pool"]) == ["Need a CRM 1", "Need a CRM 2", "Need a CRM 3"]      # nothing is windowed away
    assert len(world.live.calls) == 1 and {i["url"] for i in ans["live_candidates"]["items"]} == {LIVE1, LIVE2}
    assert stored["time_range"] is None and stored["time_window_days"] is None
    assert [r["post_url"] for r in stored["results"]] == [r["post_url"] for r in seen["pool"]]


def test_background_path_with_no_saved_posts_still_uses_live(bgenv, monkeypatch):
    world = bg_world()
    stored, seen = background(bgenv, monkeypatch, world, [])
    ans = json.loads(stored["claude_answer"])
    assert seen["pool"] == [] and ans["format"] == "no_results" and len(ans["live_candidates"]["items"]) == 2


def test_background_live_failure_keeps_the_historical_answer(bgenv, monkeypatch):
    world = bg_world()
    world.live.raises = TimeoutError("slow")
    stored, _seen = background(bgenv, monkeypatch, world, [recent(1, 5)])
    ans = json.loads(stored["claude_answer"])
    assert "live_candidates" not in ans and ans["platforms"][0]["posts"]


def test_background_path_ignores_normal_prompts_and_keeps_explicit_ranges_independent_of_live(bgenv, monkeypatch):
    world = bg_world(query=NOT_LEAD)
    stored, seen = background(bgenv, monkeypatch, world, [recent(1, 5), recent(2, 90)], query=NOT_LEAD)
    assert len(seen["pool"]) == 2 and world.live.calls == [] and "live_candidates" not in json.loads(stored["claude_answer"])
    world2 = bg_world()
    stored2, seen2 = background(bgenv, monkeypatch, world2, [recent(1, 5), recent(2, 90)], msg_over={"time_window_days": 7})
    assert len(seen2["pool"]) == 2 and len(world2.live.calls) == 1                     # the saved posts untouched; Live ran on its own window
    assert stored2["time_range"] is None                                                # Live's window is never written to the message


# ── the post-answer links endpoint does not show a second, duplicate section ─────────────────────────────────────────────
def test_the_old_links_section_stays_silent_for_a_workflow_message(wf, monkeypatch):
    world = World("s1")
    run_stream(wf, monkeypatch, world)
    again = rl.process(world.live.deps, owner_key=EMAIL, owner_type="email", chat_id="s1", request_id="req-1")
    assert again == {"state": "none"} and rl.render_links(world.live.stored(0, "s1")) == [] and len(world.live.calls) == 1
