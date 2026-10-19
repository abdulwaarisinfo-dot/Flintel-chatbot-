"""
tests/test_credit_cost.py — usage-based search cost (credit_cost.py, the router
effort fields, and how routes.search uses them).

ALL of these are "unit (fake DB), not live": pure functions, mongomock
collections and a stubbed model call. No real MongoDB, no network, no real LLM.
Nothing here proves behaviour of a live MongoDB 3 deployment.
"""
import ast
import json
import logging
import sys
from pathlib import Path

import pytest

import credit_cost as cc
import search_credits as sc

# Reuse (not modify) the route-level harness of the existing credit route tests.
from tests.test_search_credit_routes import (  # noqa: F401  (fixtures + helpers)
    CHAT_ROUTER, KEY1, KEY2, SEARCH_ROUTER, app, body, credits_doc, env, used)

ROOT = Path(__file__).resolve().parent.parent
QUERY = "Find people looking for a CRM for small teams"


def routed(**extra):
    return {**SEARCH_ROUTER, **extra}


# ── validation (unit) ────────────────────────────────────────────────────────
@pytest.mark.parametrize("effort,cost", [("standard", 3), ("standard", 4), ("standard", 5),
                                         ("complex", 5), ("complex", 6), ("complex", 8),
                                         ("deep", 8), ("deep", 11), ("deep", 14)])
def test_valid_proposals_are_charged_exactly_as_proposed(effort, cost):
    d = cc.select_cost(routed(effort=effort, cost=cost, reason="many angles"))
    assert (d.cost, d.effort, d.reason) == (cost, effort, "many angles")


def test_effort_name_is_trimmed_and_case_insensitive_but_nothing_else_is_guessed():
    assert cc.select_cost(routed(effort=" Deep ", cost=9)).cost == 9
    assert cc.select_cost(routed(effort="deep!", cost=9)).cost == cc.fallback_cost()


@pytest.mark.parametrize("effort,cost", [
    ("standard", 2), ("standard", 6), ("complex", 4), ("complex", 9), ("deep", 7), ("deep", 15),   # out of range
    ("standard", 0), ("deep", -1), ("deep", 100),
    ("standard", 4.0), ("standard", 3.5), ("deep", 11.0),                                           # float
    ("standard", "4"), ("deep", "eleven"), ("standard", ""),                                        # str
    ("standard", True), ("standard", False), ("complex", True),                                     # bool
    ("standard", None), ("deep", [11]), ("deep", {"v": 11}),                                        # None / containers
    ("greeting", 0), ("greeting", 4), ("chat", 4), ("", 4), ("tiny", 3), (None, 4), (4, 4), (["deep"], 11),
])
def test_every_invalid_proposal_is_charged_the_fallback(effort, cost):
    d = cc.select_cost(routed(effort=effort, cost=cost, reason="x"))
    assert (d.cost, d.effort) == (4, "standard") and d.reason.startswith("fallback:")


def test_missing_fields_empty_router_result_and_non_search_intents_use_the_fallback():
    assert cc.select_cost(routed()).cost == 4                       # router answered but gave no effort
    assert cc.select_cost({}).cost == 4 and cc.select_cost(None).cost == 4      # router failed (routed = {})
    assert cc.select_cost("deep").cost == 4                         # not even a dict
    for intent in ("chat", "clarify", "blocked", "previous_results", None):
        d = cc.select_cost({"intent": intent, "effort": "deep", "cost": 11})
        assert d.cost == 4 and d.reason == cc.FALLBACK_NOT_SEARCH   # a cost is only taken from a routed search


def test_router_fallback_result_of_logics_is_charged_the_fallback():
    fallback = {"intent": "search", "reply": None, "keywords": None, "time_window_days": None, "unfiltered": None,
                "match_phrases": None, "evidence_required": None, "website_only": False}
    assert cc.select_cost(fallback).cost == 4


def test_reason_is_short_single_line_and_never_raises():
    long = " ".join(f"w{i}" for i in range(40))
    assert len(cc.clean_reason(long).split()) == 15
    assert cc.clean_reason("a\nb\x00c\t d") == "a b c d"
    assert cc.clean_reason("x" * 500) == "x" * cc.REASON_MAX_CHARS
    assert cc.clean_reason("   ") is None and cc.clean_reason(None) is None and cc.clean_reason(5) is None
    assert cc.select_cost(routed(effort="deep", cost=9, reason=["not a string"])).reason is None


def test_costs_and_ranges_come_from_config_and_the_max_is_14():
    import config
    assert config.CREDIT_COST_RANGES == {"standard": (3, 5), "complex": (5, 8), "deep": (8, 14)}
    assert config.CREDIT_MAX_COST == 14 == cc.max_cost() and config.CREDIT_FALLBACK_COST == 4


def test_fallback_is_always_a_valid_standard_cost_even_if_config_is_corrupted(monkeypatch):
    import config
    for bad in (0, -3, 99, True, "4", None, 2.5):
        monkeypatch.setattr(config, "CREDIT_FALLBACK_COST", bad)
        assert cc.fallback_cost() == 4
    for good in (3, 5):
        monkeypatch.setattr(config, "CREDIT_FALLBACK_COST", good)
        assert cc.select_cost({}).cost == good


@pytest.mark.parametrize("raw,expected", [(None, 4), ("", 4), ("  ", 4), ("3", 3), ("4", 4), ("5", 5), (" 5 ", 5)])
def test_fallback_cost_setting_valid_values(raw, expected):
    import config
    assert config._parse_credit_fallback_cost(raw) == expected


@pytest.mark.parametrize("raw", ["2", "6", "0", "-1", "abc", "4.5", "1e1", "14", "٤"])
def test_fallback_cost_setting_invalid_values_log_an_error_and_use_4(raw, caplog):
    import config
    with caplog.at_level(logging.ERROR, logger="flintel.config"):
        assert config._parse_credit_fallback_cost(raw) == 4
    assert any("CREDIT_FALLBACK_COST" in r.getMessage() for r in caplog.records)


def test_plan_credit_settings_defaults_and_invalid_values(caplog):
    import config
    assert (config.PLAN_CREDITS_STANDARD, config.PLAN_CREDITS_BUSINESS) == (400, 1500)
    assert config._parse_plan_credits("PLAN_CREDITS_STANDARD", "250", 400) == 250
    for raw in ("0", "-5", "x", "1.5", "10000000"):
        with caplog.at_level(logging.ERROR, logger="flintel.config"):
            assert config._parse_plan_credits("PLAN_CREDITS_STANDARD", raw, 400) == 400
    assert any("PLAN_CREDITS_STANDARD" in r.getMessage() for r in caplog.records)
    assert config.FREE_MONTHLY_CREDITS_DEFAULT == 40      # the free allowance setting is untouched


def test_credit_cost_has_no_model_call_and_no_io():
    tree = ast.parse((ROOT / "credit_cost.py").read_text(encoding="utf-8"))
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported <= {"logging", "re", "collections", "config"}
    text = (ROOT / "credit_cost.py").read_text(encoding="utf-8")
    assert "_call_claude" not in text and "httpx" not in text and "retry" not in text.lower().replace("never retries", "")


# ── router prompt and parser (logics.py) ────────────────────────────────────
def test_router_prompt_carries_the_effort_instructions_with_config_ranges(env):
    lg, _idx, _ = env
    prompt = lg.CLAUDE_ROUTER_SYSTEM_PROMPT
    assert lg.CLAUDE_ROUTER_EFFORT_ADDENDUM in prompt and prompt.count("SEARCH EFFORT") == 1
    for effort, (lo, hi) in {"standard": (3, 5), "complex": (5, 8), "deep": (8, 14)}.items():
        assert f'"{effort}"' in lg.CLAUDE_ROUTER_EFFORT_ADDENDUM
        assert f"from {lo} to {hi}" in lg.CLAUDE_ROUTER_EFFORT_ADDENDUM


def test_parser_passes_the_raw_effort_fields_only_for_search(env):
    lg, _idx, _ = env
    raw = json.dumps({"intent": "search", "keywords": ["crm"], "match_phrases": ["p"], "effort": "deep", "cost": 11,
                      "reason": "broad"})
    parsed = lg._parse_router_json(raw)
    assert (parsed["effort"], parsed["cost"], parsed["reason"]) == ("deep", 11, "broad")
    weird = lg._parse_router_json(json.dumps({"intent": "search", "keywords": ["crm"], "effort": ["x"], "cost": "4.5"}))
    assert (weird["effort"], weird["cost"], weird["reason"]) == (["x"], "4.5", None)   # raw: credit_cost rejects it
    for intent in ("chat", "clarify", "blocked"):
        other = lg._parse_router_json(json.dumps({"intent": intent, "reply": "hi", "effort": "deep", "cost": 11}))
        assert (other["effort"], other["cost"], other["reason"]) == (None, None, None)
    assert cc.select_cost(weird).cost == 4


def test_parser_keeps_every_existing_field_unchanged(env):
    lg, _idx, _ = env
    parsed = lg._parse_router_json(json.dumps({"intent": "search", "keywords": ["crm", "crm"], "time_window_days": 7,
                                               "match_phrases": ["p"], "evidence_required": 12}))
    assert parsed["keywords"] == ["crm"] and parsed["time_window_days"] == 7 and parsed["match_phrases"] == ["p"]
    assert parsed["intent"] == "search" and parsed["website_only"] is False
    assert lg._parse_router_json('{"intent": "nonsense"}') is None


# ── routes.search with a stubbed router (unit, fake DB) ─────────────────────
def entry_of(app_, key):
    return credits_doc(app_)["res"][sc.request_id("o1", key)]


def test_router_proposal_is_charged_and_stored_on_the_entry(app):
    app["state"]["router"] = routed(effort="deep", cost=11, reason="broad multi platform research")
    resp = app["send"](QUERY, submit_key=KEY1)
    assert resp.status_code == 303 and used(app) == 11
    e = entry_of(app, KEY1)
    assert (e["cost"], e["effort"], e["cost_reason"], e["state"]) == (11, "deep", "broad multi platform research",
                                                                         sc.COMMITTED)
    assert sc.check_invariant(credits_doc(app)) == []
    assert app["state"]["llm_calls"] == 1                      # one router call, no separate classifier call


@pytest.mark.parametrize("extra", [{}, {"effort": "deep"}, {"effort": "deep", "cost": 4}, {"effort": "standard", "cost": 4.0},
                                   {"effort": "greeting", "cost": 0}, {"effort": "complex", "cost": "6"},
                                   {"effort": "standard", "cost": True}])
def test_invalid_or_missing_proposal_costs_the_fallback_4(app, extra):
    app["state"]["router"] = routed(**extra)
    app["send"](QUERY, submit_key=KEY1)
    e = entry_of(app, KEY1)
    assert used(app) == 4 and (e["cost"], e["effort"]) == (4, "standard") and e["cost_reason"].startswith("fallback:")
    assert app["state"]["llm_calls"] == 1


def test_router_failure_is_charged_the_fallback_without_a_second_classification(app, monkeypatch):
    calls = []

    def failing(system, user, max_tokens=None, enable_web_search=False, force_json_prefill=False):
        calls.append(1)
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(app["lg"], "_call_claude", failing)
    resp = app["send"](QUERY, submit_key=KEY1)
    assert resp.status_code == 303 and used(app) == 4
    assert len(calls) == 2           # the router's own pre-existing 2 attempts; nothing added by the credit code
    assert entry_of(app, KEY1)["cost"] == 4


def test_small_talk_chat_clarify_and_blocked_never_reserve(app):
    app["send"]("Hi", submit_key=KEY1)                            # fast path: no router call at all
    assert app["state"]["llm_calls"] == 0 and credits_doc(app) == {}
    for router in (CHAT_ROUTER, {"intent": "clarify", "reply": "Which brand?", "keywords": None},
                   {"intent": "blocked", "reply": "I can't help with that.", "keywords": None}):
        app["state"]["router"] = {**router, "effort": "deep", "cost": 11}
        resp = app["send"]("Tell me something about the weather outlook", submit_key=KEY2)
        assert resp.status_code == 303
    assert credits_doc(app) == {} and app["state"]["enqueues"] == []


def test_same_key_retry_never_charges_or_classifies_again(app):
    app["state"]["router"] = routed(effort="deep", cost=11)
    first = app["send"](QUERY, submit_key=KEY1)
    calls = app["state"]["llm_calls"]
    app["state"]["router"] = routed(effort="standard", cost=3)
    again = app["send"](QUERY, submit_key=KEY1)
    assert again.headers["location"] == first.headers["location"]
    assert used(app) == 11 and app["state"]["llm_calls"] == calls and entry_of(app, KEY1)["cost"] == 11


def test_cost_larger_than_the_remaining_credits_is_denied_without_any_charge(app):
    app["state"]["limit"] = 14
    app["state"]["router"] = routed(effort="deep", cost=11)
    app["send"](QUERY, submit_key=KEY1)
    assert used(app) == 11
    denied = app["send"]("Find churn complaints about Notion", submit_key=KEY2)    # needs 11, only 3 left
    assert denied.status_code == 429 and denied.headers["X-Flintel-Denial"] == "credits-exhausted"
    assert "out of free searches" in body(denied)
    assert used(app) == 11 and len(app["state"]["enqueues"]) == 1                  # no partial charge, no enqueue
    assert sc.check_invariant(credits_doc(app)) == []


@pytest.mark.parametrize("limit,cost,accepted", [(14, 14, True), (13, 14, False), (10, 11, False), (10, 4, True),
                                                 (3, 4, False), (1, 4, False), (4, 4, True)])
def test_free_allowance_below_the_search_cost_denies_and_never_clamps(app, limit, cost, accepted):
    app["state"]["limit"] = limit
    effort = "deep" if cost > 8 else "standard"
    app["state"]["router"] = routed(effort=effort, cost=cost)
    resp = app["send"](QUERY, submit_key=KEY1)
    if accepted:
        assert resp.status_code == 303 and used(app) == cost
    else:
        assert resp.status_code == 429 and credits_doc(app) == {} and app["state"]["enqueues"] == []
        assert "out of free searches" in body(resp)


def test_exhausted_message_text_is_unchanged():
    assert sc.EXHAUSTED_MESSAGE == ("You're out of free searches until {reset}. You can still open your chats and ask "
                                    "follow-up questions.")
    assert sc.UNAVAILABLE_MESSAGE == "We couldn't verify your search credits right now. Please try again in a moment."


def test_kill_switch_reserves_nothing_whatever_the_router_proposes(app, monkeypatch):
    rt = app["rt"]
    legacy = []
    monkeypatch.setattr(rt, "_credit_store", lambda: None)
    monkeypatch.setattr(rt, "enqueue_search_job", lambda tk, kws, plat: legacy.append(tk))
    app["state"]["router"] = routed(effort="deep", cost=14)
    assert app["send"](QUERY, submit_key=KEY1).status_code == 303
    assert len(legacy) == 1 and credits_doc(app) == {}


def test_only_the_credited_search_path_uses_credit_cost():
    tree = ast.parse((ROOT / "routes.py").read_text(encoding="utf-8"))
    users = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) and inner.value.id == "credit_cost":
                    users.add(node.name)
    assert users == {"_run_credited_search", "_credit_precheck_response"}


def test_get_chat_history_and_stream_routes_never_touch_credits():
    tree = ast.parse((ROOT / "routes.py").read_text(encoding="utf-8"))
    allowed = {"search", "api_me_usage", "_credit_store", "_run_credited_search", "_credit_precheck_response"}
    offenders = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in allowed:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) \
                        and inner.value.id in ("search_credits", "credit_cost"):
                    offenders.add(node.name)
    assert offenders <= {"_credit_decision_response", "_format_credit_reset", "_enqueue"}   # _enqueue is nested in _run_credited_search


# ── how often the cost is decided (unit, fake DB, stubbed model) ────────────
# The "classifier" is the router's own single call, which already exists: the
# effort fields ride on it, so there is no separate model call to count. What is
# counted is (a) router calls and (b) how often the server decides a cost.
@pytest.fixture
def counted(app, monkeypatch):
    calls = []
    real = cc.select_cost

    def spy(routed_):
        calls.append(routed_)
        return real(routed_)
    monkeypatch.setattr(app["rt"].credit_cost, "select_cost", spy)
    return calls


def test_a_new_search_decides_its_cost_exactly_once_from_the_one_router_call(app, counted):
    app["state"]["router"] = routed(effort="complex", cost=6)
    app["send"](QUERY, submit_key=KEY1)
    assert app["state"]["llm_calls"] == 1 and len(counted) == 1 and used(app) == 6


def test_finished_and_in_flight_retries_make_no_router_call_and_decide_no_cost(app, counted):
    app["state"]["router"] = routed(effort="complex", cost=6)
    app["send"](QUERY, submit_key=KEY1)
    llm, decided = app["state"]["llm_calls"], len(counted)
    app["send"](QUERY, submit_key=KEY1)                                   # finished: redirect
    assert (app["state"]["llm_calls"], len(counted), used(app)) == (llm, decided, 6)
    rid2 = sc.request_id("o1", KEY2)
    app["db"].chats.insert_one({"chat_id": "cflight", "owner_key": "o1", "title": "t", "messages": []})
    store = app["store"]()
    t = store.reserve("o1", "anon", rid2, "x", "cflight", True, cost=9)
    store.start(t)
    store.claim(t)                                                        # sent, still in flight
    before = used(app)
    resp = app["send"](QUERY, submit_key=KEY2)
    assert resp.status_code == 200 and sc.IN_FLIGHT_MESSAGE in body(resp)
    assert (app["state"]["llm_calls"], len(counted), used(app)) == (llm, decided, before)


def test_non_search_turns_never_decide_a_cost(app, counted):
    app["send"]("Hi", submit_key=KEY1)
    assert app["state"]["llm_calls"] == 0 and counted == []
    for i, router in enumerate((CHAT_ROUTER, {"intent": "clarify", "reply": "Which brand?", "keywords": None},
                                {"intent": "blocked", "reply": "No.", "keywords": None})):
        app["state"]["router"] = {**router, "effort": "deep", "cost": 14}
        app["send"]("Tell me something about the weather outlook", submit_key=f"{i}2222222-aaaa-4bbb-8ccc-000000000009")
    assert counted == [] and credits_doc(app) == {}


def test_an_abandoned_retry_reuses_the_stored_cost_and_ignores_the_new_proposal(app, counted):
    rid = sc.request_id("o1", KEY1)
    app["db"].chats.insert_one({"chat_id": "c1", "owner_key": "o1", "title": "t", "messages": []})
    app["store"]().reserve("o1", "anon", rid, "x", "c1", True, cost=5, effort="complex", cost_reason="first")
    old = sc._aware(sc.datetime.now(sc.timezone.utc)) - sc.timedelta(hours=2)
    app["db"].flintel_credit_usage.update_one({"owner_key": "o1"}, {"$set": {f"res.{rid}.at": old}})
    app["state"]["router"] = routed(effort="deep", cost=14)               # a different proposal on the retry
    resp = app["send"](QUERY, submit_key=KEY1)
    # The router still runs once (it supplies the job's keywords, pre-existing flow); its cost is not charged.
    assert resp.status_code == 303 and app["state"]["llm_calls"] == 1
    e = entry_of(app, KEY1)
    assert used(app) == 5 and (e["cost"], e["effort"], e["cost_reason"]) == (5, "complex", "first")
    assert sc.check_invariant(credits_doc(app)) == []


def test_kill_switch_decides_no_cost_and_reserves_nothing(app, counted, monkeypatch):
    rt = app["rt"]
    monkeypatch.setattr(rt, "_credit_store", lambda: None)
    monkeypatch.setattr(rt, "enqueue_search_job", lambda tk, kws, plat: None)
    app["state"]["router"] = routed(effort="deep", cost=14)
    assert app["send"](QUERY, submit_key=KEY1).status_code == 303
    assert counted == [] and credits_doc(app) == {}
    assert app["state"]["llm_calls"] == 1      # the pre-existing router call; no credit code made any model call
