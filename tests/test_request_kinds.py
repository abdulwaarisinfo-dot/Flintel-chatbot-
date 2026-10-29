"""
tests/test_request_kinds.py — Flintel must understand WHAT the user asks before choosing retrieval, filtering, evidence and wording.

Before this change every search went through the lead pipeline (buyer intent, pain points, qualified leads). The router now also
returns a request_kind ("lead_discovery" = the unchanged pipeline, "mention_search", "research", "person_mentions"); the kind is
stored on the message and decides (1) what is retrieved, (2) how a post may be kept, dropped and labelled, (3) what the empty
state says. Person-profiling requests and bare names are NOT request kinds: they are the existing "blocked" / "clarify" intents.

What is verified here (no real LLM / Mongo / network): the router's JSON is scripted (what a correct router returns for each
request); the REAL parser, matcher, message storage, answer post-processing and empty-state code run around it. WHETHER THE LIVE
MODEL CLASSIFIES A REQUEST THIS WAY IS NOT VERIFIED HERE. The posts are invented fixtures; no real Reddit data was used.
"""
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import answer_integrity as ai  # noqa: E402
from tests.test_conversation_context import FakeChats, env, Session, session, _search  # noqa: E402,F401  (fixtures + harness)
from tests.test_answer_integrity import _sig, _answer, _rows  # noqa: E402

mongomock = pytest.importorskip("mongomock")
NOW = datetime.now(timezone.utc)


def _router(kind=None, subject=None, keywords=None, intent="search", reply=None, resolved="x"):
    out = {"intent": intent, "reply": reply, "keywords": keywords, "time_window_days": None,
           "match_phrases": ["something a buyer would write"] if intent == "search" else None, "evidence_required": None,
           "website_only": False, "url_ask_type": "none", "target_entity": "people", "intent_types": None,
           "resolved_query": resolved}
    if kind:
        out.update(request_kind=kind, subject=subject)
    return out


# ── 1. the router's contract and its parsing ───────────────────────────────────────────────────────────────────────────────
def test_router_prompt_defines_the_kinds_and_the_person_boundary(env):
    lg, _, _ = env
    p = lg.CLAUDE_ROUTER_SYSTEM_PROMPT
    for kind in ("lead_discovery", "mention_search", "research", "person_mentions"):
        assert f'"{kind}"' in p
    assert "does not say who the person is" in p and "does not link posts to one individual" in p      # the mention-only boundary
    assert "IDENTIFY, PROFILE or DIG UP a person" in p and 'intent "blocked"' in p                      # profiling stays refused
    assert 'intent "clarify"' in p and "NO clear instruction" in p                                      # a bare name is clarified
    assert "matching name does not show the posts are about the same person" in p


@pytest.mark.parametrize("kind,subject,expect_kw", [
    ("person_mentions", "Hamza Ali", ["Hamza Ali"]),
    ("mention_search", "Acme", ["Acme"]),
])
def test_mention_kinds_are_parsed_and_search_the_literal_subject(env, kind, subject, expect_kw):
    lg, _, _ = env
    parsed = lg._parse_router_json(json.dumps(_router(kind, subject, keywords=["buyer style words"])))
    assert parsed["intent"] == "search" and parsed["request_kind"] == kind and parsed["subject"] == subject
    assert parsed["keywords"] == expect_kw and parsed["match_phrases"] is None     # buyer-style phrasing is dropped for a literal search


def test_research_is_parsed_and_keeps_its_keywords(env):
    lg, _, _ = env
    parsed = lg._parse_router_json(json.dumps(_router("research", None, keywords=["shopify abandoned cart complaints"])))
    assert parsed["request_kind"] == "research" and parsed["subject"] is None and parsed["keywords"] == ["shopify abandoned cart complaints"]


@pytest.mark.parametrize("raw", [
    _router(None, None, keywords=["ai customer support"]),                          # old-style router reply: no kind at all
    _router("lead_discovery", None, keywords=["ai customer support"]),               # the default, stated
    _router("something_new", "x", keywords=["k"]),                                   # unknown kind -> the lead pipeline
    _router("mention_search", None, keywords=["k"]),                                 # a mention kind without a subject -> lead pipeline
    _router("person_mentions", "  ", keywords=["k"]),
    _router("mention_search", "x" * 200, keywords=["k"]),                            # unusable subject
    _router("mention_search", "a b c d e f g h", keywords=["k"]),
])
def test_everything_else_is_exactly_the_unchanged_lead_result(env, raw):
    lg, _, _ = env
    parsed = lg._parse_router_json(json.dumps(raw))
    assert "request_kind" not in parsed and "subject" not in parsed                 # the dict is byte-for-byte what it was before
    assert parsed["keywords"] == raw["keywords"]


def test_profiling_and_ambiguous_requests_keep_their_existing_intents(env, monkeypatch):
    lg, _, _ = env
    blocked = {"intent": "blocked", "reply": "Flintel does not identify or profile individuals.", "keywords": None,
               "time_window_days": None, "match_phrases": None, "evidence_required": None}
    clarify = {"intent": "clarify", "reply": "Do you want the posts that literally mention “Hamza Ali”? A matching name "
               "does not show the posts are about the same person.", "keywords": None, "time_window_days": None,
               "match_phrases": None, "evidence_required": None}
    for raw, intent in ((blocked, "blocked"), (clarify, "clarify")):
        monkeypatch.setattr(lg, "_call_claude", lambda *a, _raw=raw, **k: json.dumps(_raw))
        routed = lg.classify_and_maybe_chat("who is Hamza Ali and where does he live?" if intent == "blocked" else "Hamza Ali", "")
        assert routed["intent"] == intent and routed["reply"] and routed["keywords"] is None and "request_kind" not in routed


# ── 2. retrieval: a mention is a literal match, not an embedding score ─────────────────────────────────────────────────────────
def _signals(env, monkeypatch, docs):
    lg, _, _ = env
    coll = mongomock.MongoClient()["sig"].signals
    if docs:
        coll.insert_many(docs)
    monkeypatch.setattr(lg, "signals_collection", coll)
    return lg


def _doc(n, title, text, days_ago=1, platform="reddit", **extra):
    return dict({"title": title, "post_text": text, "post_url": f"https://www.reddit.com/r/x/comments/{n}/t/",
                 "platform": platform, "created_utc": NOW - timedelta(days=days_ago), "embedding": [0.1] * 8}, **extra)


def test_literal_matcher_returns_posts_that_name_the_person_without_any_buying_intent(env, monkeypatch):
    lg = _signals(env, monkeypatch, [
        _doc(1, "Met Hamza Ali at the meetup", "Great talk by Hamza Ali on compilers."),
        _doc(2, "Question", "has anyone heard of hamza ali?"),                    # lower case
        _doc(3, "Username", "follow u/HamzaAli for updates"),                      # handle without a space
        _doc(4, "Different person", "Hamza Aliyev won the match"),                  # not the same word
        _doc(5, "Only the first name", "Hamza is a nice guy"),
        _doc(6, "Unrelated", "How do I fix my CSS grid?"),
        _doc(7, "Reversed", "Ali, Hamza was listed first in the roster"),
    ])
    matcher = lg.literal_mention_matcher("Hamza Ali")
    posts = matcher("tk", ["Hamza Ali"], targeting_platform="all", limit=20)
    urls = {p["post_url"].split("/")[-3] for p in posts}
    assert urls == {"1", "2", "3", "7"}                                            # exact name only; Aliyev / Hamza alone / unrelated are out
    assert all(set(p) >= {"title", "post_text", "post_url", "platform"} for p in posts)


def test_literal_matcher_honours_platform_window_limit_and_dedupe(env, monkeypatch):
    lg = _signals(env, monkeypatch, [
        _doc(1, "Acme news", "Acme launched", days_ago=1),
        _doc(2, "Acme old", "Acme was founded", days_ago=400),
        _doc(3, "Acme on X", "Acme tweet", platform="x"),
        _doc(4, "Acme again", "Acme again", days_ago=2),
        _doc(5, "dup", "Acme dup", post_url="https://www.reddit.com/r/x/comments/1/t/"),
    ])
    m = lg.literal_mention_matcher("Acme")
    assert {p["post_url"].split("/")[-3] for p in m("t", ["Acme"], targeting_platform="reddit", limit=20)} == {"1", "2", "4"}
    assert {p["post_url"].split("/")[-3] for p in m("t", ["Acme"], targeting_platform="reddit", since_days=30, limit=20)} == {"1", "4"}
    assert len(m("t", ["Acme"], targeting_platform="all", limit=2)) == 2
    assert m("t", ["Acme"], limit=20).count(None) == 0 and len({p["post_url"] for p in m("t", ["Acme"], limit=20)}) == len(m("t", ["Acme"], limit=20))


def test_literal_matcher_zero_hits_is_empty_and_a_failed_read_is_reported_incomplete(env, monkeypatch):
    lg = _signals(env, monkeypatch, [_doc(1, "nothing", "nothing relevant here")])
    assert lg.literal_mention_matcher("Acme")("t", ["Acme"], chat_id="c1") == []
    assert lg.get_scan_status("c1", "t")["complete"] is True                          # a clean read that found nothing: a true zero

    class Broken:
        def find(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(lg, "signals_collection", Broken())
    assert lg.literal_mention_matcher("Acme")("t", ["Acme"], chat_id="c2") == []
    assert lg.get_scan_status("c2", "t")["complete"] is False                         # a failed read is "incomplete", not "no posts"


def test_matcher_for_kind_only_replaces_the_matcher_for_mention_kinds(env):
    lg, _, _ = env
    default = object()
    assert lg.matcher_for_kind(None, None, default) is default
    assert lg.matcher_for_kind("lead_discovery", None, default) is default
    assert lg.matcher_for_kind("research", "x", default) is default
    assert lg.matcher_for_kind("mention_search", "Acme", default) is not default
    assert lg.matcher_for_kind("mention_search", None, default) is default           # no subject: never searches for nothing
    assert lg.matcher_for_kind("person_mentions", "Hamza Ali", default) is not default


# ── 3. qualification and labels (the integrity layer) ──────────────────────────────────────────────────────────────────────
def _post_entry(sig, **labels):
    return dict({"index": 1, "link": sig["post_url"], "title": sig["title"], "summary": "s"}, **labels)


def _check(kind, subject, sigs, posts, query):
    with ai.request_kind_context(kind, subject, len(sigs)):
        return ai.enforce_answer_integrity(_answer(posts), sigs, sigs, query)


def test_a_mention_without_buying_intent_is_kept_and_never_labelled_a_lead():
    s = _sig(1, "Met Hamza Ali at the meetup", "Great talk by Hamza Ali on compilers.")
    # even a model that calls it irrelevant / unrelated (a buyer-lens judgement) cannot drop a post that names the subject
    out = _check("person_mentions", "Hamza Ali", [s], [_post_entry(s, role="irrelevant", product_fit="unrelated")],
                 "Find Reddit posts mentioning Hamza Ali.")
    rows = _rows(out)
    assert len(rows) == 1 and rows[0]["intent"] == "" and rows[0]["qualified"] is False and rows[0]["role"] == "discussion"
    assert "does not show the post is about any particular person" in rows[0]["intent_reason"]
    assert out["counts"]["qualified"] == 0 and out["counts"]["buyers"] == 0 and out["counts"]["shown"] == 1
    assert "qualified lead" not in out["verified_summary"] and "purchase step" not in out["verified_summary"]


def test_a_brand_mention_is_kept_with_a_mention_row_not_a_lead_row():
    s = _sig(1, "Acme vs the rest", "Switched our stack to Acme last month, happy so far.")
    out = _check("mention_search", "Acme", [s], [_post_entry(s, role="testimonial")], "Find Reddit posts mentioning Acme.")
    r = _rows(out)[0]
    assert r["intent"] == "" and not r["qualified"] and r["role"] == "discussion" and "Acme" in (r["evidence"] or "")
    assert out["counts"]["competitor"] == 0                                         # not mis-filed as a vendor/testimonial lead category


def test_keyword_matches_without_the_literal_subject_are_not_shown_as_mentions_and_say_so():
    s = _sig(1, "Hamza is great", "Hamza helped me with Ali's project")           # both words, not the name
    out = _check("person_mentions", "Hamza Ali", [s], [_post_entry(s)], "Find Reddit posts mentioning Hamza Ali.")
    assert out["format"] == "no_results" and out["no_results_detail"] == "none_relevant" and out["request_kind"] == "person_mentions"
    assert "1 post was retrieved, but none contains “Hamza Ali”" in out["message"]
    assert "demand" not in json.dumps(out).lower()


def test_lead_discovery_keeps_its_standards_and_wording():
    s = _sig(1, "Our Reddit ads budget", "We spent $500 per month on Reddit ads for our SaaS and it flopped.")
    entry = [{"index": 1, "link": s["post_url"], "role": "buyer", "purchase_intent": "explicit", "title": s["title"]}]
    q = "Find SaaS founders looking for a Reddit monitoring tool"
    plain = ai.enforce_answer_integrity(_answer(entry), [s], [s], q)
    with ai.request_kind_context(None, None, 1):
        ctx = ai.enforce_answer_integrity(_answer(entry), [s], [s], q)
    with ai.request_kind_context("lead_discovery", None, 1):
        ctx2 = ai.enforce_answer_integrity(_answer(entry), [s], [s], q)
    assert plain == ctx == ctx2                                                      # no kind / lead kind: byte-for-byte the same result
    p = _rows(plain)[0]
    assert p["intent"] == "Low" and not p["qualified"] and "qualified lead" in plain["verified_summary"]
    # and an unrelated post is still dropped by the lead rules with the original wording
    off = _sig(2, "My cat", "pictures of my cat", sub="pets")
    gone = ai.enforce_answer_integrity(_answer([{"index": 1, "link": off["post_url"], "title": off["title"], "role": "irrelevant"}]),
                                       [off], [off], q)
    assert gone["format"] == "no_results" and "relevant to the request with supporting evidence" in gone["message"]
    assert gone["no_results_detail"] == "none_relevant" and "request_kind" not in gone


def test_research_keeps_topical_relevance_but_never_rates_a_post_as_a_lead():
    on = _sig(1, "Abandoned carts killing my Shopify store", "Most of my Shopify carts are abandoned and recovery emails barely help.")
    off = _sig(2, "My cat", "pictures of my cat", sub="pets")
    posts = [_post_entry(on, index=1, role="buyer", purchase_intent="explicit", product_fit="fits"),
             _post_entry(off, index=2, role="irrelevant")]
    out = _check("research", None, [on, off], posts, "Research complaints about Shopify abandoned carts.")
    rows = _rows(out)
    assert [r["link"] for r in rows] == [on["post_url"]]                             # the off-topic post is still dropped
    assert rows[0]["intent"] == "" and rows[0]["role"] == "discussion" and not rows[0]["qualified"]
    assert out["counts"]["qualified"] == 0 and "qualified lead" not in out["verified_summary"]


def test_unverifiable_rows_are_still_labelled_as_before_in_every_kind():
    s = _sig(1, "Acme", "Acme text", **{"post_status": "removed"})
    s2 = dict(s)
    for kind, subject in (("mention_search", "Acme"), (None, None)):
        with ai.request_kind_context(kind, subject, 1):
            c = ai.classify_post(s2, {}, set(), False, None)
        assert c["verification_status"] == ai.verification_status(s2)


# ── 4. empty states: what actually happened, never "no demand" for a request that was not about demand ───────────────────────
def _no_results():
    return json.dumps({"format": "no_results", "message": "model text", "likely_reason": "model reason", "suggested_actions": []})


@pytest.mark.parametrize("kind,subject,retrieved,headline,detail", [
    ("mention_search", "Acme", 0, "No posts found mentioning “Acme”", "none_retrieved"),
    ("person_mentions", "Hamza Ali", 0, "No posts found mentioning “Hamza Ali”", "none_retrieved"),
    ("mention_search", "Acme", 3, "Posts retrieved, none mention “Acme”", "none_relevant"),
    ("research", None, 0, "No relevant discussion found", "none_retrieved"),
    ("research", None, 4, "Posts retrieved, none relevant to this topic", "none_relevant"),
    (None, None, 0, "No qualifying posts found", "none_retrieved"),
    (None, None, 5, "No qualifying posts found", "none_relevant"),
])
def test_empty_state_names_the_real_outcome_for_each_kind(env, kind, subject, retrieved, headline, detail):
    lg, _, _ = env
    with ai.request_kind_context(kind, subject, retrieved):
        out = json.loads(lg.enrich_no_results_answer(_no_results(), "q", "reddit"))
    assert out["headline"] == headline and out["no_results_detail"] == detail
    assert ("request_kind" in out) == bool(kind)
    if kind in ("mention_search", "person_mentions"):
        assert "suggested_search" not in out                                          # no reworded search for an exact name
        assert any("spelling" in s for s in out["next_steps"])
        assert not any("need" in s.lower() for s in out["next_steps"])
    if detail == "none_retrieved" and kind:
        assert "Flintel only searches the posts it has collected" in out["likely_reason"]


def test_lead_empty_state_is_unchanged_without_a_context(env):
    lg, _, _ = env
    out = json.loads(lg.enrich_no_results_answer(_no_results(), "find buyers for my crm", "reddit"))
    assert out["headline"] == "No verified purchase intent found" and "no_results_detail" not in out and "request_kind" not in out
    assert "Try broader or different wording for the same need." in out["next_steps"]


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_the_demand_note_is_for_lead_requests_only(name):
    html = (ROOT / "templates" / name).read_text(encoding="utf-8")
    line = next(l for l in html.splitlines() if "reason === 'empty'" in l)
    assert "request_kind" in line and "lead_discovery" in line
    assert "not that the demand doesn't exist" in html


# ── 5. end to end through the real storage + answer path (router scripted) ───────────────────────────────────────────────────
class KindSession(Session):
    """Session whose model reply is scripted per test and whose retrieved posts are given."""
    model_reply = None

    def _fake_analysis(self, query, pool, extra_context=None):
        self.analysis_calls.append({"query": query, "extra": extra_context or "", "pool": list(pool)})
        return json.dumps(self.model_reply(pool)) if callable(self.model_reply) else json.dumps(self.model_reply)

    def run(self, message, router_reply, matched):
        self.script.append(router_reply)
        summary = self.chats.find_one({"chat_id": "c1", "owner_key": "o1"}).get("summary") or ""
        routed = self.lg.classify_and_maybe_chat(message, summary)
        assert routed["intent"] == "search"
        extra = {k: routed[k] for k in ("request_kind", "subject") if k in routed}
        self.idx.add_search_to_chat("c1", "o1", message, self.lg.normalize_topic_key(message), routed["keywords"], "all",
                                    match_phrases=routed["match_phrases"], evidence_required=routed["evidence_required"],
                                    resolved_query=routed.get("resolved_query"), **extra)
        msg = self.chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"][-1]
        self.idx._complete_message_answer_and_results("c1", "o1", msg, matched)
        return routed, msg


@pytest.fixture
def ks(env, monkeypatch):
    lg, idx, chats = env
    s = KindSession(lg, idx, chats, monkeypatch)
    monkeypatch.setattr(idx, "append_to_chat_summary", lambda *a, **k: None)
    return s


def _pool(*pairs):
    return [{"title": t, "post_text": x, "post_url": f"https://www.reddit.com/r/x/comments/{i}/t/", "platform": "reddit",
             "subreddit": "x"} for i, (t, x) in enumerate(pairs, 1)]


def _listing(pool):
    return {"format": "source_list", "headline": "Posts", "executive_summary": f"Listed {len(pool)}.", "key_findings": [],
            "platforms": [{"platform": "reddit", "total_analyzed": len(pool), "shown_count": len(pool), "posts": [
                {"index": i, "link": p["post_url"], "title": p["title"], "summary": "mentions it", "role": "discussion",
                 "purchase_intent": "none", "product_fit": "partial", "sentiment": "neutral"} for i, p in enumerate(pool, 1)]}]}


def test_e2e_person_mentions_are_stored_noted_and_listed_without_lead_labels(ks):
    ks.model_reply = _listing
    pool = _pool(("Met Hamza Ali", "Great talk by Hamza Ali."), ("Meetup", "hamza ali was there too"))
    routed, msg = ks.run("Find Reddit posts mentioning Hamza Ali.", _router("person_mentions", "Hamza Ali", ["x"]), pool)
    assert msg["request_kind"] == "person_mentions" and msg["subject"] == "Hamza Ali" and msg["keywords"] == ["Hamza Ali"]
    note = ks.analysis_calls[-1]["extra"]
    assert "never say who the person is" in note and "These are not leads" in note and '"Hamza Ali"' in note
    ans = json.loads(msg["claude_answer"])
    assert ans["format"] == "source_list" and ans["counts"]["shown"] == 2 and ans["counts"]["qualified"] == 0
    assert all(p["intent"] == "" and not p["qualified"] for p in _rows(ans))


def test_e2e_brand_mention_and_lead_request_take_different_paths(ks):
    ks.model_reply = _listing
    pool = _pool(("Acme review", "We use Acme every day."))
    _, m1 = ks.run("Find Reddit posts mentioning Acme.", _router("mention_search", "Acme", ["Acme"]), pool)
    assert m1["request_kind"] == "mention_search" and "REQUEST KIND: mention search" in ks.analysis_calls[-1]["extra"]
    assert _rows(json.loads(m1["claude_answer"]))[0]["intent"] == ""
    # a lead request about the very same post: no kind stored, no note, lead labels
    pool2 = _pool(("Need a tool", "Need an AI customer support tool for my SaaS, budget $200/month, want a trial."))
    _, m2 = ks.run("Find founders looking for an AI customer-support tool.", _router(None, None, ["ai customer support"]), pool2)
    assert "request_kind" not in m2 and "subject" not in m2 and "REQUEST KIND" not in ks.analysis_calls[-1]["extra"]
    row = _rows(json.loads(m2["claude_answer"]))[0]
    assert row["intent"] in ("High", "Medium", "Low", "Uncertain")


def test_e2e_research_summary_has_a_note_and_no_lead_labels(ks):
    ks.model_reply = _listing
    pool = _pool(("Shopify abandoned carts", "My Shopify abandoned cart emails never convert."),)
    _, msg = ks.run("Research complaints about Shopify abandoned carts.",
                    _router("research", None, ["shopify abandoned cart complaints"]), pool)
    assert msg["request_kind"] == "research" and "REQUEST KIND: research" in ks.analysis_calls[-1]["extra"]
    ans = json.loads(msg["claude_answer"])
    assert ans["counts"]["qualified"] == 0 and all(p["intent"] == "" for p in _rows(ans))


def test_e2e_true_zero_retrieval_says_nothing_was_retrieved(ks):
    ks.model_reply = {"format": "no_results", "message": "m", "likely_reason": "r", "suggested_actions": []}
    _, msg = ks.run("Find Reddit posts mentioning Acme.", _router("mention_search", "Acme", ["Acme"]), [])
    ans = json.loads(msg["claude_answer"])
    assert ans["no_results_detail"] == "none_retrieved" and ans["headline"] == "No posts found mentioning “Acme”"
    _, lead = ks.run("Find founders looking for an AI tool.", _router(None, None, ["ai tool"]), [])
    la = json.loads(lead["claude_answer"])
    assert la["no_results_detail"] == "none_retrieved" and "request_kind" not in la and la["headline"] != ans["headline"]


def test_e2e_retrieved_but_not_relevant_is_not_called_zero_retrieval(ks):
    ks.model_reply = _listing
    pool = _pool(("Hamza is great", "Hamza helped with Ali's project"))
    _, msg = ks.run("Find Reddit posts mentioning Hamza Ali.", _router("person_mentions", "Hamza Ali", ["Hamza Ali"]), pool)
    ans = json.loads(msg["claude_answer"])
    assert ans["format"] == "no_results" and ans["no_results_detail"] == "none_relevant"
    assert ans["headline"].startswith("Posts retrieved, none mention")


def test_a_message_without_a_kind_is_stored_exactly_as_before(ks):
    ks.model_reply = _listing
    _, msg = ks.run("Find founders looking for a CRM.", _router(None, None, ["crm"]), _pool(("Need CRM", "Need a CRM asap")))
    assert "request_kind" not in msg and "subject" not in msg


# ---- deterministic person guard (before any paid router call) ----
@pytest.mark.parametrize("q,intent", [
    ("Can you find out, Hamza Ali name person on Reddit?", "clarify"),
    ("find Hamza Ali on Reddit", "clarify"),
    ("build a dossier on Hamza Ali", "blocked"),
    ("track down this person and find their home address", "blocked"),
    ("where does Hamza Ali live", "blocked"),
])
def test_person_guard_routes_before_router(env, monkeypatch, q, intent):
    lg, _, _ = env
    def boom(*a, **k):
        raise AssertionError("router LLM must not be called")
    monkeypatch.setattr(lg, "_call_claude", boom)
    out = lg.classify_and_maybe_chat(q, "")
    assert out["intent"] == intent and out["reply"] and not out["keywords"]
    assert "request_kind" not in out


@pytest.mark.parametrize("q", [
    "who is looking for a CRM",
    "find posts mentioning Hamza Ali",
    "find people looking for CRM software on Reddit",
    "track competitors pricing discussions",
])
def test_person_guard_ignores_normal_requests(env, q):
    lg, _, _ = env
    assert lg._person_guard(q) is None


# ---- STRICT_INTENT_MODE on: the non-lead kinds must behave exactly as with it off ----
@pytest.fixture
def strict_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "STRICT_INTENT_MODE", True, raising=False)
    return True


@pytest.mark.parametrize("body", [
    test_e2e_person_mentions_are_stored_noted_and_listed_without_lead_labels,
    test_e2e_brand_mention_and_lead_request_take_different_paths,
    test_e2e_research_summary_has_a_note_and_no_lead_labels,
    test_e2e_true_zero_retrieval_says_nothing_was_retrieved,
    test_e2e_retrieved_but_not_relevant_is_not_called_zero_retrieval,
], ids=lambda f: f.__name__.replace("test_e2e_", ""))
def test_non_lead_kinds_are_unchanged_when_strict_intent_mode_is_on(ks, strict_on, body):
    assert ks.lg.strict_intent_mode() is True
    body(ks)


# ---- retrieval (no embeddings) -> storage -> note -> post-processing, in one chain ----
def test_person_kind_retrieval_without_embeddings_feeds_storage_and_post_processing(ks, monkeypatch):
    lg = ks.lg
    coll = mongomock.MongoClient()["sig2"].signals
    docs = [_doc(1, "Met Hamza Ali", "Great talk by Hamza Ali."), _doc(2, "Unrelated", "CSS grid question")]
    for d in docs:
        d.pop("embedding")                                                                    # no saved embedding at all
    coll.insert_many(docs)
    monkeypatch.setattr(lg, "signals_collection", coll)
    ks.model_reply = _listing
    matched = lg.matcher_for_kind("person_mentions", "Hamza Ali", lg.get_matched_signals)("tk", ["Hamza Ali"], targeting_platform="all", limit=20)
    assert [p["post_url"].split("/")[-3] for p in matched] == ["1"]
    _, msg = ks.run("Find Reddit posts mentioning Hamza Ali.", _router("person_mentions", "Hamza Ali", ["x"]), matched)
    assert msg["request_kind"] == "person_mentions" and msg["subject"] == "Hamza Ali"
    assert "These are not leads" in ks.analysis_calls[-1]["extra"]
    ans = json.loads(msg["claude_answer"])
    assert ans["counts"]["qualified"] == 0 and all(p["intent"] == "" for p in _rows(ans))


# ══ provider_search: "find me a graphic designer" (the user is the buyer; the answer is people OFFERING the service) ══════════
def test_provider_kind_is_parsed_with_a_subject_and_offer_side_keywords(env):
    lg, _, _ = env
    assert "provider_search" in lg.REQUEST_KINDS and '"provider_search"' in lg.CLAUDE_ROUTER_SYSTEM_PROMPT
    out = lg._parse_router_json(json.dumps(_router("provider_search", "graphic designer", ["graphic designer for hire"])))
    assert out["request_kind"] == "provider_search" and out["subject"] == "graphic designer" and out["match_phrases"] is None
    assert out["keywords"] == ["graphic designer for hire"]
    bare = lg._parse_router_json(json.dumps(dict(_router("provider_search", "graphic designer", None), keywords=None)))
    assert bare["keywords"] and all("graphic designer" in k for k in bare["keywords"])             # never the buyer-style fallback
    nosubject = lg._parse_router_json(json.dumps(_router("provider_search", None, ["x"])))
    assert "request_kind" not in nosubject                                                          # no role named: the unchanged pipeline
    assert "provider_search" not in lg.request_kind_note(None, None)
    note = lg.request_kind_note("provider_search", "graphic designer")
    assert "not leads" in note and "never score purchase intent or sentiment" in note and "ONLY if the post states it" in note


@pytest.mark.parametrize("q", ["Find me Graphic Designer", "Find me a Graphic Designer on Reddit", "find Video Editor", "Search Logo Designer", "Find me Web Developer", "Find me Marketing Agency"])
def test_a_capitalised_role_is_not_mistaken_for_a_person_name(env, q):
    lg, _, _ = env
    assert lg._person_guard(q) is None


def _dated(n, title, text, days_ago):
    return {"title": title, "post_text": text, "post_url": f"https://www.reddit.com/r/x/comments/{n}/t/", "platform": "reddit",
            "subreddit": "x", "created_utc": NOW - timedelta(days=days_ago)}


PROVIDER_POOL = [
    _dated(1, "[For Hire] Freelance Graphic Designer - Branding, Logos", "Logo packages from $520. Limited slots open, 3 day turnaround. "
           "Portfolio: https://www.behance.net/someone and https://www.reddit.com/r/x/comments/9/t/. DM me.", 9),
    _dated(2, "[FOR HIRE] Professional Logo & Graphic Designer", "Taking commissions this week.", 2),
    _dated(3, "[Hiring] Need a graphic designer for my startup", "Looking for a graphic designer for a logo. Budget $300.", 1),
    _dated(4, "Looking for a graphic designer to redo our packaging", "Can anyone recommend a good graphic designer?", 3),
    _dated(5, "[For Hire] Python developer available", "Backend work, APIs. Open for new projects.", 4),
    _dated(6, "Graphic design is a hard career", "Discussion about the industry in general.", 5),
]


def _mismatched(pool):                     # the model's rows do not match the stored titles/numbers: the identity check must fail
    return {"format": "source_list", "headline": "Designers", "executive_summary": "Offers found.", "key_findings": [],
            "platforms": [{"platform": "reddit", "total_analyzed": len(pool), "shown_count": len(pool), "posts": [
                {"index": 99, "link": p["post_url"], "title": p["title"], "summary": "Great portfolio and 5 star reviews",
                 "role": "buyer", "purchase_intent": "explicit", "product_fit": "fits", "sentiment": "positive"} for p in pool]}]}


def test_e2e_provider_search_keeps_only_offers_and_shows_offer_details_not_lead_labels(ks):
    ks.model_reply = _mismatched
    routed, msg = ks.run("Find me a graphic designer on Reddit.", _router("provider_search", "graphic designer", ["graphic designer for hire"]), PROVIDER_POOL)
    assert msg["request_kind"] == "provider_search" and msg["subject"] == "graphic designer"
    assert "REQUEST KIND: provider search" in ks.analysis_calls[-1]["extra"]
    ans = json.loads(msg["claude_answer"])
    rows = _rows(ans)
    assert [r["title"] for r in rows] == ["[FOR HIRE] Professional Logo & Graphic Designer",           # newest first
                                          "[For Hire] Freelance Graphic Designer - Branding, Logos"]
    assert ans["counts"]["offers"] == 2 and ans["counts"]["qualified"] == 0 and ans["counts"]["excluded"] == 4
    for r in rows:
        assert r["intent"] == "" and not r["qualified"] and not r.get("sentiment") and r["purchase_intent"] == "none"
        assert r["summary"] != ai.SUMMARY_UNAVAILABLE and "5 star" not in r["summary"]                 # the model's unmatched text is never shown
        assert r["offer"]["how_to_reach"]
    first, second = rows
    assert second["offer"]["price"] == ["from $520"] and "Limited slots" in " ".join(second["offer"]["availability"]).capitalize() \
        and "3 day turnaround" in second["offer"]["availability"]
    assert second["offer"]["links"] == ["https://www.behance.net/someone"]                             # the reddit.com link is not a portfolio
    assert first["offer"]["price"] == [] and first["offer"]["links"] == []                             # nothing is invented
    assert "Taking commissions" in first["summary"] or first["summary"].startswith("The offer is in the post title")


def test_provider_rows_use_a_matched_model_summary_only_when_it_is_about_the_post(ks):
    def good(pool):
        return {"format": "source_list", "headline": "Designers", "executive_summary": "Offers.", "key_findings": [],
                "platforms": [{"platform": "reddit", "total_analyzed": 1, "shown_count": 1, "posts": [
                    {"index": 1, "link": pool[0]["post_url"], "title": pool[0]["title"],
                     "summary": "Offers logo packages from $520 with limited slots.", "role": "buyer", "sentiment": "positive"}]}]}
    ks.model_reply = good
    _, msg = ks.run("Find me a graphic designer.", _router("provider_search", "graphic designer", ["graphic designer for hire"]), PROVIDER_POOL[:1])
    row = _rows(json.loads(msg["claude_answer"]))[0]
    assert row["summary"] == "Offers logo packages from $520 with limited slots." and row["role"] == "discussion"
    assert row["intent"] == "" and not row.get("sentiment")                                           # the model's lead labels never survive


def test_a_lead_request_about_the_same_posts_is_unchanged(ks):
    ks.model_reply = _mismatched
    _, msg = ks.run("Find founders who need a graphic designer.", _router(None, None, ["graphic designer"]), PROVIDER_POOL)
    assert "request_kind" not in msg
    assert all("offer" not in r for r in _rows(json.loads(msg["claude_answer"])))


def test_provider_empty_states_are_about_offers_not_demand(env):
    lg, _, _ = env
    for retrieved, detail, head in ((0, "none_retrieved", "No one offering \u201cgraphic designer\u201d found"),
                                    (4, "none_relevant", "Posts retrieved, none offering \u201cgraphic designer\u201d")):
        with ai.request_kind_context("provider_search", "graphic designer", retrieved):
            out = json.loads(lg.enrich_no_results_answer(_no_results(), "Find me a graphic designer", "reddit"))
        assert out["headline"] == head and out["request_kind"] == "provider_search" and out["no_results_detail"] == detail
        low = json.dumps(out).lower()
        assert "demand" not in low and "purchase intent" not in low


def test_a_listed_post_newer_than_the_newest_stored_record_is_reported_beside_it_not_over_it():
    row = {"date_status": "dated", "published_at": "2026-10-21T00:00:00+00:00"}
    scope = {"newest_stored": {"status": "found", "published_at": "2026-10-03T00:00:00+00:00", "scope": "reddit"}}
    out = ai._time_scope_summary([row], None, scope)
    assert out["newest_stored"]["published_at"].startswith("2026-10-03")                   # the server's record is untouched
    assert out["newest_listed"].startswith("2026-10-21")                                   # and the UI is told about the newer listed post
    older = {"date_status": "dated", "published_at": "2026-09-01T00:00:00+00:00"}
    assert "newest_listed" not in ai._time_scope_summary([older], None, scope)
    assert "newest_stored" not in ai._time_scope_summary([row], None, None)                # no record from the server: still no line


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_the_ui_never_states_a_newest_stored_date_older_than_a_listed_post(name):
    html = (ROOT / "templates" / name).read_text(encoding="utf-8")
    assert "scope.newest_listed" in html and "unavailable' && !listed" in html


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_the_page_shows_an_offer_column_and_no_lead_columns_for_provider_rows(integrity_browser, name):
    from tests.test_gate2_pilot_safety import _open_any
    sigs = [dict(p, platform="reddit") for p in PROVIDER_POOL]
    with ai.request_kind_context("provider_search", "graphic designer", len(sigs)):
        data = ai.enforce_answer_integrity(_mismatched(sigs), sigs, sigs, "Find me a graphic designer",
                                           time_scope={"newest_stored": {"status": "found", "published_at": (NOW - timedelta(days=30)).isoformat(), "scope": "reddit"}})
    page, ctx, errors = _open_any(integrity_browser, name, data)
    try:
        heads = [h.strip() for h in page.locator(".answer-rendered table.ap-sources-table th").all_inner_texts()]
        assert "Offer" in heads and "Intent" not in heads and "Sentiment" not in heads and "Posted" in heads
        cell = page.inner_text(".answer-rendered table.ap-sources-table tbody tr:nth-child(2)")
        assert "from $520" in cell and "behance.net/someone" in cell and "Reply to the post" in cell
        link = page.locator(".answer-rendered table.ap-sources-table a[href='https://www.behance.net/someone']")
        assert link.count() == 1 and "noopener" in link.first.get_attribute("rel")
        assert "Summary unavailable" not in page.inner_text(".answer-rendered")
        newest = page.inner_text(".answer-rendered .ap-newest-stored")
        d = NOW - timedelta(days=2)
        assert f"{d.strftime('%b')} {d.day}, {d.year} UTC" in newest                      # the newest LISTED post, never the older record's date
        assert errors == []
    finally:
        ctx.close()
