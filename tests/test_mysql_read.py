"""
tests/test_mysql_read.py — MySQL read sources (mysql_signals.py + logics wiring)

No real MySQL / Mongo / OpenAI: an in-memory fake MySQL server answers the
two SELECTs mysql_signals issues (information_schema columns + the keyset data
query) and fails on anything that is not a SELECT.
"""
import importlib
import logging
import re
import struct
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DIM = 4
NOW = datetime.now(timezone.utc).replace(microsecond=0)
PASSWORD = "S3cr3t-pw-xyz"


# ── module setup (same stubbing pattern as tests/test_parallel_fetch.py) ─────
@pytest.fixture(scope="module")
def lg():
    import os
    os.environ.setdefault("OPENAI_API_KEY", "test-key-mysql")
    db_stub = types.ModuleType("database")
    _coll = MagicMock()
    for attr in ["db", "jobs_collection", "signals_collection", "signals_collection_2",
                 "signals_collection_4", "google_posts_collection",
                 "topic_evidence_cache_collection", "website_evidence_cache_collection"]:
        setattr(db_stub, attr, _coll)
    fi_stub = types.ModuleType("flintel")
    fi_stub.ROUTER_UNFILTERED_ADDENDUM = ""
    fi_stub.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM = ""
    fi_stub.build_google_fallback_answer_context = None
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.AsyncClient = MagicMock()
    httpx_stub.TimeoutException = Exception
    httpx_stub.HTTPStatusError = Exception
    for name, mod in {"database": db_stub, "flintel": fi_stub,
                      "website_intelligence": types.ModuleType("website_intelligence"),
                      "google": types.ModuleType("google"), "httpx": httpx_stub}.items():
        sys.modules[name] = mod
    for key in list(sys.modules):
        if key in ("logics", "config", "mysql_signals"):
            del sys.modules[key]
    mod = importlib.import_module("logics")
    mod.SIGNAL_EMBEDDING_CANDIDATE_POOL = 0
    mod.SIGNAL_EMBEDDING_RECENCY_POOL = 0
    yield mod


@pytest.fixture
def cfg(lg, monkeypatch):
    import config
    monkeypatch.setattr(config, "MYSQL_READ_ENABLED", False)
    monkeypatch.setattr(config, "MYSQL_READ_DATABASES", "flintel,flintel_static,flintel_google")
    monkeypatch.setattr(config, "MYSQL_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "STREAMING_SCORE_ENABLED", True)
    monkeypatch.setattr(config, "STRICT_INTENT_MODE", False)
    monkeypatch.setattr(config, "INTENT_BRIDGE_ENABLED", False)
    monkeypatch.setattr(config, "SCAN_DEADLINE_SECONDS", 30)
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD", 0.35)
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", 5000)
    return config


# ── vectors / fixtures ────────────────────────────────────────────────────────
Q = [1.0, 0.0, 0.0, 0.0]


def vec_with_sim(sim):
    """Unit vector whose cosine with Q is exactly `sim`."""
    return [sim, float(np.sqrt(max(0.0, 1 - sim * sim))), 0.0, 0.0]


def blob(v):
    return struct.pack("<%df" % len(v), *v)


REDDIT_COLS = ["id", "message_id", "topic_key", "search_keyword", "platform", "subreddit",
               "username", "title", "text", "post_url", "score", "num_comments", "created_utc",
               "fetched_at", "embedding", "embedding_dim", "embedding_model"]
GOOGLE_COLS = ["id", "message_id", "platform", "post_url", "text", "username",
               "subreddit_or_channel", "posted_at", "fetched_at", "google_rank", "search_keyword",
               "client_id", "created_at", "embedding", "embedding_dim", "embedding_model"]


def r_row(i, url, sim, age_days=1, text="some text", emb=True, sub="cars", raw_blob=None):
    return {"id": i, "platform": "reddit", "subreddit": sub, "title": f"title {i}", "text": text,
            "post_url": url, "created_utc": (NOW - timedelta(days=age_days)).replace(tzinfo=None),
            "fetched_at": NOW.replace(tzinfo=None),
            "embedding": (raw_blob if raw_blob is not None else (blob(vec_with_sim(sim)) if emb else None))}


def g_row(i, url, sim, posted_days=None, created_days=1, text="google text"):
    return {"id": i, "platform": "reddit", "subreddit_or_channel": "r/askcars", "text": text,
            "post_url": url,
            "posted_at": (NOW - timedelta(days=posted_days)).replace(tzinfo=None) if posted_days is not None else None,
            "created_at": (NOW - timedelta(days=created_days)).replace(tzinfo=None),
            "fetched_at": NOW.replace(tzinfo=None), "embedding": blob(vec_with_sim(sim))}


class FakeMySQL:
    """In-memory server: tables[db] = (columns, rows). Executes only the two
    SELECT shapes mysql_signals issues; anything else raises."""

    def __init__(self, tables, fail_dbs=(), fail_message="Lost connection (timeout)",
                 on_query=None, delay=0.0):
        self.tables = tables
        self.fail_dbs = set(fail_dbs)
        self.fail_message = fail_message
        self.on_query = on_query
        self.delay = delay
        self.executed = []
        self.lock = threading.Lock()
        self.closed = 0

    def connect(self):
        server = self

        class Cur:
            def __init__(self):
                self._rows = []

            def execute(self, sql, params=None):
                with server.lock:
                    server.executed.append((sql, params))
                if not sql.lstrip().upper().startswith("SELECT"):
                    raise AssertionError("non-SELECT statement sent to MySQL: " + sql)
                if "information_schema" in sql:
                    db = params[0]
                    if db in server.fail_dbs:
                        raise RuntimeError(server.fail_message)
                    cols = server.tables.get(db, ([], []))[0]
                    self._rows = [(c,) for c in cols]
                    return
                m = re.match(r"SELECT (.*) FROM `(\w+)`\.`flintel_signals` WHERE (.*) ORDER BY `id` DESC LIMIT %s$", sql)
                assert m, sql
                exprs = [e.strip() for e in re.split(r",\s*(?![^()]*\))", m.group(1))]
                db = m.group(2)
                if db in server.fail_dbs:
                    raise RuntimeError(server.fail_message)
                if server.on_query:
                    server.on_query(db)
                if server.delay:
                    time.sleep(server.delay)
                cols, rows = server.tables[db]
                p = list(params)
                last_id, limit = p[0], p[-1]
                cutoff = p[1] if len(p) == 3 else None

                def val(row, expr):
                    if expr == "NULL":
                        return None
                    if expr.startswith("COALESCE("):
                        for c in re.findall(r"`(\w+)`", expr):
                            if row.get(c) is not None:
                                return row.get(c)
                        return None
                    return row.get(expr.strip("`"))

                created_expr = None
                if cutoff is not None:
                    created_expr = m.group(3).split(" AND ")[-1].rsplit(" >= %s", 1)[0]
                out = []
                for row in sorted(rows, key=lambda r: -r["id"]):
                    if row.get("embedding") is None or row["id"] >= last_id:
                        continue
                    if cutoff is not None:
                        c = val(row, created_expr)
                        if c is None or c < cutoff:
                            continue
                    out.append(tuple(val(row, e) for e in exprs))
                    if len(out) >= limit:
                        break
                self._rows = out

            def fetchall(self):
                return self._rows

            def close(self):
                pass

        class Conn:
            def cursor(self):
                return Cur()

            def close(self):
                server.closed += 1

        return Conn()


def install(lg, cfg, monkeypatch, server, enabled=True):
    monkeypatch.setattr(cfg, "MYSQL_READ_ENABLED", enabled)
    import mysql_signals
    monkeypatch.setattr(mysql_signals, "_PYMYSQL", types.ModuleType("pymysql"))
    monkeypatch.setattr(mysql_signals, "_PYMYSQL_FAILED", False)
    monkeypatch.setattr(mysql_signals, "connect", server.connect)
    mysql_signals._warned.clear()
    mysql_signals._dim_warn_last.clear()
    return mysql_signals


def mongo_coll(docs):
    class Cursor:
        def __init__(self, d): self.d = list(d)
        def sort(self, *a, **k): return self
        def batch_size(self, n): return self
        def limit(self, n): return Cursor(self.d[:n])
        def max_time_ms(self, n): return self
        def __iter__(self): return iter(self.d)
    c = MagicMock()
    c.find.side_effect = lambda *a, **k: Cursor(docs)
    return c


def mdoc(url, sim, age_days=1):
    return {"post_url": url, "post_text": "mongo text", "title": "mongo title", "platform": "reddit",
            "subreddit": "mongosub", "created_utc": (NOW - timedelta(days=age_days)).replace(tzinfo=None),
            "embedding": vec_with_sim(sim)}


def run(lg, monkeypatch, mongo_docs, **kw):
    monkeypatch.setattr(lg, "signals_collection", mongo_coll(mongo_docs))
    calls = []

    def fake_embed(items):
        calls.append(list(items))
        return [Q for _ in items]
    monkeypatch.setattr(lg, "generate_query_embeddings_batch", fake_embed)
    state = kw.pop("scan_state", {})
    out = lg.get_matched_signals(topic_key="t", keywords=["buy a car"], scan_state=state, **kw)
    return out, state, calls


def three_dbs():
    return {
        "flintel": (REDDIT_COLS, [r_row(1, "u/f1", 0.9), r_row(2, "u/f2", 0.2),
                                  r_row(3, "u/f3", 0.5, emb=False)]),
        "flintel_static": (REDDIT_COLS[:], [r_row(1, "u/s1", 0.6)]),
        "flintel_google": (GOOGLE_COLS, [g_row(1, "u/g1", 0.7, posted_days=None, created_days=2),
                                         g_row(2, "u/g2", 0.8, posted_days=1)]),
    }


# ── 1. flag off ───────────────────────────────────────────────────────────────
def test_1_flag_off_no_mysql_import_and_mongo_result_unchanged(lg, cfg, monkeypatch, caplog):
    for m in ("mysql_signals", "pymysql"):
        sys.modules.pop(m, None)
    caplog.set_level(logging.INFO, logger="flintel-web")
    out, state, _ = run(lg, monkeypatch, [mdoc("m/1", 0.9), mdoc("m/2", 0.1)])
    assert "mysql_signals" not in sys.modules and "pymysql" not in sys.modules
    assert [p["post_url"] for p in out] == ["m/1"]
    assert "mysql" not in caplog.text.lower()
    assert all(not str(k).startswith("mysql_") for k in state.get("fetched", {}))


# ── 2. mapping of the three schemas ──────────────────────────────────────────
def test_2_rows_mapped_to_mongo_shape(lg, cfg, monkeypatch):
    srv = FakeMySQL(three_dbs())
    ms = install(lg, cfg, monkeypatch, srv)
    got = {}
    for db in ("flintel", "flintel_static", "flintel_google"):
        for docs, M in ms.iter_signal_batches(ms.MySQLSource(db), dim=DIM, connect_fn=srv.connect):
            assert M.dtype == np.float32 and M.shape == (len(docs), DIM)
            for d in docs:
                got[d["post_url"]] = d
    keys = {"title", "text", "post_url", "platform", "subreddit", "created_utc"}
    assert all(set(d) == keys for d in got.values())
    f1 = got["u/f1"]
    assert f1["title"] == "title 1" and f1["subreddit"] == "cars" and f1["text"] == "some text"
    assert f1["created_utc"].tzinfo is not None and f1["created_utc"].utcoffset() == timedelta(0)
    g1, g2 = got["u/g1"], got["u/g2"]
    assert g1["title"] is None and g1["subreddit"] == "r/askcars"
    assert g1["created_utc"] == NOW - timedelta(days=2)       # posted_at NULL -> created_at
    assert g2["created_utc"] == NOW - timedelta(days=1)       # posted_at wins
    assert "u/f3" not in got                                  # NULL embedding never returned


# ── 3. BLOB decode ────────────────────────────────────────────────────────────
def test_3_blob_decode_and_bad_length_skipped(lg, cfg, monkeypatch, caplog):
    import mysql_signals as ms
    v = np.arange(1536, dtype="<f4")
    out = ms.decode_embedding(v.tobytes(), 1536)
    assert len(v.tobytes()) == 6144 and np.array_equal(out, v)
    assert ms.decode_embedding(v.tobytes()[:6140], 1536) is None
    tables = {"flintel": (REDDIT_COLS, [r_row(1, "ok", 0.9), r_row(2, "bad1", 0, raw_blob=b"\0" * 14),
                                        r_row(3, "bad2", 0, raw_blob=b"\0" * 14)])}
    srv = FakeMySQL(tables)
    install(lg, cfg, monkeypatch, srv)
    caplog.set_level(logging.WARNING, logger="flintel-web")
    docs = [d for ds, _ in ms.iter_signal_batches(ms.MySQLSource("flintel"), dim=DIM, connect_fn=srv.connect)
            for d in ds]
    assert [d["post_url"] for d in docs] == ["ok"]
    assert caplog.text.count("embedding blob length") == 1     # throttled, not per row


# ── 4. NULL embedding: skipped, no lazy backfill / embedding call ────────────
def test_4_null_embedding_no_backfill(lg, cfg, monkeypatch):
    srv = FakeMySQL({"flintel": (REDDIT_COLS, [r_row(1, "u/emb", 0.9), r_row(2, "u/null", 0.9, emb=False)])})
    install(lg, cfg, monkeypatch, srv)
    monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", "flintel")
    seen = []
    monkeypatch.setattr(lg, "_lazy_backfill_missing_embeddings",
                        lambda raw, coll, fn: seen.extend(raw) or [])
    out, _, calls = run(lg, monkeypatch, [])
    urls = [p["post_url"] for p in out]
    assert urls == ["u/emb"]
    assert not any(d.get("post_url") == "u/null" for d in seen)
    assert calls == [["buy a car"]]                              # only the query embedding
    data_sql = [s for s, _ in srv.executed if "information_schema" not in s]
    assert all("`embedding` IS NOT NULL" in s for s in data_sql)


# ── 5. scoring, mixed merge, cross-source dedupe ─────────────────────────────
def test_5_threshold_merge_and_dedupe(lg, cfg, monkeypatch):
    srv = FakeMySQL(three_dbs())
    install(lg, cfg, monkeypatch, srv)
    mongo = [mdoc("m/1", 0.95), mdoc("m/low", 0.30), mdoc("u/g2", 0.40)]   # u/g2 also in MySQL (0.8)
    out, state, _ = run(lg, monkeypatch, mongo)
    urls = [p["post_url"] for p in out]
    assert urls == ["m/1", "u/f1", "u/g2", "u/g1", "u/s1"]       # sorted by similarity, >= 0.35 only
    assert urls.count("u/g2") == 1 and abs(state["sims"]["u/g2"] - 0.8) < 1e-5   # highest kept
    assert "m/low" not in urls and "u/f2" not in urls
    # MySQL scoring equals the Mongo scorer: same vector -> same similarity
    assert abs(state["sims"]["u/f1"] - 0.9) < 1e-6
    for lbl in ("mysql_flintel", "mysql_flintel_static", "mysql_flintel_google"):
        assert lbl in state["fetched"]
    assert state["complete"] is True and state["failed_labels"] == []


def test_5b_np_helper_matches_mongo_math(lg):
    rng = np.random.default_rng(0)
    D = rng.normal(size=(7, 5)).astype(np.float32)
    Qm = rng.normal(size=(3, 5)).astype(np.float32)
    Qm = Qm / np.linalg.norm(Qm, axis=1, keepdims=True)
    ref = (np.array(D.tolist(), dtype=np.float32) /
           np.linalg.norm(np.array(D.tolist(), dtype=np.float32), axis=1, keepdims=True)) @ Qm.T
    assert np.allclose(lg._np_max_scores(D, Qm), ref.max(axis=1))


# ── 6. time window ────────────────────────────────────────────────────────────
def test_6_time_window_in_sql(lg, cfg, monkeypatch):
    tables = three_dbs()
    tables["flintel"][1].append(r_row(9, "u/old", 0.95, age_days=30))
    tables["flintel_google"][1].append(g_row(9, "u/gold", 0.95, posted_days=30, created_days=1))
    srv = FakeMySQL(tables)
    install(lg, cfg, monkeypatch, srv)
    out, _, _ = run(lg, monkeypatch, [], since_days=7)
    urls = [p["post_url"] for p in out]
    assert "u/old" not in urls and "u/gold" not in urls and "u/f1" in urls and "u/g2" in urls
    data = [(s, p) for s, p in srv.executed if "information_schema" not in s]
    g_sql = [s for s, _ in data if "`flintel_google`" in s][0]
    f_sql = [s for s, _ in data if "`flintel`.`" in s][0]
    assert "COALESCE(`posted_at`, `created_at`) >= %s" in g_sql
    assert "`created_utc` >= %s" in f_sql
    assert all(len(p) == 3 and isinstance(p[1], datetime) for _, p in data)


# ── 7. one DB fails ───────────────────────────────────────────────────────────
def test_7_one_db_fails_others_returned(lg, cfg, monkeypatch):
    srv = FakeMySQL(three_dbs(), fail_dbs={"flintel_static"})
    install(lg, cfg, monkeypatch, srv)
    out, state, _ = run(lg, monkeypatch, [mdoc("m/1", 0.95)])
    urls = [p["post_url"] for p in out]
    assert {"m/1", "u/f1", "u/g1", "u/g2"} <= set(urls) and "u/s1" not in urls
    assert state["failed_labels"] == ["mysql_flintel_static"] and state["complete"] is False
    assert "mysql_flintel_static" not in state["new_wm"]


# ── 8. cancel / deadline ──────────────────────────────────────────────────────
def test_8a_cancel_stops_between_batches(lg, cfg, monkeypatch):
    import mysql_signals as ms
    rows = [r_row(i, f"u/{i}", 0.9) for i in range(1, 21)]
    srv = FakeMySQL({"flintel": (REDDIT_COLS, rows)})
    install(lg, cfg, monkeypatch, srv)
    flag = {"n": 0}
    gen = ms.iter_signal_batches(ms.MySQLSource("flintel"), dim=DIM, batch_size=5,
                                 connect_fn=srv.connect, cancelled=lambda: flag["n"] >= 1)
    with pytest.raises(ms.ScanCancelled):
        for _ in gen:
            flag["n"] += 1
    assert len([s for s, _ in srv.executed if "information_schema" not in s]) == 1
    assert srv.closed == 1


def test_8b_cancel_event_stops_mysql_scan(lg, cfg, monkeypatch):
    rows = [r_row(i, f"u/{i}", 0.9) for i in range(1, 51)]
    ev = threading.Event()
    srv = FakeMySQL({"flintel": (REDDIT_COLS, rows)}, on_query=lambda db: ev.set(), delay=0.05)
    install(lg, cfg, monkeypatch, srv)
    monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", "flintel")
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", 5)
    out, state, _ = run(lg, monkeypatch, [mdoc("m/1", 0.9)], cancel_event=ev)
    # the MySQL worker stops at the next batch boundary: nothing from it is kept,
    # the source is failed (same handling as a cancelled Mongo collection)
    assert not any(p["post_url"].startswith("u/") for p in out)
    assert state["complete"] is False and "mysql_flintel" in state["failed_labels"]
    assert len([s for s, _ in srv.executed if "information_schema" not in s]) <= 2


def test_8c_deadline_stops_slow_mysql(lg, cfg, monkeypatch):
    rows = [r_row(i, f"u/{i}", 0.9) for i in range(1, 201)]
    srv = FakeMySQL({"flintel": (REDDIT_COLS, rows)}, delay=0.2)
    install(lg, cfg, monkeypatch, srv)
    monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", "flintel")
    monkeypatch.setattr(cfg, "SCAN_DEADLINE_SECONDS", 1)
    monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_FETCH_BATCH", 2)
    t0 = time.monotonic()
    out, state, _ = run(lg, monkeypatch, [mdoc("m/1", 0.9)])
    assert time.monotonic() - t0 < 5
    assert [p["post_url"] for p in out] == ["m/1"]               # Mongo result still returned
    assert state["failed_labels"] == ["mysql_flintel"]
    time.sleep(0.5)                                               # worker notices and stops
    n = len(srv.executed)
    time.sleep(0.6)
    assert len(srv.executed) == n                                 # no further batches


# ── 9. password never logged ──────────────────────────────────────────────────
def test_9_password_not_in_logs_or_errors(lg, cfg, monkeypatch, caplog):
    srv = FakeMySQL(three_dbs(), fail_dbs={"flintel"},
                    fail_message=f"Access denied for user 'ro' (password {PASSWORD})")
    ms = install(lg, cfg, monkeypatch, srv)
    caplog.set_level(logging.DEBUG)
    run(lg, monkeypatch, [])
    assert "mysql_flintel: MySQL read failed" in caplog.text
    assert PASSWORD not in caplog.text


def test_9b_connect_error_masks_password(lg, cfg, monkeypatch):
    import mysql_signals as ms
    fake_pymysql = types.ModuleType("pymysql")

    def bad_connect(**kw):
        raise RuntimeError(f"cannot login with {kw['password']}")
    fake_pymysql.connect = bad_connect
    monkeypatch.setattr(ms, "_PYMYSQL", fake_pymysql)
    with pytest.raises(ConnectionError) as ei:
        ms.connect()
    assert PASSWORD not in str(ei.value) and "***" in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


# ── 10. database-name validation ──────────────────────────────────────────────
def test_10_db_name_validation(lg, cfg, monkeypatch, caplog):
    srv = FakeMySQL(three_dbs())
    ms = install(lg, cfg, monkeypatch, srv)
    monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", "flintel, flintel; DROP TABLE x ,ok_db,../x")
    caplog.set_level(logging.WARNING, logger="flintel-web")
    # (MYSQL SOURCE FAILURE FIX) invalid names are kept as failing sources with a
    # safe label (never a silent skip, never in SQL)
    srcs = ms.configured_sources()
    assert [s.label for s in srcs] == ["mysql_flintel", "mysql_invalid_1", "mysql_ok_db", "mysql_invalid_2"]
    assert "DROP TABLE" in caplog.text and "source fails" in caplog.text
    with pytest.raises(ValueError):
        next(ms.iter_signal_batches(srcs[1], dim=DIM, connect_fn=srv.connect))
    assert ms.build_select("flintel; DROP TABLE x", set(REDDIT_COLS), False)[0] is None
    with pytest.raises(ValueError):
        next(ms.iter_signal_batches("x`; DROP TABLE y", dim=DIM, connect_fn=srv.connect))


# ── 11. SELECT only ───────────────────────────────────────────────────────────
def test_11_only_select_statements(lg, cfg, monkeypatch):
    srv = FakeMySQL(three_dbs())        # the fake raises on any non-SELECT
    ms = install(lg, cfg, monkeypatch, srv)
    out, state, _ = run(lg, monkeypatch, [], since_days=30)
    assert state["failed_labels"] == [] and out
    assert srv.executed and all(s.lstrip().upper().startswith("SELECT") for s, _ in srv.executed)
    cur = srv.connect().cursor()
    for bad in ("DELETE FROM x", "UPDATE x SET a=1", "CREATE INDEX i ON t(a)", " insert into x values (1)"):
        with pytest.raises(ValueError):
            ms._select(cur, bad)


# ── extra: missing required columns -> that DB skipped, others fine ─────────────
def test_missing_required_columns_fails_db(lg, cfg, monkeypatch, caplog):
    tables = three_dbs()
    tables["flintel_static"] = (["id", "title", "embedding"], [])       # no post_url / text
    srv = FakeMySQL(tables)
    install(lg, cfg, monkeypatch, srv)
    caplog.set_level(logging.WARNING, logger="flintel-web")
    out, state, _ = run(lg, monkeypatch, [])
    assert "missing required column(s) post_url, text" in caplog.text
    assert {"u/f1", "u/g1"} <= {p["post_url"] for p in out}
    # (MYSQL SOURCE FAILURE FIX) a failure, not a successful skip
    assert state["failed_labels"] == ["mysql_flintel_static"] and state["complete"] is False
