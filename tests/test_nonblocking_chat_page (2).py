"""
tests/test_nonblocking_chat_page.py — opening a chat (or the redirect after
sending "Hello") must not wait for a posts scan of an older unfinished
research turn; that turn must still be finished, exactly once, by the
durable turn-finisher (turn_finisher.py + index._finish_turn_job).

Root cause reproduced here: view_chat() called _fill_in_message_outputs()
for every older search turn missing `results`/`claude_answer`, and that runs
get_evidence_with_topup() (a full posts scan) synchronously before the page
renders. With the scan stubbed to take 3 s, GET /chat/{id} took > 3 s.

Everything runs against in-memory mongomock with the scan, the LLM and the
summary stubbed — no real LLM, Mongo or network.
"""
import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_answer_persistence import env  # noqa: F401  (fixture)
from tests.test_reliability_ux_perf import (  # noqa: F401  (fixtures + helpers)
    OWNER, _open, _wait_chat, app_env, browser,
)

mongomock = pytest.importorskip("mongomock")

SCAN_SECONDS = 3.0
POSTS = [{"title": f"Need a CRM post {i}", "post_text": "Looking for a CRM for my sales team. " * 10,
          "post_url": f"https://www.reddit.com/r/crm/comments/abc{i}/need_a_crm/", "platform": "reddit",
          "subreddit": "crm"} for i in range(20)]


def _answer_for(pool):
    return json.dumps({"format": "source_list", "headline": "People are looking for a CRM",
                       "executive_summary": "Several teams ask for CRM recommendations.",
                       "platforms": [{"platform": "reddit", "total_analyzed": len(pool), "shown_count": 2,
                                      "posts": [{"index": i + 1, "source": "crm", "title": p["title"],
                                                 "summary": "Asks for a CRM."} for i, p in enumerate(pool[:2])]}]})


def _turn(query, seconds_ago, answered=False):
    now = datetime.now(timezone.utc)
    return {"query": query, "topic_key": query.lower(), "keywords": ["crm"], "match_phrases": None,
            "targeting_platform": "reddit", "time_window_days": None, "time_range": None, "evidence_required": 15,
            "unfiltered": False, "website_only": False, "google_fallback_triggered": True,
            "search_progress": None, "search_progress_generated": True, "message_type": "search",
            "requested_at": now - timedelta(seconds=seconds_ago),
            "answered_at": now if answered else None,
            "results": POSTS[:2] if answered else None,
            "claude_answer": _answer_for(POSTS) if answered else None}


def _chat_turn(query, answer, seconds_ago=0):
    return {"query": query, "topic_key": None, "keywords": [], "targeting_platform": None, "time_window_days": None,
            "requested_at": datetime.now(timezone.utc) - timedelta(seconds=seconds_ago), "results": [],
            "claude_answer": answer, "message_type": "chat"}


@pytest.fixture
def fin(app_env, monkeypatch):
    """app_env + mongomock chats/busy/queue collections + a slow, counted scan
    and a counted answer call."""
    lg, idx, rt, _list_chats, state = app_env
    import turn_finisher as tf
    mdb = mongomock.MongoClient()["flintel_test"]
    chats, busy, queue = mdb.chats, mdb.busy, mdb.turn_finishing
    dbmod = sys.modules["database"]
    monkeypatch.setattr(dbmod, "turn_finishing_collection", queue, raising=False)
    monkeypatch.setattr(dbmod, "chats_collection", chats, raising=False)
    monkeypatch.setattr(idx, "chats_collection", chats)
    monkeypatch.setattr(idx, "busy_owners_collection", busy)
    monkeypatch.setattr(sys.modules["scheduler"], "start_trending_keywords_scheduler", lambda: None, raising=False)
    calls = {"scan": 0, "analyze": 0, "fallback": 0, "scan_threads": set()}
    lock = threading.Lock()
    knobs = {"scan_seconds": SCAN_SECONDS, "posts": POSTS, "analyze_error": None}

    def slow_scan(**kw):
        with lock:
            calls["scan"] += 1
            calls["scan_threads"].add(threading.current_thread().name)
        time.sleep(knobs["scan_seconds"])
        return list(knobs["posts"])

    def fake_analyze(query, pool, extra_context=None, **k):
        with lock:
            calls["analyze"] += 1
        if knobs["analyze_error"]:
            raise knobs["analyze_error"]
        return _answer_for(pool)

    def fake_fallback(chat_id, owner_key, topic_key, *a, **k):
        with lock:
            calls["fallback"] += 1
        idx._clear_owner_busy(owner_key)

    monkeypatch.setattr(idx, "get_evidence_with_topup", slow_scan)
    monkeypatch.setattr(idx, "analyze_with_claude", fake_analyze)
    monkeypatch.setattr(idx, "_timeout_fallback_answer", fake_fallback)
    monkeypatch.setattr(idx, "append_to_chat_summary", lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["flintel"], "should_trigger_immediately", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(sys.modules["flintel"], "format_google_stub_results", lambda docs: [], raising=False)
    monkeypatch.setattr(sys.modules["flintel"], "merge_matched_and_google_results",
                        lambda m, g, max_total=None, **k: list(m)[:max_total] if max_total else list(m), raising=False)
    monkeypatch.setattr(tf, "SWEEP_INTERVAL_SECONDS", 0.2)
    yield {"lg": lg, "idx": idx, "rt": rt, "tf": tf, "chats": chats, "queue": queue, "busy": busy,
           "calls": calls, "knobs": knobs, "state": state}
    _stop_worker(tf)


def _seed(chats, chat_id, messages, owner=OWNER):
    now = datetime.now(timezone.utc)
    chats.insert_one({"chat_id": chat_id, "owner_key": owner, "owner_type": "guest", "title": chat_id,
                      "messages": messages, "summary": "", "created_at": now, "updated_at": now})


def _msg(chats, chat_id, query, owner=OWNER):
    doc = chats.find_one({"chat_id": chat_id, "owner_key": owner})
    return next(m for m in reversed(doc["messages"]) if m.get("query") == query)


def _request(idx, path):
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b"",
                    "session": {}, "app": idx.app})


def _view(f, chat_id):
    from fastapi import BackgroundTasks
    tasks = BackgroundTasks()
    t0 = time.perf_counter()
    resp = f["rt"].view_chat(_request(f["idx"], f"/chat/{chat_id}"), chat_id, tasks)
    return resp, time.perf_counter() - t0, tasks


def _wait(cond, timeout=15.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


def _finished(f, chat_id, query):
    m = _msg(f["chats"], chat_id, query)
    return m.get("results") is not None and m.get("claude_answer") is not None


_WORKER = {}


def _start_worker(tf):
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()

    async def _go():
        tf.start()
    asyncio.run_coroutine_threadsafe(_go(), loop).result(5)
    _WORKER["loop"], _WORKER["thread"] = loop, th


def _stop_worker(tf):
    loop = _WORKER.pop("loop", None)
    if loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(tf.stop(), loop).result(10)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        _WORKER.pop("thread").join(5)


def _idle(f, chat_id, query, extra=0.6):
    """Waits until the turn is finished AND its queue document is gone, then a
    little longer, so any late duplicate would have shown up."""
    jid = f["tf"].job_id(chat_id, OWNER, query.lower())
    assert _wait(lambda: _finished(f, chat_id, query) and f["queue"].find_one({"_id": jid}) is None, 20)
    time.sleep(extra)


# ── 1. reproduce the delay at its source, then show the page no longer waits ──
def test_old_synchronous_fill_in_is_what_made_the_page_slow(fin):
    """The work the page used to do inline takes the full scan time."""
    f = fin
    _seed(f["chats"], "c1", [_turn("Find people looking for a CRM", 60), _chat_turn("Hello", "Hi there!")])
    msgs = f["chats"].find_one({"chat_id": "c1"})["messages"]
    t0 = time.perf_counter()
    f["idx"]._fill_in_message_outputs("c1", OWNER, msgs, skip_topic_key=None, background_tasks=None)
    assert time.perf_counter() - t0 >= SCAN_SECONDS


def test_chat_page_returns_promptly_and_older_turn_is_finished_later(fin):
    f = fin
    _seed(f["chats"], "c1", [_turn("Find people looking for a CRM", 60), _chat_turn("Hello", "Hi there!")])
    resp, elapsed, tasks = _view(f, "c1")
    assert resp.status_code == 200
    assert elapsed < 1.0, f"GET /chat took {elapsed:.2f}s with a {SCAN_SECONDS}s scan pending"
    html = resp.body.decode()
    assert "Researching" in html                       # still honestly unfinished on this render
    assert tasks.tasks == []                           # nothing tied to this response
    m = _msg(f["chats"], "c1", "Find people looking for a CRM")
    assert m["results"] is None and m["claude_answer"] is None      # not faked as done/failed
    # ... and it IS finished afterwards, without another message or visit
    _idle(f, "c1", "Find people looking for a CRM")
    m = _msg(f["chats"], "c1", "Find people looking for a CRM")
    ans = json.loads(m["claude_answer"])
    assert ans["format"] == "source_list" and ans["headline"] == "People are looking for a CRM"
    links = [p.get("link") for pl in ans["platforms"] for p in pl["posts"]]
    assert links == [POSTS[0]["post_url"], POSTS[1]["post_url"]]   # real source links patched in
    assert m["results"] and m["results"][0]["post_url"] == POSTS[0]["post_url"]
    assert f["calls"]["scan"] == 1 and f["calls"]["analyze"] == 1
    # reopening shows it completed, promptly, and starts nothing new
    resp2, elapsed2, _ = _view(f, "c1")
    assert elapsed2 < 1.0
    html2 = resp2.body.decode()
    assert "status-completed" in html2 and "Researching</span>" not in html2
    time.sleep(0.3)
    assert f["calls"]["scan"] == 1 and f["calls"]["analyze"] == 1


# ── 2. eventually processed with no page view at all ─────────────────────────
def test_new_turn_is_queued_and_finished_by_the_worker_without_any_visit(fin):
    f = fin
    idx, tf = f["idx"], f["tf"]
    _seed(f["chats"], "c2", [])
    idx.add_search_to_chat("c2", OWNER, "Find people looking for a CRM", "find people looking for a crm",
                           ["crm"], "reddit", evidence_required=15)
    jid = tf.job_id("c2", OWNER, "find people looking for a crm")
    job = f["queue"].find_one({"_id": jid})
    assert job is not None and job["lease_owner"] is None
    due_in = (job["due_at"].replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    # not due before its stream has had time to take the lease
    assert tf.NEW_TURN_DELAY_SECONDS - 5 < due_in <= tf.NEW_TURN_DELAY_SECONDS
    assert tf.claim() is None
    # nobody streams it and nobody reopens the chat: time passes
    old = datetime.now(timezone.utc) - timedelta(seconds=idx.RESPONSE_TIMEOUT + 90)
    f["chats"].update_one({"chat_id": "c2"}, {"$set": {"messages.0.requested_at": old}})
    f["queue"].update_one({"_id": jid}, {"$set": {"due_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    f["knobs"]["scan_seconds"] = 0.2
    _start_worker(tf)
    _idle(f, "c2", "Find people looking for a CRM")
    m = _msg(f["chats"], "c2", "Find people looking for a CRM")
    assert json.loads(m["claude_answer"])["format"] == "source_list" and m["results"]
    assert f["calls"]["analyze"] == 1


def test_backfill_finds_unfinished_turns_nobody_queued(fin):
    f = fin
    tf = f["tf"]
    _seed(f["chats"], "c3", [_turn("Find people looking for a CRM", 900), _chat_turn("Hello", "Hi!"),
                             _turn("Already answered", 900, answered=True)])
    assert f["queue"].count_documents({}) == 0
    assert tf.backfill_if_due() == 1
    assert f["queue"].find_one({"_id": tf.job_id("c3", OWNER, "find people looking for a crm")})
    assert tf.backfill_if_due() == 0                    # one run per interval across processes
    f["knobs"]["scan_seconds"] = 0.1
    assert tf.process_due() == 1
    assert _finished(f, "c3", "Find people looking for a CRM")
    assert f["queue"].count_documents({"_id": {"$ne": tf.BACKFILL_DOC_ID}}) == 0


# ── 3. no duplicates, no races ───────────────────────────────────────────────
def test_concurrent_page_views_and_workers_process_the_turn_once(fin):
    f = fin
    tf = f["tf"]
    f["knobs"]["scan_seconds"] = 0.8
    _seed(f["chats"], "c4", [_turn("Find people looking for a CRM", 60), _chat_turn("Hello", "Hi!")])
    _start_worker(tf)
    errors, times = [], []

    def view():
        try:
            _, el, _ = _view(f, "c4")
            times.append(el)
        except Exception as exc:   # pragma: no cover
            errors.append(exc)

    def work():
        try:
            tf.process_due()
        except Exception as exc:   # pragma: no cover
            errors.append(exc)
    threads = [threading.Thread(target=view) for _ in range(8)] + [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors
    assert max(times) < 1.0
    _idle(f, "c4", "Find people looking for a CRM", extra=1.0)
    assert f["calls"]["scan"] == 1, f["calls"]
    assert f["calls"]["analyze"] == 1, f["calls"]


def test_lease_is_exclusive_expires_and_only_its_holder_can_release(fin, monkeypatch):
    f = fin
    tf = f["tf"]
    assert tf.enqueue("c5", OWNER, "t", due_in_seconds=0)
    assert tf.enqueue("c5", OWNER, "t", due_in_seconds=0)          # idempotent
    assert f["queue"].count_documents({}) == 1
    first = tf.claim()
    assert first and first["lease_owner"]
    assert tf.claim() is None                                      # held
    assert tf.enqueue("c5", OWNER, "t", pull_forward=True)         # a page view cannot steal it
    assert tf.claim() is None
    stale = dict(first, lease_owner="someone-else")
    tf._finish(stale)                                              # non-holder: no effect
    assert f["queue"].count_documents({}) == 1
    # a crashed holder: the lease expires and the turn is claimed again
    f["queue"].update_one({"_id": first["_id"]}, {"$set": {"due_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    second = tf.claim()
    assert second and second["lease_owner"] != first["lease_owner"]
    tf._release(first, 0, count_attempt=True)                      # old holder: no effect
    assert f["queue"].find_one({"_id": first["_id"]})["lease_owner"] == second["lease_owner"]
    tf._finish(second)
    assert f["queue"].count_documents({}) == 0


# ── 4. never marked completed/failed just to move on ─────────────────────────
def test_turn_waiting_for_evidence_stays_unfinished_and_is_retried(fin):
    f = fin
    tf = f["tf"]
    f["knobs"].update(scan_seconds=0.05, posts=POSTS[:3])              # below the 15-post target
    _seed(f["chats"], "c6", [_turn("Find people looking for a CRM", 30), _chat_turn("Hello", "Hi!")])
    _view(f, "c6")
    assert _wait(lambda: f["calls"]["scan"] >= 1)
    jid = tf.job_id("c6", OWNER, "find people looking for a crm")
    assert _wait(lambda: (f["queue"].find_one({"_id": jid}) or {}).get("lease_owner") is None)
    job = f["queue"].find_one({"_id": jid})
    m = _msg(f["chats"], "c6", "Find people looking for a CRM")
    assert m["results"] is None and m["claude_answer"] is None
    assert f["calls"]["analyze"] == 0 and job["attempts"] == 0
    due_in = (job["due_at"].replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    assert 0 < due_in <= tf.PENDING_RETRY_SECONDS + 1
    # once the evidence is there, the next pass finishes it normally
    f["knobs"]["posts"] = POSTS
    f["queue"].update_one({"_id": jid}, {"$set": {"due_at": datetime.now(timezone.utc)}})
    assert tf.process_due() == 1 and _finished(f, "c6", "Find people looking for a CRM")


def test_failed_answer_call_is_retried_with_backoff_not_marked_failed(fin):
    f = fin
    tf = f["tf"]
    f["knobs"].update(scan_seconds=0.05, analyze_error=RuntimeError("llm down"))
    _seed(f["chats"], "c7", [_turn("Find people looking for a CRM", 900), _chat_turn("Hello", "Hi!")])
    jid = tf.job_id("c7", OWNER, "find people looking for a crm")
    tf.enqueue("c7", OWNER, "find people looking for a crm")
    assert tf.process_due() == 1
    m = _msg(f["chats"], "c7", "Find people looking for a CRM")
    # still unfinished, not "failed" (results are saved even when the answer
    # call fails — unchanged _complete_message_answer_and_results() behaviour)
    assert m["claude_answer"] is None
    assert "Failed" not in json.dumps(m, default=str) and "error" not in m
    job = f["queue"].find_one({"_id": jid})
    assert job["attempts"] == 1 and job["lease_owner"] is None
    assert f["busy"].count_documents({}) == 0                       # busy lock released as before
    f["knobs"]["analyze_error"] = None
    f["queue"].update_one({"_id": jid}, {"$set": {"due_at": datetime.now(timezone.utc)}})
    assert tf.process_due() == 1 and _finished(f, "c7", "Find people looking for a CRM")


def test_empty_evidence_after_timeout_uses_the_existing_fallback(fin):
    """No-results behaviour: past RESPONSE_TIMEOUT with nothing found, the
    same _timeout_fallback_answer() path as before is taken (once)."""
    f = fin
    tf = f["tf"]
    f["knobs"].update(scan_seconds=0.05, posts=[])
    _seed(f["chats"], "c8", [_turn("Find people looking for a CRM", f["idx"].RESPONSE_TIMEOUT + 5),
                             _chat_turn("Hello", "Hi!")])
    tf.enqueue("c8", OWNER, "find people looking for a crm")
    assert tf.process_due() == 1
    assert f["calls"]["fallback"] == 1 and f["calls"]["analyze"] == 0


# ── 5. the stream keeps its own turn; busy owners are not overlapped ─────────
TK = "find people looking for a crm"


def _stream(f, monkeypatch, chat_id, topic_key=TK):
    """The REAL stream_answer route; returns its raw SSE generator."""
    import types
    rt = f["rt"]
    monkeypatch.setattr(rt, "StreamingResponse", lambda gen, **kw: types.SimpleNamespace(body_iterator=gen))
    return iter(rt.stream_answer(_request(f["idx"], f"/chat/{chat_id}/stream"), chat_id, topic_key).body_iterator)


@pytest.fixture
def stream_guard(fin, monkeypatch):
    """Records any scan/answer the STREAM itself would start (routes' own
    bindings), so a test can prove the stream did not duplicate the work."""
    rt = fin["rt"]
    hits = {"scan": 0, "analyze": 0}

    def no_scan(**kw):
        hits["scan"] += 1
        return []

    def no_analyze(*a, **k):
        hits["analyze"] += 1
        return "{}"
    monkeypatch.setattr(rt, "get_evidence_with_topup", no_scan)
    monkeypatch.setattr(rt, "analyze_with_claude", no_analyze, raising=False)
    return hits


def test_newest_pending_turn_is_left_to_its_stream(fin, monkeypatch, stream_guard):
    f = fin
    tf = f["tf"]
    _seed(f["chats"], "c9", [_turn("Find people looking for a CRM", 5)])
    resp, elapsed, _ = _view(f, "c9")
    assert elapsed < 1.0 and 'data-pending-stream="true"' in resp.body.decode()
    assert f["queue"].count_documents({}) == 0                    # the page did not queue it
    tf.enqueue("c9", OWNER, TK)                                   # e.g. created by add_search_to_chat, now due
    gen = _stream(f, monkeypatch, "c9")
    assert next(gen).startswith("retry:")                         # the stream has started on the turn
    try:
        job = f["queue"].find_one({})
        assert job["lease_owner"].startswith(tf.STREAM_LEASE_PREFIX)
        assert tf.process_due() == 0                              # the worker cannot claim a streamed turn
        assert f["calls"]["scan"] == 0                            # not scanned behind the stream's back
    finally:
        gen.close()


# ── 5b. stream lease: worker and stream never work on the same turn ──────────
def test_stream_and_worker_leases_exclude_each_other(fin, monkeypatch):
    f = fin
    tf = f["tf"]
    woken = []
    monkeypatch.setattr(tf, "wake", lambda ids=None: woken.append(list(ids or [])) or "recorded")
    # a stream holds the turn: no worker claim, even when due
    s1 = tf.hold_for_stream("c11", OWNER, TK)
    assert s1 and s1.startswith(tf.STREAM_LEASE_PREFIX)
    f["queue"].update_one({}, {"$set": {"due_at": datetime.now(timezone.utc) + timedelta(seconds=60)}})
    assert tf.claim() is None and not tf.worker_holds("c11", OWNER, TK)
    # a second stream (reconnect / second tab) takes it over; the first can no longer release it
    s2 = tf.hold_for_stream("c11", OWNER, TK)
    assert s2 and s2 != s1
    assert tf.release_stream("c11", OWNER, TK, s1) is False
    assert f["queue"].find_one({})["lease_owner"] == s2 and woken == []
    # releasing hands the turn back, due NOW (no waiting period), and wakes the worker
    assert tf.release_stream("c11", OWNER, TK, s2) is True
    assert woken == [[tf.job_id("c11", OWNER, TK)]]
    job = f["queue"].find_one({})
    assert job["lease_owner"] is None
    assert job["due_at"].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    # a worker holds the turn: a stream cannot take it (unless forced after waiting)
    w = tf.claim()
    assert w and tf.worker_holds("c11", OWNER, TK)
    assert tf.hold_for_stream("c11", OWNER, TK) is None
    forced = tf.hold_for_stream("c11", OWNER, TK, force=True)
    assert forced and f["queue"].find_one({})["lease_owner"] == forced
    tf._finish(w)                                                 # the old worker lease is gone: no effect
    tf._release(w, 0, count_attempt=True)
    assert f["queue"].find_one({})["lease_owner"] == forced
    # an expired worker lease (crashed worker) can be taken by a stream
    tf.release_stream("c11", OWNER, TK, forced)
    w2 = tf.claim()
    f["queue"].update_one({}, {"$set": {"due_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    assert not tf.worker_holds("c11", OWNER, TK)
    assert tf.hold_for_stream("c11", OWNER, TK) and w2


@pytest.mark.parametrize("website_only", [False, True])
def test_stream_waits_for_the_workers_answer_instead_of_computing_another(fin, monkeypatch, stream_guard,
                                                                         website_only):
    f = fin
    tf, idx = f["tf"], f["idx"]
    turn = _turn("Find people looking for a CRM", 30)
    turn["website_only"] = website_only
    _seed(f["chats"], "c12", [turn])
    tf.enqueue("c12", OWNER, TK)
    w = tf.claim()                                                # the worker is processing this turn
    assert w
    gen = _stream(f, monkeypatch, "c12")
    out = []
    th = threading.Thread(target=lambda: out.extend(gen), daemon=True)
    th.start()
    time.sleep(1.2)
    assert th.is_alive() and not any('"delta"' in c for c in out)   # waiting, not answering
    answer = _answer_for(POSTS)
    idx.save_signal_results_to_chat("c12", OWNER, TK, POSTS[:2])     # the worker's result lands
    idx.save_claude_answer_to_chat("c12", OWNER, TK, answer)
    tf._finish(w)
    th.join(10)
    assert not th.is_alive()
    deltas = [json.loads(c[6:]) for c in out if c.startswith("data: ") and '"delta"' in c]
    done = [json.loads(c[6:]) for c in out if c.startswith("data: ") and '"done"' in c]
    assert "".join(d["delta"] for d in deltas) == answer             # the worker's answer, replayed
    assert done and done[0]["results"][0]["post_url"] == POSTS[0]["post_url"]
    assert stream_guard == {"scan": 0, "analyze": 0}                 # no second scan / answer
    assert f["queue"].count_documents({}) == 0


def test_stream_takes_over_when_the_worker_lets_go_unanswered(fin, monkeypatch, stream_guard):
    f = fin
    tf = f["tf"]
    _seed(f["chats"], "c13", [_turn("Find people looking for a CRM", 30)])
    tf.enqueue("c13", OWNER, TK)
    w = tf.claim()
    gen = _stream(f, monkeypatch, "c13")
    first = []
    th = threading.Thread(target=lambda: first.append(next(gen)), daemon=True)
    th.start()
    time.sleep(1.2)
    assert not first                                               # still waiting for the worker
    tf._release(w, 20, count_attempt=False)                        # worker pass ended, still no answer
    th.join(10)
    try:
        assert first and first[0].startswith("retry:")             # the stream now runs its own flow
        assert f["queue"].find_one({})["lease_owner"].startswith(tf.STREAM_LEASE_PREFIX)
        assert tf.process_due() == 0
    finally:
        gen.close()


# ── 5c. dropped stream: the worker finishes the turn right away ─────────────
def test_dropped_stream_hands_the_turn_to_the_worker_immediately(fin, monkeypatch, stream_guard):
    f = fin
    tf = f["tf"]
    f["knobs"]["scan_seconds"] = 0.2
    _seed(f["chats"], "c14", [_turn("Find people looking for a CRM", 5)])   # newest turn, 5 s old
    tf.enqueue("c14", OWNER, TK, due_in_seconds=tf.NEW_TURN_DELAY_SECONDS)
    gen = _stream(f, monkeypatch, "c14")
    assert next(gen).startswith("retry:")
    assert tf.process_due() == 0                                   # streamed: not the worker's
    t0 = time.perf_counter()
    gen.close()                                                    # the browser went away mid-research
    # released and woken: finished right away — no seven-minute wait
    assert _wait(lambda: _finished(f, "c14", "Find people looking for a CRM")
                 and f["queue"].count_documents({}) == 0, 5)
    assert time.perf_counter() - t0 < 5
    time.sleep(0.3)
    assert f["calls"]["scan"] == 1 and f["calls"]["analyze"] == 1


def test_stream_that_finishes_normally_leaves_nothing_for_the_worker(fin, monkeypatch, stream_guard):
    f = fin
    tf, idx = f["tf"], f["idx"]
    _seed(f["chats"], "c15", [_turn("Find people looking for a CRM", 5)])
    tf.enqueue("c15", OWNER, TK, due_in_seconds=tf.NEW_TURN_DELAY_SECONDS)
    gen = _stream(f, monkeypatch, "c15")
    next(gen)
    idx.save_signal_results_to_chat("c15", OWNER, TK, POSTS[:2])   # what the stream saves on completion
    idx.save_claude_answer_to_chat("c15", OWNER, TK, _answer_for(POSTS))
    gen.close()                                                    # release wakes the worker:
    assert _wait(lambda: f["queue"].count_documents({}) == 0, 5)  # one cheap check removes it ...
    time.sleep(0.3)
    assert f["calls"]["scan"] == 0 and f["calls"]["analyze"] == 0  # ... and no work is redone


# ── 5d. crash recovery: never removed before the turn is persisted ─────────
def test_turn_stays_queued_until_answer_and_results_are_persisted(fin, monkeypatch):
    f = fin
    tf, idx = f["tf"], f["idx"]
    f["knobs"]["scan_seconds"] = 0.05
    _seed(f["chats"], "c16", [_turn("Find people looking for a CRM", 60), _chat_turn("Hello", "Hi!")])
    tf.enqueue("c16", OWNER, TK)
    real_save = idx.save_claude_answer_to_chat

    def failing_save(*a, **k):
        raise RuntimeError("mongo write failed")
    monkeypatch.setattr(idx, "save_claude_answer_to_chat", failing_save)
    assert tf.process_due() == 1
    job = f["queue"].find_one({})
    assert job is not None and job["lease_owner"] is None          # still queued
    assert _msg(f["chats"], "c16", "Find people looking for a CRM")["claude_answer"] is None
    monkeypatch.setattr(idx, "save_claude_answer_to_chat", real_save)
    f["queue"].update_one({}, {"$set": {"due_at": datetime.now(timezone.utc)}})
    assert tf.process_due() == 1 and _finished(f, "c16", "Find people looking for a CRM")
    assert f["queue"].count_documents({}) == 0


def test_turn_claimed_by_a_crashed_worker_is_finished_after_the_lease_expires(fin):
    f = fin
    tf = f["tf"]
    f["knobs"]["scan_seconds"] = 0.05
    _seed(f["chats"], "c17", [_turn("Find people looking for a CRM", 60), _chat_turn("Hello", "Hi!")])
    tf.enqueue("c17", OWNER, TK)
    crashed = tf.claim()                                           # claimed, then the process died
    assert crashed and tf.process_due() == 0                       # nobody else touches it meanwhile
    f["queue"].update_one({}, {"$set": {"due_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    assert tf.process_due() == 1 and _finished(f, "c17", "Find people looking for a CRM")
    assert f["calls"]["analyze"] == 1 and f["queue"].count_documents({}) == 0


def test_backfill_takes_newest_chats_first_without_an_updated_at_sort(fin, monkeypatch):
    f = fin
    tf = f["tf"]
    monkeypatch.setattr(tf, "BACKFILL_MAX_CHATS", 1)
    _seed(f["chats"], "older", [_turn("Find people looking for a CRM", 900)])
    _seed(f["chats"], "newer", [_turn("Find people looking for a CRM", 900)])
    # the OLDER chat was updated most recently: order must follow creation (_id), not updated_at
    f["chats"].update_one({"chat_id": "older"}, {"$set": {"updated_at": datetime.now(timezone.utc) + timedelta(days=1)}})
    assert tf.backfill_if_due() == 1
    assert f["queue"].find_one({"_id": tf.job_id("newer", OWNER, TK)}) is not None


def test_owner_busy_defers_without_scanning(fin):
    f = fin
    tf, idx = f["tf"], f["idx"]
    _seed(f["chats"], "c10", [_turn("Find people looking for a CRM", 60), _chat_turn("Hello", "Hi!")])
    idx._set_owner_busy(OWNER)                                    # e.g. a stream for this owner is running
    tf.enqueue("c10", OWNER, "find people looking for a crm")
    assert tf.process_due() == 1
    assert f["calls"]["scan"] == 0
    job = f["queue"].find_one({})
    assert job["attempts"] == 0 and job["lease_owner"] is None
    idx._clear_owner_busy(OWNER)
    f["knobs"]["scan_seconds"] = 0.05
    f["queue"].update_one({}, {"$set": {"due_at": datetime.now(timezone.utc)}})
    assert tf.process_due() == 1 and _finished(f, "c10", "Find people looking for a CRM")


# ── 6. real browser, real HTTP server with the worker started at startup ─────
@pytest.fixture
def live_server(fin):
    uvicorn = pytest.importorskip("uvicorn")
    import socket
    idx = fin["idx"]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(idx.app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.02)
    assert fin["tf"].is_running()                                 # started by the real startup hook
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(timeout=10)


TURN_JS = """(q) => { const t = [...document.querySelectorAll('.turn-block')].find(
    b => b.querySelector('.turn-question').textContent.trim() === q);
  if (!t) return null; const chip = t.querySelector('.turn-chip');
  return {cls: chip ? chip.className : '', label: chip ? chip.querySelector('.turn-status-label').textContent.trim() : '',
          links: [...t.querySelectorAll('a[href*="reddit.com/r/crm/comments"]')].map(a => a.href),
          text: t.innerText}; }"""


def test_real_disconnect_releases_the_stream_and_the_worker_finishes_promptly(fin, live_server, monkeypatch):
    """A REAL HTTP client drops a research stream mid-scan (uvicorn + Starlette).
    Before the stream-disconnect fix the generator's finally only ran when
    Python's cyclic GC happened to run: the busy flag and the turn lease
    stayed held and the turn could not be finished."""
    import http.client
    from urllib.parse import urlsplit
    f = fin
    tf, idx, rt = f["tf"], f["idx"], f["rt"]
    fl = sys.modules["flintel"]
    monkeypatch.setattr(fl, "calculate_search_progress_percent", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(fl, "is_time_only_request", lambda r: False, raising=False)
    monkeypatch.setattr(rt, "_set_owner_busy", idx._set_owner_busy)      # real busy flag for the stream
    monkeypatch.setattr(rt, "_is_owner_busy", idx._is_owner_busy)

    def stream_scan(cancel_event=None, **kw):                             # the stream's own, slow scan
        end = time.time() + 30
        while time.time() < end and not (cancel_event and cancel_event.is_set()):
            time.sleep(0.05)
        return []
    monkeypatch.setattr(rt, "get_evidence_with_topup", stream_scan)
    f["knobs"]["scan_seconds"] = 0.3
    _seed(f["chats"], "cd", [_turn("Find people looking for a CRM", 5)])
    tf.enqueue("cd", OWNER, TK, due_in_seconds=tf.NEW_TURN_DELAY_SECONDS)
    u = urlsplit(live_server)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    conn.request("GET", "/chat/cd/stream?topic_key=" + TK.replace(" ", "%20"))
    resp = conn.getresponse()
    assert resp.status == 200
    lines = [resp.readline() for _ in range(3)]
    assert lines[0].startswith(b"retry:")
    jid = tf.job_id("cd", OWNER, TK)
    assert _wait(lambda: ((f["queue"].find_one({"_id": jid}) or {}).get("lease_owner") or "").startswith(
        tf.STREAM_LEASE_PREFIX), 5)
    assert f["busy"].count_documents({"owner_key": OWNER}) == 1
    time.sleep(0.5)
    t0 = time.perf_counter()
    conn.sock.shutdown(2)
    conn.close()                                                           # the browser went away
    assert _wait(lambda: f["busy"].count_documents({}) == 0, 8), "busy flag still set after the disconnect"
    assert _wait(lambda: _finished(f, "cd", "Find people looking for a CRM"), 10)
    assert time.perf_counter() - t0 < 10                                   # not minutes
    assert f["calls"]["analyze"] == 1
    assert _wait(lambda: f["queue"].find_one({"_id": jid}) is None, 5)


def test_browser_hello_is_fast_and_older_research_finishes(browser, fin, live_server):
    f = fin
    query = "Find people looking for a CRM"
    _seed(f["chats"], "cb", [_turn(query, 60)])
    # the research turn is no longer the newest one once "Hello" is sent
    page = _open(browser, live_server)
    try:
        page.goto(live_server + "/chat/cb")
        _wait_chat(page, "cb")
        page.evaluate("document.querySelectorAll('[class*=welcome]').forEach(e => e.remove()); 0")
        timings = []
        for text in ("Hello", "Thanks"):
            n = page.evaluate("document.querySelectorAll('.turn-block').length")
            page.fill("#search-input", text)
            t0 = time.perf_counter()
            page.locator("#search-input").press("Enter")
            page.wait_for_function(
                "n => { const t = document.querySelectorAll('.turn-block'); const l = t[t.length - 1];"
                " return t.length > n && !!l.querySelector('.answer-wrap') &&"
                " l.querySelector('.answer-wrap').innerText.trim().length > 0; }", arg=n, timeout=10000)
            timings.append(time.perf_counter() - t0)
        assert max(timings) < 1.5, timings                      # the 3 s scan is not on this path
        assert f["state"]["calls"] == []                        # greetings: no LLM
        # the older research turn is finished in the background, exactly once
        _idle(f, "cb", query)
        assert f["calls"]["scan"] == 1 and f["calls"]["analyze"] == 1
        # ... by the worker (event-loop executor), never in a request thread
        assert not any("AnyIO" in name for name in f["calls"]["scan_threads"]), f["calls"]["scan_threads"]
        # reopening the chat shows the right results and statuses
        t0 = time.perf_counter()
        page.goto(live_server + "/chat/cb")
        _wait_chat(page, "cb")
        reopen = time.perf_counter() - t0
        research = page.evaluate(TURN_JS, query)
        hello = page.evaluate(TURN_JS, "Hello")
        assert "status-completed" in research["cls"] and research["label"].startswith("Researched"), research
        assert POSTS[0]["post_url"] in research["links"], research
        assert hello is not None and hello["cls"] == "" and "Researching" not in hello["text"]
        assert page.evaluate("document.querySelectorAll('[data-pending-stream=\"true\"]').length") == 0
        assert reopen < 1.5
        assert page._errors == []
        time.sleep(0.5)
        assert f["calls"]["scan"] == 1 and f["calls"]["analyze"] == 1
    finally:
        page.close()
