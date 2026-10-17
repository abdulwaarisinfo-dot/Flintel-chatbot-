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

(TRENDING SEARCH JOBS) Before the Google step, the same keywords — trimmed,
de-duplicated case-insensitively and capped at config.SEARCH_WEBSITE_KEYWORDS
(default 200 considered per cycle; 0/negative/invalid = off) — each become
their OWN flintel_search_jobs job via the existing logics.enqueue_search_job()
(topic_key "auto_trending:<normalized keyword>", keywords [that keyword],
targeting_platform TRENDING_JOB_TARGETING_PLATFORM). Keywords that already
have a job are skipped, so an existing job is never updated or reset. The
legacy combined job (topic_key "auto_trending") is no longer written and is
left as it is. This step has its own try/except (and one per keyword): a
failure there is only logged and the Google step still runs. The Google
step receives the generated list exactly as before (the cap only applies
to the jobs). Jobs are written ONLY by a cycle that won the schedule claim:
a fallback cycle (Mongo schedule unavailable, so several workers may run
at once) writes no jobs and still runs the Google step.

FIRE-AND-FORGET: start_trending_keywords_scheduler() schedules an asyncio
background task and returns immediately — it never blocks FastAPI
startup, and the recurring cycle never blocks any incoming request's
response time, since both calls happen in a worker thread
(asyncio.to_thread) rather than on the event loop. This mirrors
_trigger_google_fallback_search()'s own "kick off work on a background
thread/task and don't make the caller wait for it" shape.

PERSISTED SCHEDULE (TRENDING SCHEDULE STATE): the next due time lives in
Mongo (database.auto_keywords_collection = flintel_auto_keywords, one
document _id "trending_keywords"), so a restart/deploy does not reset the
12-hour timer. A worker runs a cycle only after winning ONE atomic
find_one_and_update claim (next_due_at <= now -> next_due_at = now + 12h), so
several workers do not each run it. No saved state yet -> the first cycle is
due immediately. If Mongo cannot be read/claimed, the loop falls back to the
old in-memory behaviour for one cycle (sleep 12h, run, no claim) and then
tries Mongo again; during such an outage several workers CAN each run a
cycle (no coordination without Mongo).

NEVER CRASHES: every failure path (Claude call fails, empty/None
keywords, the Google/RapidAPI call fails, an unexpected exception
anywhere in one cycle) is caught, logged as a warning, and skipped — the
loop always comes back and tries again on the next 12-hour cycle. Nothing
in this file can bring down the app or stop future cycles from running.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import config
import database
from database import google_posts_collection
from google import search_google_for_reddit_posts
from logics import enqueue_search_job, generate_trending_keywords_via_web_search, normalize_topic_key

log = logging.getLogger(__name__)

# How often the trending-keywords chain runs, in seconds (12 hours).
TRENDING_KEYWORDS_INTERVAL_SECONDS = 43200

# Fixed search_keyword_for_storage tag for every stub this scheduler ever
# writes — lets a later reader tell "came from the scheduled trending job"
# apart from "came from an actual user search", per google_posts_collection's
# existing search_keyword field. Never changes.
TRENDING_KEYWORDS_SEARCH_TAG = "auto_trending"

# topic_key prefix of the one-keyword trending jobs ("auto_trending:<normalized keyword>").
# Never equals the legacy combined job's topic_key "auto_trending".
TRENDING_JOB_TOPIC_PREFIX = "auto_trending:"

# targeting_platform of the trending flintel_search_jobs job. "reddit" so
# Background Service #1 does not spend X/Twitter (RapidAPI) calls on ~200
# keywords every cycle. Change this one value to widen it (e.g. "all").
TRENDING_JOB_TARGETING_PLATFORM = "reddit"

# (TRENDING SCHEDULE STATE) persisted-schedule settings
SCHEDULE_STATE_ID = "trending_keywords"        # the one state document's _id
SCHEDULE_MAX_SLEEP_SECONDS = 3600              # re-read the due time at least hourly
SCHEDULE_CLAIM_LOST_RECHECK_SECONDS = 60       # after losing a claim to another worker

# Outcome of the most recent _run_trending_keywords_cycle() call, filled in by
# the cycle itself (status / keywords_count / stubs_count / job_queued) so the
# loop can record it in the state document. Side channel only: the cycle's
# return value is unchanged. Never holds keywords.
_cycle_outcome: dict = {}


def _search_website_keywords_limit() -> int:
    """config.SEARCH_WEBSITE_KEYWORDS read live; 0 when <= 0 or not a number."""
    try:
        limit = int(getattr(config, "SEARCH_WEBSITE_KEYWORDS", 0))
    except (TypeError, ValueError):
        return 0
    return limit if limit > 0 else 0


def _trending_job_keywords(keywords, limit: int) -> list:
    """Strip, drop empties, de-duplicate case-insensitively (first spelling
    and order kept), then cap at `limit`."""
    out, seen = [], set()
    for kw in keywords or []:
        if not isinstance(kw, str):
            continue
        kw = kw.strip()
        if not kw:
            continue
        key = kw.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(kw)
        if len(out) >= limit:
            break
    return out


def _trending_topic_keys(job_keywords: list) -> list:
    """(topic_key, keyword) per keyword. A keyword that normalizes to nothing
    is dropped; when two normalize to the same topic_key the first is kept."""
    out, seen = [], set()
    for kw in job_keywords:
        normalized = normalize_topic_key(kw)
        if not normalized:
            continue
        topic_key = TRENDING_JOB_TOPIC_PREFIX + normalized
        if topic_key in seen:
            continue
        seen.add(topic_key)
        out.append((topic_key, kw))
    return out


def _existing_job_topic_keys(topic_keys: list) -> set:
    """topic_keys that already have a flintel_search_jobs document (one query)."""
    cursor = database.jobs_collection.find({"topic_key": {"$in": topic_keys}}, {"topic_key": 1, "_id": 0})
    return {doc.get("topic_key") for doc in cursor}


async def _queue_trending_search_job(keywords) -> None:
    """Write each new trending keyword as its own search job; keywords that
    already have a job are skipped (never updated or reset).
    Never raises: a failure is logged and the caller carries on."""
    _cycle_outcome["job_queued"] = False
    try:
        limit = _search_website_keywords_limit()
        if limit <= 0:
            # kill-switch: job writing off. Logged (was silent) so a blank/0/invalid
            # SEARCH_WEBSITE_KEYWORDS on the server is visible in the logs.
            log.warning(
                "Trending-keywords scheduler: search job NOT written — "
                f"SEARCH_WEBSITE_KEYWORDS is off/invalid "
                f"(env={os.getenv('SEARCH_WEBSITE_KEYWORDS')!r}); Google search continues."
            )
            return
        job_keywords = _trending_job_keywords(keywords, limit)
        jobs = _trending_topic_keys(job_keywords)
        if not jobs:
            log.warning("Trending-keywords scheduler: search job NOT written — no usable keywords after cleanup.")
            return
        try:
            existing = await asyncio.to_thread(_existing_job_topic_keys, [topic_key for topic_key, _ in jobs])
        except Exception as exc:
            # nothing is written without knowing what exists: never risk resetting a job
            log.warning(
                f"Trending-keywords scheduler: looking up existing search jobs failed "
                f"({type(exc).__name__}); no jobs written this cycle, Google search continues."
            )
            return
        created = skipped = failed = 0
        for topic_key, keyword in jobs:
            if topic_key in existing:
                skipped += 1
                continue
            try:
                await asyncio.to_thread(enqueue_search_job, topic_key, [keyword], TRENDING_JOB_TARGETING_PLATFORM)
                created += 1
            except Exception as exc:
                failed += 1
                # type only: the message could echo document content (keywords)
                log.warning(
                    f"Trending-keywords scheduler: queuing one search job failed "
                    f"({type(exc).__name__}); continuing with the next keyword."
                )
        _cycle_outcome["job_queued"] = created > 0
        log.info(
            f"Trending-keywords scheduler: jobs created={created} "
            f"skipped_existing={skipped} failed={failed}"
        )
    except Exception as exc:
        # type only: the message could echo document content (keywords)
        log.warning(
            f"Trending-keywords scheduler: queuing the search jobs failed "
            f"({type(exc).__name__}); Google search continues."
        )


async def _run_trending_keywords_cycle(write_jobs: bool = True):
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
    too, so a single bad cycle can never kill the scheduler loop below.

    write_jobs=False (fallback cycles, run without the schedule claim):
    no search jobs are written — overlapping uncoordinated cycles could
    otherwise reset a job another cycle just created — and job_queued stays
    False; the Google step still runs."""
    _cycle_outcome.clear()
    try:
        keywords = await asyncio.to_thread(generate_trending_keywords_via_web_search)
        if not keywords:
            _cycle_outcome.update(status="skipped_no_keywords", keywords_count=0)
            log.warning("Trending-keywords scheduler: no keywords generated this cycle — skipping.")
            return
        _cycle_outcome["keywords_count"] = len(keywords)

        # (TRENDING SEARCH JOB) own try/except inside; never raises
        if write_jobs:
            await _queue_trending_search_job(keywords)
        else:
            _cycle_outcome["job_queued"] = False
            log.warning(
                "Trending-keywords scheduler: search jobs NOT written — cycle ran "
                "without the schedule claim (fallback); Google search continues."
            )

        stubs = await asyncio.to_thread(
            search_google_for_reddit_posts,
            keywords,
            google_posts_collection,
            TRENDING_KEYWORDS_SEARCH_TAG,
        )
        _cycle_outcome.update(status="ok", stubs_count=len(stubs) if stubs else 0)
        log.info(
            f"Trending-keywords scheduler: cycle complete | "
            f"keywords={len(keywords)} | stubs={len(stubs) if stubs else 0}"
        )
    except Exception as exc:
        _cycle_outcome["status"] = "failed"
        log.warning(f"Trending-keywords scheduler: cycle failed, will retry next cycle: {exc}")


# ── (TRENDING SCHEDULE STATE) persisted schedule + atomic claim ─────────────
# The sync helpers below talk to Mongo and run via asyncio.to_thread().

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value):
    """Mongo may hand back naive datetimes: they are UTC. None if not a datetime."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _state_collection():
    coll = getattr(database, "auto_keywords_collection", None)
    if coll is None:
        raise RuntimeError("auto_keywords_collection is not available")
    return coll


def _read_schedule_state():
    return _state_collection().find_one({"_id": SCHEDULE_STATE_ID})


def _init_schedule_state(now: datetime) -> None:
    """Create the state document, due immediately. Several workers may race
    here: exactly one insert wins, the others get DuplicateKeyError, which is
    fine — who RUNS the cycle is still decided by the atomic claim."""
    try:
        _state_collection().insert_one({"_id": SCHEDULE_STATE_ID, "next_due_at": now})
    except DuplicateKeyError:
        pass


def _claim_due_cycle(now: datetime):
    """ONE atomic find_one_and_update: matches only while next_due_at <= now
    (or is missing/null) and, in the same operation, moves next_due_at to
    now + interval and marks the cycle running. Returns the updated document
    for the winner, None for everyone else."""
    return _state_collection().find_one_and_update(
        {"_id": SCHEDULE_STATE_ID,
         "$or": [{"next_due_at": {"$lte": now}}, {"next_due_at": None}]},
        {"$set": {
            "next_due_at": now + timedelta(seconds=TRENDING_KEYWORDS_INTERVAL_SECONDS),
            "last_started_at": now,
            "last_status": "running",
        }},
        return_document=ReturnDocument.AFTER,
    )


def _outcome_update(finished_at: datetime) -> dict:
    """$set/$unset for the finished cycle's result (never next_due_at)."""
    out = dict(_cycle_outcome)
    status = out.get("status")
    if status not in ("ok", "skipped_no_keywords", "failed"):
        status = "failed"
    sets = {"last_finished_at": finished_at, "last_status": status}
    unsets = {}
    for key, field in (("keywords_count", "last_keywords_count"),
                       ("stubs_count", "last_stubs_count"),
                       ("job_queued", "last_job_queued")):
        if key in out:
            sets[field] = out[key]
        else:
            unsets[field] = ""
    update = {"$set": sets}
    if unsets:
        update["$unset"] = unsets
    return update


def _finish_claimed_cycle(started_at_raw) -> None:
    """Record the outcome on the document THIS claim started (matched by
    last_started_at, so a slow cycle never overwrites a newer claim)."""
    _state_collection().update_one(
        {"_id": SCHEDULE_STATE_ID, "last_started_at": started_at_raw},
        _outcome_update(_utcnow()),
    )


def _record_fallback_cycle(started_at: datetime) -> None:
    """After an in-memory fallback cycle: if Mongo works again, push
    next_due_at to started_at + interval (only forward, never back) and
    record the outcome, so the recovered schedule does not immediately run
    the cycle a second time."""
    push = started_at + timedelta(seconds=TRENDING_KEYWORDS_INTERVAL_SECONDS)
    update = _outcome_update(_utcnow())
    update["$set"].update({"next_due_at": push, "last_started_at": started_at})
    try:
        _state_collection().update_one(
            {"_id": SCHEDULE_STATE_ID,
             "$or": [{"next_due_at": {"$lt": push}}, {"next_due_at": None}]},
            update,
            upsert=True,
        )
    except DuplicateKeyError:
        pass                     # a newer due time is already stored — keep it


async def _run_claimed_cycle(started_at_raw) -> None:
    await _run_trending_keywords_cycle()
    try:
        await asyncio.to_thread(_finish_claimed_cycle, started_at_raw)
    except Exception as exc:
        log.warning(f"Trending-keywords scheduler: could not record the cycle result "
                    f"({type(exc).__name__}); schedule is unaffected.")


async def _persisted_schedule_step(first_check: bool) -> float:
    """One check of the persisted schedule; returns seconds to wait before
    the next check. Raises on a Mongo read/init/claim error (the loop then
    falls back to the in-memory timer)."""
    now = _utcnow()
    state = await asyncio.to_thread(_read_schedule_state)
    if state is None:
        if first_check:
            log.info("Trending-keywords scheduler started — next cycle due now (no saved state)")
        await asyncio.to_thread(_init_schedule_state, now)
        due = now
    else:
        due = _aware_utc(state.get("next_due_at")) or now
        if first_check:
            log.info(f"Trending-keywords scheduler started — next cycle due at "
                     f"{due.isoformat()} (from flintel_auto_keywords)")
    if due > now:
        return min((due - now).total_seconds(), SCHEDULE_MAX_SLEEP_SECONDS)
    claimed = await asyncio.to_thread(_claim_due_cycle, now)
    if not claimed:
        log.info("Trending-keywords scheduler: cycle already claimed by another worker — "
                 "not running it here, will recheck.")
        return SCHEDULE_CLAIM_LOST_RECHECK_SECONDS
    await _run_claimed_cycle(claimed.get("last_started_at", now))
    return 0


async def _trending_keywords_loop():
    """Runs the trending cycle every TRENDING_KEYWORDS_INTERVAL_SECONDS for
    the lifetime of the app process, on the PERSISTED schedule (see
    _persisted_schedule_step): the due time survives restarts/deploys, a
    cycle runs only in the worker that wins the atomic claim, and the
    loop re-reads the due time at least every SCHEDULE_MAX_SLEEP_SECONDS.
    No saved state yet -> the first cycle runs right away.

    Mongo unavailable (read/claim error): log the exception TYPE only, then
    the old in-memory behaviour for ONE cycle — sleep the full interval, run
    the cycle without a claim and without writing search jobs — record it in
    Mongo if possible, and go back to the persisted schedule. Bounded: at
    most one Mongo attempt per interval while the outage lasts, never a
    tight retry loop. During an outage several workers may each run a
    cycle (Google step only)."""
    first_check = True
    while True:
        try:
            wait = await _persisted_schedule_step(first_check)
        except Exception as exc:
            first_check = False
            log.warning(
                f"Trending-keywords scheduler: Mongo schedule unavailable "
                f"({type(exc).__name__}) — falling back to the in-memory "
                f"{TRENDING_KEYWORDS_INTERVAL_SECONDS}s timer for one cycle; "
                f"Mongo scheduling is retried after it."
            )
            await asyncio.sleep(TRENDING_KEYWORDS_INTERVAL_SECONDS)
            fallback_started = _utcnow()
            await _run_trending_keywords_cycle(write_jobs=False)
            try:
                await asyncio.to_thread(_record_fallback_cycle, fallback_started)
            except Exception as rec_exc:
                log.warning(f"Trending-keywords scheduler: could not record the fallback cycle "
                            f"({type(rec_exc).__name__}).")
            continue
        first_check = False
        if wait > 0:
            await asyncio.sleep(wait)


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
