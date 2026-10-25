"""
tests/test_partial_footer.py — the "Source posts" footer line of a stored report.

Data meaning (answer_integrity.enforce_answer_integrity, unchanged):
  shown_count     rows displayed for the platform (after the integrity step drops listed posts)
  total_analyzed  posts of that platform sent to analysis
  counts.listed   entries recovered from the model's reply; counts.excluded of them were dropped

For a COMPLETE answer the footer reads "Reddit · 4 of 22" (unchanged). For a PARTIAL answer the reply was cut
off, so posts it did not reach are missing from the table; the footer must say so and must not read as if those
posts had been rejected as irrelevant. Both templates share the code, so every case runs on both.

No network: the pages are rendered locally and every browser request is fulfilled locally.
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_answer_persistence import STATE_JS, TEMPLATES, _for_template, _reopen  # noqa: E402

CUT_OFF = " (answer cut off; posts it did not reach were not judged)"
FOOTER_JS = """() => [...document.querySelectorAll('.ap-source-counts')].map(e => e.innerText.trim())"""


def _post(n, **kw):
    return dict({"index": n, "source": "saas", "title": f"Post {n} title", "summary": f"Summary {n}.",
                 "link": f"https://www.reddit.com/r/saas/comments/p{n}/"}, **kw)


def _report(posts, shown=None, total=None, counts=None, status=None, platform="reddit"):
    entry = {"platform": platform, "posts": [_post(n) for n in range(1, posts + 1)]}
    if shown is not None:
        entry["shown_count"] = shown
    if total is not None:
        entry["total_analyzed"] = total
    answer = {"format": "source_list", "headline": "Buyers ask for monitoring tools",
              "executive_summary": "Some posts ask for a tool.", "platforms": [entry]}
    if counts is not None:
        answer["counts"] = counts
    if status is not None:
        answer["answer_status"] = status
    return answer


def _counts(listed, excluded, qualified, **extra):
    return dict({"listed": listed, "excluded": excluded,
                 "categories": {"qualified": qualified, "researching": 0, "unconfirmed": 0, "discussion": 0,
                                "competitor": 0, "unavailable": 0}}, **extra)


def _chat(answer):
    return _for_template({"chat_id": "c1", "title": "t", "messages": [
        {"query": "Find buyers", "topic_key": "a", "results": [], "claude_answer": json.dumps(answer)}]})


def _footer(browser, name, answer):
    """(footer line, turn state, page errors): the per-platform line of the stored report as the page shows it."""
    page, errors = _reopen(browser, name, _chat(answer))
    try:
        lines = page.evaluate(FOOTER_JS)
        state = page.evaluate(STATE_JS)
        assert len(state) == 1, state
        footer = [t for t in lines if t.startswith(("Reddit", "X (Twitter)"))]
        assert len(footer) <= 1, lines
        return (footer[0] if footer else None), state[0], errors
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_complete_answer_keeps_the_existing_footer(integrity_browser, name):
    answer = _report(4, shown=4, total=22, counts=_counts(listed=7, excluded=3, qualified=1))
    footer, state, errors = _footer(integrity_browser, name, answer)
    assert footer is None                       # the classification line is not shown for a complete answer
    assert state["chipClass"] == "status-completed" and state["chip"] != "Partial answer", state
    assert errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_explicit_complete_status_is_not_treated_as_partial(integrity_browser, name):
    answer = _report(4, shown=4, total=22, counts=_counts(listed=4, excluded=0, qualified=2), status="complete")
    footer, state, errors = _footer(integrity_browser, name, answer)
    assert footer is None
    assert state["chipClass"] == "status-completed" and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_partial_answer_says_the_answer_was_cut_off(integrity_browser, name):
    answer = _report(6, shown=6, total=20, counts=_counts(listed=6, excluded=0, qualified=1), status="partial")
    footer, state, errors = _footer(integrity_browser, name, answer)
    assert footer == f"Reddit · 6 of 20 analyzed posts shown{CUT_OFF}"
    # the unrecovered posts are not described as rejected, irrelevant or excluded
    assert not re.search(r"irrelevant|rejected|excluded|not relevant", footer.replace("were not judged", ""), re.I)
    # the existing chip contract is unchanged: label and class
    assert state["chip"] == "Partial answer" and state["chipClass"] == "status-failed", state
    assert state["askAgain"] is True and "cut off before it finished" in state["shown"]
    assert errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_partial_answer_with_excluded_posts_keeps_the_excluded_clause(integrity_browser, name):
    # 20 analyzed, the cut-off reply listed 12, the integrity step dropped 2 of them, 10 are shown
    answer = _report(10, shown=10, total=20, counts=_counts(listed=12, excluded=2, qualified=3), status="partial")
    footer, state, errors = _footer(integrity_browser, name, answer)
    assert footer == f"Reddit · 10 of 20 analyzed posts shown{CUT_OFF}"
    assert footer.count("cut off") == 1 and "qualified" not in footer and "excluded" not in footer
    assert state["chip"] == "Partial answer" and state["chipClass"] == "status-failed" and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_partial_answer_without_any_counts_adds_no_numbers_or_qualifier(integrity_browser, name):
    footer, state, errors = _footer(integrity_browser, name, _report(3, status="partial"))
    assert footer == "Reddit"                             # nothing to qualify: no counts were stored
    assert state["chip"] == "Partial answer" and state["chipClass"] == "status-failed" and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_complete_answer_without_any_counts_is_unchanged(integrity_browser, name):
    footer, state, errors = _footer(integrity_browser, name, _report(3))
    assert footer is None and state["chipClass"] == "status-completed" and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_partial_answer_with_platform_counts_but_no_totals(integrity_browser, name):
    answer = _report(3, shown=3, total=5, status="partial")          # no top-level counts object
    footer, _, errors = _footer(integrity_browser, name, answer)
    assert footer == f"Reddit · 3 of 5 analyzed posts shown{CUT_OFF}" and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_partial_answer_names_each_platform_but_qualifies_once(integrity_browser, name):
    answer = _report(2, shown=2, total=8, counts=_counts(listed=3, excluded=1, qualified=0), status="partial")
    answer["platforms"].append({"platform": "x", "shown_count": 1, "total_analyzed": 4, "posts": [
        _post(3, source="user", link="https://x.com/u/status/3")]})
    page, errors = _reopen(integrity_browser, name, _chat(answer))
    try:
        lines = page.evaluate(FOOTER_JS)
    finally:
        page.close()
    footer = next(t for t in lines if t.startswith("Reddit"))
    assert footer == ("Reddit · 2 of 8 analyzed posts shown | X (Twitter) · 1 of 4 analyzed posts shown"
                      + CUT_OFF)
    assert errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_footer_branch_is_driven_by_the_status_the_page_receives(name):
    """Static guard on the shared template code (the browser tests above prove the behaviour)."""
    src = (ROOT / "templates" / name).read_text(encoding="utf-8")
    start = src.index("function apSourcesTable(")
    block = src[start:src.index("container.appendChild(counts);", start)]
    assert "function apSourcesTable(platforms, container, heading, totals, partial, complete)" in block
    assert "analyzed posts shown" in block and "answer cut off; posts it did not reach were not judged" in block
    assert "qualified lead" not in block and "competitor/vendor" not in block      # no classification counts in the UI
    assert ("apSourcesTable(data.platforms, container, 'Source posts', data.counts, data.answer_status === 'partial',\n"
            "      data.answer_status == null || data.answer_status === 'complete')") in src       # complete = no status, or "complete"
    # the comparison view builds one footer per subject, through the same function and the same flag
    assert "apSourcesTable(subj.platforms, container, null, null, data.answer_status === 'partial')" in src


# ── comparison view: one footer per subject, built by the same function, with no totals ─────────────────────────────
def _comparison(counts=True, status=None):
    """Two subjects. As the integrity step stores them, total_analyzed is the analyzed pool for the platform (the same
    number under every subject) and shown_count is that subject's own shown rows."""
    def subject(name, rows, shown):
        entry = {"platform": "reddit", "posts": [_post(n) for n in rows]}
        if counts:
            entry.update(shown_count=shown, total_analyzed=5)
        return {"name": name, "platforms": [entry]}
    answer = {"format": "comparison", "headline": "A vs B", "executive_summary": "They differ.",
              "subjects": [subject("Tool A", [1, 2], 2), subject("Tool B", [3], 1)], "conclusion": "A wins."}
    if status is not None:
        answer["answer_status"] = status
    return answer


def _footers(browser, name, answer):
    page, errors = _reopen(browser, name, _chat(answer))
    try:
        lines = [t for t in page.evaluate(FOOTER_JS) if t.startswith("Reddit")]
        return lines, page.evaluate(STATE_JS)[0], errors
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_complete_comparison_keeps_the_existing_footers(integrity_browser, name):
    lines, state, errors = _footers(integrity_browser, name, _comparison())
    assert lines == []
    assert state["chipClass"] == "status-completed" and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_partial_comparison_qualifies_each_subject_footer(integrity_browser, name):
    lines, state, errors = _footers(integrity_browser, name, _comparison(status="partial"))
    assert lines == [f"Reddit · 2 of 5 analyzed posts shown{CUT_OFF}", f"Reddit · 1 of 5 analyzed posts shown{CUT_OFF}"]
    assert state["chip"] == "Partial answer" and state["chipClass"] == "status-failed" and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_partial_comparison_without_counts_adds_no_numbers_or_qualifier(integrity_browser, name):
    lines, state, errors = _footers(integrity_browser, name, _comparison(counts=False, status="partial"))
    assert lines == ["Reddit", "Reddit"]
    assert state["chip"] == "Partial answer" and errors == []


# --- excluded-posts note: a complete report whose stored counts confirm exclusions says so, without any counts ---------------
NOTE = "Some retrieved posts were left out of this report because they were duplicates or not relevant to your request."


def _lines(browser, name, answer):
    page, errors = _reopen(browser, name, _chat(answer))
    try:
        return page.evaluate(FOOTER_JS), errors
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_complete_answer_with_excluded_posts_shows_the_plain_note(integrity_browser, name):
    lines, errors = _lines(integrity_browser, name, _report(4, shown=4, total=22, counts=_counts(listed=7, excluded=3, qualified=1)))
    assert lines == [NOTE] and errors == []
    assert not re.search(r"\d|qualified|research|competitor|unconfirmed|discussion|unavailable", lines[0], re.I)   # no counts or categories


@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("answer", [
    _report(4, shown=4, total=22, counts=_counts(listed=4, excluded=0, qualified=2)),                    # zero exclusions
    _report(4, shown=4, total=22, counts=_counts(listed=4, excluded=0, qualified=2), status="complete"),  # explicit complete, zero
    _report(3),                                                                                         # no counts stored (older answer)
    _report(3, status="complete"),                                                                      # explicit complete, no counts
])
def test_no_note_when_nothing_is_confirmed_excluded(integrity_browser, name, answer):
    lines, errors = _lines(integrity_browser, name, answer)
    assert lines == [] and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("status", [None, "complete"])
def test_note_for_a_recognised_complete_status_with_positive_exclusions(integrity_browser, name, status):
    # production complete answers carry no answer_status at all; "complete" is accepted as the explicit spelling
    lines, errors = _lines(integrity_browser, name, _report(4, shown=4, total=22, counts=_counts(listed=7, excluded=3, qualified=1), status=status))
    assert lines == [NOTE] and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("status", ["incomplete", "limited", "something-new", ""])
def test_no_note_for_incomplete_limited_or_unknown_status(integrity_browser, name, status):
    lines, errors = _lines(integrity_browser, name, _report(4, shown=4, total=22, counts=_counts(listed=7, excluded=3, qualified=1), status=status))
    assert lines == [] and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_partial_answer_keeps_only_the_cut_off_notice_not_the_note(integrity_browser, name):
    lines, errors = _lines(integrity_browser, name, _report(10, shown=10, total=20, counts=_counts(listed=12, excluded=2, qualified=3), status="partial"))
    assert lines == [f"Reddit · 10 of 20 analyzed posts shown{CUT_OFF}"] and errors == []
