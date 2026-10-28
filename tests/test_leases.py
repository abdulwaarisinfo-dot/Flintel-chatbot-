"""
tests/test_leases.py — Issue 2: a live search keeps its slot and busy flag by heartbeat; a crashed/abandoned one is reclaimed.
Everything here is deterministic: stale-ness is driven by backdating `started_at` or by tiny, patched lease constants with wide
margins; no test depends on a 400 s wait. (mongomock: it proves the logic, not production atomicity.)
"""
import datetime as dt
import threading
import time

import pytest

from tests.test_independent_chats_stop_retry import (  # noqa: F401  (fixtures + helpers)
    conc, env, app_env, fin, live_server, Q_A, Q_B, _tk, send, chat_of, start_stream, wait, cancel, msg_of, busy_keys,
    slot_holders, _race, Flaky, flaky, OWNER,
)

UTC = dt.timezone.utc


def _backdate(f, seconds, **flt):
    old = dt.datetime.now(UTC) - dt.timedelta(seconds=seconds)
    f["busy"].update_many(flt, {"$set": {"started_at": old}})


def _acquire(idx, owner, chat):
    holder = f"{owner}::{chat}"
    assert idx.reserve_search_slot(owner, holder, 1) is True
    assert idx.acquire_owner_busy(holder) is True
    return holder


def test_without_a_beat_a_search_older_than_the_timeout_is_reclaimable_control(conc):
    f, idx = conc, conc["idx"]
    holder = _acquire(idx, "lease-owner", "c1")
    _backdate(f, idx.BUSY_FLAG_TIMEOUT_SECONDS + 600)                          # "has been running for ~17 minutes" without a beat
    assert idx.reserve_search_slot("lease-owner", "lease-owner::c2", 1) is True       # the documented risk: the cap is lost
    assert idx._is_owner_busy(holder) is False                                 # ... and so is the same-chat protection


def test_a_heartbeat_keeps_a_search_running_far_beyond_the_old_timeout_live(conc):
    f, idx = conc, conc["idx"]
    holder = _acquire(idx, "lease-owner", "c1")
    _backdate(f, idx.BUSY_FLAG_TIMEOUT_SECONDS + 600)
    assert idx.refresh_busy_lease(holder) is True                              # one heartbeat
    assert idx._is_owner_busy(holder) is True                                  # same-chat duplicate protection
    assert idx.reserve_search_slot("lease-owner", "lease-owner::c2", 1) is False       # the owner cap holds
    assert idx.acquire_owner_busy(holder) is False


def test_with_the_heartbeat_thread_a_live_search_outlives_the_stale_limit_and_an_abandoned_one_does_not(conc, monkeypatch):
    f, idx = conc, conc["idx"]
    monkeypatch.setattr(idx, "LEASE_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(idx, "LEASE_STALE_SECONDS", 0.5)
    holder = _acquire(idx, "lease-owner", "c1")
    stop = idx.start_lease_heartbeat(holder)
    time.sleep(1.6)                                                            # > 3 x the stale limit
    assert idx.reserve_search_slot("lease-owner", "lease-owner::c2", 1) is False       # still live
    assert idx.acquire_owner_busy(holder) is False
    stop()                                                                     # the process "dies": no more beats
    time.sleep(0.9)                                                            # > the stale limit
    assert idx.reserve_search_slot("lease-owner", "lease-owner::c2", 1) is True         # reclaimed after the defined timeout
    assert idx.acquire_owner_busy(holder) is True


def test_an_abandoned_search_is_reclaimed_after_its_timeout_whichever_kind_of_document(conc):
    f, idx = conc, conc["idx"]
    holder = _acquire(idx, "lease-owner", "c1")
    idx.refresh_busy_lease(holder)                                             # a beating search that then stops beating
    _backdate(f, idx.LEASE_STALE_SECONDS + 5)
    assert idx.reserve_search_slot("lease-owner", "lease-owner::c2", 1) is True
    assert idx.acquire_owner_busy(holder) is True
    f["busy"].delete_many({})
    holder = _acquire(idx, "lease-owner", "c3")                                # a never-beaten one: the fixed 400 s rule
    _backdate(f, idx.BUSY_FLAG_TIMEOUT_SECONDS - 10)
    assert idx.reserve_search_slot("lease-owner", "lease-owner::c4", 1) is False       # not yet
    _backdate(f, idx.BUSY_FLAG_TIMEOUT_SECONDS + 10)
    assert idx.reserve_search_slot("lease-owner", "lease-owner::c4", 1) is True


def test_a_failing_heartbeat_is_reported_never_raised_and_the_search_is_not_lost_by_a_short_outage(conc, flaky, monkeypatch):
    f, idx = conc, conc["idx"]
    monkeypatch.setattr(idx, "LEASE_HEARTBEAT_SECONDS", 0.05)
    holder = _acquire(idx, "lease-owner", "c1")
    flaky.down = True
    assert idx.refresh_busy_lease(holder) is False                             # reported, not raised
    assert idx.store_is_degraded()
    stop = idx.start_lease_heartbeat(holder)
    time.sleep(0.4)                                                            # the thread survives a store outage
    flaky.down = False
    idx._degraded["until"] = 0.0
    _backdate(f, idx.LEASE_STALE_SECONDS - 30)                                 # a short outage: still inside the lease
    time.sleep(0.4)                                                            # the thread beats again by itself
    doc = f["busy"].find_one({"owner_key": holder})
    assert doc and doc.get("lease") is True and (dt.datetime.now(UTC) - doc["started_at"].replace(tzinfo=UTC)).total_seconds() < 5
    stop()


def test_a_heartbeat_re_takes_documents_that_were_reclaimed_from_a_live_search(conc, monkeypatch):
    f, idx = conc, conc["idx"]
    monkeypatch.setattr(idx, "LEASE_HEARTBEAT_SECONDS", 0.05)
    holder = _acquire(idx, "lease-owner", "c1")
    f["busy"].delete_many({})                                                  # e.g. cleared by an operator / a long outage
    assert idx.refresh_busy_lease(holder) is False                             # a refresh never resurrects a reclaimed lease
    stop = idx.start_lease_heartbeat(holder)
    assert wait(lambda: f["busy"].find_one({"owner_key": holder}) is not None, 3)    # the live search takes its flag back
    stop()


def test_competing_reclaims_of_a_stale_slot_have_exactly_one_winner(conc):
    f, idx = conc, conc["idx"]
    _acquire(idx, "lease-owner", "dead")
    _backdate(f, idx.BUSY_FLAG_TIMEOUT_SECONDS + 50)
    got = _race(16, lambda i: idx.reserve_search_slot("lease-owner", f"lease-owner::new{i}", 1))
    assert got.count(True) == 1 and got.count(False) == 15, got
    assert len(slot_holders(f, "lease-owner")) == 1


def test_a_refresh_that_lands_before_a_reclaim_makes_the_reclaim_miss(conc):
    f, idx = conc, conc["idx"]
    holder = _acquire(idx, "lease-owner", "c1")
    _backdate(f, idx.BUSY_FLAG_TIMEOUT_SECONDS + 50)
    slot = f["busy"].find_one({"owner_key": {"$regex": "#slot"}})
    observed = slot["started_at"]                                              # what a reclaimer read as stale ...
    assert idx.refresh_busy_lease(holder) is True                              # ... then the live holder beats
    res = f["busy"].delete_one({"owner_key": slot["owner_key"], "started_at": observed})   # the reclaimer's conditional delete
    assert res.deleted_count == 0 and f["busy"].find_one({"owner_key": slot["owner_key"]}) is not None


def test_completion_and_cancellation_stop_the_heartbeat_and_free_everything(conc, monkeypatch):
    f, ctl, idx = conc, conc["ctl"], conc["idx"]
    monkeypatch.setattr(idx, "LEASE_HEARTBEAT_SECONDS", 0.05)
    base = sum(t.name == "busy-lease" for t in threading.enumerate())
    ctl.hold(Q_A, scan=True)
    chat = chat_of(send(f, Q_A))
    s = start_stream(f, chat, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    assert wait(lambda: sum(t.name == "busy-lease" for t in threading.enumerate()) == base + 1, 3)
    ctl.release(Q_A)                                                           # completion
    assert s.joined(10) and "done" in s.kinds()
    assert wait(lambda: sum(t.name == "busy-lease" for t in threading.enumerate()) == base, 3)
    assert busy_keys(f) == [] and slot_holders(f) == []
    ctl.hold(Q_B, scan=True)
    chat_b = chat_of(send(f, Q_B))
    sb = start_stream(f, chat_b, Q_B)
    assert wait(lambda: _tk(Q_B) in ctl.scan_started)
    assert cancel(f, chat_b, Q_B)[1]["status"] == "cancelled"                   # cancellation
    assert sb.joined(10)
    assert wait(lambda: sum(t.name == "busy-lease" for t in threading.enumerate()) == base, 3)
    assert busy_keys(f) == [] and slot_holders(f) == []


def test_a_running_stream_keeps_the_cap_and_duplicate_protection_after_its_documents_age(conc, monkeypatch):
    f, ctl, idx = conc, conc["ctl"], conc["idx"]
    monkeypatch.setattr(idx, "LEASE_HEARTBEAT_SECONDS", 0.1)
    monkeypatch.setattr(f["rt"].config, "MAX_CONCURRENT_SEARCHES_PER_OWNER", 1, raising=False)
    ctl.hold(Q_A, scan=True)
    chat = chat_of(send(f, Q_A))
    start_stream(f, chat, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    for _ in range(3):                                                         # "the search has been running for 400+ s", thrice
        _backdate(f, idx.BUSY_FLAG_TIMEOUT_SECONDS + 100)
        assert wait(lambda: all((dt.datetime.now(UTC) - d["started_at"].replace(tzinfo=UTC)).total_seconds() < 5
                                for d in f["busy"].find({})), 3)               # the next beat renews them
        assert send(f, Q_B).status_code == 200                                 # cap still holds
        assert send(f, Q_A, chat_id=chat).status_code == 200                   # same-chat duplicate still declined
    ctl.release(Q_A)
