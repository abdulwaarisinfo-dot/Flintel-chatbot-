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



@pytest.fixture(autouse=True)
def _no_scan_state_leaks_between_tests():
    """A scan whose last waiter left now lives on for SCAN_DISCONNECT_GRACE_SECONDS (a page refresh re-attaches to it), and a finished
    scan / unsaved answer is kept for a short time. Tests reuse the same chat/topic keys, so none of that may outlive its test."""
    import sys

    def reset():
        lg = sys.modules.get("logics")
        if lg is not None and hasattr(lg, "_SCAN_REGISTRY"):
            with lg._SCAN_REGISTRY_LOCK:
                for entry in list(lg._SCAN_REGISTRY.values()):
                    try:
                        entry["cancel_event"].set()
                        t = entry.pop("grace_timer", None)
                        if t is not None:
                            t.cancel()
                    except Exception:
                        pass
                lg._SCAN_REGISTRY.clear()
    reset()
    yield
    reset()


# HISTORICAL-ONLY SEARCH: Live Web Search is switched off at source level (reddit_live.LIVE_SEARCH_EXECUTION_ENABLED = False).
# These modules test the LEGACY Live implementation in isolation, so they re-enable the switch for their own duration only.
# Every other test module (and the production default) runs with it off. tests/test_historical_only.py asserts the off state.
_LEGACY_LIVE_MODULES = ("test_reddit_live", "test_reddit_live_claim_race", "test_reddit_live_endpoint", "test_live_content_analysis",
                        "test_live_search_intents", "test_active_intent_workflow", "test_active_intent_frontend",
                        "test_active_intent", "test_active_intent_kinds", "test_active_intent_window", "test_intent_classifier",
                        "test_corrective_patch", "test_activation_resume", "test_range_poll_cap")


@pytest.fixture(autouse=True)
def _legacy_live_switch(request):
    if request.module.__name__.split(".")[-1] not in _LEGACY_LIVE_MODULES:
        yield
        return
    import reddit_live
    old = reddit_live.LIVE_SEARCH_EXECUTION_ENABLED
    reddit_live.LIVE_SEARCH_EXECUTION_ENABLED = True
    try:
        yield
    finally:
        reddit_live.LIVE_SEARCH_EXECUTION_ENABLED = old
