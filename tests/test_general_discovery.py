"""
tests/test_general_discovery.py — "run a general unfiltered sample of recent threads" is a request for a SAMPLE, not a keyword search.

Covers: the pure detector; the /search route (the gate that used to throw the request away); Hacker News general discovery (HN alone and
with Reddit); the six zero-result explanations from real coverage numbers; the answer's signal report; the browser renderer. All offline:
the Hacker News API is a fake, the Reddit stores are the repo's test doubles / mongomock. Simulated data is labelled as such.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

mongomock = pytest.importorskip("mongomock")
import flintel
import hackernews_live
import platforms
import signal_intel as si
from tests.test_hackernews_pipeline import FakeFetch, QUERY, hn, hn_rec, no_reddit, lg  # noqa: F401  (fixtures + helpers)
from tests.test_recency_phase2 import _search  # noqa: F401
from tests.test_time_ranges import REF, app_env, env, fixed_clock  # noqa: F401,E402
from tests.test_strict_intent import _set_cfg, ib  # noqa: F401

import sys

UTC = timezone.utc
NOW = datetime.now(UTC).replace(microsecond=0)
ROOT = Path(__file__).resolve().parent.parent
USER_REQUEST = "Yes, run a general unfiltered sample of recent threads."


# ── the detector ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("q", [
    USER_REQUEST, "Show me recent discussions", "Show unfiltered threads", "What are people discussing?", "Give me recent Hacker News posts.",
    "Show me what is happening on Hacker News", "give me a general sample", "Show me the latest posts from the last 7 days",
    "show me what's new on hn"])
def test_general_requests_are_recognised(q):
    assert si.is_general_discovery_request(q) is True


@pytest.mark.parametrize("q", [
    "Show recent threads about CRM pain", "latest iPhone", "Is anyone looking for an AI agent for customer support?", "Find buyers for my SaaS",
    "recent posts about gemini privacy", "run a general sample of rust threads", "", None, 42, "hello"])
def test_requests_with_a_topic_or_no_cue_are_not(q):
    assert si.is_general_discovery_request(q) is False


def _real_detector(monkeypatch):
    """The detector lives in signal_intel (never stubbed by the route harnesses), so nothing needs to be injected."""
    return None


# ── the /search route: the gate that used to discard the request ────────────────────────────────────────────────────────
@pytest.mark.parametrize("router", [{"keywords": None}, {"keywords": ["ai agent customer support"]}, {"keywords": ["crm"], "unfiltered": False}])
def test_route_turns_the_request_into_an_unfiltered_search_whatever_the_router_said(fixed_clock, monkeypatch, router):
    _real_detector(monkeypatch)
    msg = _search(fixed_clock, monkeypatch, USER_REQUEST, **router)
    assert msg["unfiltered"] is True and msg["keywords"] == []                       # no topic keywords at all (not the sentence's own words)
    assert msg["time_window_days"] == 30 and msg["time_window_source"] == "default_recent"    # the documented default recent window


def test_route_keeps_a_period_the_user_gave(fixed_clock, monkeypatch):
    _real_detector(monkeypatch)
    msg = _search(fixed_clock, monkeypatch, "Show me recent threads from the last 7 days", keywords=None)
    assert msg["unfiltered"] is True and msg["keywords"] == [] and (msg.get("time_range") or msg["time_window_days"] == 7)


def test_route_leaves_topic_searches_alone(fixed_clock, monkeypatch):
    _real_detector(monkeypatch)
    for q in ("Find buyers for CRM tools", "Show recent threads about CRM pain", "Is anyone looking for an AI agent for customer support?"):
        msg = _search(fixed_clock, monkeypatch, q, keywords=["crm"])
        assert not msg.get("unfiltered") and msg["keywords"] == ["crm"], q


# ── router-assisted safety net: a phrasing the message detector misses, but whose router keywords are all generic discovery words ──────────
UNSEEN_BROAD = "What are folks pleading for around here lately?"             # not recognised by the message detector (unknown words)


def test_a_missed_broad_phrasing_whose_router_keywords_are_generic_becomes_general_discovery(fixed_clock, monkeypatch):
    assert si.is_general_discovery_request(UNSEEN_BROAD) is False
    msg = _search(fixed_clock, monkeypatch, UNSEEN_BROAD, keywords=["complaints", "unmet needs"])
    assert msg["unfiltered"] is True and msg["keywords"] == []


def test_router_keywords_that_carry_a_topic_never_trigger_the_safety_net(fixed_clock, monkeypatch):
    for kws in (["complaints", "notion"], ["unmet needs", "dental billing"], ["crm"]):
        msg = _search(fixed_clock, monkeypatch, UNSEEN_BROAD, keywords=kws)
        assert not msg.get("unfiltered") and msg["keywords"] == kws, kws


# ── Hacker News general discovery ────────────────────────────────────────────────────────────────────────────────────────
def spread(n=20, hours_apart=6):
    base = int((NOW - timedelta(minutes=10)).timestamp())
    return [hn_rec(100 + i, title=f"Story {i}", ts=base - i * hours_apart * 3600) for i in range(n)]


def test_hn_only_general_discovery_needs_no_keyword_and_touches_no_reddit_store(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    monkeypatch.setattr(ib, "rerank_with_intent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no intent filter for a general sample")))
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=True)
    monkeypatch.setattr(hackernews_live, "fetch", FakeFetch(spread()))
    fetch = hackernews_live.fetch
    out = lg.get_matched_signals("t", [], "hackernews", 5, 30, True, None, False, None, "c", None, None, None, None)
    (call,) = fetch.calls
    assert call["general"] is True and call["keywords"] == [] and call["pool_n"] == 100
    assert len(out) == 5 and all(m["platform"] == "hackernews" for m in out)


def test_the_sample_is_spread_across_the_period_not_just_the_newest_minutes(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    monkeypatch.setattr(hackernews_live, "fetch", FakeFetch(spread(20, 6)))
    out = lg.get_matched_signals("t", [], "hackernews", 5, 30, True, None, False, None, "c", None, None, None, None)
    times = [m["created_utc"] for m in out]
    assert times == sorted(times, reverse=True)                                       # newest first
    assert (times[0] - times[-1]) > 3 * 24 * 3600                                      # covers days, not minutes
    assert len({m["post_url"] for m in out}) == 5                                      # no duplicates


def test_title_only_link_stories_are_kept_in_general_mode_and_say_so(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    rec = dict(hn_rec(7, title="A link story"), post_text="A link story", text_source="title",
               limitations=["Link story without body text: only the title is available as evidence."])
    monkeypatch.setattr(hackernews_live, "fetch", FakeFetch([rec]))
    out = lg.get_matched_signals("t", [], "hackernews", 5, 30, True, None, False, None, "c", None, None, None, None)
    assert out[0]["text_source"] == "title" and "only the title" in out[0]["limitations"][0]
    c = si.classify_post(out[0])
    assert c["text_source"] == "title" and any("Only the title" in x for x in c["limitations"])


def test_reddit_and_hn_general_sample_gives_each_platform_a_share_and_reddit_still_runs_unfiltered(lg, hn, monkeypatch):
    seen = []
    reddit = [{"title": f"r{i}", "post_text": "x", "post_url": f"https://www.reddit.com/r/a/comments/{i}/t/", "platform": "reddit"} for i in range(4)]
    monkeypatch.setattr(lg, "_get_matched_signals_core", lambda *a, **k: seen.append((a, k)) or list(reddit))
    monkeypatch.setattr(hackernews_live, "fetch", FakeFetch(spread()))
    out = lg.get_matched_signals("t", [], "reddit,hackernews", 6, 30, True, None, False, None, "c", None, None, None, None)
    assert seen and seen[0][0][5] is True                                              # the original unfiltered path ran with unfiltered=True
    assert len(out) == 6 and {m["platform"] for m in out} == {"reddit", "hackernews"}
    assert sum(1 for m in out if m["platform"] == "hackernews") == 3


def test_a_failing_hn_branch_leaves_the_reddit_general_sample_unchanged(lg, hn, monkeypatch):
    reddit = [{"title": "r", "post_text": "x", "post_url": "https://www.reddit.com/r/a/comments/1/t/", "platform": "reddit"}]
    monkeypatch.setattr(lg, "_get_matched_signals_core", lambda *a, **k: list(reddit))
    monkeypatch.setattr(hackernews_live, "fetch", FakeFetch(boom=True))
    out = lg.get_matched_signals("t", [], "reddit,hackernews", 6, 30, True, None, False, None, "c", None, None, None, None)
    assert out == reddit and "could not be searched" in lg.hackernews_note("c", "t")


def test_topic_specific_hn_search_still_uses_the_keywords_and_the_text_rule(lg, hn, monkeypatch):
    no_reddit(lg, monkeypatch)
    _set_cfg(monkeypatch, INTENT_BRIDGE_ENABLED=False)
    lg.get_matched_signals("t", ["crm", "sales"], "hackernews", 15, None, False, None, False, None, "c", None, QUERY, None, None)
    (call,) = hn.calls
    assert call["keywords"] == ["crm", "sales"] and not call.get("general")


# ── the fetcher in general mode (fake HTTP) ──────────────────────────────────────────────────────────────────────────────
def hit(i, ts, text=None, title=None):
    return {"objectID": str(i), "title": title or f"Story {i}", "story_text": text, "created_at_i": int(ts), "author": f"u{i}",
            "points": 10 + i, "num_comments": i, "url": f"https://example.com/{i}"}


def test_general_fetch_uses_an_empty_query_newest_first_and_the_window():
    start, end = NOW - timedelta(days=7), NOW
    urls = []
    ts = int((NOW - timedelta(hours=2)).timestamp())

    def get(url, timeout):
        urls.append(url)
        return {"hits": [hit(1, ts), hit(2, ts - 60, text="<p>Ask HN body</p>")], "nbPages": 1}
    recs, st = hackernews_live.fetch([], pool_n=10, start=start, end=end, general=True, http_get=get, now=NOW)
    assert "query=&" in urls[0] and "/search_by_date?" in urls[0] and "numericFilters=created_at_i%3E%3D" in urls[0]
    assert [r["hn_id"] for r in recs] == ["1", "2"] and st["state"] == "ok"
    assert recs[0]["text_source"] == "title" and recs[0]["post_text"] == "Story 1"             # the post's own title, flagged
    assert recs[1]["text_source"] == "story_text" and recs[1]["post_text"] == "Ask HN body"
    assert recs[0]["author"] == "u1" and recs[0]["hn_points"] == 11 and recs[0]["external_url"] == "https://example.com/1"


def test_topic_mode_still_drops_title_only_stories():
    ts = int((NOW - timedelta(hours=2)).timestamp())
    recs, st = hackernews_live.fetch(["crm"], pool_n=10, start=NOW - timedelta(days=7), end=NOW, http_get=lambda u, t: {"hits": [hit(1, ts)], "nbPages": 1}, now=NOW)
    assert recs == [] and st["no_text"] == 1


def test_general_fetch_window_boundaries_dedup_and_pagination():
    start, end = NOW - timedelta(days=7), NOW
    s, e = int(start.timestamp()), int(end.timestamp())
    pages = [{"hits": [hit(1, s), hit(2, s - 1), hit(3, e), hit(4, e - 1)], "nbPages": 3},
             {"hits": [hit(4, e - 1), hit(5, s + 5)], "nbPages": 3},
             {"hits": [hit(6, s + 9)], "nbPages": 3}]
    calls = []

    def get(url, timeout):
        calls.append(url)
        return pages[int(re.search(r"page=(\d+)", url).group(1))]
    recs, st = hackernews_live.fetch([], pool_n=50, start=start, end=end, general=True, http_get=get, max_pages=3, now=NOW + timedelta(days=1))
    assert [r["hn_id"] for r in recs] == ["1", "4", "5", "6"]          # start included, one second before / exactly end excluded, duplicate dropped
    assert len(calls) == 3 and st["outside_window"] == 2 and st["duplicates"] == 1


def test_probe_newest_states():
    ts = int((NOW - timedelta(days=3)).timestamp())
    assert hackernews_live.probe_newest(lambda u, t: {"hits": [{"created_at_i": ts}]})[0] == "ok"
    assert hackernews_live.probe_newest(lambda u, t: {"hits": []}) == ("empty", None)
    assert hackernews_live.probe_newest(lambda u, t: (_ for _ in ()).throw(RuntimeError()))[0] == "failed"
    assert hackernews_live.probe_newest(lambda u, t: {"nope": 1})[0] == "failed"


# ── coverage and the six explanations, from real data ────────────────────────────────────────────────────────────────────
def stored(docs):
    coll = mongomock.MongoClient()["t"]["signals"]
    for d in docs:
        coll.insert_one(d)
    return coll


def doc(i, when, platform="reddit"):
    return {"platform": platform, "post_url": f"https://www.reddit.com/r/a/comments/{i}/t/", "title": f"p{i}", "created_utc": when}


WIN = (NOW - timedelta(days=1), NOW)


def code_of(cov_list):
    return si.combine_zero_results([si.explain_zero_result(c) for c in cov_list])["code"]


def test_empty_database_is_reported_as_nothing_collected(lg, monkeypatch):
    monkeypatch.setattr(lg, "signals_collection", stored([]))
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    cov = lg.probe_stored_coverage("reddit", *WIN)
    assert cov["total"] == 0 and si.explain_zero_result(cov)["code"] == si.NO_POSTS_COLLECTED


def test_posts_exist_but_none_in_the_requested_range(lg, monkeypatch):
    old, older = NOW - timedelta(days=20), NOW - timedelta(days=40)
    monkeypatch.setattr(lg, "signals_collection", stored([doc(1, old), doc(2, older)]))
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    cov = lg.probe_stored_coverage("reddit", *WIN)
    assert cov["total"] == 1 and cov["in_range"] == 0
    assert cov["newest"].startswith(old.isoformat()[:16]) and cov["oldest"].startswith(older.isoformat()[:16])
    e = si.explain_zero_result(cov)
    assert e["code"] == si.NONE_IN_RANGE and old.isoformat()[:10] in e["message"]


def test_posts_exist_in_range_general_means_filters_topic_means_no_match(lg, monkeypatch):
    monkeypatch.setattr(lg, "signals_collection", stored([doc(1, NOW - timedelta(hours=3)), doc(2, NOW - timedelta(hours=5))]))
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    cov = lg.probe_stored_coverage("reddit", *WIN)
    assert cov["in_range"] == 2
    assert si.explain_zero_result(dict(cov, mode="general"))["code"] == si.EXCLUDED_BY_FILTERS
    assert si.explain_zero_result(dict(cov, mode="topic", topic="fusion"))["code"] == si.NO_TOPIC_MATCH


def test_a_probe_failure_is_a_failure_not_an_empty_result(lg, monkeypatch):
    class Boom:
        def find(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(lg, "signals_collection", Boom())
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    cov = lg.probe_stored_coverage("reddit", *WIN)
    assert "coverage probe failed" in cov["error"] and si.explain_zero_result(cov)["code"] == si.DATABASE_UNAVAILABLE


def test_mysql_enabled_and_mongo_empty_is_unknown_never_a_false_zero(lg, monkeypatch):
    monkeypatch.setattr(lg, "signals_collection", stored([]))
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    _set_cfg(monkeypatch, MYSQL_READ_ENABLED=True)
    cov = lg.probe_stored_coverage("reddit", *WIN)
    assert cov["total"] is None and si.explain_zero_result(cov)["code"] == si.UNKNOWN


@pytest.mark.parametrize("status,want", [
    ({"state": "failed", "reason": "timeout"}, si.COLLECTION_FAILED),
    ({"state": "rate_limited", "reason": "rate_limited"}, si.COLLECTION_FAILED),
    ({"state": "empty", "fetched_hits": 0, "probe": "ok", "newest_available": (NOW - timedelta(days=30)).isoformat()}, si.NONE_IN_RANGE),
    ({"state": "empty", "fetched_hits": 0, "probe": "empty"}, si.NO_POSTS_COLLECTED),
    ({"state": "empty", "fetched_hits": 30, "no_text": 30, "candidates": 0}, si.EXCLUDED_BY_FILTERS),
])
def test_hn_status_maps_to_the_right_explanation(lg, hn, status, want):
    lg._HN_STATUS[("c", "t")] = status
    msg = {"targeting_platform": "hackernews", "topic_key": "t", "time_window_days": 1, "query": "x"}
    assert code_of(lg.zero_result_coverage_for(msg, "c")) == want


def test_hn_selected_but_flag_off_is_not_silently_searched(lg):
    msg = {"targeting_platform": "hackernews", "topic_key": "t"}
    cov = lg.zero_result_coverage_for(msg, "c")
    assert platforms.get("hackernews") is None
    assert all(c.get("source") != "Hacker News" for c in cov)                                       # never reported as searched when it is not enabled


NR = json.dumps({"format": "no_results", "answer_status": "complete", "no_results_reason": "empty", "message": "No stored post matched this topic."})


def test_no_results_answer_uses_the_real_reason_when_coverage_is_given_and_is_otherwise_unchanged(lg):
    base = lg.enrich_no_results_answer(NR, "show recent threads", "reddit", None, 30, [])
    assert lg.enrich_no_results_answer(NR, "show recent threads", "reddit", None, 30, [], coverage_fn=None) == base
    cov = lambda: [{"source": "Hacker News", "mode": "general", "total": 1, "in_range": 0, "newest": "2026-10-01T00:00:00+00:00",
                    "window_start": "2026-11-01T00:00:00+00:00", "window_end": "2026-11-04T00:00:00+00:00"}]
    d = json.loads(lg.enrich_no_results_answer(NR, "show recent threads", "reddit", None, 30, [], coverage_fn=cov))
    assert d["no_results_code"] == si.NONE_IN_RANGE and "No relevant discussion found" not in d["headline"]
    assert d["coverage"]["total"] == 1 and "2026-10-01" in d["message"]
    broken = json.loads(lg.enrich_no_results_answer(NR, "x", "reddit", None, 30, [], coverage_fn=lambda: 1 / 0))
    assert broken["headline"] == json.loads(base)["headline"]                         # a coverage failure never breaks the answer


# ── the signal report on an answer ───────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _report_on_for_these_tests(monkeypatch):
    """The report is OFF by default (see test_report_is_off_by_default_everywhere); these tests exercise it switched ON."""
    _set_cfg(monkeypatch, SIGNAL_REPORT_ENABLED=True)


ANSWER = json.dumps({"format": "source_list", "headline": "h", "executive_summary": "s", "platforms": []})
POSTS = [{"title": "Ask HN: Looking for an AI agent vendor", "post_text": "We're looking for a vendor to automate support. Budget approved.",
          "post_url": "https://news.ycombinator.com/item?id=1", "platform": "hackernews", "author": "a", "created_utc": NOW.timestamp() - 3600}]


def test_report_is_attached_post_integrity_and_changes_nothing_else(lg):
    out = json.loads(lg.enrich_signal_report(ANSWER, POSTS, query=QUERY, mode="topic", coverage={"returned": 1}))
    assert {k: v for k, v in out.items() if k != "signal_report"} == json.loads(ANSWER)
    assert out["signal_report"]["items"][0]["class"] == si.DIRECT_DEMAND and out["signal_report"]["coverage"] == {"returned": 1}
    assert POSTS[0]["post_url"] == out["signal_report"]["items"][0]["url"]


@pytest.mark.parametrize("answer", [NR, json.dumps({"format": "disallowed"}), "not json", json.dumps({"format": "source_list", "answer_status": "incomplete"}),
                                    json.dumps([1])])
def test_report_is_skipped_for_non_post_answers(lg, answer):
    assert lg.enrich_signal_report(answer, POSTS, query=QUERY) == answer


def test_report_flag_and_empty_posts_leave_the_answer_untouched(lg, monkeypatch):
    assert lg.enrich_signal_report(ANSWER, [], query=QUERY) == ANSWER
    _set_cfg(monkeypatch, SIGNAL_REPORT_ENABLED=False)
    assert lg.enrich_signal_report(ANSWER, POSTS, query=QUERY) == ANSWER
    _set_cfg(monkeypatch, SIGNAL_REPORT_ENABLED=True)
    twice = lg.enrich_signal_report(lg.enrich_signal_report(ANSWER, POSTS, query=QUERY), POSTS, query=QUERY)
    assert twice.count("signal_report") == 1


def test_result_coverage_states_sources_period_and_basis(lg, hn):
    lg._HN_STATUS[("c", "t")] = {"state": "ok", "fetched_hits": 37, "candidates": 5}
    cov = lg.build_result_coverage({"targeting_platform": "reddit,hackernews", "topic_key": "t", "time_window_days": 7}, POSTS, "c")
    assert cov["sources"] == ["Reddit", "Hacker News"] and cov["returned"] == 1 and cov["hackernews_examined"] == 37
    assert cov["requested_from"] and cov["newest_post"] and "no live web search" in cov["basis"]


# ── the browser renderer (both templates) ────────────────────────────────────────────────────────────────────────────────
TEMPLATES = ["chat.html", "index.html"]


def _fn(template):
    text = (ROOT / "templates" / template).read_text(encoding="utf-8")
    m = re.search(r"  const SIGNAL_ORDER = .*?\n  function appendSignalReport\(data, container\) \{.*?\n  \}\n", text, re.S)
    assert m, template
    return text, m.group(0)


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_report_is_rendered_before_the_freshness_note_and_is_not_answer_content(template):
    text, _ = _fn(template)
    assert re.search(r"appendSignalReport\(data, renderedEl\);[^\n]*\n\s*if \(!answerEmpty\) appendFreshnessNote\(data, renderedEl\);", text)
    assert "signal_report: 1" in text and ".signal-report {" in text and "#C1FB3B" in text


REPORT = {"signal_report": {"mode": "topic", "statement": "No confirmed direct buying demand was found in the retrieved discussions.",
          "coverage": {"sources": ["Hacker News"], "returned": 2, "basis": "Hacker News fetched live from its public API; no live web search."},
          "breakdown": {"DIRECT_DEMAND": 0, "PROBLEM_SIGNAL": 0, "MARKET_SIGNAL": 1, "COMPETITOR_SIGNAL": 1, "GENERAL_DISCUSSION": 0},
          "items": [{"class": "COMPETITOR_SIGNAL", "title": "<img src=x onerror=window.__pwn=1> Show HN: Knemora", "url": "https://news.ycombinator.com/item?id=1",
                     "platform": "hackernews", "author": "u", "published_at": "2026-11-01T10:05:00+00:00", "quote": "Show HN: Knemora",
                     "intent_explanation": "The author presents their own product.", "limitations": ["Not evidence of a buyer."]},
                    {"class": "MARKET_SIGNAL", "title": "js link", "url": "javascript:window.__pwn=2", "quote": None, "limitations": []},
                    {"class": "BOGUS", "title": "ignored", "url": "https://x"}]}}


@pytest.mark.parametrize("template", TEMPLATES)
def test_renderer_shows_text_only_labels_quotes_and_https_links(integrity_browser, template):
    _t, fn = _fn(template)
    page = integrity_browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/*", lambda r: r.fulfill(status=200, headers={"Content-Type": "text/html"}, body="<!doctype html><html><body><div id='c'></div></body></html>"))
    page.goto("http://flintel.test/")
    page.add_script_tag(content=fn)
    page.evaluate(f"appendSignalReport({json.dumps(REPORT)}, document.getElementById('c'))")
    box = page.inner_text("section.signal-report")
    assert page.evaluate("window.__pwn") is None and page.query_selector("section.signal-report img") is None
    assert "No confirmed direct buying demand was found" in box and "Competitor signal" in box and "Market signal" in box and "Searched: Hacker News" in box
    assert "“Show HN: Knemora”" in box and "Limitations: Not evidence of a buyer." in box and "ignored" not in box
    hrefs = page.eval_on_selector_all("section.signal-report li a", "els => els.map(e => e.href)")
    assert hrefs == ["https://news.ycombinator.com/item?id=1"] and "Direct demand" not in box.split("Competitor signal")[0].split("\n")[-1]
    assert errors == []
    page.evaluate("document.getElementById('c').innerHTML = ''")
    page.evaluate("appendSignalReport({signal_report: {items: []}}, document.getElementById('c')); appendSignalReport(null, document.getElementById('c'))")
    assert page.query_selector("section.signal-report") is None
    page.close()


def _flag(env_extra):
    import os, subprocess, sys as _s
    env = {k: v for k, v in os.environ.items() if k != "SIGNAL_REPORT_ENABLED"}
    env.update({"SESSION_SECRET_KEY": "test-only-session-key-not-for-deployment"}, **env_extra)
    out = subprocess.run([_s.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); import config; print(config.SIGNAL_REPORT_ENABLED)"],
                         env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-300:]
    return out.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("env,want", [({}, "False"), ({"SIGNAL_REPORT_ENABLED": "true"}, "True"), ({"SIGNAL_REPORT_ENABLED": "maybe"}, "False"),
                                      ({"SIGNAL_REPORT_ENABLED": "false"}, "False")])
def test_report_is_off_by_default_everywhere(env, want):
    assert _flag(env) == want


def test_with_the_flag_off_an_answer_is_exactly_what_it_was(lg, monkeypatch):
    _set_cfg(monkeypatch, SIGNAL_REPORT_ENABLED=False)
    assert lg.enrich_signal_report(ANSWER, POSTS, query=QUERY) == ANSWER


def test_a_probe_that_returns_junk_is_no_coverage_information_not_a_fact(lg, monkeypatch):
    from unittest.mock import MagicMock
    monkeypatch.setattr(lg, "signals_collection", MagicMock())
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    assert lg.probe_stored_coverage("reddit", *WIN).get("unusable") is True
    assert lg.zero_result_coverage_for({"targeting_platform": "reddit", "topic_key": "t"}, "c") == []
    generic = lg.enrich_no_results_answer(NR, "q", "reddit", None, 30, [])
    assert lg.enrich_no_results_answer(NR, "q", "reddit", None, 30, [], coverage_fn=lambda: lg.zero_result_coverage_for(
        {"targeting_platform": "reddit", "topic_key": "t"}, "c")) == generic              # unchanged: the generic wording stays
