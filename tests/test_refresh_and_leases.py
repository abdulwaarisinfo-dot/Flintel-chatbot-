"""
tests/test_refresh_and_leases.py — P0 follow-up: (3) a refresh must not restart an active run, (2) live searches keep their lease.
(1) lives in the browser tests of tests/test_cancel_confirmation.py.
"""
import sys
import threading
import time

import pytest

from tests.test_scan_resilience import lg, _set_cfg  # noqa: F401  (fixture + helper)


# ═══ Issue 3 root cause, at the unit level: the scan registry ═══════════════════════════════════════════════════════════
def _scan_that_waits(calls, gate):
    def scan(ev):
        calls.append(ev)
        end = time.time() + 10
        while not gate.is_set() and not ev.is_set() and time.time() < end:
            time.sleep(0.01)
        return [] if ev.is_set() else ["post"]
    return scan


def test_a_refresh_reattaches_to_the_same_scan_within_the_grace_period(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=True, SINGLE_FLIGHT_STALE_SECONDS=600, SCAN_DISCONNECT_GRACE_SECONDS=5)
    calls, gate = [], threading.Event()
    h1 = lg.acquire_scan("chat", "tk", _scan_that_waits(calls, gate))
    time.sleep(0.05)
    h1.release()                                            # the page was refreshed: its stream closed
    assert not h1.cancel_event.is_set()                     # BEFORE: cancelled here, and started again from scratch
    h2 = lg.acquire_scan("chat", "tk", _scan_that_waits(calls, gate))   # the new stream
    assert not h2.started_here and len(calls) == 1          # the SAME run: no second scan
    assert h2.future is h1.future
    gate.set()
    assert h2.future.result(5) == ["post"] and not h2.cancel_event.is_set()
    h2.release()


def test_an_abandoned_scan_is_cancelled_when_the_grace_period_ends(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=True, SINGLE_FLIGHT_STALE_SECONDS=600, SCAN_DISCONNECT_GRACE_SECONDS=0.3)
    calls, gate = [], threading.Event()
    h = lg.acquire_scan("chat", "tk-abandoned", _scan_that_waits(calls, gate))
    h.release()
    assert not h.cancel_event.is_set()
    end = time.time() + 3
    while not h.cancel_event.is_set() and time.time() < end:
        time.sleep(0.02)
    assert h.cancel_event.is_set()                          # nobody came back: the work does not run forever
    assert ("chat", "tk-abandoned") not in lg._SCAN_REGISTRY
    h2 = lg.acquire_scan("chat", "tk-abandoned", _scan_that_waits(calls, gate))
    assert h2.started_here and len(calls) == 2              # a later request starts a fresh scan
    gate.set()
    h2.release(cancel_now=True)


def test_stop_cancels_the_scan_at_once_with_or_without_a_waiter(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=True, SINGLE_FLIGHT_STALE_SECONDS=600, SCAN_DISCONNECT_GRACE_SECONDS=30)
    calls, gate = [], threading.Event()
    h = lg.acquire_scan("chat", "tk-a", _scan_that_waits(calls, gate))
    h.release(cancel_now=True)                              # Stop with the stream still attached
    assert h.cancel_event.is_set()
    h2 = lg.acquire_scan("chat", "tk-b", _scan_that_waits(calls, gate))
    h2.release()                                            # stream gone (refresh) -> grace period running
    assert not h2.cancel_event.is_set()
    assert lg.cancel_scan("chat", "tk-b") is True           # ... and the user then presses Stop: no stream is needed
    assert h2.cancel_event.is_set()
    assert lg.cancel_scan("chat", "tk-b") is False          # idempotent
    assert lg.cancel_scan("chat", "never-started") is False
    gate.set()


def test_other_chats_scans_are_not_touched_by_a_grace_expiry_or_a_stop(lg, monkeypatch):
    _set_cfg(monkeypatch, SINGLE_FLIGHT_SCAN_ENABLED=True, SINGLE_FLIGHT_STALE_SECONDS=600, SCAN_DISCONNECT_GRACE_SECONDS=0.2)
    calls, gate = [], threading.Event()
    a = lg.acquire_scan("chat-A", "tk", _scan_that_waits(calls, gate))
    b = lg.acquire_scan("chat-B", "tk", _scan_that_waits(calls, gate))
    a.release()
    time.sleep(0.6)
    assert a.cancel_event.is_set() and not b.cancel_event.is_set()
    assert lg.cancel_scan("chat-A", "tk") is False
    gate.set()
    b.release(cancel_now=True)


# ═══ Issue 3, end to end in a real browser against the real routes: a refresh never resubmits an active query ═══════════
from tests.test_independent_chats_stop_retry import (  # noqa: E402,F401  (fixtures + helpers)
    conc, ui, env, app_env, fin, live_server, Q_A, Q_B, _tk, msg_of, bwait, click_stop, submit_from_home, composer, turns,
)


@pytest.fixture
def counted(ui, monkeypatch):
    """Counts everything a duplicate submission would touch: POST /search, collector-job enqueues, credit-store lookups."""
    f = ui
    n = {"enqueue": 0, "credit_store": 0}
    real_enqueue, real_store = f["rt"].enqueue_search_job, f["rt"]._credit_store

    def enqueue(*a, **k):
        n["enqueue"] += 1
        return real_enqueue(*a, **k)

    def store(*a, **k):
        n["credit_store"] += 1
        return real_store(*a, **k)
    monkeypatch.setattr(f["rt"], "enqueue_search_job", enqueue)
    monkeypatch.setattr(f["rt"], "_credit_store", store)
    f["n"] = n
    # The real scan persists its evidence in the per-chat topic cache (MongoDB); the stubbed scan must do the same for a
    # reconnecting stream to be able to take a finished scan's result from the database.
    import mongomock
    lg = sys.modules["logics"]
    cache = mongomock.MongoClient()["cache"].topic_evidence
    monkeypatch.setattr(lg, "topic_evidence_cache_collection", cache)
    real_scan = f["rt"].get_evidence_with_topup

    def scan_and_cache(**kw):
        posts = real_scan(**kw)
        if posts:
            lg.save_topic_evidence_cache(kw["chat_id"], kw["owner_key"], kw["topic_key"], list(posts), kw.get("keywords"))
        return posts
    monkeypatch.setattr(f["rt"], "get_evidence_with_topup", scan_and_cache)
    return f


def _start(f, hold_scan=False, hold_analyze=False):
    ctl = f["ctl"]
    ctl.hold(Q_A, scan=hold_scan, analyze=hold_analyze)
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1")
    started = ctl.scan_started if hold_scan else ctl.analyze_started
    assert bwait(page, lambda: _tk(Q_A) in started, 25)
    chat = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")
    return page, chat


def _searches(page):
    return [p for p in page._posts if p[0] == "/search"]


def _snapshot(f, chat):
    ctl, n = f["ctl"], f["n"]
    return {"msgs": len(f["chats"].find_one({"chat_id": chat})["messages"]), "scan": len(ctl.scan_started),
            "analyze": len(ctl.analyze_started), "enqueue": n["enqueue"], "credit": n["credit_store"]}


def test_refresh_during_the_scan_keeps_the_same_run_and_resubmits_nothing(counted):
    f, ctl = counted, counted["ctl"]
    page, chat = _start(f, hold_scan=True)
    before = _snapshot(f, chat)
    assert before == {"msgs": 1, "scan": 1, "analyze": 0, "enqueue": before["enqueue"], "credit": before["credit"]}
    for _ in range(3):                                                         # several refreshes while it runs
        page.reload()
        page.wait_for_function("document.getElementById('submit-btn').classList.contains('is-stop')", timeout=15000)
    assert _snapshot(f, chat) == before                                        # one message, ONE scan, no new job / credit lookup
    assert len(_searches(page)) == 1 and not ctl.scan_cancelled                # the original run was never interrupted
    assert msg_of(f, chat).get("cancelled_at") is None
    ctl.release(Q_A)                                                           # the original run finishes and the page shows it
    page.wait_for_function("document.querySelectorAll('.answer-wrap').length === 1", timeout=25000)
    assert _snapshot(f, chat) == {**before, "analyze": 1}
    assert msg_of(f, chat)["claude_answer"] and page._errors == []


def test_refresh_during_the_model_call_waits_for_that_call_and_never_starts_a_second(counted):
    f, ctl = counted, counted["ctl"]
    page, chat = _start(f, hold_analyze=True)
    before = _snapshot(f, chat)
    page.reload()
    page.wait_for_timeout(2500)
    assert _snapshot(f, chat) == before and len(_searches(page)) == 1          # no second model call, scan or job
    ctl.release(Q_A)
    page.wait_for_function("document.querySelectorAll('.answer-wrap').length === 1", timeout=25000)
    assert _snapshot(f, chat) == before and msg_of(f, chat)["claude_answer"]


def test_refresh_after_completion_shows_the_answer_and_runs_nothing(counted):
    f, ctl = counted, counted["ctl"]
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    page.wait_for_function("document.querySelectorAll('.answer-wrap').length === 1", timeout=25000)
    chat = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")
    before = _snapshot(f, chat)
    for _ in range(2):
        page.reload()
        page.wait_for_function("document.querySelectorAll('.answer-wrap').length === 1", timeout=15000)
    page.wait_for_timeout(1500)
    assert _snapshot(f, chat) == before and len(_searches(page)) == 1
    assert turns(page)[0]["answered"] and page._errors == []


def test_refresh_after_stop_stays_stopped_and_runs_nothing(counted):
    f, ctl = counted, counted["ctl"]
    page, chat = _start(f, hold_scan=True)
    click_stop(page)
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    assert bwait(page, lambda: msg_of(f, chat).get("cancelled_at") is not None, 15)
    before = _snapshot(f, chat)
    page.reload()
    page.wait_for_function("document.querySelector('.turn-stopped-note') !== null", timeout=15000)
    page.wait_for_timeout(1500)
    assert _snapshot(f, chat) == before and len(_searches(page)) == 1
    assert turns(page)[0]["chip"] == "Stopped" and msg_of(f, chat)["claude_answer"] is None


def test_refresh_after_a_genuine_failure_shows_the_failure_and_does_not_silently_run_it_again(counted, monkeypatch):
    f, ctl = counted, counted["ctl"]
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("model down")
    working_model = f["rt"].analyze_with_claude
    monkeypatch.setattr(f["rt"], "analyze_with_claude", boom)
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    assert bwait(page, lambda: len(calls) >= 1, 25)
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Failed'", timeout=15000)
    chat = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")
    assert bwait(page, lambda: msg_of(f, chat).get("failed_at") is not None, 10)    # the failure is recorded
    before = _snapshot(f, chat)
    for _ in range(2):
        page.reload()
        page.wait_for_function("document.querySelector('.turn-failed-note') !== null", timeout=15000)
    page.wait_for_timeout(1500)
    assert turns(page)[0]["chip"] == "Failed" and not turns(page)[0]["answered"]    # the real state, not a quietly produced answer
    assert _snapshot(f, chat) == before and len(calls) == 1 and len(_searches(page)) == 1
    assert msg_of(f, chat)["claude_answer"] is None
    # an EXPLICIT retry is a new run under the normal rules
    monkeypatch.setattr(f["rt"], "analyze_with_claude", working_model)       # the model works again
    page.fill("#search-input", Q_A + " for startups")
    page.press("#search-input", "Enter")
    page.wait_for_function("document.querySelectorAll('.answer-wrap').length === 1", timeout=25000)
    assert len(f["chats"].find_one({"chat_id": chat})["messages"]) == 2 and len(_searches(page)) == 2


def test_the_failure_marker_is_atomic_and_never_overrides_an_answer_or_a_stop(conc):
    f, idx = conc, conc["idx"]
    chat = chat_of_(f, Q_A)
    assert idx.mark_turn_failed(chat, OWNER_, _tk(Q_A), msg_of(f, chat)["request_id"]) is True
    assert idx.mark_turn_failed(chat, OWNER_, _tk(Q_A), msg_of(f, chat)["request_id"]) is False       # once
    import turn_control
    assert turn_control.is_closed(msg_of(f, chat)) is True
    idx.save_claude_answer_to_chat(chat, OWNER_, _tk(Q_A), "an answer")        # another runner finished it after all
    assert turn_control.is_closed(msg_of(f, chat)) is False                    # an answer always wins
    chat2 = chat_of_(f, Q_B)
    assert idx.mark_turn_cancelled(chat2, OWNER_, topic_key=_tk(Q_B)) == idx.CANCEL_CANCELLED
    assert idx.mark_turn_failed(chat2, OWNER_, _tk(Q_B), msg_of(f, chat2)["request_id"]) is False    # a stop is not a failure
    chat3 = chat_of_(f, "Find people asking for a time tracking tool")
    idx.save_claude_answer_to_chat(chat3, OWNER_, _tk("Find people asking for a time tracking tool"), "done")
    assert idx.mark_turn_failed(chat3, OWNER_, _tk("Find people asking for a time tracking tool"), msg_of(f, chat3)["request_id"]) is False


def chat_of_(f, q):
    from tests.test_independent_chats_stop_retry import send, chat_of
    return chat_of(send(f, q))


from tests.test_independent_chats_stop_retry import OWNER as OWNER_  # noqa: E402
