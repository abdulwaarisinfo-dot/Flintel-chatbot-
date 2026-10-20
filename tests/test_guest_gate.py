"""
tests/test_guest_gate.py — the guest search gate (guest_gate.py) and everything wired to it:

  * GuestGate state machine (reserve / complete / release / stale recovery) on mongomock, including a
    thread race and a failing store;
  * the real POST /search handler (routes.search) as a visitor: one genuine search, denial headers, nothing
    charged / enqueued / left behind on a refusal, small talk unaffected, signed-in users unaffected;
  * completion / release through the real answer pipeline (index._complete_message_answer_and_results);
  * the durable identity cookie (index.py) and the auth continuation (/login, /signup, Google callback);
  * the two browser modals in real Chromium (sign-up modal vs. search-limit modal, accessibility).

No real LLM, Mongo or network.
"""
import asyncio
import datetime as dt
import html
import importlib
import json
import re
import sys
import threading
from pathlib import Path

import pytest
from starlette.requests import Request

from tests.test_answer_persistence import env, GOOD  # noqa: F401  (fixture + sample answer)
from tests.test_search_credit_routes import (  # noqa: F401  (fixtures + helpers)
    CHAT_ROUTER, KEY1, KEY2, SEARCH_ROUTER, app, body, flat_cost,
)

mongomock = pytest.importorskip("mongomock")
ROOT = Path(__file__).resolve().parent.parent
T0 = dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=dt.timezone.utc)


def _gate_mod():
    sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None
    return importlib.import_module("guest_gate")


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t = self.t + dt.timedelta(seconds=seconds)


@pytest.fixture
def gg():
    return _gate_mod()


@pytest.fixture
def coll():
    return mongomock.MongoClient()["gate"]["flintel_guest_gate"]


@pytest.fixture
def clock():
    return Clock()


def make_gate(gg, coll, clock, limit=1, stale=900):
    return gg.GuestGate(coll, clock=clock, limit=limit, stale_seconds=stale)


# ───────────────────────── 1. state machine ─────────────────────────────────
def test_first_reserve_allows_and_a_second_live_search_is_busy(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    first = gate.reserve("g1", "t1", "topic", "c1")
    assert (first.kind, first.reserved, first.resumed) == (gg.ALLOW, True, False)
    second = gate.reserve("g1", "t2", "topic", "c1")
    assert second.kind == gg.BUSY and not second.reserved
    assert coll.find_one({"_id": "g1"})["inflight"]["token"] == "t1"        # the loser changed nothing


def test_same_token_again_is_a_resume_not_a_second_search(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    gate.reserve("g1", "t1", "topic", "c1")
    again = gate.reserve("g1", "t1", "topic", "c1")
    assert again.kind == gg.ALLOW and again.reserved and again.resumed
    assert coll.find_one({"_id": "g1"})["completed"] == 0


def test_completion_counts_once_and_exhausts_the_allowance(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    gate.reserve("g1", "t1", "topic", "c1")
    assert gate.complete("g1", "t1", "c1", "topic") == "completed"
    assert gate.complete("g1", "t1", "c1", "topic") == "duplicate"           # a repeated completion never counts twice
    doc = coll.find_one({"_id": "g1"})
    assert doc["completed"] == 1 and doc["inflight"] is None and doc["last_chat_id"] == "c1"
    denied = gate.reserve("g1", "t2", "topic2", "c2")
    assert denied.kind == gg.AUTH_REQUIRED and denied.chat_id == "c1" and not denied.reserved
    assert gate.peek("g1").kind == gg.AUTH_REQUIRED
    assert gate.state("g1")["used"] is True


def test_completion_of_a_token_that_was_never_reserved_is_ignored(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    gate.reserve("g1", "t1", "topic", "c1")
    assert gate.complete("g1", "never-reserved", "c1", "topic") == "duplicate"
    assert coll.find_one({"_id": "g1"})["completed"] == 0
    assert gate.complete("nobody", "t1") == "duplicate"


def test_release_frees_the_reservation_and_counts_nothing(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    gate.reserve("g1", "t1", "topic", "c1")
    assert gate.release("g1", "t1") is True
    assert coll.find_one({"_id": "g1"})["completed"] == 0
    assert gate.reserve("g1", "t2", "topic", "c1").kind == gg.ALLOW


def test_there_is_no_cap_on_incomplete_attempts(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    for i in range(40):
        assert gate.reserve("g1", f"t{i}", "topic", "c1").kind == gg.ALLOW
        assert gate.release("g1", f"t{i}") is True
    assert coll.find_one({"_id": "g1"})["completed"] == 0
    assert gate.peek("g1").kind == gg.ALLOW


def test_only_the_owning_token_can_release(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    gate.reserve("g1", "t1", "topic", "c1")
    assert gate.release("g1", "somebody-else") is False
    assert gate.reserve("g1", "t2").kind == gg.BUSY                          # still held by t1


def test_stale_reservation_stops_blocking_and_a_late_release_cannot_free_the_new_search(gg, coll, clock):
    gate = make_gate(gg, coll, clock, stale=900)
    gate.reserve("g1", "old", "topic", "c1")
    clock.advance(899)
    assert gate.reserve("g1", "t2").kind == gg.BUSY                          # not stale yet
    clock.advance(2)
    assert gate.peek("g1").kind == gg.ALLOW                                  # a dead search no longer blocks
    taken = gate.reserve("g1", "new", "topic", "c2")
    assert taken.kind == gg.ALLOW and taken.reserved
    assert gate.release("g1", "old") is False                                # the stale request's timeout cannot ...
    assert coll.find_one({"_id": "g1"})["inflight"]["token"] == "new"        # ... release a newer search
    assert gate.reserve("g1", "third").kind == gg.BUSY


def test_limit_zero_switches_the_gate_off_without_touching_the_store(gg, coll, clock):
    gate = make_gate(gg, coll, clock, limit=0)
    assert gate.reserve("g1", "t1").kind == gg.ALLOW and gate.peek("g1").kind == gg.ALLOW
    assert coll.count_documents({}) == 0


def test_a_larger_configured_limit_allows_that_many_completed_searches(gg, coll, clock):
    gate = make_gate(gg, coll, clock, limit=2)
    for i in (1, 2):
        assert gate.reserve("g1", f"t{i}").kind == gg.ALLOW
        gate.complete("g1", f"t{i}", f"c{i}")
    assert gate.reserve("g1", "t3").kind == gg.AUTH_REQUIRED


def test_concurrent_reserves_admit_exactly_one(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    kinds, barrier = [], threading.Barrier(16)

    def attempt(i):
        barrier.wait()
        kinds.append(gate.reserve("g1", f"tok-{i}", "topic", "c1").kind)
    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert kinds.count(gg.ALLOW) == 1 and kinds.count(gg.BUSY) == 15


def test_guests_are_independent_of_each_other(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    gate.reserve("g1", "t1")
    gate.complete("g1", "t1", "c1")
    assert gate.reserve("g2", "t2").kind == gg.ALLOW


class _BrokenColl:
    def __getattr__(self, name):
        def fail(*a, **k):
            raise RuntimeError("store down")
        return fail


def test_a_failing_store_fails_closed_and_the_hooks_never_raise(gg, clock, monkeypatch):
    gate = make_gate(gg, _BrokenColl(), clock)
    assert gate.reserve("g1", "t1").kind == gg.UNAVAILABLE
    assert gate.peek("g1").kind == gg.UNAVAILABLE
    assert gate.complete("g1", "t1") == "unavailable" and gate.release("g1", "t1") is False
    with pytest.raises(RuntimeError):
        gate.state("g1")
    monkeypatch.setattr(gg, "default_gate", lambda: gate)
    assert gg.on_answer_complete("g1", "c1", "topic", "t1") == "unavailable"
    assert gg.on_search_incomplete("g1", "t1") is False
    assert gg.auth_required_for("g1") is None and gg.resume_chat_id("g1") is None   # never a false popup


def test_owner_classification_and_hook_guards(gg, monkeypatch):
    assert gg.is_guest_owner("3f2c9a64d0a74b1c8f1e") is True
    assert gg.is_guest_owner("person@example.com") is False
    assert not gg.is_guest_owner("") and not gg.is_guest_owner(None)
    monkeypatch.setattr(gg, "default_gate", lambda: pytest.fail("a registered owner / missing token must not reach the gate"))
    assert gg.on_answer_complete("person@example.com", "c", "t", "tok") is None
    assert gg.on_search_incomplete("person@example.com", "tok") is None
    assert gg.on_answer_complete("g1", "c", "t", None) is None
    assert gg.auth_required_for("person@example.com") is None and gg.resume_chat_id("person@example.com") is None


def test_resume_and_auth_required_helpers_read_the_server_state(gg, coll, clock, monkeypatch):
    gate = make_gate(gg, coll, clock)
    monkeypatch.setattr(gg, "default_gate", lambda: gate)
    assert gg.resume_chat_id("g1") is None and gg.auth_required_for("g1") is None
    gate.reserve("g1", "t1")
    assert gg.resume_chat_id("g1") is None                                   # in flight is not "used"
    gate.complete("g1", "t1", "c1", "topic")
    assert gg.resume_chat_id("g1") == "c1"
    assert gg.auth_required_for("g1") == "auth_required"
    assert gg.auth_required_for("g1", "c1") == "auth_required"
    assert gg.auth_required_for("g1", "some-other-chat") is None             # only the completed search's page


# ───────────────────────── 2. routes ────────────────────────────────────────
@pytest.fixture
def gapp(app, monkeypatch, gg):
    """The credit-route harness with the guest gate ON (limit 1) for the guest owner `guest-1`."""
    rt = app["rt"]
    cfg = sys.modules["config"]
    monkeypatch.setattr(cfg, "GUEST_SEARCH_LIMIT", 1, raising=False)
    gate_coll = mongomock.MongoClient()["gate_routes"]["flintel_guest_gate"]
    sys.modules["database"].guest_gate_collection = gate_coll
    owner = {"key": "guest-1", "type": "anon"}
    monkeypatch.setattr(rt, "get_owner", lambda request: (owner["key"], owner["type"]))
    monkeypatch.setattr(rt, "_guest_identity_confirmed", lambda request, who: True)   # cookie proof: see the e2e tests below
    app.update({"gate_coll": gate_coll, "owner": owner, "gg": gg})
    return app


def gate_doc(a):
    return a["gate_coll"].find_one({"_id": a["owner"]["key"]}) or {}


def last_token(a):
    return gate_doc(a)["inflight"]["token"]


def chat_of(resp):
    return resp.headers["location"].rsplit("/", 1)[-1]


@pytest.mark.usefixtures("flat_cost")
def test_visitor_gets_one_search_then_a_sign_up_denial(gapp):
    first = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert first.status_code == 303
    assert gate_doc(gapp)["inflight"]["chat_id"] == chat_of(first) and gate_doc(gapp)["completed"] == 0
    # the answer arrives (the real pipeline calls this hook) -> the allowance is used
    token = last_token(gapp)
    gapp["gg"].on_answer_complete("guest-1", chat_of(first), "topic", token)
    assert gate_doc(gapp)["completed"] == 1
    enq = len(gapp["state"]["enqueues"])
    denied = gapp["send"]("Find churn complaints about Notion", submit_key=KEY2)
    assert denied.status_code == 403 and denied.headers["X-Flintel-Denial"] == "guest-auth-required"
    assert "Retry-After" not in denied.headers
    assert 'data-denial="guest-auth-required"' in body(denied) and 'id="guest-gate-marker"' in body(denied)
    assert len(gapp["state"]["enqueues"]) == enq                                  # nothing queued ...
    assert gapp["db"].flintel_credit_usage.count_documents({}) == 1               # ... nothing new charged
    assert gapp["db"].chats.count_documents({"owner_key": "guest-1", "messages": {"$size": 0}}) == 0   # no empty chat left


@pytest.mark.usefixtures("flat_cost")
def test_second_search_while_the_first_is_running_is_a_busy_message_not_a_popup(gapp):
    gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    busy = gapp["send"]("Find churn complaints about Notion", submit_key=KEY2)
    assert busy.status_code == 409 and busy.headers["X-Flintel-Denial"] == "guest-search-in-progress"
    assert 'id="guest-gate-marker"' not in body(busy) and "still finishing" in body(busy)
    assert len(gapp["state"]["enqueues"]) == 1


@pytest.mark.usefixtures("flat_cost")
def test_failed_or_timed_out_searches_can_be_retried_without_limit(gapp):
    for i in range(6):
        resp = gapp["send"]("Find people looking for a CRM for small teams", submit_key=f"{i:08d}-aaaa-4bbb-8ccc-000000000001")
        assert resp.status_code == 303
        gapp["gg"].on_search_incomplete("guest-1", last_token(gapp))              # error / timeout / cancelled
        assert gate_doc(gapp)["completed"] == 0
    assert len(gapp["state"]["enqueues"]) == 6


@pytest.mark.usefixtures("flat_cost")
def test_small_talk_is_never_gated(gapp):
    gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    gapp["gg"].on_answer_complete("guest-1", "c", "t", last_token(gapp))
    gapp["state"]["router"] = CHAT_ROUTER
    ok = gapp["send"]("Thanks, what else can you look into?", submit_key="33333333-aaaa-4bbb-8ccc-000000000003")
    assert ok.status_code == 303 and gate_doc(gapp)["completed"] == 1


def test_gate_store_outage_is_a_temporary_error_not_a_popup(gapp):
    class Broken:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise RuntimeError("down")
            return fail
    sys.modules["database"].guest_gate_collection = Broken()
    resp = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert resp.status_code == 503 and resp.headers["X-Flintel-Denial"] == "guest-gate-unavailable"
    assert resp.headers["Retry-After"] == "30" and 'id="guest-gate-marker"' not in body(resp)
    assert gapp["state"]["enqueues"] == [] and gapp["db"].flintel_credit_usage.count_documents({}) == 0


@pytest.mark.usefixtures("flat_cost")
def test_refused_credit_reservation_releases_the_guest_reservation(gapp):
    gapp["state"]["limit"] = 0                                                    # the credit store refuses
    denied = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert denied.status_code == 429
    assert gate_doc(gapp)["inflight"] is None and gate_doc(gapp)["completed"] == 0   # not stuck "busy"


@pytest.mark.usefixtures("flat_cost")
def test_guest_credit_denial_is_labelled_guest_and_a_signed_in_one_is_not(gapp):
    gapp["state"]["limit"] = 0
    guest = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert guest.status_code == 429 and guest.headers["X-Flintel-Denial"] == "credits-exhausted"
    assert guest.headers["X-Flintel-Audience"] == "guest"
    gapp["owner"].update(key="member@example.com", type="user")
    member = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY2)
    assert member.status_code == 429 and member.headers["X-Flintel-Denial"] == "credits-exhausted"
    assert "X-Flintel-Audience" not in member.headers
    assert 'data-state="credit_limit"' in body(member) or "credit_limit" in body(member)


@pytest.mark.usefixtures("flat_cost")
def test_signed_in_users_are_never_gated(gapp):
    gapp["owner"].update(key="member@example.com", type="user")
    for i in range(3):
        resp = gapp["send"]("Find people looking for a CRM for small teams", submit_key=f"{i:08d}-aaaa-4bbb-8ccc-0000000000aa")
        assert resp.status_code == 303
    assert gapp["gate_coll"].count_documents({}) == 0


@pytest.mark.usefixtures("flat_cost")
def test_retry_of_a_request_that_is_still_running_cannot_release_it(gapp, monkeypatch):
    rt = gapp["rt"]
    first = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    token = last_token(gapp)
    monkeypatch.setattr(rt, "_usage_request_id", lambda: token)                   # a retry that reuses the token
    again = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert again.status_code == 303 and chat_of(again) == chat_of(first)
    assert gate_doc(gapp)["inflight"]["token"] == token                           # the original is still protected
    assert len(gapp["state"]["enqueues"]) == 1


# ───────────────────── 3. completion through the answer pipeline ────────────
@pytest.fixture
def pipeline(env, monkeypatch, gg):
    lg, idx, chats = env
    cfg = sys.modules["config"]
    monkeypatch.setattr(cfg, "GUEST_SEARCH_LIMIT", 1, raising=False)
    gate_coll = mongomock.MongoClient()["gate_pipeline"]["flintel_guest_gate"]
    sys.modules["database"].guest_gate_collection = gate_coll
    chats.update_one({"chat_id": "c1", "owner_key": "guest-1"}, {"$set": {"summary": ""}})
    post = [{"title": "Hiring freeze", "post_text": "We have a hiring freeze.",
             "post_url": "https://www.reddit.com/r/pakistan/comments/p1/", "platform": "reddit"}]

    def run(llm_text, token="tok-1", reserve=True):
        q = "Find users looking for AI jobs in Pakistan"
        idx.add_search_to_chat("c1", "guest-1", q, lg.normalize_topic_key(q), ["kw"], "reddit")
        msg = chats.find_one({"chat_id": "c1", "owner_key": "guest-1"})["messages"][-1]
        msg["requested_at"] = dt.datetime(2026, 10, 20, 12, 0, 0)
        msg["request_id"] = token
        if reserve:
            gg.GuestGate(gate_coll).reserve("guest-1", token, msg["topic_key"], "c1")
        def llm(*a, **k):
            calls.append(1)
            if isinstance(llm_text, Exception):
                raise llm_text
            return llm_text
        monkeypatch.setattr(lg, "_call_claude", llm)
        idx._complete_message_answer_and_results("c1", "guest-1", msg, post)
        return msg

    calls = []
    return {"run": run, "coll": gate_coll, "lg": lg, "idx": idx, "chats": chats, "llm_calls": calls, "post": post}


def test_a_real_saved_answer_counts_exactly_once(pipeline):
    pipeline["run"](json.dumps(GOOD))
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 1 and doc["inflight"] is None and doc["last_chat_id"] == "c1"
    saved = pipeline["chats"].find_one({"chat_id": "c1", "owner_key": "guest-1"})["messages"][-1]
    assert saved.get("claude_answer")
    msg = dict(saved, claude_answer=None)                                         # the same request is processed again
    pipeline["idx"]._complete_message_answer_and_results("c1", "guest-1", msg, [])
    assert pipeline["coll"].find_one({"_id": "guest-1"})["completed"] == 1        # a duplicate never counts twice


def test_a_failed_analysis_releases_and_counts_nothing(pipeline):
    pipeline["run"](RuntimeError("model down"))
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 0 and doc["inflight"] is None                       # free to try again


def test_pipeline_ignores_registered_owners(pipeline, gg):
    lg, idx, chats = pipeline["lg"], pipeline["idx"], pipeline["chats"]
    q = "Find users looking for AI jobs in Pakistan"
    idx.add_search_to_chat("c9", "member@example.com", q, lg.normalize_topic_key(q), ["kw"], "reddit")
    msg = chats.find_one({"chat_id": "c9", "owner_key": "member@example.com"})["messages"][-1]
    msg["requested_at"], msg["request_id"] = dt.datetime(2026, 10, 20, 12, 0, 0), "tok-9"
    idx._complete_message_answer_and_results("c9", "member@example.com", msg, [])
    assert pipeline["coll"].count_documents({}) == 0


def test_timeout_fallback_saves_an_answer_but_never_counts(pipeline, gg, monkeypatch):
    lg = pipeline["lg"]
    gate = gg.GuestGate(pipeline["coll"])
    gate.reserve("guest-1", "tok-t", "topic", "c1")
    q = "Find users looking for AI jobs in Pakistan"
    pipeline["idx"].add_search_to_chat("c1", "guest-1", q, "topic", ["kw"], "reddit")
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: json.dumps(GOOD))
    try:
        lg._timeout_fallback_answer("c1", "guest-1", "topic", q, ["kw"], request_id="tok-t")
    except Exception:
        pass                                                                       # only the gate outcome matters here
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 0 and doc["inflight"] is None


# ───────────────────── 4. durable identity cookie ───────────────────────────
@pytest.fixture
def web(env, monkeypatch):
    """index.app with the real middleware stack (session + the guest identity cookie)."""
    testclient = pytest.importorskip("starlette.testclient")
    lg, idx, chats = env
    return idx, testclient.TestClient(idx.app, base_url="https://flintel.test")


def _gid(client):
    return client.cookies.get("fl_gid", domain="flintel.test")


def test_a_visitor_gets_a_signed_httponly_long_lived_identity_cookie(web):
    idx, client = web
    resp = client.get("/robots.txt")
    set_cookies = [h for h in resp.headers.get_list("set-cookie") if h.startswith("fl_gid=")]
    if not set_cookies:                                                            # first page that resolves an identity
        resp = client.get("/")
        set_cookies = [h for h in resp.headers.get_list("set-cookie") if h.startswith("fl_gid=")]
    assert set_cookies, "no guest identity cookie was issued"
    cookie = set_cookies[0].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie
    assert f"max-age={400 * 86400}" in cookie and "path=/" in cookie


def test_the_identity_survives_a_new_session_and_a_forged_cookie_is_ignored(web):
    idx, client = web
    client.get("/")
    gid = _gid(client)
    assert gid
    client.cookies.delete("session", domain="flintel.test")                                               # "restart the browser": session cookie lost
    client.get("/")
    assert _gid(client) == gid                                                     # same persistent identity
    client.cookies.set("fl_gid", "attacker-chosen-value", domain="flintel.test")
    client.get("/")
    assert _gid(client) != "attacker-chosen-value"                                 # unsigned / forged -> a new server-made id
    assert idx.peek_guest_id(Request({"type": "http", "headers": [(b"cookie", b"fl_gid=forged")], "session": {}})) is None


# ───────────────────── 5. auth continuation ─────────────────────────────────
def _post_form(client, path, data, headers=None):
    return client.post(path, data=data, headers=headers or {}, follow_redirects=False)


@pytest.fixture
def auth(env, monkeypatch, gg):
    idx, rt = env[1], None
    testclient = pytest.importorskip("starlette.testclient")
    rt = sys.modules.get("routes") or importlib.import_module("routes")
    cfg = sys.modules["config"]
    monkeypatch.setattr(cfg, "GUEST_SEARCH_LIMIT", 1, raising=False)
    mdb = mongomock.MongoClient()["auth_flow"]
    gate_coll = mdb["flintel_guest_gate"]
    sys.modules["database"].guest_gate_collection = gate_coll
    users = mdb["users"]
    chats = mdb["chats"]
    monkeypatch.setattr(idx, "chats_collection", chats)
    monkeypatch.setattr(idx, "users_collection", users)
    for mod in (idx, rt):
        for name in ("users_collection", "chats_collection"):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, {"users_collection": users, "chats_collection": chats}[name])
    class FakePwd:                                                                # passlib is stubbed out in this suite
        hash = staticmethod(lambda pw: "h:" + pw)
        verify = staticmethod(lambda pw, h: h == "h:" + pw)
    monkeypatch.setattr(rt, "pwd_context", FakePwd)
    client = testclient.TestClient(idx.app, base_url="https://flintel.test")
    return {"client": client, "idx": idx, "rt": rt, "gate": gate_coll, "chats": chats, "users": users}


def _guest_with_completed_chat(a, gg):
    """A visitor whose one search completed in chat `c-guest`; returns the guest id."""
    a["client"].get("/")
    gid = a["idx"].peek_guest_id  # sanity: helper exists
    owner = None
    for cookie in a["client"].cookies.jar:
        if cookie.name == "fl_gid":
            owner = a["idx"]._read_guest_cookie(type("R", (), {"cookies": {"fl_gid": cookie.value}})())
    assert owner
    a["chats"].insert_one({"chat_id": "c-guest", "owner_key": owner, "owner_type": "anon", "title": "t", "messages": [
        {"query": "q", "topic_key": "tk", "claude_answer": "a", "request_id": "tok"}]})
    gate = gg.GuestGate(a["gate"])
    gate.reserve(owner, "tok", "tk", "c-guest")
    gate.complete(owner, "tok", "c-guest", "tk")
    return owner


def test_signup_moves_the_guest_chat_to_the_account_and_resumes_it_without_a_new_search(auth, gg):
    owner = _guest_with_completed_chat(auth, gg)
    resp = _post_form(auth["client"], "/signup", {"email": "new@example.com", "password": "correct-horse-1",
                                                  "confirm_password": "correct-horse-1", "resume": "1"},
                      {"Accept": "application/json"})
    data = resp.json()
    assert resp.status_code == 200 and data["ok"] is True and data["redirect"] == "/chat/c-guest"
    chat = auth["chats"].find_one({"chat_id": "c-guest"})
    assert chat["owner_key"] == "new@example.com" and len(chat["messages"]) == 1   # moved, nothing re-run or duplicated
    assert auth["gate"].find_one({"_id": owner})["completed"] == 1                 # still exactly one guest search


def test_resume_is_taken_from_server_state_never_from_the_client(auth, gg):
    _guest_with_completed_chat(auth, gg)
    auth["chats"].insert_one({"chat_id": "c-foreign", "owner_key": "someone@else.example", "title": "x", "messages": []})
    resp = _post_form(auth["client"], "/signup", {"email": "new2@example.com", "password": "correct-horse-1",
                                                  "confirm_password": "correct-horse-1", "resume": "1",
                                                  "chat_id": "c-foreign", "next": "/chat/c-foreign"},
                      {"Accept": "application/json"})
    assert resp.json()["redirect"] == "/chat/c-guest"                              # the forged id is ignored
    assert auth["chats"].find_one({"chat_id": "c-foreign"})["owner_key"] == "someone@else.example"


def test_without_the_resume_flag_a_signup_behaves_as_before(auth, gg):
    _guest_with_completed_chat(auth, gg)
    resp = _post_form(auth["client"], "/signup", {"email": "plain@example.com", "password": "correct-horse-1",
                                                  "confirm_password": "correct-horse-1"})
    assert resp.status_code in (302, 303) and resp.headers["location"] == "/"


def test_resume_does_nothing_for_a_visitor_who_never_used_the_allowance(auth, gg):
    auth["client"].get("/")
    resp = _post_form(auth["client"], "/signup", {"email": "early@example.com", "password": "correct-horse-1",
                                                  "confirm_password": "correct-horse-1", "resume": "1"},
                      {"Accept": "application/json"})
    assert resp.json()["redirect"] == "/"


def test_failed_json_login_returns_an_error_not_a_redirect(auth):
    resp = _post_form(auth["client"], "/login", {"email": "nobody@example.com", "password": "wrong-password-1"},
                      {"Accept": "application/json"})
    data = resp.json()
    assert resp.status_code in (400, 401, 403) and data["ok"] is False and data["error"]


class _Oauth:
    """Stands in for authlib's client: no network, a fixed Google identity."""
    def __init__(self, fail=False):
        self.fail = fail
        self.google = self

    async def authorize_redirect(self, request, redirect_uri):
        from starlette.responses import RedirectResponse
        return RedirectResponse("https://accounts.google.test/auth", status_code=302)

    async def authorize_access_token(self, request):
        if self.fail:
            raise RuntimeError("denied")
        return {"userinfo": {"sub": "g-123", "email": "gg@example.com", "name": "G G"}}


def test_google_signin_resumes_the_same_conversation_once_and_a_duplicate_callback_is_harmless(auth, gg, monkeypatch):
    owner = _guest_with_completed_chat(auth, gg)
    monkeypatch.setattr(auth["rt"], "oauth", _Oauth())
    client = auth["client"]
    assert client.get("/auth/google/login?resume=1", follow_redirects=False).status_code == 302
    first = client.get("/auth/google/callback", follow_redirects=False)
    assert first.status_code in (302, 307) and first.headers["location"] == "/chat/c-guest"
    assert auth["chats"].find_one({"chat_id": "c-guest"})["owner_key"] == "gg@example.com"
    second = client.get("/auth/google/callback", follow_redirects=False)           # the callback fires again
    assert second.headers["location"] == "/"                                       # the one-shot flag is gone; nothing re-run
    assert auth["chats"].count_documents({"chat_id": "c-guest"}) == 1
    assert auth["gate"].find_one({"_id": owner})["completed"] == 1


def test_google_signin_without_the_resume_link_goes_home(auth, gg, monkeypatch):
    _guest_with_completed_chat(auth, gg)
    monkeypatch.setattr(auth["rt"], "oauth", _Oauth())
    auth["client"].get("/auth/google/login", follow_redirects=False)
    assert auth["client"].get("/auth/google/callback", follow_redirects=False).headers["location"] == "/"


def test_google_failure_returns_to_the_same_conversation_with_a_message(auth, gg, monkeypatch):
    _guest_with_completed_chat(auth, gg)
    monkeypatch.setattr(auth["rt"], "oauth", _Oauth(fail=True))
    auth["client"].get("/auth/google/login?resume=1", follow_redirects=False)
    resp = auth["client"].get("/auth/google/callback", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/chat/c-guest?auth_error=google_failed"
    assert auth["chats"].find_one({"chat_id": "c-guest"})["messages"]                # nothing was lost


def test_post_auth_target_rejects_a_conversation_the_account_does_not_own(auth):
    rt = auth["rt"]
    auth["chats"].insert_one({"chat_id": "c-other", "owner_key": "someone@else.example", "title": "x", "messages": []})
    req = type("R", (), {"session": {}})()
    assert rt._post_auth_target(req, "c-other", "me@example.com") == "/"
    assert rt._post_auth_target(req, None, "me@example.com") == "/"
    assert "active_chat_id" not in req.session


# ───────────────────── 6. template wiring ───────────────────────────────────
def _read(name):
    return (ROOT / "templates" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["index.html", "chat.html"])
def test_both_templates_carry_the_same_gate_assets(name):
    t = _read(name)
    for marker in ("GUEST-GATE-CSS-BEGIN", "GUEST-GATE-LIMIT-MODAL-BEGIN", "GUEST-GATE-JS-BEGIN", 'id="auth-overlay"',
                   'id="limit-overlay"', 'name="resume"', 'id="auth-google-link"', 'role="dialog"'):
        assert marker in t, (name, marker)
    for copy in ("Your next insight starts here.", "Create your free account", "Your current search and its answer will be preserved.",
                 "You've reached your search limit.", "View upgrade options", "Maybe later"):
        assert copy in t, (name, copy)
    assert 'href="/billing/upgrade"' in t and "/api/me/usage" in t
    assert t.count("flintelSearchResponseAction(response) === 'repost'") == t.count("window.flintelGate.handleDenial(response)")


def _block(t, begin, end):
    a, b = t.index(begin), t.index(end)
    return t[a:b]


def test_gate_css_modal_and_script_are_identical_in_both_templates():
    a, b = _read("index.html"), _read("chat.html")
    for begin, end in (("/* GUEST-GATE-CSS-BEGIN", "/* GUEST-GATE-CSS-END */"),
                       ("<!-- GUEST-GATE-LIMIT-MODAL-BEGIN", "<!-- GUEST-GATE-LIMIT-MODAL-END -->"),
                       ("/* GUEST-GATE-JS-BEGIN", "/* GUEST-GATE-JS-END */")):
        assert _block(a, begin, end) == _block(b, begin, end)


def test_the_overlays_live_outside_main_so_soft_navigation_cannot_remove_them():
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.parents = [], {}

        def handle_starttag(self, tag, attrs):
            if tag in ("meta", "link", "img", "input", "br", "hr", "path"):
                return
            i = dict(attrs).get("id")
            if i in ("auth-overlay", "limit-overlay", "guest-gate-marker"):
                self.parents[i] = list(self.stack)
            self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
    for name in ("index.html", "chat.html"):
        p = P()
        p.feed(_read(name))
        assert "main" not in p.parents["auth-overlay"] and "main" not in p.parents["limit-overlay"], name
        assert "main" in p.parents["guest-gate-marker"], name                      # the marker travels with the page content


# ───────────────────── 7. the modals in real Chromium ───────────────────────
try:
    import jinja2
    from playwright.sync_api import sync_playwright  # noqa: F401
    HAVE_BROWSER = True
except Exception:          # pragma: no cover
    HAVE_BROWSER = False

needs_browser = pytest.mark.skipif(not HAVE_BROWSER, reason="jinja2/playwright unavailable")


def render(name, **ctx):
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.ChainableUndefined)
    e.globals["url_for"] = lambda *a, **k: "#"
    base = dict(request=None, chats=[], user=None, chat_id=None, chat=None, pending_stream_topic_key=None,
                messages=[], plan_label="Free")
    base.update(ctx)
    return e.get_template(name).render(**base)


CHAT = {"chat_id": "c1", "title": "T", "messages": [], "platform": "All Platforms"}


@pytest.fixture
def page(integrity_browser):
    ctx = integrity_browser.new_context(viewport={"width": 1200, "height": 800})
    pg = ctx.new_page()
    pg.route("**/*", lambda route: route.abort())                                  # no CDN / network: the page must work alone
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.errors = errors
    yield pg
    ctx.close()


ORIGIN = "http://flintel.test"


def load(pg, html_text):
    """Serves the rendered template from a real (fake) origin so relative fetches like /api/me/usage resolve."""
    pg.route(ORIGIN + "/__page__", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html_text))
    pg.goto(ORIGIN + "/__page__", wait_until="domcontentloaded")
    pg.wait_for_function("window.flintelGate !== undefined")


def fake(headers):
    return "{headers:{get:(k)=>(%s)[k]||null}}" % json.dumps(headers)


def visible(pg, sel):
    return pg.evaluate("(s)=>{const e=document.querySelector(s);return !!e&&e.classList.contains('visible')}", sel)


@needs_browser
@pytest.mark.parametrize("name,ctx", [("index.html", {}), ("chat.html", {"chat": CHAT, "chat_id": "c1"})])
def test_classification_uses_the_server_headers_not_the_status_code(page, name, ctx):
    load(page, render(name, **ctx))
    cases = [
        ({"X-Flintel-Denial": "guest-auth-required"}, "guest-auth"),
        ({"X-Flintel-Denial": "credits-exhausted", "X-Flintel-Audience": "guest"}, "guest-auth"),
        ({"X-Flintel-Denial": "credits-exhausted"}, "credit-limit"),
        ({"X-Flintel-Denial": "guest-search-in-progress"}, None),
        ({"X-Flintel-Denial": "guest-gate-unavailable"}, None),
        ({"X-Flintel-Denial": "guest-identity-required"}, None),
        ({"X-Flintel-Denial": "credit-store-unavailable"}, None),
        ({}, None),
    ]
    for headers, expected in cases:
        assert page.evaluate(f"window.flintelGate.denialKind({fake(headers)})") == expected, headers
    assert page.evaluate("window.flintelGate.denialKind(null)") is None
    assert page.evaluate("window.flintelGate.denialKind({})") is None
    assert page.errors == []


@needs_browser
@pytest.mark.parametrize("name,ctx", [("index.html", {}), ("chat.html", {"chat": CHAT, "chat_id": "c1"})])
def test_guest_denial_opens_only_the_signup_modal_with_the_required_copy(page, name, ctx):
    load(page, render(name, **ctx))
    assert not visible(page, "#auth-overlay") and not visible(page, "#limit-overlay")
    page.evaluate(f"window.flintelGate.handleDenial({fake({'X-Flintel-Denial': 'guest-auth-required'})})")
    assert visible(page, "#auth-overlay") and not visible(page, "#limit-overlay")
    dlg = page.locator("#auth-overlay [role=dialog]")
    text = dlg.inner_text()
    assert "Your next insight starts here." in text and "You've used your free search." in text
    assert "Create your free account" in text and "Your current search and its answer will be preserved." in text
    assert "Sign in" in text
    assert page.locator("#auth-apple-btn").is_hidden()                              # no non-working provider button
    assert page.get_attribute("#auth-google-link", "href") == "/auth/google/login?resume=1"
    assert page.eval_on_selector_all("#auth-overlay input[name=resume]", "els=>els.map(e=>e.value)") == ["1", "1"]
    assert dlg.get_attribute("aria-modal") == "true" and dlg.get_attribute("aria-labelledby") == "auth-gate-title"
    assert page.evaluate("document.getElementById('auth-gate-title').textContent") == "Your next insight starts here."
    assert page.errors == []


@needs_browser
def test_signed_in_credit_denial_opens_only_the_upgrade_modal(page):
    load(page, render("index.html", user={"email": "m@example.com", "name": "M"}))
    page.evaluate(f"window.flintelGate.handleDenial({fake({'X-Flintel-Denial': 'credits-exhausted'})})")
    assert visible(page, "#limit-overlay") and not visible(page, "#auth-overlay")
    text = page.locator("#limit-overlay [role=dialog]").inner_text()
    assert "You've reached your search limit." in text and "View upgrade options" in text and "Maybe later" in text
    assert page.get_attribute("#limit-cta", "href") == "/billing/upgrade"
    assert "Create your free account" not in text
    page.click("#limit-later-btn")
    assert not visible(page, "#limit-overlay")


@needs_browser
def test_unrelated_denials_open_no_modal(page):
    load(page, render("index.html"))
    for h in ({"X-Flintel-Denial": "guest-search-in-progress"}, {"X-Flintel-Denial": "guest-gate-unavailable"},
              {"X-Flintel-Denial": "credit-store-unavailable"}, {}):
        page.evaluate(f"window.flintelGate.handleDenial({fake(h)})")
    assert not visible(page, "#auth-overlay") and not visible(page, "#limit-overlay")


@needs_browser
def test_limit_modal_shows_only_real_usage_figures(page):
    page.route("**/api/me/usage", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(
        {"plan": "Free", "credits": {"status": "ok", "used": 3, "limit": 3, "remaining": 0, "resets_at": "2026-11-01T00:00:00+00:00"}})))
    load(page, render("index.html", user={"email": "m@example.com"}))
    page.evaluate("window.flintelGate.openLimit()")
    page.wait_for_function("!document.getElementById('limit-facts').hidden")
    facts = page.locator("#limit-facts").inner_text()
    assert "3 of 3" in facts and "Free" in facts and "reset" in facts.lower()


@needs_browser
def test_limit_modal_invents_nothing_when_usage_is_unavailable(page):
    page.route("**/api/me/usage", lambda route: route.fulfill(status=500, body="boom"))
    load(page, render("index.html", user={"email": "m@example.com"}))
    page.evaluate("window.flintelGate.openLimit()")
    page.wait_for_timeout(200)
    assert page.locator("#limit-facts").is_hidden() and visible(page, "#limit-overlay")


@needs_browser
def test_answer_first_then_the_modal_and_closing_never_unlocks_anything(page):
    load(page, render("chat.html", chat=CHAT, chat_id="c1"))
    page.evaluate("window.flintelGate.afterAnswer({done: true})")                  # no flag: nothing opens
    page.wait_for_timeout(900)
    assert not visible(page, "#auth-overlay")
    page.evaluate("window.flintelGate.afterAnswer({done: true, guest_gate: 'auth_required'})")
    page.wait_for_timeout(300)
    assert not visible(page, "#auth-overlay")                                      # the answer gets a moment first
    page.wait_for_selector("#auth-overlay.visible", timeout=2000)
    page.keyboard.press("Escape")
    assert not visible(page, "#auth-overlay")
    assert page.evaluate("document.getElementById('auth-overlay').classList.contains('gate-mode')") is False
    page.evaluate(f"window.flintelGate.handleDenial({fake({'X-Flintel-Denial': 'guest-auth-required'})})")
    assert visible(page, "#auth-overlay")                                          # the next attempt reopens it


@needs_browser
def test_server_rendered_marker_reopens_the_modal_after_a_refresh(page):
    load(page, render("chat.html", chat=CHAT, chat_id="c1", guest_gate_state="auth_required"))
    page.wait_for_selector("#auth-overlay.visible", timeout=2000)
    assert "Your next insight starts here." in page.locator("#auth-overlay [role=dialog]").inner_text()
    load(page, render("chat.html", chat=CHAT, chat_id="c1", guest_gate_state=None))
    page.wait_for_timeout(600)
    assert not visible(page, "#auth-overlay")


@needs_browser
def test_denial_page_markers_pick_the_right_modal(page):
    load(page, render("index.html", denial="guest-auth-required", error="x"))
    page.wait_for_selector("#auth-overlay.visible", timeout=2000)
    load(page, render("index.html", denial="credits-exhausted", error="x", user={"email": "m@example.com"}))
    page.wait_for_selector("#limit-overlay.visible", timeout=2000)
    assert not visible(page, "#auth-overlay")
    load(page, render("index.html", denial="guest-search-in-progress", error="x"))
    page.wait_for_timeout(600)
    assert not visible(page, "#auth-overlay") and not visible(page, "#limit-overlay")


@needs_browser
def test_dialog_is_accessible_focus_moves_in_is_trapped_and_returns(page):
    load(page, render("chat.html", chat=CHAT, chat_id="c1"))
    page.focus("#search-input")
    page.evaluate(f"window.flintelGate.handleDenial({fake({'X-Flintel-Denial': 'guest-auth-required'})})")
    page.wait_for_function("document.getElementById('auth-overlay').contains(document.activeElement)")
    for _ in range(14):                                                             # Tab never leaves the dialog
        page.keyboard.press("Tab")
        assert page.evaluate("document.getElementById('auth-overlay').contains(document.activeElement)")
    for _ in range(14):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("document.getElementById('auth-overlay').contains(document.activeElement)")
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement && document.activeElement.id") == "search-input"
    for lab in ("login-email", "signup-email", "signup-password", "signup-confirm-password"):
        assert page.locator(f"label[for={lab}]").count() == 1


@needs_browser
def test_gate_mode_form_posts_json_and_follows_only_a_local_redirect(page):
    seen = {}

    def signup(route):
        seen["accept"] = route.request.headers.get("accept")
        seen["body"] = route.request.post_data
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "redirect": "//evil.example/x"}))
    page.route("**/signup", signup)
    load(page, render("index.html"))
    page.evaluate(f"window.flintelGate.handleDenial({fake({'X-Flintel-Denial': 'guest-auth-required'})})")
    page.fill("#signup-email", "a@example.com")
    page.fill("#signup-password", "correct-horse-1")
    page.fill("#signup-confirm-password", "correct-horse-1")
    page.click("#signup-form button[type=submit]")
    page.wait_for_function("!document.querySelector('#signup-form button[type=submit]').classList.contains('is-busy')")
    assert seen["accept"] == "application/json" and re.search(r'name="resume"\r\n\r\n1', seen["body"])
    assert "evil.example" not in page.url and page.url.endswith("/__page__")        # a protocol-relative redirect is refused
    assert page.locator("#auth-error").is_visible()
    assert page.evaluate("window.flintelGate.safeRedirect('//evil.example')") is False
    assert page.evaluate("window.flintelGate.safeRedirect('/chat/abc')") is True
    assert page.evaluate("window.flintelGate.safeRedirect('https://evil.example')") is False


@needs_browser
def test_the_normal_sign_in_modal_is_unchanged_and_not_in_gate_mode(page):
    load(page, render("index.html"))
    page.evaluate("document.getElementById('signin-btn') && document.getElementById('signin-btn').click()")
    if not visible(page, "#auth-overlay"):
        page.evaluate("window.flintelGate.closeGuest(); document.getElementById('auth-overlay').classList.add('visible')")
    assert page.evaluate("document.getElementById('auth-overlay').classList.contains('gate-mode')") is False
    assert page.get_attribute("#auth-google-link", "href") == "/auth/google/login"
    assert page.locator("#auth-gate-title").is_hidden() and page.locator("#auth-gate-note").is_hidden()


FAKE_STREAM = """
(() => {
  class FakeEventSource {
    constructor(url) {
      this.url = url; this.readyState = 1; window.__streamUrls = (window.__streamUrls || []).concat([url]);
      const send = (obj, ms) => setTimeout(() => { if (this.readyState !== 2 && this.onmessage) this.onmessage({data: JSON.stringify(obj)}); }, ms);
      send({delta: %(answer)s}, 50);
      send(Object.assign({done: true, results: []}, %(extra)s), 150);
    }
    close() { this.readyState = 2; }
    addEventListener() {}
  }
  window.EventSource = FakeEventSource;
})();
"""


def _pending_chat():
    return {"chat_id": "c1", "title": "T", "platform": "All Platforms", "messages": [{
        "query": "Find users looking for AI jobs", "topic_key": "tk1", "claude_answer": None, "message_type": "search",
        "requested_at": dt.datetime(2026, 10, 20, 12, 0, 0)}]}


@needs_browser
@pytest.mark.parametrize("flag,expect_modal", [({"guest_gate": "auth_required"}, True), ({}, False)])
def test_streamed_answer_is_on_screen_before_the_signup_modal(page, flag, expect_modal):
    page.add_init_script(FAKE_STREAM % {"answer": json.dumps(json.dumps(GOOD)), "extra": json.dumps(flag)})
    load(page, render("chat.html", chat=_pending_chat(), chat_id="c1", pending_stream_topic_key="tk1"))
    page.wait_for_selector(".answer-wrap", timeout=5000)                             # the answer is rendered ...
    assert not visible(page, "#auth-overlay")                                         # ... while no modal is open yet
    if expect_modal:
        page.wait_for_selector("#auth-overlay.visible", timeout=4000)                 # ... and only then the sign-up modal
        assert page.locator(".answer-wrap").count() == 1                              # the answer stays behind it
        assert "Your next insight starts here." in page.locator("#auth-overlay [role=dialog]").inner_text()
    else:
        page.wait_for_timeout(1200)
        assert not visible(page, "#auth-overlay")
    assert page.errors == []


# ══════════════════ HARDENING PASS ═════════════════════════════════════════════════════════════════
# 1. stale-reservation race ────────────────────────────────────────────────────────────────────────
def _takeover(gg, coll, clock, stale=900):
    """A reserved, went stale, B took over. Returns the gate (A = 'tok-a', B = 'tok-b')."""
    gate = make_gate(gg, coll, clock, stale=stale)
    assert gate.reserve("g1", "tok-a", "topic", "chat-a").kind == gg.ALLOW
    clock.advance(stale + 1)
    taken = gate.reserve("g1", "tok-b", "topic", "chat-b")
    assert taken.kind == gg.ALLOW and taken.reserved and not taken.resumed
    return gate


def test_race_late_completion_of_the_old_search_cannot_complete_or_free_the_newer_reservation(gg, coll, clock):
    gate = _takeover(gg, coll, clock)
    assert gate.complete("g1", "tok-a", "chat-a", "topic") == "superseded"
    doc = coll.find_one({"_id": "g1"})
    assert doc["completed"] == 0                                              # nothing counted for the old worker
    assert doc["inflight"]["token"] == "tok-b" and doc["inflight"]["chat_id"] == "chat-b"   # B's slot untouched
    assert gate.reserve("g1", "tok-c").kind == gg.BUSY                        # B is still protected
    assert gate.complete("g1", "tok-b", "chat-b", "topic") == "completed"     # B is the one that counts
    final = coll.find_one({"_id": "g1"})
    assert final["completed"] == 1 and final["last_chat_id"] == "chat-b" and final["inflight"] is None


def test_race_late_completion_after_the_newer_search_already_counted_is_not_a_second_count(gg, coll, clock):
    gate = _takeover(gg, coll, clock)
    assert gate.complete("g1", "tok-b", "chat-b", "topic") == "completed"
    assert gate.complete("g1", "tok-a", "chat-a", "topic") == "superseded"
    doc = coll.find_one({"_id": "g1"})
    assert doc["completed"] == 1 and doc["last_chat_id"] == "chat-b"          # no double count, resume target not overwritten
    assert gate.reserve("g1", "tok-d").kind == gg.AUTH_REQUIRED


def test_race_late_release_of_the_old_search_leaves_the_newer_reservation_alone(gg, coll, clock):
    gate = _takeover(gg, coll, clock)
    assert gate.release("g1", "tok-a") is False
    assert coll.find_one({"_id": "g1"})["inflight"]["token"] == "tok-b"
    assert gate.reserve("g1", "tok-c").kind == gg.BUSY


def test_race_recovery_then_new_reservation_then_every_late_event_from_the_old_worker(gg, coll, clock):
    gate = _takeover(gg, coll, clock)
    before = coll.find_one({"_id": "g1"})
    for _ in range(3):                                                        # the old worker keeps trying
        assert gate.complete("g1", "tok-a", "chat-a", "topic") == "superseded"
        assert gate.release("g1", "tok-a") is False
        assert gate.reserve("g1", "tok-a", "topic", "chat-a").kind == gg.BUSY  # and cannot re-take the slot
    after = coll.find_one({"_id": "g1"})
    assert after["completed"] == before["completed"] == 0 and after["inflight"] == before["inflight"]


def test_race_old_search_finishing_before_anyone_took_over_still_counts(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    gate.reserve("g1", "tok-a", "topic", "chat-a")
    clock.advance(5000)                                                       # stale, but nobody recovered it yet
    assert gate.complete("g1", "tok-a", "chat-a", "topic") == "completed"
    assert coll.find_one({"_id": "g1"})["completed"] == 1


def test_race_retry_of_a_released_search_counts_when_the_slot_is_free_but_not_when_a_newer_one_holds_it(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    gate.reserve("g1", "tok-a", "topic", "chat-a")
    gate.release("g1", "tok-a")                                               # first attempt errored
    assert gate.complete("g1", "tok-a", "chat-a", "topic") == "completed"      # the automatic retry succeeded
    gate.reserve("g2", "tok-a", "topic", "chat-a")
    gate.release("g2", "tok-a")
    gate.reserve("g2", "tok-b", "topic", "chat-b")                            # the guest started a new search meanwhile
    assert gate.complete("g2", "tok-a", "chat-a", "topic") == "superseded"
    assert coll.find_one({"_id": "g2"})["inflight"]["token"] == "tok-b"


def test_race_two_finishers_at_the_same_instant_count_at_most_once(gg, clock):
    for round_no in range(25):
        c = mongomock.MongoClient()[f"race{round_no}"]["g"]
        gate = make_gate(gg, c, clock)
        gate.reserve("g1", "tok-a", "topic", "chat-a")
        # force the overlap: B holds the slot while both A and B finish at once
        c.update_one({"_id": "g1"}, {"$set": {"inflight": {"token": "tok-b", "topic_key": "topic", "chat_id": "chat-b", "started_at": clock()}},
                                     "$push": {"tokens": "tok-b"}})
        results, barrier = {}, threading.Barrier(2)

        def finish(tok):
            barrier.wait()
            results[tok] = gate.complete("g1", tok, f"chat-{tok}", "topic")
        threads = [threading.Thread(target=finish, args=(tk,)) for tk in ("tok-a", "tok-b")]
        [x.start() for x in threads]
        [x.join() for x in threads]
        doc = c.find_one({"_id": "g1"})
        assert doc["completed"] == 1, (round_no, results)
        assert results["tok-b"] == "completed" and results["tok-a"] == "superseded"


def test_completion_never_exceeds_the_limit_even_for_tokens_that_never_overlapped(gg, coll, clock):
    gate = make_gate(gg, coll, clock, limit=1)
    gate.reserve("g1", "tok-a")
    coll.update_one({"_id": "g1"}, {"$push": {"tokens": "tok-x"}})            # an extra, legitimately reserved token
    assert gate.complete("g1", "tok-a", "c1") == "completed"
    assert gate.complete("g1", "tok-x", "c2") == "superseded"
    assert coll.find_one({"_id": "g1"})["completed"] == 1


# ── signing in while a guest search is in flight ───────────────────────────────────────────────────
# 3. answers that must / must not count ────────────────────────────────────────────────────────────
def test_answer_counts_classification(gg):
    assert gg.answer_counts(None) is True                                      # caller did not pass one
    assert gg.answer_counts(json.dumps(GOOD)) is True
    assert gg.answer_counts(json.dumps({"format": "no_results", "message": "Nothing matched."})) is True   # genuine no-results
    assert gg.answer_counts("Plain prose answer") is True
    assert gg.answer_counts(json.dumps({"format": "source_list", "answer_status": "incomplete"})) is False
    assert gg.answer_counts(json.dumps({"format": "source_list", "answer_status": "partial", "headline": "x"})) is False
    assert gg.answer_counts("") is False and gg.answer_counts("   ") is False


def test_pipeline_genuine_no_results_answer_counts_exactly_once(pipeline):
    pipeline["run"](json.dumps({"format": "no_results", "message": "No matching posts were found for this request."}))
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 1 and doc["inflight"] is None


def test_pipeline_truncated_answers_do_not_count(pipeline):
    from tests.test_answer_persistence import CUT_BEFORE_TEXT, CUT_MID_ANSWER
    for n, raw in enumerate((CUT_BEFORE_TEXT, CUT_MID_ANSWER)):
        pipeline["run"](raw, token=f"tok-cut-{n}")
        doc = pipeline["coll"].find_one({"_id": "guest-1"})
        assert doc["completed"] == 0 and doc["inflight"] is None, raw[:30]      # released, free to try again


def test_pipeline_website_only_analysis_counts_once_and_failure_does_not(pipeline, monkeypatch):
    wi = sys.modules["website_intelligence"]
    idx, lg, chats, gg = pipeline["idx"], pipeline["lg"], pipeline["chats"], _gate_mod()

    def run(token):
        q = "https://example.com"
        idx.add_search_to_chat("cw", "guest-1", q, lg.normalize_topic_key(q), ["kw"], "reddit")
        msg = chats.find_one({"chat_id": "cw", "owner_key": "guest-1"})["messages"][-1]
        msg.update(website_only=True, website_evidence={"url": q, "structured_evidence": {}, "evidence_quality": "thin"},
                   requested_at=dt.datetime(2026, 10, 20, 12, 0, 0), request_id=token)
        gg.GuestGate(pipeline["coll"]).reserve("guest-1", token, msg["topic_key"], "cw")
        idx._complete_message_answer_and_results("cw", "guest-1", msg, [])

    def boom(**k):
        raise RuntimeError("model down")
    monkeypatch.setattr(wi, "build_website_insight_answer", boom, raising=False)
    run("tok-w1")
    assert pipeline["coll"].find_one({"_id": "guest-1"})["completed"] == 0
    monkeypatch.setattr(wi, "build_website_insight_answer", lambda **k: json.dumps(GOOD), raising=False)
    run("tok-w2")
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 1 and doc["inflight"] is None


# 2. cookie persistence / fail-closed identity (real middleware stack) ───────────────────────────
_REAL_IDENTITY_CHECK = []


@pytest.fixture(autouse=True)
def _capture_real_identity_check(env):
    """Keeps routes._guest_identity_confirmed as shipped (gapp swaps it out for the legacy tests)."""
    _REAL_IDENTITY_CHECK.clear()
    _REAL_IDENTITY_CHECK.append(importlib.import_module("routes")._guest_identity_confirmed)
    yield


@pytest.fixture
def e2e(gapp, monkeypatch):
    """gapp with the REAL identity resolution (cookie + session), the real middleware and the real auth routes."""
    testclient = pytest.importorskip("starlette.testclient")
    idx, rt = gapp["idx"], gapp["rt"]
    monkeypatch.setattr(rt, "get_owner", idx.get_owner)
    monkeypatch.setattr(rt, "_guest_identity_confirmed", _REAL_IDENTITY_CHECK[0])
    users = gapp["db"]["users"]
    for mod in (idx, rt):
        monkeypatch.setattr(mod, "users_collection", users, raising=False)

    class FakePwd:
        hash = staticmethod(lambda pw: "h:" + pw)
        verify = staticmethod(lambda pw, h: h == "h:" + pw)
    monkeypatch.setattr(rt, "pwd_context", FakePwd)
    gapp["users"] = users
    gapp["new_client"] = lambda scheme="https": testclient.TestClient(idx.app, base_url=f"{scheme}://flintel.test")
    return gapp


def post_search(client, key=KEY1, query="Find people looking for a CRM for small teams"):
    return client.post("/search", data={"query": query, "platform": "All Platforms", "submit_key": key}, follow_redirects=False)


def cookie_value(client):
    return client.cookies.get("fl_gid", domain="flintel.test")


def gid_of(idx, client):
    return idx._read_guest_cookie(type("R", (), {"cookies": {"fl_gid": cookie_value(client)}})())


@pytest.mark.usefixtures("flat_cost")
def test_a_search_without_the_identity_cookie_is_refused_before_anything_is_written(e2e):
    client = e2e["new_client"]()
    resp = post_search(client)                                                 # never loaded a page: no cookie
    assert resp.status_code == 403 and resp.headers["X-Flintel-Denial"] == "guest-identity-required"
    assert "cookies are enabled" in html.unescape(resp.text) and 'id="guest-gate-marker"' not in resp.text
    assert e2e["gate_coll"].count_documents({}) == 0                           # no gate record, no allowance consumed
    assert e2e["state"]["enqueues"] == [] and e2e["db"].flintel_credit_usage.count_documents({}) == 0
    assert e2e["db"].chats.count_documents({"messages": {"$size": 0}}) == 0
    assert cookie_value(client)                                                # the refusal itself issues the cookie ...
    ok = post_search(client, KEY2)                                             # ... so the very next attempt is admitted
    assert ok.status_code == 303 and len(e2e["state"]["enqueues"]) == 1
    assert e2e["gate_coll"].count_documents({}) == 1


@pytest.mark.usefixtures("flat_cost")
def test_a_browser_that_rejects_cookies_never_receives_a_fresh_allowance(e2e):
    for i in range(6):
        client = e2e["new_client"]()                                           # every request looks like a brand-new browser
        resp = post_search(client, f"{i:08d}-aaaa-4bbb-8ccc-000000000001")
        assert resp.status_code == 403 and resp.headers["X-Flintel-Denial"] == "guest-identity-required"
    assert e2e["gate_coll"].count_documents({}) == 0 and e2e["state"]["enqueues"] == []


@pytest.mark.usefixtures("flat_cost")
def test_tampered_or_foreign_cookies_cannot_change_gate_state(e2e):
    client = e2e["new_client"]()
    client.get("/")
    gid = cookie_value(client)
    for bad in ("garbage", gid[:-3] + "AAA", gid + "x", "a" * 500):
        c2 = e2e["new_client"]()
        c2.cookies.set("fl_gid", bad, domain="flintel.test")
        resp = post_search(c2)
        assert resp.status_code == 403 and resp.headers["X-Flintel-Denial"] == "guest-identity-required", bad[:12]
    assert e2e["gate_coll"].count_documents({}) == 0
    c3 = e2e["new_client"]()                                                   # signed, but not a server-made id shape
    c3.cookies.set("fl_gid", e2e["idx"]._guest_cookie_signer.dumps("not-a-32-hex-id"), domain="flintel.test")
    assert post_search(c3).status_code == 403 and e2e["gate_coll"].count_documents({}) == 0


@pytest.mark.usefixtures("flat_cost")
def test_a_guest_whose_identity_cookie_was_lost_keeps_the_same_identity_not_a_new_one(e2e):
    """The session still names the guest: the cookie is re-issued for the SAME id, never a fresh allowance."""
    client = e2e["new_client"]()
    client.get("/")
    before = gid_of(e2e["idx"], client)
    client.cookies.delete("fl_gid", domain="flintel.test")
    assert post_search(client).status_code == 403                              # not admitted on the lost-cookie request
    assert gid_of(e2e["idx"], client) == before                                # same id re-issued
    assert post_search(client, KEY2).status_code == 303                        # and admitted on the next one


def test_identity_survives_refresh_tabs_logout_and_restart_when_cookies_are_retained(e2e):
    idx = e2e["idx"]
    tab1 = e2e["new_client"]()
    tab1.get("/")
    gid = gid_of(idx, tab1)
    assert gid
    tab1.get("/")
    tab1.get("/")                                                              # refreshes
    assert gid_of(idx, tab1) == gid
    tab2 = e2e["new_client"]()                                                 # a second tab shares the browser's jar
    tab2.cookies.set("fl_gid", cookie_value(tab1), domain="flintel.test")
    tab2.get("/")
    assert gid_of(idx, tab2) == gid
    tab1.get("/logout")                                                        # logout drops the session, not the identity
    tab1.get("/")
    assert gid_of(idx, tab1) == gid
    restarted = e2e["new_client"]()                                            # restart: only persistent cookies remain
    restarted.cookies.set("fl_gid", cookie_value(tab1), domain="flintel.test")
    restarted.get("/")
    assert gid_of(idx, restarted) == gid
    header = [h for h in e2e["new_client"]().get("/").headers.get_list("set-cookie") if h.startswith("fl_gid=")][0].lower()
    assert f"max-age={400 * 86400}" in header                                  # persistent, not a session cookie


@pytest.mark.parametrize("scheme,forwarded,secure", [
    ("https", None, True), ("http", "https", True), ("http", "https, http", True),
    ("http", None, False), ("http", "http", False), ("https", "http", False)])
def test_secure_flag_follows_the_scheme_the_visitor_used(e2e, scheme, forwarded, secure):
    headers = {"X-Forwarded-Proto": forwarded} if forwarded else {}
    resp = e2e["new_client"](scheme).get("/", headers=headers)
    header = [h for h in resp.headers.get_list("set-cookie") if h.startswith("fl_gid=")][0].lower()
    assert ("secure" in [p.strip() for p in header.split(";")]) is secure
    assert "httponly" in header and "samesite=lax" in header


# 4. authentication while a search is in flight / failed auth / duplicate callbacks ───────────────
def _guest_search(e2e):
    client = e2e["new_client"]()
    client.get("/")
    resp = post_search(client)
    assert resp.status_code == 303, resp.text[:200]
    return client, resp.headers["location"].rsplit("/", 1)[-1]


@pytest.mark.usefixtures("flat_cost")
def test_email_login_after_a_completed_guest_search_resumes_it_and_duplicates_are_safe(e2e):
    e2e["users"].insert_one({"email": "back@example.com", "password_hash": "h:correct-horse-1", "provider": "email"})
    client, chat_id = _guest_search(e2e)
    guest_id = gid_of(e2e["idx"], client)
    token = e2e["gate_coll"].find_one({"_id": guest_id})["inflight"]["token"]
    e2e["gg"].on_answer_complete(guest_id, chat_id, "t", token, json.dumps(GOOD))
    for _ in range(2):                                                                  # a duplicate submit
        resp = client.post("/login", data={"email": "back@example.com", "password": "correct-horse-1", "resume": "1"},
                           headers={"Accept": "application/json"})
        assert resp.json() == {"ok": True, "redirect": f"/chat/{chat_id}"}
    assert e2e["db"].chats.count_documents({"chat_id": chat_id}) == 1
    assert e2e["db"].chats.find_one({"chat_id": chat_id})["owner_key"] == "back@example.com"
    assert len(e2e["state"]["enqueues"]) == 1 and e2e["gate_coll"].find_one({"_id": guest_id})["completed"] == 1


@pytest.mark.usefixtures("flat_cost")
def test_failed_authentication_leaves_the_guest_conversation_and_allowance_untouched(e2e):
    client, chat_id = _guest_search(e2e)
    guest_id = gid_of(e2e["idx"], client)
    before = e2e["gate_coll"].find_one({"_id": guest_id})
    bad_login = client.post("/login", data={"email": "nobody@example.com", "password": "wrong-password-1", "resume": "1"},
                            headers={"Accept": "application/json"})
    bad_signup = client.post("/signup", data={"email": "x@example.com", "password": "short", "confirm_password": "short", "resume": "1"},
                             headers={"Accept": "application/json"})
    mismatch = client.post("/signup", data={"email": "x@example.com", "password": "correct-horse-1",
                                            "confirm_password": "different-horse-1", "resume": "1"},
                           headers={"Accept": "application/json"})
    assert [r.json()["ok"] for r in (bad_login, bad_signup, mismatch)] == [False, False, False]
    assert e2e["db"].chats.find_one({"chat_id": chat_id})["owner_key"] == guest_id     # still the guest's
    assert e2e["gate_coll"].find_one({"_id": guest_id}) == before                       # gate record unchanged


# 3. retries, refreshes and entry points ───────────────────────────────────────────────────────────
@pytest.mark.usefixtures("flat_cost")
def test_same_key_retry_of_an_in_flight_search_enqueues_nothing_and_returns_to_the_original_chat(gapp):
    first = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    token = last_token(gapp)
    for _ in range(3):                                                            # double click / network retry / refresh-resubmit
        again = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
        assert again.status_code == 303 and chat_of(again) == chat_of(first)
    assert len(gapp["state"]["enqueues"]) == 1                                    # no duplicate work
    assert gapp["db"].flintel_credit_usage.find_one({"owner_key": "guest-1"})["used_by_period"] and used_for(gapp) == 1
    chat = gapp["db"].chats.find_one({"chat_id": chat_of(first)})
    assert len(chat["messages"]) == 1                                             # no duplicate message / answer
    assert gate_doc(gapp)["inflight"]["token"] == token and gate_doc(gapp)["completed"] == 0


def used_for(a, owner="guest-1"):
    doc = a["db"].flintel_credit_usage.find_one({"owner_key": owner}) or {}
    return sum(int(v) for v in (doc.get("used_by_period") or {}).values())


@pytest.mark.usefixtures("flat_cost")
def test_same_key_retry_after_completion_recovers_the_original_answer_instead_of_the_signup_denial(gapp):
    first = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    gapp["gg"].on_answer_complete("guest-1", chat_of(first), "t", last_token(gapp), json.dumps(GOOD))
    again = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert again.status_code == 303 and chat_of(again) == chat_of(first)          # back to the finished conversation
    assert len(gapp["state"]["enqueues"]) == 1 and used_for(gapp) == 1
    new = gapp["send"]("Find churn complaints about Notion", submit_key=KEY2)     # a different request IS gated
    assert new.status_code == 403 and new.headers["X-Flintel-Denial"] == "guest-auth-required"


@pytest.mark.usefixtures("flat_cost")
def test_legacy_path_without_a_credit_store_is_gated_too(gapp, monkeypatch):
    rt = gapp["rt"]
    queued = []
    monkeypatch.setattr(rt, "_credit_store", lambda: None)
    monkeypatch.setattr(rt, "enqueue_search_job", lambda tk, kws, plat: queued.append(tk))
    first = gapp["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert first.status_code == 303 and len(queued) == 1
    assert gate_doc(gapp)["inflight"]["chat_id"] == chat_of(first)
    busy = gapp["send"]("Find churn complaints about Notion", submit_key=KEY2)
    assert busy.status_code == 409 and busy.headers["X-Flintel-Denial"] == "guest-search-in-progress" and len(queued) == 1
    gapp["gg"].on_answer_complete("guest-1", chat_of(first), "t", last_token(gapp), json.dumps(GOOD))
    denied = gapp["send"]("Find churn complaints about Notion", submit_key="44444444-aaaa-4bbb-8ccc-000000000004")
    assert denied.status_code == 403 and denied.headers["X-Flintel-Denial"] == "guest-auth-required" and len(queued) == 1
    assert gapp["db"].chats.count_documents({"owner_key": "guest-1", "messages": {"$size": 0}}) == 0


@pytest.mark.usefixtures("flat_cost")
def test_refreshing_the_chat_page_of_an_in_flight_search_queues_nothing_and_keeps_the_reservation(e2e):
    client, chat_id = _guest_search(e2e)
    guest_id = gid_of(e2e["idx"], client)
    before = e2e["gate_coll"].find_one({"_id": guest_id})
    jobs_before = e2e["db"].jobs.count_documents({})
    for _ in range(3):
        page = client.get(f"/chat/{chat_id}")
        assert page.status_code == 200 and "Find people looking for a CRM" in html.unescape(page.text)
    assert e2e["db"].jobs.count_documents({}) == jobs_before == 1 and len(e2e["state"]["enqueues"]) == 1
    assert e2e["gate_coll"].find_one({"_id": guest_id}) == before
    assert 'id="guest-gate-marker"' not in page.text                              # still running: no modal yet


@pytest.mark.usefixtures("flat_cost")
def test_a_stranger_cannot_open_or_stream_a_guests_conversation(e2e):
    client, chat_id = _guest_search(e2e)
    stranger = e2e["new_client"]()
    stranger.get("/")
    assert stranger.get(f"/chat/{chat_id}", follow_redirects=False).status_code in (302, 303, 307, 404)
    assert e2e["gate_coll"].count_documents({}) == 1


# structural guards: a future entry point cannot silently skip the gate ───────────────────────────
def _functions(path):
    import ast
    src = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    top = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in top:
        calls = {c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", None)
                 for c in ast.walk(fn) if isinstance(c, ast.Call)}
        yield fn.name, calls, ast.get_source_segment(src, fn) or ""


def test_every_function_that_creates_a_search_goes_through_the_gate():
    found = {}
    for name, calls, source in _functions("routes.py"):
        if "add_search_to_chat" in calls or "enqueue_search_job" in calls:
            found[name] = ("_guest_reserve" in calls) or ("_run_credited_search" in calls)
    assert found, "expected to find the search entry points"
    assert all(found.values()), f"search entry point(s) without the guest gate: {[n for n, ok in found.items() if not ok]}"
    assert "search" in found and "_run_credited_search" in found


def test_every_function_that_saves_an_answer_reports_to_the_gate():
    allowed_without = {"save_claude_answer_to_chat"}          # the storage helper itself
    missing = []
    for path in ("routes.py", "index.py", "logics.py"):
        for name, calls, source in _functions(path):
            if "save_claude_answer_to_chat" in calls and name not in allowed_without and "guest_gate" not in source:
                missing.append(f"{path}:{name}")
    assert missing == [], f"answer-producing function(s) that never tell the guest gate: {missing}"


def test_secure_flag_is_set_when_the_public_origin_is_https_even_if_the_proxy_sends_no_forwarding_header(e2e, monkeypatch):
    monkeypatch.setattr(sys.modules["config"], "SITE_URL", "https://app.flintel.example", raising=False)
    resp = e2e["new_client"]("http").get("/")                                     # app sees plain http, no X-Forwarded-Proto
    header = [h for h in resp.headers.get_list("set-cookie") if h.startswith("fl_gid=")][0].lower()
    assert "secure" in [p.strip() for p in header.split(";")]
    monkeypatch.setattr(sys.modules["config"], "SITE_URL", "", raising=False)
    resp = e2e["new_client"]("http").get("/")
    header = [h for h in resp.headers.get_list("set-cookie") if h.startswith("fl_gid=")][0].lower()
    assert "secure" not in [p.strip() for p in header.split(";")]
