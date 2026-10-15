"""
FLINTEL — DURABLE FINISHING OF UNFINISHED RESEARCH TURNS
============================================================================
Why this exists: GET /chat/{id} used to run a full posts scan
(get_evidence_with_topup) SYNCHRONOUSLY for every older search turn that
was still missing `results` or `claude_answer`, before the page could be
rendered. A chat with one such turn — typically a research turn the user
left running when they typed "Hello" — made every page load wait for the
whole scan (seconds).

What changed: the page no longer does that work. The work itself is the
same function as before (index._fill_in_message_outputs() for that one
message, with every existing completion, persistence, answer-generation,
source-link, busy-lock and timeout-fallback rule unchanged); only WHERE
and WHEN it runs changed:

  * Durable queue — one small Mongo document per unfinished turn in
    database.turn_finishing_collection (flintel_turn_finishing), keyed
    "<chat_id>|<owner_key>|<topic_key>". Written when a search turn is
    created (index.add_search_to_chat), when a chat page shows an
    unfinished older turn (routes.view_chat), and by a periodic backfill
    that finds unfinished turns nobody enqueued (older data, a failed
    enqueue). Mongo is the source of truth, so a restart or a crashed
    worker loses nothing: the turn itself stays unfinished in the chat
    and its queue document stays until the turn is finished.

  * Lease — a worker processes a turn only after winning ONE atomic
    find_one_and_update claim (due_at <= now -> due_at = now + LEASE,
    lease_owner = <token>). Several requests, several worker coroutines
    and several app processes therefore never process the same turn at
    the same time; a crashed worker's lease simply expires and the turn
    is claimed again. Only the lease holder may delete or reschedule it.
    A research stream takes the same lease while it runs
    (hold_for_stream/release_stream), so a worker never processes a turn
    that is being streamed, a stream that finds a worker on its turn waits
    for that answer instead of computing a second one, and a dropped
    stream hands its turn to the worker immediately.

  * Worker loop — started at FastAPI startup (same pattern as
    scheduler.py: asyncio task, sync Mongo/LLM work in asyncio.to_thread,
    never crashes). It wakes every SWEEP_INTERVAL_SECONDS, or immediately
    when a page asks for it (wake()), so turns are finished even if the
    user never sends another message or opens the chat again.

What it never does: mark a turn completed or failed by itself. A turn is
removed from the queue only when the chat shows both `results` and
`claude_answer` set (by the existing code paths), or when the chat/turn no
longer exists. A turn still waiting for evidence, or whose answer call
failed, is retried later (backoff), exactly as a later page view used to
retry it.

Limitation (no external job queue exists in this architecture): the
worker runs inside the web process(es). Turns are durable in Mongo, but
they are only processed while at least one web process with the startup
hook is running. If the web app is started with lifespan disabled, wake()
falls back to a short-lived thread for the turns a page just enqueued;
other turns wait until a process with the worker is running.
"""

import asyncio
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

log = logging.getLogger("flintel-turn-finisher")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


# How often an idle worker looks for due turns (seconds). A page view or a
# new turn wakes it earlier.
SWEEP_INTERVAL_SECONDS = _env_float("TURN_FINISH_SWEEP_SECONDS", 15)
# How long one claim is held. Longer than one scan + one answer call
# (CLAUDE_TIMEOUT_SECONDS default 60); an expired lease is claimed again.
LEASE_SECONDS = _env_float("TURN_FINISH_LEASE_SECONDS", 900)
# Turns processed concurrently by one process.
CONCURRENCY = max(1, int(_env_float("TURN_FINISH_CONCURRENCY", 2)))
# Retry delay while the owner has another answer in flight.
BUSY_RETRY_SECONDS = _env_float("TURN_FINISH_BUSY_RETRY_SECONDS", 10)
# Retry delay while a turn is still waiting for evidence.
PENDING_RETRY_SECONDS = _env_float("TURN_FINISH_PENDING_RETRY_SECONDS", 20)
# Backoff after a pass that should have produced an answer but did not
# (e.g. the answer call failed): BASE * 2^attempts, capped.
BACKOFF_BASE_SECONDS = _env_float("TURN_FINISH_BACKOFF_BASE_SECONDS", 15)
BACKOFF_MAX_SECONDS = _env_float("TURN_FINISH_BACKOFF_MAX_SECONDS", 600)
# Backfill of unfinished turns that have no queue document.
BACKFILL_INTERVAL_SECONDS = _env_float("TURN_FINISH_BACKFILL_SECONDS", 1800)
BACKFILL_MAX_CHATS = int(_env_float("TURN_FINISH_BACKFILL_MAX_CHATS", 500))

BACKFILL_DOC_ID = "__backfill__"

# (STREAM LEASE) A research stream holds the same per-turn lease as a worker
# while it runs, so a worker and a stream never process the same turn at the
# same time. Stream lease tokens start with this prefix.
STREAM_LEASE_PREFIX = "stream:"
# A new turn becomes due for the worker after this delay, unless its stream
# has taken the lease by then (it normally does within a second or two).
NEW_TURN_DELAY_SECONDS = _env_float("TURN_FINISH_NEW_TURN_DELAY_SECONDS", 60)

# process_fn(chat_id, owner_key, topic_key) -> (status, delay_seconds)
#   status "done"    : nothing left to do for this turn -> queue doc removed
#   status "retry"   : not finished yet -> retried after delay_seconds
#                      (None = use the backoff)
#   status "busy"    : owner has another answer in flight -> retried soon,
#                      does not count as an attempt
_state = {"process_fn": None, "loop": None, "event": None, "tasks": [], "started": False}
_state_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _collection():
    """Looked up at call time so tests/harnesses that replace the database
    module (or the attribute) are honoured. None if unavailable."""
    try:
        import database
        return getattr(database, "turn_finishing_collection", None)
    except Exception:
        return None


def job_id(chat_id: str, owner_key: str, topic_key: str) -> str:
    return f"{chat_id}|{owner_key}|{topic_key}"


def configure(process_fn):
    """Registers the function that actually finishes one turn (index.py's
    _finish_turn_job). Called when index.py is imported."""
    _state["process_fn"] = process_fn


# ── queue writes ─────────────────────────────────────────────────────────────
def enqueue(chat_id: str, owner_key: str, topic_key: str, due_in_seconds: float = 0.0,
            pull_forward: bool = False) -> bool:
    """Records that this turn must be finished. Idempotent: one document per
    turn. `due_in_seconds` only applies when the document is created.
    `pull_forward=True` (a page is looking at the turn) makes an existing,
    currently unleased document due now — it never touches a document whose
    lease is held by a worker. Never raises."""
    coll = _collection()
    if coll is None or not chat_id or not owner_key or not topic_key:
        return False
    now = _now()
    _id = job_id(chat_id, owner_key, topic_key)
    try:
        coll.update_one(
            {"_id": _id},
            {"$setOnInsert": {
                "chat_id": chat_id, "owner_key": owner_key, "topic_key": topic_key,
                "due_at": now + timedelta(seconds=max(0.0, float(due_in_seconds or 0))),
                "lease_owner": None, "attempts": 0, "created_at": now,
            }},
            upsert=True,
        )
        if pull_forward:
            coll.update_one({"_id": _id, "lease_owner": None, "due_at": {"$gt": now}},
                            {"$set": {"due_at": now}})
        return True
    except Exception as exc:
        log.warning(f"Enqueueing unfinished turn failed for {_id}: {exc}")
        return False


def claim(only_ids=None):
    """Atomically claims ONE due turn (due_at <= now) for this caller, or
    returns None. The claim moves due_at to now + LEASE_SECONDS and stores
    a fresh lease token, so no other worker/request/process can claim it
    until it is released or the lease expires."""
    coll = _collection()
    if coll is None:
        return None
    from pymongo import ReturnDocument
    now = _now()
    query = {"due_at": {"$lte": now}, "_id": {"$ne": BACKFILL_DOC_ID}}
    if only_ids is not None:
        query["_id"] = {"$in": [i for i in only_ids if i != BACKFILL_DOC_ID]}
    token = uuid.uuid4().hex
    try:
        doc = coll.find_one_and_update(
            query,
            {"$set": {"due_at": now + timedelta(seconds=LEASE_SECONDS), "lease_owner": token,
                      "leased_at": now},
             "$inc": {"claims": 1}},
            sort=[("due_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
    except Exception as exc:
        log.warning(f"Claiming an unfinished turn failed: {exc}")
        return None
    if not isinstance(doc, dict) or doc.get("lease_owner") != token:
        return None
    return doc


def _finish(doc):
    """Removes the queue document — only if this caller still holds the lease."""
    coll = _collection()
    try:
        coll.delete_one({"_id": doc["_id"], "lease_owner": doc["lease_owner"]})
    except Exception as exc:
        log.warning(f"Removing finished turn {doc.get('_id')} from the queue failed: {exc}")


def _release(doc, delay_seconds: float, count_attempt: bool, error: str = None):
    """Gives the turn back to the queue, due again after delay_seconds —
    only if this caller still holds the lease. A failed write here is safe:
    the lease simply expires and the turn is claimed again."""
    coll = _collection()
    update = {"$set": {"due_at": _now() + timedelta(seconds=max(1.0, float(delay_seconds))),
                       "lease_owner": None, "last_error": error}}
    if count_attempt:
        update["$inc"] = {"attempts": 1}
    try:
        coll.update_one({"_id": doc["_id"], "lease_owner": doc["lease_owner"]}, update)
    except Exception as exc:
        log.warning(f"Rescheduling unfinished turn {doc.get('_id')} failed: {exc}")


def _aware(dt):
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def hold_for_stream(chat_id: str, owner_key: str, topic_key: str, force: bool = False):
    """(STREAM LEASE) Called when a research stream starts working on a turn.
    One atomic upsert takes the turn's lease for the stream unless a WORKER
    currently holds an unexpired lease on it. Returns:
      token  — the stream holds the turn (release it with release_stream());
      None   — a worker is processing the turn right now (the stream should
               wait for its answer instead of computing a second one);
      False  — the queue is unavailable (the stream proceeds as before).
    A stream may take over another stream's lease (a reconnect/second tab —
    the existing busy-wait handles that case). `force=True` takes the lease
    unconditionally (used after the stream has waited out the deadline)."""
    coll = _collection()
    if coll is None or not chat_id or not owner_key or not topic_key:
        return False
    from pymongo import ReturnDocument
    from pymongo.errors import DuplicateKeyError
    now = _now()
    _id = job_id(chat_id, owner_key, topic_key)
    token = STREAM_LEASE_PREFIX + uuid.uuid4().hex
    flt = {"_id": _id}
    if not force:
        flt["$or"] = [{"lease_owner": None}, {"due_at": {"$lte": now}},
                      {"lease_owner": {"$regex": "^" + STREAM_LEASE_PREFIX}}]
    try:
        doc = coll.find_one_and_update(
            flt,
            {"$set": {"lease_owner": token, "due_at": now + timedelta(seconds=LEASE_SECONDS), "leased_at": now},
             "$setOnInsert": {"chat_id": chat_id, "owner_key": owner_key, "topic_key": topic_key,
                              "attempts": 0, "created_at": now}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return None          # the document exists and a worker holds it
    except Exception as exc:
        log.warning(f"Taking the stream lease failed for {_id}: {exc}")
        return False
    if not isinstance(doc, dict):
        return False
    return token if doc.get("lease_owner") == token else None


def worker_holds(chat_id: str, owner_key: str, topic_key: str) -> bool:
    """True while a worker (not a stream) holds an unexpired lease on the turn."""
    coll = _collection()
    if coll is None:
        return False
    try:
        doc = coll.find_one({"_id": job_id(chat_id, owner_key, topic_key)}, {"lease_owner": 1, "due_at": 1})
    except Exception:
        return False
    if not isinstance(doc, dict):
        return False
    lease = doc.get("lease_owner")
    due = _aware(doc.get("due_at"))
    return (isinstance(lease, str) and bool(lease) and not lease.startswith(STREAM_LEASE_PREFIX)
            and isinstance(due, datetime) and due > _now())


def release_stream(chat_id: str, owner_key: str, topic_key: str, token) -> bool:
    """(STREAM LEASE) Called from the stream's finally — normal end, error or
    client disconnect. Hands the turn back to the queue, due now, and wakes
    the worker: a finished turn is then simply removed, and a turn whose
    stream dropped is finished by the worker right away instead of waiting.
    Only the lease's own holder can release it (a newer stream that took
    over keeps it). Never raises."""
    if not token:
        return False
    coll = _collection()
    if coll is None:
        return False
    _id = job_id(chat_id, owner_key, topic_key)
    try:
        res = coll.update_one({"_id": _id, "lease_owner": token},
                              {"$set": {"lease_owner": None, "due_at": _now()}})
        released = bool(getattr(res, "modified_count", 0))
    except Exception as exc:
        log.warning(f"Releasing the stream lease failed for {_id}: {exc}")
        return False
    if released:
        wake([_id])
    return released


def backoff_seconds(attempts: int) -> float:
    try:
        return min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, int(attempts))))
    except OverflowError:
        return BACKOFF_MAX_SECONDS


# ── processing ───────────────────────────────────────────────────────────────
def process_one(only_ids=None) -> bool:
    """Claims and processes at most one due turn. Returns True if a turn was
    claimed (whatever its outcome), False if nothing was due. Never raises."""
    doc = claim(only_ids)
    if doc is None:
        return False
    fn = _state["process_fn"]
    if fn is None:
        _release(doc, SWEEP_INTERVAL_SECONDS, count_attempt=False, error="no process_fn configured")
        return True
    try:
        status, delay = fn(doc["chat_id"], doc["owner_key"], doc["topic_key"])
    except Exception as exc:
        log.warning(f"Finishing turn {doc.get('_id')} raised: {exc}")
        _release(doc, backoff_seconds(doc.get("attempts", 0)), count_attempt=True, error=str(exc)[:300])
        return True
    if status == "done":
        _finish(doc)
    elif status == "busy":
        _release(doc, delay if delay is not None else BUSY_RETRY_SECONDS, count_attempt=False)
    else:
        _release(doc, delay if delay is not None else backoff_seconds(doc.get("attempts", 0)),
                 count_attempt=delay is None)
    return True


def process_due(max_jobs: int = 50, only_ids=None) -> int:
    """Processes due turns one after another (each behind its own claim)."""
    n = 0
    while n < max_jobs and process_one(only_ids):
        n += 1
    return n


# ── backfill ─────────────────────────────────────────────────────────────────
def backfill_if_due(chats_collection=None) -> int:
    """At most once per BACKFILL_INTERVAL_SECONDS across all processes (one
    atomic claim on a fixed document): enqueues every unfinished search turn
    found in up to BACKFILL_MAX_CHATS most recently created matching chats. Returns how
    many turns were (re-)enqueued. Never raises."""
    coll = _collection()
    if coll is None:
        return 0
    if chats_collection is None:
        try:
            import database
            chats_collection = getattr(database, "chats_collection", None)
        except Exception:
            chats_collection = None
    if chats_collection is None:
        return 0
    now = _now()
    try:
        from pymongo.errors import DuplicateKeyError
        try:
            coll.insert_one({"_id": BACKFILL_DOC_ID, "due_at": datetime(1970, 1, 1, tzinfo=timezone.utc),
                             "next_run_at": datetime(1970, 1, 1, tzinfo=timezone.utc)})
        except DuplicateKeyError:
            pass
        won = coll.find_one_and_update(
            {"_id": BACKFILL_DOC_ID, "next_run_at": {"$lte": now}},
            {"$set": {"next_run_at": now + timedelta(seconds=BACKFILL_INTERVAL_SECONDS),
                      "last_started_at": now,
                      # never "due" for claim(): far future
                      "due_at": datetime(9999, 1, 1, tzinfo=timezone.utc)}},
        )
    except Exception as exc:
        log.warning(f"Backfill claim failed: {exc}")
        return 0
    if not isinstance(won, dict):
        return 0
    count = 0
    try:
        cursor = chats_collection.find(
            {"messages": {"$elemMatch": {
                "topic_key": {"$nin": [None, ""]},
                "$or": [{"claude_answer": None}, {"results": None}],
            }}},
            {"_id": 0, "chat_id": 1, "owner_key": 1, "messages.topic_key": 1,
             "messages.claude_answer": 1, "messages.results": 1},
        # Sorted by _id (always indexed; newest chats first), not updated_at:
        # chats have no updated_at index, so that sort would be a blocking
        # in-memory sort of whole chat documents.
        ).sort("_id", -1).limit(BACKFILL_MAX_CHATS)
        for chat in cursor:
            for key in unfinished_topic_keys(chat.get("messages") or []):
                if enqueue(chat.get("chat_id"), chat.get("owner_key"), key):
                    count += 1
    except Exception as exc:
        log.warning(f"Backfill scan failed: {exc}")
    if count:
        log.info(f"Backfill enqueued {count} unfinished turn(s)")
    return count


def unfinished_topic_keys(messages: list) -> list:
    """topic_keys whose LATEST message (the one every save helper writes to,
    see index._set_on_latest_message) is a search turn still missing
    `results` or `claude_answer`."""
    latest = {}
    for m in messages or []:
        tk = m.get("topic_key")
        if tk:
            latest[tk] = m
    return [tk for tk, m in latest.items() if m.get("results") is None or m.get("claude_answer") is None]


# ── worker loop (FastAPI startup) ────────────────────────────────────────────
async def _wait_for_wake(timeout: float):
    ev = _state["event"]
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    ev.clear()


async def _worker(i: int):
    while True:
        try:
            claimed = await asyncio.to_thread(process_one)
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # never let the loop die
            log.warning(f"Turn-finisher worker {i} pass failed: {exc}")
            claimed = False
        if not claimed:
            await _wait_for_wake(SWEEP_INTERVAL_SECONDS)


async def _backfill_loop():
    while True:
        try:
            await asyncio.to_thread(backfill_if_due)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(f"Turn-finisher backfill pass failed: {exc}")
        await asyncio.sleep(min(BACKFILL_INTERVAL_SECONDS, 300))


def start():
    """Starts CONCURRENCY worker coroutines plus the backfill loop on the
    running event loop. Called from FastAPI startup; returns immediately."""
    with _state_lock:
        if _state["started"]:
            return
        loop = asyncio.get_running_loop()
        _state["loop"] = loop
        _state["event"] = asyncio.Event()
        _state["tasks"] = [loop.create_task(_worker(i)) for i in range(CONCURRENCY)]
        _state["tasks"].append(loop.create_task(_backfill_loop()))
        _state["started"] = True
    log.info(f"Turn finisher started ({CONCURRENCY} worker(s), sweep every {SWEEP_INTERVAL_SECONDS:.0f}s)")


async def stop():
    with _state_lock:
        tasks, _state["tasks"] = _state["tasks"], []
        _state["started"] = False
        _state["loop"] = None
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except BaseException:
            pass


def is_running() -> bool:
    loop = _state["loop"]
    return bool(_state["started"] and loop is not None and not loop.is_closed() and loop.is_running())


def wake(job_ids=None) -> str:
    """Asks for prompt processing of due turns. Thread-safe; never blocks and
    never raises. With the worker running in this process the worker is
    woken. Otherwise (lifespan disabled) a short-lived thread processes just
    `job_ids` — the queue documents are already durable, so if that thread
    dies the turns are still processed by any running worker later."""
    try:
        if is_running():
            _state["loop"].call_soon_threadsafe(_state["event"].set)
            return "worker"
        if job_ids:
            ids = list(job_ids)
            threading.Thread(target=process_due, kwargs={"max_jobs": len(ids), "only_ids": ids},
                             name="turn-finisher-once", daemon=True).start()
            return "thread"
    except Exception as exc:
        log.warning(f"Waking the turn finisher failed: {exc}")
    return "none"
