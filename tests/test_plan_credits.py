"""
tests/test_plan_credits.py — cost-aware reservations, paid-plan monthly limits
and activate_plan() (search_credits.py, database.py).

EVERY test here is "unit (fake DB), not live": mongomock collections, a fake
clock and stub callbacks. No real MongoDB, no network, no LLM. mongomock does
not reproduce MongoDB's replication, write concern or failover, and the
concurrency tests run in one process. They show that the CODE issues one
conditional single-document update and that the logic holds when those updates
are atomic (the guarantee MongoDB gives for one document); they are not proof
about a live cluster. The concurrency tests wrap the collection in a lock that
makes each operation atomic, exactly the guarantee being relied on.
"""
import ast
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

mongomock = pytest.importorskip("mongomock")

import search_credits as sc  # noqa: E402
from tests.test_search_credit_routes import (  # noqa: E402,F401  (fixtures + helpers)
    KEY1, SEARCH_ROUTER, app, body, env)

ROOT = Path(__file__).resolve().parent.parent
OWNER = "user@example.com"
GUEST = "5b0f6f0e-9d1c-4c0e-9c55-0a7d9f1c2e11"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
PERIOD = "2026-10"
ACK = SimpleNamespace(acknowledged=True, matched_count=1, upserted_id=None)


class Clock:
    def __init__(self, start=NOW):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


class Atomic:
    """Each collection operation runs under one lock = MongoDB's single-document atomicity."""

    def __init__(self, inner):
        self._inner, self._lock = inner, threading.RLock()

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def locked(*a, **k):
            with self._lock:
                return attr(*a, **k)
        return locked


class World:
    def __init__(self, with_plans=True):
        self.db = mongomock.MongoClient()["plan_credits"]
        self.db.flintel_credit_usage.create_index("owner_key", unique=True)
        self.db.flintel_plans.create_index("owner_key", unique=True)
        self.clock = Clock()
        self.coll = Atomic(self.db.flintel_credit_usage)
        self.plans = Atomic(self.db.flintel_plans)
        self.saved = []
        self.limit = 40
        self.with_plans = with_plans

    def save(self, chat_id, owner_key, credit_request_id=None, **payload):
        self.saved.append(credit_request_id)
        return True

    def store(self, **kw):
        return sc.CreditStore(self.coll, self.db.chats, self.db.jobs, self.save, clock=self.clock,
                              enabled=lambda: True, limit=lambda: self.limit,
                              plans=self.plans if self.with_plans else None, **kw)

    def doc(self, owner=OWNER):
        return self.db.flintel_credit_usage.find_one({"owner_key": owner}) or {}

    def used(self, owner=OWNER, period=PERIOD):
        return int((self.doc(owner).get("used_by_period") or {}).get(period, 0))

    def plan(self, plan="standard", days=30, owner=OWNER, ref=None):
        return sc.activate_plan(self.plans, owner, plan, self.clock() + timedelta(days=days),
                                ref or f"txn_{plan}_{days}", now=self.clock())

    def ok(self, owner=OWNER):
        assert sc.check_invariant(self.doc(owner)) == [], self.doc(owner)


@pytest.fixture
def w():
    return World()


def reserve(store, rid, cost, owner=OWNER, owner_type="email", **kw):
    return store.reserve(owner, owner_type, rid, "topic", "chat1", True, cost=cost, **kw)


def run_search(store, rid, cost, owner=OWNER):
    t = reserve(store, rid, cost, owner)
    assert isinstance(t, sc.Ticket), t
    d = store.run(t, lambda r: ACK, {"query": "q"})
    assert d.kind == "done"
    return t


# ── A. cost-aware accounting ─────────────────────────────────────────────────
def test_reserve_holds_the_cost_and_stores_effort_and_reason(w):
    t = reserve(w.store(), "r1", 11, effort="deep", cost_reason="broad research")
    e = w.doc()["res"]["r1"]
    assert (t.cost, w.used()) == (11, 11)
    assert (e["cost"], e["effort"], e["cost_reason"], e["state"]) == (11, "deep", "broad research", sc.RESERVED)
    assert "query" not in json.dumps(e, default=str)
    assert w.doc()["limit_max_by_period"][PERIOD] == 40
    w.ok()


@pytest.mark.parametrize("bad", [0, -1, True, False, 4.0, "4", None, [4]])
def test_reserve_rejects_a_non_integer_or_non_positive_cost_without_writing(w, bad):
    with pytest.raises(ValueError):
        reserve(w.store(), "r1", bad)
    assert w.doc() == {}


def test_default_cost_is_one_credit(w):
    t = w.store().reserve(OWNER, "email", "r1", "topic", "chat1", True)
    assert t.cost == 1 and w.used() == 1 and w.doc()["res"]["r1"]["cost"] == 1


def test_invariant_holds_after_every_transition_of_a_costed_search(w):
    store = w.store()
    t = reserve(store, "r1", 9)
    w.ok()
    assert store.start(t) == sc.DONE
    w.ok()
    assert store.claim(t) == sc.DONE
    w.ok()
    assert store.mark_enqueued(t) == sc.DONE
    w.ok()
    assert store.commit(t) == sc.DONE
    w.ok()
    assert w.used() == 9 and w.doc()["res"]["r1"]["state"] == sc.COMMITTED


def test_full_run_commits_with_the_stored_cost(w):
    run_search(w.store(), "r1", 7)
    assert w.used() == 7 and w.saved == ["r1"] and w.doc()["res"]["r1"]["cost"] == 7
    w.ok()


def test_self_refund_returns_exactly_the_entry_cost(w):
    store = w.store()
    reserve(store, "keep", 5)
    t = reserve(store, "r1", 11)
    assert w.used() == 16
    assert store.self_refund(t) is True
    assert w.used() == 5 and w.doc()["res"]["r1"]["state"] == sc.REFUNDED
    w.ok()


def test_stale_reservation_recovery_refunds_exactly_its_cost(w):
    store = w.store()
    reserve(store, "gone", 13)
    w.clock.advance(seconds=901)                    # "gone" is now abandoned; "keep" is fresh
    reserve(store, "keep", 4)
    assert w.used() == 17
    store.recover(OWNER)
    assert w.used() == 4 and w.doc()["res"]["gone"]["state"] == sc.REFUNDED
    w.ok()


def test_a_sent_search_is_never_refunded_automatically(w):
    store = w.store()
    t = reserve(store, "r1", 6)
    store.start(t)
    assert store.claim(t) == sc.DONE
    assert store.self_refund(t) is False
    w.clock.advance(seconds=5000)
    store.recover(OWNER)
    assert w.used() == 6 and w.doc()["res"]["r1"]["state"] != sc.REFUNDED
    w.ok()


def test_prune_archives_exactly_the_committed_cost(w):
    store = w.store()
    run_search(store, "r1", 9)
    reserve(store, "r2", 3)
    w.clock.advance(days=70)                        # two months later: October entries are prunable
    store.recover(OWNER)
    doc = w.doc()
    assert doc["archived_by_period"][PERIOD] == 9 and "r1" not in doc["res"]   # committed -> archived by its cost
    assert doc["used_by_period"][PERIOD] == 9 and "r2" not in doc["res"]       # r2 (3) was refunded as stale, then dropped
    w.ok()


def test_legacy_entries_without_a_cost_hold_one_credit(w):
    w.db.flintel_credit_usage.insert_one({
        "owner_key": OWNER, "owner_type": "email", "used_by_period": {PERIOD: 2}, "limit_max_by_period": {PERIOD: 40},
        "archived_by_period": {},
        "res": {"old1": {"state": sc.COMMITTED, "reason": "normal", "period": PERIOD, "attempt": "a", "ev": 3},
                "old2": {"state": sc.RESERVED, "reason": None, "period": PERIOD, "attempt": "b", "ev": 1,
                         "at": w.clock(), "started_at": None, "sent_once": False}}})
    w.ok()
    store = w.store()
    assert sc._entry_cost(w.doc()["res"]["old1"]) == 1
    t = reserve(store, "new", 4)
    assert w.used() == 6
    w.ok()
    w.clock.advance(seconds=901)
    store.recover(OWNER)                            # old2 (reserved, stale) refunds exactly 1, new refunds 4
    assert w.used() == 1
    w.ok()
    assert t.cost == 4


def test_a_corrupted_stored_cost_is_treated_as_one_never_negative(w):
    for bad in (0, -5, True, 2.5, "9", None):
        assert sc._entry_cost({"cost": bad}) == 1


def test_retry_resume_and_reconciliation_keep_the_stored_cost(w):
    store = w.store()
    t = reserve(store, "r1", 5, effort="complex", cost_reason="x")
    store.start(t)
    w.clock.advance(seconds=901)                    # abandoned, never sent: resumable
    again = reserve(store, "r1", 9, effort="deep")  # the retry's own proposal must be ignored
    assert isinstance(again, sc.Ticket) and again.cost == 5 and w.used() == 5
    e = w.doc()["res"]["r1"]
    assert (e["cost"], e["effort"], e["cost_reason"]) == (5, "complex", "x")
    w.ok()


def test_duplicate_submit_in_flight_is_not_charged_again(w):
    store = w.store()
    t = reserve(store, "r1", 8)
    store.start(t)
    store.claim(t)
    dup = reserve(store, "r1", 8)
    assert isinstance(dup, sc.Decision) and dup.kind == "busy" and w.used() == 8
    w.ok()


def test_reservation_after_a_refund_reuses_the_cost_decided_for_that_request_id(w):
    store = w.store()
    t = reserve(store, "r1", 6, effort="complex")
    assert store.self_refund(t) is True and w.used() == 0
    again = reserve(store, "r1", 13, effort="deep")
    assert isinstance(again, sc.Ticket) and again.cost == 6 and w.used() == 6
    assert w.doc()["res"]["r1"]["effort"] == "complex"
    w.ok()


def test_refunded_marker_with_a_legacy_entry_takes_the_new_cost(w):
    w.db.flintel_credit_usage.insert_one({
        "owner_key": OWNER, "owner_type": "email", "used_by_period": {PERIOD: 0}, "limit_max_by_period": {PERIOD: 40},
        "archived_by_period": {},
        "res": {"r1": {"state": sc.REFUNDED, "reason": None, "period": PERIOD, "attempt": "a", "ev": 4}}})
    t = reserve(w.store(), "r1", 7)
    assert isinstance(t, sc.Ticket) and t.cost == 7 and w.used() == 7
    w.ok()


# ── A. atomic conditional reservation (denial and concurrency) ───────────────
def test_cost_above_the_remaining_credits_is_denied_without_any_write(w):
    store = w.store()
    reserve(store, "a", 14)
    reserve(store, "b", 14)
    reserve(store, "c", 9)                         # used 37, remaining 3
    before = json.dumps(w.doc(), default=str, sort_keys=True)
    d = reserve(store, "d", 4)
    assert isinstance(d, sc.Decision) and d.kind == "exhausted"
    assert json.dumps(w.doc(), default=str, sort_keys=True) == before        # nothing changed at all
    ok = reserve(store, "e", 3)                    # exactly the remaining credits is accepted
    assert isinstance(ok, sc.Ticket) and w.used() == 40
    assert reserve(store, "f", 3).kind == "exhausted" and w.used() == 40
    w.ok()


def test_cost_above_the_whole_limit_is_denied_even_for_a_new_owner_and_creates_nothing(w):
    w.limit = 10
    d = reserve(w.store(), "r1", 11)
    assert d.kind == "exhausted" and w.doc() == {}
    w.limit = 3
    assert reserve(w.store(), "r1", 4).kind == "exhausted" and w.doc() == {}
    assert reserve(w.store(), "r1", 3).cost == 3


def test_free_allowance_below_the_max_cost_is_reported_not_clamped(w):
    w.limit = 8
    store = w.store()
    assert reserve(store, "deep", 11).kind == "exhausted"                      # 11 > 8: denied, never clamped to 8
    t = reserve(store, "std", 5)
    assert t.cost == 5 and w.used() == 5                                       # not rewritten to something else
    assert reserve(store, "deep2", 8).kind == "exhausted"                      # 5 + 8 > 8
    assert w.used() == 5
    w.ok()


def test_exhausted_decision_carries_the_unchanged_message_inputs(w):
    w.limit = 4
    d = reserve(w.store(), "r1", 5)
    assert d.kind == "exhausted" and d.resets_at == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert sc.EXHAUSTED_MESSAGE.format(reset="X").startswith("You're out of free searches until X.")


def test_ten_parallel_reservations_costing_4_with_5_credits_remaining_allow_exactly_one(w):
    store = w.store()
    reserve(store, "seed", 35)                      # remaining 5 of 40
    results, barrier = [], threading.Barrier(10)

    def worker(i):
        barrier.wait()
        results.append(reserve(w.store(), f"p{i}", 4))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    tickets = [r for r in results if isinstance(r, sc.Ticket)]
    denied = [r for r in results if isinstance(r, sc.Decision)]
    assert len(tickets) == 1 and len(denied) == 9 and all(d.kind == "exhausted" for d in denied)
    assert w.used() == 39 and len(w.doc()["res"]) == 2
    w.ok()


def test_parallel_reservations_for_a_new_owner_never_overdraw(w):
    w.limit = 20
    results, barrier = [], threading.Barrier(12)

    def worker(i):
        barrier.wait()
        results.append(reserve(w.store(), f"p{i}", 7))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    tickets = [r for r in results if isinstance(r, sc.Ticket)]
    assert len(tickets) == 2 and w.used() == 14                                # floor(20 / 7)
    assert all(isinstance(r, sc.Decision) for r in results if r not in tickets)
    w.ok()


def test_reservation_is_one_conditional_update_with_the_cost_in_both_filter_and_increment(w):
    calls = []

    class Spy(Atomic):
        def update_one(self, filt, update, *a, **k):
            calls.append((filt, update))
            return self._inner.update_one(filt, update, *a, **k)
    w.coll = Spy(w.db.flintel_credit_usage)
    store = w.store()
    reserve(store, "seed", 1)
    calls.clear()
    reserve(store, "r1", 9)
    assert len(calls) == 1
    filt, update = calls[0]
    assert {f"used_by_period.{PERIOD}": {"$lte": 31}} in filt["$or"]
    assert update["$inc"] == {f"used_by_period.{PERIOD}": 9} and update["$set"]["res.r1"]["cost"] == 9


# ── C. plans ─────────────────────────────────────────────────────────────────
def test_everyone_is_free_without_a_plan_record(w):
    store = w.store()
    assert store.limit_for(OWNER, w.clock()) == 40 and store.plan_for(OWNER) == "free"
    assert store.limit_for(GUEST, w.clock(), "anon") == 40


def test_active_plan_replaces_the_free_allowance(w):
    store = w.store()
    w.plan("standard")
    assert store.limit_for(OWNER, w.clock(), "email") == 400 and store.plan_for(OWNER) == "standard"
    w.plan("business", ref="txn_b")
    assert store.limit_for(OWNER, w.clock(), "email") == 1500
    assert store.limit_for("someone@else.com", w.clock(), "email") == 40            # other owners unaffected


def test_plan_applies_only_inside_its_active_window(w):
    store = w.store()
    w.plan("standard", days=10)
    assert store.limit_for(OWNER) == 400
    w.clock.advance(days=10)                                                         # active_until is exclusive
    assert store.limit_for(OWNER) == 40
    sc.activate_plan(w.plans, OWNER, "business", w.clock() + timedelta(days=60), "future", now=w.clock(),
                     active_from=w.clock() + timedelta(days=30))
    assert store.limit_for(OWNER) == 40                                              # not active yet
    w.clock.advance(days=31)
    assert store.limit_for(OWNER) == 1500


def test_a_guest_never_gets_a_plan_even_if_a_record_is_planted(w):
    w.db.flintel_plans.insert_one({"owner_key": GUEST, "plan": "business", "active_from": w.clock() - timedelta(days=1),
                                   "active_until": w.clock() + timedelta(days=30), "source_ref": "x"})
    reads = []

    class Spy(Atomic):
        def find_one(self, *a, **k):
            reads.append(a)
            return self._inner.find_one(*a, **k)
    w.plans = Spy(w.db.flintel_plans)
    store = w.store()
    assert store.limit_for(GUEST, w.clock(), "anon") == 40 and store.limit_for(GUEST, w.clock()) == 40
    assert store.limit_for(OWNER, w.clock(), "anon") == 40                           # owner_type "anon" is never paid
    assert reads == []                                                               # not even looked up


@pytest.mark.parametrize("record", [
    {"plan": "enterprise"}, {"plan": "free"}, {"plan": None}, {"plan": "standard"},
    {"plan": "standard", "active_from": "2026-10-01", "active_until": "2026-12-01"},
    {"plan": "standard", "active_until": NOW + timedelta(days=5)},
    {"plan": "standard", "active_from": NOW - timedelta(days=5)},
])
def test_a_malformed_plan_record_is_not_an_active_plan(w, record):
    w.db.flintel_plans.insert_one({"owner_key": OWNER, **record})
    assert w.store().limit_for(OWNER, w.clock(), "email") == 40


def test_the_plan_limit_is_not_added_to_the_free_allowance(w):
    w.plan("standard")
    store = w.store()
    reserve(store, "a", 14)
    assert w.doc()["limit_max_by_period"][PERIOD] == 400
    for i in range(27):
        assert isinstance(reserve(store, f"b{i}", 14), sc.Ticket)
    assert w.used() == 28 * 14 == 392
    assert reserve(store, "over", 14).kind == "exhausted" and w.used() == 392      # 406 > 400: the plan limit, not 440
    assert isinstance(reserve(store, "fits", 8), sc.Ticket) and w.used() == 400
    w.ok()


def test_upgrade_in_the_middle_of_the_month_keeps_usage_and_raises_the_limit(w):
    store = w.store()
    for i in range(2):
        reserve(store, f"f{i}", 14)                                                  # 28 of 40
    assert reserve(store, "blocked", 14).kind == "exhausted"
    w.plan("standard")
    t = reserve(store, "after", 14)
    assert isinstance(t, sc.Ticket) and w.used() == 42                               # usage kept, limit now 400
    assert w.doc()["limit_max_by_period"][PERIOD] == 400
    w.ok()


def test_expiry_with_usage_above_the_free_limit_denies_until_next_month_without_rewriting_counters(w):
    w.plan("standard", days=10)
    store = w.store()
    for i in range(5):
        reserve(store, f"p{i}", 14)                                                  # 70 used, over the free 40
    snapshot = json.dumps(w.doc(), default=str, sort_keys=True)
    w.clock.advance(days=11)
    d = reserve(store, "late", 3)
    assert d.kind == "exhausted"
    assert json.dumps(w.doc(), default=str, sort_keys=True) == snapshot              # no rewrite, never negative
    assert sc.check_invariant(w.doc()) == []                                         # C <= limit_max (the plan's 400)
    w.clock.advance(days=20)                                                         # November: fresh free month
    t = reserve(store, "nov", 14)
    assert isinstance(t, sc.Ticket) and w.used(period="2026-11") == 14
    assert w.used() == 70


def test_expiry_with_usage_under_the_free_limit_keeps_working_on_the_free_allowance(w):
    w.plan("standard", days=5)
    store = w.store()
    reserve(store, "a", 14)
    w.clock.advance(days=6)
    assert isinstance(reserve(store, "b", 14), sc.Ticket) and w.used() == 28
    assert isinstance(reserve(store, "c", 12), sc.Ticket) and reserve(store, "d", 1).kind == "exhausted"
    w.ok()


def test_plan_lookup_failure_fails_closed_and_writes_nothing(w):
    class Down(Atomic):
        def find_one(self, *a, **k):
            raise RuntimeError("plans store down")
    w.plans = Down(w.db.flintel_plans)
    store = w.store()
    with pytest.raises(sc.CreditStoreError):
        reserve(store, "r1", 4)
    assert w.doc() == {}
    with pytest.raises(sc.CreditStoreError):
        store.limit_for(OWNER, w.clock(), "email")
    assert store.precheck_exhausted(OWNER, "r1", cost=4, owner_type="email") is None      # optimisation never blocks
    assert store.status(OWNER, "email")["status"] == "unavailable"
    assert reserve(store, "g1", 4, owner=GUEST, owner_type="anon").cost == 4              # guests never read plans


def test_plan_lookup_failure_does_not_break_an_in_flight_duplicate_answer(w):
    store = w.store()
    t = reserve(store, "r1", 4)
    store.start(t)
    store.claim(t)

    class Down(Atomic):
        def find_one(self, *a, **k):
            raise RuntimeError("down")
    w.plans = Down(w.db.flintel_plans)
    assert w.store().lookup(OWNER, "r1").kind == "busy"


def test_status_reports_the_plan_and_its_limit(w):
    store = w.store()
    assert (store.status(OWNER, "email")["limit"], store.status(OWNER, "email")["plan"]) == (40, "free")
    w.plan("business")
    s = store.status(OWNER, "email")
    assert (s["limit"], s["plan"], s["remaining"]) == (1500, "business", 1500)


def test_precheck_uses_the_cost_and_the_plan_limit(w):
    store = w.store()
    reserve(store, "a", 14)
    reserve(store, "b", 14)
    assert store.precheck_exhausted(OWNER, "new", cost=14, owner_type="email").kind == "exhausted"
    assert store.precheck_exhausted(OWNER, "new", cost=12, owner_type="email") is None
    assert store.precheck_exhausted(OWNER, "a", cost=14, owner_type="email") is None      # existing entry: left to reserve
    w.plan("standard")
    assert store.precheck_exhausted(OWNER, "new", cost=14, owner_type="email") is None


# ── C. activate_plan (idempotent, not wired to any route) ────────────────────
def test_activate_plan_creates_the_record(w):
    until = w.clock() + timedelta(days=30)
    assert sc.activate_plan(w.plans, OWNER, "standard", until, "txn_1", now=w.clock()) == "activated"
    rec = w.db.flintel_plans.find_one({"owner_key": OWNER})
    assert rec["plan"] == "standard" and rec["source_ref"] == "txn_1" and rec["applied_refs"] == ["txn_1"]
    assert sc._aware(rec["active_from"]) == w.clock() and sc._aware(rec["active_until"]) == until
    assert sc._aware(rec["updated_at"]) == w.clock()
    assert w.db.flintel_plans.count_documents({}) == 1


def test_same_source_ref_twice_neither_extends_nor_activates_twice(w):
    first = w.clock() + timedelta(days=30)
    assert sc.activate_plan(w.plans, OWNER, "standard", first, "txn_1", now=w.clock()) == "activated"
    w.clock.advance(days=3)
    again = sc.activate_plan(w.plans, OWNER, "business", w.clock() + timedelta(days=400), "txn_1", now=w.clock())
    assert again == "duplicate"
    rec = w.db.flintel_plans.find_one({"owner_key": OWNER})
    assert rec["plan"] == "standard" and sc._aware(rec["active_until"]) == first and rec["applied_refs"] == ["txn_1"]
    assert w.db.flintel_plans.count_documents({}) == 1


def test_a_new_source_ref_is_a_renewal_or_upgrade_and_an_old_one_is_never_replayed(w):
    t1 = w.clock() + timedelta(days=30)
    sc.activate_plan(w.plans, OWNER, "standard", t1, "txn_1", now=w.clock())
    t2 = w.clock() + timedelta(days=60)
    assert sc.activate_plan(w.plans, OWNER, "business", t2, "txn_2", now=w.clock()) == "activated"
    rec = w.db.flintel_plans.find_one({"owner_key": OWNER})
    assert rec["plan"] == "business" and sc._aware(rec["active_until"]) == t2 and rec["source_ref"] == "txn_2"
    assert sc.activate_plan(w.plans, OWNER, "standard", t1, "txn_1", now=w.clock()) == "duplicate"   # out-of-order replay
    rec = w.db.flintel_plans.find_one({"owner_key": OWNER})
    assert rec["plan"] == "business" and sc._aware(rec["active_until"]) == t2
    assert sorted(rec["applied_refs"]) == ["txn_1", "txn_2"]


def test_concurrent_duplicate_activations_apply_exactly_once(w):
    results, barrier = [], threading.Barrier(10)

    def worker():
        barrier.wait()
        results.append(sc.activate_plan(w.plans, OWNER, "standard", w.clock() + timedelta(days=30), "txn_same",
                                        now=w.clock()))
    threads = [threading.Thread(target=worker) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["activated"] + ["duplicate"] * 9
    assert w.db.flintel_plans.count_documents({"owner_key": OWNER}) == 1
    assert w.db.flintel_plans.find_one({"owner_key": OWNER})["applied_refs"] == ["txn_same"]


def test_concurrent_activations_with_different_refs_keep_one_consistent_record(w):
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        sc.activate_plan(w.plans, OWNER, "standard" if i % 2 else "business", w.clock() + timedelta(days=30 + i),
                         f"txn_{i}", now=w.clock())
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert w.db.flintel_plans.count_documents({"owner_key": OWNER}) == 1
    rec = w.db.flintel_plans.find_one({"owner_key": OWNER})
    assert sorted(rec["applied_refs"]) == sorted(f"txn_{i}" for i in range(8))
    assert rec["source_ref"] in rec["applied_refs"] and rec["plan"] in ("standard", "business")


@pytest.mark.parametrize("args", [
    (GUEST, "standard", 30, "t"), ("", "standard", 30, "t"), (None, "standard", 30, "t"),       # not a registered email
    (OWNER, "free", 30, "t"), (OWNER, "enterprise", 30, "t"), (OWNER, None, 30, "t"),           # not a paid plan
    (OWNER, "standard", 30, ""), (OWNER, "standard", 30, "  "), (OWNER, "standard", 30, None),   # no idempotency key
    (OWNER, "standard", -1, "t"), (OWNER, "standard", 0, "t"),                                   # already over
])
def test_activate_plan_rejects_bad_arguments_without_writing(w, args):
    owner, plan, days, ref = args
    with pytest.raises(ValueError):
        sc.activate_plan(w.plans, owner, plan, w.clock() + timedelta(days=days), ref, now=w.clock())
    assert w.db.flintel_plans.count_documents({}) == 0


def test_activate_plan_requires_a_datetime_end(w):
    with pytest.raises(ValueError):
        sc.activate_plan(w.plans, OWNER, "standard", "2026-12-01", "t", now=w.clock())


def test_activate_plan_store_failure_raises_so_the_caller_does_not_acknowledge(w):
    class Down:
        def update_one(self, *a, **k):
            raise RuntimeError("down")
    with pytest.raises(sc.CreditStoreError):
        sc.activate_plan(Down(), OWNER, "standard", w.clock() + timedelta(days=1), "t", now=w.clock())

    class Unacked:
        def update_one(self, *a, **k):
            return SimpleNamespace(acknowledged=False)
    with pytest.raises(sc.CreditStoreError):
        sc.activate_plan(Unacked(), OWNER, "standard", w.clock() + timedelta(days=1), "t", now=w.clock())


def test_activate_plan_is_not_wired_to_any_route_or_module():
    for name in ("routes.py", "index.py", "logics.py", "google.py", "scheduler.py", "database.py", "credit_cost.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))      # code only: comments may mention it
        used = [n for n in ast.walk(tree) if (isinstance(n, ast.Name) and n.id == "activate_plan")
                or (isinstance(n, ast.Attribute) and n.attr == "activate_plan")
                or (isinstance(n, ast.alias) and n.name == "activate_plan")]
        assert not used, name
    assert "activate_plan" in (ROOT / "search_credits.py").read_text(encoding="utf-8")


# ── route level: plan limit through the real POST /search ───────────────────
def test_search_route_uses_the_plan_limit_for_a_registered_owner(app, monkeypatch):
    rt, db, state = app["rt"], app["db"], app["state"]
    db.flintel_plans.create_index("owner_key", unique=True)
    monkeypatch.setattr(rt, "get_owner", lambda request: (OWNER, "email"))
    store = lambda: sc.CreditStore(db.flintel_credit_usage, app["idx"].chats_collection, db.jobs,  # noqa: E731
                                   app["idx"].add_search_to_chat, enabled=lambda: True, limit=lambda: 40,
                                   plans=db.flintel_plans)
    monkeypatch.setattr(rt, "_credit_store", store)
    state["router"] = {**SEARCH_ROUTER, "effort": "deep", "cost": 14}
    for i in range(3):
        assert app["send"]("Find people looking for a CRM for small teams", submit_key=f"{i}1111111-aaaa-4bbb-8ccc-000000000001"
                           ).status_code in (303, 429)
    free_used = db.flintel_credit_usage.find_one({"owner_key": OWNER})["used_by_period"]
    assert sum(free_used.values()) == 28                                    # two accepted, the third needs 42 > 40
    sc.activate_plan(db.flintel_plans, OWNER, "standard", datetime.now(timezone.utc) + timedelta(days=30), "txn_route")
    resp = app["send"]("Find people looking for a CRM for small teams", submit_key="9a111111-aaaa-4bbb-8ccc-000000000009")
    assert resp.status_code == 303
    assert sum(db.flintel_credit_usage.find_one({"owner_key": OWNER})["used_by_period"].values()) == 42


def test_search_route_answers_503_when_the_plan_store_fails(app, monkeypatch):
    rt, db = app["rt"], app["db"]

    class Down:
        def find_one(self, *a, **k):
            raise RuntimeError("plans down")
    monkeypatch.setattr(rt, "get_owner", lambda request: (OWNER, "email"))
    monkeypatch.setattr(rt, "_credit_store", lambda: sc.CreditStore(
        db.flintel_credit_usage, app["idx"].chats_collection, db.jobs, app["idx"].add_search_to_chat,
        enabled=lambda: True, limit=lambda: 40, plans=Down()))
    resp = app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert resp.status_code == 503 and resp.headers["X-Flintel-Denial"] == "credit-store-unavailable"
    assert sc.UNAVAILABLE_MESSAGE in body(resp)
    assert app["state"]["enqueues"] == [] and db.flintel_credit_usage.count_documents({}) == 0


# ── database ownership (no real server) ──────────────────────────────────────
DB_SCRIPT = r"""
import json
import mongomock, pymongo
pymongo.MongoClient = mongomock.MongoClient
import database
names = database.db3.list_collection_names()
coll = database.plans_collection
info = database.db3["flintel_plans"].index_information() if "flintel_plans" in names else {}
print(json.dumps({"collections": sorted(names), "plans_indexes": {k: bool(v.get("unique")) for k, v in info.items()},
                  "write_concern": coll.write_concern.document, "db_name": coll.database.name,
                  "db3_name": database.db3.name, "same_client": coll.database.client is database.client_3,
                  "credit_wc": database.credit_usage_collection.write_concern.document}))
"""


def _import_database(kill_switch):
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MONGODB", "CREDIT_"))}
    env.update({"SESSION_SECRET_KEY": "test-only-session-key-not-for-deployment",
                "MONGODB_URI": "mongodb://primary.invalid:27017", "MONGODB3": "mongodb://third.invalid:27017",
                "MONGODB2": "", "MONGODB4": "", "PYTHONPATH": str(ROOT)})
    if kill_switch is not None:
        env["CREDIT_ENFORCEMENT_DISABLED"] = kill_switch
    out = subprocess.run([sys.executable, "-c", DB_SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True,
                         timeout=60, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_plans_collection_is_on_db3_with_majority_write_concern_and_a_unique_index():
    state = _import_database(None)
    assert state["write_concern"] == {"w": "majority"} == state["credit_wc"]
    assert state["db_name"] == state["db3_name"] and state["same_client"] is True
    assert state["plans_indexes"].get("owner_key_1") is True


def test_kill_switch_import_creates_no_plans_collection_or_index():
    state = _import_database("true")
    assert "flintel_plans" not in state["collections"] and state["plans_indexes"] == {}


def test_credit_and_plan_code_never_use_the_primary_db_other_databases_or_user_counters():
    for name in ("search_credits.py", "credit_cost.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for forbidden in ("MONGODB2", "MONGODB4", "users_collection", "message_count", "import database"):
            if name == "search_credits.py" and forbidden == "import database":
                continue      # default_store() imports the module to take its db3 handles
            assert forbidden not in text, (name, forbidden)
    text = (ROOT / "search_credits.py").read_text(encoding="utf-8")
    assert "database.plans_collection" in text or 'getattr(database, "plans_collection"' in text
    assert "database.credit_usage_collection" in text
    tree = ast.parse(text)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ("db", "db2", "db4")
                and isinstance(n.value, ast.Name) and n.value.id == "database"]


def test_paid_limits_come_from_config_not_from_environment_shortcuts():
    text = (ROOT / "search_credits.py").read_text(encoding="utf-8")
    assert "os.environ" not in text and "getenv" not in text
    assert sc.plan_credits("standard") == 400 and sc.plan_credits("business") == 1500 and sc.plan_credits("free") is None
    assert sc.configured_limit() == 40
