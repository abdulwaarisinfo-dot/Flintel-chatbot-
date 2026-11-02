"""
tests/test_range_poll_cap.py — stream_answer's wait-for-more-evidence polling is capped for searches with an EXPLICIT time range
(routes.RANGE_SEARCH_MAX_EXTRA_POLLS) and unchanged for searches without one. Everything is mocked: retrieval, the model, Live
(OpenAI + Reddit metadata), the database. The real stream_answer, including its real polling loop, runs.
"""
import datetime
import json
import sys
import time
import types

import pytest

from tests.test_active_intent_workflow import (CRM, EMAIL, HIST_ROW, NOT_LEAD, World, _Req, _no_network, _clean, ain, enable,  # noqa: F401
                                                rl, rs, seed_stream_message, timerange)
from tests.test_reddit_live import on  # noqa: F401
from tests.test_reliability_ux_perf import app_env, env  # noqa: F401


def typed_range():
    """A user-typed range (any explicit range counts): the last 3 hours, as the stored message document."""
    end = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    return timerange.TimeRange(start=end - datetime.timedelta(hours=3), end=end, tz_name="UTC", kind="rolling", phrase="last 3 hours").to_doc(end)


def drive(app_env, monkeypatch, *, query, rows, cap=1, response_timeout=9, msg_over=None, world=None):
    lg, idx, rt, chats, state = app_env
    rs._common_stubs(app_env, monkeypatch)
    monkeypatch.setattr(sys.modules["flintel"], "calculate_search_progress_percent", lambda *a, **k: 1, raising=False)
    monkeypatch.setattr(rt, "get_owner", lambda request: (EMAIL, "email"))
    monkeypatch.setattr(rt, "RANGE_SEARCH_MAX_EXTRA_POLLS", cap)
    calls, pools = [], []
    monkeypatch.setattr(rt, "get_evidence_with_topup", lambda **kw: calls.append(kw) or [dict(r) for r in rows])
    monkeypatch.setattr(rt, "get_matched_signals", lambda *a, **k: [])
    monkeypatch.setattr(rt, "analyze_with_claude", lambda q, pool, extra_context=None: pools.append([dict(r) for r in pool]) or json.dumps(rs.ANSWER))
    if world is not None:
        import active_intent
        active_intent.DEPS_FACTORY = lambda: world.live.deps
        import intent_classifier
        intent_classifier.DEPS_FACTORY = lambda: world.live.cls_deps
    seed_stream_message(chats, "s1", query=query, **(msg_over or {}))
    monkeypatch.setattr(rt, "StreamingResponse", lambda gen, **kw: types.SimpleNamespace(body_iterator=gen))
    monkeypatch.setattr(rt, "STREAM_CHUNK_DELAY_SECONDS", 0, raising=False)
    monkeypatch.setattr(rt, "RESPONSE_TIMEOUT", response_timeout, raising=False)
    monkeypatch.setattr(rt.config, "STRICT_WAIT_SECONDS", 2, raising=False)
    monkeypatch.setattr(rt.config, "NONBLOCKING_STREAM_ENABLED", True, raising=False)
    monkeypatch.setattr(rt, "strict_intent_mode", lambda: False)                    # the default: strict mode is NOT turned on
    monkeypatch.setattr(rt, "get_scan_status", lambda *a, **k: {"complete": True})
    events, t0 = [], time.time()
    for line in rt.stream_answer(_Req(), "s1", "find crm buyers").body_iterator:
        if isinstance(line, str) and line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    return calls, pools, events, time.time() - t0, chats


def test_constant_is_small_and_documented(app_env):
    assert app_env[2].RANGE_SEARCH_MAX_EXTRA_POLLS == 3


def test_a_normal_search_keeps_polling_until_the_timeout(app_env, on, monkeypatch):
    calls, _p, _e, secs, _c = drive(app_env, monkeypatch, query=NOT_LEAD, rows=[HIST_ROW] * 3, cap=1)
    assert len(calls) > 2 and secs >= 8                         # the cap (1) does not apply: it polls on until RESPONSE_TIMEOUT (9 s)
    assert all("time_range" not in kw for kw in calls)


def test_a_range_search_scans_once_then_stops_after_the_capped_polls(app_env, on, monkeypatch):
    calls, _p, _e, secs, _c = drive(app_env, monkeypatch, query=NOT_LEAD, rows=[HIST_ROW] * 3, cap=2, response_timeout=60,
                                    msg_over={"time_range": typed_range()})
    assert len(calls) == 1 + 2                                  # the initial scan + exactly the configured extra polls
    assert secs < 30                                            # it did not wait for the 60 s RESPONSE_TIMEOUT


@pytest.mark.parametrize("cap", [0, 1, 3])
def test_the_initial_scan_is_separate_from_the_cap(app_env, on, monkeypatch, cap):
    calls, *_ = drive(app_env, monkeypatch, query=NOT_LEAD, rows=[HIST_ROW] * 3, cap=cap, response_timeout=60, msg_over={"time_range": typed_range()})
    assert len(calls) == 1 + cap and cap + 1 >= 1               # even with cap 0 the initial scan happens


def test_the_evidence_already_collected_is_used_when_the_cap_is_reached(app_env, on, monkeypatch):
    calls, pools, events, _s, _c = drive(app_env, monkeypatch, query=NOT_LEAD, rows=[HIST_ROW] * 3, cap=1, response_timeout=60,
                                         msg_over={"time_range": typed_range()})
    assert len(pools) == 1 and len(pools[0]) == 3               # the model was given the evidence from the scan
    done = [e for e in events if e.get("done")]
    assert len(done) == 1 and done[0].get("results")            # a normal completed answer, not a timeout / empty one


def test_a_range_search_that_already_has_enough_evidence_does_not_poll(app_env, on, monkeypatch):
    calls, *_ = drive(app_env, monkeypatch, query=NOT_LEAD, rows=[HIST_ROW] * 15, cap=3, response_timeout=60, msg_over={"time_range": typed_range()})
    assert len(calls) == 1


def test_active_intent_is_capped_with_unchanged_boundaries_and_one_live_call(app_env, on, monkeypatch):
    enable(monkeypatch, minutes=1500, wait=10)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", 4, raising=False)
    world = World("s1")
    calls, pools, events, secs, chats = drive(app_env, monkeypatch, query=CRM, rows=[HIST_ROW] * 3, cap=2, response_timeout=60, world=world)
    assert len(calls) == 1 + 2 and secs < 30                    # Active Intent window = explicit range: capped
    windows = {(kw["time_range"]["start"], kw["time_range"]["end"]) for kw in calls}
    assert len(windows) == 1                                    # every scan and poll used the identical boundaries
    tr = timerange.range_from_doc(calls[0]["time_range"])
    assert tr.end - tr.start == datetime.timedelta(minutes=1500)
    assert len(world.live.calls) == 1                           # Live ran exactly once, polling or not
    assert rl._utc(tr.start) in json.dumps(world.live.calls[0]["payload"]) and rl._utc(tr.end) in json.dumps(world.live.calls[0]["payload"])
    assert len([e for e in events if e.get("done")]) == 1
