"""
tests/test_recency_phase2.py
============================
PHASE 2, STEP 2 — recency-aware retrieval, ranking and honest freshness disclosure.

  1  time intent   "latest / recent / current" with no period -> the documented default lookback; an explicit period or calendar
                   range always wins; no temporal wording -> no window; "their current CRM" is not a time filter; a follow-up
                   keeps a period the router carried into its restatement
  2  retrieval     the window is enforced at the retrieval boundary (Mongo query AND the Python guard); unknown dates never
                   enter a bounded window but stay usable without one
  3  ranking       the default path orders by similarity + a small freshness bonus BEFORE any cap; the intent-bridge path now
                   receives the validated publication time
  4  integrity     in a time-bounded report, rows outside the window / undated are listed but never counted or cited; "N recent
                   buyers" and "current demand" claims are reconciled server-side; an unbounded report discloses undated posts
  5  UI            unknown dates are stated, dates are shown as UTC calendar dates (UTC-boundary test), the active period is
                   shown, and nothing claims live monitoring or collector freshness

Offline and synthetic: every post is invented; no network, no real Reddit data, no production database.
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
import timerange  # noqa: E402
from tests.test_strict_intent import Harness, _mkdoc, _set_cfg, lg  # noqa: E402,F401
from datetime import datetime as _dt  # noqa: E402
from tests.test_time_ranges import REF, app_env, env, fixed_clock  # noqa: E402,F401
from tests.test_reliability_ux_perf import OWNER, _Req, _research_router  # noqa: E402

UTC = timezone.utc
D = timedelta
NOW = datetime.now(UTC)


# ═══ 1. time intent ═════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("text", [
    "Find the latest buyer-intent discussions about CRM", "recent complaints about HubSpot", "current demand for AI note takers",
    "What are the newest posts about Notion alternatives", "Show me the most recent requests for a scheduling tool",
    "people currently looking for a CRM", "who recently switched away from Salesforce", "current buyers for project tools",
])
def test_recency_wording_is_recognised(text):
    assert timerange.recency_intent(text) is True


@pytest.mark.parametrize("text", [
    "Find people looking for a CRM", "people complaining about their current CRM", "users unhappy with their current tool",
    "Find the latest iPhone case buyers", "recent graduates looking for jobs", "newest version of Notion", "", None, 5,
    "people using their current setup",
])
def test_ordinary_or_unrelated_wording_is_not_a_time_filter(text):
    assert timerange.recency_intent(text) is False


def test_lookback_precedence():
    rng = object()
    assert timerange.effective_lookback("latest leads", None, None, rng, None, 30) == (None, rng, "explicit_range")
    assert timerange.effective_lookback("latest leads", None, 7, None, None, 30) == (7, None, "explicit_period")
    assert timerange.effective_lookback("latest leads", None, None, None, rng, 30) == (None, rng, "restated_range")
    assert timerange.effective_lookback("latest leads", None, None, None, None, 30) == (30, None, "default_recent")
    assert timerange.effective_lookback("only agencies", "the latest CRM leads, only agencies", None, None, None, 30) == (30, None, "default_recent")
    assert timerange.effective_lookback("find CRM leads", None, None, None, None, 30) == (None, None, "none")
    assert timerange.effective_lookback("latest leads", None, None, None, None, 0) == (None, None, "none")        # default off
    assert timerange.effective_lookback("latest leads", None, None, None, None, 9999, max_days=365) == (365, None, "default_recent")
    assert timerange.effective_lookback("latest leads", None, True, None, None, 30)[2] == "default_recent"         # bool is not a period


def _router(lg, monkeypatch, **over):
    base = dict(_research_router("x"), resolved_query=None)
    base.update(over)
    real = lg._call_claude

    def fake(system, user, *a, **k):
        if system == lg.CLAUDE_ROUTER_SYSTEM_PROMPT:
            return json.dumps(base)
        return real(system, user, *a, **k)
    monkeypatch.setattr(lg, "_call_claude", fake)


def _search(fixed_clock, monkeypatch, query, **router_over):
    lg, idx, rt, chats, state = fixed_clock
    _router(lg, monkeypatch, **router_over)
    resp = rt.search(_Req(), query=query, platform="Reddit", chat_id=None, tz="Asia/Karachi")
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    return chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]


@pytest.mark.parametrize("query", ["Find the latest buyer-intent discussions about CRM", "recent complaints about HubSpot",
                                   "Find current demand for AI note takers"])
def test_route_applies_the_documented_default_window(fixed_clock, monkeypatch, query):
    msg = _search(fixed_clock, monkeypatch, query)
    assert msg["time_window_days"] == 30 and not msg.get("time_range")


def test_route_default_follows_the_configuration(fixed_clock, monkeypatch):
    import config
    monkeypatch.setattr(config, "RECENT_DEFAULT_WINDOW_DAYS", 45)
    assert _search(fixed_clock, monkeypatch, "latest CRM buyers")["time_window_days"] == 45
    monkeypatch.setattr(config, "RECENT_DEFAULT_WINDOW_DAYS", 0)
    assert _search(fixed_clock, monkeypatch, "latest CRM buyers")["time_window_days"] is None


def test_route_keeps_the_period_the_user_gave(fixed_clock, monkeypatch):
    msg = _search(fixed_clock, monkeypatch, "Find latest CRM buyers in the last 7 days")
    tr = msg["time_range"]
    assert msg["time_window_days"] is None and tr["kind"] == "rolling"
    assert tr["end"] - tr["start"] == D(days=7)
    assert _search(fixed_clock, monkeypatch, "latest CRM buyers", time_window_days=14)["time_window_days"] == 14   # router period kept


def test_route_keeps_an_explicit_calendar_range(fixed_clock, monkeypatch):
    msg = _search(fixed_clock, monkeypatch, "Find latest CRM buyers from October 9 to October 12")
    assert msg["time_range"]["kind"] == "date_range" and msg["time_window_days"] is None


def test_route_without_temporal_wording_is_unbounded(fixed_clock, monkeypatch):
    for q in ("Find people looking for a CRM", "people complaining about their current CRM"):
        msg = _search(fixed_clock, monkeypatch, q)
        assert msg["time_window_days"] is None and not msg.get("time_range"), q


def test_a_followup_keeps_the_period_the_router_carried_into_its_restatement(fixed_clock, monkeypatch):
    msg = _search(fixed_clock, monkeypatch, "now only agencies",
                  resolved_query="Find CRM buyers, only agencies, in the last 14 days")
    tr = msg["time_range"]
    assert tr["kind"] == "rolling" and msg["time_window_days"] is None
    assert tr["end"] - tr["start"] == D(days=14)
    # a restatement that carries "latest" gets the default; one with no temporal wording stays unbounded
    assert _search(fixed_clock, monkeypatch, "only agencies", resolved_query="the latest CRM leads, only agencies")["time_window_days"] == 30
    assert _search(fixed_clock, monkeypatch, "only agencies", resolved_query="CRM leads, only agencies")["time_window_days"] is None


# ═══ 2. retrieval ═══════════════════════════════════════════════════════════════════════════════════════════════════
def _docs(sim_old=0.9, sim_new=0.9):
    ins = NOW - D(days=1)
    return [_mkdoc(1, sim_old, inserted=ins, created=NOW - D(days=400)),            # old, valid
            _mkdoc(2, sim_new, inserted=ins, created=NOW - D(days=3)),              # recent, valid
            dict(_mkdoc(3, 0.9, inserted=ins), created_utc=None),                   # no date
            _mkdoc(4, 0.9, inserted=ins, created=NOW + D(days=30)),                 # future
            _mkdoc(5, 0.9, inserted=ins, created=datetime(1970, 1, 1, tzinfo=UTC)), # epoch zero
            _mkdoc(6, 0.9, inserted=ins, created=datetime(2008, 3, 1, tzinfo=UTC))] # valid historical


class _Cur(list):
    def sort(self, *a, **k): return self
    def batch_size(self, n): return self
    def limit(self, n): return _Cur(self[:n])


class Signals:
    """Collection double: applies the $gte/$lt it is sent (a non-datetime value never satisfies them, like Mongo) and never
    sorts, so documents with a missing date cannot break the double. `honour=False` ignores the bounds (Python guard only)."""
    def __init__(self, docs, honour=True):
        self.docs, self.honour, self.queries = list(docs), honour, []

    def find(self, query, projection=None):
        self.queries.append(query)
        rng = {}

        def walk(q):
            for k, v in q.items():
                if k == "$and":
                    [walk(x) for x in v]
                elif k == "created_utc" and isinstance(v, dict):
                    rng.update(v)
        walk(query)
        out = []
        for d in self.docs:
            if not d.get("embedding"):
                continue
            c = d.get("created_utc")
            if self.honour and rng:
                if not isinstance(c, _dt):
                    continue
                cc = c if c.tzinfo else c.replace(tzinfo=UTC)
                if ("$gte" in rng and cc < rng["$gte"]) or ("$lt" in rng and cc >= rng["$lt"]):
                    continue
            d = dict(d)
            if projection is not None and projection.get("_id") == 0:
                d.pop("_id", None)
            out.append(d)
        return _Cur(out)


RecordingSignals = Signals


def _get(lg, monkeypatch, docs, since_days=None, limit=20, python_only=False, **cfg):
    h = Harness(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, **cfg)
    coll = Signals(docs, honour=not python_only)
    monkeypatch.setattr(lg, "signals_collection", coll)
    out = lg.get_matched_signals("t", ["ai agents"], "all", limit=limit, since_days=since_days,
                                 match_phrases=["need an ai agent"], user_query="find ai agent buyers")
    return [p["title"] for p in out], coll


@pytest.mark.parametrize("python_only", [False, True])
def test_a_30_day_window_keeps_only_valid_recent_posts_on_every_path(lg, monkeypatch, python_only):
    titles, coll = _get(lg, monkeypatch, _docs(), since_days=30, python_only=python_only)
    assert titles == ["post 2"]                                     # not old, undated, future, epoch-zero or 2008
    if not python_only:
        q = json.dumps(coll.queries, default=str)
        assert "$gte" in q and "$lt" in q                           # the bound is in the Mongo query itself


def test_without_a_window_old_and_undated_posts_are_still_usable(lg, monkeypatch):
    titles, _ = _get(lg, monkeypatch, _docs(), since_days=None)
    assert set(titles) == {"post 1", "post 2", "post 3", "post 4", "post 5", "post 6"}      # nothing silently dropped


# ═══ 3. ranking ═════════════════════════════════════════════════════════════════════════════════════════════════════
def _order(lg, monkeypatch, specs, limit=20, **cfg):
    """specs: [(n, similarity, age_days)] -> titles in returned order."""
    docs = [_mkdoc(n, sim, inserted=NOW - D(days=1), created=NOW - D(days=age)) for n, sim, age in specs]
    return _get(lg, monkeypatch, docs, since_days=None, limit=limit, **cfg)[0]


def test_a_recent_post_beats_an_older_one_of_comparable_similarity(lg, monkeypatch):
    assert _order(lg, monkeypatch, [(1, 0.80, 300), (2, 0.78, 1)])[:2] == ["post 2", "post 1"]


def test_a_clearly_better_older_match_still_wins(lg, monkeypatch):
    assert _order(lg, monkeypatch, [(1, 0.95, 300), (2, 0.60, 1)])[:2] == ["post 1", "post 2"]


def test_a_recent_but_irrelevant_post_is_not_returned_at_all(lg, monkeypatch):
    assert _order(lg, monkeypatch, [(1, 0.70, 200), (2, 0.20, 0.01)]) == ["post 1"]          # below the similarity threshold


def test_the_cap_does_not_drop_recent_comparable_candidates(lg, monkeypatch):
    specs = [(i, 0.90 - i * 0.002, 300) for i in range(1, 11)] + [(11, 0.88, 1), (12, 0.87, 1)]
    kept = _order(lg, monkeypatch, specs, limit=3)
    assert "post 11" in kept and "post 12" in kept
    off = _order(lg, monkeypatch, specs, limit=3, RECENCY_RANK_WEIGHT=0)                      # weight 0 = the old ordering
    assert "post 11" not in off and "post 12" not in off


def test_an_unknown_date_never_gets_a_freshness_bonus(lg, monkeypatch):
    docs = [dict(_mkdoc(1, 0.80, inserted=NOW), created_utc=None), _mkdoc(2, 0.79, inserted=NOW, created=NOW - D(days=1))]
    titles, _ = _get(lg, monkeypatch, docs)
    assert titles[:2] == ["post 2", "post 1"]
    assert lg._recency_bonus({"created_utc": NOW + D(days=40)}) == 0 == lg._recency_bonus({"created_utc": datetime(1970, 1, 1)})


def test_the_bonus_is_small_and_decays(lg, monkeypatch):
    fresh, month, old = (lg._recency_bonus({"created_utc": NOW - D(days=d)}) for d in (0, 30, 400))
    assert fresh == pytest.approx(0.05, abs=1e-3) and month == pytest.approx(0.025, abs=1e-3) and old < 1e-4


# intent-bridge path: the validated publication time must reach the ranker
def _cand(url, published_at):
    c = {"title": url, "post_text": "x " * 20, "post_url": url, "platform": "reddit"}
    if published_at:
        c["published_at"] = published_at
    return c


def _cls():
    return {"intent": "buyer_demand", "intents": ["buyer_demand"], "confidence": 0.9, "commercial_signal": 0.7,
            "urgency": 0.2, "pain_intensity": 0.2, "actor_type": "company", "actor_role": "buyer", "specificity": 0.6,
            "secondary_intent": None, "secondary_confidence": None, "ambiguous": False, "noise": False}


def _qi():
    from tests.test_strict_intent import _qi
    return _qi()


@pytest.mark.parametrize("strict", [False, True])
def test_the_bridge_ranker_receives_the_publication_time(strict):
    import intent_bridge as ib
    old, new = _cand("old", (NOW - D(days=400)).isoformat()), _cand("new", (NOW - D(hours=3)).isoformat())
    passing = [(old, 0.8, _cls()), (new, 0.8, _cls())]               # identical quality; the OLD one is listed first
    assert [c["post_url"] for c in ib._rank_passing(passing, _qi(), strict=strict)] == ["new", "old"]


def test_the_bridge_treats_a_missing_date_as_unknown_not_as_fresh():
    import intent_bridge as ib
    undated, old = _cand("undated", None), _cand("old", (NOW - D(days=400)).isoformat())
    out = [c["post_url"] for c in ib._rank_passing([(old, 0.8, _cls()), (undated, 0.8, _cls())], _qi())]
    assert out[0] == "old"            # (STEP 2.1) unknown earns no recency credit: no advantage over a 400-day-old post either
    fresh = _cand("fresh", (NOW - D(hours=1)).isoformat())
    assert [c["post_url"] for c in ib._rank_passing([(undated, 0.8, _cls()), (fresh, 0.8, _cls())], _qi())][0] == "fresh"


def test_candidates_reach_the_bridge_with_validated_dates_only(lg, monkeypatch):
    import intent_bridge as ib
    seen = []
    docs = [_mkdoc(1, 0.9, inserted=NOW, created=NOW - D(days=2)), _mkdoc(2, 0.9, inserted=NOW, created=NOW + D(days=30)),
            _mkdoc(3, 0.9, inserted=NOW, created=datetime(1970, 1, 1, tzinfo=UTC))]
    h = Harness(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False, INTENT_BRIDGE_ENABLED=True)
    monkeypatch.setattr(lg, "signals_collection", RecordingSignals(docs))

    def fake(user_query, candidates, limit, topic_sims=None, plan_key=None):
        seen.extend(candidates)
        return list(candidates)
    monkeypatch.setattr(ib, "rerank_with_intent", fake)
    lg.get_matched_signals("t", ["ai agents"], "all", limit=10, since_days=None, match_phrases=["x"], user_query="find buyers")
    got = {c["title"]: c.get("published_at") for c in seen}
    assert got["post 1"] and got["post 2"] is None and got["post 3"] is None


# ═══ 4. answer integrity in a time-bounded report ═══════════════════════════════════════════════════════════════════
CRM_Q = "Find people looking for a CRM"


def _sig(n, title, when, *, text=None):
    s = {"title": f"[synthetic] {title}", "post_text": text or (
        f"[synthetic] Looking for a CRM for team {title}. The budget is approved by finance and we will subscribe this quarter."),
         "post_url": f"https://www.reddit.com/r/crm/comments/p{n}/post_{n}/", "platform": "reddit", "subreddit": "CRM"}
    if when is not None:
        s["published_at"] = when.isoformat()
    return s


def _scope(days=30, ref=None):
    ref = ref or NOW
    return {"start": (ref - D(days=days)).isoformat(), "end": (ref + timerange.FUTURE_TOLERANCE).isoformat(),
            "label": f"last {days} days", "days": days}


def _answer(sigs, summary="", headline=None, findings=None, **extra):
    posts = [{"index": i + 1, "title": s["title"], "link": s["post_url"], "role": "buyer", "purchase_intent": "explicit",
              "product_fit": "fits"} for i, s in enumerate(sigs)]
    d = {"format": "source_list", "executive_summary": summary, "key_findings": findings or [],
         "platforms": [{"platform": "reddit", "total_analyzed": len(posts), "shown_count": len(posts), "posts": posts}]}
    if headline is not None:
        d["headline"] = headline
    d.update(extra)
    return d


def _run(sigs, scope=None, **kw):
    return ai.enforce_answer_integrity(_answer(sigs, **kw), sigs, sigs, CRM_Q, time_scope=scope)


def _rows(out):
    return [r for e in out["platforms"] for r in e["posts"]]


def _mixed():
    return [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", NOW - D(days=200)),     # in window / out of window
            _sig(3, "cedar", None), _sig(4, "delta", NOW - D(days=5))]                  # undated / in window


def test_out_of_window_and_undated_rows_are_listed_but_never_counted():
    out = _run(_mixed(), _scope())
    rows = {r["title"].split("]")[1].strip(): r for r in _rows(out)}
    assert [rows[k].get("in_window") for k in ("alpha", "bravo", "cedar", "delta")] == [True, False, False, True]
    assert [rows[k]["date_status"] for k in ("alpha", "bravo", "cedar", "delta")] == ["dated", "dated", "undated", "dated"]
    c = out["counts"]
    assert (c["shown"], c["qualified"], c["buyers"], c["relevant"]) == (4, 2, 2, 2)           # only alpha and delta count
    assert c["shown"] == c["relevant"] + c["uncertain"] + c["unverified"]
    assert out["time_scope"] == {"window": {"start": _scope()["start"], "end": _scope()["end"], "label": "last 30 days"},
                                 "shown": 4, "dated": 3, "undated": 1, "in_window": 2, "not_counted": 2}
    assert "counted for the last 30 days only" in out["verified_summary"] and "2 listed posts" in out["verified_summary"]
    assert rows["bravo"]["link"].startswith("https://")                                        # still traceable


def test_a_finding_resting_on_out_of_window_or_undated_posts_is_removed():
    f = lambda idx: {"finding": "Teams want a CRM", "evidence_type": "Buyer", "signal_strength": "Strong",
                     "supporting_post_indices": idx, "impact": "Teams want a CRM."}
    out = _run(_mixed(), _scope(), findings=[f([2, 3]), f([1, 2, 3])])
    assert len(out["key_findings"]) == 1 and out["key_findings"][0]["supporting_post_indices"] == [1]


def test_recent_buyer_counts_are_reconciled_against_the_window():
    out = _run(_mixed(), _scope(), summary="We found 12 recent buyers. Pricing is the main concern.",
               headline="Seven current buyers found")
    assert out["executive_summary"] == "We found two recent buyers. Pricing is the main concern."
    assert out["headline"] == "Two current buyers found"


def test_current_demand_cannot_be_claimed_when_no_buyer_falls_in_the_window():
    old_only = [_sig(1, "alpha", NOW - D(days=300)), _sig(2, "bravo", None)]
    out = _run(old_only, _scope(), summary="Current demand is strong. Pricing is the main concern.", headline="Current demand is strong")
    assert out["counts"]["buyers"] == 0 and out["counts"]["qualified"] == 0
    assert out["executive_summary"] == "Pricing is the main concern."
    assert out["headline"] == "Buyer posts found in the last 30 days: none."
    assert "0 qualified leads" in out["verified_summary"] or "qualified" in out["verified_summary"]


def test_valid_in_window_evidence_keeps_its_claims():
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", NOW - D(days=4))]
    out = _run(sigs, _scope(), summary="We found two recent buyers.", headline="Two recent buyers found")
    assert out["executive_summary"] == "We found two recent buyers." and out["headline"] == "Two recent buyers found"


def test_an_unbounded_report_keeps_undated_evidence_and_discloses_it():
    sigs = _mixed()
    unbounded = _run(sigs)
    assert "in_window" not in _rows(unbounded)[0] and unbounded["counts"]["qualified"] == 4          # nothing silently dropped
    assert unbounded["time_scope"] == {"window": None, "shown": 4, "dated": 3, "undated": 1}
    assert "publication date" not in unbounded["verified_summary"]          # the prose summary keeps its existing contract ...
    allp = _run([_sig(1, "alpha", NOW - D(days=2))])                          # ... the disclosure is the structured time_scope
    assert allp["time_scope"] == {"window": None, "shown": 1, "dated": 1, "undated": 0}


def test_phase1_behaviour_is_unchanged_without_a_window():
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", None)]
    base = _run(sigs)
    assert base["counts"]["qualified"] == 2 and base["analysis_check"] == {"source_verified": 2, "analysis_verified": 2, "analysis_unverified": 0}
    foreign = _answer(sigs)
    foreign["platforms"][0]["posts"][1].update(title="[synthetic] another post entirely")
    out = ai.enforce_answer_integrity(foreign, sigs, sigs, CRM_Q)
    assert out["counts"]["qualified"] == 1 and out["analysis_check"]["analysis_unverified"] == 1


def test_a_window_does_not_disturb_the_analysis_statistics():
    out = _run(_mixed(), _scope())
    assert out["analysis_check"] == {"source_verified": 4, "analysis_verified": 4, "analysis_unverified": 0}


def test_build_time_scope_is_measured_at_the_request_time(lg):
    req = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    s = lg.build_time_scope(7, None, req)
    assert s["label"] == "last 7 days" and s["days"] == 7
    assert timerange.to_utc(s["start"]) == req - D(days=7) and timerange.to_utc(s["end"]) == req + timerange.FUTURE_TOLERANCE
    assert lg.build_time_scope(None, None, req) is None and lg.build_time_scope(0, None, req) is None
    rng = timerange.TimeRange(start=req - D(days=3), end=req, tz_name="UTC", kind="rolling", phrase="x")
    assert lg.build_time_scope(None, rng.to_doc(req), req)["days"] is None


# ═══ 5. UI ══════════════════════════════════════════════════════════════════════════════════════════════════════════
from tests.test_answer_persistence import ORIGIN, _render  # noqa: E402


def _open(browser, name, answer, tz="Asia/Karachi"):
    chat = {"chat_id": "c1", "owner_key": "o", "title": "t", "created_at": NOW, "updated_at": NOW, "summary": "",
            "messages": [{"query": "latest CRM buyers", "topic_key": "latest crm buyers", "results": [], "keywords": ["crm"],
                          "claude_answer": json.dumps(answer), "requested_at": NOW, "answered_at": NOW,
                          "targeting_platform": "reddit", "message_type": "search"}]}
    pages = {"/chat/c1": _render("chat.html", chat), "/": _render("index.html", None)}
    ctx = browser.new_context(timezone_id=tz, locale="en-US")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def handle(route):
        url = route.request.url
        path = url[len(ORIGIN):].split("?")[0] if url.startswith(ORIGIN) else None
        return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[path]) if path in pages \
            else route.fulfill(status=200, body="")
    page.route("**/*", handle)
    if name == "chat.html":
        page.goto(ORIGIN + "/chat/c1")
    else:
        page.goto(ORIGIN + "/")
        page.evaluate("navigateTo('/chat/c1'); 0")
        page.wait_for_function("document.querySelectorAll('.turn-block').length > 0")
    page.wait_for_selector(".answer-rendered table, .answer-rendered .ap-time-scope", timeout=5000)
    return page, ctx, errors


def _table(page):
    return page.evaluate("""() => [...document.querySelectorAll('.answer-rendered table tr')].map(
        r => [...r.children].map(c => c.innerText.trim()))""")


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_ui_shows_the_period_unknown_dates_and_utc_dates(integrity_browser, name):
    boundary = datetime(NOW.year, NOW.month, NOW.day, 23, 30, tzinfo=UTC) - D(days=3)           # 23:30 UTC: next day in Karachi
    sigs = [_sig(1, "alpha", boundary), _sig(2, "bravo", None), _sig(3, "cedar", NOW - D(days=300))]
    out = _run(sigs, _scope())
    page, ctx, errors = _open(integrity_browser, name, out)
    try:
        text = page.inner_text(".answer-rendered")
        assert "Showing posts from the last 30 days." in text and "2 listed posts are outside it" in text
        rows = _table(page)
        header = rows[0]
        col = header.index("Posted")
        want = boundary.strftime("%b ") + str(boundary.day) + ", " + str(boundary.year) + " UTC"
        cells = [r[col] for r in rows[1:]]
        assert want in cells                                      # the UTC day, not Karachi's next day
        assert cells.count("Date unknown") == 1                   # honest, not a placeholder dash
        assert not any(c in ("—", "-", "N/A", "1970") for c in cells)
        low = text.lower()
        assert not any(w in low for w in ("real-time", "real time", "live monitoring", "up to date", "collector", "continuously"))
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_ui_without_a_window_discloses_undated_posts_only(integrity_browser, name):
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", None)]
    page, ctx, errors = _open(integrity_browser, name, _run(sigs))
    try:
        text = page.inner_text(".answer-rendered")
        assert "1 of 2 listed posts has no valid publication date, so its age is unknown." in text
        assert "Showing posts from" not in text and errors == []
    finally:
        ctx.close()
    page, ctx, errors = _open(integrity_browser, name, _run([_sig(1, "alpha", NOW - D(days=2))]))
    try:
        assert "publication date" not in page.inner_text(".answer-rendered").replace("Posted", "")
    finally:
        ctx.close()


# ═══ 6. wiring and date provenance ═════════════════════════════════════════════════════════════════════════════════
def test_a_row_date_is_only_ever_the_stored_records_validated_date():
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", None), _sig(3, "cedar", NOW + D(days=30))]
    ans = _answer(sigs)
    for p in ans["platforms"][0]["posts"]:
        p["published_at"] = "2026-01-01T00:00:00+00:00"             # a date the model wrote itself
    rows = {r["title"].split("]")[1].strip(): r for r in _rows(ai.enforce_answer_integrity(ans, sigs, sigs, CRM_Q))}
    assert rows["alpha"]["published_at"] == (NOW - D(days=2)).isoformat()
    assert "published_at" not in rows["bravo"] and "published_at" not in rows["cedar"]          # unknown stays unknown
    assert rows["bravo"]["date_status"] == rows["cedar"]["date_status"] == "undated"


def test_patch_applies_the_window_set_by_the_caller_and_nothing_otherwise(lg):
    sigs = _mixed()
    raw = json.dumps(_answer(sigs))
    with lg.time_scope_context(_scope()):
        scoped = json.loads(lg._patch_post_urls_into_answer(raw, sigs, query=CRM_Q))
    plain = json.loads(lg._patch_post_urls_into_answer(raw, sigs, query=CRM_Q))
    assert scoped["time_scope"]["window"]["label"] == "last 30 days" and scoped["counts"]["qualified"] == 2
    assert plain["time_scope"]["window"] is None and plain["counts"]["qualified"] == 4
    assert lg._TIME_SCOPE.get() is None                               # the context is restored after use


def test_the_completed_answer_of_a_windowed_message_carries_its_window(env, monkeypatch):
    lg, idx, chats = env
    chats.update_one({"chat_id": "c1", "owner_key": "o1"}, {"$set": {"summary": ""}})
    sigs = _mixed()
    q = "latest CRM buyers"
    idx.add_search_to_chat("c1", "o1", q, lg.normalize_topic_key(q), ["kw"], "reddit", time_window_days=30)
    msg = chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"][-1]
    msg["requested_at"] = NOW
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps(_answer(sigs)))
    idx._complete_message_answer_and_results("c1", "o1", msg, sigs)
    saved = json.loads(chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"][-1]["claude_answer"])
    assert saved["time_scope"]["window"]["label"] == "last 30 days"
    assert [r.get("in_window") for r in _rows(saved)] == [True, False, False, True] or \
           sorted(r.get("in_window") for r in _rows(saved)) == [False, False, True, True]
    assert saved["counts"]["qualified"] == 2


def test_the_timeout_fallback_search_uses_the_messages_own_window(env, monkeypatch):
    lg, idx, chats = env
    seen = {}

    def fake_topup(**kw):
        seen.update(kw)
        return []
    monkeypatch.setattr(lg, "get_evidence_with_topup", fake_topup)
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps({"format": "no_results", "message": "none"}))
    for kw in ({"time_window_days": 30, "requested_at": NOW}, {}):
        seen.clear()
        try:
            lg._timeout_fallback_answer("c1", "o1", "tk", "latest CRM buyers", ["crm"], None, 20, **kw)
        except Exception as exc:                                   # persistence is not under test; the retrieval call is
            print("fallback stopped after retrieval:", type(exc).__name__)
        assert ("since_days" in seen and seen["since_days"] == 30) if kw else "since_days" not in seen


def test_strict_mode_receives_the_window_too(lg, monkeypatch):
    seen = {}
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: True)
    monkeypatch.setattr(lg, "_strict_patch_answer", lambda data, sigs, text, **kw: seen.update(kw) or text)
    sigs = _mixed()
    with lg.time_scope_context(_scope()):
        lg._patch_post_urls_into_answer(json.dumps(_answer(sigs)), sigs, query=CRM_Q)
    assert seen["time_scope"]["label"] == "last 30 days"


def test_recent_buyer_claims_in_a_comparison_subject_match_that_subjects_window_rows():
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", NOW - D(days=3)), _sig(3, "cedar", NOW - D(days=400))]
    posts = [{"index": i + 1, "title": x["title"], "link": x["post_url"], "role": "buyer", "purchase_intent": "explicit",
              "product_fit": "fits"} for i, x in enumerate(sigs)]

    def subject(name, ps, summary):
        return {"name": name, "summary": summary, "platforms": [{"platform": "reddit", "total_analyzed": len(ps),
                                                                  "shown_count": len(ps), "posts": ps}]}
    data = {"format": "comparison", "executive_summary": "",
            "subjects": [subject("A", posts[:1], "We found nine recent buyers."), subject("B", posts[1:], "We found nine recent buyers.")]}
    out = ai.enforce_answer_integrity(data, sigs, sigs, CRM_Q, time_scope=_scope())
    assert [sj["summary"] for sj in out["subjects"]] == ["Buyer posts found in the last 30 days: one.",
                                                          "Buyer posts found in the last 30 days: one."]
