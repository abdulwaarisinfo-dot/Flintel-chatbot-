"""
tests/test_search_modes.py — LIVE / HISTORICAL / COMBINED: mode handling, canonical-URL dedup across paths, provenance, partial
failure, coverage honesty, route plumbing, cache isolation, UI control. All offline; fakes are labelled in the tests.
"""
import json
from pathlib import Path

import pytest

import search_modes as sm
from tests.test_recency_phase2 import _Req, _router, OWNER  # noqa: F401
from tests.test_time_ranges import REF, app_env, env, fixed_clock  # noqa: F401,E402

ROOT = Path(__file__).resolve().parent.parent


# ── modes ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [("live", "live"), ("HISTORICAL", "historical"), (" Combined ", "combined"),
                                          (None, "combined"), ("", "combined"), ("nonsense", "combined"), (5, "combined")])
def test_normalize_mode(raw, expected):
    assert sm.normalize_mode(raw) == expected


def test_default_is_combined_and_labels_are_the_agreed_words():
    assert sm.DEFAULT_MODE == "combined"
    assert sm.LABELS == {"live": "Live", "historical": "Historical", "combined": "Combined"}


# ── URL identity ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("a,b", [
    ("https://www.reddit.com/r/saas/comments/abc123/some_title/", "https://old.reddit.com/r/SaaS/comments/abc123/other_slug"),
    ("https://reddit.com/r/x/comments/abc123/t/?utm_source=share&utm_medium=web", "http://np.reddit.com/r/x/comments/abc123/t"),
    ("https://redd.it/abc123", "https://www.reddit.com/r/x/comments/abc123/t/"),
    ("https://news.ycombinator.com/item?id=42", "http://news.ycombinator.com/item?id=42&ref=x#c"),
    ("news.ycombinator.com/item?id=42", "https://news.ycombinator.com/item?id=42")])
def test_same_post_different_url_forms_share_one_identity(a, b):
    assert sm.canonical_url(a) == sm.canonical_url(b) != ""


def test_different_posts_do_not_collide():
    assert sm.canonical_url("https://news.ycombinator.com/item?id=42") != sm.canonical_url("https://news.ycombinator.com/item?id=43")
    assert sm.canonical_url("https://reddit.com/r/x/comments/abc123/t/") != sm.canonical_url("https://reddit.com/r/x/comments/abc124/t/")
    # a Reddit comment permalink is NOT the same item as its post
    assert sm.canonical_url("https://reddit.com/r/x/comments/abc123/t/zz9") != sm.canonical_url("https://reddit.com/r/x/comments/abc123/t/")


@pytest.mark.parametrize("bad", [None, "", "   ", 5, {}, "http://"])
def test_unusable_urls_give_empty_identity(bad):
    assert sm.canonical_url(bad) == ""


# ── merge + provenance ───────────────────────────────────────────────────────────────────────────────────────────────────
def P(url, **kw):
    return dict({"post_url": url, "platform": "hackernews", "title": "t"}, **kw)


def test_merge_counts_a_post_found_by_both_paths_once_and_marks_both():
    live = [P("https://news.ycombinator.com/item?id=1", hn_id="1"), P("https://news.ycombinator.com/item?id=2", hn_id="2")]
    hist = [P("http://news.ycombinator.com/item?id=1&ref=zz"), P("https://news.ycombinator.com/item?id=3")]
    out, st = sm.merge(live, hist)
    assert len(out) == 3 and st == {"live": 1, "historical": 1, "both": 1, "duplicates_removed": 1}
    by_id = {sm.post_identity(p): p["retrieval_path"] for p in out}
    assert by_id[sm.canonical_url("https://news.ycombinator.com/item?id=1")] == "both"


def test_merge_does_not_mutate_inputs_and_survives_junk():
    live = [P("https://news.ycombinator.com/item?id=1")]
    out, _ = sm.merge(live + [None, "x", 3], [None])
    assert "retrieval_path" not in live[0] and len(out) == 1


def test_merge_keeps_posts_without_urls_separate_and_respects_limit():
    out, _ = sm.merge([{"title": "a"}, {"title": "b"}], [P("https://x.example/p/1"), P("https://x.example/p/2")], limit=3)
    assert len(out) == 3


# ── coverage honesty ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_unrequested_path_is_not_reported_as_empty():
    c = sm.mode_coverage("historical", live_status="empty", historical_status="ok")
    assert c["live"] == "not_requested" and c["historical"] == "ok" and not c["partial"]
    assert any("not a view of what was posted most recently" in n for n in c["notes"])


def test_partial_failure_is_reported_and_not_mistaken_for_no_match():
    c = sm.mode_coverage("combined", live_status={"state": "failed"}, historical_status="ok")
    assert c["partial"] is True and c["failed_paths"] == ["live"] and c["live"] == "failed"


def test_live_with_no_source_is_unavailable_not_empty():
    c = sm.mode_coverage("live", live_status="unavailable")
    assert c["live"] == "unavailable" and c["historical"] == "not_requested" and any("No live source" in n for n in c["notes"])


def test_both_paths_failing_is_not_partial():
    c = sm.mode_coverage("combined", "failed", "failed")
    assert c["partial"] is False and set(c["failed_paths"]) == {"live", "historical"}


# ── retrieval wrapper (logics) ───────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def lg(fixed_clock):
    return fixed_clock[0]          # the application's logics module, as the route tests load it (no live database)


def _call(lg, mode, **kw):
    with lg.search_mode_scope(mode) as info:
        return lg.get_matched_signals("t", ["crm"], kw.get("platform", "Reddit"), 10, 7, False, chat_id="c"), info


def test_without_a_scope_nothing_changes(lg, monkeypatch):
    monkeypatch.setattr(lg, "_get_matched_signals_hn", lambda *a, **k: ["sentinel"])
    assert lg.get_matched_signals("t", ["k"], "all", 5) == ["sentinel"]


def test_historical_never_touches_a_live_source(lg, monkeypatch):
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (True, False))
    monkeypatch.setattr(lg, "_hn_only_matched", lambda *a, **k: pytest.fail("live source used in historical mode"))
    monkeypatch.setattr(lg, "_hn_start", lambda *a, **k: pytest.fail("live fetch started in historical mode"))
    monkeypatch.setattr(lg, "_get_matched_signals_core", lambda *a, **k: [P("https://reddit.com/r/x/comments/a1/t/", platform="reddit")])
    res, info = _call(lg, "historical")
    assert [p["retrieval_path"] for p in res] == ["historical"]
    assert info["live"] == "not_requested" and info["historical"] == "ok"


def test_live_with_no_live_source_returns_nothing_and_says_so(lg, monkeypatch):
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (False, False))
    monkeypatch.setattr(lg, "_get_matched_signals_core", lambda *a, **k: pytest.fail("stored path used in live mode"))
    monkeypatch.setattr(lg, "_get_matched_signals_hn", lambda *a, **k: pytest.fail("stored path used in live mode"))
    res, info = _call(lg, "live")
    assert res == [] and info["live"] == "unavailable" and info["historical"] == "not_requested"


def test_live_uses_only_the_live_source(lg, monkeypatch):
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (True, False))
    monkeypatch.setattr(lg, "_hn_only_matched", lambda *a, **k: [P("https://news.ycombinator.com/item?id=9", hn_id="9")])
    monkeypatch.setattr(lg, "_get_matched_signals_core", lambda *a, **k: pytest.fail("stored path used in live mode"))
    monkeypatch.setitem(lg._HN_STATUS, ("c", "t"), {"state": "ok"})
    res, info = _call(lg, "live")
    assert [p["retrieval_path"] for p in res] == ["live"] and info["live"] == "ok"


def test_live_failure_is_reported_as_failure_not_empty(lg, monkeypatch):
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (True, False))

    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(lg, "_hn_only_matched", boom)
    res, info = _call(lg, "live")
    assert res == [] and info["live"] == "failed"


def test_combined_dedups_across_url_forms_and_marks_provenance(lg, monkeypatch):
    stored_hn = P("https://news.ycombinator.com/item?id=1&ref=a")                      # stored copy of a post the live path also found
    live_in_result = P("https://news.ycombinator.com/item?id=2", hn_id="2")
    stored_reddit = P("https://reddit.com/r/x/comments/a1/t/", platform="reddit")
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (True, False))
    monkeypatch.setitem(lg._HN_STATUS, ("c", "t"), {"state": "ok"})

    def fake_hn(*a, **k):
        lg._mode_scope_now()["_hn_box"] = {"recs": [P("https://news.ycombinator.com/item?id=1", hn_id="1"), live_in_result]}
        return [stored_reddit, stored_hn, live_in_result, P("https://www.reddit.com/r/X/comments/a1/other/", platform="reddit")]
    monkeypatch.setattr(lg, "_get_matched_signals_hn", fake_hn)
    res, info = _call(lg, "combined")
    assert len(res) == 3 and info["stats"] == {"live": 1, "historical": 1, "both": 1, "duplicates_removed": 1}
    paths = {sm.post_identity(p): p["retrieval_path"] for p in res}
    assert paths[sm.canonical_url("https://news.ycombinator.com/item?id=1")] == "both"
    assert paths[sm.canonical_url("https://news.ycombinator.com/item?id=2")] == "live"


def test_combined_with_failed_live_still_returns_the_historical_results(lg, monkeypatch):
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (True, False))
    monkeypatch.setitem(lg._HN_STATUS, ("c", "t"), {"state": "failed"})
    monkeypatch.setattr(lg, "_get_matched_signals_hn", lambda *a, **k: [P("https://reddit.com/r/x/comments/a1/t/", platform="reddit")])
    res, info = _call(lg, "combined")
    assert len(res) == 1 and info["live"] == "failed" and info["historical"] == "ok"
    cov = sm.mode_coverage(info["mode"], info["live"], info["historical"], info["stats"])
    assert cov["partial"] is True


def test_combined_general_discovery_passes_unfiltered_through(lg, monkeypatch):
    seen = {}
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (False, False))

    def fake(*a, **k):
        seen["unfiltered"] = a[5]
        return []
    monkeypatch.setattr(lg, "_get_matched_signals_hn", fake)
    with lg.search_mode_scope("combined"):
        lg.get_matched_signals("t", [], "Reddit", 5, 7, True)
    assert seen["unfiltered"] is True


def test_cache_key_is_isolated_per_mode_and_unchanged_for_combined(lg):
    assert lg._cache_topic_key("abc") == "abc"
    with lg.search_mode_scope("combined"):
        assert lg._cache_topic_key("abc") == "abc"
    with lg.search_mode_scope("historical"):
        assert lg._cache_topic_key("abc") == "abc::historical"
    with lg.search_mode_scope("live"):
        assert lg._cache_topic_key("abc") == "abc::live"
    assert lg._cache_topic_key("abc") == "abc"          # the scope is left cleanly


def test_scope_is_per_thread(lg):
    import threading
    seen = []
    with lg.search_mode_scope("historical"):
        t = threading.Thread(target=lambda: seen.append(lg._mode_scope_now()))
        t.start(); t.join()
    assert seen == [None]


# ── answer disclosure ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_answer_gets_a_search_mode_block_only_when_a_mode_was_chosen(lg):
    ans = json.dumps({"format": "x", "text": "hello"})
    assert lg.enrich_search_mode(ans, None) == ans
    out = json.loads(lg.enrich_search_mode(ans, {"mode": "live", "live": "ok", "historical": "not_requested", "stats": {"live": 2, "historical": 0, "both": 0}}))
    assert out["search_mode"]["mode"] == "live" and out["search_mode"]["historical"] == "not_requested"
    assert lg.enrich_search_mode("not json", {"mode": "live"}) == "not json"


# ── route plumbing ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _post(fixed_clock, monkeypatch, query, mode):
    lg, idx, rt, chats, state = fixed_clock
    _router(lg, monkeypatch)
    resp = rt.search(_Req(), query=query, platform="Reddit", chat_id=None, tz="Asia/Karachi", search_mode=mode)
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    return chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]


@pytest.mark.parametrize("mode,stored", [("live", "live"), ("Historical", "historical"), ("combined", "combined"), ("bogus", "combined")])
def test_route_stores_the_chosen_mode(fixed_clock, monkeypatch, mode, stored):
    assert _post(fixed_clock, monkeypatch, "Find latest CRM buyers", mode)["search_mode"] == stored


def test_route_without_a_mode_leaves_the_message_as_before(fixed_clock, monkeypatch):
    assert "search_mode" not in _post(fixed_clock, monkeypatch, "Find latest CRM buyers", None)


# ── UI ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_both_pages_include_the_mode_control_with_a_visible_default():
    part = (ROOT / "templates" / "_search_mode.html").read_text()
    assert 'name="search_mode"' in part and 'value="combined"' in part and 'aria-checked="true" data-mode="combined"' in part
    for f in ("index.html", "chat.html"):
        t = (ROOT / "templates" / f).read_text()
        assert '{% include "_search_mode.html" %}' in t and "appendSearchMode" in t and "search_mode: 1" in t


def test_mode_control_is_brand_consistent_and_emoji_free():
    part = (ROOT / "templates" / "_search_mode.html").read_text()
    assert "#C1FB3B" in part and not any(ord(ch) > 0x2000 for ch in part)


# ── general discovery in every mode ──────────────────────────────────────────────────────────────────────────────────────
def test_general_discovery_reaches_the_live_path_unfiltered(lg, monkeypatch):
    seen = {}
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (True, True))

    def fake(topic, kw, plat, limit, since, uq, ss, tr, cid, unfiltered=False):
        seen["unfiltered"], seen["kw"] = unfiltered, kw
        return [P("https://news.ycombinator.com/item?id=5", hn_id="5")]
    monkeypatch.setattr(lg, "_hn_only_matched", fake)
    with lg.search_mode_scope("live"):
        res = lg.get_matched_signals("t", [], "Hacker News", 5, 7, True, chat_id="c")
    assert seen == {"unfiltered": True, "kw": []} and res[0]["retrieval_path"] == "live"


def test_general_discovery_reaches_the_historical_path_unfiltered(lg, monkeypatch):
    seen = {}
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (True, False))
    monkeypatch.setattr(lg, "_get_matched_signals_core", lambda *a, **k: seen.setdefault("unfiltered", a[5]) and [])
    with lg.search_mode_scope("historical"):
        lg.get_matched_signals("t", [], "all", 5, 7, True, chat_id="c")
    assert seen["unfiltered"] is True


def test_route_called_directly_without_the_field_does_not_break(fixed_clock, monkeypatch):
    """Direct calls (as the other route tests do) leave `search_mode` at FastAPI's Form default object, not a string."""
    lg, idx, rt, chats, state = fixed_clock
    _router(lg, monkeypatch)
    resp = rt.search(_Req(), query="Find latest CRM buyers", platform="Reddit", chat_id=None, tz="Asia/Karachi")
    assert resp.status_code in (200, 303)


# ── source capability + unsupported combinations ─────────────────────────────────────────────────────────────────────────
def test_capability_table_matches_the_code_facts():
    assert sm.SOURCE_MODES["reddit"] == {"live": False, "historical": True}
    assert sm.SOURCE_MODES["hackernews"] == {"live": True, "historical": True}
    import reddit_live
    assert reddit_live.LIVE_SEARCH_EXECUTION_ENABLED is False          # the table says Reddit has no live path BECAUSE this is off


@pytest.mark.parametrize("mode,keys,expected", [
    ("live", ["reddit"], ["Reddit"]), ("live", ["reddit", "hackernews"], ["Reddit"]), ("live", ["hackernews"], []),
    ("combined", ["reddit", "hackernews"], ["Reddit"]), ("historical", ["reddit", "hackernews"], []), ("live", ["x"], [])])
def test_unsupported_sources(mode, keys, expected):
    assert sm.unsupported_sources(mode, keys) == expected


def test_coverage_explains_the_unsupported_source():
    c = sm.mode_coverage("live", "unavailable", None, None, ["Reddit"])
    assert c["unsupported_sources"] == ["Reddit"] and any("does not support Live" in n for n in c["notes"])
    c = sm.mode_coverage("combined", "ok", "ok", None, ["Reddit"])
    assert any("stored conversations only" in n for n in c["notes"])
    assert "unsupported_sources" not in sm.mode_coverage("historical", None, "ok")


def test_the_scope_records_unsupported_sources(lg, monkeypatch):
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (False, False))
    with lg.search_mode_scope("live") as info:
        lg.get_matched_signals("t", ["k"], "Reddit", 5)
    assert info["unsupported"] == ["Reddit"] and info["live"] == "unavailable"
