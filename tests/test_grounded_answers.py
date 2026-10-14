"""
tests/test_grounded_answers.py — Flintel answers only from verified data.

  * exploratory questions ("How can you help me find leads for my CRM?") get a
    fixed reply built from capabilities this codebase actually implements —
    no model call, no advice, no invented findings, an explicit "nothing
    searched yet" — and an offer; a bare "yes" right after it runs exactly that
    search through the normal research pipeline;
  * a business / market question the router answers as "chat" without any
    collected evidence never shows the model's text (no generic advice);
    with stored posts given to the router (a follow-up about results) the
    grounded reply is kept; ordinary small talk is untouched;
  * a missing router reply gets an honest fixed message — no second,
    general-purpose LLM call; the router runs without web search and its
    instructions forbid general-knowledge answers;
  * capability descriptions match the implementation (platform list read from
    the templates, matching / links / dates / time ranges / outreach exist);
  * dates on answer posts come only from the matched stored post; a post
    without one shows none (browser check).

Real /search route against stub modules and a dict-backed chats collection;
recording LLM stub. No real LLM / Mongo / network.
"""
import copy
import json
import re
import sys
from pathlib import Path

import pytest

from tests.test_answer_persistence import ORIGIN, _for_template, _render, browser, env  # noqa: F401
from tests.test_reliability_ux_perf import ListChats

ROOT = Path(__file__).resolve().parent.parent
OWNER = "o1"
SEARCH_ROUTER = {"intent": "search", "reply": None, "keywords": ["crm"], "time_window_days": None,
                 "match_phrases": ["looking for a CRM"], "evidence_required": None, "website_only": False,
                 "url_ask_type": "none", "target_entity": "people looking for a CRM", "intent_types": None,
                 "resolved_query": "Find people looking for a CRM."}
GENERIC_ADVICE = ("The best strategy is content marketing: SaaS founders typically struggle with churn, onboarding "
                  "and pricing, and 73% of buyers prefer free trials. Focus on LinkedIn ads and cold email.")
# phrases that would mean model-written market content reached the user
ADVICE_MARKERS = ("best strategy", "typically struggle", "73%", "LinkedIn ads", "cold email")


@pytest.fixture
def flow(env, monkeypatch):
    lg, idx, _ = env
    rt = sys.modules["routes"] if "routes" in sys.modules else __import__("routes")
    chats = ListChats()
    monkeypatch.setattr(idx, "chats_collection", chats)
    state = {"router": SEARCH_ROUTER, "calls": [], "jobs": []}

    def fake_llm(system, user, max_tokens=None, enable_web_search=False, force_json_prefill=False):
        state["calls"].append({"system": system, "user": user, "web": enable_web_search})
        if system == lg.CLAUDE_ROUTER_SYSTEM_PROMPT:
            return json.dumps(state["router"])
        return json.dumps({"format": "source_list", "headline": "h", "executive_summary": "s", "platforms": []})
    monkeypatch.setattr(lg, "_call_claude", fake_llm)
    monkeypatch.setattr(rt, "get_owner", lambda request: (OWNER, "guest"))
    monkeypatch.setattr(rt, "_is_owner_busy", lambda k: False)
    monkeypatch.setattr(rt, "_set_owner_busy", lambda k: None)
    monkeypatch.setattr(rt, "generate_chat_title", lambda q: "t")
    monkeypatch.setattr(rt, "enqueue_search_job", lambda *a, **k: state["jobs"].append(a))
    fl = sys.modules["flintel"]
    monkeypatch.setattr(fl, "should_trigger_immediately", lambda *a, **k: False, raising=False)

    class Req:
        def __init__(self):
            self.session = {}

    def send(query, chat_id=None):
        resp = rt.search(Req(), query=query, platform="Reddit", chat_id=chat_id)
        assert resp.status_code == 303
        cid = resp.headers["location"].rsplit("/", 1)[-1]
        return cid, chats.find_one({"chat_id": cid, "owner_key": OWNER})
    return lg, rt, chats, state, send


def _router_calls(lg, state):
    return [c for c in state["calls"] if c["system"] == lg.CLAUDE_ROUTER_SYSTEM_PROMPT]


# ── exploratory questions: verified capabilities + an offer, nothing searched ─
@pytest.mark.parametrize("text,offer", [
    ("How can you help me find leads for my CRM?", "Find people looking for a CRM"),
    ("How could Flintel help me find customers for my dental clinic?", "Find people looking for a dental clinic"),
    ("How do I use Flintel to find buyers for my agency?", "Find people looking for an agency"),
    ("How can you help me understand my competitors?", "Find Reddit conversations about my competitors"),
    ("What can you do for my CRM startup?", "Find Reddit conversations about my CRM startup"),
])
def test_exploratory_question_gets_verified_capabilities_and_an_offer(flow, text, offer):
    lg, rt, chats, state, send = flow
    cid, doc = send(text)
    msg = doc["messages"][-1]
    assert msg["message_type"] == "chat"
    answer = msg["claude_answer"]
    assert "I haven't searched anything for this yet" in answer and f'"{offer}"' in answer
    assert lg.VERIFIED_CAPABILITIES_TEXT in answer
    assert state["calls"] == [] and state["jobs"] == []               # no model, no research
    assert doc["research_offer"] == {"query": offer, "answer": answer}
    assert not re.search(r"\d+ (?:posts|leads|buyers|people)", answer)  # no counts claimed


def test_yes_runs_exactly_the_offered_search(flow):
    lg, rt, chats, state, send = flow
    cid, _ = send("How can you help me find leads for my CRM?")
    _, doc = send("yes", cid)
    msg = doc["messages"][-1]
    assert msg["query"] == "yes — Find people looking for a CRM"
    assert msg.get("message_type") != "chat" and msg["claude_answer"] is None       # research pending
    router = _router_calls(lg, state)
    assert len(router) == 1 and "Find people looking for a CRM" in router[0]["user"]
    assert len(state["jobs"]) == 1


def test_yes_without_a_current_offer_is_routed_normally(flow):
    lg, rt, chats, state, send = flow
    cid, _ = send("How can you help me find leads for my CRM?")
    send("Find people looking for an ERP", cid)                       # the offer is no longer the last reply
    state["calls"].clear()
    state["router"] = {"intent": "chat", "reply": "Okay!", "keywords": None, "time_window_days": None}
    _, doc = send("yes", cid)
    assert doc["messages"][-1]["query"] == "yes"
    assert len(_router_calls(lg, state)) == 1 and "Find people looking for a CRM" not in _router_calls(lg, state)[0]["user"]


# ── router "chat" replies are grounded ───────────────────────────────────────
@pytest.mark.parametrize("text", ["What's the best marketing strategy for a SaaS startup?",
                                  "How do I get more customers for my bakery?",
                                  "What are the biggest trends in fintech right now?"])
def test_generic_business_advice_from_the_router_never_reaches_the_user(flow, text):
    lg, rt, chats, state, send = flow
    state["router"] = {"intent": "chat", "reply": GENERIC_ADVICE, "keywords": None, "time_window_days": None}
    cid, doc = send(text)
    answer = doc["messages"][-1]["claude_answer"]
    assert not any(m in answer for m in ADVICE_MARKERS)
    assert "haven't searched" in answer and "Reply **yes**" in answer
    assert state["jobs"] == []                                         # explaining is not researching
    offer = doc["research_offer"]["query"]
    _, doc = send("yes please", cid)                                  # the confirmation keeps the context
    assert doc["messages"][-1]["query"] == f"yes please — {offer}" and len(state["jobs"]) == 1


def test_a_follow_up_about_stored_posts_keeps_the_grounded_router_reply(flow):
    lg, rt, chats, state, send = flow
    chats.docs[("c1", OWNER)] = _chat_with_result()
    state["router"] = {"intent": "chat", "reply": "Post 2 says the pricing felt too high for a 3-person team.",
                       "keywords": None, "time_window_days": None}
    _, doc = send("What did post 2 say about pricing?", "c1")
    router = _router_calls(lg, state)
    assert router and "Referenced post data" in router[0]["user"]       # evidence was given to the router
    assert doc["messages"][-1]["claude_answer"] == state["router"]["reply"]


def test_ordinary_conversation_is_untouched(flow):
    lg, rt, chats, state, send = flow
    state["router"] = {"intent": "chat", "reply": "Glad it helped!", "keywords": None, "time_window_days": None}
    _, doc = send("That was really useful earlier, appreciate you")
    assert doc["messages"][-1]["claude_answer"] == "Glad it helped!"


def test_missing_router_reply_gets_an_honest_message_not_a_second_llm_call(flow):
    lg, rt, chats, state, send = flow
    state["router"] = {"intent": "chat", "reply": "", "keywords": None, "time_window_days": None}
    _, doc = send("tell me something")
    assert doc["messages"][-1]["claude_answer"] == lg.CHAT_UNAVAILABLE_REPLY
    assert [c["system"] for c in state["calls"]] == [lg.CLAUDE_ROUTER_SYSTEM_PROMPT]


def test_router_runs_without_web_search_and_forbids_general_knowledge(flow):
    lg, rt, chats, state, send = flow
    send("Find people looking for a CRM")
    assert _router_calls(lg, state)[0]["web"] is False
    p = " ".join(lg.CLAUDE_ROUTER_SYSTEM_PROMPT.split())
    assert "GROUNDING (absolute — Flintel is an evidence product, not a general chatbot)" in p
    assert "never general-knowledge answers presented as Flintel's findings" in p
    assert "using your own general knowledge of what's commonly discussed" not in p
    assert "general knowledge questions, a follow-up" not in p


def test_explicit_research_still_works(flow):
    lg, rt, chats, state, send = flow
    _, doc = send("Find Reddit posts from people complaining about Acme")
    assert len(_router_calls(lg, state)) == 1 and len(state["jobs"]) == 1
    assert doc["messages"][-1]["claude_answer"] is None


@pytest.mark.parametrize("text", ["yes", "Yes please", "go ahead", "haan kar do", "sure, do it"])
def test_confirmations(env, text):
    lg, _, _ = env
    assert lg.is_confirmation(text)


@pytest.mark.parametrize("text", ["yes but only in the USA", "ok thanks", "no", "yes find ERP buyers instead"])
def test_not_bare_confirmations(env, text):
    lg, _, _ = env
    assert not lg.is_confirmation(text)


# ── capability descriptions match the implementation ─────────────────────────
def test_capability_text_matches_the_code(env):
    lg, idx, _ = env
    html = (ROOT / "templates" / "chat.html").read_text()
    options = re.findall(r'<li role="option"[^>]*data-value="([^"]+)"[^>]*>(.*?)</li>', html, re.S)
    soon = {v for v, body in options if "platform-opt-soon" in body}
    live = {v for v, body in options if "platform-opt-soon" not in body and v != "All Platforms"}
    assert live == {"Reddit"} and soon == {"X / Twitter", "LinkedIn", "Facebook"}
    text = lg.VERIFIED_CAPABILITIES_TEXT + lg.SMALLTALK_REPLIES["platforms"] + lg.SMALLTALK_REPLIES["how_it_works"]
    for name in soon:
        assert name in text
    assert "Reddit today" in lg.VERIFIED_CAPABILITIES_TEXT and "coming soon" in lg.VERIFIED_CAPABILITIES_TEXT
    # each claimed capability exists
    assert callable(lg.generate_query_embeddings_batch)                 # "matched by meaning"
    assert callable(lg._patch_post_urls_into_answer)                    # "original link"
    assert callable(lg.build_outreach_report)                           # "outreach notes"
    import timerange
    assert timerange.parse_time_range("from the last 24 hours", __import__("datetime").datetime(
        2026, 10, 14, tzinfo=__import__("datetime").timezone.utc)) is not None   # "narrow by time"
    assert "buyers" in lg.build_guidance_note("x") or "buyer" in lg.CLAUDE_ANALYSIS_SYSTEM_PROMPT


def test_analysis_prompt_forbids_filler_advice(env):
    lg, _, _ = env
    p = " ".join(lg.CLAUDE_ANALYSIS_SYSTEM_PROMPT.split())
    # the analysis prompt is pinned byte-for-byte (test_strict_intent); its existing rules already tie
    # every insight to a finding and forbid presenting a suggestion as something the posts said
    assert "each traceable to a specific finding above" in p
    assert "never present a suggestion as something the posts said" in p


# ── dates on answer posts: only from the matched stored post ─────────────────
def test_post_dates_come_only_from_matched_signals(env):
    lg, _, _ = env
    answer = json.dumps({"format": "source_list", "headline": "h", "executive_summary": "s", "platforms": [
        {"platform": "reddit", "total_analyzed": 2, "shown_count": 2, "posts": [
            {"index": 1, "title": "Need a CRM for my shop", "summary": "a", "published_at": "1999-01-01"},
            {"index": 2, "title": "Spreadsheets are failing us", "summary": "b"}]}]})
    signals = [{"title": "Need a CRM for my shop", "post_text": "Need a CRM for my shop, any ideas?",
                "post_url": "https://www.reddit.com/r/crm/comments/a/", "platform": "reddit",
                "published_at": "2026-10-12T09:15:00+00:00"},
               {"title": "Spreadsheets are failing us", "post_text": "Spreadsheets are failing us at 6 people.",
                "post_url": "https://www.reddit.com/r/crm/comments/b/", "platform": "reddit"}]
    posts = json.loads(lg._patch_post_urls_into_answer(answer, signals))["platforms"][0]["posts"]
    assert posts[0]["published_at"] == "2026-10-12T09:15:00+00:00"     # the stored date replaces the model's
    assert posts[0]["link"] == "https://www.reddit.com/r/crm/comments/a/"
    assert "published_at" not in posts[1]                              # unknown stays unknown


def _chat_with_result(post_dates=("2026-10-12T09:15:00+00:00", None)):
    answer = {"format": "source_list", "headline": "Two people want a simple CRM",
              "executive_summary": "Both posts ask for a simple CRM.", "platforms": [
                  {"platform": "reddit", "total_analyzed": 2, "shown_count": 2, "posts": [
                      dict({"index": 1, "source": "crm", "title": "Need a CRM for my shop", "summary": "Asks for a CRM.",
                            "sentiment": "neutral", "link": "https://www.reddit.com/r/crm/comments/a/"},
                           **({"published_at": post_dates[0]} if post_dates[0] else {})),
                      dict({"index": 2, "source": "crm", "title": "CRM pricing", "summary": "Pricing too high.",
                            "sentiment": "negative", "link": "https://www.reddit.com/r/crm/comments/b/"},
                           **({"published_at": post_dates[1]} if post_dates[1] else {}))]}]}
    return {"chat_id": "c1", "owner_key": OWNER, "title": "CRM", "summary": "",
            "messages": [{"query": "Find people looking for a CRM", "topic_key": "crm", "keywords": ["crm"],
                          "targeting_platform": "reddit", "results": [], "claude_answer": json.dumps(answer)}]}


def test_browser_shows_a_posted_date_only_when_it_is_known(browser):
    chat = _for_template(_chat_with_result())
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    html = _render("chat.html", chat)

    def handle(route):
        if route.request.url.startswith(ORIGIN + "/chat/c1"):
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=html)
        if "marked" in route.request.url or "purify" in route.request.url:
            return route.fulfill(status=200, body="")
        return route.fulfill(status=200, body="")
    page.route("**/*", handle)
    try:
        page.goto(ORIGIN + "/chat/c1")
        page.wait_for_function("document.querySelectorAll('.ap-sources-table tr').length >= 3", timeout=5000)
        table = page.evaluate("""[...document.querySelectorAll('.ap-sources-table tr')].map(r =>
            [...r.querySelectorAll('th,td')].map(c => c.innerText.trim()))""")
        head, first, second = table[0], table[1], table[2]
        col = head.index("Posted")
        assert "2026" in first[col] and "Oct" in first[col]               # the stored date
        assert second[col] == "—"                                          # unknown: nothing guessed
        assert errors == []
    finally:
        page.close()


# ── empty / insufficient evidence ────────────────────────────────────────────
def test_empty_dataset_demands_an_honest_no_results_answer(env, monkeypatch):
    lg, _, _ = env
    seen = {}

    def fake_llm(system, user, **kw):
        seen["system"], seen["user"] = system, user
        return json.dumps({"format": "no_results", "message": "No matching posts were found."})
    monkeypatch.setattr(lg, "_call_claude", fake_llm)
    lg.analyze_with_claude("How do I get customers for my bakery?", [],
                           extra_context=lg.build_guidance_note("How do I get customers for my bakery?"))
    user = " ".join(seen["user"].split())
    assert "no_results" in user and "general-knowledge prose" in user               # no filler answer
    assert "do not fill the gap with general business advice" in user


def test_posts_without_text_are_not_treated_as_evidence(env):
    lg, _, _ = env
    stubs = [{"title": "r/crm", "post_text": None, "post_url": "https://www.reddit.com/r/crm/"},
             {"title": "Only a title", "post_text": "", "post_url": "https://www.reddit.com/r/crm/comments/x/"}]
    assert lg.build_claude_post_context(stubs) == []
