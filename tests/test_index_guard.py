"""
tests/test_index_guard.py - required unique indexes: verification (not just create_index), fail-closed credit operations,
recovery without a restart, duplicates reported and never merged, readiness, and the billing "never discard a paid event" rule.

Evidence class: mongomock. It proves the logic (what is verified, what is refused, what stays available). It does NOT prove how a
real MongoDB server reports `index_information()` or builds a unique index over existing duplicates - that is staging work.
"""
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

mongomock = pytest.importorskip("mongomock")
import billing  # noqa: E402
import index_guard as ig  # noqa: E402
import search_credits as sc  # noqa: E402
from tests.test_billing_hardening import cfg, db, wd  # noqa: F401,E402
from tests.test_credit_carryover import ALICE, NOW, created_raw, deliver_c, plan, store  # noqa: E402
from tests.test_guest_credits import EMAIL, G, acct, acct_usage, acct_used, clock, coll, finish, world  # noqa: F401,E402
from tests.test_guest_gate import gapp, gg, _capture_real_identity_check  # noqa: F401,E402
from tests.test_answer_persistence import env  # noqa: F401,E402
from tests.test_search_credit_routes import app  # noqa: F401,E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_registry():
    ig.reset()
    yield
    ig.reset()


class Spy:
    """Collection proxy that records every WRITE call (and can forbid every call)."""
    WRITES = {"insert_one", "insert_many", "update_one", "update_many", "replace_one", "delete_one", "delete_many",
              "find_one_and_update", "find_one_and_delete", "find_one_and_replace", "create_index", "drop_index", "bulk_write"}

    def __init__(self, coll, forbid_all=False):
        self._c, self.writes, self.calls, self._forbid = coll, [], [], forbid_all

    def __getattr__(self, name):
        attr = getattr(self._c, name)
        if not callable(attr):
            return attr

        def wrapper(*a, **k):
            self.calls.append(name)
            if self._forbid:
                raise AssertionError(f"unexpected call {name}")
            if name in self.WRITES:
                self.writes.append(name)
            return attr(*a, **k)
        return wrapper


def mk(name="c", unique=True, extra=None, sparse=False, partial=None, key="owner_key"):
    c = mongomock.MongoClient()["idx"][name]
    kwargs = {}
    if unique:
        kwargs["unique"] = True
    if sparse:
        kwargs["sparse"] = True
    if partial:
        kwargs["partialFilterExpression"] = partial
    if unique or sparse or partial is not None or extra == "plain":
        c.create_index(key, **kwargs)
    return c


# ═════════════════ 1. verification, not creation ═════════════════
def test_a_single_field_unique_index_is_ok():
    assert ig.verify_unique_index(mk()) == ("ok", {})


def test_sparse_unique_is_accepted_partial_unique_is_not():
    assert ig.verify_unique_index(mk(unique=True, sparse=True))[0] == "ok"
    state, _ = ig.verify_unique_index(mk(unique=False, partial={"owner_key": {"$exists": True}}, extra="plain"))
    assert state != "ok"


def test_missing_index():
    assert ig.verify_unique_index(mk(unique=False))[0] == "missing"


def test_a_non_unique_index_is_not_enough():
    assert ig.verify_unique_index(mk(unique=False, extra="plain"))[0] == "not_unique"


def test_a_compound_index_containing_the_key_is_not_enough():
    c = mongomock.MongoClient()["idx"]["compound"]
    c.create_index([("owner_key", 1), ("period", 1)], unique=True)
    assert ig.verify_unique_index(c)[0] == "missing"


def test_duplicates_are_reported_with_hashed_samples_and_never_merged():
    raw = mongomock.MongoClient()["idx"]["dups"]
    raw.insert_many([{"owner_key": "a@example.test"}, {"owner_key": "a@example.test"}, {"owner_key": "b@example.test"}])
    spy = Spy(raw)
    state, detail = ig.verify_unique_index(spy)
    assert state == "duplicates" and detail["duplicate_keys"] == 1 and len(detail["samples"][0]) == 10
    assert "a@example.test" not in str(detail)                         # no raw key (PII) in what is reported/logged
    assert spy.writes == [] and raw.count_documents({}) == 3          # nothing merged, nothing deleted, nothing created


def test_registering_a_collection_with_duplicates_does_not_build_or_alter_anything(caplog):
    raw = mongomock.MongoClient()["idx"]["dups2"]
    raw.insert_many([{"owner_key": "a@example.test"}, {"owner_key": "a@example.test"}])
    spy = Spy(raw)
    assert ig.register("flintel_credit_usage", spy) == "duplicates"
    assert spy.writes == [] and any("duplicates" in r.getMessage() and "never merged" in r.getMessage() for r in caplog.records)


def test_an_unreachable_server_is_unverifiable_not_ok():
    class Down:
        def index_information(self):
            raise RuntimeError("down")
    assert ig.verify_unique_index(Down())[0] == "unverifiable"
    assert ig.register("x", Down()) == "unverifiable" and ig.check("x")[0] is False


def test_a_subsystem_that_is_switched_off_is_not_checked_at_all():
    spy = Spy(mk(), forbid_all=True)
    assert ig.register("flintel_credit_usage", spy, required=False) == "not_required"
    assert ig.check("flintel_credit_usage") == (True, "not_required") and spy.calls == []


def test_an_unregistered_name_always_passes():
    assert ig.check("never-registered") == (True, "unmanaged")


# ═════════════════ 2. fail closed: every operation that can create a document, charge or entitlement ═════════════════
@pytest.fixture
def broken(wd):
    """The real credit store and plan collection, with BOTH required unique indexes missing (registered as such)."""
    usage_spy, plans_spy = Spy(wd.usage), Spy(wd.plans)
    ig.register("flintel_credit_usage", mongomock.MongoClient()["g"]["u"])         # a collection without the unique index
    ig.register("flintel_plans", mongomock.MongoClient()["g"]["p"])
    st = sc.CreditStore(usage_spy, wd.chats, wd.jobs, lambda *a, **k: True, clock=lambda: NOW, enabled=lambda: True,
                        limit=lambda: 40, plans=plans_spy)
    return type("B", (), {"st": st, "usage": usage_spy, "plans": plans_spy, "wd": wd})


def test_usage_operations_are_refused_and_write_nothing(broken):
    st, W = broken.st, broken.usage
    for call in (lambda: st.reserve(ALICE, "email", "r1", "t", "c", True, cost=3),
                 lambda: st.freeze_free_period(ALICE, "2026-03", "c" * 32, "9001", "2026-04-09", NOW),
                 lambda: st.seed_guest_handover(ALICE, "email", "guest", "2026-03", 3, None, 0, NOW),
                 lambda: st.seed_guest_handover(ALICE, "email", "guest", "2026-02", 3, None, 0, NOW),       # the pending path too
                 lambda: st.resolve_pending_guest_handover(ALICE, "guest", "waive", "reason", 3, 0, NOW)):
        with pytest.raises(sc.CreditStoreError, match="required unique index unavailable: flintel_credit_usage"):
            call()
    assert W.writes == [] and broken.wd.usage.count_documents({}) == 0


def test_plan_operations_are_refused_and_write_nothing(broken):
    P, plans = broken.plans, broken.plans
    end = NOW + timedelta(days=30)
    for call in (lambda: sc.activate_plan(P, ALICE, "standard", end, "manual:1", now=NOW),
                 lambda: sc.set_plan_entitlement(P, ALICE, "standard", NOW, end, "ls:1", 1, NOW),
                 lambda: sc.claim_activation(P, ALICE, "a" * 32, NOW),
                 lambda: sc.activate_with_carryover(P, ALICE, "standard", NOW, end, "ls:1", 1, NOW, "2026-04-09", end,
                                                    {"claim_id": "a" * 32, "sub_id": "9001", "free_period": "2026-03", "free_limit": 40})):
        with pytest.raises(sc.CreditStoreError, match="required unique index unavailable: flintel_plans"):
            call()
    assert plans.writes == [] and broken.wd.plans.count_documents({}) == 0


def test_nothing_to_move_needs_no_index_and_makes_no_write(broken):
    assert broken.st.seed_guest_handover(ALICE, "email", "guest", "2026-03", 0)["status"] == "nothing_to_move"
    assert broken.usage.writes == []


def test_settlement_refund_recovery_and_reads_stay_available(wd):
    """The app stays usable for diagnostics and recovery: an in-flight reservation can still be refunded / settled."""
    st = store(wd)
    t = st.reserve(ALICE, "email", "r1", "t", "c1", True, cost=3)
    assert isinstance(t, sc.Ticket)
    ig.register("flintel_credit_usage", mongomock.MongoClient()["g"]["u"])                 # the index "disappears"
    assert st.status(ALICE)["used"] == 3                                                    # read
    assert st.lookup(ALICE, "r1") is not None                                               # read
    st.recover(ALICE)                                                                       # recovery sweep
    assert st.self_refund(t) is True                                                        # settlement / refund
    assert st.status(ALICE)["used"] == 0
    with pytest.raises(sc.CreditStoreError):
        st.reserve(ALICE, "email", "r2", "t", "c1", True, cost=3)                           # but nothing new is admitted


def test_the_route_level_answer_is_unavailable_not_a_crash(gapp):
    ig.register("flintel_credit_usage", mongomock.MongoClient()["g"]["u"])
    resp = gapp["send"]("Find people looking for a CRM for small teams", submit_key="aaaaaaaa-1111-4bbb-8ccc-000000000001")
    assert resp.status_code == 503                                                           # refused as 'unavailable' (not a crash)
    assert gapp["gate_coll"].count_documents({"inflight": {"$ne": None}}) == 0               # the guest reservation was released


# ═════════════════ 3. healing without a restart ═════════════════
def test_creating_the_index_heals_the_subsystem_after_the_refresh_interval(wd, monkeypatch):
    raw = mongomock.MongoClient()["g"]["u"]
    assert ig.register("flintel_credit_usage", raw) == "missing"
    clock = {"t": 1000.0}
    monkeypatch.setattr(ig, "_now", lambda: clock["t"])
    ig._registry["flintel_credit_usage"]["checked_at"] = clock["t"]
    assert ig.check("flintel_credit_usage")[0] is False                                      # inside the interval: cached
    raw.create_index("owner_key", unique=True)                                               # the operator fixes it
    assert ig.check("flintel_credit_usage")[0] is False                                      # still cached, no hammering
    clock["t"] += ig.REFRESH_SECONDS + 1
    assert ig.check("flintel_credit_usage") == (True, "ok")
    assert isinstance(store(wd).reserve(ALICE, "email", "r1", "t", "c", True, cost=3), sc.Ticket)


def test_duplicates_are_rechecked_only_every_five_minutes(monkeypatch):
    raw = mongomock.MongoClient()["g"]["d"]
    raw.insert_many([{"owner_key": "a@example.test"}, {"owner_key": "a@example.test"}])
    spy = Spy(raw)
    clock = {"t": 0.0}
    monkeypatch.setattr(ig, "_now", lambda: clock["t"])
    assert ig.register("flintel_credit_usage", spy) == "duplicates"
    base = spy.calls.count("aggregate")
    clock["t"] += ig.REFRESH_SECONDS + 5
    ig.check("flintel_credit_usage")
    assert spy.calls.count("aggregate") == base                                              # the expensive scan is not repeated
    clock["t"] += ig.REFRESH_DUPLICATES_SECONDS
    ig.check("flintel_credit_usage")
    assert spy.calls.count("aggregate") == base + 1 and raw.count_documents({}) == 2         # and nothing was merged


# ═════════════════ 4. billing: a paid event is never discarded ═════════════════
def test_a_paid_event_with_the_plans_index_missing_is_stored_and_completes_after_the_index_exists(cfg, wd, monkeypatch):
    raw = mongomock.MongoClient()["g"]["p"]
    ig.register("flintel_plans", raw)
    clock = {"t": 100.0}
    monkeypatch.setattr(ig, "_now", lambda: clock["t"])
    ig._registry["flintel_plans"]["checked_at"] = clock["t"]
    raw_event = created_raw()
    with pytest.raises(sc.CreditStoreError):
        deliver_c(wd, raw_event, NOW, credits=store(wd))
    assert wd.subs.find_one({"owner_key": ALICE})["plan"] == "standard"                      # the subscription state IS stored
    assert wd.events.count_documents({"state": "failed"}) == 1 and plan(wd) is None           # event re-processable, no entitlement
    raw.create_index("owner_key", unique=True)
    clock["t"] += ig.REFRESH_SECONDS + 1
    assert deliver_c(wd, raw_event, NOW, credits=store(wd))["status"] == "applied"           # the provider's retry / a re-send
    assert plan(wd)["plan"] == "standard"


def test_checkout_gate_follows_the_live_verification(monkeypatch, gapp):
    rt = gapp["rt"]
    monkeypatch.setattr(rt.billing, "configured", lambda: True)
    raw = mongomock.MongoClient()["g"]["p"]
    ig.register("flintel_plans", raw)
    assert rt._billing_checkout_enabled() is False
    raw.create_index("owner_key", unique=True)
    monkeypatch.setattr(ig, "_now", lambda: 10 ** 9)                                         # refresh is due
    assert rt._billing_checkout_enabled() is True


# ═════════════════ 5. guests: hand-over is deferred, not lost ═════════════════
def test_a_guest_signup_while_the_usage_index_is_missing_claims_the_identity_and_the_seed_is_applied_after_recovery(world, monkeypatch):
    assert finish(world.gate, G, "d", 3) == "completed"
    raw = mongomock.MongoClient()["g"]["u"]
    ig.register("flintel_credit_usage", raw)
    assert world.gg.handover_to_account(G, EMAIL, now=world.clock.t) is None                  # seed refused (swallowed, logged)
    assert world.coll.find_one({"_id": G})["handover"]["to"] == EMAIL                         # but the identity IS closed
    assert acct_usage(world) == {}
    raw.create_index("owner_key", unique=True)
    monkeypatch.setattr(ig, "_now", lambda: 10 ** 9)
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) == "seeded" and acct_used(world) == 3


# ═════════════════ 6. readiness ═════════════════
def _ready(rt):
    import json
    resp = rt.api_health_ready()
    return resp.status_code, json.loads(resp.body)


def test_readiness_is_ok_when_nothing_is_registered_or_everything_is_verified(gapp):
    rt = gapp["rt"]
    assert _ready(rt) == (200, {"status": "ok", "subsystems": {"credits": "unmanaged", "billing_plans": "unmanaged"}})
    ig.register("flintel_credit_usage", mk())
    ig.register("flintel_plans", mk("p"))
    assert _ready(rt)[1] == {"status": "ok", "subsystems": {"credits": "ok", "billing_plans": "ok"}}


def test_readiness_reports_the_affected_subsystem_as_unavailable_but_stays_200(gapp):
    rt = gapp["rt"]
    ig.register("flintel_credit_usage", mongomock.MongoClient()["g"]["u"])
    ig.register("flintel_plans", mk("p"))
    code, body = _ready(rt)
    assert code == 200 and body["status"] == "degraded"
    assert body["subsystems"] == {"credits": "unavailable", "billing_plans": "ok"} and body["reasons"] == {"credits": "missing"}


def test_readiness_with_the_kill_switch_does_not_call_credits_unavailable(gapp):
    ig.register("flintel_credit_usage", mongomock.MongoClient()["g"]["u"], required=False)
    assert _ready(gapp["rt"])[1]["subsystems"]["credits"] == "not_required"


# ═════════════════ 7. wiring and separation of concerns ═════════════════
def test_startup_registers_both_required_indexes_with_the_same_required_rules_as_their_creation():
    src = (ROOT / "database.py").read_text(encoding="utf-8")
    assert re.search(r'index_guard\.register\("flintel_credit_usage", credit_usage_collection,\s*required=not getattr\(_config, "CREDIT_ENFORCEMENT_DISABLED", False\)\)', src)
    assert 'index_guard.register("flintel_plans", plans_collection, required=_plans_index_required)' in src
    assert "_plans_index_required = _billing_configured or not getattr(_config, \"CREDIT_ENFORCEMENT_DISABLED\", False)" in src


def test_performance_only_indexes_are_not_part_of_the_guard():
    src = (ROOT / "index_guard.py").read_text(encoding="utf-8")
    assert "handover.to" in src                         # mentioned only as the example of what is NOT covered
    for name in ("flintel_guest_gate", "turn_finishing", "flintel_subscriptions"):
        assert f'register("{name}"' not in (ROOT / "database.py").read_text(encoding="utf-8")


def test_only_operations_that_can_create_documents_or_entitlements_are_gated():
    import ast
    tree = ast.parse((ROOT / "search_credits.py").read_text(encoding="utf-8"))
    gated = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "_require_index":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "_require_index":
                    gated.add(node.name)
    assert gated == {"activate_plan", "set_plan_entitlement", "claim_activation", "activate_with_carryover", "freeze_free_period",
                     "seed_guest_handover", "resolve_pending_guest_handover", "reserve"}
    # settlement, refund, recovery and reads are deliberately NOT gated
    for settle in ("start", "self_refund", "claim", "commit", "recover", "recover_entry", "status", "lookup", "late_delivery",
                   "revoke_claim", "decide_zero", "release_activation", "commit_guest_hold", "release_guest_hold"):
        assert settle not in gated
