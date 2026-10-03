#!/usr/bin/env python3
"""
diagnostics/watermark_probe.py  —  READ-ONLY. Run on YOUR machine (needs your own env vars).
==========================================================================================
Decides which field is safe to use as the "new document arrived" watermark for
incremental rescan, WITHOUT guessing. It never imports database.py (that module
runs create_index() at import) and uses only: estimated_document_count,
count_documents, index_information, find, aggregate($sample). No writes.

It never prints connection strings. Credentials come from the environment you
already use for Flintel:  MONGODB_URI, MONGODB2, MONGODB4  (+ MONGODB_DB,
default "flintel_bot"; collection "flintel_signals").

USAGE
    python diagnostics/watermark_probe.py                # all configured clusters
    python diagnostics/watermark_probe.py --sample 800   # bigger sample

WHAT IT REPORTS, PER CLUSTER
  1. indexes that exist on flintel_signals (names + keys only)
  2. type of _id   (ObjectId => _id timestamp is a usable insert-time clock)
  3. which ingestion-timestamp-looking fields exist (and how often)
  4. skew = ObjectId insert time - created_utc  (late-ingested old posts)
  5. LATE-EMBEDDING EVIDENCE: among the newest docs by _id, what fraction has
     NO embedding yet. > 0 means embeddings are attached AFTER insert, so a
     watermark on _id / created_utc would miss them when the embedding lands.
"""
import argparse
import os
import sys
from datetime import timezone

CANDIDATE_TS_FIELDS = [
    "ingested_at", "scraped_at", "inserted_at", "fetched_at", "collected_at",
    "embedded_at", "embedding_created_at", "created_at", "updated_at",
    "date_added", "crawled_at", "saved_at",
]


def _clusters():
    out = []
    for label, var in (("primary", "MONGODB_URI"), ("mongo_2", "MONGODB2"), ("mongo_4", "MONGODB4")):
        uri = os.getenv(var)
        if uri:
            out.append((label, uri))
    return out


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def probe(label, uri, db_name, sample_n):
    from pymongo import MongoClient
    client = MongoClient(uri, serverSelectionTimeoutMS=15000)
    coll = client[db_name]["flintel_signals"]
    print("=" * 78)
    print(f"[{label}] {db_name}.flintel_signals")
    try:
        print("  estimated docs      :", coll.estimated_document_count())
        print("  indexes             :", {k: v.get("key") for k, v in coll.index_information().items()})
    except Exception as exc:                                   # noqa: BLE001
        print("  (could not read counts/indexes):", type(exc).__name__)
        return

    docs = list(coll.aggregate([{"$sample": {"size": sample_n}},
                                {"$project": {"embedding": 0}}], allowDiskUse=False))
    n = len(docs) or 1
    types = {}
    for d in docs:
        t = type(d.get("_id")).__name__
        types[t] = types.get(t, 0) + 1
    print(f"  sample size         : {len(docs)}")
    print("  _id types           :", types)

    presence = {f: sum(1 for d in docs if d.get(f) is not None) for f in CANDIDATE_TS_FIELDS}
    print("  timestamp-like fields present (count of sample):",
          {k: v for k, v in presence.items() if v} or "NONE of " + ", ".join(CANDIDATE_TS_FIELDS))

    skews = []
    for d in docs:
        oid, cu = d.get("_id"), _aware(d.get("created_utc"))
        if hasattr(oid, "generation_time") and cu is not None:
            skews.append((oid.generation_time - cu).total_seconds() / 86400.0)
    if skews:
        skews.sort()
        late1 = sum(1 for s in skews if s > 1) / len(skews)
        late7 = sum(1 for s in skews if s > 7) / len(skews)
        print(f"  insert_time - created_utc (days): median={skews[len(skews)//2]:.2f} "
              f"p90={skews[int(len(skews)*0.9)]:.2f}  late>1d={late1:.0%}  late>7d={late7:.0%}")
        print("    -> a created_utc-only watermark would MISS this share of late-ingested posts")
    else:
        print("  insert_time vs created_utc: n/a (_id is not an ObjectId, or created_utc missing)")

    # Late-embedding evidence: newest docs by _id, how many lack an embedding?
    try:
        newest = list(coll.find({}, {"_id": 1, "embedding": 1}).sort("_id", -1).limit(300))
        if newest:
            lacking = sum(1 for d in newest if not d.get("embedding"))
            print(f"  newest {len(newest)} docs by _id lacking embedding: {lacking} "
                  f"({lacking/len(newest):.0%})")
            if lacking:
                print("    -> embeddings are attached AFTER insert; an _id/created_utc watermark "
                      "alone would miss docs when their embedding arrives")
    except Exception as exc:                                   # noqa: BLE001
        print("  (late-embedding check skipped):", type(exc).__name__)
    client.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=500)
    args = ap.parse_args()
    clusters = _clusters()
    if not clusters:
        print("No MONGODB_URI / MONGODB2 / MONGODB4 in the environment — nothing to probe.")
        return 1
    db_name = os.getenv("MONGODB_DB", "flintel_bot")
    for label, uri in clusters:
        try:
            probe(label, uri, db_name, args.sample)
        except Exception as exc:                               # noqa: BLE001
            print(f"[{label}] probe failed: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
