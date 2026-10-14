"""
tests/test_reliability_ux_perf.py — regression tests for the four
"Final Flintel Reliability, UX & Performance" fixes:

  1. platform persistence: the platform a chat was sent with is saved on the
     chat (chats.selected_platform) and rendered back into chat.html's
     #platform-input, so Reddit stays Reddit after a new-chat submit, a
     follow-up, sidebar switches and back/forward; older chats without the
     field fall back to their last research message, then "All Platforms";
  2. composer: the temporary box shown while the first prompt is processed has
     the same element / size as the real composer and no "~2 credits" text;
  3. navigation: the sidebar renders from a light projection (no messages),
     HTML pages are gzip-compressed (Set-Cookie kept, SSE untouched), a click
     gives instant feedback, duplicate clicks are dropped and a stale
     response never overwrites the newer chat;
  4. small talk: answered before any LLM / search / job in new and existing
     chats, without touching another owner's chat.

The browser tests run the REAL FastAPI app (index.app + routes) in-process:
served by uvicorn on a local port (lifespan off: no scheduler).
Stub modules + a dict-backed chats collection, recording LLM stub — no real
LLM / Mongo / network. Timings measured here are local, in-process numbers.
"""
import asyncio
import copy
import json
import sys
import time

import pytest

from tests.test_answer_persistence import FakeChats, env  # noqa: F401  (fixture)

try:
    from playwright.sync_api import sync_playwright
    from starlette.testclient import TestClient
    HAVE_BROWSER = True
except Exception:          # pragma: no cover
    HAVE_BROWSER = False

ORIGIN = "http://flintel.test"
OWNER = "o1"


class ListChats(FakeChats):
    """FakeChats + the find().sort() / insert_one the sidebar and new chats use."""
    def __init__(self):
        super().__init__()
        self.find_calls = []

    def insert_one(self, doc):
        self.docs[(doc["chat_id"], doc["owner_key"])] = copy.deepcopy(doc)

    def find(self, flt, projection=None):
        self.find_calls.append((copy.deepcopy(flt), copy.deepcopy(projection)))
        rows = [copy.deepcopy(d) for (cid, owner), d in self.docs.items() if owner == flt.get("owner_key")]
        if projection:
            keep = [k for k, v in projection.items() if v and k != "_id"]
            rows = [{k: r[k] for k in keep if k in r} for r in rows]

        class Cur(list):
            def sort(self, key, direction):
                def k(r):
                    v = r.get(key)
                    return v.replace(tzinfo=None).timestamp() if hasattr(v, "timestamp") else 0
                return Cur(sorted(self, key=k, reverse=direction < 0))
        return Cur(rows)


def _research_router(q):
    return {"intent": "search", "reply": None, "keywords": ["crm"], "time_window_days": None,
            "match_phrases": [q], "evidence_required": None, "website_only": False, "url_ask_type": "none",
            "target_entity": "buyers", "intent_types": None, "resolved_query": q}


@pytest.fixture
def app_env(env, monkeypatch):
    lg, idx, _old = env
    rt = sys.modules["routes"] if "routes" in sys.modules else __import__("routes")
    chats = ListChats()
    monkeypatch.setattr(idx, "chats_collection", chats)
    state = {"calls": [], "jobs": [], "router_delay": 0.0}

    def fake_llm(system, user, max_tokens=None, enable_web_search=False, force_json_prefill=False):
        state["calls"].append(system[:40])
        if system == lg.CLAUDE_ROUTER_SYSTEM_PROMPT:
            time.sleep(state["router_delay"])
            return json.dumps(_research_router(user.rsplit("User's new message:", 1)[-1].strip()))
        return json.dumps({"format": "source_list", "headline": "h", "executive_summary": "s", "platforms": []})
    monkeypatch.setattr(lg, "_call_claude", fake_llm)
    for mod in (rt, idx):
        if hasattr(mod, "get_owner"):
            monkeypatch.setattr(mod, "get_owner", lambda request: (OWNER, "guest"))
        if hasattr(mod, "get_current_user"):
            monkeypatch.setattr(mod, "get_current_user", lambda request: None)
    monkeypatch.setattr(rt, "_is_owner_busy", lambda k: False)
    monkeypatch.setattr(rt, "_set_owner_busy", lambda k: None)
    monkeypatch.setattr(rt, "generate_chat_title", lambda q: q[:40])
    monkeypatch.setattr(rt, "enqueue_search_job", lambda *a, **k: state["jobs"].append(a))
    fl = sys.modules["flintel"]
    monkeypatch.setattr(fl, "should_trigger_immediately", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(fl, "generate_search_progress_content", lambda *a, **k: None, raising=False)
    return lg, idx, rt, chats, state


def _seed(chats, chat_id, title, platform_key=None, selected=None, owner=OWNER, minutes_ago=0):
    import datetime
    now = datetime.datetime.utcnow()
    msgs = []
    if platform_key:
        msgs.append({"query": f"Find buyers {title}", "topic_key": f"find buyers {title}".lower(), "keywords": ["crm"],
                     "targeting_platform": platform_key, "requested_at": now, "answered_at": now, "results": [],
                     "claude_answer": json.dumps({"format": "source_list", "headline": f"Answer {title}",
                                                  "executive_summary": f"Summary {title}", "platforms": []}),
                     "message_type": "search"})
    doc = {"chat_id": chat_id, "owner_key": owner, "owner_type": "guest", "title": title, "messages": msgs,
           "summary": "", "created_at": now, "updated_at": now - datetime.timedelta(minutes=minutes_ago)}
    if selected:
        doc["selected_platform"] = selected
    chats.docs[(chat_id, owner)] = doc


class _Req:
    def __init__(self):
        self.session = {}


# ── 1. platform persistence (server side) ────────────────────────────────────
@pytest.mark.parametrize("raw,shown", [
    ("reddit", "Reddit"), ("Reddit", "Reddit"), ("x_twitter,linkedin", "X / Twitter, LinkedIn"),
    ("X / Twitter, LinkedIn", "X / Twitter, LinkedIn"), ("all", "All Platforms"), ("All Platforms", "All Platforms"),
    (None, "All Platforms"), ("", "All Platforms"), ("<script>", "All Platforms"),
    ("Reddit, X / Twitter, LinkedIn, Facebook", "All Platforms"),
])
def test_platform_display_value(env, raw, shown):
    lg, _, _ = env
    assert lg.platform_display_value(raw) == shown


def test_research_submit_saves_and_renders_the_chosen_platform(app_env):
    lg, idx, rt, chats, state = app_env
    resp = rt.search(_Req(), query="Find people looking for a CRM", platform="Reddit", chat_id=None)
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    doc = chats.find_one({"chat_id": cid, "owner_key": OWNER})
    assert doc["selected_platform"] == "Reddit"
    assert doc["messages"][-1]["targeting_platform"] == "reddit"
    assert rt._chat_platform_display(doc) == "Reddit"
    # a follow-up with another selection updates it
    rt.search(_Req(), query="Find people looking for an ERP", platform="X / Twitter, LinkedIn", chat_id=cid)
    assert chats.find_one({"chat_id": cid, "owner_key": OWNER})["selected_platform"] == "X / Twitter, LinkedIn"


def test_older_chats_without_a_saved_platform_fall_back_safely(app_env):
    lg, idx, rt, chats, state = app_env
    _seed(chats, "old-reddit", "Old reddit", platform_key="reddit")
    _seed(chats, "old-all", "Old all", platform_key="all")
    _seed(chats, "empty", "Empty")
    _seed(chats, "bad", "Bad", selected="<img onerror=x>")
    disp = lambda c: rt._chat_platform_display(chats.find_one({"chat_id": c, "owner_key": OWNER}))
    assert disp("old-reddit") == "Reddit" and disp("old-all") == "All Platforms"
    assert disp("empty") == "All Platforms" and disp("bad") == "All Platforms"
    assert rt._chat_platform_display(None) == "All Platforms"


def test_small_talk_does_not_reset_the_chat_platform(app_env):
    lg, idx, rt, chats, state = app_env
    resp = rt.search(_Req(), query="Find people looking for a CRM", platform="Reddit", chat_id=None)
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    rt.search(_Req(), query="Thanks", platform="Reddit", chat_id=cid)
    assert chats.find_one({"chat_id": cid, "owner_key": OWNER})["selected_platform"] == "Reddit"


# ── 3. navigation (server side) ──────────────────────────────────────────────
def test_sidebar_list_uses_a_light_projection(app_env):
    lg, idx, rt, chats, state = app_env
    _seed(chats, "a", "A", platform_key="reddit", minutes_ago=5)
    _seed(chats, "b", "B", platform_key="all", minutes_ago=1)
    _seed(chats, "x", "Other owner", owner="someone-else")
    rows = idx.get_user_chat_list(OWNER)
    assert [r["chat_id"] for r in rows] == ["b", "a"]                       # newest first, owner-scoped
    assert all("messages" not in r for r in rows)
    flt, proj = chats.find_calls[-1]
    assert flt == {"owner_key": OWNER} and proj.get("messages") is None and proj["title"] == 1
    assert "get_user_chats(owner_key)" in open(rt.__file__).read()          # /chats JSON endpoint unchanged


def test_platform_save_and_chat_exists_are_owner_scoped(app_env):
    lg, idx, rt, chats, state = app_env
    _seed(chats, "theirs", "Theirs", owner="someone-else")
    assert idx.chat_exists("theirs", OWNER) is False and idx.chat_exists("theirs", "someone-else") is True
    assert idx.chat_exists(None, OWNER) is False
    idx.set_chat_platform("theirs", OWNER, "Reddit")                         # wrong owner: no-op
    assert "selected_platform" not in chats.find_one({"chat_id": "theirs", "owner_key": "someone-else"})


def _run_mw(idx, response, accept="gzip, deflate"):
    from starlette.requests import Request
    req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                   "headers": [(b"accept-encoding", accept.encode())]})

    async def call_next(_):
        return response
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(1) as ex:                  # own thread: never clashes with a running loop
        return ex.submit(asyncio.run, idx.gzip_html_pages(req, call_next)).result()


def test_html_pages_are_gzipped_with_every_cookie_kept(env):
    import gzip
    from starlette.responses import HTMLResponse
    lg, idx, _ = env
    body = "<html>" + "x" * 5000 + "</html>"
    r = HTMLResponse(body)
    r.set_cookie("session", "abc"); r.set_cookie("other", "def")
    out = _run_mw(idx, r)
    assert out.headers["content-encoding"] == "gzip" and "accept-encoding" in out.headers["vary"].lower()
    assert gzip.decompress(out.body).decode() == body
    assert int(out.headers["content-length"]) == len(out.body)
    cookies = [v for k, v in out.raw_headers if k == b"set-cookie"]
    assert len(cookies) == 2
    assert "content-encoding" not in _run_mw(idx, HTMLResponse(body), accept="identity").headers
    assert "content-encoding" not in _run_mw(idx, HTMLResponse("<p>tiny</p>")).headers


def test_event_streams_and_json_are_never_buffered_or_compressed(env):
    from starlette.responses import JSONResponse, StreamingResponse
    lg, idx, _ = env

    async def gen():
        yield b"data: 1\n\n"
    sse = StreamingResponse(gen(), media_type="text/event-stream")
    assert _run_mw(idx, sse) is sse
    js = JSONResponse({"a": "b" * 5000})
    assert _run_mw(idx, js) is js


def test_gzip_middleware_in_a_real_app_keeps_background_tasks_and_streams(env):
    if not HAVE_BROWSER:
        pytest.skip("starlette TestClient not installed")
    from fastapi import BackgroundTasks, FastAPI
    from starlette.responses import HTMLResponse, StreamingResponse
    lg, idx, _ = env
    ran = []
    mini = FastAPI()
    mini.middleware("http")(idx.gzip_html_pages)

    @mini.get("/page")
    def page(tasks: BackgroundTasks):
        tasks.add_task(lambda: ran.append("bg"))
        r = HTMLResponse("<html>" + "y" * 4000 + "</html>", background=tasks)
        r.set_cookie("session", "s1")
        return r

    @mini.get("/stream")
    def stream():
        def gen():
            yield "data: one\n\n"
            yield "data: two\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    c = TestClient(mini)
    r = c.get("/page", headers={"accept-encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and r.text.startswith("<html>yyy") and ran == ["bg"]
    assert r.cookies.get("session") == "s1"
    s = c.get("/stream", headers={"accept-encoding": "gzip"})
    assert "content-encoding" not in s.headers and s.text == "data: one\n\ndata: two\n\n"


# ── 4. small talk (server side) ──────────────────────────────────────────────
@pytest.mark.parametrize("text", ["Hello", "Hey", "Hi", "How are you?", "Thanks", "I am Hamza"])
def test_small_talk_new_and_existing_chat_has_no_llm_search_or_job(app_env, text):
    lg, idx, rt, chats, state = app_env
    _seed(chats, "mine", "Mine", platform_key="reddit")
    for chat_id in (None, "mine"):
        resp = rt.search(_Req(), query=text, platform="All Platforms", chat_id=chat_id)
        assert resp.status_code == 303
        cid = resp.headers["location"].rsplit("/", 1)[-1]
        if chat_id:
            assert cid == "mine"
        msg = chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]
        assert msg["message_type"] == "chat" and msg["claude_answer"]
    assert state["calls"] == [] and state["jobs"] == []


def test_small_talk_never_writes_into_another_owners_chat(app_env):
    lg, idx, rt, chats, state = app_env
    _seed(chats, "theirs", "Theirs", owner="someone-else")
    resp = rt.search(_Req(), query="Hello", platform="All Platforms", chat_id="theirs")
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    assert cid != "theirs"
    assert chats.find_one({"chat_id": "theirs", "owner_key": "someone-else"})["messages"] == []


@pytest.mark.parametrize("text", ["Hello, find people looking for a CRM", "Hi! who needs a dentist in Lahore?",
                                  "Thanks, now show me buyers for SEO services"])
def test_business_questions_are_not_small_talk(app_env, text):
    lg, idx, rt, chats, state = app_env
    rt.search(_Req(), query=text, platform="Reddit", chat_id=None)
    assert any(c == lg.CLAUDE_ROUTER_SYSTEM_PROMPT[:40] for c in state["calls"]) and len(state["jobs"]) == 1


# ── 2. composer markup (static) ──────────────────────────────────────────────
def test_temporary_dock_matches_the_real_composer_and_has_no_credit_label():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "templates"
    idx_html = (root / "index.html").read_text()
    chat_html = (root / "chat.html").read_text()
    dock = idx_html[idx_html.index('<div class="docked-input-wrap" id="docked-input-wrap" hidden>'):]
    dock = dock[:dock.index("</form>") if "</form>" in dock[:3000] else 3000]
    assert "~2 credits" not in dock and "docked-credit-estimate" not in dock
    assert '<textarea id="followup-input" class="search-input" rows="1"' in dock
    assert "margin-bottom: 16px" not in dock                                 # no inline size override
    assert 'value="{{ selected_platform or \'All Platforms\' }}"' in chat_html


# ── browser: the real app in Chromium ────────────────────────────────────────
@pytest.fixture(scope="module")
def browser():
    if not HAVE_BROWSER:
        pytest.skip("playwright / starlette TestClient not installed")
    try:
        p = sync_playwright().start()
        b = p.chromium.launch()
    except Exception as exc:   # pragma: no cover
        pytest.skip(f"chromium unavailable: {exc}")
    yield b
    b.close()
    p.stop()


@pytest.fixture
def server(app_env):
    """The real app on a real local HTTP server (uvicorn in a thread)."""
    uvicorn = pytest.importorskip("uvicorn")
    import socket
    import threading
    idx = app_env[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(idx.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(timeout=5)


def _open(browser, base, viewport=(1280, 860)):
    """A page talking to the real app; outside scripts (CDN) are answered empty."""
    page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
    page._errors, page._hits = [], []
    page.on("pageerror", lambda e: page._errors.append(str(e)))

    def handle(route):
        req = route.request
        if not req.url.startswith(base):
            return route.fulfill(status=200, body="")
        page._hits.append((req.method, req.url[len(base):].split("?")[0]))
        if "/stream" in req.url:
            return route.fulfill(status=204, body="")                      # no live SSE in tests
        return route.continue_()
    page.route("**/*", handle)
    return page


def _wait_chat(page, cid=None):
    cond = f"location.pathname.endsWith('{cid}')" if cid else "location.pathname.startsWith('/chat/')"
    page.wait_for_function(f"{cond} && !!document.querySelector('.thread-inner') && "
                           "!document.querySelector('main.main').classList.contains('is-navigating')", timeout=10000)


PLATFORM_JS = """() => ({input: document.getElementById('platform-input').value,
  label: document.getElementById('platforms-label').textContent.trim(),
  checked: [...document.querySelectorAll('#platforms-menu li[aria-selected=true]')].map(li => li.dataset.value)})"""


def test_browser_reddit_survives_submit_followup_switches_and_back_forward(browser, app_env, server):
    lg, idx, rt, chats, state = app_env
    _seed(chats, "other", "Other chat", platform_key="all", minutes_ago=30)
    page = _open(browser, server)
    try:
        page.goto(server + "/")
        page.evaluate("document.querySelectorAll('[class*=welcome]').forEach(e => e.remove()); 0")
        page.click("#platforms-btn")
        page.locator("#platforms-menu li[data-value='Reddit']").click()
        page.mouse.click(5, 5)
        page.fill("#search-input", "Find people looking for a CRM")
        page.locator("#search-input").press("Enter")
        _wait_chat(page)
        cid = page.evaluate("location.pathname").rsplit("/", 1)[-1]
        assert page.evaluate(PLATFORM_JS) == {"input": "Reddit", "label": "Reddit", "checked": ["Reddit"]}
        doc = chats.find_one({"chat_id": cid, "owner_key": OWNER})
        assert doc["selected_platform"] == "Reddit" and doc["messages"][-1]["targeting_platform"] == "reddit"

        # follow-up (small talk) in the same chat keeps it
        n = page.evaluate("document.querySelectorAll('.turn-block').length")
        page.fill("#search-input", "Thanks")
        page.locator("#search-input").press("Enter")
        page.wait_for_function("n => document.querySelectorAll('.turn-block').length > n", arg=n, timeout=10000)
        page.wait_for_timeout(200)
        assert page.evaluate(PLATFORM_JS)["input"] == "Reddit"

        # repeated sidebar switches
        for _ in range(2):
            page.locator(".session-item-link[href$='/chat/other']").click()
            _wait_chat(page, "other")
            assert page.evaluate(PLATFORM_JS)["input"] == "All Platforms"
            page.locator(f".session-item-link[href$='/chat/{cid}']").click()
            _wait_chat(page, cid)
            assert page.evaluate(PLATFORM_JS) == {"input": "Reddit", "label": "Reddit", "checked": ["Reddit"]}

        # back / forward
        page.go_back(); _wait_chat(page, "other")
        assert page.evaluate(PLATFORM_JS)["input"] == "All Platforms"
        page.go_forward(); _wait_chat(page, cid)
        assert page.evaluate(PLATFORM_JS)["input"] == "Reddit"
        # a full reload of the chat page (chat.html's own script) shows it too
        page.reload(); _wait_chat(page, cid)
        assert page.evaluate(PLATFORM_JS) == {"input": "Reddit", "label": "Reddit", "checked": ["Reddit"]}
        assert page._errors == []
    finally:
        page.close()


SAMPLER_JS = """() => { window.__boxes = [];
  const sample = () => { [...document.querySelectorAll('.search-box')].filter(b => b.offsetParent).forEach(b => {
      const i = b.querySelector('textarea, input.search-input');
      window.__boxes.push({h: Math.round(b.getBoundingClientRect().height), text: b.innerText,
                           tag: i ? i.tagName : null, path: location.pathname, dock: !!b.closest('.optimistic-dock')}); });
    if (window.__boxes.length < 4000) requestAnimationFrame(sample); };
  requestAnimationFrame(sample); return 0; }"""


@pytest.mark.parametrize("viewport", [(1280, 860), (390, 844)])
def test_browser_composer_is_stable_and_shows_no_credit_label_while_processing(browser, app_env, server, viewport):
    lg, idx, rt, chats, state = app_env
    state["router_delay"] = 0.8                       # keeps the first prompt "processing" for a while
    page = _open(browser, server, viewport)
    try:
        page.goto(server + "/")
        page.evaluate("document.querySelectorAll('[class*=welcome]').forEach(e => e.remove()); 0")
        page.fill("#search-input", "Find people looking for a CRM")
        before = page.evaluate("Math.round(document.querySelector('.search-box').getBoundingClientRect().height)")
        page.evaluate(SAMPLER_JS)
        page.locator("#search-input").press("Enter")
        _wait_chat(page)
        page.wait_for_timeout(300)
        boxes = page.evaluate("window.__boxes")
        during = [b for b in boxes if b["dock"]]                    # temporary box while processing
        after = [b for b in boxes if b["path"].startswith("/chat/")]
        assert during and after, "the temporary dock and the real composer were both sampled"
        assert not any("credit" in b["text"].lower() for b in boxes)
        assert {b["tag"] for b in boxes} == {"TEXTAREA"}
        heights = {b["h"] for b in during + after}
        assert len(heights) == 1, f"composer height changed: before={before} seen={sorted(heights)}"
        if viewport[0] > 800:
            assert heights == {before}
        assert page._errors == []
    finally:
        page.close()


def test_browser_navigation_is_instant_deduplicated_and_never_stale(browser, app_env, server):
    lg, idx, rt, chats, state = app_env
    for i in range(6):
        _seed(chats, f"c{i}", f"Chat {i}", platform_key="reddit" if i % 2 else "all", minutes_ago=i)
    page = _open(browser, server)
    try:
        page.goto(server + "/chat/c0")
        _wait_chat(page, "c0")
        # instant feedback: synchronously after the click
        fb = page.evaluate("""() => { document.querySelector(".session-item-link[href$='/chat/c3']").click();
            return {active: [...document.querySelectorAll('#sessions-list .session-item.active a')].map(a => a.getAttribute('href')),
                    dim: document.querySelector('main.main').classList.contains('is-navigating')}; }""")
        assert fb == {"active": ["/chat/c3"], "dim": True}
        _wait_chat(page, "c3")
        assert "Summary Chat 3" in page.inner_text("main.main")

        # double click -> one request
        page._hits.clear()
        page.evaluate("""() => { const l = document.querySelector(".session-item-link[href$='/chat/c4']"); l.click(); l.click(); }""")
        _wait_chat(page, "c4")
        page.wait_for_timeout(200)
        assert [h for h in page._hits if h == ("GET", "/chat/c4")] == [("GET", "/chat/c4")]

        # rapid A -> B: the slower, older response must not win
        page.evaluate("""() => { document.querySelector(".session-item-link[href$='/chat/c1']").click();
                                 document.querySelector(".session-item-link[href$='/chat/c2']").click(); }""")
        _wait_chat(page, "c2")
        page.wait_for_timeout(400)
        main = page.inner_text("main.main")
        assert "Summary Chat 2" in main and "Summary Chat 1" not in main
        assert page.evaluate(PLATFORM_JS)["input"] == "All Platforms"      # c2's platform, not c1's (Reddit)
        assert page.evaluate("[...document.querySelectorAll('#sessions-list .session-item.active a')].map(a => a.getAttribute('href'))") == ["/chat/c2"]

        # local timing: click -> destination content swapped in
        times = []
        for c in ("c5", "c1", "c3"):
            t0 = time.perf_counter()
            page.locator(f".session-item-link[href$='/chat/{c}']").click()
            _wait_chat(page, c)
            times.append(time.perf_counter() - t0)
        assert max(times) < 1.0, times

        # "New chat" double submit creates no duplicate request
        page._hits.clear()
        page.evaluate("""() => { const f = [...document.querySelectorAll('form')].find(f => (f.getAttribute('action') || '').endsWith('/chats/new'));
                                 f.requestSubmit(); f.requestSubmit(); }""")
        page.wait_for_function("location.pathname === '/'", timeout=10000)
        page.wait_for_timeout(200)
        assert [h for h in page._hits if h[1] == "/chats/new"] == [("POST", "/chats/new")]
        assert page._errors == []
    finally:
        page.close()


def test_browser_small_talk_answers_fast_without_llm(browser, app_env, server):
    lg, idx, rt, chats, state = app_env
    _seed(chats, "c0", "Chat 0", platform_key="reddit")
    page = _open(browser, server)
    try:
        page.goto(server + "/chat/c0")
        _wait_chat(page, "c0")
        for text in ("Hello", "How are you?"):
            n = page.evaluate("document.querySelectorAll('.turn-block').length")
            page.fill("#search-input", text)
            t0 = time.perf_counter()
            page.locator("#search-input").press("Enter")
            page.wait_for_function("n => { const t = document.querySelectorAll('.turn-block'); return t.length > n && "
                                   "t[t.length - 1].querySelector('.answer-wrap') && "
                                   "t[t.length - 1].querySelector('.answer-wrap').innerText.trim().length > 0; }",
                                   arg=n, timeout=10000)
            assert time.perf_counter() - t0 < 1.0
        # brand-new chat
        page.goto(server + "/")
        page.evaluate("document.querySelectorAll('[class*=welcome]').forEach(e => e.remove()); 0")
        page.fill("#search-input", "Hey")
        t0 = time.perf_counter()
        page.locator("#search-input").press("Enter")
        _wait_chat(page)
        page.wait_for_function("document.querySelector('.turn-block .answer-wrap') && "
                               "document.querySelector('.turn-block .answer-wrap').innerText.trim().length > 0", timeout=10000)
        assert time.perf_counter() - t0 < 1.0
        assert state["calls"] == [] and state["jobs"] == []
        assert page.evaluate(PLATFORM_JS)["input"] == "All Platforms"
        assert page._errors == []
    finally:
        page.close()


def test_browser_keyboard_submit_and_multiline_still_work(browser, app_env, server):
    lg, idx, rt, chats, state = app_env
    _seed(chats, "c0", "Chat 0", platform_key="reddit")
    page = _open(browser, server)
    try:
        page.goto(server + "/chat/c0")
        _wait_chat(page, "c0")
        box = page.locator("#search-input")
        h1 = page.evaluate("Math.round(document.querySelector('.search-box').getBoundingClientRect().height)")
        box.fill("first line")
        page._hits.clear()
        box.press("Shift+Enter")
        box.type("second line")
        assert page.evaluate("document.getElementById('search-input').value") == "first line\nsecond line"
        assert not [h for h in page._hits if h[1] == "/search"]                  # Shift+Enter did not send
        h2 = page.evaluate("Math.round(document.querySelector('.search-box').getBoundingClientRect().height)")
        assert h2 > h1                                                            # grows with the text
        n = page.evaluate("document.querySelectorAll('.turn-block').length")
        box.fill("Thanks")
        box.press("Enter")
        page.wait_for_function("n => document.querySelectorAll('.turn-block').length > n", arg=n, timeout=10000)
        assert [h for h in page._hits if h == ("POST", "/search")] == [("POST", "/search")]
        assert page._errors == []
    finally:
        page.close()
