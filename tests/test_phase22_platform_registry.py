"""
tests/test_phase22_platform_registry.py
=======================================
PHASE 2.2 — the platform registry, the canonical signal, platform-aware cache keys, registry-driven labels.

No platform is enabled by these tests: Reddit is the only enabled entry, the others are only recognised.
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
from tests.test_platform_conformance import _reddit_record  # noqa: E402
from tests.test_recency_phase2 import CRM_Q, _answer, _mixed, _rows, _scope, _sig  # noqa: E402
from tests.test_strict_intent import lg  # noqa: E402,F401
from tests.test_time_ranges import app_env, env  # noqa: E402,F401

UTC = timezone.utc
D = timedelta
NOW = datetime.now(UTC)


# ═══ the registry ═══════════════════════════════════════════════════════════════════════════════════════════════════
def test_reddit_is_the_only_enabled_platform_and_existing_tables_are_unchanged(lg):
    assert platforms.enabled_keys() == ("reddit",) and lg.LIVE_PLATFORM_KEYS == ("reddit",)
    assert lg._PLATFORM_LABELS == {"x_twitter": "X / Twitter", "reddit": "Reddit", "linkedin": "LinkedIn", "facebook": "Facebook"}
    assert tuple(platforms.REGISTRY) == lg._PLATFORM_ORDER                                  # selection order is unchanged


@pytest.mark.parametrize("url,key", [
    ("https://www.reddit.com/r/a/comments/ab/t/", "reddit"), ("https://old.reddit.com/r/a/", "reddit"), ("https://redd.it/abc", "reddit"),
    ("reddit.com/r/a/comments/ab", "reddit"), ("https://x.com/u/status/1", "x_twitter"), ("https://mobile.twitter.com/u/status/1", "x_twitter"),
    ("https://www.linkedin.com/posts/x", "linkedin"), ("https://www.facebook.com/groups/1/posts/2", "facebook"), ("https://fb.com/p", "facebook"),
    ("https://dropbox.com/s/x", None), ("https://notreddit.com/r/a", None), ("https://example.com/x.com/foo", None),
    ("https://box.com/file", None), ("", None), (None, None), (5, None)])
def test_platform_detection_matches_the_host_not_a_substring(url, key):
    assert platforms.from_url(url) == key


def test_every_former_detection_site_now_agrees(lg):
    import importlib.util
    spec = importlib.util.spec_from_file_location("flintel_real_for_registry_test", ROOT / "flintel.py")
    flintel = importlib.util.module_from_spec(spec)        # the test environment may stub the `flintel` name; load the real file
    spec.loader.exec_module(flintel)
    for url in ("https://www.reddit.com/r/a/comments/ab/", "https://x.com/u/status/1", "https://twitter.com/u/status/1",
                "https://www.linkedin.com/p", "https://www.facebook.com/p", "https://fb.com/p", "https://dropbox.com/x", ""):
        legacy = lg._infer_platform_from_url(url)
        assert flintel._infer_platform_from_url(url) == legacy
        ai_name = ai.platform_of({"post_url": url})
        key = platforms.from_url(url)
        assert legacy == ({"x_twitter": "twitter"}.get(key, key))                                # logics.py token for X: "twitter"
        assert ai_name == ({"x_twitter": "x"}.get(key, key) or "unknown")                        # integrity token for X: "x"


def test_a_stored_platform_value_still_wins_over_the_url():
    assert ai.platform_of({"platform": "Twitter", "post_url": "https://reddit.com/r/a"}) == "x"
    assert ai.platform_of({"platform": "LinkedIn"}) == "linkedin" and ai.platform_of({"platform": "Bluesky"}) == "bluesky"
    assert ai.platform_of({}) == "unknown"


def test_the_earliest_valid_date_comes_from_the_registry_without_invented_cutoffs():
    from intent_prototype import opportunity
    assert platforms.earliest_date("reddit") == platforms.DEFAULT_EARLIEST_DATE == datetime(2005, 6, 1, tzinfo=UTC)
    assert all(platforms.earliest_date(k) == platforms.DEFAULT_EARLIEST_DATE for k in ("x_twitter", "linkedin", "facebook", None, "nope"))
    assert all(p.earliest_date in (None, platforms.DEFAULT_EARLIEST_DATE) for p in platforms.REGISTRY.values())
    assert timerange.PUBLICATION_FLOOR == opportunity._PUBLICATION_FLOOR == platforms.DEFAULT_EARLIEST_DATE


@pytest.mark.parametrize("a,b", [
    ("https://www.reddit.com/r/CRM/comments/AbC1/t/", "http://reddit.com/r/crm/comments/abc1"),
    ("https://old.reddit.com/r/CRM/comments/abc1/?x=1#y", "https://www.reddit.com/r/CRM/comments/abc1/t")])
def test_reddit_source_ids_ignore_scheme_host_case_query_and_slug(a, b):
    assert platforms.source_id({"post_url": a}) == platforms.source_id({"post_url": b}) == "reddit:abc1"


def test_other_platforms_use_the_normalised_url_and_missing_urls_give_no_id():
    assert platforms.source_id({"platform": "linkedin", "post_url": "https://WWW.linkedin.com/posts/x/?utm=1#a"}) == "linkedin.com/posts/x"
    assert platforms.source_id({"platform": "reddit"}) == "" and platforms.source_id({}) == "" and platforms.source_id(None) == ""


def test_community_reads_the_canonical_field_then_the_legacy_one_then_the_url():
    assert platforms.community({"platform": "reddit", "community": "A", "subreddit": "B"}) == "A"
    assert platforms.community({"platform": "reddit", "subreddit": "B"}) == "B"
    assert platforms.community({"post_url": "https://www.reddit.com/r/Fromurl/comments/ab/t"}) == "Fromurl"
    assert platforms.community({"platform": "linkedin", "subreddit": "x"}) is None                # a subreddit is Reddit's concept
    assert platforms.community({"platform": "linkedin", "community": "Group"}) == "Group"
    li = {"platform": "linkedin", "post_url": "https://linkedin.com/p"}
    assert ai.rules_for(li).community(li) is None and ai.rules_for(li).name == "generic"


def test_removed_content_markers_come_from_the_registry():
    assert platforms.is_removed({"platform": "reddit", "post_text": "[removed]"}) and platforms.is_removed(
        {"platform": "reddit", "title": "[Deleted]", "post_text": "x"})
    assert platforms.is_removed({"platform": "reddit", "removed_by_category": "moderator"})
    assert not platforms.is_removed({"platform": "linkedin", "post_text": "[removed]"})            # a Reddit marker, not LinkedIn's
    assert ai.verification_status({"platform": "reddit", "post_url": "u", "title": "t", "post_text": "[removed]"}) == "removed"


# ═══ the canonical signal ═══════════════════════════════════════════════════════════════════════════════════════════
def test_a_legacy_reddit_record_is_normalised_without_losing_anything():
    legacy = _reddit_record(1, NOW - D(days=2))
    n = platforms.normalize_signal(legacy)
    assert (n["platform"], n["url"], n["community"], n["source_id"]) == ("reddit", legacy["post_url"], "CRM", "reddit:abc1")
    assert n["subreddit"] == "CRM" and n["published_at"] == (NOW - D(days=2)).isoformat()      # backward-compatible alias kept
    assert all(n[k] == v for k, v in legacy.items())                                           # every original field intact
    assert "author_id" not in n and legacy.get("community") is None                            # nothing invented; input not mutated
    assert platforms.normalize_signal(n) == n                                                  # idempotent


def test_normalisation_never_fabricates():
    n = platforms.normalize_signal({"platform": "reddit", "title": "t"})
    assert set(n) == {"platform", "title"}                                                     # no url, id, community or date
    n = platforms.normalize_signal({"platform": "Twitter", "post_url": "https://x.com/u/status/1", "created_utc": "garbage"})
    assert n["platform"] == "x_twitter" and n["raw_platform"] == "Twitter" and "published_at" not in n and "community" not in n


@pytest.mark.parametrize("scope", [_scope(30), None], ids=["30-day window", "unbounded"])
def test_legacy_and_normalised_records_behave_identically_in_the_shared_logic(lg, scope):
    sigs = _mixed()
    normed = [platforms.normalize_signal(s) for s in sigs]
    a = ai.enforce_answer_integrity(_answer(sigs), sigs, sigs, CRM_Q, time_scope=scope)
    b = ai.enforce_answer_integrity(_answer(normed), normed, normed, CRM_Q, time_scope=scope)
    keep = ("link", "source_id", "platform", "date_status", "published_at", "in_window", "role", "qualified", "verification_status")
    assert [{k: r.get(k) for k in keep} for r in _rows(a)] == [{k: r.get(k) for k in keep} for r in _rows(b)]
    assert a["counts"] == b["counts"] and a["time_scope"] == b["time_scope"]
    window = lg._since_days_window(30)
    assert [lg._in_window(s, window) for s in sigs] == [lg._in_window(s, window) for s in normed]
    assert [timerange.post_published_at(s) for s in sigs] == [timerange.post_published_at(s) for s in normed]


# ═══ platform-aware cache keys ═══════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("a,b", [
    ("linkedin,x_twitter", "x_twitter,linkedin"), ("X / Twitter, LinkedIn", "linkedin, x/twitter"), ("twitter", "x"),
    ("x_twitter", "X / Twitter"), ("Reddit", "reddit"), ("reddit, reddit", "reddit")])
def test_equivalent_selections_give_equivalent_keys(a, b):
    assert platforms.selection_key(a) == platforms.selection_key(b)


@pytest.mark.parametrize("raw,key", [
    (None, "all"), ("", "all"), ("all", "all"), ("All Platforms", "all"), ("garbage", "all"), ("reddit", "reddit"),
    ("reddit,linkedin", "reddit,linkedin"), ("linkedin,reddit", "reddit,linkedin"), ("x_twitter,reddit,linkedin,facebook", "all"),
    ("x_twitter,linkedin", "x_twitter,linkedin")])
def test_default_and_canonical_selection_keys(raw, key):
    assert platforms.selection_key(raw) == key


def _cache(posts, platform_key=None, **extra):
    d = {"posts": posts, "keywords": ["k"], "match_phrases": ["p"]}
    if platform_key is not None:
        d["platform_key"] = platform_key
    d.update(extra)
    return d


class _M:
    def __init__(self, returns=()):
        self.returns, self.calls = list(returns), []

    def __call__(self, topic_key, keywords, targeting_platform="all", since_days=None, unfiltered=False, match_phrases=None, limit=None,
                 signals_collection_2=None, chat_id=None, signals_collection_4=None, user_query=None, time_range=None):
        self.calls.append((targeting_platform, since_days))
        return list(self.returns)


def _topup(lg, monkeypatch, cached, matcher, platform="all", saved=None, required=1, **kw):
    monkeypatch.setattr(lg, "get_cached_topic_evidence", lambda c, t: cached)
    monkeypatch.setattr(lg, "save_topic_evidence_cache", lambda *a, **k: (saved.update(args=a, kwargs=k) if saved is not None else None))
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: False)
    return lg.get_evidence_with_topup("c1", "o1", "topic", ["k"], required, matcher, match_phrases=["p"], targeting_platform=platform, **kw)


def _p(n, days=1, **extra):
    return dict(_reddit_record(n, None), published_at=(datetime.now(UTC) - D(days=days)).isoformat(), **extra)


def test_the_same_chat_and_keywords_for_a_different_platform_never_reuse_the_cache(lg, monkeypatch):
    cached = _cache([_p(1)], platform_key="reddit")
    m = _M(returns=[_p(9)])
    assert [p["post_url"] for p in _topup(lg, monkeypatch, cached, m, platform="linkedin")] == [_p(9)["post_url"]]
    assert m.calls == [("linkedin", None)]                                       # re-searched, and the reddit posts are not merged in
    m2 = _M()
    assert len(_topup(lg, monkeypatch, cached, m2, platform="reddit")) == 1 and m2.calls == []        # its own selection: served


def test_selection_order_and_aliases_do_not_split_the_cache(lg, monkeypatch):
    cached = _cache([_p(1)], platform_key="x_twitter,linkedin")
    for sel in ("linkedin,x_twitter", "X / Twitter, LinkedIn", "twitter,linkedin"):
        m = _M()
        assert len(_topup(lg, monkeypatch, cached, m, platform=sel)) == 1 and m.calls == [], sel


def test_default_and_empty_selection_use_the_all_key(lg, monkeypatch):
    saved = {}
    _topup(lg, monkeypatch, None, _M(returns=[_p(1)]), platform="", saved=saved)
    assert saved["kwargs"]["extra_fields"]["platform_key"] == "all"
    saved.clear()
    _topup(lg, monkeypatch, None, _M(returns=[_p(1)]), platform=None, saved=saved)
    assert saved["kwargs"]["extra_fields"]["platform_key"] == "all"
    m = _M()
    assert len(_topup(lg, monkeypatch, _cache([_p(1)], platform_key="all"), m, platform="")) == 1 and m.calls == []
    assert _topup(lg, monkeypatch, _cache([_p(1)], platform_key="reddit"), _M(), platform="all") == []      # reddit != all, kept apart


def test_the_saved_cache_record_stores_the_canonical_platform_key(lg, monkeypatch):
    saved = {}
    _topup(lg, monkeypatch, None, _M(returns=[_p(1)]), platform="LinkedIn, X / Twitter", saved=saved)
    assert saved["kwargs"]["extra_fields"] == {"platform_key": "x_twitter,linkedin"}
    saved.clear()
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: True)
    monkeypatch.setattr(lg, "get_cached_topic_evidence", lambda c, t: None)
    monkeypatch.setattr(lg, "save_topic_evidence_cache", lambda *a, **k: saved.update(kwargs=k))
    lg.get_evidence_with_topup("c1", "o1", "t", ["k"], 1, _M(returns=[_p(1)]), match_phrases=["p"], targeting_platform="reddit")
    assert saved["kwargs"]["extra_fields"] == {"platform_key": "reddit", "strict_mode": True}


def test_existing_reddit_caches_without_a_platform_key_keep_working(lg, monkeypatch):
    legacy = _cache([_p(1)])                                                       # written before the registry: Reddit-only product
    for sel in ("reddit", "all", "", None):
        m = _M()
        assert len(_topup(lg, monkeypatch, legacy, m, platform=sel)) == 1 and m.calls == [], sel
    for sel in ("linkedin", "x_twitter,linkedin"):
        m = _M()
        assert _topup(lg, monkeypatch, legacy, m, platform=sel) == [] and len(m.calls) == 1, sel


def test_the_window_filter_and_top_up_still_work_after_the_key_change(lg, monkeypatch):
    cached = _cache([_p(1, days=1), _p(2, days=300), dict(_p(3), published_at=None)], platform_key="reddit")
    m = _M(returns=[_p(9, days=2)])
    out = _topup(lg, monkeypatch, cached, m, platform="reddit", required=3, since_days=7)
    assert [p["post_url"] for p in out] == [_p(1)["post_url"], _p(9)["post_url"]]               # stale + undated dropped, top-up merged
    assert m.calls == [("reddit", 7)]
    m = _M()
    assert len(_topup(lg, monkeypatch, cached, m, platform="reddit", required=1, since_days=7)) == 1 and m.calls == []
    assert _topup(lg, monkeypatch, cached, _M(), platform="linkedin", required=1, since_days=7) == []        # other platform: nothing


def test_the_scan_deadline_fallback_reads_the_cache_through_the_same_rules(lg):
    cached = _cache([_p(1, days=1), _p(2, days=300)], platform_key="reddit")
    assert [p["post_url"] for p in lg.usable_cached_posts(cached, "reddit", 7)] == [_p(1)["post_url"]]
    assert lg.usable_cached_posts(cached, "linkedin", 7) == [] and len(lg.usable_cached_posts(cached, "reddit", None)) == 2
    assert lg.usable_cached_posts(None, "reddit", 7) == [] and lg.usable_cached_posts({"posts": "oops"}, "reddit", 7) == []
    rng = timerange.TimeRange(start=NOW - D(days=400), end=NOW - D(days=200), tz_name="UTC", kind="range", phrase="x", capped=False, note="")
    assert [p["post_url"] for p in lg.usable_cached_posts(cached, "reddit", None, rng.to_doc(NOW))] == [_p(2)["post_url"]]


# ═══ registry-driven labels ═══════════════════════════════════════════════════════════════════════════════════════
def test_the_answer_carries_the_registry_community_label_for_the_table():
    sigs = [_sig(1, "alpha", NOW - D(days=2))]
    out = ai.enforce_answer_integrity(_answer(sigs), sigs, sigs, CRM_Q)
    entry = out["platforms"][0]
    assert (entry["community_label"], entry["community_prefix"]) == ("Subreddit", "r/")
    assert platforms.community_display("linkedin") == ("Source", "") and platforms.community_display(None) == ("Source", "")


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_the_ui_uses_the_table_label_and_prefix_and_keeps_reddit_as_before(integrity_browser, name):
    from tests.test_recency_phase2 import _open
    sigs = [_sig(1, "alpha", NOW - D(days=2))]
    out = ai.enforce_answer_integrity(_answer(sigs), sigs, sigs, CRM_Q)
    out["platforms"][0]["posts"][0]["source"] = "CRM"
    page, ctx, errors = _open(integrity_browser, name, out)
    try:
        rows = page.evaluate("""() => [...document.querySelectorAll('.answer-rendered table tr')].map(r => [...r.children].map(c => c.innerText.trim()))""")
        assert rows[0][1] == "Subreddit" and rows[1][1] == "r/CRM" and errors == []
    finally:
        ctx.close()
    # an answer saved BEFORE the registry (no label / prefix fields) renders exactly the same
    legacy = json.loads(json.dumps(out))
    legacy["platforms"][0].pop("community_label"), legacy["platforms"][0].pop("community_prefix")
    page, ctx, errors = _open(integrity_browser, name, legacy)
    try:
        rows = page.evaluate("""() => [...document.querySelectorAll('.answer-rendered table tr')].map(r => [...r.children].map(c => c.innerText.trim()))""")
        assert rows[0][1] == "Subreddit" and rows[1][1] == "r/CRM"
    finally:
        ctx.close()
    # a table that names its community differently (synthetic unit test of the renderer; no such platform is enabled)
    other = json.loads(json.dumps(out))
    other["platforms"][0].update(platform="linkedin", community_label="Group", community_prefix="#")
    page, ctx, errors = _open(integrity_browser, name, other)
    try:
        rows = page.evaluate("""() => [...document.querySelectorAll('.answer-rendered table tr')].map(r => [...r.children].map(c => c.innerText.trim()))""")
        assert rows[0][1] == "Group" and rows[1][1] == "#CRM" and errors == []
    finally:
        ctx.close()


# ═══ added after the first mutation run (survivors: normalisation of an invalid stored date, the shared clock, route wiring) ═══
def test_an_invalid_published_at_is_dropped_by_normalisation_not_kept():
    for bad in ("garbage", "1970-01-01T00:00:00+00:00", (NOW + D(days=30)).isoformat(), None, ""):
        n = platforms.normalize_signal({"platform": "reddit", "post_url": "https://www.reddit.com/r/a/comments/ab/", "published_at": bad})
        assert "published_at" not in n, bad


def test_validation_and_window_share_one_clock_even_when_it_is_not_the_wall_clock(lg):
    # a fixed clock one year AHEAD of the wall clock: only a post validated against that same reading is a valid past time
    ahead = datetime.now(UTC) + D(days=365)
    post = {"platform": "reddit", "post_url": "https://www.reddit.com/r/a/comments/ab/t/", "created_utc": ahead - D(hours=1)}
    window = lg._since_days_window(7, now=ahead)
    assert lg._in_window(post, window, now=ahead) is True and lg._in_window(post, window) is True      # default = the window's clock
    assert timerange.post_published_at(post) is None                                                    # the wall clock disagrees
    rng = timerange.TimeRange(start=ahead - D(days=2), end=ahead, tz_name="UTC", kind="range", phrase="x", capped=False, note="")
    assert timerange.filter_posts([post], rng, now=ahead)[0] == [post]
    assert timerange.filter_posts([post], rng)[0] == []                                                 # without the shared clock: unknown


def test_the_scan_deadline_route_reads_the_cache_through_the_platform_and_window_rules(lg, routes_env, monkeypatch):
    import threading
    from tests.test_freshness_disclosure import SAVED_POSTS, _cancelled_scan, _stream_answer
    wrote, release, holder, calls = threading.Event(), threading.Event(), {}, []
    real = routes_env.routes.usable_cached_posts
    monkeypatch.setattr(routes_env.routes, "usable_cached_posts", lambda *a, **k: calls.append((a, k)) or real(*a, **k))
    answer, _ = _stream_answer(routes_env, monkeypatch, True, topup_override=_cancelled_scan(lg, wrote, release, holder),
                               nonblocking=True, response_timeout=1, cached_posts=SAVED_POSTS)
    release.set()
    wrote.wait(5)
    assert len(calls) == 1 and calls[0][0][0]["posts"]                           # the cached record went through the helper
    assert answer["data_freshness"]["counts"]["evidence_posts"] == 3             # legacy (unkeyed) Reddit cache: still served


from tests.test_scan_resilience import routes_env  # noqa: E402,F401
