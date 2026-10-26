"""
tests/test_freshness_disclosure.py — LI-02 Milestone 2: freshness, coverage and cache transparency.

What is covered
  * summarize_publication_times: newest / oldest / counts against values worked out BY HAND in each test, for
    missing, unreadable, naive, aware, future, duplicate and discovery-only posts
  * get_evidence_with_topup provenance: saved results vs a search that ran for THIS request, top-up, a time
    range, a cache that does not match, an incomplete scan, a scan that never reported its status
  * the wording of build_data_freshness for normal, empty, cached, partial and unknown-date results
  * the feature flag (default OFF in code, nothing in config.py): off = no provenance, answer text untouched
  * golden regression: result membership, ORDER and the cache document are identical with and without it
  * call sites: index.py (saved answer), routes.py (the provenance argument)
  * browser rendering in chat.html and index.html

No network, no database, no model: collections are in-memory fakes, the model is a stub.
"""
import json
import math
import re
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_strict_intent import Harness, _mkdoc, _set_cfg, lg  # noqa: E402,F401
from tests.test_answer_persistence import env  # noqa: E402,F401  (fixture: lg, idx, chats)

UTC = timezone.utc
REF = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
FLAG = "FRESHNESS_DISCLOSURE_ENABLED"

# Everything the statement must never say. "complete" is only allowed inside "not a complete view of Reddit".
FORBIDDEN = re.compile(
    r"real[- ]?time|\blive\b|up[- ]to[- ]date|\blatest\b|\bfresh|just now|minute-level|new collection|nothing new|"
    r"all posts on reddit|\bverified\b|original publication|\bcompletely\b|\bevery post\b", re.I)
WINDOW_PHRASE = "all posts Flintel has collected so far (no time limit)"


@pytest.fixture(autouse=True)
def _isolated_scan_status(request):
    """The scan-status registry is process-wide; start every test from an empty one."""
    if "lg" in request.fixturenames:
        request.getfixturevalue("lg")._SCAN_STATUS.clear()
    yield


def _on(monkeypatch, value=True):
    _set_cfg(monkeypatch, **{FLAG: value})


def iso(*a):
    return datetime(*a, tzinfo=UTC).isoformat()


def _p(n, date="__none__", **extra):
    post = {"title": f"t{n}", "post_text": "x", "post_url": f"https://www.reddit.com/r/t/comments/{n}/p/",
            "platform": "reddit"}
    if date != "__none__":
        post["published_at"] = date
    post.update(extra)
    return post


# ═════════════════════════════════════════════════════════════════════════
# A. publication-time summary
# ═════════════════════════════════════════════════════════════════════════

def test_summary_matches_independently_worked_out_values(lg):
    posts = [
        _p(1, iso(2026, 10, 25, 8, 0)),                         # newest valid: 28 h before REF
        _p(2, iso(2026, 10, 20, 0, 0)),
        _p(3, iso(2026, 9, 1, 0, 0)),                           # oldest valid: 55 d 12 h before REF
        _p(4),                                                   # no date
        _p(5, "not a date"),                                     # unreadable
        _p(6, iso(2026, 10, 26, 12, 0, 1)),                      # one second AFTER the reference
        _p(7, "2026-10-26T12:00:00"),                            # naive, read as UTC; exactly REF - 0 -> not future
        _p(8, iso(2026, 10, 24, 12, 0), google_rank=3),          # discovery-only link: never counted
        _p(2, iso(2026, 1, 1, 0, 0)),                            # repeated URL of post 2: counted once
    ]
    s = lg.summarize_publication_times(posts, REF)
    assert s["evidence_posts"] == 7                              # 1,2,3,4,5,6,7
    assert s["with_date"] == 4                                   # 1,2,3,7
    assert (s["no_date"], s["invalid_date"], s["future_date"]) == (1, 1, 1)
    assert s["discovery_only"] == 1
    assert s["naive_assumed_utc"] == 1
    assert s["newest"] == iso(2026, 10, 26, 12, 0)               # the naive one, read as UTC, equals REF
    assert s["oldest"] == iso(2026, 9, 1, 0, 0)
    assert s["newest_age_seconds"] == 0
    assert s["oldest_age_seconds"] == 55 * 86400 + 12 * 3600 == 4795200                      # 55 d 12 h, worked out by hand
    assert s["with_date"] + s["no_date"] + s["invalid_date"] + s["future_date"] == s["evidence_posts"]


def test_future_dates_never_become_newest_or_oldest(lg):
    s = lg.summarize_publication_times([_p(1, iso(2027, 1, 1)), _p(2, iso(2026, 10, 1))], REF)
    assert s["future_date"] == 1 and s["with_date"] == 1
    assert s["newest"] == s["oldest"] == iso(2026, 10, 1)


def test_exactly_at_the_reference_is_not_future_and_one_second_after_is(lg):
    at = lg.summarize_publication_times([_p(1, REF.isoformat())], REF)
    after = lg.summarize_publication_times([_p(1, (REF + timedelta(seconds=1)).isoformat())], REF)
    assert (at["with_date"], at["future_date"]) == (1, 0)
    assert (after["with_date"], after["future_date"]) == (0, 1) and after["newest"] is None


@pytest.mark.parametrize("raw,bucket", [
    (None, "no_date"), ("", "no_date"),
    ("garbage", "invalid_date"), ("2026-13-45T00:00:00Z", "invalid_date"), ("   ", "no_date"),
    (True, "invalid_date"), (False, "invalid_date"), (float("nan"), "invalid_date"), (float("inf"), "invalid_date"),
    (-5, "invalid_date"), (0, "invalid_date"),
    (1.7e12, "invalid_date"),                                    # epoch MILLISECONDS are rejected, not misread
    ([], "invalid_date"), ({"a": 1}, "invalid_date"),
    (1761480000, "with_date"),                                   # epoch seconds: 2025-10-26 12:00 UTC
    ("2026-10-25T10:00:00Z", "with_date"),
    ("2026-10-25T10:00:00+05:00", "with_date"),
    ("2026-10-25 10:00:00", "with_date"),
    (datetime(2026, 10, 25, 10, 0), "with_date"),                # naive datetime
    (datetime(2026, 10, 25, 10, 0, tzinfo=UTC), "with_date"),
    (datetime(2099, 1, 1, tzinfo=UTC), "future_date"),
    (4102444800, "future_date"),                                 # epoch seconds for 2100-01-01
])
def test_each_kind_of_date_lands_in_exactly_one_bucket(lg, raw, bucket):
    s = lg.summarize_publication_times([_p(1, raw)], REF)
    counted = {k: s[k] for k in ("with_date", "no_date", "invalid_date", "future_date")}
    assert counted == {k: int(k == bucket) for k in counted}, (raw, counted)
    assert s["evidence_posts"] == 1


def test_timezones_are_converted_to_utc(lg):
    plus5 = lg.summarize_publication_times([_p(1, "2026-10-25T10:00:00+05:00")], REF)
    minus8 = lg.summarize_publication_times([_p(1, "2026-10-25T10:00:00-08:00")], REF)
    assert plus5["newest"] == iso(2026, 10, 25, 5, 0)
    assert minus8["newest"] == iso(2026, 10, 25, 18, 0)
    assert plus5["naive_assumed_utc"] == minus8["naive_assumed_utc"] == 0


def test_naive_values_are_read_as_utc_and_counted(lg):
    s = lg.summarize_publication_times([_p(1, datetime(2026, 10, 25, 10, 0)), _p(2, "2026-10-25T09:00:00")], REF)
    assert s["newest"] == iso(2026, 10, 25, 10, 0) and s["oldest"] == iso(2026, 10, 25, 9, 0)
    assert s["naive_assumed_utc"] == 2


def test_created_utc_is_used_only_when_published_at_is_absent_or_unusable(lg):
    only_created = {"post_url": "u1", "created_utc": datetime(2026, 10, 1, tzinfo=UTC)}
    bad_pub_good_created = {"post_url": "u2", "published_at": "junk", "created_utc": datetime(2026, 10, 2, tzinfo=UTC)}
    both = {"post_url": "u3", "published_at": iso(2026, 10, 3), "created_utc": datetime(2026, 1, 1, tzinfo=UTC)}
    s = lg.summarize_publication_times([only_created, bad_pub_good_created, both], REF)
    assert s["with_date"] == 3 and s["invalid_date"] == 0
    assert s["oldest"] == iso(2026, 10, 1) and s["newest"] == iso(2026, 10, 3)


def test_discovery_stubs_and_duplicate_urls_are_not_evidence_posts(lg):
    stub_none = _p(1, iso(2026, 10, 1), google_rank=None)        # key present with None still means "stub"
    stub = _p(2, iso(2026, 10, 2), google_rank=4)
    s = lg.summarize_publication_times([stub_none, stub, _p(3, iso(2026, 10, 3)), _p(3, iso(2026, 10, 4))], REF)
    assert s["evidence_posts"] == 1 and s["discovery_only"] == 2
    assert s["newest"] == s["oldest"] == iso(2026, 10, 3)


def test_posts_without_a_url_are_each_counted(lg):
    s = lg.summarize_publication_times([{"published_at": iso(2026, 10, 1)}, {"published_at": iso(2026, 10, 2)}], REF)
    assert s["evidence_posts"] == 2 and s["with_date"] == 2


def test_no_valid_dates_gives_no_newest_or_oldest(lg):
    s = lg.summarize_publication_times([_p(1), _p(2, "junk"), _p(3, iso(2030, 1, 1))], REF)
    assert s["newest"] is s["oldest"] is None
    assert s["newest_age_seconds"] is s["oldest_age_seconds"] is None


@pytest.mark.parametrize("posts", [None, [], [None, 3, "x", [], {}], [{"published_at": object()}]])
def test_unusable_input_never_raises(lg, posts):
    s = lg.summarize_publication_times(posts, REF)
    assert s["with_date"] == 0 and s["newest"] is None


def test_extreme_aware_datetime_does_not_raise(lg):
    edge = datetime.min.replace(tzinfo=timezone(timedelta(hours=5)))
    s = lg.summarize_publication_times([_p(1, edge)], REF)
    assert s["invalid_date"] == 1


def test_a_missing_or_naive_reference_still_works(lg):
    assert lg.summarize_publication_times([_p(1, iso(2020, 1, 1))], None)["with_date"] == 1
    assert lg.summarize_publication_times([_p(1, iso(2020, 1, 1))], datetime(2026, 10, 26, 12, 0))["with_date"] == 1


# ═════════════════════════════════════════════════════════════════════════
# B. provenance recorded by get_evidence_with_topup
# ═════════════════════════════════════════════════════════════════════════

NOW = datetime.now(UTC)
OLD = NOW - timedelta(hours=3)


def _docs():
    return [_mkdoc(i, s, inserted=OLD - timedelta(minutes=10 * i))
            for i, s in enumerate([0.95, 0.90, 0.80, 0.70, 0.60, 0.50], start=1)]


def _poll(h, prov, need=50, chat="chat1", topic="topic1", **kw):
    return h.lg.get_evidence_with_topup(
        chat, "owner", topic, ["ai agents"], need, h.lg.get_matched_signals,
        match_phrases=["need an ai agent"], user_query="find ai agent buyers", provenance_out=prov, **kw)


def test_empty_cache_is_a_search_and_not_saved_results(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _docs())
    prov = {}
    posts = _poll(h, prov)
    assert len(posts) == 6 and h.coll.calls >= 1
    assert prov["scan_count"] == 1 and not prov.get("saved_used")
    assert "scan_incomplete" not in prov and "scan_unconfirmed" not in prov
    assert prov["search_time"] >= NOW


def test_full_cache_is_reused_and_no_search_runs(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _docs())
    _poll(h, {})                                                 # request 1 fills the cache
    calls_before = h.coll.calls
    updated = h.cache.docs[("chat1", "topic1")]["updated_at"]
    prov = {}
    posts = _poll(h, prov, need=3)                               # request 2 needs only 3: the cache is enough
    assert len(posts) == 6
    assert h.coll.calls == calls_before                          # no scan at all
    assert not prov.get("scan_count") and "search_time" not in prov
    assert prov["saved_used"] is True and prov["saved_count"] == 6
    assert prov["saved_updated_at"] == updated


def test_short_cache_is_extended_by_a_search(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _docs())
    _poll(h, {})
    saved_before = h.cache.docs[("chat1", "topic1")]["updated_at"]     # the cache's update time BEFORE this request
    assert isinstance(saved_before, datetime)
    calls_before = h.coll.calls
    prov = {}
    _poll(h, prov, need=50)                                      # 6 cached < 50 wanted -> top-up scan
    assert h.coll.calls > calls_before
    assert prov["scan_count"] == 1 and prov["saved_used"] is True and prov["saved_count"] == 6
    assert prov["saved_updated_at"] == saved_before              # exactly the earlier update time, not this search's
    assert prov["search_time"] >= saved_before                   # and the two times are separate facts


def test_a_cache_hit_after_this_requests_own_search_does_not_become_saved_results(lg, monkeypatch):
    """Polling: poll 1 searches (and fills the cache), poll 2 is served from that cache. Both belong to ONE request."""
    h = Harness(lg, monkeypatch, _docs())
    prov = {}
    _poll(h, prov)
    _poll(h, prov, need=3)
    assert prov["scan_count"] == 1
    assert not prov.get("saved_used")
    assert lg.build_data_freshness([], REF, provenance=prov)["mode"] == "scanned"


def test_time_range_bypasses_the_cache_and_records_the_filter_counts(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _docs())
    _poll(h, {})                                                 # a cache exists, and must not be used
    tr = lg.timerange.TimeRange(start=NOW - timedelta(hours=3, minutes=25), end=NOW - timedelta(hours=3, minutes=5),
                                tz_name="UTC", kind="rolling", phrase="x")
    prov = {}
    posts = _poll(h, prov, time_range=tr)
    assert prov["scan_count"] == 1 and not prov.get("saved_used")
    assert prov["range"]["kept"] == len(posts)
    assert set(prov["range"]) == {"kept", "outside", "no_timestamp"}
    assert lg.build_data_freshness(posts, REF, provenance=prov, time_range=tr)["mode"] == "range_scan"


def test_a_cache_for_a_different_search_is_not_reported_as_saved_results(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _docs())
    h.cache.docs[("chat1", "topic1")] = {"chat_id": "chat1", "topic_key": "topic1", "keywords": ["something else"],
                                         "match_phrases": ["other"], "updated_at": NOW,
                                         "posts": [{"post_url": "x"}] * 80}
    prov = {}
    _poll(h, prov)
    assert prov["scan_count"] == 1 and not prov.get("saved_used")


def test_a_matcher_that_reports_an_incomplete_scan_is_recorded(lg, monkeypatch):
    h = Harness(lg, monkeypatch, [])

    def matcher(topic_key, keywords, **kw):
        lg._register_scan_status(kw["chat_id"], topic_key, False)
        return []
    prov = {}
    lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, matcher, provenance_out=prov)
    assert prov["scan_count"] == 1 and prov["scan_incomplete"] is True and "scan_unconfirmed" not in prov


def test_a_matcher_that_never_reports_its_status_is_unconfirmed_even_with_a_stale_entry(lg, monkeypatch):
    Harness(lg, monkeypatch, [])
    lg._register_scan_status("c", "t", True)                    # left over from an EARLIER request

    def matcher(topic_key, keywords, **kw):
        return []
    prov = {}
    lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, matcher, provenance_out=prov)
    assert prov["scan_unconfirmed"] is True and "scan_incomplete" not in prov


def test_a_matcher_that_reports_complete_is_not_flagged(lg, monkeypatch):
    Harness(lg, monkeypatch, [])

    def matcher(topic_key, keywords, **kw):
        lg._register_scan_status(kw["chat_id"], topic_key, True)
        return []
    prov = {}
    lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, matcher, provenance_out=prov)
    assert prov["scan_count"] == 1 and not prov.get("scan_incomplete") and not prov.get("scan_unconfirmed")


def test_a_search_that_raised_is_not_recorded_as_having_run(lg, monkeypatch):
    Harness(lg, monkeypatch, [])

    def matcher(topic_key, keywords, **kw):
        raise RuntimeError("boom")
    prov = {}
    with pytest.raises(RuntimeError):
        lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, matcher, provenance_out=prov)
    assert prov == {}


def test_provenance_writes_never_break_the_search(lg, monkeypatch):
    Harness(lg, monkeypatch, [])

    class Hostile(dict):
        def get(self, *a, **k):
            raise RuntimeError("no")

        def update(self, *a, **k):
            raise RuntimeError("no")

        def __setitem__(self, k, v):
            raise RuntimeError("no")
    out = lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, lambda tk, kw, **k: [{"post_url": "u"}],
                                     provenance_out=Hostile())
    assert out == [{"post_url": "u"}]


# ═════════════════════════════════════════════════════════════════════════
# C. wording
# ═════════════════════════════════════════════════════════════════════════

SEARCH_AT = datetime(2026, 10, 26, 11, 58, tzinfo=UTC)
SAVED_AT = datetime(2026, 10, 24, 18, 2, tzinfo=UTC)
THREE = [_p(1, iso(2026, 10, 25, 8, 0)), _p(2, iso(2026, 10, 20, 0, 0)), _p(3, iso(2026, 9, 1, 0, 0))]


def _fd(lg, posts=THREE, **kw):
    return lg.build_data_freshness(posts, REF, **kw)


def test_scanned_statement_exact(lg):
    f = _fd(lg, provenance={"scan_count": 1, "search_time": SEARCH_AT})
    assert f["statement"] == (
        "Stored posts were searched on 26 Oct 2026, 11:58 UTC. 3 stored posts were considered. "
        "Stored post dates: newest 25 Oct 2026 (about 28 hours before this answer), oldest 1 Sep 2026. "
        f"Window: {WINDOW_PHRASE}. Collection time is unknown, and this is not a complete view of Reddit.")
    assert f["mode"] == "scanned" and f["searched_this_request"] is True and f["partial"] is None
    assert f["collection_time"] == "unknown" and f["post_dates"]["basis"] == "stored_post_date"
    assert f["counts"]["evidence_posts"] == 3 and f["post_dates"]["newest"] == iso(2026, 10, 25, 8, 0)


def test_saved_statement_exact_and_it_never_claims_a_search(lg):
    f = _fd(lg, provenance={"saved_used": True, "saved_updated_at": SAVED_AT})
    assert f["statement"] == (
        "These results were reused from an earlier search in this chat (saved results last updated 24 Oct 2026, 18:02 UTC). "
        "No new search of stored posts was run for this answer, so posts stored since then are not included. "
        "3 stored posts were considered. "
        "Stored post dates: newest 25 Oct 2026 (about 28 hours before this answer), oldest 1 Sep 2026. "
        f"Window: {WINDOW_PHRASE}. Collection time is unknown, and this is not a complete view of Reddit.")
    assert f["mode"] == "saved_only" and f["searched_this_request"] is False and f["search_time_utc"] is None
    assert not re.search(r"\bsearched\b|\bsearching\b", f["statement"])
    assert f["saved_results_updated_at"] == SAVED_AT.isoformat()


def test_saved_plus_search_keeps_the_four_times_apart(lg):
    f = _fd(lg, provenance={"scan_count": 1, "search_time": SEARCH_AT, "saved_used": True, "saved_updated_at": SAVED_AT})
    s = f["statement"]
    assert f["mode"] == "saved_plus_scan"
    opening = s.split("Stored post dates")[0]
    assert "(last updated 24 Oct 2026, 18:02 UTC)" in opening               # saved-results update time, in the opening
    assert "searching stored posts again on 26 Oct 2026, 11:58 UTC" in opening     # search activity, in the opening
    assert opening.index("last updated") < opening.index("searching stored posts again")
    assert "24 Oct 2026, 18:02 UTC" not in opening.split("searching stored posts again")[1]   # not reused as a search time
    assert "26 Oct 2026, 11:58 UTC" not in opening.split("searching stored posts again")[0]   # nor the search time as an update time
    assert "Stored post dates: newest 25 Oct 2026" in s                     # stored post date
    assert "Collection time is unknown" in s                                # collection time
    assert f["saved_results_updated_at"] != f["search_time_utc"]


def test_saved_results_with_an_unknown_update_time_say_so(lg):
    f = _fd(lg, provenance={"saved_used": True, "saved_updated_at": None})
    assert "(saved results update time unknown)" in f["statement"] and f["saved_results_updated_at"] is None


@pytest.mark.parametrize("bad", ["junk", 12, object(), datetime(2026, 10, 24, 18, 2)])
def test_odd_saved_update_times_are_handled(lg, bad):
    f = _fd(lg, provenance={"saved_used": True, "saved_updated_at": bad})
    assert f["mode"] == "saved_only" and "saved results" in f["statement"]


def test_no_provenance_says_the_gathering_is_not_recorded(lg):
    for prov in (None, {}, "x", []):
        f = _fd(lg, provenance=prov)
        assert f["mode"] == "unknown" and f["searched_this_request"] is False
        assert f["statement"].startswith("How these results were gathered is not recorded for this answer.")
        assert not re.search(r"\bsearched\b|\bsearching\b|reused", f["statement"])


def test_range_statement_names_the_range_and_what_it_left_out(lg):
    tr = lg.timerange.TimeRange(start=datetime(2026, 10, 25, tzinfo=UTC), end=datetime(2026, 10, 26, tzinfo=UTC),
                                tz_name="UTC", kind="day", phrase="yesterday")
    prov = {"scan_count": 1, "search_time": SEARCH_AT, "range": {"kept": 3, "outside": 4, "no_timestamp": 2}}
    f = _fd(lg, provenance=prov, time_range=tr)
    assert f["mode"] == "range_scan"
    assert f"limited to {tr.describe()} by stored post date" in f["statement"]
    assert "Left out by date: 4 outside the range and 2 without a stored date." in f["statement"]
    assert "Window:" not in f["statement"]                                  # stated once, not twice


def test_a_days_window_is_described(lg):
    f = _fd(lg, provenance={"scan_count": 1, "search_time": SEARCH_AT}, time_window_days=7)
    assert "Window: the last 7 days." in f["statement"] and f["window"] == "the last 7 days"
    assert _fd(lg, provenance={}, time_window_days=1)["window"] == "the last 1 day"


def test_window_text_matches_the_existing_no_results_wording(lg):
    tr = lg.timerange.TimeRange(start=datetime(2026, 10, 25, tzinfo=UTC), end=datetime(2026, 10, 26, tzinfo=UTC),
                                tz_name="UTC", kind="day", phrase="yesterday")
    for args in ({"time_range": tr}, {"time_window_days": 30}, {}):
        raw = json.dumps({"format": "no_results", "message": "m"})
        out = json.loads(lg.enrich_no_results_answer(raw, "q", "reddit", args.get("time_range"),
                                                    args.get("time_window_days"), None))
        assert out["searched"]["time_window"] == lg.describe_search_window(args.get("time_range"),
                                                                           args.get("time_window_days"))


def test_empty_result_never_claims_nothing_was_posted(lg):
    f = _fd(lg, posts=[], provenance={"scan_count": 1, "search_time": SEARCH_AT}, time_window_days=3)
    s = f["statement"]
    assert "0 stored posts were considered" in s
    assert "No stored posts matched this search." in s
    assert s.count("the last 3 days") == 1 and "Window: the last 3 days." in s       # the window is stated exactly once
    assert "very recent posts may not be stored yet" in s and "does not show that nothing was posted" in s
    assert "A temporary error can also produce an empty result." in s
    assert "Stored post dates" not in s and f["post_dates"]["newest"] is None


@pytest.mark.parametrize("prov,reason,text", [
    ({"scan_count": 1, "search_time": SEARCH_AT, "scan_incomplete": True}, "source_unreadable",
     "Partial results: one or more sources could not be read, so posts may be missing."),
    ({"scan_count": 1, "search_time": SEARCH_AT, "scan_unconfirmed": True}, "unconfirmed",
     "Because the search could not be confirmed, posts may be missing."),
])
def test_partial_and_unconfirmed_scans_are_stated(lg, prov, reason, text):
    f = _fd(lg, provenance=prov)
    assert f["partial"] == reason and text in f["statement"]


def test_a_scan_deadline_is_stated_as_partial(lg):
    f = _fd(lg, provenance={"scan_count": 1, "search_time": SEARCH_AT}, timed_out=True)
    assert f["partial"] == "time_limit"
    assert "Partial results: the search stopped at its time limit before every source was checked" in f["statement"]


def test_reused_saved_results_are_not_called_partial(lg):
    assert _fd(lg, provenance={"saved_used": True, "saved_updated_at": SAVED_AT})["partial"] is None


def test_complete_scan_status_is_not_partial(lg):
    assert _fd(lg, provenance={"scan_count": 1, "search_time": SEARCH_AT})["partial"] is None


def test_evidence_limit_is_stated_only_when_reached(lg):
    prov = {"scan_count": 1, "search_time": SEARCH_AT}
    hit = _fd(lg, provenance=prov, matched_count=25, evidence_limit=25)
    miss = _fd(lg, provenance=prov, matched_count=24, evidence_limit=25)
    none = _fd(lg, provenance=prov)
    assert hit["evidence_limit_reached"] and "Showing up to 25 matching posts; more may exist." in hit["statement"]
    assert not miss["evidence_limit_reached"] and "more may exist" not in miss["statement"]
    assert not none["evidence_limit_reached"]


def test_unknown_dates_are_stated_and_not_counted(lg):
    posts = THREE + [_p(4), _p(5, "junk"), _p(6, iso(2030, 1, 1))]
    s = _fd(lg, posts=posts, provenance={"scan_count": 1, "search_time": SEARCH_AT})["statement"]
    assert "6 stored posts were considered" in s
    assert "Not counted in the date range: 1 without a date, 1 with an unreadable date, 1 dated after this answer." in s
    assert "oldest 1 Sep 2026" in s


def test_no_usable_date_at_all(lg):
    s = _fd(lg, posts=[_p(1), _p(2, "junk")], provenance={"scan_count": 1, "search_time": SEARCH_AT})["statement"]
    assert "None of these posts has a usable stored post date, so their age cannot be stated." in s
    assert "newest" not in s and "Not counted" not in s


def test_age_phrases(lg):
    f = lg._fd_age
    assert f(0) == f(3599) == "under an hour" and f(-50) == "under an hour"
    assert f(3600) == "about 1 hour" and f(7200) == "about 2 hours" and f(2 * 86400 - 1) == "about 47 hours"
    assert f(2 * 86400) == "about 2 days" and f(59 * 86400) == "about 59 days"
    assert f(60 * 86400) == "about 2 months" and f(2 * 365 * 86400) == "about 2 years"


STATEMENT_CASES = [
    {"provenance": {"scan_count": 1, "search_time": SEARCH_AT}},
    {"provenance": {"saved_used": True, "saved_updated_at": SAVED_AT}},
    {"provenance": {"scan_count": 1, "search_time": SEARCH_AT, "saved_used": True, "saved_updated_at": SAVED_AT}},
    {"provenance": {"scan_count": 1, "search_time": SEARCH_AT, "range": {"kept": 1, "outside": 0, "no_timestamp": 0}}},
    {"provenance": {}}, {"provenance": None},
    {"provenance": {"scan_count": 1, "search_time": SEARCH_AT, "scan_incomplete": True}, "timed_out": True},
    {"provenance": {"scan_count": 1, "search_time": SEARCH_AT, "scan_unconfirmed": True},
     "matched_count": 9, "evidence_limit": 9},
]


@pytest.mark.parametrize("posts", [THREE, [], [_p(1)], [_p(1, "junk"), _p(2, iso(2031, 1, 1))]])
@pytest.mark.parametrize("kw", STATEMENT_CASES)
def test_no_statement_makes_a_forbidden_claim(lg, posts, kw):
    s = _fd(lg, posts=posts, **kw)["statement"]
    assert not FORBIDDEN.search(s), s
    assert "collected" not in s.replace(WINDOW_PHRASE, ""), s              # "collected" only describes the stored data
    assert "Collection time is unknown" in s and "not a complete view of Reddit" in s
    assert not re.search(r"\bcomplete\b", s.replace("not a complete view of Reddit", "")), s


@pytest.mark.parametrize("kw", [k for k in STATEMENT_CASES if not (k["provenance"] or {}).get("scan_count")])
def test_a_search_is_never_claimed_unless_one_ran(lg, kw):
    f = _fd(lg, **kw)
    assert f["searched_this_request"] is False and f["search_time_utc"] is None
    assert not re.search(r"\bsearched\b|\bsearching\b", f["statement"]), f["statement"]


# ═════════════════════════════════════════════════════════════════════════
# C2. corrective regression: no search terms, unconfirmed activity, window stated once
# ═════════════════════════════════════════════════════════════════════════

SEARCHED_CLAIM = re.compile(r"were searched|searched on|searching stored posts again|searched stored", re.I)
TIME_OF_DAY = re.compile(r"\d{2}:\d{2} UTC")


def _poll_terms(h, prov, keywords, phrases=None, **kw):
    return h.lg.get_evidence_with_topup("chat1", "owner", "topic1", keywords, 50, h.lg.get_matched_signals,
                                        match_phrases=phrases, user_query="find ai agent buyers",
                                        provenance_out=prov, **kw)


@pytest.mark.parametrize("keywords,phrases", [([], None), ([], []), ([""], None), ([None, ""], [""]), (None, None)])
def test_no_search_terms_is_recorded_as_not_searched(lg, monkeypatch, keywords, phrases):
    h = Harness(lg, monkeypatch, _docs())
    prov = {}
    assert _poll_terms(h, prov, keywords, phrases) == []
    assert h.coll.calls == 0                                      # the collection really was not read
    assert prov == {"scan_skipped": True}                         # no scan_count, no search time, no status flags
    f = lg.build_data_freshness([], REF, provenance=prov)
    assert f["mode"] == "no_search_run" and f["search_status"] == "not_run"
    assert f["searched_this_request"] is False and f["search_time_utc"] is None
    assert f["statement"].startswith(
        "No search of stored posts was run for this answer because the request had no search terms. "
        "0 stored posts were considered.")
    assert not SEARCHED_CLAIM.search(f["statement"]) and not TIME_OF_DAY.search(f["statement"])
    assert "No stored posts matched" not in f["statement"]        # nothing was matched, because nothing was searched
    assert "does not show that nothing was posted" in f["statement"]
    assert f["partial"] is None


def test_a_time_range_with_no_search_terms_is_also_not_a_search(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _docs())
    tr = lg.timerange.TimeRange(start=NOW - timedelta(hours=4), end=NOW, tz_name="UTC", kind="rolling", phrase="x")
    prov = {}
    _poll_terms(h, prov, [], None, time_range=tr)
    assert prov == {"scan_skipped": True} and "range" not in prov and h.coll.calls == 0


def test_an_unfiltered_query_without_terms_is_still_a_search(lg, monkeypatch):
    Harness(lg, monkeypatch, [])

    def matcher(topic_key, keywords, **kw):
        lg._register_scan_status(kw["chat_id"], topic_key, True)       # what the unfiltered branch does
        return []
    prov = {}
    lg.get_evidence_with_topup("c", "o", "t", [], 5, matcher, unfiltered=True, provenance_out=prov)
    assert prov["scan_count"] == 1 and "scan_skipped" not in prov and "scan_unconfirmed" not in prov


def test_odd_keyword_values_never_break_the_search_with_or_without_tracking(lg, monkeypatch):
    Harness(lg, monkeypatch, [])
    for kw in (5, object(), 3.5):
        for prov in (None, {}):
            out = lg.get_evidence_with_topup("c", "o", "t", kw, 5, lambda tk, k, **x: [{"post_url": "u"}], provenance_out=prov)
            assert out == [{"post_url": "u"}]
    p = {}
    lg.get_evidence_with_topup("c", "o", "t", 5, 5, lambda tk, k, **x: [], provenance_out=p)
    assert p.get("scan_count") == 1 and "scan_skipped" not in p          # unreadable terms: never claim "no search"


def test_search_terms_given_as_phrases_only_still_count_as_a_search(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _docs())
    prov = {}
    _poll_terms(h, prov, [], ["need an ai agent"])
    assert prov["scan_count"] == 1 and "scan_skipped" not in prov and h.coll.calls >= 1


def test_no_search_terms_with_cached_results_still_reports_the_cache_not_a_search(lg):
    prov = {"saved_used": True, "saved_updated_at": SAVED_AT, "scan_skipped": True}
    f = _fd(lg, provenance=prov)
    assert f["mode"] == "saved_only" and f["search_status"] == "not_run"
    assert "No new search of stored posts was run for this answer" in f["statement"]
    assert not SEARCHED_CLAIM.search(f["statement"])


def test_a_recorded_search_is_never_overridden_by_a_skip_flag(lg):
    """If a search was recorded for this request, a stray "skipped" flag must not turn it into "no search was run"."""
    f = _fd(lg, provenance={"scan_count": 1, "search_time": SEARCH_AT, "scan_skipped": True})
    assert f["mode"] == "scanned" and f["search_status"] == "confirmed"
    assert f["statement"].startswith("Stored posts were searched on 26 Oct 2026, 11:58 UTC.")


UNCONFIRMED = {"scan_count": 1, "search_time": SEARCH_AT, "scan_unconfirmed": True}
NO_TIME = "could not be confirmed, so no search time is given"


def test_an_unconfirmed_search_is_never_stated_as_done_and_gets_no_time(lg):
    f = _fd(lg, provenance=UNCONFIRMED)
    s = f["statement"]
    assert s.startswith(f"A search of stored posts was attempted for this answer but {NO_TIME}. 3 stored posts were considered.")
    assert not SEARCHED_CLAIM.search(s) and "11:58" not in s and not TIME_OF_DAY.search(s)
    assert f["search_status"] == "unconfirmed" and f["searched_this_request"] is False
    assert f["search_time_utc"] is None and f["partial"] == "unconfirmed"
    assert "Because the search could not be confirmed, posts may be missing." in s
    assert "Whether every source was read" not in s                # one consistent explanation, not two


def test_an_unconfirmed_search_over_saved_results_keeps_the_update_time_and_drops_the_search_time(lg):
    f = _fd(lg, provenance=dict(UNCONFIRMED, saved_used=True, saved_updated_at=SAVED_AT))
    s = f["statement"]
    assert f["mode"] == "saved_plus_scan" and f["search_status"] == "unconfirmed"
    assert s.startswith("Saved results from an earlier search in this chat (last updated 24 Oct 2026, 18:02 UTC) were used. "
                        f"A search of stored posts was attempted for this answer but {NO_TIME}, and it is not known "
                        "whether it added posts. 3 stored posts were considered.")
    assert "extended" not in s and "combined" not in s
    assert not SEARCHED_CLAIM.search(s) and "11:58" not in s and s.count("UTC") == 1     # only the update time is shown


def test_an_unconfirmed_range_search_is_worded_as_attempted(lg):
    tr = lg.timerange.TimeRange(start=datetime(2026, 10, 25, tzinfo=UTC), end=datetime(2026, 10, 26, tzinfo=UTC),
                                tz_name="UTC", kind="day", phrase="yesterday")
    prov = dict(UNCONFIRMED, range={"kept": 3, "outside": 1, "no_timestamp": 0})
    f = _fd(lg, provenance=prov, time_range=tr)
    s = f["statement"]
    assert f["mode"] == "range_scan" and f["search_status"] == "unconfirmed"
    assert s.startswith(f"A search of stored posts limited to {tr.describe()} by stored post date was attempted but {NO_TIME}.")
    assert not SEARCHED_CLAIM.search(s) and not TIME_OF_DAY.search(s)
    assert "Left out by date: 1 outside the range" in s


def test_an_unconfirmed_empty_result_does_not_claim_a_search_matched_nothing(lg):
    f = _fd(lg, posts=[], provenance=UNCONFIRMED)
    s = f["statement"]
    assert "No stored posts are available for this answer." in s and "No stored posts matched this search" not in s
    assert not SEARCHED_CLAIM.search(s) and "does not show that nothing was posted" in s


def test_a_confirmed_incomplete_search_is_still_stated_as_a_search_with_its_time(lg):
    f = _fd(lg, provenance={"scan_count": 1, "search_time": SEARCH_AT, "scan_incomplete": True})
    assert f["search_status"] == "confirmed" and f["searched_this_request"] is True
    assert f["statement"].startswith("Stored posts were searched on 26 Oct 2026, 11:58 UTC.")
    assert f["partial"] == "source_unreadable"


@pytest.mark.parametrize("prov,status", [
    ({"scan_count": 1, "search_time": SEARCH_AT}, "confirmed"),
    ({"scan_count": 1, "search_time": SEARCH_AT, "scan_unconfirmed": True}, "unconfirmed"),
    ({"scan_skipped": True}, "not_run"),
    ({"saved_used": True, "saved_updated_at": SAVED_AT}, "not_run"),
    ({}, "not_recorded"), (None, "not_recorded"),
])
def test_the_search_status_field_matches_the_wording(lg, prov, status):
    f = _fd(lg, provenance=prov)
    assert f["search_status"] == status
    assert f["searched_this_request"] is (status == "confirmed")
    assert bool(SEARCHED_CLAIM.search(f["statement"])) is (status == "confirmed")
    assert (f["search_time_utc"] is not None) is (status == "confirmed")


@pytest.mark.parametrize("window_kw,phrase", [
    ({}, WINDOW_PHRASE), ({"time_window_days": 3}, "the last 3 days"), ({"time_window_days": 1}, "the last 1 day"),
])
@pytest.mark.parametrize("prov", [{"scan_count": 1, "search_time": SEARCH_AT}, UNCONFIRMED, {"scan_skipped": True}, {}])
def test_an_empty_result_states_the_window_exactly_once(lg, window_kw, phrase, prov):
    s = _fd(lg, posts=[], provenance=prov, **window_kw)["statement"]
    assert s.count(phrase) == 1 and f"Window: {phrase}." in s, s


def test_an_empty_range_result_states_the_range_once(lg):
    tr = lg.timerange.TimeRange(start=datetime(2026, 10, 25, tzinfo=UTC), end=datetime(2026, 10, 26, tzinfo=UTC),
                                tz_name="UTC", kind="day", phrase="yesterday")
    prov = {"scan_count": 1, "search_time": SEARCH_AT, "range": {"kept": 0, "outside": 0, "no_timestamp": 0}}
    s = _fd(lg, posts=[], provenance=prov, time_range=tr)["statement"]
    assert s.count(tr.describe()) == 1 and "No stored posts matched this search." in s


@pytest.mark.parametrize("prov", [
    {"scan_count": 1, "search_time": SEARCH_AT}, UNCONFIRMED, {"scan_skipped": True}, {"scan_count": 1, "search_time": SEARCH_AT, "scan_incomplete": True},
    dict(UNCONFIRMED, saved_used=True, saved_updated_at=SAVED_AT), {},
])
@pytest.mark.parametrize("posts", [THREE, []])
def test_corrected_wording_still_passes_the_forbidden_claims_check(lg, prov, posts):
    s = _fd(lg, posts=posts, provenance=prov)["statement"]
    assert not FORBIDDEN.search(s), s
    assert "Collection time is unknown" in s and "not a complete view of Reddit" in s


def test_enrichment_with_no_search_terms_says_so_end_to_end(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _docs())
    prov = {}
    posts = _poll_terms(h, prov, [], None)
    _on(monkeypatch)
    out = json.loads(lg.enrich_freshness_answer(json.dumps(ANSWER), posts, provenance=prov, reference_utc=REF))
    assert out["data_freshness"]["search_status"] == "not_run" and "had no search terms" in out["data_freshness"]["statement"]


def test_normal_searches_are_unchanged_by_the_search_terms_check(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _docs())
    prov = {}
    posts = _poll_terms(h, prov, ["ai agents"], ["need an ai agent"])
    assert [p["post_url"] for p in posts] == EXPECTED_ORDER and prov["scan_count"] == 1 and "scan_skipped" not in prov


# ═════════════════════════════════════════════════════════════════════════
# C3. false-confirmation regression: unscored exits, cancelled scans, attribution, snapshots
# ═════════════════════════════════════════════════════════════════════════

# The three scoring mechanisms behave differently, so every case runs on all of them:
#   streaming  default: query embeddings are needed BEFORE the collections are read, scoring happens per batch
#   legacy     SIGNAL_EMBEDDING_CANDIDATE_POOL > 0: collections are read first, embeddings prepared afterwards
#   nonstream  STREAMING_SCORE_ENABLED=False: same order as legacy, unlimited pool
MODES = ["streaming", "legacy", "nonstream"]


def _mode(lg, monkeypatch, mode, docs=None):
    h = Harness(lg, monkeypatch, docs if docs is not None else _docs())
    lg._SCAN_STATUS.clear()
    if mode == "legacy":
        monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_CANDIDATE_POOL", 500)
    elif mode == "nonstream":
        _set_cfg(monkeypatch, STREAMING_SCORE_ENABLED=False)
    return h


def _status(lg):
    st = lg.get_scan_status("chat1", "topic1")
    return None if st is None else {"complete": st["complete"], "scored": st["scored"]}


@pytest.mark.parametrize("mode", MODES)
def test_valid_searches_are_still_confirmed_on_every_mechanism(lg, monkeypatch, mode):
    h = _mode(lg, monkeypatch, mode)
    prov = {}
    posts = _poll_terms(h, prov, ["ai agents"], ["need an ai agent"])
    assert [p["post_url"] for p in posts] == EXPECTED_ORDER                    # membership and order unchanged
    assert _status(lg) == {"complete": True, "scored": True}
    assert set(prov) == {"scan_count", "search_time", "scan_urls"}              # no flag of any kind
    assert prov["scan_urls"] == EXPECTED_ORDER                                 # exactly the posts this scan returned
    f = lg.build_data_freshness(posts, REF, provenance=prov)
    assert f["search_status"] == "confirmed" and f["searched_this_request"] is True
    assert f["statement"].startswith("Stored posts were searched on ")


@pytest.mark.parametrize("mode", MODES)
def test_unusable_embeddings_are_never_a_confirmed_search(lg, monkeypatch, mode):
    h = _mode(lg, monkeypatch, mode)
    monkeypatch.setattr(lg, "generate_query_embeddings_batch", lambda texts: None)
    prov = {}
    posts = _poll_terms(h, prov, ["ai agents"], ["need an ai agent"])
    assert posts == []
    # the registry keeps the meaning the strict wait logic relies on, and adds the truth about scoring
    assert _status(lg) == {"complete": True, "scored": False}
    if mode == "streaming":
        assert h.coll.calls == 0                                               # failed BEFORE reading anything
    else:
        assert h.coll.calls >= 1                                               # collections were read, never scored
    assert {k: v for k, v in prov.items() if k != "search_time"} == {
        "scan_count": 1, "scan_unconfirmed": True, "scan_unscored": True, "scan_urls": []}
    f = lg.build_data_freshness(posts, REF, provenance=prov)
    assert (f["search_status"], f["mode"], f["searched_this_request"], f["search_time_utc"], f["partial"]) == (
        "unconfirmed", "scanned", False, None, "unconfirmed")
    assert f["statement"].startswith(
        f"A search of stored posts was attempted for this answer but {NO_TIME}. 0 stored posts were considered. "
        "No stored posts are available for this answer.")
    assert not SEARCHED_CLAIM.search(f["statement"]) and not TIME_OF_DAY.search(f["statement"])
    assert "matched this search" not in f["statement"]


@pytest.mark.parametrize("mode", MODES)
def test_unusable_embeddings_with_saved_posts_and_a_larger_need_do_not_extend_them(lg, monkeypatch, mode):
    h = _mode(lg, monkeypatch, mode)
    _set_cfg(monkeypatch, INCREMENTAL_RESCAN_ENABLED=False)                    # a full scan, so scoring is reached (see next test)
    saved = _poll_terms(h, {}, ["ai agents"], ["need an ai agent"])            # a normal request fills the cache
    assert len(saved) == 6
    saved_at = h.cache.docs[("chat1", "topic1")]["updated_at"]
    h.cache.docs[("chat1", "topic1")].pop("query_embedding", None)             # force the embeddings to be rebuilt
    h.coll.calls = 0
    monkeypatch.setattr(lg, "generate_query_embeddings_batch", lambda texts: None)
    prov = {}
    posts = _poll_terms(h, prov, ["ai agents"], ["need an ai agent"])          # needs 50 > 6 saved: a top-up is tried
    assert [p["post_url"] for p in posts] == EXPECTED_ORDER                    # the saved posts come back unchanged
    assert prov["saved_used"] is True and prov["saved_count"] == 6 and prov["saved_updated_at"] == saved_at
    assert prov["scan_unconfirmed"] is True and prov["scan_unscored"] is True and "scan_incomplete" not in prov
    f = lg.build_data_freshness(posts, REF, provenance=prov)
    s = f["statement"]
    assert (f["search_status"], f["mode"], f["searched_this_request"], f["search_time_utc"]) == (
        "unconfirmed", "saved_plus_scan", False, None)
    assert s.startswith("Saved results from an earlier search in this chat (last updated ")
    assert f"were used. A search of stored posts was attempted for this answer but {NO_TIME}, and it is not known " \
           "whether it added posts. 6 stored posts were considered." in s
    assert "extended" not in s and "combined" not in s and not SEARCHED_CLAIM.search(s)
    assert s.count(" UTC") == len(re.findall(r"\d{2}:\d{2} UTC", s)) == 1      # the saved-results time only, no search time


@pytest.mark.parametrize("mode,reads,status,unscored", [
    ("streaming", False, "unconfirmed", True),     # needs the embeddings up front: fails before reading anything
    ("legacy", True, "unconfirmed", True),         # always a full scan here: collections read, then no embeddings
    ("nonstream", True, "confirmed", False),       # incremental: an empty delta returns before embeddings are needed
])
def test_the_mechanism_decides_whether_a_failed_embedding_matters(lg, monkeypatch, mode, reads, status, unscored):
    """Not every valid search reaches the scoring step. With the incremental rescan on, the non-streaming path finds
    no newer documents and returns before query embeddings are needed: that search ran to its end and stays confirmed."""
    h = _mode(lg, monkeypatch, mode)
    _poll_terms(h, {}, ["ai agents"], ["need an ai agent"])
    h.cache.docs[("chat1", "topic1")].pop("query_embedding", None)
    h.coll.calls = 0
    monkeypatch.setattr(lg, "generate_query_embeddings_batch", lambda texts: None)
    prov = {}
    posts = _poll_terms(h, prov, ["ai agents"], ["need an ai agent"])
    assert [p["post_url"] for p in posts] == EXPECTED_ORDER
    assert (h.coll.calls > 0) is reads
    assert _status(lg) == {"complete": True, "scored": not unscored}
    f = lg.build_data_freshness(posts, REF, provenance=prov)
    assert f["search_status"] == status and (prov.get("scan_unscored") is True) is unscored


@pytest.mark.parametrize("mode", MODES)
def test_all_zero_query_embeddings_are_not_a_confirmed_search_either(lg, monkeypatch, mode):
    """Embeddings exist but cannot be normalised (a second, separate early return in the legacy/non-streaming path)."""
    h = _mode(lg, monkeypatch, mode)
    monkeypatch.setattr(lg, "generate_query_embeddings_batch", lambda texts: [[0.0, 0.0, 0.0, 0.0] for _ in texts])
    prov = {}
    posts = _poll_terms(h, prov, ["ai agents"], ["need an ai agent"])
    assert posts == [] and _status(lg) == {"complete": True, "scored": False}
    assert prov["scan_unconfirmed"] is True and prov["scan_unscored"] is True
    f = lg.build_data_freshness(posts, REF, provenance=prov)
    assert (f["search_status"], f["search_time_utc"]) == ("unconfirmed", None) and not SEARCHED_CLAIM.search(f["statement"])


def test_a_scan_that_registers_nothing_is_still_unconfirmed(lg, monkeypatch):
    h = _mode(lg, monkeypatch, "streaming")
    prov = {}
    lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, lambda tk, k, **x: [], provenance_out=prov)
    assert prov["scan_unconfirmed"] is True and "scan_unscored" not in prov


def test_mark_unscored_only_touches_the_status_of_the_calling_thread(lg):
    lg._SCAN_STATUS.clear()
    lg._register_scan_status("c", "t", True)
    lg._mark_scan_unscored("c", "t")
    assert lg.get_scan_status("c", "t")["scored"] is False and lg.get_scan_status("c", "t")["complete"] is True
    lg._register_scan_status("c", "t", True)                                   # a fresh registration is scored again
    assert lg.get_scan_status("c", "t")["scored"] is True
    other = threading.Thread(target=lg._register_scan_status, args=("c", "t", False))
    other.start(); other.join()
    lg._mark_scan_unscored("c", "t")                                           # the entry belongs to another thread
    st = lg.get_scan_status("c", "t")
    assert st["scored"] is True and st["complete"] is False
    lg._mark_scan_unscored(None, "t"), lg._mark_scan_unscored("c", None), lg._mark_scan_unscored("x", "y")


def test_the_status_keeps_the_fields_the_strict_wait_logic_reads(lg):
    lg._SCAN_STATUS.clear()
    lg._register_scan_status("c", "t", False)
    st = lg.get_scan_status("c", "t")
    assert st["complete"] is False and isinstance(st["at"], float) and st["scored"] is True
    assert st["thread"] == threading.get_ident()
    assert lg.get_scan_status("c", "missing") is None


# ── registry attribution: a status written by ANOTHER thread is never this request's confirmation ──────────────
def _foreign_status(lg, complete):
    t = threading.Thread(target=lg._register_scan_status, args=("c", "t", complete))
    t.start(); t.join()


def _run_scan(lg, matcher):
    prov = {}
    lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, matcher, provenance_out=prov)
    return prov


def test_another_threads_status_during_my_scan_is_not_my_confirmation(lg, monkeypatch):
    Harness(lg, monkeypatch, [])
    lg._SCAN_STATUS.clear()

    def matcher(tk, kw, **x):                    # I never report; a cancelled scan of an earlier request does, meanwhile
        _foreign_status(lg, True)
        return []
    prov = _run_scan(lg, matcher)
    assert prov["scan_unconfirmed"] is True and "scan_incomplete" not in prov
    f = lg.build_data_freshness([], REF, provenance=prov)
    assert f["search_status"] == "unconfirmed" and f["search_time_utc"] is None and not SEARCHED_CLAIM.search(f["statement"])


def test_another_threads_status_overwriting_mine_is_unconfirmed_not_mixed(lg, monkeypatch):
    Harness(lg, monkeypatch, [])
    lg._SCAN_STATUS.clear()

    def matcher(tk, kw, **x):
        lg._register_scan_status("c", "t", True)           # my own report ...
        _foreign_status(lg, False)                         # ... overwritten by the other scan before I read it
        return []
    prov = _run_scan(lg, matcher)
    assert prov["scan_unconfirmed"] is True and "scan_incomplete" not in prov     # not credited with the other scan's False


def test_my_own_status_is_still_my_confirmation(lg, monkeypatch):
    Harness(lg, monkeypatch, [])
    lg._SCAN_STATUS.clear()
    _foreign_status(lg, False)                              # an older foreign entry exists beforehand

    def matcher(tk, kw, **x):
        lg._register_scan_status("c", "t", True)
        return []
    prov = _run_scan(lg, matcher)
    assert set(prov) == {"scan_count", "search_time", "scan_urls"} and prov["scan_urls"] == []


# ── cancelled scans ────────────────────────────────────────────────────────────────────────────────────────────
def _cancelled_over_saved_posts(lg, monkeypatch, mode="streaming", after_start=False):
    h = _mode(lg, monkeypatch, mode)
    _poll_terms(h, {}, ["ai agents"], ["need an ai agent"])
    saved_at = h.cache.docs[("chat1", "topic1")]["updated_at"]
    ev = threading.Event()
    prov = {}
    if after_start:                                         # the deadline releases the scan WHILE it runs
        real = h.lg.get_matched_signals

        def matcher(tk, kw, **x):
            out = real(tk, kw, **x)
            ev.set()
            return out
        posts = h.lg.get_evidence_with_topup("chat1", "owner", "topic1", ["ai agents"], 50, matcher,
                                             match_phrases=["need an ai agent"], user_query="q",
                                             provenance_out=prov, cancel_event=ev)
    else:
        ev.set()
        posts = _poll_terms(h, prov, ["ai agents"], ["need an ai agent"], cancel_event=ev)
    return posts, prov, saved_at


@pytest.mark.parametrize("mode", ["streaming", "nonstream"])
def test_a_scan_that_was_really_stopped_is_never_described_as_extending_saved_results(lg, monkeypatch, mode):
    posts, prov, saved_at = _cancelled_over_saved_posts(lg, monkeypatch, mode, after_start=False)
    assert _status(lg) == {"complete": False, "scored": True}                  # these scans honour the event and report it
    assert prov["scan_cancelled"] is True and prov["scan_incomplete"] is True and prov["scan_urls"] == []
    assert prov["saved_used"] is True and prov["saved_updated_at"] == saved_at
    assert [p["post_url"] for p in posts] == EXPECTED_ORDER                    # the evidence is the saved posts, unchanged
    f = lg.build_data_freshness(posts, REF, provenance=prov, timed_out=True)
    s = f["statement"]
    assert (f["search_status"], f["mode"], f["searched_this_request"], f["search_time_utc"], f["partial"]) == (
        "cancelled", "scan_cancelled", False, None, "time_limit")
    assert (f["scan_results_included"], f["finished_before_cancel"]) == (None, False)      # it returned nothing to include
    assert s.startswith(
        "Saved results from an earlier search in this chat (last updated ")
    assert ("were used. A new search of stored posts was started for this answer but was stopped before it finished, "
            "so its results are not included and it did not extend the saved results. ") in s
    assert "Partial results: the search stopped at its time limit before every source was checked" in s
    assert not SEARCHED_CLAIM.search(s) and "extended by" not in s
    assert f["saved_results_updated_at"] == saved_at.isoformat() and s.count(" UTC") == 1


def test_the_legacy_scan_ignores_the_event_so_it_is_a_finished_search(lg, monkeypatch):
    """Not every mechanism honours a cancel event: the legacy scan runs to its end, reports itself complete and returns
    its results. Calling that 'stopped before it finished' would be false."""
    posts, prov, saved_at = _cancelled_over_saved_posts(lg, monkeypatch, "legacy", after_start=False)
    assert _status(lg) == {"complete": True, "scored": True}
    assert prov["scan_cancelled"] is True and "scan_incomplete" not in prov and prov["scan_urls"] == []
    f = lg.build_data_freshness(posts, REF, provenance=prov, timed_out=True)
    assert (f["search_status"], f["mode"], f["finished_before_cancel"], f["partial"]) == (
        "confirmed", "saved_plus_scan", True, None)
    assert f["scan_results_included"] is None and f["searched_this_request"] is True      # it found nothing new to include
    assert "stopped before it finished" not in f["statement"] and "stopped at its time limit" not in f["statement"]


# ── the event is set AFTER the scan produced its results ────────────────────────────────────────────────────────
def _new_docs(n=3):
    return [_mkdoc(i, 0.9, inserted=datetime.now(UTC)) for i in range(7, 7 + n)]       # newer than everything saved


def _cancel_after_completion(lg, monkeypatch, mode, add_docs):
    h = _mode(lg, monkeypatch, mode)
    first = _poll_terms(h, {}, ["ai agents"], ["need an ai agent"])
    assert [p["post_url"] for p in first] == EXPECTED_ORDER
    saved_at = h.cache.docs[("chat1", "topic1")]["updated_at"]
    h.coll.docs.extend(add_docs)
    ev, real, prov = threading.Event(), h.lg.get_matched_signals, {}

    def matcher(tk, kw, **x):
        out = real(tk, kw, **x)
        ev.set()                                            # released only once the scan has produced its results
        return out
    posts = h.lg.get_evidence_with_topup("chat1", "owner", "topic1", ["ai agents"], 50, matcher,
                                         match_phrases=["need an ai agent"], user_query="q",
                                         provenance_out=prov, cancel_event=ev)
    return h, first, posts, prov, saved_at


@pytest.mark.parametrize("mode", ["streaming", "nonstream"])
def test_results_produced_before_a_cancel_are_returned_and_disclosed_as_included(lg, monkeypatch, mode):
    h, first, posts, prov, saved_at = _cancel_after_completion(lg, monkeypatch, mode, _new_docs(3))
    new_urls = [f"https://www.reddit.com/r/t/comments/{i}/p/" for i in (7, 8, 9)]
    # 1. the evidence really DOES contain the scan's results (this is what the old wording denied)
    assert sorted(p["post_url"] for p in posts) == sorted(EXPECTED_ORDER + new_urls)
    assert [p["post_url"] for p in posts[:6]] == EXPECTED_ORDER                # saved posts first, order unchanged
    assert sorted(prov["scan_urls"]) == sorted(new_urls)                       # recorded exactly: the new ones only
    assert sorted(p["post_url"] for p in h.cache.docs[("chat1", "topic1")]["posts"]) == sorted(EXPECTED_ORDER + new_urls)
    # 2. the registry says the scan ran to its end although the event was set afterwards
    assert prov["scan_cancelled"] is True and "scan_incomplete" not in prov and "scan_unconfirmed" not in prov
    assert _status(lg) == {"complete": True, "scored": True}
    # 3. the disclosure reflects what was used, with no cancel or time-limit claim
    f = lg.build_data_freshness(posts, REF, provenance=prov, timed_out=True)
    s = f["statement"]
    assert (f["search_status"], f["mode"], f["searched_this_request"], f["partial"]) == (
        "confirmed", "saved_plus_scan", True, None)
    assert (f["scan_results_included"], f["finished_before_cancel"]) == (True, True)
    assert f["search_time_utc"] is not None and f["counts"]["evidence_posts"] == 9
    assert s.startswith("Saved results from an earlier search in this chat (last updated ")
    assert "were extended by searching stored posts again on " in s and "9 stored posts were considered." in s
    assert "stopped" not in s and "not included" not in s and "Partial" not in s
    assert f["saved_results_updated_at"] == saved_at.isoformat()


def test_results_produced_before_a_cancel_with_nothing_new_are_a_finished_search(lg, monkeypatch):
    h, first, posts, prov, _ = _cancel_after_completion(lg, monkeypatch, "streaming", [])
    assert [p["post_url"] for p in posts] == EXPECTED_ORDER and prov["scan_urls"] == []
    f = lg.build_data_freshness(posts, REF, provenance=prov)
    assert (f["search_status"], f["finished_before_cancel"], f["partial"]) == ("confirmed", True, None)
    assert "stopped" not in f["statement"] and "not included" not in f["statement"]


def test_a_finished_search_whose_posts_were_not_in_the_evidence_used_says_so(lg, monkeypatch):
    """The scan finished and found posts, but the answer was built from the older saved list (as routes.py does at its
    deadline). The search is confirmed; its posts are NOT claimed as included."""
    h, first, returned, prov, saved_at = _cancel_after_completion(lg, monkeypatch, "streaming", _new_docs(3))
    assert len(returned) == 9 and len(prov["scan_urls"]) == 3
    f = lg.build_data_freshness(first, REF, provenance=prov, timed_out=True)   # evidence = the 6 saved posts only
    s = f["statement"]
    assert (f["search_status"], f["mode"], f["searched_this_request"], f["partial"]) == (
        "confirmed", "scan_late", True, "late_results")
    assert (f["scan_results_included"], f["finished_before_cancel"], f["counts"]["evidence_posts"]) == (False, False, 6)
    assert s.startswith("Saved results from an earlier search in this chat (last updated ")
    assert ("were used. A new search of stored posts finished on ") in s
    assert ", but the posts it found are not included in this answer. 6 stored posts were considered." in s
    assert "Partial results: the finished search found posts that this answer does not include." in s
    assert "extended" not in s and "stopped" not in s and "stopped at its time limit" not in s


def test_a_time_range_scan_that_finished_before_the_cancel_keeps_its_results_and_says_so(lg, monkeypatch):
    h = _mode(lg, monkeypatch, "streaming")
    tr = lg.timerange.TimeRange(start=NOW - timedelta(hours=4), end=NOW - timedelta(hours=2, minutes=50),
                                tz_name="UTC", kind="rolling", phrase="x")
    ev, real, prov = threading.Event(), h.lg.get_matched_signals, {}

    def matcher(tk, kw, **x):
        out = real(tk, kw, **x)
        ev.set()
        return out
    posts = h.lg.get_evidence_with_topup("chat1", "owner", "topic1", ["ai agents"], 50, matcher,
                                         match_phrases=["need an ai agent"], user_query="q", time_range=tr,
                                         provenance_out=prov, cancel_event=ev)
    assert [p["post_url"] for p in posts] == EXPECTED_ORDER                    # the range path returned its results
    assert prov["scan_urls"] == EXPECTED_ORDER and prov["scan_cancelled"] is True and "range" in prov
    f = lg.build_data_freshness(posts, REF, provenance=prov, time_range=tr, timed_out=True)
    assert (f["search_status"], f["mode"], f["partial"], f["finished_before_cancel"]) == (
        "confirmed", "range_scan", None, True)
    assert f["statement"].startswith("Stored posts were searched on ") and f"limited to {tr.describe()}" in f["statement"]
    assert "stopped" not in f["statement"] and "not included" not in f["statement"]


def test_a_partly_found_stopped_scan_whose_posts_are_included_says_so(lg):
    saved, found = THREE, [_p(7, iso(2026, 10, 25, 9, 0)), _p(8, iso(2026, 10, 25, 10, 0))]
    prov = {"scan_count": 1, "search_time": SEARCH_AT, "scan_cancelled": True, "scan_incomplete": True,
            "saved_used": True, "saved_updated_at": SAVED_AT, "scan_urls": [p["post_url"] for p in found]}
    f = _fd(lg, posts=saved + found, provenance=prov, timed_out=True)
    s = f["statement"]
    assert (f["search_status"], f["mode"], f["searched_this_request"], f["search_time_utc"], f["partial"]) == (
        "cancelled", "scan_cancelled", False, None, "time_limit")
    assert (f["scan_results_included"], f["finished_before_cancel"]) == (True, False)
    assert ("were used. A new search of stored posts was started for this answer but was stopped before it finished; "
            "the posts it had found by then are included. 5 stored posts were considered.") in s
    assert "not included" not in s and "did not extend" not in s and "11:58" not in s
    # the same stopped scan with none of its posts in the evidence keeps the 'not included' wording
    g = _fd(lg, posts=saved, provenance=prov, timed_out=True)
    assert g["scan_results_included"] is False and "its results are not included and it did not extend" in g["statement"]


def test_without_a_record_of_the_scans_posts_the_cautious_cancelled_wording_is_kept(lg):
    prov = {"scan_count": 1, "search_time": SEARCH_AT, "scan_cancelled": True, "saved_used": True,
            "saved_updated_at": SAVED_AT}                                      # no "scan_urls": cannot tell what was used
    f = _fd(lg, provenance=prov, timed_out=True)
    assert (f["search_status"], f["scan_results_included"], f["finished_before_cancel"]) == ("cancelled", None, False)
    assert "its results are not included" in f["statement"]


def test_a_cancel_flag_never_hides_an_unclean_scan(lg):
    for flag in ({"scan_incomplete": True}, {"scan_unconfirmed": True}):
        prov = dict({"scan_count": 1, "search_time": SEARCH_AT, "scan_cancelled": True, "scan_urls": []}, **flag)
        f = _fd(lg, provenance=prov)
        assert f["finished_before_cancel"] is False and f["searched_this_request"] is False


def test_scan_urls_are_collected_only_while_tracking_and_only_for_new_posts(lg, monkeypatch):
    h = _mode(lg, monkeypatch, "streaming")
    assert lg._prov_urls(None, [_p(1)]) is None
    assert lg._prov_urls({}, [_p(1), _p(2), {"x": 1}, "junk", _p(2)], [_p(1)]) == [_p(2)["post_url"], _p(2)["post_url"]]
    assert lg._prov_urls({}, None) == [] and lg._prov_urls({}, 5) is None
    prov = {}
    _poll_terms(h, prov, ["ai agents"], ["need an ai agent"])                  # first request: all six are new
    assert prov["scan_urls"] == EXPECTED_ORDER
    again = {}
    _poll_terms(h, again, ["ai agents"], ["need an ai agent"])                 # second: nothing new
    assert again["scan_urls"] == []
    out = h.lg.get_evidence_with_topup("chat9", "o", "t9", ["ai agents"], 50, h.lg.get_matched_signals,
                                       match_phrases=["need an ai agent"], user_query="q")      # tracking off
    assert [p["post_url"] for p in out] == EXPECTED_ORDER


def test_a_cancelled_scan_without_saved_results_and_without_a_deadline(lg, monkeypatch):
    h = _mode(lg, monkeypatch, "streaming")
    ev = threading.Event(); ev.set()
    prov = {}
    posts = _poll_terms(h, prov, ["ai agents"], ["need an ai agent"], cancel_event=ev)
    f = lg.build_data_freshness(posts, REF, provenance=prov)                   # not a deadline: a disconnect
    assert (f["search_status"], f["partial"]) == ("cancelled", "cancelled")
    assert f["statement"].startswith("A search of stored posts was started for this answer but was stopped before it "
                                     "finished, so its results are not included. ")
    assert "Partial results: the search was stopped before it finished, so posts may be missing." in f["statement"]
    assert not SEARCHED_CLAIM.search(f["statement"])


def test_an_event_that_is_never_set_changes_nothing(lg, monkeypatch):
    h = _mode(lg, monkeypatch, "streaming")
    prov = {}
    posts = _poll_terms(h, prov, ["ai agents"], ["need an ai agent"], cancel_event=threading.Event())
    assert [p["post_url"] for p in posts] == EXPECTED_ORDER and "scan_cancelled" not in prov
    assert lg.build_data_freshness(posts, REF, provenance=prov)["search_status"] == "confirmed"


def test_a_cancelled_range_scan_does_not_report_its_filter_counts(lg):
    prov = {"scan_count": 1, "search_time": SEARCH_AT, "scan_cancelled": True,
            "range": {"kept": 3, "outside": 9, "no_timestamp": 4}}
    tr = lg.timerange.TimeRange(start=datetime(2026, 10, 25, tzinfo=UTC), end=datetime(2026, 10, 26, tzinfo=UTC),
                                tz_name="UTC", kind="day", phrase="yesterday")
    s = _fd(lg, provenance=prov, time_range=tr)["statement"]
    assert "Left out by date" not in s and "limited to" not in s.split("Stored post dates")[0]
    assert s.startswith("A search of stored posts was started for this answer but was stopped before it finished")


def test_cancellation_is_not_inspected_when_the_feature_is_off(lg, monkeypatch):
    h = _mode(lg, monkeypatch, "streaming")

    class Boom:
        def is_set(self):
            raise AssertionError("cancel_event must not be read for provenance when tracking is off")
    # the matcher is called without cancel support, so an event that explodes when read proves it is never read here
    out = lg.get_evidence_with_topup("chat1", "owner", "topic1", ["ai agents"], 50, lambda tk, k, **x: [],
                                     cancel_event=Boom())
    assert out == []


# ── snapshots and late writes ──────────────────────────────────────────────────────────────────────────────────
class Racy(dict):
    """A provenance dict that another thread overwrites in the middle of the first read made through .get()."""
    def __init__(self, first, second):
        super().__init__(first)
        self._second, self.reads = second, 0

    def get(self, key, default=None):
        self.reads += 1
        if self.reads == 2:
            self.clear(); self.update(self._second)
        return super().get(key, default)


CONFIRMED_REC = {"scan_count": 1, "search_time": SEARCH_AT, "scan_unconfirmed": False, "scan_cancelled": False,
                 "scan_incomplete": False}
CANCELLED_REC = {"scan_count": 1, "search_time": SEARCH_AT, "scan_unconfirmed": True, "scan_cancelled": True,
                 "scan_incomplete": True}


def test_the_disclosure_is_built_from_one_snapshot(lg):
    racy = Racy(CONFIRMED_REC, CANCELLED_REC)
    f = lg.build_data_freshness(THREE, REF, provenance=racy)
    expected = lg.build_data_freshness(THREE, REF, provenance=dict(CONFIRMED_REC))
    assert racy.reads == 0                                                     # the live dict is never read field by field
    assert f == expected and f["search_status"] == "confirmed"
    assert f["statement"].startswith("Stored posts were searched on 26 Oct 2026, 11:58 UTC.")


def test_a_snapshot_also_covers_the_nested_range_record(lg):
    rng = {"kept": 1, "outside": 2, "no_timestamp": 3}
    prov = dict(CONFIRMED_REC, range=rng)
    tr = lg.timerange.TimeRange(start=datetime(2026, 10, 25, tzinfo=UTC), end=datetime(2026, 10, 26, tzinfo=UTC),
                                tz_name="UTC", kind="day", phrase="yesterday")
    before = _fd(lg, provenance=prov, time_range=tr)
    rng.update(outside=99)
    assert "2 outside the range and 3 without" in before["statement"]


def test_concurrent_writers_never_produce_a_mixed_disclosure(lg):
    prov = dict(CONFIRMED_REC)
    stop = threading.Event()

    def writer():
        flip = False
        while not stop.is_set():
            prov.update(CANCELLED_REC if flip else CONFIRMED_REC)             # each record is complete: one update()
            flip = not flip
    t = threading.Thread(target=writer); t.start()
    seen = set()
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            f = lg.build_data_freshness(THREE, REF, provenance=prov)
            seen.add(f["search_status"])
            if f["search_status"] == "confirmed":
                assert f["searched_this_request"] is True and f["search_time_utc"] == SEARCH_AT.isoformat()
                assert f["statement"].startswith("Stored posts were searched on 26 Oct 2026, 11:58 UTC.")
                assert f["partial"] is None
            else:
                assert f["search_status"] == "cancelled" and f["searched_this_request"] is False
                assert f["search_time_utc"] is None and not SEARCHED_CLAIM.search(f["statement"])
                assert f["statement"].startswith("A search of stored posts was started for this answer but was stopped")
    finally:
        stop.set(); t.join()
    assert seen == {"confirmed", "cancelled"}                                  # both states really were observed


def test_a_provenance_record_is_applied_with_a_single_update(lg, monkeypatch):
    Harness(lg, monkeypatch, [])
    calls = []

    class Watched(dict):
        def update(self, *a, **k):
            calls.append(dict(*a, **k)); super().update(*a, **k)

        def __setitem__(self, k, v):
            calls.append({k: v}); super().__setitem__(k, v)
    prov = Watched()
    lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, lambda tk, k, **x: [], provenance_out=prov, cancel_event=threading.Event())
    assert len(calls) == 1 and set(calls[0]) == {"scan_count", "search_time", "scan_unconfirmed", "scan_urls"}


def test_a_late_write_after_the_response_was_built_cannot_change_that_response(lg, monkeypatch):
    _on(monkeypatch)
    prov = {}                                                                  # the request found nothing recorded yet
    built = lg.enrich_freshness_answer(json.dumps(ANSWER), THREE, provenance=prov, reference_utc=REF, timed_out=True)
    before = json.loads(built)["data_freshness"]
    assert before["search_status"] == "not_recorded" and before["partial"] == "time_limit"
    # the cancelled scan finishes afterwards and writes into the same dict, as the real scan thread does
    lg._register_scan_status("c", "t", False)
    lg._prov_note(prov, "scan", "c", "t", None, saved_updated_at=SAVED_AT, saved_count=3, cancelled=True)
    assert prov["scan_count"] == 1 and prov["scan_cancelled"] is True            # the late write really happened
    assert json.loads(built)["data_freshness"] == before                         # ... and the built answer is untouched
    again = lg.enrich_freshness_answer(built, THREE, provenance=prov, reference_utc=REF)
    assert again == built                                                      # an existing disclosure is never rewritten


def test_a_late_write_does_not_reach_a_later_request(lg, monkeypatch):
    Harness(lg, monkeypatch, [])
    first, second = {}, {}
    lg._prov_note(first, "scan", "c", "t", None, cancelled=True)               # the abandoned request's thread writes late
    assert second == {}                                                        # each request owns its own dict
    assert lg.build_data_freshness(THREE, REF, provenance=second)["search_status"] == "not_recorded"


# ═════════════════════════════════════════════════════════════════════════
# D. enrich_freshness_answer and the flag
# ═════════════════════════════════════════════════════════════════════════

ANSWER = {"format": "source_list", "headline": "H", "executive_summary": "S",
          "platforms": [{"platform": "reddit", "total_analyzed": 1, "shown_count": 1, "posts": []}]}
PROV = {"scan_count": 1, "search_time": SEARCH_AT}


def _enrich(lg, answer=ANSWER, **kw):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    kw.setdefault("provenance", PROV)
    return text, lg.enrich_freshness_answer(text, THREE, reference_utc=REF, **kw)


def test_the_flag_is_off_in_code_and_absent_from_config(lg):
    import config
    assert not hasattr(config, FLAG) or getattr(config, FLAG) is False
    assert FLAG not in (ROOT / "config.py").read_text(encoding="utf-8")        # no production config change
    assert lg.freshness_enabled() is False


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("true", False), ("1", False), (1, False),
                                            (None, False), ("", False)])
def test_only_a_real_boolean_true_enables_the_feature(lg, monkeypatch, value, expected):
    _set_cfg(monkeypatch, **{FLAG: value})
    assert lg.freshness_enabled() is expected


def test_flag_off_leaves_the_answer_untouched(lg):
    text, out = _enrich(lg)
    assert out is text                                                          # the very same string back


def test_flag_off_with_every_kind_of_input_is_a_no_op(lg):
    for answer in (ANSWER, "plain text", "", None, [1, 2], {"format": "no_results"}):
        text = answer if isinstance(answer, str) or answer is None else json.dumps(answer)
        assert lg.enrich_freshness_answer(text, THREE, provenance={}) is text


def test_flag_on_adds_one_key_and_changes_nothing_else(lg, monkeypatch):
    _on(monkeypatch)
    text, out = _enrich(lg)
    new, old = json.loads(out), json.loads(text)
    assert "data_freshness" in new and new["data_freshness"]["mode"] == "scanned"
    new.pop("data_freshness")
    assert new == old                                                           # parsed content identical
    assert list(json.loads(out))[: len(old)] == list(old)                       # original keys keep their order


def test_flag_on_without_provenance_changes_nothing(lg, monkeypatch):
    _on(monkeypatch)
    text, out = _enrich(lg, provenance=None)
    assert out is text


def test_flag_on_with_empty_provenance_says_it_is_not_recorded(lg, monkeypatch):
    _on(monkeypatch)
    _, out = _enrich(lg, provenance={})
    assert json.loads(out)["data_freshness"]["mode"] == "unknown"


@pytest.mark.parametrize("answer", [
    "Plain prose, not JSON.", "", "   ", "[1, 2, 3]", '"a string"', "null", "{broken",
    {"no": "format key"}, {"format": 7},
    dict(ANSWER, answer_status="incomplete"),
    {"format": "disallowed", "message": "no"}, {"format": "not_available", "message": "no"},
    dict(ANSWER, data_freshness={"statement": "already there"}),
])
def test_other_answers_are_returned_unchanged(lg, monkeypatch, answer):
    _on(monkeypatch)
    text, out = _enrich(lg, answer=answer)
    assert out == text


@pytest.mark.parametrize("fmt", ["source_list", "trend_report", "comparison", "no_results"])
def test_every_search_format_gets_the_statement(lg, monkeypatch, fmt):
    _on(monkeypatch)
    _, out = _enrich(lg, answer={"format": fmt, "message": "m"})
    assert "statement" in json.loads(out)["data_freshness"]


def test_partial_answers_keep_their_status_and_get_the_statement(lg, monkeypatch):
    _on(monkeypatch)
    _, out = _enrich(lg, answer=dict(ANSWER, answer_status="partial"))
    data = json.loads(out)
    assert data["answer_status"] == "partial" and "data_freshness" in data


def test_enriching_twice_does_not_stack(lg, monkeypatch):
    _on(monkeypatch)
    _, once = _enrich(lg)
    assert lg.enrich_freshness_answer(once, THREE, provenance=PROV) == once


def test_hostile_inputs_never_raise(lg, monkeypatch):
    _on(monkeypatch)
    for prov in ({"scan_count": "x", "search_time": object()}, {"range": "no"}, {"saved_used": 1, "scan_count": None},
                 {"scan_count": 1, "search_time": float("nan")}):
        for posts in (None, [None], [{"published_at": object()}], "text"):
            out = lg.enrich_freshness_answer(json.dumps(ANSWER), posts, provenance=prov,
                                             matched_count="x", evidence_limit=[], time_window_days="y")
            assert json.loads(out)["format"] == "source_list"


def test_a_freshness_only_object_does_not_count_as_answer_content(lg):
    assert lg._answer_has_content({"format": "source_list", "data_freshness": {"statement": "Some text."}}) is False
    assert lg._answer_has_content({"format": "source_list", "headline": "H", "data_freshness": {"statement": "x"}}) is True


def test_answer_integrity_and_finalisation_pass_the_key_through(lg, monkeypatch):
    _on(monkeypatch)
    _, out = _enrich(lg)
    posts = [_p(1, iso(2026, 10, 25))]
    final, results = lg._finalize_answer_and_results(out, posts, seed="find buyers")
    assert json.loads(final)["data_freshness"]["mode"] == "scanned"
    assert len(results) == len(posts)


# ═════════════════════════════════════════════════════════════════════════
# E. golden regression: membership, order and the cache are unchanged
# ═════════════════════════════════════════════════════════════════════════

EXPECTED_ORDER = [f"https://www.reddit.com/r/t/comments/{i}/p/" for i in range(1, 7)]
_VOLATILE = {"updated_at", "scanned_at", "created_at"}


def _scenario(lg, monkeypatch, with_provenance, time_range=None):
    h = Harness(lg, monkeypatch, _docs())
    lg._SCAN_STATUS.clear()
    mk = (lambda: {}) if with_provenance else (lambda: None)
    steps = []
    for need in (50, 3, 50):
        if time_range is None:
            posts = _poll(h, mk(), need=need)
        else:
            posts = _poll(h, mk(), need=need, time_range=time_range)
        doc = h.cache.docs.get(("chat1", "topic1")) or {}
        steps.append((posts, {k: v for k, v in doc.items() if k not in _VOLATILE}, h.cache.writes))
    return steps


def test_golden_order_and_membership_are_unchanged_by_provenance(lg, monkeypatch):
    plain = _scenario(lg, monkeypatch, with_provenance=False)
    tracked = _scenario(lg, monkeypatch, with_provenance=True)
    assert plain == tracked                                                    # posts, cache document, write count
    for posts, _doc, _writes in plain:
        assert [p["post_url"] for p in posts] == EXPECTED_ORDER                # worked-out similarity order


def test_golden_time_range_path_is_unchanged_by_provenance(lg, monkeypatch):
    tr = lg.timerange.TimeRange(start=NOW - timedelta(hours=4), end=NOW - timedelta(hours=2, minutes=50),
                                tz_name="UTC", kind="rolling", phrase="x")
    plain = _scenario(lg, monkeypatch, False, time_range=tr)
    tracked = _scenario(lg, monkeypatch, True, time_range=tr)
    assert plain == tracked
    assert [p["post_url"] for p in plain[0][0]] == EXPECTED_ORDER[:len(plain[0][0])] and plain[0][0]


def test_golden_flag_on_leaves_every_other_field_of_the_answer_identical(lg, monkeypatch):
    base = json.dumps(dict(ANSWER, key_findings=[{"finding": "f"}], counts={"relevant": 1}))
    _on(monkeypatch)
    on = json.loads(lg.enrich_freshness_answer(base, THREE, provenance=PROV))
    on.pop("data_freshness")
    assert on == json.loads(base)


# ═════════════════════════════════════════════════════════════════════════
# F. call sites
# ═════════════════════════════════════════════════════════════════════════

def _run_complete(env, monkeypatch, *, flag, prov, llm=None):
    lg, idx, chats = env
    answer = llm or json.dumps(ANSWER)
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: answer)
    if flag:
        _on(monkeypatch)
    chats.update_one({"chat_id": "c1", "owner_key": "o1"}, {"$set": {"summary": ""}})
    idx.add_search_to_chat("c1", "o1", "Find users looking for AI jobs", lg.normalize_topic_key("q"), ["kw"], "reddit")
    msg = chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"][-1]
    msg["requested_at"] = datetime(2026, 10, 11, 10, 0, 0)
    post = [{"title": "Hiring freeze", "post_text": "We have a hiring freeze.", "published_at": iso(2026, 10, 9, 12, 0),
             "post_url": "https://www.reddit.com/r/pakistan/comments/p1/", "platform": "reddit"}]
    kw = {} if prov is None else {"freshness_prov": prov}
    idx._complete_message_answer_and_results("c1", "o1", msg, post, **kw)
    return json.loads(chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"][-1]["claude_answer"])


def test_index_saves_the_statement_when_the_flag_is_on(env, monkeypatch):
    saved = _run_complete(env, monkeypatch, flag=True, prov={"scan_count": 1, "search_time": SEARCH_AT})
    fd = saved["data_freshness"]
    assert fd["mode"] == "scanned" and fd["counts"]["evidence_posts"] == 1
    assert fd["post_dates"]["newest"] == iso(2026, 10, 9, 12, 0)
    assert "Stored posts were searched on 26 Oct 2026, 11:58 UTC." in fd["statement"]


def test_index_reports_saved_results_as_saved_results(env, monkeypatch):
    saved = _run_complete(env, monkeypatch, flag=True, prov={"saved_used": True, "saved_updated_at": SAVED_AT})
    s = saved["data_freshness"]["statement"]
    assert "reused from an earlier search" in s and "No new search of stored posts was run" in s
    assert not re.search(r"\bsearched\b", s)


def test_index_flag_off_saves_exactly_what_it_saved_before(env, monkeypatch):
    off = _run_complete(env, monkeypatch, flag=False, prov=None)
    assert "data_freshness" not in off


def test_index_flag_off_ignores_a_provenance_dict_it_is_handed(env, monkeypatch):
    off = _run_complete(env, monkeypatch, flag=False, prov={"scan_count": 1, "search_time": SEARCH_AT})
    assert "data_freshness" not in off


def test_index_flag_on_and_off_differ_only_by_the_new_key(env, monkeypatch):
    off = _run_complete(env, monkeypatch, flag=False, prov=None)
    env[2].docs.clear()
    on = _run_complete(env, monkeypatch, flag=True, prov={"scan_count": 1, "search_time": SEARCH_AT})
    on.pop("data_freshness")
    assert on == off


def test_index_leaves_non_json_and_cut_off_answers_alone(env, monkeypatch):
    saved = _run_complete(env, monkeypatch, flag=True, prov={"scan_count": 1, "search_time": SEARCH_AT},
                          llm='{"format": "source_list", "headline": "Remote work dominates", "executive_summary": "People say open')
    assert saved.get("answer_status") == "partial"


def test_the_timeout_fallback_passes_provenance_only_when_the_flag_is_on(env, monkeypatch):
    lg, idx, chats = env
    seen, saved = [], []

    def fake_topup(**kw):
        seen.append(dict(kw))
        if "provenance_out" in kw:
            kw["provenance_out"].update(saved_used=True, saved_updated_at=SAVED_AT, saved_count=1)
        return [{"title": "T", "post_text": "x", "post_url": "https://www.reddit.com/r/a/comments/1/b/",
                 "platform": "reddit", "published_at": iso(2026, 10, 9, 12, 0)}]
    gate = types.ModuleType("guest_gate")
    gate.PROCEED, gate.ABANDON, gate.DELIVER, gate.DISCARD, gate.WAIT, gate.DEFER = "proceed", "abandon", "deliver", "discard", "wait", "defer"
    gate.on_work_start = lambda *a, **k: gate.PROCEED
    gate.on_work_renew = lambda *a, **k: True
    gate.mark_limited = lambda key, rid, text: text
    gate.on_answer_ready = lambda *a, **k: gate.DELIVER
    gate.on_search_incomplete = lambda *a, **k: None
    gate.on_work_end = lambda *a, **k: None
    gate.superseded_answer = lambda: "{}"
    monkeypatch.setitem(sys.modules, "guest_gate", gate)
    monkeypatch.setattr(lg, "get_evidence_with_topup", fake_topup)
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps(ANSWER))
    monkeypatch.setattr(idx, "get_chat_session", lambda *a, **k: {"summary": ""})
    monkeypatch.setattr(idx, "save_answer_for_gate", lambda cid, ok, tk, answer, *a, **k: saved.append(answer) or True)
    monkeypatch.setattr(idx, "save_signal_results_to_chat", lambda *a, **k: None)
    monkeypatch.setattr(idx, "append_to_chat_summary", lambda *a, **k: None)
    monkeypatch.setattr(idx, "_clear_owner_busy", lambda *a, **k: None)

    def run(flag):
        seen.clear(), saved.clear()
        _set_cfg(monkeypatch, **{FLAG: flag})
        lg._timeout_fallback_answer("c1", "o1", "tk", "Find AI jobs", keywords=["k"], gate_owner="g")
    run(False)
    assert len(seen) == 1 and "provenance_out" not in seen[0]
    assert len(saved) == 1 and "data_freshness" not in json.loads(saved[0])
    run(True)
    assert len(seen) == 1 and isinstance(seen[0].get("provenance_out"), dict)
    fd = json.loads(saved[0])["data_freshness"]
    assert fd["mode"] == "saved_only" and "No new search of stored posts was run" in fd["statement"]
    assert fd["counts"]["evidence_posts"] == 1


from tests.test_scan_resilience import routes_env  # noqa: E402,F401  (fixture: routes.py against a stubbed index)


def test_routes_streaming_passes_provenance_only_when_the_flag_is_on(routes_env, monkeypatch):
    env = routes_env
    captured = []

    def capture(**kw):
        captured.append(kw)
        return []
    monkeypatch.setattr(env.routes, "get_evidence_with_topup", capture)
    for flag in (False, True):
        captured.clear()
        _set_cfg(monkeypatch, NONBLOCKING_STREAM_ENABLED=False, STRICT_INTENT_MODE=False, **{FLAG: flag})
        gen = iter(env.routes.stream_answer(MagicMock(), f"chat-{flag}", "tk").body_iterator)
        try:
            next(gen)
        finally:
            gen.close()
        assert captured, "the streaming route did not call get_evidence_with_topup"
        assert ("provenance_out" in captured[0]) is flag
        if flag:
            assert captured[0]["provenance_out"] == {}


def _stream_answer(env, monkeypatch, flag, saved_prov=None, topup_override=None, nonblocking=False, response_timeout=None,
                   before_answer=None, cached_posts=None):
    """Runs the REAL streaming generator to completion with a stub model; returns (answer dict, done event)."""
    rt = env.routes
    posts = [{"title": f"t{i}", "post_text": "x", "post_url": f"https://www.reddit.com/r/a/comments/{i}/b/",
              "platform": "reddit", "published_at": iso(2026, 10, 9, 12, 0)} for i in range(25)]

    def topup(**kw):
        prov = kw.get("provenance_out")
        if prov is not None:
            prov.update(saved_prov or {"scan_count": 1, "search_time": SEARCH_AT})
        return posts
    gate = types.SimpleNamespace(
        DELIVER="d", WAIT="w", PROCEED="p", ABANDON="a", DISCARD="x", DEFER="f", gate_owner=lambda o, m: "gk",
        on_work_start=lambda *a, **k: "p", on_work_end=lambda *a, **k: None, superseded_answer=lambda: "{}",
        on_work_renew=lambda *a, **k: True, mark_limited=lambda k, r, t: t, on_answer_ready=lambda *a, **k: "d",
        on_search_incomplete=lambda *a, **k: None, on_delivery_failed=lambda *a, **k: None)
    monkeypatch.setattr(rt, "get_evidence_with_topup", topup_override or topup)
    if response_timeout is not None:
        monkeypatch.setattr(rt, "RESPONSE_TIMEOUT", response_timeout)
    def analyze(q, pool, extra_context=None):
        if before_answer is not None:
            before_answer()
        return json.dumps(ANSWER)
    monkeypatch.setattr(rt, "analyze_with_claude", analyze)
    if cached_posts is not None:
        monkeypatch.setattr(rt, "get_cached_topic_evidence",
                            lambda cid, tk: {"posts": list(cached_posts() if callable(cached_posts) else cached_posts)})
    monkeypatch.setattr(rt, "_patch_post_urls_into_answer", lambda a, pool, query=None: a)
    monkeypatch.setattr(rt, "_finalize_answer_and_results", lambda a, pool, seed="": (a, pool))
    monkeypatch.setattr(rt, "guest_gate", gate)
    monkeypatch.setattr(rt, "_guest_is_gated", lambda *a, **k: False)
    monkeypatch.setattr(rt, "_guest_done_extra", lambda *a, **k: {})
    _set_cfg(monkeypatch, NONBLOCKING_STREAM_ENABLED=nonblocking, SINGLE_FLIGHT_SCAN_ENABLED=True,
             STRICT_INTENT_MODE=False, SCAN_DEADLINE_SECONDS=0, **{FLAG: flag})
    gen = iter(rt.stream_answer(MagicMock(), f"chat-{flag}-{nonblocking}", "tk").body_iterator)
    text, done = "", None
    try:
        for chunk in gen:
            payload = json.loads(chunk[len("data: "):]) if chunk.startswith("data: ") else {}
            text += payload.get("delta", "")
            if payload.get("done"):
                done = payload
                break
    finally:
        gen.close()
    return json.loads(text), done


def test_routes_stream_adds_the_statement_when_the_flag_is_on(routes_env, monkeypatch):
    answer, done = _stream_answer(routes_env, monkeypatch, True)
    fd = answer["data_freshness"]
    assert fd["mode"] == "scanned" and fd["counts"]["evidence_posts"] == 25
    assert fd["evidence_limit_reached"] is True and "Showing up to 25 matching posts" in fd["statement"]
    assert fd["post_dates"]["newest"] == iso(2026, 10, 9, 12, 0)


def test_routes_stream_saved_results_are_not_called_a_search(routes_env, monkeypatch):
    answer, _ = _stream_answer(routes_env, monkeypatch, True,
                               saved_prov={"saved_used": True, "saved_updated_at": SAVED_AT})
    s = answer["data_freshness"]["statement"]
    assert answer["data_freshness"]["mode"] == "saved_only" and "No new search of stored posts was run" in s
    assert not re.search(r"\bsearched\b", s)


def test_routes_stream_a_scan_that_hit_its_deadline_is_stated_as_partial(routes_env, monkeypatch):
    import time as _time

    def slow_topup(**kw):
        ev = kw.get("cancel_event")
        end = _time.monotonic() + 8
        while _time.monotonic() < end and not (ev is not None and ev.is_set()):
            _time.sleep(0.02)
        return []
    answer, _ = _stream_answer(routes_env, monkeypatch, True, topup_override=slow_topup, nonblocking=True,
                               response_timeout=1)
    fd = answer["data_freshness"]
    assert fd["partial"] == "time_limit", fd
    assert "Partial results: the search stopped at its time limit" in fd["statement"]
    assert fd["mode"] == "unknown" and not re.search(r"\bsearched\b", fd["statement"])      # nothing was recorded as run


SAVED_POSTS = [{"title": f"s{i}", "post_text": "x", "post_url": f"https://www.reddit.com/r/a/comments/s{i}/b/",
                "platform": "reddit", "published_at": iso(2026, 10, 9, 12, 0)} for i in range(3)]


def _cancelled_scan(lg, wrote, release=None, holder=None):
    """A scan that, like the real one, only notices its cancellation after the deadline released it, and then
    writes its provenance into the request's dict (optionally only once `release` is set)."""
    def topup(**kw):
        ev, prov = kw.get("cancel_event"), kw.get("provenance_out")
        if holder is not None:
            holder["prov"] = prov
        end = time.monotonic() + 10
        while time.monotonic() < end and not (ev is not None and ev.is_set()):
            time.sleep(0.02)
        if release is not None:
            release.wait(10)
        lg._register_scan_status(kw["chat_id"], kw["topic_key"], False)        # a cancelled scan reports itself incomplete
        lg._prov_note(prov, "scan", kw["chat_id"], kw["topic_key"], None, saved_updated_at=SAVED_AT, saved_count=3,
                      cancelled=ev is not None and ev.is_set())
        wrote.set()
        return []
    return topup


def test_routes_stream_a_scan_cancelled_at_the_deadline_is_not_described_as_extending_saved_results(lg, routes_env, monkeypatch):
    wrote = threading.Event()
    answer, _ = _stream_answer(routes_env, monkeypatch, True, topup_override=_cancelled_scan(lg, wrote), nonblocking=True,
                               response_timeout=1, before_answer=lambda: wrote.wait(5), cached_posts=SAVED_POSTS)
    fd = answer["data_freshness"]
    s = fd["statement"]
    assert wrote.is_set()
    assert (fd["search_status"], fd["mode"], fd["searched_this_request"], fd["search_time_utc"], fd["partial"]) == (
        "cancelled", "scan_cancelled", False, None, "time_limit")
    assert fd["counts"]["evidence_posts"] == 3                                  # the evidence is the saved posts only
    assert s.startswith("Saved results from an earlier search in this chat (last updated 24 Oct 2026, 18:02 UTC) were used. "
                        "A new search of stored posts was started for this answer but was stopped before it finished, so "
                        "its results are not included and it did not extend the saved results. 3 stored posts were considered.")
    assert "Partial results: the search stopped at its time limit before every source was checked" in s
    assert "extended by" not in s and not SEARCHED_CLAIM.search(s)


def test_routes_stream_a_scan_that_finishes_after_the_response_cannot_change_it_or_a_later_request(lg, routes_env, monkeypatch):
    wrote, release, holder = threading.Event(), threading.Event(), {}
    answer, _ = _stream_answer(routes_env, monkeypatch, True, topup_override=_cancelled_scan(lg, wrote, release, holder),
                               nonblocking=True, response_timeout=1, cached_posts=SAVED_POSTS)
    fd = answer["data_freshness"]
    assert not wrote.is_set()                                                   # the scan has NOT written yet
    assert (fd["search_status"], fd["mode"], fd["partial"]) == ("not_recorded", "unknown", "time_limit")
    assert fd["statement"].startswith("How these results were gathered is not recorded for this answer. 3 stored posts")
    assert not SEARCHED_CLAIM.search(fd["statement"])
    release.set()                                                               # now the late thread writes its record
    assert wrote.wait(5)
    assert holder["prov"]["scan_count"] == 1 and holder["prov"]["scan_cancelled"] is True
    assert answer["data_freshness"] == fd                                       # the response that was built is unchanged
    # a later request has its own dict: the late write is invisible to it
    later, _ = _stream_answer(routes_env, monkeypatch, True)
    assert later["data_freshness"]["search_status"] == "confirmed" and later["data_freshness"]["partial"] is None
    assert later["data_freshness"]["mode"] == "scanned"


NEW_POSTS = [{"title": f"n{i}", "post_text": "x", "post_url": f"https://www.reddit.com/r/a/comments/n{i}/b/",
              "platform": "reddit", "published_at": iso(2026, 10, 10, 12, 0)} for i in range(2)]


def _finishing_scan(lg, wrote):
    """A scan that the deadline released while it was still working, but which then ran to its end: it reports itself
    complete and records the two new posts it found (and saved to the cache)."""
    def topup(**kw):
        ev, prov = kw.get("cancel_event"), kw.get("provenance_out")
        end = time.monotonic() + 10
        while time.monotonic() < end and not (ev is not None and ev.is_set()):
            time.sleep(0.02)
        lg._register_scan_status(kw["chat_id"], kw["topic_key"], True)
        lg._prov_note(prov, "scan", kw["chat_id"], kw["topic_key"], None, saved_updated_at=SAVED_AT, saved_count=3,
                      cancelled=ev is not None and ev.is_set(), scan_urls=[p["post_url"] for p in NEW_POSTS])
        wrote.set()
        return SAVED_POSTS + NEW_POSTS
    return topup


def test_routes_stream_a_scan_that_finished_before_the_cancel_and_is_in_the_evidence_is_disclosed_as_used(lg, routes_env, monkeypatch):
    wrote = threading.Event()

    def cache_after_the_scan_saved():            # the deadline path reads the cache; the scan has already written it
        wrote.wait(5)
        return SAVED_POSTS + NEW_POSTS
    answer, _ = _stream_answer(routes_env, monkeypatch, True, topup_override=_finishing_scan(lg, wrote), nonblocking=True,
                               response_timeout=1, cached_posts=cache_after_the_scan_saved)
    fd, s = answer["data_freshness"], answer["data_freshness"]["statement"]
    assert wrote.is_set() and fd["counts"]["evidence_posts"] == 5                # the scan's posts ARE in the evidence
    assert (fd["search_status"], fd["mode"], fd["searched_this_request"], fd["partial"]) == (
        "confirmed", "saved_plus_scan", True, None)
    assert (fd["scan_results_included"], fd["finished_before_cancel"]) == (True, True)
    assert "were extended by searching stored posts again on " in s and "5 stored posts were considered." in s
    assert "stopped" not in s and "not included" not in s and "Partial" not in s


def test_routes_stream_a_finished_scan_whose_posts_missed_the_evidence_is_not_claimed(lg, routes_env, monkeypatch):
    wrote = threading.Event()
    answer, _ = _stream_answer(routes_env, monkeypatch, True, topup_override=_finishing_scan(lg, wrote), nonblocking=True,
                               response_timeout=1, before_answer=lambda: wrote.wait(5), cached_posts=SAVED_POSTS)
    fd, s = answer["data_freshness"], answer["data_freshness"]["statement"]
    assert wrote.is_set() and fd["counts"]["evidence_posts"] == 3                # the evidence is the older saved list
    assert (fd["search_status"], fd["mode"], fd["searched_this_request"], fd["partial"]) == (
        "confirmed", "scan_late", True, "late_results")
    assert (fd["scan_results_included"], fd["finished_before_cancel"]) == (False, False)
    assert ", but the posts it found are not included in this answer. 3 stored posts were considered." in s
    assert "Partial results: the finished search found posts that this answer does not include." in s
    assert "extended" not in s and "stopped" not in s


def test_routes_stream_flag_off_and_on_return_the_same_answer_and_results(routes_env, monkeypatch):
    off, done_off = _stream_answer(routes_env, monkeypatch, False)
    on, done_on = _stream_answer(routes_env, monkeypatch, True)
    assert "data_freshness" not in off
    on.pop("data_freshness")
    assert on == off                                                              # answer: only the new key differs
    assert done_on["results"] == done_off["results"]                              # same posts, same order
    assert [p["post_url"] for p in done_off["results"]] == [f"https://www.reddit.com/r/a/comments/{i}/b/" for i in range(25)]


# ═════════════════════════════════════════════════════════════════════════
# G. rendering (chat.html and index.html share the renderer)
# ═════════════════════════════════════════════════════════════════════════

from tests.test_answer_persistence import STATE_JS, TEMPLATES, _for_template, _reopen  # noqa: E402

NOTE_JS = """() => [...document.querySelectorAll('.answer-rendered .data-freshness')].map(e => (
  {text: e.textContent, tag: e.tagName, role: e.getAttribute('role'), imgs: e.querySelectorAll('img,script').length,
   last: e.parentElement.lastElementChild === e}))"""


def _page(browser, name, answer):
    chat = _for_template({"chat_id": "c1", "title": "t", "messages": [
        # "<" and ">" are written as JSON escapes: the test template is rendered without HTML auto-escaping
        {"query": "Find buyers", "topic_key": "a", "results": [],
         "claude_answer": json.dumps(answer).replace("<", "\\u003c").replace(">", "\\u003e")}]})
    page, errors = _reopen(browser, name, chat)
    try:
        return page.evaluate(NOTE_JS), page.evaluate(STATE_JS), errors
    finally:
        page.close()


def _answer_with(statement, **extra):
    return dict(ANSWER, data_freshness={"statement": statement}, **extra)


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_shows_the_statement_under_the_answer(integrity_browser, name):
    text = "Stored posts were searched on 26 Oct 2026, 11:58 UTC. 3 stored posts were considered."
    notes, state, errors = _page(integrity_browser, name, _answer_with(text))
    assert [n["text"] for n in notes] == [text]
    assert notes[0]["tag"] == "P" and notes[0]["role"] == "note" and notes[0]["last"] is True
    assert state[0]["chipClass"] == "status-completed" and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_shows_nothing_extra_without_the_key(integrity_browser, name):
    notes, state, errors = _page(integrity_browser, name, ANSWER)
    assert notes == [] and state[0]["chipClass"] == "status-completed" and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("fd", [None, "text", 5, [], {}, {"statement": ""}, {"statement": "   "}, {"statement": 7},
                                {"statement": None}])
def test_browser_ignores_a_malformed_freshness_value(integrity_browser, name, fd):
    notes, state, errors = _page(integrity_browser, name, dict(ANSWER, data_freshness=fd))
    assert notes == [] and errors == [] and state[0]["chipClass"] == "status-completed"


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_renders_the_statement_as_text_only(integrity_browser, name):
    notes, _state, errors = _page(integrity_browser, name, _answer_with('<img src=x onerror="window.__x=1"><script>1</script>'))
    assert len(notes) == 1 and notes[0]["imgs"] == 0 and "<img" in notes[0]["text"] and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_a_freshness_only_answer_is_still_an_unavailable_answer(integrity_browser, name):
    answer = {"format": "source_list", "answer_status": "incomplete", "data_freshness": {"statement": "Some text."}}
    notes, state, errors = _page(integrity_browser, name, answer)
    assert notes == [] and state[0]["chip"] == "Answer unavailable" and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_works_with_the_no_results_layout(integrity_browser, name):
    answer = {"format": "no_results", "message": "Nothing matched.", "headline": "No qualifying posts found",
              "data_freshness": {"statement": "No stored posts matched in the last 3 days."}}
    notes, _state, errors = _page(integrity_browser, name, answer)
    assert [n["text"] for n in notes] == ["No stored posts matched in the last 3 days."] and errors == []


@pytest.mark.parametrize("name", TEMPLATES)
def test_browser_partial_answer_keeps_its_notice_and_gets_the_statement(integrity_browser, name):
    notes, state, errors = _page(integrity_browser, name, _answer_with("Statement.", answer_status="partial"))
    assert [n["text"] for n in notes] == ["Statement."] and state[0]["chip"] == "Partial answer" and errors == []


def test_both_templates_carry_the_same_renderer_and_meta_entry():
    for name in TEMPLATES:
        html = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "data_freshness: 1" in html and html.count("function appendFreshnessNote") == 1
        assert ".data-freshness {" in html
        assert html.count("appendFreshnessNote(data, renderedEl)") == 1
    a = (ROOT / "logics.py").read_text(encoding="utf-8")
    assert '"data_freshness"' in a.split("_ANSWER_META_KEYS =", 1)[1].split("\n", 1)[0]
