"""
tests/test_guidance_and_intro.py

  * small talk: bare name introductions ("I am Hamza") are answered by the
    existing fast path (no LLM / router / search), by name; a greeting plus a
    real business question still goes to the router;
  * data-backed guidance: a business-guidance question the router marks
    "guidance" runs the EXISTING research pipeline (search job + stored-post
    matching + grounded analysis) and the analysis receives the guidance
    instructions together with the matched stored posts — also when there are
    no posts (honest limitation); with no known market the router's
    clarifying question is kept (never self-resolved into a guessed search);
  * explicit prospect searches are unchanged (no guidance note).

Real /search route + real index helpers against stub modules and a dict-backed
fake chats collection; the LLM is a recording stub. No real LLM / Mongo /
network — this proves the WIRING, not live model behaviour or real data.
"""
import copy
import json
import sys

import pytest

from tests.test_answer_persistence import env  # noqa: F401  (fixture)

GUIDANCE_Q = "What's the best way to get customers for my dental clinic?"
GUIDANCE_ROUTER = {"intent": "search", "reply": None, "keywords": ["dentist recommendation", "looking for a dentist"],
                   "time_window_days": None, "match_phrases": ["Can anyone recommend a good dentist?"],
                   "evidence_required": None, "website_only": False, "url_ask_type": "none",
                   "target_entity": "people looking for a dentist", "intent_types": ["active_opportunity"],
                   "resolved_query": "What's the best way to get customers for a dental clinic?", "guidance": True}
PROSPECT_ROUTER = dict(GUIDANCE_ROUTER, guidance=False, resolved_query="Find people looking for a dentist.")
STORED = [{"title": "Need a dentist in Lahore", "post_text": "Can anyone recommend a good dentist in DHA? Mine retired.",
           "post_url": "https://www.reddit.com/r/lahore/comments/d1/", "platform": "reddit", "subreddit": "lahore"},
          {"title": "Dental clinic marketing", "post_text": "We run ads for clinics, DM me for rates.",
           "post_url": "https://www.reddit.com/r/marketing/comments/d2/", "platform": "reddit", "subreddit": "marketing"}]


@pytest.fixture
def route(env, monkeypatch):
    lg, idx, chats = env
    rt = sys.modules["routes"]

    def insert_one(doc):
        chats.docs[(doc["chat_id"], doc["owner_key"])] = copy.deepcopy(doc)
    chats.insert_one = insert_one
    state = {"router": None, "calls": [], "jobs": [], "resolve_calls": []}

    def fake_llm(system, user, max_tokens=None, enable_web_search=False, force_json_prefill=False):
        state["calls"].append({"system": system, "user": user})
        if system == lg.CLAUDE_ROUTER_SYSTEM_PROMPT or system.startswith(lg.CLAUDE_ROUTER_SYSTEM_PROMPT[:200]):
            return json.dumps(state["router"])
        return json.dumps({"format": "source_list", "headline": "h", "executive_summary": "From the posts: one "
                           "person is actively asking for a dentist.", "platforms": []})
    monkeypatch.setattr(lg, "_call_claude", fake_llm)
    monkeypatch.setattr(rt, "get_owner", lambda request: ("o1", "anon"))
    monkeypatch.setattr(rt, "_is_owner_busy", lambda k: False)
    monkeypatch.setattr(rt, "_set_owner_busy", lambda k: None)
    monkeypatch.setattr(rt, "generate_chat_title", lambda q: "t")
    monkeypatch.setattr(rt, "enqueue_search_job", lambda tk, kws, plat: state["jobs"].append((tk, kws, plat)))

    def fake_resolve(query, summary):
        state["resolve_calls"].append(query)
        return {"keywords": ["customer acquisition"], "match_phrases": None, "time_window_days": None}
    monkeypatch.setattr(rt, "resolve_unclear_topic", fake_resolve)

    class Req:
        session = {}

    def send(query, chat_id=None):
        resp = rt.search(Req(), query=query, platform="All Platforms", chat_id=chat_id)
        assert resp.status_code == 303
        cid = resp.headers["location"].rsplit("/", 1)[-1]
        return cid, chats.find_one({"chat_id": cid, "owner_key": "o1"})
    return lg, idx, chats, state, send


def _router_calls(lg, state):
    return [c for c in state["calls"] if c["system"] == lg.CLAUDE_ROUTER_SYSTEM_PROMPT]


# ── small talk: introductions ────────────────────────────────────────────────
@pytest.mark.parametrize("text,name", [
    ("I am Hamza", "Hamza"), ("I'm Hamza", "Hamza"), ("Hi, I am Hamza", "Hamza"), ("hello! my name is Hamza Ali", "Hamza Ali"),
    ("this is Sara", "Sara"), ("mera naam Hamza hai", "Hamza"), ("main hamza hoon", "Hamza"), ("I am Hamza here", "Hamza"),
])
def test_introductions_are_acknowledged_by_name(env, text, name):
    lg, _, _ = env
    reply = lg.smalltalk_reply(text)
    assert reply and reply.startswith(f"Nice to meet you, {name}!")


@pytest.mark.parametrize("text", ["I am fine", "I am confused", "I am new here", "I am looking for a CRM",
                                  "I am Hamza from Lahore", "I am a founder", "This is great", "I am interested",
                                  "I am selling shoes", "Hi I'm Hamza, what's the best way to get customers?"])
def test_non_introductions_are_not_fast_pathed(env, text):
    lg, _, _ = env
    assert lg.smalltalk_reply(text) is None


def test_intro_through_the_route_needs_no_llm_and_is_saved(route):
    lg, idx, chats, state, send = route
    cid, doc = send("I am Hamza")
    assert state["calls"] == [] and state["jobs"] == []
    msg = doc["messages"][-1]
    assert msg["message_type"] == "chat" and msg["claude_answer"].startswith("Nice to meet you, Hamza!")
    assert "I am Hamza" in doc["summary"]


@pytest.mark.parametrize("text", ["Hello", "Hi", "How are you?", "What can you do?", "Thanks", "I am Hamza"])
def test_simple_messages_make_no_llm_or_search_calls(route, text):
    lg, idx, chats, state, send = route
    send(text)
    assert state["calls"] == [] and state["jobs"] == []


def test_greeting_plus_business_question_reaches_the_router(route):
    lg, idx, chats, state, send = route
    state["router"] = GUIDANCE_ROUTER
    send("Hi! " + GUIDANCE_Q)
    assert len(_router_calls(lg, state)) == 1 and len(state["jobs"]) == 1


# ── data-backed guidance ─────────────────────────────────────────────────────
def test_router_prompt_and_parser_support_guidance(env):
    lg, _, _ = env
    p = lg.CLAUDE_ROUTER_SYSTEM_PROMPT
    assert "BUSINESS GUIDANCE QUESTIONS" in p and "never from general knowledge" in p
    assert lg._parse_router_json(json.dumps(GUIDANCE_ROUTER))["guidance"] is True
    assert lg._parse_router_json(json.dumps({"intent": "clarify", "reply": "What do you sell?", "keywords": None,
                                             "time_window_days": None, "guidance": True}))["guidance"] is True
    chat = lg._parse_router_json(json.dumps({"intent": "chat", "reply": "x", "keywords": None,
                                             "time_window_days": None, "guidance": True}))
    assert chat["guidance"] is False                                    # only search / clarify carry it
    assert lg._parse_router_json(json.dumps(PROSPECT_ROUTER))["guidance"] is False


def test_guidance_runs_the_existing_pipeline_with_a_guidance_note(route):
    lg, idx, chats, state, send = route
    state["router"] = GUIDANCE_ROUTER
    cid, doc = send(GUIDANCE_Q)
    assert len(state["jobs"]) == 1 and state["jobs"][0][1] == GUIDANCE_ROUTER["keywords"]   # monitoring job
    msg = doc["messages"][-1]
    assert msg["claude_answer"] is None and "BUSINESS GUIDANCE REQUEST" in msg["website_note"]
    assert GUIDANCE_Q in msg["website_note"]


@pytest.mark.parametrize("matched", [STORED, []])
def test_analysis_gets_the_stored_posts_and_the_guidance_rules(route, matched):
    lg, idx, chats, state, send = route
    state["router"] = GUIDANCE_ROUTER
    cid, doc = send(GUIDANCE_Q)
    msg = doc["messages"][-1]
    state["calls"].clear()
    idx._complete_message_answer_and_results(cid, "o1", msg, copy.deepcopy(matched))
    analysis = [c for c in state["calls"] if c["system"] != lg.CLAUDE_ROUTER_SYSTEM_PROMPT]
    assert analysis, "the grounded analysis step ran"
    user = analysis[-1]["user"]
    assert "BUSINESS GUIDANCE REQUEST" in user and "do not fill the gap with general business advice" in user
    if matched:
        assert "Can anyone recommend a good dentist in DHA?" in user           # the stored post itself
        assert "We run ads for clinics" in user                                 # seller kept visible to the rules
    saved = chats.find_one({"chat_id": cid, "owner_key": "o1"})["messages"][-1]
    assert saved["claude_answer"]                                               # persisted


def test_guidance_note_merges_with_an_existing_website_note(route, monkeypatch):
    lg, idx, chats, state, send = route
    rt = sys.modules["routes"]
    state["router"] = GUIDANCE_ROUTER
    real_add = rt.add_search_to_chat
    seen = {}

    def spy(*a, **k):
        seen.update(k)
        return real_add(*a, **k)
    monkeypatch.setattr(rt, "add_search_to_chat", spy)
    monkeypatch.setattr(rt, "_build_website_note", lambda ctx, rel: "WEBSITE NOTE", raising=False)
    cid, _ = send("first message")
    chats.update_one({"chat_id": cid, "owner_key": "o1"},
                     {"$set": {"last_website_context": {"url": "https://clinic.test", "keywords": ["dentist"]}}})
    state["router"] = dict(GUIDANCE_ROUTER, website_topic_relation="related")
    send(GUIDANCE_Q, cid)
    assert seen["website_note"].startswith("WEBSITE NOTE") and "BUSINESS GUIDANCE REQUEST" in seen["website_note"]


def test_unknown_market_is_asked_about_not_guessed(route):
    lg, idx, chats, state, send = route
    state["router"] = {"intent": "clarify", "reply": "What do you sell, and to whom? I answer this from real "
                       "conversations Flintel monitors, not generic advice.", "keywords": None,
                       "time_window_days": None, "guidance": True}
    cid, doc = send("What's the best way to get customers?")
    assert state["resolve_calls"] == [] and state["jobs"] == []          # no guessed topic, no search
    assert "What do you sell" in doc["messages"][-1]["claude_answer"]


def test_normal_clarify_still_self_resolves(route):
    lg, idx, chats, state, send = route
    state["router"] = {"intent": "clarify", "reply": "Which topic?", "keywords": None, "time_window_days": None}
    send("show me today's posts")
    assert state["resolve_calls"] == ["show me today's posts"] and len(state["jobs"]) == 1


def test_explicit_prospect_search_is_unchanged(route):
    lg, idx, chats, state, send = route
    state["router"] = PROSPECT_ROUTER
    cid, doc = send("Find people looking for a dentist in Lahore")
    msg = doc["messages"][-1]
    assert len(state["jobs"]) == 1 and not msg.get("website_note")
