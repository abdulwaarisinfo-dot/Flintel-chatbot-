"""
tests/test_temporal_integrity_step21.py
=======================================
PHASE 2, STEP 2.1 — closes the gaps the final Step 2 verification confirmed.

  1  topic cache   a cache hit may not bypass the lookback: cached posts are checked against the SAME window as a fresh scan
  2  claims        temporal prose (counts in digits and words, "this week", surges, "live data", "current demand is strong")
                   is validated against the stored publication dates, in bounded AND unbounded searches
  3  formats       trend_report / no_results / not_available / disallowed get the same validation and the time scope
  4  ranking       an unknown publication date earns no recency credit on the intent-bridge path either

Offline and synthetic; fixed relative clock; no network, no real Reddit data, no database.
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
import timerange  # noqa: E402
from tests.test_recency_phase2 import (CRM_Q, _answer, _cand, _cls, _mixed, _qi, _rows, _run, _scope, _sig)  # noqa: E402
from tests.test_strict_intent import lg  # noqa: E402,F401
from tests.test_time_ranges import env  # noqa: E402,F401
from tests.test_nonblocking_chat_page import fin  # noqa: E402,F401
from tests.test_time_ranges import app_env  # noqa: E402,F401

UTC = timezone.utc
D = timedelta
NOW = datetime.now(UTC)


# ═══ 1. the topic cache honours the lookback ═══════════════════════════════════════════════════════════════════════
def _post(n, when, **extra):
    p = {"title": f"cached {n}", "post_text": "synthetic text " * 5, "post_url": f"https://www.reddit.com/r/x/comments/c{n}/",
         "platform": "reddit"}
    if when is not None:
        p["published_at"] = when.isoformat() if isinstance(when, datetime) else when
    p.update(extra)
    return p


class _Matcher:
    """Stands in for get_matched_signals: records the call, returns what it was given (no scan_state support)."""
    def __init__(self, returns=()):
        self.returns, self.calls = list(returns), []

    def __call__(self, topic_key, keywords, targeting_platform="all", since_days=None, unfiltered=False, match_phrases=None,
                 limit=None, signals_collection_2=None, chat_id=None, signals_collection_4=None, user_query=None,
                 time_range=None):
        self.calls.append({"since_days": since_days, "limit": limit, "time_range": time_range})
        return list(self.returns)


def _topup(lg, monkeypatch, cached, matcher, required=3, **kw):
    monkeypatch.setattr(lg, "get_cached_topic_evidence",
                        lambda c, t: {"posts": list(cached), "keywords": ["k"], "match_phrases": ["p"]})
    monkeypatch.setattr(lg, "save_topic_evidence_cache", lambda *a, **k: None)
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: False)
    return lg.get_evidence_with_topup("c1", "o1", "topic", ["k"], required, matcher, match_phrases=["p"], **kw)


def _titles(posts):
    return [p["title"] for p in posts]


@pytest.mark.parametrize("days", [7, 30])
def test_a_cache_full_of_stale_posts_cannot_satisfy_a_bounded_query(lg, monkeypatch, days):
    stale = [_post(i, NOW - D(days=days + 20 + i)) for i in range(5)]
    m = _Matcher()
    assert _topup(lg, monkeypatch, stale, m, required=3, since_days=days) == []
    assert len(m.calls) == 1 and m.calls[0]["since_days"] == days            # the normal retrieval ran instead


def test_a_mixed_cache_returns_only_the_in_window_posts(lg, monkeypatch):
    cache = [_post(1, NOW - D(days=2)), _post(2, NOW - D(days=200)), _post(3, NOW - D(days=5)), _post(4, NOW - D(days=90))]
    m = _Matcher()
    out = _topup(lg, monkeypatch, cache, m, required=2, since_days=7)
    assert _titles(out) == ["cached 1", "cached 3"] and m.calls == []        # enough valid evidence: no new search needed


def test_a_cache_of_only_undated_or_invalid_dated_posts_cannot_satisfy_the_requirement(lg, monkeypatch):
    cache = [_post(1, None), _post(2, "not a date"), _post(3, NOW + D(days=40)), _post(4, datetime(1970, 1, 1, tzinfo=UTC)),
             _post(5, datetime(2003, 5, 1, tzinfo=UTC))]
    m = _Matcher()
    assert _topup(lg, monkeypatch, cache, m, required=2, since_days=30) == []
    assert len(m.calls) == 1                                                  # undated / future / pre-2005 count for nothing


def test_insufficient_valid_cache_tops_up_and_never_pads_with_stale_posts(lg, monkeypatch):
    cache = [_post(1, NOW - D(days=1)), _post(2, NOW - D(days=300)), _post(3, None)]
    fresh = [_post(9, NOW - D(days=2))]
    m = _Matcher(returns=fresh)
    out = _topup(lg, monkeypatch, cache, m, required=3, since_days=7)
    assert _titles(out) == ["cached 1", "cached 9"]                           # a shortfall is reported as a shortfall
    assert len(m.calls) == 1 and m.calls[0]["since_days"] == 7 and m.calls[0]["limit"] >= 3


def test_a_post_the_top_up_returns_outside_the_window_is_still_dropped(lg, monkeypatch):
    out = _topup(lg, monkeypatch, [], _Matcher(returns=[_post(1, NOW - D(days=2)), _post(2, NOW - D(days=100)), _post(3, None)]),
                 required=3, since_days=7)
    assert _titles(out) == ["cached 1"]


def test_an_unbounded_search_keeps_serving_its_cache_unchanged(lg, monkeypatch):
    cache = [_post(1, NOW - D(days=900)), _post(2, None), _post(3, NOW - D(days=1))]
    m = _Matcher()
    out = _topup(lg, monkeypatch, cache, m, required=3, since_days=None)
    assert _titles(out) == ["cached 1", "cached 2", "cached 3"] and m.calls == []


@pytest.mark.parametrize("bad", [0, -5, True, None])
def test_a_non_positive_lookback_is_not_a_window(lg, monkeypatch, bad):
    cache = [_post(1, NOW - D(days=900)), _post(2, None), _post(3, NOW - D(days=1))]
    assert len(_topup(lg, monkeypatch, cache, _Matcher(), required=3, since_days=bad)) == 3


def test_an_explicit_calendar_range_keeps_its_own_semantics(lg, monkeypatch):
    rng = timerange.TimeRange(start=NOW - D(days=40), end=NOW - D(days=20), tz_name="UTC", kind="range", phrase="x",
                              capped=False, note="")
    cache = [_post(1, NOW - D(days=1)), _post(2, NOW - D(days=30))]                # the cache is not used for a range
    inside, outside, undated = _post(7, NOW - D(days=30)), _post(8, NOW - D(days=2)), _post(9, None)
    m = _Matcher(returns=[inside, outside, undated])
    out = _topup(lg, monkeypatch, cache, m, required=2, time_range=rng)
    assert _titles(out) == ["cached 7"] and m.calls[0]["time_range"] is rng


def test_cached_posts_outside_the_window_cannot_inflate_counts_or_support_findings(lg, monkeypatch):
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", NOW - D(days=200)), _sig(3, "cedar", None)]
    served = _topup(lg, monkeypatch, [dict(s, title=s["title"]) for s in sigs], _Matcher(), required=1, since_days=7)
    assert [s["post_url"] for s in served] == [sigs[0]["post_url"]]                 # only the in-window post leaves the cache
    f = {"finding": "Teams want a CRM", "evidence_type": "Buyer", "signal_strength": "Strong",
         "supporting_post_indices": [2, 3], "impact": "Teams want a CRM."}
    # even if the stale posts reached the answer (cache disabled elsewhere), they count for nothing in a 7-day report
    out = _run(sigs, _scope(7), findings=[f], summary="We found 3 recent buyers.")
    assert out["counts"]["buyers"] == 1 and out["counts"]["qualified"] == 1 and out["key_findings"] == []


# ═══ 2. temporal claims are validated server-side ══════════════════════════════════════════════════════════════════
OLD_UNDATED = lambda: [_sig(1, "alpha", NOW - D(days=300)), _sig(2, "bravo", None)]
EXACT = ["Buyers are most active this week.", "Demand has surged over the past month.", "Nine buyers posted yesterday.",
         "Interest is trending upward right now.", "The data is live and up to date.", "Current demand is strong."]
KEEP = "Pricing is the main concern."


def _all_text(out):
    return json.dumps({k: v for k, v in out.items() if k not in ("platforms", "counts", "verified_summary", "time_scope")})


@pytest.mark.parametrize("claim", EXACT)
@pytest.mark.parametrize("scope", [_scope(7), None], ids=["7-day window", "unbounded"])
def test_unsupported_temporal_prose_is_removed_and_other_narrative_kept(claim, scope):
    for sigs in (OLD_UNDATED(), _mixed()):
        out = _run(sigs, scope, summary=f"{claim} {KEEP}", findings=[{
            "finding": "Teams want a CRM", "title": "Teams want a CRM", "evidence_type": "Buyer", "signal_strength": "Moderate",
            "supporting_post_indices": [1, 2, 3, 4], "impact": f"{claim} Budget was mentioned."}])
        text = _all_text(out)
        assert claim not in text and claim.rstrip(".") not in text
        assert KEEP in out["executive_summary"]                                    # the non-temporal sentence survives
        for f in out["key_findings"]:
            assert "Budget was mentioned." in f.get("impact", "")                  # ... and so does the rest of a finding


@pytest.mark.parametrize("text,bad", [
    ("Twelve recent buyers found", True), ("12 recent buyers found", True), ("Seven current leads", True),
    ("Two recent buyers found", False)])
@pytest.mark.parametrize("scope", [_scope(30), None], ids=["30-day window", "unbounded"])
def test_word_and_digit_counts_in_a_headline_match_the_server(text, bad, scope):
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", NOW - D(days=4))]        # two buyers
    out = _run(sigs, scope, headline=text)
    assert out["counts"]["buyers"] == 2
    if bad:
        assert out["headline"].lower().startswith("two ")                              # the SERVER count, never the claim
        assert "twelve" not in out["headline"].lower() and "12" not in out["headline"] and "seven" not in out["headline"].lower()
    else:
        assert out["headline"].startswith("Two ")


def test_word_counts_are_checked_like_digit_counts_in_a_zero_evidence_report():
    out = _run(OLD_UNDATED(), _scope(7), headline="Twelve recent buyers found", summary="We found twelve recent buyers.")
    assert out["counts"]["buyers"] == 0
    assert "twelve" not in _all_text(out).lower() and "12" not in out["headline"]
    assert out["headline"] == "No recent buyers found"             # the server's zero; a negated clause is kept as written (Phase 2.2)


def test_supported_time_references_and_counts_are_kept():
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", NOW - D(days=4)), _sig(3, "cedar", NOW - D(days=40))]
    ok = _run(sigs, None, summary="In the last 7 days there were two buyers. Two recent buyers asked for a CRM.")
    assert ok["executive_summary"].startswith("In the last seven days there were two buyers.")
    wrong = _run(sigs, None, summary="In the last 7 days there were nine buyers. In the past 60 days there were three buyers.")
    assert "nine" not in wrong["executive_summary"] and "past sixty days there were three buyers" in wrong["executive_summary"]
    none_in_period = _run(OLD_UNDATED(), None, summary="Buyers posted in the last 7 days. " + KEEP)
    assert none_in_period["executive_summary"] == KEEP


def test_negated_or_hedged_statements_are_disclosures_not_claims():
    for ok in ("The data is not live.", "There is no sign of a surge.", "We cannot confirm that demand is current."):
        assert _run(_mixed(), _scope(), summary=f"{ok} {KEEP}")["executive_summary"].startswith(ok)


def test_currency_words_survive_only_inside_a_bounded_search_with_evidence():
    sigs = [_sig(1, "alpha", NOW - D(days=2))]
    assert "recent" in _run(sigs, _scope(), summary="Recent buyers asked about pricing.")["executive_summary"].lower()
    unbounded = _run(sigs, None, summary="Recent buyers asked about pricing. Buyers are currently comparing vendors.")["executive_summary"]
    assert "ecent" not in unbounded and "currently" not in unbounded and "pricing" in unbounded
    assert "recent" not in _run(OLD_UNDATED(), _scope(7), summary="Recent buyers asked about pricing.")["executive_summary"].lower()


def test_trend_claims_are_removed_everywhere_in_a_source_report():
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", NOW - D(days=20))]
    for claim in ("Demand rose sharply compared to last month.", "Mentions are growing.", "This was the busiest period."):
        assert claim.rstrip(".") not in _run(sigs, None, summary=f"{claim} {KEEP}")["executive_summary"]


# ═══ 3. every response format ═════════════════════════════════════════════════════════════════════════════════════════
def _patch(lg, data, sigs, scope=None):
    with lg.time_scope_context(scope):
        return json.loads(lg._patch_post_urls_into_answer(json.dumps(data), sigs, query=CRM_Q))


def _trend(**over):
    d = {"format": "trend_report", "topic": "CRM", "window": "Last 30 days", "platforms": "reddit",
         "shift_table": {"headers": ["Metric", "Start", "End", "Change"], "rows": [["Positive", "50%", "60%", "+10 pts"]]},
         "interpretation": "Sentiment improved. Interest is trending upward right now. Pricing is the main concern.",
         "weekly_table": {"headers": ["Week", "Positive"], "rows": [["Week 1", "50%"]]},
         "positive_drivers": [{"platform": "reddit", "post": "Growing excitement about the new CRM", "sentiment": "positive", "theme": "x"}],
         "trend": "Demand will keep rising.", "conclusion": "The data is live and up to date. Teams like it."}
    d.update(over)
    return d


def test_a_trend_report_without_comparative_dated_evidence_makes_no_trend_claim(lg):
    one_week = [_sig(1, "a", NOW - D(days=1)), _sig(2, "b", NOW - D(days=2))]
    scope = _scope(30)
    for sigs in (one_week, [], [_sig(3, "c", None)]):
        out = _patch(lg, _trend(), sigs, scope)
        assert out["trend_check"]["comparative_evidence"] is False and out["trend_check"]["figures_verified"] is False
        assert all(k not in out for k in ("shift_table", "weekly_table", "trend"))
        assert "trending upward" not in out["interpretation"] and "Pricing is the main concern." in out["interpretation"]
        assert "live" not in out["conclusion"] and "Teams like it." in out["conclusion"]
        assert out["window"] == "Last 30 days" and out["time_scope"]["window"]["label"] == "last 30 days"
        assert out["positive_drivers"][0]["post"].startswith("Growing excitement")      # post paraphrases are not claims


def test_dated_evidence_in_two_weeks_is_no_longer_treated_as_a_trend(lg):
    # Phase 2.1 kept the model's tables when posts fell in two different weeks. That was the defect: dates are not a trend.
    # Phase 2.2 removes the model's figures always (tests/test_phase22_trend_report.py has the full matrix).
    sigs = [_sig(1, "a", NOW - D(days=2)), _sig(2, "b", NOW - D(days=16))]
    out = _patch(lg, _trend(window="Last 90 days"), sigs, _scope(30))
    assert out["trend_check"]["comparative_evidence"] is False and "shift_table" not in out and "weekly_table" not in out
    assert out["window"] == "Last 30 days"                                       # the window the server applied, not the model's
    assert "live" not in out["conclusion"]                                       # a freshness claim is never supported


def test_an_unbounded_trend_report_states_that_no_time_limit_applied(lg):
    out = _patch(lg, _trend(), [_sig(1, "a", NOW - D(days=1))], None)
    assert out["window"] == "All collected posts (no time limit)" and out["time_scope"]["window"] is None


@pytest.mark.parametrize("fmt", ["no_results", "not_available", "disallowed"])
def test_formats_without_posts_cannot_claim_demand_trends_or_freshness(lg, fmt):
    data = {"format": fmt, "message": "Current demand is strong and the data is live.", "likely_reason": "Interest is surging.",
            "headline": "Twelve recent buyers found", "suggested_actions": [], "next_steps": ["Try a broader search."]}
    out = _patch(lg, data, [], _scope(7))
    text = json.dumps(out)
    for bad in ("demand is strong", "is live", "surging", "Twelve", "recent buyers"):
        assert bad not in text
    assert out["message"] and out["next_steps"] == ["Try a broader search."]       # a safe message replaces the claim
    assert out["time_scope"]["window"]["label"] == "last 7 days"


def test_a_clean_no_results_answer_only_gains_the_time_scope(lg):
    data = {"format": "no_results", "message": "No posts matched.", "likely_reason": "The topic is narrow."}
    out = _patch(lg, data, [], None)
    assert out["message"] == "No posts matched." and out["likely_reason"] == "The topic is narrow." and out["time_scope"]["window"] is None


def test_plain_text_and_unknown_formats_were_left_alone_until_gate_2(lg):
    # Phase 2.1 documented these two paths as uncovered. Gate 2 closes them (tests/test_gate2_pilot_safety.py has the matrix):
    # a plain-text answer that is nothing but an unsupported claim is replaced by the fixed unavailable reply, and a JSON answer
    # with an unrecognised format is validated like a no_results answer. Wording outside the vocabulary is still not covered.
    assert lg._patch_post_urls_into_answer("Demand is surging.", [], query=CRM_Q) == lg.CHAT_UNAVAILABLE_REPLY
    raw = json.dumps({"format": "mystery", "message": "Demand is surging. Pricing matters."})
    out = json.loads(lg._patch_post_urls_into_answer(raw, [_sig(1, "a", NOW)], query=CRM_Q))
    assert out["message"] == "Pricing matters." and out["format"] == "mystery"
    assert lg._patch_post_urls_into_answer("Buyers are flocking in.", [], query=CRM_Q) == "Buyers are flocking in."   # still uncovered


def test_strict_mode_validates_the_same_formats(lg, monkeypatch):
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: True)
    out = _patch(lg, {"format": "no_results", "message": "Demand is surging. No posts matched."}, [], _scope(7))
    assert "surging" not in out["message"] and "No posts matched." in out["message"]


def test_a_source_report_keeps_its_table_and_gains_nothing_it_did_not_have(lg):
    sigs = _mixed()
    out = _patch(lg, _answer(sigs), sigs, _scope())
    assert out["time_scope"]["window"]["label"] == "last 30 days" and out["counts"]["qualified"] == 2
    assert all(r["link"].startswith("https://") for r in _rows(out))


def test_the_timeout_fallback_answer_is_validated_like_any_other(env, monkeypatch):
    lg, idx, chats = env
    saved = {}
    sigs = [_sig(1, "alpha", NOW - D(days=2))]
    monkeypatch.setattr(lg, "get_evidence_with_topup", lambda **kw: list(sigs))
    claim = {"format": "no_results", "message": "Current demand is strong. No verified posts."}
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps(claim))
    monkeypatch.setattr(lg, "analyze_with_claude", lambda *a, **k: json.dumps(claim), raising=False)
    monkeypatch.setattr(idx, "save_answer_for_gate", lambda chat_id, owner, topic, answer, *a, **k: saved.update(answer=answer) or True)
    try:
        lg._timeout_fallback_answer("c1", "o1", "tk", "latest CRM buyers", ["crm"], None, 5, time_window_days=30, requested_at=NOW)
    except Exception as exc:                                       # persistence is not under test; the saved answer is
        print("fallback stopped after saving:", type(exc).__name__)
    assert saved, "the fallback did not reach the save step"
    final = json.loads(saved["answer"])
    assert "demand is strong" not in json.dumps(final) and final["time_scope"]["window"]["label"] == "last 30 days"


# ═══ 4. unknown-date ranking is consistent ════════════════════════════════════════════════════════════════════════
def _order_bridge(*pairs):
    import intent_bridge as ib
    cands = {u: _cand(u, (NOW - D(days=d)).isoformat() if d is not None else None) for u, d in pairs}
    return [c["post_url"] for c in ib._rank_passing([(cands[u], 0.8, _cls()) for u, _ in pairs], _qi())]


def test_an_unknown_date_earns_no_recency_advantage_on_the_bridge_path():
    assert _order_bridge(("undated", None), ("d90", 90)) == ["d90", "undated"]            # was: undated first (+0.021)
    assert _order_bridge(("d90", 90), ("undated", None)) == ["d90", "undated"]
    # a dated post, however old, is never ranked BELOW an unknown one by the old 0.35 credit; unknown is never ahead of it
    assert _order_bridge(("undated", None), ("old", 800))[-1] == "undated"
    assert _order_bridge(("old", 800), ("undated", None))[-1] == "undated"


def test_valid_publication_decay_is_unchanged_on_the_bridge_path():
    assert _order_bridge(("d90", 90), ("d30", 30), ("new", 0.1), ("undated", None)) == ["new", "d30", "d90", "undated"]


def test_the_shared_weights_file_is_not_modified_by_the_bridge():
    import intent_bridge as ib
    from intent_prototype import opportunity
    _order_bridge(("undated", None), ("d90", 90))
    assert opportunity.load_weights()["recency"]["missing_date_score"] == 0.35           # the prototype keeps its value


def test_default_and_bridge_paths_agree_that_an_unknown_date_gets_nothing(lg):
    assert lg._recency_bonus({"title": "x"}) == 0.0
    assert lg._recency_bonus({"created_utc": NOW + D(days=40)}) == 0.0


# ═══ 5. the live streaming route applies the same validation ═══════════════════════════════════════════════════════
def test_the_streaming_route_validates_temporal_claims_and_saves_the_message_window(request, monkeypatch):
    nb = pytest.importorskip("tests.test_nonblocking_chat_page")
    f = request.getfixturevalue("fin")
    rt = f["rt"]
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", NOW - D(days=300)), _sig(3, "cedar", None)]
    turn = dict(nb._turn(CRM_Q, 1), evidence_required=len(sigs), time_window_days=7, requested_at=NOW)
    nb._seed(f["chats"], "c21", [turn])
    claims = _answer(sigs, summary="Twelve recent buyers found. Demand has surged over the past month. " + KEEP,
                     headline="The data is live and up to date")
    monkeypatch.setattr(rt, "get_evidence_with_topup", lambda **kw: [dict(s) for s in sigs])
    monkeypatch.setattr(rt, "analyze_with_claude", lambda *a, **k: json.dumps(claims), raising=False)
    events = []
    for chunk in nb._stream(f, monkeypatch, "c21", topic_key=CRM_Q.lower()): 
        events.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
        if '"done"' in events[-1] or '"error"' in events[-1]:
            break
    assert '"done"' in events[-1], events[-3:]
    saved = json.loads(f["chats"].find_one({"chat_id": "c21"})["messages"][-1]["claude_answer"])
    assert saved["time_scope"]["window"]["label"] == "last 7 days" and saved["counts"]["buyers"] == 1
    text = json.dumps({k: v for k, v in saved.items() if k not in ("platforms", "counts", "verified_summary", "time_scope")})
    for bad in ("Twelve", "surged", "live and up to date"):
        assert bad not in text
    assert KEEP in saved["executive_summary"]


def test_the_incremental_scan_state_is_built_from_the_filtered_cache_too(lg, monkeypatch):
    seen = {}
    monkeypatch.setattr(lg, "_prepare_scan_state", lambda cached, *a, **k: seen.update(posts=_titles(cached["posts"])) or None)
    cache = [_post(1, NOW - D(days=1)), _post(2, NOW - D(days=300))]
    _topup(lg, monkeypatch, cache, _Matcher(), required=3, since_days=7)
    assert seen["posts"] == ["cached 1"]                       # stale posts are not carried into a delta scan
