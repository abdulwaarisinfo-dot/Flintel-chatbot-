"""
tests/test_no_next_steps.py — the "Try one of these next steps" section
(suggested actions, keyword chips, "Ask a related question") is gone from
no_results answers on every rendering path, while the answer's own text,
results, status chips and error states still show.

Paths covered in real Chromium: reopened chat (chat.html direct load and
index.html soft navigation), live streaming (SSE), the backend's safe-envelope
fallback, and an answer that carries suggested actions only. No real LLM /
Mongo / network.
"""
import json
import pathlib

import pytest

from tests.test_answer_persistence import (  # noqa: F401  (fixtures)
    GOOD, ORIGIN, STATE_JS, _for_template, _reopen, browser, env,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ("chat.html", "index.html")

NO_RESULTS = {
    "format": "no_results",
    "message": "This search didn't surface anyone looking to go to Australia in the last 7 days.",
    "likely_reason": "Small sample this week.",
    "suggested_actions": [
        {"type": "broaden_time", "label": "Extend to last 12 months"},
        {"type": "broaden_platforms", "label": "Include all public platforms + travel forums and news sections"},
        {"type": "broaden_term", "label": "Search broader/nearby terms", "suggestions": ["international holiday", "family holiday"]},
        {"type": "try_nearest_alternative", "label": "Search related intent signals", "suggestion": None},
    ],
    "related_questions": ["Who is planning a trip to New Zealand?"],
    "clarifying_question": "Do you want to focus on families or solo travellers?",
}
GONE = ("Try one of these next steps", "Extend to last 12 months", "Include all public platforms",
        "Search broader/nearby terms", "Search related intent signals", "See if there",
        "No close alternative found", "Try a broader or more general version", "Try these related searches",
        "Ask a related question", "New Zealand", "international holiday")
SECTION_DOM = "document.querySelectorAll('.keyword-chip, .keyword-chips').length"


def _assert_gone(text):
    for phrase in GONE:
        assert phrase not in text, phrase


# ── static ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", TEMPLATES)
def test_section_code_removed(name):
    s = (ROOT / "templates" / name).read_text(encoding="utf-8")
    for gone in ("heading.textContent = 'Try one of these next steps'", "function noResultsItem",
                 "function clickableTerm", "function suggestedSearchTerms", "NO_RESULTS_ACTION_EMOJI",
                 ".keyword-chip {", "--chip-bg:", "data.suggested_actions.forEach", "'Ask a related question'"):
        assert gone not in s, gone
    # shared link styles kept
    assert "--link-color: #009dff;" in s
    assert ".answer-rendered a { color: var(--link-color); text-decoration: underline; }" in s
    assert ".post-field .field-text a { color: var(--link-color);" in s


@pytest.mark.parametrize("name", TEMPLATES)
def test_green_non_link_accents_untouched(name):
    s = (ROOT / "templates" / name).read_text(encoding="utf-8")
    # Accent colour is now electric blue (#2563EB); the rules themselves are unchanged and no lime remains.
    assert "#a3e635" not in s.lower()
    for rule in (".btn-outline:hover { border-color: #2563EB; color: #2563EB; }",
                 ".auth-switch button { color: #2563EB; font-weight: 600; }",
                 ".add-btn:hover { color: #2563EB; }"):
        assert rule in s, rule


# ── reopened chats (chat.html and index.html soft-nav) ───────────────────────
def _chat(*answers):
    return _for_template({"chat_id": "c1", "title": "t", "messages": [
        {"query": f"q{i}", "topic_key": f"t{i}", "results": [], "claude_answer": a if isinstance(a, str) else json.dumps(a)}
        for i, a in enumerate(answers)]})


@pytest.mark.parametrize("name", TEMPLATES)
def test_reopened_no_results_has_no_section_but_keeps_its_text(browser, name):
    page, errors = _reopen(browser, name, _chat(NO_RESULTS, GOOD))
    try:
        rows = page.evaluate(STATE_JS)
        _assert_gone(rows[0]["shown"])
        assert NO_RESULTS["message"] in rows[0]["shown"]
        assert "Small sample this week." in rows[0]["shown"]
        assert NO_RESULTS["clarifying_question"] in rows[0]["shown"]
        assert rows[0]["chip"] == "Completed"
        assert "competition is intense" in rows[1]["shown"]                    # normal answer unchanged
        assert page.evaluate(SECTION_DOM) == 0
        assert errors == []
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_actions_only_answer_gets_short_honest_message(browser, name):
    only = {"format": "no_results", "suggested_actions": NO_RESULTS["suggested_actions"]}
    page, errors = _reopen(browser, name, _chat(only))
    try:
        row = page.evaluate(STATE_JS)[0]
        _assert_gone(row["shown"])
        assert "didn't surface enough relevant evidence" in row["shown"]
        assert "doesn't necessarily mean those discussions don't exist" in row["shown"]
        assert row["chip"] == "Completed" and not row["askAgain"]
        assert errors == []
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_fallback_message_not_added_when_answer_has_its_own_text(browser, name):
    page, errors = _reopen(browser, name, _chat({"format": "no_results", "likely_reason": "Only a reason."}))
    try:
        row = page.evaluate(STATE_JS)[0]
        assert "Only a reason." in row["shown"]
        assert "didn't surface enough relevant evidence" not in row["shown"]
        assert errors == []
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_backend_safe_envelope_fallback(browser, env, name):
    lg, _, _ = env
    envelope = lg._extract_json_object_from_text("this is not json at all")
    assert json.loads(envelope)["suggested_actions"]                            # backend still sends them
    page, errors = _reopen(browser, name, _chat(envelope))
    try:
        row = page.evaluate(STATE_JS)[0]
        _assert_gone(row["shown"])
        assert "Extend to last 30 days" not in row["shown"] and "Include all platforms" not in row["shown"]
        assert "this is not json at all" in row["shown"]                      # its message (error) still shows
        assert "unexpected formatting issue" in row["shown"]
        assert errors == []
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_unavailable_state_still_shows(browser, name):
    page, errors = _reopen(browser, name, _chat({"format": "source_list", "searched": {"query": "x"}}))
    try:
        row = page.evaluate(STATE_JS)[0]
        assert row["chip"] == "Answer unavailable" and row["askAgain"]
        assert errors == []
    finally:
        page.close()


# ── live streaming ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", TEMPLATES)
def test_streamed_no_results_has_no_section(browser, name):
    raw = json.dumps(NO_RESULTS)
    sse = "retry: 86400000\n\n" + "".join(f"data: {json.dumps({'delta': raw[i:i + 40]})}\n\n" for i in range(0, len(raw), 40)) + \
          f"data: {json.dumps({'done': True, 'results': [], 'requested_at': '2026-10-11T10:00:00'})}\n\n"
    chat = _for_template({"chat_id": "c1", "messages": [
        {"query": "Find people looking to go to Australia", "topic_key": "live", "results": None, "claude_answer": None}]})
    page, errors = _reopen(browser, name, chat, sse=sse, pending="live")
    try:
        page.wait_for_function("!!document.querySelector('.turn-block .answer-wrap .answer-rendered')")
        page.evaluate("document.querySelector('.turn-block .answer-wrap').click()")      # skip the reveal animation
        page.wait_for_function("!document.querySelector('.turn-block .answer-wrap').hasAttribute('aria-busy')")
        row = page.evaluate(STATE_JS)[0]
        _assert_gone(row["shown"])
        assert NO_RESULTS["message"] in row["shown"]
        assert page.evaluate(SECTION_DOM) == 0
        assert errors == []
    finally:
        page.close()
