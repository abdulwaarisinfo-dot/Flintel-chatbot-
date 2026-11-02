"""
tests/test_intent_classifier.py — the GPT classifier that replaced the regex gate of Active Intent (intent_classifier.py).
All mocked: mongomock, a fake callable standing in for the model (it only ever answers what the test tells it to), no network, no
OpenAI request, no production database. What these tests can NOT show: whether the real model judges real requests well. They
show that the gate follows the model's answer, fails closed, is claimed and paid for once, and changes nothing else.
"""
import copy
import json
import threading
from datetime import timedelta

import pytest

mongomock = pytest.importorskip("mongomock")
import active_intent as ain  # noqa: E402
import intent_classifier as ic  # noqa: E402
import reddit_live as rl  # noqa: E402
import usage_meter  # noqa: E402
from tests.test_active_intent import (CRM, HURL, ai_on, answer_json, attach_classifier, cls_resp, hrow, live_env, prepare,  # noqa: F401,E402
                                      set_classifier_caps, stored_msg, _no_network, _no_inflight_leak, _reset_factory)
from tests.test_reddit_live import OWNER, on  # noqa: F401,E402


def decide(env, **kw):
    return ic.decide(owner_key=OWNER, chat_id="c1", request_id="r0", **kw)


def record(env):
    return env.chats.find_one({"chat_id": "c1"})["messages"][0].get("active_intent")


# ── what the model is given ────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_model_gets_the_actual_query_the_restatement_and_the_saved_keywords_and_no_tools(ai_on):
    env = live_env(msg_over={"query": "find people who need an AI agent", "resolved_query": "Find businesses that want to hire someone to build an AI agent",
                             "keywords": ["ai agent developer", "automation"]})
    assert decide(env)[0] is True
    (call,) = env.cls_calls
    p = call["payload"]
    assert "find people who need an AI agent" in p["input"] and "want to hire someone to build an AI agent" in p["input"]
    assert "ai agent developer, automation" in p["input"]
    assert "tools" not in p and "tool_choice" not in p and "max_tool_calls" not in p and p["store"] is False
    assert p["text"]["format"]["type"] == "json_schema" and p["text"]["format"]["strict"] is True
    assert call["url"] == ic.config.OPENAI_RESPONSES_URL and call["timeout"] == ic.timeout_seconds()


def test_the_instructions_hold_no_phrase_list_or_pattern():
    text = ic.INSTRUCTIONS.lower()
    for fragment in ("actively looking", "looking for", "need a", "recommendations for", "regex", "hiring"):
        assert fragment not in text
    assert "ambiguous" in text and "not sure" in text                 # uncertain -> does not qualify


def test_the_request_text_is_cleaned_and_bounded():
    p = ic.build_payload("a\x00b" + "x" * 900, None, ["k" * 200] * 50 + [5, None])
    assert "\x00" not in p["input"] and len(p["input"]) < 2200


# ── the decision: positive, negative, ambiguous, malformed ──────────────────────────────────────────────────────────────────
def test_positive_negative_and_ambiguous_answers(ai_on):
    for answer, expected, reason in ((cls_resp(True, "high"), True, "active_demand"), (cls_resp(False, "high"), False, "not_active_demand"),
                                     (cls_resp(False, "low"), False, "not_active_demand"), (cls_resp(True, "medium"), False, "uncertain"),
                                     (cls_resp(True, "low"), False, "uncertain")):
        env = live_env()
        env.cls_override = answer
        assert decide(env) == (expected, reason)


@pytest.mark.parametrize("data", [None, [], "x", {"status": "failed"}, cls_resp(text="{"), cls_resp(text="[]"), cls_resp(text="true"),
                                  cls_resp(text='{"active_demand": 1, "confidence": "high"}'),
                                  cls_resp(text='{"active_demand": true, "confidence": "certain"}'),
                                  cls_resp(text='{"active_demand": true, "confidence": "high", "extra": 1}'),
                                  cls_resp(text='{"confidence": "high"}'), cls_resp(text='```json\n{"active_demand": true, "confidence": "high"}\n```')])
def test_parse_decision_is_strict(data):
    assert ic.parse_decision(data)[0] is False


# ── once per message, atomically ───────────────────────────────────────────────────────────────────────────────────────────
def test_repeated_calls_for_one_message_make_one_api_call_and_one_budget_slot(ai_on):
    env = live_env()
    results = [decide(env) for _ in range(5)]
    assert results[0] == (True, "active_demand") and all(r == (True, "already_decided") for r in results[1:])
    assert len(env.cls_calls) == 1 and len(env.cls_usage) == 1
    assert env.chats.database["cls_budget"].find_one()["count"] == 1
    rec = record(env)
    assert rec["state"] == "decided" and rec["decision"] is True and "query" not in json.dumps(rec, default=str)


def test_a_negative_decision_is_also_remembered_and_never_asked_again(ai_on):
    env = live_env()
    attach_classifier(env, decide=lambda t: False)
    assert decide(env) == (False, "not_active_demand") and decide(env) == (False, "already_decided")
    assert len(env.cls_calls) == 1


def test_concurrent_callers_make_exactly_one_call_and_agree(ai_on):
    env = live_env()
    gate, started = threading.Event(), threading.Event()
    real = env.cls_deps.post

    def slow(url, headers, payload, timeout):
        started.set()
        gate.wait(5)
        return real(url, headers, payload, timeout)
    env.cls_deps.post = slow
    out = []
    ts = [threading.Thread(target=lambda: out.append(decide(env))) for _ in range(6)]
    for t in ts:
        t.start()
    started.wait(5)
    gate.set()
    for t in ts:
        t.join(10)
    assert len(env.cls_calls) == 1 and len(out) == 6 and all(r[0] is True for r in out)


def test_a_caller_that_loses_the_claim_waits_for_the_winners_decision(ai_on, monkeypatch):
    env = live_env()
    now = env.clock()
    env.chats.update_one({"chat_id": "c1"}, {"$set": {"messages.0.active_intent": {"claim_id": "x", "state": "claiming", "claimed_at": now}}})
    monkeypatch.setattr(ic.time, "sleep", lambda s: env.chats.update_one({"chat_id": "c1"}, {"$set": {"messages.0.active_intent.state": "decided",
                                                                                                         "messages.0.active_intent.decision": True}}))
    assert decide(env) == (True, "already_decided") and env.cls_calls == []                  # it never called the model itself


def test_a_pending_claim_that_never_resolves_fails_closed(ai_on, monkeypatch):
    env = live_env()
    env.chats.update_one({"chat_id": "c1"}, {"$set": {"messages.0.active_intent": {"claim_id": "x", "state": "claiming", "claimed_at": env.clock()}}})
    monkeypatch.setattr(ic, "timeout_seconds", lambda: 0)
    monkeypatch.setattr(ic.time, "sleep", lambda s: None)
    assert decide(env) == (False, "decision_pending") and env.cls_calls == []


def test_a_stale_claim_is_final_and_never_retried(ai_on):
    env = live_env()
    old = env.clock() - timedelta(hours=1)
    env.chats.update_one({"chat_id": "c1"}, {"$set": {"messages.0.active_intent": {"claim_id": "x", "state": "claiming", "claimed_at": old}}})
    assert decide(env) == (False, "stale_claim") and env.cls_calls == []


# ── budget (its own) and usage (its own) ───────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("g,o", [(0, 10), (10, 0), (None, 10), (10, None), (-1, 10), (True, 10), ("5", 5), (ic.GLOBAL_CAP_CEILING + 1, 10),
                                 (10, ic.OWNER_CAP_CEILING + 1)])
def test_missing_or_invalid_caps_mean_no_call_and_no_gate(ai_on, monkeypatch, g, o):
    env = live_env()
    set_classifier_caps(monkeypatch, g, o)
    assert decide(env) == (False, "classifier_caps_invalid") and env.cls_calls == [] and record(env) is None
    assert prepare(env).active is False and env.calls == []


def test_the_global_and_the_owner_caps_are_enforced_atomically_and_refusals_call_nothing(ai_on, monkeypatch):
    set_classifier_caps(monkeypatch, 2, 1)
    env = live_env()
    env.chats.insert_one({"chat_id": "c2", "owner_key": OWNER, "messages": [dict(env.chats.find_one({"chat_id": "c1"})["messages"][0], request_id="r9")]})
    assert decide(env)[0] is True                                         # owner slot 1 of 1
    second = ic.decide(owner_key=OWNER, chat_id="c2", request_id="r9")
    assert second == (False, "budget_refused") and len(env.cls_calls) == 1
    assert env.chats.find_one({"chat_id": "c2"})["messages"][0]["active_intent"]["state"] == "skipped"


def test_the_classifier_budget_is_separate_from_the_live_budget(ai_on):
    env = live_env()
    prepare(env).finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    live_day = env.budget.find_one()
    cls_day = env.chats.database["cls_budget"].find_one()
    assert live_day["count"] == 1 and cls_day["count"] == 1
    assert env.budget.name != env.chats.database["cls_budget"].name and ic.BUDGET_COLLECTION == "active_intent_classifier_budget"


def test_usage_is_recorded_once_per_call_under_its_own_call_type(ai_on, monkeypatch):
    env = live_env()
    decide(env)
    assert len(env.cls_usage) == 1 and env.cls_usage[0][2] == "ok"
    seen = []
    monkeypatch.setattr(usage_meter, "record_usage", lambda *a, **k: seen.append((a, k)))
    ic._record_usage("gpt-x", cls_resp(), "ok")
    (args, kwargs), = seen
    assert args[0] == "intent_classifier" == ic.CALL_TYPE and args[1] == "gpt-x" and args[2:4] == (120, 20)
    assert kwargs["extra"]["purpose"] == "active_intent_classifier" and kwargs["extra"]["web_search_enabled"] is False


def test_a_failed_call_is_still_recorded_and_counted(ai_on):
    env = live_env()
    env.cls_raises = TimeoutError("slow")
    assert decide(env) == (False, "api_error") and len(env.cls_usage) == 1 and env.cls_usage[0][2] == "error"
    assert env.chats.database["cls_budget"].find_one()["count"] == 1 and record(env)["state"] == "failed"
    assert decide(env) == (False, "already_failed") and len(env.cls_calls) == 1       # no retry


# ── structural rules come first: nothing is paid for a search that could never qualify ───────────────────────────────────────
@pytest.mark.parametrize("over", [{"time_range": {"start": 1}}, {"time_window_days": 7}, {"request_kind": "mention_search"}, {"website_only": True},
                                  {"keywords": []}, {"credit_request_id": None}])
def test_ineligible_searches_never_reach_the_classifier(ai_on, over):
    env = live_env(msg_over=over)
    assert prepare(env).active is False and env.cls_calls == [] and env.calls == []


def test_guests_and_a_disabled_feature_never_reach_the_classifier(ai_on, monkeypatch):
    env = live_env()
    assert ain.Run.prepare(stored_msg(env), owner_key="anon-123", chat_id="c1").active is False and env.cls_calls == []
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", False, raising=False)
    ic.DEPS_FACTORY = lambda: env.cls_deps
    assert ain.Run.prepare(stored_msg(env), owner_key=OWNER, chat_id="c1").active is False and env.cls_calls == []


def test_live_inactive_means_no_classifier_call(ai_on, monkeypatch):
    env = live_env()
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_LINKS_ENABLED", False, raising=False)
    assert prepare(env).active is False and env.cls_calls == []


# ── Historical Search is unchanged whatever the model says ─────────────────────────────────────────────────────────────────
def test_a_negative_decision_leaves_the_message_and_historical_results_exactly_as_they_were(ai_on):
    env = live_env()
    attach_classifier(env, decide=lambda t: False)
    msg = stored_msg(env)
    before = copy.deepcopy(msg)
    run = prepare(env, msg=msg)
    pool = [hrow(1), hrow(2, age_minutes=900)]
    snapshot = copy.deepcopy(pool)
    assert msg == before and run.historical(pool) is pool and pool == snapshot
    base = answer_json(HURL)
    assert run.finish(base, historical=pool) == base and env.calls == []


def test_a_positive_decision_changes_only_the_time_range_and_the_classifier_never_sees_results(ai_on):
    env = live_env()
    msg = stored_msg(env)
    before = copy.deepcopy(msg)
    run = prepare(env, msg=msg)
    assert run.active is True
    changed = {k for k in set(msg) | set(before) if msg.get(k) != before.get(k)}
    assert changed == {"time_range"}
    payload_text = json.dumps(env.cls_calls[0]["payload"])
    assert "results" not in env.cls_calls[0]["payload"] and "claude_answer" not in payload_text


# ── the Live call (one request, same limits, richer instructions) ───────────────────────────────────────────────────────────
def test_the_live_request_carries_the_wording_the_keywords_and_the_demand_instructions_and_keeps_its_limits(ai_on):
    env = live_env(msg_over={"query": "find people who need AI agents", "resolved_query": "Find businesses wanting an AI agent built",
                             "keywords": ["ai agent", "automation"]})
    run = prepare(env)
    run.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    p = env.calls[0]["payload"]
    assert "Find businesses wanting an AI agent built" in p["input"] and "find people who need AI agents" in p["input"]
    assert "ai agent, automation" in p["input"] and "do not limit the search to them" in p["input"]
    ins = p["instructions"].lower()
    for needle in ("genuine, current demand", "do not depend on exact keywords", "unsure", "never invent"):
        assert needle in ins
    assert p["tools"] == [{"type": "web_search", "filters": {"allowed_domains": ["reddit.com"]}}] and p["max_tool_calls"] == 1
    start, end = env.chats.find_one({"chat_id": "c1"})["messages"][0]["live_reddit"]["window"].values()
    assert start in p["input"] and end in p["input"] and start in p["instructions"]          # the window text is still there
    assert len(env.calls) == 1 and len(env.cls_calls) == 1                                    # one Live call + one classifier call, no more


def test_the_ordinary_live_links_request_is_unchanged():
    plain = rl.build_payload("find a crm")
    assert plain == rl.build_payload("find a crm", None, {"query": "other", "keywords": ["x"]})     # context is ignored without a window
    assert "demand" not in plain["instructions"].lower() and plain["input"] == "Find Reddit posts for this request: find a crm"


def test_out_of_window_and_unverifiable_live_posts_are_still_rejected(ai_on):
    env = live_env(items=[(rl_url, "t") for rl_url in ("https://www.reddit.com/r/SaaS/comments/abc123/x/", "https://www.reddit.com/r/SaaS/comments/def456/y/")])
    env.ages.update({"abc123": 600, "def456": None})                  # far too old / not returned by Reddit's metadata
    out = json.loads(prepare(env).finish(answer_json(HURL), historical=[hrow(1, url=HURL)]))
    assert "live_candidates" not in out and out["platforms"]


def test_forced_interleaving_all_callers_read_the_message_before_any_claims_it(ai_on):
    """Every caller sees 'no decision yet' and tries to claim: only the atomic $exists:false update lets exactly one through."""
    env = live_env()
    n = 6
    barrier = threading.Barrier(n, timeout=10)
    real_find = env.chats.find_one
    seen = threading.local()

    def find_one(*a, **k):
        doc = real_find(*a, **k)
        if not getattr(seen, "done", False):          # each caller's FIRST read (the pre-claim one) waits for all the others
            seen.done = True
            barrier.wait()
        return doc
    env.cls_deps.chats = type("C", (), {"find_one": staticmethod(find_one), "update_one": staticmethod(env.chats.update_one)})()
    out = []
    ts = [threading.Thread(target=lambda: out.append(decide(env))) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    assert len(env.cls_calls) == 1 and len(out) == n and all(r[0] is True for r in out)
    assert env.chats.database["cls_budget"].find_one()["count"] == 1


@pytest.mark.parametrize("what", ["find_one", "update_one"])
def test_an_unexpected_database_error_fails_closed(ai_on, what):
    env = live_env()

    class Broken:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("db down")
            return boom
    setattr(env.cls_deps, "chats", Broken())
    assert decide(env) == (False, "exception:RuntimeError") and env.cls_calls == []
    assert prepare(env).active is False
