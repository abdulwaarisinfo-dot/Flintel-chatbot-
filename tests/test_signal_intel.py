"""
tests/test_signal_intel.py — signal_intel.py: classification (5 classes), evidence integrity, topic relevance vs intent, no false demand,
zero-result explanations. Pure and offline.

The post texts below are SIMULATED fixtures written to reflect the three Hacker News examples discussed (a competitor/market post, a
privacy discussion, a security incident) and typical buying/problem posts. They are not copies of real posts.
"""
import json

import pytest

import signal_intel as si

Q = "Is anyone looking for an AI agent for customer support?"
TS = 1793100000.0


def post(title, body="", **kw):
    p = {"title": title, "post_text": body, "post_url": "https://news.ycombinator.com/item?id=42", "platform": "hackernews",
         "author": "someone", "created_utc": TS}
    p.update(kw)
    return p


KNEMORA = post("Show HN: Knemora - a local-first AI knowledge assistant",
               "We built Knemora so teams can search their internal docs privately, on their own machines. Feedback is welcome.")
GEMINI = post("Gemini privacy: your chats may be reviewed by humans",
              "Google says conversations can be read by reviewers even with history off. Many users are worried about the privacy implications.")
GITBOOK = post("GitBook security incident: customer workspaces were accessed",
               "GitBook disclosed a security incident in which an attacker accessed some customer workspaces. Credentials were rotated.")
BUY = post("Ask HN: Looking for an AI agent vendor for customer support",
           "We're looking for a vendor to automate tier-1 support. We have a budget approved for Q1 and want a team who has shipped this.")
DEV = post("Need a developer for a Shopify integration",
           "I need a developer to build a custom Shopify integration. Looking to hire a freelancer this month.")
PROBLEM = post("Our support queue is out of control",
               "Our small team is struggling with ticket volume and we keep missing replies. How do you handle this? Any advice?")
GENERAL = post("Ask HN: What are you reading this week?", "Share what books you are enjoying right now.")
PROBE = post("Ask HN: Is anyone looking for an AI agent for customer support?",
             "I'm thinking of building one and want to know if there is a market. Would anyone pay for this?")


# ── the five classes ─────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("p,want", [(BUY, si.DIRECT_DEMAND), (DEV, si.DIRECT_DEMAND), (PROBLEM, si.PROBLEM_SIGNAL),
                                    (KNEMORA, si.COMPETITOR_SIGNAL), (GENERAL, si.GENERAL_DISCUSSION)])
def test_each_class(p, want):
    assert si.classify_post(p, Q)["class"] == want


def test_market_signal_examples():
    assert si.classify_post(GITBOOK, Q)["class"] == si.MARKET_SIGNAL
    assert si.classify_post(GEMINI, Q)["class"] in (si.MARKET_SIGNAL, si.PROBLEM_SIGNAL)       # a concern about a market development


@pytest.mark.parametrize("p", [KNEMORA, GEMINI, GITBOOK, PROBE, GENERAL, PROBLEM])
def test_the_three_hn_examples_and_other_non_buyers_are_never_direct_demand(p):
    assert si.classify_post(p, Q)["class"] != si.DIRECT_DEMAND


def test_asking_whether_others_want_it_is_a_market_probe_not_a_buyer():
    c = si.classify_post(PROBE, Q)
    assert c["class"] == si.MARKET_SIGNAL and c["rule"] == "validation" and "not a request to buy" in c["intent_explanation"]
    assert any("validating" in x for x in c["inferred"])


def test_a_founder_post_is_competitor_with_a_limitation():
    c = si.classify_post(KNEMORA, Q)
    assert c["class"] == si.COMPETITOR_SIGNAL
    assert any("not evidence of a buyer" in x for x in c["limitations"])


# ── topic relevance is separate from intent ──────────────────────────────────────────────────────────────────────────────
def test_an_on_topic_post_is_not_thereby_a_buying_signal():
    on_topic = post("Open-source AI agent framework for customer support teams",
                    "A discussion of how AI agents handle customer support tickets and where they fail.")
    c = si.classify_post(on_topic, Q)
    assert c["scores"]["topic_relevance"] >= 0.5                       # about the same topic ...
    assert c["class"] in (si.GENERAL_DISCUSSION, si.MARKET_SIGNAL)     # ... without any request to obtain anything
    assert c["scores"]["commercial_relevance"] <= 0.3 and c["scores"]["intent_strength"] <= 0.5


def test_an_off_topic_buyer_is_still_a_buyer_with_low_topic_relevance():
    c = si.classify_post(DEV, Q)
    assert c["class"] == si.DIRECT_DEMAND and c["scores"]["topic_relevance"] < 0.5


def test_general_discovery_does_not_evaluate_topic_relevance():
    c = si.classify_post(GEMINI, None)
    assert c["scores"]["topic_relevance"] is None and "not evaluated" in c["relevance_explanation"]


def test_local_first_ai_assistant_is_not_automatically_a_customer_support_opportunity():
    c = si.classify_post(KNEMORA, Q)
    assert c["class"] == si.COMPETITOR_SIGNAL and c["scores"]["commercial_relevance"] < 0.5


# ── evidence integrity ───────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("p", [KNEMORA, GEMINI, GITBOOK, BUY, DEV, PROBLEM, PROBE, GENERAL])
def test_quotes_are_exact_substrings_and_fields_are_kept_verbatim(p):
    c = si.classify_post(p, Q)
    text = p["title"] + "\n" + p["post_text"]
    if c["quote"]:
        assert c["quote"] in text                                         # never fabricated
        assert all(q.split("“", 1)[-1].rstrip("”") in text for q in c["confirmed"])
    assert c["url"] == p["post_url"] and c["title"] == p["title"] and c["author"] == "someone" and c["platform"] == "hackernews"
    assert c["published_at"] == "2026-10-27T11:20:00+00:00" or c["published_at"].startswith("2026-")


def test_missing_data_is_reported_never_invented():
    c = si.classify_post({"title": "Something", "post_url": "", "created_utc": None}, Q)
    assert c["author"] is None and c["published_at"] is None and c["url"] == ""
    assert any("publication time" in x for x in c["limitations"]) and any("author" in x for x in c["limitations"])
    assert si.classify_post(None, Q)["class"] == si.GENERAL_DISCUSSION
    assert any("no readable text" in x for x in si.classify_post({}, Q)["limitations"])


def test_invalid_timestamps_are_not_trusted():
    for bad in (0, -5, "never", True, None):
        assert si.classify_post(post("t", "b", created_utc=bad), Q)["published_at"] in (None, "never")


def test_title_only_evidence_is_labelled_and_lowers_confidence():
    full = si.classify_post(DEV, Q)
    title_only = si.classify_post(post("I need a developer to build a Shopify integration", "", text_source="title"), Q)
    assert title_only["text_source"] == "title" and any("Only the title" in x for x in title_only["limitations"])
    assert title_only["scores"]["evidence_confidence"] < full["scores"]["evidence_confidence"]


def test_confidence_never_reaches_certainty():
    assert max(si.classify_post(p, Q)["scores"]["evidence_confidence"] for p in (BUY, DEV, KNEMORA)) <= 0.9


# ── no false buying intent ───────────────────────────────────────────────────────────────────────────────────────────────
def test_no_direct_demand_statement_is_exact_and_adjacent_signals_are_separate():
    rep = si.build_signal_report([KNEMORA, GEMINI, GITBOOK], Q)
    assert rep["direct_demand_count"] == 0
    assert rep["statement"].startswith("No confirmed direct buying demand was found in the retrieved discussions.")
    assert "Adjacent signals are shown separately" in rep["statement"]
    assert sum(rep["breakdown"].values()) == 3 and rep["breakdown"][si.DIRECT_DEMAND] == 0


def test_demand_is_counted_only_for_explicit_requests():
    rep = si.build_signal_report([BUY, DEV, KNEMORA, GEMINI, PROBLEM], Q)
    assert rep["direct_demand_count"] == 2 and "2 of 5" in rep["statement"]
    assert [i["class"] for i in rep["items"]].count(si.DIRECT_DEMAND) == 2


def test_general_mode_statement_says_the_posts_were_not_selected_for_intent():
    rep = si.build_signal_report([GENERAL, GEMINI], None, mode="general")
    assert rep["statement"].startswith("General discovery sample: these posts were not selected for buying intent.")
    assert rep["query"] is None and rep["items"][0]["scores"]["topic_relevance"] is None


def test_empty_input():
    rep = si.build_signal_report([], Q)
    assert rep["classified"] == 0 and "nothing was classified" in rep["statement"]


def test_opportunity_extraction_only_from_quotes():
    d = si.classify_post(BUY, Q)
    opp = si.extract_opportunity(BUY, d)
    assert opp["solution_sought"] == d["quote"] and opp["commercial_cue"] and "budget" in opp["unknown"]
    assert si.extract_opportunity(KNEMORA) is None and si.extract_opportunity(GITBOOK) is None
    p = si.classify_post(PROBLEM, Q)
    assert si.extract_opportunity(PROBLEM, p)["problem"] == p["quote"]
    using = post("Need an alternative", "We currently use Zendesk and it is painful. We need a tool that handles refunds.")
    assert "Zendesk" in si.extract_opportunity(using)["currently_using"] and si.extract_opportunity(using)["currently_using"] in using["title"] + "\n" + using["post_text"]


def test_report_keeps_post_order_urls_and_does_not_mutate_input():
    posts = [dict(BUY, post_url="https://news.ycombinator.com/item?id=1"), dict(KNEMORA, post_url="https://news.ycombinator.com/item?id=2")]
    before = json.dumps(posts, sort_keys=True)
    rep = si.build_signal_report(posts, Q)
    assert [i["url"] for i in rep["items"]] == [p["post_url"] for p in posts] and [i["rank"] for i in rep["items"]] == [1, 2]
    assert json.dumps(posts, sort_keys=True) == before


# ── six distinct zero-result explanations ────────────────────────────────────────────────────────────────────────────────
def zr(**kw):
    return si.explain_zero_result(dict({"source": "Hacker News", "mode": "topic"}, **kw))


def test_the_six_explanations_are_distinct():
    cases = {
        si.SOURCE_UNAVAILABLE: zr(source_available=False),
        si.COLLECTION_FAILED: zr(error="timeout"),
        si.NO_POSTS_COLLECTED: zr(total=0),
        si.NONE_IN_RANGE: zr(total=1, in_range=0, window_start="2026-11-01T00:00:00+00:00", window_end="2026-11-02T00:00:00+00:00",
                             newest="2026-10-20T10:00:00+00:00", oldest="2026-01-01T00:00:00+00:00"),
        si.EXCLUDED_BY_FILTERS: zr(total=1, in_range=40, examined=40, excluded_filters=40),
        si.NO_TOPIC_MATCH: zr(total=1, in_range=40, examined=40, excluded_filters=0, topic="fusion reactors"),
    }
    for code, e in cases.items():
        assert e["code"] == code
    assert len({e["message"] for e in cases.values()}) == 6 and len({e["headline"] for e in cases.values()}) == 6
    for e in cases.values():
        assert "No relevant discussion found" not in e["headline"] + e["message"] and e["next_steps"]


def test_explanations_carry_the_real_numbers():
    e = zr(total=1, in_range=0, window_start="2026-11-01T00:00:00+00:00", window_end="2026-11-02T00:00:00+00:00",
           newest="2026-10-20T10:00:00+00:00", oldest="2026-01-01T00:00:00+00:00")
    assert "2026-10-20 10:00 UTC" in e["message"] and "2026-01-01" in e["message"] and "2026-11-01" in e["message"]
    assert "2026-10-20 10:00 UTC" in e["next_steps"][0]
    f = zr(total=1, in_range=12, examined=12, excluded_filters=12)
    assert "12 Hacker News posts" in f["message"]
    assert zr(total=1, in_range=7, mode="topic", topic="gemini")["message"].count("7 Hacker News posts") == 1


def test_a_failure_is_never_called_empty_and_unknown_is_honest():
    e = zr(error="the saved-post scan did not complete")
    assert e["code"] == si.COLLECTION_FAILED and "not an empty result" in e["message"]
    u = si.explain_zero_result({})
    assert u["code"] == si.UNKNOWN and "cannot be narrowed down" in u["message"]


def test_general_mode_with_posts_in_range_is_a_filter_exclusion_not_a_topic_mismatch():
    assert zr(mode="general", total=1, in_range=5)["code"] == si.EXCLUDED_BY_FILTERS
    assert zr(mode="topic", total=1, in_range=5)["code"] == si.NO_TOPIC_MATCH


def test_combining_sources_keeps_every_reason():
    a = si.explain_zero_result({"source": "Reddit", "total": 0})
    b = si.explain_zero_result({"source": "Hacker News", "error": "timeout"})
    c = si.combine_zero_results([a, b])
    assert c["code"] == si.PARTIAL_RETRIEVAL and "Reddit" in c["headline"] and "Hacker News" in c["headline"] and len(c["next_steps"]) >= 2
    d = si.combine_zero_results([si.explain_zero_result({"source": "Reddit", "total": 0}), si.explain_zero_result({"source": "Hacker News", "total": 5, "in_range": 0})])
    assert d["code"] == "MIXED"                                     # two different absences, no failure: still MIXED
    assert si.combine_zero_results([a]) is a and si.combine_zero_results([]) is None


# ── performance guard (measured: ~2 ms per ordinary post; 200 KB adversarial posts <= ~250 ms, linear) ─────────────────────
@pytest.mark.parametrize("text", ["we " * 70000, "i am looking for " * 12000, "x" * 200000, ("a" + " " * 50) * 4000, "i need " * 30000, "!?.\n" * 50000])
def test_classification_is_linear_on_pathological_text(text):
    import time
    t0 = time.perf_counter()
    si.classify_post({"title": "t", "post_text": text}, "ai agent support")
    assert time.perf_counter() - t0 < 3.0                       # ~10x the measured worst case: only a backtracking regression would fail


def test_a_typical_result_set_is_classified_quickly():
    import time
    posts = [dict(BUY, post_text=BUY["post_text"] * 20, post_url=f"https://news.ycombinator.com/item?id={i}") for i in range(25)]
    t0 = time.perf_counter()
    rep = si.build_signal_report(posts, Q)
    assert rep["classified"] == 25 and time.perf_counter() - t0 < 2.0
