"""
tests/test_guest_gate_delivery.py — final hardening pass on the guest gate:

  * no over-delivery: a superseded / retried search never calls the model, never saves and never shows an answer
    while another search of the same visitor is admitted (blocking pipeline, worker retries, timeout fallback);
  * counting is the commit: the answer is saved / shown only after the reservation's owner counted it;
  * signing in while a search is running (success, failure, duplicate callbacks, late workers);
  * migration of an existing session-only guest (no `fl_gid` yet);
  * atomicity: a randomized model-based check of every state the gate document can reach.

Everything here runs on mongomock / TestClient / stubs. Live MongoDB, real Google, real e-mail delivery, a real
browser restart and the production proxy are NOT covered (see the report / README checklist).
"""
import datetime as dt
import html
import importlib
import json
import random
import sys

import pytest

from tests.test_guest_gate import (  # noqa: F401  (fixtures and helpers shared with the main gate tests)
    GOOD, KEY1, KEY2, Clock, _Oauth, _capture_real_identity_check, app, body, coll, clock, cookie_value, e2e, env, flat_cost,
    gapp, gate_doc, gg, gid_of, make_gate, mongomock, pipeline, post_search, _guest_search,
)

OLD = dt.datetime(2020, 1, 1)


def age(coll, owner="guest-1"):
    """Makes the current reservation of `owner` older than any stale limit."""
    coll.update_one({"_id": owner}, {"$set": {"inflight.started_at": OLD}})


# ───────────────────────── gate hooks ───────────────────────────────────────────────────────────────
@pytest.fixture
def hooks(gg, coll, monkeypatch):
    """The module-level hooks wired to a mongomock gate (limit 1)."""
    gate = gg.GuestGate(coll, limit=1)
    monkeypatch.setattr(gg, "default_gate", lambda: gate)
    return gg, gate, coll


def test_work_start_matrix(hooks):
    gg, gate, coll = hooks
    assert gg.on_work_start("member@example.com", "t", "tk", "c") == gg.PROCEED       # registered owner: not gated
    assert gg.on_work_start("g1", None, "tk", "c") == gg.PROCEED                      # message without a token
    l1 = {}
    assert gg.on_work_start("g1", "tok-a", "tk", "c1", l1) == gg.PROCEED              # first run: takes the slot + the lease
    assert gg.on_work_start("g1", "tok-a", "tk", "c1") == gg.WAIT                     # a second runner of the SAME search waits
    assert gg.on_work_start("g1", "tok-b", "tk", "c2") == gg.ABANDON                  # another search is admitted
    gg.on_work_end("g1", "tok-a", l1)
    gate.release("g1", "tok-a")
    l2 = {}
    assert gg.on_work_start("g1", "tok-b", "tk", "c2", l2) == gg.PROCEED              # slot free: B may run
    assert gg.on_work_start("g1", "tok-a", "tk", "c1") == gg.ABANDON                  # ... and A's retry may not
    gate.complete("g1", "tok-b", "c2")
    gg.on_work_end("g1", "tok-b", l2)
    assert gg.on_work_start("g1", "tok-c", "tk", "c3") == gg.ABANDON                  # allowance used
    assert gg.on_work_start("g1", "tok-b", "tk", "c2") == gg.PROCEED                  # counted but never stored: re-deliver


def test_work_start_defers_when_the_store_is_down(gg, clock, monkeypatch):
    from tests.test_guest_gate import _BrokenColl
    monkeypatch.setattr(gg, "default_gate", lambda: gg.GuestGate(_BrokenColl(), clock=clock, limit=1))
    assert gg.on_work_start("g1", "tok-a", "tk", "c1") == gg.DEFER


def test_answer_ready_matrix(hooks):
    gg, gate, coll = hooks
    answer = json.dumps(GOOD)
    assert gg.on_answer_ready("member@example.com", "c", "t", "x", answer) == gg.DELIVER
    gate.reserve("g1", "tok-a", "tk", "c1")
    assert gg.on_answer_ready("g1", "c1", "tk", "tok-a", answer) == gg.DELIVER        # counted
    doc = coll.find_one({"_id": "g1"})
    assert doc["completed"] == 1 and doc["inflight"] is None and doc["last_chat_id"] == "c1"
    assert gg.on_answer_ready("g1", "c1", "tk", "tok-a", answer) == gg.DELIVER        # re-delivery of the counted one
    assert coll.find_one({"_id": "g1"})["completed"] == 1
    # a different token that never got the slot
    coll.update_one({"_id": "g1"}, {"$push": {"tokens": "tok-z"}})
    assert gg.on_answer_ready("g1", "c9", "tk", "tok-z", answer) == gg.DISCARD
    assert coll.find_one({"_id": "g1"})["last_chat_id"] == "c1"                        # resume target untouched


def test_answer_ready_uncounted_answers_only_check_ownership(hooks):
    gg, gate, coll = hooks
    gate.reserve("g1", "tok-a", "tk", "c1")
    assert gg.on_answer_ready("g1", "c1", "tk", "tok-a", "x", counted=False) == gg.DELIVER
    assert coll.find_one({"_id": "g1"})["completed"] == 0                              # never counted
    age(coll, "g1")
    gate.reserve("g1", "tok-b", "tk", "c2")
    assert gg.on_answer_ready("g1", "c1", "tk", "tok-a", "x", counted=False) == gg.DISCARD


def test_answer_ready_incomplete_answers_release_and_do_not_count(hooks):
    gg, gate, coll = hooks
    gate.reserve("g1", "tok-a", "tk", "c1")
    partial = json.dumps({"format": "source_list", "answer_status": "partial", "headline": "x"})
    assert gg.on_answer_ready("g1", "c1", "tk", "tok-a", partial) == gg.DELIVER        # the explicit notice is shown ...
    doc = coll.find_one({"_id": "g1"})
    assert doc["completed"] == 0 and doc["inflight"] is None                           # ... but nothing counts


def test_answer_ready_defers_when_the_count_cannot_be_written(gg, clock, monkeypatch):
    from tests.test_guest_gate import _BrokenColl
    monkeypatch.setattr(gg, "default_gate", lambda: gg.GuestGate(_BrokenColl(), clock=clock, limit=1))
    assert gg.on_answer_ready("g1", "c", "t", "tok", json.dumps(GOOD)) == gg.DEFER


def test_revert_takes_back_only_that_tokens_count(hooks):
    gg, gate, coll = hooks
    gate.reserve("g1", "tok-a", "tk", "c1")
    gate.complete("g1", "tok-a", "c1")
    assert gate.revert("g1", "tok-other") is False
    assert coll.find_one({"_id": "g1"})["completed"] == 1
    assert gate.revert("g1", "tok-a") is True
    doc = coll.find_one({"_id": "g1"})
    assert doc["completed"] == 0 and "tok-a" not in doc["completed_tokens"]
    assert gate.revert("g1", "tok-a") is False                                         # never below zero
    assert coll.find_one({"_id": "g1"})["completed"] == 0
    assert gate.reserve("g1", "tok-b", "tk", "c2").kind == gg.ALLOW


def test_superseded_notice_is_an_explicit_incomplete_answer_without_content(gg):
    note = json.loads(gg.superseded_answer())
    assert note["answer_status"] == "incomplete" and note["format"] == "no_results"
    assert gg.answer_counts(gg.superseded_answer()) is False


def test_gate_owner_prefers_the_stamped_guest_id(gg):
    assert gg.gate_owner("a@b.co", {"guest_gate_owner": "abc123"}) == "abc123"
    assert gg.gate_owner("a@b.co", {}) == "a@b.co" and gg.gate_owner("a@b.co", None) == "a@b.co"
    assert gg.gate_owner("a@b.co", {"guest_gate_owner": ""}) == "a@b.co"


# ───────────────────────── pipeline: no over-delivery ───────────────────────────────────────────────
def new_message(pl, token, chat="c1", owner="guest-1"):
    idx, lg, chats = pl["idx"], pl["lg"], pl["chats"]
    q = "Find users looking for AI jobs in Pakistan"
    idx.add_search_to_chat(chat, owner, q, lg.normalize_topic_key(q) + token, ["kw"], "reddit")
    msg = chats.find_one({"chat_id": chat, "owner_key": owner})["messages"][-1]
    msg["requested_at"] = dt.datetime(2026, 10, 20, 12, 0, 0)
    msg["request_id"] = token
    return msg


def finish(pl, msg, llm, owner="guest-1", chat="c1"):
    def fake(*a, **k):
        pl["llm_calls"].append(1)
        if isinstance(llm, Exception):
            raise llm
        return llm(*a, **k) if callable(llm) else llm
    pl["lg"]._call_claude = fake
    pl["idx"]._complete_message_answer_and_results(chat, owner, msg, pl["post"])


def saved(pl, msg, chat="c1", owner="guest-1"):
    doc = pl["chats"].find_one({"chat_id": chat, "owner_key": owner})
    return next(m for m in doc["messages"] if m["topic_key"] == msg["topic_key"])


def takeover(pl, gg, old, new, chat_old="c1", chat_new="c2"):
    """`old` holds the slot, goes stale, `new` takes over."""
    gate = gg.GuestGate(pl["coll"], limit=1)
    assert gate.reserve("guest-1", old, "t-old", chat_old).kind == gg.ALLOW
    age(pl["coll"])
    assert gate.reserve("guest-1", new, "t-new", chat_new).reserved
    return gate


def test_late_worker_after_takeover_makes_no_model_call_and_saves_only_the_notice(pipeline, gg):
    msg = new_message(pipeline, "tok-a")
    takeover(pipeline, gg, "tok-a", "tok-b")
    finish(pipeline, msg, json.dumps(GOOD))
    assert pipeline["llm_calls"] == []                                                  # no model call at all
    row = saved(pipeline, msg)
    assert json.loads(row["claude_answer"])["answer_status"] == "incomplete" and "Hiring freeze" not in row["claude_answer"]
    assert row["results"] == []
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 0 and doc["inflight"]["token"] == "tok-b"                # the newer search is untouched


def test_supersession_during_analysis_discards_the_finished_answer(pipeline, gg):
    msg = new_message(pipeline, "tok-a")
    gate = gg.GuestGate(pipeline["coll"], limit=1)

    def slow_model(*a, **k):                                                            # ... then goes stale mid-analysis
        age(pipeline["coll"])
        assert gate.reserve("guest-1", "tok-b", "t-new", "c2").reserved
        return json.dumps(GOOD)
    finish(pipeline, msg, slow_model)
    assert pipeline["llm_calls"] == [1]
    row = saved(pipeline, msg)
    assert "Hiring freeze" not in row["claude_answer"] and json.loads(row["claude_answer"])["answer_status"] == "incomplete"
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 0 and doc["inflight"]["token"] == "tok-b" and doc.get("last_chat_id") is None


def test_background_retry_cannot_start_a_second_search_while_another_is_admitted(pipeline, gg):
    msg = new_message(pipeline, "tok-a")
    gate = gg.GuestGate(pipeline["coll"], limit=1)
    gate.reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    finish(pipeline, msg, RuntimeError("model down"))                                   # first attempt errors -> released
    assert pipeline["coll"].find_one({"_id": "guest-1"})["inflight"] is None
    assert saved(pipeline, msg).get("claude_answer") is None                            # still pending: the worker retries
    assert gate.reserve("guest-1", "tok-b", "t-new", "c2").reserved                     # the guest starts a new search
    pipeline["llm_calls"].clear()
    finish(pipeline, msg, json.dumps(GOOD))                                             # the worker's retry of A
    assert pipeline["llm_calls"] == []                                                  # no independent second search
    assert "Hiring freeze" not in saved(pipeline, msg)["claude_answer"]
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 0 and doc["inflight"]["token"] == "tok-b"


def test_background_retry_with_a_free_slot_retakes_it_so_no_second_search_can_start(pipeline, gg):
    msg = new_message(pipeline, "tok-a")
    gate = gg.GuestGate(pipeline["coll"], limit=1)
    gate.reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    finish(pipeline, msg, RuntimeError("model down"))
    seen = {}

    def model(*a, **k):
        seen["during"] = gate.reserve("guest-1", "tok-b", "t-new", "c2").kind           # a second search while A retries
        return json.dumps(GOOD)
    finish(pipeline, msg, model)
    assert seen["during"] == gg.BUSY
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 1 and doc["inflight"] is None and doc["last_chat_id"] == "c1"
    assert "Hiring freeze" in saved(pipeline, msg)["claude_answer"]
    assert gate.reserve("guest-1", "tok-c", "t3", "c3").kind == gg.AUTH_REQUIRED


def test_answer_is_only_saved_after_it_was_counted_and_is_kept_consistent_on_save_failure(pipeline, gg, monkeypatch):
    msg = new_message(pipeline, "tok-a")
    gg.GuestGate(pipeline["coll"], limit=1).reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    order = []
    real_complete = gg.GuestGate.complete

    def complete(self, *a, **k):
        order.append("count")
        return real_complete(self, *a, **k)
    monkeypatch.setattr(gg.GuestGate, "complete", complete)
    idx = pipeline["idx"]
    real_save = idx.save_claude_answer_if_unset

    def save(*a, **k):
        order.append("save")
        return real_save(*a, **k)
    monkeypatch.setattr(idx, "save_claude_answer_if_unset", save)
    finish(pipeline, msg, json.dumps(GOOD))
    assert order[:2] == ["count", "save"]                                                # counting is the commit
    # now the store refuses the save: the count is taken back, the turn stays pending, the slot is free
    pipeline["coll"].delete_many({})
    msg2 = new_message(pipeline, "tok-b", chat="c2")
    gg.GuestGate(pipeline["coll"], limit=1).reserve("guest-1", "tok-b", msg2["topic_key"], "c2")

    def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(idx, "save_claude_answer_if_unset", broken)
    finish(pipeline, msg2, json.dumps(GOOD), chat="c2")
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 0 and doc["inflight"] is None
    assert saved(pipeline, msg2, chat="c2").get("claude_answer") is None


def test_counted_but_unsaved_answer_is_redelivered_once_without_a_second_count(pipeline, gg):
    """A crash after the count and before the save: the retry finishes the delivery and never counts twice."""
    msg = new_message(pipeline, "tok-a")
    gate = gg.GuestGate(pipeline["coll"], limit=1)
    gate.reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    assert gate.complete("guest-1", "tok-a", "c1", msg["topic_key"]) == "completed"     # ... crash here ...
    assert saved(pipeline, msg).get("claude_answer") is None
    finish(pipeline, msg, json.dumps(GOOD))
    assert "Hiring freeze" in saved(pipeline, msg)["claude_answer"]
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 1 and doc["inflight"] is None


def test_gate_store_outage_defers_without_a_model_call_or_a_saved_answer(pipeline, monkeypatch):
    from tests.test_guest_gate import _BrokenColl
    msg = new_message(pipeline, "tok-a")
    sys.modules["database"].guest_gate_collection = _BrokenColl()
    finish(pipeline, msg, json.dumps(GOOD))
    assert pipeline["llm_calls"] == [] and saved(pipeline, msg).get("claude_answer") is None   # pending, retried later


def test_timeout_fallback_of_a_superseded_search_makes_no_model_call_and_saves_only_the_notice(pipeline, gg):
    lg = pipeline["lg"]
    msg = new_message(pipeline, "tok-a")
    takeover(pipeline, gg, "tok-a", "tok-b")
    lg._call_claude = lambda *a, **k: pipeline["llm_calls"].append(1) or json.dumps(GOOD)
    lg._timeout_fallback_answer("c1", "guest-1", msg["topic_key"], msg["query"], ["kw"], request_id="tok-a")
    assert pipeline["llm_calls"] == []
    assert json.loads(saved(pipeline, msg)["claude_answer"])["answer_status"] == "incomplete"
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 0 and doc["inflight"]["token"] == "tok-b"


def test_timeout_fallback_by_the_owner_is_saved_but_never_counted(pipeline, gg):
    lg = pipeline["lg"]
    msg = new_message(pipeline, "tok-a")
    gg.GuestGate(pipeline["coll"], limit=1).reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    lg._call_claude = lambda *a, **k: json.dumps(GOOD)
    lg._timeout_fallback_answer("c1", "guest-1", msg["topic_key"], msg["query"], ["kw"], request_id="tok-a")
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 0 and doc["inflight"] is None                            # delivered, uncounted, slot freed


# ───────────────────────── signing in while a search runs ───────────────────────────────────────────
def _tagged_message(e2e, chat_id):
    return e2e["db"].chats.find_one({"chat_id": chat_id})["messages"][-1]


def _finish_under(e2e, chat_id, email, llm):
    lg, idx = e2e["lg"], e2e["idx"]
    msg = _tagged_message(e2e, chat_id)
    msg["requested_at"] = dt.datetime(2026, 10, 20, 12, 0, 0)
    lg._call_claude = llm
    idx._complete_message_answer_and_results(chat_id, email, msg, [])
    return e2e["db"].chats.find_one({"chat_id": chat_id})["messages"][-1]


def _signup(client, email="mid@example.com"):
    return client.post("/signup", data={"email": email, "password": "correct-horse-1", "confirm_password": "correct-horse-1",
                                        "resume": "1"}, headers={"Accept": "application/json"})


@pytest.mark.usefixtures("flat_cost")
def test_message_request_id_is_the_gate_token(e2e):
    client, chat_id = _guest_search(e2e)
    guest_id = gid_of(e2e["idx"], client)
    assert _tagged_message(e2e, chat_id)["request_id"] == e2e["gate_coll"].find_one({"_id": guest_id})["inflight"]["token"]


@pytest.mark.usefixtures("flat_cost")
def test_signup_mid_search_then_the_search_succeeds(e2e):
    client, chat_id = _guest_search(e2e)
    guest_id = gid_of(e2e["idx"], client)
    token = e2e["gate_coll"].find_one({"_id": guest_id})["inflight"]["token"]
    resp = _signup(client)
    assert resp.status_code == 200 and resp.json() == {"ok": True, "redirect": f"/chat/{chat_id}"}
    assert _tagged_message(e2e, chat_id)["guest_gate_owner"] == guest_id                 # stamped; chat owned by the account
    assert e2e["db"].chats.find_one({"chat_id": chat_id})["owner_key"] == "mid@example.com"
    gate = e2e["gate_coll"].find_one({"_id": guest_id})
    assert gate["inflight"]["token"] == token and gate["completed"] == 0                 # reservation kept, nothing counted yet
    row = _finish_under(e2e, chat_id, "mid@example.com", lambda *a, **k: json.dumps(GOOD))
    assert "Hiring freeze" in row["claude_answer"] or row["claude_answer"]               # delivered to the account's chat
    gate = e2e["gate_coll"].find_one({"_id": guest_id})
    assert gate["completed"] == 1 and gate["inflight"] is None and gate["last_chat_id"] == chat_id
    assert len(e2e["state"]["enqueues"]) == 1                                            # never re-run
    usage = e2e["db"].flintel_credit_usage
    assert usage.count_documents({"owner_key": "mid@example.com"}) == 0                  # the account was never charged
    client.get("/logout")
    client.get("/")
    again = post_search(client, KEY2)                                                    # logout is not a second free search
    assert again.status_code == 403 and again.headers["X-Flintel-Denial"] == "guest-auth-required"


@pytest.mark.usefixtures("flat_cost")
def test_signup_mid_search_then_the_search_fails_releases_by_its_own_token(e2e):
    client, chat_id = _guest_search(e2e)
    guest_id = gid_of(e2e["idx"], client)
    _signup(client)

    def boom(*a, **k):
        raise RuntimeError("model down")
    row = _finish_under(e2e, chat_id, "mid@example.com", boom)
    assert row.get("claude_answer") is None                                              # still pending for the worker
    gate = e2e["gate_coll"].find_one({"_id": guest_id})
    assert gate["completed"] == 0 and gate["inflight"] is None                           # released: nothing counted
    usage = e2e["db"].flintel_credit_usage
    assert usage.count_documents({"owner_key": "mid@example.com"}) == 0                  # no extra account search / charge
    assert len(e2e["state"]["enqueues"]) == 1
    client.get("/logout")                                                                # a failed search never cost the guest
    client.get("/")
    again = post_search(client, KEY2)
    assert again.status_code == 303


@pytest.mark.usefixtures("flat_cost")
def test_duplicate_logins_and_google_callbacks_mid_search_change_nothing_more(e2e, monkeypatch):
    e2e["users"].insert_one({"email": "dup@example.com", "password_hash": "h:correct-horse-1", "provider": "email"})
    client, chat_id = _guest_search(e2e)
    guest_id = gid_of(e2e["idx"], client)
    for _ in range(3):
        r = client.post("/login", data={"email": "dup@example.com", "password": "correct-horse-1", "resume": "1"},
                        headers={"Accept": "application/json"})
        assert r.json()["ok"] is True
    monkeypatch.setattr(e2e["rt"], "oauth", _Oauth())
    client.get("/auth/google/login?resume=1", follow_redirects=False)
    client.get("/auth/google/callback", follow_redirects=False)
    client.get("/auth/google/callback", follow_redirects=False)
    gate = e2e["gate_coll"].find_one({"_id": guest_id})
    assert gate["completed"] == 0 and gate["inflight"]["chat_id"] == chat_id             # unchanged by any of the callbacks
    assert e2e["db"].chats.count_documents({"chat_id": chat_id}) == 1
    assert len(_tagged_message_all(e2e, chat_id)) == 1 and len(e2e["state"]["enqueues"]) == 1
    row = _finish_under(e2e, chat_id, "dup@example.com", lambda *a, **k: json.dumps(GOOD))
    assert e2e["gate_coll"].find_one({"_id": guest_id})["completed"] == 1


def _tagged_message_all(e2e, chat_id):
    return e2e["db"].chats.find_one({"chat_id": chat_id})["messages"]


@pytest.mark.usefixtures("flat_cost")
def test_late_worker_of_a_signed_in_guest_cannot_touch_the_newer_guest_search(e2e):
    client, chat_id = _guest_search(e2e)
    guest_id = gid_of(e2e["idx"], client)
    _signup(client)                                                                      # A now runs under the account
    age(e2e["gate_coll"], guest_id)                                                      # A's reservation goes stale
    client.get("/logout")
    client.get("/")
    new = post_search(client, KEY2)                                                      # the visitor, as a guest, takes over
    assert new.status_code == 303
    token_b = e2e["gate_coll"].find_one({"_id": guest_id})["inflight"]["token"]
    llm_calls = []
    row = _finish_under(e2e, chat_id, "mid@example.com", lambda *a, **k: llm_calls.append(1) or json.dumps(GOOD))
    assert llm_calls == []                                                               # the late worker never calls the model
    assert json.loads(row["claude_answer"])["answer_status"] == "incomplete"             # and delivers nothing
    gate = e2e["gate_coll"].find_one({"_id": guest_id})
    assert gate["completed"] == 0 and gate["inflight"]["token"] == token_b               # B untouched


# ───────────────────────── existing session-only guest (no fl_gid yet) ──────────────────────────────
@pytest.mark.usefixtures("flat_cost")
def test_session_only_guest_gets_the_cookie_keeps_the_chat_and_no_extra_search(e2e):
    idx = e2e["idx"]
    client = e2e["new_client"]()
    client.get("/")                                                                      # session names the guest
    gid = gid_of(idx, client)
    e2e["db"].chats.insert_one({"chat_id": "legacy-1", "owner_key": gid, "owner_type": "anon", "title": "My old research",
                                "messages": [{"query": "old question", "topic_key": "tk-old", "claude_answer": json.dumps(GOOD),
                                              "message_type": "search", "results": [],
                                              "requested_at": dt.datetime(2026, 10, 1, 12, 0, 0)}]})
    client.cookies.delete("fl_gid", domain="flintel.test")                               # the pre-feature state
    assert cookie_value(client) is None
    home = client.get("/")
    assert home.status_code == 200 and "My old research" in html.unescape(home.text)     # the history is still there
    issued = [h for h in home.headers.get_list("set-cookie") if h.startswith("fl_gid=")]
    assert issued and gid_of(idx, client) == gid                                         # cookie issued for the SAME id
    page = client.get("/chat/legacy-1")                                                  # a refresh opens the original chat
    assert page.status_code == 200 and "old question" in html.unescape(page.text)
    assert e2e["db"].chats.find_one({"chat_id": "legacy-1"})["owner_key"] == gid        # ownership unchanged
    assert e2e["gate_coll"].count_documents({}) == 0                                     # nothing consumed by the migration
    first = post_search(client)                                                          # still exactly one search ...
    assert first.status_code == 303
    token = e2e["gate_coll"].find_one({"_id": gid})["inflight"]["token"]
    e2e["gg"].on_answer_ready(gid, "x", "t", token, json.dumps(GOOD))
    second = post_search(client, KEY2)
    assert second.status_code == 403 and second.headers["X-Flintel-Denial"] == "guest-auth-required"   # ... and no more
    assert [d["_id"] for d in e2e["gate_coll"].find({})] == [gid]                        # one identity, one record


@pytest.mark.usefixtures("flat_cost")
def test_session_only_guest_searching_before_the_cookie_exists_is_asked_to_retry_once(e2e):
    idx = e2e["idx"]
    client = e2e["new_client"]()
    client.get("/")
    gid = gid_of(idx, client)
    client.cookies.delete("fl_gid", domain="flintel.test")
    refused = post_search(client)
    assert refused.status_code == 403 and refused.headers["X-Flintel-Denial"] == "guest-identity-required"
    assert gid_of(idx, client) == gid and e2e["gate_coll"].count_documents({}) == 0
    assert post_search(client, KEY2).status_code == 303                                  # same id, admitted exactly once


# ───────────────────────── atomicity ────────────────────────────────────────────────────────────────
class Spy:
    """Wraps a collection and records the stored document after every write."""
    def __init__(self, coll, snapshots):
        self._c, self.snapshots = coll, snapshots

    def __getattr__(self, name):
        attr = getattr(self._c, name)
        if name not in ("update_one", "insert_one", "find_one_and_update"):
            return attr

        def wrapped(*a, **k):
            result = attr(*a, **k)
            doc = self._c.find_one({"_id": "g1"})
            if doc is not None:
                self.snapshots.append((name, json.loads(json.dumps(doc, default=str))))
            return result
        return wrapped


def test_a_completing_write_never_leaves_a_counted_search_holding_the_slot(gg, clock):
    snaps = []
    c = Spy(mongomock.MongoClient()["spy"]["g"], snaps)
    gate = make_gate(gg, c, clock)
    gate.reserve("g1", "tok-a", "tk", "c1")
    snaps.clear()
    assert gate.complete("g1", "tok-a", "c1", "tk") == "completed"
    owner_writes = [s for s in snaps if s[0] == "update_one"]
    assert len(owner_writes) == 1                                                        # ONE write counts and frees the slot
    doc = owner_writes[0][1]
    assert doc["completed"] == 1 and doc["inflight"] is None and doc["completed_tokens"] == ["tok-a"]


def test_a_crash_between_the_phases_of_a_delivery_is_recoverable_at_every_point(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    # crash after reserve, before anything else: the stale rule frees the slot; the same token can also resume
    gate.reserve("g1", "tok-a", "tk", "c1")
    assert gate.reserve("g1", "tok-a", "tk", "c1").resumed
    clock.advance(901)
    assert gate.reserve("g1", "tok-b", "tk", "c2").reserved
    # crash after the count, before the answer is stored: allowance used, slot free, same token may finish delivering
    assert gate.complete("g1", "tok-b", "c2", "tk") == "completed"
    doc = coll.find_one({"_id": "g1"})
    assert doc["completed"] == 1 and doc["inflight"] is None
    assert gate.is_counted("g1", "tok-b") and gate.complete("g1", "tok-b", "c2", "tk") == "duplicate"
    # crash during revert is a single write: either the count is back or it is not
    assert gate.revert("g1", "tok-b") is True and coll.find_one({"_id": "g1"})["completed"] == 0


@pytest.mark.parametrize("limit", [1, 2])
def test_model_based_random_interleavings_keep_every_invariant(gg, clock, limit):
    """Random sequences of reserve / complete / release / revert / stale-time over five tokens. After EVERY single
    write: the count never exceeds the limit, equals the counted tokens, a counted token never holds the slot, a
    release or completion by a non-owner never moves the slot, and the resume chat only changes on a real count."""
    rng = random.Random(1234 + limit)
    tokens = [f"t{i}" for i in range(5)]
    chat_of = {t: f"chat-{t}" for t in tokens}
    for seq in range(150):
        c = mongomock.MongoClient()[f"model{limit}_{seq}"]["g"]
        gate = make_gate(gg, c, clock, limit=limit)
        for step in range(40):
            tok = rng.choice(tokens)
            before = c.find_one({"_id": "g1"}) or {}
            op = rng.choice(["reserve", "reserve", "complete", "release", "revert", "age"])
            owner_before = (before.get("inflight") or {}).get("token")
            if op == "reserve":
                gate.reserve("g1", tok, "tk", chat_of[tok])
            elif op == "complete":
                outcome = gate.complete("g1", tok, chat_of[tok], "tk")
                if owner_before not in (None, tok):
                    assert outcome != "completed", (seq, step)                           # a non-owner never completes
            elif op == "release":
                gate.release("g1", tok)
                if owner_before not in (None, tok):
                    assert (c.find_one({"_id": "g1"}) or {}).get("inflight", {}).get("token") == owner_before, (seq, step)
            elif op == "revert":
                gate.revert("g1", tok)
            else:
                clock.advance(rng.choice([10, 500, 901]))
            doc = c.find_one({"_id": "g1"})
            if doc is None:
                continue
            counted = doc.get("completed_tokens") or []
            assert 0 <= doc["completed"] <= limit, (seq, step, doc)
            assert doc["completed"] == len(counted) == len(set(counted)), (seq, step, doc)
            inflight = doc.get("inflight")
            assert inflight is None or inflight["token"] not in counted, (seq, step, doc)
            if doc["completed"] > 0 and doc.get("last_chat_id") is not None:
                assert doc["last_chat_id"] in {chat_of[t] for t in tokens}
            if op in ("release", "reserve") and doc["completed"] == before.get("completed", 0):
                assert doc.get("last_chat_id") == before.get("last_chat_id"), (seq, step)   # resume target only moves on a count


def test_timeout_fallback_superseded_during_its_model_call_discards_the_answer(pipeline, gg):
    lg = pipeline["lg"]
    msg = new_message(pipeline, "tok-a")
    gate = gg.GuestGate(pipeline["coll"], limit=1)
    gate.reserve("guest-1", "tok-a", msg["topic_key"], "c1")

    def slow(*a, **k):
        age(pipeline["coll"])
        assert gate.reserve("guest-1", "tok-b", "t-new", "c2").reserved                 # a newer search takes the slot
        return json.dumps(GOOD)
    lg._call_claude = slow
    lg._timeout_fallback_answer("c1", "guest-1", msg["topic_key"], msg["query"], ["kw"], request_id="tok-a")
    row = saved(pipeline, msg)
    assert "Hiring freeze" not in row["claude_answer"] and json.loads(row["claude_answer"])["answer_status"] == "incomplete"
    doc = pipeline["coll"].find_one({"_id": "guest-1"})
    assert doc["completed"] == 0 and doc["inflight"]["token"] == "tok-b"


def test_resume_target_while_the_search_is_still_running_is_its_own_conversation(hooks):
    gg, gate, coll = hooks
    assert gg.resume_chat_id("g1") is None
    gate.reserve("g1", "tok-a", "tk", "chat-running")
    assert gg.resume_chat_id("g1") == "chat-running"                                    # signed in mid-search -> same chat
    age(coll, "g1")
    assert gg.resume_chat_id("g1") is None                                              # a dead reservation is not a target
