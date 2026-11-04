"""
tests/test_hackernews_fetch.py — hackernews_live.py: URL building, normalisation, window verification, the bounded parallel fetch.
No network: the HTTP layer is a fake.
"""
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

import hackernews_live as hn
import timerange

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
END = NOW
START = END - timedelta(days=7)
S, E = int(START.timestamp()), int(END.timestamp())


def hit(i, ts, text="<p>Ask HN: need a CRM &amp; more</p><p>second para</p>", title="Ask HN: which CRM?", url=None):
    h = {"objectID": str(i), "title": title, "story_text": text, "created_at_i": int(ts)}
    if url:
        h["url"] = url
    return h


def fake_api(pages_by_query, calls=None):
    def get(url, timeout):
        if calls is not None:
            calls.append((url, timeout))
        q = parse_qs(urlparse(url).query)["query"][0]
        page = int(parse_qs(urlparse(url).query)["page"][0])
        data = pages_by_query[q]
        return data[page] if isinstance(data, list) else data
    return get


# ── URL building ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_url_without_a_window():
    u = urlparse(hn.build_url("crm tool"))
    q = parse_qs(u.query)
    assert u.netloc == "hn.algolia.com" and u.path == "/api/v1/search"
    assert q == {"query": ["crm tool"], "tags": ["story"], "hitsPerPage": ["50"], "page": ["0"]}


def test_url_with_the_half_open_window_and_by_date():
    u = urlparse(hn.build_url("crm", start=START, end=END, page=2, hits_per_page=30, by_date=True))
    q = parse_qs(u.query)
    assert u.path == "/api/v1/search_by_date" and q["page"] == ["2"] and q["hitsPerPage"] == ["30"]
    assert q["numericFilters"] == [f"created_at_i>={S},created_at_i<{E}"]          # start inclusive, end exclusive


def test_url_with_only_one_bound():
    assert parse_qs(urlparse(hn.build_url("x", start=START)).query)["numericFilters"] == [f"created_at_i>={S}"]
    assert parse_qs(urlparse(hn.build_url("x", end=END)).query)["numericFilters"] == [f"created_at_i<{E}"]


def test_query_is_url_encoded():
    assert "query=a+%26+b%3Dc" in hn.build_url("a & b=c")


# ── pool size: the Reddit wide-pool rule ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("limit,want", [(10, 100), (15, 100), (25, 100), (30, 120), (49, 196), (50, 200), (100, 200)])
def test_pool_size_is_the_reddit_rule(limit, want):
    assert hn.pool_size(limit, 4, 100, 200) == want


# ── windows: same convention as get_matched_signals ──────────────────────────────────────────────────────────────────────
def test_window_bounds_explicit_range_wins():
    tr = timerange.TimeRange(start=START, end=END, tz_name="UTC", kind="rolling", phrase="last 7 days")
    assert hn.window_bounds(30, tr, NOW) == (START, END)
    assert hn.window_bounds(30, tr.to_doc(NOW), NOW) == (START, END)


def test_window_bounds_since_days_and_unbounded():
    s, e = hn.window_bounds(7, None, NOW)
    assert s == NOW - timedelta(days=7) and e == NOW + timerange.FUTURE_TOLERANCE
    assert hn.window_bounds(None, None, NOW) == (None, None)
    assert hn.window_bounds(0, None, NOW) == (None, None) and hn.window_bounds(True, None, NOW) == (None, None)
    assert hn.window_bounds(99999, None, NOW, max_days=100)[0] == NOW - timedelta(days=100)


@pytest.mark.parametrize("ts,kept", [
    (S, True),                     # exactly start: included
    (S - 1, False),                # one second before
    (E - 1, True),
    (E, False),                    # exactly end: excluded
    (E + 3600, False),             # future relative to the window
])
def test_boundaries_match_the_existing_window_tests(ts, kept):
    rec = hn.normalize_hit(hit(1, ts))
    assert hn.in_window(rec, START, END, now=NOW + timedelta(days=1)) is kept


def test_missing_or_invalid_or_future_dates_are_never_in_a_window():
    assert hn.normalize_hit(dict(hit(1, S), created_at_i=None)) is None
    assert hn.normalize_hit(dict(hit(1, S), created_at_i="soon")) is None
    assert hn.normalize_hit(dict(hit(1, S), created_at_i=True)) is None
    future = hn.normalize_hit(hit(2, int((NOW + timedelta(days=3)).timestamp())))
    assert hn.in_window(future, None, None, now=NOW) is False                       # beyond now + tolerance
    ancient = hn.normalize_hit(hit(3, 0))
    assert hn.in_window(ancient, None, None, now=NOW) is False                      # 1970 placeholder


# ── normalisation ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_record_shape_and_html_stripping():
    r = hn.normalize_hit(hit(8863, S + 5, url="https://example.com/post"))
    assert r["platform"] == "hackernews" and r["post_url"] == "https://news.ycombinator.com/item?id=8863" and r["hn_id"] == "8863"
    assert r["title"] == "Ask HN: which CRM?" and r["external_url"] == "https://example.com/post"
    assert r["post_text"] == "Ask HN: need a CRM & more\n\nsecond para"
    assert r["created_utc"] == float(S + 5) and r["published_at"] == datetime.fromtimestamp(S + 5, UTC).isoformat()
    assert "subreddit" not in r


@pytest.mark.parametrize("raw,want", [
    ("<p>a &lt;b&gt; &amp; c</p>", "a <b> & c"), ("x<br>y", "x\ny"), ("<a href=\"u\">link</a> text", "link text"),
    ("  spaced   out  ", "spaced out"), ("", ""), (None, ""), (5, ""),
])
def test_strip_html(raw, want):
    assert hn.strip_html(raw) == want


@pytest.mark.parametrize("bad", [None, [], {}, {"objectID": "x1", "title": "t", "created_at_i": 1},
                                 {"objectID": "1", "title": "", "created_at_i": 1}, {"objectID": "1", "created_at_i": 1}])
def test_unusable_hits_are_dropped(bad):
    assert hn.normalize_hit(bad) is None


def test_text_required_rule():
    assert hn.is_candidate(hn.normalize_hit(hit(1, S))) is True
    assert hn.is_candidate(hn.normalize_hit(hit(2, S, text=None))) is False          # a link story has no body: not a match
    assert hn.is_candidate(hn.normalize_hit(hit(3, S, text="<p>  </p>"))) is False


# ── the fetch ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def run(pages, keywords=("crm", "sales"), **kw):
    kw.setdefault("pool_n", 50)
    kw.setdefault("start", START)
    kw.setdefault("end", END)
    kw.setdefault("now", NOW)
    return hn.fetch(list(keywords), http_get=fake_api(pages), **kw)


def test_dedup_no_text_and_window_are_all_applied_and_counted():
    pages = {"crm": {"hits": [hit(1, S), hit(2, S - 1), hit(3, E), hit(4, E - 1), hit(5, E - 5, text=None), hit(1, S)], "nbPages": 1},
             "sales": {"hits": [hit(11, S + 100), hit(1, S)], "nbPages": 1}}
    recs, st = run(pages)
    assert [r["hn_id"] for r in recs] == ["1", "11", "4"]
    assert st["state"] == "ok" and st["no_text"] == 1 and st["outside_window"] == 2 and st["duplicates"] == 2
    assert all(r["platform"] == "hackernews" for r in recs)


def test_keywords_are_merged_round_robin_and_capped_at_the_pool():
    pages = {"a": {"hits": [hit(1, S), hit(2, S), hit(3, S)], "nbPages": 1}, "b": {"hits": [hit(11, S), hit(12, S)], "nbPages": 1}}
    assert [r["hn_id"] for r in run(pages, ("a", "b"), pool_n=4)[0]] == ["1", "11", "2", "12"]
    assert [r["hn_id"] for r in run(pages, ("a", "b"), pool_n=2)[0]] == ["1", "11"]


def test_one_request_per_keyword_in_parallel():
    pages = {"a": {"hits": [hit(1, S)], "nbPages": 1}, "b": {"hits": [hit(2, S)], "nbPages": 1}, "c": {"hits": [hit(3, S)], "nbPages": 1}}
    started, lock, peak, live = [], threading.Lock(), [0], [0]

    def slow(url, timeout):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.15)
        with lock:
            live[0] -= 1
            started.append(url)
        return pages[parse_qs(urlparse(url).query)["query"][0]]
    recs, st = hn.fetch(["a", "b", "c"], pool_n=10, http_get=slow, now=NOW)
    assert len(recs) == 3 and st["requests"] == 3 and peak[0] >= 2


def test_paging_stops_at_the_cap_the_last_page_and_a_full_pool():
    calls = []
    pages = {"a": [{"hits": [hit(i, S) for i in range(1, 4)], "nbPages": 5}, {"hits": [hit(i, S) for i in range(4, 7)], "nbPages": 5},
                   {"hits": [hit(i, S) for i in range(7, 10)], "nbPages": 5}, {"hits": [hit(99, S)], "nbPages": 5}]}
    recs, _ = hn.fetch(["a"], pool_n=100, http_get=fake_api(pages, calls), max_pages=3, now=NOW)
    assert len(calls) == 3 and len(recs) == 9                                  # page cap
    calls.clear()
    recs, _ = hn.fetch(["a"], pool_n=4, http_get=fake_api(pages, calls), max_pages=3, now=NOW)
    assert len(recs) == 4 and len(calls) == 2                                  # pool full -> no more pages
    calls.clear()
    one = {"a": {"hits": [hit(1, S)], "nbPages": 1}}
    hn.fetch(["a"], pool_n=50, http_get=fake_api(one, calls), max_pages=3, now=NOW)
    assert len(calls) == 1                                                     # nbPages reached


def test_failure_states_are_honest():
    boom = lambda url, timeout: (_ for _ in ()).throw(RuntimeError("x"))
    recs, st = hn.fetch(["a"], pool_n=5, http_get=boom, deadline_seconds=3, now=NOW)
    assert recs == [] and st["state"] == "failed" and st["reason"] == "all_requests_failed"
    assert hn.fetch([], pool_n=5)[1]["state"] == "skipped" and hn.fetch(["a"], pool_n=0)[1]["reason"] == "no_pool"
    limited = lambda url, timeout: (_ for _ in ()).throw(hn.RateLimited())
    recs, st = hn.fetch(["a", "b"], pool_n=5, http_get=limited, now=NOW)
    assert recs == [] and st["state"] == "rate_limited"
    empty = {"a": {"hits": [], "nbPages": 0}}
    assert hn.fetch(["a"], pool_n=5, http_get=fake_api(empty), now=NOW)[1]["state"] == "empty"


def test_partial_when_one_keyword_fails():
    good = {"hits": [hit(1, S)], "nbPages": 1}

    def get(url, timeout):
        if "query=bad" in url:
            raise RuntimeError("down")
        return good
    recs, st = hn.fetch(["good", "bad"], pool_n=5, http_get=get, now=NOW)
    assert [r["hn_id"] for r in recs] == ["1"] and st["state"] == "partial" and st["failed_requests"] >= 1


def test_a_transient_error_is_retried_once():
    n = {"c": 0}

    def flaky(url, timeout):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("blip")
        return {"hits": [hit(1, S)], "nbPages": 1}
    recs, st = hn.fetch(["a"], pool_n=5, http_get=flaky, deadline_seconds=10, now=NOW)
    assert len(recs) == 1 and st["state"] == "partial" and st["requests"] == 2


def test_the_hard_deadline_returns_in_time_and_never_raises():
    def hang(url, timeout):
        time.sleep(3)
        return {"hits": [hit(1, S)], "nbPages": 1}
    t0 = time.monotonic()
    recs, st = hn.fetch(["a", "b"], pool_n=5, http_get=hang, deadline_seconds=0.5, now=NOW)
    assert time.monotonic() - t0 < 2.0 and recs == [] and st["state"] == "failed" and st["reason"] == "timeout"


def test_per_request_timeout_is_bounded_by_the_cap_and_the_deadline():
    seen = []
    hn.fetch(["a"], pool_n=5, http_get=lambda u, t: seen.append(t) or {"hits": [], "nbPages": 0}, deadline_seconds=30, now=NOW)
    assert seen and seen[0] <= hn.PER_REQUEST_TIMEOUT_CAP
    seen.clear()
    hn.fetch(["a"], pool_n=5, http_get=lambda u, t: seen.append(t) or {"hits": [], "nbPages": 0}, deadline_seconds=2, now=NOW)
    assert seen[0] <= 2.0


def test_the_default_http_layer_identifies_itself_and_is_not_used_when_a_fake_is_given(monkeypatch):
    called = []
    monkeypatch.setattr(hn, "_default_http_get", lambda *a, **k: called.append(1) or {"hits": []})
    hn.fetch(["a"], pool_n=5, http_get=lambda u, t: {"hits": [], "nbPages": 0}, now=NOW)
    assert called == [] and "Flintel" in hn.USER_AGENT


def test_query_cleaning():
    assert hn.clean_queries(["  crm  ", "CRM", "", None, 5, "sales  tool"]) == ["crm", "sales tool"]
    assert len(hn.clean_queries([f"k{i}" for i in range(50)])) == hn.MAX_QUERIES
