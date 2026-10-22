"""
tests/test_credit_corrections.py - the corrective pass on guest credits and the first-paid carryover:

  1. routed-cost accounting for guest searches when credit enforcement is OFF (the kill switch), fail-closed behaviour
  2. the handed-over guest record is CLOSED: only the legitimate hold token can finish; stale / released / never-held tokens
     and new reservations can not count (race + duplicate-retry tests)
  3. a handover whose target month is over: recoverable "pending_expired", explicit one-time resolution, and the refusal
     (retryable) of a first paid activation while it is unresolved
  4. a counted answer whose delivery failed after the handover: the count is kept, observable, and re-delivered without
     recounting
  5. first-paid carryover when the free window is the FOLLOWING month; cancellation followed by reactivation

Evidence class: mongomock + forced interleavings (a collection proxy that runs a callback right before / after a chosen
write) with a fixed clock. This is logical evidence that the conditional filters enforce the rules; it is NOT proof of
behaviour under real MongoDB concurrency, and nothing here talks to Lemon Squeezy, Google or a mailbox.
"""
import datetime as dt
import json
import logging
import sys

import pytest

from tests.test_guest_credits import (  # noqa: F401  (fixtures + helpers)
    EMAIL, G, OTHER, T0, UTC, Clock, acct, acct_usage, acct_used, clock, coll, finish, inv_ok, world,
)
from tests.test_guest_gate import (  # noqa: F401  (routes harness)
    chat_of, gapp, gate_doc, gg, last_token, _capture_real_identity_check,
)
from tests.test_answer_persistence import env, GOOD  # noqa: F401
from tests.test_search_credit_routes import app, KEY1, KEY2  # noqa: F401
from tests.test_credit_cost import QUERY, counted, routed  # noqa: F401
from tests.test_credit_carryover import (  # noqa: F401
    ALICE, D, NOW, T1, FREE, activate, carry, created_raw, deliver_c, entry_of, free_charge, plan, store, sub_event, usage,
)
from tests.test_credit_carryover import inv_ok as carry_inv_ok  # noqa: F401
from tests.test_billing_hardening import cfg, db, wd  # noqa: F401

mongomock = pytest.importorskip("mongomock")
import billing  # noqa: E402
import guest_gate  # noqa: E402
import search_credits as sc  # noqa: E402

ANSWER = json.dumps(GOOD)


# ═════════════════ 1. routed cost with the kill switch ═════════════════
@pytest.fixture
def killed(gapp, monkeypatch):
    """Credit enforcement OFF (the kill switch) + the guest gate ON in credit mode (allowance 5), for the guest `guest-1`."""
    rt = gapp["rt"]
    monkeypatch.setattr(sys.modules["config"], "GUEST_CREDIT_ALLOWANCE", 5, raising=False)
    monkeypatch.setattr(rt, "_credit_store", lambda: None)
    seen = []

    def fake_enqueue(topic_key, keywords, platform):
        doc = gate_doc(gapp)
        seen.append(list(doc.get("token_costs") or []))        # what was bound at the moment the job was queued
    monkeypatch.setattr(rt, "enqueue_search_job", fake_enqueue)
    gapp["seen"] = seen
    return gapp


@pytest.mark.parametrize("effort,cost", [("standard", 3), ("complex", 6), ("deep", 14)])
def test_kill_switch_guest_search_is_counted_at_its_routed_cost_bound_before_the_enqueue(killed, counted, effort, cost):
    killed["state"]["router"] = routed(effort=effort, cost=cost, reason="many angles")
    resp = killed["send"](QUERY, submit_key=KEY1)
    assert resp.status_code == 303
    assert len(counted) == 1                                           # exactly one cost decision, by the existing rules
    assert killed["seen"] and killed["seen"][0][-1]["c"] == cost       # bound BEFORE the job was enqueued
    assert gate_doc(killed)["inflight"]["cost"] == cost
    killed["gg"].on_answer_complete("guest-1", chat_of(resp), "t", last_token(killed))
    doc = gate_doc(killed)
    assert doc["credits_used"] == cost and "cost_fallback_tokens" not in doc    # the REAL cost, never the fixed one
    assert killed["db"].flintel_credit_usage.count_documents({}) == 0           # kill switch: no credit document at all
    nxt = killed["send"]("Find churn complaints about Notion", submit_key=KEY2)
    if cost >= 5:                                                      # the allowance (5) is reached: an account is needed
        assert nxt.status_code == 403 and nxt.headers["X-Flintel-Denial"] == "guest-auth-required"
    else:
        assert nxt.status_code == 303                                  # 3 < 5: still room (a search may overshoot, never be refused up front)


def test_kill_switch_standard_cost_below_the_allowance_leaves_room(killed, counted):
    killed["state"]["router"] = routed(effort="standard", cost=3)
    resp = killed["send"](QUERY, submit_key=KEY1)
    killed["gg"].on_answer_complete("guest-1", chat_of(resp), "t", last_token(killed))
    assert gate_doc(killed)["credits_used"] == 3
    again = killed["send"]("Find churn complaints about Notion", submit_key=KEY2)
    assert again.status_code == 303                                    # 3 < 5: the next search is still allowed


def test_kill_switch_invalid_router_output_uses_the_existing_fallback_rule_and_records_why(killed, counted):
    killed["state"]["router"] = routed(effort="bogus", cost=3)
    assert killed["send"](QUERY, submit_key=KEY1).status_code == 303
    entry = gate_doc(killed)["token_costs"][-1]
    import credit_cost
    assert entry["c"] == credit_cost.fallback_cost() and entry["r"].startswith("fallback:")   # visible, not silent


def test_kill_switch_cost_failure_fails_closed_nothing_reserved_or_enqueued(killed, counted, monkeypatch):
    def boom(_routed):
        raise RuntimeError("cost calculation broke")
    monkeypatch.setattr(killed["rt"].guest_gate, "routed_cost", boom)
    resp = killed["send"](QUERY, submit_key=KEY1)
    assert resp.status_code != 303 and killed["seen"] == []
    assert killed["gate_coll"].count_documents({}) == 0               # no reservation was written


def test_a_cost_that_is_not_usable_also_fails_closed(killed, counted, monkeypatch):
    import credit_cost
    monkeypatch.setattr(killed["rt"].credit_cost, "select_cost", lambda r_: credit_cost.CostDecision(0, "standard", "x"))
    resp = killed["send"](QUERY, submit_key=KEY1)
    assert resp.status_code != 303 and killed["gate_coll"].count_documents({}) == 0 and killed["seen"] == []


def test_gate_off_makes_no_cost_decision(killed, counted, monkeypatch):
    monkeypatch.setattr(sys.modules["config"], "GUEST_CREDIT_ALLOWANCE", 0, raising=False)
    assert killed["send"](QUERY, submit_key=KEY1).status_code == 303
    assert counted == [] and killed["gate_coll"].count_documents({}) == 0


def test_a_registered_user_under_the_kill_switch_makes_no_cost_decision(killed, counted):
    killed["owner"].update(key="member@example.com", type="email")
    assert killed["send"](QUERY, submit_key=KEY1).status_code == 303
    assert counted == [] and killed["gate_coll"].count_documents({}) == 0


def test_credit_enabled_guest_search_binds_the_stored_charge_before_the_enqueue(gapp, monkeypatch):
    rt = gapp["rt"]
    monkeypatch.setattr(sys.modules["config"], "GUEST_CREDIT_ALLOWANCE", 5, raising=False)
    seen = []
    real = rt.enqueue_search_job

    def spy(topic_key, keywords, platform, credit_request_id=None, attribute=False):
        seen.append(list(gate_doc(gapp).get("token_costs") or []))
        return real(topic_key, keywords, platform, credit_request_id=credit_request_id, attribute=attribute)
    monkeypatch.setattr(rt, "enqueue_search_job", spy)
    gapp["state"]["router"] = routed(effort="complex", cost=6)
    assert gapp["send"](QUERY, submit_key=KEY1).status_code == 303
    assert seen and seen[0][-1]["c"] == 6
    entry = next(iter(gapp["db"].flintel_credit_usage.find_one({"owner_key": "guest-1"})["res"].values()))
    assert entry["cost"] == 6                                          # the gate holds the SAME cost the store charged


def test_a_search_with_no_bound_cost_is_charged_the_fallback_and_audited(world, caplog):
    import credit_cost
    gate = world.gate
    assert gate.reserve(G, "old", "t", "c").kind == "allow"            # reserved by code that bound no cost
    with caplog.at_level(logging.WARNING):
        assert gate.complete(G, "old", "c", "t") == "completed"
    doc = world.coll.find_one({"_id": G})
    assert doc["credits_used"] == credit_cost.fallback_cost() and doc["cost_fallback_tokens"] == ["old"]
    assert any("no bound cost" in r.getMessage() for r in caplog.records)
    assert finish(gate, "g2", "new", 4) == "completed" and "cost_fallback_tokens" not in world.coll.find_one({"_id": "g2"})


# ═════════════════ 2. the closed record: race + duplicate-retry tests ═════════════════
class Hook:
    """Collection proxy: runs callbacks right before / after an update_one that matches a predicate."""

    def __init__(self, coll):
        self._c = coll
        self.before, self.after = [], []

    def __getattr__(self, name):
        return getattr(self._c, name)

    def update_one(self, filt, update, *a, **k):
        for item in list(self.before):
            if item[0](filt, update):
                self.before.remove(item)
                item[1]()
        result = self._c.update_one(filt, update, *a, **k)
        for item in list(self.after):
            if item[0](filt, update):
                self.after.remove(item)
                item[1]()
        return result


def claims(world):
    return world.gg.handover_to_account(G, EMAIL, now=world.clock.t)


def test_the_hold_token_completes_once_even_when_everything_is_repeated(world):
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    assert claims(world)["applied_hold"] == 4 and acct_used(world) == 4
    assert world.gate.complete(G, "run", "c", "t") == "completed"
    assert world.gg._settle_hold(G, "run") == "committed"
    for _ in range(2):                                                  # duplicate completion / settle / reconcile / claim
        assert world.gate.complete(G, "run", "c", "t") == "duplicate"
        assert world.gg._settle_hold(G, "run") == "noop"
        assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) in ("seeded", "noop", None)
        assert claims(world)["status"] == "duplicate"
    assert world.gate.state(G)["credits_used"] == 4 and acct_used(world) == 4
    inv_ok(world)


def test_the_hold_token_cannot_count_after_reconcile_fenced_it_and_the_refund_is_exactly_once(world):
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    claims(world)
    world.clock.advance(901)                                            # the search is gone (stale)
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) == "released" and acct_used(world) == 0
    assert world.gate.complete(G, "run", "c", "t") != "completed"       # a late finisher is refused
    assert world.gate.state(G)["credits_used"] == 0 and acct_used(world) == 0
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "auth_required"      # and cannot restart either
    for _ in range(2):
        assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) in ("noop", None)
    assert acct_used(world) == 0
    inv_ok(world)


def test_a_hold_that_went_stale_after_the_claim_may_still_finish_before_reconcile_settles_it(world):
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    claims(world)
    world.clock.advance(901)
    assert world.gate.complete(G, "run", "c", "t") == "completed"       # not yet fenced: still the legitimate hold
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) == "committed"
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) in ("noop", None)
    assert world.gate.state(G)["credits_used"] == 4 and acct_used(world) == 4
    inv_ok(world)


@pytest.mark.parametrize("credits_before", [0, 3])
def test_a_reservation_stale_at_claim_time_can_not_count_afterwards(world, credits_before):
    if credits_before:
        assert finish(world.gate, G, "done", credits_before) == "completed"
    assert world.gate.reserve(G, "old", "t", "c", cost=4).kind == "allow"
    world.clock.advance(1000)                                           # stale: the claim makes no hold for it
    out = claims(world)
    assert (out is None) == (credits_before == 0)                        # nothing to move for 0 credits: no account write
    handover = world.coll.find_one({"_id": G})["handover"]               # ... but the record IS claimed (closed) either way
    assert handover["hold"] is None and handover["snapshot"] == credits_before
    assert world.gate.complete(G, "old", "c", "t") != "completed"       # the late runner can not count
    assert world.gate.state(G)["credits_used"] == credits_before and acct_used(world) == credits_before
    assert world.gate.complete(G, "old", "c", "t") != "completed"       # duplicate attempt: same
    inv_ok(world)


def test_a_search_released_before_the_claim_can_not_be_retried_into_a_count(world):
    assert finish(world.gate, G, "done", 3) == "completed"
    assert world.gate.reserve(G, "failed", "t", "c", cost=4).kind == "allow"
    assert world.gate.release(G, "failed") is True                      # failed and released
    claims(world)
    assert world.gate.reserve(G, "failed", "t", "c", cost=4).kind == "auth_required"
    assert world.gate.complete(G, "failed", "c", "t") != "completed"
    assert world.gate.state(G)["credits_used"] == 3 and acct_used(world) == 3


def test_a_handed_over_record_refuses_new_reservations_and_reports_used(world):
    assert finish(world.gate, G, "done", 3) == "completed"             # 3 < 5: the guest would still have room
    claims(world)
    st = world.gate.state(G)
    assert st["handed_over"] is True and st["used"] is True
    assert world.gate.peek(G).kind == "auth_required"
    for i in range(2):
        assert world.gate.reserve(G, f"new{i}", "t", "c", cost=1).kind == "auth_required"
    assert world.gate.complete(G, "new0", "c", "t") != "completed"
    assert world.gate.state(G)["credits_used"] == 3 and acct_used(world) == 3


def test_a_reservation_racing_the_claim_makes_the_claim_retry_and_become_a_hold(world):
    assert finish(world.gate, G, "done", 3) == "completed"
    hook = Hook(world.gate.coll)
    raced = {}

    def reserve_now():
        raced["d"] = guest_gate.GuestGate(world.coll, clock=world.clock, credit_allowance=5, stale_seconds=900) \
            .reserve(G, "late", "t", "c", cost=4)
    hook.before.append((lambda f, u: "handover" in str(u) and "$set" in u, reserve_now))
    world.gate.coll = hook
    out = claims(world)
    assert raced["d"].kind == "allow"                                   # the search got in before the claim was written
    assert (out["applied_snapshot"], out["applied_hold"]) == (3, 4)     # so it is a HOLD, not an escape
    assert world.gate.complete(G, "late", "c", "t") == "completed" and world.gg._settle_hold(G, "late") == "committed"
    assert acct_used(world) == 7 and world.gate.state(G)["credits_used"] == 7
    inv_ok(world)


def test_the_hold_token_resumes_through_reserve_after_the_claim_and_a_released_one_does_not(world):
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    claims(world)
    d = world.gate.reserve(G, "run", "t", "c")                          # a background retry / stream resumes it
    assert d.kind == "allow" and d.resumed is True
    assert world.gg.on_work_start(G, "run", "t", "c", {}) == world.gg.PROCEED
    assert world.gate.fence_release(G, "run") is True
    assert world.gate.reserve(G, "run", "t", "c").kind == "auth_required"     # fenced: no longer legitimate
    assert world.gg.on_work_start(G, "run", "t", "c", {}) == world.gg.ABANDON


def test_a_second_account_can_not_take_a_claimed_record(world):
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    claims(world)
    assert world.gg.handover_to_account(G, OTHER, now=world.clock.t) is None
    assert acct_used(world, owner=OTHER) == 0 and acct_used(world) == 4


# ═════════════════ 3. a handover whose target month is over ═════════════════
MARCH = dt.datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
APRIL = dt.datetime(2026, 4, 5, 12, 0, tzinfo=UTC)


def claimed_in_march(world, credits=5):
    world.clock.t = MARCH
    assert finish(world.gate, G, "done", credits) == "completed"
    assert world.gate.claim_handover(G, EMAIL, MARCH)["period"] == "2026-03"       # the claim was made, the seed never ran
    world.clock.t = APRIL


def test_a_seed_after_the_target_month_is_pending_not_applied_and_not_moved(world):
    claimed_in_march(world)
    assert world.gg.reconcile_handover(G, EMAIL, now=APRIL) == "pending_expired"
    doc = acct_usage(world)
    assert doc["guest_handover_pending"]["period"] == "2026-03" and doc["guest_handover_pending"]["snapshot"] == 5
    assert "guest_handover" not in doc and not doc.get("used_by_period")        # nothing deducted anywhere, nothing 'complete'
    assert world.acct.status(EMAIL)["remaining"] == 40 and world.acct.has_pending_guest_handover(EMAIL)
    before = acct_usage(world)
    for _ in range(3):                                                  # duplicate retries: no write, same answer
        assert world.gg.reconcile_handover(G, EMAIL, now=APRIL) == "pending_expired"
        assert world.gg.handover_to_account(G, EMAIL, now=APRIL)["status"] == "pending_expired"
    assert acct_usage(world) == before


def test_a_late_seed_inside_the_target_month_still_applies_and_a_future_target_is_not_expired(world):
    world.clock.t = MARCH
    assert finish(world.gate, G, "done", 5) == "completed"
    world.gate.claim_handover(G, EMAIL, MARCH)
    world.clock.t = MARCH + dt.timedelta(days=5)
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) == "seeded"
    assert acct_used(world, "2026-03") == 5 and not world.acct.has_pending_guest_handover(EMAIL)
    # a month-end claim targets April; the seed seconds later (still March) is a FUTURE month, applied normally
    g2, e2 = "guest-bbbb", "next.month@example.test"
    world.clock.t = dt.datetime(2026, 3, 31, 23, 59, 30, tzinfo=UTC)
    assert finish(world.gate, g2, "d2", 4) == "completed"
    out = world.gg.handover_to_account(g2, e2, now=world.clock.t)
    assert out["status"] == "applied" and acct_used(world, "2026-04", e2) == 4


def test_a_repeat_after_a_month_rollover_of_an_already_applied_handover_stays_a_duplicate(world):
    world.clock.t = MARCH
    assert finish(world.gate, G, "done", 5) == "completed"
    assert world.gg.handover_to_account(G, EMAIL, now=MARCH)["status"] == "applied"
    world.clock.t = APRIL
    assert world.gg.handover_to_account(G, EMAIL, now=APRIL)["status"] == "duplicate"
    assert not world.acct.has_pending_guest_handover(EMAIL) and acct_used(world, "2026-03") == 5


def test_apply_late_deducts_from_the_original_month_only_and_exactly_once(world):
    claimed_in_march(world)
    world.gg.reconcile_handover(G, EMAIL, now=APRIL)
    assert world.gg.resolve_handover(G, EMAIL, "apply_late", "ops-1", now=APRIL) == "applied_late"
    doc = acct_usage(world)
    assert acct_used(world, "2026-03") == 5 and acct_used(world, "2026-04") == 0     # March only; April untouched
    assert doc["guest_handover"]["late"] is True and doc["guest_handover"]["resolution"]["outcome"] == "apply_late"
    assert doc["guest_handover"]["resolution"]["reason"] == "ops-1" and "guest_handover_pending" not in doc
    assert world.acct.status(EMAIL)["remaining"] == 40                  # today's allowance is the April grant, unaffected
    before = acct_usage(world)
    for outcome in ("apply_late", "waive", "apply_late"):               # duplicate / conflicting resolutions
        assert world.gg.resolve_handover(G, EMAIL, outcome, "again", now=APRIL) == "duplicate"
    assert world.gg.reconcile_handover(G, EMAIL, now=APRIL) in ("seeded", None)
    assert acct_usage(world) == before
    inv_ok(world)


def test_waive_records_the_decision_and_deducts_nothing(world):
    claimed_in_march(world)
    world.gg.reconcile_handover(G, EMAIL, now=APRIL)
    assert world.gg.resolve_handover(G, EMAIL, "waive", "support goodwill", now=APRIL) == "waived"
    doc = acct_usage(world)
    assert doc["guest_handover"]["waived"] is True and doc["guest_handover"]["applied_snapshot"] == 0
    assert doc["guest_handover"]["resolution"]["reason"] == "support goodwill" and not doc.get("used_by_period")
    assert world.gg.resolve_handover(G, EMAIL, "apply_late", "x", now=APRIL) == "duplicate"
    assert not world.acct.has_pending_guest_handover(EMAIL)


def test_apply_late_is_clamped_to_the_original_months_room(world):
    claimed_in_march(world, credits=8)
    world.clock.t = MARCH                                               # the account spent 38 of March's 40 meanwhile
    for i, cost in enumerate((10, 10, 18)):
        t = world.acct.reserve(EMAIL, "email", f"r{i}", "t", "c", True, cost=cost)
        assert world.acct.start(t) == sc.DONE and world.acct.claim(t) == sc.DONE
    world.clock.t = APRIL
    world.gg.reconcile_handover(G, EMAIL, now=APRIL)
    assert world.gg.resolve_handover(G, EMAIL, "apply_late", "ops", now=APRIL) == "applied_late"
    assert acct_used(world, "2026-03") == 40 and acct_usage(world)["guest_handover"]["applied_snapshot"] == 2
    inv_ok(world)


def test_a_resolution_is_never_automatic_and_bad_requests_change_nothing(world):
    claimed_in_march(world)
    world.gg.reconcile_handover(G, EMAIL, now=APRIL)
    before = acct_usage(world)
    for args in ((G, EMAIL, "auto", "r"), (G, EMAIL, "waive", ""), (G, EMAIL, "waive", "   "), (G, EMAIL, None, "r"),
                 ("someone@x.test", EMAIL, "waive", "r")):
        assert world.gg.resolve_handover(*args, now=APRIL) == "invalid"
    assert world.gg.resolve_handover(G, OTHER, "waive", "r", now=APRIL) == "no_pending"       # not this account's guest
    assert world.gg.resolve_handover("guest-zzz", EMAIL, "waive", "r", now=APRIL) == "no_pending"
    for _ in range(3):
        world.gg.reconcile_handover(G, EMAIL, now=APRIL)
        world.gg.handover_to_account(G, EMAIL, now=APRIL)
    assert acct_usage(world) == before and world.acct.has_pending_guest_handover(EMAIL)


def test_a_hold_counted_is_included_a_hold_released_is_excluded_an_undecided_one_blocks_the_resolution(world):
    world.clock.t = MARCH
    assert finish(world.gate, G, "done", 3) == "completed"
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    world.gate.claim_handover(G, EMAIL, MARCH)
    world.clock.t = APRIL
    world.coll.update_one({"_id": G}, {"$set": {"inflight.started_at": APRIL}})       # still running in April
    world.gg.reconcile_handover(G, EMAIL, now=APRIL)
    assert world.acct.has_pending_guest_handover(EMAIL)
    assert world.gg.resolve_handover(G, EMAIL, "apply_late", "r", now=APRIL) == "hold_undecided"
    assert world.acct.has_pending_guest_handover(EMAIL) and "guest_handover" not in acct_usage(world)
    assert world.gate.complete(G, "run", "c", "t") == "completed"       # the hold finishes: counted
    assert world.gg.resolve_handover(G, EMAIL, "apply_late", "r", now=APRIL) == "applied_late"
    assert acct_used(world, "2026-03") == 7 and acct_usage(world)["guest_handover"]["applied_hold"] == 4


def test_a_released_hold_is_excluded_from_a_late_resolution(world):
    world.clock.t = MARCH
    assert finish(world.gate, G, "done", 3) == "completed"
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    world.gate.claim_handover(G, EMAIL, MARCH)
    world.clock.t = APRIL                                               # the search died long ago
    world.gg.reconcile_handover(G, EMAIL, now=APRIL)
    assert world.gg.resolve_handover(G, EMAIL, "apply_late", "r", now=APRIL) == "applied_late"
    assert acct_used(world, "2026-03") == 3 and acct_usage(world)["guest_handover"]["applied_hold"] == 0
    assert world.gate.complete(G, "run", "c", "t") != "completed"       # fenced: it can never count afterwards
    inv_ok(world)


# ═════════════════ 3b. pending handover versus the first paid activation (billing, real webhook path) ═════════════════
FEB = dt.datetime(2026, 2, 10, 12, 0, tzinfo=UTC)


class BClock(Clock):
    pass


@pytest.fixture
def bworld(cfg, wd, monkeypatch):
    """Billing world (webhook + plan record + credit store `wd`) with a guest gate whose handovers go to ALICE."""
    gclock = BClock(NOW)
    gate = guest_gate.GuestGate(mongomock.MongoClient()["bgate"]["g"], clock=gclock, credit_allowance=5, stale_seconds=900)
    holder = {"now": NOW}
    monkeypatch.setattr(guest_gate, "default_gate", lambda: gate)
    monkeypatch.setattr(guest_gate, "_account_store", lambda: store(wd, holder["now"]))
    monkeypatch.setattr(sc, "default_store", lambda: store(wd, holder["now"]))
    return type("B", (), {"gate": gate, "wd": wd, "clock": gclock, "holder": holder})


def b_guest(b, credits, at):
    b.clock.t = at
    assert finish(b.gate, G, "g1", credits) == "completed"


def test_a_pending_handover_makes_the_first_activation_retryable_until_an_explicit_resolution(bworld):
    wd = bworld.wd
    b_guest(bworld, 5, FEB)
    assert bworld.gate.claim_handover(G, ALICE, FEB)["period"] == "2026-02"      # claimed in February, never seeded
    free_charge(wd, 20)                                                 # March free usage of the account
    raw = created_raw()
    for _ in range(3):                                                  # the provider retries: busy every time, nothing moves
        with pytest.raises(billing.WebhookBusy):
            deliver_c(wd, raw, NOW, credits=store(wd))
        assert plan(wd) is None and not usage(wd).get("freeze") and not usage(wd).get("carryover_by_period")
        assert usage(wd)["guest_handover_pending"]["period"] == "2026-02"
    assert (usage(wd).get("used_by_period") or {}).get("2026-02") is None        # nothing charged to any month
    assert guest_gate.resolve_handover(G, ALICE, "waive", "ops: reviewed", now=NOW, store=store(wd)) == "waived"
    out = deliver_c(wd, raw, NOW, credits=store(wd))                    # the same event, retried after the resolution
    assert out["status"] == "applied" and carry(wd)["state"] == "decided"
    assert entry_of(wd)["bonus"] == 20                                  # the explicit decision stands: February is not touched
    assert guest_gate.resolve_handover(G, ALICE, "apply_late", "again", now=NOW, store=store(wd)) == "duplicate"
    assert deliver_c(wd, raw, NOW, credits=store(wd)) == {"status": "duplicate"}
    carry_inv_ok(wd)


def test_apply_late_then_activation_proceeds_with_the_original_month_recorded(bworld):
    wd = bworld.wd
    b_guest(bworld, 5, FEB)
    bworld.gate.claim_handover(G, ALICE, FEB)
    free_charge(wd, 10)
    raw = created_raw()
    with pytest.raises(billing.WebhookBusy):
        deliver_c(wd, raw, NOW, credits=store(wd))
    assert guest_gate.resolve_handover(G, ALICE, "apply_late", "ops", now=NOW, store=store(wd)) == "applied_late"
    assert deliver_c(wd, raw, NOW, credits=store(wd))["status"] == "applied"
    assert usage(wd)["used_by_period"]["2026-02"] == 5 and entry_of(wd)["bonus"] == 30     # February recorded; March carry = 40-10
    carry_inv_ok(wd)


def test_a_claimed_but_unseeded_handover_inside_its_month_is_applied_by_the_activation_and_lowers_the_carryover(bworld):
    wd = bworld.wd
    b_guest(bworld, 5, NOW)
    assert bworld.gate.claim_handover(G, ALICE, NOW)["period"] == "2026-03"      # claimed, never seeded (an outage)
    free_charge(wd, 10)
    assert deliver_c(wd, created_raw(), NOW, credits=store(wd))["status"] == "applied"
    assert usage(wd)["guest_handover"]["applied_snapshot"] == 5
    assert entry_of(wd)["bonus"] == 25 and entry_of(wd)["frozen_used"] == 15     # 40 - (10 own + 5 guest)
    carry_inv_ok(wd)


def test_a_handover_check_that_errors_is_not_ready_the_activation_is_retried(bworld, monkeypatch):
    monkeypatch.setattr(guest_gate, "default_gate", lambda: (_ for _ in ()).throw(RuntimeError("gate store down")))
    with pytest.raises(billing.WebhookBusy):
        deliver_c(bworld.wd, created_raw(), NOW, credits=store(bworld.wd))
    assert plan(bworld.wd) is None


def test_without_any_guest_handover_the_activation_is_unchanged(bworld):
    free_charge(bworld.wd, 20)
    assert deliver_c(bworld.wd, created_raw(), NOW, credits=store(bworld.wd))["status"] == "applied"
    assert entry_of(bworld.wd)["bonus"] == 20


# ═════════════════ 4. delivery failure after the handover ═════════════════
def test_a_counted_hold_whose_delivery_failed_keeps_its_count_and_is_redelivered_without_recounting(world, caplog):
    g = world.gg
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    claims(world)
    assert g.on_answer_ready(G, "c", "t", "run", ANSWER) == g.DELIVER           # counted; the account hold committed
    with caplog.at_level(logging.WARNING):
        for _ in range(3):                                              # the save keeps failing: three attempts
            g.on_delivery_failed(G, "run")
            g.on_search_incomplete(G, "run")
    doc = world.coll.find_one({"_id": G})
    assert doc["credits_used"] == 4 and "run" in doc["completed_tokens"]                  # the count is KEPT
    assert len(doc["handover"]["delivery_failures"]) == 3 and acct_used(world) == 4       # observable; charged once
    assert sum("count is KEPT" in r.getMessage() for r in caplog.records) == 3
    # operators can find it: counted token + recorded failures (+ the chat message without claude_answer)
    assert world.coll.count_documents({"handover.delivery_failures": {"$exists": True, "$ne": []}}) == 1
    lease = {}
    assert g.on_work_start(G, "run", "t", "c", lease) == g.PROCEED                        # the durable retry: counted -> proceeds
    assert g.on_answer_ready(G, "c", "t", "run", ANSWER, lease=lease) == g.DELIVER        # 'duplicate' internally: no recount
    g.on_work_end(G, "run", lease)
    assert world.gate.state(G)["credits_used"] == 4 and acct_used(world) == 4
    assert world.gate.complete(G, "run", "c", "t") == "duplicate"
    inv_ok(world)


def test_a_snapshot_token_whose_delivery_failed_after_the_claim_is_kept_and_redelivered(world):
    g = world.gg
    assert world.gate.reserve(G, "t1", "t", "c", cost=4).kind == "allow"
    assert g.on_answer_ready(G, "c", "t", "t1", ANSWER) == g.DELIVER                      # counted, then the claim snapshots it
    assert claims(world)["applied_snapshot"] == 4
    g.on_delivery_failed(G, "t1")
    g.on_search_incomplete(G, "t1")
    assert world.gate.state(G)["credits_used"] == 4 and acct_used(world) == 4
    lease = {}
    assert g.on_work_start(G, "t1", "t", "c", lease) == g.PROCEED
    assert g.on_answer_ready(G, "c", "t", "t1", ANSWER, lease=lease) == g.DELIVER
    assert world.gate.state(G)["credits_used"] == 4 and acct_used(world) == 4
    inv_ok(world)


def test_before_any_handover_a_failed_delivery_still_gives_the_credit_back(world):
    g = world.gg
    assert world.gate.reserve(G, "t1", "t", "c", cost=4).kind == "allow"
    assert g.on_answer_ready(G, "c", "t", "t1", ANSWER) == g.DELIVER
    g.on_delivery_failed(G, "t1")
    g.on_search_incomplete(G, "t1")
    assert world.gate.state(G)["credits_used"] == 0 and "delivery_failures" not in (world.coll.find_one({"_id": G}).get("handover") or {})
    assert world.gate.reserve(G, "t1", "t", "c", cost=4).kind == "allow"                  # the retry takes the slot again
    assert world.gate.complete(G, "t1", "c", "t") == "completed" and world.gate.state(G)["credits_used"] == 4


def test_revert_racing_the_claim_is_decided_by_whichever_write_lands_first(world):
    # (a) the revert wins: the claim re-reads and the credits are not in its snapshot (nothing to move)
    assert world.gate.reserve(G, "t1", "t", "c", cost=4).kind == "allow"
    assert world.gate.complete(G, "t1", "c", "t") == "completed"
    hook = Hook(world.gate.coll)
    hook.before.append((lambda f, u: "handover" in str(u) and "$set" in u, lambda: world.gate.revert(G, "t1")))
    world.gate.coll = hook
    assert claims(world) is None
    assert world.gate.state(G)["credits_used"] == 0 and acct_used(world) == 0
    # (b) the claim wins: the revert is refused, the count and the account charge agree
    other = "guest-race-b"
    assert world.gate.reserve(other, "t2", "t", "c", cost=4).kind == "allow"
    assert world.gate.complete(other, "t2", "c", "t") == "completed"
    hook2 = Hook(world.coll)
    world.gate.coll = hook2
    hook2.before.append((lambda f, u: "$pull" in u, lambda: world.gg.handover_to_account(other, OTHER, now=world.clock.t)))
    assert world.gate.revert(other, "t2") is False
    assert world.gate.state(other)["credits_used"] == 4 and acct_used(world, owner=OTHER) == 4
    assert len(world.coll.find_one({"_id": other})["handover"]["delivery_failures"]) == 1


# ═════════════════ 5. first-paid carryover into the following month; cancellation then reactivation ═════════════════
def test_first_paid_activation_when_the_handover_window_is_the_following_month(bworld):
    wd = bworld.wd
    signup = dt.datetime(2026, 3, 29, 0, 0, 1, tzinfo=UTC)               # fewer than 72h left in March: the window is April
    b_guest(bworld, 7, signup)
    bworld.holder["now"] = signup
    out = guest_gate.handover_to_account(G, ALICE, now=signup)
    assert out["status"] == "applied" and usage(wd)["used_by_period"] == {"2026-04": 7}
    assert store(wd, signup).status(ALICE)["remaining"] == 40            # March is untouched: fully spendable
    at = dt.datetime(2026, 3, 30, 12, 0, tzinfo=UTC)
    bworld.holder["now"] = at
    assert deliver_c(wd, created_raw(at), at, credits=store(wd, at))["status"] == "applied"
    assert carry(wd)["free_period"] == "2026-04"                         # the FOLLOWING month's grant is the carried one
    assert entry_of(wd, plan(wd)["credit_period_key"])["bonus"] == 33    # 40 - 7
    assert store(wd, at).status(ALICE)["limit"] == 433
    assert usage(wd)["freeze"]["2026-04"]["id"] == carry(wd)["claim_id"]
    assert deliver_c(wd, created_raw(at), at, credits=store(wd, at)) == {"status": "duplicate"}
    carry_inv_ok(wd)


def test_cancellation_then_reactivation_never_gets_a_second_bonus(cfg, wd):
    free_charge(wd, 20)
    _out, st, raw = activate(wd)
    first, entry = dict(carry(wd)), dict(entry_of(wd))
    assert st.status(ALICE)["limit"] == 420
    at = NOW + D(days=2)
    cancel = sub_event("subscription_cancelled", status="cancelled", cancelled=True, updated=at, renews=NOW + D(days=30),
                       ends=NOW + D(days=30))
    assert deliver_c(wd, cancel, at, credits=store(wd, at))["status"] == "applied"
    assert store(wd, at).status(ALICE)["limit"] == 420                   # cancelled: paid through the period, same allowance
    assert carry(wd) == first and entry_of(wd) == entry
    expired_at = NOW + D(days=40)
    expire = sub_event("subscription_expired", status="expired", updated=expired_at, ends=expired_at, renews=NOW + D(days=70))
    deliver_c(wd, expire, expired_at, credits=store(wd, expired_at))
    assert store(wd, expired_at).status(ALICE)["limit"] == 40            # back on the free allowance
    later = NOW + D(days=60)
    again = sub_event("subscription_created", sid="9002", updated=later, renews=later + D(days=30), created=later)
    assert deliver_c(wd, again, later, credits=store(wd, later))["status"] == "applied"
    s = store(wd, later).status(ALICE)
    assert s["limit"] == 400 and s["period"] != first["period_key"]      # exactly the grant, a new period, no bonus
    assert carry(wd) == first and entry_of(wd) == entry                  # the one decision is unchanged
    assert deliver_c(wd, again, later, credits=store(wd, later)) == {"status": "duplicate"}
    carry_inv_ok(wd)


def test_cancel_then_resume_inside_the_period_keeps_the_single_bonus(cfg, wd):
    free_charge(wd, 20)
    _out, st, raw = activate(wd)
    marker = dict(carry(wd))
    at = NOW + D(days=2)
    cancel = sub_event("subscription_cancelled", status="cancelled", cancelled=True, updated=at, renews=NOW + D(days=30),
                       ends=NOW + D(days=30))
    deliver_c(wd, cancel, at, credits=store(wd, at))
    resume = sub_event("subscription_resumed", status="active", cancelled=False, updated=at + D(seconds=60),
                       renews=NOW + D(days=30))
    deliver_c(wd, resume, at + D(seconds=60), credits=store(wd, at + D(seconds=60)))
    s = store(wd, at + D(minutes=5)).status(ALICE)
    assert s["limit"] == 420 and carry(wd) == marker and entry_of(wd)["bonus"] == 20
    carry_inv_ok(wd)
