"""
FLINTEL — TRENDING-KEYWORDS BACKGROUND SCHEDULER
============================================================================
Self-contained orchestration module — same one-way-dependency spirit as
google.py/website_intelligence.py: this file imports FROM logics.py,
google.py, and database.py, never the other way around. It owns no
keyword-generation or storage logic of its own; it only calls, in order,
two functions that already exist and already degrade gracefully on their
own:

  1. logics.generate_trending_keywords_via_web_search() — one Claude call
     (with live web search) that returns up to 200 trending keyword
     strings, or None on any failure.
  2. google.search_google_for_reddit_posts(keywords, google_posts_collection,
     search_keyword_for_storage="auto_trending") — exactly as it already
     works for any other caller; the "auto_trending" tag is the ONLY
     thing this file adds, so a stub written by this scheduler can later
     be told apart from one written by a real user search.

WHAT THIS FILE DOES NOT DO: no new keyword-generation logic, no new
storage pattern, no new Mongo fields, no changes to google.py or
logics.py. Purely scheduling + the two calls above, in order.

FIRE-AND-FORGET: start_trending_keywords_scheduler() schedules an asyncio
background task and returns immediately — it never blocks FastAPI
startup, and the recurring cycle never blocks any incoming request's
response time, since both calls happen in a worker thread
(asyncio.to_thread) rather than on the event loop. This mirrors
_trigger_google_fallback_search()'s own "kick off work on a background
thread/task and don't make the caller wait for it" shape.

NEVER CRASHES: every failure path (Claude call fails, empty/None
keywords, the Google/RapidAPI call fails, an unexpected exception
anywhere in one cycle) is caught, logged as a warning, and skipped — the
loop always comes back and tries again on the next 12-hour cycle. Nothing
in this file can bring down the app or stop future cycles from running.
"""

import asyncio
import logging

from database import google_posts_collection
from google import search_google_for_reddit_posts
from logics import generate_trending_keywords_via_web_search

log = logging.getLogger(__name__)

# How often the trending-keywords chain runs, in seconds (12 hours).
TRENDING_KEYWORDS_INTERVAL_SECONDS = 43200

# Fixed search_keyword_for_storage tag for every stub this scheduler ever
# writes — lets a later reader tell "came from the scheduled trending job"
# apart from "came from an actual user search", per google_posts_collection's
# existing search_keyword field. Never changes.
TRENDING_KEYWORDS_SEARCH_TAG = "auto_trending"


async def _run_trending_keywords_cycle():
    """One full cycle of the chain: generate up to 200 trending keywords,
    then hand them as-is to search_google_for_reddit_posts() tagged
    "auto_trending". Both underlying calls are synchronous/blocking
    (httpx, Claude API, Mongo) — run via asyncio.to_thread() so this
    cycle never blocks the event loop (and therefore never blocks any
    concurrent incoming request) while it's in flight.

    Skips the cycle (logs a warning, returns) if the keyword-generation
    step returns None or an empty list — never calls
    search_google_for_reddit_posts() with nothing to search for. Any
    other unexpected exception anywhere in this function is caught here
    too, so a single bad cycle can never kill the scheduler loop below."""
    try:
        keywords = await asyncio.to_thread(generate_trending_keywords_via_web_search)
        if not keywords:
            log.warning("Trending-keywords scheduler: no keywords generated this cycle — skipping.")
            return

        stubs = await asyncio.to_thread(
            search_google_for_reddit_posts,
            keywords,
            google_posts_collection,
            TRENDING_KEYWORDS_SEARCH_TAG,
        )
        log.info(
            f"Trending-keywords scheduler: cycle complete | "
            f"keywords={len(keywords)} | stubs={len(stubs) if stubs else 0}"
        )
    except Exception as exc:
        log.warning(f"Trending-keywords scheduler: cycle failed, will retry next cycle: {exc}")


async def _trending_keywords_loop():
    """Runs _run_trending_keywords_cycle() every
    TRENDING_KEYWORDS_INTERVAL_SECONDS, forever, for the lifetime of the
    app process. Sleeps first, then runs — the first real cycle fires
    12 hours after the app starts, matching "har 12 ghante baad ye chain
    call kare" literally as a recurring 12-hour cadence from startup,
    not an immediate run-once-at-startup. (If an immediate first cycle
    on startup is wanted instead, swap the order of the two lines in this
    loop — nothing else needs to change.)"""
    while True:
        await asyncio.sleep(TRENDING_KEYWORDS_INTERVAL_SECONDS)
        await _run_trending_keywords_cycle()


def start_trending_keywords_scheduler():
    """Call once, at FastAPI startup (see index.py's startup event/
    lifespan hook). Schedules _trending_keywords_loop() as a background
    asyncio task and returns immediately — never awaits it, never blocks
    app startup. Must be called from inside a running event loop (i.e.
    from within an async startup/lifespan function), since
    asyncio.create_task() requires one.

    Never raises: if scheduling the task itself somehow fails (e.g. this
    is called with no running event loop), that failure is caught and
    logged — the app still starts up normally, it just runs without this
    background job, exactly like any other degrade-gracefully failure
    elsewhere in this product."""
    try:
        asyncio.create_task(_trending_keywords_loop())
        log.info(
            f"Trending-keywords scheduler started — cycle every "
            f"{TRENDING_KEYWORDS_INTERVAL_SECONDS}s (12h), tag="
            f"{TRENDING_KEYWORDS_SEARCH_TAG!r}."
        )
    except Exception as exc:
        log.warning(f"Trending-keywords scheduler: could not start: {exc}")
