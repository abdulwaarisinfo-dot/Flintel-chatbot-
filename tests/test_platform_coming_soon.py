"""
tests/test_platform_coming_soon.py - only supported platforms (today: Reddit) are selectable / searchable.
UI: the three "Coming soon" options are aria-disabled in both templates, and the selector JS (run in real Chromium when available)
ignores clicks/keys on them, makes All Platforms = Reddit only, and never shows a disabled platform in the summary.
Server: logics.supported_platforms / the /search request path drop unsupported or unknown values (crafted or stale requests).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ("templates/index.html", "templates/chat.html")


from tests.test_platform_multiselect import lg  # noqa: E402,F401  (stubbed-module logics fixture)


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_unsupported_options_are_marked_disabled_and_keep_their_badge(tpl):
    html = (ROOT / tpl).read_text(encoding="utf-8")
    for name in ("X / Twitter", "LinkedIn", "Facebook"):
        li = re.search(r'<li [^>]*data-value="%s"[^>]*>.*?</li>' % re.escape(name), html, re.S).group(0)
        assert 'aria-disabled="true"' in li and 'tabindex="-1"' in li and 'aria-selected="false"' in li
        assert "platform-opt-soon" in li and "Coming soon" in li
    reddit = re.search(r'<li [^>]*data-value="Reddit"[^>]*>', html).group(0)
    assert "aria-disabled" not in reddit and 'tabindex="0"' in reddit
    assert re.search(r'\.platform-opt\[aria-disabled="true"\]\s*\{\s*cursor: not-allowed', html)


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_selector_script_ignores_disabled_options(tpl):
    html = (ROOT / tpl).read_text(encoding="utf-8")
    assert "li.getAttribute('aria-disabled') !== 'true'" in html.replace('"', "'")          # platformOptions filter + keyboard
    assert "if (li.getAttribute('aria-disabled') === 'true') return;" in html               # toggle ignores them
    assert "platformOptions(menu).map((o) => o.dataset.value)" in html                      # All = every SUPPORTED platform


@pytest.fixture
def page(integrity_browser):
    pg = integrity_browser.new_page()
    yield pg
    pg.close()


@pytest.mark.parametrize("tpl", TEMPLATES)
def test_selector_behaviour_in_a_real_browser(page, tpl):
    html = (ROOT / tpl).read_text(encoding="utf-8")
    start = html.index("  const PLATFORM_ALL = 'All Platforms';")
    end = html.index("  // PLATFORM-MULTI-HELPERS-END")
    helpers = html[start:end]
    items = "".join(
        '<li role="option" tabindex="%s" data-value="%s" aria-selected="false"%s><span class="platform-opt-logo"></span></li>'
        % ("-1" if dis else "0", name, ' aria-disabled="true"' if dis else "")
        for name, dis in (("All Platforms", False), ("X / Twitter", True), ("Reddit", False), ("LinkedIn", True), ("Facebook", True)))
    page.set_content('<ul id="platforms-menu">%s</ul><span id="platforms-label"></span><input id="platform-input" value="All Platforms">' % items)
    page.evaluate("() => {%s; window.t = togglePlatformOption; window.s = syncPlatformSelectionFromInput; window.M = document.getElementById('platforms-menu'); window.L = document.getElementById('platforms-label'); window.I = document.getElementById('platform-input'); }" % helpers)
    state = lambda: page.evaluate("() => ({input: I.value, label: L.textContent, sel: Array.from(M.querySelectorAll('li')).filter(l => l.getAttribute('aria-selected')==='true').map(l => l.dataset.value)})")  # noqa: E731
    page.evaluate("() => s()")                                                              # initial: All = Reddit only
    assert state() == {"input": "Reddit", "label": "Reddit", "sel": ["All Platforms", "Reddit"]}
    for name in ("X / Twitter", "LinkedIn", "Facebook"):                                    # clicks on disabled options do nothing
        page.evaluate("(n) => t(M, L, I, M.querySelector('li[data-value=\"' + n + '\"]'))", name)
        assert state()["sel"] == ["All Platforms", "Reddit"] and "Twitter" not in state()["label"]
    page.evaluate("() => t(M, L, I, M.querySelector('li[data-value=\"Reddit\"]'))")         # Reddit deselects normally
    assert state() == {"input": "All Platforms", "label": "All Platforms", "sel": []}
    page.evaluate("() => t(M, L, I, M.querySelector('li[data-value=\"Reddit\"]'))")         # and selects again
    assert state()["input"] == "Reddit"
    page.evaluate("() => t(M, L, I, M.querySelector('li[data-value=\"Reddit\"]')); t(M, L, I, M.querySelector('li[data-value=\"All Platforms\"]'))")
    assert state() == {"input": "Reddit", "label": "Reddit", "sel": ["All Platforms", "Reddit"]}   # All Platforms -> Reddit only
    page.evaluate("() => { I.value = 'X / Twitter, LinkedIn'; s(); }")                      # stale saved state -> supported only
    assert state()["input"] == "Reddit" and "LinkedIn" not in state()["label"]
    page.evaluate("() => { M.querySelector('li[data-value=\"Facebook\"]').click(); }")      # raw click: no handler selects it either
    assert "Facebook" not in state()["sel"]


@pytest.mark.parametrize("raw,expected", [
    ("All Platforms", "reddit"), ("", "reddit"), (None, "reddit"), ("Reddit", "reddit"), ("X / Twitter", "reddit"),
    ("LinkedIn, Facebook", "reddit"), ("X / Twitter, Reddit, LinkedIn", "reddit"), ("nonsense", "reddit"),
    ("reddit,x_twitter", "reddit"), ("Reddit; DROP", "reddit"), ("all", "reddit"), ("x_twitter,linkedin,facebook", "reddit"),
])
def test_server_only_ever_returns_supported_platforms(lg, raw, expected):
    assert lg.supported_platforms(raw) == expected


def test_existing_normalizer_is_untouched_and_the_search_route_uses_the_supported_filter(lg):
    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    assert "targeting_platform = supported_platforms(platform)" in src
    assert "platform_display_value(platform)" not in src                       # the saved/display value is the filtered one
    assert lg.normalize_platforms("X / Twitter, LinkedIn") == "x_twitter,linkedin"        # pre-existing contract unchanged
