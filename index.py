"""
FLINTEL — WEB SERVICE (v7)
============================
Everything from v3 is UNCHANGED and still works exactly as before:
  1. Take a user prompt (brand/topic/product name) from a simple web form.
  2. Generate FUZZY KEYWORDS in plain Python (template-based, no Claude call).
     These are UNCHANGED regardless of platform selection.
  3. Patch (upsert) them into the SAME MongoDB used by Background Service #1
     — collection `flintel_search_jobs` — with status="pending".
  4. Every search redirects straight back to the chat page (index.html),
     where results from `flintel_signals` for that topic appear inline as
     part of the same chat turn — no separate status page.
  5. Auth (Google OAuth + email/password), accounts in `flintel_users`.
  6. Chat/session memory (like Claude / ChatGPT) in `flintel_users_chat`,
     keyed by email (signed in) or an anon UUID cookie (guest), with guest
     -> email migration on sign-up/login.
  7. Matched-signal output (title + post_text + post_url + platform),
     matched purely by search_keyword against the job's generated keyword
     list, saved onto the same chat message as the original prompt.

v4 — CLAUDE ANALYSIS LAYER (NEW, on top of everything above):
  - The matched signals from step 7 are NOT shown to the user directly as
    raw dumped output. Instead, once matched, they (title + text ONLY —
    never the URL, never platform, never job internals) are handed to
    Claude as grounding context, together with the user's own chat
    prompt.
  - Claude then decides, naturally, how to actually answer what the user
    asked — a summary, a sentiment breakdown, a drafted reply, an
    opinion, a plain "not enough data", whatever fits the question.
    Whenever Claude judges a table is the clearest way to explain
    something, it writes one (as part of its normal answer text) — no
    special-casing needed here, it's just Claude writing markdown.
  - Model used: claude-haiku-4-5-20251001 (cheap + fast, configurable via
    CLAUDE_MODEL env var).
  - CHUNKING: if a topic's matched posts are too many to comfortably fit
    one call, they're split into batches (CLAUDE_POSTS_PER_CHUNK posts
    per batch). Each batch gets its own cheap Haiku call that extracts
    only the grounded points relevant to the user's question from that
    batch (a "map" step). Those batch notes are then combined into one
    final Haiku call, using the same analysis system prompt, to produce
    the actual answer (a "reduce" step). Small topics skip all of this
    and go straight to a single call.
  - COST: only Claude's OUTPUT (the final answer text) is ever stored on
    the chat message. The INPUT (the matched posts) is never re-saved
    alongside it — it's already sitting in `flintel_signals` /
    reconstructable from the job's keyword list, so storing it a second
    time would be pure waste. If the answer is ever missing (message
    predates this feature, or a first attempt failed), it's regenerated
    once matched posts are available and then cached the same way.
  - POST CARDS (title + post_url) are completely unaffected — the exact
    same `results` field (title, post_text, post_url, platform) computed
    by get_matched_signals() / saved by save_signal_results_to_chat()
    keeps working exactly as it did in v3. Claude never sees post_url and
    never produces it; the card UI renders the real one directly from
    `flintel_signals`, as-is, same as before.

NOTE ON PLATFORM TARGETING (v2, unchanged):
  The search form sends which platform the user picked in the
  "All Platforms" dropdown (All Platforms / Reddit / X / Twitter / LinkedIn /
  Facebook). This does NOT change keyword generation in any way — the exact
  same fuzzy keywords are sent either way. All that happens is a new field,
  `targeting_platform`, is added to the job document:
    - "All Platforms" selected  -> targeting_platform = "all"
    - "Reddit" selected         -> targeting_platform = "reddit"
    - "X / Twitter" selected    -> targeting_platform = "x_twitter"
    - "LinkedIn" selected       -> targeting_platform = "linkedin"
    - "Facebook" selected       -> targeting_platform = "facebook"
  Background Service #1 (or a later version of it) can read this field to
  decide whether to search everywhere or restrict itself to one platform.
  This service itself does no filtering — it only tags the job.

NOTE: This service sends KEYWORDS ONLY. Background Service #1 searches
Reddit SITE-WIDE per keyword (no subreddit restriction) — so there is no
subreddit targeting to generate here. Wherever a matching post actually
lives on Reddit, its real subreddit is captured automatically by the
background service.

This service does NOT talk to Background Service #1 directly. The only
connection between the two is the SAME MongoDB connection string
(MONGODB_URI / MONGODB_DB) and the SAME collection names. Claude is only
ever called by THIS service, purely to turn already-matched signals into
an answer for the user — it never touches jobs_collection or
signals_collection itself.

Stack: FastAPI + Jinja2 templates + pymongo + Authlib (Google OAuth) +
passlib (password hashing) + Starlette SessionMiddleware (login sessions
AND the anonymous chat-owner cookie) + httpx (Claude API calls).

Run:
    pip install fastapi uvicorn jinja2 python-multipart pymongo python-dotenv \
                authlib httpx itsdangerous "passlib[bcrypt]"
    uvicorn index:app --reload --port 8080

Required env vars (add to .env):
    MONGODB_URI=...
    MONGODB_DB=flintel_bot
    SESSION_SECRET_KEY=some-long-random-string
    GOOGLE_CLIENT_ID=...
    GOOGLE_CLIENT_SECRET=...
    ANTHROPIC_API_KEY=...
    CLAUDE_MODEL=claude-haiku-4-5-20251001   # optional, this is the default
    RESPONSE_TIMEOUT=60                      # optional, this is the default (seconds)
    MAX_POSTS_PER_PLATFORM=3                 # optional, this is the default (v7, see below)

── v4.1 FIX ───────────────────────────────────────────────────────────────
Only ONE behavior changed from the v4 file above: the /search route used to
always `RedirectResponse(url="/")` no matter what, which bounced every
follow-up search back to the home screen instead of keeping the user on the
chat thread they were just talking in (like Claude/ChatGPT do). It now
redirects to `/chat/{chat_id}` — the same chat the message was just saved
into — falling back to "/" only if chat bookkeeping itself failed. Nothing
else in that update was touched.

── v4.2 FIX ───────────────────────────────────────────────────────────────
FastAPI's auto-generated API docs are now disabled: the FastAPI(...) app is
constructed with docs_url=None, redoc_url=None, openapi_url=None, so
/docs, /redoc, and /openapi.json all 404 instead of publicly exposing every
route, request/response shape, and internal field name. Nothing else in
this file was touched.

── v4.3 FIX (this file) ───────────────────────────────────────────────────
Adds ONE new capability: deleting a single chat. A new
`POST /chat/{chat_id}/delete` route lets the current owner (signed-in
email, or guest UUID) remove exactly ONE of their own chats —
never every chat belonging to them, and never a chat belonging to a
different owner. It reuses the exact same owner-scoping pattern already
used by get_chat_session()/view_chat() (filtering by BOTH chat_id AND
owner_key on the Mongo query itself, not just checking after the fact),
so a guest or another account can't delete a chat by guessing its id —
identical protection to how a chat is already read. If the deleted chat
happened to be the currently active one, `active_chat_id` is cleared from
the session so the home page falls back to showing no active chat instead
of a stale/missing one. Nothing else in this file was touched.

── v4.4 FIX ───────────────────────────────────────────────────────────────
POST /chats/new now reuses the currently active chat instead of creating a
brand-new empty one if that active chat belongs to the same owner and has
zero messages yet — avoids piling up dead, message-less chats in the
sidebar from repeated "New chat" clicks. Nothing else in that update was
touched.

── v5 FEATURE ───────────────────────────────────────────────────────────
Adds a ROUTING step in front of everything else in POST /search. A single
cheap Claude call now decides whether the user's message is:

  - a genuine SEARCH request (wants social-listening data pulled about a
    brand/product/topic) -> the ENTIRE v1–v4.3 pipeline above runs exactly
    as it always has, completely UNTOUCHED: fuzzy keywords are generated,
    a job is patched into flintel_search_jobs, and matched flintel_signals
    posts get analyzed by Claude — all 100% as before, byte-for-byte the
    same functions (generate_fuzzy_keywords, enqueue_search_job,
    add_search_to_chat, get_matched_signals, analyze_with_claude,
    save_signal_results_to_chat, save_claude_answer_to_chat).

  - a plain CHAT message (a greeting, small talk, a general question,
    "what's up", a follow-up about something already discussed, etc.) ->
    NOTHING is generated or patched into flintel_search_jobs for it at
    all — no fuzzy keywords, no job, no signal matching. Claude answers
    it directly (in the SAME routing call, to save a round trip) and the
    reply is saved onto the chat as a new lightweight message
    ("message_type": "chat"). The existing search pipeline is never
    touched or invoked for these messages.

Also adds a short, plain-PYTHON (no extra Claude call) rolling SUMMARY
kept on each chat doc — one condensed line per turn, capped to the last
CHAT_SUMMARY_MAX_TURNS turns — so Claude gets cheap conversational
continuity (for the routing decision, and for any chat-type reply)
without ever being sent the full raw message history for the chat.

Any failure anywhere in the new routing step (Claude API error, bad JSON,
session hiccup) safely falls back to the "search" path, so this new
feature can only ever add a shortcut — it can never break or block the
pre-existing search pipeline, which stays the safe default on any doubt.

── v6 FEATURE ─────────────────────────────────────────────────────────────
Adds TWO small, self-contained additions on top of v5. Nothing else in
this file — no other route, function, or behavior — was touched.

1. RESPONSE TIMEOUT (new `RESPONSE_TIMEOUT` env var, default 60 seconds):
   Previously, a search-type message with no matched signals yet just sat
   forever with `claude_answer = None` — home()/view_chat() would keep
   silently retrying `get_matched_signals()` on every page load, with no
   answer ever shown if the background service never found anything for
   that topic.
   Now, `_fill_in_message_outputs()` checks how long it's been since the
   message's own `requested_at` timestamp. Once that exceeds
   RESPONSE_TIMEOUT seconds with STILL no matched posts, it calls
   `analyze_with_claude(query, [])` exactly once — the SAME function
   already used for real answers, just handed an empty post list. That
   function already has a built-in "no posts were found, say that plainly"
   branch (see analyze_with_claude's docstring, case 1) — so this reuses
   existing, already-reviewed prompting instead of adding a new one, and
   Claude naturally replies with a plain "sorry, couldn't find anything on
   this yet" — exactly the way Claude/ChatGPT would when they genuinely
   have nothing to go on. That answer is cached via the existing
   save_claude_answer_to_chat()/append_to_chat_summary() calls, so it's
   generated once and never re-billed. If matched posts show up LATER
   (background service just took longer than 60s), that's fine too:
   `needs_answer` only re-triggers if `claude_answer` is still falsy, so
   once the timeout answer is cached, it stays as the final answer for
   that message, same caching rule as every other answer in this file.
   Before the timeout is reached, behavior is 100% unchanged: still just
   silently waits and retries on next page load, exactly like v1-v5.

2. ABUSE / HARMFUL CONTENT BLOCKING (folded into the existing v5 router):
   The SAME single cheap Claude call in classify_and_maybe_chat() now also
   screens for abusive, harassing, hateful, sexually explicit, threatening,
   or otherwise harmful messages — no second API call, no new service, it
   just adds a third possible classification alongside "search" and "chat":
   `{"intent": "blocked", "reply": "<short, polite decline>"}`.
   A "blocked" message never reaches the search pipeline (no keywords, no
   job) and never reaches the plain-chat fallback either — it's saved onto
   the chat as a normal lightweight "chat"-type message (same shape/field
   as v5's chat messages, so no template changes are needed anywhere) whose
   `claude_answer` is just the polite decline text, with CLAUDE_BLOCKED_
   FALLBACK_REPLY used as a safety-net string if the router flagged
   "blocked" but didn't return usable reply text.
   Same safety rule as the rest of the v5 router: ANY failure in this step
   (API error, bad JSON, timeout) still falls back to intent="search", so
   this is a best-effort courtesy layer on top of the product, not a
   guaranteed content filter — it can only ever add a shortcut/decline on
   top of the existing pipeline, it can never be the reason a genuine
   search silently fails to run.

── v7 FEATURE (this file) ─────────────────────────────────────────────────
Adds TWO small, self-contained changes, BOTH scoped entirely inside
get_matched_signals() (the same function that has always powered post
cards / Claude's grounding data). Nothing else in this file — no other
route, function, template contract, or behavior — was touched.

1. PER-PLATFORM RESULT CAP (new `MAX_POSTS_PER_PLATFORM` env var, default
   3): Previously, MAX_MATCHED_RESULTS (default 25) was one shared budget
   across every platform combined — e.g. a single search with
   targeting_platform="all" could come back as 20 Reddit posts and only 1
   X/Twitter post if that's simply what matched first, however lopsided.
   Now, in addition to that existing overall MAX_MATCHED_RESULTS safety
   cap (still respected, still unit-for-unit the same variable/behavior
   as before), each individual platform is ALSO capped at
   MAX_POSTS_PER_PLATFORM matches for that one search — e.g. with the
   default of 3, a single search now returns AT MOST 3 Reddit posts, AT
   MOST 3 X/Twitter posts, AT MOST 3 LinkedIn posts, and AT MOST 3
   Facebook posts, so no one platform can crowd out the others in a
   single prompt's results. MAX_POSTS_PER_PLATFORM is a plain env var —
   change it in .env (or override at process start) and restart to pick
   a different per-platform number later; no code change needed.

2. BROADER KEYWORD MATCHING (title / post_text substring matching, ON TOP
   OF the existing search_keyword field matching — the existing
   search_keyword matching is completely untouched and still works
   exactly as perfectly/exactly as it always has): previously, a signal
   only ever counted as a match if its own search_keyword-like field
   (see _KEYWORD_FIELD_CANDIDATES) matched one of the job's generated
   keywords. Now, a signal ALSO counts as a match if any of the job's
   generated keywords appears as a case-insensitive substring inside
   that signal's OWN title or post_text (see the new
   _text_matches_keyword() helper below) — even if its search_keyword
   field doesn't match at all. This is a pure OR: a signal matches if
   EITHER its search_keyword field matches, OR the keyword phrase shows
   up in its title, OR the keyword phrase shows up in its post_text —
   any one of the three is enough, and matching more than one of them
   doesn't count it twice (each matched signal still only ever appears
   once in the results, exactly as before via the existing seen_urls
   de-duplication). Because a signal that only matches via title/text
   (and not search_keyword) would never even be fetched by the OLD
   Mongo query (which only ever filtered on the keyword field), the
   Mongo query itself was widened with additional case-insensitive
   regex OR-conditions on the title/text field candidates, so those
   documents are actually retrieved from `flintel_signals` before the
   Python-side check confirms the match. Everything else about
   get_matched_signals() — its return shape ({title, post_text,
   post_url, platform}), its signature, its platform-targeting filter,
   its de-duplication by post_url, and every other caller in this file
   (analyze_with_claude, build_claude_post_context, post cards, etc.) —
   is completely unchanged.
──────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import json
import uuid
import logging
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from passlib.context import CryptContext
from authlib.integrations.starlette_client import OAuth

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("flintel-web")

# ─────────────────────────────────────────────────────────────────────────────
# ENV / CONFIG — SAME MongoDB as Background Service #1
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB  = os.getenv("MONGODB_DB", "flintel_bot")

MAX_KEYWORDS = int(os.getenv("MAX_KEYWORDS", "20"))

# How many matched (post_text + url) results to surface per topic. Kept
# separate from MAX_KEYWORDS since it's about signal output, not keyword
# generation. (v7: this remains the OVERALL safety cap across every
# platform combined — see MAX_POSTS_PER_PLATFORM below for the new
# additional per-platform cap layered on top of this one.)
MAX_MATCHED_RESULTS = int(os.getenv("MAX_MATCHED_RESULTS", "25"))

# (v7, NEW) Caps how many matched posts ANY SINGLE platform can contribute
# to one search's results — e.g. with the default of 3, at most 3 Reddit
# posts AND at most 3 X/Twitter posts (etc.) show up for one prompt, even
# if many more than that actually matched, so no one platform can crowd
# out the others. Purely a config value — change it in .env and restart
# to use a different number later; see get_matched_signals() below for
# where it's applied.
MAX_POSTS_PER_PLATFORM = int(os.getenv("MAX_POSTS_PER_PLATFORM", "3"))

SESSION_SECRET_KEY  = os.getenv("SESSION_SECRET_KEY", "dev-only-change-me")
GOOGLE_CLIENT_ID    = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# ── Claude analysis layer config (v4) ──────────────────────────────────────
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL            = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_MAX_TOKENS       = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))
CLAUDE_MAP_MAX_TOKENS   = int(os.getenv("CLAUDE_MAP_MAX_TOKENS", "512"))
CLAUDE_POSTS_PER_CHUNK  = int(os.getenv("CLAUDE_POSTS_PER_CHUNK", "12"))
CLAUDE_TIMEOUT_SECONDS  = float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "30"))
CLAUDE_API_URL          = "https://api.anthropic.com/v1/messages"
CLAUDE_API_VERSION      = "2023-06-01"

# ── Router + chat-summary config (v5) ──────────────────────────────────────
CLAUDE_ROUTER_MAX_TOKENS       = int(os.getenv("CLAUDE_ROUTER_MAX_TOKENS", "400"))
CHAT_SUMMARY_MAX_TURNS         = int(os.getenv("CHAT_SUMMARY_MAX_TURNS", "8"))
CHAT_SUMMARY_TURN_CHAR_LIMIT   = int(os.getenv("CHAT_SUMMARY_TURN_CHAR_LIMIT", "160"))

# ── Response-timeout config (v6) ────────────────────────────────────────────
# How long (seconds) a search-type message is allowed to sit with no
# matched flintel_signals before we stop silently waiting and instead give
# the user a plain, natural "nothing found on this yet" answer, the same
# way Claude/ChatGPT would rather than leaving them staring at a blank
# turn forever. See _fill_in_message_outputs() below.
RESPONSE_TIMEOUT = int(os.getenv("RESPONSE_TIMEOUT", "60"))

client = MongoClient(MONGODB_URI)
db = client[MONGODB_DB]

# Same two collections Background Service #1 already uses.
jobs_collection    = db.flintel_search_jobs
signals_collection = db.flintel_signals

# User accounts (Google OAuth + email/password).
users_collection = db.flintel_users
users_collection.create_index("email", unique=True, sparse=True)
users_collection.create_index("google_id", unique=True, sparse=True)

# Chat/session memory (Claude/ChatGPT-style conversations).
chats_collection = db.flintel_users_chat
chats_collection.create_index("chat_id", unique=True)
chats_collection.create_index("owner_key")

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

# Required for login sessions (stores a signed cookie, not the DB user doc).
# Also doubles as where we stash the anonymous chat-owner UUID and the
# currently active chat_id, exactly like a browser-local "current chat"
# pointer in Claude/ChatGPT.
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY)

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

# ─────────────────────────────────────────────────────────────────────────────
# TOPIC NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────

def normalize_topic_key(query: str) -> str:
    """Turns a user's raw prompt into a stable cache/job key.
    e.g. "  Nike  " -> "nike", "Nike Shoes!!" -> "nike shoes" """
    cleaned = re.sub(r"[^a-z0-9\s]", "", query.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM TARGETING
# ─────────────────────────────────────────────────────────────────────────────

# Maps whatever label the "All Platforms" dropdown sent to a stable key.
# This ONLY tags the job — it never changes what keywords get generated.
PLATFORM_KEY_MAP = {
    "all platforms": "all",
    "all":           "all",
    "reddit":        "reddit",
    "x / twitter":   "x_twitter",
    "x/twitter":     "x_twitter",
    "twitter":       "x_twitter",
    "x":             "x_twitter",
    "linkedin":      "linkedin",
    "facebook":      "facebook",
}


def normalize_platform(raw: str) -> str:
    """Falls back to "all" for anything unrecognized, so a missing/garbled
    dropdown value never accidentally narrows a search."""
    key = (raw or "").strip().lower()
    return PLATFORM_KEY_MAP.get(key, "all")


# ─────────────────────────────────────────────────────────────────────────────
# FUZZY KEYWORD GENERATION — plain Python, template-based, no Claude call
# UNCHANGED from v1 — platform selection never alters this.
# ─────────────────────────────────────────────────────────────────────────────

KEYWORD_TEMPLATES = [
    "{q}",
    "{q} review",
    "{q} reviews",
    "{q} honest review",
    "{q} complaint",
    "{q} complaints",
    "{q} problem",
    "{q} problems",
    "{q} issue",
    "{q} issues",
    "{q} experience",
    "{q} experiences",
    "is {q} worth it",
    "is {q} good",
    "{q} worth it",
    "switched from {q}",
    "switched to {q}",
    "alternative to {q}",
    "{q} vs",
    "{q} pricing",
    "{q} customer service",
    "{q} support",
    "{q} quality",
    "{q} scam",
    "{q} recommend",
    "thoughts on {q}",
    "anyone using {q}",
    "anyone tried {q}",
]


def generate_fuzzy_keywords(query: str) -> list:
    """Builds a list of search phrases around the user's query using fixed
    templates. Deterministic, fast, and needs no external API call."""
    q = query.strip()
    if not q:
        return []

    seen = set()
    keywords = []
    for template in KEYWORD_TEMPLATES:
        phrase = template.format(q=q).strip()
        key = phrase.lower()
        if key not in seen:
            seen.add(key)
            keywords.append(phrase)
        if len(keywords) >= MAX_KEYWORDS:
            break

    return keywords


# ─────────────────────────────────────────────────────────────────────────────
# JOB QUEUE — patch into the SAME MongoDB Background Service #1 reads
# ─────────────────────────────────────────────────────────────────────────────

def enqueue_search_job(topic_key: str, keywords: list, targeting_platform: str):
    """Upserts a job by topic_key. Re-searching the same topic simply
    resets it to pending instead of creating a duplicate job.

    `keywords` is generated exactly as before (v1) — `targeting_platform`
    is the only new field, added so Background Service #1 can optionally
    restrict itself to one platform instead of searching everywhere."""
    jobs_collection.update_one(
        {"topic_key": topic_key},
        {"$set": {
            "topic_key":          topic_key,
            "keywords":           keywords,
            "targeting_platform": targeting_platform,  # "all" | "reddit" | "x_twitter" | "linkedin" | "facebook"
            "status":             "pending",
            "requested_at":       datetime.now(timezone.utc),
            "started_at":         None,
            "completed_at":       None,
            "matched_count":      0,
            "error":              None,
        }},
        upsert=True,
    )
    log.info(
        f"Job queued | topic_key={topic_key} | keywords={len(keywords)} "
        f"| targeting_platform={targeting_platform}"
    )


def get_job(topic_key: str):
    return jobs_collection.find_one({"topic_key": topic_key}, {"_id": 0})


def get_signal_count(topic_key: str) -> int:
    return signals_collection.count_documents({"topic_key": topic_key})


def get_signals(topic_key: str, limit: int = 25):
    return list(
        signals_collection.find({"topic_key": topic_key}, {"_id": 0})
        .sort("created_utc", -1)
        .limit(limit)
    )


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL MATCHING — pull post_text + post_url for signals whose search
# keyword matches one of the keywords generated for this job, OR (v7, NEW)
# whose title/post_text itself contains one of those keywords. This is
# exactly what still powers the post cards (title + post_url, as-is).
# Claude (below) only ever sees title + post_text from whatever this
# returns — never post_url, never platform.
#
# `flintel_signals` docs may use slightly different field names depending
# on how Background Service #1 writes them, so this reads a small list of
# likely candidates for each field instead of hard-coding a single name.
# Adjust FIELD candidates below if your signals schema differs.
# ─────────────────────────────────────────────────────────────────────────────

_KEYWORD_FIELD_CANDIDATES  = ["search_keyword", "keyword", "matched_keyword", "query_keyword", "keywords"]
_TITLE_FIELD_CANDIDATES    = ["title", "post_title", "headline"]
_TEXT_FIELD_CANDIDATES     = ["post_text", "text", "body", "content", "selftext"]
_URL_FIELD_CANDIDATES      = ["post_url", "url", "link", "permalink"]
_PLATFORM_FIELD_CANDIDATES = ["platform", "source", "source_platform"]

# Maps a job's targeting_platform ("all" | "reddit" | "x_twitter" |
# "linkedin" | "facebook" — see normalize_platform()) to the value(s) a
# signal doc's own platform field may use. "all" has no entry here since
# it means "no filter" and is handled separately.
_PLATFORM_DOC_VALUES = {
    "reddit":    ["reddit"],
    "x_twitter": ["twitter", "x", "x_twitter", "x/twitter"],
    "linkedin":  ["linkedin"],
    "facebook":  ["facebook"],
}


def _first_present(doc: dict, candidates: list):
    for field in candidates:
        value = doc.get(field)
        if value:
            return value
    return None


def _signal_platform_matches(doc: dict, targeting_platform: str) -> bool:
    """True if this signal's platform field (or, if that's missing, the
    platform inferred from its post URL) matches the job's
    targeting_platform. "all" (or anything unrecognized) means no
    filtering — every platform is allowed, exactly as it behaves today.
    Only when the user actually picked a specific platform (Reddit, X /
    Twitter, LinkedIn, Facebook) does this narrow results down to signals
    from that one platform."""
    if not targeting_platform or targeting_platform == "all":
        return True

    allowed = _PLATFORM_DOC_VALUES.get(targeting_platform)
    if not allowed:
        return True  # unrecognized targeting value -> don't accidentally exclude everything

    doc_platform = _first_present(doc, _PLATFORM_FIELD_CANDIDATES)
    if not doc_platform or not isinstance(doc_platform, str):
        # No platform field on this doc — fall back to guessing from its
        # URL rather than excluding it outright.
        doc_platform = _infer_platform_from_url(_first_present(doc, _URL_FIELD_CANDIDATES))
    if not doc_platform:
        return False

    return doc_platform.strip().lower() in allowed


def _signal_keyword_matches(doc: dict, keyword_set: set) -> bool:
    """True if this signal's search-keyword field matches ANY keyword in
    keyword_set (case-insensitive), whichever one it happens to be.
    Handles the keyword field being a single string OR a list (in case a
    signal doc records more than one matched keyword).

    UNCHANGED from v3/v4/v5/v6 — this exact-field check is completely
    untouched by v7; it still works exactly as perfectly as it always
    has. v7 only ever ADDS more ways a signal can match (see
    _text_matches_keyword() below) — it never removes or loosens this
    one."""
    if not keyword_set:
        return True  # no keyword filter to apply -> don't exclude anything

    raw_value = None
    for field in _KEYWORD_FIELD_CANDIDATES:
        if field in doc and doc[field]:
            raw_value = doc[field]
            break

    if not raw_value:
        return False

    if isinstance(raw_value, (list, tuple, set)):
        candidates = raw_value
    else:
        candidates = [raw_value]

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip().lower() in keyword_set:
            return True
    return False


def _text_matches_keyword(text: str, keyword_set: set) -> bool:
    """(v7, NEW) True if ANY keyword in keyword_set appears as a
    case-insensitive SUBSTRING somewhere inside `text`. This is what lets
    a signal count as a match purely because a keyword phrase shows up in
    its own title or post_text, even when its search_keyword field
    doesn't match at all — a pure additional OR path alongside
    _signal_keyword_matches() above, never a replacement for it."""
    if not text or not isinstance(text, str):
        return False
    text_lower = text.lower()
    for kw in keyword_set:
        if kw and kw in text_lower:
            return True
    return False


def _infer_platform_from_url(url: str):
    """Fallback for when a signal doc doesn't have a usable platform field:
    guesses the platform from the post URL's domain so the icon/badge still
    matches reality (e.g. a twitter.com/x.com link shows the X icon instead
    of falling back to a generic globe). Only used when the doc's own
    platform field is missing — never overrides an actual platform value."""
    if not url or not isinstance(url, str):
        return None
    domain = url.lower()
    if "twitter.com" in domain or "x.com" in domain:
        return "twitter"
    if "reddit.com" in domain:
        return "reddit"
    if "facebook.com" in domain or "fb.com" in domain:
        return "facebook"
    if "linkedin.com" in domain:
        return "linkedin"
    return None


def get_matched_signals(topic_key: str, keywords: list, targeting_platform: str = "all", limit: int = None) -> list:
    """Reads `flintel_signals` and keeps only the signals that match this
    job's generated keywords. topic_key match is intentionally NOT
    required: Background Service #1 may store its own topic_key for a
    signal, but what decides a match here is purely the keyword-matching
    rules below.

    A signal counts as a match if ANY ONE of these is true (v7: this is
    now three OR'd conditions instead of just the first one):
      1. its search-keyword field matches one of our generated keywords
         (see _signal_keyword_matches() — UNCHANGED, still exact/perfect,
         same as v3-v6), OR
      2. (v7, NEW) one of our generated keywords appears as a
         case-insensitive substring inside its OWN title, OR
      3. (v7, NEW) one of our generated keywords appears as a
         case-insensitive substring inside its OWN post_text.
    Matching via more than one of these at once still only ever produces
    ONE entry in the results (de-duplicated by post_url exactly as
    before) — this only widens WHICH signals can match, it never changes
    how a matched signal is de-duplicated or shaped.

    `targeting_platform` (the same "all" | "reddit" | "x_twitter" |
    "linkedin" | "facebook" value already stored on the job/message) is
    applied on top: "all" pulls a match from whichever platform it came
    from, exactly as before; any specific platform restricts matches to
    signals from that platform only — the user's dropdown choice decides
    this, nothing else.

    (v7, NEW) PER-PLATFORM CAP: on top of the existing overall `limit`
    (MAX_MATCHED_RESULTS by default — still respected, still the same
    variable/behavior as before), each individual platform can
    contribute AT MOST MAX_POSTS_PER_PLATFORM matches to this call's
    results (default 3) — e.g. with targeting_platform="all", a single
    search now returns at most 3 Reddit posts AND at most 3 X/Twitter
    posts AND at most 3 LinkedIn posts AND at most 3 Facebook posts,
    instead of one shared budget that a single platform could dominate.
    Purely a config value (MAX_POSTS_PER_PLATFORM env var) — change it
    in .env and restart the process to use a different number later.

    Returns {title, post_text, post_url, platform} for each match — this
    is the only signal-derived output ever shown to the user (via post
    cards) or persisted onto a chat message's `results` (platform is
    included only so the UI can show which platform a result came from;
    it isn't used for anything else here). Never touches jobs_collection
    or the raw `signals` list returned by get_signals().

    NOTE (v7): this function's SIGNATURE, RETURN SHAPE, and every caller
    (analyze_with_claude via build_claude_post_context, post cards via
    save_signal_results_to_chat, etc.) are completely unchanged — only
    the matching rule and the per-platform cap described above are new.
    It's also the single source of truth Claude's analysis is grounded
    in — see build_claude_post_context() below, which strips
    post_url/platform back out before anything goes to Claude."""
    limit = limit or MAX_MATCHED_RESULTS
    keyword_list = [k for k in (keywords or []) if k]
    keyword_set = {k.strip().lower() for k in keyword_list}
    if not keyword_set:
        return []

    # (v7) Widen the Mongo query itself: previously this only ever
    # filtered on the keyword field(s) with $in. Now it ALSO fetches any
    # doc whose title/text field candidates contain one of our keywords
    # as a case-insensitive substring (via a single combined regex per
    # field), so documents that would only match via title/text (and
    # never had a matching search_keyword field) are actually retrieved
    # here in the first place, instead of being invisible to the query
    # before the Python-side check below even gets a chance to run.
    or_conditions = [{field: {"$in": keyword_list}} for field in _KEYWORD_FIELD_CANDIDATES]
    escaped_keywords = [re.escape(k) for k in keyword_list if k]
    if escaped_keywords:
        combined_pattern = "|".join(escaped_keywords)
        for field in _TITLE_FIELD_CANDIDATES + _TEXT_FIELD_CANDIDATES:
            or_conditions.append({field: {"$regex": combined_pattern, "$options": "i"}})
    mongo_query = {"$or": or_conditions}

    # Fetch a larger pool than `limit` since matches are now filtered
    # further (per-platform caps below), same spirit as the old `limit *
    # 5` headroom, just bumped up a bit since the query itself is now
    # broader too.
    raw_docs = list(
        signals_collection.find(mongo_query, {"_id": 0})
        .sort("created_utc", -1)
        .limit(limit * 10)
    )

    matched = []
    seen_urls = set()
    platform_counts = {}  # (v7, NEW) per-platform running count for this call

    for doc in raw_docs:
        title     = _first_present(doc, _TITLE_FIELD_CANDIDATES)
        post_text = _first_present(doc, _TEXT_FIELD_CANDIDATES)

        # (v7) A signal matches if EITHER its search_keyword field matches
        # (unchanged, exact match), OR the keyword shows up inside its own
        # title, OR the keyword shows up inside its own post_text. Any one
        # of the three is enough.
        is_match = (
            _signal_keyword_matches(doc, keyword_set)
            or _text_matches_keyword(title, keyword_set)
            or _text_matches_keyword(post_text, keyword_set)
        )
        if not is_match:
            continue
        if not _signal_platform_matches(doc, targeting_platform):
            continue

        post_url  = _first_present(doc, _URL_FIELD_CANDIDATES)
        platform  = _first_present(doc, _PLATFORM_FIELD_CANDIDATES) or _infer_platform_from_url(post_url)

        if not title and not post_text and not post_url:
            continue
        if post_url and post_url in seen_urls:
            continue

        # (v7, NEW) Per-platform cap: once a platform has already
        # contributed MAX_POSTS_PER_PLATFORM matches to this call, skip
        # any further matches from that same platform (but keep scanning
        # raw_docs — a different platform may still have room).
        platform_key = (platform or "unknown").strip().lower()
        if platform_counts.get(platform_key, 0) >= MAX_POSTS_PER_PLATFORM:
            continue

        if post_url:
            seen_urls.add(post_url)

        matched.append({"title": title, "post_text": post_text, "post_url": post_url, "platform": platform})
        platform_counts[platform_key] = platform_counts.get(platform_key, 0) + 1

        if len(matched) >= limit:
            break

    return matched


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS LAYER (v4)
#
# Matched signals never get dumped to the user directly. They're handed to
# Claude (title + text ONLY — never post_url, never platform, never job
# internals) together with the user's actual chat prompt, and Claude
# decides how to answer — a summary, a sentiment read, a drafted reply, an
# opinion, "not enough data", a markdown table if that's clearest, etc.
#
# Only the FINAL ANSWER TEXT is ever stored (see save_claude_answer_to_chat
# below) — the input posts are never re-saved next to it, since they
# already live in flintel_signals / are reconstructable from the message's
# own keyword list. That's the cost-saving rule: store output only.
#
# UNCHANGED IN v5/v6/v7 — this whole section is exactly as it was in v4.
# The v5 router decides WHETHER a message even reaches this layer; the v6
# response-timeout fallback (see _fill_in_message_outputs) is the only
# other CALLER of analyze_with_claude() added on top — it reuses this
# function completely as-is, just with an empty post list. v7 only
# changed WHICH signals get_matched_signals() returns upstream of this;
# nothing here changed as a result.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_ANALYSIS_SYSTEM_PROMPT = """
You are the AI assistant inside Flintel, a social listening platform.
You work like Claude or ChatGPT — the user can ask you anything, not just
"give me a summary." Answer naturally, the way you would in any normal
conversation.
For the topic the user searched for, you have real posts pulled from
Reddit and X/Twitter as context — each one given to you as just its
title and text (nothing else). Use this data whenever it's relevant to
what the user is asking.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOU CAN BE ASKED LITERALLY ANYTHING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The user might ask for a sentiment breakdown, a quick summary, a specific
post, a comparison to a competitor, an opinion, advice on how to respond
to the criticism, or something that has nothing to do with the posts at
all. Read the actual question and answer it directly — don't force every
reply into a "summary + sentiment" shape just because that's common. Some
examples of the range you should handle naturally:
— "What's the sentiment on Nike?" → give an overview, with sentiment
  (Positive/Negative/Mixed) on the posts you call out.
— "Just give me the top 3 complaints" → three bullets, nothing else.
— "Is this getting better or worse?" → focus on direction and why.
— "Draft a reply to that Reddit thread" → write the reply, not an analysis.
— "What do you think we should do about this?" → give your actual opinion.
— A question unrelated to the posts entirely → answer it like any capable
  assistant would, using your general knowledge — you're not limited to
  only discussing the fetched posts.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GROUNDING — WHEN YOU'RE TALKING ABOUT THE POSTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Whenever your answer draws on the fetched posts specifically:
— Only state what the posts actually say — never invent numbers,
  percentages, dates, quotes, or posts that weren't provided to you.
— You do NOT have links/URLs for these posts — never write, guess, or
  fabricate a URL for any post. If the user asks for a link, say the
  link will be shown separately alongside your answer, not to invent one.
— If the provided posts are too few or too vague to answer what was
  asked, say that plainly instead of padding the answer or making
  something up.
— It's fine to also bring in your own general knowledge alongside the
  posts (e.g. background on a competitor, general context) — just make
  clear what's coming from the actual fetched data versus what you
  already know.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TONE AND FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Write like a sharp, honest person talking to someone who asked a real
question — not like a report generator. Plain language, short paragraphs
or bullets only where they actually help. No JSON, no code blocks, no
rigid template. Don't mention that you're an AI, that this is a "mock",
or narrate your own reasoning process — just answer.
"""

# Cheap "map" step used only when a topic has enough matched posts that
# sending them all in one shot would be wasteful/risky context-wise. Each
# chunk gets condensed down to only the points relevant to the user's
# question before the final Haiku call ever sees them.
CLAUDE_MAP_STEP_SYSTEM_PROMPT = """
You are helping analyze one batch of social media posts (Reddit/X) about a
topic, as a pre-processing step before another AI writes the actual answer.
Read the user's question and the posts below (title + text only). Pull out
ONLY the grounded points from THESE posts that are relevant to the user's
question — specific complaints, praise, themes, or facts actually present
in the text. Do not invent anything not present in the posts. Do not answer
the user's question yet — just list the relevant grounded points as short
bullets. If nothing in these posts is relevant to the question, say exactly:
"No relevant points in this batch." Never include, guess, or reference a
post URL — you were not given any.
"""


def build_claude_post_context(matched_signals: list) -> list:
    """Strips a matched-signals list (which has title/post_text/post_url/
    platform, used for post cards) down to ONLY title + text — this is the
    single point where post_url and platform are dropped before anything
    is sent to Claude. Skips a post entirely if it has neither a title nor
    any text to offer."""
    posts = []
    for m in matched_signals or []:
        title = (m.get("title") or "").strip()
        text  = (m.get("post_text") or "").strip()
        if not title and not text:
            continue
        posts.append({"title": title, "text": text})
    return posts


def chunk_list(items: list, chunk_size: int) -> list:
    """Generic chunker: splits `items` into consecutive batches of at most
    `chunk_size`. Used to keep each Claude call's context small and cheap
    regardless of how many posts a topic matched."""
    if not items:
        return []
    if chunk_size <= 0:
        return [items]
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def _format_posts_block(posts: list) -> str:
    lines = []
    for i, p in enumerate(posts, 1):
        title = p.get("title") or "(no title)"
        text = p.get("text") or "(no text)"
        lines.append(f"[Post {i}]\nTitle: {title}\nText: {text}")
    return "\n\n".join(lines)


def _call_claude(system_prompt: str, user_message: str, max_tokens: int = None) -> str:
    """Single call to the Anthropic Messages API. Raises on any failure —
    callers decide how to degrade gracefully (never let this block the
    search job or the post cards, which don't depend on Claude at all)."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens or CLAUDE_MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": CLAUDE_API_VERSION,
        "content-type": "application/json",
    }

    with httpx.Client(timeout=CLAUDE_TIMEOUT_SECONDS) as http_client:
        response = http_client.post(CLAUDE_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    text_blocks = [
        block.get("text", "") for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    return "\n".join(t for t in text_blocks if t).strip()


def _map_chunk(query: str, posts_chunk: list) -> str:
    posts_block = _format_posts_block(posts_chunk)
    user_message = f"User's question: {query}\n\nPosts:\n{posts_block}"
    return _call_claude(CLAUDE_MAP_STEP_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_MAP_MAX_TOKENS)


def analyze_with_claude(query: str, matched_signals: list) -> str:
    """Turns (user question + matched signals) into the actual answer the
    user sees, using CLAUDE_ANALYSIS_SYSTEM_PROMPT. Handles three cases:

      1. No usable posts at all -> Claude still answers, told plainly that
         there's no post data yet, per the system prompt's own grounding
         rule. (v6: this is the exact branch the response-timeout fallback
         in _fill_in_message_outputs() relies on — it calls this function
         with an empty list purely to reach this case.)
      2. Few enough posts to fit one call -> single direct call.
      3. Enough posts that chunking is worth it -> map step condenses each
         chunk of CLAUDE_POSTS_PER_CHUNK posts down to grounded notes,
         then one reduce call (still using the main system prompt) turns
         all the notes + the question into the final answer.

    Returns the final answer text only — this is the only thing callers
    should persist (see save_claude_answer_to_chat)."""
    posts = build_claude_post_context(matched_signals)

    if not posts:
        user_message = (
            f"User's question: {query}\n\n"
            "No posts were found for this topic yet — you have no post data "
            "to ground an answer in. Say that plainly, then answer anything "
            "else in the question you still can from general knowledge."
        )
        return _call_claude(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)

    chunks = chunk_list(posts, CLAUDE_POSTS_PER_CHUNK)

    if len(chunks) <= 1:
        posts_block = _format_posts_block(posts)
        user_message = f"User's question: {query}\n\nPosts (title + text only):\n{posts_block}"
        return _call_claude(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)

    # Multiple chunks -> map-reduce so no single call has to swallow every
    # matched post at once.
    notes = []
    for chunk in chunks:
        try:
            note = _map_chunk(query, chunk)
        except Exception as exc:
            log.warning(f"Claude map-step failed for a chunk (skipping chunk): {exc}")
            continue
        if note:
            notes.append(note)

    combined_notes = "\n\n---\n\n".join(notes) if notes else "(no grounded points extracted)"
    user_message = (
        f"User's question: {query}\n\n"
        f"Below are grounded notes already condensed from {len(posts)} posts "
        f"(title + text only), split into batches. Treat these notes as your "
        f"only factual grounding about the posts, and answer the user's "
        f"actual question naturally.\n\nNotes:\n{combined_notes}"
    )
    return _call_claude(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ROUTING LAYER (v5, extended in v6 with abuse/harm blocking)
#
# Runs BEFORE anything else in POST /search. A single cheap Claude call
# decides whether the user's message is a genuine "search" (wants social-
# listening data pulled about a brand/product/topic — the v1-v4 pipeline
# above should run exactly as it always has), a plain "chat" message (a
# greeting, small talk, a general question, a follow-up about something
# already discussed, etc. — nothing should be queued into
# flintel_search_jobs for it at all), or (v6, NEW) "blocked" — abusive,
# harassing, hateful, sexually explicit, or threatening content, which
# should neither be searched for nor answered normally, just declined.
#
# When it's "chat" or "blocked", the SAME call also writes the reply
# directly (one round trip instead of two). The reply is grounded only in
# the short, plain-Python chat summary below — never the raw matched
# posts, and never flintel_signals at all, since neither message type
# triggers any signal matching.
#
# Safety rule: ANY failure here (bad JSON, API error, timeout, missing
# key) defaults to {"intent": "search"} so the pre-existing pipeline is
# always the fallback — this routing layer can only ever add a shortcut
# (a direct chat reply, or a polite decline), it can never silently
# swallow a real search request. This also means the v6 abuse-blocking
# check is a best-effort courtesy layer, not a guaranteed content filter:
# on a routing failure, an abusive message falls through to the normal
# search pipeline exactly like any other message would, same as v5.
#
# UNCHANGED IN v7 — this entire section is untouched.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_ROUTER_SYSTEM_PROMPT = """
You are the routing brain inside Flintel, a social-listening platform.
Every message a user types goes through you FIRST, before anything else
happens in the product.
Your ONLY job: classify this message into exactly one of three types:

1. "search" — the message is asking Flintel to research/monitor/pull
   social-media data about a brand, product, company, person, or topic.
   A brand-new social-listening job is about to be queued for whatever
   the user typed. Do NOT try to answer it yourself — just classify it.

2. "chat" — a normal conversational message that doesn't need any new
   data pulled at all: greetings ("hi", "hello", "what's up", "kia chal
   raha hai aj kal"), small talk, thanks, general knowledge questions, a
   follow-up question about something already discussed in this
   conversation, or a request to just talk. Answer the user's message
   yourself, directly and naturally, the way Claude/ChatGPT would in any
   normal conversation.

3. "blocked" — the message is abusive, harassing, hateful, sexually
   explicit, threatening, or otherwise harmful (directed at you, at a
   person, or at any group). Do not search for it and do not answer it
   normally. Instead write a short, calm, firm decline as the reply —
   don't lecture, don't repeat or quote the harmful content back, don't
   moralize at length, just briefly decline and invite them to ask
   something else.

A short, auto-summarized conversation history (may be empty) is given
below for continuity when classifying and when writing a "chat" or
"blocked" reply. Keep any reply conversational and plain — don't mention
you're an AI or that this is a "mock", and don't narrate your own
reasoning.
Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in EXACTLY one of these three shapes:
{"intent": "search", "reply": null}
{"intent": "chat", "reply": "<your natural reply text here>"}
{"intent": "blocked", "reply": "<short, polite decline text>"}
"""

CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT = """
You are the AI assistant inside Flintel, a social listening platform.
Answer the user's message naturally and directly, the way Claude or
ChatGPT would in any normal conversation. Plain language, no rigid
template, no JSON, no code blocks. Don't mention you're an AI or that
this is a "mock".
"""

# (v6) Safety-net text used only if the router itself flagged a message as
# "blocked" but, for whatever reason, didn't return usable reply text —
# never re-sent to Claude (no extra call, and no reason to hand harmful
# content to another prompt just to get a decline message).
CLAUDE_BLOCKED_FALLBACK_REPLY = (
    "I can't help with that one. Happy to help you look into a brand, "
    "product, or topic instead, or just chat about something else."
)


def _parse_router_json(raw: str):
    """Best-effort JSON parse of the router's output — strips ```json
    fences if Claude added them anyway, and validates the shape. Returns
    None on anything unexpected so the caller falls back to the safe
    "search" default instead of ever guessing.

    (v6) Now also accepts "blocked" alongside "search"/"chat" — same
    validation rule as "chat": a reply is expected as a string, and if
    it isn't one, the caller's fallback text is used instead."""
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    if intent not in ("search", "chat", "blocked"):
        return None
    reply = data.get("reply")
    if intent in ("chat", "blocked") and not isinstance(reply, str):
        reply = None
    return {"intent": intent, "reply": reply}


def classify_and_maybe_chat(query: str, chat_summary: str) -> dict:
    """(v5, extended in v6) Single cheap Claude call that classifies the
    user's message as "search", "chat", or "blocked" (v6) and, for the
    latter two, writes the reply in the same call — saves a second round
    trip versus classifying then answering/declining separately. Falls
    back to {"intent": "search", "reply": None} on ANY failure (API
    error, timeout, bad JSON) so the pre-existing search pipeline is
    always the safe default — only the chat-reply/abuse-blocking
    shortcuts can ever be skipped by a routing hiccup, never a genuine
    search request."""
    user_message = (
        f"Conversation so far (auto-summarized, may be empty):\n"
        f"{chat_summary or '(no earlier messages in this chat)'}\n\n"
        f"User's new message: {query}"
    )
    try:
        raw = _call_claude(CLAUDE_ROUTER_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_ROUTER_MAX_TOKENS)
    except Exception as exc:
        log.warning(f"Router Claude call failed (defaulting to 'search'): {exc}")
        return {"intent": "search", "reply": None}

    parsed = _parse_router_json(raw)
    if not parsed:
        log.warning(f"Router returned unparseable output (defaulting to 'search'): {raw[:200]!r}")
        return {"intent": "search", "reply": None}
    return parsed


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def append_to_chat_summary(chat_id: str, owner_key: str, query: str, answer: str):
    """(v5) Keeps a short, PLAIN-PYTHON (no extra Claude call) running
    summary on the chat doc itself — one condensed line per turn. This is
    what classify_and_maybe_chat() reads for continuity, so passing
    conversation context to Claude stays cheap and small no matter how
    long a chat gets — Claude is never sent the full raw message history,
    only this rolling, auto-generated summary. Keeps only the last
    CHAT_SUMMARY_MAX_TURNS lines; older lines roll off automatically.
    Best-effort: never allowed to raise past its caller."""
    if not chat_id or not owner_key:
        return
    line = f"User: {_trim(query, CHAT_SUMMARY_TURN_CHAR_LIMIT)} | Assistant: {_trim(answer, CHAT_SUMMARY_TURN_CHAR_LIMIT)}"

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


# ─────────────────────────────────────────────────────────────────────────────
# USER ACCOUNTS — v2 (Google OAuth + email/password, `flintel_users`)
# ─────────────────────────────────────────────────────────────────────────────

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


def upsert_google_user(google_id: str, email: str, name: str):
    """Logs in an existing Google user, links Google to a matching
    email/password account, or creates a brand-new account."""
    now = datetime.now(timezone.utc)

    user = users_collection.find_one({"google_id": google_id})
    if user:
        users_collection.update_one({"_id": user["_id"]}, {"$set": {"last_login_at": now}})
        return users_collection.find_one({"_id": user["_id"]})

    # Same email already registered manually -> link the Google identity
    # to that existing account instead of creating a duplicate.
    existing = users_collection.find_one({"email": email})
    if existing:
        users_collection.update_one(
            {"_id": existing["_id"]},
            {"$set": {"google_id": google_id, "name": name, "provider": "both", "last_login_at": now}},
        )
        return users_collection.find_one({"_id": existing["_id"]})

    result = users_collection.insert_one({
        "email":          email,
        "name":           name,
        "google_id":      google_id,
        "password_hash":  None,
        "provider":       "google",
        "created_at":     now,
        "last_login_at":  now,
    })
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
#                            platform), UNCHANGED from v3, shown as-is.
#       * `claude_answer` -> (v4) Claude's natural-language answer to the
#                            user's own prompt, grounded in those same
#                            matched posts (title + text only). Only this
#                            OUTPUT is stored — the posts fed in as input
#                            are never duplicated here, since they already
#                            live in flintel_signals. (v6: if no posts are
#                            ever matched within RESPONSE_TIMEOUT seconds,
#                            this instead ends up holding a plain "nothing
#                            found on this yet" answer — see
#                            _fill_in_message_outputs below.)
#   - (v5, NEW) A message may instead be `"message_type": "chat"` — a
#     plain conversational turn the v5 router decided didn't need any
#     data pulled at all (including, as of v6, a polite decline for a
#     "blocked" message — same shape, no template changes needed). These
#     have no topic_key/keywords/results, only `query` + `claude_answer`,
#     and never touch flintel_search_jobs or flintel_signals in any way.
#     Messages with no `message_type` (every message from before this
#     update, and every new search-type message) are treated as ordinary
#     search messages, exactly as before.
#   - (v5, NEW) `summary` — a short, plain-Python rolling digest of the
#     chat (see append_to_chat_summary above), used purely to give the
#     router/chat-reply calls cheap continuity without ever sending
#     Claude the full raw message history.
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
# UNCHANGED IN v7 — this entire section is untouched.
# ─────────────────────────────────────────────────────────────────────────────

def get_anon_id(request: Request) -> str:
    """Stable random ID for a guest (not signed in) visitor, stored in
    their session cookie so their chats persist across requests."""
    anon_id = request.session.get("anon_id")
    if not anon_id:
        anon_id = uuid.uuid4().hex
        request.session["anon_id"] = anon_id
    return anon_id


def get_owner(request: Request):
    """Returns (owner_key, owner_type) for whoever is making the request.
    Signed-in users are keyed by email; guests are keyed by their anon
    UUID."""
    user = get_current_user(request)
    if user and user.get("email"):
        return user["email"], "email"
    return get_anon_id(request), "anon"


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
                        keywords: list, targeting_platform: str):
    """Appends a search as a new message in the chat, and auto-titles the
    chat from the very first query if it hasn't been named yet.

    UNCHANGED from v4 — this is only ever called for messages the v5
    router classified as "search". `keywords` is still stored on the
    message (unchanged from before) so later matching/debugging can use
    it — but note it is purely a behind-the-scenes field: nothing in this
    service renders it back to the user on the chat surface. `results`
    starts empty and gets filled in later by save_signal_results_to_chat()
    once matching signals show up. `claude_answer` starts empty too and
    is filled in once by save_claude_answer_to_chat() the first time
    Claude has posts to work with (or, as of v6, once RESPONSE_TIMEOUT
    seconds pass with none found) — after that it's cached and never
    regenerated for this message."""
    now = datetime.now(timezone.utc)
    message = {
        "query":              query,
        "topic_key":          topic_key,
        "keywords":           keywords,
        "targeting_platform": targeting_platform,
        "requested_at":       now,
        "results":            [],   # filled in later: [{title, post_text, post_url, platform}, ...]
        "claude_answer":      None, # filled in once: Claude's answer text, grounded in `results`
    }

    chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key})
    update = {"$push": {"messages": message}, "$set": {"updated_at": now}}
    if chat and not chat.get("messages"):
        update["$set"]["title"] = generate_chat_title(query)

    chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key}, update)


def add_chat_message_to_chat(chat_id: str, owner_key: str, query: str, answer: str):
    """(v5, NEW) Appends a plain conversational turn — NOT a search — to
    the chat. No topic_key/keywords/targeting_platform, no
    flintel_search_jobs entry, no post-card results, no signal matching
    ever happens for these. This is for messages classify_and_maybe_chat()
    decided were just normal chat (greetings, small talk, general
    questions, follow-ups, etc.), or (v6) a polite decline for a
    "blocked" message — both are saved with the exact same shape, since
    both render identically (query + answer text, no post cards), so no
    template changes are needed for the v6 abuse-blocking addition.

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
        "requested_at":       now,
        "results":            [],
        "claude_answer":      answer,
        "message_type":       "chat",
    }

    chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key})
    update = {"$push": {"messages": message}, "$set": {"updated_at": now}}
    if chat and not chat.get("messages"):
        update["$set"]["title"] = generate_chat_title(query)

    chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key}, update)


def save_signal_results_to_chat(chat_id: str, owner_key: str, topic_key: str, results: list):
    """Best-effort: writes the matched title/post_text/post_url/platform
    output onto the SAME chat message that holds the original user prompt
    for this topic, exactly as computed by get_matched_signals() — no
    keyword list, job status, or anything else about the job is written
    here. This is the post-cards data, unchanged from v3 in shape (v7
    only changes WHICH signals get_matched_signals() returns upstream —
    this function itself is untouched).

    Safe to call repeatedly (e.g. on every chat/home page load while the
    background job is still filling in signals) — it just overwrites
    `results` with the latest matched set for that message."""
    if not results:
        return
    chats_collection.update_one(
        {"chat_id": chat_id, "owner_key": owner_key, "messages.topic_key": topic_key},
        {"$set": {
            "messages.$.results": results,
            "updated_at": datetime.now(timezone.utc),
        }},
    )


def save_claude_answer_to_chat(chat_id: str, owner_key: str, topic_key: str, answer: str):
    """(v4) Writes ONLY Claude's final answer text onto the same chat
    message — never the posts that were sent in as input, since those
    already live in flintel_signals and don't need duplicating here. This
    is what makes re-opening a chat later show the exact same answer
    again, as-is, with no need to re-call Claude. (v6: also used to cache
    the plain "nothing found yet" timeout answer — same function, same
    caching behavior, no changes needed here.)"""
    if not answer:
        return
    chats_collection.update_one(
        {"chat_id": chat_id, "owner_key": owner_key, "messages.topic_key": topic_key},
        {"$set": {
            "messages.$.claude_answer": answer,
            "updated_at": datetime.now(timezone.utc),
        }},
    )


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


def _fill_in_message_outputs(chat_id: str, owner_key: str, messages: list):
    """Shared by home() and view_chat(): for any SEARCH-type message in
    `messages` that's still missing its post-card `results` and/or its
    `claude_answer`, looks up matching signals ONCE and uses that single
    lookup for both:
      - post cards keep working exactly like v3 (results saved as-is), and
      - Claude only gets called (and only gets billed) the first time real
        matched posts are actually available for that message, then the
        answer is cached forever after (only the answer, not the posts).
    Best-effort per message — one message failing must never block the
    rest of the page, and Claude failures must never affect post cards.

    (v5, NEW) Messages with no topic_key are plain chat-type turns (routed
    away from the search pipeline entirely back in /search, and already
    fully answered at that time) — skipped here immediately, since there
    is nothing to fill in for them and they were never meant to touch
    flintel_signals at all.

    (v6, NEW) If a search-type message STILL has no matched posts, it no
    longer just waits forever: once RESPONSE_TIMEOUT seconds have passed
    since the message's own `requested_at`, this calls
    analyze_with_claude(query, []) exactly once — reusing that function's
    existing "no posts yet, say so plainly" branch unchanged — so the
    user gets a natural "sorry, nothing found on this yet" answer instead
    of a permanently blank turn. That answer is cached the same way every
    other answer in this file is, so it's generated (and billed) once.

    UNCHANGED IN v7 — this function calls get_matched_signals() exactly
    as before; only what THAT function returns is now broader (see v7
    notes on get_matched_signals() above)."""
    for msg in messages or []:
        if not msg.get("topic_key"):
            continue

        needs_results = not msg.get("results")
        needs_answer = not msg.get("claude_answer")
        if not needs_results and not needs_answer:
            continue

        try:
            matched = get_matched_signals(
                msg["topic_key"],
                msg.get("keywords", []),
                targeting_platform=msg.get("targeting_platform", "all"),
            )
        except Exception as exc:
            log.warning(f"Signal matching failed for topic_key={msg.get('topic_key')}: {exc}")
            continue

        if not matched:
            # (v6) Nothing matched yet — before, this silently gave up for
            # this page load and just retried again next time. Now, only
            # once the message has been waiting longer than
            # RESPONSE_TIMEOUT seconds, give the user a plain "nothing
            # found" answer instead of leaving the turn blank forever.
            if needs_answer and _elapsed_seconds(msg.get("requested_at")) >= RESPONSE_TIMEOUT:
                try:
                    answer = analyze_with_claude(msg["query"], [])
                    msg["claude_answer"] = answer
                    save_claude_answer_to_chat(chat_id, owner_key, msg["topic_key"], answer)
                    try:
                        append_to_chat_summary(chat_id, owner_key, msg["query"], answer)
                    except Exception as exc:
                        log.warning(f"Updating chat summary failed for topic_key={msg.get('topic_key')}: {exc}")
                except Exception as exc:
                    log.warning(f"Timeout-fallback Claude analysis failed for topic_key={msg.get('topic_key')}: {exc}")
            continue

        if needs_results:
            msg["results"] = matched
            try:
                save_signal_results_to_chat(chat_id, owner_key, msg["topic_key"], matched)
            except Exception as exc:
                log.warning(f"Saving matched results to chat failed for topic_key={msg.get('topic_key')}: {exc}")

        if needs_answer:
            try:
                answer = analyze_with_claude(msg["query"], matched)
                msg["claude_answer"] = answer
                save_claude_answer_to_chat(chat_id, owner_key, msg["topic_key"], answer)
                # (v5) Best-effort: fold this now-answered search turn into
                # the same rolling summary chat-type turns use, so later
                # chat-type replies / routing decisions in this chat can
                # reference it too.
                try:
                    append_to_chat_summary(chat_id, owner_key, msg["query"], answer)
                except Exception as exc:
                    log.warning(f"Updating chat summary failed for topic_key={msg.get('topic_key')}: {exc}")
            except Exception as exc:
                log.warning(f"Claude analysis failed for topic_key={msg.get('topic_key')}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — SEARCH / CHAT
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def home(request: Request):
    chats, chat_id, chat = [], None, None
    try:
        owner_key, _owner_type = get_owner(request)
        chats = get_user_chats(owner_key)
        chat_id = request.session.get("active_chat_id")
        if chat_id:
            # Full chat doc (messages + any results/claude_answer already
            # saved) so index.html's conversation area can render real
            # turns instead of the empty placeholder.
            chat = get_chat_session(chat_id, owner_key)
            if chat and chat.get("messages"):
                _fill_in_message_outputs(chat_id, owner_key, chat["messages"])
    except Exception as exc:
        log.warning(f"Chat lookup failed on home page: {exc}")

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": get_current_user(request),
            "chats": chats,
            "chat_id": chat_id,
            "chat": chat,
        },
    )


@app.post("/search")
def search(
    request: Request,
    query: str = Form(...),
    platform: str = Form("All Platforms"),
    chat_id: str = Form(None),
):
    topic_key = normalize_topic_key(query)
    targeting_platform = normalize_platform(platform)

    if not topic_key:
        # Best-effort context for the error re-render only — never let a
        # chat-lookup hiccup get in the way of showing the validation error.
        chats_safe, chat_id_safe = [], None
        try:
            owner_key, _owner_type = get_owner(request)
            chats_safe = get_user_chats(owner_key)
            chat_id_safe = request.session.get("active_chat_id")
        except Exception as exc:
            log.warning(f"Chat lookup failed while rendering empty-query error: {exc}")

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": "Please enter a search term.",
                "query": query,
                "user": get_current_user(request),
                "chats": chats_safe,
                "chat_id": chat_id_safe,
            },
        )

    # ─────────────────────────────────────────────────────────────────────
    # v5 ROUTING STEP (v6: now also screens for abuse/harm) — runs BEFORE
    # anything else below. Decides whether this message is "search" (the
    # entire v1-v4 pipeline below runs exactly as it always has), "chat"
    # (nothing is generated/patched into flintel_search_jobs at all —
    # Claude just answers directly), or (v6) "blocked" (nothing is
    # generated/patched either — Claude just declines directly).
    #
    # Owner/active-chat resolution + the router call itself are wrapped
    # in one try/except: ANY failure here (corrupt session, Mongo hiccup,
    # Claude API error, bad JSON) falls back to intent="search" with the
    # owner/chat vars left unset, and the search pipeline below re-resolves
    # them itself exactly as it did before this update — so a routing
    # failure can NEVER block or skip a real search job, only the chat-
    # reply/abuse-blocking shortcuts are ever at risk.
    # ─────────────────────────────────────────────────────────────────────
    owner_key = owner_type = None
    active_chat_id = None
    intent = "search"
    chat_reply = None

    try:
        owner_key, owner_type = get_owner(request)
        active_chat_id = chat_id or request.session.get("active_chat_id")
        if not active_chat_id or not get_chat_session(active_chat_id, owner_key):
            active_chat_id = create_chat_session(owner_key, owner_type, title=generate_chat_title(query))
        request.session["active_chat_id"] = active_chat_id

        existing_chat = get_chat_session(active_chat_id, owner_key)
        chat_summary = (existing_chat or {}).get("summary") or ""

        routed = classify_and_maybe_chat(query, chat_summary)
        intent = routed.get("intent", "search")
        chat_reply = routed.get("reply")
    except Exception as exc:
        log.warning(f"v5 routing step failed for query={query!r} (defaulting to normal search pipeline): {exc}")
        intent = "search"

    # ── CHAT-TYPE OR BLOCKED-TYPE MESSAGE: answer/decline directly, ─────
    # ── never touch the fuzzy-keyword / job-queue / signal-matching    ──
    # ── pipeline at all. (v6: "blocked" reuses the exact same handling ──
    # ── as "chat" — same message shape, same redirect — the only       ──
    # ── difference is where the answer text comes from below.)         ──
    if intent in ("chat", "blocked"):
        if intent == "blocked":
            # Never re-sent to Claude for a fallback — a canned decline is
            # enough, and there's no reason to hand harmful content to
            # another prompt just to get a polite "no".
            answer = (chat_reply or "").strip() or CLAUDE_BLOCKED_FALLBACK_REPLY
        else:
            answer = (chat_reply or "").strip()
            if not answer:
                # Router classified this as chat but didn't return usable
                # reply text (e.g. truncated/odd output) — fall back to a
                # second, plain conversational call rather than showing
                # nothing.
                try:
                    answer = _call_claude(CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT, query)
                except Exception as exc:
                    log.warning(f"Chat fallback Claude call failed for query={query!r}: {exc}")
                    answer = "Sorry, I couldn't come up with a reply just now — please try again."

        redirect_chat_id = None
        try:
            if not owner_key:
                owner_key, owner_type = get_owner(request)
            if not active_chat_id or not get_chat_session(active_chat_id, owner_key):
                active_chat_id = create_chat_session(owner_key, owner_type, title=generate_chat_title(query))
            request.session["active_chat_id"] = active_chat_id

            add_chat_message_to_chat(active_chat_id, owner_key, query, answer)
            try:
                append_to_chat_summary(active_chat_id, owner_key, query, answer)
            except Exception as exc:
                log.warning(f"Updating chat summary failed for chat_id={active_chat_id}: {exc}")
            redirect_chat_id = active_chat_id
        except Exception as exc:
            log.warning(f"Saving {intent}-type message failed for query={query!r}: {exc}")

        if redirect_chat_id:
            return RedirectResponse(url=f"/chat/{redirect_chat_id}", status_code=303)
        return RedirectResponse(url="/", status_code=303)

    # ── SEARCH-TYPE MESSAGE: everything below is the v1-v4.3 pipeline, ──
    # ── 100% UNCHANGED — same functions, same order, same behavior.    ──

    # Keyword generation is completely untouched by platform selection, and
    # this ALWAYS runs and enqueues the job — exactly like v1/v2/v3 — no
    # matter what happens with the chat/session bookkeeping (or Claude)
    # below. This is the part Background Service #1 depends on, so it must
    # never be blocked by the chat feature or the Claude analysis layer.
    keywords = generate_fuzzy_keywords(query)
    enqueue_search_job(topic_key, keywords, targeting_platform)

    # Chat/session bookkeeping is best-effort on top of the above: if
    # anything here fails — a stale/corrupt session cookie, a hiccup on the
    # flintel_users_chat collection, a Claude API error, etc. — it must
    # NEVER take down or skip the actual search job that was just queued.
    #
    # v4.1 FIX: track which chat this search actually landed in
    # (`redirect_chat_id`) so the response below can send the browser back
    # to that SAME chat thread instead of always bouncing to "/".
    #
    # (v5) Reuses owner_key/active_chat_id already resolved above by the
    # routing step when available, so the same chat/job land together —
    # but re-resolves them itself if that earlier step didn't run/failed,
    # so this branch never depends on the routing step having succeeded.
    redirect_chat_id = None
    try:
        if not owner_key:
            owner_key, owner_type = get_owner(request)
        active_chat_id = active_chat_id or chat_id or request.session.get("active_chat_id")
        if not active_chat_id or not get_chat_session(active_chat_id, owner_key):
            active_chat_id = create_chat_session(owner_key, owner_type, title=generate_chat_title(query))
        request.session["active_chat_id"] = active_chat_id
        add_search_to_chat(active_chat_id, owner_key, query, topic_key, keywords, targeting_platform)
        redirect_chat_id = active_chat_id
    except Exception as exc:
        log.warning(f"Chat bookkeeping failed for topic_key={topic_key} (job was still queued): {exc}")

    # Stay on the same chat thread — like Claude/ChatGPT keeping you in the
    # conversation you're in, instead of bouncing back to the home screen.
    # Falls back to "/" only if chat bookkeeping itself failed above (so
    # there's no chat_id to redirect to). Background Service #1 still works
    # purely off flintel_search_jobs, so none of this ever affects it.
    if redirect_chat_id:
        return RedirectResponse(url=f"/chat/{redirect_chat_id}", status_code=303)
    return RedirectResponse(url="/", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — CHATS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/chats")
def list_chats(request: Request):
    """Sidebar-style list of every chat belonging to the current owner
    (signed-in email, or guest UUID)."""
    owner_key, _owner_type = get_owner(request)
    return {"chats": get_user_chats(owner_key)}


@app.post("/chats/new")
def new_chat(request: Request, title: str = Form(None)):
    """Starts a brand-new chat and makes it the active one, the same as
    clicking "New chat" in Claude/ChatGPT.

    v4.4 FIX: clicking "New chat" repeatedly used to create a fresh empty
    chat doc every single time, even if the currently active chat was
    ALSO already empty (user hit "New chat" but never actually sent a
    prompt into it yet) — leaving a trail of dead, message-less chats in
    the sidebar. Now, if the currently active chat belongs to this same
    owner and still has zero messages, that same empty chat is reused
    (just re-marked active) instead of creating another one. A brand-new
    chat doc is only created when there's no active chat, it belongs to
    someone else, or it already has at least one message in it — i.e.
    the user actually used it — which matches how Claude/ChatGPT avoid
    piling up empty conversations from repeated "New chat" clicks."""
    owner_key, owner_type = get_owner(request)

    active_chat_id = request.session.get("active_chat_id")
    if active_chat_id:
        active_chat = get_chat_session(active_chat_id, owner_key)
        if active_chat and not active_chat.get("messages"):
            # Already-empty chat for this same owner -> reuse it instead of
            # spawning a duplicate empty one.
            if title:
                chats_collection.update_one(
                    {"chat_id": active_chat_id, "owner_key": owner_key},
                    {"$set": {"title": title, "updated_at": datetime.now(timezone.utc)}},
                )
            request.session["active_chat_id"] = active_chat_id
            return RedirectResponse(url="/", status_code=303)

    chat_id = create_chat_session(owner_key, owner_type, title=title)
    request.session["active_chat_id"] = chat_id
    return RedirectResponse(url="/", status_code=303)


@app.get("/chat/{chat_id}")
def view_chat(request: Request, chat_id: str):
    """Opens a specific past chat and makes it active again — this is how
    a returning user (or a user who just logged back in with their email)
    gets the same chat back, including any previously matched post cards
    AND Claude's previously generated answer, exactly as saved.

    Note for the template: render each message's `query`, `results`
    (title, post_text, post_url, platform) as post cards, and
    `claude_answer` as the actual answer text (markdown-ish plain text —
    render it as-is, Claude writes its own paragraphs/bullets/tables
    inline when it decides that's clearest). `keywords` stays on the
    message purely for internal use and should not be displayed here.
    (v5) A message with `"message_type": "chat"` has no `results` to show
    (it's always an empty list) — just render `query` + `claude_answer`
    like a normal conversational turn, with no post cards underneath.
    (v6) A polite "blocked" decline and a v6 timeout "nothing found yet"
    answer both use this exact same rendering path already — no template
    changes needed for either."""
    owner_key, _owner_type = get_owner(request)
    chat = get_chat_session(chat_id, owner_key)
    if not chat:
        return RedirectResponse(url="/", status_code=303)

    request.session["active_chat_id"] = chat_id

    # Same best-effort fill-in as home(): compute post cards + Claude's
    # answer for any SEARCH-type message that doesn't have them yet, so
    # opening a chat straight from the sidebar shows output immediately
    # instead of only after a home-page visit. Chat-type messages are
    # skipped inside _fill_in_message_outputs itself (nothing to fill in).
    if chat.get("messages"):
        _fill_in_message_outputs(chat_id, owner_key, chat["messages"])

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user": get_current_user(request),
            "chat": chat,
            "chats": get_user_chats(owner_key),
        },
    )


@app.post("/chat/{chat_id}/delete")
def delete_chat(request: Request, chat_id: str):
    """(v4.3) Deletes exactly ONE chat belonging to the current owner —
    never every chat for that owner, and never a chat belonging to a
    different owner (signed-in email or guest UUID).

    Scoping works exactly like get_chat_session()/view_chat() above: the
    delete is filtered on BOTH chat_id AND owner_key at the database
    level (see delete_chat_session()), not just checked afterwards — so a
    guest or another account can never delete a chat by guessing its id,
    the same guarantee already relied on for reading a chat.

    If the deleted chat was the currently active one, `active_chat_id` is
    cleared from the session so home() doesn't try to keep rendering a
    chat that no longer exists. Sidebar/history for every other chat
    belonging to this owner is completely untouched — this route never
    touches any chat_id other than the one passed in."""
    owner_key, _owner_type = get_owner(request)
    deleted = delete_chat_session(chat_id, owner_key)

    if deleted and request.session.get("active_chat_id") == chat_id:
        request.session.pop("active_chat_id", None)

    return RedirectResponse(url="/", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — AUTH: GOOGLE OAUTH
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/auth/google/login")
async def google_login(request: Request):
    redirect_uri = request.url_for("google_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback")
async def google_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:
        log.warning(f"Google OAuth callback failed: {exc}")
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Google sign-in failed. Please try again.", "user": None},
        )

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.google.parse_id_token(request, token)

    google_id = userinfo["sub"]
    email = userinfo["email"]
    name = userinfo.get("name") or email

    # Capture the guest UUID (if any) BEFORE login overwrites how get_owner()
    # resolves this request, so we can migrate any guest chats onto the
    # account being signed into — same as v2's existing linking behavior,
    # just extended to chat history too.
    anon_id = request.session.get("anon_id")

    user_doc = upsert_google_user(google_id=google_id, email=email, name=name)
    _log_user_in(request, user_doc)
    try:
        migrate_anon_chats_to_owner(anon_id, email)
    except Exception as exc:
        log.warning(f"Guest chat migration failed for {email} (sign-in still succeeded): {exc}")
    log.info(f"Google sign-in | email={email}")

    return RedirectResponse(url="/")


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — AUTH: EMAIL + PASSWORD
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/signup")
def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    email_norm = email.strip().lower()

    if password != confirm_password:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Passwords do not match.", "user": None},
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Password must be at least 8 characters.", "user": None},
        )

    if users_collection.find_one({"email": email_norm}):
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "An account with that email already exists.", "user": None},
        )

    anon_id = request.session.get("anon_id")

    password_hash = pwd_context.hash(password)
    user_doc = create_email_user(email_norm, password_hash)
    _log_user_in(request, user_doc)
    try:
        migrate_anon_chats_to_owner(anon_id, email_norm)
    except Exception as exc:
        log.warning(f"Guest chat migration failed for {email_norm} (signup still succeeded): {exc}")
    log.info(f"New email signup | email={email_norm}")

    return RedirectResponse(url="/", status_code=303)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email_norm = email.strip().lower()
    user = users_collection.find_one({"email": email_norm})

    if not user or not user.get("password_hash") or not pwd_context.verify(password, user["password_hash"]):
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Invalid email or password.", "query": None, "user": None},
        )

    anon_id = request.session.get("anon_id")

    users_collection.update_one(
        {"_id": user["_id"]}, {"$set": {"last_login_at": datetime.now(timezone.utc)}}
    )
    _log_user_in(request, user)
    try:
        migrate_anon_chats_to_owner(anon_id, email_norm)
    except Exception as exc:
        log.warning(f"Guest chat migration failed for {email_norm} (login still succeeded): {exc}")
    log.info(f"Email login | email={email_norm}")

    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    # Clearing the session only drops the login cookie and the guest UUID
    # for THIS browser session — nothing is deleted from Mongo. Because
    # chats are stored keyed by email, logging back in with the same email
    # looks the chats up again via get_owner() -> get_user_chats() and
    # they're right where they were, same as Claude/ChatGPT.
    request.session.clear()
    return RedirectResponse(url="/")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("index:app", host="0.0.0.0", port=8080, reload=True)
