"""
FLINTEL — WEB SERVICE (v4)
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
    opinion, a plain "not enough data" — whatever fits the question.
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

── v4.1 FIX (this file) ──────────────────────────────────────────────────────
Only ONE behavior changed from the v4 file above: the /search route used to
always `RedirectResponse(url="/")` no matter what, which bounced every
follow-up search back to the home screen instead of keeping the user on the
chat thread they were just talking in (like Claude/ChatGPT do). It now
redirects to `/chat/{chat_id}` — the same chat the message was just saved
into — falling back to "/" only if chat bookkeeping itself failed. Nothing
else in this file was touched.
──────────────────────────────────────────────────────────────────────────────
"""

import os
import re
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
# generation.
MAX_MATCHED_RESULTS = int(os.getenv("MAX_MATCHED_RESULTS", "25"))

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

app = FastAPI(title="Flintel Web Service — v4")
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
# keyword matches one of the keywords generated for this job. UNCHANGED
# from v3 — this is exactly what still powers the post cards (title +
# post_url, as-is). Claude (below) only ever sees title + post_text from
# whatever this returns — never post_url, never platform.
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
    signal doc records more than one matched keyword)."""
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
    """Reads `flintel_signals` and keeps only the signals whose
    search-keyword field matches ANY keyword generated for this job
    (whichever one it is) — regardless of that signal's own topic_key.
    topic_key match is intentionally NOT required: Background Service #1
    may store its own topic_key for a signal, but what decides a match
    here is purely whether search_keyword is one of our generated
    keywords.

    `targeting_platform` (the same "all" | "reddit" | "x_twitter" |
    "linkedin" | "facebook" value already stored on the job/message) is
    applied on top: "all" pulls a match from whichever platform it came
    from, exactly as before; any specific platform restricts matches to
    signals from that platform only — the user's dropdown choice decides
    this, nothing else.

    Returns {title, post_text, post_url, platform} for each match — this
    is the only signal-derived output ever shown to the user (via post
    cards) or persisted onto a chat message's `results` (platform is
    included only so the UI can show which platform a result came from;
    it isn't used for anything else here). Never touches jobs_collection
    or the raw `signals` list returned by get_signals().

    NOTE (v4): this function is completely unchanged from v3. It's also
    the single source of truth Claude's analysis is grounded in — see
    build_claude_post_context() below, which strips post_url/platform
    back out before anything goes to Claude."""
    limit = limit or MAX_MATCHED_RESULTS
    keyword_list = [k for k in (keywords or []) if k]
    keyword_set = {k.strip().lower() for k in keyword_list}
    if not keyword_set:
        return []

    # Query directly on the keyword field(s) with $in for efficiency —
    # no topic_key in the filter at all. _signal_keyword_matches() below
    # re-checks case-insensitively (and handles a list-valued keyword
    # field) so a case/whitespace difference doesn't cause a false miss.
    mongo_query = {"$or": [{field: {"$in": keyword_list}} for field in _KEYWORD_FIELD_CANDIDATES]}

    raw_docs = list(
        signals_collection.find(mongo_query, {"_id": 0})
        .sort("created_utc", -1)
        .limit(limit * 5)
    )

    matched = []
    seen_urls = set()
    for doc in raw_docs:
        if not _signal_keyword_matches(doc, keyword_set):
            continue
        if not _signal_platform_matches(doc, targeting_platform):
            continue

        title     = _first_present(doc, _TITLE_FIELD_CANDIDATES)
        post_text = _first_present(doc, _TEXT_FIELD_CANDIDATES)
        post_url  = _first_present(doc, _URL_FIELD_CANDIDATES)
        platform  = _first_present(doc, _PLATFORM_FIELD_CANDIDATES) or _infer_platform_from_url(post_url)

        if not title and not post_text and not post_url:
            continue
        if post_url and post_url in seen_urls:
            continue
        if post_url:
            seen_urls.add(post_url)

        matched.append({"title": title, "post_text": post_text, "post_url": post_url, "platform": platform})
        if len(matched) >= limit:
            break

    return matched


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS LAYER (v4, NEW)
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
         rule.
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
#       * `claude_answer` -> (v4, NEW) Claude's natural-language answer to
#                            the user's own prompt, grounded in those same
#                            matched posts (title + text only). Only this
#                            OUTPUT is stored — the posts fed in as input
#                            are never duplicated here, since they already
#                            live in flintel_signals.
#   - When an anonymous user signs up / logs in, their guest chats are
#     re-keyed onto their email so nothing is lost.
#   - Because chats are always looked up by owner_key (the email, once
#     signed in), logging out and logging back in with the same email
#     brings the exact same chat history back — nothing is deleted on
#     logout.
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


def add_search_to_chat(chat_id: str, owner_key: str, query: str, topic_key: str,
                        keywords: list, targeting_platform: str):
    """Appends a search as a new message in the chat, and auto-titles the
    chat from the very first query if it hasn't been named yet.

    `keywords` is still stored on the message (unchanged from before) so
    later matching/debugging can use it — but note it is purely a
    behind-the-scenes field: nothing in this service renders it back to
    the user on the chat surface. `results` starts empty and gets filled
    in later by save_signal_results_to_chat() once matching signals show
    up. `claude_answer` (v4) starts empty too and is filled in once by
    save_claude_answer_to_chat() the first time Claude has posts to work
    with — after that it's cached and never regenerated for this message."""
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


def save_signal_results_to_chat(chat_id: str, owner_key: str, topic_key: str, results: list):
    """Best-effort: writes the matched title/post_text/post_url/platform
    output onto the SAME chat message that holds the original user prompt
    for this topic, exactly as computed by get_matched_signals() — no
    keyword list, job status, or anything else about the job is written
    here. This is the post-cards data, unchanged from v3.

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
    again, as-is, with no need to re-call Claude."""
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
    """(v4) Shared by home() and view_chat(): for any message in `messages`
    that's still missing its post-card `results` and/or its
    `claude_answer`, looks up matching signals ONCE and uses that single
    lookup for both:
      - post cards keep working exactly like v3 (results saved as-is), and
      - Claude only gets called (and only gets billed) the first time real
        matched posts are actually available for that message, then the
        answer is cached forever after (only the answer, not the posts).
    Best-effort per message — one message failing must never block the
    rest of the page, and Claude failures must never affect post cards."""
    for msg in messages or []:
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
    # to that SAME chat thread instead of always bouncing to "/". This is
    # the only behavioral change in this whole file.
    redirect_chat_id = None
    try:
        owner_key, owner_type = get_owner(request)
        active_chat_id = chat_id or request.session.get("active_chat_id")
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
    clicking "New chat" in Claude/ChatGPT."""
    owner_key, owner_type = get_owner(request)
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
    message purely for internal use and should not be displayed here."""
    owner_key, _owner_type = get_owner(request)
    chat = get_chat_session(chat_id, owner_key)
    if not chat:
        return RedirectResponse(url="/", status_code=303)

    request.session["active_chat_id"] = chat_id

    # Same best-effort fill-in as home(): compute post cards + Claude's
    # answer for any message that doesn't have them yet, so opening a chat
    # straight from the sidebar shows output immediately instead of only
    # after a home-page visit.
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