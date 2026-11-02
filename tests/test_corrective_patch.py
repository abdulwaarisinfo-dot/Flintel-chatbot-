"""
tests/test_corrective_patch.py — three narrow corrections on top of the Live-content / 1500-minute implementation:
  A. flintel_google: the Active Intent window accepts only an ORIGINAL publication time (`posted_at`), never `created_at`;
  B. safe diagnostics for the URLs a Live search offered and for the Reddit metadata lookup (no validation rule changed);
  C. the origin of a Historical lookback is RECORDED (`time_window_source`), not guessed from its length.
Mocked everywhere: the FakeMySQL / Mongo stand-ins of tests/test_mysql_read.py, a fake Responses reply, a fake Reddit endpoint. No network.
"""
import copy
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

import active_intent as ain
import mysql_signals as ms
import reddit_live as rl
import timerange
from timerange import TimeRange
from tests.test_mysql_read import (FakeMySQL, GOOGLE_COLS, REDDIT_COLS, cfg, g_row, install, lg, mdoc, r_row, run, NOW)  # noqa: F401,E402
from tests.test_active_intent import (HURL, URL2, ai_on, answer_json, hrow, live_env, prepare, resp, stored_msg,  # noqa: F401,E402
                                      _no_network, _no_inflight_leak, _reset_factory)
from tests.test_reddit_live import URL1, on  # noqa: F401,E402
from tests.test_reliability_ux_perf import app_env, env  # noqa: F401,E402
from tests.test_time_ranges import fixed_clock  # noqa: F401,E402

END = NOW
START = END - timedelta(minutes=1500)
AI_WINDOW = TimeRange(start=START, end=END, tz_name="UTC", kind=ain.WINDOW_KIND, phrase="last 1500 minutes")
TYPED = TimeRange(start=START, end=END, tz_name="UTC", kind="rolling", phrase="last 1500 minutes")     # any other workflow's range


def naive(t):
    return t.astimezone(timezone.utc).replace(tzinfo=None)


def google_row(i, url, posted, created):
    r = g_row(i, url, 0.9)
    r["posted_at"] = naive(posted) if posted is not None else None
    r["created_at"] = naive(created)
    return r


def google_tables(rows):
    return {"flintel": (REDDIT_COLS, []), "flintel_static": (REDDIT_COLS[:], []), "flintel_google": (GOOGLE_COLS, rows)}


INSIDE = START + timedelta(minutes=600)
OUTSIDE = START - timedelta(minutes=600)
ROWS = [google_row(1, "g/posted_in_created_in", INSIDE, INSIDE),
        google_row(2, "g/posted_null_created_in", None, INSIDE),                 # the case the correction is about
        google_row(3, "g/posted_out_created_in", OUTSIDE, INSIDE),
        google_row(4, "g/posted_in_created_out", INSIDE, OUTSIDE),
        google_row(5, "g/posted_null_created_out", None, OUTSIDE),
        google_row(6, "g/posted_future_created_in", END + timedelta(minutes=30), INSIDE)]


# ── A. Google publication time ─────────────────────────────────────────────────────────────────────────────────────────────
def test_the_marker_is_one_explicit_value_shared_by_both_modules():
    assert ain.WINDOW_KIND == ms.ACTIVE_INTENT_WINDOW_KIND == "active_intent_window"
    tr = ain.recent_window(END, 1500)
    assert tr.kind == ain.WINDOW_KIND and timerange.range_from_doc(tr.to_doc(END)).kind == ain.WINDOW_KIND   # survives the stored form


def test_strict_mode_uses_only_posted_at_for_the_google_table_and_never_created_at(lg, cfg, monkeypatch):
    srv = FakeMySQL(google_tables(copy.deepcopy(ROWS)))
    install(lg, cfg, monkeypatch, srv)
    out, _s, _c = run(lg, monkeypatch, [], time_range=AI_WINDOW, limit=50)
    assert {p["post_url"] for p in out} == {"g/posted_in_created_in", "g/posted_in_created_out"}      # only a real posted_at inside the window
    sql = [s for s, _ in srv.executed if "`flintel_google`" in s and "information_schema" not in s][0]
    assert "`posted_at` >= %s" in sql and "COALESCE" not in sql and "created_at" not in sql.split("FROM")[0].replace("`created_utc`", "")
    for p in out:
        assert p["published_at"] and timerange.to_utc(p["published_at"]) == next(timerange.to_utc(r["posted_at"].replace(tzinfo=timezone.utc)) for r in ROWS if r["post_url"] == p["post_url"])


@pytest.mark.parametrize("kw", [{"time_range": TYPED}, {"since_days": 7}, {"since_days": 1}], ids=["typed_range", "lookback_7d", "lookback_1d"])
def test_every_historical_time_workflow_uses_posted_at_alone_for_google(lg, cfg, monkeypatch, kw):
    srv = FakeMySQL(google_tables(copy.deepcopy(ROWS)))
    install(lg, cfg, monkeypatch, srv)
    out, _s, _c = run(lg, monkeypatch, [], limit=50, **kw)
    sql = [s for s, _ in srv.executed if "`flintel_google`" in s and "information_schema" not in s][0]
    assert "`posted_at` >= %s" in sql and "COALESCE" not in sql and "created_at" not in sql         # typed ranges and lookbacks too
    urls = {p["post_url"] for p in out}
    assert "g/posted_null_created_in" not in urls and "g/posted_null_created_out" not in urls          # undated: never eligible via created_at
    assert "g/posted_in_created_out" in urls or kw.get("since_days")                                    # a real posted_at in range qualifies (typed range)


def test_google_rows_without_a_window_keep_no_invented_publication_date(lg, cfg, monkeypatch):
    srv = FakeMySQL(google_tables(copy.deepcopy(ROWS)))
    install(lg, cfg, monkeypatch, srv)
    out, _s, _c = run(lg, monkeypatch, [], limit=50)                                                     # no time window at all
    by = {p["post_url"]: p for p in out}
    assert "g/posted_null_created_in" in by                                                              # still retrievable (no filter was asked for)
    assert not by["g/posted_null_created_in"].get("published_at")                                        # ...but its ingestion time is not shown as a date
    assert timerange.to_utc(by["g/posted_in_created_in"]["published_at"]) is not None


@pytest.mark.parametrize("kind", [ain.WINDOW_KIND, "rolling"], ids=["active_intent_window", "typed_range"])
def test_google_boundaries_are_half_open_future_dates_excluded_and_timezones_are_handled(lg, cfg, monkeypatch, kind):
    plus5 = timezone(timedelta(hours=5))
    tr = TimeRange(start=START.astimezone(plus5), end=END.astimezone(plus5), tz_name="Asia/Karachi", kind=kind, phrase="t")   # non-UTC aware bounds
    rows = [google_row(1, "g/just_before_start", START - timedelta(seconds=1), END),
            google_row(2, "g/at_start", START, END),
            google_row(3, "g/last_second", END - timedelta(seconds=1), END),
            google_row(4, "g/at_end", END, START),
            google_row(5, "g/future", END + timedelta(days=2), START + timedelta(minutes=5))]
    srv = FakeMySQL(google_tables(rows))
    install(lg, cfg, monkeypatch, srv)
    out, _s, _c = run(lg, monkeypatch, [], time_range=tr, limit=50)
    assert {p["post_url"] for p in out} == {"g/at_start", "g/last_second"}
    data = [p for s, p in srv.executed if "information_schema" not in s]
    assert data and all(p[1] == naive(START) for p in data)                          # the bound reaches SQL as UTC, whatever the range's zone


def test_an_unusable_publication_value_is_no_publication_time():
    for bad in ("garbage", "", 0, -5, [], {}, b"x", True):
        assert ms._aware(bad) is None
    assert ms._aware(None) is None


def test_the_diagnostic_accepts_exactly_what_the_parser_accepts():
    reply = mixed_reply()
    links, _calls, _sources = rl.parse_response(reply, 50)
    assert rl.diagnose_response(reply, 50)["accepted"] == len(links) == 1


def test_the_reddit_tables_are_unaffected_in_strict_mode(lg, cfg, monkeypatch):
    rows = [dict(r_row(1, "u/in", 0.9), created_utc=naive(INSIDE)), dict(r_row(2, "u/out", 0.9), created_utc=naive(OUTSIDE)),
            dict(r_row(3, "u/null", 0.9), created_utc=None)]
    srv = FakeMySQL({"flintel": (REDDIT_COLS, rows), "flintel_static": (REDDIT_COLS[:], []), "flintel_google": (GOOGLE_COLS, [])})
    install(lg, cfg, monkeypatch, srv)
    out, _s, _c = run(lg, monkeypatch, [], time_range=AI_WINDOW, limit=50)
    assert {p["post_url"] for p in out} == {"u/in"}
    assert "`created_utc` >= %s" in [s for s, _ in srv.executed if "`flintel`.`" in s and "information_schema" not in s][0]


def test_build_select_modes():
    google = set(GOOGLE_COLS)
    for strict in (False, True):                                                                          # a Google-shaped table: posted_at alone, always
        sql = ms.build_select("flintel_google", google, True, strict)[0]
        assert "COALESCE" not in sql and "`posted_at` >= %s" in sql
        assert "created_at" not in ms.build_select("flintel_google", google, False, strict)[0]
    reddit = set(REDDIT_COLS)
    assert ms.build_select("flintel", reddit, True)[0] == ms.build_select("flintel", reddit, True, False)[0]
    assert "`created_utc` >= %s" in ms.build_select("flintel", reddit, True)[0]
    mixed = reddit | {"created_at"}                                                                       # a Reddit-shaped table keeps its documented mapping
    assert "COALESCE(`created_utc`, `created_at`)" in ms.build_select("x", mixed, True, False)[0]
    assert "created_at" not in ms.build_select("x", mixed, True, True)[0]                                 # ...except in the Active Intent window
    both = google | {"created_utc"}
    assert "COALESCE(`created_utc`, `posted_at`)" in ms.build_select("x", both, True, True)[0]
    neither = {"id", "post_url", "text", "embedding", "created_at"}                                       # only an ingestion-time column
    sql, _o = ms.build_select("x", neither, True, True)
    assert "`created_at`" not in sql and "NULL >= %s" in sql
    assert "`created_at`" in ms.build_select("x", neither, True, False)[0]                                  # not Google-shaped (no posted_at): as before


def test_a_table_without_any_publication_column_yields_no_rows_and_is_not_a_failure(lg, cfg, monkeypatch):
    cols = ["id", "message_id", "platform", "post_url", "text", "subreddit_or_channel", "created_at", "embedding", "embedding_dim"]
    row = dict(g_row(1, "g/only_ingested", 0.9))
    row.pop("posted_at", None)
    srv = FakeMySQL({"flintel": (REDDIT_COLS, []), "flintel_static": (REDDIT_COLS[:], []), "flintel_google": (cols, [row])})
    install(lg, cfg, monkeypatch, srv)
    out, state, _c = run(lg, monkeypatch, [], time_range=AI_WINDOW, limit=50)
    assert not out and not state.get("failed_labels")


# ── B. URL and metadata diagnostics ─────────────────────────────────────────────────────────────────────────────────────────
CASES = [("https://www.reddit.com/r/SaaS/comments/abc123/x/", True, "accepted", "post"),
         ("https://old.reddit.com/r/SaaS/comments/abc123/x/", True, "accepted", "post"),
         ("https://www.reddit.com/r/SaaS/comments/abc123/x/?utm_source=a#c", True, "accepted", "post"),    # a query string is NOT a rejection reason
         ("https://www.reddit.com/r/SaaS/", False, "path_not_a_post", "subreddit_page"),
         ("https://www.reddit.com/r/SaaS/new/", False, "path_not_a_post", "subreddit_page"),
         ("https://www.reddit.com/search/?q=ai", False, "path_not_a_post", "search"),
         ("https://www.reddit.com/r/SaaS/s/AbCdEf", False, "path_not_a_post", "share_link"),
         ("https://www.reddit.com/user/someone", False, "path_not_a_post", "user"),
         ("https://www.reddit.com/r/SaaS/comments/ab/x/", False, "path_not_a_post", "post_without_valid_id"),
         ("https://redd.it/abc123", False, "host_not_allowed", "short_link"),
         ("https://i.redd.it/x.jpg", False, "host_not_allowed", "media"),
         ("https://example.com/r/SaaS/comments/abc123/x/", False, "host_not_allowed", "other_site"),
         ("http://www.reddit.com/r/SaaS/comments/abc123/x/", False, "not_https", "post"),
         ("https://www.reddit.com:8443/r/SaaS/comments/abc123/x/", False, "userinfo_port_or_encoded_host", "post"),
         ("https://user@www.reddit.com/r/SaaS/comments/abc123/x/", False, "userinfo_port_or_encoded_host", "post"),
         ("https://www.reddit.com/r/SaaS/comments/abc123/é/", False, "non_ascii_or_control_chars", "post_without_valid_id"),
         ("https://www.reddit.com/r/SaaS/comments/abc123/" + "x" * 600, False, "too_long", "other"),
         (None, False, "not_a_string", "none"), ("", False, "not_a_string", "none")]


@pytest.mark.parametrize("url,ok,reason,shape", CASES)
def test_classify_url_explains_each_url_and_never_disagrees_with_the_validator(url, ok, reason, shape):
    assert rl.classify_url(url) == (ok, reason, shape)
    assert (rl.valid_reddit_url(url) is not None) is ok                  # the diagnostic mirrors the (unchanged) rule, it does not make it


def test_the_validation_rules_themselves_are_unchanged():
    accepted = [u for u, ok, _r, _s in CASES if ok]
    assert [rl.valid_reddit_url(u) for u in accepted] == ["https://www.reddit.com/r/SaaS/comments/abc123/x/", "https://old.reddit.com/r/SaaS/comments/abc123/x/",
                                                            "https://www.reddit.com/r/SaaS/comments/abc123/x/"]
    assert rl._HOSTS == frozenset({"reddit.com", "www.reddit.com", "old.reddit.com"}) and rl._MAX_URL_LEN == 500


def mixed_reply():
    reply = resp([(URL1, "A post"), ("https://www.reddit.com/r/SaaS/", "sub"), ("https://redd.it/abc123", "short")])
    reply["output"][-1]["content"][0]["annotations"].append({"type": "url_citation", "url": "https://www.reddit.com/search/?q=secret+words", "title": "t"})
    reply["output"][-1]["content"][0]["text"] = "MODEL PROSE WITH SECRET POST CONTENT"
    return reply


def test_the_diagnostic_summarises_counts_formats_reasons_and_leaks_no_content():
    d = rl.diagnose_response(mixed_reply())
    assert d["sources"] == 3 and d["citations"] == 4 and d["unique_discovered"] == 4 and d["accepted"] == 1 and d["rejected"] == 3
    assert d["shapes"] == {"post": 1, "subreddit_page": 1, "short_link": 1, "search": 1}
    assert d["rejection_reasons"] == {"path_not_a_post": 2, "host_not_allowed": 1}
    blob = json.dumps(d)
    assert "secret" not in blob.lower() and "PROSE" not in blob and "?q=" not in blob and "utm" not in blob          # no query strings, no model text
    assert all(set(s) == {"host", "path", "reason", "shape"} for s in d["rejected_samples"]) and len(d["rejected_samples"]) <= 5


@pytest.mark.parametrize("data", [None, {}, [], "x", {"output": "x"}, {"output": [None, 5, {"type": "message", "content": None}]}])
def test_the_diagnostic_never_raises(data):
    assert isinstance(rl.diagnose_response(data), dict)


def test_a_200_with_no_valid_post_urls_now_explains_itself_in_the_result_the_attempt_and_the_log(ai_on, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="flintel.active_intent")
    env = live_env(items=[("https://www.reddit.com/r/SaaS/", "sub"), ("https://www.reddit.com/search/?q=ai", "s"), ("https://redd.it/abc123", "r")])
    run_ = prepare(env)
    run_.start()
    run_.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    assert env.calls and env.meta_calls == []                                           # a lookup was never even attempted: nothing valid to look up
    att = env.attempts.find_one()
    assert att["outcome"] == "no_valid_reddit_urls" and att["web_search_calls"] == 1
    d = att["url_diagnostics"]
    assert d["unique_discovered"] == 3 and d["accepted"] == 0 and d["rejected"] == 3
    assert d["shapes"] == {"subreddit_page": 1, "search": 1, "short_link": 1}
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert '"event": "live_branch_failed"' in text and '"reason": "no_valid_reddit_urls"' in text and '"url_diagnostics"' in text and '"rejection_reasons"' in text
    assert "MODEL PROSE" not in text


def test_the_metadata_lookup_outcome_is_recorded_ok_failed_and_per_reason(ai_on):
    env = live_env(items=[(URL1, "A"), (URL2, "B")])
    env.ages["def456"] = None                                                             # Reddit does not return the second post
    run_ = prepare(env)
    run_.start()
    run_.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    v = env.attempts.find_one()["verification"]
    assert v["lookup"] == {"requested": 2, "returned": 1, "outcome": "ok", "error": None, "status": None}
    assert v["unverifiable_reasons"]["not_in_reply"] == 1 and v["verified"] == 1
    env2 = live_env(items=[(URL1, "A")])
    env2.meta_raises = TimeoutError("slow")
    r2 = prepare(env2)
    r2.start()
    r2.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    v2 = env2.attempts.find_one()["verification"]
    assert v2["lookup"]["outcome"] == "failed" and v2["lookup"]["error"] == "TimeoutError" and v2["lookup_failed"] == 1


def test_the_unverifiable_reasons_are_counted_separately():
    start, end = START, END
    fresh = (end - timedelta(minutes=5)).timestamp()
    info = {"aaa111": {"created_utc": fresh, "subreddit": "Other", "removed": False, "title": "t", "body": ""},      # wrong subreddit
            "bbb222": {"created_utc": fresh, "subreddit": "SaaS", "removed": False, "title": None, "body": ""},       # no title
            "ccc333": {"created_utc": "garbage", "subreddit": "SaaS", "removed": False, "title": "t", "body": ""}}    # unusable date
    links = [{"url": f"https://www.reddit.com/r/SaaS/comments/{i}/x/"} for i in ("aaa111", "bbb222", "ccc333", "ddd444")]
    deps = rl.Deps(None, None, None, None, lambda: end, lambda *a: None, reddit_info=lambda ids, t: info)
    kept, stats = rl.verify_publication(deps, links, (start, end))
    assert kept == [] and stats["unverifiable_reasons"] == {"no_post_ref": 0, "not_in_reply": 1, "subreddit_mismatch": 1, "no_title": 1, "bad_timestamp": 1}


# ── C. range origin ────────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("fields,expected", [
    ({"time_window_days": 30, "time_window_source": "default_recent", "query": "people currently looking for a crm"}, True),
    ({"time_window_days": 30, "time_window_source": "explicit_period", "query": "recent posts from the last 30 days"}, False),   # the user asked for 30 days
    ({"time_window_days": 30, "query": "people currently looking for a crm"}, False),                                           # no recorded origin: the user's own
    ({"time_window_days": 7, "time_window_source": "default_recent", "query": "recent crm posts"}, True),                       # origin, not length, decides
    ({"time_window_days": 30, "time_window_source": "something_else", "query": "currently"}, False),
    ({"time_window_days": None, "time_window_source": "default_recent"}, False),
    ({"time_window_days": 30, "time_window_source": "default_recent", "time_range": {"start": 1}}, False),
    ({"time_window_days": True, "time_window_source": "default_recent"}, False),
])
def test_only_a_recorded_default_lookback_is_treated_as_automatic(fields, expected):
    assert ain._default_recency_lookback(fields) is expected


@pytest.mark.parametrize("fields,applies", [({}, True), ({"time_window_days": 30, "time_window_source": "default_recent"}, True),
                                            ({"time_window_days": 30, "time_window_source": "explicit_period"}, False),
                                            ({"time_window_days": 30}, False), ({"time_window_days": 7, "time_window_source": "explicit_period"}, False)])
def test_the_historical_window_follows_the_recorded_origin(fields, applies):
    assert ain.historical_window_applies(dict(fields, query="x"), ain.recent_window(END, 1500)) is applies


def search_with_router(app_env, monkeypatch, query, router_days):
    lg, idx, rt, chats, state = app_env

    def fake_llm(system, user, max_tokens=None, enable_web_search=False, force_json_prefill=False):
        if system == lg.CLAUDE_ROUTER_SYSTEM_PROMPT:
            return json.dumps({"intent": "search", "reply": None, "keywords": ["crm"], "time_window_days": router_days, "match_phrases": [query],
                               "evidence_required": None, "website_only": False, "url_ask_type": "none", "target_entity": "buyers",
                               "intent_types": None, "resolved_query": query})
        return json.dumps({"format": "source_list", "headline": "h", "executive_summary": "s", "platforms": []})
    monkeypatch.setattr(lg, "_call_claude", fake_llm)
    from tests.test_reliability_ux_perf import _Req, OWNER
    resp_ = rt.search(_Req(), query=query, platform="Reddit", chat_id=None)
    cid = resp_.headers["location"].rsplit("/", 1)[-1]
    return chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]


def test_search_records_a_default_recency_lookback_as_default_recent(fixed_clock, monkeypatch):
    msg = search_with_router(fixed_clock, monkeypatch, "Find people currently looking for a CRM", None)
    assert msg["time_window_days"] == 30 and msg["time_window_source"] == "default_recent"


@pytest.mark.parametrize("query", ["Find people currently looking for a CRM", "Find people looking for a CRM"])
def test_search_records_a_period_from_the_router_as_explicit_even_when_it_equals_the_default(fixed_clock, monkeypatch, query):
    msg = search_with_router(fixed_clock, monkeypatch, query, 30)                      # the router found a 30-day period in the user's words
    assert msg["time_window_days"] == 30 and msg["time_window_source"] == "explicit_period"
    assert ain.historical_window_applies(msg, ain.recent_window(END, 1500)) is False   # so Historical keeps the user's 30 days


def test_search_records_nothing_when_there_is_no_lookback(fixed_clock, monkeypatch):
    msg = search_with_router(fixed_clock, monkeypatch, "Find people looking for a CRM", None)
    assert msg.get("time_window_days") is None and "time_window_source" not in msg


def test_add_search_to_chat_stores_the_origin_only_with_a_lookback(app_env):
    lg, idx, rt, chats, state = app_env
    idx.add_search_to_chat("c1", "o1", "q", "t1", ["k"], "reddit", time_window_days=30, time_window_source="default_recent")
    idx.add_search_to_chat("c1", "o1", "q2", "t2", ["k"], "reddit", time_window_source="default_recent")
    idx.add_search_to_chat("c1", "o1", "q3", "t3", ["k"], "reddit")
    msgs = chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"]
    assert msgs[0]["time_window_source"] == "default_recent" and "time_window_source" not in msgs[1] and "time_window_source" not in msgs[2]
