#!/usr/bin/env python3
"""
CORPUS PROVENANCE VERIFICATION — read-only, free
===========================================================================
Answers one question with evidence instead of assertion:

    Are the documents in sample.jsonl the same real documents that are in
    the Flintel MongoDB corpus, unmodified?

WHAT IT DOES
  offline   structural audit of sample.jsonl + labels.jsonl alone:
            duplicate ids, embedding dimensions, empty text, field
            coverage, label alignment. No network at all.
  mongo     additionally re-reads each sampled document BACK OUT of
            MongoDB by its stored URL and compares the text byte for byte.
            This is the definitive check.

READ-ONLY GUARANTEE
  Uses find() with a projection, and estimated_document_count(). No
  insert, update, delete, replace, drop, index or bulk call exists in this
  file — validate.py selftest asserts that mechanically across every file
  in this package.

COST
  Zero. No Claude calls. No OpenAI calls. No embeddings computed.

    python intent_prototype/verify_corpus.py offline --diag-dir ./diag
    python intent_prototype/verify_corpus.py mongo   --diag-dir ./diag
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intent_prototype import schemas                                   # noqa: E402

MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB2 = os.getenv("MONGODB2", "")
MONGODB4 = os.getenv("MONGODB4", "")
MONGODB_DB = os.getenv("MONGODB_DB", "flintel_bot")
COLLECTION = "flintel_signals"

TEXT_FIELDS = ("post_text", "text", "body", "content", "selftext")
TITLE_FIELDS = ("title", "post_title", "headline", "name")
URL_FIELDS = ("post_url", "url", "link", "permalink")

# The seed terms embedding_diagnostic.py used to bias half its sample.
# Reproduced here so the topic-bias measurement below is exact rather than
# a guess about what the sampler did.
SEED_TERMS = [
    "ai agent", "ai agents", "chatbot", "automation", "hubspot", "crm",
    "shopify", "openai", "whatsapp", "hiring", "designer", "developer",
    "salesforce", "alternative", "saas", "agency", "freelance",
]


def _log(m=""):
    print(m, flush=True)


def _read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def _first_present(doc, keys):
    for k in keys:
        v = doc.get(k)
        if v not in (None, ""):
            return v
    return None


def _load(diag_dir):
    sp = os.path.join(diag_dir, "sample.jsonl")
    lp = os.path.join(diag_dir, "labels.jsonl")
    if not os.path.exists(sp):
        sys.exit(f"FATAL: {sp} not found")
    docs = _read_jsonl(sp)
    labels = _read_jsonl(lp) if os.path.exists(lp) else []
    return docs, labels


# ═══════════════════════════════════════════════════════════════════════
# OFFLINE — structural audit, no network
# ═══════════════════════════════════════════════════════════════════════

def cmd_offline(args):
    docs, labels = _load(args.diag_dir)

    _log("=" * 74)
    _log("CORPUS AUDIT — offline (no network, no cost)")
    _log("=" * 74)
    _log(f"  sample.jsonl rows : {len(docs):,}")
    _log(f"  labels.jsonl rows : {len(labels):,}")
    _log("")

    # ── identity ──────────────────────────────────────────────────────
    ids = [d.get("id") for d in docs]
    dupes = {i: c for i, c in Counter(ids).items() if c > 1}
    _log("IDENTITY")
    _log(f"  unique ids        : {len(set(ids)):,} of {len(ids):,}")
    if dupes:
        _log(f"  ** {len(dupes)} id(s) appear more than once **")
        _log("     The three clusters mirror each other, so the same post can be")
        _log("     sampled from more than one. Downstream tools key by id, so a")
        _log("     duplicate collapses to a single document — the effective corpus")
        _log(f"     is {len(set(ids)):,}, not {len(ids):,}.")
        for i, c in list(dupes.items())[:5]:
            _log(f"       {c}x  {i[:80]}")
    else:
        _log("  no duplicate ids — each sampled document is distinct")

    synthetic = [i for i in ids if isinstance(i, str) and not i.startswith("http")]
    if synthetic:
        _log(f"  {len(synthetic)} id(s) are hash fallbacks, not URLs "
             f"(the document had no url field)")
        _log("     These cannot be looked up again in Mongo by URL.")
    _log("")

    # ── per source ────────────────────────────────────────────────────
    _log("SOURCE")
    for src, n in Counter(d.get("source") for d in docs).most_common():
        _log(f"  {src:<18} {n:>5}")
    _log("")

    # ── embeddings ────────────────────────────────────────────────────
    dims = Counter(len(d["embedding"]) for d in docs if isinstance(d.get("embedding"), list))
    missing_emb = sum(1 for d in docs if not isinstance(d.get("embedding"), list))
    _log("EMBEDDINGS")
    for dim, n in dims.most_common():
        _log(f"  {dim} dims{'':<10} {n:>5}")
    if missing_emb:
        _log(f"  ** {missing_emb} document(s) carry no embedding **")
    if len(dims) > 1:
        _log("  ** mixed dimensions — these cannot all come from one model **")
    _log("")

    # ── text integrity ────────────────────────────────────────────────
    empty_text = [d["id"] for d in docs if not (d.get("post_text") or "").strip()]
    no_title = sum(1 for d in docs if not (d.get("title") or "").strip())
    no_url = sum(1 for d in docs if not (d.get("post_url") or "").strip())
    no_date = sum(1 for d in docs if not d.get("created_utc"))
    lens = sorted(len(d.get("post_text") or "") for d in docs)
    _log("TEXT")
    _log(f"  empty post_text   : {len(empty_text)}")
    _log(f"  missing title     : {no_title}")
    _log(f"  missing url       : {no_url}")
    _log(f"  missing date      : {no_date}")
    if lens:
        _log(f"  length  min {lens[0]}  p50 {lens[len(lens)//2]}  "
             f"p90 {lens[int(len(lens)*0.9)]}  max {lens[-1]}")
        tiny = sum(1 for n in lens if n < 40)
        if tiny:
            _log(f"  {tiny} document(s) under 40 characters — intent may not be")
            _log("     recoverable from these at all; expect them labelled ambiguous")
    _log("")

    # ── how topic-biased is this sample, measurably ───────────────────
    hits = 0
    for d in docs:
        blob = f"{d.get('title') or ''} {d.get('post_text') or ''}".lower()
        if any(t in blob for t in SEED_TERMS):
            hits += 1
    _log("SAMPLING BIAS")
    _log(f"  documents containing a seed term : {hits:,} of {len(docs):,} "
         f"({hits / max(1, len(docs)):.0%})")
    _log("  embedding_diagnostic.py drew HALF of each source's sample by matching")
    _log("  those seed terms and half unconditioned. So this set deliberately")
    _log("  over-represents the probe topics.")
    _log("")
    _log("  CONSEQUENCE: this is a valid set for measuring PER-CLASS accuracy, and")
    _log("  it is NOT a basis for estimating how common any intent is in the real")
    _log("  corpus. The label distribution here is the distribution of a biased")
    _log("  1-2% slice, not of Flintel's data.")
    _log("")

    # ── label alignment ───────────────────────────────────────────────
    if labels:
        by_id = {d["id"] for d in docs}
        lids = {r.get("id") for r in labels}
        _log("LABELS")
        _log(f"  labels matching a sampled document : {len(lids & by_id):,}")
        orphan = lids - by_id
        unlabeled = by_id - lids
        if orphan:
            _log(f"  ** {len(orphan)} label(s) reference no sampled document **")
        if unlabeled:
            _log(f"  ** {len(unlabeled)} sampled document(s) carry no label **")
        raw = Counter(r.get("intent") for r in labels)
        mapped = Counter(schemas.map_legacy_intent(r.get("intent")) for r in labels)
        _log("")
        _log(f"  distribution under the live {len(schemas.INTENTS)}-intent taxonomy:")
        for intent, n in mapped.most_common():
            _log(f"    {intent:<24} {n:>4}")
        retired = {k: v for k, v in raw.items() if k not in schemas.INTENT_SET}
        if retired:
            _log("")
            _log("  labels written in the diagnostic's vocabulary, remapped:")
            for k, v in retired.items():
                _log(f"    {k:<24} {v:>4}  ->  {schemas.map_legacy_intent(k)}")
    _log("")
    _log("=" * 74)
    _log("NEXT: `verify_corpus.py mongo` re-reads these documents out of MongoDB")
    _log("      and compares the text byte for byte. Read-only and free.")
    _log("=" * 74)


# ═══════════════════════════════════════════════════════════════════════
# MONGO — the definitive check
# ═══════════════════════════════════════════════════════════════════════

def cmd_mongo(args):
    docs, _ = _load(args.diag_dir)

    sources = [(n, u) for n, u in (("mongo_primary", MONGODB_URI),
                                   ("mongo_2", MONGODB2),
                                   ("mongo_4", MONGODB4)) if u]
    if not sources:
        sys.exit("FATAL: no MONGODB_URI / MONGODB2 / MONGODB4 in the environment")

    try:
        from pymongo import MongoClient
    except ImportError:
        sys.exit("FATAL: pymongo not installed —  pip install pymongo")

    _log("=" * 74)
    _log("CORPUS VERIFICATION — re-reading documents from MongoDB (READ-ONLY)")
    _log("=" * 74)
    _log(f"  configured sources: {[n for n, _ in sources]}")
    _log("  operations used   : find() with a projection, "
         "estimated_document_count()")
    _log("  no write operation of any kind is performed")
    _log("")

    by_source = defaultdict(list)
    for d in docs:
        by_source[d.get("source")].append(d)

    projection = {"_id": 0, "embedding": 1, "created_utc": 1,
                  **{f: 1 for f in TEXT_FIELDS},
                  **{f: 1 for f in TITLE_FIELDS},
                  **{f: 1 for f in URL_FIELDS}}

    totals = {}
    checked = matched = text_diff = not_found = skipped = 0
    examples = []

    for label, uri in sources:
        mine = by_source.get(label, [])
        client = None
        try:
            client = MongoClient(uri, serverSelectionTimeoutMS=20000)
            coll = client[MONGODB_DB][COLLECTION]
            totals[label] = coll.estimated_document_count()
            _log(f"  {label}: ~{totals[label]:,} documents in the live collection, "
                 f"{len(mine)} to verify")

            sample = mine[:args.limit] if args.limit else mine
            for d in sample:
                url = d.get("post_url") or ""
                if not url.startswith("http"):
                    skipped += 1
                    continue
                checked += 1
                live = None
                for field in URL_FIELDS:
                    live = coll.find_one({field: url}, projection)
                    if live:
                        break
                if not live:
                    not_found += 1
                    if len(examples) < 5:
                        examples.append(("NOT FOUND", url, "", ""))
                    continue
                live_text = _first_present(live, TEXT_FIELDS) or ""
                if live_text == (d.get("post_text") or ""):
                    matched += 1
                else:
                    text_diff += 1
                    if len(examples) < 5:
                        examples.append(("TEXT DIFFERS", url,
                                         (d.get("post_text") or "")[:90],
                                         str(live_text)[:90]))
        except Exception as exc:                                  # noqa: BLE001
            # No URI is ever printed — only the exception type.
            _log(f"  {label}: UNREACHABLE ({type(exc).__name__})")
        finally:
            if client is not None:
                client.close()

    _log("")
    _log("RESULT")
    _log(f"  documents checked          : {checked:,}")
    _log(f"  found and text IDENTICAL   : {matched:,}")
    _log(f"  found but text DIFFERS     : {text_diff:,}")
    _log(f"  not found in the cluster   : {not_found:,}")
    _log(f"  skipped (no usable url)    : {skipped:,}")
    if totals:
        _log("")
        _log(f"  live corpus total          : ~{sum(totals.values()):,}")
        _log(f"  sampled                    : {len(docs):,} "
             f"({len(docs) / max(1, sum(totals.values())):.2%})")

    if examples:
        _log("")
        _log("  EXAMPLES")
        for kind, url, a, b in examples:
            _log(f"    [{kind}] {url[:70]}")
            if a or b:
                _log(f"       sample.jsonl : {a}")
                _log(f"       live mongo   : {b}")

    _log("")
    if checked and matched == checked:
        _log("  VERDICT: every checked document is present in MongoDB with byte-")
        _log("           identical text. sample.jsonl is a faithful read-only")
        _log("           snapshot of the real Flintel corpus.")
    elif not_found and not text_diff:
        _log("  VERDICT: text matches wherever a document was found, but some are")
        _log("           missing. Either the collection has changed since the")
        _log("           sample was taken, or those documents live in a different")
        _log("           cluster than the one recorded in `source`.")
    elif text_diff:
        _log("  VERDICT: some documents' text has CHANGED since sampling. Labelling")
        _log("           sample.jsonl would label stale content. Re-run the sample")
        _log("           stage before building Tier 1.")
    else:
        _log("  VERDICT: nothing could be verified — see the errors above.")
    _log("=" * 74)


def main():
    ap = argparse.ArgumentParser(description="Corpus provenance verification (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("offline", help="structural audit; no network")
    o.add_argument("--diag-dir", default="./diag")
    o.set_defaults(func=cmd_offline)

    m = sub.add_parser("mongo", help="re-read from MongoDB and compare (read-only)")
    m.add_argument("--diag-dir", default="./diag")
    m.add_argument("--limit", type=int, default=0,
                   help="verify only the first N per source; 0 = all")
    m.set_defaults(func=cmd_mongo)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
