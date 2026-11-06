"""The seven distinguishable 'nothing to show' situations, per search mode. Offline (mongomock + the repo's Hacker News doubles)."""
from datetime import datetime, timedelta, timezone

import pytest

mongomock = pytest.importorskip("mongomock")
import signal_intel as si
from tests.test_general_discovery import NOW, WIN, code_of, doc, stored  # noqa: F401
from tests.test_hackernews_pipeline import hn, lg  # noqa: F401  (fixtures: Hacker News registered; the logics module)


def msg(platform, mode, **kw):
    return dict({"targeting_platform": platform, "search_mode": mode, "query": "fusion power", "topic_key": "tk", "time_window_days": 1}, **kw)


def codes(lg_, m, chat="c"):
    return [si.explain_zero_result(c)["code"] for c in lg_.zero_result_coverage_for(m, chat)]


def _stores(lg_, monkeypatch, docs):
    monkeypatch.setattr(lg_, "signals_collection", stored(docs))
    monkeypatch.setattr(lg_, "signals_collection_2", None)
    monkeypatch.setattr(lg_, "signals_collection_4", None)


def test_live_on_a_source_without_a_live_path_is_unsupported_not_empty_and_its_stored_data_is_not_probed(lg, hn, monkeypatch):
    _stores(lg, monkeypatch, [doc(1, NOW - timedelta(hours=2))])
    monkeypatch.setattr(lg, "probe_stored_coverage", lambda *a, **k: pytest.fail("stored data must not be probed in Live mode"))
    cov = lg.zero_result_coverage_for(msg("reddit", "live"), "c")
    ex = si.explain_zero_result(cov[0])
    assert [c["source"] for c in cov] == ["Reddit"] and ex["code"] == si.UNSUPPORTED_MODE
    assert "does not support Live" in ex["headline"] and "not queried" in ex["message"] and any("Historical" in s for s in ex["next_steps"])


def test_live_on_reddit_and_hackernews_explains_each_source_separately(lg, hn, monkeypatch):
    lg._HN_STATUS[("c", "tk")] = {"state": "empty", "fetched_hits": 0, "probe": "ok", "newest_available": (NOW - timedelta(days=9)).isoformat()}
    cov = lg.zero_result_coverage_for(msg("reddit,hackernews", "live"), "c")
    assert [si.explain_zero_result(c)["code"] for c in cov] == [si.UNSUPPORTED_MODE, si.NONE_IN_RANGE]
    assert si.combine_zero_results([si.explain_zero_result(c) for c in cov])["code"] == "MIXED"


def test_historical_on_hackernews_reports_the_stored_posts_not_a_live_fetch_that_never_ran(lg, hn, monkeypatch):
    lg._HN_STATUS[("c", "tk")] = {"state": "failed", "reason": "timeout"}          # a stale live status must be ignored in Historical mode
    _stores(lg, monkeypatch, [doc(1, NOW - timedelta(days=30), platform="hackernews")])
    cov = lg.zero_result_coverage_for(msg("hackernews", "historical"), "c")
    assert len(cov) == 1 and cov[0]["source"] == "Hacker News" and cov[0]["total"] == 1 and cov[0]["in_range"] == 0
    assert si.explain_zero_result(cov[0])["code"] == si.NONE_IN_RANGE


def test_historical_on_an_empty_store_is_no_posts_collected(lg, hn, monkeypatch):
    _stores(lg, monkeypatch, [])
    assert codes(lg, msg("hackernews", "historical")) == [si.NO_POSTS_COLLECTED]
    assert codes(lg, msg("reddit", "historical")) == [si.NO_POSTS_COLLECTED]


def test_combined_with_a_failed_live_path_and_an_empty_stored_path_is_partial_retrieval(lg, hn, monkeypatch):
    _stores(lg, monkeypatch, [doc(1, NOW - timedelta(hours=2))])
    lg._HN_STATUS[("c", "tk")] = {"state": "failed", "reason": "timeout"}
    cov = lg.zero_result_coverage_for(msg("reddit,hackernews", "combined", time_window_days=1), "c")
    combined = si.combine_zero_results([si.explain_zero_result(c) for c in cov])
    assert combined["code"] == si.PARTIAL_RETRIEVAL and "Hacker News" in combined["headline"]


def test_a_search_without_a_chosen_mode_behaves_exactly_as_before(lg, hn, monkeypatch):
    _stores(lg, monkeypatch, [doc(1, NOW - timedelta(hours=2))])
    m = {"targeting_platform": "reddit", "query": "fusion power", "topic_key": "tk", "time_window_days": 1}
    assert codes(lg, m) == [si.NO_TOPIC_MATCH]
    assert code_of(lg.zero_result_coverage_for(m, "c")) == si.NO_TOPIC_MATCH


# ── every situation has its own code ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("situation,cov,code", [
    ("no relevant results (posts exist in range, none on topic)", {"source": "X", "total": 5, "in_range": 5, "mode": "topic", "topic": "t"}, si.NO_TOPIC_MATCH),
    ("source unavailable", {"source": "X", "source_available": False}, si.SOURCE_UNAVAILABLE),
    ("collection failed", {"source": "X", "error": "timeout"}, si.COLLECTION_FAILED),
    ("historical data unavailable (nothing collected)", {"source": "X", "total": 0}, si.NO_POSTS_COLLECTED),
    ("no posts in the requested period", {"source": "X", "total": 5, "in_range": 0}, si.NONE_IN_RANGE),
    ("unsupported search mode", {"source": "X", "mode_unsupported": "live"}, si.UNSUPPORTED_MODE),
    ("database unavailable", {"source": "X", "error": "e", "db_unavailable": True}, si.DATABASE_UNAVAILABLE),
    ("excluded by filters", {"source": "X", "in_range": 4, "examined": 4, "excluded_filters": 4}, si.EXCLUDED_BY_FILTERS),
    ("coverage unknown", {"source": "X"}, si.UNKNOWN),
])
def test_each_situation_has_its_own_explanation(situation, cov, code):
    ex = si.explain_zero_result(cov)
    assert ex["code"] == code and ex["headline"] and ex["message"] and ex["next_steps"], situation
    assert code in si.ZERO_CODES


def test_a_failure_plus_an_absence_is_partial_retrieval_but_two_absences_stay_mixed():
    fail = si.explain_zero_result({"source": "A", "error": "timeout"})
    none = si.explain_zero_result({"source": "B", "total": 0})
    other = si.explain_zero_result({"source": "C", "total": 3, "in_range": 0})
    assert si.combine_zero_results([fail, none])["code"] == si.PARTIAL_RETRIEVAL
    assert si.combine_zero_results([none, other])["code"] == "MIXED"
    assert si.combine_zero_results([fail, fail])["code"] == si.COLLECTION_FAILED
