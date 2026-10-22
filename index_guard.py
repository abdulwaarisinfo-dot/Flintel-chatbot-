"""
index_guard.py - verification of the unique indexes the credit and billing code needs for CORRECTNESS.

Why: search_credits.py and billing.py create their one-document-per-owner records with insert / upsert calls whose
race handling is "a duplicate-key error means somebody else created it first". Without a UNIQUE index on `owner_key` that
error never happens and a lost race silently creates a SECOND document for the owner (split counters, bypassed limits, two
plan records). Calling create_index() and logging a warning when it raises is not enough: the index may be missing for a
reason that raised nothing visible (privileges, a failed build, an index with the wrong options), and nothing consumed a
failure. This module VERIFIES the result (index_information()) and records a state per collection:

    ok            a single-field unique index on the key exists (not partial)
    not_required  the subsystem is switched off (credit kill switch / billing not configured): nothing is checked
    missing       no index on the key
    not_unique    an index on the key exists but is not unique
    duplicates    the key already has duplicate values: the unique index cannot be built until an OPERATOR resolves them.
                  Duplicates are only REPORTED (count + hashed samples, never the raw keys); they are NEVER merged or deleted here.
    unverifiable  the server could not be asked (re-checked later)

search_credits.py consults check() before every credit operation that can create a charge, a document or an entitlement
(see _require_index there) and refuses with CreditStoreError ("unavailable") while the state is not ok. Settlement, refund,
recovery and read paths are not gated, so the app stays up for diagnostics and recovery. A refused state is re-verified lazily
(30 s; 5 min for duplicates), so creating the index (or removing the duplicates) heals the subsystem without a restart.

Performance-only indexes (e.g. a future flintel_guest_gate {handover.to}) are deliberately NOT part of this module.
A name that was never registered is "unmanaged" and always passes (unit tests, scripts).
"""
import hashlib
import logging
import threading
import time

log = logging.getLogger("flintel.index_guard")

REFRESH_SECONDS = 30
REFRESH_DUPLICATES_SECONDS = 300
OK_STATES = ("ok", "not_required", "unmanaged")
_DUPLICATE_SCAN_MAX_MS = 5000
_DUPLICATE_SAMPLE = 20

_lock = threading.RLock()
_registry = {}        # name -> {"coll", "field", "required", "state", "detail", "checked_at", "refreshing"}


def _now() -> float:
    return time.monotonic()


def _digest(value) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]


def verify_unique_index(coll, field="owner_key"):
    """-> (state, detail). Read-only: index_information() and, only when the index is not ok, a bounded duplicate scan."""
    try:
        info = coll.index_information()
    except Exception as exc:
        return "unverifiable", {"error": type(exc).__name__}
    single = []
    try:
        for spec in (info or {}).values():
            key = spec.get("key") or []
            if len(key) == 1 and tuple(key[0])[0] == field:
                single.append((spec, tuple(key[0])[1] in (1, -1)))      # hashed / text / geo keys can never be unique here
    except Exception:
        return "unverifiable", {"error": "malformed_index_information"}
    for spec, ordered in single:
        if ordered and spec.get("unique") is True and not spec.get("partialFilterExpression"):
            return "ok", {}
    state = "not_unique" if single else "missing"
    try:
        dups = list(coll.aggregate(
            [{"$group": {"_id": "$" + field, "n": {"$sum": 1}}}, {"$match": {"n": {"$gt": 1}}}, {"$limit": _DUPLICATE_SAMPLE + 1}],
            allowDiskUse=True, maxTimeMS=_DUPLICATE_SCAN_MAX_MS))
    except Exception as exc:
        return state, {"duplicate_scan_error": type(exc).__name__}
    if dups:
        shown = dups[:_DUPLICATE_SAMPLE]
        return "duplicates", {"duplicate_keys": len(shown), "more": len(dups) > _DUPLICATE_SAMPLE,
                              "samples": [_digest(d.get("_id")) for d in shown]}
    return state, {}


def _apply(ent, state, detail, name):
    previous = ent.get("state")
    ent.update(state=state, detail=detail, checked_at=_now())
    if state in OK_STATES:
        if previous not in (None, *OK_STATES):
            log.warning(f"index_guard: {name} is verified again ({state}); the subsystem is available")
        return
    if state != previous:
        log.error(f"index_guard: the required UNIQUE index on {name} is not available (state={state}, detail={detail}). "
                  f"Credit/billing operations that could create a document, charge or entitlement are REFUSED until it exists; "
                  f"reads, settlement, refunds and recovery still work. Duplicates (if any) are never merged automatically: "
                  f"an operator must resolve them, then create the unique index on owner_key.")


def register(name, coll, field="owner_key", required=True):
    """Verify now and remember the collection so check() can re-verify later. Never raises (startup must not fail)."""
    ent = {"coll": coll, "field": field, "required": bool(required), "state": None, "detail": {}, "checked_at": 0.0,
           "refreshing": False}
    try:
        if not required:
            _apply(ent, "not_required", {}, name)
        else:
            state, detail = verify_unique_index(coll, field)
            _apply(ent, state, detail, name)
    except Exception as exc:                                  # defensive: never break startup
        ent.update(state="unverifiable", detail={"error": type(exc).__name__}, checked_at=_now())
    with _lock:
        _registry[name] = ent
    return ent["state"]


def _due(ent) -> bool:
    wait = REFRESH_DUPLICATES_SECONDS if ent["state"] == "duplicates" else REFRESH_SECONDS
    return _now() - ent["checked_at"] >= wait


def check(name):
    """-> (ok, state). Unregistered -> (True, "unmanaged"). A refused state is re-verified lazily (one thread at a time)."""
    with _lock:
        ent = _registry.get(name)
        if ent is None:
            return True, "unmanaged"
        if ent["state"] in OK_STATES:
            return True, ent["state"]
        refresh = _due(ent) and not ent["refreshing"]
        if refresh:
            ent["refreshing"] = True
    if refresh:
        try:
            state, detail = verify_unique_index(ent["coll"], ent["field"])
            with _lock:
                _apply(ent, state, detail, name)
        except Exception:
            with _lock:
                ent["checked_at"] = _now()
        finally:
            with _lock:
                ent["refreshing"] = False
    with _lock:
        return ent["state"] in OK_STATES, ent["state"]


def is_registered(name) -> bool:
    with _lock:
        return name in _registry


def snapshot():
    """{name: state} after giving every refused entry its chance to re-verify. No keys, no counts, nothing sensitive."""
    with _lock:
        names = list(_registry)
    return {name: check(name)[1] for name in names}


def detail(name) -> dict:
    with _lock:
        ent = _registry.get(name)
        return dict(ent["detail"]) if ent else {}


def reset():
    """Forget every registration (tests only)."""
    with _lock:
        _registry.clear()
