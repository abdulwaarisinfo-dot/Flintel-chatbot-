"""
tests/test_matched_results_encoding.py — the stored matched results must reach
the page script exactly as saved.

Root cause: both templates wrote
    data-matched-results="{{ (msg.results or []) | tojson | e }}"
Jinja's tojson returns already-"safe" Markup, so `| e` did nothing and the
JSON's own double quotes ended the attribute at `[{`. The script then got
"[{" (JSON.parse failed -> []), and the rest of the JSON became stray HTML
attributes — post text such as `x onmouseover=...` could become a real
attribute. Fix: `| tojson | forceescape` (" -> &#34;; tojson already turns
< > & ' into \\u escapes), which the browser decodes back to the exact JSON.

The page HTML comes from the REAL view_chat route; Chromium parses it.
No real LLM / Mongo / network.
"""
import copy
import json
import sys

import pytest

from tests.test_answer_persistence import ORIGIN, env  # noqa: F401  (fixture)

try:
    import jinja2
    from playwright.sync_api import sync_playwright
    HAVE_BROWSER = True
except Exception:          # pragma: no cover
    HAVE_BROWSER = False

TRICKY = [
    {"title": 'He said "hi" & left', "post_text": "It's <b>bold</b> & 'quoted' — naïve café 😀 日本語",
     "post_url": "https://www.reddit.com/r/crm/comments/a1/?q=1&x=\"2\"", "platform": "reddit",
     "subreddit": "crm", "created_utc": 1760000000, "id": "t3_a1"},
    {"title": "x onmouseover=window.__pwned=1 y", "post_text": '"><img src=x onerror="window.__pwned=2">',
     "post_url": "https://www.reddit.com/r/crm/comments/a2/", "platform": "reddit", "id": "t3_a2"},
    {"title": "</div><script>window.__pwned=3</script>", "post_text": "\\u0022 backslash \\ and \n newline\ttab",
     "post_url": "https://www.reddit.com/r/crm/comments/a3/", "platform": "reddit"},
    {"title": "missing fields only"},
]
NO_RESULTS_ANSWER = json.dumps({"format": "no_results", "headline": "No strong matches",
                                "executive_summary": "Nothing matched exactly."})


def _chat(results):
    return {"chat_id": "c1", "owner_key": "o1", "owner_type": "guest", "title": "Encoding", "summary": "",
            "messages": [{"query": "find crm buyers", "topic_key": "find crm buyers", "keywords": ["crm"],
                          "targeting_platform": "reddit", "message_type": "search", "results": copy.deepcopy(results),
                          "claude_answer": NO_RESULTS_ANSWER}]}


def _view_chat_html(env, monkeypatch, results):
    from fastapi import BackgroundTasks
    from starlette.requests import Request
    lg, idx, chats = env
    rt = sys.modules["routes"] if "routes" in sys.modules else __import__("routes")
    chats.docs[("c1", "o1")] = _chat(results)
    monkeypatch.setattr(rt, "get_owner", lambda request: ("o1", "guest"))
    monkeypatch.setattr(rt, "get_user_chat_list", lambda owner_key: [{"chat_id": "c1", "title": "Encoding"}])
    monkeypatch.setattr(rt, "get_current_user", lambda request: None)
    request = Request({"type": "http", "method": "GET", "path": "/chat/c1", "headers": [], "query_string": b"",
                       "session": {}, "app": idx.app})
    resp = rt.view_chat(request, "c1", BackgroundTasks())
    return resp.body.decode("utf-8"), chats


def _attr_value(html):
    """The raw attribute exactly as served (before any browser decoding)."""
    i = html.index('data-matched-results="') + len('data-matched-results="')
    return html[i:html.index('"', i)]


@pytest.mark.parametrize("results", [TRICKY, [], [{"title": "only a title"}]])
def test_served_attribute_is_one_well_formed_value(env, monkeypatch, results):
    import html as htmllib
    html, _ = _view_chat_html(env, monkeypatch, results)
    raw = _attr_value(html)
    for ch in ('"', "<", ">", "'"):
        assert ch not in raw                                         # nothing can end the attribute / open a tag
    assert json.loads(htmllib.unescape(raw)) == (results or [])      # decodes to the exact stored data
    assert html.count("<script>window.__pwned") == 0


def test_stored_results_are_not_modified_by_rendering(env, monkeypatch):
    html, chats = _view_chat_html(env, monkeypatch, TRICKY)
    assert chats.docs[("c1", "o1")]["messages"][0]["results"] == TRICKY


def test_templates_use_the_forced_escape():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "templates"
    for name in ("chat.html", "index.html"):
        s = (root / name).read_text()
        assert 'data-matched-results="{{ (msg.results or []) | tojson | forceescape }}"' in s
        assert "| tojson | e }}" not in s


@pytest.fixture(scope="module")
def browser():
    if not HAVE_BROWSER:
        pytest.skip("playwright/jinja2 not installed")
    try:
        p = sync_playwright().start()
        b = p.chromium.launch()
    except Exception as exc:   # pragma: no cover
        pytest.skip(f"chromium unavailable: {exc}")
    yield b
    b.close()
    p.stop()


def _open(browser, html):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def handle(route):
        if route.request.url.startswith(ORIGIN + "/chat/c1"):
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=html)
        return route.fulfill(status=200, body="")
    page.route("**/*", handle)
    page.goto(ORIGIN + "/chat/c1")
    page.wait_for_timeout(300)
    return page, errors


@pytest.mark.parametrize("results", [TRICKY, []])
def test_browser_reconstructs_the_exact_stored_results(browser, env, monkeypatch, results):
    html, _ = _view_chat_html(env, monkeypatch, results)
    page, errors = _open(browser, html)
    try:
        got = page.evaluate("JSON.parse(document.querySelector('.answer-wrap').dataset.matchedResults)")
        assert got == results
        attrs = page.evaluate("[...document.querySelector('.answer-wrap').attributes].map(a => a.name).sort()")
        assert set(attrs) <= {"class", "data-matched-results", "data-last-rendered-raw"}   # no injected attributes
        assert page.evaluate("window.__pwned") is None                   # no injected handler / script ran
        page.mouse.move(200, 200)
        page.evaluate("document.querySelectorAll('*').forEach(e => e.dispatchEvent(new MouseEvent('mouseover')))")
        assert page.evaluate("window.__pwned") is None
        assert errors == []
    finally:
        page.close()


def test_browser_shows_closest_matches_as_text_not_markup(browser, env, monkeypatch):
    html, _ = _view_chat_html(env, monkeypatch, TRICKY)
    page, errors = _open(browser, html)
    try:
        page.wait_for_function("document.body.innerText.includes('Closest matches found')", timeout=5000)
        text = page.inner_text("main")
        assert 'He said "hi" & left' in text and "</div><script>window.__pwned=3</script>" in text
        assert page.evaluate("document.querySelectorAll('.answer-wrap img, .answer-wrap script').length") == 0
        assert page.evaluate("window.__pwned") is None and errors == []
    finally:
        page.close()


def test_browser_malformed_attribute_falls_back_without_errors(browser, env, monkeypatch):
    html, _ = _view_chat_html(env, monkeypatch, TRICKY)
    html = html.replace(_attr_value(html), "[{broken", 1)
    page, errors = _open(browser, html)
    try:
        page.wait_for_timeout(300)
        assert "Closest matches found" not in page.inner_text("main")    # nothing invented
        assert "didn't surface enough relevant evidence" in page.inner_text("main")   # the answer still renders
        assert errors == []
    finally:
        page.close()
