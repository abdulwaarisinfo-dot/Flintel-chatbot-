"""
billing.py — Lemon Squeezy subscriptions -> FLINTEL plan entitlements.

Separate from the search/credit hot path: nothing here runs during a search. It does
three things and reuses the existing credit machinery for the rest:

  1. create_checkout()   server-side hosted-checkout creation for ONE allow-listed plan.
                         The plan -> Lemon Squeezy variant mapping lives only in config
                         (environment); the browser sends a plan NAME and nothing else.
  2. handle_webhook()    verifies the HMAC-SHA256 signature over the RAW body, de-duplicates
                         the delivery, validates it (store, variant, status, owner) and
                         records the subscription in `flintel_subscriptions`.
  3. recompute()         derives the owner's entitlement from ALL of that owner's stored
                         subscriptions and writes it into `flintel_plans` through
                         search_credits.set_plan_entitlement() — the record the existing
                         limit_for()/active_plan() already read. Credits are therefore never
                         "granted" or "added": the monthly limit of an active plan is
                         plan_credits(plan) (400 / 1500) and usage counters are never touched,
                         so a retried or duplicated event cannot create credits twice.

CREDIT PERIOD: a paid plan's allowance (400 / 1500) is counted in the BILLING period it was paid for,
not in the calendar month. Each subscription document carries `period_key` (the UTC date the paid period
ends) and `period_ends`; recompute() copies the best subscription's pair into the plan record
(`credit_period_key` / `credit_period_ends`) and CreditStore.allowance() counts usage under that key. A
new period starts ONLY when the provider's paid-through date jumps forward by at least
MIN_PERIOD_JUMP_DAYS (a renewal), so a replayed, duplicated or re-ordered event, an upgrade/downgrade
inside the period, or a late `subscription_updated` can neither open a second counter nor reset usage.
Usage made on the free plan (calendar-month counter) never carries into the paid period.

FIRST-PAID CARRYOVER (only when the webhook is given a CreditStore; routes.py passes it): the unused free credits of
the owner's first genuine free -> paid activation become a bonus of that FIRST paid period only. See _first_activation:
claim (plan record) -> freeze + snapshot (usage document) -> activate with the decision in one conditional write ->
verify. Pre-feature subscribers (a plan record without `carryover`), a resubscription and a later plan change never
receive it; a renewal opens a new period key and is exactly the plan grant. Those checks span three documents and are a
protocol of fencing and eventual correction, not a cross-document transaction.

PAYMENT IDENTITY: a paid invoice (`subscription-invoices` id) is claimed once in flintel_billing_events
as `payment:<subscription>:<invoice>`; `subscription_payment_success` and `subscription_payment_recovered`
for the same invoice share that claim, so one renewal is applied exactly once whatever the event names,
bodies, order or concurrency. Only invoices whose status is "paid" count.

REFUNDS (policy R2): a FULL refund of the invoice that funded the CURRENT period (period_invoice_id)
revokes access for that period only (`revoked_period_key`); the next separately paid period is not
affected. A partial refund is recorded (`refunded_invoices`) and revokes nothing. A refund of an older
invoice, or one that cannot be matched to the funding invoice, never revokes (it is recorded and logged
at ERROR for review).

STORAGE (database.py)
  flintel_subscriptions   _id = Lemon Squeezy subscription id. owner_key, plan, status, variant,
                          paid_through, entitled_until, ls_updated_at, rev, period_key/period_ends, period_invoice_id,
                          refunded_invoices, revoked_period_key ... (no card data, no signed URLs)
  flintel_billing_events  _id = SHA-256 of the raw body (Lemon Squeezy sends no event id; a retry
                          re-sends the identical body) or `payment:<sub>:<invoice>`. state
                          processing|done|failed. No payload.

ENTITLEMENT RULES (see _until_for)
  active / on_trial  -> paid through `renews_at` (clamped to now + MAX_PERIOD_DAYS)
  cancelled          -> until `ends_at`
  expired            -> ends now
  past_due / unpaid / paused -> NO extension: only what was already paid for (paid_through)
  A failed payment never extends anything; refunds: see above.

Live Lemon Squeezy behaviour (payload shapes, checkout API) is implemented from its documented
API and is UNVERIFIED against a real account until a test-mode run.
"""
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import search_credits as sc

log = logging.getLogger("flintel.billing")

API_BASE = "https://api.lemonsqueezy.com"
JSONAPI = "application/vnd.api+json"
HTTP_TIMEOUT_SECONDS = 15.0

PAID_PLANS = sc.PAID_PLANS


def free_monthly_credits() -> int:
    """The free plan's monthly credits (for the pricing page)."""
    return sc.configured_limit()


PRICE_USD = {sc.PLAN_STANDARD: 19, sc.PLAN_BUSINESS: 59}     # display only; Lemon Squeezy charges the variant's price
PLAN_LABEL = {sc.PLAN_STANDARD: "Standard", sc.PLAN_BUSINESS: "Business"}
PLAN_RANK = {sc.PLAN_STANDARD: 1, sc.PLAN_BUSINESS: 2}
_VARIANT_SETTING = {sc.PLAN_STANDARD: "LEMONSQUEEZY_VARIANT_ID_STANDARD", sc.PLAN_BUSINESS: "LEMONSQUEEZY_VARIANT_ID_BUSINESS"}

MAX_PERIOD_DAYS = 35               # a monthly plan is never paid further ahead than this
MIN_PERIOD_JUMP_DAYS = 20          # a paid-through date this far beyond the current period's end is a renewal
FUNDING_REASONS = frozenset({"initial", "renewal"})   # invoices that start a billing period ("updated" = proration)
STATUS_RANK = {"on_trial": 0, "active": 0, "paused": 1, "past_due": 1, "unpaid": 1, "cancelled": 2, "expired": 3}
STALE_PROCESSING_SECONDS = 300     # an event stuck in "processing" this long may be taken over
MAX_BODY_BYTES = 1_000_000

SUBSCRIPTION_EVENTS = frozenset({"subscription_created", "subscription_updated", "subscription_cancelled",
                                 "subscription_resumed", "subscription_expired", "subscription_paused",
                                 "subscription_unpaused"})
PAYMENT_EVENTS = frozenset({"subscription_payment_success", "subscription_payment_recovered",
                            "subscription_payment_failed", "subscription_payment_refunded"})
KNOWN_STATUSES = frozenset({"on_trial", "active", "paused", "past_due", "unpaid", "cancelled", "expired"})
PAYING_STATUSES = frozenset({"active", "on_trial"})
_ID_RE = re.compile(r"^[0-9]{1,20}$")


# ── errors ───────────────────────────────────────────────────────────────────
class BillingError(Exception):
    """Base class. `public` is safe to show to a user; the exception text is never shown."""
    public = "Billing is unavailable right now."


class BillingNotConfigured(BillingError):
    public = "Checkout isn't available right now."


class CheckoutError(BillingError):
    public = "We couldn't start checkout. Please try again in a moment."


class AlreadySubscribed(BillingError):
    public = "You already have an active subscription. Manage it from your billing portal."


class WebhookRejected(Exception):
    """The delivery must be answered with `http_status` and must change no state."""

    def __init__(self, http_status, reason):
        super().__init__(reason)
        self.http_status, self.reason = http_status, reason


class WebhookBusy(Exception):
    """Another worker is processing the same delivery right now: answer 409 so it is retried."""


# ── configuration (read at call time, like search_credits) ────────────────────
def _cfg():
    import config
    return config


def _setting(name) -> str:
    return str(getattr(_cfg(), name, "") or "").strip()


def test_mode() -> bool:
    return bool(getattr(_cfg(), "LEMONSQUEEZY_TEST_MODE", False))


def variant_id_for(plan):
    name = _VARIANT_SETTING.get(plan)
    return _setting(name) if name else ""


def plan_for_variant(variant_id):
    """The FLINTEL plan configured for a Lemon Squeezy variant id, or None."""
    text = str(variant_id).strip() if variant_id is not None and not isinstance(variant_id, bool) else ""
    if not text:
        return None
    for plan in (sc.PLAN_STANDARD, sc.PLAN_BUSINESS):
        configured = variant_id_for(plan)
        if configured and configured == text:
            return plan
    return None


def configured() -> bool:
    """All five settings present and the two variant ids different (otherwise a plan could be mistaken for the other)."""
    names = ("LEMONSQUEEZY_API_KEY", "LEMONSQUEEZY_STORE_ID", "LEMONSQUEEZY_WEBHOOK_SECRET",
             "LEMONSQUEEZY_VARIANT_ID_STANDARD", "LEMONSQUEEZY_VARIANT_ID_BUSINESS")
    if not all(_setting(n) for n in names):
        return False
    return variant_id_for(sc.PLAN_STANDARD) != variant_id_for(sc.PLAN_BUSINESS)


def plan_catalog():
    """What the pricing page shows (server-side values only)."""
    return [{"plan": p, "label": PLAN_LABEL[p], "price_usd": PRICE_USD[p], "credits": sc.plan_credits(p)}
            for p in (sc.PLAN_STANDARD, sc.PLAN_BUSINESS)]


# ── time helpers ──────────────────────────────────────────────────────────────
def _utcnow():
    return datetime.now(timezone.utc)


def _aware(dt):
    return sc._aware(dt)


def _parse_time(value):
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return _aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def _clamp(dt, now):
    return min(dt, now + timedelta(days=MAX_PERIOD_DAYS)) if dt is not None else None


# ── signatures ────────────────────────────────────────────────────────────────
def verify_signature(raw: bytes, header) -> bool:
    """Lemon Squeezy signs the RAW request body with HMAC-SHA256 (hex) using the webhook secret and
    sends it in X-Signature. Constant-time comparison; False for a missing secret or header."""
    secret = _setting("LEMONSQUEEZY_WEBHOOK_SECRET")
    if not secret or not isinstance(header, str) or not header.strip() or not isinstance(raw, (bytes, bytearray)):
        return False
    expected = hmac.new(secret.encode("utf-8"), bytes(raw), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), header.strip().lower().encode("utf-8", "replace"))


def _owner_sig(owner, plan) -> str:
    key = hmac.new(_setting("LEMONSQUEEZY_WEBHOOK_SECRET").encode("utf-8"), b"flintel-checkout-owner-v1", hashlib.sha256).digest()
    return hmac.new(key, f"{owner}\n{plan}".encode("utf-8"), hashlib.sha256).hexdigest()[:40]


def _authentic_owner(custom):
    """The owner e-mail carried in the checkout's custom data, or None unless our own signature on it is valid."""
    if not isinstance(custom, dict):
        return None
    owner, plan, sig = custom.get("owner"), custom.get("plan"), custom.get("sig")
    if not (isinstance(owner, str) and isinstance(plan, str) and isinstance(sig, str)) or plan not in sc.PAID_PLANS:
        return None
    if not _setting("LEMONSQUEEZY_WEBHOOK_SECRET"):
        return None
    if not hmac.compare_digest(_owner_sig(owner, plan).encode("ascii"), sig.strip().lower().encode("utf-8", "replace")):
        return None
    return owner if sc.registered_owner(owner) else None


# ── checkout ──────────────────────────────────────────────────────────────────
def _http_post(url, payload, headers):
    import httpx
    return httpx.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)


def _http_get(url, headers):
    import httpx
    return httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)


def _api_headers():
    return {"Accept": JSONAPI, "Content-Type": JSONAPI, "Authorization": f"Bearer {_setting('LEMONSQUEEZY_API_KEY')}"}


def _checkout_url_ok(url) -> bool:
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    host = url[len("https://"):].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].lower()
    return host == "lemonsqueezy.com" or host.endswith(".lemonsqueezy.com")


def create_checkout(owner_email, plan, redirect_url, post=None) -> str:
    """Hosted-checkout URL for `plan`, bound to `owner_email`. Raises ValueError for a plan that is not
    allow-listed or an owner that cannot hold a plan, BillingNotConfigured, CheckoutError."""
    if plan not in sc.PAID_PLANS:
        raise ValueError("unknown plan")
    if not sc.registered_owner(owner_email):
        raise ValueError("a plan needs a registered e-mail account")
    if not configured():
        raise BillingNotConfigured("billing is not configured")
    variant = variant_id_for(plan)
    payload = {"data": {
        "type": "checkouts",
        "attributes": {
            "checkout_data": {"email": owner_email,
                              "custom": {"owner": owner_email, "plan": plan, "sig": _owner_sig(owner_email, plan)}},
            "product_options": {"redirect_url": redirect_url, "enabled_variants": [int(variant)]},
            "test_mode": test_mode(),
        },
        "relationships": {"store": {"data": {"type": "stores", "id": _setting("LEMONSQUEEZY_STORE_ID")}},
                          "variant": {"data": {"type": "variants", "id": variant}}},
    }}
    try:
        response = (post or _http_post)(f"{API_BASE}/v1/checkouts", payload, _api_headers())
    except Exception as exc:                       # timeout, DNS, TLS ... never echo the text
        log.warning(f"checkout request failed: {type(exc).__name__}")
        raise CheckoutError("provider unreachable") from None
    status = getattr(response, "status_code", None)
    if status not in (200, 201):
        log.warning(f"checkout request rejected by the provider: HTTP {status}")
        raise CheckoutError("provider rejected the request")
    try:
        url = response.json()["data"]["attributes"]["url"]
    except Exception:
        log.warning("checkout response could not be read")
        raise CheckoutError("unreadable provider response") from None
    if not _checkout_url_ok(url):
        log.warning("checkout response carried an unexpected URL")
        raise CheckoutError("unexpected checkout url")
    return url


# ── subscription state ────────────────────────────────────────────────────────
def _until_for(status, ends_at, paid_through, now):
    """The moment up to which the subscription entitles its owner (None = not at all)."""
    if status in PAYING_STATUSES:
        return paid_through
    if status == "cancelled":
        return _clamp(ends_at, now) or paid_through
    if status == "expired":
        return min(ends_at, now) if ends_at else now
    return paid_through                              # paused / past_due / unpaid: no extension


def _date_key(dt) -> str:
    return _aware(dt).strftime("%Y-%m-%d")


def _subscription_fields(sub_id, attrs, plan, owner, current, now):
    status = attrs["status"]
    renews_at, ends_at = _parse_time(attrs.get("renews_at")), _parse_time(attrs.get("ends_at"))
    paid_through = _aware(current.get("paid_through")) if current and current.get("paid_through") else None
    if status in PAYING_STATUSES and renews_at:
        cand = _clamp(renews_at, now)
        paid_through = max(paid_through, cand) if paid_through else cand
    elif status == "cancelled" and ends_at and paid_through is None:
        paid_through = _clamp(ends_at, now)
    # billing period (the credit counter's identity): starts once, and again only on a renewal-sized jump
    cur_end = _aware(current.get("period_ends")) if current and current.get("period_ends") else None
    period_key = (current or {}).get("period_key")
    period_ends = cur_end
    if paid_through is not None:
        if cur_end is None or not period_key:
            period_ends, period_key = paid_through, _date_key(paid_through)
        elif status in PAYING_STATUSES and paid_through >= cur_end + timedelta(days=MIN_PERIOD_JUMP_DAYS):
            period_ends, period_key = paid_through, _date_key(paid_through)
        elif paid_through > cur_end:
            period_ends = paid_through
    entitled_until = _until_for(status, ends_at, paid_through, now)
    revoked_at = (current or {}).get("revoked_at")
    if revoked_at and (current or {}).get("revoked_period_key") == period_key and entitled_until is not None:
        entitled_until = min(entitled_until, _aware(revoked_at))       # a fully refunded period stays closed
    fields = {
        "owner_key": owner, "plan": plan, "status": status, "cancelled": bool(attrs.get("cancelled")),
        "variant_id": str(attrs.get("variant_id")), "store_id": str(attrs.get("store_id")),
        "customer_id": str(attrs.get("customer_id")) if attrs.get("customer_id") is not None else None,
        "order_id": str(attrs.get("order_id")) if attrs.get("order_id") is not None else None,
        "renews_at": renews_at, "ends_at": ends_at, "paid_through": paid_through,
        "entitled_until": entitled_until, "period_key": period_key, "period_ends": period_ends,
        "started_at": (current or {}).get("started_at") or _parse_time(attrs.get("created_at")) or now,
        "test_mode": bool(attrs.get("test_mode")), "ls_updated_at": _parse_time(attrs.get("updated_at")),
        "updated_at": now,
    }
    return fields


def _apply_subscription(subs, sub_id, attrs, plan, owner, event_name, now):
    """Compare-and-set of one subscription document, ordered by Lemon Squeezy's own `updated_at`.
    Returns "applied" or "stale"."""
    incoming = _parse_time(attrs.get("updated_at"))
    if incoming is None:
        raise WebhookRejected(400, "missing updated_at")
    for _attempt in range(5):
        current = subs.find_one({"_id": sub_id})
        if current is None:
            fields = _subscription_fields(sub_id, attrs, plan, owner, None, now)
            try:
                subs.insert_one({"_id": sub_id, **fields, "rev": 1, "last_event": event_name})
            except Exception as exc:
                if sc._is_duplicate_key(exc):
                    continue
                raise
            return "applied"
        stored_at = _aware(current.get("ls_updated_at")) if current.get("ls_updated_at") else None
        if stored_at is not None and incoming < stored_at:
            return "stale"
        if stored_at is not None and incoming == stored_at and \
                STATUS_RANK.get(attrs["status"], 0) < STATUS_RANK.get(current.get("status"), 0):
            return "stale"        # same-second timestamps cannot order two states: never move backwards
        if current.get("owner_key") and current["owner_key"] != owner:
            raise WebhookRejected(200, "owner conflict")
        fields = _subscription_fields(sub_id, attrs, plan, owner, current, now)
        result = subs.update_one({"_id": sub_id, "rev": current.get("rev")},
                                 {"$set": {**fields, "last_event": event_name}, "$inc": {"rev": 1}})
        if getattr(result, "matched_count", 1) == 1:
            return "applied"
    raise sc.CreditStoreError("subscription write contention")


def default_credit_store():
    """The production credit store, or None while credit enforcement is switched off (then no carryover is computed).
    The webhook route calls this so that routes.py itself never reaches the credit store outside search()/usage."""
    return sc.default_store()


def _plan_inputs(subs, owner, now):
    """What the owner's plan record should say, derived from ALL of their stored subscriptions. -> ("no_subscription" |
    "nothing_to_write", None) or ("ready", {docs, rev, best, start, end, period, entitled})."""
    try:
        docs = list(subs.find({"owner_key": owner}))
    except Exception as exc:
        raise sc.CreditStoreError(f"subscription read failed: {type(exc).__name__}") from exc
    if not docs:
        return "no_subscription", None
    rev = sum(int(d.get("rev") or 0) for d in docs)

    def until(d):
        value = d.get("entitled_until")
        return _aware(value) if value else None
    entitled = [d for d in docs if d.get("plan") in sc.PAID_PLANS and until(d) and until(d) > now]
    if entitled:
        best = max(entitled, key=lambda d: (PLAN_RANK[d["plan"]], until(d)))
        end = until(best)
    else:                                            # nothing entitles right now: close the window
        past = [d for d in docs if d.get("plan") in sc.PAID_PLANS and until(d)]
        if not past:
            return "nothing_to_write", None
        best = max(past, key=lambda d: until(d))
        end = min(until(best), now)
    started = _aware(best.get("started_at")) if best.get("started_at") else now
    start = min(started, end - timedelta(seconds=1))
    period = {}
    if entitled and best.get("period_key") and best.get("period_ends"):
        period = {"credit_period_key": best["period_key"], "credit_period_ends": _aware(best["period_ends"])}
    return "ready", {"docs": docs, "rev": rev, "best": best, "start": start, "end": end, "period": period,
                     "entitled": bool(entitled)}


def _source_ref(d) -> str:
    return f"ls-sub:{d['best']['_id']}:rev{d['rev']}"


def recompute(subs, plans, owner, now=None, credits=None) -> str:
    """Write the owner's plan record from ALL of their stored subscriptions. Deterministic and idempotent
    (the same documents give the same record); ordered by billing_rev (see set_plan_entitlement).
    `credits` (a CreditStore) switches on the first free -> paid CARRYOVER (see _first_activation); without it this is
    exactly the original behaviour."""
    now = _aware(now or _utcnow())
    code, d = _plan_inputs(subs, owner, now)
    if code != "ready":
        return code
    if credits is None:
        return sc.set_plan_entitlement(plans, owner, d["best"]["plan"], d["start"], d["end"], _source_ref(d), d["rev"], now,
                                       **d["period"])
    return _recompute_with_carryover(subs, plans, owner, d, now, credits)


def _recompute_with_carryover(subs, plans, owner, d, now, credits) -> str:
    best = d["best"]
    try:
        row = plans.find_one({"owner_key": owner})
    except Exception as exc:
        raise sc.CreditStoreError(f"plan read failed: {type(exc).__name__}") from exc
    carry = (row or {}).get("carryover") if isinstance((row or {}).get("carryover"), dict) else None
    prior_history = any(x["_id"] != best["_id"] and x.get("plan") in sc.PAID_PLANS for x in d["docs"])

    def plain(mark_undecided=False):
        return sc.set_plan_entitlement(plans, owner, best["plan"], d["start"], d["end"], _source_ref(d), d["rev"], now,
                                       mark_undecided=mark_undecided, **d["period"])
    if not d["entitled"]:                                      # a closing write needs no claim
        return plain(mark_undecided=(row is None and not prior_history))
    eligible_row = row is None or (carry is not None and carry.get("state") in ("undecided", "claiming"))
    if not eligible_row or (row is None and prior_history):    # pre-feature subscriber, decided, or a resubscription
        return plain()
    if not d["period"]:                                        # no billing period key: no carryover; a live claim still defers
        if carry and carry.get("state") == "claiming":
            at = _aware(carry.get("claim_at"))
            if at is None or (now - at).total_seconds() < sc._claim_stale_seconds():
                raise WebhookBusy()
        return plain()
    return _first_activation(subs, plans, owner, d, now, credits)


def _guest_handover_ready(owner, credits, now) -> bool:
    """(GUEST HANDOVER) Before the FIRST paid activation: apply any guest handover claimed for this owner and refuse (False ->
    WebhookBusy, a retryable 409) while one is still unresolved - its target month is over and nobody has explicitly applied
    or waived it - so the carryover can never silently omit credits the guest already used. Any error is 'not ready':
    nothing is guessed. An operator resolves it with guest_gate.resolve_handover() and the provider's retry (or a replay of
    the event) then proceeds."""
    try:
        import guest_gate
        ready = guest_gate.handover_ready_for_activation(owner, credits, now)
    except Exception as exc:
        log.warning(f"billing: the guest handover check failed ({type(exc).__name__}); the activation is retried later")
        return False
    if not ready:
        log.warning(f"billing: first activation of {owner} deferred - a guest handover is pending an explicit resolution")
    return ready


def _first_activation(subs, plans, owner, d, now, credits) -> str:
    """The first free -> paid activation, fenced end to end:
      0 guest check             any guest handover claimed for this owner is applied; WebhookBusy (409) while one is unresolved
      1 claim   (plan record)   one attempt at a time; another live claim -> WebhookBusy (409, retried by the provider)
      2 freeze  (usage doc)     the free month's counter is frozen and the unused free credits snapshotted in ONE
                                conditional update; deferred (WebhookBusy) while a free reservation is still refundable
      3 re-read                 the subscription documents again, so the write below uses current data
      4 activate (plan record)  ONE conditional update: plan fields + the carryover decision, matching only while the claim is
                                still ours and the record is exactly the version read
      5 verify                  if the subscription advanced meanwhile, write the newer entitlement (ordinary ordered write)
    The freeze STAYS after step 4 (it is what refuses a stale free reservation); it is cleared lazily once the plan no longer
    entitles. These checks across the plan, usage and subscription documents are a protocol (fenced, then eventually
    corrected), NOT a cross-document transaction."""
    import uuid
    claim_id = uuid.uuid4().hex
    best, period = d["best"], d["period"]["credit_period_key"]
    if not _guest_handover_ready(owner, credits, now):
        raise WebhookBusy()      # a guest handover is unresolved: nothing is claimed, frozen or written; retried later
    status = sc.claim_activation(plans, owner, claim_id, now)
    if status == "busy":
        raise WebhookBusy()
    if status == "ineligible":
        return sc.set_plan_entitlement(plans, owner, best["plan"], d["start"], d["end"], _source_ref(d), d["rev"], now, **d["period"])
    free_period = None
    try:
        free_period = credits.carryover_free_period(owner, now)
        credits.freeze_free_period(owner, free_period, claim_id, best["_id"], period, now)
        code, d2 = _plan_inputs(subs, owner, now)
        if code != "ready" or not d2["entitled"] or d2["best"]["_id"] != best["_id"] \
                or d2["period"].get("credit_period_key") != period:
            credits.release_activation(owner, claim_id, free_period)          # the inputs moved: start over on the retry
            raise WebhookBusy()
        d = d2
        outcome = sc.activate_with_carryover(
            plans, owner, d["best"]["plan"], d["start"], d["end"], _source_ref(d), d["rev"], now, period,
            d["period"]["credit_period_ends"],
            {"claim_id": claim_id, "sub_id": d["best"]["_id"], "free_period": free_period, "free_limit": credits.limit()})
    except sc.ActivationDeferred:
        credits.release_activation(owner, claim_id, free_period)
        raise WebhookBusy() from None
    if outcome == "applied":
        written = d["rev"]
        for _attempt in range(3):                                              # eventual correction, not atomicity
            code3, d3 = _plan_inputs(subs, owner, now)
            if code3 != "ready" or d3["rev"] <= written:
                break
            sc.set_plan_entitlement(plans, owner, d3["best"]["plan"], d3["start"], d3["end"], _source_ref(d3), d3["rev"], now,
                                    **d3["period"])
            written = d3["rev"]
        return "applied"
    if outcome == "foreign_entitled":
        sc.decide_zero(plans, owner, claim_id, now)                            # never a bonus for an entitlement we did not write
        return "superseded"
    if outcome == "superseded":
        credits.release_activation(owner, claim_id, free_period)
        return "superseded"
    raise WebhookBusy()                                                        # "lost": another attempt owns the claim now


# ── invoice and refund bookkeeping (compare-and-set on the subscription document) ──
def _update_subscription(subs, sub_id, build):
    """Read-modify-write one subscription document, compare-and-set on `rev`. `build(current)` returns
    (changes, note); empty changes write nothing. Returns the note, or None if the document is missing."""
    for _attempt in range(5):
        current = subs.find_one({"_id": sub_id})
        if current is None:
            return None
        changes, note = build(current)
        if not changes:
            return note or "unchanged"
        result = subs.update_one({"_id": sub_id, "rev": current.get("rev")}, {"$set": changes, "$inc": {"rev": 1}})
        if getattr(result, "matched_count", 1) == 1:
            return note or "recorded"
    raise sc.CreditStoreError("subscription write contention")


def _revoke_current_period(cur, now) -> dict:
    """Changes that close the CURRENT billing period of `cur` (idempotent)."""
    key = cur.get("period_key")
    if not key or cur.get("revoked_period_key") == key:
        return {}
    until = _aware(cur["entitled_until"]) if cur.get("entitled_until") else None
    return {"revoked_period_key": key, "revoked_at": now, "entitled_until": min(until, now) if until else None}


def _record_paid_invoice(subs, sub_id, inv_id, reason, created, now):
    """Remember which invoice funds the current period (newest initial/renewal invoice wins, by the
    invoice's own created_at, so a late replay of an older invoice cannot take its place). If that invoice was
    already fully refunded (the refund event came first) the period is closed at once."""
    def build(cur):
        changes, note = {}, None
        funding, funding_at = cur.get("period_invoice_id"), cur.get("period_invoice_at")
        if reason in FUNDING_REASONS:
            if funding == inv_id:
                pass
            elif funding is None or funding_at is None or created >= _aware(funding_at):
                changes.update(period_invoice_id=inv_id, period_invoice_at=created)
                funding = inv_id
            else:
                note = "older_invoice"
        if funding == inv_id and (cur.get("refunded_invoices") or {}).get(inv_id) == "full":
            changes.update(_revoke_current_period(cur, now))
        return changes, note
    return _update_subscription(subs, sub_id, build)


def _refund_kind(attrs):
    """"full", "partial" or None (cannot be classified from the invoice data -> never acted on)."""
    total, amount = attrs.get("total"), attrs.get("refunded_amount")
    is_int = lambda v: isinstance(v, int) and not isinstance(v, bool)    # noqa: E731
    if attrs.get("status") == "refunded":
        return "full" if (not is_int(total) or not is_int(amount) or amount >= total) else None
    if is_int(total) and is_int(amount) and total > 0 and amount >= total:
        return "full"
    if is_int(total) and is_int(amount) and 0 < amount < total:
        return "partial"
    return None


def _apply_refund(subs, sub_id, inv_id, kind, reason, created, now):
    """Record a refund and, for a FULL refund of the invoice that funds the current period, close that period."""
    def build(cur):
        changes, note = {}, kind or "unclassified"
        known = (cur.get("refunded_invoices") or {}).get(inv_id)
        if kind and known != "full":
            changes[f"refunded_invoices.{inv_id}"] = kind
        if kind is None:
            changes["refund_review_at"] = now
            log.error(f"billing refund sub={sub_id} invoice={inv_id}: refund data cannot be classified; access unchanged, review needed")
        elif kind == "full" and reason in FUNDING_REASONS:
            funding, funding_at = cur.get("period_invoice_id"), cur.get("period_invoice_at")
            if funding == inv_id:
                changes.update(_revoke_current_period(cur, now))
                note = "revoked"
            elif funding is not None and funding_at is not None and created is not None and created < _aware(funding_at):
                note = "stale_refund"                     # an older period's invoice: the current period was paid separately
            else:
                note = "unmatched_refund"                 # the funding invoice is unknown or older than this one: never guess
                changes["refund_review_at"] = now
                log.error(f"billing refund sub={sub_id} invoice={inv_id}: cannot be matched to the invoice funding the current "
                          "period; access unchanged, review needed")
        return changes, note
    return _update_subscription(subs, sub_id, build)


# ── webhook ───────────────────────────────────────────────────────────────────
def _claim(events, key, name, now):
    try:
        events.insert_one({"_id": key, "event": name, "state": "processing", "claimed_at": now, "attempts": 1})
        return "claimed"
    except Exception as exc:
        if not sc._is_duplicate_key(exc):
            raise
    doc = events.find_one({"_id": key}) or {}
    if doc.get("state") == "done":
        return "duplicate"
    claimed = _aware(doc["claimed_at"]) if doc.get("claimed_at") else None
    if doc.get("state") == "failed" or claimed is None or (now - claimed).total_seconds() > STALE_PROCESSING_SECONDS:
        taken = events.update_one({"_id": key, "state": doc.get("state"), "claimed_at": doc.get("claimed_at")},
                                  {"$set": {"state": "processing", "claimed_at": now}, "$inc": {"attempts": 1}})
        return "claimed" if getattr(taken, "matched_count", 0) == 1 else "busy"
    return "busy"


def _finish(events, key, state, result):
    try:
        events.update_one({"_id": key}, {"$set": {"state": state, "result": result, "finished_at": _utcnow()}})
    except Exception as exc:                          # the work is done; a lost marker only costs a safe re-run
        log.warning(f"billing event marker not updated: {type(exc).__name__}")


def fetch_subscription(sub_id, get=None):
    """The current subscription object from the API (used after a successful payment, so a renewal is
    never missed if the matching subscription_updated is late or lost)."""
    if not _ID_RE.match(str(sub_id)):
        raise WebhookRejected(200, "bad subscription id")
    try:
        response = (get or _http_get)(f"{API_BASE}/v1/subscriptions/{sub_id}", _api_headers())
        if getattr(response, "status_code", None) != 200:
            raise RuntimeError(f"HTTP {getattr(response, 'status_code', None)}")
        data = response.json()["data"]
    except Exception as exc:
        log.warning(f"subscription refresh failed: {type(exc).__name__}")
        raise sc.CreditStoreError("subscription refresh failed") from None
    return data


def _validated_subscription(data, store_id):
    """(sub_id, attrs, plan) from a subscription object, or raises WebhookRejected(200, reason) for
    a payload that is valid JSON but not ours / not usable (acknowledged, nothing changed)."""
    if not isinstance(data, dict) or data.get("type") != "subscriptions":
        raise WebhookRejected(200, "not a subscription object")
    sub_id, attrs = data.get("id"), data.get("attributes")
    if not isinstance(sub_id, str) or not _ID_RE.match(sub_id) or not isinstance(attrs, dict):
        raise WebhookRejected(400, "malformed subscription")
    if str(attrs.get("store_id")) != store_id:
        raise WebhookRejected(200, "other store")
    plan = plan_for_variant(attrs.get("variant_id"))
    if plan is None:
        raise WebhookRejected(200, "unknown variant")
    if attrs.get("status") not in KNOWN_STATUSES:
        raise WebhookRejected(200, "unknown status")
    return sub_id, attrs, plan


def handle_webhook(raw, signature, event_header, subs, events, plans, now=None, get=None, credits=None) -> dict:
    """Process one delivery. Returns {"status": applied|duplicate|stale|ignored, ...}. Raises WebhookRejected
    (answer with .http_status, nothing changed), WebhookBusy (409, retried later) or lets a store/provider
    error propagate (500: Lemon Squeezy retries; every step is safe to repeat)."""
    now = _aware(now or _utcnow())
    if not configured():
        raise WebhookRejected(503, "billing not configured")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_BODY_BYTES:
        raise WebhookRejected(413, "body too large")
    if not verify_signature(raw, signature):
        raise WebhookRejected(401, "invalid signature")
    try:
        body = json.loads(bytes(raw).decode("utf-8"))
        meta, data = body["meta"], body["data"]
        name = meta["event_name"]
        if not (isinstance(body, dict) and isinstance(meta, dict) and isinstance(data, dict) and isinstance(name, str)):
            raise ValueError
    except Exception:
        raise WebhookRejected(400, "malformed payload") from None
    if event_header is not None and event_header != name:
        raise WebhookRejected(400, "event name mismatch")
    if name not in SUBSCRIPTION_EVENTS and name not in PAYMENT_EVENTS:
        return {"status": "ignored", "reason": "unhandled event"}
    if bool(meta.get("test_mode")) != test_mode():
        return {"status": "ignored", "reason": "test/live mode mismatch"}

    key = hashlib.sha256(bytes(raw)).hexdigest()
    claim = _claim(events, key, name, now)
    if claim == "duplicate":
        return {"status": "duplicate"}
    if claim == "busy":
        raise WebhookBusy()
    try:
        result = _process(name, meta, data, subs, events, plans, now, get, credits)
    except WebhookRejected as rejected:
        _finish(events, key, "done", f"rejected:{rejected.reason}")
        if rejected.http_status == 200:
            return {"status": "ignored", "reason": rejected.reason}
        raise
    except Exception:
        _finish(events, key, "failed", "error")
        raise
    _finish(events, key, "done", result["status"])
    return result


def _process(name, meta, data, subs, events, plans, now, get, credits=None):
    store_id = _setting("LEMONSQUEEZY_STORE_ID")
    if name in SUBSCRIPTION_EVENTS:
        sub_id, attrs, plan = _validated_subscription(data, store_id)
        stored = subs.find_one({"_id": sub_id}) or {}
        owner = _authentic_owner(meta.get("custom_data"))
        if owner is None:
            owner = stored.get("owner_key")
        if not owner:
            raise WebhookRejected(200, "no verified owner")
        outcome = _apply_subscription(subs, sub_id, attrs, plan, owner, name, now)
        written = recompute(subs, plans, owner, now, credits)
        log.info(f"billing event {name} sub={sub_id} -> {outcome}/{written}")
        return {"status": outcome, "plan_record": written}

    # payment events: data is a subscription invoice
    attrs = data.get("attributes") if data.get("type") == "subscription-invoices" else None
    inv_id = data.get("id")
    if not isinstance(attrs, dict) or not isinstance(attrs.get("subscription_id"), (int, str)):
        raise WebhookRejected(400, "malformed invoice")
    if not isinstance(inv_id, (str, int)) or isinstance(inv_id, bool) or not _ID_RE.match(str(inv_id)):
        raise WebhookRejected(400, "malformed invoice")
    inv_id = str(inv_id)
    if str(attrs.get("store_id")) != store_id:
        raise WebhookRejected(200, "other store")
    sub_id = str(attrs["subscription_id"])
    if not _ID_RE.match(sub_id):
        raise WebhookRejected(400, "malformed invoice")
    stored = subs.find_one({"_id": sub_id})
    if not stored or not stored.get("owner_key"):
        if name in ("subscription_payment_success", "subscription_payment_recovered"):
            raise WebhookBusy()           # the subscription is not recorded yet: be retried, never lost
        raise WebhookRejected(200, "unknown subscription")
    owner = stored["owner_key"]
    when = _parse_time(attrs.get("updated_at")) or now
    created = _parse_time(attrs.get("created_at"))
    reason = attrs.get("billing_reason")
    if name == "subscription_payment_failed":
        subs.update_one({"_id": sub_id}, {"$set": {"last_payment_failed_at": when}})
        log.info(f"billing event {name} sub={sub_id} -> recorded (no entitlement change)")
        return {"status": "applied", "plan_record": "unchanged"}
    if name == "subscription_payment_refunded":
        kind = _refund_kind(attrs)
        if reason in FUNDING_REASONS and created is None and kind == "full":
            raise WebhookRejected(200, "refund without invoice created_at")
        note = _apply_refund(subs, sub_id, inv_id, kind, reason, created, now)
        written = recompute(subs, plans, owner, now, credits)
        log.info(f"billing event {name} sub={sub_id} -> {note}/{written}")
        return {"status": "applied", "plan_record": written, "refund": note}

    # success / recovered: one claim per INVOICE, whatever the event name
    if attrs.get("status") != "paid":
        raise WebhookRejected(200, "invoice not paid")
    if reason in FUNDING_REASONS and created is None:
        raise WebhookRejected(200, "invoice without created_at")
    key = f"payment:{sub_id}:{inv_id}"
    claim = _claim(events, key, name, now)
    if claim == "duplicate":
        return {"status": "duplicate_payment"}
    if claim == "busy":
        raise WebhookBusy()
    try:
        subs.update_one({"_id": sub_id}, {"$set": {"last_payment_ok_at": when}})
        fresh = fetch_subscription(sub_id, get)       # the provider's current view (renews_at moved)
        _id, attrs2, plan = _validated_subscription(fresh, store_id)
        if _id != sub_id:
            raise WebhookRejected(200, "refresh mismatch")
        outcome = _apply_subscription(subs, sub_id, attrs2, plan, owner, name, now)
        invoice_note = _record_paid_invoice(subs, sub_id, inv_id, reason, created, now)
        written = recompute(subs, plans, owner, now, credits)
    except Exception:
        _finish(events, key, "failed", "error")
        raise
    _finish(events, key, "done", outcome)
    log.info(f"billing event {name} sub={sub_id} -> {outcome}/{invoice_note}/{written}")
    return {"status": outcome, "plan_record": written, "invoice": invoice_note}


# ── status for the pricing page / API ─────────────────────────────────────────
def status_for_owner(subs, plans, owner, now=None) -> dict:
    """What this owner's account looks like, for display. Free unless the stored subscriptions entitle."""
    now = _aware(now or _utcnow())
    base = {"billing_enabled": configured(), "plan": sc.PLAN_FREE, "status": None, "cancelled": False,
            "renews_at": None, "ends_at": None, "can_manage": False}
    if not sc.registered_owner(owner):
        return base
    try:
        docs = list(subs.find({"owner_key": owner}))
    except Exception as exc:
        raise sc.CreditStoreError(f"subscription read failed: {type(exc).__name__}") from exc
    docs = [d for d in docs if d.get("plan") in sc.PAID_PLANS]
    if not docs:
        return base

    def until(d):
        return _aware(d["entitled_until"]) if d.get("entitled_until") else None
    live = [d for d in docs if until(d) and until(d) > now]
    pick = max(live, key=lambda d: (PLAN_RANK[d["plan"]], until(d))) if live else \
        max(docs, key=lambda d: _aware(d["ls_updated_at"]) if d.get("ls_updated_at") else now)
    iso = lambda v: _aware(v).isoformat() if v else None   # noqa: E731
    out = {**base, "plan": pick["plan"] if live else sc.PLAN_FREE, "status": pick.get("status"),
           "cancelled": bool(pick.get("cancelled")), "renews_at": iso(pick.get("renews_at")),
           "ends_at": iso(pick.get("ends_at")), "entitled_until": iso(pick.get("entitled_until")),
           "can_manage": True, "revoked": bool(pick.get("revoked_period_key")) and not live, "entitled": bool(live),
           "subscription_plan": pick["plan"], "activation": "active" if live else "none"}
    if plans is not None:
        pend = pending_activation(subs, plans, owner, now)
        if pend is not None:      # a paid subscription exists but the account still has FREE limits: never show it as active
            out["activation"] = pend["state"]
            out["subscription_plan"] = pend["plan"]
            if pend["state"] == "pending":      # report the plan enforcement applies now (an earlier, lower plan), else free
                out.update(plan=pend.get("enforced_plan") or sc.PLAN_FREE, entitled=bool(pend.get("enforced_plan")))
    return out


def pending_activation(subs, plans, owner, now=None):
    """DERIVED (no stored state): is a paid subscription not (yet) backed by an activated entitlement?
    -> None | {"state": "pending" | "expired_unactivated", "plan", "sub_id", "entitled_until"}.
    pending: the subscription entitles right now but the owner's plan record does not carry that subscription's billing
    period. expired_unactivated: that paid period has already ended and was never activated - NOT extended, NOT refunded
    here: surfaced for an operator decision. Raises sc.CreditStoreError on a read failure."""
    now = _aware(now or _utcnow())
    if not sc.registered_owner(owner):
        return None
    try:
        docs = [d for d in subs.find({"owner_key": owner}) if d.get("plan") in sc.PAID_PLANS and d.get("entitled_until")]
        row = plans.find_one({"owner_key": owner}) if plans is not None else None
    except Exception as exc:
        raise sc.CreditStoreError(f"pending-activation read failed: {type(exc).__name__}") from exc
    if not docs:
        return None
    activated = {row.get("credit_period_key")} if isinstance(row, dict) and row.get("plan") in sc.PAID_PLANS else set()
    live = [d for d in docs if _aware(d["entitled_until"]) > now]
    if live:
        best = max(live, key=lambda d: (PLAN_RANK[d["plan"]], _aware(d["entitled_until"])))
        rank_ok = sc.active_plan(row, now) is not None and PLAN_RANK.get(sc.active_plan(row, now), 0) >= PLAN_RANK[best["plan"]]
        if rank_ok:
            return None
        return {"state": "pending", "plan": best["plan"], "sub_id": best["_id"], "entitled_until": _aware(best["entitled_until"]),
                "enforced_plan": sc.active_plan(row, now)}     # the plan limits are applied under right now (None = free)
    never = [d for d in docs if d.get("period_key") and d.get("period_key") not in activated and not d.get("revoked_period_key")]
    if never:
        d = max(never, key=lambda d: _aware(d["entitled_until"]))
        return {"state": "expired_unactivated", "plan": d["plan"], "sub_id": d["_id"], "entitled_until": _aware(d["entitled_until"])}
    return None


_RESUME_LAST = {}                 # owner -> monotonic time of the last attempt (per process; a restart simply allows a retry)
_RESUME_LOCK = threading.Lock()
RESUME_MIN_INTERVAL_SECONDS = 30


def resume_activation(subs, plans, owner, credits, now=None, min_interval=None):
    """Re-enter the EXISTING first-activation protocol for an owner whose paid subscription is waiting for its entitlement.
    The only writer is recompute() (claim -> freeze -> checked activation write), so no check is skipped and a repeat,
    a concurrent call, a stale subscription revision or a replayed webhook cannot grant twice. Rate limited per owner.
    -> "not_pending" | "rate_limited" | "activated" | "deferred" | "expired_unactivated" | "error". Never raises."""
    try:
        state = pending_activation(subs, plans, owner, now)
        if state is None:
            return "not_pending"
        if state["state"] == "expired_unactivated":
            log.warning(f"billing: the paid period of subscription {state['sub_id']} ended before activation; left for an "
                        f"operator decision (no extension, no refund, no entitlement)")
            return "expired_unactivated"
        wait = RESUME_MIN_INTERVAL_SECONDS if min_interval is None else min_interval
        with _RESUME_LOCK:
            last = _RESUME_LAST.get(owner)
            if last is not None and time.monotonic() - last < wait:
                return "rate_limited"
            _RESUME_LAST[owner] = time.monotonic()
            if len(_RESUME_LAST) > 5000:
                _RESUME_LAST.clear()
        recompute(subs, plans, owner, now, credits)
        return "activated" if pending_activation(subs, plans, owner, now) is None else "deferred"
    except WebhookBusy:
        return "deferred"
    except Exception as exc:
        log.warning(f"billing: resuming the activation failed ({type(exc).__name__}); it stays pending")
        return "error"


def _default_collections():
    import database
    return database.subscriptions_collection, database.plans_collection


def resume_activation_default(owner, credits, now=None):
    """resume_activation() with the production collections. Never raises."""
    try:
        subs, plans = _default_collections()
        return resume_activation(subs, plans, owner, credits, now)
    except Exception as exc:
        log.warning(f"billing: resume unavailable ({type(exc).__name__})")
        return "error"


def has_live_subscription(subs, owner, now=None):
    """The owner's best currently-entitling subscription document (to refuse a second checkout), or None."""
    now = _aware(now or _utcnow())
    try:
        docs = list(subs.find({"owner_key": owner}))
    except Exception as exc:
        raise sc.CreditStoreError(f"subscription read failed: {type(exc).__name__}") from exc
    live = [d for d in docs if d.get("plan") in sc.PAID_PLANS and d.get("entitled_until") and _aware(d["entitled_until"]) > now]
    return max(live, key=lambda d: (PLAN_RANK[d["plan"]], _aware(d["entitled_until"]))) if live else None


def fresh_portal_url(subs, owner, get=None):
    """A freshly fetched customer-portal URL for the owner's own subscription, or None if they have none.
    The portal link is pre-signed and valid for 24 hours, so it is fetched when the owner asks for it and
    never stored. Raises sc.CreditStoreError (read/provider failure) or BillingError (unexpected answer)."""
    now = _utcnow()
    try:
        docs = [d for d in subs.find({"owner_key": owner}) if d.get("plan") in sc.PAID_PLANS]
    except Exception as exc:
        raise sc.CreditStoreError(f"subscription read failed: {type(exc).__name__}") from exc
    if not docs:
        return None
    live = [d for d in docs if d.get("entitled_until") and _aware(d["entitled_until"]) > now]
    pick = max(live, key=lambda d: (PLAN_RANK[d["plan"]], _aware(d["entitled_until"]))) if live else \
        max(docs, key=lambda d: _aware(d["ls_updated_at"]) if d.get("ls_updated_at") else now)
    data = fetch_subscription(pick["_id"], get)
    attrs = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attrs, dict) or str(data.get("id")) != str(pick["_id"]) or str(attrs.get("store_id")) != _setting("LEMONSQUEEZY_STORE_ID"):
        raise BillingError("unexpected subscription answer")
    urls = attrs.get("urls") if isinstance(attrs.get("urls"), dict) else {}
    url = urls.get("customer_portal")
    if not _checkout_url_ok(url):
        raise BillingError("unexpected portal url")
    return url
