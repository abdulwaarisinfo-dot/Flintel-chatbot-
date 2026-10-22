"""
tests/test_guest_credits.py - the guest CREDIT allowance (guest_gate.py) and the hand-over of a guest's used credits to a
newly created account (guest_gate.handover_to_account / reconcile_handover + search_credits.seed_guest_handover).

What these tests are: deterministic logical tests of the REAL GuestGate and the REAL CreditStore on mongomock with a fixed clock.
What they are NOT: proof of real MongoDB concurrency (mongomock serialises single-document operations; the thread test only shows
that the conditional filter admits one reservation in that harness), and nothing here talks to Google, Lemon Squeezy or a mailbox.

Numbers: guest allowance 5 credits, free grant 40, a "search" costs whatever is bound (4 = the existing deterministic fallback).
"""
import datetime as dt
import json
import sys
import threading

import pytest

from tests.test_search_credit_routes import flat_cost  # noqa: F401  (fixture)
from tests.test_guest_gate import (  # noqa: F401  (fixtures + helpers)
    e2e, gapp, gate_doc, gg, last_token, post_search, gid_of, chat_of, _capture_real_identity_check,
)
from tests.test_guest_gate import auth, _guest_with_completed_chat, _Oauth  # noqa: F401
from tests.test_answer_persistence import env, GOOD  # noqa: F401
from tests.test_search_credit_routes import app, KEY1, KEY2  # noqa: F401

mongomock = pytest.importorskip("mongomock")
import search_credits as sc  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
EMAIL = "new.user@example.test"
OTHER = "someone.else@example.test"
G = "guest-aaaa"


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t = self.t + dt.timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def coll():
    return mongomock.MongoClient()["gate"]["flintel_guest_gate"]


@pytest.fixture
def acct(clock):
    """The account side: a real CreditStore (free grant 40) on mongomock."""
    m = mongomock.MongoClient()["acct"]
    m.u.create_index("owner_key", unique=True)
    return sc.CreditStore(m.u, m.chats, m.jobs, lambda *a, **k: True, clock=clock, enabled=lambda: True, limit=lambda: 40, plans=m.plans)


@pytest.fixture
def world(gg, coll, acct, clock, monkeypatch):
    """A credit-mode gate (allowance 5) wired in as the process-wide gate, plus the account store as the credit store."""
    gate = gg.GuestGate(coll, clock=clock, credit_allowance=5, stale_seconds=900)
    monkeypatch.setattr(gg, "default_gate", lambda: gate)
    monkeypatch.setattr(gg, "_account_store", lambda: acct)
    monkeypatch.setattr(sc, "default_store", lambda: acct)
    return type("W", (), {"gg": gg, "gate": gate, "coll": coll, "acct": acct, "clock": clock})


def finish(gate, guest, token, cost, topic="t"):
    """One genuine search start to finish: reserve (with its cost), complete."""
    assert gate.reserve(guest, token, topic, "chat1", cost=cost).kind == "allow"
    return gate.complete(guest, token, "chat1", topic)


def acct_usage(w, owner=EMAIL):
    return w.acct.coll.find_one({"owner_key": owner}) or {}


def acct_used(w, period="2026-10", owner=EMAIL):
    return (acct_usage(w, owner).get("used_by_period") or {}).get(period, 0)


def inv_ok(w, owner=EMAIL):
    assert sc.check_invariant(acct_usage(w, owner)) == []


# ═════════════════ 1. the gate in credit mode ═════════════════
def test_credits_are_counted_cumulatively_and_one_search_may_overshoot_the_allowance(world):
    gate = world.gate
    assert gate.credits_mode and gate.limit == 5
    assert finish(gate, G, "t1", 4) == "completed"
    st = gate.state(G)
    assert (st["credits_used"], st["completed"], st["used"], st["mode"]) == (4, 1, False, "credits")      # 4 < 5: still allowed
    assert gate.peek(G).kind == "allow"
    assert finish(gate, G, "t2", 4) == "completed"                                                          # 4 + 4 = 8: overshoot allowed
    st = gate.state(G)
    assert (st["credits_used"], st["completed"], st["used"]) == (8, 2, True)
    assert gate.reserve(G, "t3", "t", "chat1", cost=1).kind == "auth_required"                              # and now closed
    assert gate.peek(G).kind == "auth_required"


def test_one_expensive_search_from_zero_is_allowed_and_then_closes_the_gate(world):
    assert finish(world.gate, G, "big", 7) == "completed"
    assert world.gate.state(G)["credits_used"] == 7 and world.gate.reserve(G, "n", "t", "c", cost=1).kind == "auth_required"


def test_exactly_the_allowance_closes_the_gate(world):
    assert finish(world.gate, G, "five", 5) == "completed"
    assert world.gate.state(G)["used"] is True


def test_a_search_that_does_not_complete_counts_nothing_and_can_be_retried(world):
    gate = world.gate
    assert gate.reserve(G, "t1", "t", "c", cost=4).kind == "allow"
    assert gate.release(G, "t1") is True                                                                    # failed / timed out
    assert gate.state(G)["credits_used"] == 0 and gate.state(G)["completed"] == 0
    assert gate.reserve(G, "t2", "t", "c", cost=4).kind == "allow"                                          # a retry is free
    assert gate.complete(G, "t2", "c", "t") == "completed" and gate.state(G)["credits_used"] == 4


def test_a_replayed_completion_counts_once(world):
    gate = world.gate
    assert finish(gate, G, "t1", 4) == "completed"
    assert gate.complete(G, "t1", "chat1", "t") == "duplicate"
    assert gate.state(G)["credits_used"] == 4 and gate.state(G)["completed"] == 1


def test_the_last_bound_cost_wins_and_a_missing_cost_uses_the_existing_deterministic_fallback(world):
    import credit_cost
    gate = world.gate
    assert gate.reserve(G, "t1", "t", "c", cost=2).kind == "allow"
    assert gate.bind_cost(G, "t1", 3) is True                                                               # the credit reservation's stored charge
    assert gate.complete(G, "t1", "c", "t") == "completed" and gate.state(G)["credits_used"] == 3
    assert gate.bind_cost(G, "t1", 0) is False and gate.bind_cost(G, "t1", True) is False                   # unusable costs are refused
    gate.coll.delete_one({"_id": "g2"})
    assert gate.reserve("g2", "u1", "t", "c").kind == "allow"                                                # no cost at all
    assert gate.complete("g2", "u1", "c", "t") == "completed"
    assert gate.state("g2")["credits_used"] == credit_cost.fallback_cost() >= 1


def test_revert_removes_only_that_tokens_credits(world):
    gate = world.gate
    assert finish(gate, G, "t1", 3) == "completed"
    assert finish(gate, G, "t2", 1) == "completed"
    assert gate.revert(G, "t2") is True
    assert gate.state(G)["credits_used"] == 3 and gate.state(G)["completed"] == 1
    assert gate.revert(G, "t2") is False                                                                    # not counted any more


def test_concurrent_reserves_admit_exactly_one(world):
    gate, results = world.gate, []

    def go(i):
        results.append(gate.reserve(G, f"t{i}", "t", "c", cost=4).kind)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results).count("allow") == 1 and set(results) <= {"allow", "busy"}


def test_allowance_zero_switches_the_gate_off(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, credit_allowance=0)
    assert gate.reserve(G, "t1", "t", "c", cost=4).kind == "allow" and coll.count_documents({}) == 0


# ═════════════════ 2. configuration precedence ═════════════════
@pytest.mark.parametrize("allowance,explicit,legacy,expected", [
    (None, False, 1, ("credits", 5)),           # nothing set: the default 5 credits
    (None, True, 2, ("searches", 2)),           # only the old variable: its old meaning
    (None, True, 0, ("searches", 0)),           # the old variable's "off"
    (3, True, 2, ("credits", 3)),               # both: the new one wins
    (3, False, 1, ("credits", 3)),
    (0, True, 2, ("credits", 0)),               # 0 = off, and it wins over the old variable
    (7, False, 1, ("credits", 7)),
])
def test_gate_mode_precedence(gg, monkeypatch, allowance, explicit, legacy, expected):
    cfg = sys.modules["config"]
    monkeypatch.setattr(cfg, "GUEST_CREDIT_ALLOWANCE", allowance, raising=False)
    monkeypatch.setattr(cfg, "GUEST_SEARCH_LIMIT_EXPLICIT", explicit, raising=False)
    monkeypatch.setattr(cfg, "GUEST_SEARCH_LIMIT", legacy, raising=False)
    assert gg.gate_mode() == expected and gg.configured_limit() == expected[1]


def test_the_config_module_parses_the_new_variable(monkeypatch):
    import importlib
    cfg = importlib.import_module("config")
    assert cfg.GUEST_CREDIT_ALLOWANCE_DEFAULT == 5
    assert cfg.GUEST_HANDOVER_MIN_REMAINING_HOURS == 72 and cfg.ACTIVATION_CLAIM_STALE_SECONDS == 120


def test_a_gate_built_with_an_explicit_search_limit_keeps_the_legacy_meaning(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, limit=2, stale_seconds=900)
    assert not gate.credits_mode and gate.limit == 2
    for i in (1, 2):
        assert gate.reserve(G, f"t{i}", "t", "c").kind == "allow" and gate.complete(G, f"t{i}", "c", "t") == "completed"
    assert gate.state(G)["used"] is True and gate.reserve(G, "t3", "t", "c").kind == "auth_required"


# ═════════════════ 3. records written before credit mode (Option A) ═════════════════
def test_a_legacy_record_without_a_recoverable_cost_counts_zero_credits_and_is_never_invented(world):
    world.coll.insert_one({"_id": G, "completed": 2, "completed_tokens": ["a", "b"], "tokens": ["a", "b"], "inflight": None,
                           "created_at": T0, "updated_at": T0})
    st = world.gate.state(G)
    assert (st["credits_used"], st["completed"]) == (0, 2) and st["used"] is False       # read-only recovery: nothing written yet
    assert "credits_used" not in world.coll.find_one({"_id": G})
    assert world.gate.reserve(G, "t3", "t", "c", cost=4).kind == "allow"                  # the one-time extra allowance of old records
    doc = world.coll.find_one({"_id": G})
    assert (doc["credits_used"], doc["legacy_unmatched"]) == (0, 2)                      # persisted once, 2 searches unmatched


def test_a_legacy_record_recovers_costs_only_through_the_reliable_chain(world):
    msg_chat = {"chat_id": "chat1", "owner_key": G, "messages": [{"request_id": "a", "credit_request_id": "rid-a"}]}
    world.acct.chats.insert_one(msg_chat)
    world.acct.coll.insert_one({"owner_key": G, "owner_type": "anon", "used_by_period": {"2026-10": 4}, "archived_by_period": {},
                                "limit_max_by_period": {"2026-10": 40},
                                "res": {"rid-a": {"state": sc.COMMITTED, "reason": sorted(sc.DELIVERED_REASONS)[0], "cost": 4}}})
    world.coll.insert_one({"_id": G, "completed": 2, "completed_tokens": ["a", "b"], "tokens": ["a", "b"], "inflight": None,
                           "created_at": T0, "updated_at": T0})
    assert world.gate.recover_legacy(world.coll.find_one({"_id": G})) == (4, 1)          # 'a' matched (4 credits), 'b' unmatched (0)
    assert world.gate.state(G)["credits_used"] == 4


# ═════════════════ 4. hand-over to a newly created account ═════════════════
@pytest.mark.parametrize("guest_credits,remaining", [(0, 40), (3, 37), (5, 35), (7, 33), (8, 32)])
def test_the_new_accounts_starting_credits_are_the_free_grant_minus_the_guest_credits(world, guest_credits, remaining):
    if guest_credits:
        assert finish(world.gate, G, "t1", guest_credits) == "completed"
    out = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert (out is not None) == bool(guest_credits)                                       # nothing to move: nothing written
    s = world.acct.status(EMAIL)
    assert (s["limit"], s["remaining"], s["period"]) == (40, remaining, "2026-10")
    assert acct_used(world) == guest_credits
    inv_ok(world)


def test_the_handover_runs_once_whichever_path_calls_it_and_never_for_another_account(world):
    assert finish(world.gate, G, "t1", 4) == "completed"
    first = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    again = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)                       # a double submit / the Google path later
    assert first["status"] == "applied" and again["status"] == "duplicate"
    assert acct_used(world) == 4
    assert world.gg.handover_to_account(G, OTHER, now=world.clock.t) is None                # guest usage moves to ONE account only
    assert acct_used(world, owner=OTHER) == 0 and world.acct.status(OTHER)["remaining"] == 40
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) in ("seeded", None)
    assert acct_used(world) == 4
    inv_ok(world)


def test_a_non_guest_owner_is_never_handed_anything(world):
    assert world.gg.handover_to_account("someone@example.test", EMAIL) is None
    assert world.gg.reconcile_handover("someone@example.test", EMAIL) is None


def test_the_handover_is_clamped_to_what_the_grant_has_room_for(world):
    assert finish(world.gate, G, "t1", 8) == "completed"
    for i in range(3):                                                                      # the account already spent 38 of 40
        t = world.acct.reserve(EMAIL, "email", f"r{i}", "t", "c", True, cost=(10 if i < 2 else 18))
        assert isinstance(t, sc.Ticket) and world.acct.start(t) == sc.DONE and world.acct.claim(t) == sc.DONE
    out = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert out["applied_snapshot"] == 2 and acct_used(world) == 40
    assert world.acct.status(EMAIL)["remaining"] == 0                                       # never negative
    inv_ok(world)


def test_a_search_still_running_at_signup_is_held_then_committed_when_it_completes(world):
    assert finish(world.gate, G, "t1", 3) == "completed"
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"                   # still working when the visitor signs up
    out = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert (out["applied_snapshot"], out["applied_hold"]) == (3, 4) and acct_used(world) == 7
    assert world.acct.status(EMAIL)["remaining"] == 33
    assert world.gate.complete(G, "run", "c", "t") == "completed"                           # the search ends and counts
    assert world.gg._settle_hold(G, "run") == "committed"
    assert acct_used(world) == 7 and acct_usage(world)["guest_handover"]["hold"]["state"] == "committed"
    assert world.gg._settle_hold(G, "run") == "noop"                                        # repeating changes nothing
    inv_ok(world)


def test_a_search_that_fails_after_the_handover_gives_its_credits_back_exactly_once(world):
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    out = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert out["applied_hold"] == 4 and acct_used(world) == 4
    assert world.gg._settle_hold(G, "run", released=True) == "released"
    assert acct_used(world) == 0 and world.acct.status(EMAIL)["remaining"] == 40
    assert world.gg._settle_hold(G, "run", released=True) in ("noop", None)                  # a second release does nothing
    assert acct_used(world) == 0
    # the released search can no longer be counted on the guest side (decided once, on the gate document)
    assert world.gate.complete(G, "run", "c", "t") != "completed"
    assert world.gate.state(G)["credits_used"] == 0
    inv_ok(world)


def test_a_counted_search_cannot_be_released(world):
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert world.gate.complete(G, "run", "c", "t") == "completed"
    assert world.gate.fence_release(G, "run") is False                                      # counted: the fence refuses
    assert world.gg._settle_hold(G, "run", released=True) == "committed"
    assert acct_used(world) == 4
    inv_ok(world)


def test_completion_in_the_race_with_the_claim_is_either_in_the_snapshot_or_the_hold_never_both_or_neither(world, monkeypatch):
    """The claim's compare-and-set carries credits_used + updated_at: a completion landing between its read and its write
    makes it re-read, so the credits are counted exactly once."""
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    real = world.coll.update_one
    fired = {"n": 0}

    class Proxy:
        def __getattr__(self, name):
            return getattr(world.coll, name)

        def update_one(self, filt, update, *a, **k):
            if "handover" in str(update) and not fired["n"]:
                fired["n"] = 1
                assert world.gate.complete(G, "run", "c", "t") == "completed"          # lands right before the claim's write
            return real(filt, update, *a, **k)
    monkeypatch.setattr(world.gate, "coll", Proxy())
    out = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert fired["n"] == 1
    assert (out["applied_snapshot"], out["applied_hold"]) == (4, 0) and acct_used(world) == 4     # exactly once
    inv_ok(world)


def test_a_claimed_but_unseeded_handover_is_finished_by_the_reconcile_hook(world):
    assert finish(world.gate, G, "t1", 5) == "completed"
    assert world.gate.claim_handover(G, EMAIL, world.clock.t) is not None                   # the process died right after the claim
    assert acct_used(world) == 0
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) == "seeded"
    assert acct_used(world) == 5 and world.acct.status(EMAIL)["remaining"] == 35
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) == "seeded"             # idempotent
    assert acct_used(world) == 5
    inv_ok(world)


def test_an_abandoned_hold_is_released_by_the_reconcile_hook_once_the_search_is_gone(world):
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"
    world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert acct_used(world) == 4
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) == "pending"            # still running: nothing decided
    world.clock.advance(901)                                                                # the reservation is stale: the search is gone
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) == "released"
    assert acct_used(world) == 0
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) in ("noop", None)
    assert acct_used(world) == 0
    inv_ok(world)


def test_a_wrong_account_cannot_reconcile_someone_elses_handover(world):
    assert finish(world.gate, G, "t1", 5) == "completed"
    world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert world.gg.reconcile_handover(G, OTHER, now=world.clock.t) is None
    assert acct_used(world, owner=OTHER) == 0


# ═════════════════ 5. the month boundary (72-hour rule) ═════════════════
@pytest.mark.parametrize("at,expected", [
    (dt.datetime(2026, 3, 28, 12, 0, tzinfo=UTC), "2026-03"),            # 3.5 days left: this month
    (dt.datetime(2026, 3, 28, 23, 59, 59, tzinfo=UTC), "2026-03"),       # 72h + 1s left
    (dt.datetime(2026, 3, 29, 0, 0, 0, tzinfo=UTC), "2026-03"),          # exactly 72h left: still this month
    (dt.datetime(2026, 3, 29, 0, 0, 1, tzinfo=UTC), "2026-04"),          # just under 72h: next month
    (dt.datetime(2026, 3, 31, 23, 59, 30, tzinfo=UTC), "2026-04"),
    (dt.datetime(2026, 12, 31, 23, 59, 30, tzinfo=UTC), "2027-01"),
])
def test_handover_window_period(at, expected):
    assert sc.handover_window_period(at) == expected


def test_a_signup_seconds_before_midnight_does_not_burn_the_credits_into_a_month_that_is_about_to_end(world):
    assert finish(world.gate, G, "t1", 7) == "completed"
    world.clock.t = dt.datetime(2026, 3, 31, 23, 59, 30, tzinfo=UTC)
    out = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert out["status"] == "applied"
    s = world.acct.status(EMAIL)
    assert (s["period"], s["limit"], s["used"], s["remaining"]) == ("2026-03", 40, 0, 40)   # March spendable: the full 40
    world.clock.t = dt.datetime(2026, 4, 1, 0, 0, 30, tzinfo=UTC)
    s = world.acct.status(EMAIL)
    assert (s["period"], s["limit"], s["used"], s["remaining"]) == ("2026-04", 40, 7, 33)   # April: 40 - 7
    inv_ok(world)


def test_control_a_signup_with_days_left_charges_the_current_month(world):
    assert finish(world.gate, G, "t1", 7) == "completed"
    world.clock.t = dt.datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    s = world.acct.status(EMAIL)
    assert (s["period"], s["used"], s["remaining"]) == ("2026-03", 7, 33)
    inv_ok(world)


# ═════════════════ 6. a new account's free month still flows into the first paid month only once ═════════════════
def test_handed_over_credits_lower_the_one_time_carryover_exactly_like_other_free_usage(world):
    """7 guest credits + 3 own = 10 used of 40 in the free month -> a first paid activation carries 30, not 40."""
    assert finish(world.gate, G, "t1", 7) == "completed"
    world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    t = world.acct.reserve(EMAIL, "email", "own", "t", "c", True, cost=3)
    assert isinstance(t, sc.Ticket) and world.acct.start(t) == sc.DONE and world.acct.claim(t) == sc.DONE
    assert world.acct.carryover_free_period(EMAIL, world.clock.t) == "2026-10"
    fz = world.acct.freeze_free_period(EMAIL, "2026-10", "c" * 32, "9001", "2026-11-09", world.clock.t)
    assert fz == {"bonus": 30, "frozen_used": 10}
    inv_ok(world)


# ═════════════════ 7. the real routes: sign-up / Google and the search path in credit mode ═════════════════
@pytest.fixture
def credit_routes(e2e, monkeypatch):
    """The end-to-end harness with the guest gate in CREDIT mode (allowance 5) and the real cost router."""
    cfg = sys.modules["config"]
    monkeypatch.setattr(cfg, "GUEST_CREDIT_ALLOWANCE", 5, raising=False)
    monkeypatch.setattr(sc, "default_store", lambda: e2e["store"]())      # the harness's credit store (enforcement is off in tests)
    return e2e


def used_of(a, owner):
    doc = a["db"].flintel_credit_usage.find_one({"owner_key": owner}) or {}
    return sum(int(v) for v in (doc.get("used_by_period") or {}).values())


def test_a_guest_search_in_credit_mode_binds_the_credit_stores_charge_to_the_gate(credit_routes):
    a = credit_routes
    client = a["new_client"]()
    client.get("/")
    resp = post_search(client)
    assert resp.status_code == 303, resp.text[:300]
    guest = gid_of(a["idx"], client)
    doc = a["gate_coll"].find_one({"_id": guest})
    charged = used_of(a, guest)
    assert charged >= 1 and doc["inflight"]["cost"] == charged                              # the gate holds the SAME cost the store charged
    assert {e["c"] for e in doc["token_costs"]} == {charged}                                # (bound again after the store's reserve)
    a["gg"].on_answer_complete(guest, chat_of(resp), "t", doc["inflight"]["token"], json.dumps(GOOD))
    assert a["gate_coll"].find_one({"_id": guest})["credits_used"] == charged


def test_email_signup_hands_the_guests_credits_to_the_new_account_once(credit_routes):
    from tests.otp_helpers import signup_with_code
    a = credit_routes
    client = a["new_client"]()
    client.get("/")
    resp = post_search(client)
    guest = gid_of(a["idx"], client)
    token = a["gate_coll"].find_one({"_id": guest})["inflight"]["token"]
    a["gg"].on_answer_complete(guest, chat_of(resp), "t", token, json.dumps(GOOD))
    credits = a["gate_coll"].find_one({"_id": guest})["credits_used"]
    assert credits >= 1
    out = signup_with_code(client, "handover.new@example.com", password="correct-horse-1")
    assert out.json().get("ok") is True
    doc = a["db"].flintel_credit_usage.find_one({"owner_key": "handover.new@example.com"})
    assert doc is not None and doc["guest_handover"]["guest"] == guest
    assert doc["guest_handover"]["snapshot"] == credits
    assert sum((doc.get("used_by_period") or {}).values()) == doc["guest_handover"]["applied_snapshot"] == min(credits, 40)
    assert sc.check_invariant(doc) == []
    assert a["gate_coll"].find_one({"_id": guest})["handover"]["to"] == "handover.new@example.com"


def test_logging_in_to_an_existing_account_never_hands_anything_over(credit_routes):
    a = credit_routes
    a["users"].insert_one({"email": "old@example.com", "password_hash": "h:correct-horse-1", "provider": "email"})
    client = a["new_client"]()
    client.get("/")
    resp = post_search(client)
    guest = gid_of(a["idx"], client)
    a["gg"].on_answer_complete(guest, chat_of(resp), "t", a["gate_coll"].find_one({"_id": guest})["inflight"]["token"], json.dumps(GOOD))
    r = client.post("/login", data={"email": "old@example.com", "password": "correct-horse-1", "resume": "1"},
                    headers={"Accept": "application/json"})
    assert r.json()["ok"] is True
    assert "handover" not in a["gate_coll"].find_one({"_id": guest})
    assert a["db"].flintel_credit_usage.find_one({"owner_key": "old@example.com"}) is None


def test_upsert_google_user_reports_whether_it_created_the_account(env):
    import index
    users = mongomock.MongoClient()["g"]["users"]
    index.users_collection = users
    first, second = {}, {}
    u1 = index.upsert_google_user("gid-1", "g.user@example.com", "G User", created_out=first)
    u2 = index.upsert_google_user("gid-1", "g.user@example.com", "G User", created_out=second)
    assert first.get("created") is True and not second.get("created") and u1["_id"] == u2["_id"]
    users.insert_one({"email": "manual@example.com", "password_hash": "h", "provider": "email"})
    linked = {}
    index.upsert_google_user("gid-2", "manual@example.com", "M", created_out=linked)       # linking an existing e-mail account
    assert not linked.get("created")
    assert index.upsert_google_user("gid-3", "plain@example.com", "P") is not None          # the old call shape still works


# ── Google sign-in: only a CREATED account receives the hand-over ──
@pytest.fixture
def gauth(auth, monkeypatch):
    cfg = sys.modules["config"]
    monkeypatch.setattr(cfg, "GUEST_CREDIT_ALLOWANCE", 5, raising=False)
    m = mongomock.MongoClient()["g_acct"]
    m.u.create_index("owner_key", unique=True)
    store = sc.CreditStore(m.u, m.chats, m.jobs, lambda *a, **k: True, enabled=lambda: True, limit=lambda: 40, plans=m.plans)
    monkeypatch.setattr(sc, "default_store", lambda: store)
    auth["acct_coll"] = m.u
    return auth


def test_google_signup_hands_the_guests_credits_over_once_and_a_duplicate_callback_adds_nothing(gauth, gg, monkeypatch):
    owner = _guest_with_completed_chat(gauth, gg)
    credits = gauth["gate"].find_one({"_id": owner})["credits_used"]
    assert credits >= 1
    monkeypatch.setattr(gauth["rt"], "oauth", _Oauth())
    client = gauth["client"]
    client.get("/auth/google/login?resume=1", follow_redirects=False)
    client.get("/auth/google/callback", follow_redirects=False)
    doc = gauth["acct_coll"].find_one({"owner_key": "gg@example.com"})
    assert doc is not None and sum(doc["used_by_period"].values()) == min(credits, 40) == doc["guest_handover"]["applied_snapshot"]
    assert sc.check_invariant(doc) == []
    client.get("/auth/google/callback", follow_redirects=False)                           # the callback fires again
    again = gauth["acct_coll"].find_one({"owner_key": "gg@example.com"})
    assert sum(again["used_by_period"].values()) == sum(doc["used_by_period"].values())


def test_a_returning_google_user_gets_no_handover(gauth, gg, monkeypatch):
    gauth["users"].insert_one({"email": "gg@example.com", "google_id": "g-123", "name": "G G", "provider": "google"})
    owner = _guest_with_completed_chat(gauth, gg)
    monkeypatch.setattr(gauth["rt"], "oauth", _Oauth())
    gauth["client"].get("/auth/google/login?resume=1", follow_redirects=False)
    gauth["client"].get("/auth/google/callback", follow_redirects=False)
    assert gauth["acct_coll"].find_one({"owner_key": "gg@example.com"}) is None
    assert "handover" not in gauth["gate"].find_one({"_id": owner})
test_guest_credits.py
