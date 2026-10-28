"""
Integration-checkout-only scenario tests (NOT part of p0_final.patch). They add coverage the integration review asked for
beyond the patch's own tests, without changing any of those tests:
  * slots + busy flags are released after success, cancellation AND failure (stream error, mid-flight model failure)
  * a delayed cancellation request and a failing cancellation request in the real browser
"""
import pytest

from tests.test_independent_chats_stop_retry import (  # noqa: F401  (fixtures + helpers)
    conc, ui, env, app_env, fin, live_server, Q_A, Q_B, _tk, send, chat_of, start_stream, wait, cancel, msg_of, busy_keys,
    slot_holders, bwait, click_stop, submit_from_home, composer, turns,
)


def _all_free(f):
    return busy_keys(f) == [] and slot_holders(f) == []


def test_slots_and_flags_are_released_after_success(conc):
    f = conc
    chat = chat_of(send(f, Q_A))
    s = start_stream(f, chat, Q_A)
    assert s.joined(10) and "done" in s.kinds()
    assert wait(lambda: _all_free(f), 5), (busy_keys(f), slot_holders(f))


def test_slots_and_flags_are_released_after_cancellation(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, scan=True)
    chat = chat_of(send(f, Q_A))
    s = start_stream(f, chat, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    assert cancel(f, chat, Q_A)[1]["status"] == "cancelled"
    assert s.joined(10)
    assert wait(lambda: _all_free(f), 5), (busy_keys(f), slot_holders(f))


def test_slots_and_flags_are_released_after_a_model_failure(conc, monkeypatch):
    f = conc

    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(f["rt"], "analyze_with_claude", boom)
    chat = chat_of(send(f, Q_A))
    s = start_stream(f, chat, Q_A)
    assert s.joined(10) and "error" in s.kinds()
    assert wait(lambda: _all_free(f), 5), (busy_keys(f), slot_holders(f))
    assert chat_of(send(f, Q_B))                                               # the owner can search again at once


def test_slots_and_flags_are_released_after_a_scan_failure(conc, monkeypatch):
    f = conc

    def boom(*a, **k):
        raise RuntimeError("scan down")
    monkeypatch.setattr(f["rt"], "get_evidence_with_topup", boom)
    monkeypatch.setattr(f["rt"], "RESPONSE_TIMEOUT", 2, raising=False)   # the stream polls until this timeout
    chat = chat_of(send(f, Q_A))
    s = start_stream(f, chat, Q_A)
    assert s.joined(30)
    assert wait(lambda: _all_free(f), 5), (busy_keys(f), slot_holders(f))


def test_stopped_turn_never_reappears_with_an_answer_after_the_late_model_result_and_a_reload(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, analyze=True)
    chat = chat_of(send(f, Q_A))
    s = start_stream(f, chat, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.analyze_started)
    assert cancel(f, chat, Q_A)[1]["status"] == "cancelled"
    ctl.release(Q_A)                                                           # the in-flight model call returns late
    assert s.joined(10)
    page = f["rt"].view_chat(__import__("tests.test_independent_chats_stop_retry", fromlist=["x"])._request(f, "GET", f"/chat/{chat}"),
                             chat, __import__("fastapi").BackgroundTasks()).body.decode()
    m = msg_of(f, chat)
    assert m["cancelled_at"] and m["claude_answer"] is None and m["results"] is None
    import re
    assert "Stopped" in page and not re.search(r'<[^>]*class="[^"]*answer-wrap', page)   # (the CSS rule text is not an element)


# ── browser: the cancellation request is delayed / fails ──────────────────────────────────────────────────────────────
def _running_in_browser(ui):
    f, ctl = ui, ui["ctl"]
    ctl.hold(Q_A, scan=True)
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1")
    assert bwait(page, lambda: _tk(Q_A) in ctl.scan_started, 25)
    chat = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")
    return f, ctl, page, chat


def test_browser_a_delayed_cancel_request_shows_stopped_at_once_and_the_server_stops_when_it_lands(ui):
    f, ctl, page, chat = _running_in_browser(ui)
    held = []
    page.route("**/cancel", lambda route: held.append(route))                  # the cancel request hangs in the network
    click_stop(page)
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    st = composer(page)
    assert st["value"] == Q_A and not st["stop"] and "Search failed" not in page.evaluate("document.body.innerText")
    assert bwait(page, lambda: len(held) >= 1, 10)
    assert msg_of(f, chat).get("cancelled_at") is None                         # not delivered yet
    held[0].continue_()                                                        # ... the network recovers
    assert bwait(page, lambda: msg_of(f, chat).get("cancelled_at") is not None, 15)
    assert bwait(page, lambda: _tk(Q_A) in ctl.scan_cancelled, 10)
    assert page._errors == []


def test_browser_a_failing_cancel_request_is_retried_then_reported_and_never_shows_a_false_failure(ui):
    f, ctl, page, chat = _running_in_browser(ui)
    attempts = []
    page.route("**/cancel", lambda route: (attempts.append(1), route.abort())[1])   # every attempt fails
    click_stop(page)
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    assert bwait(page, lambda: len(attempts) >= 3, 20)                         # retried
    assert bwait(page, lambda: "stopped" in page.evaluate("document.body.innerText").lower() and
                 page.locator(".search-inline-error").count() > 0, 15)
    text = page.evaluate("document.querySelector('.search-inline-error').textContent")
    assert "may still be running" in text                                      # honest: stop was NOT confirmed
    assert "Search failed" not in page.evaluate("document.body.innerText")
    st = composer(page)
    assert not st["stop"]                                                      # the composer is usable again
    assert msg_of(f, chat).get("cancelled_at") is None                         # the server never got the stop ...
    # ... but the browser dropped its stream, so the server-side work is torn down by the disconnect, not left running forever
    assert bwait(page, lambda: _tk(Q_A) in ctl.scan_cancelled, 20)


def test_KNOWN_RISK_a_search_running_longer_than_the_busy_timeout_loses_its_slot_and_flag(conc, monkeypatch):
    """Documents CURRENT behaviour (not a desired one): neither the slot nor the busy flag is refreshed while a search runs
    (they are written at stream start). After BUSY_FLAG_TIMEOUT_SECONDS (400 s) both count as stale, so a LONG search
    (chats showing "Researching 8m+" exist) no longer counts against the cap and its chat accepts a duplicate. The busy flag
    already behaved like this before the patch; the cap inherits it."""
    import datetime as dt
    f, ctl, idx = conc, conc["ctl"], conc["idx"]
    monkeypatch.setattr(f["rt"].config, "MAX_CONCURRENT_SEARCHES_PER_OWNER", 1, raising=False)
    ctl.hold(Q_A, scan=True)
    chat = chat_of(send(f, Q_A))
    start_stream(f, chat, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    assert send(f, Q_B).status_code == 200                                     # at the cap while fresh
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=idx.BUSY_FLAG_TIMEOUT_SECONDS + 5)
    f["busy"].update_many({}, {"$set": {"started_at": old}})                   # what 400 s of running looks like
    assert send(f, Q_B).status_code == 303                                     # the cap no longer sees the running search
    ctl.release(Q_A)
