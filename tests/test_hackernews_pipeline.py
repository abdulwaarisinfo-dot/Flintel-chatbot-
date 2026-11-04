"""
tests/test_hackernews_pipeline.py — Hacker News inside get_matched_signals(): HN-only (no Reddit stores touched), Reddit + HN (parallel,
one merged intent-bridge call), time windows, Active Intent window, fallbacks, caching, flag-off parity. All offline: the Algolia
fetch is a fake and the Reddit stores are the repo's own test doubles (tests/test_strict_intent.py).
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import active_intent as ain
import hackernews_live
import platforms
import timerange
from tests.test_strict_intent import Harness, _mkdoc, _set_cfg, ib, lg  # noqa: F401  (lg: module-scoped fixture)
from tests.test_reliability_ux_perf import env  # noqa: F401  (fixture: stubbed app modules, real index)

UTC = timezone.utc
NOW = datetime.now(UTC)
QUERY = "find people asking for a CRM"


def hn_rec(i, title=None, ts=None):
    ts = ts or int((NOW - timedelta(hours=3)).timestamp())
    return {"title": title or f"Ask HN {i}", "post_text": f"body of hn story {i} about a crm", "post_url": f"https://news.ycombinator.com/item?id={i}",
            "platform": "hackernews", "created_utc": float(ts), "hn_id": str(i),
            "published_at": datetime.fromtimestamp(ts, UTC).isoformat()}


class FakeFetch:
    """Replaces hackernews_live.fetch: records every call, returns `recs` (or raises / sleeps)."""
    def __init__(self, recs=(), status=None, delay=0.0, boom=False):
        self.recs, self.delay, self.boom, self.calls = list(recs), delay, boom, []
        self.status = status or {"state": "ok", "requests": 1, "failed_requests": 0, "fetched_hits": len(self.recs), "no_text": 0,
                                 "outside_window": 0, "duplicates": 0, "reason": None}
        self.started = threading.Event()

    def __call__(self, keywords, **kw):
        self.calls.append(dict(kw, keywords=list(keywords)))
        self.started.set()
        if self.delay:
            time.sleep(self.delay)
        if self.boom:
            raise RuntimeError("boom")
        return [dict(r) for r in self.recs], dict(self.status)


@pytest.fixture
def hn(lg, monkeypatch):
    """Hacker News registered (flag on) + logics' import-time maps rebuilt, HN fetch faked, caches cleared."""
    before = dict(platforms.REGISTRY)
    platforms.apply_flags(hackernews=True)
    monkeypatch.setattr(lg, "_PLATFORM_ORDER", ("x_twitter", "reddit", "hackernews", "linkedin", "facebook"))
    monkeypatch.setitem(lg.PLATFORM_KEY_MAP, "hacker news", "hackernews")
    monkeypatch.setitem(lg.PLATFORM_KEY_MAP, "hackernews", "hackernews")
    monkeypatch.setitem(lg.PLATFORM_KEY_MAP, "hn", "hackernews")
    monkeypatch.setattr(lg, "LIVE_PLATFORM_KEYS", platforms.enabled_keys())
    monkeypatch.setattr(lg, "_PLATFORM_LABELS", {k: p.label for k, p in platforms.REGISTRY.items()})
    lg._HN_CACHE.clear()
    lg._HN_STATUS.clear()
    fake = FakeFetch([hn_rec(1), hn_rec(2)])
    monkeypatch.setattr(hackernews_live, "fetch", fake)
    _set_cfg(monkeypatch, HN_FETCH_TIMEOUT_SECONDS=12, HN_MAX_PAGES_PER_KEYWORD=3, HN_HITS_PER_PAGE=50,
             INTENT_CANDIDATE_MULTIPLIER=4, INTENT_CANDIDATE_MIN=100, INTENT_CANDIDATE_MAX=200)
    try:
        yield fake
    finally:
        platforms.apply_flags(hackernews=False)
        lg._HN_CACHE.clear()
        lg._HN_STATUS.clear()
        assert dict(platforms.REGISTRY) == before


class Exploding:
    """A Reddit store that fails the test if anything touches it."""
    def __getattr__(self, name):
        raise AssertionError(f"Reddit store touched: {name}")


def no_reddit(lg, monkeypatch):
    monkeypatch.setattr(lg, "_get_matched_signals_core", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Reddit core called")))
    monkeypatch.setattr(lg, "signals_collection", Exploding())
    monkeypatch.setattr(lg, "generate_query_embeddings_batch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("embedding called")))


# ── flag off = pass-through ──────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("target", ["reddit", "all", None, "hackernews", "reddit,hackernews"])
def test_flag_off_is_a_pure_passthrough(lg, monkeypatch, target):
    assert platforms.get("hackernews") is None
    seen = []
    monkeypatch.setattr(lg, "_get_matched_signals_core", lambda *a, **k: seen.append((a, k)) or ["R"])
    fake = FakeFetch([hn_rec(1)])
    monkeypatch.setattr(hackernews_live, "fetch", fake)
    out = lg.get_matched_signals("t", ["crm"], target, 15, None, False, None, False, None, "c", None, QUERY, None, None, None)
    assert out == ["R"] and fake.calls == [] and len(seen) == 1
    args, kwargs = seen[0]
    assert "_hn_box" not in kwargs and args[2] == target and args[3] == 15 and args[11] == QUERY


def test_flag_off_helpers_say_no(lg):
    assert lg._hn_wanted("hackernews") == (False, False) and lg.hackernews_only("hackernews") is False
    assert lg.hackernews_note("c", "t") is None


# ── HN alone ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("limit,pool", [(10, 100), (15, 100), (30, 120), (60, 200)])
def test_hn_only_touches_no_reddit_store_and_sizes_the_pool_like_reddit(lg, hn, monkeypatch, limit, pool):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=False, STRICT_INTENT_MODE=False)
    out = lg.get_matched_signals("t", ["crm", "sales tool"], "hackernews", limit, None, False, ["phrase"], False, None, "c", None, QUERY, None, None)
    (call,) = hn.calls
    assert call["keywords"] == ["crm", "sales tool"] and call["pool_n"] == pool            # the router's own keywords; the Reddit sizing rule
    assert [r["platform"] for r in out] == ["hackernews", "hackernews"] and len(out) <= limit


def test_hn_only_candidates_go_to_the_same_intent_bridge_once(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=True, INCREMENTAL_RESCAN_ENABLED=True)
    seen = []

    def fake(user_query, candidates, limit, topic_sims=None, plan_key=None):
        seen.append((user_query, [c["post_url"] for c in candidates], limit, topic_sims, plan_key))
        return list(reversed(candidates))
    monkeypatch.setattr(ib, "rerank_with_intent", fake)
    out = lg.get_matched_signals("t", ["crm"], "hackernews", 15, None, False, None, False, None, "chat1", None, QUERY, None, None)
    assert len(seen) == 1
    uq, urls, limit, sims, plan_key = seen[0]
    assert uq == QUERY and limit == 15 and sims == [0.0, 0.0] and plan_key == ("chat1", "t")
    assert [m["post_url"] for m in out] == list(reversed(urls))                      # the bridge's order is kept
    assert all(m["platform"] == "hackernews" for m in out)


def test_hn_only_bridge_failure_falls_back_to_the_pool_never_fewer_posts(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=True)
    monkeypatch.setattr(ib, "rerank_with_intent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    out = lg.get_matched_signals("t", ["crm"], "hackernews", 15, None, False, None, False, None, "c", None, QUERY, None, None)
    assert [m["hn_id"] for m in out] == ["1", "2"]


@pytest.mark.parametrize("failure", ["raise", "empty"])
def test_hn_only_failure_returns_nothing_without_crashing(lg, hn, monkeypatch, failure):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=False)
    monkeypatch.setattr(hackernews_live, "fetch", FakeFetch([], status={"state": "failed", "reason": "all_requests_failed"}, boom=(failure == "raise")))
    assert lg.get_matched_signals("t", ["crm"], "hackernews", 15, None, False, None, False, None, "c", None, QUERY, None, None) == []
    assert "could not be searched" in lg.hackernews_note("c", "t")


def test_hn_only_scan_state_never_stores_incremental_state(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=False)
    st = {}
    lg.get_matched_signals("t", ["crm"], "hackernews", 15, None, False, None, False, None, "c", None, QUERY, st, None)
    assert st["delta_mode"] is False and st["sims"] == {} and "complete" not in st and st["hackernews"]["state"] == "ok"


# ── Reddit + HN ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def reddit_docs():
    t = datetime.now(UTC)
    return [_mkdoc(i, 0.9, inserted=t - timedelta(minutes=i), created=t - timedelta(hours=i)) for i in (1, 2, 3)]


def test_both_selected_merges_one_pool_and_calls_the_bridge_exactly_once(lg, hn, monkeypatch):
    h = Harness(lg, monkeypatch, reddit_docs(), INCREMENTAL_RESCAN_ENABLED=False, INTENT_BRIDGE_ENABLED=True)
    seen = []

    def fake(user_query, candidates, limit, topic_sims=None, plan_key=None):
        seen.append((list(candidates), topic_sims))
        return sorted(candidates, key=lambda c: c["title"])
    monkeypatch.setattr(ib, "rerank_with_intent", fake)
    out = lg.get_matched_signals("t", ["crm"], "reddit,hackernews", 15, None, False, ["x"], False, None, "c", None, QUERY, None, None)
    assert len(seen) == 1                                                             # ONE prototype call on the merged pool
    cands, sims = seen[0]
    assert sorted({c["platform"] for c in cands}) == ["hackernews", "reddit"] and len(cands) == 5
    assert len(sims) == len(cands) and sorted(sims)[:2] == [0.0, 0.0] and sorted(sims)[2] > 0.5       # HN has no embedding similarity
    assert {m["platform"] for m in out} == {"reddit", "hackernews"} and len(out) == 5
    assert len(hn.calls) == 1 and hn.calls[0]["pool_n"] == 100                          # its own 4x pool, clamped
    assert "searched live" in lg.hackernews_note("c", "t")


def test_the_hn_fetch_starts_before_the_reddit_scan_reads(lg, hn, monkeypatch):
    h = Harness(lg, monkeypatch, reddit_docs(), INCREMENTAL_RESCAN_ENABLED=False, INTENT_BRIDGE_ENABLED=True)
    real_find = h.coll.find
    order = []

    def find(*a, **k):
        order.append(("reddit_read", hn.started.wait(2)))                          # HN must already be running (in parallel)
        return real_find(*a, **k)
    monkeypatch.setattr(h.coll, "find", find)
    monkeypatch.setattr(ib, "rerank_with_intent", lambda q, c, n, topic_sims=None, plan_key=None: list(c))
    lg.get_matched_signals("t", ["crm"], "reddit,hackernews", 15, None, False, ["x"], False, None, "c", None, QUERY, None, None)
    assert order and all(started for _n, started in order)


def test_a_failing_or_slow_hn_branch_leaves_the_reddit_result_exactly_as_without_it(lg, hn, monkeypatch):
    h = Harness(lg, monkeypatch, reddit_docs(), INCREMENTAL_RESCAN_ENABLED=False, INTENT_BRIDGE_ENABLED=True)
    monkeypatch.setattr(ib, "rerank_with_intent", lambda q, c, n, topic_sims=None, plan_key=None: sorted(c, key=lambda x: x["title"]))
    base = lg.get_matched_signals("t", ["crm"], "reddit", 15, None, False, ["x"], False, None, "c", None, QUERY, None, None)
    monkeypatch.setattr(hackernews_live, "fetch", FakeFetch(boom=True))
    lg._HN_CACHE.clear()
    broken = lg.get_matched_signals("t", ["crm"], "reddit,hackernews", 15, None, False, ["x"], False, None, "c", None, QUERY, None, None)
    assert [m["post_url"] for m in broken] == [m["post_url"] for m in base] and len(base) == 3
    assert "could not be searched" in lg.hackernews_note("c", "t")


def test_a_hung_hn_branch_is_left_behind_after_its_own_deadline(lg, hn, monkeypatch):
    Harness(lg, monkeypatch, reddit_docs(), INCREMENTAL_RESCAN_ENABLED=False, INTENT_BRIDGE_ENABLED=True)
    _set_cfg(monkeypatch, HN_FETCH_TIMEOUT_SECONDS=1)
    monkeypatch.setattr(hackernews_live, "fetch", FakeFetch([hn_rec(9)], delay=6.0))
    monkeypatch.setattr(ib, "rerank_with_intent", lambda q, c, n, topic_sims=None, plan_key=None: list(c))
    t0 = time.monotonic()
    out = lg.get_matched_signals("t", ["crm"], "reddit,hackernews", 15, None, False, ["x"], False, None, "c", None, QUERY, None, None)
    assert time.monotonic() - t0 < 5.5 and len(out) == 3 and all(m["platform"] == "reddit" for m in out)
    assert "timeout" in lg.hackernews_note("c", "t")


def test_duplicate_urls_across_platforms_are_deduplicated(lg, hn, monkeypatch):
    docs = reddit_docs()
    Harness(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, INTENT_BRIDGE_ENABLED=True)
    monkeypatch.setattr(hackernews_live, "fetch", FakeFetch([hn_rec(1), hn_rec(1), hn_rec(2)]))
    monkeypatch.setattr(ib, "rerank_with_intent", lambda q, c, n, topic_sims=None, plan_key=None: list(c))
    out = lg.get_matched_signals("t", ["crm"], "reddit,hackernews", 15, None, False, ["x"], False, None, "c", None, QUERY, None, None)
    urls = [m["post_url"] for m in out]
    assert len(urls) == len(set(urls)) == 5


def test_reddit_empty_before_the_hook_then_the_hn_pool_is_ranked_on_its_own(lg, hn, monkeypatch):
    Harness(lg, monkeypatch, [], INCREMENTAL_RESCAN_ENABLED=False, INTENT_BRIDGE_ENABLED=True)
    seen = []
    monkeypatch.setattr(ib, "rerank_with_intent", lambda q, c, n, topic_sims=None, plan_key=None: seen.append(len(c)) or list(c))
    out = lg.get_matched_signals("t", ["crm"], "reddit,hackernews", 15, None, False, ["x"], False, None, "c", None, QUERY, None, None)
    assert [m["hn_id"] for m in out] == ["1", "2"] and seen == [2]                      # still exactly one bridge call


def test_an_unchanged_incremental_rescan_returns_nothing_as_before(lg, hn, monkeypatch):
    def core(*a, **k):
        a[12]["unchanged"] = True
        return []
    monkeypatch.setattr(lg, "_get_matched_signals_core", core)
    assert lg.get_matched_signals("t", ["crm"], "reddit,hackernews", 15, None, False, None, False, None, "c", None, QUERY, {}, None) == []


def test_bridge_off_hn_only_fills_the_slots_reddit_left_empty(lg, hn, monkeypatch):
    Harness(lg, monkeypatch, reddit_docs(), INCREMENTAL_RESCAN_ENABLED=False, INTENT_BRIDGE_ENABLED=False)
    out = lg.get_matched_signals("t", ["crm"], "reddit,hackernews", 4, None, False, ["x"], False, None, "c", None, QUERY, None, None)
    assert [m["platform"] for m in out].count("reddit") == 3 and len(out) == 4          # Reddit's three first, HN fills the last slot
    full = lg.get_matched_signals("t", ["crm"], "reddit,hackernews", 3, None, False, ["x"], False, None, "c", None, QUERY, None, None)
    assert [m["platform"] for m in full] == ["reddit"] * 3                              # Reddit is never displaced


def test_unfiltered_requests_keep_the_reddit_path_and_do_not_start_hn(lg, hn, monkeypatch):
    monkeypatch.setattr(lg, "_get_matched_signals_core", lambda *a, **k: ["R"])
    assert lg.get_matched_signals("t", ["crm"], "reddit,hackernews", 15, None, True, None, False, None, "c", None, QUERY, None, None) == ["R"]
    assert hn.calls == []


# ── time windows ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_last_7_days_restricts_hn_to_exactly_the_reddit_window(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=False)
    end = datetime.now(UTC).replace(microsecond=0)
    tr = timerange.TimeRange(start=end - timedelta(days=7), end=end, tz_name="UTC", kind="rolling", phrase="last 7 days")
    lg.get_matched_signals("t", ["crm"], "hackernews", 15, None, False, None, False, None, "c", None, QUERY, None, tr.to_doc(end))
    (call,) = hn.calls
    assert call["start"] == tr.start and call["end"] == tr.end and call["by_date"] is False


def test_router_period_uses_the_same_since_days_window_as_reddit(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=False)
    lg.get_matched_signals("t", ["crm"], "hackernews", 15, 7, False, None, False, None, "c", None, QUERY, None, None)
    (call,) = hn.calls
    assert abs((call["start"] - (datetime.now(UTC) - timedelta(days=7))).total_seconds()) < 5
    assert abs((call["end"] - (datetime.now(UTC) + timerange.FUTURE_TOLERANCE)).total_seconds()) < 5


def test_no_period_means_an_unbounded_search(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=False)
    lg.get_matched_signals("t", ["crm"], "hackernews", 15, None, False, None, False, None, "c", None, QUERY, None, None)
    assert (hn.calls[0]["start"], hn.calls[0]["end"]) == (None, None)


# ── Active Intent window (1500 minutes) ──────────────────────────────────────────────────────────────────────────────────
def test_the_active_intent_window_bounds_the_hn_fetch_and_uses_date_order(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=False)
    end = datetime.now(UTC).replace(microsecond=0)
    win = ain.recent_window(end, 1500)
    lg.get_matched_signals("t", ["crm"], "hackernews", 15, None, False, None, False, None, "c", None, QUERY, None, win.to_doc(end))
    (call,) = hn.calls
    assert call["end"] - call["start"] == timedelta(minutes=1500) and call["end"] == end and call["by_date"] is True


def test_a_users_own_range_is_never_replaced_by_the_window(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=False)
    end = datetime.now(UTC).replace(microsecond=0)
    mine = timerange.TimeRange(start=end - timedelta(days=3), end=end, tz_name="UTC", kind="rolling", phrase="last 3 days")
    lg.get_matched_signals("t", ["crm"], "hackernews", 15, None, False, None, False, None, "c", None, QUERY, None, mine.to_doc(end))
    assert hn.calls[0]["start"] == mine.start and hn.calls[0]["by_date"] is False


@pytest.mark.parametrize("value", [0, -5, 10081, True, "1500", None])
def test_window_validation_still_fails_closed(monkeypatch, value):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", value, raising=False)
    minutes, why = ain.window_setting()
    assert minutes is None and why in ("invalid", "over_maximum")


@pytest.mark.parametrize("value", [1, 30, 1500, 10080])
def test_window_validation_accepts_the_documented_range(monkeypatch, value):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", value, raising=False)
    assert ain.window_setting() == (value, "ok")


# ── caching, selection plumbing, notes, jobs ─────────────────────────────────────────────────────────────────────────────
def test_repeated_polls_reuse_the_pool_for_the_ttl_but_a_new_window_refetches(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=False)
    for _ in range(3):
        lg.get_matched_signals("t", ["crm"], "hackernews", 15, None, False, None, False, None, "c", None, QUERY, None, None)
    assert len(hn.calls) == 1
    lg.get_matched_signals("t", ["crm"], "hackernews", 15, 7, False, None, False, None, "c", None, QUERY, None, None)
    assert len(hn.calls) == 2
    monkeypatch.setattr(lg, "_HN_TTL_OK", -1.0)
    lg._HN_CACHE.clear()
    lg.get_matched_signals("t", ["crm"], "hackernews", 15, None, False, None, False, None, "c", None, QUERY, None, None)
    assert len(hn.calls) == 3


def test_selection_plumbing(lg, hn):
    assert lg.supported_platforms("Hacker News") == "hackernews"
    assert lg.supported_platforms("Reddit, Hacker News") == "reddit,hackernews"
    assert lg.supported_platforms("All Platforms") == "reddit,hackernews"
    assert lg.supported_platforms("Reddit") == "reddit"
    assert lg.unsupported_requested("X / Twitter, Hacker News") == (["X / Twitter"], True)
    assert lg.unsupported_requested("Hacker News") == ([], True)
    assert lg.platform_display_value("reddit,hackernews") == "Reddit, Hacker News"
    assert lg.platform_display_value("hackernews") == "Hacker News"
    assert lg._signal_platform_matches({"platform": "hackernews"}, "hackernews") is True
    assert lg._signal_platform_matches({"platform": "reddit"}, "hackernews") is False
    assert lg._signal_platform_matches({"post_url": "https://news.ycombinator.com/item?id=5"}, "reddit,hackernews") is True


def test_hackernews_only_and_jobs(lg, hn, monkeypatch):
    assert lg.hackernews_only("hackernews") is True
    for other in ("reddit", "reddit,hackernews", "all", None, ""):
        assert lg.hackernews_only(other) is False
    seen = []

    class Jobs:
        def update_one(self, flt, update, upsert=False):
            seen.append(update["$set"]["targeting_platform"])
    monkeypatch.setattr(lg, "jobs_collection", Jobs())
    lg.enqueue_search_job("t", ["k"], "reddit,hackernews")
    lg.enqueue_search_job("t", ["k"], "reddit")
    assert seen == ["all", "reddit"]                                                  # the collector contract is unchanged


def test_routes_skip_the_collector_job_for_hn_alone():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "routes.py").read_text(encoding="utf-8")
    assert len(re.findall(r"hackernews_only\(targeting_platform\)", src)) == 3


@pytest.mark.parametrize("status,needle", [
    ({"state": "ok", "candidates": 4}, "searched live this time (4 candidates"),
    ({"state": "partial", "candidates": 1}, "only partly searched"),
    ({"state": "empty", "candidates": 0}, "no qualifying stories"),
    ({"state": "skipped"}, "not searched"),
    ({"state": "failed", "reason": "timeout"}, "could not be searched this time (timeout)"),
    ({"state": "rate_limited", "reason": "rate_limited"}, "could not be searched"),
])
def test_the_per_platform_note_is_honest(lg, status, needle):
    lg._HN_STATUS[("c", "t")] = status
    assert needle in lg.hackernews_note("c", "t")
    lg._HN_STATUS.clear()
    assert lg.hackernews_note("c", "t") is None


def test_freshness_statement_names_hn_only_when_it_was_part_of_the_search(lg):
    ref = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
    base = lg.build_data_freshness([], ref)
    assert base["statement"].endswith("this is not a complete view of Reddit.")
    with_note = lg.build_data_freshness([], ref, platform_note="Hacker News could not be searched this time (timeout); this answer does not include it.")
    assert "Hacker News could not be searched" in with_note["statement"] and with_note["statement"].endswith("Reddit or Hacker News.")
    assert {k: v for k, v in base.items() if k != "statement"} == {k: v for k, v in with_note.items() if k != "statement"}


# ── no Reddit-discovery (Google) call for Hacker News alone ──────────────────────────────────────────────────────────────
def test_hn_alone_makes_no_google_discovery_call_but_other_selections_still_do(env, monkeypatch):
    app_lg, idx, _chats = env
    pf = app_lg.platforms                         # the platforms module THIS index/logics pair uses (the env fixture swaps its own copy)
    calls = []
    monkeypatch.setattr(idx.google_search, "search_google_for_reddit_posts", lambda *a, **k: calls.append((a, k)) or [], raising=False)
    monkeypatch.setattr(idx, "_turn_stopped_now", lambda *a, **k: False, raising=False)
    base = {"topic_key": "t", "keywords": ["crm"], "request_id": "r1"}
    pf.apply_flags(hackernews=True)
    try:
        idx._trigger_google_fallback_search("c", "o@x.com", dict(base, targeting_platform="hackernews"))
        assert calls == []
        for other in ("reddit", "reddit,hackernews", "all", None):
            idx._trigger_google_fallback_search("c", "o@x.com", dict(base, targeting_platform=other))
        assert len(calls) == 4
    finally:
        pf.apply_flags(hackernews=False)
    calls.clear()                                                                      # flag off: even "hackernews" is just an unknown value
    idx._trigger_google_fallback_search("c", "o@x.com", dict(base, targeting_platform="hackernews"))
    assert len(calls) == 1


# ── the app's statements about itself stay true ─────────────────────────────────────────────────────────────────────────────
def test_capability_statements_name_hacker_news_only_through_the_flag_gated_rewrite(lg):
    how, plat, ver = lg._with_hackernews_capabilities(lg.SMALLTALK_REPLIES["how_it_works"], lg.SMALLTALK_REPLIES["platforms"],
                                                      lg.VERIFIED_CAPABILITIES_TEXT)
    assert "Hacker News searched live" in how and "Hacker News searched live" in ver and "**Hacker News** (searched live)" in plat
    for text in (how, plat, ver):
        assert "X / Twitter" in text and "coming soon" in text                       # the still-disabled platforms are still "coming soon"
    # flag off: the constants in this module are exactly the old statements (no Hacker News anywhere)
    assert platforms.get("hackernews") is None
    for text in (lg.SMALLTALK_REPLIES["how_it_works"], lg.SMALLTALK_REPLIES["platforms"], lg.VERIFIED_CAPABILITIES_TEXT):
        assert "Hacker News" not in text
    assert "Reddit today;" in lg.VERIFIED_CAPABILITIES_TEXT
