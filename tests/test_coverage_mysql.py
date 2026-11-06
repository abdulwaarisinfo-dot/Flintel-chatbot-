"""Zero-result coverage across MongoDB AND MySQL: every distinction in the explanation, never a claim of complete coverage when it is unknown.
MySQL is a fake connection (labelled): no server, no network. Mongo is mongomock."""
from datetime import datetime, timedelta, timezone

import pytest

mongomock = pytest.importorskip("mongomock")
import importlib
import sys
import signal_intel as si
from tests.test_general_discovery import NOW, WIN, stored, code_of  # noqa: F401
from tests.test_strict_intent import _set_cfg  # noqa: F401
from tests.test_time_ranges import app_env, env, fixed_clock  # noqa: F401,E402

UTC = timezone.utc


class _Live:
    """Resolve `mysql_signals` at call time: other test modules reload / replace it, and logics imports it lazily from sys.modules."""
    def __getattr__(self, name):
        return getattr(sys.modules.get("mysql_signals") or importlib.import_module("mysql_signals"), name)


ms = _Live()


@pytest.fixture
def lg(fixed_clock):
    return fixed_clock[0]


def doc(days_ago):
    return {"platform": "reddit", "post_url": f"https://www.reddit.com/r/a/comments/{days_ago}/t/", "title": "t",
            "created_utc": NOW - timedelta(days=days_ago)}


class FakeCursor:
    """Answers the three probe statements from a canned table; records every SQL it is given (all must be SELECTs)."""
    def __init__(self, rows, cols=("id", "post_url", "text", "embedding", "created_utc"), fail=None):
        self.rows, self.cols, self.fail, self.sql, self.params = rows, cols, fail, [], []

    def execute(self, sql, params=None):
        if self.fail:
            raise self.fail
        self.sql.append(sql)
        self.params.append(params)
        self._sql, self._params = sql, params

    def fetchall(self):
        s = self._sql
        if "information_schema" in s:
            return [(c,) for c in self.cols]
        times = [r for r in self.rows]
        if "MIN(" in s:
            return [(len(times), min(times) if times else None, max(times) if times else None)]
        lo, hi = None, None
        p = list(self._params or [])
        if ">=" in s:
            lo = p.pop(0)
        if "< %s" in s:
            hi = p.pop(0)
        return [(sum(1 for t in times if (lo is None or t >= lo) and (hi is None or t < hi)),)]

    def close(self):
        pass


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def close(self):
        pass


def naive(days_ago):
    return (NOW - timedelta(days=days_ago)).astimezone(UTC).replace(tzinfo=None)


# ── the probe itself ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_probe_counts_total_range_and_span_with_select_only_bounded_sql():
    cur = FakeCursor([naive(10), naive(3), naive(0.5)])
    r = ms.probe_coverage("flintel", NOW - timedelta(days=1), NOW, connect_fn=lambda: FakeConn(cur))
    assert r["total"] is True and r["in_range"] == 1 and r["oldest"] < r["newest"]
    assert all(s.lstrip().upper().startswith("SELECT") for s in cur.sql)
    assert all("MAX_EXECUTION_TIME" in s for s in cur.sql if "information_schema" not in s)
    assert all("%s" in s or "information_schema" in s or not (p or ()) for s, p in zip(cur.sql, cur.params))   # values are parameters


def test_probe_rejects_an_invalid_database_name_before_any_connection():
    called = []
    with pytest.raises(ValueError):
        ms.probe_coverage("x`; DROP TABLE y; --", connect_fn=lambda: called.append(1))
    assert called == []


def test_probe_on_an_empty_table_says_empty_not_unknown():
    r = ms.probe_coverage("flintel", NOW - timedelta(days=1), NOW, connect_fn=lambda: FakeConn(FakeCursor([])))
    assert r["total"] is False and r["in_range"] == 0


def test_probe_raises_when_the_server_fails():
    with pytest.raises(RuntimeError):
        ms.probe_coverage("flintel", connect_fn=lambda: FakeConn(FakeCursor([], fail=RuntimeError("down"))))


def test_probe_raises_when_there_is_no_publication_column():
    with pytest.raises(ms.SourceUnreadable):
        ms.probe_coverage("flintel", connect_fn=lambda: FakeConn(FakeCursor([], cols=("id", "post_url", "text", "embedding"))))


# ── integrated into the explanation ─────────────────────────────────────────────────────────────────────────────────────
def _stores(lg, monkeypatch, docs):
    monkeypatch.setattr(lg, "signals_collection", stored(docs))
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)


def _mysql(lg, monkeypatch, *, probe, cursor=None, boom=None):
    _set_cfg(monkeypatch, MYSQL_READ_ENABLED=True, MYSQL_READ_DATABASES="flintel", ZERO_RESULT_MYSQL_PROBE_ENABLED=probe)
    monkeypatch.setattr(sys.modules.get("mysql_signals") or importlib.import_module("mysql_signals"), "connect",
                        (lambda: (_ for _ in ()).throw(boom)) if boom else (lambda: FakeConn(cursor)))


def test_1_no_matching_posts_topic(lg, monkeypatch):
    _stores(lg, monkeypatch, [doc(0.2)])
    cov = lg.probe_stored_coverage("reddit", *WIN)
    assert si.explain_zero_result(dict(cov, mode="topic", topic="fusion"))["code"] == si.NO_TOPIC_MATCH


def test_2_no_historical_data_collected(lg, monkeypatch):
    _stores(lg, monkeypatch, [])
    assert si.explain_zero_result(lg.probe_stored_coverage("reddit", *WIN))["code"] == si.NO_POSTS_COLLECTED


def test_3_source_unavailable(lg):
    assert si.explain_zero_result({"source": "X", "source_available": False})["code"] == si.SOURCE_UNAVAILABLE


def test_4_collection_failure_is_the_live_or_collector_side():
    assert si.explain_zero_result({"source": "Hacker News", "error": "timeout"})["code"] == si.COLLECTION_FAILED


def test_5_database_unavailable_is_distinct_from_collection_failure(lg, monkeypatch):
    class Boom:
        def find(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(lg, "signals_collection", Boom())
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    ex = si.explain_zero_result(lg.probe_stored_coverage("reddit", *WIN))
    assert ex["code"] == si.DATABASE_UNAVAILABLE and "not an empty result" in ex["message"]


def test_6_unknown_coverage_when_mysql_is_enabled_but_not_probed(lg, monkeypatch):
    _stores(lg, monkeypatch, [])
    _mysql(lg, monkeypatch, probe=False)
    cov = lg.probe_stored_coverage("reddit", *WIN)
    ex = si.explain_zero_result(cov)
    assert cov["total"] is None and ex["code"] == si.UNKNOWN and "not probed" in ex["message"]


def test_6b_mongo_has_posts_none_in_range_but_mysql_unprobed_is_NOT_none_in_range(lg, monkeypatch):
    _stores(lg, monkeypatch, [doc(10)])                         # Mongo: posts exist, none in the last day
    _mysql(lg, monkeypatch, probe=False)
    ex = si.explain_zero_result(lg.probe_stored_coverage("reddit", *WIN))
    assert ex["code"] == si.UNKNOWN                              # MySQL might hold in-range posts: complete coverage is not claimed


def test_7_filters_excluded_results():
    ex = si.explain_zero_result({"source": "Reddit", "in_range": 5, "examined": 5, "excluded_filters": 5, "mode": "topic"})
    assert ex["code"] == si.EXCLUDED_BY_FILTERS


def test_mysql_probe_on_and_empty_everywhere_is_no_posts_collected(lg, monkeypatch):
    _stores(lg, monkeypatch, [])
    _mysql(lg, monkeypatch, probe=True, cursor=FakeCursor([]))
    assert si.explain_zero_result(lg.probe_stored_coverage("reddit", *WIN))["code"] == si.NO_POSTS_COLLECTED


def test_mysql_probe_on_finds_posts_out_of_range(lg, monkeypatch):
    _stores(lg, monkeypatch, [])
    _mysql(lg, monkeypatch, probe=True, cursor=FakeCursor([naive(20), naive(15)]))
    cov = lg.probe_stored_coverage("reddit", *WIN)
    assert cov["total"] == 1 and cov["in_range"] == 0 and si.explain_zero_result(cov)["code"] == si.NONE_IN_RANGE


def test_mysql_probe_on_finds_posts_in_range_so_it_is_a_topic_miss(lg, monkeypatch):
    _stores(lg, monkeypatch, [])
    _mysql(lg, monkeypatch, probe=True, cursor=FakeCursor([naive(0.3)]))
    cov = lg.probe_stored_coverage("reddit", *WIN)
    assert cov["in_range"] == 1 and si.explain_zero_result(dict(cov, mode="topic", topic="fusion"))["code"] == si.NO_TOPIC_MATCH


def test_mysql_unreachable_is_unknown_never_empty(lg, monkeypatch):
    _stores(lg, monkeypatch, [])
    _mysql(lg, monkeypatch, probe=True, boom=ConnectionError("MySQL connect failed"))
    cov = lg.probe_stored_coverage("reddit", *WIN)
    ex = si.explain_zero_result(cov)
    assert cov["total"] is None and ex["code"] == si.UNKNOWN and "could not be probed" in ex["message"]


def test_the_probe_is_off_by_default():
    import config
    assert config.ZERO_RESULT_MYSQL_PROBE_ENABLED is False
