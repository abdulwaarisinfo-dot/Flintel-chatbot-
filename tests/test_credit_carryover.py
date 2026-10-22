"""
tests/test_credit_carryover.py - the one-time FIRST free -> paid carryover (search_credits.py + billing.py).

What these tests are: deterministic, logical tests of the REAL CreditStore and the REAL billing.handle_webhook, on mongomock,
with a fixed clock and forced interleavings (a collection proxy that runs a callback right before / after a chosen write).
What they are NOT: proof of real MongoDB atomicity or of Lemon Squeezy's live behaviour. mongomock serialises single-document
operations; the claims here are "the conditional filters we wrote enforce X" - whether the production database honours them
under real concurrency has not been verified (needs a staging run).

Numbers used everywhere: free grant 40, Standard 400, Business 1500, first period key KEY0 = 2026-04-09, free month 2026-03.
"""
import json
import types
from datetime import timedelta

import pytest

from tests.test_billing_hardening import (  # noqa: F401  (fixtures: cfg, db, wd)
    ALICE, BOB, KEY0, KEY1, NOW, V_BIZ, V_STD, D, Fresh, cfg, db, inv, store_at, sub_event, wd,
)
from tests.test_billing_lemonsqueezy import sign  # noqa: E402

mongomock = pytest.importorskip("mongomock")
import billing  # noqa: E402
import search_credits as sc  # noqa: E402

FREE = "2026-03"
T1 = NOW + D(days=30)


# ───────────────────────── helpers ─────────────────────────────────────────────────────────────────
class Hook:
    """A transparent proxy for a collection that runs callbacks right before / after an update_one that matches a predicate."""

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


def is_freeze_write(filt, update):
    return "$setOnInsert" in update and any(str(k).startswith("freeze.") for k in update.get("$set", {}))


def is_activation_write(filt, update):
    return "carryover.claim_id" in filt and "carryover.state" in update.get("$set", {}) and update["$set"]["carryover.state"] == "decided" \
        and "plan" in update["$set"]


def store(wd, now=NOW, coll=None, plans=None):
    return sc.CreditStore(coll or wd.usage, wd.chats, wd.jobs, lambda *a, **k: True, clock=lambda: now, enabled=lambda: True,
                          limit=lambda: 40, plans=plans or wd.plans)


def deliver_c(wd, raw, now=NOW, get=None, credits=None, plans=None):
    return billing.handle_webhook(raw, sign(raw), None, wd.subs, wd.events, plans or wd.plans, now=now, get=get, credits=credits)


def usage(wd, owner=ALICE):
    return wd.usage.find_one({"owner_key": owner}) or {}


def plan(wd, owner=ALICE):
    return wd.plans.find_one({"owner_key": owner})


def inv_ok(wd, owner=ALICE):
    assert sc.check_invariant(usage(wd, owner)) == []


def free_used(wd, owner=ALICE):
    return (usage(wd, owner).get("used_by_period") or {}).get(FREE, 0)


def free_charge(wd, credits, now=NOW, prefix="f", owner=ALICE, final=True):
    """Free-month usage of `credits` (10 per search, a remainder in one more). final=True moves each reservation to
    `sending` (permanently charged); final=False leaves the LAST one `reserved` (still refundable). Returns the tickets."""
    st, tickets, left, i = store(wd, now), [], credits, 0
    while left > 0:
        cost = min(10, left)
        t = st.reserve(owner, "email", f"{prefix}{i}", "topic", "chat1", True, cost=cost)
        assert isinstance(t, sc.Ticket), t
        tickets.append(t)
        left -= cost
        i += 1
    for k, t in enumerate(tickets):
        if final or k < len(tickets) - 1:
            assert st.start(t) == sc.DONE and st.claim(t) == sc.DONE
    return tickets


def created_raw(now=NOW, sid="9001", variant=V_STD, claim="standard", status="active", updated=None):
    return sub_event("subscription_created", sid=sid, variant=variant, claim_plan=claim, updated=updated or now, status=status,
                     renews=now + D(days=30))


def activate(wd, st=None, now=NOW, **kw):
    st = st or store(wd, now)
    raw = created_raw(now, **kw)
    return deliver_c(wd, raw, now, credits=st), st, raw


def spend(wd, st, rid, cost, final=False):
    """Reserve `cost`. final=True also moves it to `sending` (permanently charged; a bare reservation is swept after a while)."""
    t = st.reserve(ALICE, "email", rid, "topic", "chat1", True, cost=cost)
    if final and isinstance(t, sc.Ticket):
        assert st.start(t) == sc.DONE and st.claim(t) == sc.DONE
    return t


def carry(wd):
    return plan(wd).get("carryover")


def entry_of(wd, period=KEY0):
    m = carry(wd)
    return ((usage(wd).get("carryover_by_period") or {}).get(period) or {}).get(m["claim_id"])


def entry_of_all(wd, period=KEY0):
    return (usage(wd).get("carryover_by_period") or {}).get(period) or {}


def remaining(st, now=NOW, owner=ALICE):
    return st.status(owner)["remaining"]


# ═════════════════════ 1. the carryover amounts and the single bonus calculation ═════════════════════
@pytest.mark.parametrize("free,bonus,limit", [(0, 40, 440), (10, 30, 430), (20, 20, 420), (40, 0, 400)])
def test_the_first_paid_activation_carries_the_unused_free_credits(cfg, wd, free, bonus, limit):
    if free:
        free_charge(wd, free)
    out, st, _raw = activate(wd)
    assert out["status"] == "applied"
    p, m = plan(wd), carry(wd)
    assert (p["plan"], p["credit_period_key"], p["billing_rev"]) == ("standard", KEY0, 1)
    assert m["state"] == "decided" and m["period_key"] == KEY0 and m["sub_id"] == "9001" and m["free_period"] == FREE
    assert m["free_limit"] == 40 and not m.get("no_bonus")
    assert entry_of(wd)["bonus"] == bonus and entry_of(wd)["frozen_used"] == free
    s = st.status(ALICE)
    assert (s["limit"], s["used"], s["remaining"], s["period"]) == (limit, 0, limit, KEY0)
    assert usage(wd)["freeze"][FREE]["id"] == m["claim_id"]            # the freeze stays (persistent) while the plan entitles
    inv_ok(wd)


def test_the_bonus_is_computed_in_one_place_and_all_four_paths_agree(cfg, wd):
    free_charge(wd, 20)
    _out, st, _ = activate(wd)
    assert spend(wd, st, "p0", 100).cost == 100                       # paid used 100 (limit 420)
    doc, rec = usage(wd), plan(wd)
    assert sc.carryover_bonus(rec, doc, KEY0) == 20
    assert sc.carryover_bonus(rec, doc, "2026-05-09") == 0            # any other period: nothing
    s = st.status(ALICE)
    assert (s["limit"], s["used"], s["remaining"]) == (420, 100, 320)
    assert st.allowance(ALICE, NOW)[0] == 420 and st.limit_for(ALICE, NOW) == 420
    assert st.precheck_exhausted(ALICE, "new", cost=320) is None
    assert st.precheck_exhausted(ALICE, "new", cost=321).kind == "exhausted"
    assert isinstance(spend(wd, store(wd), "ok", 320), sc.Ticket)     # admission: exactly the remaining 320 fits ...
    assert spend(wd, store(wd), "one-more", 1).kind == "exhausted"    # ... and one more does not
    inv_ok(wd)


def test_a_bonus_below_the_stored_snapshot_lowers_every_path_without_rewriting_history(cfg, wd):
    free_charge(wd, 20)
    _out, st, _ = activate(wd)
    assert isinstance(spend(wd, st, "p0", 100), sc.Ticket) and isinstance(spend(wd, st, "p1", 315), sc.Ticket)   # used 415 of 420
    before = {k: usage(wd)[k] for k in ("used_by_period", "archived_by_period", "limit_max_by_period")}
    wd.usage.update_one({"owner_key": ALICE}, {"$inc": {f"used_by_period.{FREE}": 10}})      # an anomaly: the free counter moved (20 -> 30)
    # NB the anomaly breaks C = L + A for the FREE month on purpose; this test is about the paid period's own numbers
    s = st.status(ALICE)
    assert entry_of(wd)["bonus"] == 20                                # the stored snapshot is never rewritten
    assert sc.carryover_bonus(plan(wd), usage(wd), KEY0) == 10        # effective = min(20, 40 - 30)
    assert (s["limit"], s["used"], s["remaining"]) == (410, 415, 0)   # lowered, clamped, never negative
    assert st.allowance(ALICE, NOW)[0] == 410 and st.limit_for(ALICE, NOW) == 410
    assert st.precheck_exhausted(ALICE, "x", cost=1).kind == "exhausted" and spend(wd, store(wd), "z", 1).kind == "exhausted"
    after = usage(wd)
    assert after["used_by_period"][KEY0] == before["used_by_period"][KEY0] and after["archived_by_period"] == before["archived_by_period"]
    assert after["limit_max_by_period"][KEY0] >= 420                  # the high-water mark is never lowered


# ═════════════════════ 2. eligibility: first genuine activation only ═════════════════════
def test_a_pre_feature_subscriber_is_never_eligible_and_their_record_is_untouched(cfg, wd):
    """A plan record without `carryover` (written before this feature): later events never add a claim, a marker or a bonus."""
    free_charge(wd, 20)
    sc.set_plan_entitlement(wd.plans, ALICE, "standard", NOW - D(days=40), NOW - D(days=10), "legacy:1", 1, NOW - D(days=40))   # lapsed
    wd.subs.insert_one({"_id": "8000", "owner_key": ALICE, "plan": "standard", "rev": 5, "status": "expired",
                        "started_at": NOW - D(days=40), "entitled_until": NOW - D(days=10)})   # the lapsed subscription behind it
    snapshot = dict(plan(wd))
    out, st, _ = activate(wd)                                          # a brand-new subscription of the same owner
    p = plan(wd)
    assert "carryover" not in p and "carryover" not in snapshot
    assert p["plan"] == "standard" and p["billing_rev"] >= 1 and p["source_ref"] != "legacy:1"
    assert st.status(ALICE)["limit"] == 400                           # the plain grant, no bonus
    assert not usage(wd).get("freeze") and not usage(wd).get("carryover_by_period")
    inv_ok(wd)


def test_a_resubscription_after_a_lapse_gets_nothing_and_the_marker_is_terminal(cfg, wd):
    free_charge(wd, 20)
    _out, st, _ = activate(wd)
    first = dict(carry(wd))
    deliver_c(wd, sub_event("subscription_expired", status="expired", updated=NOW + D(days=40), ends=NOW + D(days=40),
                            renews=NOW + D(days=70)), NOW + D(days=40), credits=store(wd, NOW + D(days=40)))
    assert plan(wd)["active_until"].replace(tzinfo=None) <= (NOW + D(days=40)).replace(tzinfo=None)
    later = NOW + D(days=60)
    raw = sub_event("subscription_created", sid="9002", updated=later, renews=later + D(days=30), created=later)
    out = deliver_c(wd, raw, later, credits=store(wd, later))
    assert out["status"] == "applied"
    assert carry(wd) == first                                          # the decision is terminal: same claim id, same period
    s = store(wd, later).status(ALICE)
    assert s["limit"] == 400 and s["period"] != KEY0                  # a new period key, no bonus
    inv_ok(wd)


def test_an_owner_whose_first_write_is_not_an_active_window_stays_eligible(cfg, wd):
    raw = created_raw(status="past_due")
    deliver_c(wd, raw, NOW, credits=store(wd))
    assert plan(wd) is None or (plan(wd).get("carryover") or {}).get("state") in (None, "undecided")
    free_charge(wd, 20)
    later = NOW + D(days=1)
    pay = sub_event("subscription_updated", status="active", updated=later, renews=later + D(days=30))
    out = deliver_c(wd, pay, later, credits=store(wd, later))
    assert out["status"] == "applied"
    s = store(wd, later).status(ALICE)
    assert carry(wd)["state"] == "decided" and s["limit"] >= 400


def test_the_carryover_needs_a_billing_period_key_otherwise_nothing_is_granted(cfg, wd):
    free_charge(wd, 20)
    sc.set_plan_entitlement(wd.plans, "x@example.test", "standard", NOW - D(days=1), NOW + D(days=29), "m:1", 1, NOW)   # unrelated row
    code, d = billing._plan_inputs(wd.subs, ALICE, NOW)
    assert code == "no_subscription"                                  # sanity of the helper
    # a subscription whose document has no period (never happens with billing's own writes) is written plainly
    raw = created_raw()
    st = store(wd)
    orig = billing._plan_inputs

    def no_period(subs, owner, now):
        c, data = orig(subs, owner, now)
        if data:
            data["period"] = {}
        return c, data
    billing._plan_inputs = no_period
    try:
        deliver_c(wd, raw, NOW, credits=st)
    finally:
        billing._plan_inputs = orig
    assert plan(wd).get("carryover") is None and st.status(ALICE)["limit"] == 400


# ═════════════════════ 3. duplicate / replayed deliveries ═════════════════════
def test_duplicate_and_replayed_deliveries_never_add_or_change_the_bonus(cfg, wd):
    free_charge(wd, 20)
    st = store(wd)
    raw = created_raw()
    assert deliver_c(wd, raw, NOW, credits=st)["status"] == "applied"
    frozen = (dict(carry(wd)), dict(entry_of(wd)), plan(wd)["billing_rev"], dict(usage(wd)["freeze"]))
    assert deliver_c(wd, raw, NOW, credits=st) == {"status": "duplicate"}                     # byte-identical redelivery
    pay = inv(inv_id="70", reason="initial", created=NOW)
    get = Fresh(raw)
    assert deliver_c(wd, pay, NOW, get=get, credits=st)["status"] in ("applied", "stale")
    assert deliver_c(wd, inv("subscription_payment_recovered", inv_id="70", created=NOW, updated=NOW + D(seconds=3)), NOW, get=get,
                     credits=st) == {"status": "duplicate_payment"}
    assert (dict(carry(wd)), dict(entry_of(wd))) == frozen[:2] and dict(usage(wd)["freeze"]) == frozen[3]
    assert store(wd).status(ALICE)["limit"] == 420
    assert len(list(wd.plans.find({"owner_key": ALICE}))) == 1
    inv_ok(wd)


def test_concurrent_first_activation_events_produce_one_claim_one_bonus(cfg, wd):
    """created and the initial payment arrive together: the second sees a live claim (409) or the decided marker - never a second bonus."""
    free_charge(wd, 20)
    st = store(wd)
    raw = created_raw()
    plans = Hook(wd.plans)
    seen = {}

    def other_event():
        seen["second"] = pytest.raises(billing.WebhookBusy, deliver_c, wd, inv(inv_id="71", reason="initial", created=NOW), NOW,
                                       Fresh(raw), st, plans)
    plans.before.append((is_activation_write, other_event))            # the second event arrives while the first is about to write
    assert deliver_c(wd, raw, NOW, credits=st, plans=plans)["status"] == "applied"
    assert seen["second"]                                              # it was deferred (409), not applied
    assert carry(wd)["state"] == "decided" and entry_of(wd)["bonus"] == 20
    assert len(list(wd.plans.find({"owner_key": ALICE}))) == 1
    assert deliver_c(wd, inv(inv_id="71", reason="initial", created=NOW), NOW, Fresh(raw), st)["status"] in ("applied", "stale")
    assert store(wd).status(ALICE)["limit"] == 420 and carry(wd)["claim_id"] == usage(wd)["freeze"][FREE]["id"]
    inv_ok(wd)


# ═════════════════════ 4. renewal isolation and billing-period safety ═════════════════════
def renew_c(wd, st, at=T1, inv_id="77", sid="9001", name="subscription_payment_success"):
    sub_raw = sub_event("subscription_updated", sid=sid, updated=at, renews=at + D(days=30))
    return deliver_c(wd, inv(name, inv_id=inv_id, sid=sid, created=at), at, get=Fresh(sub_raw), credits=st), sub_raw


def test_a_genuine_renewal_resets_to_exactly_the_plan_grant_with_no_old_bonus_or_balance(cfg, wd):
    free_charge(wd, 20)
    _out, st, raw = activate(wd)
    deliver_c(wd, inv(inv_id="70", reason="initial", created=NOW), NOW, get=Fresh(raw), credits=st)
    assert isinstance(spend(wd, st, "a", 100, final=True), sc.Ticket)                                # 420 limit, 100 used, 320 unused
    st1 = store(wd, T1)
    out, sub1 = renew_c(wd, st1)
    assert out["status"] == "applied" and plan(wd)["credit_period_key"] == KEY1
    s = st1.status(ALICE)
    assert (s["limit"], s["used"], s["remaining"], s["period"]) == (400, 0, 400, KEY1)   # exactly the grant, not 720
    assert usage(wd)["used_by_period"][KEY0] == 100                                       # the old period is untouched
    assert sc.carryover_bonus(plan(wd), usage(wd), KEY1) == 0 and carry(wd)["period_key"] == KEY0
    T2 = T1 + D(days=30)
    st2 = store(wd, T2)
    renew_c(wd, st2, at=T2, inv_id="78")
    assert (st2.status(ALICE)["limit"], st2.status(ALICE)["remaining"]) == (400, 400)
    inv_ok(wd)


def test_duplicate_and_stale_renewal_events_cannot_reset_or_change_a_newer_period(cfg, wd):
    free_charge(wd, 20)
    _out, st, raw = activate(wd)
    st1 = store(wd, T1)
    renew_c(wd, st1)
    T2 = T1 + D(days=30)
    st2 = store(wd, T2)
    _o, sub2 = renew_c(wd, st2, at=T2, inv_id="78")
    assert isinstance(spend(wd, st2, "k2a", 50), sc.Ticket)                              # 50 spent in the newest period
    state = (plan(wd)["credit_period_key"], plan(wd)["billing_rev"], dict(usage(wd)["used_by_period"]))
    # the byte-identical renewal 2 again
    assert deliver_c(wd, inv("subscription_payment_success", inv_id="78", created=T2), T2, get=Fresh(sub2), credits=st2)["status"] \
        == "duplicate"
    # an OLD renewal (invoice 77, different body) and an old subscription update arrive late
    old_pay = deliver_c(wd, inv("subscription_payment_recovered", inv_id="77", created=T1, updated=T1 + D(seconds=9)), T2,
                        get=Fresh(sub_event("subscription_updated", updated=T1, renews=T1 + D(days=30))), credits=st2)
    assert old_pay["status"] == "duplicate_payment"
    old_sub = deliver_c(wd, sub_event("subscription_updated", updated=T1 - D(seconds=5), renews=T1 + D(days=30)), T2, credits=st2)
    assert old_sub["status"] == "stale"
    assert (plan(wd)["credit_period_key"], plan(wd)["billing_rev"], dict(usage(wd)["used_by_period"])) == state
    s = st2.status(ALICE)
    assert (s["limit"], s["used"], s["remaining"]) == (400, 50, 350)
    inv_ok(wd)


# ═════════════════════ 5. plan changes inside the first period ═════════════════════
def sub_change(variant, claim, at, renews=None, sid="9001"):
    return sub_event("subscription_updated", sid=sid, variant=variant, claim_plan=claim, updated=at, renews=renews or NOW + D(days=30))


def test_an_upgrade_and_a_downgrade_in_the_first_period_keep_the_original_bonus_and_never_add_it_again(cfg, wd):
    free_charge(wd, 20)
    _out, st, raw = activate(wd)
    assert isinstance(spend(wd, st, "a", 300, final=True), sc.Ticket)                                # Standard 420, 300 used
    marker, entry = dict(carry(wd)), dict(entry_of(wd))
    at = NOW + D(days=2)
    st2 = store(wd, at)
    up = sub_change(V_BIZ, "business", at)
    assert deliver_c(wd, up, at, credits=st2)["status"] == "applied"
    p, s = plan(wd), st2.status(ALICE)
    assert (p["plan"], p["credit_period_key"], p["billing_rev"]) == ("business", KEY0, 2)
    assert (s["limit"], s["used"], s["remaining"]) == (1520, 300, 1220)                  # 1500 + the ORIGINAL 20, usage kept
    assert dict(carry(wd)) == marker and dict(entry_of(wd)) == entry
    assert deliver_c(wd, up, at, credits=st2) == {"status": "duplicate"}                 # the replay changes nothing
    at2 = NOW + D(days=3)
    st3 = store(wd, at2)
    down = sub_change(V_STD, "standard", at2)
    deliver_c(wd, down, at2, credits=st3)
    s = st3.status(ALICE)
    assert (plan(wd)["plan"], s["limit"], s["remaining"]) == ("standard", 420, 120)      # 400 + 20 - 300, nothing erased
    assert dict(carry(wd)) == marker and dict(entry_of(wd)) == entry
    renew_c(wd, store(wd, T1))
    assert store(wd, T1).status(ALICE)["limit"] == 400                                   # the renewal is exactly the grant
    inv_ok(wd)


# ═════════════════════ 6. the freeze: reservations vs the snapshot (forced interleavings) ═════════════════════
def test_A_a_free_reservation_that_lands_before_the_freeze_is_in_the_snapshot(cfg, wd):
    free_charge(wd, 20)
    usage_h = Hook(wd.usage)
    st = store(wd, coll=usage_h)
    held = {}

    def reservation_lands():
        held["t"] = store(wd).reserve(ALICE, "email", "R", "topic", "chat1", True, cost=10)     # state reserved: refundable
    usage_h.before.append((is_freeze_write, reservation_lands))
    raw = created_raw()
    with pytest.raises(billing.WebhookBusy):                               # the freeze write failed (counter moved); R is refundable
        deliver_c(wd, raw, NOW, credits=st)
    assert isinstance(held["t"], sc.Ticket) and free_used(wd) == 30
    assert plan(wd)["carryover"]["state"] == "undecided" and not usage(wd).get("freeze")      # nothing frozen, claim released
    assert (store(wd).status(ALICE)["limit"], store(wd).status(ALICE)["remaining"]) == (40, 10)   # still the free allowance
    c = store(wd)
    assert c.start(held["t"]) == sc.DONE and c.claim(held["t"]) == sc.DONE                      # R is now permanently charged
    assert deliver_c(wd, raw, NOW, credits=store(wd))["status"] == "applied"
    s = store(wd).status(ALICE)
    assert (entry_of(wd)["bonus"], entry_of(wd)["frozen_used"], free_used(wd), s["limit"], s["remaining"]) == (10, 30, 30, 410, 410)
    inv_ok(wd)


def test_A2_the_same_race_with_the_reservation_refunded_before_the_retry(cfg, wd):
    free_charge(wd, 20)
    usage_h = Hook(wd.usage)
    st = store(wd, coll=usage_h)
    got = {}
    usage_h.before.append((is_freeze_write, lambda: got.setdefault("t", store(wd).reserve(ALICE, "email", "R", "t", "chat1", True, cost=10))))
    raw = created_raw()
    with pytest.raises(billing.WebhookBusy):
        deliver_c(wd, raw, NOW, credits=st)
    assert store(wd).self_refund(got["t"]) and free_used(wd) == 20
    assert deliver_c(wd, raw, NOW, credits=store(wd))["status"] == "applied"
    s = store(wd).status(ALICE)
    assert (entry_of(wd)["bonus"], s["limit"], s["remaining"]) == (20, 420, 420)
    inv_ok(wd)


def test_B_a_free_reservation_after_the_freeze_is_refused_and_is_charged_to_the_paid_period_on_retry(cfg, wd):
    free_charge(wd, 20)
    plans_h = Hook(wd.plans)
    st = store(wd, plans=plans_h)
    refused = {}
    plans_h.before.append((is_activation_write, lambda: refused.setdefault("d", store(wd).reserve(ALICE, "email", "R", "t", "chat1", True, cost=10))))
    assert deliver_c(wd, created_raw(), NOW, credits=st, plans=plans_h)["status"] == "applied"
    assert getattr(refused["d"], "kind", None) == "unavailable"                          # frozen: refused, nothing written
    assert free_used(wd) == 20 and "R" not in usage(wd)["res"]
    assert (entry_of(wd)["bonus"], store(wd).status(ALICE)["limit"]) == (20, 420)
    t = store(wd).reserve(ALICE, "email", "R", "t", "chat1", True, cost=10)               # the retry lands on the PAID period
    assert isinstance(t, sc.Ticket) and t.period == KEY0
    s = store(wd).status(ALICE)
    assert (free_used(wd), usage(wd)["used_by_period"][KEY0], s["remaining"]) == (20, 10, 410)
    inv_ok(wd)


def test_C1_two_reservations_both_before_the_freeze(cfg, wd):
    free_charge(wd, 20)
    usage_h = Hook(wd.usage)
    st = store(wd, coll=usage_h)
    tickets = []

    def both():
        for rid in ("R1", "R2"):
            tickets.append(store(wd).reserve(ALICE, "email", rid, "t", "chat1", True, cost=10))
    usage_h.before.append((is_freeze_write, both))
    raw = created_raw()
    with pytest.raises(billing.WebhookBusy):
        deliver_c(wd, raw, NOW, credits=st)
    assert free_used(wd) == 40 and all(isinstance(t, sc.Ticket) for t in tickets)
    c = store(wd)
    for t in tickets:
        assert c.start(t) == sc.DONE and c.claim(t) == sc.DONE
    assert deliver_c(wd, raw, NOW, credits=store(wd))["status"] == "applied"
    s = store(wd).status(ALICE)
    assert (entry_of(wd)["bonus"], free_used(wd), s["limit"], s["remaining"]) == (0, 40, 400, 400)
    assert carry(wd)["state"] == "decided"                                                # decided with a zero bonus: terminal
    inv_ok(wd)


def test_C2_one_reservation_before_the_freeze_and_one_after(cfg, wd):
    free_charge(wd, 20)
    usage_h, plans_h = Hook(wd.usage), Hook(wd.plans)
    st = store(wd, coll=usage_h, plans=plans_h)
    got = {}
    usage_h.before.append((is_freeze_write, lambda: got.setdefault("r1", store(wd).reserve(ALICE, "email", "R1", "t", "chat1", True, cost=10))))
    raw = created_raw()
    with pytest.raises(billing.WebhookBusy):
        deliver_c(wd, raw, NOW, credits=st, plans=plans_h)
    c = store(wd)
    assert c.start(got["r1"]) == sc.DONE and c.claim(got["r1"]) == sc.DONE
    plans_h.before.append((is_activation_write, lambda: got.setdefault("r2", store(wd).reserve(ALICE, "email", "R2", "t", "chat1", True, cost=10))))
    assert deliver_c(wd, raw, NOW, credits=store(wd, coll=usage_h, plans=plans_h), plans=plans_h)["status"] == "applied"
    assert getattr(got["r2"], "kind", None) == "unavailable" and free_used(wd) == 30
    s = store(wd).status(ALICE)
    assert (entry_of(wd)["bonus"], s["limit"], s["remaining"]) == (10, 410, 410)
    inv_ok(wd)


def test_C3_both_reservations_after_the_freeze_are_refused(cfg, wd):
    free_charge(wd, 20)
    plans_h = Hook(wd.plans)
    st = store(wd, plans=plans_h)
    got = []
    plans_h.before.append((is_activation_write, lambda: [got.append(store(wd).reserve(ALICE, "email", r, "t", "chat1", True, cost=10)) for r in ("R1", "R2")]))
    assert deliver_c(wd, created_raw(), NOW, credits=st, plans=plans_h)["status"] == "applied"
    assert [getattr(g, "kind", None) for g in got] == ["unavailable", "unavailable"] and free_used(wd) == 20
    assert (entry_of(wd)["bonus"], store(wd).status(ALICE)["limit"]) == (20, 420)
    for r in ("R1", "R2"):
        assert isinstance(store(wd).reserve(ALICE, "email", r, "t", "chat1", True, cost=10), sc.Ticket)
    assert store(wd).status(ALICE)["remaining"] == 400                                    # 420 - 20: the same total as C1
    inv_ok(wd)


def test_E_a_reservation_that_read_the_allowance_as_free_before_the_plan_write_cannot_land_after_it(cfg, wd):
    free_charge(wd, 20)
    plans_h = Hook(wd.plans)
    st = store(wd, plans=plans_h)
    res = {}

    def stale_reserve():
        c = store(wd)
        real = c._allowance_full
        calls = {"n": 0}

        def first_call_stale(owner_key, now=None, owner_type=None, doc=sc._UNSET):
            calls["n"] += 1
            if calls["n"] == 1:                                                        # the read made BEFORE the plan write
                return 40, FREE, sc.period_resets_at(NOW), sc.PLAN_FREE, {}
            return real(owner_key, now, owner_type, doc)
        c._allowance_full = first_call_stale
        res["t"] = c.reserve(ALICE, "email", "R", "t", "chat1", True, cost=10)
    plans_h.after.append((is_activation_write, stale_reserve))
    assert deliver_c(wd, created_raw(), NOW, credits=st, plans=plans_h)["status"] == "applied"
    assert isinstance(res["t"], sc.Ticket) and res["t"].period == KEY0                  # redirected to the paid period
    s = store(wd).status(ALICE)
    assert (free_used(wd), usage(wd)["used_by_period"][KEY0], entry_of(wd)["bonus"], s["limit"], s["remaining"]) == (20, 10, 20, 420, 410)
    inv_ok(wd)


# ═════════════════════ 7. claim fencing: a paused activation, takeover, orphan freezes ═════════════════════
def test_X_a_paused_activation_cannot_write_a_stale_snapshot_after_its_freeze_was_revoked(cfg, wd):
    """A claims and freezes (bonus 20), pauses past the stale limit; a free reservation revokes it and lands; A's write matches nothing."""
    free_charge(wd, 20)
    st = store(wd)
    S_A = "a" * 32
    assert sc.claim_activation(wd.plans, ALICE, S_A, NOW) == "claimed"
    assert st.freeze_free_period(ALICE, FREE, S_A, "9001", KEY0, NOW)["bonus"] == 20
    assert (st.status(ALICE)["limit"], free_used(wd)) == (40, 20)
    assert store(wd, NOW + D(seconds=60)).reserve(ALICE, "email", "Rx", "t", "chat1", True, cost=3).kind == "unavailable"   # young claim
    later = NOW + D(seconds=200)
    stl = store(wd, later)
    r = stl.reserve(ALICE, "email", "R", "t", "chat1", True, cost=10)                      # revokes the stale claim, clears, lands
    assert isinstance(r, sc.Ticket) and free_used(wd) == 30
    assert plan(wd)["carryover"]["state"] == "undecided" and not usage(wd).get("freeze")
    out = sc.activate_with_carryover(wd.plans, ALICE, "standard", NOW, NOW + D(days=30), "ls-sub:9001:rev1", 1, later, KEY0,
                                     NOW + D(days=30), {"claim_id": S_A, "sub_id": "9001", "free_period": FREE, "free_limit": 40})
    assert out == "lost"                                                                   # fenced: nothing written
    assert plan(wd).get("plan") is None and stl.status(ALICE)["limit"] == 40 and stl.status(ALICE)["remaining"] == 10
    inv_ok(wd)


def test_Y1_a_competing_attempt_defers_while_the_claim_is_live(cfg, wd):
    free_charge(wd, 20)
    st = store(wd)
    assert sc.claim_activation(wd.plans, ALICE, "a" * 32, NOW) == "claimed"
    assert sc.claim_activation(wd.plans, ALICE, "b" * 32, NOW + D(seconds=60)) == "busy"
    with pytest.raises(billing.WebhookBusy):
        deliver_c(wd, created_raw(), NOW + D(seconds=60), credits=store(wd, NOW + D(seconds=60)))
    assert plan(wd).get("plan") is None and carry(wd)["claim_id"] == "a" * 32              # no second freeze, no plan write
    assert not usage(wd).get("freeze")


def test_Y2_takeover_by_a_competing_attempt_fences_the_paused_one_and_the_snapshot_is_the_winners_own(cfg, wd):
    free_charge(wd, 20)
    S_A = "a" * 32
    stA = store(wd)
    assert sc.claim_activation(wd.plans, ALICE, S_A, NOW) == "claimed"
    stA.freeze_free_period(ALICE, FREE, S_A, "9001", KEY0, NOW)
    later = NOW + D(seconds=200)
    usage_h = Hook(wd.usage)
    stB = store(wd, later, coll=usage_h)
    got = {}
    # a free reservation lands after B cleared A's orphan freeze but before B's own freeze (interleaving D)
    usage_h.before.append((is_freeze_write, lambda: got.setdefault("r", store(wd, later).reserve(ALICE, "email", "R", "t", "chat1", True, cost=10))))
    raw = created_raw(NOW)
    with pytest.raises(billing.WebhookBusy):                                               # R is refundable: B defers
        deliver_c(wd, raw, later, credits=stB)
    assert isinstance(got["r"], sc.Ticket) and free_used(wd) == 30 and not usage(wd).get("freeze")
    c = store(wd, later)
    assert c.start(got["r"]) == sc.DONE and c.claim(got["r"]) == sc.DONE
    assert deliver_c(wd, raw, later, credits=store(wd, later))["status"] == "applied"
    m = carry(wd)
    assert m["claim_id"] != S_A and entry_of(wd)["bonus"] == 10                            # B's own snapshot, not A's 20
    s = store(wd, later).status(ALICE)
    assert (s["limit"], s["remaining"]) == (410, 410)
    # A resumes: its write and its cleanup both match nothing
    assert sc.activate_with_carryover(wd.plans, ALICE, "standard", NOW, NOW + D(days=30), "ls-sub:9001:rev1", 1, later, KEY0,
                                      NOW + D(days=30), {"claim_id": S_A, "sub_id": "9001", "free_period": FREE, "free_limit": 40}) == "lost"
    assert store(wd, later)._clear_freeze(ALICE, FREE, S_A) is False
    assert usage(wd)["freeze"][FREE]["id"] == m["claim_id"] and store(wd, later).status(ALICE)["limit"] == 410
    inv_ok(wd)


def test_D1_an_orphan_freeze_is_cleared_by_the_next_reservation_without_the_old_holder(cfg, wd):
    """B took over (claim now S_B) but crashed before clearing A's freeze."""
    free_charge(wd, 20)
    S_A, S_B = "a" * 32, "b" * 32
    st = store(wd)
    assert sc.claim_activation(wd.plans, ALICE, S_A, NOW) == "claimed"
    st.freeze_free_period(ALICE, FREE, S_A, "9001", KEY0, NOW)
    later = NOW + D(seconds=200)
    assert sc.claim_activation(wd.plans, ALICE, S_B, later) == "claimed"                  # B's takeover; then B "crashes"
    assert usage(wd)["freeze"][FREE]["id"] == S_A                                          # A's freeze is an orphan
    stl = store(wd, later)
    t = stl.reserve(ALICE, "email", "R", "t", "chat1", True, cost=10)                      # clears the orphan itself and proceeds
    assert isinstance(t, sc.Ticket) and not usage(wd).get("freeze") and free_used(wd) == 30
    assert carry(wd)["claim_id"] == S_B and carry(wd)["state"] == "claiming"               # B's claim untouched
    assert stl.start(t) == sc.DONE and stl.claim(t) == sc.DONE
    # a later attempt (after B's claim goes stale) re-snapshots with the real counter
    much_later = later + D(seconds=200)
    assert deliver_c(wd, created_raw(NOW), much_later, credits=store(wd, much_later))["status"] == "applied"
    assert (entry_of(wd)["bonus"], store(wd, much_later).status(ALICE)["limit"]) == (10, 410)
    inv_ok(wd)


def test_D2_an_abort_only_revoke_that_crashed_before_the_unfreeze_leaves_a_clearable_orphan(cfg, wd):
    free_charge(wd, 20)
    S_A = "a" * 32
    st = store(wd)
    sc.claim_activation(wd.plans, ALICE, S_A, NOW)
    st.freeze_free_period(ALICE, FREE, S_A, "9001", KEY0, NOW)
    later = NOW + D(seconds=200)
    assert sc.revoke_claim(wd.plans, ALICE, S_A, later) is True                            # the revoke landed, the unfreeze did not
    assert plan(wd)["carryover"]["state"] == "undecided" and usage(wd)["freeze"][FREE]["id"] == S_A
    t = store(wd, later).reserve(ALICE, "email", "R", "t", "chat1", True, cost=10)
    assert isinstance(t, sc.Ticket) and not usage(wd).get("freeze")
    for _ in range(2):                                                                     # repeated recovery is a no-op
        assert store(wd, later)._clear_freeze(ALICE, FREE, S_A) is False
    inv_ok(wd)


def test_D3_a_crash_after_clearing_the_orphan_before_the_new_freeze_leaves_the_free_month_usable(cfg, wd):
    free_charge(wd, 20)
    S_A, S_B = "a" * 32, "b" * 32
    st = store(wd)
    sc.claim_activation(wd.plans, ALICE, S_A, NOW)
    st.freeze_free_period(ALICE, FREE, S_A, "9001", KEY0, NOW)
    later = NOW + D(seconds=200)
    sc.claim_activation(wd.plans, ALICE, S_B, later)
    assert store(wd, later)._clear_freeze(ALICE, FREE, S_A) is True                        # cleaned, then B crashes
    t = store(wd, later).reserve(ALICE, "email", "R", "t", "chat1", True, cost=10)
    assert isinstance(t, sc.Ticket) and free_used(wd) == 30                                # nothing is frozen: the search runs


def test_D4_a_crash_after_the_freeze_before_the_plan_write_blocks_only_until_the_claim_goes_stale(cfg, wd):
    free_charge(wd, 20)
    S_B = "b" * 32
    st = store(wd)
    sc.claim_activation(wd.plans, ALICE, S_B, NOW)
    st.freeze_free_period(ALICE, FREE, S_B, "9001", KEY0, NOW)
    for secs in (1, 60, 119):
        assert store(wd, NOW + D(seconds=secs)).reserve(ALICE, "email", f"R{secs}", "t", "chat1", True, cost=3).kind == "unavailable"
    t = store(wd, NOW + D(seconds=121)).reserve(ALICE, "email", "Rlate", "t", "chat1", True, cost=3)
    assert isinstance(t, sc.Ticket) and plan(wd)["carryover"]["state"] == "undecided" and not usage(wd).get("freeze")


def test_S_a_late_freeze_write_by_a_fenced_attempt_after_the_winner_completed_is_an_inert_orphan(cfg, wd):
    """A claims and pauses; B takes over and completes (bonus 20, limit 420); A then writes its freeze: nothing it writes counts."""
    free_charge(wd, 20)
    S_A = "a" * 32
    sc.claim_activation(wd.plans, ALICE, S_A, NOW)
    later = NOW + D(seconds=200)
    assert deliver_c(wd, created_raw(NOW), later, credits=store(wd, later))["status"] == "applied"
    winner = dict(carry(wd))
    assert store(wd, later).status(ALICE)["limit"] == 420 and winner["claim_id"] != S_A
    usage_doc = usage(wd)
    assert usage_doc["freeze"][FREE]["id"] == winner["claim_id"]                           # the winner's freeze persists
    # A's late write: the usage document has a freeze, so A's own freeze write finds one and defers; it cannot overwrite
    stA = store(wd, later)
    with pytest.raises(sc.ActivationDeferred):
        stA.freeze_free_period(ALICE, FREE, S_A, "9001", KEY0, later)
    assert usage(wd)["freeze"][FREE]["id"] == winner["claim_id"] and S_A not in entry_of_all(wd)
    assert dict(carry(wd)) == winner and entry_of(wd)["bonus"] == 20 and store(wd, later).status(ALICE)["limit"] == 420
    assert sc.activate_with_carryover(wd.plans, ALICE, "standard", NOW, NOW + D(days=30), "ls-sub:9001:rev9", 9, later, KEY0,
                                      NOW + D(days=30), {"claim_id": S_A, "sub_id": "9001", "free_period": FREE, "free_limit": 40}) == "lost"
    inv_ok(wd)


def test_S2_a_fenced_freeze_written_after_the_winners_freeze_was_cleared_is_cleared_by_the_next_free_reservation(cfg, wd):
    """The plan lapses in the same month; A's orphan freeze (written late, claim long gone) must not block anyone."""
    free_charge(wd, 20)
    S_A = "a" * 32
    sc.claim_activation(wd.plans, ALICE, S_A, NOW)
    later = NOW + D(seconds=200)
    assert deliver_c(wd, created_raw(NOW), later, credits=store(wd, later))["status"] == "applied"
    winner = carry(wd)["claim_id"]
    wd.usage.update_one({"owner_key": ALICE}, {"$unset": {f"freeze.{FREE}": ""}})                      # the winner's freeze is gone ...
    store(wd, later).freeze_free_period(ALICE, FREE, S_A, "9001", KEY0, later)                         # ... A's late write succeeds
    assert usage(wd)["freeze"][FREE]["id"] == S_A and entry_of(wd)["bonus"] == 20                      # winner's entry intact (keyed by claim)
    assert store(wd, later).status(ALICE)["limit"] == 420                                              # the paid allowance is unaffected
    wd.plans.update_one({"owner_key": ALICE}, {"$set": {"active_until": later - D(seconds=1)}})        # the plan lapses in March
    t = store(wd, later + D(days=1)).reserve(ALICE, "email", "R", "t", "chat1", True, cost=3)
    assert isinstance(t, sc.Ticket) and not usage(wd).get("freeze") and t.period == FREE and carry(wd)["claim_id"] == winner


# ═════════════════════ 8. decide-zero and the plan record's safety ═════════════════════
def test_V3_a_non_claim_activation_while_a_claim_is_held_ends_in_decide_zero_with_no_bonus(cfg, wd):
    free_charge(wd, 20)
    S_A = "a" * 32
    st = store(wd)
    sc.claim_activation(wd.plans, ALICE, S_A, NOW)
    st.freeze_free_period(ALICE, FREE, S_A, "9001", KEY0, NOW)
    sc.activate_plan(wd.plans, ALICE, "standard", NOW + D(days=30), "manual:x", now=NOW)                # a writer that never claimed
    assert st.status(ALICE)["limit"] == 400 and carry(wd)["state"] == "claiming"
    out = sc.activate_with_carryover(wd.plans, ALICE, "standard", NOW, NOW + D(days=30), "ls-sub:9001:rev1", 1, NOW, KEY0,
                                     NOW + D(days=30), {"claim_id": S_A, "sub_id": "9001", "free_period": FREE, "free_limit": 40})
    assert out == "foreign_entitled"
    assert sc.decide_zero(wd.plans, ALICE, S_A, NOW) is True
    m = carry(wd)
    assert m["state"] == "decided" and m["no_bonus"] is True
    s = st.status(ALICE)
    # a manual (calendar-month) grant shares the month's counter: the 20 already spent in March still count, no bonus is added
    assert (s["limit"], s["remaining"]) == (400, 380) and sc.carryover_bonus(plan(wd), usage(wd), KEY0) == 0
    inv_ok(wd)


def test_the_claim_never_touches_billing_fields_and_billing_writes_never_touch_the_claim(cfg, wd):
    free_charge(wd, 20)
    sc.set_plan_entitlement(wd.plans, ALICE, "standard", NOW - D(days=1), NOW + D(days=29), "ls-sub:1:rev1", 1, NOW,
                            credit_period_key=KEY0, credit_period_ends=NOW + D(days=30), mark_undecided=True)
    base = {k: v for k, v in plan(wd).items() if k != "carryover"}
    assert plan(wd)["carryover"] == {"state": "undecided"}
    assert sc.claim_activation(wd.plans, ALICE, "a" * 32, NOW + D(days=40)) in ("claimed", "ineligible")
    # the row is entitling at NOW; at +40 days it is not: a claim is allowed and changes only carryover
    after = {k: v for k, v in plan(wd).items() if k != "carryover"}
    assert after == base and plan(wd)["carryover"]["state"] in ("claiming", "decided")
    held = dict(plan(wd)["carryover"])
    sc.set_plan_entitlement(wd.plans, ALICE, "business", NOW - D(days=1), NOW + D(days=59), "ls-sub:1:rev2", 2, NOW,
                            credit_period_key=KEY1, credit_period_ends=NOW + D(days=60))
    assert dict(plan(wd)["carryover"]) == held and plan(wd)["billing_rev"] == 2 and plan(wd)["plan"] == "business"


def test_a_claim_only_plan_row_is_inert(cfg, wd):
    free_charge(wd, 20)
    st = store(wd)
    assert sc.claim_activation(wd.plans, ALICE, "a" * 32, NOW) == "claimed"
    row = plan(wd)
    assert set(row) == {"_id", "owner_key", "carryover"} and "billing_rev" not in row
    assert sc.active_plan(row, NOW) is None and st.plan_for(ALICE, NOW) == "free"
    s = st.status(ALICE)
    assert (s["limit"], s["remaining"], s["plan"]) == (40, 20, "free")
    assert len(list(wd.plans.find({"owner_key": ALICE}))) == 1


def test_a_billing_write_between_the_claim_and_the_activation_is_not_lost_and_not_overwritten(cfg, wd):
    """A newer revision lands while the claim is held: the claimant is superseded, releases the claim, and nothing is overwritten."""
    free_charge(wd, 20)
    st = store(wd)
    S = "a" * 32
    sc.claim_activation(wd.plans, ALICE, S, NOW)
    st.freeze_free_period(ALICE, FREE, S, "9001", KEY0, NOW)
    sc.set_plan_entitlement(wd.plans, ALICE, "standard", NOW - D(days=2), NOW - D(days=1), "ls-sub:9001:rev5", 5, NOW)   # closing write, rev 5
    row = dict(plan(wd))
    assert row["carryover"]["state"] == "claiming" and row["billing_rev"] == 5
    out = sc.activate_with_carryover(wd.plans, ALICE, "standard", NOW, NOW + D(days=30), "ls-sub:9001:rev2", 2, NOW, KEY0,
                                     NOW + D(days=30), {"claim_id": S, "sub_id": "9001", "free_period": FREE, "free_limit": 40})
    assert out == "superseded" and plan(wd)["billing_rev"] == 5 and plan(wd)["plan"] == "standard"
    st.release_activation(ALICE, S, FREE)
    assert plan(wd)["carryover"]["state"] == "undecided" and not usage(wd).get("freeze")
    assert st.status(ALICE)["limit"] == 40                                                 # the closed window leaves the free allowance
    inv_ok(wd)


# ═════════════════════ 9. activation vs upgrade / renewal: the state after EVERY individual write ═════════════════════
def _watch_plan_inputs(monkeypatch, on_call):
    real = billing._plan_inputs
    calls = {"n": 0, "busy": False}

    def wrapped(subs, owner, now):
        if calls["busy"]:                                  # a nested delivery made BY the hook: not one of the outer flow's reads
            return real(subs, owner, now)
        calls["n"] += 1
        calls["busy"] = True
        try:
            return on_call(calls["n"], real, subs, owner, now)
        finally:
            calls["busy"] = False
    monkeypatch.setattr(billing, "_plan_inputs", wrapped)
    return calls


def test_activation_vs_upgrade_asserts_every_write_not_only_the_retry(cfg, wd, monkeypatch):
    free_charge(wd, 20)
    st = store(wd)
    raw = created_raw()
    trace = []

    def snap(label):
        p = plan(wd) or {}
        s = st.status(ALICE)
        trace.append((label, p.get("plan"), p.get("billing_rev"), (p.get("carryover") or {}).get("state"), s["limit"], s["used"], s["remaining"]))

    upgrade = sub_change(V_BIZ, "business", NOW + D(seconds=5))
    held = {"stale": None}

    def on_call(n, real, subs, owner, now):
        if n == 1:                                                     # recompute's own read (W1)
            snap("W0 before any plan write")
            return real(subs, owner, now)
        if n == 2:                                                     # the pre-write re-read: serve STALE data, then the upgrade lands
            code, d = real(subs, owner, now)
            held["stale"] = (code, d)
            snap("W3 claim + freeze written, plan still inert")
            with pytest.raises(billing.WebhookBusy):                   # the upgrade event: applied to the subscription, plan write deferred
                deliver_c(wd, upgrade, NOW + D(seconds=5), credits=store(wd, NOW + D(seconds=5)))
            assert sub_doc(wd)["rev"] == 2 and sub_doc(wd)["variant_id"] == V_BIZ
            snap("W5 upgrade deferred: plan row unchanged")
            return held["stale"]
        snap("W7 post-write verify sees the newer subscription" if n == 3 else "W7b second verify finds nothing newer")
        return real(subs, owner, now)
    _watch_plan_inputs(monkeypatch, on_call)
    out = deliver_c(wd, raw, NOW, credits=st)
    snap("W8 final")
    assert out["status"] == "applied"
    labels = {t[0]: t[1:] for t in trace}
    assert labels["W0 before any plan write"][3:] == (40, 20, 20)                      # free allowance
    assert labels["W3 claim + freeze written, plan still inert"][0] is None and labels["W3 claim + freeze written, plan still inert"][3:] == (40, 20, 20)
    assert labels["W5 upgrade deferred: plan row unchanged"][0] is None
    # the activation write used the STALE (revision 1, Standard) data: a valid earlier state, limit 420
    assert labels["W7 post-write verify sees the newer subscription"] == ("standard", 1, "decided", 420, 0, 420)
    assert labels["W7b second verify finds nothing newer"] == ("business", 2, "decided", 1520, 0, 1520)   # corrected by the first verify
    # ... and the writer itself corrected it, without waiting for the provider's retry
    assert labels["W8 final"] == ("business", 2, "decided", 1520, 0, 1520)
    assert carry(wd)["period_key"] == KEY0 and entry_of(wd)["bonus"] == 20
    again = deliver_c(wd, upgrade, NOW + D(seconds=5), credits=store(wd, NOW + D(seconds=5)))   # the provider's retry of the upgrade
    assert again["status"] in ("stale", "duplicate", "applied") and store(wd).status(ALICE)["limit"] == 1520
    inv_ok(wd)


def sub_doc(wd, sid="9001"):
    return wd.subs.find_one({"_id": sid})


def test_activation_vs_upgrade_if_the_writer_dies_after_the_stale_write_the_retry_corrects_it(cfg, wd, monkeypatch):
    free_charge(wd, 20)
    st = store(wd)
    raw = created_raw()
    upgrade = sub_change(V_BIZ, "business", NOW + D(seconds=5))
    real = billing._plan_inputs
    state = {"n": 0, "stale": None}

    def on_call(n, real_fn, subs, owner, now):
        if n == 2:
            code, d = real_fn(subs, owner, now)
            state["stale"] = (code, d)
            with pytest.raises(billing.WebhookBusy):
                deliver_c(wd, upgrade, NOW + D(seconds=5), credits=store(wd, NOW + D(seconds=5)))
            return state["stale"]
        if n == 3:
            raise RuntimeError("the worker died right after the activation write")
        return real_fn(subs, owner, now)
    _watch_plan_inputs(monkeypatch, on_call)
    with pytest.raises(RuntimeError):
        deliver_c(wd, raw, NOW, credits=st)
    monkeypatch.setattr(billing, "_plan_inputs", real)
    p = plan(wd)
    assert (p["plan"], p["billing_rev"], p["carryover"]["state"]) == ("standard", 1, "decided")
    assert store(wd).status(ALICE)["limit"] == 420                                         # the documented lag
    out = deliver_c(wd, upgrade, NOW + D(seconds=5), credits=store(wd, NOW + D(seconds=5)))   # the provider retries the deferred event
    assert out["status"] == "applied"
    assert plan(wd)["plan"] == "business" and plan(wd)["billing_rev"] >= 2 and store(wd).status(ALICE)["limit"] == 1520
    assert entry_of(wd)["bonus"] == 20
    inv_ok(wd)


def test_activation_vs_renewal_never_attaches_the_bonus_to_the_wrong_period(cfg, wd, monkeypatch):
    free_charge(wd, 20)
    st = store(wd)
    raw = created_raw()
    sub_raw = sub_event("subscription_updated", updated=T1, renews=T1 + D(days=30))
    renewal = inv("subscription_payment_success", inv_id="77", created=T1)               # a paid renewal invoice: a NEW period key
    monkeypatch.setattr(sc._cfg(), "ACTIVATION_CLAIM_STALE_SECONDS", 10 ** 9, raising=False)              # the renewal's clock is 30 days on; keep the claim live

    def on_call(n, real_fn, subs, owner, now):
        if n == 2:                                                      # the pre-write re-read: the period moved
            with pytest.raises(billing.WebhookBusy):
                deliver_c(wd, renewal, T1, get=Fresh(sub_raw), credits=store(wd, T1))
            assert sub_doc(wd)["period_key"] == KEY1
        return real_fn(subs, owner, now)
    real_inputs = billing._plan_inputs
    _watch_plan_inputs(monkeypatch, on_call)
    with pytest.raises(billing.WebhookBusy):                            # the writer sees P' != P: it abandons and releases
        deliver_c(wd, raw, NOW, credits=st)
    assert plan(wd)["carryover"]["state"] == "undecided" and not usage(wd).get("freeze") and plan(wd).get("plan") is None
    monkeypatch.setattr(billing, "_plan_inputs", real_inputs)
    # the provider retries the deferred renewal event (the older `created` event is now stale and writes nothing)
    out = deliver_c(wd, renewal, T1, get=Fresh(sub_raw), credits=store(wd, T1))                # stages for the CURRENT first period
    assert out["status"] == "applied" and out.get("plan_record") == "applied"
    m = carry(wd)
    assert m["period_key"] == plan(wd)["credit_period_key"] == KEY1
    # the retry runs on the renewal's own clock (April), so the free month it snapshots is April (nothing spent there): bonus 40
    assert m["free_period"] == "2026-04" and entry_of(wd, KEY1)["bonus"] == 40
    assert sc.carryover_bonus(plan(wd), usage(wd), KEY0) == 0           # no bonus under the old key
    inv_ok(wd)


def test_a_stale_activation_after_a_takeover_leaves_the_winners_plan_untouched(cfg, wd):
    free_charge(wd, 20)
    S_A = "a" * 32
    sc.claim_activation(wd.plans, ALICE, S_A, NOW)
    store(wd).freeze_free_period(ALICE, FREE, S_A, "9001", KEY0, NOW)
    later = NOW + D(seconds=200)
    newer = sub_event("subscription_created", updated=later, renews=later + D(days=30))
    assert deliver_c(wd, newer, later, credits=store(wd, later))["status"] == "applied"
    winner = (plan(wd)["billing_rev"], plan(wd)["credit_period_key"], dict(carry(wd)))
    out = sc.activate_with_carryover(wd.plans, ALICE, "standard", NOW, NOW + D(days=30), "ls-sub:9001:rev0", 1, later, KEY0,
                                     NOW + D(days=30), {"claim_id": S_A, "sub_id": "9001", "free_period": FREE, "free_limit": 40})
    assert out == "lost" and (plan(wd)["billing_rev"], plan(wd)["credit_period_key"], dict(carry(wd))) == winner


# ═════════════════════ 10. the webhook route hands the credit store to billing ═════════════════════
def test_without_a_credit_store_billing_behaves_exactly_as_before(cfg, wd):
    free_charge(wd, 20)
    out = deliver_c(wd, created_raw(), NOW, credits=None)
    assert out["status"] == "applied"
    assert "carryover" not in plan(wd) and not usage(wd).get("freeze")
    assert store(wd).status(ALICE)["limit"] == 400
