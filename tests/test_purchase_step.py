"""
tests/test_purchase_step.py — what makes a post a QUALIFIED (High intent) lead.

Rule under test (answer_integrity.purchase_signals / classify_post):
  * High needs a verified source, a verified analysis, a buyer role, product fit "fits" AND an explicit purchase step
    written by the author: an approved / allocated / authorised budget (not a bare amount), an active procurement
    process, an unconditional commitment to buy / sign / subscribe / switch, or a switch that is actually underway.
  * Budget, timing, evaluation (trial, demo, pilot, a concrete quote request) and willingness to pay are separate
    SIGNALS. They are recorded, listed in the row's reason, and never qualify a lead on their own or together.
  * A step that is negated, hypothetical, conditional (also when the condition is separated from it), exploratory,
    paused, still pending authorisation, or already spent is not a step.

Every post text below is a SYNTHETIC sentence written for this test.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import answer_integrity as ai  # noqa: E402

QUERY = ("I run a B2B SaaS company that helps businesses monitor Reddit conversations and find people "
         "actively looking for a solution like ours.")
TITLE = "Need a Reddit monitoring tool for our SaaS team"
SIGNALS = ("budget", "timing", "evaluation", "willingness")

# (id, text after the request title, ceiling, signals that must be present, signals that must be absent, why-demoted)
CASES = [
    # ── weaker signals, alone or together: never a purchase step ──────────────────────────────────────────────────
    ("A01 budget amount only",                    "Budget is $50 per month.", "research", {"budget"}, set(), None),
    ("A02 spend amount",                          "We can spend A$150 per month.", "research", {"budget"}, set(), None),
    ("A03 pricing / cost words only",             "Pricing and cost matter, but we have not set a figure.", "research", set(), set(SIGNALS), None),
    ("A04 bare quote request",                    "Please send a quote.", "research", {"evaluation"}, {"budget"}, None),
    ("A05 timing: asap",                          "We need this asap.", "research", {"timing"}, set(), None),
    ("A06 timing: this quarter",                  "We want it live this quarter.", "research", {"timing"}, set(), None),
    ("A07 quarter reference only",                "Planning for Q3.", "research", {"timing"}, set(), None),
    ("A08 demo request",                          "Can someone give us a demo?", "research", {"evaluation"}, set(), None),
    ("A09 pilot",                                 "We would like to run a pilot first.", "research", {"evaluation"}, set(), None),
    ("A10 budget + trial + timing",               "Budget is $200 per month and we can start a trial this week.", "research",
     {"budget", "evaluation", "timing"}, set(), None),
    ("A11 budget + timing + pilot + demo",        "Our budget is $200 per month, we need it asap and want a free pilot and a demo.", "research",
     {"budget", "timing", "evaluation"}, set(), None),
    ("A12 would pay",                             "I would pay $30 for something like this.", "research", {"willingness"}, set(), None),
    ("A13 will pay for a good one",               "We will pay for a good one.", "research", {"willingness"}, set(), None),
    ("A14 trial and error is not a trial",        "We have been doing trial and error with spreadsheets.", "research", set(), {"evaluation"}, None),
    ("A15 concrete quote request",                "Can you send me a quote for 5 seats?", "research", {"evaluation"}, set(), None),
    # ── explicit purchase steps ───────────────────────────────────────────────────────────────────────────────────
    ("B01 budget approved",                       "Our budget is approved for Q3.", "explicit", {"budget", "timing"}, set(), None),
    ("B02 budget allocated",                      "The budget has been allocated and finance signed off.", "explicit", set(), set(), None),
    ("B03 CFO approved the budget",               "The CFO approved the budget for this tool.", "explicit", set(), set(), None),
    ("B04 ready to buy",                          "We are ready to buy this month.", "explicit", {"timing"}, set(), None),
    ("B05 unconditional future purchase",         "We will purchase a tool by the end of the month.", "explicit", {"timing"}, set(), None),
    ("B06 plan to subscribe",                     "We plan to subscribe this month.", "explicit", {"timing"}, set(), None),
    ("B07 can buy today",                         "I can buy it today.", "explicit", set(), set(), None),
    ("B08 switching underway",                    "We are switching from Brand24 this month.", "explicit", {"timing"}, set(), None),
    ("B09 decided to replace",                    "We have decided to replace our current tool.", "explicit", set(), set(), None),
    ("B10 in procurement",                        "We are in the procurement process now.", "explicit", set(), set(), None),
    ("B11 purchase order raised",                 "A purchase order has been raised for this tool.", "explicit", set(), set(), None),
    ("B12 benign 'if anyone knows'",              "If anyone knows a good one, we are ready to buy this week.", "explicit", {"timing"}, set(), None),
    ("B13 approved and authorised",               "Budget approved and authorized by finance.", "explicit", set(), set(), None),
    ("B14 approved budget with an amount",        "We have an approved budget of $500 per month.", "explicit", {"budget"}, set(), None),
    ("B15 approval stands, purchase waits (own sentence)",
     "The budget is approved. However, we will buy only after the demo.", "explicit", {"evaluation"}, set(), None),
    ("B16 approval + benign question",            "Budget is approved. If anyone knows a good tool, tell us.", "explicit", set(), set(), None),
    ("B17 we raised a purchase order",            "We raised a purchase order for it yesterday.", "explicit", set(), set(), None),
    ("B18 will switch",                           "We will switch from Brand24 next month.", "explicit", {"timing"}, set(), None),
    # ── negation ──────────────────────────────────────────────────────────────────────────────────────────────────
    ("C01 not ready to buy",                      "We are not ready to buy yet.", "research", set(), set(), None),
    ("C02 no plans to purchase",                  "We have no plans to purchase anything now.", "research", set(), set(), None),
    ("C03 will not buy",                          "We will not buy until next year.", "research", set(), set(), None),
    ("C04 budget not approved",                   "The budget is not approved.", "research", {"budget"}, set(), None),
    ("C05 no budget approved",                    "No budget has been approved.", "research", set(), {"budget"}, None),
    ("C06 have not decided to switch",            "We have not decided to switch.", "research", set(), set(), None),
    ("C07 never buy without a trial",             "We never buy tools without a trial.", "research", {"evaluation"}, set(), None),
    ("C08 have not decided whether to buy",       "We haven't decided whether to buy.", "research", set(), set(), None),
    ("C09 a question, not a statement",           "Is the budget approved? Nobody knows.", "research", {"budget"}, set(), None),
    ("C10 nobody approved",                       "Nobody has approved the budget.", "research", set(), set(), None),
    # ── hypothetical commitments ──────────────────────────────────────────────────────────────────────────────────
    ("D01 would buy",                             "We would buy it immediately.", "research", {"willingness"}, set(), None),
    ("D02 maybe we will purchase",                "Maybe we will purchase one next quarter.", "research", set(), set(), "hypothetical"),
    ("D03 might subscribe",                       "We might subscribe later this year.", "research", set(), set(), None),
    ("D04 if we had budget we would buy",         "If we had budget we would buy this today.", "research", set(), set(), None),
    ("D05 thinking about switching",              "We are thinking about switching from Brand24.", "research", set(), set(), None),
    ("D06 considering replacing",                 "We are considering replacing our current tool.", "research", set(), set(), None),
    ("D07 want to replace",                       "We want to replace our current tool.", "research", set(), set(), None),
    ("D08 looking to switch at some point",       "We are looking to switch from Brand24 at some point.", "research", set(), set(), None),
    ("D09 someday we will buy",                   "Someday we will buy something like this.", "research", set(), set(), "hypothetical"),
    # a subject-less "will probably buy" is not parsed as a commitment at all, so nothing needs demoting: still research
    ("D10 just exploring, will probably buy",     "We are just exploring and will probably buy later.", "research", set(), set(), None),
    ("D11 could purchase if the price is right",  "We could purchase one if the price is right.", "research", set(), set(), None),
    ("D12 hypothetically we plan to subscribe",   "Hypothetically we plan to subscribe this month.", "research", {"timing"}, set(), "hypothetical"),
    # ── conditions, including conditions separated from the commitment ───────────────────────────────────────────
    ("E01 ready to buy if the pilot works",       "We are ready to buy if the pilot works.", "research", {"evaluation"}, set(), "conditional"),
    ("E02 condition first",                       "If it fits our workflow, we will purchase it.", "research", set(), set(), "conditional"),
    ("E03 condition after a dash",                "We will purchase it - assuming the pilot goes well.", "research", {"evaluation"}, set(), "conditional"),
    ("E04 condition in parentheses",              "We will purchase it (assuming the pilot goes well).", "research", {"evaluation"}, set(), "conditional"),
    ("E05 dependency in the next sentence",       "We are ready to buy. That depends on the pilot results.", "research", {"evaluation"}, set(), "conditional"),
    ("E06 'but only if' in the next sentence",    "We will subscribe. But only if the trial convinces us.", "research", {"evaluation"}, set(), "conditional"),
    ("E07 'only if' in the next sentence",        "We will buy. Only if the pilot works.", "research", {"evaluation"}, set(), "conditional"),
    ("E08 benign question in the next sentence",  "We will buy this month. If anyone knows a good one, tell us.", "explicit", {"timing"}, set(), None),
    ("E09 'when' clause",                         "When the pilot ends we will purchase.", "research", {"evaluation"}, set(), "conditional"),
    ("E10 purchase once budget is approved",      "We will purchase when the budget is approved.", "research", {"budget"}, set(), "conditional"),
    ("E11 approval itself conditional",           "Budget is approved if the pilot works.", "research", {"evaluation"}, set(), "conditional"),
    ("E12 provided that",                         "We plan to subscribe this month, provided the plugin integrates with our CRM.", "research", {"timing"}, set(), "conditional"),
    ("E13 subject to review",                     "We will purchase this month, subject to legal review.", "research", {"timing"}, set(), "conditional"),
    ("E14 approved, spend conditional (one sentence)",
     "Our budget is approved, but we will only spend it if the pilot works.", "research", {"evaluation"}, set(), "conditional"),
    # ── paused procurement, approved-but-not-authorised budgets ──────────────────────────────────────────────────
    ("F01 procurement on hold",                   "Procurement is on hold.", "research", set(), set(), None),
    ("F02 started but paused",                    "We started the procurement process but it is paused.", "research", set(), set(), "paused"),
    ("F03 on hold in the next sentence",          "We are in the procurement process. It is currently on hold.", "research", set(), set(), "paused"),
    ("F04 approved then frozen",                  "Our budget was approved but then frozen.", "research", set(), set(), "paused"),
    ("F05 approved last year, spent",             "The budget was approved last year but we already spent it.", "research", set(), set(), "paused"),
    ("F06 approved in principle, pending sign-off", "The budget is approved in principle, pending sign-off from finance.", "research", set(), set(), "pending"),
    ("F07 approved but not yet released",         "The budget is approved but not yet released.", "research", set(), set(), None),
    ("F08 approval requested",                    "We have requested budget approval.", "research", {"budget"}, set(), None),
    ("F09 hoping to get it approved",             "I am hoping to get the budget approved next month.", "research", {"timing"}, set(), "pending"),
    ("F10 approved, finance must still authorise", "Budget approved, but we still need finance to authorize the spend.", "research", set(), set(), "pending"),
    ("F11 amount only, no approval",              "Our budget is $500 per month.", "research", {"budget"}, set(), None),
    ("F12 conditional approval amount",           "A budget of $500 would be approved if the tool works.", "research", {"budget"}, set(), None),
    ("F13 purchase order on hold",                "The purchase order is on hold.", "research", set(), set(), None),
    ("F14 raised a PO, procurement on hold",      "We raised a purchase order, but procurement has been put on hold.", "research", set(), set(), "paused"),
    ("F15 approved but cancelled",                "Budget approved but cancelled.", "research", set(), set(), "paused"),
    # ── switching / replacement ───────────────────────────────────────────────────────────────────────────────────
    ("G01 looking to replace",                    "Looking to replace our current tool.", "research", set(), set(), None),
    ("G02 might replace",                         "We might replace our current tool.", "research", set(), set(), None),
    ("G03 alternatives to X",                     "Alternatives to Brand24? Our startup finds it too broad.", "research", set(), set(), None),
    ("G04 switched last year",                    "We switched from Brand24 last year.", "research", set(), set(), None),
    ("G05 switching if the demo goes well",       "We are switching from Brand24 if the demo goes well.", "research", {"evaluation"}, set(), "conditional"),
    # ── other ─────────────────────────────────────────────────────────────────────────────────────────────────────
    ("H01 past purchase",                         "We bought a tool last year and it was fine.", "research", set(), set(), None),
]


def _present(sg):
    return {k for k in SIGNALS if sg[k]}


@pytest.mark.parametrize("cid,text,ceiling,present,absent,why", CASES, ids=[c[0] for c in CASES])
def test_signals_and_ceiling(cid, text, ceiling, present, absent, why):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == ceiling, (cid, sg)
    assert present <= _present(sg), (cid, _present(sg), sg)
    assert not (absent & _present(sg)), (cid, _present(sg), sg)
    assert bool(sg["purchase_step"]) == (ceiling == "explicit"), (cid, sg)
    if why:
        assert why in {u["why"] for u in sg["unconfirmed"]}, (cid, sg["unconfirmed"])


def test_a_stated_step_without_a_request_has_no_ceiling():
    """The author must ask for a solution: a step in a post that asks for nothing is not a lead."""
    assert ai._intent_ceiling("Our team is struggling to track Reddit mentions", "We are ready to buy.") == "none"


def test_the_step_sentence_is_returned_so_it_can_be_shown_as_evidence():
    sg = ai.purchase_signals(TITLE, "We are a B2B SaaS team. Our budget is approved for Q3. We would love a demo.")
    assert sg["purchase_step"] == ["Our budget is approved for Q3."]
    assert "We would love a demo." in sg["evaluation"] or sg["evaluation"] == ["We would love a demo."]


# ── classify_post: the rule as a user sees it ────────────────────────────────────────────────────────────────────
def _classify(title, text, model=None, identity="title"):
    sig = {"title": title, "post_text": text, "post_url": "https://www.reddit.com/r/saas/comments/zz1/x/",
           "subreddit": "saas", "platform": "reddit"}
    return ai.classify_post(sig, dict(model or {}), ai.product_terms(QUERY), bool(ai._ICP_MARKERS.search(QUERY)), identity)


WEAK = "We are a B2B SaaS team. Budget is $200 per month and we can start a trial this week, ideally asap."


def test_weaker_signals_together_stay_medium_and_are_listed_in_the_reason():
    out = _classify(TITLE, WEAK)
    assert (out["purchase_intent"], out["product_fit"], out["intent"], out["qualified"]) == ("research", "fits", "Medium", False)
    reason = out["intent_reason"].lower()
    assert "budget" in reason and "timeline" in reason and "trial" in reason and "no confirmed purchase step" in reason
    assert "states no budget" not in reason                       # never denies a signal the post makes
    assert out["purchase_signals"] == {"budget": True, "timing": True, "evaluation": True, "willingness": False,
                                       "unconfirmed": False, "purchase_step": False}


def test_the_same_post_with_an_approved_budget_is_high_and_shows_the_step_as_evidence():
    out = _classify(TITLE, WEAK + " Our budget is approved.")
    assert (out["purchase_intent"], out["intent"], out["qualified"]) == ("explicit", "High", True)
    assert "approved" in out["evidence"].lower() and out["evidence_source"] == "excerpt"
    assert out["purchase_signals"]["purchase_step"] is True


def test_a_budget_amount_is_not_an_approval():
    out = _classify(TITLE, "We are a B2B SaaS team. Our budget is $500 per month.")
    assert (out["intent"], out["qualified"]) == ("Medium", False)


def test_an_explicit_step_with_only_partial_fit_is_not_high():
    out = _classify("Looking for a CRM plugin that logs leads",
                    "We are a B2B company and we also watch Reddit. We plan to subscribe this month.")
    assert out["product_fit"] == "partial" and out["purchase_intent"] == "explicit"
    assert (out["intent"], out["qualified"]) == ("Medium", False)


def test_the_model_cannot_raise_a_post_with_only_weaker_signals():
    out = _classify(TITLE, WEAK, model={"role": "buyer", "purchase_intent": "explicit", "product_fit": "fits"})
    assert (out["purchase_intent"], out["intent"], out["qualified"]) == ("research", "Medium", False)


@pytest.mark.parametrize("text,word", [
    ("We are a B2B SaaS team. We are ready to buy if the pilot works.", "conditional"),
    ("We are a B2B SaaS team. Our budget is approved in principle, pending sign-off from finance.", "not yet authori"),
    ("We are a B2B SaaS team. We are in the procurement process, but it is on hold.", "on hold"),
])
def test_a_demoted_step_is_explained_in_the_reason(text, word):
    out = _classify(TITLE, text)
    assert (out["intent"], out["qualified"]) == ("Medium", False)
    assert out["purchase_signals"]["unconfirmed"] is True and word in out["intent_reason"].lower()


def test_a_negated_step_is_medium():
    out = _classify(TITLE, "We are a B2B SaaS team. We are not ready to buy yet.")
    assert (out["intent"], out["qualified"]) == ("Medium", False)


def test_an_unverified_source_is_still_uncertain_not_judged_on_content():
    out = ai.classify_post({"title": TITLE, "post_text": "", "post_url": "https://www.reddit.com/r/saas/comments/zz2/x/"},
                           {}, ai.product_terms(QUERY), True, "title")
    assert out["intent"] == "Uncertain" and out["qualified"] is False and out["purchase_intent"] == "unknown"


def test_the_definition_shown_to_users_names_the_step_and_the_signals_that_do_not_qualify():
    d = ai.QUALIFIED_DEFINITION.lower()
    assert "explicit purchase step" in d and "approved" in d
    assert "budget, timing, trials, demos and willingness to pay" in d and "do not qualify" in d
    assert "states a budget, trial or purchase step" not in d


# ── documented limitation (G5 follow-up, not part of this change) ────────────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="G5 follow-up: narrative wording such as 'explicit purchase intent' is not yet checked "
                                       "against the qualified rows; only 'high-intent', 'strong' and 'qualified' claims are")
def test_known_limitation_narrative_claim_of_explicit_intent_survives_without_a_qualified_row():
    sig = {"title": TITLE, "post_text": "We are a B2B SaaS team. " + "Budget is $200 per month and we can start a trial this week.",
           "post_url": "https://www.reddit.com/r/saas/comments/zz9/x/", "platform": "reddit", "subreddit": "saas"}
    answer = {"format": "source_list",
              "executive_summary": "One post shows explicit purchase intent with a stated budget.",
              "platforms": [{"platform": "reddit", "posts": [
                  {"index": 1, "link": sig["post_url"], "title": TITLE, "summary": "Needs a Reddit monitoring tool.",
                   "role": "buyer", "purchase_intent": "explicit", "product_fit": "fits"}]}]}
    out = ai.enforce_answer_integrity(answer, [sig], [sig], QUERY)
    assert out["counts"]["qualified"] == 0                                   # true today: the row is Medium
    assert "explicit purchase intent" not in out["executive_summary"].lower()   # NOT true today: the wording survives
