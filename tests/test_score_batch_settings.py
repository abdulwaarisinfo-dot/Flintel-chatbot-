"""
tests/test_score_batch_settings.py
==================================
Independent FETCH and SCORING batch sizes, MongoDB and MySQL.

    SIGNAL_EMBEDDING_FETCH_BATCH         (existing, unchanged) Mongo cursor batch + MySQL keyset batch / SQL LIMIT
    SIGNAL_EMBEDDING_MONGO_SCORE_BATCH   (new) rows buffered per Mongo streaming scoring flush. Explicit value: used as given,
                                         independent of the fetch batch. Unset/invalid: the legacy min(fetch batch, 2000),
                                         resolved at scan time (so 2000 at the default fetch batch of 5000).
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
            "print(repr((config.SIGNAL_EMBEDDING_FETCH_BATCH, config.SIGNAL_EMBEDDING_MONGO_SCORE_BATCH, "
            "config.SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH)))" % str(ROOT))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, cwd="/", timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return eval(out.stdout.strip().splitlines()[-1], {"__builtins__": {}}), out.stderr      # (fetch, mongo explicit-or-None, mysql)


def test_defaults_are_5000_fetch_no_explicit_mongo_value_and_mysql_scoring_equal_to_the_fetch_batch():
    assert _config_in_subprocess({})[0] == (5000, None, 5000)          # None = Mongo scoring uses the legacy min(fetch, 2000) at scan time


def test_mongo_scoring_can_be_configured_to_10000_without_touching_the_fetch_batch():
    assert _config_in_subprocess({MONGO_NAME: "10000"})[0] == (5000, 10000, 5000)


def test_mysql_scoring_is_configured_independently_of_the_fetch_batch():
    assert _config_in_subprocess({MYSQL_NAME: "800"})[0] == (5000, None, 800)
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "7000", MYSQL_NAME: "800"})[0] == (7000, None, 800)


def test_the_mysql_scoring_default_follows_a_changed_fetch_batch_so_todays_behaviour_is_preserved():
    # MySQL has always scored each fetched batch in one operation: unset => scoring batch == fetch batch, whatever it is
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "7000"})[0] == (7000, None, 7000)
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "1000"})[0] == (1000, None, 1000)


def test_the_fetch_batch_is_unchanged_by_either_scoring_setting():
    assert _config_in_subprocess({MONGO_NAME: "10000", MYSQL_NAME: "700"})[0][0] == 5000
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "123", MONGO_NAME: "9", MYSQL_NAME: "800"})[0] == (123, 9, 800)


@pytest.mark.parametrize("bad", ["0", "-1", "-5000", "abc", "3.5", "1e3", "", "   ", "0x10"])
def test_invalid_scoring_values_fall_back_to_the_defaults(bad):
    (fetch, mongo, mysql), stderr = _config_in_subprocess({MONGO_NAME: bad, MYSQL_NAME: bad, "SIGNAL_EMBEDDING_FETCH_BATCH": "4000"})
    assert (fetch, mongo, mysql) == (4000, None, 4000)                           # never 0, negative or a crash; Mongo -> legacy default
    assert (("not a positive integer" in stderr) is True) or bad.strip() == ""      # a bad value is reported; an empty one is "unset"


# ── MySQL scoring minimum (500) ──
@pytest.mark.parametrize("value", ["1", "2", "100", "499"])
def test_mysql_scoring_below_the_minimum_is_raised_to_500_with_a_warning(value):
    (fetch, mongo, mysql), stderr = _config_in_subprocess({MYSQL_NAME: value})
    assert (fetch, mongo, mysql) == (5000, None, 500)
    assert "below the minimum 500" in stderr and MYSQL_NAME in stderr


def test_mysql_scoring_exactly_500_and_above_500_are_used_as_given_without_a_warning():
    for value in ("500", "501", "1999", "5000", "12000"):
        got, stderr = _config_in_subprocess({MYSQL_NAME: value})
        assert got == (5000, None, int(value)) and "minimum" not in stderr


@pytest.mark.parametrize("bad", ["0", "-1", "-500", "abc", "500.5", "5e2", "0x1F4"])
def test_mysql_scoring_invalid_values_use_the_default_not_the_minimum_clamp(bad):
    got, stderr = _config_in_subprocess({MYSQL_NAME: bad, "SIGNAL_EMBEDDING_FETCH_BATCH": "3000"})
    assert got == (3000, None, 3000) and "not a positive integer" in stderr


def test_mysql_scoring_unset_follows_the_fetch_batch_and_never_drops_below_500():
    assert _config_in_subprocess({})[0] == (5000, None, 5000)
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "1000"})[0] == (1000, None, 1000)
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "100"})[0] == (100, None, 500)   # a 100-row fetched batch is one operation anyway
    assert _config_in_subprocess({"SIGNAL_EMBEDDING_FETCH_BATCH": "100", MYSQL_NAME: "50"})[0] == (100, None, 500)


def test_the_minimum_applies_to_mysql_only_mongo_scoring_keeps_its_own_validation():
    assert _config_in_subprocess({MONGO_NAME: "10"})[0] == (5000, 10, 5000)
    assert _config_in_subprocess({MONGO_NAME: "1"})[0] == (5000, 1, 5000)


def test_the_validator_itself(lg, caplog):
    import config
    f = config._positive_int_env
    assert f("NO_SUCH_VARIABLE_FOR_TEST", 7) == 7
    assert f("NO_SUCH_VARIABLE_FOR_TEST", None) is None             # "no explicit value" (the Mongo scoring variable)
    assert f("NO_SUCH_VARIABLE_FOR_TEST", 7, 500) == 500          # an unset default is also held at the minimum
    os.environ["SB_T"] = "12"
    try:
        assert f("SB_T", 7) == 12
        os.environ["SB_T"] = " 12 "
        assert f("SB_T", 7) == 12
        os.environ["SB_T"] = "0"
        with caplog.at_level(logging.WARNING, logger="flintel.config"):
            assert f("SB_T", 7) == 7
        assert "SB_T" in caplog.text and "not a positive integer" in caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="flintel.config"):
            assert f("SB_T", None) is None                           # invalid + no default: warn, then "no explicit value"
        assert "SB_T" in caplog.text and "legacy min(SIGNAL_EMBEDDING_FETCH_BATCH, 2000)" in caplog.text
    finally:
        os.environ.pop("SB_T", None)
    assert {MONGO_NAME, MYSQL_NAME, MYSQL_NAME + "_MIN"} <= set(config.__all__) and config.SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH_MIN == 500


def test_the_constants_are_defined_once_and_used_where_documented():
    cfg_src = (ROOT / "config.py").read_text()
    log_src = (ROOT / "logics.py").read_text()
    assert cfg_src.count(f"\n{MONGO_NAME} = ") == 1 and cfg_src.count(f"\n{MYSQL_NAME} = ") == 1
    assert 'SIGNAL_EMBEDDING_FETCH_BATCH = int(os.getenv("SIGNAL_EMBEDDING_FETCH_BATCH", "5000"))' in cfg_src       # untouched
    assert '"batch": _mongo_score_batch(),' in log_src
    assert "    return max(1, min(int(SIGNAL_EMBEDDING_FETCH_BATCH or 2000), 2000))" in log_src                          # the legacy default, verbatim
    assert log_src.count("min(int(SIGNAL_EMBEDDING_FETCH_BATCH or 2000), 2000)") == 1                              # ...and only in the resolver
    assert 'SIGNAL_EMBEDDING_MONGO_SCORE_BATCH = _positive_int_env("SIGNAL_EMBEDDING_MONGO_SCORE_BATCH", None)' in cfg_src
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


def test_mongo_scoring_unset_at_the_default_fetch_batch_is_2000_and_the_final_partial_buffer_is_scored(lg, cfg, monkeypatch):
    urls, sizes = _run_mongo(lg, cfg, monkeypatch, 4500, fetch=5000, score=None)       # None = no explicit value
    assert sizes == [2000, 2000, 500]
    assert _run_mongo(lg, cfg, monkeypatch, 4500, fetch=5000, score=2000)[1] == sizes   # explicit 2000 behaves the same at fetch 5000


def test_mongo_scoring_can_be_10000_and_a_single_partial_buffer_is_still_scored(lg, cfg, monkeypatch):
    urls, sizes = _run_mongo(lg, cfg, monkeypatch, 4500, fetch=5000, score=10000)
    assert sizes == [4500]


def test_an_explicit_mongo_scoring_batch_is_not_capped_by_a_smaller_fetch_batch(lg, cfg, monkeypatch):
    urls, sizes = _run_mongo(lg, cfg, monkeypatch, 4500, fetch=1000, score=2000)      # explicit: independent of the fetch batch
    assert sizes == [2000, 2000, 500]


@pytest.mark.parametrize("fetch,expected", [(1000, [1000, 1000, 1000, 1000, 500]), (1999, [1999, 1999, 502]),
                                            (2000, [2000, 2000, 500]), (2001, [2000, 2000, 500]), (5000, [2000, 2000, 500])])
def test_unset_mongo_scoring_batch_keeps_the_legacy_min_of_fetch_and_2000_through_the_real_scorer(lg, cfg, monkeypatch, fetch, expected):
    urls, sizes = _run_mongo(lg, cfg, monkeypatch, 4500, fetch=fetch, score=None)      # fetch below / just below / at / just above / above 2000
    assert sizes == expected and sum(sizes) == 4500


def test_unset_mongo_scoring_batch_follows_a_fetch_batch_changed_at_runtime(lg, cfg, monkeypatch):
    # the legacy code read SIGNAL_EMBEDDING_FETCH_BATCH on every scan; so does the unset default
    sizes = [_run_mongo(lg, cfg, monkeypatch, 3000, fetch=f, score=None)[1] for f in (700, 1500, 5000, 700)]
    assert sizes == [[700, 700, 700, 700, 200], [1500, 1500], [2000, 1000], [700, 700, 700, 700, 200]]


@pytest.mark.parametrize("fetch,expected", [(1, 1), (500, 500), (1999, 1999), (2000, 2000), (2001, 2000), (5000, 2000), (10000, 2000),
                                            (0, 2000), (None, 2000), (-5, 1)])
def test_the_unset_default_is_exactly_the_legacy_expression(lg, monkeypatch, fetch, expected):
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", fetch)
    for unset in (None, 0):
        monkeypatch.setattr(lg, MONGO_NAME, unset)
        assert lg._mongo_score_batch() == expected == max(1, min(int(fetch or 2000), 2000))


@pytest.mark.parametrize("fetch", [1000, 2000, 5000])
@pytest.mark.parametrize("explicit", [1, 500, 1000, 2000, 10000])
def test_an_explicit_value_always_wins_whatever_the_fetch_batch(lg, monkeypatch, fetch, explicit):
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", fetch)
    monkeypatch.setattr(lg, MONGO_NAME, explicit)
    assert lg._mongo_score_batch() == explicit and lg.SIGNAL_EMBEDDING_FETCH_BATCH == fetch


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "3.5", "1e3"])
@pytest.mark.parametrize("fetch", ["1000", "2000", "5000"])
def test_an_invalid_explicit_mongo_value_warns_and_resolves_to_the_legacy_default(lg, monkeypatch, bad, fetch):
    (f, mongo, mysql), stderr = _config_in_subprocess({MONGO_NAME: bad, "SIGNAL_EMBEDDING_FETCH_BATCH": fetch})
    assert mongo is None and f == int(fetch) and "not a positive integer" in stderr
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", f)
    monkeypatch.setattr(lg, MONGO_NAME, mongo)
    assert lg._mongo_score_batch() == min(int(fetch), 2000)


def test_mongo_results_are_identical_for_the_unset_default_at_any_fetch_batch_and_for_explicit_2000(lg, cfg, monkeypatch):
    base = _run_mongo(lg, cfg, monkeypatch, 4500, fetch=5000, score=2000)[0]
    assert base and len(base) < 4500
    for fetch in (500, 1000, 1999, 2000, 2001, 5000):
        assert _run_mongo(lg, cfg, monkeypatch, 4500, fetch=fetch, score=None)[0] == base       # exact, ordered
        assert _run_mongo(lg, cfg, monkeypatch, 4500, fetch=fetch, score=2000)[0] == base


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
    for fetch, score in ((5000, 2000), (1234, 7), (50, 10000), (1000, None), (3000, None)):      # None = unset (legacy default)
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


class _Tagged(np.ndarray):
    """A fetched-batch matrix that remembers which row (post_url) each of its rows is, through slicing and fancy indexing."""
    def __array_finalize__(self, obj):
        self.tags = getattr(obj, "tags", None)

    def __getitem__(self, k):
        r = super().__getitem__(k)
        if isinstance(r, _Tagged) and getattr(self, "tags", None) is not None:
            r.tags = self.tags[k]
        return r


def _run_mysql(lg, cfg, monkeypatch, n, fetch, score, since_days=None, caplog=None, trace=None):
    """trace (optional dict): filled with "fetched" (post_urls of every fetched batch, in order) and "scored" (post_urls of
    every chunk handed to the scorer, in order), so a test can prove which rows were scored, how often and in what order."""
    server = FakeMySQL({"flintel": (REDDIT_COLS, _mysql_rows(n))})
    install(lg, cfg, monkeypatch, server)
    monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", "flintel")
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", fetch)
    monkeypatch.setattr(lg, MYSQL_NAME, score)
    chunks = []
    real = getattr(lg._np_max_scores, "_real", lg._np_max_scores)         # never wrap an earlier recorder in the same test
    if trace is not None:
        trace["fetched"], trace["scored"] = [], []
        import mysql_signals
        real_iter = getattr(mysql_signals.iter_signal_batches, "_real", mysql_signals.iter_signal_batches)

        def _tagged_iter(*a, **k):
            gen = real_iter(*a, **k)
            try:
                for docs, M in gen:
                    T = np.asarray(M).view(_Tagged)
                    T.tags = np.array([d["post_url"] for d in docs], dtype=object)
                    trace["fetched"].append(list(T.tags))
                    yield docs, T
            finally:
                gen.close()
        _tagged_iter._real = real_iter
        monkeypatch.setattr(mysql_signals, "iter_signal_batches", _tagged_iter)

    def _rec(D, Q):
        chunks.append(len(D))
        if trace is not None:
            trace["scored"].append(list(D.tags))
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
    urls, chunks, limits, _ = _run_mysql(lg, cfg, monkeypatch, 2600, fetch=1200, score=500)
    assert chunks == [500, 500, 200, 500, 500, 200, 200]               # each fetched batch (1200, 1200, 200) in order, partial chunks scored
    assert set(limits) == {1200}                                        # the SQL LIMIT still follows the fetch batch only


def test_mysql_scoring_batch_larger_than_the_fetch_batch(lg, cfg, monkeypatch):
    urls, chunks, limits, _ = _run_mysql(lg, cfg, monkeypatch, 12, fetch=5, score=10000)
    assert chunks == [5, 5, 2] and set(limits) == {5}                  # a scoring chunk can never span two fetched batches


def test_mysql_results_and_sql_are_identical_for_any_scoring_batch_size(lg, cfg, monkeypatch):
    n, fetch = 2600, 1200                                              # fetched batches: 1200, 1200, 200 (not divisible by any score size below)
    base_trace = {}
    base_urls, base_chunks, base_limits, base_sql = _run_mysql(lg, cfg, monkeypatch, n, fetch=fetch, score=fetch, trace=base_trace)
    assert base_urls and len(base_urls) < n
    base_rows = [u for b in base_trace["scored"] for u in b]
    assert base_chunks == [1200, 1200, 200] and len(base_rows) == n and len(set(base_rows)) == n   # baseline: each row once, one op per batch
    for score in (1, 499, 500, 501, 700, 1199, 1200, 10000):          # sizes below 500 are held at 500 by the minimum
        trace = {}
        urls, chunks, limits, sql = _run_mysql(lg, cfg, monkeypatch, n, fetch=fetch, score=score, trace=trace)
        eff = min(fetch, max(500, score))                              # effective scoring batch (a chunk never spans two fetched batches)
        # results, SQL text and fetch LIMIT unchanged
        assert urls == base_urls
        assert limits == base_limits and set(limits) == {fetch} and sql == base_sql
        # no chunk exceeds the effective size; within every fetched batch all chunks but the last are full and the last holds the remainder
        assert all(1 <= c <= eff for c in chunks)
        pos = 0
        for batch in trace["fetched"]:
            sizes = chunks[pos:pos + -(-len(batch) // eff)]
            pos += len(sizes)
            assert sizes[:-1] == [eff] * (len(sizes) - 1)             # every non-final chunk is full
            assert sizes[-1] == len(batch) - eff * (len(sizes) - 1)    # the final chunk is exactly the remaining rows
            assert sum(sizes) == len(batch)
        assert pos == len(chunks)
        # exact row identities: the scorer saw every fetched row once, in fetch order, identical to the unchunked baseline
        assert trace["fetched"] == base_trace["fetched"]
        scored = [u for c in trace["scored"] for u in c]
        assert scored == [u for b in trace["fetched"] for u in b] == base_rows
        assert len(scored) == n and len(set(scored)) == n              # no omitted row, no duplicated row


def test_mysql_chunking_applies_after_the_time_window_filter(lg, cfg, monkeypatch):
    base = _run_mysql(lg, cfg, monkeypatch, 2600, fetch=1200, score=1200, since_days=10)
    chunked = _run_mysql(lg, cfg, monkeypatch, 2600, fetch=1200, score=500, since_days=10)
    assert chunked[0] == base[0] and base[0]
    assert all(c <= 500 for c in chunked[1]) and sum(chunked[1]) == sum(base[1]) < 2600     # rows outside the window never scored


def test_mysql_fetch_logging_still_reports_the_fetch_batch(lg, cfg, monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _run_mysql(lg, cfg, monkeypatch, 12, fetch=5, score=600)
    line = next(r.getMessage() for r in caplog.records if "streamed-score" in r.getMessage() and "mysql_" in r.getMessage())
    assert " batch=5 " in line and "score_batch=600" in line


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
    n = 2300                                                                        # fetch 1000 -> 1000,1000,300 ; score 500 -> 2300 is not divisible by 500
    base_urls, base_chunks, _, _ = _run_mysql(lg, cfg, monkeypatch, n, fetch=1000, score=1000)
    urls, chunks, limits, _ = _run_mysql(lg, cfg, monkeypatch, n, fetch=1000, score=500)
    assert chunks == [500, 500, 500, 500, 300] and sum(chunks) == n                 # the 300-row tail is scored once
    assert urls == base_urls and len(urls) == len(set(urls))                       # same rows, none duplicated, none lost
    assert set(urls) <= {f"u/{i}" for i in range(n)} and set(limits) == {1000}


def test_mysql_effective_scoring_batch_is_never_below_500_even_if_the_module_value_is_patched(lg, cfg, monkeypatch):
    for bad in (1, 2, 499, 0, None, -7):
        _, chunks, limits, _ = _run_mysql(lg, cfg, monkeypatch, 1300, fetch=1300, score=bad)
        assert chunks == [500, 500, 300] and set(limits) == {1300}
