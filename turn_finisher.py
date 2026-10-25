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
import hashlib
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


# ── mode (TURN_FINISH_* flags, see config.py) ────────────────────────────────
# "legacy"  : TURN_FINISH_ENABLED is unset/false. Exactly the behaviour from before the flags existed: in production
#             database.py defines no queue collection, so every function below is a no-op (a test may inject one).
# "dry_run" : TURN_FINISH_ENABLED=true and processing is not (or may not yet be) allowed. The ONLY thing the queue does is
#             enqueue(): it writes one small document per turn. It never claims, leases, processes, retries, completes or
#             deletes a turn, never starts a worker or backfill task, never spawns a thread, and never calls the model.
#             Streams are not touched either: hold_for_stream() answers "queue unavailable" (the stream proceeds exactly as
#             in legacy mode) and worker_holds()/release_stream() do nothing, so no lease is ever held.
# "active"  : enabled AND TURN_FINISH_DRY_RUN=false AND the processing governors exist. Not reachable in this version:
#             the budget, retry and age limits are separate, later changes, so _PROCESSING_GOVERNED stays False and
#             TURN_FINISH_DRY_RUN=false is ignored (with a warning) rather than starting unthrottled processing.
_PROCESSING_GOVERNED = False


def mode() -> str:
    """"legacy" | "dry_run" | "active" (see above). Read at call time so tests and the environment are honoured.
    If the configuration cannot be imported or read this fails CLOSED ("dry_run"), never to "legacy"."""
    try:
        import config
        enabled = bool(getattr(config, "TURN_FINISH_ENABLED", False))
        dry_run = bool(getattr(config, "TURN_FINISH_DRY_RUN", True))
    except Exception:
        return "dry_run"          # fail closed: if the flags cannot be read, never fall back to processing
    if not enabled:
        return "legacy"
    if dry_run or not _PROCESSING_GOVERNED:
        return "dry_run"
    return "active"


def _cfg_value(name, default):
    """A config attribute, or `default` if the configuration cannot be imported/read (never raises)."""
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def job_id(chat_id: str, owner_key: str, topic_key: str) -> str:
    return f"{chat_id}|{owner_key}|{topic_key}"


def configure(process_fn):
    """Registers the function that actually finishes one turn (index.py's
    _finish_turn_job). Called when index.py is imported."""
    _state["process_fn"] = process_fn


# ── limits (config.py). Applied only OUTSIDE "legacy" mode; every value is bounded ───────────────────────────────────────
FAR_FUTURE = datetime(9999, 1, 1, tzinfo=timezone.utc)
BUDGET_PREFIX = "__budget__:"
HEARTBEAT_PREFIX = "__heartbeat__:"
MIN_RETRY_SECONDS = 5.0                 # no retry loop tighter than this in a governed mode
ATTENTION_BACKOFF_SECONDS = 3600.0      # once a job has failed ATTEMPTS_WARN times it is retried at most hourly
REPORT_MAX_TIME_MS = 3000
REPORT_COUNT_CAP = 10000
_PROCESS = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _bounded(name, default, lo, hi):
    """An int config value inside [lo, hi]; anything else (missing, bool, str, out of range) -> `default`."""
    v = _cfg_value(name, default)
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else default


def _ref(value) -> str:
    """Short, non-reversible reference for logs and reports. Job ids contain the owner and the query text; those are
    never logged or reported."""
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:10]


def _lease_seconds() -> float:
    """How long one claim is held. Outside legacy mode never shorter than one full pass can take (otherwise a live worker
    loses its lease and the turn is processed twice): RESPONSE_TIMEOUT + CLAUDE_TIMEOUT_SECONDS + 60."""
    if mode() == "legacy":
        return LEASE_SECONDS
    try:
        floor = float(_cfg_value("RESPONSE_TIMEOUT", 360)) + float(_cfg_value("CLAUDE_TIMEOUT_SECONDS", 60)) + 60.0
    except (TypeError, ValueError):
        floor = 480.0
    return max(LEASE_SECONDS, floor)


_stats = {"passes": 0, "done": 0, "retry": 0, "busy": 0, "errors": 0, "parked": 0, "budget_deferred": 0,
          "last_pass_at": None, "last_error": None, "last_summary_at": None, "budget_logged_window": None}


def _bump(key):
    _stats[key] = _stats.get(key, 0) + 1


def pass_limit() -> int:
    """The per-UTC-hour PASS budget (TURN_FINISH_MAX_PASSES_PER_HOUR, 1..1000, default 20). A pass is one attempt to process
    one claimed turn; it is NOT a money amount: a pass may cost a scan only, or several model calls (see the README)."""
    return _bounded("TURN_FINISH_MAX_PASSES_PER_HOUR", 20, 1, 1000)


def _window_id(now) -> str:
    return now.strftime("%Y-%m-%dT%H")


def _next_window(now):
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def take_pass(now=None):
    """Takes ONE unit of this UTC hour's pass budget. Returns the budget document id (to refund, see refund_pass) or None.

    One document per hour: {_id: "__budget__:<YYYY-MM-DDTHH>", used}. The only write that raises `used` is
    find_one_and_update({_id, used: {$lt: limit}}, {$inc: {used: 1}}, upsert). A single-document update is atomic in MongoDB, so
    `used` can never exceed the limit, whatever the number of processes, and a different hour is a different document, so no
    clock difference can reset a window. If the document exists but the filter fails (budget used up) the upsert tries to insert
    the same _id and raises DuplicateKeyError; two first-time callers race the same way. On DuplicateKeyError the document is
    re-read: used >= limit means exhausted, otherwise retry (3 tries). Any other error, an unexpected answer, or no collection
    means NO budget (fail closed): a unit may be burnt without a pass, never the reverse.
    Fixed-window behaviour: up to `limit` passes in the last minutes of one hour and `limit` in the first of the next, i.e. up
    to 2 x limit in a short span; a process whose clock runs ahead simply uses the next window's document early."""
    coll = _collection()
    if coll is None:
        return None
    try:
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError
    except Exception:
        return None
    now = _now() if now is None else now
    limit = pass_limit()
    window = _window_id(now)
    wid = BUDGET_PREFIX + window
    for _ in range(3):
        try:
            doc = coll.find_one_and_update(
                {"_id": wid, "used": {"$lt": limit}},
                {"$inc": {"used": 1}, "$setOnInsert": {"window": window, "created_at": now}},
                upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            try:
                cur = coll.find_one({"_id": wid}, {"used": 1})
            except Exception:
                return None
            used = cur.get("used") if isinstance(cur, dict) else None
            if isinstance(used, int) and not isinstance(used, bool) and used >= limit:
                return None                                  # exhausted for this hour
            continue                                         # a first-time race: the document exists now, retry
        except Exception as exc:
            log.warning(f"Pass budget unavailable ({type(exc).__name__}); no pass is started")
            return None
        used = doc.get("used") if isinstance(doc, dict) else None
        if isinstance(used, int) and not isinstance(used, bool) and 1 <= used <= limit:
            return wid
        return None                                          # unexpected answer: fail closed
    return None


def refund_pass(wid) -> bool:
    """Gives a unit back when the pass turned out to do no work (the owner was busy). Only ever lowers `used`, never below 0;
    a failed refund leaves the unit spent (conservative)."""
    coll = _collection()
    if coll is None or not wid:
        return False
    try:
        res = coll.update_one({"_id": wid, "used": {"$gt": 0}}, {"$inc": {"used": -1}})
        return bool(getattr(res, "modified_count", 0))
    except Exception:
        return False


def _budget_blocked() -> bool:
    """True while this process knows the hour's budget is used up: it then does not even claim a turn."""
    until = _state.get("budget_blocked_until")
    if until is None:
        return False
    if _now() >= until:
        _state["budget_blocked_until"] = None
        return False
    return True


def _too_old(doc) -> bool:
    created = _aware(doc.get("created_at"))
    if not isinstance(created, datetime):
        return False
    return _now() - created > timedelta(days=_bounded("TURN_FINISH_MAX_AGE_DAYS", 30, 1, 365))


def _park(doc, reason):
    """Takes a job out of rotation WITHOUT deleting it or touching the chat: parked jobs are never claimed. The user's turn
    stays exactly as it was (unfinished); an operator can requeue it (requeue_parked)."""
    coll = _collection()
    try:
        coll.update_one({"_id": doc["_id"], "lease_owner": doc["lease_owner"]},
                        {"$set": {"parked": True, "parked_at": _now(), "parked_reason": reason, "due_at": FAR_FUTURE,
                                  "lease_owner": None}})
    except Exception as exc:
        log.warning(f"Parking turn ref={_ref(doc.get('_id'))} failed ({type(exc).__name__}); its lease will expire")
        return
    _bump("parked")
    log.warning(f"turn parked ref={_ref(doc.get('_id'))} reason={reason} attempts={doc.get('attempts', 0)}; "
                f"the turn is unchanged in its chat; requeue with turn_finisher.requeue_parked")


def requeue_parked(chat_id: str, owner_key: str, topic_key: str) -> bool:
    """OPERATOR tool: puts a parked job back in the queue, due now, with its attempts reset. Never raises."""
    coll = _collection()
    if coll is None:
        return False
    try:
        res = coll.update_one({"_id": job_id(chat_id, owner_key, topic_key), "parked": True},
                              {"$set": {"due_at": _now(), "lease_owner": None, "attempts": 0, "needs_attention": False},
                               "$unset": {"parked": "", "parked_at": "", "parked_reason": ""}})
        return bool(getattr(res, "modified_count", 0))
    except Exception:
        return False


def _defer_for_budget(doc):
    """The hour's budget is used up: hand the turn back, due when the next hour starts (plus a small stable jitter), not
    counted as an attempt, and stop claiming in this process until then."""
    now = _now()
    nxt = _next_window(now)
    ref = _ref(doc.get("_id"))
    _state["budget_blocked_until"] = nxt
    _release(doc, (nxt - now).total_seconds() + (int(ref, 16) % 30), count_attempt=False)
    _bump("budget_deferred")
    window = _window_id(now)
    if _stats.get("budget_logged_window") != window:
        _stats["budget_logged_window"] = window
        log.warning(f"turn pass budget used up for {window} (limit {pass_limit()} passes/hour); turns are deferred to the "
                    f"next hour, none is dropped")


def unfinished_turns(messages: list) -> dict:
    """{topic_key: its LATEST message} for every search turn still missing `results` or `claude_answer`."""
    latest = {}
    for m in messages or []:
        tk = m.get("topic_key")
        if tk:
            latest[tk] = m
    return {tk: m for tk, m in latest.items() if m.get("results") is None or m.get("claude_answer") is None}


def ondemand_topic_keys(messages: list, now=None) -> list:
    """The older unfinished turns a chat page view may record. legacy: all (as before). dry run: none. Governed: only turns
    requested within TURN_FINISH_ONDEMAND_MAX_AGE_HOURS (0 = none, the default), newest first, at most
    TURN_FINISH_ONDEMAND_MAX_TURNS, so a page view can never cause unbounded synchronous queue work."""
    m = mode()
    if m == "legacy":
        return unfinished_topic_keys(messages)
    if m == "dry_run":
        return []
    hours = _bounded("TURN_FINISH_ONDEMAND_MAX_AGE_HOURS", 0, 0, 720)
    if hours == 0:
        return []
    cap = _bounded("TURN_FINISH_ONDEMAND_MAX_TURNS", 5, 1, 50)
    cutoff = (_now() if now is None else now) - timedelta(hours=hours)
    recent = []
    for key, msg in unfinished_turns(messages).items():
        asked = _aware(msg.get("requested_at"))
        if isinstance(asked, datetime) and asked >= cutoff:
            recent.append((asked, key))
    recent.sort(reverse=True)
    return [key for _asked, key in recent[:cap]]


# ── observability: reports, heartbeat, status, summary log (no query text, owner, e-mail or job id ever appears) ─────────
def status() -> dict:
    """In-memory status of THIS process (no database access)."""
    return {"mode": mode(), "processing_governed": _PROCESSING_GOVERNED, "started": bool(_state["started"]),
            "tasks": len(_state["tasks"]), "process": _ref(_PROCESS), "stats": dict(_stats),
            "budget_blocked_until": _state.get("budget_blocked_until")}


def heartbeat(now=None) -> bool:
    """Records that this process's worker loop is alive (one small document per process; old ones are pruned hourly)."""
    coll = _collection()
    if coll is None:
        return False
    now = _now() if now is None else now
    try:
        coll.update_one({"_id": HEARTBEAT_PREFIX + _PROCESS},
                        {"$set": {"last_seen": now, "mode": mode(), "pid": os.getpid()}}, upsert=True)
        if _state.get("heartbeat_pruned_at") is None or (now - _state["heartbeat_pruned_at"]).total_seconds() > 3600:
            _state["heartbeat_pruned_at"] = now
            coll.delete_many({"_id": {"$regex": "^" + HEARTBEAT_PREFIX}, "last_seen": {"$lt": now - timedelta(days=1)}})
        return True
    except Exception as exc:
        log.debug(f"Heartbeat failed ({type(exc).__name__})")
        return False


def report(now=None) -> dict:
    """Read-only, bounded snapshot of the queue for operators: counts (each capped at REPORT_COUNT_CAP and limited to
    REPORT_MAX_TIME_MS), the age of the oldest due job, this hour's pass budget and recent worker heartbeats. Nothing here
    contains a query, owner, e-mail or job id. Never raises, never writes."""
    now = _now() if now is None else now
    out = {"mode": mode(), "processing_governed": _PROCESSING_GOVERNED,
           "enabled": bool(_cfg_value("TURN_FINISH_ENABLED", False)), "dry_run": bool(_cfg_value("TURN_FINISH_DRY_RUN", True)),
           "collection": False, "errors": [], "capped": False}
    coll = _collection()
    if coll is None:
        return out
    out["collection"] = True
    job = {"chat_id": {"$exists": True}}                      # queue documents only (not budget / heartbeat / marker)

    def count(flt):
        try:
            n = coll.count_documents({"$and": [job, flt]}, limit=REPORT_COUNT_CAP, maxTimeMS=REPORT_MAX_TIME_MS)
        except Exception as exc:
            out["errors"].append(type(exc).__name__)
            return None
        if n >= REPORT_COUNT_CAP:
            out["capped"] = True
        return n
    unleased_due = {"parked": {"$ne": True}, "lease_owner": None, "due_at": {"$lte": now}}
    out["queue"] = {
        "total": count({}), "due": count(unleased_due),
        "leased": count({"lease_owner": {"$ne": None}, "due_at": {"$gt": now}}),
        "lease_expired": count({"lease_owner": {"$ne": None}, "due_at": {"$lte": now}}),
        "parked": count({"parked": True}), "needs_attention": count({"needs_attention": True}),
        "oldest_due_age_seconds": None}
    try:
        oldest = list(coll.find({"$and": [job, unleased_due]}, {"due_at": 1}).sort("due_at", 1).limit(1))
        if oldest and isinstance(_aware(oldest[0].get("due_at")), datetime):
            out["queue"]["oldest_due_age_seconds"] = max(0, int((now - _aware(oldest[0]["due_at"])).total_seconds()))
    except Exception as exc:
        out["errors"].append(type(exc).__name__)
    used = None
    try:
        cur = coll.find_one({"_id": BUDGET_PREFIX + _window_id(now)}, {"used": 1})
        used = cur.get("used") if isinstance(cur, dict) else 0
    except Exception as exc:
        out["errors"].append(type(exc).__name__)
    limit = pass_limit()
    out["budget"] = {"window": _window_id(now), "used": used, "limit": limit,
                     "remaining": None if not isinstance(used, int) else max(0, limit - used),
                     "unit": "passes (not money)"}
    beats = []
    try:
        for d in coll.find({"_id": {"$regex": "^" + HEARTBEAT_PREFIX}, "last_seen": {"$gte": now - timedelta(hours=1)}}).limit(10):
            seen = _aware(d.get("last_seen"))
            beats.append({"process": _ref(d.get("_id")), "mode": d.get("mode"),
                          "age_seconds": max(0, int((now - seen).total_seconds())) if isinstance(seen, datetime) else None})
    except Exception as exc:
        out["errors"].append(type(exc).__name__)
    out["heartbeats"] = beats
    return out


def log_summary_if_due(now=None, force=False):
    """One INFO line every TURN_FINISH_SUMMARY_SECONDS (60..3600, default 300) with the counts above. Returns the report or None."""
    now = _now() if now is None else now
    last = _stats.get("last_summary_at")
    if not force and last is not None and (now - last).total_seconds() < _bounded("TURN_FINISH_SUMMARY_SECONDS", 300, 60, 3600):
        return None
    _stats["last_summary_at"] = now
    rep = report(now)
    q, b = rep.get("queue") or {}, rep.get("budget") or {}
    log.info("turn queue: mode=%s total=%s due=%s leased=%s lease_expired=%s parked=%s attention=%s oldest_due_s=%s "
             "passes=%s/%s heartbeats=%d errors=%d", rep["mode"], q.get("total"), q.get("due"), q.get("leased"),
             q.get("lease_expired"), q.get("parked"), q.get("needs_attention"), q.get("oldest_due_age_seconds"),
             b.get("used"), b.get("limit"), len(rep.get("heartbeats") or []), len(rep.get("errors") or []))
    return rep


# ── queue writes ─────────────────────────────────────────────────────────────
def enqueue(chat_id: str, owner_key: str, topic_key: str, due_in_seconds: float = 0.0,
            pull_forward: bool = False) -> bool:
    """Records that this turn must be finished. Idempotent: one document per
    turn. `due_in_seconds` only applies when the document is created.
    `pull_forward=True` (a page is looking at the turn) makes an existing,
    currently unleased document due now — it never touches a document whose
    lease is held by a worker. Never raises.
    Dry run: the page-view intake of older turns (pull_forward=True) is skipped before any write; only NEW turns are
    recorded."""
    if pull_forward and mode() == "dry_run":
        return False
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
            coll.update_one({"_id": _id, "lease_owner": None, "parked": {"$ne": True}, "due_at": {"$gt": now}},
                            {"$set": {"due_at": now}})
        return True
    except Exception as exc:
        log.warning(f"Enqueueing unfinished turn failed ref={_ref(_id)} ({type(exc).__name__})")
        return False


def claim(only_ids=None):
    """Atomically claims ONE due turn (due_at <= now) for this caller, or
    returns None. The claim moves due_at to now + LEASE_SECONDS and stores
    a fresh lease token, so no other worker/request/process can claim it
    until it is released or the lease expires. (Dry run: never claims.)"""
    if mode() == "dry_run":
        return None
    coll = _collection()
    if coll is None:
        return None
    from pymongo import ReturnDocument
    now = _now()
    # only real queue jobs (they always carry chat_id): the backfill marker, budget and heartbeat documents can never be claimed
    query = {"due_at": {"$lte": now}, "parked": {"$ne": True}, "chat_id": {"$exists": True}, "_id": {"$ne": BACKFILL_DOC_ID}}
    if only_ids is not None:
        query["_id"] = {"$in": [i for i in only_ids if i != BACKFILL_DOC_ID]}
    token = uuid.uuid4().hex
    try:
        doc = coll.find_one_and_update(
            query,
            {"$set": {"due_at": now + timedelta(seconds=_lease_seconds()), "lease_owner": token,
                      "leased_at": now},
             "$inc": {"claims": 1}},
            sort=[("due_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
    except Exception as exc:
        log.warning(f"Claiming an unfinished turn failed ({type(exc).__name__})")
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
        log.warning(f"Removing finished turn ref={_ref(doc.get('_id'))} from the queue failed ({type(exc).__name__})")


def _release(doc, delay_seconds: float, count_attempt: bool, error: str = None, extra: dict = None):
    """Gives the turn back to the queue, due again after delay_seconds —
    only if this caller still holds the lease. A failed write here is safe:
    the lease simply expires and the turn is claimed again."""
    coll = _collection()
    update = {"$set": {"due_at": _now() + timedelta(seconds=max(1.0, float(delay_seconds))),
                       "lease_owner": None, "last_error": error, **(extra or {})}}
    if count_attempt:
        update["$inc"] = {"attempts": 1}
    try:
        coll.update_one({"_id": doc["_id"], "lease_owner": doc["lease_owner"]}, update)
    except Exception as exc:
        log.warning(f"Rescheduling unfinished turn ref={_ref(doc.get('_id'))} failed ({type(exc).__name__})")


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
    unconditionally (used after the stream has waited out the deadline).
    Dry run: always False (no lease is taken; the stream proceeds as in legacy mode)."""
    if mode() == "dry_run":
        return False
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
            {"$set": {"lease_owner": token, "due_at": now + timedelta(seconds=_lease_seconds()), "leased_at": now},
             "$setOnInsert": {"chat_id": chat_id, "owner_key": owner_key, "topic_key": topic_key,
                              "attempts": 0, "created_at": now}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return None          # the document exists and a worker holds it
    except Exception as exc:
        log.warning(f"Taking the stream lease failed ref={_ref(_id)} ({type(exc).__name__})")
        return False
    if not isinstance(doc, dict):
        return False
    return token if doc.get("lease_owner") == token else None


def worker_holds(chat_id: str, owner_key: str, topic_key: str) -> bool:
    """True while a worker (not a stream) holds an unexpired lease on the turn. (Dry run: no worker exists: False.)"""
    if mode() == "dry_run":
        return False
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
    if not token or mode() == "dry_run":
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
        log.warning(f"Releasing the stream lease failed ref={_ref(_id)} ({type(exc).__name__})")
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
def _retry(doc, ref, delay, count_attempt, error, governed):
    """Gives a turn back for another try. Governed modes add three bounds: no retry sooner than MIN_RETRY_SECONDS; after
    TURN_FINISH_ATTEMPTS_WARN failing passes the job is flagged `needs_attention` (one ERROR log) and retried at most hourly;
    and a job older than TURN_FINISH_MAX_AGE_DAYS is parked at its next claim. Nothing is deleted and no turn is marked
    complete or failed."""
    attempts = int(doc.get("attempts") or 0)
    base = delay if delay is not None else backoff_seconds(attempts)
    extra = None
    if governed:
        base = max(float(base), MIN_RETRY_SECONDS)
        after = attempts + (1 if count_attempt else 0)
        if after >= _bounded("TURN_FINISH_ATTEMPTS_WARN", 8, 1, 100):
            base = max(base, ATTENTION_BACKOFF_SECONDS)
            extra = {"needs_attention": True}
            if not doc.get("needs_attention"):
                extra["attention_at"] = _now()
                log.error(f"turn needs attention ref={ref} failing_passes={after}; retried at most hourly")
    _release(doc, base, count_attempt, error, extra)


def process_one(only_ids=None) -> bool:
    """Claims and processes at most one due turn. Returns True if a turn was
    claimed (whatever its outcome), False if nothing was due. Never raises.
    Dry run: returns False at once; process_fn (the model call) is never reached.
    This is the ONLY place process_fn is called, so every entry point (workers, process_due, the wake thread) passes through
    the governors below, which apply outside legacy mode: the hour's pass budget (take_pass, refunded when the owner was
    busy), parking of jobs older than TURN_FINISH_MAX_AGE_DAYS, bounded retries and the lease floor."""
    m = mode()
    if m == "dry_run":
        return False
    governed = m == "active"
    if governed and _budget_blocked():
        return False                                          # the hour's budget is used up: do not even claim
    doc = claim(only_ids)
    if doc is None:
        return False
    ref = _ref(doc.get("_id"))
    fn = _state["process_fn"]
    if fn is None:
        _release(doc, SWEEP_INTERVAL_SECONDS, count_attempt=False, error="no process_fn configured")
        return True
    window = None
    if governed:
        if _too_old(doc):
            _park(doc, "max_age")
            return True
        window = take_pass()
        if window is None:
            _defer_for_budget(doc)
            return True
    _bump("passes")
    _stats["last_pass_at"] = _now()
    try:
        status, delay = fn(doc["chat_id"], doc["owner_key"], doc["topic_key"])
    except Exception as exc:
        _bump("errors")
        _stats["last_error"] = type(exc).__name__
        log.warning(f"turn pass raised ref={ref} error={type(exc).__name__}")
        _retry(doc, ref, None, True, type(exc).__name__, governed)
        return True
    log.info(f"turn pass ref={ref} outcome={status if status in ('done', 'busy') else 'retry'} attempts={doc.get('attempts', 0)}")
    if status == "done":
        _bump("done")
        _finish(doc)
    elif status == "busy":
        _bump("busy")
        _release(doc, delay if delay is not None else BUSY_RETRY_SECONDS, count_attempt=False)
        if governed:
            refund_pass(window)                               # the owner was busy: no scan, no model call happened
    else:
        _bump("retry")
        _retry(doc, ref, delay, delay is None, None, governed)
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
    atomic claim on a fixed document): enqueues the unfinished search turns
    found in the most recently created matching chats. Returns how many turns
    were (re-)enqueued. Never raises.
    Outside legacy mode it is OFF unless TURN_FINISH_BACKFILL_ENABLED=true and is bounded: at most
    TURN_FINISH_BACKFILL_MAX_CHATS chats (1..200, default 20) and TURN_FINISH_BACKFILL_MAX_TURNS turns (1..200, default 50)
    per pass, only turns requested within TURN_FINISH_BACKFILL_MAX_AGE_DAYS (1..30, default 3), with a server-side time limit;
    `.limit()` can never receive 0 (= unlimited)."""
    bounded = mode() != "legacy"
    if bounded and not (_cfg_value("TURN_FINISH_BACKFILL_ENABLED", False) is True):
        return 0
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
    max_chats = _bounded("TURN_FINISH_BACKFILL_MAX_CHATS", 20, 1, 200) if bounded else BACKFILL_MAX_CHATS
    max_turns = _bounded("TURN_FINISH_BACKFILL_MAX_TURNS", 50, 1, 200) if bounded else None
    cutoff = now - timedelta(days=_bounded("TURN_FINISH_BACKFILL_MAX_AGE_DAYS", 3, 1, 30)) if bounded else None
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
        log.warning(f"Backfill claim failed ({type(exc).__name__})")
        return 0
    if not isinstance(won, dict):
        return 0
    count = 0
    elem = {"topic_key": {"$nin": [None, ""]}, "$or": [{"claude_answer": None}, {"results": None}]}
    if bounded:
        elem["requested_at"] = {"$gte": cutoff}
    try:
        cursor = chats_collection.find(
            {"messages": {"$elemMatch": elem}},
            {"_id": 0, "chat_id": 1, "owner_key": 1, "messages.topic_key": 1, "messages.requested_at": 1,
             "messages.claude_answer": 1, "messages.results": 1},
        # Sorted by _id (always indexed; newest chats first), not updated_at:
        # chats have no updated_at index, so that sort would be a blocking
        # in-memory sort of whole chat documents.
        ).sort("_id", -1).limit(max_chats)
        if bounded and hasattr(cursor, "max_time_ms"):
            cursor = cursor.max_time_ms(REPORT_MAX_TIME_MS * 2)
        for chat in cursor:
            for key, msg in unfinished_turns(chat.get("messages") or []).items():
                if bounded:
                    asked = _aware(msg.get("requested_at"))
                    if not isinstance(asked, datetime) or asked < cutoff:
                        continue
                    if count >= max_turns:
                        break
                if enqueue(chat.get("chat_id"), chat.get("owner_key"), key):
                    count += 1
            if bounded and count >= max_turns:
                break
    except Exception as exc:
        log.warning(f"Backfill scan failed ({type(exc).__name__})")
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


def _housekeeping():
    try:
        heartbeat()
        log_summary_if_due()
    except Exception as exc:
        log.debug(f"Housekeeping failed ({type(exc).__name__})")


async def _worker(i: int):
    while True:
        try:
            claimed = await asyncio.to_thread(process_one)
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # never let the loop die
            log.warning(f"Turn-finisher worker {i} pass failed ({type(exc).__name__})")
            claimed = False
        if i == 0:
            await asyncio.to_thread(_housekeeping)               # heartbeat + periodic summary line; never raises
        if not claimed:
            await _wait_for_wake(SWEEP_INTERVAL_SECONDS)


async def _backfill_loop():
    while True:
        try:
            await asyncio.to_thread(backfill_if_due)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(f"Turn-finisher backfill pass failed ({type(exc).__name__})")
        await asyncio.sleep(min(BACKFILL_INTERVAL_SECONDS, 300))


def _ensure_indexes():
    """The one performance index (due_at), only when processing is active: created once per process if missing, never at
    import, never in dry run. A failure is logged and ignored."""
    coll = _collection()
    try:
        have = coll.index_information()
        if not any([k[0] for k in v.get("key", [])] == ["due_at"] for v in have.values()):
            coll.create_index("due_at")
    except Exception as exc:
        log.warning(f"Could not ensure the turn queue due_at index ({type(exc).__name__})")


def start():
    """Starts CONCURRENCY worker coroutines plus the backfill loop on the
    running event loop. Called from FastAPI startup; returns immediately.
    Starts nothing when there is no queue collection (feature off) or in dry-run mode."""
    if mode() == "dry_run":
        log.info("Turn finisher: dry run: turns are only recorded; no worker or backfill task")
        if _cfg_value("TURN_FINISH_DRY_RUN", True) is False and not _PROCESSING_GOVERNED:
            log.warning("TURN_FINISH_DRY_RUN=false is ignored: processing is not enabled in this build, so the queue "
                        "stays in dry-run")
        return
    if _collection() is None:
        return
    governed = mode() == "active"
    if governed:
        _ensure_indexes()
    with _state_lock:
        if _state["started"]:
            return
        loop = asyncio.get_running_loop()
        _state["loop"] = loop
        _state["event"] = asyncio.Event()
        _state["tasks"] = [loop.create_task(_worker(i)) for i in range(CONCURRENCY)]
        if not governed or _cfg_value("TURN_FINISH_BACKFILL_ENABLED", False) is True:   # backfill is OFF by default
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
    dies the turns are still processed by any running worker later.
    Dry run: does nothing and starts no thread."""
    if mode() == "dry_run":
        return "none"
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
        log.warning(f"Waking the turn finisher failed ({type(exc).__name__})")
    return "none"
