"""
tests/test_live_search_intents.py — Live Reddit search for BOTH discovery outcomes (active demand, active discussion), driven by the
router's stored keywords and the configured Live window, while Historical Search is left exactly as it is. All mocked: mongomock, a fake
model, no network, no OpenAI call. The fake model only answers what a test tells it to; these tests prove the plumbing, not the
real model's judgement.
"""
import copy
import json
import datetime
from datetime import timedelta, timezone

import pytest

mongomock = pytest.importorskip("mongomock")
import active_intent as ain  # noqa: E402
import timerange  # noqa: E402
import reddit_live as rl  # noqa: E402
from tests.test_active_intent import (HURL, URL2, ai_on, answer_json, attach_classifier, cls_resp, hrow, live_env, prepare,  # noqa: F401,E402
                                      stored_msg, _no_network, _no_inflight_leak, _reset_factory)
from tests.test_reddit_live import on  # noqa: F401,E402
from tests.test_reliability_ux_perf import app_env, env  # noqa: F401,E402

NINE = ["ai agent developer", "build ai agent", "ai automation agency", "need ai assistant", "hire ai agent builder",
        "customer support ai agent", "ai agent for small business", "looking for ai agents", "ai agents recommendations"]
DEMAND_Q = "find people who want someone to build an AI agent for them"
DISCUSSION_Q = "give me ai agents posts people actively talking about"


def env_with(answer, query, kind=None, items=None, **over):
    extra = {"keywords": list(NINE), "query": query}
    if kind:
        extra.update(request_kind=kind, subject="ai agents")
    extra.update(over)
    env = live_env(**({"items": items} if items else {}), msg_over=extra)
    env.cls_override = answer
    return env


def run_live(env, **kw):
    msg = stored_msg(env)
    run = prepare(env, msg=msg)
    run.start()
    out = run.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    return run, msg, json.loads(out)


# ── the two outcomes ────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", [None, "research"])
def test_active_demand_runs_the_demand_search_and_is_labelled_as_results(ai_on, kind):
    env = env_with(cls_resp(intent="demand"), DEMAND_Q, kind)
    run, _m, out = run_live(env)
    assert run.intent == "demand"
    ins = env.calls[0]["payload"]["instructions"]
    assert "genuine, current demand" in ins and "intention to hire or buy" in ins and "currently discussing" not in ins
    lc = out["live_candidates"]
    assert lc["intent"] == "demand" and lc["label"] == "Live Reddit results" and {i["intent"] for i in lc["items"]} == {"demand"}
    assert lc["statement"].startswith("Live Reddit results:") and "not leads" not in lc["statement"]


@pytest.mark.parametrize("kind", [None, "research"])
def test_active_discussion_runs_the_discussion_search_without_a_demand_filter_and_is_labelled_as_discussions(ai_on, kind):
    env = env_with(cls_resp(intent="discussion"), DISCUSSION_Q, kind)
    run, _m, out = run_live(env)
    assert run.intent == "discussion"
    ins = env.calls[0]["payload"]["instructions"]
    assert "currently discussing it" in ins and "do not need to be looking to buy or hire" in ins
    for demand_only in ("genuine, current demand", "intention to hire or buy", "a real need", "request for recommendations"):
        assert demand_only not in ins                                                  # no demand-only filter on discussions
    assert "what conversations to look for" in env.calls[0]["payload"]["input"]
    lc = out["live_candidates"]
    assert lc["intent"] == "discussion" and lc["label"] == "Live Reddit discussions" and {i["intent"] for i in lc["items"]} == {"discussion"}
    st = lc["statement"]
    assert st.startswith("Live Reddit discussions:") and "not leads or confirmed buyers" in st and "looking for a solution" not in st


def test_the_two_instruction_sets_differ_only_in_the_intent_part_of_the_live_request():
    win = ("2026-11-01T11:30:00Z", "2026-11-01T12:00:00Z")
    ctx = {"query": "q", "keywords": ["a", "b"]}
    d, s = rl.build_payload("q", win, dict(ctx, mode="demand")), rl.build_payload("q", win, dict(ctx, mode="discussion"))
    assert d == rl.build_payload("q", win, ctx)                                          # no mode = the demand search, as before
    assert {k for k in d if d[k] != s[k]} == {"instructions", "input"}
    assert d["tools"] == s["tools"] and d["max_tool_calls"] == s["max_tool_calls"] == 1 and d["tool_choice"] == s["tool_choice"] == "required"
    base = d["instructions"][:d["instructions"].index(" First work out what the user actually wants.")]
    assert s["instructions"].startswith(base) and "Only return Reddit posts that were published between" in s["instructions"]


@pytest.mark.parametrize("answer", [cls_resp(intent="neither"), cls_resp(intent="demand", confidence="medium"),
                                    cls_resp(intent="demand", confidence="low"), cls_resp(intent="discussion", confidence="medium"),
                                    cls_resp(intent="discussion", confidence="low")])
@pytest.mark.parametrize("kind", [None, "research"])
def test_generic_research_and_anything_below_high_confidence_never_reach_live(ai_on, kind, answer):
    env = env_with(answer, "summarise what the ai agent market looks like", kind)
    msg = stored_msg(env)
    run = prepare(env, msg=msg)
    assert run.active is False and env.calls == [] and len(env.cls_calls) == 1


@pytest.mark.parametrize("name,setup", [("timeout", lambda e: setattr(e, "cls_raises", TimeoutError("slow"))),
                                        ("malformed", lambda e: setattr(e, "cls_override", cls_resp(text="discussion"))),
                                        ("old shape", lambda e: setattr(e, "cls_override", cls_resp(text='{"active_demand": true, "confidence": "high"}'))),
                                        ("unknown intent", lambda e: setattr(e, "cls_override", cls_resp(text='{"intent": "chat", "confidence": "high"}')))])
def test_classifier_failures_fail_closed_for_both_outcomes(ai_on, name, setup):
    env = env_with(cls_resp(intent="discussion"), DISCUSSION_Q, "research")
    setup(env)
    assert prepare(env).active is False and env.calls == []


@pytest.mark.parametrize("kind", ["provider_search", "person_mentions", "mention_search", "made_up", ""])
def test_restricted_and_unknown_kinds_are_never_asked_even_for_a_discussion_request(ai_on, kind):
    env = env_with(cls_resp(intent="discussion"), DISCUSSION_Q, kind or None, **({"request_kind": kind} if kind == "" else {}))
    if kind == "":
        env.chats.update_one({"chat_id": "c1"}, {"$set": {"messages.0.request_kind": ""}})
    run = prepare(env)
    assert run.active is False and env.cls_calls == [] and env.calls == []


# ── Historical's time range and Live's window are independent ─────────────────────────────────────────────────────────────────
SEVEN_DAYS = {"time_window_days": 7}
TYPED_RANGE = {"time_range": {"start": "2026-10-25T00:00:00+00:00", "end": "2026-11-01T00:00:00+00:00", "tz": "UTC", "kind": "rolling",
                              "phrase": "last 7 days"}}


@pytest.mark.parametrize("query,hist,applied", [
    ("find people currently looking for ai agents", {}, True),
    ("give me ai agents posts people actively talking about", {}, True),
    ("find people currently looking for ai agents", {"time_window_days": 30}, True),     # the default lookback of a recency word is not the user's
    ("recent posts about ai agents", {"time_window_days": 30}, True),
    ("ai agents posts people actively talking about", {"time_window_days": 30}, False),   # 30 days but no recency word: a period the user named
    ("recent posts about ai agents", {"time_window_days": 7}, False),                    # a named period wins even with a recency word
    ("give me ai agents posts people actively talking about", SEVEN_DAYS, False),
    ("give me ai agents posts people actively talking about", TYPED_RANGE, False),
], ids=["none-currently", "none-actively", "default30-currently", "default30-recent", "30-no-recency", "7days-recent", "7days", "typed"])
def test_the_historical_search_gets_the_live_interval_only_when_the_user_gave_it_no_range_of_its_own(ai_on, query, hist, applied):
    env = env_with(cls_resp(intent="discussion"), query, "research", **hist)
    msg = stored_msg(env)
    before = copy.deepcopy(msg)
    run = prepare(env, msg=msg)
    assert run.active is True and len(env.cls_calls) == 1                        # Live qualifies either way
    if applied:
        assert {k for k in set(msg) | set(before) if msg.get(k) != before.get(k)} == {"time_range"}
        h = timerange.range_from_doc(msg["time_range"])
        assert (h.start, h.end) == (run.window_range.start, run.window_range.end) and msg["time_window_days"] == before["time_window_days"]
    else:
        assert msg == before                                                     # the user's range / period: not overwritten, not reinterpreted


def test_the_post_answer_links_endpoint_still_refuses_time_bounded_searches():
    from tests.test_reddit_live import message
    now = rl.timerange.to_utc(message(0)["answered_at"]) + timedelta(seconds=5)
    base = message(0, claude_answer=json.dumps({"format": "source_list", "platforms": []}))
    assert rl.eligible(dict(base, time_window_days=7), now) == (False, "time_bounded")
    assert rl.eligible(dict(base, **TYPED_RANGE), now) == (False, "time_bounded")


@pytest.mark.parametrize("hist", [SEVEN_DAYS, TYPED_RANGE], ids=["seven_days", "typed_range"])
@pytest.mark.parametrize("kind", ["provider_search", "person_mentions", "mention_search", "made_up"])
def test_restricted_kinds_stay_restricted_with_or_without_a_historical_range(ai_on, kind, hist):
    env = env_with(cls_resp(intent="discussion"), DISCUSSION_Q, kind, **hist)
    run = prepare(env)
    assert run.active is False and env.cls_calls == [] and env.calls == []


# ── the Live window and Historical Search ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("intent", ["demand", "discussion"])
def test_live_and_historical_use_the_same_configured_1500_minute_interval(ai_on, monkeypatch, intent):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", 1500, raising=False)
    env = env_with(cls_resp(intent=intent), "ai agents posts people are currently discussing", "research", time_window_days=7)
    run, msg, out = run_live(env)
    assert run.window_range.end - run.window_range.start == timedelta(minutes=1500)
    rec = env.chats.find_one({"chat_id": "c1"})["messages"][0]["live_reddit"]["window"]
    assert rec == {"start": rl._utc(run.window_range.start), "end": rl._utc(run.window_range.end)}
    p = env.calls[0]["payload"]
    assert rec["start"] in p["instructions"] and rec["end"] in p["instructions"] and rec["start"] in p["input"]
    assert out["live_candidates"]["window"]["minutes"] == 1500 and out["live_candidates"]["live_window_minutes"] == 1500
    assert msg["time_range"] is None and msg["time_window_days"] == 7             # a named period: Historical keeps it (Live still ran)


def test_historical_rows_and_answer_are_untouched_by_either_outcome(ai_on):
    for intent in ("demand", "discussion"):
        env = env_with(cls_resp(intent=intent), DISCUSSION_Q, "research")
        run = prepare(env)
        pool = [hrow(1, url=HURL), hrow(2, age_minutes=100000)]
        snap = copy.deepcopy(pool)
        assert run.historical(pool) is pool and pool == snap
        base = answer_json(HURL)
        out = json.loads(run.finish(base, historical=pool))
        assert {k: v for k, v in out.items() if k != "live_candidates"} == json.loads(base)


# ── keywords, one search, existing merge ──────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("intent", ["demand", "discussion"])
def test_the_routers_keywords_are_reused_unchanged_in_one_combined_reddit_search(ai_on, intent):
    env = env_with(cls_resp(intent=intent), DISCUSSION_Q, "research")
    run, msg, _out = run_live(env)
    assert msg["keywords"] == NINE
    p = env.calls[0]["payload"]
    assert p["input"].count("Search reddit.com for posts matching these keywords:") == 1
    assert "Search reddit.com for posts matching these keywords: " + ", ".join(NINE) in p["input"]
    assert "ONE search" in p["instructions"] and "once per keyword" in p["instructions"]
    assert len(env.calls) == 1 and len(env.cls_calls) == 1
    assert p["tools"] == [{"type": "web_search", "filters": {"allowed_domains": ["reddit.com"]}}]
    assert p["max_tool_calls"] == 1 and p["tool_choice"] == "required" and p["store"] is False


def test_live_posts_already_in_the_saved_posts_are_deduplicated_for_discussions_too(ai_on):
    env = env_with(cls_resp(intent="discussion"), DISCUSSION_Q, "research", items=[(HURL, "same as saved"), (URL2, "a new discussion")])
    run, _m, out = run_live(env)
    assert [i["url"] for i in out["live_candidates"]["items"]] == [URL2] and "duplicate" in out["live_candidates"]["statement"]


def test_disabled_active_intent_asks_nobody_and_searches_nothing(ai_on, monkeypatch):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", False, raising=False)
    env = env_with(cls_resp(intent="discussion"), DISCUSSION_Q, "research")
    run = prepare(env)
    assert run.active is False and env.cls_calls == [] and env.calls == []


def test_one_classifier_slot_and_one_live_slot_per_message_for_a_discussion(ai_on):
    env = env_with(cls_resp(intent="discussion"), DISCUSSION_Q, "research")
    run_live(env)
    prepare(env).finish(answer_json(HURL), historical=[])              # a repeated run of the same message
    assert env.chats.database["cls_budget"].find_one()["count"] == 1 and env.budget.find_one()["count"] == 1
    assert len(env.cls_calls) == 1 and len(env.calls) == 1


# ── stream level: Historical receives the user's range unchanged, Live receives exactly 1500 minutes ──────────────────────────
@pytest.fixture
def wfx(app_env, on, monkeypatch):
    from tests.test_active_intent_workflow import enable
    enable(monkeypatch, minutes=1500)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", 4, raising=False)
    return app_env


@pytest.mark.parametrize("hist,check", [
    (SEVEN_DAYS, lambda kw: kw["since_days"] == 7 and "time_range" not in kw),
    (TYPED_RANGE, lambda kw: kw["time_range"] == TYPED_RANGE["time_range"] and kw["since_days"] is None),
], ids=["seven_days", "typed_range"])
def test_in_the_real_stream_historical_gets_the_users_range_unchanged_and_live_gets_1500_minutes(wfx, monkeypatch, hist, check):
    from tests.test_active_intent_workflow import World, run_stream, final_answer, HIST_URL, CRM
    from tests.test_active_intent_workflow import timerange as tr_mod
    world = World("s1")
    done, _u, chats = run_stream(wfx, monkeypatch, world, msg_over=hist)
    (kw,) = world.retrieval_kwargs
    assert check(kw)                                                        # Historical: exactly the user's range / period
    wdoc = world.live.stored(0, "s1")["window"]
    assert tr_mod.to_utc(wdoc["end"]) - tr_mod.to_utc(wdoc["start"]) == timedelta(minutes=1500)
    payload = world.live.calls[0]["payload"]
    assert wdoc["start"] in payload["instructions"] and wdoc["end"] in payload["instructions"]
    assert "time_range" not in payload and "time_window_days" not in payload and "since_days" not in payload      # the two never mix
    assert "last 7 days" not in json.dumps(payload) and "7 days" not in json.dumps(payload)                    # Historical's period is not in the Live request
    ans = final_answer(done, chats, "s1")
    assert ans["platforms"][0]["posts"][0]["link"] == HIST_URL and ans["live_candidates"]["window"]["minutes"] == 1500
    assert len(world.live.calls) == 1 and len(world.live.cls_calls) == 1
    stored = chats.docs[("s1", "alice@example.com")]["messages"][0]
    assert stored.get("time_window_days") == hist.get("time_window_days") and stored.get("time_range") == hist.get("time_range")


def test_credits_classifier_budget_merge_and_dedup_are_unchanged_with_a_seven_day_historical_range(ai_on):
    env = env_with(cls_resp(intent="discussion"), DISCUSSION_Q, "research", items=[(HURL, "saved already"), (URL2, "new")], **SEVEN_DAYS)
    run, _m, out = run_live(env)
    assert [i["url"] for i in out["live_candidates"]["items"]] == [URL2] and "duplicate" in out["live_candidates"]["statement"]
    assert env.chats.database["cls_budget"].find_one()["count"] == 1 and env.budget.find_one()["count"] == 1
    prepare(env).finish(answer_json(HURL), historical=[])
    assert len(env.cls_calls) == 1 and len(env.calls) == 1                   # once per message, whatever the range
