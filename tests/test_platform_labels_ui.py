"""The shipped normalizePlatform / platformDisplayName (both templates) in a real browser: Hacker News is Hacker News (not X, not "Web"),
nothing is matched by a bare letter, and an unidentifiable platform is shown as unidentified rather than invented."""
import json
from pathlib import Path

import pytest

from tests.test_ui_answer_blocks import _block

ROOT = Path(__file__).resolve().parent.parent
CASES = [("Hacker News", "hackernews", "Hacker News"), ("hackernews", "hackernews", "Hacker News"), ("HN", "hackernews", "Hacker News"),
         ("reddit", "reddit", "Reddit"), ("Reddit", "reddit", "Reddit"), ("x", "x", "X (Twitter)"), ("X (Twitter)", "x", "X (Twitter)"),
         ("twitter", "x", "X (Twitter)"), ("x_twitter", "x", "X (Twitter)"), ("X / Twitter", "x", "X (Twitter)"),
         ("linkedin", "linkedin", "LinkedIn"), ("facebook", "facebook", "Facebook"),
         ("Xerox forum", "other", "Xerox forum"), ("Mixcloud", "other", "Mixcloud"),          # the letter x alone never means X / Twitter
         ("unknown", "other", "Source not identified"), ("", "other", "Source not identified"), (None, "other", "Source not identified")]


@pytest.mark.parametrize("template", ["chat.html", "index.html"])
def test_platform_names_are_matched_exactly_and_never_invented(integrity_browser, template):
    src = (ROOT / "templates" / template).read_text()
    js = _block(src, "function normalizePlatform(", "n") + "\n" + _block(src, "function platformDisplayName(", "d")
    pg = integrity_browser.new_page()
    try:
        pg.set_content(f"<script>{js}</script>")
        for raw, key, name in CASES:
            got = pg.evaluate("(r)=>{const k=normalizePlatform(r);return [k,platformDisplayName(k,r)]}", raw)
            assert got == [key, name], (raw, got)
    finally:
        pg.close()


@pytest.mark.parametrize("template", ["chat.html", "index.html"])
def test_hacker_news_has_its_own_icon_field_labels_and_style(template):
    src = (ROOT / "templates" / template).read_text()
    assert "hackernews: '<svg" in src and "hackernews: { sourceLabel" in src and ".platform-icon-badge.hackernews" in src
    assert "s.includes('x')" not in src                                                      # the substring test that mislabelled platforms
