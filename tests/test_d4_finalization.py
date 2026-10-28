"""
tests/test_d4_finalization.py — D4: a turn that carries the durable `pending_answer` state is finalized by compare-and-set, so two
concurrent finalizers cannot both store the answer or both append the chat summary; turns WITHOUT that state keep the old
unconditional save. Plus the legacy-message (no request id) safety guard. mongomock: proves the decisions, not MongoDB's atomicity.
"""
import threading
import time

import pytest

from tests.test_multiworker_recovery import (  # noqa: F401  (fixtures + helpers)
    mw, bg, conc, env, app_env, fin, live_server, Q_A, Q_B, OWNER, POSTS, _tk, send, chat_of, wait, msg_of, _race, another_worker,
    abortable_stream, _msg,
)


def _turn(f, q=Q_A):
    chat = chat_of(send(f, q))
    return chat, msg_of(f, chat)["request_id"]


def _spy_summary(monkeypatch, *modules):
    calls = []
    for mod in modules:
        real = mod.append_to_chat_summary
        monkeypatch.setattr(mod, "append_to_chat_summary", lambda *a, _r=real, **k: (calls.append(a[2:4]), _r(*a, **k))[1])
    return calls


# ── concurrent finalizers ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_of_many_concurrent_finalizers_of_a_pending_turn_exactly_one_stores_its_answer(mw):
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    assert idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid, "raw")
    wins = _race(16, lambda i: idx.save_answer_for_gate(chat, OWNER, _tk(Q_A), f"final {i}", OWNER, rid))
    assert wins.count(True) == 1 and wins.count(False) == 15, wins
    m = msg_of(f, chat)
    assert m["claude_answer"].startswith("final ") and m["answer_cas"] is True
    assert "pending_answer" not in m and "scan_lease_at" not in m
    assert msg_of(f, chat)["claude_answer"] == m["claude_answer"]                # and it never changes afterwards
    assert idx.save_answer_for_gate(chat, OWNER, _tk(Q_A), "a LATE finalizer", OWNER, rid) is False   # even after pending was removed
    assert msg_of(f, chat)["claude_answer"] == m["claude_answer"]


def test_two_background_runners_finalizing_the_same_pending_turn_append_the_summary_once(bg, monkeypatch):
    f, idx = bg, bg["idx"]
    chat, rid = _turn(f)
    msg = _msg(f, chat)
    assert idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid, f["calls"] and "raw" or "raw")
    summaries = _spy_summary(monkeypatch, idx)
    threads = [threading.Thread(target=idx._complete_message_answer_and_results, args=(chat, OWNER, dict(msg), list(POSTS)), daemon=True)
               for _ in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert f["calls"]["n"] == 0                                                   # both reused the kept answer: no model call
    assert len(summaries) == 1                                                    # the summary was appended by the winner only
    m = msg_of(f, chat)
    assert m["claude_answer"] and m["answer_cas"] is True


def test_a_stream_that_finishes_a_dead_workers_turn_stores_and_summarizes_once(mw, monkeypatch):
    f, ctl, rt = mw, mw["ctl"], mw["rt"]
    chat, rid = _turn(f)
    summaries = _spy_summary(monkeypatch, rt)
    real_patch = rt._patch_post_urls_into_answer
    died = {"n": 0}

    def die_once(*a, **k):
        if not died["n"]:
            died["n"] = 1
            raise SystemExit("killed after the model call")
        return real_patch(*a, **k)
    monkeypatch.setattr(rt, "_patch_post_urls_into_answer", die_once)
    a = abortable_stream(f, chat, Q_A)
    assert a.joined(20) and msg_of(f, chat)["pending_answer"] and not summaries
    another_worker(f)
    b = abortable_stream(f, chat, Q_A)
    assert b.joined(20) and '"done": true' in b.text()
    assert len(summaries) == 1 and msg_of(f, chat)["answer_cas"] is True and len(ctl.analyze_started) == 1
    # a straggler of the same attempt that finishes later changes nothing and appends nothing
    assert f["idx"].finalize_pending_answer(chat, OWNER, _tk(Q_A), rid, "straggler") is False
    assert len(summaries) == 1 and msg_of(f, chat)["claude_answer"] != "straggler"


# ── legacy turns keep the unconditional save ──────────────────────────────────────────────────────────────────────────────────
def test_a_turn_without_pending_state_keeps_the_old_unconditional_save(mw):
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    assert idx.finalize_pending_answer(chat, OWNER, _tk(Q_A), rid, "x") is None   # no pending state: the caller's legacy path runs
    assert idx.save_answer_for_gate(chat, OWNER, _tk(Q_A), "first", OWNER, rid) is True
    assert idx.save_answer_for_gate(chat, OWNER, _tk(Q_A), "second", OWNER, rid) is True     # unconditional, exactly as before
    m = msg_of(f, chat)
    assert m["claude_answer"] == "second" and "answer_cas" not in m
    f["chats"].update_one({"chat_id": chat}, {"$unset": {"messages.0.request_id": ""}})       # a message that never had a request id
    idx.save_pending_answer(chat, OWNER, _tk(Q_A), None, "raw")                                # (no id: no durable state either)
    assert idx.finalize_pending_answer(chat, OWNER, _tk(Q_A), None, "x") is None


# ── Stop and explicit retry ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_stopped_pending_turn_is_never_finalized(mw):
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid, "raw")
    assert idx.mark_turn_cancelled(chat, OWNER, topic_key=_tk(Q_A)) == idx.CANCEL_CANCELLED   # handled by another worker
    another_worker(f)
    assert idx.finalize_pending_answer(chat, OWNER, _tk(Q_A), rid, "late final") is False
    assert idx.save_answer_for_gate(chat, OWNER, _tk(Q_A), "late final", OWNER, rid) is False
    m = msg_of(f, chat)
    assert m["cancelled_at"] and m["claude_answer"] is None and "answer_cas" not in m


def test_a_late_finalizer_of_attempt_one_cannot_land_on_the_retry(mw):
    f, idx = mw, mw["idx"]
    chat, rid1 = _turn(f)
    idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid1, "attempt 1 raw")
    assert idx.mark_turn_failed(chat, OWNER, _tk(Q_A), rid1) is True
    chat2 = chat_of(send(f, Q_A, chat_id=chat))                                       # explicit retry
    rid2 = f["chats"].find_one({"chat_id": chat})["messages"][1]["request_id"]
    assert chat2 == chat and rid2 != rid1
    idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid2, "attempt 2 raw")
    assert idx.finalize_pending_answer(chat, OWNER, _tk(Q_A), rid1, "attempt 1 LATE final") is True    # an answer wins over a failure
    msgs = f["chats"].find_one({"chat_id": chat})["messages"]
    assert msgs[0]["claude_answer"] == "attempt 1 LATE final"                         # it landed on ITS OWN message ...
    assert msgs[1]["claude_answer"] is None and msgs[1]["pending_answer"] == "attempt 2 raw"   # ... never on the retry
    assert idx.finalize_pending_answer(chat, OWNER, _tk(Q_A), rid2, "attempt 2 final") is True
    assert f["chats"].find_one({"chat_id": chat})["messages"][1]["claude_answer"] == "attempt 2 final"


# ── legacy messages without a request id ───────────────────────────────────────────────────────────────────────────────────────
def _legacy_then_retry(f):
    chat, rid = _turn(f)
    f["chats"].update_one({"chat_id": chat}, {"$unset": {"messages.0.request_id": ""}})   # an old message: no request id
    chat2 = chat_of(send(f, Q_A, chat_id=chat))                                          # a retry of the same text: has one
    assert chat2 == chat
    msgs = f["chats"].find_one({"chat_id": chat})["messages"]
    assert "request_id" not in msgs[0] and msgs[1]["request_id"]
    return chat, msgs[1]["request_id"]


def test_a_stale_worker_of_a_legacy_message_cannot_write_run_state_onto_the_retry(mw):
    f, idx = mw, mw["idx"]
    chat, rid2 = _legacy_then_retry(f)
    t = _tk(Q_A)
    assert idx.claim_scan_lease(chat, OWNER, t, None) is True            # no turn it may act on: fail-open "scan", nothing recorded
    assert idx.save_pending_answer(chat, OWNER, t, None, "stale legacy raw") is False
    assert idx.mark_scan_done(chat, OWNER, t, None) is False
    assert idx.clear_scan_lease(chat, OWNER, t, None) is False
    assert idx.mark_turn_failed(chat, OWNER, t, None) is False
    assert idx.finalize_pending_answer(chat, OWNER, t, None, "stale legacy final") is None
    retry = f["chats"].find_one({"chat_id": chat})["messages"][1]
    assert not any(retry.get(k) for k in ("pending_answer", "scan_lease_at", "scan_lease_id", "scan_done_at", "failed_at", "answer_cas"))
    assert retry["claude_answer"] is None


def test_a_legacy_turn_with_no_retry_still_works_without_a_request_id(mw):
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    f["chats"].update_one({"chat_id": chat}, {"$unset": {"messages.0.request_id": ""}})
    t = _tk(Q_A)
    assert idx.claim_scan_lease(chat, OWNER, t, None)
    assert idx.save_pending_answer(chat, OWNER, t, None, "raw") is True
    assert idx.read_run_state(chat, OWNER, t, None)["pending_answer"] == "raw"


def test_KNOWN_LIMITATION_the_pre_existing_plain_saves_still_resolve_to_the_newest_message_of_a_topic(mw):
    """Documents what is NOT changed: save_claude_answer_to_chat / save_signal_results_to_chat / _set_on_latest_message address the
    NEWEST message with the topic (the 'BUG 3 FIX' design) because they carry no request id. A stale worker of a legacy turn using
    those plain saves could still write onto a newer retry. Closing it means changing every legacy writer - a broad change."""
    f, idx = mw, mw["idx"]
    chat, rid2 = _legacy_then_retry(f)
    idx.save_claude_answer_to_chat(chat, OWNER, _tk(Q_A), "stale legacy answer")
    msgs = f["chats"].find_one({"chat_id": chat})["messages"]
    assert msgs[0]["claude_answer"] is None and msgs[1]["claude_answer"] == "stale legacy answer"
