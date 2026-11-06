"""Re-audit of the five-class classifier (rules-2): the assignment's own examples, conservative edges, evidence rules, separate scores.
Rule-based on synthetic phrasing. This proves the rules do what they say; it is NOT a measurement of accuracy on real discussions."""
import pytest

import signal_intel as si

URL = "https://a.test/1"


def cls(text, query="invoicing tool", **extra):
    return si.classify_post(dict({"title": text[:60], "post_text": text, "post_url": URL, "platform": "hackernews"}, **extra), query)


@pytest.mark.parametrize("text,expected", [
    ("I need a tool that can automate my invoices", si.DIRECT_DEMAND),
    ("My current invoicing software is too expensive", si.PROBLEM_SIGNAL),
    ("Is anyone else looking for an affordable invoicing tool?", si.MARKET_SIGNAL),
    ("I switched from Product A to Product B because the pricing was better", si.COMPETITOR_SIGNAL),
    ("Generic thoughts on business software trends this year", si.GENERAL_DISCUSSION),
    ("Looking for a recommendation for invoicing software, budget $50", si.DIRECT_DEMAND),
    ("I hate this invoicing software, so slow", si.PROBLEM_SIGNAL),
    ("We need a developer to build a CRM", si.DIRECT_DEMAND),
    ("Show HN: I built an invoicing tool", si.COMPETITOR_SIGNAL),
])
def test_the_five_classes(text, expected):
    assert cls(text)["class"] == expected


@pytest.mark.parametrize("text", [
    "I don't need an invoicing tool", "I used to need a CRM but not anymore", "What is the best way to learn Python?",
    "The invoicing software market is growing", "Anyone know why my invoice PDF is blank?", "", "   "])
def test_uncertain_or_negated_text_falls_back_to_general_discussion(text):
    c = cls(text)
    assert c["class"] == si.GENERAL_DISCUSSION and c["quote"] is None


def test_a_question_is_not_purchase_intent():
    assert cls("Is anyone else looking for an affordable invoicing tool?")["class"] != si.DIRECT_DEMAND
    assert cls("What do people use for invoicing?")["class"] != si.DIRECT_DEMAND


def test_a_complaint_is_never_promoted_to_a_buyer():
    for t in ("My current invoicing software is too expensive", "I hate this invoicing software, so slow"):
        assert cls(t)["class"] != si.DIRECT_DEMAND


@pytest.mark.parametrize("text", ["I need a tool that can automate my invoices", "My current invoicing software is too expensive",
                                  "I switched from Product A to Product B because the pricing was better", "Looking for a recommendation for invoicing software"])
def test_every_quote_is_an_exact_substring_of_the_post(text):
    c = cls(text)
    assert c["quote"] and c["quote"] in text


def test_no_class_without_evidence_has_a_quote_or_confidence_above_the_cap():
    c = cls("Generic thoughts on business software trends")
    assert c["quote"] is None and c["scores"]["evidence_confidence"] <= 0.6 and c["scores"]["intent_strength"] == 0.0


def test_the_five_dimensions_are_separate_and_present():
    s = cls("I need a tool that can automate my invoices")["scores"]
    assert set(s) == {"topic_relevance", "use_case_relevance", "intent_strength", "commercial_relevance", "evidence_confidence"}
    off_topic = cls("I need a tool that can automate my invoices", query="kubernetes operators")["scores"]
    assert off_topic["topic_relevance"] < s["topic_relevance"] and off_topic["intent_strength"] == s["intent_strength"]   # topic != intent


def test_classification_does_not_depend_on_the_retrieval_path_or_input_order():
    t = "I switched from Product A to Product B because the pricing was better"
    base = {k: v for k, v in cls(t).items() if k != "retrieval_path"}
    for path in ("live", "historical", "both"):
        assert {k: v for k, v in cls(t, retrieval_path=path).items() if k != "retrieval_path"} == base


def test_title_only_posts_are_flagged_and_capped():
    c = si.classify_post({"title": "I need a tool that can automate my invoices", "post_text": "", "post_url": URL}, "invoices")
    assert c["text_source"] == "title" and any("title" in x.lower() for x in c["limitations"])


def test_classifier_version_is_reported():
    assert cls("x")["classifier"] == "rules-3"


@pytest.mark.parametrize("text", ["What do you use for time tracking?", "which tool do people use for a small agency invoices?", "what do you guys use for customer surveys"])
def test_a_what_do_you_use_question_is_never_read_as_idea_validation(text):
    """Found by the fresh v3 evaluation: 'do you use' was in the validation pattern, which turned recommendation requests into MARKET_SIGNAL."""
    assert cls(text)["rule"] != "validation" and cls(text)["class"] != si.MARKET_SIGNAL


def test_asking_whether_people_actually_use_something_is_still_validation():
    assert cls("Do people actually use AI note takers in client meetings?")["class"] == si.MARKET_SIGNAL
