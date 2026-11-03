"""
tests/test_historical_only.py — HISTORICAL-ONLY SEARCH: GPT Live Web Search is removed from the user's search path, Active Intent and
its Historical window are not.

All mocked: no network, no OpenAI call, no production database. Uses the shared helpers of the existing Active Intent tests but
NEVER re-enables the source-level switch (reddit_live.LIVE_SEARCH_EXECUTION_ENABLED), so everything here runs exactly as production does.
What these tests cannot show: a real browser session, a real OpenAI response, or real Mongo/MySQL behaviour (see the other suites for those).
"""
import copy
import datetime
import socket
import time
from datetime import timedelta, timezone

import pytest

pytest.importorskip("mongomock")
import active_intent as ain  # noqa: E402
import intent_classifier as ic  # noqa: E402
import reddit_live as rl  # noqa: E402
import timerange  # noqa: E402
from tests.test_active_intent import OWNER, attach_classifier, live_env, prepare, stored_msg  # noqa: E402,F401
from tests.test_reddit_live import on  # noqa: E402,F401
from tests.test_answer_persistence import env  # noqa: E402,F401  (fixture: stubbed app modules)
from tests.test_scan_resilience import lg  # noqa: E402,F401  (fixture: the logics module, stubbed environment)
from tests.test_billing_lemonsqueezy import app, cfg, db, make_request  # noqa: E402,F401
from tests.test_active_intent_window import END  # noqa: E402,F401

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def _clean():
    yield
    ain.DEPS_FACTORY = None
    ic.DEPS_FACTORY = None


@pytest.fixture
def ai_only(monkeypatch):
    """Active Intent ON with a 1500-minute window; NO Live setting of any kind (the Live flags and caps stay unset)."""
    for k in ("REDDIT_LIVE_LINKS_ENABLED", "REDDIT_LIVE_DAILY_CAP_GLOBAL", "REDDIT_LIVE_DAILY_CAP_PER_OWNER", "REDDIT_LIVE_MAX_INFLIGHT"):
        monkeypatch.delattr(ain.config, k, raising=False)
    monkeypatch.setattr(ain.config, "OPENAI_API_KEY", "test-key-not-real", raising=False)     # the classifier's own requirement
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", True, raising=False)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", 1500, raising=False)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_CLASSIFIER_DAILY_CAP_GLOBAL", 1000, raising=False)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_CLASSIFIER_DAILY_CAP_PER_OWNER", 100, raising=False)
    return ain


def _env(intent="demand", **over):
    env = live_env(**over)
    attach_classifier(env, decide=lambda text: True)
    env.cls_override = ic_resp(intent)
    return env


def ic_resp(intent):
    from tests.test_active_intent import cls_resp
    return cls_resp(intent=intent)


def _live_must_not_run(monkeypatch):
    calls = []
    for name in ("process", "default_deps", "build_payload", "_http_post"):
        monkeypatch.setattr(rl, name, lambda *a, _n=name, **k: calls.append(_n) or (_ for _ in ()).throw(AssertionError(f"rl.{_n} was called")))
    return calls


# ── the switch ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_source_level_switch_is_off_in_this_module():
    assert rl.LIVE_SEARCH_EXECUTION_ENABLED is False


def test_live_is_inactive_even_with_every_live_setting_on(on):
    assert rl.is_active() is False                       # flag + caps + in-flight all valid, switch off


def test_process_does_nothing_even_with_every_live_setting_on(on):
    class Boom:
        def __getattr__(self, n):
            raise AssertionError("process touched deps: " + n)
    assert rl.process(Boom(), owner_key=OWNER, owner_type="email", chat_id="c1", request_id="r") == {"state": "disabled"}
    out = rl.process(Boom(), owner_key=OWNER, owner_type="email", chat_id="c1", request_id="r", pre_answer=True)
    assert out["state"] == "disabled" and out["reason"] == "live_search_removed"


# ── Active Intent works with no Live setting at all ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("intent", ["demand", "discussion"])
def test_active_intent_classifies_and_applies_the_1500_minute_window_without_live(ai_only, monkeypatch, intent):
    calls = _live_must_not_run(monkeypatch)
    env = _env(intent)
    msg = stored_msg(env)
    msg["requested_at"] = END.replace(tzinfo=None)
    tr, got, _why = ain.apply_window(msg, owner_key=OWNER, chat_id="c1", mode="stream")
    assert got == intent and len(env.cls_calls) == 1                       # classified once, by the existing classifier
    assert tr is not None and tr.end - tr.start == timedelta(minutes=1500) and tr.end == END
    hist = timerange.range_from_doc(msg["time_range"])                     # Historical receives the same half-open interval
    assert (hist.start, hist.end) == (tr.start, tr.end) and hist.kind == ain.WINDOW_KIND
    assert calls == []


def test_neither_is_not_a_window_and_nothing_is_applied(ai_only):
    env = _env("neither")
    msg = stored_msg(env)
    before = copy.deepcopy(msg)
    tr, got, _ = ain.apply_window(msg, owner_key=OWNER, chat_id="c1", mode="stream")
    assert tr is None and msg == before


@pytest.mark.parametrize("over", [{"time_range": {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-08T00:00:00+00:00", "tz": "UTC"}}, {"time_window_days": 7}])
def test_a_users_own_range_is_never_overwritten(ai_only, over):
    env = _env("demand", msg_over=over)
    msg = stored_msg(env)
    before = copy.deepcopy(msg)
    ain.apply_window(msg, owner_key=OWNER, chat_id="c1", mode="stream")
    assert msg == before


def test_guests_still_get_neither_a_classification_nor_a_window(ai_only):
    env = _env("demand")
    msg = stored_msg(env)
    tr, kind, reason = ain.apply_window(msg, owner_key="guest-uuid", chat_id="c1", mode="stream", owner_type="anon")
    assert tr is None and reason == "guest" and env.cls_calls == [] and msg.get("time_range") is None


def test_the_once_per_message_claim_still_holds(ai_only):
    env = _env("demand")
    msg = stored_msg(env)
    ain.apply_window(msg, owner_key=OWNER, chat_id="c1", mode="background_retrieval")
    ain.apply_window(msg, owner_key=OWNER, chat_id="c1", mode="stream")
    assert len(env.cls_calls) == 1


def test_disabled_active_intent_still_does_nothing(ai_only, monkeypatch):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", False, raising=False)
    env = _env("demand")
    assert ain.apply_window(stored_msg(env), owner_key=OWNER, chat_id="c1", mode="stream")[0] is None and env.cls_calls == []


# ── the Run lifecycle with Live removed ──────────────────────────────────────────────────────────────────────────────────
def test_run_never_starts_a_live_thread_and_finish_returns_at_once_with_the_historical_answer(ai_only, monkeypatch):
    calls = _live_must_not_run(monkeypatch)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_LIVE_WAIT_SECONDS", 30, raising=False)
    env = _env("demand")
    run = prepare(env)
    assert run.active is True
    matched = [{"post_url": "https://www.reddit.com/r/x/comments/abc123/t/"}]
    assert run.historical(matched) is matched                                 # the saved posts, untouched
    assert run._thread is None and run._done.is_set() and run.live_result["state"] == "disabled"
    answer = '{"format": "source_list", "executive_summary": "Saved posts.", "platforms": []}'
    t0 = time.monotonic()
    assert run.finish(answer, historical=matched) == answer                  # exactly the historical answer, no live_candidates
    assert time.monotonic() - t0 < 2 and calls == []


def test_finish_without_a_prior_start_also_does_not_wait_or_call_live(ai_only, monkeypatch):
    calls = _live_must_not_run(monkeypatch)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_LIVE_WAIT_SECONDS", 30, raising=False)
    run = prepare(_env("discussion"))
    t0 = time.monotonic()
    out = run.finish("ANSWER", historical=[], historical_timed_out=False)
    assert out == "ANSWER" and time.monotonic() - t0 < 2 and calls == [] and run._thread is None


def test_empty_historical_results_do_not_fall_back_to_live(ai_only, monkeypatch):
    calls = _live_must_not_run(monkeypatch)
    run = prepare(_env("demand"))
    assert run.historical([]) == []
    assert run.finish("NO RESULTS ANSWER", historical=[]) == "NO RESULTS ANSWER" and calls == []


def test_a_live_candidates_block_is_never_added(ai_only):
    run = prepare(_env("demand"))
    out = run.finish('{"format": "source_list", "platforms": []}', historical=[])
    assert "live_candidates" not in out


# ── the endpoint and the frontend ────────────────────────────────────────────────────────────────────────────────────────
def test_the_live_links_endpoint_never_reaches_the_live_function(app, on, monkeypatch):
    calls = _live_must_not_run(monkeypatch)
    monkeypatch.setattr(app.rt, "get_owner", lambda request: (OWNER, "email"))
    req = make_request(app.idx, "/chat/c1/live-links", headers={"x-flintel-live-links": "1"})
    resp = app.rt.live_links(req, chat_id="c1", request_id="r0", topic_key=None)
    import json
    assert resp.status_code == 200 and json.loads(resp.body.decode()) == {"state": "disabled"} and calls == []


def test_the_template_global_is_off_so_the_page_never_requests_live_links(app, on):
    assert app.rt.templates.env.globals["live_links_enabled"]() is False      # chat.html then renders LIVE_LINKS_ON = false


def test_no_source_file_other_than_the_two_live_modules_calls_a_web_search_model_for_a_search():
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parent.parent
    hits = {}
    for p in root.glob("*.py"):
        if p.name in ("reddit_live.py",):
            continue
        n = len(re.findall(r"reddit_live\.process\(", p.read_text(encoding="utf-8", errors="ignore")))
        if n:
            hits[p.name] = n
    assert hits == {"active_intent.py": 1, "routes.py": 1}    # the only callers; both reach a function that is switched off


# ── (HARDENING) the Router prompt and every other user-facing claim of web access ───────────────────────────────────────────────────
import pathlib  # noqa: E402
import re  # noqa: E402
ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_no_prompt_claims_the_user_request_model_has_a_web_search_tool(lg):
    for name in ("CLAUDE_ROUTER_SYSTEM_PROMPT", "CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT"):
        text = getattr(lg, name)
        assert "web_search" not in text, name
        assert not re.search(r"(?i)(access to|use|using|search the|with) (a |the )?(live )?web[ _]?search", text), name
        assert "real-time access" not in text, name
    assert "do not have web search" in lg.CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT


def test_the_router_call_passes_enable_web_search_false(lg, monkeypatch):
    seen = {}

    def fake(system_prompt, user_message, max_tokens=None, enable_web_search=False, force_json_prefill=False):
        seen["web"] = enable_web_search
        seen["system"] = system_prompt
        return '{"intent": "search", "keywords": ["crm"], "time_window_days": null, "reply": null}'
    monkeypatch.setattr(lg, "_call_claude", fake)
    lg.classify_and_maybe_chat("find people looking for a crm", "", None, None)
    assert seen["web"] is False and seen["system"] is lg.CLAUDE_ROUTER_SYSTEM_PROMPT


def _py_sources():
    for p in sorted(ROOT.glob("*.py")):
        yield p.name, p.read_text(encoding="utf-8", errors="ignore")


def test_web_search_tool_declarations_and_enablers_are_exactly_the_known_ones():
    import ast
    declares = {n for n, t in _py_sources() if re.search(r"\{\s*\"type\":\s*\"web_search\"", t)}
    assert declares == {"logics.py", "reddit_live.py"}      # logics: the gated _call_claude option; reddit_live: the removed Live search
    enabling = []                                           # real calls only (comments and docstrings are not Call nodes)
    for n, t in _py_sources():
        for node in ast.walk(ast.parse(t)):
            if isinstance(node, ast.Call) and any(k.arg == "enable_web_search" and isinstance(k.value, ast.Constant) and k.value.value is True
                                                  for k in node.keywords):
                enabling.append((n, node.lineno))
    assert len(enabling) == 1 and enabling[0][0] == "logics.py"
    src = (ROOT / "logics.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(src) if l.startswith("def generate_trending_keywords_via_web_search"))
    end = next(i for i in range(start + 1, len(src)) if src[i].startswith("def ") or src[i].startswith("# ───"))
    assert start < enabling[0][1] <= end                    # the ONE call is inside the trending-keywords helper
    calls = {n for n, t in _py_sources() if "generate_trending_keywords_via_web_search" in t and n != "logics.py"}
    assert calls == {"scheduler.py"}                         # a background job, never a request path


# ── saved Live content is shown as saved ─────────────────────────────────────────────────────────────────────────────────────────
def test_the_persisted_post_answer_section_is_labelled_as_saved_not_live(on):
    from tests.test_reddit_live_endpoint import render, _live_section, _ok_record
    from tests.test_reddit_live import URL1
    sec = _live_section(render(_ok_record([{"url": URL1, "title": "An old saved post"}])))
    assert sec and "An old saved post" in sec and "Previously saved links" in sec and "no longer runs" in sec
    assert "Live web-search links" not in sec and "Live Reddit" not in sec


def test_a_new_conversation_without_saved_live_data_has_no_live_section(on):
    from tests.test_reddit_live_endpoint import render, _live_section
    html = render()
    assert _live_section(html) is None and "const LIVE_LINKS_ON = false;" in html      # no section, and the page never asks for one
    assert "Previously saved links" not in html.split("<script")[0]


def test_chat_page_ships_no_live_label_in_its_markup_or_renderers():
    for t in ("chat.html", "index.html"):
        text = (ROOT / "templates" / t).read_text(encoding="utf-8")
        assert "'Live Reddit discussions'" not in text and "'Live Reddit results'" not in text and "live Reddit '" not in text, t
