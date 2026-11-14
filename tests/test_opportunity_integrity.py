"""Regression + invariant tests for the claim checks (report_claims), the opportunity categories / entity grouping / recency
(opportunity_layer) and their wiring into answer_integrity. Posts are synthetic fixtures modelled on the two reported failures
(a travel-software search and a CRM search); no network, no model, no database."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import answer_integrity as ai
import opportunity_layer as ol
import report_claims as rc

RD = "https://www.reddit.com/r/{}/comments/{}/t/"
NOW = datetime.now(timezone.utc)


def _post(sub, pid, title, text, author="someone", days=5):
    return {"title": title, "post_text": text, "post_url": RD.format(sub, pid), "platform": "reddit", "author": author,
            "published_at": (NOW - timedelta(days=days)).isoformat()}


def _answer(posts, query, summary="", role="buyer", intent="explicit", findings=None):
    data = {"format": "source_list", "executive_summary": summary, "key_findings": findings or [],
            "platforms": [{"platform": "reddit", "posts": [{"index": i + 1, "link": p["post_url"], "title": p["title"], "role": role,
                                                              "purchase_intent": intent, "summary": "The author requested a demo and a quote."}
                                                             for i, p in enumerate(posts)]}]}
    return ai.enforce_answer_integrity(data, posts, posts, query)


def _rows(out):
    return [p for pl in out["platforms"] for p in pl["posts"]]


NONPROFIT = _post("nonprofit", "n1", "Which CRM for a small nonprofit?", "We are a nonprofit with 5 staff. Can anyone recommend a CRM? Budget is $40/month.", "alice")
RECRUIT = _post("recruiting", "r1", "Recruiting CRM discussion", "I work in recruiting and wonder how people use a CRM day to day.", "bob")


# ── CRM report failures ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_summary_naming_companies_that_are_not_in_the_source_table_loses_that_sentence():
    out = _answer([NONPROFIT, RECRUIT], "Find people looking for CRM recommendations",
                  summary="SourceWhale and Loxo recruiters discuss recruiting tools. A nonprofit asked for a CRM.", role="buyer", intent="research")
    s = out["executive_summary"]
    assert "SourceWhale" not in s and "Loxo" not in s
    assert s.strip()                                                         # never left empty: the surviving sentence or the server line


def test_a_name_that_does_appear_in_a_displayed_post_is_kept():
    p = _post("crm", "x1", "Has anyone tried HubSpot for a nonprofit?", "We are comparing HubSpot with a spreadsheet. Any advice?", "carol")
    out = _answer([p], "Find people comparing CRM tools", summary="One author is comparing HubSpot with a spreadsheet.", intent="research")
    assert "HubSpot" in out["executive_summary"]


def test_a_demo_or_quote_request_is_removed_when_no_cited_post_makes_it():
    out = _answer([NONPROFIT], "Find CRM recommendations", summary="Post 1 requested a demo. Post 1 asked for CRM recommendations.", intent="research")
    s = out["executive_summary"]
    assert "demo" not in s.lower()
    assert "recommend" in s.lower()


def test_a_demo_request_the_author_really_made_is_kept():
    p = _post("crm", "d1", "Need a CRM demo", "We need a CRM and would like a demo from any vendor. Please reply.", "dave")
    out = _answer([p], "Find CRM buyers", summary="Post 1 asked for a demo.", intent="explicit")
    assert "demo" in out["executive_summary"].lower()


def test_a_vendor_offering_a_demo_does_not_support_a_demo_request_claim():
    p = _post("saas", "v1", "Show: FooCRM", "Hi, I built FooCRM. Book a free demo on our site and see pricing.", "foo_dev")
    assert not rc._request_supported("demo", p["post_text"])
    assert not rc._request_supported("pricing", p["post_text"])


def test_the_row_summary_does_not_claim_a_request_the_source_lacks():
    out = _answer([NONPROFIT], "Find CRM recommendations", intent="research")
    summary = _rows(out)[0]["summary"]
    assert "demo" not in summary.lower() and "quote" not in summary.lower()


# ── travel-software report failures ────────────────────────────────────────────────────────────────────────────────────────
OK1 = _post("opensource", "o1", "Openkoda: open source business application platform", "At Openkoda we build an open source platform. See our repo.", "okoda_team", days=400)
OK2 = _post("selfhosted", "o2", "Openkoda 1.7 released", "Openkoda release notes for the platform we build. Feedback welcome.", "okoda_team", days=380)
AIRLINE = _post("travel", "a1", "Why do airlines overbook flights?", "I was bumped from a flight last week. Interesting how airlines handle this.", "flyer", days=7)
PARTNERO = _post("saas", "p1", "Partnero referral program tool", "We built Partnero for referral programs. Check it out.", "partnero_dev", days=30)


def test_two_posts_by_one_vendor_are_one_entity_not_two_signals():
    out = _answer([OK1, OK2], "Find open source platform for building business applications", role="competitor_promo", intent="none")
    opp = out["opportunities"]
    rows = _rows(out)
    assert rows[0]["entity_id"] == rows[1]["entity_id"]
    assert opp["identity_groups"] == 1
    assert opp["multi_post_entities"] and opp["multi_post_entities"][0]["posts"] == 2


def test_an_integration_opportunity_needs_partnership_language_in_the_post():
    out = _answer([PARTNERO], "Find referral program tools",
                  summary="Partnero is a possible integration opportunity for the user.", role="competitor_promo", intent="none")
    assert "integration" not in out["executive_summary"].lower()
    assert _rows(out)[0]["opportunity_category"] == ol.COMPETITOR              # not a partnership without the author saying so


def test_an_author_who_asks_for_a_partnership_is_a_potential_partnership_not_a_buyer():
    p = _post("saas", "pp1", "Looking for integration partners", "We are looking for integration partners for our booking API. DM us.", "api_co")
    out = _answer([p], "Find travel booking integrations", role="competitor_promo", intent="none")
    assert _rows(out)[0]["opportunity_category"] == ol.PARTNER
    assert out["opportunities"]["confirmed_buyer_signals"] == 0


def test_an_airline_discussion_without_purchase_intent_is_never_a_buyer():
    out = _answer([AIRLINE], "Find travel booking software discussions", role="discussion", intent="none")
    row = _rows(out)[0]
    assert row["opportunity_category"] in (ol.GENERAL, ol.PROBLEM) and not row["current_opportunity"]
    assert out["opportunities"]["confirmed_buyer_signals"] == 0
    assert "No confirmed buyer signal" in out["opportunities"]["statement"]


def test_an_old_post_is_historical_and_never_a_current_opportunity():
    old = _post("crm", "h1", "Need a CRM for my agency", "I need a CRM for my agency. Budget is $100 a month and we plan to buy this week.", "erin", days=500)
    out = _answer([old], "Find CRM buyers", intent="explicit")
    row = _rows(out)[0]
    assert row["recency"] == "historical" and row["current_opportunity"] is False
    assert out["opportunities"]["historical_rows"] == 1


# ── invariants ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_confirmed_buyer_requires_a_verified_analysis_matched_row():
    for row in ({"verification_status": "unverified", "analysis_verified": True, "role": "buyer", "purchase_intent": "explicit"},
                {"verification_status": "verified", "analysis_verified": False, "role": "buyer", "purchase_intent": "explicit"},
                {"verification_status": "verified", "analysis_verified": True, "role": "buyer", "purchase_intent": "research"},
                {"verification_status": "verified", "analysis_verified": True, "role": "discussion", "purchase_intent": "none"}):
        assert ol.categorize(row, "") != ol.CONFIRMED
    assert ol.categorize({"verification_status": "verified", "analysis_verified": True, "role": "buyer", "purchase_intent": "explicit"}, "") == ol.CONFIRMED


def test_independent_buyers_with_similar_titles_are_not_merged():
    a = _post("crm", "i1", "Need a CRM for our shop", "We need a CRM. Budget $50.", "buyer_one")
    b = _post("crm", "i2", "Need a CRM for our shop", "Our team needs a CRM too. Budget $60.", "buyer_two")
    out = _answer([a, b], "Find CRM buyers", intent="explicit")
    assert out["opportunities"]["identity_groups"] == 2


def test_the_same_author_is_one_entity_even_when_the_titles_differ():
    a = _post("crm", "j1", "Need a CRM", "We need a CRM. Budget $50.", "same_person")
    b = _post("sales", "j2", "Which sales tool?", "Which sales tool is good? Budget $50.", "Same_Person")
    out = _answer([a, b], "Find CRM buyers", intent="research")
    assert out["opportunities"]["identity_groups"] == 1


def test_every_row_has_exactly_one_category_and_the_counts_add_up():
    out = _answer([NONPROFIT, RECRUIT, OK1, AIRLINE], "Find CRM recommendations", intent="research")
    opp = out["opportunities"]
    assert sum(opp["categories"].values()) == opp["listed_posts"] == len(_rows(out))
    assert all(r["opportunity_category"] in ol.CATEGORIES for r in _rows(out))


def test_summary_sentences_never_cite_a_row_that_is_not_in_the_table():
    out = _answer([NONPROFIT], "Find CRM recommendations", summary="Post 1 asked for a CRM. Post 7 asked for a CRM too.", intent="research")
    assert "Post 7" not in out["executive_summary"] and "Post 1" in out["executive_summary"]


def test_the_claim_check_only_removes_sentences_it_never_adds_text():
    text = "A nonprofit asked for a CRM. Zoomly requested a demo."
    out = rc.drop_unsupported_claims(text, {1: "Which CRM for a nonprofit? We need a CRM."}, "CRM")
    assert out in ("A nonprofit asked for a CRM.", "")
    assert set(out.split()) <= set(text.split())


def test_no_displayed_rows_means_no_claim_check():
    assert rc.drop_unsupported_claims("Zoomly requested a demo.", {}, "") == "Zoomly requested a demo."


def test_recency_boundaries():
    assert ol.recency((NOW - timedelta(days=10)).isoformat(), NOW) == "current"
    assert ol.recency((NOW - timedelta(days=200)).isoformat(), NOW) == "historical"
    assert ol.recency(None, NOW) == "undated" and ol.recency("garbage", NOW) == "undated"


def test_a_negated_request_statement_is_kept_it_claims_no_request():
    rows = {1: "Which CRM for a nonprofit? We need a CRM."}
    assert rc.drop_unsupported_claims("No author asked for a demo.", rows, "CRM") == "No author asked for a demo."


def test_an_aggregate_sentence_that_names_nobody_is_left_to_the_other_checks():
    rows = {1: "Which CRM for a nonprofit? We need a CRM."}
    assert rc.drop_unsupported_claims("Leads want a demo.", rows, "CRM") == "Leads want a demo."


def test_a_dangling_post_reference_is_removed_but_a_year_or_publication_name_is_not():
    rows = {1: "Which CRM for a nonprofit? We need a CRM."}
    assert rc.drop_unsupported_claims("Post 9 asked for a CRM.", rows, "CRM") == ""
    assert rc.drop_unsupported_claims("The Washington Post 2024 covered CRM tools.", rows, "CRM Washington Post") != ""


# ── entity identity: verified author or verified canonical company ONLY ────────────────────────────────────────────────────
def _vendor(i, title, author=None, **extra):
    row = {"index": i, "link": f"https://x/{i}", "title": title, "role": "competitor_promo", "purchase_intent": "none",
           "verification_status": "verified", "analysis_verified": True, "relevance": "relevant", "source_id": f"s{i}"}
    post = dict({"title": title, "post_text": "", "author": author}, **extra)
    return row, post


def _ents(*pairs):
    rows = [r for r, _ in pairs]
    return ol.group_entities(rows, {r["link"]: p for r, p in pairs})


def test_two_unrelated_vendors_sharing_the_word_agencies_stay_separate():
    e = _ents(_vendor(1, "Show HN: Alpha CRM for Agencies", "alpha_dev"), _vendor(2, "Show HN: Beta CRM for Agencies", "beta_dev"))
    assert e[1] != e[2]


def test_two_unrelated_vendors_sharing_the_word_crm_stay_separate():
    e = _ents(_vendor(1, "Alpha CRM", "a1"), _vendor(2, "Beta CRM", "b1"), _vendor(3, "Gamma CRM tool", None))
    assert len(set(e.values())) == 3


def test_the_same_verified_author_with_different_titles_is_one_entity():
    e = _ents(_vendor(1, "Launching Alpha", "Alpha_Dev"), _vendor(2, "Alpha 2 released", "u/alpha_dev"))
    assert e[1] == e[2]


def test_an_explicitly_verified_canonical_company_groups_different_authors():
    e = _ents(_vendor(1, "Hello from the team", "a1", canonical_company="Acme Inc", company_identity_verified=True),
              _vendor(2, "Another announcement", "a2", canonical_company="acme inc", company_identity_verified=True))
    assert e[1] == e[2]


def test_a_company_name_that_is_not_marked_verified_groups_nothing():
    e = _ents(_vendor(1, "x", "a1", canonical_company="Acme", company_identity_verified=False),
              _vendor(2, "y", "a2", canonical_company="Acme"))
    assert e[1] != e[2]


def test_missing_or_placeholder_authors_never_merge_unrelated_records():
    e = _ents(_vendor(1, "Alpha launch", None), _vendor(2, "Beta launch", "[deleted]"), _vendor(3, "Gamma launch", ""), _vendor(4, "Delta", "Unknown"))
    assert len(set(e.values())) == 4


def test_independent_buyers_with_the_same_title_are_never_merged_on_title_similarity():
    rows = [{"index": i, "link": f"https://x/{i}", "title": "Need a CRM for Agencies", "role": "buyer"} for i in (1, 2)]
    e = ol.group_entities(rows, {"https://x/1": {"author": "b1"}, "https://x/2": {"author": "b2"}})
    assert e[1] != e[2]


def test_duplicate_vendor_posts_with_a_verified_identity_count_once():
    out = _answer([OK1, OK2], "Find open source platform for building business applications", role="competitor_promo", intent="none")
    opp = out["opportunities"]
    assert opp["listed_posts"] == 2 and opp["identity_groups"] == 1 and opp["unique_authors"] == 1


def test_a_shared_leading_product_name_is_only_a_possible_relationship_and_changes_no_count():
    rows_posts = [_vendor(1, "Show HN: Openkoda platform", "a1"), _vendor(2, "Openkoda 1.7 released", "a2")]
    rows = [r for r, _ in rows_posts]
    posts = {r["link"]: p for r, p in rows_posts}
    ents = ol.group_entities(rows, posts)
    assert ents[1] != ents[2]                                                 # not merged
    poss = ol.possible_relationships(rows, posts, ents)
    assert len(poss) == 1 and poss[0]["verified"] is False and poss[0]["rows"] == [1, 2]
    assert ol.possible_relationships([_vendor(1, "Show HN: Alpha CRM for Agencies")[0], _vendor(2, "Show HN: Beta CRM for Agencies")[0]], {}, {1: "e1", 2: "e2"}) == []


# ── the counts are different metrics and entity grouping cannot alter the pipeline's counts ───────────────────────────────
def test_entity_grouping_never_changes_the_retrieved_displayed_or_buyer_counts():
    out = _answer([OK1, OK2, NONPROFIT, RECRUIT], "Find CRM recommendations", intent="research")
    opp, counts = out["opportunities"], out["counts"]
    shown = len(_rows(out))
    assert opp["listed_posts"] == counts["shown"] == shown                      # grouping neither adds nor removes a displayed post
    assert counts["retrieved"] == 4 and counts["retrieved"] == shown + counts["excluded"] + out["reconciliation"]["not_listed"]
    assert opp["distinct_sources"] == shown
    assert opp["identity_groups"] + opp["records_without_identity"] <= opp["listed_posts"]
    assert opp["confirmed_buyer_signals"] == sum(1 for r in _rows(out) if r["opportunity_category"] == ol.CONFIRMED)
    assert opp["confirmed_buyer_groups"] <= opp["confirmed_buyer_signals"]
    assert opp["current_opportunity_rows"] == sum(1 for r in _rows(out) if r["current_opportunity"])


def test_posts_with_no_known_author_are_counted_apart_from_known_authors():
    out = _answer([_post("a", "k1", "Need CRM", "We need a CRM. Budget $20.", author=""), _post("a", "k2", "Need CRM too", "We need a CRM as well. Budget $30.", author="unknown"),
                   NONPROFIT], "Find CRM buyers", intent="explicit")
    opp = out["opportunities"]
    assert opp["unknown_author_posts"] == 2 and opp["unique_authors"] == 1 and opp["listed_posts"] == 3
    assert opp["identity_groups"] == 1 and opp["records_without_identity"] == 2          # an unknown-author record is never a verified identity
