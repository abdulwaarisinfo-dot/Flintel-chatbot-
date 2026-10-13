"""
tests/test_platform_multiselect.py — Platforms dropdown with logos and
multi-select (one, several, or all platforms), and the removed "All Platforms"
and "Completed" pills under each question.

  * backend: normalize_platforms / matchers / job platform (stub modules, no
    real Mongo / LLM / network);
  * real Chromium (Playwright) when available: the Jinja-rendered templates.
"""
import datetime
import importlib
import json
import pathlib
import sys
import types
from unittest.mock import MagicMock

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEMPLATES = ("chat.html", "index.html")


# ── backend ──────────────────────────────────────────────────────────────────
@pytest.fixture
def lg(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-platforms")
    db = types.ModuleType("database")
    db.__getattr__ = lambda name: MagicMock(name=f"database.{name}")
    fi = types.ModuleType("flintel")
    fi.__getattr__ = lambda name: "" if name.isupper() else MagicMock(name=f"flintel.{name}")
    hx = types.ModuleType("httpx")
    hx.AsyncClient = MagicMock()
    hx.TimeoutException = hx.HTTPStatusError = Exception
    gg = types.ModuleType("google")
    gg.__getattr__ = lambda name: MagicMock(name=f"google.{name}")
    for name, mod in {"database": db, "flintel": fi, "httpx": hx, "google": gg,
                      "website_intelligence": types.ModuleType("website_intelligence")}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    for name in ("logics", "config"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    mod = importlib.import_module("logics")
    yield mod
    for name in ("logics", "config"):
        sys.modules.pop(name, None)


@pytest.mark.parametrize("raw,expected", [
    ("All Platforms", "all"), ("", "all"), (None, "all"), ("garbage", "all"),
    ("Reddit", "reddit"), ("X / Twitter", "x_twitter"), ("LinkedIn", "linkedin"), ("Facebook", "facebook"),
    ("X / Twitter, LinkedIn", "x_twitter,linkedin"),
    ("LinkedIn, X / Twitter", "x_twitter,linkedin"),              # fixed order
    ("Reddit, LinkedIn, Facebook", "reddit,linkedin,facebook"),
    ("Reddit, Reddit", "reddit"),
    ("X / Twitter, Reddit, LinkedIn, Facebook", "all"),           # every platform = all
    ("Reddit, All Platforms", "all"),
    ("Reddit, nonsense", "reddit"),
])
def test_normalize_platforms(lg, raw, expected):
    assert lg.normalize_platforms(raw) == expected


@pytest.mark.parametrize("raw", ["All Platforms", "all", "Reddit", "X / Twitter", "x", "twitter", "LinkedIn", "Facebook", "", "junk"])
def test_single_values_unchanged(lg, raw):
    assert lg.normalize_platforms(raw) == lg.normalize_platform(raw)


def test_signal_matcher_multi(lg):
    docs = {"reddit": {"platform": "reddit"}, "x": {"platform": "twitter"}, "li": {"platform": "linkedin"},
            "fb": {"platform": "facebook"}, "url_li": {"post_url": "https://www.linkedin.com/posts/abc"}}
    hit = {k for k, d in docs.items() if lg._signal_platform_matches(d, "x_twitter,linkedin")}
    assert hit == {"x", "li", "url_li"}
    assert all(lg._signal_platform_matches(d, "all") for d in docs.values())
    assert {k for k, d in docs.items() if lg._signal_platform_matches(d, "reddit")} == {"reddit"}


def test_flintel_default_matcher_multi():
    sys.modules.pop("flintel", None)
    try:
        fl = importlib.import_module("flintel")
        assert fl._default_platform_matches({"platform": "reddit"}, "reddit,facebook")
        assert not fl._default_platform_matches({"platform": "linkedin"}, "reddit,facebook")
        assert fl._default_platform_matches({"platform": "linkedin"}, "all")
    finally:
        sys.modules.pop("flintel", None)


def test_single_platform_query(lg):
    assert lg._single_platform_query("anything", "reddit") is True
    assert lg._single_platform_query("anything", "x_twitter,linkedin") is False
    assert lg._single_platform_query("posts on reddit", "x_twitter,linkedin") is True   # query text still counts


def test_job_gets_all_for_multi_and_single_unchanged(lg, monkeypatch):
    jobs = MagicMock()
    monkeypatch.setattr(lg, "jobs_collection", jobs)
    lg.enqueue_search_job("t1", ["k"], "x_twitter,linkedin")
    lg.enqueue_search_job("t2", ["k"], "reddit")
    sent = [c.args[1]["$set"]["targeting_platform"] for c in jobs.update_one.call_args_list]
    assert sent == ["all", "reddit"]


def test_search_route_uses_normalize_platforms():
    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    assert "targeting_platform = normalize_platforms(platform)" in src


# ── templates (static) ───────────────────────────────────────────────────────
def _src(name):
    return (ROOT / "templates" / name).read_text(encoding="utf-8")


def _helpers(name):
    s = _src(name)
    a = s.index("  // PLATFORM-MULTI-HELPERS-BEGIN")
    return s[a:s.index("// PLATFORM-MULTI-HELPERS-END", a)]


def test_helpers_identical_in_both_templates():
    assert _helpers("chat.html") == _helpers("index.html")


def test_platform_pill_removed_and_completed_hidden():
    s = _src("chat.html")
    assert "{%- if _plat in ['all', 'all platforms'] -%}All Platforms" not in s
    assert ".turn-chip.status-completed { display: none; }" in s


# ── real browser ─────────────────────────────────────────────────────────────
try:
    import jinja2
    from playwright.sync_api import sync_playwright
    HAVE_BROWSER = True
except Exception:          # pragma: no cover
    HAVE_BROWSER = False

ORIGIN = "http://flintel.test"
NOW = datetime.datetime(2026, 10, 13, 10, 0, 0)
ANSWER = {"format": "source_list", "headline": "H", "executive_summary": "People want heavy bikes.",
          "platforms": [{"platform": "reddit", "total_analyzed": 1, "shown_count": 1, "posts": [
              {"index": 1, "source": "bikes", "title": "Post", "summary": "s", "sentiment": "neutral",
               "link": "https://www.reddit.com/r/bikes/comments/p1/"}]}]}


def _render(name, answered=True, platform="all"):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")),
                             undefined=jinja2.ChainableUndefined)
    env.globals["url_for"] = lambda *a, **k: "#"
    msgs = [{"query": "Find me users who are looking to buy Heavy bikes", "topic_key": "t0",
             "targeting_platform": platform, "requested_at": NOW, "results": [],
             "claude_answer": json.dumps(ANSWER) if answered else None}]
    chat = {"chat_id": "c1", "title": "t", "messages": msgs} if name == "chat.html" else None
    return env.get_template(name).render(request=None, chat=chat, chats=[], user=None,
                                         chat_id="c1" if chat else None,
                                         pending_stream_topic_key=None, messages=msgs if chat else [])


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


def _open(browser, name, via_index=False):
    pages = {"/chat/c1": _render("chat.html"), "/": _render("index.html")}
    page = browser.new_page()
    errors, posts = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: posts.append(r.post_data or "") if r.method == "POST" else None)

    def handle(route):
        url = route.request.url
        path = url[len(ORIGIN):].split("?")[0] if url.startswith(ORIGIN) else None
        if path in pages:
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[path])
        return route.fulfill(status=200, body="")
    page.route("**/*", handle)
    if name == "chat.html":
        page.goto(ORIGIN + "/chat/c1")
        if via_index:
            raise AssertionError("use name='index.html' for the soft-nav path")
    else:
        page.goto(ORIGIN + "/")
        if via_index:
            page.evaluate("navigateTo('/chat/c1'); 0")
            page.wait_for_selector(".turn-block")
    page.evaluate("document.querySelectorAll('[class*=welcome]').forEach(e => e.style.display = 'none'); 0")
    return page, errors, posts


STATE = """(() => ({
  value: document.getElementById('platform-input').value,
  label: document.getElementById('platforms-label').textContent.trim(),
  logos: document.querySelectorAll('#platforms-label .platform-opt-logo').length,
  selected: [...document.querySelectorAll('#platforms-menu li[aria-selected="true"]')].map(l => l.dataset.value),
  open: document.getElementById('platforms-menu').style.display === 'block',
}))()"""


@pytest.mark.parametrize("name,via_index", [("chat.html", False), ("index.html", False), ("index.html", True)])
def test_browser_multiselect(browser, name, via_index):
    page, errors, _ = _open(browser, name, via_index)
    try:
        opts = page.evaluate("[...document.querySelectorAll('#platforms-menu li[data-value]')].map(l => [l.dataset.value, !!l.querySelector('.platform-opt-logo svg')])")
        assert opts == [["All Platforms", True], ["X / Twitter", True], ["Reddit", True], ["LinkedIn", True], ["Facebook", True]]
        s = page.evaluate(STATE)
        assert s["value"] == "All Platforms" and s["label"] == "All Platforms" and s["selected"] == ["All Platforms"]
        page.click("#platforms-btn")
        menu = page.locator("#platforms-menu")
        menu.get_by_role("option", name="X / Twitter").click()
        s = page.evaluate(STATE)
        assert s["value"] == "X / Twitter" and s["label"] == "X / Twitter" and s["logos"] == 1 and s["open"]
        menu.get_by_role("option", name="LinkedIn").click()
        s = page.evaluate(STATE)
        assert s["value"] == "X / Twitter, LinkedIn" and s["label"] == "2 Platforms" and s["logos"] == 2
        assert s["selected"] == ["X / Twitter", "LinkedIn"] and s["open"]
        menu.get_by_role("option", name="Reddit").click()
        assert page.evaluate(STATE)["value"] == "X / Twitter, Reddit, LinkedIn"
        menu.get_by_role("option", name="Facebook").click()                        # all four = All Platforms
        s = page.evaluate(STATE)
        assert s["value"] == "All Platforms" and s["selected"] == ["All Platforms"] and s["logos"] == 0
        menu.get_by_role("option", name="Reddit").click()
        menu.get_by_role("option", name="Reddit").click()                          # nothing left = All
        assert page.evaluate(STATE)["value"] == "All Platforms"
        menu.get_by_role("option", name="Facebook").click()
        menu.get_by_role("option", name="All Platforms").click()                   # All resets and closes
        s = page.evaluate(STATE)
        assert s["value"] == "All Platforms" and not s["open"]
        page.click("#platforms-btn")
        menu.get_by_role("option", name="Reddit").focus()
        page.keyboard.press("Enter")
        assert page.evaluate(STATE)["value"] == "Reddit"
        page.keyboard.press("Escape")
        assert not page.evaluate(STATE)["open"]
        page.click("#platforms-btn")
        page.mouse.click(5, 5)                                                     # click outside closes
        assert not page.evaluate(STATE)["open"]
        assert errors == []
    finally:
        page.close()


def test_browser_pills_gone(browser):
    page, errors, _ = _open(browser, "chat.html")
    try:
        visible = page.evaluate("[...document.querySelectorAll('.turn-meta .turn-chip')].filter(c => c.offsetParent !== null).map(c => c.textContent.trim())")
        assert visible == []                                     # no "All Platforms", no "Completed"
        assert page.evaluate("document.querySelector('.turn-question').textContent.includes('Heavy bikes')")
        assert page.evaluate("document.querySelector('.answer-rendered').textContent.length") > 0
        assert errors == []
    finally:
        page.close()


def test_browser_pending_chip_still_shows_then_hides(browser):
    page = browser.new_page()
    html = _render("chat.html", answered=False)
    page.route("**/*", lambda r: r.fulfill(status=200, headers={"Content-Type": "text/html"}, body=html)
               if r.request.url.startswith(ORIGIN + "/chat/c1") else r.fulfill(status=200, body=""))
    try:
        page.goto(ORIGIN + "/chat/c1")
        chip = page.locator(".turn-chip[data-turn-timer]")
        assert chip.is_visible()                                 # the live "Searching…" timer stays
        page.evaluate("finishTurnTimer(document.querySelector('.turn-block'), 'completed'); 0")
        assert not chip.is_visible()                             # becomes "Completed" -> hidden
    finally:
        page.close()


def test_browser_submits_multi_value(browser):
    page, errors, posts = _open(browser, "chat.html")
    try:
        page.click("#platforms-btn")
        page.locator("#platforms-menu").get_by_role("option", name="LinkedIn").click()
        page.locator("#platforms-menu").get_by_role("option", name="Facebook").click()
        page.fill("#search-input", "heavy bikes")
        page.locator("#search-input").press("Enter")
        page.wait_for_timeout(600)
        assert posts and 'X / Twitter' not in posts[0]
        assert "LinkedIn, Facebook" in posts[0]
    finally:
        page.close()
