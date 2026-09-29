"""github_signals.py

Self-contained, read-only signal source backed by JSON files checked into
this repo's Mongo/Mongo1/Mongo2/Mongo3 folders, instead of the live
flintel_signals MongoDB collection. See the module docstring at the bottom
of this file for the full picture.
"""

import os
import json
import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (env-driven, all defaults live right here — no config.py import,
# per the one-way-dependency rule below).
# ---------------------------------------------------------------------------

def _env_bool(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        log.warning(f"github_signals: invalid int for {name}={raw!r}, using default {default}")
        return default


GITHUB_SIGNALS_ENABLED = _env_bool("GITHUB_SIGNALS_ENABLED", True)
GITHUB_SIGNALS_BASE_DIR = os.getenv(
    "GITHUB_SIGNALS_BASE_DIR",
    os.path.dirname(os.path.abspath(__file__)),
)
GITHUB_SIGNALS_DIRS = [
    d.strip() for d in os.getenv("GITHUB_SIGNALS_DIRS", "Mongo,Mongo1,Mongo2,Mongo3").split(",")
    if d.strip()
]
GITHUB_SIGNALS_CANDIDATE_POOL = _env_int("GITHUB_SIGNALS_CANDIDATE_POOL", 500)

# ---------------------------------------------------------------------------
# Module-level cache. Populated exactly once, lazily, on first call to a
# public function — never at import time.
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_docs = None  # None = not loaded yet; [] = loaded, nothing found/enabled


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _first_present(record, keys):
    """Return the first non-empty value among `keys` in `record`, else None."""
    for key in keys:
        try:
            value = record.get(key)
        except AttributeError:
            return None
        if value is not None and value != "":
            return value
    return None


def _parse_created_utc(value):
    """Best-effort parse of a created_utc value into a tz-aware UTC datetime.

    Accepts an ISO-8601 string (with or without a trailing "Z") or an epoch
    number (seconds). Returns None if it can't be parsed, which tells the
    caller to skip the record.
    """
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            # datetime.fromisoformat doesn't accept a trailing "Z" before
            # Python 3.11, so normalize it to an explicit UTC offset first.
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return None


def _is_valid_embedding(value):
    """True only for a non-empty list/tuple of numbers."""
    if not isinstance(value, (list, tuple)) or len(value) == 0:
        return False
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return False
    return True


def _normalize_record(record):
    """Turn one raw JSON record into a flintel_signals-shaped doc, or None
    if it should be skipped (missing/invalid embedding, empty text, or an
    unparseable created_utc)."""
    if not isinstance(record, dict):
        return None

    embedding = record.get("embedding")
    if not _is_valid_embedding(embedding):
        return None

    post_text = _first_present(record, ["post_text", "text", "body", "content", "selftext"])
    if not isinstance(post_text, str) or not post_text.strip():
        return None

    created_utc = _parse_created_utc(_first_present(record, ["created_utc"]))
    if created_utc is None:
        return None

    title = _first_present(record, ["title", "post_title", "headline"])
    post_url = _first_present(record, ["post_url", "url", "link", "permalink"])
    platform = _first_present(record, ["platform", "source", "source_platform"])

    return {
        "embedding": embedding,
        "title": title if isinstance(title, str) else (title or ""),
        "post_text": post_text,
        "post_url": post_url if isinstance(post_url, str) else (post_url or ""),
        "platform": platform if isinstance(platform, str) else (platform or ""),
        "created_utc": created_utc,
    }


def _iter_records_from_payload(payload):
    """Yield raw record dicts out of whatever shape a JSON file decoded to:
    a list, a dict wrapping a list under a known key, or a single dict doc."""
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(payload, dict):
        for key in ("data", "signals", "posts", "items", "results"):
            inner = payload.get(key)
            if isinstance(inner, list):
                for item in inner:
                    if isinstance(item, dict):
                        yield item
                return
        # No known wrapper key with a list inside — treat the dict itself
        # as a single record.
        yield payload


def _load_one_json_file(path):
    """Load and normalize every record out of a single .json/.jsonl file.
    Never raises — a corrupt/unreadable file just yields nothing."""
    docs = []
    try:
        is_jsonl = path.lower().endswith(".jsonl")
        with open(path, "r", encoding="utf-8") as f:
            if is_jsonl:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    for record in _iter_records_from_payload(payload):
                        doc = _normalize_record(record)
                        if doc is not None:
                            docs.append(doc)
            else:
                payload = json.load(f)
                for record in _iter_records_from_payload(payload):
                    doc = _normalize_record(record)
                    if doc is not None:
                        docs.append(doc)
    except Exception as exc:
        log.warning(f"github_signals: skipping unreadable/corrupt file {path!r}: {exc}")
        return []
    return docs


def _discover_json_files(base_dir, subdirs):
    """Recursively find every .json/.jsonl file under each configured
    subdirectory of base_dir. Missing subdirectories are silently skipped."""
    files = []
    for subdir in subdirs:
        root = os.path.join(base_dir, subdir)
        if not os.path.isdir(root):
            continue
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if name.lower().endswith((".json", ".jsonl")):
                        files.append(os.path.join(dirpath, name))
        except OSError as exc:
            log.warning(f"github_signals: could not walk directory {root!r}: {exc}")
    return files


def _load_all():
    """Do the actual disk scan + normalize + sort. Never raises — any
    unexpected failure results in an empty cache rather than a crash."""
    if not GITHUB_SIGNALS_ENABLED:
        log.info("github_signals: disabled via GITHUB_SIGNALS_ENABLED, nothing loaded")
        return []

    try:
        files = _discover_json_files(GITHUB_SIGNALS_BASE_DIR, GITHUB_SIGNALS_DIRS)
    except Exception as exc:
        log.warning(f"github_signals: failed to discover files under {GITHUB_SIGNALS_BASE_DIR!r}: {exc}")
        return []

    all_docs = []
    files_loaded = 0
    for path in files:
        try:
            docs = _load_one_json_file(path)
        except Exception as exc:
            # _load_one_json_file already catches its own errors, but this
            # belt-and-suspenders catch guarantees one bad file can never
            # take the whole load down.
            log.warning(f"github_signals: unexpected error loading {path!r}: {exc}")
            docs = []
        if docs:
            files_loaded += 1
            all_docs.extend(docs)

    try:
        all_docs.sort(key=lambda d: d["created_utc"], reverse=True)
    except Exception as exc:
        log.warning(f"github_signals: failed to sort loaded docs by created_utc: {exc}")

    log.info(
        f"github_signals: loaded {len(all_docs)} valid doc(s) from {files_loaded} file(s) "
        f"(scanned {len(files)} file(s) under {GITHUB_SIGNALS_DIRS} in {GITHUB_SIGNALS_BASE_DIR!r})"
    )
    return all_docs


def _ensure_loaded():
    global _cache_docs
    if _cache_docs is not None:
        return _cache_docs
    with _cache_lock:
        if _cache_docs is None:
            try:
                _cache_docs = _load_all()
            except Exception as exc:
                log.warning(f"github_signals: load failed entirely, falling back to empty cache: {exc}")
                _cache_docs = []
    return _cache_docs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_github_signal_docs(cutoff=None, limit=None):
    """Return flintel_signals-shaped docs loaded from the repo's JSON
    folders, newest-first.

    - cutoff: an optional timezone-aware datetime; only docs with
      created_utc >= cutoff are returned.
    - limit: max docs to return (defaults to GITHUB_SIGNALS_CANDIDATE_POOL).

    Never raises. Returns [] if disabled, unconfigured, or nothing loaded.
    """
    try:
        docs = _ensure_loaded()
        if not docs:
            return []

        effective_limit = limit if isinstance(limit, int) and limit >= 0 else GITHUB_SIGNALS_CANDIDATE_POOL

        if cutoff is not None:
            try:
                filtered = [d for d in docs if d["created_utc"] >= cutoff]
            except TypeError as exc:
                # e.g. a naive cutoff compared against aware datetimes.
                log.warning(f"github_signals: cutoff comparison failed, ignoring cutoff: {exc}")
                filtered = docs
        else:
            filtered = docs

        return filtered[:effective_limit]
    except Exception as exc:
        log.warning(f"github_signals: get_github_signal_docs failed: {exc}")
        return []


def reload_github_signals():
    """Clear the cache and reload from disk. Returns the number of valid
    docs loaded (0 on any failure). Not called anywhere yet — here for a
    future admin/reload endpoint."""
    global _cache_docs
    try:
        with _cache_lock:
            _cache_docs = _load_all()
        return len(_cache_docs)
    except Exception as exc:
        log.warning(f"github_signals: reload_github_signals failed: {exc}")
        with _cache_lock:
            _cache_docs = []
        return 0


# ---------------------------------------------------------------------------
# What this module is for
# ---------------------------------------------------------------------------
# flintel_signals normally lives in MongoDB (see database.py), fed by
# whatever live scrapers/ingestion jobs populate it. This module is a
# parallel, read-only, file-backed stand-in for that same shape of data:
# it walks the Mongo/Mongo1/Mongo2/Mongo3 folders checked into this repo,
# reads every .json/.jsonl file it finds, and normalizes each record into
# the exact same doc shape a flintel_signals query would return
# ({embedding, title, post_text, post_url, platform, created_utc}) so
# whatever calls get_github_signal_docs() can treat these docs identically
# to real database documents — merge them into the same candidate pool,
# rank them, embed-match them, whatever the caller needs. It is entirely
# self-contained (stdlib only, no imports from this project's other
# modules) so it can be dropped in or ripped out without touching
# index.py/logics.py/flintel.py/database.py, and it never does any disk
# I/O until the first time a caller actually asks for docs.
