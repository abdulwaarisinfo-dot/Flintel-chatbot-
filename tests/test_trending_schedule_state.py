"""
tests/test_trending_schedule_state.py — persisted 12-hour schedule for the
trending-keywords cycle (scheduler.py + database.auto_keywords_collection).

Deterministic: a controllable clock, a fake asyncio.sleep that advances it and
stops the (infinite) loop after N sleeps, an in-memory fake collection, and
stub `database` / `google` / `logics` modules (sys.modules restored after each
test). No real Mongo / Claude / RapidAPI. NOTE: the fake collection serialises
find_one_and_update with a lock — that models, but does not prove, real Mongo
atomicity.
"""
import asyncio
import copy
import importlib
import logging
import sys
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError

T0 = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)
INTERVAL = 43200
SECRET = "mongodb+srv://user:S3CR3T@cluster"


class Stop(BaseException):
    """Ends the infinite loop from the fake sleep (not an Exception, so the
    scheduler's own `except Exception` handlers cannot swallow it)."""


def _cmp(v):
    """Compare datetimes as UTC instants (naive = UTC), like Mongo does."""
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


class FakeStateCollection:
    def __init__(self, naive=False):
        self.docs = {}
        self.lock = threading.Lock()
        self.naive = naive                 # store datetimes naive (pymongo default tz_aware=False)
        self.fail = {"find_one": 0, "find_one_and_update": 0, "update_one": 0, "insert_one": 0}
        self.calls = {k: 0 for k in self.fail}
        self.dup_errors = 0
        self.read_barrier = None

    def _store(self, v):
        if isinstance(v, datetime):
            v = v.astimezone(timezone.utc).replace(microsecond=(v.microsecond // 1000) * 1000)
            return v.replace(tzinfo=None) if self.naive else v
        return v

    def _maybe_fail(self, op):
        self.calls[op] += 1
        if self.fail[op]:
            self.fail[op] -= 1
            raise ServerSelectionTimeoutError(f"cannot reach {SECRET}")

    def _match_field(self, doc, field, cond):
        val = doc.get(field)
        if cond is None:
            return val is None
        if isinstance(cond, dict):
            for op, ref in cond.items():
                if val is None:
                    return False
                if op == "$lte" and not _cmp(val) <= _cmp(ref):
                    return False
                if op == "$lt" and not _cmp(val) < _cmp(ref):
                    return False
            return True
        return _cmp(val) == _cmp(self._store(cond))

    def _match(self, doc, flt):
        for k, cond in flt.items():
            if k == "$or":
                if not any(self._match(doc, sub) for sub in cond):
                    return False
            elif not self._match_field(doc, k, cond):
                return False
        return True

    def _apply(self, doc, update):
        for k, v in update.get("$set", {}).items():
            doc[k] = self._store(v)
        for k in update.get("$unset", {}):
            doc.pop(k, None)

    def find_one(self, flt):
        self._maybe_fail("find_one")
        with self.lock:
            d = self.docs.get(flt["_id"])
            snapshot = copy.deepcopy(d) if d else None
        if self.read_barrier is not None:
            # both readers have taken their snapshot before either may go on
            # (and insert) — forces the "both saw no document" race
            self.read_barrier.wait(timeout=5)
        return snapshot

    def insert_one(self, doc):
        self._maybe_fail("insert_one")
        with self.lock:
            if doc["_id"] in self.docs:
                self.dup_errors += 1
                raise DuplicateKeyError("E11000 duplicate key")
            self.docs[doc["_id"]] = {k: self._store(v) for k, v in doc.items()}

    def find_one_and_update(self, flt, update, return_document=None):
        self._maybe_fail("find_one_and_update")
        with self.lock:
            d = self.docs.get(flt["_id"])
            if d is None or not self._match(d, flt):
                return None
            self._apply(d, update)
            return copy.deepcopy(d)

    def update_one(self, flt, update, upsert=False):
        self._maybe_fail("update_one")
        with self.lock:
            d = self.docs.get(flt["_id"])
            if d is not None and self._match(d, flt):
                self._apply(d, update)
                return
            if upsert:
                if d is not None:
                    self.dup_errors += 1
                    raise DuplicateKeyError("E11000 duplicate key")
                new = {"_id": flt["_id"]}
                self._apply(new, update)
                self.docs[flt["_id"]] = new


class Env:
    def __init__(self):
        self.now = T0
        self.sleeps = []
        self.max_sleeps = 1
        self.cycles = []
        self.keywords = ["a", "b", "c"]
        self.gen_exc = None


@pytest.fixture
def env(monkeypatch):
    e = Env()
    coll = FakeStateCollection()

    def fake_generate():
        e.cycles.append(e.now)
        if e.gen_exc:
            raise e.gen_exc
        return e.keywords

    db = types.ModuleType("database")
    db.google_posts_collection = object()
    db.auto_keywords_collection = coll
    gg = types.ModuleType("google")
    gg.search_google_for_reddit_posts = lambda kws, c, tag: ["stub"] * 2
    lgm = types.ModuleType("logics")
    lgm.generate_trending_keywords_via_web_search = fake_generate
    lgm.enqueue_search_job = lambda topic, kws, plat: None
    monkeypatch.setitem(sys.modules, "database", db)
    monkeypatch.setitem(sys.modules, "google", gg)
    monkeypatch.setitem(sys.modules, "logics", lgm)
    monkeypatch.delitem(sys.modules, "scheduler", raising=False)
    import config
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", 200)
    mod = importlib.import_module("scheduler")
    monkeypatch.setattr(mod, "_utcnow", lambda: e.now)

    async def fake_sleep(seconds):
        e.sleeps.append(seconds)
        e.now = e.now + timedelta(seconds=seconds)
        if len(e.sleeps) >= e.max_sleeps:
            raise Stop()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    e.mod, e.coll, e.db = mod, coll, db
    yield e
    sys.modules.pop("scheduler", None)


def run_loop(e, max_sleeps):
    e.max_sleeps = len(e.sleeps) + max_sleeps
    with pytest.raises(Stop):
        asyncio.run(e.mod._trending_keywords_loop())


def state(e):
    return e.coll.docs.get("trending_keywords")


# 1
def test_1_no_saved_state_first_cycle_runs_now(env, caplog):
    caplog.set_level(logging.INFO)
    run_loop(env, 1)
    assert env.cycles == [T0]                                   # ran immediately
    st = state(env)
    assert st["next_due_at"] == T0 + timedelta(seconds=INTERVAL)
    assert st["last_started_at"] == T0 and st["last_status"] == "ok"
    assert st["last_keywords_count"] == 3 and st["last_stubs_count"] == 2 and st["last_job_queued"] is True
    assert set(st) == {"_id", "next_due_at", "last_started_at", "last_status", "last_finished_at",
                       "last_keywords_count", "last_stubs_count", "last_job_queued"}   # no keyword list
    assert env.sleeps == [3600]                                  # then waits (capped at 1h)
    assert "next cycle due now (no saved state)" in caplog.text


# 2
def test_2_future_due_time_not_run_early(env):
    due = T0 + timedelta(seconds=5000)
    env.coll.docs["trending_keywords"] = {"_id": "trending_keywords", "next_due_at": due}
    run_loop(env, 3)
    assert env.sleeps[:2] == [3600, 1400]                         # capped, then the remainder
    assert env.cycles == [due]                                    # ran exactly at the due time
    assert state(env)["next_due_at"] == due + timedelta(seconds=INTERVAL)


# 3
def test_3_restart_keeps_persisted_due_time(env, caplog):
    run_loop(env, 1)                                              # process 1: first cycle
    assert len(env.cycles) == 1
    env.now = T0 + timedelta(seconds=600)                         # restart 10 minutes later
    caplog.clear()
    caplog.set_level(logging.INFO)
    run_loop(env, 3)                                              # process 2: same collection
    assert len(env.cycles) == 1                                   # no extra cycle
    assert env.now <= T0 + timedelta(seconds=INTERVAL)
    assert (f"next cycle due at {(T0 + timedelta(seconds=INTERVAL)).isoformat()} "
            "(from flintel_auto_keywords)") in caplog.text


# 4
def test_4_concurrent_claims_only_one_wins(env):
    env.coll.docs["trending_keywords"] = {"_id": "trending_keywords", "next_due_at": T0}
    barrier = threading.Barrier(2)
    results = []

    def claimer():
        barrier.wait()
        results.append(env.mod._claim_due_cycle(T0))
    ts = [threading.Thread(target=claimer) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(1 for r in results if r) == 1

    # and two loops' steps racing on a due document: one cycle total
    env.coll.docs["trending_keywords"] = {"_id": "trending_keywords", "next_due_at": T0}
    env.cycles.clear()

    async def both():
        return await asyncio.gather(env.mod._persisted_schedule_step(False),
                                    env.mod._persisted_schedule_step(False))
    waits = asyncio.run(both())
    assert len(env.cycles) == 1 and sorted(waits) == [0, 60]


# 5
@pytest.mark.parametrize("case,expected", [("exception", "failed"), ("no_keywords", "skipped_no_keywords")])
def test_5_cycle_outcomes_keep_claim_deadline(env, case, expected):
    if case == "exception":
        env.gen_exc = RuntimeError("claude down")
    else:
        env.keywords = []
    run_loop(env, 1)                                              # scheduler stays alive (sleeps)
    st = state(env)
    assert st["last_status"] == expected
    assert st["next_due_at"] == T0 + timedelta(seconds=INTERVAL)  # unchanged by completion
    assert st["last_finished_at"] == T0
    if case == "no_keywords":
        assert st["last_keywords_count"] == 0 and "last_stubs_count" not in st
    assert env.sleeps == [3600]


# 6
def test_6_read_failure_falls_back_to_in_memory_timer(env, caplog):
    env.coll.fail["find_one"] = 99
    caplog.set_level(logging.WARNING)
    run_loop(env, 1)
    assert env.sleeps == [INTERVAL] and env.cycles == []           # legacy: sleep 12h first
    assert "Mongo schedule unavailable (ServerSelectionTimeoutError)" in caplog.text
    assert "S3CR3T" not in caplog.text and "mongodb+srv" not in caplog.text


# 7
def test_7_claim_failure_falls_back(env, caplog):
    env.coll.docs["trending_keywords"] = {"_id": "trending_keywords", "next_due_at": T0}
    env.coll.fail["find_one_and_update"] = 99
    env.coll.fail["update_one"] = 99
    caplog.set_level(logging.WARNING)
    run_loop(env, 2)
    assert env.sleeps[0] == INTERVAL
    assert env.cycles == [T0 + timedelta(seconds=INTERVAL)]       # fallback cycle after 12h, no claim
    assert "Mongo schedule unavailable (ServerSelectionTimeoutError)" in caplog.text
    assert "S3CR3T" not in caplog.text


# 8
def test_8_recovery_returns_to_persisted_schedule(env):
    env.coll.fail["find_one"] = 1                                  # one failed read, then Mongo works
    run_loop(env, 2)
    fallback_at = T0 + timedelta(seconds=INTERVAL)
    assert env.cycles == [fallback_at]                             # exactly one cycle, no duplicate
    st = state(env)
    assert st["next_due_at"] == fallback_at + timedelta(seconds=INTERVAL)   # fallback recorded
    assert st["last_status"] == "ok"
    assert env.sleeps == [INTERVAL, 3600]                          # back on the persisted schedule
    assert env.coll.calls["find_one"] == 2


def test_8b_outage_retry_is_bounded(env):
    env.coll.fail["find_one"] = 99
    env.coll.fail["update_one"] = 99
    run_loop(env, 3)
    assert env.sleeps == [INTERVAL, INTERVAL, INTERVAL]            # one Mongo attempt per interval
    assert env.coll.calls["find_one"] == 3 and len(env.cycles) == 2


# 9
def test_9_naive_mongo_datetimes_are_utc(env, monkeypatch):
    env.coll.naive = True
    due = (T0 + timedelta(seconds=1800)).replace(tzinfo=None)      # naive = UTC
    env.coll.docs["trending_keywords"] = {"_id": "trending_keywords", "next_due_at": due}
    run_loop(env, 2)
    assert env.sleeps[0] == 1800                                    # read as UTC, not local time
    assert env.cycles == [T0 + timedelta(seconds=1800)]
    st = state(env)
    assert st["next_due_at"].tzinfo is None                         # stored like Mongo would
    assert env.mod._aware_utc(st["next_due_at"]) == T0 + timedelta(seconds=1800 + INTERVAL)
    assert env.mod._aware_utc(datetime(2026, 1, 1, 5, 0)) == datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)


# 10
def test_10_first_initialisation_race_one_winner(env):
    env.coll.read_barrier = threading.Barrier(2)                   # both see "no document"

    async def both():
        return await asyncio.gather(env.mod._persisted_schedule_step(True),
                                    env.mod._persisted_schedule_step(True))
    waits = asyncio.run(both())
    env.coll.read_barrier = None
    assert env.coll.dup_errors == 1                                 # second insert lost, handled
    assert len(env.cycles) == 1 and sorted(waits) == [0, 60]
    assert state(env)["next_due_at"] == T0 + timedelta(seconds=INTERVAL)


# 11
def test_11_existing_trending_job_tests_still_pass():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_trending_job.py", "-q", "-p", "no:cacheprovider"],
                       cwd=str(root), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:]
