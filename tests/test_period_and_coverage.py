"""Issue 1 (date range) and Issue 5 (search coverage): an explicit period is searched as asked and the report states requested vs applied.
Synthetic messages; no network. The model router is not exercised (no key)."""
from datetime import datetime, timedelta, timezone

import pytest

import active_intent
import hackernews_live as hn
import timerange as tr
from tests.test_mapreduce_identity import env, lg  # noqa: F401

REF = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _days(r):
    return round((r.end - r.start).total_seconds() / 86400.0)


# ── Issue 1: what the parser understands ────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,days", [
    ("Find people looking for AI development and automation services from the last 30 days.", 30),
    ("in the past 30 days", 30), ("over the previous 30 days", 30), ("last 30 days", 30), ("the last thirty days", 30),
    ("past month", 30), ("the past 30-day window", 30), ("a 30-day period", 30), ("last 30-days", 30), ("last 30d", 30),
    ("last 3 months", 90), ("past 6 months", 180), ("past year", 365), ("last 2 weeks", 14), ("last 24 hours", 1),
])
def test_an_explicit_lookback_becomes_that_many_days(text, days):
    r = tr.parse_time_range(text, REF, "UTC")
    assert isinstance(r, tr.TimeRange) and _days(r) == days, (text, r)
    assert r.end == REF and r.start == REF - timedelta(days=days)


def test_a_long_lead_discovery_prompt_with_a_period_in_it_keeps_the_period():
    prompt = ("I run an AI development and automation agency. Find people who are actively looking for AI agents, automation or custom AI development. "
              "Prioritise recent, relevant conversations, newest first, and exclude promotions. Search only the last 30 days. Today I want a short list.")
    r = tr.parse_time_range(prompt, REF, "UTC")
    assert isinstance(r, tr.TimeRange) and _days(r) == 30


def test_when_several_periods_are_mentioned_the_longest_wins_and_the_note_says_so():
    r = tr.parse_time_range("Show what happened in the last 24 hours and also the last 30 days", REF, "UTC")
    assert _days(r) == 30 and "Several periods" in r.note
    r2 = tr.parse_time_range("the last 30 days, with the last 24 hours first", REF, "UTC")
    assert _days(r2) == 30                                        # order in the sentence does not matter: never silently the shortest


def test_calendar_month_wording_keeps_its_documented_meaning():
    assert tr.parse_time_range("last month", REF, "UTC").kind == "month"        # previous calendar month (unchanged)
    assert tr.parse_time_range("recent posts", REF, "UTC") is None              # a bare recency word is a default elsewhere, not a range here


@pytest.mark.parametrize("text,expected", [("from the last 30 days", True), ("the past month", True), ("a 30-day window", True), ("yesterday", True),
                                           ("since Monday", True), ("Find people looking for a CRM", False), ("AI agents for sales", False)])
def test_mentions_period_sees_a_stated_period_even_when_it_cannot_be_parsed(text, expected):
    assert tr.mentions_period(text) is expected


# ── Issue 1: the window reaches the retrieval layers unchanged ────────────────────────────────────────────────────────────────────────────
def test_a_30_day_range_reaches_the_live_hacker_news_fetch_as_a_30_day_window():
    r = tr.parse_time_range("last 30 days", REF, "UTC")
    start, end = hn.window_bounds(None, r, REF, 3650)
    assert (end - start).days == 30
    url = hn.build_url("ai automation", start=start, end=end, by_date=True)
    lo = int(start.timestamp()); hi = int(end.timestamp())
    assert f"created_at_i%3E%3D{lo}" in url and f"created_at_i%3C{hi}" in url and (hi - lo) // 86400 == 30


def test_the_router_period_in_days_is_a_window_of_that_many_days():
    start, end = hn.window_bounds(30, None, REF, 3650)
    assert (end - start).days == 30 + 0 or round((end - start).total_seconds() / 86400) == 30


def test_explicit_range_beats_router_days_and_the_default_recency_window():
    r = tr.parse_time_range("last 30 days", REF, "UTC")
    assert tr.effective_lookback("find latest leads in the last 30 days", None, 1, r, None)[2] == "explicit_range"
    days, rng, src = tr.effective_lookback("find latest leads", None, None, None, None, default_days=30)
    assert (days, src) == (30, "default_recent")


# ── Issue 1: the automatic Live-sized window must never replace a period the user wrote ───────────────────────────────────────────────
def test_active_intent_window_does_not_replace_a_period_the_user_stated_but_the_parser_missed():
    win = active_intent.recent_window(REF, 1500)
    msg = {"query": "Find people looking for AI automation over the course of the past couple of weeks", "time_range": None, "time_window_days": None}
    assert tr.mentions_period(msg["query"]) is True
    assert active_intent.historical_window_applies(msg, win) is False


def test_active_intent_window_still_applies_when_the_user_gave_no_period_at_all():
    win = active_intent.recent_window(REF, 1500)
    assert active_intent.historical_window_applies({"query": "Find people looking for AI automation", "time_range": None, "time_window_days": None}, win) is True
    assert active_intent.historical_window_applies({"query": "x", "time_range": {"start": "2026-10-09T12:00:00+00:00", "end": "2026-11-08T12:00:00+00:00"}}, win) is False


def test_the_active_intent_window_kind_constant_matches_the_one_logics_uses(lg):
    assert lg._ACTIVE_INTENT_WINDOW_KIND == active_intent.WINDOW_KIND


# ── Issue 5: the coverage block ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _msg(query, days=None, source=None, rng=None, platform="all"):
    m = {"query": query, "requested_at": REF, "topic_key": "t1", "targeting_platform": platform}
    if rng is not None:
        m["time_range"] = rng.to_doc(REF)
    if days is not None:
        m["time_window_days"] = days
    if source:
        m["time_window_source"] = source
    return m


@pytest.fixture
def hn_on(lg, monkeypatch):
    import platforms
    platforms.apply_flags(hackernews=True)
    yield
    platforms.apply_flags(hackernews=False)


def _answer(shown=1, excluded=0, relevant=1, **extra):
    return dict({"format": "source_list", "counts": {"retrieved": 5, "shown": shown, "excluded": excluded, "relevant": relevant,
                                                      "excluded_reasons": {"duplicate": 0, "irrelevant": excluded, "unverifiable": 0}},
                 "reconciliation": {"not_listed": 0, "listed_balanced": True, "pool_balanced": True}}, **extra)


def test_coverage_states_requested_and_applied_period_when_they_agree(lg, hn_on):
    rng = tr.parse_time_range("last 30 days", REF, "UTC")
    cov = lg.build_search_coverage(_msg("find AI buyers in the last 30 days", rng=rng), _answer(), {"mode": "combined", "live": "ok", "historical": "ok", "stats": {}}, [{}] * 5, "c1")
    assert cov["requested_period"]["days"] == 30 and cov["applied_period"]["days"] == 30 and cov["applied_period"]["origin"] == "requested"
    assert cov["period_ok"] is True and cov["period_note"] is None and cov["mode"] == "combined" and cov["retrieval"] == "complete"


def test_coverage_flags_a_requested_period_that_was_not_applied(lg, hn_on):
    """The failure that was reported: 30 days requested, ~25 hours searched. It is now stated, never silent."""
    win = active_intent.recent_window(REF, 1500)
    msg = _msg("find AI buyers in the last 30 days", rng=win)                    # the automatic window was the one applied
    cov = lg.build_search_coverage(msg, _answer(), {"mode": "combined", "live": "ok", "historical": "ok", "stats": {}}, [{}] * 5, "c1")
    assert cov["period_ok"] is False and "NOT fully applied" in cov["period_note"]
    assert cov["applied_period"]["origin"] == "automatic_recent_window" and cov["applied_period"]["days"] < 2
    assert cov["retrieval"] == "partial"


def test_an_unparsed_stated_period_is_reported_not_silently_replaced(lg, hn_on):
    cov = lg.build_search_coverage(_msg("find AI buyers from the past couple of weeks"), _answer(), {"mode": "historical", "live": "not_requested", "historical": "ok", "stats": {}}, [{}], "c1")
    assert cov["requested_period"]["interpreted"] is False and cov["period_ok"] is False and cov["applied_period"] is None


def test_an_automatic_default_window_is_labelled_as_such(lg, hn_on):
    cov = lg.build_search_coverage(_msg("find the latest AI buyers", days=30, source="default_recent"), _answer(), {"mode": "historical", "live": "not_requested", "historical": "ok", "stats": {}}, [{}], "c1")
    assert cov["applied_period"]["origin"] == "automatic_default" and cov["requested_period"] is None and cov["period_ok"] is True


@pytest.mark.parametrize("mode,live,hist,hn_state,reddit_state", [
    ("combined", "ok", "ok", "searched", "searched"),
    ("live", "ok", "not_requested", "searched", "not_searched"),
    ("historical", "not_requested", "ok", "not_searched", "searched"),
    ("combined", "failed", "ok", "failed", "searched"),
    ("combined", "ok", "failed", "searched", "failed"),
    ("combined", "unavailable", "ok", "unavailable", "searched"),
    ("combined", "rate_limited", "ok", "unavailable", "searched"),
    ("combined", "ok", "unavailable", "searched", "unavailable"),
    ("combined", "weird", "ok", "unconfirmed", "searched"),
    ("combined", "partial", "ok", "partial", "searched"),
    ("combined", "ok", "partial", "searched", "partial"),
])
def test_each_source_is_searched_unavailable_or_not_searched_exactly_as_it_happened(lg, hn_on, mode, live, hist, hn_state, reddit_state):
    cov = lg.build_search_coverage(_msg("q"), _answer(), {"mode": mode, "live": live, "historical": hist, "stats": {}}, [{}], "c1")
    by = {s["key"]: s for s in cov["sources"]}
    assert by["hackernews"]["state"] == hn_state and by["reddit"]["state"] == reddit_state
    if {hn_state, reddit_state} & {"unavailable", "failed", "unconfirmed", "partial"}:
        assert cov["retrieval"] == "partial" and cov["partial_reasons"]
    assert "Reddit" in by["reddit"]["label"] and (by["reddit"]["via"], by["hackernews"]["via"]) == ("stored", "live")


def test_a_source_is_never_called_searched_when_its_path_did_not_run(lg, hn_on):
    cov = lg.build_search_coverage(_msg("q"), _answer(), {"mode": "historical", "live": "not_requested", "historical": "ok", "stats": {}}, [{}], "c1")
    hn_row = next(s for s in cov["sources"] if s["key"] == "hackernews")
    assert hn_row["state"] == "not_searched" and "Historical" in hn_row["detail"]


def test_coverage_counts_come_from_the_same_records_as_the_report(lg, hn_on):
    ans = _answer(shown=2, excluded=3, relevant=2)
    ans["counts"]["excluded_reasons"]["duplicate"] = 1
    cov = lg.build_search_coverage(_msg("q"), ans, {"mode": "combined", "live": "ok", "historical": "ok", "stats": {"duplicates_removed": 4}}, [{}] * 9, "c1")
    assert cov["counts"] == {"retrieved": 9, "duplicates": 5, "excluded": 3, "relevant": 2, "displayed": 2, "not_listed": 0}


def test_failed_analysis_batches_and_unbalanced_counts_make_retrieval_partial(lg, hn_on):
    ans = _answer(analysis_coverage={"batches": 5, "failed_batches": 2, "posts": 60})
    cov = lg.build_search_coverage(_msg("q"), ans, {"mode": "historical", "live": "not_requested", "historical": "ok", "stats": {}}, [{}], "c1")
    assert cov["retrieval"] == "partial" and any("2 of 5" in r for r in cov["partial_reasons"])
    bad = _answer(); bad["reconciliation"]["pool_balanced"] = False
    assert "did not fully reconcile" in " ".join(lg.build_search_coverage(_msg("q"), bad, {"mode": "historical", "historical": "ok", "stats": {}}, [{}], "c1")["partial_reasons"])


def test_enrich_search_coverage_adds_the_block_once_and_leaves_other_formats_alone(lg, hn_on):
    import json
    out = json.loads(lg.enrich_search_coverage(json.dumps(_answer()), _msg("q"), {"mode": "combined", "live": "ok", "historical": "ok", "stats": {}}, [{}], "c1"))
    assert "search_coverage" in out
    again = lg.enrich_search_coverage(json.dumps(out), _msg("q"), {"mode": "live"}, [{}], "c1")
    assert json.loads(again)["search_coverage"]["mode"] == "combined"             # not overwritten
    na = json.dumps({"format": "not_available", "message": "x"})
    assert lg.enrich_search_coverage(na, _msg("q"), {"mode": "combined"}, [], "c1") == na
    assert lg.enrich_search_coverage("not json", _msg("q"), {"mode": "combined"}, [], "c1") == "not json"


# ── Issue 1 end to end through the real /search route (router simulated; no network) ───────────────────────────────────────────────────
from datetime import timedelta as _D

from tests.test_recency_phase2 import _search  # noqa: E402
from tests.test_time_ranges import app_env, fixed_clock  # noqa: E402,F401


@pytest.mark.parametrize("query,days", [
    ("Find people looking for AI development and automation services from the last 30 days.", 30),
    ("Find AI automation buyers over the past month", 30),
    ("Find AI automation buyers; the last 24 hours matter least, search the last 30 days", 30),
    ("Look for demand for AI agents in the last 3 months", 90),
])
def test_the_route_searches_exactly_the_period_the_user_asked_for(fixed_clock, monkeypatch, query, days):
    msg = _search(fixed_clock, monkeypatch, query)
    rng = msg["time_range"]
    assert rng["kind"] == "rolling" and rng["end"] - rng["start"] == _D(days=days)
    assert msg.get("time_window_days") is None                              # the explicit range, not a coarser number, is what is stored


def test_a_router_that_returns_one_day_cannot_shrink_an_explicit_30_day_request(fixed_clock, monkeypatch):
    msg = _search(fixed_clock, monkeypatch, "Find people looking for AI automation from the last 30 days", time_window_days=1)
    assert msg["time_range"]["end"] - msg["time_range"]["start"] == _D(days=30)


def test_the_router_period_is_used_when_the_wording_is_not_parseable_and_is_never_replaced_by_a_default(fixed_clock, monkeypatch):
    msg = _search(fixed_clock, monkeypatch, "Find people looking for AI automation over the past couple of weeks", time_window_days=14)
    assert msg["time_window_days"] == 14 and not msg.get("time_range")
    assert msg.get("time_window_source") == "explicit_period"


# ── one explicit "last 30 days" request traced through every stage: the SAME interval everywhere ───────────────────────────────────────────
def test_trace_last_30_days_parse_route_scope_live_window_stored_filter_and_report(fixed_clock, monkeypatch, lg):
    """Stages, all synthetic: (1) parser, (2) the real /search route (router simulated) and the stored message, (3) the time scope the integrity layer
    and the answer use, (4) the live Hacker News window and request URL, (5) the stored-post date filter, (6) the coverage block in the report."""
    import platforms
    from tests.test_recency_phase2 import _search as route_search
    flc, idx, rt, chats, state = fixed_clock
    query = "Find people looking for AI development and automation services from the last 30 days."
    # (2) route -> stored message
    msg = route_search(fixed_clock, monkeypatch, query, time_window_days=1)         # a router that wrongly says 1 day
    doc = msg["time_range"]
    start, end = tr.to_utc(doc["start"]), tr.to_utc(doc["end"])
    assert end - start == timedelta(days=30) and doc["kind"] == "rolling" and "30 days" in doc["phrase"]
    # (1) the parser, given the same text and the same reference instant, gives the same interval
    parsed = tr.parse_time_range(query, end, "UTC")
    assert (parsed.start, parsed.end) == (start, end)
    # (3) the scope used by the integrity layer / answer text
    scope = lg.build_time_scope(msg.get("time_window_days"), doc, msg.get("requested_at"))
    assert tr.to_utc(scope["start"]) == start and tr.to_utc(scope["end"]) == end
    # (4) the live Hacker News window and the request that would be sent
    hs, he = hn.window_bounds(msg.get("time_window_days"), doc, None, 3650)
    assert (hs, he) == (start, end)
    url = hn.build_url("ai automation", start=hs, end=he, by_date=True)
    assert f"created_at_i%3E%3D{int(start.timestamp())}" in url and f"created_at_i%3C{int(end.timestamp())}" in url
    # (5) the stored-post filter keeps day 29, drops day 31 and a future-dated post
    posts = [{"post_url": "u1", "platform": "reddit", "published_at": (end - timedelta(days=29)).isoformat()},
             {"post_url": "u2", "platform": "reddit", "published_at": (end - timedelta(days=31)).isoformat()},
             {"post_url": "u3", "platform": "reddit", "published_at": (end + timedelta(days=3)).isoformat()}]
    kept = tr.filter_posts(posts, tr.range_from_doc(doc), now=end)
    kept = kept[0] if isinstance(kept, tuple) else kept
    assert [p["post_url"] for p in kept] == ["u1"]
    # (6) the report states the same interval, as requested, and says nothing is wrong
    platforms.apply_flags(hackernews=True)
    try:
        cov = lg.build_search_coverage(dict(msg, query=query), {"format": "source_list", "counts": {}}, {"mode": "combined", "live": "ok", "historical": "ok", "stats": {}}, [], "c1")
    finally:
        platforms.apply_flags(hackernews=False)
    assert tr.to_utc(cov["applied_period"]["from"]) == start and tr.to_utc(cov["applied_period"]["to"]) == end and cov["applied_period"]["origin"] == "requested"
    assert cov["requested_period"]["days"] == 30.0 and cov["period_ok"] is True


def test_a_skipped_source_or_cut_off_scan_never_yields_complete_coverage(lg, hn_on):
    skipped = lg.build_search_coverage(_msg("q"), _answer(), {"mode": "historical", "live": "not_requested", "historical": "ok", "stats": {}}, [{}], "c1")
    assert skipped["retrieval"] == "partial" and any("not searched" in r for r in skipped["partial_reasons"])
    cut = lg.build_search_coverage(_msg("q"), _answer(), {"mode": "combined", "live": "ok", "historical": "ok", "stats": {}}, [{}], "c1", True)
    assert {s["key"]: s["state"] for s in cut["sources"]}["reddit"] == "partial" and cut["retrieval"] == "partial"
    full = lg.build_search_coverage(_msg("q"), _answer(), {"mode": "combined", "live": "ok", "historical": "ok", "stats": {}}, [{}], "c1")
    assert full["retrieval"] == "complete"
    none = lg.build_search_coverage(_msg("q"), _answer(), {"mode": "combined", "live": "failed", "historical": "failed", "stats": {}}, [], "c1")
    assert none["retrieval"] == "unavailable"
