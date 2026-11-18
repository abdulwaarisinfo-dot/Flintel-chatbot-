"""user_indexes.py - the one-time-safe index setup for flintel_users (kept out of database.py so it can be unit-tested)."""
import logging

log = logging.getLogger("flintel.database")


def ensure_google_id_index(coll):
    """Uniqueness for REAL Google ids only. The legacy `google_id_1` index (unique + sparse) also indexes an explicit null, so a
    second email/password user (google_id: null) raised E11000. It is replaced by a PARTIAL unique index that only covers string
    values. Only an index is dropped - never a document - and no user data is read or modified. If the legacy index is dropped
    and the new one cannot be built (e.g. real duplicate google_ids exist), startup continues and the failure is logged loudly.
    Idempotent: when the partial index already exists nothing is created and the legacy name is never recreated."""
    name, partial = "google_id_unique_partial", {"google_id": {"$type": "string"}}
    try:
        info = coll.index_information()
        if (info.get(name) or {}).get("partialFilterExpression") == partial:
            legacy = info.get("google_id_1")
            if legacy and not legacy.get("partialFilterExpression"):
                coll.drop_index("google_id_1")
            return True
        legacy = info.get("google_id_1")
        if legacy and not legacy.get("partialFilterExpression"):
            coll.drop_index("google_id_1")
            log.warning("Dropped the legacy unique+sparse users.google_id_1 index (it rejected a second null google_id); replacing it with a partial unique index.")
        coll.create_index([("google_id", 1)], name=name, unique=True, partialFilterExpression=partial)
        return True
    except Exception as exc:
        log.error(f"Could not ensure the partial unique users.google_id index ({type(exc).__name__}: {exc}). "
                  "Email sign-ups still work; Google-id uniqueness is NOT enforced until it exists.")
        return False
