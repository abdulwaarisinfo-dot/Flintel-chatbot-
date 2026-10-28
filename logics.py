"""
FLINTEL — LOGICS (the "brain")
============================================================================
Pulls the heavy "brain" logic out of index.py — signal matching, keyword
generation/fallback, the router, the analysis layer, the website-keyword
extraction wrapper, and the single LLM call function. This is what
actually shrinks index.py's size/load; index.py keeps only FastAPI app 
wiring plus chat/session/Mongo orchestration (flintel_users /
flintel_users_chat).

MOVED HERE VERBATIM (no logic change) from index.py: normalize_topic_key, 
normalize_platform, keyword-fallback generation, job/signal Mongo reads,
every signal-matching helper (including phrase-matching), get_matched_
signals() itself (matching rules 100% UNCHANGED), the whole Claude
analysis layer (map-reduce, prompts), the router, the topic-resolver, the
website-keyword-extraction wrapper, and the post-processing helpers
(format extraction, answer-finalization, URL/website-context patching).

_call_claude() / _call_claude_stream() talk to OpenAI's Responses API
(GPT-5 mini) — see each function's own docstring.

(TIMEOUT-SIMPLIFICATION CHANGE) _timeout_fallback_answer()'s old tier-3
"loose_candidates / near_match_confidence / near_match_offer" branch has
been removed entirely — see that function's own docstring.
_extract_near_match_confidence() is kept defined (for now) but is dead
code — nothing calls it anymore.

(CIRCULAR-IMPORT NOTE) index.py imports several names FROM this module
(see index.py's own top-of-file import block) — so this module must
NEVER import index.py at module load time. The one function here that
still needs a few index.py-owned functions (_timeout_fallback_answer(),
which needs flintel_users_chat orchestration helpers that correctly stay
defined in index.py) imports them LAZILY, inside its own function body —
see that function's docstring for why.

(ANALYST-PROMPT UPGRADE) CLAUDE_ANALYSIS_SYSTEM_PROMPT below has been
replaced with a "market intelligence analyst" style prompt (research
objective -> evidence classification -> pattern-finding -> pain-vs-intent
distinction -> adaptive-depth report), producing a richer JSON shape
(executive_summary / key_findings / detailed_findings / market_pattern /
conclusion / followup_question) instead of the old flat "summary" +
"followups[]" shape. NOTHING about retrieval/matching/keywords changed —
this only changes how Claude writes up the posts it's already given.
The two post-construction `.replace(...)` calls right after the prompt
were updated to match this new prompt's actual wording (the old ones
were written against the old prompt's phrasing and would have silently
stopped injecting MAX_CHAT_EVIDENCE_POSTS otherwise) — see the comment
right above those calls.

(EMBEDDING-BASED MATCHING CHANGE) get_matched_signals()'s own internal
"is this signal a match" decision (non-unfiltered branch only) has been
swapped from keyword/title/text substring matching to embedding cosine-
similarity matching against a single query embedding built from this
call's own keywords + match_phrases. This function's SIGNATURE, RETURN
SHAPE, and every caller are completely unchanged — see its own updated
docstring below for the full mechanism. The old keyword/phrase substring
helpers (_signal_keyword_matches, _text_matches_keyword,
_text_matches_any_phrase, _phrase_matches_text) are left defined,
untouched, but are now dead code as far as get_matched_signals() itself
is concerned — nothing else in this file, or in the rest of the product
(router, Claude prompts, evidence cache, timeout fallback, website-
intelligence, keyword/phrase generation), was touched by this change.

(EMBEDDING-MATCH QUERY/LOGGING CLEANUP) Two small, purely additive/
non-behavioral follow-ups to the EMBEDDING-BASED MATCHING CHANGE above,
both scoped to inside get_matched_signals() only:
  1. The Mongo existence guard on the "embedding" field now reads
     {"$ne": None, "$exists": True} instead of just {"$ne": None} — a
     bare "$ne: None" can, depending on driver/version behavior, also
     match documents where the field is simply absent, and
     _cosine_similarity() already safely scores any such doc's missing
     embedding as 0.0 (getting dropped by the threshold check either
     way) — so this isn't a bug fix, just making the query's intent
     explicit/clean.
  2. A single log.debug() line (never info/warning, so silent unless
     DEBUG logging is explicitly enabled) right after scored_docs is
     sorted, reporting — for this call's topic_key — the candidate pool
     size, the current SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD value, how
     many candidates cleared it, and the top 5 raw cosine-similarity
     scores across the whole candidate pool (computed purely for this
     log line, never used for matching/filtering). This exists only to
     give real production data to inform future tuning of
     SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD (config.py) — it changes no
     return value, no filtering, no caching, nothing else about this
     function's behavior.

(QUERY-EMBEDDING CACHE KEYING-MISMATCH FIX) get_matched_signals() now
takes an additional optional `chat_id` parameter (last in the signature,
default None, so every existing caller that doesn't pass it behaves
exactly as before). It exists purely so the QUERY-EMBEDDING CACHING
block (inside this function) keys its Mongo document the SAME way the
existing per-chat evidence-posts cache (get_cached_topic_evidence() /
save_topic_evidence_cache()) already does — {"chat_id": chat_id,
"topic_key": topic_key} — instead of "topic_key" alone, so the two
caches can never end up on two different documents for what is really
the same (chat, topic) pair. When `chat_id` isn't given at all, the
embedding-cache lookup/write is skipped entirely (uncached, exactly as
if this caching feature didn't exist) rather than ever writing an
ambiguous, chat-less document. get_evidence_with_topup() was updated to
pass its own `chat_id` through to matcher_fn() (get_matched_signals())
so the real production polling path stays cache-consistent; nothing
else about either function changed.

(PER-PHRASE EMBEDDING MATCHING FIX) get_matched_signals()'s own single,
combined query embedding (built by joining keywords + match_phrases into
ONE string) has been replaced with PER-ITEM embeddings — one embedding
per keyword and per match_phrase, generated in a single batched OpenAI
call via the new generate_query_embeddings_batch() — and the match
decision for each document is now the MAX cosine similarity across all
of those per-item embeddings, not the similarity against one blended/
averaged embedding. This exists purely to stop dilution: when
match_phrases genuinely cover different sub-topics/angles (e.g. "pricing
complaints" and "customer support delays" for the same brand), the old
single combined embedding averaged those angles together, so a document
that matched only ONE of them strongly showed a lower, diluted
similarity than it would have against that one phrase's own embedding.
See generate_query_embeddings_batch()'s own docstring and get_matched_
signals()'s own updated docstring for the full mechanism. The single-
string generate_query_embedding() is left completely untouched/as-is —
still defined, still used elsewhere/available for backward-compat — it
is simply no longer called from inside get_matched_signals() itself. The
QUERY-EMBEDDING CACHE (topic_evidence_cache_collection's own
"query_embedding" field) now stores/reads a LIST of vectors instead of a
single vector — see get_matched_signals()'s own docstring for the
updated cache shape; every other rule of that cache (signature-based
invalidation, chat_id+topic_key keying, "no chat_id -> skip caching
entirely") is completely unchanged.
"""

import re
import json
import time
import math
import hashlib
import hmac
import secrets
import heapq
import contextlib
import contextvars
import inspect
import threading
import concurrent.futures
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import httpx

import flintel
import timerange   # (DATE/TIME) natural-language search ranges
import platforms   # (PLATFORM REGISTRY) platform identity, URL hosts, labels, earliest valid date
import answer_integrity   # (ANSWER INTEGRITY) deterministic checks on the listed posts
import website_intelligence
import google as google_search   # the new google.py module

from database import (
    jobs_collection, signals_collection, signals_collection_2, signals_collection_4, google_posts_collection, topic_evidence_cache_collection,
    website_evidence_cache_collection,
)

from config import (
    MAX_KEYWORDS, CLAUDE_MAX_KEYWORDS, MAX_MATCHED_RESULTS,
    MAX_TIME_WINDOW_DAYS, MAX_POSTS_PER_PLATFORM,
    MAX_CHAT_EVIDENCE_POSTS, MAX_ANALYSIS_EVIDENCE,
    MIN_ANALYSIS_EVIDENCE, ANTHROPIC_API_KEY, CLAUDE_MODEL,
    CLAUDE_API_URL, CLAUDE_API_VERSION,
    LLM_MODEL, LLM_REASONING_EFFORT, LLM_REASONING_HEADROOM, LLM_VERBOSITY,
    OPENAI_RESPONSES_URL,
    CLAUDE_MAX_TOKENS, CLAUDE_MAP_MAX_TOKENS,
    CLAUDE_POSTS_PER_CHUNK, CLAUDE_NOTES_PER_CHUNK, CLAUDE_TIMEOUT_SECONDS,
    CLAUDE_ROUTER_MAX_TOKENS, CLAUDE_TOPIC_RESOLVER_MAX_TOKENS,
    MAX_WEBSITE_KEYWORDS, WEBSITE_FETCH_TIMEOUT_SECONDS,
    WEBSITE_FETCH_MAX_CHARS, CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS,
    TOPIC_CACHE_MAX_EVIDENCE, TOPIC_CACHE_MIN_TOPUP,
    MAX_WEBSITE_PAGES, WEBSITE_DISCOVERY_PATH_HINTS,
    WEBSITE_EVIDENCE_CACHE_TTL_DAYS, WEBSITE_EVIDENCE_MAX_TOKENS,
    WEBSITE_INSIGHT_MAX_TOKENS,
    # (EMBEDDING-BASED MATCHING CHANGE) mirrors the background service's
    # own embedding config, name-for-name — see config.py's own docstring.
    EMBEDDING_MODEL, OPENAI_API_KEY, EMBEDDING_TIMEOUT, EMBEDDING_MAX_CHARS,
    SIGNAL_EMBEDDING_CANDIDATE_POOL, SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD,
    SIGNAL_EMBEDDING_RECENCY_POOL, ROUTER_MAX_MATCH_PHRASES,
    SIGNAL_EMBEDDING_MAX_SCAN,
    SIGNAL_EMBEDDING_FETCH_BATCH,
    SIGNAL_EMBEDDING_MONGO_SCORE_BATCH, SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH,
    SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH_MIN,
    LAZY_EMBED_ENABLED, LAZY_EMBED_MAX_DOCS_PER_QUERY,
    LAZY_EMBED_BATCH_SIZE, LAZY_EMBED_MIN_TEXT_CHARS,
)

import logging
log = logging.getLogger("flintel-web")
try: import usage_meter as _um
except Exception: _um = None


def _usage_attribution(**ids):
    """Tags metered API calls with the search they serve (no-op without usage_meter)."""
    import contextlib
    return _um.attribution(**ids) if _um is not None else contextlib.nullcontext()


def _usage_request_id():
    """Request id of the search being handled, if any (set by POST /search)."""
    return _um.current_request_id() if _um is not None else None

# (RETRIEVAL RECALL FIX) Bounds on the lexical candidate tier's generated
# Mongo query — see _build_lexical_or_clause(). LEXICAL_MAX_TERMS caps how
# many distinct terms the alternation regex may contain (most selective
# first), and _LEXICAL_SEARCH_FIELDS lists the document fields it is
# applied to — the same title/text field names the rest of this module
# already reads via _TITLE_FIELD_CANDIDATES/_TEXT_FIELD_CANDIDATES.

# ─────────────────────────────────────────────────────────────────────────────
# STRICT INTENT MODE + INCREMENTAL RESCAN — shared helpers
# (config.STRICT_INTENT_MODE / config.INCREMENTAL_RESCAN_ENABLED)
#
# Every helper reads config LIVE (not at import) so a flag flip — and a test's
# monkeypatch — takes effect immediately. With STRICT_INTENT_MODE off, none of
# the strict branches added across this module run.
# ─────────────────────────────────────────────────────────────────────────────

def _cfg_value(name: str, default=None):
    try:
        import config as _config_module  # noqa: PLC0415
        return getattr(_config_module, name, default)
    except Exception:
        return default


def strict_intent_mode() -> bool:
    """True when STRICT_INTENT_MODE is on (accuracy over quantity)."""
    return bool(_cfg_value("STRICT_INTENT_MODE", False))


# (scan status) (chat_id, topic_key) -> {"complete": bool, "at": monotonic}.
# Written by get_matched_signals() at the end of each scan; read by the
# strict wait logic. "complete" == every collection fetch succeeded.
_SCAN_STATUS: dict = {}
_SCAN_STATUS_LOCK = threading.Lock()
_SCAN_STATUS_MAX = 2000


def _register_scan_status(chat_id, topic_key, complete: bool, scored: bool = True) -> None:
    """`complete` keeps its meaning (every collection read without error; the strict wait logic reads it).
    (FRESHNESS) `scored` is False for an exit that registered a status WITHOUT having scored anything
    (the query embeddings were unusable): that call is not evidence that a search produced results.
    `thread` is the registering thread, so a request can tell its own status from another scan's."""
    if not chat_id or not topic_key:
        return
    with _SCAN_STATUS_LOCK:
        if len(_SCAN_STATUS) >= _SCAN_STATUS_MAX:
            for k in list(_SCAN_STATUS)[: _SCAN_STATUS_MAX // 2]:
                _SCAN_STATUS.pop(k, None)
        _SCAN_STATUS[(chat_id, topic_key)] = {"complete": bool(complete), "at": time.monotonic(),
                                              "scored": bool(scored), "thread": threading.get_ident()}


def _mark_scan_unscored(chat_id, topic_key) -> None:
    """(FRESHNESS) Called just before get_matched_signals() returns [] because no usable query embeddings
    existed AFTER the collections were read: flags the status THIS thread registered as not scored.
    A status registered by another thread is left alone."""
    if not chat_id or not topic_key:
        return
    with _SCAN_STATUS_LOCK:
        entry = _SCAN_STATUS.get((chat_id, topic_key))
        if entry is not None and entry.get("thread") == threading.get_ident():
            entry["scored"] = False


def get_scan_status(chat_id, topic_key) -> Optional[dict]:
    with _SCAN_STATUS_LOCK:
        entry = _SCAN_STATUS.get((chat_id, topic_key))
        return dict(entry) if entry else None


# ─────────────────────────────────────────────────────────────────────────────
# SCAN RESILIENCE (SCAN FIX, step 3): single-flight registry + cancel helpers
#
#   SCAN_DEADLINE_SECONDS         wait limit for the 3 collection scans (0 = none)
#   SINGLE_FLIGHT_SCAN_ENABLED    one scan per (chat_id, topic_key)
#   SINGLE_FLIGHT_STALE_SECONDS   a registry entry older than this is ignored
#   STREAMING_SCORE_ENABLED       score batches while fetching (see get_matched_signals)
#   MONGO_FIND_MAX_TIME_MS        server-side time limit on every signals find
#
# Every flag is read LIVE through _cfg_value(); a flag that is off (or 0)
# leaves that section's behaviour exactly as it was before this step.
# ─────────────────────────────────────────────────────────────────────────────

def _apply_find_max_time(cursor, max_ms) -> None:
    """Applies `.max_time_ms()` to a pymongo cursor when max_ms > 0. pymongo's
    cursor methods mutate and return the same cursor, so the return value is
    deliberately ignored (works with any cursor-like object)."""
    try:
        if max_ms and max_ms > 0:
            fn = getattr(cursor, "max_time_ms", None)
            if callable(fn):
                fn(int(max_ms))
    except Exception as exc:  # never let a cosmetic limit break a scan
        log.warning(f"max_time_ms not applied: {exc}")


def _is_exec_timeout(exc: BaseException) -> bool:
    return type(exc).__name__ in ("ExecutionTimeout", "NetworkTimeout")


class _ScanCancelled(Exception):
    """Internal: a scan worker noticed its cancel event."""


def _mongo_score_batch() -> int:
    """Rows buffered per Mongo streaming scoring flush. An explicit SIGNAL_EMBEDDING_MONGO_SCORE_BATCH is used as given; when it
    is unset (or was invalid) the legacy effective batch applies, read at call time so a runtime change of
    SIGNAL_EMBEDDING_FETCH_BATCH is still followed: max(1, min(int(fetch or 2000), 2000))."""
    if SIGNAL_EMBEDDING_MONGO_SCORE_BATCH:
        return max(1, int(SIGNAL_EMBEDDING_MONGO_SCORE_BATCH))
    return max(1, min(int(SIGNAL_EMBEDDING_FETCH_BATCH or 2000), 2000))


# ─────────────────────────────────────────────────────────────────────────────
# (MYSQL READ) Extra signal sources read from MySQL (see mysql_signals.py).
# Only used when config.MYSQL_READ_ENABLED is on; mysql_signals (and pymysql)
# are imported lazily, so with the flag off nothing here runs or is imported.
# ─────────────────────────────────────────────────────────────────────────────
def _np_max_scores(D, Q):
    """Max cosine of each row of D against the normalized query matrix Q —
    the same NumPy math as the Mongo streamed scorer (_s_flush)."""
    D = np.asarray(D, dtype=np.float32)
    d_norms = np.linalg.norm(D, axis=1, keepdims=True)
    d_norms = np.where(d_norms == 0, 1.0, d_norms)
    D = D / d_norms
    return (D @ Q.T).max(axis=1)


def _mysql_sources_for_scan(stream_ctx) -> list:
    """MySQL sources to add to this scan ([] when the flag is off).

    Two different outcomes, on purpose (MYSQL SOURCE FAILURE FIX):
      * UNSUPPORTED MODE -> intentional skip (one warning, scan NOT failed):
        MySQL rows are scored while streaming, so they need unlimited mode +
        STREAMING_SCORE_ENABLED. In legacy pool mode or with streaming off
        MySQL is not part of the search at all — a configuration choice.
      * SOURCE CANNOT BE READ -> failure: missing pymysql, an invalid
        database name, missing required columns / table / permission, a
        time-window query on a table without a date column, connect or query
        errors. Every configured source is returned here; the read raises and
        _fetch_mysql_pool() adds the label to _fetch_failures, so the scan is
        incomplete and no watermark advances."""
    if not _cfg_value("MYSQL_READ_ENABLED", False):
        return []
    try:
        import mysql_signals  # noqa: PLC0415
    except Exception as exc:
        log.warning(f"MYSQL_READ_ENABLED is on but mysql_signals failed to import: {type(exc).__name__}")
        return []
    if stream_ctx is None:
        mysql_signals._warn_once(
            "needs_streaming",
            "MYSQL_READ_ENABLED is on but MySQL sources need unlimited mode "
            "(SIGNAL_EMBEDDING_CANDIDATE_POOL/RECENCY_POOL <= 0) with STREAMING_SCORE_ENABLED=true; "
            "MySQL sources skipped for this configuration.",
        )
        return []
    return mysql_signals.configured_sources()


class ScanHandle:
    """One request's handle on a (possibly shared) scan.

    future        concurrent.futures.Future that resolves to the scan's list
    cancel_event  threading.Event; set => the scan stops as soon as it safely can
    release()     call when this request finishes or disconnects. The scan is
                  cancelled only when NO waiter is left. Idempotent.
    """

    def __init__(self, key, entry, started_here: bool, registry_enabled: bool):
        self._key = key
        self._entry = entry
        self._registry_enabled = registry_enabled
        self._released = False
        self.started_here = started_here
        self.future = entry["future"]
        self.cancel_event = entry["cancel_event"]

    def release(self) -> None:
        with _SCAN_REGISTRY_LOCK:
            if self._released:
                return
            self._released = True
            entry = self._entry
            entry["waiters"] = max(0, entry["waiters"] - 1)
            if entry["waiters"] == 0 and not entry["future"].done():
                entry["cancel_event"].set()
                # Nobody wants this scan any more: a later request must start a
                # fresh one rather than join a cancelled scan.
                if self._registry_enabled and _SCAN_REGISTRY.get(self._key) is entry:
                    _SCAN_REGISTRY.pop(self._key, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


_SCAN_REGISTRY: dict = {}
_SCAN_REGISTRY_LOCK = threading.Lock()


def _start_scan_thread(key, entry, scan_fn, registry_enabled: bool) -> None:
    def _runner():
        fut = entry["future"]
        try:
            if not fut.set_running_or_notify_cancel():
                return
            try:
                fut.set_result(scan_fn(entry["cancel_event"]))
            except BaseException as exc:  # noqa: BLE001 - delivered through the future
                fut.set_exception(exc)
        finally:
            if registry_enabled:
                with _SCAN_REGISTRY_LOCK:
                    if _SCAN_REGISTRY.get(key) is entry:
                        _SCAN_REGISTRY.pop(key, None)

    threading.Thread(target=_runner, name="flintel-scan", daemon=True).start()


def acquire_scan(chat_id, topic_key, scan_fn) -> "ScanHandle":
    """Starts - or joins - the scan for (chat_id, topic_key).

    `scan_fn(cancel_event)` performs the scan and returns a list. If a scan for
    the same key is already running (and younger than SINGLE_FLIGHT_STALE_SECONDS
    and not cancelled) NO second scan is started: the returned handle points at
    the running scan's future/cancel_event and the waiter count goes up. The
    scan is cancelled only when the last waiter calls handle.release(). The
    registry entry is removed in a `finally` when the scan ends (result or error).

    SINGLE_FLIGHT_SCAN_ENABLED=false (or a missing chat_id/topic_key): the
    registry is not used; every call runs its own scan with its own cancel_event.

    LIMITATION: the registry is process-local (a dict + a lock). Under a
    multi-worker / multi-process uvicorn deployment each worker has its own
    registry, so two requests landing on different workers can still run two
    scans. Nothing is written to MongoDB (MONGODB3 included) for this registry."""
    use_registry = bool(_cfg_value("SINGLE_FLIGHT_SCAN_ENABLED", True)) and bool(chat_id) and bool(topic_key)
    key = (chat_id, topic_key)

    if not use_registry:
        entry = {"future": concurrent.futures.Future(), "cancel_event": threading.Event(),
                 "waiters": 1, "started": time.monotonic()}
        handle = ScanHandle(key, entry, True, False)
        _start_scan_thread(key, entry, scan_fn, False)
        return handle

    try:
        stale_s = float(_cfg_value("SINGLE_FLIGHT_STALE_SECONDS", 600) or 0)
    except (TypeError, ValueError):
        stale_s = 600.0

    with _SCAN_REGISTRY_LOCK:
        existing = _SCAN_REGISTRY.get(key)
        if existing is not None:
            fresh = (stale_s <= 0 or (time.monotonic() - existing["started"]) < stale_s)
            if (not existing["future"].done()) and (not existing["cancel_event"].is_set()) and fresh:
                existing["waiters"] += 1
                return ScanHandle(key, existing, False, True)
            _SCAN_REGISTRY.pop(key, None)      # finished / cancelled / stale -> replace
        entry = {"future": concurrent.futures.Future(), "cancel_event": threading.Event(),
                 "waiters": 1, "started": time.monotonic()}
        _SCAN_REGISTRY[key] = entry
        handle = ScanHandle(key, entry, True, True)
    _start_scan_thread(key, entry, scan_fn, True)
    return handle


def strict_wait_done(matched_count: int, target: int, waited_seconds: float,
                     scan_complete: bool, response_timeout: float = None) -> bool:
    """STRICT wait/target rule (W2 + W3). True => stop waiting and answer now.

    * target reached                                  -> done (target is an
      upper bound, never a requirement);
    * W2: the scan is complete (every cluster read OK) AND at least one
      qualifying post exists                          -> done, even below the
      target ("8 qualify -> give 8");
    * W3: STRICT_WAIT_SECONDS elapsed                 -> done (hard cap);
    * RESPONSE_TIMEOUT remains the ultimate ceiling.

    With zero qualifying posts the loop keeps waiting (up to W3) because a
    brand-new topic may still be ingested by the background service."""
    if matched_count >= (target or 0) and matched_count > 0:
        return True
    if scan_complete and matched_count > 0:
        return True
    cap = float(_cfg_value("STRICT_WAIT_SECONDS", 75))
    if response_timeout:
        cap = min(cap, float(response_timeout))
    return waited_seconds >= cap


_KNOWN_PLATFORM_WORDS = ("reddit", "twitter", "linkedin", "facebook")


def _single_platform_query(user_query, targeting_platform) -> bool:
    """True when the request is clearly about ONE platform: the dropdown picked
    a specific platform, or the query text names exactly one platform."""
    _tp = (targeting_platform or "all").strip().lower()
    if _tp not in ("", "all") and "," not in _tp:   # (PLATFORM MULTI-SELECT) several platforms != one
        return True
    q = (user_query or "").lower()
    named = [w for w in _KNOWN_PLATFORM_WORDS if re.search(rf"\b{w}\b", q)]
    return len(named) == 1


def _accepts_kw(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


# ── incremental rescan: watermark helpers ───────────────────────────────────

def _watermark_field() -> str:
    """The configured watermark field, or "_id" when left on auto."""
    return (_cfg_value("INCREMENTAL_WATERMARK_FIELD", "") or "").strip() or "_id"


def _aware_utc(dt):
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _doc_watermark(doc: dict, field: str):
    """The watermark datetime of one raw doc for `field`, or None."""
    try:
        if field == "_id":
            oid = doc.get("_id")
            gen = getattr(oid, "generation_time", None)
            return _aware_utc(gen)
        return _aware_utc(doc.get(field))
    except Exception:
        return None


def _watermark_clause(field: str, value: datetime, overlap_seconds: int) -> dict:
    """Mongo clause selecting docs newer than `value` minus the overlap."""
    start = value - timedelta(seconds=max(0, int(overlap_seconds or 0)))
    if field == "_id":
        from bson import ObjectId  # noqa: PLC0415
        return {"_id": {"$gt": ObjectId.from_datetime(start)}}
    return {field: {"$gte": start}}


def _scan_signature(keywords, match_phrases, targeting_platform, since_days,
                    user_query, strict: bool, field: str) -> str:
    """A delta is only valid for the exact query it was recorded for."""
    base = _compute_query_signature(keywords, match_phrases)
    raw = f"{base}|{targeting_platform}|{since_days}|{(user_query or '').strip().lower()}|{int(bool(strict))}|{field}"
    # (MYSQL CARRY FIX) Only when MySQL is enabled: the configured database
    # set is part of the signature, so enabling MySQL or changing
    # MYSQL_READ_DATABASES invalidates a carry recorded without those sources
    # (it forces one full scan whose carry holds the highest similarity across
    # Mongo + every MySQL source). Flag off: `raw` is unchanged, so the
    # Mongo-only signature (and watermark reuse) stays exactly as before.
    if _cfg_value("MYSQL_READ_ENABLED", False):
        raw += "|mysql:" + _mysql_signature_part()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _mysql_signature_part() -> str:
    """Order-independent, de-duplicated MYSQL_READ_DATABASES for the scan
    signature (list order and spacing do not force a rescan)."""
    raw = _cfg_value("MYSQL_READ_DATABASES", "") or ""
    names = sorted({p.strip() for p in str(raw).split(",") if p.strip()})
    return ",".join(names)


_INC_POLLS: dict = {}
_INC_POLLS_LOCK = threading.Lock()


def _next_poll_number(chat_id, topic_key) -> int:
    with _INC_POLLS_LOCK:
        if len(_INC_POLLS) > 4000:
            _INC_POLLS.clear()
        n = _INC_POLLS.get((chat_id, topic_key), 0) + 1
        _INC_POLLS[(chat_id, topic_key)] = n
        return n


LEXICAL_MAX_TERMS = 40
_LEXICAL_SEARCH_FIELDS = ("title", "post_text", "text", "body", "content", "selftext")

# (INTENT TAXONOMY) The only intent_types values accepted from the
# router — anything else is dropped rather than trusted. Mirrors the
# five categories defined in CLAUDE_ROUTER_SYSTEM_PROMPT below.
_VALID_INTENT_TYPES = (
    "active_opportunity",
    "solution_evaluation",
    "problem_signal",
    "provider_seller",
    "general_discussion",
)


def normalize_topic_key(query: str) -> str:
    """Turns a user's raw prompt into a stable cache/job key.
    e.g. "  Nike  " -> "nike", "Nike Shoes!!" -> "nike shoes"

    UNCHANGED by the keyword-generation swap: topic_key is still derived
    from the raw query string and is used only as the flintel_search_jobs
    upsert key / chat message linkage — it has nothing to do with which
    keywords get matched against flintel_signals."""
    cleaned = re.sub(r"[^a-z0-9\s]", "", query.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM TARGETING
# ─────────────────────────────────────────────────────────────────────────────

# Maps whatever label the "All Platforms" dropdown sent to a stable key.
# This ONLY tags the job — it never changes what keywords get generated,
# and it is still driven ONLY by this dropdown field, never by words in
# the user's chat message.
PLATFORM_KEY_MAP = {
    "all platforms": "all",
    "all":           "all",
    "reddit":        "reddit",
    "x / twitter":   "x_twitter",
    "x/twitter":     "x_twitter",
    "twitter":       "x_twitter",
    "x":             "x_twitter",
    "linkedin":      "linkedin",
    "facebook":      "facebook",
}


def normalize_platform(raw: str) -> str:
    """Falls back to "all" for anything unrecognized, so a missing/garbled
    dropdown value never accidentally narrows a search."""
    key = (raw or "").strip().lower()
    return PLATFORM_KEY_MAP.get(key, "all")


# (PLATFORM MULTI-SELECT) The dropdown can send several platforms at once,
# e.g. "X / Twitter, LinkedIn". One platform -> its usual single key (same as
# normalize_platform); several -> a comma list in fixed order, e.g.
# "x_twitter,linkedin"; every platform, nothing usable, or "all" -> "all".
_PLATFORM_ORDER = ("x_twitter", "reddit", "linkedin", "facebook")


def normalize_platforms(raw: str) -> str:
    picked = set()
    for part in (raw or "").split(","):
        key = PLATFORM_KEY_MAP.get(part.strip().lower())
        if key == "all":
            return "all"
        if key:
            picked.add(key)
    ordered = [p for p in _PLATFORM_ORDER if p in picked]
    if not ordered or len(ordered) == len(_PLATFORM_ORDER):
        return "all"
    return ",".join(ordered)


def supported_platforms(raw: str) -> str:
    """(COMING SOON) Request-path enforcement: the normalized selection restricted to platforms that are actually live
    (LIVE_PLATFORM_KEYS, today "reddit"). Unsupported or unknown values are dropped; "all", empty, or nothing supported
    means every supported platform. Never returns an unsupported platform."""
    live = [p for p in _PLATFORM_ORDER if p in LIVE_PLATFORM_KEYS]
    key = normalize_platforms(raw)
    picked = [] if key == "all" else [k for k in key.split(",") if k in live]
    return ",".join(picked or live)


def unsupported_requested(raw) -> tuple:
    """(COMING SOON) What the request explicitly asked for that is not live: (labels, names_a_live_source). Only platforms
    the request names count; "All Platforms", empty and unknown values never produce a label. names_a_live_source is True
    when the request names a live platform or means "all", so an unsupported-only request is (labels, False)."""
    labels, live = [], False
    for part in (raw or "").split(","):
        key = PLATFORM_KEY_MAP.get(part.strip().lower())
        if key == "all" or key in LIVE_PLATFORM_KEYS:
            live = True
        elif key in _PLATFORM_LABELS and _PLATFORM_LABELS[key] not in labels:
            labels.append(_PLATFORM_LABELS[key])
    return labels, live


# (PLATFORM PERSISTENCE) The platform dropdown's display value for a stored
# selection — only ever built from known labels (never echoes user input).
_PLATFORM_LABELS = {k: p.label for k, p in platforms.REGISTRY.items()}      # (PLATFORM REGISTRY)


def platform_display_value(key_or_raw) -> str:
    """"Reddit" / "X / Twitter, LinkedIn" / "All Platforms" for a stored key
    ("reddit", "x_twitter,linkedin", "all") or a raw dropdown value."""
    raw = key_or_raw if isinstance(key_or_raw, str) else ""
    keys = [k for k in (raw.split(",") if raw else []) if k.strip() in _PLATFORM_LABELS]
    key = ",".join(k.strip() for k in keys) if keys and len(keys) == len(raw.split(",")) else normalize_platforms(raw)
    if not key or key == "all":
        return "All Platforms"
    return ", ".join(_PLATFORM_LABELS[k] for k in key.split(",") if k in _PLATFORM_LABELS) or "All Platforms"


# ─────────────────────────────────────────────────────────────────────────────
# (NO-RESULTS UI) Facts for the structured "nothing qualifying found" answer,
# filled in by Flintel — never by the model: what was searched (the message's
# own query, platforms and time window), next steps limited to what the app
# can actually do, and a suggested search built from the user's own request
# or the terms this search really used. Platforms that are only "Coming soon"
# in the selector are reported as not collected yet, never as searched.
# ─────────────────────────────────────────────────────────────────────────────
LIVE_PLATFORM_KEYS = platforms.enabled_keys()      # (PLATFORM REGISTRY) today: ("reddit",)
_BUYER_QUERY_RE = re.compile(r"\b(?:buy|buying|buyers?|purchas\w*|leads?|customers?|clients?|prospects?|looking for|"
                             r"need(?:s|ing)?|pay(?:ing)? for|hire|hiring|license|upgrade|demand)\b", re.IGNORECASE)


def enrich_no_results_answer(answer_text, query, targeting_platform=None, time_range=None,
                             time_window_days=None, keywords=None):
    """Adds Flintel-verified fields to a "no_results" answer (see above).
    Any other answer, unparseable text or a cut-off ("incomplete") answer is
    returned unchanged. Never raises."""
    try:
        data = json.loads((answer_text or "").strip())
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict) or data.get("format") != "no_results" or data.get("answer_status") == "incomplete":
        return answer_text
    try:
        reason = data.get("no_results_reason") if data.get("no_results_reason") in ("empty", "unverified", "error") else "empty"
        key = normalize_platforms(targeting_platform or "all")
        keys = list(LIVE_PLATFORM_KEYS) if key == "all" else [k for k in key.split(",") if k]
        searched = [_PLATFORM_LABELS[k] for k in keys if k in LIVE_PLATFORM_KEYS and k in _PLATFORM_LABELS]
        not_collected = [_PLATFORM_LABELS[k] for k in keys if k not in LIVE_PLATFORM_KEYS and k in _PLATFORM_LABELS]
        tr = time_range if isinstance(time_range, timerange.TimeRange) else timerange.range_from_doc(time_range)
        days = time_window_days if isinstance(time_window_days, int) and time_window_days > 0 else None
        if tr is not None:
            window = tr.describe()
        elif days:
            window = f"the last {days} day{'s' if days != 1 else ''}"
        else:
            window = "all posts Flintel has collected so far (no time limit)"
        data["searched"] = {"query": query or "", "platforms": searched, "time_window": window,
                            "not_collected": not_collected}
        data["no_results_reason"] = reason
        if reason == "error":
            data["headline"] = "The answer couldn't be completed"
        elif reason == "unverified":
            data["headline"] = "No verifiable posts to show"
        elif data.get("near_match_confidence") == "high":
            data["headline"] = "No exact match — closest posts below"
        else:
            data["headline"] = ("No verified purchase intent found" if _BUYER_QUERY_RE.search(query or "")
                                else "No qualifying posts found")
        alternative = None
        for action in data.get("suggested_actions") or []:
            if (isinstance(action, dict) and action.get("type") == "try_nearest_alternative"
                    and isinstance(action.get("suggestion"), str) and action["suggestion"].strip()):
                alternative = " ".join(action["suggestion"].split())[:80]
                break
        steps = []
        if reason == "error":
            steps.append("Run the same search again — the search ran, but writing the answer failed.")
        else:
            if not_collected:
                verb = "is" if len(not_collected) == 1 else "are"
                steps.append(f"Search Reddit instead — {', '.join(not_collected)} {verb} not collected yet (coming soon).")
            if tr is not None or days:
                steps.append("Search a wider time window, for example the last 30 days.")
            steps.append("Try broader or different wording for the same need.")
            if alternative:
                steps.append(f"Try a close alternative: {alternative}.")
        data["next_steps"] = steps
        suggested = None
        if reason != "error":
            base = " ".join((query or "").split())
            if tr is not None and tr.phrase:
                base = re.sub(re.escape(tr.phrase), " ", base, flags=re.IGNORECASE)
                base = " ".join(re.sub(r"\b(?:from|in|during|for|over|within)\s*$", "", base.strip(" ,.?!")).split())
            kw = [k.strip() for k in (keywords or []) if isinstance(k, str) and k.strip()][:3]
            if alternative:
                suggested = f"Find Reddit posts about {alternative}"
            elif (tr is not None or days) and base:
                suggested = f"{base} from the last 30 days"
            elif len(kw) >= 2:
                suggested = "Find Reddit posts about " + ", ".join(kw[:-1]) + f" or {kw[-1]}"
        if suggested and suggested.strip().lower() != (query or "").strip().lower():
            data["suggested_search"] = suggested[:200]
        else:
            data.pop("suggested_search", None)
        return json.dumps(data, ensure_ascii=False)
    except Exception as exc:
        log.warning(f"No-results enrichment failed (answer left unchanged): {exc}")
        return answer_text


# ─────────────────────────────────────────────────────────────────────────────
# (FRESHNESS DISCLOSURE, LI-02 M2) Server-built, factual statement about an
# answer's evidence: stored post dates, the window searched, whether a search
# really ran for THIS request or saved results were reused, and what is not
# known. Nothing here changes which posts are returned or in what order.
#
# Four different times are kept apart and never merged:
#   * stored post date      -> each post's "published_at" / "created_utc" as
#                              stored. It is NOT verified as the original
#                              publication time (a MySQL source may fall back
#                              to a row-creation column), so it is only ever
#                              called "stored post date".
#   * saved-results update  -> the cache document's "updated_at": when the
#                              saved results last CHANGED, not when they were
#                              last checked.
#   * search activity       -> a scan of stored posts during THIS request,
#                              recorded by get_evidence_with_topup(). It is a
#                              read of stored data, never a collection.
#   * collection time       -> always reported as unknown.
# Flag: FRESHNESS_DISCLOSURE_ENABLED, read live, default OFF in code. Off =
# no provenance is recorded and the answer text is returned untouched.
# ─────────────────────────────────────────────────────────────────────────────

_FD_VERSION = 1
_FD_SKIP_FORMATS = {"disallowed", "not_available"}      # not about the posts found
_FD_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def freshness_enabled() -> bool:
    """True only when FRESHNESS_DISCLOSURE_ENABLED is the boolean True."""
    return _cfg_value("FRESHNESS_DISCLOSURE_ENABLED", False) is True


def _fd_utc(value):
    try:
        return timerange.to_utc(value)
    except Exception:
        return None


def _fd_is_naive(value) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).tzinfo is None
        except ValueError:
            return False
    return False


def _fd_fmt(dt, with_time: bool = False) -> str:
    text = f"{dt.day} {_FD_MONTHS[dt.month - 1]} {dt.year}"
    return f"{text}, {dt.hour:02d}:{dt.minute:02d} UTC" if with_time else text


def _fd_age(seconds) -> str:
    """Coarse age, e.g. "under an hour", "about 3 days"."""
    s = max(0, int(seconds))
    if s < 3600:
        return "under an hour"
    if s < 2 * 86400:
        h = s // 3600
        return f"about {h} hour{'s' if h != 1 else ''}"
    if s < 60 * 86400:
        return f"about {s // 86400} days"
    if s < 2 * 365 * 86400:
        return f"about {s // (30 * 86400)} months"
    return f"about {s // (365 * 86400)} years"


def summarize_publication_times(posts, reference_utc=None) -> dict:
    """Counts and newest/oldest STORED POST DATE of `posts`, relative to one
    reference instant. A post's date is its "published_at", else "created_utc".
    Missing / unreadable / future dates are counted and never used for newest
    or oldest; Google discovery stubs ("google_rank" key) and repeated URLs are
    not counted as evidence posts. Naive datetimes are read as UTC. Never raises."""
    ref = _fd_utc(reference_utc) if reference_utc is not None else None
    if ref is None:
        ref = datetime.now(timezone.utc)
    out = {"evidence_posts": 0, "with_date": 0, "no_date": 0, "invalid_date": 0, "future_date": 0,
           "naive_assumed_utc": 0, "discovery_only": 0,
           "newest": None, "oldest": None, "newest_age_seconds": None, "oldest_age_seconds": None}
    seen = set()
    dated = []
    for post in posts or []:
        if not isinstance(post, dict):
            continue
        url = post.get("post_url")
        if url:
            if url in seen:
                continue
            seen.add(url)
        if "google_rank" in post:
            out["discovery_only"] += 1
            continue
        out["evidence_posts"] += 1
        raws = [post.get(k) for k in ("published_at", "created_utc")
                if post.get(k) is not None and not (isinstance(post.get(k), str) and not post.get(k).strip())]
        if not raws:
            out["no_date"] += 1
            continue
        when = None
        for raw in raws:
            when = _fd_utc(raw)
            if when is not None:
                if _fd_is_naive(raw):
                    out["naive_assumed_utc"] += 1
                break
        if when is None:
            out["invalid_date"] += 1
        elif when > ref:
            out["future_date"] += 1
        else:
            out["with_date"] += 1
            dated.append(when)
    if dated:
        newest, oldest = max(dated), min(dated)
        out["newest"], out["oldest"] = newest.isoformat(), oldest.isoformat()
        out["newest_age_seconds"] = int((ref - newest).total_seconds())
        out["oldest_age_seconds"] = int((ref - oldest).total_seconds())
    return out


def describe_search_window(time_range=None, time_window_days=None) -> str:
    """The window wording enrich_no_results_answer() already uses."""
    tr = time_range if isinstance(time_range, timerange.TimeRange) else timerange.range_from_doc(time_range)
    days = time_window_days if isinstance(time_window_days, int) and time_window_days > 0 else None
    if tr is not None:
        return tr.describe()
    if days:
        return f"the last {days} day{'s' if days != 1 else ''}"
    return "all posts Flintel has collected so far (no time limit)"


def _prov_marker(chat_id, topic_key):
    entry = get_scan_status(chat_id, topic_key)
    return None if entry is None else entry.get("at")


def _prov_urls(out, posts, already=None):
    """(FRESHNESS) URLs of `posts` that are not in `already`; None when tracking is off or anything is odd."""
    if out is None:
        return None
    try:
        known = {p.get("post_url") for p in (already or []) if isinstance(p, dict) and p.get("post_url")}
        return [u for u in (p.get("post_url") for p in (posts or []) if isinstance(p, dict)) if u and u not in known]
    except Exception:
        return None


def _prov_note(out, kind, chat_id=None, topic_key=None, marker_before=None,
               saved_updated_at=None, saved_count=0, range_stats=None, searchable=True, cancelled=False,
               scan_urls=None) -> None:
    """Records what get_evidence_with_topup() just did into `out` (a per-request
    dict). kind: "saved_only" | "scan" | "range". `searchable=False` means the
    matcher was given nothing to search for and returned without searching: that
    is recorded as "scan_skipped", never as a search. `cancelled=True` means the
    caller had already abandoned this scan (deadline / disconnect) - that is only
    a flag: whether its results were used is decided later, from `scan_urls` (the
    post URLs this scan returned that were not already saved) against the evidence
    the answer was really built from. A scan counts as reporting its own status only
    when a status was registered during THIS call by THIS thread, and that status
    says scoring ran; anything else is recorded as "scan_unconfirmed".
    The whole record is applied with ONE dict.update(), so a reader never sees
    half of it. Write-only, never raises, no effect when `out` is None."""
    if out is None:
        return
    try:
        if kind == "saved_only":
            if not out.get("scan_count") and not out.get("saved_used"):
                out.update(saved_used=True, saved_updated_at=saved_updated_at, saved_count=saved_count)
            return
        rec = {}
        if not out.get("scan_count") and not out.get("saved_used") and saved_count > 0:
            rec.update(saved_used=True, saved_updated_at=saved_updated_at, saved_count=saved_count)
        if not searchable:
            rec["scan_skipped"] = True
            out.update(rec)
            return
        after = get_scan_status(chat_id, topic_key)
        mine = (after is not None and after.get("thread") == threading.get_ident()
                and (marker_before is None or after.get("at") != marker_before))
        rec["scan_count"] = out.get("scan_count", 0) + 1
        rec["search_time"] = datetime.now(timezone.utc)
        if range_stats is not None:
            rec["range"] = dict(range_stats)
        if cancelled:
            rec["scan_cancelled"] = True
        if scan_urls is not None:
            rec["scan_urls"] = list(dict.fromkeys(list(out.get("scan_urls") or []) + list(scan_urls)))[:2000]
        if not mine:
            rec["scan_unconfirmed"] = True       # this call never reported its own status
        elif after.get("scored") is False:
            rec["scan_unconfirmed"] = True       # a status was registered, but nothing was scored
            rec["scan_unscored"] = True
        elif not after.get("complete"):
            rec["scan_incomplete"] = True
        out.update(rec)
    except Exception as exc:
        log.warning(f"Freshness provenance note failed (ignored): {exc}")


def build_data_freshness(posts, reference_utc=None, *, time_range=None, time_window_days=None,
                         provenance=None, matched_count=None, evidence_limit=None,
                         timed_out: bool = False) -> dict:
    """The "data_freshness" object for one answer, including a plain-text
    "statement". Pure given its inputs. Never raises.

    "search_status" says what is known about THIS request's search:
      confirmed    a scan ran and reported its own status -> its time is given
      unconfirmed  a scan was attempted but never reported its own status, or reported that nothing was
                   scored -> NO time, qualified wording
      cancelled    the caller abandoned the scan (deadline / disconnect) -> its results are not included
      not_run      the matcher was given no search terms and did not search (or saved results were reused)
      not_recorded nothing was recorded (also: another request's scan, another worker)
    `provenance` is copied ONCE up front, so a scan thread writing to it meanwhile cannot produce a mixture."""
    ref = _fd_utc(reference_utc) if reference_utc is not None else None
    if ref is None:
        ref = datetime.now(timezone.utc)
    try:
        prov = dict(provenance) if isinstance(provenance, dict) else {}       # one snapshot, read only from here on
        if isinstance(prov.get("range"), dict):
            prov["range"] = dict(prov["range"])
    except Exception:
        prov = {}
    summary = summarize_publication_times(posts, ref)
    n = summary["evidence_posts"]
    window = describe_search_window(time_range, time_window_days)
    attempted = bool(prov.get("scan_count"))
    # A cancel flag only says the caller had abandoned the scan by the time it returned. What matters is whether the
    # scan finished cleanly (own status, complete, scored) and whether its results are in the evidence used here.
    cancel_flag = attempted and bool(prov.get("scan_cancelled"))
    scan_urls = prov.get("scan_urls") if isinstance(prov.get("scan_urls"), list) else None
    evidence_urls = {p.get("post_url") for p in (posts or []) if isinstance(p, dict) and "google_rank" not in p
                     and p.get("post_url")}
    scan_used = None if scan_urls is None else bool(evidence_urls.intersection(scan_urls))
    scan_clean = attempted and not prov.get("scan_incomplete") and not prov.get("scan_unconfirmed")
    cancelled, late_results, finished_before_cancel = cancel_flag, False, False
    if cancel_flag and scan_clean and scan_urls is not None:
        cancelled = False
        if scan_used or not scan_urls:
            finished_before_cancel = True      # it ran to its end and its results (if any) are in the evidence
        else:
            late_results = True                # it ran to its end, but none of its posts are in the evidence used
    results_included = (scan_used if scan_urls else None) if (cancel_flag and scan_urls is not None) else None
    unconfirmed = attempted and not cancelled and bool(prov.get("scan_unconfirmed"))
    confirmed = attempted and not cancelled and not unconfirmed
    saved_used = bool(prov.get("saved_used"))
    not_run = bool(prov.get("scan_skipped")) and not attempted
    rng = prov.get("range") if isinstance(prov.get("range"), dict) else None
    if cancelled:
        mode = "scan_cancelled"
    elif late_results:
        mode = "scan_late"
    elif rng is not None and attempted:
        mode = "range_scan"
    elif attempted and saved_used:
        mode = "saved_plus_scan"
    elif attempted:
        mode = "scanned"
    elif saved_used:
        mode = "saved_only"
    elif not_run:
        mode = "no_search_run"
    else:
        mode = "unknown"
    search_status = ("confirmed" if confirmed else "unconfirmed" if unconfirmed else "cancelled" if cancelled
                     else "not_run" if (not_run or mode == "saved_only") else "not_recorded")
    search_time = _fd_utc(prov.get("search_time")) if confirmed else None
    saved_at = _fd_utc(prov.get("saved_updated_at")) if saved_used else None

    scan_complete = None
    if attempted:
        scan_complete = False if prov.get("scan_incomplete") else (None if prov.get("scan_unconfirmed") else True)
    partial = None
    if late_results:
        partial = "late_results"
    elif finished_before_cancel:
        partial = None                         # the search finished and its posts are in the evidence
    elif timed_out:
        partial = "time_limit"
    elif cancelled:
        partial = "cancelled"
    elif scan_complete is False:
        partial = "source_unreadable"
    elif attempted and scan_complete is None:
        partial = "unconfirmed"

    lines = []
    when = f" on {_fd_fmt(search_time, True)}" if search_time else ""
    no_time = "could not be confirmed, so no search time is given"
    saved_txt = f"last updated {_fd_fmt(saved_at, True)}" if saved_at else "update time unknown"
    consider = f"{n} stored post{'s' if n != 1 else ''} {'was' if n == 1 else 'were'} considered"
    if mode == "saved_only":
        lines.append(f"These results were reused from an earlier search in this chat (saved results {saved_txt}). "
                     f"No new search of stored posts was run for this answer, so posts stored since then are not included. "
                     f"{consider}.")
    elif mode == "saved_plus_scan" and confirmed:
        lines.append(f"Saved results from an earlier search in this chat ({saved_txt}) were extended by searching "
                     f"stored posts again{when}. {consider}.")
    elif mode == "saved_plus_scan":
        lines.append(f"Saved results from an earlier search in this chat ({saved_txt}) were used. A search of stored "
                     f"posts was attempted for this answer but {no_time}, and it is not known whether it added "
                     f"posts. {consider}.")
    elif mode == "scan_late" and saved_used:
        lines.append(f"Saved results from an earlier search in this chat ({saved_txt}) were used. A new search of "
                     f"stored posts finished{when}, but the posts it found are not included in this answer. {consider}.")
    elif mode == "scan_late":
        lines.append(f"A search of stored posts finished{when}, but the posts it found are not included in this "
                     f"answer. {consider}.")
    elif mode == "scan_cancelled" and results_included and saved_used:
        lines.append(f"Saved results from an earlier search in this chat ({saved_txt}) were used. A new search of "
                     f"stored posts was started for this answer but was stopped before it finished; the posts it had "
                     f"found by then are included. {consider}.")
    elif mode == "scan_cancelled" and results_included:
        lines.append(f"A search of stored posts was started for this answer but was stopped before it finished; the "
                     f"posts it had found by then are included. {consider}.")
    elif mode == "scan_cancelled" and saved_used:
        lines.append(f"Saved results from an earlier search in this chat ({saved_txt}) were used. A new search of "
                     f"stored posts was started for this answer but was stopped before it finished, so its results "
                     f"are not included and it did not extend the saved results. {consider}.")
    elif mode == "scan_cancelled":
        lines.append(f"A search of stored posts was started for this answer but was stopped before it finished, so "
                     f"its results are not included. {consider}.")
    elif mode == "range_scan" and confirmed:
        lines.append(f"Stored posts were searched{when}, limited to {window} by stored post date. {consider}.")
    elif mode == "range_scan":
        lines.append(f"A search of stored posts limited to {window} by stored post date was attempted but {no_time}. "
                     f"{consider}.")
    elif mode == "scanned" and confirmed:
        lines.append(f"Stored posts were searched{when}. {consider}.")
    elif mode == "scanned":
        lines.append(f"A search of stored posts was attempted for this answer but {no_time}. {consider}.")
    elif mode == "no_search_run":
        lines.append(f"No search of stored posts was run for this answer because the request had no search terms. "
                     f"{consider}.")
    else:
        lines.append(f"How these results were gathered is not recorded for this answer. {consider}.")
    if rng is not None and attempted and not cancelled and (rng.get("outside") or rng.get("no_timestamp")):
        lines.append(f"Left out by date: {int(rng.get('outside') or 0)} outside the range and "
                     f"{int(rng.get('no_timestamp') or 0)} without a stored date.")
    if n == 0:
        if confirmed or mode == "saved_only":
            lines.append("No stored posts matched this search. Flintel searches only posts it has already stored, very "
                         "recent posts may not be stored yet, and this does not show that nothing was posted. A temporary "
                         "error can also produce an empty result.")
        else:
            lines.append("No stored posts are available for this answer. This does not show that nothing was posted, "
                         "and a temporary error can also produce an empty result.")
    elif summary["with_date"]:
        newest = _fd_utc(summary["newest"])
        oldest = _fd_utc(summary["oldest"])
        text = (f"Stored post dates: newest {_fd_fmt(newest)} ({_fd_age(summary['newest_age_seconds'])} before this answer), "
                f"oldest {_fd_fmt(oldest)}.")
        lines.append(text)
    else:
        lines.append("None of these posts has a usable stored post date, so their age cannot be stated.")
    undated = summary["no_date"] + summary["invalid_date"] + summary["future_date"]
    if n and undated:
        bits = []
        if summary["no_date"]:
            bits.append(f"{summary['no_date']} without a date")
        if summary["invalid_date"]:
            bits.append(f"{summary['invalid_date']} with an unreadable date")
        if summary["future_date"]:
            bits.append(f"{summary['future_date']} dated after this answer")
        if summary["with_date"]:
            lines.append(f"Not counted in the date range: {', '.join(bits)}.")
    if mode != "range_scan":
        lines.append(f"Window: {window}.")
    if partial == "time_limit":
        lines.append("Partial results: the search stopped at its time limit before every source was checked, "
                     "so posts may be missing.")
    elif partial == "late_results":
        lines.append("Partial results: the finished search found posts that this answer does not include.")
    elif partial == "cancelled":
        lines.append("Partial results: the search was stopped before it finished, so posts may be missing.")
    elif partial == "source_unreadable":
        lines.append("Partial results: one or more sources could not be read, so posts may be missing.")
    elif partial == "unconfirmed":
        lines.append("Because the search could not be confirmed, posts may be missing.")
    limit_reached = bool(isinstance(evidence_limit, int) and evidence_limit > 0
                         and isinstance(matched_count, int) and matched_count >= evidence_limit)
    if limit_reached:
        lines.append(f"Showing up to {evidence_limit} matching posts; more may exist.")
    lines.append("Collection time is unknown, and this is not a complete view of Reddit.")

    return {
        "version": _FD_VERSION,
        "reference_utc": ref.isoformat(),
        "mode": mode,
        "search_status": search_status,
        "searched_this_request": confirmed,
        "scan_results_included": results_included,
        "finished_before_cancel": finished_before_cancel,
        "search_time_utc": search_time.isoformat() if search_time else None,
        "saved_results_updated_at": saved_at.isoformat() if saved_at else None,
        "window": window,
        "post_dates": {"basis": "stored_post_date", "newest": summary["newest"], "oldest": summary["oldest"]},
        "counts": {k: summary[k] for k in ("evidence_posts", "with_date", "no_date", "invalid_date",
                                           "future_date", "discovery_only")},
        "partial": partial,
        "evidence_limit_reached": limit_reached,
        "collection_time": "unknown",
        "statement": " ".join(lines),
    }


def enrich_freshness_answer(answer_text, posts, *, time_range=None, time_window_days=None, provenance=None,
                            matched_count=None, evidence_limit=None, timed_out: bool = False,
                            reference_utc=None):
    """Adds "data_freshness" to a structured answer. Returns `answer_text`
    unchanged when the flag is off, `provenance` is None (the caller did not
    track it), the text is not a structured answer, the answer is cut off, or
    its format is not about the posts found. Never raises."""
    if not freshness_enabled() or provenance is None:
        return answer_text
    try:
        data = json.loads((answer_text or "").strip())
    except (ValueError, TypeError, AttributeError):
        return answer_text
    if (not isinstance(data, dict) or not isinstance(data.get("format"), str)
            or data.get("answer_status") == "incomplete" or data["format"] in _FD_SKIP_FORMATS
            or "data_freshness" in data):
        return answer_text
    try:
        data["data_freshness"] = build_data_freshness(
            posts, reference_utc, time_range=time_range, time_window_days=time_window_days,
            provenance=provenance, matched_count=matched_count, evidence_limit=evidence_limit,
            timed_out=timed_out)
        return json.dumps(data, ensure_ascii=False)
    except Exception as exc:
        log.warning(f"Freshness disclosure failed (answer left unchanged): {exc}")
        return answer_text


# ─────────────────────────────────────────────────────────────────────────────
# FUZZY KEYWORD GENERATION — plain Python, template-based, no Claude call
#
# (KEYWORD-GENERATION SWAP) This function and KEYWORD_TEMPLATES are KEPT
# COMPLETELY UNCHANGED from v1, but they are NO LONGER the primary path
# for a "search" message. They are now used ONLY as a safety-net FALLBACK
# — see classify_and_maybe_chat() / the /search route below — for the
# rare case where the Claude routing call fails outright, or succeeds but
# doesn't return a usable "keywords" list for a "search" intent. This
# guarantees a search can never end up with zero keywords, exactly the
# same safety philosophy already used everywhere else in this file.
# ─────────────────────────────────────────────────────────────────────────────

KEYWORD_TEMPLATES = [
    "{q}",
    "{q} review",
    "{q} reviews",
    "{q} honest review",
    "{q} complaint",
    "{q} complaints",
    "{q} problem",
    "{q} problems",
    "{q} issue",
    "{q} issues",
    "{q} experience",
    "{q} experiences",
    "is {q} worth it",
    "is {q} good",
    "{q} worth it",
    "switched from {q}",
    "switched to {q}",
    "alternative to {q}",
    "{q} vs",
    "{q} pricing",
    "{q} customer service",
    "{q} support",
    "{q} quality",
    "{q} scam",
    "{q} recommend",
    "thoughts on {q}",
    "anyone using {q}",
    "anyone tried {q}",
]


def generate_fuzzy_keywords(query: str) -> list:
    """Builds a list of search phrases around the user's query using fixed
    templates. Deterministic, fast, and needs no external API call.

    (KEYWORD-GENERATION SWAP) Still here, byte-for-byte unchanged — now
    called only as the fallback path described above, not on every
    search."""
    q = query.strip()
    if not q:
        return []

    seen = set()
    keywords = []
    for template in KEYWORD_TEMPLATES:
        phrase = template.format(q=q).strip()
        key = phrase.lower()
        if key not in seen:
            seen.add(key)
            keywords.append(phrase)
        if len(keywords) >= MAX_KEYWORDS:
            break

    return keywords


# ─────────────────────────────────────────────────────────────────────────────
# JOB QUEUE — patch into the SAME MongoDB Background Service #1 reads
# ─────────────────────────────────────────────────────────────────────────────

def enqueue_search_job(topic_key: str, keywords: list, targeting_platform: str, credit_request_id: str = None,
                       attribute: bool = False):
    """Upserts a job by topic_key. Re-searching the same topic simply
    resets it to pending instead of creating a duplicate job.

    UNCHANGED by the keyword-generation swap: this function has no idea
    (and doesn't need to know) whether `keywords` came from Claude's
    router call or the generate_fuzzy_keywords() fallback — it just
    stores whatever list it's handed, exactly as before. Also UNCHANGED
    by the TIME-WINDOW FEATURE: a time window only ever narrows which
    ALREADY-COLLECTED signals are matched (see get_matched_signals()
    below) — it has no bearing on what Background Service #1 goes out
    and fetches, so it is never stored on the job document.

    (SEARCH CREDITS) With `credit_request_id` the UpdateResult is returned, so
    the caller can tell its own enqueue was applied. Only with `attribute=True`
    (JOB_ATTRIBUTION_MODE=field) does the SAME upsert also push the id onto
    `credit_request_ids` (last JOB_ATTRIBUTION_CAP ids kept) — bounded
    evidence for recovery, not an idempotency guarantee. Compatibility of that
    field with the external collector (not in this repository) is UNVERIFIED.
    If the collection's validator definitively rejects the attribution write
    (WriteError code 121, see _is_definitive_validation_rejection), it is
    retried ONCE without the field and attribution is switched off for this
    process. Any other error — timeouts, network errors, write-concern
    errors — propagates unchanged and is never retried here. Without
    `credit_request_id` the update, the log line and the return value (None)
    are exactly as before."""
    update = {"$set": {
        "topic_key":          topic_key,
        "keywords":           keywords,
        # "all" | "reddit" | "x_twitter" | "linkedin" | "facebook". (PLATFORM MULTI-SELECT) a
        # multi-platform pick is fetched as "all"; matching narrows it to the chosen platforms.
        "targeting_platform": "all" if "," in (targeting_platform or "") else targeting_platform,
        "status":             "pending",
        "requested_at":       datetime.now(timezone.utc),
        "started_at":         None,
        "completed_at":       None,
        "matched_count":      0,
        "error":              None,
        "time_range":         None,   # (DATE/TIME) see record_job_time_range()
    }}
    attributed = bool(credit_request_id and attribute and not _JOB_ATTRIBUTION_REJECTED["value"])
    if attributed:
        import config as _credit_config   # read at call time (tests patch config)
        cap = int(getattr(_credit_config, "JOB_ATTRIBUTION_CAP", 50) or 50)
        update["$push"] = {"credit_request_ids": {"$each": [credit_request_id], "$slice": -cap}}
    try:
        result = jobs_collection.update_one({"topic_key": topic_key}, update, upsert=True)
    except Exception as exc:
        if not (attributed and _is_definitive_validation_rejection(exc)):
            raise   # ambiguous or unrelated: never retried here
        # The server evaluated the attribution write and refused it, so nothing
        # was applied. Defence in depth: confirm this request's id is not on the
        # job before sending anything else; if that cannot be confirmed, the
        # original error propagates (the caller holds the charge, never refunds).
        try:
            landed = jobs_collection.find_one({"topic_key": topic_key, "credit_request_ids": credit_request_id},
                                              {"_id": 1}) is not None
        except Exception:
            raise exc
        if landed:
            raise exc
        _JOB_ATTRIBUTION_REJECTED["value"] = True
        log.error("Job attribution rejected by flintel_search_jobs validation (code 121); attribution "
                  "disabled for this process — set JOB_ATTRIBUTION_MODE=off.")
        result = jobs_collection.update_one({"topic_key": topic_key},
                                            {k: v for k, v in update.items() if k != "$push"}, upsert=True)
    log.info(
        f"Job queued | topic_key={topic_key} | keywords={len(keywords)} "
        f"| targeting_platform={targeting_platform}"
        + (f" | credit_request_id={credit_request_id}" if credit_request_id else "")
    )
    return result if credit_request_id else None



# (SEARCH CREDITS) Process-local: set after the first definitive validation
# rejection of an attribution write; reset only by a process restart.
_JOB_ATTRIBUTION_REJECTED = {"value": False}


def _is_definitive_validation_rejection(exc) -> bool:
    """True only for a single-document WriteError with code 121
    (DocumentValidationFailure): the server evaluated the write and refused
    it, so it was not applied — also under retryable writes, where an
    already-applied first attempt would return its recorded success, not 121.
    Never for write-concern errors (the write may have been applied),
    timeouts, network errors, or any other code."""
    try:
        from pymongo.errors import WriteConcernError, WriteError
    except Exception:
        return False
    return (isinstance(exc, WriteError) and not isinstance(exc, WriteConcernError)
            and getattr(exc, "code", None) == 121)


def record_job_time_range(topic_key: str, time_range) -> None:
    """(DATE/TIME) Best-effort: puts the requested publication range on the
    search job so the collecting service CAN fetch for that window. The
    collector is a separate service (not in this repository); whether it
    honours the field cannot be verified here. Matching never depends on it:
    results are always filtered by their own publication time."""
    tr = time_range if isinstance(time_range, timerange.TimeRange) else timerange.range_from_doc(time_range)
    if tr is None:
        return
    try:
        jobs_collection.update_one({"topic_key": topic_key},
                                   {"$set": {"time_range": {"start": tr.start, "end": tr.end, "tz": tr.tz_name}}})
    except Exception as exc:
        log.warning(f"Saving the job time range failed for topic_key={topic_key}: {exc}")


def build_time_range_note(time_range) -> str:
    """(DATE/TIME) Analysis instructions for a message with an explicit range."""
    tr = time_range if isinstance(time_range, timerange.TimeRange) else timerange.range_from_doc(time_range)
    if tr is None:
        return ""
    lines = [
        "REQUESTED TIME RANGE (from the user's own words):",
        f"- Interpreted as: {tr.describe()}.",
    ]
    if tr.note:
        lines.append(f"- {tr.note}")
    lines += [
        "- Every post given to you was published inside this range (checked against each post's own "
        "publication time). Posts with no known publication time were left out.",
        "- State this interpreted range near the start of your answer, in the user's timezone.",
        "- If there are no posts or very few, say so plainly for this range. Do not imply a wider period was "
        "searched and do not add posts from outside it. You may OFFER a wider range as a separate option.",
        "- Flintel searches posts its collector has already stored; very recent posts (the last few minutes) may "
        "not be collected yet, so an empty very-recent range is not proof that nothing was posted.",
    ]
    return "\n".join(lines)

def get_job(topic_key: str):
    return jobs_collection.find_one({"topic_key": topic_key}, {"_id": 0})


def get_signal_count(topic_key: str) -> int:
    return signals_collection.count_documents({"topic_key": topic_key})


def get_signals(topic_key: str, limit: int = 25):
    return list(
        signals_collection.find({"topic_key": topic_key}, {"_id": 0})
        .sort("created_utc", -1)
        .limit(limit)
    )


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL MATCHING — pull post_text + post_url for signals whose search
# keyword matches one of the keywords generated for this job, OR (v7)
# whose title/post_text itself contains one of those keywords. This is
# exactly what still powers the post cards (title + post_url, as-is).
# Claude (below) only ever sees title + post_text from whatever this
# returns — never post_url, never platform.
#
# COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP: everything in this
# section works purely off whatever `keywords` list it's handed — it has
# no idea, and doesn't care, whether those keywords came from Claude's
# router call or the old fuzzy-template fallback.
#
# `flintel_signals` docs may use slightly different field names depending
# on how Background Service #1 writes them, so this reads a small list of
# likely candidates for each field instead of hard-coding a single name.
# Adjust FIELD candidates below if your signals schema differs.
#
# (EMBEDDING-BASED MATCHING CHANGE) The helper functions in this section
# (_signal_keyword_matches, _text_matches_keyword, _text_matches_any_
# phrase, _phrase_matches_text) are left fully intact below — they are
# simply no longer called from inside get_matched_signals()'s own
# non-unfiltered matching decision (see that function's own updated
# docstring). Harmless dead code, kept for now rather than deleted.
# ─────────────────────────────────────────────────────────────────────────────

_KEYWORD_FIELD_CANDIDATES  = ["search_keyword", "keyword", "matched_keyword", "query_keyword", "keywords"]
_TITLE_FIELD_CANDIDATES    = ["title", "post_title", "headline"]
_TEXT_FIELD_CANDIDATES     = ["post_text", "text", "body", "content", "selftext"]
_URL_FIELD_CANDIDATES      = ["post_url", "url", "link", "permalink"]
_PLATFORM_FIELD_CANDIDATES = ["platform", "source", "source_platform"]

# Maps a job's targeting_platform ("all" | "reddit" | "x_twitter" |
# "linkedin" | "facebook" — see normalize_platform()) to the value(s) a
# signal doc's own platform field may use. "all" has no entry here since
# it means "no filter" and is handled separately.
_PLATFORM_DOC_VALUES = {
    "reddit":    ["reddit"],
    "x_twitter": ["twitter", "x", "x_twitter", "x/twitter"],
    "linkedin":  ["linkedin"],
    "facebook":  ["facebook"],
}


def _first_present(doc: dict, candidates: list):
    for field in candidates:
        value = doc.get(field)
        if value:
            return value
    return None


def _signal_platform_matches(doc: dict, targeting_platform: str) -> bool:
    """True if this signal's platform field (or, if that's missing, the
    platform inferred from its post URL) matches the job's
    targeting_platform. "all" (or anything unrecognized) means no
    filtering — every platform is allowed, exactly as it behaves today.
    Only when the user actually picked a specific platform (Reddit, X /
    Twitter, LinkedIn, Facebook) does this narrow results down to signals
    from that one platform."""
    if not targeting_platform or targeting_platform == "all":
        return True
    if "," in targeting_platform:   # (PLATFORM MULTI-SELECT) e.g. "x_twitter,linkedin"
        return any(_signal_platform_matches(doc, p) for p in targeting_platform.split(",") if p)

    allowed = _PLATFORM_DOC_VALUES.get(targeting_platform)
    if not allowed:
        return True  # unrecognized targeting value -> don't accidentally exclude everything

    doc_platform = _first_present(doc, _PLATFORM_FIELD_CANDIDATES)
    if not doc_platform or not isinstance(doc_platform, str):
        # No platform field on this doc — fall back to guessing from its
        # URL rather than excluding it outright.
        doc_platform = _infer_platform_from_url(_first_present(doc, _URL_FIELD_CANDIDATES))
    if not doc_platform:
        return False

    return doc_platform.strip().lower() in allowed


def _signal_keyword_matches(doc: dict, keyword_set: set) -> bool:
    """True if this signal's search-keyword field matches ANY keyword in
    keyword_set (case-insensitive), whichever one it happens to be.
    Handles the keyword field being a single string OR a list (in case a
    signal doc records more than one matched keyword).

    UNCHANGED from v3/v4/v5/v6/v7 and UNCHANGED by the keyword-generation
    swap — this exact-field check is completely untouched; it still works
    exactly as it always has, regardless of where `keyword_set` came
    from.

    (EMBEDDING-BASED MATCHING CHANGE) No longer called from inside
    get_matched_signals() — left intact, unused, harmless dead code."""
    if not keyword_set:
        return True  # no keyword filter to apply -> don't exclude anything

    raw_value = None
    for field in _KEYWORD_FIELD_CANDIDATES:
        if field in doc and doc[field]:
            raw_value = doc[field]
            break

    if not raw_value:
        return False

    if isinstance(raw_value, (list, tuple, set)):
        candidates = raw_value
    else:
        candidates = [raw_value]

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip().lower() in keyword_set:
            return True
    return False


def _text_matches_keyword(text: str, keyword_set: set) -> bool:
    """(v7, tightened by BUGFIX PACK #2) True if ANY keyword in
    keyword_set appears as a case-insensitive WHOLE-WORD/PHRASE match
    somewhere inside `text` — this is what lets a signal count as a match
    purely because a keyword phrase shows up in its own title or
    post_text, even when its search_keyword field doesn't match at all —
    a pure additional OR path alongside _signal_keyword_matches() above,
    never a replacement for it.

    (BUGFIX PACK #2): previously this did a bare `kw in text_lower`
    substring check, which meant a short/generic keyword like "buy" would
    also match inside completely unrelated words like "buying" or
    "buyer" — pulling in irrelevant posts that happened to contain that
    letter sequence as a fragment. Now uses a `\\b<keyword>\\b`
    word-boundary regex instead, so a keyword only counts as a match when
    it appears as a genuine whole word/phrase in the text, not as a
    fragment glued onto other letters. Signature, return type, and every
    caller are otherwise unchanged.

    (EMBEDDING-BASED MATCHING CHANGE) No longer called from inside
    get_matched_signals() — left intact, unused, harmless dead code."""
    if not text or not isinstance(text, str):
        return False
    text_lower = text.lower()
    for kw in keyword_set:
        if not kw:
            continue
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, text_lower):
            return True
    return False


_PHRASE_MATCH_STOPWORDS = {
    "a", "an", "the", "my", "your", "their", "is", "are", "to", "for",
    "of", "in", "on", "with", "and", "it", "this", "that",
}


def _phrase_matches_text(phrase: str, text: str, loose: bool = False) -> bool:
    """(PHRASE-MATCHING FEATURE) True if `phrase` (a short, natural 4-10
    word phrase — see the router's own "match_phrases" field) is
    genuinely reflected in `text`, checked two ways:
      1. The full phrase appears as a direct case-insensitive substring
         of `text` (a strong, exact signal), OR
      2. At least 70% of the phrase's MEANINGFUL words (a small, generic
         stopword list is dropped first — "a", "the", "is", etc., never
         topic-specific) appear as whole-word matches somewhere in
         `text`.
    This is what stops a single bare word like "agents" from ever
    matching a post on its own — a phrase carries several meaningful
    words, and an unrelated post will not contain 70%+ of them.

    `loose=True` lowers the fraction threshold to 40% instead of 70% —
    used ONLY for the tier-3 "closest match" candidate pool, never for
    the normal/primary match path.

    Returns False immediately if either `phrase` or `text` is falsy.

    (EMBEDDING-BASED MATCHING CHANGE) No longer called from inside
    get_matched_signals() — left intact, unused, harmless dead code."""
    if not phrase or not text or not isinstance(phrase, str) or not isinstance(text, str):
        return False

    text_lower = text.lower()
    phrase_lower = phrase.lower().strip()
    if not phrase_lower:
        return False

    if phrase_lower in text_lower:
        return True

    phrase_words = [w for w in re.findall(r"[a-z0-9']+", phrase_lower) if w not in _PHRASE_MATCH_STOPWORDS]
    if not phrase_words:
        return False

    threshold = 0.4 if loose else 0.7
    matched_count = 0
    for word in phrase_words:
        pattern = r"\b" + re.escape(word) + r"\b"
        if re.search(pattern, text_lower):
            matched_count += 1

    return (matched_count / len(phrase_words)) >= threshold


def _text_matches_any_phrase(text: str, phrases: list, loose: bool = False) -> bool:
    """(PHRASE-MATCHING FEATURE) True if _phrase_matches_text() is True
    for ANY phrase in `phrases`. Returns False immediately if `text` or
    `phrases` is falsy — never raises.

    (EMBEDDING-BASED MATCHING CHANGE) No longer called from inside
    get_matched_signals() — left intact, unused, harmless dead code."""
    if not text or not phrases:
        return False
    return any(_phrase_matches_text(phrase, text, loose=loose) for phrase in phrases)


def _infer_platform_from_url(url: str):
    """Fallback for when a signal doc doesn't have a usable platform field:
    guesses the platform from the post URL's domain so the icon/badge still
    matches reality (e.g. a twitter.com/x.com link shows the X icon instead
    of falling back to a generic globe). Only used when the doc's own
    platform field is missing — never overrides an actual platform value."""
    if not url or not isinstance(url, str):
        return None
    key = platforms.from_url(url)           # (PLATFORM REGISTRY) host match, not a substring of the whole URL
    return "twitter" if key == "x_twitter" else key     # the token this helper has always returned for X


# ─────────────────────────────────────────────────────────────────────────────
# POST ENRICHMENT HELPERS — subreddit / title derivation for answer posts.
# ─────────────────────────────────────────────────────────────────────────────

_SUBREDDIT_URL_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]+)", re.IGNORECASE)

# Values Claude (or a data source) uses when it doesn't actually know the
# title / source. Compared lower-cased and stripped.
_PLACEHOLDER_TITLES = {"", "(no title)", "no title", "untitled", "(untitled)", "n/a", "none", "null"}
_PLACEHOLDER_SOURCES = {"", "unknown", "(unknown)", "n/a", "na", "none", "null", "-",
                        "reddit", "r/", "x", "twitter", "linkedin", "facebook"}


def _subreddit_from_url(url):
    """Extract the subreddit name (without "r/") from a Reddit URL, e.g.
    https://www.reddit.com/r/AI_Agents/comments/abc/... -> "AI_Agents".
    Returns None for non-Reddit URLs, Reddit URLs without /r/, or bad input."""
    if not url or not isinstance(url, str):
        return None
    m = _SUBREDDIT_URL_RE.search(url)
    return m.group(1) if m else None


def _normalize_subreddit(value):
    """"r/AI_Agents" / "/r/AI_Agents" / "AI_Agents" -> "AI_Agents"; else None."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    v = re.sub(r"^/?r/", "", v, flags=re.IGNORECASE).strip().strip("/")
    return v or None


def _signal_subreddit(doc: dict, post_url):
    """Subreddit for a matched signal: the doc's own "subreddit" field if
    present, otherwise derived from a Reddit post_url. None otherwise."""
    return _normalize_subreddit((doc or {}).get("subreddit")) or _subreddit_from_url(post_url)


def _is_placeholder_title(title) -> bool:
    if title is None:
        return True
    if not isinstance(title, str):
        return True
    return title.strip().lower() in _PLACEHOLDER_TITLES


def _derive_post_title(title, post_text, max_chars: int = 80) -> str:
    """Return the real title when there is one; otherwise build a short
    label from post_text (first sentence, or the first ~max_chars cut at a
    word boundary with "..."). Returns "" only when both are empty."""
    if not _is_placeholder_title(title):
        return title.strip()
    text = " ".join((post_text or "").split()) if isinstance(post_text, str) else ""
    if not text:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    first = m.group(1).strip() if m else text
    if len(first) <= max_chars:
        return first
    cut = first[:max_chars]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-") + "..."


# ─────────────────────────────────────────────────────────────────────────────
# EMBEDDING-BASED MATCHING — helpers used by get_matched_signals()'s own
# non-unfiltered matching decision (see that function below). Mirrors the
# background service's own embedding config/client/generation logic
# name-for-name (see config.py's own docstring for the mirrored
# constants) — this is ONLY for generating the QUERY embedding(s) for
# a given call's keywords + match_phrases; document embeddings themselves
# are generated and saved onto flintel_signals.embedding by the
# background service, never regenerated here.
# ─────────────────────────────────────────────────────────────────────────────

_openai_client = None


def _get_openai_client():
    """Lazy OpenAI client, built once and reused — mirrors the background
    service's own _get_openai_client() exactly: returns None (never
    raises) when OPENAI_API_KEY isn't set, so every caller can treat a
    missing key as "no embeddings available" rather than a crash."""
    global _openai_client
    if not OPENAI_API_KEY:
        return None
    if _openai_client is None:
        try:
            from openai import OpenAI
            _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        except Exception as exc:
            log.warning(f"OpenAI client init failed: {exc}")
            return None
    return _openai_client


def generate_query_embedding(text: str):
    """Generates a single embedding (list[float]) for `text` using
    EMBEDDING_MODEL — mirrors the background service's own
    generate_embedding() logic exactly: truncates to EMBEDDING_MAX_CHARS,
    wraps the API call in try/except, and returns None on ANY failure
    (missing key, client init failure, API error, timeout, empty text)
    rather than ever raising. This is ONLY for embedding a QUERY string
    (this call's own keywords + match_phrases joined together) — document
    embeddings are already generated and saved by the background service
    and are never regenerated here.

    (PER-PHRASE EMBEDDING MATCHING FIX) Left completely AS-IS — no
    longer called from inside get_matched_signals() (which now calls
    generate_query_embeddings_batch() below instead, so every keyword/
    phrase gets its own embedding instead of one blended one), but kept
    here byte-for-byte unchanged: it may still be used elsewhere / is
    kept for backward-compat."""
    if not text or not isinstance(text, str):
        return None
    client = _get_openai_client()
    if not client:
        return None
    try:
        truncated = text[:EMBEDDING_MAX_CHARS]
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=truncated,
            timeout=EMBEDDING_TIMEOUT,
        )
        if _um:
            try: _um.record_usage("embedding", EMBEDDING_MODEL, response.usage.prompt_tokens, 0)
            except Exception: pass
        return response.data[0].embedding
    except Exception as exc:
        log.warning(f"Query-embedding generation failed: {exc}")
        return None


def generate_query_embeddings_batch(texts: list):
    """(PER-PHRASE EMBEDDING MATCHING FIX) Generates a SEPARATE embedding
    for EACH text (each keyword or each match_phrase) in `texts`, in a
    single batched OpenAI API call (one HTTP round trip, `input` given as
    a list) — never one combined/blended embedding for all of them
    joined together. This is what gives per-phrase precision: when
    match_phrases genuinely cover different sub-topics/angles for the
    same brand (e.g. "pricing complaints" vs "customer support delays"),
    a document that only strongly matches ONE of those angles is still
    caught, because it gets to be scored against that ONE phrase's own,
    un-diluted embedding rather than against a single averaged embedding
    of every phrase mashed together.

    Mirrors generate_query_embedding()'s exact fail-safe contract: never
    raises. Returns None (never raises) on a missing API key, a client-
    init failure, an API error, a timeout, or empty/all-falsy input —
    exactly the same failure modes generate_query_embedding() already
    guards against, just applied to a batch call instead of a single
    string.

    Falsy/non-string entries in `texts` are dropped before the API call
    (e.g. an empty string that slipped into a keywords/match_phrases
    list) — the returned list is in the SAME ORDER as `texts` AFTER that
    filtering, never the original, unfiltered order.

    Returns list[list[float]] (one embedding vector per surviving text,
    in order) on success, or None on total failure (including the case
    where every entry in `texts` was falsy/non-string and nothing usable
    was left to send)."""
    if not texts:
        return None
    client = _get_openai_client()
    if not client:
        return None
    try:
        truncated = [t[:EMBEDDING_MAX_CHARS] for t in texts if t and isinstance(t, str)]
        if not truncated:
            return None
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=truncated,
            timeout=EMBEDDING_TIMEOUT,
        )
        if _um:
            try: _um.record_usage("embedding", EMBEDDING_MODEL, response.usage.prompt_tokens, 0)
            except Exception: pass
        return [item.embedding for item in response.data]
    except Exception as exc:
        log.warning(f"Batch query-embedding generation failed: {exc}")
        return None


def _cosine_similarity(vec_a, vec_b) -> float:
    """Plain-Python cosine similarity (dot product / (magnitude_a *
    magnitude_b)) — no numpy dependency, so no new requirement is added.
    Returns 0.0 (never raises) whenever either vector is missing/empty,
    or the two vectors' lengths don't match, so a single corrupt/missing
    embedding on a document only ever drops that one document out of
    contention — it can never crash the whole matching function."""
    if not vec_a or not vec_b:
        return 0.0
    if len(vec_a) != len(vec_b):
        return 0.0
    try:
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        mag_a = math.sqrt(sum(a * a for a in vec_a))
        mag_b = math.sqrt(sum(b * b for b in vec_b))
        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0
        return dot / (mag_a * mag_b)
    except Exception:
        return 0.0


def _normalize_query_embeddings(query_embeddings):
    """(RETRIEVAL RECALL FIX — performance enabler) Pre-normalize this
    call's query embeddings ONCE, so the per-document scoring loop below
    only has to do dot products.

    The original code called _cosine_similarity(doc_embedding, qe) once
    per (document x query embedding) pair, and that function recomputed
    BOTH magnitudes every single time — so the query vector's magnitude
    (identical for every document in the pool) was recomputed tens of
    thousands of times per search, and the document's magnitude was
    recomputed once per query item instead of once per document. With ~10
    query items that is roughly an order of magnitude of pure waste.

    Removing it is what makes a RELEVANCE-selected candidate pool
    affordable at the same latency the old recency-only pool cost — see
    _max_similarity_against() and the hybrid pool build in
    get_matched_signals() below.

    Returns a list of (normalized_vector, length) tuples, skipping any
    unusable/zero-magnitude query vector. Never raises."""
    normalized = []
    for qe in (query_embeddings or []):
        try:
            if not qe:
                continue
            mag = math.sqrt(sum(v * v for v in qe))
            if mag == 0.0:
                continue
            normalized.append(([v / mag for v in qe], len(qe)))
        except (TypeError, ValueError):
            continue
    return normalized


def _max_similarity_against(doc_embedding, normalized_queries) -> float:
    """(RETRIEVAL RECALL FIX) Maximum cosine similarity between one
    document embedding and ANY of this call's pre-normalized query
    embeddings — mathematically identical to the previous
    max(_cosine_similarity(doc_embedding, qe) for qe in query_embeddings),
    just without the repeated magnitude recomputation.

    The MAX (rather than mean) semantics are deliberately unchanged from
    the PER-PHRASE EMBEDDING MATCHING FIX: a document only has to match
    ONE keyword/phrase/intent-expression strongly to be picked up.

    Returns 0.0 (never raises) for a missing/corrupt/length-mismatched
    document embedding, exactly like _cosine_similarity() did — so such a
    document is simply dropped by the threshold check rather than
    crashing the search."""
    if not doc_embedding or not normalized_queries:
        return 0.0
    try:
        doc_mag = math.sqrt(sum(v * v for v in doc_embedding))
        if doc_mag == 0.0:
            return 0.0
        doc_len = len(doc_embedding)
        # `best` starts as None, not 0.0, so an all-negative similarity
        # set returns the true (negative) maximum exactly as the previous
        # max(_cosine_similarity(...)) did, rather than being silently
        # floored at 0.0. A length-mismatched query contributes 0.0 for
        # the same reason: that is precisely what _cosine_similarity()
        # returned for a mismatch, and it participated in the max.
        best = None
        for unit_q, q_len in normalized_queries:
            if q_len != doc_len:
                score = 0.0
            else:
                score = sum(a * b for a, b in zip(doc_embedding, unit_q)) / doc_mag
            if best is None or score > best:
                best = score
        return 0.0 if best is None else best
    except (TypeError, ValueError):
        return 0.0


def _build_lexical_or_clause(keyword_list: list, phrase_list: list) -> list:
    """(RETRIEVAL RECALL FIX — hybrid retrieval, lexical half) Builds the
    Mongo $or conditions that let the candidate pool be selected by
    RELEVANCE instead of purely by recency.

    WHY THIS EXISTS: the previous candidate pool was
    `find({"embedding": {...}}).sort("created_utc", -1).limit(POOL)` —
    i.e. the newest N documents, with NO relevance condition whatsoever.
    Every document older than the Nth-newest was structurally unreachable
    by every query, however perfect a match it was. That is a retrieval
    failure that the product then reported to the user as "no posts
    found", i.e. as an absence of data.

    Each term becomes a case-insensitive, word-boundary-anchored regex on
    the title/text fields. Word boundaries matter: a bare substring match
    would let a short term like "AI" hit inside "said", "maintain", etc.
    Terms shorter than 3 characters are dropped for the same reason.

    Multi-word phrases are also split into their content words, so a
    phrase like "need someone to build an AI agent" can still pull in a
    post that expresses the same idea with different connecting words —
    the embedding stage afterwards is what decides genuine relevance;
    this stage only has to RECALL plausible candidates.

    Returns [] when nothing usable survives, which the caller treats as
    "no lexical tier for this query" and falls back to the recency tier
    alone — never an error."""
    terms = set()
    for term in list(keyword_list or []) + list(phrase_list or []):
        if not isinstance(term, str):
            continue
        cleaned = term.strip().lower()
        # A whole match_phrase is now a full sentence, which would almost
        # never appear verbatim — so only short-enough terms are used
        # whole; longer ones contribute their content words instead.
        if 3 <= len(cleaned) <= 40:
            terms.add(cleaned)
        # Content words out of multi-word terms (stopwords dropped so a
        # phrase doesn't contribute near-useless terms like "the"/"for").
        for word in re.findall(r"[a-z0-9']+", cleaned):
            if len(word) >= 4 and word not in _PHRASE_MATCH_STOPWORDS:
                terms.add(word)

    if not terms:
        return []

    # Keep the MOST SELECTIVE terms first (longer/multi-word terms match
    # fewer documents than single common words) and bound the total, so
    # the generated pattern can never grow without limit as the router
    # returns more keywords/phrases.
    ordered = sorted(terms, key=lambda t: (-len(t), t))[:LEXICAL_MAX_TERMS]

    # ONE alternation regex per field — not one condition per term.
    # Emitting a separate {field: {$regex: term}} for every term x field
    # combination produced hundreds of conditions in a single $or (438 for
    # a realistic 10-keyword + 12-phrase query), which MongoDB would have
    # to evaluate individually against every scanned document. A single
    # \b(?:a|b|c)\b alternation per field collapses that to one pass per
    # field while matching exactly the same documents.
    try:
        alternation = r"\b(?:" + "|".join(re.escape(t) for t in ordered) + r")\b"
        re.compile(alternation)
    except re.error:
        return []

    return [
        {field: {"$regex": alternation, "$options": "i"}}
        for field in _LEXICAL_SEARCH_FIELDS
    ]


def _compute_query_signature(keywords: list, match_phrases: list) -> str:
    """(QUERY-EMBEDDING CACHING) Deterministic, order-independent
    fingerprint of a given keywords + match_phrases combination — used
    to decide whether a previously-cached query embedding for a topic
    (stored on topic_evidence_cache_collection, see get_matched_
    signals()'s own updated docstring) is still valid for THIS call's
    keywords/match_phrases, or whether they've changed enough that a
    fresh embedding must be generated instead.

    Both lists are lowercased/trimmed and SORTED before hashing, so the
    exact same set of keywords/phrases in a different order (e.g. a
    router call that returns the same terms in a different sequence)
    still produces the identical signature, and a cached embedding is
    correctly reused rather than needlessly regenerated. Returns a
    short hex sha256 digest; never raises — any unexpected input just
    hashes down to a (still valid, still comparable) signature for an
    effectively-empty query."""
    try:
        kw_part = "|".join(sorted(
            k.strip().lower() for k in (keywords or []) if isinstance(k, str) and k.strip()
        ))
        ph_part = "|".join(sorted(
            p.strip().lower() for p in (match_phrases or []) if isinstance(p, str) and p.strip()
        ))
        combined = f"kw:{kw_part}::ph:{ph_part}"
    except Exception:
        combined = ""
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _recency_bonus(doc, now=None) -> float:
    """RECENCY_RANK_WEIGHT * freshness(0..1) of one candidate, from its validated publication time (published_at, else
    created_utc, through timerange.publication_time: future / pre-2005 / unreadable = unknown = 0)."""
    try:
        weight = float(_cfg_value("RECENCY_RANK_WEIGHT", 0.05) or 0)
        if weight <= 0 or not isinstance(doc, dict):
            return 0.0
        when = timerange.post_published_at(doc)
        if when is None:
            return 0.0
        half = float(_cfg_value("RECENCY_HALF_LIFE_DAYS", 30) or 30) or 30.0
        age_days = max(0.0, ((now or datetime.now(timezone.utc)) - when).total_seconds() / 86400.0)
        return weight * (0.5 ** (age_days / half))
    except Exception:
        return 0.0


_TIME_SCOPE = contextvars.ContextVar("flintel_time_scope", default=None)


@contextlib.contextmanager
def time_scope_context(scope):
    """Makes `scope` (from build_time_scope) the publication window that _patch_post_urls_into_answer() applies to the answer
    it post-processes in this thread/context. A context, not a new argument, so every existing call (and test double of
    _patch_post_urls_into_answer) keeps its signature."""
    token = _TIME_SCOPE.set(scope)
    try:
        yield
    finally:
        _TIME_SCOPE.reset(token)


# ── newest stored post (Gate 2) ──────────────────────────────────────────────────────────────────────────────────────────
# What the user may be told about the age of the data: the PUBLICATION time of the newest valid post stored for the searched
# platform selection. It is a lower bound on how stale the stored data is, nothing more: it is not when anything was
# collected or stored, and it says nothing about collector health or coverage (a quiet community has an old newest post
# even when collection is perfect). Computed from the same sources a search reads (primary + read-only mirrors; MySQL is
# never used: its time columns are unverified), bounded above by now + the future tolerance so a future-dated record can
# never be "newest", validated like every other publication time, cached for a few minutes, and never raises.
_NEWEST_TTL_SECONDS = 300
_NEWEST_MAX_TIME_MS = 3000
_NEWEST_CACHE = {}


def _newest_platform_clause(selection: str):
    keys = [k for k in selection.split(",") if k in platforms.REGISTRY] if selection != "all" else []
    if not keys:
        return None
    values = sorted({v for k in keys for v in _PLATFORM_DOC_VALUES.get(k, [k])})
    hosts = sorted({h for k in keys for h in platforms.REGISTRY[k].hosts})
    return {"$or": [{"platform": {"$regex": "^(" + "|".join(re.escape(v) for v in values) + ")$", "$options": "i"}},
                    {"platform": {"$in": [None, ""]}, "post_url": {"$regex": "(" + "|".join(re.escape(h) for h in hosts) + ")", "$options": "i"}}]}


def newest_stored_post(targeting_platform=None, now=None) -> dict:
    """{"status": found|empty|partial|unavailable, "published_at": ISO UTC or None, "scope": canonical selection,
    "sources_read": n, "sources_failed": n}. `partial`: some source could not be read, the date is the newest of the others."""
    sel = platforms.selection_key(targeting_platform)
    if now is None:
        hit = _NEWEST_CACHE.get(sel)
        if hit and time.monotonic() - hit[0] < _NEWEST_TTL_SECONDS:
            return dict(hit[1])
    ref = now or datetime.now(timezone.utc)
    query = {"created_utc": {"$lt": ref + timerange.FUTURE_TOLERANCE}}
    clause = _newest_platform_clause(sel)
    if clause:
        query = {"$and": [query, clause]}
    single = sel if "," not in sel and sel != "all" else None
    newest, ok, failed = None, 0, 0
    for coll in (signals_collection, signals_collection_2, signals_collection_4):
        if coll is None:
            continue
        try:
            docs = list(coll.find(query, {"created_utc": 1, "_id": 0}).sort("created_utc", -1).limit(1).max_time_ms(_NEWEST_MAX_TIME_MS))
            ok += 1
            when = timerange.publication_time(docs[0].get("created_utc"), now=ref, platform=single) if docs else None
            if when is not None and (newest is None or when > newest):
                newest = when
        except Exception as exc:
            failed += 1
            log.warning(f"newest_stored_post: a source could not be read ({type(exc).__name__})")
    status = "unavailable" if ok == 0 else ("empty" if newest is None else ("partial" if failed else "found"))
    result = {"status": status, "published_at": newest.isoformat() if newest else None, "scope": sel,
              "sources_read": ok, "sources_failed": failed}
    if now is None and status != "unavailable":
        _NEWEST_CACHE[sel] = (time.monotonic(), dict(result))
    return result


def build_time_scope(time_window_days=None, time_range=None, requested_at=None, targeting_platform=None):
    """The effective publication window of one message, for the answer checks and the UI: {"start","end","label","days"}
    (ISO UTC) or None when the search is not time-bounded. A window is measured at the REQUEST time, so a reopened chat is
    judged against the moment it was asked, not today. With `targeting_platform` (the production callers) the scope also
    carries "newest_stored" (newest_stored_post() for that selection) — even for an unbounded search, which then has
    no start / end."""
    scope = _build_window_scope(time_window_days, time_range, requested_at)
    if targeting_platform is None:
        return scope
    try:
        return dict(scope or {}, newest_stored=newest_stored_post(targeting_platform))
    except Exception:
        return scope


def _build_window_scope(time_window_days=None, time_range=None, requested_at=None):
    try:
        tr = time_range if isinstance(time_range, timerange.TimeRange) else timerange.range_from_doc(time_range)
        if tr is not None:
            return {"start": tr.start.isoformat(), "end": tr.end.isoformat(), "label": tr.describe(), "days": None}
        if isinstance(time_window_days, int) and not isinstance(time_window_days, bool) and time_window_days > 0:
            days = min(time_window_days, MAX_TIME_WINDOW_DAYS)
            ref = timerange.to_utc(requested_at) or datetime.now(timezone.utc)
            return {"start": (ref - timedelta(days=days)).isoformat(), "end": (ref + timerange.FUTURE_TOLERANCE).isoformat(),
                    "label": f"last {days} day{'s' if days != 1 else ''}", "days": days}
    except Exception as exc:
        log.warning(f"build_time_scope failed (treating the search as unbounded): {exc}")
    return None


def get_matched_signals(topic_key: str, keywords: list, targeting_platform: str = "all",
                         limit: int = None, since_days: int = None, unfiltered: bool = False,
                         match_phrases: list = None, loose: bool = False,
                         signals_collection_2=None, chat_id: str = None,
                         signals_collection_4=None, user_query: str = None,
                         scan_state: dict = None, time_range=None,
                         cancel_event: "threading.Event" = None) -> list:
    """Reads `flintel_signals` and keeps only the signals that are
    genuinely relevant to this job's topic. topic_key match is
    intentionally NOT required: Background Service #1 may store its own
    topic_key for a signal, but what decides a match here is purely the
    relevance check described below.

    (EMBEDDING-BASED MATCHING CHANGE, per-phrase precision added by the
    PER-PHRASE EMBEDDING MATCHING FIX) The core "is this signal a match"
    decision is EMBEDDING COSINE-SIMILARITY based, not keyword/title/
    text substring based:
      1. `keywords` + `match_phrases` (when given) are treated as
         SEPARATE query items — never joined into one combined string —
         and a SEPARATE embedding is generated for EACH one, in a single
         batched OpenAI call, via generate_query_embeddings_batch().
         This avoids the "diluted/averaged" similarity a single combined
         embedding used to produce when match_phrases genuinely covered
         different sub-topics/angles (e.g. "pricing complaints" vs
         "customer support delays") — a document that only strongly
         matches ONE angle is scored against that angle's own, un-
         diluted embedding, not an average of every angle mashed
         together.
      2. Only documents that already have a saved `embedding` field
         (produced ahead of time by the background service — never
         regenerated here) are even considered as candidates — this is
         enforced directly in the Mongo query itself
         (`{"embedding": {"$ne": None, "$exists": True}}`), on top of the
         existing time-window cutoff (see TIME-WINDOW FEATURE below,
         unchanged).
      3. Each candidate document's similarity score is the MAXIMUM
         cosine similarity between its own saved embedding and ANY ONE
         of this call's per-keyword/per-phrase query embeddings
         (_cosine_similarity() computed once per query embedding, then
         the max is taken) — so a document only needs to strongly match
         ONE keyword or ONE phrase to be picked up, exactly as if that
         one phrase had run as its own standalone search. A document
         whose best (max) similarity falls below
         SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD is not considered a match
         at all.
      4. Candidates that clear the threshold are sorted by this max
         similarity score, HIGHEST FIRST, before the platform filter,
         per-platform cap, and overall `limit` below are applied — so the
         best-matching posts are always the ones kept when a cap trims
         the list. (The existing `created_utc` descending sort still
         governs the Mongo-level FETCH stage only, purely to pick which
         SIGNAL_EMBEDDING_CANDIDATE_POOL-sized pool of recent-first
         candidates gets its similarity computed in the first place.)
      5. If query embeddings cannot be generated at all for this call
         (e.g. OPENAI_API_KEY is missing, or the embeddings API call
         fails), this function fails safe and returns an empty list —
         exactly the same "no keywords -> no results" safety behavior
         this function already had before this change, just triggered by
         a different failure mode.
    The OLD keyword-field / title-substring / phrase-substring matching
    rules (_signal_keyword_matches, _text_matches_keyword, _text_matches_
    any_phrase, _phrase_matches_text) are no longer used by this
    function — they remain defined above, untouched, as harmless dead
    code. generate_query_embedding() (the single-string version) is
    ALSO no longer called from inside this function — it remains defined
    above, untouched, byte-for-byte as-is, for backward-compat / any
    other caller; this function now calls generate_query_embeddings_
    batch() instead.

    (EMBEDDING-MATCH QUERY/LOGGING CLEANUP) Two small, purely additive/
    non-behavioral follow-ups live inside this function now: the Mongo
    "embedding" existence guard is explicit (`$ne: None` AND
    `$exists: True`), and — only when DEBUG logging is enabled — a single
    log.debug() line reports this call's candidate-pool size, the current
    similarity threshold, how many candidates cleared it, and the top 5
    raw (max, across all query embeddings) similarity scores seen across
    the whole pool, keyed by topic_key. Neither change affects the return
    value, the matching decision, or anything else about this function.

    (QUERY-EMBEDDING CACHING, updated shape by the PER-PHRASE EMBEDDING
    MATCHING FIX) Step 1 above (generating this call's own per-item query
    embeddings from keywords + match_phrases) is cache-aware, so a topic
    being polled repeatedly (e.g. stream_answer()'s ~2s polling loop, or
    repeated follow-ups on the same topic) doesn't hit the OpenAI
    embeddings API on every single call:
      - This caching layer is keyed on the SAME document the per-chat
        evidence-posts cache (get_cached_topic_evidence() / save_topic_
        evidence_cache()) already maintains for this topic — i.e. the
        topic_evidence_cache_collection document keyed on
        {"chat_id": chat_id, "topic_key": topic_key} — so the two caches
        can never drift onto two different documents for what is really
        the same (chat, topic) pair.
      - `chat_id` (optional parameter, default None, always LAST in this
        function's signature so every existing caller that doesn't pass
        it behaves exactly as before) controls this entirely: when a
        `chat_id` IS given, before calling generate_query_embeddings_
        batch(), this function computes a signature for this call's own
        keywords + match_phrases (via _compute_query_signature() —
        order-independent, so the same set in a different order still
        matches) and looks up topic_evidence_cache_collection for the
        {"chat_id": chat_id, "topic_key": topic_key} document that
        already has a "query_embedding" field (now a LIST of vectors —
        one per keyword/phrase, in the same order as query_items below)
        whose saved "query_embedding_signature" matches. If found, that
        SAVED list of embeddings is reused directly —
        generate_query_embeddings_batch() is never called at all for
        this call. On a cache miss (new topic in this chat, or
        keywords/match_phrases changed since the last cached embeddings),
        generate_query_embeddings_batch() is called exactly as before,
        and — best-effort — the fresh list of embeddings + its signature
        (plus "chat_id") are saved back onto that same {"chat_id":
        chat_id, "topic_key": topic_key} document in
        topic_evidence_cache_collection for next time.
      - When NO `chat_id` is given (the default — e.g. an older/direct
        caller that doesn't pass one, or an unfiltered call), this
        caching layer is skipped ENTIRELY: no Mongo read, no Mongo
        write, and generate_query_embeddings_batch() is called fresh,
        uncached, exactly as if this feature didn't exist. This avoids
        ever writing an orphan/ambiguous document that isn't scoped to a
        real chat.
      - FAIL-SAFE: every Mongo read/write in this caching layer is
        wrapped in its own try/except; ANY failure (missing collection,
        connection hiccup, malformed cached doc, etc.) silently falls
        back to the original, uncached generate_query_embeddings_batch()
        call — this caching layer can never cause matching to fail,
        return different results, or crash. The existing "no query
        embeddings at all -> return []" safety behavior is completely
        unchanged.
      - This is purely an optimization on top of Step 1's OWN embedding
        generation — it has nothing to do with, and never touches, the
        separate per-chat evidence-posts cache (get_cached_topic_
        evidence() / save_topic_evidence_cache() / get_evidence_with_
        topup()), which keeps its own existing fields and behavior on
        the very same Mongo documents untouched (this function only ever
        adds/reads the "query_embedding" / "query_embedding_signature" /
        "chat_id" fields on that document — it never reads or writes
        "posts", "post_urls_seen", "evidence_count", etc.).

    `targeting_platform` (the same "all" | "reddit" | "x_twitter" |
    "linkedin" | "facebook" value already stored on the job/message) is
    still applied on top, completely UNCHANGED: "all" pulls a match from
    whichever platform it came from; any specific platform restricts
    matches to signals from that platform only — the user's dropdown
    choice decides this, nothing else.

    (v7) PER-PLATFORM CAP: on top of the existing overall `limit`
    (MAX_MATCHED_RESULTS by default — still respected, still the same
    variable/behavior as before), each individual platform can
    contribute AT MOST MAX_POSTS_PER_PLATFORM matches to this call's
    results (default 3). UNCHANGED.

    (EVIDENCE-BUDGET FEATURE) `limit` is no longer always the static
    MAX_MATCHED_RESULTS default — a caller may now also pass a dynamic
    value derived from a message's own `evidence_required` (the router's
    per-query evidence-planner estimate, clamped between
    MIN_ANALYSIS_EVIDENCE and MAX_ANALYSIS_EVIDENCE). This function's own
    signature/logic is otherwise completely unchanged: `limit=None`
    (the default, and what every pre-existing call site still passes for
    an older/non-search message) falls back to MAX_MATCHED_RESULTS
    exactly as it always has.

    (TIME-WINDOW FEATURE) `since_days`, default None: when a positive int
    is given, this ADDS an extra AND-condition on top of everything
    above — a signal must ALSO have `created_utc` within the last
    `since_days` days to be eligible at all. This is applied BOTH at the
    Mongo query level (so old signals are excluded from the DB fetch
    itself, not just filtered out afterward in Python) AND, defensively,
    once more in the Python loop below (in case a signal doc is missing a
    usable `created_utc` value — such a doc is simply excluded rather
    than assumed to pass). `since_days=None` (the default, and what every
    call site used before this feature) means NO time filtering at all —
    behavior is then 100% identical to before this feature. The time
    window is purely a narrowing on top of the embedding-similarity
    match: a signal still has to clear the similarity threshold above;
    the time window can only ever exclude MORE signals, never match one
    that wouldn't otherwise match on similarity. UNCHANGED by this
    change other than being combined with the new embedding-existence
    Mongo filter instead of the old keyword $or.

    Results are sorted by embedding similarity DESCENDING (best match
    first) before the caps above are applied (previously sorted by
    `created_utc` descending at this stage — that sort is now used only
    at the earlier Mongo-fetch stage, to pick the candidate pool).

    Returns {title, post_text, post_url, platform} for each match — this
    is the only signal-derived output ever shown to the user (via post
    cards) or persisted onto a chat message's `results` (platform is
    included only so the UI can show which platform a result came from;
    it isn't used for anything else here). Never touches jobs_collection
    or the raw `signals` list returned by get_signals().

    COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP: this function's
    SIGNATURE (aside from the earlier, optional, default-None
    `since_days` parameter, and the newer, optional, default-None
    `chat_id` parameter), RETURN SHAPE, and every existing caller are
    exactly as they were before — it has no idea whether `keywords` came
    from Claude's router call or the old fuzzy-template fallback; it now
    just uses them (plus match_phrases) to build the per-item query
    embeddings for its own matching instead.

    (SECOND-COLLECTION MERGE) `signals_collection_2`, default None: an
    optional second Mongo collection handle. When provided, its docs are
    combined into the same matching pool as `signals_collection`'s own
    docs — see the inline comments below at both call sites (the
    `unfiltered` branch's flintel.get_unfiltered_matched_signals() call,
    and the primary `raw_docs` fetch) for exactly how. Every matching
    rule downstream (embedding-similarity match, platform filter,
    text-required check, dedup, per-platform cap, limit) is completely
    unchanged and applies identically to docs from either collection.

    (QUATERNARY MONGO) `signals_collection_4` (default None) is folded
    into the same raw_docs pool exactly like `signals_collection_2`
    above — same candidate pool, same downstream matching
    (embedding-similarity, platform filter, text-required check, dedup,
    per-platform cap, limit). No separate ignore/filter logic is needed:
    a missing/invalid embedding already scores 0.0 via
    _cosine_similarity() and gets dropped by the threshold check, and
    the Mongo query itself already requires a saved embedding field."""
    # (DATE/TIME) An explicit [start, end) publication range from the user's own
    # words. It replaces since_days; the lower bound reuses every existing
    # `cutoff` check below and the upper bound is checked at the same places.
    _tr = time_range if isinstance(time_range, timerange.TimeRange) else timerange.range_from_doc(time_range)
    if _tr is not None:
        since_days = (max(1, math.ceil((datetime.now(timezone.utc) - _tr.start).total_seconds() / 86400))
                      if unfiltered else None)
    if unfiltered:
        _unf = flintel.get_unfiltered_matched_signals(
            signals_collection,
            since_days=since_days,
            targeting_platform=targeting_platform,
            limit=limit or MAX_MATCHED_RESULTS,
            max_per_platform=MAX_POSTS_PER_PLATFORM,
            signals_collection_2=signals_collection_2,
            signals_collection_4=signals_collection_4,
        )
        _register_scan_status(chat_id, topic_key, True)
        if _tr is not None:
            _unf, _ = timerange.filter_posts(_unf, _tr)
        return _unf

    limit = limit or MAX_MATCHED_RESULTS
    keyword_list = [k for k in (keywords or []) if k]
    phrase_list = [p for p in (match_phrases or []) if p]
    if not keyword_list and not phrase_list:
        return []

    # (EMBEDDING-BASED MATCHING CHANGE) Mongo query now carries NO
    # keyword-based $or conditions at all — only the (unchanged)
    # time-window cutoff, plus a new filter requiring a saved, non-null
    # "embedding" field, so only documents the background service has
    # already embedded are ever considered as candidates.
    #
    # (EMBEDDING-MATCH QUERY/LOGGING CLEANUP) "$exists": True added
    # alongside "$ne": None to make the existence guard explicit — purely
    # a query-clarity change, not a behavior change (a missing embedding
    # field was already effectively excluded downstream, since
    # _cosine_similarity() safely scores it as 0.0, which never clears
    # SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD).
    mongo_query = {"embedding": {"$ne": None, "$exists": True}}

    # (TIME-WINDOW FEATURE) Compute an optional cutoff and AND it onto the
    # embedding-existence clause via $and, so a time window narrows the
    # embedding-similarity match instead of replacing it. since_days is
    # sanity-clamped between 1 and MAX_TIME_WINDOW_DAYS — anything else
    # (None, 0, negative, absurdly large) means "no time filter", handled
    # the exact same way this function always behaved before this
    # feature.
    cutoff = None
    cutoff_end = None
    if isinstance(since_days, int) and since_days > 0:
        clamped_days = min(since_days, MAX_TIME_WINDOW_DAYS)
        cutoff = datetime.now(timezone.utc) - timedelta(days=clamped_days)
        # (TIMESTAMP CORRECTNESS) a "last N days" window has an upper bound too: a post dated in the future is not
        # inside any past window (cutoff_end is honoured by every scan path below, as for an explicit time range).
        cutoff_end = datetime.now(timezone.utc) + timerange.FUTURE_TOLERANCE
        mongo_query = {"$and": [mongo_query, {"created_utc": {"$gte": cutoff, "$lt": cutoff_end}}]}
    if _tr is not None:
        cutoff, cutoff_end = _tr.start, _tr.end
        mongo_query = {"$and": [mongo_query, {"created_utc": {"$gte": cutoff, "$lt": cutoff_end}}]}

    # (INCREMENTAL RESCAN) `scan_state` is an optional in/out dict supplied by
    # get_evidence_with_topup(); None (the default, and every other caller)
    # means a plain full scan exactly as before. When it carries a
    # "watermarks" mapping (collection label -> datetime) this call reads
    # ONLY docs newer than that watermark (minus a small overlap), scores
    # just those, and merges them with the previously selected posts in
    # scan_state["carry"] ((url, similarity, post) triples) so the result is
    # the same as a full rescan. Outputs written back into scan_state:
    #   delta_mode, fetched, new_wm, new_wm_created, bad_id, complete,
    #   unchanged, sims.
    _wm_field = ((scan_state.get("field") or _watermark_field()) if scan_state is not None else None)
    _delta_wm = (scan_state or {}).get("watermarks") if scan_state is not None else None
    if scan_state is not None:
        scan_state["delta_mode"] = bool(_delta_wm)
        scan_state["field"] = _wm_field
        scan_state.setdefault("fetched", {})
        scan_state.setdefault("new_wm", {})
        scan_state.setdefault("new_wm_created", {})
    _overlap_s = int(_cfg_value("INCREMENTAL_OVERLAP_SECONDS", 90) or 0)
    _fetch_failures = []

    # (SCAN RESILIENCE) Cancellation + deadline plumbing.
    #   cancel_event      caller's threading.Event (e.g. all waiters gone)
    #   _deadline_cancel  set by THIS call when SCAN_DEADLINE_SECONDS is hit
    # Workers poll both about every 1000 docs and stop early. A worker that
    # was cancelled returns nothing and writes NOTHING into scan_state, and the
    # collection counts as failed, so its watermark can never advance past docs
    # that were not actually read. _scan_lock orders worker writes against the
    # deadline decision (a worker either finishes before it, or is ignored).
    _deadline_cancel = threading.Event()
    _scan_lock = threading.Lock()
    _find_max_ms = int(_cfg_value("MONGO_FIND_MAX_TIME_MS", 0) or 0)

    def _cancelled() -> bool:
        return _deadline_cancel.is_set() or (cancel_event is not None and cancel_event.is_set())

    _stream_ctx = None      # set below when streaming score is active
    _stream_out = {}        # label -> per-collection streamed-score result

    # ── (RETRIEVAL RECALL FIX) HYBRID CANDIDATE POOL ────────────────────
    # THE BUG THIS REPLACES: the candidate pool used to be, for every
    # collection, simply
    #     find({"embedding": {...}}).sort("created_utc", -1).limit(POOL)
    # — the NEWEST `POOL` documents, with no relevance condition in the
    # query at all. Cosine similarity was then computed only over that
    # recency window. The practical consequence: every document older
    # than the POOL-th newest one was UNREACHABLE by every query ever
    # run, no matter how perfect a semantic match it was. On a corpus of
    # tens of thousands of signals that meant only the most recent ~1%
    # was ever searched, and the product reported the resulting misses to
    # the user as "no posts found" — i.e. it reported a RETRIEVAL failure
    # as an ABSENCE OF DATA.
    #
    # THE FIX: hybrid retrieval — a lexical tier plus the original
    # recency tier, unioned, then ranked by exactly the same embedding
    # cosine similarity as before.
    #   TIER 1 (lexical, NEW): the same query AND-ed with a keyword/phrase
    #     $or (see _build_lexical_or_clause()). This reaches relevant
    #     documents ANYWHERE in the collection regardless of age, which is
    #     what actually fixes the missing-recall problem.
    #   TIER 2 (recency, UNCHANGED): the original newest-N query, kept
    #     verbatim so that a genuinely semantic match which shares no
    #     literal vocabulary with the query can still be found in recent
    #     data exactly as it is today. Nothing that matches now stops
    #     matching.
    # Ranking/threshold/caps downstream are untouched: the embedding
    # similarity stage is still the only thing that decides relevance.
    # The lexical tier only decides which documents get CONSIDERED.
    _pool_projection = {"_id": 0, "embedding": 1, **{
        f: 1 for f in (
            _TITLE_FIELD_CANDIDATES + _TEXT_FIELD_CANDIDATES +
            _URL_FIELD_CANDIDATES + _PLATFORM_FIELD_CANDIDATES + ["created_utc", "subreddit"]
        )
    }}
    lexical_or = _build_lexical_or_clause(keyword_list, phrase_list)

    def _fetch_mysql_pool(source, label):
        """(MYSQL READ) Stream one MySQL source and score it with the same
        query matrix / cosine / threshold as the Mongo streamed path. Rows
        come as (docs, float32 matrix) batches; NULL/invalid embeddings are
        already skipped (no lazy backfill for MySQL). Results go into
        _stream_out[label] in the Mongo format; always returns [] raw docs.
        No watermark: a MySQL source is fully scanned every time, and in
        delta mode posts already carried over are skipped (url dedupe), so
        the merged result equals a full rescan."""
        import mysql_signals  # noqa: PLC0415  (only reached when the flag is on)
        _st = _stream_ctx
        pairs = []
        consumed = 0
        new = 0
        failed = False
        was_cancelled = False
        known = set()
        if scan_state is not None and scan_state.get("delta_mode"):
            known = {c[0] for c in (scan_state.get("carry") or []) if c and c[0]}
        t0 = time.monotonic()
        gen = None
        try:
            if _cancelled():
                raise _ScanCancelled()
            gen = mysql_signals.iter_signal_batches(
                source, cutoff=cutoff, batch_size=SIGNAL_EMBEDDING_FETCH_BATCH,
                dim=_st["dim"], cancelled=_cancelled,
            )
            for docs, M in gen:
                consumed += len(docs)
                if cutoff is not None:
                    # same defensive time-window rule as the Mongo path
                    keep = [k for k, d in enumerate(docs)
                            if isinstance(d.get("created_utc"), datetime) and d["created_utc"] >= cutoff
                            and (cutoff_end is None or d["created_utc"] < cutoff_end)]
                    if len(keep) != len(docs):
                        docs = [docs[k] for k in keep]
                        M = M[keep]
                if docs:
                    # score the fetched batch in chunks of SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH rows (default = the fetch
                    # batch, i.e. one chunk, as before; never below SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH_MIN = 500). Rows stay in order, so the result is the same for any chunk size.
                    _sb = max(SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH_MIN, int(SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH or 0))
                    for _i in range(0, len(docs), _sb):
                        scores = _np_max_scores(M[_i:_i + _sb], _st["Q"])
                        for sc, dd in zip(scores, docs[_i:_i + _sb]):
                            if float(sc) >= SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD:
                                if known and dd.get("post_url") in known:
                                    continue               # already carried (delta dedupe)
                                pairs.append((float(sc), dd))
                                new += 1
                if _cancelled():
                    raise _ScanCancelled()
        except (_ScanCancelled, mysql_signals.ScanCancelled):
            was_cancelled = True
            log.warning(f"{label}: scan cancelled after {consumed} rows (source treated as incomplete)")
        except Exception as exc:
            failed = True
            log.warning(f"{label}: MySQL read failed after {consumed} rows: {mysql_signals.safe_error(exc)}")
        finally:
            if gen is not None:
                try:
                    gen.close()
                except Exception:
                    pass
        elapsed = time.monotonic() - t0
        if was_cancelled:
            _fetch_failures.append(label)
            return []
        with _scan_lock:
            if _cancelled():
                _fetch_failures.append(label)
                return []
            if failed:
                _fetch_failures.append(label)
            if scan_state is not None:
                scan_state["fetched"][label] = consumed
            _stream_out[label] = {"pairs": pairs, "consumed": consumed, "new": new}
        log.info(
            f"{label} streamed-score: scanned={consumed} kept={len(pairs)} "
            f"batch={SIGNAL_EMBEDDING_FETCH_BATCH} score_batch={SIGNAL_EMBEDDING_MYSQL_SCORE_BATCH} elapsed={elapsed:.2f}s"
        )
        return []

    def _fetch_candidate_pool(collection, label):
        """Fetch raw candidate docs for one collection.

        UNLIMITED MODE (SIGNAL_EMBEDDING_CANDIDATE_POOL <= 0, the new default):
          Skips the lexical Tier 1 entirely. Opens a cursor over ALL docs in
          the time window that have a valid embedding — no .limit(). Uses
          batch_size for efficient network transfer. Scoring (numpy vectorized
          cosine) happens in the caller's existing scoring loop so query
          embeddings are available. An optional SIGNAL_EMBEDDING_MAX_SCAN cap
          stops iteration early and logs a warning if hit.
          Returns: list of raw docs (same shape as legacy mode).

        LEGACY MODE (SIGNAL_EMBEDDING_CANDIDATE_POOL > 0):
          Original hybrid Tier1 (lexical, capped) + Tier2 (recency, capped).
          Returns: list of raw docs (unchanged behaviour).

        Never raises — failures are logged; that tier contributes nothing.
        """
        if getattr(collection, "is_mysql_source", False) is True:     # (MYSQL READ)
            return _fetch_mysql_pool(collection, label)
        # ── UNLIMITED MODE ────────────────────────────────────────────────
        if SIGNAL_EMBEDDING_CANDIDATE_POOL <= 0 and SIGNAL_EMBEDDING_RECENCY_POOL <= 0:
            # mongo_query already carries the time-window filter (created_utc
            # >= cutoff). Add embedding-exists so every returned doc is
            # guaranteed to have an embedding, saving a None-check per doc.
            unlimited_query = dict(mongo_query)
            unlimited_query["embedding"] = {"$exists": True, "$ne": None}

            fetch_query = unlimited_query
            fetch_projection = _pool_projection
            _max_wm = None
            _max_created = None
            _bad_id = False
            if scan_state is not None:
                fetch_projection = dict(_pool_projection)
                if _wm_field == "_id":
                    fetch_projection.pop("_id", None)   # delta needs _id
                else:
                    fetch_projection[_wm_field] = 1
                _prev_wm = (_delta_wm or {}).get(label)
                if _prev_wm is not None:
                    try:
                        fetch_query = {"$and": [
                            unlimited_query,
                            _watermark_clause(_wm_field, _prev_wm, _overlap_s),
                        ]}
                    except Exception as exc:
                        log.warning(f"{label}: watermark clause failed, full scan: {exc}")
                        fetch_query = unlimited_query

            docs = []
            scanned = 0
            _t0 = time.monotonic()
            _failed_here = False
            _was_cancelled = False

            # (STREAMING SCORE) When active, docs are scored in NumPy batches
            # as they arrive and below-threshold docs are dropped at once, so
            # the whole corpus is never held in RAM. Only docs with no
            # embedding are kept raw (the lazy backfill needs them).
            _st = _stream_ctx
            _s_pairs = []
            _s_batch_emb = []
            _s_batch_docs = []
            _s_consumed = 0
            _s_new = 0
            _s_known = set()
            if _st is not None and scan_state is not None and scan_state.get("delta_mode"):
                _s_known = {c[0] for c in (scan_state.get("carry") or []) if c and c[0]}

            def _s_flush():
                if not _s_batch_emb:
                    return
                D = np.array(_s_batch_emb, dtype=np.float32)
                d_norms = np.linalg.norm(D, axis=1, keepdims=True)
                d_norms = np.where(d_norms == 0, 1.0, d_norms)
                D = D / d_norms
                scores = (D @ _st["Q"].T).max(axis=1)
                for sc, dd in zip(scores, _s_batch_docs):
                    if float(sc) >= SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD:
                        # embedding is not used after scoring (downstream only
                        # reads title/text/url/platform/subreddit); dropping it
                        # frees ~1536 floats per kept doc.
                        dd.pop("embedding", None)
                        _s_pairs.append((float(sc), dd))
                _s_batch_emb.clear()
                _s_batch_docs.clear()

            try:
                if _cancelled():
                    raise _ScanCancelled()
                cursor = (
                    collection.find(fetch_query, fetch_projection)
                    .sort("created_utc", -1)
                    .batch_size(SIGNAL_EMBEDDING_FETCH_BATCH)
                )
                _apply_find_max_time(cursor, _find_max_ms)
                for doc in cursor:
                    scanned += 1
                    if scanned % 1000 == 0 and _cancelled():
                        raise _ScanCancelled()
                    if scan_state is not None:
                        _w = _doc_watermark(doc, _wm_field)
                        if _w is None and _wm_field == "_id":
                            _bad_id = True
                        if _w is not None and (_max_wm is None or _w > _max_wm):
                            _max_wm = _w
                        _c = _aware_utc(doc.get("created_utc"))
                        if _c is not None and (_max_created is None or _c > _max_created):
                            _max_created = _c
                    if SIGNAL_EMBEDDING_MAX_SCAN > 0 and scanned > SIGNAL_EMBEDDING_MAX_SCAN:
                        log.warning(
                            f"{label}: SIGNAL_EMBEDDING_MAX_SCAN={SIGNAL_EMBEDDING_MAX_SCAN} "
                            f"reached after scanning {scanned - 1} docs; retrieval is intentionally "
                            f"bounded — some time-window docs were not scored."
                        )
                        break
                    if _st is None:
                        docs.append(doc)
                        continue

                    # ---- streamed path: same per-doc rules as the scoring loop
                    # that runs over raw_docs when streaming is off ----
                    _s_consumed += 1
                    if _s_known and _first_present(doc, _URL_FIELD_CANDIDATES) in _s_known:
                        continue                       # overlap re-read (delta dedupe)
                    _s_new += 1
                    emb = doc.get("embedding")
                    if not emb:
                        docs.append(doc)               # lazy-backfill candidate
                        continue
                    if cutoff is not None:
                        doc_created = doc.get("created_utc")
                        if not isinstance(doc_created, datetime):
                            continue
                        if doc_created.tzinfo is None:
                            doc_created = doc_created.replace(tzinfo=timezone.utc)
                        if doc_created < cutoff:
                            continue
                        if cutoff_end is not None and doc_created >= cutoff_end:
                            continue
                    if not isinstance(emb, (list, tuple)):
                        continue
                    if len(emb) != _st["dim"]:
                        log.warning(
                            f"embedding dim mismatch (expected {_st['dim']}, "
                            f"got {len(emb)}) for {doc.get('post_url', '?')} — skipping"
                        )
                        continue
                    _s_batch_emb.append(emb)
                    _s_batch_docs.append(doc)
                    if len(_s_batch_emb) >= _st["batch"]:
                        _s_flush()
                if _st is not None:
                    _s_flush()
            except _ScanCancelled:
                _was_cancelled = True
                _failed_here = True
                log.warning(f"{label}: scan cancelled after {scanned} docs (collection treated as incomplete)")
            except Exception as exc:
                _failed_here = True
                if _is_exec_timeout(exc):
                    log.warning(f"{label}: Mongo find hit MONGO_FIND_MAX_TIME_MS / network timeout "
                                f"after {scanned} docs: {exc}")
                else:
                    log.warning(f"{label}: unlimited-mode cursor failed: {exc}")
                if _st is not None:
                    try:
                        _s_flush()      # score what was already read, as before
                    except Exception as flush_exc:
                        log.warning(f"{label}: final batch score failed: {flush_exc}")

            _elapsed = time.monotonic() - _t0
            if _was_cancelled:
                # Cancelled/deadline: what was collected is NOT a complete scan
                # of this collection — discard it and record nothing.
                _fetch_failures.append(label)
                return []

            with _scan_lock:
                if _cancelled():
                    # The deadline / a cancel fired while this worker was
                    # finishing: nothing is recorded and the collection is
                    # failed (never a silent partial "complete" scan).
                    _fetch_failures.append(label)
                    return []
                if _failed_here:
                    _fetch_failures.append(label)
                if scan_state is not None:
                    scan_state["fetched"][label] = _s_consumed if _st is not None else len(docs)
                    # A failed/partial collection must NOT advance its watermark.
                    if not _failed_here:
                        if _max_wm is not None:
                            scan_state["new_wm"][label] = _max_wm
                        if _max_created is not None:
                            scan_state["new_wm_created"][label] = _max_created
                    if _bad_id:
                        scan_state["bad_id"] = True
                if _st is not None:
                    _stream_out[label] = {
                        "pairs": _s_pairs, "consumed": _s_consumed, "new": _s_new,
                    }

            if _st is not None:
                log.info(
                    f"{label} streamed-score: scanned={_s_consumed} kept={len(_s_pairs)} "
                    f"batch={_st['batch']} elapsed={_elapsed:.2f}s"
                )
                log.info(f"{label} fetched={_s_consumed} elapsed={_elapsed:.2f}s (scanned={scanned})")
                return docs
            if len(docs) > 100000:
                log.warning(
                    f"{label}: fetched={len(docs)} docs — very large result set; "
                    f"consider setting SIGNAL_EMBEDDING_MAX_SCAN to bound fetch cost."
                )
            log.info(
                f"{label} fetched={len(docs)} elapsed={_elapsed:.2f}s "
                f"(scanned={scanned})"
            )
            return docs

        # ── LEGACY HYBRID MODE (CANDIDATE_POOL > 0) ───────────────────────
        docs, seen = [], set()

        def _absorb(cursor_docs):
            for doc in cursor_docs:
                key = doc.get("post_url") or doc.get("url") or doc.get("link") or id(doc)
                if key in seen:
                    continue
                seen.add(key)
                docs.append(doc)

        # TIER 1 — lexical/relevance-selected, any age.
        if lexical_or:
            try:
                _lex_cursor = (
                    collection.find(
                        {"$and": [mongo_query, {"$or": lexical_or}]}, _pool_projection
                    )
                    .sort("created_utc", -1)
                    .limit(SIGNAL_EMBEDDING_CANDIDATE_POOL)
                )
                _apply_find_max_time(_lex_cursor, _find_max_ms)
                _absorb(_lex_cursor)
            except Exception as exc:
                if _is_exec_timeout(exc):
                    _fetch_failures.append(label)
                log.warning(f"{label}: lexical candidate tier failed (falling back to recency tier only): {exc}")

        # TIER 2 — the original recency window, unchanged.
        try:
            _rec_cursor = (
                collection.find(mongo_query, _pool_projection)
                .sort("created_utc", -1)
                .limit(SIGNAL_EMBEDDING_RECENCY_POOL)
            )
            _apply_find_max_time(_rec_cursor, _find_max_ms)
            _absorb(_rec_cursor)
        except Exception as exc:
            if _is_exec_timeout(exc):
                _fetch_failures.append(label)
            log.warning(f"{label}: recency candidate tier failed: {exc}")

        return docs

    # (PER-PHRASE EMBEDDING MATCHING FIX / SCAN RESILIENCE) The query-embedding
    # preparation below is the ORIGINAL block, unchanged in what it does (same
    # cache read, same single batched generate call, same cache write); it was
    # only wrapped in a memoized function so it can run BEFORE the collection
    # scan (needed to score while streaming) yet still run once, at its old
    # place, when streaming is off.
    _qe_memo = {}

    def _prep_query_embeddings():
        if "v" in _qe_memo:
            return _qe_memo["v"]
        # (PER-PHRASE EMBEDDING MATCHING FIX) Build the query ITEMS list for
        # this call: every keyword AND every match_phrase, kept as SEPARATE
        # items — never joined into one combined string. Each item gets its
        # own embedding below (via generate_query_embeddings_batch(), one
        # batched OpenAI call), so a document only has to strongly match ONE
        # of these items to be picked up, instead of being scored against a
        # single diluted/averaged embedding of everything combined.
        query_items = list(keyword_list) + list(phrase_list)

        # (QUERY-EMBEDDING CACHING) Reuse a previously-generated, saved list
        # of query embeddings for this (chat_id, topic_key) pair when it was
        # built from this exact same set of keywords + match_phrases —
        # avoids hitting the OpenAI embeddings API on every repeated call for
        # the same topic (e.g. a ~2s polling loop). Entirely best-effort and
        # fail-safe — any Mongo hiccup here just falls through to the
        # original, uncached generate_query_embeddings_batch() call below,
        # exactly as if this caching layer didn't exist.
        #
        # (KEYING-MISMATCH FIX) Keyed on {"chat_id": chat_id, "topic_key":
        # topic_key} — the SAME document get_cached_topic_evidence() /
        # save_topic_evidence_cache() already read/write for this topic, so
        # the embedding cache and the evidence-posts cache can never end up
        # on two different documents. When no chat_id is given at all, this
        # entire caching layer is skipped (no Mongo read/write) and a fresh,
        # uncached set of embeddings is generated instead — never writes an
        # ambiguous, chat-less document.
        #
        # (PER-PHRASE EMBEDDING MATCHING FIX) The cached/stored
        # "query_embedding" field now holds a LIST of vectors (one per
        # query_items entry, in the same order) instead of a single vector.
        query_signature = _compute_query_signature(keyword_list, phrase_list)
        query_embeddings = None

        if chat_id:
            cached_embedding_doc = None
            try:
                cached_embedding_doc = topic_evidence_cache_collection.find_one(
                    {"chat_id": chat_id, "topic_key": topic_key},
                    {"_id": 0, "query_embedding": 1, "query_embedding_signature": 1},
                )
            except Exception as exc:
                log.warning(f"Query-embedding cache read failed for chat_id={chat_id} topic_key={topic_key}: {exc}")
                cached_embedding_doc = None

            if (
                cached_embedding_doc
                and cached_embedding_doc.get("query_embedding")
                and cached_embedding_doc.get("query_embedding_signature") == query_signature
            ):
                # Already a list-of-lists (per-keyword/per-phrase vectors),
                # saved this same shape by the cache-write branch below.
                query_embeddings = cached_embedding_doc["query_embedding"]

            if not query_embeddings:
                query_embeddings = generate_query_embeddings_batch(query_items)
                if query_embeddings:
                    try:
                        topic_evidence_cache_collection.update_one(
                            {"chat_id": chat_id, "topic_key": topic_key},
                            {"$set": {
                                "chat_id": chat_id,
                                "topic_key": topic_key,
                                "query_embedding": query_embeddings,
                                "query_embedding_signature": query_signature,
                            }},
                            upsert=True,
                        )
                    except Exception as exc:
                        log.warning(f"Query-embedding cache save failed for chat_id={chat_id} topic_key={topic_key}: {exc}")
        else:
            # (KEYING-MISMATCH FIX) No chat_id given at all — skip the
            # embedding cache entirely (no Mongo read, no Mongo write) rather
            # than ever writing a chat-less, ambiguous document. Falls back
            # to generating fresh, uncached, exactly as if this caching
            # feature didn't exist.
            query_embeddings = generate_query_embeddings_batch(query_items)
        _qe_memo["v"] = query_embeddings
        return query_embeddings

    # (PARALLEL FETCH) In unlimited mode, the three collections are fetched
    # concurrently via ThreadPoolExecutor(max_workers=3). pymongo cursors are
    # thread-safe when each thread uses its own cursor (which they do here —
    # each future calls _fetch_candidate_pool on a different collection object).
    # Each fetch wraps its own try/except inside _fetch_candidate_pool, so a
    # single failing collection never drops the others. Result order is
    # preserved: primary first, then _2, then _4 (same as the old sequential
    # order) — futures are submitted in that order and results collected in
    # submission order.
    # In legacy mode (CANDIDATE_POOL > 0), the fetches remain sequential
    # because the ThreadPoolExecutor is still used but the behaviour inside
    # _fetch_candidate_pool is unchanged (no timing difference at typical
    # legacy pool sizes of 500-1000 docs).
    _collections_to_fetch = [
        (signals_collection, "signals_collection"),
    ]
    if signals_collection_2 is not None:
        _collections_to_fetch.append((signals_collection_2, "signals_collection_2"))
    if signals_collection_4 is not None:
        _collections_to_fetch.append((signals_collection_4, "signals_collection_4"))

    # (STREAMING SCORE) Active only in UNLIMITED mode with STREAMING_SCORE_ENABLED.
    # Query embeddings must exist BEFORE the scan so each batch can be scored as
    # it arrives; no usable query embeddings => [] without scanning anything.
    # Legacy mode (CANDIDATE_POOL > 0) and the flag-off path are untouched.
    if (
        SIGNAL_EMBEDDING_CANDIDATE_POOL <= 0 and SIGNAL_EMBEDDING_RECENCY_POOL <= 0
        and _cfg_value("STREAMING_SCORE_ENABLED", True)
    ):
        _qe_early = _prep_query_embeddings()
        if not _qe_early or not _normalize_query_embeddings(_qe_early):
            _register_scan_status(chat_id, topic_key, True, scored=False)   # nothing was read or scored
            return []
        try:
            _raw_q_s = [q for q in _qe_early if q and len(q) > 0]
            _Qs = np.array(_raw_q_s, dtype=np.float32)
            _qn_s = np.linalg.norm(_Qs, axis=1, keepdims=True)
            _qn_s = np.where(_qn_s == 0, 1.0, _qn_s)
            _Qs = _Qs / _qn_s
            _stream_ctx = {
                "Q": _Qs, "dim": _Qs.shape[1],
                # rows buffered per NumPy scoring flush: the explicit SIGNAL_EMBEDDING_MONGO_SCORE_BATCH, else the legacy
                # min(SIGNAL_EMBEDDING_FETCH_BATCH, 2000) (see _mongo_score_batch)
                "batch": _mongo_score_batch(),
            }
        except Exception as exc:
            log.warning(f"streaming score unavailable (numpy query matrix failed), using fetch-then-score: {exc}")
            _stream_ctx = None

    # (MYSQL READ) MySQL databases as additional sources (labels mysql_<db>).
    # Flag off -> nothing added, nothing imported.
    if _cfg_value("MYSQL_READ_ENABLED", False):
        for _msrc in _mysql_sources_for_scan(_stream_ctx):
            _collections_to_fetch.append((_msrc, _msrc.label))

    try:
        _deadline_s = float(_cfg_value("SCAN_DEADLINE_SECONDS", 90) or 0)
    except (TypeError, ValueError):
        _deadline_s = 0.0

    if len(_collections_to_fetch) == 1 and _deadline_s <= 0:
        # Single collection, no deadline — no thread overhead needed.
        raw_docs = _fetch_candidate_pool(*_collections_to_fetch[0])
    else:
        # (SCAN DEADLINE) The executor is managed by hand: a `with` block would
        # call shutdown(wait=True) on exit and sit through every slow worker,
        # defeating the deadline. Futures are waited on with a deadline; the
        # ones that did not finish are recorded as failed, their workers are
        # told to stop (cancel event), and the pool is shut down WITHOUT waiting.
        # A Python thread cannot be killed — the worker itself polls the event.
        raw_docs = []
        _pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(3, len(_collections_to_fetch)))
        _fetch_futures = []
        _unfinished = set()
        _stop_reason = None
        try:
            for _coll, _label in _collections_to_fetch:
                _fetch_futures.append(_pool.submit(_fetch_candidate_pool, _coll, _label))
            _pending = set(_fetch_futures)
            _t_wait = time.monotonic()
            while _pending:
                if _deadline_s > 0:
                    _left = _deadline_s - (time.monotonic() - _t_wait)
                    if _left <= 0:
                        _stop_reason = "deadline"
                        break
                    _slice = min(0.5, _left)
                else:
                    _slice = 0.5
                _done_now, _pending = concurrent.futures.wait(_pending, timeout=_slice)
                if _pending and cancel_event is not None and cancel_event.is_set():
                    _stop_reason = "cancelled"
                    break
            _unfinished = set(_pending)
            if _unfinished:
                with _scan_lock:
                    _deadline_cancel.set()          # workers stop reading
                    for (_coll, _label), _fut in zip(_collections_to_fetch, _fetch_futures):
                        if _fut in _unfinished:
                            _fetch_failures.append(_label)
                            if scan_state is not None:
                                # never advance the watermark of an unfinished collection
                                scan_state.get("new_wm", {}).pop(_label, None)
                                scan_state.get("new_wm_created", {}).pop(_label, None)
                            _stream_out.pop(_label, None)
                            log.warning(f"{_label} scan deadline hit, skipping"
                                        if _stop_reason == "deadline"
                                        else f"{_label} scan cancelled, skipping")
        finally:
            _pool.shutdown(wait=False, cancel_futures=True)

        for (_coll, _label), _fut in zip(_collections_to_fetch, _fetch_futures):
            if _fut in _unfinished:
                continue
            try:
                raw_docs.extend(_fut.result())
            except Exception as exc:
                log.warning(f"{_label} fetch failed (skipping): {exc}")
                _fetch_failures.append(_label)
                _stream_out.pop(_label, None)

        if _stop_reason == "cancelled":
            # Every waiter is gone: nothing useful can be done with a partial
            # scan. Report it as incomplete and stop.
            _fetch_failures[:] = list(dict.fromkeys(_fetch_failures))
            _register_scan_status(chat_id, topic_key, False)
            if scan_state is not None:
                scan_state["complete"] = False
            return []

    _fetch_failures[:] = list(dict.fromkeys(_fetch_failures))
    if scan_state is not None:
        scan_state["failed_labels"] = list(_fetch_failures)

    # (SCAN STATUS) Recorded for the strict wait logic: "complete" means every
    # collection was read without error.
    _register_scan_status(chat_id, topic_key, not _fetch_failures)
    # Streamed results of the collections that finished (labels in submission
    # order, i.e. primary, _2, _4 — the same order raw_docs would have had).
    _stream_pairs = []
    _stream_consumed = 0
    _stream_new = 0
    if _stream_ctx is not None:
        for _coll, _label in _collections_to_fetch:
            _o = _stream_out.get(_label)
            if _o is None:
                continue
            _stream_pairs.extend(_o["pairs"])
            _stream_consumed += _o["consumed"]
            _stream_new += _o["new"]
    if scan_state is not None:
        scan_state["complete"] = not _fetch_failures
        # fetched_total / new_after_dedupe keep their meaning: docs SCANNED
        # (not docs kept after the similarity threshold).
        scan_state["fetched_total"] = (_stream_consumed if _stream_ctx is not None else len(raw_docs))
        if _stream_ctx is not None:
            # overlap re-reads were already dropped inside the workers
            if scan_state.get("delta_mode"):
                scan_state["new_after_dedupe"] = _stream_new
        elif scan_state.get("delta_mode") and raw_docs:
            # Overlap re-reads of posts we already hold are dropped (dedupe by
            # post_url) — only genuinely new URLs count as a delta.
            _known = {c[0] for c in (scan_state.get("carry") or []) if c and c[0]}
            if _known:
                raw_docs = [d for d in raw_docs
                            if _first_present(d, _URL_FIELD_CANDIDATES) not in _known]
            scan_state["new_after_dedupe"] = len(raw_docs)
        if (scan_state.get("delta_mode") and not _fetch_failures
                and (_stream_new == 0 if _stream_ctx is not None else not raw_docs)):
            # EMPTY DELTA: nothing new anywhere -> skip query embedding,
            # scoring, the interpreter and the bridge entirely.
            scan_state["unchanged"] = True
            log.info(
                f"incremental scan: topic_key={topic_key} new_docs=0 -> unchanged "
                f"(matcher scoring/bridge/interpreter skipped)"
            )
            return []

    # (SCAN RESILIENCE) Query embeddings: prepared by _prep_query_embeddings()
    # (defined above the fetch). Already done up-front when streaming score is
    # active; otherwise this is its original place in the flow. Memoized, so
    # the embeddings/cache are never generated or read twice.
    query_embeddings = _prep_query_embeddings()
    if not query_embeddings:
        _mark_scan_unscored(chat_id, topic_key)       # (FRESHNESS) collections were read, nothing was scored
        return []

    # (LAZY EMBEDDING BACKFILL) Find any docs in raw_docs that are missing
    # their embedding vector, embed them now, save to the primary collection,
    # and append to raw_docs so they participate in the scoring pass below.
    # Best-effort: any failure inside is caught and logged — never raises.
    try:
        _backfilled = _lazy_backfill_missing_embeddings(
            raw_docs, signals_collection, generate_query_embeddings_batch
        )
        if _backfilled:
            raw_docs.extend(_backfilled)
    except Exception as _lbe:  # noqa: BLE001
        log.warning(f"lazy_backfill outer guard: {_lbe}")

    # (RETRIEVAL RECALL FIX) Normalize the query vectors ONCE for this
    # call — see _normalize_query_embeddings(). If none survive (all
    # empty/zero-magnitude), behave exactly like the previous "no usable
    # query embeddings" case and return [] rather than scoring everything
    # as 0.0.
    _normalized_queries = _normalize_query_embeddings(query_embeddings)
    if not _normalized_queries:
        _mark_scan_unscored(chat_id, topic_key)       # (FRESHNESS) collections were read, nothing was scored
        return []

    # (UNLIMITED POOL / EMBEDDING-BASED MATCHING) Score every candidate doc
    # against EVERY query embedding and keep the MAX similarity — a document
    # only has to strongly match ONE keyword/phrase to be picked up. Anything
    # below the threshold is not a match at all and is dropped here.
    #
    # UNLIMITED MODE: uses batched numpy vectorized scoring (D @ Q.T) for
    # efficiency — 2000 docs per batch, query matrix built once.
    # LEGACY MODE: keeps the existing per-doc _max_similarity_against() loop
    # unchanged so nothing regresses when positive pool constants are set.
    # (STREAMING SCORE) pairs already scored while fetching come first, then
    # whatever is left in raw_docs (lazy-backfilled docs) is scored below —
    # the same (similarity, doc) pairs, in the same order, as fetch-then-score.
    scored_docs = list(_stream_pairs)

    if SIGNAL_EMBEDDING_CANDIDATE_POOL <= 0 and SIGNAL_EMBEDDING_RECENCY_POOL <= 0:
        # ── Unlimited mode: batched numpy ────────────────────────────────
        _NP_BATCH = 2000
        t_score = time.time()

        # Build normalized query matrix Q (n_q × dim) once.
        try:
            _raw_q = [q for q in query_embeddings if q and len(q) > 0]
            _Q = np.array(_raw_q, dtype=np.float32)
            _q_norms = np.linalg.norm(_Q, axis=1, keepdims=True)
            _q_norms = np.where(_q_norms == 0, 1.0, _q_norms)
            _Q = _Q / _q_norms
            _expected_dim = _Q.shape[1]
        except Exception as exc:
            log.warning(f"numpy query matrix build failed, falling back to per-doc scoring: {exc}")
            _Q = None
            _expected_dim = None

        if _Q is not None:
            _batch_emb  = []
            _batch_docs = []

            def _flush_np_batch():
                if not _batch_emb:
                    return
                D = np.array(_batch_emb, dtype=np.float32)
                d_norms = np.linalg.norm(D, axis=1, keepdims=True)
                d_norms = np.where(d_norms == 0, 1.0, d_norms)
                D = D / d_norms
                scores = (D @ _Q.T).max(axis=1)          # (batch,)
                for s, d in zip(scores, _batch_docs):
                    if float(s) >= SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD:
                        scored_docs.append((float(s), d))
                _batch_emb.clear()
                _batch_docs.clear()

            for doc in raw_docs:
                # Defensive time-window check (same as legacy path below).
                if cutoff is not None:
                    doc_created = doc.get("created_utc")
                    if not isinstance(doc_created, datetime):
                        continue
                    if doc_created.tzinfo is None:
                        doc_created = doc_created.replace(tzinfo=timezone.utc)
                    if doc_created < cutoff:
                        continue
                    if cutoff_end is not None and doc_created >= cutoff_end:
                        continue

                emb = doc.get("embedding")
                if not emb or not isinstance(emb, (list, tuple)):
                    continue
                if len(emb) != _expected_dim:
                    log.warning(
                        f"embedding dim mismatch (expected {_expected_dim}, "
                        f"got {len(emb)}) for {doc.get('post_url', '?')} — skipping"
                    )
                    continue

                _batch_emb.append(emb)
                _batch_docs.append(doc)
                if len(_batch_emb) >= _NP_BATCH:
                    _flush_np_batch()

            _flush_np_batch()  # final partial batch

            log.info(
                f"unlimited scoring | pool={len(raw_docs)} "
                f"passed_threshold={len(scored_docs)} "
                f"score_elapsed={time.time() - t_score:.2f}s"
            )
        else:
            # numpy build failed — fall back to per-doc path
            for doc in raw_docs:
                if cutoff is not None:
                    doc_created = doc.get("created_utc")
                    if not isinstance(doc_created, datetime):
                        continue
                    if doc_created.tzinfo is None:
                        doc_created = doc_created.replace(tzinfo=timezone.utc)
                    if doc_created < cutoff:
                        continue
                    if cutoff_end is not None and doc_created >= cutoff_end:
                        continue
                similarity = _max_similarity_against(doc.get("embedding"), _normalized_queries)
                if similarity >= SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD:
                    scored_docs.append((similarity, doc))

    else:
        # ── Legacy mode: per-doc _max_similarity_against() ───────────────
        for doc in raw_docs:
            # (TIME-WINDOW FEATURE) Defensive second check: if a cutoff is
            # active, make sure this doc's own created_utc actually satisfies
            # it too (guards against a doc with a missing/odd created_utc
            # slipping through the Mongo-level filter in some edge case) — a
            # doc with no usable created_utc is excluded rather than assumed
            # to pass, since we can't confirm it's within the window.
            if cutoff is not None:
                doc_created = doc.get("created_utc")
                if not isinstance(doc_created, datetime):
                    continue
                if doc_created.tzinfo is None:
                    doc_created = doc_created.replace(tzinfo=timezone.utc)
                if doc_created < cutoff:
                    continue
                if cutoff_end is not None and doc_created >= cutoff_end:
                    continue

            doc_embedding = doc.get("embedding")
            # (RETRIEVAL RECALL FIX) Mathematically identical to the previous
            # max(_cosine_similarity(doc_embedding, qe) for qe in
            # query_embeddings) — same MAX semantics, same threshold, same
            # result — but using the query vectors normalized once above
            # instead of recomputing every magnitude for every document.
            similarity = _max_similarity_against(doc_embedding, _normalized_queries)
            if similarity < SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD:
                continue

            scored_docs.append((similarity, doc))

    # (INCREMENTAL RESCAN) Delta mode: if none of the NEW docs clears the
    # similarity threshold the final selection cannot change — return
    # without running the bridge/interpreter. Otherwise merge the previously
    # selected posts back in (with their remembered similarities) so the
    # downstream sort/bridge/cap/limit sees exactly what a full rescan sees.
    if scan_state is not None and scan_state.get("delta_mode"):
        if not scored_docs:
            scan_state["unchanged"] = True
            log.info(
                f"incremental scan: topic_key={topic_key} new_docs={len(raw_docs)} "
                f"none above threshold -> unchanged"
            )
            return []
        _have = set()
        for _sim_c, _doc_c in scored_docs:
            _u = _first_present(_doc_c, _URL_FIELD_CANDIDATES)
            if _u:
                _have.add(_u)
        for _u, _sim_c, _post in (scan_state.get("carry") or []):
            if _u and _u not in _have:
                scored_docs.append((float(_sim_c), _post))

    # Sort by similarity, highest first, BEFORE per-platform cap / overall
    # limit are applied — so the best-matching posts are always kept.
    # (RECENCY) Similarity stays the primary key; a small freshness bonus from the post's validated PUBLICATION time lets a
    # recent post overtake an older one of comparable similarity BEFORE the per-platform cap / evidence limit / intent-bridge
    # candidate cap can drop it. Unknown dates get no bonus (never "fresh"). RECENCY_RANK_WEIGHT=0 = the old ordering.
    _rk_now = datetime.now(timezone.utc)
    scored_docs.sort(key=lambda pair: pair[0] + _recency_bonus(pair[1], _rk_now), reverse=True)

    # (EMBEDDING-MATCH QUERY/LOGGING CLEANUP) Purely additive, observability-
    # only debug logging — guarded by log.isEnabledFor() so the extra
    # similarity computation across the whole raw_docs pool (done here
    # only for the log line's own top-5 view, separate from scored_docs'
    # threshold-filtered scores above) never runs at all unless DEBUG
    # logging is explicitly enabled. Never touches scored_docs, matched,
    # or any return value — silent, side-effect-free otherwise.
    if log.isEnabledFor(logging.DEBUG):
        try:
            if _stream_ctx is not None:
                # the raw pool was streamed away; below-threshold scores are gone
                all_scores = [sc for sc, _d in scored_docs[:5]]
            else:
                all_scores = sorted(
                    (
                        _max_similarity_against(doc.get("embedding"), _normalized_queries)
                        for doc in raw_docs
                    ),
                    reverse=True,
                )
            log.debug(
                f"Embedding-match debug | topic_key={topic_key} | pool_size={len(raw_docs)} "
                f"| similarity_threshold={SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD} "
                f"| passed_threshold={len(scored_docs)} | top5_scores={all_scores[:5]}"
            )
        except Exception as exc:
            log.debug(f"Embedding-match debug logging failed for topic_key={topic_key}: {exc}")

    # ── (EVIDENCE-BUDGET THROTTLE FIX) ──────────────────────────────────
    # MAX_POSTS_PER_PLATFORM (default 3) is a DISPLAY-balance rule: it
    # exists so one platform can't fill every post card. But this same
    # function's output is ALSO the analysis evidence handed to Claude,
    # sized by the router's own evidence_required planner (15-100, see
    # MIN/MAX_ANALYSIS_EVIDENCE). Applying a flat cap of 3 per platform to
    # that path meant a carefully-planned budget of, say, 60 posts
    # silently returned 3 for a Reddit-dominant corpus — starving the
    # analysis and pushing Claude toward "no_results"/weak findings even
    # when plenty of relevant posts had been matched.
    #
    # Display capping already happens downstream and is untouched
    # (MAX_CHAT_EVIDENCE_POSTS = 7 in the analysis prompt, and
    # flintel.merge_matched_and_google_results()'s own cap), so the
    # balance intent is preserved without throttling retrieval: the cap
    # now SCALES with the requested limit instead of being flat. Small
    # limits keep the original behavior exactly (limit<=6 -> 3 per
    # platform, unchanged), larger evidence budgets get proportionally
    # more headroom.
    effective_max_per_platform = max(MAX_POSTS_PER_PLATFORM, limit // 2)
    _trunc_hit = []   # appended to whenever a cap/limit/pool bound excluded a candidate
    # (STRICT) A request that is about ONE platform (dropdown or named in the
    # query) is not subject to the display-balance cap: "25 Reddit posts"
    # must be able to return 25 Reddit posts. Flag off: line above stands.
    _strict = strict_intent_mode()
    if _strict and _single_platform_query(user_query, targeting_platform):
        effective_max_per_platform = max(effective_max_per_platform, limit)

    def _apply_cap_and_limit(candidates, cap, n):
        """Apply per-platform cap and overall limit to an ordered candidate list.

        Extracted so both the narrow path (flag off) and the bridge path
        (flag on, applied to bridge-reranked output) share identical logic.
        Returns a new list; never mutates the input.
        """
        out = []
        pc = {}
        seen = set()
        for item in candidates:
            pkey = (item.get("platform") or "unknown").strip().lower()
            url  = item.get("post_url")
            if url and url in seen:
                continue
            if pc.get(pkey, 0) >= cap:
                _trunc_hit.append(1)
                continue
            if url:
                seen.add(url)
            out.append(item)
            pc[pkey] = pc.get(pkey, 0) + 1
            if len(out) >= n:
                break
        return out

    # ── (INTENT BRIDGE HOOK) ─────────────────────────────────────────────────
    # When INTENT_BRIDGE_ENABLED=true and a user_query is present, collect a
    # WIDE pool first (pool_n candidates, no per-platform cap during
    # collection), pass it to the intent bridge for re-ranking, then apply the
    # per-platform cap and limit to the bridge-ordered result.
    #
    # When the flag is off (or user_query is empty), the narrow path runs:
    # it is structurally identical to the previous implementation — same loop,
    # same cap, same limit, same dedup — so flag-off output is line-for-line
    # unchanged.
    #
    # Fail-safe: any exception at any step returns the narrow-path result
    # (similarity-ranked, cap+limit already applied). The bridge NEVER crashes
    # the production request path and NEVER returns fewer posts.
    try:
        from config import (  # noqa: PLC0415
            INTENT_BRIDGE_ENABLED,
            INTENT_CANDIDATE_MULTIPLIER,
            INTENT_CANDIDATE_MIN,
            INTENT_CANDIDATE_MAX,
        )
        # STRICT forces the bridge on: without it there is no intent filter.
        _bridge_active = bool(
            (INTENT_BRIDGE_ENABLED or _strict) and user_query and user_query.strip()
        )
    except Exception:
        _bridge_active = False

    _sim_by_url = {}   # post_url -> similarity (remembered for incremental rescan)

    if _bridge_active:
        # ── Wide-collection path (flag on) ───────────────────────────────
        # Step 1: collect up to pool_n candidates without per-platform cap.
        pool_n = max(
            INTENT_CANDIDATE_MIN,
            min(limit * INTENT_CANDIDATE_MULTIPLIER, INTENT_CANDIDATE_MAX),
        )

        wide_matched = []
        wide_sims    = []
        wide_seen    = set()

        for _similarity, doc in scored_docs:
            title     = _first_present(doc, _TITLE_FIELD_CANDIDATES)
            post_text = _first_present(doc, _TEXT_FIELD_CANDIDATES)

            if not _signal_platform_matches(doc, targeting_platform):
                continue
            # (TEXT-REQUIRED AT PICK-TIME) post_text is mandatory — a doc
            # with no post_text is never counted as a match, even if it
            # cleared the embedding-similarity threshold. This check has to
            # live HERE, inside the matching loop, rather than later in
            # build_claude_post_context() (logics.py's own Claude-context
            # builder): doing it here means an evidence_required budget
            # (e.g. 50) is always sized against text-guaranteed posts, and
            # the timeout/Google-fallback "did we actually find anything"
            # check (which looks at whether this function's own result is
            # empty) is always judged against real, analyzable posts rather
            # than title-only stubs that would silently get dropped
            # downstream anyway.
            if not post_text:
                continue

            post_url = _first_present(doc, _URL_FIELD_CANDIDATES)
            platform = (
                _first_present(doc, _PLATFORM_FIELD_CANDIDATES)
                or _infer_platform_from_url(post_url)
            )

            if not title and not post_text and not post_url:
                continue
            if post_url and post_url in wide_seen:
                continue

            # No per-platform cap here — bridge needs the full diversity.
            if post_url:
                wide_seen.add(post_url)

            wide_matched.append(timerange.with_published_at(   # (DATE/TIME) original publication time
                {"title": title, "post_text": post_text, "post_url": post_url, "platform": platform,
                 "subreddit": _signal_subreddit(doc, post_url)}, doc)
            )
            wide_sims.append(_similarity)
            if post_url:
                _sim_by_url[post_url] = _similarity

            if len(wide_matched) >= pool_n:
                _trunc_hit.append(1)
                break

        # Step 2: call intent bridge; fall back to wide_matched on error.
        try:
            from intent_bridge import rerank_with_intent  # noqa: PLC0415
            _bridge_kwargs = {"topic_sims": wide_sims}
            if (chat_id and topic_key and _cfg_value("INCREMENTAL_RESCAN_ENABLED", True)
                    and _accepts_kw(rerank_with_intent, "plan_key")):
                # interpreter plan cached per (chat_id, topic_key) — no extra
                # LLM call on every poll.
                _bridge_kwargs["plan_key"] = (chat_id, topic_key)
            bridge_ordered = rerank_with_intent(
                user_query, wide_matched, limit,
                **_bridge_kwargs,
            )
        except Exception as _bridge_exc:
            log.warning(f"intent_bridge hook failed (non-fatal): {_bridge_exc}")
            bridge_ordered = wide_matched

        # Step 3: apply per-platform cap + overall limit to bridge output.
        matched = _apply_cap_and_limit(bridge_ordered, effective_max_per_platform, limit)

    else:
        # ── Narrow path (flag off / no query) — identical to pre-bridge code ──
        matched = []
        seen_urls = set()
        platform_counts = {}  # (v7) per-platform running count for this call

        for _similarity, doc in scored_docs:
            title     = _first_present(doc, _TITLE_FIELD_CANDIDATES)
            post_text = _first_present(doc, _TEXT_FIELD_CANDIDATES)

            if not _signal_platform_matches(doc, targeting_platform):
                continue

            # (TEXT-REQUIRED AT PICK-TIME) post_text is mandatory — a doc
            # with no post_text is never counted as a match, even if it
            # cleared the embedding-similarity threshold. This check has to
            # live HERE, inside the matching loop, rather than later in
            # build_claude_post_context() (logics.py's own Claude-context
            # builder): doing it here means an evidence_required budget
            # (e.g. 50) is always sized against text-guaranteed posts, and
            # the timeout/Google-fallback "did we actually find anything"
            # check (which looks at whether this function's own result is
            # empty) is always judged against real, analyzable posts rather
            # than title-only stubs that would silently get dropped
            # downstream anyway.
            if not post_text:
                continue

            post_url  = _first_present(doc, _URL_FIELD_CANDIDATES)
            platform  = _first_present(doc, _PLATFORM_FIELD_CANDIDATES) or _infer_platform_from_url(post_url)

            if not title and not post_text and not post_url:
                continue
            if post_url and post_url in seen_urls:
                continue

            # (v7) Per-platform cap: once a platform has already contributed
            # MAX_POSTS_PER_PLATFORM matches to this call, skip any further
            # matches from that same platform (but keep scanning raw_docs —
            # a different platform may still have room).
            platform_key = (platform or "unknown").strip().lower()
            if platform_counts.get(platform_key, 0) >= effective_max_per_platform:
                _trunc_hit.append(1)
                continue

            if post_url:
                seen_urls.add(post_url)

            matched.append(timerange.with_published_at(   # (DATE/TIME) original publication time
                {"title": title, "post_text": post_text, "post_url": post_url, "platform": platform,
                 "subreddit": _signal_subreddit(doc, post_url)}, doc))
            if post_url:
                _sim_by_url[post_url] = _similarity
            platform_counts[platform_key] = platform_counts.get(platform_key, 0) + 1

            if len(matched) >= limit:
                break

    if scan_state is not None:
        scan_state["sims"] = {
            m["post_url"]: _sim_by_url[m["post_url"]]
            for m in matched if m.get("post_url") in _sim_by_url
        }
        scan_state["truncated"] = bool(_trunc_hit) or len(matched) >= limit
    return matched


# ─────────────────────────────────────────────────────────────────────────────
# TOPIC EVIDENCE CACHE (per-chat, per-topic post reuse)
#
# A caching layer ON TOP OF get_matched_signals() — never a reimplementation
# of its matching rules. When the same topic (chat_id + topic_key) is asked
# about repeatedly in the same chat, this avoids re-querying Mongo and
# re-sending Claude the exact same posts on every follow-up: cached posts
# are reused as-is when they already satisfy the evidence budget, and only
# the DELTA is fetched (via the real get_matched_signals(), passed in as
# matcher_fn) when more depth is genuinely requested. Cache-awareness is
# wired in index.py/routes.py, at the call sites that already call
# get_matched_signals() directly — get_matched_signals() itself is
# completely untouched by this feature.
# ─────────────────────────────────────────────────────────────────────────────

def get_cached_topic_evidence(chat_id: str, topic_key: str) -> Optional[dict]:
    """Reads the already-cached evidence posts for this chat + topic.
    Returns None if there's nothing cached yet (first time this topic is
    asked about in this chat)."""
    if not chat_id or not topic_key:
        return None
    try:
        return topic_evidence_cache_collection.find_one(
            {"chat_id": chat_id, "topic_key": topic_key}, {"_id": 0}
        )
    except Exception as exc:
        log.warning(f"Topic-evidence cache read failed for topic_key={topic_key}: {exc}")
        return None


def save_topic_evidence_cache(chat_id: str, owner_key: str, topic_key: str,
                                posts: list, keywords: list, match_phrases: list = None,
                                extra_fields: dict = None):
    """Upserts the cache — the FULL posts list is overwritten each time
    (the caller has already merged old + new posts before calling this).

    (CACHE TRUNCATION ORDER FIX) get_evidence_with_topup() always builds
    `posts` as [older cached posts..., newer freshly-fetched posts...] —
    so when the list needs to be capped, keeping the FIRST N would keep
    the OLDEST posts and silently drop the newest, most-recently-
    discovered ones once a topic's evidence count reaches the cap. Slicing
    from the end instead keeps the LAST N (i.e. the newest) posts.

    `extra_fields` (default None => document identical to before) lets the
    caller add small fields to the same $set, e.g. {"strict_mode": True}."""
    if not chat_id or not topic_key:
        return
    capped_posts = (posts or [])[-TOPIC_CACHE_MAX_EVIDENCE:]
    now = datetime.now(timezone.utc)
    set_doc = {
        "chat_id": chat_id,
        "topic_key": topic_key,
        "owner_key": owner_key,
        "posts": capped_posts,
        "post_urls_seen": [p.get("post_url") for p in capped_posts if p.get("post_url")],
        "evidence_count": len(capped_posts),
        "keywords": keywords or [],
        "match_phrases": match_phrases,
        "updated_at": now,
    }
    if extra_fields:
        set_doc.update(extra_fields)
    try:
        topic_evidence_cache_collection.update_one(
            {"chat_id": chat_id, "topic_key": topic_key},
            {"$set": set_doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception as exc:
        log.warning(f"Topic-evidence cache save failed for topic_key={topic_key}: {exc}")


def _prepare_scan_state(cached, chat_id, topic_key, keywords, match_phrases,
                        targeting_platform, since_days, user_query, strict,
                        matcher_fn, unfiltered, fetch_limit=None):
    """Builds the in/out dict for get_matched_signals(scan_state=...), or None
    for a plain full scan. Never raises.

    A delta (watermarks + carried posts) is built only when ALL hold:
      - INCREMENTAL_RESCAN_ENABLED, not an `unfiltered` query, a chat_id, and
        a matcher that accepts `scan_state`;
      - the cache doc holds a watermark recorded for this exact query
        signature + watermark field, plus a remembered similarity for EVERY
        cached post;
      - it is not the periodic forced full scan (every Nth poll, N =
        INCREMENTAL_FULL_SCAN_EVERY_N_POLLS) — late-ingested/late-embedded
        docs are invisible to an _id/created_utc watermark, so a complete
        scan is repeated now and then. An explicitly configured ingestion
        field is trusted the same way; set N=0 to disable the periodic scan.
    Otherwise the state is empty (still returned, so the first full scan
    RECORDS watermarks for the next poll)."""
    try:
        if not _cfg_value("INCREMENTAL_RESCAN_ENABLED", True):
            return None
        if unfiltered or not chat_id or not topic_key:
            return None
        if not _accepts_kw(matcher_fn, "scan_state"):
            return None

        field = _watermark_field()
        sig = _scan_signature(keywords, match_phrases, targeting_platform, since_days,
                              user_query, strict, field)
        state = {"sig": sig, "field": field}

        poll_no = _next_poll_number(chat_id, topic_key)
        every_n = int(_cfg_value("INCREMENTAL_FULL_SCAN_EVERY_N_POLLS", 10) or 0)
        if every_n > 0 and poll_no % every_n == 0:
            state["why_full"] = "periodic_full_scan"
            return state

        stored_wm = (cached or {}).get("scan_watermark")
        stored_sims = (cached or {}).get("scan_sims")
        stored_field = (cached or {}).get("scan_field")
        if (
            not isinstance(stored_wm, dict) or not stored_wm
            or (cached or {}).get("scan_sig") != sig
            or stored_field not in (field, "created_utc")   # created_utc = _id fallback
            or not isinstance(stored_sims, list)
        ):
            state["why_full"] = "no_valid_watermark"
            return state
        # A previous result that was cut by a limit/cap/pool bound can only be
        # extended by a full scan when the new limit is larger: posts excluded
        # then are neither carried nor in the delta.
        prev_limit = (cached or {}).get("scan_limit")
        if (
            (cached or {}).get("scan_truncated", True)
            and fetch_limit is not None
            and not (isinstance(prev_limit, int) and fetch_limit <= prev_limit)
        ):
            state["why_full"] = "limit_grew_after_truncation"
            return state
        state["field"] = stored_field

        sim_by_url = {}
        for pair in stored_sims:
            try:
                sim_by_url[pair[0]] = float(pair[1])
            except Exception:
                continue
        carry = []
        for post in (cached or {}).get("posts") or []:
            url = post.get("post_url")
            if not url or url not in sim_by_url:
                state["why_full"] = "missing_sim_for_cached_post"
                return state
            carry.append((url, sim_by_url[url], post))

        wms = {}
        for label, value in stored_wm.items():
            aware = _aware_utc(value)
            if aware is not None:
                wms[label] = aware
        if not wms:
            state["why_full"] = "unreadable_watermark"
            return state
        state["watermarks"] = wms
        state["carry"] = carry
        return state
    except Exception as exc:
        log.warning(f"incremental scan: state preparation failed, using full scan: {exc}")
        return None


def _persist_scan_state(chat_id, topic_key, scan_state, merged, cached, elapsed):
    """Stores the new watermark + remembered similarities on the existing
    topic-evidence cache doc. ONE small update, and only when something is
    new (first scan, newly fetched docs, or an advanced watermark). Any
    failure is logged and ignored: the next poll then simply does a full scan."""
    try:
        field = scan_state.get("field") or "_id"
        new_wm = scan_state.get("new_wm") or {}
        if scan_state.get("bad_id"):
            # _id is not an ObjectId in this data -> created_utc is the only
            # usable clock (late-ingested old posts are then caught by the
            # periodic full scan).
            new_wm = scan_state.get("new_wm_created") or {}
            field = "created_utc"
        fetched = scan_state.get("fetched") or {}
        total_new = sum(int(v or 0) for v in fetched.values())
        if scan_state.get("new_after_dedupe") is not None:
            total_new = int(scan_state["new_after_dedupe"])   # overlap re-reads aren't "new"
        log.info(
            "incremental scan: " + " ".join(
                f"collection={lbl} new_docs={cnt}" for lbl, cnt in fetched.items()
            ) + f" elapsed={elapsed:.2f}s "
            f"(watermark={ {k: v.isoformat() for k, v in (new_wm or {}).items()} }, "
            f"mode={'delta' if scan_state.get('delta_mode') else 'full:' + str(scan_state.get('why_full', 'first_scan'))})"
        )
        if not scan_state.get("complete", False) or scan_state.get("failed_labels"):
            return                      # a collection failed/was cancelled: never advance
        if not new_wm and (cached or {}).get("scan_watermark"):
            return                      # nothing new, nothing to advance

        old_wm = (cached or {}).get("scan_watermark") or {}
        merged_wm = {}
        for label in set(list(old_wm) + list(new_wm)):
            old_v = _aware_utc(old_wm.get(label))
            new_v = _aware_utc(new_wm.get(label))
            candidates = [v for v in (old_v, new_v) if v is not None]
            if candidates:
                merged_wm[label] = max(candidates)
        advanced = (not old_wm) or any(
            _aware_utc(old_wm.get(k)) != v for k, v in merged_wm.items()
        )
        if not advanced and total_new == 0:
            return

        sims = dict(scan_state.get("sims") or {})
        old_sims = {}
        for pair in (cached or {}).get("scan_sims") or []:
            try:
                old_sims[pair[0]] = float(pair[1])
            except Exception:
                continue
        pairs = []
        for post in (merged or [])[-TOPIC_CACHE_MAX_EVIDENCE:]:
            url = post.get("post_url")
            if not url:
                continue
            sim = sims.get(url, old_sims.get(url))
            if sim is not None:
                pairs.append([url, float(sim)])

        set_doc = {
            "chat_id": chat_id,
            "topic_key": topic_key,
            "scan_watermark": merged_wm,
            "scan_field": field,
            "scan_sig": scan_state.get("sig"),
            "scan_sims": pairs,
            "scanned_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        if not scan_state.get("unchanged"):
            set_doc["scan_limit"] = scan_state.get("_fetch_limit")
            set_doc["scan_truncated"] = bool(scan_state.get("truncated", True))
        topic_evidence_cache_collection.update_one(
            {"chat_id": chat_id, "topic_key": topic_key},
            {"$set": set_doc},
            upsert=True,
        )
    except Exception as exc:
        log.warning(f"incremental scan: persisting watermark failed (next poll = full scan): {exc}")


def _since_days_window(since_days, now=None):
    """(start, end) UTC of a `since_days` lookback measured at `now` (default: the current time, read once), or None when the
    search is not bounded. The window is half-open: start <= t < end, end = now + the Step 1 future tolerance."""
    if isinstance(since_days, bool) or not isinstance(since_days, (int, float)) or since_days <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=min(since_days, MAX_TIME_WINDOW_DAYS)), now + timerange.FUTURE_TOLERANCE


def _in_window(post, window, now=None) -> bool:
    """True only for a post whose VALIDATED publication time (Step 1 contract) lies inside the half-open `window`; undated,
    unreadable, future and pre-floor dates are unknown and never inside a bounded window. The post is validated against the
    SAME clock reading as the window (`now`, default: the window's own end minus the tolerance), so validation and window
    cannot disagree about a boundary instant."""
    ref = now or (window[1] - timerange.FUTURE_TOLERANCE)
    when = timerange.post_published_at(post, now=ref)
    return when is not None and window[0] <= when < window[1]


def google_stubs_apply(targeting_platform) -> bool:
    """Google-discovered stubs are Reddit records: they may be merged into an answer only when the request's platform selection
    includes Reddit ("all" or a list containing it)."""
    sel = platforms.selection_key(targeting_platform)
    return sel == "all" or "reddit" in sel.split(",")


def _cache_platform_ok(cached, targeting_platform) -> bool:
    """(Phase 2.2) A topic-evidence cache record may only serve the platform selection it was collected for. New records
    store `platform_key` (platforms.selection_key). A record without one predates the registry, when Reddit was the only
    platform ever collected: it is accepted for "reddit" and "all" selections only."""
    want = platforms.selection_key(targeting_platform)
    stored = (cached or {}).get("platform_key")
    if stored is None:
        return want in ("all", "reddit")
    return stored == want


def usable_cached_posts(cached, targeting_platform=None, since_days=None, time_range=None) -> list:
    """The posts of a topic-evidence cache record that may be served for THIS request: right platform selection, and, for a
    bounded request, inside its window (same rules as get_evidence_with_topup). Never raises."""
    try:
        if not cached or not _cache_platform_ok(cached, targeting_platform):
            return []
        posts = list(cached.get("posts") or [])
        tr = time_range if isinstance(time_range, timerange.TimeRange) else timerange.range_from_doc(time_range)
        if tr is not None:
            return timerange.filter_posts(posts, tr)[0]
        window = _since_days_window(since_days)
        return [p for p in posts if _in_window(p, window)] if window is not None else posts
    except Exception:
        return []


def get_evidence_with_topup(chat_id: str, owner_key: str, topic_key: str,
                              keywords: list, evidence_required: int,
                              matcher_fn, match_phrases: list = None,
                              targeting_platform: str = "all",
                              since_days: int = None, unfiltered: bool = False,
                              user_query: str = None,
                              cancel_event: "threading.Event" = None,
                              time_range=None, provenance_out: dict = None) -> list:
    """CORE FUNCTION — instead of running a fully-fresh query every time
    the same topic is asked about again:
      1. Check the cache first.
      2. If the cache already has evidence_required (or more) posts,
         return those cached posts as-is — no new Mongo query at all.
      3. If the cache is short (or empty), fetch only the DELTA of new
         evidence needed (matcher_fn gets a limit of
         max(evidence_required, cached_count + TOPIC_CACHE_MIN_TOPUP)),
         de-duplicate the old + new posts, update the cache ONLY if that
         actually added something new (skips a wasted write when a
         repeated poll finds nothing new yet), and return the full
         merged list.

    `matcher_fn` is get_matched_signals() itself, passed in via dependency
    injection, so this function never duplicates its own matching-query
    logic — it is purely the caching/top-up decision layer on top of it.

    (KEYING-MISMATCH FIX) `chat_id` is now also passed straight through to
    matcher_fn() as its own `chat_id` keyword argument, so get_matched_
    signals()'s own QUERY-EMBEDDING CACHING block shares the exact same
    {"chat_id": chat_id, "topic_key": topic_key} document this function's
    own evidence-posts cache (get_cached_topic_evidence() / save_topic_
    evidence_cache()) already reads/writes — the two caches can never end
    up on two different Mongo documents for the same (chat, topic) pair.
    Nothing else about this function changed."""
    strict = strict_intent_mode()
    # (FRESHNESS) get_matched_signals() returns [] WITHOUT searching when it is given no keywords and no
    # phrases (unless the query is "unfiltered"); that call must not be recorded as a search.
    _can_search = True
    if provenance_out is not None:         # only when tracking: with the feature off this line does nothing
        try:
            _can_search = bool(unfiltered) or any(k for k in (keywords or [])) or any(p for p in (match_phrases or []))
        except Exception:
            _can_search = True
    # (DATE/TIME) A message with an explicit publication range never reuses or
    # writes the topic cache / incremental-scan state: those are shared by every
    # message with the same text in this chat and know nothing about ranges
    # ("today" asked again tomorrow must not be served yesterday's posts).
    _tr = time_range if isinstance(time_range, timerange.TimeRange) else timerange.range_from_doc(time_range)
    if _tr is not None:
        _call_kwargs = dict(
            targeting_platform=targeting_platform, since_days=None, unfiltered=unfiltered,
            match_phrases=match_phrases, limit=max(evidence_required or MIN_ANALYSIS_EVIDENCE, TOPIC_CACHE_MIN_TOPUP),
            signals_collection_2=signals_collection_2, chat_id=chat_id,
            signals_collection_4=signals_collection_4, user_query=user_query,
        )
        if _accepts_kw(matcher_fn, "time_range"):
            _call_kwargs["time_range"] = _tr
        if cancel_event is not None and _accepts_kw(matcher_fn, "cancel_event"):
            _call_kwargs["cancel_event"] = cancel_event
        _pm = _prov_marker(chat_id, topic_key) if provenance_out is not None else None
        posts, stats = timerange.filter_posts(matcher_fn(topic_key, keywords, **_call_kwargs), _tr)
        _prov_note(provenance_out, "range", chat_id, topic_key, _pm, range_stats=stats, searchable=_can_search,
                   cancelled=bool(provenance_out is not None and cancel_event is not None and cancel_event.is_set()),
                   scan_urls=_prov_urls(provenance_out, posts))   # (FRESHNESS) write-only
        if stats["outside"] or stats["no_timestamp"]:
            log.info(f"Time-range filter | topic_key={topic_key} | kept={stats['kept']} "
                     f"outside={stats['outside']} no_timestamp={stats['no_timestamp']}")
        return posts
    cached = get_cached_topic_evidence(chat_id, topic_key)
    if strict and cached is not None and not cached.get("strict_mode"):
        # Evidence cached before strict mode was switched on was never
        # intent-filtered; it must not be served as "qualified" posts.
        cached = None
    if cached is not None and not _cache_platform_ok(cached, targeting_platform):
        # (PLATFORM) the same text and keywords asked for another platform selection: that evidence belongs to a different
        # search. It is neither served nor merged (the next save overwrites it with this selection's posts).
        cached = None
    if (cached is not None and cached.get("posts") and "keywords" in cached
            and _compute_query_signature(cached.get("keywords"), cached.get("match_phrases"))
            != _compute_query_signature(keywords, match_phrases)):
        # (MULTI-TURN CONTEXT) topic_key is the raw message text, so a short
        # follow-up ("now in USA") repeated later in the same chat for a
        # DIFFERENT search shares the key. Posts cached for other
        # keywords/phrases are another search's evidence — never reuse them
        # (same rule the query-embedding cache on this document already
        # applies via query_embedding_signature). A legacy doc that never
        # recorded its keywords is left to the old behaviour.
        cached = None
    cached_posts = (cached or {}).get("posts") or []
    # (RECENCY, STEP 2.1) A bounded lookback (since_days: an explicit period or the documented default) applies to CACHED
    # posts exactly as it does to a fresh scan: only posts with a VALIDATED publication time inside the window survive.
    # Without this a cache hit that merely met `evidence_required` bypassed the window and served stale / undated posts.
    # Fewer valid posts than required -> the normal top-up below runs; the result is never padded with out-of-window posts.
    _window = _since_days_window(since_days)
    if _window is not None and cached_posts:
        _kept = [p for p in cached_posts if _in_window(p, _window)]
        if len(_kept) != len(cached_posts):
            log.info(f"Topic cache window filter | topic_key={topic_key} | kept={len(_kept)} dropped={len(cached_posts) - len(_kept)} "
                     f"since_days={since_days}")
            cached = dict(cached or {}, posts=_kept)     # the scan state's carried posts follow the same filter
            cached_posts = _kept
    cached_count = len(cached_posts)

    if cached_count >= (evidence_required or MIN_ANALYSIS_EVIDENCE):
        # Cache already has enough — no new fetch needed.
        _prov_note(provenance_out, "saved_only", saved_updated_at=(cached or {}).get("updated_at"),
                   saved_count=cached_count)           # (FRESHNESS) write-only: no search ran
        return cached_posts

    # A top-up is needed — fetch at least TOPIC_CACHE_MIN_TOPUP more than
    # what's already cached, even if the raw delta would be smaller.
    fetch_limit = max(
        evidence_required or MIN_ANALYSIS_EVIDENCE,
        cached_count + TOPIC_CACHE_MIN_TOPUP,
    )

    # (INCREMENTAL RESCAN) After the first full scan, later polls read only
    # docs newer than the stored per-collection watermark. None => a plain
    # full scan (flag off, unfiltered query, matcher without support, no
    # usable stored state, or a forced periodic full scan).
    scan_state = _prepare_scan_state(
        cached, chat_id, topic_key, keywords, match_phrases, targeting_platform,
        since_days, user_query, strict, matcher_fn, unfiltered, fetch_limit,
    )
    _call_kwargs = dict(
        targeting_platform=targeting_platform,
        since_days=since_days, unfiltered=unfiltered,
        match_phrases=match_phrases, limit=fetch_limit,
        signals_collection_2=signals_collection_2, chat_id=chat_id,
        signals_collection_4=signals_collection_4,
        user_query=user_query,
    )
    if scan_state is not None:
        _call_kwargs["scan_state"] = scan_state
    # (SCAN RESILIENCE) Passed only to a matcher that accepts it, and only when
    # a cancel event was actually given — every other call is unchanged.
    if cancel_event is not None and _accepts_kw(matcher_fn, "cancel_event"):
        _call_kwargs["cancel_event"] = cancel_event
    _t_scan = time.monotonic()
    _pm = _prov_marker(chat_id, topic_key) if provenance_out is not None else None
    fresh_posts = matcher_fn(topic_key, keywords, **_call_kwargs)
    _prov_note(provenance_out, "scan", chat_id, topic_key, _pm, searchable=_can_search,
               cancelled=bool(provenance_out is not None and cancel_event is not None and cancel_event.is_set()),
               scan_urls=_prov_urls(provenance_out, fresh_posts, cached_posts),
               saved_updated_at=(cached or {}).get("updated_at"), saved_count=cached_count)   # (FRESHNESS) write-only

    # De-dup: old cached posts + new posts, keyed on post_url.
    seen_urls = {p.get("post_url") for p in cached_posts if p.get("post_url")}
    merged = list(cached_posts)
    for post in fresh_posts:
        url = post.get("post_url")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        merged.append(post)

    # (REDUNDANT-WRITE FIX) Only write to the cache when something
    # genuinely new was actually added. Without this, a caller that
    # polls repeatedly while nothing new is being found yet (e.g.
    # stream_answer()'s own polling loop, which calls this function
    # every ~2s while waiting for evidence to show up) would trigger a
    # full Mongo write on every single poll, even when the top-up fetch
    # keeps returning nothing new — this skips that write while still
    # always returning the correct, up-to-date merged list.
    if len(merged) > cached_count:
        save_topic_evidence_cache(
            chat_id, owner_key, topic_key, merged, keywords, match_phrases,
            extra_fields=dict({"platform_key": platforms.selection_key(targeting_platform)},
                              **({"strict_mode": True} if strict else {})),
        )
    if scan_state is not None:
        scan_state["_fetch_limit"] = fetch_limit
        _persist_scan_state(
            chat_id, topic_key, scan_state, merged, cached, time.monotonic() - _t_scan,
        )
    if _window is not None:        # (RECENCY, STEP 2.1) nothing outside the active window leaves this function
        merged = [p for p in merged if _in_window(p, _window)]
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS LAYER (v4, ANALYST-PROMPT UPGRADE)
#
# Matched signals never get dumped to the user directly. They're handed to
# Claude (title + text ONLY — never post_url, never platform, never job
# internals) together with the user's actual chat prompt, and Claude turns
# that into the final answer.
#
# Only the FINAL ANSWER TEXT is ever stored (see save_claude_answer_to_chat
# below) — the input posts are never re-saved next to it, since they
# already live in flintel_signals / are reconstructable from the message's
# own keyword list. That's the cost-saving rule: store output only.
#
# COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP OR THE TIME-WINDOW /
# PAIN-POINT / CLARIFY FEATURE / ANALYST-PROMPT UPGRADE: this whole section
# (CLAUDE_ANALYSIS_SYSTEM_PROMPT, build_claude_post_context(), chunk_list(),
# _format_posts_block(), _map_chunk(), _call_claude()) only ever consumes
# ALREADY-MATCHED posts (the output of get_matched_signals()) — it has no
# idea, and doesn't care, which keyword list or time window produced those
# matches. The ANALYST-PROMPT UPGRADE only changes the CONTENT of the
# system prompt string itself (i.e. how Claude is instructed to write up
# those posts) — every function in this section, and the map-reduce
# machinery around it, is otherwise byte-for-byte unchanged.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_ANALYSIS_SYSTEM_PROMPT = """
You are the Market Intelligence Analysis Engine inside Flintel, a
social-listening platform. You are handed a user's research question
plus real public posts (title + text only — never a URL, platform, or
internal job/keyword data) that were retrieved for it.

YOUR JOB IS NOT to summarize or list what posts say. Showing posts is
NOT the product — a user can already get raw posts from Google or
Reddit search directly; that gives them zero reason to use Flintel.

YOUR JOB IS to behave like a market intelligence analyst: understand the
research objective behind the question, read every post for what it
actually reveals, extract the real signal, find the patterns across
posts, separate fact from inference, and deliver a structured,
evidence-based report that explains what the evidence MEANS — not what
each post literally said. Posts themselves are supporting proof, shown
last, in minimal form — never the main content of your answer.

──────────────────────────────────────────────────────────────────────────
STEP 1 — UNDERSTAND THE RESEARCH OBJECTIVE

Before analyzing anything, work out (internally, never written out):
what is the user actually trying to learn? Buyer intent? Pain points?
Market trends? Competitor intelligence? Product demand? Sentiment?
Operational complaints? Opportunities? Read for the real objective
behind the wording, not just the literal keywords — people phrase the
same underlying question in endless different ways.

──────────────────────────────────────────────────────────────────────────
STEP 2 — EVIDENCE BUDGET IS GIVEN, NOT CHOSEN BY YOU

You are given however many posts the system retrieved (this can be
anywhere from a handful to close to a hundred) — you do not control or
request that number. More posts does NOT mean a better report:
- Analyze every post that is genuinely relevant and usable.
- Ignore posts that are clearly irrelevant — do not force them in.
- Never manufacture a finding out of a weak or unrelated post just to
  pad the finding count.
- When several posts repeat the same underlying point, combine them
  into ONE pattern/finding — never list the same point three separate
  times because three posts happened to say it.

──────────────────────────────────────────────────────────────────────────
STEP 3 — ANALYZE EACH RELEVANT POST

For every post that matters to the research objective, determine:

Evidence type (classify internally, use in your findings where useful):
- Direct Evidence — explicitly describes the exact problem, demand,
  intent, or behavior being researched.
- Indirect Evidence — reveals the underlying issue through a related
  experience, without stating it head-on.
- Contextual Evidence — adds broader market context without directly
  proving the specific question asked.
- Builder/Provider Evidence — comes from someone building or selling a
  solution, describing their own experience.
- Buyer Evidence — comes from someone actively searching for,
  comparing, requesting, or purchasing a solution.
These are not equally strong — weight them accordingly, and say so when
it matters to the finding.

Signal strength — label each finding descriptively, never numerically:
Strong / Moderate / Weak, based on how directly and consistently the
evidence supports it.

──────────────────────────────────────────────────────────────────────────
STEP 4 — EXTRACT ONLY WHAT ANSWERS THE QUESTION

From relevant posts, pull out: specific problems, repeated complaints,
customer needs, buying signals, requested solutions, pricing concerns,
reliability issues, operational friction, feature requests, competitor
mentions, workflow problems, adoption barriers, recurring language, and
evidence for or against demand — but ONLY when it actually answers
"why is this useful for the user's research question?" Do not extract a
detail just because it appeared in a post.

──────────────────────────────────────────────────────────────────────────
STEP 5 — FIND PATTERNS, DON'T REPEAT EVIDENCE

Compare posts against each other. If multiple posts point at the same
underlying issue (e.g. one mentions high pricing, another mentions
expensive per-message costs, another mentions affordability concerns),
that is ONE finding — "Pricing pressure / affordability" — referencing
all of them together, never three separate findings repeating the same
point.

──────────────────────────────────────────────────────────────────────────
STEP 6 — SEPARATE FACT FROM INTERPRETATION (NON-NEGOTIABLE)

- Evidence: what the posts literally say or show.
- Finding: the pattern reasonably inferred from multiple pieces of
  evidence.
- Interpretation: what that pattern may mean for the market.
Never present an inference as if a post said it directly. Never claim
more certainty than the evidence supports.

──────────────────────────────────────────────────────────────────────────
STEP 7 — PAIN POINT ≠ BUYING INTENT (CRITICAL DISTINCTION)

"This tool is too expensive" is a PAIN POINT. It is NOT evidence that
someone wants to buy a cheaper alternative — do not conflate the two.
"I built my own automation because existing tools were expensive" shows
market friction / a builder response, not proven buyer intent either.
Only label something as buyer intent when the evidence actually shows:
actively searching for a solution, asking for recommendations, asking
which product to choose, requesting vendors, comparing providers, asking
about pricing to purchase, or explicitly stating intent to buy/adopt.
Maintain this distinction everywhere in the report — never blur pain
signals into demand/intent signals.

──────────────────────────────────────────────────────────────────────────
STEP 8 — GROUNDING (overrides everything above)

Every factual claim must come from the posts you were given. Never
invent a post, a stat, a quote, or a sentiment that isn't actually
supported by what's in front of you. If you were given ANY relevant
posts (evidence_count > 0), you MUST analyze and answer from them —
never respond as if you have no data when posts were actually provided.
Read every post you were given before deciding what to include. Only
use "no_results" when the posts are genuinely empty or genuinely
irrelevant after reading all of them — never as a shortcut because there
were few or weak posts; few-but-relevant posts still get analyzed
honestly.

SENTIMENT IS A LABEL, NOT A SELECTION FILTER (unless the user explicitly
asks for one): topic/intent relevance is always the dominant criterion
for which posts/findings you include — never sentiment. Do not chase a
spread of sentiments for variety's own sake. The only exception: if the
user's own query explicitly asks for a sentiment-scoped result ("only
complaints", "positive reviews only"), then sentiment becomes a real
filter for that request.

CONVERSATION CONTINUITY: if a short summary of earlier turns is included
below (labeled "Conversation so far"), read it before writing any
suggestion or follow-up. If the user already explicitly declined or
narrowed away from a broader scope, location, platform, or term in an
earlier turn, do not re-suggest that same thing. If, after honoring that
narrower scope, there's genuinely nothing relevant, say so plainly
instead of re-offering what was already declined.

──────────────────────────────────────────────────────────────────────────
DEPTH LADDER — NON-NEGOTIABLE (this is the GOLDEN RULE)

Depth always comes from the actual quantity/quality of evidence you were
given — never from an assumption that "the response should be long."

- 1-2 genuinely relevant posts -> a short, honest answer. Give the full
  value of what you found, but say explicitly that this is only one or
  two signals — never manufacture a fake broader pattern out of it.
  Keep executive_summary to no more than 2-3 sentences, key_findings to
  1 entry (2 at most), and OMIT market_pattern entirely (there genuinely
  isn't a broader pattern in this case).
- 3-10 relevant posts, one angle -> 1-2 genuine findings, each grounded
  in its own evidence. Do not write a drawn-out executive_summary for
  this much evidence.
- 10+ relevant posts, multiple angles -> up to 3 key_findings plus a
  short market_pattern; detailed_findings only if the user asks for
  detail (see RESPONSE SIZE CONTROL).

Never stretch thin evidence into something long. Never short-change
strong evidence either. Depth per finding comes from the weight of its
evidence, not from a fixed template.

──────────────────────────────────────────────────────────────────────────
CONCISENESS DISCIPLINE — NON-NEGOTIABLE

Every section of your report must say something ONCE, in its most useful
form — never restate the same point in different words across multiple
fields. This is the single most common quality failure: writing the same
insight three times (once in a finding's "impact", again in
"detailed_findings", again in "market_pattern" or "conclusion") using
different phrasing. That is NOT thoroughness — it is repetition, and it
must be eliminated.

Concretely:
- "impact" (inside key_findings) states the business implication in ONE
  tight sentence — not two, not three.
- "what_evidence_shows" and "why_it_matters" (inside detailed_findings)
  together should read as a single tight paragraph, not a restatement of
  what key_findings already said. If detailed_findings would just repeat
  key_findings in longer form, do not include it — key_findings alone is
  enough for that report's depth level.
- "market_pattern" synthesizes ACROSS findings — it must add a genuinely
  new observation (the throughline connecting the findings), never
  summarize each finding again one by one.
- "conclusion" is the practical takeaway only — it never repeats the
  executive_summary, never repeats market_pattern, never re-lists the
  findings. If you find yourself writing sentences that could be deleted
  without losing any actual information, delete them before responding.

Before finalizing your answer, silently check: does any sentence in this
report just re-say something already said elsewhere in different words?
If yes, cut it. A tighter, cleaner report that respects the reader's time
is always better than an exhaustive one that repeats itself. Aim for
roughly 40% shorter than your first instinct, achieved by CUTTING
repetition and padding — never by cutting real, distinct findings or
grounding evidence.

──────────────────────────────────────────────────────────────────────────
PREDICTION / INTERPRETATION DISCIPLINE

"market_pattern" and any other forward-looking interpretation are
OPTIONAL, never mandatory. Only write one when the evidence genuinely
supports it — present signal compared against a past baseline pointing
to a grounded direction (e.g. "the current data shows X, the earlier
baseline was Y, so this suggests Z is trending up") — and always hedge
it: "suggests", "indicates", never stated as settled fact.

If the evidence is too thin or too scattered to support a genuine
direction, keep "market_pattern" short or OMIT it completely — never
write a filler prediction just to fill the field. Not every single query
needs a forced prediction.

──────────────────────────────────────────────────────────────────────────
RESPONSE SIZE CONTROL — DEFAULT IS MEDIUM (overrides the depth ladder)

Default to a MEDIUM-length report a reader can act on in about a minute.
The analysis logic above stays the same; only how much is written out changes.
- headline: 4-9 words naming what was found. Never an instruction.
- research_objective: one short sentence (internal — never shown to the customer).
- executive_summary: 2-3 sentences, hard cap. This IS the answer.
- key_findings: at most 3 (2 is better when evidence is thin), each "impact" ONE short sentence.
- detailed_findings: empty for 1-2 relevant posts or a simple question.
  With 3+ relevant posts, up to 3 items — but ONLY when each one adds
  concrete specifics from the posts (budgets, use cases, constraints,
  what exactly was asked) that the key_findings table doesn't already
  say. If the user asks for detail / deep dive / full report, include
  them (max 3).
- market_pattern: 1 sentence only when genuinely supported, else omit.
- conclusion: optional, 1 sentence, only if it adds something new. It is
  shown at the end WITHOUT a heading, so never write "In conclusion".
- business_insight: only when the user is clearly selling something or
  asking for leads/customers/positioning, max 2 sentences, plus at most
  4 business_insight_points.
- Each post "summary": max 12-15 words.
- followup_question: one short question.
- Total target: a reader should finish the whole report in about 40 seconds.
Scale to the user's prompt: simple question -> shorter answer;
"detail"/"deep"/"poori detail" -> expanded. Never pad.

──────────────────────────────────────────────────────────────────────────
LEAD / CUSTOMER-FINDING REQUESTS — NEVER REFUSE THESE

Requests like "find users/customers/leads for my business or website",
"who needs this", "help me grow my business" ARE valid social-listening
searches. Flintel never profiles individuals — it reads public
conversation and reports buyer-intent and pain-point signals. So:
- NEVER use "disallowed" or "not_available" for these requests.
  "disallowed" is ONLY for a request to identify, profile, or dig up a
  specific NAMED person.
- If relevant posts exist: use "source_list", read the posts as
  buyer/pain signals, and use "business_insight" (max 2 sentences) for how
  the business could position around them. Never claim the posts are a
  prospect list.
- If the posts are unrelated to the business: use "no_results" with ONE
  plain sentence saying no relevant buyer conversation was found, plus the
  usual suggested_actions. Never explain what Flintel cannot do, and never
  lecture.
- NEVER recommend anything outside Flintel: no CRM tools, B2B databases,
  forums, communities, or other platforms.

──────────────────────────────────────────────────────────────────────────
OUTPUT CONTRACT — STRICT JSON ONLY, no markdown code fences, no
preamble, no text outside the JSON object. `"format"` is ALWAYS the
first field. Pick exactly one of the six formats below.

THIS IS ABSOLUTE: the very FIRST character of your entire response must
be `{` — nothing before it. Do not narrate your own reasoning anywhere,
in any form, before, inside (outside a JSON string value), or after the
JSON object. Do not wrap the JSON in a code fence. The very LAST
character must be `}`. If you feel the urge to explain your thinking
before responding, that urge is the signal to stop and output the JSON
object instead.

──────────────────────────────────────────────────────────────────────────
FORMAT 1 — "source_list"
The primary analyst-report format, used for sentiment/opinion/demand/
pain-point/general research queries.
{
  "format": "source_list",
  "headline": "<4-9 words stating WHAT WAS FOUND, e.g. 'Reddit buyers actively looking for a car' — a finding, never an instruction like 'Identify Reddit users who...'>",
  "research_objective": "<internal only, never shown: one short sentence on what this analysis is trying to answer>",
  "executive_summary": "<the answer itself, 2-3 sentences: lead with what was found (state the honest count of posts you list), then the single most important pattern. May be split into two short paragraphs with a blank line. You may bold 1-3 key phrases with **double asterisks**. Never restate the user's request back to them.>",
  "findings_heading": "<short heading for the findings table that fits THIS question, e.g. 'What buyers want', 'The main complaints', 'What people are comparing'>",
  "key_findings": [
    {
      "finding": "<plain-language name of the pattern, a few words, e.g. 'Tight budgets and monthly-payment limits'>",
      "evidence_type": "<Direct|Indirect|Contextual|Builder/Provider|Buyer> (internal — not shown as a label)",
      "signal_strength": "<Strong|Moderate|Weak> (shown as a small 'Strength' column)",
      "impact": "<ONE plain sentence: what the posts show and why it matters — say how many posts support it when it is only one or two>",
      "supporting_post_indices": [<int>, <int>]
    }
  ],
  "details_heading": "<optional heading for detailed_findings that fits the question, e.g. 'The clearest opportunities' — omit when detailed_findings is empty>",
  "detailed_findings": [
    {
      "title": "<short name, matching one from key_findings>",
      "what_evidence_shows": "<the concrete specifics from those posts — budgets, use cases, constraints, what exactly was asked. Specific, not a longer restatement of the table row>",
      "why_it_matters": "<optional ONE sentence implication that the table row did not already say — omit this field rather than repeat>",
      "supporting_post_indices": [<int>, <int>]
    }
  ],
  "market_pattern": "<ONE sentence, only if genuinely supported by the evidence, else omit this field entirely>",
  "conclusion": "<optional, ONE sentence, only if it adds something new. Shown without a heading. Never repeat the summary or findings.>",
  "followup_question": "<ONE genuinely useful next-step question that moves the research forward — never a list of 3>",
  "platforms": [
    {
      "platform": "<reddit|x|linkedin|facebook>",
      "total_analyzed": <int — honest count of relevant posts actually found for this platform>,
      "shown_count": <int — how many are in "posts" below>,
      "posts": [
        {
          "index": <int — matches the numbers used in supporting_post_indices above>,
          "source": "<subreddit/handle/page name>",
          "title": "<post title, or a short label if none>",
          "summary": "<max 12-15 words, minimal paraphrase — this is a reference, not the analysis>",
          "sentiment": "<positive|mixed|negative|neutral>",
          "role": "<buyer|discussion|competitor_promo|testimonial|irrelevant — see POST LABELS>",
          "purchase_intent": "<explicit|research|none>",
          "product_fit": "<fits|partial|unrelated>",
          "evidence": "<a short quote copied word for word from THIS post's title or text that supports its labels — omit if none>",
          "link": "<real post URL if available, else omit this field entirely>",
          "google_rank": <int, omit unless this post came from the supplementary Google search>
        }
      ]
    }
  ],
  "business_insight_heading": "<OPTIONAL short heading, e.g. 'How to reach these buyers' — omit together with business_insight>",
  "business_insight": "<OPTIONAL — see instructions below>",
  "business_insight_points": ["<OPTIONAL, at most 4 short concrete actions, each tied to a finding above>"]
}
Only include platforms that actually returned usable data. Set
"ranked": true at the top level (alongside "format") instead of the
default false when the user asked for something specific and ordered
(e.g. "top 10 complaints") — same schema, but posts are ordered by
rank/relevance.

The "posts" list under "platforms" is EVIDENCE/REFERENCE material only —
it exists so the user can verify where a finding came from, not to
re-tell the user what each post says. Keep each post's own "summary"
field to one short sentence; all the real analysis lives in
"key_findings" / "detailed_findings" / "market_pattern" / "conclusion"
above it, never in the post list itself.

REFERENCES RULE: every "supporting_post_indices" entry must correspond
to a real post you were actually given — never invent an index, a URL,
an author, or a date. If source metadata (platform, title) isn't
available for a post, omit that detail rather than fabricating it.
Only cite a post in any text field ("Post N") if that same post is listed
in "posts" with that index; never cite a post you did not list.

POST LABELS (each listed post): "role" — buyer (the author asks for a tool,
service or solution for themselves), discussion (general talk, advice,
strategy or hypothetical questions), competitor_promo (someone presenting
or launching their own product), testimonial (someone describing a tool
they already use), irrelevant. "purchase_intent" — explicit only when the
author asks for a solution AND states an explicit purchase step: an approved
or allocated budget (not just an amount), a committed decision to buy, sign,
subscribe or switch, or a procurement process under way. Hypothetical,
conditional, negated, paused or merely exploratory statements are not
explicit. A request for recommendations, a budget amount, a deadline, a
trial, demo or pilot, a quote request, or willingness to pay is research,
alone or combined. "product_fit" — whether the
author's need is one the user's product or topic addresses; general
marketing questions and unrelated use cases are unrelated (do not list
them). Competitor promotions and testimonials are market context, never
buyer demand: never count them as buyers, leads or demand, and never rate
a demand finding Strong on them.

OPTIONAL FIELD — "business_insight" (source_list format only): after
grounding everything above strictly in the evidence, you MAY add a
short analyst take — what this pattern might suggest about emerging
demand and how a business could position around it. This is explicitly
your own reasoning layered ON TOP of the grounded data — phrase it as
interpretation (e.g. "These posts suggest...", "From a positioning
standpoint..."), never as another grounded fact. Omit entirely when a
business angle isn't naturally relevant.
"business_insight_points" (optional, max 4): short, concrete actions,
each traceable to a specific finding above (e.g. "Show the monthly
payment up front — two posts set a monthly budget"). Never write
invented slogans, ad copy in quotation marks, prices, guarantees or
statistics, and never present a suggestion as something the posts said.

──────────────────────────────────────────────────────────────────────────
FORMAT 2 — "trend_report"
For "how has sentiment changed over time" / "sentiment over the last N
days" queries. Same analyst depth applies to "interpretation" and
"trend" below — explain what's driving the shift and what it means, not
just the raw numbers.
{
  "format": "trend_report",
  "topic": "<brand/topic name>",
  "window": "<e.g. 'Last 30 days'>",
  "platforms": "<comma-separated platforms actually covered>",
  "shift_table": {
    "headers": ["Metric", "<period start label>", "<period end label>", "Change"],
    "rows": [["Positive mentions", "52%", "61%", "+9 pts"], ...]
  },
  "interpretation": "<what's driving the shift and what it means, grounded in the posts — depth scales with how much real signal exists>",
  "weekly_table": {
    "headers": ["Week", "Positive", "Neutral", "Negative", "Notable Events"],
    "rows": [["Week 1 (Aug 3-9)", "50%", "35%", "15%", "<short grounded note>"], ...]
  },
  "positive_drivers": [
    {"platform": "<platform>", "post": "<paraphrased post>", "sentiment": "positive", "theme": "<short theme label>"}
  ],
  "negative_drivers": [
    {"platform": "<platform>", "post": "<paraphrased post>", "sentiment": "negative", "theme": "<short theme label>"}
  ],
  "trend": "<trajectory going forward, grounded in what's actually observed>",
  "conclusion": "<what the trend consistently shows and the practical takeaway>",
  "followup_question": "<ONE useful next-step question>"
}
Every row/driver must be grounded in real matched posts — never
fabricate a number you don't have evidence for; omit a field rather than
inventing it.

──────────────────────────────────────────────────────────────────────────
FORMAT 3 — "comparison"
For "Compare X vs Y" queries (2+ subjects). Same analyst-report depth
applies within each subject as "source_list" above.
{
  "format": "comparison",
  "research_objective": "<what this comparison is actually trying to answer>",
  "executive_summary": "<how the subjects differ overall, and why it matters — depth scales with complexity>",
  "subjects": [
    {
      "name": "<subject name>",
      "sentiment": {"positive": "<pct>", "neutral": "<pct>", "negative": "<pct>"},
      "key_findings": [ /* same shape as FORMAT 1's key_findings, scoped to this subject */ ],
      "platforms": [ /* same platforms/posts reference structure as FORMAT 1 */ ]
    }
  ],
  "market_pattern": "<the real difference between the subjects, synthesized>",
  "conclusion": "<what the comparison consistently shows and the practical takeaway>",
  "followup_question": "<ONE useful next-step question>"
}

──────────────────────────────────────────────────────────────────────────
FORMAT 4 — "no_results"
For when little or nothing relevant was actually found.
{
  "format": "no_results",
  "searched": {"query": "<what was searched>", "platforms": ["<...>"], "time_window": "<e.g. 'last 7 days'>"},
  "message": "<plain statement of what was searched and that little/nothing turned up>",
  "likely_reason": "<brief, genuine explanation — e.g. small/new brand, private-group discussion>",
  "suggested_actions": [
    {"type": "broaden_time", "label": "<e.g. 'Extend to last 30 days'>"},
    {"type": "broaden_platforms", "label": "<e.g. 'Include all platforms + news'>"},
    {"type": "broaden_term", "label": "Search a broader term", "suggestion": null},
    {"type": "try_nearest_alternative", "label": "<e.g. 'Try Hyderabad instead?'>", "suggestion": "<nearest alternative term, or omit this entire action if none>"}
  ],
  "clarifying_question": "<only include this field if asking for more context would genuinely help — omit otherwise>",
  "near_match_confidence": "<\\"high\\" | \\"low\\" | null — only present when you were given a set of LOOSER, secondary candidate posts to judge; null when no such candidates were given, or when you genuinely don't think any of them are close to what was asked>",
  "near_match_offer": "<short, professional (not apologetic) sentence stating plainly that there's no exact match for this topic but a looser/adjacent set of posts was found, then asking permission to share them — ONLY include this field when near_match_confidence is \\"low\\">"
}
"suggestion" inside suggested_actions must be null unless there's a
genuinely grounded alternative term to offer — never invent a
plausible-sounding brand/term with no real signal behind it.

NEAREST-ALTERNATIVE SUGGESTION ("try_nearest_alternative"): when nothing
relevant was found for the searched topic, use your own general
knowledge to check whether there is a genuinely CLOSE alternative worth
suggesting:
- If the topic is a LOCATION, suggest the geographically NEAREST
  comparable location — never a distant/unrelated one.
- If the topic is NOT a location, suggest the closest CONCEPTUALLY
  adjacent alternative — something roughly ~90% similar.
- Only include this action when genuinely confident about a real, close
  alternative. If none exists, omit the action entirely — never invent
  one.

CLOSEST-MATCHES TIER-3 REFINEMENT ("near_match_confidence" /
"near_match_offer"): sometimes, alongside a genuinely empty primary
search, you may be given a SEPARATE, SECOND set of looser candidate
posts — found via a broader, best-effort secondary match attempt — for
you to judge.
- If you're genuinely confident (roughly 90%+ close) —
  "near_match_confidence": "high". Write "message" as if these ARE your
  answer's posts — do not hedge or apologize for them.
- If you're not confident enough to show them outright, but they're not
  nothing either — "near_match_confidence": "low". Plainly tell the
  user Flintel does not have an exact match, but a looser/adjacent set
  of posts was found, and ask permission before sharing them. These
  posts are withheld from display until the user says yes in a
  follow-up turn — never describe their content in "message" or
  "likely_reason" in this case.
- If you were given no such candidates, or don't think any are close —
  "near_match_confidence": null, and omit "near_match_offer".

CRITICAL GUARDRAIL FOR "likely_reason" (and every other text field in
this format, and in "not_available"/"disallowed" below): NEVER name,
suggest, or imply any platform, tool, marketplace, directory, search
engine, community, or channel OUTSIDE Flintel as a better place to look.
Phrase "likely_reason" purely in terms of why THIS search came up short,
and let "suggested_actions" (all of which are things FLINTEL ITSELF can
do) be the only next steps offered.

TONE FOR "no_results" (write like a sharp analyst reporting back, not a
form rejection):
- Open by stating plainly WHAT was searched and WHERE.
- Be honest that no strong/high-intent match was found, but frame it as
  information, not failure.
- If ANY posts were matched at all (even loosely relevant, low-intent
  ones), do not discard them — describe what was found in plain terms.
- Never sound like a rejection or a canned apology.
- Never point the user toward a different platform, tool, or channel as
  the place to actually find this.

"NOT RETRIEVED" IS NOT "DOES NOT EXIST" (absolute rule):
You are shown only the posts this ONE search surfaced — never the whole
corpus. You are therefore never in a position to know whether relevant
conversations exist. Report on THIS SEARCH, never on reality:
- Correct: "this search didn't surface anyone actively looking for X."
- FORBIDDEN: "there are no people looking for X", "nobody is discussing
  X", "that demand doesn't exist", or any phrasing that converts an
  empty result set into a claim about the world.
- "likely_reason" must explain why THIS search came up short, and must
  never assert that the underlying demand or conversation is absent.

PREFER RELATED SIGNALS OVER "no_results" (absolute rule):
"no_results" is a last resort, NOT the default whenever nothing matches
the request word-for-word. Before choosing it, re-read every post for
RELATED signal — someone describing the underlying problem without ever
naming the solution, someone evaluating alternatives, someone OFFERING
the thing when the user wanted buyers (or vice versa). If any post
carries genuine related signal, use "source_list" instead and label
those entries explicitly as related/indirect signals rather than
confirmed matches, saying plainly which are direct and which are
inferred. Honestly-labelled related signal is far more useful than
"no_results", and a user must never be told nothing was found while
genuinely related evidence sits in the post list you were handed.
Never inflate in the other direction either: a related signal is never
described as a confirmed buyer, a confirmed lead, or an active
opportunity unless that post's own words support that reading.

──────────────────────────────────────────────────────────────────────────
FORMAT 5 — "not_available"
For capabilities Flintel doesn't support yet (e.g. job listings, anything
outside social-listening).
{
  "format": "not_available",
  "message": "<brief, honest explanation of what isn't available yet and what Flintel can do instead>"
}

CLARIFICATION — "not_available" vs "no_results": "not_available" is
ONLY for things Flintel structurally cannot do at all. A request like
"find me customers for my website/product" IS a valid social-listening
search — if that search runs but finds little or nothing, that is a
"no_results" outcome, NEVER "not_available".

──────────────────────────────────────────────────────────────────────────
FORMAT 6 — "disallowed"
ONLY for requests to identify, profile, or target a specific NAMED
individual person. Never use this for generic "find customers/users/leads"
requests — those are normal searches (see LEAD / CUSTOMER-FINDING REQUESTS).
{
  "format": "disallowed",
  "message": "<brief, non-preachy explanation, redirecting to what Flintel can help with instead>"
}

──────────────────────────────────────────────────────────────────────────
SENTIMENT TAG RULE (applies to every post, in every format above, with no
exception): every individual post object must include a "sentiment"
field set to exactly one of these four lowercase strings —
"positive", "mixed", "negative", "neutral" — and nothing else. Never
omit this field on any post. Never use a free-text or capitalized value.

──────────────────────────────────────────────────────────────────────────
POST-COUNT LIMIT: never include more than 7 posts total, combined across
every platform, in the "platforms" reference list. If given both
grounded posts and discovery-only Google posts, choose the best
combination of up to 7 by genuine relevance — never force an even split,
never pad with a low-quality post just to reach a count. A
discovery-only post's "summary" must say its content hasn't been
fetched yet (e.g. "Content not yet available — found via search, rank
#<n>"), never a fabricated one.

──────────────────────────────────────────────────────────────────────────
NOISE REMOVAL: never repeat a finding twice, never list every post
separately when several support one pattern, never include irrelevant
post details, never invent generic business advice unrelated to the
evidence, never repeat the executive summary inside the conclusion.
Maximum useful intelligence, minimum unnecessary text.

──────────────────────────────────────────────────────────────────────────
RESPONSE FORMAT INTELLIGENCE: choose the format based on what's asked
and what you found, not rigid keyword triggers. If you genuinely cannot
support "source_list", "trend_report", or "comparison" honestly, use
"no_results" instead of forcing a thin answer into one of those shapes.

TONE: write like a trusted research analyst briefing a client —
natural, clear, confident, concise and evidence-led. Never like a
generic chatbot summarizing what it read, and never like a consultant
filling a template. No "As an AI..." framing, no filler openers, no
corporate hedging, and never claim more confidence than the grounding
supports.

HOW TO COMMUNICATE THE FINDINGS (presentation rules — they never change
which posts count, how they are classified, or any rule above):
- Lead with the answer. The headline and the first sentence say what
  Flintel found ("We found 7 Reddit posts from people actively shopping
  for a used car..."), never the request restated as a task ("Identify
  Reddit users who...", "This analysis aims to...").
- Plain, conversational professional English. Short sentences. Bold
  sparingly with **double asterisks** (a few key phrases at most).
- Group related posts into a few meaningful patterns and say why each
  matters — the evidence and the interpretation, each said once.
- Say each point ONCE. The summary, the findings table, the details and
  any closing line must each add something new.
- Quantify honestly from the posts you list: "one post", "two of the
  seven", "most of them". Use "several", "many", "repeatedly", "most" or
  "users keep asking" only when that is literally true of those posts —
  a single post asking something is "one user", never "users repeatedly".
- No time claims ("recent", "this week", "trending") unless dates were
  actually given to you.
- Describe each post's intent exactly as strong as its own words: someone
  weighing whether to finance or buy a beater is "deciding how to pay",
  not "ready to buy"; never imply every person listed will buy.
- Practical next steps only when the evidence supports them and the user
  is selling, positioning or looking for leads/customers.
- Never write report labels in the text: no "Evidence type:", "Signal
  strength:", "Impact:", "Conclusion:", "Business Insight:", "In
  conclusion", "Key takeaway". The interface already shows the
  structure.
- Avoid consultant phrasing: "leverage", "actionable insights", "it is
  worth noting", "in today's landscape", "unlock", "stakeholders",
  "robust opportunity", "game-changer".
- Adapt the length to the question: a simple ask gets a short answer;
  a rich research request can use the fuller structure.
"""

# (ANALYST-PROMPT UPGRADE) These two .replace() calls inject
# MAX_CHAT_EVIDENCE_POSTS into the prompt's own POST-COUNT LIMIT section.
# Their target substrings were UPDATED to match the new prompt's exact
# wording above ("never include more than 7 posts total" /
# "combination of up to 7 by genuine relevance") — the OLD substrings
# ("more than 7 posts total" without "never include" prefix change is
# fine and still matches; but the old second substring
# "up to 7\nbased on genuine relevance" no longer appears anywhere in
# the new prompt text, since the wording changed to "up to 7 by genuine
# relevance" on a single line). Left as a no-op silently, that mismatch
# would have meant every chat response quietly used a hardcoded "7"
# instead of the real MAX_CHAT_EVIDENCE_POSTS config value — so the
# second .replace() target below was corrected to match the new prompt.
CLAUDE_ANALYSIS_SYSTEM_PROMPT = (
    CLAUDE_ANALYSIS_SYSTEM_PROMPT
    .replace("more than 7 posts total", f"more than {MAX_CHAT_EVIDENCE_POSTS} posts total")
    .replace("up to 7 by genuine relevance", f"up to {MAX_CHAT_EVIDENCE_POSTS} by genuine relevance")
)

# (STRICT INTENT MODE) The prompt used when config.STRICT_INTENT_MODE is on.
# CLAUDE_ANALYSIS_SYSTEM_PROMPT above is NOT modified — flag off returns that
# very object, so the default prompt is byte-for-byte unchanged.
_STRICT_NO_PADDING_RULE = """STRICT INTENT MODE — NO RELATED-SIGNAL PADDING (absolute rule):
Every post you were handed already passed an intent filter, but read each one
anyway. For any request about buyers, people looking, or leads:
- A post whose author is SELLING, building, promoting, hiring for, or offering
  the thing is NEVER a buyer, a lead or an opportunity. Never list it as one,
  and never label it "related" or "indirect" to fill the list. Omit it.
- A post that merely MENTIONS the topic (opinion, news, "anyone tried X?")
  without the author stating their OWN need is not a lead. Omit it.
- Do not use "source_list" entries to stretch a thin result set. If fewer
  posts qualify than were requested, list exactly the ones that qualify and
  state the number plainly (for example "8 qualified results") — never pad
  toward the requested count.
- If nothing qualifies, use "no_results" and say that THIS search surfaced no
  qualifying posts."""

_STRICT_POST_COUNT_RULE = """POST-COUNT LIMIT (strict mode): list every qualifying post you were given,
once each, and never more than you were given. Do not cut a qualifying post
to reach a fixed number, and never pad with a low-quality post to reach one.
The "platforms" list and the post cards shown to the user are the same set.
A discovery-only post's "summary" must say its content hasn't been fetched
yet, never a fabricated one."""


def _build_strict_analysis_prompt(base: str) -> str:
    out = re.sub(
        r'PREFER RELATED SIGNALS OVER "no_results".*?(?=\n\n──)',
        lambda _m: _STRICT_NO_PADDING_RULE, base, count=1, flags=re.S,
    )
    out = re.sub(
        r'POST-COUNT LIMIT: never include more than.*?(?=\n\n──)',
        lambda _m: _STRICT_POST_COUNT_RULE, out, count=1, flags=re.S,
    )
    return out


CLAUDE_ANALYSIS_SYSTEM_PROMPT_STRICT = _build_strict_analysis_prompt(CLAUDE_ANALYSIS_SYSTEM_PROMPT)


def _analysis_system_prompt() -> str:
    """The analysis system prompt for this request: the original constant
    (same object) when strict mode is off."""
    return CLAUDE_ANALYSIS_SYSTEM_PROMPT_STRICT if strict_intent_mode() else CLAUDE_ANALYSIS_SYSTEM_PROMPT


def _strict_count_note(n: int) -> str:
    """Appended to the user message in strict mode only ("" otherwise)."""
    if not strict_intent_mode():
        return ""
    return (
        f"\n\nSTRICT MODE: {n} post(s) are supplied above and each already passed "
        f"the intent filter. In your opening message state the number of results "
        f"you actually list (e.g. \"N qualified results\") — do not claim or imply "
        f"more, and do not pad."
    )

# Cheap "map" step used only when a topic has enough matched posts that
# sending them all in one shot would be wasteful/risky context-wise. Each
# chunk gets condensed down to only the points relevant to the user's
# question before the final Haiku call ever sees them.
#
# UNCHANGED.
CLAUDE_MAP_STEP_SYSTEM_PROMPT = """
You are helping analyze one batch of social media posts (Reddit/X) about a
topic, as a pre-processing step before another AI writes the actual answer.
Read the user's question and the posts below (title + text only). Pull out
ONLY the grounded points from THESE posts that are relevant to the user's
question — specific complaints, praise, themes, or facts actually present
in the text. Do not invent anything not present in the posts. Do not answer
the user's question yet — just list the relevant grounded points as short
bullets. If nothing in these posts is relevant to the question, say exactly:
"No relevant points in this batch." Never include, guess, or reference a
post URL — you were not given any.
End every bullet with the number(s) of the post(s) it came from, exactly as
given, e.g. "(Post 14)" or "(Post 3, Post 17)".

After the bullets, write a line containing only "POST RECORDS:" and then one
JSON object per line for each post above that is relevant to the question
(skip the others; if none is relevant, leave this block out):
{"post": <its number>, "title": "<its title, copied exactly as given>", "quote": "<4 to 25 consecutive words copied exactly from THIS post's title or text>", "summary": "<one short sentence about THIS post only>", "role": "<buyer|discussion|competitor_promo|testimonial|irrelevant>", "purchase_intent": "<explicit|research|none>", "product_fit": "<fits|partial|unrelated>"}
role: buyer = the author asks for a tool, service or solution for themselves;
competitor_promo = presenting their own product; testimonial = describing a
tool they already use; discussion = anything else on topic. purchase_intent:
explicit only when the author asks for a solution AND states an explicit
purchase step (an approved or allocated budget, a committed decision to buy,
sign, subscribe or switch, or a procurement process under way; never
hypothetical, conditional, negated or paused); research = asks for
recommendations, even with a budget amount, deadline, trial, demo or pilot,
quote request or willingness to pay. Each record describes only the post whose
number it carries. Never include a URL.
"""

# (BUGFIX PACK #3) Second-level "notes-of-notes" condensing step — only
# ever invoked when a topic already needed enough first-level post
# chunking that the resulting notes themselves would be too many to
# combine directly in one final reduce call. Purely consolidates/
# de-duplicates ALREADY-grounded notes; never introduces anything new.
CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT = """
You are consolidating multiple batches of already-condensed grounded notes
about social media posts, as a pre-processing step before another AI
writes the final answer. Read the user's question and the notes below.
Combine them into a single, shorter set of grounded bullet points relevant
to the user's question — remove duplicates, merge repeated themes, but
never invent anything not already present in the notes. Do not answer the
user's question yet — just output the consolidated grounded bullets. If
none of the notes contain anything relevant to the question, say exactly:
"No relevant points in this batch."
Keep the "(Post N)" tags on every bullet you keep; when you merge bullets,
keep all of their tags. Never add a post number that was not in the notes.
"""


def build_claude_post_context(matched_signals: list) -> list:
    """Strips a matched-signals list (which has title/post_text/post_url/
    platform, used for post cards) down to ONLY title + text — this is the
    single point where post_url and platform are dropped before anything
    is sent to Claude.

    (TEXT-REQUIRED FILTER) Skips a post unless it has real post_text.
    Title alone is no longer enough to reach Claude — this drops
    title-only stub entries (e.g. a Google-fallback stub whose "title"
    is just a subreddit name and whose post_text is always None) before
    they're ever handed to analyze_with_claude() / analyze_with_claude_
    stream(). This can only ever REMOVE posts from what Claude sees; it
    never adds or alters anything else. Everything downstream of this
    function (matching, evidence-budget sizing, merge_matched_and_
    google_results(), the post-card `results` a user actually sees) is
    untouched — only Claude's own analysis input is narrowed here.

    (POST ENRICHMENT) Output is unchanged; the filtering now lives in
    _aligned_post_context() so the answer post-processor can map Claude's
    1-based "[Post N]" index back to the exact signal that produced it."""
    return [post for post, _sig in _aligned_post_context(matched_signals)]


def _aligned_post_context(matched_signals: list) -> list:
    """Same filter/order as build_claude_post_context(), but returns
    (post, source_signal) pairs. Position i (0-based) here is "[Post i+1]"
    in every prompt built from that list — single-call and map-reduce."""
    pairs = []
    for m in matched_signals or []:
        if not isinstance(m, dict):
            continue
        title = (m.get("title") or "").strip()
        text  = (m.get("post_text") or "").strip()
        if not text:
            continue
        post = {"title": title, "text": text}
        posted = timerange.post_published_at(m)        # (DATE/TIME) original publication time, if known
        if posted is not None:
            post["posted_utc"] = posted.strftime("%Y-%m-%d %H:%M UTC")
        pairs.append((post, m))
    return pairs


def chunk_list(items: list, chunk_size: int) -> list:
    """Generic chunker: splits `items` into consecutive batches of at most
    `chunk_size`. Used to keep each Claude call's context small and cheap
    regardless of how many posts a topic matched."""
    if not items:
        return []
    if chunk_size <= 0:
        return [items]
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def _format_posts_block(posts: list, start_index: int = 1) -> str:
    """Numbered posts block for Claude. A post carrying "_n" (set by the
    map-reduce path, see _number_posts()) keeps that GLOBAL number so chunk
    2 starts at e.g. [Post 13], not [Post 1]. An empty title is omitted
    rather than sent as a "(no title)" placeholder Claude would copy."""
    lines = []
    for offset, p in enumerate(posts):
        n = p.get("_n") or (start_index + offset)
        title = (p.get("title") or "").strip()
        text = p.get("text") or "(no text)"
        posted = f"Posted: {p['posted_utc']}\n" if p.get("posted_utc") else ""   # (DATE/TIME)
        if title and not _is_placeholder_title(title):
            lines.append(f"[Post {n}]\nTitle: {title}\n{posted}Text: {text}")
        else:
            lines.append(f"[Post {n}]\n{posted}Text: {text}")
    return "\n\n".join(lines)


def _number_posts(posts: list) -> list:
    """Copies of `posts` tagged with their global 1-based number ("_n"),
    used before map-reduce chunking so every chunk keeps global indices."""
    return [dict(p, _n=i) for i, p in enumerate(posts, 1)]


def _lazy_backfill_missing_embeddings(
    raw_docs: list,
    signals_collection,
    embed_fn,
) -> list:
    """Find docs in `raw_docs` with missing/null/empty embedding vectors,
    embed their `post_text` in batches via `embed_fn` (a function that
    accepts list[str] and returns list[list[float]] or None — same contract
    as generate_query_embeddings_batch()), save the vectors to
    `signals_collection` (primary only — never mirror collections), attach
    the vectors back to the in-memory doc dicts, and return the newly-
    vectorized docs so the caller can append them for immediate scoring.

    MUST NEVER RAISE — any failure is logged and silently skipped.

    Only called when LAZY_EMBED_ENABLED is True (config default "1").
    At most LAZY_EMBED_MAX_DOCS_PER_QUERY docs are backfilled per query call.
    Only docs whose post_text has at least LAZY_EMBED_MIN_TEXT_CHARS chars.

    pymongo bulk_write uses ordered=False; each UpdateOne filter includes the
    missing-embedding guard so concurrent background writers cannot have their
    vectors overwritten.

    IMPORTANT SCOPE LIMITATION
    --------------------------
    This function only operates on the `raw_docs` list that was assembled
    immediately before it is called.  Those docs were fetched from Mongo
    using a query that already filters for ``embedding: {$exists: True,
    $ne: None}``, so in practice `raw_docs` will only contain docs whose
    stored embedding field is ``[]`` (empty list) — the one falsy value
    that slips through that filter.  Documents that have never been
    embedded and are therefore absent from Mongo entirely are *not* seen
    here at all.

    To do a full historical backfill across all three collections, use the
    standalone ``backfill_embeddings.py`` CLI script instead:

        python backfill_embeddings.py --report
        python backfill_embeddings.py --apply --collection all
    """
    if not LAZY_EMBED_ENABLED:
        return []

    try:
        missing_clause = {"$or": [
            {"embedding": {"$exists": False}},
            {"embedding": None},
            {"embedding": []},
        ]}

        # Collect docs that are missing vectors.
        candidates = []
        for doc in raw_docs:
            emb = doc.get("embedding")
            if not emb:  # covers None, [], 0, ""
                candidates.append(doc)

        if not candidates:
            return []

        # Filter by minimum text length and cap total.
        to_embed = [
            d for d in candidates
            if len(d.get("post_text") or "") >= LAZY_EMBED_MIN_TEXT_CHARS
        ][:LAZY_EMBED_MAX_DOCS_PER_QUERY]

        if not to_embed:
            return []

        log.info(f"lazy_backfill: embedding {len(to_embed)} docs with missing vectors")

        newly_vectorized = []

        for batch_start in range(0, len(to_embed), LAZY_EMBED_BATCH_SIZE):
            batch = to_embed[batch_start: batch_start + LAZY_EMBED_BATCH_SIZE]
            texts = [d.get("post_text", "") for d in batch]

            try:
                vectors = embed_fn(texts)
            except Exception as exc:
                log.warning(f"lazy_backfill: embed batch failed: {exc}")
                continue

            if not vectors or len(vectors) != len(batch):
                log.warning("lazy_backfill: embed batch returned unexpected vector count, skipping")
                continue

            # Bulk-write vectors back to primary collection only.
            try:
                from pymongo import UpdateOne
                ops = []
                for doc, vec in zip(batch, vectors):
                    doc_id = doc.get("_id")
                    if doc_id is None or not vec:
                        continue
                    ops.append(UpdateOne(
                        {"_id": doc_id, **missing_clause},
                        {"$set": {"embedding": vec}},
                    ))
                if ops:
                    signals_collection.bulk_write(ops, ordered=False)
            except Exception as exc:
                log.warning(f"lazy_backfill: bulk_write failed: {exc}")
                # Fall through — still attach vectors in-memory for this query.

            # Attach vectors to in-memory docs so they score immediately.
            for doc, vec in zip(batch, vectors):
                if vec:
                    doc["embedding"] = vec
                    newly_vectorized.append(doc)

        if newly_vectorized:
            log.info(f"lazy_backfill: {len(newly_vectorized)} docs vectorized and added to scoring pool")
        return newly_vectorized

    except Exception as exc:  # noqa: BLE001
        log.warning(f"lazy_backfill: unexpected error (skipping): {exc}")
        return []


def _call_claude(system_prompt: str, user_message: str, max_tokens: int = None, enable_web_search: bool = False, force_json_prefill: bool = False) -> str:
    """Single call to the OpenAI Responses API (GPT-5 mini). Raises
    on any failure — callers decide how to degrade gracefully (never let
    this block the search job or the post cards, which don't depend on
    this call at all).

    (CHAT WEB-SEARCH FEATURE) `enable_web_search` (default False — every
    existing caller that doesn't pass it behaves exactly as before this
    feature): when True, adds the OpenAI web_search tool to the
    request payload so the model can look up current/recent information
    instead of relying purely on its own training knowledge.

    (STRUCTURAL JSON-ONLY FIX) `force_json_prefill` (default False — every
    existing caller that doesn't pass it behaves exactly as before this
    fix): when True, sets `text={"format":{"type":"json_object"}}` in the
    request payload, instructing the OpenAI Responses API to emit a valid
    JSON object. This replaces the Anthropic assistant-prefill technique
    ({" leading character) — the semantics are identical from the caller's
    perspective: pass True for system prompts that ALWAYS return a JSON
    object (never for map/notes-reduce prompts, which return plain text)."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    effective_max_tokens = max_tokens or CLAUDE_MAX_TOKENS
    payload: dict = {
        "model": LLM_MODEL,
        "max_output_tokens": effective_max_tokens + LLM_REASONING_HEADROOM,
        "instructions": system_prompt,
        "input": user_message,
        "reasoning": {"effort": LLM_REASONING_EFFORT},
        "store": False,
    }
    if LLM_VERBOSITY:
        payload["verbosity"] = LLM_VERBOSITY
    if force_json_prefill:
        payload["text"] = {"format": {"type": "json_object"}}
        # OpenAI Responses API requires the word "json" to appear in `input`
        # (not just in `instructions`) when text.format.type == "json_object".
        # Guard: if the caller's user_message already contains "json"
        # (case-insensitive) we leave it untouched; otherwise we append a
        # one-line hint so the API doesn't return 400.
        if "json" not in (user_message or "").lower():
            payload["input"] = (user_message or "") + "\n\nReturn your answer as a single valid JSON object only."
    if enable_web_search:
        payload["tools"] = [{"type": "web_search"}]
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "content-type": "application/json",
    }

    with httpx.Client(timeout=CLAUDE_TIMEOUT_SECONDS) as http_client:
        response = http_client.post(OPENAI_RESPONSES_URL, headers=headers, json=payload)
        if response.status_code >= 400:
            log.warning(
                f"LLM API error {response.status_code} | model={LLM_MODEL} | "
                f"max_output_tokens={payload['max_output_tokens']} | "
                f"system_chars={len(system_prompt or '')} | "
                f"user_message_chars={len(user_message or '')} | "
                f"body={response.text[:2000]}"
            )
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "incomplete":
            log.warning(f"LLM response incomplete | model={LLM_MODEL} | reason={data.get('incomplete_details')}")
        if _um:
            try: _um.record_usage("llm", LLM_MODEL, *_um.extract_responses_usage(data), status=("incomplete" if data.get("status") == "incomplete" else "ok"), extra={"web_search_enabled": bool(enable_web_search), "response_id": data.get("id")})
            except Exception: pass

    # Extract text from output items
    result_parts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text = part.get("text", "")
                    if text:
                        result_parts.append(text)
    result = "\n".join(result_parts).strip()
    return result


def _map_chunk(query: str, posts_chunk: list) -> str:
    posts_block = _format_posts_block(posts_chunk)
    user_message = f"User's question: {query}\n\nPosts:\n{posts_block}"
    return _call_claude(CLAUDE_MAP_STEP_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_MAP_MAX_TOKENS)


def _condense_notes_chunk(query: str, notes_chunk: list) -> str:
    """(BUGFIX PACK #3 — 2ND-LEVEL CHUNKING) Consolidates one batch of
    already-condensed first-level notes into a single, shorter set of
    grounded bullets, using CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT. Only ever
    called from analyze_with_claude() when the number of first-level
    notes exceeds CLAUDE_NOTES_PER_CHUNK — see below."""
    notes_block = "\n\n---\n\n".join(notes_chunk)
    user_message = f"User's question: {query}\n\nNotes:\n{notes_block}"
    return _call_claude(CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_MAP_MAX_TOKENS)


def _notes_unavailable_user_message(query: str) -> str:
    """(EMPTY-NOTES GROUNDING FIX) Shared user-message text for the case
    where posts were retrieved but every map/condense step failed or
    returned empty — used by both analyze_with_claude() (non-streaming)
    and analyze_with_claude_stream() (streaming) so the two functions
    cannot drift apart.  Mirrors the wording style of the no-posts branch
    in the same functions: STRICTLY no_results JSON, Do NOT answer from
    general knowledge."""
    return (
        f"User's question: {query}\n\n"
        "Note: posts were retrieved for this topic but could not be "
        "analysed right now (map step returned no grounded notes — "
        "possible API timeout or transient error). Respond STRICTLY in "
        "the no_results JSON format defined above. Do NOT answer from "
        "general knowledge or outside web knowledge. In the message "
        "field: plainly state that evidence was found but could not be "
        "processed at this time. In suggested_actions: only Flintel-"
        "internal next-steps (retry the search, broaden time window / "
        "platform / terms) — kabhi bhi kisi bahar ke tool ka naam mat "
        "lo. Tone: confident analyst giving a status report."
    )


# ── (POST RECORDS) map-step records tied to their stored posts ──────────────
# The map step is the only step that sees each post's title and text, so it
# also writes one record per relevant post (CLAUDE_MAP_STEP_SYSTEM_PROMPT).
# The server keeps a record only after answer_integrity.check_post_record()
# accepts it against that exact post, carries the kept records to
# _patch_post_urls_into_answer() under a per-process signature the model never
# sees, and answer_integrity re-checks each record before a row uses it. A
# "_post_records" field the model writes itself is removed and never used.
_POST_RECORDS_FIELD = "_post_records"
_POST_RECORDS_KEY = secrets.token_bytes(32)
_POST_RECORDS_MARK = re.compile(r"^[ \t]*POST RECORDS:[ \t]*$", re.I | re.M)


def _split_map_reply(reply, chunk: list, fingerprints: dict):
    """(notes, records) from one map reply: the notes without the records
    block, and {post number: record} for records that belong to this chunk and
    pass check_post_record(). A post number claimed twice keeps no record."""
    if not isinstance(reply, str):
        return reply, {}
    mark = _POST_RECORDS_MARK.search(reply)
    if not mark:
        return reply, {}
    in_chunk = {p["_n"]: p for p in chunk if p.get("_n")}
    kept, seen = {}, set()
    for line in reply[mark.end():].splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        n = _parse_post_index(record.get("post")) if isinstance(record, dict) else None
        if n is None:
            continue
        if n in seen:
            kept.pop(n, None)
            continue
        seen.add(n)
        post = in_chunk.get(n)
        if post is None:
            continue
        checked = answer_integrity.check_post_record(
            record, post.get("title"), post.get("text"), [fp for m, fp in fingerprints.items() if m != n])
        if checked:
            kept[n] = checked
    return reply[:mark.start()].rstrip(), kept


def _records_mac(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hmac.new(_POST_RECORDS_KEY, body, hashlib.sha256).hexdigest()


def _with_server_fields(answer_text, records: dict = None):
    """The final analysis JSON with any model-written "_post_records" removed
    and, for a listed-posts answer, the server's signed records added."""
    if not isinstance(answer_text, str):
        return answer_text
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^json\s*", "", cleaned.strip("`").strip(), flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict):
        return answer_text
    had = data.pop(_POST_RECORDS_FIELD, None) is not None
    if records and data.get("format") in ("source_list", "comparison"):
        payload = {str(n): r for n, r in sorted(records.items())}
        data[_POST_RECORDS_FIELD] = {"records": payload, "mac": _records_mac(payload)}
    elif not had:
        return answer_text
    return json.dumps(data, ensure_ascii=False)


def _take_post_records(data) -> Optional[dict]:
    """Removes "_post_records" from `data`, whoever wrote it, and returns
    {post number: record} only when it carries this process's signature."""
    if not isinstance(data, dict) or _POST_RECORDS_FIELD not in data:
        return None
    raw = data.pop(_POST_RECORDS_FIELD)
    try:
        payload, mac = raw["records"], raw["mac"]
        if not (isinstance(payload, dict) and isinstance(mac, str) and hmac.compare_digest(mac, _records_mac(payload))):
            return None
        return {int(n): r for n, r in payload.items() if isinstance(r, dict)}
    except Exception:
        return None


def analyze_with_claude(query: str, matched_signals: list, extra_context: str = None) -> str:
    """Turns (user question + matched signals) into the actual answer the
    user sees, using CLAUDE_ANALYSIS_SYSTEM_PROMPT. Handles three cases:

      1. No usable posts at all -> Claude still answers, told plainly that
         there's no post data yet, per the system prompt's own grounding
         rule. (v6: this is the exact branch the response-timeout fallback
         in _fill_in_message_outputs() relies on — it calls this function
         with an empty list purely to reach this case.)
      2. Few enough posts to fit one call -> single direct call.
      3. Enough posts that chunking is worth it -> map step condenses each
         chunk of CLAUDE_POSTS_PER_CHUNK posts down to grounded notes,
         then (BUGFIX PACK #3) IF there are more of those notes than
         CLAUDE_NOTES_PER_CHUNK, a second condensing pass chunks the notes
         themselves and condenses each batch further first, THEN one
         final reduce call (still using the main system prompt) turns
         all the (possibly twice-condensed) notes + the question into the
         final answer. Small/medium topics skip the second pass entirely
         and behave exactly as before this bugfix pack.

    Returns the final answer text only — this is the only thing callers
    should persist (see save_claude_answer_to_chat).

    UNCHANGED except for the additive second-level chunking pass described
    above (BUGFIX PACK #3), and the additive optional `extra_context`
    parameter: when provided, it's appended to the user_message built in
    every branch below, right before that branch's _call_claude(...) call
    — no other line of this function's logic/branches changes. This
    function only ever sees ALREADY-MATCHED posts — it has no idea whether
    a time window was applied to produce them.

    (SIMULATED-STREAM FIX) This function is ALSO now the one
    GET /chat/{chat_id}/stream calls to get its complete answer text —
    still exactly this same, byte-for-byte unchanged function, no new
    parameters, no new branches. See that route / the module docstring
    for why.

    (CHAT WEB-SEARCH FEATURE) Every _call_claude(...) invocation inside
    this function is UNCHANGED — none of them pass enable_web_search=True.
    This function only ever analyzes ALREADY-MATCHED, grounded posts, and
    has no reason to reach out to the live web."""
    _pd_note = _post_display_note(query)          # (POST DISPLAY PREFERENCE) None = unchanged
    if _pd_note:
        extra_context = (extra_context + "\n\n" + _pd_note) if extra_context else _pd_note
    posts = build_claude_post_context(matched_signals)

    if not posts:
        user_message = (
            f"User's question: {query}\n\n"
            "Note: no posts were matched for this topic yet — flintel_signals "
            "mein abhi is exact topic ka data collect nahi hua. Respond "
            "STRICTLY in the no_results JSON format defined above — never "
            "switch to free-text/general-knowledge prose for this case. "
            "message field mein: (a) plainly batao kya search kiya gaya, "
            "(b) ek honest, non-apologetic reason do (e.g. 'ye topic abhi "
            "tak zyada discuss nahi hua ho sakta hai, ya niche/new brand "
            "hai'), (c) suggested_actions mein sirf Flintel ke apne "
            "next-steps do (broaden time/platform/term) — kabhi bhi kisi "
            "bahar ke tool/platform ka naam mat lo. Tone: confident analyst "
            "jo status report de raha hai, na ke koi form-based rejection."
        )
        if extra_context:
            user_message += "\n\n" + extra_context
        return _with_server_fields(_extract_json_object_from_text(_call_claude(_analysis_system_prompt(), user_message, force_json_prefill=True)))

    # (POST ENRICHMENT) Global post numbers survive chunking: chunk 2's
    # first post is "[Post 13]", not "[Post 1]".
    numbered = _number_posts(posts)
    chunks = chunk_list(numbered, CLAUDE_POSTS_PER_CHUNK)

    if len(chunks) <= 1:
        posts_block = _format_posts_block(posts)
        user_message = f"User's question: {query}\n\nPosts (title + text only):\n{posts_block}" + _strict_count_note(len(posts))
        if extra_context:
            user_message += "\n\n" + extra_context
        return _with_server_fields(_extract_json_object_from_text(_call_claude(_analysis_system_prompt(), user_message, force_json_prefill=True)))

    # Multiple chunks -> map-reduce so no single call has to swallow every
    # matched post at once.
    # (POST RECORDS) each map reply's records are checked against their own
    # posts here; the kept ones travel to the final answer, signed.
    fingerprints = {p["_n"]: answer_integrity.post_fingerprint(p.get("title"), p.get("text")) for p in numbered}
    notes, records = [], {}
    for chunk in chunks:
        try:
            note = _map_chunk(query, chunk)
        except Exception as exc:
            log.warning(f"Claude map-step failed for a chunk (skipping chunk): {exc}")
            continue
        note, found = _split_map_reply(note, chunk, fingerprints)
        records.update(found)                       # chunks are disjoint: one record per post at most
        if note:
            notes.append(note)

    # (BUGFIX PACK #3 — 2ND-LEVEL CHUNKING) If the number of condensed
    # first-level notes itself is large enough that combining them all in
    # one final reduce call could be unwieldy, chunk the notes into
    # batches of CLAUDE_NOTES_PER_CHUNK and condense each batch further
    # with one extra cheap call per batch, BEFORE the final reduce call
    # below. This only activates for topics big enough to need it — for
    # everything else `notes` is left exactly as-is, so behavior is
    # identical to before this bugfix pack.
    if len(notes) > CLAUDE_NOTES_PER_CHUNK:
        note_chunks = chunk_list(notes, CLAUDE_NOTES_PER_CHUNK)
        condensed_notes = []
        for note_chunk in note_chunks:
            try:
                condensed = _condense_notes_chunk(query, note_chunk)
            except Exception as exc:
                log.warning(f"Claude notes-condense step failed for a batch (skipping batch): {exc}")
                continue
            if condensed:
                condensed_notes.append(condensed)
        if condensed_notes:
            notes = condensed_notes

    # (EMPTY-NOTES GROUNDING FIX) If every map/condense step failed or
    # returned empty, do NOT call Claude with "(no grounded points
    # extracted)" — that text would slip past the no-results branch and
    # reach Claude without a "do not answer from general knowledge"
    # instruction.  Route to the same grounded refusal used above.
    if not notes:
        log.warning(
            f"analyze_with_claude: all map/condense steps returned empty "
            f"({len(chunks)} chunk(s) attempted) — routing to no_results branch. "
            f"query={query!r}"
        )
        user_message = _notes_unavailable_user_message(query)
        if extra_context:
            user_message += "\n\n" + extra_context
        return _with_server_fields(_extract_json_object_from_text(_call_claude(_analysis_system_prompt(), user_message, force_json_prefill=True)))

    combined_notes = "\n\n---\n\n".join(notes)
    user_message = (
        f"User's question: {query}\n\n"
        f"Below are grounded notes already condensed from {len(posts)} posts "
        f"(title + text only), split into batches. Treat these notes as your "
        f"only factual grounding about the posts, and answer the user's "
        f"actual question naturally. Each note ends with \"(Post N)\" tags — "
        f"use only those N values for a post's \"index\" and for "
        f"\"supporting_post_indices\".\n\nNotes:\n{combined_notes}"
    )
    if extra_context:
        user_message += "\n\n" + extra_context
    return _with_server_fields(_extract_json_object_from_text(_call_claude(_analysis_system_prompt(), user_message, force_json_prefill=True)),
                               records)


# ─────────────────────────────────────────────────────────────────────────────
# STREAMING ADD-ON — additive alternate path only, see module docstring.
# _call_claude() and analyze_with_claude() above are completely untouched
# and remain what every existing caller uses.
#
# (SIMULATED-STREAM FIX) `_call_claude_stream()` and
# `analyze_with_claude_stream()` below are kept fully intact, unchanged,
# and importable/callable exactly as before — GET /chat/{chat_id}/stream
# simply no longer calls them (it now calls the blocking analyze_with_claude()
# instead, so it can post_url-patch the answer before ever sending any of it
# out). Nothing about these two functions themselves changed.
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude_stream(system_prompt: str, user_message: str, max_tokens: int = None):
    """(STREAMING ADD-ON) Same OpenAI Responses API call as
    _call_claude(), except with "stream": true — instead of blocking
    until the whole response is ready, this yields each text delta AS
    the API streams it back (word-by-word / token-by-token), so a
    caller can forward pieces to the browser live instead of waiting for
    the entire answer.

    Purely ADDITIVE: _call_claude() itself is untouched and is still used
    by every existing caller (routing, map step, notes-reduce step, the
    non-streaming analyze_with_claude()). This generator is only used by
    analyze_with_claude_stream().

    Yields plain text chunks (str). Raises on any failure — same
    degrade-gracefully convention as _call_claude()."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    effective_max_tokens = max_tokens or CLAUDE_MAX_TOKENS
    payload = {
        "model": LLM_MODEL,
        "max_output_tokens": effective_max_tokens + LLM_REASONING_HEADROOM,
        "instructions": system_prompt,
        "input": user_message,
        "reasoning": {"effort": LLM_REASONING_EFFORT},
        "store": False,
        "stream": True,
    }
    if LLM_VERBOSITY:
        payload["verbosity"] = LLM_VERBOSITY
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "content-type": "application/json",
    }

    with httpx.Client(timeout=CLAUDE_TIMEOUT_SECONDS) as http_client:
        with http_client.stream("POST", OPENAI_RESPONSES_URL, headers=headers, json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    event = json.loads(data_str)
                except (ValueError, TypeError):
                    continue
                event_type = event.get("type", "")
                if event_type == "response.output_text.delta":
                    text = event.get("delta", "")
                    if text:
                        yield text
                elif event_type == "response.completed":
                    if _um:
                        try:
                            _r_usage = ((event.get("response") or {}).get("usage") or {})
                            _um.record_usage("llm_stream", LLM_MODEL, _r_usage.get("input_tokens"), _r_usage.get("output_tokens"), status="ok",
                                             extra={"response_id": (event.get("response") or {}).get("id")})
                        except Exception: pass
                elif event_type in ("response.failed", "response.incomplete"):
                    if _um:
                        try:
                            _r_usage = ((event.get("response") or {}).get("usage") or {})
                            _um.record_usage("llm_stream", LLM_MODEL, _r_usage.get("input_tokens"), _r_usage.get("output_tokens"), status="incomplete",
                                             extra={"response_id": (event.get("response") or {}).get("id")})
                        except Exception: pass
                    log.warning(f"LLM stream ended early | type={event_type} | model={LLM_MODEL}")
                    return


def analyze_with_claude_stream(query: str, matched_signals: list, extra_context: str = None):
    """(STREAMING ADD-ON) Mirrors analyze_with_claude()'s exact branches
    (no posts / single call / map-reduce, including the BUGFIX PACK #3
    second-level note chunking) byte-for-byte — the ONLY difference is
    that the final, user-facing call streams its text via
    _call_claude_stream() instead of blocking via _call_claude(). Any
    earlier map/notes-condense calls (never shown to the user directly)
    are UNCHANGED — still plain, blocking _call_claude() calls, exactly
    as in analyze_with_claude().

    This is a generator: yields text chunks (str) as they stream in. The
    caller is responsible for collecting them into the final full answer
    — this function itself does not persist anything, exactly like
    analyze_with_claude().

    (SIMULATED-STREAM FIX) Left fully intact and unchanged. No longer
    called by GET /chat/{chat_id}/stream (see that route + the module
    docstring), but still here, still correct, still usable by any future
    caller that genuinely wants raw live token-by-token output rather
    than the patched-then-paced text the stream route now sends.

    (EMPTY-NOTES GROUNDING FIX) `extra_context` added — mirrors
    analyze_with_claude()'s own parameter so the new empty-notes branch
    can append it exactly as the other branches do."""
    _pd_note = _post_display_note(query)          # (POST DISPLAY PREFERENCE) None = unchanged
    if _pd_note:
        extra_context = (extra_context + "\n\n" + _pd_note) if extra_context else _pd_note
    posts = build_claude_post_context(matched_signals)

    if not posts:
        # SOURCE-GROUNDING: no retrieved evidence — must NOT fall back to
        # general knowledge.  Mirror the non-streaming branch exactly: send
        # the no_results JSON instruction so Claude returns a structured
        # "insufficient evidence" response rather than answering from its
        # own knowledge.  force_json_prefill is not available on the
        # streaming path, but the system prompt's no_results format
        # instruction is strong enough on its own — the instruction below
        # explicitly forbids free-text / general-knowledge prose.
        user_message = (
            f"User's question: {query}\n\n"
            "Note: no posts were matched for this topic yet — flintel_signals "
            "mein abhi is exact topic ka data collect nahi hua. Respond "
            "STRICTLY in the no_results JSON format defined above — never "
            "switch to free-text/general-knowledge prose for this case. "
            "message field mein: (a) plainly batao kya search kiya gaya, "
            "(b) ek honest, non-apologetic reason do (e.g. 'ye topic abhi "
            "tak zyada discuss nahi hua ho sakta hai, ya niche/new brand "
            "hai'), (c) suggested_actions mein sirf Flintel ke apne "
            "next-steps do (broaden time/platform/term) — kabhi bhi kisi "
            "bahar ke tool/platform ka naam mat lo. Tone: confident analyst "
            "jo status report de raha hai, na ke koi form-based rejection."
        )
        if extra_context:
            user_message += "\n\n" + extra_context
        yield from _call_claude_stream(_analysis_system_prompt(), user_message)
        return

    # (POST ENRICHMENT) Global post numbers survive chunking: chunk 2's
    # first post is "[Post 13]", not "[Post 1]".
    chunks = chunk_list(_number_posts(posts), CLAUDE_POSTS_PER_CHUNK)

    if len(chunks) <= 1:
        posts_block = _format_posts_block(posts)
        user_message = f"User's question: {query}\n\nPosts (title + text only):\n{posts_block}" + _strict_count_note(len(posts))
        if extra_context:
            user_message += "\n\n" + extra_context
        yield from _call_claude_stream(_analysis_system_prompt(), user_message)
        return

    # Map step + optional 2nd-level note chunking: identical logic to
    # analyze_with_claude(), still blocking (never shown to the user
    # directly) — only the final call below streams.
    notes = []
    for chunk in chunks:
        try:
            note = _map_chunk(query, chunk)
        except Exception as exc:
            log.warning(f"Claude map-step failed for a chunk (skipping chunk): {exc}")
            continue
        if note:
            notes.append(note)

    if len(notes) > CLAUDE_NOTES_PER_CHUNK:
        note_chunks = chunk_list(notes, CLAUDE_NOTES_PER_CHUNK)
        condensed_notes = []
        for note_chunk in note_chunks:
            try:
                condensed = _condense_notes_chunk(query, note_chunk)
            except Exception as exc:
                log.warning(f"Claude notes-condense step failed for a batch (skipping batch): {exc}")
                continue
            if condensed:
                condensed_notes.append(condensed)
        if condensed_notes:
            notes = condensed_notes

    # (EMPTY-NOTES GROUNDING FIX) Mirror the non-streaming branch: if
    # every map/condense step failed or returned empty, route to the
    # grounded refusal instead of passing the placeholder text.
    if not notes:
        log.warning(
            f"analyze_with_claude_stream: all map/condense steps returned "
            f"empty ({len(chunks)} chunk(s) attempted) — routing to "
            f"no_results branch. query={query!r}"
        )
        user_message = _notes_unavailable_user_message(query)
        if extra_context:
            user_message += "\n\n" + extra_context
        yield from _call_claude_stream(_analysis_system_prompt(), user_message)
        return

    combined_notes = "\n\n---\n\n".join(notes)
    user_message = (
        f"User's question: {query}\n\n"
        f"Below are grounded notes already condensed from {len(posts)} posts "
        f"(title + text only), split into batches. Treat these notes as your "
        f"only factual grounding about the posts, and answer the user's "
        f"actual question naturally. Each note ends with \"(Post N)\" tags — "
        f"use only those N values for a post's \"index\" and for "
        f"\"supporting_post_indices\".\n\nNotes:\n{combined_notes}"
    )
    if extra_context:
        user_message += "\n\n" + extra_context
    yield from _call_claude_stream(_analysis_system_prompt(), user_message)


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ROUTING LAYER (v5, extended in v6 with abuse/harm blocking, again
# with keyword generation, again with time-window parsing, pain-point-aware
# keyword generation, and a 4th "clarify" intent, again with the
# ROUTER INTENT REFINEMENT described in the module docstring above, and
# now again with the CHAT WEB-SEARCH FEATURE described there too)
#
# Runs BEFORE anything else in POST /search. A single cheap Claude call
# decides whether the user's message is a genuine "search" (wants social-
# listening data pulled about a brand/product/topic, INCLUDING a general
# pain-point/complaint angle on a platform — see ROUTER INTENT REFINEMENT),
# a plain "chat" message (INCLUDING a truly open-ended "what's happening on
# this platform" question with no subject at all — see ROUTER INTENT
# REFINEMENT), "blocked" (abusive/harmful content), or "clarify" (a
# search-shaped message with truly nothing — not even a general angle — to
# search for).
#
# (KEYWORD-GENERATION SWAP) The SAME call now ALSO returns a "keywords"
# field when intent="search" — still just ONE Claude call total, no new
# round trip.
#
# (TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE) The SAME call now ALSO
# returns a "time_window_days" field when intent="search" (int, or null
# if the user gave no time range), generates pain-point/prospect-style
# keywords when that's what the message is asking for, and can return
# intent="clarify" instead of guessing at an unclear topic.
#
# (CHAT WEB-SEARCH FEATURE) The SAME call now ALSO has the Anthropic
# web_search tool available to it (see _call_claude()'s own
# `enable_web_search` parameter and classify_and_maybe_chat() below) —
# this only ever matters for the "chat" branch, since that's the only
# branch where Claude writes a user-facing reply directly; "search" still
# only ever returns keywords/time_window_days, never a reply, so having
# the tool available changes nothing about that branch's own output
# contract.
#
# Safety rule: ANY failure here (bad JSON, API error, timeout, missing
# key) defaults to {"intent": "search", "reply": None, "keywords": None,
# "time_window_days": None} so the pre-existing pipeline is always the
# fallback — this routing layer can only ever add a shortcut, it can
# never silently swallow a real search request or leave one with zero
# keywords.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_ROUTER_SYSTEM_PROMPT = """
You are the routing brain inside Flintel, a social-listening platform.
Every message a user types goes through you FIRST, before anything else
happens in the product.
Your job: classify this message into exactly one of FOUR types, and for
"search" messages, ALSO generate the search representation Flintel's
retrieval layer will use afterward, plus an optional time window.

HOW FLINTEL ACTUALLY SEARCHES (this determines what you must produce —
read it before generating anything):
Flintel does NOT do literal keyword matching on post text. It runs
EMBEDDING-BASED SEMANTIC retrieval: your "match_phrases" are each turned
into a vector and compared, by meaning, against the vector of every
candidate post. A post matches when it MEANS the same thing as one of
your phrases — the words do not have to overlap at all. Your "keywords"
are used separately, for lexical candidate recall and for an external
Google/Reddit discovery search.

This has one overriding consequence for you:

  THE USER'S LITERAL WORDS ARE NOT THE SEARCH. THE USER'S INTENT IS.

A person Flintel should find almost never phrases things the way the
person searching does. Someone worth surfacing for "find businesses that
want WhatsApp AI agents" will realistically have written "we get way too
many customer enquiries on WhatsApp and can't keep up", or "anyone know
how to connect ChatGPT to WhatsApp?", or "looking to hire someone to
build a chatbot for our store" — and not one of those contains the
phrase the user typed. Your job is to write the phrases THOSE people
would actually write.

1. "search" — the message is asking Flintel to research/monitor/pull
   social-media data about a brand, product, company, person, industry,
   or topic, AND that topic/subject is actually clear from the message
   itself (or from the conversation history below). For "search"
   messages, ALSO return:

   - "target_entity": a short plain-language description of WHO Flintel
     is being asked to find — not what the topic is. This is the single
     most important field for getting the right people back, because the
     same topic word can mean opposite searches:
       "find people who SELL AI agents"        -> providers/agencies/
                                                  builders OFFERING it
       "find businesses LOOKING FOR AI agents" -> buyers with demand or
                                                  an unsolved problem
     Both are "about AI agents"; they are completely different people.
     Write this as e.g. "agencies and freelancers who build and sell AI
     agent solutions" or "business owners who need customer-support
     automation but haven't bought it yet". Null for non-search intents.

   - "intent_types": an array naming which kinds of signal genuinely
     answer this request, chosen from EXACTLY these five values:
       "active_opportunity"  — explicitly looking to hire, buy, outsource,
                               or commission a solution.
       "solution_evaluation" — actively comparing, evaluating, researching,
                               or asking for recommendations.
       "problem_signal"      — clearly has the problem the solution solves,
                               but is not explicitly shopping yet.
       "provider_seller"     — offering, selling, building, or promoting
                               the solution.
       "general_discussion"  — on-topic but with no commercial intent.
     Pick the ones that actually answer the question, usually 1-3. A
     "find me people who sell X" request is ["provider_seller"]. A "find
     businesses that need X" request is typically
     ["active_opportunity", "solution_evaluation", "problem_signal"].
     Never include all five just to be safe. Null for non-search intents.

   - "keywords": an array of literal search terms.
     These are used for LEXICAL candidate recall and for an external
     Google/Reddit discovery search — so unlike match_phrases these SHOULD
     be short, concrete, literal terms that plausibly appear as real words
     in real posts.
     - Cover the topic itself AND its common real-world vocabulary:
       genuine synonyms, the abbreviation and the expansion, adjacent
       product names for the same job, and the words people use for the
       underlying problem rather than the solution. For "WhatsApp AI
       agents" that legitimately includes things like "whatsapp chatbot",
       "whatsapp automation", "customer support automation" — these are
       not padding, they are how the same need is actually written.
     - Do NOT invent unrelated angles the user never implied (e.g. adding
       "pricing" or "review" variants to a request that had nothing to do
       with pricing or reviews). Breadth of VOCABULARY for the user's
       actual intent: yes. Breadth of TOPIC beyond their intent: no.
     - Up to 10 keywords maximum, never more.
     - PAIN-POINT / PROSPECT PATTERN: if the user describes selling or
       promoting something and wants to find people who might need it
       (e.g. "I run an AI agent company, find people whose website is
       slow / carts are getting abandoned", "I run a travel agency, find
       people saying their travel experience was bad", or any similar
       "I offer X, find people with problem Y" message, in ANY industry —
       these are just examples, not a fixed list), generate keywords
       around the PROBLEM/SYMPTOM the user described (e.g. "website
       slow", "site is slow", "abandoned cart", "bad travel experience",
       "trip was ruined", "poor customer service"), NOT around the
       seller's own product or company name. Read whatever problem the
       user actually describes and reflect that back — never assume a
       fixed industry or fixed symptom list.
     - GENERAL PAIN-POINT / COMPLAINT PATTERN (no brand, product, or
       industry named at all): a message like "reddit par log kya
       problems face kar rahe hain" or "what are people complaining
       about on twitter" names NO specific brand/product/industry, but it
       DOES name a real, searchable subject — general problems/
       complaints/frustrations on that platform. Treat this as "search"
       (NOT "clarify" — see the "clarify" rules below), and generate
       keywords around common frustration/complaint phrasing that would
       plausibly appear in real posts, e.g. "so frustrated with", "sick
       of dealing with", "worst experience with", "wish there was a
       better", "fed up with", "nightmare trying to", "why is it so hard
       to", "anyone else hate". This is DIFFERENT from the fully
       open-ended "what's happening on this platform" case described
       under "chat" below — the distinguishing line is whether the user
       named ANY angle (a brand, a product, an industry, or a general
       problem/complaint framing) versus naming nothing at all beyond the
       platform itself.
     - GENERAL DEMAND / MARKET-RESEARCH PATTERN (no platform named either):
       a message that asks what people are frustrated with, annoyed by, or
       wish existed — in any domain, industry, or "technology" broadly — even
       WITHOUT naming a specific platform, is still a valid "search" subject:
       the complaint/demand angle itself (e.g. "annoyed with", "frustrated
       by", "wish there was", "biggest problem with") is the searchable topic.
       This is different from a truly platform-only, topic-less question like
       "reddit par kya chal raha hai" (which stays "chat" per the rule below)
       — the distinguishing signal is whether the message names ANY complaint/
       demand/pain-point angle, not whether it names a platform. Generate
       keywords around the complaint/frustration phrasing the user implied
       (e.g. "so annoyed with", "wish there was a tool for", "sick of dealing
       with", "biggest pain point in"), the same way the PAIN-POINT / PROSPECT
       PATTERN above does.
     - Keep each keyword short and natural — a phrase a real person would
       actually type, not a formal category label.
     - Never include meta wording that describes the user's REQUEST to
       you rather than the topic itself — words like "reddit", "twitter",
       "x", "linkedin", "facebook", "posts", "posts about", "show me",
       "today", "find", "search" describe what/where the user wants
       searched, not something that would appear inside an actual post,
       so leave them out of the keyword list (the platform itself is
       handled separately, by a dropdown the user already picked — you
       are only responsible for the topic keywords).

   - "website_only": a boolean, true ONLY when the user's message is
     JUST a website URL (or a URL plus only trivial filler like "check
     this out", "yeh dekho") with NO other stated ask/angle at all. In
     this case, still return "keywords": null (the downstream website-
     reading pipeline fills in real keywords from the site's own
     content, exactly like the existing website-URL flow) — "website_only"
     is what tells the backend to answer the user with a WEBSITE
     BUSINESS BREAKDOWN, not a Reddit/X-evidence report, even though
     keyword generation / background search still runs normally.
     Default false for everything else, including when a URL is shared
     ALONGSIDE a real ask (e.g. "find me leads from this site") — in
     that case treat it as a normal "search" with website_only: false.

   - "match_phrases": THE MOST IMPORTANT FIELD. An array of 8 to 12
     complete, natural sentences — each one written as if it were an
     actual excerpt from a real post by one of the people described in
     "target_entity".

     These are what get embedded and semantically compared against real
     posts, so write them the way the TARGET writes, not the way the
     searcher asked. Rules:

     - Write FULL first-person sentences (roughly 6-16 words), not
       keyword fragments. "we're drowning in customer messages on
       WhatsApp and need to automate replies" retrieves well;
       "whatsapp automation" does not — a short fragment embeds poorly
       against a real multi-sentence post and is the single most common
       cause of relevant posts being missed.
     - COVER EVERY intent_type you listed, with at least 2 distinct
       phrases each. If you listed "provider_seller", some phrases must
       be written in a SELLER's voice ("we build custom AI agents for
       ecommerce brands", "my agency automates customer support with
       AI", "just launched my AI agent platform for small businesses").
       If you listed "problem_signal", some must be written in a
       SUFFERER's voice, describing the problem with NO mention of the
       solution at all ("spending hours every day replying to the same
       customer questions").
     - VARY THE VOCABULARY DELIBERATELY. Several phrases must express the
       same intent while avoiding the user's own words entirely, because
       the strongest matches are usually posts that never use the
       searcher's terminology. For a HubSpot-alternative request that
       means phrases about outgrowing the current CRM, its cost, or
       migrating away — not repetitions of "HubSpot alternative".
     - Include the problem/symptom level, not just the solution level.
       People with budget very often describe a frustration, never the
       product category.
     - Respect stated constraints (seniority, budget, industry, region,
       recency) by weaving them into some phrases naturally — never as a
       bolted-on label.
     - Never meta wording ("reddit", "posts", "show me", "find me").
     - Reason freshly from THIS request every time. Never reuse phrasing
       from these instructions' own examples: they illustrate the SHAPE
       required, they are not a template or a fixed vocabulary, and they
       are not limited to any industry.

   - "time_window_days": an integer, or null.
     - If the user's message itself implies a time range, convert it to
       an approximate number of days: "today"/"aaj" -> 1, "this week" /
       "last 7 days" -> 7, "last 2 weeks" -> 14, "last month" / "past 30
       days" -> 30, "last 3 months" -> 90, "last 6 months" -> 180, "last
       year" -> 365, and so on for any other phrasing that clearly names
       a time span.
     - If the user gives NO time range at all, this MUST be null — do
       NOT invent or assume one.
     - A missing/null time_window_days is NOT by itself a reason to ask
       for clarification (see "clarify" below) — plenty of valid search
       requests never mention a time range at all.

   - "evidence_required": an integer.
     - Estimate approximately how much evidence (matched posts) is needed
       to properly answer this question — NOT a fixed formula. Consider:
       whether a time period was mentioned and how long it is, whether the
       user asks for a trend/change over time, whether the user asks for a
       comparison between two or more entities, whether it's simple
       sentiment, whether the user asks for reasons/drivers/recurring
       themes, and overall complexity of the question.
     - A simple, narrow question with no time range typically needs
       roughly 20-30. A trend/change question over a longer window
       typically needs meaningfully more. A comparison between entities,
       especially over a longer window, typically needs the most. A
       "why are people complaining" style question needs enough to
       identify recurring patterns, not just a handful of posts.
     - These are illustrative guidelines, not fixed rules — reason about
       the actual query every time.
     - Never below 15, never above 100 — the backend will clamp this
       regardless, so pick the number that's genuinely right for the
       question, not a number chosen to avoid clamping.

   Do NOT try to answer the user's question yourself for "search" —
   only classify and produce the keyword list / time window.

2. "chat" — a normal conversational message that doesn't need any new
   data pulled at all: greetings ("hi", "hello", "what's up", "kia chal
   raha hai aj kal"), small talk, thanks, a follow-up question about
   something already discussed in this conversation (answered ONLY from
   the posts, results or website evidence given to you below), or a
   question about Flintel itself. Reply briefly and naturally.
   "keywords" and "time_window_days" must be null for this type.

   GROUNDING (absolute — Flintel is an evidence product, not a general
   chatbot): a "chat" reply must never contain business or market advice,
   example customer pain points, trends, opportunities, competitor
   weaknesses, statistics, post counts, dates, quotes or links that are not
   in the evidence given to you below, and never general-knowledge answers
   presented as Flintel's findings. For a general-knowledge question
   unrelated to anything Flintel monitors, say plainly that Flintel answers
   from the real conversations it collects, and offer to search what
   people are saying about that topic. For a business or market question
   with no evidence given below, do not advise — say nothing has been
   searched yet and offer to run the search.

   QUESTIONS ABOUT FLINTEL ITSELF are always "chat", never "search": what
   Flintel/you can do, how it works, how to use it, what it can find,
   which platforms it covers, how it can help — as long as the message
   names no topic, brand, product, market or audience to look into ("What
   can you do for me?", "How does Flintel work?", "What can you find?",
   "How do I use this?"). Answer from what Flintel does; never start a
   search for them. The moment a research target is named ("What can you
   find about my competitor Acme?", "How can you help me find buyers for
   my CRM?") it is a normal research request and the rules above/below
   apply.

   ALSO classify as "chat" (not "search", not "clarify") when the message
   asks a broad, platform-wide "what's trending / what's happening / what
   are the top problems right now" question with NO specific brand,
   product, narrower topic, or industry named, AND no general problem/
   complaint framing either — e.g. "reddit par kya chal raha hai", "abhi
   reddit par top problem kya hai", "what's trending on twitter today"
   with truly nothing else to go on. For these, do NOT generate search
   keywords and do NOT trigger a database search — and do NOT describe
   what is trending from your own knowledge (that would be an unverified
   claim). Instead, in one or two sentences, say Flintel can only report
   what people are saying once it searches a topic, and ask which brand,
   product, industry or audience to look at — or invite them to share
   their business or website link. "keywords" and "time_window_days" stay
   null for this case, exactly like any other "chat" message.

   This "chat" case is DIFFERENT from the GENERAL PAIN-POINT / COMPLAINT
   PATTERN described under "search" above: if the user names ANY angle at
   all — a brand, product, industry, or even just a general problem/
   complaint framing (e.g. "reddit par log kya problems face kar rahe
   hain") — that IS "search", not this "chat" case. The distinguishing
   factor is simple: is there an actual searchable angle, or is the
   question just "what's happening in general" with nothing else?

   Agar user ke message mein kisi post/result ka NUMBER ya POSITION ka
   reference ho (e.g. "3 number wala", "pehla wala", "post #2", "us
   doosre wale ke baare mein") — ye ek REFERENCE-TO-PREVIOUS-RESULT
   follow-up hai, normal chat nahi. Is case mein bhi intent "chat" hi
   rahega, lekin reply likhte waqt neeche diye gaye "Referenced post
   data" block (agar provided ho) ko hi apni asal source maano — apne
   general knowledge se mat banao. Agar "Referenced post data" ek hi
   specific post deta hai, seedha usi post ke title/summary/sentiment/
   link ke baare mein confidently jawab do. Agar wo poori numbered list
   deta hai (kyunke exact number resolve nahi ho saka), pehle list mein
   se sahi number khud match karo phir usi ke baare mein jawab do — agar
   diya gaya number genuinely list mein exist nahi karta (out of range),
   honestly bata do ke itne posts nahi the, generic confusion mat dikhao.

3. "blocked" — the message is abusive, harassing, hateful, sexually
   explicit, threatening, or otherwise harmful (directed at you, at a
   person, or at any group). Do not search for it and do not answer it
   normally. Instead write a short, calm, firm decline as the reply —
   don't lecture, don't repeat or quote the harmful content back, don't
   moralize at length, just briefly decline and invite them to ask
   something else. "keywords" and "time_window_days" must be null for
   this type.

4. "clarify" — the message is clearly ASKING for a search/social-listening
   pull (it has search-shaped phrasing: "reddit posts", "show me", "find
   me", a time range, etc.) but does NOT actually name any clear
   topic/brand/product/industry/subject to search for, and does NOT even
   name a general problem/complaint angle (see the GENERAL PAIN-POINT /
   COMPLAINT PATTERN under "search" above, which is a "search", not a
   "clarify") — neither in the message itself nor anywhere in the short
   conversation history below. Examples: "aaj ke reddit posts dikhao"
   alone, "last 6 months ke posts do" alone, "show me today's posts" with
   nothing else — there is no brand, product, topic, industry, or problem
   mentioned anywhere for Flintel to actually search. In this case, do
   NOT guess a topic and do NOT fall back to some generic/meaningless
   keyword. Instead, write a short, natural, single clarifying question
   asking what topic/brand/industry/problem they want to look into.
   "keywords" and "time_window_days" must be null for this type (there is
   no job to run yet). IMPORTANT: only use "clarify" when the TOPIC itself
   is missing — a message that clearly names a topic/brand/industry (or
   even just a general problem angle) but simply doesn't mention a time
   range is STILL "search" with time_window_days: null, never "clarify".

   ADDITIONALLY: if the user's message contains a website URL (a link
   starting with http:// or https://) together with ANY stated request,
   ask, or angle at all — including a generic one like "find me
   customers", "find me leads", "promote my site", "market my website",
   or similar — this is NEVER "clarify", even though no specific
   topic/brand/industry is named. A URL is itself enough context to
   search from (Flintel can read the site directly), so treat this as
   "search" with "website_only": false. In this case ALSO generate
   "keywords" and "match_phrases" from the ASK/ANGLE TEXT ALONE, exactly
   as you would for the same request without any URL (ignore the URL
   itself completely; never put the URL, the domain name, or words like
   "website"/"site"/"link" into keywords). For a generic ask such as
   "find me sales", "get me leads", "find customers", apply the
   PAIN-POINT / PROSPECT PATTERN and buyer-intent phrasing normally.
   The downstream website-reading step will separately generate
   website-derived keywords and the backend merges both lists, so do not
   try to guess the business's products yourself. Return "keywords": null
   and "match_phrases": null ONLY if the message is a bare URL with no
   ask/angle at all (that case stays website_only: true, unchanged).
   Only use "clarify" when there is NEITHER a URL NOR any named topic/
   brand/industry/problem angle anywhere in the message.

   When a URL is present together with an ask, ALSO return "url_ask_type":
   - "generic_own_business": the user wants something for their OWN business
     or website in general (sales, leads, customers, growth, promotion,
     "find people for this", in ANY language or phrasing), with NO separate
     named topic, problem, or angle.
   - "specific_topic": the user names a distinct topic, problem, product
     angle, or question alongside the URL (e.g. complaints about something,
     competitors, a particular feature or issue).
   Decide from the meaning of the message, never from specific words.
   For a bare URL with no ask, use "none". Without a URL, use null.

   When writing the clarifying reply, sound like a helpful consultant, not
   a form validator: briefly explain WHY you're asking (so the search
   actually finds something relevant to them), and in one natural
   sentence give them two easy ways forward — name a topic/brand/
   industry, OR just paste their website link, since Flintel can read a
   shared website link automatically and pull the right keywords from it.
   Keep it to 1-2 natural sentences, never robotic or repetitive-sounding.

BUSINESS GUIDANCE QUESTIONS (strategy and next steps for the user's OWN
customers or market) — e.g. "what's the best way to get customers?", "which
customer acquisition strategy should I use?", "what opportunities are
emerging in my market?", "what are customers struggling with right now?",
"which customer segment should I target?", "what should I do next to find
potential customers?", in ANY language. Flintel answers these ONLY from the
real conversations it monitors, never from general knowledge, so such a
message is NEVER "chat":
- If the user's business, product, market, or industry is known — from this
  message, the conversation history, or a saved website context — return
  "search" with "guidance": true. Build "keywords" and "match_phrases" to
  retrieve evidence for the question in THAT market: people looking to buy,
  asking for recommendations, comparing or switching options, describing
  problems or unmet needs (buyer-intent and pain-point phrasing — never
  phrasing about marketing tactics). "resolved_query" restates the question
  with the market named. With a saved website context and no other market
  named, follow the own-business rule there (use_website_context) and also
  set "guidance": true.
- If no business, product, market, or industry is known anywhere, return
  "clarify" with "guidance": true: ask in one or two sentences what they
  sell and to whom (or for their website link), and say plainly that
  Flintel answers this from real conversations it monitors rather than
  generic advice. Give no tips.
General factual questions that are not about the user's own customers or
market (e.g. "what is a CRM?") are not covered by this rule.

FOLLOW-UP MESSAGES — ONE CHAT, MANY TURNS (read the conversation history
below before classifying ANY message):
Users refine a search over many messages. For every message, first decide
from the history whether it REFINES the current search or STARTS A NEW one.
- REFINES the most recent search: the message changes, adds, or removes a
  criterion WITHOUT naming a different subject — e.g. a location ("now in
  USA", "only in Pakistan"), a result count ("at least 20 posts", "10
  more", "show more"), a time range, or a filter (seniority, budget,
  remote, "only buyers", "exclude agencies"), or "same but ...". Such a
  message is "search" (never "clarify" or "chat" when the history has a
  search to refine). Keep the current subject, target, and every
  constraint still in force, then apply the change:
    * A new location REPLACES the previous location(s), unless the user
      combines them ("together", "both", "all three", "also", "as well",
      "and", "aur", "bhi") — then use exactly the union of the locations
      named. "Only in X" means exactly X.
    * A stated count stays in force for later refinements of the same
      search until the user changes it or starts a new search. "More" / "N
      more" asks for additional results on the same search.
    * Adding a filter keeps the existing ones; removing one ("without X",
      "drop the X filter") removes only that one.
    * Never keep a constraint the user has since replaced or removed.
- STARTS A NEW SEARCH: the message names a different subject or target
  (e.g. "now find users looking to buy a car" after a jobs search). Start
  fresh: do NOT carry over earlier locations, counts, filters, or time
  ranges unless the user restates them or explicitly refers back to them
  ("same countries", "in those places").
- Earlier ANSWERS and the posts already shown are context, not
  requirements — the current request is what the user has asked for.
- Do not ask a question the history already answers.
- A "[meaning: ...]" tag in the history is an EARLIER interpretation of
  that turn, not a confirmed fact — it may be wrong. Use it as context,
  but the user's own words (the literal text on each line, and above all
  their NEW message) take priority whenever they conflict with it.
For "search", also return "resolved_query": ONE standalone sentence that
restates the FULL current request after applying the rules above — who to
find, the subject, and every location, count, time range, and filter
still in force — understandable with no history at all, in the user's
language. If the message is already standalone, restate it as-is. All
other search fields ("keywords", "match_phrases", "target_entity",
"time_window_days", "evidence_required") must describe this resolved
request, not just the literal new message — and when it asks for at least
N posts, "evidence_required" must be at least N.

A short, auto-summarized conversation history (may be empty) is given
below for continuity when classifying and when writing a "chat",
"blocked", or "clarify" reply, or when a "search" follow-up implicitly
refers back to a topic already discussed (e.g. if the topic was named
earlier in the conversation, a later "today ke posts do" can still be
"search", using that earlier topic — only ask "clarify" when the topic
truly cannot be determined from the message OR this history). Keep any
reply conversational and plain — don't mention you're an AI or that this
is a "mock", and don't narrate your own reasoning.
Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in EXACTLY one of these four shapes:
{"intent": "search", "reply": null, "keywords": ["<keyword1>", "<keyword2>"], "time_window_days": null, "match_phrases": ["<full sentence 1>", "<full sentence 2>"], "evidence_required": <int|null>, "website_only": false, "url_ask_type": "generic_own_business"|"specific_topic"|"none"|null, "target_entity": "<who to find>"|null, "intent_types": ["<one or more of: active_opportunity, solution_evaluation, problem_signal, provider_seller, general_discussion>"]|null, "resolved_query": "<the full current request as one standalone sentence>"}
{"intent": "chat", "reply": "<your natural reply text here>", "keywords": null, "time_window_days": null, "match_phrases": null, "evidence_required": null}
{"intent": "blocked", "reply": "<short, polite decline text>", "keywords": null, "time_window_days": null, "match_phrases": null, "evidence_required": null}
{"intent": "clarify", "reply": "<short, natural clarifying question>", "keywords": null, "time_window_days": null, "match_phrases": null, "evidence_required": null}
"""

CLAUDE_ROUTER_WEBSITE_CONTEXT_ADDENDUM = """
SAVED WEBSITE CONTEXT (applies ONLY when a "Saved website context" block
appears in the input. If absent, ignore this section; use_website_context
is false.)
The user shared THEIR OWN website earlier in this chat; its URL, business
and already-extracted keywords are given. Decide which case applies:
1. OWN-BUSINESS REQUEST: user wants something for their own business/
   niche/website WITHOUT naming a different topic ("mere niche se related
   posts do", "mere liye leads dhoond kar do", "sales do", "find customers
   for me"). Return intent="search", "use_website_context": true,
   "keywords": null, "match_phrases": null (the backend will itself generate
   FRESH keywords and phrases from the saved website evidence combined with
   this new message — never reuse old ones and never generate them here).
   While a saved website context exists
   such a message is NEVER "clarify" and NEVER "chat".
2. SPECIFIC TOPIC CONNECTED TO THE WEBSITE: user names a specific
   product/angle that belongs to what the website offers. intent="search",
   "use_website_context": false, generate keywords/match_phrases for THAT
   topic normally, "website_topic_relation": "related".
3. TOPIC UNRELATED TO THE WEBSITE: normal search with keywords for that
   topic, "use_website_context": false, "website_topic_relation":
   "unrelated". Never mix the website's keywords in.
4. Everything else (greetings, blocked etc.): use_website_context false.
Agar "Saved website context" block maujood hai, to is baar ka message,
chahe kitni bhi tarah likha ho, agar apne (saved website ke) business/
niche/customers/market ke baare mein hi baat kar raha hai — bina kisi
naye, alag topic/brand ke naam liye — to ALWAYS use_website_context:
true return karo. Isme koi bhi phrasing chal sakti hai: "aur dikhao",
"iske baare mein reviews", "kya log isko pasand karte hain", "sales
kaisi hain" — sirf fixed keywords match hona zaroori nahi. Sirf tab
use_website_context: false do jab user CLEARLY ek naya, alag topic/
brand/industry name kare jiska is website se koi taaluq nahi. This
check applies BEFORE you decide to generate your own fresh keywords for
a "search" intent, too — cases 2 and 3 above only apply once you've
confirmed the message is NOT actually an own-business follow-up per
this rule; never silently generate unrelated keywords for what is
really an own-business request just because it doesn't literally repeat
earlier wording.
Extended search JSON shape:
{"intent":"search","reply":null,"keywords":[...]|null,"time_window_days":<int|null>,"match_phrases":[...]|null,"evidence_required":<int|null>,"website_only":false,"url_ask_type":"generic_own_business"|"specific_topic"|"none"|null,"use_website_context":<true|false>,"website_topic_relation":"related"|"unrelated"|null}
"""

# (USAGE-BASED CREDITS) Asks the SAME router call for the search's effort
# category and a whole-number cost inside that category's range (config.py
# CREDIT_COST_RANGES). Purely additive: it changes no routing decision and no
# existing field; credit_cost.py validates the answer on the server and charges
# a fixed fallback for anything invalid. The model never touches a balance.
from config import CREDIT_COST_RANGES as _CREDIT_COST_RANGES

CLAUDE_ROUTER_EFFORT_ADDENDUM = """
SEARCH EFFORT (applies ONLY to the "search" shape — never add these keys to
"chat", "blocked" or "clarify"). Add three more keys to the "search" object:
"effort", "cost" and "reason". They only say how much work this search is; they
never change what you search for or how you route.
"effort" is exactly one of:
  "standard" — one clear topic, brand or question of ordinary scope. cost: a whole number from %d to %d
  "complex"  — several topics, segments, platforms or comparisons, or a narrow/filtered ask. cost: a whole number from %d to %d
  "deep"     — broad multi-angle research, large scope, or many constraints at once. cost: a whole number from %d to %d
"cost" is a plain JSON integer inside the range of the effort you chose (no quotes, no decimals).
"reason" is optional: at most 15 words on why, without quoting the user's message.
Extended search JSON shape:
{"intent":"search", ...all the search keys above..., "effort":"standard"|"complex"|"deep","cost":<int>,"reason":"<=15 words"|null}
""" % (_CREDIT_COST_RANGES["standard"] + _CREDIT_COST_RANGES["complex"] + _CREDIT_COST_RANGES["deep"])

CLAUDE_ROUTER_SYSTEM_PROMPT = (
    CLAUDE_ROUTER_SYSTEM_PROMPT
    + "\n" + flintel.ROUTER_UNFILTERED_ADDENDUM
    + "\n" + flintel.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM
    + "\n" + CLAUDE_ROUTER_WEBSITE_CONTEXT_ADDENDUM
    + "\n" + CLAUDE_ROUTER_EFFORT_ADDENDUM
)

CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT = """
You are the AI assistant inside Flintel, a social listening platform.
Answer the user's message naturally and directly, the way Claude or
ChatGPT would in any normal conversation. Plain language, no rigid
template, no JSON, no code blocks. Don't mention you're an AI or that
this is a "mock".

You also have access to a live web_search tool — use it whenever the
user's question is about something current, recent, or beyond your own
training knowledge, instead of saying you don't have real-time access or
citing a knowledge cutoff. Search first, then answer plainly and
naturally from what you find.
"""

# (v6) Safety-net text used only if the router itself flagged a message as
# "blocked" but, for whatever reason, didn't return usable reply text —
# never re-sent to Claude (no extra call, and no reason to hand harmful
# content to another prompt just to get a decline message).
CLAUDE_BLOCKED_FALLBACK_REPLY = (
    "I can't help with that one. Happy to help you look into a brand, "
    "product, or topic instead, or just chat about something else."
)

# (TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE, reworded by ROUTER INTENT
# REFINEMENT) Same safety-net pattern as CLAUDE_BLOCKED_FALLBACK_REPLY
# above: used only if the router flagged "clarify" but didn't return
# usable reply text — never re-sent to Claude. Reworded to match the same
# consultant tone the router prompt now asks for, and to mention the
# website-link shortcut (handled automatically by the existing,
# UNCHANGED WEBSITE-URL KEYWORD EXTRACTION FEATURE on the user's next
# message).
# (MULTI-TURN CONTEXT) Shown only when the router failed twice in a chat
# that already has history (see classify_and_maybe_chat): no search is run,
# because the message may depend on that history.
ROUTER_UNAVAILABLE_REPLY = (
    "Sorry — I couldn't process that message just now, so I didn't run a "
    "search (I'd rather not guess and show you unrelated posts). Please send "
    "it again, or write out the full request."
)

CLAUDE_CLARIFY_FALLBACK_REPLY = (
    "Bilkul — bas yeh batayein aap kis brand, product, ya industry ke "
    "baare mein Reddit aur baaki platforms se data dekhna chahte hain. "
    "Agar apni website ka link bhi share kar dein, main us se directly "
    "aap ke business se related conversations nikaal dunga."
)


def _parse_router_json(raw: str, allow_previous_results: bool = False):
    """Best-effort JSON parse of the router's output — strips ```json
    fences if Claude added them anyway, and validates the shape. Returns
    None on anything unexpected so the caller falls back to the safe
    "search" default instead of ever guessing.

    (v6) Accepts "blocked" alongside "search"/"chat" — a reply is
    expected as a string for both, and if it isn't one, the caller's
    fallback text is used instead.

    (KEYWORD-GENERATION SWAP) Now also parses/validates a "keywords"
    field for "search" intent: must be a JSON array of non-empty
    strings; each is trimmed, de-duplicated case-insensitively, and
    capped at CLAUDE_MAX_KEYWORDS. Anything malformed (missing, not a
    list, empty after cleaning) simply results in keywords=None — the
    caller (classify_and_maybe_chat / the /search route) is what applies
    the generate_fuzzy_keywords() fallback in that case, so this function
    itself never needs to know about that fallback.

    (TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE):
      - Accepts "clarify" as a 4th valid intent, treated like "chat"/
        "blocked" for reply-string validation (a string reply is
        expected; anything else falls back to None so the caller's own
        fallback text is used).
      - Also parses/validates "time_window_days" for "search" intent:
        must be a positive int (or a numeric string Claude accidentally
        quoted), clamped between 1 and MAX_TIME_WINDOW_DAYS. Anything
        else (missing, null, zero, negative, non-numeric, or intent !=
        "search") simply results in time_window_days=None, meaning "no
        time filter" — the exact same as if this feature didn't exist.

    (EVIDENCE-BUDGET FEATURE):
      - Also parses/validates "evidence_required" for "search" intent
        ONLY: accepts an int (or a numeric string Claude accidentally
        quoted), clamped between MIN_ANALYSIS_EVIDENCE and
        MAX_ANALYSIS_EVIDENCE. Anything else (missing, null, non-numeric,
        or intent != "search") simply results in evidence_required=None
        — exactly the same "missing means use the default" convention
        already used by "keywords"/"time_window_days" above.

    (ROUTER INTENT REFINEMENT) This function's logic is completely
    UNCHANGED — the refinement is pure prompt wording inside
    CLAUDE_ROUTER_SYSTEM_PROMPT above; the four valid intents, their
    field shapes, and every validation/clamping rule here are identical
    to before.

    (CHAT WEB-SEARCH FEATURE) This function's logic is completely
    UNCHANGED — the feature only affects _call_claude()'s request
    payload and does not change the router's own JSON output contract in
    any way.

    (PHRASE-MATCHING FEATURE) Also parses/validates a NEW, SEPARATE
    "match_phrases" field for "search" intent — cleaned the same way
    "keywords" is (trimmed, non-strings/empties dropped, de-duplicated
    case-insensitively), but capped at 7 entries instead of
    CLAUDE_MAX_KEYWORDS. A missing/malformed field simply results in
    match_phrases=None — no exception, no different fallback behavior
    for anything else in this function. This field never touches the
    job document or the Google search call; it exists purely for
    get_matched_signals()'s own loose title/text phrase check."""
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    # (OPTION A — REUSE PREVIOUS RESULTS) a 5th intent, accepted ONLY when the
    # caller showed the router the previous results (allow_previous_results).
    if intent == "previous_results" and allow_previous_results:
        numbers = []
        for n in data.get("previous_results_numbers") or []:
            if isinstance(n, int) and not isinstance(n, bool) and n > 0 and n not in numbers:
                numbers.append(n)
        return {"intent": "previous_results", "reply": None, "keywords": None, "time_window_days": None,
                "unfiltered": False, "match_phrases": None, "evidence_required": None,
                "website_only": False, "use_website_context": False, "website_topic_relation": None,
                "url_ask_type": None, "target_entity": None, "intent_types": None, "resolved_query": None,
                "previous_results_numbers": numbers or None}
    if intent not in ("search", "chat", "blocked", "clarify"):
        return None
    reply = data.get("reply")
    if intent in ("chat", "blocked", "clarify") and not isinstance(reply, str):
        reply = None

    keywords = None
    match_phrases = None
    time_window_days = None
    evidence_required = None
    unfiltered = False
    website_only = False
    use_website_context = False
    website_topic_relation = None
    url_ask_type = None
    target_entity = None
    intent_types = None
    resolved_query = None
    effort = cost = effort_reason = None   # (USAGE-BASED CREDITS) raw; credit_cost.py validates
    if intent == "search":
        effort, cost, effort_reason = data.get("effort"), data.get("cost"), data.get("reason")
        raw_keywords = data.get("keywords")
        if isinstance(raw_keywords, list):
            cleaned_keywords = []
            seen = set()
            for kw in raw_keywords:
                if not isinstance(kw, str):
                    continue
                kw_clean = kw.strip()
                if not kw_clean:
                    continue
                key = kw_clean.lower()
                if key in seen:
                    continue
                seen.add(key)
                cleaned_keywords.append(kw_clean)
                if len(cleaned_keywords) >= CLAUDE_MAX_KEYWORDS:
                    break
            keywords = cleaned_keywords or None

        raw_phrases = data.get("match_phrases")
        if isinstance(raw_phrases, list):
            cleaned_phrases = []
            seen_phrases = set()
            for phrase in raw_phrases:
                if not isinstance(phrase, str):
                    continue
                phrase_clean = phrase.strip()
                if not phrase_clean:
                    continue
                key = phrase_clean.lower()
                if key in seen_phrases:
                    continue
                seen_phrases.add(key)
                cleaned_phrases.append(phrase_clean)
                # (SEMANTIC QUERY REPRESENTATION FIX) Raised from a hard 7
                # to ROUTER_MAX_MATCH_PHRASES. match_phrases are now the
                # primary semantic search representation (the router is
                # asked for 8-12 full intent-expressing sentences covering
                # every relevant intent_type), so truncating at 7 would
                # silently discard whole intent angles — e.g. dropping
                # every seller-voice phrase and leaving only buyer-voice
                # ones — which is exactly the recall loss this change set
                # exists to fix.
                if len(cleaned_phrases) >= ROUTER_MAX_MATCH_PHRASES:
                    break
            match_phrases = cleaned_phrases or None

        # (INTENT TAXONOMY) Two new, optional, purely additive fields.
        # Both default to None, and every existing caller that ignores
        # them behaves exactly as before.
        raw_target_entity = data.get("target_entity")
        if isinstance(raw_target_entity, str) and raw_target_entity.strip():
            target_entity = raw_target_entity.strip()

        # (MULTI-TURN CONTEXT) The router's standalone restatement of the
        # current request, resolved against the conversation history.
        raw_resolved = data.get("resolved_query")
        if isinstance(raw_resolved, str) and raw_resolved.strip():
            resolved_query = " ".join(raw_resolved.split())[:500]

        raw_intent_types = data.get("intent_types")
        if isinstance(raw_intent_types, list):
            cleaned_intent_types = []
            for it in raw_intent_types:
                if not isinstance(it, str):
                    continue
                it_clean = it.strip().lower()
                if it_clean in _VALID_INTENT_TYPES and it_clean not in cleaned_intent_types:
                    cleaned_intent_types.append(it_clean)
            intent_types = cleaned_intent_types or None

        raw_window = data.get("time_window_days")
        parsed_window = None
        if isinstance(raw_window, bool):
            parsed_window = None  # guard: bool is a subclass of int in Python
        elif isinstance(raw_window, int):
            parsed_window = raw_window
        elif isinstance(raw_window, str) and raw_window.strip().isdigit():
            parsed_window = int(raw_window.strip())
        if isinstance(parsed_window, int) and parsed_window > 0:
            time_window_days = min(parsed_window, MAX_TIME_WINDOW_DAYS)

        # (EVIDENCE-BUDGET FEATURE) Same accept-int-or-numeric-string
        # tolerance as time_window_days above, but clamped into
        # [MIN_ANALYSIS_EVIDENCE, MAX_ANALYSIS_EVIDENCE] instead of
        # [1, MAX_TIME_WINDOW_DAYS] — the backend is the final authority
        # on the bound regardless of what the model actually picked.
        raw_evidence = data.get("evidence_required")
        parsed_evidence = None
        if isinstance(raw_evidence, bool):
            parsed_evidence = None  # guard: bool is a subclass of int in Python
        elif isinstance(raw_evidence, int):
            parsed_evidence = raw_evidence
        elif isinstance(raw_evidence, str) and raw_evidence.strip().isdigit():
            parsed_evidence = int(raw_evidence.strip())
        if isinstance(parsed_evidence, int):
            evidence_required = max(MIN_ANALYSIS_EVIDENCE, min(parsed_evidence, MAX_ANALYSIS_EVIDENCE))

        unfiltered = bool(data.get("unfiltered") is True)
        website_only = bool(data.get("website_only") is True)
        use_website_context = bool(data.get("use_website_context") is True)
        _rel = data.get("website_topic_relation")
        website_topic_relation = _rel if _rel in ("related", "unrelated") else None
        _uat = data.get("url_ask_type")
        url_ask_type = _uat if _uat in ("generic_own_business", "specific_topic", "none") else None

    # (DATA-BACKED GUIDANCE) a business-guidance question (see the router prompt)
    guidance = bool(data.get("guidance") is True and intent in ("search", "clarify"))
    return {"intent": intent, "reply": reply, "keywords": keywords, "time_window_days": time_window_days,
            "unfiltered": unfiltered, "match_phrases": match_phrases, "evidence_required": evidence_required,
            "website_only": website_only, "use_website_context": use_website_context,
            "website_topic_relation": website_topic_relation, "url_ask_type": url_ask_type,
            "target_entity": target_entity, "intent_types": intent_types,
            "resolved_query": resolved_query, "guidance": guidance,
            "effort": effort, "cost": cost, "reason": effort_reason}


# ─────────────────────────────────────────────────────────────────────────────
# (SMALL-TALK FAST PATH) A message that is ONLY a greeting / "how are you" /
# thanks / goodbye / "what can you do" gets a ready reply here, so it never
# pays for the router LLM call (a ~30k-character prompt with web search on).
# The WHOLE message must be one of these phrases (after removing punctuation,
# an optional leading greeting and an optional "there / Flintel / team"):
# "hello, find people looking for a CRM" is not small talk and goes to the
# router exactly as before. Anything unsure returns None = router as before.
# Deliberately NOT covered: yes / no / ok (they can answer a question asked
# earlier in the chat), anything with a URL or a digit, non-English small
# talk (the router replies in the user's own language) — except a bare name
# introduction ("mera naam Hamza hai"), handled by _introduced_name() below.
# ─────────────────────────────────────────────────────────────────────────────
_SMALLTALK_MAX_WORDS = 8
_SMALLTALK_LEAD = re.compile(
    r"^(?:(?:hi+|hello+|hey+|hiya|howdy|yo|greetings|good\s+(?:morning|afternoon|evening|day))"
    r"(?:\s+(?:there|flintel|team|all|everyone|friend|buddy))?\s+)+"
)
_SMALLTALK_TAIL = re.compile(r"(?:\s+(?:there|flintel|team|all|everyone|friend|buddy|mate|today|again|so\s+much|a\s+lot|man))+$")
_SMALLTALK_PHRASES = {
    "greeting": {"hi", "hii", "hello", "hey", "heya", "hiya", "howdy", "yo", "greetings",
                 "good morning", "good afternoon", "good evening", "good day"},
    "how_are_you": {"how are you", "how are you doing", "how r u", "how are u", "how r you",
                    "how is it going", "hows it going", "how s it going", "how are things",
                    "how is your day", "how s your day", "hows your day", "how have you been",
                    "how do you do", "whats up", "what s up", "wassup", "sup"},
    "thanks": {"thanks", "thank you", "thank u", "thx", "ty", "many thanks", "thanks a lot",
               "appreciate it", "much appreciated", "cheers", "great thanks", "ok thanks",
               "okay thanks", "ok thank you", "okay thank you", "perfect thanks", "awesome thanks"},
    "goodbye": {"bye", "goodbye", "good bye", "bye bye", "see you", "see you later", "see ya",
                "good night", "take care"},
    "capabilities": {"what can you do", "what do you do", "what can flintel do", "what does flintel do",
                     "what are you", "who are you", "what is flintel",
                     "what can i ask you",
                     "what can i ask", "help", "what are your capabilities", "what can you help with",
                     "what can you help me with"},
}
SMALLTALK_REPLIES = {
    "greeting": ("Hi! I'm Flintel. Tell me a brand, product, or topic and I'll find what people are "
                 "saying about it on Reddit — or the people actively looking for it."),
    "how_are_you": ("Doing well, thanks for asking! What would you like to look into today — a brand, "
                    "a product, or the people looking for something like yours?"),
    "thanks": "You're welcome! Let me know whenever you want to dig into something else.",
    "goodbye": "Bye for now! Come back anytime you want to see what people are saying about something.",
    "capabilities": ("I search real Reddit conversations for you. A few things you can ask:\n\n"
                     "- **Find buyers and leads** — \"Find people looking for a CRM for small teams\"\n"
                     "- **See what people say about a brand or product** — \"What do people complain about with Notion?\"\n"
                     "- **Spot pain points and trends** in a market or topic\n"
                     "- **Share your website link** and I'll find conversations relevant to your business\n\n"
                     "Just type what you're looking for."),
    # (CAPABILITY QUESTIONS)
    "how_it_works": ("Here's how Flintel works:\n\n"
                     "1. **You ask in plain words** — a brand, a product, a market, or the kind of people you want to find.\n"
                     "2. **I search real public conversations** Flintel has collected (Reddit today; X / Twitter, LinkedIn "
                     "and Facebook are coming soon) and match posts by meaning, not just keywords.\n"
                     "3. **I read the matching posts and answer** with what they actually show — buyers kept apart from "
                     "sellers and general discussion — and link every original post so you can check it.\n\n"
                     "You can narrow a search by platform or by time (\"from the last 3 hours\", \"October 9 to 12\"). "
                     "What would you like to look into?"),
    "how_to_use": ("Using Flintel is simple:\n\n"
                   "1. **Type what you're looking for**, e.g. \"Find people looking for a CRM for a small team\" or "
                   "\"What do people complain about with Notion?\"\n"
                   "2. **Optionally pick a platform** with the selector next to the send button, or add a time range "
                   "(\"from yesterday\", \"last 24 hours\").\n"
                   "3. **Read the answer** — each point links to the original posts.\n"
                   "4. **Follow up** in the same chat: ask for outreach ideas for those posts, narrow the search, "
                   "or share your website link so I can look for conversations relevant to your business.\n\n"
                   "What would you like to start with?"),
    "platforms": ("Right now I search **Reddit**. X / Twitter, LinkedIn and Facebook are coming soon — you'll see "
                  "them in the platform selector next to the send button. Tell me what you'd like to look into."),
}


# (CAPABILITY QUESTIONS) A question whose SUBJECT is Flintel itself and that
# names NO research target: what it can do / find, how it works, how to use
# it, which platforms it covers. Parsed as a whole sentence — question word,
# auxiliary, Flintel as the subject, a capability verb, then at most a
# "for me / for my business" beneficiary and filler. Any other word left over
# (a brand, a topic, "find buyers for my CRM") means it is not this intent and
# the message goes to the router as before.
_CAP_SELF = r"(?:you|u|flintel|this|it|this (?:app|tool|platform|thing|site|product|bot|service)|the (?:app|tool|platform|bot|service))"
_CAP_AUX = r"(?:can|could|will|would|do|does|should|might|are|is)"
_CAP_BENEF = (r"(?: (?:for|to) (?:me|us|you|people like me|someone like me|my (?:(?:small|own|new|online|local) )?(?:business|company|team|startup|brand|agency|"
              r"shop|store|product|work|clients)|our (?:(?:small|own|new|online|local) )?(?:business|company|team|startup|brand|agency|shop|store|product)))?")
_CAP_FILLER = r"(?: (?:exactly|here|actually|really|in general|anyway|again|then|now|right now|today|please|best|well|"\
              r"properly|effectively|in short|basically|in practice))*"
_CAP_KIND = r"(?:exactly )?(?:(?:all|else|kinds? of things|sorts? of things|types? of (?:things|data|posts|results|stuff|leads|info|information)|stuff) )?"
_CAP_VERB = (r"(?:do|find|search|search for|track|monitor|offer|show(?: me| us)?|tell(?: me| us)?|look for|discover|analy[sz]e|"
             r"dig up|get(?: me| us)?|help(?: me| us)?(?: with)?|provide|give(?: me| us)?|cover|support|research)")
_CAP_PATTERNS = [
    ("capabilities", rf"what {_CAP_KIND}{_CAP_AUX} {_CAP_SELF} {_CAP_VERB}{_CAP_BENEF}{_CAP_FILLER}"),
    ("capabilities", rf"what {_CAP_KIND}{_CAP_SELF} {_CAP_AUX} {_CAP_VERB}{_CAP_BENEF}{_CAP_FILLER}"),   # "tell me what you can do"
    ("capabilities", rf"(?:how )?{_CAP_AUX} {_CAP_SELF} help(?: me| us)?{_CAP_BENEF}{_CAP_FILLER}"),
    ("capabilities", rf"how {_CAP_AUX} {_CAP_SELF} (?:help|assist)(?: me| us| my (?:business|company|team|startup|brand|agency|shop|store))?"
                     rf"{_CAP_BENEF}{_CAP_FILLER}"),
    ("capabilities", rf"how {_CAP_AUX} {_CAP_SELF} be (?:useful|helpful|of use){_CAP_BENEF}{_CAP_FILLER}"),
    ("capabilities", rf"what {_CAP_AUX} {_CAP_SELF} (?:good for|useful for|for|capable of|able to do){_CAP_FILLER}"),
    ("capabilities", r"what (?:are|is) (?:your|flintel s|flintels|its|the) (?:capabilities|features|skills|abilities|use cases?)"
                     + _CAP_FILLER),
    ("capabilities", r"(?:who|what) (?:are|is) (?:you|flintel|this)" + _CAP_FILLER),
    ("capabilities", r"what can (?:i|we) (?:ask|do|search|find|look for|use (?:you|flintel|it|this) for)"
                     r"(?: (?:you|with you|here|with flintel|on flintel|with this|with it))?" + _CAP_FILLER),
    ("capabilities", r"what should (?:i|we) ask(?: you| flintel)?" + _CAP_FILLER),
    ("how_it_works", rf"how {_CAP_AUX} {_CAP_SELF} (?:work|works|find (?:things|posts|stuff|results|leads)|search|get (?:its|your) data)"
                     rf"{_CAP_FILLER}"),
    ("how_it_works", r"how (?:does|do) (?:flintel|this|it|this (?:app|tool|platform|thing)) work" + _CAP_FILLER),
    ("how_it_works", r"where (?:do|does) (?:you|flintel|this|it) (?:get|find) (?:your|its|the) (?:data|posts|results|information)"
                     + _CAP_FILLER),
    ("how_to_use", rf"how (?:do|can|should|would) (?:i|we) (?:use|start using|get started with|work with|make the most of|get the most out of) "
                   rf"{_CAP_SELF}{_CAP_BENEF}{_CAP_FILLER}"),
    ("how_to_use", rf"how to (?:use|get started with|start using) {_CAP_SELF}{_CAP_BENEF}{_CAP_FILLER}"),
    ("how_to_use", r"(?:how|where) (?:do|should|can) (?:i|we) (?:get started|start|begin)(?: with (?:you|flintel|this|it))?"
                   + _CAP_FILLER),
    ("platforms", r"(?:what|which) (?:platforms|sites|websites|sources|networks|social networks|channels) "
                  rf"(?:do|can|does) {_CAP_SELF} (?:search|cover|support|use|check|monitor|track|look at|work with)" + _CAP_FILLER),
]
_CAP_RES = [(cat, re.compile(rf"^{pat}$")) for cat, pat in _CAP_PATTERNS]
_CAP_TAIL = re.compile(r"(?:\s+(?:there|team|all|everyone|friend|buddy|mate|again|man))+$")
_CAP_LEAD = re.compile(r"^(?:(?:so|ok|okay|um+|uh+|well|and|but|please|just curious|quick question|"
                       r"tell me|can you tell me|could you tell me|i want to know|i d like to know|i would like to know|"
                       r"i m curious|i am curious|i wonder|i was wondering)\s+)+")


def capability_question(query: str):
    """The capability sub-intent ("capabilities" | "how_it_works" | "how_to_use"
    | "platforms") when `query` is a question about Flintel itself with no
    research target, else None."""
    text = (query or "").strip().lower()
    if not text or len(text) > 120 or "://" in text or "www." in text or re.search(r"\d", text):
        return None
    text = text.replace("’", "'")
    text = re.sub(r"[^a-z' ]+", " ", text)
    text = re.sub(r"\s+", " ", text.replace("'", " ")).strip()
    text = _CAP_TAIL.sub("", _SMALLTALK_LEAD.sub("", text + " ").strip()).strip()   # keeps "flintel" (a subject here)
    text = _CAP_LEAD.sub("", text + " ").strip()
    if not text or len(text.split()) > 14:
        return None
    for cat, rx in _CAP_RES:
        if rx.match(text):
            return cat
    return None


# ─────────────────────────────────────────────────────────────────────────────
# (GROUNDED ANSWERS) Exploratory questions about what Flintel could do for a
# named goal ("How can you help me find leads for my CRM?") are answered from
# Flintel's VERIFIED product behaviour only — never with market advice,
# example pain points or opportunities Flintel has not found — and end with an
# offer to run the real search. Nothing has been searched at that point, and
# the reply says so. A bare "yes" to that offer starts exactly that search.
#
# Every capability named below is implemented in this codebase:
#   * search over posts Flintel has collected, matched by meaning
#     (embedding retrieval in get_matched_signals);
#   * Reddit now; X / Twitter, LinkedIn, Facebook shown as "Coming soon" in
#     the platform selector (templates);
#   * each post shown with its original link and, when stored, its original
#     publication time (results / published_at);
#   * buyers kept apart from sellers and general discussion (analysis and
#     outreach rules); time ranges (timerange.py); outreach notes on the posts
#     already shown (Option A).
# ─────────────────────────────────────────────────────────────────────────────
_EXPLORE_VERBS = (r"find|get|reach|discover|identify|spot|research|track|monitor|understand|analy[sz]e|grow|generate|"
                  r"land|win|attract|source|validate|learn about|see|look for|search for|connect with|sell to|market to")
_EXPLORE_PATTERNS = [
    rf"^how (?:can|could|would|will|do|does|might) (?:you|flintel|this|it) (?:help (?:me|us) |help )?(?:to )?"
    rf"(?P<verb>{_EXPLORE_VERBS}) (?P<rest>.+)$",
    r"^how (?:can|could|would|will|might) (?:you|flintel|this|it) help(?: me| us)? (?:with|for|on) (?P<rest>.+)$",
    rf"^how (?:can|could|do|should|would) (?:i|we) use (?:you|flintel|this|it) (?:to|for) (?:(?P<verb>{_EXPLORE_VERBS}) )?(?P<rest>.+)$",
    r"^what (?:can|could|would) (?:you|flintel|this|it) (?:do|find) for (?P<rest>(?:my|our|a|an|the) .+)$",
    r"^(?:is|would|could) (?:flintel|this|it|you) (?:be )?(?:useful|helpful|good|any good) (?:for|to) (?P<rest>.+)$",
    r"^(?:can|could) (?:you|flintel|this|it) help(?: me| us)? with (?P<rest>.+)$",
]
_EXPLORE_RES = [re.compile(p, re.IGNORECASE) for p in _EXPLORE_PATTERNS]
_LEAD_WORDS_RE = re.compile(
    r"^(?:more |new |some |potential |paying |the right )?(?:leads|customers|clients|buyers|prospects|users|people|"
    r"subscribers)(?: (?:for|interested in|who need|who want|looking for|to buy|that need|that want))? "
    r"(?:(?:my|our|a|an|the) )?(?P<what>.+)$", re.IGNORECASE)


def _clean_question(query: str) -> str:
    text = (query or "").replace("’", "'").strip()
    text = re.sub(r"[?!.]+\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:(?:hi|hello|hey|so|ok|okay|well|please)[, ]+)+", "", text, flags=re.IGNORECASE)
    return text


def exploratory_target(query: str):
    """{"verb", "target", "offer"} for an exploratory "how could Flintel help
    me with X" question, else None. `offer` is the research request a "yes"
    will run. Questions about Flintel alone (no X) are capability questions."""
    if not query or len(query) > 220 or "://" in query or "www." in query.lower():
        return None
    if capability_question(query):
        return None
    text = _clean_question(query)
    for rx in _EXPLORE_RES:
        m = rx.match(text)
        if not m:
            continue
        rest = m.group("rest").strip(" ,")
        verb = (m.groupdict().get("verb") or "").lower()
        if len(rest.split()) < 1 or len(rest) < 2:
            return None
        lead = _LEAD_WORDS_RE.match(rest)
        if lead and lead.group("what"):
            what = lead.group("what").strip()
            article = "" if re.match(r"(?i)^(?:a|an|the|my|our)\b", what) else ("an " if what[:1].lower() in "aeiou" else "a ")
            offer = f"Find people looking for {article}{what}"
        elif verb in ("understand", "track", "monitor", "analyse", "analyze", "research", "learn about", "see"):
            offer = f"Find Reddit conversations about {rest}"
        elif verb:
            offer = f"Find Reddit posts that help me {verb} {rest}"
        else:
            offer = f"Find Reddit conversations about {rest}"
        target = f"{verb} {rest}".strip() if verb else f"with {rest}"
        return {"verb": verb, "target": target, "offer": offer[:200]}
    return None


VERIFIED_CAPABILITIES_TEXT = (
    "- **Search the posts Flintel has collected** — Reddit today; X / Twitter, LinkedIn and Facebook are coming "
    "soon — matched by meaning, not just exact keywords.\n"
    "- **Show every matching post with its original link** and, where it's stored, when it was posted.\n"
    "- **Keep people asking to buy separate** from sellers, competitors and general discussion.\n"
    "- **Narrow by platform or time** (\"from the last 24 hours\", \"October 9 to 12\").\n"
    "- **Turn the posts you get into outreach notes** for the ones that look like real prospects."
)


def exploratory_reply(info: dict) -> str:
    """Deterministic reply for exploratory_target(): verified capabilities, an
    explicit "nothing searched yet", and the exact search a "yes" will run."""
    return (
        f"I haven't searched anything for this yet, so I don't have leads, findings or numbers to show you. "
        f"Here's what I can actually do to help you {info['target']}:\n\n"
        f"{VERIFIED_CAPABILITIES_TEXT}\n\n"
        "What I can't do is promise results before searching — it depends on what people have actually posted and "
        "what Flintel has collected.\n\n"
        f"Want me to search now? Reply **yes** and I'll run: \"{info['offer']}\"."
    )


def scope_reply(offer: str) -> str:
    """(GROUNDED ANSWERS) Used instead of a model-written chat reply to a
    business / market question: no advice from general knowledge, just what
    Flintel can do and an offer to run the real search."""
    return (
        "I don't answer business or market questions from general knowledge — I answer from real conversations "
        "Flintel has collected, and I haven't searched for this yet, so I have no findings to share.\n\n"
        f"{VERIFIED_CAPABILITIES_TEXT}\n\n"
        f"Want me to search now? Reply **yes** and I'll run: \"{offer}\"."
    )


CHAT_UNAVAILABLE_REPLY = ("I couldn't put a reply together for that just now. I answer from the conversations "
                          "Flintel has collected — tell me a brand, product, topic, or the kind of people you want "
                          "to find, and I'll search.")

_CONFIRM_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|ok|okay|alright|go ahead|do it|please do|yes please|sure thing|sounds good|"
    r"let s do it|lets do it|let s go|go for it|search|search now|run it|haan|han|ha|ji|ji haan|jee|theek hai|"
    r"thik hai|kar do|karo|chalo)(?: (?:please|pls|now|go ahead|do it|search|kar do|karo))*$")


def is_confirmation(query: str) -> bool:
    """A bare yes / go ahead (English or Roman Urdu), nothing else."""
    text = re.sub(r"[^a-z' ]+", " ", (query or "").lower().replace("’", "'")).replace("'", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return bool(text) and len(text.split()) <= 5 and bool(_CONFIRM_RE.match(text))


_BUSINESS_Q_RE = re.compile(
    r"\b(?:customers?|clients?|leads?|buyers?|prospects?|market(?:s|ing)?|sales|sell(?:ing|s)?|revenue|growth|grow|"
    r"competitors?|competition|pricing|prices?|demand|trends?|trending|niche|audience|startups?|business(?:es)?|"
    r"brands?|products?|saas|campaigns?|strategy|strategies|acquisition|acquire|monetiz\w*|profit\w*|"
    r"conversion|churn|retention|outreach|opportunit\w*|pain points?|users?|subscribers?)\b"
    r"|\b(?:gahak|grahak|customer|kaarobar|karobar|bechna|bech)\b", re.IGNORECASE)


def is_business_question(query: str) -> bool:
    """True when a message is about customers, markets, competitors, demand,
    selling or growth — topics Flintel may only answer from collected data."""
    return bool(_BUSINESS_Q_RE.search(query or ""))


def smalltalk_reply(query: str):
    """Ready reply for a message that is ONLY small talk, else None (see above)."""
    cap = capability_question(query)          # (CAPABILITY QUESTIONS) questions about Flintel itself
    if cap:
        return SMALLTALK_REPLIES[cap]
    text = (query or "").strip().lower()
    if not text or len(text) > 80 or "://" in text or "www." in text or re.search(r"\d", text):
        return conversational_reply(query)
    text = text.replace("’", "'")
    text = re.sub(r"[^a-z' ]+", " ", text)          # punctuation / emoji -> space
    text = re.sub(r"\s+", " ", text.replace("'", " ")).strip()
    if not text or len(text.split()) > _SMALLTALK_MAX_WORDS:
        return conversational_reply(query)
    for category in ("greeting",):
        if text in _SMALLTALK_PHRASES[category]:
            return SMALLTALK_REPLIES[category]
    rest = _SMALLTALK_TAIL.sub("", _SMALLTALK_LEAD.sub("", text + " ").strip()).strip()
    if not rest:                                   # only greeting words ("hey there", "hello flintel")
        return SMALLTALK_REPLIES["greeting"]
    for category in ("how_are_you", "thanks", "goodbye", "capabilities"):
        if rest in _SMALLTALK_PHRASES[category]:
            return SMALLTALK_REPLIES[category]
    name = _introduced_name(rest)
    if name:
        return SMALLTALK_INTRO_REPLY.format(name=name)
    return conversational_reply(query)


# ─────────────────────────────────────────────────────────────────────────────
# (FAST CONVERSATION) Pure social messages — greetings, "how are you",
# thanks, "that was helpful", "I'm good, you?", goodbyes, in English or Roman
# Urdu, alone or combined ("Salam, kya haal hai?", "thank you so much, that
# was helpful") — are answered without the router LLM. The message is split
# into clauses and EVERY clause must be one of these social acts; a single
# clause with anything else in it ("hi, find CRM buyers", "thanks, now the
# USA") sends the whole message to the router as before. The reply is
# composed from the acts found (not one canned text per message), says
# nothing factual, and points back to research. Bare "ok" / "yes" / "no" are
# never treated as small talk: they may answer a question asked earlier.
# ─────────────────────────────────────────────────────────────────────────────
_CONV_FILLER = r"(?: (?:there|flintel|team|all|everyone|friend|buddy|mate|today|again|so much|a lot|man|bro|yaar|ji|sir))*"
_CONV_ACTS = {
    "greeting": (r"(?:hi+|hello+|hey+|hiya|howdy|yo|greetings|good (?:morning|afternoon|evening|day)|hey there)"
                 r"|(?P<ru_greet>salam|salaam|slam|aoa|as+alam\w*(?: o| u)?(?: alaikum| alikum| alaykum| aleikum)?|"
                 r"as+alamu?alaikum|adaab|namaste)"),
    "how_are_you": (r"(?:how are (?:you|u)(?: doing)?|how r (?:you|u)|how (?:is|s) it going|hows it going|"
                    r"how are things|how (?:is|s) your day(?: going| been)?|hows your day(?: going| been)?|"
                    r"how have you been|how do you do|what s up|whats up|wassup|sup|how about you|and you|"
                    r"what about you|hope you re (?:doing )?(?:well|good|fine)|hope you are (?:doing )?(?:well|good|fine)|"
                    r"hope all is well|hope you re having a good day)"
                    r"|(?P<ru_how>(?:aap |ap |tum )?(?:kya|kia|kiya) (?:haal|hal) (?:hai|hain|he|ha)|"
                    r"(?:aap |ap |tum )?(?:kaise|kese|kaisay|kesay|kaisy) (?:ho|hain|hen|he|hai)|"
                    r"(?:kya|kia) (?:chal raha|scene) (?:hai|he)|sab (?:theek|thik|khair) (?:hai|he))"),
    "wellbeing": (r"(?:i m|im|i am) (?:good|fine|great|well|ok|okay|alright|doing (?:well|good|great|fine|ok|okay))"
                  r"(?: too| as well)?|all good|doing (?:well|good|great|fine)|not bad|pretty good"
                  r"|(?P<ru_well>(?:main |mai |mein )?(?:theek|thik|thek) (?:hoon|hun|hu|hon)|alhamdulillah|alhamdolillah)"),
    "thanks": (r"(?:(?:ok|okay|haha+|lol|great|awesome|perfect|nice|cool|wow) )?(?:thanks|thank you|thank u|thx|ty|many thanks|thanks a (?:lot|ton|bunch)|"
               r"thank you (?:so|very) much|thanks so much|much appreciated|appreciate it|cheers)"
               r"|(?P<ru_thanks>(?:bohat |bahut |boht )?(?:shukriya|shukria|shukrya|meherbani|mehrbani)|jazak ?allah(?: khair)?)"),
    "appreciation": (r"(?:ok |okay )?(?:that (?:was|is|s) (?:very |really |so |super )?(?:helpful|great|useful|awesome|amazing|perfect|nice|cool)|"
                     r"(?:very |really |super )?(?:helpful|great|awesome|nice|cool|perfect|amazing|excellent|wonderful)|"
                     r"lol|haha+|hehe+|love it|good job|well done|nice one)"
                     r"|(?P<ru_appr>(?:bohat |bahut |boht )?(?:acha|achha|zabardast|kamaal|wah|shabash))"),
    "goodbye": (r"(?:bye|goodbye|good bye|bye bye|see you(?: later| soon)?|see ya|good night|take care|talk later|ttyl|"
                r"bye for now)"
                r"|(?P<ru_bye>(?:khuda|allah) hafiz)"),
}
_CONV_RES = {act: re.compile(rf"^(?:{pat}){_CONV_FILLER}$") for act, pat in _CONV_ACTS.items()}
_CONV_REPLY = {
    "en": {"greeting": "Hi!", "how_are_you": "Doing well, thanks for asking!", "wellbeing": "Glad to hear it!",
           "thanks": "You're welcome!", "appreciation": "Glad that helped!",
           "goodbye": "Bye for now — come back anytime you want to look into something.",
           "next": ("What would you like to look into — a brand, a product, or the people looking for something "
                    "like yours?")},
    "ru": {"greeting": "Wa alaikum assalam!", "how_are_you": "Main theek hoon, shukriya!",
           "wellbeing": "Sun kar acha laga!", "thanks": "Koi baat nahi!", "appreciation": "Khushi hui ke kaam aaya!",
           "goodbye": "Allah hafiz! Jab chahein wapas aaiye.",
           "next": "Batayein, kis brand, product ya topic par logon ki baatein dekhni hain?"},
}


def conversational_reply(query: str):
    """Reply for a message made ONLY of social acts (see above), else None."""
    raw = (query or "").strip().lower().replace("’", "'")
    if not raw or len(raw) > 160 or "://" in raw or "www." in raw or re.search(r"\d", raw):
        return None
    clauses = [re.sub(r"\s+", " ", re.sub(r"[^a-z' ]+", " ", c).replace("'", " ")).strip()
               for c in re.split(r"[,.!?;:\n]+|\s+(?:and|&)\s+", raw)]
    clauses = [c for c in clauses if c]
    if not clauses or sum(len(c.split()) for c in clauses) > 25:
        return None
    acts, roman_urdu, salam = [], False, False
    for clause in clauses:
        for act, rx in _CONV_RES.items():
            m = rx.match(clause)
            if m:
                acts.append(act)
                groups = m.groupdict()
                roman_urdu = roman_urdu or any(v for k, v in groups.items() if k.startswith("ru_"))
                salam = salam or bool(groups.get("ru_greet"))
                break
        else:
            return None                      # something that is not small talk
    if set(acts) == {"wellbeing"}:
        return None                          # "I'm fine" alone may answer a question asked earlier
    lang = _CONV_REPLY["ru" if roman_urdu else "en"]
    parts = []
    if "greeting" in acts:
        parts.append(_CONV_REPLY["ru"]["greeting"] if salam else _CONV_REPLY["en"]["greeting"])
    if "wellbeing" in acts:
        parts.append(lang["wellbeing"])
    if "how_are_you" in acts:
        parts.append(lang["how_are_you"])
    if "thanks" in acts and "wellbeing" not in acts:          # "I'm good, thanks" thanks for asking
        parts.append(lang["thanks"])
    elif "appreciation" in acts and "thanks" not in acts:
        parts.append(lang["appreciation"])
    parts.append(lang["goodbye"] if "goodbye" in acts else lang["next"])
    return " ".join(parts)


# (SMALL-TALK FAST PATH — INTRODUCTIONS) "I am Hamza" / "my name is Hamza" /
# "mera naam Hamza hai" — ONLY a name, nothing else ("I am Hamza from Lahore",
# "I am looking for…" go to the router). Words that describe a state or are
# common English ("I am fine", "I am confused", "I am new") are never names.
_INTRO_RE = re.compile(
    r"^(?:i am|i m|im|my name is|my name s|myself|this is|it s|its|mera naam|mera name|main|mai)\s+"
    r"([a-z]{2,15}(?: [a-z]{2,15})?)$")
_INTRO_TAIL_RE = re.compile(r"\s+(?:here|hoon|hun|hu|hai|he)$")
_NOT_A_NAME = set("""
a an the not so very just also still really from at in with on for to and or but your you here there back new
fine good great ok okay well ready done sure busy happy sad glad sorry tired bored lost stuck free available
alone home late early curious hungry sick ill alright cool awesome amazing nice right wrong good bad better
best fan user customer founder owner developer student manager marketer seller buyer freelancer agency startup
business company team admin human bot ai robot guest new old young back again today now online here also only
just about trying looking building working interested confused excited worried thinking wondering asking going
coming selling buying planning hoping searching struggling running doing what who why how where when which
""".split())
_NOT_A_NAME_SUFFIX = ("ed", "ing", "ly", "ful", "less", "ous", "ive", "able", "ible")
SMALLTALK_INTRO_REPLY = ("Nice to meet you, {name}! I'm Flintel. Tell me a brand, product, or topic and I'll find "
                         "what people are saying about it on Reddit — or the people actively looking for it.")


def _introduced_name(text: str):
    """"Hamza" (title-cased) when `text` is only a name introduction, else None."""
    m = _INTRO_RE.match(_INTRO_TAIL_RE.sub("", text or ""))
    if not m:
        return None
    words = m.group(1).split()
    if any(w in _NOT_A_NAME or w.endswith(_NOT_A_NAME_SUFFIX) for w in words):
        return None
    return " ".join(w.capitalize() for w in words)


# ─────────────────────────────────────────────────────────────────────────────
# (OPTION A — REUSE PREVIOUS RESULTS) When the latest research answer in a chat
# offered next steps such as "(A) the five posts formatted as outreach targets
# … (B) broader monitoring …", a reply choosing to work WITH those posts must
# reuse the exact stored posts, never start a new search. The router is shown
# the stored results (below) and may then answer intent "previous_results";
# routes.py builds the outreach report from the stored posts with
# build_outreach_report(). Choices that ask for new / more / broader / refined
# results stay "search" exactly as before (that is Option B).
# ─────────────────────────────────────────────────────────────────────────────
PREVIOUS_RESULTS_MAX_POSTS = 10


def build_previous_results_router_block(original_query: str, offered_next_step: str, posts: list) -> str:
    """Text appended to the router's user message (never to its system prompt)
    describing the latest research result in this chat and the one extra intent
    it may use for this message."""
    lines = []
    for i, raw in enumerate((posts if isinstance(posts, list) else [])[:PREVIOUS_RESULTS_MAX_POSTS]):
        p = _clean_stored_post(raw, i)
        sub = f"r/{p['source']}" if p["source"] else p["platform"]
        lines.append(f"{p['number']}. {sub} — {p['title'][:140] or 'Untitled post'}")
    shown = "\n".join(lines) if lines else "(that answer showed no posts)"
    offer = " ".join((offered_next_step or "").split())[:600] or "(none)"
    return (
        "Latest research result in this chat (stored, already shown to the user):\n"
        f"Request: {' '.join((original_query or '').split())[:300]}\n"
        f"Posts shown:\n{shown}\n"
        f"Next step the assistant offered: {offer}\n\n"
        "EXTRA INTENT FOR THIS MESSAGE ONLY — \"previous_results\": use it when the user's new message "
        "asks to do something WITH the posts already shown above — e.g. turn them into outreach targets, "
        "suggested messaging or reply ideas, qualify or rank them, or picks an offered option that means "
        "exactly that — WITHOUT asking for new, more, different, broader, or refined results. Return "
        "{\"intent\": \"previous_results\", \"previous_results_numbers\": [<post numbers>] or null}, using "
        "numbers only when the user limits it to specific posts. Any choice or request that needs new "
        "research (more posts, broader or continuous monitoring, another location, time range, or filter) "
        "is NOT previous_results — classify it with the normal rules. When unsure, use the normal rules."
    )


# ─────────────────────────────────────────────────────────────────────────────
# (DATA-BACKED GUIDANCE) A business-guidance question ("what's the best way to
# get customers?") that the router turned into a search over the user's market
# carries this note into the analysis step (via the message's existing note →
# extra_context), so the answer is built from the matched posts only.
# ─────────────────────────────────────────────────────────────────────────────
def build_guidance_note(question: str) -> str:
    q = " ".join((question or "").split())[:300]
    return (
        f"BUSINESS GUIDANCE REQUEST — the user asked: \"{q}\". Answer it ONLY from the posts above:\n"
        "- Report the patterns the posts actually show. Keep people actively looking to buy or switch "
        "separate from general discussion, from sellers or providers promoting their own offer, and from "
        "unrelated posts.\n"
        "- Name customer problems, unmet needs, or opportunities only where the posts show them, and point to "
        "the supporting posts.\n"
        "- Recommend only what follows from these posts; for each recommendation say which posts support it "
        "and why. Never present a strategy as \"the best\" — at most say what this set of posts suggests.\n"
        "- If the posts are too few, off-topic, or show no buying intent, say plainly that the monitored data "
        "is not enough for a reliable recommendation, and do not fill the gap with general business advice.\n"
        "- Sort the evidence into: buyers (asking to buy, switch or hire now), potential buyers (the problem, "
        "no stated plan to buy), general discussion, competitors / vendors / service providers, and product "
        "announcements. Self-promotion by a provider is never counted as buyer demand.\n"
        "- Say how many relevant posts each point rests on. With fewer than five relevant posts, call it a small "
        "sample and keep conclusions tentative.\n"
        "- Keep three things apart: what the posts show (observations, with their [Post N] references), what "
        "might explain it (hypotheses, labelled as such), and suggested next steps.\n"
        "- Product fit: suggest offering the user's product to someone only when that person's stated problem "
        "is one the user has said their product solves; otherwise say the fit is not verified. Never invent "
        "product capabilities, integrations or prices.\n"
        "- A post's \"Posted\" line is its original publication time. Mention dates only from those lines; never "
        "invent or estimate a date for a post without one, and flag old posts as possibly no longer current."
    )


# (OPTION A GUARD) Deterministic safety net for a reply that is NOTHING BUT a
# pick from the options the assistant itself offered ("(A) … or (B) …",
# "(1) … (2) …"). Anything longer than a bare pick goes to the router exactly as
# before. Used by routes.py so that picking the "work with these posts" option
# never starts a search (even if the router fails or mislabels it), and an
# unclear pick is asked about instead of searched.
import difflib as _difflib
import unicodedata as _unicodedata

_OFFER_MARKER_RE = re.compile(
    r"\(\s*([A-Da-d1-4])\s*\)"                 # (A)  (b)  (1)
    r"|(?<![\w(/])([A-D1-4])\)"                # A)   1)
    r"|\boption\s+([A-Da-d1-4])\b\s*[:\-—]?",  # option A / option 2
    re.IGNORECASE)
# The text around the markers must read as an offer, not as a numbered list.
_OFFER_CUE_RE = re.compile(
    r"\?|\b(?:prefer|would\s+you\s+like|do\s+you\s+want|want\s+me\s+to|which\s+(?:one|option|would)|"
    r"choose|pick|shall\s+i|should\s+i|i\s+can\s+(?:also\s+)?do|happy\s+to)\b", re.IGNORECASE)
_NUM_LETTER = {"1": "a", "2": "b", "3": "c", "4": "d"}
_OFFER_TRANSFORM_RE = re.compile(
    r"\b(?:outreach|messag\w*|repl(?:y|ies)|respond\w*|draft\w*|format\w*|template\w*|qualif\w*|rank\w*|"
    r"prioriti\w*|dm|dms|pitch\w*|these|those|above|listed|shown|existing)\b"
    r"|\bthe\s+(?:[\w\-]+\s+){0,2}(?:posts|results|leads|threads)\b", re.IGNORECASE)
_OFFER_NEW_SEARCH_RE = re.compile(
    r"\b(?:more|monitor\w*|broader|broaden\w*|expand\w*|additional|continuous\w*|ongoing|alerts?|track\w*|"
    r"new\s+search|another\s+search|search\s+again|wider|fresh|other\s+platforms|further\s+research)\b",
    re.IGNORECASE)


def _offer_fields(answer):
    """Every text field the stored answer DISPLAYS that could carry an offer,
    in priority order. chat.html shows followup_question as a chip in the
    committed template (the working copy hides it), so it is checked first;
    business_insight / executive_summary / conclusion etc. are rendered as text
    by renderSourceList()/renderComparison()/renderNoResults()."""
    if isinstance(answer, dict):
        data = answer
    else:
        text = answer if isinstance(answer, str) else ""
        try:
            data = json.loads(text.strip())
        except (ValueError, TypeError):
            return [text] if text.strip() else []
        if not isinstance(data, dict):
            return [text] if text.strip() else []
    out = []
    for key in ("followup_question", "followups", "conclusion", "business_insight", "business_insight_points",
                "executive_summary", "summary", "market_pattern", "message", "clarifying_question",
                "near_match_offer"):
        value = data.get(key)
        values = value if isinstance(value, list) else [value]
        joined = " ".join(v for v in values if isinstance(v, str) and v.strip())
        if joined.strip():
            out.append(joined)
    return out


def parse_offer(text: str) -> dict:
    """{"options": {"a": "<text>", "b": …}, "numbered": bool} for text that offers
    at least two lettered or numbered options, else {"options": {}}."""
    text = text if isinstance(text, str) else ""
    marks = list(_OFFER_MARKER_RE.finditer(text))
    options, numbered = {}, False
    for i, m in enumerate(marks):
        raw = (m.group(1) or m.group(2) or m.group(3)).lower()
        numbered = numbered or raw.isdigit()
        letter = _NUM_LETTER.get(raw, raw)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        chunk = re.split(r"[?!]|\.\s", text[m.end():end])[0]          # this option's own sentence only
        chunk = re.sub(r"^[\s:,\-—]+|[\s,;]+(?:or|and)?[\s,;]*$", "", chunk).strip()
        if chunk and letter not in options:
            options[letter] = chunk
    if len(options) < 2 or not _OFFER_CUE_RE.search(text):
        return {"options": {}, "numbered": False}
    return {"options": options, "numbered": numbered}


def parse_offer_options(offered: str) -> dict:
    """{"a": "<option text>", …} or {} (see parse_offer)."""
    return parse_offer(offered)["options"]


def find_offer_in_answer(answer):
    """(offer_text, offer) from the first displayed field of a stored answer that
    offers options; ("", {"options": {}}) when none does."""
    for text in _offer_fields(answer):
        offer = parse_offer(text)
        if offer["options"]:
            return text, offer
    return "", {"options": {}, "numbered": False}


def offer_option_kind(option_text: str) -> str:
    """"previous_results" (work with the posts already shown), "new_search"
    (needs new research), or "unclear"."""
    transform = bool(_OFFER_TRANSFORM_RE.search(option_text or ""))
    new = bool(_OFFER_NEW_SEARCH_RE.search(option_text or ""))
    if transform and not new:
        return "previous_results"
    if new and not transform:
        return "new_search"
    return "unclear"


# Words that may surround a pick without changing it (English + Roman Urdu).
_PICK_FILLER = set("""
ok okay yes yeah yep sure great cool alright hmm haan han ji jee theek thik acha achha accha chalo i i'd id i'll
ill i'm im think guess probably definitely def would d ll will let lets s us go with prefer choose pick want like
take do mujhe mere liye humein hamein hum main lunga lungi karo kar kardo kar do please pls plz thanks thank you
is sounds looks seems be better best good great fine perfect right works one wala wali walay waala waali hai he
hain chahiye chahie behtar sahi bilkul zabardast for me then it ahead that sir bro the my choice
""".split())
_ORDINAL_PICK = {"first": "a", "1st": "a", "second": "b", "2nd": "b", "third": "c", "3rd": "c", "fourth": "d",
                 "4th": "d", "pehla": "a", "pehli": "a", "pehle": "a", "doosra": "b", "dusra": "b", "doosri": "b",
                 "dusri": "b", "doosre": "b", "dusre": "b", "teesra": "c", "tisra": "c", "teesri": "c"}
_BOTH_PICK = {"both", "dono", "donon"}


def parse_option_pick(message: str):
    """Letters picked by a message that is ONLY an option pick, else None.
    "Option A is better", "go with B", "1", "2nd one", "pehla wala",
    "mujhe A chahiye", "dono" -> picks. "I want a" / "do a" (the article "a"),
    "number 1" (a post reference) and anything asking for more are not."""
    raw = _unicodedata.normalize("NFKC", message or "").replace("’", "'").strip()
    if not raw or "://" in raw or len(raw) > 80:
        return None
    raw = re.sub(r"'(?:d|ll|m|s|re|ve)\b", "", raw, flags=re.IGNORECASE)   # "I'd" -> I, "let's" -> let
    tokens = re.findall(r"\(\s*[A-Za-z1-4]\s*\)|[A-Za-z0-9]+", raw)
    if not tokens or len(tokens) > 10:
        return None
    picks, both = [], False
    for i, tok in enumerate(tokens):
        low = tok.lower().strip("'")
        prev = tokens[i - 1].lower() if i else ""
        if tok.startswith("("):                                   # "(A)" / "(1)"
            inner = tok.strip("() ").lower()
            picks.append(_NUM_LETTER.get(inner, inner))
        elif low in ("option", "choice", "opt"):
            continue
        elif low in _BOTH_PICK:
            both = True
        elif low in ("a", "b", "c", "d"):
            # "a" is also the English article: only a pick when written "A",
            # after option/choice, or as the very first word ("a please").
            if low == "a" and tok == "a" and prev not in ("option", "choice", "opt") and i != 0:
                return None
            picks.append(low)
        elif low in ("1", "2", "3", "4"):
            if prev in ("number", "no", "post", "#"):
                return None                                       # a post reference, not a pick
            picks.append(_NUM_LETTER[low])
        elif low in _ORDINAL_PICK:
            picks.append(_ORDINAL_PICK[low])
        elif low in ("and", "aur"):
            continue
        elif low in _PICK_FILLER:
            continue
        else:
            return None                                           # anything else: not a bare pick
    picks = list(dict.fromkeys(picks))
    if both:
        return sorted(picks) if len(picks) >= 2 else ["a", "b"]
    return picks if picks else None


def detect_offer_selection(message: str, offered: str):
    """None, or {"letters", "options", "numbered", "kind"} when `message` is only a
    pick from the options in `offered`. kind: "previous_results", "new_search",
    "both" (picked a "these posts" option and another one) or "unclear"."""
    letters = parse_option_pick(message)
    offer = parse_offer(offered)
    options = offer["options"]
    if not letters or not options:
        return None
    if any(letter not in options for letter in letters):
        return {"letters": letters, "options": options, "numbered": offer["numbered"], "kind": "unclear"}
    kinds = [offer_option_kind(options[letter]) for letter in letters]
    if len(kinds) == 1:
        kind = kinds[0]
    elif "previous_results" in kinds and "unclear" not in kinds:
        kind = "both"
    else:
        kind = "unclear"
    return {"letters": letters, "options": options, "numbered": offer["numbered"], "kind": kind}


def offer_clarification_reply(options: dict, routing_failed: bool = False, numbered: bool = False) -> str:
    """Asks which option the user meant, instead of guessing or searching."""
    labels = []
    for letter, text in sorted((options or {}).items()):
        kind = offer_option_kind(text)
        hint = (" — uses the posts I already showed you" if kind == "previous_results"
                else " — runs a new search" if kind == "new_search" else "")
        label = str("abcd".index(letter) + 1) if numbered and letter in "abcd" else letter.upper()
        labels.append(f"- **({label})** {_outreach_text(text, 240)}{hint}")
    lead = ("I couldn't process that reply just now, so I haven't started anything. "
            if routing_failed else "Just to check before I start anything — ")
    if not labels:
        return lead + "which option did you mean? Reply with the option, or tell me what you'd like me to do."
    return lead + "which one do you want?\n\n" + "\n".join(labels)


# A follow-up that talks about the posts already shown (so the router may need
# them) — the previous-results block is only sent then, or when the last answer
# offered options. Plain refinements ("now in USA") never get it.
_MENTIONS_PREVIOUS_RE = re.compile(
    r"\b(?:outreach|messag\w*|repl(?:y|ies)|respond\w*|draft\w*|dms?|pitch\w*|qualif\w*|rank\w*|prioriti\w*|"
    r"reach\s+out|contact\s+them|these|those|above|same\s+posts|inko|inhe|inhein|unko|unhe|unhein|yeh\s+posts?|"
    r"in\s+posts?|un\s+posts?)\b", re.IGNORECASE)
# A follow-up that asks for new / more / different research.
_NEW_RESEARCH_RE = re.compile(
    r"\b(?:more|additional|another|new|fresh|other|different|broader|wider|expand\w*|monitor\w*|track\w*|"
    r"search\w*|find|look\s+for|scan\w*|again|latest|recent|last\s+\w+\s+(?:days?|weeks?|months?|years?)|"
    r"past\s+\w+\s+(?:days?|weeks?|months?|years?)|this\s+(?:week|month|year)|mazeed|naye|nayi|naya|"
    r"dhoond\w*|dhund\w*|talash\w*)\b", re.IGNORECASE)


def mentions_previous_results(message: str) -> bool:
    return bool(_MENTIONS_PREVIOUS_RE.search(message or ""))


def requests_new_research(message: str) -> bool:
    return bool(_NEW_RESEARCH_RE.search(message or ""))


def previous_or_new_clarification(post_count: int) -> str:
    """Asked when the router wants to reuse the shown posts but the message also
    asks for new research — never silently turns a search into a report."""
    return ("Just to check before I start anything — do you want outreach notes for the "
            f"{post_count} posts I already showed you, or a new search for more posts? "
            "Tell me which, and I'll do just that.")


OUTREACH_REPORT_SYSTEM_PROMPT = """
You prepare outreach notes for a Flintel user from public social-media posts
that Flintel ALREADY found and showed them. Work only from the posts and the
context given. Return STRICT JSON only.

For EACH post, in this order of thinking:
1. Read the post text and title. What is the author's actual question,
   problem, goal, or experience? Copy the post's title exactly into
   "post_title", and quote the post exactly in "evidence" when a short quote
   supports your reading (Flintel checks titles and quotes against the stored
   post and drops notes it cannot match to it).
2. Who is the author? One of: "prospect" (has the need themselves),
   "researcher" (comparing or learning, no stated plan to buy), "seller"
   (offers a product/service, or looks for clients, users, testers or
   partners), "on_behalf" (asks for someone else), "unclear". A business
   owner describing their own business ("I run a bakery") is NOT a seller
   when they are looking for something to buy.
3. Category — be strict, never upgrade weak intent:
   - "qualified": the post itself states a current need the user's offer
     could plausibly meet (e.g. actively looking for / evaluating a tool).
   - "interested": the post shows genuine buying intent (asking for
     recommendations, comparing options to buy) but the specific need,
     constraints or timing are not stated.
   - "weak": related pain or interest, but no stated intent to buy or switch.
   - "ambiguous": the post could be read several ways, or the text is too
     thin to tell.
   - "irrelevant": not a fit (e.g. a seller, an unrelated topic).
4. Product fit — compare the author's stated problem with what the user has
   told Flintel about their offer ("What the user has told Flintel"):
   "fits" (the offer, as described, solves the stated problem), "partial"
   (solves part of it, or a key requirement — integration, platform,
   region, budget — is not confirmed), "unsupported" (it does not solve it;
   e.g. a broad booking engine for a host who needs a channel manager is not
   a fit just because both are "travel"), "unknown" (the user has not said
   what they offer, or the post does not say enough). Never assume a
   capability, integration or compatibility the user has not stated.
5. A truthful point of connection, taken from the post. Never invent one.
6. A conversational approach that helps the author first — usually answer or
   ask, not pitch. Never start with a generic opener or praise.
7. A draft — ONLY for "qualified" or "interested" posts whose fit is not
   "unsupported": a complete, ready-to-edit reply the user could adapt,
   written to THIS author about THIS post (3-6 short sentences): acknowledge
   their actual situation, give one genuinely useful point or question, and
   mention the user's offer only with facts the user gave — when fit is
   "unknown" or "partial", do not claim it solves their problem; ask instead.
   No links, usernames, contact details, prices, or invented experience. For
   every other post "draft" is null and "do_not_contact_reason" says why.

Truthfulness (absolute):
- Never invent the user's experiences, identity, expertise, results,
  customers, or any relationship with the author.
- Never invent product features, prices or outcomes. Only mention the user's
  offer using facts given under "What the user has told Flintel"; if that is
  empty or thin, refer to it neutrally ("your product") and list what is
  missing in "missing_info".
- Never treat weak or ambiguous intent as confirmed.
- No storytelling unless the given context supports it.
- No URLs, usernames, or contact details in your text.

Platform rules: Flintel cannot see each community's rules. A draft is for
the user to edit, never something Flintel posts or sends. Also give the key
point to acknowledge, one useful question, and a short outline. Note
anything that could break typical rules (self-promotion, unsolicited DMs,
AI-written content).

Style: plain, specific, concise. No corporate jargon, hype, or exaggerated
praise. Never repeat the same sentence pattern for every post.

Quality check before answering: every field is tied to THIS post, every
claim is supported by the post or the given context, nothing promotional
the author did not invite.

Output JSON:
{
  "posts": [
    {
      "number": <int, as given>,
      "post_title": "<this post's title, copied exactly>",
      "category": "qualified" | "interested" | "weak" | "ambiguous" | "irrelevant",
      "author_type": "prospect" | "researcher" | "seller" | "on_behalf" | "unclear",
      "need": "<the author's actual need/problem, in one sentence, or null if not stated>",
      "evidence": "<an EXACT short quote copied word for word from this post's title or text, or null>",
      "reasoning": "<one or two sentences: why this category>",
      "intent_warning": "<what is weak, ambiguous or unconfirmed — null only for clear cases>",
      "approach": "<how to engage helpfully, one or two sentences; null for irrelevant>",
      "acknowledge": "<the key point to acknowledge; null for irrelevant>",
      "question": "<one useful question to ask the author; null for irrelevant>",
      "outline": ["<2-4 short steps for the user's own reply>"],
      "platform_caution": "<rule risk to check for this post, or null>",
      "product_fit": "fits" | "partial" | "unsupported" | "unknown",
      "fit_reason": "<one sentence: which stated need the offer does or does not meet, and what is unverified>",
      "draft": "<complete ready-to-edit message, or null>",
      "do_not_contact_reason": "<why no outreach is suggested, or null>"
    }
  ],
  "missing_info": ["<information Flintel needs from the user to personalise further, e.g. what they sell>"]
}
"""

_OUTREACH_CATEGORIES = ("qualified", "interested", "weak", "ambiguous", "irrelevant")
_OUTREACH_HEADINGS = {
    "qualified": "Qualified prospects (qualified leads)",
    "interested": "Interested — potential leads: buying intent, but the need isn't spelled out",
    "weak": "Weak signals — market intelligence only, intent not confirmed",
    "ambiguous": "Ambiguous — read the post before reaching out",
    "irrelevant": "Not a fit — competitors / providers and unrelated posts, no outreach suggested",
}
_OUTREACH_FITS = {"fits": "Fits — the offer, as you described it, meets the stated need",
                  "partial": "Partial — part of the need, or a key requirement is unconfirmed",
                  "unsupported": "Doesn't fit the stated need",
                  "unknown": "Not verified — tell me what you offer, or the post doesn't say enough"}
_DO_NOT_CONTACT = {"weak": "no stated intent to buy or switch — useful as market intelligence only.",
                   "ambiguous": "intent is unclear — read the post before deciding to reach out.",
                   "irrelevant": "a seller, competitor or unrelated post — not a prospect."}
OUTREACH_STALE_DAYS = 90
_OUTREACH_AUTHOR = {"prospect": "Has the need themselves", "researcher": "Researching / comparing",
                    "seller": "Seller or service provider", "on_behalf": "Asking for someone else",
                    "unclear": "Unclear"}
_URL_IN_TEXT_RE = re.compile(
    r"\b[a-z][a-z0-9+.\-]*://\S+"                                   # any scheme://…
    r"|\bmailto:\S+|\bwww\.\S+"
    r"|\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+\b"                       # e-mail addresses
    r"|\b(?:[a-z0-9\-]+\.)+(?:com|net|org|io|co|ai|app|dev|me|us|uk|pk|in|biz|info|xyz|gg|so|ly)\b(?:/\S*)?",
    re.IGNORECASE)
OUTREACH_EVIDENCE_CHARS = 1500


def _outreach_text(value, limit=600):
    """LLM text field -> safe single-line markdown text (no links it made up)."""
    if not isinstance(value, str):
        return ""
    text = " ".join(_URL_IN_TEXT_RE.sub("(link removed)", " ".join(value.split())).split())
    text = text[:limit].replace("[", "(").replace("]", ")").replace("<", "‹").replace(">", "›")
    return text.replace("@", "＠")      # never lets the markdown renderer auto-link an e-mail


def _outreach_post_heading(post) -> str:
    title = ((post.get("title") or "Untitled post").strip().replace("[", "(").replace("]", ")"))[:200]
    where = f"r/{post['source']}" if post.get("source") else (post.get("platform") or "")
    link = post.get("link")
    head = f"[{title}]({link})" if isinstance(link, str) and link.startswith(("http://", "https://")) else title
    return f"**{head}**" + (f" — {where}" if where else "")


def outreach_posts_list(posts: list) -> str:
    """Plain list of the stored posts (title, community, original link)."""
    return "\n".join(f"{p['number']}. {_outreach_post_heading(p)}" for p in posts)


def _plain_str(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _clean_stored_post(post, index: int) -> dict:
    """A stored post with every field coerced to a safe type (malformed records
    never raise). Identity fields come only from the stored record."""
    post = post if isinstance(post, dict) else {}
    number = post.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        number = index + 1
    link = post.get("link")
    link = link.strip() if isinstance(link, str) and link.strip().startswith(("http://", "https://")) else None
    published = timerange.post_published_at(post)          # (DATE/TIME) original publication time or None
    return {"number": number, "title": _plain_str(post.get("title")), "source": _plain_str(post.get("source")),
            "platform": _plain_str(post.get("platform")), "link": link, "summary": _plain_str(post.get("summary")),
            "post_text": _plain_str(post.get("post_text")),
            "published_at": published.isoformat() if published is not None else None}


def _norm_words(text: str) -> str:
    """Lower-cased words in ANY script (Urdu, Arabic, Hindi, …): NFKC, combining
    marks dropped (so vowel marks never split a word), everything that is not a
    letter or digit becomes a space."""
    text = _unicodedata.normalize("NFKC", text or "").casefold()
    out = []
    for ch in text:
        cat = _unicodedata.category(ch)
        if cat.startswith("M"):
            continue
        out.append(ch if (ch.isalnum() and not cat.startswith("N") or ch.isdigit()) else " ")
    return " ".join("".join(out).split())


def _word_matches(word: str, hay_words: set) -> bool:
    if word in hay_words:
        return True
    if len(word) < 5:
        return False
    for h in hay_words:                       # spelling variants: "trialed" / "trialled"
        if len(h) >= 5 and (h[:5] == word[:5]) and _difflib.SequenceMatcher(None, word, h).ratio() >= 0.85:
            return True
    return False


def _post_text_for_matching(post) -> str:
    return " ".join([post["title"], post["post_text"] or post["summary"]])


def _evidence_matches_post(evidence, post) -> bool:
    """True when the model's quote verifiably comes from THIS stored post (title
    or text; Flintel's stored summary only when no post text was stored) — an
    exact match, or the same words allowing small spelling variants. A
    paraphrase is not verifiable and does not match."""
    ev = _norm_words(evidence if isinstance(evidence, str) else "")
    non_ascii = any(ord(c) > 127 for c in ev)
    if len(ev) < (4 if non_ascii else 8):
        return False
    hay = _norm_words(_post_text_for_matching(post))
    if ev in hay:
        return True
    ev_words = [w for w in ev.split() if len(w) >= (2 if non_ascii else 3)]
    hay_words = set(hay.split())
    if len(ev_words) < 3:
        return False
    return sum(_word_matches(w, hay_words) for w in ev_words) / len(ev_words) >= 0.85


def _title_matches_post(echo, post) -> bool:
    """True when the model's copy of the title is this stored post's title."""
    a, b = _norm_words(echo if isinstance(echo, str) else ""), _norm_words(post["title"])
    if not a or not b:
        return False
    if a == b or (len(a) >= 8 and (a in b or b in a)):
        return True
    words = a.split()
    return len(words) >= 3 and sum(_word_matches(w, set(b.split())) for w in words) / len(words) >= 0.85


def _attach_item(item, post, posts):
    """(ok, note): may these model notes be shown under `post`? The notes are
    tied to a post by its copied title and/or a verified quote; anything that
    points at another post, or could belong to several, is rejected."""
    others = [q for q in posts if q is not post]
    echo = item.get("post_title")
    title_here = _title_matches_post(echo, post)
    title_elsewhere = [q for q in others if _title_matches_post(echo, q)]
    quote_here = _evidence_matches_post(item.get("evidence"), post)
    quote_elsewhere = [q for q in others if _evidence_matches_post(item.get("evidence"), q)]
    if echo and not title_here and title_elsewhere:
        return False, f"these notes name post {title_elsewhere[0]['number']}, so they were left out here"
    if title_here and not title_elsewhere:
        if quote_elsewhere and not quote_here:
            return False, (f"these notes name this post but quote post {quote_elsewhere[0]['number']}, "
                           "so they were left out")
        return True, None
    if quote_here and not quote_elsewhere:
        return True, None
    if quote_here and quote_elsewhere:
        return False, (f"the quote also appears in post {quote_elsewhere[0]['number']}, so I couldn't tell "
                       "which post these notes are about")
    return False, "the notes could not be matched to this post's title or text, so they were left out"


_BUYER_SIGNAL_RE = re.compile(
    r"\b(?:looking\s+(?:for|to\s+(?:buy|switch|move|replace|get|find|try|use))"
    r"(?!\s+(?:\w+\s+){0,3}?(?:clients|customers|users|beta|testers|partners|partnerships|developers|"
    r"agencies|freelancers|co-?founders?|feedback|investors|sponsors|affiliates|resellers))"
    r"|need(?:s|ed)?\s+(?:a|an|some|help|to\s+find|recommendations?|something)|recommend\w*|suggestions?|"
    r"which\s+(?:\w+\s+){0,3}(?:should|do|would)|best\s+(?:\w+\s+){0,3}for|alternatives?\s+to|"
    r"switch\w*\s+(?:from|to|crms?|tools?|providers?)|moving\s+(?:off|away\s+from)|replace\s+(?:our|my)|"
    r"\w+\s+(?:vs\.?|versus)\s+\w+|\w+\s+or\s+\w+\s+for\s+(?:a|an|my|our)|evaluat\w*|trial\w*|budget|"
    r"willing\s+to\s+pay|want\s+to\s+(?:buy|switch|find|use|try|get)|shopping\s+for|in\s+the\s+market\s+for|"
    r"any\s+(?:good\s+)?(?:options|tools|apps|software|crms?)|worth\s+(?:it|paying|buying)|"
    r"chahiye|chahie|kaun\s*sa|kon\s*sa|konsa|kaunsa|suggest\s+kar\w*|recommend\s+kar\w*|batao|bataen|"
    r"lena\s+hai|leni\s+hai|khareed\w*|kharid\w*)\b"
    r"|چاہیے|کونسا|کون سا|تجویز|خرید|चाहिए|कौन सा|सुझाव|खरीद", re.IGNORECASE)
# Narrow on purpose: someone OFFERING a product/service or seeking users,
# testers, clients or partners — never "I run a bakery" / "my agency needs".
_SELLER_SIGNAL_RE = re.compile(
    r"\b(?:i|we)\s+(?:just\s+)?(?:built|launched|developed|shipped)\s+(?:a|an|my|our|this)\s+(?:[\w\-]+\s+){0,2}?"
    r"(?:app|tool|product|platform|saas|startup|extension|plugin|service|crm|website|site|bot)\b"
    r"|\b(?:i\s*'?m|i\s+am|we\s*'?re|we\s+are)\s+building\b|\bcheck\s+out\s+(?:my|our)\b"
    r"|\blooking\s+for\s+(?:\w+\s+){0,2}?(?:beta\s+testers|testers|early\s+users|first\s+users|"
    r"clients|customers|partners|partnerships|affiliates|resellers|investors)\b"
    r"|\bfeedback\s+on\s+(?:my|our)\b|\b(?:referral|affiliate)\s+(?:fees?|partners?|program)\b"
    r"|\bhire\s+me\b|\bdm\s+me\s+(?:for|if)\b|\b(?:we|i)\s+offer\s+(?:a|an|our|my)?\s*(?:services?|"
    r"solutions?|discounts?)\b", re.IGNORECASE)


def _honest_category(category: str, item: dict, post: dict):
    """(category, note). "qualified"/"interested" are only kept when the STORED
    post itself shows buying intent, the author is the buyer (or asks for one),
    and the post is not someone selling or seeking users. "qualified" also
    needs a stated need; without one, genuine buying intent becomes
    "interested". No formal quote is required — the stored post is checked.
    Downgrades carry a visible note; nothing is ever upgraded."""
    if category not in ("qualified", "interested"):
        return category, None
    text = _post_text_for_matching(post)
    author = item.get("author_type")
    buyer = bool(_BUYER_SIGNAL_RE.search(text))
    seller = bool(_SELLER_SIGNAL_RE.search(text))
    selling_note = "the post reads as someone selling, promoting, or looking for users — not treated as a prospect."
    if seller and not buyer:
        return "irrelevant", selling_note
    if author == "seller":
        if buyer and not seller:
            return "ambiguous", ("labelled a seller, but the post itself asks to buy something — "
                                 "read it before reaching out.")
        return "irrelevant", selling_note
    if seller:
        return "ambiguous", "the post mixes buying language with selling or looking for users — read it first."
    if author not in ("prospect", "on_behalf"):
        return "weak", "the author is not shown to be buying, so this stays a weak signal."
    if not buyer:
        return "weak", "the post itself does not state an intent to buy or switch, so this stays a weak signal."
    if category == "qualified" and not _plain_str(item.get("need")):
        return "interested", "the post shows buying intent, but the specific need isn't stated."
    return category, None


def _honest_fit(category: str, item: dict, business_context):
    """(category, fit_or_None, note). Product fit is only enforced when the model
    stated one ("product_fit"); an absent field means "not assessed" and changes
    nothing. "fits"/"partial" cannot be verified when the user has not said what
    they offer -> "unknown". Unsupported fit -> no outreach. A qualified lead
    needs a fit Flintel could check; otherwise it stays a potential lead."""
    fit = item.get("product_fit")
    if fit not in _OUTREACH_FITS:
        return category, None, None
    note = None
    if fit in ("fits", "partial") and not _plain_str(business_context):
        fit, note = "unknown", "you haven't told Flintel what you offer, so product fit can't be checked."
    if fit == "unsupported" and category != "irrelevant":
        return "irrelevant", fit, "the offer, as described, doesn't meet this person's stated need — do not contact."
    if category == "qualified" and fit in ("unknown", "partial"):
        return "interested", fit, (note or "product fit isn't confirmed, so this is a potential lead, not a "
                                          "qualified one.")
    return category, fit, note


def _outreach_posted(post) -> str:
    """The stored post's original publication date, with a staleness flag."""
    t = timerange.to_utc(post.get("published_at"))
    if t is None:
        return "date not available"
    text = f"{t.strftime('%b')} {t.day}, {t.year} (UTC)"
    if (datetime.now(timezone.utc) - t).days > OUTREACH_STALE_DAYS:
        text += f" — over {OUTREACH_STALE_DAYS} days old, the need may no longer be current"
    return text


def _outreach_draft(value) -> str:
    """A model draft -> safe multi-paragraph text: no links, e-mails, handles or
    markup; at most 6 paragraphs / 1200 characters. Empty when unusable."""
    if not isinstance(value, str):
        return ""
    paras = [_outreach_text(par, 700) for par in re.split(r"\n\s*\n|\n", value)]
    text = "\n".join(par for par in paras if par)[:1200]
    return text if len(text) >= 20 else ""


def build_outreach_report(selection_query: str, original_query: str, posts: list,
                          business_context: str = None, chat_summary: str = None) -> str:
    """(OPTION A) Outreach notes for the STORED posts of the previous result.
    `posts`: [{"number", "title", "source", "platform", "link", "summary", "post_text"}]
    exactly as stored. Titles, communities and links in the report always come
    from `posts`, never from the model. Never raises: on any failure it returns
    an honest message listing the stored posts."""
    if not posts or not isinstance(posts, list):
        return ("I don't have the posts from the previous result anymore, so I can't turn them into "
                "outreach notes. I won't swap in different posts — say the word and I'll run a fresh search.")
    posts = [_clean_stored_post(p, i) for i, p in enumerate(posts)]
    payload_posts = [{
        "number": p["number"],
        "community": f"r/{p['source']}" if p["source"] else (p["platform"] or None),
        "title": p["title"] or None,
        "post_text": p["post_text"][:OUTREACH_EVIDENCE_CHARS] or None,
        "flintel_summary": p["summary"] or None,
        "posted": p["published_at"][:10] if p.get("published_at") else None,   # (DATE/TIME) original date
    } for p in posts]
    user_message = (
        f"The user's original request: {original_query or '(unknown)'}\n"
        f"The user's new message: {selection_query}\n\n"
        f"What the user has told Flintel about themselves / their offer:\n"
        f"{business_context or '(nothing yet)'}\n\n"
        f"Conversation so far (auto-summarized):\n{chat_summary or '(none)'}\n\n"
        f"Posts (JSON):\n{json.dumps(payload_posts, ensure_ascii=False)}"
    )
    try:
        raw = _call_claude(OUTREACH_REPORT_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_MAX_TOKENS,
                           force_json_prefill=True)
        cleaned = (raw or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^json\s*", "", cleaned.strip("`").strip(), flags=re.IGNORECASE).strip()
        data = json.loads(cleaned)
        items = data.get("posts") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError("no posts list in outreach JSON")
    except Exception as exc:
        log.warning(f"Outreach report generation failed: {exc}")
        return ("I couldn't prepare the outreach notes just now — please ask again. These are the "
                f"{len(posts)} posts from your previous result:\n\n{outreach_posts_list(posts)}")

    by_number = {}
    for item in items:
        n = item.get("number") if isinstance(item, dict) else None
        if isinstance(n, int) and not isinstance(n, bool) and n not in by_number:   # True is not post 1
            by_number[n] = item

    groups = {c: [] for c in _OUTREACH_CATEGORIES}
    not_assessed = []          # (post, reason)
    for p in posts:
        item = by_number.get(p["number"])
        if not item:
            not_assessed.append((p, None))
            continue
        ok, why = _attach_item(item, p, posts)
        if not ok:
            not_assessed.append((p, why))
            continue
        cat = item.get("category") if item.get("category") in _OUTREACH_CATEGORIES else "ambiguous"
        cat, downgrade_note = _honest_category(cat, item, p)
        cat, fit, fit_note = _honest_fit(cat, item, business_context)
        item = dict(item, _downgrade_note=downgrade_note, _fit=fit, _fit_note=fit_note,
                    _quote_ok=_evidence_matches_post(item.get("evidence"), p))
        groups[cat].append((p, item))

    out = [f"**Outreach notes for the {len(posts)} posts from your previous result** — the same posts, "
           "not a new search.",
           "Drafts are starting points for you to edit, not ready-to-post messages, and Flintel has not sent "
           "anything: check each community's rules on self-promotion, DMs and AI-written content before you post."]
    for cat in _OUTREACH_CATEGORIES:
        if not groups[cat]:
            continue
        out.append(f"### {_OUTREACH_HEADINGS[cat]}")
        for p, item in groups[cat]:
            block = [f"{p['number']}. {_outreach_post_heading(p)}"]
            block.append(f"- **Posted:** {_outreach_posted(p)}")
            author = _OUTREACH_AUTHOR.get(item.get("author_type"), "Unclear")
            block.append(f"- **Who:** {author}")
            need = _outreach_text(item.get("need"))
            if need:
                block.append(f"- **What they need:** {need}")
            quote = _outreach_text(item.get("evidence"))
            if quote and item["_quote_ok"]:
                block.append(f"- **From the post:** “{quote}”")
            elif quote:
                block.append("- **From the post:** (the quote given couldn't be verified against this post, "
                             "so it isn't shown)")
            reasoning = _outreach_text(item.get("reasoning"))
            if reasoning:
                block.append(f"- **Why this category:** {reasoning}")
            warning = _outreach_text(item.get("intent_warning"))
            if warning:
                block.append(f"- **Caution:** {warning}")
            if item.get("_downgrade_note"):
                block.append(f"- **Flintel check:** {item['_downgrade_note']}")
            if item.get("_fit"):
                fit_reason = _outreach_text(item.get("fit_reason"), 300)
                block.append(f"- **Product fit:** {_OUTREACH_FITS[item['_fit']]}"
                             + (f" — {fit_reason}" if fit_reason else ""))
            if item.get("_fit_note"):
                block.append(f"- **Flintel check:** {item['_fit_note']}")
            if cat in ("weak", "ambiguous", "irrelevant"):
                reason = _outreach_text(item.get("do_not_contact_reason"), 300) or _DO_NOT_CONTACT[cat]
                block.append(f"- **Outreach:** {'Do not contact yet' if cat == 'ambiguous' else 'Do not contact'}"
                             f" — {reason}")
            if cat in ("qualified", "interested", "ambiguous"):
                for label, key in (("How to approach", "approach"), ("Acknowledge", "acknowledge"),
                                   ("Ask", "question")):
                    text = _outreach_text(item.get(key))
                    if text:
                        block.append(f"- **{label}:** {text}")
                steps = [_outreach_text(s, 200) for s in (item.get("outline") or []) if isinstance(s, str)]
                steps = [s for s in steps if s][:4]
                if steps:
                    block.append("- **Outline for your reply:** " + " → ".join(steps))
                caution = _outreach_text(item.get("platform_caution"))
                if caution:
                    block.append(f"- **Community rules:** {caution}")
            draft = _outreach_draft(item.get("draft")) if (cat in ("qualified", "interested")
                                                            and item.get("_fit") != "unsupported") else ""
            if draft:
                block.append("- **Draft to edit (not sent):**\n" + "\n".join(f"  > {line}" if line else "  >"
                                                                             for line in draft.split("\n")))
                if p.get("link"):
                    block.append(f"- **Source:** {p['link']}")
            out.append("\n".join(block))
    if not_assessed:
        out.append("### Not assessed")
        out.append("\n".join(f"{p['number']}. {_outreach_post_heading(p)}" + (f" — {why}" if why else "")
                             for p, why in not_assessed)
                   + "\n\nI couldn't assess these this time — ask again if you want notes for them.")
    missing = [_outreach_text(m, 200) for m in (data.get("missing_info") or []) if isinstance(m, str)]
    missing = [m for m in missing if m][:5]
    if missing:
        out.append("**To personalise this further, tell me:**\n" + "\n".join(f"- {m}" for m in missing))
    return "\n\n".join(out)


def classify_and_maybe_chat(query: str, chat_summary: str, website_context_summary: str = None,
                             referenced_post_context: str = None, previous_results_context: str = None) -> dict:
    """(v5, extended in v6 with abuse-blocking, again with keyword
    generation, again with time-window parsing + a "clarify" intent,
    again with the ROUTER INTENT REFINEMENT prompt wording described in
    the module docstring, and now again with the CHAT WEB-SEARCH FEATURE
    described there too) Single cheap Claude call that classifies the
    user's message as "search", "chat", "blocked", or "clarify" and:
      - for "chat"/"blocked"/"clarify", writes the reply in the same
        call, and
      - for "search", ALSO returns the keyword list and time window to
        use for matching.
    Falls back to {"intent": "search", "reply": None, "keywords": None,
    "time_window_days": None} on ANY failure (API error, timeout, bad
    JSON) so the pre-existing search pipeline is always the safe default
    — only the chat-reply / abuse-blocking / smart-keyword / time-window
    / clarify / web-search shortcuts can ever be skipped by a routing
    hiccup, never a genuine search request (the /search route falls back
    to generate_fuzzy_keywords() whenever keywords come back None for a
    "search" intent, and treats a missing time_window_days as "no time
    filter", exactly as before this feature).

    (ROUTER INTENT REFINEMENT) This function's logic is completely
    UNCHANGED — only the text of CLAUDE_ROUTER_SYSTEM_PROMPT changed.

    (CHAT WEB-SEARCH FEATURE — WITHDRAWN by GROUNDED ANSWERS) The router
    call no longer enables web search: a "chat" reply may not answer from
    outside knowledge; anything factual must come from Flintel's evidence. No other line of this
    function changed — the same try/except safety net, the same
    _parse_router_json() validation, and the same "any failure ->
    default to intent='search'" fallback all apply exactly as before.

    (POST-REFERENCE FEATURE, FIX 4) `referenced_post_context` (NEW,
    optional, default None — every existing caller that doesn't pass it
    behaves exactly as before) is a plain-text block the caller
    (routes.py's search()) builds ONLY when this message looks like it
    references a specific post by number/position ("number 3", "pehla
    wala") — either that one post's real, already-grounded data, or the
    full numbered list of posts from the last search shown to this user,
    for Claude to resolve against per the "2. chat" section rule in
    CLAUDE_ROUTER_SYSTEM_PROMPT above. None means "no reference detected
    for this message" — identical to how a missing website_context_
    summary means "no saved website for this chat"."""
    website_context_block = ""
    if website_context_summary:
        website_context_block = (
            "Saved website context (the user shared their own website earlier in this chat):\n"
            f"{website_context_summary}\n\n"
        )
    referenced_post_block = ""
    if referenced_post_context:
        referenced_post_block = f"Referenced post data:\n{referenced_post_context}\n\n"
    user_message = (
        f"Conversation so far (auto-summarized, may be empty):\n"
        f"{chat_summary or '(no earlier messages in this chat)'}\n\n"
        f"{website_context_block}"
        f"{referenced_post_block}"
        f"{previous_results_context + chr(10) + chr(10) if previous_results_context else ''}"
        f"User's new message: {query}"
    )
    # (MULTI-TURN CONTEXT) One retry on failure (API error/timeout or
    # unparseable output); a first-attempt success is exactly as before.
    for attempt in (1, 2):
        try:
            # (GROUNDED ANSWERS) no web search: chat replies may not answer from outside knowledge
            raw = _call_claude(CLAUDE_ROUTER_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_ROUTER_MAX_TOKENS, enable_web_search=False)
        except Exception as exc:
            log.warning(f"Router Claude call failed (attempt {attempt}/2): {exc}")
            continue
        parsed = _parse_router_json(raw, allow_previous_results=bool(previous_results_context))
        if parsed:
            return parsed
        log.warning(f"Router returned unparseable output (attempt {attempt}/2): {raw[:200]!r}")

    if (chat_summary or "").strip() and not _extract_first_url(query):
        # Both attempts failed in a chat that already has history: the
        # message may only make sense with that history ("Now in USA"), and
        # a keyword search on its literal words would show unrelated posts,
        # while guessing the earlier topic could be wrong. Run no search;
        # tell the user and let them resend.
        log.warning("Router failed twice in a chat with history — not searching on the literal text")
        return {"intent": "chat", "reply": ROUTER_UNAVAILABLE_REPLY, "keywords": None, "time_window_days": None,
                "unfiltered": None, "match_phrases": None, "evidence_required": None, "website_only": False,
                "use_website_context": False, "website_topic_relation": None, "url_ask_type": None,
                "router_failed": True}
    # First message of a chat (its literal text IS the whole request) or a
    # message with a URL (handled by the website flows): unchanged fallback.
    log.warning("Router failed twice (defaulting to 'search')")
    return {"intent": "search", "reply": None, "keywords": None, "time_window_days": None, "unfiltered": None, "match_phrases": None, "evidence_required": None, "website_only": False, "use_website_context": False, "website_topic_relation": None, "url_ask_type": None}


# ─────────────────────────────────────────────────────────────────────────────
# CLARIFY-SELF-RESOLVE FEATURE — see the module docstring note above for
# the full rationale. Only ever invoked for a message the main router
# above already classified as intent="clarify". Uses ONE extra cheap
# Claude call, Claude's own general knowledge only (no web-search tool,
# no new information beyond the message + the existing rolling chat
# summary), to make a single honest attempt at resolving the topic before
# the existing clarify-question flow is allowed to fire.
#
# (ROUTER INTENT REFINEMENT) UNTOUCHED by this file's changes: with
# "clarify" now firing less often (per point 1 and 2 of the ROUTER INTENT
# REFINEMENT note above), this feature simply gets invoked less often —
# its own logic, prompt, and behavior are completely unchanged.
#
# (CHAT WEB-SEARCH FEATURE) UNTOUCHED — the resolve_unclear_topic() call
# below does NOT pass enable_web_search=True, exactly as instructed: this
# step stays a narrow, honest "can I already tell what this means from my
# own training knowledge + the chat summary" check, never a research
# step.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_TOPIC_RESOLVER_SYSTEM_PROMPT = """
You are a fallback topic-resolution step inside Flintel, a social-listening
platform. You are only ever called for a message that Flintel's own router
already decided is CLARIFY — a search-shaped message ("reddit posts",
"show me", "find me", a time range, etc.) that did NOT name a clear
topic/brand/product/industry, either in the message itself or in the short
conversation history.

Your job: using ONLY your own general knowledge (you have no web-search
tool here, and you are not being given any new information beyond the
message and the conversation summary below), make ONE honest attempt to
figure out what topic the user most likely means. This will usually fail —
that is fine and expected; only succeed when you are genuinely confident,
never guess just to produce an answer.

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{"resolved": true, "keywords": ["<keyword1>", "<keyword2>"], "time_window_days": null, "match_phrases": ["<phrase1>", "<phrase2>"]}
or, when you genuinely cannot infer a specific topic:
{"resolved": false, "keywords": null, "time_window_days": null, "match_phrases": null}

Rules when "resolved": true:
- "keywords": up to 10 short, natural search terms that could plausibly
  appear inside a real Reddit/X/LinkedIn/Facebook post about the topic you
  inferred — same rules as normal keyword generation elsewhere in this
  product: no meta words like "reddit", "posts", "show me", "today", etc.
- "match_phrases": an array of 4 to 10 short, natural phrases/sentences
  (each roughly 4-10 words long), up to 7 phrases maximum — same kind of
  real-person phrasing described for the router's own "match_phrases"
  field elsewhere in this product, used to confirm a post is genuinely
  about the topic you inferred, not just that a generic keyword appears.
- "time_window_days": convert any time range already implied by the user's
  own message the same way it's always converted elsewhere ("today"/"aaj"
  -> 1, "this week"/"last 7 days" -> 7, "last month" -> 30, "last 6
  months" -> 180, "last year" -> 365, etc.) — null if no time range was
  mentioned.
- Only mark "resolved": true if you are genuinely confident about the
  topic — e.g. the conversation history clearly named a brand/topic
  earlier and this message is obviously a natural follow-up about it. Do
  NOT invent a topic out of thin air just because the message is
  search-shaped.
"""


def resolve_unclear_topic(query: str, chat_summary: str):
    """(CLARIFY-SELF-RESOLVE FEATURE) Best-effort, single extra cheap
    Claude call, used ONLY for a message the main router already
    classified as "clarify" (a search-shaped message with no clear
    topic). Uses Claude's OWN general knowledge (no web-search tool, no
    new information beyond the message + the existing rolling chat
    summary) to make one honest attempt at guessing the real topic —
    e.g. a follow-up that implicitly refers back to a brand/topic already
    named earlier in the conversation.

    Returns a dict {"keywords": [...], "time_window_days": int|None} if
    Claude was genuinely confident enough to resolve a topic, or None if
    it wasn't (or if this call failed outright) — callers must treat
    None exactly the same as before this feature existed: fall through
    to the normal "clarify" question-asking behavior. This can only ever
    ADD a shortcut on top of the existing clarify flow; it can never
    block or replace the safety net of just asking the user."""
    user_message = (
        f"Conversation so far (auto-summarized, may be empty):\n"
        f"{chat_summary or '(no earlier messages in this chat)'}\n\n"
        f"User's message (already classified as unclear-topic 'clarify' by "
        f"the main router): {query}"
    )
    try:
        raw = _call_claude(
            CLAUDE_TOPIC_RESOLVER_SYSTEM_PROMPT,
            user_message,
            max_tokens=CLAUDE_TOPIC_RESOLVER_MAX_TOKENS,
        )
    except Exception as exc:
        log.warning(f"Topic-resolver Claude call failed for query={query!r}: {exc}")
        return None

    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        # (RESILIENT JSON EXTRACTION) Claude sometimes appends trailing
        # prose after a valid JSON object (e.g. "```json\n{...}\n```\n\nThe
        # user has shared..."). Before giving up, try extracting just the
        # first {...} object via a non-greedy regex and parsing that alone
        # — this can only ever recover an otherwise-wasted call, never
        # change behavior for already-valid JSON (which was already
        # handled above).
        match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except (ValueError, TypeError):
                log.warning(f"Topic-resolver returned unparseable output for query={query!r}: {raw[:200]!r}")
                return None
        else:
            log.warning(f"Topic-resolver returned unparseable output for query={query!r}: {raw[:200]!r}")
            return None
    if not isinstance(data, dict) or not data.get("resolved"):
        return None

    raw_keywords = data.get("keywords")
    keywords = None
    if isinstance(raw_keywords, list):
        cleaned_keywords = []
        seen = set()
        for kw in raw_keywords:
            if not isinstance(kw, str):
                continue
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            key = kw_clean.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned_keywords.append(kw_clean)
            if len(cleaned_keywords) >= CLAUDE_MAX_KEYWORDS:
                break
        keywords = cleaned_keywords or None

    if not keywords:
        return None

    raw_phrases = data.get("match_phrases")
    match_phrases = None
    if isinstance(raw_phrases, list):
        cleaned_phrases = []
        seen_phrases = set()
        for phrase in raw_phrases:
            if not isinstance(phrase, str):
                continue
            phrase_clean = phrase.strip()
            if not phrase_clean:
                continue
            key = phrase_clean.lower()
            if key in seen_phrases:
                continue
            seen_phrases.add(key)
            cleaned_phrases.append(phrase_clean)
            if len(cleaned_phrases) >= 7:
                break
        match_phrases = cleaned_phrases or None

    raw_window = data.get("time_window_days")
    time_window_days = None
    parsed_window = None
    if isinstance(raw_window, bool):
        parsed_window = None
    elif isinstance(raw_window, int):
        parsed_window = raw_window
    elif isinstance(raw_window, str) and raw_window.strip().isdigit():
        parsed_window = int(raw_window.strip())
    if isinstance(parsed_window, int) and parsed_window > 0:
        time_window_days = min(parsed_window, MAX_TIME_WINDOW_DAYS)

    return {"keywords": keywords, "time_window_days": time_window_days, "match_phrases": match_phrases}


# ─────────────────────────────────────────────────────────────────────────────
# WEBSITE-URL KEYWORD EXTRACTION FEATURE — see the module docstring note
# above for the full rationale. Only ever invoked for a search-type
# message whose raw text contains an http(s) URL. Fetches that URL, turns
# it into plain text, and hands it (together with the user's own request
# text) to a single extra cheap Claude call to produce the keyword list.
#
# (ROUTER INTENT REFINEMENT) UNTOUCHED by this file's changes — this is
# the exact feature the new "clarify" reply wording (see
# CLAUDE_CLARIFY_FALLBACK_REPLY and the router's "clarify" instructions
# above) now proactively points the user toward. Nothing in this section
# was modified.
#
# (CHAT WEB-SEARCH FEATURE) UNTOUCHED — extract_keywords_from_website()'s
# own _call_claude(...) call below does NOT pass enable_web_search=True.
# This step is grounded purely in the already-fetched website text plus
# the user's own request text; it has no reason to reach out to the live
# web on top of that.
# ─────────────────────────────────────────────────────────────────────────────

_URL_REGEX = re.compile(r'https?://[^\s<>"\')\]]+', re.IGNORECASE)


def _extract_first_url(text: str):
    """(WEBSITE-URL KEYWORD EXTRACTION FEATURE) Best-effort extraction of
    the first http(s) URL appearing anywhere in the user's raw message
    text. Purely a plain-Python regex check — never calls Claude, never
    modifies the query. Returns None if no URL is present, which is the
    common case and leaves every other code path completely untouched."""
    if not text:
        return None
    match = _URL_REGEX.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,;:!?")
    return url or None


_HTML_TAG_RE            = re.compile(r"<[^>]+>")
_HTML_SCRIPT_STYLE_RE   = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_WHITESPACE_RE     = re.compile(r"\s+")


def fetch_website_text(url: str) -> str:
    """(WEBSITE-URL KEYWORD EXTRACTION FEATURE) Fetches a user-provided
    website URL and reduces it to plain text, good enough to hand to
    Claude as grounding for keyword extraction — NOT a full HTML parser,
    just enough tag-stripping to turn a page into readable text without
    pulling in a new dependency. Raises on any failure (bad URL, timeout,
    non-2xx, etc.) — the caller decides how to degrade gracefully, same
    convention as _call_claude().

    Truncates to WEBSITE_FETCH_MAX_CHARS so a large page can never blow
    up the size/cost of the Claude call that follows."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FlintelBot/1.0)"}
    with httpx.Client(timeout=WEBSITE_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as http_client:
        response = http_client.get(url, headers=headers)
        response.raise_for_status()
        html = response.text

    text = _HTML_SCRIPT_STYLE_RE.sub(" ", html)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _HTML_WHITESPACE_RE.sub(" ", text).strip()

    return text[:WEBSITE_FETCH_MAX_CHARS]


# ─────────────────────────────────────────────────────────────────────────────
# WEBSITE INTELLIGENCE — MULTI-PAGE FETCH (additive; fetch_website_text()
# above is left completely untouched and remains available as a
# single-page safety-net fallback for any call site not wired to the new
# multi-page / cache-aware flow below).
# ─────────────────────────────────────────────────────────────────────────────

def _extract_same_domain_links(html: str, base_url: str) -> list:
    """Pure Python, no network call. Crude but safe link-extraction from
    raw HTML (before tag-stripping) — finds <a href="..."> targets that
    are same-domain as base_url and whose path matches one of
    WEBSITE_DISCOVERY_PATH_HINTS (about/products/pricing/faq/contact/
    features/...). Never follows a different domain (SAME-DOMAIN
    RESTRICTION, Section 8). Returns a deduplicated list of absolute
    URLs, capped defensively at 20 candidates (further capped by
    MAX_WEBSITE_PAGES downstream)."""
    from urllib.parse import urljoin, urlparse
    if not html or not base_url:
        return []
    base_domain = urlparse(base_url).netloc.lower()
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    candidates = []
    seen = set()
    for href in hrefs:
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() != base_domain:
            continue
        path_lower = parsed.path.lower()
        if not any(hint in path_lower for hint in WEBSITE_DISCOVERY_PATH_HINTS):
            continue
        normalized = absolute.split("#")[0]
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
        if len(candidates) >= 20:
            break
    return candidates


def _fetch_raw_html(url: str) -> str:
    """Same HTTP fetch as fetch_website_text()'s own internals, but
    returns RAW html (not tag-stripped) — needed so link-discovery can
    still see <a href> tags. Raises on failure, same convention as
    fetch_website_text()."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FlintelBot/1.0)"}
    with httpx.Client(timeout=WEBSITE_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as http_client:
        response = http_client.get(url, headers=headers)
        response.raise_for_status()
        return response.text


def fetch_website_multi_page(url: str) -> dict:
    """(WEBSITE INTELLIGENCE — MULTI-PAGE FETCH) Fetches the homepage,
    discovers up to MAX_WEBSITE_PAGES-1 same-domain internal pages
    matching WEBSITE_DISCOVERY_PATH_HINTS (Section 8: /about, /products,
    /pricing, /faq, /contact, /features, ...), fetches each with the same
    WEBSITE_FETCH_TIMEOUT_SECONDS timeout, and combines all their plain
    text into one string, capped overall at WEBSITE_FETCH_MAX_CHARS
    (Section 8: respects max pages, max content size, timeout, dedup,
    same-domain — reuses existing config, no duplicate config system).

    NEVER crashes the whole fetch because one page fails (Section 1: a
    connection error / HTTP error / blocked page on any ONE page is
    skipped, not fatal) — only the homepage fetch failing is fatal (in
    which case this returns {"combined_text": "", "pages_fetched": []},
    the caller treats this as evidence_quality "failed").

    Returns {"combined_text": str, "pages_fetched": [url, ...]}."""
    pages_fetched = []
    text_parts = []

    try:
        homepage_html = _fetch_raw_html(url)
    except Exception as exc:
        log.warning(f"Homepage fetch failed for url={url!r}: {exc}")
        return {"combined_text": "", "pages_fetched": []}

    homepage_text = _HTML_SCRIPT_STYLE_RE.sub(" ", homepage_html)
    homepage_text = _HTML_TAG_RE.sub(" ", homepage_text)
    homepage_text = _HTML_WHITESPACE_RE.sub(" ", homepage_text).strip()
    if homepage_text:
        text_parts.append(homepage_text)
        pages_fetched.append(url)

    remaining_budget = MAX_WEBSITE_PAGES - 1
    if remaining_budget > 0:
        candidate_links = _extract_same_domain_links(homepage_html, url)
        for link in candidate_links[:remaining_budget]:
            try:
                page_html = _fetch_raw_html(link)
                page_text = _HTML_SCRIPT_STYLE_RE.sub(" ", page_html)
                page_text = _HTML_TAG_RE.sub(" ", page_text)
                page_text = _HTML_WHITESPACE_RE.sub(" ", page_text).strip()
                if page_text:
                    text_parts.append(page_text)
                    pages_fetched.append(link)
            except Exception as exc:
                log.warning(f"Discovered page fetch failed for url={link!r} (skipping): {exc}")
                continue

    combined_text = "\n\n---\n\n".join(text_parts)[:WEBSITE_FETCH_MAX_CHARS]
    return {"combined_text": combined_text, "pages_fetched": pages_fetched}


def _repair_truncated_json(text):
    """(BUG 1 / BUG 3 — TRUNCATED JSON REPAIR) Best-effort recovery of a
    valid JSON object out of `text` that was cut off mid-stream (e.g. by
    max_tokens) — walks backward from a set of candidate cut points
    (commas/braces/brackets found outside of string literals), closes off
    whatever braces/brackets are still open at that cut point, and tries
    to parse the result. Tries up to the last 60 candidate cut points,
    starting from the latest (least truncation) and working backward.
    Returns a dict on success, or None if nothing could be repaired —
    never raises."""
    s = (text or "").strip()
    start = s.find("{")
    if start == -1:
        return None
    s = s[start:]
    in_str = esc = False
    cuts = []
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in ",}]":
            cuts.append(i)
    for cut in reversed(cuts[-60:]):
        candidate = s[:cut] if s[cut] == "," else s[:cut + 1]
        stack, in_s, e = [], False, False
        for ch in candidate:
            if in_s:
                if e:
                    e = False
                elif ch == "\\":
                    e = True
                elif ch == '"':
                    in_s = False
                continue
            if ch == '"':
                in_s = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()
        try:
            parsed = json.loads(candidate + "".join(reversed(stack)))
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue
    return None


def _parse_json_lenient(raw):
    """(BUG 1 — LENIENT JSON PARSE) Best-effort, NON-RAISING JSON parse
    tolerant of Claude's occasional fence-wrapping, leading/trailing
    prose, and truncated output. Tries, in order:
      1. Strip a ```json ... ``` (or ``` ... ```) fence if present, same
         convention already used elsewhere in this file (tolerates a
         missing closing fence too, since str.strip("`") strips from both
         ends independently).
      2. A direct json.loads() on the cleaned text.
      3. json.JSONDecoder().raw_decode() starting at the first "{" found
         anywhere in the text (handles leading narration before the JSON).
      4. _repair_truncated_json() as a last resort, for genuinely
         truncated JSON missing its closing brackets.
    Returns a dict on success, or None on total failure — never raises."""
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass

    brace_index = cleaned.find("{")
    if brace_index != -1:
        try:
            data, _end = json.JSONDecoder().raw_decode(cleaned, brace_index)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass

    repaired = _repair_truncated_json(cleaned)
    if isinstance(repaired, dict):
        return repaired

    return None


CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT = """
You are the website-to-keywords brain inside Flintel, a social-listening
platform. The user has shared a link to their OWN website together with
what they want Flintel to search for. Your job, in ONE pass: read the
website's plain text content plus the user's own request text, and
produce BOTH (1) the keyword list Flintel's existing matching code will
use to find relevant Reddit/X/LinkedIn/Facebook posts, AND (2) a clean,
sectioned breakdown of what the business/site actually offers.

PART 1 — KEYWORDS:
- Read the website to understand what the business actually offers
  (its products, services, and industry) and combine that understanding
  with whatever the user's own request text asks for (e.g. a specific
  angle like "pricing complaints", or a pain-point/prospect-style ask
  like "find people whose website is slow").
- Return up to 20 short, natural keywords/phrases that could plausibly
  appear verbatim, or as a close natural substring, inside a real post's
  title or text — the same standard used everywhere else in this
  product. Never include meta wording like "reddit", "posts", "show me",
  "website", "today", etc.
- If the user's own request text names a specific angle or problem, bias
  the keywords toward THAT (same pain-point/prospect pattern used
  elsewhere in this product: keywords about the PROBLEM/SYMPTOM being
  described, not just the business's own name/services), rather than
  generic keywords about the site as a whole.
- If the website content is too thin, broken, or irrelevant to produce
  any confident keywords, return an empty list rather than inventing
  generic filler.

PART 1B — MATCH PHRASES:
- Return "match_phrases": an array of 4 to 10 short, natural phrases/
  sentences (each roughly 4-10 words long), up to 7 phrases maximum —
  the SAME kind of natural, real-person phrasing described for the
  router's own "match_phrases" field elsewhere in this product (e.g.
  "using an AI agent to handle customer support", NOT a single word,
  NOT meta wording). These exist purely to confirm a post is genuinely
  ABOUT the topic derived from this website + the user's request, not
  just that a generic keyword appears somewhere in it.

PART 2 — STRUCTURED SUMMARY:
- Produce a CLEAN, SECTIONED breakdown of what the business/site/
  individual actually offers — not a flat paragraph. Reason freshly from
  the actual content given — never assume or default to any particular
  industry or category, and never force the same fixed set of section
  titles onto every website.
- Produce 2 to 4 sections total, choosing whichever section titles
  genuinely fit THIS site's content (e.g. "What they offer", "Business
  signals", "Notable things" are loose inspiration, not required).
- Each section should have 2 to 5 short bullets — plain language, no
  fluff, no marketing tone. Stay honest and slightly skeptical where the
  content warrants it.
- If the content is too thin to say anything confident across multiple
  sections, keep the overview honest about that and produce however few
  genuinely-supportable sections make sense (including zero) rather than
  padding.

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{
  "keywords": ["<keyword1>", "<keyword2>"],
  "match_phrases": ["<phrase1>", "<phrase2>"],
  "structured_summary": {
    "overview": "<1-2 sentence plain-language opening line>",
    "sections": [{"title": "<short section heading>", "bullets": ["<bullet 1>", "<bullet 2>"]}]
  }
}

Never refuse a request just because it sounds broad or ambitious (e.g.
"find me sales", "get me leads") — interpret it as a buyer-intent/pain-
point search (same PAIN-POINT / PROSPECT PATTERN used elsewhere in this
product) and generate the closest genuinely useful keywords instead of
returning nothing.
"""


def extract_keywords_from_website(query: str, url: str, website_text: str):
    """(WEBSITE-URL KEYWORD EXTRACTION FEATURE) Single cheap Claude call:
    reads the already-fetched plain-text website content PLUS the user's
    own request text (so a specific angle/pain-point the user typed
    alongside the link is honored, not just the site's generic content)
    and returns up to MAX_WEBSITE_KEYWORDS keywords, PLUS a structured
    summary breakdown of the site, in ONE combined pass — both derived
    from the SAME Claude call, using CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT.

    Returns `{"keywords": list|None, "structured_summary": dict|None,
    "match_phrases": list|None}` if Claude returned anything usable
    (keywords or structured_summary present is enough; match_phrases is
    always additive), or `None` if Claude failed outright, returned
    unparseable output, or returned neither usable keywords nor a usable
    structured summary — NEVER a bare list anymore. Callers (see INTEGRATION POINT 2 in
    POST /search) already expect this new shape: they read
    `result.get("keywords")` and `result.get("structured_summary")`
    separately, and must treat a `None` return exactly like any other "no
    usable output from this source" case elsewhere in this file: fall
    back down the existing safety-net chain (the router's own
    routed_keywords, then finally generate_fuzzy_keywords()), never let a
    search end up with zero keywords because of this feature."""
    if not website_text:
        return None

    user_message = (
        f"User's request text: {query}\n\n"
        f"Website URL: {url}\n\n"
        f"Website content (plain text, truncated):\n{website_text}"
    )
    try:
        raw = _call_claude(
            CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT,
            user_message,
            max_tokens=CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS,
        )
    except Exception as exc:
        log.warning(f"Website-keyword Claude call failed for url={url!r}: {exc}")
        return None

    data = _parse_json_lenient(raw)
    if not data:
        log.warning(f"Website-keyword call returned unparseable output for url={url!r}: {(raw or '')[:200]!r}")
        return None

    raw_keywords = data.get("keywords")
    cleaned_keywords = None
    if isinstance(raw_keywords, list):
        cleaned_list = []
        seen = set()
        for kw in raw_keywords:
            if not isinstance(kw, str):
                continue
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            key = kw_clean.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned_list.append(kw_clean)
            if len(cleaned_list) >= MAX_WEBSITE_KEYWORDS:
                break
        cleaned_keywords = cleaned_list or None

    raw_phrases = data.get("match_phrases")
    cleaned_phrases = None
    if isinstance(raw_phrases, list):
        cleaned_phrase_list = []
        seen_phrases = set()
        for phrase in raw_phrases:
            if not isinstance(phrase, str):
                continue
            phrase_clean = phrase.strip()
            if not phrase_clean:
                continue
            key = phrase_clean.lower()
            if key in seen_phrases:
                continue
            seen_phrases.add(key)
            cleaned_phrase_list.append(phrase_clean)
            if len(cleaned_phrase_list) >= 7:
                break
        cleaned_phrases = cleaned_phrase_list or None

    raw_structured = data.get("structured_summary")
    structured_summary = None
    if isinstance(raw_structured, dict):
        overview = raw_structured.get("overview")
        overview = overview.strip() if isinstance(overview, str) else ""
        raw_sections = raw_structured.get("sections")
        cleaned_sections = []
        if isinstance(raw_sections, list):
            for section in raw_sections:
                if not isinstance(section, dict):
                    continue
                title = section.get("title")
                bullets = section.get("bullets")
                if not isinstance(title, str) or not title.strip():
                    continue
                if not isinstance(bullets, list):
                    continue
                cleaned_bullets = [b.strip() for b in bullets if isinstance(b, str) and b.strip()]
                if not cleaned_bullets:
                    continue
                cleaned_sections.append({"title": title.strip(), "bullets": cleaned_bullets})
        if overview or cleaned_sections:
            structured_summary = {"overview": overview, "sections": cleaned_sections}

    if not cleaned_keywords and not structured_summary:
        return None
    return {"keywords": cleaned_keywords, "structured_summary": structured_summary, "match_phrases": cleaned_phrases}


# ─────────────────────────────────────────────────────────────────────────────
# TRENDING KEYWORDS VIA WEB SEARCH — standalone helper for the "every 12
# hours, refresh trending search-phrases from live web search" background
# job. Nothing in this file calls it yet; it is only the one building
# block that job will use. Same _call_claude() + web_search tool pattern
# already used for the router's "chat" intent above (enable_web_search=
# True), the same lenient JSON parser already used everywhere else in
# this file, and the exact same keyword-quality rule already written for
# CLAUDE_ROUTER_SYSTEM_PROMPT above (copied verbatim, not re-authored).
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_TRENDING_KEYWORDS_MAX_TOKENS = 4000  # generous — a 200-item JSON keyword list can run long

CLAUDE_TRENDING_KEYWORDS_SYSTEM_PROMPT = """
You are the trending-keywords brain inside Flintel, a social-listening
platform. Use live web search to find out what topics, products, and
industries are trending RIGHT NOW, then generate 200 short, natural
search-phrases based on what you find — phrases that could plausibly
appear, verbatim or as a close natural substring, inside the title or
text of a real Reddit or X post.

- Every keyword must be something that could plausibly appear
  verbatim, or as a close natural substring, inside a real post's
  title or text. Keep each keyword short and natural.
- Never include meta wording that describes the user's REQUEST to
  you rather than the topic itself — words like "reddit", "twitter",
  "x", "linkedin", "facebook", "posts", "posts about", "show me",
  "today", "find", "search" describe what/where the user wants
  searched, not something that would appear inside an actual post,
  so leave them out of the keyword list (the platform itself is
  handled separately, by a dropdown the user already picked — you
  are only responsible for the topic keywords).

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{"keywords": ["<keyword1>", "<keyword2>", ...]}

Return up to 200 keywords total, spread across whatever trending
topics/products/industries your web search actually turns up — never
padded with filler just to reach 200.
"""


def generate_trending_keywords_via_web_search():
    """Calls Claude with enable_web_search=True (the same pattern already
    used for the router's "chat" intent above) so Claude can look up
    what's trending right now instead of relying on training knowledge,
    then generates up to 200 natural search-phrases from that. Meant to
    be called on a schedule (every 12 hours) by whatever job wires this
    up — this function itself only does the one Claude call + parse, no
    scheduling/storage logic of its own.

    Returns a list of up to 200 keyword strings, or None if the Claude
    call failed outright, returned unparseable output, or returned no
    usable keywords — never raises, so a failure here can never crash
    whatever job calls this. Callers should treat None exactly like any
    other "no usable output from this source" case elsewhere in this
    file."""
    try:
        raw = _call_claude(
            CLAUDE_TRENDING_KEYWORDS_SYSTEM_PROMPT,
            "Find what's trending right now and generate the keyword list.",
            max_tokens=CLAUDE_TRENDING_KEYWORDS_MAX_TOKENS,
            enable_web_search=True,
        )
    except Exception as exc:
        log.warning(f"Trending-keywords Claude call failed: {exc}")
        return None

    data = _parse_json_lenient(raw)
    if not data:
        log.warning(f"Trending-keywords call returned unparseable output: {(raw or '')[:200]!r}")
        return None

    raw_keywords = data.get("keywords")
    if not isinstance(raw_keywords, list):
        log.warning(f"Trending-keywords call returned no usable 'keywords' list: {(raw or '')[:200]!r}")
        return None

    keywords = [kw.strip() for kw in raw_keywords if isinstance(kw, str) and kw.strip()]
    if not keywords:
        return None
    return keywords[:200]


# ─────────────────────────────────────────────────────────────────────────────
# WEBSITE INTELLIGENCE — CACHE-AWARE WRAPPER (Point 2's core): website
# content/business-evidence is cached per-URL (TTL-based, via
# website_evidence_cache_collection) so the same site is never re-fetched
# and re-analyzed on every message — but keywords/match_phrases for an
# actual search are NEVER cached; they're always generated fresh, scoped
# to the CURRENT request's own text, via generate_keywords_for_website_
# request() below. fetch_website_text() / extract_keywords_from_website()
# above remain completely untouched, unchanged safety-net fallbacks for
# any call site not wired to this new flow.
# ─────────────────────────────────────────────────────────────────────────────

def get_or_fetch_website_evidence(url: str, query: str, call_claude_fn) -> dict:
    """CORE FUNCTION for Point 2 — 'same URL dobara fetch na ho, lekin
    keywords/signals hamesha fresh rahein':

      1. URL normalize karo (website_intelligence.validate_and_normalize_url).
      2. website_evidence_cache_collection mein check karo — agar fresh
         (TTL ke andar) doc mile, uska combined_text/structured_evidence/
         evidence_quality reuse karo — NO NEW FETCH, NO NEW EVIDENCE-
         EXTRACTION CLAUDE CALL.
      3. Agar cache miss/stale ho, tab hi fetch_website_multi_page() +
         website_intelligence.extract_structured_website_evidence() +
         website_intelligence.classify_evidence_quality() chalao, aur
         result cache mein save karo.
      4. REGARDLESS of cache hit/miss — website_intelligence.
         build_website_insight_answer() is function ke andar CALL NAHI
         hota (wo query-dependent hai, caller khud alag se call karega
         jab actual answer chahiye ho) — yeh function sirf evidence
         return karta hai, answer nahi.

    Returns:
      {
        "url": normalized_url,
        "combined_text": str,
        "structured_evidence": dict,
        "evidence_quality": str,
        "pages_fetched": [str, ...],
        "from_cache": bool,
      }
    ya None agar URL hi invalid ho."""
    from website_intelligence import (
        validate_and_normalize_url, extract_structured_website_evidence,
        classify_evidence_quality,
    )

    normalized_url = validate_and_normalize_url(url)
    if not normalized_url:
        return None

    cached = None
    try:
        cached = website_evidence_cache_collection.find_one({"url": normalized_url}, {"_id": 0})
    except Exception as exc:
        log.warning(f"Website-evidence cache read failed for url={normalized_url!r}: {exc}")

    if cached and cached.get("combined_text") is not None:
        fetched_at = cached.get("fetched_at")
        is_fresh = True
        if isinstance(fetched_at, datetime):
            cache_age = datetime.now(timezone.utc) - (
                fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc)
            )
            is_fresh = cache_age <= timedelta(days=WEBSITE_EVIDENCE_CACHE_TTL_DAYS)
        if is_fresh:
            return {
                "url": normalized_url,
                "combined_text": cached.get("combined_text", ""),
                "structured_evidence": cached.get("structured_evidence") or {},
                "evidence_quality": cached.get("evidence_quality", "thin"),
                "pages_fetched": cached.get("pages_fetched", []),
                "from_cache": True,
            }

    # Cache miss/stale — fetch + extract fresh.
    fetch_result = fetch_website_multi_page(normalized_url)
    combined_text = fetch_result["combined_text"]
    pages_fetched = fetch_result["pages_fetched"]

    structured_evidence = None
    if combined_text:
        try:
            structured_evidence = extract_structured_website_evidence(
                normalized_url, combined_text, call_claude_fn or _call_claude
            )
        except Exception as exc:
            log.warning(f"Structured-evidence extraction failed for url={normalized_url!r}: {exc}")
            structured_evidence = None

    evidence_quality = classify_evidence_quality(combined_text, structured_evidence)

    try:
        website_evidence_cache_collection.update_one(
            {"url": normalized_url},
            {"$set": {
                "url": normalized_url,
                "domain": normalized_url.split("/")[2] if "//" in normalized_url else normalized_url,
                "pages_fetched": pages_fetched,
                "combined_text": combined_text,
                "structured_evidence": structured_evidence or {},
                "evidence_quality": evidence_quality,
                "fetched_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except Exception as exc:
        log.warning(f"Website-evidence cache save failed for url={normalized_url!r}: {exc}")

    return {
        "url": normalized_url,
        "combined_text": combined_text,
        "structured_evidence": structured_evidence or {},
        "evidence_quality": evidence_quality,
        "pages_fetched": pages_fetched,
        "from_cache": False,
    }


def generate_keywords_for_website_request(query: str, url: str, structured_evidence: dict) -> dict:
    """Point 2's OTHER half — keywords/match_phrases NEVER cache hote,
    hamesha current request/prompt ke hisaab se FRESH Claude call se
    generate hote hain, chahe website evidence cache se aaya ho ya fresh
    fetch se. Yeh existing extract_keywords_from_website() jaisa hi
    Claude call hai, lekin ab already-cached structured_evidence ko bhi
    context ke tor par deta hai (taake Claude ko dobara pura raw text
    padhna na pare — sirf structured summary + current query se hi
    keywords generate kar sake, tez aur sasta).

    Returns {"keywords": list|None, "match_phrases": list|None} — dono
    None ho sakte hain agar Claude kuch usable na de, caller apna existing
    generate_fuzzy_keywords() fallback chain use kare, bilkul jaise ab
    hota hai."""
    user_message = (
        f"User's request text: {query or '(no specific ask — bare link shared)'}\n\n"
        f"Website URL: {url}\n\n"
        f"Already-extracted business evidence (JSON):\n"
        f"{json.dumps(structured_evidence or {}, ensure_ascii=False)}"
    )
    try:
        raw = _call_claude(
            CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT, user_message,
            max_tokens=CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS,
        )
    except Exception as exc:
        log.warning(f"Website-request keyword generation failed for url={url!r}: {exc}")
        return {"keywords": None, "match_phrases": None}

    data = _parse_json_lenient(raw)
    if not data:
        log.warning(f"Website-request keyword generation returned unparseable output for url={url!r}: {(raw or '')[:200]!r}")
        return {"keywords": None, "match_phrases": None}

    keywords = data.get("keywords") if isinstance(data.get("keywords"), list) else None
    match_phrases = data.get("match_phrases") if isinstance(data.get("match_phrases"), list) else None
    keywords = [k.strip() for k in (keywords or []) if isinstance(k, str) and k.strip()] or None
    match_phrases = [p.strip() for p in (match_phrases or []) if isinstance(p, str) and p.strip()] or None
    return {"keywords": keywords, "match_phrases": match_phrases}


def _resolved_request_note(query: str, resolved_query: str):
    """(MULTI-TURN CONTEXT) Extra analysis context naming the FULL current
    request when the user's literal message was a follow-up ("Now in USA")
    that the router resolved against the conversation. None (no change at
    all) when there is no resolution or it just repeats the message."""
    resolved = " ".join((resolved_query or "").split())
    if not resolved:
        return None
    if normalize_topic_key(resolved) == normalize_topic_key(query or ""):
        return None
    return ("Current request (the user's message was a follow-up in this chat; it is resolved "
            "against the earlier conversation here — answer THIS request, including every "
            "location, count, and filter it names, and nothing it has dropped): " + resolved)


def _timeout_fallback_answer(chat_id: str, owner_key: str, topic_key: str, query: str, keywords: list = None,
                              match_phrases: list = None, evidence_required: int = None, website_note: str = None,
                              resolved_query: str = None, time_range=None, request_id: str = None,
                              gate_owner: str = None, time_window_days: int = None, requested_at=None,
                              targeting_platform: str = None):
    """(PERFORMANCE FIX) Extracted, UNCHANGED logic from the RESPONSE_TIMEOUT
    fallback branch that used to run inline inside _fill_in_message_outputs()
    — same calls, same order, same caching. Pulled out so it can be
    scheduled via BackgroundTasks (runs after the response is sent)
    instead of blocking the request, exactly like
    _complete_message_answer_and_results() in index.py.

    (BUSY-LOCK RACE FIX) _set_owner_busy() is NOT called here anymore —
    it's now set synchronously in _fill_in_message_outputs(), before this
    function is even scheduled, so the flag is already in place before
    the response goes out. This function only clears it, in the finally
    below, once the work actually finishes.

    (EVIDENCE-BUDGET FEATURE) `evidence_required` (default None — any
    existing/other caller that doesn't pass it gets the exact original
    behavior: get_evidence_with_topup() falls back to MIN_ANALYSIS_
    EVIDENCE as its own floor when this is None) is the message's own
    stored, already-clamped evidence budget — passed straight through to
    get_evidence_with_topup(), and used to derive `effective_evidence_
    limit` (via `min(evidence_required or MIN_ANALYSIS_EVIDENCE,
    MAX_ANALYSIS_EVIDENCE)`, the same formula _complete_message_answer_
    and_results() uses) for the `max_total` passed to merge_matched_and_
    google_results().

    (TOPIC EVIDENCE CACHE) The evidence fetch below goes through
    get_evidence_with_topup() rather than calling get_matched_signals()
    directly — this topic's already-cached posts are reused as-is when
    they already satisfy evidence_required, and only the delta is
    fetched (via get_matched_signals, injected as matcher_fn) otherwise.
    get_matched_signals() itself, and its matching rules, are completely
    untouched by this — get_evidence_with_topup() is purely a caching
    layer on top of it. (KEYING-MISMATCH FIX: get_evidence_with_topup()
    already has this call's own `chat_id` in scope and now passes it
    straight through to matcher_fn(), so get_matched_signals()'s own
    query-embedding cache stays keyed on the same {chat_id, topic_key}
    document as this evidence-posts cache — nothing needed to change
    here.)

    (RESPONSE_TIMEOUT'S NEW ROLE) By the time this is called,
    _fill_in_message_outputs() already confirmed the merged
    (flintel_signals + Google) pool was empty as of that check — but
    this function re-fetches BOTH sources fresh rather than trusting
    that stale snapshot, since some time may have passed between that
    check and this actually running (especially when scheduled via
    BackgroundTasks). If a real merged pool exists now, this answers
    normally from it.

    (CLOSEST-MATCHES TIER-3 REMOVED) The old "loose_candidates /
    near_match_confidence / near_match_offer / withhold-until-user-
    confirms" branch is GONE. Whatever merged_pool has — even a handful
    of posts, even zero — is handed straight to analyze_with_claude().
    Zero posts still lands in analyze_with_claude()'s own existing "no
    posts yet" honest branch (untouched) — that IS the correct
    "I genuinely found nothing" outcome, never a fabricated "closest
    match" offer.

    (CIRCULAR-IMPORT NOTE) get_chat_session / save_claude_answer_to_chat /
    save_signal_results_to_chat / append_to_chat_summary / _clear_owner_busy
    all still live in index.py (per FILE 3's own "KEEP in index.py" list —
    they're flintel_users_chat Mongo orchestration, not "brain" logic) —
    imported here LAZILY, inside the function body, specifically to avoid
    a circular import: index.py imports _timeout_fallback_answer FROM
    logics.py, so logics.py cannot import index.py names at module load
    time. By the time this function actually RUNS, index.py has already
    finished importing logics.py, so this local import resolves fine."""
    from index import (
        get_chat_session, save_claude_answer_to_chat, save_answer_for_gate,
        save_signal_results_to_chat, append_to_chat_summary, _clear_owner_busy,
    )
    import guest_gate
    gate_key = gate_owner or owner_key       # (GUEST GATE) the identity the gate knows this search by

    def _superseded():
        """(GUEST GATE) This search no longer owns its reservation: finish the turn with the explicit notice only."""
        try:
            # compare-and-set: the notice can never overwrite an answer another runner already stored
            if save_answer_for_gate(chat_id, owner_key, topic_key, guest_gate.superseded_answer(), gate_key, request_id):
                save_signal_results_to_chat(chat_id, owner_key, topic_key, [])
        except Exception as exc:
            log.warning(f"Saving the superseded-search notice failed for topic_key={topic_key}: {exc}")
    gate_lease = {}
    try:
        # (GUEST GATE) ask the gate BEFORE any model call (a background retry may not start a second search)
        verdict = guest_gate.on_work_start(gate_key, request_id, topic_key, chat_id, gate_lease)
        if verdict != guest_gate.PROCEED:
            if verdict == guest_gate.ABANDON:
                _superseded()
            return
        # (EVIDENCE-BUDGET FEATURE) Resolved once, up front — used for
        # both the get_matched_signals() `limit` pass below and the
        # merge_matched_and_google_results() `max_total` pass. Mirrors
        # the exact formula used in _complete_message_answer_and_results().
        effective_evidence_limit = min(
            evidence_required or MIN_ANALYSIS_EVIDENCE,
            MAX_ANALYSIS_EVIDENCE,
        )

        # (BUG FIX — DON'T RE-SUGGEST A DECLINED ALTERNATIVE) Same
        # continuity context as _complete_message_answer_and_results()
        # above — best-effort, never blocks this fallback answer.
        try:
            existing_chat = get_chat_session(chat_id, owner_key)
            chat_summary_for_answer = (existing_chat or {}).get("summary") or ""
        except Exception as exc:
            log.warning(f"Chat summary lookup failed for topic_key={topic_key}: {exc}")
            chat_summary_for_answer = ""
        continuity_ctx = None
        if chat_summary_for_answer:
            continuity_ctx = (
                "Conversation so far (auto-summarized, may be empty) — see "
                "the CONVERSATION CONTINUITY instruction above for how to "
                "use this:\n" + chat_summary_for_answer
            )

        # (MERGE BEFORE ANSWERING) ONE fresh re-check — never trusts the
        # earlier "empty" snapshot that triggered this call. Now cache-
        # aware via get_evidence_with_topup(): reuses this topic's already-
        # cached posts when they already satisfy evidence_required, and
        # only fetches the delta (via get_matched_signals, injected as
        # matcher_fn) otherwise — same wiring as _fill_in_message_
        # outputs()'s own call site in index.py.
        _fresh_prov = {} if freshness_enabled() else None      # (FRESHNESS) per-request provenance; None = off
        _fb_platform = targeting_platform or "all"            # missing selection = all enabled platforms, as for every other path
        try:
            _strict_fb = strict_intent_mode()
            _fb_kwargs = {"user_query": query} if _strict_fb else {}
            if _fresh_prov is not None:
                _fb_kwargs["provenance_out"] = _fresh_prov
            with _usage_attribution(chat_id=chat_id, owner_key=owner_key, topic_key=topic_key, request_id=request_id):
                matched = get_evidence_with_topup(
                    chat_id=chat_id, owner_key=owner_key, topic_key=topic_key,
                    keywords=keywords or [], evidence_required=evidence_required,
                    matcher_fn=get_matched_signals, match_phrases=match_phrases,
                    targeting_platform=_fb_platform, **_fb_kwargs,       # (PILOT SAFETY) the request's own selection, never a hard-coded "all"
                    **({"time_range": time_range} if time_range else {}),   # (DATE/TIME)
                    **({"since_days": time_window_days} if time_window_days else {}),   # (RECENCY) the message's own window
                )
        except Exception as exc:
            log.warning(f"Signal matching failed for topic_key={topic_key}: {exc}")
            matched = []
        try:
            # STRICT: Google stubs never pad the count, so don't even fetch.
            stub_docs = ([] if (strict_intent_mode() or not google_stubs_apply(_fb_platform)) else
                         google_search.get_stub_results_for_keywords(
                             google_posts_collection, keywords or []))
        except Exception as exc:
            log.warning(f"Fetching Google-fallback stubs failed for topic_key={topic_key}: {exc}")
            stub_docs = []
        google_results = flintel.format_google_stub_results(stub_docs)
        merged_pool = flintel.merge_matched_and_google_results(
            matched, google_results, max_total=effective_evidence_limit,
            **({"fill_with_stubs": False} if strict_intent_mode() else {}))

        # (CLOSEST-MATCHES TIER-3 REMOVED) No tier-3 branch anymore.
        # Whatever merged_pool has — even a handful of posts, even zero —
        # is handed straight to analyze_with_claude(). Zero posts still
        # lands in analyze_with_claude()'s own existing "no posts yet"
        # honest branch (untouched) — that IS the correct "I genuinely
        # found nothing" outcome, never a fabricated "closest match" offer.
        extra_ctx_parts = [continuity_ctx] if continuity_ctx else []
        # (MIXED EVIDENCE HANDLING) Always added — same reminder as
        # _complete_message_answer_and_results() in index.py.
        extra_ctx_parts.append(flintel.build_mixed_evidence_note())
        if google_results:
            extra_ctx_parts.append(flintel.build_combined_source_context(len(matched), len(google_results)))
        if website_note:
            extra_ctx_parts.append(website_note)
        _rq_note = _resolved_request_note(query, resolved_query)   # (MULTI-TURN CONTEXT)
        if _rq_note:
            extra_ctx_parts.append(_rq_note)
        extra_ctx = "\n\n".join(extra_ctx_parts) if extra_ctx_parts else None

        if not guest_gate.on_work_renew(gate_key, request_id, gate_lease, force=True):
            return                  # (GUEST GATE) this runner was taken over: no model call, nothing saved or shown
        with _usage_attribution(chat_id=chat_id, owner_key=owner_key, topic_key=topic_key, request_id=request_id):
            answer = analyze_with_claude(query, merged_pool, extra_context=extra_ctx)
        with time_scope_context(build_time_scope(time_window_days, time_range, requested_at, targeting_platform=_fb_platform)):   # (RECENCY)
            answer = _patch_post_urls_into_answer(answer, merged_pool, query=query)
        answer = enrich_no_results_answer(answer, query, "all", time_range=time_range, keywords=keywords)   # (NO-RESULTS UI)
        answer = enrich_freshness_answer(answer, merged_pool, time_range=time_range, provenance=_fresh_prov,   # (FRESHNESS)
                                         matched_count=len(matched), evidence_limit=effective_evidence_limit)
        final_answer, results_to_save = _finalize_answer_and_results(answer, merged_pool, seed=query)

        final_answer = guest_gate.mark_limited(gate_key, request_id, final_answer)   # (GUEST GATE) says "limited results"
        # (GUEST GATE) a timeout-fallback answer is never counted, but only the reservation's owner may save it
        verdict = guest_gate.on_answer_ready(gate_key, chat_id, topic_key, request_id, final_answer, counted=False,
                                             lease=gate_lease)
        if verdict != guest_gate.DELIVER:
            if verdict == guest_gate.DISCARD:
                _superseded()
            return                                          # WAIT: taken over; DEFER: stays pending for a later retry
        if not save_answer_for_gate(chat_id, owner_key, topic_key, final_answer, gate_key, request_id):
            return                                          # another runner's answer is already stored: this one is dropped
        # (GUEST GATE) a saved timeout-fallback answer is NOT a completed search: free the guest's
        # reservation, but only if THIS request still owns it (a newer search is never released).
        guest_gate.on_search_incomplete(gate_key, request_id)
        try:
            save_signal_results_to_chat(chat_id, owner_key, topic_key, results_to_save)
        except Exception as exc:
            log.warning(f"Saving google-fallback results failed for topic_key={topic_key}: {exc}")
        try:
            append_to_chat_summary(chat_id, owner_key, query, final_answer, keywords=keywords,
                                   resolved_query=resolved_query)
        except Exception as exc:
            log.warning(f"Updating chat summary failed for topic_key={topic_key}: {exc}")
    except Exception as exc:
        log.warning(f"Timeout-fallback analysis failed for topic_key={topic_key}: {exc}")
        guest_gate.on_search_incomplete(gate_key, request_id)   # (GUEST GATE) nothing usable was saved
    finally:
        guest_gate.on_work_end(gate_key, request_id, gate_lease)   # give the work lease back (any exit)
        _clear_owner_busy(owner_key)
# (BUGFIX PACK #1) Formats CLAUDE_ANALYSIS_SYSTEM_PROMPT can return that
# mean "no real grounded data to show" — when claude_answer parses to one
# of these, the matching message's post-card `results` are treated as
# empty for that render, instead of showing loosely-matched posts Claude
# itself already rejected as irrelevant/unavailable/disallowed.
_NO_DATA_CLAUDE_FORMATS = {"no_results", "not_available", "disallowed"}


# (MISSING-ANSWER FIX) Keys that carry no answer text of their own: an object
# made only of these renders as an EMPTY answer on the page.
_ANSWER_META_KEYS = {"format", "searched", "near_match_confidence", "answer_status", "website_context", "data_freshness", "time_scope"}


def _answer_has_content(data) -> bool:
    """True when a format-bearing answer object has at least one non-empty
    piece of text the page can actually show (anywhere outside the meta keys)."""
    def has_text(v):
        if isinstance(v, str):
            return bool(v.strip())
        if isinstance(v, dict):
            return any(has_text(x) for x in v.values())
        if isinstance(v, list):
            return any(has_text(x) for x in v)
        return False
    return isinstance(data, dict) and any(has_text(v) for k, v in data.items() if k not in _ANSWER_META_KEYS)


def _incomplete_answer_envelope() -> str:
    """(MISSING-ANSWER FIX) Saved instead of an answer with nothing to show
    (e.g. output cut off before any text). A KNOWN format ("no_results") so
    every frontend renders it, plus "answer_status": "incomplete" so the page
    can show an honest "answer unavailable" state instead of "Completed"."""
    return json.dumps({
        "format": "no_results",
        "answer_status": "incomplete",
        "searched": {"query": None, "platforms": [], "time_window": None},
        "message": "The answer for this search was cut off before it finished, so there is nothing to show.",
        "likely_reason": "The answer generation step stopped early. Running the search again usually works.",
        "suggested_actions": [],
    }, ensure_ascii=False)


def _extract_json_object_from_text(text: str) -> str:
    """(DEFENSIVE JSON-EXTRACTION) A small, complex system prompt can
    occasionally cause even a well-instructed model to prepend a short
    burst of "thinking out loud" text (e.g. narrating its own reasoning
    steps) before the actual JSON object it was told to return — this
    happened in practice with CLAUDE_ANALYSIS_SYSTEM_PROMPT's own
    "CORE PRINCIPLE" line reading like a step-by-step template the model
    started echoing back verbatim as labeled headers. The prompt itself
    has been reworded to make this far less likely, but this function is
    the SAFETY NET underneath that prompt fix: it finds and extracts the
    actual JSON object from `text` NO MATTER what surrounds it, so a
    single occasional slip never reaches the frontend as a raw wall of
    text.

    Strategy, in order:
      1. If a ```...``` fence exists anywhere in `text` (not just at the
         very start), try parsing whatever is between the FIRST pair of
         fences (optionally preceded by a "json" language tag).
      2. Otherwise (or if that fails), find the FIRST "{" anywhere in
         `text` and use Python's own JSON decoder to parse a complete,
         correctly-balanced JSON value starting there — this correctly
         handles nested braces and braces that appear inside quoted
         string values, unlike a naive character-counting approach,
         since it's the real parser doing the work, not a guess. If
         that specific "{" doesn't lead to valid JSON (e.g. it was a
         stray brace inside plain narration, not the real object's
         start), tries the NEXT "{" in the text, and so on, until one
         succeeds or none are left.
      3. If nothing above produces valid, parseable JSON, return `text`
         completely unchanged — this function can only ever CLEAN a
         response, never make one worse than doing nothing.

    Returns a JSON string (re-serialized from whatever was successfully
    parsed) on success, or the original `text` unchanged on failure.
    Never raises."""
    if not text or not isinstance(text, str):
        return text

    stripped = text.strip()

    fence_match = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            parsed = json.loads(candidate)
            # (BUG 3) Require "format" too — otherwise a complete, valid,
            # but IRRELEVANT nested JSON object living inside truncated
            # text (which itself has no "format" key) could get returned
            # instead of the real top-level answer object.
            if isinstance(parsed, dict) and "format" in parsed:
                if not _answer_has_content(parsed):          # (MISSING-ANSWER FIX)
                    log.warning("Claude answer had a format but no displayable content — saving an incomplete-answer envelope")
                    return _incomplete_answer_envelope()
                return json.dumps(parsed, ensure_ascii=False)
        except (ValueError, TypeError):
            pass

    brace_index = stripped.find("{")
    while brace_index != -1:
        try:
            parsed, _end_index = json.JSONDecoder().raw_decode(stripped, brace_index)
            if isinstance(parsed, dict) and "format" in parsed:
                if not _answer_has_content(parsed):          # (MISSING-ANSWER FIX)
                    log.warning("Claude answer had a format but no displayable content — saving an incomplete-answer envelope")
                    return _incomplete_answer_envelope()
                return json.dumps(parsed, ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        brace_index = stripped.find("{", brace_index + 1)

    # (BUG 3 — TRUNCATED JSON REPAIR) Nothing above found a complete,
    # valid, "format"-bearing JSON object — try repairing genuinely
    # truncated JSON (e.g. cut off by max_tokens) before giving up.
    repaired = _repair_truncated_json(stripped)
    if isinstance(repaired, dict) and repaired.get("format"):
        # (MISSING-ANSWER FIX) A cut-off answer is never "complete": keep
        # whatever real text survived, marked "partial"; if nothing
        # displayable survived (e.g. only {"format": ...}), save the
        # explicit incomplete-answer envelope instead of an empty answer.
        if not _answer_has_content(repaired):
            log.warning("Claude answer was truncated before any content — saving an incomplete-answer envelope")
            return _incomplete_answer_envelope()
        log.warning("Claude answer was truncated JSON — repaired (marked partial) before saving")
        repaired["answer_status"] = "partial"
        return json.dumps(repaired, ensure_ascii=False)

    # (FINAL SAFETY NET — NEVER PERSIST BARE PROSE) Every caller of this
    # function immediately hands the return value to
    # save_claude_answer_to_chat(), which persists it in Mongo and serves
    # it back on every future page load/refresh of this message — so
    # returning raw, un-enveloped text here (the old behavior) meant a
    # single bad generation could leave a message permanently showing an
    # unstyled wall of text (or, worse, leaked "thinking out loud"
    # narration like "I need to work through this carefully...") instead
    # of the app's normal card UI, until someone manually re-triggered
    # that exact message. With force_json_prefill now on every caller of
    # _call_claude() that feeds this function, reaching this point at all
    # should be rare (the model can no longer start its reply with prose
    # — see _call_claude()'s own docstring) — this is the last-resort
    # backstop for genuinely pathological cases (e.g. an empty/whitespace
    # response, or something that still isn't valid JSON even once
    # repaired). Wrapping `text` into a real, KNOWN_FORMAT-compliant
    # "no_results" object guarantees the frontend (chat.html's
    # renderStructuredAnswer()) ALWAYS receives parseable, format-bearing
    # JSON — never raw prose — so the card UI renders every single time,
    # with no dependency on window.marked/DOMPurify having loaded.
    log.warning(
        "Claude answer had no parseable JSON object even after repair — "
        "wrapping as a safe no_results envelope instead of persisting raw "
        f"text (first 200 chars: {text[:200]!r})"
    )
    fallback_message = text.strip() if text and text.strip() else (
        "Search ran, but the answer couldn't be generated in the expected "
        "format this time."
    )
    # Keep this readable/short in the UI — the raw leaked text (if any) is
    # still in the log line above for debugging, but showing an entire
    # rambling reasoning trace to the user is never useful.
    if len(fallback_message) > 400:
        fallback_message = fallback_message[:400].rsplit(" ", 1)[0] + "…"
    safe_envelope = {
        "format": "no_results",
        "no_results_reason": "error",   # (NO-RESULTS UI) an answer-generation failure, not an empty search
        "searched": {"query": None, "platforms": [], "time_window": None},
        "message": fallback_message,
        "likely_reason": "The answer generation step hit an unexpected formatting issue.",
        "suggested_actions": [
            {"type": "broaden_time", "label": "Extend to last 30 days"},
            {"type": "broaden_platforms", "label": "Include all platforms"},
        ],
    }
    return json.dumps(safe_envelope, ensure_ascii=False)


def _extract_claude_format(answer_text: str):
    """(BUGFIX PACK #1) Best-effort parse of a claude_answer string to
    pull out its "format" field, mirroring the same fence-stripping
    tolerance already used by _parse_router_json() elsewhere in this
    file. Returns None (never raises) if the text is missing, isn't
    valid JSON, or doesn't have a string "format" field — callers treat
    None as "unknown/can't tell", which always means "show results as
    before", never "hide them". This only ever narrows what's shown when
    it can positively confirm Claude's own JSON said there was nothing
    relevant; it can never hide results based on a guess."""
    if not answer_text:
        return None
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fmt = data.get("format")
    return fmt if isinstance(fmt, str) else None


def _extract_near_match_confidence(answer_text: str):
    """(CLOSEST-MATCHES TIER-3 REFINEMENT) Best-effort parse of a
    claude_answer string to pull out its "near_match_confidence" field
    (only present on the "no_results" format's tier-3 refinement — see
    CLAUDE_ANALYSIS_SYSTEM_PROMPT's own instruction for what this
    means). Same fence-stripping tolerance as _extract_claude_format().
    Returns None (never raises) if the text is missing, isn't valid
    JSON, or doesn't have a "near_match_confidence" field at all — this
    is indistinguishable from Claude itself explicitly returning
    null/None for that field, which is the correct, safe default
    ("no loose candidates existed" / "treat as ordinary no_results")."""
    if not answer_text:
        return None
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    confidence = data.get("near_match_confidence")
    return confidence if confidence in ("high", "low") else None


# (BUG FIX — DON'T HIDE REAL MATCHED POSTS / KEEP A POSITIVE CLOSING NOTE)
# Rotated the same cheap-hash way as flintel.py's own _pick_invite_line()
# and website_intelligence.py's own _pick_variant(), so the same
# sentence doesn't repeat mechanically every time.
_CLOSEST_MATCHES_NOTE_VARIANTS = [
    "These are the closest posts I found — happy to run a more targeted search if you give me a specific angle.",
    "I've shared the nearest matches below in case they're useful — let me know if you'd like this narrowed down further.",
    "Sharing the closest posts I could find below — point me at a more specific angle and I can dig further.",
    "Yeh sabse qareeb posts hain jo mujhe mile — agar aap koi khaas angle bata dein to main zyada targeted search kar sakta hoon.",
]


def _pick_closest_matches_note(seed: str = "") -> str:
    """Same cheap, deterministic-hash rotation already used elsewhere in
    this product (flintel.py's _pick_invite_line, website_intelligence.py's
    _pick_variant) — avoids the exact same sentence every time without
    needing a random source or stored state."""
    if not seed:
        return _CLOSEST_MATCHES_NOTE_VARIANTS[0]
    idx = sum(ord(c) for c in seed) % len(_CLOSEST_MATCHES_NOTE_VARIANTS)
    return _CLOSEST_MATCHES_NOTE_VARIANTS[idx]


def _append_closest_matches_note(answer_text: str, seed: str = "") -> str:
    """(BUG FIX) Purely additive, non-destructive post-processing step,
    same parse/patch/re-serialize pattern already used by
    _patch_post_urls_into_answer() and _inject_website_context_into_answer().
    Appends one short, rotating, positive closing line onto the
    format's own "message" field — never changes Claude's own honest
    assessment text otherwise. If answer_text isn't valid JSON, or has
    no "message" field, the original text is returned unchanged.

    (BUG FIX — DON'T SHOW STALE "TRY THESE NEXT STEPS" ALONGSIDE REAL
    POSTS) Also strips "suggested_actions" and "clarifying_question"
    from the JSON in this same pass — see the inline comment below for
    why, and _finalize_answer_and_results()'s docstring for confirmation
    that this function is ONLY ever invoked when real matched posts are
    about to be shown to the user."""
    if not answer_text:
        return answer_text
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict):
        return answer_text

    val = data.get("message")
    if not isinstance(val, str) or not val.strip():
        return answer_text

    # (BUG FIX — DON'T SHOW STALE "TRY THESE NEXT STEPS" ALONGSIDE REAL
    # POSTS) Once real matched posts are being shown below this text
    # (which is the ONLY case this function is ever called for — see
    # _finalize_answer_and_results()), the format's own "suggested_actions"
    # (broaden time/platform/term) and "clarifying_question" fields become
    # redundant/contradictory — the user already sees real posts, so
    # "try broadening your search" reads as odd next to them. Remove
    # both fields here, in this branch ONLY. A genuine no_results answer
    # (matched empty) never reaches this function — see
    # _finalize_answer_and_results() — so its suggested_actions /
    # clarifying_question are completely untouched by this change.
    data.pop("suggested_actions", None)
    data.pop("clarifying_question", None)

    data["message"] = val.strip() + " " + _pick_closest_matches_note(seed)
    return json.dumps(data, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# (POST DISPLAY PREFERENCE) Showing real posts stays the default. Only when the
# CURRENT request explicitly says not to show posts ("don't give me posts",
# "no posts", "just summarize", "posts mat dikhao") is the answer returned
# without its post list — the retrieved posts are still given to the model as
# evidence and the saved `results` are unchanged. An explicit request for posts
# ("show me posts", "3 example posts") wins over / combines with that.
# ─────────────────────────────────────────────────────────────────────────────
_POST_WORDS = r"(?:posts?|links?|examples?|threads?)"
# words that refer to the displayed post list when the user turns it OFF
# ("sources" = the source table); not used on the want-posts side
_HIDE_WORDS = r"(?:posts?|links?|examples?|threads?|sources?)"
# "posts from X / older than / about Y ..." is a FILTER on which posts to
# find, not a request to hide all posts -> never treated as no-posts
_NOT_A_FILTER = (r"(?!\s+(?:from|on|about|older|newer|before|after|by|in|that|which|where|under|over|"
                 r"with|without|mentioning|containing|than)\b)")
# a "no posts"/"hide posts" instruction ends its clause here
_CLAUSE_END = r"(?=\s*(?:[,.;:!?)\-\u2014]|$|\b(?:and|just|only|please|pls|plz)\b))"
_NO_POSTS_PATTERNS = [
    re.compile(r"\b(?:don'?t|do\s+not|dont|no\s+need\s+to|never|stop)\s+(?:show|give|list|send|display|include|share|add|paste|want|need)(?:ing)?\s+"
               r"(?:me\s+|us\s+)?(?:any\s+|the\s+)?(?:individual\s+|actual\s+|raw\s+|specific\s+|reddit\s+|social\s+media\s+)?"
               + _HIDE_WORDS + r"\b" + _NOT_A_FILTER, re.I),
    # "without posts" -- but not "find posts without links" (posts named
    # earlier in the same sentence = a search filter)
    re.compile(r"(?:^|[.?!]\s*)(?:(?!\b(?:posts?|threads?|comments?)\b)[^.?!])*?"
               r"\bwithout\s+(?:any\s+|the\s+)?(?:individual\s+|actual\s+|raw\s+)?(?:posts?|links?|examples?|sources?)\b"
               + _NOT_A_FILTER, re.I),
    # "no posts, ..." only at the START of a clause (or right after "but") --
    # not "I got no posts" / "there were no posts"
    re.compile(r"(?:^|(?<=[.?!,;:(\-\u2014])|(?<=\bbut))\s*no\s+(?:individual\s+|actual\s+|raw\s+)?(?:posts?|links?|sources?)\s*"
               r"(?:please|pls|plz|needed|necessary|required)?\s*"
               r"(?:[,.;:!)\-\u2014]|$|\b(?:just|only)\b)", re.I),
    re.compile(r"\b(?:just|only)\s+(?:summari[sz]e|(?:give|tell|send)\s+(?:me\s+)?(?:a\s+|the\s+)?(?:summary|overview|gist))\b"
               r"|(?:^|[,.;:!?]\s*)(?:(?!\b(?:posts?|threads?|comments?)\b)[^,.;:!?])*?"
               r"\b(?:summary|overview)\s+only\b(?=\s*(?:[,.;:!?)]|$|please|pls|plz))"
               r"|\b(?:just|only)\s+(?:a|the)\s+(?:summary|overview)\b", re.I),
    # "give me the overall sentiment instead of showing sources" -- a
    # synthesized answer named earlier in the same sentence
    re.compile(r"\b(?:summary|summari[sz]e|overview|sentiment|consensus|gist|takeaways?|analysis|insights?|explanation|"
               r"explain|conclusion|breakdown|answer)\b[^.?!]{0,200}?\binstead\s+of\s+"
               r"(?:showing\s+|listing\s+|giving\s+|displaying\s+|sharing\s+)?(?:me\s+)?(?:the\s+|any\s+)?"
               r"(?:individual\s+|actual\s+|raw\s+)?(?:posts?|sources?)\b", re.I),
    # "hide the posts." / "omit the sources, just ..."
    re.compile(r"\b(?:hide|omit|skip|exclude|remove|leave\s+out)\s+(?:the\s+|all\s+|any\s+)?(?:individual\s+)?"
               r"(?:posts?|links?|sources?)\b" + _CLAUSE_END, re.I),
    re.compile(r"\bposts?\s+(?:mat|na|nahi|nahin|mt)\s+(?:dikha\w*|do|dena|bhej\w*|chahiy\w*|chaiy\w*|lagao|daal\w*)\b", re.I),
    re.compile(r"\bposts?\s+ki\s+(?:zar(?:u|oo)rat|zrurat)\s+nahi\w*", re.I),
    re.compile(r"\b(?:sirf|bas)\s+(?:summary|khulasa|summari[sz]e)\b", re.I),
]
_WANT_POSTS_PATTERNS = [
    re.compile(r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a\s+few|few|some|a\s+couple\s+of|couple\s+of)\s+"
               r"(?:example\s+|sample\s+|relevant\s+|top\s+|reddit\s+)?" + _POST_WORDS + r"\b", re.I),
    # display verbs only -- "find/search for/look for posts" is retrieval
    # wording, not a request to be SHOWN posts (see _RETRIEVAL_VERB_BEFORE)
    re.compile(r"\b(?:show|give|list|send|include|share|add|paste)\s+"
               r"(?:me\s+|us\s+)?(?:the\s+|some\s+|a\s+few\s+|few\s+|any\s+)?"
               r"(?:example\s+|sample\s+|relevant\s+|top\s+|actual\s+|reddit\s+)?" + _POST_WORDS + r"\b", re.I),
    re.compile(r"\bwith\s+(?:some\s+|a\s+few\s+|few\s+)?(?:example\s+|sample\s+)?(?:posts?|examples?|links?)\b", re.I),
    re.compile(r"\bposts?\s+(?:bhi\s+)?(?:dikhao|dikha\s+do|do|dena|bhejo)\b", re.I),
    # elliptical count with a DISPLAY verb: "show me 5, but no individual
    # posts" -> a display request too, so together with the hide wording it
    # is a MIXED request (show only that many), like "don't show posts but
    # give me 2 examples". Retrieval verbs ("find 5") never count.
    re.compile(r"\b(?:show|give|list|send)\s+(?:me\s+|us\s+)?"
               r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b(?=\s*(?:[,.;:!?]|$|\bbut\b))", re.I),
]
# "find 10 posts" / "search for 5 reddit posts": the count belongs to the
# search, so it is not a display request either
_RETRIEVAL_VERB_BEFORE = re.compile(
    r"\b(?:find|search(?:\s+for)?|look\s+(?:for|up)|fetch|pull|get|scan|scrape|collect|retrieve)\s+"
    r"(?:me\s+|us\s+)?$", re.I)
# a negation word earlier in the same clause ("don't give me posts"); a
# "but/however/instead/and" after it starts a new, un-negated clause
_NEGATED_BEFORE = re.compile(
    r"\b(?:don'?t|do\s+not|dont|no\s+need\s+to|never|without|stop|skip)\b"
    r"(?:(?!\b(?:but|however|though|instead|except|and)\b)[^,.;!?])*$", re.I)


# typographic apostrophes (’ ‘ ʼ ′ `) -> "'" before matching, so "don’t"
# reads like "don't". One char -> one char: match offsets are unchanged.
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u2032": "'", "`": "'"})


def _explicit_no_posts_request(query: str) -> bool:
    q = (query or "").translate(_APOSTROPHES)
    return any(p.search(q) for p in _NO_POSTS_PATTERNS)


def _explicit_posts_request(query: str) -> bool:
    """True when the query asks FOR posts (not when that phrase is negated,
    e.g. the "give me posts" inside "don't give me posts")."""
    q = (query or "").translate(_APOSTROPHES)
    for pat in _WANT_POSTS_PATTERNS:
        for m in pat.finditer(q):
            before = q[max(0, m.start() - 40):m.start()]
            if _NEGATED_BEFORE.search(before) or _RETRIEVAL_VERB_BEFORE.search(before):
                continue
            return True
    return False


def _post_display_note(query: str):
    """Extra analysis context for THIS request only; None (no change at all)
    unless the user explicitly asked not to see posts."""
    if not _explicit_no_posts_request(query):
        return None
    if _explicit_posts_request(query):
        return ("Note (this request only): the user asked for a synthesized answer AND explicitly "
                "asked to see example posts. Write the summary/findings as usual, and in "
                "\"platforms\" list only as many posts as they asked for (if they named a number), "
                "choosing the most representative ones.")
    return ("Note (this request only): the user explicitly asked NOT to be shown individual posts. "
            "Still read and use every post you were given as evidence for the summary, but return "
            "\"platforms\" as an empty list [] — no post entries, no post titles or links in the text. "
            "Keep every claim grounded in those posts.")


def _strip_displayed_posts(answer_text: str) -> str:
    """Empty the displayed post lists (source_list "platforms" / comparison
    subjects' "platforms"); everything else in the answer is kept as is."""
    if not answer_text:
        return answer_text
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict):
        return answer_text
    if data.get("format") == "source_list" and data.get("platforms"):
        data["platforms"] = []
    elif data.get("format") == "comparison":
        changed = False
        for subject in data.get("subjects") or []:
            if isinstance(subject, dict) and subject.get("platforms"):
                subject["platforms"] = []
                changed = True
        if not changed:
            return answer_text
    else:
        return answer_text
    return json.dumps(data, ensure_ascii=False)


def _finalize_answer_and_results(answer_text: str, matched: list, seed: str = ""):
    """(CHANGE 3 — STOP OVERRIDING CLAUDE'S OWN "NO DATA" VERDICT) Shared
    decision point for both the non-streaming path
    (_complete_message_answer_and_results) and the streaming route
    (routes.py): given Claude's own answer text and the posts actually
    matched, decides what to persist as this message's final `results`.

    Previously this force-showed `matched` posts (with an appended
    closing note) whenever Claude's own answer format was a "no_data"
    one (no_results/not_available/disallowed) but `matched` was
    non-empty — on the theory that Claude's format choice was purely
    about narrative confidence, not about whether real data existed.
    Now that CHANGE 2's phrase-matching keeps `matched` itself far more
    tightly scoped to genuine topical relevance, that override is no
    longer needed and is actively wrong: Claude's own honest "no_results"
    verdict is trusted and respected — the primary/normal answer path
    never force-shows posts Claude itself already judged irrelevant.
    (`_append_closest_matches_note()`/`_pick_closest_matches_note()`
    remain defined elsewhere in this file but are no longer called from
    here or anywhere else automatically.)

    Genuinely empty results (no data, or Claude picked a "no_data"
    format) are saved as an empty list, exactly as before.

    (POST DISPLAY PREFERENCE) Every caller passes this message's own query
    as `seed`: when it explicitly excludes posts (and does not also ask for
    them), the answer's displayed post list is emptied here. `results` (and
    the evidence the answer was written from) are not touched.

    Returns (final_answer_text, results_to_save)."""
    claude_format = _extract_claude_format(answer_text)
    if claude_format in _NO_DATA_CLAUDE_FORMATS:
        return answer_text, []
    if seed and _explicit_no_posts_request(seed) and not _explicit_posts_request(seed):
        answer_text = _strip_displayed_posts(answer_text)
    # (ANSWER INTEGRITY) each stored result carries its place in the checked
    # report (in_report / excluded / not_selected / not_analyzed).
    if strict_intent_mode():
        # Strict: cards and answer text are ONE list.
        return answer_text, answer_integrity.annotate_results(_strict_cards_for_answer(answer_text, matched), answer_text)
    return answer_text, answer_integrity.annotate_results(matched, answer_text)


# ─────────────────────────────────────────────────────────────────────────────
# POST_URL PATCH FIX — additive post-processing only, see module docstring.
# ─────────────────────────────────────────────────────────────────────────────

def _best_matching_post(title: str, matched_signals: list):
    """(POST_URL FIX) Best-effort match of a title string (as it appears
    inside Claude's JSON answer) back to one of the real matched_signals
    entries, so its real post_url can be looked up. Tries an exact
    case-insensitive match first, then falls back to a loose
    containment check (either string contains the other) since Claude
    may lightly reword/trim a title when it copies it into its answer.
    Returns the matching signal dict, or None if nothing looks like a
    reasonable match — callers must treat None as "leave the link field
    alone", never guess a URL."""
    if not title:
        return None
    title_norm = title.strip().lower()
    if not title_norm:
        return None

    for sig in matched_signals or []:
        sig_title = (sig.get("title") or "").strip().lower()
        if sig_title and sig_title == title_norm:
            return sig

    for sig in matched_signals or []:
        sig_title = (sig.get("title") or "").strip().lower()
        if not sig_title:
            continue
        if sig_title in title_norm or title_norm in sig_title:
            return sig

    return None


_ENRICH_STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "your", "with", "this",
    "that", "from", "they", "them", "their", "have", "has", "had", "was",
    "were", "will", "would", "can", "could", "should", "about", "into",
    "what", "which", "who", "how", "why", "when", "where", "there", "here",
    "any", "all", "some", "more", "most", "very", "just", "also", "than",
    "then", "its", "it's", "our", "out", "one", "use", "using", "used",
    "asks", "ask", "asking", "says", "said", "user", "users", "post", "poster",
    "looking", "wants", "want", "need", "needs", "like", "get", "gets",
}

# Summary-similarity match: fraction of the summary's content words that
# appear in the post text. Must clear the threshold AND beat the runner-up
# by a clear margin — a near-tie is treated as "no match".
_SUMMARY_MATCH_MIN_SCORE = 0.5
_SUMMARY_MATCH_MIN_MARGIN = 0.15
_SUMMARY_MATCH_MIN_TOKENS = 3


def _content_tokens(text) -> set:
    if not isinstance(text, str) or not text:
        return set()
    words = re.findall(r"[a-z0-9][a-z0-9_'+-]*", text.lower())
    return {w for w in words if len(w) >= 3 and w not in _ENRICH_STOPWORDS}


def _summary_overlap(summary_tokens: set, post_text) -> float:
    if not summary_tokens:
        return 0.0
    return len(summary_tokens & _content_tokens(post_text)) / len(summary_tokens)


def _match_by_summary(summary, candidates: list):
    """Confident summary->signal match, or None (low score / near-tie)."""
    s_tokens = _content_tokens(summary)
    if len(s_tokens) < _SUMMARY_MATCH_MIN_TOKENS or not candidates:
        return None
    scored = sorted(
        ((_summary_overlap(s_tokens, c.get("post_text")), i) for i, c in enumerate(candidates)),
        reverse=True,
    )
    best_score, best_i = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < _SUMMARY_MATCH_MIN_SCORE:
        return None
    if best_score - runner_up < _SUMMARY_MATCH_MIN_MARGIN:
        return None
    return candidates[best_i]


def _parse_post_index(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _resolve_answer_post(post: dict, matched_signals: list, aligned: list):
    """Map one post object from Claude's answer back to a real matched
    signal. Tries, in order: (1) its "index" against the aligned prompt
    order, (2) its title (only when not a placeholder), (3) summary/text
    similarity. Returns the signal dict, or None when nothing is confident."""
    summary_tokens = _content_tokens(post.get("summary"))

    # 1. Index — sanity-checked: if the summary is substantive but shares
    #    no content word with the indexed post, the index is not trusted.
    idx = _parse_post_index(post.get("index"))
    if idx is not None and 1 <= idx <= len(aligned):
        cand = aligned[idx - 1]
        if len(summary_tokens) < _SUMMARY_MATCH_MIN_TOKENS or _summary_overlap(summary_tokens, cand.get("post_text")) > 0:
            return cand

    # 2. Title — never with a placeholder like "(no title)".
    title = post.get("title")
    if not _is_placeholder_title(title):
        match = _best_matching_post(title, matched_signals)
        if match:
            return match

    # 3. Summary similarity against posts Claude actually saw.
    return _match_by_summary(post.get("summary"), aligned)


# ─────────────────────────────────────────────────────────────────────────────
# STRICT MODE — result-quality verification (only reached when
# config.STRICT_INTENT_MODE is on). Principle: the card a user sees (URL,
# title, subreddit, summary) must come from ONE database document, and a
# post whose identity cannot be established confidently is not shown at all.
# ─────────────────────────────────────────────────────────────────────────────

_SUMMARY_UNAVAILABLE = "Summary unavailable"
_SUMMARY_EXEMPT_CAPS = {
    "reddit", "twitter", "linkedin", "facebook", "google", "the", "this", "that",
    "post", "poster", "author", "user", "someone", "asks", "seeking",
}


def _norm_title_tokens(title) -> list:
    if not isinstance(title, str):
        return []
    return re.findall(r"[a-z0-9]+", title.lower())


def _title_jaccard(a, b) -> float:
    ta, tb = set(_norm_title_tokens(a)), set(_norm_title_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _stem(token: str) -> str:
    for suf in ("ings", "ing", "ers", "er", "ed", "es", "ly", "s"):
        if len(token) > len(suf) + 3 and token.endswith(suf):
            return token[: -len(suf)]
    return token


def _invented_specifics(summary: str, haystack: str) -> list:
    """Numbers and mid-sentence Capitalised names in `summary` that do not
    appear in `haystack` (the post's title + text)."""
    low = haystack.lower()
    missing = []
    words = summary.split()
    for i, raw in enumerate(words):
        w = raw.strip(".,;:!?()[]{}\"'`*")
        if not w:
            continue
        if any(ch.isdigit() for ch in w):
            digits = re.sub(r"[^0-9a-z.]", "", w.lower())
            if digits and digits not in low:
                missing.append(w)
            continue
        sentence_start = i == 0 or words[i - 1].endswith((".", "!", "?", ":"))
        if (not sentence_start and len(w) >= 3 and w[0].isupper()
                and w.lower() not in _SUMMARY_EXEMPT_CAPS):
            if w.lower() not in low:
                missing.append(w)
    return missing


def _summary_supported(summary, title, text) -> bool:
    """True when the summary is grounded in THIS post: enough of its content
    words occur in the post and it names nothing (number / proper noun) the
    post does not contain."""
    if not isinstance(summary, str) or not summary.strip():
        return False
    tokens = _content_tokens(summary)
    if not tokens:
        return False
    haystack = f"{title or ''}\n{text or ''}"
    post_stems = {_stem(t) for t in _content_tokens(haystack)}
    hit = sum(1 for t in tokens if _stem(t) in post_stems)
    min_overlap = float(_cfg_value("STRICT_SUMMARY_MIN_OVERLAP", 0.5))
    if hit / len(tokens) < min_overlap:
        return False
    return not _invented_specifics(summary, haystack)


def _resolve_answer_post_strict(post: dict, aligned: list):
    """Strict identity resolution of one answer post -> (signal, 1-based index)
    or (None, None). Requires agreement between the signals the model could
    have meant; a conflict or weak match resolves to NOTHING (post dropped).
    Never uses a URL the model supplied."""
    min_j = float(_cfg_value("STRICT_TITLE_MATCH_MIN_JACCARD", 0.6))
    title = post.get("title")
    has_title = isinstance(title, str) and title.strip() and not _is_placeholder_title(title)
    summary = post.get("summary")
    s_tokens = _content_tokens(summary)

    def summary_ok(sig):
        return _summary_supported(summary, sig.get("title"), sig.get("post_text"))

    idx = _parse_post_index(post.get("index"))
    idx_sig = aligned[idx - 1] if (idx is not None and 1 <= idx <= len(aligned)) else None

    title_sig = None
    if has_title:
        scored = []
        for i, sig in enumerate(aligned):
            st = sig.get("title") or ""
            exact = bool(st.strip()) and st.strip().lower() == title.strip().lower()
            j = 1.0 if exact else _title_jaccard(title, st)
            if j >= min_j:
                scored.append((j, i))
        scored.sort(reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] > 0.0):
            title_sig = aligned[scored[0][1]]
            title_pos = scored[0][1] + 1

    def pos_of(sig):
        for i, a in enumerate(aligned):
            if a is sig or (a.get("post_url") and a.get("post_url") == sig.get("post_url")):
                return i + 1
        return None

    if idx_sig is not None:
        if title_sig is None:
            if not has_title:
                return idx_sig, idx
            # The title matched no post: trust the index only if the
            # summary corroborates it.
            if len(s_tokens) >= _SUMMARY_MATCH_MIN_TOKENS and summary_ok(idx_sig):
                return idx_sig, idx
            return None, None
        if title_sig is idx_sig or (title_sig.get("post_url") and title_sig.get("post_url") == idx_sig.get("post_url")):
            return idx_sig, idx
        # Index and title disagree: keep the one the summary supports, and
        # only if exactly one does.
        supported = [sg for sg in (idx_sig, title_sig) if summary_ok(sg)]
        if len(supported) == 1:
            chosen = supported[0]
            return chosen, pos_of(chosen)
        return None, None

    if title_sig is not None:
        return title_sig, pos_of(title_sig)

    # No usable index, no usable title: summary similarity with margin AND
    # the strict overlap/specifics check.
    match = _match_by_summary(summary, aligned)
    if match is not None and summary_ok(match):
        return match, pos_of(match)
    return None, None


def _strict_patch_answer(data: dict, matched_signals: list, answer_text: str, query=None, post_records=None,
                         time_scope=None) -> str:
    """Strict replacement for the permissive link/source/title patching."""
    fmt = data.get("format")
    if fmt not in ("source_list", "comparison"):
        return answer_text
    try:
        aligned = [sig for _post, sig in _aligned_post_context(matched_signals)]
    except Exception:
        aligned = []
    state = {"kept": 0, "dropped": 0, "seen": 0}

    def _patch_platform_list(platforms):
        if not isinstance(platforms, list):
            return platforms
        out = []
        for entry in platforms:
            if not isinstance(entry, dict) or not isinstance(entry.get("posts"), list):
                out.append(entry)
                continue
            new_posts, used = [], set()
            for post in entry["posts"]:
                if not isinstance(post, dict):
                    continue
                state["seen"] += 1
                try:
                    sig, pos = _resolve_answer_post_strict(post, aligned)
                except Exception:
                    sig, pos = None, None
                key = (sig.get("post_url") or id(sig)) if sig else None
                if sig is None or key in used:
                    state["dropped"] += 1
                    continue
                used.add(key)
                new = dict(post)
                if query is not None:   # the model's own title/number, for answer_integrity's identity check
                    new["_model_title"], new["_model_index"] = post.get("title"), post.get("index")
                url = sig.get("post_url")
                if url:
                    new["link"] = url
                else:
                    new.pop("link", None)
                if "url" in new:
                    new["url"] = url or ""
                if pos is not None:
                    new["index"] = pos
                platform = (sig.get("platform") or _infer_platform_from_url(url) or "")
                if str(platform).strip().lower() == "reddit":
                    sub = _normalize_subreddit(sig.get("subreddit")) or _subreddit_from_url(url)
                    if sub:
                        new["source"] = sub
                    else:
                        new.pop("source", None)
                db_title = _derive_post_title(sig.get("title"), sig.get("post_text"))
                if db_title:
                    new["title"] = db_title
                if not (isinstance(new.get("summary"), str)
                        and new["summary"].startswith("Content not yet available")):
                    if not _summary_supported(new.get("summary"), sig.get("title"), sig.get("post_text")):
                        new["summary"] = _SUMMARY_UNAVAILABLE
                new_posts.append(new)
                state["kept"] += 1
            if new_posts:
                entry = dict(entry, posts=new_posts)
                if "shown_count" in entry:
                    entry["shown_count"] = len(new_posts)
                if "total_analyzed" in entry:
                    entry["total_analyzed"] = len(new_posts)
                out.append(entry)
        return out

    if fmt == "source_list":
        data["platforms"] = _patch_platform_list(data.get("platforms"))
    else:
        for subject in data.get("subjects") or []:
            if isinstance(subject, dict):
                subject["platforms"] = _patch_platform_list(subject.get("platforms"))

    if state["seen"] and state["kept"] == 0:
        # Every listed post failed identity verification: say so honestly
        # instead of showing unverifiable cards.
        data = {
            "format": "no_results",
            "no_results_reason": "unverified",   # (NO-RESULTS UI) posts found, none verifiable
            "searched": {"query": "", "platforms": [], "time_window": ""},
            "message": ("This search surfaced posts, but none of them could be verified "
                        "against their source text, so none are shown as qualified results."),
            "likely_reason": "The listed posts could not be matched back to their original posts with confidence.",
            "suggested_actions": [],
        }
    elif query is not None:
        data = answer_integrity.enforce_answer_integrity(data, matched_signals, aligned, query, post_records,
                                                         time_scope=time_scope)
    return json.dumps(data, ensure_ascii=False)


def _strict_cards_for_answer(answer_text: str, matched: list) -> list:
    """Cards shown under the answer == the posts the (already strictly
    patched) answer lists, in the answer's order. Formats without a posts
    list keep `matched` unchanged."""
    if not answer_text or not matched:
        return matched
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return matched
    if not isinstance(data, dict) or data.get("format") not in ("source_list", "comparison"):
        return matched
    if data.get("format") == "source_list":
        groups = [data.get("platforms")]
    else:
        groups = [sub.get("platforms") for sub in (data.get("subjects") or []) if isinstance(sub, dict)]
    by_url = {m.get("post_url"): m for m in matched if m.get("post_url")}
    cards, seen, listed = [], set(), 0
    for platforms in groups:
        for entry in platforms or []:
            for post in (entry.get("posts") if isinstance(entry, dict) else None) or []:
                listed += 1
                link = post.get("link") if isinstance(post, dict) else None
                if link in by_url and link not in seen:
                    seen.add(link)
                    cards.append(by_url[link])
    return cards if listed else matched


def _patch_post_urls_into_answer(answer_text: str, matched_signals: list, query=None, time_scope=None) -> str:
    """(POST_URL FIX) CLAUDE_ANALYSIS_SYSTEM_PROMPT asks for a "link"
    field with "the real post URL if available" on every post it lists —
    but Claude is deliberately NEVER shown post_url (see
    build_claude_post_context(), unchanged), so it could never actually
    supply a real one. This best-effort, PURELY ADDITIVE post-processing
    step fixes that: it parses the already-generated answer_text as
    JSON, walks the known formats that carry post lists ("source_list"
    and "comparison"), and for every post object it finds, matches its
    "title" back to one of THIS message's already-matched signals (via
    _best_matching_post()) and sets "link" to that signal's REAL
    post_url — the exact same URL already used by the post cards,
    computed by get_matched_signals(), never anything Claude itself
    supplied or guessed.

    Grounding is preserved: this never lets Claude decide a URL. It only
    ever substitutes in a URL Flintel already knows to be real, and only
    when the title can be confidently matched back to one specific
    signal; if no confident match is found, that post simply keeps
    whatever "link" value (usually absent) Claude left it with — it is
    never given a made-up URL by this function either.

    Best-effort/non-destructive: if answer_text isn't valid JSON, isn't
    one of the two known list-based formats, or matched_signals is
    empty, the original answer_text is returned completely UNCHANGED —
    this can only ever ADD a real link where one is confidently
    resolvable, it can never remove or alter anything else in the
    answer.

    (SIMULATED-STREAM FIX) Now called by GET /chat/{chat_id}/stream on
    the COMPLETE answer text BEFORE any of it is streamed out to the
    browser (previously it only ran after streaming had already
    finished, so the live-streamed text a user first saw never had real
    links — only a later cached re-render did). This function itself is
    completely unchanged; only WHEN/WHERE it's called shifted.

    (google_rank / subreddit PATCHING) In the SAME pass, once a post's
    title is confidently matched back to a signal, if that signal came
    from the Google side of a merged pool (see flintel.merge_matched_
    and_google_results()) and has a "google_rank" and/or "subreddit"
    field, those are patched onto the post too — "google_rank" always,
    "subreddit" only as a fallback "source" if Claude didn't already
    set one. Same best-effort, non-destructive rule as the link patch:
    never invented, only filled in when a confident match already
    exists.

    (POST ENRICHMENT) Matching now goes index -> title -> summary
    similarity (_resolve_answer_post()). On a confident match the SYSTEM
    sets "link" (real post_url), "source" (subreddit for Reddit posts) and
    "title" (real title, or one derived from post_text) — overriding
    Claude's values such as "unknown" / "(no title)". "summary" and
    "sentiment" are never touched. No confident match -> no link/source
    is ever added."""
    if not answer_text:
        return answer_text
    if not matched_signals and query is None:
        return answer_text

    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        # (GATE 2) a plain-text answer (not JSON, not a cut-off JSON object): no evidence rows exist to check it against, so it
        # may not state a time-dependent fact about the data. Only when the production caller passes the query.
        if query is not None and not cleaned.lstrip().startswith(("{", "[")):
            checked = answer_integrity.validate_plain_text(answer_text)
            return checked if (checked or "").strip() else CHAT_UNAVAILABLE_REPLY
        return answer_text
    if not isinstance(data, dict):
        return answer_text
    if data.get("format") not in ("source_list", "comparison"):
        # (STEP 2.1 / GATE 2) every other structured answer — trend_report, no_results, not_available, disallowed AND a missing
        # or unrecognised format: temporal claims are validated and the time scope is stated server-side
        if query is None:
            return answer_text
        if time_scope is None:
            time_scope = _TIME_SCOPE.get()
        return json.dumps(answer_integrity.enforce_temporal_claims(
            data, matched_signals, time_scope, generic=data.get("format") not in answer_integrity.TEMPORAL_ONLY_FORMATS),
            ensure_ascii=False)
    if not matched_signals:
        return answer_text
    # (POST RECORDS) removed here whoever wrote it, so no return below can
    # carry it into the saved answer; kept only when signed by this process.
    if _POST_RECORDS_FIELD in data:
        post_records = _take_post_records(data)
        answer_text = json.dumps(data, ensure_ascii=False)
    else:
        post_records = None

    fmt = data.get("format")
    if time_scope is None:
        time_scope = _TIME_SCOPE.get()        # (RECENCY) set by the caller through time_scope_context()
    if strict_intent_mode():
        # Strict: identity must be confirmed; unconfirmed posts are removed.
        return _strict_patch_answer(data, matched_signals, answer_text, query=query, post_records=post_records,
                                    time_scope=time_scope)
    changed = False

    # (POST ENRICHMENT) Index i (1-based) in Claude's answer == i-th entry
    # of this aligned list — same filter/order build_claude_post_context()
    # used to build "[Post i]" in the prompt (all callers pass the same
    # merged_pool to analyze_with_claude() and to this function).
    try:
        aligned = [sig for _post, sig in _aligned_post_context(matched_signals)]
    except Exception:
        aligned = []

    def _set(post, key, value):
        nonlocal changed
        if post.get(key) != value:
            post[key] = value
            changed = True

    def _patch_platform_list(platforms):
        if not isinstance(platforms, list):
            return
        for platform_entry in platforms:
            if not isinstance(platform_entry, dict):
                continue
            posts = platform_entry.get("posts")
            if not isinstance(posts, list):
                continue
            for post in posts:
                if not isinstance(post, dict):
                    continue
                try:
                    match = _resolve_answer_post(post, matched_signals, aligned)
                except Exception:
                    match = None
                if not match:
                    # No confident match: never invent a link/source. Only
                    # replace a placeholder title with a label from Claude's
                    # OWN summary for that same post (display text only).
                    if _is_placeholder_title(post.get("title")) and isinstance(post.get("summary"), str):
                        label = _derive_post_title(None, post["summary"])
                        if label:
                            _set(post, "title", label)
                    continue

                if query is not None:   # the model's own title, for answer_integrity's identity check
                    post["_model_title"] = post.get("title")
                post_url = match.get("post_url")
                if post_url:
                    _set(post, "link", post_url)

                platform = (match.get("platform") or _infer_platform_from_url(post_url) or "")
                if str(platform).strip().lower() == "reddit":
                    sub = _normalize_subreddit(match.get("subreddit")) or _subreddit_from_url(post_url)
                    if sub:
                        _set(post, "source", sub)

                new_title = _derive_post_title(match.get("title"), match.get("post_text"))
                if not new_title and _is_placeholder_title(post.get("title")) and isinstance(post.get("summary"), str):
                    new_title = _derive_post_title(None, post["summary"])
                if new_title:
                    _set(post, "title", new_title)

                # google_rank: only present on Google-side signals of a
                # merged pool (flintel.merge_matched_and_google_results()).
                if match.get("google_rank") is not None:
                    _set(post, "google_rank", match["google_rank"])
                # (GROUNDED ANSWERS) the post's original publication time — only
                # from the matched stored signal, never from the model.
                if timerange.post_published_at(match) is not None:
                    _set(post, "published_at", timerange.post_published_at(match).isoformat())

    if fmt == "source_list":
        _patch_platform_list(data.get("platforms"))
    elif fmt == "comparison":
        subjects = data.get("subjects")
        if isinstance(subjects, list):
            for subject in subjects:
                if isinstance(subject, dict):
                    _patch_platform_list(subject.get("platforms"))
    else:
        return answer_text

    # (ANSWER INTEGRITY) Production callers pass the user's query: the listed
    # posts are then classified, filtered, renumbered and counted against the
    # stored posts (answer_integrity.py). Without it, only the patch above runs.
    if query is not None:
        data = answer_integrity.enforce_answer_integrity(data, matched_signals, aligned, query, post_records,
                                                         time_scope=time_scope)
        return json.dumps(data, ensure_ascii=False)

    if not changed:
        return answer_text

    return json.dumps(data, ensure_ascii=False)


def _inject_website_context_into_answer(answer_text: str, website_context: dict) -> str:
    """Purely additive, Python-side post-processing — parses the
    already-generated Claude JSON answer and injects `website_context`
    as a new top-level field at the START of the JSON object (before
    "format"), without depending on any model-side instruction to add
    it. Mirrors the same parse/patch/re-serialize pattern already used
    by _patch_post_urls_into_answer(): best-effort and non-destructive
    — if answer_text isn't valid JSON, or website_context is falsy,
    the original answer_text is returned completely unchanged."""
    if not answer_text or not website_context:
        return answer_text
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict):
        return answer_text
    new_data = {"website_context": _clean_website_context(website_context), **data}
    return json.dumps(new_data, ensure_ascii=False)


def _clean_website_context(ctx):
    """(GATE 2) The model-written website overview may describe the user's site freely, but a sentence that speaks about
    Flintel / Reddit / the stored data may not claim live monitoring, freshness, trends or counts (answer_integrity.
    validate_data_claims). Same shape in, same shape out; anything unexpected is returned unchanged."""
    try:
        if not isinstance(ctx, dict):
            return ctx
        out = dict(ctx)
        if isinstance(out.get("overview"), str):
            out["overview"] = answer_integrity.validate_data_claims(out["overview"])
        if isinstance(out.get("sections"), list):
            secs = []
            for sec in out["sections"]:
                if not isinstance(sec, dict):
                    secs.append(sec)
                    continue
                sec = dict(sec)
                if isinstance(sec.get("title"), str):
                    sec["title"] = answer_integrity.validate_data_claims(sec["title"])
                if isinstance(sec.get("bullets"), list):
                    sec["bullets"] = [b2 for b2 in (answer_integrity.validate_data_claims(b) if isinstance(b, str) else b
                                                    for b in sec["bullets"]) if not (isinstance(b2, str) and not b2.strip())]
                secs.append(sec)
            out["sections"] = secs
        return out
    except Exception:
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
