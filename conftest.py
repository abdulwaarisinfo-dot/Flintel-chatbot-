import os

# config.py refuses to start without a strong SESSION_SECRET_KEY; tests get a
# throwaway one unless the environment already provides a key.
os.environ.setdefault("SESSION_SECRET_KEY", "test-only-session-key-not-for-deployment")


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

