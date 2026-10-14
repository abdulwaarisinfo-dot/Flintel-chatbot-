"""
tests/test_smalltalk_fast_path.py — a message that is ONLY small talk ("hi",
"how are you doing?", "thanks", "what can you do?") is answered without the
router LLM call; every other message (research requests, including ones that
start with "hello") goes through the router exactly as before.

Through the real /search route with stub modules and a dict-backed fake chats
collection; the LLM is a recording stub. No real LLM / Mongo / network.
"""
import copy
import json
import sys

import pytest

from tests.test_answer_persistence import env  # noqa: F401  (fixture)

POSITIVE = [
    ("Hi", "greeting"), ("hi!", "greeting"), ("Hello", "greeting"), ("hey there", "greeting"),
    ("Hello Flintel!", "greeting"), ("Good morning", "greeting"), ("good evening 👋", "greeting"),
    ("How are you doing?", "how_are_you"), ("how are you", "how_are_you"), ("Hi, how are you?", "how_are_you"),
    ("Hey there! How's it going?", "how_are_you"), ("what's up", "how_are_you"), ("How are you doing today?", "how_are_you"),
    ("Thanks", "thanks"), ("thank you so much!", "thanks"), ("Thanks a lot", "thanks"), ("ok thanks", "thanks"),
    ("appreciate it", "thanks"), ("Bye", "goodbye"), ("see you later", "goodbye"),
    ("What can you do?", "capabilities"), ("Hi, what can you do?", "capabilities"), ("who are you", "capabilities"),
    ("How does this work?", "capabilities"), ("help", "capabilities"),
]
NEGATIVE = [
    "Hello, find people looking for a CRM for small teams",
    "hi can you find buyers for my dentist clinic",
    "Thanks, now do the same for the USA",
    "thanks! what about Notion?",
    "How are you guys handling churn?",          # a research-style question
    "what can you do for my SaaS?",
    "help me find leads for my bakery",
    "how are people reviewing the iPhone 16",
    "hello https://example.com",
    "Find users looking for AI jobs in Pakistan",
    "yes", "no", "ok", "okay", "sure",           # may answer a question asked earlier
    "number 3",
    "kia chal raha hai aj kal",                  # non-English small talk: router replies in the user's language
    "salaam",
    "",
    "hi " * 12,
    "hey what's up with Tesla stock complaints",
]


@pytest.fixture
def route(env, monkeypatch):
    lg, idx, chats = env
    rt = sys.modules["routes"]

    def insert_one(doc):
        chats.docs[(doc["chat_id"], doc["owner_key"])] = copy.deepcopy(doc)
    chats.insert_one = insert_one
    calls, jobs = [], []
    router = {"intent": "search", "reply": None, "keywords": ["crm small teams"], "time_window_days": None,
              "match_phrases": ["looking for a CRM"], "evidence_required": None, "website_only": False,
              "url_ask_type": "none", "target_entity": "small teams", "intent_types": None,
              "resolved_query": "Find people looking for a CRM for small teams."}

    def fake_llm(system, user, max_tokens=None, enable_web_search=False, force_json_prefill=False):
        calls.append({"system": system, "user": user, "web": enable_web_search, "max_tokens": max_tokens})
        return json.dumps(router)
    monkeypatch.setattr(lg, "_call_claude", fake_llm)
    monkeypatch.setattr(rt, "get_owner", lambda request: ("o1", "anon"))
    monkeypatch.setattr(rt, "_is_owner_busy", lambda k: False)
    monkeypatch.setattr(rt, "_set_owner_busy", lambda k: None)
    monkeypatch.setattr(rt, "enqueue_search_job", lambda tk, kws, plat: jobs.append((tk, kws, plat)))

    class Req:
        session = {}

    def send(query, chat_id=None):
        resp = rt.search(Req(), query=query, platform="All Platforms", chat_id=chat_id)
        assert resp.status_code == 303
        return resp.headers["location"].rsplit("/", 1)[-1]
    return lg, chats, calls, jobs, send


# ── classifier ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,category", POSITIVE)
def test_smalltalk_detected(env, text, category):
    lg, _, _ = env
    assert lg.smalltalk_reply(text) == lg.SMALLTALK_REPLIES[category]


@pytest.mark.parametrize("text", NEGATIVE)
def test_everything_else_goes_to_router(env, text):
    lg, _, _ = env
    assert lg.smalltalk_reply(text) is None


# ── through the real /search route ───────────────────────────────────────────
@pytest.mark.parametrize("text,category", [("How are you doing?", "how_are_you"), ("Hi", "greeting"),
                                           ("Thanks", "thanks"), ("What can you do?", "capabilities")])
def test_smalltalk_skips_router_and_is_saved(route, text, category):
    lg, chats, calls, jobs, send = route
    chat_id = send(text)
    assert calls == [] and jobs == []                                   # no LLM call, no search job
    doc = chats.find_one({"chat_id": chat_id, "owner_key": "o1"})
    msg = doc["messages"][-1]
    assert msg["message_type"] == "chat" and msg["query"] == text
    assert msg["claude_answer"] == lg.SMALLTALK_REPLIES[category]
    assert text in doc["summary"]                                       # conversation history kept


def test_research_request_still_uses_router_unchanged(route):
    lg, chats, calls, jobs, send = route
    q = "Hello, find people looking for a CRM for small teams"
    chat_id = send(q)
    assert len(calls) == 1
    c = calls[0]
    assert c["system"] == lg.CLAUDE_ROUTER_SYSTEM_PROMPT and c["web"] is True     # same router call as before
    assert c["max_tokens"] == lg.CLAUDE_ROUTER_MAX_TOKENS and q in c["user"]
    assert len(jobs) == 1 and jobs[0][1] == ["crm small teams"]          # research pipeline ran
    msg = chats.find_one({"chat_id": chat_id, "owner_key": "o1"})["messages"][-1]
    assert msg.get("message_type") != "chat" and msg["claude_answer"] is None


def test_mixed_conversation_keeps_history(route):
    lg, chats, calls, jobs, send = route
    chat_id = send("Hi")
    send("Find people looking for a CRM for small teams", chat_id)
    send("thanks!", chat_id)
    msgs = chats.find_one({"chat_id": chat_id, "owner_key": "o1"})["messages"]
    assert [m["query"] for m in msgs] == ["Hi", "Find people looking for a CRM for small teams", "thanks!"]
    assert [m.get("message_type") == "chat" for m in msgs] == [True, False, True]
    assert len(calls) == 1 and len(jobs) == 1                            # only the research turn hit the router
    # the router saw the earlier greeting in the chat summary, as before
    assert "Hi" in calls[0]["user"]


def test_smalltalk_in_website_chat_never_starts_a_website_search(route):
    lg, chats, calls, jobs, send = route
    chat_id = send("Hi")
    chats.update_one({"chat_id": chat_id, "owner_key": "o1"},
                     {"$set": {"last_website_context": {"url": "https://acme.test", "keywords": ["acme crm"],
                                                         "match_phrases": ["acme"]}}})
    send("How are you doing?", chat_id)
    assert calls == [] and jobs == []
    assert chats.find_one({"chat_id": chat_id, "owner_key": "o1"})["messages"][-1]["message_type"] == "chat"
