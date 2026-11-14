"""Fixes found while validating the lead MVP (each with its regression test): local-geography false demotions and recency that must keep ageing
after a lead is stored."""
from datetime import datetime, timedelta, timezone

import pytest

import business_profile as bp
import lead_engine as le
import lead_intent as li
from tests.test_lead_mvp import FORM, GOOD, Fake, web, lg, env  # noqa: F401

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def plumber(places="Leeds"):
    user, errors = bp.validate({"business_name": "Leeds Plumbing Co", "sells": "plumbing and boiler repair services", "ideal_customers": "homeowners",
                                "problems_solved": "leaking pipes, blocked drains", "geo_scope": "city", "geo_places": places})
    assert not errors
    return bp.build(user)


def lead(title, profile, body="", days=2):
    rec = le.normalize_post({"title": title, "post_text": body, "post_url": "https://www.reddit.com/r/t/comments/g1/x/", "platform": "reddit", "author": "u",
                             "published_at": (NOW - timedelta(days=days)).isoformat()}, NOW)
    return le.qualify(rec, profile, NOW)


# ── local geography ────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["Need a plumber in January for a leaking pipe", "I need a plumber, a leaking pipe flooded my kitchen in Easter week",
                                  "Need a plumber in Christmas week for a leaking pipe", "Need a plumber in Excel Road for a leaking pipe",
                                  "I need a plumber from Checkatrade for a leaking pipe", "Need a plumber at Easter, leaking pipe", "Need a plumber in Summer, leaking pipe"])
def test_calendar_words_holidays_streets_and_brands_are_not_places(text):
    sg = li.signature(plumber())
    assert li.other_place(text, sg) is None
    assert lead(text, plumber())["label"] != "Not a lead" or "place" not in " ".join(lead(text, plumber())["reasons"])


def test_a_clear_other_city_still_excludes_and_names_the_place():
    for text, place in (("Need a plumber in Manchester for a leaking pipe", "Manchester"), ("I need a plumber near Sydney Harbour, leaking pipe", "Sydney Harbour")):
        got = lead(text, plumber())
        assert got["label"] == "Not a lead" and got["geography"] == {"status": "mismatch", "place": place} and place in " ".join(got["reasons"])


def test_a_place_the_user_lists_is_never_a_mismatch():
    text = "Need a plumber in Headingley for a leaking pipe"
    assert lead(text, plumber("Leeds"))["label"] == "Not a lead"                        # documented limit: an unlisted suburb looks like another place
    assert lead(text, plumber("Leeds, Headingley"))["label"] == "High potential"          # the remedy: list the suburbs you serve


def test_the_profile_page_tells_a_local_business_to_list_its_suburbs(web):
    c, who, state, lr = web
    page = c.get("/leads/profile").text
    assert "suburbs" in page and "excluded" in page


# ── recency keeps ageing after a lead is stored ─────────────────────────────────────────────────────────────────────────────
def stored(label="High potential", recency="recent", days=5, published=True):
    return {"source_id": "reddit:a1", "label": label, "recency": recency, "is_current": recency == "recent", "reasons": ["why"],
            "published_at": (NOW - timedelta(days=days)).isoformat() if published else None}


def test_a_stored_recent_lead_becomes_historical_and_loses_its_high_rating_with_time():
    l = stored()
    assert le.refresh_lead(l, NOW)["label"] == "High potential"
    later = le.refresh_lead(l, NOW + timedelta(days=100))
    assert later["recency"] == "historical" and later["is_current"] is False and later["label"] == "Needs review"
    assert later["label_at_scan"] == "High potential" and later["recency_at_scan"] == "recent" and "now older than 90 days" in later["reasons"][0] and "why" in later["reasons"]
    assert l["label"] == "High potential" and l["recency"] == "recent"                      # the stored record itself is not modified


def test_relevant_is_demoted_too_and_needs_review_stays():
    assert le.refresh_lead(stored("Relevant"), NOW + timedelta(days=200))["label"] == "Needs review"
    kept = le.refresh_lead(stored("Needs review", "historical", days=200), NOW + timedelta(days=300))
    assert kept["label"] == "Needs review" and "label_at_scan" not in kept


def test_undated_and_invalid_leads_are_left_exactly_as_stored():
    for rec in ("undated", "invalid"):
        l = stored("Needs review", rec, published=False)
        assert le.refresh_lead(l, NOW + timedelta(days=500)) == l


def test_a_lead_never_becomes_more_recent_by_refreshing():
    old = stored("Needs review", "historical", days=300)
    assert le.refresh_lead(old, NOW)["recency"] == "historical"


def test_the_pages_show_the_aged_rating_and_the_counts_follow(web):
    from datetime import timedelta as td
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)
    loc = c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"]
    lead_id = lr._leads().list_leads("owner@x.com")[0]["lead_id"]
    assert "High potential" in c.get(f"/leads/item/{lead_id}").text and ">1<" in c.get("/leads").text.replace(" ", "").replace("\n", "")
    real_now = lr._now
    lr._now = lambda: real_now() + td(days=120)                                           # the same lead, reopened four months later
    try:
        detail = c.get(f"/leads/item/{lead_id}").text
        assert 'class="tag hp"' not in detail and "Needs review" in detail and "now older than 90 days" in detail
        scan_page = c.get(loc).text
        assert 'class="tag hp"' not in scan_page and 'class="tag hp"' not in c.get("/leads/my").text                  # no High potential badge on any card
        assert "0 of them current" in scan_page and "<b>0</b><span>High potential</span>" in scan_page
        assert lr._leads().counts("owner@x.com", lr._now())["high_potential_new"] == 0
    finally:
        lr._now = real_now
    assert lr._leads().list_leads("owner@x.com")[0]["label"] == "High potential"           # storage untouched: a re-scan can still rate it again
