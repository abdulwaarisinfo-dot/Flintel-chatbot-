"""
tests/test_multiworker_recovery.py — a refresh / reconnect that is served by ANOTHER worker must not repeat work.

"Another worker" shares MongoDB and nothing else. mongomock cannot be shared between two processes, so the second worker is
simulated by forgetting every piece of PROCESS-LOCAL state (scan registry, stop registry, in-memory busy fallback) while the
first worker's threads keep running, exactly as they would in their own process; the database stays shared. What these tests
prove is therefore that the recovery decisions are made from the turn's own message, not that MongoDB itself is atomic (see
tests/test_mongo_atomicity.py, which needs a real server).
"""
import threading
import time

import pytest

from tests.test_independent_chats_stop_retry import (  # noqa: F401  (fixtures + helpers)
    conc, ui, env, app_env, fin, live_server, Q_A, Q_B, OWNER, POSTS, _tk, _request, send, chat_of, wait, cancel, msg_of, busy_keys,
    slot_holders, _race, _answer_for,
)
import turn_control


def abortable_stream(f, chat_id, query, owner=OWNER):
    """Like start_stream(), but the 'browser' can disconnect: the generator is closed at its next yield (as the server does)."""
    resp = f["rt"].stream_answer(_request(f, "GET", f"/chat/{chat_id}/stream", owner=owner), chat_id, _tk(query))
    gen = resp._source_generator
    out = type("S", (), {})()
    out.events, out.error, out.abort = [], None, threading.Event()

    def run():
        try:
            for chunk in gen:
                out.events.append(chunk)
                if out.abort.is_set():
                    break
        except BaseException as exc:
            out.error = exc
        finally:
            try:
                gen.close()
            except BaseException:
                pass
    out.thread = threading.Thread(target=run, daemon=True)
    f["streams"].append(out)
    out.thread.start()
    out.joined = lambda t=15: (out.thread.join(t), not out.thread.is_alive())[1]
    out.text = lambda: "".join(c for c in out.events if isinstance(c, str))
    return out


def another_worker(f):
    """What a second worker process does not have: this process's memory. (The first worker's own threads keep running.)"""
    import logics
    with logics._SCAN_REGISTRY_LOCK:
        logics._SCAN_REGISTRY.clear()
    with turn_control._lock:
        turn_control._events.clear()
    f["idx"]._local_reset_for_tests()


@pytest.fixture
def mw(conc, monkeypatch):
    """conc + a persisted topic cache (what the real scan writes) + counters for everything a repeat would touch."""
    import sys
    import mongomock
    import turn_finisher
    f = conc
    # production runs the turn finisher in DRY RUN (it records but never processes turns), so a dropped stream is NOT finished by a
    # background worker: recovery is the next stream's job. (In "legacy" mode the worker finishes it; the browser refresh tests in
    # test_refresh_and_leases.py run in that default mode.)
    monkeypatch.setattr(turn_finisher, "mode", lambda: "dry_run")
    lg = sys.modules["logics"]
    cache = mongomock.MongoClient()["mw"].topic_evidence
    monkeypatch.setattr(lg, "topic_evidence_cache_collection", cache)
    real_scan, real_analyze = f["rt"].get_evidence_with_topup, f["rt"].analyze_with_claude
    n = {"enqueue": 0, "credit": 0}

    def scan(**kw):
        posts = real_scan(**kw)
        if posts and not kw["cancel_event"].is_set():
            lg.save_topic_evidence_cache(kw["chat_id"], kw["owner_key"], kw["topic_key"], list(posts), kw.get("keywords"))
        return posts
    monkeypatch.setattr(f["rt"], "get_evidence_with_topup", scan)
    real_enqueue, real_store = f["rt"].enqueue_search_job, f["rt"]._credit_store
    monkeypatch.setattr(f["rt"], "enqueue_search_job", lambda *a, **k: (n.__setitem__("enqueue", n["enqueue"] + 1), real_enqueue(*a, **k))[1])
    monkeypatch.setattr(f["rt"], "_credit_store", lambda *a, **k: (n.__setitem__("credit", n["credit"] + 1), real_store(*a, **k))[1])
    f["n"] = n
    return f


def _snap(f, chat):
    ctl, n = f["ctl"], f["n"]
    return {"msgs": len(f["chats"].find_one({"chat_id": chat})["messages"]), "scan": len(ctl.scan_started),
            "analyze": len(ctl.analyze_started), "enqueue": n["enqueue"], "credit": n["credit"]}


def _run_fields(f, chat, i=-1):
    m = msg_of(f, chat, i=i)
    return {k: m.get(k) for k in ("scan_lease_at", "scan_done_at", "pending_answer")}


# ═══ refresh served by another worker ═══════════════════════════════════════════════════════════════════════════════════
def test_other_worker_refresh_during_the_scan_waits_for_the_running_scan_and_never_scans_again(mw):
    f, ctl = mw, mw["ctl"]
    ctl.hold(Q_A, scan=True)
    chat = chat_of(send(f, Q_A))
    before = _snap(f, chat)
    a = abortable_stream(f, chat, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    assert _run_fields(f, chat)["scan_lease_at"] is not None                   # the scan thread holds a lease on the turn's message
    a.abort.set()                                                              # the browser refreshes ...
    assert a.joined(15)
    another_worker(f)                                                          # ... and the new stream lands on another worker
    b = abortable_stream(f, chat, Q_A)
    assert wait(lambda: ": waiting for the running scan" in b.text(), 20)      # B waits (its first events may follow the old duplicate-refresh wait)
    assert len(ctl.scan_started) == 1                                          # B did not start a second scan while A's runs
    ctl.release(Q_A)                                                           # A's scan thread finishes (in "its" process)
    assert b.joined(20) and '"done": true' in b.text()
    after = _snap(f, chat)
    assert after == {**before, "scan": 1, "analyze": 1}                       # one message, ONE scan, ONE model call, no new job / credit
    m = msg_of(f, chat)
    assert m["claude_answer"] and "scan_lease_at" not in m and "pending_answer" not in m and "scan_done_at" not in m


def test_other_worker_refresh_after_the_scan_finished_reuses_it_and_does_not_scan(mw):
    f, ctl = mw, mw["ctl"]
    ctl.hold(Q_A, analyze=True)
    chat = chat_of(send(f, Q_A))
    a = abortable_stream(f, chat, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.analyze_started)                       # scan done, model call running
    assert _run_fields(f, chat)["scan_done_at"] is not None
    a.abort.set()
    another_worker(f)
    ctl.release(Q_A)                                                           # the model call returns in the first worker
    assert a.joined(15)
    # whatever the first worker managed to save, a second stream must finish the turn without scanning or asking again
    if not msg_of(f, chat)["claude_answer"]:
        b = abortable_stream(f, chat, Q_A)
        assert b.joined(20)
    assert msg_of(f, chat)["claude_answer"]
    assert len(ctl.scan_started) == 1 and len(ctl.analyze_started) == 1


def test_other_worker_reuses_the_model_answer_a_dead_worker_left_on_the_message(mw, monkeypatch):
    f, ctl = mw, mw["ctl"]
    chat = chat_of(send(f, Q_A))
    rt = f["rt"]
    real_patch = rt._patch_post_urls_into_answer
    died = {"n": 0}

    def die_once(*a, **k):
        if not died["n"]:
            died["n"] = 1
            raise SystemExit("worker killed right after the model call")
        return real_patch(*a, **k)
    monkeypatch.setattr(rt, "_patch_post_urls_into_answer", die_once)
    a = abortable_stream(f, chat, Q_A)
    assert a.joined(20) and isinstance(a.error, SystemExit)
    m = msg_of(f, chat)
    assert m["claude_answer"] is None and m["pending_answer"]                  # paid for, not yet saved, durable
    assert len(ctl.analyze_started) == 1
    another_worker(f)
    b = abortable_stream(f, chat, Q_A)                                         # the reconnect, on another worker
    assert b.joined(20) and '"done": true' in b.text()
    assert len(ctl.analyze_started) == 1                                       # NO second model call
    m = msg_of(f, chat)
    assert m["claude_answer"] and "pending_answer" not in m
    assert _snap(f, chat)["msgs"] == 1 and f["n"]["enqueue"] == 1


def test_a_dead_workers_stale_scan_lease_is_taken_over_by_the_next_stream(mw):
    f, ctl = mw, mw["ctl"]
    chat = chat_of(send(f, Q_A))
    idx = f["idx"]
    rid = msg_of(f, chat)["request_id"]
    assert idx.claim_scan_lease(chat, OWNER, _tk(Q_A), rid)                    # "worker A" claims and then dies (never beats / finishes)
    b = abortable_stream(f, chat, Q_A)
    time.sleep(1.2)
    assert len(ctl.scan_started) == 0 and b.thread.is_alive()                  # the live lease is respected
    import datetime as dt
    f["chats"].update_one({"chat_id": chat}, {"$set": {"messages.0.scan_lease_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=idx.SCAN_LEASE_STALE_SECONDS + 5)}})
    assert b.joined(30) and '"done": true' in b.text()                         # the lease went stale: B scans and answers
    assert len(ctl.scan_started) == 1 and msg_of(f, chat)["claude_answer"]


def test_scan_lease_claims_are_atomic_and_the_run_state_follows_the_turn(mw):
    f, idx = mw, mw["idx"]
    chat = chat_of(send(f, Q_A))
    rid = msg_of(f, chat)["request_id"]
    got = _race(16, lambda i: idx.claim_scan_lease(chat, OWNER, _tk(Q_A), rid))
    assert sum(1 for g in got if g) == 1 and got.count(False) == 15, got      # exactly one scanner (the winner gets a lease id)
    st = idx.read_run_state(chat, OWNER, _tk(Q_A), rid)
    assert st["scan_running"] and not st["scan_done"] and not st["answered"]
    assert idx.mark_scan_done(chat, OWNER, _tk(Q_A), rid) is True
    st = idx.read_run_state(chat, OWNER, _tk(Q_A), rid)
    assert st["scan_done"] and not st["scan_running"]
    assert idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid, "raw answer") is True
    assert idx.read_run_state(chat, OWNER, _tk(Q_A), rid)["pending_answer"] == "raw answer"
    idx.save_claude_answer_to_chat(chat, OWNER, _tk(Q_A), "final")             # the real answer clears the run state
    assert _run_fields(f, chat) == {"scan_lease_at": None, "scan_done_at": None, "pending_answer": None}
    assert idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid, "late") is False   # nothing is written onto an answered turn


def test_run_state_is_never_written_onto_a_stopped_or_failed_turn(mw):
    f, idx = mw, mw["idx"]
    c1, c2 = chat_of(send(f, Q_A)), chat_of(send(f, Q_B))
    r1, r2 = msg_of(f, c1)["request_id"], msg_of(f, c2)["request_id"]
    assert idx.mark_turn_cancelled(c1, OWNER, topic_key=_tk(Q_A)) == idx.CANCEL_CANCELLED
    assert idx.mark_turn_failed(c2, OWNER, _tk(Q_B), r2) is True
    for chat, q, rid in ((c1, Q_A, r1), (c2, Q_B, r2)):
        assert idx.claim_scan_lease(chat, OWNER, _tk(q), rid) is False
        assert idx.mark_scan_done(chat, OWNER, _tk(q), rid) is False
        assert idx.save_pending_answer(chat, OWNER, _tk(q), rid, "x") is False
        assert _run_fields(f, chat) == {"scan_lease_at": None, "scan_done_at": None, "pending_answer": None}


# ═══ cancellation when only the stored flag is shared ═══════════════════════════════════════════════════════════════════
def test_a_stop_that_reaches_another_worker_stops_the_first_workers_run_through_the_stored_flag(mw):
    f, ctl = mw, mw["ctl"]
    ctl.hold(Q_A, scan=True)
    chat = chat_of(send(f, Q_A))
    a = abortable_stream(f, chat, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    idx = f["idx"]
    # the cancel request is handled by ANOTHER worker: its route writes the stored flag and clears the chat's busy flag, but it has
    # neither the first worker's stop signal nor its scan registry
    another_worker(f)
    assert cancel(f, chat, Q_A)[1]["status"] == "cancelled"
    assert a.joined(15) and '"cancelled": true' in a.text()
    assert wait(lambda: _tk(Q_A) in ctl.scan_cancelled, 10)                    # the first worker's scan was really cancelled
    assert msg_of(f, chat)["claude_answer"] is None and _run_fields(f, chat)["scan_done_at"] is None
    assert wait(lambda: busy_keys(f) == [] and slot_holders(f) == [], 10)
    another_worker(f)
    b = abortable_stream(f, chat, Q_A)                                         # a reload on another worker: stopped, nothing runs
    assert b.joined(10) and '"cancelled": true' in b.text() and len(ctl.scan_started) == 1 and not ctl.analyze_started


# ═══ explicit retry after a genuine failure ═════════════════════════════════════════════════════════════════════════════
def test_retry_after_a_genuine_failure_is_a_fresh_run_and_never_reuses_the_failed_runs_state(mw, monkeypatch):
    f, ctl = mw, mw["ctl"]
    rt = f["rt"]
    real = rt.analyze_with_claude
    calls = {"n": 0}

    def flaky_model(query, pool, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model down")
        return real(query, pool, **k)
    monkeypatch.setattr(rt, "analyze_with_claude", flaky_model)
    chat = chat_of(send(f, Q_A))
    a = abortable_stream(f, chat, Q_A)
    assert a.joined(20) and "analysis failed" in a.text()
    failed = msg_of(f, chat)
    assert failed["failed_at"] and failed["claude_answer"] is None and failed.get("scan_done_at")   # the failed run's scan WAS done
    another_worker(f)
    again = abortable_stream(f, chat, Q_A)                                     # a reload: shows the failure, runs nothing
    assert again.joined(10) and "analysis failed" in again.text() and calls["n"] == 1 and len(ctl.scan_started) == 1
    # explicit retry: a NEW message / request id on the same chat; it must scan and ask again, from its own empty state
    chat2 = chat_of(send(f, Q_A, chat_id=chat))
    assert chat2 == chat
    msgs = f["chats"].find_one({"chat_id": chat})["messages"]
    assert len(msgs) == 2 and msgs[1]["request_id"] != msgs[0]["request_id"]
    assert not any(msgs[1].get(k) for k in ("scan_lease_at", "scan_done_at", "pending_answer", "failed_at"))
    b = abortable_stream(f, chat, Q_A)
    assert b.joined(30) and '"done": true' in b.text()
    msgs = f["chats"].find_one({"chat_id": chat})["messages"]
    assert calls["n"] == 2 and len(ctl.scan_started) == 2                      # fresh scan and a fresh model call
    assert msgs[1]["claude_answer"] and msgs[0]["claude_answer"] is None and msgs[0]["failed_at"]   # the failed turn stays failed


# ═══ the page-view BackgroundTasks path ══════════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def bg(mw, monkeypatch):
    f = mw
    idx = f["idx"]
    calls = {"n": 0, "gate": None}

    def analyze(query, pool, extra_context=None, **k):
        calls["n"] += 1
        if calls["gate"] is not None:
            calls["gate"].wait(10)
        return _answer_for(pool)
    monkeypatch.setattr(idx, "analyze_with_claude", analyze)
    f["calls"] = calls
    return f


def _msg(f, chat):
    return msg_of(f, chat)


def test_background_path_takes_the_busy_flag_atomically_one_worker_wins(bg):
    idx = bg["idx"]
    got = _race(16, lambda i: idx._take_busy(f"{OWNER}::bg-chat"))
    assert got.count(True) == 1 and got.count(False) == 15, got
    idx._clear_owner_busy(f"{OWNER}::bg-chat")
    assert idx._take_busy(f"{OWNER}::bg-chat") is True


def test_background_task_keeps_its_lease_alive_while_it_runs_and_frees_everything_after(bg, monkeypatch):
    import datetime as dt
    f, idx = bg, bg["idx"]
    monkeypatch.setattr(idx, "LEASE_HEARTBEAT_SECONDS", 0.05)
    chat = chat_of(send(f, Q_A))
    msg = _msg(f, chat)
    key = idx.busy_scope_key(OWNER, chat)
    assert idx._take_busy(key)
    f["calls"]["gate"] = threading.Event()
    t = threading.Thread(target=idx._complete_message_answer_and_results, args=(chat, OWNER, msg, list(POSTS)), daemon=True)
    t.start()
    assert wait(lambda: f["calls"]["n"] == 1, 10)
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=idx.BUSY_FLAG_TIMEOUT_SECONDS + 100)
    f["busy"].update_many({}, {"$set": {"started_at": old}})                   # "the task has been running for 8 minutes"
    assert wait(lambda: idx._is_owner_busy(key), 3)                            # the heartbeat renewed it: still protected
    assert idx.acquire_owner_busy(key) is False
    f["calls"]["gate"].set()
    t.join(15)
    assert _msg(f, chat)["claude_answer"]
    assert wait(lambda: sum(th.name == "busy-lease" for th in threading.enumerate()) == 0, 3)


def test_background_task_finishes_a_turn_with_the_raw_answer_a_dead_task_left_behind(bg, monkeypatch):
    f, idx = bg, bg["idx"]
    chat = chat_of(send(f, Q_A))
    msg = _msg(f, chat)
    real_patch = idx._patch_post_urls_into_answer
    died = {"n": 0}

    def die_once(*a, **k):
        if not died["n"]:
            died["n"] = 1
            raise SystemExit("task killed after the model call")
        return real_patch(*a, **k)
    monkeypatch.setattr(idx, "_patch_post_urls_into_answer", die_once)
    with pytest.raises(SystemExit):
        idx._complete_message_answer_and_results(chat, OWNER, msg, list(POSTS))
    assert _msg(f, chat)["pending_answer"] and f["calls"]["n"] == 1 and _msg(f, chat)["claude_answer"] is None
    another_worker(f)
    idx._complete_message_answer_and_results(chat, OWNER, _msg(f, chat), list(POSTS))     # the next page view, on another worker
    m = _msg(f, chat)
    assert m["claude_answer"] and "pending_answer" not in m and f["calls"]["n"] == 1      # finished with the SAME answer


def test_background_task_does_nothing_for_a_stopped_or_failed_turn(bg):
    f, idx = bg, bg["idx"]
    c1, c2 = chat_of(send(f, Q_A)), chat_of(send(f, Q_B))
    assert idx.mark_turn_cancelled(c1, OWNER, topic_key=_tk(Q_A)) == idx.CANCEL_CANCELLED
    assert idx.mark_turn_failed(c2, OWNER, _tk(Q_B), msg_of(f, c2)["request_id"]) is True
    for chat in (c1, c2):
        idx._fill_in_message_outputs(chat, OWNER, [_msg(f, chat)], background_tasks=None)
    assert f["calls"]["n"] == 0
    assert _msg(f, c1)["claude_answer"] is None and _msg(f, c2)["claude_answer"] is None


def test_two_workers_viewing_the_chat_at_once_start_exactly_one_background_answer(bg):
    f, idx = bg, bg["idx"]
    chat = chat_of(send(f, Q_A))
    f["calls"]["gate"] = threading.Event()

    def view(i):
        idx._fill_in_message_outputs(chat, OWNER, [_msg(f, chat)], background_tasks=None)
    t = threading.Thread(target=lambda: _race(6, view), daemon=True)
    t.start()
    assert wait(lambda: f["calls"]["n"] >= 1, 15)
    time.sleep(0.5)
    f["calls"]["gate"].set()
    t.join(30)
    assert f["calls"]["n"] == 1 and _msg(f, chat)["claude_answer"]


# ═══ credits / jobs / messages are idempotent whichever worker serves the request ═════════════════════════════════════
from tests.test_search_credit_routes import app, flat_cost, credits_doc, used, KEY1, KEY2  # noqa: E402,F401
import search_credits as sc  # noqa: E402


@pytest.mark.usefixtures("flat_cost")
def test_a_replayed_submit_on_another_worker_neither_recharges_nor_duplicates(app):
    a = app
    first = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    chat = first.headers["location"].rsplit("/", 1)[-1]
    assert used(a) == 1 and len(a["state"]["enqueues"]) == 1
    import logics
    with logics._SCAN_REGISTRY_LOCK:
        logics._SCAN_REGISTRY.clear()                                          # "another worker"
    for _ in range(3):
        again = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
        assert again.status_code == 303 and again.headers["location"].endswith(chat)
    assert used(a) == 1 and len(a["state"]["enqueues"]) == 1                   # one charge, one collector job
    assert len(a["db"].chats.find_one({"chat_id": chat})["messages"]) == 1     # one message
    assert sc.check_invariant(credits_doc(a)) == []
