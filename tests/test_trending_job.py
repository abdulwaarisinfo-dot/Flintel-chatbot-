"""
tests/test_trending_job.py — every trending keyword is written as its OWN
flintel_search_jobs job (scheduler.py + config.SEARCH_WEBSITE_KEYWORDS);
keywords that already have a job are skipped, never updated or reset.

No real Mongo / Claude / RapidAPI: scheduler.py is imported against stub
`database`, `google` and `logics` modules (sys.modules is restored after each
test), and the calls are fakes that record their arguments.

Tests 16-18 instead use the REAL logics.enqueue_search_job() and
normalize_topic_key() (via test_answer_persistence's `env` fixture), writing to
an in-memory mongomock flintel_search_jobs collection, so stored documents can
be compared field by field.
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
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_answer_persistence import env  # noqa: E402,F401  (fixture: real logics, stub database/google/httpx)


class Calls:
    def __init__(self):
        self.jobs = []
        self.google = []
        self.finds = []


def _normalize_topic_key(query):
    """Same rules as logics.normalize_topic_key()."""
    cleaned = re.sub(r"[^a-z0-9\s]", "", query.strip().lower())
    return re.sub(r"\s+", " ", cleaned).strip()


class FakeJobs:
    """flintel_search_jobs: only the lookup scheduler.py makes."""
    def __init__(self, state, calls):
        self.state, self.calls = state, calls

    def find(self, flt, projection):
        self.calls.finds.append((flt, projection))
        if self.state["find_exc"]:
            raise self.state["find_exc"]
        wanted = flt["topic_key"]["$in"]
        return iter([{"topic_key": k} for k in wanted if k in self.state["existing"]])


@pytest.fixture
def sch(monkeypatch):
    """Fresh scheduler module wired to fakes; returns (module, calls, state, db)."""
    calls = Calls()
    state = {"keywords": None, "job_exc": None, "google_exc": None, "find_exc": None,
             "existing": {"auto_trending"}}                      # the legacy combined job exists

    def fake_generate():
        return state["keywords"]

    def fake_enqueue(topic_key, keywords, targeting_platform):
        exc = state["job_exc"]
        if isinstance(exc, dict):
            exc = exc.get(keywords[0])
        if exc:
            raise exc
        calls.jobs.append((topic_key, list(keywords), targeting_platform))
        state["existing"].add(topic_key)                          # the job now exists

    def fake_google(keywords, collection, tag):
        calls.google.append((keywords, collection, tag))
        if state["google_exc"]:
            raise state["google_exc"]
        return ["stub"] * 3

    db = types.ModuleType("database")
    db.google_posts_collection = object()
    db.jobs_collection = FakeJobs(state, calls)
    gg = types.ModuleType("google")
    gg.search_google_for_reddit_posts = fake_google
    lg = types.ModuleType("logics")
    lg.generate_trending_keywords_via_web_search = fake_generate
    lg.enqueue_search_job = fake_enqueue
    lg.normalize_topic_key = _normalize_topic_key

    monkeypatch.setitem(sys.modules, "database", db)
    monkeypatch.setitem(sys.modules, "google", gg)
    monkeypatch.setitem(sys.modules, "logics", lg)
    monkeypatch.delitem(sys.modules, "scheduler", raising=False)
    import config
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", 200)
    mod = importlib.import_module("scheduler")
    yield mod, calls, state, db
    sys.modules.pop("scheduler", None)


def cycle(mod):
    # A fresh thread has no running event loop, so asyncio.run() works even when
    # another test module has left one running in the main thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, mod._run_trending_keywords_cycle()).result()


def job(keyword, topic=None):
    return ("auto_trending:" + (topic or keyword), [keyword], "reddit")


# 1
def test_1_five_keywords_five_jobs_and_google(sch, caplog):
    mod, calls, st, db = sch
    st["keywords"] = ["a", "b", "c", "d", "e"]
    caplog.set_level(logging.INFO)
    cycle(mod)
    assert calls.jobs == [job(k) for k in "abcde"]
    assert len(calls.google) == 1
    kws, coll, tag = calls.google[0]
    assert kws is st["keywords"] and coll is db.google_posts_collection and tag == "auto_trending"
    assert "jobs created=5 skipped_existing=0 failed=0" in caplog.text
    assert "cycle complete" in caplog.text
    assert "'a'" not in caplog.text            # keyword list itself is never logged
    assert mod._cycle_outcome["job_queued"] is True


# 2
def test_2_250_keywords_capped_for_jobs_only(sch):
    mod, calls, st, _ = sch
    st["keywords"] = [f"kw{i}" for i in range(250)]
    original = list(st["keywords"])
    cycle(mod)
    assert calls.jobs == [job(k) for k in original[:200]]
    assert calls.google[0][0] is st["keywords"] and calls.google[0][0] == original   # Google list untouched


# 3
def test_3_limit_from_config_independent_of_max_keywords(sch, monkeypatch):
    mod, calls, st, _ = sch
    import config
    st["keywords"] = [f"kw{i}" for i in range(120)]
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", 50)
    cycle(mod)
    assert len(calls.jobs) == 50
    for mk, cmk in ((1, 1), (500, 500), (0, 0)):
        monkeypatch.setattr(config, "MAX_KEYWORDS", mk)
        monkeypatch.setattr(config, "CLAUDE_MAX_KEYWORDS", cmk)
        cycle(mod)
        assert len(calls.finds[-1][0]["topic_key"]["$in"]) == 50     # still 50 considered per cycle
    assert len(calls.jobs) == 50                                     # ... and none rewritten


# 4
@pytest.mark.parametrize("value", [0, -5, "abc", None])
def test_4_disabled_or_invalid_limit_no_job_google_runs(sch, monkeypatch, caplog, value):
    mod, calls, st, _ = sch
    import config
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", value)
    st["keywords"] = ["a", "b"]
    caplog.set_level(logging.WARNING)
    cycle(mod)                                  # no exception
    assert calls.jobs == [] and calls.finds == [] and len(calls.google) == 1
    assert "search job NOT written — SEARCH_WEBSITE_KEYWORDS is off/invalid" in caplog.text
    assert mod._cycle_outcome["job_queued"] is False


# 5
def test_5_dedupe_strip_case_insensitive(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["AI Agents", "ai agents", " ai agents ", "x", "", "   ", None]
    cycle(mod)
    assert calls.jobs == [job("AI Agents", "ai agents"), job("x")]
    assert calls.google[0][0] is st["keywords"]                     # Google still gets the raw list


# 6
def test_6_one_failed_job_others_still_created(sch, caplog):
    mod, calls, st, _ = sch
    st["keywords"] = ["a", "b", "c"]
    st["job_exc"] = {"b": RuntimeError("mongo down: keywords=['b']")}
    caplog.set_level(logging.INFO)
    cycle(mod)                                  # does not raise
    assert calls.jobs == [job("a"), job("c")]
    assert len(calls.google) == 1
    assert "queuing one search job failed (RuntimeError)" in caplog.text
    assert "jobs created=2 skipped_existing=0 failed=1" in caplog.text
    assert "mongo down" not in caplog.text      # message (could echo keywords) not logged
    assert mod._cycle_outcome["job_queued"] is True


# 7
def test_7_google_failure_jobs_still_written(sch, caplog):
    mod, calls, st, _ = sch
    st["keywords"] = ["a", "b"]
    st["google_exc"] = RuntimeError("rapidapi 500")
    caplog.set_level(logging.WARNING)
    cycle(mod)                                  # does not raise
    assert calls.jobs == [job("a"), job("b")]
    assert "cycle failed, will retry next cycle" in caplog.text


# 8
@pytest.mark.parametrize("value", [None, []])
def test_8_no_keywords_no_job_no_google(sch, caplog, value):
    mod, calls, st, _ = sch
    st["keywords"] = value
    caplog.set_level(logging.WARNING)
    cycle(mod)
    assert calls.jobs == [] and calls.google == [] and calls.finds == []
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


# 10 — one job per keyword
def test_10_three_new_keywords_three_jobs_with_normalized_topic_keys(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["Reddit Monitoring!", "Social   Listening", "B2B leads"]
    cycle(mod)
    assert calls.jobs == [job("Reddit Monitoring!", "reddit monitoring"),
                          job("Social   Listening", "social listening"),
                          job("B2B leads", "b2b leads")]


# 11
def test_11_existing_job_is_skipped_untouched(sch, caplog):
    mod, calls, st, _ = sch
    st["existing"].add("auto_trending:b")
    st["keywords"] = ["a", "b", "c"]
    caplog.set_level(logging.INFO)
    cycle(mod)
    assert calls.jobs == [job("a"), job("c")]                # no call at all for "b"
    assert "jobs created=2 skipped_existing=1 failed=0" in caplog.text


def test_11b_all_existing_nothing_written_job_queued_false(sch, caplog):
    mod, calls, st, _ = sch
    st["keywords"] = ["a", "b"]
    cycle(mod)
    calls.jobs.clear()
    caplog.set_level(logging.INFO)
    cycle(mod)                                               # next cycle, same keywords
    assert calls.jobs == []
    assert "jobs created=0 skipped_existing=2 failed=0" in caplog.text
    assert mod._cycle_outcome["job_queued"] is False


# 12
def test_12_one_lookup_for_all_keywords(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["a", "b", "c"]
    cycle(mod)
    assert calls.finds == [({"topic_key": {"$in": ["auto_trending:a", "auto_trending:b", "auto_trending:c"]}},
                            {"topic_key": 1, "_id": 0})]


# 13
def test_13_same_normalized_topic_key_one_job(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["Nike Shoes!!", "nike shoes", "NIKE   shoes"]   # all -> "nike shoes"
    cycle(mod)
    assert calls.jobs == [job("Nike Shoes!!", "nike shoes")]


def test_13b_keyword_normalizing_to_nothing_is_skipped(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["!!!", "a"]
    cycle(mod)
    assert calls.jobs == [job("a")]


# 14
def test_14_lookup_failure_no_jobs_google_runs(sch, caplog):
    mod, calls, st, _ = sch
    st["keywords"] = ["a", "A", " b ", "", "b"]
    original = list(st["keywords"])
    st["find_exc"] = RuntimeError("mongo down: ['a']")
    caplog.set_level(logging.WARNING)
    cycle(mod)                                  # does not raise
    assert calls.jobs == [] and len(calls.google) == 1
    assert calls.google[0][0] is st["keywords"] and calls.google[0][0] == original   # full, uncleaned list
    assert "looking up existing search jobs failed (RuntimeError)" in caplog.text
    assert "mongo down" not in caplog.text
    assert mod._cycle_outcome["job_queued"] is False and mod._cycle_outcome["status"] == "ok"


# 15
def test_15_legacy_combined_job_never_written(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["auto_trending", "auto trending", "a"]
    cycle(mod)
    topics = [t for t, _k, _p in calls.jobs]
    assert "auto_trending" not in topics
    assert all(t.startswith("auto_trending:") and len(t) > len("auto_trending:") for t in topics)
    assert all("auto_trending" not in f[0]["topic_key"]["$in"] for f in calls.finds)


# 16-17 — the REAL enqueue_search_job() writing to an in-memory flintel_search_jobs
_T = datetime(2026, 1, 2, 3, 4, 5)        # naive: mongomock returns naive datetimes
LEGACY_JOB = {"topic_key": "auto_trending", "keywords": [f"old{i}" for i in range(200)], "targeting_platform": "reddit",
              "status": "completed", "requested_at": _T, "started_at": _T, "completed_at": _T, "matched_count": 41,
              "error": None, "time_range": None, "worker_note": "legacy"}
EXISTING_JOB = {"topic_key": "auto_trending:b", "keywords": ["b"], "targeting_platform": "reddit", "status": "running",
                "requested_at": _T, "started_at": _T, "completed_at": None, "matched_count": 7, "error": None,
                "time_range": None, "custom_field": {"keep": True}}


@pytest.fixture
def real_jobs(env, monkeypatch):
    """Scheduler wired to the real logics.enqueue_search_job()/normalize_topic_key();
    only keyword generation and the Google call are faked."""
    mongomock = pytest.importorskip("mongomock")
    lg, _idx, _chats = env
    jobs = mongomock.MongoClient()["flintel"]["flintel_search_jobs"]
    jobs.insert_many([copy.deepcopy(LEGACY_JOB), copy.deepcopy(EXISTING_JOB)])
    monkeypatch.setattr(lg, "jobs_collection", jobs)                    # where the real enqueue writes
    db = sys.modules["database"]
    monkeypatch.setattr(db, "jobs_collection", jobs, raising=False)      # where the scheduler looks
    monkeypatch.setattr(db, "google_posts_collection", object(), raising=False)
    state = {"keywords": None, "google": []}
    monkeypatch.setattr(sys.modules["google"], "search_google_for_reddit_posts",
                        lambda kws, coll, tag: state["google"].append(kws) or ["stub"], raising=False)
    monkeypatch.setattr(lg, "generate_trending_keywords_via_web_search", lambda: state["keywords"])
    import config
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", 200)
    monkeypatch.delitem(sys.modules, "scheduler", raising=False)
    mod = importlib.import_module("scheduler")
    assert mod.enqueue_search_job is lg.enqueue_search_job and mod.normalize_topic_key is lg.normalize_topic_key
    yield mod, jobs, state
    sys.modules.pop("scheduler", None)


def _docs(jobs):
    return {d["topic_key"]: d for d in jobs.find({})}


# 16
def test_16_real_enqueue_existing_and_legacy_jobs_keep_every_field(real_jobs):
    mod, jobs, st = real_jobs
    before = _docs(jobs)
    st["keywords"] = ["a", "b", " B ", "auto_trending", "", "C!!", "c"]   # "C!!" and "c" -> one topic_key
    cycle(mod)
    after = _docs(jobs)
    assert after["auto_trending"] == before["auto_trending"]             # legacy: every field and _id
    assert after["auto_trending:b"] == before["auto_trending:b"]         # existing: still "running", no reset
    assert sorted(after) == ["auto_trending", "auto_trending:a", "auto_trending:autotrending",
                             "auto_trending:b", "auto_trending:c"]
    new = after["auto_trending:a"]
    assert {k: new[k] for k in ("keywords", "targeting_platform", "status", "matched_count", "error")} == \
        {"keywords": ["a"], "targeting_platform": "reddit", "status": "pending", "matched_count": 0, "error": None}
    assert after["auto_trending:c"]["keywords"] == ["C!!"]                # first spelling kept
    assert mod._cycle_outcome["job_queued"] is True
    cycle(mod)                                                            # same keywords again
    assert _docs(jobs) == after                                           # nothing rewritten, no requested_at bump
    assert mod._cycle_outcome["job_queued"] is False
    assert len(st["google"]) == 2 and all(g is st["keywords"] for g in st["google"])


# 17
@pytest.mark.parametrize("failing,queued", [({"auto_trending:x"}, True), ({"auto_trending:x", "auto_trending:y"}, False)])
def test_17_real_enqueue_write_failures_and_job_queued(real_jobs, monkeypatch, failing, queued):
    mod, jobs, st = real_jobs
    before = _docs(jobs)
    real_update = jobs.update_one

    def update_one(flt, upd, upsert=False):
        if flt["topic_key"] in failing:
            raise RuntimeError("write refused")
        return real_update(flt, upd, upsert=upsert)
    monkeypatch.setattr(jobs, "update_one", update_one)
    st["keywords"] = ["x", "y", "b"]
    cycle(mod)
    after = _docs(jobs)
    assert mod._cycle_outcome["job_queued"] is queued                     # True only if a new job was written
    assert set(after) - set(before) == {"auto_trending:x", "auto_trending:y"} - failing
    assert after["auto_trending"] == before["auto_trending"] and after["auto_trending:b"] == before["auto_trending:b"]
    assert mod._cycle_outcome["status"] == "ok" and st["google"] == [st["keywords"]]


# 18 — fallback (unclaimed) cycles write no jobs, even when their lookup misses
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


def test_18_fallback_cycles_leave_running_job_unchanged(real_jobs, monkeypatch, caplog):
    mod, jobs, st = real_jobs
    before = _docs(jobs)
    assert before["auto_trending:b"]["status"] == "running"
    st["keywords"] = ["a", "b", "c"]
    # each overlapping cycle's lookup misses "b" — writing would reset the running job
    monkeypatch.setattr(mod, "_existing_job_topic_keys", lambda keys: set())
    caplog.set_level(logging.WARNING)
    _run_fallback_cycles(mod, monkeypatch, 2)
    assert _docs(jobs) == before                                          # running + legacy: every field; nothing new
    assert len(st["google"]) == 2 and all(g is st["keywords"] for g in st["google"])
    assert mod._cycle_outcome["job_queued"] is False and mod._cycle_outcome["status"] == "ok"
    assert caplog.text.count("cycle ran without the schedule claim (fallback)") == 2
