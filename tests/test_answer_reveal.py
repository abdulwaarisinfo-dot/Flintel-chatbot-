"""
tests/test_answer_reveal.py — progressive reveal of a freshly streamed answer
(frontend only, both templates).

  * node: the real ANSWER-REVEAL-HELPERS block extracted from each template
    (same convention as TURN-TIMER-HELPERS in test_templates_render.py);
  * static: the "done" handler still runs its completion/unlock calls first;
  * real Chromium (Playwright) when available: the Jinja-rendered template with
    a stubbed SSE endpoint, exercising the actual stream -> render -> reveal path.
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


def _src(name):
    return (ROOT / "templates" / name).read_text(encoding="utf-8")


def _helpers(name):
    s = _src(name)
    return s[s.index("// ANSWER-REVEAL-HELPERS-BEGIN"):s.index("// ANSWER-REVEAL-HELPERS-END")]


def _node(name, expr, prelude="", replace=None):
    code = _helpers(name)
    if replace:
        code = code.replace(*replace)
    code = prelude + "\n" + code + f"\nconsole.log(JSON.stringify({expr}));"
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ── static ───────────────────────────────────────────────────────────────────
def test_helper_block_identical_in_both_templates():
    assert _helpers("chat.html") == _helpers("index.html")
    assert "const ANSWER_REVEAL_ENABLED = true;" in _helpers("chat.html")
    assert "const REVEAL_MAX_MS = 5000;" in _helpers("chat.html")


@pytest.mark.parametrize("name", TEMPLATES)
def test_done_handler_completes_first_then_reveals(name):
    s = _src(name)
    done = s[s.index("if (payload.done) {"):]
    done = done[:done.index("if (payload.error) {")]
    call = "startAnswerReveal(wrap, { isCurrent: () => myGen === softNavGeneration });"
    assert done.count(call) == 1
    assert done.index("setSubmitLoading(false);") < done.index(call)
    assert done.index("finishTurnTimer(pendingTurn, 'completed', payload.requested_at);") < done.index(call)
    assert done.index("if (myGen !== softNavGeneration)") < done.index(call)
    # the only call site: saved answers (page load / soft navigation) never animate
    assert s.count("startAnswerReveal(") == 2          # definition + the one call above


# ── node: pure helpers ───────────────────────────────────────────────────────
pytestmark_node = pytest.mark.skipif(not NODE, reason="node not installed")


@pytestmark_node
@pytest.mark.parametrize("name", TEMPLATES)
def test_duration_respects_max_and_min_speed(name):
    plans = _node(name, "[[0,0,0],[300,0,3],[2000,10,8],[50000,200,120],[10**7,10**5,10**4]]"
                        ".map(a => computeRevealPlan(...a))")
    for p in plans:
        assert p["totalMs"] <= 5000 and p["cps"] >= 40
    assert plans[1]["cps"] == 120 and plans[1]["totalMs"] < 5000      # short answer: normal speed
    assert plans[3]["totalMs"] == pytest.approx(5000)                   # long answer: sped up to fit


@pytestmark_node
@pytest.mark.parametrize("name", TEMPLATES)
def test_bad_plan_inputs_do_not_crash(name):
    plans = _node(name, "[[NaN,undefined,null],['abc',-5,{}],[Infinity,-Infinity,1e400]].map(a => computeRevealPlan(...a))")
    for p in plans:
        assert p["totalMs"] >= 0 and p["totalMs"] <= 5000 and p["cps"] >= 40


@pytestmark_node
@pytest.mark.parametrize("name", TEMPLATES)
def test_motion_conditions_and_kill_switch(name):
    got = _node(name, "["
                "revealMotionAllowed({matchMedia: () => ({matches: false})}, {hidden: false}),"
                "revealMotionAllowed({matchMedia: () => ({matches: true})}, {hidden: false}),"   # reduced motion
                "revealMotionAllowed({matchMedia: () => ({matches: false})}, {hidden: true}),"   # hidden tab
                "revealMotionAllowed({matchMedia: () => { throw new Error('x'); }}, {hidden: false}),"
                "revealMotionAllowed({}, {hidden: false}),"
                "revealMotionAllowed(null, null)]")
    assert got == [True, False, False, False, True, False]
    off = _node(name, "revealMotionAllowed({matchMedia: () => ({matches: false})}, {hidden: false})",
                replace=("const ANSWER_REVEAL_ENABLED = true;", "const ANSWER_REVEAL_ENABLED = false;"))
    assert off is False


@pytestmark_node
@pytest.mark.parametrize("name", TEMPLATES)
def test_start_with_invalid_input_never_throws(name):
    got = _node(name, "[startAnswerReveal(null, {win: W, doc: D}).animated,"
                      " startAnswerReveal({}, {win: W, doc: D}).animated,"
                      " startAnswerReveal({querySelector: () => { throw new Error('boom'); }}, {win: W, doc: D}).animated,"
                      " startAnswerReveal({querySelector: () => null}, {win: W, doc: D}).animated]",
                prelude="const W = {matchMedia: () => ({matches: false})}; const D = {hidden: false, contains: () => true};")
    assert got == [False, False, False, False]


@pytestmark_node
@pytest.mark.parametrize("name", TEMPLATES)
def test_skip_keys(name):
    got = _node(name, "[isRevealSkipKey('Escape', {tagName: 'INPUT'}), isRevealSkipKey(' ', {tagName: 'DIV'}),"
                      " isRevealSkipKey(' ', {tagName: 'TEXTAREA'}), isRevealSkipKey('End', {tagName: 'BODY'}),"
                      " isRevealSkipKey('End', {tagName: 'INPUT'}), isRevealSkipKey('a', null),"
                      " isRevealSkipKey(' ', {isContentEditable: true}), isRevealSkipKey('Escape', undefined)]")
    assert got == [True, True, False, True, False, False, False, True]


# ── real browser ─────────────────────────────────────────────────────────────
try:
    import jinja2
    from playwright.sync_api import sync_playwright
    HAVE_BROWSER = True
except Exception:          # pragma: no cover - depends on the machine
    HAVE_BROWSER = False

ORIGIN = "http://flintel.test"
ANSWER = {
    "format": "source_list", "headline": "Job seekers describe a slow but recovering AI job market",
    "executive_summary": "People looking for AI roles in Pakistan say openings exist but competition is intense. "
                         "Several mention remote work for US companies as the most realistic route.",
    "key_findings": [
        {"finding": "Remote roles are the main target", "impact": "Most posts aim at US remote jobs.", "supporting_post_indices": [1, 2]},
        {"finding": "Interview loops are long", "impact": "Candidates report 4-6 rounds.", "supporting_post_indices": [2]},
    ],
    "conclusion": "Demand is real, but most success stories involve remote work.",
    "platforms": [{"platform": "reddit", "total_analyzed": 3, "shown_count": 3, "posts": [
        {"index": i, "source": "pakistan", "title": f"Post title {i}", "summary": f"Summary of post {i}",
         "sentiment": "neutral", "link": f"https://www.reddit.com/r/pakistan/comments/p{i}/"} for i in (1, 2, 3)]}],
}
RAW = json.dumps(ANSWER)
NOW = datetime.datetime(2026, 10, 11, 10, 0, 0)


def _msg(topic, answered):
    return {"query": f"Question {topic}", "topic_key": topic, "targeting_platform": "all", "requested_at": NOW,
            "results": [], "claude_answer": RAW if answered else None}


def _render(name, chat_id, messages, pending=None):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")),
                             undefined=jinja2.ChainableUndefined)
    env.globals["url_for"] = lambda *a, **k: "#"
    chat = {"chat_id": chat_id, "title": "t", "messages": messages}
    return env.get_template(name).render(request=None, chat=chat, chats=[], user=None, chat_id=chat_id,
                                         pending_stream_topic_key=pending, messages=messages)


def _sse():
    out = "retry: 86400000\n\n"
    for i in range(0, len(RAW), 40):
        out += f"data: {json.dumps({'delta': RAW[i:i + 40]})}\n\n"
    return out + f"data: {json.dumps({'done': True, 'results': [], 'requested_at': '2026-10-11T10:00:00'})}\n\n"


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


class Page:
    """One page on a fake origin; the SSE response can be held back."""

    def __init__(self, browser, name, *, reduced=False, hold=False, earlier=0):
        self.pages = {
            "/chat/c1": _render(name, "c1", [_msg(f"old{i}", True) for i in range(earlier)] + [_msg("tk1", False)], "tk1"),
            "/chat/c2": _render(name, "c2", [_msg("saved", True)]),
        }
        self.held, self.errors, self.warnings = [], [], []
        self.hold = hold
        self.page = browser.new_page(reduced_motion="reduce" if reduced else "no-preference",
                                     viewport={"width": 1100, "height": 700})
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.warnings.append(m.text) if m.type == "warning" else None)
        self.page.route("**/*", self._handle)

    def _handle(self, route):
        url = route.request.url
        if url.startswith(ORIGIN):
            path = url[len(ORIGIN):].split("?")[0]
            if path.endswith("/stream"):
                if self.hold:
                    self.held.append(route)
                    return None
                return route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=_sse())
            if path in self.pages:
                return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=self.pages[path])
        ctype = "application/javascript" if url.endswith(".js") else "text/plain"
        return route.fulfill(status=200, headers={"Content-Type": ctype}, body="")

    def open(self, path="/chat/c1"):
        self.page.goto(ORIGIN + path)
        return self

    def release(self):
        self.page.wait_for_function("() => true")
        for _ in range(100):
            if self.held:
                break
            self.page.wait_for_timeout(50)
        r = self.held.pop()
        r.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=_sse())

    def ev(self, js, arg=None):
        return self.page.evaluate(js, arg)

    def close(self):
        self.page.close()


LIVE_WRAP = "document.querySelector('.turn-block[data-topic-key=\"tk1\"] .answer-wrap')"
# what the existing renderer produces for this answer with no animation at all
REFERENCE_JS = """(sel) => { const w = eval(sel);
  const ref = document.createElement('div'); ref.className = 'answer-wrap';
  ref.dataset.matchedResults = w.dataset.matchedResults;
  const a = document.createElement('div'); a.className = 'answer-raw'; a.textContent = w.querySelector('.answer-raw').textContent;
  const b = document.createElement('div'); b.className = 'answer-rendered';
  ref.append(a, b); document.body.appendChild(ref);
  if (typeof renderStructuredAnswer === 'function') renderStructuredAnswer(ref);
  else renderMarkdownAnswers({ querySelectorAll: (s) => (ref.matches(s) ? [ref] : []) });
  const html = b.innerHTML; ref.remove(); return html; }"""
STATE_JS = """(sel) => { const w = eval(sel); if (!w) return null; const r = w.querySelector('.answer-rendered');
  return { busy: w.getAttribute('aria-busy'), html: r.innerHTML, text: r.textContent,
           leftovers: r.querySelectorAll('[style*="opacity"], [style*="visibility"], [style*="transform"]').length,
           emptyStyle: r.querySelectorAll('[style=""]').length }; }"""


def _wait_live_wrap(pg):
    pg.page.wait_for_function(f"() => !!{LIVE_WRAP}", timeout=10000)


def _assert_final(pg):
    st = pg.ev(STATE_JS, LIVE_WRAP)
    assert st["busy"] is None and st["leftovers"] == 0 and st["emptyStyle"] == 0
    assert st["html"] == pg.ev(REFERENCE_JS, LIVE_WRAP)               # identical to a plain render
    assert "Post title 3" in st["text"] and "Remote roles are the main target" in st["text"]
    assert pg.ev(f"{LIVE_WRAP}.querySelectorAll('a[href*=\"reddit.com\"]').length") == 3
    assert pg.errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_live_answer_reveals_and_ends_identical(browser, name):
    pg = Page(browser, name).open()
    try:
        _wait_live_wrap(pg)
        mid = pg.ev(STATE_JS, LIVE_WRAP)
        assert mid["busy"] == "true"                                      # animation running
        hidden_rows = pg.ev(f"[...{LIVE_WRAP}.querySelectorAll('tbody > tr')].filter(r => r.style.visibility === 'hidden').length")
        assert hidden_rows > 0                                            # rows revealed later, layout kept
        assert pg.ev(f"getComputedStyle({LIVE_WRAP}.querySelector('thead') || document.body).visibility") == "visible"
        pg.page.wait_for_function(f"() => !{LIVE_WRAP}.hasAttribute('aria-busy')", timeout=8000)
        _assert_final(pg)
        assert pg.ev("document.getElementById('submit-btn') ? !document.getElementById('submit-btn').classList.contains('loading') : true")
    finally:
        pg.close()


@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("how", ["click", "Escape", "End"])
def test_browser_skip_reveals_everything_immediately(browser, name, how):
    pg = Page(browser, name).open()
    try:
        _wait_live_wrap(pg)
        assert pg.ev(STATE_JS, LIVE_WRAP)["busy"] == "true"
        if how == "click":
            pg.ev(f"{LIVE_WRAP}.click()")
        else:
            pg.page.keyboard.press(how)
        _assert_final(pg)                                                 # no waiting at all
    finally:
        pg.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_reduced_motion_and_hidden_tab_show_instantly(browser, name):
    pg = Page(browser, name, reduced=True).open()
    try:
        _wait_live_wrap(pg)
        _assert_final(pg)
    finally:
        pg.close()
    pg = Page(browser, name, hold=True).open()
    try:
        pg.ev("Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })")
        pg.release()
        _wait_live_wrap(pg)
        _assert_final(pg)
    finally:
        pg.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_saved_answers_never_animate(browser, name):
    pg = Page(browser, name).open("/chat/c2")
    try:
        sel = "document.querySelector('.turn-block[data-topic-key=\"saved\"] .answer-wrap')"
        st = pg.ev(STATE_JS, sel)
        assert st["busy"] is None and "Post title 3" in st["text"] and st["leftovers"] == 0
        assert st["html"] == pg.ev(REFERENCE_JS, sel)
        # soft navigation back to the same saved chat: also instant
        pg.ev("navigateTo('/chat/c2')")
        pg.page.wait_for_function(f"() => !!{sel} && {sel}.querySelector('.answer-rendered').textContent.length > 0")
        st = pg.ev(STATE_JS, sel)
        assert st["busy"] is None and st["leftovers"] == 0 and st["html"] == pg.ev(REFERENCE_JS, sel)
        assert pg.errors == []
    finally:
        pg.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_error_mid_animation_restores_full_answer(browser, name):
    pg = Page(browser, name).open()
    try:
        _wait_live_wrap(pg)
        assert pg.ev(STATE_JS, LIVE_WRAP)["busy"] == "true"
        # force every later animation frame to throw
        pg.ev("window.__realRect = Element.prototype.getBoundingClientRect;"
              "Element.prototype.getBoundingClientRect = function () { throw new Error('sabotage'); }; 0")
        pg.page.wait_for_function(f"() => !{LIVE_WRAP}.hasAttribute('aria-busy')", timeout=3000)
        pg.ev("Element.prototype.getBoundingClientRect = window.__realRect; 0")
        _assert_final(pg)
        assert any("[answer-reveal] animation error" in w for w in pg.warnings)
    finally:
        pg.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_soft_navigation_mid_animation_is_clean(browser, name):
    pg = Page(browser, name).open()
    try:
        _wait_live_wrap(pg)
        assert pg.ev(STATE_JS, LIVE_WRAP)["busy"] == "true"
        pg.ev("navigateTo('/chat/c2')")
        sel = "document.querySelector('.turn-block[data-topic-key=\"saved\"] .answer-wrap')"
        pg.page.wait_for_function(f"() => !!{sel}")
        pg.page.wait_for_timeout(300)
        assert pg.ev("activeAnswerReveal") is None                       # animation torn down
        assert pg.ev(STATE_JS, sel)["busy"] is None and pg.errors == []
    finally:
        pg.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_does_not_move_a_reader_who_scrolled_up(browser, name):
    pg = Page(browser, name, hold=True, earlier=6).open()
    try:
        pg.page.wait_for_timeout(1200)                      # let the page's own smooth scroll finish
        pg.ev("window.scrollTo(0, 0)")
        pg.page.wait_for_timeout(200)
        top = pg.ev("window.scrollY")
        assert pg.ev("document.documentElement.scrollHeight - (window.scrollY + window.innerHeight)") > 500
        pg.release()
        _wait_live_wrap(pg)
        assert pg.ev(STATE_JS, LIVE_WRAP)["busy"] == "true"
        ys = [pg.ev("window.scrollY")]
        for _ in range(8):
            pg.page.wait_for_timeout(150)
            ys.append(pg.ev("window.scrollY"))
        assert set(ys) == {top}
        pg.ev(f"{LIVE_WRAP}.click()")
        _assert_final(pg)
    finally:
        pg.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_follows_a_reader_at_the_bottom(browser, name):
    pg = Page(browser, name, hold=True, earlier=6).open()
    try:
        pg.page.wait_for_timeout(1200)
        pg.ev("window.scrollTo(0, document.documentElement.scrollHeight)")
        pg.page.wait_for_timeout(100)
        start = pg.ev("window.scrollY")
        pg.release()
        _wait_live_wrap(pg)
        pg.page.wait_for_function(f"() => !{LIVE_WRAP}.hasAttribute('aria-busy')", timeout=8000)
        assert pg.ev("window.scrollY") > start                           # followed the growing answer
        _assert_final(pg)
    finally:
        pg.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_scrolling_up_mid_animation_stops_following(browser, name):
    pg = Page(browser, name, hold=True, earlier=6).open()
    try:
        pg.page.wait_for_timeout(1200)
        pg.ev("window.scrollTo(0, document.documentElement.scrollHeight)")
        pg.page.wait_for_timeout(150)
        pg.release()
        _wait_live_wrap(pg)
        pg.page.mouse.move(500, 300)
        pg.page.mouse.wheel(0, -600)
        pg.page.wait_for_timeout(200)
        y = pg.ev("window.scrollY")
        pg.page.wait_for_function(f"() => !{LIVE_WRAP}.hasAttribute('aria-busy')", timeout=8000)
        assert pg.ev("window.scrollY") == y                              # reader's position left alone
        _assert_final(pg)
    finally:
        pg.close()
