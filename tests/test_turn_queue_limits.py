"""
tests/test_turn_queue_limits.py - durable turn queue, SLICES 2-5: observability, scope gates, the atomic hourly PASS budget,
retry / parking / lease safety.

Everything here is a TEST DOUBLE: mongomock collections, a spy instead of the model, a fake clock. The shipped hard guard
`turn_finisher._PROCESSING_GOVERNED` stays False; the tests that need the governed ("active") code path switch it on for the
duration of ONE test only (monkeypatch), never through an environment variable or a helper in the product code, and the first
test proves that no environment combination reaches the active mode in the real module.

The budget is a PASS budget (an upper bound on queue processing attempts per UTC hour), NOT a money cap.
"""
import asyncio
import json
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

mongomock = pytest.importorskip("mongomock")
ROOT = Path(__file__).resolve().parent.parent

import turn_finisher as tf  # noqa: E402
from tests.test_turn_queue_wiring import (  # noqa: F401,E402  (fixtures + helpers)
    OWNER, PARSE, _CountingQueue, run_async, _fifty_older_unfinished, _flags, _run, _seed, _turn, _view, app_env, browser, env, fin, queue, spy,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 25, 10, 30, 0, tzinfo=UTC)
SECRET_OWNER = "private.person@example.test"
SECRET_QUERY = "find buyers for my secret product"


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def clock(monkeypatch):
    holder = {"now": T0}
    monkeypatch.setattr(tf, "_now", lambda: holder["now"])
    return holder


@pytest.fixture
def cfg(monkeypatch):
    def setter(**values):
        import config                                   # looked up when called: other fixtures may re-import config
        for k, v in values.items():
            monkeypatch.setattr(config, k, v, raising=False)
    return setter


@pytest.fixture
def active(monkeypatch, queue, clock, cfg):
    """The governed path, TEST ONLY: the flags on, the hard guard switched on for this test alone, a spy for the model."""
    _flags(monkeypatch, True, False)
    monkeypatch.setattr(tf, "_PROCESSING_GOVERNED", True)
    monkeypatch.setattr(tf, "_stats", {k: (None if k.startswith("last") or k == "budget_logged_window" else 0) for k in tf._stats})
    monkeypatch.setitem(tf._state, "budget_blocked_until", None)
    monkeypatch.setitem(tf._state, "heartbeat_pruned_at", None)
    box = types.SimpleNamespace(calls=[], result=("done", None), queue=queue, clock=clock, cfg=cfg)

    def fn(*a):
        box.calls.append(a)
        if isinstance(box.result, BaseException):
            raise box.result
        return box.result
    monkeypatch.setitem(tf._state, "process_fn", fn)
    assert tf.mode() == "active"
    return box


def _job(coll, chat="c1", owner="o1", topic="t1", created=None, due=None, **extra):
    now = tf._now()
    doc = {"_id": tf.job_id(chat, owner, topic), "chat_id": chat, "owner_key": owner, "topic_key": topic,
           "due_at": due or now - timedelta(seconds=1), "lease_owner": None, "attempts": 0, "created_at": created or now}
    doc.update(extra)
    coll.insert_one(doc)
    return doc["_id"]


def _get(coll, jid):
    return coll.find_one({"_id": jid})


# ── the hard guard ────────────────────────────────────────────────────────────────────────────────────────────────────
MODE_SCRIPT = r"""
import json, config, turn_finisher as tf
print(json.dumps({"mode": tf.mode(), "governed": tf._PROCESSING_GOVERNED}))
"""


def test_the_shipped_hard_guard_is_false_and_no_environment_reaches_the_active_mode():
    assert tf._PROCESSING_GOVERNED is False
    for en in (None, "true", "TRUE", "false", "1"):
        for dr in (None, "true", "false", "garbage"):
            extra = {}
            if en is not None:
                extra["TURN_FINISH_ENABLED"] = en
            if dr is not None:
                extra["TURN_FINISH_DRY_RUN"] = dr
            out = _run(MODE_SCRIPT, **extra)
            assert out["governed"] is False and out["mode"] in ("legacy", "dry_run"), (en, dr, out)
    for path in ROOT.glob("*.py"):                                                  # nothing in the product flips it
        text = path.read_text(encoding="utf-8")
        assert "_PROCESSING_GOVERNED = True" not in text and "_PROCESSING_GOVERNED=True" not in text, path.name


# ── SLICE 2: observability ────────────────────────────────────────────────────────────────────────────────────────────
def _seed_report_world(coll, clock):
    now = clock["now"]
    _job(coll, "chatA", SECRET_OWNER, SECRET_QUERY, due=now - timedelta(seconds=90))                      # due, oldest
    _job(coll, "chatB", SECRET_OWNER, "second " + SECRET_QUERY, due=now - timedelta(seconds=10))          # due
    _job(coll, "chatC", "o3", "t3", due=now + timedelta(seconds=600), lease_owner="tok")                  # leased
    _job(coll, "chatD", "o4", "t4", due=now - timedelta(seconds=5), lease_owner="old")                    # lease expired
    _job(coll, "chatE", "o5", "t5", due=tf.FAR_FUTURE, parked=True)                                       # parked
    _job(coll, "chatF", "o6", "t6", due=now + timedelta(seconds=60), needs_attention=True)                # needs attention
    coll.insert_one({"_id": tf.BUDGET_PREFIX + tf._window_id(now), "used": 4, "window": tf._window_id(now)})
    coll.insert_one({"_id": tf.HEARTBEAT_PREFIX + "123-abcdef", "last_seen": now - timedelta(seconds=20), "mode": "active"})
    coll.insert_one({"_id": tf.BACKFILL_DOC_ID, "due_at": tf.FAR_FUTURE})


def test_report_is_read_only_bounded_and_contains_no_user_content(active):
    q = _CountingQueue(active.queue)
    sys.modules["database"].turn_finishing_collection = q
    _seed_report_world(active.queue, active.clock)
    active.cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=10)
    rep = tf.report()
    assert q.writes == 0                                                            # read-only
    assert rep["queue"] == {"total": 6, "due": 2, "leased": 1, "lease_expired": 1, "parked": 1, "needs_attention": 1,
                            "oldest_due_age_seconds": 90}
    assert rep["budget"] == {"window": "2026-10-25T10", "used": 4, "limit": 10, "remaining": 6, "unit": "passes (not money)"}
    assert [h["age_seconds"] for h in rep["heartbeats"]] == [20] and rep["errors"] == [] and rep["capped"] is False
    blob = json.dumps(rep)
    for secret in (SECRET_OWNER, SECRET_QUERY, "chatA", "private", "secret product"):
        assert secret not in blob
    assert rep["processing_governed"] is True and rep["mode"] == "active"


def test_report_counts_are_capped_and_a_failing_query_is_reported_not_raised(active, monkeypatch):
    _seed_report_world(active.queue, active.clock)
    monkeypatch.setattr(tf, "REPORT_COUNT_CAP", 3)
    rep = tf.report()
    assert rep["queue"]["total"] == 3 and rep["capped"] is True

    class Broken:
        def __getattr__(self, name):
            return getattr(active.queue, name)

        def count_documents(self, *a, **k):
            raise RuntimeError("boom with secret " + SECRET_QUERY)
    sys.modules["database"].turn_finishing_collection = Broken()
    rep = tf.report()
    assert "RuntimeError" in rep["errors"] and SECRET_QUERY not in json.dumps(rep)          # class name only


def test_report_without_a_collection_is_a_harmless_stub(monkeypatch):
    _flags(monkeypatch, False, True)
    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace())
    rep = tf.report()
    assert rep["collection"] is False and rep["mode"] == "legacy" and "queue" not in rep


def test_heartbeat_writes_only_its_own_document_and_is_never_claimed(active):
    _job(active.queue, "c1", "o1", "t1", due=T0 + timedelta(hours=5))
    assert tf.heartbeat() is True
    beat = active.queue.find_one({"_id": tf.HEARTBEAT_PREFIX + tf._PROCESS})
    assert tf._aware(beat["last_seen"]) == T0 and beat["mode"] == "active"
    assert active.queue.count_documents({}) == 2
    old = tf.HEARTBEAT_PREFIX + "999-old"
    active.queue.insert_one({"_id": old, "last_seen": T0 - timedelta(days=3)})
    active.clock["now"] = T0 + timedelta(hours=2)                                   # prune window passed
    assert tf.heartbeat() is True and active.queue.find_one({"_id": old}) is None
    active.queue.update_one({"_id": tf.HEARTBEAT_PREFIX + tf._PROCESS}, {"$set": {"due_at": T0 - timedelta(days=1)}})
    active.queue.insert_one({"_id": tf.BUDGET_PREFIX + "x", "used": 0, "due_at": T0 - timedelta(days=1)})
    assert tf.claim() is None                                                       # budget / heartbeat documents are never jobs
    assert active.calls == []


def test_logs_carry_a_short_reference_and_never_the_query_owner_or_error_text(active, caplog):
    caplog.set_level("INFO", logger="flintel-turn-finisher")
    jid = _job(active.queue, "chatX", SECRET_OWNER, SECRET_QUERY)
    active.result = RuntimeError("model said: " + SECRET_QUERY + " for " + SECRET_OWNER)
    assert tf.process_one() is True
    active.result = ("done", None)
    _job(active.queue, "chatY", SECRET_OWNER, "another " + SECRET_QUERY)
    assert tf.process_one() is True
    tf.log_summary_if_due(force=True)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (SECRET_OWNER, SECRET_QUERY, "chatX", "chatY", "model said"):
        assert secret not in text, secret
    assert f"ref={tf._ref(jid)}" in text and "error=RuntimeError" in text and "turn queue: mode=active" in text
    assert _get(active.queue, jid)["last_error"] == "RuntimeError"                  # stored as a class name, not the message


def test_summary_log_is_rate_limited_and_status_is_in_memory(active, caplog):
    caplog.set_level("INFO", logger="flintel-turn-finisher")
    assert tf.log_summary_if_due() is not None and tf.log_summary_if_due() is None   # second call within the interval
    active.clock["now"] = T0 + timedelta(seconds=301)
    assert tf.log_summary_if_due() is not None
    st = tf.status()
    assert st["mode"] == "active" and st["processing_governed"] is True and st["tasks"] == 0 and "stats" in st


# ── SLICE 3: scope gates ──────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,raw,expected", [
    ("TURN_FINISH_BACKFILL_MAX_CHATS", None, 20), ("TURN_FINISH_BACKFILL_MAX_CHATS", "0", 20),
    ("TURN_FINISH_BACKFILL_MAX_CHATS", "-5", 20), ("TURN_FINISH_BACKFILL_MAX_CHATS", "abc", 20),
    ("TURN_FINISH_BACKFILL_MAX_CHATS", "1000000000", 20), ("TURN_FINISH_BACKFILL_MAX_CHATS", "200", 200),
    ("TURN_FINISH_BACKFILL_MAX_CHATS", "1", 1), ("TURN_FINISH_BACKFILL_MAX_TURNS", "0", 50),
    ("TURN_FINISH_BACKFILL_MAX_AGE_DAYS", "31", 3), ("TURN_FINISH_BACKFILL_MAX_AGE_DAYS", "30", 30),
    ("TURN_FINISH_ONDEMAND_MAX_AGE_HOURS", None, 0), ("TURN_FINISH_ONDEMAND_MAX_AGE_HOURS", "24", 24),
    ("TURN_FINISH_ONDEMAND_MAX_AGE_HOURS", "-1", 0), ("TURN_FINISH_ONDEMAND_MAX_TURNS", "0", 5),
    ("TURN_FINISH_MAX_PASSES_PER_HOUR", None, 20), ("TURN_FINISH_MAX_PASSES_PER_HOUR", "0", 20),
    ("TURN_FINISH_MAX_PASSES_PER_HOUR", "1001", 20), ("TURN_FINISH_MAX_PASSES_PER_HOUR", "7", 7),
    ("TURN_FINISH_ATTEMPTS_WARN", "0", 8), ("TURN_FINISH_MAX_AGE_DAYS", "400", 30), ("TURN_FINISH_SUMMARY_SECONDS", "5", 300)])
def test_the_limits_are_parsed_safely_and_can_never_be_zero_or_unbounded(name, raw, expected):
    extra = {} if raw is None else {name: raw}
    out = _run("import json, config; print(json.dumps(getattr(config, %r)))" % name, **extra)
    assert out == expected


def test_backfill_is_off_by_default_and_never_touches_the_chats_outside_legacy(active, cfg):
    chats = mongomock.MongoClient()["c"].chats
    chats.insert_one({"chat_id": "c", "owner_key": "o", "messages": [_turn("q", 5)]})

    class Spy:
        finds = 0

        def find(self, *a, **k):
            Spy.finds += 1
            return chats.find(*a, **k)
    cfg(TURN_FINISH_BACKFILL_ENABLED=False)
    assert tf.backfill_if_due(Spy()) == 0 and Spy.finds == 0
    assert active.queue.count_documents({}) == 0


class _SpyChats:
    """Delegates to a mongomock chats collection and records the .limit() it receives."""

    def __init__(self, coll):
        self.coll, self.limits = coll, []

    def find(self, *a, **k):
        outer = self
        cur = self.coll.find(*a, **k)

        class Wrap:
            def sort(self, *s):
                nonlocal cur
                cur = cur.sort(*s)
                return self

            def limit(self, n):
                outer.limits.append(n)
                nonlocal cur
                cur = cur.limit(n)
                return self

            def max_time_ms(self, ms):
                outer.limits.append(("max_time_ms", ms))
                return self

            def __iter__(self):
                return iter(cur)
        return Wrap()


def _chat(chats, i, seconds_ago):
    chats.insert_one({"chat_id": f"chat{i:03d}", "owner_key": f"o{i}", "messages": [_turn(f"question {i}", seconds_ago)]})


def test_enabled_backfill_is_bounded_by_chats_turns_age_and_never_limit_zero(active, cfg):
    chats = mongomock.MongoClient()["c"].chats
    for i in range(30):
        _chat(chats, i, 3600)                                                       # recent (1 hour old)
    for i in range(30, 36):
        _chat(chats, i, 10 * 86400)                                                 # 10 days old: beyond the 3-day cutoff
    cfg(TURN_FINISH_BACKFILL_ENABLED=True, TURN_FINISH_BACKFILL_MAX_CHATS=0, TURN_FINISH_BACKFILL_MAX_TURNS=4)
    spy = _SpyChats(chats)
    # a bad configured value (0) falls back to the default (20 chats); the per-pass turn cap (4) then bounds the work
    assert tf.backfill_if_due(spy) == 4
    assert 0 not in spy.limits and 20 in spy.limits and any(isinstance(x, tuple) for x in spy.limits)
    docs = list(active.queue.find({"chat_id": {"$exists": True}}))
    assert len(docs) == 4 and all(d["lease_owner"] is None for d in docs)
    assert tf.backfill_if_due(spy) == 0                                             # the marker: once per interval
    active.clock["now"] = T0 + timedelta(seconds=tf.BACKFILL_INTERVAL_SECONDS + 1)
    cfg(TURN_FINISH_BACKFILL_MAX_TURNS=200)
    got = tf.backfill_if_due(spy)
    assert got >= 20 and active.queue.count_documents({"topic_key": {"$regex": "question 3[0-5]"}}) == 0    # old turns never


def test_page_view_intake_is_none_in_legacy_dry_run_and_default_active_then_bounded(monkeypatch, active, cfg):
    msgs = []
    for i in range(30):
        msgs.append(_turn(f"recent {i}", 3600 + i))                                 # 1 hour old
    for i in range(20):
        msgs.append(_turn(f"ancient {i}", 40 * 86400))                              # 40 days old
    assert tf.ondemand_topic_keys(msgs) == []                                       # active default: ONDEMAND_MAX_AGE_HOURS=0
    cfg(TURN_FINISH_ONDEMAND_MAX_AGE_HOURS=24, TURN_FINISH_ONDEMAND_MAX_TURNS=5)
    keys = tf.ondemand_topic_keys(msgs)
    assert len(keys) == 5 and all(k.startswith("recent") for k in keys)
    assert keys == ["recent 0", "recent 1", "recent 2", "recent 3", "recent 4"]     # newest first, capped
    monkeypatch.setattr(tf, "_PROCESSING_GOVERNED", False)                          # dry run
    assert tf.mode() == "dry_run" and tf.ondemand_topic_keys(msgs) == []
    _flags(monkeypatch, False, True)                                                # legacy: everything, as before
    assert tf.mode() == "legacy" and len(tf.ondemand_topic_keys(msgs)) == 50


def test_a_governed_page_view_makes_a_bounded_number_of_queue_writes(monkeypatch, cfg, fin):
    f = fin
    _flags(monkeypatch, True, False)
    monkeypatch.setattr(tf, "_PROCESSING_GOVERNED", True)
    cfg(TURN_FINISH_ONDEMAND_MAX_AGE_HOURS=24, TURN_FINISH_ONDEMAND_MAX_TURNS=5)
    monkeypatch.setattr(tf, "wake", lambda ids=None: "none")                        # observe, do not process
    counting = _CountingQueue(f["queue"])
    monkeypatch.setattr(sys.modules["database"], "turn_finishing_collection", counting, raising=False)
    ids = f["idx"].schedule_unfinished_turns("big", OWNER, _fifty_older_unfinished(f["idx"]))
    assert len(ids) == 5 and counting.writes <= 10                                  # 50 unfinished turns: at most 2 x cap writes
    cfg(TURN_FINISH_ONDEMAND_MAX_AGE_HOURS=0)
    counting.writes = 0
    assert f["idx"].schedule_unfinished_turns("big", OWNER, _fifty_older_unfinished(f["idx"])) == [] and counting.writes == 0


def test_recording_a_turn_never_marks_a_chat_turn_complete_or_failed(active, cfg):
    chats = mongomock.MongoClient()["c"].chats
    for i in range(5):
        _chat(chats, i, 3600)
    before = list(chats.find({}, {"_id": 0}))
    cfg(TURN_FINISH_BACKFILL_ENABLED=True, TURN_FINISH_ONDEMAND_MAX_AGE_HOURS=24)
    tf.backfill_if_due(chats)
    tf.ondemand_topic_keys([_turn("x", 5)])
    assert list(chats.find({}, {"_id": 0})) == before                               # the chats are untouched


# ── SLICE 4: the atomic hourly PASS budget ────────────────────────────────────────────────────────────────────────────
def test_the_budget_is_exhausted_exactly_at_its_limit(active, cfg):
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=3)
    got = [tf.take_pass() for _ in range(5)]
    assert [bool(g) for g in got] == [True, True, True, False, False]
    doc = active.queue.find_one({"_id": tf.BUDGET_PREFIX + "2026-10-25T10"})
    assert doc["used"] == 3 and doc["window"] == "2026-10-25T10"


@pytest.mark.parametrize("limit,threads,tries", [(7, 24, 3), (1, 16, 2), (50, 8, 20)])
def test_concurrent_first_time_initialisation_and_use_never_exceed_the_limit(active, cfg, limit, threads, tries):
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=limit)
    barrier, granted = threading.Barrier(threads), []
    lock = threading.Lock()

    def work():
        barrier.wait()                                                              # every thread hits the EMPTY window at once
        for _ in range(tries):
            wid = tf.take_pass()
            with lock:
                granted.append(wid)
    ts = [threading.Thread(target=work) for _ in range(threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok = [g for g in granted if g]
    assert len(ok) == min(limit, threads * tries)
    assert active.queue.find_one({"_id": tf.BUDGET_PREFIX + "2026-10-25T10"})["used"] == len(ok) <= limit


def test_window_changes_use_a_new_document_and_the_fixed_window_boundary_allows_two_limits(active, cfg):
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=2)
    late, early = T0.replace(minute=59, second=58), T0.replace(minute=59, second=58) + timedelta(seconds=3)
    assert tf.take_pass(late) and tf.take_pass(late) and tf.take_pass(late) is None
    assert tf.take_pass(early) and tf.take_pass(early) and tf.take_pass(early) is None      # next hour: a fresh document
    ids = sorted(d["_id"] for d in active.queue.find({"_id": {"$regex": "^" + tf.BUDGET_PREFIX}}))
    assert ids == [tf.BUDGET_PREFIX + "2026-10-25T10", tf.BUDGET_PREFIX + "2026-10-25T11"]
    assert [active.queue.find_one({"_id": i})["used"] for i in ids] == [2, 2]       # documented: up to 2 x limit across a boundary


def test_clock_skew_between_processes_cannot_reset_or_ping_pong_a_window(active, cfg):
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=2)
    ahead, behind = T0 + timedelta(hours=1), T0                                     # process A is an hour ahead of process B
    assert tf.take_pass(ahead) and tf.take_pass(ahead) and tf.take_pass(ahead) is None
    assert tf.take_pass(behind) and tf.take_pass(behind) and tf.take_pass(behind) is None
    assert tf.take_pass(ahead) is None                                              # B did not reset A's window
    used = {d["_id"]: d["used"] for d in active.queue.find({"_id": {"$regex": "^" + tf.BUDGET_PREFIX}})}
    assert used == {tf.BUDGET_PREFIX + "2026-10-25T11": 2, tf.BUDGET_PREFIX + "2026-10-25T10": 2}


class _FlakyBudget:
    """Wraps the queue: runs `hook` for the first find_one_and_update, then delegates (or raises)."""

    def __init__(self, coll, first_error=None, always_error=None, reread_error=None, returns=None):
        self._c, self.first_error, self.always_error, self.reread_error, self.returns = coll, first_error, always_error, reread_error, returns
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._c, name)

    def find_one_and_update(self, *a, **k):
        self.calls += 1
        if self.always_error is not None:
            raise self.always_error
        if self.first_error is not None and self.calls == 1:
            self._c.update_one({"_id": a[0]["_id"]}, {"$setOnInsert": {"window": "w"}, "$inc": {"used": 1}}, upsert=True)  # the racer
            raise self.first_error
        if self.returns is not None:
            return self.returns
        return self._c.find_one_and_update(*a, **k)

    def find_one(self, *a, **k):
        if self.reread_error is not None and a and isinstance(a[0], dict) and str(a[0].get("_id", "")).startswith(tf.BUDGET_PREFIX):
            raise self.reread_error
        return self._c.find_one(*a, **k)


def test_a_duplicate_key_race_is_retried_but_an_exhausted_budget_stays_exhausted(active, cfg):
    from pymongo.errors import DuplicateKeyError
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=3)
    flaky = _FlakyBudget(active.queue, first_error=DuplicateKeyError("E11000"))
    sys.modules["database"].turn_finishing_collection = flaky
    assert tf.take_pass() and flaky.calls == 2                                      # the racer used 1, the retry got the 2nd
    assert active.queue.find_one({"_id": tf.BUDGET_PREFIX + "2026-10-25T10"})["used"] == 2
    sys.modules["database"].turn_finishing_collection = active.queue
    assert tf.take_pass() and tf.take_pass() is None and tf.take_pass() is None     # real duplicate key when exhausted
    # a duplicate key whose re-read fails is not guessed: no budget
    sys.modules["database"].turn_finishing_collection = _FlakyBudget(
        active.queue, always_error=DuplicateKeyError("E11000"), reread_error=RuntimeError("down"))
    assert tf.take_pass() is None


@pytest.mark.parametrize("error", ["server_selection", "autoreconnect", "write_concern", "generic"])
def test_ambiguous_database_errors_fail_closed(active, cfg, error):
    from pymongo import errors as pe
    exc = {"server_selection": pe.ServerSelectionTimeoutError("t"), "autoreconnect": pe.AutoReconnect("x"),
           "write_concern": pe.WriteConcernError({"code": 64, "errmsg": "wc"}) if hasattr(pe, "WriteConcernError") else RuntimeError("wc"),
           "generic": RuntimeError("secret " + SECRET_QUERY)}[error]
    sys.modules["database"].turn_finishing_collection = _FlakyBudget(active.queue, always_error=exc)
    assert tf.take_pass() is None                                                   # no pass without a confirmed unit


@pytest.mark.parametrize("answer", [None, {"used": 0}, {"used": 99}, {"used": True}, {"nothing": 1}, "x"])
def test_an_unexpected_database_answer_means_no_budget(active, cfg, answer):
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=3)
    sys.modules["database"].turn_finishing_collection = _FlakyBudget(active.queue, returns=answer or {"used": "n/a"} if answer is None else answer)
    assert tf.take_pass() is None


def test_no_collection_means_no_budget(monkeypatch):
    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace())
    assert tf.take_pass() is None and tf.refund_pass("x") is False


def test_a_refund_only_lowers_the_counter_and_a_failed_refund_keeps_the_unit(active, cfg):
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=2)
    wid = tf.take_pass()
    assert tf.refund_pass(wid) is True and active.queue.find_one({"_id": wid})["used"] == 0
    assert tf.refund_pass(wid) is False and active.queue.find_one({"_id": wid})["used"] == 0     # never below 0
    wid = tf.take_pass()

    class NoRefund:
        def __getattr__(self, name):
            return getattr(active.queue, name)

        def update_one(self, *a, **k):
            raise RuntimeError("down")
    sys.modules["database"].turn_finishing_collection = NoRefund()
    assert tf.refund_pass(wid) is False
    assert active.queue.find_one({"_id": wid})["used"] == 1                         # conservative: the unit stays spent


def test_a_busy_owner_gets_its_unit_back_but_a_real_pass_keeps_it(active, cfg):
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=5)
    _job(active.queue, "c1", "o1", "t1")
    active.result = ("busy", None)
    assert tf.process_one() is True
    assert active.queue.find_one({"_id": tf.BUDGET_PREFIX + "2026-10-25T10"})["used"] == 0
    active.clock["now"] = T0 + timedelta(seconds=tf.BUSY_RETRY_SECONDS + 1)
    active.result = ("retry", 20)                                                   # waiting for evidence: a real scan, counted
    assert tf.process_one() is True
    assert active.queue.find_one({"_id": tf.BUDGET_PREFIX + "2026-10-25T10"})["used"] == 1


def test_an_exhausted_budget_stops_every_processing_entry_point_without_dropping_a_turn(active, cfg, monkeypatch):
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=1)
    assert tf.take_pass()                                                           # the hour's only pass is gone
    ids = [_job(active.queue, f"c{i}", "o", f"t{i}") for i in range(4)]
    assert tf.process_one() is True                                                 # claimed, then deferred: model not called
    assert active.calls == [] and tf._state["budget_blocked_until"] == T0.replace(minute=0, second=0) + timedelta(hours=1)
    deferred = [d for d in active.queue.find({"chat_id": {"$exists": True}}) if tf._aware(d["due_at"]) > T0 + timedelta(minutes=29)]
    assert len(deferred) == 1 and deferred[0]["attempts"] == 0 and deferred[0]["lease_owner"] is None
    claims_before = sum(d.get("claims", 0) for d in active.queue.find({"chat_id": {"$exists": True}}))
    assert tf.process_one() is False and tf.process_due() == 0                      # this process no longer even claims
    assert sum(d.get("claims", 0) for d in active.queue.find({"chat_id": {"$exists": True}})) == claims_before
    ran = []
    with monkeypatch.context() as mp:
        mp.setattr(threading, "Thread", lambda target=None, kwargs=None, **k: types.SimpleNamespace(
            start=lambda: ran.append(target(**(kwargs or {})))))
        assert tf.wake(ids) == "thread" and ran == [0] and active.calls == []      # the wake-thread path obeys the budget too

    async def run_worker():
        task = asyncio.get_running_loop().create_task(tf._worker(1))
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    monkeypatch.setitem(tf._state, "event", None)
    monkeypatch.setitem(tf._state, "loop", None)

    async def main():
        tf._state["event"] = asyncio.Event()
        monkeypatch.setattr(tf, "SWEEP_INTERVAL_SECONDS", 0.05)
        await run_worker()
    run_async(main)
    assert active.calls == []                                                       # the async worker obeys it too
    assert active.queue.count_documents({"chat_id": {"$exists": True}}) == 4        # nothing was dropped
    active.clock["now"] = T0 + timedelta(hours=1, minutes=1)                        # a new hour: processing resumes
    assert tf.process_one() is True and len(active.calls) == 1


def test_outside_the_active_mode_no_budget_is_ever_taken(monkeypatch, queue, spy):
    _flags(monkeypatch, False, True)
    tf.enqueue("c1", "o1", "t1", due_in_seconds=0)
    assert tf.process_one() is True and spy                                         # legacy: ungoverned, exactly as before
    assert queue.find_one({"_id": {"$regex": "^" + tf.BUDGET_PREFIX}}) is None
    _flags(monkeypatch, True, True)                                                 # dry run: nothing is processed at all
    tf.enqueue("c2", "o2", "t2", due_in_seconds=0)
    assert tf.process_one() is False and queue.find_one({"_id": {"$regex": "^" + tf.BUDGET_PREFIX}}) is None


# ── SLICE 5: retry, parking and lease safety ──────────────────────────────────────────────────────────────────────────
def test_failures_are_bounded_flagged_once_and_never_retried_in_a_tight_loop(active, cfg, caplog):
    cfg(TURN_FINISH_ATTEMPTS_WARN=3, TURN_FINISH_MAX_PASSES_PER_HOUR=1000)
    jid = _job(active.queue)
    active.result = RuntimeError("fail " + SECRET_QUERY)
    caplog.set_level("ERROR", logger="flintel-turn-finisher")
    seen = []
    for n in range(1, 6):
        active.clock["now"] += timedelta(hours=2)                                   # always due again
        assert tf.process_one() is True
        d = _get(active.queue, jid)
        seen.append((d["attempts"], bool(d.get("needs_attention")), (d["due_at"].replace(tzinfo=UTC) - active.clock["now"]).total_seconds()))
    assert [s[0] for s in seen] == [1, 2, 3, 4, 5]
    assert [s[1] for s in seen] == [False, False, True, True, True]
    assert seen[0][2] >= tf.MIN_RETRY_SECONDS and seen[1][2] >= tf.MIN_RETRY_SECONDS
    assert all(s[2] >= tf.ATTENTION_BACKOFF_SECONDS for s in seen[2:])               # once flagged: at most hourly
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "needs attention" in errors[0] and SECRET_QUERY not in errors[0]


def test_a_waiting_for_evidence_retry_is_not_an_attempt_and_has_a_floor(active, cfg):
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=1000)
    jid = _job(active.queue)
    active.result = ("retry", 0.2)
    assert tf.process_one() is True
    d = _get(active.queue, jid)
    assert d["attempts"] == 0 and (d["due_at"].replace(tzinfo=UTC) - T0).total_seconds() >= tf.MIN_RETRY_SECONDS


def test_an_old_job_is_parked_not_deleted_not_processed_and_not_unparked_by_a_page_view(active, cfg):
    chats = mongomock.MongoClient()["c"].chats
    chats.insert_one({"chat_id": "c1", "owner_key": "o1", "messages": [_turn("t1", 5)]})
    before = list(chats.find({}, {"_id": 0}))
    jid = _job(active.queue, created=T0 - timedelta(days=31))
    assert tf.process_one() is True
    d = _get(active.queue, jid)
    assert active.calls == [] and d["parked"] is True and d["parked_reason"] == "max_age" and d["lease_owner"] is None
    assert d["due_at"].replace(tzinfo=UTC) == tf.FAR_FUTURE and d["attempts"] == 0
    assert tf.claim() is None and tf.process_one() is False                         # parked jobs are never claimed
    assert tf.enqueue("c1", "o1", "t1", due_in_seconds=0, pull_forward=True) is True    # a page view
    assert _get(active.queue, jid)["parked"] is True and _get(active.queue, jid)["due_at"].replace(tzinfo=UTC) == tf.FAR_FUTURE
    assert tf.report()["queue"]["parked"] == 1
    assert list(chats.find({}, {"_id": 0})) == before                               # the user's turn is untouched
    assert tf.requeue_parked("c1", "o1", "t1") is True
    d = _get(active.queue, jid)
    assert "parked" not in d and d["attempts"] == 0 and d["lease_owner"] is None and tf._aware(d["due_at"]) == T0
    assert tf.requeue_parked("c1", "o1", "t1") is False                             # not parked any more


def test_the_lease_never_expires_before_a_pass_can_finish_and_a_stale_holder_cannot_clobber(active, cfg, monkeypatch):
    monkeypatch.setattr(tf, "LEASE_SECONDS", 1.0)                                   # a mis-set tiny lease
    cfg(RESPONSE_TIMEOUT=360, CLAUDE_TIMEOUT_SECONDS=60)
    jid = _job(active.queue)
    first = tf.claim()
    assert (first["due_at"].replace(tzinfo=UTC) - T0).total_seconds() >= 360 + 60 + 60     # floored in a governed mode
    assert tf.claim() is None                                                       # leased: nobody else can take it
    active.clock["now"] = T0 + timedelta(seconds=600)                               # lease expired (crashed worker)
    second = tf.claim()
    assert second is not None and second["lease_owner"] != first["lease_owner"]
    tf._finish(first)                                                               # the stale holder finishes late ...
    assert _get(active.queue, jid) is not None                                      # ... and cannot delete the new holder's job
    tf._release(first, 5, False)
    assert _get(active.queue, jid)["lease_owner"] == second["lease_owner"]          # nor release it
    tf._finish(second)
    assert _get(active.queue, jid) is None
    monkeypatch.setattr(tf, "_PROCESSING_GOVERNED", False)
    _flags(monkeypatch, False, True)                                                # legacy keeps the configured value
    assert tf._lease_seconds() == 1.0


def test_two_workers_never_claim_the_same_turn(active):
    _job(active.queue)
    barrier, got = threading.Barrier(8), []

    def work():
        barrier.wait()
        got.append(tf.claim())
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len([g for g in got if g]) == 1


def test_a_finished_turn_is_removed_but_nothing_else_is_ever_deleted(active, cfg):
    cfg(TURN_FINISH_MAX_PASSES_PER_HOUR=1000)
    done = _job(active.queue, "c1", "o1", "t1")
    active.result = ("done", None)
    assert tf.process_one() is True and _get(active.queue, done) is None            # only on an explicit "done"
    keep = _job(active.queue, "c2", "o2", "t2")
    active.result = ("retry", None)
    assert tf.process_one() is True and _get(active.queue, keep) is not None        # a retry keeps the job
    text = (ROOT / "turn_finisher.py").read_text(encoding="utf-8")
    assert text.count("delete_one(") == 1 and text.count("delete_many(") == 1       # _finish (holder-guarded) + heartbeat prune
    assert "delete_many({\"_id\": {\"$regex\": \"^\" + HEARTBEAT_PREFIX}" in text


def test_the_queue_still_never_touches_the_credit_system():
    text = (ROOT / "turn_finisher.py").read_text(encoding="utf-8")
    assert "search_credits" not in text and "credit" not in text.lower()
