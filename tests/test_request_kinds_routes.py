"""
tests/test_request_kinds_routes.py — the real POST /search handler (routes.search) with the scripted router: the request kind is
stored on the message, credits are charged as for any search, a brand mention queues its collector job with the literal subject,
and a request that names a PERSON queues no collector job at all (it only searches posts that are already stored).
"""
import pytest

from tests.test_answer_persistence import env  # noqa: F401  (fixture used by `app`)

from tests.test_search_credit_routes import app, flat_cost, credits_doc, used, KEY1, KEY2, SEARCH_ROUTER  # noqa: F401
import search_credits as sc

PERSON = dict(SEARCH_ROUTER, keywords=["Hamza Ali"], match_phrases=None, request_kind="person_mentions", subject="Hamza Ali",
              resolved_query="Find Reddit posts mentioning Hamza Ali.")
BRAND = dict(SEARCH_ROUTER, keywords=["Acme"], match_phrases=None, request_kind="mention_search", subject="Acme",
             resolved_query="Find Reddit posts mentioning Acme.")
RESEARCH = dict(SEARCH_ROUTER, keywords=["shopify abandoned cart complaints"], request_kind="research", subject=None,
                resolved_query="Research complaints about Shopify abandoned carts.")


def _message(a, resp):
    chat_id = resp.headers["location"].rsplit("/", 1)[-1]
    return a["db"].chats.find_one({"chat_id": chat_id})["messages"][-1]


@pytest.mark.usefixtures("flat_cost")
def test_a_person_mention_request_is_stored_charged_once_and_queues_no_collector_job(app):
    app["state"]["router"] = PERSON
    resp = app["send"]("Find Reddit posts mentioning Hamza Ali.", submit_key=KEY1)
    assert resp.status_code == 303
    msg = _message(app, resp)
    assert msg["request_kind"] == "person_mentions" and msg["subject"] == "Hamza Ali" and msg["keywords"] == ["Hamza Ali"]
    assert app["state"]["enqueues"] == [] and app["db"].jobs.count_documents({}) == 0       # no new collection about a person
    assert used(app) == 1 and sc.check_invariant(credits_doc(app)) == []                      # an accepted search is charged exactly once
    again = app["send"]("Find Reddit posts mentioning Hamza Ali.", submit_key=KEY1)         # a replay: no second charge, no job
    assert again.status_code == 303 and used(app) == 1 and app["state"]["enqueues"] == []


@pytest.mark.usefixtures("flat_cost")
def test_a_brand_mention_request_queues_its_job_with_the_literal_subject(app):
    app["state"]["router"] = BRAND
    resp = app["send"]("Find Reddit posts mentioning Acme.", submit_key=KEY1)
    msg = _message(app, resp)
    assert msg["request_kind"] == "mention_search" and msg["subject"] == "Acme"
    assert len(app["state"]["enqueues"]) == 1
    job = app["db"].jobs.find_one({"topic_key": app["state"]["enqueues"][0][0]})
    assert job["keywords"] == ["Acme"]


@pytest.mark.usefixtures("flat_cost")
def test_research_and_lead_requests_behave_as_before_apart_from_the_stored_kind(app):
    app["state"]["router"] = RESEARCH
    msg = _message(app, app["send"]("Research complaints about Shopify abandoned carts.", submit_key=KEY1))
    assert msg["request_kind"] == "research" and len(app["state"]["enqueues"]) == 1
    app["state"]["router"] = SEARCH_ROUTER
    lead = _message(app, app["send"]("Find people looking for a CRM for small teams", submit_key=KEY2))
    assert "request_kind" not in lead and "subject" not in lead and len(app["state"]["enqueues"]) == 2
    assert used(app) == 2
