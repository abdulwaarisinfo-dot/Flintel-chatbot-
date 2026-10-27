"""
tests/platform_conformance.py — the contract a platform adapter must satisfy (Phase 2.2).
=========================================================================================

A future platform is added to the platform registry (platforms.py) and gets a collector OUTSIDE this repository's shared
engine. It must not reimplement freshness, ranking or evidence integrity. To prove that the shared engine works for it, add
one `AdapterFixture` to tests/test_platform_conformance.py::ADAPTERS; every check below then runs against it.

The fixture's records are SYNTHETIC test data written for these checks — they are not collected data and are never presented
as such. Today ADAPTERS holds the Reddit fixture only; no other platform is enabled or exercised.

What the contract proves: given records shaped like the adapter's stored records, the shared code (a) identifies the platform,
(b) derives one stable source id from equivalent URLs, (c) accepts only plausible publication times and treats a missing one as
unknown, (d) exposes community / optional author metadata without inventing it, (e) filters by a half-open window, (f) keeps
every shown row attributable to its stored record, and (g) never serves one platform selection's cache to another.
What it does NOT prove: that a real collector produces such records, that the platform's data is complete or fresh, that its
text is classified well by the (English-language, Reddit-tuned) purchase-intent rules, or anything about its API or rate limits.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

import answer_integrity as ai
import platforms
import timerange

UTC = timezone.utc
D = timedelta
N = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)          # the fixed clock every conformance check uses


@dataclass(frozen=True)
class AdapterFixture:
    key: str                                             # registry key
    make_record: Callable                                # (n, when, **extra) -> a stored-shaped record; `when` None = no date
    equivalent_urls: Tuple[str, ...]                     # URLs of ONE post in different spellings (scheme, www, query, slash)
    other_url: str                                       # a URL of a different post
    expected_community: Optional[str] = None             # community make_record(...) carries (None: the platform has none)
    expected_source_id: Optional[str] = None             # id equivalent_urls must normalise to (None: the normalised URL)
    other_selection: str = "linkedin"                    # a platform selection that must NOT share this platform's cache


def _answer_for(sigs):
    posts = [{"index": i + 1, "title": s["title"], "link": s["post_url"], "role": "buyer", "purchase_intent": "explicit",
              "product_fit": "fits"} for i, s in enumerate(sigs)]
    return {"format": "source_list", "executive_summary": "", "key_findings": [],
            "platforms": [{"platform": "x", "total_analyzed": len(posts), "shown_count": len(posts), "posts": posts}]}


# ── (a) identity ──────────────────────────────────────────────────────────────────────────────────────────────────────
def check_identity(fx):
    rec = fx.make_record(1, N - D(days=1))
    assert platforms.of(rec) == fx.key
    assert platforms.from_url(rec["post_url"]) == fx.key
    assert platforms.selection_key(fx.key) == fx.key
    assert platforms.get(fx.key) is not None
    assert platforms.canonical_key(rec["platform"]) == fx.key
    nrec = platforms.normalize_signal(rec, now=N)
    assert nrec["platform"] == fx.key and nrec["url"] == rec["post_url"]            # required canonical fields
    assert all(nrec[k] == v for k, v in rec.items() if k not in ("platform", "published_at"))   # nothing original is lost


# ── (b) URL and source id ─────────────────────────────────────────────────────────────────────────────────────────────
def check_source_id(fx):
    ids = {platforms.source_id(dict(fx.make_record(1, N), post_url=u)) for u in fx.equivalent_urls}
    assert len(ids) == 1 and "" not in ids, ids
    if fx.expected_source_id:
        assert ids == {fx.expected_source_id}
    assert platforms.source_id(dict(fx.make_record(2, N), post_url=fx.other_url)) not in ids
    nourl = {k: v for k, v in fx.make_record(3, N).items() if k not in ("post_url", "url", "link", "permalink")}
    assert platforms.source_id(nourl) == "" and "source_id" not in platforms.normalize_signal(nourl, now=N)      # not fabricated
    for rules in (ai.rules_for(fx.make_record(1, N)),):
        assert rules.source_id(dict(fx.make_record(1, N), post_url=fx.equivalent_urls[0])) in ids


# ── (c) timestamps and (d) missing dates ──────────────────────────────────────────────────────────────────────────────────
def check_timestamps(fx):
    ok = fx.make_record(1, N - D(hours=2))
    assert timerange.post_published_at(ok, now=N) == N - D(hours=2)
    limit = N + timerange.FUTURE_TOLERANCE
    for bad in (None, "", "garbage", datetime(1970, 1, 1, tzinfo=UTC), limit, limit + D(days=30)):
        assert timerange.post_published_at(fx.make_record(2, bad), now=N) is None, bad
    assert timerange.post_published_at(fx.make_record(3, limit - D(microseconds=1)), now=N) is not None
    naive = fx.make_record(4, (N - D(hours=1)).replace(tzinfo=None))
    assert timerange.post_published_at(naive, now=N) == N - D(hours=1)            # a naive time is read as UTC


def check_missing_date_is_unknown(fx):
    undated = platforms.normalize_signal(fx.make_record(1, None), now=N)
    assert "published_at" not in undated                                          # never replaced by ingestion / scan time
    future = platforms.normalize_signal(fx.make_record(2, N + D(days=3)), now=N)
    assert "published_at" not in future
    assert "published_at" in platforms.normalize_signal(fx.make_record(3, N - D(days=3)), now=N)


# ── (e) community / author ────────────────────────────────────────────────────────────────────────────────────────────────
def check_community_and_author(fx):
    rec = fx.make_record(1, N)
    assert platforms.community(rec) == fx.expected_community
    assert ai.rules_for(rec).community(rec) == fx.expected_community
    n = platforms.normalize_signal(rec, now=N)
    assert n.get("community") == fx.expected_community
    assert "author_id" not in n                                                    # not invented
    assert platforms.normalize_signal(dict(rec, author_id="a1"), now=N)["author_id"] == "a1"
    label, prefix = platforms.community_display(fx.key)
    assert isinstance(label, str) and isinstance(prefix, str)


# ── (f) bounded-window filtering ─────────────────────────────────────────────────────────────────────────────────────────
def check_window_filtering(lg, fx):
    window = lg._since_days_window(7, now=N)
    recent, old, undated = fx.make_record(1, N - D(days=2)), fx.make_record(2, N - D(days=40)), fx.make_record(3, None)
    at_end = fx.make_record(4, window[1])
    got = [lg._in_window(r, window, now=N) for r in (recent, old, undated, at_end)]
    assert got == [True, False, False, False]
    live = datetime.now(UTC)                                                      # the cache helper reads the wall clock
    recent, old, undated = fx.make_record(1, live - D(days=2)), fx.make_record(2, live - D(days=40)), fx.make_record(3, None)
    cached = {"posts": [recent, old, undated], "platform_key": platforms.selection_key(fx.key)}
    assert lg.usable_cached_posts(cached, fx.key, 7) == [recent]
    assert len(lg.usable_cached_posts(cached, fx.key, None)) == 3                 # unbounded keeps every record


# ── (g) attribution and evidence integrity ───────────────────────────────────────────────────────────────────────────────
def check_attribution(fx):
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    sigs = [fx.make_record(1, now - D(days=1)), fx.make_record(2, now - D(days=300)), fx.make_record(3, None)]
    scope = {"start": (now - D(days=30)).isoformat(), "end": (now + timerange.FUTURE_TOLERANCE).isoformat(), "label": "last 30 days"}
    out = ai.enforce_answer_integrity(_answer_for(sigs), sigs, sigs, "find buyers", time_scope=scope)
    rows = [r for e in out["platforms"] for r in e["posts"]]
    assert {r["link"] for r in rows} == {s["post_url"] for s in sigs}              # every shown row is a stored record
    assert all(r["source_id"] == platforms.source_id(s) for r, s in zip(sorted(rows, key=lambda r: r["index"]), sigs))
    assert {r["link"]: r["in_window"] for r in rows} == {sigs[0]["post_url"]: True, sigs[1]["post_url"]: False, sigs[2]["post_url"]: False}
    ghost = _answer_for(sigs)
    ghost["platforms"][0]["posts"].append({"index": 9, "title": "invented", "link": "https://example.invalid/none", "role": "buyer",
                                           "purchase_intent": "explicit", "product_fit": "fits"})
    again = ai.enforce_answer_integrity(ghost, sigs, sigs, "find buyers", time_scope=scope)
    assert "https://example.invalid/none" not in json.dumps(again["platforms"])  # a row not backed by a stored record is excluded
    entry = out["platforms"][0]
    assert isinstance(entry["community_label"], str) and isinstance(entry["community_prefix"], str)


# ── (h) cache isolation ──────────────────────────────────────────────────────────────────────────────────────────────────
def check_cache_isolation(lg, monkeypatch, fx):
    rec = fx.make_record(1, datetime.now(UTC) - D(days=1))
    calls = []

    def matcher(topic_key, keywords, targeting_platform="all", since_days=None, unfiltered=False, match_phrases=None, limit=None,
                signals_collection_2=None, chat_id=None, signals_collection_4=None, user_query=None, time_range=None):
        calls.append(targeting_platform)
        return []

    monkeypatch.setattr(lg, "get_cached_topic_evidence", lambda c, t: {"posts": [rec], "keywords": ["k"], "match_phrases": ["p"],
                                                                      "platform_key": platforms.selection_key(fx.key)})
    monkeypatch.setattr(lg, "save_topic_evidence_cache", lambda *a, **k: None)
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: False)
    same = lg.get_evidence_with_topup("c", "o", "t", ["k"], 1, matcher, match_phrases=["p"], targeting_platform=fx.key)
    assert same == [rec] and calls == []                                          # its own selection: served from the cache
    other = lg.get_evidence_with_topup("c", "o", "t", ["k"], 1, matcher, match_phrases=["p"], targeting_platform=fx.other_selection)
    assert other == [] and calls == [fx.other_selection]                          # another selection: never served


ALL_CHECKS = ("check_identity", "check_source_id", "check_timestamps", "check_missing_date_is_unknown",
              "check_community_and_author", "check_attribution")
