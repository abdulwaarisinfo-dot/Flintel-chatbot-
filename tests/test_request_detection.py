"""
tests/test_requtest_detection.py - answer_integrity._is_request: what counts as a request for a solution.

A request needs a request cue AND something that says what is wanted. Before this change that had to be a generic
noun (tool, app, software, CRM ...), so "Looking for an alternative to HubSpot" or "What do you recommend for
abandoned-cart emails?" were treated as no request: the post became a "discussion", its intent was forced to none,
and it could never qualify. Now a named alternative or an explicit recommendation ask also counts.

Pure functions on invented sentences; no network, no database, no model. These tests show the rule behaves as
designed. They say nothing about precision or recall on real posts, which have not been measured.
"""
import pytest

import answer_integrity as ai

QUERY = "Find people seeking alternatives to HubSpot because of pricing"


def _sig(title, text):
    return {"title": title, "post_text": text, "post_url": "https://www.reddit.com/r/SaaS/comments/abc123/x/",
            "platform": "reddit", "subreddit": "SaaS"}


def _classify(title, text, query=QUERY):
    """The model's own label says buyer / explicit / fits; classify_post can only lower it."""
    model = {"title": title, "role": "buyer", "purchase_intent": "explicit", "product_fit": "fits"}
    return ai.classify_post(_sig(title, text), model, ai.product_terms(query), bool(ai._ICP_MARKERS.search(query)), "map_record")


# ── sentences that are requests ───────────────────────────────────────────────────────────────────
POSITIVE = [
    "Looking for an alternative to HubSpot because of pricing.",
    "Any good alternatives to Mailchimp?",
    "I need an alternative to Zapier for our team.",
    "What are the best alternatives to Notion?",
    "Does anyone recommend a replacement for Asana?",
    "What do you recommend for abandoned-cart emails?",
    "Which do you recommend for scheduling posts?",
    "Any recommendations for abandoned-cart emails?",
    "Can anyone recommend something for invoicing?",
]


@pytest.mark.parametrize("sentence", POSITIVE)
def test_named_alternative_and_recommendation_asks_are_requests(sentence):
    assert ai._is_request(sentence), sentence


# ── controls: requests that already worked because they name a generic solution ─────────────────────
CONTROLS = ["Looking for a CRM.", "Which tool do you recommend for abandoned-cart emails?", "Any good tools for invoicing?"]


@pytest.mark.parametrize("sentence", CONTROLS)
def test_requests_with_a_generic_noun_are_unchanged(sentence):
    assert ai._is_request(sentence), sentence
    assert ai._REQUEST.search(sentence) and ai._SOLUTION.search(sentence)


# ── sentences that are NOT requests ─────────────────────────────────────────────────────────────────
NEGATIVE = [
    "Here are five alternatives to Salesforce that I tested last year.",   # listicle
    "I recommend these alternatives to Salesforce.",                       # recommending, not asking
    "The best alternatives to Notion in 2025.",
    "My top alternatives to HubSpot are listed below.",
    "Recommendations for abandoned-cart emails: here is what worked for us.",
    "Some recommendations for abandoned-cart emails.",
    "We switched from HubSpot to Zoho last month.",                        # plain statement of a switch
    "We moved off HubSpot because of pricing.",
    "I switched to Acme and it saved us hours every week.",                # testimonial
    "HubSpot raised prices again and support does not answer.",           # complaint without a request
    "We built an alternative to HubSpot.",                                 # promotion
    "What do you use for abandoned-cart emails?",                          # no recommendation cue: unchanged
    "Alternatives to Brand24? Our startup finds it too broad.",           # no ask cue: unchanged (see test_purchase_step G03)
]


@pytest.mark.parametrize("sentence", NEGATIVE)
def test_listicles_statements_testimonials_and_complaints_are_not_requests(sentence):
    assert not ai._is_request(sentence), sentence


def test_a_lower_case_product_name_is_not_detected():
    """Known limit, stated so it is not mistaken for coverage: the name must start with a capital or a digit."""
    assert not ai._is_request("Looking for an alternative to hubspot because of pricing.")


# ── role of the whole post ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("title,text", [
    ("Alternative to HubSpot?", "Looking for an alternative to HubSpot because of pricing."),
    ("Email for my shop", "Prices went up again. Any good alternatives to Mailchimp for a small shop?"),
    ("Abandoned carts", "What do you recommend for abandoned-cart emails? Trying to decide this month."),
])
def test_posts_with_these_requests_are_buyers(title, text):
    assert ai._detect_role(title, text)[0] == "buyer"


@pytest.mark.parametrize("title,text,role", [
    ("5 alternatives to Salesforce", "Here are five alternatives to Salesforce that I tested last year. Notes on pricing below.", "discussion"),
    ("We moved off HubSpot", "We moved off HubSpot last month because of pricing. The migration took two days.", "discussion"),
    ("HubSpot pricing", "HubSpot raised prices again and support does not answer. So frustrating.", "discussion"),
    ("Launch", "We built an alternative to HubSpot. Try our new tool, launch discount this week.", "competitor_promo"),
    ("Switched to Acme", "We switched to Acme and it saved us hours every week.", "testimonial"),
])
def test_other_posts_keep_their_role(title, text, role):
    assert ai._detect_role(title, text)[0] == role


def test_a_promotion_with_a_request_style_title_is_still_a_promotion():
    """A named-alternative title is also how promotions are headlined; it must not hide a promotional body."""
    role, sentence = ai._detect_role("Looking for an alternative to HubSpot?", "We built one. Try our new tool, launch discount this week.")
    assert role == "competitor_promo" and sentence == "We built one."


# ── what the label means for the user ─────────────────────────────────────────────────────────────
def test_a_named_alternative_request_is_no_longer_demoted_to_discussion():
    out = _classify("Alternative to HubSpot?", "Looking for an alternative to HubSpot because of pricing.")
    assert out["role"] == "buyer"
    assert not any("asks for no tool or solution" in u for u in out["uncertainty"])


def test_a_recommendation_ask_with_an_explicit_step_still_qualifies():
    out = _classify("What do you use for abandoned-cart emails?",
                    "Our budget was approved this week and we will subscribe to whatever works best. "
                    "What do you recommend for abandoned-cart emails?",
                    query="Find businesses looking for an email marketing platform")
    assert out["role"] == "buyer" and out["qualified"] is True


def test_researching_a_solution_is_not_a_purchase_step():
    """The request is detected, but a request alone never qualifies: no step, no lead."""
    out = _classify("Alternative to HubSpot?", "Looking for an alternative to HubSpot because of pricing.")
    assert out["role"] == "buyer" and out["qualified"] is False
    assert out["purchase_intent"] in ("none", "research")


def test_a_conditional_step_still_does_not_qualify_after_the_change():
    out = _classify("Alternative to HubSpot?",
                    "Looking for an alternative to HubSpot because of pricing. We will start a free trial on "
                    "Monday and subscribe if it works.")
    assert out["role"] == "buyer" and out["qualified"] is False


def test_the_ceiling_needs_a_request_and_now_accepts_these_requests():
    step = "Our budget was approved this week."
    assert ai._intent_ceiling("Email for my shop", f"{step} What do you recommend for abandoned-cart emails?") == "explicit"
    assert ai._intent_ceiling("Email for my shop", f"{step} I recommend these alternatives to Salesforce.") == "none"
    assert ai._intent_ceiling("Notes", f"{step}") == "none"          # a step with no request has no ceiling


# ── corrections after review: false positives found in the first version of the new patterns ─────────
# Each sentence below was a "request" under the first version (and "discussion" before this work).
FALSE_REQUESTS = [
    # general advice or unrelated suggestions are not product requests
    "Looking for advice on dealing with a difficult client.",
    "I need some suggestions for names for my newsletter.",
    "I need advice on pricing my newsletter.",   # (a generic noun such as "SaaS" would still match the old noun-based rule)
    # negated asks
    "I do not have any recommendations for abandoned-cart emails.",
    "We don't need an alternative to HubSpot, it works fine.",
    "I don't want alternatives to Zapier, I just want it fixed.",
    "We do not need alternatives to Salesforce.",
    # incidental "any" / "what" / "which"
    "I don't think any of the alternatives to Salesforce are good.",
    "Any of the alternatives to Salesforce would be fine by me.",
    "That is what I tell people: alternatives to Salesforce are overrated.",
    "I will show which alternatives to Salesforce are worth it.",
    # a past switching story
    "We were looking for an alternative to HubSpot and switched to Acme.",
    "I was looking for an alternative to Zapier and then moved to Make.",
    "We had been searching for an alternative to HubSpot for months.",
]


@pytest.mark.parametrize("sentence", FALSE_REQUESTS)
def test_corrected_false_positives_are_not_requests(sentence):
    assert not ai._is_request(sentence), sentence


@pytest.mark.parametrize("title,text", [
    ("Client", "Looking for advice on dealing with a difficult client."),
    ("Names", "I need some suggestions for names for my newsletter."),
    ("Tools", "I do not have any recommendations for abandoned-cart emails."),
    ("HubSpot", "We don't need an alternative to HubSpot, it works fine."),
    ("Zapier", "I don't want alternatives to Zapier, I just want it fixed."),
    ("Salesforce", "I don't think any of the alternatives to Salesforce are good."),
    ("Salesforce", "That is what I tell people: alternatives to Salesforce are overrated."),
    ("Salesforce", "I will show which alternatives to Salesforce are worth it."),
])
def test_corrected_false_positive_posts_are_discussion_again(title, text):
    assert ai._detect_role(title, text)[0] == "discussion"


def test_a_past_switching_story_is_not_a_current_request():
    """"Looking for an alternative" in the past tense, already settled in the same sentence, is a story, not an ask."""
    role, _ = ai._detect_role("Our move", "We were looking for an alternative to HubSpot and switched to Acme. It saved us hours every week.")
    assert role == "discussion"
    # the same words in the present tense are still an ask
    assert ai._is_request("We are looking for an alternative to HubSpot because of pricing.")
    assert ai._detect_role("Our move", "We are looking for an alternative to HubSpot because of pricing.")[0] == "buyer"


# ── the guards must not swallow real asks ───────────────────────────────────────────────────────────
STILL_REQUESTS = [
    "We are looking for an alternative to HubSpot because of pricing.",
    "I am not sure which one, but any good alternatives to Mailchimp?",     # the negation sits in an earlier clause
    "What are the best alternatives to Notion?",
    "Which alternatives to Asana would you recommend?",
    "And what are some alternatives to Trello?",
    "Searching for a replacement for Asana, any ideas?",
    "Does anyone know good alternatives to Mailchimp?",
    "Looking for recommendations for abandoned-cart emails.",
]


@pytest.mark.parametrize("sentence", STILL_REQUESTS)
def test_genuine_asks_survive_the_guards(sentence):
    assert ai._is_request(sentence), sentence


def test_a_promotion_with_a_request_in_the_body_is_still_a_promotion():
    role, _ = ai._detect_role("Launch", "Looking for an alternative to HubSpot? Try our new product today.")
    assert role == "competitor_promo"
    role, _ = ai._detect_role("Looking for an alternative to HubSpot?", "We built one. Try our new tool, launch discount this week.")
    assert role == "competitor_promo"


# ── final correction: restore the testimonial, drop second-person / third-party / rhetorical / listicle false asks,
#    and fix the guards (present perfect, unrelated negation, completed stories) ───────────────────────────────
# Priority 1: classification that existed before this work must not change.
def test_a_request_phrase_followed_by_a_switching_success_story_is_still_a_testimonial():
    """It was a testimonial before this work. A request phrase without a generic noun must not hide it."""
    role, _ = ai._detect_role("Acme", "If you are looking for an alternative to HubSpot, we switched to Acme and it saved us hours every week.")
    assert role == "testimonial"


def test_both_promotion_protections_still_hold():
    assert ai._detect_role("Looking for an alternative to HubSpot?", "We built one. Try our new tool, launch discount this week.")[0] == "competitor_promo"
    assert ai._detect_role("Launch", "Looking for an alternative to HubSpot? Try our new product today.")[0] == "competitor_promo"


# Priority 2: sentences that mention alternatives to a named product without being the author's own ask.
NOT_THE_AUTHORS_ASK = [
    "Why would anyone look for alternatives to HubSpot?",                                 # rhetorical
    "If you are looking for an alternative to HubSpot, try Acme.",                       # second-person marketing
    "If you are looking for an alternative to HubSpot, Acme has simple pricing.",
    "Anyone looking for alternatives to HubSpot should read the comparison linked below.",
    "You should be looking for an alternative to HubSpot.",
    "My clients are looking for alternatives to HubSpot.",                                # third party
    "People who are looking for alternatives to HubSpot usually want lower prices.",
    "Many teams are looking for alternatives to HubSpot.",
    "They are looking for an alternative to HubSpot.",
    "My boss wants an alternative to HubSpot.",
    "He needs an alternative to HubSpot.",
]


@pytest.mark.parametrize("sentence", NOT_THE_AUTHORS_ASK)
def test_rhetorical_second_person_and_third_party_sentences_are_not_requests(sentence):
    assert not ai._is_request(sentence), sentence


@pytest.mark.parametrize("title,text,role", [
    ("What are the best alternatives to Notion in 2025?", "Here is our ranking of seven options with pricing and notes.", "discussion"),   # question listicle
    ("Looking for an alternative to HubSpot?", "Meet Acme, a CRM built for small teams with simple pricing.", "discussion"),               # soft promotion
    ("What are the best alternatives to Notion?", "", "buyer"),                                                                         # title-only ask
    ("What are the best alternatives to Notion?", "I run a small team and our budget is limited. We need something simple.", "buyer"),   # personal context
    ("Alt", "Looking for an alternative to HubSpot because of pricing.", "buyer"),
])
def test_a_question_headline_is_a_buyer_only_when_the_post_is_personal(title, text, role):
    assert ai._detect_role(title, text)[0] == role


def test_a_named_alternative_ask_is_still_not_a_lead_without_a_purchase_step():
    ask = "Looking for an alternative to HubSpot because of pricing."
    assert _classify("Alt", ask)["qualified"] is False                                           # request alone: no lead
    assert _classify("Alt", f"Our budget was approved this week. {ask}")["qualified"] is True    # existing rule still applies
    for not_an_ask in ("If you are looking for an alternative to HubSpot, try Acme.", "My clients are looking for alternatives to HubSpot."):
        out = _classify("Tip", f"Our budget was approved this week. {not_an_ask}")
        assert out["role"] == "discussion" and out["qualified"] is False


# Priority 3: the guards.
GENUINE_ASKS = [
    "I have been looking for an alternative to HubSpot for weeks.",                      # present perfect is not "past and settled"
    "I've been searching for an alternative to HubSpot for weeks.",
    "We're looking for an alternative to HubSpot because of pricing.",
    "Our team is looking for an alternative to HubSpot.",
    "My company needs an alternative to Zapier.",
    "My team and I are looking for an alternative to HubSpot.",
    "Currently looking for alternatives to Mailchimp for our shop.",
    "Hi all, looking for an alternative to HubSpot.",
    "I haven't found anything so I'm looking for an alternative to HubSpot.",            # unrelated negation earlier in the sentence
    "Not sure what to use — any good alternatives to Mailchimp?",                   # negation in an earlier clause (dash)
    "We moved to Zoho last year but are now looking for an alternative to Zoho.",       # a story BEFORE the ask does not cancel it
    "Looking for an alternative to HubSpot because we moved to remote work.",
    "Do you have any good alternatives to Mailchimp?",
]


@pytest.mark.parametrize("sentence", GENUINE_ASKS)
def test_genuine_first_person_and_question_asks_remain_detectable(sentence):
    assert ai._is_request(sentence), sentence


COMPLETED_OR_NEGATED = [
    "There is no reason whatsoever for us to even consider looking for an alternative to HubSpot.",   # long-distance negation
    "After searching for alternatives to HubSpot we chose Acme.",                                    # completed story, no conjunction
    "I spent a week looking for an alternative to HubSpot and finally picked Acme.",                 # completed story, adverb
    "We were looking for an alternative to HubSpot and switched to Acme.",
]


@pytest.mark.parametrize("sentence", COMPLETED_OR_NEGATED)
def test_negation_and_completed_stories_are_not_requests(sentence):
    assert not ai._is_request(sentence), sentence


def test_i_have_been_looking_is_a_buyer_post():
    assert ai._detect_role("Alt", "I have been looking for an alternative to HubSpot for weeks.")[0] == "buyer"


def test_generic_noun_requests_are_deliberately_unchanged(): 
    """The generic-noun path does not use the new checks and behaves exactly as before this work, including its known
    weaknesses (a negated, past, second-person or aggregate sentence with a noun is still a request). Pinned here so
    any change to it is deliberate."""
    for sentence in ("Looking for a CRM.", "We don't need a CRM, the spreadsheet works fine.",
                     "We were looking for a CRM and switched to Acme.", "If you are looking for a CRM, try Acme.",
                     "Anyone looking for a CRM should read the comparison below.", "Which tool do you recommend for abandoned-cart emails?"):
        assert ai._is_request(sentence) == bool(ai._REQUEST.search(sentence) and ai._SOLUTION.search(sentence)) is True, sentence


def test_the_testimonial_check_keeps_its_original_noun_based_condition():
    """Pins the line itself, not just the second-person case: even with a live first-person ask in the same sentence,
    a sentence that matches the testimonial pattern stays a testimonial, exactly as before this work. (Trade-off: such a
    mixed sentence is not treated as a buyer; the benefit is that existing classification does not change.)"""
    role, _ = ai._detect_role("Acme", "I switched to Acme and it saved us hours every week, now I am looking for an alternative to Acme.")
    assert role == "testimonial"
