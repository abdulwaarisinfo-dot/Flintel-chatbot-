"""
tests/test_score_batch_settings.py
==================================
Independent FETCH and SCORING batch sizes, MongoDB and MySQL.

    SIGNAL_EMBEDDING_FETCH_BATCH         (existing, unchanged) Mongo cursor batch + MySQL keyset batch / SQL LIMIT
    SIGNAL_EMBEDDING_MONGO_SCORE_BATCH   (new, default 2000)   rows buffered per Mongo streaming scoring flush
    SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH   (new, default = the effective fetch batch)
                                         rows scored per NumPy operation inside each fetched MySQL batch

Synthetic rows only; the existing in-memory MySQL server and Mongo cursor doubles of tests/test_mysql_read.py are reused.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_mysql_read import (REDDIT_COLS, FakeMySQL, cfg, install, lg, mdoc, r_row, run,  # noqa: E402,F401
                                   vec_with_sim)

MONGO_NAME = "SIGNAL_EMBEDDING_MONGO_SCORE_BATCH"
MYSQL_NAME = "SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH"


# ═══ configuration: defaults, overrides, independence, invalid values ═══════════════════════════════════════════════════
def _config_in_subprocess(env_extra):
    """The settings as config.py resolves them at import, in a clean interpreter with exactly this environment."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SIGNAL_EMBEDDING_")}
    env.update({"SESSION_SECRET_KEY": "x" * 48, "OPENAI_API_KEY": "test-key", "PYTHONDONTWRITEBYTECODE": "1"}, **env_extra)
    code = ("import sys; sys.path.insert(0, %r); import config; "
            "print(config.SIGNAL_EMBEDDING_FETCH_BATCH, config.SIGNAL_EMBEDDING_MONGO_SCORE_BATCH, "
            "config.SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH)" % str(ROOT))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, cwd="/", timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return tuple(int(x) for x in out.stdout.split()[-3:]), out.stderr


def test_defaults_are_5000_fetch_2000_mongo_scoring_and_mysql_scoring_equal_to_the_fetch_batch():
    assert _config_in_subprocess({})[0] == (5000, 2000, 5000)


def test_mongo_scoring_can_be_configured_to_10000_without_touching_the_fetch_batch():
    assert _config_in_subprocess({MONGO_NAME: "10000"})[0] == (5000, 10000, 5000)


def test_mysql_scoring_is_configured_independently_of_the_fetch_batch():
    assert _config_in_subprocess({MYSQL_NAME: "300"})[0] == (5000, 2000, 300)
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "7000", MYSQL_NAME: "300"})[0] == (7000, 2000, 300)


def test_the_mysql_scoring_default_follows_a_changed_fetch_batch_so_todays_behaviour_is_preserved():
    # MySQL has always scored each fetched batch in one operation: unset => scoring batch == fetch batch, whatever it is
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "7000"})[0] == (7000, 2000, 7000)
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "1000"})[0] == (1000, 2000, 1000)


def test_the_fetch_batch_is_unchanged_by_either_scoring_setting():
    assert _config_in_subprocess({MONGO_NAME: "10000", MYSQL_NAME: "10"})[0][0] == 5000
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "123", MONGO_NAME: "9", MYSQL_NAME: "8"})[0] == (123, 9, 8)


@pytest.mark.parametrize("bad", ["0", "-1", "-5000", "abc", "3.5", "1e3", "", "   ", "0x10"])
def test_invalid_scoring_values_fall_back_to_the_defaults(bad):
    (fetch, mongo, mysql), stderr = _config_in_subprocess({MONGO_NAME: bad, MYSQL_NAME: bad, "SIGNAL_EMBEDDING_FETCH_BATCH": "4000"})
    assert (fetch, mongo, mysql) == (4000, 2000, 4000)                           # never 0, negative or a crash
    assert (("not a positive integer" in stderr) is True) or bad.strip() == ""      # a bad value is reported; an empty one is "unset"


def test_the_validator_itself(lg, caplog):
    import config
    f = config._positive_int_env
    assert f("NO_SUCH_VARIABLE_FOR_TEST", 7) == 7
    os.environ["SB_T"] = "12"
    try:
        assert f("SB_T", 7) == 12
        os.environ["SB_T"] = " 12 "
        assert f("SB_T", 7) == 12
        os.environ["SB_T"] = "0"
        with caplog.at_level(logging.WARNING, logger="flintel.config"):
            assert f("SB_T", 7) == 7
        assert "SB_T" in caplog.text and "not a positive integer" in caplog.text
    finally:
        os.environ.pop("SB_T", None)
    assert {MONGO_NAME, MYSQL_NAME} <= set(config.__all__)


def test_the_constants_are_defined_once_and_used_where_documented():
    cfg_src = (ROOT / "config.py").read_text()
    log_src = (ROOT / "logics.py").read_text()
    assert cfg_src.count(f"\n{MONGO_NAME} = ") == 1 and cfg_src.count(f"\n{MYSQL_NAME} = ") == 1
    assert 'SIGNAL_EMBEDDING_FETCH_BATCH = int(os.getenv("SIGNAL_EMBEDDING_FETCH_BATCH", "5000"))' in cfg_src       # untouched
    assert '"batch": max(1, int(SIGNAL_EMBEDDING_MONGO_SCORE_BATCH or 2000))' in log_src
    assert "min(int(SIGNAL_EMBEDDING_FETCH_BATCH or 2000), 2000)" not in log_src                                  # the hard cap is gone
    assert "batch_size=SIGNAL_EMBEDDING_FETCH_BATCH" in log_src and ".batch_size(SIGNAL_EMBEDDING_FETCH_BATCH)" in log_src
    assert "_NP_BATCH = 2000" in log_src                                                                          # legacy path untouched


# ═══ MongoDB streaming scoring ═══════════════════════════════════════════════════════════════════════════════════════
class _Recorder:
    """numpy stand-in for logics: records the size of every batch the Mongo streaming scorer builds (np.array of embeddings)."""
    def __init__(self):
        self.sizes = []

    def __getattr__(self, name):
        return getattr(np, name)

    def array(self, obj, *a, **k):
        if isinstance(obj, list) and obj and isinstance(obj[0], (list, tuple)):
            self.sizes.append(len(obj))
        return np.array(obj, *a, **k)


def _mongo_docs(n):
    # similarities cycle through values clearly above / below the 0.35 threshold; distinct so the order is unambiguous
    sims = [0.95 - 0.004 * i if i % 3 else 0.10 + 0.001 * i for i in range(n)]
    return [mdoc(f"m/{i}", s) for i, s in enumerate(sims)]


def _run_mongo(lg, cfg, monkeypatch, n, fetch, score):
    rec = _Recorder()
    monkeypatch.setattr(lg, "np", rec)
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", fetch)
    monkeypatch.setattr(lg, MONGO_NAME, score)
    out, state, _ = run(lg, monkeypatch, _mongo_docs(n), limit=500)
    return [(m["post_url"]) for m in out], rec.sizes[1:-1]         # first / last are the 1-row query matrices (streaming Q, then the later non-streaming Q)


def test_mongo_scoring_defaults_to_2000_and_ignores_a_larger_fetch_batch(lg, cfg, monkeypatch):
    urls, sizes = _run_mongo(lg, cfg, monkeypatch, 4500, fetch=5000, score=2000)
    assert sizes == [2000, 2000, 500]                                # the final partial buffer (500) is scored


def test_mongo_scoring_can_be_10000_and_a_single_partial_buffer_is_still_scored(lg, cfg, monkeypatch):
    urls, sizes = _run_mongo(lg, cfg, monkeypatch, 4500, fetch=5000, score=10000)
    assert sizes == [4500]


def test_a_smaller_fetch_batch_no_longer_caps_the_mongo_scoring_batch(lg, cfg, monkeypatch):
    urls, sizes = _run_mongo(lg, cfg, monkeypatch, 4500, fetch=1000, score=2000)      # before: min(1000, 2000) = 1000
    assert sizes == [2000, 2000, 500]


def test_mongo_scoring_batch_smaller_than_the_fetch_batch(lg, cfg, monkeypatch):
    urls, sizes = _run_mongo(lg, cfg, monkeypatch, 25, fetch=5000, score=10)
    assert sizes == [10, 10, 5]


def test_mongo_fetch_still_uses_the_fetch_batch_setting(lg, cfg, monkeypatch):
    seen = []

    class Cursor:
        def __init__(self, d): self.d = list(d)
        def sort(self, *a, **k): return self
        def batch_size(self, n): seen.append(n); return self
        def limit(self, n): return self
        def max_time_ms(self, n): return self
        def __iter__(self): return iter(self.d)
    from unittest.mock import MagicMock
    coll = MagicMock()
    coll.find.side_effect = lambda *a, **k: Cursor(_mongo_docs(5))
    monkeypatch.setattr(lg, "signals_collection", coll)
    monkeypatch.setattr(lg, "generate_query_embeddings_batch", lambda items: [[1.0, 0.0, 0.0, 0.0] for _ in items])
    for fetch, score in ((5000, 2000), (1234, 7), (50, 10000)):
        seen.clear()
        monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", fetch)
        monkeypatch.setattr(lg, MONGO_NAME, score)
        lg.get_matched_signals(topic_key="t", keywords=["buy a car"], scan_state={})
        assert seen and set(seen) == {fetch}                          # the cursor batch never follows the scoring setting


def test_mongo_results_are_identical_for_any_scoring_batch_size(lg, cfg, monkeypatch):
    base, _ = _run_mongo(lg, cfg, monkeypatch, 37, fetch=5000, score=2000)
    assert base and len(base) < 37                                    # some rows are below the threshold and filtered out
    for score in (1, 2, 7, 36, 37, 38, 10000):
        assert _run_mongo(lg, cfg, monkeypatch, 37, fetch=5000, score=score)[0] == base


@pytest.mark.parametrize("bad", [0, None, -3])
def test_a_bad_mongo_scoring_value_cannot_cause_an_empty_batch_loop(lg, cfg, monkeypatch, bad):
    base, _ = _run_mongo(lg, cfg, monkeypatch, 20, fetch=5000, score=2000)
    urls, sizes = _run_mongo(lg, cfg, monkeypatch, 20, fetch=5000, score=bad)
    assert urls == base and sizes and all(s >= 1 for s in sizes)


# ═══ MySQL scoring ═══════════════════════════════════════════════════════════════════════════════════════════════════
def _mysql_rows(n):
    sims = [0.93 - 0.01 * i if i % 4 else 0.12 + 0.001 * i for i in range(n)]
    return [r_row(i + 1, f"u/{i}", s, age_days=(1 if i % 5 else 30)) for i, s in enumerate(sims)]


def _run_mysql(lg, cfg, monkeypatch, n, fetch, score, since_days=None, caplog=None):
    server = FakeMySQL({"flintel": (REDDIT_COLS, _mysql_rows(n))})
    install(lg, cfg, monkeypatch, server)
    monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", "flintel")
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", fetch)
    monkeypatch.setattr(lg, MYSQL_NAME, score)
    chunks = []
    real = getattr(lg._np_max_scores, "_real", lg._np_max_scores)         # never wrap an earlier recorder in the same test
    def _rec(D, Q):
        chunks.append(len(D))
        return real(D, Q)
    _rec._real = real
    monkeypatch.setattr(lg, "_np_max_scores", _rec)
    out, state, _ = run(lg, monkeypatch, [], limit=500, **({"since_days": since_days} if since_days else {}))
    limits = [p[-1] for sql, p in server.executed if "information_schema" not in sql]
    sqls = sorted({sql for sql, p in server.executed})
    return [m["post_url"] for m in out], chunks, limits, sqls


def test_mysql_scoring_equal_to_the_fetch_batch_scores_each_fetched_batch_in_one_operation(lg, cfg, monkeypatch):
    urls, chunks, limits, _ = _run_mysql(lg, cfg, monkeypatch, 12, fetch=5, score=5)
    assert chunks == [5, 5, 2]                                         # = today's behaviour: one operation per fetched batch
    assert limits[:3] == [5, 5, 5]


def test_mysql_scoring_batch_smaller_than_the_fetch_batch(lg, cfg, monkeypatch):
    urls, chunks, limits, _ = _run_mysql(lg, cfg, monkeypatch, 12, fetch=5, score=2)
    assert chunks == [2, 2, 1, 2, 2, 1, 2]                             # each fetched batch (5, 5, 2) in order, partial chunks scored
    assert set(limits) == {5}                                          # the SQL LIMIT still follows the fetch batch only


def test_mysql_scoring_batch_larger_than_the_fetch_batch(lg, cfg, monkeypatch):
    urls, chunks, limits, _ = _run_mysql(lg, cfg, monkeypatch, 12, fetch=5, score=10000)
    assert chunks == [5, 5, 2] and set(limits) == {5}                  # a scoring chunk can never span two fetched batches


def test_mysql_results_and_sql_are_identical_for_any_scoring_batch_size(lg, cfg, monkeypatch):
    base_urls, _, base_limits, base_sql = _run_mysql(lg, cfg, monkeypatch, 23, fetch=7, score=7)
    assert base_urls and len(base_urls) < 23
    for score in (1, 2, 3, 6, 7, 8, 100, 10000):
        urls, chunks, limits, sql = _run_mysql(lg, cfg, monkeypatch, 23, fetch=7, score=score)
        assert urls == base_urls                                       # same rows, same order, same threshold filtering
        assert limits == base_limits and sql == base_sql               # SQL text and LIMIT parameters unchanged
        assert sum(chunks) == 23                                       # every fetched row was scored exactly once


def test_mysql_chunking_applies_after_the_time_window_filter(lg, cfg, monkeypatch):
    base = _run_mysql(lg, cfg, monkeypatch, 23, fetch=7, score=7, since_days=10)
    chunked = _run_mysql(lg, cfg, monkeypatch, 23, fetch=7, score=2, since_days=10)
    assert chunked[0] == base[0] and base[0]
    assert all(c <= 2 for c in chunked[1]) and sum(chunked[1]) == sum(base[1]) < 23     # rows outside the window never scored


def test_mysql_fetch_logging_still_reports_the_fetch_batch(lg, cfg, monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _run_mysql(lg, cfg, monkeypatch, 12, fetch=5, score=2)
    line = next(r.getMessage() for r in caplog.records if "streamed-score" in r.getMessage() and "mysql_" in r.getMessage())
    assert " batch=5 " in line and "score_batch=2" in line


@pytest.mark.parametrize("bad", [0, None, -4])
def test_a_bad_mysql_scoring_value_cannot_cause_an_empty_batch_loop(lg, cfg, monkeypatch, bad):
    base = _run_mysql(lg, cfg, monkeypatch, 12, fetch=5, score=5)[0]
    urls, chunks, limits, _ = _run_mysql(lg, cfg, monkeypatch, 12, fetch=5, score=bad)
    assert urls == base and sum(chunks) == 12 and all(c >= 1 for c in chunks)


def test_the_legacy_non_streaming_scorer_keeps_its_own_fixed_batch(lg, cfg, monkeypatch):
    monkeypatch.setattr(lg, MONGO_NAME, 3)
    monkeypatch.setattr(lg, MYSQL_NAME, 3)
    src = (ROOT / "logics.py").read_text()
    legacy = src[src.index("_NP_BATCH = 2000"):src.index("_NP_BATCH = 2000") + 5000]
    assert "SIGNAL_EMBEDDING_MONGO_SCORE_BATCH" not in legacy and "SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH" not in legacy
    assert "len(_batch_emb) >= _NP_BATCH" in legacy


# ═══ final non-empty partial buffer: scored exactly once ═════════════════════════════════════════════════════════════
def test_mongo_final_partial_buffer_is_scored_exactly_once(lg, cfg, monkeypatch):
    urls, sizes = _run_mongo(lg, cfg, monkeypatch, 37, fetch=5000, score=10)         # 37 is not divisible by 10
    assert sizes == [10, 10, 10, 7] and sum(sizes) == 37                            # the 7-row tail is scored, nothing twice
    expected = [d["post_url"] for d in _mongo_docs(37) if "post_url" in d]
    assert len(urls) == len(set(urls))                                             # no row returned twice
    assert set(urls) <= set(expected) and urls                                     # and every returned row is a real input row


def test_mysql_final_partial_chunk_is_scored_exactly_once(lg, cfg, monkeypatch):
    n = 23                                                                          # 23 rows, fetch 7 -> 7,7,7,2 ; score 3 -> not a divisor of either
    base_urls, base_chunks, _, _ = _run_mysql(lg, cfg, monkeypatch, n, fetch=7, score=7)
    urls, chunks, limits, _ = _run_mysql(lg, cfg, monkeypatch, n, fetch=7, score=3)
    assert sum(chunks) == n and chunks[-1] == 2 and chunks.count(3) == 6 and chunks.count(1) == 3
    assert urls == base_urls and len(urls) == len(set(urls))                       # same rows, none duplicated, none lost
    assert set(urls) <= {f"u/{i}" for i in range(n)}
