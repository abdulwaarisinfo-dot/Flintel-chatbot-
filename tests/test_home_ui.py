"""
tests/test_home_ui.py — homepage UI: "Explore Flintel" visibility, the
time-of-day greeting, and that the first-use layout and the search box are
unchanged.

State rule (routes.owner_has_started_conversation): an owner has "started" once
at least one of THEIR saved turns has a non-blank user message. It is derived
from existing chat records only (no new field, no browser flag). Known
limitation: Explore Flintel can reappear if all of an owner's conversation
history is deleted, or if a guest clears their cookies (a new guest owner).

Rendering goes through the real routes.home()/routes.search() handlers and the
real templates with stub modules and mongomock; the browser-layout tests use
the shared headless Chromium and skip when it is unavailable. No real Mongo,
LLM or network.
"""
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.requests import Request

from tests.test_answer_persistence import env  # noqa: F401  (fixture: stub modules, real index/routes)

mongomock = pytest.importorskip("mongomock")

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "templates" / "index.html"
CHAT_HTML = ROOT / "templates" / "chat.html"


@pytest.fixture
def home(env, monkeypatch):
    lg, idx, _ = env
    rt = sys.modules["routes"]
    db = mongomock.MongoClient()["home_ui"]
    monkeypatch.setattr(idx, "chats_collection", db.chats)
    monkeypatch.setattr(rt, "get_user_chat_list", lambda k: [])
    monkeypatch.setattr(rt, "get_current_user", lambda r: None)
    monkeypatch.setattr(rt, "_is_owner_busy", lambda k: False)
    monkeypatch.setattr(rt, "_set_owner_busy", lambda k: None)
    monkeypatch.setattr(rt, "_clear_owner_busy", lambda k: None)
    state = {"owner": "o1"}
    monkeypatch.setattr(rt, "get_owner", lambda request: (state["owner"], "anon"))

    def request(session=None):
        return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"",
                        "session": session if session is not None else {}, "app": idx.app,
                        "router": idx.app.router})

    def render(session=None):
        return rt.home(request(session)).body.decode("utf-8")

    def add_chat(owner, messages, chat_id=None):
        db.chats.insert_one({"chat_id": chat_id or f"c{db.chats.count_documents({}) + 1}", "owner_key": owner,
                             "messages": messages})
    return {"rt": rt, "idx": idx, "db": db, "state": state, "render": render, "request": request,
            "add_chat": add_chat}


def has_explore(html):
    return 'class="explore-panel"' in html and 'data-query="Find people looking for alternatives"' in html


def has_greeting(html):
    return 'id="home-greeting"' in html


def hero_is_started(html):
    return re.search(r'<div class="content-wrap[^"]*\bhero-started\b[^"]*" id="hero-wrap"', html) is not None


# ── the "conversation started" rule ─────────────────────────────────────────
@pytest.mark.parametrize("messages,expected", [
    ([], False),                                                      # an empty chat record
    ([{"query": ""}], False),
    ([{"query": "   \n\t"}], False),
    ([{"query": None}, {"claude_answer": "orphan answer"}], False),   # no user text at all
    ([{"query": "  "}, {"query": "Find CRM buyers"}], True),         # one real message is enough
    ([{"query": "Hi", "message_type": "chat", "claude_answer": "Hello!"}], True),   # small talk is a real message
    ([{"query": "x"}], True),
])
def test_started_rule_for_the_owner_own_chats(home, messages, expected):
    home["add_chat"]("o1", messages)
    assert home["rt"].owner_has_started_conversation("o1") is expected


def test_started_rule_is_owner_scoped(home):
    home["add_chat"]("someone-else", [{"query": "a real message"}])
    home["add_chat"]("o1", [])                                         # o1 only has an empty chat
    rt = home["rt"]
    assert rt.owner_has_started_conversation("someone-else") is True
    assert rt.owner_has_started_conversation("o1") is False
    assert rt.owner_has_started_conversation("nobody") is False
    assert rt.owner_has_started_conversation("") is False and rt.owner_has_started_conversation(None) is False


def test_started_rule_counts_any_chat_not_just_the_first(home):
    home["add_chat"]("o1", [])
    home["add_chat"]("o1", [{"query": "  "}])
    assert home["rt"].owner_has_started_conversation("o1") is False
    home["add_chat"]("o1", [{"query": "now a real one"}])
    assert home["rt"].owner_has_started_conversation("o1") is True


def test_started_rule_treats_a_store_error_as_started(home, monkeypatch):
    class Broken:
        def find_one(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(home["idx"], "chats_collection", Broken())
    assert home["rt"].owner_has_started_conversation("o1") is True      # Explore never reappears on a hiccup


def test_started_rule_query_is_read_only_and_owner_filtered(home, monkeypatch):
    seen = []

    class Spy:
        def find_one(self, filt, proj=None):
            seen.append((filt, proj))
            return None

        def __getattr__(self, name):
            raise AssertionError(f"unexpected collection call: {name}")
    monkeypatch.setattr(home["idx"], "chats_collection", Spy())
    home["rt"].owner_has_started_conversation("o1")
    assert len(seen) == 1 and seen[0][0]["owner_key"] == "o1" and seen[0][1] == {"_id": 1}


# ── rendering: first-use homepage is unchanged ─────────────────────────────
def test_first_use_homepage_keeps_the_original_hero_and_explore(home):
    html = home["render"]()
    assert '<p class="eyebrow">Search</p>' in html
    assert re.search(r'<h1 class="headline">What(\'|&#39;)s the internet saying\?</h1>', html)
    assert has_explore(html) and html.count('data-query="Find') == 3
    assert not has_greeting(html) and not hero_is_started(html)


def test_owner_with_only_an_empty_chat_still_sees_the_first_use_homepage(home):
    home["add_chat"]("o1", [])
    html = home["render"]()
    assert has_explore(html) and not has_greeting(html)


# ── rendering: started homepage ─────────────────────────────────────────────
def test_started_homepage_hides_explore_and_shows_the_greeting(home):
    home["add_chat"]("o1", [{"query": "Find CRM buyers"}])
    html = home["render"]()
    assert not has_explore(html)
    assert 'class="explore-panel"' not in html and 'data-query="Find' not in html and 'explore-title">' not in html
    assert has_greeting(html) and hero_is_started(html)
    assert '<p class="eyebrow">Search</p>' not in html and "internet saying?</h1>" not in html
    assert re.search(r'<h1 class="headline headline-greeting" id="home-greeting" data-greeting-hours=\'\[', html)


def test_explore_stays_hidden_on_every_return_to_the_homepage(home):
    home["add_chat"]("o1", [{"query": "hello"}])
    for _ in range(3):                                                  # reloads / returning from a chat
        assert not has_explore(home["render"]({"active_chat_id": "c1"}))


def test_other_owners_are_not_affected(home):
    home["add_chat"]("o1", [{"query": "hello"}])
    home["state"]["owner"] = "brand-new-guest"
    html = home["render"]()
    assert has_explore(html) and not has_greeting(html)
    home["state"]["owner"] = "o1"
    assert not has_explore(home["render"]())


def test_every_index_render_follows_the_same_rule_including_error_pages(home):
    rt = home["rt"]
    home["add_chat"]("o1", [{"query": "hello"}])
    err = rt.search(home["request"](), query="   ", platform="All Platforms", chat_id=None).body.decode("utf-8")
    assert "Please enter a search term." in err and not has_explore(err) and has_greeting(err)
    home["state"]["owner"] = "first-timer"
    err = rt.search(home["request"](), query="   ", platform="All Platforms", chat_id=None).body.decode("utf-8")
    assert "Please enter a search term." in err and has_explore(err) and not has_greeting(err)


def test_chat_page_never_runs_the_started_query():
    text = CHAT_HTML.read_text(encoding="utf-8")
    assert "conversation_started" not in text and "home_greeting" not in text


def test_templates_render_without_the_helpers(env):
    """index.html must not break where the route helpers are not registered."""
    from jinja2 import Environment, FileSystemLoader
    out = Environment(loader=FileSystemLoader(str(ROOT / "templates")), autoescape=True).get_template(
        "index.html").render(user=None, chats=[], chat=None, chat_id=None)
    assert has_explore(out) and not has_greeting(out)


# ── the greeting: table, boundaries, first-paint timezone ───────────────────
@pytest.mark.parametrize("hour,label", [
    (0, "Night thoughts"), (4, "Night thoughts"), (5, "Morning thoughts"), (11, "Morning thoughts"),
    (12, "Afternoon thoughts"), (16, "Afternoon thoughts"), (17, "Evening thoughts"), (20, "Evening thoughts"),
    (21, "Night thoughts"), (23, "Night thoughts"),
])
def test_greeting_boundaries(home, hour, label):
    rt = home["rt"]
    assert rt.greeting_for_hour(hour) == label and rt.GREETING_BY_HOUR[hour] == label


def test_greeting_table_covers_all_24_hours_with_the_approved_ranges(home):
    table = home["rt"].GREETING_BY_HOUR
    assert len(table) == 24
    expected = (["Night thoughts"] * 5 + ["Morning thoughts"] * 7 + ["Afternoon thoughts"] * 5 +
                ["Evening thoughts"] * 4 + ["Night thoughts"] * 3)
    assert list(table) == expected


def test_first_paint_uses_the_given_timezone(home):
    rt = home["rt"]
    t = datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc)
    assert rt.greeting_first_paint("UTC", t) == "Afternoon thoughts"
    assert rt.greeting_first_paint("Asia/Karachi", t) == "Evening thoughts"          # 17:30 local
    assert rt.greeting_first_paint("Pacific/Auckland", t) == "Night thoughts"        # 01:30 next day
    assert rt.greeting_first_paint("America/Los_Angeles", t) == "Morning thoughts"   # 05:30
    assert rt.greeting_first_paint("Not/AZone", t) == "Afternoon thoughts"           # invalid -> UTC


def test_first_paint_timezone_falls_back_from_session_to_the_default(home, monkeypatch):
    rt = home["rt"]
    monkeypatch.setattr(rt, "_utcnow", lambda: datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc))
    home["add_chat"]("o1", [{"query": "hello"}])
    label = lambda html: re.search(r'id="home-greeting"[^>]*>([^<]+)<', html).group(1)   # noqa: E731
    assert label(home["render"]({"tz": "Pacific/Auckland"})) == "Night thoughts"            # session timezone
    default = label(home["render"]({}))
    assert default == rt.greeting_first_paint(rt._request_timezone(home["request"]()))        # DEFAULT_USER_TIMEZONE
    assert label(home["render"]({"tz": "garbage"})) == default                                # invalid session tz ignored


def test_rendered_table_matches_python(home):
    home["add_chat"]("o1", [{"query": "hello"}])
    html = home["render"]()
    raw = re.search(r"data-greeting-hours='([^']+)'", html).group(1)
    assert json.loads(raw.replace("&#34;", '"')) == list(home["rt"].GREETING_BY_HOUR)


# ── static checks on the templates ──────────────────────────────────────────
def _helper(path):
    text = path.read_text(encoding="utf-8")
    start = text.index("window.flintelRefreshGreeting = function")
    end = text.index("</script>", start)
    return text[start:end]


def test_greeting_helper_is_identical_in_both_templates_and_called_after_each_swap():
    assert _helper(INDEX_HTML) == _helper(CHAT_HTML)
    for path in (INDEX_HTML, CHAT_HTML):
        text = path.read_text(encoding="utf-8")
        assert text.count("currentMain.innerHTML = newMain.innerHTML;\n      if (window.flintelRefreshGreeting)") == 1
        assert "visibilitychange" in _helper(path)


def test_enriqueta_is_added_to_the_existing_google_fonts_request_only():
    for path in (INDEX_HTML, CHAT_HTML):
        text = path.read_text(encoding="utf-8")
        links = re.findall(r'<link href="(https://fonts\.googleapis\.com/[^"]+)"', text)
        assert links == ["https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700"
                         "&family=Enriqueta:wght@400&display=swap"]
        assert "fonts.gstatic" not in text and "@import" not in text.split("</style>")[0].split("<style")[1][:200]


def test_search_box_and_hero_container_base_css_are_unchanged():
    css = INDEX_HTML.read_text(encoding="utf-8")
    for needle in ("""  .content-wrap {
    display: flex; align-items: center; justify-content: center;
    min-height: 663px;
    padding: 0 20px;
  }
  .content-inner { width: 100%; max-width: 720px; }
  @media (min-width: 1060px) {
    .content-wrap { padding-left: 109px; padding-right: 109px; justify-content: center; }
    .content-inner { max-width: 946px; width: 100%; }
  }""", """  .search-box {
    background: #1e1d1c;
    border: 1px solid #3d3d3b;
    border-radius: 18px;
    padding: 18px 24px 19px 24px;""", """  .eyebrow { color: #757473; font-size: 15px; letter-spacing: 0.15px; margin-bottom: 13px; }
  .headline {
    color: #fff; font-size: 27px; letter-spacing: -0.54px; font-weight: 500;
    margin-bottom: 31px; line-height: 1.2;
  }"""):
        assert needle in css
    # the narrower box / fit-to-viewport rules exist only for the started hero, and only above mobile
    block = css[css.index("@media (min-width: 768px) {\n    /* the started hero"):]
    block = block[:block.index("\n  }\n") + 5]
    assert ".content-wrap.hero-started .content-inner { width: 85%; max-width: 804px; }" in block
    assert "calc(100vh - 62px)" in block and "padding-bottom: 28vh" in block


def test_no_green_focus_border_on_the_search_box_in_either_template():
    for path in (INDEX_HTML, CHAT_HTML):
        css = path.read_text(encoding="utf-8").split("</style>")[0]
        assert ".search-box:focus-within {" not in css and "search-box:focus" not in css
        assert "linear-gradient(90deg, #a3e635, #4d7c0f) border-box" not in css


# ── the helper's behaviour in a JavaScript engine ───────────────────────────
NODE_SCRIPT = r"""
const listeners = {};
const els = [];
globalThis.window = globalThis;
globalThis.document = {
  visibilityState: 'visible',
  addEventListener: (n, f) => { (listeners[n] = listeners[n] || []).push(f); },
  querySelectorAll: () => els,
};
function el(hours, text) {
  return { textContent: text, getAttribute: (n) => (n === 'data-greeting-hours' ? hours : null) };
}
const table = %s;
const RealDate = Date;
function setHour(h) { globalThis.Date = class extends RealDate { getHours() { return h; } }; }
%s
%s
const out = { labels: [], bound: 0 };
const e = el(JSON.stringify(table), 'server fallback');
els.push(e);
for (let h = 0; h < 24; h++) { setHour(h); window.flintelRefreshGreeting(); out.labels.push(e.textContent); }
// a second copy of the helper (the other template) must not bind a second listener
%s
out.bound = (listeners.visibilitychange || []).length;
// visibility change re-evaluates the local hour
setHour(18); e.textContent = 'stale'; listeners.visibilitychange[0](); out.afterVisible = e.textContent;
document.visibilityState = 'hidden'; setHour(7); listeners.visibilitychange[0](); out.hiddenIgnored = e.textContent;
// bad data never throws and keeps the server label
const bad = el('not json', 'server label'); els.length = 0; els.push(bad);
window.flintelRefreshGreeting(); out.bad = bad.textContent;
const short = el('["only one"]', 'kept'); els.length = 0; els.push(short); setHour(5);
window.flintelRefreshGreeting(); out.short = short.textContent;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_greeting_helper_in_node(home):
    table = list(home["rt"].GREETING_BY_HOUR)
    body = _helper(INDEX_HTML)
    script = NODE_SCRIPT % (json.dumps(table), body, "", body)
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30,
                                    check=True).stdout.strip().splitlines()[-1])
    assert out["labels"] == table                                  # the browser's hour picks the label, all 24 hours
    assert out["bound"] == 1                                        # one visibilitychange listener, however many copies
    assert out["afterVisible"] == "Evening thoughts"                # 18:00 local when the tab becomes visible
    assert out["hiddenIgnored"] == "Evening thoughts"               # a hidden tab does not update
    assert out["bad"] == "server label" and out["short"] == "kept"


# ── real-browser layout ─────────────────────────────────────────────────────
def _page(browser, html, width, height=900, hour=None):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()
    page.route("**/*", lambda route: route.abort())                 # offline: no fonts, CDN scripts or API calls
    if hour is not None:
        page.add_init_script(f"Date.prototype.getHours = function () {{ return {hour}; }};")
    page.goto("about:blank")                                        # init scripts run on navigation, not on set_content
    page.set_content(html, wait_until="domcontentloaded")
    return ctx, page


def _metrics(page):
    return page.evaluate("""() => {
      const r = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect();
        return {x: b.x, y: b.y, w: b.width, h: b.height, cx: b.x + b.width / 2}; };
      const h = document.querySelector('#hero-wrap h1');
      return { box: r('#hero-wrap .search-box'), inner: r('#hero-wrap .content-inner'), h1: r('#hero-wrap h1'),
               font: h ? getComputedStyle(h).fontSize : null, family: h ? getComputedStyle(h).fontFamily : null,
               align: h ? getComputedStyle(h).textAlign : null, text: h ? h.textContent : null,
               overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
               explore: !!document.querySelector('.explore-panel') };
    }""")


FIRST_USE_BOX_WIDTH = {1440: 946, 1280: 806, 1100: 626, 900: 604}          # unchanged first-use homepage
STARTED_BOX_WIDTH = {1920: 804, 1440: 804, 1366: 758, 1280: 685, 1100: 532, 900: 513, 768: 401}   # 85% of the column, max 804


@pytest.mark.parametrize("width", [1440, 1280, 1100, 900])
def test_first_use_layout_is_unchanged_and_started_box_is_narrower(home, integrity_browser, width):
    first_html = home["render"]()
    home["add_chat"]("o1", [{"query": "hello"}])
    started_html = home["render"]()
    ctx1, p1 = _page(integrity_browser, first_html, width)
    ctx2, p2 = _page(integrity_browser, started_html, width, hour=18)
    try:
        a, b = _metrics(p1), _metrics(p2)
    finally:
        ctx1.close()
        ctx2.close()
    assert a["explore"] is True and b["explore"] is False
    assert round(a["box"]["w"]) == FIRST_USE_BOX_WIDTH[width] and a["font"] == "27px"          # first-use: exactly as before
    assert round(b["box"]["w"]) == STARTED_BOX_WIDTH[width] < a["box"]["w"]                  # started: narrower
    assert abs(b["box"]["cx"] - a["box"]["cx"]) < 1.5                                          # still centered
    assert b["text"] == "Evening thoughts"                                                     # the local hour wins
    assert b["align"] == "center" and abs(b["h1"]["cx"] - b["inner"]["cx"]) < 1               # greeting centered on the column
    assert b["font"] == "36px" and "Enriqueta" in b["family"]                                  # smaller greeting
    assert 30 <= b["box"]["y"] - (b["h1"]["y"] + b["h1"]["h"]) <= 50                          # greeting-to-box gap
    assert not a["overflow"] and not b["overflow"]


def test_started_greeting_and_box_are_raised(home, integrity_browser):
    home["add_chat"]("o1", [{"query": "hello"}])
    ctx, page = _page(integrity_browser, home["render"](), 1440, 800, hour=14)
    try:
        m = _metrics(page)
    finally:
        ctx.close()
    assert m["text"] == "Afternoon thoughts"
    assert m["h1"]["y"] < 230 and m["box"]["y"] < 320                    # was 272 / 374 at 1440x800


@pytest.mark.parametrize("width", [1920, 1440, 1366, 1280, 1100, 900, 768])
@pytest.mark.parametrize("height", [1080, 800, 700, 650, 560])
def test_started_homepage_never_scrolls(home, integrity_browser, width, height):
    home["add_chat"]("o1", [{"query": "hello"}])
    ctx, page = _page(integrity_browser, home["render"](), width, height, hour=14)
    try:
        sizes = page.evaluate("() => [document.documentElement.scrollHeight, document.documentElement.clientHeight]")
        m = _metrics(page)
    finally:
        ctx.close()
    assert sizes[0] <= sizes[1], f"page scrolls at {width}x{height}: {sizes}"
    assert m["box"]["y"] + m["box"]["h"] <= height and not m["overflow"]       # greeting and box fully on screen


def test_mobile_started_homepage_is_unchanged_except_for_the_smaller_greeting(home, integrity_browser):
    home["add_chat"]("o1", [{"query": "hello"}])
    first = home["render"]()
    ctx, page = _page(integrity_browser, first, 390, 800, hour=3)
    try:
        m = _metrics(page)
        sizes = page.evaluate("() => [document.documentElement.scrollHeight, document.documentElement.clientHeight]")
    finally:
        ctx.close()
    assert m["text"] == "Night thoughts" and m["font"] == "28px" and not m["overflow"] and not m["explore"]
    assert round(m["box"]["w"]) == 350 and m["box"]["x"] == 20                      # same box as before on mobile
    assert sizes[0] <= sizes[1]


def test_server_label_is_the_fallback_when_the_browser_hour_is_unavailable(home, integrity_browser, monkeypatch):
    rt = home["rt"]
    monkeypatch.setattr(rt, "_utcnow", lambda: datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc))
    home["add_chat"]("o1", [{"query": "hello"}])
    html = home["render"]({"tz": "UTC"})
    ctx, page = _page(integrity_browser, html.replace("window.flintelRefreshGreeting = function",
                                                      "window.flintelNoop = function"), 1440)
    try:
        assert _metrics(page)["text"] == "Morning thoughts"
    finally:
        ctx.close()


# ═════════════════════════════════════════════════════════════════════════════
# The conversation never goes past the search box, on any screen; no focus highlight.
# ═════════════════════════════════════════════════════════════════════════════
from tests.test_answer_persistence import GOOD, NOW, ORIGIN, _for_template   # noqa: E402
from tests.test_outreach_ui import _report                                    # noqa: E402

jinja2 = pytest.importorskip("jinja2")
LONG_URL = "https://www.example.com/" + "very-long-path-segment-" * 14 + "end"
SCREENS = [(1920, 1000), (1536, 800), (1440, 800), (1366, 700), (1280, 720), (1100, 700), (1060, 700), (1059, 700),
           (900, 800), (800, 800), (768, 800), (390, 800)]
COLUMN_WIDTH = {1920: 804, 1536: 804, 1440: 804, 1366: 758, 1280: 685, 1100: 532, 1060: 498, 1059: 649, 900: 513,
                800: 428, 768: 401, 390: 358}          # 85% of the space between the gutters, never more than 804px

MEASURE_COLUMN = """() => {
  const box = document.querySelector('.docked-input-wrap .search-box');
  const bb = box.getBoundingClientRect();
  const col = document.querySelector('.thread-inner, .conversation-area').getBoundingClientRect();
  const clipped = (el) => { const r = el.getBoundingClientRect(); let left = r.left, right = r.right;
    for (let a = el.parentElement; a && !a.matches('.thread-inner, .conversation-area'); a = a.parentElement) {
      if (getComputedStyle(a).overflowX !== 'visible') { const ar = a.getBoundingClientRect(); left = Math.max(left, ar.left); right = Math.min(right, ar.right); } }
    return [left, right]; };
  const outside = [];
  document.querySelectorAll('.thread-inner *, .conversation-area *').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return;
    const r = el.getBoundingClientRect(); if (!r.width || !r.height) return;
    const [l, rt] = clipped(el);
    if (l < bb.left - 1 || rt > bb.right + 1) outside.push(el.tagName.toLowerCase() + '.' + String(el.className).split(' ')[0]);
  });
  return {box: [bb.left, bb.right], column: [col.left, col.right], outside, scrollX: document.documentElement.scrollWidth > document.documentElement.clientWidth};
}"""
INJECT_WIDE = """() => { const t = document.querySelector('.thread-inner .turn-block:last-child .answer-rendered, .conversation-area .turn-block:last-child .answer-rendered') || document.querySelector('.thread-inner, .conversation-area');
  t.style.display = 'block';
  t.insertAdjacentHTML('beforeend', '<table><thead><tr><th>A</th><th>B</th><th>C</th><th>D</th><th>E</th><th>F</th></tr></thead><tbody><tr>' + [1,2,3,4,5,6].map(i => '<td>cell ' + i + ' ' + 'wide_unbreakable_content_'.repeat(3) + '</td>').join('') + '</tr></tbody></table><pre><code>' + 'const veryLongLine = "' + 'x'.repeat(260) + '";</code></pre><img src="data:image/svg+xml;utf8,<svg xmlns=%27http://www.w3.org/2000/svg%27 width=%271400%27 height=%2780%27/>">'); 0 }"""


def _jinja(name, chat):
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.ChainableUndefined,
                           autoescape=True)
    e.globals["url_for"] = lambda *a, **k: "#"
    return e.get_template(name).render(request=None, chat=chat, chats=[], user=None, chat_id=(chat or {}).get("chat_id"),
                                       pending_stream_topic_key=None, messages=(chat or {}).get("messages", []))


def _long_chat(lg, monkeypatch):
    outreach = _report(lg, monkeypatch)                                 # a real report: wide table, cards, copy buttons
    base = {"topic_key": None, "keywords": [], "results": [], "message_type": "chat", "requested_at": NOW}
    msgs = [dict(base, query="Find buyers for our product " + LONG_URL,
                 claude_answer="Here is a link " + LONG_URL + " and a long unbroken token " + "x" * 120),
            dict(base, query="Now make me suggested message for outreach", claude_answer=outreach),
            {"query": "Compare tools", "topic_key": "q", "keywords": [], "results": [], "claude_answer": json.dumps(GOOD),
             "requested_at": NOW, "targeting_platform": "reddit"}]
    return _for_template({"chat_id": "c1", "title": "t", "messages": msgs})


def _open_chat(browser, html, width, height):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()
    page.route("**/*", lambda route: route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=html)
               if route.request.url == ORIGIN + "/chat/c1" else route.fulfill(status=200, body=""))
    page.goto(ORIGIN + "/chat/c1")
    page.wait_for_timeout(200)
    page.evaluate("document.querySelectorAll('[class*=welcome]').forEach(e => e.remove()); 0")
    return ctx, page


@pytest.mark.parametrize("width,height", SCREENS)
def test_chat_page_conversation_stays_inside_the_search_box_on_every_screen(env, integrity_browser, monkeypatch, width, height):
    lg, idx, _ = env
    html = _jinja("chat.html", _long_chat(lg, monkeypatch))
    ctx, page = _open_chat(integrity_browser, html, width, height)
    try:
        real = page.evaluate(MEASURE_COLUMN)
        page.evaluate(INJECT_WIDE)                                      # a wide table, a long code line, a 1400px picture
        wide = page.evaluate(MEASURE_COLUMN)
    finally:
        ctx.close()
    for m in (real, wide):
        assert abs(m["box"][0] - m["column"][0]) < 1 and abs(m["box"][1] - m["column"][1]) < 1   # same left and right edge
        assert m["outside"] == [], f"content outside the search box at {width}x{height}: {m['outside']}"
        assert not m["scrollX"]
    assert abs((real["box"][1] - real["box"][0]) - COLUMN_WIDTH[width]) < 1.5


@pytest.mark.parametrize("width", [1920, 1440, 1100, 768])
def test_index_page_conversation_area_shares_the_dock_column(env, integrity_browser, monkeypatch, width):
    lg, idx, _ = env
    html = _jinja("index.html", _long_chat(lg, monkeypatch))
    ctx = integrity_browser.new_context(viewport={"width": width, "height": 800})
    page = ctx.new_page()
    page.route("**/*", lambda route: route.abort())
    page.goto("about:blank")
    page.set_content(html, wait_until="domcontentloaded")
    try:
        page.evaluate("""() => { const d = document.getElementById('docked-input-wrap'); d.hidden = false; d.style.display = 'block';
                               document.getElementById('conversation-area').classList.remove('hidden'); 0 }""")
        m = page.evaluate(MEASURE_COLUMN)
    finally:
        ctx.close()
    assert abs(m["box"][0] - m["column"][0]) < 1 and abs(m["box"][1] - m["column"][1]) < 1
    assert m["outside"] == [] and not m["scrollX"]


FOCUS_JS = """() => { const el = document.querySelector('%s'); const box = el.closest('.search-box'); const cs = getComputedStyle(box);
  const snap = () => ({border: cs.borderTopColor, width: cs.borderTopWidth, image: cs.backgroundImage, color: cs.backgroundColor, shadow: cs.boxShadow});
  const before = snap(); el.focus(); const focused = document.activeElement === el; const after = snap(); return {before, after, focused}; }"""


def _assert_no_focus_highlight(r):
    assert r["focused"] is True
    assert r["before"] == r["after"]                                    # focusing changes nothing about the box
    assert r["after"]["image"] == "none" and r["after"]["border"] == "rgb(61, 61, 59)"
    assert "163, 230, 53" not in " ".join(r["after"].values())          # no green anywhere


def test_focusing_the_homepage_search_box_shows_no_green_stroke(home, integrity_browser):
    home["add_chat"]("o1", [{"query": "hello"}])
    for html in (home["render"](), ):
        ctx, page = _page(integrity_browser, html, 1440, 800, hour=14)
        try:
            _assert_no_focus_highlight(page.evaluate(FOCUS_JS % "#hero-wrap #search-input"))
        finally:
            ctx.close()


def test_focusing_the_first_use_and_chat_page_search_boxes_shows_no_green_stroke(home, env, integrity_browser, monkeypatch):
    first = home["render"]()                                            # first-use homepage is also covered
    ctx, page = _page(integrity_browser, first, 1440, 800)
    try:
        _assert_no_focus_highlight(page.evaluate(FOCUS_JS % "#hero-wrap #search-input"))
    finally:
        ctx.close()
    lg, idx, _ = env
    ctx, page = _open_chat(integrity_browser, _jinja("chat.html", _long_chat(lg, monkeypatch)), 1440, 800)
    try:
        _assert_no_focus_highlight(page.evaluate(FOCUS_JS % ".docked-input-wrap #search-input"))
    finally:
        ctx.close()
