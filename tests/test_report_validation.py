"""
tests/test_report_validation.py — report-validation guardrails (answer_integrity.py)
run through the real logics._patch_post_urls_into_answer().

SYNTHETIC FIXTURES. The posts below are invented test data that model each
failure PATTERN found in the audit of a saved six-post report; they are not
that report's stored records, which were not available.
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import answer_integrity as ai  # noqa: E402
from tests.test_answer_persistence import env  # noqa: E402,F401

QUERY = ("I run a B2B SaaS company that helps businesses monitor Reddit conversations and find people "
         "actively looking for a solution like ours.")

POSTS = {   # prompt number -> (subreddit, title, text)
    1: ("agency", "Which tool do you use to track brand mentions on Reddit?",
        "We are a small agency and want a tool to track brand mentions across subreddits. Any recommendations?"),
    2: ("SaaS", "Need a Reddit monitoring tool, budget $200 per month",
        "Looking for a Reddit monitoring tool for our SaaS. Budget is $200 per month and we can start a trial this week. "
        "The budget is approved."),
    3: ("SaaS", "I built a Reddit lead finder",
        "I built a tool that scores Reddit threads by buying intent for SaaS founders and can track brand "
        "mentions across subreddits. Free tier available."),
    4: ("marketing", "Recommend me a social listening tool for marketing teams",
        "Our marketing team needs a social listening tool that covers Reddit. What do you recommend?"),
    5: ("cooking", "Best cast iron pan for a beginner?", "Which pan should I buy for searing steak at home?"),
}


def _url(n):
    return f"https://www.reddit.com/r/{POSTS[n][0]}/comments/syn{n}/post_{n}/"


def _pool(hour="12:00"):
    return [{"title": t, "post_text": x, "post_url": _url(n), "platform": "reddit", "subreddit": s,
             "created_utc": f"2026-10-0{n}T{hour}:00+00:00"} for n, (s, t, x) in POSTS.items()]


def _entry(n, summary, **labels):
    return dict({"index": n, "title": POSTS[n][1], "summary": summary, "sentiment": "neutral"}, **labels)


OWN = {   # each post's own, supported summary
    1: "Agency wants a tool to track brand mentions across subreddits.",
    2: "Wants a Reddit monitoring tool with a $200 monthly budget and a trial this week.",
    3: "Builder of a tool that scores Reddit threads by buying intent.",
    4: "Marketing team needs a social listening tool that covers Reddit.",
}


def _answer(posts, **narrative):
    return json.dumps(dict({"format": "source_list", "headline": "Reddit monitoring demand",
                            "platforms": [{"platform": "reddit", "total_analyzed": len(posts), "shown_count": len(posts),
                                           "posts": posts}]}, **narrative))


def _run(lg, posts, hour="12:00", **narrative):
    return json.loads(lg._patch_post_urls_into_answer(_answer(posts, **narrative), _pool(hour), query=QUERY))


def _rows(out):
    return [p for e in out["platforms"] for p in e["posts"]]


def _row(out, n):
    return next(p for p in _rows(out) if p["link"] == _url(n))


def _table_no(out, n):
    return _row(out, n)["index"]


@pytest.fixture
def lg(env):
    return env[0]


# ── 1. an analysis counts for a post only with proof of identity ─────────────
# Proof = the model's own title equals the stored title AND its post number is
# that post's number, or a quotation of 4+ words found in this post and in no
# other retrieved post. Summary word overlap is never proof.

def test_1_wrong_same_topic_index_does_not_carry_its_analysis(lg):
    # Entry numbered 1 (agency, no budget) but written about post 2 (budget, trial).
    out = _run(lg, [dict(_entry(1, OWN[2], role="buyer", purchase_intent="explicit", product_fit="fits",
                                evidence="Budget is $200 per month"), title=POSTS[2][1])])
    row = _row(out, 1)                                    # the resolver did attach it to post 1
    assert row["summary"] == "Summary unavailable" and "$200" not in json.dumps(row)
    assert row["analysis_verified"] is False and row["analysis_identity"] is None
    assert row["qualified"] is False and row["intent"] == "Uncertain"
    assert any("could not confirm" in u for u in row["uncertainty"])
    assert row["role"] == "buyer"                         # from post 1's own text, not the entry's labels


def test_2_title_and_post_number_establish_identity(lg):
    out = _run(lg, [_entry(2, OWN[2], role="buyer", purchase_intent="explicit", product_fit="fits")])
    row = _row(out, 2)
    assert row["analysis_identity"] == "title" and row["summary"] == OWN[2]
    assert row["intent"] == "High" and row["qualified"] is True
    assert "_model_title" not in row and "_model_index" not in row


def test_3_unique_quotation_establishes_identity_and_unsupported_summary_is_hidden(lg):
    out = _run(lg, [dict(_entry(1, "Agency tracking brand mentions for 12 client brands across subreddits.",
                                role="buyer", purchase_intent="research", product_fit="fits",
                                evidence="want a tool to track brand mentions"), title="Agency question")])
    row = _row(out, 1)
    assert row["analysis_identity"] == "quote" and row["evidence_source"] == "model_quote"
    assert row["summary"] == "Summary unavailable"        # "12" is not in the post


def test_4_summary_overlap_is_not_identity(lg):
    # The summary is fully supported by post 2, but neither the title nor a quotation ties it there.
    out = _run(lg, [dict(_entry(2, OWN[2], role="buyer", purchase_intent="explicit", product_fit="fits"),
                         title="Monitoring tool request")])
    row = _row(out, 2)
    assert row["analysis_verified"] is False and row["qualified"] is False and row["intent"] == "Uncertain"
    assert row["summary"] == "Summary unavailable" and out["counts"]["qualified"] == 0


def test_5_labels_alone_are_not_identity(lg):
    out = _run(lg, [{"index": 2, "role": "buyer", "purchase_intent": "explicit", "product_fit": "fits"}])
    row = _row(out, 2)
    assert row["analysis_verified"] is False and row["qualified"] is False


def test_6_quotation_found_in_another_post_is_not_identity(lg):
    shared = "track brand mentions across subreddits"           # in posts 1 and 3
    assert shared in POSTS[1][2] and shared in POSTS[3][2]
    out = _run(lg, [dict(_entry(1, OWN[1], role="buyer", purchase_intent="research", product_fit="fits",
                                evidence=shared), title="Agency question")])
    assert _row(out, 1)["analysis_verified"] is False


def test_7_correct_title_with_another_post_number_is_not_identity(lg):
    out = _run(lg, [{"index": 4, "title": POSTS[1][1], "role": "buyer", "purchase_intent": "explicit",
                     "product_fit": "fits"}])
    row = _row(out, 4)                                    # attached by its number
    assert row["analysis_verified"] is False and row["qualified"] is False


def test_8_short_quotation_is_not_identity(lg):
    out = _run(lg, [dict(_entry(1, OWN[1], role="buyer", purchase_intent="research", product_fit="fits",
                                evidence="Any recommendations?"), title="Agency question")])
    assert _row(out, 1)["analysis_verified"] is False


def test_9_strict_mode_exact_title_establishes_identity(lg, monkeypatch):
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: True)
    out = _run(lg, [_entry(2, "Founder wants a monitoring tool.", role="buyer", purchase_intent="explicit",
                           product_fit="fits")])
    row = _row(out, 2)
    assert row["analysis_identity"] == "title" and row["intent"] == "High"
    assert "_model_title" not in row and "_model_index" not in row


def test_10_strict_mode_similar_title_is_not_identity(lg, monkeypatch):
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: True)
    out = _run(lg, [dict(_entry(2, OWN[2], role="buyer", purchase_intent="explicit", product_fit="fits"),
                         title="Need a Reddit monitoring tool with budget $200 per month")])
    rows = [r for r in _rows(out) if r["link"] == _url(2)] if out.get("format") == "source_list" else []
    assert all(r["analysis_verified"] is False and not r["qualified"] for r in rows)
    assert out.get("counts", {}).get("qualified", 0) == 0


# ── 2. reasons never deny what the displayed post shows ─────────────────────

def _post_2_without_its_approval(monkeypatch):
    """Post 2 as a research-level post: it states a budget, a trial and a timeline but no purchase step."""
    weak = dict(POSTS)
    weak[2] = POSTS[2][:2] + (POSTS[2][2].replace(" The budget is approved.", ""),)
    assert "approved" not in weak[2][2]
    monkeypatch.setitem(globals(), "POSTS", weak)


def test_11_reason_never_denies_a_budget_the_post_states(lg, monkeypatch):
    _post_2_without_its_approval(monkeypatch)             # the premise: a research-level post that states a budget
    out = _run(lg, [_entry(2, OWN[2], role="buyer", purchase_intent="research", product_fit="fits")])
    row = _row(out, 2)
    assert row["purchase_intent"] == "research" and "$200" in row["evidence"]
    assert "states no budget" not in row["intent_reason"] and "budget" in row["intent_reason"]


def test_12_reason_never_denies_a_request_the_post_makes(lg):
    out = _run(lg, [_entry(4, OWN[4], role="discussion", purchase_intent="none", product_fit="fits")])
    row = _row(out, 4)
    assert row["role"] == "discussion"
    assert "not asking for a tool" not in row["intent_reason"] and "asks for a tool" in row["intent_reason"]


# ── 3. narrative claims agree with the rows and the qualified count ─────────

def test_13_role_given_to_a_cited_post_must_match_its_row(lg):
    out = _run(lg, [_entry(2, OWN[2], role="buyer", purchase_intent="explicit", product_fit="fits"),
                    _entry(3, OWN[3], role="competitor_promo", purchase_intent="none", product_fit="partial")],
               business_insight_points=["Use the testimonial (Post 3) as social proof.",
                                        "Reply to Post 2 with a pilot offer."])
    assert out["business_insight_points"] == [f"Reply to Post {_table_no(out, 2)} with a pilot offer."]


def test_14_demand_claimed_from_vendor_rows_is_removed(lg):
    out = _run(lg, [_entry(3, OWN[3], role="competitor_promo", purchase_intent="none", product_fit="partial"),
                    _entry(4, OWN[4], role="buyer", purchase_intent="research", product_fit="fits")],
               market_pattern="Post 3 shows related need. Post 4 asks for a social listening tool.")
    assert out["market_pattern"] == f"Post {_table_no(out, 4)} asks for a social listening tool."


def test_15_strength_claims_need_a_qualified_row(lg):
    posts = [_entry(1, OWN[1], role="buyer", purchase_intent="research", product_fit="fits"),
             _entry(4, OWN[4], role="buyer", purchase_intent="research", product_fit="fits")]
    out = _run(lg, posts, conclusion="Two posts are strong, immediate outreach opportunities; the rest map competitors.",
               market_pattern="Shows pockets of high-intent buyer activity.",
               business_insight="No post shows high-intent buying yet.")
    assert out["counts"]["qualified"] == 0
    assert "strong, immediate" not in out["conclusion"] and "the rest map competitors" in out["conclusion"]
    assert out["market_pattern"] == ""
    assert out["business_insight"] == "No post shows high-intent buying yet."      # negated: kept


def test_16_strength_claim_citing_a_qualified_row_is_kept(lg):
    out = _run(lg, [_entry(2, OWN[2], role="buyer", purchase_intent="explicit", product_fit="fits")],
               conclusion="Post 2 is a qualified lead with a stated budget.")
    assert out["conclusion"] == f"Post {_table_no(out, 2)} is a qualified lead with a stated budget."


# ── 4. post counts and category counts ─────────────────────────────────────

def test_17_post_count_must_be_a_server_count_even_when_a_7_appears_in_a_post_time(lg):
    posts = [_entry(n, OWN[n], role="buyer", purchase_intent="research", product_fit="fits") for n in (1, 4)]
    out = _run(lg, posts, hour="17:05", market_pattern="Small sample (7 posts) shows vendor activity.")
    assert out["market_pattern"] == ""


def test_18_numbers_are_compared_as_whole_numbers(lg):
    out = _run(lg, [_entry(2, OWN[2], role="buyer", purchase_intent="explicit", product_fit="fits")],
               market_pattern="Post 2 mentions a $20 budget.", conclusion="Post 2 mentions a $200 budget.")
    assert out["market_pattern"] == ""                     # "$20" is not "$200"
    assert out["conclusion"] == f"Post {_table_no(out, 2)} mentions a $200 budget."


def test_19_finding_count_is_the_posts_its_text_cites(lg):
    posts = [_entry(n, OWN[n], role="buyer", purchase_intent="research", product_fit="fits") for n in (1, 4)] + \
            [_entry(3, OWN[3], role="competitor_promo", purchase_intent="none", product_fit="partial")]
    out = _run(lg, posts, key_findings=[{"finding": "Teams asking for monitoring tools", "evidence_type": "Contextual",
                                         "signal_strength": "Moderate", "supporting_post_indices": [1, 3, 4],
                                         "impact": "Two posts (Posts 1 and 4) ask for monitoring tools."}])
    f = out["key_findings"][0]
    assert f["supporting_count"] == 2
    assert sorted(f["supporting_post_indices"]) == sorted([_table_no(out, 1), _table_no(out, 4)])


def test_20_categories_add_up_to_the_rows_and_summary_defines_qualified(lg):
    posts = [_entry(2, OWN[2], role="buyer", purchase_intent="explicit", product_fit="fits"),
             _entry(1, OWN[1], role="buyer", purchase_intent="research", product_fit="fits"),
             _entry(4, OWN[4], role="discussion", purchase_intent="none", product_fit="fits"),
             _entry(3, OWN[3], role="competitor_promo", purchase_intent="none", product_fit="partial"),
             _entry(5, "Asks which cast iron pan to buy.", role="irrelevant")]
    out = _run(lg, posts)
    c = out["counts"]
    assert c["categories"] == {"qualified": 1, "researching": 1, "unconfirmed": 0, "discussion": 1,
                               "competitor": 1, "unavailable": 0}
    assert sum(c["categories"].values()) == c["shown"] == len(_rows(out)) == c["listed"] - c["excluded"]
    assert out["verified_summary"] == (
        "Checked against the stored posts: 1 qualified lead, 1 buyer still researching, 1 general discussion post, "
        "1 competitor or vendor post; 1 of 5 listed posts excluded. " + ai.QUALIFIED_DEFINITION)


def test_22_budget_trial_and_timing_without_an_approval_stay_medium(lg, monkeypatch):
    """The same post 2 without its approval sentence: a budget amount, a trial and a timeline are signals, not a purchase
    step. The verified entry says "explicit"; the post's own text caps it, so there is no qualified lead."""
    _post_2_without_its_approval(monkeypatch)
    out = _run(lg, [_entry(2, OWN[2], role="buyer", purchase_intent="explicit", product_fit="fits")])
    row = _row(out, 2)
    assert row["analysis_identity"] == "title" and row["summary"] == OWN[2]
    assert (row["purchase_intent"], row["intent"], row["qualified"]) == ("research", "Medium", False)
    assert row["purchase_signals"] == {"budget": True, "timing": True, "evaluation": True, "willingness": False,
                                       "unconfirmed": False, "purchase_step": False}
    assert out["counts"]["qualified"] == 0 and "budget" in row["intent_reason"] and "no confirmed purchase step" in row["intent_reason"]
    assert out["verified_summary"].startswith("Checked against the stored posts: 0 qualified leads")


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_21_footer_uses_categories_and_keeps_the_old_line_for_saved_answers(name):
    src = (ROOT / "templates" / name).read_text(encoding="utf-8")
    start = src.index("function apSourcesTable(")
    block = src[start:src.index("container.appendChild(counts);", start)]
    assert "totals.categories" in block and "listed excluded" in block
    assert "excluded (of ${totals.retrieved} retrieved)" in block                 # answers saved before the patch
    assert re.search(r"if \(totals && totals\.categories.*?\} else if \(totals && typeof totals\.relevant", block, re.S)
