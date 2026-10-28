"""
Validation-stage audit tests for p0_workers.patch (NOT part of the patch). Each test states the property that was asked about.
mongomock only: they check the code's decisions, not MongoDB's atomicity.
"""
import datetime as dt
import threading

import pytest

from tests.test_multiworker_recovery import (  # noqa: F401  (fixtures + helpers)
    mw, conc, env, app_env, fin, live_server, Q_A, Q_B, OWNER, POSTS, _tk, send, chat_of, wait, msg_of, busy_keys, slot_holders,
    _race, another_worker, _run_fields,
)

UTC = dt.timezone.utc


def _turn(f, q=Q_A):
    chat = chat_of(send(f, q))
    return chat, msg_of(f, chat)["request_id"]


def _age_lease(f, chat, seconds):
    f["chats"].update_one({"chat_id": chat}, {"$set": {"messages.0.scan_lease_at": dt.datetime.now(UTC) - dt.timedelta(seconds=seconds)}})


# 1. two workers cannot both claim the same scan ---------------------------------------------------------------------------
def test_only_one_worker_claims_a_scan_and_a_stale_lease_is_taken_by_exactly_one_more(mw):
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    assert sum(1 for g in _race(24, lambda i: idx.claim_scan_lease(chat, OWNER, _tk(Q_A), rid)) if g) == 1
    _age_lease(f, chat, idx.SCAN_LEASE_STALE_SECONDS + 10)
    assert sum(1 for g in _race(24, lambda i: idx.claim_scan_lease(chat, OWNER, _tk(Q_A), rid)) if g) == 1


# 3. a stale worker must not overwrite the state of a newer lease ----------------------------------------------------------
def test_a_stale_scan_thread_cannot_clear_or_finish_over_a_newer_workers_lease(mw):
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    a_id = idx.claim_scan_lease(chat, OWNER, _tk(Q_A), rid)                    # worker A claims ...
    _age_lease(f, chat, idx.SCAN_LEASE_STALE_SECONDS + 10)                     # ... stalls, its lease goes stale
    b_id = idx.claim_scan_lease(chat, OWNER, _tk(Q_A), rid)                    # worker B takes over and is scanning now
    assert a_id and b_id and a_id != b_id
    b_lease = msg_of(f, chat)["scan_lease_at"]
    idx.clear_scan_lease(chat, OWNER, _tk(Q_A), rid, a_id)                     # A finally fails / is cancelled
    assert msg_of(f, chat).get("scan_lease_at") == b_lease, "A's late clear removed B's live lease"


def test_a_stale_scan_thread_cannot_mark_done_over_a_newer_workers_lease(mw):
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    a_id = idx.claim_scan_lease(chat, OWNER, _tk(Q_A), rid)
    _age_lease(f, chat, idx.SCAN_LEASE_STALE_SECONDS + 10)
    b_id = idx.claim_scan_lease(chat, OWNER, _tk(Q_A), rid)                    # B owns the lease
    idx.mark_scan_done(chat, OWNER, _tk(Q_A), rid, a_id)                       # A (stale) reports "done"
    m = msg_of(f, chat)
    assert m.get("scan_lease_at") is not None and m.get("scan_lease_id") == b_id and not m.get("scan_done_at"), "A's late done-marker overwrote B's lease"
    idx.mark_scan_lease(chat, OWNER, _tk(Q_A), rid, a_id)                      # nor can A's late heartbeat renew it
    before = msg_of(f, chat)["scan_lease_at"]
    idx.mark_scan_lease(chat, OWNER, _tk(Q_A), rid, a_id)
    assert msg_of(f, chat)["scan_lease_at"] == before
    assert idx.mark_scan_done(chat, OWNER, _tk(Q_A), rid, b_id) is True        # the real holder's done-marker lands
    assert msg_of(f, chat)["scan_done_at"] and "scan_lease_id" not in msg_of(f, chat)


# 2. pending answers --------------------------------------------------------------------------------------------------------
def test_the_first_pending_answer_is_kept_a_later_writer_does_not_replace_it(mw):
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    assert idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid, "answer from worker A")
    idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid, "answer from stale worker B")
    assert msg_of(f, chat)["pending_answer"] == "answer from worker A"


def test_a_pending_answer_survives_until_the_final_answer_is_stored_and_is_then_removed(mw):
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid, "raw")
    another_worker(f)
    assert idx.read_run_state(chat, OWNER, _tk(Q_A), rid)["pending_answer"] == "raw"      # a crash between the two writes loses nothing
    assert idx.save_claude_answer_if_unset(chat, OWNER, _tk(Q_A), "final") is True
    assert idx.save_claude_answer_if_unset(chat, OWNER, _tk(Q_A), "second finalizer") is False   # CAS: stored once
    m = msg_of(f, chat)
    assert m["claude_answer"] == "final" and "pending_answer" not in m


@pytest.mark.xfail(strict=True, reason="BY DESIGN (D4 narrowed): a turn WITHOUT durable pending_answer state keeps the legacy unconditional save; turns WITH it are CAS (tests/test_d4_finalization.py)")
def test_two_concurrent_finalizers_of_a_registered_users_turn_store_the_answer_once(mw):
    """save_answer_for_gate() for a registered user (no gate token) uses the plain, non-CAS save. Two finalizers of one turn
    (possible only after a lease was lost) must not both 'win'."""
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    wins = _race(8, lambda i: idx.save_answer_for_gate(chat, OWNER, _tk(Q_A), f"final {i}", OWNER, None))
    stored = msg_of(f, chat)["claude_answer"]
    assert sum(1 for w in wins if w is True) == 1, f"{wins} - last writer wins: {stored!r}"


# 4. Stop handled by another worker blocks every later write ----------------------------------------------------------------
def test_after_a_stop_written_by_another_worker_no_later_write_lands(mw):
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)
    assert idx.mark_turn_cancelled(chat, OWNER, topic_key=_tk(Q_A)) == idx.CANCEL_CANCELLED     # the other worker's stop
    another_worker(f)                                                                         # this worker has never heard of it
    assert idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid, "late raw") is False
    assert idx.claim_scan_lease(chat, OWNER, _tk(Q_A), rid) is False
    assert idx.mark_scan_done(chat, OWNER, _tk(Q_A), rid) is False
    assert idx.save_claude_answer_if_unset(chat, OWNER, _tk(Q_A), "late final") is False
    idx.save_claude_answer_to_chat(chat, OWNER, _tk(Q_A), "late plain final")
    idx.save_signal_results_to_chat(chat, OWNER, _tk(Q_A), POSTS[:2])
    idx.save_search_progress_to_chat(chat, OWNER, _tk(Q_A), {"intro": "x"})
    m = msg_of(f, chat)
    assert m["cancelled_at"] and m["claude_answer"] is None and m["results"] is None and not m.get("search_progress")
    assert not any(m.get(k) for k in ("pending_answer", "scan_lease_at", "scan_done_at"))


# 5. a genuine retry is a new request ----------------------------------------------------------------------------------------
def test_a_retry_cannot_inherit_state_and_the_old_attempts_late_writes_miss_it(mw):
    f, idx = mw, mw["idx"]
    chat, rid1 = _turn(f)
    idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid1, "attempt 1 raw")
    idx.mark_scan_done(chat, OWNER, _tk(Q_A), rid1)
    assert idx.mark_turn_failed(chat, OWNER, _tk(Q_A), rid1) is True
    chat2 = chat_of(send(f, Q_A, chat_id=chat))                                # explicit retry: same chat, same text
    assert chat2 == chat
    msgs = f["chats"].find_one({"chat_id": chat})["messages"]
    rid2 = msgs[1]["request_id"]
    assert rid2 != rid1
    assert idx.read_run_state(chat, OWNER, _tk(Q_A), rid2) == {"scan_running": False, "scan_done": False, "pending_answer": None,
                                                                  "answered": False, "closed": False}
    idx.save_pending_answer(chat, OWNER, _tk(Q_A), rid1, "attempt 1 LATE raw")       # attempt 1 is still alive somewhere
    idx.mark_scan_done(chat, OWNER, _tk(Q_A), rid1)
    msgs = f["chats"].find_one({"chat_id": chat})["messages"]
    assert not any(msgs[1].get(k) for k in ("pending_answer", "scan_done_at", "scan_lease_at"))


# competing reclaim of a stale busy flag / background task ---------------------------------------------------------------
def test_competing_reclaims_of_a_stale_background_flag_have_one_winner(mw):
    f, idx = mw, mw["idx"]
    key = f"{OWNER}::bg"
    assert idx._take_busy(key)
    f["busy"].update_many({}, {"$set": {"started_at": dt.datetime.now(UTC) - dt.timedelta(seconds=idx.BUSY_FLAG_TIMEOUT_SECONDS + 60)}})
    assert _race(24, lambda i: idx._take_busy(key)).count(True) == 1


# fail-open behaviour, documented -----------------------------------------------------------------------------------------------
def test_DOCUMENTED_scan_lease_claim_fails_open_when_the_database_errors(mw, monkeypatch):
    """Current behaviour: if the claim itself errors, EVERY racer is told to scan (True). Same as before the patch (every stream
    scanned); it can duplicate a read-only scan during a database outage, never a collector job, credit or message."""
    f, idx = mw, mw["idx"]
    chat, rid = _turn(f)

    class Broken:
        def find_one(self, *a, **k):
            raise RuntimeError("down")
    monkeypatch.setattr(idx, "chats_collection", Broken())
    assert all(_race(8, lambda i: idx.claim_scan_lease(chat, OWNER, _tk(Q_A), rid)))
