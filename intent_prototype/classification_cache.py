"""
INTENT CLASSIFICATION CACHE
============================================================================
Har post ki intent classification ko post_url par cache karta hai, taake
top-up ya dobara search par Claude dobara na chale.

Public API:
    get_many(post_urls) -> {post_url: classification_dict}
    save_many(items)    -> None      # items = [(post_url, classification_dict), ...]

Rules:
  * INTENT_CACHE_ENABLED False ho to get_many() {} deta hai aur save_many()
    kuch nahi karta.
  * FAIL-SAFE: har Mongo read/write apne try/except mein hai. Koi bhi error
    ho to get_many() {} deta hai aur save_many() chupke se guzar jata hai.
    Cache kabhi search ko fail nahi karta.
  * Har entry ke saath schema_version save hota hai. Taxonomy badle to
    purani entries (version mismatch) ignore hoti hain.
  * Naya Mongo connection nahi kholta — database.py ka maujooda client
    use karta hai (sirf _get_collection() mein, ek hi jagah).
  * Document _id = post_url, is liye alag unique index ki zaroorat nahi.
"""

import logging
from datetime import datetime, timedelta

from config import (
    INTENT_CACHE_ENABLED,
    INTENT_CACHE_TTL_DAYS,
    INTENT_CACHE_COLLECTION,
)

log = logging.getLogger(__name__)

# Schema/taxonomy version. schemas.py mein INTENT_SCHEMA_VERSION ya
# SCHEMA_VERSION ho to wahi use hota hai, warna yeh fallback constant.
_FALLBACK_SCHEMA_VERSION = "v1"
try:
    from . import schemas as _schemas  # type: ignore
except Exception:
    try:
        import schemas as _schemas  # type: ignore
    except Exception:
        _schemas = None

SCHEMA_VERSION = str(
    getattr(_schemas, "INTENT_SCHEMA_VERSION", None)
    or getattr(_schemas, "SCHEMA_VERSION", None)
    or _FALLBACK_SCHEMA_VERSION
)

_GET_MANY_BATCH = 500

_collection = None          # cached collection handle
_index_ready = False        # TTL index sirf ek baar banao


def _utcnow() -> datetime:
    # Mongo naive UTC datetimes wapas deta hai, is liye naive UTC use karo.
    return datetime.utcnow()


def _get_collection():
    """
    database.py ke maujooda client se collection lo. Yeh woh SINGLE jagah
    hai jahan database.py ke API ko adapt karna pad sakta hai.
    Collection MONGODB3 (jobs/users/chats wala DB) mein rehni chahiye.
    """
    global _collection
    if _collection is not None:
        return _collection

    import database  # maujooda module — naya connection nahi

    name = INTENT_CACHE_COLLECTION
    col = None
    for attr in ("get_collection", "get_tertiary_collection", "get_db3_collection"):
        fn = getattr(database, attr, None)
        if callable(fn):
            col = fn(name)
            break
    if col is None:
        for attr in ("db3", "db", "tertiary_db", "database"):
            handle = getattr(database, attr, None)
            if handle is not None:
                col = handle[name]
                break
    if col is None:
        raise RuntimeError("database.py se collection access nahi mil saka")

    _collection = col
    return col


def _ensure_ttl_index(col) -> None:
    """created_at par TTL index (INTENT_CACHE_TTL_DAYS). Best-effort."""
    global _index_ready
    if _index_ready:
        return
    try:
        if INTENT_CACHE_TTL_DAYS > 0:
            ttl_seconds = int(INTENT_CACHE_TTL_DAYS) * 86400
            try:
                col.create_index("created_at", expireAfterSeconds=ttl_seconds,
                                 name="created_at_ttl")
            except Exception:
                # Index pehle se alag TTL ke saath ho sakta hai — collMod try karo.
                col.database.command(
                    "collMod", col.name,
                    index={"name": "created_at_ttl",
                           "expireAfterSeconds": ttl_seconds},
                )
        _index_ready = True
    except Exception as e:
        log.warning("intent cache: TTL index setup failed: %s", e)
        # _index_ready False hi rehne do; agli call dobara try karegi.


def get_many(post_urls) -> dict:
    """Cache mein maujood, expire na hui, current-schema classifications."""
    if not INTENT_CACHE_ENABLED or not post_urls:
        return {}
    try:
        urls = [u for u in dict.fromkeys(post_urls) if u]
        if not urls:
            return {}
        col = _get_collection()
        _ensure_ttl_index(col)

        query_extra = {"schema_version": SCHEMA_VERSION}
        if INTENT_CACHE_TTL_DAYS > 0:
            # TTL monitor lag karta hai, is liye read par bhi expiry check.
            query_extra["created_at"] = {
                "$gte": _utcnow() - timedelta(days=int(INTENT_CACHE_TTL_DAYS))
            }

        out = {}
        for i in range(0, len(urls), _GET_MANY_BATCH):
            batch = urls[i:i + _GET_MANY_BATCH]
            q = {"_id": {"$in": batch}}
            q.update(query_extra)
            for doc in col.find(q, {"classification": 1}):
                cls = doc.get("classification")
                if isinstance(cls, dict):
                    out[doc["_id"]] = cls
        return out
    except Exception as e:
        log.warning("intent cache: get_many failed (ignored): %s", e)
        return {}


def save_many(items) -> None:
    """items = [(post_url, classification_dict), ...] — upsert, created_at ke saath."""
    if not INTENT_CACHE_ENABLED or not items:
        return
    try:
        from pymongo import UpdateOne

        now = _utcnow()
        ops = []
        for post_url, classification in items:
            if not post_url or not isinstance(classification, dict):
                continue
            ops.append(UpdateOne(
                {"_id": post_url},
                {"$set": {
                    "post_url": post_url,
                    "classification": classification,
                    "schema_version": SCHEMA_VERSION,
                    "created_at": now,
                }},
                upsert=True,
            ))
        if not ops:
            return
        col = _get_collection()
        _ensure_ttl_index(col)
        col.bulk_write(ops, ordered=False)
    except Exception as e:
        log.warning("intent cache: save_many failed (ignored): %s", e)
