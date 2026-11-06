#!/usr/bin/env python3
"""Synthetic staging seed. Inserts clearly-labelled FAKE discussions into the staging database's `flintel_signals` collection so retrieval,
deduplication and classification can be exercised. Nothing here is real: no real user, post, URL or customer information is used, and no
production data is read or copied.

Safety properties (each is covered by tests/test_staging_seed.py):
  * DRY-RUN is the default: it prints the plan and opens NO connection. Writing needs --apply.
  * --apply is refused unless the isolation preflight verdict is PASSED (FLINTEL_ENV=staging, production hosts/secrets excluded, ...) AND the
    operator re-types the target database name with --confirm-db, matching MONGODB_DB exactly.
  * Inserts only, via $setOnInsert upserts keyed on a deterministic message_id: re-running never duplicates and NEVER overwrites an existing
    record. There is no delete, drop, update-in-place or index code in this file.
  * Bounded: at most --max-records (default 60, hard ceiling 500). It uses plain pymongo (the application is not imported), so it creates no indexes.
  * ONE RUN AT A TIME. Without a unique index on message_id (this tool never creates one), two simultaneous upserts of the same record can both
    insert. A database-level lock would need a write to a marker document, another collection or an index, all forbidden here, so instead:
    (1) --apply requires --i-confirm-single-run (you attest that no other seed process is running against this database, on any machine);
    (2) an exclusive lock file on THIS machine refuses a second local process (non-blocking: the second one exits 2 at once);
    (3) after writing, a read-only check reports any message_id that exists more than once (exit 1). Duplicates are reported, never deleted.
  * Every document carries synthetic=true, _seed="flintel-staging-seed-v1", message_id "stg-seed-*", username "synthetic_user_*" and a URL on the
    reserved `.invalid` TLD, so it can never be mistaken for a real discussion.
  * No embeddings by default. --synthetic-vectors adds FREE deterministic placeholder vectors (labelled; they do not match real query embeddings).
    --embed + --i-authorize-paid-embeddings generates REAL embeddings through OpenAI (paid, metered): it needs the paid-phase preflight, is
    never implied by any other flag, and has not been run by the author.
Exit codes: 0 ok, 1 error / partial failure, 2 refused (safety gate), 3 bad arguments.
"""
import argparse
import contextlib
import hashlib
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SEED_TAG = "flintel-staging-seed-v1"
HARD_CEILING = 500
DEFAULT_MAX = 60
VECTOR_DIM = 1536
HOST = "synthetic.flintel-staging.invalid"            # reserved TLD: never resolves, never a real site
TOPICS = ["invoicing", "appointment scheduling", "customer onboarding", "expense reporting", "support ticketing", "inventory tracking"]

# (category, intended behaviour, templates). {t} = topic. Text is generic and synthetic.
CATEGORIES = [
    ("direct_demand", "explicit request to obtain a product", 8, [
        "I need a tool that can handle {t} for a five person team. Budget is around $40 a month, any recommendations?",
        "We are looking for software for {t}. Must integrate with our accounting system. Who can we hire or buy from?",
        "Looking for a recommendation for {t} software, budget $50. Needs a free trial."]),
    ("product_request", "asks whether a product exists", 6, [
        "Is there a simple app for {t} that works offline? Willing to pay for a good one.",
        "Does anyone know a service for {t}? We want to replace our spreadsheet."]),
    ("problem", "frustration without an explicit purchase request", 8, [
        "Our current {t} process keeps breaking and wastes hours every week. So frustrating.",
        "My {t} software is too expensive and too slow. I hate how complicated it is."]),
    ("competitor_comparison", "compares or switches products", 6, [
        "I switched from Product A to Product B for {t} because the pricing was better.",
        "Product A vs Product B for {t}: here is how they compare after a month of use."]),
    ("market_discussion", "industry commentary, no intent", 6, [
        "Generic thoughts on how {t} tools are changing this year and what vendors are doing.",
        "A survey of how small companies approach {t} and why adoption is uneven."]),
    ("question_no_intent", "a question that is not purchase intent", 6, [
        "What is the best way to learn how {t} works in a small company?",
        "Is anyone else curious how {t} is usually handled? Just wondering."]),
    ("ambiguous", "commercial intent unclear", 4, [
        "Thinking about whether {t} is worth automating at some point, not sure yet.",
        "We might look at {t} options next year, maybe. Still deciding."]),
]
# Special records exercising the edges (appended after the categories).
SPECIALS = ["duplicate_pair", "title_only", "empty_text", "undated"]


def _vector(seed):
    """Deterministic placeholder vector (free). NOT a real embedding."""
    out, i = [], 0
    while len(out) < VECTOR_DIM:
        h = hashlib.sha256(f"{seed}:{i}".encode()).digest()
        out.extend((b - 127.5) / 127.5 for b in h)
        i += 1
    v = out[:VECTOR_DIM]
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [round(x / n, 6) for x in v]


def build_records(max_records=DEFAULT_MAX, now=None):
    """Deterministic list of synthetic documents (same input -> same message_ids). Never random, never reads any database."""
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    recs, n = [], 0

    def base(i, category, platform, title, text, days_ago, url=None):
        plat = platform
        return {
            "message_id": f"stg-seed-{i:04d}", "platform": plat, "title": title, "text": text,
            "post_url": url or f"https://{HOST}/{plat}/{i:04d}", "username": f"synthetic_user_{i:04d}", "score": (i * 7) % 50,
            "num_comments": (i * 3) % 20, "created_utc": now - timedelta(days=days_ago, hours=(i * 5) % 24), "fetched_at": now,
            "search_keyword": "synthetic",
            "synthetic": True, "_seed": SEED_TAG, "_seed_category": category,
        }
    for cat, _why, count, templates in CATEGORIES:
        for k in range(count):
            n += 1
            topic = TOPICS[(n + k) % len(TOPICS)]
            text = templates[k % len(templates)].format(t=topic)
            plat = "reddit" if n % 2 else "hackernews"
            recs.append(base(n, cat, plat, text[:70].rstrip(" ,.") , text, days_ago=(n * 3) % 80))
    # edge records
    n += 1; u = f"https://{HOST}/hackernews/dup-{n:04d}"
    recs.append(base(n, "duplicate_pair", "hackernews", "Synthetic duplicate A", "I need a tool for invoicing. Budget is $30.", 2, url=u))
    n += 1
    recs.append(base(n, "duplicate_pair", "hackernews", "Synthetic duplicate B", "I need a tool for invoicing. Budget is $30.", 2, url=u + "?utm_source=synthetic"))
    n += 1
    recs.append(base(n, "title_only", "reddit", "Synthetic title only: looking for a scheduling app", "", 5))
    recs[-1]["text"] = recs[-1]["title"]
    n += 1
    recs.append(base(n, "empty_text", "reddit", "Synthetic empty body", "", 6))
    n += 1
    d = base(n, "undated", "hackernews", "Synthetic undated post", "Question about invoicing, no publication time stored.", 1)
    d.pop("created_utc")
    recs.append(d)
    return recs[: max(0, int(max_records))]


def seed(coll, docs, apply):
    """Idempotent insert-only write. Returns a summary dict. Never overwrites, never deletes. On the first failure it stops and reports."""
    summary = {"planned": len(docs), "inserted": 0, "already_present": 0, "failed": 0, "applied": bool(apply), "error": None}
    if not apply:
        return summary
    for d in docs:
        try:
            res = coll.update_one({"message_id": d["message_id"]}, {"$setOnInsert": d}, upsert=True)
        except Exception as exc:                          # stop at the first failure; safe to re-run (idempotent)
            summary["failed"] += 1
            summary["error"] = type(exc).__name__
            break
        if getattr(res, "upserted_id", None) is not None:
            summary["inserted"] += 1
        else:
            summary["already_present"] += 1
    return summary


def openai_embedder(api_key, model, http_post=None):
    """REAL, PAID embeddings (OpenAI). Bounded: batches of 50, 30 s timeout. Not run by the author; tests inject a fake."""
    def embed(texts):
        import httpx
        post = http_post or httpx.post
        out = []
        for i in range(0, len(texts), 50):
            r = post("https://api.openai.com/v1/embeddings", headers={"Authorization": f"Bearer {api_key}"},
                     json={"model": model, "input": [t[:6000] or " " for t in texts[i:i + 50]]}, timeout=30)
            r.raise_for_status()
            out.extend(x["embedding"] for x in r.json()["data"])
        return out
    return embed


def attach_embeddings(docs, mode, embedder=None):
    """mode: None | "synthetic" | "real". Documents without text get none (nothing to embed)."""
    for d in docs:
        d.pop("embedding", None)
    if mode == "synthetic":
        for d in docs:
            d["embedding"] = _vector(d["message_id"])
            d["embedding_synthetic"] = True
    elif mode == "real":
        todo = [d for d in docs if (d.get("text") or "").strip()]
        for d, v in zip(todo, embedder([d["text"] for d in todo])):
            d["embedding"] = v
            d["embedding_provider"] = "openai"
    return docs


@contextlib.contextmanager
def single_run_lock(db_name, uri):
    """Exclusive, non-blocking, machine-local lock for ONE target database. Yields True when held, False when another process holds it (or locking is
    unavailable on this platform). The key is a hash of the database name and the credential-free host list, never of a password; the lock file lives in
    the system temp directory and holds no data. It protects against two processes on one machine only; the --i-confirm-single-run attestation
    covers other machines."""
    try:
        import fcntl
    except ImportError:                                           # e.g. Windows: no safe lock available -> refuse rather than pretend
        yield False
        return
    from tools import staging_preflight as pf
    key = hashlib.sha256(f"{db_name}|{pf.endpoint_text(uri).split('/', 1)[0]}".encode()).hexdigest()[:16]
    path = os.path.join(tempfile.gettempdir(), f"flintel-staging-seed-{key}.lock")
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


def find_duplicates(coll):
    """READ-ONLY: message_ids of seed records present more than once. Never deletes or repairs anything."""
    pipeline = [{"$match": {"_seed": SEED_TAG}}, {"$group": {"_id": "$message_id", "n": {"$sum": 1}}}, {"$match": {"n": {"$gt": 1}}}]
    return sorted(str(r["_id"]) for r in coll.aggregate(pipeline))


def main(argv=None, env=None, client_factory=None):
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write to the database (default is a dry run that opens no connection)")
    ap.add_argument("--confirm-db", help="re-type the target database name (must equal MONGODB_DB)")
    ap.add_argument("--max-records", type=int, default=DEFAULT_MAX)
    ap.add_argument("--synthetic-vectors", action="store_true", help="free deterministic placeholder embeddings (labelled; do not match real query embeddings)")
    ap.add_argument("--embed", action="store_true", help="REAL embeddings through OpenAI: PAID")
    ap.add_argument("--i-authorize-paid-embeddings", action="store_true")
    ap.add_argument("--i-confirm-single-run", action="store_true",
                    help="REQUIRED with --apply: you confirm no other seed process is running against this database anywhere (no unique index protects against two)")
    a = ap.parse_args(argv)
    if not 1 <= a.max_records <= HARD_CEILING:
        print(f"--max-records must be 1..{HARD_CEILING}")
        return 3
    if a.embed and a.synthetic_vectors:
        print("choose --synthetic-vectors or --embed, not both")
        return 3
    if a.embed and not a.i_authorize_paid_embeddings:
        print("--embed is PAID: it also requires --i-authorize-paid-embeddings (and your separate authorisation)")
        return 2
    docs = build_records(a.max_records)
    mode = "real" if a.embed else "synthetic" if a.synthetic_vectors else None
    by_cat = {}
    for d in docs:
        by_cat[d["_seed_category"]] = by_cat.get(d["_seed_category"], 0) + 1
    print(f"plan: {len(docs)} synthetic records {by_cat}; embeddings: {mode or 'none'}; mode: {'APPLY' if a.apply else 'DRY RUN (no connection opened)'}")
    if not a.apply:
        if a.embed:
            print("dry run: no embedding call is made")
        return 0
    # ---- write gate: isolation proven, and the database name re-typed
    from tools import staging_preflight as pf
    phase = "paid" if a.embed else "isolation"
    rows = pf.run(env, phase)
    v = pf.verdict(rows, phase)
    if v != "PASSED":
        print(f"refused: preflight (phase={phase}) verdict {v}:", *[x["name"] for x in rows if x["status"] != "PASS" and not (phase == "isolation" and x["phase"] == "paid")], sep="\n  ")
        return 2
    dbname = env.get("MONGODB_DB") or ""
    if not dbname or a.confirm_db != dbname:
        print("refused: --confirm-db must exactly equal MONGODB_DB (the database that will be written)")
        return 2
    if not a.i_confirm_single_run:
        print("refused: --apply also requires --i-confirm-single-run (only one seed process may run against a database at a time; no index prevents duplicates)")
        return 2
    with single_run_lock(dbname, env.get("MONGODB_URI", "")) as held:
        if not held:
            print("refused: another seed process holds the lock for this database on this machine (or locking is unavailable here); run one at a time")
            return 2
        return _write(a, env, docs, mode, dbname, client_factory)


def _write(a, env, docs, mode, dbname, client_factory):
    try:
        if mode == "real":
            docs = attach_embeddings(docs, "real", openai_embedder(env["OPENAI_API_KEY"], env.get("EMBEDDING_MODEL", "text-embedding-3-small")))
        else:
            docs = attach_embeddings(docs, mode)
    except Exception as exc:
        print("embedding failed; nothing was written:", type(exc).__name__)
        return 1
    if client_factory is None:
        from pymongo import MongoClient

        def client_factory(uri):
            return MongoClient(uri, serverSelectionTimeoutMS=8000, connectTimeoutMS=8000)
    try:
        coll = client_factory(env["MONGODB_URI"])[dbname]["flintel_signals"]
        summary = seed(coll, docs, True)
    except Exception as exc:
        print("database error; nothing further was attempted:", type(exc).__name__)
        return 1
    print(f"done: inserted={summary['inserted']} already_present={summary['already_present']} failed={summary['failed']}"
          + (f" error={summary['error']}" if summary["error"] else ""))
    try:                                                                      # read-only verification that no record was duplicated
        dups = find_duplicates(coll)
    except Exception as exc:
        print("duplicate check could not run (UNKNOWN):", type(exc).__name__)
        return 1
    if dups:
        print(f"DUPLICATES FOUND for {len(dups)} message_id(s) (e.g. {dups[0]}): two seed runs probably overlapped. Nothing was deleted; resolve manually.")
        return 1
    print("duplicate check: none")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
