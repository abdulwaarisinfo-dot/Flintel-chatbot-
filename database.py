"""
database.py — ALL MongoDB connection and collection setup for Flintel.

Pure extraction from index.py: this module owns the Mongo client, the
database handle, every collection handle, and every index-creation call
that used to live inline in index.py. Zero behavior change from before —
same URI/DB env vars, same collection names, same indexes, same log 
messages on startup.

This file has ZERO dependency on index.py, flintel.py, or
website_intelligence.py — they import FROM this module, never the
reverse.
"""

import os
import logging

from dotenv import load_dotenv
from pymongo import MongoClient

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
busy_owners_collection = db.flintel_busy_owners
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
google_posts_collection = db.flintel_google_posts

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
topic_evidence_cache_collection = db.flintel_topic_evidence_cache

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
