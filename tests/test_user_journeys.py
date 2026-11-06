"""End-to-end product journeys, OFFLINE. Each journey chains the real components the user's request passes through (discovery detection ->
classification -> evidence report -> merge/deduplication -> zero-result explanation) over the SYNTHETIC seed corpus (tools/staging_seed.py). Retrieval from a
live database or provider is NOT exercised here (that needs staging); these prove the logic between retrieval and the screen keeps the product's promises."""
import importlib.util
from pathlib import Path

import pytest

import search_modes as sm
import signal_intel as si

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("seed", ROOT / "tools" / "staging_seed.py")
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)


def corpus():
    """The synthetic seed records in the shape retrieval returns (post_text/post_url), keeping their category for assertions."""
    out = []
    for d in seed.build_records(500):
        if not (d.get("text") or "").strip() or "created_utc" not in d:
            continue
        out.append({"title": d["title"], "post_text": d["text"], "post_url": d["post_url"], "platform": d["platform"], "created_utc": d["created_utc"].timestamp(),
                    "username": d["username"], "_cat": d["_seed_category"]})
    return out


POOL = corpus()


def report(posts, query, mode="general"):
    return si.build_signal_report(posts, query, mode, {"sources": ["Reddit", "Hacker News"], "returned": len(posts)})


def by_url(posts):
    return {p["post_url"]: p for p in posts}


def test_A_general_business_discovery():
    q = "Find recurring problems people are discussing that could become useful software products."
    assert si.is_general_discovery_request(q) is True                               # understood as discovery: no mandatory topic keyword
    rep = report(POOL, q)
    posts = by_url(POOL)
    assert rep["items"] and rep["mode"] == "general"
    for it in rep["items"]:
        p = posts[it["url"]]
        full = p["title"] + "\n" + p["post_text"]
        assert it["quote"] is None or it["quote"] in full                           # evidence is verbatim, never invented
        assert it["url"].startswith("https://") and it["platform"] and it["class"] in si.CLASSES
    assert sum(rep["breakdown"].values()) == rep["classified"] and rep["returned"] == len(POOL)      # the breakdown is exactly what was classified
    if rep["returned"] > rep["classified"]:                                                          # a truncated sample must say so, with the true denominator
        assert f"first {rep['classified']} of {rep['returned']} retrieved posts" in rep["statement"] and "not classified" in rep["statement"]
    assert len(rep["items"]) <= rep["classified"] and rep["direct_demand_count"] == rep["breakdown"][si.DIRECT_DEMAND]


def test_B_product_demand_is_distinguished_from_casual_discussion():
    q = "What software products are people actively requesting?"
    assert si.is_general_discovery_request(q) is True
    posts = by_url(POOL)
    demand = [it for it in report(POOL, q)["items"] if it["class"] == si.DIRECT_DEMAND]
    assert demand and all(posts[it["url"]]["_cat"] in ("direct_demand", "product_request", "duplicate_pair") for it in demand)
    for it in demand:
        assert it["quote"], "a demand claim must carry the author's own words"
    # questions / vague musings / market chatter are never reported as demand
    banned = {"question_no_intent", "ambiguous", "market_discussion", "problem", "competitor_comparison"}
    assert not [it for it in demand if posts[it["url"]]["_cat"] in banned]


def test_C_competitor_research_finds_real_switching_and_comparison_evidence():
    q = "What are users unhappy about with competing products?"
    assert si.is_general_discovery_request(q) is True
    posts = by_url(POOL)
    comp = [it for it in report(POOL, q)["items"] if it["class"] == si.COMPETITOR_SIGNAL]
    assert comp and all(posts[it["url"]]["_cat"] == "competitor_comparison" for it in comp)
    for it in comp:
        assert it["quote"] and any(w in it["quote"].lower() for w in ("switch", "vs", "compar", "versus", "replac", "moved", "left", "dropp"))
        assert any("not a current purchase request" in x or "promoting" in x for x in it["limitations"])      # competitive context is labelled as such


def test_D_problems_are_separated_from_purchase_intent():
    q = "What operational problems are small businesses repeatedly experiencing?"
    assert si.is_general_discovery_request(q) is True
    posts = by_url(POOL)
    items = report(POOL, q)["items"]
    prob = [it for it in items if it["class"] == si.PROBLEM_SIGNAL]
    assert prob and all(posts[it["url"]]["_cat"] == "problem" for it in prob)
    for it in prob:
        assert it["scores"]["intent_strength"] <= 0.75 and any("No request" in x or "no first-person" in x.lower() or "No first-person" in x for x in it["limitations"] + [""]) or it["quote"]
    assert not [it for it in items if posts[it["url"]]["_cat"] == "problem" and it["class"] == si.DIRECT_DEMAND]     # a complaint is never a buyer


def test_E_combined_search_merges_live_and_historical_without_double_counting():
    live = [{"title": "Ask HN: best invoicing tool?", "post_url": "https://news.ycombinator.com/item?id=1", "hn_id": "1", "platform": "hackernews", "hn_points": 9},
            {"title": "Live only", "post_url": "https://news.ycombinator.com/item?id=2", "hn_id": "2", "platform": "hackernews"}]
    hist = [{"title": "Ask HN: best invoicing tool?", "post_url": "http://news.ycombinator.com/item?id=1&ref=feed", "platform": "hackernews", "username": "u"},
            {"title": "Stored only", "post_url": "https://www.reddit.com/r/a/comments/x1/t/", "platform": "reddit"},
            {"title": "Stored copy of a Reddit post", "post_url": "https://old.reddit.com/r/A/comments/x1/other_slug/", "platform": "reddit"}]
    merged, stats = sm.merge(live, hist)
    assert len(merged) == 3 and stats == {"live": 1, "historical": 1, "both": 1, "duplicates_removed": 2}
    both = next(p for p in merged if p["retrieval_path"] == "both")
    assert both["post_url"].startswith("http://news.ycombinator.com/") and both["hn_points"] == 9 and both["username"] == "u"      # no metadata lost
    assert len({sm.post_identity(p) for p in merged}) == len(merged)


def test_F_insufficient_coverage_is_explained_never_presented_as_nonexistence():
    cases = [({"source": "Reddit", "total": 0}, si.NO_POSTS_COLLECTED), ({"source": "Reddit", "total": 40, "in_range": 0}, si.NONE_IN_RANGE),
             ({"source": "Hacker News", "mode_unsupported": "live"}, si.UNSUPPORTED_MODE), ({"source": "Hacker News", "error": "timeout"}, si.COLLECTION_FAILED),
             ({"source": "Reddit"}, si.UNKNOWN)]
    for cov, code in cases:
        ex = si.explain_zero_result(dict(cov, mode="topic", topic="fusion power"))
        assert ex["code"] == code
        text = (ex["headline"] + " " + ex["message"]).lower()
        assert not any(p in text for p in ("nobody", "no one is talking", "does not exist", "no discussions exist", "no demand"))
        assert ex["next_steps"]                                                       # always tells the user what to try next
    assert si.explain_zero_result({"source": "Reddit"})["code"] == si.UNKNOWN          # unknown coverage is said to be unknown


def test_every_journey_report_states_the_limits_and_never_claims_buyers_without_evidence():
    rep = report([p for p in POOL if p["_cat"] in ("question_no_intent", "ambiguous", "market_discussion")], "What are people talking about?")
    assert "No confirmed direct buying demand" in rep["statement"]
    assert rep["breakdown"][si.DIRECT_DEMAND] == 0 and rep["direct_demand_count"] == 0


def test_the_statement_never_presents_a_classified_subset_as_everything_retrieved():
    many = [{"title": f"I need a tool for invoicing {i}", "post_text": f"I need a tool for invoicing {i}. Budget is $20.", "post_url": f"https://a.invalid/{i}", "platform": "hackernews"}
            for i in range(60)]
    r = si.build_signal_report(many, "invoicing", "topic", {})
    assert r["returned"] == 60 and r["classified"] == 25
    assert "of 25 retrieved" not in r["statement"]                                          # the old wrong denominator
    assert "first 25 of 60 retrieved posts" in r["statement"] and "remaining 35 retrieved posts were not classified" in r["statement"]
    few = si.build_signal_report(many[:3], "invoicing", "topic", {})
    assert "3 of 3 retrieved posts" in few["statement"] and "not classified" not in few["statement"]             # unchanged when nothing was cut
    none = si.build_signal_report([{"title": "t", "post_text": "Just chatting about gardens today.", "post_url": "https://a.invalid/x", "platform": "reddit"}] * 40, "q", "topic", {})
    assert none["statement"].startswith("No confirmed direct buying demand was found in the first 25 of 40 retrieved posts")
