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

Delivery (send_code): SMTP, configured only through environment variables — EMAIL_BACKEND=smtp (or just SMTP_HOST),
EMAIL_FROM, SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_USE_SSL / SMTP_USE_TLS. Nothing is configured
by default: with no provider the sign-up answers "unavailable" instead of creating anything. EMAIL_BACKEND=console
writes the code to the server log for LOCAL DEVELOPMENT ONLY and is refused when SITE_URL is https.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

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

    def issue(self, email, ip_key=None):
        """-> (OK, code, retry_after_seconds) | (COOLDOWN|LIMITED, None, retry_after_seconds). The caller emails the code
        and calls discard() if the email could not be sent, so a failed delivery never burns the visitor's allowance."""
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
        fields = {"code_hash": _hash(email, code), "code_expires_at": now + timedelta(seconds=CODE_TTL_SECONDS),
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
def _truthy(name, default):
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip().lower() in ("1", "true", "yes", "on")


def build_message(to_addr, code, sender):
    msg = EmailMessage()
    msg["Subject"] = f"Your Flintel verification code: {code}"
    msg["From"] = sender
    msg["To"] = to_addr
    minutes = CODE_TTL_SECONDS // 60
    msg.set_content(
        f"Your Flintel verification code is {code}.\n\n"
        f"Enter it in the sign-up window to continue. It expires in {minutes} minutes and works once.\n\n"
        "If you didn't ask for this, you can safely ignore this email — no account is created without the code.\n")
    msg.add_alternative(
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:420px;margin:0 auto;padding:24px;color:#191817">'
        '<p style="font-size:15px;margin:0 0 16px">Your Flintel verification code is</p>'
        f'<p style="font-size:34px;letter-spacing:8px;font-weight:700;margin:0 0 16px">{code}</p>'
        f'<p style="font-size:13px;color:#555;margin:0 0 8px">Enter it in the sign-up window to continue. It expires in {minutes} minutes and works once.</p>'
        '<p style="font-size:12px;color:#888;margin:0">If you didn\'t ask for this, you can safely ignore this email.</p></div>',
        subtype="html")
    return msg


def send_code(to_addr, code):
    """Email the code. Raises EmailNotConfigured / EmailSendFailed; never logs the code except in the dev console backend."""
    backend = (os.getenv("EMAIL_BACKEND") or "").strip().lower()
    host = (os.getenv("SMTP_HOST") or "").strip()
    if backend == "console":
        site = (os.getenv("SITE_URL") or "").strip().lower()
        try:
            import config
            site = str(getattr(config, "SITE_URL", "") or site).lower()
        except Exception:
            pass
        if site.startswith("https://"):
            raise EmailNotConfigured("EMAIL_BACKEND=console is for local development and is refused when SITE_URL is https")
        log.warning(f"[DEV ONLY - EMAIL_BACKEND=console] verification code for {to_addr}: {code}")
        return
    if backend not in ("", "smtp") or not host:
        raise EmailNotConfigured("no email provider configured (set EMAIL_BACKEND=smtp with SMTP_HOST and EMAIL_FROM)")
    sender = (os.getenv("EMAIL_FROM") or "").strip()
    if not sender:
        raise EmailNotConfigured("EMAIL_FROM is not set")
    try:
        port = int((os.getenv("SMTP_PORT") or "587").strip())
    except ValueError:
        raise EmailNotConfigured("SMTP_PORT is not a number")
    user = os.getenv("SMTP_USERNAME") or ""
    password = os.getenv("SMTP_PASSWORD") or ""
    use_ssl = _truthy("SMTP_USE_SSL", port == 465)
    use_tls = _truthy("SMTP_USE_TLS", not use_ssl)
    msg = build_message(to_addr, code, sender)
    try:
        ctx = ssl.create_default_context()
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=12, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=12)
        with server:
            if use_tls and not use_ssl:
                server.starttls(context=ctx)
            if user:
                server.login(user, password)
            server.send_message(msg)
    except Exception as exc:
        log.warning(f"Verification email could not be sent ({type(exc).__name__})")        # never the message text: may echo the address/credentials
        raise EmailSendFailed(type(exc).__name__)


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
