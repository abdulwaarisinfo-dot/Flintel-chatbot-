"""
tests/test_mysql_source_fixes.py — the two audit fixes on top of the MySQL read task.

FIX 1 (MYSQL SOURCE FAILURE FIX): an enabled MySQL source that cannot be read
(pymysql missing, missing required columns / table, invalid name, no date column
for a time-window query) is a FAILED source — scan incomplete, no watermark
advance — never a successful skip. Unsupported modes (legacy pool, streaming
off) stay an intentional skip.

FIX 2 (MYSQL CARRY FIX): with MySQL enabled the configured database set is part
of the incremental scan signature, so enabling MySQL or changing
MYSQL_READ_DATABASES forces one full scan; flag off the signature is unchanged.

Fakes only (no real MySQL / Mongo / OpenAI). Helpers come from test_mysql_read.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_mysql_read import (  # noqa: F401  (lg / cfg are fixtures)
    lg, cfg, FakeMySQL, install, run, three_dbs, mdoc, r_row, g_row, blob, vec_with_sim,
    REDDIT_COLS, GOOGLE_COLS, PASSWORD, Q,
)

KW, PH = ["buy a car", "used car"], ["looking to buy"]


# ── shared: a tiny in-memory topic-evidence cache + 2-poll driver ────────────
class FakeCache:
    def __init__(self):
        self.doc = {}
        self.writes = []

    def find_one(self, q, proj=None):
        return dict(self.doc) if self.doc else None

    def update_one(self, q, upd, upsert=False):
        self.writes.append(dict(upd.get("$set", {})))
        self.doc.update(upd.get("$set", {}))


def poll(lg, monkeypatch, cache, mongo_docs, caplog=None):
    from tests.test_mysql_read import mongo_coll
    monkeypatch.setattr(lg, "signals_collection", mongo_coll(mongo_docs))
    monkeypatch.setattr(lg, "topic_evidence_cache_collection", cache)
    monkeypatch.setattr(lg, "generate_query_embeddings_batch", lambda items: [Q for _ in items])
    return lg.get_evidence_with_topup(
        "chat1", "owner1", "topic1", KW, 50, lg.get_matched_signals, match_phrases=PH,
    )


@pytest.fixture
def inc(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "INCREMENTAL_RESCAN_ENABLED", True)
    monkeypatch.setattr(cfg, "INCREMENTAL_WATERMARK_FIELD", "created_utc")
    monkeypatch.setattr(cfg, "INCREMENTAL_FULL_SCAN_EVERY_N_POLLS", 0)
    return cfg


def sims(cache):
    return {u: s for u, s in cache.doc.get("scan_sims", [])}


# ═════════════════════════════ FIX 1 ═════════════════════════════════════════
def test_f1_enabled_but_pymysql_missing_is_failure(lg, cfg, monkeypatch, caplog):
    import mysql_signals as ms
    real_connect = ms.connect
    install(lg, cfg, monkeypatch, FakeMySQL(three_dbs()))
    monkeypatch.setattr(ms, "connect", real_connect)       # real path: no pymysql
    monkeypatch.setattr(ms, "_PYMYSQL", None)
    monkeypatch.setattr(ms, "_PYMYSQL_FAILED", False)
    import builtins
    real_import = builtins.__import__

    def no_pymysql(name, *a, **k):
        if name == "pymysql":
            raise ImportError("No module named 'pymysql'")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_pymysql)
    caplog.set_level(logging.WARNING, logger="flintel-web")
    out, st, _ = run(lg, monkeypatch, [mdoc("m/1", 0.9)])
    assert [p["post_url"] for p in out] == ["m/1"]                      # Mongo still answers
    assert st["failed_labels"] == ["mysql_flintel", "mysql_flintel_static", "mysql_flintel_google"]
    assert st["complete"] is False
    assert caplog.text.count("pymysql is not available") == 1          # one-time warning kept
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize("cols,since,reason", [
    (["id", "title", "embedding"], None, "missing required column(s) post_url, text"),
    ([], None, "missing required column(s) id, post_url, text, embedding"),      # table/permission missing
    (["id", "post_url", "text", "embedding"], 7, "no created/posted column"),     # time window impossible
])
def test_f1_unreadable_schema_is_failure(lg, cfg, monkeypatch, caplog, cols, since, reason):
    t = three_dbs()
    t["flintel_static"] = (cols, [])
    install(lg, cfg, monkeypatch, FakeMySQL(t))
    caplog.set_level(logging.WARNING, logger="flintel-web")
    kw = {"since_days": since} if since else {}
    out, st, _ = run(lg, monkeypatch, [mdoc("m/1", 0.9)], **kw)
    assert st["failed_labels"] == ["mysql_flintel_static"] and st["complete"] is False
    assert reason in caplog.text and "mysql_flintel_static: MySQL read failed" in caplog.text
    assert "u/f1" in [p["post_url"] for p in out]                       # healthy sources still used


def test_f1_invalid_db_name_is_failure_not_skip(lg, cfg, monkeypatch):
    srv = FakeMySQL(three_dbs())
    install(lg, cfg, monkeypatch, srv)
    monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", "flintel,bad name;x")
    out, st, _ = run(lg, monkeypatch, [])
    assert st["failed_labels"] == ["mysql_invalid_1"] and st["complete"] is False
    assert not any("bad name" in s for s, _ in srv.executed)            # never reaches SQL


@pytest.mark.parametrize("mode", ["streaming_off", "legacy_pool"])
def test_f1_unsupported_mode_stays_intentional_skip(lg, cfg, monkeypatch, caplog, mode):
    srv = FakeMySQL(three_dbs())
    install(lg, cfg, monkeypatch, srv)
    if mode == "streaming_off":
        monkeypatch.setattr(cfg, "STREAMING_SCORE_ENABLED", False)
    else:
        monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_CANDIDATE_POOL", 500)
        monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_RECENCY_POOL", 500)
    caplog.set_level(logging.WARNING, logger="flintel-web")
    out, st, _ = run(lg, monkeypatch, [mdoc("m/1", 0.9)])
    assert st.get("failed_labels") == [] and st["complete"] is True
    assert srv.executed == []                                           # MySQL not touched
    assert "MySQL sources skipped for this configuration" in caplog.text


def test_f1_failed_source_does_not_advance_watermark(lg, inc, monkeypatch):
    # healthy MySQL: watermark + sims are persisted
    install(lg, inc, monkeypatch, FakeMySQL(three_dbs()))
    ok = FakeCache()
    poll(lg, monkeypatch, ok, [mdoc("m/1", 0.9)])
    assert ok.doc.get("scan_watermark")
    # same scan with one unreadable MySQL source: posts may be cached, but no
    # watermark / scan state is written
    t = three_dbs()
    t["flintel_google"] = (["id", "embedding"], [])
    install(lg, inc, monkeypatch, FakeMySQL(t))
    bad = FakeCache()
    merged = poll(lg, monkeypatch, bad, [mdoc("m/1", 0.9)])
    assert "m/1" in [p["post_url"] for p in merged]
    assert "scan_watermark" not in bad.doc and "scan_sims" not in bad.doc
    assert not any("scan_watermark" in w for w in bad.writes)
    # an existing watermark is not advanced either
    wm_before = dict(ok.doc["scan_watermark"])
    poll(lg, monkeypatch, ok, [mdoc("m/1", 0.9), mdoc("m/new", 0.95, age_days=0)])
    assert ok.doc["scan_watermark"] == wm_before


# ═════════════════════════════ FIX 2 ═════════════════════════════════════════
def test_f2_flag_off_signature_identical_to_pre_fix(lg, cfg, monkeypatch):
    # literals computed with the pre-fix logics._scan_signature (MySQL-read task code)
    monkeypatch.setattr(cfg, "MYSQL_READ_ENABLED", False)
    assert lg._scan_signature(KW, PH, "all", 7, "Find buyers", False, "created_utc") == \
        "44ff2b0cbbc169e24756e7de7aeabe094f7ae11049af34a692806290b04fee41"
    assert lg._scan_signature(["x"], None, "reddit", None, None, True, "_id") == \
        "7781ae90eb4f7966f42d91797dc7b0f47e0b5372f29babd8bd175b37c1a15248"
    # MySQL settings are ignored while the flag is off
    for dbs in ("flintel", "a,b,c", ""):
        monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", dbs)
        assert lg._scan_signature(KW, PH, "all", 7, "Find buyers", False, "created_utc") == \
            "44ff2b0cbbc169e24756e7de7aeabe094f7ae11049af34a692806290b04fee41"


def test_f2_signature_tracks_enabled_database_set(lg, cfg, monkeypatch):
    def sig():
        return lg._scan_signature(KW, PH, "all", 7, "Find buyers", False, "created_utc")
    monkeypatch.setattr(cfg, "MYSQL_READ_ENABLED", False)
    off = sig()
    monkeypatch.setattr(cfg, "MYSQL_READ_ENABLED", True)
    monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", "flintel,flintel_google")
    on = sig()
    monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", " flintel_google , flintel,flintel")
    assert sig() == on                               # order / spaces / duplicates don't matter
    monkeypatch.setattr(cfg, "MYSQL_READ_DATABASES", "flintel,flintel_google,flintel_static")
    assert len({off, on, sig()}) == 3


def test_f2_enabling_mysql_after_mongo_only_carry(lg, inc, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="flintel-web")
    mongo = [mdoc("dup/x", 0.5), mdoc("m/1", 0.9)]
    cache = FakeCache()
    install(lg, inc, monkeypatch, FakeMySQL(three_dbs()), enabled=False)   # poll 1: Mongo only
    poll(lg, monkeypatch, cache, mongo)
    assert abs(sims(cache)["dup/x"] - 0.5) < 1e-6
    t = three_dbs()
    t["flintel_google"][1].append({**g_row(9, "dup/x", 0.9, posted_days=1)})
    install(lg, inc, monkeypatch, FakeMySQL(t), enabled=True)              # poll 2: MySQL on
    caplog.clear()
    merged = poll(lg, monkeypatch, cache, mongo)
    assert "mode=full:no_valid_watermark" in caplog.text                   # carry invalidated
    assert abs(sims(cache)["dup/x"] - 0.9) < 1e-6                          # highest score kept
    assert [p["post_url"] for p in merged].count("dup/x") == 1


def test_f2_changing_database_list_forces_full_scan(lg, inc, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="flintel-web")
    cache = FakeCache()
    install(lg, inc, monkeypatch, FakeMySQL(three_dbs()))
    monkeypatch.setattr(inc, "MYSQL_READ_DATABASES", "flintel")
    poll(lg, monkeypatch, cache, [mdoc("m/1", 0.9)])
    # same set, different order/spacing -> delta allowed
    monkeypatch.setattr(inc, "MYSQL_READ_DATABASES", " flintel ")
    caplog.clear()
    poll(lg, monkeypatch, cache, [mdoc("m/1", 0.9)])
    assert "mode=delta" in caplog.text or "-> unchanged" in caplog.text
    # a database added -> full scan, and its posts appear
    monkeypatch.setattr(inc, "MYSQL_READ_DATABASES", "flintel,flintel_google")
    caplog.clear()
    merged = poll(lg, monkeypatch, cache, [mdoc("m/1", 0.9)])
    assert "mode=full:no_valid_watermark" in caplog.text
    assert {"u/g1", "u/g2"} <= {p["post_url"] for p in merged}


def test_f2_highest_score_dedupe_across_carry_mongo_and_all_mysql(lg, inc, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="flintel-web")
    t = three_dbs()
    t["flintel"][1].append(r_row(40, "dup/x", 0.6))
    t["flintel_static"][1].append(r_row(41, "dup/x", 0.7))
    t["flintel_google"][1].append(g_row(42, "dup/x", 0.85, posted_days=1))
    mongo = [mdoc("dup/x", 0.5), mdoc("m/1", 0.9)]
    cache = FakeCache()
    install(lg, inc, monkeypatch, FakeMySQL(t))
    first = poll(lg, monkeypatch, cache, mongo)                            # full scan
    assert [p["post_url"] for p in first].count("dup/x") == 1
    assert abs(sims(cache)["dup/x"] - 0.85) < 1e-6
    t2 = three_dbs()
    t2["flintel"][1].append(r_row(40, "dup/x", 0.6))
    t2["flintel_static"][1].append(r_row(41, "dup/x", 0.7))
    t2["flintel_google"][1].append(g_row(42, "dup/x", 0.85, posted_days=1))
    t2["flintel"][1].append(r_row(77, "new/y", 0.97))                      # genuinely new
    install(lg, inc, monkeypatch, FakeMySQL(t2))
    caplog.clear()
    second = poll(lg, monkeypatch, cache, mongo)                           # delta (same config)
    assert "mode=delta" in caplog.text
    urls = [p["post_url"] for p in second]
    assert urls.count("dup/x") == 1 and "new/y" in urls
    assert abs(sims(cache)["dup/x"] - 0.85) < 1e-6 and abs(sims(cache)["new/y"] - 0.97) < 1e-6
