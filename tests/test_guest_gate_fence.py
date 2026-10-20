"""
tests/test_guest_gate_fence.py — the work-lease EXPIRY race.

Scenario: a runner is still generating when its work lease expires; a second runner takes the lease for the SAME search.
What the fence guarantees (and what it does not):

  guaranteed   * the count happens at most once, and a runner that no longer holds the lease cannot count (the lease
                 check and the count are ONE atomic update in the gate);
               * the stored answer is first-writer-wins (a conditional update on the chat message), so a stale runner can
                 never overwrite the winner;
               * for a guest search the winner is chosen AND stored before the first answer chunk is streamed, so no
                 conflicting text reaches a browser; a runner that lost replays the stored winner;
               * a lease can only be renewed by the runner that holds it; renewals keep a legitimate long search leased.
  NOT guaranteed * exactly-once MODEL execution: after a lease expires a second model call can still overlap with the
                 first (one wasted model call); the fence only stops it from counting, storing or being shown.

Mongo is mongomock (conditional updates and the thread races are exercised on it); live MongoDB is not covered.
"""
import datetime as dt
import html
import json
import sys
import threading
import time

import pytest

from tests.test_guest_gate import (  # noqa: F401  (fixtures shared with the main gate tests)
    GOOD, KEY1, KEY2, Clock, _capture_real_identity_check, app, clock, coll, e2e, env, flat_cost, gapp, gg, gid_of,
    make_gate, mongomock, pipeline, post_search, _guest_search,
)
from tests.test_guest_gate_stream import (  # noqa: F401
    GOOD_ANSWER, doc as stream_doc, run_stream, shown, spy,
)
from tests.test_scan_resilience import routes_env  # noqa: F401
from tests.test_strict_intent import lg  # noqa: F401

OLD = dt.datetime(2020, 1, 1)


class Atomic:
    """Serialises reads/writes of a mongomock collection. MongoDB makes every single-document update atomic; mongomock
    does not promise that between threads, so the thread-race tests wrap the collection to model the property they rely
    on (the code under test supplies the conditional filter; the database supplies the atomicity)."""
    _LOCK_OPS = ("update_one", "find_one_and_update", "insert_one", "find_one", "update_many", "delete_many")

    def __init__(self, coll):
        self._c, self._lock = coll, threading.RLock()

    def __getattr__(self, name):
        attr = getattr(self._c, name)
        if name not in self._LOCK_OPS:
            return attr

        def locked(*a, **k):
            with self._lock:
                return attr(*a, **k)
        return locked
ANSWER_A = json.dumps({"format": "source_list", "headline": "ANSWER-FROM-THE-STALE-WORKER", "executive_summary": "a"})
ANSWER_B = json.dumps({"format": "source_list", "headline": "ANSWER-FROM-THE-CURRENT-WORKER", "executive_summary": "b"})


def expire_lease(coll_, owner="guest-1"):
    coll_.update_one({"_id": owner}, {"$set": {"work_lease.until": OLD}})


# ───────────────────────── gate level: the commit fence ───────────────────────────────────────────────
def test_commit_is_fenced_atomically_while_another_runner_holds_an_unexpired_lease(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, limit=1, stale_seconds=900, work_lease_seconds=120)
    gate.reserve("g1", "tok", "tk", "c1")
    run1 = gate.acquire_work("g1", "tok")
    clock.advance(121)                                               # run1's lease expires while it is still working
    run2 = gate.acquire_work("g1", "tok")
    assert run2 and run2 != run1
    assert gate.complete("g1", "tok", "c1", "tk", run1) == "fenced"   # the stale runner cannot count ...
    d = coll.find_one({"_id": "g1"})
    assert d["completed"] == 0 and d["inflight"]["token"] == "tok" and d["work_lease"]["run"] == run2   # ... or touch the state
    assert gate.complete("g1", "tok", "c1", "tk", run2) == "completed"
    assert coll.find_one({"_id": "g1"})["completed"] == 1


def test_a_stale_runner_may_still_commit_when_nobody_took_the_lease_over(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, limit=1, stale_seconds=900, work_lease_seconds=120)
    gate.reserve("g1", "tok", "tk", "c1")
    run1 = gate.acquire_work("g1", "tok")
    clock.advance(500)                                               # expired, but still the only runner
    assert gate.complete("g1", "tok", "c1", "tk", run1) == "completed"


def test_a_taken_over_runner_whose_replacement_died_may_commit_once_that_lease_expired_too(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, limit=1, stale_seconds=900, work_lease_seconds=120)
    gate.reserve("g1", "tok", "tk", "c1")
    run1 = gate.acquire_work("g1", "tok")
    clock.advance(121)
    run2 = gate.acquire_work("g1", "tok")                            # run2 then crashes: it never ends its lease
    clock.advance(121)
    assert gate.complete("g1", "tok", "c1", "tk", run1) == "completed"


def test_the_count_never_exceeds_one_when_two_runners_race_to_commit(gg, clock):
    """Run 25 times: the old runner (expired lease) and the new runner commit at the same instant."""
    for n in range(25):
        c = Atomic(mongomock.MongoClient()[f"race{n}"]["g"])
        gate = gg.GuestGate(c, clock=clock, limit=1, stale_seconds=900, work_lease_seconds=120)
        gate.reserve("g1", "tok", "tk", "c1")
        run1 = gate.acquire_work("g1", "tok")
        clock.advance(121)
        run2 = gate.acquire_work("g1", "tok")
        out, barrier = {}, threading.Barrier(2)

        def commit(name, run):
            barrier.wait()
            out[name] = gate.complete("g1", "tok", f"c-{name}", "tk", run)
        ts = [threading.Thread(target=commit, args=("old", run1)), threading.Thread(target=commit, args=("new", run2))]
        [t.start() for t in ts]; [t.join() for t in ts]
        d = c.find_one({"_id": "g1"})
        # whichever order the two writes land in, only the current lease holder counts: the stale one is fenced (it
        # arrived first) or finds the search already counted (it arrived second) - never "completed"
        assert d["completed"] == 1 and out["new"] == "completed" and out["old"] in ("fenced", "duplicate"), (n, out)
        assert d["last_chat_id"] == "c-new"                          # the resume target is the winner's, never the stale one's


# ───────────────────────── gate level: renewal ───────────────────────────────────────────────────────
def test_a_legitimate_long_search_keeps_its_lease_and_its_reservation_by_renewing(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, limit=1, stale_seconds=300, work_lease_seconds=120)
    gate.reserve("g1", "tok", "tk", "c1")
    run = gate.acquire_work("g1", "tok")
    for _ in range(6):                                               # 6 x 100 s = 600 s, far past one lease and the stale limit
        clock.advance(100)
        assert gate.renew_work("g1", "tok", run) is True
        assert gate.acquire_work("g1", "tok") is None                # nobody else can start a second generation
        assert gate.reserve("g1", "other", "tk2", "c2").kind == gg.BUSY   # and the live reservation is not taken over as stale
    assert gate.complete("g1", "tok", "c1", "tk", run) == "completed"


def test_without_renewal_the_same_long_search_loses_its_lease_and_its_reservation(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, limit=1, stale_seconds=300, work_lease_seconds=120)
    gate.reserve("g1", "tok", "tk", "c1")
    run = gate.acquire_work("g1", "tok")
    clock.advance(301)
    assert gate.acquire_work("g1", "tok")                            # a second runner may start
    assert gate.reserve("g1", "other", "tk2", "c2").reserved         # and the reservation is taken over as stale


def test_an_old_worker_cannot_renew_a_lease_that_was_taken_over(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, limit=1, stale_seconds=900, work_lease_seconds=120)
    gate.reserve("g1", "tok", "tk", "c1")
    run1 = gate.acquire_work("g1", "tok")
    clock.advance(121)
    run2 = gate.acquire_work("g1", "tok")
    before = coll.find_one({"_id": "g1"})["work_lease"]
    assert gate.renew_work("g1", "tok", run1) is False               # superseded: refused
    assert coll.find_one({"_id": "g1"})["work_lease"] == before      # and the new holder's lease is untouched
    assert gate.renew_work("g1", "tok", run2) is True


def test_a_lease_that_expired_but_was_not_taken_over_can_still_be_renewed(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, limit=1, stale_seconds=900, work_lease_seconds=120)
    gate.reserve("g1", "tok", "tk", "c1")
    run = gate.acquire_work("g1", "tok")
    clock.advance(300)
    assert gate.renew_work("g1", "tok", run) is True
    assert gate.acquire_work("g1", "tok") is None


def test_renewal_also_covers_an_already_counted_search_whose_answer_is_still_to_be_stored(gg, coll, clock):
    gate = gg.GuestGate(coll, clock=clock, limit=1, stale_seconds=900, work_lease_seconds=120)
    gate.reserve("g1", "tok", "tk", "c1")
    gate.complete("g1", "tok", "c1", "tk")
    run = gate.acquire_work("g1", "tok")
    clock.advance(100)
    assert gate.renew_work("g1", "tok", run) is True and gate.renew_work("g1", "tok", "not-the-holder") is False


def test_the_renew_hook_is_throttled_and_reports_a_lost_lease(gg, coll, clock, monkeypatch):
    gate = gg.GuestGate(coll, clock=clock, limit=1, stale_seconds=900, work_lease_seconds=120)
    monkeypatch.setattr(gg, "default_gate", lambda: gate)
    gate.reserve("g1", "tok", "tk", "c1")
    lease = {}
    assert gg.on_work_start("g1", "tok", "tk", "c1", lease) == gg.PROCEED
    calls = []
    real = gate.renew_work
    monkeypatch.setattr(gate, "renew_work", lambda *a: calls.append(a) or real(*a))
    for _ in range(5):
        assert gg.on_work_renew("g1", "tok", lease) is True          # inside the first third of the lease: skipped
    assert calls == []
    assert gg.on_work_renew("g1", "tok", lease, force=True) is True and len(calls) == 1   # forced before a model call
    clock.advance(121)
    gate.acquire_work("g1", "tok")                                   # another runner takes the lease over
    assert gg.on_work_renew("g1", "tok", lease, force=True) is False  # this runner is told to stop
    assert gg.on_work_renew("member@example.com", "tok", lease, force=True) is True   # registered owners: not applicable
    assert gg.on_work_renew("g1", "tok", {}, force=True) is True      # no lease dict: not applicable


# ───────────────────────── chat store: compare-and-set answer save ────────────────────────────────────
@pytest.fixture
def cas_env(pipeline, monkeypatch):
    """The answer-pipeline fixture with a REAL conditional-update chat collection (mongomock)."""
    idx = pipeline["idx"]
    chats = mongomock.MongoClient()["fence_chats"]["chats"]
    chats.insert_one({"chat_id": "c1", "owner_key": "guest-1", "owner_type": "anon", "title": "t", "messages": []})
    monkeypatch.setattr(idx, "chats_collection", chats)
    pipeline["mchats"] = chats
    return pipeline


def make_msg(pl, token, topic="topic-x"):
    idx = pl["idx"]
    idx.add_search_to_chat("c1", "guest-1", "Find users looking for AI jobs", topic, ["kw"], "reddit")
    row = pl["mchats"].find_one({"chat_id": "c1"})["messages"][-1]
    row["requested_at"] = dt.datetime(2026, 10, 20, 12, 0, 0)
    row["request_id"] = token
    return row


def headline(row):
    ans = row.get("claude_answer")
    return json.loads(ans).get("headline") if ans else None


def stored(pl, topic="topic-x"):
    msgs = pl["mchats"].find_one({"chat_id": "c1"})["messages"]
    return next(m for m in reversed(msgs) if m["topic_key"] == topic)


def test_answer_save_is_first_writer_wins(cas_env):
    idx = cas_env["idx"]
    make_msg(cas_env, "tok")
    assert idx.save_claude_answer_if_unset("c1", "guest-1", "topic-x", ANSWER_B) is True
    assert idx.save_claude_answer_if_unset("c1", "guest-1", "topic-x", ANSWER_A) is False     # the stale writer loses
    assert stored(cas_env)["claude_answer"] == ANSWER_B and stored(cas_env).get("answered_at")
    assert idx.save_claude_answer_if_unset("c1", "guest-1", "no-such-topic", ANSWER_A) is False
    assert idx.save_claude_answer_if_unset("c1", "guest-1", "topic-x", "") is False


def test_answer_save_only_targets_the_newest_message_with_that_topic(cas_env):
    idx = cas_env["idx"]
    make_msg(cas_env, "tok-1")
    assert idx.save_claude_answer_if_unset("c1", "guest-1", "topic-x", ANSWER_A) is True
    make_msg(cas_env, "tok-2")                                        # the same prompt again -> a second message
    assert idx.save_claude_answer_if_unset("c1", "guest-1", "topic-x", ANSWER_B) is True
    msgs = cas_env["mchats"].find_one({"chat_id": "c1"})["messages"]
    assert [m["claude_answer"] for m in msgs] == [ANSWER_A, ANSWER_B]


def test_two_threads_saving_at_once_store_exactly_one_answer(cas_env, monkeypatch):
    idx = cas_env["idx"]
    monkeypatch.setattr(idx, "chats_collection", Atomic(cas_env["mchats"]))      # MongoDB's per-document atomicity
    for n in range(25):
        topic = f"t{n}"
        make_msg(cas_env, f"tok{n}", topic)
        barrier, wins = threading.Barrier(2), []

        def save(ans):
            barrier.wait()
            wins.append((ans, idx.save_claude_answer_if_unset("c1", "guest-1", topic, ans)))
        ts = [threading.Thread(target=save, args=(a,)) for a in (ANSWER_A, ANSWER_B)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert sorted(w for _, w in wins) == [False, True], wins
        assert stored(cas_env, topic)["claude_answer"] == next(a for a, w in wins if w)


def test_only_gated_guest_searches_use_the_conditional_save(cas_env):
    idx = cas_env["idx"]
    cas_env["mchats"].insert_one({"chat_id": "cm", "owner_key": "member@example.com", "owner_type": "email", "title": "t",
                                  "messages": [{"topic_key": "tk", "claude_answer": "old"}]})
    assert idx.save_answer_for_gate("cm", "member@example.com", "tk", "new", "member@example.com", "tok") is True
    doc = cas_env["mchats"].find_one({"chat_id": "cm"})
    assert doc["messages"][0]["claude_answer"] == "new"               # registered users: the unchanged, unconditional save


# ───────────────────────── pipeline: lease expires while the original worker runs ────────────────────────
def run_worker(pl, msg, model, results, name):
    pl["lg"]._call_claude = model
    pl["idx"]._complete_message_answer_and_results("c1", "guest-1", dict(msg), pl["post"])
    results[name] = "done"


def test_original_worker_finishes_after_a_second_worker_took_over_and_finished(cas_env, gg):
    """Lease expired -> second worker takes over, generates, counts and stores -> the original (slow) worker finishes
    last. It must not count again, must not overwrite, and the stored answer stays the winner's."""
    msg = make_msg(cas_env, "tok-a")
    gg.GuestGate(cas_env["coll"], limit=1).reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    entered, release_a, calls = threading.Event(), threading.Event(), []

    def model_a(*a, **k):
        calls.append("A")
        entered.set()
        assert release_a.wait(10)
        return ANSWER_A
    results = {}
    ta = threading.Thread(target=run_worker, args=(cas_env, msg, model_a, results, "A"))
    cas_env["lg"]._call_claude = model_a
    ta.start()
    assert entered.wait(10)
    expire_lease(cas_env["coll"])                                     # A's lease expires while A is still generating
    # B takes over and runs to completion (own model function; the module-level hook is shared, so swap carefully)
    cas_env["lg"]._call_claude = lambda *a, **k: calls.append("B") or ANSWER_B
    cas_env["idx"]._complete_message_answer_and_results("c1", "guest-1", dict(msg), cas_env["post"])
    assert headline(stored(cas_env, msg["topic_key"])) == "ANSWER-FROM-THE-CURRENT-WORKER"
    assert cas_env["coll"].find_one({"_id": "guest-1"})["completed"] == 1
    release_a.set()
    ta.join(10)
    assert calls == ["A", "B"]                                        # the model calls DID overlap (not exactly-once) ...
    assert headline(stored(cas_env, msg["topic_key"])) == "ANSWER-FROM-THE-CURRENT-WORKER"   # ... but A could not overwrite B
    d = cas_env["coll"].find_one({"_id": "guest-1"})
    assert d["completed"] == 1 and d["inflight"] is None and d["last_chat_id"] == "c1"


def test_original_worker_finishing_while_the_second_worker_is_still_running_is_fenced(cas_env, gg):
    msg = make_msg(cas_env, "tok-a")
    gg.GuestGate(cas_env["coll"], limit=1).reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    a_in, a_go, b_in, b_go = (threading.Event() for _ in range(4))
    calls = []

    def model(*a, **k):
        who = threading.current_thread().name
        calls.append(who)
        if who == "worker-A":
            a_in.set(); assert a_go.wait(10); return ANSWER_A
        b_in.set(); assert b_go.wait(10); return ANSWER_B
    cas_env["lg"]._call_claude = model
    ta = threading.Thread(target=cas_env["idx"]._complete_message_answer_and_results, name="worker-A",
                          args=("c1", "guest-1", dict(msg), cas_env["post"]))
    tb = threading.Thread(target=cas_env["idx"]._complete_message_answer_and_results, name="worker-B",
                          args=("c1", "guest-1", dict(msg), cas_env["post"]))
    ta.start(); assert a_in.wait(10)
    expire_lease(cas_env["coll"])
    tb.start(); assert b_in.wait(10)                                  # both are inside the model call now
    a_go.set(); ta.join(10)                                           # the stale worker finishes FIRST
    d = cas_env["coll"].find_one({"_id": "guest-1"})
    assert d["completed"] == 0 and d["inflight"]["token"] == "tok-a"  # it could not count ...
    assert stored(cas_env, msg["topic_key"]).get("claude_answer") is None   # ... and stored nothing
    b_go.set(); tb.join(10)
    assert headline(stored(cas_env, msg["topic_key"])) == "ANSWER-FROM-THE-CURRENT-WORKER"
    d = cas_env["coll"].find_one({"_id": "guest-1"})
    assert d["completed"] == 1 and d["inflight"] is None


def test_a_worker_that_lost_its_lease_before_the_model_call_makes_no_model_call(cas_env, gg, monkeypatch):
    msg = make_msg(cas_env, "tok-a")
    gate = gg.GuestGate(cas_env["coll"], limit=1)
    gate.reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    gate.acquire_work("guest-1", "tok-a")                             # the CURRENT holder (some other runner)
    real_start = gg.on_work_start

    def stale_start(owner, token, topic, chat, lease=None):
        lease["run"] = "an-older-run-id"                              # this runner believes it holds the lease
        return gg.PROCEED
    monkeypatch.setattr(gg, "on_work_start", stale_start)
    calls = []
    cas_env["lg"]._call_claude = lambda *a, **k: calls.append(1) or ANSWER_A
    cas_env["idx"]._complete_message_answer_and_results("c1", "guest-1", dict(msg), cas_env["post"])
    assert calls == [] and stored(cas_env, msg["topic_key"]).get("claude_answer") is None
    assert cas_env["coll"].find_one({"_id": "guest-1"})["completed"] == 0
    monkeypatch.setattr(gg, "on_work_start", real_start)


def test_crash_then_recovery_is_safe_and_counts_once(cas_env, gg):
    msg = make_msg(cas_env, "tok-a")
    gate = gg.GuestGate(cas_env["coll"], limit=1)
    gate.reserve("guest-1", "tok-a", msg["topic_key"], "c1")
    assert gate.acquire_work("guest-1", "tok-a")                      # the runner dies here: nothing ends its lease
    calls = []
    cas_env["lg"]._call_claude = lambda *a, **k: calls.append(1) or ANSWER_B
    cas_env["idx"]._complete_message_answer_and_results("c1", "guest-1", dict(msg), cas_env["post"])
    assert calls == []                                                # still leased: the retry waits
    expire_lease(cas_env["coll"])
    cas_env["idx"]._complete_message_answer_and_results("c1", "guest-1", dict(msg), cas_env["post"])
    assert calls == [1] and headline(stored(cas_env, msg["topic_key"])) == "ANSWER-FROM-THE-CURRENT-WORKER"
    d = cas_env["coll"].find_one({"_id": "guest-1"})
    assert d["completed"] == 1 and d["inflight"] is None and d.get("work_lease") is None
    cas_env["idx"]._complete_message_answer_and_results("c1", "guest-1", dict(stored(cas_env, msg["topic_key"])), cas_env["post"])
    assert calls == [1] and cas_env["coll"].find_one({"_id": "guest-1"})["completed"] == 1   # a replay does nothing more


def test_a_superseded_search_finishing_after_a_newer_reservation_neither_counts_nor_overwrites(cas_env, gg):
    msg_a = make_msg(cas_env, "tok-a", "topic-a")
    gate = gg.GuestGate(cas_env["coll"], limit=1)
    gate.reserve("guest-1", "tok-a", "topic-a", "c1")
    run_a = gate.acquire_work("guest-1", "tok-a")
    cas_env["coll"].update_one({"_id": "guest-1"}, {"$set": {"inflight.started_at": OLD, "work_lease.until": OLD}})
    assert gate.reserve("guest-1", "tok-b", "topic-b", "c2").reserved       # a newer search takes the slot
    make_msg(cas_env, "tok-b", "topic-b")
    assert cas_env["idx"].save_claude_answer_if_unset("c1", "guest-1", "topic-b", ANSWER_B) is True
    verdict = gg.on_answer_ready("guest-1", "c1", "topic-a", "tok-a", ANSWER_A, lease={"run": run_a})
    assert verdict == gg.DISCARD                                      # A's answer is neither counted nor delivered
    cas_env["idx"]._abandon_guest_message("c1", "guest-1", dict(msg_a), "guest-1")
    assert json.loads(stored(cas_env, "topic-a")["claude_answer"])["answer_status"] == "incomplete"   # only the notice
    assert stored(cas_env, "topic-b")["claude_answer"] == ANSWER_B    # B's stored answer is untouched
    d = cas_env["coll"].find_one({"_id": "guest-1"})
    assert d["completed"] == 0 and d["inflight"]["token"] == "tok-b"
    # the notice itself can never overwrite a real answer
    assert cas_env["idx"].save_claude_answer_if_unset("c1", "guest-1", "topic-b", gg.superseded_answer()) is False


# ───────────────────────── streaming: conflicting output ──────────────────────────────────────────────
def test_nothing_is_streamed_before_the_winning_answer_is_stored(spy):
    spy.reserve()
    order = []
    inner = spy.routes.save_answer_for_gate

    def recording(*a, **k):
        order.append("save")
        return inner(*a, **k)
    spy.routes.save_answer_for_gate = recording
    gen = iter(spy.routes.stream_answer(__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(), "chat-g", "tk").body_iterator)
    for chunk in gen:
        if '"delta"' in chunk:
            order.append("chunk")
        if '"done": true' in chunk:
            break
    gen.close()
    assert order.count("save") == 1 and order[0] == "save" and "chunk" in order        # the save precedes the first chunk


def test_stale_stream_that_finishes_after_the_winner_replays_the_winners_text_only(spy, monkeypatch):
    rt = spy.routes
    spy.reserve()
    a_in, a_go = threading.Event(), threading.Event()
    answers = {"A": ANSWER_A, "B": ANSWER_B}

    def model(*a, **k):
        who = threading.current_thread().name
        spy.analyze_calls.append(who)
        if who == "stream-A":
            a_in.set(); assert a_go.wait(10)
        return answers["A" if who == "stream-A" else "B"]
    monkeypatch.setattr(rt, "analyze_with_claude", model)
    out_a = []
    ta = threading.Thread(target=lambda: out_a.extend(run_stream(spy)), name="stream-A")
    ta.start(); assert a_in.wait(10)
    expire_lease(spy.coll, "owner1")                                  # A's lease expires while A is still generating
    out_b = run_stream(spy)                                           # B (this thread, named MainThread) wins
    a_go.set(); ta.join(10)
    assert spy.analyze_calls.count("stream-A") == 1 and len(spy.analyze_calls) == 2   # two model calls overlapped
    assert "CURRENT-WORKER" in shown(out_b) and "STALE-WORKER" not in shown(out_b)
    assert "CURRENT-WORKER" in shown(out_a) and "STALE-WORKER" not in shown(out_a)    # A replayed B's stored answer
    assert spy.saved == [ANSWER_B]                                    # one stored answer, never overwritten
    d = stream_doc(spy)
    assert d["completed"] == 1 and d["inflight"] is None


def test_stale_stream_fenced_while_the_winner_is_still_running_waits_and_replays(spy, monkeypatch):
    rt = spy.routes
    spy.reserve()
    a_in, a_go, b_in, b_go = (threading.Event() for _ in range(4))

    def model(*a, **k):
        who = threading.current_thread().name
        spy.analyze_calls.append(who)
        if who == "stream-A":
            a_in.set(); assert a_go.wait(10); return ANSWER_A
        b_in.set(); assert b_go.wait(10); return ANSWER_B
    monkeypatch.setattr(rt, "analyze_with_claude", model)
    out_a, out_b = [], []
    ta = threading.Thread(target=lambda: out_a.extend(run_stream(spy)), name="stream-A")
    tb = threading.Thread(target=lambda: out_b.extend(run_stream(spy)), name="stream-B")
    ta.start(); assert a_in.wait(10)
    expire_lease(spy.coll, "owner1")
    tb.start(); assert b_in.wait(10)
    a_go.set()                                                        # the stale stream's model returns first ...
    time.sleep(0.4)
    assert spy.saved == [] and stream_doc(spy)["completed"] == 0       # ... it is fenced: nothing counted or stored
    assert "STALE-WORKER" not in shown(out_a)                         # and no chunk of its own text has been sent
    b_go.set()
    ta.join(15); tb.join(15)
    assert spy.saved == [ANSWER_B] and stream_doc(spy)["completed"] == 1
    assert "CURRENT-WORKER" in shown(out_b) and "CURRENT-WORKER" in shown(out_a) and "STALE-WORKER" not in shown(out_a)


def test_a_second_tab_after_the_winner_is_stored_gets_the_stored_answer_without_a_model_call(spy):
    spy.reserve()
    run_stream(spy)
    spy.analyze_calls.clear()
    out = run_stream(spy)                                             # refresh / retry / second tab: the cached replay
    assert spy.analyze_calls == [] and GOOD_ANSWER in shown(out) and len(spy.saved) == 1 and stream_doc(spy)["completed"] == 1


# ───────────────────────── random interleavings with leases ───────────────────────────────────────────
@pytest.mark.parametrize("limit", [1, 2])
def test_model_based_invariants_hold_with_leases_renewals_and_fenced_commits(gg, clock, limit):
    import random
    rng = random.Random(77 + limit)
    tokens = [f"t{i}" for i in range(4)]
    for seq in range(120):
        c = mongomock.MongoClient()[f"fence{limit}_{seq}"]["g"]
        gate = gg.GuestGate(c, clock=clock, limit=limit, stale_seconds=900, work_lease_seconds=120)
        runs = {t: [] for t in tokens}
        for step in range(50):
            tok = rng.choice(tokens)
            op = rng.choice(["reserve", "acquire", "renew", "complete", "complete", "release", "age", "revert"])
            before = c.find_one({"_id": "g1"}) or {}
            if op == "reserve":
                gate.reserve("g1", tok, "tk", f"chat-{tok}")
            elif op == "acquire":
                run = gate.acquire_work("g1", tok)
                if run:
                    runs[tok].append(run)
            elif op == "renew" and runs[tok]:
                run = rng.choice(runs[tok])
                ok = gate.renew_work("g1", tok, run)
                lease = (c.find_one({"_id": "g1"}) or {}).get("work_lease") or {}
                if ok:
                    assert lease.get("run") == run, (seq, step)           # only the holder renews
            elif op == "complete":
                run = rng.choice(runs[tok]) if runs[tok] else None
                lease = before.get("work_lease") or {}
                held_by_other_live = bool(lease) and run and lease.get("run") != run and gg._aware(lease.get("until")) > clock()
                outcome = gate.complete("g1", tok, f"chat-{tok}", "tk", run)
                if held_by_other_live:
                    assert outcome != "completed", (seq, step, outcome)   # a taken-over runner never counts
            elif op == "release":
                gate.release("g1", tok)
            elif op == "revert":
                gate.revert("g1", tok)
            else:
                clock.advance(rng.choice([10, 100, 500, 901]))
            d = c.find_one({"_id": "g1"})
            if d is None:
                continue
            counted = d.get("completed_tokens") or []
            assert 0 <= d["completed"] <= limit and d["completed"] == len(counted) == len(set(counted)), (seq, step, d)
            assert d.get("inflight") is None or d["inflight"]["token"] not in counted, (seq, step, d)


# ───────────────────────── retries replay the stored answer ───────────────────────────────────────────
@pytest.mark.usefixtures("flat_cost")
def test_retries_replay_the_saved_answer_without_a_second_charge_or_job(e2e):
    client, chat_id = _guest_search(e2e)
    guest_id = gid_of(e2e["idx"], client)
    token = e2e["gate_coll"].find_one({"_id": guest_id})["inflight"]["token"]
    msg = e2e["db"].chats.find_one({"chat_id": chat_id})["messages"][-1]
    charged = e2e["db"].flintel_credit_usage.find_one({"owner_key": guest_id})["used_by_period"]
    jobs = e2e["db"].jobs.count_documents({})
    lg, idx = e2e["lg"], e2e["idx"]
    calls = []
    lg._call_claude = lambda *a, **k: calls.append(1) or json.dumps(GOOD)
    msg["requested_at"] = dt.datetime(2026, 10, 20, 12, 0, 0)
    idx._complete_message_answer_and_results(chat_id, guest_id, dict(msg), [])
    answer = e2e["db"].chats.find_one({"chat_id": chat_id})["messages"][-1]["claude_answer"]
    assert answer and calls == [1] and e2e["gate_coll"].find_one({"_id": guest_id})["completed"] == 1
    # retries: the same submit again, a second worker run, and the stream route (the browser's reconnect)
    again = post_search(client, KEY1)
    assert again.status_code == 303 and again.headers["location"].endswith(chat_id)
    reloaded = e2e["db"].chats.find_one({"chat_id": chat_id})["messages"][-1]      # what a worker really loads: already answered
    reloaded["requested_at"] = dt.datetime(2026, 10, 20, 12, 0, 0)
    idx._complete_message_answer_and_results(chat_id, guest_id, dict(reloaded), [])
    replay = client.get(f"/chat/{chat_id}/stream", params={"topic_key": msg["topic_key"]})
    assert replay.status_code == 200 and "retry:" in replay.text
    assert json.loads([ln for ln in replay.text.splitlines() if ln.startswith("data:") and '"delta"' in ln][0][6:])["delta"] == answer
    assert calls == [1]                                                          # no further model call
    assert e2e["db"].flintel_credit_usage.find_one({"owner_key": guest_id})["used_by_period"] == charged   # no second charge
    assert e2e["db"].jobs.count_documents({}) == jobs and len(e2e["state"]["enqueues"]) == 1                # no duplicate job
    assert e2e["db"].chats.find_one({"chat_id": chat_id})["messages"][-1]["claude_answer"] == answer         # answer unchanged
    assert e2e["gate_coll"].find_one({"_id": guest_id})["completed"] == 1
