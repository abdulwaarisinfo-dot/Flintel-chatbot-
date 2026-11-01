"""
tests/test_active_intent.py — active lead intent (active_intent.py) and the pre-answer mode of reddit_live.py. UNIT level.

Everything is mocked: mongomock, a fake callable in place of the OpenAI HTTP call, a made-up key. No network, no real OpenAI
request, no production database. What these tests can NOT show: that the real web_search tool accepts the request, what Reddit
URLs it returns, how fresh they are, or what a call costs.
"""
import copy
import json
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest

mongomock = pytest.importorskip("mongomock")
import active_intent as ain  # noqa: E402
import reddit_live as rl  # noqa: E402
import timerange  # noqa: E402
from tests.test_reddit_live import Env, OWNER, URL1, message, on  # noqa: F401,E402  (builders + the Live fixture)

CRM = "Find people on Reddit who are actively looking for a CRM or asking for recommendations for a CRM for their business."
URL2 = "https://www.reddit.com/r/smallbusiness/comments/def456/which_crm_should_i_use/"
URL3 = "https://www.reddit.com/r/startups/comments/ghi789/need_help_picking_software/"
HURL = "https://www.reddit.com/r/sales/comments/hhh111/looking_for_a_crm/"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def _no_inflight_leak():
    assert rl.INFLIGHT.count == 0
    yield
    assert rl.INFLIGHT.count == 0, "an in-flight slot was leaked"


@pytest.fixture
def ai_on(on, monkeypatch):
    """Workflow on (Live already on via `on`), 30-minute window, a short wait for the live branch."""
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", True, raising=False)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", 30, raising=False)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_LIVE_WAIT_SECONDS", 5, raising=False)
    return ain


def resp(items, *, call_status="completed", with_call=True, status="completed"):
    """A Responses-API-shaped dict: [(url, title)] as citations (+ sources), with or without a web_search_call item."""
    out = []
    if with_call:
        out.append({"type": "web_search_call", "id": "ws_1", "status": call_status,
                    "action": {"type": "search", "sources": [{"type": "url", "url": u} for u, _ in items]}})
    out.append({"type": "message", "content": [{"type": "output_text", "text": "MODEL PROSE NEVER SHOWN",
                "annotations": [{"type": "url_citation", "url": u, "title": t, "start_index": 0, "end_index": 1} for u, t in items]}]})
    return {"id": "resp_1", "status": status, "output": out, "usage": {"input_tokens": 100, "output_tokens": 50}}


def live_env(items=((URL1, "Looking for a CRM for my small business"),), *, msg_over=None, **resp_kw):
    """A Live backend whose stored message is the UNANSWERED active-lead search the workflow runs on."""
    over = dict(claude_answer=None, answered_at=None, query=CRM, resolved_query=None)
    over.update(msg_over or {})
    env = Env([message(0, **over)])
    env.response = resp(list(items), **resp_kw)
    return env


def stored_msg(env):
    return copy.deepcopy(env.chats.find_one({"chat_id": "c1"})["messages"][0])


def prepare(env, mode="stream", msg=None, monkeypatch=None):
    ain.DEPS_FACTORY = lambda: env.deps
    return ain.Run.prepare(msg if msg is not None else stored_msg(env), owner_key=OWNER, chat_id="c1", mode=mode)


@pytest.fixture(autouse=True)
def _reset_factory():
    yield
    ain.DEPS_FACTORY = None


def hrow(n=1, text="We are actively looking for a CRM, any recommendations?", url=None, age_minutes=5):
    return {"title": f"Looking for a CRM {n}", "post_text": text, "post_url": url or f"https://www.reddit.com/r/sales/comments/h{n}h{n}h{n}/t/",
            "platform": "reddit", "created_utc": (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).timestamp()}


def answer_json(*links, fmt="source_list"):
    data = {"format": fmt, "executive_summary": "Analysed the saved posts.", "platforms": [
        {"platform": "reddit", "posts": [{"index": i + 1, "link": l, "title": "t"} for i, l in enumerate(links)]}] if links else []}
    return json.dumps(data)


# ── the predicate ──────────────────────────────────────────────────────────────────────────────────────────────────────
POSITIVE = [
    CRM,
    "Find people actively looking for a CRM",
    "Show me businesses that need a CRM",
    "Find people asking which CRM they should use",
    "Find people asking for CRM recommendations",
    "Find people looking for alternatives to HubSpot",
    "Who is looking to switch from Salesforce to something cheaper?",
    "Find founders actively seeking a solution to manage customer onboarding",
    "List teams in the market for project management software",
    "Find small business owners who need help choosing accounting software",
    "Get me companies currently searching for a helpdesk tool",
    "Find users asking for recommendations for invoicing software",
    "Find people who want to replace their current CRM",
    "Find anyone looking for a scheduling app",
]
NEGATIVE = [
    "What is a CRM?",
    "How does a CRM work?",
    "Explain the benefits of CRM software for small businesses",
    "Best CRM for small business",
    "How do I choose a CRM for my business?",
    "What do people on Reddit say about HubSpot?",
    "Find the history of CRM software",
    "Find posts about CRM trends",
    "Find people complaining about HubSpot pricing",
    "Show me CRM tutorials for businesses",
    "Find people who need a CRM tutorial",
    "Find CRM jobs for people looking for work",
    "Compare Salesforce and HubSpot",
    "Summarize last week's discussion about project management tools",
    "Which CRM should I use?",
    "",
    "   ",
]


@pytest.mark.parametrize("text", POSITIVE)
def test_active_lead_intent_prompts_are_detected(text):
    assert ain.is_active_lead_intent(text) == (True, "active_lead_intent")


@pytest.mark.parametrize("text", NEGATIVE)
def test_informational_and_non_lead_prompts_are_not_detected(text):
    assert ain.is_active_lead_intent(text)[0] is False


@pytest.mark.parametrize("value", [None, 5, [], {}, b"find people looking for a crm"])
def test_predicate_is_total(value):
    assert ain.is_active_lead_intent(value)[0] is False


# ── the window ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("configured,expected", [(30, 30), (10, 10), (40, 40), (41, 40), (90, 40), (1000, 40), (0, 30), (-5, 30),
                                                 ("x", 30), (None, 30), (True, 30), (2.5, 30)])
def test_window_is_configurable_but_never_more_than_forty_minutes(monkeypatch, configured, expected):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", configured, raising=False)
    assert ain.window_minutes() == expected


def test_recent_window_is_a_half_open_range_in_the_type_a_typed_range_uses(ai_on):
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    tr = ain.recent_window(now)
    assert (tr.end - tr.start) == timedelta(minutes=30) and tr.end == now and tr.start == now - timedelta(minutes=30)
    doc = tr.to_doc(now)
    back = timerange.range_from_doc(doc)
    assert back.start == tr.start and back.end == tr.end
    assert back.contains(now - timedelta(minutes=29)) and not back.contains(now - timedelta(minutes=31)) and not back.contains(now)


# ── eligibility / preparing a run ──────────────────────────────────────────────────────────────────────────────────────────
def test_disabled_by_default_changes_nothing(on):
    env = live_env()
    msg = stored_msg(env)
    run = ain.Run.prepare(msg, owner_key=OWNER, chat_id="c1")
    assert run.active is False and msg == stored_msg(env)
    assert run.finish("ANSWER", historical=[]) == "ANSWER" and run.historical([1, 2]) == [1, 2]
    run.start()
    assert env.calls == []


def test_eligible_active_lead_search_gets_the_recent_window_in_memory_only(ai_on):
    env = live_env()
    msg = stored_msg(env)
    assert msg["time_range"] is None and msg["time_window_days"] is None
    run = prepare(env, msg=msg)
    assert run.active is True
    tr = timerange.range_from_doc(msg["time_range"])
    assert tr is not None and (tr.end - tr.start) == timedelta(minutes=30)
    assert env.chats.find_one({"chat_id": "c1"})["messages"][0]["time_range"] is None       # never persisted
    assert msg["time_window_days"] is None


@pytest.mark.parametrize("minutes,expected", [(90, 40), (15, 15), ("bad", 30)])
def test_the_applied_window_follows_the_configuration_and_its_cap(ai_on, monkeypatch, minutes, expected):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", minutes, raising=False)
    env = live_env()
    msg = stored_msg(env)
    prepare(env, msg=msg)
    tr = timerange.range_from_doc(msg["time_range"])
    assert (tr.end - tr.start) == timedelta(minutes=expected)


@pytest.mark.parametrize("over", [
    {"time_range": {"start": datetime(2026, 10, 1, tzinfo=timezone.utc), "end": datetime(2026, 10, 8, tzinfo=timezone.utc), "tz": "UTC"}},
    {"time_window_days": 7}, {"time_window_days": 30}, {"request_kind": "research"}, {"request_kind": "mention_search"},
    {"request_kind": "provider_search"}, {"website_only": True}, {"website_context": {"url": "https://x.example"}},
    {"keywords": []}, {"credit_request_id": None}, {"evidence_required": None}, {"query": "What is a CRM?"}])
def test_ineligible_searches_are_left_exactly_as_they_were(ai_on, over):
    env = live_env(msg_over=over)
    msg = stored_msg(env)
    before = copy.deepcopy(msg)
    run = prepare(env, msg=msg)
    assert run.active is False and msg == before                  # incl. every explicit time range, exactly as provided
    run.start()
    assert run.finish("ANSWER", historical=[]) == "ANSWER" and env.calls == []


def test_a_guest_owner_is_never_eligible(ai_on):
    env = live_env()
    ain.DEPS_FACTORY = lambda: env.deps
    msg = stored_msg(env)
    run = ain.Run.prepare(msg, owner_key="anon-guest-id", chat_id="c1", mode="stream")
    assert run.active is False and msg["time_range"] is None


def test_nothing_happens_while_the_live_feature_is_inactive(ai_on, monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_LINKS_ENABLED", False, raising=False)
    env = live_env()
    msg = stored_msg(env)
    run = prepare(env, msg=msg)
    assert run.active is False and msg["time_range"] is None and env.calls == []


@pytest.mark.parametrize("bad", [None, "x", 5, []])
def test_prepare_never_raises(ai_on, bad):
    assert ain.Run.prepare(bad, owner_key=OWNER, chat_id="c1").active is False


# ── normalise, deduplicate, rank ─────────────────────────────────────────────────────────────────────────────────────────
def test_historical_and_live_items_keep_their_provenance_and_status():
    hist = ain.normalize_historical([hrow(1), {"title": "stub", "post_text": None, "post_url": "https://www.reddit.com/r/x/comments/stub11/z/"}])
    live = ain.normalize_live([{"url": URL1, "title": "A title"}, {"url": "https://evil.com/x", "title": "bad"}, {"url": None}])
    assert [e.source_type for e in hist] == ["historical", "historical"] and [e.source_type for e in live] == ["live_web_search"]
    assert hist[0].has_content is True and hist[0].verification == "verified" and hist[0].published_at
    assert hist[1].has_content is False and hist[1].verification == "unverified"
    assert live[0].has_content is False and live[0].verification == "unverified" and live[0].published_at is None
    assert live[0].relevance is None


def test_title_relevance_is_lexical_overlap_on_the_title_only():
    terms = ain._terms("Find people actively looking for a CRM", "crm")
    assert terms == ["crm"]                                            # request words, stop words and generic words are not terms
    assert ain.title_relevance(terms, "Looking for a CRM") == 1.0 and ain.title_relevance(terms, "Anyone use Notion?") == 0.0
    assert ain.title_relevance(["crm", "invoicing"], "Need a CRM today") == 0.5
    assert ain.title_relevance(terms, None) == 0.0 and ain.title_relevance([], "CRM") == 0.0


def test_duplicates_are_removed_and_live_items_are_ranked_by_title_match():
    hist = ain.normalize_historical([hrow(1, url=HURL)])
    live = ain.normalize_live([{"url": URL2, "title": "Which CRM should I use for my agency?"},
                               {"url": HURL + "?utm_source=x", "title": "same post as the saved one"},
                               {"url": URL3, "title": "Need help picking software"},
                               {"url": URL2, "title": "duplicate inside live"},
                               {"url": "https://old.reddit.com/r/startups/comments/jkl012/crm_for_startups/", "title": "CRM for startups"}])
    kept, dups = ain.combine(hist, live, ain._terms("crm"))
    assert dups == 2 and [c.url for c in kept][0] != HURL
    assert [c.relevance for c in kept] == sorted([c.relevance for c in kept], reverse=True)
    assert kept[-1].url == URL3 and kept[-1].relevance == 0.0           # no title overlap: last, never dropped or promoted
    assert HURL not in [c.url for c in kept] and "live_web_search" in hist[0].also_found_by


def test_at_most_eight_candidates_are_kept():
    live = ain.normalize_live([{"url": f"https://www.reddit.com/r/a_{i}/comments/abc{i:03d}/t/", "title": "CRM"} for i in range(15)])
    assert len(ain.combine([], live, ["crm"])[0]) == ain.MAX_LIVE_CANDIDATES


# ── the block that goes into the answer ───────────────────────────────────────────────────────────────────────────────────
def test_block_is_honest_about_what_a_live_candidate_is():
    cands = ain.combine([], ain.normalize_live([{"url": URL1, "title": "Looking for a CRM"}]), ["crm"])[0]
    block = ain.build_block(cands, historical_count=0, window=30, duplicates_removed=0, historical_state="completed")
    assert block["verification"] == "unverified_title_and_url_only" and block["live_results_limited_to_window"] is False
    item = block["items"][0]
    assert set(item) == {"rank", "title", "url", "source", "verification_status", "evidence", "title_relevance"}
    assert item["verification_status"] == "unverified" and item["evidence"] == "title_and_url_only" and item["source"] == "live_web_search"
    st = block["statement"]
    assert "has not read these posts" in st and "verified their dates" in st and "buying" in st
    assert "not limited to the last 30 minutes" in st and "ranked by how well their titles match" in st
    assert "No analysed saved-post evidence" in st and "just now" not in st.lower()


@pytest.mark.parametrize("hist_count,state,needle", [(3, "completed", "analysed in the answer above"),
                                                     (0, "completed", "No analysed saved-post evidence"),
                                                     (0, "timed_out", "did not finish in time")])
def test_block_describes_the_historical_side_without_inventing_anything(hist_count, state, needle):
    cands = ain.combine([], ain.normalize_live([{"url": URL1, "title": "CRM"}]), ["crm"])[0]
    assert needle in ain.build_block(cands, historical_count=hist_count, window=30, duplicates_removed=0, historical_state=state)["statement"]


def test_attach_adds_the_block_to_a_json_answer_and_leaves_anything_else_alone():
    block = {"statement": "s", "items": []}
    out, outcome = ain.attach(answer_json(HURL), block)
    assert outcome == "attached" and json.loads(out)["live_candidates"] == block and json.loads(out)["platforms"][0]["posts"][0]["link"] == HURL
    for text in ("plain prose", "[1,2]", "", None):
        assert ain.attach(text, block) == (text, "answer_not_json")


# ── the pre-answer mode of reddit_live (the live branch) ─────────────────────────────────────────────────────────────────
def pre(env, owner=OWNER):
    return rl.process(env.deps, owner_key=owner, owner_type="email", chat_id="c1", request_id="r0", pre_answer=True)


def test_pre_answer_success_needs_a_completed_web_search_call_and_a_validated_url(on):
    env = live_env()
    res = pre(env)
    assert res["state"] == "ok" and res["reason"] == "ok" and res["web_search_calls"] == 1 and res["validated_urls"] == 1
    assert res["http_status"] == 200 and res["links"][0]["url"] == URL1
    payload = env.calls[0]["payload"]
    assert payload["tools"][0]["type"] == "web_search" and payload["tools"][0]["filters"] == {"allowed_domains": ["reddit.com"]}
    assert payload["tool_choice"] == "required" and payload["max_tool_calls"] == 1 and payload["include"] == ["web_search_call.action.sources"]
    assert env.stored()["workflow"] == "active_intent" and env.stored()["state"] == "ok"
    att = env.attempts.find_one({})
    assert att["completed_search_calls"] == 1 and att["outcome"] == "ok"


@pytest.mark.parametrize("name,kw,reason", [
    ("citations but no web_search_call item", dict(with_call=False), "no_completed_web_search_call"),
    ("search call not completed", dict(call_status="in_progress"), "no_completed_web_search_call"),
    ("search call failed", dict(call_status="failed"), "no_completed_web_search_call"),
    ("response status failed", dict(status="failed"), "response_failed")])
def test_http_200_with_citations_alone_is_not_a_success(on, name, kw, reason):
    env = live_env(**kw)
    res = pre(env)
    assert res["state"] == "none" and res["reason"] == reason and "links" not in res
    assert env.stored()["state"] == "error" and not env.stored().get("links")
    assert env.attempts.find_one({})["outcome"] == reason and len(env.calls) == 1


def test_a_completed_search_with_no_valid_reddit_urls_is_empty_not_ok(on):
    env = live_env(items=[("https://evil.com/x", "bad"), ("https://www.reddit.com/user/someone", "profile")])
    res = pre(env)
    assert res["state"] == "none" and res["reason"] == "no_valid_reddit_urls" and res["web_search_calls"] == 1 and res["validated_urls"] == 0
    assert env.stored()["state"] == "empty"


def test_the_post_answer_endpoint_rules_are_unchanged(on):
    """Same completed-call requirement, but its replies carry no extra keys and an answered message is required."""
    env = Env([message(0)])
    env.response = resp([(URL1, "t")], with_call=False)
    assert env.run() == {"state": "none"} and env.stored()["state"] == "error"
    env2 = Env([message(0)])
    env2.response = resp([(URL1, "t")])
    out = env2.run()
    assert out["state"] == "ok" and "reason" not in out and "workflow" not in env2.stored()
    unanswered = Env([message(0, claude_answer=None, answered_at=None)])
    assert unanswered.run()["state"] == "ineligible" and unanswered.calls == []


def test_a_workflow_record_never_produces_a_second_links_section(on):
    env = live_env()
    pre(env)
    assert env.run() == {"state": "none"}                              # the post-answer endpoint reports nothing for it
    assert rl.render_links(env.stored()) == [] and len(env.calls) == 1
    assert pre(env)["links"][0]["url"] == URL1 and len(env.calls) == 1  # but a repeated pre-answer ask reads it, without a call


@pytest.mark.parametrize("name,setup,reason", [
    ("budget database down", lambda env: setattr(env.deps, "budget", type("B", (), {"find_one_and_update": lambda s, *a, **k: (_ for _ in ()).throw(RuntimeError("db"))})()), "budget_or_cooldown_refused"),
    ("api timeout", lambda env: setattr(env, "raises", TimeoutError("slow")), "api_error"),
])
def test_pre_answer_failures_report_a_safe_reason_and_never_raise(on, name, setup, reason):
    env = live_env()
    setup(env)
    res = pre(env)
    assert res["state"] == "none" and res["reason"] == reason and "links" not in res


def test_owner_cooldown_and_in_flight_limit_are_reported_with_their_reasons(on, monkeypatch):
    env = live_env()
    owner_hash = rl._owner_hash(OWNER)
    env.budget.insert_one({"_id": "day:" + env.clock().strftime("%Y-%m-%d"), "count": 1, "owners": {owner_hash: 1}, "last": {owner_hash: env.clock()}})
    assert pre(env)["reason"] == "budget_or_cooldown_refused" and env.calls == []
    env2 = live_env()
    assert rl.INFLIGHT.try_enter(2) and rl.INFLIGHT.try_enter(2)
    try:
        res = pre(env2)
    finally:
        rl.INFLIGHT.leave(); rl.INFLIGHT.leave()
    assert res["state"] == "busy" and res["reason"] == "in_flight_limit" and env2.calls == [] and env2.stored() is None


def test_pre_answer_is_signed_in_only_and_owner_isolated(on):
    env = live_env()
    assert rl.process(env.deps, owner_key="anon", owner_type="anon", chat_id="c1", request_id="r0", pre_answer=True)["reason"] == "guest_or_no_owner"
    assert rl.process(env.deps, owner_key="mallory@example.com", owner_type="email", chat_id="c1", request_id="r0", pre_answer=True)["state"] == "not_found"
    assert env.calls == []


@pytest.mark.parametrize("over", [{"time_range": {"start": 1}}, {"time_window_days": 7}, {"request_kind": "mention_search"}, {"website_only": True}])
def test_pre_answer_keeps_every_search_side_rule(on, over):
    env = live_env(msg_over=over)
    assert pre(env)["state"] == "ineligible" and env.calls == []


# ── Run end to end (live branch fake, historical side given) ─────────────────────────────────────────────────────────────
def test_both_branches_contribute_to_one_answer(ai_on):
    env = live_env(items=[(URL1, "Looking for a CRM for my small business"), (URL2, "Which CRM should I use?")])
    run = prepare(env)
    run.start()
    out = run.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    data = json.loads(out)
    assert data["platforms"][0]["posts"][0]["link"] == HURL                    # the historical analysis is untouched
    lc = data["live_candidates"]
    assert {i["url"] for i in lc["items"]} == {URL1, URL2} and all(i["source"] == "live_web_search" for i in lc["items"])
    assert all(i["verification_status"] == "unverified" for i in lc["items"]) and "analysed in the answer above" in lc["statement"]
    assert len(env.calls) == 1


def test_historical_zero_still_runs_live_and_the_answer_uses_the_live_evidence(ai_on):
    env = live_env()
    run = prepare(env)
    out = run.finish(json.dumps({"format": "no_results", "message": "No matching posts were found."}), historical=[])
    data = json.loads(out)
    assert data["format"] == "no_results" and data["live_candidates"]["items"][0]["url"] == URL1
    assert "No analysed saved-post evidence" in data["live_candidates"]["statement"] and len(env.calls) == 1


def test_live_zero_leaves_the_historical_answer_exactly_as_it_was(ai_on):
    env = live_env(items=[])
    run = prepare(env)
    original = answer_json(HURL)
    assert run.finish(original, historical=[hrow(1, url=HURL)]) == original and len(env.calls) == 1


def test_both_empty_reports_nothing_found_and_invents_nothing(ai_on):
    env = live_env(items=[])
    run = prepare(env)
    original = json.dumps({"format": "no_results", "message": "No matching posts were found."})
    assert run.finish(original, historical=[]) == original


@pytest.mark.parametrize("name,setup", [
    ("budget refused", lambda env: setattr(env.deps, "budget", type("B", (), {"find_one_and_update": lambda s, *a, **k: (_ for _ in ()).throw(RuntimeError("db"))})())),
    ("live timeout", lambda env: setattr(env, "raises", TimeoutError("slow"))),
    ("live server error", lambda env: setattr(env, "raises", ValueError("bad json"))),
    ("200 without a web_search_call", lambda env: setattr(env, "response", resp([(URL1, "t")], with_call=False)))])
def test_live_failure_never_discards_the_historical_answer(ai_on, name, setup):
    env = live_env()
    setup(env)
    run = prepare(env)
    original = answer_json(HURL)
    assert run.finish(original, historical=[hrow(1, url=HURL)]) == original


def test_live_blocked_by_the_in_flight_limit_or_cooldown_keeps_the_historical_answer(ai_on):
    env = live_env()
    owner_hash = rl._owner_hash(OWNER)
    env.budget.insert_one({"_id": "day:" + env.clock().strftime("%Y-%m-%d"), "count": 1, "owners": {owner_hash: 1}, "last": {owner_hash: env.clock()}})
    original = answer_json(HURL)
    assert prepare(env).finish(original, historical=[hrow(1, url=HURL)]) == original and env.calls == []


def test_a_live_branch_that_overruns_its_wait_is_left_behind_without_losing_the_answer(ai_on, monkeypatch):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_LIVE_WAIT_SECONDS", 1, raising=False)
    env = live_env()
    release = threading.Event()
    first = env._post

    def slow(url, headers, payload, timeout):
        assert release.wait(20)
        return first(url, headers, payload, timeout)
    env.deps.post = slow
    run = prepare(env)
    original = answer_json(HURL)
    try:
        assert run.finish(original, historical=[hrow(1, url=HURL)]) == original
    finally:
        release.set()
        run._thread.join(20)


def test_live_branch_does_not_wait_for_the_historical_branch(ai_on):
    """Live is started by start() and its request is made while the 'historical' side is still working. A design that waited for
    the historical result first would never reach the wait below."""
    env = live_env()
    called = threading.Event()
    first = env._post

    def post(url, headers, payload, timeout):
        called.set()
        return first(url, headers, payload, timeout)
    env.deps.post = post
    run = prepare(env)
    run.start()
    assert called.wait(10), "the live request was not made while the historical branch was still running"
    out = run.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    assert "live_candidates" in json.loads(out) and len(env.calls) == 1


def test_historical_failure_does_not_fabricate_results_or_pass_as_success(ai_on):
    env = live_env()
    run = prepare(env)
    out = json.loads(run.finish(json.dumps({"format": "no_results", "message": "No matching posts were found."}),
                                historical=[], historical_timed_out=True))
    st = out["live_candidates"]["statement"]
    assert "did not finish in time" in st and "No analysed saved-post evidence" not in st      # the failure is named, not hidden
    assert out["format"] == "no_results" and "platforms" not in out                           # no historical row was invented


def test_live_candidates_do_not_claim_the_historical_window_or_a_publication_date(ai_on):
    env = live_env()
    out = json.loads(prepare(env).finish(answer_json(), historical=[]))
    lc = out["live_candidates"]
    assert lc["live_results_limited_to_window"] is False and lc["historical_window_minutes"] == 30
    assert all("published_at" not in i and "date" not in i and "intent" not in " ".join(i) for i in lc["items"])
    assert "not limited to the last 30 minutes" in lc["statement"]
    payload = env.calls[0]["payload"]
    assert "date" not in json.dumps(payload["tools"]).lower() and "recency" not in json.dumps(payload).lower()


def test_duplicate_urls_are_removed_before_the_answer(ai_on):
    env = live_env(items=[(HURL, "Looking for a CRM"), (URL2, "Which CRM should I use?"), (URL2, "Which CRM should I use?")])
    out = json.loads(prepare(env).finish(answer_json(HURL), historical=[hrow(1, url=HURL)]))
    assert [i["url"] for i in out["live_candidates"]["items"]] == [URL2]
    assert "duplicate" in out["live_candidates"]["statement"]


def test_finish_is_idempotent_and_never_makes_a_second_paid_call(ai_on):
    env = live_env()
    run = prepare(env)
    first = run.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    assert run.finish(first, historical=[hrow(1, url=HURL)]) == first
    again = prepare(env).finish(answer_json(HURL), historical=[hrow(1, url=HURL)])         # e.g. a resumed stream
    assert json.loads(again)["live_candidates"]["items"][0]["url"] == URL1 and len(env.calls) == 1


def test_background_mode_filters_saved_posts_to_the_window_with_the_existing_range_filter(ai_on):
    env = live_env()
    run = prepare(env, mode="background")
    rows = [hrow(1, age_minutes=5), hrow(2, age_minutes=29), hrow(3, age_minutes=45), {"title": "undated", "post_text": "x", "post_url": "https://www.reddit.com/r/a/comments/und11/t/"}]
    kept = run.historical(rows)
    assert [r["title"] for r in kept] == ["Looking for a CRM 1", "Looking for a CRM 2"]
    run.finish(answer_json(), historical=kept)


def test_diagnostics_are_structured_and_carry_no_secrets(ai_on, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="flintel.active_intent")
    env = live_env()
    run = prepare(env)
    run.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    text = "\n".join(r.getMessage() for r in caplog.records)
    for event in ("intent_detected", "historical_branch_started", "live_branch_started", "historical_branch_completed",
                  "live_branch_completed", "evidence_combined", "final_response"):
        assert f'"event": "{event}"' in text, event
    assert "test-key-not-real" not in text and "Bearer" not in text and "Authorization" not in text and CRM not in text
    combined = [json.loads(r.getMessage().split("active_intent ", 1)[1]) for r in caplog.records if "evidence_combined" in r.getMessage()][0]
    assert combined["final_combined"] == 2 and combined["live"] == 1 and combined["historical"] == 1
    live_done = [json.loads(r.getMessage().split("active_intent ", 1)[1]) for r in caplog.records if "live_branch_completed" in r.getMessage()][0]
    assert live_done["web_search_calls"] == 1 and live_done["validated_urls"] == 1 and live_done["http_status"] == 200


def test_rejections_are_logged_with_a_reason(ai_on, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="flintel.active_intent")
    env = live_env(msg_over={"time_window_days": 7})
    prepare(env)
    ain.Run.prepare({"query": "What is a CRM?", "request_id": "r"}, owner_key=OWNER, chat_id="c1")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert '"event": "live_eligibility_rejected"' in text and '"reason": "time_bounded"' in text
    assert '"event": "intent_not_detected"' in text and '"reason": "informational"' in text
