"""
tests/test_activation_resume.py - Phase A: a deferred first paid activation is DERIVED (subscription entitles, plan record does not)
and is resumed through the existing checked protocol (billing.recompute) after a handover is resolved or on a rate-limited owner
status request. No periodic sweep, no queue, HTTP 409 unchanged. Evidence class: mongomock + forced interleavings; not real MongoDB
or Lemon Squeezy.
"""
import threading
from datetime import timedelta

import pytest

mongomock = pytest.importorskip("mongomock")
import billing  # noqa: E402
import guest_gate  # noqa: E402
import index_guard as ig  # noqa: E402
import search_credits as sc  # noqa: E402
from tests.test_billing_hardening import cfg, db, wd  # noqa: F401,E402
from tests.test_credit_carryover import ALICE, NOW, carry, created_raw, deliver_c, entry_of, free_charge, plan, store, sub_event, usage  # noqa: E402
from tests.test_credit_carryover import inv_ok as carry_inv_ok  # noqa: E402
from tests.test_credit_carryover import V_BIZ, sub_change  # noqa: E402
from tests.test_credit_corrections import FEB, G, b_guest, bworld  # noqa: F401,E402
from tests.test_guest_gate import gg  # noqa: F401,E402
from tests.test_billing_hardening import D  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ig.reset()
    billing._RESUME_LAST.clear()
    yield
    ig.reset()
    billing._RESUME_LAST.clear()


def pend(b):
    """A pending first activation: guest handover claimed in an earlier month (unresolved) + the paid event deferred."""
    wd = b.wd
    b_guest(b, 5, FEB)
    assert b.gate.claim_handover(G, ALICE, FEB)["period"] == "2026-02"
    free_charge(wd, 20)
    raw = created_raw()
    with pytest.raises(billing.WebhookBusy):
        deliver_c(wd, raw, NOW, credits=store(wd))
    return raw


def status(wd, now=NOW):
    return billing.status_for_owner(wd.subs, wd.plans, ALICE, now)


def resume(wd, now=NOW, interval=0):
    return billing.resume_activation(wd.subs, wd.plans, ALICE, store(wd, now), now, min_interval=interval)


def test_activation_resumes_by_itself_after_the_handover_is_resolved(bworld, monkeypatch):
    wd = bworld.wd
    monkeypatch.setattr(billing, "_default_collections", lambda: (wd.subs, wd.plans))
    pend(bworld)
    assert plan(wd) is None and wd.events.count_documents({"state": "failed"}) == 1
    assert guest_gate.resolve_handover(G, ALICE, "waive", "ops", now=NOW, store=store(wd)) == "waived"
    assert plan(wd)["plan"] == "standard" and carry(wd)["state"] == "decided"            # no re-send, no manual recompute
    assert entry_of(wd)["bonus"] == 20 and store(wd).status(ALICE)["limit"] == 420
    carry_inv_ok(wd)


def test_repeats_replays_and_status_requests_never_double_grant(bworld, monkeypatch):
    wd = bworld.wd
    monkeypatch.setattr(billing, "_default_collections", lambda: (wd.subs, wd.plans))
    raw = pend(bworld)
    guest_gate.resolve_handover(G, ALICE, "waive", "ops", now=NOW, store=store(wd))
    grant = lambda: (plan(wd)["credit_period_key"], dict(carry(wd)), dict(entry_of(wd)), dict(usage(wd)["used_by_period"]))  # noqa: E731
    snap = grant()
    assert guest_gate.resolve_handover(G, ALICE, "waive", "again", now=NOW, store=store(wd)) == "duplicate"
    for _ in range(3):
        assert resume(wd) == "not_pending"
    # the originally deferred event may be processed once now (its subscription revision moves); the GRANT must not
    assert deliver_c(wd, raw, NOW, credits=store(wd))["status"] in ("duplicate", "stale", "applied")
    assert deliver_c(wd, raw, NOW, credits=store(wd)) == {"status": "duplicate"}
    assert grant() == snap
    key = plan(wd)["credit_period_key"]
    assert len(usage(wd)["carryover_by_period"][key]) == 1 and store(wd).status(ALICE)["limit"] == 420


def test_no_entitlement_and_no_active_status_before_activation(bworld):
    wd = bworld.wd
    pend(bworld)
    s = status(wd)
    assert s["activation"] == "pending" and s["entitled"] is False and s["plan"] == "free" and s["subscription_plan"] == "standard"
    assert store(wd).status(ALICE)["limit"] == 40 and store(wd).plan_for(ALICE) == "free"
    assert resume(wd) == "deferred"                                                       # still unresolved: stays pending
    assert plan(wd) is None or plan(wd).get("plan") is None
    assert status(wd)["entitled"] is False


def test_status_after_activation_is_active(bworld):
    wd = bworld.wd
    pend(bworld)
    guest_gate.resolve_handover(G, ALICE, "waive", "ops", now=NOW, store=store(wd))
    assert resume(wd) == "activated" and resume(wd) == "not_pending"
    s = status(wd)
    assert (s["activation"], s["entitled"], s["plan"]) == ("active", True, "standard")


def test_status_without_any_subscription_or_for_a_plain_paid_flow_is_unchanged(cfg, wd):
    assert status(wd)["activation"] if "activation" in status(wd) else True
    free_charge(wd, 20)
    assert deliver_c(wd, created_raw(), NOW, credits=store(wd))["status"] == "applied"      # normal path: no handover at all
    assert (status(wd)["activation"], status(wd)["entitled"]) == ("active", True)
    assert resume(wd) == "not_pending"


def test_concurrent_resumes_grant_exactly_once(bworld):
    wd = bworld.wd
    pend(bworld)
    guest_gate.resolve_handover(G, ALICE, "waive", "ops", now=NOW, store=store(wd))     # (auto-resume off here: no collections patched)
    assert plan(wd) is None or plan(wd).get("plan") is None
    out, bar = [], threading.Barrier(6)

    def go():
        bar.wait()
        out.append(resume(wd))
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert set(out) <= {"activated", "deferred", "not_pending"} and "activated" in out
    key = plan(wd)["credit_period_key"]
    assert wd.plans.count_documents({"owner_key": ALICE}) == 1 and len(usage(wd)["carryover_by_period"][key]) == 1
    assert entry_of(wd)["bonus"] == 20 and store(wd).status(ALICE)["limit"] == 420
    carry_inv_ok(wd)


def test_resume_while_another_activation_holds_the_claim_defers_without_a_second_grant(bworld):
    wd = bworld.wd
    pend(bworld)
    guest_gate.resolve_handover(G, ALICE, "waive", "ops", now=NOW, store=store(wd))
    sc.claim_activation(wd.plans, ALICE, "f" * 32, NOW)                                    # a live claim of someone else
    assert resume(wd) == "deferred" and not (plan(wd) or {}).get("plan")
    sc.revoke_claim(wd.plans, ALICE, "f" * 32, force=True)
    assert resume(wd) == "activated" and entry_of(wd)["bonus"] == 20


def test_stale_and_replayed_events_after_activation_cannot_reset_the_period_or_reapply_the_carryover(bworld):
    wd = bworld.wd
    raw = pend(bworld)
    guest_gate.resolve_handover(G, ALICE, "waive", "ops", now=NOW, store=store(wd))
    assert resume(wd) == "activated"
    before = (plan(wd)["credit_period_key"], plan(wd)["billing_rev"], dict(carry(wd)), dict(entry_of(wd)))
    older = sub_event("subscription_updated", updated=NOW - D(days=2), renews=NOW + D(days=10))
    for r in (raw, older, raw):
        deliver_c(wd, r, NOW + D(seconds=30), credits=store(wd, NOW + D(seconds=30)))
    after = (plan(wd)["credit_period_key"], plan(wd)["billing_rev"], dict(carry(wd)), dict(entry_of(wd)))
    assert after[0] == before[0] and after[1] >= before[1] and after[2:] == before[2:]
    assert store(wd, NOW + D(seconds=31)).status(ALICE)["limit"] == 420


def test_restart_and_database_error_recovery_is_safe_and_idempotent(bworld):
    wd = bworld.wd
    pend(bworld)
    guest_gate.resolve_handover(G, ALICE, "waive", "ops", now=NOW, store=store(wd))

    class Down:
        def __getattr__(self, name):
            raise sc.CreditStoreError("database down")
    assert billing.resume_activation(wd.subs, wd.plans, ALICE, Down(), NOW, min_interval=0) in ("error", "deferred")
    assert not (plan(wd) or {}).get("plan")                                                 # nothing granted
    billing._RESUME_LAST.clear()                                                            # "restart": only memory is lost
    assert resume(wd) == "activated" and resume(wd) == "not_pending"
    assert entry_of(wd)["bonus"] == 20


def test_rate_limit_bounds_the_work_of_repeated_status_requests(bworld, monkeypatch):
    wd = bworld.wd
    pend(bworld)
    calls = []
    real = billing.recompute
    monkeypatch.setattr(billing, "recompute", lambda *a, **k: calls.append(1) or real(*a, **k))
    res = [billing.resume_activation(wd.subs, wd.plans, ALICE, store(wd), NOW) for _ in range(50)]
    assert len(calls) == 1 and res.count("rate_limited") == 49


def test_a_period_that_expires_before_activation_is_not_extended_refunded_or_entitled(bworld):
    wd = bworld.wd
    pend(bworld)
    later = NOW + D(days=90)
    writes = []
    spy = type("S", (), {"__getattr__": lambda self, n: getattr(wd.plans, n)})()
    assert status(wd, later)["activation"] == "expired_unactivated" and status(wd, later)["entitled"] is False
    assert billing.resume_activation(wd.subs, wd.plans, ALICE, store(wd, later), later, min_interval=0) == "expired_unactivated"
    assert not (plan(wd) or {}).get("plan") and store(wd, later).status(ALICE)["limit"] == 40
    assert wd.subs.find_one({"owner_key": ALICE})["entitled_until"].replace(tzinfo=None) < later.replace(tzinfo=None)   # untouched


def test_missing_index_blocks_resume_and_repair_allows_it_idempotently(bworld, monkeypatch):
    wd = bworld.wd
    pend(bworld)
    guest_gate.resolve_handover(G, ALICE, "waive", "ops", now=NOW, store=store(wd))
    raw = mongomock.MongoClient()["g"]["p"]
    ig.register("flintel_plans", raw)
    assert resume(wd) in ("error", "deferred") and not (plan(wd) or {}).get("plan") and not usage(wd).get("carryover_by_period")
    raw.create_index("owner_key", unique=True)
    monkeypatch.setattr(ig, "_now", lambda: 10 ** 9)
    billing._RESUME_LAST.clear()
    assert resume(wd) == "activated" and resume(wd) == "not_pending"
    assert entry_of(wd)["bonus"] == 20


def test_unverifiable_usage_index_never_grants_credits(bworld):
    wd = bworld.wd
    pend(bworld)
    guest_gate.resolve_handover(G, ALICE, "waive", "ops", now=NOW, store=store(wd))

    class Down:
        def index_information(self):
            raise RuntimeError("down")
    ig.register("flintel_credit_usage", Down())
    assert resume(wd) in ("error", "deferred") and not (plan(wd) or {}).get("plan")      # fail closed: never a grant
    assert not usage(wd).get("carryover_by_period")


def test_normal_authenticated_spending_and_free_owner_resume_are_untouched(cfg, wd):
    st = store(wd)
    assert isinstance(st.reserve(ALICE, "email", "r1", "t", "c", True, cost=4), sc.Ticket)
    assert st.status(ALICE)["used"] == 4
    assert resume(wd) == "not_pending" and wd.plans.count_documents({}) == 0


def test_index_guard_rejects_hashed_and_malformed_specs():
    class C:
        def __init__(self, info):
            self.info = info

        def index_information(self):
            return self.info

        def aggregate(self, *a, **k):
            return []
    assert ig.verify_unique_index(C({"x": {"key": [("owner_key", "hashed")], "unique": True}}))[0] != "ok"
    assert ig.verify_unique_index(C({"x": {"key": 5, "unique": True}}))[0] == "unverifiable"
    assert ig.verify_unique_index(C({"x": {"key": [("owner_key", 1)], "unique": "yes"}}))[0] != "ok"
    assert ig.verify_unique_index(C({"x": {"key": [("owner_key", -1)], "unique": True}}))[0] == "ok"


class _FailPlanWrites:
    """A plans collection whose WRITES fail while reads work: a plan-store outage between the subscription write and the plan write."""

    def __init__(self, coll):
        self._c = coll

    def __getattr__(self, name):
        return getattr(self._c, name)

    def update_one(self, *a, **k):
        raise RuntimeError("plan store down")

    def insert_one(self, *a, **k):
        raise RuntimeError("plan store down")


def test_pending_upgrade_status_reports_the_plan_in_force_and_changes_nothing(cfg, wd):
    """An active Standard plan record + a live Business subscription whose plan write failed (the provider will retry): the status
    shows what enforcement applies (Standard, entitled) with the pending upgrade kept separate; enforcement and records are untouched."""
    assert deliver_c(wd, created_raw(), NOW)["status"] == "applied"                       # plain activation (no carryover): Standard 400
    at = NOW + D(days=2)
    with pytest.raises(sc.CreditStoreError):                                              # upgrade event: subscription written, plan write fails
        deliver_c(wd, sub_change(V_BIZ, "business", at), at, plans=_FailPlanWrites(wd.plans))
    st = store(wd, at)
    assert plan(wd)["plan"] == "standard" and st.plan_for(ALICE, at) == "standard" and st.limit_for(ALICE, at) == 400
    assert wd.subs.find_one({"owner_key": ALICE})["plan"] == "business"                  # the subscription is ahead of the plan record
    before = (plan(wd), usage(wd), list(wd.subs.find({})))
    s = status(wd, at)
    assert (s["plan"], s["entitled"], s["activation"], s["subscription_plan"]) == ("standard", True, "pending", "business")
    assert st.limit_for(ALICE, at) == 400                                                 # enforcement is unchanged
    assert (plan(wd), usage(wd), list(wd.subs.find({}))) == before                        # the status read wrote nothing
