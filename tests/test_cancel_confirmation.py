"""
tests/test_cancel_confirmation.py — Issue 1: "Stopped" is shown at once, but a stop the server did not confirm is never presented
as a confirmed one, and a failed stop never resubmits, refunds or restarts anything. (The original delayed / permanently failing
scenarios also live, untouched, in tests/test_integration_scenarios.py.)
"""
import pytest

from tests.test_refresh_and_leases import (  # noqa: F401  (fixtures + helpers)
    counted, ui, conc, env, app_env, fin, live_server, _start, _snapshot, _searches, Q_A, _tk, msg_of, bwait, click_stop, composer,
)


def _chip(page):
    return page.evaluate("document.querySelector('.turn-chip .turn-status-label').textContent.trim()")


def _notice(page):
    return page.evaluate("(document.querySelector('.search-inline-error') || {}).textContent || ''")


def test_a_confirmed_stop_shows_stopped_and_no_warning(counted):
    f, ctl = counted, counted["ctl"]
    page, chat = _start(f, hold_scan=True)
    click_stop(page)
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    assert bwait(page, lambda: msg_of(f, chat).get("cancelled_at") is not None, 15)
    page.wait_for_timeout(3000)                                                # longer than every retry would take
    assert _chip(page) == "Stopped" and "confirm" not in _notice(page)
    assert composer(page)["value"] == Q_A and len(_searches(page)) == 1


def test_a_delayed_stop_is_shown_at_once_and_confirmed_when_it_lands(counted):
    f, ctl = counted, counted["ctl"]
    page, chat = _start(f, hold_scan=True)
    held = []
    page.route("**/cancel", lambda route: held.append(route))
    click_stop(page)
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    assert bwait(page, lambda: len(held) == 1, 10) and msg_of(f, chat).get("cancelled_at") is None
    held[0].continue_()
    assert bwait(page, lambda: msg_of(f, chat).get("cancelled_at") is not None, 15)
    page.wait_for_timeout(1500)
    assert _chip(page) == "Stopped" and "confirm" not in _notice(page)


@pytest.mark.parametrize("failure", ["network", "http500"])
def test_a_permanently_failing_stop_is_reported_honestly_and_changes_nothing_else(counted, failure):
    f, ctl, n = counted, counted["ctl"], counted["n"]
    page, chat = _start(f, hold_scan=True)
    before = _snapshot(f, chat)
    attempts = []

    def handler(route):
        attempts.append(1)
        route.abort() if failure == "network" else route.fulfill(status=500, body="boom")
    page.route("**/cancel", handler)
    click_stop(page)
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")   # immediate feedback
    assert bwait(page, lambda: "confirm" in _notice(page), 25)
    assert len(attempts) >= 3                                                  # retried, then best-effort beacon
    assert _chip(page) == "Stop not confirmed"                                  # no longer reads as a confirmed stop
    assert "may still be running" in _notice(page) and "Reload" in _notice(page)
    assert "may still be running" in page.evaluate("document.querySelector('.turn-stopped-note').textContent")
    assert msg_of(f, chat).get("cancelled_at") is None                         # the server never confirmed
    st = composer(page)
    assert st["value"] == Q_A and not st["stop"]                               # the prompt is editable; nothing was resent
    assert len(_searches(page)) == 1                                           # no automatic resubmission ...
    assert _snapshot(f, chat)["enqueue"] == before["enqueue"] and _snapshot(f, chat)["credit"] == before["credit"]   # ... or credit use
    assert _snapshot(f, chat)["msgs"] == 1 and page._errors == []


def test_after_an_unconfirmed_stop_a_reload_shows_the_real_state_without_restarting_the_run(counted):
    f, ctl = counted, counted["ctl"]
    page, chat = _start(f, hold_scan=True)
    page.route("**/cancel", lambda route: route.abort())
    click_stop(page)
    assert bwait(page, lambda: "confirm" in _notice(page), 25)
    before = _snapshot(f, chat)
    page.unroute("**/cancel")
    page.reload()
    # the server never recorded a stop, so the page honestly shows the turn as still running - and reconnects to it
    page.wait_for_function("document.getElementById('submit-btn').classList.contains('is-stop')", timeout=15000)
    assert _snapshot(f, chat) == before and len(_searches(page)) == 1
    click_stop(page)                                                           # the user presses Stop again, the network is back
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    assert bwait(page, lambda: msg_of(f, chat).get("cancelled_at") is not None, 15)
