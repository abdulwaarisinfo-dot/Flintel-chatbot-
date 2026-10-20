import os

# config.py refuses to start without a strong SESSION_SECRET_KEY; tests get a
# throwaway one unless the environment already provides a key.
os.environ.setdefault("SESSION_SECRET_KEY", "test-only-session-key-not-for-deployment")
# Search credits are enforced by default in the app. The pre-existing route
# tests stub `database` with MagicMock and exercise the pre-credit /search
# behaviour, so the suite runs with the kill switch on unless the environment
# says otherwise; tests/test_search_credit*.py switch enforcement on explicitly.
os.environ.setdefault("CREDIT_ENFORCEMENT_DISABLED", "true")
# The guest search gate (guest_gate.py) is ON by default in the app (one genuine search per visitor). The
# pre-existing route tests drive /search as a guest with stubbed storage and expect the old behaviour, so the
# suite runs with the gate off unless a test switches it on (tests/test_guest_gate.py does, explicitly).
os.environ.setdefault("GUEST_SEARCH_LIMIT", "0")


import pytest  # noqa: E402


@pytest.fixture(scope="session")
def integrity_browser():
    """One Chromium for the answer-integrity render tests, started once per session.
    Playwright refuses to START inside a running event loop, which other modules'
    browser fixtures can leave behind; an instance started earlier keeps working.
    Falls back to the preinstalled binary when the Playwright package expects
    another browser build."""
    sp = pytest.importorskip("playwright.sync_api")
    try:
        p = sp.sync_playwright().start()
    except Exception as exc:
        pytest.skip(f"playwright unavailable: {exc}")
    b = None
    for kwargs in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try:
            b = p.chromium.launch(**kwargs)
            break
        except Exception:
            continue
    if b is None:
        p.stop()
        pytest.skip("chromium unavailable")
    yield b
    b.close()
    p.stop()

