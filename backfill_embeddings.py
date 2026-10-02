#!/usr/bin/env python3
"""
backfill_embeddings.py
======================
Standalone CLI tool to identify and backfill missing embedding vectors
in Flintel signal collections.

Usage
-----
  python backfill_embeddings.py --report
      Print a count of docs missing embeddings in each collection.
      No writes.

  python backfill_embeddings.py --apply [--collection {primary,2,4,all}] [--limit N]
      Generate and write embeddings for docs that are missing them.
      Default collection: all.  Default limit: 0 (no limit).

  python backfill_embeddings.py --dry-run [--collection {primary,2,4,all}] [--limit N]
      Same as --apply but prints what would be written without touching Mongo.

Flags
-----
  --report              Count and print missing-embedding docs per collection.
  --apply               Generate + write embeddings to Mongo.
  --dry-run             Simulate --apply without writing.
  --collection NAME     Which collection: primary | 2 | 4 | all  (default: all)
  --limit N             Process at most N docs per collection (0 = unlimited).

Notes
-----
- Uses OPENAI_API_KEY and MONGODB_URI / MONGODB_DB from the environment
  (or from a .env file in the same directory).
- Embedding generation uses generate_query_embeddings_batch() from logics.py.
- Docs whose post_text is shorter than LAZY_EMBED_MIN_TEXT_CHARS are skipped
  (same rule as _lazy_backfill_missing_embeddings).
- Writes are bulk_write(ordered=False) with an UpdateOne filter that only
  sets the embedding when none already exists, so the script is safe to run
  concurrently or repeatedly.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# Allow running from any directory
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env if present (python-dotenv optional)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_embeddings")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _missing_filter():
    """MongoDB filter that matches docs with missing/null/empty embedding."""
    return {
        "$or": [
            {"embedding": {"$exists": False}},
            {"embedding": None},
            {"embedding": []},
        ]
    }


def _count_missing(collection, label: str) -> int:
    try:
        n = collection.count_documents(_missing_filter())
        log.info(f"{label}: {n} docs missing embeddings")
        return n
    except Exception as exc:
        log.error(f"{label}: count failed — {exc}")
        return -1


def _fetch_missing(collection, label: str, limit: int) -> list:
    """Fetch docs missing embeddings from *collection*."""
    try:
        cursor = collection.find(
            _missing_filter(),
            {"_id": 1, "post_url": 1, "post_text": 1},
        )
        if limit:
            cursor = cursor.limit(limit)
        docs = list(cursor)
        log.info(f"{label}: fetched {len(docs)} missing-embedding docs")
        return docs
    except Exception as exc:
        log.error(f"{label}: fetch failed — {exc}")
        return []


def _backfill(collection, label: str, docs: list, embed_fn, dry_run: bool,
              min_text_chars: int) -> int:
    """
    Embed *docs* and write vectors back to *collection*.
    Returns the number of docs successfully written (or that would be written).
    """
    from pymongo import UpdateOne

    to_embed = [
        d for d in docs
        if len(d.get("post_text") or "") >= min_text_chars
    ]

    skipped = len(docs) - len(to_embed)
    if skipped:
        log.info(f"{label}: skipped {skipped} docs (text too short)")

    if not to_embed:
        log.info(f"{label}: nothing to embed")
        return 0

    # Embed in batches of 100
    BATCH = 100
    written = 0
    for start in range(0, len(to_embed), BATCH):
        batch = to_embed[start: start + BATCH]
        texts = [d.get("post_text", "") for d in batch]
        try:
            vectors = embed_fn(texts)
        except Exception as exc:
            log.error(f"{label}: embed_fn failed on batch starting {start} — {exc}")
            continue

        if not vectors or len(vectors) != len(batch):
            log.error(f"{label}: embed_fn returned unexpected result for batch {start}")
            continue

        ops = []
        for doc, vec in zip(batch, vectors):
            if not vec:
                continue
            ops.append(UpdateOne(
                {"_id": doc["_id"], "$or": [
                    {"embedding": {"$exists": False}},
                    {"embedding": None},
                    {"embedding": []},
                ]},
                {"$set": {"embedding": vec}},
            ))

        if not ops:
            continue

        if dry_run:
            log.info(f"[DRY-RUN] {label}: would write {len(ops)} embeddings")
            written += len(ops)
        else:
            try:
                result = collection.bulk_write(ops, ordered=False)
                n = result.modified_count
                log.info(f"{label}: wrote {n} embeddings")
                written += n
            except Exception as exc:
                log.error(f"{label}: bulk_write failed — {exc}")

    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(
        description="Backfill missing embedding vectors in Flintel signal collections.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", action="store_true",
                      help="Count missing-embedding docs per collection. No writes.")
    mode.add_argument("--apply", action="store_true",
                      help="Generate and write embeddings.")
    mode.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="Simulate --apply without writing.")
    p.add_argument("--collection", choices=["primary", "2", "4", "all"],
                   default="all",
                   help="Which collection(s) to process (default: all).")
    p.add_argument("--limit", type=int, default=0, metavar="N",
                   help="Max docs per collection (0 = unlimited).")
    return p.parse_args()


def _selected_collections(which: str):
    """Return list of (collection_object, label) pairs."""
    from database import (
        signals_collection,
        signals_collection_2,
        signals_collection_4,
    )
    all_collections = [
        (signals_collection, "signals_collection"),
        (signals_collection_2, "signals_collection_2"),
        (signals_collection_4, "signals_collection_4"),
    ]
    if which == "all":
        return all_collections
    if which == "primary":
        return [all_collections[0]]
    if which == "2":
        return [all_collections[1]]
    if which == "4":
        return [all_collections[2]]
    return all_collections


def main():
    args = _parse_args()

    # Import config values
    try:
        from config import LAZY_EMBED_MIN_TEXT_CHARS
    except ImportError:
        LAZY_EMBED_MIN_TEXT_CHARS = 50

    collections = _selected_collections(args.collection)

    # ---- REPORT mode ----
    if args.report:
        total = 0
        for coll, label in collections:
            n = _count_missing(coll, label)
            if n > 0:
                total += n
        print(f"\nTotal missing: {total}")
        return

    # ---- APPLY / DRY-RUN mode ----
    # Import embed function from logics — avoids duplicating embedding logic.
    try:
        from logics import generate_query_embeddings_batch as embed_fn
    except Exception as exc:
        log.error(f"Failed to import generate_query_embeddings_batch from logics: {exc}")
        sys.exit(1)

    dry_run = args.dry_run
    if dry_run:
        log.info("DRY-RUN mode — no writes will be made")

    total_written = 0
    for coll, label in collections:
        docs = _fetch_missing(coll, label, args.limit)
        if not docs:
            continue
        written = _backfill(
            collection=coll,
            label=label,
            docs=docs,
            embed_fn=embed_fn,
            dry_run=dry_run,
            min_text_chars=LAZY_EMBED_MIN_TEXT_CHARS,
        )
        total_written += written

    action = "would write" if dry_run else "wrote"
    log.info(f"Done — {action} {total_written} embeddings total")


if __name__ == "__main__":
    main()
