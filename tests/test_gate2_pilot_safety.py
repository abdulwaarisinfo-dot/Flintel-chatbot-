"""
tests/test_gate2_pilot_safety.py
================================
GATE 2 — minimal pilot-safety fixes.

  1  platform isolation   the timeout fallback uses the request's own platform selection (cache key, retrieval, Google stubs)
  2  answer paths         plain-text replies, unknown / missing formats, website text and the Option A switch
  3  pilot configuration  the repository defaults of the settings a controlled Reddit pilot depends on
  4  newest stored post   the publication date of the newest stored post for the searched scope, and how it is shown

Synthetic records only (every title is marked); a fake collection stands in for the database; no network.
"""
import json
import re
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
from tests.test_recency_phase2 import CRM_Q, _answer, _router, _scope, _sig  # noqa: E402
from tests.test_reliability_ux_perf import OWNER, _Req  # noqa: E402
from tests.test_strict_intent import lg  # noqa: E402,F401
from tests.test_time_ranges import app_env, env, fixed_clock  # noqa: E402,F401
from tests.test_nonblocking_chat_page import fin  # noqa: E402,F401

UTC = timezone.utc
D = timedelta
NOW = datetime.now(UTC)


# ═══ 1. platform isolation in the timeout fallback ═══════════════════════════════════════════════════════════════════
def _fallback(env_fixture, monkeypatch, platform, **extra):
    lg, idx, chats = env_fixture
    seen = {"topup": [], "stubs": 0}
    monkeypatch.setattr(lg, "get_evidence_with_topup", lambda **kw: seen["topup"].append(kw) or [])

    def stubs(*a, **k):
        seen["stubs"] += 1
        return []
    monkeypatch.setattr(lg.google_search, "get_stub_results_for_keywords", stubs)
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps({"format": "no_results", "message": "none"}))
    kw = dict(extra)
    if platform != "__omit__":
        kw["targeting_platform"] = platform
    try:
        lg._timeout_fallback_answer("c1", "o1", "tk", "latest CRM buyers", ["crm"], None, 5, **kw)
    except Exception as exc:                                       # saving the answer is not under test; the retrieval call is
        print("fallback stopped after retrieval:", type(exc).__name__)
    return seen


@pytest.mark.parametrize("selection", ["reddit", "linkedin", "x_twitter,linkedin", "X / Twitter, LinkedIn", "all"])
def test_the_fallback_retrieves_with_the_requests_own_platform_selection(env, monkeypatch, selection):
    seen = _fallback(env, monkeypatch, selection)
    assert [c["targeting_platform"] for c in seen["topup"]] == [selection]


@pytest.mark.parametrize("omitted", ["__omit__", None, ""])
def test_a_request_with_no_selection_still_means_all_enabled_platforms(env, monkeypatch, omitted):
    seen = _fallback(env, monkeypatch, omitted)
    assert [c["targeting_platform"] for c in seen["topup"]] == ["all"]


@pytest.mark.parametrize("selection,stubs", [("reddit", 1), ("all", 1), ("Reddit, LinkedIn", 1), ("linkedin", 0), ("x_twitter,linkedin", 0)])
def test_google_stubs_are_reddit_records_and_are_never_merged_into_another_platforms_answer(env, monkeypatch, selection, stubs):
    assert _fallback(env, monkeypatch, selection)["stubs"] == stubs


def test_the_timeout_cache_of_another_platform_is_neither_served_nor_merged(env, monkeypatch):
    lg, idx, chats = env
    reddit_post = {"title": "[synthetic] reddit post", "post_text": "x " * 20, "post_url": "https://www.reddit.com/r/a/comments/r1/t/",
                   "platform": "reddit", "published_at": (NOW - D(days=1)).isoformat()}
    monkeypatch.setattr(lg, "get_cached_topic_evidence", lambda c, t: {"posts": [reddit_post], "keywords": ["crm"], "match_phrases": None,
                                                                      "platform_key": "reddit"})
    saved, calls = {}, []
    monkeypatch.setattr(lg, "save_topic_evidence_cache", lambda *a, **k: saved.update(args=a, kwargs=k))
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: False)
    monkeypatch.setattr(lg, "get_matched_signals", lambda *a, **k: calls.append(k.get("targeting_platform")) or [])
    monkeypatch.setattr(lg.google_search, "get_stub_results_for_keywords", lambda *a, **k: [])
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps({"format": "no_results", "message": "none"}))
    captured = {}
    real_patch = lg._patch_post_urls_into_answer
    monkeypatch.setattr(lg, "_patch_post_urls_into_answer",
                        lambda text, pool, query=None, time_scope=None: captured.update(pool=list(pool)) or real_patch(text, pool, query=query, time_scope=time_scope))
    try:
        lg._timeout_fallback_answer("c1", "o1", "tk", "latest CRM buyers", ["crm"], None, 1, targeting_platform="linkedin")
    except Exception as exc:
        print("fallback stopped:", type(exc).__name__)
    assert calls == ["linkedin"]                                              # re-searched for LinkedIn, not served from Reddit's cache
    assert all("reddit" not in json.dumps(p) for p in captured.get("pool", []))     # nothing of the Reddit cache reached the answer


@pytest.mark.parametrize("selection", ["reddit", "Reddit", "all", None])
def test_reddit_and_default_requests_keep_using_their_cache_in_the_fallback(env, monkeypatch, selection):
    lg, idx, chats = env
    post = {"title": "[synthetic] p", "post_text": "x " * 20, "post_url": "https://www.reddit.com/r/a/comments/r1/t/", "platform": "reddit",
            "published_at": (NOW - D(days=1)).isoformat()}
    key = platforms.selection_key(selection)
    monkeypatch.setattr(lg, "get_cached_topic_evidence", lambda c, t: {"posts": [post], "keywords": ["crm"], "match_phrases": None, "platform_key": key})
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: False)
    calls = []
    monkeypatch.setattr(lg, "get_matched_signals", lambda *a, **k: calls.append(1) or [])
    monkeypatch.setattr(lg.google_search, "get_stub_results_for_keywords", lambda *a, **k: [])
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps({"format": "no_results", "message": "none"}))
    try:
        lg._timeout_fallback_answer("c1", "o1", "tk", "latest CRM buyers", ["crm"], None, 1, targeting_platform=selection)
    except Exception as exc:
        print("fallback stopped:", type(exc).__name__)
    assert calls == []                                                         # served from the matching cache


def test_both_production_callers_pass_the_messages_platform_to_the_fallback():
    src = (ROOT / "index.py").read_text()
    calls = [m.start() for m in re.finditer(r"_timeout_fallback_answer(?:\(\n|, chat_id)", src)]       # direct call and background task
    assert len(calls) == 2
    for start in calls:
        call = src[start:src.index("\n                        )", start)]                  # this call only, up to its closing parenthesis
        assert 'targeting_platform=msg.get("targeting_platform")' in call


# ═══ 2. answer paths ═════════════════════════════════════════════════════════════════════════════════════════════════
CLAIMS = ["The data is live and up to date.", "Flintel monitors Reddit in real time.", "Demand has surged over the past month.",
          "Buyers posted yesterday.", "In the last 7 days there were nine buyers.", "We found 12 buyers.", "Interest is trending upward right now.",
          "Current demand is strong.", "Our index is updated continuously."]
KEEP = ["I search Reddit conversations that Flintel has already stored.", "You can ask for up to 25 posts.", "Tell me a brand, product or topic.",
        "The data is not live.", "Very recent posts may not be collected yet, so an empty result is not proof that nothing was posted.",
        "I can't tell how current the stored data is.", "There is no sign of a surge.", "Pricing is the main concern."]


@pytest.mark.parametrize("claim", CLAIMS)
def test_plain_text_claims_about_the_data_are_removed_and_neighbours_kept(claim):
    out = ai.validate_plain_text(f"{claim} I can search for CRM buyers on Reddit.")
    assert out == "I can search for CRM buyers on Reddit."


@pytest.mark.parametrize("text", KEEP)
def test_legitimate_text_uncertainty_and_negations_are_not_removed(text):
    assert ai.validate_plain_text(text) == text


def test_plain_text_markers_are_stripped_without_breaking_the_sentence():
    assert ai.validate_plain_text("Currently, I search Reddit.") == "I search Reddit."
    assert ai.validate_plain_text("Latest posts are shown first.") == "Posts are shown first."


def test_plain_text_that_is_only_a_claim_becomes_an_empty_result():
    assert ai.validate_plain_text("The data is live and up to date.") == ""
    assert ai.validate_plain_text(None) is None and ai.validate_plain_text("") == ""


def _chat(fixed_clock, monkeypatch, reply, query="what colour is a ripe banana", intent="chat"):
    lg, idx, rt, chats, state = fixed_clock
    _router(lg, monkeypatch, intent=intent, reply=reply, keywords=None, time_window_days=None, match_phrases=None, evidence_required=None)
    resp = rt.search(_Req(), query=query, platform="Reddit", chat_id=None, tz="Asia/Karachi")
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    return chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]["claude_answer"], lg


@pytest.mark.parametrize("intent", ["chat", "clarify"])
def test_a_model_written_chat_or_clarify_reply_cannot_state_unsupported_facts(fixed_clock, monkeypatch, intent):
    saved, lg = _chat(fixed_clock, monkeypatch, "Our Reddit data is live and up to date. Tell me which product you sell.", intent=intent)
    assert saved == "Tell me which product you sell."


def test_a_chat_reply_that_is_only_a_claim_gets_the_fixed_unavailable_reply(fixed_clock, monkeypatch):
    saved, lg = _chat(fixed_clock, monkeypatch, "Demand has surged over the past month.")
    assert saved == lg.CHAT_UNAVAILABLE_REPLY


def test_a_clean_chat_reply_and_the_fixed_small_talk_reply_are_untouched(fixed_clock, monkeypatch):
    saved, lg = _chat(fixed_clock, monkeypatch, "I search Reddit posts Flintel has stored; the data is not live. Which topic?")
    assert saved == "I search Reddit posts Flintel has stored; the data is not live. Which topic?"
    lg2, idx, rt, chats, state = fixed_clock
    resp = rt.search(_Req(), query="hello", platform="Reddit", chat_id=None, tz="Asia/Karachi")
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    assert chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]["claude_answer"] == lg2.smalltalk_reply("hello")


def test_a_non_json_answer_is_checked_as_plain_text(lg):
    sigs = [_sig(1, "a", NOW - D(days=1))]
    assert lg._patch_post_urls_into_answer("Demand is surging. Pricing matters.", sigs, query=CRM_Q) == "Pricing matters."
    assert lg._patch_post_urls_into_answer("Demand is surging.", [], query=CRM_Q) == lg.CHAT_UNAVAILABLE_REPLY
    assert lg._patch_post_urls_into_answer("Demand is surging.", sigs) == "Demand is surging."          # no query: not a production caller
    cut = '{"format": "source_list", "executive_summary": "Demand is surging'
    assert lg._patch_post_urls_into_answer(cut, sigs, query=CRM_Q) == cut                              # a cut-off object is not plain text


@pytest.mark.parametrize("data", [{"message": "Demand is surging. Pricing matters."}, {"format": None, "message": "Demand is surging. Pricing matters."},
                                  {"format": "mystery", "message": "Demand is surging. Pricing matters."},
                                  {"format": "mystery", "nested": {"deep": ["Demand is surging.", "Pricing matters."]}}])
def test_a_missing_or_unknown_format_is_validated(lg, data):
    with lg.time_scope_context(_scope(7)):
        out = json.loads(lg._patch_post_urls_into_answer(json.dumps(data), [_sig(1, "a", NOW)], query=CRM_Q))
    text = json.dumps({k: v for k, v in out.items() if k != "time_scope"})
    assert "surging" not in text and "Pricing matters." in text
    assert out["time_scope"]["window"]["label"] == "last 7 days"


def test_an_unknown_format_with_only_legitimate_text_is_unchanged_apart_from_the_time_scope(lg):
    data = {"format": "mystery", "message": "No posts matched. The data is not live."}
    out = json.loads(lg._patch_post_urls_into_answer(json.dumps(data), [], query=CRM_Q))
    assert out["message"] == data["message"] and out["format"] == "mystery"


def test_website_text_is_checked_only_where_it_speaks_about_flintel_or_the_data(lg):
    ctx = {"overview": "Acme offers real-time analytics for sales teams. Flintel monitors Reddit in real time.",
           "sections": [{"title": "Product", "bullets": ["Live dashboards for every account.", "Reddit demand has surged this week.", "Pricing starts low."]},
                        {"title": "Flintel data is live and up to date", "bullets": []}, "not a dict"]}
    out = json.loads(lg._inject_website_context_into_answer(json.dumps({"format": "no_results", "message": "m"}), ctx))["website_context"]
    assert out["overview"] == "Acme offers real-time analytics for sales teams."
    assert out["sections"][0] == {"title": "Product", "bullets": ["Live dashboards for every account.", "Pricing starts low."]}
    assert out["sections"][1]["title"] == "" and out["sections"][2] == "not a dict"
    assert lg._clean_website_context("odd") == "odd" and lg._clean_website_context({"overview": 5}) == {"overview": 5}


# ── Option A ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _previous_results(fixed_clock, monkeypatch, enabled):
    import config
    lg, idx, rt, chats, state = fixed_clock
    monkeypatch.setattr(config, "OPTION_A_ENABLED", enabled, raising=False)
    first = _search_once(fixed_clock, monkeypatch)
    calls = []
    monkeypatch.setattr(rt, "build_outreach_report", lambda *a, **k: calls.append(1) or "OUTREACH REPORT (synthetic)")
    monkeypatch.setattr(rt, "classify_and_maybe_chat", lambda *a, **k: {"intent": "previous_results", "previous_results_numbers": None})
    resp = rt.search(_Req(), query="Option A please", platform="Reddit", chat_id=first, tz="Asia/Karachi")
    cid = resp.headers["location"].rsplit("/", 1)[-1]
    return calls, chats.find_one({"chat_id": cid, "owner_key": OWNER})["messages"][-1]["claude_answer"], rt


def _search_once(fixed_clock, monkeypatch):
    lg, idx, rt, chats, state = fixed_clock
    _router(lg, monkeypatch)
    resp = rt.search(_Req(), query="Find people looking for a CRM", platform="Reddit", chat_id=None, tz="Asia/Karachi")
    return resp.headers["location"].rsplit("/", 1)[-1]


def test_option_a_is_off_by_default_and_cannot_be_invoked(fixed_clock, monkeypatch):
    import config
    assert config.OPTION_A_ENABLED is False                                    # the repository default
    calls, saved, rt = _previous_results(fixed_clock, monkeypatch, enabled=False)
    assert calls == []                                                         # build_outreach_report never ran
    assert saved == rt.OPTION_A_DISABLED_REPLY and "isn't available" in saved   # an honest reply, not silence and not a report


def test_option_a_is_reversible_by_the_switch_alone(fixed_clock, monkeypatch):
    calls, saved, rt = _previous_results(fixed_clock, monkeypatch, enabled=True)
    assert calls == [1] and saved == "OUTREACH REPORT (synthetic)"             # exactly the pre-Gate-2 behaviour when switched on


def test_the_router_is_not_offered_the_previous_result_when_option_a_is_off(fixed_clock, monkeypatch):
    import config
    lg, idx, rt, chats, state = fixed_clock
    monkeypatch.setattr(config, "OPTION_A_ENABLED", False, raising=False)
    cid = _search_once(fixed_clock, monkeypatch)
    block_calls = []
    monkeypatch.setattr(rt, "build_previous_results_router_block", lambda *a, **k: block_calls.append(1) or "BLOCK")
    monkeypatch.setattr(rt, "mentions_previous_results", lambda q: True)
    seen = {}
    monkeypatch.setattr(rt, "classify_and_maybe_chat", lambda *a, **k: seen.update(k) or {"intent": "chat", "reply": "ok then"})
    latest = {"offer": {"options": ["A", "B"]}, "original_query": "q", "offered": "Option A: outreach notes", "posts": [{"number": 1}]}
    monkeypatch.setattr(rt, "_get_latest_search_result", lambda chat: latest)           # a chat whose last answer offered options
    rt.search(_Req(), query="outreach notes for these posts", platform="Reddit", chat_id=cid, tz="Asia/Karachi")
    assert block_calls == [] and seen.get("previous_results_context") is None
    monkeypatch.setattr(config, "OPTION_A_ENABLED", True, raising=False)                # the same chat with the switch on
    seen.clear()
    rt.search(_Req(), query="outreach notes for these posts", platform="Reddit", chat_id=cid, tz="Asia/Karachi")
    assert block_calls == [1] and seen.get("previous_results_context") == "BLOCK"


def test_picking_the_option_in_a_reply_does_not_start_option_a_when_it_is_off(fixed_clock, monkeypatch):
    import config
    lg, idx, rt, chats, state = fixed_clock
    cid = _search_once(fixed_clock, monkeypatch)
    calls = []
    monkeypatch.setattr(rt, "build_outreach_report", lambda *a, **k: calls.append(1) or "OUTREACH REPORT (synthetic)")
    monkeypatch.setattr(rt, "detect_offer_selection", lambda q, offered: {"kind": "previous_results", "options": [], "numbered": False})
    monkeypatch.setattr(rt, "classify_and_maybe_chat", lambda *a, **k: {"intent": "chat", "reply": "ok then"})   # the router did NOT pick it
    for enabled in (False, True):
        calls.clear()
        monkeypatch.setattr(config, "OPTION_A_ENABLED", enabled, raising=False)
        resp = rt.search(_Req(), query="go with A", platform="Reddit", chat_id=cid, tz="Asia/Karachi")
        saved = chats.find_one({"chat_id": resp.headers["location"].rsplit("/", 1)[-1], "owner_key": OWNER})["messages"][-1]["claude_answer"]
        if enabled:
            assert calls == [1] and saved == "OUTREACH REPORT (synthetic)"
        else:
            assert calls == [] and saved == rt.OPTION_A_DISABLED_REPLY


# ═══ 3. pilot configuration (repository defaults) ════════════════════════════════════════════════════════════════════
PILOT = {"STRICT_INTENT_MODE": "false", "INTENT_BRIDGE_ENABLED": "", "MYSQL_READ_ENABLED": "false", "OPTION_A_ENABLED": "false"}


@pytest.mark.parametrize("name,default", sorted(PILOT.items()))
def test_the_repository_defaults_match_the_proposed_pilot_configuration(name, default):
    src = (ROOT / "config.py").read_text()
    m = re.search(rf'^{name}\s*=\s*os\.getenv\("{name}",\s*"([^"]*)"\)', src, re.M)
    assert m and m.group(1) == default, f"{name}: {m.group(1) if m else 'not defined'}"


def test_freshness_disclosure_is_not_defined_in_config_and_stays_off():
    # FRESHNESS_DISCLOSURE_ENABLED belongs to LI-02 M2; it is read with a code default of False and has no config attribute.
    import config
    assert not hasattr(config, "FRESHNESS_DISCLOSURE_ENABLED")
    assert not re.search(r"FRESHNESS_DISCLOSURE_ENABLED\s*=", (ROOT / "config.py").read_text())


def test_the_pilot_never_reads_mysql_for_the_newest_stored_post():
    src = (ROOT / "logics.py").read_text()
    body = src[src.index("def newest_stored_post"):src.index("def build_time_scope")]
    assert "mysql" not in body.lower()


# ═══ 4. newest stored post ═══════════════════════════════════════════════════════════════════════════════════════════
class _Cursor(list):
    def sort(self, *a, **k):
        return _Cursor(sorted(self, key=lambda d: d["created_utc"], reverse=True))

    def limit(self, n):
        return _Cursor(self[:n])

    def max_time_ms(self, ms):
        return self


class FakeColl:
    """A collection double that evaluates the ONE query shape newest_stored_post sends: a `created_utc` upper bound (only real
    datetimes can satisfy it, like Mongo's type bracketing) and the optional platform clause."""
    def __init__(self, docs, fail=False):
        self.docs, self.fail, self.queries = list(docs), fail, []

    def find(self, query, projection=None):
        if self.fail:
            raise RuntimeError("source down")
        self.queries.append(query)
        conds = query["$and"] if "$and" in query else [query]
        bound = conds[0]["created_utc"]["$lt"]
        clause = conds[1] if len(conds) > 1 else None
        out = []
        for d in self.docs:
            c = d.get("created_utc")
            if not isinstance(c, datetime):
                continue
            if (c if c.tzinfo else c.replace(tzinfo=UTC)) >= bound:
                continue
            if clause:
                plat, url = d.get("platform"), d.get("post_url", "")
                ok = False
                for branch in clause["$or"]:
                    p = branch["platform"]
                    if "$regex" in p:
                        ok = ok or bool(plat and re.search(p["$regex"], plat, re.I))
                    else:
                        host_ok = re.search(branch["post_url"]["$regex"], url, re.I)
                        ok = ok or (plat in (None, "") and bool(host_ok))
                if not ok:
                    continue
            out.append({"created_utc": c})
        return _Cursor(out)


def _doc(when, platform="reddit", url="https://www.reddit.com/r/a/comments/x1/t/", **extra):
    d = {"created_utc": when, "platform": platform, "post_url": url}
    d.update(extra)
    return d


@pytest.fixture
def colls(lg, monkeypatch):
    def install(primary, c2=None, c4=None):
        monkeypatch.setattr(lg, "signals_collection", primary)
        monkeypatch.setattr(lg, "signals_collection_2", c2)
        monkeypatch.setattr(lg, "signals_collection_4", c4)
        lg._NEWEST_CACHE.clear()
    return install


def test_the_newest_valid_stored_post_is_reported(lg, colls):
    colls(FakeColl([_doc(NOW - D(days=9)), _doc(NOW - D(days=2)), _doc(NOW - D(days=40))]))
    r = lg.newest_stored_post("reddit")
    assert (r["status"], r["published_at"], r["scope"]) == ("found", (NOW - D(days=2)).isoformat(), "reddit")


def test_future_dated_undated_and_invalid_records_are_never_the_newest(lg, colls):
    docs = [_doc(NOW + D(days=40)), _doc(None), _doc("2026-10-20T00:00:00+00:00"), _doc(datetime(1970, 1, 1, tzinfo=UTC)), _doc(NOW - D(days=5))]
    colls(FakeColl(docs))
    assert lg.newest_stored_post("all")["published_at"] == (NOW - D(days=5)).isoformat()


def test_a_row_creation_time_is_never_used_in_place_of_the_publication_time(lg, colls):
    colls(FakeColl([_doc(NOW - D(days=30), ingested_at=NOW, created_at=NOW, inserted_at=NOW)]))
    assert lg.newest_stored_post("reddit")["published_at"] == (NOW - D(days=30)).isoformat()


def test_an_empty_or_unusable_scope_is_reported_as_empty(lg, colls):
    colls(FakeColl([]))
    assert lg.newest_stored_post("reddit")["status"] == "empty"
    colls(FakeColl([_doc(datetime(1970, 1, 1, tzinfo=UTC)), _doc(NOW + D(days=3))]))
    r = lg.newest_stored_post("reddit")
    assert r["status"] == "empty" and r["published_at"] is None


def test_the_platform_selection_scopes_the_answer_like_the_search_does(lg, colls):
    li = _doc(NOW - D(hours=1), platform="LinkedIn", url="https://www.linkedin.com/posts/p1")
    rd = _doc(NOW - D(days=3), platform="Reddit")
    nop = _doc(NOW - D(days=2), platform=None, url="https://old.reddit.com/r/a/comments/y2/t/")
    colls(FakeColl([li, rd, nop]))
    assert lg.newest_stored_post("reddit")["published_at"] == (NOW - D(days=2)).isoformat()          # a Reddit URL without a platform counts
    assert lg.newest_stored_post("all")["published_at"] == (NOW - D(hours=1)).isoformat()
    assert lg.newest_stored_post("linkedin")["published_at"] == (NOW - D(hours=1)).isoformat()
    assert lg.newest_stored_post("X / Twitter, LinkedIn")["scope"] == "x_twitter,linkedin" and \
        lg.newest_stored_post("linkedin,x_twitter")["scope"] == "x_twitter,linkedin"
    assert lg.newest_stored_post(None)["scope"] == "all" and lg.newest_stored_post("")["scope"] == "all"


def test_mirrors_are_read_and_a_failing_source_makes_the_answer_partial_or_unavailable(lg, colls):
    primary, mirror = FakeColl([_doc(NOW - D(days=6))]), FakeColl([_doc(NOW - D(days=1))])
    colls(primary, mirror)
    r = lg.newest_stored_post("reddit")
    assert r["status"] == "found" and r["published_at"] == (NOW - D(days=1)).isoformat() and r["sources_read"] == 2
    colls(primary, FakeColl([], fail=True))
    r = lg.newest_stored_post("reddit")
    assert r["status"] == "partial" and r["sources_failed"] == 1 and r["published_at"] == (NOW - D(days=6)).isoformat()
    colls(FakeColl([], fail=True), FakeColl([], fail=True))
    r = lg.newest_stored_post("reddit")
    assert r["status"] == "unavailable" and r["published_at"] is None
    assert lg.newest_stored_post("reddit")["status"] == "unavailable"                              # a failure is not cached


def test_the_lookup_is_cached_briefly_and_never_raises(lg, colls):
    primary = FakeColl([_doc(NOW - D(days=2))])
    colls(primary)
    lg.newest_stored_post("reddit")
    lg.newest_stored_post("reddit")
    assert len(primary.queries) == 1                                                               # second call served from the cache
    lg.newest_stored_post("reddit", now=NOW)                                                       # an explicit clock bypasses it
    assert len(primary.queries) == 2
    colls(object())                                                                                # a source that is not a collection at all
    assert lg.newest_stored_post("reddit")["status"] == "unavailable"


def test_the_query_is_bounded_above_by_the_future_tolerance(lg, colls):
    primary = FakeColl([_doc(NOW - D(days=2))])
    colls(primary)
    fixed = datetime(2026, 3, 1, 12, tzinfo=UTC)
    lg.newest_stored_post("all", now=fixed)
    assert primary.queries[-1] == {"created_utc": {"$lt": fixed + timerange.FUTURE_TOLERANCE}}


def test_build_time_scope_adds_the_newest_post_only_for_production_callers(lg, colls):
    colls(FakeColl([_doc(NOW - D(days=3))]))
    assert lg.build_time_scope(None, None, NOW) is None and lg.build_time_scope(7, None, NOW)["label"] == "last 7 days"      # unchanged API
    unbounded = lg.build_time_scope(None, None, NOW, targeting_platform="reddit")
    assert set(unbounded) == {"newest_stored"} and unbounded["newest_stored"]["status"] == "found"
    bounded = lg.build_time_scope(7, None, NOW, targeting_platform="reddit")
    assert bounded["label"] == "last 7 days" and bounded["newest_stored"]["published_at"] == (NOW - D(days=3)).isoformat()
    assert ai._window_bounds(unbounded) is None and ai._window_bounds(bounded) is not None


def test_the_answer_carries_a_cleaned_newest_stored_record(lg):
    sigs = [_sig(1, "a", NOW - D(days=2))]
    for status, when, shown in [("found", (NOW - D(days=3)).isoformat(), "found"), ("partial", (NOW - D(days=3)).isoformat(), "partial"),
                                ("empty", None, "empty"), ("unavailable", None, "unavailable"),
                                ("found", "garbage", "unavailable"), ("found", (NOW + D(days=3)).isoformat(), "unavailable")]:
        scope = dict(_scope(7), newest_stored={"status": status, "published_at": when, "scope": "reddit", "extra": "dropped"})
        ns = ai.enforce_answer_integrity(_answer(sigs), sigs, sigs, CRM_Q, time_scope=scope)["time_scope"]["newest_stored"]
        assert ns["status"] == shown and set(ns) == {"status", "published_at", "scope"}, (status, when)
    for bad in (None, "x", {}, {"status": "weird"}):
        out = ai.enforce_answer_integrity(_answer(sigs), sigs, sigs, CRM_Q, time_scope=dict(_scope(7), newest_stored=bad))
        assert "newest_stored" not in out["time_scope"]


def test_every_structured_format_carries_the_same_newest_stored_record(lg):
    scope = dict(newest_stored={"status": "found", "published_at": (NOW - D(days=3)).isoformat(), "scope": "reddit"})
    for fmt in ("no_results", "not_available", "disallowed", "trend_report", "mystery"):
        with lg.time_scope_context(scope):
            out = json.loads(lg._patch_post_urls_into_answer(json.dumps({"format": fmt, "message": "m"}), [], query=CRM_Q))
        assert out["time_scope"]["newest_stored"]["published_at"] == (NOW - D(days=3)).isoformat() and out["time_scope"]["window"] is None
    internal = ai.enforce_answer_integrity(
        {"format": "source_list", "executive_summary": "", "key_findings": [], "platforms": [{"platform": "reddit", "posts": [
            {"index": 1, "title": "x", "link": "https://example.invalid/none", "role": "buyer", "purchase_intent": "explicit", "product_fit": "fits"}]}]},
        [_sig(1, "a", NOW)], [_sig(1, "a", NOW)], CRM_Q, time_scope=scope)
    assert internal["format"] == "no_results" and internal["time_scope"]["newest_stored"]["status"] == "found"


def test_the_streaming_route_saves_the_newest_stored_post_for_the_messages_platform(request, monkeypatch):
    import tests.test_nonblocking_chat_page as nb
    f = request.getfixturevalue("fin")
    rt = f["rt"]
    seen = []

    def fake(platform=None, now=None):
        seen.append(platform)
        return {"status": "found", "published_at": (NOW - D(days=4)).isoformat(), "scope": platforms.selection_key(platform),
                "sources_read": 1, "sources_failed": 0}
    monkeypatch.setitem(rt.build_time_scope.__globals__, "newest_stored_post", fake)
    sigs = [_sig(1, "alpha", NOW - D(days=2))]
    turn = dict(nb._turn(CRM_Q, 1), evidence_required=1, time_window_days=None, requested_at=NOW, targeting_platform="reddit")
    nb._seed(f["chats"], "g2", [turn])
    monkeypatch.setattr(rt, "get_evidence_with_topup", lambda **kw: [dict(s) for s in sigs])
    monkeypatch.setattr(rt, "analyze_with_claude", lambda *a, **k: json.dumps(_answer(sigs)), raising=False)
    for chunk in nb._stream(f, monkeypatch, "g2", topic_key=CRM_Q.lower()):
        s = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        if '"done"' in s or '"error"' in s:
            break
    saved = json.loads(f["chats"].find_one({"chat_id": "g2"})["messages"][-1]["claude_answer"])
    assert seen == ["reddit"]
    assert saved["time_scope"]["window"] is None                                        # an unbounded search ...
    assert saved["time_scope"]["newest_stored"] == {"status": "found", "published_at": (NOW - D(days=4)).isoformat(), "scope": "reddit"}


# ── how it is shown ───────────────────────────────────────────────────────────────────────────────────────────────────
from tests.test_answer_persistence import ORIGIN, _render  # noqa: E402

FORBIDDEN = ("last collected", "last updated", "live", "real-time", "real time", "up to date", "freshly", "continuously")


def _open_any(browser, name, answer):
    chat = {"chat_id": "c1", "owner_key": "o", "title": "t", "created_at": NOW, "updated_at": NOW, "summary": "",
            "messages": [{"query": "latest CRM buyers", "topic_key": "latest crm buyers", "results": [], "keywords": ["crm"],
                          "claude_answer": json.dumps(answer), "requested_at": NOW, "answered_at": NOW,
                          "targeting_platform": "reddit", "message_type": "search"}]}
    pages = {"/chat/c1": _render("chat.html", chat), "/": _render("index.html", None)}
    ctx = browser.new_context(timezone_id="Asia/Karachi", locale="en-US")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def handle(route):
        url = route.request.url
        path = url[len(ORIGIN):].split("?")[0] if url.startswith(ORIGIN) else None
        return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[path]) if path in pages else route.fulfill(status=200, body="")
    page.route("**/*", handle)
    if name == "chat.html":
        page.goto(ORIGIN + "/chat/c1")
    else:
        page.goto(ORIGIN + "/")
        page.evaluate("navigateTo('/chat/c1'); 0")
        page.wait_for_function("document.querySelectorAll('.turn-block').length > 0")
    page.wait_for_selector(".answer-rendered .ap-newest-stored", timeout=5000)
    return page, ctx, errors


def _answer_with(fmt, status, when, window=None):
    ns = {"status": status, "published_at": when, "scope": "reddit"}
    scope = dict(window or {}, newest_stored=ns)
    if fmt == "source_list":
        sigs = [_sig(1, "alpha", NOW - D(days=5))]          # a listed post is never newer than the newest stored post (the old fixture was impossible)
        return ai.enforce_answer_integrity(_answer(sigs), sigs, sigs, CRM_Q, time_scope=scope)
    return ai.enforce_temporal_claims({"format": fmt, "message": "Nothing to report.", "searched": {"time_window": "all posts"}}, [], scope)


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
@pytest.mark.parametrize("fmt", ["source_list", "no_results", "not_available", "disallowed"])
def test_the_ui_states_the_newest_stored_post_plainly(integrity_browser, name, fmt):
    when = datetime(NOW.year, NOW.month, NOW.day, 23, 30, tzinfo=UTC) - D(days=3)           # 23:30 UTC: the next day in Karachi
    page, ctx, errors = _open_any(integrity_browser, name, _answer_with(fmt, "found", when.isoformat()))
    try:
        line = page.inner_text(".answer-rendered .ap-newest-stored")
        want = when.strftime("%b ") + str(when.day) + ", " + str(when.year) + " UTC"
        assert want in line and "not when Flintel stored it" in line
        assert page.locator(".answer-rendered .ap-newest-stored").count() == 1                  # shown once, not duplicated
        low = page.inner_text(".answer-rendered").lower()
        assert not any(w in low for w in FORBIDDEN) and errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
@pytest.mark.parametrize("status,expect", [("unavailable", "unavailable. Flintel does not record when posts were collected."),
                                          ("empty", "none found for this search scope."),
                                          ("partial", "a newer post may exist.")])
def test_the_ui_says_when_the_date_cannot_be_given(integrity_browser, name, status, expect):
    when = (NOW - D(days=3)).isoformat() if status == "partial" else None
    page, ctx, errors = _open_any(integrity_browser, name, _answer_with("not_available", status, when))
    try:
        line = page.inner_text(".answer-rendered .ap-newest-stored")
        assert expect in line and not any(w in line.lower() for w in FORBIDDEN) and errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_an_answer_without_the_record_shows_no_line(integrity_browser, name):
    sigs = [_sig(1, "alpha", NOW - D(days=2)), _sig(2, "bravo", None)]
    out = ai.enforce_answer_integrity(_answer(sigs), sigs, sigs, CRM_Q)                 # an older / unscoped answer
    chat_answer = dict(out)
    from tests.test_recency_phase2 import _open
    page, ctx, errors = _open(integrity_browser, name, chat_answer)
    try:
        assert page.locator(".answer-rendered .ap-newest-stored").count() == 0 and errors == []
    finally:
        ctx.close()
