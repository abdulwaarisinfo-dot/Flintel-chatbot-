"""Reliability and abuse protection for the lead pages: non-blocking handlers, hard deadlines, atomic scan quota, the per-owner scan lock, failed scans
counting, the website-check limit and the progress / double-submit UI."""
import asyncio
import threading
import time

import pytest

import lead_connectors as lc
import lead_engine as le
import lead_store as ls
import business_profile as bp
from tests.test_lead_mvp import AtomicColl, Coll, FORM, GOOD, Fake, NOW, P_CRM, web, lg, env, scan  # noqa: F401


# ── the quota and the lock (store level, real threads) ─────────────────────────────────────────────────────────────────────
def test_the_daily_quota_is_exact_under_many_threads():
    st = ls.LeadStore(Coll(), Coll(), AtomicColl())
    wins = []
    ts = [threading.Thread(target=lambda: wins.append(st.claim("a@x.com", "scan", 5, NOW))) for _ in range(60)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(wins) == 5


def test_quota_is_per_owner_per_kind_and_per_day_and_zero_denies():
    st = ls.LeadStore(Coll(), Coll(), AtomicColl())
    assert all(st.claim("a@x.com", "scan", 2, NOW) for _ in range(2)) and not st.claim("a@x.com", "scan", 2, NOW)
    assert st.claim("b@x.com", "scan", 2, NOW) and st.claim("a@x.com", "profile_fetch", 2, NOW)
    from datetime import timedelta
    assert st.claim("a@x.com", "scan", 2, NOW + timedelta(days=1))
    assert not st.claim("c@x.com", "scan", 0, NOW) and not ls.LeadStore(Coll(), Coll(), None).claim("c@x.com", "scan", 5, NOW)
    with pytest.raises(PermissionError):
        st.claim("anon-guest", "scan", 5, NOW)


def test_the_scan_lock_admits_one_owner_scan_at_a_time_and_expires_on_its_own():
    from datetime import timedelta
    st = ls.LeadStore(Coll(), Coll(), AtomicColl())
    assert st.acquire_scan_lock("a@x.com", 90, NOW) is True and st.acquire_scan_lock("a@x.com", 90, NOW) is False
    assert st.acquire_scan_lock("b@x.com", 90, NOW) is True                                  # other owners are not blocked
    assert st.acquire_scan_lock("a@x.com", 90, NOW + timedelta(seconds=91)) is True          # a crashed worker cannot lock an owner out
    st.release_scan_lock("a@x.com")
    assert st.acquire_scan_lock("a@x.com", 90, NOW) is True


def test_the_lock_is_exclusive_under_many_threads():
    st = ls.LeadStore(Coll(), Coll(), AtomicColl())
    wins = []
    ts = [threading.Thread(target=lambda: wins.append(st.acquire_scan_lock("a@x.com", 90, NOW))) for _ in range(40)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(wins) == 1


# ── the pages ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
class _Req:
    session = {}

    def __init__(self, **form):
        self._form = form

    async def form(self):
        class F(dict):
            def getlist(s, k):
                v = s.get(k)
                return v if isinstance(v, list) else [v]
        return F(self._form)


def _run_in_its_own_loop(coro):
    """asyncio.run() refuses to run when another test's Playwright fixture left a running loop in this thread (see tests/conftest.py), so the
    coroutine gets a fresh thread with a fresh loop."""
    box = {}

    def target():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:                                                         # re-raised in the caller
            box["error"] = exc
    t = threading.Thread(target=target)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_a_slow_scan_does_not_block_the_event_loop(web):
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)

    class Slow(Fake):
        def fetch(self, concepts, d, l):
            time.sleep(0.8)
            return {"status": "ok", "records": [GOOD], "detail": ""}
    lr.CONNECTOR_FACTORY = lambda sel: [Slow("reddit", "Reddit")]

    async def main():
        gaps = []

        async def ticker():
            last = time.perf_counter()
            for _ in range(50):
                await asyncio.sleep(0.05)
                now = time.perf_counter(); gaps.append(now - last); last = now
        t = asyncio.create_task(ticker())
        await asyncio.sleep(0.12)
        r = await lr.leads_find_start(_Req(sources=["reddit"], days="30"))
        await t
        return max(gaps), r.headers["location"]
    worst, loc = _run_in_its_own_loop(main())
    assert loc.startswith("/leads/scan/") and worst < 0.35, worst                           # before the fix the loop stalled for the whole scan (about 0.8 s here)


def test_a_source_that_never_answers_is_cut_off_at_the_deadline_and_reported_failed(web):
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)

    class Hang(Fake):
        def fetch(self, concepts, d, l):
            time.sleep(3)
            return {"status": "ok", "records": [GOOD], "detail": ""}
    lr.SOURCE_DEADLINE = 0.4
    lr.CONNECTOR_FACTORY = lambda sel: [Hang("hackernews", "Hacker News", path="live"), Fake("reddit", "Reddit", [GOOD])]
    t0 = time.monotonic()
    loc = c.post("/leads/find", data={"sources": ["reddit", "hackernews"], "days": "30"}, follow_redirects=False).headers["location"]
    assert time.monotonic() - t0 < 2.0
    page = c.get(loc).text
    assert "Hacker News failed" in page and "did not finish within" in page and "This scan was not complete" in page and "Looking for a CRM" in page


def test_run_scan_reports_a_timed_out_source_as_failed_never_searched():
    class Hang(Fake):
        def fetch(self, concepts, d, l):
            time.sleep(2)
            return {"status": "ok", "records": [GOOD], "detail": ""}
    t0 = time.monotonic()
    s = le.run_scan(P_CRM, [Hang("hackernews", "Hacker News"), Fake("reddit", "Reddit", [GOOD])], now=NOW, source_deadline=0.3)
    assert time.monotonic() - t0 < 1.5
    st = {x["key"]: x for x in s["coverage"]["sources"]}
    assert st["hackernews"]["state"] == "failed" and "did not finish" in st["hackernews"]["detail"] and st["reddit"]["state"] == "searched"
    assert s["coverage"]["status"] == "partial" and s["counts"]["displayed"] == 1


def test_sources_run_concurrently_not_one_after_another():
    class Nap(Fake):
        def fetch(self, concepts, d, l):
            time.sleep(0.5)
            return {"status": "ok", "records": [], "detail": ""}
    t0 = time.monotonic()
    le.run_scan(P_CRM, [Nap("a", "A"), Nap("b", "B"), Nap("c", "C")], now=NOW, source_deadline=5)
    assert time.monotonic() - t0 < 1.2                                                       # sequential would be 1.5 s


def test_concurrent_submissions_run_one_scan_and_the_rest_are_told_to_wait(web):
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)
    ran = []

    class Slow(Fake):
        def fetch(self, concepts, d, l):
            ran.append(1)
            time.sleep(0.5)
            return {"status": "ok", "records": [GOOD], "detail": ""}
    lr.CONNECTOR_FACTORY = lambda sel: [Slow("reddit", "Reddit")]
    res = []
    ts = [threading.Thread(target=lambda: res.append(c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers.get("location", ""))) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(1 for x in res if x.startswith("/leads/scan/")) == 1 and sum(1 for x in res if x == "/leads/find?error=busy") == 7 and len(ran) == 1
    assert "already running" in c.get("/leads/find?error=busy").text
    assert len(lr._leads().list_scans("owner@x.com")) == 1


def test_the_daily_cap_holds_when_many_requests_arrive_together(web, monkeypatch):
    c, who, state, lr = web
    monkeypatch.setenv("LEAD_SCANS_PER_DAY", "3")
    c.post("/leads/profile", data=FORM)
    lr.CONNECTOR_FACTORY = lambda sel: [Fake("reddit", "Reddit", [GOOD])]
    res = []

    def go():
        res.append(c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers.get("location", ""))
    for _ in range(3):                                                                         # three waves of concurrent requests
        ts = [threading.Thread(target=go) for _ in range(5)]
        [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(1 for x in res if x.startswith("/leads/scan/")) == 3
    assert len(lr._leads().list_scans("owner@x.com")) == 3


def test_failed_scans_count_toward_the_cap(web, monkeypatch):
    c, who, state, lr = web
    monkeypatch.setenv("LEAD_SCANS_PER_DAY", "3")
    c.post("/leads/profile", data=FORM)
    calls = []

    def boom(sel):
        calls.append(1)
        raise RuntimeError("source layer down")
    lr.CONNECTOR_FACTORY = boom
    locs = [c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"] for _ in range(5)]
    assert locs == ["/leads/find?error=failed"] * 3 + ["/leads/find?error=cap"] * 2 and len(calls) == 3   # retries cannot go on forever
    assert lr._leads().list_scans("owner@x.com") == []                                       # nothing was saved for the failures


def test_a_refused_scan_does_not_leave_the_account_locked(web, monkeypatch):
    c, who, state, lr = web
    monkeypatch.setenv("LEAD_SCANS_PER_DAY", "1")
    c.post("/leads/profile", data=FORM)
    lr.CONNECTOR_FACTORY = lambda sel: [Fake("reddit", "Reddit", [GOOD])]
    c.post("/leads/find", data={"sources": ["reddit"], "days": "30"})
    assert c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"] == "/leads/find?error=cap"
    monkeypatch.setenv("LEAD_SCANS_PER_DAY", "5")
    assert c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"].startswith("/leads/scan/")


def test_website_checks_are_limited_per_day_and_the_profile_still_saves(web, monkeypatch):
    c, who, state, lr = web
    monkeypatch.setenv("LEAD_PROFILE_FETCHES_PER_DAY", "1")
    fetched = []
    lr._FETCH = lambda url: fetched.append(url) or {"text": "<title>Acme</title><h1>Hi</h1>"}
    c.post("/leads/profile", data=dict(FORM, website="one.example"))
    c.post("/leads/profile", data=dict(FORM, website="two.example"))
    assert fetched == ["https://one.example"]
    prof = lr._profiles().get("owner@x.com")
    assert prof["website"]["status"] == "not_analyzed" and "limit for website checks" in prof["website"]["message"]


def test_a_website_that_hangs_does_not_hang_the_request(web, monkeypatch):
    c, who, state, lr = web
    monkeypatch.setattr(lr, "WEBSITE_HARD_LIMIT", 0.4)
    lr._FETCH = lambda url: time.sleep(2) or {"text": "<title>x</title>"}
    t0 = time.monotonic()
    r = c.post("/leads/profile", data=dict(FORM, website="slow.example"), follow_redirects=False)
    assert r.status_code == 303 and time.monotonic() - t0 < 1.5
    assert lr._profiles().get("owner@x.com")["website"]["status"] == "unavailable"


def test_the_find_page_has_a_progress_area_and_a_form_that_disables_its_button(web):
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)
    html = c.get("/leads/find").text
    assert 'id="scan-form"' in html and 'id="scan-progress"' in html and 'role="status"' in html and "aria-live" in html
    assert "setAttribute('disabled'" in html or "disabled = true" in html or ".disabled=true" in html.replace(" ", "")


# ── the Hacker News adapter against the REAL response shape ─────────────────────────────────────────────────────────────
def test_the_hacker_news_adapter_turns_a_real_shaped_response_into_the_expected_record():
    from datetime import timedelta
    payload = {"hits": [{"objectID": "99", "title": "Ask HN: CRM for recruiters?", "story_text": "We are looking for a CRM for our recruitment agency. Any recommendations?",
                         "author": "dave", "created_at_i": int((NOW - timedelta(days=2)).timestamp()), "url": None, "points": 5, "num_comments": 2},
                        {"objectID": "100", "title": "No timestamp story", "story_text": "text", "author": "x", "url": None}], "nbPages": 1}
    urls = []

    def get(url, timeout):                                                                    # the production http_get returns PARSED JSON, not a string
        urls.append(url)
        return payload
    out = lc.HackerNewsConnector(http_get=get, now=NOW).fetch([{"query": "crm for recruiters", "keywords": []}], 30, 20)
    assert out["status"] == "ok" and len(out["records"]) == 1 and urls and urls[0].startswith("https://hn.algolia.com/api/v1/search_by_date?")
    rec = out["records"][0]
    assert rec["post_url"] == "https://news.ycombinator.com/item?id=99" and rec["author"] == "dave" and rec["hn_id"] == "99" and "recommendations" in rec["post_text"]
    s = le.run_scan(P_CRM, [type("C", (), {"key": "hackernews", "label": "Hacker News", "retrieval_path": "live", "fetch": lambda self, c, d, l: out})()], now=NOW)
    lead = s["leads"][0]
    assert (lead["platform"], lead["source_id"], lead["author"]) == ("hackernews", "hackernews:99", "dave") and lead["label"] in ("High potential", "Relevant")
    assert s["coverage"]["status"] == "complete"


def test_a_malformed_hacker_news_response_is_reported_not_swallowed():
    out = lc.HackerNewsConnector(http_get=lambda url, timeout: "not json at all", now=NOW).fetch([{"query": "q", "keywords": []}], 30, 20)
    assert out["status"] in ("empty", "failed", "partial") and out["records"] == []
