"""
tests/test_timestamp_correctness.py
===================================
PHASE 2, STEP 1 — a stored publication time is trusted only when it is a plausible past date.

  * missing, unreadable, epoch-zero / pre-Reddit (before 2005-06-01) and future (> now + 10 min) values are all treated the
    same way: UNKNOWN. They are never shown as a post date, never score as "fresh", and a "last N days" window never admits
    them. Ingestion / scan time is never substituted.
  * legitimate historical dates (2008, 2024, ...) and recent dates are untouched; timerange.to_utc stays a plain parser
    (it also parses range endpoints), and explicit time ranges behave as before.

Offline and synthetic: every post below is invented for these tests.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import timerange  # noqa: E402
from intent_prototype import opportunity  # noqa: E402
from tests.test_strict_intent import Harness, _mkdoc, lg  # noqa: E402,F401  (fixture + harness)
from tests.test_strict_intent import FakeSignals  # noqa: E402

UTC = timezone.utc
NOW = datetime.now(UTC)
D = timedelta
CFG = {"half_life_days": 30, "floor": 0.0, "missing_date_score": 0.35}


# ── publication_time: one rule for every kind of bad value ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", [
    None, "", "   ", "not a date", "2024-13-45T00:00:00Z", True, False, [], {},
    0, 0.0, -5, -1.7e9, 1.7e12, "0",                              # zero, negative, millisecond epoch, numeric string
    datetime(1970, 1, 1), datetime(1970, 1, 1, tzinfo=UTC), "1970-01-01T00:00:00Z",       # epoch zero
    datetime(1999, 12, 31, tzinfo=UTC), datetime(2005, 5, 31, 23, 59, tzinfo=UTC),         # before Reddit existed
    NOW + D(days=40), lambda: datetime.now(UTC) + D(minutes=11), NOW.replace(tzinfo=None) + D(days=1),
    (NOW + D(days=3)).isoformat(),                                                                           # future
])
def test_missing_invalid_epoch_zero_and_future_values_are_unknown(value):
    value = value() if callable(value) else value          # evaluated at run time: a long suite must not age it into the tolerance
    assert timerange.publication_time(value) is None


@pytest.mark.parametrize("value,expected", [
    (datetime(2008, 3, 1, tzinfo=UTC), datetime(2008, 3, 1, tzinfo=UTC)),                 # historical
    (datetime(2005, 6, 1, tzinfo=UTC), datetime(2005, 6, 1, tzinfo=UTC)),                 # the floor itself is valid
    ("2024-01-05T00:00:00Z", datetime(2024, 1, 5, tzinfo=UTC)),
    (datetime(2024, 1, 5, 12, 0), datetime(2024, 1, 5, 12, 0, tzinfo=UTC)),               # naive = UTC (unchanged convention)
    (datetime(2024, 1, 5, 12, 0, tzinfo=timezone(timedelta(hours=5))), datetime(2024, 1, 5, 7, 0, tzinfo=UTC)),
    (1_700_000_000, datetime.fromtimestamp(1_700_000_000, tz=UTC)),                        # epoch seconds
])
def test_valid_historical_values_are_kept(value, expected):
    assert timerange.publication_time(value) == expected


def test_recent_values_and_small_clock_skew_are_kept():
    for v in (NOW - D(hours=2), NOW - D(seconds=1), NOW + D(minutes=5)):
        assert timerange.publication_time(v) is not None
    assert timerange.publication_time(NOW + D(minutes=5)) == NOW + D(minutes=5)           # not clamped, not rewritten


def test_the_reference_instant_can_be_supplied():
    ref = datetime(2024, 6, 1, tzinfo=UTC)
    assert timerange.publication_time(datetime(2024, 6, 1, 0, 5, tzinfo=UTC), now=ref) is not None
    assert timerange.publication_time(datetime(2024, 6, 1, 1, 0, tzinfo=UTC), now=ref) is None


def test_to_utc_is_still_a_plain_parser_for_range_endpoints():
    assert timerange.to_utc(datetime(1970, 1, 1)) == datetime(1970, 1, 1, tzinfo=UTC)
    assert timerange.to_utc(NOW + D(days=40)) == NOW + D(days=40)
    assert timerange.to_utc(0) is None and timerange.to_utc("junk") is None and timerange.to_utc(True) is None


# ── carrying the date onto result posts ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("created", [NOW + D(days=40), datetime(1970, 1, 1), 0, None, "junk"])
def test_a_bad_stored_date_is_never_carried_onto_a_result_post(created):
    doc = {"created_utc": created, "_id": "oid-with-a-recent-ingestion-time", "ingested_at": NOW}
    post = timerange.with_published_at({"title": "t"}, doc)
    assert "published_at" not in post and timerange.published_iso(doc) is None        # and nothing else is substituted


def test_a_valid_stored_date_is_carried_unchanged():
    when = NOW - D(days=3)
    assert timerange.with_published_at({}, {"created_utc": when})["published_at"] == when.astimezone(UTC).isoformat()
    assert timerange.published_iso({"created_utc": datetime(2008, 3, 1)}) == "2008-03-01T00:00:00+00:00"


def test_post_published_at_falls_back_to_a_valid_created_utc_only():
    valid = NOW - D(days=2)
    assert timerange.post_published_at({"published_at": (NOW + D(days=9)).isoformat(), "created_utc": valid}) == valid
    assert timerange.post_published_at({"published_at": "junk", "created_utc": datetime(1970, 1, 1)}) is None
    assert timerange.post_published_at({"published_at": valid.isoformat()}) == valid


def test_an_explicit_range_keeps_valid_dates_and_counts_bad_ones_as_undated():
    rng = timerange.TimeRange(start=NOW - D(days=7), end=NOW + D(minutes=1), tz_name="UTC", kind="x", phrase="x")
    posts = [{"id": "ok", "created_utc": NOW - D(days=1)}, {"id": "future", "created_utc": NOW + D(days=5)},
             {"id": "zero", "created_utc": datetime(1970, 1, 1)}, {"id": "none"}]
    kept, stats = timerange.filter_posts(posts, rng)
    assert [p["id"] for p in kept] == ["ok"]
    assert stats == {"kept": 1, "outside": 0, "no_timestamp": 3}


# ── recency scoring ─────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", [NOW + D(days=30), NOW + D(minutes=30), datetime(1970, 1, 1, tzinfo=UTC),
                                   datetime(1999, 1, 1), "1970-01-01T00:00:00Z", "junk", None, 0, ""])
def test_future_epoch_zero_invalid_and_missing_dates_all_get_the_missing_date_score(value):
    assert opportunity.recency_score(value, CFG) == 0.35


def test_a_future_date_is_never_scored_as_fresh():
    assert opportunity.recency_score(NOW + D(days=30), CFG) < opportunity.recency_score(NOW - D(hours=1), CFG)


def test_valid_recent_and_historical_dates_keep_their_decay():
    assert opportunity.recency_score(NOW, CFG) == pytest.approx(1.0, abs=1e-3)
    assert opportunity.recency_score(NOW + D(minutes=5), CFG) == pytest.approx(1.0, abs=1e-3)      # skew counts as "now"
    assert opportunity.recency_score(NOW - D(days=30), CFG) == pytest.approx(0.5, abs=1e-3)
    assert opportunity.recency_score(NOW - D(days=60), CFG) == pytest.approx(0.25, abs=1e-3)
    assert opportunity.recency_score(datetime(2008, 3, 1, tzinfo=UTC), CFG) == pytest.approx(0.0, abs=1e-6)
    assert opportunity.recency_score(NOW - D(days=30), dict(CFG, missing_date_score=0.9)) == pytest.approx(0.5, abs=1e-3)


# ── retrieval: logics.get_matched_signals ───────────────────────────────────────────────────────────────────────────
class RecordingSignals(FakeSignals):
    """FakeSignals that also enforces the upper bound it is sent, and remembers the queries."""
    def __init__(self, docs):
        super().__init__(docs)
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(query)
        cur = super().find(query, projection)
        ranges = []

        def walk(q):
            if isinstance(q, dict):
                for k, v in q.items():
                    if k == "$and":
                        [walk(s) for s in v]
                    elif k == "created_utc" and isinstance(v, dict) and "$lt" in v:
                        ranges.append(v["$lt"])
        walk(query)
        docs = [d for d in cur._d if not ranges or (isinstance(d.get("created_utc"), datetime) and d["created_utc"] < ranges[0])]
        cur._d = docs
        return cur


def _retrieve(lg, monkeypatch, docs, since_days=None, record_only=False):
    h = Harness(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False)
    coll = RecordingSignals(docs) if not record_only else FakeSignals(docs)     # FakeSignals ignores "$lt": python guard only
    monkeypatch.setattr(lg, "signals_collection", coll)
    out = lg.get_matched_signals("t", ["ai agents"], "all", limit=10, since_days=since_days,
                                 match_phrases=["need an ai agent"], user_query="find ai agent buyers")
    return {p["title"]: p for p in out}, coll


def _docs():
    ins = NOW - D(days=1)
    return [_mkdoc(1, 0.90, inserted=ins, created=NOW - D(days=1)),                   # valid recent
            _mkdoc(2, 0.90, inserted=ins, created=NOW + D(days=40)),                  # future
            _mkdoc(3, 0.90, inserted=ins, created=datetime(1970, 1, 1, tzinfo=UTC)),  # epoch zero
            _mkdoc(4, 0.90, inserted=ins, created=NOW + D(minutes=5)),                # within clock skew
            _mkdoc(5, 0.90, inserted=ins, created=datetime(2008, 3, 1, tzinfo=UTC))]    # valid historical


def test_a_last_n_days_window_never_admits_a_future_dated_post(lg, monkeypatch):
    got, coll = _retrieve(lg, monkeypatch, _docs(), since_days=7)
    assert set(got) == {"post 1", "post 4"}                                           # recent + skew only
    assert got["post 1"]["published_at"] and "post 2" not in got and "post 3" not in got
    assert any("$lt" in str(q) for q in coll.queries)                                 # the bound is in the Mongo query itself


def test_the_python_side_guard_holds_even_if_the_query_bound_were_ignored(lg, monkeypatch):
    got, _ = _retrieve(lg, monkeypatch, _docs(), since_days=7, record_only=True)
    assert set(got) == {"post 1", "post 4"}


def test_without_a_window_bad_dates_are_returned_but_carry_no_date(lg, monkeypatch):
    got, _ = _retrieve(lg, monkeypatch, _docs(), since_days=None)
    assert set(got) == {"post 1", "post 2", "post 3", "post 4", "post 5"}             # not silently dropped ...
    assert "published_at" not in got["post 2"] and "published_at" not in got["post 3"]  # ... but never shown as a date
    assert got["post 5"]["published_at"].startswith("2008-03-01")                     # legitimate history untouched
    assert got["post 1"]["published_at"] and got["post 4"]["published_at"]


def test_a_missing_date_is_excluded_by_a_window_and_included_without_one(lg, monkeypatch):
    docs = [dict(_mkdoc(6, 0.9, inserted=NOW - D(days=1)), created_utc=None)]
    assert _retrieve(lg, monkeypatch, docs, since_days=7)[0] == {}
    assert "published_at" not in _retrieve(lg, monkeypatch, docs, since_days=None)[0]["post 6"]


# ── retrieval: the unfiltered window path in flintel.py ─────────────────────────────────────────────────────────────
class _Coll:
    def __init__(self, docs, honour_query=True):
        self.docs, self.honour, self.queries = docs, honour_query, []

    def find(self, query, projection=None):
        self.queries.append(query)
        c = query.get("created_utc", {})
        sel = [d for d in self.docs if not self.honour or (d["created_utc"] >= c["$gte"] and d["created_utc"] < c["$lt"])]

        class Cur(list):
            def sort(s, *a, **k): return s
            def limit(s, n): return Cur(s[:n])
        return Cur(dict(d) for d in sel)


def _flintel_docs():
    base = {"post_text": "body", "platform": "reddit", "subreddit": "t"}
    return [dict(base, title="recent", post_url="https://www.reddit.com/r/t/comments/1/a/", created_utc=NOW - D(days=1)),
            dict(base, title="future", post_url="https://www.reddit.com/r/t/comments/2/b/", created_utc=NOW + D(days=40)),
            dict(base, title="skew", post_url="https://www.reddit.com/r/t/comments/3/c/", created_utc=NOW + D(minutes=5))]


@pytest.mark.parametrize("honour_query", [True, False])
def test_the_unfiltered_window_path_excludes_future_posts(honour_query):
    import importlib.util
    spec = importlib.util.spec_from_file_location("flintel_under_test", ROOT / "flintel.py")
    flintel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(flintel)                       # the real module (the `lg` fixture stubs the name "flintel")
    coll = _Coll(_flintel_docs(), honour_query)
    out = flintel.get_unfiltered_matched_signals(coll, 7, "all", limit=10)
    assert {p["title"] for p in out} == {"recent", "skew"}
    assert "$lt" in coll.queries[0]["created_utc"]
