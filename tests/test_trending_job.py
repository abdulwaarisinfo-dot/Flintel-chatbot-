"""
tests/test_trending_job.py — each 12-hour trending cycle writes exactly ONE
flintel_search_jobs job holding ALL of that cycle's cleaned keywords
(scheduler.py + config.SEARCH_WEBSITE_KEYWORDS). The job's topic_key is
"auto_trending:<UTC time of the cycle, YYYYMMDDTHHMMSSZ>", so every new cycle
creates a NEW job and an earlier cycle's job is never updated or reset.

No real Mongo / Claude / RapidAPI: scheduler.py is imported against stub
`database`, `google` and `logics` modules (sys.modules is restored after each
test), and the calls are fakes that record their arguments. The scheduler's
clock (`_utcnow`) is a variable the tests move forward.

The `real_jobs` tests instead use the REAL logics.enqueue_search_job() (via
test_answer_persistence's `env` fixture), writing to an in-memory mongomock
flintel_search_jobs collection, so stored documents can be compared field by
field and counted (the acceptance check: one new "pending" document per cycle).
"""
import asyncio
import concurrent.futures
import copy
import importlib
import logging
import os
import re
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_answer_persistence import env  # noqa: E402,F401  (fixture: real logics, stub database/google/httpx)

T0 = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)
KEY0 = "auto_trending:20261011T000000Z"              # topic_key of a cycle that runs at T0
KEY12 = "auto_trending:20261011T120000Z"             # ... and of the next one, 12 hours later
TOPIC_KEY_FORMAT = re.compile(r"^auto_trending:\d{8}T\d{6}Z$")


class Calls:
    def __init__(self):
        self.jobs = []
        self.google = []


@pytest.fixture
def sch(monkeypatch):
    """Fresh scheduler module wired to fakes; returns (module, calls, state, db).
    state["now"] is the scheduler's clock."""
    calls = Calls()
    state = {"keywords": None, "job_exc": None, "google_exc": None, "now": T0}

    def fake_generate():
        return state["keywords"]

    def fake_enqueue(topic_key, keywords, targeting_platform):
        if state["job_exc"]:
            raise state["job_exc"]
        calls.jobs.append((topic_key, list(keywords), targeting_platform))

    def fake_google(keywords, collection, tag):
        calls.google.append((keywords, collection, tag))
        if state["google_exc"]:
            raise state["google_exc"]
        return ["stub"] * 3

    db = types.ModuleType("database")
    db.google_posts_collection = object()
    gg = types.ModuleType("google")
    gg.search_google_for_reddit_posts = fake_google
    lg = types.ModuleType("logics")
    lg.generate_trending_keywords_via_web_search = fake_generate
    lg.enqueue_search_job = fake_enqueue

    monkeypatch.setitem(sys.modules, "database", db)
    monkeypatch.setitem(sys.modules, "google", gg)
    monkeypatch.setitem(sys.modules, "logics", lg)
    monkeypatch.delitem(sys.modules, "scheduler", raising=False)
    import config
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", 200)
    mod = importlib.import_module("scheduler")
    monkeypatch.setattr(mod, "_utcnow", lambda: state["now"])
    yield mod, calls, state, db
    sys.modules.pop("scheduler", None)


def cycle(mod, **kwargs):
    # A fresh thread has no running event loop, so asyncio.run() works even when
    # another test module has left one running in the main thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, mod._run_trending_keywords_cycle(**kwargs)).result()


# 1 — one cycle, one job, every keyword
def test_1_five_keywords_one_job_with_all_of_them_and_google(sch, caplog):
    mod, calls, st, db = sch
    st["keywords"] = ["a", "b", "c", "d", "e"]
    caplog.set_level(logging.INFO)
    cycle(mod)
    assert calls.jobs == [(KEY0, ["a", "b", "c", "d", "e"], "reddit")]      # exactly ONE call, full list
    assert len(calls.google) == 1
    kws, coll, tag = calls.google[0]
    assert kws is st["keywords"] and coll is db.google_posts_collection and tag == "auto_trending"
    assert f"job created topic_key={KEY0} keywords=5" in caplog.text
    assert "cycle complete" in caplog.text
    assert "'a'" not in caplog.text            # keyword list itself is never logged
    assert mod._cycle_outcome["job_queued"] is True


def test_1b_topic_key_is_prefix_plus_utc_cycle_time_and_never_the_legacy_key(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["auto_trending", "auto trending", "a"]
    st["now"] = datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=timezone.utc)
    cycle(mod)
    [(topic_key, kws, _p)] = calls.jobs
    assert topic_key == "auto_trending:20260102T030405Z" and TOPIC_KEY_FORMAT.match(topic_key)
    assert topic_key != "auto_trending" and kws == ["auto_trending", "auto trending", "a"]


# 2
def test_2_250_keywords_capped_at_200_in_the_single_job_google_list_untouched(sch):
    mod, calls, st, _ = sch
    st["keywords"] = [f"kw{i}" for i in range(250)]
    original = list(st["keywords"])
    cycle(mod)
    assert len(calls.jobs) == 1 and calls.jobs[0][1] == original[:200]
    assert calls.google[0][0] is st["keywords"] and calls.google[0][0] == original   # Google list untouched


# 3
def test_3_limit_from_config_independent_of_max_keywords(sch, monkeypatch):
    mod, calls, st, _ = sch
    import config
    st["keywords"] = [f"kw{i}" for i in range(120)]
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", 50)
    for n, (mk, cmk) in enumerate(((20, 10), (1, 1), (500, 500), (0, 0))):
        monkeypatch.setattr(config, "MAX_KEYWORDS", mk)
        monkeypatch.setattr(config, "CLAUDE_MAX_KEYWORDS", cmk)
        st["now"] = T0 + timedelta(hours=12 * n)
        cycle(mod)
        assert len(calls.jobs[-1][1]) == 50                       # still 50 per cycle, whatever the user-search limits
    assert len(calls.jobs) == 4                                   # one job per cycle


# 4 — kill-switch
@pytest.mark.parametrize("value", [0, -5, "abc", None])
def test_4_disabled_or_invalid_limit_no_job_google_runs(sch, monkeypatch, caplog, value):
    mod, calls, st, _ = sch
    import config
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", value)
    st["keywords"] = ["a", "b"]
    caplog.set_level(logging.WARNING)
    cycle(mod)                                  # no exception
    assert calls.jobs == [] and len(calls.google) == 1
    assert "search job NOT written — SEARCH_WEBSITE_KEYWORDS is off/invalid" in caplog.text
    assert mod._cycle_outcome["job_queued"] is False


# 5
def test_5_dedupe_strip_case_insensitive_in_the_one_job(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["AI Agents", "ai agents", " ai agents ", "x", "", "   ", None]
    cycle(mod)
    assert calls.jobs == [(KEY0, ["AI Agents", "x"], "reddit")]     # first spelling and order kept
    assert calls.google[0][0] is st["keywords"]                     # Google still gets the raw list


def test_5b_keywords_are_not_dropped_for_normalizing_to_nothing(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["!!!", "Nike Shoes!!", "nike shoes", "a"]     # cleaning is strip + dedupe + cap only
    cycle(mod)
    assert calls.jobs == [(KEY0, ["!!!", "Nike Shoes!!", "nike shoes", "a"], "reddit")]


def test_5c_nothing_usable_after_cleanup_no_job_google_still_runs(sch, caplog):
    mod, calls, st, _ = sch
    st["keywords"] = ["", "   ", None]
    caplog.set_level(logging.WARNING)
    cycle(mod)
    assert calls.jobs == [] and len(calls.google) == 1
    assert "no usable keywords after cleanup" in caplog.text
    assert mod._cycle_outcome["job_queued"] is False


# 6 — enqueue failure
def test_6_enqueue_failure_no_crash_google_still_runs_type_only_logged(sch, caplog):
    mod, calls, st, _ = sch
    st["keywords"] = ["a", "b", "c"]
    st["job_exc"] = RuntimeError("mongo down: keywords=['b']")
    caplog.set_level(logging.INFO)
    cycle(mod)                                  # does not raise
    assert calls.jobs == []                     # the one call failed ...
    assert len(calls.google) == 1 and calls.google[0][0] is st["keywords"]   # ... Google still ran, full list
    assert "queuing the search job failed (RuntimeError)" in caplog.text
    assert "mongo down" not in caplog.text      # message (could echo keywords) not logged
    assert "'b'" not in caplog.text
    assert "job created" not in caplog.text
    assert mod._cycle_outcome["job_queued"] is False and mod._cycle_outcome["status"] == "ok"


def test_6b_enqueue_is_called_exactly_once_even_when_it_fails(sch):
    mod, calls, st, _ = sch
    attempts = []
    st["keywords"] = ["a", "b", "c"]

    def failing(topic_key, keywords, targeting_platform):
        attempts.append(topic_key)
        raise RuntimeError("boom")
    mod.enqueue_search_job = failing
    cycle(mod)
    assert attempts == [KEY0]                                   # no per-keyword retry loop


# 7
def test_7_google_failure_job_still_written(sch, caplog):
    mod, calls, st, _ = sch
    st["keywords"] = ["a", "b"]
    st["google_exc"] = RuntimeError("rapidapi 500")
    caplog.set_level(logging.WARNING)
    cycle(mod)                                  # does not raise
    assert calls.jobs == [(KEY0, ["a", "b"], "reddit")]
    assert "cycle failed, will retry next cycle" in caplog.text


# 8
@pytest.mark.parametrize("value", [None, []])
def test_8_no_keywords_no_job_no_google(sch, caplog, value):
    mod, calls, st, _ = sch
    st["keywords"] = value
    caplog.set_level(logging.WARNING)
    cycle(mod)
    assert calls.jobs == [] and calls.google == []
    assert "no keywords generated this cycle — skipping." in caplog.text


# 9
def _config_value(env_updates):
    env = {k: v for k, v in os.environ.items()
           if k not in ("SEARCH_WEBSITE_KEYWORDS", "MAX_KEYWORDS", "CLAUDE_MAX_KEYWORDS")}
    env.update(env_updates)
    out = subprocess.run(
        [sys.executable, "-c",
         "import config; print(config.SEARCH_WEBSITE_KEYWORDS, config.MAX_KEYWORDS, config.CLAUDE_MAX_KEYWORDS)"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


def test_9_config_default_env_override_and_other_defaults():
    assert _config_value({}) == ["200", "20", "10"]
    assert _config_value({"SEARCH_WEBSITE_KEYWORDS": "75"}) == ["75", "20", "10"]
    assert _config_value({"SEARCH_WEBSITE_KEYWORDS": "abc"}) == ["0", "20", "10"]   # invalid -> off, no crash


def test_platform_constant_interval_and_loop_order_unchanged():
    src = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    assert 'TRENDING_JOB_TARGETING_PLATFORM = "reddit"' in src
    assert "TRENDING_KEYWORDS_INTERVAL_SECONDS = 43200" in src
    loop = src[src.index("async def _trending_keywords_loop"):src.index("def start_trending_keywords_scheduler")]
    assert loop.index("await asyncio.sleep(TRENDING_KEYWORDS_INTERVAL_SECONDS)") < \
        loop.index("await _run_trending_keywords_cycle(write_jobs=False)")   # sleep first, then cycle (no jobs)
    assert "async def _run_claimed_cycle(started_at_raw) -> None:\n    await _run_trending_keywords_cycle()\n" in src


def test_per_keyword_job_helpers_are_gone():
    src = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    assert "_trending_topic_keys" not in src and "_existing_job_topic_keys" not in src
    assert "jobs_collection" not in src                          # no lookup of existing jobs any more


# 10 — two cycles, two jobs
def test_10_two_cycles_two_different_topic_keys_two_calls_old_job_call_untouched(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["a", "b", "c"]
    cycle(mod)
    first = copy.deepcopy(calls.jobs[0])
    st["now"] = T0 + timedelta(hours=12)
    st["keywords"] = ["c", "d", "e", "f"]
    cycle(mod)
    assert [j[0] for j in calls.jobs] == [KEY0, KEY12] and len({j[0] for j in calls.jobs}) == 2
    assert calls.jobs[0] == first == (KEY0, ["a", "b", "c"], "reddit")        # the earlier cycle's call is as it was
    assert calls.jobs[1] == (KEY12, ["c", "d", "e", "f"], "reddit")
    assert len(calls.google) == 2


def test_10b_same_keywords_next_cycle_still_a_new_job(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["a", "b"]
    cycle(mod)
    st["now"] = T0 + timedelta(hours=12)
    cycle(mod)                                                                 # identical keywords
    assert calls.jobs == [(KEY0, ["a", "b"], "reddit"), (KEY12, ["a", "b"], "reddit")]


# 11 — fallback / unclaimed cycles write no job
def test_11_write_jobs_false_no_job_google_runs(sch, caplog):
    mod, calls, st, db = sch
    st["keywords"] = ["a", "b", "c"]
    caplog.set_level(logging.WARNING)
    cycle(mod, write_jobs=False)
    assert calls.jobs == []
    assert len(calls.google) == 1 and calls.google[0][0] is st["keywords"]
    assert "search jobs NOT written — cycle ran without the schedule claim (fallback)" in caplog.text
    assert mod._cycle_outcome["job_queued"] is False and mod._cycle_outcome["status"] == "ok"


# 12-13 — the REAL enqueue_search_job() writing to an in-memory flintel_search_jobs
_T = datetime(2026, 1, 2, 3, 4, 5)        # naive: mongomock returns naive datetimes
LEGACY_JOB = {"topic_key": "auto_trending", "keywords": [f"old{i}" for i in range(200)], "targeting_platform": "reddit",
              "status": "completed", "requested_at": _T, "started_at": _T, "completed_at": _T, "matched_count": 41,
              "error": None, "time_range": None, "worker_note": "legacy"}
PER_KEYWORD_JOB = {"topic_key": "auto_trending:b", "keywords": ["b"], "targeting_platform": "reddit", "status": "running",
                   "requested_at": _T, "started_at": _T, "completed_at": None, "matched_count": 7, "error": None,
                   "time_range": None, "custom_field": {"keep": True}}      # left by the old one-job-per-keyword code


@pytest.fixture
def real_jobs(env, monkeypatch):
    """Scheduler wired to the real logics.enqueue_search_job(); only keyword generation,
    the Google call and the clock are faked."""
    mongomock = pytest.importorskip("mongomock")
    lg, _idx, _chats = env
    jobs = mongomock.MongoClient()["flintel"]["flintel_search_jobs"]
    jobs.insert_many([copy.deepcopy(LEGACY_JOB), copy.deepcopy(PER_KEYWORD_JOB)])
    monkeypatch.setattr(lg, "jobs_collection", jobs)                    # where the real enqueue writes
    db = sys.modules["database"]
    monkeypatch.setattr(db, "google_posts_collection", object(), raising=False)
    state = {"keywords": None, "google": [], "now": T0}
    monkeypatch.setattr(sys.modules["google"], "search_google_for_reddit_posts",
                        lambda kws, coll, tag: state["google"].append(kws) or ["stub"], raising=False)
    monkeypatch.setattr(lg, "generate_trending_keywords_via_web_search", lambda: state["keywords"])
    import config
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", 200)
    monkeypatch.delitem(sys.modules, "scheduler", raising=False)
    mod = importlib.import_module("scheduler")
    monkeypatch.setattr(mod, "_utcnow", lambda: state["now"])
    assert mod.enqueue_search_job is lg.enqueue_search_job
    yield mod, jobs, state
    sys.modules.pop("scheduler", None)


def _docs(jobs):
    return {d["topic_key"]: d for d in jobs.find({})}


# 12 — acceptance: one cycle -> exactly 1 new pending document; 12 hours later -> exactly 1 more
def test_12_real_enqueue_one_new_document_per_cycle_all_keywords_pending(real_jobs):
    mod, jobs, st = real_jobs
    before = _docs(jobs)
    st["keywords"] = [f"kw{i}" for i in range(30)] + ["KW1", " kw2 ", ""]    # 30 distinct after cleanup
    cycle(mod)
    after1 = _docs(jobs)
    new1 = set(after1) - set(before)
    assert new1 == {KEY0}                                                    # exactly ONE new document
    doc = after1[KEY0]
    assert doc["status"] == "pending" and doc["targeting_platform"] == "reddit"
    assert doc["keywords"] == [f"kw{i}" for i in range(30)]                  # all, deduped, in a single list
    assert doc["matched_count"] == 0 and doc["error"] is None
    for key in before:                                                       # legacy + per-keyword jobs: every field untouched
        assert after1[key] == before[key]
    assert mod._cycle_outcome["job_queued"] is True
    # the collector picks the job up and starts working on it
    jobs.update_one({"topic_key": KEY0}, {"$set": {"status": "running", "matched_count": 9}})
    running = _docs(jobs)[KEY0]
    # 12 hours later: exactly one more NEW document; the first one is not reset
    st["now"] = T0 + timedelta(hours=12)
    st["keywords"] = ["x", "y", "z"]
    cycle(mod)
    after2 = _docs(jobs)
    assert set(after2) - set(after1) == {KEY12} and len(after2) == len(before) + 2
    assert after2[KEY0] == running and after2[KEY0]["status"] == "running"   # earlier cycle's job: untouched, not reset
    assert after2[KEY12]["status"] == "pending" and after2[KEY12]["keywords"] == ["x", "y", "z"]
    assert len(st["google"]) == 2 and all(g is not None for g in st["google"])


def test_12b_real_enqueue_no_per_keyword_documents(real_jobs):
    mod, jobs, st = real_jobs
    st["keywords"] = ["a", "b", "c", "auto_trending"]
    cycle(mod)
    topics = set(_docs(jobs))
    assert topics == {"auto_trending", "auto_trending:b", KEY0}              # no "auto_trending:a" / ":c" / ":autotrending"
    assert jobs.count_documents({}) == 3


# 13 — failures
def test_13_real_enqueue_write_failure_no_document_google_runs(real_jobs, monkeypatch):
    mod, jobs, st = real_jobs
    before = _docs(jobs)

    def update_one(*_a, **_k):
        raise RuntimeError("write refused")
    monkeypatch.setattr(jobs, "update_one", update_one)
    st["keywords"] = ["x", "y"]
    cycle(mod)                                                                # does not raise
    assert _docs(jobs) == before                                              # nothing new, nothing changed
    assert mod._cycle_outcome["job_queued"] is False and mod._cycle_outcome["status"] == "ok"
    assert st["google"] == [st["keywords"]]


# 14 — fallback (unclaimed) cycles write no job
class _StopLoop(BaseException):
    """Ends the infinite loop (not an Exception, so the loop cannot swallow it)."""


def _run_fallback_cycles(mod, monkeypatch, n):
    """Drives _trending_keywords_loop() through its fallback branch n times:
    the persisted-schedule step always fails and sleeps return at once."""
    async def schedule_down(first_check):
        raise RuntimeError("schedule unavailable")
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > n:
            raise _StopLoop()
    monkeypatch.setattr(mod, "_persisted_schedule_step", schedule_down)
    monkeypatch.setattr(mod, "_record_fallback_cycle", lambda started: None)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        with pytest.raises(_StopLoop):
            ex.submit(asyncio.run, mod._trending_keywords_loop()).result()


def test_14_fallback_cycles_write_no_document_and_leave_every_job_unchanged(real_jobs, monkeypatch, caplog):
    mod, jobs, st = real_jobs
    before = _docs(jobs)
    st["keywords"] = ["a", "b", "c"]
    caplog.set_level(logging.WARNING)
    _run_fallback_cycles(mod, monkeypatch, 2)
    assert _docs(jobs) == before                                              # no new document, running + legacy untouched
    assert len(st["google"]) == 2 and all(g is st["keywords"] for g in st["google"])
    assert mod._cycle_outcome["job_queued"] is False and mod._cycle_outcome["status"] == "ok"
    assert caplog.text.count("cycle ran without the schedule claim (fallback)") == 2
