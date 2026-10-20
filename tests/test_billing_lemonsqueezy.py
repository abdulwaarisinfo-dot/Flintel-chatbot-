"""
tests/test_billing_lemonsqueezy.py — Lemon Squeezy checkout + webhook -> subscription -> plan entitlement.

EVERY test here is "unit (mocked provider), not live": mongomock collections, synthetic payloads built in
this file, a fake clock and injected HTTP callables. No real Lemon Squeezy account, no network, no real
charge, no credentials (the key / secret below are made-up test strings). What these tests can NOT show is
that Lemon Squeezy's real payloads and checkout API look exactly like the documented shapes used here —
that needs a test-mode run (see README "Billing").
"""
import asyncio
import hashlib
import hmac
import json
import sys
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest
from starlette.requests import Request

from tests.test_answer_persistence import env  # noqa: F401  (fixture: stubbed app modules)

mongomock = pytest.importorskip("mongomock")
import billing  # noqa: E402
import search_credits as sc  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
SECRET, API_KEY, STORE = "whsec_test_not_a_real_secret", "lsq_test_not_a_real_key", "4242"
V_STD, V_BIZ = "1001", "2002"
ALICE, BOB = "alice@example.test", "bob@example.test"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


@pytest.fixture
def cfg(monkeypatch):
    """Billing configuration for billing.py / search_credits.py (they read config at call time)."""
    ns = types.SimpleNamespace(
        LEMONSQUEEZY_API_KEY=API_KEY, LEMONSQUEEZY_STORE_ID=STORE, LEMONSQUEEZY_WEBHOOK_SECRET=SECRET,
        LEMONSQUEEZY_VARIANT_ID_STANDARD=V_STD, LEMONSQUEEZY_VARIANT_ID_BUSINESS=V_BIZ, LEMONSQUEEZY_TEST_MODE=False)
    monkeypatch.setattr(billing, "_cfg", lambda: ns)
    monkeypatch.setattr(sc, "_cfg", lambda: ns)
    return ns


@pytest.fixture
def db():
    d = mongomock.MongoClient()["billing"]
    d.plans.create_index("owner_key", unique=True)
    return types.SimpleNamespace(subs=d.subs, events=d.events, plans=d.plans)


def sign(raw):
    return hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def custom(owner=ALICE, plan="standard"):
    return {"owner": owner, "plan": plan, "sig": billing._owner_sig(owner, plan)}


def sub_event(name="subscription_created", sid="9001", status="active", variant=V_STD, owner=ALICE, claim_plan="standard",
              renews=None, ends=None, updated=None, store=STORE, test=False, custom_data="auto", cancelled=False,
              created=None, portal="https://acme.lemonsqueezy.com/billing?x=1"):
    renews = renews or NOW + timedelta(days=31)
    updated = updated or NOW
    meta = {"event_name": name, "test_mode": test, "webhook_id": "w1"}
    if custom_data == "auto":
        meta["custom_data"] = custom(owner, claim_plan)
    elif custom_data is not None:
        meta["custom_data"] = custom_data
    attrs = {"store_id": int(store), "customer_id": 77, "order_id": 55, "variant_id": int(variant), "status": status,
             "cancelled": cancelled, "renews_at": iso(renews), "ends_at": iso(ends) if ends else None,
             "created_at": iso(created or NOW - timedelta(days=1)), "updated_at": iso(updated),
             "test_mode": test, "urls": {"customer_portal": portal}}
    return json.dumps({"meta": meta, "data": {"type": "subscriptions", "id": sid, "attributes": attrs}}).encode()


def invoice_event(name="subscription_payment_success", sid="9001", store=STORE, updated=None, inv_id="31", created=None):
    attrs = {"store_id": int(store), "subscription_id": int(sid), "status": "paid", "billing_reason": "renewal",
             "created_at": iso(created or updated or NOW), "updated_at": iso(updated or NOW)}
    return json.dumps({"meta": {"event_name": name, "test_mode": False}, "data": {"type": "subscription-invoices", "id": inv_id,
                                                                                   "attributes": attrs}}).encode()


def deliver(db, raw, now=NOW, signature="auto", header=None, get=None):
    sig = sign(raw) if signature == "auto" else signature
    return billing.handle_webhook(raw, sig, header, db.subs, db.events, db.plans, now=now, get=get)


def store_for(db, now=NOW):
    return sc.CreditStore(None, None, None, None, clock=lambda: now, enabled=lambda: True, limit=lambda: 40, plans=db.plans)


def plan_record(db, owner=ALICE):
    return db.plans.find_one({"owner_key": owner})


def snapshot(db):
    return ({d["_id"]: d for d in db.subs.find()}, {d["_id"]: d for d in db.events.find()}, [d for d in db.plans.find()])


# ── configuration and plan -> variant mapping ────────────────────────────────
def test_plan_to_variant_mapping_is_server_side_only(cfg):
    assert billing.variant_id_for("standard") == V_STD and billing.variant_id_for("business") == V_BIZ
    assert billing.plan_for_variant(V_STD) == "standard" and billing.plan_for_variant(int(V_BIZ)) == "business"
    assert billing.variant_id_for("free") == "" and billing.variant_id_for("enterprise") == ""
    for unknown in ("999", "", None, True, "1001 "[:-1] + "0"):
        assert billing.plan_for_variant(unknown) is None


def test_catalog_has_the_required_prices_and_credits(cfg):
    catalog = {p["plan"]: p for p in billing.plan_catalog()}
    assert (catalog["standard"]["price_usd"], catalog["standard"]["credits"]) == (19, 400)
    assert (catalog["business"]["price_usd"], catalog["business"]["credits"]) == (59, 1500)


@pytest.mark.parametrize("missing", ["LEMONSQUEEZY_API_KEY", "LEMONSQUEEZY_STORE_ID", "LEMONSQUEEZY_WEBHOOK_SECRET",
                                     "LEMONSQUEEZY_VARIANT_ID_STANDARD", "LEMONSQUEEZY_VARIANT_ID_BUSINESS"])
def test_billing_is_not_configured_when_any_setting_is_missing(cfg, missing):
    assert billing.configured() is True
    setattr(cfg, missing, "")
    assert billing.configured() is False


def test_identical_variant_ids_disable_billing(cfg):
    cfg.LEMONSQUEEZY_VARIANT_ID_BUSINESS = V_STD
    assert billing.configured() is False


# ── checkout creation ────────────────────────────────────────────────────────
class Resp:
    def __init__(self, status=201, body=None, bad_json=False):
        self.status_code, self._body, self._bad = status, body, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


GOOD_URL = "https://acme.lemonsqueezy.com/checkout/custom/abc?signature=zzz"


def good_response():
    return Resp(201, {"data": {"attributes": {"url": GOOD_URL}}})


def test_checkout_request_is_built_from_server_side_values_only(cfg):
    seen = {}

    def post(url, payload, headers):
        seen.update(url=url, payload=payload, headers=headers)
        return good_response()
    url = billing.create_checkout(ALICE, "business", "https://app.example/billing/upgrade?checkout=success", post=post)
    assert url == GOOD_URL
    attrs = seen["payload"]["data"]["attributes"]
    rel = seen["payload"]["data"]["relationships"]
    assert seen["url"] == "https://api.lemonsqueezy.com/v1/checkouts"
    assert seen["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert rel["variant"]["data"]["id"] == V_BIZ and rel["store"]["data"]["id"] == STORE
    assert attrs["product_options"]["enabled_variants"] == [int(V_BIZ)]
    assert attrs["checkout_data"]["email"] == ALICE
    assert attrs["checkout_data"]["custom"] == custom(ALICE, "business")
    assert attrs["test_mode"] is False
    flat = json.dumps(seen["payload"])
    assert "price" not in flat and "credits" not in flat


def test_checkout_rejects_plans_that_are_not_allow_listed(cfg):
    post = lambda *a: pytest.fail("the provider must not be called")  # noqa: E731
    for bad in ("free", "enterprise", "", None, V_STD, "standard ", "STANDARD"):
        with pytest.raises(ValueError):
            billing.create_checkout(ALICE, bad, "https://app.example/x", post=post)


def test_checkout_needs_a_registered_email_owner(cfg):
    for guest in ("a1b2c3d4e5f6", "", None):
        with pytest.raises(ValueError):
            billing.create_checkout(guest, "standard", "https://app.example/x", post=lambda *a: pytest.fail("called"))


def test_checkout_not_configured_never_calls_the_provider(cfg):
    cfg.LEMONSQUEEZY_API_KEY = ""
    with pytest.raises(billing.BillingNotConfigured):
        billing.create_checkout(ALICE, "standard", "https://app.example/x", post=lambda *a: pytest.fail("called"))


@pytest.mark.parametrize("response", [
    Resp(500, {}), Resp(401, {"errors": [{"detail": "SECRET-LOOKING-TEXT"}]}), Resp(422, {}), Resp(201, None, bad_json=True),
    Resp(201, {"data": {}}), Resp(201, {"data": {"attributes": {"url": "http://acme.lemonsqueezy.com/x"}}}),
    Resp(201, {"data": {"attributes": {"url": "https://evil.example/checkout"}}}),
    Resp(201, {"data": {"attributes": {"url": "https://lemonsqueezy.com.evil.example/x"}}}),
])
def test_checkout_provider_failures_become_a_safe_error(cfg, response):
    with pytest.raises(billing.CheckoutError) as err:
        billing.create_checkout(ALICE, "standard", "https://app.example/x", post=lambda *a: response)
    assert "SECRET" not in err.value.public and "SECRET" not in str(err.value)


def test_checkout_network_failure_becomes_a_safe_error(cfg):
    def boom(*a):
        raise TimeoutError("connect timeout to 10.0.0.1 with key " + API_KEY)
    with pytest.raises(billing.CheckoutError) as err:
        billing.create_checkout(ALICE, "standard", "https://app.example/x", post=boom)
    assert API_KEY not in str(err.value) and API_KEY not in err.value.public


def test_checkout_uses_test_mode_when_configured(cfg):
    cfg.LEMONSQUEEZY_TEST_MODE = True
    seen = {}
    billing.create_checkout(ALICE, "standard", "https://app.example/x", post=lambda u, p, h: seen.update(p=p) or good_response())
    assert seen["p"]["data"]["attributes"]["test_mode"] is True


# ── signature verification ───────────────────────────────────────────────────
def test_signature_valid_and_invalid(cfg):
    raw = b'{"meta": {}}'
    assert billing.verify_signature(raw, sign(raw)) is True
    assert billing.verify_signature(raw, sign(raw).upper()) is True            # hex is case-insensitive
    assert billing.verify_signature(raw + b" ", sign(raw)) is False              # body changed after signing
    assert billing.verify_signature(raw, sign(raw)[:-1] + ("0" if sign(raw)[-1] != "0" else "1")) is False
    for bad in (None, "", "   ", "zz", "é" * 64, 123):
        assert billing.verify_signature(raw, bad) is False
    cfg.LEMONSQUEEZY_WEBHOOK_SECRET = ""
    assert billing.verify_signature(raw, sign(raw)) is False                      # no secret: nothing verifies


def test_invalid_signature_changes_no_state(cfg, db):
    raw = sub_event()
    for sig in (None, "", "0" * 64, sign(raw + b"x")):
        with pytest.raises(billing.WebhookRejected) as err:
            deliver(db, raw, signature=sig)
        assert err.value.http_status == 401
    assert snapshot(db) == ({}, {}, [])


def test_webhook_refuses_everything_when_billing_is_not_configured(cfg, db):
    cfg.LEMONSQUEEZY_WEBHOOK_SECRET = ""
    with pytest.raises(billing.WebhookRejected) as err:
        deliver(db, sub_event(), signature="anything")
    assert err.value.http_status == 503 and snapshot(db) == ({}, {}, [])


# ── unknown / malformed events ───────────────────────────────────────────────
@pytest.mark.parametrize("raw", [b"not json", b"[]", b"{}", b'{"meta": {}, "data": {}}', b'{"meta": {"event_name": 5}, "data": {}}',
                                 b'{"meta": [], "data": {}}', b"\xff\xfe"])
def test_malformed_signed_payloads_are_rejected_without_state(cfg, db, raw):
    with pytest.raises(billing.WebhookRejected) as err:
        deliver(db, raw)
    assert err.value.http_status == 400 and snapshot(db) == ({}, {}, [])


def test_unknown_events_are_acknowledged_and_ignored(cfg, db):
    for name in ("order_created", "license_key_created", "affiliate_activated", "something_new"):
        raw = json.dumps({"meta": {"event_name": name}, "data": {"type": "orders", "id": "1", "attributes": {}}}).encode()
        assert deliver(db, raw)["status"] == "ignored"
    assert snapshot(db) == ({}, {}, [])


def test_event_name_header_must_match_the_payload(cfg, db):
    with pytest.raises(billing.WebhookRejected) as err:
        deliver(db, sub_event("subscription_created"), header="subscription_expired")
    assert err.value.http_status == 400 and snapshot(db) == ({}, {}, [])
    assert deliver(db, sub_event("subscription_created"), header="subscription_created")["status"] == "applied"


@pytest.mark.parametrize("kw", [
    dict(store="9999"), dict(variant="31337"), dict(status="mystery"), dict(test=True), dict(custom_data=None),
    dict(custom_data={"owner": ALICE, "plan": "standard", "sig": "0" * 40}),       # forged owner signature
    dict(custom_data={"owner": ALICE, "plan": "standard"}),
    dict(custom_data={"owner": "guestuuid", "plan": "standard", "sig": "x"}),
])
def test_events_that_are_not_ours_or_not_attributable_grant_nothing(cfg, db, kw):
    result = deliver(db, sub_event(**kw))
    assert result["status"] == "ignored"
    assert db.plans.count_documents({}) == 0 and db.subs.count_documents({}) == 0


def test_a_signature_made_for_one_user_cannot_be_used_for_another(cfg, db):
    stolen = {"owner": BOB, "plan": "standard", "sig": custom(ALICE, "standard")["sig"]}
    assert deliver(db, sub_event(owner=BOB, custom_data=stolen))["status"] == "ignored"
    assert db.plans.count_documents({}) == 0


def test_live_events_are_ignored_in_test_mode_and_vice_versa(cfg, db):
    cfg.LEMONSQUEEZY_TEST_MODE = True
    assert deliver(db, sub_event(test=False))["status"] == "ignored"
    assert deliver(db, sub_event(test=True))["status"] == "applied"


# ── activation, credits ──────────────────────────────────────────────────────
def test_standard_activation_gives_400_credits_to_the_right_user(cfg, db):
    assert store_for(db).limit_for(ALICE) == 40                       # free before
    assert deliver(db, sub_event())["status"] == "applied"
    rec = plan_record(db)
    assert rec["plan"] == "standard" and sc.active_plan(rec, NOW) == "standard"
    assert store_for(db).plan_for(ALICE, owner_type="email") == "standard" and store_for(db).limit_for(ALICE) == 400
    assert store_for(db).limit_for(BOB) == 40 and db.plans.count_documents({"owner_key": BOB}) == 0
    sub = db.subs.find_one({"_id": "9001"})
    assert sub["owner_key"] == ALICE and sub["status"] == "active" and sub["variant_id"] == V_STD and "card" not in json.dumps(sub, default=str)


def test_business_activation_gives_1500_credits(cfg, db):
    deliver(db, sub_event(sid="9002", variant=V_BIZ, claim_plan="business"))
    assert plan_record(db)["plan"] == "business" and store_for(db).limit_for(ALICE) == 1500


def test_plan_comes_from_the_variant_not_from_the_custom_data(cfg, db):
    deliver(db, sub_event(variant=V_STD, claim_plan="business"))      # the claim says business, the paid variant is standard
    assert plan_record(db)["plan"] == "standard"


def test_plan_record_matches_the_shape_the_audit_and_active_plan_expect(cfg, db):
    deliver(db, sub_event())
    rec = plan_record(db)
    assert rec["source_ref"] in rec["applied_refs"] and all(isinstance(r, str) and r.strip() for r in rec["applied_refs"])
    assert rec["active_until"] > rec["active_from"] and isinstance(rec["updated_at"], datetime)


def test_renewal_extends_the_period_once_and_credits_stay_the_monthly_allowance(cfg, db):
    deliver(db, sub_event(renews=NOW + timedelta(days=31), updated=NOW))
    later = NOW + timedelta(days=31)
    renewal = sub_event("subscription_updated", renews=later + timedelta(days=30), updated=later)
    assert deliver(db, renewal, now=later)["status"] == "applied"
    rec = plan_record(db)
    assert sc._aware(rec["active_until"]) == later + timedelta(days=30)
    assert store_for(db, later).limit_for(ALICE) == 400                # monthly allowance, not 800
    before = snapshot(db)
    assert deliver(db, renewal, now=later)["status"] == "duplicate"    # the retry changes nothing
    assert snapshot(db) == before


def test_payment_success_refreshes_from_the_provider_and_extends_exactly_once(cfg, db):
    deliver(db, sub_event(renews=NOW + timedelta(days=31), updated=NOW))
    later = NOW + timedelta(days=31)
    calls = []

    def get(url, headers):
        calls.append(url)
        data = json.loads(sub_event("subscription_updated", renews=later + timedelta(days=30), updated=later))["data"]
        return Resp(200, {"data": data})
    raw = invoice_event("subscription_payment_success", updated=later)
    assert deliver(db, raw, now=later, get=get)["status"] == "applied"
    assert sc._aware(plan_record(db)["active_until"]) == later + timedelta(days=30)
    assert calls == ["https://api.lemonsqueezy.com/v1/subscriptions/9001"]
    before = snapshot(db)
    assert deliver(db, raw, now=later, get=get)["status"] == "duplicate" and len(calls) == 1
    assert snapshot(db) == before


def test_payment_success_with_a_failing_refresh_is_retried_not_lost(cfg, db):
    deliver(db, sub_event())
    raw = invoice_event("subscription_payment_success")
    with pytest.raises(sc.CreditStoreError):
        deliver(db, raw, get=lambda u, h: Resp(503, {}))
    assert db.events.find_one({"state": "failed"}) is not None
    good = lambda u, h: Resp(200, {"data": json.loads(sub_event("subscription_updated", renews=NOW + timedelta(days=60), updated=NOW + timedelta(hours=1)))["data"]})  # noqa: E731
    assert deliver(db, raw, get=good)["status"] == "applied"           # the retry is processed, not treated as a duplicate
    assert db.events.find_one({"state": "done", "event": "subscription_payment_success"}) is not None


def test_failed_payment_creates_no_entitlement_and_does_not_extend(cfg, db):
    deliver(db, sub_event(renews=NOW + timedelta(days=31)))
    due = NOW + timedelta(days=31)
    deliver(db, invoice_event("subscription_payment_failed", updated=due), now=due)
    # the provider then marks the subscription past_due and (wrongly or not) shows a later renews_at: no extension for a non-paying status
    deliver(db, sub_event("subscription_updated", status="past_due", renews=due + timedelta(days=30), updated=due + timedelta(minutes=5)),
            now=due + timedelta(minutes=5))
    rec = plan_record(db)
    assert sc._aware(rec["active_until"]) == due                        # exactly what was already paid for
    assert store_for(db, due + timedelta(hours=1)).limit_for(ALICE) == 40
    assert db.subs.find_one({"_id": "9001"})["last_payment_failed_at"] is not None


def test_a_subscription_that_never_paid_gets_nothing(cfg, db):
    deliver(db, sub_event(status="unpaid"))
    assert db.plans.count_documents({}) == 0 and store_for(db).limit_for(ALICE) == 40
    deliver(db, sub_event("subscription_updated", status="past_due", updated=NOW + timedelta(minutes=1)), now=NOW + timedelta(minutes=1))
    assert db.plans.count_documents({}) == 0


def test_cancellation_keeps_access_until_the_end_of_the_paid_period(cfg, db):
    end = NOW + timedelta(days=20)
    deliver(db, sub_event(renews=end))
    deliver(db, sub_event("subscription_cancelled", status="cancelled", cancelled=True, renews=end, ends=end, updated=NOW + timedelta(minutes=1)),
            now=NOW + timedelta(minutes=1))
    assert store_for(db, NOW + timedelta(days=19)).limit_for(ALICE) == 400
    assert store_for(db, end + timedelta(seconds=1)).limit_for(ALICE) == 40
    state = billing.status_for_owner(db.subs, db.plans, ALICE, now=NOW + timedelta(days=1))
    assert state["plan"] == "standard" and state["cancelled"] is True and state["ends_at"].startswith(iso(end)[:10])


def test_resuming_a_cancelled_subscription_restores_access(cfg, db):
    end = NOW + timedelta(days=20)
    deliver(db, sub_event(renews=end))
    deliver(db, sub_event("subscription_cancelled", status="cancelled", cancelled=True, ends=end, updated=NOW + timedelta(minutes=1)), now=NOW + timedelta(minutes=1))
    deliver(db, sub_event("subscription_resumed", status="active", renews=end + timedelta(days=30), updated=NOW + timedelta(minutes=2)), now=NOW + timedelta(minutes=2))
    assert store_for(db, end + timedelta(days=1)).limit_for(ALICE) == 400


def test_expiry_ends_access_immediately_and_returns_to_the_free_plan(cfg, db):
    deliver(db, sub_event(renews=NOW + timedelta(days=31)))
    t = NOW + timedelta(days=2)
    deliver(db, sub_event("subscription_expired", status="expired", ends=t - timedelta(hours=1), updated=t), now=t)
    assert store_for(db, t).plan_for(ALICE, owner_type="email") == "free" and store_for(db, t).limit_for(ALICE) == 40
    rec = plan_record(db)
    assert sc._aware(rec["active_until"]) > sc._aware(rec["active_from"]) and sc._aware(rec["active_until"]) <= t


def test_access_lapses_by_itself_if_no_further_event_ever_arrives(cfg, db):
    deliver(db, sub_event(renews=NOW + timedelta(days=31)))
    assert store_for(db, NOW + timedelta(days=30)).limit_for(ALICE) == 400
    assert store_for(db, NOW + timedelta(days=32)).limit_for(ALICE) == 40


def test_far_future_renewal_dates_are_clamped_to_one_billing_period(cfg, db):
    deliver(db, sub_event(renews=NOW + timedelta(days=3650)))
    assert sc._aware(plan_record(db)["active_until"]) <= NOW + timedelta(days=billing.MAX_PERIOD_DAYS)


# ── idempotency, ordering, concurrency ───────────────────────────────────────
def test_duplicate_delivery_is_a_no_op(cfg, db):
    raw = sub_event()
    assert deliver(db, raw)["status"] == "applied"
    before = snapshot(db)
    for _ in range(3):
        assert deliver(db, raw)["status"] == "duplicate"
    assert snapshot(db) == before
    assert db.subs.find_one({"_id": "9001"})["rev"] == 1 and len(plan_record(db)["applied_refs"]) == 1


def test_stale_events_never_overwrite_newer_state(cfg, db):
    t1, t2, t3 = NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2)
    deliver(db, sub_event("subscription_created", renews=NOW + timedelta(days=31), updated=t1), now=t3)
    deliver(db, sub_event("subscription_updated", variant=V_BIZ, renews=NOW + timedelta(days=31), updated=t3), now=t3)
    assert plan_record(db)["plan"] == "business"
    old = sub_event("subscription_updated", variant=V_STD, status="expired", ends=t2, updated=t2)   # older than t3, arrives late
    assert deliver(db, old, now=t3)["status"] == "stale"
    assert plan_record(db)["plan"] == "business" and store_for(db, t3).limit_for(ALICE) == 1500
    assert db.subs.find_one({"_id": "9001"})["status"] == "active"


def test_an_update_arriving_before_the_creation_event_is_handled(cfg, db):
    deliver(db, sub_event("subscription_updated", updated=NOW + timedelta(minutes=5)), now=NOW + timedelta(minutes=5))
    assert plan_record(db)["plan"] == "standard"
    assert deliver(db, sub_event("subscription_created", updated=NOW), now=NOW + timedelta(minutes=6))["status"] == "stale"
    assert db.subs.count_documents({}) == 1


def test_same_delivery_in_flight_elsewhere_is_told_to_retry(cfg, db):
    raw = sub_event()
    db.events.insert_one({"_id": hashlib.sha256(raw).hexdigest(), "event": "subscription_created", "state": "processing",
                          "claimed_at": NOW, "attempts": 1})
    with pytest.raises(billing.WebhookBusy):
        deliver(db, raw, now=NOW + timedelta(seconds=5))
    assert snapshot(db)[0] == {} and db.plans.count_documents({}) == 0
    assert deliver(db, raw, now=NOW + timedelta(seconds=billing.STALE_PROCESSING_SECONDS + 5))["status"] == "applied"   # abandoned claim is taken over


def test_a_crash_after_the_claim_is_retried_and_does_not_double_apply(cfg, db, monkeypatch):
    raw = sub_event()
    real = billing.recompute
    monkeypatch.setattr(billing, "recompute", lambda *a, **k: (_ for _ in ()).throw(sc.CreditStoreError("db down")))
    with pytest.raises(sc.CreditStoreError):
        deliver(db, raw)
    assert db.events.find_one({"state": "failed"}) is not None and db.plans.count_documents({}) == 0
    monkeypatch.setattr(billing, "recompute", real)
    assert deliver(db, raw)["status"] in ("applied", "stale")          # subscription write was kept, plan record written now
    assert plan_record(db)["plan"] == "standard" and len(plan_record(db)["applied_refs"]) == 1
    assert deliver(db, raw)["status"] == "duplicate"


# ── ownership, plan switching ────────────────────────────────────────────────
def test_a_subscription_cannot_be_moved_to_another_user(cfg, db):
    deliver(db, sub_event(owner=ALICE))
    result = deliver(db, sub_event("subscription_updated", owner=BOB, updated=NOW + timedelta(minutes=1)), now=NOW + timedelta(minutes=1))
    assert result["status"] == "ignored"
    assert db.subs.find_one({"_id": "9001"})["owner_key"] == ALICE and db.plans.count_documents({"owner_key": BOB}) == 0


def test_events_without_custom_data_use_the_stored_owner(cfg, db):
    deliver(db, sub_event())
    deliver(db, sub_event("subscription_updated", custom_data=None, variant=V_BIZ, updated=NOW + timedelta(minutes=1)), now=NOW + timedelta(minutes=1))
    assert plan_record(db)["plan"] == "business"


def test_users_have_independent_plans(cfg, db):
    deliver(db, sub_event(sid="1", owner=ALICE, variant=V_STD))
    deliver(db, sub_event(sid="2", owner=BOB, variant=V_BIZ, claim_plan="business"))
    assert (store_for(db).limit_for(ALICE), store_for(db).limit_for(BOB)) == (400, 1500)
    deliver(db, sub_event("subscription_expired", sid="2", owner=BOB, variant=V_BIZ, claim_plan="business", status="expired", ends=NOW,
                          updated=NOW + timedelta(minutes=1)), now=NOW + timedelta(minutes=1))
    assert (store_for(db, NOW + timedelta(minutes=2)).limit_for(ALICE), store_for(db, NOW + timedelta(minutes=2)).limit_for(BOB)) == (400, 40)


def test_the_best_of_several_subscriptions_wins(cfg, db):
    deliver(db, sub_event(sid="1", variant=V_STD))
    deliver(db, sub_event(sid="2", variant=V_BIZ, claim_plan="business", updated=NOW + timedelta(seconds=1)), now=NOW + timedelta(seconds=1))
    assert plan_record(db)["plan"] == "business"
    deliver(db, sub_event("subscription_expired", sid="2", variant=V_BIZ, claim_plan="business", status="expired", ends=NOW,
                          updated=NOW + timedelta(seconds=2)), now=NOW + timedelta(seconds=2))
    assert plan_record(db)["plan"] == "standard"                        # falls back to the still-paid subscription


# ── the plan-record writer ────────────────────────────────────────────────────
def test_set_plan_entitlement_is_ordered_and_replaces_a_manual_record(db):
    sc.activate_plan(db.plans, ALICE, "business", NOW + timedelta(days=5), "manual-1", now=NOW)
    assert db.plans.find_one({"owner_key": ALICE}).get("billing_rev") is None
    a = (db.plans, ALICE, "standard", NOW, NOW + timedelta(days=30))
    assert sc.set_plan_entitlement(*a, "ls:1", 2, now=NOW) == "applied"
    assert sc.set_plan_entitlement(*a, "ls:old", 2, now=NOW) == "superseded"
    assert sc.set_plan_entitlement(*a, "ls:older", 1, now=NOW) == "superseded"
    rec = db.plans.find_one({"owner_key": ALICE})
    assert rec["source_ref"] == "ls:1" and rec["plan"] == "standard" and rec["billing_rev"] == 2
    assert sc.set_plan_entitlement(db.plans, ALICE, "standard", NOW, NOW + timedelta(days=60), "ls:2", 3, now=NOW) == "applied"
    assert db.plans.count_documents({"owner_key": ALICE}) == 1


@pytest.mark.parametrize("bad", [dict(plan="free"), dict(owner="guest-uuid"), dict(ref=""), dict(rev=0), dict(rev=True),
                                 dict(end_before_start=True)])
def test_set_plan_entitlement_validates_its_arguments(db, bad):
    args = dict(plan="standard", owner=ALICE, ref="r", rev=1)
    args.update(bad)
    end = NOW - timedelta(days=1) if bad.get("end_before_start") else NOW + timedelta(days=1)
    with pytest.raises(ValueError):
        sc.set_plan_entitlement(db.plans, args["owner"], args["plan"], NOW, end, args["ref"], args["rev"], now=NOW)
    assert db.plans.count_documents({}) == 0


def test_free_plan_and_existing_manual_activation_still_work(cfg, db):
    assert store_for(db).limit_for(ALICE) == 40 and store_for(db).limit_for("guest-uuid-1", owner_type="anon") == 40
    sc.activate_plan(db.plans, ALICE, "standard", NOW + timedelta(days=30), "manual-pay-1", now=NOW)
    assert store_for(db).limit_for(ALICE) == 400
    assert store_for(db, NOW + timedelta(days=31)).limit_for(ALICE) == 40


# ── routes (real handlers, stubbed app modules) ──────────────────────────────
def make_request(idx, path, method="POST", headers=None, body=b"", session=None):
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}
    hdrs = {"host": "testserver", **(headers or {})}
    scope = {"type": "http", "method": method, "path": path, "headers": [(k.lower().encode(), v.encode()) for k, v in hdrs.items()],
             "query_string": b"", "session": session if session is not None else {}, "app": idx.app, "router": idx.app.router,
             "server": ("testserver", 80), "scheme": "http"}
    return Request(scope, receive)


@pytest.fixture
def app(env, cfg, db, monkeypatch):
    lg, idx, _ = env
    rt = sys.modules["routes"]
    user = {"value": {"id": "u1", "email": ALICE}}
    monkeypatch.setattr(rt, "get_current_user", lambda request: user["value"])
    monkeypatch.setattr(rt, "_billing_stores", lambda: (db.subs, db.events, db.plans))
    posts = []

    def fake_post(url, payload, headers):
        posts.append(payload)
        return good_response()
    monkeypatch.setattr(billing, "_http_post", fake_post)
    monkeypatch.setattr(billing, "_utcnow", lambda: NOW)       # the routes read the clock inside billing.py
    return types.SimpleNamespace(rt=rt, idx=idx, user=user, posts=posts, db=db)


def body_json(resp):
    return json.loads(resp.body.decode())


def checkout(app, plan, **kw):
    return app.rt.billing_checkout(make_request(app.idx, "/api/billing/checkout", **kw), plan=plan)


def test_checkout_route_requires_login(app):
    app.user["value"] = None
    resp = checkout(app, "standard")
    assert resp.status_code == 401 and body_json(resp) == {"error": "not_authenticated"} and app.posts == []


def test_checkout_route_returns_a_checkout_url_for_the_signed_in_user_only(app):
    resp = checkout(app, "standard")
    assert resp.status_code == 200 and body_json(resp) == {"url": GOOD_URL}
    sent = app.posts[0]["data"]["attributes"]["checkout_data"]
    assert sent["email"] == ALICE and sent["custom"]["owner"] == ALICE
    app.user["value"] = {"id": "u2", "email": BOB}
    checkout(app, "business")
    assert app.posts[1]["data"]["attributes"]["checkout_data"]["custom"]["owner"] == BOB


@pytest.mark.parametrize("plan", ["free", "enterprise", "", V_STD, "Standard", "standard; DROP", "../standard"])
def test_checkout_route_rejects_unlisted_plans(app, plan):
    resp = checkout(app, plan)
    assert resp.status_code == 400 and body_json(resp) == {"error": "invalid_plan"} and app.posts == []


def test_checkout_route_blocks_cross_site_posts(app):
    resp = checkout(app, "standard", headers={"origin": "https://evil.example"})
    assert resp.status_code == 403 and app.posts == []
    assert checkout(app, "standard", headers={"origin": "http://testserver"}).status_code == 200


def test_checkout_route_when_billing_is_not_configured(app, cfg):
    cfg.LEMONSQUEEZY_WEBHOOK_SECRET = ""
    resp = checkout(app, "standard")
    assert resp.status_code == 503 and body_json(resp) == {"error": "billing_unavailable"} and app.posts == []


def test_checkout_route_hides_provider_failures(app, monkeypatch):
    monkeypatch.setattr(billing, "_http_post", lambda u, p, h: Resp(401, {"errors": [{"detail": "Unauthenticated " + API_KEY}]}))
    resp = checkout(app, "standard")
    assert resp.status_code == 503 and body_json(resp)["error"] == "checkout_failed" and API_KEY not in resp.body.decode()
    monkeypatch.setattr(billing, "_http_post", lambda u, p, h: (_ for _ in ()).throw(TimeoutError(API_KEY)))
    resp = checkout(app, "standard")
    assert resp.status_code == 503 and API_KEY not in resp.body.decode()


def test_checkout_route_refuses_a_second_subscription(app):
    deliver(app.db, sub_event())
    resp = checkout(app, "business")
    assert resp.status_code == 409 and body_json(resp)["error"] == "already_subscribed"
    assert "portal_url" not in body_json(resp) and app.posts == []      # a signed 24h link is never stored or echoed


def test_status_route_is_private_to_the_signed_in_user(app):
    app.user["value"] = None
    assert app.rt.billing_status(make_request(app.idx, "/api/billing/status", "GET")).status_code == 401
    app.user["value"] = {"id": "u1", "email": ALICE}
    deliver(app.db, sub_event(sid="1", owner=ALICE, variant=V_STD))
    deliver(app.db, sub_event(sid="2", owner=BOB, variant=V_BIZ, claim_plan="business"))
    mine = body_json(app.rt.billing_status(make_request(app.idx, "/api/billing/status", "GET")))
    assert mine["plan"] == "standard" and mine["entitled"] is True
    app.user["value"] = {"id": "u2", "email": BOB}
    assert body_json(app.rt.billing_status(make_request(app.idx, "/api/billing/status", "GET")))["plan"] == "business"
    app.user["value"] = {"id": "u3", "email": "carol@example.test"}
    assert body_json(app.rt.billing_status(make_request(app.idx, "/api/billing/status", "GET")))["plan"] == "free"


def webhook(app, raw, signature="auto", name=None):
    headers = {"x-signature": sign(raw) if signature == "auto" else signature, "content-length": str(len(raw))}
    if signature is None:
        headers.pop("x-signature")
    if name:
        headers["x-event-name"] = name
    return run_async(app.rt.lemonsqueezy_webhook(make_request(app.idx, "/webhooks/lemonsqueezy", headers=headers, body=raw)))


def run_async(coro):
    """Run a coroutine on a private event loop in its own thread: other test modules can leave a loop
    running or closed in the main thread, which must not matter here."""
    box = {}

    def target():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:      # noqa: BLE001 - re-raised in the caller
            box["error"] = exc
    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_webhook_route_needs_no_login_but_needs_a_valid_signature(app):
    app.user["value"] = None
    raw = sub_event()
    assert webhook(app, raw, signature="0" * 64).status_code == 401
    assert webhook(app, raw, signature=None).status_code == 401
    assert snapshot(app.db) == ({}, {}, [])
    resp = webhook(app, raw)
    assert resp.status_code == 200 and body_json(resp) == {"status": "applied"}
    assert plan_record(app.db)["plan"] == "standard"
    resp = webhook(app, raw)
    assert resp.status_code == 200 and body_json(resp) == {"status": "duplicate"}


def test_webhook_route_acknowledges_unknown_events_and_rejects_garbage(app):
    ignored = json.dumps({"meta": {"event_name": "order_created"}, "data": {}}).encode()
    assert webhook(app, ignored).status_code == 200
    assert webhook(app, b"garbage").status_code == 400
    big = b"x" * (billing.MAX_BODY_BYTES + 1)
    assert webhook(app, big).status_code == 413


def test_webhook_route_asks_for_a_retry_on_busy_and_on_internal_errors(app, monkeypatch):
    monkeypatch.setattr(billing, "handle_webhook", lambda *a, **k: (_ for _ in ()).throw(billing.WebhookBusy()))
    assert webhook(app, sub_event()).status_code == 409
    monkeypatch.setattr(billing, "handle_webhook", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret text " + SECRET)))
    resp = webhook(app, sub_event())
    assert resp.status_code == 500 and SECRET not in resp.body.decode()


def test_webhook_route_unconfigured_grants_nothing(app, cfg):
    cfg.LEMONSQUEEZY_WEBHOOK_SECRET = ""
    resp = webhook(app, sub_event(), signature="anything")
    assert resp.status_code == 503 and snapshot(app.db) == ({}, {}, [])


def pricing_html(app, billing_enabled=True):
    resp = app.rt.billing_upgrade(make_request(app.idx, "/billing/upgrade", "GET"))
    return resp.body.decode()


def test_pricing_page_shows_real_prices_credits_and_checkout_buttons(app):
    html = pricing_html(app)
    assert "$19" in html and "$59" in html and "400 search credits" in html and "1,500 search credits" in html
    assert html.count('data-plan="standard"') == 1 and html.count('data-plan="business"') == 1
    assert "Coming soon" not in html and "Waitlist" not in html and "Pricing TBD" not in html
    assert "/api/billing/checkout" in html and "checkout" in html


def test_pricing_page_signed_out_asks_to_sign_in_and_has_no_checkout_buttons(app):
    app.user["value"] = None
    html = pricing_html(app)
    assert "Sign in to subscribe" in html and "data-plan=" not in html


def test_pricing_page_does_not_claim_an_active_plan_before_the_webhook(app):
    html = pricing_html(app)                                  # signed in, paid, but no verified event yet
    assert html.count("Your current plan") == 1               # only the Free card
    deliver(app.db, sub_event())
    html = pricing_html(app)
    assert html.count("Your current plan") == 1 and "data-plan=" not in html   # now the Standard card, no buy buttons


def test_pricing_page_with_billing_unconfigured_disables_the_buttons(app, cfg):
    cfg.LEMONSQUEEZY_API_KEY = ""
    html = pricing_html(app)
    assert html.count(" disabled>") == 2 and "isn't available right now" in html
