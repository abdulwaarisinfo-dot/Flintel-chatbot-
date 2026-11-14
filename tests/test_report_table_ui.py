"""Issue 3 (table structure), Issue 5 (coverage panel) and the omitted-post lists in the real page (both templates, real Chromium, network blocked).
Answers are built by the real server-side functions from synthetic records."""
import json
from datetime import datetime, timezone

import pytest

import answer_integrity as ai
import timerange as tr
from tests.test_answer_persistence import TEMPLATES, _for_template, _reopen, env  # noqa: F401

HN = "https://news.ycombinator.com/item?id={}"
Q = "Find people looking for AI development and automation services in the last 30 days"
REF = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def P(i, title, text, platform="hackernews", **extra):
    return dict({"title": title, "post_text": text, "post_url": HN.format(i), "platform": platform, "author": f"u{i}"}, **extra)


A = P(1, "Ask HN: Who can build AI agents for our ops team?", "We are looking for an AI development agency to automate our operations. Budget is $8k a month. Any recommendations?",
      published_at="2026-11-02T09:00:00+00:00")
B = P(2, "Looking for automation help for our logistics workflows", "Our team needs an automation partner to build AI workflows for logistics. Can anyone recommend a vendor?")
C = P(3, "Show HN: Flowbot, AI automation platform", "I built Flowbot, an AI automation SaaS. Free trial, feedback welcome.")
D = P(4, "Pizza dough hydration", "How much water should I use for 00 flour pizza dough at home?")


def answer(lg, listed, pool, exact_for=None, summary_for=None):
    posts = []
    for i, p in enumerate(listed):
        e = {"index": i + 1, "link": p["post_url"], "title": p["title"], "role": "buyer", "purchase_intent": "explicit"}
        if exact_for and p is exact_for:
            e["evidence"] = "budget is $8k a month"
        elif p is not exact_for and exact_for:
            e["evidence"] = "a sentence that is not in the post at all"
        if summary_for and p is summary_for:
            e["summary"] = "The author wants an agency to automate operations."
        posts.append(e)
    data = {"format": "source_list", "executive_summary": "Found posts.", "key_findings": [], "platforms": [{"platform": "hackernews", "posts": posts}]}
    out = ai.enforce_answer_integrity(data, pool, pool, Q)
    rng = tr.parse_time_range("last 30 days", REF, "UTC")
    msg = {"query": Q, "requested_at": REF, "topic_key": "t", "targeting_platform": "all", "time_range": rng.to_doc(REF)}
    out["search_coverage"] = lg.build_search_coverage(msg, out, {"mode": "combined", "live": "ok", "historical": "failed", "stats": {"duplicates_removed": 2}}, pool, "c1")
    return out, ai.annotate_results(pool, json.dumps(out))


@pytest.fixture
def built(env):
    global lg
    lg = env[0]
    import platforms
    platforms.apply_flags(hackernews=True)
    out, results = answer(lg, [A, B, C, D], [A, B, C, D, P(5, "A retrieved buyer the writer skipped", "We need an AI automation vendor for our warehouse. Please recommend one.")],
                          exact_for=A, summary_for=A)
    yield out, results
    platforms.apply_flags(hackernews=False)


def open_page(browser, name, out, results, width=1200):
    chat = {"chat_id": "c1", "title": "t", "messages": [{"query": Q, "topic_key": "q", "results": results, "claude_answer": json.dumps(out)}]}
    page, errors = _reopen(browser, name, _for_template(chat))
    page.set_viewport_size({"width": width, "height": 900})
    return page, errors


HEADERS = ["#", "Source", "Post", "Evidence", "Assessment", "Source check", "Posted"]
flat = lambda t: " ".join(t.split())


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_row_has_the_same_cells_under_the_right_headings(integrity_browser, built, name):
    out, results = built
    page, errors = open_page(integrity_browser, name, out, results)
    try:
        heads = [h.strip() for h in page.locator(".answer-rendered table.ap-sources-table th").all_inner_texts()]
        assert heads == HEADERS
        rows = page.evaluate("""() => [...document.querySelectorAll('.ap-sources-table tbody tr')].map(tr => [...tr.children].map(td => [td.getAttribute('data-label'), td.innerText.trim()]))""")
        assert len(rows) >= 2
        for r in rows:
            assert [c[0] for c in r] == HEADERS                                                   # same schema, each cell labelled by its heading
        for r in rows:
            assess, check = flat(r[4][1]), flat(r[5][1])
            for label in ("Intent", "Role", "Purchase", "Fit"):
                assert label in assess
            for label in ("Source", "Analysis", "Relevance"):
                assert label in check
        assert errors == []
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_evidence_kinds_and_the_ai_summary_are_labelled_apart(integrity_browser, built, name):
    out, results = built
    page, errors = open_page(integrity_browser, name, out, results)
    try:
        by_title = {r[2]: flat(r[3]) for r in page.evaluate("""() => [...document.querySelectorAll('.ap-sources-table tbody tr')].map(tr => [...tr.children].map(td => td.innerText.trim()))""")}
        a = by_title[A["title"]]
        assert "Exact quote" in a and "verified in the original post" in a and "“Budget is $8k a month”" in a
        assert "AI summary The author wants an agency" in a                                       # the summary is a separate labelled line, not a quote
        b = by_title[B["title"]]
        assert "Excerpt from the original post" in b and "not a model quote" in b and "Exact quote" not in b
        assert "a sentence that is not in the post at all" not in b                              # the fabricated quotation is not shown anywhere
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_assessment_separates_each_dimension_and_marks_the_reasoning_as_an_inference(integrity_browser, built, name):
    out, results = built
    page, errors = open_page(integrity_browser, name, out, results)
    try:
        cells = page.evaluate("""() => [...document.querySelectorAll('.ap-sources-table tbody tr')].map(tr => tr.children[4].innerText)""")
        a = flat(cells[0])
        assert "Intent" in a and "Purchase" in a and "Why (inference)" in a
        assert "Purchase: " not in a and " · Fit:" not in a                                   # no longer one crowded line
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_on_a_phone_the_table_stacks_with_labels_and_does_not_overflow(integrity_browser, built, name):
    out, results = built
    page, errors = open_page(integrity_browser, name, out, results, width=375)
    try:
        assert page.evaluate("document.documentElement.scrollWidth") <= 376
        assert page.evaluate("getComputedStyle(document.querySelector('.ap-sources-table thead')).display") == "none"
        content = page.evaluate("getComputedStyle(document.querySelector('.ap-sources-table tbody td:nth-child(4)'), '::before').content")
        assert "Evidence" in content
        assert page.evaluate("getComputedStyle(document.querySelector('.ap-sources-table tbody tr')).display") == "block"
        wrap = page.evaluate("(() => { const w = document.querySelector('.ap-table-wrap'); return [w.scrollWidth, w.clientWidth]; })()")
        assert wrap[0] <= wrap[1] + 1
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_a_row_with_missing_fields_keeps_the_schema_and_states_what_is_missing(integrity_browser, name):
    out = {"format": "source_list", "executive_summary": "x", "key_findings": [],
           "platforms": [{"platform": "hackernews", "total_analyzed": 1, "shown_count": 1, "posts": [
               {"index": 1, "link": HN.format(9), "title": "Bare row", "platform": "hackernews", "verification_status": "unverified"}]}],
           "counts": {"retrieved": 1, "shown": 1, "excluded": 0}}
    page, errors = open_page(integrity_browser, name, out, [])
    try:
        rows = page.evaluate("""() => [...document.querySelectorAll('.ap-sources-table tbody tr')].map(tr => [...tr.children].map(td => td.innerText.trim()))""")
        heads = [h.strip() for h in page.locator(".answer-rendered table.ap-sources-table th").all_inner_texts()]
        assert heads == ["#", "Source", "Post", "Summary"] and len(rows) == 1 and len(rows[0]) == len(heads)      # no intent data: the plain schema, every cell present
        text = flat(page.inner_text(".ap-sources-table"))
        assert "Hacker News" in text and errors == []
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_coverage_panel_states_mode_period_sources_counts_and_completeness(integrity_browser, built, name):
    out, results = built
    page, errors = open_page(integrity_browser, name, out, results)
    try:
        panel = flat(page.inner_text(".search-coverage"))
        assert "Mode Combined" in panel
        assert "Period requested" in panel and "last 30 days" in panel and "Period applied" in panel and "as requested" in panel
        assert "Hacker News Searched" in panel and "stories only" in panel
        assert "Reddit Failed" in panel                                                            # the stored read errored: shown as failed, not unavailable
        assert "Retrieved 5" in panel and "Duplicates" in panel and "Excluded" in panel and "Relevant" in panel and "Displayed" in panel
        assert "Retrieval Partial" in panel and "Reddit failed" in panel
        assert errors == []
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_coverage_panel_warns_when_the_period_applied_is_shorter_than_requested(integrity_browser, built, name):
    out, results = built
    import active_intent
    win = active_intent.recent_window(REF, 1500)
    msg = {"query": Q, "requested_at": REF, "topic_key": "t", "targeting_platform": "all", "time_range": win.to_doc(REF)}
    out["search_coverage"] = lg.build_search_coverage(msg, out, {"mode": "combined", "live": "ok", "historical": "ok", "stats": {}}, [A, B], "c1")
    page, errors = open_page(integrity_browser, name, out, results)
    try:
        panel = flat(page.inner_text(".search-coverage"))
        assert "Warning" in panel and "NOT fully applied" in panel and "automatic recent window" in panel
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_not_listed_and_excluded_posts_are_visible_with_links(integrity_browser, built, name):
    out, results = built
    page, errors = open_page(integrity_browser, name, out, results)
    try:
        page.evaluate("document.querySelectorAll('.ap-omitted').forEach(d => d.open = true)")
        omitted = page.evaluate("""() => [...document.querySelectorAll('.ap-omitted')].map(d => [d.querySelector('summary').innerText, [...d.querySelectorAll('li')].map(li => li.innerText), [...d.querySelectorAll('a')].map(a => a.href)])""")
        titles = {o[0].split(" (")[0]: o for o in omitted}
        assert "Retrieved but not listed in the report" in titles and "Excluded from the report" in titles
        nl = titles["Retrieved but not listed in the report"]
        assert any("A retrieved buyer the writer skipped" in li for li in nl[1]) and all(h.startswith("https://") for h in nl[2])
        ex = titles["Excluded from the report"]
        assert any("Pizza dough hydration" in li and "not relevant" in li for li in ex[1])
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_malformed_rows_from_the_writer_do_not_break_the_page_or_misalign_the_table(integrity_browser, name, monkeypatch):
    import jinja2
    import tests.test_answer_persistence as tap

    def escaping_render(tpl, chat, pending=None):          # the real app autoescapes its templates; the shared test renderer does not
        e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(tap.ROOT / "templates")), undefined=jinja2.ChainableUndefined, autoescape=True)
        e.globals["url_for"] = lambda *a, **k: "#"
        return e.get_template(tpl).render(request=None, chat=chat, chats=[], user=None, chat_id=(chat or {}).get("chat_id"),
                                          pending_stream_topic_key=pending, messages=(chat or {}).get("messages", []))
    monkeypatch.setattr(tap, "_render", escaping_render)
    out = {"format": "source_list", "executive_summary": "x", "key_findings": [],
           "platforms": [{"platform": "hackernews", "total_analyzed": 3, "shown_count": 3, "posts": [
               None, "text", {"index": 1, "link": HN.format(1), "title": {"nested": "object"}, "summary": ["a", "list"], "evidence": 42,
                              "intent": "High", "role": "buyer", "purchase_intent": "explicit", "product_fit": "fits", "uncertainty": "not-a-list",
                              "verification_status": "verified", "analysis_verified": True, "relevance": "relevant", "platform": "hackernews"},
               {"index": 2, "link": "javascript:alert(1)", "title": "<img src=x onerror=window.__pwn=1>", "intent": "Low", "platform": "hackernews"}]}],
           "counts": {"retrieved": 2, "shown": 2, "excluded": 0}}
    page, errors = open_page(integrity_browser, name, out, [])
    try:
        rows = page.evaluate("""() => [...document.querySelectorAll('.ap-sources-table tbody tr')].map(tr => tr.children.length)""")
        heads = page.locator(".answer-rendered table.ap-sources-table th").count()
        assert rows and all(n == heads for n in rows)
        assert page.evaluate("window.__pwn") is None and page.evaluate("document.querySelectorAll('.ap-sources-table img').length") == 0
        assert page.evaluate("[...document.querySelectorAll('.ap-sources-table a')].every(a => !a.href.startsWith('javascript:'))")
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_the_opportunity_summary_shows_buyer_statement_entities_and_age(integrity_browser, built, name):
    out, results = built
    opp = out["opportunities"]
    assert opp["listed_posts"] == len(out["platforms"][0]["posts"])
    page, errors = open_page(integrity_browser, name, out, results)
    try:
        box = flat(page.inner_text(".opportunity-summary"))
        assert "Opportunity summary" in box
        # this fixture's historical source FAILED: the buyer-signal line must carry that, not a bare "no confirmed buyer signal"
        assert "No confirmed buyer signal was found in the listed posts, but the search was not complete" in box and "Reddit failed" in box
        assert f"{opp['listed_posts']} listed post" in box and f"{opp['identity_groups']} verified identity group" in box
        assert errors == []
    finally:
        page.close()
