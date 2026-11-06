"""
Search modes and credits. The credit policy itself is untouched; these tests pin down that the mode cannot change it, using the existing
credit-route harness (real POST /search handler, mongomock credit store, stub router/LLM). No pricing, plan or amount is modified.

Intended (existing) policy, verified by the existing suites and re-checked here for every mode:
  * the cost of a search comes only from the router's validated proposal (credit_cost.select_cost) - the mode is not an input;
  * it is charged once, before retrieval, under request_id(owner, submit_key); the same key again is a free redirect;
  * an exhausted owner is denied (429) before any job is enqueued; small talk is free.
"""
import re
from pathlib import Path

import pytest
from starlette.requests import Request

from tests.test_search_credit_routes import (app, flat_cost, used, credits_doc, KEY1, KEY2, CHAT_ROUTER, sc)  # noqa: F401
from tests.test_answer_persistence import env  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
Q = "Find people looking for a CRM for small teams"
MODES = [None, "live", "historical", "combined", "LIVE ", "bogus", "'; drop table", "x" * 5000]


def send(a, query, key, mode):
    kw = {} if mode is None else {"search_mode": mode}
    rt, idx = a["rt"], a["idx"]
    req = Request({"type": "http", "method": "POST", "path": "/search", "headers": [], "query_string": b"",
                   "session": a.setdefault("_session", {}), "app": idx.app, "router": idx.app.router})
    return rt.search(req, query=query, platform="All Platforms", chat_id=None, submit_key=key, **kw)


def test_every_mode_costs_exactly_what_no_mode_costs(app):
    """Real select_cost (no flat_cost stub): the charge for a mode (valid, odd or hostile) equals the charge without one."""
    costs = []
    for i, mode in enumerate(MODES):
        a_used = used(app)
        key = "%08d-aaaa-4bbb-8ccc-000000000001" % (i + 1)
        r = send(app, Q, key, mode)
        assert r.status_code == 303, mode
        costs.append(used(app) - a_used)
    assert len(set(costs)) == 1 and costs[0] >= 1, costs


def test_combined_is_one_charge_and_one_job_not_one_per_path(app):
    r = send(app, Q, KEY1, "combined")
    assert r.status_code == 303 and len(app["state"]["enqueues"]) == 1
    rid = sc.request_id("o1", KEY1)
    assert list(credits_doc(app)["res"]) == [rid] and credits_doc(app)["res"][rid]["state"] == sc.COMMITTED
    assert sc.check_invariant(credits_doc(app)) == []


@pytest.mark.parametrize("first,second", [("live", "live"), ("combined", "historical"), (None, "live"), ("live", "combined")])
def test_a_retry_with_the_same_key_never_charges_twice_whatever_the_mode(app, first, second):
    a = send(app, Q, KEY1, first)
    once, calls = used(app), app["state"]["llm_calls"]
    b = send(app, Q, KEY1, second)
    assert b.status_code == 303 and b.headers["location"] == a.headers["location"]
    assert used(app) == once and len(app["state"]["enqueues"]) == 1 and app["state"]["llm_calls"] == calls
    chat = app["db"].chats.find_one({"chat_id": a.headers["location"].rsplit("/", 1)[-1]})
    assert len(chat["messages"]) == 1                                   # not a second message either


def test_switching_mode_on_a_deliberate_new_submit_is_a_new_search_with_a_new_charge(app):
    """Existing policy (test_two_intentional_submits...): a new submit key is a new search. Changing the mode does not make it free or cheaper."""
    send(app, Q, KEY1, "historical")
    one = used(app)
    send(app, Q, KEY2, "live")
    assert used(app) == 2 * one


@pytest.mark.usefixtures("flat_cost")          # as the existing exhaustion tests: a 1-credit charge against a 1-credit allowance
@pytest.mark.parametrize("mode", [None, "live", "historical", "combined", "bogus"])
def test_an_exhausted_owner_is_denied_in_every_mode_before_any_job(app, mode):
    app["state"]["limit"] = 1
    send(app, Q, KEY1, None)
    start = used(app)
    denied = send(app, "Find churn complaints about Notion", KEY2, mode)
    assert denied.status_code == 429 and denied.headers["X-Flintel-Denial"] == "credits-exhausted"
    assert used(app) == start and len(app["state"]["enqueues"]) == 1


@pytest.mark.parametrize("mode", ["live", "combined"])
def test_small_talk_stays_free_in_a_mode(app, mode):
    app["state"]["router"] = CHAT_ROUTER
    r = send(app, "Thanks, what else can you research?", KEY1, mode)
    assert r.status_code == 303 and used(app) == 0 and app["state"]["enqueues"] == []


def test_the_stored_mode_is_always_a_valid_enum_never_the_raw_input(app):
    for i, mode in enumerate(["LIVE ", "bogus", "'; drop table", "x" * 5000]):
        r = send(app, Q, "%08d-aaaa-4bbb-8ccc-0000000000aa" % i, mode)
        chat = app["db"].chats.find_one({"chat_id": r.headers["location"].rsplit("/", 1)[-1]})
        assert chat["messages"][-1].get("search_mode") in ("live", "combined")           # "LIVE " is trimmed + folded; the rest fall to the default


def test_retrieval_code_has_no_access_to_the_credit_store():
    """Partial failure / cache / retrieval can't charge: the mode layer and the pure module never touch credits."""
    for f in ("search_modes.py",):
        assert not re.search(r"credit", (ROOT / f).read_text(), re.I)
    src = (ROOT / "logics.py").read_text()
    layer = src[src.index("def _live_state("):src.index("# TOPIC EVIDENCE CACHE")]
    assert not re.search(r"credit|CreditStore|search_credits", layer)


# ── Hacker News-only billing: documented, not changed ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", [None, "live", "historical", "combined"])
def test_hn_only_search_is_charged_like_any_search_and_enqueues_no_collector_job(app, monkeypatch, mode):
    """Existing behaviour from the Hacker News feature: nothing is collected for HN alone, so no job is queued (expects_job=False, the same
    path website-URL searches use, which the existing suite shows are charged). The charge, the committed ledger entry and the saved message
    are identical to a normal search; only the job is absent. Whether HN-only should cost the same is a product decision, not changed here."""
    monkeypatch.setattr(app["rt"], "hackernews_only", lambda p: True)
    normal_before = used(app)
    r = send(app, Q, KEY1, mode)
    assert r.status_code == 303 and app["state"]["enqueues"] == []                  # no collector job
    rid = sc.request_id("o1", KEY1)
    assert used(app) - normal_before >= 1 and credits_doc(app)["res"][rid]["state"] == sc.COMMITTED
    assert sc.check_invariant(credits_doc(app)) == []
    chat = app["db"].chats.find_one({"chat_id": r.headers["location"].rsplit("/", 1)[-1]})
    assert len(chat["messages"]) == 1


def test_hn_only_costs_the_same_as_a_search_that_does_enqueue(app, monkeypatch):
    send(app, Q, KEY1, None)
    normal = used(app)
    monkeypatch.setattr(app["rt"], "hackernews_only", lambda p: True)
    send(app, Q, KEY2, None)
    assert used(app) - normal == normal
