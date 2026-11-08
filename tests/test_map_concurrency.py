"""The map step (one model request per batch of posts) used to run strictly one request after another. It now runs with bounded concurrency,
keeps the order of the notes, retries a failed batch once, and reports batches that still failed instead of presenting a partial analysis as complete.
Model calls are simulated here (no network, no key); the speedup measured is the scheduling effect, not a model latency."""
import json
import threading
import time

import pytest

from tests.test_mapreduce_identity import SIMILAR, _final_answer, _map_reply, _pool, _record, QUERY, env, lg  # noqa: F401


def test_results_keep_input_order_even_when_later_calls_finish_first(lg, monkeypatch):
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 4)
    delays = [0.20, 0.15, 0.10, 0.05, 0.0, 0.12]

    def fn(i):
        time.sleep(delays[i])
        return f"note-{i}"
    res, failed = lg._concurrent_calls(fn, [(i,) for i in range(6)], "t")
    assert res == [f"note-{i}" for i in range(6)] and failed == 0


def test_concurrency_is_bounded_and_really_happens(lg, monkeypatch):
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 3)
    live, peak, lock = [0], [0], threading.Lock()

    def fn(i):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return i
    res, failed = lg._concurrent_calls(fn, [(i,) for i in range(12)], "t")
    assert res == list(range(12)) and failed == 0 and 2 <= peak[0] <= 3


def test_concurrency_one_is_the_previous_sequential_behaviour(lg, monkeypatch):
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 1)
    order = []

    def fn(i):
        order.append(i)
        return i
    assert lg._concurrent_calls(fn, [(i,) for i in range(5)], "t") == ([0, 1, 2, 3, 4], 0) and order == [0, 1, 2, 3, 4]


def test_a_failed_call_is_retried_once_and_a_persistent_failure_is_counted(lg, monkeypatch):
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 4)
    seen = {}

    def fn(i):
        seen[i] = seen.get(i, 0) + 1
        if i == 1 and seen[i] == 1:
            raise RuntimeError("429 once")
        if i == 3:
            raise RuntimeError("always")
        return f"ok{i}"
    res, failed = lg._concurrent_calls(fn, [(i,) for i in range(5)], "t")
    assert res == ["ok0", "ok1", "ok2", None, "ok4"] and failed == 1 and seen[1] == 2 and seen[3] == 2


def test_measured_speedup_for_84_batches_the_size_of_a_1000_post_search(lg, monkeypatch):
    """Scheduling effect only: 84 simulated model calls of 20 ms each. Sequential is ~1.7 s; four at a time is ~0.45 s."""
    def fn(i):
        time.sleep(0.02)
        return i
    args = [(i,) for i in range(84)]
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 1)
    t = time.time(); lg._concurrent_calls(fn, args, "t"); seq = time.time() - t
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 4)
    t = time.time(); lg._concurrent_calls(fn, args, "t"); par = time.time() - t
    print(f"sequential {seq:.2f}s, concurrency 4 {par:.2f}s, speedup {seq / par:.1f}x")
    assert par < seq * 0.5


def _run(lg, monkeypatch, n_posts, fail_batch=None, concurrency=4):
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", concurrency)
    seen = {}

    def fake_call(system, user, **kw):
        if system is lg.CLAUDE_MAP_STEP_SYSTEM_PROMPT:
            import re
            nums = [int(n) for n in re.findall(r"\[Post (\d+)\]", user)]
            if fail_batch is not None and fail_batch in nums:
                raise RuntimeError("model unavailable")
            return _map_reply(nums, [_record(n) for n in nums if n in SIMILAR])
        if system is lg.CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT:
            return "- condensed (Post 7)"
        seen["final_user"] = user
        return _final_answer([])
    monkeypatch.setattr(lg, "_call_claude", fake_call)
    return json.loads(lg.analyze_with_claude(QUERY, _pool(n_posts))), seen


def test_a_complete_analysis_carries_no_partial_marker(lg, monkeypatch):
    out, _ = _run(lg, monkeypatch, 26)
    assert "analysis_coverage" not in out


def test_a_batch_that_fails_twice_is_reported_not_hidden(lg, monkeypatch):
    out, seen = _run(lg, monkeypatch, 26, fail_batch=1)          # batch 1 holds posts 1-12
    assert out["analysis_coverage"] == {"batches": 3, "failed_batches": 1, "posts": 26}
    assert "Notes:" in seen["final_user"] or "notes" in seen["final_user"].lower()


def test_every_batch_failing_is_a_no_results_answer_not_a_success(lg, monkeypatch):
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 4)

    def fake_call(system, user, **kw):
        if system is lg.CLAUDE_MAP_STEP_SYSTEM_PROMPT:
            raise RuntimeError("down")
        return json.dumps({"format": "no_results", "message": "could not be processed"})
    monkeypatch.setattr(lg, "_call_claude", fake_call)
    out = json.loads(lg.analyze_with_claude(QUERY, _pool(26)))
    assert out["format"] == "no_results" and "platforms" not in out


# ── review additions: results of healthy batches survive one failing batch; context reaches the workers; consistent counts ──────────────
def test_notes_of_healthy_batches_reach_the_final_answer_when_one_batch_fails(lg, monkeypatch):
    out, seen = _run(lg, monkeypatch, 26, fail_batch=1)                  # batch 1 = posts 1-12 fails twice; batches 2 and 3 are healthy
    final = seen["final_user"]
    assert "(Post 19)" in final                                          # the note of a post in a healthy batch (13-24) is used
    assert "(Post 7)" not in final                                       # post 7 sat in the failed batch: nothing is invented for it
    assert out["analysis_coverage"]["failed_batches"] == 1


def test_a_batch_that_fails_once_is_retried_and_the_answer_is_complete(lg, monkeypatch):
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 4)
    import re
    calls = {"n": 0}

    def fake_call(system, user, **kw):
        if system is lg.CLAUDE_MAP_STEP_SYSTEM_PROMPT:
            nums = [int(n) for n in re.findall(r"\[Post (\d+)\]", user)]
            if 1 in nums:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("429 once")
            return _map_reply(nums, [_record(n) for n in nums if n in SIMILAR])
        return _final_answer([])
    monkeypatch.setattr(lg, "_call_claude", fake_call)
    out = json.loads(lg.analyze_with_claude(QUERY, _pool(26)))
    assert calls["n"] == 2 and "analysis_coverage" not in out            # failed once, retried, succeeded: no partial marker


def test_request_scoped_context_reaches_the_worker_threads(lg, monkeypatch):
    """Usage attribution is a contextvar; the workers must see the caller's value (copy_context), or per-request cost attribution would be lost."""
    import contextvars
    var = contextvars.ContextVar("probe", default=None)
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 4)
    var.set("request-42")
    res, failed = lg._concurrent_calls(lambda i: var.get(), [(i,) for i in range(8)], "t")
    assert res == ["request-42"] * 8 and failed == 0


def test_only_exceptions_are_contained_and_the_number_of_requests_is_bounded(lg, monkeypatch):
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 4)
    n = {"calls": 0}
    lock = threading.Lock()

    def fn(i):
        with lock:
            n["calls"] += 1
        raise ValueError("boom")
    res, failed = lg._concurrent_calls(fn, [(i,) for i in range(6)], "t")
    assert res == [None] * 6 and failed == 6 and n["calls"] == 12      # exactly one retry each, never more


def test_condense_failures_keep_the_coverage_counts_consistent(lg, monkeypatch):
    monkeypatch.setattr(lg, "CLAUDE_MAP_CONCURRENCY", 4)
    monkeypatch.setattr(lg, "CLAUDE_NOTES_PER_CHUNK", 2)                  # 3 map notes -> 2 condense batches
    import re

    def fake_call(system, user, **kw):
        if system is lg.CLAUDE_MAP_STEP_SYSTEM_PROMPT:
            nums = [int(n) for n in re.findall(r"\[Post (\d+)\]", user)]
            return _map_reply(nums, [_record(n) for n in nums if n in SIMILAR])
        if system is lg.CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT:
            raise RuntimeError("condense down")
        return _final_answer([])
    monkeypatch.setattr(lg, "_call_claude", fake_call)
    out = json.loads(lg.analyze_with_claude(QUERY, _pool(26)))
    cov = out["analysis_coverage"]
    assert cov["batches"] == 3 + 2 and cov["failed_batches"] == 2 and cov["failed_batches"] <= cov["batches"]
