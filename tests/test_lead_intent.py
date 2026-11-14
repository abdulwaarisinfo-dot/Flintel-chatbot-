"""lead_intent / the profile-aware qualification: the example sentences from the brief, the guards, the profile alignment, the local-business place
rule, non-English frames with profile terms, and regression floors on the evaluation sets (SYNTHETIC data; the floors are set below the measured
numbers so they catch a regression, they are not accuracy claims)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import business_profile as bp
import lead_engine as le
import lead_intent as li

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation"))
import lead_eval_data as data  # noqa: E402
import run_lead_eval as R  # noqa: E402

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def profile(name):
    user, errors = bp.validate(data.BUSINESSES[name])
    assert not errors
    return bp.build(user)


def lead_for(biz, title, body, days=3):
    rec = le.normalize_post({"title": title, "post_text": body, "post_url": "https://www.reddit.com/r/t/comments/zz9/x/", "platform": "reddit", "author": "u",
                             "published_at": (NOW - timedelta(days=days)).isoformat()}, NOW)
    return le.qualify(rec, profile(biz), NOW)


# ── the six examples from the brief, each for the business it belongs to ───────────────────────────────────────────────────
@pytest.mark.parametrize("biz,title,body,intent,label", [
    ("plumber", "I need a plumber in Leeds.", "", "SERVICE_REQUEST", "High potential"),
    ("shopify", "Our checkout conversion is terrible.", "", "STRONG_PROBLEM_SIGNAL", "Relevant"),
    ("shopify", "How much does a Shopify developer charge?", "", "SOLUTION_RESEARCH", "Relevant"),
    ("marketing", "Can anyone recommend a marketing agency?", "", "RECOMMENDATION_REQUEST", "High potential"),
    ("saas", "We're considering switching from our current applicant tracking system.", "", "SOLUTION_RESEARCH", "Relevant"),
])
def test_the_examples_in_the_brief_are_read_correctly(biz, title, body, intent, label):
    lead = lead_for(biz, title, body)
    assert (lead["intent"], lead["label"]) == (intent, label), lead["reasons"]
    assert lead["evidence"]["quote"] and lead["evidence"]["quote"] in (title + "\n" + body)


def test_a_question_with_no_domain_is_not_a_lead():
    lead = lead_for("saas", "What is the best alternative to our current software?", "")
    assert lead["label"] in ("Needs review", "Not a lead") and lead["label"] != "High potential"          # "software" alone says nothing about THIS business


def test_the_same_sentence_is_a_lead_for_one_business_and_not_for_another():
    t = "Can anyone recommend a good Shopify developer for our store?"
    assert lead_for("shopify", t, "")["label"] in ("High potential", "Relevant")
    for other in ("plumber", "skincare", "designer"):
        assert lead_for(other, t, "")["label"] in ("Not a lead", "Needs review") and lead_for(other, t, "")["label"] != "High potential", other


# ── what an author is DOING (frames) ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,frame", [
    ("I'm looking for someone to redo our website.", "REQUEST"), ("We need a new accountant.", "REQUEST"),
    ("Can anyone recommend a good dentist?", "RECOMMEND"), ("What do you use for invoicing?", "RECOMMEND"),
    ("How much does a logo cost?", "PRICE"), ("Is it worth paying for premium hosting?", "PRICE"),
    ("Notion vs Obsidian for a small team?", "COMPARE"), ("What are alternatives to Mailchimp?", "COMPARE"),
    ("We're switching from our current CRM.", "REPLACE"), ("Our usual plumber retired and we need a new one.", "REPLACE"),
    ("Our onboarding is a mess and we keep losing customers.", "PROBLEM"), ("I wish someone could just handle our books.", "WISH"),
])
def test_frames_recognise_what_the_author_is_doing(text, frame):
    assert frame in [f["frame"] for f in li.find_frames(text, "")]


@pytest.mark.parametrize("text", ["The council announced pipe replacement works across the city next month.", "A water company was fined for leaking pipes.",
                                  "Online retail sales climbed last quarter according to the report.", "Shopify reported quarterly revenue growth today."])
def test_a_third_person_report_has_no_frame(text):
    assert li.find_frames(text, "") == []


@pytest.mark.parametrize("title,body,guard", [
    ("We build Shopify stores", "We are a Shopify development agency. DM us for a free audit.", "vendor"),
    ("Show HN: X", "I built X, a tool for recruiters. Free trial.", "vendor"),
    ("What is Shopify?", "Can someone explain what Shopify is?", "definition"),
    ("Who designed this logo?", "Does anyone know who designed the logo of that club?", "definition"),
    ("Here's how I fixed it", "Step by step guide to fixing my own tap.", "tutorial"),
    ("Looking for a job", "I'm looking for a junior designer position. Resume in comments.", "job_seeker"),
    ("Would founders pay for this?", "Is anyone actually looking for this? Trying to validate my idea.", "validation"),
])
def test_guards_stop_advertising_definitions_tutorials_job_seekers_and_idea_validation(title, body, guard):
    assert li.guard_of(title, body) == guard


def test_a_question_about_price_is_not_swallowed_by_the_definition_guard():
    assert li.guard_of("What does a logo designer cost?", "") is None
    assert li.guard_of("Anyone available to fix our cart page?", "We need a developer this week.") is None          # a request, not "I'm available"


# ── alignment: the business decides ────────────────────────────────────────────────────────────────────────────────────────
def test_a_generic_topic_word_alone_is_only_a_partial_match_and_never_a_high_potential_lead():
    lead = lead_for("saas", "Looking for software to track my package deliveries", "")
    assert lead["fit"]["business_fit"] in ("partial", "none") and lead["label"] != "High potential"


def test_a_distinctive_identifier_or_two_content_words_make_a_strong_match():
    sg = li.signature(profile("shopify"))
    assert li.align("We need help with our Shopify store", sg)["business_fit"] == "strong"
    sg = li.signature(profile("plumber"))
    assert li.align("a plumber for a leaking pipe", sg)["business_fit"] == "strong"
    assert li.align("a plumber", sg)["business_fit"] == "strong"            # a service business: asking for the provider role is asking for the service
    assert li.align("something about a boiler", sg)["business_fit"] == "partial"
    assert li.align("something about a garden", sg)["business_fit"] == "none"


def test_word_families_match_across_forms():
    assert li.stem("plumbing") == li.stem("plumber") == li.stem("plumbers") and li.stem("developer") == li.stem("development") == li.stem("develop")
    assert li.stem("designs") == li.stem("designer") == li.stem("design")


def test_an_acronym_of_the_offer_is_an_identifier():
    sg = li.signature(profile("saas"))
    assert li.align("Need an ATS for 20 people", sg)["business_fit"] == "strong"


def test_generic_words_never_count_on_their_own_even_when_the_user_typed_them():
    sg = li.signature(profile("saas"))
    assert li.align("we keep hiring the wrong way and manage people badly", sg)["business_fit"] == "none"


# ── a local business ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_local_business_needs_its_place_and_rejects_another_one():
    assert lead_for("plumber", "Need a plumber in Leeds", "I need a plumber in Leeds for a leaking pipe.")["label"] == "High potential"
    other = lead_for("plumber", "Need a plumber in Sydney", "I need a plumber in Sydney for a leaking pipe under my sink.")
    assert other["label"] == "Not a lead" and "different place" in " ".join(other["reasons"])
    unknown = lead_for("plumber", "Need a plumber", "I need a plumber for a leaking pipe under my sink.")
    assert unknown["label"] == "Needs review" and unknown["geography"]["status"] == "unknown"


def test_a_non_local_business_ignores_places():
    assert lead_for("shopify", "Need a Shopify developer in Berlin", "I run a store in Berlin and need a Shopify developer.")["label"] in ("High potential", "Relevant")


# ── other languages: the frames exist, the PROFILE must carry terms in that language ──────────────────────────────────────
@pytest.mark.parametrize("biz,title,body", [
    ("plumber", "Necesito un fontanero en Leeds", "Necesito un fontanero en Leeds para una fuga de agua."),
    ("shopify", "Suche Shopify Entwickler", "Wir suchen einen Shopify Entwickler für unseren Online-Shop."),
    ("skincare", "Je cherche une crème pour peau sèche", "Je cherche une bonne crème pour ma peau sèche, des recommandations ?"),
])
def test_non_english_requests_are_found_when_the_profile_has_terms_in_that_language(biz, title, body):
    assert lead_for(biz, title, body)["label"] in ("High potential", "Relevant")


def test_non_english_without_matching_profile_terms_is_not_a_lead():
    assert lead_for("marketing", "Necesito un fontanero en Madrid", "Necesito un fontanero para una fuga de agua.")["label"] in ("Not a lead", "Needs review")


# ── time and evidence ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_an_old_post_with_a_perfect_match_is_historical_needs_review_never_current():
    lead = lead_for("shopify", "Need a Shopify developer", "I need a Shopify developer to customize our theme.", days=400)
    assert lead["label"] == "Needs review" and lead["is_current"] is False and lead["recency"] == "historical"


def test_every_surfaced_lead_in_the_dataset_has_an_exact_quote_from_its_own_post():
    for biz, spec in data.BUSINESSES.items():
        for it in [i for i in data.items() if i["biz"] == biz][:12]:
            lead = lead_for(biz, it["title"], it["body"], days=it["days"])
            if lead["label"] != "Not a lead":
                full = (lead["title"] + "\n" + lead["text"]).strip() if lead["text"] and lead["text"] != lead["title"] else lead["title"]
                assert lead["evidence"]["quote"] and lead["evidence"]["quote"] in full and lead["url"]


# ── regression floors on the evaluation sets (set below the measured numbers) ───────────────────────────────────────────────
@pytest.mark.parametrize("split,min_recall,min_precision", [("dev", 0.85, 0.95), ("held", 0.85, 0.95), ("fresh", 0.80, 0.95), ("baseline", 0.90, 0.95)])
def test_evaluation_floors(split, min_recall, min_precision):
    m = R.metrics(R.evaluate(le, split))
    assert m["recall"] >= min_recall and m["precision"] >= min_precision, m
    assert m["none_surfaced"] == 0 and m["review_overconfident"] <= 1


def test_every_kind_of_negative_stays_out_of_the_surfaced_set():
    rows = R.evaluate(le)
    assert not [r["id"] for r in rows if r["expect"] == "none" and r["label"] in R.SURFACED]
    assert not [r["id"] for r in rows if r["kind"] in ("vendor_promo", "definition", "news", "tutorial", "chat", "job_seeker", "validation_probe", "offering_services") and r["label"]]


def test_off_domain_demand_posts_are_almost_never_surfaced():
    import glob
    import json
    posts = [json.loads(l) for f in sorted(glob.glob(str(Path(__file__).resolve().parent.parent / "evaluation" / "classification_eval_v*.jsonl"))) for l in open(f, encoding="utf-8") if l.strip()]
    recs = [{"title": p["title"], "post_text": p["text"], "post_url": f"https://www.reddit.com/r/t/comments/n{i}x/x/", "platform": "reddit", "author": f"u{i}",
             "published_at": (NOW - timedelta(days=3)).isoformat()} for i, p in enumerate(posts)]
    surfaced = 0
    for biz in data.BUSINESSES:
        s = le.run_scan(profile(biz), [R._Conn(recs)], now=NOW)
        surfaced += sum(1 for l in s["leads"] if l["label"] in R.SURFACED)
    assert surfaced / (len(recs) * len(data.BUSINESSES)) < 0.01, surfaced               # measured 4 / 1470; v1 was 49 / 1470
