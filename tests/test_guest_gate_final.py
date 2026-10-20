"""
tests/test_guest_gate_final.py — last review points of the guest gate:

  1. one model call per accepted search: the per-search work lease (second tab / retry / worker / stream);
  2. crash recovery: after the count but before the answer is stored, and after it is stored but before the browser
     got it — recovered by refresh / reconnect without a second search, count or credit charge;
  3. timeout / scan-deadline answers stay uncounted AND are shown as "Limited results", not as a finished search.

Mongo is mongomock; there is no live MongoDB, Google, e-mail delivery, browser restart or proxy here.
"""
import datetime as dt
import html
import json
import sys
import threading
import time

import pytest

from tests.test_guest_gate import (  # noqa: F401  (fixtures shared with the main gate tests)
    GOOD, KEY1, Clock, _capture_real_identity_check, app, clock, coll, e2e, env, flat_cost, gapp, gg, gid_of, make_gate,
    mongomock, pipeline, post_search, _guest_search,
)
from tests.test_guest_gate_delivery import new_message, saved, age  # noqa: F401
from tests.test_guest_gate_stream import (  # noqa: F401
    GOOD_ANSWER, doc as stream_doc, run_stream, shown, spy, take_over,
)
from tests.test_scan_resilience import routes_env  # noqa: F401
from tests.test_strict_intent import lg  # noqa: F401


# ───────────────────── 1. work lease (gate level) ───────────────────────────────────────────────────
def test_work_lease_admits_one_runner_per_search_and_only_its_holder_can_end_it(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    gate.reserve("g1", "tok-a", "tk", "c1")
    run1 = gate.acquire_work("g1", "tok-a")
    assert run1 and gate.acquire_work("g1", "tok-a") is None                     # a second runner is refused
    assert gate.end_work("g1", "tok-a", "someone-else") is False                 # only the holder gives it back
    assert gate.acquire_work("g1", "tok-a") is None
    assert gate.end_work("g1", "tok-a", run1) is True
    assert gate.acquire_work("g1", "tok-a")                                      # free again


def test_work_lease_expires_by_itself_after_a_crash(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, limit=1, stale_seconds=900, work_lease_seconds=120)
    gate.reserve("g1", "tok-a", "tk", "c1")
    assert gate.acquire_work("g1", "tok-a")
    clock.advance(119)
    assert gate.acquire_work("g1", "tok-a") is None                              # the crashed runner still excludes others ...
    clock.advance(2)
    assert gate.acquire_work("g1", "tok-a")                                      # ... but only for the lease time


def test_work_lease_is_only_for_the_search_that_owns_the_slot_or_was_already_counted(gg, coll, clock):
    gate = make_gate(gg, coll, clock)
    gate.reserve("g1", "tok-a", "tk", "c1")
    assert gate.acquire_work("g1", "tok-other") is None                          # not the owner of the slot
    gate.complete("g1", "tok-a", "c1")
    assert gate.acquire_work("g1", "tok-a")                                      # counted, answer still to be stored: allowed
    assert gate.acquire_work("nobody", "tok-a") is None


def test_work_lease_length_is_bounded_by_the_stale_limit(gg):
    assert 30 <= gg.configured_work_lease_seconds() <= gg.configured_stale_seconds() - 30


def test_a_waiting_runner_never_releases_the_slot_of_the_runner_that_is_working(gg, coll, clock, monkeypatch):
    gate = make_gate(gg, coll, clock)
    monkeypatch.setattr(gg, "default_gate", lambda: gate)
    gate.reserve("g1", "tok-a", "tk", "c1")
    first, second = {}, {}
    assert gg.on_work_start("g1", "tok-a", "tk", "c1", first) == gg.PROCEED
    assert gg.on_work_start("g1", "tok-a", "tk", "c1", second) == gg.WAIT
    gg.on_work_end("g1", "tok-a", second)                                        # the waiter's finally: must change nothing
    assert coll.find_one({"_id": "g1"})["inflight"]["token"] == "tok-a"
    assert gate.acquire_work("g1", "tok-a") is None                              # the first runner still holds its lease
    gg.on_work_end("g1", "tok-a", first)
    assert gate.acquire_work("g1", "tok-a")


# ───────────────────── 1. one model call per search (pipeline / worker) ─────────────────────────────
def test_two_runners_of_the_same_search_make_one_model_call(pipeline, gg):
    msg1 = new_message(pipeline, "tok-a")
    msg2 = dict(msg1)                                                            # the page / worker loaded its own copy
    gg.GuestGate(pipeline["coll"], limit=1).reserve("guest-1", "tok-a", msg1["topic_key"], "c1")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_model(*a, **k):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return json.dumps(GOOD)
    pipeline["lg"]._call_claude = slow_model
    t = threading.Thread(target=lambda: pipeline["idx"]._complete_message_answer_and_results("c1", "guest-1", msg1, pipeline["post"]))
    t.start()
    assert entered.wait(10)
    pipeline["idx"]._complete_message_answer_and_results("c1", "guest-1", msg2, pipeline["post"])   # second tab / retry / worker
    assert calls == [1]                                                          # it did NOT start a second model call
    assert saved(pipeline, msg1).get("claude_answer") is None                    # and stored nothing of its own
    release.set()
    t.join(10)
    assert calls == [1] and "Hiring freeze" in saved(pipeline, msg1)["claude_answer"]
    d = pipeline["coll"].find_one({"_id": "guest-1"})
    assert d["completed"] == 1 and d["inflight"] is None and d.get("work_lease") is None


def test_a_hard_crashed_runner_blocks_a_retry_only_until_its_lease_expires(pipeline, gg):
    msg = new_message(pipeline, "tok-a")
    gate = gg.GuestGate(pipeline["coll"], limit=1)
    gate.reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    assert gate.acquire_work("guest-1", "tok-a")                                 # the runner dies here: no finally ran
    calls = []
    pipeline["lg"]._call_claude = lambda *a, **k: calls.append(1) or json.dumps(GOOD)
    pipeline["idx"]._complete_message_answer_and_results("c1", "guest-1", msg, pipeline["post"])
    assert calls == [] and saved(pipeline, msg).get("claude_answer") is None     # waits (the turn stays pending)
    pipeline["coll"].update_one({"_id": "guest-1"}, {"$set": {"work_lease.until": dt.datetime(2020, 1, 1)}})
    pipeline["idx"]._complete_message_answer_and_results("c1", "guest-1", msg, pipeline["post"])
    assert calls == [1] and "Hiring freeze" in saved(pipeline, msg)["claude_answer"]
    assert pipeline["coll"].find_one({"_id": "guest-1"})["completed"] == 1


# ───────────────────── 1. one model call per search (streams) ────────────────────────────────────────
def test_a_second_tab_waits_for_the_first_streams_answer_and_replays_it(spy, monkeypatch):
    rt = spy.routes
    spy.reserve()
    entered, release = threading.Event(), threading.Event()

    def slow_model(*a, **k):
        spy.analyze_calls.append(1)
        entered.set()
        assert release.wait(10)
        return GOOD_ANSWER
    monkeypatch.setattr(rt, "analyze_with_claude", slow_model)

    def save(chat, owner, tk, ans):
        spy.saved.append(ans)
        spy.msg["claude_answer"] = ans                                           # what the real store does for the page / poll
    monkeypatch.setattr(rt, "save_claude_answer_to_chat", save)
    first_out = []
    t = threading.Thread(target=lambda: first_out.extend(run_stream(spy)))
    t.start()
    assert entered.wait(10)
    threading.Timer(0.5, release.set).start()
    second_out = run_stream(spy)                                                 # the second tab, while the first is working
    t.join(10)
    assert spy.analyze_calls == [1]                                              # ONE model call for the accepted search
    assert GOOD_ANSWER in shown(first_out) and GOOD_ANSWER in shown(second_out)  # both tabs get the answer
    d = stream_doc(spy)
    assert d["completed"] == 1 and d["inflight"] is None and d.get("work_lease") is None
    assert json.loads([c for c in second_out if '"done": true' in c][0][6:])["guest_gate"] == "auth_required"


def test_a_closed_stream_hands_the_search_over_immediately(spy):
    spy.reserve()
    spy.scan_seconds = 10
    from unittest.mock import MagicMock
    gen = iter(spy.routes.stream_answer(MagicMock(), "chat-g", "tk").body_iterator)
    next(gen); next(gen); next(gen)
    assert spy.gate.acquire_work("owner1", "tok-a") is None                      # the first stream holds the lease while it works
    gen.close()                                                                  # the browser went away
    run = spy.gate.acquire_work("owner1", "tok-a")
    assert run                                                                   # the worker / a refresh can take over NOW
    spy.gate.end_work("owner1", "tok-a", run)


# ───────────────────── 2. crash recovery ──────────────────────────────────────────────────────────────
def test_crash_after_the_count_and_before_the_answer_is_stored_a_refresh_regenerates_it_once(spy):
    """Counted, slot cleared, nothing stored. The refresh's stream finishes the delivery: one model call, no new search,
    no second count, no new reservation."""
    spy.reserve()
    assert spy.gate.complete("owner1", "tok-a", "chat-g", "tk") == "completed"   # ... the process dies here ...
    assert spy.msg.get("claude_answer") is None and spy.saved == []
    out = run_stream(spy)                                                        # the page's stream after the refresh
    assert spy.analyze_calls == [1] and spy.saved == [GOOD_ANSWER]
    d = stream_doc(spy)
    assert d["completed"] == 1 and d["inflight"] is None and d["completed_tokens"] == ["tok-a"] and d["tokens"] == ["tok-a"]
    assert json.loads([c for c in out if '"done": true' in c][0][6:])["guest_gate"] == "auth_required"   # sign-up flag after the answer


def test_crash_after_the_answer_is_stored_and_before_the_browser_got_it_the_reconnect_replays_it(spy):
    spy.reserve()
    assert spy.gate.complete("owner1", "tok-a", "chat-g", "tk") == "completed"
    spy.msg["claude_answer"] = GOOD_ANSWER                                       # stored; the connection died before delivery
    spy.msg["results"] = []
    before = stream_doc(spy)
    out = run_stream(spy)                                                        # the browser's EventSource reconnects
    assert spy.analyze_calls == [] and spy.saved == []                           # no model call, nothing re-saved
    assert GOOD_ANSWER in shown(out)
    assert json.loads([c for c in out if '"done": true' in c][0][6:])["guest_gate"] == "auth_required"
    assert stream_doc(spy) == before                                             # gate untouched: no count, no reservation


@pytest.mark.usefixtures("flat_cost")
def test_refresh_after_each_crash_point_never_adds_a_search_a_count_or_a_charge(e2e):
    client, chat_id = _guest_search(e2e)
    guest_id = gid_of(e2e["idx"], client)
    token = e2e["gate_coll"].find_one({"_id": guest_id})["inflight"]["token"]
    usage = lambda: e2e["db"].flintel_credit_usage.find_one({"owner_key": guest_id})
    charged = usage()["used_by_period"]
    enq = len(e2e["state"]["enqueues"])
    # crash point 1: counted, answer NOT stored. The page must not open the sign-up modal before the answer exists.
    assert e2e["gg"].GuestGate(e2e["gate_coll"], limit=1).complete(guest_id, token, chat_id, "tk") == "completed"
    page = client.get(f"/chat/{chat_id}")
    assert page.status_code == 200 and 'id="guest-gate-marker"' not in page.text
    # crash point 2: answer stored, browser never received it. A refresh shows the answer, then the modal.
    e2e["db"].chats.update_one({"chat_id": chat_id}, {"$set": {"messages.0.claude_answer": json.dumps(GOOD), "messages.0.results": []}})
    page = client.get(f"/chat/{chat_id}")
    assert 'id="guest-gate-marker"' in page.text and "Hiring freeze" in html.unescape(page.text) or "competition is intense" in html.unescape(page.text)
    for _ in range(2):                                                           # repeated refreshes
        assert client.get(f"/chat/{chat_id}").status_code == 200
    assert usage()["used_by_period"] == charged                                  # no second charge
    assert len(e2e["state"]["enqueues"]) == enq                                  # no second search / job
    assert e2e["gate_coll"].find_one({"_id": guest_id})["completed"] == 1        # counted exactly once
    again = post_search(client, "55555555-aaaa-4bbb-8ccc-000000000005")
    assert again.status_code == 403 and again.headers["X-Flintel-Denial"] == "guest-auth-required"


# ───────────────────── 3. limited (timeout) results are labelled and uncounted ────────────────────────
def test_limited_stamp_applies_only_to_gated_guest_answers(gg, coll, clock, monkeypatch):
    gate = make_gate(gg, coll, clock)
    monkeypatch.setattr(gg, "default_gate", lambda: gate)
    out = json.loads(gg.mark_limited("g1", "tok", json.dumps(GOOD)))
    assert out["answer_status"] == "limited" and out["headline"] == GOOD["headline"]      # content untouched
    assert gg.mark_limited("member@example.com", "tok", json.dumps(GOOD)) == json.dumps(GOOD)   # registered users unchanged
    assert gg.mark_limited("g1", None, json.dumps(GOOD)) == json.dumps(GOOD)
    assert gg.mark_limited("g1", "tok", "plain prose") == "plain prose"
    partial = json.dumps({"format": "source_list", "answer_status": "partial", "headline": "x"})
    assert gg.mark_limited("g1", "tok", partial) == partial                      # an existing incomplete/partial status wins
    assert gg.answer_counts(gg.mark_limited("g1", "tok", json.dumps(GOOD))) is False   # and a limited answer never counts


def test_timeout_fallback_answer_of_a_guest_is_saved_as_limited_and_stays_uncounted(pipeline, gg):
    lg = pipeline["lg"]
    msg = new_message(pipeline, "tok-a")
    gg.GuestGate(pipeline["coll"], limit=1).reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    lg._call_claude = lambda *a, **k: json.dumps(GOOD)
    lg._timeout_fallback_answer("c1", "guest-1", msg["topic_key"], msg["query"], ["kw"], request_id="tok-a")
    row = json.loads(saved(pipeline, msg)["claude_answer"])
    assert row["answer_status"] == "limited" and row["headline"] == GOOD["headline"]
    d = pipeline["coll"].find_one({"_id": "guest-1"})
    assert d["completed"] == 0 and d["inflight"] is None                         # uncounted; the guest can search again


def test_timeout_fallback_answer_of_a_registered_user_is_unchanged(pipeline):
    lg, idx = pipeline["lg"], pipeline["idx"]
    q = "Find users looking for AI jobs in Pakistan"
    idx.add_search_to_chat("cm", "member@example.com", q, "tk-member", ["kw"], "reddit")
    lg._call_claude = lambda *a, **k: json.dumps(GOOD)
    lg._timeout_fallback_answer("cm", "member@example.com", "tk-member", q, ["kw"], request_id="rid-1")
    msg = pipeline["chats"].find_one({"chat_id": "cm", "owner_key": "member@example.com"})["messages"][-1]
    assert "answer_status" not in json.loads(msg["claude_answer"])


# the page: a limited result is not shown as a finished search
from tests.test_answer_persistence import STATE_JS, TEMPLATES, _for_template, _reopen, _researched  # noqa: E402


@pytest.fixture
def browser(integrity_browser):          # the session-wide Chromium of tests/conftest.py
    return integrity_browser

LIMITED_CHAT = {"chat_id": "c1", "title": "Limits", "messages": [
    {"query": "Finished search", "topic_key": "a", "results": [], "claude_answer": json.dumps(GOOD)},
    {"query": "Timed out search", "topic_key": "b", "results": [],
     "claude_answer": json.dumps(dict(GOOD, answer_status="limited"))},
    {"query": "Cut off search", "topic_key": "c", "results": [],
     "claude_answer": json.dumps(dict(GOOD, answer_status="partial"))},
    {"query": "Empty search", "topic_key": "d", "results": [],
     "claude_answer": json.dumps({"format": "no_results", "answer_status": "incomplete", "message": ""})},
]}


@pytest.mark.parametrize("name", TEMPLATES)
def test_page_labels_limited_results_differently_from_finished_and_cut_off_ones(browser, name):
    page, errors = _reopen(browser, name, _for_template(LIMITED_CHAT))
    try:
        rows = page.evaluate(STATE_JS)
        assert _researched(rows[0]) and "competition is intense" in rows[0]["shown"]        # finished: unchanged
        assert not rows[0]["askAgain"] and "limited" not in rows[0]["shown"].lower()
        assert rows[1]["chip"] == "Limited results" and rows[1]["chipClass"] == "status-failed"
        assert "competition is intense" in rows[1]["shown"]                                  # the findings are still there
        assert "did not use up your free search" in rows[1]["shown"] and rows[1]["askAgain"]
        assert rows[2]["chip"] == "Partial answer" and "cut off before it finished" in rows[2]["shown"]
        assert rows[3]["chip"] == "Answer unavailable"
        assert errors == []
    finally:
        page.close()
