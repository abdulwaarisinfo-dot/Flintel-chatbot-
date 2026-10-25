"""
tests/test_turn_queue_wiring.py - durable turn queue, SLICE 1: default-off wiring + a dry run that only RECORDS turns.

What this file proves (mongomock + subprocesses; no real MongoDB, no network, no model):

  * TURN_FINISH_ENABLED is off unless it is exactly "true" (any case); TURN_FINISH_DRY_RUN is on unless it is exactly "false".
  * Disabled: database.py defines no queue collection, creates no collection or index, and start() creates no task.
  * Enabled: the handle exists (flintel_turn_finishing, majority write concern) but nothing is created in the database.
  * Dry run (the only enabled mode in this version; TURN_FINISH_DRY_RUN=false is still a dry run until the processing
    limits exist): enqueue() is the ONLY permitted operation. No claim, lease, release, processing, retry, completion,
    worker/backfill task, thread or model call happens, and streams are left exactly as in the legacy mode.
  * With the flag unset the module behaves exactly as before (the 21 tests of test_nonblocking_chat_page.py run unmodified).
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

mongomock = pytest.importorskip("mongomock")
ROOT = Path(__file__).resolve().parent.parent

import turn_finisher as tf  # noqa: E402
from tests.test_nonblocking_chat_page import (  # noqa: F401,E402  (fixtures + helpers)
    OWNER, _chat_turn, _msg, _seed, _start_worker, _turn, _view, app_env, browser, env, fin,
)

_REAL_THREAD = threading.Thread          # captured at import: some tests replace threading.Thread


def run_async(coro_fn, timeout=30):
    """Runs `await coro_fn()` on a fresh event loop in a helper thread. Earlier tests in the same process (a session-wide
    Playwright instance) can leave an event loop running in the main thread, where asyncio.run() would raise."""
    box = {}

    def target():
        box["value"] = asyncio.run(coro_fn())
    t = _REAL_THREAD(target=target)
    t.start()
    t.join(timeout)
    assert "value" in box, "the coroutine did not finish"
    return box["value"]


BASE_ENV_DROP = ("TURN_FINISH_ENABLED", "TURN_FINISH_DRY_RUN", "CREDIT_ENFORCEMENT_DISABLED")


def _run(script, **extra):
    env_vars = {k: v for k, v in os.environ.items() if not k.startswith(("MONGODB",)) and k not in BASE_ENV_DROP}
    env_vars.update({"SESSION_SECRET_KEY": "test-only-session-key-not-for-deployment", "PYTHONPATH": str(ROOT),
                     "MONGODB_URI": "mongodb://primary.invalid:27017", "MONGODB3": "mongodb://third.invalid:27017",
                     "MONGODB2": "", "MONGODB4": "", "PYTHONDONTWRITEBYTECODE": "1"})
    env_vars.update(extra)
    out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env_vars, capture_output=True, text=True,
                         timeout=90, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


# ── 1. flag parsing (the real config.py in a fresh process) ───────────────────────────────────────────────────────────
PARSE = r"""
import json, config
print(json.dumps({"enabled": config.TURN_FINISH_ENABLED, "dry_run": config.TURN_FINISH_DRY_RUN}))
"""


@pytest.mark.parametrize("raw,expected", [
    (None, False), ("", False), ("   ", False), ("false", False), ("1", False), ("yes", False), ("on", False), ("tru", False),
    ("true", True), ("TRUE", True), ("True", True), ("  true  ", True)])
def test_turn_finish_enabled_is_off_unless_exactly_true(raw, expected):
    extra = {} if raw is None else {"TURN_FINISH_ENABLED": raw}
    assert _run(PARSE, **extra)["enabled"] is expected


@pytest.mark.parametrize("raw,expected", [
    (None, True), ("", True), ("true", True), ("TRUE", True), ("0", True), ("no", True), ("off", True), ("garbage", True),
    ("false", False), ("FALSE", False), (" False ", False)])
def test_turn_finish_dry_run_is_on_unless_exactly_false(raw, expected):
    extra = {} if raw is None else {"TURN_FINISH_DRY_RUN": raw}
    assert _run(PARSE, **extra)["dry_run"] is expected


def test_the_flags_are_exported_by_config():
    exported = (ROOT / "config.py").read_text(encoding="utf-8").split("__all__ = [")[1].split("\n]")[0]
    assert '"TURN_FINISH_ENABLED"' in exported and '"TURN_FINISH_DRY_RUN"' in exported


# ── 2. the real database.py + turn_finisher in a fresh process (mongomock instead of MongoDB) ────────────────────────
IMPORT = r"""
import asyncio, json
import mongomock, pymongo
pymongo.MongoClient = mongomock.MongoClient            # database.py does `from pymongo import MongoClient`
import database, turn_finisher as tf

async def starter():
    tf.start()
    return {"started": tf._state["started"], "tasks": len(tf._state["tasks"]), "running": tf.is_running()}

state = asyncio.run(starter())
handle = getattr(database, "turn_finishing_collection", None)
names = database.db3.list_collection_names()
print(json.dumps({
    "handle": handle is not None,
    "handle_name": getattr(handle, "name", None),
    "write_concern": str(getattr(handle, "write_concern", None)) if handle is not None else None,
    "collection_created": "flintel_turn_finishing" in names,
    "mode": tf.mode(), "collection_seen_by_queue": tf._collection() is not None, "start": state,
}))
"""


def test_disabled_defines_no_handle_creates_nothing_and_starts_no_task():
    state = _run(IMPORT)
    assert state["handle"] is False and state["collection_created"] is False
    assert state["mode"] == "legacy" and state["collection_seen_by_queue"] is False
    assert state["start"] == {"started": False, "tasks": 0, "running": False}


@pytest.mark.parametrize("value", ["1", "yes", "tru", "false"])
def test_a_non_true_value_keeps_the_queue_off(value):
    state = _run(IMPORT, TURN_FINISH_ENABLED=value)
    assert state["handle"] is False and state["collection_created"] is False and state["start"]["tasks"] == 0


@pytest.mark.parametrize("dry_run", [None, "true", "false"])
def test_explicit_enabling_defines_the_handle_but_creates_nothing_and_stays_a_dry_run(dry_run):
    extra = {"TURN_FINISH_ENABLED": "TRUE"}
    if dry_run is not None:
        extra["TURN_FINISH_DRY_RUN"] = dry_run
    state = _run(IMPORT, **extra)
    assert state["handle"] is True and state["handle_name"] == "flintel_turn_finishing"
    assert "majority" in state["write_concern"]
    assert state["collection_created"] is False                       # a client-side handle: no collection, no index
    assert state["mode"] == "dry_run"                                 # TURN_FINISH_DRY_RUN=false does NOT start processing yet
    assert state["start"] == {"started": False, "tasks": 0, "running": False}


# ── 3. exactly what a dry run permits ─────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def queue(monkeypatch):
    coll = mongomock.MongoClient()["turn_queue_test"].turn_finishing
    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace(turn_finishing_collection=coll))
    return coll


def _flags(monkeypatch, enabled, dry_run=True):
    import config
    monkeypatch.setattr(config, "TURN_FINISH_ENABLED", enabled, raising=False)
    monkeypatch.setattr(config, "TURN_FINISH_DRY_RUN", dry_run, raising=False)


@pytest.fixture
def spy(monkeypatch):
    """process_fn stands for the model call: it must never be reached in a dry run."""
    calls = []
    monkeypatch.setitem(tf._state, "process_fn", lambda *a: calls.append(a) or ("done", None))
    return calls


def _snapshot(coll):
    return sorted(((d["_id"], d.get("lease_owner"), d.get("attempts"), d.get("claims"), d.get("due_at")) for d in coll.find({})),
                  key=lambda r: r[0])


@pytest.mark.parametrize("dry_run", [True, False])
def test_dry_run_permits_enqueue_and_nothing_else(monkeypatch, queue, spy, dry_run):
    _flags(monkeypatch, True, dry_run)                     # False is still a dry run in this version
    assert tf.mode() == "dry_run"
    # enqueue is permitted: one idempotent document, due now, never leased
    assert tf.enqueue("c1", "o1", "t1", due_in_seconds=0) is True and tf.enqueue("c1", "o1", "t1", due_in_seconds=0) is True
    assert tf.enqueue("c1", "o1", "t1", due_in_seconds=0, pull_forward=True) is False     # page-view intake: skipped, no write
    assert tf.enqueue("c9", "o9", "t9", due_in_seconds=0, pull_forward=True) is False
    assert queue.count_documents({}) == 1                                                  # t9 was NOT recorded
    jid = tf.job_id("c1", "o1", "t1")
    before = _snapshot(queue)
    assert before[0][1] is None and before[0][2] == 0
    # a foreign lease sits on a second document: it must stay untouched too
    queue.insert_one({"_id": "c2|o2|t2", "chat_id": "c2", "owner_key": "o2", "topic_key": "t2", "attempts": 0,
                      "due_at": datetime.now(timezone.utc) - timedelta(seconds=5), "lease_owner": "someone-else"})
    before = _snapshot(queue)

    # forbidden: claiming, processing, leasing, releasing, waking, workers
    assert tf.claim() is None and tf.claim(only_ids=[jid]) is None
    assert tf.process_one() is False and tf.process_due() == 0 and tf.process_one(only_ids=[jid]) is False
    assert tf.hold_for_stream("c1", "o1", "t1") is False and tf.hold_for_stream("c1", "o1", "t1", force=True) is False
    assert tf.worker_holds("c2", "o2", "t2") is False                              # no worker can exist
    assert tf.release_stream("c1", "o1", "t1", "stream:abc") is False
    assert tf.release_stream("c2", "o2", "t2", "someone-else") is False

    def no_thread(*a, **k):
        raise AssertionError("a dry run must not start a thread")
    monkeypatch.setattr(threading, "Thread", no_thread)
    assert tf.wake([jid]) == "none" and tf.wake() == "none"

    async def start_it():
        tf.start()
        return list(tf._state["tasks"]), tf.is_running()
    monkeypatch.setitem(tf._state, "started", False)
    tasks, running = run_async(start_it)
    assert tasks == [] and running is False
    assert spy == []                                                                # the model/turn finisher was never reached
    assert _snapshot(queue) == before                                               # not one field changed


def test_a_dry_run_never_calls_the_model_nor_finishes_a_turn(monkeypatch, fin):
    """Whole app flow with the real index/routes code: a new turn is recorded; older unfinished turns are recorded on a page
    view; nothing is scanned, answered, retried or completed, and the stream path is exactly the legacy one."""
    f = fin
    _flags(monkeypatch, True, True)
    idx, queue_coll, calls = f["idx"], f["queue"], f["calls"]
    _seed(f["chats"], "d1", [_turn("Find people looking for a CRM", 600), _chat_turn("Hello", "Hi there!")])
    resp, elapsed, tasks = _view(f, "d1")                                           # a page view must NOT record older turns
    assert resp.status_code == 200 and elapsed < 1.0 and tasks.tasks == []
    _seed(f["chats"], "d2", [])
    idx.add_search_to_chat("d2", OWNER, "Another research question", "another research question", ["crm"], "reddit",
                           evidence_required=15)                                    # a new turn is recorded too
    recorded = {d["_id"]: d for d in queue_coll.find({})}
    assert tf.job_id("d1", OWNER, "find people looking for a crm") not in recorded          # older turn: not recorded on a page view
    assert tf.job_id("d2", OWNER, "another research question") in recorded
    assert all(d.get("lease_owner") is None and d.get("attempts") == 0 and not d.get("claims") for d in recorded.values())
    _start_worker(tf)                                                               # startup hook: must start nothing
    assert tf._state["tasks"] == [] and tf.is_running() is False
    for d in recorded.values():                                                     # even when everything is overdue
        queue_coll.update_one({"_id": d["_id"]}, {"$set": {"due_at": datetime.now(timezone.utc) - timedelta(seconds=30)}})
    time.sleep(1.0)                                                                 # several sweep intervals (0.2 s in this fixture)
    assert calls["scan"] == 0 and calls["analyze"] == 0 and calls["fallback"] == 0
    assert _msg(f["chats"], "d1", "Find people looking for a CRM")["results"] is None
    assert _msg(f["chats"], "d1", "Find people looking for a CRM")["claude_answer"] is None
    assert _msg(f["chats"], "d2", "Another research question")["claude_answer"] is None
    assert tf.hold_for_stream("d2", OWNER, "another research question") is False    # the stream proceeds as in legacy mode
    assert all(d.get("lease_owner") is None for d in queue_coll.find({}))


# ── 4. the flag unset: unchanged behaviour (the injected-collection tests keep working) ───────────────────────────────
def test_with_the_flag_unset_the_queue_behaves_as_before(monkeypatch, queue, spy):
    _flags(monkeypatch, False, True)
    assert tf.mode() == "legacy"
    assert tf.enqueue("c1", "o1", "t1", due_in_seconds=0) is True
    token = tf.hold_for_stream("c1", "o1", "t1")
    assert isinstance(token, str) and token.startswith(tf.STREAM_LEASE_PREFIX)     # a stream takes its lease as before
    assert tf.release_stream("c1", "o1", "t1", token) is True                       # hands the turn back and wakes a thread
    deadline = time.time() + 5
    while queue.count_documents({}) and time.time() < deadline:                     # the legacy wake thread processes it
        time.sleep(0.05)
    assert spy == [("c1", "o1", "t1")] and queue.count_documents({}) == 0           # processed once, finished turn removed


def test_with_no_collection_everything_is_a_no_op(monkeypatch):
    _flags(monkeypatch, False, True)
    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace())
    assert tf._collection() is None
    assert tf.enqueue("c", "o", "t") is False and tf.claim() is None and tf.process_one() is False
    assert tf.hold_for_stream("c", "o", "t") is False and tf.worker_holds("c", "o", "t") is False
    assert tf.release_stream("c", "o", "t", "tok") is False and tf.backfill_if_due() == 0 and tf.wake() == "none"

    async def start_it():
        tf.start()
        return list(tf._state["tasks"])
    monkeypatch.setitem(tf._state, "started", False)
    assert run_async(start_it) == []                                            # disabled startup creates no task


class _CountingQueue:
    """A queue handle that counts every write (and delegates), to bound the work a request can cause."""

    def __init__(self, coll):
        self._c = coll
        self.writes = 0

    def __getattr__(self, name):
        return getattr(self._c, name)

    def update_one(self, *a, **k):
        self.writes += 1
        return self._c.update_one(*a, **k)

    def insert_one(self, *a, **k):
        self.writes += 1
        return self._c.insert_one(*a, **k)

    def find_one_and_update(self, *a, **k):
        self.writes += 1
        return self._c.find_one_and_update(*a, **k)


def _fifty_older_unfinished(idx):
    now = datetime.now(timezone.utc)
    msgs = [_turn(f"older question {i}", 3600 + i) for i in range(50)]
    return msgs


def test_a_page_view_with_50_older_unfinished_turns_makes_zero_queue_writes_in_dry_run(monkeypatch, fin):
    f = fin
    _flags(monkeypatch, True, True)
    counting = _CountingQueue(f["queue"])
    monkeypatch.setattr(sys.modules["database"], "turn_finishing_collection", counting, raising=False)
    ids = f["idx"].schedule_unfinished_turns("big", OWNER, _fifty_older_unfinished(f["idx"]))   # the real page-view hook
    assert ids == [] and counting.writes == 0 and f["queue"].count_documents({}) == 0
    _seed(f["chats"], "big2", _fifty_older_unfinished(f["idx"]))
    resp, _elapsed, _tasks = _view(f, "big2")                                       # and through the real view_chat route
    assert resp.status_code == 200 and counting.writes == 0 and f["queue"].count_documents({}) == 0


def test_the_legacy_page_view_intake_still_records_older_turns(monkeypatch, fin):
    f = fin
    _flags(monkeypatch, False, True)                                                # flag unset: exactly the old behaviour
    assert tf.mode() == "legacy"
    woken = []
    monkeypatch.setattr(tf, "wake", lambda ids=None: woken.append(ids) or "worker")      # observe the wake, do not process
    ids = f["idx"].schedule_unfinished_turns("big", OWNER, _fifty_older_unfinished(f["idx"])[:6])
    assert len(ids) == 6 and f["queue"].count_documents({}) == 6 and woken == [ids]


def test_the_mode_fails_closed_when_the_configuration_cannot_be_read(monkeypatch, queue, spy):
    monkeypatch.setitem(sys.modules, "config", None)                                # `import config` now raises ImportError
    assert tf.mode() == "dry_run"
    assert tf.claim() is None and tf.process_one() is False and tf.hold_for_stream("c", "o", "t") is False
    assert tf.enqueue("c", "o", "t", pull_forward=True) is False and tf.wake(["x"]) == "none"
    assert spy == []

    async def start_it():
        tf.start()                                                                  # must not raise on the log line either
        return list(tf._state["tasks"])
    monkeypatch.setitem(tf._state, "started", False)
    assert run_async(start_it) == []


def test_the_queue_never_touches_the_credit_system():
    text = (ROOT / "turn_finisher.py").read_text(encoding="utf-8")
    assert "search_credits" not in text and "credit" not in text.lower()
