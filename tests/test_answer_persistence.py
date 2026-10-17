"""
tests/test_answer_persistence.py — completed answers must come back when a
chat is reopened, and "Completed" must never sit above an empty answer.

Root cause covered: an LLM answer cut off before any text (max-output-token
truncation) was "repaired" into a bare {"format": ...} object, saved as the
answer, shown as "Completed", and rendered as an empty block (the frontend's
"anything visible?" check counted divider/table elements, not text).

No real LLM / Mongo / network: logics/index/routes are imported against stub
modules and a dict-backed fake chats collection; only _call_claude is faked.
"""
import copy
import datetime
import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOOD = {"format": "source_list", "headline": "Job seekers describe a slow market",
        "executive_summary": "People in Pakistan say AI roles exist but competition is intense.",
        "key_findings": [{"finding": "Remote roles are the target", "impact": "Most aim at US remote jobs.",
                          "supporting_post_indices": [1]}],
        "platforms": [{"platform": "reddit", "total_analyzed": 1, "shown_count": 1, "posts": [
            {"index": 1, "source": "pakistan", "title": "Hiring freeze", "summary": "s", "sentiment": "neutral",
             "link": "https://www.reddit.com/r/pakistan/comments/p1/"}]}]}
CUT_BEFORE_TEXT = ('{"format": "source_list", "searched": {"query": "AI jobs", "platforms": ["reddit"], '
                   '"time_window": "last 7 days"}, "headline": "Job seekers describe a sl')
CUT_MID_ANSWER = '{"format": "source_list", "headline": "Remote work dominates", "executive_summary": "People say open'


class FakeChats:
    def __init__(self):
        self.docs = {}

    def find_one(self, flt, projection=None):
        doc = self.docs.get((flt.get("chat_id"), flt.get("owner_key")))
        return copy.deepcopy(doc) if doc else None

    def update_one(self, flt, update, upsert=False):
        key = (flt.get("chat_id"), flt.get("owner_key"))
        doc = self.docs.setdefault(key, {"chat_id": key[0], "owner_key": key[1], "messages": []})
        for k, v in (update.get("$set") or {}).items():
            if k.startswith("messages.") and k.count(".") >= 2:        # messages.<idx>.<field>
                _, idx, field = k.split(".", 2)
                doc["messages"][int(idx)][field] = copy.deepcopy(v)
            else:
                doc[k] = copy.deepcopy(v)
        for k, v in (update.get("$push") or {}).items():
            doc.setdefault(k, []).append(copy.deepcopy(v))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-persist")
    db = types.ModuleType("database")
    db.__getattr__ = lambda name: MagicMock(name=f"database.{name}")
    fi = types.ModuleType("flintel")
    fi.ROUTER_UNFILTERED_ADDENDUM = ""
    fi.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM = ""
    fi.build_google_fallback_answer_context = None
    fi.format_google_stub_results = lambda docs: []
    fi.merge_matched_and_google_results = lambda matched, google, max_total=None, **k: list(matched)[:max_total]
    fi.build_mixed_evidence_note = lambda: "MIXED"
    fi.build_combined_source_context = lambda a, b: "COMBINED"
    fi.build_unfiltered_answer_context = lambda q, d: "UNFILTERED"
    hx = types.ModuleType("httpx")
    hx.AsyncClient = MagicMock()
    hx.TimeoutException = Exception
    hx.HTTPStatusError = Exception
    gg = types.ModuleType("google")
    gg.get_stub_results_for_keywords = lambda coll, kws: []
    plc = types.ModuleType("passlib.context")
    plc.CryptContext = MagicMock()
    aus = types.ModuleType("authlib.integrations.starlette_client")
    aus.OAuth = MagicMock()
    stubs = {"database": db, "flintel": fi, "httpx": hx, "google": gg,
             "website_intelligence": types.ModuleType("website_intelligence"),
             "passlib": types.ModuleType("passlib"), "passlib.context": plc,
             "authlib": types.ModuleType("authlib"), "authlib.integrations": types.ModuleType("authlib.integrations"),
             "authlib.integrations.starlette_client": aus, "scheduler": types.ModuleType("scheduler")}
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)
    for name in ("logics", "config", "index", "routes"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    lg = importlib.import_module("logics")
    idx = importlib.import_module("index")
    chats = FakeChats()
    monkeypatch.setattr(idx, "chats_collection", chats)
    monkeypatch.setattr(idx, "strict_intent_mode", lambda: False)
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: False)
    yield lg, idx, chats
    for name in ("logics", "config", "index", "routes"):
        sys.modules.pop(name, None)


# ── backend: what gets saved ─────────────────────────────────────────────────
def test_complete_answer_saved_unchanged(env):
    lg, _, _ = env
    assert json.loads(lg._extract_json_object_from_text(json.dumps(GOOD))) == GOOD


@pytest.mark.parametrize("raw", [
    CUT_BEFORE_TEXT,                                   # cut before any answer text
    '{"format": "source_list", "executive_summary": "',
    '{"format": "source_list"}',                       # complete but empty
    '```json\n{"format": "comparison", "subjects": []}\n```',
])
def test_answer_without_text_becomes_explicit_incomplete(env, raw):
    lg, _, _ = env
    out = json.loads(lg._extract_json_object_from_text(raw))
    assert out["answer_status"] == "incomplete" and out["format"] == "no_results"
    assert "cut off" in out["message"]


def test_cut_off_answer_keeps_its_text_and_is_marked_partial(env):
    lg, _, _ = env
    out = json.loads(lg._extract_json_object_from_text(CUT_MID_ANSWER))
    assert out["answer_status"] == "partial" and out["headline"] == "Remote work dominates"


@pytest.mark.parametrize("data,expected", [
    ({"format": "no_results", "message": "Nothing found."}, True),
    ({"format": "source_list", "platforms": [{"platform": "reddit"}]}, True),
    ({"format": "source_list", "searched": {"query": "x"}, "near_match_confidence": "low"}, False),
    ({"format": "trend_report", "trend": "  ", "headline": ""}, False),
    ("not a dict", False),
])
def test_answer_has_content(env, data, expected):
    lg, _, _ = env
    assert lg._answer_has_content(data) is expected


def test_prose_safety_net_unchanged(env):
    lg, _, _ = env
    out = json.loads(lg._extract_json_object_from_text("Plain prose with no JSON at all."))
    assert out["format"] == "no_results" and out["message"] == "Plain prose with no JSON at all."
    assert "answer_status" not in out


# ── lifecycle: generate -> persist -> reopen ─────────────────────────────────
NOW = datetime.datetime(2026, 10, 11, 10, 0, 0)
QUERIES = [("Find users looking for AI jobs in Pakistan", json.dumps(GOOD)),
           ("Now in USA", CUT_BEFORE_TEXT),
           ("Now in China", CUT_MID_ANSWER)]


def _model_content(answer):
    """The answer minus the fields answer_integrity.py adds next to the model's own."""
    out = {k: v for k, v in answer.items() if k not in ("counts", "excluded_posts", "verified_summary")}
    out["key_findings"] = [{k: v for k, v in f.items() if k in GOOD["key_findings"][0]} for f in out["key_findings"]]
    out["platforms"] = [dict(e, posts=[{k: v for k, v in p.items() if k in GOOD["platforms"][0]["posts"][0]}
                                       for p in e["posts"]]) for e in out["platforms"]]
    return out


def _run_session(lg, idx, chats, monkeypatch):
    """Three research turns through the REAL save path; only the LLM is faked."""
    outputs = []
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: outputs.pop(0))
    chats.update_one({"chat_id": "c1", "owner_key": "o1"}, {"$set": {"summary": ""}})
    post = [{"title": "Hiring freeze", "post_text": "We have a hiring freeze.",
             "post_url": "https://www.reddit.com/r/pakistan/comments/p1/", "platform": "reddit"}]
    for i, (q, llm_text) in enumerate(QUERIES):
        idx.add_search_to_chat("c1", "o1", q, lg.normalize_topic_key(q), ["kw"], "reddit")
        msg = chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"][-1]
        msg["requested_at"] = NOW
        outputs.append(llm_text)
        idx._complete_message_answer_and_results("c1", "o1", msg, post)
    return chats.find_one({"chat_id": "c1", "owner_key": "o1"})


def test_every_completed_request_has_a_persisted_answer(env, monkeypatch):
    lg, idx, chats = env
    doc = _run_session(lg, idx, chats, monkeypatch)
    msgs = doc["messages"]
    assert [m["query"] for m in msgs] == [q for q, _ in QUERIES]                     # original order
    answers = [json.loads(m["claude_answer"]) for m in msgs]
    assert _model_content(answers[0]) == GOOD                                        # normal answer intact
    assert answers[0]["counts"]["relevant"] == 1                                     # + server-side checks
    assert answers[1]["answer_status"] == "incomplete"                               # never an empty "answer"
    assert answers[2]["answer_status"] == "partial" and answers[2]["headline"] == "Remote work dominates"
    for a in answers:
        assert lg._answer_has_content(a)                                             # nothing blank is persisted


# ── reopen in a fresh page (real Chromium) ───────────────────────────────────
try:
    import jinja2
    from playwright.sync_api import sync_playwright
    HAVE_BROWSER = True
except Exception:          # pragma: no cover
    HAVE_BROWSER = False

ORIGIN = "http://flintel.test"
TEMPLATES = ("chat.html", "index.html")
STATE_JS = """() => [...document.querySelectorAll('.turn-block')].map(t => {
  const w = t.querySelector('.answer-wrap');
  const chip = t.querySelector('.turn-chip.status-completed, .turn-chip.status-pending, .turn-chip.status-failed');
  const vis = (e) => !!e && getComputedStyle(e).display !== 'none';
  const raw = w && w.querySelector('.answer-raw'), r = w && w.querySelector('.answer-rendered');
  const shown = w ? ((vis(r) ? r.innerText : '') + (vis(raw) ? raw.innerText : '')).trim() : '';
  return { q: t.querySelector('.turn-question').textContent.trim(), chip: chip ? chip.textContent.trim() : null,
           chipClass: chip ? [...chip.classList].filter(c => c.startsWith('status-')).join(' ') : null,
           shown, askAgain: !!t.querySelector('.answer-ask-again') }; })"""


def _render(name, chat, pending=None):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")),
                             undefined=jinja2.ChainableUndefined)
    env.globals["url_for"] = lambda *a, **k: "#"
    return env.get_template(name).render(request=None, chat=chat, chats=[], user=None,
                                         chat_id=(chat or {}).get("chat_id"), pending_stream_topic_key=pending,
                                         messages=(chat or {}).get("messages", []))


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


def _reopen(browser, name, chat, *, sse=None, pending=None):
    """Fresh page load of the persisted chat: chat.html directly, or the home page
    (index.html) then a sidebar soft-navigation into the chat."""
    pages = {"/chat/c1": _render("chat.html", chat, pending), "/": _render("index.html", None)}
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def handle(route):
        url = route.request.url
        path = url[len(ORIGIN):].split("?")[0] if url.startswith(ORIGIN) else None
        if path == "/chat/c1/stream" and sse is not None:
            return route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=sse)
        if path in pages:
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[path])
        return route.fulfill(status=200, body="")
    page.route("**/*", handle)
    if name == "chat.html":
        page.goto(ORIGIN + "/chat/c1")
    else:
        page.goto(ORIGIN + "/")
        page.evaluate("navigateTo('/chat/c1'); 0")
        page.wait_for_function("document.querySelectorAll('.turn-block').length > 0")
    page.wait_for_timeout(300)
    return page, errors


def _for_template(chat):
    out = copy.deepcopy(chat)
    for m in out["messages"]:
        m.setdefault("requested_at", NOW)
        m.setdefault("targeting_platform", "reddit")
    return out


@pytest.mark.parametrize("name", TEMPLATES)
def test_reopened_chat_restores_prompts_and_answers(browser, env, monkeypatch, name):
    lg, idx, chats = env
    doc = _for_template(_run_session(lg, idx, chats, monkeypatch))
    page, errors = _reopen(browser, name, doc)
    try:
        rows = page.evaluate(STATE_JS)
        assert [r["q"] for r in rows] == [q for q, _ in QUERIES]
        assert rows[0]["chip"] == "Completed" and "competition is intense" in rows[0]["shown"]
        assert rows[1]["chip"] == "Answer unavailable" and "didn't finish" in rows[1]["shown"] and rows[1]["askAgain"]
        assert rows[2]["chip"] == "Partial answer" and "Remote work dominates" in rows[2]["shown"]
        assert "cut off before it finished" in rows[2]["shown"]
        for r in rows:
            assert r["shown"]                                                        # never an empty answer
        page.locator(".turn-block").nth(1).locator(".answer-ask-again").click()
        assert page.evaluate("document.getElementById('search-input').value") == "Now in USA"
        assert errors == []
    finally:
        page.close()


LEGACY = {"chat_id": "c1", "title": "Old chat", "messages": [
    {"query": "Old good answer", "topic_key": "a", "results": [], "claude_answer": json.dumps(GOOD)},
    {"query": "Old blank answer", "topic_key": "b", "results": [],
     "claude_answer": json.dumps({"format": "source_list", "searched": {"query": "x"}})},
    {"query": "Old unknown fields", "topic_key": "c", "results": [],
     "claude_answer": json.dumps({"format": "source_list", "summary_text": "Kept under an unknown field."})},
    {"query": "Old no_results", "topic_key": "d", "results": [],
     "claude_answer": json.dumps({"format": "no_results", "message": "Nothing turned up."})},
    {"query": "Interrupted request", "topic_key": "e", "results": None, "claude_answer": None},
    {"query": "Old empty trend", "topic_key": "f", "results": [], "claude_answer": json.dumps({"format": "trend_report"})},
    {"query": "Old not_available", "topic_key": "g", "results": [],
     "claude_answer": json.dumps({"format": "not_available", "message": "That data isn't available."})},
    {"query": "Old actions-only no_results", "topic_key": "h", "results": [],
     "claude_answer": json.dumps({"format": "no_results", "suggested_actions": [{"type": "broaden_time", "label": "Extend to last 30 days"}]})},
]}


@pytest.mark.parametrize("name", TEMPLATES)
def test_existing_history_stays_compatible(browser, name):
    page, errors = _reopen(browser, name, _for_template(LEGACY))
    try:
        rows = page.evaluate(STATE_JS)
        assert [r["q"] for r in rows] == [m["query"] for m in LEGACY["messages"]]
        assert rows[0]["chip"] == "Completed" and "competition is intense" in rows[0]["shown"]
        assert rows[1]["chip"] == "Answer unavailable" and rows[1]["askAgain"]       # was: blank under "Completed"
        assert rows[2]["chip"] == "Completed" and "Kept under an unknown field." in rows[2]["shown"]  # text never hidden
        assert rows[3]["chip"] == "Completed" and rows[3]["shown"] == "Nothing turned up."
        assert rows[4]["chipClass"] == "status-pending" and rows[4]["chip"] != "Completed"   # interrupted: still pending, never "Completed"
        assert rows[5]["chip"] == "Answer unavailable"                               # labels-only skeleton is not an answer
        assert rows[6]["chip"] == "Completed" and rows[6]["shown"] == "That data isn't available."
        assert rows[7]["chip"] == "Completed" and "Extend to last 30 days" in rows[7]["shown"]
        assert errors == []
        # nothing re-renders in a loop
        n = page.evaluate("""() => new Promise(res => { let c = 0; const o = new MutationObserver(ms => { c += ms.filter(m => {
              const el = m.target.nodeType === 1 ? m.target : m.target.parentElement;
              return !(el && el.closest('.turn-chip'));                   // the pending timer ticks every second
            }).length; });
            o.observe(document.body, {childList: true, subtree: true, characterData: true});
            setTimeout(() => { o.disconnect(); res(c); }, 800); })""")
        assert n == 0
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_live_stream_incomplete_answer_is_not_shown_as_completed(browser, env, name):
    lg, _, _ = env
    envelope = lg._extract_json_object_from_text(CUT_BEFORE_TEXT)
    sse = "retry: 86400000\n\n" + f"data: {json.dumps({'delta': envelope})}\n\n" + \
          f"data: {json.dumps({'done': True, 'results': [], 'requested_at': '2026-10-11T10:00:00'})}\n\n"
    chat = _for_template({"chat_id": "c1", "messages": [
        {"query": "Now in USA", "topic_key": "live", "results": None, "claude_answer": None}]})
    page, errors = _reopen(browser, name, chat, sse=sse, pending="live")
    try:
        page.wait_for_function("!!document.querySelector('.turn-block .answer-wrap')")
        page.evaluate("document.querySelector('.turn-block .answer-wrap').click()")      # skip the reveal animation
        page.wait_for_function("document.querySelector('.turn-chip.status-failed') !== null")
        rows = page.evaluate(STATE_JS)
        assert rows[0]["chip"] == "Answer unavailable" and "didn't finish" in rows[0]["shown"]
        assert errors == []
    finally:
        page.close()


# ── "Ask this again" ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", TEMPLATES)
def test_ask_again_sends_the_original_query_once(browser, env, monkeypatch, name):
    """The button only fills the box (no auto-submit); one Enter = exactly one
    POST /search carrying the original question."""
    lg, idx, chats = env
    doc = _for_template(_run_session(lg, idx, chats, monkeypatch))
    posts = []
    pages = {"/chat/c1": _render("chat.html", doc), "/": _render("index.html", None)}
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def handle(route):
        req = route.request
        path = req.url[len(ORIGIN):].split("?")[0] if req.url.startswith(ORIGIN) else None
        if req.method == "POST" and path == "/search":
            posts.append(req.post_data or "")
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages["/chat/c1"])
        if path in pages:
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[path])
        return route.fulfill(status=200, body="")
    page.route("**/*", handle)
    try:
        if name == "chat.html":
            page.goto(ORIGIN + "/chat/c1")
        else:
            page.goto(ORIGIN + "/")
            page.evaluate("navigateTo('/chat/c1'); 0")
            page.wait_for_function("document.querySelectorAll('.answer-ask-again').length > 0")
        page.evaluate("document.querySelectorAll('[class*=welcome]').forEach(e => { if (!e.closest('.answer-wrap')) e.style.display = 'none'; }); 0")
        page.locator(".turn-block").nth(1).locator(".answer-ask-again").click()
        page.wait_for_timeout(300)
        assert posts == []                                                   # clicking alone never submits
        assert page.evaluate("document.getElementById('search-input').value") == "Now in USA"
        page.locator("#search-input").press("Enter")
        page.wait_for_timeout(800)
        assert len(posts) == 1                                               # one submit, no duplicate
        import re
        fields = dict(re.findall(r'name="([^"]+)"\r\n\r\n(.*?)\r\n', posts[0], re.S))   # multipart FormData
        assert fields.get("query") == "Now in USA" and fields.get("chat_id") == "c1"
        assert errors == []
    finally:
        page.close()


def test_ask_again_on_the_server_adds_one_turn_and_keeps_the_old_one(env, monkeypatch):
    """Through the real /search route: a re-ask appends ONE new pending message;
    the old 'incomplete' message is untouched, and the new answer is saved on
    the new message even though both share the same topic_key."""
    lg, idx, chats = env
    doc = _run_session(lg, idx, chats, monkeypatch)
    old = copy.deepcopy(doc["messages"])
    rt = sys.modules["routes"]
    jobs = []
    monkeypatch.setattr(rt, "get_owner", lambda request: ("o1", "guest"))
    monkeypatch.setattr(rt, "_is_owner_busy", lambda k: False)
    monkeypatch.setattr(rt, "_set_owner_busy", lambda k: None)
    monkeypatch.setattr(rt, "_clear_owner_busy", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(rt, "generate_chat_title", lambda q: "t")
    monkeypatch.setattr(rt, "enqueue_search_job", lambda tk, kws, plat: jobs.append(tk))
    router = {"intent": "search", "reply": None, "keywords": ["ai jobs usa"], "time_window_days": None,
              "match_phrases": ["I want an AI job in the USA"], "evidence_required": None, "website_only": False,
              "url_ask_type": "none", "target_entity": "job seekers", "intent_types": None,
              "resolved_query": "Find users looking for AI jobs in the USA."}
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps(router))

    class Req:
        session = {}
    resp = rt.search(Req(), query="Now in USA", platform="Reddit", chat_id="c1")
    assert resp.status_code == 303
    msgs = chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"]
    assert len(msgs) == len(old) + 1                                         # exactly one new turn
    assert msgs[:len(old)] == old                                            # history untouched
    assert msgs[-1]["query"] == "Now in USA" and msgs[-1]["claude_answer"] is None   # pending, not "Completed"
    assert msgs[-1]["topic_key"] == old[1]["topic_key"] == "now in usa"
    idx.save_claude_answer_to_chat("c1", "o1", "now in usa", json.dumps(GOOD))
    msgs = chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"]
    assert json.loads(msgs[-1]["claude_answer"]) == GOOD                     # lands on the new turn
    assert msgs[1] == old[1]                                                 # old incomplete turn unchanged



@pytest.mark.parametrize("name", TEMPLATES)
def test_reopen_through_the_real_view_chat_route(browser, env, monkeypatch, name):
    """The page HTML comes from the REAL GET /chat/{id} handler (view_chat:
    get_chat_session -> _fill_in_message_outputs -> TemplateResponse), not a
    hand-rendered template."""
    from fastapi import BackgroundTasks
    from starlette.requests import Request
    lg, idx, chats = env
    _run_session(lg, idx, chats, monkeypatch)
    rt = sys.modules["routes"]
    monkeypatch.setattr(rt, "get_owner", lambda request: ("o1", "guest"))
    monkeypatch.setattr(rt, "get_user_chats", lambda owner_key: [{"chat_id": "c1", "title": "Old chat"}])
    monkeypatch.setattr(rt, "get_current_user", lambda request: None)
    request = Request({"type": "http", "method": "GET", "path": "/chat/c1", "headers": [], "query_string": b"",
                       "session": {}, "app": idx.app})
    tasks = BackgroundTasks()
    resp = rt.view_chat(request, "c1", tasks)
    html = resp.body.decode("utf-8")
    assert tasks.tasks == []                                   # nothing re-generated on reopen
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    pages = {"/chat/c1": html, "/": _render("index.html", None)}

    def handle(route):
        url = route.request.url
        path = url[len(ORIGIN):].split("?")[0] if url.startswith(ORIGIN) else None
        if path in pages:
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[path])
        return route.fulfill(status=200, body="")
    page.route("**/*", handle)
    try:
        if name == "chat.html":
            page.goto(ORIGIN + "/chat/c1")
        else:
            page.goto(ORIGIN + "/")
            page.evaluate("navigateTo('/chat/c1'); 0")
            page.wait_for_function("document.querySelectorAll('.turn-block').length > 0")
        page.wait_for_timeout(300)
        rows = page.evaluate(STATE_JS)
        assert [r["q"] for r in rows] == [q for q, _ in QUERIES]
        assert [r["chip"] for r in rows] == ["Completed", "Answer unavailable", "Partial answer"]
        assert "competition is intense" in rows[0]["shown"] and "Remote work dominates" in rows[2]["shown"]
        assert errors == []
    finally:
        page.close()



# ── backend and frontend agree on "has content" ──────────────────────────────
import shutil as _shutil
import subprocess as _subprocess
PARITY_CASES = [
    GOOD, {"format": "source_list"}, {"format": "trend_report", "trend": " "},
    {"format": "no_results", "message": "x"}, {"format": "source_list", "searched": {"query": "q"}},
    {"format": "source_list", "website_context": {"url": "https://a.b", "summary": "site"}},
    {"format": "comparison", "subjects": [{"name": "", "platforms": []}]},
    {"format": "source_list", "platforms": [{"platform": "reddit"}]},
    {"format": "trend_report", "volume": 42},
]


@pytest.mark.skipif(not _shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("name", TEMPLATES)
def test_frontend_and_backend_content_rule_match(env, name):
    lg, _, _ = env
    src = (ROOT / "templates" / name).read_text(encoding="utf-8")
    a = src.index("  function answerHasText(data) {")
    b = src.index("  // ANSWER-STATE-HELPERS-END")
    code = src[a:b] + f"\nconsole.log(JSON.stringify({json.dumps(PARITY_CASES)}.map(answerHasText)));"
    out = _subprocess.run([_shutil.which("node"), "-e", code], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [lg._answer_has_content(c) for c in PARITY_CASES]
