"""
database.py — ALL MongoDB connection and collection setup for Flintel.

Pure extraction from index.py: this module owns the Mongo client, the
database handle, every collection handle, and every index-creation call
that used to live inline in index.py. Zero behavior change from before — 
same URI/DB env vars, same collection names, same indexes, same log 
messages on startup. 

(SCAN RESILIENCE) The only addition is build_mongo_client_kwargs(): every
MongoClient below now gets connection/selection (and, for the signals
clients, socket) timeouts from config.py, so a hung query or unreachable
server fails instead of blocking forever. Setting a MONGO_*_TIMEOUT_MS
flag to 0 omits that kwarg (= the old, no-timeout behaviour).

This file has ZERO dependency on index.py, flintel.py, or
website_intelligence.py — they import FROM this module, never the
reverse.
"""

import os
import logging

from dotenv import load_dotenv
from pymongo import MongoClient

import config as _config

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
# Configured here (the first module imported by index.py) so it's set up
# before any of this module's own startup log lines below run.
# logging.basicConfig() is a no-op on any later call from index.py, so
# there's no duplicate-configuration issue either way.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("flintel-db")

# ─────────────────────────────────────────────────────────────────────────────
# ENV / CONFIG — SAME MongoDB as Background Service #1
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB  = os.getenv("MONGODB_DB", "flintel_bot")


# (SCAN RESILIENCE — MONGO TIMEOUTS) Builds the timeout kwargs for a
# MongoClient from config.py's flags. The flags are read live (at call
# time, via the config module), not copied at import. A flag whose value
# is 0, negative, None or not a number is simply left out of the dict, so
# pymongo's own default (the old behaviour) applies for that setting.
#   MONGO_SERVER_SELECTION_TIMEOUT_MS -> serverSelectionTimeoutMS
#   MONGO_CONNECT_TIMEOUT_MS          -> connectTimeoutMS
#   MONGO_SOCKET_TIMEOUT_MS           -> socketTimeoutMS
# include_socket_timeout=False never adds socketTimeoutMS (used for the
# jobs/users/chats cluster, where a long-but-legitimate operation must not
# be cut off by a socket read timeout).
def build_mongo_client_kwargs(include_socket_timeout: bool = True) -> dict:
    mapping = [
        ("serverSelectionTimeoutMS", "MONGO_SERVER_SELECTION_TIMEOUT_MS"),
        ("connectTimeoutMS", "MONGO_CONNECT_TIMEOUT_MS"),
    ]
    if include_socket_timeout:
        mapping.append(("socketTimeoutMS", "MONGO_SOCKET_TIMEOUT_MS"))

    kwargs = {}
    for kwarg_name, flag_name in mapping:
        try:
            value = int(getattr(_config, flag_name, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            kwargs[kwarg_name] = value
    return kwargs


client = MongoClient(MONGODB_URI, **build_mongo_client_kwargs(include_socket_timeout=True))
db = client[MONGODB_DB]

# (MONGODB3 — new primary home for every collection EXCEPT
# flintel_signals) A second, independent MongoClient, built only
# from MONGODB3 (config.py). Unlike MONGODB2 (an optional read-only
# mirror), this one is required: every collection below other than
# signals_collection now lives here.
from config import MONGODB3

# No socketTimeoutMS here: jobs/users/chats/cache operations must not be
# cut off by a socket read timeout — only server selection + connect.
client_3 = MongoClient(MONGODB3, **build_mongo_client_kwargs(include_socket_timeout=False))
db3 = client_3[MONGODB_DB]

jobs_collection    = db3.flintel_search_jobs
signals_collection = db.flintel_signals   # stays on the PRIMARY cluster — unchanged

# (SECONDARY SIGNALS-ONLY MONGO — READ-ONLY MIRROR) A second, completely
# independent MongoClient, built only from MONGODB2 (config.py). This is
# NOT a fallback and NOT used for writes/indexes anywhere — it only ever
# gives a read handle onto that second cluster's flintel_signals
# collection, under the exact same MONGODB_DB name the primary client
# uses. If MONGODB2 isn't set, or the connection fails for any reason,
# signals_collection_2 stays None and the app keeps starting normally —
# this must never be able to crash startup.
from config import MONGODB2

signals_collection_2 = None
if MONGODB2:
    try:
        client_2 = MongoClient(MONGODB2, **build_mongo_client_kwargs(include_socket_timeout=True))
        signals_collection_2 = client_2[MONGODB_DB].flintel_signals
    except Exception as exc:
        log.warning(f"Could not connect secondary MongoDB (MONGODB2): {exc}")
        signals_collection_2 = None

# (QUATERNARY SIGNALS-ONLY MONGO, READ-ONLY MIRROR) A second, completely
# independent MongoClient, built only from MONGODB4 (config.py). This is
# NOT a fallback and NOT used for writes/indexes anywhere — it only ever
# gives a read handle onto that fourth cluster's flintel_signals
# collection, under the exact same MONGODB_DB name the primary client
# uses. If MONGODB4 isn't set, or the connection fails for any reason,
# signals_collection_4 stays None and the app keeps starting normally —
# this must never be able to crash startup.
from config import MONGODB4

signals_collection_4 = None
if MONGODB4:
    try:
        client_4 = MongoClient(MONGODB4, **build_mongo_client_kwargs(include_socket_timeout=True))
        signals_collection_4 = client_4[MONGODB_DB].flintel_signals
    except Exception as exc:
        log.warning(f"Could not connect quaternary MongoDB (MONGODB4): {exc}")
        signals_collection_4 = None

# User accounts (Google OAuth + email/password). — now on MONGODB3
users_collection = db3.flintel_users
users_collection.create_index("email", unique=True, sparse=True)
users_collection.create_index("google_id", unique=True, sparse=True)

# Chat/session memory (Claude/ChatGPT-style conversations). — now on MONGODB3
chats_collection = db3.flintel_users_chat

# (PER-USER BUSY LOCK) A dedicated small collection, keyed by owner_key —
# not an in-memory dict, and not a field bolted onto chats_collection.
# Reasons this fits the existing pattern best:
#   - Every other piece of cross-request state in this file (jobs, chat
#     sessions, users) already lives in its own Mongo collection, keyed
#     by the same owner_key/chat_id fields used everywhere else — this
#     follows that exact convention rather than inventing a new pattern.
#   - It must survive across multiple server processes/workers and a
#     server restart (an in-memory dict would only be visible to whichever
#     single worker process happened to handle a given request, silently
#     failing to block a second request from the same user if it landed
#     on a different worker — a real correctness gap for anything beyond
#     a single-process deployment).
#   - It's independent of which chat the in-flight request belongs to
#     (the busy state is per-OWNER, not per-chat), so it doesn't belong
#     as a field on a specific chat document in chats_collection.
busy_owners_collection = db3.flintel_busy_owners
busy_owners_collection.create_index("owner_key", unique=True)
chats_collection.create_index("chat_id", unique=True)
chats_collection.create_index("owner_key")

# (PERFORMANCE FIX) signals_collection had NO indexes at all — every
# get_matched_signals() call (topic_key lookups and the time/platform-
# filtered unfiltered-mode query) was doing a full collection scan. Adding
# these is purely a speed improvement: it changes no query's results,
# only how fast MongoDB can find them.
#
# (DEPLOYMENT CRASH FIX) create_index() is normally a no-op if an index on
# this field already exists — EXCEPT when one already exists under a
# DIFFERENT name (e.g. a pre-existing "signals_topic_key" index), in which
# case MongoDB raises IndexOptionsConflict instead of silently reusing it.
# The actual goal here was only ever "make sure some index covers this
# field" — if one already exists under any name, that goal is already
# satisfied, so this failure is caught and logged rather than allowed to
# crash the whole app at startup.
try:
    signals_collection.create_index("topic_key")
except Exception as exc:
    log.warning(f"Could not create index on signals_collection.topic_key (likely already exists under a different name): {exc}")
try:
    signals_collection.create_index("created_utc")
except Exception as exc:
    log.warning(f"Could not create index on signals_collection.created_utc (likely already exists under a different name): {exc}")

# (GOOGLE-SEARCH REDDIT-POST DISCOVERY) Lightweight "stub" documents for
# Reddit URLs surfaced by google.py's RapidAPI Google search — real
# content for each URL is fetched later, out of band, by Background
# Service #2 (not part of this repo), which is why this collection only
# ever stores discovery metadata (post_url, discovered_at, search
# keywords, google_rank, subreddit) plus a reddit_fetched flag that
# starts False.
google_posts_collection = db3.flintel_google_posts

try:
    # Prevents duplicate stubs for the same discovered URL across
    # repeated searches/keywords; google.py's upsert relies on this.
    google_posts_collection.create_index("post_url", unique=True)
except Exception as exc:
    log.warning(f"Could not create index on google_posts_collection.post_url (likely already exists under a different name): {exc}")
try:
    # Speeds up get_stub_results_for_keywords() lookups.
    google_posts_collection.create_index("search_keyword")
except Exception as exc:
    log.warning(f"Could not create index on google_posts_collection.search_keyword (likely already exists under a different name): {exc}")
try:
    # Lets Background Service #2 efficiently find pending
    # (reddit_fetched: false) stubs to process.
    google_posts_collection.create_index("reddit_fetched")
except Exception as exc:
    log.warning(f"Could not create index on google_posts_collection.reddit_fetched (likely already exists under a different name): {exc}")
try:
    # Sort-by-recency support, mirrors signals_collection's own
    # created_utc index.
    google_posts_collection.create_index("discovered_at")
except Exception as exc:
    log.warning(f"Could not create index on google_posts_collection.discovered_at (likely already exists under a different name): {exc}")

# (TOPIC EVIDENCE CACHE) Per-chat, per-topic cache of already-matched
# evidence posts — so the same topic doesn't re-query Mongo and re-send
# Claude the same posts on every follow-up. Key is always the
# (chat_id, topic_key) combination — so each chat/user has its own
# cache, never any cross-user mixing.
#
# Document shape:
#   {
#     "chat_id": str,
#     "topic_key": str,
#     "owner_key": str,               # extra safety scoping
#     "posts": [ {title, post_text, post_url, platform}, ... ],
#     "post_urls_seen": [str, ...],   # fast de-dup check on top-up
#     "evidence_count": int,          # len(posts), cached for quick reads
#     "keywords": [str, ...],         # the keywords these posts were matched by
#     "match_phrases": [str, ...] | None,
#     "created_at": datetime,
#     "updated_at": datetime,
#   }
topic_evidence_cache_collection = db3.flintel_topic_evidence_cache

try:
    # One cache row per topic — this is the key an upsert writes against.
    topic_evidence_cache_collection.create_index(
        [("chat_id", 1), ("topic_key", 1)], unique=True
    )
except Exception as exc:
    log.warning(f"Could not create index on topic_evidence_cache_collection.chat_id+topic_key: {exc}")

try:
    # TTL index — old cache entries expire automatically per
    # TOPIC_CACHE_TTL_DAYS (config.py). TTL is expressed in seconds, so
    # the config's day-count is converted here, at index-creation time.
    from config import TOPIC_CACHE_TTL_DAYS
    if TOPIC_CACHE_TTL_DAYS > 0:
        topic_evidence_cache_collection.create_index(
            "updated_at", expireAfterSeconds=TOPIC_CACHE_TTL_DAYS * 86400
        )
except Exception as exc:
    log.warning(f"Could not create TTL index on topic_evidence_cache_collection.updated_at: {exc}")

# (WEBSITE EVIDENCE CACHE) Per-URL cache of already-fetched website
# content + structured business evidence — taake same URL baar baar
# refetch/re-analyze na ho. Global (chat-independent) hai, kyunke website
# ka content khud kisi user-specific cheez ka hissa nahi — ek website ka
# data har chat/user ke liye same hi hoga.
#
# Document shape:
#   {
#     "url": str,                    # normalized URL — unique key
#     "domain": str,
#     "pages_fetched": [str, ...],
#     "combined_text": str,          # multi-page combined, truncated text
#     "structured_evidence": {       # business/products/pricing/etc schema
#         "title": str, "description": str, "business": str,
#         "products_services": [...], "pricing": [...],
#         "target_customer": str, "features": [...],
#         "value_proposition": str, "important_pages": [...],
#         "contact_information": {...}, "faq": [...],
#     },
#     "evidence_quality": str,       # "failed" | "thin" | "strong" | "mixed"
#     "fetched_at": datetime,
#     "updated_at": datetime,
#   }
website_evidence_cache_collection = db3.flintel_website_evidence_cache

try:
    website_evidence_cache_collection.create_index("url", unique=True)
except Exception as exc:
    log.warning(f"Could not create index on website_evidence_cache_collection.url: {exc}")

try:
    from config import WEBSITE_EVIDENCE_CACHE_TTL_DAYS
    if WEBSITE_EVIDENCE_CACHE_TTL_DAYS > 0:
        website_evidence_cache_collection.create_index(
            "updated_at", expireAfterSeconds=WEBSITE_EVIDENCE_CACHE_TTL_DAYS * 86400
        )
except Exception as exc:
    log.warning(f"Could not create TTL index on website_evidence_cache_collection.updated_at: {exc}")

# (TRENDING SCHEDULE STATE) Persisted schedule of scheduler.py's 12-hourly
# trending-keywords cycle, so a restart/deploy does not reset the timer and
# several workers coordinate through one atomic claim. Holds a single small
# document, _id "trending_keywords" (next_due_at, last_started_at,
# last_finished_at, last_status, last_*_count, last_job_queued) — never the
# keyword list. No extra index: the fixed _id is the only lookup key.
auto_keywords_collection = db3.flintel_auto_keywords

# (SEARCH CREDITS) One document per owner (search_credits.py): monthly
# counters plus one entry per request id. Every write is a single-document
# conditional update, so it uses write concern "majority": an acknowledged
# claim or refund must not be rolled back by a failover. The unique owner_key
# index guarantees one document per owner (and so one entry per request id).
# The handle below is client-side only (no server call). The index — which
# also creates the collection on the server — is only created while credit
# enforcement is on: with CREDIT_ENFORCEMENT_DISABLED=true, startup touches
# nothing new in the database.
from pymongo import WriteConcern

credit_usage_collection = db3.get_collection("flintel_credit_usage", write_concern=WriteConcern("majority"))
if not getattr(_config, "CREDIT_ENFORCEMENT_DISABLED", False):
    try:
        credit_usage_collection.create_index("owner_key", unique=True)
    except Exception as exc:
        log.warning(f"Could not create flintel_credit_usage.owner_key index: {exc}")
# (INDEX GUARD) create_index() returning (or its failure being logged) proves nothing: VERIFY that the unique owner_key
# index exists. While it does not, search_credits.py refuses every credit operation that could create a document, charge or
# entitlement (fail closed); reads, refunds and recovery keep working; duplicates are reported, never merged. See index_guard.py.
import index_guard
index_guard.register("flintel_credit_usage", credit_usage_collection,
                     required=not getattr(_config, "CREDIT_ENFORCEMENT_DISABLED", False))

# (PLAN CREDITS) One document per registered email owner with a paid plan
# (search_credits.activate_plan / limit_for), on the same MONGODB3 database and
# with the same majority write concern. Read-only for request handling; only
# activate_plan (not wired to any route yet) writes it. The unique owner_key
# index is what makes activate_plan idempotent under concurrent duplicates. As
# for the credit collection, nothing is created while enforcement is disabled.
plans_collection = db3.get_collection("flintel_plans", write_concern=WriteConcern("majority"))
# (BILLING) The unique owner_key index is also created when Lemon Squeezy billing is configured, so
# the billing writer's one-record-per-owner guarantee does not depend on the credit kill switch.
_billing_configured = all(getattr(_config, _n, "") for _n in (
    "LEMONSQUEEZY_API_KEY", "LEMONSQUEEZY_STORE_ID", "LEMONSQUEEZY_WEBHOOK_SECRET",
    "LEMONSQUEEZY_VARIANT_ID_STANDARD", "LEMONSQUEEZY_VARIANT_ID_BUSINESS"))
# plans_owner_index_ok is False only when the index was needed and could not be built (for example
# duplicate owner_key rows already exist). routes.py then refuses NEW checkouts; webhooks and existing
# subscriptions keep working (billing never discards a paid event), and nothing here modifies or deletes data.
plans_owner_index_ok = True
if _billing_configured or not getattr(_config, "CREDIT_ENFORCEMENT_DISABLED", False):
    try:
        plans_collection.create_index("owner_key", unique=True)
    except Exception as exc:
        plans_owner_index_ok = False
        if _billing_configured:
            log.error(f"BILLING ALERT: could not create the unique flintel_plans.owner_key index ({type(exc).__name__}): "
                      "new checkouts are disabled until duplicate plan records are cleaned up and the index exists")
        else:
            log.warning(f"Could not create flintel_plans.owner_key index: {exc}")
# (INDEX GUARD) verified, not assumed: same required-ness as the creation above. A failed verification also clears
# plans_owner_index_ok (new checkouts are refused) and makes the plan-record writers refuse to create/alter entitlements.
_plans_index_required = _billing_configured or not getattr(_config, "CREDIT_ENFORCEMENT_DISABLED", False)
if index_guard.register("flintel_plans", plans_collection, required=_plans_index_required) not in index_guard.OK_STATES:
    plans_owner_index_ok = False

# (BILLING, billing.py) Lemon Squeezy subscription state and processed webhook deliveries, same
# database and write concern as flintel_plans. Both use the document _id as their unique key
# (subscription id / SHA-256 of the raw delivery body), so no index is needed for idempotency;
# the owner_key index below only speeds up the per-owner lookup. No card data is ever stored.
subscriptions_collection = db3.get_collection("flintel_subscriptions", write_concern=WriteConcern("majority"))
billing_events_collection = db3.get_collection("flintel_billing_events", write_concern=WriteConcern("majority"))
if _billing_configured:
    try:
        subscriptions_collection.create_index("owner_key")
    except Exception as exc:
        log.warning(f"Could not create flintel_subscriptions.owner_key index: {exc}")

# (GUEST GATE, guest_gate.py) One small document per guest identity, keyed by the document _id (the
# guest id), so its atomic reserve / complete / release updates need no extra index. Holds only a
# counter, the in-flight reservation and timestamps — no IP address, e-mail or search text.
guest_gate_collection = db3.get_collection("flintel_guest_gate", write_concern=WriteConcern("majority"))

# (EMAIL SIGN-UP CODES, email_verification.py) One small document per address being verified (_id = the
# normalised address) plus one send counter per client address. Holds only a keyed HASH of the 6-digit code,
# attempt/send counters and timestamps - never the code itself. Documents delete themselves after `expires_at`.
email_codes_collection = db3.get_collection("flintel_email_codes", write_concern=WriteConcern("majority"))
try:
    email_codes_collection.create_index("expires_at", expireAfterSeconds=0)
except Exception as exc:
    log.warning(f"Could not create flintel_email_codes.expires_at TTL index (codes still expire by their own timestamp): {exc}")
