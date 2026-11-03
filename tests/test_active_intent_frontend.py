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
    assert page.inner_text("section.live-links .live-links-title") == "Previously saved Reddit results — date-checked, content not assessed"
    links = page.eval_on_selector_all("section.live-links li a", "els => els.map(e => [e.href, e.textContent, e.rel, e.target])")
    assert links == [["https://www.reddit.com/r/a_b/comments/abc123/x/", "<img src=x onerror=window.__pwn=1> Which CRM?", "noopener noreferrer nofollow ugc", "_blank"],
                     ["https://www.reddit.com/r/c_d/comments/def456/y/", "https://www.reddit.com/r/c_d/comments/def456/y/", "noopener noreferrer nofollow ugc", "_blank"]]
    tags = page.eval_on_selector_all("section.live-links li", "els => els.map(e => e.textContent.includes('saved Reddit result'))")
    assert tags == [True, True]
    assert "posted 2026-11-01 10:05 UTC" in page.inner_text("section.live-links li:first-child")
    assert "posted" not in page.inner_text("section.live-links li:nth-child(2)")      # no verified date -> none invented
    assert "not current results" in page.inner_text("section.live-links")        # the one accurate note replaces the stored "live" wording
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
    assert page.inner_text("section.live-links .live-links-title") == "Previously saved Reddit discussions — date-checked, content not assessed"
    tag = page.inner_text("section.live-links li")
    assert "saved Reddit discussion" in tag and "buyer" not in tag.lower() and "lead" not in tag.lower()
    page.close()
    page, errors = _page(integrity_browser, template, GOOD)
    assert page.inner_text("section.live-links .live-links-title") == "Previously saved Reddit results — date-checked, content not assessed"
    assert "saved Reddit result" in page.inner_text("section.live-links li")
    assert errors == []
    page.close()


ANALYSED = """{live_candidates: {intent: 'demand', statement: 'long statement',
  report: {main_finding: 'Teams want support automation.', themes: [{theme: 'Support automation', verified_post_count: 2, why_it_matters: 'Repetitive tickets'}, {theme: 'B', verified_post_count: 1}, {theme: 'C', verified_post_count: 1}, {theme: 'D is cut', verified_post_count: 1}],
           implications: ['one', 'two', 'three', 'four is cut'], limitations: ['Web search finds some posts, not all of them.']},
  items: [{rank: 1, title: 'Looking for an AI agent builder', url: 'https://www.reddit.com/r/SaaS/comments/abc123/x/', subreddit: 'SaaS',
           date_verified: true, published_at: '2026-11-01T10:05:00+00:00', analysis: {summary: 'Store needs an AI agent.', sentiment: 'neutral'}},
          {rank: 2, title: '<img src=x onerror=window.__pwn=1>', url: 'https://www.reddit.com/r/a_b/comments/def456/y/', analysis: null}]}}"""


@pytest.mark.parametrize("template", TEMPLATES)
def test_an_analysed_block_shows_the_concise_report_and_each_posts_own_summary_as_text(integrity_browser, template):
    page, errors = _page(integrity_browser, template, ANALYSED)
    assert page.evaluate("window.__pwn") is None and page.query_selector("section.live-links img") is None
    assert "summaries by GPT" in page.inner_text("section.live-links .live-links-title")
    assert page.inner_text("section.live-links .live-links-finding") == "Teams want support automation."
    themes = page.eval_on_selector_all("section.live-links .live-links-themes li", "els => els.map(e => e.textContent)")
    assert themes[0] == "Support automation — 2 verified posts. Repetitive tickets" and len(themes) == 3          # at most 3 themes
    first, second = page.eval_on_selector_all("section.live-links > ul:nth-of-type(2) > li", "els => els.map(e => e.textContent)")
    assert "r/SaaS" in first and "sentiment: neutral" in first and "Store needs an AI agent." in first
    assert "content not assessed" in second and "sentiment" not in second and "Store needs" not in second                # no summary bleeds over
    assert page.inner_text("section.live-links").count("four is cut") == 0 and "Limitations:" in page.inner_text("section.live-links")
    assert errors == []
    page.close()


UNAVAILABLE = """{live_candidates: {intent: 'demand', statement: 's',
  items: [{rank: 1, title: 'Real post', url: 'https://www.reddit.com/r/SaaS/comments/abc123/x/', subreddit: 'SaaS', date_verified: true,
           published_at: '2026-11-01T10:05:00+00:00', analysis: null, content_status: 'analysis_unavailable'}]}}"""


@pytest.mark.parametrize("template", TEMPLATES)
def test_a_valid_post_without_analysis_is_kept_and_clearly_marked_analysis_unavailable(integrity_browser, template):
    page, errors = _page(integrity_browser, template, UNAVAILABLE)
    li = page.inner_text("section.live-links li")
    assert "Real post" in li and "analysis unavailable" in li and "sentiment" not in li and page.query_selector(".live-links-summary") is None
    assert errors == []
    page.close()


# ── (HISTORICAL-ONLY SEARCH) old saved answers are labelled as saved; new answers show no such section ─────────────────────────
LEGACY_STORED = """{live_candidates: {source: 'live_web_search', intent: 'demand', label: 'Live Reddit results',
  statement: 'Live Reddit results: 1 Reddit post found by a separate live web search for people who appear to be looking for a solution (the 1500-minute live search window).',
  items: [{rank: 1, title: 'Looking for a CRM', url: 'https://www.reddit.com/r/SaaS/comments/abc123/x/', source: 'live_web_search', date_verified: true,
           published_at: '2026-11-01T10:05:00+00:00', subreddit: 'SaaS', analysis: {summary: 'Needs a CRM.', sentiment: 'neutral'}}]}}"""


@pytest.mark.parametrize("template", TEMPLATES)
def test_an_old_saved_live_block_is_kept_but_never_presented_as_live_or_current(integrity_browser, template):
    page, errors = _page(integrity_browser, template, LEGACY_STORED)
    box = page.inner_text("section.live-links")
    assert page.inner_text("section.live-links .live-links-title").startswith("Previously saved Reddit results")
    assert "Looking for a CRM" in box and "Needs a CRM." in box and "posted 2026-11-01 10:05 UTC" in box      # the saved content is preserved
    assert "not current results" in box and "no longer runs" in box
    assert "Live Reddit" not in box and "live Reddit" not in box and "live search window" not in box and "separate live web search" not in box
    assert page.get_attribute("section.live-links", "aria-label").startswith("Previously saved")
    assert errors == []
    page.close()


NEW_STYLE = """{format: 'source_list', executive_summary: 'Analysed the saved posts.', platforms: [], data_freshness: {statement: 'Saved posts only.'}}"""


@pytest.mark.parametrize("template", TEMPLATES)
def test_a_new_historical_only_answer_renders_no_live_or_saved_live_section(integrity_browser, template):
    page, errors = _page(integrity_browser, template, NEW_STYLE)
    assert page.query_selector("section.live-links") is None and "Live Reddit" not in page.inner_text("#c") and errors == []
    page.close()


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_renderer_source_has_no_live_heading_left(template):
    _text, fn = _function_source(template)
    assert "Live Reddit" not in fn and "live Reddit" not in lc_free(fn)


def lc_free(fn):
    return fn.replace("lc.", "")
