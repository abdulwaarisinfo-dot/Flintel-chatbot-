"""
tests/test_historical_1500_window.py — the Historical search's existing time-range path applies ONE [start, end) interval of ORIGINAL
publication time to BOTH MongoDB and MySQL. This file pins that behaviour for the 1500-minute interval the Active Intent workflow gives
it; it changes no retrieval code (the workflow only supplies the same `time_range` a user-typed range would). The fakes are the existing
FakeMySQL / Mongo stand-ins of tests/test_mysql_read.py: no network, no database.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_mysql_read import (FakeMySQL, REDDIT_COLS, GOOGLE_COLS, cfg, g_row, install, lg, mdoc, r_row, run, NOW)  # noqa: F401
from timerange import TimeRange

END = NOW
START = END - timedelta(minutes=1500)
TR = TimeRange(start=START, end=END, tz_name="UTC", kind="rolling", phrase="last 1500 minutes")

#            label                         created_utc offset            expected inside
CASES = [("one_second_before_start", START - timedelta(seconds=1), False),
         ("exactly_at_start", START, True),
         ("middle", START + timedelta(minutes=700), True),
         ("one_second_before_end", END - timedelta(seconds=1), True),
         ("exactly_at_end", END, False),
         ("future_dated", END + timedelta(minutes=5), False)]


def naive(t):
    return t.astimezone(timezone.utc).replace(tzinfo=None)


def mongo_docs():
    docs = []
    for i, (label, when, _inside) in enumerate(CASES):
        d = mdoc(f"m/{label}", 0.95)
        d["created_utc"] = naive(when)                         # stored naive-UTC, as the signals collections hold it
        docs.append(d)
    for label, when in (("missing_date", None), ("invalid_date", "not a date"), ("epoch_zero", 0)):
        d = mdoc(f"m/{label}", 0.95)
        d["created_utc"] = when
        docs.append(d)
    return docs


def mysql_tables():
    rows = []
    for i, (label, when, _inside) in enumerate(CASES, start=1):
        r = r_row(i, f"u/{label}", 0.9)
        r["created_utc"] = naive(when)
        rows.append(r)
    r = r_row(50, "u/missing_date", 0.9)
    r["created_utc"] = None
    rows.append(r)
    return {"flintel": (REDDIT_COLS, rows), "flintel_static": (REDDIT_COLS[:], []), "flintel_google": (GOOGLE_COLS, [])}


def test_mongodb_and_mysql_apply_the_same_exact_half_open_boundary(lg, cfg, monkeypatch):
    srv = FakeMySQL(mysql_tables())
    install(lg, cfg, monkeypatch, srv)
    out, _state, _calls = run(lg, monkeypatch, mongo_docs(), time_range=TR, limit=50)
    got = {p["post_url"] for p in out}
    inside = {label for label, _w, ok in CASES if ok}
    outside = {label for label, _w, ok in CASES if not ok} | {"missing_date"}
    for label in inside:
        assert f"m/{label}" in got and f"u/{label}" in got, label                 # inside the interval: eligible in BOTH sources
    for label in outside:
        assert f"m/{label}" not in got and f"u/{label}" not in got, label        # outside / future / undated: in NEITHER
    assert "m/invalid_date" not in got and "m/epoch_zero" not in got              # unreadable timestamps never count as inside


def test_the_mysql_query_carries_the_exact_start_and_the_mongo_query_both_ends(lg, cfg, monkeypatch):
    srv = FakeMySQL(mysql_tables())
    install(lg, cfg, monkeypatch, srv)
    coll_docs = mongo_docs()
    run(lg, monkeypatch, coll_docs, time_range=TR, limit=50)
    data = [(s, p) for s, p in srv.executed if "information_schema" not in s]
    assert data and all(len(p) == 3 and p[1] == naive(START) for _s, p in data)    # the SQL lower bound is exactly the start (UTC, naive)
    f_sql = [s for s, _ in data if "`flintel`.`" in s][0]
    assert "`created_utc` >= %s" in f_sql                                          # the original publication-time column
    import logics
    find_args = [c.args[0] for c in logics.signals_collection.find.call_args_list]
    assert any(repr({"$gte": START, "$lt": END}) in repr(q) for q in find_args)    # MongoDB: [start, end) on created_utc


def test_without_a_range_nothing_about_the_scan_is_time_bounded(lg, cfg, monkeypatch):
    srv = FakeMySQL(mysql_tables())
    install(lg, cfg, monkeypatch, srv)
    out, _s, _c = run(lg, monkeypatch, mongo_docs(), limit=50)
    urls = {p["post_url"] for p in out}
    assert "u/one_second_before_start" in urls and "m/one_second_before_start" in urls   # the unchanged, un-windowed behaviour
    data = [p for s, p in srv.executed if "information_schema" not in s]
    assert all(len(p) == 2 for p in data)                                                # no cutoff parameter at all


def test_the_google_table_keeps_its_documented_posted_at_then_created_at_mapping(lg, cfg, monkeypatch):
    """Pre-existing, documented in mysql_signals: flintel_google has no created_utc, so the time column is
    COALESCE(posted_at, created_at). This is a fallback to ingestion time ONLY when posted_at is NULL - recorded here, not changed."""
    tables = mysql_tables()
    tables["flintel_google"] = (GOOGLE_COLS, [g_row(1, "u/g_in", 0.9, posted_days=None, created_days=0),
                                              g_row(2, "u/g_old", 0.9, posted_days=5, created_days=0)])
    srv = FakeMySQL(tables)
    install(lg, cfg, monkeypatch, srv)
    out, _s, _c = run(lg, monkeypatch, [], time_range=TR, limit=50)
    urls = {p["post_url"] for p in out}
    assert "u/g_old" not in urls                       # a real posted_at outside the interval excludes the row, whatever created_at says
    g_sql = [s for s, _ in srv.executed if "`flintel_google`" in s and "information_schema" not in s][0]
    assert "COALESCE(`posted_at`, `created_at`) >= %s" in g_sql
