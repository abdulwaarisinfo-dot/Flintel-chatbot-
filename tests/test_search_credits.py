"""
tests/test_search_credits.py — search_credits.py (Stage 1 backend).

Everything runs against in-memory mongomock with a fake clock. The job write
is the REAL logics.enqueue_search_job() and the message save is the REAL
index.add_search_to_chat(), both pointed at mongomock collections; writes can
be scripted to apply-then-raise, raise, or be held and applied later. Ordered
races use the store's checkpoint hook — no sleeps. check_invariant() runs
after the interesting steps. No real Mongo, LLM or network.
"""
import random
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_answer_persistence import env  # noqa: F401  (fixture: stub modules, real logics/index)

mongomock = pytest.importorskip("mongomock")
from pymongo.errors import NetworkTimeout  # noqa: E402

import search_credits as sc  # noqa: E402

OWNER, OTHER = "o1", "o2"
TOPIC = "find crm buyers"
PAYLOAD = {"query": "Find CRM buyers", "topic_key": TOPIC, "keywords": ["crm buyers"], "targeting_platform": "reddit"}


class Clock:
    def __init__(self, start=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)


class Flaky:
    """Wraps a collection; scripted rules make a matching update_one apply
    then raise, raise without applying, or be held (applied later)."""

    def __init__(self, inner):
        self.inner, self.rules, self.held, self.calls = inner, [], [], []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def on(self, match, mode, times=1):
        self.rules.append({"match": match, "mode": mode, "left": times})

    def update_one(self, filt, update, *a, **k):
        self.calls.append((filt, update))
        for rule in self.rules:
            if rule["left"] and rule["match"](filt, update):
                rule["left"] -= 1
                if rule["mode"] == "apply_then_raise":
                    self.inner.update_one(filt, update, *a, **k)
                    raise NetworkTimeout("timed out after applying")
                if rule["mode"] == "raise":
                    raise NetworkTimeout("timed out")
                if rule["mode"] == "hold":
                    self.held.append((filt, update, a, k))
                    raise NetworkTimeout("timed out; still pending")
                if isinstance(rule["mode"], tuple):                 # ("raise_exc"|"apply_then_exc", exc)
                    kind, exc = rule["mode"]
                    if kind == "apply_then_exc":
                        self.inner.update_one(filt, update, *a, **k)
                    raise exc
                if callable(rule["mode"]):
                    rule["mode"](self.inner, filt, update)
        return self.inner.update_one(filt, update, *a, **k)

    def release(self):
        held, self.held = self.held, []
        for filt, update, a, k in held:
            self.inner.update_one(filt, update, *a, **k)


@pytest.fixture
def world(env, monkeypatch):
    lg, idx, _ = env
    db = mongomock.MongoClient()["credits_test"]
    coll = Flaky(db.flintel_credit_usage)
    db.flintel_credit_usage.create_index("owner_key", unique=True)
    chats = Flaky(db.chats)
    jobs = Flaky(db.jobs)
    monkeypatch.setattr(idx, "chats_collection", chats)
    monkeypatch.setattr(lg, "jobs_collection", jobs)
    monkeypatch.setattr(sys.modules["turn_finisher"], "enqueue", lambda *a, **k: None, raising=False)
    clock = Clock()
    # JOB_ATTRIBUTION_MODE=field explicitly for the Stage 1 recovery tests; the
    # off-mode tests below switch it with state["attribute"] = False.
    state = {"limit": 40, "enqueues": [], "attribute": True}

    def enqueue_fn(rid):
        state["enqueues"].append(rid)
        return lg.enqueue_search_job(TOPIC, ["crm buyers"], "reddit", credit_request_id=rid,
                                     attribute=state["attribute"])

    def make_store(clock_=None, limit=None, checkpoint=None):
        return sc.CreditStore(coll, chats, jobs, idx.add_search_to_chat, clock=clock_ or clock,
                              enabled=lambda: True, limit=limit or (lambda: state["limit"]), checkpoint=checkpoint)

    def new_chat(chat_id="c1", owner=OWNER):
        db.chats.insert_one({"chat_id": chat_id, "owner_key": owner, "title": "t", "messages": [], "summary": ""})
        return chat_id

    w = {"lg": lg, "idx": idx, "db": db, "coll": coll, "chats": chats, "jobs": jobs, "clock": clock,
         "state": state, "enqueue_fn": enqueue_fn, "make_store": make_store, "new_chat": new_chat}
    w["store"] = make_store()
    return w


def doc(w, owner=OWNER):
    return w["db"].flintel_credit_usage.find_one({"owner_key": owner})


def entry(w, rid, owner=OWNER):
    return ((doc(w, owner) or {}).get("res") or {}).get(rid)


def used(w, period="2026-10", owner=OWNER):
    return int(((doc(w, owner) or {}).get("used_by_period") or {}).get(period, 0))


def inv(w, owner=OWNER):
    problems = sc.check_invariant(doc(w, owner))
    assert problems == [], problems


def messages(w, chat_id="c1", rid=None, owner=OWNER):
    d = w["db"].chats.find_one({"chat_id": chat_id, "owner_key": owner}) or {}
    msgs = d.get("messages") or []
    return [m for m in msgs if rid is None or m.get("credit_request_id") == rid]


def job_ids(w):
    d = w["db"].jobs.find_one({"topic_key": TOPIC}) or {}
    return d.get("credit_request_ids") or []


def reserve(w, rid, store=None, chat_id="c1", expects_job=True, retry_message=None, topic=TOPIC):
    return (store or w["store"]).reserve(OWNER, "anon", rid, topic, chat_id, expects_job, retry_message=retry_message)


def claim_matcher(rid):
    return lambda f, u: f.get(f"res.{rid}.state") == sc.STARTED and f"res.{rid}.sent_once" in (u.get("$set") or {})


def is_job_write(f, u):
    return "credit_request_ids" in (u.get("$push") or {})


# ── pure helpers ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("now,period,reset", [
    (datetime(2026, 10, 15, 12, tzinfo=timezone.utc), "2026-10", datetime(2026, 11, 1, tzinfo=timezone.utc)),
    (datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc), "2026-10", datetime(2026, 11, 1, tzinfo=timezone.utc)),
    (datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc), "2026-12", datetime(2027, 1, 1, tzinfo=timezone.utc)),
    (datetime(2028, 2, 29, 8, 0, tzinfo=timezone.utc), "2028-02", datetime(2028, 3, 1, tzinfo=timezone.utc)),
    (datetime(2026, 11, 1, 0, 0), "2026-11", datetime(2026, 12, 1, tzinfo=timezone.utc)),   # naive = UTC
])
def test_period_and_reset(now, period, reset):
    assert sc.period_key(now) == period and sc.period_resets_at(now) == reset
    assert sc.previous_period("2027-01") == "2026-12" and sc.previous_period("2026-11") == "2026-10"


def test_request_id_is_owner_scoped_and_validated():
    key = "0123456789abcdef0123"
    assert sc.request_id("o1", key) == sc.request_id("o1", key)
    assert sc.request_id("o1", key) != sc.request_id("o2", key)
    for bad in (None, "", "short", "x" * 65, "has space and more chars", "bad$chars-0123456789"):
        a, b = sc.request_id("o1", bad), sc.request_id("o1", bad)
        assert a != b and len(a) == 32   # no deduplication for missing/invalid keys


def test_message_save_filters():
    normal = sc.message_save_filter("c1", "o1", "r1")
    assert normal == {"chat_id": "c1", "owner_key": "o1", "messages.credit_request_id": {"$ne": "r1"},
                      "credit_close.r1": {"$exists": False}}
    late = sc.message_save_filter("c1", "o1", "r1", late_delivery=("A", "T"))
    assert late["credit_close.r1.phase"] == "closed" and late["credit_close.r1.attempt"] == "A"
    assert late["credit_close.r1.close_token"] == "T" and "credit_close.r1" not in late
    with pytest.raises(ValueError):
        sc.message_save_filter("c1", "o1", "r1", late_delivery=("A", ""))


def test_config_parsing(caplog):
    import config
    assert config._parse_free_monthly_credits(None) == 40 and config._parse_free_monthly_credits("") == 40
    for good in ("1", "3", "40", "10000", " 25 "):
        assert config._parse_free_monthly_credits(good) == int(good)
    caplog.clear()
    for bad in ("0", "-1", "abc", "1e3", "10001", "4.5"):
        assert config._parse_free_monthly_credits(bad) == 40
    assert caplog.text.count("FREE_MONTHLY_CREDITS=") == 6
    assert config._parse_credit_kill_switch(None) is False
    assert config._parse_credit_kill_switch("TRUE") is True and "EMERGENCY ONLY" in caplog.text
    for other in ("1", "yes", "false", "on"):
        assert config._parse_credit_kill_switch(other) is False
    assert "ignored (only 'true' disables" in caplog.text


# ── R13: the matched-zero classification table ──────────────────────────────
ALL_STATES = [sc.RESERVED, sc.STARTED, sc.SENDING, sc.ENQUEUED, sc.MESSAGE_FAILED, sc.NEEDS_REC, sc.COMMITTED,
              sc.REFUNDED]
REASONS = {sc.NEEDS_REC: [sc.R_AMBIGUOUS, sc.R_MSG_MISSING],
           sc.COMMITTED: [sc.R_NORMAL, sc.R_REPAIRED, sc.R_ALREADY, sc.R_CHAT_MISSING, sc.R_MANUAL]}
EXPECTED_FROM = {"T2": (sc.RESERVED, None), "T3s": (sc.STARTED, None), "T4": (sc.STARTED, None),
                 "T6a": (sc.SENDING, None), "T6b": (sc.SENDING, None), "T6c": (sc.SENDING, None),
                 "T7": (sc.SENDING, None), "T8": (sc.ENQUEUED, None), "T9": (sc.ENQUEUED, None),
                 "T10": (sc.MESSAGE_FAILED, None), "T11": (sc.NEEDS_REC, sc.R_AMBIGUOUS),
                 "TSTALE": (sc.ENQUEUED, None), "T12": (sc.STARTED, None), "T13": (sc.NEEDS_REC, sc.R_AMBIGUOUS),
                 "T15": (sc.COMMITTED, sc.R_MANUAL)}


def _observations():
    yield None
    for state in ALL_STATES:
        for reason in REASONS.get(state, [None]):
            for attempt in ("A", "B"):
                yield {"state": state, "reason": reason, "attempt": attempt, "ev": 9}


@pytest.mark.parametrize("transition", sorted(EXPECTED_FROM))
def test_classification_table_is_total_and_safe(transition):
    state, reason = EXPECTED_FROM[transition]
    expected = {"state": state, "reason": reason, "attempt": "A"}
    for obs in _observations():
        out = sc.classify(transition, expected, obs)
        assert out in {sc.RETRY_FRESH, sc.RETRY_PAYLOAD, sc.DONE, sc.PROCEED, sc.COMPLETED, sc.SUPERSEDED,
                       sc.TERMINAL, sc.TERMINAL_NO_DELIVERY, sc.NOOP, sc.CONFLICT, sc.CLAIM_UNCONFIRMED, sc.SAME_KEY}
        if obs and obs["state"] == sc.COMMITTED and obs["reason"] in (sc.R_CHAT_MISSING, sc.R_MANUAL):
            assert out not in (sc.DONE, sc.COMPLETED), (transition, obs, out)       # never success
        if obs and obs["state"] in (sc.COMMITTED, sc.REFUNDED) and transition != "T15" and not (
                obs["state"] == state and obs["reason"] == reason and obs["attempt"] == "A"):
            assert out not in (sc.RETRY_FRESH, sc.RETRY_PAYLOAD), (transition, obs, out)   # no write to terminal
        if transition == "T4":
            assert out not in (sc.DONE, sc.PROCEED, sc.COMPLETED)                  # only matched==1 sends


@pytest.mark.parametrize("transition,obs,expected_out", [
    ("T6a", {"state": sc.NEEDS_REC, "reason": sc.R_AMBIGUOUS, "attempt": "A"}, sc.RETRY_FRESH),
    ("T6a", {"state": sc.ENQUEUED, "reason": None, "attempt": "A"}, sc.DONE),
    ("T6a", {"state": sc.NEEDS_REC, "reason": sc.R_MSG_MISSING, "attempt": "A"}, sc.PROCEED),
    ("T6a", {"state": sc.COMMITTED, "reason": sc.R_REPAIRED, "attempt": "A"}, sc.COMPLETED),
    ("T6a", {"state": sc.COMMITTED, "reason": sc.R_MANUAL, "attempt": "A"}, sc.TERMINAL),
    ("T6a", {"state": sc.COMMITTED, "reason": sc.R_CHAT_MISSING, "attempt": "A"}, sc.TERMINAL_NO_DELIVERY),
    ("T6a", {"state": sc.MESSAGE_FAILED, "reason": None, "attempt": "A"}, sc.CONFLICT),
    ("T6a", None, sc.CONFLICT),
    ("T4", None, sc.SUPERSEDED), ("T4", {"state": sc.REFUNDED, "reason": None, "attempt": "A"}, sc.SUPERSEDED),
    ("T4", {"state": sc.STARTED, "reason": None, "attempt": "B"}, sc.SUPERSEDED),
    ("T4", {"state": sc.SENDING, "reason": None, "attempt": "A"}, sc.CONFLICT),
    ("T3s", {"state": sc.SENDING, "reason": None, "attempt": "A"}, sc.CLAIM_UNCONFIRMED),
    ("T8", {"state": sc.NEEDS_REC, "reason": sc.R_MSG_MISSING, "attempt": "A"}, sc.RETRY_FRESH),
    ("T8", {"state": sc.COMMITTED, "reason": sc.R_MANUAL, "attempt": "A"}, sc.TERMINAL),
    ("T8", {"state": sc.COMMITTED, "reason": sc.R_CHAT_MISSING, "attempt": "A"}, sc.CONFLICT),
    ("T9", {"state": sc.COMMITTED, "reason": sc.R_MANUAL, "attempt": "A"}, sc.TERMINAL_NO_DELIVERY),
    ("T6b", {"state": sc.NEEDS_REC, "reason": sc.R_AMBIGUOUS, "attempt": "A"}, sc.RETRY_PAYLOAD),
    ("T6b", {"state": sc.COMMITTED, "reason": sc.R_MANUAL, "attempt": "A"}, sc.TERMINAL_NO_DELIVERY),
    ("T11", {"state": sc.COMMITTED, "reason": sc.R_MANUAL, "attempt": "A"}, sc.NOOP),
])
def test_classification_rows(transition, obs, expected_out):
    state, reason = EXPECTED_FROM[transition]
    if obs is not None:
        obs = {**obs, "ev": 7}
    assert sc.classify(transition, {"state": state, "reason": reason, "attempt": "A"}, obs) == expected_out


def test_rule_s_only_ev_changed():
    assert sc.classify("T6a", {"state": sc.SENDING, "reason": None, "attempt": "A"},
                       {"state": sc.SENDING, "reason": None, "attempt": "A", "ev": 5}) == sc.RETRY_FRESH


# ── reservations, limits and the invariant ──────────────────────────────────
def test_first_search_happy_path(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    assert isinstance(t, sc.Ticket) and used(w) == 1
    d = w["store"].run(t, w["enqueue_fn"], PAYLOAD)
    assert d.kind == "done" and d.chat_id == "c1"
    e = entry(w, "r1")
    assert e["state"] == sc.COMMITTED and e["reason"] == sc.R_NORMAL and e["sent_once"] is True
    assert [m["credit_request_id"] for m in messages(w)] == ["r1"] and job_ids(w) == ["r1"]
    assert w["state"]["enqueues"] == ["r1"]
    inv(w)


def test_website_search_without_keywords_has_no_job(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1", expects_job=False)
    d = w["store"].run(t, None, {**PAYLOAD, "keywords": [], "website_only": True})
    assert d.kind == "done" and w["state"]["enqueues"] == [] and job_ids(w) == []
    assert entry(w, "r1")["state"] == sc.COMMITTED and len(messages(w, rid="r1")) == 1
    inv(w)


@pytest.mark.parametrize("limit", [1, 3, 40])
def test_limit_is_the_configured_value(world, limit):
    w = world
    w["state"]["limit"] = limit
    w["new_chat"]()
    for i in range(limit):
        assert isinstance(reserve(w, f"r{i}"), sc.Ticket)
    d = reserve(w, "over")
    assert d.kind == "exhausted" and d.resets_at == datetime(2026, 11, 1, tzinfo=timezone.utc) and d.cleanup_ok
    assert used(w) == limit and doc(w)["limit_max_by_period"]["2026-10"] == limit
    inv(w)


@pytest.mark.parametrize("limit", [3, 40])
def test_concurrent_reservations_never_exceed_the_limit(world, limit):
    w = world
    w["state"]["limit"] = limit
    results = []
    lock = threading.Lock()

    def go(i):
        r = reserve(w, f"t{i}")
        with lock:
            results.append(r)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(50)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(isinstance(r, sc.Ticket) for r in results) == limit
    assert all(r.kind == "exhausted" for r in results if not isinstance(r, sc.Ticket))
    assert used(w) == limit
    inv(w)


def test_lowering_and_raising_the_limit_mid_month(world):
    w = world
    for i in range(35):
        reserve(w, f"r{i}")
    w["state"]["limit"] = 30
    assert reserve(w, "x").kind == "exhausted" and used(w) == 35            # nothing revoked
    assert doc(w)["limit_max_by_period"]["2026-10"] == 40
    inv(w)
    w["state"]["limit"] = 50
    assert isinstance(reserve(w, "y"), sc.Ticket) and doc(w)["limit_max_by_period"]["2026-10"] == 50
    inv(w)


def test_two_servers_with_different_limits(world):
    w = world
    a = w["make_store"](limit=lambda: 40)
    b = w["make_store"](limit=lambda: 45)
    for i in range(20):
        reserve(w, f"a{i}", store=a)
        reserve(w, f"b{i}", store=b)
    assert doc(w)["limit_max_by_period"]["2026-10"] == 45 and used(w) == 40
    inv(w)


def test_concurrent_same_key_creates_one_reservation(world):
    w = world
    results, lock = [], threading.Lock()

    def go():
        r = reserve(w, "same")
        with lock:
            results.append(r)
    threads = [threading.Thread(target=go) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(isinstance(r, sc.Ticket) for r in results) == 1
    assert all(r.kind == "busy" for r in results if not isinstance(r, sc.Ticket))
    assert used(w) == 1
    inv(w)


@pytest.mark.parametrize("start_refunded", [False, True])
def test_same_key_across_the_month_boundary_reserves_once(world, start_refunded):
    w = world
    october = Clock(datetime(2026, 10, 31, 23, 59, 59, 900000, tzinfo=timezone.utc))
    november = Clock(datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc))
    so, sn = w["make_store"](clock_=october), w["make_store"](clock_=november)
    if start_refunded:
        t = reserve(w, "edge", store=so)
        assert so.self_refund(t) and used(w, "2026-10") == 0
    results, lock = [], threading.Lock()

    def go(store):
        r = reserve(w, "edge", store=store)
        with lock:
            results.append(r)
    threads = [threading.Thread(target=go, args=(so if i % 2 else sn,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(isinstance(r, sc.Ticket) for r in results) == 1
    assert used(w, "2026-10") + used(w, "2026-11") == 1
    inv(w)


def test_refund_on_one_request_never_touches_another(world):
    w = world
    t1, t2 = reserve(w, "r1"), reserve(w, "r2")
    assert w["store"].self_refund(t1)
    assert used(w) == 1 and entry(w, "r2")["state"] == sc.RESERVED
    assert not w["store"].self_refund(t1)                                    # second refund does nothing
    assert used(w) == 1
    inv(w)
    assert t2.state == sc.RESERVED


# ── claim and enqueue failures ──────────────────────────────────────────────
def test_claim_timeout_after_the_write_applied_never_enqueues(world, caplog):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["coll"].on(claim_matcher("r1"), "apply_then_raise")
    d = w["store"].run(t, w["enqueue_fn"], PAYLOAD)
    assert d.kind == "unavailable" and not d.cleanup_ok
    assert w["state"]["enqueues"] == [] and job_ids(w) == []
    e = entry(w, "r1")
    assert e["state"] == sc.SENDING and e["claim_unconfirmed"] is True and used(w) == 1
    assert "claim unconfirmed, not sending" in caplog.text
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    assert entry(w, "r1")["state"] == sc.NEEDS_REC and entry(w, "r1")["reason"] == sc.R_AMBIGUOUS and used(w) == 1
    r = reserve(w, "r1")                                                    # same-key retry: no enqueue
    assert r.kind == "reconciliation" and w["state"]["enqueues"] == []
    inv(w)


def test_claim_timeout_not_applied_self_refunds(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["coll"].on(claim_matcher("r1"), "raise")
    d = w["store"].run(t, w["enqueue_fn"], PAYLOAD)
    assert d.kind == "unavailable" and d.cleanup_ok
    assert entry(w, "r1")["state"] == sc.REFUNDED and used(w) == 0 and w["state"]["enqueues"] == []
    t2 = reserve(w, "r1")                                                   # T1r: one new charge
    assert isinstance(t2, sc.Ticket) and t2.attempt != t.attempt and used(w) == 1
    assert w["store"].run(t2, w["enqueue_fn"], PAYLOAD).kind == "done"
    inv(w)


def test_claim_and_self_refund_both_fail_then_recovery_refunds_unsent(world):
    w = world
    t = reserve(w, "r1")
    w["store"].start(t)
    w["coll"].on(claim_matcher("r1"), "raise")
    w["coll"].on(lambda f, u: (u.get("$set") or {}).get("res.r1.state") == sc.REFUNDED, "raise")
    assert w["store"].claim(t) == sc.CLAIM_UNCONFIRMED
    assert entry(w, "r1")["state"] == sc.STARTED
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    assert entry(w, "r1")["state"] == sc.REFUNDED and used(w) == 0
    assert w["store"].claim(t) == sc.SUPERSEDED                              # the late claim can't send
    inv(w)


def test_enqueue_timeout_after_the_job_applied(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["jobs"].on(is_job_write, "apply_then_raise")
    d = w["store"].run(t, w["enqueue_fn"], PAYLOAD)
    assert d.kind == "unavailable" and job_ids(w) == ["r1"]
    e = entry(w, "r1")
    assert e["state"] == sc.SENDING and e["failed_at"] and e["pending_message"]["query"] == PAYLOAD["query"]
    w["store"].recover(OWNER)                                               # E2 -> enqueued -> repair
    e = entry(w, "r1")
    assert e["state"] == sc.COMMITTED and e["reason"] == sc.R_REPAIRED and used(w) == 1
    assert len(messages(w, rid="r1")) == 1 and len(w["state"]["enqueues"]) == 1
    inv(w)


def test_enqueue_timeout_then_same_key_retry_before_recovery(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["jobs"].on(is_job_write, "apply_then_raise")
    w["store"].run(t, w["enqueue_fn"], PAYLOAD)
    d = w["store"].lookup(OWNER, "r1")
    assert d.kind == "redirect" and d.chat_id == "c1"
    assert len(messages(w, rid="r1")) == 1 and w["state"]["enqueues"] == ["r1"] and used(w) == 1
    inv(w)


def test_enqueue_timeout_not_applied_stays_charged(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["jobs"].on(is_job_write, "raise")
    w["store"].run(t, w["enqueue_fn"], PAYLOAD)
    w["store"].recover(OWNER)
    assert entry(w, "r1")["state"] == sc.NEEDS_REC and entry(w, "r1")["reason"] == sc.R_AMBIGUOUS
    assert used(w) == 1 and reserve(w, "r1").kind == "reconciliation" and len(w["state"]["enqueues"]) == 1
    inv(w)


# ── R1: how a job can land late ─────────────────────────────────────────────
def _held_send(w, rid="r1"):
    """T4 succeeded and the real enqueue write was sent but is still pending; the original dies."""
    w["new_chat"]()
    t = reserve(w, rid)
    w["store"].start(t)
    assert w["store"].claim(t) == sc.DONE
    w["jobs"].on(is_job_write, "hold")
    with pytest.raises(NetworkTimeout):
        w["enqueue_fn"](rid)
    return t


def _no_refund_ever(w, rid="r1", period="2026-10"):
    e = entry(w, rid)
    assert e is not None and e["state"] != sc.REFUNDED and used(w, period) == 1


def test_r1a_pending_write_lands_after_reconciliation_then_same_key_repair(world):
    w = world
    _held_send(w)
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    assert entry(w, "r1")["state"] == sc.NEEDS_REC and entry(w, "r1")["reason"] == sc.R_AMBIGUOUS
    w["jobs"].release()                                                     # E2 appears
    assert job_ids(w) == ["r1"]
    w["store"].recover(OWNER)
    assert entry(w, "r1")["state"] == sc.ENQUEUED
    _no_refund_ever(w)
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    assert entry(w, "r1")["reason"] == sc.R_MSG_MISSING
    _no_refund_ever(w)
    assert w["store"].lookup(OWNER, "r1").kind == "continue"
    d = reserve(w, "r1", retry_message=PAYLOAD)                             # ending 1: same-key repair
    assert d.kind == "redirect" and entry(w, "r1")["reason"] == sc.R_REPAIRED and len(messages(w, rid="r1")) == 1
    _no_refund_ever(w)
    inv(w)


def test_r1a_pending_write_lands_and_stays_unresolved_until_close(world):
    w = world
    _held_send(w)
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    w["jobs"].release()
    for _ in range(10):                                                     # ending 2: no retry
        w["store"].recover(OWNER)
        w["clock"].advance(901)
        _no_refund_ever(w)
        inv(w)
    w["clock"].t = datetime(2026, 12, 3, tzinfo=timezone.utc)               # across the month boundary + pruning
    w["store"].recover(OWNER)
    assert entry(w, "r1")["state"] == sc.NEEDS_REC and entry(w, "r1")["reason"] == sc.R_MSG_MISSING
    _no_refund_ever(w)
    with pytest.raises(ValueError):
        w["store"].resolve(OWNER, "r1", "refund")
    res = w["store"].resolve(OWNER, "r1", "close")
    assert res["result"] == "closed" and entry(w, "r1")["reason"] == sc.R_MANUAL
    _no_refund_ever(w)
    inv(w)


def test_r1c_applied_ack_lost_original_dies(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["store"].start(t)
    w["store"].claim(t)
    w["jobs"].on(is_job_write, "apply_then_raise")
    with pytest.raises(NetworkTimeout):
        w["enqueue_fn"]("r1")
    w["clock"].advance(901)
    w["store"].recover(OWNER)                                               # E2 present at first recovery
    assert entry(w, "r1")["state"] == sc.ENQUEUED
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    assert entry(w, "r1")["reason"] == sc.R_MSG_MISSING
    _no_refund_ever(w)
    inv(w)


def test_r1d_acknowledged_original_dies_before_t6a(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["store"].start(t)
    w["store"].claim(t)
    assert sc.is_e1(w["enqueue_fn"]("r1"))
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    assert entry(w, "r1")["state"] == sc.ENQUEUED
    _no_refund_ever(w)
    inv(w)


def test_r1e_pending_write_original_alive_overrides_reconciliation(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")

    def at(name, **ctx):
        if name == "after_claim":
            w["clock"].advance(901)
            w["store"].recover(OWNER)                                       # no E2 yet -> ambiguous
            assert entry(w, "r1")["state"] == sc.NEEDS_REC
    store = w["make_store"](checkpoint=at)
    d = store.run(t, w["enqueue_fn"], PAYLOAD)                              # the write lands, E1 held
    assert d.kind == "done"
    e = entry(w, "r1")
    assert e["state"] == sc.COMMITTED and e["reason"] == sc.R_NORMAL and len(messages(w, rid="r1")) == 1
    _no_refund_ever(w)
    inv(w)


# ── ordered races ───────────────────────────────────────────────────────────
def test_enqueue_in_progress_while_error_refund_close_and_retry_paths_run(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    seen = {}

    def at(name, **ctx):
        if name != "after_claim":
            return
        ticket = ctx["ticket"]
        clone = sc.Ticket(**{**ticket.__dict__})
        assert w["store"].self_refund(clone) is False                       # never from sending
        w["clock"].advance(901)
        w["store"].recover(OWNER)
        assert entry(w, "r1")["state"] == sc.NEEDS_REC
        with pytest.raises(ValueError):
            w["store"].resolve(OWNER, "r1", "refund")
        seen["retry"] = w["store"].lookup(OWNER, "r1").kind
        assert w["state"]["enqueues"] == []
    store = w["make_store"](checkpoint=at)
    d = store.run(t, w["enqueue_fn"], PAYLOAD)
    assert seen["retry"] == "reconciliation"
    assert d.kind == "done" and entry(w, "r1")["state"] == sc.COMMITTED
    assert w["state"]["enqueues"] == ["r1"] and used(w) == 1 and len(messages(w, rid="r1")) == 1
    inv(w)


def test_close_while_enqueue_in_progress_then_late_delivery(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")

    def at(name, **ctx):
        if name == "after_claim":
            w["clock"].advance(901)
            w["store"].recover(OWNER)
            assert w["store"].resolve(OWNER, "r1", "close")["result"] == "closed"
    store = w["make_store"](checkpoint=at)
    d = store.run(t, w["enqueue_fn"], PAYLOAD)                              # E1 -> T6a sees manual_close
    e = entry(w, "r1")
    assert e["state"] == sc.COMMITTED and e["reason"] == sc.R_MANUAL                # never reopened
    assert len(messages(w, rid="r1")) == 1 and any("late_delivery_at" in n for n in e["audit"])
    assert d.kind == "redirect" and used(w) == 1
    inv(w)


def test_refund_first_then_late_claim_cannot_send(world):
    w = world
    t = reserve(w, "r1")
    w["store"].start(t)
    w["clock"].advance(901)
    w["store"].recover(OWNER)                                               # T3
    assert entry(w, "r1")["state"] == sc.REFUNDED
    assert w["store"].claim(t) == sc.SUPERSEDED and w["state"]["enqueues"] == []
    t2 = reserve(w, "r1")                                                   # T1r with a new attempt
    assert w["store"].claim(t) == sc.SUPERSEDED                             # the old attempt still can't
    assert w["store"].run(t2, w["enqueue_fn"], PAYLOAD).kind in ("done", "redirect")
    assert w["state"]["enqueues"] == ["r1"] and used(w) == 1
    inv(w)


@pytest.mark.parametrize("recovery_first", [True, False])
def test_r3_original_t6a_against_recovery_t11(world, recovery_first):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["store"].start(t)
    w["store"].claim(t)
    w["clock"].advance(901)
    w["store"].recover(OWNER)                                               # ambiguous (no job yet)
    result = w["enqueue_fn"]("r1")
    assert sc.is_e1(result)
    t.e1 = True
    if recovery_first:
        w["store"].recover(OWNER)                                           # T11 -> enqueued, then T6a DONE
        assert w["store"].mark_enqueued(t) == sc.DONE
    else:
        assert w["store"].mark_enqueued(t) == sc.DONE                       # T6a override first
        w["store"].recover(OWNER)                                           # recovery: nothing to do
    assert entry(w, "r1")["state"] == sc.ENQUEUED
    assert w["store"]._save_and_commit(t, PAYLOAD).kind == "done"
    assert len(messages(w, rid="r1")) == 1 and entry(w, "r1")["state"] == sc.COMMITTED
    inv(w)


@pytest.mark.parametrize("close_first", [True, False])
def test_r5_close_against_original_t6a(world, close_first):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["store"].start(t)
    w["store"].claim(t)
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    ev = entry(w, "r1")["ev"]
    w["enqueue_fn"]("r1")
    t.e1 = True
    if close_first:
        assert w["store"].resolve(OWNER, "r1", "close", expected_ev=ev)["result"] == "closed"
        assert w["store"].mark_enqueued(t) == sc.TERMINAL
        assert entry(w, "r1")["reason"] == sc.R_MANUAL
    else:
        assert w["store"].mark_enqueued(t) == sc.DONE
        assert w["store"].resolve(OWNER, "r1", "close", expected_ev=ev)["result"] == "superseded"
        assert entry(w, "r1")["state"] == sc.ENQUEUED
    inv(w)


@pytest.mark.parametrize("close_first", [True, False])
def test_r6_close_against_recovery_t11(world, close_first):
    w = world
    _held_send(w)
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    w["jobs"].release()
    if close_first:
        assert w["store"].resolve(OWNER, "r1", "close")["result"] == "closed"
        w["store"].recover(OWNER)                                           # T11 finds a terminal state
        assert entry(w, "r1")["reason"] == sc.R_MANUAL and messages(w, rid="r1") == []
    else:
        w["store"].recover(OWNER)
        assert entry(w, "r1")["state"] == sc.ENQUEUED
        assert w["store"].resolve(OWNER, "r1", "close")["result"] == "superseded"
    inv(w)


@pytest.mark.parametrize("order", ["retry,recovery,original", "retry,original,recovery", "recovery,retry,original",
                                   "recovery,original,retry", "original,retry,recovery", "original,recovery,retry"])
def test_r7_retry_recovery_and_original_after_landing(world, order):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["store"].start(t)
    w["store"].claim(t)
    w["clock"].advance(901)
    w["store"].recover(OWNER)                                               # ambiguous
    w["enqueue_fn"]("r1")                                                   # job lands (E1 for the original)
    t.e1 = True
    for actor in order.split(","):
        if actor == "retry":
            reserve(w, "r1", retry_message=PAYLOAD)
        elif actor == "recovery":
            w["store"].recover(OWNER)
        else:
            outcome = w["store"].mark_enqueued(t)
            if outcome in (sc.DONE, sc.PROCEED):
                w["store"]._save_and_commit(t, PAYLOAD)
        inv(w)
    w["store"].recover(OWNER)
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    reserve(w, "r1", retry_message=PAYLOAD)
    assert entry(w, "r1")["state"] == sc.COMMITTED and len(messages(w, rid="r1")) == 1
    assert w["state"]["enqueues"] == ["r1"] and used(w) == 1
    inv(w)


def test_r8_stale_original_against_resumed_attempt(world):
    w = world
    w["new_chat"]()
    a = reserve(w, "r1")
    w["store"].start(a)
    w["clock"].advance(901)
    b = reserve(w, "r1")                                                    # T12 resume, attempt B
    assert isinstance(b, sc.Ticket) and b.attempt != a.attempt and used(w) == 1
    assert w["store"].claim(a) == sc.SUPERSEDED
    assert w["store"].run(b, w["enqueue_fn"], PAYLOAD).kind == "done"
    assert w["state"]["enqueues"] == ["r1"] and used(w) == 1
    inv(w)


def test_r10_recovery_marks_message_missing_while_original_saves(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")

    def at(name, **ctx):
        if name == "before_message":
            w["clock"].advance(901)
            w["store"].recover(OWNER)                                       # stale enqueued, no message yet
            assert entry(w, "r1")["reason"] == sc.R_MSG_MISSING
    store = w["make_store"](checkpoint=at)
    assert store.run(t, w["enqueue_fn"], PAYLOAD).kind == "done"
    e = entry(w, "r1")
    assert e["state"] == sc.COMMITTED and e["reason"] == sc.R_NORMAL and len(messages(w, rid="r1")) == 1
    inv(w)


@pytest.mark.parametrize("closed_in_between", [False, True])
def test_r11_t6b_against_recovery_t7(world, closed_in_between):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["jobs"].on(is_job_write, "raise")

    def at(name, **ctx):
        if name == "after_enqueue":
            w["clock"].advance(901)
            w["store"].recover(OWNER)                                       # stale sending, no E2 -> ambiguous
            if closed_in_between:
                w["store"].resolve(OWNER, "r1", "close")
    store = w["make_store"](checkpoint=at)
    assert store.run(t, w["enqueue_fn"], PAYLOAD).kind == "unavailable"
    e = entry(w, "r1")
    if closed_in_between:
        assert e["reason"] == sc.R_MANUAL and e["pending_message"] is None
    else:
        assert e["state"] == sc.NEEDS_REC and e["pending_message"]["query"] == PAYLOAD["query"]
    inv(w)


def test_r12_constant_version_changes_stop_after_three_tries(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["store"].start(t)
    w["store"].claim(t)
    w["enqueue_fn"]("r1")

    def bump(inner, f, u):
        inner.update_one({"owner_key": OWNER}, {"$inc": {"res.r1.ev": 1}})
    w["coll"].on(lambda f, u: (u.get("$set") or {}).get("res.r1.state") == sc.ENQUEUED, bump, times=3)
    assert w["store"].mark_enqueued(t) == sc.CONFLICT
    assert entry(w, "r1")["state"] == sc.SENDING and used(w) == 1
    inv(w)


def test_concurrent_recovery_refunds_once(world):
    w = world
    reserve(w, "r1")
    w["clock"].advance(901)
    threads = [threading.Thread(target=w["store"].recover, args=(OWNER,)) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert entry(w, "r1")["state"] == sc.REFUNDED and used(w) == 0
    inv(w)


# ── month boundary ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("state", [sc.RESERVED, sc.STARTED, sc.SENDING, sc.ENQUEUED, sc.MESSAGE_FAILED,
                                   sc.NEEDS_REC, sc.COMMITTED])
def test_each_state_crossing_into_the_next_month(world, state):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    if state != sc.RESERVED:
        w["store"].start(t)
    if state in (sc.SENDING, sc.ENQUEUED, sc.MESSAGE_FAILED, sc.NEEDS_REC, sc.COMMITTED):
        w["store"].claim(t)
        w["enqueue_fn"]("r1")
        t.e1 = True
    if state in (sc.ENQUEUED, sc.MESSAGE_FAILED, sc.COMMITTED):
        w["store"].mark_enqueued(t)
    if state == sc.MESSAGE_FAILED:
        w["store"].record_message_failure(t, PAYLOAD)
    if state == sc.NEEDS_REC:
        w["jobs"].inner.update_one({"topic_key": TOPIC}, {"$set": {"credit_request_ids": []}})
        w["clock"].advance(901)
        w["store"].recover(OWNER)
    if state == sc.COMMITTED:
        w["store"]._save_and_commit(t, PAYLOAD)
    assert entry(w, "r1")["state"] == state
    w["clock"].t = datetime(2026, 11, 2, tzinfo=timezone.utc)
    for _ in range(3):
        w["store"].recover(OWNER)
        reserve(w, "r1", retry_message=PAYLOAD)
    if state in (sc.SENDING, sc.ENQUEUED, sc.MESSAGE_FAILED, sc.NEEDS_REC, sc.COMMITTED):
        # sent: stays charged to October, a same-key retry never charges November
        assert used(w, "2026-10") == 1 and used(w, "2026-11") == 0 and entry(w, "r1")["state"] != sc.REFUNDED
    else:
        # never sent: the stale October hold is refunded, the retry is one fresh November charge
        assert used(w, "2026-10") == 0 and used(w, "2026-11") == 1 and entry(w, "r1")["period"] == "2026-11"
    base = used(w, "2026-11")
    for i in range(40 - base):                                               # the rest of a full new allowance
        assert isinstance(reserve(w, f"n{i}"), sc.Ticket)
    assert used(w, "2026-11") == 40 and reserve(w, "n40").kind == "exhausted"
    inv(w)


def test_month_boundary_racing_with_recovery_and_pruning(world):
    w = world
    w["clock"].t = datetime(2026, 10, 31, 23, 40, tzinfo=timezone.utc)
    t = reserve(w, "old")
    w["store"].start(t)
    w["clock"].t = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    nov = w["make_store"]()
    results, lock = [], threading.Lock()

    def refund():
        w["store"].recover(OWNER)

    def fresh():
        r = reserve(w, "new", store=nov)
        with lock:
            results.append(r)

    def retry():
        r = reserve(w, "old", store=nov)
        with lock:
            results.append(r)
    threads = [threading.Thread(target=f) for f in (refund, fresh, retry)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    old = entry(w, "old")
    assert used(w, "2026-11") >= 1
    if old["state"] == sc.REFUNDED:
        assert used(w, "2026-10") == 0
    else:                                                                    # resumed (T12) or re-reserved (T1r)
        assert used(w, "2026-10") + used(w, "2026-11") == 2
    inv(w)
    w["clock"].t = datetime(2026, 12, 5, tzinfo=timezone.utc)                # prune October
    w["store"].recover(OWNER)
    inv(w)


# ── repair target safeguards ────────────────────────────────────────────────
def _message_failed_entry(w, chat_id="c1"):
    w["new_chat"](chat_id)
    t = reserve(w, "r1", chat_id=chat_id)
    w["store"].start(t)
    w["store"].claim(t)
    w["enqueue_fn"]("r1")
    t.e1 = True
    w["store"].mark_enqueued(t)
    w["store"].record_message_failure(t, {**PAYLOAD, "chat_id": "evil"})
    return t


def test_rp1_repair_writes_only_to_the_entry_chat(world, monkeypatch):
    w = world
    _message_failed_entry(w, "A")
    w["new_chat"]("B")
    before_b = w["db"].chats.find_one({"chat_id": "B"})
    assert "chat_id" not in entry(w, "r1")["pending_message"]
    monkeypatch.setattr(w["idx"], "create_chat_session", lambda *a, **k: pytest.fail("chat created"))
    d = reserve(w, "r1", chat_id="B", retry_message=PAYLOAD)
    assert d.kind == "redirect" and d.chat_id == "A"
    assert len(messages(w, "A", rid="r1")) == 1 and w["db"].chats.find_one({"chat_id": "B"}) == before_b


def test_rp1_deleted_chat_is_never_recreated(world):
    w = world
    _message_failed_entry(w, "A")
    w["new_chat"]("B")
    w["db"].chats.delete_one({"chat_id": "A"})
    w["store"].recover(OWNER)
    e = entry(w, "r1")
    assert e["state"] == sc.COMMITTED and e["reason"] == sc.R_CHAT_MISSING
    assert e["pending_message"] is None and e["topic_key"] is None
    assert w["db"].chats.count_documents({}) == 1 and messages(w, "B") == []
    assert reserve(w, "r1", chat_id="B", retry_message=PAYLOAD).chat_id is None
    inv(w)


def test_rp2_retry_inputs_with_another_topic_do_not_repair(world):
    w = world
    _held_send(w)
    w["jobs"].release()
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    assert entry(w, "r1")["reason"] == sc.R_MSG_MISSING
    d = reserve(w, "r1", retry_message={**PAYLOAD, "topic_key": "other"}, topic="other")
    assert d.kind == "reconciliation" and messages(w, rid="r1") == []


def test_rp3_chat_of_another_owner_counts_as_missing(world):
    w = world
    _message_failed_entry(w, "A")
    w["db"].chats.update_one({"chat_id": "A"}, {"$set": {"owner_key": OTHER}})
    w["store"].recover(OWNER)
    assert entry(w, "r1")["reason"] == sc.R_CHAT_MISSING
    assert w["db"].chats.find_one({"chat_id": "A"})["messages"] == []


def test_rp4_chat_id_is_never_rewritten(world):
    w = world
    w["new_chat"]()
    for rid in ("a", "b"):
        t = reserve(w, rid)
        w["store"].run(t, w["enqueue_fn"], PAYLOAD)
    t = reserve(w, "c")
    w["store"].self_refund(t)
    reserve(w, "c")
    w["clock"].advance(901)
    reserve(w, "c")                                                          # resume
    w["store"].recover(OWNER)
    for filt, update in w["coll"].calls:
        for key in (update.get("$set") or {}):
            assert not key.endswith(".chat_id"), (filt, update)


def test_rp5_late_delivery_from_another_chat_is_refused(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["store"].start(t)
    w["store"].claim(t)
    w["enqueue_fn"]("r1")
    t.e1 = True
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    w["store"].resolve(OWNER, "r1", "close")
    t.chat_id = "elsewhere"
    w["store"].late_delivery(t, PAYLOAD)
    assert messages(w, rid="r1") == []


def test_rp6_refunded_entries_never_keep_pending_messages(world):
    w = world
    t = reserve(w, "r1")
    w["store"].self_refund(t)
    assert entry(w, "r1")["pending_message"] is None
    t2 = reserve(w, "r1")
    assert entry(w, "r1")["pending_message"] is None and t2.state == sc.RESERVED


# ── operator close, the marker, C3 and C3i ──────────────────────────────────
def _needs_rec(w, landed=True):
    """An entry in needs_reconciliation whose original request is still 'running'."""
    w["new_chat"]()
    t = reserve(w, "r1")
    w["store"].start(t)
    w["store"].claim(t)
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    if landed:
        w["enqueue_fn"]("r1")
        t.e1 = True
    return t


def _marker(w, rid="r1", chat_id="c1"):
    return ((w["db"].chats.find_one({"chat_id": chat_id}) or {}).get("credit_close") or {}).get(rid)


def _place_closing_marker(w, token="old", at=None, rid="r1"):
    w["db"].chats.update_one({"chat_id": "c1"}, {"$set": {f"credit_close.{rid}": {
        "phase": "closing", "token": token, "at": at or w["clock"]()}}})


def test_m1_marker_present_and_entry_not_terminal(world):
    w = world
    t = _needs_rec(w)
    _place_closing_marker(w)                                                # operator stopped after C1
    assert w["store"].mark_enqueued(t) == sc.DONE                           # E1 overrides ambiguous
    d = w["store"]._save_and_commit(t, PAYLOAD)
    assert d.kind == "reconciliation" and messages(w, rid="r1") == []
    e = entry(w, "r1")
    assert e["state"] == sc.ENQUEUED and e["pending_message"]["query"] == PAYLOAD["query"]
    w["store"].recover(OWNER)                                               # repair blocked by the marker
    assert messages(w, rid="r1") == [] and _marker(w)["phase"] == "closing"
    assert w["idx"].add_search_to_chat("c1", OWNER, credit_request_id="r1", late_delivery=(t.attempt, "x"),
                                       **PAYLOAD) is False
    assert lookup_kind(w) in ("busy", "reconciliation")
    inv(w)


def lookup_kind(w):
    return w["store"].lookup(OWNER, "r1").kind


@pytest.mark.parametrize("order", ["save,c1", "c1,save,c3", "c1,c3,save"])
def test_m2_marker_creation_racing_with_original_delivery(world, order):
    w = world
    t = _needs_rec(w)
    w["store"].mark_enqueued(t)
    steps = order.split(",")
    if steps[0] == "save":
        assert w["store"]._save_and_commit(t, PAYLOAD).kind == "done"
        res = w["store"].resolve(OWNER, "r1", "close")
        assert res["result"] in ("superseded", "already_present")
        assert len(messages(w, rid="r1")) == 1
    else:
        _place_closing_marker(w, token="op")
        if steps[1] == "save":
            assert w["store"]._save_and_commit(t, PAYLOAD).kind == "reconciliation"
            assert messages(w, rid="r1") == []
        # the operator finishes its own close (C2 then C3) holding token "op"
        fresh = entry(w, "r1")
        assert w["store"]._cas(OWNER, "r1", sc._exp(fresh), set_={
            "state": sc.COMMITTED, "reason": sc.R_MANUAL, "close_token": "op"})[0]
        assert w["store"].complete_c3(OWNER, "r1", "c1", "op", fresh["attempt"])
        if steps[1] == "c3":
            w["store"].late_delivery(t, PAYLOAD)                            # the original's read sees the close
            assert len(messages(w, rid="r1")) == 1
            assert any("late_delivery_at" in n for n in entry(w, "r1")["audit"])
        else:
            assert messages(w, rid="r1") == []
    inv(w)


def test_m3_operator_crash_after_c1_then_rerun(world):
    w = world
    _needs_rec(w, landed=False)                                             # no evidence: stays unresolved
    _place_closing_marker(w, token="crashed")
    assert w["store"].resolve(OWNER, "r1", "close")["result"] == "close_in_progress"
    w["store"].recover(OWNER)
    assert _marker(w)["token"] == "crashed" and messages(w, rid="r1") == []
    assert w["store"].resolve(OWNER, "r1", "close", force=True)["result"] == "closed"
    assert _marker(w)["phase"] == "closed" and entry(w, "r1")["reason"] == sc.R_MANUAL
    inv(w)


def test_m3_operator_crash_after_c1_then_abort(world):
    w = world
    t = _needs_rec(w)
    _place_closing_marker(w, token="crashed")
    w["store"].mark_enqueued(t)
    w["store"]._save_and_commit(t, PAYLOAD)                                 # blocked, message stored
    ev_before = entry(w, "r1")["ev"]
    assert w["store"].resolve(OWNER, "r1", "abort_close")["result"] == "aborted"
    assert _marker(w) is None and entry(w, "r1")["ev"] > ev_before
    w["store"].recover(OWNER)
    assert len(messages(w, rid="r1")) == 1 and entry(w, "r1")["reason"] == sc.R_REPAIRED
    inv(w)


def test_m4_crash_after_c2_before_c3(world):
    w = world
    t = _needs_rec(w)
    fresh = entry(w, "r1")
    w["store"]._cas(OWNER, "r1", sc._exp(fresh), set_={"state": sc.COMMITTED, "reason": sc.R_MANUAL,
                                                        "close_token": "T1"})
    _place_closing_marker(w, token="T1")
    w["store"].late_delivery(t, PAYLOAD)
    assert messages(w, rid="r1") == []                                      # S-late blocked until C3
    w["store"].recover(OWNER)
    assert _marker(w)["phase"] == "closing"                                 # younger than 900 s
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    assert _marker(w)["phase"] == "closed" and _marker(w)["close_token"] == "T1"
    w["store"].late_delivery(t, PAYLOAD)
    assert len(messages(w, rid="r1")) == 1
    inv(w)


def test_m5_old_paused_run_and_new_forced_run(world):
    w = world
    _needs_rec(w)
    stale = entry(w, "r1")
    _place_closing_marker(w, token="old")
    assert w["store"].resolve(OWNER, "r1", "close", force=True)["result"] == "closed"
    ok, _ = w["store"]._cas(OWNER, "r1", sc._exp(stale), set_={"state": sc.COMMITTED, "reason": sc.R_MANUAL,
                                                                "close_token": "old"})
    assert ok is False                                                       # the old C2 fails
    assert w["store"].complete_c3(OWNER, "r1", "c1", "old", stale["attempt"]) is False
    assert _marker(w)["phase"] == "closed"
    inv(w)


@pytest.mark.parametrize("abort_first", [True, False])
def test_m6_abort_against_old_c2(world, abort_first):
    w = world
    _needs_rec(w)
    stale = entry(w, "r1")
    _place_closing_marker(w, token="old")
    c2 = lambda: w["store"]._cas(OWNER, "r1", sc._exp(stale), set_={  # noqa: E731
        "state": sc.COMMITTED, "reason": sc.R_MANUAL, "close_token": "old"})[0]
    if abort_first:
        assert w["store"].resolve(OWNER, "r1", "abort_close")["result"] == "aborted"
        assert c2() is False and entry(w, "r1")["state"] == sc.NEEDS_REC
    else:
        assert c2() is True
        assert w["store"].resolve(OWNER, "r1", "abort_close")["result"] == "closed"
        assert _marker(w)["phase"] == "closed"
    inv(w)


def test_m7_close_with_the_chat_deleted(world):
    w = world
    _needs_rec(w)
    w["db"].chats.delete_one({"chat_id": "c1"})
    assert w["store"].resolve(OWNER, "r1", "close")["result"] == "closed"
    assert w["db"].chats.count_documents({}) == 0 and entry(w, "r1")["reason"] == sc.R_MANUAL
    inv(w)


def test_c3a_closed_marker_but_entry_not_terminal(world):
    w = world
    t = _needs_rec(w)
    w["db"].chats.update_one({"chat_id": "c1"}, {"$set": {"credit_close.r1": {
        "phase": "closed", "token": "x", "attempt": t.attempt, "close_token": "x"}}})
    w["store"].late_delivery(t, PAYLOAD)
    assert messages(w, rid="r1") == []
    assert w["store"].resolve(OWNER, "r1", "close")["result"] == "close_marker_inconsistent"
    w["store"].recover(OWNER)
    assert _marker(w)["phase"] == "closed"
    assert w["store"].resolve(OWNER, "r1", "abort_close")["result"] == "aborted" and _marker(w) is None


@pytest.mark.parametrize("bad", ["attempt", "close_token"])
def test_c3b_mismatched_closed_marker_blocks_s_late(world, bad):
    w = world
    t = _needs_rec(w)
    w["store"].resolve(OWNER, "r1", "close")
    w["db"].chats.update_one({"chat_id": "c1"}, {"$set": {f"credit_close.r1.{bad}": "wrong"}})
    w["store"].late_delivery(t, PAYLOAD)
    assert messages(w, rid="r1") == []


def test_c3e_recovery_takeover_is_narrow(world):
    w = world
    _needs_rec(w)
    _place_closing_marker(w, token="op", at=w["clock"]() - timedelta(hours=2))
    w["store"].recover(OWNER)
    assert _marker(w)["token"] == "op"                                      # not for a non-terminal entry
    fresh = entry(w, "r1")
    w["store"]._cas(OWNER, "r1", sc._exp(fresh), set_={"state": sc.COMMITTED, "reason": sc.R_MANUAL,
                                                        "close_token": "op"})
    w["db"].chats.update_one({"chat_id": "c1"}, {"$set": {"credit_close.r1.at": w["clock"]()}})
    w["store"].recover(OWNER)
    assert _marker(w)["phase"] == "closing"                                 # younger than 900 s
    w["clock"].advance(901)
    w["store"].recover(OWNER)
    assert _marker(w)["phase"] == "closed"


def test_c3f_pruned_entry_with_a_closing_marker(world):
    w = world
    _needs_rec(w)
    fresh = entry(w, "r1")
    w["store"]._cas(OWNER, "r1", sc._exp(fresh), set_={"state": sc.COMMITTED, "reason": sc.R_MANUAL,
                                                        "close_token": "op"})
    _place_closing_marker(w, token="op")
    w["clock"].t = datetime(2026, 12, 20, tzinfo=timezone.utc)
    w["store"]._prune(OWNER)
    assert entry(w, "r1") is None
    assert w["store"].complete_c3(OWNER, "r1", "c1", "op", fresh["attempt"]) is False
    assert _marker(w)["phase"] == "closing"
    inv(w)


def test_c3g_takeover_without_force_before_900_seconds(world):
    w = world
    _needs_rec(w)
    _place_closing_marker(w, token="op")
    assert w["store"].resolve(OWNER, "r1", "close")["result"] == "close_in_progress"
    w["clock"].advance(901)
    assert w["store"].resolve(OWNER, "r1", "close")["result"] == "closed"


@pytest.mark.parametrize("takeover_first", [True, False])
def test_c3h_takeover_against_the_older_holder_c3(world, takeover_first):
    w = world
    _needs_rec(w)
    fresh = entry(w, "r1")
    w["store"]._cas(OWNER, "r1", sc._exp(fresh), set_={"state": sc.COMMITTED, "reason": sc.R_MANUAL,
                                                        "close_token": "old"})
    _place_closing_marker(w, token="old")
    marker = _marker(w)
    if takeover_first:
        assert w["store"]._take_over(OWNER, "c1", "r1", marker, "new")
        assert w["store"].complete_c3(OWNER, "r1", "c1", "old", fresh["attempt"]) is False
        assert w["store"].complete_c3(OWNER, "r1", "c1", "new", fresh["attempt"]) is True
    else:
        assert w["store"].complete_c3(OWNER, "r1", "c1", "old", fresh["attempt"]) is True
        assert w["store"]._take_over(OWNER, "c1", "r1", marker, "new") is False
    m = _marker(w)
    assert m["phase"] == "closed" and m["close_token"] == "old" and m["attempt"] == fresh["attempt"]


@pytest.mark.parametrize("problem", ["deleted", "not_committed", "other_reason", "attempt", "chat_id",
                                     "empty_token", "missing_token"])
def test_c3i_precheck_refuses_an_inconsistent_entry(world, problem, caplog):
    w = world
    _needs_rec(w)
    fresh = entry(w, "r1")
    set_ = {"state": sc.COMMITTED, "reason": sc.R_MANUAL, "close_token": "op"}
    if problem == "not_committed":
        set_ = {}
    elif problem == "other_reason":
        set_["reason"] = sc.R_REPAIRED
    elif problem == "chat_id":
        set_["chat_id"] = "other"
    elif problem == "empty_token":
        set_["close_token"] = ""
    elif problem == "missing_token":
        set_["close_token"] = None
    if set_:
        w["store"]._cas(OWNER, "r1", sc._exp(fresh), set_=set_)
    if problem == "deleted":
        w["db"].flintel_credit_usage.update_one({"owner_key": OWNER}, {"$unset": {"res.r1": ""}})
    _place_closing_marker(w, token="op")
    before = _marker(w)
    attempt = "someone-else" if problem == "attempt" else fresh["attempt"]
    assert w["store"].complete_c3(OWNER, "r1", "c1", "op", attempt) is False
    assert _marker(w) == before and "close_precheck_failed" in caplog.text
    assert w["idx"].add_search_to_chat("c1", OWNER, credit_request_id="r1",
                                       late_delivery=(fresh["attempt"], "op"), **PAYLOAD) is False


# ── message save failures ───────────────────────────────────────────────────
def test_message_save_raising_is_repaired_once(world):
    w = world
    w["new_chat"]()
    t = reserve(w, "r1")
    w["chats"].on(lambda f, u: "messages" in (u.get("$push") or {}), "apply_then_raise")
    d = w["store"].run(t, w["enqueue_fn"], PAYLOAD)
    assert d.kind == "redirect" and entry(w, "r1")["state"] == sc.MESSAGE_FAILED
    w["store"].recover(OWNER)
    w["store"].recover(OWNER)
    e = entry(w, "r1")
    assert e["state"] == sc.COMMITTED and e["reason"] == sc.R_ALREADY and len(messages(w, rid="r1")) == 1
    inv(w)


# ── status and pruning ──────────────────────────────────────────────────────
def test_status_reports_the_current_month(world):
    w = world
    w["new_chat"]()
    for i in range(3):
        w["store"].run(reserve(w, f"r{i}"), w["enqueue_fn"], PAYLOAD)
    s = w["store"].status(OWNER)
    assert s["status"] == "ok" and s["used"] == 3 and s["remaining"] == 37 and s["limit"] == 40
    assert s["period"] == "2026-10" and s["resets_at"] == "2026-11-01T00:00:00+00:00"
    assert s["needs_reconciliation"] == 0
    w["coll"].on(lambda f, u: True, "raise", times=100)
    w["coll"].inner.find_one = None
    broken = sc.CreditStore(type("Bad", (), {"find_one": lambda *a, **k: (_ for _ in ()).throw(NetworkTimeout("x"))})(),
                            w["chats"], w["jobs"], w["idx"].add_search_to_chat, clock=w["clock"], enabled=lambda: True)
    assert broken.status(OWNER)["status"] == "unavailable"


def test_pruning_archives_old_committed_entries(world):
    w = world
    w["new_chat"]()
    w["store"].run(reserve(w, "r1"), w["enqueue_fn"], PAYLOAD)
    t = reserve(w, "r2")
    w["store"].self_refund(t)
    w["clock"].t = datetime(2026, 12, 2, tzinfo=timezone.utc)
    w["store"].recover(OWNER)
    d = doc(w)
    assert d["res"] == {} and d["archived_by_period"]["2026-10"] == 1 and d["used_by_period"]["2026-10"] == 1
    inv(w)


def test_default_store_is_none_with_the_kill_switch(monkeypatch):
    import config
    monkeypatch.setattr(config, "CREDIT_ENFORCEMENT_DISABLED", True)
    assert sc.default_store() is None and sc.enforcement_enabled() is False


# ── random interleavings ────────────────────────────────────────────────────
@pytest.mark.parametrize("seed", range(6))
def test_random_interleavings_keep_every_invariant(world, seed):
    w = world
    rng = random.Random(seed)
    w["state"]["limit"] = 8
    w["new_chat"]()
    store = w["store"]
    tickets, refunded_attempts, enqueue_calls = {}, set(), []
    real_enqueue = w["enqueue_fn"]

    def enqueue_fn(rid):
        enqueue_calls.append((rid, tickets[rid].attempt))
        return real_enqueue(rid)

    rids = [f"r{i}" for i in range(60)]
    months = [datetime(2026, 10, 3, tzinfo=timezone.utc), datetime(2026, 11, 3, tzinfo=timezone.utc),
              datetime(2026, 12, 3, tzinfo=timezone.utc)]
    for step in range(400):
        op = rng.choice(["reserve", "reserve", "run", "claim_only", "recover", "advance", "close", "flaky", "month"])
        rid = rng.choice(rids)
        try:
            _random_step(w, rng, store, op, rid, tickets, enqueue_fn, months)
        except sc.CreditStoreError:
            pass                                                             # the route answers 503
        d = doc(w) or {}
        assert sc.check_invariant(d) == [], (step, op)
        for r, e in (d.get("res") or {}).items():
            if e.get("state") == sc.REFUNDED:
                assert not e.get("sent_once")
                refunded_attempts.add((r, e.get("attempt")))
        for period, c in (d.get("used_by_period") or {}).items():
            assert 0 <= int(c) <= 8
    w["coll"].rules.clear()
    w["jobs"].rules.clear()
    w["jobs"].release()
    assert len(enqueue_calls) == len(set(enqueue_calls))      # each reservation enqueues at most once
    assert not (refunded_attempts & set(enqueue_calls))      # no reservation both refunded and enqueued
    inv(w)


def _random_step(w, rng, store, op, rid, tickets, enqueue_fn, months):
    """One random operation; credit-store errors propagate like they do in the route."""
    if op == "reserve":
        r = reserve(w, rid, retry_message=PAYLOAD)
        if isinstance(r, sc.Ticket):
            tickets[rid] = r
    elif op == "run" and rid in tickets and tickets[rid].state in (sc.RESERVED, sc.STARTED):
        store.run(tickets[rid], enqueue_fn, PAYLOAD)
    elif op == "claim_only" and rid in tickets and tickets[rid].state in (sc.RESERVED, sc.STARTED):
        t = tickets[rid]
        if t.state == sc.RESERVED:
            store.start(t)
        store.claim(t)
    elif op == "recover":
        store.recover(OWNER)
    elif op == "advance":
        w["clock"].advance(rng.choice([60, 901, 3600]))
    elif op == "close":
        e = entry(w, rid)
        if e and e["state"] == sc.NEEDS_REC:
            store.resolve(OWNER, rid, "close", force=rng.random() < 0.5)
    elif op == "flaky":
        target = rng.choice([w["jobs"], w["coll"]])
        target.on(lambda f, u: True, rng.choice(["apply_then_raise", "raise", "hold"]))
        if rng.random() < 0.5:
            w["jobs"].release()
    elif op == "month":
        w["clock"].t = max(w["clock"].t, rng.choice(months))


# ═════════════════════════════════════════════════════════════════════════════
# FOLLOW-UP: opt-in job attribution, the code-121 fallback, the precheck,
# and toggling enforcement.
# ═════════════════════════════════════════════════════════════════════════════
from pymongo.errors import (  # noqa: E402
    AutoReconnect, ExecutionTimeout, OperationFailure, WriteConcernError, WriteError, WTimeoutError)

REJECT_121 = WriteError("Document failed validation", code=121)


def _job_doc(w, jobs=None):
    d = dict((jobs or w["db"].jobs).find_one({"topic_key": TOPIC}) or {})
    d.pop("_id", None)
    d.pop("requested_at", None)
    return d


def test_attribution_mode_parsing(caplog):
    import config
    assert config._parse_job_attribution_mode(None) == "off" and config._parse_job_attribution_mode("") == "off"
    assert config._parse_job_attribution_mode("field") == "field" and config._parse_job_attribution_mode(" FIELD ") == "field"
    assert config._parse_job_attribution_mode("off") == "off"
    for bad in ("on", "true", "1", "fields"):
        assert config._parse_job_attribution_mode(bad) == "off"
    assert caplog.text.count("JOB_ATTRIBUTION_MODE=") == 4


@pytest.mark.parametrize("mode,expected", [("off", False), ("field", True)])
def test_job_attribution_enabled_reads_the_mode(world, monkeypatch, mode, expected):
    import config
    monkeypatch.setattr(config, "JOB_ATTRIBUTION_MODE", mode)
    assert sc.job_attribution_enabled() is expected and sc.attribution_mode() == mode


def test_off_mode_job_document_is_identical_to_the_pre_credit_write(world):
    w = world
    lg = w["lg"]
    legacy_jobs = w["db"].legacy_jobs
    lg.jobs_collection = legacy_jobs
    assert lg.enqueue_search_job(TOPIC, ["crm buyers"], "reddit") is None          # pre-credit call
    lg.jobs_collection = w["jobs"]
    result = lg.enqueue_search_job(TOPIC, ["crm buyers"], "reddit", credit_request_id="r1", attribute=False)
    assert sc.is_e1(result)                                                         # E1 without any new field
    assert _job_doc(w) == _job_doc(w, legacy_jobs) and "credit_request_ids" not in _job_doc(w)
    assert all("$push" not in u for _, u in w["jobs"].calls)


def test_off_mode_normal_search_commits_on_its_own_write_result(world):
    w = world
    w["state"]["attribute"] = False
    w["new_chat"]()
    d = w["store"].run(reserve(w, "r1"), w["enqueue_fn"], PAYLOAD)
    assert d.kind == "done" and entry(w, "r1")["reason"] == sc.R_NORMAL and job_ids(w) == []
    inv(w)


@pytest.mark.parametrize("mode", ["apply_then_raise", "raise"])
def test_off_mode_ambiguous_enqueue_is_held_never_refunded(world, mode):
    w = world
    w["state"]["attribute"] = False
    w["new_chat"]()
    w["jobs"].on(lambda f, u: "$set" in u and "keywords" in u["$set"], mode)       # off mode: no $push to match
    assert w["store"].run(reserve(w, "r1"), w["enqueue_fn"], PAYLOAD).kind == "unavailable"
    for _ in range(3):
        w["store"].recover(OWNER)
        w["clock"].advance(901)
    e = entry(w, "r1")
    assert e["state"] == sc.NEEDS_REC and e["reason"] == sc.R_AMBIGUOUS and used(w) == 1
    assert reserve(w, "r1").kind == "reconciliation" and len(w["state"]["enqueues"]) == 1
    inv(w)


def test_field_stripped_by_the_collector_is_held_never_refunded(world):
    w = world
    w["new_chat"]()
    w["jobs"].on(is_job_write, "apply_then_raise")
    w["store"].run(reserve(w, "r1"), w["enqueue_fn"], PAYLOAD)
    w["db"].jobs.update_one({"topic_key": TOPIC}, {"$unset": {"credit_request_ids": ""}})   # collector rewrite
    w["store"].recover(OWNER)
    e = entry(w, "r1")
    assert e["state"] == sc.NEEDS_REC and e["reason"] == sc.R_AMBIGUOUS and used(w) == 1
    inv(w)


def test_definitive_121_rejection_falls_back_once_without_the_field(world, caplog):
    w = world
    w["new_chat"]()
    w["jobs"].on(is_job_write, ("raise_exc", REJECT_121))                          # rejected, NOT applied
    d = w["store"].run(reserve(w, "r1"), w["enqueue_fn"], PAYLOAD)
    assert d.kind == "done" and entry(w, "r1")["reason"] == sc.R_NORMAL and used(w) == 1
    job_writes = [u for _, u in w["jobs"].calls if "$set" in u and "keywords" in u["$set"]]
    assert len(job_writes) == 2 and "$push" in job_writes[0] and "$push" not in job_writes[1]
    assert "credit_request_ids" not in _job_doc(w) and w["lg"]._JOB_ATTRIBUTION_REJECTED["value"] is True
    assert "code 121" in caplog.text and PAYLOAD["query"] not in caplog.text
    import config
    config.JOB_ATTRIBUTION_MODE = "field"
    try:
        assert sc.job_attribution_enabled() is False and sc.attribution_mode() == "field_rejected"
    finally:
        config.JOB_ATTRIBUTION_MODE = "off"
    w["lg"].enqueue_search_job(TOPIC, ["crm buyers"], "reddit", credit_request_id="r2", attribute=True)
    assert "$push" not in w["jobs"].calls[-1][1]                                     # stays off in this process
    inv(w)


def test_121_fallback_is_refused_when_the_id_is_already_on_the_job(world):
    w = world
    w["new_chat"]()
    w["jobs"].on(is_job_write, ("apply_then_exc", REJECT_121))                     # inconsistent: applied AND 121
    assert w["store"].run(reserve(w, "r1"), w["enqueue_fn"], PAYLOAD).kind == "unavailable"
    job_writes = [u for _, u in w["jobs"].calls if "$set" in u and "keywords" in u["$set"]]
    assert len(job_writes) == 1 and w["lg"]._JOB_ATTRIBUTION_REJECTED["value"] is False
    assert entry(w, "r1")["state"] == sc.SENDING and entry(w, "r1")["failed_at"] and used(w) == 1


def test_121_fallback_is_refused_when_the_confirmation_read_fails(world):
    w = world
    w["jobs"].on(is_job_write, ("raise_exc", REJECT_121))
    w["jobs"].find_one = lambda *a, **k: (_ for _ in ()).throw(NetworkTimeout("read timed out"))
    with pytest.raises(WriteError):
        w["lg"].enqueue_search_job(TOPIC, ["x"], "reddit", credit_request_id="r1", attribute=True)
    assert w["lg"]._JOB_ATTRIBUTION_REJECTED["value"] is False


AMBIGUOUS = [NetworkTimeout("timed out"), AutoReconnect("reconnect"), ExecutionTimeout("max time", code=50),
             WriteConcernError("wc", code=64), WTimeoutError("wtimeout", code=64),
             WriteError("duplicate", code=11000), OperationFailure("not a write error", code=121)]


@pytest.mark.parametrize("applied", [False, True], ids=["not_applied", "applied"])
@pytest.mark.parametrize("exc", AMBIGUOUS, ids=[type(e).__name__ + str(getattr(e, "code", "")) for e in AMBIGUOUS])
def test_ambiguous_or_other_errors_never_fall_back(world, exc, applied):
    w = world
    w["jobs"].on(is_job_write, ("apply_then_exc" if applied else "raise_exc", exc))
    with pytest.raises(type(exc)):
        w["lg"].enqueue_search_job(TOPIC, ["x"], "reddit", credit_request_id="r1", attribute=True)
    assert len(w["jobs"].calls) == 1 and w["lg"]._JOB_ATTRIBUTION_REJECTED["value"] is False
    assert w["lg"]._is_definitive_validation_rejection(exc) is False
    assert w["lg"]._is_definitive_validation_rejection(REJECT_121) is True


def test_pre_credit_enqueue_errors_still_propagate(world):
    w = world
    w["jobs"].on(lambda f, u: True, ("raise_exc", REJECT_121))
    with pytest.raises(WriteError):
        w["lg"].enqueue_search_job(TOPIC, ["x"], "reddit")                         # no id: never a fallback
    assert len(w["jobs"].calls) == 1


# ── precheck: optimization only ─────────────────────────────────────────────
def _snapshot(w):
    d = doc(w)
    return None if d is None else {k: v for k, v in d.items() if k != "_id"}


def test_precheck_is_read_only_and_only_denies_new_ids_at_the_limit(world):
    w = world
    w["state"]["limit"] = 2
    store = w["store"]
    assert store.precheck_exhausted(OWNER, "new") is None                           # no document yet
    reserve(w, "a")
    t_b = reserve(w, "b")
    before, calls = _snapshot(w), len(w["coll"].calls)
    d = store.precheck_exhausted(OWNER, "new")
    assert d.kind == "exhausted" and d.cleanup_ok and d.resets_at == datetime(2026, 11, 1, tzinfo=timezone.utc)
    for existing in ("a", "b"):
        assert store.precheck_exhausted(OWNER, existing) is None                    # same key: the reservation decides
    store.self_refund(t_b)
    assert store.precheck_exhausted(OWNER, "b") is None                              # refunded entry: still left to T1r
    assert store.precheck_exhausted(OWNER, "new") is None                            # a credit is free again
    assert len(w["coll"].calls) == calls + 1                                          # only the self-refund wrote
    assert _snapshot(w)["used_by_period"] == {"2026-10": 1} and before["used_by_period"] == {"2026-10": 2}
    inv(w)


def test_precheck_returns_none_on_a_store_error(world):
    w = world
    broken = sc.CreditStore(type("Bad", (), {"find_one": lambda *a, **k: (_ for _ in ()).throw(NetworkTimeout("x"))})(),
                            w["chats"], w["jobs"], w["idx"].add_search_to_chat, clock=w["clock"],
                            enabled=lambda: True, limit=lambda: 1)
    assert broken.precheck_exhausted(OWNER, "x") is None


def test_two_requests_competing_for_the_final_credit(world):
    w = world
    w["state"]["limit"] = 3
    reserve(w, "a")
    reserve(w, "b")
    store = w["store"]
    assert store.precheck_exhausted(OWNER, "c") is None and store.precheck_exhausted(OWNER, "d") is None
    first, second = reserve(w, "c"), reserve(w, "d")                                 # both passed the precheck
    assert isinstance(first, sc.Ticket) and second.kind == "exhausted"               # the reservation decides
    assert store.precheck_exhausted(OWNER, "e").kind == "exhausted"
    retry = store.precheck_exhausted(OWNER, "c")                                     # same-key retry of the winner
    assert retry is None and reserve(w, "c").kind == "busy" and used(w) == 3
    inv(w)


def test_concurrent_precheck_and_reservation_for_the_final_credits(world):
    w = world
    w["state"]["limit"] = 5
    store = w["store"]
    for i in range(3):
        reserve(w, f"pre{i}")
    results, prechecked, lock = [], [], threading.Lock()

    def go(i):
        rid = f"t{i}" if i < 16 else "pre0"                                         # 4 same-key retries
        d = store.precheck_exhausted(OWNER, rid)
        r = d if d is not None else reserve(w, rid)
        with lock:
            results.append((rid, r))
            prechecked.append(d is not None)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    tickets = [rid for rid, r in results if isinstance(r, sc.Ticket)]
    assert len(tickets) == 2 and used(w) == 5                                         # exactly the two final credits
    assert all(r.kind == "busy" for rid, r in results if rid == "pre0")              # retries: no new charge
    assert all(r.kind == "exhausted" for rid, r in results if rid != "pre0" and not isinstance(r, sc.Ticket))
    inv(w)


# ── enforcement toggled off and back on ─────────────────────────────────────
def test_entries_survive_a_switch_off_and_recover_by_state_when_back_on(world):
    w = world
    w["new_chat"]()
    reserve(w, "held")                                                               # reserved, never sent
    t = reserve(w, "sent")
    w["store"].start(t)
    w["store"].claim(t)                                                              # sending; job never written
    w["store"].run(reserve(w, "done"), w["enqueue_fn"], PAYLOAD)
    frozen = _snapshot(w)
    # ── switched off: the pre-credit path runs; nothing touches credit documents ──
    w["lg"].enqueue_search_job("untracked topic", ["x"], "reddit")
    w["idx"].add_search_to_chat("c1", OWNER, "untracked", "untracked topic", ["x"], "reddit")
    w["clock"].advance(3 * 3600)
    assert _snapshot(w) == frozen
    # ── back on: the ordinary state- and time-based rules apply ──
    w["store"].recover(OWNER)
    assert entry(w, "held")["state"] == sc.REFUNDED                                  # never sent: refunded
    assert entry(w, "sent")["state"] == sc.NEEDS_REC and entry(w, "sent")["reason"] == sc.R_AMBIGUOUS   # held
    assert entry(w, "done")["state"] == sc.COMMITTED and used(w) == 2
    untracked = [m for m in messages(w) if m["query"] == "untracked"]
    assert len(untracked) == 1 and "credit_request_id" not in untracked[0]          # never charged afterwards
    assert set((doc(w)["res"]).keys()) == {"held", "sent", "done"}
    inv(w)
