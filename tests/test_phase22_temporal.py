"""
tests/test_phase22_temporal.py
==============================
PHASE 2.2 — evidence integrity.

  1  trend_report      the model's trend figures are never presented as results; dates in two weeks are not a trend
  2  boundaries        timestamp validation and the half-open retrieval window agree at every instant (fixed clock)
  3  time scope        every structured format carries the effective window; an unbounded search is distinguishable from a
                       bounded search with no evidence
  4  grammar           removing an unsupported temporal marker leaves a sensible sentence

Synthetic records only; fixed clock where an instant matters; no network.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import answer_integrity as ai  # noqa: E402
import platforms  # noqa: E402
import timerange  # noqa: E402
from tests.test_recency_phase2 import CRM_Q, _answer, _mixed, _run, _scope, _sig  # noqa: E402
from tests.test_strict_intent import lg  # noqa: E402,F401
from tests.test_time_ranges import app_env, env  # noqa: E402,F401

UTC = timezone.utc
D = timedelta
NOW = datetime.now(UTC)
TOL = timerange.FUTURE_TOLERANCE
US = timedelta(microseconds=1)


def _patch(lg, data, sigs, scope=None):
    with lg.time_scope_context(scope):
        return json.loads(lg._patch_post_urls_into_answer(json.dumps(data), sigs, query=CRM_Q))


# ═══ 1. trend_report ═══════════════════════════════════════════════════════════════════════════════════════════════
def _trend(**over):
    d = {"format": "trend_report", "topic": "CRM", "window": "Last 30 days", "platforms": "reddit",
         "shift_table": {"headers": ["Metric", "Start", "End", "Change"], "rows": [["Positive mentions", "20%", "85%", "+65 pts"]]},
         "weekly_table": {"headers": ["Week", "Positive"], "rows": [["Week 3 (Mar 3-9)", "99%"], ["Week 4 (Mar 10-16)", "12%"]]},
         "interpretation": "Sentiment improved dramatically. Demand surged. Buyers asked about pricing.",
         "trend": "Demand will keep rising.", "conclusion": "Adoption is accelerating. Teams compare vendors on price.",
         "positive_drivers": [{"platform": "reddit", "post": "Growing excitement about the CRM", "sentiment": "positive", "theme": "x"}],
         "negative_drivers": [{"platform": "reddit", "post": "Pricing is confusing", "sentiment": "negative", "theme": "y"}],
         "followup_question": "Which vendors do you compare?"}
    d.update(over)
    return d


FIGURES = ("shift_table", "weekly_table", "trend")
CLAIMS = ("dramatically", "surged", "keep rising", "accelerating", "+65", "99%", "Week 3")


def _no_model_figures(out):
    assert all(k not in out for k in FIGURES)
    text = json.dumps({k: v for k, v in out.items() if k not in ("time_scope", "evidence_summary", "evidence_note")})
    for claim in CLAIMS:
        assert claim not in text, claim
    assert out["trend_check"] == {"comparative_evidence": False, "reason": "no_server_side_trend_calculation",
                                  "dated_posts": out["evidence_summary"]["dated"], "figures_verified": False}


@pytest.mark.parametrize("scope", [_scope(30), None], ids=["30-day window", "unbounded"])
def test_two_posts_in_different_weeks_do_not_make_a_trend(lg, scope):
    sigs = [_sig(1, "a", NOW - D(days=2)), _sig(2, "b", NOW - D(days=16))]
    out = _patch(lg, _trend(), sigs, scope)
    _no_model_figures(out)
    assert out["evidence_summary"]["posts"] == 2 and out["evidence_summary"]["dated"] == 2


def test_two_posts_180_days_apart_in_an_unbounded_search_do_not_make_a_trend(lg):
    sigs = [_sig(1, "a", NOW - D(days=2)), _sig(2, "b", NOW - D(days=182))]
    out = _patch(lg, _trend(), sigs, None)
    _no_model_figures(out)
    assert out["window"] == "All collected posts (no time limit)" and out["time_scope"]["window"] is None
    assert out["evidence_summary"]["earliest"] < out["evidence_summary"]["latest"]


def test_no_amount_of_dated_evidence_turns_model_figures_into_results(lg):
    sigs = [_sig(i, f"p{i}", NOW - D(days=7 * i)) for i in range(1, 13)]          # twelve posts, twelve different weeks
    _no_model_figures(_patch(lg, _trend(), sigs, None))                           # there is no server-side trend calculation


def test_fabricated_weekly_rows_and_forecasts_are_removed_even_when_nothing_else_is(lg):
    data = {"format": "trend_report", "topic": "CRM", "weekly_table": {"headers": ["Week"], "rows": [["Week 1"]]},
            "trend": "Demand will keep rising.", "shift_table": {"headers": [], "rows": []}}
    out = _patch(lg, data, [_sig(1, "a", NOW - D(days=1))], _scope(30))
    assert all(k not in out for k in FIGURES) and out["topic"] == "CRM"


@pytest.mark.parametrize("sigs,scope,expect", [
    ([], None, "No retrieved posts support a period comparison."),
    ([], _scope(30), "No retrieved posts support a period comparison for the last 30 days."),
    ([_sig(1, "a", None)], None, "1 without a valid publication date"),
    ([_sig(1, "a", NOW - D(days=400))], _scope(30), "No retrieved posts support a period comparison for the last 30 days."),
], ids=["empty", "empty, bounded", "undated only", "old only, bounded"])
def test_insufficient_or_empty_evidence_is_described_honestly(lg, sigs, scope, expect):
    out = _patch(lg, _trend(), sigs, scope)
    _no_model_figures(out)
    assert expect in out["evidence_note"] and "does not compute sentiment or volume changes" in out["evidence_note"]


def test_the_evidence_summary_is_computed_from_validated_dates_only(lg):
    sigs = [_sig(1, "a", NOW - D(days=2)), _sig(2, "b", None), _sig(3, "c", NOW + D(days=30)), _sig(4, "d", NOW - D(days=40))]
    s = _patch(lg, _trend(), sigs, None)["evidence_summary"]
    assert (s["posts"], s["dated"], s["undated"]) == (4, 2, 2)                    # the future date is unknown, not "newest"
    assert s["earliest"] == (NOW - D(days=40)).isoformat() and s["latest"] == (NOW - D(days=2)).isoformat()
    note = _patch(lg, _trend(), sigs, None)["evidence_note"]
    assert "2 without a valid publication date" in note and "Based on 4 retrieved posts" in note


def test_descriptive_narrative_and_post_level_content_survive(lg):
    data = _trend(interpretation="Several buyers asked about pricing. Sentiment improved dramatically. There is no evidence of a trend.")
    out = _patch(lg, data, [_sig(1, "a", NOW - D(days=2))], None)
    assert out["interpretation"] == "Several buyers asked about pricing. There is no evidence of a trend."
    assert out["conclusion"] == "Teams compare vendors on price."
    assert out["positive_drivers"] == data["positive_drivers"] and out["negative_drivers"] == data["negative_drivers"]
    assert out["followup_question"] == "Which vendors do you compare?" and out["topic"] == "CRM"


@pytest.mark.parametrize("bad", [
    {"format": "trend_report"},
    {"format": "trend_report", "shift_table": None, "weekly_table": 5, "interpretation": ["a", {"b": "Demand surged."}], "positive_drivers": "x"},
    {"format": "trend_report", "interpretation": None, "trend": 3, "conclusion": ["Demand will keep rising."], "window": 7},
    {"format": "trend_report", "shift_table": {"rows": "oops"}, "weekly_table": [], "negative_drivers": [None, 3]},
    {"format": "trend_report", "interpretation": {"nested": {"deep": "Adoption is accelerating."}}},
])
def test_malformed_trend_payloads_never_raise_and_never_keep_figures(lg, bad):
    out = ai.enforce_temporal_claims(json.loads(json.dumps(bad)), [{"published_at": 5}, {"created_utc": "zzz"}, None, _sig(1, "a", NOW)], _scope(7))
    assert all(k not in out for k in FIGURES) and out["trend_check"]["figures_verified"] is False
    assert "accelerating" not in json.dumps(out) and "surged" not in json.dumps(out)


def test_the_trend_ui_shows_the_server_note_and_period_not_tables(integrity_browser):
    from tests.test_recency_phase2 import _open
    sigs = [_sig(1, "a", NOW - D(days=2)), _sig(2, "b", NOW - D(days=16))]
    with_scope = _trend()
    import logics as _lg  # noqa: F401  (loaded by the lg fixture elsewhere; the patch is called through the module below)
    for name in ("chat.html", "index.html"):
        out = ai.enforce_temporal_claims(json.loads(json.dumps(with_scope)), sigs, _scope(30))
        page, ctx, errors = _open(integrity_browser, name, out)
        try:
            text = page.inner_text(".answer-rendered")
            assert "Showing posts from the last 30 days." in text and "Based on 2 retrieved posts" in text
            assert "Overall Sentiment Shift" not in text and "Weekly Sentiment Breakdown" not in text
            assert "Interpretation" in text and "Buyers asked about pricing." in text and errors == []
        finally:
            ctx.close()


# ═══ 2. timestamp boundaries (fixed clock) ═════════════════════════════════════════════════════════════════════════
N = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
UPPER = N + TOL                                    # exclusive: nothing at or after this instant is a valid publication time
FLOOR = platforms.DEFAULT_EARLIEST_DATE


@pytest.mark.parametrize("delta,valid", [(-D(seconds=1), True), (-US, True), (D(0), False), (US, False), (D(seconds=1), False)])
def test_the_future_tolerance_limit_is_exclusive(delta, valid):
    assert (timerange.publication_time(UPPER + delta, now=N) is not None) is valid


@pytest.mark.parametrize("delta,valid", [(-US, False), (D(0), True), (US, True)])
def test_the_earliest_valid_date_is_inclusive(delta, valid):
    assert (timerange.publication_time(FLOOR + delta, now=N) is not None) is valid
    assert (timerange.publication_time(FLOOR + delta, now=N, platform="reddit") is not None) is valid


def _window(days=7):
    return N - D(days=days), N + TOL


def _post(when):
    return {"post_url": "https://www.reddit.com/r/x/comments/abc/t/", "platform": "reddit", "created_utc": when}


@pytest.mark.parametrize("delta,inside", [(-US, False), (D(0), True), (US, True)])
def test_the_window_lower_bound_is_inclusive(lg, delta, inside):
    start, end = lg._since_days_window(7, now=N)
    assert (start, end) == (N - D(days=7), UPPER)
    assert lg._in_window(_post(start + delta), (start, end), now=N) is inside


@pytest.mark.parametrize("delta,inside", [(-D(seconds=1), True), (-US, True), (D(0), False), (US, False), (D(seconds=1), False)])
def test_a_post_exactly_at_the_exclusive_upper_bound_cannot_qualify(lg, delta, inside):
    window = lg._since_days_window(7, now=N)
    assert lg._in_window(_post(window[1] + delta), window, now=N) is inside


def test_the_window_upper_bound_works_independently_of_the_tolerance(lg):
    # a window that ends BEFORE now + tolerance: the post is a valid publication time but outside the window. This is the
    # case the (formerly "equivalent") upper-bound mutation changes; it is not redundant with the validation.
    window = (N - D(days=1), N - D(hours=1))
    assert timerange.publication_time(N - D(minutes=30), now=N) is not None
    assert lg._in_window(_post(N - D(minutes=30)), window, now=N) is False
    assert lg._in_window(_post(N - D(hours=1) - US), window, now=N) is True
    assert lg._in_window(_post(N - D(hours=1)), window, now=N) is False


def test_validation_and_window_use_one_clock_reading(lg):
    window = lg._since_days_window(7, now=N)
    # the default 'now' of _in_window is the window's own clock, so a later wall clock cannot move the boundary
    assert lg._in_window(_post(UPPER - US), window) is True and lg._in_window(_post(UPPER), window) is False


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("not a date", None), (0, None), ("1970-01-01T00:00:00+00:00", None), ("2005-05-31T23:59:59+00:00", None),
    ((N - D(hours=1)).replace(tzinfo=None), N - D(hours=1)),                                         # naive = UTC
    ((N - D(hours=1)).astimezone(timezone(D(hours=5))), N - D(hours=1)),                             # aware, other zone
    ((N - D(hours=1)).isoformat().replace("+00:00", "Z"), N - D(hours=1)),                           # ISO with Z
    ((N - D(hours=1)).isoformat(), N - D(hours=1)),
])
def test_missing_invalid_naive_and_aware_timestamps(value, expected):
    got = timerange.publication_time(value, now=N)
    assert got == expected and (got is None or got.tzinfo is not None)


def test_an_explicit_calendar_range_keeps_its_half_open_semantics_on_one_clock():
    rng = timerange.TimeRange(start=N - D(days=10), end=N - D(days=5), tz_name="UTC", kind="range", phrase="x", capped=False, note="")
    posts = [dict(_post(t), title=name) for name, t in [("before", rng.start - US), ("start", rng.start), ("inside", rng.start + D(days=1)),
                                                        ("last", rng.end - US), ("end", rng.end), ("none", None)]]
    kept, stats = timerange.filter_posts(posts, rng, now=N)
    assert [p["title"] for p in kept] == ["start", "inside", "last"]
    assert stats == {"kept": 3, "outside": 2, "no_timestamp": 1}


def test_the_prototype_ranker_uses_the_same_exclusive_limit():
    from intent_prototype import opportunity
    assert opportunity._PUBLICATION_FLOOR == platforms.DEFAULT_EARLIEST_DATE == timerange.PUBLICATION_FLOOR     # drift guard
    cfg = {"half_life_days": 30, "floor": 0.0, "missing_date_score": 0.35}
    assert opportunity.recency_score(datetime.now(UTC) + TOL + D(seconds=5), cfg) == 0.35                   # beyond the limit: unknown


# ═══ 3. the effective time scope is carried by every structured format ═══════════════════════════════════════════════
@pytest.mark.parametrize("fmt", ["no_results", "not_available", "disallowed", "trend_report"])
@pytest.mark.parametrize("days", [7, None])
def test_each_format_carries_the_effective_window(lg, fmt, days):
    req = datetime.now(UTC)
    scope = lg.build_time_scope(days, None, req)
    data = {"format": fmt, "message": "Nothing to show."}
    out = _patch(lg, data, [], scope)
    if days:
        assert out["time_scope"]["window"]["label"] == f"last {days} days"
        assert timerange.to_utc(out["time_scope"]["window"]["end"]) == req + TOL
    else:
        assert out["time_scope"]["window"] is None                              # unbounded: no window, and not "zero in a window"


def test_the_phase1_internal_no_results_carries_the_time_scope(lg):
    sigs = [_sig(1, "alpha", NOW - D(days=2))]
    posts = [{"index": 1, "title": "x", "link": "https://example.com/not-in-the-stored-set", "role": "buyer",
              "purchase_intent": "explicit", "product_fit": "fits"}]
    ans = {"format": "source_list", "executive_summary": "x", "key_findings": [],
           "platforms": [{"platform": "reddit", "total_analyzed": 1, "shown_count": 1, "posts": posts}]}
    for scope, label in ((_scope(7), "last 7 days"), (None, None)):
        out = ai.enforce_answer_integrity(json.loads(json.dumps(ans)), sigs, sigs, CRM_Q, time_scope=scope)
        assert out["format"] == "no_results" and out["no_results_reason"] == "empty"
        assert (out["time_scope"]["window"] or {}).get("label") == label and out["time_scope"]["shown"] == 0


def test_the_displayed_window_matches_the_retrieval_window(lg):
    req = datetime.now(UTC)
    scope = lg.build_time_scope(7, None, req)
    out = json.loads(lg.enrich_no_results_answer(
        json.dumps(_patch(lg, {"format": "no_results", "message": "x"}, [], scope)), CRM_Q, "all", None, 7, ["crm"]))
    assert out["searched"]["time_window"] == "the last 7 days" and out["time_scope"]["window"]["label"] == "last 7 days"
    assert lg._since_days_window(7, now=req)[1] == timerange.to_utc(out["time_scope"]["window"]["end"])      # same end instant
    unbounded = json.loads(lg.enrich_no_results_answer(
        json.dumps(_patch(lg, {"format": "no_results", "message": "x"}, [], None)), CRM_Q, "all", None, None, ["crm"]))
    assert "no time limit" in unbounded["searched"]["time_window"] and unbounded["time_scope"]["window"] is None


def test_no_format_claims_the_window_means_fresh_collection(lg):
    for fmt in ("no_results", "not_available", "disallowed", "trend_report"):
        text = json.dumps(_patch(lg, {"format": fmt, "message": "Nothing."}, [], _scope(7))).lower()
        assert not any(w in text for w in ("freshly", "real-time", "live", "up to date", "collected today", "continuously"))


@pytest.mark.parametrize("fmt", ["not_available", "disallowed"])
@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_the_ui_states_the_period_for_answers_that_cannot_answer(integrity_browser, fmt, name):
    from tests.test_recency_phase2 import _open
    out = _patch_pure(fmt)
    page, ctx, errors = _open(integrity_browser, name, out)
    try:
        text = page.inner_text(".answer-rendered")
        assert "Showing posts from the last 30 days." in text and "Nothing to report." in text and errors == []
    finally:
        ctx.close()


def _patch_pure(fmt):
    return ai.enforce_temporal_claims({"format": fmt, "message": "Nothing to report."}, [], _scope(30))


# ═══ 4. grammar after a marker is removed ═════════════════════════════════════════════════════════════════════════════
def _sum(text, scope=None, sigs=None):
    sigs = sigs if sigs is not None else [_sig(1, "old", NOW - D(days=300))]
    return _run(sigs, scope, summary=text)["executive_summary"]


@pytest.mark.parametrize("text,expected", [
    ("Latest posts show strong intent.", "Posts show strong intent."),                          # leading marker: capital restored
    ("Currently, buyers compare vendors.", "Buyers compare vendors."),
    ("Buyers are currently comparing vendors.", "Buyers are comparing vendors."),                 # middle marker
    ("Pricing worries buyers, recently.", "Pricing worries buyers."),
    ("Recent buyers asked about pricing. Latest leads want a demo.", "Buyers asked about pricing. Leads want a demo."),   # several
    ("Demand has surged. Pricing is the main concern.", "Pricing is the main concern."),         # whole sentence removed
    ("The data is live and up to date.", "Buyer posts among those shown: one."),                   # nothing left: server sentence
])
def test_removed_markers_leave_sensible_sentences(text, expected):
    assert _sum(text) == expected


def test_an_emptied_headline_or_summary_gets_a_server_sentence():
    sigs = [_sig(1, "a", NOW - D(days=2))]
    out = _run(sigs, _scope(30), summary="Demand has surged.", headline="The data is live and up to date")
    assert out["headline"] == out["executive_summary"] == "Buyer posts found in the last 30 days: one."
    out = _run(sigs, None, summary="Demand has surged.")
    assert out["executive_summary"] == "Buyer posts among those shown: one."


@pytest.mark.parametrize("text", ["There are no recent buyers.", "We cannot confirm that demand is current.",
                                  "The data is not live.", "There is no sign of a surge.", "Not recently updated."])
def test_negated_or_hedged_statements_are_kept_exactly_as_written(text):
    for scope in (None, _scope(30)):
        assert _sum(text, scope) == text                                  # never edited into a stronger / affirmative claim


def test_stripping_never_inverts_a_negation():
    assert _sum("No recent buyers asked for a demo.") == "No recent buyers asked for a demo."


def test_unknown_wording_remains_a_documented_limitation():
    # the validator recognises a closed vocabulary; phrasing outside it is NOT removed (Phase 2.1 audit, section 3)
    assert _sum("Buyers are flocking in.") == "Buyers are flocking in."
