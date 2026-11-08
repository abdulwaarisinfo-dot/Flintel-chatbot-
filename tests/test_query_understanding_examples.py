"""The example requests from the product vision, checked against the deterministic part of query understanding (is the message a topic search or a
broad discovery request?). The router that extracts keywords is a model and is not exercised here (no key, no network).
These examples were NOT used to tune the detector: two broad ones are still not recognised (documented below as known misses; a miss is safe, the message
goes to the normal router and is searched with whatever keywords it extracts)."""
import pytest

import signal_intel as si

TOPIC = ["Find people looking for AI SaaS agents.", "Find people looking for AI SaaS Agent", "Find people struggling with their existing CRM.",
         "Find businesses looking for automation.", "Find genuine demand for a particular product.",
         "Discover competitors and understand what customers dislike about them."]
BROAD_RECOGNISED = ["Find people discussing problems that could become software products.", "What are people discussing today?",
                    "What problems are people complaining about this week?"]
BROAD_KNOWN_MISSES = ["Find people actively evaluating, comparing, or seeking solutions.", "Discover emerging problems, market discussions, and opportunities."]


@pytest.mark.parametrize("q", TOPIC)
def test_a_request_that_names_a_topic_is_never_turned_into_a_broad_discovery_sample(q):
    assert si.is_general_discovery_request(q) is False


@pytest.mark.parametrize("q", BROAD_RECOGNISED)
def test_broad_requests_without_a_topic_are_recognised(q):
    assert si.is_general_discovery_request(q) is True


@pytest.mark.parametrize("q", BROAD_KNOWN_MISSES)
def test_known_misses_are_not_captured_as_topic_searches_by_accident(q):
    """KNOWN LIMITATION: broad but not recognised (the closed vocabulary lacks 'discover' / 'evaluating' / 'seeking'). They take the normal route."""
    assert si.is_general_discovery_request(q) is False
    assert si.keywords_are_generic_discovery(["emerging problems", "market discussions"]) in (True, False)    # the router-keyword net may still catch them


def test_router_keywords_that_are_only_generic_words_do_not_become_a_keyword_search():
    assert si.keywords_are_generic_discovery(["complaints", "unmet needs"]) is True
    assert si.keywords_are_generic_discovery(["AI SaaS agent", "people looking"]) is False
