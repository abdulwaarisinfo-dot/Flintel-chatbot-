"""PROPOSED regression tests for the corrective stage (F1, F2, F4). SCRATCH copy: not in the repo. Every sentence is synthetic."""
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

# (id, text, ceiling, why the step was NOT counted (None = no unconfirmed entry needed))
F1 = [
    ("F1-01 customers are ready, not the author", "Our customers are ready to buy but we need a tool to find them.", "research", "other"),
    ("F1-02 'they' with no antecedent is unclear", "They are ready to buy; which tool tracks them?", "research", "ambiguous"),
    ("F1-03 approved last quarter, already bought", "Last quarter our budget was approved and we bought a tool; now another team needs one.", "research", "past"),
    ("F1-04 approved 2 years ago",                "Our budget was approved 2 years ago.", "research", "past"),
    ("F1-05 subject-less 'ready to buy' is the author's", "Ready to buy this month.", "explicit", None),
    ("F1-06 'our team'",                          "Our team is ready to buy this month.", "explicit", None),
    ("F1-07 third party earlier, the author acts", "Our clients want a tool and we are ready to buy one.", "explicit", None),
    ("F1-08 first person",                        "I'm ready to buy one this week.", "explicit", None),
    ("F1-09 recent approval is not stale",        "Our budget was approved this month.", "explicit", None),
    ("F1-10 approval then a new commitment",      "Our budget was approved and we will buy one this month.", "explicit", None),
]
F2 = [
    ("F2-01 negation does not cross 'and'",       "We don't have one and are ready to buy this month.", "explicit", None),
    ("F2-02 'would' comes after the step",        "We are ready to buy and would love your recommendations.", "explicit", None),
    ("F2-03 'no longer' is about the old tool",   "Our current tool is no longer supported, so we are ready to buy a replacement.", "explicit", None),
    ("F2-04 'requested' a demo, not an approval", "We requested a demo and are ready to buy this week.", "explicit", None),
    ("F2-05 directly negated",                    "We are not ready to buy yet.", "research", None),
    ("F2-06 negated within three words",          "We have not yet decided to switch.", "research", None),
    ("F2-07 modal before the step",               "We would be ready to buy next year.", "research", "hypothetical"),
    ("F2-08 'might' before the step",             "We might be ready to buy next quarter.", "research", "hypothetical"),
    ("F2-09 no longer planning",                  "We are no longer planning to buy.", "research", None),
    ("F2-10 approval requested, not granted",     "We have requested budget approval and are ready to buy this week.", "research", "pending"),
    ("F2-11 conditional still demotes",           "We are ready to buy if the pilot works.", "research", "conditional"),
    ("F2-12 paused still demotes",                "We started the procurement process but it is paused.", "research", "paused"),
]


# F1 (policy). WHO a buy / sign / switch commitment belongs to decides whether it counts:
#   me / org  the author, or the author's own organisation: a boss, manager, cofounder, executive, colleague, department or
#             company that is the author's ("our CTO has decided to buy"). Counts as a purchase step.
#   other     customers, clients, users and other outsiders, and generalisations about other companies or teams. Does not count.
#   unclear   a named person, an unanchored role ("the CEO"), an unresolved pronoun, "another team", and a generic company, team or
#             department that nothing ties to the author. Does not count ("ambiguous"), so the post is never High on that alone.
#   hearsay   the author heard, read or was told it ("I heard my boss is ready to buy"). Does not count ("hearsay") unless the post gives
#             an independent purchase step. Qualification still needs the buyer role, product fit, a verified source and analysis.
BUYER_ORG_ROLE = [
 "My boss is ready to buy a tool this month.", "My boss wants a tool, ready to buy this month.", "Our CTO has decided to buy a monitoring tool.",
 "Our CEO is ready to sign this week.", "My manager is about to purchase something.", "My cofounder is ready to buy this month.",
 "Our CFO has agreed to purchase a monitoring tool.", "My colleague Priya has decided to subscribe.", "Sarah, our CFO, is ready to buy this month.",
 "Our team lead is ready to buy this week.", "Our tech lead has decided to buy a monitoring tool.", "My marketing team lead is about to purchase something.",
 "My boss approved the budget and we will buy one this month.",
 "My boss and I are ready to buy this month.", "I and my boss are ready to buy this month.",
]
BUYER_DEPARTMENT = [
 "Our marketing team is ready to buy this month.", "My department is ready to buy a monitoring tool this week.",
 "Our procurement team has decided to buy a monitoring tool.", "Our sales team is about to purchase a CRM add-on.",
 "The marketing team is ready to buy this month and I am collecting options.", "Marketing is ready to buy this month.",
 "The company is ready to buy a monitoring tool this month.", "Our customer support team is ready to buy a helpdesk tool this month.",
 "Our lead gen team is ready to buy this month.", "Our team of 5 people is ready to buy this month.",
]
BUYER_AUTHOR = [
 "Ready to buy this month.", "Looking for a tool, ready to buy this month.", "I'm ready to buy one this week.",
 "Honestly ready to buy this month.", "Still ready to buy a tool this month.", "CEO here, ready to buy a tool this month.",
 "CEO here and looking for a tool, ready to buy this month.", "CEO of a 10-person startup, ready to buy this month.",
 "As the CEO I am ready to buy a tool.", "As a manager, ready to buy one this week.", "I'm the director of marketing and ready to buy this month.",
 "We have 200 users and are ready to buy a tool.", "We need customers, ready to buy a tool this month.", "Using Reddit, ready to buy this month.",
 "Need monitoring on Reddit, ready to buy this month.", "Our company is ready to buy a monitoring tool this month.", "Our company, Acme, is ready to buy a tool this month.",
 "We're a small team, ready to buy this month.", "Our manager approved it and we are ready to buy.",
]
BUYER_BOTH_ACTORS = [
 "My boss is ready to buy and I'm ready to buy too.", "Our customers are ready to buy and we are ready to buy a tool to serve them.",
 "Sarah is ready to buy, and I am ready to buy as well.", "My client said the CEO is ready to buy, and I'm ready to buy too.",
 "I told my boss I'm ready to buy this month.", "We and our customers are ready to buy.",
 "Our customers asked for a tool and I am ready to buy one this week.", "My boss wants a tool, so I'm ready to buy one.",
 "Their team is ready to buy, and our team is ready to buy too.",
]
NOT_BUYER_OUTSIDER = [
 "Our customers are ready to buy but we need a tool to find them.", "Customers are ready to buy; which tool tracks them?",
 "My client is ready to buy a monitoring tool this month.", "I'm helping a client who is ready to buy this week.", "A customer told me they are ready to buy.",
 "Our users are planning to switch to a competitor.", "My customers want a tool and are ready to buy it.", "I run a SaaS and my customers are ready to buy.",
 "Our users love it, ready to buy more seats.", "A customer asked me about it, and they are ready to buy.", "A customer asked me about a tool so they're ready to buy now.",
 "Our customers keep asking and they are ready to buy.", "We told our customers they are ready to buy.",
 "My client's CEO is ready to buy this month.", "A client's team has decided to buy a monitoring tool.", "Our customers' marketing teams are ready to buy.",
 "My client's marketing team is ready to buy this month.", "Companies are ready to buy tools like this.",
 "Marketing teams are ready to buy AI tools, according to a survey.", "Marketing teams everywhere are ready to buy.",
 "Sales departments are ready to buy automation.",
 "My boss says our customers are ready to buy.", "I told my boss the client is ready to buy this month.",
 "Our leads are ready to buy this month.", "My sales leads are about to purchase something.",
]
UNCLEAR_BUYER = [
 "Sarah is ready to buy a tool this month.", "Sarah decided to buy a monitoring tool.", "Mike's ready to buy this week.", "I asked John and he is ready to buy.",
 "Sarah from sales said she's ready to buy.", "Sarah mentioned a few tools and she is ready to buy one.", "The CEO is ready to sign this week.",
 "A manager is about to purchase something.", "They are ready to buy; which tool tracks them?", "Their team is ready to buy this month.",
 "My friend is ready to buy this month.", "Another team is ready to buy this month.", "Someone is ready to buy this month.",
 "Sarah wants a tool, ready to buy this month.", "They want a tool, ready to buy this month.", "A friend asked me for a tool, ready to buy this month.",
 "My client said the CEO is ready to buy.", "A company is ready to buy this month.",
]

# generic company / team / department: the author's only when the sentence or the one before anchors it ("we", "our", "I")
# and names no one else. Run with a NEUTRAL title that anchors nothing.
NEUTRAL_TITLE = "Can anyone recommend a Reddit monitoring tool?"
GENERIC_ANCHORED = [
 "I work in marketing. Marketing is ready to buy this month.", "Our company is looking at tools. The company is ready to buy this month.",
 "The company is ready to buy a monitoring tool this month and I am comparing options.",
 "I lead procurement and we are collecting quotes. Procurement is ready to buy this month.",
]
GENERIC_UNANCHORED = [
 "The company is ready to buy a monitoring tool this month.", "The marketing team is ready to buy this month.", "Marketing is ready to buy this month.",
 "The team is ready to buy this month.", "I work in sales. Marketing is ready to buy this month.",
 "I spoke to a client and the company is ready to buy this month.", "My client has a team. The team is ready to buy this month.",
 "The company is ready to buy, says my client.", "Marketing is ready to buy and Sarah is collecting quotes.",
]
# hearsay: reported, not stated by the buyer. Unconfirmed unless the post gives an independent purchase step.
HEARSAY = [
 "I heard my boss is ready to buy this month.", "My boss said he is ready to buy one.",
 "I read that marketing is ready to buy more tools this year.", "A survey says the marketing team is ready to buy.",
 "Apparently the company is ready to buy a monitoring tool this month.", "Our CTO is ready to buy, apparently.",
 "I asked my boss and he is ready to buy.", "My colleague Priya told me she has decided to subscribe.",
 "I heard we will buy one this month.", "Word is our CFO has agreed to purchase a monitoring tool.", "My boss told me he is ready to buy this week.",
]
HEARSAY_WITH_EVIDENCE = [
 "I heard my boss is ready to buy, and the budget was approved this month.", "I heard my boss is ready to buy. Our budget was approved this month.",
 "I heard my boss is ready to buy. We are ready to buy too.",
]
NOT_HEARSAY = [
 "My boss is ready to buy this month.", "I asked around for a tool and we are ready to buy one this month.",
 "I heard good things about it and we are ready to buy one this month.",
 "I read the docs and we are ready to buy this month.", "Our CTO has decided to buy a monitoring tool.",
]


# first-person wording ("we will buy", "we're ready to buy") is not a verified commitment by itself. Three different things:
#   a REPORTED statement   the author relays it (a boss told me, I heard, apparently)           -> "hearsay", unconfirmed
#   a SPECULATIVE one      the author guesses, hopes, or the commitment is vague (probably, someday)  -> "hypothetical", unconfirmed
#   an AUTHOR-SIDE one     the author states it in their own voice                               -> counts
# and an independent purchase step elsewhere in the post counts on its own, whatever the first-person statement is.
FIRST_PERSON_REPORTED = [
    "My boss told me we will buy one this week.", "My boss said we're ready to buy.", "She told me we are going to buy a tool this month.",
    "Our CFO said we will buy it this week.", "I heard we will buy one this month.", "Apparently we will buy a monitoring tool this month.",
    "Word is we will buy a monitoring tool this month.",
]
FIRST_PERSON_SPECULATIVE = [
    "I think we will buy one this month.", "I guess we will buy a tool this month.", "I hope we will buy one this month.",
    "We will probably buy one this month.", "We'll likely buy one this month.", "Hopefully we will buy one this month.",
    "We will buy one eventually.", "We will buy one someday.", "We will buy one at some point.", "We will buy one, I guess.",
    "Maybe we will buy one this month.", "I think my boss is ready to buy.",
]
FIRST_PERSON_AUTHOR_SIDE = [
    "We will buy one this month.", "We will definitely buy one this week.", "I will buy one this month.", "We're going to buy one this week.",
    "I told my boss we will buy one this month.", "I asked around and we will buy one this month.",
    "We are ready to buy and would love your recommendations.", "We will buy one this month and probably add seats later.",
    "I think the tools are great and we will buy one this month.", "My boss said we're ready to buy, and I'm ready to buy too.",
]
# Uncertainty only counts when it qualifies THE STEP: up to four words before it, inside it, or within six words after it, never across
# "and / so / but / because" or a clause break, and never from another sentence. G1's own markers (would, maybe, probably, someday,
# eventually ...) are kept as they were; "I think", "likely", "hopefully", "at some point" are NOT sentence-wide demotion rules.
UNCERTAINTY_ABOUT_THE_STEP = [
    "I think we will buy one this month.", "I think our team is ready to buy this month.", "I guess we will buy a tool this month.",
    "We'll likely buy one this month.", "Likely we will buy one this month.", "Hopefully we will buy one this month.",
    "We will buy one at some point.", "We will buy one sometime next year.", "We will buy one, I guess.", "We will buy one this month, likely.",
    "I hope we are ready to buy this month.", "I'm not sure but I think we will buy one.",
    "We will buy one this month, I think.", "We will buy one this month, I hope.", "We will buy one this month (I guess).",
    "We will buy one this month, I suppose so.", "We will buy one this month, hopefully before Friday.",
    "We will buy one this month, likely from Acme.", "We will buy a license for the whole team next quarter, I guess.",
    "We will buy a license for the whole team next quarter (I guess).", "We will buy a license for the whole team next quarter, I think.",
    "We will probably buy one this month.", "We will buy one eventually.", "Maybe we will buy one this month.",       # G1's own markers
]
UNCERTAINTY_NOT_ABOUT_THE_STEP = [
    "I think the tools are great and we will buy one this month.", "I think Reddit monitoring is useful, we will buy one this month.",
    "I think this is a good tool so we will buy one this month.", "We will buy one this month. Hopefully it works.",
    "We will buy one this month. I think it is great.", "We will buy one this month because I think it is the best option.",
    "We will buy one this month, and I think it will pay off.", "Our budget is likely larger than $5k and we will buy one this month.",
    "I think the best option is clear: we will buy one this month.", "We will buy one this month; likely add seats later.",
    "Likely buyers are small teams. We will buy one this month.", "We will buy one this month and probably add seats later.",
    "I think it works so we will buy one this month.", "I think it is good and we will buy one this month.",
    "We will buy one this month, I think it is great.", "We will buy one this month, I guess it is fine.",   # an opinion about the tool,
    "We will buy one this month, I hope it works.", "We will buy one this month (I think it is great).",    # not about the purchase
    "We will buy one this month, I believe the pricing is fair.",
    "We will buy a license for the whole team next quarter, I think it is great.", "We will buy a license for the whole team next quarter, I guess it is fine.",
    "I think everyone on the team agrees that this is the one we will buy this month.",
]
UNCERTAINTY_WITH_INDEPENDENT_STEP = [
    "I think we will buy one this month, and our budget was approved.",
    "Hopefully we will buy one this month. Our budget was approved last week.",
    "We will probably buy one next year. We are ready to buy one this week.",
    "Likely we will buy one eventually, but our budget is approved.",
]
FIRST_PERSON_WITH_INDEPENDENT_SUPPORT = [
    "My boss told me we will buy one this week. Our budget was approved this month.",
    "I think we will buy one this month, and our budget was approved.",
    "I heard we will buy one this month. We are ready to buy one this week.",
]


_ID = lambda t: t[:60]  # noqa: E731
NEUTRAL = ("Can anyone recommend a Reddit monitoring tool?")  # a title that anchors nothing to the author


@pytest.mark.parametrize("text", BUYER_ORG_ROLE + BUYER_DEPARTMENT + BUYER_AUTHOR + BUYER_BOTH_ACTORS + NOT_HEARSAY + FIRST_PERSON_AUTHOR_SIDE, ids=_ID)
def test_f1_organisational_or_author_commitment_counts(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "explicit" and sg["purchase_step"], sg


@pytest.mark.parametrize("text", BUYER_ORG_ROLE + BUYER_DEPARTMENT + BUYER_AUTHOR + NOT_HEARSAY, ids=_ID)
def test_f1_a_counted_commitment_leaves_nothing_unconfirmed(text):
    assert not ai.purchase_signals(TITLE, text)["unconfirmed"]


@pytest.mark.parametrize("text", NOT_BUYER_OUTSIDER, ids=_ID)
def test_f1_outsider_commitment_does_not_count(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "research" and not sg["purchase_step"], sg
    assert "other" in {u["why"] for u in sg["unconfirmed"]}, sg


@pytest.mark.parametrize("title,text", [(TITLE, t) for t in UNCLEAR_BUYER] + [(NEUTRAL, t) for t in GENERIC_UNANCHORED],
                         ids=lambda v: v[:60])
def test_f1_unclear_buyer_is_never_rated_high(title, text):
    sg = ai.purchase_signals(title, text)
    assert ai._intent_ceiling(title, text) == "research" and not sg["purchase_step"], sg
    assert "ambiguous" in {u["why"] for u in sg["unconfirmed"]}, sg
    out = _classify(text, title)
    assert (out["intent"], out["qualified"]) == ("Medium", False), out
    assert "unclear" in out["intent_reason"], out["intent_reason"]


@pytest.mark.parametrize("text", GENERIC_ANCHORED, ids=_ID)
def test_f1_a_generic_company_or_department_counts_when_the_context_anchors_it_to_the_author(text):
    sg = ai.purchase_signals(NEUTRAL, text)
    assert ai._intent_ceiling(NEUTRAL, text) == "explicit" and sg["purchase_step"] and not sg["unconfirmed"], sg


@pytest.mark.parametrize("text", GENERIC_ANCHORED[:2], ids=_ID)
def test_f1_the_same_generic_sentence_is_unclear_without_the_anchor(text):
    bare = text.split(". ")[-1]
    assert ai._intent_ceiling(NEUTRAL, bare) == "research", bare


@pytest.mark.parametrize("text", HEARSAY, ids=_ID)
def test_f1_hearsay_is_unconfirmed(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "research" and not sg["purchase_step"], sg
    assert "hearsay" in {u["why"] for u in sg["unconfirmed"]}, sg
    out = _classify(text)
    assert (out["intent"], out["qualified"]) == ("Medium", False) and "heard or read" in out["intent_reason"], out


@pytest.mark.parametrize("text", HEARSAY_WITH_EVIDENCE, ids=_ID)
def test_f1_hearsay_does_not_cancel_an_independent_purchase_step(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "explicit" and sg["purchase_step"], sg
    assert "hearsay" in {u["why"] for u in sg["unconfirmed"]}, sg                       # still listed as unconfirmed


def test_f1_budget_timing_and_evaluation_are_not_extra_evidence_for_hearsay():
    out = _classify("I heard my boss is ready to buy. Budget is $500 per month, we want a trial this week, asap.")
    assert (out["intent"], out["qualified"]) == ("Medium", False), out


@pytest.mark.parametrize("text", [
    "Sarah is ready to buy, and our budget was approved this month.",
    "Sarah is ready to buy and we are ready to buy too.",
    "They are ready to buy; our budget was approved this month.",
    "My client said the CEO is ready to buy, and I'm ready to buy too.",
], ids=_ID)
def test_f1_an_unclear_statement_does_not_cancel_a_genuine_step_elsewhere(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "explicit" and sg["purchase_step"], sg


def test_f1_the_same_words_are_split_by_who_the_subject_is():
    # a department is not decided by its name: the purchasing team counts, a discussion about other teams does not
    for yes, no in [("Our marketing team is ready to buy this month.", "Marketing teams are ready to buy this month, says a survey."),
                    ("Our sales team is about to purchase a CRM add-on.", "Sales departments are ready to buy automation."),
                    ("My boss is ready to buy this month.", "My client's boss is ready to buy this month."),
                    ("My boss is ready to buy this month.", "I heard my boss is ready to buy this month."),
                    ("Our CTO has decided to buy a tool.", "Sarah decided to buy a tool.")]:
        assert ai._intent_ceiling(TITLE, yes) == "explicit", yes
        assert ai._intent_ceiling(TITLE, no) == "research", no


@pytest.mark.parametrize("text", FIRST_PERSON_REPORTED, ids=_ID)
def test_first_person_wording_that_is_reported_is_hearsay(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "research" and not sg["purchase_step"], sg
    assert "hearsay" in {u["why"] for u in sg["unconfirmed"]}, sg


@pytest.mark.parametrize("text", FIRST_PERSON_SPECULATIVE, ids=_ID)
def test_first_person_wording_that_is_speculative_is_not_a_commitment(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "research" and not sg["purchase_step"], sg
    assert "hypothetical" in {u["why"] for u in sg["unconfirmed"]}, sg
    out = _classify(text)
    assert (out["intent"], out["qualified"]) == ("Medium", False), out


@pytest.mark.parametrize("text", UNCERTAINTY_ABOUT_THE_STEP, ids=_ID)
def test_uncertainty_that_qualifies_the_step_makes_it_unconfirmed(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "research" and not sg["purchase_step"], sg
    assert "hypothetical" in {u["why"] for u in sg["unconfirmed"]}, sg


@pytest.mark.parametrize("text", UNCERTAINTY_NOT_ABOUT_THE_STEP, ids=_ID)
def test_uncertainty_elsewhere_in_the_sentence_does_not_demote_the_step(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "explicit" and sg["purchase_step"] and not sg["unconfirmed"], sg


def test_after_the_step_i_guess_qualifies_the_purchase_but_i_think_it_is_great_does_not():
    # the same words after the step: they qualify the commitment only when they end the clause
    unsure = ai.purchase_signals(TITLE, "We will buy one this month, I guess.")
    opinion = ai.purchase_signals(TITLE, "We will buy one this month, I think it is great.")
    assert ai._intent_ceiling(TITLE, "We will buy one this month, I guess.") == "research" and not unsure["purchase_step"]
    assert "hypothetical" in {u["why"] for u in unsure["unconfirmed"]}
    assert ai._intent_ceiling(TITLE, "We will buy one this month, I think it is great.") == "explicit"
    assert opinion["purchase_step"] and not opinion["unconfirmed"]
    row = _classify("We are a B2B SaaS team. We will buy one this month, I think it is great.")
    assert (row["intent"], row["qualified"]) == ("High", True), row


def test_a_clause_ending_i_guess_qualifies_the_purchase_however_long_the_clause_is():
    long_clause = "We will buy a license for the whole team next quarter, I guess."          # the marker is more than six words after the step
    sg = ai.purchase_signals(TITLE, long_clause)
    assert ai._intent_ceiling(TITLE, long_clause) == "research" and not sg["purchase_step"], sg
    assert "hypothetical" in {u["why"] for u in sg["unconfirmed"]}, sg
    # and the opinion about the tool is still not read as hedging the purchase, short or long
    for opinion in ("We will buy one this month, I think it is great.",
                    "We will buy a license for the whole team next quarter, I think it is great."):
        assert ai._intent_ceiling(TITLE, opinion) == "explicit", opinion


def test_a_hedge_after_a_second_comma_is_not_attributed_to_the_purchase():
    # documents the limit of the rule: the hedge must follow the step's own clause after at most one comma or parenthesis
    assert ai._intent_ceiling(TITLE, "We will buy one this month, the tool has everything we need, I think.") == "explicit"


def test_a_marker_cut_off_by_the_edge_of_the_window_is_not_read_as_ending_the_clause():
    # "I guess" falls exactly on the edge of the 120-character window and the sentence goes on: it must not be taken as the clause end
    text = "We will buy " + "x" * 110 + ", I guess it is fine."
    assert ai._intent_ceiling(TITLE, text) == "explicit"


@pytest.mark.parametrize("text", UNCERTAINTY_WITH_INDEPENDENT_STEP, ids=_ID)
def test_an_uncertain_step_does_not_cancel_an_independent_one(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "explicit" and sg["purchase_step"], sg
    assert "hypothetical" in {u["why"] for u in sg["unconfirmed"]}, sg                  # the uncertain statement is still listed


@pytest.mark.parametrize("text", FIRST_PERSON_WITH_INDEPENDENT_SUPPORT, ids=_ID)
def test_an_independent_step_counts_whatever_the_first_person_statement_is(text):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == "explicit" and sg["purchase_step"], sg
    assert sg["unconfirmed"], sg                                                         # the unsupported statement is still listed


def test_the_three_kinds_of_first_person_statement_are_distinguished():
    for reported, speculative, own in [("My boss told me we will buy one this week.", "I think we will buy one this week.", "We will buy one this week."),
                                       ("I heard we are ready to buy this month.", "We are probably ready to buy this month.", "We are ready to buy this month.")]:
        assert ai._intent_ceiling(TITLE, reported) == "research"
        assert ai._intent_ceiling(TITLE, speculative) == "research"
        assert ai._intent_ceiling(TITLE, own) == "explicit"


@pytest.mark.parametrize("text", ["We will buy one this month.", "The company is ready to buy this month."], ids=_ID)
def test_an_author_side_commitment_still_needs_every_other_gate(text):
    # attribution alone never makes a High lead: request, product fit, a verified analysis and a verifiable source still apply
    ok = "We are a B2B SaaS team. " + text
    assert _classify(ok)["qualified"] is True                                            # the control
    assert _classify(ok, identity=None)["qualified"] is False                            # analysis not provably about this post
    assert _classify(text, "Our team is struggling to track Reddit mentions")["qualified"] is False   # no request for a solution
    out = _classify("We are a B2B company and we also watch Reddit. " + text, "Looking for a CRM plugin that logs leads")
    assert out["product_fit"] == "partial" and (out["intent"], out["qualified"]) == ("Medium", False)
    sig = {"title": TITLE, "post_text": ok, "post_url": "", "subreddit": "saas", "platform": "reddit"}
    assert ai.classify_post(sig, {}, ai.product_terms(QUERY), True, "title")["qualified"] is False   # no verifiable source


def test_f1_both_actors_record_the_outsider_but_keep_the_author_step():
    sg = ai.purchase_signals(TITLE, "Our customers are ready to buy and we are ready to buy a tool to serve them.")
    assert "other" in {u["why"] for u in sg["unconfirmed"]} and sg["purchase_step"]


# an author-owned organisation is attributed, but that alone never makes a High-intent lead: every other gate still applies
def test_f1_an_attributed_company_statement_still_needs_product_fit():
    out = _classify("We are a B2B company and we also watch Reddit. The company plans to subscribe this month.", "Looking for a CRM plugin that logs leads")
    assert out["purchase_intent"] == "explicit" and out["product_fit"] == "partial"
    assert (out["intent"], out["qualified"]) == ("Medium", False)


def test_f1_an_attributed_company_statement_still_needs_a_request_for_a_solution():
    out = _classify("The company is ready to buy.", "Our team is struggling to track Reddit mentions")
    assert out["purchase_intent"] == "none" and out["qualified"] is False


def test_f1_an_attributed_company_statement_still_needs_a_verified_analysis_and_source():
    text = "We are a B2B SaaS team. The company is ready to buy this month."
    assert _classify(text)["qualified"] is True                                    # the control
    assert _classify(text, identity=None)["qualified"] is False                    # analysis not provably about this post
    sig = {"title": TITLE, "post_text": text, "post_url": "", "subreddit": "saas", "platform": "reddit"}
    out = ai.classify_post(sig, {}, ai.product_terms(QUERY), True, "title")
    assert out["qualified"] is False and out["intent"] != "High", out                     # no verifiable source


def test_f1_only_commitments_are_attributed_to_an_actor():
    # an approval or procurement step is not attributed to an actor, so a boss's or finance's approval still counts
    for text in ("Our budget was approved by finance this month.", "My boss approved the budget.", "Procurement process has started."):
        assert ai._intent_ceiling(TITLE, text) == "explicit", text


@pytest.mark.xfail(strict=True, reason="KNOWN LIMIT, out of scope: only buy / sign / switch commitments are attributed to an actor and checked "
                                       "for hearsay or speculation. An approved budget or a procurement step is not, so a customer's, a "
                                       "third party's, a reported or a guessed approval still counts, exactly as in G1. Attributing "
                                       "approvals changes the approval logic and is a separate stage.")
@pytest.mark.parametrize("text", ["Our customers' budget was approved this month.", "Their budget was approved this month.",
                                  "I heard the budget was approved this month.", "I think our budget is approved.",
                                  "My boss said the budget has been approved.", "Apparently the budget is approved."], ids=_ID)
def test_known_limitation_approvals_are_not_attributed(text):
    assert ai._intent_ceiling(TITLE, text) == "research"




@pytest.mark.parametrize("cid,text,ceiling,why", F1 + F2, ids=[c[0] for c in F1 + F2])
def test_f1_f2_ceiling(cid, text, ceiling, why):
    sg = ai.purchase_signals(TITLE, text)
    assert ai._intent_ceiling(TITLE, text) == ceiling, (cid, sg)
    if why:
        assert why in {u["why"] for u in sg["unconfirmed"]}, (cid, sg["unconfirmed"])


def _classify(text, title=TITLE, query=QUERY, identity="title"):
    sig = {"title": title, "post_text": text, "post_url": "https://www.reddit.com/r/saas/comments/zz1/x/",
           "subreddit": "saas", "platform": "reddit"}
    return ai.classify_post(sig, {}, ai.product_terms(query), bool(ai._ICP_MARKERS.search(query)), identity)


@pytest.mark.parametrize("text", ["We have approval but procurement is paused.", "We need to replace our current tool."])
def test_f4_reason_never_says_the_post_states_nothing(text):
    out = _classify(text)
    assert (out["intent"], out["qualified"]) == ("Medium", False)
    reason = out["intent_reason"]
    assert "states no" not in reason and "no budget, timeline or purchase step was recognised" in reason


def test_f4_a_recognised_signal_keeps_its_specific_reason():
    out = _classify("We are a B2B SaaS team. Budget is $200 per month.")
    assert "states a budget" in out["intent_reason"] and "recognised" not in out["intent_reason"]


def test_a_customers_readiness_no_longer_makes_a_high_intent_lead():
    out = _classify("Our customers are ready to buy but we need a tool to find them.")
    assert (out["purchase_intent"], out["intent"], out["qualified"]) == ("research", "Medium", False)
    assert "someone else" in out["intent_reason"]


# general-purpose guard: posts on a non-lead query that are not buyers never gain intent or a qualified flag
@pytest.mark.parametrize("query,title,text", [
    ("What do people think about the new Notion pricing changes?", "Notion's new pricing is a rip-off for small teams",
     "The new Notion pricing doubled our bill. Our customers are ready to buy elsewhere."),
    ("Remote work trends in 2026", "Remote work is shrinking in 2026",
     "Remote work trends in 2026 show companies pulling people back. Budget was approved last year."),
])
def test_non_buyer_rows_are_unaffected(query, title, text):
    out = _classify(text, title, query)
    assert out["role"] == "discussion" and out["purchase_intent"] == "none" and out["qualified"] is False and out["intent"] == "Low"


# ---- per-sentence cache -------------------------------------------------------------------------------------------------
import time  # noqa: E402

_CORPUS = [c[1] for c in F1 + F2] + BUYER_ORG_ROLE + BUYER_DEPARTMENT + BUYER_AUTHOR + BUYER_BOTH_ACTORS + NOT_BUYER_OUTSIDER + UNCLEAR_BUYER + HEARSAY + GENERIC_ANCHORED + GENERIC_UNANCHORED + [
    "We have approval but procurement is paused.", "We need to replace our current tool.",
    "Budget is $200 per month. We are ready to buy if the pilot works. It depends on legal.",
    "We are ready to buy, but only if the demo goes well, and we will sign this week; the budget was approved last quarter.",
    "Ready to buy. Ready to buy. Ready to buy! Is the budget approved? Our budget was approved and we will buy this month.",
    "x" * 300 + " ready to buy this month " + "y" * 300,
]


def _without_cache(monkeypatch):
    real = ai._sentence_facts
    monkeypatch.setattr(ai, "_sentence_facts", lambda sentences, i, cache: real(sentences, i, {}))


@pytest.mark.parametrize("text", _CORPUS)
def test_cache_preserves_every_classification_result(monkeypatch, text):
    cached = ai.purchase_signals(TITLE, text)
    ceiling = ai._intent_ceiling(TITLE, text)
    _without_cache(monkeypatch)
    assert ai.purchase_signals(TITLE, text) == cached
    assert ai._intent_ceiling(TITLE, text) == ceiling


def test_cache_preserves_the_full_row_for_classified_posts(monkeypatch):
    rows = [_classify(t) for t in _CORPUS]
    _without_cache(monkeypatch)
    assert [_classify(t) for t in _CORPUS] == rows


def test_sentence_facts_are_computed_once_per_sentence_however_many_steps_it_has(monkeypatch):
    calls = []
    real = ai._clause_spans
    monkeypatch.setattr(ai, "_clause_spans", lambda s: (calls.append(s), real(s))[1])
    text = " ".join(["ready to buy"] * 40) + "."                       # 40 steps in ONE sentence
    ai.purchase_signals(TITLE, text)
    assert len(calls) == 1, len(calls)                                # one scan of that sentence, never one per step
    calls.clear()
    ai.purchase_signals(TITLE, "Ready to buy this month. Ready to buy this week. We will buy one.")
    assert len(calls) == 3, len(calls)                                # 3 sentences with steps: one scan each


def _best(fn, runs=3):
    best = None
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best


@pytest.mark.parametrize("unit", ["ready to buy ", "ready to buy and ", "ready to buy, ", "we will buy; ", "Ready to buy. ",
                                  "We are ready to buy this month and "])
def test_repeated_sentence_patterns_do_not_grow_faster_than_linearly(unit):
    small, big = unit * 150, unit * 600                                # 4x the input
    t_small = _best(lambda: ai.purchase_signals(TITLE, small))
    t_big = _best(lambda: ai.purchase_signals(TITLE, big))
    assert t_big < max(t_small, 0.002) * 4 * 3, (unit, t_small, t_big)  # 4x input, allow 3x slack for noise: nothing quadratic


# ---- product relevance: documented, NOT changed by this stage ---------------------------------------------------------------
def test_a_post_entirely_about_another_product_is_still_excluded():
    out = _classify("We are a B2B company. We will buy a new office chair this month.", "Need recommendations for an ergonomic office chair")
    assert out.get("_exclude") == "irrelevant"


@pytest.mark.parametrize("text", ["We will buy a Reddit monitoring tool this month.", "We are ready to buy this month.",
                                  "Our budget is approved.", "The company is ready to buy; I'm just comparing options."], ids=_ID)
def test_a_genuine_commitment_in_a_post_about_the_target_product_is_still_high(text):
    out = _classify("We are a B2B SaaS team. " + text)
    assert (out["purchase_intent"], out["product_fit"], out["intent"], out["qualified"]) == ("explicit", "fits", "High", True), out


@pytest.mark.xfail(strict=True, reason="KNOWN LIMIT, out of scope (product fit is not changed in this stage): fit is judged for the whole "
                                       "post and the purchase step is never tied to the product, so a commitment to buy something unrelated "
                                       "inside a post that does ask for the target product still qualifies. Same result before G1 and in G1.")
def test_known_limitation_an_unrelated_purchase_inside_a_relevant_post_still_qualifies():
    out = _classify("We are a B2B SaaS team. We will buy a new office chair this month.")
    assert out["qualified"] is False


@pytest.mark.xfail(strict=True, reason="KNOWN LIMIT, deliberately unchanged: a pronoun takes its actor from the same sentence only, so "
                                       "'He' in the next sentence stays unclear (never High) even when the previous sentence names the boss.")
def test_known_limitation_a_pronoun_is_not_resolved_across_sentences():
    assert ai._intent_ceiling(TITLE, "My boss wants a tool. He is ready to buy this month.") == "explicit"
