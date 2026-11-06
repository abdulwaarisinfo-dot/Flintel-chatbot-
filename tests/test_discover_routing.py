"""Market-level discovery questions must be recognised as general discovery (no mandatory topic keyword), and topic searches must never be captured.
The 20 discovery phrases are the ones named in the product brief (used to design the rule, so this is a regression pin, not an unbiased measurement);
the topic phrases include adversarial ones that share the discovery vocabulary."""
import pytest

import signal_intel as si

DISCOVERY = [
    "What problems are people discussing today?", "What are people complaining about?", "What products are people requesting?",
    "What businesses are struggling with?", "What are people trying to solve?", "What tools are people dissatisfied with?",
    "What competitors are people switching to?", "What opportunities are emerging?", "What are people repeatedly asking for?",
    "What are people willing to pay for?", "What are people struggling to find?", "What unmet needs are appearing?",
    "Show me recent discussions", "Find people who need help",
    "Find recurring problems people are discussing that could become useful software products.",
    "What software products are people actively requesting?", "What are users unhappy about with competing products?",
    "What operational problems are small businesses repeatedly experiencing?", "Yes, run a general unfiltered sample of recent threads.",
    "What are people talking about on Hacker News right now?"]
TOPIC = [
    "Find people looking for a CRM for small teams", "What do people think about HubSpot pricing?", "Invoicing tool complaints",
    "Show me discussions about Notion alternatives", "What are people saying about Stripe fees?", "Find buyers for AI note takers",
    "Reddit posts about Shopify checkout problems", "competitors of Calendly", "Why are users leaving Slack?", "best CRM for real estate agents",
    "What problems do freelancers have with invoicing?", "What are people complaining about regarding Notion?",
    "What products are people requesting for dogs?", "Find people who need help with taxes", "What opportunities are emerging in healthcare?",
    "What tools are people dissatisfied with for payroll?", "What are small businesses struggling with in logistics?",
    "Which CRM should a salon use?", "Find unmet needs in the pet industry", "What are people willing to pay for in fitness apps?",
    "Latest iPhone", "Is anyone looking for an AI agent?", "Show recent threads about CRM pain", "", "   ", None]


@pytest.mark.parametrize("q", DISCOVERY)
def test_market_level_questions_are_general_discovery(q):
    assert si.is_general_discovery_request(q) is True


@pytest.mark.parametrize("q", TOPIC)
def test_topic_searches_are_never_treated_as_discovery(q):
    assert si.is_general_discovery_request(q) is False


def test_the_detector_never_raises_on_odd_input():
    for bad in (5, [], {}, object(), "\x00", "?" * 5000, "what " * 2000):
        assert si.is_general_discovery_request(bad) in (True, False)


# ── routing benchmark files (review pass): 113 designed queries (dev) + 67 fresh queries written BEFORE the vocabulary was extended ───────────────
import json
from pathlib import Path

_EVAL = Path(__file__).resolve().parent.parent / "evaluation"


def _score(name):
    Q = json.loads((_EVAL / name).read_text())
    tp = fp = fn = tn = 0
    wrong_fp = []
    for x in Q:
        got = si.is_general_discovery_request(x["q"])
        tp += got and x["expected"]
        fn += (not got) and x["expected"]
        tn += (not got) and not x["expected"]
        if got and not x["expected"]:
            fp += 1
            wrong_fp.append(x["q"])
    return {"n": len(Q), "tp": tp, "fn": fn, "fp": fp, "tn": tn, "fp_queries": wrong_fp}


def test_routing_files_are_large_balanced_and_cover_every_requested_category():
    dev = json.loads((_EVAL / "discovery_routing_queries.json").read_text())
    fresh = json.loads((_EVAL / "discovery_routing_queries_v2_fresh.json").read_text())
    assert len(dev) + len(fresh) >= 100 and len({x["q"] for x in dev} & {x["q"] for x in fresh}) == 0
    cats = {x["category"].split(" (")[0] for x in dev}
    assert {"broad market discovery", "problems people discuss", "unmet needs / demand", "emerging demand / opportunities", "competitor discovery",
            "topic-specific searches", "adversarial: discovery words + a real topic", "ambiguous"} <= cats
    for x in dev + fresh:
        assert set(x) == {"q", "expected", "category"} and isinstance(x["expected"], bool)


def test_development_routing_benchmark():
    """Tuned against (the vocabulary was extended using ONLY its misses), so optimistic. Floors are regression guards."""
    r = _score("discovery_routing_queries.json")
    assert r["tp"] / (r["tp"] + r["fn"]) >= 0.85
    assert r["tp"] / (r["tp"] + r["fp"]) >= 0.95
    assert r["fp"] <= 1, r["fp_queries"]               # the one known ambiguity is 'Who needs software?' (generic words only)


def test_fresh_routing_benchmark_is_the_honest_estimate_and_topic_search_is_never_captured():
    """Written BEFORE the vocabulary was extended and never used to tune it. Measured: recall 17/31 (55%), precision 17/17, topic searches captured 0/36.
    A miss is safe (the message goes to the normal router as before); a false positive would drop the user's topic, so precision is what is protected."""
    r = _score("discovery_routing_queries_v2_fresh.json")
    assert r["fp"] == 0, r["fp_queries"]
    assert r["tp"] / (r["tp"] + r["fn"]) >= 0.45
    assert r["tp"] == r["tp"] + 0 and r["tp"] / (r["tp"] + r["fp"]) == 1.0


@pytest.mark.parametrize("q", ["What issues are businesses running into?", "Which tools are people leaving?", "What new demand is appearing?",
                               "What are the hottest topics right now?", "Give me today's top discussions"])
def test_vocabulary_extension_recognises_these_generic_discovery_questions(q):
    assert si.is_general_discovery_request(q) is True


@pytest.mark.parametrize("q", ["What issues are restaurants running into with delivery apps?", "Which tools are people leaving Evernote for?",
                               "What new demand is appearing for heat pumps?", "What are the hottest topics in cryptocurrency?", "Give me today's top discussions about Tesla"])
def test_the_same_questions_with_a_topic_stay_topic_searches(q):
    assert si.is_general_discovery_request(q) is False


# ── post-review: a separate development set (v3) drove the second vocabulary extension; a NEW held-out set (v4) was evaluated once ──────────
def test_routing_sets_v3_dev_and_v4_heldout_are_disjoint_from_each_other_and_the_older_sets():
    names = ["discovery_routing_queries.json", "discovery_routing_queries_v2_fresh.json", "discovery_routing_dev_v3.json",
             "discovery_routing_heldout_v4.json", "discovery_routing_dev_v5.json", "discovery_routing_heldout_v6.json"]
    sets = [{x["q"].strip().lower() for x in json.loads((_EVAL / n).read_text())} for n in names]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            assert not (sets[i] & sets[j]), (names[i], names[j])
    assert len(sets[2]) >= 80 and len(sets[3]) >= 90


def test_dev_v3_benchmark_the_second_vocabulary_extension_was_driven_by_its_misses_only():
    """Measured: recall 46/49 after the extension (about 29/49 before), false positives 0/37. Optimistic: it is the set that was tuned on."""
    r = _score("discovery_routing_dev_v3.json")
    assert r["fp"] == 0, r["fp_queries"]
    assert r["tp"] / (r["tp"] + r["fn"]) >= 0.85


def test_v4_was_evaluated_once_then_used_to_check_the_third_extension_so_it_is_no_longer_a_fresh_held_out_set():
    """v4 measured 31/50 (62%) before the third vocabulary extension and 36/50 after it (the extension was written from dev v5 misses only; no v4
    query was read or used). Because it was run a second time it is retired as 'held-out'; v6 replaces it. False positives stay 0."""
    r = _score("discovery_routing_heldout_v4.json")
    assert r["fp"] == 0, r["fp_queries"]
    assert r["tp"] == 36 and r["tp"] + r["fn"] == 50


def test_dev_v5_the_third_extension_was_driven_by_its_misses_only():
    """Measured: recall 7/50 before the extension (closed vocabulary only), 41/50 after (grammar tier + vocabulary). Optimistic: it is the tuning set."""
    r = _score("discovery_routing_dev_v5.json")
    assert r["fp"] == 0, r["fp_queries"]
    assert r["tp"] / (r["tp"] + r["fn"]) >= 0.75


def test_heldout_v6_is_the_honest_estimate_evaluated_once_without_tuning():
    """Written after the third extension was frozen, evaluated ONCE, never tuned on. Measured: recall 28/49 (57%), false positives 0/41, i.e. no topic
    or non-discovery message captured. The 82% on dev v5 did NOT generalise; expect roughly 55-60% on unseen broad questions. Pinned so a change is conscious."""
    r = _score("discovery_routing_heldout_v6.json")
    assert r["fp"] == 0, r["fp_queries"]
    assert r["tp"] == 28 and r["tp"] + r["fn"] == 49


@pytest.mark.parametrize("q", ["What are Tesla owners complaining about?", "What are founders building with Rust?", "What are people struggling to find in used cars lately?",
                               "What problems are being discussed in cybersecurity today?", "Which complaints are rising for Cloudflare this week?",
                               "What are people hoping to find in a used laptop?", "What problems do dentists have with scheduling software?"])
def test_the_grammar_tier_never_captures_a_topic_even_when_the_verbs_look_generic(q):
    assert si.is_general_discovery_request(q) is False


@pytest.mark.parametrize("q", ["What have people been grumbling about recently?", "What is annoying people the most this month?",
                               "Where are the unmet needs piling up?", "What are users demanding right now?"])
def test_the_grammar_tier_accepts_verb_forms_before_any_topic_slot(q):
    assert si.is_general_discovery_request(q) is True
