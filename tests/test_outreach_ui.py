"""
tests/test_outreach_ui.py — presentation of the outreach-notes answer.

The outreach answer is plain markdown from logics.build_outreach_report(),
unchanged. The page now lays that SAME text out as a review (summary table,
one card per lead, a table for posts not to contact, copy buttons) instead of
one long markdown list. These tests build real reports with the real backend
function (only the LLM call is stubbed) and check, in Chromium, for both
templates, that every value in the stored text is still shown, that links and
copy buttons work, that untrusted text stays text, and that any other reply —
or a report whose structure isn't recognised — renders exactly as before.
No real LLM / Mongo / network.
"""
import json
import re

import pytest

from tests.test_answer_persistence import (  # noqa: F401  (fixtures + helpers)
    NOW, ORIGIN, ROOT, TEMPLATES, _for_template, browser, env,
)

jinja2 = pytest.importorskip("jinja2")


def _render(name, chat):
    """Rendered like the real app: Starlette's Jinja2Templates enables autoescape."""
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")),
                           undefined=jinja2.ChainableUndefined, autoescape=True)
    e.globals["url_for"] = lambda *a, **k: "#"
    return e.get_template(name).render(request=None, chat=chat, chats=[], user=None,
                                       chat_id=(chat or {}).get("chat_id"), pending_stream_topic_key=None,
                                       messages=(chat or {}).get("messages", []))


def _reopen(browser, name, chat, origin=ORIGIN, context=None):
    """chat.html loaded directly, or index.html then a soft-navigation into the chat."""
    pages = {"/chat/c1": _render("chat.html", chat), "/": _render("index.html", None)}
    page = (context or browser).new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def handle(route):
        url = route.request.url
        path = url[len(origin):].split("?")[0] if url.startswith(origin) else None
        if path in pages:
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[path])
        return route.fulfill(status=200, body="")
    page.route("**/*", handle)
    if name == "chat.html":
        page.goto(origin + "/chat/c1")
    else:
        page.goto(origin + "/")
        page.evaluate("navigateTo('/chat/c1'); 0")
        page.wait_for_function("document.querySelectorAll('.turn-block').length > 0")
    page.wait_for_timeout(300)
    page.evaluate("document.querySelectorAll('[class*=welcome]').forEach(e => e.remove()); 0")
    return page, errors

POSTS = [
    ("Best PMS for three properties on Airbnb, Vrbo, and Booking.com?", "ShortTermRentals",
     "https://www.reddit.com/r/ShortTermRentals/comments/1wtvzda/best_pms/",
     "I manage three short-term rentals and I'm looking for recommendations. I want a PMS primarily to centralize "
     "guest messaging, automate calendar syncing, and eventually set up a simple direct booking page."),
    ("Hiring a Product Designer (Mobile First)", "technepal", "https://www.reddit.com/r/technepal/comments/aa1/hiring/",
     "We’re hiring a Product Designer (Mobile First) to join Ticket Nepal, a travel booking platform being built for "
     "Nepali travellers and travel agencies."),
    ("Launching an app", "DarjeelingCity", "https://www.reddit.com/r/DarjeelingCity/comments/aa2/launch/",
     "We’ve started onboarding homestays for our upcoming app, StayRide. DM us if you want to partner."),
    ("Calendar view to manage your clients’ trips", "TravelBuilderPro",
     "https://www.reddit.com/r/TravelBuilderPro/comments/aa5/calendar/",
     "Calendar view to manage your clients’ trips is now live in our product."),
]
DRAFT = ("I hear you — for three listings you want something simple that centralizes messaging and keeps calendars "
         "in sync.\nDo you already use a channel manager? Tell me which channels you use and I’ll outline two setups.")


def _model():
    lead = {"number": 1, "post_title": POSTS[0][0], "category": "qualified", "author_type": "prospect",
            "need": "A simple, low-cost PMS for three short-term rentals.",
            "evidence": "I want a PMS primarily to centralize guest messaging, automate calendar syncing, and "
                        "eventually set up a simple direct booking page.",
            "reasoning": "The author states current needs and constraints.",
            "intent_warning": "Deployment preference and budget are unknown.",
            "product_fit": "partial", "fit_reason": "phptravels covers direct booking; channel integrations unconfirmed.",
            "approach": "Acknowledge their priorities and ask which channels they use.",
            "acknowledge": "They want a simple, low-cost tool.",
            "question": "Do you use a channel manager, and do you prefer hosted or self-hosted?",
            "outline": ["Acknowledge simplicity and cost.", "Ask about channels.", "Suggest two setups."],
            "platform_caution": "Keep the reply helpful and public.", "draft": DRAFT}
    amb = {"number": 2, "post_title": POSTS[1][0], "category": "ambiguous", "author_type": "seller",
           "need": "Building Ticket Nepal; unclear whether they will buy or build.",
           "evidence": "We’re hiring a Product Designer (Mobile First) to join Ticket Nepal",
           "reasoning": "Active build, no explicit buying signal.", "intent_warning": "No timeline or budget.",
           "product_fit": "unknown", "fit_reason": "Requirements unknown.",
           "do_not_contact_reason": "No explicit buying signal.",
           "approach": "Ask one neutral product question.", "acknowledge": "They are hiring a designer.",
           "question": "Will you build your booking engine in-house or evaluate white-label platforms?",
           "outline": ["Congratulate them.", "Ask the question."],
           "platform_caution": "Recruitment posts may discourage vendor pitches."}
    nf = lambda n, need, ev: {"number": n, "post_title": POSTS[n - 1][0], "category": "irrelevant",
                              "author_type": "seller", "need": need, "evidence": ev,
                              "reasoning": "Not a booking-software buyer.", "intent_warning": "No buyer intent.",
                              "product_fit": "unsupported", "fit_reason": "They are not buying.",
                              "do_not_contact_reason": "No stated need to purchase booking software."}
    return {"posts": [lead, amb,
                      nf(3, "Onboarding homestays and seeking partners.", "We’ve started onboarding homestays for our upcoming app, StayRide"),
                      nf(4, "Competitor feature announcement.", "Calendar view to manage your clients’ trips")],
            "missing_info": ["Do you offer hosted SaaS plans as well as self-hosted options?",
                             "Which integrations are supported out of the box?"]}


def _report(lg, monkeypatch, model=None, titles=None):
    posts = [{"number": i + 1, "title": (titles or {}).get(i + 1, t), "source": s, "platform": "reddit", "link": l,
              "summary": "", "post_text": x} for i, (t, s, l, x) in enumerate(POSTS)]
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps(model or _model()))
    return lg.build_outreach_report("Now make me suggested message for outreach", "Find buyers", posts,
                                    business_context="phptravels — white-label travel booking software.")


def _chat(*answers):
    msgs = [{"query": f"Question {i}", "topic_key": None, "keywords": [], "results": [], "claude_answer": a,
             "message_type": "chat", "requested_at": NOW} for i, a in enumerate(answers)]
    return _for_template({"chat_id": "c1", "title": "Outreach", "messages": msgs})


def _stored_values(md):
    """Every value the stored markdown carries: field values, draft lines, titles, 'tell me' items."""
    vals = []
    for line in md.split("\n"):
        m = re.match(r"^- \*\*(.+?):\*\*\s?(.*)$", line)
        if m and m.group(2):
            vals.append(m.group(2))
        q = re.match(r"^\s*>\s?(.*)$", line)
        if q and q.group(1):
            vals.append(q.group(1))
        t = re.match(r"^\d+\. \*\*\[(.+?)\]\(", line)
        if t:
            vals.append(t.group(1))
        mi = re.match(r"^- (?!\*\*)(.*)$", line)
        if mi:
            vals.append(mi.group(1))
    return [" ".join(v.split()) for v in vals]


LAYOUT_JS = """() => { const r = document.querySelector('.or-report'); if (!r) return null;
  return { text: r.innerText,
    headline: r.querySelector('.or-headline').textContent,
    summary: [...r.querySelectorAll('.or-summary tbody tr')].map(tr => [...tr.cells].map(c => c.textContent)),
    sections: [...r.querySelectorAll('.or-section-title')].map(e => e.textContent),
    cards: r.querySelectorAll('.or-card').length,
    tableRows: r.querySelectorAll('.or-table tbody tr').length,
    copies: [...r.querySelectorAll('.or-copy')].map(b => b.dataset.copyText),
    links: [...r.querySelectorAll('a')].map(a => ({href: a.getAttribute('href'), target: a.target, rel: a.rel})),
    fit: [...r.querySelectorAll('.or-fit-badge')].map(b => b.textContent) }; }"""


@pytest.mark.parametrize("name", TEMPLATES)
def test_outreach_report_gets_the_review_layout_and_keeps_every_value(browser, env, monkeypatch, name):
    lg, _, _ = env
    md = _report(lg, monkeypatch)
    assert md.startswith("**Outreach notes for the 4 posts")            # the stored text itself is unchanged
    page, errors = _reopen(browser, name, _chat(md))
    try:
        out = page.evaluate(LAYOUT_JS)
        assert out is not None, "outreach answer was not given the review layout"
        assert out["headline"] == "Outreach review — 4 posts"
        # the report's own opening sentence and headings stay visible, word for word
        assert "Outreach notes for the 4 posts from your previous result — the same posts, not a new search." in out["text"]
        for heading in ("Interested — potential leads: buying intent, but the need isn't spelled out",
                        "Ambiguous — read the post before reaching out",
                        "Not a fit — competitors / providers and unrelated posts, no outreach suggested"):
            assert heading in out["text"]
        assert out["summary"] == [["Potential leads", "1", "Review and reply carefully"],
                                  ["Ambiguous", "1", "Do not contact yet"],
                                  ["Not a fit", "2", "Do not contact"]]
        assert out["sections"] == ["Potential lead — 1", "Ambiguous — 1", "Not a fit — 2",
                                   "To personalize future outreach"]
        assert out["cards"] == 2 and out["tableRows"] == 2
        assert out["fit"] == ["Partial", "Not verified"]
        shown = " ".join(out["text"].split())
        missing = [v for v in _stored_values(md)
                   if v not in shown and not all(p in shown for p in v.split(" — ") if p)]
        assert missing == [], missing
        # copy buttons: lead quote, lead question, draft, ambiguous quote, ambiguous question
        assert len(out["copies"]) == 5
        assert out["copies"][2] == DRAFT                                  # exact draft, line break kept
        assert all(not c.startswith("“") and not c.endswith("”") for c in out["copies"])
        assert {l["href"] for l in out["links"]} >= {p[2] for p in POSTS}
        assert all(l["href"].startswith("https://") and l["target"] == "_blank" and "noopener" in l["rel"]
                   for l in out["links"])
        assert errors == []
    finally:
        page.close()


def test_copy_button_puts_the_exact_text_on_the_clipboard(browser, env, monkeypatch):
    lg, _, _ = env
    md = _report(lg, monkeypatch)
    ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"], viewport={"width": 1280, "height": 2400})
    page, errors = _reopen(browser, "chat.html", _chat(md), origin="https://flintel.test", context=ctx)  # secure context
    try:
        for selector, expected in ((".or-draft-text .or-copy", DRAFT),
                                   (".or-ask .or-copy", _model()["posts"][0]["question"])):
            page.evaluate(f"document.querySelector('{selector}').scrollIntoView({{block: 'center'}}); 0")
            page.locator(selector).first.click()
            page.wait_for_function(f"document.querySelector('{selector}').classList.contains('copied')")
            assert page.evaluate("navigator.clipboard.readText()") == expected
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_other_replies_and_unrecognised_reports_render_as_before(browser, env, monkeypatch, name):
    lg, _, _ = env
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: "not json")
    failed = lg.build_outreach_report("outreach please", "Find buyers",
                                      [{"number": 1, "title": "A post", "source": "x", "link": "https://www.reddit.com/r/x/1"}])
    assert failed.startswith("I couldn't prepare the outreach notes")
    good = _report(lg, monkeypatch)
    unknown_heading = good.replace("### Ambiguous — read the post before reaching out", "### Something new")
    stray_line = good.replace("- **Who:** Has the need themselves", "- **Who:** Has the need themselves\nfree text")
    page, errors = _reopen(browser, name, _chat("**Hello** there, how can I help?", failed, unknown_heading,
                                                stray_line))
    try:
        assert page.evaluate("document.querySelectorAll('.or-report').length") == 0
        shown = page.evaluate("[...document.querySelectorAll('.answer-wrap')].map(w => w.innerText).join(' ')")
        for text in ("Hello", "I couldn't prepare the outreach notes", "Something new", "free text"):
            assert text in shown
        assert errors == []
    finally:
        page.close()


@pytest.mark.parametrize("name", TEMPLATES)
def test_untrusted_post_text_stays_text(browser, env, monkeypatch, name):
    lg, _, _ = env
    evil = '<img src=x onerror="window.__pwned=1">Best PMS <b>now</b>'
    model = _model()
    model["posts"][0]["post_title"] = evil
    md = _report(lg, monkeypatch, model=model, titles={1: evil})
    assert evil.replace("[", "(") in md                                  # raw HTML is in the stored text
    page, errors = _reopen(browser, name, _chat(md))
    try:
        assert page.evaluate("document.querySelectorAll('.or-report').length") == 1
        assert page.evaluate("window.__pwned") is None
        assert page.evaluate("document.querySelectorAll('.or-report img, .or-report b').length") == 0
        assert evil in page.evaluate("document.querySelector('.or-report .or-title').textContent")
        assert errors == []
    finally:
        page.close()


def test_both_templates_share_the_same_outreach_renderer():
    blocks = []
    for name in TEMPLATES:
        src = (ROOT / "templates" / name).read_text(encoding="utf-8")
        a, b = src.index("// OUTREACH-UI-BEGIN"), src.index("// OUTREACH-UI-END")
        blocks.append(src[a:b])
        assert "renderOutreachReport(raw, renderedEl)" in src
    assert blocks[0] == blocks[1]
