"""
tests/test_trending_job.py — trending keywords also written as a flintel_search_jobs
job (scheduler.py + config.SEARCH_WEBSITE_KEYWORDS).

No real Mongo / Claude / RapidAPI: scheduler.py is imported against stub
`database`, `google` and `logics` modules (sys.modules is restored after each
test), and the two calls are fakes that record their arguments.
"""
import asyncio
import importlib
import logging
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Calls:
    def __init__(self):
        self.jobs = []
        self.google = []


@pytest.fixture
def sch(monkeypatch):
    """Fresh scheduler module wired to fakes; returns (module, calls, set_keywords)."""
    calls = Calls()
    state = {"keywords": None, "job_exc": None, "google_exc": None}

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
    yield mod, calls, state, db
    sys.modules.pop("scheduler", None)


def cycle(mod):
    return asyncio.run(mod._run_trending_keywords_cycle())


# 1
def test_1_five_keywords_job_and_google(sch, caplog):
    mod, calls, st, db = sch
    st["keywords"] = ["a", "b", "c", "d", "e"]
    caplog.set_level(logging.INFO)
    cycle(mod)
    assert calls.jobs == [("auto_trending", ["a", "b", "c", "d", "e"], "reddit")]
    assert len(calls.google) == 1
    kws, coll, tag = calls.google[0]
    assert kws is st["keywords"] and coll is db.google_posts_collection and tag == "auto_trending"
    assert "job queued | topic_key=auto_trending | keywords=5" in caplog.text
    assert "cycle complete" in caplog.text
    assert "'a'" not in caplog.text            # keyword list itself is never logged


# 2
def test_2_250_keywords_capped_for_job_only(sch):
    mod, calls, st, _ = sch
    st["keywords"] = [f"kw{i}" for i in range(250)]
    original = list(st["keywords"])
    cycle(mod)
    assert len(calls.jobs[0][1]) == 200 and calls.jobs[0][1] == original[:200]
    assert calls.google[0][0] is st["keywords"] and calls.google[0][0] == original   # Google list untouched


# 3
def test_3_limit_from_config_independent_of_max_keywords(sch, monkeypatch):
    mod, calls, st, _ = sch
    import config
    st["keywords"] = [f"kw{i}" for i in range(120)]
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", 50)
    cycle(mod)
    assert len(calls.jobs[-1][1]) == 50
    for mk, cmk in ((1, 1), (500, 500), (0, 0)):
        monkeypatch.setattr(config, "MAX_KEYWORDS", mk)
        monkeypatch.setattr(config, "CLAUDE_MAX_KEYWORDS", cmk)
        cycle(mod)
        assert len(calls.jobs[-1][1]) == 50


# 4
@pytest.mark.parametrize("value", [0, -5, "abc", None])
def test_4_disabled_or_invalid_limit_no_job_google_runs(sch, monkeypatch, value):
    mod, calls, st, _ = sch
    import config
    monkeypatch.setattr(config, "SEARCH_WEBSITE_KEYWORDS", value)
    st["keywords"] = ["a", "b"]
    cycle(mod)                                  # no exception
    assert calls.jobs == [] and len(calls.google) == 1


# 5
def test_5_dedupe_strip_case_insensitive(sch):
    mod, calls, st, _ = sch
    st["keywords"] = ["AI Agents", "ai agents", " ai agents ", "x", "", "   ", None]
    cycle(mod)
    assert calls.jobs[0][1] == ["AI Agents", "x"]
    assert calls.google[0][0] is st["keywords"]                     # Google still gets the raw list


# 6
def test_6_job_failure_google_still_runs(sch, caplog):
    mod, calls, st, _ = sch
    st["keywords"] = ["a"]
    st["job_exc"] = RuntimeError("mongo down: keywords=['a']")
    caplog.set_level(logging.WARNING)
    cycle(mod)                                  # does not raise
    assert len(calls.google) == 1
    assert "queuing the search job failed (RuntimeError)" in caplog.text
    assert "mongo down" not in caplog.text      # message (could echo keywords) not logged


# 7
def test_7_google_failure_job_still_written(sch, caplog):
    mod, calls, st, _ = sch
    st["keywords"] = ["a", "b"]
    st["google_exc"] = RuntimeError("rapidapi 500")
    caplog.set_level(logging.WARNING)
    cycle(mod)                                  # does not raise
    assert calls.jobs == [("auto_trending", ["a", "b"], "reddit")]
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
        loop.index("await _run_trending_keywords_cycle()")          # sleep first, then cycle
