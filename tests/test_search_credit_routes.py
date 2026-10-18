"""
tests/test_search_credit_routes.py — search credits through the real POST
/search handler (routes.search) with stub modules, mongomock collections, a
recording stub LLM, the real enqueue_search_job() and the real
add_search_to_chat(). Covers Option B (non-search turns at zero credits), the
429/503 denials with X-Flintel-Denial, same-key retries, the kill switch and
/api/me/usage. No real LLM, Mongo or network.
"""
import ast
import html
import json
import sys
from pathlib import Path

import pytest
from starlette.requests import Request

from tests.test_answer_persistence import env  # noqa: F401  (fixture)

mongomock = pytest.importorskip("mongomock")
import search_credits as sc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KEY1, KEY2 = "11111111-aaaa-4bbb-8ccc-000000000001", "22222222-aaaa-4bbb-8ccc-000000000002"
SEARCH_ROUTER = {"intent": "search", "reply": None, "keywords": ["crm small teams"], "time_window_days": None,
                 "match_phrases": ["looking for a CRM"], "evidence_required": None, "website_only": False,
                 "url_ask_type": "none", "target_entity": "small teams", "intent_types": None,
                 "resolved_query": "Find people looking for a CRM for small teams."}
CHAT_ROUTER = {"intent": "chat", "reply": "Happy to help — what would you like to research?", "keywords": None}


@pytest.fixture
def app(env, monkeypatch):
    lg, idx, _ = env
    rt = sys.modules["routes"]
    db = mongomock.MongoClient()["credit_routes"]
    db.flintel_credit_usage.create_index("owner_key", unique=True)
    monkeypatch.setattr(idx, "chats_collection", db.chats)
    monkeypatch.setattr(lg, "jobs_collection", db.jobs)
    monkeypatch.setattr(sys.modules["turn_finisher"], "enqueue", lambda *a, **k: None, raising=False)
    state = {"router": SEARCH_ROUTER, "llm_calls": 0, "enqueues": [], "limit": 40, "store_broken": False,
             "attribute_flags": []}

    def fake_llm(system, user, max_tokens=None, enable_web_search=False, force_json_prefill=False):
        state["llm_calls"] += 1
        return json.dumps(state["router"])
    monkeypatch.setattr(lg, "_call_claude", fake_llm)
    monkeypatch.setattr(rt, "get_owner", lambda request: ("o1", "anon"))
    monkeypatch.setattr(rt, "_is_owner_busy", lambda k: False)
    monkeypatch.setattr(rt, "_set_owner_busy", lambda k: None)
    monkeypatch.setattr(rt, "_clear_owner_busy", lambda k: None)

    def enqueue(topic_key, keywords, platform, credit_request_id=None, attribute=False):
        state["enqueues"].append((topic_key, credit_request_id))
        state["attribute_flags"].append(attribute)
        return lg.enqueue_search_job(topic_key, keywords, platform, credit_request_id=credit_request_id,
                                     attribute=attribute)
    monkeypatch.setattr(rt, "enqueue_search_job", enqueue)

    class Broken:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise sc.CreditStoreError("store down")
            return fail

    def store():
        coll = Broken() if state["store_broken"] else db.flintel_credit_usage
        return sc.CreditStore(coll, idx.chats_collection, db.jobs, idx.add_search_to_chat,
                              enabled=lambda: True, limit=lambda: state["limit"])
    monkeypatch.setattr(rt, "_credit_store", store)
    session = {}

    def send(query, submit_key=None, chat_id=None):
        req = Request({"type": "http", "method": "POST", "path": "/search", "headers": [], "query_string": b"",
                       "session": session, "app": idx.app, "router": idx.app.router})
        return rt.search(req, query=query, platform="All Platforms", chat_id=chat_id, submit_key=submit_key)
    return {"rt": rt, "idx": idx, "lg": lg, "db": db, "state": state, "send": send, "store": store}


def credits_doc(a):
    return a["db"].flintel_credit_usage.find_one({"owner_key": "o1"}) or {}


def used(a):
    return sum(int(v) for v in (credits_doc(a).get("used_by_period") or {}).values())


def body(resp):
    return html.unescape(resp.body.decode("utf-8"))


def test_accepted_search_costs_one_credit(app):
    resp = app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/chat/")
    chat_id = resp.headers["location"].rsplit("/", 1)[-1]
    rid = sc.request_id("o1", KEY1)
    assert used(app) == 1 and app["state"]["enqueues"][0][1] == rid
    chat = app["db"].chats.find_one({"chat_id": chat_id})
    assert [m.get("credit_request_id") for m in chat["messages"]] == [rid]
    job = app["db"].jobs.find_one({"topic_key": app["state"]["enqueues"][0][0]})
    assert job is not None and "credit_request_ids" not in job                    # JOB_ATTRIBUTION_MODE=off (default)
    assert app["state"]["attribute_flags"] == [False]
    assert credits_doc(app)["res"][rid]["state"] == sc.COMMITTED
    assert sc.check_invariant(credits_doc(app)) == []


def test_same_key_retry_redirects_without_a_new_charge_or_router_call(app):
    first = app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    calls = app["state"]["llm_calls"]
    again = app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert again.status_code == 303 and again.headers["location"] == first.headers["location"]
    assert app["state"]["llm_calls"] == calls and used(app) == 1 and len(app["state"]["enqueues"]) == 1
    chat_id = first.headers["location"].rsplit("/", 1)[-1]
    assert len(app["db"].chats.find_one({"chat_id": chat_id})["messages"]) == 1


def test_two_intentional_submits_with_the_same_text_are_two_searches(app):
    app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    app["send"]("Find people looking for a CRM for small teams", submit_key=KEY2)
    assert used(app) == 2 and len(app["state"]["enqueues"]) == 2


def test_exhausted_search_is_denied_before_enqueue_and_chat_still_works(app):
    app["state"]["limit"] = 1
    app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert used(app) == 1
    denied = app["send"]("Find churn complaints about Notion", submit_key=KEY2)
    assert denied.status_code == 429
    assert denied.headers["X-Flintel-Denial"] == "credits-exhausted" and int(denied.headers["Retry-After"]) > 0
    assert "out of free searches" in body(denied) and 'data-denial="credits-exhausted"' in body(denied)
    assert len(app["state"]["enqueues"]) == 1 and used(app) == 1
    assert app["db"].chats.count_documents({"owner_key": "o1", "messages": {"$size": 0}}) == 0   # cleaned up
    app["state"]["router"] = CHAT_ROUTER                                     # Option B: chat turns still work
    ok = app["send"]("Thanks, what else can you look into?", submit_key="33333333-aaaa-4bbb-8ccc-000000000003")
    assert ok.status_code == 303
    chat = app["db"].chats.find_one({"chat_id": ok.headers["location"].rsplit("/", 1)[-1]})
    assert chat["messages"][-1]["message_type"] == "chat" and used(app) == 1


def test_small_talk_never_touches_credits(app):
    app["state"]["limit"] = 1
    app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    resp = app["send"]("Hi", submit_key=KEY2)
    assert resp.status_code == 303 and used(app) == 1


def test_unavailable_credit_store_is_503_and_distinct(app):
    app["state"]["store_broken"] = True
    resp = app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert resp.status_code == 503 and resp.headers["X-Flintel-Denial"] == "credit-store-unavailable"
    assert resp.headers["Retry-After"] == "30"
    assert sc.UNAVAILABLE_MESSAGE in body(resp) and "out of free searches" not in body(resp)
    assert app["state"]["enqueues"] == []


def test_in_flight_duplicate_gets_the_busy_card(app):
    rid = sc.request_id("o1", KEY1)
    app["db"].chats.insert_one({"chat_id": "c1", "owner_key": "o1", "title": "t", "messages": []})
    store = app["store"]()
    t = store.reserve("o1", "anon", rid, "x", "c1", True)
    store.start(t)
    store.claim(t)                                                           # the original is sending
    resp = app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert resp.status_code == 200 and sc.IN_FLIGHT_MESSAGE in body(resp)
    assert "X-Flintel-Denial" not in resp.headers and app["state"]["enqueues"] == []


def test_kill_switch_runs_the_pre_credit_code(app, monkeypatch):
    rt = app["rt"]
    legacy_calls = []
    monkeypatch.setattr(rt, "_credit_store", lambda: None)
    monkeypatch.setattr(rt, "enqueue_search_job", lambda tk, kws, plat: legacy_calls.append((tk, kws, plat)))
    resp = app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert resp.status_code == 303 and len(legacy_calls) == 1 and credits_doc(app) == {}
    chat = app["db"].chats.find_one({"chat_id": resp.headers["location"].rsplit("/", 1)[-1]})
    assert "credit_request_id" not in chat["messages"][-1]


def test_api_me_usage_is_additive(app, monkeypatch):
    rt = app["rt"]
    app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    monkeypatch.setattr(rt, "get_current_user", lambda request: {"id": "64b7f0c2a1b2c3d4e5f60718", "email": "o1"})
    monkeypatch.setattr(rt, "users_collection", type("U", (), {"find_one": lambda self, *a, **k: {"message_count": 7}})())
    req = Request({"type": "http", "method": "GET", "path": "/api/me/usage", "headers": [], "query_string": b"",
                   "session": {}})
    out = json.loads(rt.api_me_usage(req).body)
    assert out["message_count"] == 7 and out["plan"] == "Free"
    c = out["credits"]
    assert c["enforced"] is True and c["status"] == "ok" and c["used"] == 1 and c["remaining"] == 39
    assert c["limit"] == 40 and c["resets_at"].endswith("+00:00")
    monkeypatch.setattr(rt, "_credit_store", lambda: None)
    assert json.loads(rt.api_me_usage(req).body)["credits"] == {"enforced": False, "status": "disabled"}


def test_only_search_and_usage_read_the_credit_store():
    tree = ast.parse((ROOT / "routes.py").read_text(encoding="utf-8"))
    users = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "_credit_store":
                    users.add(node.name)
    assert users == {"search", "api_me_usage"}   # GET chat pages, the stream and reloads never touch credits


def test_field_mode_records_the_request_id_on_the_job(app, monkeypatch):
    import config
    monkeypatch.setattr(config, "JOB_ATTRIBUTION_MODE", "field")
    app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    rid = sc.request_id("o1", KEY1)
    assert app["state"]["attribute_flags"] == [True]
    assert app["db"].jobs.find_one({"credit_request_ids": rid}) is not None


def test_kill_switch_performs_zero_credit_store_operations(app, monkeypatch):
    rt = app["rt"]
    import config

    class Tripwire:
        def __getattr__(self, name):
            raise AssertionError(f"credit store touched with the kill switch on: {name}")
    monkeypatch.setattr(config, "CREDIT_ENFORCEMENT_DISABLED", True)
    monkeypatch.setattr(sys.modules["database"], "credit_usage_collection", Tripwire(), raising=False)
    monkeypatch.setattr(rt, "_credit_store", lambda: sc.default_store())            # the real wiring
    legacy = []
    monkeypatch.setattr(rt, "enqueue_search_job", lambda tk, kws, plat: legacy.append(tk))
    assert app["send"]("Find people looking for a CRM for small teams", submit_key=KEY1).status_code == 303
    app["state"]["router"] = CHAT_ROUTER
    assert app["send"]("What else can you look into?", submit_key=KEY2).status_code == 303
    monkeypatch.setattr(rt, "get_current_user", lambda request: {"id": "64b7f0c2a1b2c3d4e5f60718", "email": "o1"})
    monkeypatch.setattr(rt, "users_collection", type("U", (), {"find_one": lambda self, *a, **k: {}})())
    req = Request({"type": "http", "method": "GET", "path": "/api/me/usage", "headers": [], "query_string": b"",
                   "session": {}})
    assert json.loads(rt.api_me_usage(req).body)["credits"] == {"enforced": False, "status": "disabled"}
    assert len(legacy) == 1 and credits_doc(app) == {}


# ── W1–W6: website-URL searches through the real /search handler ───────────
SITE = "https://acme-crm.example.com"
EVIDENCE = {"url": SITE, "combined_text": "Acme CRM helps small teams track deals.",
            "structured_evidence": {"business": "Acme CRM", "title": "Acme CRM"}, "evidence_quality": "good"}
BARE_URL_ROUTER = {**SEARCH_ROUTER, "keywords": None, "match_phrases": None, "website_only": True,
                   "url_ask_type": "none", "resolved_query": None}
URL_ASK_ROUTER = {**SEARCH_ROUTER, "keywords": None, "match_phrases": None, "website_only": False,
                  "url_ask_type": "generic_own_business", "resolved_query": None}


@pytest.fixture
def site(app, monkeypatch):
    rt = app["rt"]
    calls = {"fetch": 0, "keywords": 0}
    knobs = {"evidence": EVIDENCE, "keywords": {"keywords": ["crm for small teams", "deal tracking"],
                                                "match_phrases": ["looking for a simple CRM"]}}

    def fetch(url, query, call_claude):
        calls["fetch"] += 1
        return knobs["evidence"]

    def keywords(query, url, structured):
        calls["keywords"] += 1
        return knobs["keywords"]
    monkeypatch.setattr(rt, "get_or_fetch_website_evidence", fetch)
    monkeypatch.setattr(rt, "generate_keywords_for_website_request", keywords)
    monkeypatch.setattr(rt.website_intelligence, "format_structured_summary_for_answer",
                        lambda summary: "Acme CRM summary", raising=False)
    app["state"]["router"] = BARE_URL_ROUTER
    return {**app, "calls": calls, "knobs": knobs}


def _chat_of(a, resp):
    return a["db"].chats.find_one({"chat_id": resp.headers["location"].rsplit("/", 1)[-1]})


def test_w1_accepted_bare_url_search(site):
    resp = site["send"](SITE, submit_key=KEY1)
    assert resp.status_code == 303 and used(site) == 1
    rid = sc.request_id("o1", KEY1)
    msg = _chat_of(site, resp)["messages"][-1]
    assert msg["website_only"] is True and msg["credit_request_id"] == rid
    assert msg["website_evidence"]["url"] == SITE and msg["keywords"] == ["crm for small teams", "deal tracking"]
    assert len(site["state"]["enqueues"]) == 1 and site["state"]["enqueues"][0][1] == rid
    job = site["db"].jobs.find_one({"topic_key": site["state"]["enqueues"][0][0]})
    assert job is not None and "credit_request_ids" not in job                     # attribution off by default
    assert _chat_of(site, resp)["last_website_context"]["url"] == SITE
    assert credits_doc(site)["res"][rid]["state"] == sc.COMMITTED
    assert sc.check_invariant(credits_doc(site)) == []


def test_w2_accepted_bare_url_search_without_keywords(site):
    site["knobs"]["keywords"] = {"keywords": None, "match_phrases": None}
    site["knobs"]["evidence"] = {**EVIDENCE, "structured_evidence": {}}             # no seed: no fuzzy keywords
    resp = site["send"](SITE, submit_key=KEY1)
    assert resp.status_code == 303 and used(site) == 1 and site["state"]["enqueues"] == []
    msg = _chat_of(site, resp)["messages"][-1]
    assert msg["website_only"] is True and msg["credit_request_id"] == sc.request_id("o1", KEY1)
    assert sc.check_invariant(credits_doc(site)) == []


def test_w3_exhausted_bare_url_search(site):
    site["state"]["limit"] = 1
    site["send"](SITE, submit_key=KEY1)
    fetches, kw_calls = site["calls"]["fetch"], site["calls"]["keywords"]
    denied = site["send"]("https://other-site.example.com", submit_key=KEY2)
    assert denied.status_code == 429 and denied.headers["X-Flintel-Denial"] == "credits-exhausted"
    assert int(denied.headers["Retry-After"]) > 0 and "out of free searches" in body(denied)
    # Option B: the fetch decides whether this is a search at all, so it still runs;
    # the precheck then stops before keyword generation and the reservation.
    assert site["calls"]["fetch"] == fetches + 1 and site["calls"]["keywords"] == kw_calls
    assert len(site["state"]["enqueues"]) == 1 and used(site) == 1
    assert site["db"].chats.count_documents({"owner_key": "o1", "messages": {"$size": 0}}) == 0
    assert sc.check_invariant(credits_doc(site)) == []


def test_w3b_exhausted_url_plus_ask_skips_the_fetch(site):
    site["state"]["limit"] = 1
    site["send"](SITE, submit_key=KEY1)
    site["state"]["router"] = URL_ASK_ROUTER
    fetches = site["calls"]["fetch"]
    denied = site["send"](f"Find customers for my site {SITE}", submit_key=KEY2)
    assert denied.status_code == 429 and site["calls"]["fetch"] == fetches          # no fetch, no keywords
    assert len(site["state"]["enqueues"]) == 1 and used(site) == 1


def test_w3c_time_range_question_is_not_prechecked(site):
    site["state"]["limit"] = 1
    site["send"](SITE, submit_key=KEY1)
    site["state"]["router"] = URL_ASK_ROUTER
    fetches = site["calls"]["fetch"]
    resp = site["send"](f"Find customers for my site {SITE} from 10/9", submit_key=KEY2)   # day/month unclear
    assert resp.status_code == 303                                                   # the question, not a 429
    msg = _chat_of(site, resp)["messages"][-1]
    assert msg["message_type"] == "chat" and "October 9 or September 10" in msg["claude_answer"]
    assert site["calls"]["fetch"] == fetches + 1 and used(site) == 1                  # existing interpretation kept


def test_w4_same_key_retry_of_a_website_search(site):
    first = site["send"](SITE, submit_key=KEY1)
    llm, fetches, kws = site["state"]["llm_calls"], site["calls"]["fetch"], site["calls"]["keywords"]
    again = site["send"](SITE, submit_key=KEY1)
    assert again.status_code == 303 and again.headers["location"] == first.headers["location"]
    assert (site["state"]["llm_calls"], site["calls"]["fetch"], site["calls"]["keywords"]) == (llm, fetches, kws)
    assert used(site) == 1 and len(site["state"]["enqueues"]) == 1
    assert len(_chat_of(site, first)["messages"]) == 1


def test_w5_url_plus_ask_search_is_charged_once(site):
    site["state"]["router"] = URL_ASK_ROUTER
    resp = site["send"](f"Find customers for my site {SITE}", submit_key=KEY1)
    assert resp.status_code == 303 and used(site) == 1 and site["calls"]["fetch"] == 1
    msg = _chat_of(site, resp)["messages"][-1]
    assert not msg.get("website_only") and msg["credit_request_id"] == sc.request_id("o1", KEY1)
    assert msg["website_context"] == "Acme CRM summary" and len(site["state"]["enqueues"]) == 1
    assert sc.check_invariant(credits_doc(site)) == []


def test_w6_kill_switch_keeps_the_pre_credit_website_path(site, monkeypatch):
    rt = site["rt"]
    legacy = []
    monkeypatch.setattr(rt, "_credit_store", lambda: None)
    monkeypatch.setattr(rt, "enqueue_search_job", lambda tk, kws, plat: legacy.append((tk, kws, plat)))
    resp = site["send"](SITE, submit_key=KEY1)
    assert resp.status_code == 303 and len(legacy) == 1 and credits_doc(site) == {}
    msg = _chat_of(site, resp)["messages"][-1]
    assert msg["website_only"] is True and "credit_request_id" not in msg
