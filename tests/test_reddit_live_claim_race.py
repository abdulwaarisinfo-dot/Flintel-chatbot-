"""
tests/test_reddit_live_claim_race.py — the atomic per-message claim of reddit_live.process, under FORCED interleaving.

The property: however many callers have already read the SAME unclaimed message, at most ONE of them may reach the paid HTTP
request. The database claim (`update_one` filtered on `messages.<i>.live_reddit: {"$exists": False}`) is what guarantees it.

Deterministic by construction - no sleeps, no reliance on thread timing or on the GIL:
  * test 1 needs no threads at all: every caller is handed the SAME pre-claim snapshot of the chat as its first read (a stale
    read), then the callers run one after another;
  * test 2 uses real threads, but only to put every caller INSIDE process() having read the unclaimed message; a barrier makes
    all of them wait until all have read, then an event "turnstile" lets them continue strictly one at a time in a fixed order.
    Waits carry a timeout only so a broken test fails instead of hanging.
The per-owner minimum interval would otherwise hide a second claim (it would refuse the second budget slot), so the clock is
advanced past it for every later caller (each caller has its own fixed clock): a duplicate claim then reaches the HTTP layer if, and only if, the claim filter allows it.

Everything is mocked: mongomock, httpx.MockTransport (so the count is taken at the real HTTP layer), a made-up key, no network.
"""
import copy
import socket
import threading
from datetime import timedelta

import pytest

mongomock = pytest.importorskip("mongomock")
import httpx as _REAL_HTTPX  # noqa: E402  (bound before any other test module can stub sys.modules['httpx'])
import reddit_live as rl  # noqa: E402
from tests.test_reddit_live import Env, OWNER, NOW, good_response, mock_httpx, on  # noqa: F401,E402  (fixtures + builders)

STEP = timedelta(seconds=rl.config.REDDIT_LIVE_OWNER_MIN_INTERVAL_SECONDS + 1)     # > the per-owner interval, << the answered_at window


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def _no_inflight_leak():
    assert rl.INFLIGHT.count == 0
    yield
    assert rl.INFLIGHT.count == 0, "an in-flight slot was leaked"


def _is_claim(update):
    """True for the one write that sets a message's whole live_reddit record (the claim)."""
    sets = (update or {}).get("$set") or {}
    return any(k.endswith(".live_reddit") and isinstance(v, dict) and v.get("state") == rl.STATE_CLAIMING for k, v in sets.items())


class Chats:
    """Collection wrapper: records the order of reads and claims; optionally hands out a stale snapshot, gates the first read,
    or drops the claim's `$exists: False` condition (to prove the scenario is sensitive to it)."""

    def __init__(self, real, name, log, snapshot=None, on_first_read=None, drop_exists=False):
        self.real, self.name, self.log = real, name, log
        self.snapshot, self.on_first_read, self.drop_exists = snapshot, on_first_read, drop_exists
        self.read_once = False

    def find_one(self, *a, **k):
        if self.read_once:
            return self.real.find_one(*a, **k)
        self.read_once = True
        doc = copy.deepcopy(self.snapshot) if self.snapshot is not None else self.real.find_one(*a, **k)
        self.log.append(("read", self.name))
        if self.on_first_read:
            self.on_first_read()
        return doc

    def update_one(self, flt, upd, *a, **k):
        if _is_claim(upd):
            self.log.append(("claim", self.name))
            if self.drop_exists:
                flt = {key: v for key, v in flt.items() if not (key.endswith(".live_reddit") and v == {"$exists": False})}
        return self.real.update_one(flt, upd, *a, **k)

    def __getattr__(self, item):
        return getattr(self.real, item)


@pytest.fixture
def http(mock_httpx, on, monkeypatch):
    """The real reddit_live._http_post over httpx.MockTransport; `http['requests']` is the count of paid requests."""
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_GLOBAL", 1000, raising=False)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_PER_OWNER", 1000, raising=False)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", rl.INFLIGHT_CEILING, raising=False)
    mock_httpx["install"](lambda req: _REAL_HTTPX.Response(200, json=good_response()))
    return mock_httpx


def _env():
    env = Env()
    env.deps.post = rl._http_post
    return env


def _call(env):
    return rl.process(env.deps, owner_key=OWNER, owner_type="email", chat_id="c1", request_id="r0")


def _facts(env, http):
    msg = env.chats.find_one({"chat_id": "c1"})["messages"][0]
    return {"requests": len(http["requests"]), "slots": (env.counter() or {}).get("count", 0),
            "attempts": env.attempts.count_documents({}), "claim_id": (msg.get("live_reddit") or {}).get("claim_id")}


# ── 1. stale reads, no threads at all ───────────────────────────────────────────────────────────────────────────────────
def _run_stale(n, drop_exists):
    env = _env()
    real = env.chats
    snapshot = real.find_one({"chat_id": "c1", "owner_key": OWNER})
    assert "live_reddit" not in snapshot["messages"][0]                       # every caller's first read: the message is unclaimed
    log, results = [], []
    for i in range(n):
        env.clock.t = NOW + STEP * i
        env.deps.chats = Chats(real, i, log, snapshot=snapshot, drop_exists=drop_exists)
        results.append(_call(env))
        if i == 0:
            first_claim = real.find_one({"chat_id": "c1"})["messages"][0]["live_reddit"]["claim_id"]
    return env, log, results, first_claim


@pytest.mark.parametrize("n", [2, 5])
def test_callers_that_all_read_the_unclaimed_message_make_at_most_one_paid_request(http, n):
    env, log, results, first_claim = _run_stale(n, drop_exists=False)
    facts = _facts(env, http)
    assert [e for e in log if e[0] == "claim"] == [("claim", i) for i in range(n)]       # every caller really attempted the claim
    assert facts["requests"] == 1 and facts["slots"] == 1 and facts["attempts"] == 1
    assert facts["claim_id"] == first_claim                                           # the first claim was never overwritten
    assert results[0]["state"] == "ok" and all(r == results[0] for r in results)      # late callers just read the stored outcome


def test_the_stale_read_scenario_is_sensitive_to_the_exists_condition(http):
    """Same scenario with the claim's `$exists: False` condition removed: it MUST let more than one caller reach the HTTP layer,
    otherwise the test above would not detect that condition being deleted from reddit_live.process."""
    env, log, results, first_claim = _run_stale(5, drop_exists=True)
    facts = _facts(env, http)
    assert facts["requests"] > 1 and facts["requests"] == facts["slots"] == facts["attempts"]
    assert facts["claim_id"] != first_claim


# ── 2. real threads, forced to interleave by a barrier and a turnstile (order fixed by events, not by timing) ──────────
def _run_threads(n, drop_exists):
    env = _env()
    real = env.chats
    log, results = [], [None] * n
    all_read = threading.Barrier(n)
    turn = [threading.Event() for _ in range(n + 1)]
    turn[0].set()

    def worker(i):
        def after_first_read():
            all_read.wait(30)                    # nobody goes on until EVERY caller has read the unclaimed message
            assert turn[i].wait(30)              # then strictly in order: caller i runs only after caller i-1 has finished
        deps = rl.Deps(Chats(real, i, log, on_first_read=after_first_read, drop_exists=drop_exists), env.budget, env.attempts,
                       rl._http_post, (lambda i=i: NOW + STEP * i), env.deps.record_usage)   # caller i's own fixed clock
        try:
            results[i] = rl.process(deps, owner_key=OWNER, owner_type="email", chat_id="c1", request_id="r0")
        finally:
            turn[i + 1].set()
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert not any(t.is_alive() for t in threads), "a worker did not finish"
    return env, log, results


@pytest.mark.parametrize("n", [2, 6])
def test_threads_forced_to_read_before_any_claim_make_at_most_one_paid_request(http, n):
    env, log, results = _run_threads(n, drop_exists=False)
    reads = [i for i, e in enumerate(log) if e[0] == "read"]
    claims = [i for i, e in enumerate(log) if e[0] == "claim"]
    assert len(reads) == n and len(claims) == n and max(reads) < min(claims)             # all N reads precede the first claim write
    assert [e[1] for e in log if e[0] == "claim"] == list(range(n))                      # and the claims happen in the forced order
    facts = _facts(env, http)
    assert facts["requests"] == 1 and facts["slots"] == 1 and facts["attempts"] == 1
    assert results[0]["state"] == "ok" and all(r == results[0] for r in results)


def test_the_threaded_scenario_is_sensitive_to_the_exists_condition(http):
    env, log, results = _run_threads(4, drop_exists=True)
    reads = [i for i, e in enumerate(log) if e[0] == "read"]
    claims = [i for i, e in enumerate(log) if e[0] == "claim"]
    assert max(reads) < min(claims)
    assert _facts(env, http)["requests"] > 1
