"""
tests/test_credit_finalization.py - the final pass:

  1. a guest identity that CREATED an account is closed for good (also with zero credits / no search yet): a logout does not
     give it a fresh guest allowance; new visitors, existing-account logins and the in-flight hold token are unaffected
  2. the first paid activation after an expired (pending) handover that was explicitly resolved: what the carryover does
     and does not include, for "apply_late" and for "waive"
  3. a counted-but-undelivered answer after the handover: real answer pipeline, repeated failure, redelivery without
     recounting, and the targeted re-queue that does not depend on the periodic backfill

Evidence class: mongomock + forced interleavings + a fixed clock. Logical evidence that the conditional filters enforce the
rules - NOT proof of behaviour under real MongoDB concurrency, and nothing here talks to Lemon Squeezy, Google or a mailbox.
"""
import datetime as dt
import json
import sys

import pytest

from tests.otp_helpers import signup_with_code
from tests.test_guest_credits import (  # noqa: F401  (fixtures + helpers)
    EMAIL, G, OTHER, T0, UTC, acct, acct_usage, acct_used, clock, coll, credit_routes, e2e, finish, gauth, inv_ok, world,
)
from tests.test_guest_gate import (  # noqa: F401
    _Oauth, _capture_real_identity_check, auth, gapp, gg, gid_of, pipeline, post_search,
)
from tests.test_answer_persistence import env, GOOD  # noqa: F401
from tests.test_search_credit_routes import app, KEY1, KEY2  # noqa: F401
from tests.test_credit_carryover import (  # noqa: F401
    ALICE, D, NOW, FREE, carry, created_raw, deliver_c, entry_of, free_charge, plan, store, usage,
)
from tests.test_credit_carryover import inv_ok as carry_inv_ok  # noqa: F401
from tests.test_credit_corrections import FEB, Hook, b_guest, bworld  # noqa: F401
from tests.test_billing_hardening import cfg, db, wd  # noqa: F401

mongomock = pytest.importorskip("mongomock")
import billing  # noqa: E402
import guest_gate  # noqa: E402
import search_credits as sc  # noqa: E402

ANSWER = json.dumps(GOOD)


# ═════════════════ 1. a handed-over guest identity can not be reused ═════════════════
def test_email_signup_without_any_search_closes_the_identity_and_a_logout_does_not_reopen_it(credit_routes):
    a = credit_routes
    client = a["new_client"]()
    client.get("/")
    guest = gid_of(a["idx"], client)
    assert signup_with_code(client, "closed.user@example.com").json().get("ok") is True
    doc = a["gate_coll"].find_one({"_id": guest})
    assert doc["handover"]["to"] == "closed.user@example.com" and doc["handover"]["snapshot"] == 0 and doc["credits_used"] == 0
    assert a["db"].flintel_credit_usage.find_one({"owner_key": "closed.user@example.com"}) is None   # nothing to move: no account write
    client.get("/logout", follow_redirects=False)                                    # the guest cookie survives a logout
    assert gid_of(a["idx"], client) == guest
    denied = post_search(client)
    assert denied.status_code == 403 and denied.headers["X-Flintel-Denial"] == "guest-auth-required"
    assert a["gate_coll"].find_one({"_id": guest})["credits_used"] == 0 and a["state"]["enqueues"] == []
    again = post_search(client, KEY2)                                                # and again: still closed
    assert again.status_code == 403


def test_a_new_visitor_is_not_blocked_by_somebody_elses_closed_identity(credit_routes):
    a = credit_routes
    first = a["new_client"]()
    first.get("/")
    signup_with_code(first, "closed.two@example.com")
    first.get("/logout", follow_redirects=False)
    assert post_search(first).status_code == 403
    newcomer = a["new_client"]()                                                     # another browser: a new identity
    newcomer.get("/")
    assert gid_of(a["idx"], newcomer) != gid_of(a["idx"], first)
    assert post_search(newcomer, KEY2).status_code == 303


def test_logging_in_to_an_existing_account_does_not_close_the_guest_identity(credit_routes):
    a = credit_routes
    a["users"].insert_one({"email": "old@example.com", "password_hash": "h:correct-horse-1", "provider": "email"})
    client = a["new_client"]()
    client.get("/")
    guest = gid_of(a["idx"], client)
    r = client.post("/login", data={"email": "old@example.com", "password": "correct-horse-1"}, headers={"Accept": "application/json"})
    assert r.json()["ok"] is True
    client.get("/logout", follow_redirects=False)
    assert a["gate_coll"].find_one({"_id": guest}) is None                           # a login is not an account creation
    assert post_search(client).status_code == 303                                    # the visitor is an ordinary guest again


def test_google_signup_without_any_search_closes_the_identity_but_a_returning_google_user_does_not(gauth, gg, monkeypatch):
    monkeypatch.setattr(gauth["rt"], "oauth", _Oauth())
    client = gauth["client"]
    client.get("/")
    client.get("/auth/google/login", follow_redirects=False)
    client.get("/auth/google/callback", follow_redirects=False)
    doc = gauth["gate"].find_one({})
    assert doc["handover"]["to"] == "gg@example.com" and doc["handover"]["snapshot"] == 0
    client.get("/logout", follow_redirects=False)
    assert gg.default_gate().reserve(doc["_id"], "t-after", "t", "c", cost=1).kind == "auth_required"


def test_google_login_of_an_existing_account_leaves_a_never_searched_visitor_untouched(gauth, gg, monkeypatch):
    gauth["users"].insert_one({"email": "gg@example.com", "google_id": "g-123", "name": "G G", "provider": "google"})
    monkeypatch.setattr(gauth["rt"], "oauth", _Oauth())
    gauth["client"].get("/")
    gauth["client"].get("/auth/google/login", follow_redirects=False)
    gauth["client"].get("/auth/google/callback", follow_redirects=False)
    assert gauth["gate"].count_documents({}) == 0


def test_a_zero_credit_signup_with_a_running_search_keeps_the_hold_token_able_to_finish_exactly_once(world):
    assert world.gate.reserve(G, "run", "t", "c", cost=4).kind == "allow"            # 0 credits used, one search running
    out = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert out["applied_hold"] == 4 and acct_used(world) == 4
    assert world.gate.reserve(G, "run", "t", "c").kind == "allow"                    # the hold resumes
    assert world.gate.complete(G, "run", "c", "t") == "completed" and world.gg._settle_hold(G, "run") == "committed"
    assert world.gate.complete(G, "run", "c", "t") == "duplicate"
    assert world.gate.reserve(G, "again", "t", "c", cost=1).kind == "auth_required"  # and now the identity is closed
    assert acct_used(world) == 4 and world.gate.state(G)["credits_used"] == 4
    inv_ok(world)


def test_a_zero_credit_claim_racing_the_visitors_first_search_ends_in_a_hold_not_a_lost_search(world):
    """The claim sees no record, the visitor's first search creates it, the claim's insert collides and is retried."""
    real = world.coll

    class Proxy:
        fired = False

        def __getattr__(self, name):
            return getattr(real, name)

        def insert_one(self, doc, *a, **k):
            if "handover" in doc and not self.fired:
                self.fired = True
                assert guest_gate.GuestGate(real, clock=world.clock, credit_allowance=5, stale_seconds=900) \
                    .reserve(G, "first", "t", "c", cost=4).kind == "allow"
            return real.insert_one(doc, *a, **k)
    world.gate.coll = Proxy()
    out = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert out is not None and out["applied_hold"] == 4                              # the search is the HOLD
    assert world.gate.complete(G, "first", "c", "t") == "completed" and acct_used(world) == 4


def test_a_store_outage_at_signup_still_closes_the_identity_and_reconcile_applies_it_later(world, monkeypatch):
    assert finish(world.gate, G, "d", 5) == "completed"
    monkeypatch.setattr(world.gg, "_account_store", lambda: None)
    monkeypatch.setattr(sc, "enforcement_enabled", lambda: True)                     # enforcement on, store unavailable
    assert world.gg.handover_to_account(G, EMAIL, now=world.clock.t) is None
    assert world.gate.reserve(G, "n", "t", "c", cost=1).kind == "auth_required" and acct_used(world) == 0
    monkeypatch.setattr(world.gg, "_account_store", lambda: world.acct)
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) == "seeded" and acct_used(world) == 5
    assert world.gg.reconcile_handover(G, EMAIL, now=world.clock.t) == "seeded" and acct_used(world) == 5   # idempotent
    inv_ok(world)


def test_with_credit_enforcement_off_nothing_is_claimed_documented_limit(world, monkeypatch):
    """Kill switch: there is no account ledger, so the identity is NOT closed (an emergency mode)."""
    monkeypatch.setattr(world.gg, "_account_store", lambda: None)
    monkeypatch.setattr(sc, "enforcement_enabled", lambda: False)
    assert finish(world.gate, G, "d", 3) == "completed"
    assert world.gg.handover_to_account(G, EMAIL, now=world.clock.t) is None
    assert "handover" not in world.coll.find_one({"_id": G})


def test_seeding_nothing_writes_nothing(world):
    assert world.acct.seed_guest_handover(EMAIL, "email", G, "2026-10", 0) == {
        "status": "nothing_to_move", "applied_snapshot": 0, "applied_hold": 0}
    assert world.acct.seed_guest_handover(EMAIL, "email", G, "2026-03", 0)["status"] == "nothing_to_move"   # even for a past month
    assert acct_usage(world) == {} and not world.acct.has_pending_guest_handover(EMAIL)


# ═════════════════ 2. late handover accounting versus the first paid carryover ═════════════════
#
# INTENDED OUTCOME (documented in README): the first paid activation carries the account's OWN unused free credits of the
# activation month (its authoritative balance right before paying). A pending handover whose target month is over is
# resolved explicitly:
#   apply_late  records the guest usage in the ORIGINAL month's ledger (used + archived together; invariant holds)
#   waive       records that nothing is deducted
# Neither changes the activation month's balance, so the carryover is identical for both: an expired month's deduction can
# not be moved into the activation month without charging an unrelated period (a policy decision, see the report).
@pytest.mark.parametrize("outcome,feb_used", [("apply_late", 5), ("waive", None)])
def test_activation_after_an_expired_handover_is_resolved_carries_the_accounts_own_unused_credits(bworld, outcome, feb_used):
    wd = bworld.wd
    b_guest(bworld, 5, FEB)
    assert bworld.gate.claim_handover(G, ALICE, FEB)["period"] == "2026-02"
    free_charge(wd, 20)                                                              # the account's own March usage
    raw = created_raw()
    with pytest.raises(billing.WebhookBusy):
        deliver_c(wd, raw, NOW, credits=store(wd))                                   # pending: not silently skipped
    assert plan(wd) is None
    assert guest_gate.resolve_handover(G, ALICE, outcome, f"{outcome} test", now=NOW, store=store(wd)) in ("applied_late", "waived")
    assert deliver_c(wd, raw, NOW, credits=store(wd))["status"] == "applied"
    doc = usage(wd)
    assert doc["used_by_period"].get("2026-02") == feb_used and doc["used_by_period"][FREE] == 20
    assert carry(wd)["free_period"] == FREE and entry_of(wd)["bonus"] == 20 and entry_of(wd)["frozen_used"] == 20
    assert store(wd).status(ALICE)["limit"] == 420
    assert doc["guest_handover"]["resolution"]["outcome"] == outcome and "guest_handover_pending" not in doc
    assert len(doc["carryover_by_period"][plan(wd)["credit_period_key"]]) == 1       # one bonus entry only
    before = dict(usage(wd))
    other = "waive" if outcome == "apply_late" else "apply_late"
    for o in (outcome, other):                                                       # duplicate / conflicting resolutions
        assert guest_gate.resolve_handover(G, ALICE, o, "again", now=NOW, store=store(wd)) == "duplicate"
    assert deliver_c(wd, raw, NOW, credits=store(wd)) == {"status": "duplicate"}
    assert usage(wd) == before
    carry_inv_ok(wd)


def test_a_resolution_made_before_any_activation_attempt_gives_the_same_carryover(bworld):
    wd = bworld.wd
    b_guest(bworld, 5, FEB)
    bworld.gate.claim_handover(G, ALICE, FEB)
    free_charge(wd, 20)
    assert guest_gate.reconcile_handover(G, ALICE, now=NOW, store=store(wd)) == "pending_expired"      # e.g. a usage read
    assert guest_gate.resolve_handover(G, ALICE, "apply_late", "ops", now=NOW, store=store(wd)) == "applied_late"
    assert deliver_c(wd, created_raw(), NOW, credits=store(wd))["status"] == "applied"
    assert entry_of(wd)["bonus"] == 20 and usage(wd)["used_by_period"]["2026-02"] == 5
    carry_inv_ok(wd)


def test_apply_late_is_refused_for_a_month_frozen_by_a_carryover_but_waive_still_works(bworld):
    wd = bworld.wd
    b_guest(bworld, 5, FEB)
    bworld.gate.claim_handover(G, ALICE, FEB)
    guest_gate.reconcile_handover(G, ALICE, now=NOW, store=store(wd))
    wd.usage.update_one({"owner_key": ALICE}, {"$set": {"freeze.2026-02": {"id": "x" * 32, "at": NOW}}})
    assert guest_gate.resolve_handover(G, ALICE, "apply_late", "ops", now=NOW, store=store(wd)) == "period_frozen"
    assert store(wd).has_pending_guest_handover(ALICE) and "guest_handover" not in usage(wd)
    assert guest_gate.resolve_handover(G, ALICE, "waive", "ops", now=NOW, store=store(wd)) == "waived"


# ═════════════════ 3. counted-but-undelivered answers: discoverability and redelivery ═════════════════
def test_a_failed_delivery_requeues_the_turn_under_the_account_without_depending_on_the_backfill(world, monkeypatch):
    queued = []
    import turn_finisher
    monkeypatch.setattr(turn_finisher, "enqueue", lambda *a, **k: queued.append((a, k)) or True)
    g = world.gg
    assert world.gate.reserve(G, "run", "t", "chat9", cost=4).kind == "allow"
    claims = world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    assert claims["applied_hold"] == 4
    assert g.on_answer_ready(G, "chat9", "topic9", "run", ANSWER) == g.DELIVER
    g.on_delivery_failed(G, "run")
    assert queued and queued[0][0][:3] == ("chat9", EMAIL, "topic9")                 # the ACCOUNT owns the chat now
    g.on_delivery_failed(G, "run")                                                   # repeated failure: idempotent re-queue
    assert len(queued) == 2 and world.gate.state(G)["credits_used"] == 4 and acct_used(world) == 4
    assert world.coll.count_documents({"handover.delivery_failures": {"$exists": True, "$ne": []}}) == 1


def test_a_failure_of_an_older_token_is_recorded_for_operators_but_not_guessed_into_the_queue(world, monkeypatch):
    queued = []
    import turn_finisher
    monkeypatch.setattr(turn_finisher, "enqueue", lambda *a, **k: queued.append(a) or True)
    g = world.gg
    assert world.gate.reserve(G, "t1", "t", "c1", cost=2).kind == "allow"
    assert g.on_answer_ready(G, "c1", "tp1", "t1", ANSWER) == g.DELIVER
    world.coll.update_one({"_id": G}, {"$set": {"completed_tokens": ["t1", "t2"]}})   # a later completion exists
    world.gg.handover_to_account(G, EMAIL, now=world.clock.t)
    g.on_delivery_failed(G, "t1")
    assert queued == [] and len(world.coll.find_one({"_id": G})["handover"]["delivery_failures"]) == 1


def test_the_real_pipeline_keeps_the_count_when_the_save_fails_after_the_handover_and_redelivers_without_recounting(pipeline, gg, monkeypatch):
    lg, idx, chats, coll = pipeline["lg"], pipeline["idx"], pipeline["chats"], pipeline["coll"]
    q = "Find users looking for AI jobs in Pakistan"
    idx.add_search_to_chat("c1", "guest-1", q, lg.normalize_topic_key(q), ["kw"], "reddit")
    msg = chats.find_one({"chat_id": "c1", "owner_key": "guest-1"})["messages"][-1]
    msg["requested_at"], msg["request_id"] = dt.datetime(2026, 10, 20, 12, 0, 0), "tok-r"
    gate = gg.GuestGate(coll)
    assert gate.reserve("guest-1", "tok-r", msg["topic_key"], "c1").kind == "allow"
    assert gate.claim_handover("guest-1", "new.user@example.test")["hold"]["token"] == "tok-r"     # the signup happens mid-search
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps(GOOD))
    real_save, boom = idx.save_claude_answer_if_unset, {"n": 2}

    def flaky(*a, **k):
        if boom["n"] > 0:
            boom["n"] -= 1
            raise RuntimeError("database write failed")
        return real_save(*a, **k)
    monkeypatch.setattr(idx, "save_claude_answer_if_unset", flaky)
    queued = []
    import turn_finisher
    monkeypatch.setattr(turn_finisher, "enqueue", lambda *a, **k: queued.append(a) or True)
    for attempt in (1, 2):                                                           # the save fails twice
        idx._complete_message_answer_and_results("c1", "guest-1", dict(msg, claude_answer=None), pipeline["post"])
        doc = coll.find_one({"_id": "guest-1"})
        assert doc["completed"] == 1 and "tok-r" in doc["completed_tokens"]          # the count is KEPT every time
        assert len(doc["handover"]["delivery_failures"]) == attempt
        assert not chats.find_one({"chat_id": "c1", "owner_key": "guest-1"})["messages"][-1].get("claude_answer")
    assert len(queued) == 2 and queued[0][1] == "new.user@example.test"
    credits = doc["credits_used"]
    idx._complete_message_answer_and_results("c1", "guest-1", dict(msg, claude_answer=None), pipeline["post"])   # the retry
    doc = coll.find_one({"_id": "guest-1"})
    assert doc["completed"] == 1 and doc["credits_used"] == credits and len(doc["handover"]["delivery_failures"]) == 2
    assert chats.find_one({"chat_id": "c1", "owner_key": "guest-1"})["messages"][-1].get("claude_answer")   # delivered, once
    idx._complete_message_answer_and_results("c1", "guest-1", dict(msg, claude_answer=None), pipeline["post"])   # a duplicate
    assert coll.find_one({"_id": "guest-1"})["completed"] == 1
