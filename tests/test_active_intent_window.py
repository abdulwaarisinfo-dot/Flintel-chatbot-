"""
tests/test_active_intent_window.py — ONE configured time window for Active Intent: Historical retrieval and Live Reddit.
All mocked: no network, no OpenAI call, no production database. What these tests can NOT show: whether Reddit's real metadata
endpoint answers an unauthenticated server request, or how many in-window posts web search happens to find.
"""
import datetime
import copy
import json
import logging
import socket
import sys
from datetime import timedelta, timezone

import httpx as _REAL_HTTPX  # bound at import: the app_env/env fixtures stub sys.modules["httpx"]
import pytest

mongomock = pytest.importorskip("mongomock")
import active_intent as ain  # noqa: E402
import intent_classifier as ic  # noqa: E402
import reddit_live as rl  # noqa: E402
import timerange  # noqa: E402
from tests.test_active_intent import CRM, HURL, URL2, answer_json, hrow, live_env, prepare, stored_msg, ai_on  # noqa: F401,E402
from tests.test_reddit_live import OWNER, URL1, on  # noqa: F401,E402
from tests.test_reliability_ux_perf import env  # noqa: F401,E402

UTC = timezone.utc
END = datetime.datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
START = END - timedelta(minutes=30)


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


def deps_with(info):
    return rl.Deps(None, None, None, None, lambda: END, lambda *a: None, reddit_info=info)


def link(url=URL1):
    return {"url": url, "title": "t"}


def meta(created, sub="SaaS", removed=False):
    return {"created_utc": created, "subreddit": sub, "removed": removed}


# ── boundaries: [start, end) in tz-aware UTC ───────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("created,kept", [
    (START.timestamp(), True),                                   # start is INCLUDED
    ((START - timedelta(seconds=1)).timestamp(), False),         # one second older
    ((END - timedelta(seconds=1)).timestamp(), True),
    (END.timestamp(), False),                                    # end is EXCLUDED
    ((END + timedelta(minutes=5)).timestamp(), False),           # newer than the window
    (None, False), ("soon", False), (-1, False), (float("nan"), False), (True, False), ([], False),   # missing / malformed
])
def test_publication_is_checked_against_the_half_open_window(created, kept):
    out, stats = rl.verify_publication(deps_with(lambda ids, t: {"abc123": meta(created)}), [link()], (START, END))
    assert bool(out) is kept and stats["checked"] == 1
    if kept:
        assert timerange.to_utc(out[0]["published_at"]).tzinfo is not None


@pytest.mark.parametrize("minutes", [30, 1500, 10080])
def test_the_check_works_for_any_window_size(minutes):
    s = END - timedelta(minutes=minutes)
    info = {"abc123": meta((s + timedelta(seconds=1)).timestamp()), "def456": meta((s - timedelta(seconds=1)).timestamp(), "smallbusiness")}
    out, stats = rl.verify_publication(deps_with(lambda ids, t: info), [link(URL1), link(URL2)], (s, END))
    assert [l["url"] for l in out] == [URL1] and stats["outside_window"] == 1


def test_one_batched_lookup_only_for_the_found_post_ids():
    seen = []
    rl.verify_publication(deps_with(lambda ids, t: seen.append((ids, t)) or {}), [link(URL1), link(URL2)], (START, END))
    assert seen == [(["abc123", "def456"], rl.META_TIMEOUT_SECONDS)]            # the ids the model returned, never a listing


# ── fail closed ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_deleted_private_inaccessible_and_wrong_subreddit_posts_are_dropped():
    fresh = (END - timedelta(minutes=5)).timestamp()
    info = {"abc123": meta(fresh, removed=True), "def456": meta(fresh, sub="someotherplace")}        # ghi789 is simply not returned
    ghi = "https://www.reddit.com/r/startups/comments/ghi789/x/"
    out, stats = rl.verify_publication(deps_with(lambda ids, t: info), [link(URL1), link(URL2), link(ghi)], (START, END))
    assert out == [] and stats["removed"] == 1 and stats["unverifiable"] == 2


@pytest.mark.parametrize("exc", [TimeoutError("slow"), ConnectionError("down"), ValueError("malformed")])
def test_a_failed_lookup_verifies_nothing_and_never_raises(exc):
    def boom(ids, t):
        raise exc
    out, stats = rl.verify_publication(deps_with(boom), [link()], (START, END))
    assert out == [] and stats["lookup_failed"] == 1


@pytest.mark.parametrize("reply", [None, [], "x", 5])
def test_an_unexpected_lookup_reply_verifies_nothing(reply):
    assert rl.verify_publication(deps_with(lambda ids, t: reply), [link()], (START, END))[0] == []


@pytest.mark.parametrize("bad", [None, "", "https://evil.com/r/a/comments/abc123/x/", "https://www.reddit.com/user/someone", {}, 5])
def test_urls_without_a_post_id_are_unverifiable(bad):
    called = []
    out, stats = rl.verify_publication(deps_with(lambda ids, t: called.append(ids) or {}), [{"url": bad}], (START, END))
    assert out == [] and stats["unverifiable"] == 1 and called == []


def test_a_model_supplied_date_is_never_evidence():
    out, _ = rl.verify_publication(deps_with(lambda ids, t: {"abc123": meta((START - timedelta(hours=9)).timestamp())}),
                                   [{"url": URL1, "title": "posted 2 minutes ago", "published_at": END.isoformat(), "created_utc": END.timestamp()}],
                                   (START, END))
    assert out == []


# ── the real metadata request (httpx.MockTransport; nothing leaves the process) ───────────────────────────────────────────
def _mock_client(monkeypatch, handler):
    real = _REAL_HTTPX.Client
    monkeypatch.setitem(sys.modules, "httpx", _REAL_HTTPX)
    monkeypatch.setattr(_REAL_HTTPX, "Client", lambda **kw: real(transport=_REAL_HTTPX.MockTransport(handler), **{k: v for k, v in kw.items()}))


def test_reddit_info_asks_for_the_given_ids_only_and_parses_the_reply(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        body = {"data": {"children": [{"kind": "t3", "data": {"id": "abc123", "created_utc": 1.0, "subreddit": "x", "author": "a"}},
                                      {"kind": "t1", "data": {"id": "zzz"}}, "junk"]}}
        return _REAL_HTTPX.Response(200, json=body)
    _mock_client(monkeypatch, handler)
    out = rl._reddit_info(["abc123", "def456"], 8)
    assert out == {"abc123": {"created_utc": 1.0, "subreddit": "x", "removed": False}}
    assert len(seen) == 1 and seen[0].url.host == "api.reddit.com" and seen[0].url.params["id"] == "t3_abc123,t3_def456"
    assert "Authorization" not in seen[0].headers and "Cookie" not in seen[0].headers


@pytest.mark.parametrize("status", [403, 429, 500, 302])
def test_reddit_info_raises_on_http_errors_and_redirects(monkeypatch, status):
    _mock_client(monkeypatch, lambda req: _REAL_HTTPX.Response(status, headers={"location": "https://evil.example/"}))
    with pytest.raises(Exception):
        rl._reddit_info(["abc123"], 8)


def test_reddit_info_rejects_a_malformed_body(monkeypatch):
    _mock_client(monkeypatch, lambda req: _REAL_HTTPX.Response(200, content=b"<html>not json"))
    with pytest.raises(Exception):
        rl._reddit_info(["abc123"], 8)


# ── Live end to end (mocked OpenAI + metadata) ─────────────────────────────────────────────────────────────────────────────
def test_the_model_gets_the_exact_utc_boundaries_and_stays_reddit_only_with_one_call(ai_on):
    env = live_env()
    run = prepare(env)
    run.start()
    run.finish(answer_json(), historical=[])
    payload = env.calls[0]["payload"]
    rec = env.chats.find_one({"chat_id": "c1"})["messages"][0]["live_reddit"]["window"]
    assert rec["start"] in payload["instructions"] and rec["end"] in payload["instructions"]
    assert rec["start"] in payload["input"] and rec["end"] in payload["input"]
    assert rec["start"].endswith("Z") and rec["end"].endswith("Z") and "(exclusive)" in payload["instructions"]
    assert payload["tools"] == [{"type": "web_search", "filters": {"allowed_domains": ["reddit.com"]}}]
    assert payload["max_tool_calls"] == 1 and len(env.calls) == 1


@pytest.mark.parametrize("age,shown", [(5, True), (45, False), (-30, False)])
def test_only_posts_verified_inside_the_window_are_shown(ai_on, age, shown):
    env = live_env()
    env.ages["abc123"] = age
    out = json.loads(prepare(env).finish(answer_json(HURL), historical=[hrow(1, url=HURL)]))
    assert ("live_candidates" in out) is shown
    assert out["platforms"]                                                     # the historical answer is intact either way


def test_old_new_and_missing_candidates_are_filtered_individually(ai_on):
    env = live_env(items=[(URL1, "Looking for a CRM"), (URL2, "Which CRM should I use?"),
                          ("https://www.reddit.com/r/startups/comments/ghi789/x/", "need crm")])
    env.ages.update({"abc123": 5, "def456": 120, "ghi789": None})            # in window / too old / not returned (deleted or private)
    out = json.loads(prepare(env).finish(answer_json(), historical=[]))
    assert [i["url"] for i in out["live_candidates"]["items"]] == [URL1]


def test_a_metadata_failure_leaves_the_historical_answer_untouched(ai_on):
    env = live_env()
    env.meta_raises = TimeoutError("reddit slow")
    base = answer_json(HURL)
    assert prepare(env).finish(base, historical=[hrow(1, url=HURL)]) == base
    assert env.chats.find_one({"chat_id": "c1"})["messages"][0]["live_reddit"]["state"] == rl.STATE_EMPTY


def test_the_live_search_failing_leaves_the_historical_answer_untouched(ai_on):
    env = live_env()
    env.raises = TimeoutError("openai slow")
    base = answer_json(HURL)
    assert prepare(env).finish(base, historical=[hrow(1, url=HURL)]) == base


def test_live_results_never_enter_the_historical_pool(ai_on):
    env = live_env()
    run = prepare(env)
    run.start()
    pool = [hrow(1, url=HURL)]
    before = json.dumps(pool, sort_keys=True)
    out = json.loads(run.finish(answer_json(HURL), historical=pool))
    assert json.dumps(pool, sort_keys=True) == before and "live_candidates" in out        # the pool never gains a live row
    assert [p["link"] for p in out["platforms"][0]["posts"]] == [HURL]


def test_budgets_claims_cooldown_and_usage_still_apply(ai_on):
    env = live_env()
    run = prepare(env)
    run.finish(answer_json(), historical=[])
    assert len(env.calls) == 1 and len(env.usage) == 1                                # one paid call, usage recorded once
    assert env.counter() is not None
    prepare(env).finish(answer_json(), historical=[])                                 # same message again: claimed, no second call
    assert len(env.calls) == 1


def test_the_window_is_recorded_and_a_result_for_another_window_is_not_reused(ai_on, monkeypatch):
    env = live_env()
    prepare(env).finish(answer_json(), historical=[])
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", 60, raising=False)
    msg = env.chats.find_one({"chat_id": "c1"})["messages"][0]
    out = rl.process(env.deps, owner_key=OWNER, owner_type="email", chat_id="c1", request_id="r0", pre_answer=True,
                     window=(START, END))
    assert len(env.calls) == 1 and out["state"] == "none" and msg["live_reddit"]["window"]["end"] != rl._utc(END)


# ── eligibility is unchanged ───────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("over", [{"time_range": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-08T00:00:00+00:00", "tz": "UTC"}}, {"time_window_days": 7}])
def test_a_historical_time_range_is_never_overwritten_and_does_not_block_live(ai_on, over):
    env = live_env(msg_over=over)
    msg = stored_msg(env)
    before = copy.deepcopy(msg)
    run = prepare(env, msg=msg)
    assert run.active is True and msg == before                              # Historical's range / period: exactly as the user asked
    assert run.window_range.end - run.window_range.start == timedelta(minutes=30)      # Live has its own window


def test_both_entry_points_derive_the_identical_live_window_and_never_touch_the_message(ai_on, monkeypatch):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", 1500, raising=False)
    env = live_env()
    msg = stored_msg(env)
    msg["requested_at"] = END.replace(tzinfo=None)
    before = copy.deepcopy(msg)
    a = ain.apply_window(msg, owner_key=OWNER, chat_id="c1", mode="background_retrieval")
    b = ain.apply_window(msg, owner_key=OWNER, chat_id="c1", mode="stream")
    assert msg == before and msg["time_range"] is None                                   # Historical's message is untouched
    (tr_a, intent_a, _r), (tr_b, intent_b, _r2) = a, b
    assert (tr_a.start, tr_a.end) == (tr_b.start, tr_b.end) and tr_a.end == END and tr_a.end - tr_a.start == timedelta(minutes=1500)
    assert intent_a == intent_b == "demand" and len(env.cls_calls) == 1                  # the classifier was asked once


# ── safe logging ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_logs_carry_the_window_as_an_integer_and_no_content_or_secrets(ai_on, caplog, monkeypatch):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", 1500, raising=False)
    env = live_env()
    with caplog.at_level(logging.INFO):
        prepare(env).finish(answer_json(), historical=[hrow(1)])
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "1500" in text
    for forbidden in ("sk-", "Bearer", "Looking for a CRM for my small business", "We are actively looking"):
        assert forbidden not in text


# ── the background path: the window must reach RETRIEVAL (it ran before the hook before this change) ───────────────────────
@pytest.fixture
def bg(env, on, monkeypatch):
    from tests.test_answer_persistence import FakeChats
    lg, idx, _old = env
    chats = FakeChats()
    monkeypatch.setattr(idx, "chats_collection", chats)
    fi = sys.modules["flintel"]
    monkeypatch.setattr(fi, "should_trigger_immediately", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(idx, "append_to_chat_summary", lambda *a, **k: None, raising=False)
    for name, fn in (("_is_owner_busy", lambda *a: False), ("_set_owner_busy", lambda *a: None), ("_clear_owner_busy", lambda *a: None)):
        monkeypatch.setattr(idx, name, fn, raising=False)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", True, raising=False)
    monkeypatch.setattr(ic, "decide_full", lambda **kw: ("demand", "active_demand"))     # these tests are about Historical retrieval staying unchanged
    return lg, idx, chats


@pytest.mark.parametrize("minutes", [30, 1500])
def test_background_historical_retrieval_gets_its_original_arguments_not_the_live_window(bg, monkeypatch, minutes):
    lg, idx, chats = bg
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", minutes, raising=False)
    seen = []
    monkeypatch.setattr(idx, "get_evidence_with_topup", lambda **kw: seen.append(kw) or [])
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: "{}")
    monkeypatch.setattr(idx, "_complete_message_answer_and_results", lambda *a, **k: None)
    owner = "alice@example.com"
    idx.add_search_to_chat("c1", owner, CRM, lg.normalize_topic_key(CRM), ["crm"], "reddit")
    chat = chats.find_one({"chat_id": "c1", "owner_key": owner})
    chat["messages"][-1].update(evidence_required=15, request_id="req-1", credit_request_id="cr-1")
    idx._fill_in_message_outputs("c1", owner, chat["messages"])
    assert len(seen) == 1 and "time_range" not in seen[0] and seen[0]["since_days"] is None       # exactly what a normal search passes
    assert chats.find_one({"chat_id": "c1", "owner_key": owner})["messages"][-1].get("time_range") is None


def test_a_users_own_historical_range_is_passed_through_unchanged_and_blocks_the_workflow(bg, monkeypatch):
    lg, idx, chats = bg
    seen = []
    monkeypatch.setattr(idx, "get_evidence_with_topup", lambda **kw: seen.append(kw) or [])
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: "{}")
    monkeypatch.setattr(idx, "_complete_message_answer_and_results", lambda *a, **k: None)
    owner = "alice@example.com"
    mine = timerange.TimeRange(start=END - timedelta(days=3), end=END, tz_name="UTC", kind="rolling", phrase="last 3 days").to_doc(END)
    idx.add_search_to_chat("c1", owner, CRM, lg.normalize_topic_key(CRM), ["crm"], "reddit", time_range=mine)
    chat = chats.find_one({"chat_id": "c1", "owner_key": owner})
    chat["messages"][-1].update(evidence_required=15, request_id="req-1", credit_request_id="cr-1")
    idx._fill_in_message_outputs("c1", owner, chat["messages"])
    assert seen[0]["time_range"] == mine


def test_background_retrieval_is_unchanged_for_an_ordinary_search_or_when_disabled(bg, monkeypatch):
    lg, idx, chats = bg
    seen = []
    monkeypatch.setattr(idx, "get_evidence_with_topup", lambda **kw: seen.append(kw) or [])
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: "{}")
    monkeypatch.setattr(idx, "_complete_message_answer_and_results", lambda *a, **k: None)
    owner = "alice@example.com"
    asked = []
    monkeypatch.setattr(ic, "decide_full", lambda **kw: asked.append(kw["chat_id"]) or (False, "no_discovery_intent"))
    for cid, q, on_flag in (("c1", "What is a CRM and how does it work?", True), ("c2", CRM, False)):
        monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", on_flag, raising=False)
        idx.add_search_to_chat(cid, owner, q, lg.normalize_topic_key(q), ["crm"], "reddit")
        chat = chats.find_one({"chat_id": cid, "owner_key": owner})
        chat["messages"][-1].update(evidence_required=15, request_id="req-1", credit_request_id="cr-1")
        idx._fill_in_message_outputs(cid, owner, chat["messages"])
    assert len(seen) == 2 and all("time_range" not in kw for kw in seen)
    assert asked == ["c1"]                                           # the model is asked only when the feature is on


def test_a_failing_window_hook_does_not_stop_background_historical_retrieval(bg, monkeypatch):
    lg, idx, chats = bg
    seen = []
    monkeypatch.setattr(idx, "get_evidence_with_topup", lambda **kw: seen.append(kw) or [])
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: "{}")
    monkeypatch.setattr(idx, "_complete_message_answer_and_results", lambda *a, **k: None)

    def broken(msg, **kw):
        msg["time_range"] = {"start": "half", "end": "written"}                  # a failure AFTER a partial write
        raise RuntimeError("secret customer text must not be logged")
    monkeypatch.setattr(ain, "apply_window", broken)
    owner = "alice@example.com"
    idx.add_search_to_chat("c1", owner, CRM, lg.normalize_topic_key(CRM), ["crm"], "reddit")
    chat = chats.find_one({"chat_id": "c1", "owner_key": owner})
    chat["messages"][-1].update(evidence_required=15, request_id="req-1", credit_request_id="cr-1")
    msgs = chat["messages"]
    idx._fill_in_message_outputs("c1", owner, msgs)
    assert len(seen) == 1 and "time_range" not in seen[0]                         # retrieval ran, with no half-applied range
    assert msgs[-1].get("time_range") is None


def test_the_hook_failure_log_is_safe_and_an_existing_user_range_is_kept(bg, monkeypatch, caplog):
    lg, idx, chats = bg
    seen = []
    monkeypatch.setattr(idx, "get_evidence_with_topup", lambda **kw: seen.append(kw) or [])
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: "{}")
    monkeypatch.setattr(idx, "_complete_message_answer_and_results", lambda *a, **k: None)
    monkeypatch.setattr(ain, "apply_window", lambda msg, **kw: (_ for _ in ()).throw(RuntimeError("secret customer text")))
    owner = "alice@example.com"
    idx.add_search_to_chat("c1", owner, CRM, lg.normalize_topic_key(CRM), ["crm"], "reddit")
    chat = chats.find_one({"chat_id": "c1", "owner_key": owner})
    mine = timerange.TimeRange(start=END - timedelta(days=1), end=END, tz_name="UTC", kind="calendar", phrase="yesterday").to_doc(END)
    chat["messages"][-1].update(evidence_required=15, request_id="req-1", credit_request_id="cr-1", time_range=mine)
    with caplog.at_level(logging.WARNING):
        idx._fill_in_message_outputs("c1", owner, chat["messages"])
    assert seen[0]["time_range"] == mine                                             # the user's own range is untouched
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text and "secret customer text" not in text and CRM not in text
