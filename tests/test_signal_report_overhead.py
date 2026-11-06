"""With the signal report OFF (the default) answer finalisation must not pay for building its coverage. Found by the controlled concurrency
experiment: build_result_coverage() was evaluated eagerly as an argument and measured 48-79 ms in a contended finalisation path."""
import re
from pathlib import Path

import pytest

from tests.test_strict_intent import _set_cfg
from tests.test_time_ranges import app_env, env, fixed_clock  # noqa: F401,E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def lg(fixed_clock):
    return fixed_clock[0]


def test_flag_off_never_builds_the_coverage(lg, monkeypatch):
    calls = []
    monkeypatch.setattr(lg, "build_result_coverage", lambda *a, **k: calls.append(1) or {"x": 1})
    _set_cfg(monkeypatch, SIGNAL_REPORT_ENABLED=False)
    assert lg.result_coverage_if_enabled({"targeting_platform": "reddit"}, [{"post_url": "u"}] * 20, "c") is None
    assert calls == []


def test_flag_on_builds_exactly_the_same_coverage_as_before(lg, monkeypatch):
    msg = {"targeting_platform": "reddit", "topic_key": "t", "query": "q"}
    posts = [{"post_url": f"https://www.reddit.com/r/a/comments/{i}/t/", "platform": "reddit", "created_utc": 1700000000.0 + i} for i in range(5)]
    _set_cfg(monkeypatch, SIGNAL_REPORT_ENABLED=True)
    assert lg.result_coverage_if_enabled(msg, posts, "c") == lg.build_result_coverage(msg, posts, "c")


def test_the_answer_is_unchanged_with_the_flag_off_whatever_coverage_is_passed(lg, monkeypatch):
    _set_cfg(monkeypatch, SIGNAL_REPORT_ENABLED=False)
    ans = '{"format": "source_list", "headline": "x"}'
    assert lg.enrich_signal_report(ans, [{"post_url": "u"}], query="q", mode="topic", coverage=None) == ans


def test_neither_finalisation_path_evaluates_the_coverage_eagerly_any_more():
    for f in ("routes.py", "index.py"):
        src = (ROOT / f).read_text()
        assert not re.search(r"coverage\s*=\s*build_result_coverage\(", src), f
        assert "coverage=result_coverage_if_enabled(" in src, f
