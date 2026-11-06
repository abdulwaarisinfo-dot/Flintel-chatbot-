"""TRENDING_KEYWORDS_ENABLED: the trending-keywords scheduler (a paid LLM web-search call + a Google-discovery call + a search job) is controlled.
Reuses the deterministic fakes of test_trending_schedule_state.py: no real Mongo, LLM or RapidAPI."""
import asyncio

import pytest

import config
from tests.test_trending_schedule_state import env, _run  # noqa: F401  (fixture + helper)


@pytest.mark.parametrize("raw,deploy,expected", [
    (None, "", True), ("", "", True), ("  ", "", True),                       # unset: the pre-existing behaviour (production unchanged)
    (None, "production", True), (None, "staging", False), ("", "staging", False),   # unset in staging: DISABLED by default
    ("true", "staging", True), ("1", "staging", True), ("on", "staging", True),     # an explicit enable always wins
    ("false", "", False), ("0", "", False), ("off", "production", False), ("NO", "", False),
    ("maybe", "", True), ("maybe", "staging", False),                          # junk is ignored and the default applies (fail toward the default)
])
def test_the_flag_parser(raw, deploy, expected):
    assert config._parse_trending_enabled(raw, deploy) is expected


def test_the_module_default_is_enabled_outside_staging_so_production_is_unchanged(monkeypatch):
    assert config.FLINTEL_ENV != "staging"            # the test environment is not staging
    assert config.TRENDING_KEYWORDS_ENABLED is True


def _start_in_loop(e, enabled):
    e.mod.config.TRENDING_KEYWORDS_ENABLED = enabled
    marker = {"loop_called": 0, "created": 0}

    async def fake_loop():
        marker["loop_called"] += 1

    e.mod._trending_keywords_loop = fake_loop

    async def go():
        before = len(asyncio.all_tasks())
        ret = e.mod.start_trending_keywords_scheduler()
        created = len(asyncio.all_tasks()) - before
        return ret, created
    return marker, go


def test_disabled_starts_nothing_and_writes_and_calls_nothing(env, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    marker, go = _start_in_loop(env, False)
    ret, created = _run(go())
    assert ret is None and created == 0 and marker["loop_called"] == 0
    assert env.cycles == [] and env.enqueued == [] and env.google == []              # no LLM call, no job, no Google call
    assert env.coll.docs == {} and env.sleeps == []                                   # no schedule document, no recurring timer
    assert "DISABLED" in caplog.text


def test_enabled_starts_the_existing_loop_exactly_once(env):
    marker, go = _start_in_loop(env, True)
    ret, created = _run(go())
    assert created == 1                                  # exactly one background task is created when enabled


def test_enabled_preserves_the_full_existing_cycle(env):
    """With the flag on, the unchanged loop still runs a first cycle immediately on a fresh database and records the schedule."""
    env.mod.config.TRENDING_KEYWORDS_ENABLED = True
    from tests.test_trending_schedule_state import run_loop, state
    run_loop(env, 1)
    assert len(env.cycles) == 1 and len(env.google) == 1 and len(env.enqueued) == 1 and state(env) is not None


def test_a_non_true_value_never_starts_the_scheduler(env):
    for bad in (None, "yes", 1, "true"):
        marker, go = _start_in_loop(env, bad)
        _, created = _run(go())
        assert created == 0                    # only the literal boolean True enables (a stray string can never start paid work)


def test_the_startup_hook_still_calls_the_guarded_function():
    import re
    src = open("index.py").read()
    assert re.search(r"async def _start_trending_keywords_scheduler\(\):\s*\n\s*trending_keywords_scheduler\.start_trending_keywords_scheduler\(\)", src)


def test_no_other_startup_task_makes_a_paid_call_or_a_write_by_default():
    """Audit pin: the only startup hooks are the trending scheduler and the turn finisher; the finisher is dry-run (records only, no worker)
    unless TURN_FINISH_ENABLED=true, and keepalive is not wired into startup at all."""
    import re
    idx = open("index.py").read()
    assert len(re.findall(r'@app\.on_event\("startup"\)', idx)) == 2
    assert "start_keepalive" not in idx and "keepalive" not in open("routes.py").read().lower().replace("keep-alive", "")
    assert config.TURN_FINISH_ENABLED is False
