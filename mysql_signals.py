"""
FLINTEL — MySQL SIGNALS READER (read-only)
============================================================================
The background services also write signals into MySQL: one `flintel_signals`
table in each database listed in config.MYSQL_READ_DATABASES (default
`flintel`, `flintel_static`, `flintel_google`). This module READS those rows
so logics.get_matched_signals() can score them next to the Mongo collections.

Rules:
  * SELECT only. Every statement goes through _select(), which refuses
    anything that is not a SELECT. No INSERT/UPDATE/DELETE/DDL, no index, no
    backfill, no embedding generation.
  * Never imported by logics.py unless MYSQL_READ_ENABLED is on, and pymysql
    itself is imported lazily here, so a missing pymysql never breaks the app
    (the MySQL sources are then simply not used, with one warning).
  * Rows are streamed in id-keyset batches (`WHERE id < last_id ORDER BY id
    DESC LIMIT N`), so a whole table is never held in RAM and a cancel or the
    scan deadline stops between batches.
  * Each row is mapped to the SAME shape as a Mongo signal doc
    (title, text, post_url, platform, subreddit, created_utc) and its
    embedding BLOB (flat float32 little-endian) is decoded with
    numpy.frombuffer. Rows whose blob length is not dim*4 are skipped.
  * Credentials come from config/env only and never appear in a log line or
    an exception message produced here (see safe_error()).
"""

import logging
import re
import time
from datetime import datetime, timezone

import numpy as np

log = logging.getLogger("flintel-web")

TABLE = "flintel_signals"
LABEL_PREFIX = "mysql_"
_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

# ── Column mapping ───────────────────────────────────────────────────────────
# Output field (Mongo-doc name) -> candidate MySQL columns, first present wins.
# "created_utc" uses COALESCE over every candidate column that exists, so
# flintel/flintel_static map to `created_utc` and flintel_google (which has no
# created_utc) maps to COALESCE(posted_at, created_at). To add a database with
# yet another schema, extend these lists — nothing else needs to change.
FIELD_COLUMNS = {
    "title":       ["title"],
    "text":        ["text"],
    "post_url":    ["post_url"],
    "platform":    ["platform"],
    "subreddit":   ["subreddit", "subreddit_or_channel"],
    "created_utc": ["created_utc", "posted_at", "created_at"],
}
# Without these a database is skipped (with a clear warning).
REQUIRED_COLUMNS = ("id", "post_url", "text", "embedding")

_PYMYSQL = None          # module once imported
_PYMYSQL_FAILED = False
_warned = set()          # one-time warnings (per process)
_dim_warn_last = {}      # db -> monotonic time of the last dim-mismatch warning
_DIM_WARN_EVERY_S = 60.0


class ScanCancelled(Exception):
    """Raised between batches when the caller's cancelled() returns True."""


class SourceUnreadable(Exception):
    """(MYSQL SOURCE FAILURE FIX) A configured, enabled source that cannot be
    read as a signals table (missing required columns, table/permission
    missing, or no date column for a time-window query). Raised — never a
    silent skip — so the caller marks the source FAILED: the scan is then
    incomplete and no watermark advances."""


class MySQLSource:
    """One `<db>`.flintel_signals table, used as an extra scan source."""
    is_mysql_source = True

    def __init__(self, db: str, label: str = None):
        self.db = db
        self.label = label or (LABEL_PREFIX + db)

    def __repr__(self):
        return f"MySQLSource({self.db})"


def _warn_once(key, message):
    if key in _warned:
        return
    _warned.add(key)
    log.warning(message)


def _cfg(name, default=None):
    try:
        import config as _config_module  # noqa: PLC0415
        return getattr(_config_module, name, default)
    except Exception:
        return default


def _load_pymysql():
    """Import pymysql lazily. None when it is not installed (warned once)."""
    global _PYMYSQL, _PYMYSQL_FAILED
    if _PYMYSQL is not None or _PYMYSQL_FAILED:
        return _PYMYSQL
    try:
        import pymysql  # noqa: PLC0415
        _PYMYSQL = pymysql
    except Exception as exc:
        _PYMYSQL_FAILED = True
        _warn_once("no_pymysql", f"MYSQL_READ_ENABLED is on but pymysql is not available "
                                 f"({type(exc).__name__}); every MySQL source will fail "
                                 f"(scan marked incomplete) until it is installed.")
    return _PYMYSQL


def valid_db_name(name) -> bool:
    return isinstance(name, str) and bool(_DB_NAME_RE.match(name))


def configured_sources() -> list:
    """MySQLSource list for this process ([] only when MYSQL_READ_ENABLED is off).

    (MYSQL SOURCE FAILURE FIX) Every configured name becomes a source, even
    when it cannot be read: a missing pymysql or an invalid database name is
    NOT a silent skip any more. Such a source fails when it is read
    (connect() raises without pymysql; iter_signal_batches() raises
    ValueError for an invalid name before any SQL), so the scan is marked
    incomplete. An invalid name never reaches SQL or a label: its label is
    mysql_invalid_<n>."""
    if not _cfg("MYSQL_READ_ENABLED", False):
        return []
    _load_pymysql()                      # one-time warning when it is missing
    raw = _cfg("MYSQL_READ_DATABASES", "") or ""
    out, seen = [], set()
    n_invalid = 0
    for part in str(raw).split(","):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if not valid_db_name(name):
            n_invalid += 1
            _warn_once(("bad_db", name), f"MYSQL_READ_DATABASES: invalid database name {name!r} "
                                         f"(only A-Z, a-z, 0-9, _ allowed) — source fails")
            out.append(MySQLSource(name, label=f"{LABEL_PREFIX}invalid_{n_invalid}"))
            continue
        out.append(MySQLSource(name))
    return out


def safe_error(exc) -> str:
    """Exception text with every configured secret masked."""
    text = f"{type(exc).__name__}: {exc}"
    for secret in (_cfg("MYSQL_PASSWORD", ""),):
        if secret:
            text = text.replace(str(secret), "***")
    return text


def connect():
    """Open one read connection from config (utf8mb4, autocommit, timeouts)."""
    pymysql = _load_pymysql()
    if pymysql is None:
        raise RuntimeError("pymysql not available")
    kwargs = dict(
        host=_cfg("MYSQL_HOST", "") or "localhost",
        port=int(_cfg("MYSQL_PORT", 3306) or 3306),
        user=_cfg("MYSQL_USER", "") or None,
        password=_cfg("MYSQL_PASSWORD", "") or "",
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=int(_cfg("MYSQL_CONNECT_TIMEOUT", 10) or 10),
        read_timeout=int(_cfg("MYSQL_READ_TIMEOUT", 60) or 60),
    )
    if _cfg("MYSQL_SSL", False):
        ca = _cfg("MYSQL_SSL_CA", "") or ""
        kwargs["ssl"] = {"ca": ca} if ca else {"check_hostname": False}
    try:
        return pymysql.connect(**kwargs)
    except Exception as exc:
        # re-raise without the original message chain (it can echo connect args)
        raise ConnectionError(f"MySQL connect failed: {safe_error(exc)}") from None


def _select(cursor, sql, params=None):
    """The ONLY way this module runs SQL: refuses anything but a SELECT."""
    if not sql.lstrip().upper().startswith("SELECT"):
        raise ValueError("mysql_signals: only SELECT statements are allowed")
    cursor.execute(sql, params)
    return cursor.fetchall()


def _table_columns(cursor, db) -> set:
    rows = _select(
        cursor,
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        (db, TABLE),
    )
    return {str(r[0]).lower() for r in rows or []}


def build_select(db: str, columns: set, with_cutoff: bool):
    """(sql, field_order, created_expr) for one database, or (None, reason).
    Column names come only from FIELD_COLUMNS/REQUIRED_COLUMNS (fixed
    identifiers); the db name is validated; values are always parameters."""
    if not valid_db_name(db):
        return None, f"invalid database name {db!r}"
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        return None, f"missing required column(s) {', '.join(missing)}"
    select, order = ["`id`"], ["id"]
    created_expr = None
    for field, cands in FIELD_COLUMNS.items():
        present = [c for c in cands if c in columns]
        if field == "created_utc":
            if present:
                created_expr = (f"`{present[0]}`" if len(present) == 1
                                else "COALESCE(" + ", ".join(f"`{c}`" for c in present) + ")")
                select.append(created_expr)
            else:
                select.append("NULL")
        else:
            select.append(f"`{present[0]}`" if present else "NULL")
        order.append(field)
    select.append("`embedding`")
    order.append("embedding")
    where = ["`embedding` IS NOT NULL", "`id` < %s"]
    if with_cutoff:
        if created_expr is None:
            return None, "time window requested but the table has no created/posted column"
        where.append(f"{created_expr} >= %s")
    sql = (f"SELECT {', '.join(select)} FROM `{db}`.`{TABLE}` "
           f"WHERE {' AND '.join(where)} ORDER BY `id` DESC LIMIT %s")
    return sql, order


def _aware(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def decode_embedding(blob, dim: int):
    """float32 little-endian BLOB -> np.ndarray(dim,), or None when unusable."""
    if blob is None:
        return None
    try:
        raw = bytes(blob)
    except Exception:
        return None
    if len(raw) != dim * 4:
        return None
    return np.frombuffer(raw, dtype="<f4")


def _warn_dim(db, got_bytes, dim):
    now = time.monotonic()
    if now - _dim_warn_last.get(db, -1e9) < _DIM_WARN_EVERY_S:
        return
    _dim_warn_last[db] = now
    log.warning(f"{LABEL_PREFIX}{db}: embedding blob length {got_bytes} != {dim * 4} bytes "
                f"(dim {dim}) — such rows are skipped")


def iter_signal_batches(source, *, cutoff=None, batch_size=5000, dim=1536, connect_fn=None,
                        cancelled=None):
    """Yield (docs, matrix) per batch for one MySQL source.

    docs   : list of Mongo-shaped dicts {title, text, post_url, platform,
             subreddit, created_utc} (no embedding key)
    matrix : np.ndarray (len(docs), dim) float32 — row i belongs to docs[i]

    `cancelled` (optional callable) is checked before every batch query;
    when it returns True, ScanCancelled is raised.
    Raises on connect/query errors (the caller marks the source as failed).
    The connection is closed when the generator finishes or is closed."""
    db = source.db if isinstance(source, MySQLSource) else str(source)
    if not valid_db_name(db):
        raise ValueError(f"invalid database name {db!r}")
    batch_size = max(1, int(batch_size or 5000))
    conn = (connect_fn or connect)()
    try:
        cur = conn.cursor()
        try:
            columns = _table_columns(cur, db)
            built = build_select(db, columns, cutoff is not None)
            if built[0] is None:
                # (MYSQL SOURCE FAILURE FIX) unreadable -> failure, not a skip
                raise SourceUnreadable(f"{built[1]} in `{db}`.{TABLE}")
            sql, order = built
            cutoff_naive = (cutoff.astimezone(timezone.utc).replace(tzinfo=None)
                            if cutoff is not None else None)
            last_id = 2 ** 63 - 1
            i_id, i_emb = order.index("id"), order.index("embedding")
            while True:
                if cancelled is not None and cancelled():
                    raise ScanCancelled()
                params = [last_id]
                if cutoff_naive is not None:
                    params.append(cutoff_naive)
                params.append(batch_size)
                rows = _select(cur, sql, tuple(params))
                if not rows:
                    return
                docs, vecs = [], []
                for row in rows:
                    vec = decode_embedding(row[i_emb], dim)
                    if vec is None:
                        if row[i_emb] is not None:
                            _warn_dim(db, len(row[i_emb]), dim)
                        continue
                    doc = {}
                    for k, field in enumerate(order):
                        if field in ("id", "embedding"):
                            continue
                        doc[field] = row[k]
                    doc["created_utc"] = _aware(doc.get("created_utc"))
                    docs.append(doc)
                    vecs.append(vec)
                last_id = rows[-1][i_id]
                if docs:
                    yield docs, np.vstack(vecs).astype(np.float32, copy=False)
                if len(rows) < batch_size:
                    return
        finally:
            try:
                cur.close()
            except Exception:
                pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
