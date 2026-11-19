"""
email_verification.py — emailed 6-digit codes that prove a visitor owns the address they sign up with.

Flow (routes.py): POST /signup/code issues + emails a code -> POST /signup/verify checks it and marks the
browser's signed session as "verified for <email>" for a short time -> POST /signup sets the password for
exactly that address. No account is created before the address is verified.

Storage: one small document per address in `flintel_email_codes` (_id = the normalised address):

    code_hash    HMAC-SHA256(server key, "<address>:<code>") — the code itself is never stored
    code_expires_at  the code stops working here (CODE_TTL_SECONDS after it was sent)
    expires_at   the whole document is purged by a Mongo TTL index here (the end of the hourly send window)
    attempts     wrong guesses against THIS code; MAX_ATTEMPTS and the code is dead (request a new one)
    sent_at      last send; a new code may be requested only RESEND_COOLDOWN_SECONDS later
    window_start / sends   at most MAX_SENDS_PER_HOUR codes per address per hour

and one counter document per client address (_id = "ip:<hash>", at most MAX_SENDS_PER_IP_PER_HOUR sends per hour)
so the endpoint cannot be used to mail arbitrary people in bulk. A code is single-use: the successful check
deletes it in the same step.

Delivery (send_code): the official Resend API (the `resend` package), configured only through environment variables -
RESEND_API_KEY and EMAIL_FROM (an address on a domain verified in Resend). Nothing is configured by default: with no
provider the sign-up answers "unavailable" instead of creating anything. EMAIL_BACKEND=console writes the code to the server
log for LOCAL DEVELOPMENT ONLY and is refused when SITE_URL is https. send_code returns the Resend email id so the caller can
store it (email_delivery.py). API acceptance is NOT inbox delivery: Resend reports what happened later through signed webhooks.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("flintel.email_verification")

CODE_LENGTH = 6
CODE_TTL_SECONDS = 600
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 30
MAX_SENDS_PER_HOUR = 5
MAX_SENDS_PER_IP_PER_HOUR = 20
VERIFIED_SESSION_SECONDS = 900          # how long a verified address may be used to finish creating the account
_HOUR = 3600
_EMAIL_RE = re.compile(r"^[^@\s,;<>()\[\]\\\"']+@[^@\s,;<>()\[\]\\\"']+\.[^@\s,;<>()\[\]\\\"']+$")

# result words (the route maps them to HTTP statuses and user-facing messages)
OK, INVALID, EXPIRED, LOCKED, COOLDOWN, LIMITED = "ok", "invalid", "expired", "locked", "cooldown", "limited"


class EmailNotConfigured(Exception):
    """No usable email provider is configured (or the dev-only console backend is refused)."""


class EmailSendFailed(Exception):
    """The provider rejected or could not deliver the message."""


def normalise_email(value):
    e = (value or "").strip().lower()
    if not e or len(e) > 254 or not _EMAIL_RE.match(e):
        return None
    return e


def _key():
    import config
    return ("flintel-email-code-v1:" + str(config.SESSION_SECRET_KEY)).encode()


def _hash(email, code):
    return hmac.new(_key(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def new_code():
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def client_key(ip):
    return "ip:" + hashlib.sha256(str(ip or "unknown").encode()).hexdigest()[:24]


class CodeStore:
    def __init__(self, collection, clock=None):
        self.c = collection
        self.now = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _aware(dt):
        return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _ip_allowed(self, ip_key):
        """Counts one send for this client; False once the hourly allowance is spent. Never raises into the caller."""
        now = self.now()
        doc = self.c.find_one({"_id": ip_key})
        if doc and self._aware(doc.get("window_start")) and (now - self._aware(doc["window_start"])).total_seconds() < _HOUR:
            if int(doc.get("sends", 0)) >= MAX_SENDS_PER_IP_PER_HOUR:
                return False
            self.c.update_one({"_id": ip_key}, {"$inc": {"sends": 1}})
            return True
        self.c.update_one({"_id": ip_key}, {"$set": {"window_start": now, "sends": 1,
                                                       "expires_at": now + timedelta(seconds=_HOUR * 2)}}, upsert=True)
        return True

    def issue(self, email, ip_key=None, decoy=False):
        """-> (OK, code, retry_after_seconds) | (COOLDOWN|LIMITED, None, retry_after_seconds). The caller emails the code
        and calls discard() if the email could not be sent, so a failed delivery never burns the visitor's allowance.
        decoy=True (the address already has an account): identical cooldown / limits / attempt counting, but the stored hash is of a
        random token no six-digit code can match, so the record can never be verified."""
        now = self.now()
        doc = self.c.find_one({"_id": email})
        window_start, sends = now, 1
        if doc:
            sent_at = self._aware(doc.get("sent_at"))
            if sent_at and (now - sent_at).total_seconds() < RESEND_COOLDOWN_SECONDS:
                return COOLDOWN, None, max(1, int(RESEND_COOLDOWN_SECONDS - (now - sent_at).total_seconds()))
            ws = self._aware(doc.get("window_start"))
            if ws and (now - ws).total_seconds() < _HOUR:
                if int(doc.get("sends", 0)) >= MAX_SENDS_PER_HOUR:
                    return LIMITED, None, max(1, int(_HOUR - (now - ws).total_seconds()))
                window_start, sends = ws, int(doc.get("sends", 0)) + 1
        if ip_key and not self._ip_allowed(ip_key):
            return LIMITED, None, _HOUR
        code = new_code()
        fields = {"code_hash": _hash(email, ("decoy-" + secrets.token_hex(16)) if decoy else code), "code_expires_at": now + timedelta(seconds=CODE_TTL_SECONDS),
                  "expires_at": window_start + timedelta(seconds=_HOUR + 60),
                  "attempts": 0, "sent_at": now, "window_start": window_start, "sends": sends}
        if doc:
            # compare-and-set on the previous send time: two simultaneous requests cannot both be admitted
            res = self.c.update_one({"_id": email, "sent_at": doc.get("sent_at")}, {"$set": fields})
            if res.matched_count == 0:
                return COOLDOWN, None, RESEND_COOLDOWN_SECONDS
        else:
            try:
                self.c.insert_one({"_id": email, **fields})
            except Exception:                                               # duplicate key: someone else just issued
                return COOLDOWN, None, RESEND_COOLDOWN_SECONDS
        return OK, code, RESEND_COOLDOWN_SECONDS

    def discard(self, email):
        """Forget the code just issued (its email never left); the next request starts clean."""
        try:
            self.c.delete_one({"_id": email})
        except Exception as exc:
            log.warning(f"Could not discard an unsent verification code: {exc}")

    def verify(self, email, code):
        """-> OK (the code was right; it is now spent) | INVALID | EXPIRED | LOCKED."""
        code = re.sub(r"\s+", "", str(code or ""))
        now = self.now()
        # count the guess first, atomically, so parallel guesses cannot exceed the allowance
        doc = self.c.find_one_and_update(
            {"_id": email, "attempts": {"$lt": MAX_ATTEMPTS}, "code_hash": {"$exists": True}},
            {"$inc": {"attempts": 1}}, return_document=True)
        if doc is None:
            return LOCKED if self.c.find_one({"_id": email, "code_hash": {"$exists": True}}) else INVALID
        exp = self._aware(doc.get("code_expires_at"))
        if exp is None or exp <= now:
            return EXPIRED
        if not (code.isdigit() and len(code) == CODE_LENGTH) or not hmac.compare_digest(doc["code_hash"], _hash(email, code)):
            return INVALID
        spent = self.c.delete_one({"_id": email, "code_hash": doc["code_hash"]})       # single use, even under a race
        return OK if spent.deleted_count == 1 else INVALID


def default_store():
    import database
    return CodeStore(database.email_codes_collection)


# ───────────────────────── delivery ─────────────────────────────────────────
def build_message(to_addr, code, sender):
    """-> the Resend API parameters for the verification email. Subject, text and HTML are unchanged from the SMTP version."""
    minutes = CODE_TTL_SECONDS // 60
    return {
        "from": sender,
        "to": [to_addr],
        "subject": f"Your Flintel verification code: {code}",
        "text": (f"Your Flintel verification code is {code}.\n\n"
                 f"Enter it in the sign-up window to continue. It expires in {minutes} minutes and works once.\n\n"
                 "If you didn't ask for this, you can safely ignore this email - no account is created without the code.\n"),
        "html": ('<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:420px;margin:0 auto;padding:24px;color:#191817">'
                 '<p style="font-size:15px;margin:0 0 16px">Your Flintel verification code is</p>'
                 f'<p style="font-size:34px;letter-spacing:8px;font-weight:700;margin:0 0 16px">{code}</p>'
                 f'<p style="font-size:13px;color:#555;margin:0 0 8px">Enter it in the sign-up window to continue. It expires in {minutes} minutes and works once.</p>'
                 '<p style="font-size:12px;color:#888;margin:0">If you didn\'t ask for this, you can safely ignore this email.</p></div>'),
        "tags": [{"name": "purpose", "value": "signup_code"}],
    }


def build_exists_notice(to_addr, sender):
    """The email sent INSTEAD of a code when the address already has an account, so the response (and timing) never reveals it."""
    return {
        "from": sender,
        "to": [to_addr],
        "subject": "You already have a Flintel account",
        "text": ("Someone (hopefully you) tried to create a Flintel account with this email address, but one already exists.\n\n"
                 "Sign in with the method you used before (Google, or your email and password). No new account was created and nothing has changed.\n\n"
                 "If this wasn't you, you can safely ignore this email.\n"),
        "html": ('<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:420px;margin:0 auto;padding:24px;color:#191817">'
                 '<p style="font-size:15px;margin:0 0 12px">You already have a Flintel account.</p>'
                 '<p style="font-size:13px;color:#555;margin:0 0 8px">Someone (hopefully you) tried to create an account with this email address. '
                 'Sign in with the method you used before (Google, or your email and password). No new account was created and nothing has changed.</p>'
                 '<p style="font-size:12px;color:#888;margin:0">If this wasn\'t you, you can safely ignore this email.</p></div>'),
        "tags": [{"name": "purpose", "value": "signup_exists_notice"}],
    }


_SEND_TIMEOUT_SECONDS = 10


def _classify_resend_error(exc):
    """-> (kind, configuration_problem). Uses only the exception type, HTTP status and error type; the message is never logged because
    it can echo the recipient address. Configuration problems (bad key, unverified sender domain) are the operator's to fix, not the visitor's."""
    import resend.exceptions as rx
    if isinstance(exc, (rx.MissingApiKeyError, rx.InvalidApiKeyError)):
        return "auth_failed", True
    if isinstance(exc, rx.RateLimitError):
        return "rate_limited", False
    if isinstance(exc, rx.ApplicationError):
        return "provider_error", False
    if isinstance(exc, rx.ResendError) and getattr(exc, "error_type", "") == "HttpClientError":
        return "network_error", False                                # the SDK wraps a timeout / connection failure like this
    if isinstance(exc, rx.ResendError):
        msg = str(getattr(exc, "message", "") or "").lower()
        if "domain" in msg or "`from`" in msg or "from field" in msg or "sender" in msg:
            return "invalid_sender", True
        if "`to`" in msg or "to field" in msg or "recipient" in msg:
            return "invalid_recipient", False
        code = str(getattr(exc, "code", ""))
        return ("provider_error" if code.startswith("5") else "rejected_request"), False
    if isinstance(exc, RuntimeError):                                # the SDK's wrapper for a network error / timeout
        return "network_error", False
    return "send_error", False


def _resend_send(params, to_addr):
    """Call the Resend API once. -> the email id (str) or None if the response had none. Raises EmailNotConfigured / EmailSendFailed.
    No retry loop: a failed send is reported once and the caller discards the code so the visitor can simply ask again."""
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not key:
        raise EmailNotConfigured("RESEND_API_KEY is not set")
    import resend
    resend.api_key = key
    if not isinstance(resend.default_http_client, resend.RequestsClient) or getattr(resend.default_http_client, "_timeout", None) != _SEND_TIMEOUT_SECONDS:
        resend.default_http_client = resend.RequestsClient(timeout=_SEND_TIMEOUT_SECONDS)
    to_domain = to_addr.rsplit("@", 1)[-1]
    try:
        resp = resend.Emails.send(params)
    except Exception as exc:
        kind, config_problem = _classify_resend_error(exc)
        log.warning(f"Verification email NOT accepted by Resend | kind={kind} error={type(exc).__name__} "
                    f"http_status={getattr(exc, 'code', 'n/a')} error_type={getattr(exc, 'error_type', 'n/a')} to_domain={to_domain}")
        if config_problem:
            raise EmailNotConfigured(f"Resend rejected the request ({kind}); check RESEND_API_KEY and that EMAIL_FROM is on a verified domain")
        raise EmailSendFailed(kind)
    email_id = resp.get("id") if isinstance(resp, dict) else None
    email_id = email_id if isinstance(email_id, str) and email_id else None
    # Acceptance by the API is NOT delivery and says nothing about inbox placement; bounces/suppression/spam arrive later as webhooks.
    log.info(f"Email accepted by Resend API (not proof of delivery) | email_id={email_id} to_domain={to_domain}")
    return email_id


def _sender_or_raise():
    sender = (os.getenv("EMAIL_FROM") or "").strip()
    if not sender:
        raise EmailNotConfigured("EMAIL_FROM is not set")
    if "@" not in sender:
        raise EmailNotConfigured("EMAIL_FROM is not a valid sender address")
    return sender


def _backend_or_raise():
    backend = (os.getenv("EMAIL_BACKEND") or "").strip().lower()
    if backend == "smtp":
        raise EmailNotConfigured("EMAIL_BACKEND=smtp is no longer supported: set RESEND_API_KEY and EMAIL_FROM (the Resend API is used)")
    if backend not in ("", "resend", "console"):
        raise EmailNotConfigured("EMAIL_BACKEND must be unset, 'resend' or 'console'")
    return backend


def _console_refused_on_https():
    site = (os.getenv("SITE_URL") or "").strip().lower()
    try:
        import config
        site = str(getattr(config, "SITE_URL", "") or site).lower()
    except Exception:
        pass
    if site.startswith("https://"):
        raise EmailNotConfigured("EMAIL_BACKEND=console is for local development and is refused when SITE_URL is https")


def send_code(to_addr, code):
    """Email the code. -> the Resend email id (None for the dev console backend). Raises EmailNotConfigured / EmailSendFailed;
    never logs the code except in the dev console backend."""
    if _backend_or_raise() == "console":
        _console_refused_on_https()
        log.warning(f"[DEV ONLY - EMAIL_BACKEND=console] verification code for {to_addr}: {code}")
        return None
    return _resend_send(build_message(to_addr, code, _sender_or_raise()), to_addr)


def send_account_exists_notice(to_addr):
    """Sent instead of a code when the address already has an account. -> the Resend email id (None for console). Same errors as send_code."""
    if _backend_or_raise() == "console":
        _console_refused_on_https()
        log.warning(f"[DEV ONLY - EMAIL_BACKEND=console] account-exists notice for {to_addr}")
        return None
    return _resend_send(build_exists_notice(to_addr, _sender_or_raise()), to_addr)


def verified_marker(email, now=None):
    return {"email": email, "until": int((now or time.time()) + VERIFIED_SESSION_SECONDS)}


def verified_email(session, now=None):
    """The address this browser proved it owns (and has not used yet), or None."""
    try:
        m = session.get("signup_verified")
        if isinstance(m, dict) and isinstance(m.get("email"), str) and int(m.get("until", 0)) > (now or time.time()):
            return m["email"]
    except Exception:
        pass
    return None
