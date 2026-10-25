"""
tests/test_keyword_chips.py — suggested searches as separate blue keyword chips,
and one blue for inline hyperlinks (both templates, frontend only).

  * node: suggestedSearchTerms() extracted from each template;
  * static: chip/link CSS and that green non-link accents are untouched;
  * real Chromium (Playwright) when available: the Jinja-rendered template with
    a saved "no_results" answer containing the Notion example.
"""
import datetime
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ("chat.html", "index.html")
NODE = shutil.which("node")
NOTION = "look for 'Notion help', 'Notion troubleshooting', or 'Notion issues' threads"


def _src(name):
    return (ROOT / "templates" / name).read_text(encoding="utf-8")


def _terms_js(name):
    s = _src(name)
    a = s.index("  const QUOTED_TERM_RE")
    b = s.index("    return out;\n  }", a) + len("    return out;\n  }")
    return s[a:b]


# ── static ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", TEMPLATES)
def test_link_and_chip_styles(name):
    s = _src(name)
    assert "--link-color: #009dff;" in s
    assert ".answer-rendered a { color: var(--link-color); text-decoration: underline; }" in s
    assert ".post-field .field-text a { color: var(--link-color);" in s
    assert ".leadgen-field .text a { color: var(--link-color);" in s
    term = s[s.index("function clickableTerm(text) {"):]
    term = term[:term.index("\n  }\n") + 4]
    assert "#a3e635" not in term and "keyword-chip" in term and "button" in term
    assert "form.submit" not in term and "requestSubmit" not in term and ".submit()" not in term


@pytest.mark.parametrize("name", TEMPLATES)
def test_green_non_link_accents_untouched(name):
    s = _src(name)
    # The brand accent moved from lime (#a3e635) to electric blue (#2563EB); the non-link accents keep their rules with the
    # new colour (white text on the blue button for contrast), and no lime is left in the template.
    assert "#a3e635" not in s.lower()
    for rule in (".btn-outline:hover { border-color: #2563EB; color: #2563EB; }",
                 ".auth-switch button { color: #2563EB; font-weight: 600; }",
                 ".add-btn:hover { color: #2563EB; }",
                 "background: #2563EB; color: #fff; border-radius: 9px; padding: 10px 18px;"):
        assert rule in s, rule


def test_helpers_identical_in_both_templates():
    assert _terms_js("chat.html") == _terms_js("index.html")


# ── node ─────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("name", TEMPLATES)
def test_suggested_search_terms(name):
    cases = [
        {"suggestion": NOTION},
        {"suggestion": "look for “Notion help”, “Notion troubleshooting”, or “Notion issues” threads"},
        {"suggestion": 'Search "Notion help" or "Notion issues"'},
        {"suggestion": "Hyderabad"},
        {"suggestion": "Notion's API limits"},
        {"suggestion": "users' complaints about Notion"},
        {"suggestions": ["Notion help", "Notion troubleshooting", "notion help", "  ", None, 42]},
        {"suggestions": ["try 'Notion help'", "Notion issues"]},
        {"suggestions": "not-an-array", "suggestion": "Lahore"},
        {"suggestion": None}, {}, None,
    ]
    code = _terms_js(name) + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(suggestedSearchTerms)));"
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [
        ["Notion help", "Notion troubleshooting", "Notion issues"],
        ["Notion help", "Notion troubleshooting", "Notion issues"],
        ["Notion help", "Notion issues"],
        ["Hyderabad"],
        ["Notion's API limits"],
        ["users' complaints about Notion"],
        ["Notion help", "Notion troubleshooting"],
        ["Notion help", "Notion issues"],
        ["Lahore"],
        [], [], [],
    ]


# ── real browser ─────────────────────────────────────────────────────────────
try:
    import jinja2
    from playwright.sync_api import sync_playwright
    HAVE_BROWSER = True
except Exception:          # pragma: no cover
    HAVE_BROWSER = False

ORIGIN = "http://flintel.test"
NO_RESULTS = {
    "format": "no_results", "message": "Nothing relevant turned up for Notion bugs.",
    "likely_reason": "Small sample this week.",
    "suggested_actions": [
        {"type": "broaden_time", "label": "Extend to last 30 days"},
        {"type": "broaden_term", "label": "Search a broader term", "suggestions": ["Notion bugs", "Notion slow"]},
        {"type": "try_nearest_alternative", "label": "Try related searches", "suggestion": NOTION},
    ],
    "related_questions": ["What do people dislike about Notion?"],
}
SOURCE_LIST = {
    "format": "source_list", "headline": "H", "executive_summary": "Summary.",
    "platforms": [{"platform": "reddit", "total_analyzed": 1, "shown_count": 1, "posts": [
        {"index": 1, "source": "notion", "title": "Post title", "summary": "s", "sentiment": "neutral",
         "link": "https://www.reddit.com/r/notion/comments/p1/"}]}],
}
NOW = datetime.datetime(2026, 10, 13, 10, 0, 0)
VISIBLE_INPUT = "document.getElementById('search-input')"


def _render(name, answers, chat_id="c1"):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")),
                             undefined=jinja2.ChainableUndefined)
    env.globals["url_for"] = lambda *a, **k: "#"
    msgs = [{"query": f"q{i}", "topic_key": f"t{i}", "targeting_platform": "all", "requested_at": NOW,
             "results": [], "claude_answer": json.dumps(a)} for i, a in enumerate(answers)]
    chat = {"chat_id": chat_id, "title": "t", "messages": msgs} if answers else None
    return env.get_template(name).render(request=None, chat=chat, chats=[], user=None, chat_id=chat_id,
                                         pending_stream_topic_key=None, messages=msgs)


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


def _open(browser, name):
    """chat.html: the chat page itself. index.html: the real path — the home page,
    then a soft navigation to the chat (index.html's own JS renders the answer)."""
    pages = {"/chat/c1": _render("chat.html", [NO_RESULTS, SOURCE_LIST]), "/": _render("index.html", [], None)}
    page = browser.new_page()
    errors, posts = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)

    def handle(route):
        url = route.request.url
        path = url[len(ORIGIN):].split("?")[0] if url.startswith(ORIGIN) else None
        if path in pages:
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[path])
        return route.fulfill(status=200, body="")
    page.route("**/*", handle)
    if name == "chat.html":
        page.goto(ORIGIN + "/chat/c1")
    else:
        page.goto(ORIGIN + "/")
        assert page.evaluate("typeof renderMarkdownAnswers") == "function"        # index.html's own script
        page.evaluate("navigateTo('/chat/c1'); 0")
        page.wait_for_function("document.querySelectorAll('.keyword-chip').length === 5")
        assert page.evaluate("typeof renderMarkdownAnswers") == "function"        # still index.html's JS
    # the login/welcome overlay a logged-out stub page shows would cover the answer
    page.evaluate("document.querySelectorAll('[class*=welcome]').forEach(e => { if (!e.closest('.answer-wrap')) e.style.display = 'none'; }); 0")
    return page, errors, posts


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_notion_example_chips(browser, name):
    page, errors, posts = _open(browser, name)
    try:
        chips = page.evaluate("[...document.querySelectorAll('.keyword-chip')].map(c => [c.textContent, c.tagName, getComputedStyle(c).color])")
        texts = [c[0] for c in chips]
        assert texts == ["Notion bugs", "Notion slow", "Notion help", "Notion troubleshooting", "Notion issues"]
        assert all(tag == "BUTTON" and color == "rgb(0, 157, 255)" for _, tag, color in chips)
        assert page.evaluate("document.body.textContent.includes('Try these related searches:')")
        assert NOTION not in page.evaluate("[...document.querySelectorAll('.keyword-chip')].map(c => c.textContent).join('|')")
        for term in ("Notion help", "Notion troubleshooting", "Notion issues"):
            page.get_by_role("button", name=term, exact=True).click()
            assert page.evaluate(VISIBLE_INPUT + ".value") == term                   # only that keyword
            assert page.evaluate("document.activeElement === " + VISIBLE_INPUT)
        assert posts == []                                                     # no search launched
        # the other suggested actions still render/work as before
        assert page.evaluate("document.body.textContent.includes('Extend to last 30 days')")
        page.locator(".post-field", has_text="What do people dislike about Notion?").first.click()
        assert page.evaluate("document.getElementById('search-input').value") == "What do people dislike about Notion?"   # unchanged behaviour
        assert errors == []
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_links_blue_and_green_accents_kept(browser, name):
    page, errors, _ = _open(browser, name)
    try:
        link = page.evaluate("getComputedStyle(document.querySelector('.answer-rendered a[href*=\"reddit.com\"]')).color")
        assert link == "rgb(0, 157, 255)"
        page.evaluate("""(() => { const d = document.createElement('div'); d.className = 'answer-rendered';
            d.innerHTML = '<p><a id="md" href="https://example.com">md link</a></p>'; document.body.appendChild(d);
            const w = document.createElement('div'); w.className = 'auth-switch';
            w.innerHTML = '<button id="ok" type="button">Sign up</button>'; document.body.appendChild(w); return 0; })()""")
        assert page.evaluate("getComputedStyle(document.getElementById('md')).color") == "rgb(0, 157, 255)"
        assert page.evaluate("getComputedStyle(document.getElementById('ok')).color") == "rgb(163, 230, 53)"   # green kept
        # accessible contrast of chip text on the chip background over the page background
        ratio = page.evaluate("""(() => { const lum = (r,g,b) => [r,g,b].map(v => { v /= 255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); })
              .reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
            const bg = [0x17, 0x16, 0x15].map((v, i) => Math.round(v * 0.88 + [0, 157, 255][i] * 0.12));
            const L1 = lum(0, 157, 255), L2 = lum(...bg); return (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05); })()""")
        assert ratio >= 4.5
        assert errors == []
    finally:
        page.close()
