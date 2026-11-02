"""
tests/test_active_intent_kinds.py — which router request kinds may reach the Active Intent classifier (and so the Live Reddit search).
"A plain lead search" and "research" may; provider_search, person_mentions and mention_search may not. The classifier - not the kind -
decides whether a request is active demand, and its answer / confidence rules are untouched. All mocked; no network, no OpenAI call.
"""
import copy
import json
import logging

import pytest

mongomock = pytest.importorskip("mongomock")
import active_intent as ain  # noqa: E402
import reddit_live as rl  # noqa: E402
from tests.test_active_intent import (CRM, HURL, ai_on, answer_json, attach_classifier, cls_resp, hrow, live_env, prepare,  # noqa: F401,E402
                                      stored_msg, _no_network, _no_inflight_leak, _reset_factory)
from tests.test_reddit_live import message, on  # noqa: F401,E402

QUERY = "find people actively looking for AI agents"
NINE = ["ai agent developer", "build ai agent", "ai automation agency", "need ai assistant", "hire ai agent builder",
        "customer support ai agent", "ai agent for small business", "looking for ai agents", "ai agents recommendations"]


def env_for(kind=None, **over):
    extra = {"keywords": list(NINE), "query": QUERY}
    if kind:
        extra.update(request_kind=kind, subject="ai agents")
    extra.update(over)
    return live_env(msg_over=extra)


# ── eligibility, before and after ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind,ok,reason", [(None, True, "ok"), ("research", True, "ok"), ("provider_search", False, "kind"),
                                            ("person_mentions", False, "kind"), ("mention_search", False, "kind"), ("made_up", False, "kind")])
def test_eligible_pre_answer_by_kind(kind, ok, reason):
    msg = message(0, claude_answer=None, answered_at=None, **({"request_kind": kind} if kind else {}))
    assert rl.eligible_pre_answer(msg) == (ok, reason)


@pytest.mark.parametrize("over,reason", [({"website_only": True}, "website"), ({"keywords": []}, "no_keywords"),
                                         ({"credit_request_id": None}, "no_credit_request_id"), ({"failed_at": 1}, "closed")])
def test_research_kind_still_obeys_every_other_rule(over, reason):
    assert rl.eligible_pre_answer(message(0, claude_answer=None, request_kind="research", **over)) == (False, reason)


def test_the_post_answer_links_endpoint_rule_is_unchanged():
    from datetime import timedelta
    now = rl.timerange.to_utc(message(0)["answered_at"]) + timedelta(seconds=5)
    base = message(0, claude_answer=json.dumps({"format": "source_list", "platforms": []}))
    assert rl.eligible(dict(base, request_kind="research"), now) == (False, "kind")           # still refused there
    assert rl.eligible(dict(base, request_kind="mention_search"), now)[1] != "kind"           # still allowed past the kind rule there
    assert rl.ALLOWED_KINDS == (None, "mention_search")


# ── the classifier still decides ───────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", [None, "research"])
def test_a_high_confidence_active_demand_answer_reaches_the_live_search_with_the_routers_keywords(ai_on, kind):
    env = env_for(kind)
    msg = stored_msg(env)
    before = copy.deepcopy(msg["keywords"])
    run = prepare(env, msg=msg)
    assert run.active is True and len(env.cls_calls) == 1 and msg["keywords"] == before == NINE            # keywords untouched
    run.start()
    run.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    assert len(env.calls) == 1
    p = env.calls[0]["payload"]
    assert "Search reddit.com for posts matching these keywords: " + ", ".join(NINE) in p["input"]            # reused, not regenerated
    assert p["max_tool_calls"] == 1 and p["tools"][0]["filters"] == {"allowed_domains": ["reddit.com"]}
    assert all(k in env.cls_calls[0]["payload"]["input"] for k in NINE[:3])                                  # the classifier saw them too


def test_no_keyword_generation_exists_in_the_active_intent_path():
    import intent_classifier as ic
    for mod in (ain, ic, rl):
        src = open(mod.__file__, encoding="utf-8").read()
        for name in ("generate_keywords", "generate_keywords_for_website_request", "route_user_message", "CLAUDE_ROUTER"):
            assert name not in src, (mod.__name__, name)


@pytest.mark.parametrize("name,setup", [
    ("negative", lambda e: setattr(e, "cls_override", cls_resp(False, "high"))),
    ("low confidence", lambda e: setattr(e, "cls_override", cls_resp(True, "low"))),
    ("medium confidence", lambda e: setattr(e, "cls_override", cls_resp(True, "medium"))),
    ("timeout", lambda e: setattr(e, "cls_raises", TimeoutError("slow"))),
    ("malformed", lambda e: setattr(e, "cls_override", cls_resp(text="maybe"))),
])
@pytest.mark.parametrize("kind", [None, "research"])
def test_negative_uncertain_and_failed_classifications_never_reach_live(ai_on, kind, name, setup):
    env = env_for(kind)
    setup(env)
    msg = stored_msg(env)
    run = prepare(env, msg=msg)
    assert run.active is False and env.calls == [] and msg["time_range"] is None and len(env.cls_calls) == 1


@pytest.mark.parametrize("kind", ["provider_search", "person_mentions", "mention_search"])
def test_other_kinds_are_rejected_before_the_classifier_is_ever_asked(ai_on, kind):
    env = env_for(kind)
    msg = stored_msg(env)
    run = prepare(env, msg=msg)
    assert run.active is False and env.cls_calls == [] and env.calls == [] and msg["time_range"] is None


# ── Historical Search and the merged response are unaffected ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["provider_search", "person_mentions", "mention_search"])
def test_historical_search_and_the_answer_are_untouched_when_active_intent_rejects(ai_on, kind):
    env = env_for(kind)
    msg = stored_msg(env)
    before = copy.deepcopy(msg)
    run = prepare(env, msg=msg)
    pool = [hrow(1, url=HURL), hrow(2, age_minutes=900)]
    snap = copy.deepcopy(pool)
    base = answer_json(HURL)
    assert msg == before and run.historical(pool) is pool and pool == snap and run.finish(base, historical=pool) == base


def test_an_accepted_research_search_changes_only_the_in_memory_time_range_and_adds_only_the_live_block(ai_on):
    env = env_for("research")
    msg = stored_msg(env)
    before = copy.deepcopy(msg)
    run = prepare(env, msg=msg)
    assert {k for k in set(msg) | set(before) if msg.get(k) != before.get(k)} == {"time_range"}          # the shared interval, in memory
    base = answer_json(HURL)
    out = json.loads(run.finish(base, historical=[hrow(1, url=HURL)]))
    expected = json.loads(base)
    assert {k: v for k, v in out.items() if k != "live_candidates"} == expected                           # the answer is otherwise identical
    lc = out["live_candidates"]
    assert lc["source"] == "live_web_search" and lc["verification"] == "date_verified_title_and_url_only" and lc["items"]


# ── the rejection log ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_kind_rejection_log_names_the_router_kind_and_never_the_query(ai_on, caplog):
    caplog.set_level(logging.INFO, logger="flintel.active_intent")
    prepare(env_for("provider_search"))
    prepare(env_for("mystery"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert '"reason": "kind"' in text and '"request_kind": "provider_search"' in text
    assert "mystery" not in text and QUERY not in text
