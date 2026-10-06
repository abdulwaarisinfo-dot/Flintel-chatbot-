"""
tests/test_scan_resilience.py
==============================
SCAN FIX (steps 1-4): the 90-second scan deadline, cooperative cancel,
single-flight scans, streaming score, the non-blocking SSE stream and the
Mongo timeout kwargs.

Offline: no real Mongo, OpenAI/Anthropic or network. Collections are
in-memory stubs, every LLM/embedding call is a fake.

a  one slow collection -> get_matched_signals returns at the deadline, the
   fast collections still contribute, the slow one is in failed_labels
b  a deadline collection never advances its watermark, complete is False,
   and the NEXT scan re-reads it
c  cancel_event stops a slow cursor thread (polled inside the cursor loop)
d  the function never waits for the slow thread (no `with ThreadPoolExecutor`)
e  stream_answer: first SSE chunk, the Google trigger and the search-progress
   thread all happen BEFORE the scan finishes
f  single-flight registry (share / separate keys / failure clears / waiter
   counting / last release cancels / stale entry not reused)
g  streaming score == old fetch-then-score (same set, same order)
h  streaming bookkeeping: kept = only docs above the threshold, fetched =
   all scanned docs, watermark from the FULL scan
i  every new flag off / 0 -> the old behaviour
j  database.build_mongo_client_kwargs
k  the 9 flags exist in config and in config.__all__
"""
import importlib.util
import logging
import random
import sys
import threading
import time
import types
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# `lg` (logics with stubbed Mongo/HTTP) and the in-memory harness are shared
# with test_strict_intent.py so both files exercise the SAME fakes.
from tests.test_strict_intent import (          # noqa: E402,F401
    lg, Harness, _set_cfg, FakeSignals, _Cursor, _oid_at, _urls, _mkdoc, NOW, OLD,
)

DIM = 8
Q = [1.0] + [0.0] * (DIM - 1)

NEW_FLAGS = [
    "SCAN_DEADLINE_SECONDS", "NONBLOCKING_STREAM_ENABLED", "SINGLE_FLIGHT_SCAN_ENABLED",
    "SINGLE_FLIGHT_STALE_SECONDS", "STREAMING_SCORE_ENABLED",
    "MONGO_SERVER_SELECTION_TIMEOUT_MS", "MONGO_CONNECT_TIMEOUT_MS",
    "MONGO_SOCKET_TIMEOUT_MS", "MONGO_FIND_MAX_TIME_MS",
]


# ═════════════════════════════════════════════════════════════════════════
# fakes
# ═════════════════════════════════════════════════════════════════════════

def _vec(rng):
    return [rng.uniform(-1, 1) for _ in range(DIM)]


def _docs(n, seed=1):
    """Mixed bag: good vectors, empty / wrong-dimension / garbage embeddings
    and a few old docs, newest first by _id."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        ins = OLD - timedelta(seconds=i * 7)
        d = {"_id": _oid_at(ins), "post_url": f"https://www.reddit.com/r/t/comments/{i}/p/",
             "title": f"post {i}", "post_text": f"body of post {i} ai agents",
             "platform": "reddit" if i % 3 else "linkedin", "subreddit": "t",
             "embedding": _vec(rng), "created_utc": ins}
        r = i % 17
        if r == 3:
            d["embedding"] = []                       # empty -> lazy-backfill candidate
        if r == 5:
            d["embedding"] = _vec(rng)[:DIM - 1]      # wrong dimension
        if r == 7:
            d["embedding"] = "garbage"                # truthy non-list
        if r == 11:
            d["created_utc"] = ins - timedelta(days=400)
        out.append(d)
    return out


class Slow:
    """Collection whose cursor is deliberately slow. `n=None` -> endless."""

    def __init__(self, docs, delay, n=None):
        self.docs, self.delay, self.n = docs, delay, n
        self.started = threading.Event()
        self.stop = threading.Event()        # test hygiene: lets the thread end
        self.yielded = 0
        self.max_time = "unset"

    def find(self, query, projection=None):
        outer = self

        class C:
            def sort(s, *a, **k): return s
            def batch_size(s, *a): return s
            def max_time_ms(s, v):
                outer.max_time = v
                return s

            def __iter__(s):
                outer.started.set()
                i = 0
                while not outer.stop.is_set() and (outer.n is None or i < outer.n):
                    time.sleep(outer.delay)
                    outer.yielded += 1
                    d = dict(outer.docs[i % len(outer.docs)])
                    d["post_url"] += f"-slow{i}"
                    i += 1
                    yield d
        return C()


@pytest.fixture
def slow_cleanup():
    made = []
    yield made
    for s in made:
        s.stop.set()


def _prep(lg, monkeypatch, docs, **cfg):
    h = Harness(lg, monkeypatch, docs, **cfg)
    monkeypatch.setattr(lg, "generate_query_embeddings_batch", lambda t: [list(Q) for _ in t])
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD", 0.05)
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    return h


# ═════════════════════════════════════════════════════════════════════════
# a  deadline with a slow collection
# ═════════════════════════════════════════════════════════════════════════

def test_a_deadline_returns_fast_keeps_fast_results_marks_slow_failed(lg, monkeypatch, slow_cleanup):
    docs = _docs(60)
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, SCAN_DEADLINE_SECONDS=1)
    slow = Slow(docs, 0.01); slow_cleanup.append(slow)       # endless, 10ms/doc
    fast4 = FakeSignals(docs[:20])
    st = {}
    t0 = time.monotonic()
    res = lg.get_matched_signals("t", ["ai agents"], limit=500, chat_id="a1",
                                 signals_collection_2=slow, signals_collection_4=fast4,
                                 scan_state=st)
    elapsed = time.monotonic() - t0
    assert 0.9 <= elapsed < 4, elapsed                       # ~deadline + margin, not forever
    assert slow.started.is_set()
    assert res, "the finished collections must still contribute"
    assert not any("-slow" in u for u in _urls(res)), "nothing from the unfinished collection"
    assert "signals_collection_2" in st["failed_labels"]
    assert "signals_collection" not in st["failed_labels"]
    assert st["complete"] is False
    assert lg.get_scan_status("a1", "t")["complete"] is False


def test_a_deadline_log_message(lg, monkeypatch, slow_cleanup, caplog):
    docs = _docs(30)
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, SCAN_DEADLINE_SECONDS=1)
    slow = Slow(docs, 0.01); slow_cleanup.append(slow)
    with caplog.at_level(logging.INFO):
        lg.get_matched_signals("t", ["x"], limit=50, chat_id="a2", signals_collection_2=slow)
    assert any("signals_collection_2 scan deadline hit" in r.getMessage() for r in caplog.records)


# ═════════════════════════════════════════════════════════════════════════
# b  watermark is not advanced for a deadline collection; next scan re-reads it
# ═════════════════════════════════════════════════════════════════════════

def test_b_deadline_collection_does_not_advance_watermark_and_is_rescanned(lg, monkeypatch, slow_cleanup):
    docs = _docs(40)
    h = _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=True, SCAN_DEADLINE_SECONDS=1)
    slow = Slow(docs, 0.01); slow_cleanup.append(slow)
    monkeypatch.setattr(lg, "signals_collection_2", slow)

    h.poll(chat="b1", topic="tb")
    status = lg.get_scan_status("b1", "tb")
    assert status["complete"] is False
    cache = h.cache.docs.get(("b1", "tb"), {})
    assert "scan_watermark" not in cache, "an incomplete scan must never persist a watermark"

    # next scan: the collection is healthy now -> it is read again (from scratch)
    slow.stop.set()
    healthy = FakeSignals(docs[:25])
    monkeypatch.setattr(lg, "signals_collection_2", healthy)
    out = h.poll(chat="b1", topic="tb")
    assert healthy.calls >= 1 and healthy.returned[0] == sum(1 for d in docs[:25] if d.get("embedding"))
    assert lg.get_scan_status("b1", "tb")["complete"] is True
    wm = h.cache.docs[("b1", "tb")]["scan_watermark"]
    assert "signals_collection_2" in wm
    assert out


# ═════════════════════════════════════════════════════════════════════════
# c  cancel_event stops the slow cursor thread
# ═════════════════════════════════════════════════════════════════════════

def test_c_cancel_event_stops_slow_cursor_thread(lg, monkeypatch, slow_cleanup):
    docs = _docs(30)
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, SCAN_DEADLINE_SECONDS=60)
    slow = Slow(docs, 0.001); slow_cleanup.append(slow)       # 1ms/doc, endless
    ev = threading.Event()
    threading.Timer(0.4, ev.set).start()
    st = {}
    t0 = time.monotonic()
    res = lg.get_matched_signals("t", ["x"], limit=50, chat_id="c1",
                                 signals_collection_2=slow, cancel_event=ev, scan_state=st)
    assert time.monotonic() - t0 < 3
    assert res == [] and st["complete"] is False

    # the cursor loop polls every 1000 docs (~1s here): the thread must stop by itself
    deadline = time.monotonic() + 5
    last = -1
    while time.monotonic() < deadline:
        time.sleep(0.4)
        if slow.yielded == last:
            break
        last = slow.yielded
    stable = slow.yielded
    time.sleep(0.5)
    assert slow.yielded == stable, "slow cursor thread kept running after cancel"
    assert not slow.stop.is_set(), "it stopped via cancel_event, not via the test's own stop flag"


# ═════════════════════════════════════════════════════════════════════════
# d  no waiting for the slow thread
# ═════════════════════════════════════════════════════════════════════════

def test_d_does_not_wait_for_the_slow_thread(lg, monkeypatch, slow_cleanup):
    docs = _docs(30)
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, SCAN_DEADLINE_SECONDS=1)
    # 1000 docs at 20ms = ~20s before the cursor loop would notice the cancel:
    # a blocking `with ThreadPoolExecutor` would make the call take that long.
    slow = Slow(docs, 0.02); slow_cleanup.append(slow)
    t0 = time.monotonic()
    lg.get_matched_signals("t", ["x"], limit=50, chat_id="d1", signals_collection_2=slow)
    elapsed = time.monotonic() - t0
    assert elapsed < 3, elapsed
    y = slow.yielded
    time.sleep(0.3)
    assert slow.yielded > y, "the slow thread is still running in the background: we did not wait for it"


# ═════════════════════════════════════════════════════════════════════════
# e  stream_answer is non-blocking
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def routes_env(lg, monkeypatch):
    """routes.py imported against a stubbed `index` (real FastAPI app so the
    route decorators return the real functions)."""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI

    spec = importlib.util.spec_from_file_location("flintel_real_for_routes", ROOT / "flintel.py")
    real_flintel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real_flintel)

    env = types.SimpleNamespace(
        t_start=time.monotonic(), events={}, scan_cancel=None,
        scan_gate=threading.Event(), scan_seconds=2.0,
    )
    env.msg = {"topic_key": "tk", "query": "need an ai agent", "keywords": ["ai agent"],
               "requested_at": None, "google_fallback_triggered": False,
               "search_progress_generated": False}

    def fake_topup(**kw):
        env.events["scan_start"] = time.monotonic()
        ev = kw.get("cancel_event")
        env.scan_cancel = ev
        end = time.monotonic() + env.scan_seconds
        while time.monotonic() < end:
            if ev is not None and ev.is_set():
                env.events["scan_cancelled"] = time.monotonic()
                return []
            time.sleep(0.02)
        env.events["scan_end"] = time.monotonic()
        return [{"title": f"t{i}", "post_text": "x", "post_url": f"u{i}", "platform": "reddit"}
                for i in range(25)]

    def fake_google(chat_id, owner_key, msg):
        env.events["google_trigger"] = time.monotonic()

    def fake_progress(query, keywords, platform, call_claude):
        env.events["progress_thread"] = time.monotonic()
        return "searching..."

    monkeypatch.setattr(real_flintel, "generate_search_progress_content", fake_progress)

    values = dict(
        app=FastAPI(), templates=MagicMock(), pwd_context=MagicMock(), oauth=MagicMock(),
        log=logging.getLogger("routes-test"),
        RESPONSE_TIMEOUT=30, STREAM_CHUNK_CHARS=50, STREAM_CHUNK_DELAY_SECONDS=0,
        MAX_KEYWORDS=10, MAX_ANALYSIS_EVIDENCE=25, MIN_ANALYSIS_EVIDENCE=25,
        get_evidence_with_topup=fake_topup, get_matched_signals=MagicMock(),
        get_owner=lambda request: ("owner1", "anon"),
        get_chat_session=lambda cid, owner: {"messages": [env.msg]},
        _is_owner_busy=lambda k: False, strict_intent_mode=lambda: False,
        get_scan_status=lambda c, t: {}, _elapsed_seconds=lambda ts: time.monotonic() - env.t_start,
        _trigger_google_fallback_search=fake_google,
    )
    index_stub = types.ModuleType("index")
    index_stub.__dict__.update(values)
    # any other name routes.py imports from index is just a MagicMock
    index_stub.__dict__["__getattr__"] = lambda name: MagicMock(name=f"index.{name}")

    db = sys.modules["database"]
    monkeypatch.setattr(db, "users_collection", MagicMock(), raising=False)
    monkeypatch.setattr(db, "google_posts_collection", MagicMock(), raising=False)
    saved = {n: sys.modules.get(n) for n in ("index", "routes", "flintel")}
    sys.modules["index"] = index_stub
    sys.modules["flintel"] = real_flintel
    sys.modules.pop("routes", None)
    try:
        env.routes = importlib.import_module("routes")
        # Hand the raw sync generator back (newer Starlette wraps it in an
        # async iterator; the generator itself is what we want to drive).
        monkeypatch.setattr(env.routes, "StreamingResponse",
                            lambda gen, **kw: types.SimpleNamespace(body_iterator=gen))
        yield env
    finally:
        for n, old in saved.items():
            if old is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = old


def _drain_until(gen, predicate, limit_s=20):
    chunks = []
    t_end = time.monotonic() + limit_s
    while time.monotonic() < t_end:
        chunk = next(gen)
        chunks.append((time.monotonic(), chunk))
        if predicate(chunk):
            break
    return chunks


def test_e_first_chunk_google_trigger_and_progress_thread_come_before_scan_end(routes_env, monkeypatch):
    env = routes_env
    _set_cfg(monkeypatch, NONBLOCKING_STREAM_ENABLED=True, SINGLE_FLIGHT_SCAN_ENABLED=True,
             STRICT_INTENT_MODE=False)
    t_call = time.monotonic()
    resp = env.routes.stream_answer(MagicMock(), "chat-e", "tk")
    t_returned = time.monotonic()
    assert t_returned - t_call < 1.0, "StreamingResponse must be returned without waiting for the scan"
    assert "scan_end" not in env.events

    gen = iter(resp.body_iterator)
    first = next(gen)
    t_first = time.monotonic()
    second = next(gen)
    assert first.startswith("retry:")
    assert '"progress_percent": 0' in second
    assert "scan_end" not in env.events, "first SSE chunks arrived before the scan finished"

    try:
        # keep reading until the scan is done; the generator keeps emitting progress meanwhile
        chunks = _drain_until(gen, lambda c: "scan_end" in env.events, limit_s=15)
    finally:
        gen.close()

    ev = env.events
    assert "scan_start" in ev and "scan_end" in ev
    assert t_first < ev["scan_end"]
    assert ev["google_trigger"] < ev["scan_end"], "Google fallback must not wait for the scan"
    assert ev["progress_thread"] < ev["scan_end"], "search-progress thread must not wait for the scan"
    assert any('"progress_percent"' in c for _, c in chunks), "progress is streamed while scanning"


def test_e_client_disconnect_cancels_the_scan(routes_env, monkeypatch):
    env = routes_env
    env.scan_seconds = 10
    _set_cfg(monkeypatch, NONBLOCKING_STREAM_ENABLED=True, SINGLE_FLIGHT_SCAN_ENABLED=True,
             STRICT_INTENT_MODE=False)
    gen = iter(env.routes.stream_answer(MagicMock(), "chat-e2", "tk").body_iterator)
    next(gen); next(gen)
    next(gen)          # 3rd step: acquire_scan + Google trigger + progress thread have started
    deadline = time.monotonic() + 3
    while env.scan_cancel is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert env.scan_cancel is not None and not env.scan_cancel.is_set()
    gen.close()                                              # browser went away -> finally: release()
    assert env.scan_cancel.is_set(), "last waiter gone -> the background scan must be cancelled"
    deadline = time.monotonic() + 3
    while "scan_cancelled" not in env.events and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "scan_cancelled" in env.events


# ═════════════════════════════════════════════════════════════════════════
# f  single-flight
# ═════════════════════════════════════════════════════════════════════════

def test_f_same_key_runs_one_scan_and_shares_it(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=True, SINGLE_FLIGHT_STALE_SECONDS=600)
    calls, gate = [], threading.Event()

    def scan(ev):
        calls.append(ev)
        gate.wait(5)
        return ["r"]
    h1 = lg.acquire_scan("f-c", "f-t", scan)
    h2 = lg.acquire_scan("f-c", "f-t", scan)
    time.sleep(0.15)
    assert len(calls) == 1
    assert h1.future is h2.future and h1.cancel_event is h2.cancel_event
    assert h1.started_here and not h2.started_here
    gate.set()
    assert h1.future.result(2) == ["r"] and h2.future.result(2) == ["r"]


def test_f_different_key_runs_separate_scan(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=True)
    gate, n = threading.Event(), []
    def mk(tag):
        def scan(ev):
            n.append(tag)
            gate.wait(5)
            return [tag]
        return scan
    h1 = lg.acquire_scan("f-c", "f-a", mk("a"))
    h2 = lg.acquire_scan("f-c", "f-b", mk("b"))
    h3 = lg.acquire_scan("f-c2", "f-a", mk("c"))
    time.sleep(0.15)
    assert sorted(n) == ["a", "b", "c"]
    assert len({id(h1.future), id(h2.future), id(h3.future)}) == 3
    gate.set()
    assert [h.future.result(2) for h in (h1, h2, h3)] == [["a"], ["b"], ["c"]]


def test_f_failed_scan_clears_registry_entry(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=True)

    def boom(ev):
        raise RuntimeError("mongo down")
    h = lg.acquire_scan("f-c", "f-err", boom)
    with pytest.raises(RuntimeError):
        h.future.result(2)
    deadline = time.monotonic() + 2
    while ("f-c", "f-err") in lg._SCAN_REGISTRY and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ("f-c", "f-err") not in lg._SCAN_REGISTRY
    h2 = lg.acquire_scan("f-c", "f-err", lambda ev: [1])
    assert h2.started_here and h2.future.result(2) == [1]


def test_f_first_release_keeps_scan_alive_last_release_cancels(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=True, SINGLE_FLIGHT_STALE_SECONDS=600)
    gate = threading.Event()
    def scan(ev):
        gate.wait(5)
        return [] if ev.is_set() else ["done"]
    h1 = lg.acquire_scan("f-c", "f-rel", scan)
    h2 = lg.acquire_scan("f-c", "f-rel", lambda ev: ["never"])
    h1.release()
    h1.release()                                             # idempotent: must not count twice
    assert not h2.cancel_event.is_set(), "another waiter is still there"
    h2.release()
    assert h2.cancel_event.is_set(), "last waiter gone -> cancel"
    gate.set()
    assert h2.future.result(2) == []
    deadline = time.monotonic() + 2
    while ("f-c", "f-rel") in lg._SCAN_REGISTRY and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ("f-c", "f-rel") not in lg._SCAN_REGISTRY


def test_f_stale_entry_is_not_reused(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=True, SINGLE_FLIGHT_STALE_SECONDS=1)
    gate = threading.Event()
    def old(ev):
        gate.wait(5)
        return ["old"]
    h1 = lg.acquire_scan("f-c", "f-stale", old)
    time.sleep(1.2)
    h2 = lg.acquire_scan("f-c", "f-stale", lambda ev: ["new"])
    assert h2.future is not h1.future and h2.started_here
    assert h2.future.result(2) == ["new"]
    gate.set()


def test_f_cancelled_entry_is_not_reused(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=True, SINGLE_FLIGHT_STALE_SECONDS=600)
    gate = threading.Event()
    def parked(ev):
        gate.wait(5)
        return []
    h1 = lg.acquire_scan("f-c", "f-cx", parked)
    h1.release()                                             # nobody waits -> cancelled
    h2 = lg.acquire_scan("f-c", "f-cx", lambda ev: ["fresh"])
    assert h2.future is not h1.future and h2.future.result(2) == ["fresh"]
    gate.set()


# ═════════════════════════════════════════════════════════════════════════
# g  streaming score parity with the old fetch-then-score path
# ═════════════════════════════════════════════════════════════════════════

def _run_score(lg, monkeypatch, docs, streaming, *, since_days=None, three=True, batch=2000, threshold=0.05):
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, STREAMING_SCORE_ENABLED=streaming)
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", batch)
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD", threshold)
    c2 = FakeSignals(docs[len(docs) // 2:])          # overlaps the primary -> dedupe
    c4 = FakeSignals(docs[:len(docs) // 3])
    return lg.get_matched_signals("t", ["ai agents"], limit=500, since_days=since_days,
                                  signals_collection_2=c2 if three else None,
                                  signals_collection_4=c4 if three else None, chat_id="g1")


@pytest.mark.parametrize("batch", [2000, 7])           # one batch / many small batches
@pytest.mark.parametrize("since_days", [None, 30])
@pytest.mark.parametrize("three", [True, False])
def test_g_streaming_score_equals_old_path(lg, monkeypatch, batch, since_days, three):
    docs = _docs(300)
    old = _run_score(lg, monkeypatch, docs, False, since_days=since_days, three=three, batch=batch)
    new = _run_score(lg, monkeypatch, docs, True, since_days=since_days, three=three, batch=batch)
    assert len(old) > 20
    assert _urls(new) == _urls(old)                    # same set AND same order
    assert len(set(_urls(new))) == len(new)            # dedupe held across collections


def test_g_threshold_boundary_docs_agree(lg, monkeypatch):
    """docs sitting exactly on / just around the threshold land on the same side."""
    import math
    thr = 0.35
    docs = []
    for i, s in enumerate([0.30, 0.349, 0.35, 0.351, 0.36, 0.5, 0.9, 0.2, 0.35, 0.3501]):
        d = _mkdoc(i, s, inserted=OLD - timedelta(minutes=i))
        d["embedding"] = [s, math.sqrt(1 - s * s)] + [0.0] * (DIM - 2)
        docs.append(d)
    old = _run_score(lg, monkeypatch, docs, False, three=False, threshold=thr)
    new = _run_score(lg, monkeypatch, docs, True, three=False, threshold=thr)
    assert _urls(new) == _urls(old)


def test_g_scores_match_numpy_cosine(lg, monkeypatch):
    np = pytest.importorskip("numpy")
    docs = _docs(120)
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, STREAMING_SCORE_ENABLED=True)
    st = {}
    lg.get_matched_signals("t", ["x"], limit=500, chat_id="g2", scan_state=st)
    assert st["sims"]
    q = np.array(Q, dtype=np.float32)
    by_url = {d["post_url"]: d for d in docs}
    for url, s in st["sims"].items():
        e = np.array(by_url[url]["embedding"], dtype=np.float32)
        assert abs(float(e @ q / np.linalg.norm(e)) - s) < 1e-5


def test_g_lazy_backfill_candidates_survive_streaming(lg, monkeypatch):
    docs = _docs(60)
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, STREAMING_SCORE_ENABLED=True)
    monkeypatch.setattr(lg, "LAZY_EMBED_ENABLED", True)
    ordered = sorted(docs, key=lambda d: d["created_utc"], reverse=True)

    class Raw:                    # real Mongo returns `embedding: []` docs too
        def find(self, q, p=None):
            return _Cursor([dict(d) for d in ordered])
    monkeypatch.setattr(lg, "signals_collection", Raw())
    seen = []
    monkeypatch.setattr(lg, "_lazy_backfill_missing_embeddings",
                        lambda raw, coll, fn: seen.append([d["post_url"] for d in raw]) or [])
    lg.get_matched_signals("t", ["x"], limit=500, chat_id="g3")
    expected = [d["post_url"] for d in ordered if not d["embedding"]]
    assert expected and seen and seen[0] == expected


# ═════════════════════════════════════════════════════════════════════════
# h  streaming bookkeeping: kept / fetched / watermark
# ═════════════════════════════════════════════════════════════════════════

def _split_docs(n_hi, n_lo):
    hi = [_mkdoc(i, 0.9, inserted=OLD - timedelta(seconds=i)) for i in range(n_hi)]
    lo = [_mkdoc(1000 + i, 0.1, inserted=OLD - timedelta(seconds=n_hi + i)) for i in range(n_lo)]
    return hi, lo


def test_h_kept_only_above_threshold_fetched_counts_all_scanned(lg, monkeypatch, caplog):
    hi, lo = _split_docs(10, 190)
    docs = hi + lo
    h = Harness(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=True, STREAMING_SCORE_ENABLED=True)
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", 25)          # 8 batches
    st = {}
    with caplog.at_level(logging.INFO):
        res = lg.get_matched_signals("t", ["ai agents"], limit=500, chat_id="h1", scan_state=st)
    assert len(res) == 10 and set(_urls(res)) == {d["post_url"] for d in hi}
    msgs = [r.getMessage() for r in caplog.records if "streamed-score" in r.getMessage()]
    assert msgs and "scanned=200 kept=10" in msgs[0], msgs
    assert st["fetched"]["signals_collection"] == 200               # scanned, NOT kept
    assert st["complete"] is True and not st["failed_labels"]


def test_h_watermark_comes_from_the_full_scan_not_from_kept_docs(lg, monkeypatch):
    hi, lo = _split_docs(5, 50)
    newest_low = _mkdoc(9999, 0.1, inserted=NOW)              # newest of ALL docs, below the threshold
    docs = hi + lo + [newest_low]
    marks = {}
    for streaming in (False, True):
        _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=True, STREAMING_SCORE_ENABLED=streaming,
              INCREMENTAL_WATERMARK_FIELD="")
        monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD", 0.35)
        st = {}
        res = lg.get_matched_signals("t", ["ai agents"], limit=500, chat_id=f"h2{streaming}", scan_state=st)
        assert newest_low["post_url"] not in _urls(res)       # below threshold -> not kept
        marks[streaming] = st["new_wm"]["signals_collection"]
    assert marks[True] == marks[False], "streaming must produce the same watermark as the old path"
    assert marks[True] >= newest_low["_id"].generation_time.replace(microsecond=0), \
        "the watermark must cover the newest SCANNED doc, not just the newest kept one"


def test_h_kept_docs_drop_their_embedding_vectors(lg, monkeypatch):
    docs = _docs(80)
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, STREAMING_SCORE_ENABLED=True)
    res = lg.get_matched_signals("t", ["x"], limit=500, chat_id="h3")
    assert res and all("embedding" not in d for d in res)


# ═════════════════════════════════════════════════════════════════════════
# i  every new flag off -> old behaviour
# ═════════════════════════════════════════════════════════════════════════

def test_i_deadline_zero_waits_for_a_slow_but_finite_collection(lg, monkeypatch, slow_cleanup):
    docs = _docs(40)
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, SCAN_DEADLINE_SECONDS=0)
    slow = Slow(docs, 0.02, n=60); slow_cleanup.append(slow)         # ~1.2s, then ends
    st = {}
    t0 = time.monotonic()
    res = lg.get_matched_signals("t", ["x"], limit=500, chat_id="i1", signals_collection_2=slow, scan_state=st)
    assert time.monotonic() - t0 >= 1.1                               # old behaviour: waited for it
    assert st["complete"] is True and not st["failed_labels"]
    assert any("-slow" in u for u in _urls(res))


def test_i_streaming_off_uses_fetch_then_score(lg, monkeypatch, caplog):
    docs = _docs(80)
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, STREAMING_SCORE_ENABLED=False)
    with caplog.at_level(logging.INFO):
        res = lg.get_matched_signals("t", ["x"], limit=500, chat_id="i2")
    assert res
    assert not any("streamed-score" in r.getMessage() for r in caplog.records)


def test_i_streaming_on_is_actually_used_by_default_path(lg, monkeypatch, caplog):
    docs = _docs(80)
    _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, STREAMING_SCORE_ENABLED=True)
    with caplog.at_level(logging.INFO):
        lg.get_matched_signals("t", ["x"], limit=500, chat_id="i3")
    assert any("streamed-score" in r.getMessage() for r in caplog.records)


def test_i_single_flight_off_every_request_scans_on_its_own(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=False)
    gate, n = threading.Event(), []
    def mk(tag):
        def scan(ev):
            n.append(tag)
            gate.wait(3)
            return [tag]
        return scan
    h1 = lg.acquire_scan("i-c", "i-t", mk(1))
    h2 = lg.acquire_scan("i-c", "i-t", mk(2))
    time.sleep(0.15)
    assert sorted(n) == [1, 2]
    assert h1.future is not h2.future and h1.cancel_event is not h2.cancel_event
    assert ("i-c", "i-t") not in lg._SCAN_REGISTRY
    h1.release()
    assert h1.cancel_event.is_set() and not h2.cancel_event.is_set()
    gate.set()


@pytest.mark.parametrize("val,expected", [(120000, [120000]), (0, [])])
def test_i_find_max_time_ms_only_when_positive(lg, monkeypatch, val, expected):
    docs = _docs(20)
    seen = []

    class C(FakeSignals):
        def find(self, q, p=None):
            cur = super().find(q, p)
            cur.max_time_ms = lambda v: seen.append(v) or cur
            return cur
    Harness(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, MONGO_FIND_MAX_TIME_MS=val)
    monkeypatch.setattr(lg, "signals_collection", C(docs))
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    monkeypatch.setattr(lg, "generate_query_embeddings_batch", lambda t: [list(Q) for _ in t])
    lg.get_matched_signals("t", ["x"], limit=50, chat_id="i4")
    assert seen == expected


def test_i_find_execution_timeout_is_a_failed_collection_not_a_crash(lg, monkeypatch):
    pe = pytest.importorskip("pymongo.errors")
    docs = _docs(30)
    h = _prep(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=True)

    class Boom:
        def find(self, q, p=None):
            class C:
                def sort(s, *a, **k): return s
                def batch_size(s, *a): return s
                def max_time_ms(s, v): return s
                def __iter__(s):
                    for d in docs[:5]:
                        yield dict(d)
                    raise pe.ExecutionTimeout("operation exceeded time limit")
            return C()
    monkeypatch.setattr(lg, "signals_collection", Boom())
    h.poll(chat="i5", topic="ti5")
    assert "scan_watermark" not in h.cache.docs.get(("i5", "ti5"), {})
    assert lg.get_scan_status("i5", "ti5")["complete"] is False


def test_i_nonblocking_off_scan_runs_before_the_response_is_returned(routes_env, monkeypatch):
    env = routes_env
    env.scan_seconds = 0.6
    _set_cfg(monkeypatch, NONBLOCKING_STREAM_ENABLED=False, STRICT_INTENT_MODE=False)
    resp = env.routes.stream_answer(MagicMock(), "chat-i", "tk")
    assert "scan_end" in env.events, "flag off: old synchronous scan, finished before StreamingResponse"
    gen = iter(resp.body_iterator)
    try:
        assert next(gen).startswith("retry:")
    finally:
        gen.close()


def test_i_matcher_without_cancel_event_still_works(lg, monkeypatch):
    Harness(lg, monkeypatch, [], INCREMENTAL_RESCAN_ENABLED=False)
    got = {}

    def new_style(topic_key, keywords, targeting_platform="all", since_days=None, unfiltered=False,
                  match_phrases=None, limit=None, signals_collection_2=None, signals_collection_4=None,
                  chat_id=None, user_query=None, cancel_event=None):
        got["ev"] = cancel_event
        return []

    def old_style(topic_key, keywords, targeting_platform="all", since_days=None, unfiltered=False,
                  match_phrases=None, limit=None, signals_collection_2=None, signals_collection_4=None,
                  chat_id=None, user_query=None):
        got["old"] = True
        return []
    ev = threading.Event()
    lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, new_style, cancel_event=ev)
    assert got["ev"] is ev
    lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, old_style, cancel_event=ev)
    assert got["old"]


# ═════════════════════════════════════════════════════════════════════════
# j  database.build_mongo_client_kwargs
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def db_mod(lg, monkeypatch):
    """The REAL database.py, loaded under a private name with MongoClient
    replaced by a MagicMock (no connection is ever attempted)."""
    pymongo = pytest.importorskip("pymongo")
    monkeypatch.setattr(pymongo, "MongoClient", MagicMock(name="MongoClient"))
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONGODB3", "mongodb://localhost:27018")
    spec = importlib.util.spec_from_file_location("database_real_for_test", ROOT / "database.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:                                 # pragma: no cover - env specific
        pytest.skip(f"database.py cannot be loaded offline here: {exc!r}")
    return mod


def test_j_kwargs_from_config_values(db_mod, monkeypatch):
    _set_cfg(monkeypatch, MONGO_SERVER_SELECTION_TIMEOUT_MS=15000, MONGO_CONNECT_TIMEOUT_MS=16000,
             MONGO_SOCKET_TIMEOUT_MS=120000)
    assert db_mod.build_mongo_client_kwargs() == {
        "serverSelectionTimeoutMS": 15000, "connectTimeoutMS": 16000, "socketTimeoutMS": 120000}


def test_j_zero_negative_or_garbage_removes_the_kwarg(db_mod, monkeypatch):
    _set_cfg(monkeypatch, MONGO_SERVER_SELECTION_TIMEOUT_MS=0, MONGO_CONNECT_TIMEOUT_MS=-5,
             MONGO_SOCKET_TIMEOUT_MS="nonsense")
    assert db_mod.build_mongo_client_kwargs() == {}
    _set_cfg(monkeypatch, MONGO_SERVER_SELECTION_TIMEOUT_MS=15000, MONGO_CONNECT_TIMEOUT_MS=0,
             MONGO_SOCKET_TIMEOUT_MS=0)
    assert db_mod.build_mongo_client_kwargs() == {"serverSelectionTimeoutMS": 15000}


def test_j_include_socket_timeout_false_never_sets_socket_timeout(db_mod, monkeypatch):
    _set_cfg(monkeypatch, MONGO_SERVER_SELECTION_TIMEOUT_MS=15000, MONGO_CONNECT_TIMEOUT_MS=15000,
             MONGO_SOCKET_TIMEOUT_MS=120000)
    kw = db_mod.build_mongo_client_kwargs(include_socket_timeout=False)
    assert "socketTimeoutMS" not in kw
    assert kw == {"serverSelectionTimeoutMS": 15000, "connectTimeoutMS": 15000}


def test_j_env_vars_reach_the_kwargs(monkeypatch):
    """env -> config -> kwargs, end to end (config re-imported with env set)."""
    monkeypatch.setenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "4321")
    monkeypatch.setenv("MONGO_CONNECT_TIMEOUT_MS", "0")
    monkeypatch.setenv("MONGO_SOCKET_TIMEOUT_MS", "9999")
    import config
    cfg_path = ROOT / "config.py"
    spec = importlib.util.spec_from_file_location("config_env_probe", cfg_path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.MONGO_SERVER_SELECTION_TIMEOUT_MS == 4321
    assert probe.MONGO_CONNECT_TIMEOUT_MS == 0
    assert probe.MONGO_SOCKET_TIMEOUT_MS == 9999
    assert config is sys.modules["config"]                  # the live config module was not replaced


# ═════════════════════════════════════════════════════════════════════════
# k  config flags
# ═════════════════════════════════════════════════════════════════════════

def test_k_all_nine_flags_exist_and_are_exported(lg):
    import config
    assert len(NEW_FLAGS) == 9
    for name in NEW_FLAGS:
        assert hasattr(config, name), f"config.{name} missing"
        assert name in config.__all__, f"{name} missing from config.__all__"


def test_k_flags_are_exposed_by_star_import(lg):
    ns = {}
    exec("from config import *", ns)
    for name in NEW_FLAGS:
        assert name in ns, f"`from config import *` does not expose {name}"


def test_k_default_values(lg):
    import os
    for name in NEW_FLAGS:
        os.environ.pop(name, None)
    spec = importlib.util.spec_from_file_location("config_defaults_probe", ROOT / "config.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.SCAN_DEADLINE_SECONDS == 90
    assert probe.NONBLOCKING_STREAM_ENABLED is True
    assert probe.SINGLE_FLIGHT_SCAN_ENABLED is True
    assert probe.SINGLE_FLIGHT_STALE_SECONDS == 600
    assert probe.STREAMING_SCORE_ENABLED is True
    assert probe.MONGO_SERVER_SELECTION_TIMEOUT_MS == 15000
    assert probe.MONGO_CONNECT_TIMEOUT_MS == 15000
    assert probe.MONGO_SOCKET_TIMEOUT_MS == 120000
    assert probe.MONGO_FIND_MAX_TIME_MS == 120000
