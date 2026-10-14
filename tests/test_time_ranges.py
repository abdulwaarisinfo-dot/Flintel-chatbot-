"""
tests/test_time_ranges.py — natural-language date/time ranges for searches. 
 
Every parser test injects its own reference instant and timezone (no test
reads the machine clock for parsing). Ranges are half-open [start, end) in
UTC; local calendar boundaries are built in the user's timezone first.

Layers covered:
  * timerange.parse_time_range (interpretation, clarifications, DST, PKT)
  * timerange.filter_posts (boundaries, missing timestamps)
  * logics.get_matched_signals with a real scan over in-memory signals: the
    range reaches the Mongo query ($gte/$lt) and the per-doc checks, and posts
    carry their ORIGINAL publication time (published_at)
  * logics.get_evidence_with_topup: range passed to the matcher, results
    re-checked, topic cache neither read nor written
  * the real /search route: range stored on the message, shown, given to the
    analysis, put on the job; ambiguous phrases get a question, no search
  * browser: the browser's timezone is sent and the range is displayed
"""
import sys
from datetime import datetime, timedelta, timezone

import pytest

import timerange as T
from timerange import Clarification, TimeRange, parse_time_range

UTC = timezone.utc
# Wed Oct 14 2026, 12:33 UTC = 5:33 PM in Karachi (UTC+5, no DST)
REF = datetime(2026, 10, 14, 12, 33, tzinfo=UTC)
PK = "Asia/Karachi"


def pk(y, mo, d, h=0, mi=0):
    """A Karachi wall-clock time as an aware UTC instant."""
    return datetime(y, mo, d, h, mi, tzinfo=UTC) - timedelta(hours=5)


def rng(text, ref=REF, tz=PK):
    r = parse_time_range(text, ref, tz)
    assert isinstance(r, TimeRange), f"{text!r} -> {r!r}"
    return r


def clar(text, ref=REF, tz=PK):
    r = parse_time_range(text, ref, tz)
    assert isinstance(r, Clarification), f"{text!r} -> {r!r}"
    return r


# ── rolling durations ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,delta", [
    ("Find posts from the last 1 minute", timedelta(minutes=1)),
    ("posts in the last minute", timedelta(minutes=1)),
    ("Find posts from the last 2 hours.", timedelta(hours=2)),
    ("Find posts from the last 3 hours.", timedelta(hours=3)),
    ("Find posts from the last 24 hours.", timedelta(hours=24)),
    ("past hour", timedelta(hours=1)),
    ("within the past 15 minutes", timedelta(minutes=15)),
    ("since 3 hours ago", timedelta(hours=3)),
    ("the last 2 days", timedelta(days=2)),
    ("last week", timedelta(days=7)),
])
def test_rolling_windows_end_at_the_reference_instant(text, delta):
    r = rng(text)
    assert (r.start, r.end, r.kind) == (REF - delta, REF, "rolling")


# ── a past point is not a rolling window ─────────────────────────────────────
def test_hours_ago_is_a_point_not_the_last_n_hours():
    point, rolling = rng("Find posts from 3 hours ago"), rng("posts from the last 3 hours")
    assert (point.start, point.end, point.kind) == (REF - timedelta(hours=4), REF - timedelta(hours=3), "point")
    assert (rolling.start, rolling.end) == (REF - timedelta(hours=3), REF)
    assert "the last 3 hours" in point.note                       # the alternative is offered, not applied


def test_one_minute_ago_is_the_one_minute_old_slot():
    r = rng("Find posts from 1 minute ago.")
    assert (r.start, r.end) == (REF - timedelta(minutes=2), REF - timedelta(minutes=1))


# ── a past calendar day is not a rolling multi-day window ────────────────────
def test_days_ago_is_a_local_calendar_day():
    two = rng("Find posts from 2 days ago.")
    assert (two.start, two.end, two.kind) == (pk(2026, 10, 12), pk(2026, 10, 13), "day")
    ten = rng("Find posts from 10 days ago.")
    assert (ten.start, ten.end) == (pk(2026, 10, 4), pk(2026, 10, 5))
    last2 = rng("posts from the last 2 days")
    assert (last2.start, last2.end) == (REF - timedelta(days=2), REF)


# ── calendar ranges, inclusive final day ─────────────────────────────────────
def test_date_range_includes_the_whole_final_day():
    r = rng("Find posts from October 9 to October 12.")
    assert (r.start, r.end, r.kind) == (pk(2026, 10, 9), pk(2026, 10, 13), "date_range")
    assert r.start == datetime(2026, 10, 8, 19, 0, tzinfo=UTC)   # PKT midnight in UTC


@pytest.mark.parametrize("text", ["Oct 9 - 12", "between October 9 and October 12", "from 9 October to 12 October",
                                  "2026-10-09 to 2026-10-12", "October 9th through October 12th"])
def test_date_range_spellings(text):
    r = rng(text)
    assert (r.start, r.end) == (pk(2026, 10, 9), pk(2026, 10, 13))


def test_date_range_with_clock_times():
    r = rng("Find posts from October 9 at 10 AM to October 12 at 3 PM.")
    assert (r.start, r.end) == (pk(2026, 10, 9, 10), pk(2026, 10, 12, 15))


def test_year_wrapping_range_without_years():
    r = rng("Dec 28 to Jan 3")
    assert (r.start, r.end) == (pk(2025, 12, 28), pk(2026, 1, 4))


# ── days, parts of days, clock ranges ────────────────────────────────────────
def test_today_is_capped_at_the_search_time_and_yesterday_is_whole():
    today = rng("Find posts from today.")
    assert (today.start, today.end, today.capped) == (pk(2026, 10, 14), REF, True)
    y = rng("Find posts from yesterday.")
    assert (y.start, y.end, y.capped) == (pk(2026, 10, 13), pk(2026, 10, 14), False)


def test_this_morning_and_afternoon():
    m = rng("Find posts from this morning.")
    assert (m.start, m.end) == (pk(2026, 10, 14, 5), pk(2026, 10, 14, 12))
    a = rng("yesterday afternoon")
    assert (a.start, a.end) == (pk(2026, 10, 13, 12), pk(2026, 10, 13, 17))
    ln = rng("posts from last night")
    assert (ln.start, ln.end) == (pk(2026, 10, 13, 18), pk(2026, 10, 14, 6))


def test_today_from_midnight_to_3pm():
    for text in ("Find posts from today between 12 AM and 3 PM.", "today from 12 AM to 3 PM",
                 "today from midnight to 3pm"):
        r = rng(text)
        assert (r.start, r.end, r.kind) == (pk(2026, 10, 14, 0), pk(2026, 10, 14, 15), "time_range")


def test_noon_and_midnight_boundaries():
    r = rng("Find posts between 9 AM and noon today.")
    assert (r.start, r.end) == (pk(2026, 10, 14, 9), pk(2026, 10, 14, 12))
    r = rng("today between 12 PM and 1 PM")                         # 12 PM is noon
    assert (r.start, r.end) == (pk(2026, 10, 14, 12), pk(2026, 10, 14, 13))
    r = rng("yesterday from 9 PM to midnight")                       # midnight as an end = next day 00:00
    assert (r.start, r.end) == (pk(2026, 10, 13, 21), pk(2026, 10, 14, 0))
    r = rng("between 9:30 am and 11:15 am yesterday")
    assert (r.start, r.end) == (pk(2026, 10, 13, 9, 30), pk(2026, 10, 13, 11, 15))


def test_clock_range_without_a_day_uses_today_and_says_so():
    r = rng("between 9 AM and 11 AM")
    assert (r.start, r.end) == (pk(2026, 10, 14, 9), pk(2026, 10, 14, 11)) and "today" in r.note


def test_this_week_and_month():
    w = rng("Find posts from this week.")
    assert (w.start, w.end) == (pk(2026, 10, 12), REF)               # Monday 00:00 local
    m = rng("last month")
    assert (m.start, m.end) == (pk(2026, 9, 1), pk(2026, 10, 1))


# ── relative expressions around local midnight ───────────────────────────────
def test_local_day_differs_from_the_utc_day_just_after_pk_midnight():
    ref = datetime(2026, 10, 14, 19, 30, tzinfo=UTC)                 # 00:30 on Oct 15 in Karachi
    today = rng("posts from today", ref)
    assert (today.start, today.end) == (pk(2026, 10, 15), ref)
    y = rng("posts from yesterday", ref)
    assert (y.start, y.end) == (pk(2026, 10, 14), pk(2026, 10, 15))
    # the same instant in UTC is still Oct 14
    assert rng("posts from today", ref, "UTC").start == datetime(2026, 10, 14, tzinfo=UTC)


# ── other timezones, DST ─────────────────────────────────────────────────────
def test_new_york_day_is_built_in_local_time():
    ref = datetime(2026, 10, 14, 12, 33, tzinfo=UTC)                 # 8:33 AM EDT
    r = rng("posts from yesterday", ref, "America/New_York")
    assert (r.start, r.end) == (datetime(2026, 10, 13, 4, tzinfo=UTC), datetime(2026, 10, 14, 4, tzinfo=UTC))


def test_dst_fall_back_day_is_25_hours():
    ref = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)                   # Mon Nov 2, after the Nov 1 change
    r = rng("posts from yesterday", ref, "America/New_York")
    assert r.start == datetime(2026, 11, 1, 4, tzinfo=UTC) and r.end == datetime(2026, 11, 2, 5, tzinfo=UTC)
    assert r.end - r.start == timedelta(hours=25)


def test_dst_spring_forward_day_is_23_hours():
    ref = datetime(2026, 3, 9, 15, 0, tzinfo=UTC)
    r = rng("posts from yesterday", ref, "America/New_York")
    assert r.end - r.start == timedelta(hours=23)


def test_unknown_timezone_falls_back_to_karachi():
    assert T.resolve_tz("Not/AZone")[1] == PK and T.resolve_tz(None)[1] == PK
    assert not T.is_valid_tz("Not/AZone") and T.is_valid_tz("Europe/London") and T.is_valid_tz(PK)
    assert rng("posts from yesterday", REF, "Not/AZone").start == pk(2026, 10, 13)


# ── ambiguity and invalid ranges: ask, never guess ───────────────────────────
@pytest.mark.parametrize("text,needle", [
    ("Find posts from December 5", "December 5, 2025"),              # later this year, no year given
    ("posts from 10/9", "October 9 or September 10"),                # day/month order unknown
    ("posts from 3 to 5 today", "AM or PM"),
    ("today from 3:00 to 5:00", "AM or PM"),
    ("posts from Oct 12 to Oct 9", "which range"),                   # end before start
    ("posts from October 12, 2026 to October 9, 2026", "not after its start"),
    ("posts from February 30", "isn't a real calendar date"),
    ("today from 9 pm to midnight", "hasn't started yet"),           # wholly in the future (it is 5:33 PM)
])
def test_ambiguous_or_invalid_ranges_ask_a_question(text, needle):
    assert needle in clar(text).question


def test_explicit_year_removes_the_ambiguity():
    r = rng("posts from December 5, 2025")
    assert (r.start, r.end) == (pk(2025, 12, 5), pk(2025, 12, 6))


@pytest.mark.parametrize("text", ["Find people looking for a CRM", "budget may 5k", "Hello", "my last campaign failed",
                                  "who needs a 24 hour plumber", "", "show me the 3 best posts"])
def test_text_without_a_time_expression_is_left_alone(text):
    assert parse_time_range(text, REF, PK) is None


def test_description_shows_local_times_and_convention():
    d = rng("Find posts from October 9 to October 12.").describe()
    assert d.startswith("Oct 9, 2026, 12:00 AM → Oct 13, 2026, 12:00 AM (Asia/Karachi, PKT")
    assert "start included, end excluded" in d
    assert "ends at the time of this search" in rng("posts from today").describe()


# ── filtering: boundaries, missing and out-of-range timestamps ───────────────
def test_filter_keeps_start_excludes_end_and_never_guesses_missing_times():
    r = rng("Find posts from October 9 to October 12.")
    posts = [{"post_url": "start", "published_at": r.start.isoformat()},
             {"post_url": "end", "published_at": r.end.isoformat()},
             {"post_url": "just-before-end", "created_utc": r.end - timedelta(seconds=1)},
             {"post_url": "before", "published_at": (r.start - timedelta(seconds=1)).isoformat()},
             {"post_url": "epoch", "created_utc": (r.start + timedelta(hours=1)).timestamp()},
             {"post_url": "naive", "created_utc": (r.start + timedelta(hours=2)).replace(tzinfo=None)},
             {"post_url": "missing"}, {"post_url": "garbage", "published_at": "not a date"}]
    kept, stats = T.filter_posts(posts, r)
    assert [p["post_url"] for p in kept] == ["start", "just-before-end", "epoch", "naive"]
    assert stats == {"kept": 4, "outside": 2, "no_timestamp": 2}


def test_stored_range_round_trips():
    r = rng("Find posts from October 9 at 10 AM to October 12 at 3 PM.")
    doc = r.to_doc(REF)
    doc["start"], doc["end"] = doc["start"].replace(tzinfo=None), doc["end"].replace(tzinfo=None)   # as Mongo returns them
    back = T.range_from_doc(doc)
    assert (back.start, back.end, back.tz_name) == (r.start, r.end, PK)
    assert T.range_from_doc({"start": r.end, "end": r.start}) is None
    assert T.range_from_doc(None) is None and T.range_from_doc({"start": "x"}) is None


# ── the real matcher (in-memory signals) ─────────────────────────────────────
from tests.test_strict_intent import Harness, FakeSignals, _mkdoc, lg  # noqa: E402,F401


class RecordingSignals(FakeSignals):
    def __init__(self, docs):
        super().__init__(docs)
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(query)
        return super().find(query, projection)


def _range_docs(base):
    """Six posts one hour apart, newest first: base-1h ... base-6h."""
    return [_mkdoc(i, 0.95 - i * 0.01, inserted=base - timedelta(hours=i)) for i in range(1, 7)]


def test_matcher_sends_the_range_and_enforces_both_ends(lg, monkeypatch):
    base = datetime.now(UTC).replace(microsecond=0)
    docs = _range_docs(base)
    h = Harness(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False)
    h.coll = RecordingSignals(docs)
    monkeypatch.setattr(lg, "signals_collection", h.coll)
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    tr = TimeRange(start=base - timedelta(hours=5), end=base - timedelta(hours=2), tz_name=PK, kind="rolling", phrase="t")
    out = lg.get_matched_signals("t", ["ai agents"], limit=50, chat_id="c", time_range=tr)
    # FakeSignals ignores $lt on purpose: the per-doc end check must still drop newer posts
    got = sorted(p["post_url"] for p in out)
    assert got == sorted(d["post_url"] for d in docs if tr.start <= d["created_utc"] < tr.end)
    # post 5 sits exactly on the start (kept); post 2 exactly on the end (excluded)
    assert [u.split("/")[-3] for u in got] == ["3", "4", "5"]
    assert any("'$lt'" in repr(q) and "'$gte'" in repr(q) for q in h.coll.queries)
    for p in out:                                                    # original publication time is carried
        assert T.to_utc(p["published_at"]) == next(d["created_utc"] for d in docs if d["post_url"] == p["post_url"])


def test_topup_with_a_range_bypasses_the_cache_and_rechecks_results(lg, monkeypatch):
    base = datetime.now(UTC).replace(microsecond=0)
    docs = _range_docs(base)
    h = Harness(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=True)
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    h.cache.docs[("chat1", "topic1")] = {"chat_id": "chat1", "topic_key": "topic1", "keywords": ["ai agents"],
                                         "posts": [{"post_url": "cached-old", "title": "x"}] * 60}
    seen = {}

    def matcher(topic_key, keywords, **kw):
        seen.update(kw)
        return lg.get_matched_signals(topic_key, keywords, **kw) + [
            {"post_url": "no-time", "title": "t"},                               # no publication time
            {"post_url": "too-new", "published_at": base.isoformat()}]           # outside the range
    tr = TimeRange(start=base - timedelta(hours=5), end=base - timedelta(hours=2), tz_name=PK, kind="rolling", phrase="t")
    out = lg.get_evidence_with_topup("chat1", "owner", "topic1", ["ai agents"], 50, matcher,
                                     match_phrases=["need an ai agent"], time_range=tr.to_doc(base))
    urls = [p["post_url"] for p in out]
    assert "cached-old" not in urls and "no-time" not in urls and "too-new" not in urls
    assert len(urls) == 3 and isinstance(seen.get("time_range"), TimeRange) and seen.get("since_days") is None
    assert "scan_state" not in seen
    # the shared evidence cache is neither served nor overwritten (only the
    # range-independent query-embedding cache may be written by the matcher)
    assert h.cache.docs[("chat1", "topic1")]["posts"] == [{"post_url": "cached-old", "title": "x"}] * 60


def test_without_a_range_the_topup_is_unchanged(lg, monkeypatch):
    base = datetime.now(UTC).replace(microsecond=0)
    h = Harness(lg, monkeypatch, _range_docs(base), INCREMENTAL_RESCAN_ENABLED=False)
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)
    assert len(h.poll()) == 6


# ── the real /search route ───────────────────────────────────────────────────
from tests.test_reliability_ux_perf import app_env, server, browser, _Req, _open, _wait_chat, OWNER  # noqa: E402,F401
from tests.test_answer_persistence import env  # noqa: E402,F401


@pytest.fixture
def fixed_clock(app_env, monkeypatch):
    rt = app_env[2]

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return REF if tz is not None else REF.replace(tzinfo=None)
    monkeypatch.setattr(rt, "datetime", FixedDateTime)
    return app_env


def test_route_stores_shows_and_passes_on_the_range(fixed_clock, monkeypatch):
    lg, idx, rt, chats, state = fixed_clock
    recorded = []
    monkeypatch.setattr(rt, "record_job_time_range", lambda tk, tr: recorded.append((tk, tr)))
    req = _Req()
    resp = rt.search(req, query="Find people looking for a CRM from October 9 to October 12", platform="Reddit",
                     chat_id=None, tz="Asia/Karachi")
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    msg = chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]
    tr = msg["time_range"]
    assert (tr["start"], tr["end"], tr["tz"]) == (pk(2026, 10, 9), pk(2026, 10, 13), PK)
    assert tr["reference"] == REF and tr["display"].startswith("Oct 9, 2026, 12:00 AM → Oct 13, 2026, 12:00 AM")
    assert msg["time_window_days"] is None and msg["targeting_platform"] == "reddit"   # platform kept
    assert "REQUESTED TIME RANGE" in msg["website_note"] and tr["display"] in msg["website_note"]
    assert len(state["jobs"]) == 1 and recorded and recorded[0][1].start == pk(2026, 10, 9)
    assert req.session["tz"] == PK


def test_route_uses_the_browser_timezone_and_rejects_junk(fixed_clock):
    lg, idx, rt, chats, state = fixed_clock
    resp = rt.search(_Req(), query="Find CRM buyers from yesterday", platform="Reddit", chat_id=None,
                     tz="America/New_York")
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    tr = chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]["time_range"]
    assert (tr["start"], tr["tz"]) == (datetime(2026, 10, 13, 4, tzinfo=UTC), "America/New_York")
    resp = rt.search(_Req(), query="Find CRM buyers from yesterday", platform="Reddit", chat_id=None,
                     tz="<script>")
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    assert chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]["time_range"]["tz"] == PK


def test_route_asks_instead_of_searching_an_ambiguous_range(fixed_clock):
    lg, idx, rt, chats, state = fixed_clock
    resp = rt.search(_Req(), query="Find CRM buyers from December 5", platform="Reddit", chat_id=None, tz=PK)
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    msg = chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]
    assert msg["message_type"] == "chat" and "December 5, 2025" in msg["claude_answer"]
    assert state["jobs"] == []                                          # no search was run


def test_route_without_a_time_phrase_is_unchanged(fixed_clock):
    lg, idx, rt, chats, state = fixed_clock
    resp = rt.search(_Req(), query="Find people looking for a CRM", platform="Reddit", chat_id=None)
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    msg = chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]
    assert msg.get("time_range") is None and "REQUESTED TIME RANGE" not in (msg.get("website_note") or "")


def test_browser_sends_its_timezone_and_shows_the_range(browser, app_env, server):
    lg, idx, rt, chats, state = app_env
    ctx = browser.new_context(timezone_id="America/New_York", viewport={"width": 1280, "height": 860})
    page = ctx.new_page()
    page._errors, page._hits = [], []
    page.on("pageerror", lambda e: page._errors.append(str(e)))
    page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(server) and "/stream" not in route.request.url
               else route.fulfill(status=204 if "/stream" in route.request.url else 200, body=""))
    try:
        page.goto(server + "/")
        page.evaluate("document.querySelectorAll('[class*=welcome]').forEach(e => e.remove()); 0")
        page.fill("#search-input", "Find people looking for a CRM from the last 3 hours")
        page.locator("#search-input").press("Enter")
        _wait_chat(page)
        cid = page.evaluate("location.pathname").rsplit("/", 1)[-1]
        tr = chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]["time_range"]
        assert tr["tz"] == "America/New_York" and tr["kind"] == "rolling"
        assert tr["end"] - tr["start"] == timedelta(hours=3)
        shown = page.inner_text(".turn-time-range")
        assert shown.startswith("Time range: ") and "America/New_York" in shown and "end excluded" in shown
        assert page._errors == []
    finally:
        ctx.close()
