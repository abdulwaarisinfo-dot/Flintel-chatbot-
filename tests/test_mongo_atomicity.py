"""
tests/test_mongo_atomicity.py — the database operations the P0 cap and Stop depend on, run on two backends.

  * mongomock (always): proves the CODE's logic (who wins, what is refused). It is NOT proof of production atomicity -
    mongomock is an in-process emulation, so these results say nothing about how a real MongoDB serialises the updates.
  * a real MongoDB (only when MONGO_TEST_URI is set, e.g. mongodb://localhost:27017): the same tests against a throwaway
    database, which is the evidence for the atomicity claims (unique-index slot insert; conditional update of a message
    element). Without that variable they are SKIPPED, not passed.

Run on a real server:  MONGO_TEST_URI=mongodb://localhost:27017 python -m pytest tests/test_mongo_atomicity.py -q
"""
import os
import threading
import uuid

import pytest

from tests.test_answer_persistence import env  # noqa: F401  (fixture)

OWNER = "mongo-owner"
REAL = os.environ.get("MONGO_TEST_URI")


@pytest.fixture(params=["mongomock"] + (["real"] if REAL else ["real-skipped"]))
def store(request, env, monkeypatch):                       # noqa: F811
    _lg, idx, _ = env
    if request.param == "real-skipped":
        pytest.skip("MONGO_TEST_URI is not set: no real MongoDB available, atomicity on a real server is UNVERIFIED")
    if request.param == "real":
        import pymongo
        client = pymongo.MongoClient(REAL, tz_aware=True, serverSelectionTimeoutMS=3000)
        dbname = "flintel_p0_test_" + uuid.uuid4().hex[:8]
        db = client[dbname]
    else:
        import mongomock
        client, db = None, mongomock.MongoClient()["p0"]
    busy, chats = db.busy, db.chats
    busy.create_index("owner_key", unique=True)             # exactly what database.py creates
    chats.create_index("chat_id", unique=True)
    monkeypatch.setattr(idx, "busy_owners_collection", busy)
    monkeypatch.setattr(idx, "chats_collection", chats)
    yield {"idx": idx, "busy": busy, "chats": chats}
    if client is not None:
        client.drop_database(dbname)


def _race(n, fn):
    barrier, out = threading.Barrier(n), [None] * n

    def run(i):
        barrier.wait(10)
        try:
            out[i] = fn(i)
        except BaseException as exc:                         # pragma: no cover
            out[i] = exc
    ts = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(n)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    return out


def test_unique_index_admits_exactly_cap_slots_under_a_race(store):
    idx = store["idx"]
    for cap in (1, 3):
        store["busy"].delete_many({})
        got = _race(40, lambda i: idx.reserve_search_slot(OWNER, f"{OWNER}::c{i}", cap))
        assert got.count(True) == cap and got.count(False) == 40 - cap, got
        assert idx.count_owner_busy(OWNER) == cap


def test_one_chat_flag_is_taken_by_exactly_one_racer(store):
    idx = store["idx"]
    got = _race(30, lambda i: idx.acquire_owner_busy(f"{OWNER}::chat"))
    assert got.count(True) == 1 and got.count(False) == 29


def _seed_chat(store, n_messages=3):
    msgs = [{"topic_key": f"t{i}", "request_id": f"r{i}", "claude_answer": None, "results": None, "query": f"q{i}"}
            for i in range(n_messages)]
    store["chats"].insert_one({"chat_id": "c1", "owner_key": OWNER, "messages": msgs})


def test_stop_and_complete_race_on_one_message_has_exactly_one_winner(store):
    idx = store["idx"]
    both, neither = 0, 0
    for round_no in range(40):
        store["chats"].delete_many({})
        _seed_chat(store)

        def work(i):
            if i % 2:
                return idx.mark_turn_cancelled("c1", OWNER, topic_key="t2", request_id="r2")
            idx.save_claude_answer_to_chat("c1", OWNER, "t2", "the answer")
            return None
        _race(8, work)
        m = store["chats"].find_one({"chat_id": "c1"})["messages"][2]
        stopped, answered = bool(m.get("cancelled_at")), bool(m.get("claude_answer"))
        both += stopped and answered
        neither += (not stopped) and (not answered)
        # the other messages are never touched by a stop / save aimed at message 2
        others = store["chats"].find_one({"chat_id": "c1"})["messages"]
        assert all(not o.get("cancelled_at") and o["claude_answer"] is None for o in others[:2])
    assert both == 0                 # never a message that is both stopped and answered
    assert neither == 0              # every round ended one way or the other


def test_a_stop_refuses_later_saves_and_a_completed_message_refuses_a_later_stop(store):
    idx = store["idx"]
    _seed_chat(store)
    assert idx.mark_turn_cancelled("c1", OWNER, topic_key="t0", request_id="r0") == idx.CANCEL_CANCELLED
    idx.save_claude_answer_to_chat("c1", OWNER, "t0", "late")
    idx.save_signal_results_to_chat("c1", OWNER, "t0", [{"id": 1}])
    m = store["chats"].find_one({"chat_id": "c1"})["messages"][0]
    assert m["cancelled_at"] and m["claude_answer"] is None and m["results"] is None
    idx.save_claude_answer_to_chat("c1", OWNER, "t1", "done")
    assert idx.mark_turn_cancelled("c1", OWNER, topic_key="t1", request_id="r1") == idx.CANCEL_ALREADY_COMPLETE
    assert idx.mark_turn_cancelled("c1", OWNER, topic_key="t0", request_id="r0") == idx.CANCEL_ALREADY_CANCELLED
    assert idx.mark_turn_cancelled("nope", OWNER, topic_key="t0") == idx.CANCEL_NOT_FOUND


def test_scan_lease_claim_is_atomic_stale_leases_are_taken_once_and_stale_holders_cannot_write(store):
    import datetime as dt
    idx = store["idx"]
    _seed_chat(store, n_messages=1)
    args = ("c1", OWNER, "t0", "r0")
    got = _race(32, lambda i: idx.claim_scan_lease(*args))
    assert sum(1 for g in got if g) == 1 and got.count(False) == 31             # exactly one scanner
    lease_id = next(g for g in got if g)
    store["chats"].update_one({"chat_id": "c1"}, {"$set": {"messages.0.scan_lease_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=idx.SCAN_LEASE_STALE_SECONDS + 5)}})
    got2 = _race(32, lambda i: idx.claim_scan_lease(*args))                     # the holder died: exactly one takes over
    assert sum(1 for g in got2 if g) == 1
    new_id = next(g for g in got2 if g)
    assert new_id != lease_id
    assert idx.mark_scan_done(*args, lease_id) is False                          # the stale holder cannot finish over the new one
    assert idx.clear_scan_lease(*args, lease_id) is False
    assert idx.mark_scan_done(*args, new_id) is True


def test_stale_reclaim_matches_the_exact_started_at_so_a_renewed_holder_is_never_reclaimed(store):
    import datetime as dt
    idx = store["idx"]
    holder = f"{OWNER}::c1"
    assert idx.reserve_search_slot(OWNER, holder, 1) and idx.acquire_owner_busy(holder)
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=idx.BUSY_FLAG_TIMEOUT_SECONDS + 50)
    store["busy"].update_many({}, {"$set": {"started_at": old}})
    flag = store["busy"].find_one({"owner_key": holder})
    observed = flag["started_at"]                                                # what a reclaimer read as stale ...
    assert idx.refresh_busy_lease(holder) is True                                # ... and then the live holder renews
    assert store["busy"].delete_one({"owner_key": holder, "started_at": observed}).deleted_count == 0   # the reclaim misses
    assert store["busy"].find_one({"owner_key": holder}) is not None
    got = _race(24, lambda i: idx.reserve_search_slot(OWNER, f"{OWNER}::other{i}", 1))   # nobody can take the live holder's slot
    assert not any(got)
