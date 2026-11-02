"""
tests/test_active_intent_frontend.py — how the answer page shows the `live_candidates` block (templates/chat.html AND
templates/index.html carry their own copy of the renderer). The real shipped function runs in the preinstalled Chromium.
Offline; nothing is fetched.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ["chat.html", "index.html"]
ORIGIN = "http://flintel.test"


def _function_source(template):
    text = (ROOT / "templates" / template).read_text(encoding="utf-8")
    m = re.search(r"  function appendLiveCandidates\(data, container\) \{.*?\n  \}\n", text, re.S)
    assert m, f"appendLiveCandidates missing from {template}"
    return text, m.group(0)


@pytest.mark.parametrize("template", TEMPLATES)
def test_both_templates_render_the_block_right_after_the_freshness_note(template):
    text, _fn = _function_source(template)
    call = re.search(r"if \(!answerEmpty\) appendFreshnessNote\(data, renderedEl\);[^\n]*\n\s*if \(!answerEmpty\) appendLiveCandidates\(data, renderedEl\);", text)
    assert call, "the block must be appended in the same place and under the same condition as the freshness note"
    assert ".live-links {" in text and ".live-links a" in text                # the section is styled in both pages


def _page(browser, template, data_js):
    _text, fn = _function_source(template)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/*", lambda r: r.fulfill(status=200, headers={"Content-Type": "text/html"}, body="<!doctype html><html><body><div id='c'></div></body></html>"))
    page.goto(ORIGIN + "/")
    page.add_script_tag(content=fn)
    page.evaluate(f"appendLiveCandidates({data_js}, document.getElementById('c'))")
    return page, errors


GOOD = """{live_candidates: {statement: 'Live web search: 2 Reddit links found by a separate live web search. Each post date was checked.',
  items: [{rank: 1, title: '<img src=x onerror=window.__pwn=1> Which CRM?', url: 'https://www.reddit.com/r/a_b/comments/abc123/x/', date_verified: true, published_at: '2026-11-01T10:05:00+00:00'},
          {rank: 2, title: null, url: 'https://www.reddit.com/r/c_d/comments/def456/y/'},
          {rank: 3, title: 'js link', url: 'javascript:window.__pwn=2'},
          {rank: 4, title: 'plain http', url: 'http://www.reddit.com/r/e_f/comments/ghi789/z/'},
          {rank: 5, title: 'no url'}]}}"""


@pytest.mark.parametrize("template", TEMPLATES)
def test_block_shows_only_https_links_as_text_with_the_unverified_label(integrity_browser, template):
    page, errors = _page(integrity_browser, template, GOOD)
    assert page.evaluate("window.__pwn") is None and page.query_selector("section.live-links img") is None
    assert page.inner_text("section.live-links .live-links-title") == "Live Reddit results — date-checked, content not assessed"
    links = page.eval_on_selector_all("section.live-links li a", "els => els.map(e => [e.href, e.textContent, e.rel, e.target])")
    assert links == [["https://www.reddit.com/r/a_b/comments/abc123/x/", "<img src=x onerror=window.__pwn=1> Which CRM?", "noopener noreferrer nofollow ugc", "_blank"],
                     ["https://www.reddit.com/r/c_d/comments/def456/y/", "https://www.reddit.com/r/c_d/comments/def456/y/", "noopener noreferrer nofollow ugc", "_blank"]]
    tags = page.eval_on_selector_all("section.live-links li", "els => els.map(e => e.textContent.includes('live Reddit result'))")
    assert tags == [True, True]
    assert "posted 2026-11-01 10:05 UTC" in page.inner_text("section.live-links li:first-child")
    assert "posted" not in page.inner_text("section.live-links li:nth-child(2)")      # no verified date -> none invented
    assert "Each post date was checked" in page.inner_text("section.live-links")
    assert errors == []
    page.close()


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("data_js", ["null", "{}", "{live_candidates: null}", "{live_candidates: 'x'}", "{live_candidates: {items: []}}",
                                     "{live_candidates: {items: 'x'}}", "{live_candidates: {items: [{url: 'http://x'}, {title: 't'}, null]}}"])
def test_nothing_is_rendered_without_valid_candidates(integrity_browser, template, data_js):
    page, errors = _page(integrity_browser, template, data_js)
    assert page.query_selector("section.live-links") is None and errors == []
    page.close()


DISC = """{live_candidates: {intent: 'discussion', label: 'Live Reddit discussions', statement: 'Live Reddit discussions: 1 post.',
  items: [{rank: 1, title: 'Anyone using AI agents for support?', url: 'https://www.reddit.com/r/a_b/comments/abc123/x/', intent: 'discussion',
           date_verified: true, published_at: '2026-11-01T10:05:00+00:00'}]}}"""


@pytest.mark.parametrize("template", TEMPLATES)
def test_a_discussion_block_is_labelled_as_discussions_and_a_demand_block_keeps_its_label(integrity_browser, template):
    page, errors = _page(integrity_browser, template, DISC)
    assert page.inner_text("section.live-links .live-links-title") == "Live Reddit discussions — date-checked, content not assessed"
    tag = page.inner_text("section.live-links li")
    assert "live Reddit discussion" in tag and "buyer" not in tag.lower() and "lead" not in tag.lower()
    page.close()
    page, errors = _page(integrity_browser, template, GOOD)
    assert page.inner_text("section.live-links .live-links-title") == "Live Reddit results — date-checked, content not assessed"
    assert "live Reddit result" in page.inner_text("section.live-links li")
    assert errors == []
    page.close()
