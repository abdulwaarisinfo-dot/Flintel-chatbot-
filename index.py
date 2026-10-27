"""
FLINTEL — WEB SERVICE (app wiring + chat/session/Mongo orchestration)
============================================================================
This module owns: the FastAPI app itself (middleware, no-cache headers,
session setup, Google OAuth registration), password hashing, and every
`flintel_users` / `flintel_users_chat` Mongo-backed function (user
accounts, chat sessions, message bookkeeping, the busy-lock, the  
background-trigger orchestration for Google-fallback/search-progress,
and the two functions that decide when a message's answer is ready to  
fill in). 

Every constant this module used to define directly now lives in
config.py (imported here via `from config import *`, so every existing
name — MAX_KEYWORDS, RESPONSE_TIMEOUT, etc. — is still available here
and still re-exportable to routes.py exactly as before).

Every piece of "brain" logic — signal matching (including phrase
matching), keyword generation/fallback, the router, the Claude/OpenAI
analysis layer and its prompts, the website-keyword-extraction wrapper,
the single LLM call function, and the answer-finalization/URL-patching
helpers — now lives in logics.py (imported here via an explicit
`from logics import (...)` list below), so routes.py's own existing
`from index import (..., normalize_topic_key, ..., get_matched_signals,
analyze_with_claude, _call_claude, ...)` line works with ZERO changes:
index.py still exposes every one of those names, just re-exported
instead of defined locally.

(MODEL SWAP) The LLM this product calls has moved from Claude Haiku to
GPT-5 mini via OpenAI's Responses API — see logics.py's _call_claude()
for the actual call. Nothing in THIS file changed as a result, since
every call site here only ever used _call_claude()/analyze_with_claude()
by name, never by talking to the API directly.

(TIMEOUT-SIMPLIFICATION CHANGE) logics.py's _timeout_fallback_answer()
no longer has a "closest match" tier-3 fallback — see that function's
own docstring in logics.py. Nothing in this file needed to change for
that either.

(DUPLICATE-REFRESH FIX) `_fill_in_message_outputs()` now checks
`_is_owner_busy()` before scheduling either of the two Claude-calling
branches (the normal "needs answer" branch and the RESPONSE_TIMEOUT /
tier-3 fallback branch) — previously only `_set_owner_busy()` ran there,
with nothing checking whether a Claude call for this owner was already
in flight. A refresh/poll landing on a message whose `claude_answer` was
still `None` would therefore schedule a brand-new, independent Claude
call every single time, double-billing Claude and racing with the
original call over whose result gets saved last. See each call site
below for the full rationale — no other logic in this module changed.

(WEBSITE INTELLIGENCE) Three additions, all backward-compatible:
  (A) add_search_to_chat() takes two new optional params, `website_only`
      and `website_evidence`, stored on the message.
  (B) _complete_message_answer_and_results() has a new `website_only`
      branch that builds the answer via
      website_intelligence.build_website_insight_answer() instead of the
      normal Reddit-evidence analyze_with_claude() path, and forces
      `results` to [].
  (C) save_last_website_context_to_chat() stores the chat's most recent
      website context at the chat-document level, for vague follow-ups.
  Plus (D): _fill_in_message_outputs() short-circuits `website_only`
  messages straight to (B), so their answer never waits on Reddit/Google
  evidence (or the RESPONSE_TIMEOUT fallback) to show up.

(BUG FIX PACK #2 — CHAT SUMMARY CONTEXT + REFRESH/DUPLICATE-PROMPT)
  (1) _extract_summary_text_from_claude_answer() now also pulls
      "executive_summary"/"conclusion" and a "followup_question" string
      out of an analyst-style JSON answer — these are newer fields the
      JSON-ANALYSIS-PROMPT format can emit that the original field list
      didn't know about, so the rolling chat summary was silently
      dropping them.
  (2) append_to_chat_summary() takes an optional `keywords` param and,
      when given, appends a short "[searched: ...]" note to the summary
      line so later continuity/router reads know what was actually
      searched for.
  (3) add_search_to_chat() takes an optional `website_note` param,
      stored on the message and folded into analyze_with_claude()'s
      extra_context by _complete_message_answer_and_results() (and
      forwarded into _timeout_fallback_answer()'s tier-3 flow too).
  (4) REFRESH/DUPLICATE-PROMPT FIX: topic_key is derived only from the
      query text, so resending the exact same prompt in the same chat
      creates a second message with the same topic_key. The five
      save/mark helpers used to update via Mongo's positional `$`
      operator, which always resolves to the FIRST matching array
      element — so a repeated prompt's answer/results/flags always
      landed on the OLDEST message with that topic_key, and the new
      message's fields stayed None forever. These helpers now go
      through a shared _set_on_latest_message() helper that updates the
      most recent matching message by its actual array index instead.

(SIGNALS_COLLECTION_2 CONFIRMATION — this file unchanged) `index.py`
reaches signal matching in exactly one place: `_fill_in_message_outputs()`
passes `get_matched_signals` BY REFERENCE as the `matcher_fn=` argument
to `get_evidence_with_topup()` — it never calls `get_matched_signals()`
(or `get_unfiltered_matched_signals()`, which doesn't appear in this file
at all) directly. `get_evidence_with_topup()` itself (defined in
logics.py) is the one place that resolves/injects `signals_collection_2`
before invoking `matcher_fn`, so nothing here needs to import
`signals_collection_2` or pass it manually. No code in this file changed
as a result — this note is purely documentation confirming that fact for
future readers.
"""

import re
import json
import time
import uuid
import logging
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response as _StarletteResponse   # (FAST NAVIGATION) gzip_html_pages
import gzip as _gzip                                             # (FAST NAVIGATION) gzip_html_pages
from passlib.context import CryptContext
from authlib.integrations.starlette_client import OAuth

import flintel
import scheduler as trending_keywords_scheduler   # 12-hourly trending-keywords background job
import guest_gate      # (GUEST GATE) server-side limit on a visitor's genuine searches
import turn_finisher   # (NON-BLOCKING CHAT PAGE) durable finishing of older unfinished research turns
import google as google_search   # the new google.py module
    # (renamed on import to `google_search` to avoid any ambiguity
    # with the unrelated third-party `google` package some
    # environments have installed — module FILE stays google.py, only
    # the import alias differs)
from database import (
    db,
    jobs_collection,
    signals_collection,
    users_collection,
    chats_collection,
    busy_owners_collection,
    google_posts_collection,
)

from config import *          # every constant, unchanged names
def _cfg_strict_wait() -> float:
    """STRICT_WAIT_SECONDS read live from config (default 75)."""
    try:
        import config as _c
        return float(getattr(_c, "STRICT_WAIT_SECONDS", 75))
    except Exception:
        return 75.0


from logics import (
    normalize_topic_key, normalize_platform, normalize_platforms, supported_platforms, unsupported_requested, generate_fuzzy_keywords,
    enrich_no_results_answer,   # (NO-RESULTS UI)
    freshness_enabled, enrich_freshness_answer,   # (FRESHNESS DISCLOSURE)
    enqueue_search_job, get_matched_signals, analyze_with_claude,
    analyze_with_claude_stream, _extract_json_object_from_text,   # (REAL-TIME STREAMING) re-exported for routes.py's stream_answer()
    _call_claude, classify_and_maybe_chat,
    resolve_unclear_topic, _extract_first_url, fetch_website_text,
    extract_keywords_from_website, _extract_claude_format,
    _extract_near_match_confidence, _NO_DATA_CLAUDE_FORMATS,
    _patch_post_urls_into_answer, _inject_website_context_into_answer, _usage_attribution, _usage_request_id,
    _finalize_answer_and_results, _timeout_fallback_answer, build_time_scope, time_scope_context,
    get_evidence_with_topup, _resolved_request_note,
    strict_intent_mode, strict_wait_done, get_scan_status,
    CLAUDE_BLOCKED_FALLBACK_REPLY, CLAUDE_CLARIFY_FALLBACK_REPLY,
    CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT,
)

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
# logging.basicConfig() is now called once, in database.py (imported above)
# — calling it again here would just be a harmless no-op, but this module's
# own logger identity ("flintel-web") is still defined here, since that's
# unrelated to where the shared config lives.

log = logging.getLogger("flintel-web")

# ─────────────────────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Flintel Web Service — v7",
    # v4.2: docs/redoc/openapi.json are internal implementation detail, not a
    # public product surface — block all three so /docs, /redoc, and
    # /openapi.json 404 instead of exposing every route + schema to anyone.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
templates = Jinja2Templates(directory="templates")
# (SEARCH CREDITS) A fresh submit key per rendered /search form; JavaScript
# replaces it on every intentional submit, automatic retries keep it.
import secrets as _secrets
templates.env.globals.setdefault("new_submit_key", lambda: _secrets.token_hex(16))

# Required for login sessions (stores a signed cookie, not the DB user doc).
# Also doubles as where we stash the anonymous chat-owner UUID and the
# currently active chat_id, exactly like a browser-local "current chat"
# pointer in Claude/ChatGPT.
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# (GUEST IDENTITY) A guest's id used to live only in the 14-day session cookie, so it
# vanished with it (and with the guest's chats). It is now ALSO kept in a separate,
# long-lived, SIGNED first-party cookie. The id is always generated by the server; the
# browser can neither choose one nor alter it (a bad signature or shape is ignored).
# get_anon_id() resolves it; this middleware only writes the cookie when the id is new
# or was recovered from the session. Clearing cookies / another browser / incognito
# produce a new identity — that limit is inherent to anonymous visitors.
# ─────────────────────────────────────────────────────────────────────────────
from itsdangerous import URLSafeSerializer as _URLSafeSerializer, BadSignature as _BadSignature

GUEST_COOKIE_NAME = "fl_gid"
_GUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_guest_cookie_signer = _URLSafeSerializer(SESSION_SECRET_KEY, salt="flintel-guest-id-v1")


def _read_guest_cookie(request: Request):
    raw = request.cookies.get(GUEST_COOKIE_NAME)
    if not raw or len(raw) > 200:
        return None
    try:
        gid = _guest_cookie_signer.loads(raw)
    except _BadSignature:
        return None
    except Exception:
        return None
    return gid if isinstance(gid, str) and _GUEST_ID_RE.match(gid) else None


def _guest_cookie_secure(request: Request) -> bool:
    """Secure flag whenever the visitor reached us over HTTPS: the proxy's X-Forwarded-Proto, the request's own
    scheme, or — when the proxy sends no forwarding header — a configured https SITE_URL (the public origin)."""
    try:
        import config as _config_module   # read at call time (tests patch it)
        if str(getattr(_config_module, "SITE_URL", "") or "").lower().startswith("https://"):
            return True
    except Exception:
        pass
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").split(",")[0].strip().lower()
    return proto == "https"


@app.middleware("http")
async def emit_guest_identity_cookie(request: Request, call_next):
    response = await call_next(request)
    gid = getattr(request.state, "set_guest_cookie", None)
    if gid and _GUEST_ID_RE.match(str(gid)):
        import config as _config_module   # read at call time (tests patch it)
        days = int(getattr(_config_module, "GUEST_ID_COOKIE_MAX_AGE_DAYS", 400))
        response.set_cookie(
            GUEST_COOKIE_NAME, _guest_cookie_signer.dumps(gid), max_age=days * 86400, path="/",
            httponly=True, samesite="lax", secure=_guest_cookie_secure(request))
    return response

# (NO-CACHE FIX) Every page this app serves is per-user, dynamic content
# (a specific user's chat history, their active chat, credits, etc.) — none
# of it should ever be cached by a shared intermediary (a CDN, a reverse
# proxy like Varnish, or the browser's own disk cache). Without this, a
# request that happens to be served from a stale cached copy can render an
# incomplete/older version of the page (e.g. missing the docked search box
# on a chat page that was cached before it was fully rendered) until the
# cache entry naturally expires or gets revalidated — which looks exactly
# like "it's broken the first time, then fixes itself" once a later
# request (to the same or a different page) causes a fresh fetch. This
# applies the standard "never cache this" header set to every response
# this app returns, closing off that entire class of stale-cache bugs at
# the source rather than trying to chase down each individual symptom.
@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    # (FAVICON) a route that sets its own PUBLIC cache header (the brand icons)
    # keeps it; every other response still gets the no-store set below.
    if response.headers.get("Cache-Control", "").startswith("public"):
        return response
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# (FAST NAVIGATION) Compress HTML pages only. A chat page is mostly repetitive
# markup (hundreds of KB for a long chat); gzip makes it a small fraction of
# that. Deliberately NOT Starlette's GZipMiddleware: the pinned Starlette
# version would also compress the text/event-stream answers and could hold
# streamed chunks back. Everything that is not text/html passes untouched.
@app.middleware("http")
async def gzip_html_pages(request: Request, call_next):
    response = await call_next(request)
    if (not response.headers.get("content-type", "").startswith("text/html")
            or "gzip" not in request.headers.get("accept-encoding", "").lower()
            or "content-encoding" in response.headers):
        return response
    if hasattr(response, "body_iterator"):
        body = b"".join([chunk async for chunk in response.body_iterator])
    else:
        body = response.body
    raw = [(k, v) for k, v in response.headers.raw if k.lower() not in (b"content-length", b"content-encoding")]
    if len(body) >= 1024:
        body = _gzip.compress(body, compresslevel=5)
        raw.append((b"content-encoding", b"gzip"))
        if not any(k.lower() == b"vary" for k, _ in raw):
            raw.append((b"vary", b"Accept-Encoding"))
    new = _StarletteResponse(content=body, status_code=response.status_code)
    new.raw_headers = raw + [(b"content-length", str(len(body)).encode())]   # keeps every Set-Cookie
    return new

# Starts the 12-hourly trending-keywords → Google-search background job
# (scheduler.py). Fire-and-forget: schedules an asyncio task and returns
# immediately, never blocks app startup or any request.
@app.on_event("startup")
async def _start_trending_keywords_scheduler():
    trending_keywords_scheduler.start_trending_keywords_scheduler()

# Password hashing context — bcrypt, industry-standard for this use case.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Google OAuth client.
oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _extract_summary_text_from_claude_answer(answer: str) -> str:
    """(BUG FIX — CHAT-SUMMARY CONTEXT LOSS) append_to_chat_summary()
    used to blindly character-trim the raw claude_answer string to
    CHAT_SUMMARY_TURN_CHAR_LIMIT characters. Since the JSON-ANALYSIS-
    PROMPT-SWAP, claude_answer is a JSON document, so trimming it as raw
    text usually cuts the string off mid-object — discarding exactly the
    fields (a suggested next term, a followup, a likely_reason) a later
    confirmation-style reply ("yes", "sure", "search it", "haan karo")
    needs to resolve against. This is a purely additive, best-effort
    pre-processing step: it parses the answer as JSON and pulls out the
    human-readable parts most useful for conversational continuity,
    BEFORE the caller applies its own char-limit trim.

    Generic by design — reads whatever fields are present on whatever
    format the answer happens to be, never hardcodes a topic, keyword,
    or industry. Falls back to the original raw string unchanged if the
    answer isn't valid JSON (e.g. a plain "chat"/"blocked"/"clarify"
    reply, which is already plain text and doesn't need this treatment)
    or has none of the known fields — so this can only ever IMPROVE the
    summary's usefulness, never make it worse than doing nothing.

    (BUG FIX PACK #2) The field list now also includes
    "executive_summary" and "conclusion" — newer analyst-style JSON
    formats emit these instead of (or alongside) "summary"/"trend", and
    the original list was silently skipping them, so an executive-
    summary-only answer contributed NOTHING to the rolling chat summary.
    A top-level "followup_question" string (distinct from the
    "followups" list already handled below) is also pulled in, for the
    same reason."""
    if not answer:
        return answer or ""

    cleaned = answer.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()

    data = None
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            data = parsed
    except (ValueError, TypeError):
        data = None

    if not data:
        # Not JSON (a plain chat/blocked/clarify reply, or unparseable
        # output) — nothing to extract, use the text exactly as given.
        return answer

    parts = []
    for key in ("executive_summary", "summary", "message", "likely_reason",
                "interpretation", "trend", "conclusion"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())

    followup_question = data.get("followup_question")
    if isinstance(followup_question, str) and followup_question.strip():
        parts.append(followup_question.strip())

    followups = data.get("followups")
    if isinstance(followups, list):
        parts.extend(f.strip() for f in followups if isinstance(f, str) and f.strip())

    suggested_actions = data.get("suggested_actions")
    if isinstance(suggested_actions, list):
        for action in suggested_actions:
            if not isinstance(action, dict):
                continue
            for field_name in ("suggestion", "label"):
                val = action.get(field_name)
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())

    clarifying_question = data.get("clarifying_question")
    if isinstance(clarifying_question, str) and clarifying_question.strip():
        parts.append(clarifying_question.strip())

    if not parts:
        return answer

    return " | ".join(parts)


def append_to_chat_summary(chat_id: str, owner_key: str, query: str, answer: str, keywords: list = None,
                           resolved_query: str = None):
    """(v5) Keeps a short, PLAIN-PYTHON (no extra Claude call) running
    summary on the chat doc itself — one condensed line per turn. This is
    what classify_and_maybe_chat() reads for continuity, so passing
    conversation context to Claude stays cheap and small no matter how
    long a chat gets — Claude is never sent the full raw message history,
    only this rolling, auto-generated summary. Keeps only the last
    CHAT_SUMMARY_MAX_TURNS lines; older lines roll off automatically.
    Best-effort: never allowed to raise past its caller.

    (BUG FIX — CHAT-SUMMARY CONTEXT LOSS) `answer` is now run through
    _extract_summary_text_from_claude_answer() before trimming, so a
    JSON-format answer contributes its actual human-readable fields to
    the summary instead of getting sliced mid-object by the raw
    character trim — see that function's own docstring for why this
    matters for confirmation-style follow-up replies.

    (BUG FIX PACK #2) `keywords` (NEW, optional, default None — every
    existing caller that doesn't pass it behaves exactly as before)
    appends a short "[searched: ...]" note (up to the first 4 keywords)
    to the summary line for a search-type turn, so later continuity/
    router reads can tell what was actually searched for, not just what
    was asked.

    (MULTI-TURN CONTEXT) `resolved_query` (optional, default None => line
    identical to before) is the router's standalone restatement of a
    follow-up ("Now in USA" -> "Find users looking for AI jobs in the
    USA"). When it differs from the literal message it is kept on the same
    line, so every line stays self-contained and the subject of a long
    chain of short follow-ups never rolls off with the oldest line."""
    if not chat_id or not owner_key:
        return
    digest = _extract_summary_text_from_claude_answer(answer)
    user_part = _trim(query, CHAT_SUMMARY_TURN_CHAR_LIMIT)
    if _resolved_request_note(query, resolved_query):
        user_part += f" [meaning: {_trim(' '.join(resolved_query.split()), CHAT_SUMMARY_TURN_CHAR_LIMIT)}]"
    kw_note = ""
    if keywords:
        kw_list = [k for k in keywords[:4] if isinstance(k, str)]
        if kw_list:
            kw_note = f" [searched: {', '.join(kw_list)}]"
    line = (
        f"User: {user_part} | "
        f"Assistant: {_trim(digest, CHAT_SUMMARY_TURN_CHAR_LIMIT)}{kw_note}"
    )

    chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key}, {"summary": 1})
    existing_summary = (chat or {}).get("summary") or ""
    existing_lines = [l for l in existing_summary.split("\n") if l.strip()]
    existing_lines.append(line)
    trimmed_lines = existing_lines[-CHAT_SUMMARY_MAX_TURNS:]

    chats_collection.update_one(
        {"chat_id": chat_id, "owner_key": owner_key},
        {"$set": {"summary": "\n".join(trimmed_lines)}},
    )


def _elapsed_seconds(dt) -> float:
    """(v6) Best-effort elapsed-seconds calculation from a stored
    `requested_at` timestamp. Mongo may hand this back as a naive
    datetime (it was still stored as UTC under the hood) or as an
    already-aware one — this normalizes either case to UTC before
    diffing against "now", so the RESPONSE_TIMEOUT comparison below never
    raises on a naive/aware mismatch. Returns 0.0 (i.e. "just requested,
    definitely not timed out") for anything that isn't a real datetime,
    so a missing/corrupt timestamp can never accidentally trigger the
    timeout fallback early."""
    if not isinstance(dt, datetime):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _serialize_user(user: dict) -> dict:
    """Strips the password hash before a user doc ever reaches a template."""
    if not user:
        return None
    safe = dict(user)
    safe.pop("password_hash", None)
    safe["id"] = str(safe.pop("_id"))
    return safe


def get_current_user(request: Request):
    """Reads the logged-in user (if any) from the session cookie."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    try:
        user = users_collection.find_one({"_id": ObjectId(user_id)})
    except InvalidId:
        return None
    return _serialize_user(user)


def _log_user_in(request: Request, user_doc: dict):
    request.session["user_id"] = str(user_doc["_id"])
    request.session["email"] = user_doc.get("email")


def create_email_user(email: str, password_hash: str):
    now = datetime.now(timezone.utc)
    result = users_collection.insert_one({
        "email":          email,
        "name":           None,
        "google_id":      None,
        "password_hash":  password_hash,
        "provider":       "email",
        "created_at":     now,
        "last_login_at":  now,
    })
    return users_collection.find_one({"_id": result.inserted_id})


def upsert_google_user(google_id: str, email: str, name: str, picture: str = None, created_out: dict = None):
    """Logs in an existing Google user, links Google to a matching
    email/password account, or creates a brand-new account. `created_out`, when a dict is passed, gets
    {"created": True} ONLY when this call created the account (a login or a link to an existing account leaves it empty)."""
    now = datetime.now(timezone.utc)

    user = users_collection.find_one({"google_id": google_id})
    if user:
        set_fields = {"last_login_at": now}
        if picture:
            set_fields["picture"] = picture
        users_collection.update_one({"_id": user["_id"]}, {"$set": set_fields})
        return users_collection.find_one({"_id": user["_id"]})

    # Same email already registered manually -> link the Google identity
    # to that existing account instead of creating a duplicate.
    existing = users_collection.find_one({"email": email})
    if existing:
        set_fields = {"google_id": google_id, "name": name, "provider": "both", "last_login_at": now}
        if picture:
            set_fields["picture"] = picture
        users_collection.update_one(
            {"_id": existing["_id"]},
            {"$set": set_fields},
        )
        return users_collection.find_one({"_id": existing["_id"]})

    new_doc = {
        "email":          email,
        "name":           name,
        "google_id":      google_id,
        "password_hash":  None,
        "provider":       "google",
        "created_at":     now,
        "last_login_at":  now,
        "picture":        picture,
    }
    result = users_collection.insert_one(new_doc)
    if isinstance(created_out, dict):
        created_out["created"] = True
    return users_collection.find_one({"_id": result.inserted_id})


# ─────────────────────────────────────────────────────────────────────────────
# CHAT / SESSION MEMORY (`flintel_users_chat`)
#
# Mirrors how Claude/ChatGPT handle conversations:
#   - Every user (signed in or anonymous) gets a stable "owner_key".
#       * signed in  -> owner_key = their email
#       * anonymous  -> owner_key = a random UUID kept in the session cookie
#   - Searches happen inside a "chat" doc, auto-titled from the first
#     query, the same way a new Claude/ChatGPT conversation gets named
#     after your first message.
#   - Each search message holds:
#       * `results`      -> post cards data (title/post_text/post_url/
#                            platform), UNCHANGED from v3, shown as-is
#                            (BUGFIX PACK #1: except now suppressed for
#                            that render/save when claude_answer's own
#                            format says nothing relevant was found — see
#                            _fill_in_message_outputs() below).
#       * `claude_answer` -> (v4) Claude's answer text to the user's own
#                            prompt, grounded in those same matched posts
#                            (title + text only). Only this OUTPUT is
#                            stored — the posts fed in as input are never
#                            duplicated here, since they already live in
#                            flintel_signals. (v6: if no posts are ever
#                            matched within RESPONSE_TIMEOUT seconds, this
#                            instead ends up holding a plain "nothing
#                            found on this yet" answer.)
#       * `time_window_days` -> (NEW) the optional time window the router
#                            parsed out of the user's own message (int, or
#                            None if the user gave no time range). Stored
#                            on the message exactly like `keywords` is, so
#                            it stays consistent across page reloads and
#                            future re-matching of the same message.
#   - (v5) A message may instead be `"message_type": "chat"` — a plain
#     conversational turn the v5 router decided didn't need any data
#     pulled at all (including, as of v6, a polite decline for a
#     "blocked" message, and now also a clarifying question for a
#     "clarify" message). These have no topic_key/keywords/results, only
#     `query` + `claude_answer`, and never touch flintel_search_jobs or
#     flintel_signals in any way. Messages with no `message_type` (every
#     message from before this update, and every new search-type
#     message) are treated as ordinary search messages, exactly as before.
#   - (v5) `summary` — a short, plain-Python rolling digest of the chat
#     (see append_to_chat_summary above), used purely to give the
#     router/chat-reply calls cheap continuity without ever sending
#     Claude the full raw message history.
#   - (WEBSITE INTELLIGENCE) `last_website_context` — chat-level (not
#     message-level) record of the most recent website analysis in this
#     chat, see save_last_website_context_to_chat() below.
#   - When an anonymous user signs up / logs in, their guest chats are
#     re-keyed onto their email so nothing is lost.
#   - Because chats are always looked up by owner_key (the email, once
#     signed in), logging out and logging back in with the same email
#     brings the exact same chat history back — nothing is deleted on
#     logout.
#   - (v4.3) A single chat can be deleted by its owner — see
#     delete_chat_session() below — without touching any other chat, and
#     without letting anyone but that same owner_key delete it.
#
# COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP — this entire
# section is untouched aside from the one new `time_window_days`
# parameter on add_search_to_chat() described above; `keywords` is stored
# on the message exactly as before, regardless of which source produced
# it.
# ─────────────────────────────────────────────────────────────────────────────

def peek_guest_id(request: Request):
    """The guest id this browser already has (signed durable cookie, else the session), or None.
    Never creates one — used where a guest id is only needed if it exists (sign-in migration)."""
    return _read_guest_cookie(request) or request.session.get("anon_id") or None


def get_anon_id(request: Request) -> str:
    """Stable random ID for a guest (not signed in) visitor. Resolution order:
    1. the signed durable cookie (survives the 14-day session expiry),
    2. the id already in the session (every existing guest keeps their id AND their chats —
       it is adopted into the durable cookie on this response),
    3. a new server-generated id.
    The session copy is kept in step so everything that reads session["anon_id"] still works."""
    cookie_id = _read_guest_cookie(request)
    anon_id = cookie_id or request.session.get("anon_id")
    if not anon_id:
        anon_id = uuid.uuid4().hex
    if request.session.get("anon_id") != anon_id:
        request.session["anon_id"] = anon_id
    if cookie_id != anon_id:
        request.state.set_guest_cookie = anon_id     # emit_guest_identity_cookie() writes it
    return anon_id


def get_owner(request: Request):
    """Returns (owner_key, owner_type) for whoever is making the request.
    Signed-in users are keyed by email; guests are keyed by their anon
    UUID."""
    user = get_current_user(request)
    if user and user.get("email"):
        return user["email"], "email"
    return get_anon_id(request), "anon"


def increment_message_count(owner_key: str):
    """Atomically increments message_count on the user document for email-keyed (logged-in) owners.
    No-ops silently for anonymous UUID owner_keys and if the user document is not found."""
    if not owner_key or '@' not in owner_key:
        return
    try:
        users_collection.update_one({"email": owner_key}, {"$inc": {"message_count": 1}})
    except Exception:
        pass


def generate_chat_title(query: str) -> str:
    """Auto-names a chat from its first query, the same way Claude/ChatGPT
    title a new conversation from your first message."""
    title = (query or "").strip()
    if len(title) > 60:
        title = title[:57].rstrip() + "..."
    return title or "New chat"


def create_chat_session(owner_key: str, owner_type: str, title: str = None) -> str:
    now = datetime.now(timezone.utc)
    chat_id = uuid.uuid4().hex
    chats_collection.insert_one({
        "chat_id":     chat_id,
        "owner_key":   owner_key,
        "owner_type":  owner_type,  # "email" | "anon"
        "title":       title or "New chat",
        "messages":    [],
        "summary":     "",  # (v5) rolling plain-Python conversation digest
        "created_at":  now,
        "updated_at":  now,
    })
    log.info(f"Chat created | chat_id={chat_id} | owner_key={owner_key} | owner_type={owner_type}")
    return chat_id


def get_chat_session(chat_id: str, owner_key: str):
    """Only returns the chat if it belongs to this owner_key — a guest or
    another account can never read someone else's chat by guessing an id."""
    if not chat_id:
        return None
    return chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key}, {"_id": 0})


def get_user_chats(owner_key: str):
    """All chats for this owner, most recently active first — the sidebar
    list, like Claude/ChatGPT's conversation history."""
    return list(
        chats_collection.find({"owner_key": owner_key}, {"_id": 0}).sort("updated_at", -1)
    )


def get_user_chat_list(owner_key: str):
    """(FAST NAVIGATION) The sidebar list WITHOUT the chats' contents — only
    what the templates render (chat_id, title). get_user_chats() returns every
    full chat document (all messages, answers and matched posts), which made
    every page render pull megabytes from MongoDB for a long history."""
    return list(
        chats_collection.find({"owner_key": owner_key}, {"_id": 0, "chat_id": 1, "title": 1, "updated_at": 1})
        .sort("updated_at", -1)
    )


def set_chat_platform(chat_id: str, owner_key: str, display_value: str, unsearched=None):
    """(PLATFORM PERSISTENCE) Remembers the platform choice this chat was last
    used with, so its page reopens with the same selection, and which explicitly
    selected platforms that search did not cover (replaced on every search, so
    it never describes an older one). Best-effort."""
    try:
        if chat_id and display_value:
            chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key},
                                        {"$set": {"selected_platform": display_value,
                                                  "unsearched_platforms": list(unsearched or [])}})
    except Exception as exc:
        log.warning(f"Saving the platform selection failed for chat_id={chat_id}: {exc}")


def set_chat_research_offer(chat_id: str, owner_key: str, offer: dict):
    """(GROUNDED ANSWERS) Remembers the search Flintel just offered to run in
    this chat ({"query", "answer"}), so a bare "yes" right after it runs that
    search. Owner-scoped; best-effort."""
    try:
        if chat_id and isinstance(offer, dict):
            chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key},
                                        {"$set": {"research_offer": offer}})
    except Exception as exc:
        log.warning(f"Saving the research offer failed for chat_id={chat_id}: {exc}")


def chat_exists(chat_id: str, owner_key: str) -> bool:
    """(FAST PATH) Same ownership rule as get_chat_session(), without reading
    the chat's contents."""
    if not chat_id:
        return False
    return chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key}, {"_id": 1}) is not None


def delete_chat_session(chat_id: str, owner_key: str) -> bool:
    """(v4.3) Deletes exactly ONE chat — the one identified by chat_id —
    and only if it belongs to owner_key. Uses the same
    {"chat_id": ..., "owner_key": ...} filter as get_chat_session(), so
    the scoping rule is identical to the one already trusted for reading
    a chat: a guest's anon_id or a signed-in user's email can only ever
    delete chats stamped with that exact owner_key, never another
    owner's chats, and never (by construction, since chat_id is always
    an exact single id) the owner's other chats.

    Returns True if a chat was actually deleted, False if no matching
    chat existed for this owner (already gone, wrong id, or belongs to
    someone else) — callers use this to decide whether to also clear
    `active_chat_id` from the session."""
    if not chat_id:
        return False
    result = chats_collection.delete_one({"chat_id": chat_id, "owner_key": owner_key})
    deleted = result.deleted_count > 0
    if deleted:
        log.info(f"Chat deleted | chat_id={chat_id} | owner_key={owner_key}")
    else:
        log.info(f"Chat delete no-op (not found for this owner) | chat_id={chat_id} | owner_key={owner_key}")
    return deleted


def add_search_to_chat(chat_id: str, owner_key: str, query: str, topic_key: str,
                        keywords: list, targeting_platform: str, time_window_days: int = None,
                        unfiltered: bool = False, website_context: dict = None, match_phrases: list = None,
                        evidence_required: int = None,
                        website_only: bool = False, website_evidence: dict = None,
                        website_note: str = None, resolved_query: str = None, time_range: dict = None,
                        credit_request_id: str = None, late_delivery: tuple = None):
    """Appends a search as a new message in the chat, and auto-titles the
    chat from the very first query if it hasn't been named yet.

    UNCHANGED from v4 (and by the keyword-generation swap) aside from the
    ONE new `time_window_days` parameter (default None, so any future
    caller that doesn't pass it behaves exactly as before this feature) —
    this is only ever called for messages the router classified as
    "search". `keywords` is still stored on the message purely for
    internal use (later matching/debugging) — it doesn't matter, and this
    function doesn't care, whether that list came from Claude's router
    call or the generate_fuzzy_keywords() fallback. `time_window_days` is
    stored the same way, purely so later re-matching of this same message
    (see _fill_in_message_outputs() / the streaming route) can pass it
    back into get_matched_signals() consistently. `results` starts as
    `None` (NOT an empty list — see the RESULTS-RECOMPUTE FIX below) and
    gets filled in later by save_signal_results_to_chat() once matching
    signals show up. `claude_answer` starts empty too and is filled in
    once by save_claude_answer_to_chat().

    `website_context` (NEW, optional, default None — every existing
    caller that doesn't pass it behaves exactly as before) stores the
    structured website breakdown (overview + sections) computed at
    INTEGRATION POINT 2, when this search was derived from a website URL
    the user shared alongside a real ask. None means "no website context
    for this message," exactly like `time_window_days=None` already
    means "no time range" for messages that don't have one.

    (GOOGLE-FALLBACK FEATURE) `google_fallback_triggered` always starts
    False on a new message — flipped to True exactly once, by
    mark_google_fallback_triggered(), the first time
    _fill_in_message_outputs() decides to fire the Google-search
    fallback for this message. Older messages (before this feature)
    simply don't have this field at all; every read of it elsewhere
    uses .get(..., False) so that's indistinguishable from False.

    (SEARCH-PROGRESS UI) `search_progress` and `search_progress_generated`
    follow the exact same pattern — `search_progress` starts None and is
    set exactly once, by save_search_progress_to_chat(), the first time
    _fill_in_message_outputs()/stream_answer() decides to generate the
    query-aware "still searching" status copy (intro/outro/checklist)
    for this message; `search_progress_generated` is the fire-once guard
    for that, mirroring google_fallback_triggered exactly. Older
    messages simply don't have either field, and every read uses
    .get(..., False)/.get(...) so that's indistinguishable from unset.

    (PHRASE-MATCHING FEATURE) `match_phrases` (NEW, optional, default
    None — every existing caller that doesn't pass it behaves exactly as
    before) stores the 4-10-word natural phrases the router (or
    resolve_unclear_topic()/extract_keywords_from_website()) generated
    alongside `keywords`, for get_matched_signals()'s own loose title/
    text phrase check. None means "no match_phrases for this message" —
    get_matched_signals() gracefully falls back to its old keyword-word-
    boundary check in that case, exactly like before this feature.

    (EVIDENCE-BUDGET FEATURE) `evidence_required` (NEW, optional, default
    None — every existing caller that doesn't pass it behaves exactly as
    before) stores the router's own per-query evidence-planner estimate
    (already clamped to [MIN_ANALYSIS_EVIDENCE, MAX_ANALYSIS_EVIDENCE] by
    _parse_router_json()) for how much evidence this particular query
    needs, so later re-matching/re-answering of this same message (see
    _fill_in_message_outputs() / _complete_message_answer_and_results() /
    _timeout_fallback_answer()) can derive its evidence limit from it
    consistently. None means "no evidence budget for this message" — an
    older/non-search message — and every downstream consumer of this
    field falls back to its own pre-existing default exactly as it did
    before this feature existed.

    (WEBSITE INTELLIGENCE) `website_only` (NEW, optional, default False —
    every existing caller that doesn't pass it behaves exactly as
    before) permanently tags this message as one whose answer must be
    built from website evidence (website_intelligence.
    build_website_insight_answer()) instead of Reddit evidence. It is
    persisted on the message, so _fill_in_message_outputs() /
    _complete_message_answer_and_results() never have to re-decide it.
    `website_evidence` (NEW, optional, default None) is the structured
    business-evidence dict ({url, structured_evidence, evidence_quality})
    that answer needs — stored on the message itself (in addition to any
    chat-level cache) so regenerating this message's answer later uses
    the exact evidence originally used, even if a cache has since
    expired or changed.

    (BUG FIX PACK #2) `website_note` (NEW, optional, default None — every
    existing caller that doesn't pass it behaves exactly as before) is a
    short, plain-text classification note (e.g. own-business vs related
    vs unrelated website) that _complete_message_answer_and_results()
    folds into analyze_with_claude()'s extra_context for this message,
    so the answer can account for how the shared website relates to the
    query. Stored on the message so it stays consistent across
    re-answers of the same message, exactly like website_evidence.

    (SEARCH CREDITS) With `credit_request_id` the message carries that id and
    the save is guarded by search_credits.message_save_filter(): it never
    adds a second message for the same id, never writes while an operator
    close marker exists (S-normal), and `late_delivery=(attempt, close_token)`
    only matches a completed close of that exact attempt (S-late). The save
    never creates a chat (no upsert). Returns True only when the message was
    saved (matched == 1); the durable turn record and message counter follow
    only a real save. Without `credit_request_id` everything is exactly as
    before and nothing is returned."""
    now = datetime.now(timezone.utc)
    message = {
        "query":              query,
        "topic_key":          topic_key,
        "keywords":           keywords,
        "match_phrases":      match_phrases,  # (PHRASE-MATCHING FEATURE) list or None
        "targeting_platform": targeting_platform,
        "time_window_days":   time_window_days,  # (NEW) int or None — see get_matched_signals()
        "time_range":         time_range,        # (DATE/TIME) {start, end (UTC, end exclusive), tz, display, ...} or None
        "evidence_required":  evidence_required,  # (EVIDENCE-BUDGET FEATURE) int or None
        "unfiltered":         unfiltered,
        "website_only":       website_only,      # (WEBSITE INTELLIGENCE) bool — True routes answer-generation
                                                  # through the website-insight path, not Reddit evidence.
        "website_evidence":   website_evidence,  # (WEBSITE INTELLIGENCE) dict|None — structured business
                                                  # evidence needed to build the website-insight answer.
        "website_note":       website_note,      # (WEBSITE INTELLIGENCE) str|None — classification note folded
                                                  # into analyze_with_claude()'s extra_context for this message.
        "website_context":    website_context,
        "resolved_query":     resolved_query,    # (MULTI-TURN CONTEXT) str|None — router's standalone
                                                  # restatement of a follow-up message (see logics.py).
        "google_fallback_triggered": False,
        "search_progress": None,               # (SEARCH-PROGRESS UI) None until generated
        "search_progress_generated": False,    # (SEARCH-PROGRESS UI) fire-once guard, mirrors google_fallback_triggered
        "requested_at":       now,
        "request_id":         _usage_request_id(),   # (USAGE) ties every metered call of this search together
        # (RESULTS-RECOMPUTE FIX) `None` here, not `[]` — an empty list is
        # a legitimate, ALREADY-COMPUTED final value (e.g. a "no_results"
        # answer genuinely has zero results). Using None to mean "not
        # computed yet" lets _fill_in_message_outputs() tell "never
        # computed" apart from "computed and genuinely empty," so a
        # message that's truly done never gets its (slow, blocking)
        # signal-matching re-run on every later chat view.
        "results":            None,
        "claude_answer":      None, # filled in once: Claude's answer text, grounded in `results`
    }

    if credit_request_id:
        message["credit_request_id"] = credit_request_id

    chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key})
    update = {"$push": {"messages": message}, "$set": {"updated_at": now}}
    if chat and not chat.get("messages"):
        update["$set"]["title"] = generate_chat_title(query)

    if credit_request_id:
        import search_credits
        result = chats_collection.update_one(
            search_credits.message_save_filter(chat_id, owner_key, credit_request_id, late_delivery), update)
        if result.matched_count != 1:
            return False
        turn_finisher.enqueue(chat_id, owner_key, topic_key, due_in_seconds=turn_finisher.NEW_TURN_DELAY_SECONDS)
        increment_message_count(owner_key)
        return True

    chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key}, update)
    # (NON-BLOCKING CHAT PAGE) Durable record that this turn must be finished
    # even if the browser never streams it and the chat is never reopened.
    # Its stream normally takes the lease long before this delay; best-effort,
    # never raises.
    turn_finisher.enqueue(chat_id, owner_key, topic_key, due_in_seconds=turn_finisher.NEW_TURN_DELAY_SECONDS)
    increment_message_count(owner_key)


def add_chat_message_to_chat(chat_id: str, owner_key: str, query: str, answer: str):
    """(v5) Appends a plain conversational turn — NOT a search — to
    the chat. No topic_key/keywords/targeting_platform, no
    flintel_search_jobs entry, no post-card results, no signal matching
    ever happens for these. This is for messages classify_and_maybe_chat()
    decided were just normal chat (greetings, small talk, general
    questions, follow-ups, the open-ended "what's happening on this
    platform" case, etc.), (v6) a polite decline for a "blocked" message,
    or a clarifying question for a "clarify" message — all three are
    saved with the exact same shape, since all render identically (query
    + answer text, no post cards), so no template changes are needed.

    Completely separate from add_search_to_chat() above, which is
    untouched and still used for every actual search-type message exactly
    as before — the two message shapes coexist in the same `messages`
    array, distinguished by the `message_type` field ("chat" here; absent
    or "search" for ordinary search messages)."""
    now = datetime.now(timezone.utc)
    message = {
        "query":              query,
        "topic_key":          None,
        "keywords":           [],
        "targeting_platform": None,
        "time_window_days":   None,
        "requested_at":       now,
        "results":            [],
        "claude_answer":      answer,
        "message_type":       "chat",
    }

    # (FAST PATH) only needs to know whether the chat has messages yet
    chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key},
                                     {"_id": 1, "messages": {"$slice": 1}})
    update = {"$push": {"messages": message}, "$set": {"updated_at": now}}
    if chat and not chat.get("messages"):
        update["$set"]["title"] = generate_chat_title(query)

    chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key}, update)
    increment_message_count(owner_key)


def _set_on_latest_message(chat_id: str, owner_key: str, topic_key: str, fields: dict):
    """(BUG 3 FIX — REFRESH/DUPLICATE-PROMPT HIDES ANSWER) `topic_key` is
    derived only from the query text, so re-sending the exact same prompt
    in the same chat produces a second message with the SAME topic_key.
    Every save/mark helper below used to update via
    {"messages.topic_key": topic_key} + "messages.$....", and Mongo's
    positional `$` operator always resolves to the FIRST array element
    that matches the filter — so the write always landed on the OLDEST
    message with that topic_key, never the new one. The new message's
    fields stayed None forever (even though the write genuinely succeeded
    against the old message), so it never resolved/rendered.

    This finds the LAST (most recent) message with a matching topic_key
    and updates it by its actual array index instead of by positional
    match, so a repeated prompt's newest occurrence is the one that gets
    its answer/results/flags written. Best-effort: silently no-ops if the
    chat or a matching message can't be found, exactly like the old
    positional-update calls silently no-op'd on no match."""
    chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key}, {"messages.topic_key": 1})
    msgs = (chat or {}).get("messages") or []
    idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("topic_key") == topic_key), None)
    if idx is None:
        return
    update = {f"messages.{idx}.{k}": v for k, v in fields.items()}
    update["updated_at"] = datetime.now(timezone.utc)
    chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key}, {"$set": update})


def save_signal_results_to_chat(chat_id: str, owner_key: str, topic_key: str, results: list):
    """Best-effort: writes the matched title/post_text/post_url/platform
    output onto the SAME chat message that holds the original user prompt
    for this topic, exactly as computed by get_matched_signals() — no
    keyword list, job status, or anything else about the job is written
    here. This is the post-cards data, unchanged from v3 in shape.

    Safe to call repeatedly (e.g. on every chat/home page load while the
    background job is still filling in signals) — it just overwrites
    `results` with the latest matched set for that message.

    (RESULTS-RECOMPUTE FIX) Previously a no-op on ANY falsy `results`
    (including a deliberate, final `[]`) — this silently swallowed the
    BUGFIX PACK #1 gate's own "final results are genuinely empty" write,
    meaning that value never actually reached Mongo, `results` stayed at
    its uncomputed initial value forever, and _fill_in_message_outputs()
    kept re-triggering a full re-match on every later chat view. Now only
    skips the write on `None` (a real "nothing to write" signal) — an
    explicit empty list `[]` is a legitimate final value and gets
    persisted like any other.

    (BUG 3 FIX) Now targets the most recent message with this topic_key
    via _set_on_latest_message() instead of Mongo's positional `$`
    operator — see that helper's docstring for why."""
    if results is None:
        return
    _set_on_latest_message(chat_id, owner_key, topic_key, {"results": results})


def save_last_website_context_to_chat(chat_id: str, owner_key: str, context: dict):
    """(WEBSITE INTELLIGENCE — POINT 3) Chat ke top-level document par
    (kisi specific message par nahi) is chat mein SABSE RECENT website
    analysis ka context store karta hai: {url, topic_key, keywords,
    match_phrases}. Isi ki wajah se agar user baad mein ek vague follow-
    up bole ("reddit ke baare mein batao"), routes.py isi stored
    topic_key/keywords ko REUSE kar sakta hai — bina dobara Claude se
    naye keywords generate kiye, aur is tarah guarantee milta hai ke
    wahi flintel_signals/google_posts data mile jo pehle se collect ho
    chuka tha.

    Chat-level (message-level nahi) is liye, kyunke follow-up message ka
    apna koi topic_key nahi hota jab tak router decide na kare. Sirf
    SABSE RECENT context store hota hai (har baar overwrite).

    Best-effort, never raises past itself."""
    if not chat_id or not owner_key or not context:
        return
    try:
        chats_collection.update_one(
            {"chat_id": chat_id, "owner_key": owner_key},
            {"$set": {
                "last_website_context": context,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
    except Exception as exc:
        log.warning(f"Saving last_website_context failed for chat_id={chat_id}: {exc}")


def save_claude_answer_to_chat(chat_id: str, owner_key: str, topic_key: str, answer: str):
    """(v4) Writes ONLY Claude's final answer text onto the same chat
    message — never the posts that were sent in as input, since those
    already live in flintel_signals and don't need duplicating here. This
    is what makes re-opening a chat later show the exact same answer
    again, as-is, with no need to re-call Claude. (v6: also used to cache
    the plain "nothing found yet" timeout answer — same function, same
    caching behavior.)

    UNCHANGED: this still stores whatever string `answer` is, verbatim,
    with no parsing/validation.

    (BUG 3 FIX) Now targets the most recent message with this topic_key
    via _set_on_latest_message() instead of Mongo's positional `$`
    operator — see that helper's docstring for why."""
    if not answer:
        return
    _set_on_latest_message(chat_id, owner_key, topic_key, {"claude_answer": answer})
    _mark_answered_at(chat_id, owner_key, topic_key)


def _mark_answered_at(chat_id: str, owner_key: str, topic_key: str):
    """(RESEARCH STATUS) Stamps when this message's answer was FIRST saved, so a
    reopened chat can show "Researched 14s" (answered_at - requested_at). Only
    ever set once — a later re-save of the same answer never moves it.
    Best-effort, never raises: the answer itself is already saved above."""
    try:
        chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key},
                                         {"messages.topic_key": 1, "messages.answered_at": 1})
        msgs = (chat or {}).get("messages") or []
        idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("topic_key") == topic_key), None)
        if idx is None or msgs[idx].get("answered_at"):
            return
        chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key},
                                    {"$set": {f"messages.{idx}.answered_at": datetime.now(timezone.utc)}})
    except Exception as exc:
        log.warning(f"Saving answered_at failed for topic_key={topic_key}: {exc}")


def save_message_feedback(chat_id: str, owner_key: str, msg_index: int, feedback: str):
    """Store 'up' or 'down' feedback on a specific message by its array index.

    Uses the index directly (not topic_key) so repeated queries with the
    same topic_key never misdirect the write. Idempotent: if feedback is
    already set on that message it is overwritten (allows changing mind).
    Best-effort — never raises past itself."""
    if feedback not in ("up", "down"):
        return
    try:
        chats_collection.update_one(
            {"chat_id": chat_id, "owner_key": owner_key},
            {"$set": {
                f"messages.{msg_index}.msg_feedback": feedback,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
    except Exception as exc:
        log.warning(f"Saving feedback failed for chat_id={chat_id} idx={msg_index}: {exc}")


def mark_google_fallback_triggered(chat_id: str, owner_key: str, topic_key: str):
    """(GOOGLE-FALLBACK FEATURE) Same targeted-update pattern as
    save_claude_answer_to_chat()/save_signal_results_to_chat() — flips
    this one message's google_fallback_triggered field to True so
    should_trigger_google_fallback() never fires the Google-search
    fallback more than once for the same message, even if it found zero
    Reddit results.

    Best-effort, never raises past itself: any failure here is logged
    and swallowed rather than breaking whatever background task called
    this.

    (BUG 3 FIX) Now targets the most recent message with this topic_key
    via _set_on_latest_message() instead of Mongo's positional `$`
    operator — see that helper's docstring for why."""
    try:
        _set_on_latest_message(chat_id, owner_key, topic_key, {"google_fallback_triggered": True})
    except Exception as exc:
        log.warning(f"Marking google_fallback_triggered failed for topic_key={topic_key}: {exc}")


def mark_search_progress_generated(chat_id: str, owner_key: str, topic_key: str):
    """(SEARCH-PROGRESS UI) Same fire-once guard pattern as
    mark_google_fallback_triggered() — flips this one message's
    search_progress_generated field to True so the generation trigger
    never fires more than once for the same message, even if
    generate_search_progress_content() itself failed and produced
    nothing to save. Best-effort, never raises past itself.

    (BUG 3 FIX) Now targets the most recent message with this topic_key
    via _set_on_latest_message() instead of Mongo's positional `$`
    operator — see that helper's docstring for why."""
    try:
        _set_on_latest_message(chat_id, owner_key, topic_key, {"search_progress_generated": True})
    except Exception as exc:
        log.warning(f"Marking search_progress_generated failed for topic_key={topic_key}: {exc}")


def save_search_progress_to_chat(chat_id: str, owner_key: str, topic_key: str, progress_content: dict):
    """(SEARCH-PROGRESS UI) Same targeted-update pattern as
    save_claude_answer_to_chat()/save_signal_results_to_chat() — stores
    the generated {"intro", "outro", "checklist"} dict (see
    flintel.generate_search_progress_content()) on this one message, so
    the frontend can render the richer in-progress UI instead of the
    plain spinner. Best-effort, never raises past itself: a failure here
    just means this message keeps showing the plain spinner state,
    exactly like before this feature existed.

    (BUG 3 FIX) Now targets the most recent message with this topic_key
    via _set_on_latest_message() instead of Mongo's positional `$`
    operator — see that helper's docstring for why."""
    if not progress_content:
        return
    try:
        _set_on_latest_message(chat_id, owner_key, topic_key, {"search_progress": progress_content})
    except Exception as exc:
        log.warning(f"Saving search_progress failed for topic_key={topic_key}: {exc}")


def migrate_anon_chats_to_owner(anon_id: str, new_owner_key: str):
    """When a guest signs up or logs in, re-key their guest chat history
    onto their account so it isn't lost — same pattern Claude/ChatGPT use
    to carry a guest conversation over after sign-in."""
    if not anon_id or anon_id == new_owner_key:
        return
    result = chats_collection.update_many(
        {"owner_key": anon_id, "owner_type": "anon"},
        {"$set": {"owner_key": new_owner_key, "owner_type": "email"}},
    )
    if result.modified_count:
        log.info(
            f"Migrated {result.modified_count} guest chat(s) | "
            f"anon_id={anon_id} -> owner_key={new_owner_key}"
        )


def _is_owner_busy(owner_key: str) -> bool:
    """(PER-USER BUSY LOCK) True only if THIS owner_key currently has an
    in-flight Claude call that hasn't finished yet, and hasn't gone stale.
    Never checks or affects any other owner_key — this is purely a
    per-user concern, exactly like the feature spec requires."""
    if not owner_key:
        return False
    doc = busy_owners_collection.find_one({"owner_key": owner_key})
    if not doc:
        return False
    if _elapsed_seconds(doc.get("started_at")) >= BUSY_FLAG_TIMEOUT_SECONDS:
        # Stale (a crash or restart left this behind) — clear it
        # defensively right now and treat this request as not busy,
        # rather than leaving the user locked out forever.
        busy_owners_collection.delete_one({"owner_key": owner_key})
        return False
    return True


def _set_owner_busy(owner_key: str):
    """(PER-USER BUSY LOCK) Marks THIS owner_key busy — called right
    before a blocking analyze_with_claude() call begins for a user-facing
    action. upsert=True so this is safe to call even if a stale doc
    somehow already exists for this owner_key."""
    if not owner_key:
        return
    try:
        busy_owners_collection.update_one(
            {"owner_key": owner_key},
            {"$set": {"owner_key": owner_key, "started_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception as exc:
        log.warning(f"Setting busy flag failed for owner_key={owner_key}: {exc}")


def _clear_owner_busy(owner_key: str):
    """(PER-USER BUSY LOCK) Clears THIS owner_key's busy flag — called
    once the in-flight Claude call finishes, success or failure, so the
    user's very next request goes through normally right away."""
    if not owner_key:
        return
    try:
        busy_owners_collection.delete_one({"owner_key": owner_key})
    except Exception as exc:
        log.warning(f"Clearing busy flag failed for owner_key={owner_key}: {exc}")


def _trigger_google_fallback_search(chat_id: str, owner_key: str, msg: dict):
    """(GOOGLE-FALLBACK FEATURE) Fires the Google-search-for-Reddit-posts
    fallback for a message that still has no flintel_signals match after
    GOOGLE_FALLBACK_TRIGGER_SECONDS — see
    flintel.should_trigger_google_fallback() for the timing decision this
    is called in response to.

    (RACE FIX) google_fallback_triggered is now marked SYNCHRONOUSLY by
    the caller (_fill_in_message_outputs()), BEFORE this function is
    scheduled/called — not here anymore — so a second concurrent request
    for the same message can never see the flag still False and schedule
    a duplicate search. This function's only job now is to actually run
    the search and store whatever stubs it finds.

    Wrapped in try/except, never raises — a failure here just means this
    message doesn't get any Google-sourced stub links, exactly like
    before this feature existed."""
    try:
        with _usage_attribution(chat_id=chat_id, owner_key=owner_key, topic_key=msg.get("topic_key"),
                                request_id=msg.get("request_id")):
            google_search.search_google_for_reddit_posts(
                msg.get("keywords", []),
                google_posts_collection,
                search_keyword_for_storage=msg.get("topic_key"),
            )
    except Exception as exc:
        log.warning(f"Google-fallback search failed for topic_key={msg.get('topic_key')}: {exc}")


# (SEARCH-PROGRESS UI — MANDATORY FALLBACK, FIX 2) Static, non-Claude
# search_progress content in the exact same {intro, outro, checklist}
# shape the frontend already renders (see chat.html/index.html's
# renderSearchProgressBlock() and the Jinja msg.search_progress branch —
# neither one cares whether the content came from Claude or from here).
# Used ONLY when flintel.generate_search_progress_content() itself fails
# or returns nothing, so a message sitting in the RESPONSE_TIMEOUT/
# zero-post wait window is GUARANTEED to show a "still working" progress
# UI instead of silently falling back to the plain spinner. Deliberately
# generic/query-agnostic, since it only ever fires when the query-aware
# Claude generation couldn't run.
_SEARCH_PROGRESS_FALLBACK_CONTENT = {
    "intro": "Still searching across Reddit and the web for this topic.",
    "outro": "Hang tight — results will appear here as soon as they're found.",
    "checklist": [
        "Scanning recent posts and discussions",
        "Cross-checking matches against your topic",
        "Preparing a grounded answer",
    ],
}


def _generate_search_progress(chat_id: str, owner_key: str, msg: dict):
    """(SEARCH-PROGRESS UI) Fires the query-aware "still searching"
    status-copy generation for a message that's been running long
    enough that the plain spinner alone starts to feel uninformative —
    fired at the same GOOGLE_FALLBACK_TRIGGER_SECONDS mark as the
    Google-search fallback (a separate, independent action; this never
    depends on that fallback's own outcome).

    (RACE FIX) search_progress_generated is marked SYNCHRONOUSLY by the
    caller (_fill_in_message_outputs()/routes.py's stream_answer()),
    BEFORE this function is scheduled/called — not here — mirroring the
    exact same fix already applied to
    mark_google_fallback_triggered()/_trigger_google_fallback_search(),
    so a second concurrent request for the same message can never see
    the flag still False and schedule a duplicate generation call. This
    function's only job is to actually generate and save the content.

    (MANDATORY FALLBACK, FIX 2) A failure here — the Claude call errors,
    or returns no usable content — no longer leaves this message showing
    the plain spinner. It now falls back to
    _SEARCH_PROGRESS_FALLBACK_CONTENT, a static, always-available
    progress UI in the same {intro, outro, checklist} shape, so the
    "still working" UI is GUARANTEED during the RESPONSE_TIMEOUT/
    zero-post wait — not merely best-effort. save_search_progress_to_chat()
    itself is still the thing that actually persists it, and it already
    degrades gracefully (own try/except) on any storage failure."""
    try:
        with _usage_attribution(chat_id=chat_id, owner_key=owner_key, topic_key=msg.get("topic_key"),
                                    request_id=msg.get("request_id")):
            progress_content = flintel.generate_search_progress_content(
                msg.get("query"),
                msg.get("keywords", []),
                msg.get("targeting_platform"),
                _call_claude,
            )
    except Exception as exc:
        log.warning(f"Search-progress generation failed for topic_key={msg.get('topic_key')}: {exc}")
        progress_content = None

    save_search_progress_to_chat(
        chat_id, owner_key, msg["topic_key"],
        progress_content or _SEARCH_PROGRESS_FALLBACK_CONTENT,
    )


def save_claude_answer_if_unset(chat_id: str, owner_key: str, topic_key: str, answer: str) -> bool:
    """(GUEST GATE) Compare-and-set form of save_claude_answer_to_chat(): stores the answer on the newest message with
    this topic_key ONLY IF that message has no answer yet, in one conditional update. The first writer wins; a runner
    that lost (a stale worker, a second tab) gets False and must not show anything of its own - it replays what was
    stored. Returns True when this call stored the answer."""
    if not answer:
        return False
    chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key}, {"messages.topic_key": 1})
    msgs = (chat or {}).get("messages") or []
    idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("topic_key") == topic_key), None)
    if idx is None:
        return False
    field = f"messages.{idx}.claude_answer"
    result = chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key, field: None},
                                         {"$set": {field: answer, "updated_at": datetime.now(timezone.utc)}})
    matched = getattr(result, "matched_count", None)
    won = True if matched is None else matched == 1      # (a bare test double returns no result object)
    if won:
        _mark_answered_at(chat_id, owner_key, topic_key)
    return won


def save_answer_for_gate(chat_id: str, owner_key: str, topic_key: str, answer: str, gate_key, token) -> bool:
    """(GUEST GATE) Stores a gated guest search's answer with compare-and-set semantics (see
    save_claude_answer_if_unset); every other owner - registered users, gate off, no token - keeps the plain,
    unconditional save exactly as before. Returns True when this call's answer is the stored one."""
    if token and guest_gate.is_guest_owner(gate_key) and guest_gate.default_gate() is not None:
        return save_claude_answer_if_unset(chat_id, owner_key, topic_key, answer)
    save_claude_answer_to_chat(chat_id, owner_key, topic_key, answer)
    return True


def _abandon_guest_message(chat_id: str, owner_key: str, msg: dict, gate_key=None):
    """(GUEST GATE) A search that no longer owns its reservation (another search of the same visitor is admitted, or the
    allowance is used) produces nothing: the explicit "replaced by a newer search" notice is saved so the turn is
    finished for the worker and the page, and no post cards are attached. Best effort, never raises."""
    note = guest_gate.superseded_answer()
    msg["claude_answer"] = note
    msg["results"] = []
    try:
        # compare-and-set: the notice can never overwrite an answer another runner already stored
        if not save_answer_for_gate(chat_id, owner_key, msg["topic_key"], note, gate_key or owner_key, msg.get("request_id")):
            return
        save_signal_results_to_chat(chat_id, owner_key, msg["topic_key"], [])
    except Exception as exc:
        log.warning(f"Saving the superseded-search notice failed for topic_key={msg.get('topic_key')}: {exc}")


def _complete_message_answer_and_results(chat_id: str, owner_key: str, msg: dict, matched: list,
                                         freshness_prov: dict = None):
    """(PERFORMANCE FIX) Extracted, UNCHANGED logic — this is exactly what
    _fill_in_message_outputs() already did inline for the "needs_answer" +
    "needs_results" case, just pulled into its own function so it can be
    run either synchronously (existing behavior, when no background_tasks
    is available) or scheduled via BackgroundTasks (new: runs AFTER the
    response is already sent, so a slow Claude call never blocks the
    request). Not a single line of the actual logic below changed — same
    calls, same order, same caching, same BUGFIX PACK #1 results-gating.

    (BUSY-LOCK RACE FIX) _set_owner_busy() is NOT called here anymore —
    it's now set synchronously in _fill_in_message_outputs(), before this
    function is even scheduled, so the flag is already in place before
    the response goes out. This function only clears it, in the finally
    below, once the work actually finishes.

    (BUG FIX — DON'T HIDE REAL MATCHED POSTS) The results-gating decision
    now goes through _finalize_answer_and_results() instead of the old
    inline `[] if claude_format in _NO_DATA_CLAUDE_FORMATS else matched`
    — real matched posts are no longer discarded just because Claude's
    own written analysis picked a "no_data" format; see that function's
    own docstring for the full reasoning.

    (EVIDENCE-BUDGET FEATURE) The ONLY change in this function: right
    before merge_matched_and_google_results() is called, an
    `effective_evidence_limit` is resolved from this message's own stored
    `evidence_required` (falling back to MIN_ANALYSIS_EVIDENCE when the
    message has none — an older/non-search message — and always clamped
    to MAX_ANALYSIS_EVIDENCE), and passed through as that call's
    `max_total`. No other line of this function's logic/order changed.

    (WEBSITE INTELLIGENCE — WEBSITE-ONLY BRANCH) If `msg["website_only"]`
    is set, the answer is NOT built by the normal Reddit-evidence
    analyze_with_claude() path — it comes from
    website_intelligence.build_website_insight_answer(), grounded only on
    the website evidence stored on the message. The message's `results`
    (Reddit post-cards) are always forced to [] for these. This branch
    returns early, so none of the normal-path logic below runs for such a
    message. Background search-job/Google-fallback machinery is
    unaffected (it runs elsewhere, before this function) so
    flintel_signals/google_posts keep filling in for later follow-ups.

    (BUG FIX PACK #2) In the normal branch, this message's own
    `website_note` (if any) is folded into `extra_ctx_parts` right before
    they're joined into `extra_context`, and the normal branch's
    append_to_chat_summary() call now also passes this message's
    `keywords` — see both docstrings above for why. The website-only
    branch's own append_to_chat_summary() call is unchanged (no keywords,
    since that branch never used Reddit-evidence keywords to begin
    with)."""
    # (RESULTS-RECOMPUTE FIX, applied here too for the same reason) `is
    # None`, not falsy — closes a low-probability but real analogous gap:
    # if a Claude API call ever technically "succeeds" but returns zero
    # text content, analyze_with_claude() would return "" (empty string,
    # falsy), which `not msg.get(...)` would wrongly treat as "still
    # needs answering" forever, re-triggering Claude on every later view
    # exactly like the results-list bug did. claude_answer already
    # initializes to None (never "") at message creation, so this check
    # is the only piece that needed to change.
    needs_answer = msg.get("claude_answer") is None
    answer_for_format_check = msg.get("claude_answer")

    if needs_answer:
        gate_key = guest_gate.gate_owner(owner_key, msg)    # (GUEST GATE) the identity the gate knows this search by
        gate_token = msg.get("request_id")
        delivered = False
        gate_lease = {}
        try:
            # (GUEST GATE) ask the gate BEFORE any model call. A background retry of a search whose reservation was
            # released, or a superseded worker, may not start a second independent search while another one of this
            # visitor is admitted: it produces nothing (and the turn is finished with the superseded notice).
            verdict = guest_gate.on_work_start(gate_key, gate_token, msg.get("topic_key"), chat_id, gate_lease)
            if verdict != guest_gate.PROCEED:
                if verdict == guest_gate.ABANDON:
                    _abandon_guest_message(chat_id, owner_key, msg, gate_key)
                return              # WAIT: another runner is generating this very answer; DEFER: stays pending
            # (WEBSITE INTELLIGENCE — WEBSITE-ONLY BRANCH) Point 1: agar yeh
            # message ek bare-URL (ya website-focused) search hai, iska
            # answer NORMAL Reddit-evidence analyze_with_claude() se nahi
            # aata — website_intelligence.build_website_insight_answer() se
            # aata hai, jo sirf website evidence ke upar grounded hai. Search
            # job/Google-fallback/keyword-matching baqi sab NORMAL chalta
            # rehta hai (background mein flintel_signals collect hoti rehti
            # hai future follow-ups ke liye — Point 3) — sirf THIS message
            # ka claude_answer alag tareeke se banta hai, aur iske "results"
            # (Reddit post-cards) hamesha khaali rakhe jate hain.
            #
            # IMPORTANT: yeh check try: ke andar SABSE PEHLE hai, aur return
            # ke sath khatam hota hai taake baqi purana logic (Google-stub
            # merge, extra-context building, analyze_with_claude()) is
            # message ke liye bilkul na chale.
            if msg.get("website_only"):
                from website_intelligence import build_website_insight_answer
                website_evidence = msg.get("website_evidence") or {}
                if not guest_gate.on_work_renew(gate_key, gate_token, gate_lease, force=True):
                    return          # (GUEST GATE) this runner was taken over: no model call, nothing saved or shown
                answer = build_website_insight_answer(
                    url=website_evidence.get("url") or msg.get("query"),
                    query=msg.get("query"),
                    structured_evidence=website_evidence.get("structured_evidence"),
                    evidence_quality=website_evidence.get("evidence_quality", "thin"),
                    call_claude_fn=_call_claude,
                )
                # (GUEST GATE) counting is the commit: only the reservation's owner may save / show the answer
                verdict = guest_gate.on_answer_ready(gate_key, chat_id, msg["topic_key"], gate_token, answer, lease=gate_lease)
                if verdict != guest_gate.DELIVER:
                    if verdict == guest_gate.DISCARD:
                        _abandon_guest_message(chat_id, owner_key, msg, gate_key)
                    return
                if not save_answer_for_gate(chat_id, owner_key, msg["topic_key"], answer, gate_key, gate_token):
                    return          # (GUEST GATE) another runner's answer is already stored: this one is dropped
                msg["claude_answer"] = answer
                delivered = True
                # Reddit post-cards is message ke neeche kabhi nahi dikhne —
                # results forced empty, regardless of matched.
                msg["results"] = []
                save_signal_results_to_chat(chat_id, owner_key, msg["topic_key"], [])
                try:
                    append_to_chat_summary(chat_id, owner_key, msg["query"], answer)
                except Exception as exc:
                    log.warning(f"Updating chat summary failed for topic_key={msg.get('topic_key')}: {exc}")
                # (busy flag ka clear neeche wale `finally` se hota hai —
                # return par bhi woh chalta hai, alag se call ki zaroorat nahi.)
                return  # website-only path complete — skip the normal branch below entirely.

            # ── EXISTING NORMAL BRANCH (unchanged) starts here ──
            # (MERGE BEFORE ANSWERING) Pulls in Google-search stub
            # results and combines them with the flintel_signals
            # `matched` list passed in, capped at the message's own
            # evidence budget (see effective_evidence_limit below) —
            # Claude now gets ONE merged pool from both sources in a
            # single call, instead of Google only ever being a
            # last-resort replacement used when signals were empty.
            try:
                # STRICT: Google stubs never pad the count — don't fetch them.
                stub_docs = ([] if strict_intent_mode() else
                             google_search.get_stub_results_for_keywords(
                                 google_posts_collection, msg.get("keywords", [])))
            except Exception as exc:
                log.warning(f"Fetching Google-fallback stubs failed for topic_key={msg.get('topic_key')}: {exc}")
                stub_docs = []
            google_results = flintel.format_google_stub_results(stub_docs)
            # (EVIDENCE-BUDGET FEATURE) Resolved from this message's own
            # stored evidence_required (already clamped at parse time in
            # _parse_router_json()), falling back to MIN_ANALYSIS_EVIDENCE
            # for an older/non-search message that never had one, and
            # always re-clamped to MAX_ANALYSIS_EVIDENCE as a final safety
            # ceiling.
            effective_evidence_limit = min(
                msg.get("evidence_required") or MIN_ANALYSIS_EVIDENCE,
                MAX_ANALYSIS_EVIDENCE,
            )
            merged_pool = flintel.merge_matched_and_google_results(
                matched, google_results, max_total=effective_evidence_limit,
                **({"fill_with_stubs": False} if strict_intent_mode() else {})
            )

            extra_ctx_parts = []
            # (MIXED EVIDENCE HANDLING) Always added — a short, general
            # reminder to explicitly write out both sides when the
            # evidence is genuinely mixed/conflicting. Harmless when the
            # evidence isn't mixed; Claude simply has no reason to act on
            # it in that case.
            extra_ctx_parts.append(flintel.build_mixed_evidence_note())
            if msg.get("unfiltered"):
                extra_ctx_parts.append(
                    flintel.build_unfiltered_answer_context(
                        msg["query"], msg.get("time_window_days")
                    )
                )
            # (MERGE BEFORE ANSWERING) Tells Claude it has a mix of
            # grounded signals and discovery-only Google posts in the
            # same batch — never removed alongside whatever other
            # context strings are already being appended here.
            if google_results:
                extra_ctx_parts.append(
                    flintel.build_combined_source_context(len(matched), len(google_results))
                )
            # (BUG FIX — DON'T RE-SUGGEST A DECLINED ALTERNATIVE) Pull this
            # chat's own rolling summary and hand it to analyze_with_claude()
            # as extra context, so Claude can see whether the user already
            # explicitly declined/narrowed away from an earlier suggested
            # alternative — see CLAUDE_ANALYSIS_SYSTEM_PROMPT's own
            # "CONVERSATION CONTINUITY" instruction for what it does with
            # this. Best-effort: a lookup failure here just means this
            # answer is generated without that continuity context, exactly
            # like before this fix — it never blocks or breaks the answer.
            try:
                existing_chat = get_chat_session(chat_id, owner_key)
                chat_summary_for_answer = (existing_chat or {}).get("summary") or ""
            except Exception as exc:
                log.warning(f"Chat summary lookup failed for topic_key={msg.get('topic_key')}: {exc}")
                chat_summary_for_answer = ""
            if chat_summary_for_answer:
                extra_ctx_parts.append(
                    "Conversation so far (auto-summarized, may be empty) — see "
                    "the CONVERSATION CONTINUITY instruction above for how to "
                    "use this:\n" + chat_summary_for_answer
                )
            # (BUG FIX PACK #2 — WEBSITE-NOTE CONTEXT) This message's own
            # website-classification note (own-business/related/unrelated),
            # if any, is folded in last so analyze_with_claude() can
            # account for how the shared website relates to the query.
            if msg.get("website_note"):
                extra_ctx_parts.append(msg["website_note"])
            _rq_note = _resolved_request_note(msg.get("query"), msg.get("resolved_query"))   # (MULTI-TURN CONTEXT)
            if _rq_note:
                extra_ctx_parts.append(_rq_note)
            extra_ctx = "\n\n".join(extra_ctx_parts) if extra_ctx_parts else None
            if not guest_gate.on_work_renew(gate_key, gate_token, gate_lease, force=True):
                return              # (GUEST GATE) this runner was taken over: no model call, nothing saved or shown
            with _usage_attribution(chat_id=chat_id, owner_key=owner_key, topic_key=msg.get("topic_key"),
                                    request_id=msg.get("request_id")):
                answer = analyze_with_claude(msg["query"], merged_pool, extra_context=extra_ctx)
            with time_scope_context(build_time_scope(msg.get("time_window_days"), msg.get("time_range"),
                                                     msg.get("requested_at"),
                                                     targeting_platform=msg.get("targeting_platform", "all"))):   # (RECENCY) the message's own window
                answer = _patch_post_urls_into_answer(answer, merged_pool, query=msg.get("query"))
            if msg.get("website_context"):
                answer = _inject_website_context_into_answer(answer, msg["website_context"])
            answer = enrich_no_results_answer(answer, msg.get("query"), msg.get("targeting_platform"),   # (NO-RESULTS UI)
                                              msg.get("time_range"), msg.get("time_window_days"), msg.get("keywords"))
            answer = enrich_freshness_answer(   # (FRESHNESS) stored-date / search-activity statement; no-op when off
                answer, merged_pool, time_range=msg.get("time_range"), time_window_days=msg.get("time_window_days"),
                provenance=freshness_prov, matched_count=len(matched), evidence_limit=effective_evidence_limit)
            # (GUEST GATE) counting is the commit: only the reservation's owner may save / show the answer
            verdict = guest_gate.on_answer_ready(gate_key, chat_id, msg["topic_key"], gate_token, answer, lease=gate_lease)
            if verdict != guest_gate.DELIVER:
                if verdict == guest_gate.DISCARD:
                    _abandon_guest_message(chat_id, owner_key, msg, gate_key)
                return
            if not save_answer_for_gate(chat_id, owner_key, msg["topic_key"], answer, gate_key, gate_token):
                return              # (GUEST GATE) another runner's answer is already stored: this one is dropped
            msg["claude_answer"] = answer
            delivered = True
            answer_for_format_check = answer
            try:
                append_to_chat_summary(chat_id, owner_key, msg["query"], answer, keywords=msg.get("keywords"),
                                       resolved_query=msg.get("resolved_query"))
            except Exception as exc:
                log.warning(f"Updating chat summary failed for topic_key={msg.get('topic_key')}: {exc}")
        except Exception as exc:
            log.warning(f"Claude analysis failed for topic_key={msg.get('topic_key')}: {exc}")
            if not delivered:
                # (GUEST GATE) nothing was delivered: take back a count whose answer could not be stored, and free the slot
                guest_gate.on_delivery_failed(gate_key, gate_token)
                guest_gate.on_search_incomplete(gate_key, gate_token)
        finally:
            guest_gate.on_work_end(gate_key, gate_token, gate_lease)     # give the work lease back (any exit)
            _clear_owner_busy(owner_key)

    # (RESULTS-RECOMPUTE FIX) `is None`, not falsy — a genuine, already-
    # saved empty list must not be treated as "still needs computing" and
    # get needlessly recomputed/overwritten here either.
    if msg.get("results") is None:
        final_answer, results_to_save = _finalize_answer_and_results(
            answer_for_format_check, matched, seed=msg.get("query", "")
        )
        if final_answer != answer_for_format_check:
            msg["claude_answer"] = final_answer
            try:
                save_claude_answer_to_chat(chat_id, owner_key, msg["topic_key"], final_answer)
            except Exception as exc:
                log.warning(f"Saving closing-note-patched answer failed for topic_key={msg.get('topic_key')}: {exc}")
        msg["results"] = results_to_save
        try:
            save_signal_results_to_chat(chat_id, owner_key, msg["topic_key"], results_to_save)
        except Exception as exc:
            log.warning(f"Saving matched results to chat failed for topic_key={msg.get('topic_key')}: {exc}")


def _fill_in_message_outputs(chat_id: str, owner_key: str, messages: list, skip_topic_key: str = None,
                              background_tasks: BackgroundTasks = None):
    """Shared by home() and view_chat(): for any SEARCH-type message in
    `messages` that's still missing its post-card `results` and/or its
    `claude_answer`, looks up matching signals ONCE and uses that single
    lookup for both:
      - post cards keep working exactly like v3 (results saved as-is,
        subject to the BUGFIX PACK #1 gate described below), and
      - Claude only gets called (and only gets billed) the first time real
        matched posts are actually available for that message, then the
        answer is cached forever after (only the answer, not the posts).
    Best-effort per message — one message failing must never block the
    rest of the page, and Claude failures must never affect post cards.

    (PERFORMANCE FIX) New optional `background_tasks` parameter (default
    None, fully backward-compatible — any existing/other caller that
    doesn't pass it gets the EXACT original synchronous behavior, nothing
    changes for it). When provided, the "generate answer via Claude, then
    save results" step for a message that needs a fresh answer is
    scheduled via `background_tasks.add_task(...)` instead of being run
    inline — the actual logic is byte-for-byte identical either way (see
    `_complete_message_answer_and_results()`), only WHEN it runs changes:
    after the response has already been sent, instead of blocking it.
    This is what makes opening/switching to a chat fast even when one of
    its messages still needs a fresh Claude call — that message simply
    shows its existing "still gathering results" loading state for this
    one page load, exactly like a genuinely brand-new message already
    does, and resolves on the next load once the background task finishes
    and caches it — rather than the whole request blocking on a live
    Claude call that can take several seconds.

    (v5) Messages with no topic_key are plain chat-type turns — skipped
    here immediately, since there is nothing to fill in for them.

    (v6) If a search-type message STILL has no matched posts, once
    RESPONSE_TIMEOUT seconds have passed since the message's own
    `requested_at`, this calls analyze_with_claude(query, []) exactly
    once — reusing that function's existing "no posts yet, say so
    plainly" branch unchanged — so the user gets a natural "sorry,
    nothing found on this yet" answer instead of a permanently blank
    turn. That answer is cached the same way every other answer in this
    file is, so it's generated (and billed) once.

    (BUGFIX PACK #1 — RESULTS/ANSWER SYNC): once the answer for this
    message is known (freshly generated this call, or already cached),
    its "format" is checked via _extract_claude_format(). If that format
    is one of _NO_DATA_CLAUDE_FORMATS ("no_results", "not_available",
    "disallowed"), the post-card `results` saved/rendered for this
    message are forced to an empty list instead of whatever
    get_matched_signals() loosely matched.

    (POST_URL FIX) Right after a fresh (non-streaming) answer is
    generated here, it is passed through _patch_post_urls_into_answer()
    before being cached/checked for format.

    (STREAMING WIRING FIX) `skip_topic_key` (default None) lets a caller
    reserve exactly ONE search-type message so this function leaves it
    completely untouched — used by view_chat() so the new
    `GET /chat/{chat_id}/stream` route, not this function, is what
    produces that one message's first answer.

    (TIME-WINDOW FEATURE) The get_matched_signals() call also passes
    `since_days=msg.get("time_window_days")` — for every message that has
    no `time_window_days` stored on it (every message from before this
    feature, and every new message where the user gave no time range),
    this is None and behaves exactly as before. For a message that DOES
    have a stored time window, matching now also respects it, exactly the
    same way on every re-render of the same message (so a chat reopened
    later still shows results scoped to the same window it was originally
    asked for).

    (EVIDENCE-BUDGET FEATURE) The ONLY other change in this function: the
    get_matched_signals() call now also passes
    `limit=msg.get("evidence_required")` — for every message that has no
    `evidence_required` stored on it (every message from before this
    feature, and every new non-search-derived message), this is None and
    get_matched_signals() falls back to its own MAX_MATCHED_RESULTS
    default exactly as before this feature — 100% backward compatible.
    For a message that DOES have a stored evidence budget, matching now
    retrieves up to that many posts instead of the static default.

    (DUPLICATE-REFRESH FIX) Both places in this function that used to
    unconditionally call `_set_owner_busy()` and then schedule/run a
    Claude-calling function now first check `_is_owner_busy(owner_key)`.
    If this owner already has an in-flight Claude call (from an earlier
    pass over this same message — e.g. a refresh/poll that landed here
    while the original call for this message, or another message from
    the same owner, was still running), this simply skips scheduling a
    SECOND one and moves on. The message's `claude_answer` stays `None`
    until the in-flight call finishes and saves it (or, if that call
    errors out and clears the busy flag without saving anything, the
    VERY NEXT call to this function will see the owner is no longer busy
    and try again normally). This can only ever skip a redundant call —
    it never blocks or delays the original one, and it never changes
    what gets computed once a call is actually allowed to run. See each
    call site below for the specific rationale.

    (WEBSITE INTELLIGENCE — WEBSITE-ONLY SHORT-CIRCUIT) A message tagged
    `website_only=True` does NOT wait for Reddit/Google evidence to
    appear. Its answer comes purely from website evidence, so gating it
    on `if not merged_pool` (and the RESPONSE_TIMEOUT fallback that
    follows) would either stall it or replace it with a generic
    "nothing found" answer. So, right AFTER the signal lookup and the two
    immediate-trigger blocks (Google search + search-progress — both
    still fire, so flintel_signals/google_posts keep getting collected in
    the background for later follow-ups), such a message is handed
    straight to _complete_message_answer_and_results(), whose own
    website_only branch builds the answer and forces results to [].

    (BUG FIX PACK #2) Both places below that schedule/run
    _timeout_fallback_answer() now also pass this message's
    `website_note` as its new final argument, so a message stuck on the
    RESPONSE_TIMEOUT tier-3 fallback still gets its website-classification
    context folded into that answer too — see
    _complete_message_answer_and_results()'s own docstring for the
    normal-path equivalent.

    (SIGNALS_COLLECTION_2 CONFIRMATION) The get_evidence_with_topup()
    call below passes `matcher_fn=get_matched_signals` (by reference,
    never called directly here) and no `signals_collection`/
    `signals_collection_2` argument of any kind — that injection happens
    entirely inside get_evidence_with_topup() itself, in logics.py. This
    function's own parameter list and call are unchanged.

    OTHERWISE COMPLETELY UNCHANGED — this function calls
    get_matched_signals() and analyze_with_claude() exactly as before,
    using whatever `keywords` was already stored on the message by
    add_search_to_chat() at write time."""
    for msg in messages or []:
        if not msg.get("topic_key"):
            continue

        # (STREAMING WIRING FIX) Reserved for the streaming route to fill
        # in instead — leave this one message completely untouched here.
        if skip_topic_key and msg.get("topic_key") == skip_topic_key:
            continue

        # (RESULTS-RECOMPUTE FIX) `results is None` means "genuinely never
        # computed yet" — an already-saved empty list `[]` (a legitimate
        # "no_results"/"not_available"/"disallowed" outcome, see BUGFIX
        # PACK #1 below) is now correctly recognized as already-done,
        # instead of `not []` (True) wrongly triggering a full re-match
        # + re-answer on every single later chat view.
        needs_results = msg.get("results") is None
        # Same `is None` reasoning as needs_results above, applied to
        # claude_answer for full consistency (see the docstring note in
        # _complete_message_answer_and_results()).
        needs_answer = msg.get("claude_answer") is None
        if not needs_results and not needs_answer:
            continue

        # (EVIDENCE-BUDGET FEATURE) Resolved once per message: this
        # message's own stored evidence_required (already clamped to
        # [MIN_ANALYSIS_EVIDENCE, MAX_ANALYSIS_EVIDENCE] by
        # _parse_router_json() at write time), or None for an
        # older/non-search message — passed straight through as `limit`
        # below so get_matched_signals() falls back to its own
        # MAX_MATCHED_RESULTS default whenever this is None, exactly as
        # it always has.
        effective_evidence_limit = msg.get("evidence_required")

        _fresh_prov = {} if freshness_enabled() else None      # (FRESHNESS) per-request provenance; None = off
        try:
            with _usage_attribution(chat_id=chat_id, owner_key=owner_key, topic_key=msg.get("topic_key"),
                                    request_id=msg.get("request_id")):
                matched = get_evidence_with_topup(
                    chat_id=chat_id,
                    owner_key=owner_key,
                    topic_key=msg["topic_key"],
                    keywords=msg.get("keywords", []),
                    evidence_required=effective_evidence_limit,
                    matcher_fn=get_matched_signals,
                    match_phrases=msg.get("match_phrases"),
                    targeting_platform=msg.get("targeting_platform", "all"),
                    since_days=msg.get("time_window_days"),
                    unfiltered=msg.get("unfiltered", False),
                    **({"time_range": msg["time_range"]} if msg.get("time_range") else {}),   # (DATE/TIME)
                    # STRICT: the intent bridge needs the query text; without it
                    # this path would return un-filtered posts. Flag off: unchanged.
                    **({"user_query": msg.get("query")} if strict_intent_mode() else {}),
                    **({"provenance_out": _fresh_prov} if _fresh_prov is not None else {}),   # (FRESHNESS)
                )
        except Exception as exc:
            log.warning(f"Signal matching failed for topic_key={msg.get('topic_key')}: {exc}")
            continue

        elapsed = _elapsed_seconds(msg.get("requested_at"))

        # (IMMEDIATE PARALLEL TRIGGERING) Both the Google search and the
        # search-progress UI generation now fire in PARALLEL with this
        # very first flintel_signals lookup above — right away, the
        # first time this message is processed — instead of waiting
        # GOOGLE_FALLBACK_TRIGGER_SECONDS (40s) as before. Each still
        # only ever fires ONCE per message (fire-once guards below,
        # marked synchronously before dispatch, unchanged from before).
        if flintel.should_trigger_immediately(msg.get("google_fallback_triggered", False)):
            mark_google_fallback_triggered(chat_id, owner_key, msg["topic_key"])
            if background_tasks is not None:
                background_tasks.add_task(_trigger_google_fallback_search, chat_id, owner_key, msg)
            else:
                _trigger_google_fallback_search(chat_id, owner_key, msg)

        if flintel.should_trigger_immediately(msg.get("search_progress_generated", False)):
            mark_search_progress_generated(chat_id, owner_key, msg["topic_key"])
            if background_tasks is not None:
                background_tasks.add_task(_generate_search_progress, chat_id, owner_key, msg)
            else:
                _generate_search_progress(chat_id, owner_key, msg)

        # (WEBSITE INTELLIGENCE — WEBSITE-ONLY SHORT-CIRCUIT) Website-only
        # messages never wait for Reddit/Google evidence (see the
        # docstring above) — the background Google search/search-progress
        # triggers above have already fired, so flintel_signals/
        # google_posts keep filling in for later follow-ups; this message's
        # own answer is built right away by
        # _complete_message_answer_and_results()'s website_only branch,
        # and the normal `merged_pool`/RESPONSE_TIMEOUT logic below is
        # skipped entirely for it.
        if msg.get("website_only"):
            if needs_answer:
                # Same busy-lock protection as the normal branch below —
                # never schedule a second, duplicate call for this owner
                # while one is already in flight (DUPLICATE-REFRESH FIX),
                # and mark busy synchronously BEFORE scheduling
                # (BUSY-LOCK RACE FIX).
                if not _is_owner_busy(owner_key):
                    _set_owner_busy(owner_key)
                    if background_tasks is not None:
                        background_tasks.add_task(_complete_message_answer_and_results, chat_id, owner_key, msg, matched)
                    else:
                        _complete_message_answer_and_results(chat_id, owner_key, msg, matched)
            elif needs_results:
                # Answer already saved but results never got written —
                # website-only messages never show Reddit post-cards, so
                # just persist the forced-empty list.
                msg["results"] = []
                try:
                    save_signal_results_to_chat(chat_id, owner_key, msg["topic_key"], [])
                except Exception as exc:
                    log.warning(f"Saving empty website-only results failed for topic_key={msg.get('topic_key')}: {exc}")
            continue

        # (TARGET-OR-TIMEOUT RULE) Jab tak flintel_signals ki posts target
        # (evidence_required) tak nahi pahunchi aur RESPONSE_TIMEOUT bhi nahi hua,
        # answer generate mat karo, agli poll/reload par dobara check hoga.
        # Timeout ke baad jo posts hon (chahe 3, chahe 45) unse answer banega.
        target_evidence = effective_evidence_limit or MIN_ANALYSIS_EVIDENCE
        if strict_intent_mode():
            # STRICT W2 + W3: the target is an upper bound. Answer as soon as
            # the scan is complete and something qualifies, or after
            # STRICT_WAIT_SECONDS; RESPONSE_TIMEOUT stays the ceiling.
            _st = get_scan_status(chat_id, msg.get("topic_key")) or {}
            if not strict_wait_done(len(matched), target_evidence, elapsed,
                                    bool(_st.get("complete")), RESPONSE_TIMEOUT):
                continue
        elif len(matched) < target_evidence and elapsed < RESPONSE_TIMEOUT:
            continue

        # (MERGE BEFORE ANSWERING) Pulls in whatever Google-search stub
        # results already exist for this message (from ANY prior trigger
        # of the block above, possibly a previous page load) — best
        # effort, never blocks waiting for Google's own call to finish.
        # Used ONLY to decide whether there's anything to answer from at
        # all; _complete_message_answer_and_results()/
        # _timeout_fallback_answer() each independently re-fetch and
        # merge again right before actually calling analyze_with_claude(),
        # so this pool can never go stale between this check and the
        # real answer generation.
        try:
            stub_docs = ([] if strict_intent_mode() else
                         google_search.get_stub_results_for_keywords(
                             google_posts_collection, msg.get("keywords", [])))
        except Exception as exc:
            log.warning(f"Fetching Google-fallback stubs failed for topic_key={msg.get('topic_key')}: {exc}")
            stub_docs = []
        google_results = flintel.format_google_stub_results(stub_docs)
        merged_pool = flintel.merge_matched_and_google_results(
            matched, google_results,
            **({"fill_with_stubs": False} if strict_intent_mode() else {}))

        if not merged_pool:
            # (RESPONSE_TIMEOUT'S NEW ROLE) No longer the primary "wait
            # until this many seconds have passed" trigger — the merge
            # above already runs every pass, as soon as both sources
            # have anything. RESPONSE_TIMEOUT now only matters as an
            # ultimate safety ceiling: only once it's been this long
            # since requested_at AND the merged pool is STILL genuinely
            # empty does the tier-3 closest-matches flow trigger, via
            # _timeout_fallback_answer() (which does its own tier-3
            # refinement internally — see that function's docstring).
            # STRICT: an empty pool is reported honestly after the strict
            # wait cap instead of idling until the full RESPONSE_TIMEOUT.
            _empty_deadline = (
                min(RESPONSE_TIMEOUT, _cfg_strict_wait())
                if strict_intent_mode() else RESPONSE_TIMEOUT
            )
            if needs_answer and elapsed >= _empty_deadline:
                # (DUPLICATE-REFRESH FIX) A refresh/poll hitting this exact
                # timeout branch again — very plausible here specifically,
                # since the message is ALREADY past RESPONSE_TIMEOUT, so
                # every subsequent reload keeps landing on this same
                # branch — must not fire a second _timeout_fallback_answer()
                # while one is still running for this owner. Only proceed
                # (and only then mark the owner busy) if no Claude call for
                # this owner is currently in flight; otherwise skip this
                # pass entirely and let the in-flight call finish and save
                # its own answer, which the next reload will simply see.
                #
                # (BUSY-LOCK RACE FIX) Set synchronously, in THIS request,
                # before scheduling/running the work — not inside the
                # scheduled function itself, which could run after the
                # response has already gone out to the browser.
                if not _is_owner_busy(owner_key):
                    _set_owner_busy(owner_key)
                    if background_tasks is not None:
                        background_tasks.add_task(
                            _timeout_fallback_answer, chat_id, owner_key, msg["topic_key"], msg["query"],
                            msg.get("keywords", []), msg.get("match_phrases"), msg.get("evidence_required"),
                            msg.get("website_note"), msg.get("resolved_query"),
                            **({"time_range": msg["time_range"]} if msg.get("time_range") else {}),   # (DATE/TIME)
                            request_id=msg.get("request_id"),
                            gate_owner=guest_gate.gate_owner(owner_key, msg),
                            time_window_days=msg.get("time_window_days"), requested_at=msg.get("requested_at"),
                            targeting_platform=msg.get("targeting_platform"),   # (GATE 2) the request's own platform selection,   # (RECENCY)
                        )
                    else:
                        _timeout_fallback_answer(
                            chat_id, owner_key, msg["topic_key"], msg["query"],
                            msg.get("keywords", []), msg.get("match_phrases"), msg.get("evidence_required"),
                            msg.get("website_note"), msg.get("resolved_query"),
                            **({"time_range": msg["time_range"]} if msg.get("time_range") else {}),   # (DATE/TIME)
                            request_id=msg.get("request_id"),
                            gate_owner=guest_gate.gate_owner(owner_key, msg),
                            time_window_days=msg.get("time_window_days"), requested_at=msg.get("requested_at"),
                            targeting_platform=msg.get("targeting_platform"),   # (GATE 2) the request's own platform selection,   # (RECENCY)
                        )
            continue

        # (BUGFIX PACK #1) Track whatever answer text is/becomes available
        # for this message, so results can be gated on its format below —
        # starts as whatever's already cached (may be None).
        answer_for_format_check = msg.get("claude_answer")

        # (PERFORMANCE FIX) This used to run inline here, blocking the
        # request on a live Claude call whenever needs_answer was True.
        # The exact same logic now lives in
        # _complete_message_answer_and_results() — run inline (identical
        # behavior) when no background_tasks was given, or scheduled to
        # run AFTER the response is sent when it was.
        #
        # (DUPLICATE-REFRESH FIX) A refresh/poll for a message whose
        # answer is still being generated (claude_answer still None) used
        # to reach this exact branch again on every reload, since nothing
        # here checked whether a Claude call for this SAME owner was
        # already in flight — only _set_owner_busy() ran, never
        # _is_owner_busy(). That silently fired a second, independent
        # analyze_with_claude() call for the same message: double Claude
        # billing, and a race on which call's result gets saved last.
        # Now, if this owner is already busy (an earlier call for this or
        # another message from them hasn't finished yet), this request is
        # simply skipped — no new Claude call, no _set_owner_busy()
        # re-set — and the NEXT reload/poll will see claude_answer still
        # None and try again, until the in-flight call finishes and
        # clears the flag itself. This can only ever SKIP a redundant
        # call; it never blocks the original one.
        #
        # (BUSY-LOCK RACE FIX) Set synchronously, in THIS request, before
        # scheduling/running the work — closes the small window where a
        # second request from the same owner could arrive between "the
        # response is sent" and "the background task actually starts",
        # since the flag document wouldn't exist yet during that window.
        if not _is_owner_busy(owner_key):
            _set_owner_busy(owner_key)
            _fp_kw = {"freshness_prov": _fresh_prov} if _fresh_prov is not None else {}   # (FRESHNESS) off = call unchanged
            if background_tasks is not None:
                background_tasks.add_task(_complete_message_answer_and_results, chat_id, owner_key, msg, matched, **_fp_kw)
            else:
                _complete_message_answer_and_results(chat_id, owner_key, msg, matched, **_fp_kw)


# ─────────────────────────────────────────────────────────────────────────────
# (NON-BLOCKING CHAT PAGE) Finishing older unfinished research turns
# ─────────────────────────────────────────────────────────────────────────────
# view_chat() used to call _fill_in_message_outputs() for every older
# unfinished search turn before rendering, so the page waited for a full
# posts scan. It now only records those turns in the durable queue
# (turn_finisher.py) and wakes the worker; the worker calls the SAME
# _fill_in_message_outputs() for one turn at a time, behind a per-turn lease.

def _finish_turn_job(chat_id: str, owner_key: str, topic_key: str):
    """Processes ONE queued turn (called by turn_finisher under its lease).
    Returns (status, delay_seconds) — see turn_finisher.process_one().

    Never marks a turn completed or failed by itself: the only work done is
    the existing _fill_in_message_outputs() pass for that message, run
    inline (background_tasks=None), exactly what a chat page view used to
    trigger. The turn leaves the queue only once the chat shows both
    `results` and `claude_answer` set."""
    chat = get_chat_session(chat_id, owner_key)
    if not chat:
        return "done", None                      # chat deleted / not this owner's
    messages = chat.get("messages") or []
    pos = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("topic_key") == topic_key), None)
    if pos is None:
        return "done", None
    msg = messages[pos]
    if msg.get("results") is not None and msg.get("claude_answer") is not None:
        return "done", None

    # A turn being streamed is never claimed here: the stream holds this
    # turn's lease while it runs (turn_finisher.hold_for_stream) and hands
    # it back when it ends or the browser drops it.
    # Another answer call (or a stream) for this owner is in flight — the
    # same rule _fill_in_message_outputs() applies before calling the LLM.
    # Checked first so the scan is not repeated while it runs.
    if _is_owner_busy(owner_key):
        return "busy", None

    _fill_in_message_outputs(chat_id, owner_key, [msg], background_tasks=None)

    fresh = get_chat_session(chat_id, owner_key) or {}
    fresh_msgs = fresh.get("messages") or []
    fresh_msg = next((m for m in reversed(fresh_msgs) if m.get("topic_key") == topic_key), None)
    if fresh_msg is None or (fresh_msg.get("results") is not None and fresh_msg.get("claude_answer") is not None):
        return "done", None
    # Still waiting for evidence before the answer deadline: check again
    # soon (not an attempt). Past the deadline something should have
    # produced an answer (e.g. the answer call failed): back off.
    deadline = min(RESPONSE_TIMEOUT, _cfg_strict_wait()) if strict_intent_mode() else RESPONSE_TIMEOUT
    elapsed = _elapsed_seconds(fresh_msg.get("requested_at"))
    if elapsed < deadline:
        return "retry", min(turn_finisher.PENDING_RETRY_SECONDS, max(1.0, deadline - elapsed + 1))
    return "retry", None


def schedule_unfinished_turns(chat_id: str, owner_key: str, messages: list, skip_topic_key: str = None) -> list:
    """Called by view_chat() instead of the old synchronous fill-in: records
    every unfinished search turn of this chat (except the one reserved for
    the stream) in the durable queue, due now, and wakes the worker. Only
    cheap queue writes happen in the request. Never raises."""
    ids = []
    try:
        for key in turn_finisher.ondemand_topic_keys(messages):   # legacy: all; dry run: none; governed: recent + capped
            if skip_topic_key and key == skip_topic_key:
                continue
            if turn_finisher.enqueue(chat_id, owner_key, key, due_in_seconds=0, pull_forward=True):
                ids.append(turn_finisher.job_id(chat_id, owner_key, key))
        if ids:
            turn_finisher.wake(ids)
    except Exception as exc:
        log.warning(f"Scheduling unfinished turns failed for chat_id={chat_id}: {exc}") 
    return ids


turn_finisher.configure(_finish_turn_job)

 
@app.on_event("startup")
async def _start_turn_finisher():
    turn_finisher.start()


@app.on_event("shutdown")
async def _stop_turn_finisher():
    await turn_finisher.stop()


import routes  # noqa: F401  (registers every route on `app`)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("index:app", host="0.0.0.0", port=8080, reload=True)
