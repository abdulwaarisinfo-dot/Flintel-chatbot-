"""
tests/test_lead_qualification.py — evidence-backed guidance, lead
qualification and outreach drafts.

  * analysis input carries each post's ORIGINAL publication time ("Posted:")
    only when it is known; nothing is invented for posts without one;
  * the guidance note asks for buyer / potential buyer / discussion /
    competitor-provider / announcement separation, sample sizes, observations
    vs hypotheses vs next steps, product fit, and honest dates;
  * outreach: product fit is enforced deterministically (unsupported -> do not
    contact; unverifiable fit -> potential lead, not qualified), drafts appear
    only for suitable prospects, are sanitised, sit next to the STORED source
    link, and nothing claims to have been sent; unsuitable posts say
    "Do not contact" and why; publication dates and stale posts are shown;
  * Option A through the real route keeps the stored posts, dates and links
    and starts no search.

Recording LLM stub; no real LLM / Mongo / network. This proves the wiring and
the deterministic checks — not the quality of live model output.
"""
import copy
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_answer_persistence import env  # noqa: F401  (fixture)
from tests.test_option_a_outreach import flow, ORIGINAL_URLS, RESULTS, _report  # noqa: F401

RECENT = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0)
OLD = (datetime.now(timezone.utc) - timedelta(days=200)).replace(microsecond=0)

HOST = {"number": 1, "title": "Need a channel manager for my 3 Airbnb listings",
        "post_text": "I host 3 short-term rentals and keep getting double bookings between Airbnb and Booking.com. "
                     "Looking for a channel manager that syncs calendars, budget around $30/month.",
        "link": "https://www.reddit.com/r/airbnb_hosts/comments/h1/", "source": "airbnb_hosts", "platform": "reddit",
        "published_at": RECENT.isoformat()}
AGENCY = {"number": 2, "title": "Looking to switch our travel agency booking engine",
          "post_text": "We are a 12-person travel agency looking to replace our booking engine. We need hotel and "
                       "flight supplier integrations and a white-label site. Recommendations?",
          "link": "https://www.reddit.com/r/travelagents/comments/t2/", "source": "travelagents", "platform": "reddit",
          "published_at": OLD.isoformat()}
VENDOR = {"number": 3, "title": "We just launched our booking engine for agencies",
          "post_text": "We just launched our booking engine for travel agencies, check out our site, DM me for a demo.",
          "link": "https://www.reddit.com/r/travelagents/comments/v3/", "source": "travelagents", "platform": "reddit"}
CHATTER = {"number": 4, "title": "Is the travel industry recovering?",
           "post_text": "General question: do you think bookings are back to 2019 levels?",
           "link": "https://www.reddit.com/r/travel/comments/c4/", "source": "travel", "platform": "reddit"}
OFFER = "We sell a white-label travel booking engine for travel agencies, with hotel and flight supplier integrations."


def _item(n, title, category, author, fit=None, draft=None, **extra):
    d = {"number": n, "post_title": title, "category": category, "author_type": author,
         "need": extra.pop("need", "Stated need."), "evidence": None, "reasoning": "r", "intent_warning": None,
         "approach": "Help first.", "acknowledge": "ack", "question": "What matters most?", "outline": ["one"],
         "platform_caution": None, "fit_reason": extra.pop("fit_reason", None), "draft": draft,
         "do_not_contact_reason": extra.pop("do_not_contact_reason", None)}
    if fit is not None:
        d["product_fit"] = fit
    d.update(extra)
    return d


DRAFT = ("Sounds like the current engine is holding a 12-person team back.\n\nWhich supplier integrations matter "
         "most to you right now - hotels, flights, or both? Mail me at sales@vendor.example or see "
         "https://vendor.example/demo for a demo.")


def _run(lg, monkeypatch, items, posts, business_context=OFFER):
    seen = {}

    def fake_llm(system, user, **kw):
        seen["user"] = user
        return json.dumps({"posts": items, "missing_info": []})
    monkeypatch.setattr(lg, "_call_claude", fake_llm)
    return lg.build_outreach_report("Write outreach for each of those posts", "Find travel agencies looking "
                                    "for a booking engine", copy.deepcopy(posts), business_context=business_context), seen


def _block(report, url):
    """The report lines for the post whose stored link is `url`."""
    start = report.index(url)
    start = report.rfind("\n\n", 0, start) + 2
    end = report.find("\n\n", report.index(url))
    return report[start:end if end != -1 else None]


# ── the fit check (unit) ─────────────────────────────────────────────────────
@pytest.mark.parametrize("category,fit,ctx,expected", [
    ("qualified", None, "", ("qualified", None)),                     # not assessed: unchanged
    ("qualified", "fits", OFFER, ("qualified", "fits")),
    ("qualified", "fits", "", ("interested", "unknown")),            # cannot verify without the offer
    ("qualified", "partial", OFFER, ("interested", "partial")),
    ("qualified", "unknown", OFFER, ("interested", "unknown")),
    ("interested", "unsupported", OFFER, ("irrelevant", "unsupported")),
    ("weak", "unsupported", OFFER, ("irrelevant", "unsupported")),
    ("irrelevant", "fits", OFFER, ("irrelevant", "fits")),           # never upgraded
    ("qualified", "made-up", OFFER, ("qualified", None)),
])
def test_fit_rules(env, category, fit, ctx, expected):
    lg, _, _ = env
    item = {} if fit is None else {"product_fit": fit}
    cat, got_fit, _note = lg._honest_fit(category, item, ctx)
    assert (cat, got_fit) == expected


# ── outreach report ──────────────────────────────────────────────────────────
def test_suitable_prospect_gets_a_sanitised_draft_next_to_the_stored_link(env, monkeypatch):
    lg, _, _ = env
    items = [_item(2, AGENCY["title"], "qualified", "prospect", fit="fits", draft=DRAFT,
                   fit_reason="They need hotel/flight supplier integrations and a white-label site, which the offer covers.")]
    report, seen = _run(lg, monkeypatch, items, [AGENCY])
    block = _block(report, AGENCY["link"])
    assert "### Qualified prospects" in report and "**Product fit:** Fits" in block
    assert "**Draft to edit (not sent):**" in block and "> Which supplier integrations matter most" in block
    assert f"**Source:** {AGENCY['link']}" in block
    assert "vendor.example" not in report and "sales@" not in report and "(link removed)" in block
    assert "Flintel has not sent anything" in report
    # 200-day-old post: date shown and flagged
    assert f"**Posted:** {OLD.strftime('%b')} {OLD.day}, {OLD.year} (UTC)" in block and "may no longer be current" in block
    # the model saw the original date, not an invented one
    assert json.loads(seen["user"].split("Posts (JSON):\n", 1)[1])[0]["posted"] == OLD.date().isoformat()


def test_broad_product_is_not_pushed_on_a_host_with_a_different_need(env, monkeypatch):
    lg, _, _ = env
    items = [_item(1, HOST["title"], "qualified", "prospect", fit="unsupported",
                   draft="Hi! Our booking engine would be perfect for you.",
                   fit_reason="A booking engine for agencies does not sync Airbnb and Booking.com calendars.")]
    report, _ = _run(lg, monkeypatch, items, [HOST])
    block = _block(report, HOST["link"])
    assert "### Not a fit" in report and "### Qualified prospects" not in report
    assert "Do not contact" in block and "doesn't meet this person's stated need" in block
    assert "Draft to edit" not in report and "perfect for you" not in report
    assert f"**Posted:** {RECENT.strftime('%b')} {RECENT.day}, {RECENT.year} (UTC)" in block
    assert "may no longer be current" not in block


def test_without_the_users_offer_fit_is_unverified_and_not_qualified(env, monkeypatch):
    lg, _, _ = env
    items = [_item(2, AGENCY["title"], "qualified", "prospect", fit="fits", draft=DRAFT)]
    report, _ = _run(lg, monkeypatch, items, [AGENCY], business_context="")
    block = _block(report, AGENCY["link"])
    assert "### Qualified prospects" not in report and "### Interested" in report
    assert "**Product fit:** Not verified" in block and "haven't told Flintel what you offer" in block
    assert "Draft to edit (not sent)" in block                      # a help-first draft is still offered


def test_vendors_and_chatter_are_do_not_contact_without_drafts(env, monkeypatch):
    lg, _, _ = env
    items = [_item(3, VENDOR["title"], "qualified", "seller", fit="fits", draft="Let's partner up, great product!"),
             _item(4, CHATTER["title"], "weak", "researcher", fit="unknown", draft="Hey, buy our engine!",
                   need=None)]
    report, _ = _run(lg, monkeypatch, items, [VENDOR, CHATTER])
    vendor, chatter = _block(report, VENDOR["link"]), _block(report, CHATTER["link"])
    assert "### Not a fit" in report and "Do not contact" in vendor and "selling, promoting" in vendor
    assert "### Weak signals — market intelligence only" in report and "Do not contact" in chatter
    assert "Draft to edit" not in report and "buy our engine" not in report and "partner up" not in report
    assert "How to approach" not in chatter                           # market intelligence: no outreach plan
    assert "**Posted:** date not available" in vendor                  # no date is invented


def test_a_draft_needs_real_content(env, monkeypatch):
    lg, _, _ = env
    items = [_item(2, AGENCY["title"], "qualified", "prospect", fit="fits", draft="  hi  ")]
    report, _ = _run(lg, monkeypatch, items, [AGENCY])
    assert "Draft to edit" not in report and AGENCY["link"] in report


# ── analysis input and guidance rules ────────────────────────────────────────
def test_analysis_input_carries_original_dates_only_when_known(env):
    lg, _, _ = env
    signals = [{"title": "Need a CRM", "post_text": "Looking for a CRM", "post_url": "u1",
                "published_at": "2026-10-12T09:15:00+00:00"},
               {"title": "No date here", "post_text": "Also need one", "post_url": "u2"},
               {"title": "Garbage date", "post_text": "text", "post_url": "u3", "published_at": "yesterday-ish"}]
    posts = lg.build_claude_post_context(signals)
    assert posts[0] == {"title": "Need a CRM", "text": "Looking for a CRM", "posted_utc": "2026-10-12 09:15 UTC"}
    assert posts[1] == {"title": "No date here", "text": "Also need one"} and "posted_utc" not in posts[2]
    block = lg._format_posts_block(posts)
    assert "[Post 1]\nTitle: Need a CRM\nPosted: 2026-10-12 09:15 UTC\nText: Looking for a CRM" in block
    assert block.count("Posted:") == 1


def test_guidance_note_demands_evidence_classes_samples_and_honesty(env):
    lg, _, _ = env
    note = lg.build_guidance_note("How do I get customers for my booking engine?")
    for needle in ("competitors / vendors / service providers", "Self-promotion by a provider is never counted as "
                   "buyer demand", "small sample", "observations", "hypotheses", "next steps", "fit is not verified",
                   "never invent or estimate a date", "Never present a strategy as \"the best\"",
                   "do not fill the gap with general business advice"):
        assert needle in note, needle


# ── Option A through the real route: same posts, their dates, no search ──────
def test_option_a_shows_stored_dates_and_starts_no_search(flow):
    lg, state, jobs, send, chats = flow
    doc = chats.docs[("c1", "o1")]
    doc["messages"][0]["results"][0]["published_at"] = RECENT.isoformat()
    state["router"] = {"intent": "previous_results", "previous_results_numbers": None}
    report = _report(send("Option A"))
    assert jobs == []
    assert f"**Posted:** {RECENT.strftime('%b')} {RECENT.day}, {RECENT.year} (UTC)" in _block(report, ORIGINAL_URLS[0])
    assert "**Posted:** date not available" in _block(report, ORIGINAL_URLS[1])
    sent = json.loads(state["outreach_calls"][0].split("Posts (JSON):\n", 1)[1])
    assert sent[0]["posted"] == RECENT.date().isoformat() and sent[1]["posted"] is None
    assert chats.docs[("c1", "o1")]["messages"][0]["results"][1] == RESULTS[1]          # stored results untouched


@pytest.mark.parametrize("text", ["Use the same results", "Write outreach for those posts", "A", "1", "pehla wala"])
def test_follow_up_transformations_never_search(flow, text):
    lg, state, jobs, send, chats = flow
    state["router"] = {"intent": "previous_results", "previous_results_numbers": None}
    doc = send(text)
    assert jobs == [] and doc["messages"][-1]["message_type"] == "chat"
