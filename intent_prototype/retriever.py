#!/usr/bin/env python3
"""
RETRIEVER — topic-only candidate generation (spec 8)
===========================================================================
READ-ONLY. Opens MongoDB for find() only. Never calls insert, update,
delete, replace, drop or create_index. Never writes to any cluster.

The embedding does ONE job here: find documents about the right subject.
The diagnostic measured that job at AUC 0.929 on real Flintel data, which
is strong. It does NOT do intent — that is doc_classifier.py's job, and
this module deliberately retrieves WIDE and lets the classifier cut.

Two modes:
  corpus  - score against an already-sampled corpus (sample.jsonl). This
            is what validate.py uses: deterministic, no Mongo, and it
            reuses the exact 866 documents the diagnostic measured, so
            prototype numbers are directly comparable with diagnostic
            numbers.
  mongo   - live read-only retrieval from the three clusters, for the
            end-to-end demo and the latency experiment.

POOL SIZING IS A QUALITY SETTING, NOT A LATENCY LEVER. Spec 8: at least
MIN_CANDIDATE_POOL documents always; TARGET_CANDIDATE_POOL by default. If
the pipeline is slow, the answer is caching or parallelism, never a
smaller pool.
"""

import json
import os

import numpy as np

from . import schemas
from .llm import embed_texts

MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB2 = os.getenv("MONGODB2", "")
MONGODB4 = os.getenv("MONGODB4", "")
MONGODB_DB = os.getenv("MONGODB_DB", "flintel_bot")
MONGO_COLLECTION = os.getenv("FLINTEL_SIGNALS_COLLECTION", "flintel_signals")

TEXT_FIELDS = ("post_text", "text", "body", "content", "selftext")
TITLE_FIELDS = ("title", "post_title", "headline", "name")
URL_FIELDS = ("post_url", "url", "link", "permalink")
PLATFORM_FIELDS = ("platform", "source", "source_platform")


# ═══════════════════════════════════════════════════════════════════════
# SHARED
# ═══════════════════════════════════════════════════════════════════════

def _unit(mat):
    m = np.asarray(mat, dtype=np.float64)
    if m.ndim == 1:
        m = m[None, :]
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def cosine_against(query_vec, doc_vecs):
    """Cosine similarity of one query vector against a matrix of docs."""
    q = _unit(np.asarray(query_vec, dtype=np.float64))
    d = _unit(doc_vecs)
    return (d @ q.T).ravel()


def _first_present(doc, keys):
    for k in keys:
        v = doc.get(k)
        if v not in (None, ""):
            return v
    return None


# ═══════════════════════════════════════════════════════════════════════
# CORPUS MODE (offline, deterministic)
# ═══════════════════════════════════════════════════════════════════════

def load_corpus(path):
    """Load sample.jsonl as produced by embedding_diagnostic.py sample."""
    docs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d.get("embedding"), list) and d["embedding"]:
                docs.append(d)
    if not docs:
        raise ValueError(f"no usable documents in {path}")
    return docs


def corpus_matrix(docs):
    return np.asarray([d["embedding"] for d in docs], dtype=np.float64)


def retrieve_from_corpus(query_intent, docs, matrix=None, query_vec=None,
                         pool=None, sim_floor=None):
    """Score a pre-loaded corpus and return the top candidates.

    Returns (candidates, meta) where candidates is a list of
    (doc, topic_sim) sorted by similarity descending.
    """
    pool = pool or schemas.TARGET_CANDIDATE_POOL
    sim_floor = schemas.LOW_CONFIDENCE_SIM_FLOOR if sim_floor is None else sim_floor

    if matrix is None:
        matrix = corpus_matrix(docs)
    if query_vec is None:
        query_vec = embed_texts([query_intent["topic_embedding_query"]])[0]

    sims = cosine_against(query_vec, matrix)
    order = np.argsort(-sims)

    above = [i for i in order if sims[i] >= sim_floor]
    low_confidence = len(above) < schemas.MIN_CANDIDATE_POOL

    if low_confidence:
        # Spec 8: never return fewer than the floor just because similarity
        # is weak. Take the best MIN_CANDIDATE_POOL and flag it.
        chosen = list(order[:min(schemas.MIN_CANDIDATE_POOL, len(order))])
    else:
        chosen = above[:pool]

    candidates = [(docs[i], float(sims[i])) for i in chosen]
    meta = {
        "mode": "corpus",
        "corpus_size": len(docs),
        "sim_floor": sim_floor,
        "above_floor": len(above),
        "returned": len(candidates),
        "low_confidence_retrieval": low_confidence,
        "top_sim": float(sims[order[0]]) if len(order) else None,
    }
    return candidates, meta


# ═══════════════════════════════════════════════════════════════════════
# MONGO MODE (live, read-only)
# ═══════════════════════════════════════════════════════════════════════

def _normalize_doc(raw, source):
    emb = raw.get("embedding")
    if not (isinstance(emb, list) and len(emb) >= 8):
        return None
    text = _first_present(raw, TEXT_FIELDS)
    if not isinstance(text, str) or not text.strip():
        return None
    url = _first_present(raw, URL_FIELDS) or ""
    title = _first_present(raw, TITLE_FIELDS) or ""
    created = raw.get("created_utc")
    if hasattr(created, "isoformat"):
        created = created.isoformat()
    elif not isinstance(created, str):
        created = None
    return {
        "id": url or f"{source}:{abs(hash(text[:200]))}",
        "source": source,
        "title": title if isinstance(title, str) else "",
        "post_text": text,
        "post_url": url if isinstance(url, str) else "",
        "platform": _first_present(raw, PLATFORM_FIELDS) or "",
        "created_utc": created,
        "embedding": list(emb),
    }


def _read_one_cluster(uri, label, limit, projection):
    """READ-ONLY. find() with a projection and a limit. Nothing else."""
    from pymongo import MongoClient
    docs = []
    client = None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=15000)
        coll = client[MONGODB_DB][MONGO_COLLECTION]
        cursor = coll.find({"embedding": {"$exists": True}}, projection).limit(limit)
        for raw in cursor:
            d = _normalize_doc(raw, label)
            if d:
                docs.append(d)
    except Exception:                                   # noqa: BLE001
        # A cluster being unreachable must never fail the whole query;
        # the error is surfaced in meta, not raised. No URI is logged.
        return docs, False
    finally:
        if client is not None:
            client.close()
    return docs, True


def retrieve_from_mongo(query_intent, pool=None, sim_floor=None, scan_limit=8000):
    """Live read-only retrieval across the three configured clusters."""
    pool = pool or schemas.TARGET_CANDIDATE_POOL
    sim_floor = schemas.LOW_CONFIDENCE_SIM_FLOOR if sim_floor is None else sim_floor

    sources = [(n, u) for n, u in (("mongo_primary", MONGODB_URI),
                                   ("mongo_2", MONGODB2),
                                   ("mongo_4", MONGODB4)) if u]
    if not sources:
        raise RuntimeError("no MongoDB URIs configured in the environment")

    projection = {"embedding": 1, "created_utc": 1, "platform": 1, "source": 1,
                  **{f: 1 for f in TEXT_FIELDS},
                  **{f: 1 for f in TITLE_FIELDS},
                  **{f: 1 for f in URL_FIELDS}}

    docs, unreachable = [], []
    per = max(1, scan_limit // len(sources))
    for label, uri in sources:
        got, ok = _read_one_cluster(uri, label, per, projection)
        docs.extend(got)
        if not ok:
            unreachable.append(label)

    if not docs:
        raise RuntimeError("no documents with embeddings retrieved from any cluster")

    query_vec = embed_texts([query_intent["topic_embedding_query"]])[0]
    candidates, meta = retrieve_from_corpus(
        query_intent, docs, matrix=corpus_matrix(docs),
        query_vec=query_vec, pool=pool, sim_floor=sim_floor)
    meta.update({"mode": "mongo", "scanned": len(docs),
                 "unreachable_sources": unreachable})
    return candidates, meta
