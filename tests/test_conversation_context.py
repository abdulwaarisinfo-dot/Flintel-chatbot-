"""
tests/test_conversation_context.py — multi-turn follow-ups in one chat.

What is verified here (no real LLM / Mongo / network): every turn's router
call is a scripted fake that returns what a correct router returns for that
turn; the REAL code around it is exercised end to end:
  * the router is given the full relevant history on every turn,
  * its standalone "resolved_query" is parsed, stored on the message, put
    into the rolling summary, and handed to the answer step,
  * the evidence cache never serves another search's posts for a repeated
    short follow-up such as "Now in USA".
Whether the live model itself follows the router rules is NOT verified here.
"""
import copy
import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeChats:
    """Just enough of a Mongo collection for the chat doc."""

    def __init__(self):
        self.docs = {}

    def find_one(self, flt, projection=None):
        doc = self.docs.get((flt.get("chat_id"), flt.get("owner_key")))
        return copy.deepcopy(doc) if doc else None

    def update_one(self, flt, update, upsert=False):
        key = (flt.get("chat_id"), flt.get("owner_key"))
        doc = self.docs.setdefault(key, {"chat_id": key[0], "owner_key": key[1], "messages": []})
        for k, v in (update.get("$set") or {}).items():
            doc[k] = copy.deepcopy(v)
        for k, v in (update.get("$push") or {}).items():
            doc.setdefault(k, []).append(copy.deepcopy(v))


@pytest.fixture
def env(monkeypatch):
    """Real logics + index (+ routes) imported against stub external modules;
    sys.modules is restored afterwards."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-conv")
    db = types.ModuleType("database")
    db.__getattr__ = lambda name: MagicMock(name=f"database.{name}")
    fi = types.ModuleType("flintel")
    fi.ROUTER_UNFILTERED_ADDENDUM = ""
    fi.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM = ""
    fi.build_google_fallback_answer_context = None
    fi.format_google_stub_results = lambda docs: []
    fi.merge_matched_and_google_results = lambda matched, google, max_total=None, **k: list(matched)[:max_total]
    fi.build_mixed_evidence_note = lambda: "MIXED-NOTE"
    fi.build_combined_source_context = lambda a, b: "COMBINED"
    fi.build_unfiltered_answer_context = lambda q, d: "UNFILTERED"
    hx = types.ModuleType("httpx")
    hx.AsyncClient = MagicMock()
    hx.TimeoutException = Exception
    hx.HTTPStatusError = Exception
    gg = types.ModuleType("google")
    gg.get_stub_results_for_keywords = lambda coll, kws: []
    plc = types.ModuleType("passlib.context")
    plc.CryptContext = MagicMock()
    aus = types.ModuleType("authlib.integrations.starlette_client")
    aus.OAuth = MagicMock()
    stubs = {"database": db, "flintel": fi, "httpx": hx, "google": gg,
             "website_intelligence": types.ModuleType("website_intelligence"),
             "passlib": types.ModuleType("passlib"), "passlib.context": plc,
             "authlib": types.ModuleType("authlib"),
             "authlib.integrations": types.ModuleType("authlib.integrations"),
             "authlib.integrations.starlette_client": aus,
             "scheduler": types.ModuleType("scheduler")}
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)
    for name in ("logics", "config", "index", "routes"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    lg = importlib.import_module("logics")
    idx = importlib.import_module("index")
    chats = FakeChats()
    monkeypatch.setattr(idx, "chats_collection", chats)
    yield lg, idx, chats
    for name in ("logics", "config", "index", "routes"):
        sys.modules.pop(name, None)


# ── scripted router: what a CORRECT router returns for each turn ─────────────
def _search(resolved, keywords, evidence=None):
    return {"intent": "search", "reply": None, "keywords": keywords, "time_window_days": None,
            "match_phrases": [f"I am {resolved.lower()}"], "evidence_required": evidence,
            "website_only": False, "url_ask_type": "none", "target_entity": "people", "intent_types": None,
            "resolved_query": resolved}


EXAMPLE = [
    ("Find users looking for AI jobs in Pakistan.",
     _search("Find users looking for AI jobs in Pakistan.", ["ai jobs pakistan"])),
    ("Now in USA.",
     _search("Find users looking for AI jobs in the USA.", ["ai jobs usa"])),
    ("Now in China.",
     _search("Find users looking for AI jobs in China.", ["ai jobs china"])),
    ("Find at least 20 posts in China, USA, and Pakistan together.",
     _search("Find at least 20 posts from users looking for AI jobs in China, the USA, and Pakistan.",
             ["ai jobs china", "ai jobs usa", "ai jobs pakistan"], 20)),
    ("Only in Pakistan.",
     _search("Find at least 20 posts from users looking for AI jobs in Pakistan.", ["ai jobs pakistan"], 20)),
    ("Now find users looking to buy a car.",
     _search("Find users looking to buy a car.", ["buy a car", "car buying"])),
]


class Session:
    """Drives one chat through the real router call, real add_search_to_chat,
    real answer path (_complete_message_answer_and_results) and real
    append_to_chat_summary."""

    def __init__(self, lg, idx, chats, monkeypatch):
        self.lg, self.idx, self.chats = lg, idx, chats
        self.router_inputs, self.analysis_calls = [], []
        self.script = []
        monkeypatch.setattr(lg, "_call_claude", self._fake_router)
        monkeypatch.setattr(idx, "analyze_with_claude", self._fake_analysis)
        monkeypatch.setattr(idx, "strict_intent_mode", lambda: False)
        monkeypatch.setattr(idx, "save_claude_answer_to_chat", lambda *a, **k: None)
        monkeypatch.setattr(idx, "save_signal_results_to_chat", lambda *a, **k: None)
        chats.update_one({"chat_id": "c1", "owner_key": "o1"}, {"$set": {"summary": ""}})

    def _fake_router(self, system_prompt, user_message, *a, **k):
        assert system_prompt is self.lg.CLAUDE_ROUTER_SYSTEM_PROMPT
        self.router_inputs.append(user_message)
        return json.dumps(self.script.pop(0))

    def _fake_analysis(self, query, pool, extra_context=None):
        self.analysis_calls.append({"query": query, "extra": extra_context or ""})
        return json.dumps({"format": "source_list", "headline": "H", "executive_summary": f"Answer for: {query}",
                           "key_findings": [], "platforms": []})

    def turn(self, message, router_reply):
        self.script.append(router_reply)
        summary = self.chats.find_one({"chat_id": "c1", "owner_key": "o1"}).get("summary") or ""
        routed = self.lg.classify_and_maybe_chat(message, summary)
        assert routed["intent"] == "search"
        self.idx.add_search_to_chat("c1", "o1", message, self.lg.normalize_topic_key(message),
                                    routed["keywords"], "all", match_phrases=routed["match_phrases"],
                                    evidence_required=routed["evidence_required"],
                                    resolved_query=routed.get("resolved_query"))
        msg = self.chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"][-1]
        matched = [{"title": f"post for {message}", "post_text": "x", "post_url": f"https://r/{len(self.router_inputs)}"}]
        self.idx._complete_message_answer_and_results("c1", "o1", msg, matched)
        return routed, msg


@pytest.fixture
def session(env, monkeypatch):
    lg, idx, chats = env
    return Session(lg, idx, chats, monkeypatch)


# ── 1. the user's 6-turn example ─────────────────────────────────────────────
def test_example_conversation_end_to_end(session):
    msgs = []
    for message, reply in EXAMPLE:
        routed, msg = session.turn(message, reply)
        msgs.append(msg)

    # every follow-up's router call sees EVERY earlier turn, with its meaning
    last_router_input = session.router_inputs[-1]
    for message, reply in EXAMPLE[:-1]:
        assert message[:20] in last_router_input
    assert "[meaning: Find users looking for AI jobs in the USA.]" in last_router_input
    assert "[meaning: Find at least 20 posts from users looking for AI jobs in Pakistan.]" in last_router_input

    # the resolved request is stored on each message
    assert [m["resolved_query"] for m in msgs] == [r["resolved_query"] for _, r in EXAMPLE]

    # answer step: follow-ups get the full current request; standalone turns get no extra note
    notes = [c["extra"] for c in session.analysis_calls]
    assert "Current request" not in notes[0]                                    # turn 1 standalone
    assert "Current request" in notes[1] and "AI jobs in the USA" in notes[1]   # "Now in USA."
    assert "AI jobs in China" in notes[2]
    assert "China, the USA, and Pakistan" in notes[3] and "at least 20" in notes[3]
    assert "at least 20 posts from users looking for AI jobs in Pakistan" in notes[4]  # count kept, only PK
    car_note = [part for part in notes[5].split("\n\n") if part.startswith("Current request")]
    assert car_note and car_note[0].endswith("Find users looking to buy a car.")
    assert "Pakistan" not in car_note[0] and "20" not in car_note[0]          # new search: nothing carried
    assert session.analysis_calls[4]["query"] == "Only in Pakistan."            # literal message unchanged

    # count flows into the evidence budget of the refined turns
    assert msgs[3]["evidence_required"] == 20 and msgs[4]["evidence_required"] == 20
    assert msgs[5]["evidence_required"] is None



def test_router_is_told_how_to_handle_follow_ups(env):
    lg, _, _ = env
    p = lg.CLAUDE_ROUTER_SYSTEM_PROMPT
    assert "FOLLOW-UP MESSAGES — ONE CHAT, MANY TURNS" in p
    assert "REPLACES the previous location" in p and "union of the locations" in p
    assert "STARTS A NEW SEARCH" in p and "do NOT carry over earlier locations" in p
    assert "\"resolved_query\"" in p and "must be at least N" in p
    assert "never \"clarify\" or \"chat\" when the history has a" in p


# ── 2. long refinement chains don't lose the subject ─────────────────────────
def test_subject_and_count_survive_more_follow_ups_than_the_summary_keeps(session, env):
    lg, idx, _ = env
    first = "Find at least 20 posts from users looking for AI jobs in Pakistan."
    session.turn(first, _search(first, ["ai jobs pakistan"], 20))
    places = ["USA", "China", "UK", "Germany", "India", "Canada", "France", "Japan", "Brazil", "Spain"]
    assert len(places) > idx.CHAT_SUMMARY_MAX_TURNS
    for place in places:
        session.turn(f"Now in {place}.",
                     _search(f"Find at least 20 posts from users looking for AI jobs in {place}.",
                             [f"ai jobs {place}"], 20))
    session.turn("Now in Italy.", _search("Find at least 20 posts from users looking for AI jobs in Italy.",
                                          ["ai jobs italy"], 20))
    router_input = session.router_inputs[-1]
    assert first not in router_input                        # the original line has rolled off...
    # ...but the subject AND the count are still on the surviving lines
    assert "[meaning: Find at least 20 posts from users looking for AI jobs in Spain.]" in router_input


# ── 3. other realistic follow-up patterns (plumbing) ─────────────────────────
@pytest.mark.parametrize("message,resolved", [
    ("10 more", "Find 10 more posts from users looking for AI jobs in Pakistan."),
    ("aur Lahore bhi", "Find users looking for AI jobs in Pakistan and Lahore."),
    ("same but last week only", "Find users looking for AI jobs in Pakistan from the last week."),
    ("remove the remote filter", "Find users looking for AI jobs in Pakistan, any work arrangement."),
    ("only buyers, exclude agencies", "Find buyers (not agencies) looking for AI developers in Pakistan."),
])
def test_follow_up_patterns_reach_answer_and_summary(session, message, resolved):
    session.turn(*EXAMPLE[0])
    session.turn(message, _search(resolved, ["kw"]))
    assert resolved in session.analysis_calls[-1]["extra"]
    summary = session.chats.find_one({"chat_id": "c1", "owner_key": "o1"})["summary"]
    assert f"User: {message} [meaning: {resolved}]" in summary


# ── 4. unchanged behaviour for ordinary / old-style turns ────────────────────
def test_standalone_turn_summary_and_context_unchanged(session, env):
    lg, idx, chats = env
    session.turn("Find users looking to buy a car.", _search("Find users looking to buy a car", ["car"]))
    summary = chats.find_one({"chat_id": "c1", "owner_key": "o1"})["summary"]
    assert summary.startswith("User: Find users looking to buy a car. | Assistant:")
    assert "[meaning:" not in summary
    assert "Current request" not in session.analysis_calls[-1]["extra"]


def test_old_router_json_without_resolved_query(env):
    lg, _, _ = env
    raw = json.dumps({k: v for k, v in _search("x", ["kw"]).items() if k != "resolved_query"})
    assert lg._parse_router_json(raw)["resolved_query"] is None
    chat = json.dumps({"intent": "chat", "reply": "hi", "keywords": None, "time_window_days": None,
                       "match_phrases": None, "evidence_required": None, "resolved_query": "ignored"})
    assert lg._parse_router_json(chat)["resolved_query"] is None
    parsed = lg._parse_router_json(json.dumps(_search("  Find   AI jobs\n in USA ", ["kw"])))
    assert parsed["resolved_query"] == "Find AI jobs in USA"


def test_append_to_chat_summary_default_line_identical(env):
    lg, idx, chats = env
    idx.append_to_chat_summary("c2", "o2", "Nike shoes", "plain answer", keywords=["nike"])
    idx.append_to_chat_summary("c2", "o2", "Nike shoes", "plain answer", keywords=["nike"],
                               resolved_query="nike shoes")          # same request -> no meaning tag
    lines = chats.find_one({"chat_id": "c2", "owner_key": "o2"})["summary"].split("\n")
    assert lines == ["User: Nike shoes | Assistant: plain answer [searched: nike]"] * 2


def test_resolved_request_note(env):
    lg, _, _ = env
    assert lg._resolved_request_note("Now in USA.", None) is None
    assert lg._resolved_request_note("Now in USA.", "   ") is None
    assert lg._resolved_request_note("Find AI jobs in USA", "find ai jobs in usa!") is None
    note = lg._resolved_request_note("Now in USA.", "Find users looking for AI jobs in the USA.")
    assert note.endswith("Find users looking for AI jobs in the USA.")


# ── 5. evidence cache: same raw text, different search ───────────────────────
class FakeCache:
    def __init__(self, doc=None):
        self.doc = doc
        self.saved = None

    def find_one(self, flt, projection=None):
        return copy.deepcopy(self.doc)

    def update_one(self, flt, update, upsert=False):
        self.saved = update.get("$set")


def _cache_env(lg, monkeypatch, doc):
    cache = FakeCache(doc)
    monkeypatch.setattr(lg, "topic_evidence_cache_collection", cache)
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: False)
    calls = []

    def matcher(topic_key, keywords, **kw):
        calls.append(list(keywords))
        return [{"post_url": "https://r/car1", "title": "need a car"}]
    return cache, calls, matcher


AI_POSTS = [{"post_url": f"https://r/ai{i}", "title": "AI job"} for i in range(30)]


def test_repeated_follow_up_text_never_serves_previous_searchs_posts(env, monkeypatch):
    lg, _, _ = env
    doc = {"chat_id": "c1", "topic_key": "now in usa", "posts": AI_POSTS,
           "keywords": ["ai jobs usa"], "match_phrases": ["I want an AI job in the USA"]}
    cache, calls, matcher = _cache_env(lg, monkeypatch, doc)
    out = lg.get_evidence_with_topup("c1", "o1", "now in usa", ["car buying usa"], 20, matcher,
                                     match_phrases=["I want to buy a car in the USA"])
    assert calls == [["car buying usa"]]                     # fresh scan for the new search
    assert [p["post_url"] for p in out] == ["https://r/car1"]  # no AI-job posts leaked in
    assert cache.saved["keywords"] == ["car buying usa"]


def test_same_search_still_uses_cache(env, monkeypatch):
    lg, _, _ = env
    doc = {"chat_id": "c1", "topic_key": "now in usa", "posts": AI_POSTS,
           "keywords": ["ai jobs usa", "ml jobs usa"], "match_phrases": ["I want an AI job in the USA"]}
    _, calls, matcher = _cache_env(lg, monkeypatch, doc)
    out = lg.get_evidence_with_topup("c1", "o1", "now in usa", ["ML jobs USA", "ai jobs usa"], 20, matcher,
                                     match_phrases=["I want an AI job in the USA"])
    assert calls == [] and out == AI_POSTS                   # order/case-insensitive: cache hit as before


# ── 6. wiring in the other two answer paths ──────────────────────────────────
def test_stream_and_timeout_paths_pass_the_resolved_request():
    routes = (ROOT / "routes.py").read_text(encoding="utf-8")
    assert 'resolved_query=routed.get("resolved_query") if intent == "search" else None' in routes
    assert '_resolved_request_note(msg.get("query"), msg.get("resolved_query"))' in routes
    assert 'resolved_query=msg.get("resolved_query"))' in routes
    index = (ROOT / "index.py").read_text(encoding="utf-8")
    assert index.count('msg.get("website_note"), msg.get("resolved_query"),') == 2


def test_timeout_fallback_uses_resolved_request(env, monkeypatch):
    lg, idx, chats = env
    seen = {}
    monkeypatch.setattr(lg, "get_evidence_with_topup", lambda *a, **k: [{"post_url": "https://r/1", "title": "t"}])
    monkeypatch.setattr(lg, "analyze_with_claude",
                        lambda q, pool, extra_context=None: seen.setdefault("extra", extra_context) and "{}")
    monkeypatch.setattr(idx, "save_claude_answer_to_chat", lambda *a, **k: None)
    monkeypatch.setattr(idx, "save_signal_results_to_chat", lambda *a, **k: None)
    monkeypatch.setattr(idx, "_clear_owner_busy", lambda *a, **k: None)
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: False)
    lg._timeout_fallback_answer("c3", "o3", "now in usa", "Now in USA.", ["ai jobs usa"], None, 20, None,
                                "Find users looking for AI jobs in the USA.")
    assert "Find users looking for AI jobs in the USA." in seen["extra"]
    assert "[meaning: Find users looking for AI jobs in the USA.]" in chats.find_one(
        {"chat_id": "c3", "owner_key": "o3"})["summary"]


# ── 7. router retry + safe failure ───────────────────────────────────────────
GOOD_ROUTER_JSON = json.dumps(_search("Find users looking for AI jobs in the USA.", ["ai jobs usa"]))
HISTORY = "User: Find users looking for AI jobs in Pakistan. | Assistant: ... [searched: ai jobs pakistan]"
OLD_FALLBACK = {"intent": "search", "reply": None, "keywords": None, "time_window_days": None, "unfiltered": None,
                "match_phrases": None, "evidence_required": None, "website_only": False,
                "use_website_context": False, "website_topic_relation": None, "url_ask_type": None}


def _router_seq(lg, monkeypatch, outcomes):
    """Each outcome: an Exception instance (raised) or a string (returned)."""
    calls = []

    def fake(system_prompt, user_message, *a, **k):
        calls.append(user_message)
        out = outcomes[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(lg, "_call_claude", fake)
    return calls


def test_router_success_first_try_makes_one_call(env, monkeypatch):
    lg, _, _ = env
    calls = _router_seq(lg, monkeypatch, [GOOD_ROUTER_JSON])
    routed = lg.classify_and_maybe_chat("Now in USA.", HISTORY)
    assert len(calls) == 1 and routed["intent"] == "search" and routed["keywords"] == ["ai jobs usa"]
    assert not routed.get("router_failed")


@pytest.mark.parametrize("first", [RuntimeError("timeout"), "not json at all", '{"intent": "nonsense"}'])
def test_router_retries_once_then_succeeds(env, monkeypatch, first):
    lg, _, _ = env
    calls = _router_seq(lg, monkeypatch, [first, GOOD_ROUTER_JSON])
    routed = lg.classify_and_maybe_chat("Now in USA.", HISTORY)
    assert len(calls) == 2 and calls[0] == calls[1]                 # same input re-sent
    assert routed["intent"] == "search" and routed["resolved_query"] == "Find users looking for AI jobs in the USA."


def test_router_fails_twice_with_history_runs_no_search(env, monkeypatch):
    lg, _, _ = env
    calls = _router_seq(lg, monkeypatch, [RuntimeError("down"), "garbage"])
    routed = lg.classify_and_maybe_chat("Now in USA.", HISTORY)
    assert len(calls) == 2                                            # exactly one retry, no more
    assert routed["intent"] == "chat" and routed["reply"] == lg.ROUTER_UNAVAILABLE_REPLY
    assert routed["keywords"] is None and routed["match_phrases"] is None and routed["router_failed"] is True


@pytest.mark.parametrize("summary,query", [
    ("", "Find users looking for AI jobs in Pakistan."),                 # first message: literal text is the request
    (HISTORY, "find leads for https://example.com"),                     # URL: website flows handle it
])
def test_router_fails_twice_old_fallback_kept(env, monkeypatch, summary, query):
    lg, _, _ = env
    _router_seq(lg, monkeypatch, [RuntimeError("down"), RuntimeError("down")])
    assert lg.classify_and_maybe_chat(query, summary) == OLD_FALLBACK


def test_router_prompt_treats_meaning_tags_as_context(env):
    lg, _, _ = env
    p = lg.CLAUDE_ROUTER_SYSTEM_PROMPT
    assert "EARLIER interpretation" in p and "not a confirmed fact" in p
    assert "NEW message) take priority whenever they conflict" in p


# ── 8. the real /search route ────────────────────────────────────────────────
class _Req:
    def __init__(self):
        self.session = {}


def _route_env(env, monkeypatch, chat_doc):
    lg, idx, _ = env
    rt = sys.modules["routes"]
    rec = {"chat_msgs": [], "jobs": [], "searches": [], "summaries": []}
    monkeypatch.setattr(rt, "get_owner", lambda request: ("o1", "guest"))
    monkeypatch.setattr(rt, "_is_owner_busy", lambda k: False)
    monkeypatch.setattr(rt, "_set_owner_busy", lambda k: None)
    monkeypatch.setattr(rt, "_clear_owner_busy", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(rt, "get_chat_session", lambda cid, ok: copy.deepcopy(chat_doc))
    monkeypatch.setattr(rt, "create_chat_session", lambda *a, **k: "c1")
    monkeypatch.setattr(rt, "generate_chat_title", lambda q: "t")
    monkeypatch.setattr(rt, "add_chat_message_to_chat", lambda cid, ok, q, a: rec["chat_msgs"].append((q, a)))
    monkeypatch.setattr(rt, "append_to_chat_summary", lambda *a, **k: rec["summaries"].append(a))
    monkeypatch.setattr(rt, "enqueue_search_job", lambda tk, kws, plat: rec["jobs"].append((tk, list(kws))))
    monkeypatch.setattr(rt, "add_search_to_chat", lambda *a, **k: rec["searches"].append((a, k)))
    monkeypatch.setattr(rt, "save_last_website_context_to_chat", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(rt, "generate_keywords_for_website_request",
                        lambda *a, **k: {"keywords": ["website kw"], "match_phrases": None}, raising=False)
    return lg, rt, rec


WEBSITE_CHAT = {"chat_id": "c1", "owner_key": "o1", "summary": HISTORY, "messages": [{"query": "x"}],
                "last_website_context": {"url": "https://shop.example", "keywords": ["website kw"],
                                         "match_phrases": None, "structured_evidence": {"title": "Shop"}}}


def test_route_router_failed_twice_no_search_no_website_reuse(env, monkeypatch):
    lg, rt, rec = _route_env(env, monkeypatch, WEBSITE_CHAT)
    _router_seq(lg, monkeypatch, [RuntimeError("down"), RuntimeError("down")])
    resp = rt.search(_Req(), query="Now in USA.", platform="All Platforms", chat_id="c1")
    assert resp.status_code == 303
    assert rec["jobs"] == [] and rec["searches"] == []                 # nothing searched, website keywords not reused
    assert rec["chat_msgs"] == [("Now in USA.", lg.ROUTER_UNAVAILABLE_REPLY)]


def test_route_success_stores_resolved_query_and_keeps_job_key(env, monkeypatch):
    chat = {"chat_id": "c1", "owner_key": "o1", "summary": HISTORY, "messages": [{"query": "x"}]}
    lg, rt, rec = _route_env(env, monkeypatch, chat)
    _router_seq(lg, monkeypatch, [GOOD_ROUTER_JSON])
    rt.search(_Req(), query="Now in USA.", platform="All Platforms", chat_id="c1")
    # background-job contract unchanged: still keyed by the literal message's topic_key
    assert rec["jobs"] == [("now in usa", ["ai jobs usa"])]
    args, kwargs = rec["searches"][0]
    assert args[3] == "now in usa"                                   # message topic_key unchanged
    assert kwargs["resolved_query"] == "Find users looking for AI jobs in the USA."
