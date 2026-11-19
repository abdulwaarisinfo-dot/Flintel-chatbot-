"""
email_delivery.py - what Resend reports about the emails Flintel sends (webhooks) and the configuration those emails need.

A successful Resend API call means the provider ACCEPTED the request. It says nothing about the inbox. Later, Resend tells us what
happened through signed webhooks; this module verifies them, de-duplicates them and keeps one small state record per email.

Storage: collection `flintel_email_deliveries`, one document per email, _id = the Resend email id:

    state        accepted | sent | delayed | delivered | failed | suppressed | bounced | complained
    rank         the state's rank (see _RANK); a transition is applied only to a STRICTLY higher rank, so a duplicate or an out-of-order
                 event can never move an email backwards (a late "sent" cannot undo "delivered"; a late "delivered" cannot undo "bounced")
    event_ids    the last MAX_EVENT_IDS svix ids already applied. The transition and the id are written in ONE update, so a redelivered
                 webhook cannot apply twice and a crash cannot record an id without its transition
    to_hash      keyed hash of the recipient (never the address); to_domain for diagnostics
    purpose      signup_code | signup_exists_notice
    expires_at   a TTL index purges the record RETENTION_DAYS after it was created

"delivered" means the RECEIVING SERVER accepted the message. It is not inbox (vs spam) placement, which no provider can report.
Nothing here resends, retries, or edits a suppression list: the state is diagnostics only.
"""
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("flintel.email_delivery")

MAX_BODY_BYTES = 256 * 1024
RETENTION_DAYS = 30
MAX_EVENT_IDS = 50
RETRY_UNKNOWN_WITHIN_SECONDS = 120      # an event for an id we have not stored yet, this soon after it happened, may just have beaten our own write

ACCEPTED, SENT, DELAYED, DELIVERED, FAILED, SUPPRESSED, BOUNCED, COMPLAINED = (
    "accepted", "sent", "delayed", "delivered", "failed", "suppressed", "bounced", "complained")
# delivered is NOT final: a bounce can still arrive after the receiving server's first acceptance, and a complaint only ever follows delivery.
_RANK = {ACCEPTED: 0, SENT: 1, DELAYED: 2, DELIVERED: 3, FAILED: 4, SUPPRESSED: 4, BOUNCED: 4, COMPLAINED: 5}
_EVENT_STATE = {"email.sent": SENT, "email.delivered": DELIVERED, "email.delivery_delayed": DELAYED, "email.bounced": BOUNCED,
                "email.complained": COMPLAINED, "email.failed": FAILED, "email.suppressed": SUPPRESSED}
PROBLEM_STATES = frozenset({FAILED, SUPPRESSED, BOUNCED, COMPLAINED})


class WebhookRejected(Exception):
    def __init__(self, http_status, reason):
        super().__init__(reason)
        self.http_status, self.reason = http_status, reason


class WebhookRetry(Exception):
    """Ask the provider to deliver this webhook again later (HTTP 409)."""


# ───────────────────────── configuration ─────────────────────────
_REQUIRED_ENV = ("RESEND_API_KEY", "EMAIL_FROM", "RESEND_WEBHOOK_SECRET")


def config_problems(env=None):
    """Names (never values) of the email settings that are missing. Empty list = complete."""
    env = os.environ if env is None else env
    return [name for name in _REQUIRED_ENV if not str(env.get(name) or "").strip()]


def _hash_recipient(email):
    import config
    key = ("flintel-email-recipient-v1:" + str(config.SESSION_SECRET_KEY)).encode()
    return hmac.new(key, (email or "").strip().lower().encode(), hashlib.sha256).hexdigest()


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class DeliveryStore:
    def __init__(self, collection, clock=None):
        self.c = collection
        self.now = clock or (lambda: datetime.now(timezone.utc))

    def record_accepted(self, email_id, to_email, purpose):
        """The provider accepted this email. Safe to call twice for the same id."""
        now = self.now()
        self.c.update_one(
            {"_id": email_id},
            {"$setOnInsert": {"state": ACCEPTED, "rank": _RANK[ACCEPTED], "state_at": now, "event_ids": [], "created_at": now,
                              "expires_at": now + timedelta(days=RETENTION_DAYS)},
             "$set": {"to_hash": _hash_recipient(to_email), "to_domain": (to_email or "").rsplit("@", 1)[-1][:100], "purpose": purpose}},
            upsert=True)

    def get(self, email_id):
        return self.c.find_one({"_id": email_id})

    def apply_event(self, svix_id, event_type, email_id, data):
        """-> 'applied' | 'duplicate' | 'stale' | 'unknown_email' | 'recipient_mismatch'. Never raises for a bad payload."""
        new_state = _EVENT_STATE[event_type]
        doc = self.c.find_one({"_id": email_id})
        if doc is None:
            return "unknown_email"
        recipients = data.get("to")
        if isinstance(recipients, list) and recipients:
            # The payload's address is only a consistency check against what WE stored for this id - never an identity.
            if not any(isinstance(r, str) and hmac.compare_digest(_hash_recipient(r), str(doc.get("to_hash") or "")) for r in recipients):
                return "recipient_mismatch"
        fields = {"state": new_state, "rank": _RANK[new_state], "state_at": self.now(), "last_event": event_type}
        bounce = data.get("bounce")
        if isinstance(bounce, dict):                                  # only two short enum-like fields; never the free-text message
            for src, dst in (("type", "bounce_type"), ("subType", "bounce_subtype")):
                if isinstance(bounce.get(src), str):
                    fields[dst] = bounce[src][:40]
        res = self.c.update_one(
            {"_id": email_id, "rank": {"$lt": _RANK[new_state]}, "event_ids": {"$ne": svix_id}},
            {"$set": fields, "$push": {"event_ids": {"$each": [svix_id], "$slice": -MAX_EVENT_IDS}}})
        if res.matched_count == 1:
            return "applied"
        again = self.c.find_one({"_id": email_id}) or {}
        return "duplicate" if svix_id in (again.get("event_ids") or []) else "stale"


def default_store():
    import database
    return DeliveryStore(database.email_deliveries_collection)


def _event_age_seconds(created_at, now):
    try:
        return (now - _aware(datetime.fromisoformat(str(created_at).replace("Z", "+00:00")))).total_seconds()
    except Exception:
        return None


def handle_webhook(raw, headers, store, secret=None, now=None):
    """Verify and apply one Resend webhook. `raw` is the EXACT request body (bytes); `headers` is a case-insensitive mapping.
    Returns {"status": ...} for 2xx; raises WebhookRejected (401/400/413/503) or WebhookRetry (409). Never logs the body or the secret."""
    secret = (secret if secret is not None else os.getenv("RESEND_WEBHOOK_SECRET") or "").strip()
    if not secret:
        log.error("Resend webhook received but RESEND_WEBHOOK_SECRET is not set; refusing to process it")
        raise WebhookRejected(503, "webhook_not_configured")
    if len(raw) > MAX_BODY_BYTES:
        raise WebhookRejected(413, "too_large")
    svix_id, ts, sig = headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature")
    if not (svix_id and ts and sig):
        raise WebhookRejected(401, "missing_signature")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise WebhookRejected(401, "invalid_signature")
    import resend
    try:
        event = resend.Webhooks.verify({"payload": text, "headers": {"id": svix_id, "timestamp": ts, "signature": sig}, "webhook_secret": secret})
    except ValueError as exc:
        # the SDK raises ValueError for every failure: a bad/expired signature is 401, a correctly signed but unparsable body is 400
        raise WebhookRejected(400 if "parse webhook payload" in str(exc) else 401, "malformed_payload" if "parse webhook payload" in str(exc) else "invalid_signature")
    if not isinstance(event, dict) or not isinstance(event.get("type"), str) or not isinstance(event.get("data"), dict):
        raise WebhookRejected(400, "malformed_payload")
    etype, data = event["type"], event["data"]
    if etype not in _EVENT_STATE:
        return {"status": "ignored_event_type"}                     # opened/clicked/received/suppression.* ...: acknowledged, not tracked
    email_id = data.get("email_id")
    if not isinstance(email_id, str) or not email_id or len(email_id) > 100:
        raise WebhookRejected(400, "malformed_payload")
    status = store.apply_event(svix_id, etype, email_id, data)
    if status == "unknown_email":
        age = _event_age_seconds(event.get("created_at"), now or datetime.now(timezone.utc))
        if age is not None and age < RETRY_UNKNOWN_WITHIN_SECONDS:
            raise WebhookRetry()                                    # probably beat our own write of the record: ask for a redelivery
        log.info(f"Resend event for an email id this app has no record of (ignored) | event={etype}")
        return {"status": "unknown_email"}
    new_state = _EVENT_STATE[etype]
    if status == "recipient_mismatch":
        log.warning(f"Resend event ignored: recipient does not match the stored record | email_id={email_id} event={etype}")
    elif status == "applied" and new_state in PROBLEM_STATES:
        log.warning(f"Resend email problem | email_id={email_id} event={etype} to_domain={(store.get(email_id) or {}).get('to_domain')} "
                    f"bounce_type={(data.get('bounce') or {}).get('type') if isinstance(data.get('bounce'), dict) else None}")
    else:
        log.info(f"Resend event {status} | email_id={email_id} event={etype}")
    return {"status": status}
