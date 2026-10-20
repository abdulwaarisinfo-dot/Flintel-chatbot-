"""
tests/test_billing_hardening.py — hardening of the Lemon Squeezy billing: invoice identity, billing-anchored
credit periods, equal-timestamp ordering, refunds (full / partial / stale), plan-index failure, fresh portal links.

Same rules as test_billing_lemonsqueezy.py: EVERY test is "unit (mocked provider), not live": mongomock, synthetic
payloads built here, a fixed clock, injected HTTP callables, made-up secrets. No network, no real account, no charge.
What these tests cannot show is that Lemon Squeezy's real payloads / refund representation match the documented
shapes used here — that needs a test-mode run (README "Billing").
"""
import json
import logging
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from tests.test_answer_persistence import env  # noqa: F401  (fixture: stubbed app modules)
from tests.test_billing_lemonsqueezy import (  # noqa: F401  (fixtures: cfg, db, app)
    ALICE, BOB, NOW, STORE, V_BIZ, V_STD, Resp, app, body_json, cfg, checkout, db, deliver, iso, make_request,
    plan_record, run_async, sub_event, webhook,
)

mongomock = pytest.importorskip("mongomock")
import billing  # noqa: E402
import search_credits as sc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
D = timedelta
T1 = NOW + D(days=30)                # the first renewal
KEY0, KEY1 = "2026-04-09", "2026-05-09"      # billing-period keys: the UTC date each paid period ends


# ── builders ─────────────────────────────────────────────────────────────────
def inv(name="subscription_payment_success", inv_id="77", sid="9001", status="paid", reason="renewal", created=NOW,
        updated=None, total=1900, refunded_amount=None, store=STORE, with_created=True, test=False):
    attrs = {"store_id": int(store), "subscription_id": int(sid), "status": status, "billing_reason": reason,
             "total": total, "updated_at": iso(updated or created)}
    if with_created:
        attrs["created_at"] = iso(created)
    if refunded_amount is not None:
        attrs.update(refunded_amount=refunded_amount, refunded=refunded_amount > 0)
    return json.dumps({"meta": {"event_name": name, "test_mode": test},
                       "data": {"type": "subscription-invoices", "id": inv_id, "attributes": attrs}}).encode()


class Fresh:
    """Injected GET /v1/subscriptions/{id}: answers with the subscription object of a sub_event payload."""

    def __init__(self, raw):
        self.data, self.calls, self.urls = json.loads(raw)["data"], 0, []

    def __call__(self, url, headers):
        self.calls += 1
        self.urls.append(url)
        return Resp(200, {"data": self.data})


@pytest.fixture
def wd(db):
    """db plus a credit-usage collection, so the real CreditStore can be driven."""
    m = mongomock.MongoClient()["usage"]
    m.u.create_index("owner_key", unique=True)
    db.usage, db.chats, db.jobs = m.u, m.chats, m.jobs
    return db


def store_at(db, now):
    return sc.CreditStore(db.usage, db.chats, db.jobs, lambda *a, **k: True, clock=lambda: now, enabled=lambda: True,
                          limit=lambda: 40, plans=db.plans)


def spend(db, now, rid, cost=100, owner=ALICE):
    return store_at(db, now).reserve(owner, "email", rid, "topic", "chat1", True, cost=cost)


def spend_all(db, now, cost=100, prefix="r", owner=ALICE):
    """How many `cost`-credit searches fit before the allowance is exhausted."""
    n = 0
    while isinstance(spend(db, now, f"{prefix}{n}", cost, owner), sc.Ticket) and n < 40:
        n += 1
    return n


def used(db, owner=ALICE):
    return dict((db.usage.find_one({"owner_key": owner}) or {}).get("used_by_period") or {})


def subscribe(db, sid="9001", owner=ALICE, variant=V_STD, claim="standard", at=NOW, inv_id="70", renews=None):
    """subscription_created + the matching initial paid invoice (the funding invoice of the first period)."""
    raw = sub_event("subscription_created", sid=sid, owner=owner, variant=variant, claim_plan=claim, updated=at,
                    renews=renews or at + D(days=30))
    deliver(db, raw, now=at)
    out = deliver(db, inv(inv_id=inv_id, sid=sid, reason="initial", created=at), now=at, get=Fresh(raw))
    return raw, out


def renew(db, at=T1, inv_id="77", sid="9001", owner=ALICE, variant=V_STD, claim="standard", name="subscription_payment_success",
          get=None, updated_sub=None):
    """The provider's view after a renewal payment (renews_at moved a month) + the payment event for `inv_id`."""
    sub_raw = sub_event("subscription_updated", sid=sid, owner=owner, variant=variant, claim_plan=claim,
                        updated=updated_sub or at, renews=at + D(days=30))
    get = get or Fresh(sub_raw)
    return deliver(db, inv(name, inv_id=inv_id, sid=sid, created=at), now=at, get=get), get


def sub_doc(db, sid="9001"):
    return db.subs.find_one({"_id": sid})


# ═══════════════ duplicate / concurrent / success-then-recovered deliveries ═══════════════
def test_success_then_recovered_for_the_same_invoice_is_applied_once(cfg, wd):
    subscribe(wd)
    first, get = renew(wd)
    assert first["status"] == "applied"
    rev, calls, plan_rev = sub_doc(wd)["rev"], get.calls, plan_record(wd)["billing_rev"]
    second = deliver(wd, inv("subscription_payment_recovered", inv_id="77", created=T1), now=T1, get=get)
    assert second == {"status": "duplicate_payment"}
    assert get.calls == calls                                   # not even re-fetched
    assert sub_doc(wd)["rev"] == rev and plan_record(wd)["billing_rev"] == plan_rev
    assert wd.events.find_one({"_id": "payment:9001:77"})["state"] == "done"


def test_the_same_invoice_in_differently_worded_events_is_one_payment(cfg, wd):
    subscribe(wd)
    renew(wd)
    get = Fresh(sub_event("subscription_updated", updated=T1, renews=T1 + D(days=30)))
    for updated in (T1 + D(seconds=5), T1 + D(minutes=9)):              # different bodies, same invoice
        for name in ("subscription_payment_success", "subscription_payment_recovered"):
            assert deliver(wd, inv(name, inv_id="77", created=T1, updated=updated), now=T1, get=get) == {"status": "duplicate_payment"}
    assert get.calls == 0


def test_a_byte_identical_redelivery_is_a_duplicate(cfg, wd):
    subscribe(wd)
    raw = inv(inv_id="77", created=T1)
    get = Fresh(sub_event("subscription_updated", updated=T1, renews=T1 + D(days=30)))
    assert deliver(wd, raw, now=T1, get=get)["status"] == "applied"
    assert deliver(wd, raw, now=T1, get=get) == {"status": "duplicate"}
    assert get.calls == 1


def test_a_different_invoice_is_a_different_payment(cfg, wd):
    subscribe(wd)
    renew(wd, inv_id="77")
    out, _ = renew(wd, at=T1 + D(days=30), inv_id="78")
    assert out["status"] == "applied" and sub_doc(wd)["period_key"] == "2026-06-08"


def test_concurrent_success_and_recovered_for_one_invoice_apply_once(cfg, wd):
    subscribe(wd)
    started, release = threading.Event(), threading.Event()
    sub_raw = sub_event("subscription_updated", updated=T1, renews=T1 + D(days=30))
    inner = Fresh(sub_raw)

    def slow_get(url, headers):
        started.set()
        assert release.wait(10)
        return inner(url, headers)
    result = {}

    def first():
        result["a"] = deliver(wd, inv("subscription_payment_success", created=T1), now=T1, get=slow_get)
    worker = threading.Thread(target=first)
    worker.start()
    assert started.wait(10)
    with pytest.raises(billing.WebhookBusy):                            # the same invoice is being applied right now
        deliver(wd, inv("subscription_payment_recovered", created=T1), now=T1, get=inner)
    release.set()
    worker.join(10)
    assert result["a"]["status"] == "applied" and inner.calls == 1
    assert deliver(wd, inv("subscription_payment_recovered", created=T1), now=T1, get=inner) == {"status": "duplicate_payment"}
    assert inner.calls == 1 and sub_doc(wd)["period_key"] == KEY1


def test_concurrent_identical_bodies_apply_once(cfg, wd):
    subscribe(wd)
    started, release = threading.Event(), threading.Event()
    inner = Fresh(sub_event("subscription_updated", updated=T1, renews=T1 + D(days=30)))

    def slow_get(url, headers):
        started.set()
        assert release.wait(10)
        return inner(url, headers)
    raw = inv(created=T1)
    worker = threading.Thread(target=lambda: deliver(wd, raw, now=T1, get=slow_get))
    worker.start()
    assert started.wait(10)
    with pytest.raises(billing.WebhookBusy):
        deliver(wd, raw, now=T1, get=inner)
    release.set()
    worker.join(10)
    assert deliver(wd, raw, now=T1, get=inner) == {"status": "duplicate"} and inner.calls == 1


def test_one_billing_period_grants_its_allowance_exactly_once(cfg, wd):
    subscribe(wd)
    assert spend_all(wd, NOW + D(days=3)) == 4                          # the first period's 400
    assert spend_all(wd, T1 - D(minutes=1), prefix="x") == 0            # still the first period
    get = renew(wd)[1]
    deliver(wd, inv("subscription_payment_recovered", created=T1), now=T1, get=get)
    deliver(wd, inv("subscription_payment_success", created=T1), now=T1, get=get)
    after = T1 + D(hours=1)
    assert spend_all(wd, after, prefix="n") == 4                        # ONE fresh allowance, not 800
    for name in ("subscription_payment_recovered", "subscription_payment_success"):   # replays reopen nothing
        deliver(wd, inv(name, created=T1), now=after, get=get)
    assert spend_all(wd, after, prefix="z") == 0
    assert used(wd) == {KEY0: 400, KEY1: 400}


# ═══════════════ invoices that are not paid / not ready ═══════════════
@pytest.mark.parametrize("status", ["pending", "void", "refunded", "", None, "PAID"])
def test_an_invoice_that_is_not_paid_is_rejected_without_state(cfg, wd, status):
    subscribe(wd)
    before = (sub_doc(wd), plan_record(wd), wd.events.count_documents({}))
    get = Fresh(sub_event("subscription_updated", updated=T1, renews=T1 + D(days=30)))
    for name in ("subscription_payment_success", "subscription_payment_recovered"):
        out = deliver(wd, inv(name, status=status, created=T1), now=T1, get=get)
        assert out == {"status": "ignored", "reason": "invoice not paid"}
    assert get.calls == 0 and sub_doc(wd)["period_key"] == KEY0
    assert (sub_doc(wd), plan_record(wd)) == before[:2]
    assert wd.events.find_one({"_id": "payment:9001:77"}) is None
    assert store_at(wd, T1 + D(hours=1)).limit_for(ALICE) == 40         # the first period ran out; nothing extended it


def test_a_paid_funding_invoice_without_created_at_is_ignored(cfg, wd):
    subscribe(wd)
    get = Fresh(sub_event("subscription_updated", updated=T1, renews=T1 + D(days=30)))
    out = deliver(wd, inv(created=T1, with_created=False), now=T1, get=get)
    assert out["status"] == "ignored" and get.calls == 0 and sub_doc(wd)["period_key"] == KEY0


def test_a_payment_for_a_subscription_not_recorded_yet_is_retried_not_lost(cfg, wd):
    raw = sub_event("subscription_created", updated=NOW, renews=NOW + D(days=30))
    early = inv(inv_id="70", reason="initial", created=NOW)
    with pytest.raises(billing.WebhookBusy):                            # the invoice event overtook subscription_created
        deliver(wd, early, now=NOW, get=Fresh(raw))
    assert wd.subs.count_documents({}) == 0 and plan_record(wd) is None
    deliver(wd, raw, now=NOW)
    assert deliver(wd, early, now=NOW, get=Fresh(raw))["status"] == "applied"      # the retry
    assert sub_doc(wd)["period_invoice_id"] == "70"


def test_payment_failed_and_unknown_subscription_for_non_paid_events(cfg, wd):
    out = deliver(wd, inv("subscription_payment_failed", sid="123"), now=NOW)
    assert out == {"status": "ignored", "reason": "unknown subscription"}
    subscribe(wd)
    deliver(wd, inv("subscription_payment_failed", inv_id="79", status="pending", created=T1), now=T1)
    assert plan_record(wd)["active_until"].replace(tzinfo=None) == (NOW + D(days=30)).replace(tzinfo=None)


# ═══════════════ equal-timestamp conflicting states ═══════════════
def evt(status, at=NOW + D(seconds=7), name="subscription_updated", **kw):
    kw.setdefault("renews", NOW + D(days=30))
    if status in ("cancelled", "expired"):
        kw.setdefault("ends", NOW + D(days=30))
    return sub_event(name, status=status, updated=at, cancelled=(status == "cancelled"), **kw)


@pytest.mark.parametrize("first,second", [("cancelled", "active"), ("expired", "active"), ("expired", "cancelled"),
                                          ("past_due", "active"), ("cancelled", "on_trial")])
def test_a_same_second_event_never_moves_a_subscription_backwards(cfg, wd, first, second):
    subscribe(wd)
    deliver(wd, evt(first))
    snapshot = sub_doc(wd)
    out = deliver(wd, evt(second, renews=NOW + D(days=60)))
    assert out["status"] == "stale"
    doc = sub_doc(wd)
    assert doc["status"] == first and doc["entitled_until"] == snapshot["entitled_until"] and doc["rev"] == snapshot["rev"]
    assert plan_record(wd)["active_until"].replace(tzinfo=None) == snapshot["entitled_until"].replace(tzinfo=None) or first == "expired"


@pytest.mark.parametrize("first,second", [("active", "cancelled"), ("active", "expired"), ("cancelled", "expired"),
                                          ("active", "past_due")])
def test_a_same_second_event_may_move_a_subscription_forwards(cfg, wd, first, second):
    subscribe(wd)
    deliver(wd, evt(first))
    assert deliver(wd, evt(second))["status"] == "applied" and sub_doc(wd)["status"] == second


def test_same_second_events_of_the_same_state_are_both_applied(cfg, wd):
    subscribe(wd)
    deliver(wd, evt("active", renews=NOW + D(days=30)))
    assert deliver(wd, evt("active", renews=NOW + D(days=31)))["status"] == "applied"


def test_a_strictly_newer_resume_after_cancel_still_works(cfg, wd):
    subscribe(wd)
    deliver(wd, evt("cancelled"))
    out = deliver(wd, evt("active", at=NOW + D(seconds=8), name="subscription_resumed"))
    assert out["status"] == "applied" and sub_doc(wd)["status"] == "active" and plan_record(wd)["plan"] == "standard"


# ═══════════════ billing-period boundaries, renewal, upgrade, downgrade, expiry ═══════════════
def test_a_new_subscription_gets_its_own_billing_period(cfg, wd):
    subscribe(wd)
    rec = plan_record(wd)
    assert (sub_doc(wd)["period_key"], rec["credit_period_key"]) == (KEY0, KEY0)
    assert rec["credit_period_ends"].replace(tzinfo=None) == (NOW + D(days=30)).replace(tzinfo=None)


def test_the_allowance_does_not_reset_at_the_calendar_month_boundary(cfg, wd):
    subscribe(wd, at=NOW + D(days=14), renews=NOW + D(days=44))        # Mar 24 -> Apr 23
    assert spend_all(wd, NOW + D(days=15)) == 4
    for moment in (NOW + D(days=21), NOW + D(days=22), NOW + D(days=43)):     # Mar 31, Apr 1, the last day
        out = spend(wd, moment, "again")
        assert isinstance(out, sc.Decision) and out.kind == "exhausted"
        assert out.resets_at.replace(tzinfo=None) == (NOW + D(days=44)).replace(tzinfo=None)    # the period end, not Apr 1


def test_free_usage_before_subscribing_does_not_reduce_the_paid_allowance(cfg, wd):
    assert spend_all(wd, NOW, cost=10) == 4                            # the 40 free credits of March
    assert used(wd) == {"2026-03": 40}
    subscribe(wd, at=NOW + D(hours=1))
    assert spend_all(wd, NOW + D(hours=2), prefix="p") == 4            # full 400, unaffected by the free usage
    assert used(wd) == {"2026-03": 40, KEY0: 400}                      # nothing was moved, rewritten or erased


def test_a_renewal_opens_one_new_counter_and_leaves_the_old_one_alone(cfg, wd):
    subscribe(wd)
    spend_all(wd, NOW + D(days=2))
    renew(wd)
    assert sub_doc(wd)["period_key"] == KEY1 and plan_record(wd)["credit_period_key"] == KEY1
    assert spend_all(wd, T1 + D(hours=1), prefix="b") == 4
    assert used(wd) == {KEY0: 400, KEY1: 400}
    assert sc.check_invariant(wd.usage.find_one({"owner_key": ALICE})) == []


def test_the_boundary_second_belongs_to_the_old_period_until_the_renewal_is_confirmed(cfg, wd):
    subscribe(wd)
    spend_all(wd, NOW + D(days=2))
    assert isinstance(spend(wd, T1 - D(seconds=1), "late"), sc.Decision)
    assert store_at(wd, T1).limit_for(ALICE) == 40                      # unpaid past the window: free plan, never a free extension
    renew(wd)
    assert isinstance(spend(wd, T1 + D(seconds=1), "fresh"), sc.Ticket)


def test_a_renewal_seen_only_through_subscription_updated_still_opens_the_period(cfg, wd):
    subscribe(wd)
    spend_all(wd, NOW + D(days=2))
    deliver(wd, sub_event("subscription_updated", updated=T1, renews=T1 + D(days=30)), now=T1)     # payment event lost
    assert sub_doc(wd)["period_key"] == KEY1
    assert spend_all(wd, T1 + D(hours=1), prefix="n") == 4
    deliver(wd, sub_event("subscription_updated", updated=T1 + D(minutes=1), renews=T1 + D(days=30)), now=T1)
    renew(wd)                                                           # the payment event arrives late: nothing opens twice
    assert spend_all(wd, T1 + D(hours=2), prefix="q") == 0 and used(wd) == {KEY0: 400, KEY1: 400}


def test_small_renews_at_drift_inside_the_period_keeps_the_counter(cfg, wd):
    subscribe(wd)
    spend_all(wd, NOW + D(days=2), cost=100)
    deliver(wd, sub_event("subscription_updated", updated=NOW + D(days=3), renews=NOW + D(days=33)), now=NOW + D(days=3))
    assert sub_doc(wd)["period_key"] == KEY0
    assert spend_all(wd, NOW + D(days=4), prefix="m") == 0 and used(wd) == {KEY0: 400}


def test_an_upgrade_keeps_the_usage_of_the_period(cfg, wd):
    subscribe(wd)
    assert spend_all(wd, NOW + D(days=1), cost=100) == 4               # 400 of 400 used
    up = sub_event("subscription_updated", variant=V_BIZ, claim_plan="business", updated=NOW + D(days=2), renews=NOW + D(days=30))
    deliver(wd, up, now=NOW + D(days=2))
    rec = plan_record(wd)
    assert (rec["plan"], rec["credit_period_key"]) == ("business", KEY0)
    assert spend_all(wd, NOW + D(days=3), prefix="u") == 11             # 1500 - 400 used = 1100 left; usage was not erased
    assert used(wd) == {KEY0: 1500}


def test_a_downgrade_grants_nothing_and_erases_nothing(cfg, wd):
    subscribe(wd, variant=V_BIZ, claim="business")
    assert spend_all(wd, NOW + D(days=1), cost=100) == 15
    spend(wd, NOW + D(days=1), "odd", cost=100)                         # (the 16th is denied: exactly 1500)
    down = sub_event("subscription_updated", variant=V_STD, updated=NOW + D(days=2), renews=NOW + D(days=30))
    deliver(wd, down, now=NOW + D(days=2))
    assert plan_record(wd)["plan"] == "standard" and store_at(wd, NOW + D(days=3)).limit_for(ALICE) == 400
    assert isinstance(spend(wd, NOW + D(days=3), "more", cost=1), sc.Decision)      # 1500 used > 400: denied, not reset
    assert used(wd) == {KEY0: 1500}
    renew(wd, variant=V_STD)                                            # the next paid period: a fresh 400
    assert spend_all(wd, T1 + D(hours=1), prefix="d") == 4


def test_expiry_returns_to_the_calendar_month_and_touches_no_usage(cfg, wd):
    subscribe(wd)
    spend_all(wd, NOW + D(days=1))
    end = NOW + D(days=5)
    deliver(wd, sub_event("subscription_expired", status="expired", updated=end, ends=end, renews=NOW + D(days=30)), now=end)
    rec = plan_record(wd)
    assert "credit_period_key" not in rec and rec["active_until"].replace(tzinfo=None) <= end.replace(tzinfo=None)
    assert store_at(wd, end + D(hours=1)).limit_for(ALICE) == 40
    assert spend_all(wd, end + D(hours=1), cost=10, prefix="f") == 4          # the free allowance of the calendar month
    assert used(wd) == {KEY0: 400, "2026-03": 40}


def test_cancellation_keeps_the_period_and_its_usage_until_it_ends(cfg, wd):
    subscribe(wd)
    spend_all(wd, NOW + D(days=1))
    deliver(wd, evt("cancelled", at=NOW + D(days=2), ends=NOW + D(days=30)), now=NOW + D(days=2))
    rec = plan_record(wd)
    assert (rec["plan"], rec["credit_period_key"]) == ("standard", KEY0)
    assert spend_all(wd, NOW + D(days=20), prefix="c") == 0            # same counter, still 400/400
    assert store_at(wd, T1 + D(seconds=1)).limit_for(ALICE) == 40      # after the end: free again


def test_overlapping_subscriptions_count_in_their_own_periods(cfg, wd):
    subscribe(wd, sid="1", inv_id="11")                                                   # Standard, ends Apr 9
    subscribe(wd, sid="2", variant=V_BIZ, claim="business", at=NOW + D(days=1), inv_id="22")   # Business, ends Apr 10
    rec = plan_record(wd)
    assert (rec["plan"], rec["credit_period_key"]) == ("business", "2026-04-10")
    assert spend_all(wd, NOW + D(days=2), cost=100) == 15
    # an OLD event of the Standard subscription cannot displace the Business entitlement
    stale = sub_event("subscription_updated", sid="1", status="cancelled", cancelled=True, ends=NOW + D(days=30),
                      updated=NOW - D(days=1), renews=NOW + D(days=30))
    assert deliver(wd, stale, now=NOW + D(days=2))["status"] == "stale"
    assert plan_record(wd)["plan"] == "business"
    # the Business subscription ends: the still-paid Standard one carries on with ITS OWN counter
    end = NOW + D(days=5)
    deliver(wd, sub_event("subscription_expired", sid="2", status="expired", variant=V_BIZ, claim_plan="business",
                          updated=end, ends=end, renews=NOW + D(days=31)), now=end)
    rec = plan_record(wd)
    assert (rec["plan"], rec["credit_period_key"]) == ("standard", KEY0)
    assert spend_all(wd, end + D(hours=1), prefix="s") == 4
    assert used(wd) == {"2026-04-10": 1500, KEY0: 400}


def test_overlapping_subscriptions_of_different_owners_are_independent(cfg, wd):
    subscribe(wd, sid="1", inv_id="11")
    subscribe(wd, sid="2", owner=BOB, inv_id="22")
    deliver(wd, evt("expired", sid="1", ends=NOW + D(seconds=7), renews=NOW + D(days=30)))
    assert store_at(wd, NOW + D(days=1)).limit_for(BOB) == 400 and store_at(wd, NOW + D(days=1)).limit_for(ALICE) == 40


def test_status_and_the_denial_report_the_billing_period_end(cfg, wd):
    subscribe(wd)
    spend_all(wd, NOW + D(days=1))
    status = store_at(wd, NOW + D(days=1)).status(ALICE, "email")      # (a later status() would recover the unfinished test reservations)
    assert (status["plan"], status["limit"], status["period"], status["used"], status["remaining"]) == ("standard", 400, KEY0, 400, 0)
    assert status["resets_at"].startswith("2026-04-09T12:00")
    free = store_at(wd, NOW).status(BOB, "email")
    assert (free["plan"], free["limit"], free["period"]) == ("free", 40, "2026-03") and free["resets_at"].startswith("2026-04-01")


def test_a_manual_activate_plan_record_still_uses_the_calendar_month(cfg, wd):
    sc.activate_plan(wd.plans, ALICE, "standard", NOW + D(days=30), "manual-1", now=NOW)
    status = store_at(wd, NOW).status(ALICE, "email")
    assert status["period"] == "2026-03" and status["limit"] == 400 and status["resets_at"].startswith("2026-04-01")


def test_period_keys_cannot_be_confused_with_calendar_months():
    assert sc._valid_period_key("2026-04-09") and not any(sc._valid_period_key(k) for k in
                                                         ("2026-04", "2026-4-9", "x2026-04-09", None, 20260409, "2026-04-09 "))
    assert "2026-04-09" > sc.previous_period("2026-04")                  # sorts after the calendar cutoff: _prune keeps it


def test_set_plan_entitlement_period_arguments(cfg, wd):
    args = (wd.plans, ALICE, "standard", NOW, NOW + D(days=30), "ref1", 1)
    for bad in ({"credit_period_key": KEY0}, {"credit_period_ends": NOW}, {"credit_period_key": "2026-04", "credit_period_ends": NOW},
                {"credit_period_key": KEY0, "credit_period_ends": "2026-04-09"}):
        with pytest.raises(ValueError):
            sc.set_plan_entitlement(*args, now=NOW, **bad)
    assert wd.plans.count_documents({}) == 0
    assert sc.set_plan_entitlement(*args, now=NOW, credit_period_key=KEY0, credit_period_ends=NOW + D(days=30)) == "applied"
    assert plan_record(wd)["credit_period_key"] == KEY0
    assert sc.set_plan_entitlement(wd.plans, ALICE, "standard", NOW, NOW + D(days=31), "ref2", 2, now=NOW) == "applied"
    assert "credit_period_key" not in plan_record(wd)                   # a write without a period clears the old one


# ═══════════════ refunds (policy R2) ═══════════════
def test_a_full_refund_of_the_funding_invoice_revokes_that_period(cfg, wd):
    subscribe(wd)
    at = NOW + D(hours=2)
    out = deliver(wd, inv("subscription_payment_refunded", inv_id="70", reason="initial", created=NOW, status="refunded",
                          total=1900, refunded_amount=1900, updated=at), now=at)
    assert out["refund"] == "revoked"
    doc = sub_doc(wd)
    assert doc["refunded_invoices"] == {"70": "full"} and doc["revoked_period_key"] == KEY0
    assert doc["entitled_until"].replace(tzinfo=None) <= at.replace(tzinfo=None)
    assert store_at(wd, at + D(seconds=1)).limit_for(ALICE) == 40 and billing.has_live_subscription(wd.subs, ALICE, at + D(seconds=1)) is None
    # later "active" updates of the same refunded period cannot bring it back
    deliver(wd, sub_event("subscription_updated", updated=at + D(hours=1), renews=NOW + D(days=30)), now=at + D(hours=1))
    assert store_at(wd, at + D(hours=2)).limit_for(ALICE) == 40
    assert billing.status_for_owner(wd.subs, wd.plans, ALICE, at + D(hours=2))["revoked"] is True


def test_a_separately_paid_next_period_is_not_affected_by_the_old_revocation(cfg, wd):
    subscribe(wd)
    at = NOW + D(hours=2)
    deliver(wd, inv("subscription_payment_refunded", inv_id="70", reason="initial", created=NOW, status="refunded", total=1900,
                    refunded_amount=1900, updated=at), now=at)
    out, _ = renew(wd, inv_id="77")
    assert out["status"] == "applied"
    assert (sub_doc(wd)["period_key"], sub_doc(wd)["period_invoice_id"]) == (KEY1, "77")
    assert store_at(wd, T1 + D(hours=1)).limit_for(ALICE) == 400
    assert spend_all(wd, T1 + D(hours=1), prefix="r") == 4


def test_a_partial_refund_is_recorded_and_revokes_nothing(cfg, wd):
    subscribe(wd)
    at = NOW + D(hours=2)
    before = sub_doc(wd)["entitled_until"]
    out = deliver(wd, inv("subscription_payment_refunded", inv_id="70", reason="initial", created=NOW, total=1900,
                          refunded_amount=500, updated=at), now=at)
    assert out["refund"] == "partial"
    doc = sub_doc(wd)
    assert doc["refunded_invoices"] == {"70": "partial"} and doc.get("revoked_period_key") is None and doc["entitled_until"] == before
    assert store_at(wd, at + D(days=1)).limit_for(ALICE) == 400
    # a later FULL refund of the same invoice does revoke, and a re-sent partial never downgrades it
    later = at + D(hours=1)
    assert deliver(wd, inv("subscription_payment_refunded", inv_id="70", reason="initial", created=NOW, status="refunded", total=1900,
                           refunded_amount=1900, updated=later), now=later)["refund"] == "revoked"
    deliver(wd, inv("subscription_payment_refunded", inv_id="70", reason="initial", created=NOW, total=1900, refunded_amount=500,
                    updated=later + D(minutes=1)), now=later + D(minutes=1))
    assert sub_doc(wd)["refunded_invoices"] == {"70": "full"} and store_at(wd, later + D(hours=1)).limit_for(ALICE) == 40


def test_a_stale_refund_cannot_revoke_a_newer_paid_period(cfg, wd):
    subscribe(wd)                                                       # period 1, invoice 70
    renew(wd, inv_id="77")                                              # period 2, invoice 77
    at = T1 + D(hours=1)
    out = deliver(wd, inv("subscription_payment_refunded", inv_id="70", reason="initial", created=NOW, status="refunded", total=1900,
                          refunded_amount=1900, updated=at), now=at)
    assert out["refund"] == "stale_refund"
    doc = sub_doc(wd)
    assert doc["refunded_invoices"] == {"70": "full"} and doc.get("revoked_period_key") is None
    assert store_at(wd, at + D(days=1)).limit_for(ALICE) == 400 and spend_all(wd, at + D(days=1)) == 4
    # ...while a full refund of the invoice that DOES fund period 2 revokes period 2 only
    later = at + D(days=2)
    assert deliver(wd, inv("subscription_payment_refunded", inv_id="77", created=T1, status="refunded", total=1900, refunded_amount=1900,
                           updated=later), now=later)["refund"] == "revoked"
    assert sub_doc(wd)["revoked_period_key"] == KEY1 and store_at(wd, later + D(seconds=1)).limit_for(ALICE) == 40


def test_a_refund_for_another_subscription_never_touches_this_one(cfg, wd, caplog):
    subscribe(wd, sid="1", inv_id="11")
    subscribe(wd, sid="2", owner=BOB, inv_id="22", at=NOW + D(minutes=5))
    at = NOW + D(hours=1)
    with caplog.at_level(logging.ERROR, logger="flintel.billing"):
        # Alice's invoice id, sent against Bob's subscription: not Bob's funding invoice
        out = deliver(wd, inv("subscription_payment_refunded", inv_id="11", sid="2", reason="initial", created=NOW, status="refunded",
                              total=1900, refunded_amount=1900, updated=at), now=at)
    assert out["refund"] == "stale_refund"
    assert store_at(wd, at).limit_for(ALICE) == 400 and store_at(wd, at).limit_for(BOB) == 400
    assert sub_doc(wd, "1").get("refunded_invoices") is None
    # an unknown subscription is acknowledged and changes nothing
    snap = (list(wd.subs.find()), list(wd.plans.find()))
    assert deliver(wd, inv("subscription_payment_refunded", inv_id="11", sid="404", status="refunded", total=1900,
                           refunded_amount=1900), now=at)["status"] == "ignored"
    assert (list(wd.subs.find()), list(wd.plans.find())) == snap


def test_a_refund_that_cannot_be_matched_fails_closed_and_alerts(cfg, wd, caplog):
    subscribe(wd)
    with caplog.at_level(logging.ERROR, logger="flintel.billing"):
        # newer than any invoice we know funds the period: lost payment event? never guess
        out = deliver(wd, inv("subscription_payment_refunded", inv_id="99", created=T1, status="refunded", total=1900,
                              refunded_amount=1900), now=T1 - D(days=1))
    assert out["refund"] == "unmatched_refund" and sub_doc(wd).get("revoked_period_key") is None and sub_doc(wd)["refund_review_at"]
    assert "cannot be matched" in caplog.text and ALICE not in caplog.text
    assert store_at(wd, NOW + D(days=1)).limit_for(ALICE) == 400


def test_a_refund_that_arrives_before_its_payment_event_closes_the_period_when_the_payment_is_seen(cfg, wd, caplog):
    raw = sub_event("subscription_created", updated=NOW, renews=NOW + D(days=30))
    deliver(wd, raw, now=NOW)
    with caplog.at_level(logging.ERROR, logger="flintel.billing"):
        out = deliver(wd, inv("subscription_payment_refunded", inv_id="70", reason="initial", created=NOW, status="refunded", total=1900,
                              refunded_amount=1900, updated=NOW + D(minutes=1)), now=NOW + D(minutes=1))
    assert out["refund"] == "unmatched_refund" and store_at(wd, NOW + D(minutes=2)).limit_for(ALICE) == 400
    late = NOW + D(minutes=3)
    deliver(wd, inv(inv_id="70", reason="initial", created=NOW, updated=late), now=late, get=Fresh(raw))
    assert sub_doc(wd)["revoked_period_key"] == KEY0 and store_at(wd, late + D(seconds=1)).limit_for(ALICE) == 40


@pytest.mark.parametrize("attrs_total,attrs_refunded,status,kind", [
    (1900, 1900, "refunded", "full"), (1900, 1900, "paid", "full"), (None, None, "refunded", "full"), (1900, 5000, "refunded", "full"),
    (1900, 500, "paid", "partial"), (1900, 1, "paid", "partial"),
    (1900, 500, "refunded", None), (1900, 0, "paid", None), (None, None, "paid", None), (1900, None, "paid", None),
    ("1900", "1900", "paid", None), (0, 0, "paid", None), (True, True, "paid", None)])
def test_refund_data_is_classified_only_on_positive_evidence(attrs_total, attrs_refunded, status, kind):
    attrs = {"status": status, "total": attrs_total, "refunded_amount": attrs_refunded}
    assert billing._refund_kind(attrs) == kind


def test_unclassifiable_refund_data_never_changes_access_and_alerts(cfg, wd, caplog):
    subscribe(wd)
    with caplog.at_level(logging.ERROR, logger="flintel.billing"):
        out = deliver(wd, inv("subscription_payment_refunded", inv_id="70", reason="initial", created=NOW), now=NOW + D(hours=1))
    assert out["refund"] == "unclassified" and sub_doc(wd).get("revoked_period_key") is None and sub_doc(wd)["refund_review_at"]
    assert "cannot be classified" in caplog.text and store_at(wd, NOW + D(hours=2)).limit_for(ALICE) == 400


def test_a_full_refund_of_a_proration_invoice_does_not_revoke_the_period(cfg, wd):
    subscribe(wd)
    out = deliver(wd, inv("subscription_payment_refunded", inv_id="71", reason="updated", created=NOW + D(days=1), status="refunded",
                          total=500, refunded_amount=500), now=NOW + D(days=2))
    assert out["refund"] == "full" and sub_doc(wd).get("revoked_period_key") is None
    assert store_at(wd, NOW + D(days=3)).limit_for(ALICE) == 400


def test_a_repeated_full_refund_revokes_once(cfg, wd):
    subscribe(wd)
    at = NOW + D(hours=1)
    raw = inv("subscription_payment_refunded", inv_id="70", reason="initial", created=NOW, status="refunded", total=1900,
              refunded_amount=1900, updated=at)
    deliver(wd, raw, now=at)
    revoked_at, rev = sub_doc(wd)["revoked_at"], sub_doc(wd)["rev"]
    assert deliver(wd, raw, now=at + D(hours=5)) == {"status": "duplicate"}
    again = inv("subscription_payment_refunded", inv_id="70", reason="initial", created=NOW, status="refunded", total=1900,
                refunded_amount=1900, updated=at + D(hours=1))
    deliver(wd, again, now=at + D(hours=5))
    assert sub_doc(wd)["revoked_at"] == revoked_at and sub_doc(wd)["refunded_invoices"] == {"70": "full"}
    assert sub_doc(wd)["rev"] >= rev


def test_a_late_replay_of_an_older_paid_invoice_cannot_take_over_the_period(cfg, wd):
    subscribe(wd)
    renew(wd, inv_id="77")
    get = Fresh(sub_event("subscription_updated", updated=T1 + D(seconds=1), renews=T1 + D(days=30)))
    out = deliver(wd, inv(inv_id="65", reason="renewal", created=NOW - D(days=2), updated=T1 + D(minutes=1)), now=T1 + D(minutes=1), get=get)
    assert out["invoice"] == "older_invoice" and sub_doc(wd)["period_invoice_id"] == "77"
    # so a refund of 77 still revokes the current period
    assert deliver(wd, inv("subscription_payment_refunded", inv_id="77", created=T1, status="refunded", total=1900, refunded_amount=1900),
                   now=T1 + D(hours=1))["refund"] == "revoked"


# ═══════════════ manual entitlement precedence ═══════════════
def test_billing_replaces_an_active_manual_entitlement_and_says_so(cfg, wd, caplog):
    sc.activate_plan(wd.plans, ALICE, "business", NOW + D(days=90), "manual-gift-1", now=NOW)
    with caplog.at_level(logging.WARNING, logger="flintel.credits"):
        subscribe(wd)
    rec = plan_record(wd)
    assert rec["plan"] == "standard" and rec["billing_rev"] >= 1                 # explicit precedence: billing wins
    assert "ACTIVE manual plan entitlement" in caplog.text and "business" in caplog.text and ALICE not in caplog.text
    assert wd.plans.count_documents({}) == 1


def test_an_expired_manual_entitlement_is_replaced_without_a_warning(cfg, wd, caplog):
    sc.activate_plan(wd.plans, ALICE, "business", NOW + D(days=1), "manual-old", now=NOW - D(days=40), active_from=NOW - D(days=40))
    with caplog.at_level(logging.WARNING, logger="flintel.credits"):
        subscribe(wd, at=NOW + D(days=2))
    assert plan_record(wd)["plan"] == "standard" and "manual plan entitlement" not in caplog.text


def test_billing_never_warns_about_its_own_records(cfg, wd, caplog):
    subscribe(wd)
    with caplog.at_level(logging.WARNING, logger="flintel.credits"):
        renew(wd)
    assert "manual plan entitlement" not in caplog.text


# ═══════════════ plan index failure and safe webhook handling ═══════════════
def database_probe(billing_on, duplicates):
    script = r'''
import json, hashlib, sys, logging
import mongomock, pymongo
shared = mongomock.MongoClient()
db = shared["flintel_bot"]
if %(dups)s:
    db.flintel_plans.insert_many([{"owner_key": "a@example.test", "plan": "business"}, {"owner_key": "a@example.test", "plan": "standard"},
                                  {"owner_key": "b@example.test", "plan": "standard"}])
db.flintel_subscriptions.insert_one({"_id": "1", "owner_key": "b@example.test"})
def snap():
    rows = sorted(json.dumps({k: str(v) for k, v in d.items()}, sort_keys=True) for c in ("flintel_plans", "flintel_subscriptions", "flintel_billing_events") for d in db[c].find())
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()
before = snap()
pymongo.MongoClient = lambda *a, **k: shared
import database
print(json.dumps({"flag": database.plans_owner_index_ok, "unchanged": before == snap(),
                  "unique": any(v.get("unique") for k, v in db.flintel_plans.index_information().items() if k != "_id_"),
                  "plans": db.flintel_plans.count_documents({})}))
''' % {"dups": "True" if duplicates else "False"}
    base = {k: v for k, v in os.environ.items() if not k.startswith(("MONGODB", "CREDIT_", "LEMONSQUEEZY"))}
    base.update({"SESSION_SECRET_KEY": "test-only-session-key-not-for-deployment", "MONGODB_URI": "mongodb://primary.invalid:27017",
                 "MONGODB3": "mongodb://third.invalid:27017", "MONGODB2": "", "MONGODB4": "", "PYTHONPATH": str(ROOT),
                 "PYTHONDONTWRITEBYTECODE": "1"})
    if billing_on:
        base.update({"LEMONSQUEEZY_API_KEY": "k", "LEMONSQUEEZY_STORE_ID": "4242", "LEMONSQUEEZY_WEBHOOK_SECRET": "s",
                     "LEMONSQUEEZY_VARIANT_ID_STANDARD": "1001", "LEMONSQUEEZY_VARIANT_ID_BUSINESS": "2002"})
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=base, capture_output=True, text=True, timeout=90, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1]), done.stderr


def test_database_startup_with_duplicate_plan_rows_alerts_and_modifies_nothing():
    state, err = database_probe(billing_on=True, duplicates=True)
    assert state["flag"] is False and state["unchanged"] is True and state["unique"] is False and state["plans"] == 3
    assert "BILLING ALERT" in err and "a@example.test" not in err and "b@example.test" not in err      # no owner data in the alert


def test_database_startup_builds_the_unique_index_when_it_can():
    state, err = database_probe(billing_on=True, duplicates=False)
    assert state["flag"] is True and state["unique"] is True and state["unchanged"] is True and "BILLING ALERT" not in err


def test_database_startup_without_billing_only_warns():
    state, err = database_probe(billing_on=False, duplicates=True)
    assert state["unchanged"] is True and "BILLING ALERT" not in err


def break_index(monkeypatch):
    monkeypatch.setattr(sys.modules["database"], "plans_owner_index_ok", False, raising=False)


def test_checkout_is_disabled_when_the_plan_index_is_missing(app, monkeypatch):
    break_index(monkeypatch)
    resp = checkout(app, "standard")
    assert resp.status_code == 503 and body_json(resp) == {"error": "billing_unavailable"} and app.posts == []
    html = app.rt.billing_upgrade(make_request(app.idx, "/billing/upgrade", "GET")).body.decode()
    assert "data-plan=" in html and html.count(" disabled") >= 2 and "Checkout isn't available right now" in html


def test_checkout_is_enabled_when_the_index_exists(app, monkeypatch):
    monkeypatch.setattr(sys.modules["database"], "plans_owner_index_ok", True, raising=False)
    assert checkout(app, "standard").status_code == 200


def test_webhooks_and_existing_subscriptions_keep_working_without_the_index(app, monkeypatch):
    deliver(app.db, sub_event(sid="1", updated=NOW))                    # an existing subscription
    break_index(monkeypatch)
    raw = sub_event("subscription_updated", sid="1", updated=NOW + D(days=1), renews=NOW + D(days=31), variant=V_BIZ, claim_plan="business")
    resp = webhook(app, raw)
    assert resp.status_code == 200 and body_json(resp) == {"status": "applied"}
    assert plan_record(app.db)["plan"] == "business"
    status = body_json(app.rt.billing_status(make_request(app.idx, "/api/billing/status", "GET")))
    assert status["plan"] == "business" and status["entitled"] is True


def test_a_paid_event_for_an_owner_with_duplicate_plan_rows_is_kept_and_retried(app, monkeypatch, caplog):
    break_index(monkeypatch)
    app.db.plans.drop_index("owner_key_1")                              # what the failed index build leaves behind
    app.db.plans.insert_many([{"owner_key": ALICE, "plan": "standard", "active_from": NOW - D(days=9), "active_until": NOW - D(days=8)},
                              {"owner_key": ALICE, "plan": "business", "active_from": NOW - D(days=9), "active_until": NOW - D(days=8)}])
    raw = sub_event(updated=NOW)
    with caplog.at_level(logging.ERROR, logger="flintel.credits"):
        resp = webhook(app, raw)
    assert resp.status_code == 500                                      # LS retries; nothing is acknowledged as done
    assert app.db.subs.find_one({"_id": "9001"})["status"] == "active"  # the paid state is stored, not discarded
    assert app.db.plans.count_documents({"owner_key": ALICE}) == 2 and all("billing_rev" not in p for p in app.db.plans.find())
    assert app.db.events.find_one({"state": "failed"}) and "several plan records" in caplog.text
    app.db.plans.delete_one({"owner_key": ALICE, "plan": "business"})   # operator cleanup
    again = webhook(app, raw)                                           # the retry of the very same delivery
    assert again.status_code == 200 and body_json(again) == {"status": "applied"}
    assert plan_record(app.db)["plan"] == "standard" and plan_record(app.db)["billing_rev"] >= 1


def test_set_plan_entitlement_refuses_duplicate_rows_and_writes_nothing(cfg, wd, caplog):
    wd.plans.drop_index("owner_key_1")
    wd.plans.insert_many([{"owner_key": ALICE, "plan": "standard"}, {"owner_key": ALICE, "plan": "business"}])
    before = list(wd.plans.find())
    with caplog.at_level(logging.ERROR, logger="flintel.credits"):
        with pytest.raises(sc.CreditStoreError):
            sc.set_plan_entitlement(wd.plans, ALICE, "standard", NOW, NOW + D(days=30), "r", 1, now=NOW)
    assert list(wd.plans.find()) == before and ALICE not in caplog.text


def test_a_concurrent_first_insert_without_the_index_is_detected(cfg, wd):
    wd.plans.drop_index("owner_key_1")

    class Racing:
        def __init__(self, inner):
            self.inner = inner

        def insert_one(self, doc):
            self.inner.insert_one({"owner_key": doc["owner_key"], "plan": "business", "rival": True})   # the other worker won
            return self.inner.insert_one(doc)

        def __getattr__(self, name):
            return getattr(self.inner, name)
    with pytest.raises(sc.CreditStoreError):
        sc.set_plan_entitlement(Racing(wd.plans), ALICE, "standard", NOW, NOW + D(days=30), "r", 1, now=NOW)


# ═══════════════ fresh portal links ═══════════════
PORTAL = "https://acme.lemonsqueezy.com/billing?expires=1&signature=%s"


def portal_get(counter):
    def get(url, headers):
        counter.append(url)
        sid = url.rsplit("/", 1)[1]
        attrs = {"store_id": int(STORE), "urls": {"customer_portal": PORTAL % len(counter)}}
        return Resp(200, {"data": {"type": "subscriptions", "id": sid, "attributes": attrs}})
    return get


def test_the_portal_url_is_fetched_fresh_every_time_and_never_stored(cfg, wd):
    subscribe(wd)
    seen = []
    first = billing.fresh_portal_url(wd.subs, ALICE, get=portal_get(seen))
    second = billing.fresh_portal_url(wd.subs, ALICE, get=portal_get(seen))
    assert first != second and len(seen) == 2 and seen[0].endswith("/v1/subscriptions/9001")
    assert "lemonsqueezy.com/billing" not in json.dumps(list(wd.subs.find()), default=str)      # not persisted anywhere
    assert "portal_url" not in sub_doc(wd) and "can_manage" in billing.status_for_owner(wd.subs, wd.plans, ALICE, NOW)
    assert "portal_url" not in billing.status_for_owner(wd.subs, wd.plans, ALICE, NOW)


def test_the_portal_url_belongs_to_the_owner_only(cfg, wd):
    subscribe(wd, sid="1", inv_id="11")
    subscribe(wd, sid="2", owner=BOB, inv_id="22")
    seen = []
    billing.fresh_portal_url(wd.subs, BOB, get=portal_get(seen))
    assert seen == ["https://api.lemonsqueezy.com/v1/subscriptions/2"]
    assert billing.fresh_portal_url(wd.subs, "carol@example.test", get=portal_get(seen)) is None and len(seen) == 1


@pytest.mark.parametrize("answer", [
    Resp(200, {"data": {"type": "subscriptions", "id": "9001", "attributes": {"store_id": int(STORE), "urls": {"customer_portal": "https://evil.example/x"}}}}),
    Resp(200, {"data": {"type": "subscriptions", "id": "9001", "attributes": {"store_id": int(STORE), "urls": {"customer_portal": "http://acme.lemonsqueezy.com/x"}}}}),
    Resp(200, {"data": {"type": "subscriptions", "id": "9001", "attributes": {"store_id": int(STORE), "urls": {}}}}),
    Resp(200, {"data": {"type": "subscriptions", "id": "5555", "attributes": {"store_id": int(STORE), "urls": {"customer_portal": PORTAL % 1}}}}),
    Resp(200, {"data": {"type": "subscriptions", "id": "9001", "attributes": {"store_id": 1, "urls": {"customer_portal": PORTAL % 1}}}}),
])
def test_an_unexpected_portal_answer_is_refused(cfg, wd, answer):
    subscribe(wd)
    with pytest.raises(billing.BillingError):
        billing.fresh_portal_url(wd.subs, ALICE, get=lambda u, h: answer)


@pytest.mark.parametrize("answer", [Resp(500, {}), Resp(404, {}), Resp(200, None, bad_json=True)])
def test_a_portal_provider_failure_is_a_store_error(cfg, wd, answer):
    subscribe(wd)
    with pytest.raises(sc.CreditStoreError):
        billing.fresh_portal_url(wd.subs, ALICE, get=lambda u, h: answer)


def portal(app):
    return app.rt.billing_portal(make_request(app.idx, "/api/billing/portal", "GET"))


def test_portal_route_is_private_and_fresh(app, monkeypatch):
    app.user["value"] = None
    assert portal(app).status_code == 401
    app.user["value"] = {"id": "u1", "email": ALICE}
    assert portal(app).status_code == 404 and body_json(portal(app)) == {"error": "no_subscription"}
    deliver(app.db, sub_event(sid="1", owner=ALICE))
    deliver(app.db, sub_event(sid="2", owner=BOB))
    seen = []
    monkeypatch.setattr(billing, "_http_get", portal_get(seen))
    resp = portal(app)
    assert resp.status_code == 200 and body_json(resp)["url"] == PORTAL % 1 and seen == ["https://api.lemonsqueezy.com/v1/subscriptions/1"]
    app.user["value"] = {"id": "u2", "email": BOB}
    portal(app)
    assert seen[1].endswith("/subscriptions/2")                         # each user is sent to their own subscription only


def test_portal_route_hides_provider_failures(app, monkeypatch):
    deliver(app.db, sub_event(sid="1"))
    monkeypatch.setattr(billing, "_http_get", lambda u, h: (_ for _ in ()).throw(TimeoutError("lsq_test_not_a_real_key")))
    resp = portal(app)
    assert resp.status_code == 503 and "lsq_test" not in resp.body.decode()


def test_portal_route_when_billing_is_not_configured(app, cfg):
    cfg.LEMONSQUEEZY_API_KEY = ""
    assert portal(app).status_code == 503


def test_pricing_page_manages_the_subscription_through_the_fresh_link_endpoint(app):
    deliver(app.db, sub_event())
    html = app.rt.billing_upgrade(make_request(app.idx, "/billing/upgrade", "GET")).body.decode()
    assert "data-portal" in html and "/api/billing/portal" in html and "lemonsqueezy.com/billing" not in html
    assert "reset on the 1st" not in html.split("Standard")[1]           # paid cards no longer promise calendar resets


def test_webhook_route_retries_a_payment_for_an_unrecorded_subscription(app):
    resp = webhook(app, inv(inv_id="70", reason="initial", created=NOW))
    assert resp.status_code == 409 and app.db.subs.count_documents({}) == 0
    assert app.db.events.find_one({"state": "failed"})                  # marked retryable, not "done"
