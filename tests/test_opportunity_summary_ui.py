"""The Opportunity summary box in the real page (both templates, Chromium, network blocked): coverage-aware buyer-signal wording, honest identity
terminology and correct pluralization. The box reads only `opportunities` and `search_coverage`, so each case overrides those two objects."""
import copy

import pytest

from tests.test_answer_persistence import TEMPLATES, env  # noqa: F401
from tests.test_report_table_ui import built, flat, open_page  # noqa: F401

SRC = lambda **states: [{"key": k, "label": k.capitalize() if k != "hackernews" else "Hacker News", "state": v, "via": "x", "detail": ""} for k, v in states.items()]
COMPLETE = {"retrieval": "complete", "sources": SRC(hackernews="searched", reddit="searched")}


def _opp(**kw):
    base = {"categories": {"confirmed_buyer": 0, "strong_research": 0, "problem_signal": 0, "potential_partnership": 0, "competitor_signal": 0,
                           "general_discussion": 0, "unverified": 0},
            "labels": {"general_discussion": "General market discussion", "confirmed_buyer": "Confirmed buyer signal"},
            "listed_posts": 0, "distinct_sources": 0, "unique_authors": 0, "unknown_author_posts": 0, "identity_groups": 0, "records_without_identity": 0,
            "multi_post_entities": [], "possible_relationships": [], "confirmed_buyer_signals": 0, "confirmed_buyer_groups": 0, "confirmed_current": 0,
            "confirmed_historical": 0, "current_opportunity_rows": 0, "historical_rows": 0, "undated_rows": 0, "statement": "", "historical_after_days": 90}
    base.update(kw)
    return base


def _box(browser, built, name, opp, coverage="keep"):
    out, results = copy.deepcopy(built[0]), built[1]
    out["opportunities"] = opp
    if coverage is None:
        out.pop("search_coverage", None)
    elif coverage != "keep":
        out["search_coverage"] = dict(out.get("search_coverage") or {}, **coverage)
    page, errors = open_page(browser, name, out, results)
    try:
        assert errors == []
        return flat(page.inner_text(".opportunity-summary"))
    finally:
        page.close()


def case(browser, built, name, opp, coverage):
    return _box(browser, built, name, opp, coverage)


# ── identity terminology and pluralization ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("groups,label", [(0, "0 verified identity groups"), (1, "1 verified identity group"), (3, "3 verified identity groups")])
def test_identity_groups_are_pluralized_correctly(integrity_browser, built, name, groups, label):
    box = case(integrity_browser, built, name, _opp(identity_groups=groups, listed_posts=groups), COMPLETE)
    assert label in box and "identitys" not in box


@pytest.mark.parametrize("name", TEMPLATES)
def test_records_and_posts_use_singular_and_plural_forms(integrity_browser, built, name):
    one = case(integrity_browser, built, name, _opp(listed_posts=1, unique_authors=1, records_without_identity=1, unknown_author_posts=1), COMPLETE)
    many = case(integrity_browser, built, name, _opp(listed_posts=3, unique_authors=2, records_without_identity=2, unknown_author_posts=2), COMPLETE)
    assert "1 listed post " in one and "1 known author " in one and "1 record without a verified identity" in one and "1 post with no known author" in one
    assert "3 listed posts" in many and "2 known authors" in many and "2 records without a verified identity" in many and "2 posts with no known author" in many


@pytest.mark.parametrize("name", TEMPLATES)
def test_an_unknown_author_record_is_never_labelled_a_verified_identity(integrity_browser, built, name):
    box = case(integrity_browser, built, name, _opp(listed_posts=2, unknown_author_posts=2, records_without_identity=2, identity_groups=0), COMPLETE)
    assert "0 verified identity groups" in box and "2 records without a verified identity (each counted individually)" in box


# ── coverage-aware buyer-signal statement ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", TEMPLATES)
def test_a_complete_successful_search_with_no_buyer_signal_says_so_plainly(integrity_browser, built, name):
    box = case(integrity_browser, built, name, _opp(listed_posts=2), COMPLETE)
    assert "No confirmed buyer signal was found in the listed posts. Every selected source was searched." in box
    assert "does not show that no demand exists" not in box


@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("state,words", [("failed", "Reddit failed"), ("unavailable", "Reddit unavailable"), ("not_searched", "Reddit not searched"),
                                         ("partial", "Reddit only partly searched"), ("unconfirmed", "Reddit not confirmed")])
def test_a_source_that_did_not_complete_is_never_read_as_no_demand(integrity_browser, built, name, state, words):
    cov = {"retrieval": "partial", "sources": SRC(hackernews="searched", reddit=state)}
    box = case(integrity_browser, built, name, _opp(), cov)
    assert "the search was not complete" in box and words in box and "This does not show that no demand exists." in box
    assert "Every selected source was searched" not in box


@pytest.mark.parametrize("name", TEMPLATES)
def test_reddit_failure_with_zero_listed_posts_does_not_claim_absence_of_demand(integrity_browser, built, name):
    cov = {"retrieval": "partial", "sources": SRC(hackernews="searched", reddit="failed")}
    box = case(integrity_browser, built, name, _opp(listed_posts=0), cov)
    assert "0 listed posts" in box and "Reddit failed" in box and "does not show that no demand exists" in box


@pytest.mark.parametrize("name", TEMPLATES)
def test_missing_coverage_is_stated_not_assumed_complete(integrity_browser, built, name):
    box = case(integrity_browser, built, name, _opp(), None)
    assert "search coverage was not reported" in box and "does not show that no demand exists" in box


# ── historical versus current ────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", TEMPLATES)
def test_historical_buyer_signals_are_not_presented_as_current_opportunities(integrity_browser, built, name):
    opp = _opp(listed_posts=1, confirmed_buyer_signals=1, confirmed_historical=1, historical_rows=1)
    box = case(integrity_browser, built, name, opp, COMPLETE)
    assert "1 historical confirmed buyer signal" in box and "not a current opportunity" in box
    assert "current confirmed buyer signal" not in box.replace("historical confirmed buyer signal", "")


@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("n,label", [(1, "1 current confirmed buyer signal"), (2, "2 current confirmed buyer signals")])
def test_current_buyer_opportunities_are_reported_with_correct_plurals(integrity_browser, built, name, n, label):
    box = case(integrity_browser, built, name, _opp(listed_posts=n, confirmed_buyer_signals=n, confirmed_current=n), COMPLETE)
    assert label in box and "historical" not in box.split("Counts")[0]


@pytest.mark.parametrize("name", TEMPLATES)
def test_a_buyer_signal_found_in_an_incomplete_search_says_others_may_exist(integrity_browser, built, name):
    cov = {"retrieval": "partial", "sources": SRC(hackernews="searched", reddit="failed")}
    box = case(integrity_browser, built, name, _opp(listed_posts=1, confirmed_buyer_signals=1, confirmed_current=1), cov)
    assert "1 current confirmed buyer signal" in box and "other buyer signals may exist" in box and "Reddit failed" in box
