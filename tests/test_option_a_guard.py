"""
tests/test_option_a_guard.py — hardening of Option A (reuse the previous
result's posts) found in review:

  * a bare option pick never starts a search for the "these posts" option —
    not when the router says "search"/"chat", not when routing raises;
    an unclear pick is asked about; explicit new-research requests still search;
  * multiple searches in one chat: only the newest answered search is used;
  * "qualified" is enforced in code (sellers / developers / researchers /
    missing need or evidence are downgraded with a visible note);
  * bool post numbers, e-mails / bare domains, malformed stored fields and
    mismatched post text / URLs are handled; notes must quote their own post;
  * the saved report survives a reload through the real GET /chat/{id} view
    and renders in Chromium (chat.html and the index.html soft navigation).

Same stub setup as test_option_a_outreach.py. No real LLM / Mongo / network.
"""
import copy
import json
import os
import re
import sys

import pytest

from tests.test_answer_persistence import env  # noqa: F401  (fixture)
from tests.test_option_a_outreach import (  # noqa: F401  (fixture + data)
    ANSWER, OFFER, ORIGINAL_URLS, OUTREACH, POSTS, RESULTS, flow,
)

SEARCH_ROUTER = {"intent": "search", "reply": None, "keywords": ["crm buyers"], "time_window_days": None,
                 "match_phrases": ["looking for a CRM"], "evidence_required": None, "website_only": False,
                 "url_ask_type": "none", "target_entity": "CRM buyers", "intent_types": None,
                 "resolved_query": "Find more people looking to buy a CRM."}


def _report(doc):
    return doc["messages"][-1]["claude_answer"]


def _sent_posts(state, i=0):
    return json.loads(state["outreach_calls"][i].split("Posts (JSON):\n", 1)[1])


def _item(number, category="qualified", author="prospect", need="Needs a CRM.", evidence=None, **extra):
    d = {"number": number, "category": category, "author_type": author, "need": need,
         "evidence": evidence, "reasoning": "r", "intent_warning": None, "approach": "Ask first.",
         "acknowledge": "ack", "question": "q?", "outline": ["one"], "platform_caution": None}
    d.update(extra)
    return d


# ── 1. a bare option pick never starts an unintended search ──────────────────
@pytest.mark.parametrize("wrong", ["search", "chat"])
def test_router_wrong_intent_for_option_a_is_overridden(flow, wrong):
    lg, state, jobs, send, chats = flow
    state["router"] = SEARCH_ROUTER if wrong == "search" else {"intent": "chat", "reply": "Here are some generic tips",
                                                               "keywords": None, "time_window_days": None}
    doc = send("Option A is better")
    assert jobs == [] and len(state["outreach_calls"]) == 1
    assert doc["messages"][-1]["message_type"] == "chat" and "generic tips" not in _report(doc)
    assert [p["title"] for p in _sent_posts(state)] == [t for _s, t, _u, _x in POSTS]


def test_routing_exception_still_reuses_posts_and_queues_nothing(flow, monkeypatch):
    lg, state, jobs, send, chats = flow
    rt = sys.modules["routes"]

    def boom(*a, **k):
        raise RuntimeError("router down")
    monkeypatch.setattr(rt, "classify_and_maybe_chat", boom)
    doc = send("Option A is better")
    assert jobs == [] and len(state["outreach_calls"]) == 1
    for url in ORIGINAL_URLS:
        assert url in _report(doc)


def test_unreadable_chat_and_routing_failure_never_searches(flow, monkeypatch):
    lg, state, jobs, send, chats = flow
    rt = sys.modules["routes"]

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(rt, "get_chat_session", boom)
    monkeypatch.setattr(rt, "create_chat_session", boom)
    resp = rt.search(type("R", (), {"session": {}})(), query="Option A", platform="All Platforms", chat_id="c1")
    assert resp.status_code == 303 and jobs == [] and state["outreach_calls"] == []


def test_routing_exception_option_b_asks_instead_of_searching(flow, monkeypatch):
    lg, state, jobs, send, chats = flow
    rt = sys.modules["routes"]
    monkeypatch.setattr(rt, "classify_and_maybe_chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    report = _report(send("go with B"))
    assert jobs == [] and state["outreach_calls"] == []
    assert "haven't started anything" in report and "(B)" in report and "runs a new search" in report


def test_router_previous_results_for_option_b_asks_which_one(flow):
    lg, state, jobs, send, chats = flow
    state["router"] = {"intent": "previous_results", "previous_results_numbers": None}
    report = _report(send("Option B please"))
    assert jobs == [] and state["outreach_calls"] == []
    assert "which one do you want" in report and "uses the posts I already showed you" in report


def test_unknown_option_letter_asks_instead_of_searching(flow):
    lg, state, jobs, send, chats = flow
    state["router"] = SEARCH_ROUTER
    report = _report(send("option C"))
    assert jobs == [] and "(A)" in report and "(B)" in report


def test_both_options_reuse_posts_and_do_not_search(flow):
    lg, state, jobs, send, chats = flow
    state["router"] = SEARCH_ROUTER
    report = _report(send("both please"))
    assert jobs == [] and len(state["outreach_calls"]) == 1
    assert "haven't started it" in report


@pytest.mark.parametrize("text", ["Find 10 more people looking for a CRM in the USA",
                                  "Option A, but only for the USA", "Option B please", "go with B"])
def test_explicit_new_research_after_the_offer_still_searches(flow, text):
    lg, state, jobs, send, chats = flow
    state["router"] = SEARCH_ROUTER
    doc = send(text)
    assert len(jobs) == 1 and state["outreach_calls"] == []
    assert doc["messages"][-1]["claude_answer"] is None                   # a pending search message


def test_without_an_offer_behaviour_is_unchanged(flow, monkeypatch):
    lg, state, jobs, send, chats = flow
    answer = dict(ANSWER)
    answer.pop("followup_question")
    chats.docs[("c1", "o1")]["messages"][0]["claude_answer"] = json.dumps(answer)
    state["router"] = SEARCH_ROUTER
    send("Option A is better")
    assert len(jobs) == 1 and state["outreach_calls"] == []               # router decided, as before
    rt = sys.modules["routes"]
    monkeypatch.setattr(rt, "classify_and_maybe_chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    send("Option A")
    assert len(jobs) == 2                                                 # the old routing-failure fallback


# ── 2. multiple searches in one chat ─────────────────────────────────────────
CAR_POSTS = [("cars", "Which used SUV should I buy?", "https://www.reddit.com/r/cars/comments/c1/",
              "Budget 20k, looking for a reliable used SUV."),
             ("whatcarshouldIbuy", "First car advice", "https://www.reddit.com/r/whatcarshouldIbuy/comments/c2/",
              "Need recommendations for a first car.")]


def _search_msg(posts, offer=OFFER, query="q", answered=True):
    ans = {"format": "source_list", "headline": "h", "executive_summary": "s", "followup_question": offer,
           "platforms": [{"platform": "reddit", "total_analyzed": len(posts), "shown_count": len(posts), "posts": [
               {"index": i + 1, "source": s, "title": t, "summary": "x", "sentiment": "neutral", "link": u}
               for i, (s, t, u, _x) in enumerate(posts)]}]}
    return {"query": query, "topic_key": query.lower(), "keywords": ["k"], "targeting_platform": "all",
            "results": [{"title": t, "post_text": x, "post_url": u, "platform": "reddit"} for _s, t, u, x in posts],
            "claude_answer": json.dumps(ans) if answered else None}


def _chat_msg(q, a):
    return {"query": q, "topic_key": None, "results": [], "claude_answer": a, "message_type": "chat"}


def test_newest_search_is_used_not_an_older_one(flow):
    lg, state, jobs, send, chats = flow
    chats.docs[("c1", "o1")]["messages"].append(_search_msg(CAR_POSTS, query="Find car buyers"))
    chats.docs[("c1", "o1")]["messages"].append(_chat_msg("thanks", "You're welcome!"))   # intervening chat turn
    state["router"] = {"intent": "previous_results", "previous_results_numbers": None}
    state["outreach"] = json.dumps({"posts": [], "missing_info": []})
    report = _report(send("Option A"))
    assert [p["title"] for p in _sent_posts(state)] == [t for _s, t, _u, _x in CAR_POSTS]
    assert "Which used SUV should I buy?" in state["router_calls"][0]
    assert not any(t in state["router_calls"][0] for _s, t, _u, _x in POSTS)   # older CRM posts never shown
    for url in ORIGINAL_URLS:
        assert url not in report
    assert jobs == []


def test_after_an_option_a_report_the_same_search_is_still_used(flow):
    lg, state, jobs, send, chats = flow
    state["router"] = {"intent": "previous_results", "previous_results_numbers": None}
    send("Option A")
    send("Option A")                                                      # asked again after the report
    assert _sent_posts(state, 1) == _sent_posts(state, 0) and jobs == []


def test_newest_search_still_running_shows_no_results_block(flow):
    lg, state, jobs, send, chats = flow
    chats.docs[("c1", "o1")]["messages"].append(_search_msg(CAR_POSTS, query="Find car buyers", answered=False))
    state["router"] = SEARCH_ROUTER
    send("Option A is better")
    assert "Latest research result" not in state["router_calls"][0]
    assert state["outreach_calls"] == []                                  # the older CRM posts are not reused


def test_newest_search_without_posts_does_not_fall_back_to_older(flow):
    lg, state, jobs, send, chats = flow
    nores = _search_msg([], query="Find boat buyers")
    nores["claude_answer"] = json.dumps({"format": "no_results", "message": "Nothing.", "followup_question": OFFER})
    chats.docs[("c1", "o1")]["messages"].append(nores)
    state["router"] = SEARCH_ROUTER
    report = _report(send("Option A is better"))
    assert jobs == [] and state["outreach_calls"] == []
    assert "don't have the posts" in report
    for url in ORIGINAL_URLS:
        assert url not in report


# ── 3. honest classification, enforced in code ───────────────────────────────
def _outreach(flow_state, items):
    flow_state["router"] = {"intent": "previous_results", "previous_results_numbers": None}
    flow_state["outreach"] = json.dumps({"posts": items, "missing_info": []})


def _section(report, heading):
    if heading not in report:
        return ""
    rest = report.split(heading, 1)[1]
    return rest.split("\n### ", 1)[0]


def test_seller_labelled_qualified_is_not_a_prospect(flow):
    lg, state, jobs, send, chats = flow
    _outreach(state, [_item(5, author="seller", evidence="I run an agency and want partners who pay referral fees.")])
    report = _report(send("Option A"))
    assert ORIGINAL_URLS[4] not in _section(report, "### Qualified prospects")
    assert ORIGINAL_URLS[4] in _section(report, "### Not a fit")
    assert "selling, promoting, or looking for users" in report


def test_developer_seeking_users_labelled_prospect_is_downgraded(flow):
    lg, state, jobs, send, chats = flow
    dev = [("SaaS", "I built a CRM for freelancers", "https://www.reddit.com/r/SaaS/comments/d1/",
            "I built a CRM app for freelancers and I'm looking for beta testers.")]
    chats.docs[("c1", "o1")]["messages"].append(_search_msg(dev, query="Find CRM buyers"))
    _outreach(state, [_item(1, author="prospect", evidence="looking for beta testers")])
    report = _report(send("Option A"))
    assert "### Qualified prospects" not in report and dev[0][2] in _section(report, "### Not a fit")


def test_researcher_labelled_qualified_stays_weak(flow):
    lg, state, jobs, send, chats = flow
    _outreach(state, [_item(2, author="researcher", evidence="What CRM do you all use for inquiries")])
    report = _report(send("Option A"))
    assert ORIGINAL_URLS[1] in _section(report, "### Weak signals") and "### Qualified prospects" not in report


@pytest.mark.parametrize("need", [None, "", "   "])
def test_qualified_without_a_stated_need_is_not_kept(flow, need):
    # (follow-up fix) genuine buying intent without a stated need -> the "Interested" tier
    lg, state, jobs, send, chats = flow
    _outreach(state, [_item(1, need=need, evidence="Trialled two CRMs, both too complex")])
    report = _report(send("Option A"))
    assert "### Qualified prospects" not in report
    assert ORIGINAL_URLS[0] in _section(report, "### Interested") and "need isn't stated" in report


@pytest.mark.parametrize("evidence", [None, "", "They want enterprise software for 500 seats"])
def test_notes_without_a_quote_from_this_post_are_not_assessed(flow, evidence):
    lg, state, jobs, send, chats = flow
    _outreach(state, [_item(1, evidence=evidence)])
    report = _report(send("Option A"))
    assert "### Qualified prospects" not in report
    assert ORIGINAL_URLS[0] in _section(report, "### Not assessed") and "could not be matched" in report


def test_notes_quoting_another_post_cannot_attach_to_this_one(flow):
    lg, state, jobs, send, chats = flow
    # number 4 (WhatsApp) but quoting post 1's text
    _outreach(state, [_item(4, evidence="Trialled two CRMs, both too complex")])
    report = _report(send("Option A"))
    assert ORIGINAL_URLS[3] in _section(report, "### Not assessed") and "### Qualified prospects" not in report


def test_qualified_without_buying_language_in_the_post_stays_weak(flow):
    lg, state, jobs, send, chats = flow
    _outreach(state, [_item(3, evidence="Our response time is killing conversion")])
    report = _report(send("Option A"))
    assert ORIGINAL_URLS[2] in _section(report, "### Weak signals") and "does not state an intent to buy" in report


def test_genuine_buyer_stays_qualified(flow):
    lg, state, jobs, send, chats = flow
    _outreach(state, [_item(1, evidence="Trialled two CRMs, both too complex. Budget ~$50/mo")])
    report = _report(send("Option A"))
    assert ORIGINAL_URLS[0] in _section(report, "### Qualified prospects") and "Flintel check" not in report


# ── 4. data and link hardening ───────────────────────────────────────────────
def test_boolean_post_number_does_not_attach_to_post_one(flow):
    lg, state, jobs, send, chats = flow
    _outreach(state, [_item(True, evidence="Trialled two CRMs, both too complex. Budget ~$50/mo")])
    report = _report(send("Option A"))
    assert "### Qualified prospects" not in report and ORIGINAL_URLS[0] in _section(report, "### Not assessed")


def test_emails_and_bare_domains_in_model_text_are_not_links(flow):
    lg, state, jobs, send, chats = flow
    _outreach(state, [_item(1, evidence="Trialled two CRMs, both too complex. Budget ~$50/mo",
                            approach="Email sales@crm-vendor.com or see crm-vendor.io/pricing and javascript://x",
                            question="Reach me at me@example.org?")])
    report = _report(send("Option A"))
    assert "@" not in report.replace("＠", "") and "crm-vendor" not in report and "javascript" not in report
    assert "(link removed)" in report
    assert set(re.findall(r"https?://[^\s)\]]+", report)) <= set(ORIGINAL_URLS)


def test_malformed_stored_fields_do_not_break_the_report(flow):
    lg, state, jobs, send, chats = flow
    bad = _search_msg(CAR_POSTS[:1], query="Find car buyers")
    ans = json.loads(bad["claude_answer"])
    ans["platforms"][0]["posts"][0].update({"title": ["not", "a", "string"], "link": "javascript:alert(1)",
                                            "source": {"x": 1}, "summary": 42})
    ans["platforms"][0]["posts"].append({"index": 2, "title": "Second", "source": "cars",
                                         "link": "https://www.reddit.com/r/cars/comments/c9/"})
    bad["claude_answer"] = json.dumps(ans)
    bad["results"] = ["not a dict", {"post_url": None}, {"post_url": "https://www.reddit.com/r/cars/comments/c9/",
                                                          "post_text": {"oops": True}}]
    chats.docs[("c1", "o1")]["messages"].append(bad)
    _outreach(state, [])
    doc = send("Option A")
    report = _report(doc)
    assert "Untitled post" in report and "javascript" not in report
    assert "https://www.reddit.com/r/cars/comments/c9/" in report and jobs == []
    assert [p["post_text"] for p in _sent_posts(state)] == [None, None]


def test_mismatched_post_text_url_uses_title_only(flow):
    lg, state, jobs, send, chats = flow
    msg = chats.docs[("c1", "o1")]["messages"][0]
    msg["results"][0]["post_url"] = "https://www.reddit.com/r/other/comments/zz/"      # no longer matches post 1
    _outreach(state, [_item(1, evidence="Trialled two CRMs, both too complex")])     # text it can't see now
    report = _report(send("Option A"))
    assert _sent_posts(state)[0]["post_text"] is None
    assert ORIGINAL_URLS[0] in _section(report, "### Not assessed")                  # not matched -> not trusted


# ── 5. persistence + rendering after a reload (real view, real Chromium) ─────
try:
    import jinja2  # noqa: F401
    from playwright.sync_api import sync_playwright
    HAVE_BROWSER = True
except Exception:          # pragma: no cover
    HAVE_BROWSER = False

ORIGIN = "http://flintel.test"
MARKED_JS = os.environ.get("FLINTEL_TEST_MARKED_JS")      # optional local copies of the CDN scripts
PURIFY_JS = os.environ.get("FLINTEL_TEST_PURIFY_JS")


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


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_report_persists_and_renders_after_reload(browser, flow, monkeypatch, name):
    from fastapi import BackgroundTasks
    from starlette.requests import Request
    lg, state, jobs, send, chats = flow
    _outreach(state, [_item(1, evidence="Trialled two CRMs, both too complex. Budget ~$50/mo",
                            approach="Write to sales@vendor.com"),
                      _item(5, author="seller", evidence="I run an agency and want partners")])
    send("Option A is better")
    rt = sys.modules["routes"]
    idx = sys.modules["index"]
    monkeypatch.setattr(rt, "get_current_user", lambda request: None)
    monkeypatch.setattr(rt, "get_user_chats", lambda owner_key: [{"chat_id": "c1", "title": "CRM"}])
    request = Request({"type": "http", "method": "GET", "path": "/chat/c1", "headers": [], "query_string": b"",
                       "session": {}, "app": idx.app})
    html = rt.view_chat(request, "c1", BackgroundTasks()).body.decode("utf-8")      # the REAL reload path
    from tests.test_answer_persistence import _render
    pages = {"/chat/c1": html, "/": _render("index.html", None)}
    scripts = {"marked": MARKED_JS, "purify": PURIFY_JS}
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def handle(route):
        url = route.request.url
        path = url[len(ORIGIN):].split("?")[0] if url.startswith(ORIGIN) else None
        if path in pages:
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[path])
        for key, local in scripts.items():
            if key in url and local and os.path.exists(local):
                return route.fulfill(status=200, headers={"Content-Type": "application/javascript"},
                                     body=open(local, encoding="utf-8").read())
        return route.fulfill(status=200, body="")
    page.route("**/*", handle)
    try:
        if name == "chat.html":
            page.goto(ORIGIN + "/chat/c1")
        else:
            page.goto(ORIGIN + "/")
            page.evaluate("navigateTo('/chat/c1'); 0")
            page.wait_for_function("document.querySelectorAll('.turn-block').length > 1")
        page.wait_for_timeout(300)
        turn = page.locator(".turn-block").last
        assert turn.locator(".turn-question").inner_text() == "Option A is better"
        shown = turn.locator(".answer-wrap").inner_text()
        assert "Outreach notes for the 5 posts" in shown and "Qualified prospects" in shown
        assert "(link removed)" in shown and "vendor.com" not in shown and "sales" not in shown   # e-mail fully removed
        assert page.evaluate("document.querySelectorAll('a[href^=\"mailto:\"]').length") == 0
        assert turn.locator(".turn-chip").count() == 0                   # a chat reply: no research status
        if MARKED_JS and PURIFY_JS:
            assert page.evaluate("!!window.marked && !!window.DOMPurify")
            hrefs = page.evaluate("[...[...document.querySelectorAll('.turn-block')].pop()"
                                  ".querySelectorAll('.answer-rendered a')].map(a => [a.getAttribute('href'), a.target])")
            assert sorted(h for h, _t in hrefs) == sorted(ORIGINAL_URLS)
            assert all(t == "_blank" for _h, t in hrefs)
            assert page.evaluate("[...document.querySelectorAll('.turn-block')].pop()"
                                 ".querySelectorAll('.answer-rendered h3').length") >= 2
        assert errors == []
    finally:
        page.close()
