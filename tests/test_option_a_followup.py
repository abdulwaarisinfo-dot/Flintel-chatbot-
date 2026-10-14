"""
tests/test_option_a_followup.py — follow-up fixes after the second Option A
review:

  * offers are found in every DISPLAYED answer field (followup_question,
    business_insight, executive_summary, conclusion, …), lettered or numbered;
  * natural picks incl. Roman Urdu; "I want a" / "do a" / "number 1" are not
    picks; "Option A, but find five more buyers" never becomes a silent report;
  * the previous-results block is only sent when needed;
  * buyer classification: business owners are not sellers, common buying
    questions count, "interested" tier, no formal quote required;
  * evidence matching keeps Unicode, accepts spelling variants, and rejects
    notes that could belong to another post;
  * Option A / Option B, refinements, multiple searches, routing exceptions.

Same stub setup as test_option_a_outreach.py. No real LLM / Mongo / network.
"""
import json
import sys

import pytest

from tests.test_answer_persistence import env  # noqa: F401  (fixture)
from tests.test_option_a_outreach import ANSWER, ORIGINAL_URLS, POSTS, flow  # noqa: F401
from tests.test_option_a_guard import SEARCH_ROUTER, _item, _report, _search_msg, _sent_posts  # noqa: F401

REUSE_ROUTER = {"intent": "previous_results", "previous_results_numbers": None}
LETTERED = ("Would you prefer (A) the five posts formatted as outreach targets with suggested messaging, "
            "or (B) broader continuous monitoring for more CRM-intent posts?")
NUMBERED = ("Want me to do (1) outreach notes for these posts, or (2) a broader search for more CRM buyers?")


def _set_answer(chats, **fields):
    ans = dict(ANSWER)
    ans.pop("followup_question", None)
    ans.update(fields)
    chats.docs[("c1", "o1")]["messages"][0]["claude_answer"] = json.dumps(ans)


def _section(report, heading):
    return report.split(heading, 1)[1].split("\n### ", 1)[0] if heading in report else ""


# ── offer detection: every displayed field, lettered and numbered ────────────
@pytest.mark.parametrize("field", ["followup_question", "business_insight", "executive_summary", "conclusion",
                                   "market_pattern"])
def test_offer_found_in_any_displayed_field_and_never_searches(flow, field):
    lg, state, jobs, send, chats = flow
    _set_answer(chats, **{field: LETTERED})
    state["router"] = SEARCH_ROUTER                                      # router mislabels the pick
    send("Option A is better")
    assert jobs == [] and len(state["outreach_calls"]) == 1
    assert LETTERED in state["router_calls"][0]                         # the offer reached the router too


def test_offer_in_followups_list_and_plain_text_answers(env):
    lg, _, _ = env
    assert lg.find_offer_in_answer(json.dumps({"format": "source_list", "followups": ["x", LETTERED]}))[1]["options"]
    assert lg.find_offer_in_answer("Here you go. " + LETTERED)[1]["options"]


def test_numbered_offer_and_numbered_pick(flow):
    lg, state, jobs, send, chats = flow
    _set_answer(chats, followup_question=NUMBERED)
    state["router"] = SEARCH_ROUTER
    send("1")
    assert jobs == [] and len(state["outreach_calls"]) == 1


def test_numbered_list_without_an_offer_is_not_an_offer(env):
    lg, _, _ = env
    assert lg.parse_offer("Two patterns stand out: (1) pricing complaints, (2) onboarding gaps.")["options"] == {}
    assert lg.parse_offer("(A) pricing (B) onboarding")["options"] == {}


# ── picks ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,letters", [
    ("Option A is better", ["a"]), ("A", ["a"]), ("a", ["a"]), ("a please", ["a"]), ("(A)", ["a"]),
    ("I want A", ["a"]), ("I think option A", ["a"]), ("definitely A", ["a"]), ("A plz", ["a"]),
    ("go with B", ["b"]), ("let's do option A", ["a"]), ("I'd go with A", ["a"]), ("A sounds good", ["a"]),
    ("first option", ["a"]), ("the 2nd one", ["b"]), ("option 2", ["b"]), ("1", ["a"]),
    ("mujhe A chahiye", ["a"]), ("A wala theek hai", ["a"]), ("pehla wala", ["a"]), ("dusra option", ["b"]),
    ("A karo", ["a"]), ("A behtar hai", ["a"]), ("both", ["a", "b"]), ("dono", ["a", "b"]),
    ("A and B", ["a", "b"]),
])
def test_bare_picks(env, text, letters):
    lg, _, _ = env
    assert lg.parse_option_pick(text) == letters


@pytest.mark.parametrize("text", ["I want a", "do a", "I need a", "number 1", "post 2", "Option A, but find five more buyers",
                                  "Option A for the USA", "a CRM for dentists", "find 5 more", "yes", "ok thanks",
                                  "option 5", ""])
def test_not_bare_picks(env, text):
    lg, _, _ = env
    assert lg.parse_option_pick(text) is None


# ── refinements / Option B / routing exceptions ──────────────────────────────
@pytest.mark.parametrize("text", ["Option A, but find five more buyers", "Do outreach notes but also find more posts",
                                  "outreach for these and search last 30 days too"])
def test_reuse_plus_new_research_is_asked_never_silent(flow, text):
    lg, state, jobs, send, chats = flow
    _set_answer(chats, followup_question=LETTERED)
    state["router"] = REUSE_ROUTER                                       # router would silently reuse
    report = _report(send(text))
    assert jobs == [] and state["outreach_calls"] == []
    assert "outreach notes for the 5 posts I already showed you, or a new search" in report


@pytest.mark.parametrize("text", ["Option A, but find five more buyers", "Now in USA", "find 10 more"])
def test_refinements_still_search_when_router_says_search(flow, text):
    lg, state, jobs, send, chats = flow
    _set_answer(chats, followup_question=LETTERED)
    state["router"] = SEARCH_ROUTER
    send(text)
    assert len(jobs) == 1 and state["outreach_calls"] == []


def test_option_b_searches_and_option_a_does_not(flow):
    lg, state, jobs, send, chats = flow
    _set_answer(chats, business_insight=LETTERED)
    state["router"] = SEARCH_ROUTER
    send("A wala theek hai")                                             # reuse: report, no search
    assert jobs == [] and len(state["outreach_calls"]) == 1
    send("go with B")                                                    # the offer is still the latest result
    assert len(jobs) == 1 and len(state["outreach_calls"]) == 1


@pytest.mark.parametrize("text,expect_report", [("2", False), ("pehla wala", True)])
def test_routing_exception_with_numbered_and_urdu_picks(flow, monkeypatch, text, expect_report):
    lg, state, jobs, send, chats = flow
    _set_answer(chats, followup_question=NUMBERED)
    rt = sys.modules["routes"]
    monkeypatch.setattr(rt, "classify_and_maybe_chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    report = _report(send(text))
    assert jobs == []
    if expect_report:
        assert len(state["outreach_calls"]) == 1
    else:
        assert state["outreach_calls"] == [] and "(2)" in report and "runs a new search" in report


# ── previous-results block only when needed ──────────────────────────────────
def test_block_not_sent_for_plain_refinement_without_offer(flow):
    lg, state, jobs, send, chats = flow
    _set_answer(chats)                                                   # no offer anywhere
    state["router"] = SEARCH_ROUTER
    send("Now in USA")
    assert "Latest research result" not in state["router_calls"][0]


def test_block_sent_when_message_is_about_the_shown_posts(flow):
    lg, state, jobs, send, chats = flow
    _set_answer(chats)
    state["router"] = REUSE_ROUTER
    send("write outreach notes for these posts")
    assert "Latest research result" in state["router_calls"][0] and len(state["outreach_calls"]) == 1 and jobs == []


def test_router_cannot_reuse_without_the_block(flow):
    lg, state, jobs, send, chats = flow
    _set_answer(chats)
    state["router"] = REUSE_ROUTER                                       # not offered -> rejected -> retried
    send("Now in USA")
    assert state["outreach_calls"] == []


# ── multiple searches ────────────────────────────────────────────────────────
CAR = [("cars", "Which used SUV should I buy?", "https://www.reddit.com/r/cars/comments/c1/",
        "Budget 20k, looking for a reliable used SUV."),
       ("whatcarshouldIbuy", "First car advice", "https://www.reddit.com/r/whatcarshouldIbuy/comments/c2/",
        "Need recommendations for a first car.")]


def test_newest_search_offer_in_business_insight_wins(flow):
    lg, state, jobs, send, chats = flow
    newer = _search_msg(CAR, offer=None, query="Find car buyers")
    ans = json.loads(newer["claude_answer"])
    ans.pop("followup_question", None)
    ans["business_insight"] = NUMBERED
    newer["claude_answer"] = json.dumps(ans)
    chats.docs[("c1", "o1")]["messages"].append(newer)
    state["router"] = SEARCH_ROUTER
    state["outreach"] = json.dumps({"posts": [], "missing_info": []})
    send("first option")
    assert jobs == [] and [p["title"] for p in _sent_posts(state)] == [t for _s, t, _u, _x in CAR]


# ── honest classification ────────────────────────────────────────────────────
def _post(lg, title, text):
    return lg._clean_stored_post({"title": title, "post_text": text}, 0)


@pytest.mark.parametrize("title,text", [
    ("Need a CRM", "I run a small bakery and I'm looking for a CRM."),
    ("CRM", "Our startup is looking for a simple CRM, budget is tight."),
    ("CRM for agency", "My agency needs a CRM, any suggestions?"),
    ("Best CRM for real estate agents?", "Best CRM for a small real estate team?"),
    ("Leaving HubSpot", "We're looking to switch CRMs, HubSpot got too expensive."),
    ("Pipedrive vs HubSpot", "Pipedrive or HubSpot for a 5 person sales team?"),
    ("CRM chahiye", "Mujhe apne business ke liye CRM chahiye, kaunsa acha hai?"),
    ("سی آر ایم", "ہمیں اپنے کاروبار کے لیے سی آر ایم چاہیے"),
])
def test_ordinary_buyers_stay_qualified(env, title, text):
    lg, _, _ = env
    assert lg._honest_category("qualified", _item(1), _post(lg, title, text)) == ("qualified", None)


@pytest.mark.parametrize("title,text", [
    ("I built a CRM for freelancers", "I built a CRM app for freelancers, looking for beta testers."),
    ("Launch", "We just launched our CRM tool, check out our site."),
    ("Partners wanted", "Looking for agency partners, we pay referral fees."),
    ("Feedback?", "Would love feedback on my CRM extension."),
])
def test_sellers_and_user_seekers_are_not_prospects(env, title, text):
    lg, _, _ = env
    cat, note = lg._honest_category("qualified", _item(1), _post(lg, title, text))
    assert cat == "irrelevant" and "selling" in note


def test_model_seller_label_on_a_real_buyer_is_flagged_not_trusted(env):
    lg, _, _ = env
    cat, note = lg._honest_category("qualified", _item(1, author="seller"),
                                    _post(lg, "Need a CRM", "I run a bakery and need a CRM, any suggestions?"))
    assert cat == "ambiguous" and "asks to buy" in note


def test_interested_tier_and_no_upgrades(env):
    lg, _, _ = env
    p = _post(lg, "CRM recommendations?", "Can anyone recommend a CRM?")
    assert lg._honest_category("qualified", _item(1, need=None), p)[0] == "interested"
    assert lg._honest_category("interested", _item(1), p) == ("interested", None)
    assert lg._honest_category("weak", _item(1), p) == ("weak", None)              # never upgraded
    assert lg._honest_category("interested", _item(1, author="researcher"), p)[0] == "weak"


def test_qualified_without_a_formal_quote_via_title(flow):
    lg, state, jobs, send, chats = flow
    state["router"] = REUSE_ROUTER
    state["outreach"] = json.dumps({"posts": [_item(1, evidence=None, post_title=POSTS[0][1])], "missing_info": []})
    report = _report(send("Option A"))
    assert ORIGINAL_URLS[0] in _section(report, "### Qualified prospects")


# ── evidence matching ────────────────────────────────────────────────────────
@pytest.mark.parametrize("title,text,quote", [
    ("سی آر ایم چاہیے", "ہمیں چھوٹے کاروبار کے لیے سی آر ایم چاہیے", "چھوٹے کاروبار کے لیے سی آر ایم"),
    ("CRM चाहिए", "हमें अपने छोटे व्यवसाय के लिए CRM चाहिए", "छोटे व्यवसाय के लिए CRM"),
    ("نحتاج CRM", "نحتاج إلى نظام CRM لشركتنا الصغيرة", "نظام CRM لشركتنا الصغيرة"),
    ("Still looking", "Trialled two CRMs, both too complex.", "Trialed two CRMs, both too complex"),
    ("Still looking", "Trialled two CRMs, both too complex.", "trialled two crms both too complex"),
])
def test_quotes_verified_in_any_script_and_with_spelling_variants(env, title, text, quote):
    lg, _, _ = env
    assert lg._evidence_matches_post(quote, _post(lg, title, text))


@pytest.mark.parametrize("quote", ["Tried two CRMs, both were too complicated", "CRM?", None, "",
                                   "They want enterprise software for 500 seats"])
def test_unverifiable_quotes_are_rejected(env, quote):
    lg, _, _ = env
    assert not lg._evidence_matches_post(quote, _post(lg, "Still looking", "Trialled two CRMs, both too complex."))


def _two_similar_posts(chats):
    similar = [("smallbusiness", "CRM for small business", "https://www.reddit.com/r/smallbusiness/comments/s1/",
                "Looking for a CRM for my small business, any recommendations?"),
               ("Entrepreneur", "CRM for my shop", "https://www.reddit.com/r/Entrepreneur/comments/s2/",
                "Looking for a CRM for my small business. We sell candles.")]
    chats.docs[("c1", "o1")]["messages"].append(_search_msg(similar, query="Find CRM buyers"))
    return similar


def test_generic_quote_shared_by_two_posts_is_rejected(flow):
    lg, state, jobs, send, chats = flow
    similar = _two_similar_posts(chats)
    state["router"] = REUSE_ROUTER
    state["outreach"] = json.dumps({"posts": [_item(1, evidence="Looking for a CRM for my small business")],
                                    "missing_info": []})
    report = _report(send("Option A"))
    assert similar[0][2] in _section(report, "### Not assessed") and "also appears in post 2" in report


def test_title_echo_resolves_a_shared_quote(flow):
    lg, state, jobs, send, chats = flow
    similar = _two_similar_posts(chats)
    state["router"] = REUSE_ROUTER
    state["outreach"] = json.dumps({"posts": [_item(2, evidence="Looking for a CRM for my small business",
                                                    post_title="CRM for my shop")], "missing_info": []})
    report = _report(send("Option A"))
    assert similar[1][2] in _section(report, "### Qualified prospects")


def test_notes_naming_another_post_are_rejected(flow):
    lg, state, jobs, send, chats = flow
    state["router"] = REUSE_ROUTER
    state["outreach"] = json.dumps({"posts": [_item(1, post_title=POSTS[3][1], evidence=None)], "missing_info": []})
    report = _report(send("Option A"))
    assert ORIGINAL_URLS[0] in _section(report, "### Not assessed") and "name post 4" in report


def test_paraphrased_quote_is_hidden_but_title_still_attaches(flow):
    lg, state, jobs, send, chats = flow
    state["router"] = REUSE_ROUTER
    state["outreach"] = json.dumps({"posts": [_item(1, post_title=POSTS[0][1],
                                                    evidence="Tried two CRMs, both were too complicated")],
                                    "missing_info": []})
    report = _report(send("Option A"))
    section = _section(report, "### Qualified prospects")
    assert ORIGINAL_URLS[0] in section and "couldn't be verified" in section and "complicated" not in section
