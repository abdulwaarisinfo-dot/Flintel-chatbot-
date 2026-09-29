"""
FLINTEL — EMBEDDING / SEMANTIC-INTENT DIAGNOSTIC HARNESS
============================================================================
Standalone, READ-ONLY diagnostic. Answers one question with measurements
instead of opinion:

    Can text-embedding-3-small, as Flintel already stores it, support
    general-purpose intent-aware retrieval across arbitrary natural-
    language prompts — or is an additional interpretation/rerank layer
    structurally required?

WHAT THIS IS NOT
----------------
  * NOT an architecture change. Nothing here touches logics.py,
    flintel.py, config.py, database.py or any production path.
  * NOT a writer. Every Mongo handle below is opened for reads only and
    the code never calls insert/update/delete/create_index. The only
    things written are local files in --outdir.
  * NOT dependent on Flintel's own modules. It reads the same env vars
    and the same document shape, but imports nothing from the app, so it
    cannot break the app and can be run from anywhere.

WHY THE METRICS ARE THE METRICS
-------------------------------
The naive version of this test — "do relevant posts score higher than
irrelevant ones" — is misleading on OpenAI embeddings, because their
cosine similarities sit in a narrow, high band. Unrelated pairs commonly
land at 0.15-0.30, and same-topic/different-intent pairs at 0.45-0.65.
A single global threshold (Flintel currently uses 0.35) therefore mostly
measures TOPIC overlap, not intent. Two consequences shape this harness:

  1. Threshold-free ranking metrics (ROC-AUC, precision@k, nDCG) matter
     more than absolute scores. A retriever can be perfectly usable with
     every score in [0.55, 0.62] as long as the ORDER is right.
  2. Topic signal must be controlled for. TOPIC-CENTERED analysis below
     subtracts each topic's own centroid from every vector before
     re-scoring. What survives is the intent signal alone. If separation
     collapses to chance after centering, the embedding genuinely does
     not encode the distinction and no threshold, pool size or index
     will recover it — that is the finding that decides the architecture.

THREE STAGES
------------
    python embedding_diagnostic.py sample  --outdir ./diag
    python embedding_diagnostic.py label   --outdir ./diag
    python embedding_diagnostic.py measure --outdir ./diag

  sample  — pulls a RANDOM (not newest-N) read-only sample of real
            signals with their stored embeddings, from all four sources
            Flintel actually uses: MONGODB_URI, MONGODB2, MONGODB4, and
            the github_signals JSON folders.
  label   — batch-labels each sampled post with {topic, intent} using
            Claude, so a usable labeled set exists in minutes instead of
            a week of hand-labeling. Writes labels to a separate file;
            spot-check it, edit by hand where it is wrong, re-run measure.
  measure — embeds the probe queries, computes every metric, writes a
            human-readable report plus machine-readable JSON/CSV.

ENV VARS USED (same names as the app; all read-only)
    MONGODB_URI, MONGODB2, MONGODB4, MONGODB_DB
    OPENAI_API_KEY, EMBEDDING_MODEL   (default text-embedding-3-small)
    ANTHROPIC_API_KEY, CLAUDE_MODEL   (label stage only)
    GITHUB_SIGNALS_BASE_DIR, GITHUB_SIGNALS_DIRS

DEPENDENCIES: pymongo, numpy. (openai is optional — the embed call falls
back to plain urllib so the harness runs with nothing extra installed.)
"""

import argparse
import csv
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict, Counter
from datetime import datetime, timezone

import numpy as np

# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB2 = os.getenv("MONGODB2", "")
MONGODB4 = os.getenv("MONGODB4", "")
MONGODB_DB = os.getenv("MONGODB_DB", "flintel_bot")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

GITHUB_SIGNALS_BASE_DIR = os.getenv("GITHUB_SIGNALS_BASE_DIR", ".")
GITHUB_SIGNALS_DIRS = [
    d.strip()
    for d in os.getenv("GITHUB_SIGNALS_DIRS", "Mongo,Mongo1,Mongo2,Mongo3").split(",")
    if d.strip()
]

TITLE_FIELDS = ("title", "post_title", "headline")
TEXT_FIELDS = ("post_text", "text", "body", "content", "selftext")
URL_FIELDS = ("post_url", "url", "link", "permalink")
PLATFORM_FIELDS = ("platform", "source", "source_platform")

# The intent categories are a MEASUREMENT INSTRUMENT, not a proposed
# production taxonomy. They exist so the confusion matrix has axes. The
# dynamic-probe test further down deliberately ignores them, so that
# "fixed taxonomy vs. per-prompt derived meaning" can be compared as
# numbers rather than argued about.
INTENT_CATEGORIES = [
    "buyer_demand",           # wants to acquire / is shopping for a solution
    "provider_supply",        # sells / builds / offers it to others
    "hiring",                 # hiring for a role, or looking for work
    "complaint_pain",         # frustrated with a product/process
    "alternative_switching",  # evaluating a replacement, migrating away
    "usage_adoption",         # states they use / have adopted something
    "comparison",             # weighing two or more named options
    "question_info",          # asking how something works
    "competitor_research",    # studying the market/competitors
    "trend_signal",           # commentary on where a market is heading
    "general_discussion",     # on-topic but none of the above
    "irrelevant",             # off-topic entirely
]

# Natural-language probes. These are the QUERIES a user would actually
# type. Each carries the intent it is meant to retrieve, so precision can
# be scored, plus the topic it is scoped to, so topic-centering works.
# `expect` is the label a correctly-retrieved post should carry.
PROBE_QUERIES = [
    # ── same topic, six different meanings — the core discrimination test
    {"q": "Find businesses looking for AI agents",
     "expect": "buyer_demand", "topic": "ai_agents"},
    {"q": "Find people who sell AI agents",
     "expect": "provider_supply", "topic": "ai_agents"},
    {"q": "Find companies building AI agents",
     "expect": "provider_supply", "topic": "ai_agents"},
    {"q": "Find people complaining about a problem AI agents could solve",
     "expect": "complaint_pain", "topic": "ai_agents"},
    {"q": "Find people asking how AI agents work",
     "expect": "question_info", "topic": "ai_agents"},
    {"q": "Find emerging demand for AI automation",
     "expect": "trend_signal", "topic": "ai_agents"},
    # ── hiring
    {"q": "Find founders hiring product designers",
     "expect": "hiring", "topic": "hiring"},
    {"q": "Find companies hiring developers",
     "expect": "hiring", "topic": "hiring"},
    # ── CRM / SaaS switching cluster
    {"q": "Find people complaining about their CRM",
     "expect": "complaint_pain", "topic": "crm"},
    {"q": "Find companies using HubSpot",
     "expect": "usage_adoption", "topic": "crm"},
    {"q": "Find people looking for a HubSpot alternative",
     "expect": "alternative_switching", "topic": "crm"},
    # ── ecommerce
    {"q": "Find the most common complaints about Shopify",
     "expect": "complaint_pain", "topic": "ecommerce"},
    # ── brand monitoring
    {"q": "Find what people are saying about OpenAI this week",
     "expect": "general_discussion", "topic": "openai"},
    # ── competitor research
    {"q": "Find competitors offering WhatsApp automation",
     "expect": "competitor_research", "topic": "whatsapp"},
    {"q": "Find businesses looking for WhatsApp AI agents",
     "expect": "buyer_demand", "topic": "whatsapp"},
    # ── comparison
    {"q": "Compare what customers complain about across two products",
     "expect": "comparison", "topic": "crm"},
]

RRF_K = 60
RANDOM_SEED = 1337


# ═══════════════════════════════════════════════════════════════════════
# SMALL UTILITIES
# ═══════════════════════════════════════════════════════════════════════

def _first_present(doc, keys):
    for k in keys:
        v = doc.get(k)
        if v not in (None, ""):
            return v
    return None


def _valid_embedding(v):
    return isinstance(v, (list, tuple)) and len(v) > 0 and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) for x in v
    )


def _log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def _die(msg, code=2):
    print(f"\nFATAL: {msg}\n", file=sys.stderr)
    sys.exit(code)


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this",
    "these", "those", "is", "are", "was", "were", "be", "been", "being", "to",
    "of", "in", "on", "at", "for", "with", "about", "as", "by", "from", "it",
    "its", "we", "our", "you", "your", "i", "my", "me", "they", "their", "he",
    "she", "his", "her", "have", "has", "had", "do", "does", "did", "can",
    "could", "would", "should", "will", "just", "so", "not", "no", "any",
    "some", "there", "here", "what", "which", "who", "how", "when", "where",
}


def _content_words(text):
    """Content-word set for the lexical-overlap axis. Deliberately crude —
    it only has to separate 'shares vocabulary' from 'shares meaning'."""
    if not isinstance(text, str):
        return set()
    return {
        w for w in re.findall(r"[a-z0-9']+", text.lower())
        if len(w) >= 3 and w not in _STOPWORDS
    }


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# ═══════════════════════════════════════════════════════════════════════
# STATISTICS
#
# Implemented directly rather than pulled from sklearn/scipy so the
# harness has no dependency beyond numpy, and so every number in the
# report can be traced to a formula you can read here.
# ═══════════════════════════════════════════════════════════════════════

def roc_auc(pos_scores, neg_scores):
    """Probability that a randomly chosen positive outranks a randomly
    chosen negative. Computed via the Mann-Whitney U identity, with ties
    credited 0.5 — the standard convention.

    This is THE headline metric of this harness: it is threshold-free, so
    it answers "does the embedding order things correctly" independently
    of where SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD happens to be set.

        0.50 = chance (embedding carries no usable signal for this query)
        0.70 = weak but real
        0.80 = usable with reranking
        0.90 = strong
    """
    pos = np.asarray(pos_scores, dtype=float)
    neg = np.asarray(neg_scores, dtype=float)
    if pos.size == 0 or neg.size == 0:
        return float("nan")
    allv = np.concatenate([pos, neg])
    order = allv.argsort()
    ranks = np.empty(allv.size, dtype=float)
    ranks[order] = np.arange(1, allv.size + 1, dtype=float)
    # average ranks within tie groups
    sorted_vals = allv[order]
    i = 0
    while i < sorted_vals.size:
        j = i
        while j + 1 < sorted_vals.size and sorted_vals[j + 1] == sorted_vals[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    rank_sum_pos = ranks[: pos.size].sum()
    u = rank_sum_pos - pos.size * (pos.size + 1) / 2.0
    return float(u / (pos.size * neg.size))


def cohens_d(a, b):
    """Standardized mean difference — how far apart two score
    distributions are in units of their own spread. Unlike a raw mean
    gap, it is comparable across queries whose score ranges differ.
    |d| < 0.2 negligible, 0.5 medium, 0.8 large."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.size < 2 or b.size < 2:
        return float("nan")
    va, vb = a.var(ddof=1), b.var(ddof=1)
    pooled = math.sqrt(((a.size - 1) * va + (b.size - 1) * vb) / (a.size + b.size - 2))
    if pooled == 0:
        return float("nan")
    return float((a.mean() - b.mean()) / pooled)


def precision_at_k(labels_ranked, positive_label, k):
    top = labels_ranked[:k]
    if not top:
        return float("nan")
    return sum(1 for lbl in top if lbl == positive_label) / len(top)


def ndcg_at_k(labels_ranked, positive_label, k):
    gains = [1.0 if lbl == positive_label else 0.0 for lbl in labels_ranked[:k]]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    n_pos = sum(1 for lbl in labels_ranked if lbl == positive_label)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(k, n_pos)))
    return float(dcg / ideal) if ideal > 0 else float("nan")


def describe(scores):
    a = np.asarray(scores, dtype=float)
    if a.size == 0:
        return {"n": 0}
    return {
        "n": int(a.size),
        "mean": float(a.mean()),
        "std": float(a.std(ddof=1)) if a.size > 1 else 0.0,
        "min": float(a.min()),
        "p10": float(np.percentile(a, 10)),
        "p50": float(np.percentile(a, 50)),
        "p90": float(np.percentile(a, 90)),
        "max": float(a.max()),
    }


# ═══════════════════════════════════════════════════════════════════════
# STAGE 1 — SAMPLE  (read-only)
# ═══════════════════════════════════════════════════════════════════════

def _mongo_sample(uri, label, n, seed_terms):
    """Random sample from one cluster's flintel_signals.

    Uses $sample, NOT sort(created_utc).limit(N) — sampling by recency is
    the precise bug this whole investigation is about, and reproducing it
    here would bias the test set toward exactly the documents the current
    retriever can already see.

    `seed_terms` optionally biases the sample toward documents mentioning
    the probe topics, so the labeled set is not 95% off-topic noise. Both
    a targeted and an untargeted sample are taken; the untargeted half is
    what supplies genuine negatives.
    """
    try:
        from pymongo import MongoClient
    except ImportError:
        _die("pymongo not installed:  pip install pymongo numpy")

    docs = []
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=15000)
        coll = client[MONGODB_DB].flintel_signals
        total = coll.estimated_document_count()
        _log(f"  {label}: ~{total:,} documents")

        base = {"embedding": {"$ne": None, "$exists": True}}

        # Half the sample is topic-targeted so the labeled set actually
        # contains the intents we want to discriminate between.
        if seed_terms:
            pattern = r"\b(?:" + "|".join(re.escape(t) for t in seed_terms) + r")\b"
            targeted = {"$and": [base, {"$or": [
                {f: {"$regex": pattern, "$options": "i"}} for f in ("title",) + TEXT_FIELDS
            ]}]}
            try:
                cur = coll.aggregate(
                    [{"$match": targeted}, {"$sample": {"size": max(1, n // 2)}}],
                    allowDiskUse=False, maxTimeMS=120000,
                )
                got = list(cur)
                _log(f"  {label}: {len(got)} topic-targeted")
                docs.extend(got)
            except Exception as exc:
                _log(f"  {label}: targeted sample failed ({exc}) — continuing")

        # The other half is unconditioned: these are the real negatives.
        try:
            cur = coll.aggregate(
                [{"$match": base}, {"$sample": {"size": max(1, n // 2)}}],
                allowDiskUse=False, maxTimeMS=120000,
            )
            got = list(cur)
            _log(f"  {label}: {len(got)} random")
            docs.extend(got)
        except Exception as exc:
            _log(f"  {label}: random sample failed ({exc})")

        client.close()
    except Exception as exc:
        _log(f"  {label}: UNREACHABLE — {exc}")
        return []

    return docs


def _github_sample(n, seed_terms):
    """Sample the file-backed source. Reads every JSON/JSONL under the
    configured folders, then samples randomly — again NOT newest-N."""
    paths = []
    for sub in GITHUB_SIGNALS_DIRS:
        root = os.path.join(GITHUB_SIGNALS_BASE_DIR, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.lower().endswith((".json", ".jsonl")):
                    paths.append(os.path.join(dirpath, name))
    if not paths:
        _log(f"  github_signals: no JSON files under {GITHUB_SIGNALS_DIRS} — skipped")
        return []

    records = []
    for p in paths:
        try:
            if p.lower().endswith(".jsonl"):
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        records.extend(_unwrap(obj))
            else:
                with open(p, "r", encoding="utf-8") as f:
                    records.extend(_unwrap(json.load(f)))
        except Exception as exc:
            _log(f"  github_signals: skipping {p}: {exc}")

    records = [r for r in records if _valid_embedding(r.get("embedding"))]
    _log(f"  github_signals: {len(records):,} embedded records in {len(paths)} file(s)")
    if not records:
        return []

    rng = random.Random(RANDOM_SEED)
    if seed_terms:
        pat = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in seed_terms) + r")\b", re.I)
        hits = [r for r in records if pat.search(
            f"{_first_present(r, TITLE_FIELDS) or ''} {_first_present(r, TEXT_FIELDS) or ''}")]
        rest = [r for r in records if r not in hits] if len(hits) < len(records) else []
        picked = rng.sample(hits, min(len(hits), n // 2))
        picked += rng.sample(rest, min(len(rest), n - len(picked)))
        return picked
    return rng.sample(records, min(len(records), n))


def _unwrap(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "signals", "posts", "items", "results"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        return [payload]
    return []


def _normalize_doc(raw, source):
    emb = raw.get("embedding")
    if not _valid_embedding(emb):
        return None
    text = _first_present(raw, TEXT_FIELDS)
    if not isinstance(text, str) or not text.strip():
        return None
    title = _first_present(raw, TITLE_FIELDS) or ""
    url = _first_present(raw, URL_FIELDS) or ""
    platform = _first_present(raw, PLATFORM_FIELDS) or ""
    created = raw.get("created_utc")
    if isinstance(created, datetime):
        created = created.replace(tzinfo=created.tzinfo or timezone.utc).isoformat()
    elif not isinstance(created, str):
        created = None
    return {
        "id": url or f"{source}:{abs(hash(text[:200]))}",
        "source": source,
        "title": title if isinstance(title, str) else "",
        "post_text": text,
        "post_url": url if isinstance(url, str) else "",
        "platform": platform if isinstance(platform, str) else "",
        "created_utc": created,
        "embedding": list(emb),
    }


def cmd_sample(args):
    seed_terms = [
        "ai agent", "ai agents", "chatbot", "automation", "hubspot", "crm",
        "shopify", "openai", "whatsapp", "hiring", "designer", "developer",
        "salesforce", "alternative", "saas", "agency", "freelance",
    ]

    sources = []
    if MONGODB_URI:
        sources.append(("mongo_primary", MONGODB_URI))
    if MONGODB2:
        sources.append(("mongo_2", MONGODB2))
    if MONGODB4:
        sources.append(("mongo_4", MONGODB4))
    if not sources:
        _log("No MONGODB_URI / MONGODB2 / MONGODB4 set — Mongo sources skipped.")

    _log("Sampling (READ-ONLY; no writes to any cluster)")
    out = []
    per_source = max(1, args.n // max(1, len(sources) + 1))

    for label, uri in sources:
        for raw in _mongo_sample(uri, label, per_source, seed_terms):
            d = _normalize_doc(raw, label)
            if d:
                out.append(d)

    for raw in _github_sample(per_source, seed_terms):
        d = _normalize_doc(raw, "github_files")
        if d:
            out.append(d)

    if not out:
        _die("No documents sampled from ANY source. Check MONGODB_URI / "
             "MONGODB2 / MONGODB4 and GITHUB_SIGNALS_BASE_DIR, then retry.")

    # de-dup by url/text
    seen, deduped = set(), []
    for d in out:
        key = d["post_url"] or d["post_text"][:200]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(d)

    dims = Counter(len(d["embedding"]) for d in deduped)
    os.makedirs(args.outdir, exist_ok=True)
    path = os.path.join(args.outdir, "sample.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for d in deduped:
            f.write(json.dumps(d) + "\n")

    _log(f"WROTE {len(deduped):,} docs -> {path}")
    _log(f"  by source: {dict(Counter(d['source'] for d in deduped))}")
    _log(f"  embedding dims: {dict(dims)}")
    if len(dims) > 1:
        _log("  !! MIXED DIMENSIONS — vectors from different models are in the "
             "same corpus. Cosine similarity across them is meaningless. "
             "This alone would explain broken retrieval; investigate before "
             "reading any other metric.")
    _log("NEXT:  python embedding_diagnostic.py label --outdir " + args.outdir)


# ═══════════════════════════════════════════════════════════════════════
# STAGE 2 — LABEL  (Claude-assisted; edit the output by hand as needed)
# ═══════════════════════════════════════════════════════════════════════

LABEL_SYSTEM = """You label social/forum posts for a retrieval evaluation.

For EACH numbered post, decide two things:

topic  — the subject area. Choose ONE of:
  ai_agents, crm, ecommerce, openai, whatsapp, hiring, other

intent — what the AUTHOR is doing. This is about the author's PURPOSE,
not the subject matter. Choose ONE of:
  buyer_demand          author wants to acquire/hire/buy a solution
  provider_supply       author sells, builds, or offers it to others
  hiring                author is hiring for a role, or seeking a job
  complaint_pain        author is frustrated with a product or process
  alternative_switching author is evaluating a replacement / migrating
  usage_adoption        author states they use or have adopted something
  comparison            author weighs two or more named options
  question_info         author asks how something works
  competitor_research   author studies the market or competitors
  trend_signal          author comments on where a market is heading
  general_discussion    on-topic but none of the above
  irrelevant            none of the topics apply

CRITICAL: intent and topic are independent. "I need someone to build me
an AI agent" and "We build AI agents for clients" share a topic and have
OPPOSITE intents. Label what the author WANTS, not what they mention.

Set "ambiguous": true when the post genuinely could be two intents.

Return ONLY a JSON array, one object per post, in the same order:
[{"i":1,"topic":"ai_agents","intent":"buyer_demand","ambiguous":false}, ...]
No prose, no markdown fence."""


def _claude(system, user, max_tokens=4000):
    if not ANTHROPIC_API_KEY:
        _die("ANTHROPIC_API_KEY not set — needed for the label stage. "
             "(You can also hand-label sample.jsonl into labels.jsonl yourself.)")
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                payload = json.loads(r.read())
            return "".join(b.get("text", "") for b in payload.get("content", []))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            _die(f"Claude API error {e.code}: {e.read()[:400].decode(errors='replace')}")
        except Exception as exc:
            if attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            _die(f"Claude call failed: {exc}")


def cmd_label(args):
    src = os.path.join(args.outdir, "sample.jsonl")
    if not os.path.exists(src):
        _die(f"{src} not found — run the sample stage first.")

    docs = [json.loads(l) for l in open(src, encoding="utf-8")]
    _log(f"Labeling {len(docs):,} posts in batches of {args.batch}")

    labels = {}
    dest = os.path.join(args.outdir, "labels.jsonl")
    # resume support — relabeling is the expensive part, don't redo it
    if os.path.exists(dest):
        for line in open(dest, encoding="utf-8"):
            try:
                rec = json.loads(line)
                labels[rec["id"]] = rec
            except ValueError:
                pass
        _log(f"  resuming: {len(labels):,} already labeled")

    todo = [d for d in docs if d["id"] not in labels]
    for start in range(0, len(todo), args.batch):
        chunk = todo[start:start + args.batch]
        lines = []
        for i, d in enumerate(chunk, 1):
            body = (d["post_text"] or "")[:900].replace("\n", " ")
            lines.append(f"[{i}] TITLE: {d['title'][:200]}\nTEXT: {body}")
        raw = _claude(LABEL_SYSTEM, "\n\n".join(lines))
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw).strip()
        try:
            parsed = json.loads(raw)
        except ValueError:
            m = re.search(r"\[.*\]", raw, re.S)
            parsed = json.loads(m.group(0)) if m else []

        for item in parsed:
            try:
                idx = int(item.get("i", 0)) - 1
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(chunk)):
                continue
            d = chunk[idx]
            intent = item.get("intent")
            labels[d["id"]] = {
                "id": d["id"],
                "topic": item.get("topic") or "other",
                "intent": intent if intent in INTENT_CATEGORIES else "general_discussion",
                "ambiguous": bool(item.get("ambiguous")),
            }

        with open(dest, "w", encoding="utf-8") as f:
            for rec in labels.values():
                f.write(json.dumps(rec) + "\n")
        _log(f"  {min(start + args.batch, len(todo)):,}/{len(todo):,}")

    counts = Counter(r["intent"] for r in labels.values())
    _log(f"WROTE {len(labels):,} labels -> {dest}")
    _log(f"  intent distribution: {dict(counts.most_common())}")
    _log(f"  ambiguous: {sum(1 for r in labels.values() if r['ambiguous']):,}")
    _log("SPOT-CHECK labels.jsonl before trusting the report. Claude labels "
         "are a starting point, not ground truth — fix any you disagree with "
         "and re-run measure; the file is plain JSONL.")
    _log("NEXT:  python embedding_diagnostic.py measure --outdir " + args.outdir)


# ═══════════════════════════════════════════════════════════════════════
# STAGE 3 — MEASURE
# ═══════════════════════════════════════════════════════════════════════

def embed_texts(texts):
    """Embed with the SAME model the corpus was built with. Uses the
    openai package when present, plain urllib otherwise, so the harness
    runs with no extra install."""
    if not OPENAI_API_KEY:
        _die("OPENAI_API_KEY not set — needed to embed the probe queries.")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
        return [d.embedding for d in resp.data]
    except ImportError:
        pass
    body = json.dumps({"model": EMBEDDING_MODEL, "input": texts}).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=body,
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {OPENAI_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = json.loads(r.read())
    return [d["embedding"] for d in payload["data"]]


def _unit(mat):
    mat = np.asarray(mat, dtype=np.float64)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def cmd_measure(args):
    outdir = args.outdir
    src = os.path.join(outdir, "sample.jsonl")
    lbl = os.path.join(outdir, "labels.jsonl")
    for p in (src, lbl):
        if not os.path.exists(p):
            _die(f"{p} not found — run the earlier stages first.")

    docs = [json.loads(l) for l in open(src, encoding="utf-8")]
    labels = {}
    for line in open(lbl, encoding="utf-8"):
        rec = json.loads(line)
        labels[rec["id"]] = rec

    docs = [d for d in docs if d["id"] in labels]
    if not docs:
        _die("No labeled documents.")

    dims = Counter(len(d["embedding"]) for d in docs)
    if len(dims) > 1:
        keep = dims.most_common(1)[0][0]
        _log(f"MIXED DIMENSIONS {dict(dims)} — keeping only {keep}-dim vectors.")
        docs = [d for d in docs if len(d["embedding"]) == keep]

    D = _unit([d["embedding"] for d in docs])           # (N, dim) unit rows
    doc_intent = np.array([labels[d["id"]]["intent"] for d in docs])
    doc_topic = np.array([labels[d["id"]]["topic"] for d in docs])
    doc_words = [_content_words(f"{d['title']} {d['post_text']}") for d in docs]

    _log(f"Corpus: {len(docs):,} labeled docs, {D.shape[1]} dims")
    _log(f"  topics: {dict(Counter(doc_topic).most_common())}")
    _log(f"  intents: {dict(Counter(doc_intent).most_common())}")

    # ── embed probes ────────────────────────────────────────────────────
    probe_texts = [p["q"] for p in PROBE_QUERIES]
    _log(f"Embedding {len(probe_texts)} probe queries with {EMBEDDING_MODEL}")
    Q = _unit(embed_texts(probe_texts))                  # (P, dim)
    S = Q @ D.T                                          # (P, N) cosine

    report = []
    W = report.append
    W("=" * 78)
    W("FLINTEL — EMBEDDING / SEMANTIC-INTENT DIAGNOSTIC")
    W("=" * 78)
    W(f"generated      : {datetime.now(timezone.utc).isoformat()}")
    W(f"embedding model: {EMBEDDING_MODEL}  ({D.shape[1]} dims)")
    W(f"corpus         : {len(docs):,} labeled posts")
    W(f"sources        : {dict(Counter(d['source'] for d in docs))}")
    W("")

    # ── 0. GLOBAL SIMILARITY BASELINE ───────────────────────────────────
    # Everything else is meaningless without this. If random unrelated
    # pairs already sit near the production threshold, the threshold is
    # not separating anything.
    rng = np.random.default_rng(RANDOM_SEED)
    npairs = min(20000, len(docs) * 20)
    ia = rng.integers(0, len(docs), npairs)
    ib = rng.integers(0, len(docs), npairs)
    mask = ia != ib
    rand_sims = np.einsum("ij,ij->i", D[ia[mask]], D[ib[mask]])

    W("-" * 78)
    W("0. BASELINE — what does cosine similarity mean in THIS corpus?")
    W("-" * 78)
    b = describe(rand_sims)
    W(f"  random doc-doc pairs (n={b['n']:,}):")
    W(f"    mean {b['mean']:.3f}   p10 {b['p10']:.3f}   p50 {b['p50']:.3f}   "
      f"p90 {b['p90']:.3f}   max {b['max']:.3f}")
    prod_thresh = 0.35
    above = float((rand_sims >= prod_thresh).mean())
    W(f"  fraction of RANDOM pairs already >= production threshold "
      f"{prod_thresh}: {above:.1%}")
    if above > 0.25:
        W("    >> The production threshold admits a quarter or more of")
        W("       RANDOM pairs. It is not acting as a relevance filter.")
    W("")

    # ── 1. PER-QUERY RETRIEVAL QUALITY ──────────────────────────────────
    W("-" * 78)
    W("1. PER-QUERY RETRIEVAL QUALITY (raw embedding space)")
    W("-" * 78)
    W("   AUC is threshold-free: P(relevant ranks above irrelevant).")
    W("   0.50=chance  0.70=weak  0.80=usable w/ rerank  0.90=strong")
    W("")
    W(f"   {'query':<52}{'AUC':>6}{'d':>7}{'P@10':>7}{'nDCG':>7}")

    per_query = []
    for pi, probe in enumerate(PROBE_QUERIES):
        scores = S[pi]
        # Relevance = correct intent AND correct topic. Topic-only matches
        # are the interesting negatives: same subject, wrong meaning.
        rel = (doc_intent == probe["expect"]) & (doc_topic == probe["topic"])
        same_topic_wrong_intent = (doc_topic == probe["topic"]) & ~rel
        if rel.sum() < 3:
            per_query.append({"query": probe["q"], "skipped": "too few positives",
                              "n_positive": int(rel.sum())})
            continue

        order = np.argsort(-scores)
        ranked_intent = [doc_intent[i] if doc_topic[i] == probe["topic"] else "__offtopic__"
                         for i in order]

        auc_all = roc_auc(scores[rel], scores[~rel])
        auc_topic = (roc_auc(scores[rel], scores[same_topic_wrong_intent])
                     if same_topic_wrong_intent.sum() >= 3 else float("nan"))
        d_all = cohens_d(scores[rel], scores[~rel])
        p10 = precision_at_k(ranked_intent, probe["expect"], 10)
        nd = ndcg_at_k(ranked_intent, probe["expect"], 20)

        per_query.append({
            "query": probe["q"], "expect": probe["expect"], "topic": probe["topic"],
            "n_positive": int(rel.sum()),
            "n_same_topic_wrong_intent": int(same_topic_wrong_intent.sum()),
            "auc_vs_all": auc_all,
            "auc_vs_same_topic": auc_topic,
            "cohens_d": d_all,
            "p_at_5": precision_at_k(ranked_intent, probe["expect"], 5),
            "p_at_10": p10, "p_at_20": precision_at_k(ranked_intent, probe["expect"], 20),
            "ndcg_at_20": nd,
            "relevant": describe(scores[rel]),
            "same_topic_wrong_intent": describe(scores[same_topic_wrong_intent]),
            "irrelevant": describe(scores[~rel]),
        })
        W(f"   {probe['q'][:50]:<52}{auc_all:>6.2f}{d_all:>7.2f}{p10:>7.2f}{nd:>7.2f}")

    scored = [r for r in per_query if "auc_vs_all" in r]
    if scored:
        W("")
        W(f"   MEDIAN AUC vs all documents      : "
          f"{np.nanmedian([r['auc_vs_all'] for r in scored]):.3f}")
        topic_aucs = [r["auc_vs_same_topic"] for r in scored
                      if not math.isnan(r["auc_vs_same_topic"])]
        if topic_aucs:
            W(f"   MEDIAN AUC vs SAME-TOPIC wrong-intent: {np.nanmedian(topic_aucs):.3f}")
            W("     ^^ THIS is the number that decides the architecture.")
            W("        High AUC vs all + ~0.5 here means the embedding finds")
            W("        the TOPIC and is blind to the MEANING — retrieval")
            W("        cannot be fixed by pool size, threshold or index.")
    W("")

    # ── 2. INTENT CONFUSION MATRIX ──────────────────────────────────────
    W("-" * 78)
    W("2. INTENT CONFUSION — mean similarity, probe intent vs post intent")
    W("-" * 78)
    W("   Diagonal should dominate its row. If rows are flat, intents are")
    W("   indistinguishable in embedding space.")
    W("")
    present = [c for c in INTENT_CATEGORIES if (doc_intent == c).sum() >= 3]
    probe_by_intent = defaultdict(list)
    for pi, probe in enumerate(PROBE_QUERIES):
        probe_by_intent[probe["expect"]].append(pi)

    header = "   " + " " * 24 + "".join(f"{c[:9]:>10}" for c in present)
    W(header)
    confusion = {}
    for pintent, idxs in sorted(probe_by_intent.items()):
        row_vals = []
        for c in present:
            m = doc_intent == c
            row_vals.append(float(S[idxs][:, m].mean()) if m.sum() else float("nan"))
        confusion[pintent] = dict(zip(present, row_vals))
        best = max(range(len(row_vals)), key=lambda k: row_vals[k])
        marker = "  <-- OK" if present[best] == pintent else f"  <-- TOP={present[best][:12]}"
        W(f"   {pintent[:22]:<24}" + "".join(f"{v:>10.3f}" for v in row_vals) + marker)
    W("")

    # ── 3. TOPIC-CONTROLLED SEPARATION ──────────────────────────────────
    # Subtract each topic's centroid, renormalize, re-score. Whatever
    # separation survives is intent signal that is NOT just topic signal.
    W("-" * 78)
    W("3. TOPIC-CONTROLLED SEPARATION (topic centroid removed)")
    W("-" * 78)
    W("   Does intent signal exist independent of topic signal?")
    W("")
    Dc = D.copy()
    for t in set(doc_topic):
        m = doc_topic == t
        if m.sum() >= 2:
            Dc[m] -= Dc[m].mean(axis=0)
    Dc = _unit(Dc)

    W(f"   {'query':<52}{'AUC raw':>9}{'AUC centered':>14}")
    centered_rows = []
    for pi, probe in enumerate(PROBE_QUERIES):
        rel = (doc_intent == probe["expect"]) & (doc_topic == probe["topic"])
        same_topic = (doc_topic == probe["topic"]) & ~rel
        if rel.sum() < 3 or same_topic.sum() < 3:
            continue
        qc = Q[pi] - D[doc_topic == probe["topic"]].mean(axis=0)
        n = np.linalg.norm(qc)
        qc = qc / n if n else qc
        sc = Dc @ qc
        auc_raw = roc_auc(S[pi][rel], S[pi][same_topic])
        auc_cen = roc_auc(sc[rel], sc[same_topic])
        centered_rows.append({"query": probe["q"], "auc_raw_same_topic": auc_raw,
                              "auc_centered_same_topic": auc_cen})
        W(f"   {probe['q'][:50]:<52}{auc_raw:>9.2f}{auc_cen:>14.2f}")
    if centered_rows:
        mc = float(np.nanmedian([r["auc_centered_same_topic"] for r in centered_rows]))
        W("")
        W(f"   MEDIAN centered AUC: {mc:.3f}")
        if mc < 0.60:
            W("     >> Near chance. Intent is NOT separably encoded. An")
            W("        explicit interpretation/classification layer is")
            W("        REQUIRED — this is not a tuning problem.")
        elif mc < 0.75:
            W("     >> Weak but real signal. Embeddings alone will not carry")
            W("        it; a reranking layer can amplify it.")
        else:
            W("     >> Real, usable intent signal exists in the vectors.")
    W("")

    # ── 4. LEXICAL vs SEMANTIC QUADRANTS ────────────────────────────────
    # The two example classes asked for, derived rather than cherry-picked:
    #   low word overlap + high cosine + same intent  = true semantic win
    #   high word overlap + high cosine + diff intent = lexical false hit
    W("-" * 78)
    W("4. LEXICAL OVERLAP vs SEMANTIC SIMILARITY")
    W("-" * 78)
    pairs = []
    idx_by_topic = defaultdict(list)
    for i, t in enumerate(doc_topic):
        idx_by_topic[t].append(i)
    rng2 = random.Random(RANDOM_SEED)
    for t, idxs in idx_by_topic.items():
        if t == "other" or len(idxs) < 4:
            continue
        for _ in range(min(4000, len(idxs) * 12)):
            i, j = rng2.choice(idxs), rng2.choice(idxs)
            if i == j:
                continue
            pairs.append((i, j, float(D[i] @ D[j]), _jaccard(doc_words[i], doc_words[j])))

    if pairs:
        cos = np.array([p[2] for p in pairs])
        jac = np.array([p[3] for p in pairs])
        same = np.array([doc_intent[p[0]] == doc_intent[p[1]] for p in pairs])
        hi_cos = cos >= np.percentile(cos, 75)
        lo_lex = jac <= np.percentile(jac, 25)
        hi_lex = jac >= np.percentile(jac, 75)

        sem_win = hi_cos & lo_lex & same
        lex_trap = hi_cos & hi_lex & ~same
        W(f"   pairs analyzed: {len(pairs):,} (within-topic)")
        W(f"   correlation(cosine, word-overlap): {float(np.corrcoef(cos, jac)[0,1]):.3f}")
        W(f"   high-cos + LOW word overlap + SAME intent  : {int(sem_win.sum()):,}"
          f"   <- genuine semantic generalization")
        W(f"   high-cos + HIGH word overlap + DIFF intent : {int(lex_trap.sum()):,}"
          f"   <- lexical false positives")
        W("")
        W("   EXAMPLES — different wording, same meaning (semantic win):")
        for k in np.argsort(-cos * sem_win)[:args.examples]:
            if not sem_win[k]:
                break
            i, j = pairs[k][0], pairs[k][1]
            W(f"     cos={pairs[k][2]:.3f} lex={pairs[k][3]:.2f} [{doc_intent[i]}]")
            W(f"       A: {(docs[i]['title'] or docs[i]['post_text'])[:110]}")
            W(f"       B: {(docs[j]['title'] or docs[j]['post_text'])[:110]}")
        W("")
        W("   EXAMPLES — similar wording, DIFFERENT meaning (false match):")
        for k in np.argsort(-cos * lex_trap)[:args.examples]:
            if not lex_trap[k]:
                break
            i, j = pairs[k][0], pairs[k][1]
            W(f"     cos={pairs[k][2]:.3f} lex={pairs[k][3]:.2f} "
              f"[{doc_intent[i]}] vs [{doc_intent[j]}]")
            W(f"       A: {(docs[i]['title'] or docs[i]['post_text'])[:110]}")
            W(f"       B: {(docs[j]['title'] or docs[j]['post_text'])[:110]}")
    W("")

    # ── 5. THRESHOLD SWEEP ──────────────────────────────────────────────
    W("-" * 78)
    W("5. THRESHOLD SWEEP — what SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD buys")
    W("-" * 78)
    W(f"   {'thresh':>8}{'recall':>9}{'precision':>11}{'admitted':>10}")
    sweep = []
    rel_all = np.zeros(len(docs), dtype=bool)
    score_max = S.max(axis=0)
    for pi, probe in enumerate(PROBE_QUERIES):
        rel_all |= (doc_intent == probe["expect"]) & (doc_topic == probe["topic"])
    for th in [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        adm = score_max >= th
        if adm.sum() == 0:
            sweep.append({"threshold": th, "recall": 0.0, "precision": float("nan"),
                          "admitted": 0})
            W(f"   {th:>8.2f}{0.0:>9.2f}{float('nan'):>11.2f}{0:>10,}")
            continue
        recall = float((adm & rel_all).sum() / max(1, rel_all.sum()))
        prec = float((adm & rel_all).sum() / adm.sum())
        sweep.append({"threshold": th, "recall": recall, "precision": prec,
                      "admitted": int(adm.sum())})
        mark = "   <-- production" if abs(th - prod_thresh) < 1e-9 else ""
        W(f"   {th:>8.2f}{recall:>9.2f}{prec:>11.2f}{int(adm.sum()):>10,}{mark}")
    W("")

    # ── 6. CONCRETE FAILURE CASES ───────────────────────────────────────
    W("-" * 78)
    W("6. CONCRETE FAILURE CASES")
    W("-" * 78)
    failures = []
    for pi, probe in enumerate(PROBE_QUERIES):
        rel = (doc_intent == probe["expect"]) & (doc_topic == probe["topic"])
        if rel.sum() < 3:
            continue
        scores = S[pi]
        order = np.argsort(-scores)
        W(f"\n   QUERY: {probe['q']}")
        W(f"   (want intent={probe['expect']}, topic={probe['topic']})")
        W("   top-5 retrieved:")
        for rank, i in enumerate(order[:5], 1):
            ok = "OK " if rel[i] else "XX "
            W(f"     {ok}{rank}. [{scores[i]:.3f}] ({doc_intent[i]}/{doc_topic[i]}) "
              f"{(docs[i]['title'] or docs[i]['post_text'])[:88]}")
            if not rel[i]:
                failures.append({"query": probe["q"], "kind": "false_positive",
                                 "score": float(scores[i]), "rank": rank,
                                 "got_intent": str(doc_intent[i]),
                                 "got_topic": str(doc_topic[i]),
                                 "text": (docs[i]["title"] or docs[i]["post_text"])[:300],
                                 "url": docs[i]["post_url"]})
        missed = [i for i in np.argsort(scores) if rel[i]][:3]
        if missed:
            W("   lowest-scoring TRUE positives (retrieval misses):")
            for i in missed:
                W(f"     .. [{scores[i]:.3f}] "
                  f"{(docs[i]['title'] or docs[i]['post_text'])[:88]}")
                failures.append({"query": probe["q"], "kind": "false_negative",
                                 "score": float(scores[i]),
                                 "got_intent": str(doc_intent[i]),
                                 "got_topic": str(doc_topic[i]),
                                 "text": (docs[i]["title"] or docs[i]["post_text"])[:300],
                                 "url": docs[i]["post_url"]})
    W("")

    # ── 7. FIXED TAXONOMY vs DYNAMIC PER-PROMPT PROBES ──────────────────
    # Directly answers "should intent be derived per prompt rather than
    # from a fixed list". Fixed  = one generic sentence per category.
    # Dynamic = the actual user query plus paraphrases in the target's
    # own voice, which is what a router would produce.
    W("-" * 78)
    W("7. FIXED TAXONOMY vs DYNAMIC PER-PROMPT PROBES")
    W("-" * 78)
    fixed_texts = {
        "buyer_demand": "Someone looking to buy or hire a solution",
        "provider_supply": "Someone selling or offering a service",
        "hiring": "Someone hiring for a job role",
        "complaint_pain": "Someone complaining about a product",
        "alternative_switching": "Someone looking for an alternative product",
        "usage_adoption": "Someone using a product",
        "comparison": "Someone comparing two products",
        "question_info": "Someone asking how something works",
        "competitor_research": "Someone researching competitors",
        "trend_signal": "Someone discussing a market trend",
    }
    dyn_map = {
        "Find businesses looking for AI agents": [
            "We need an AI agent for our business but do not know where to start",
            "Looking for someone to build a chatbot for our company",
            "Has anyone hired an agency to automate customer support with AI?",
        ],
        "Find people who sell AI agents": [
            "We build custom AI agents for businesses",
            "Our agency delivers conversational AI and automation to clients",
            "DM me if you want an AI agent built for your workflow",
        ],
        "Find people looking for a HubSpot alternative": [
            "HubSpot is getting too expensive, what else is out there",
            "Migrating off HubSpot, looking at other CRMs",
            "Anyone switched away from HubSpot recently?",
        ],
        "Find people complaining about their CRM": [
            "Our CRM is a nightmare, the data is always out of sync",
            "Sick of how slow and clunky our CRM has become",
            "Why is every CRM so bad at basic reporting",
        ],
    }

    fixed_keys = list(fixed_texts)
    fixed_vecs = _unit(embed_texts([fixed_texts[k] for k in fixed_keys]))
    W(f"   {'query':<46}{'fixed':>8}{'dynamic':>10}{'delta':>9}")
    compare_rows = []
    for probe in PROBE_QUERIES:
        if probe["q"] not in dyn_map:
            continue
        rel = (doc_intent == probe["expect"]) & (doc_topic == probe["topic"])
        same_topic = (doc_topic == probe["topic"]) & ~rel
        if rel.sum() < 3 or same_topic.sum() < 3:
            continue
        fv = fixed_vecs[fixed_keys.index(probe["expect"])]
        s_fixed = D @ fv
        dv = _unit(embed_texts(dyn_map[probe["q"]]))
        s_dyn = (D @ dv.T).max(axis=1)   # max-similarity, as production does
        a_fixed = roc_auc(s_fixed[rel], s_fixed[same_topic])
        a_dyn = roc_auc(s_dyn[rel], s_dyn[same_topic])
        compare_rows.append({"query": probe["q"], "auc_fixed": a_fixed,
                             "auc_dynamic": a_dyn, "delta": a_dyn - a_fixed})
        W(f"   {probe['q'][:44]:<46}{a_fixed:>8.2f}{a_dyn:>10.2f}"
          f"{a_dyn - a_fixed:>+9.2f}")
    if compare_rows:
        md = float(np.median([r["delta"] for r in compare_rows]))
        W("")
        W(f"   MEDIAN delta (dynamic - fixed): {md:+.3f}")
        if md > 0.05:
            W("     >> Per-prompt derived probes beat a fixed taxonomy.")
            W("        Derive meaning from each prompt; do not hardcode intents.")
        elif md < -0.05:
            W("     >> Fixed category probes did better — the router's")
            W("        paraphrases are likely the weak link, not the taxonomy.")
        else:
            W("     >> No meaningful difference. Neither probe style rescues")
            W("        retrieval on its own; the limit is the embedding.")
    W("")

    # ── VERDICT ─────────────────────────────────────────────────────────
    W("=" * 78)
    W("VERDICT")
    W("=" * 78)
    med_all = float(np.nanmedian([r["auc_vs_all"] for r in scored])) if scored else float("nan")
    topic_aucs = [r["auc_vs_same_topic"] for r in scored
                  if not math.isnan(r.get("auc_vs_same_topic", float("nan")))]
    med_topic = float(np.nanmedian(topic_aucs)) if topic_aucs else float("nan")
    med_cent = (float(np.nanmedian([r["auc_centered_same_topic"] for r in centered_rows]))
                if centered_rows else float("nan"))

    W(f"  topic retrieval   (AUC vs all docs)        : {med_all:.3f}")
    W(f"  intent separation (AUC vs same-topic)      : {med_topic:.3f}")
    W(f"  intent separation (topic-centered)         : {med_cent:.3f}")
    W("")
    if not math.isnan(med_all) and med_all >= 0.75 and (
            math.isnan(med_topic) or med_topic < 0.65):
        W("  Embeddings FIND THE TOPIC and are LARGELY BLIND TO MEANING.")
        W("  Retrieval architecture (vector index, hybrid, bigger pool) will")
        W("  fix recall. It will NOT fix intent. A separate interpretation")
        W("  layer is structurally required.")
    elif not math.isnan(med_topic) and med_topic >= 0.75:
        W("  Embeddings carry usable intent signal. Retrieval architecture")
        W("  is the binding constraint; reranking is an optimization.")
    else:
        W("  Mixed. Read sections 1-3 before choosing; neither retrieval nor")
        W("  interpretation alone accounts for the failures.")
    W("=" * 78)

    text = "\n".join(report)
    print("\n" + text)

    with open(os.path.join(outdir, "report.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(os.path.join(outdir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "embedding_model": EMBEDDING_MODEL,
            "dims": int(D.shape[1]),
            "corpus_size": len(docs),
            "baseline_random_pairs": describe(rand_sims),
            "per_query": per_query,
            "intent_confusion": confusion,
            "topic_centered": centered_rows,
            "threshold_sweep": sweep,
            "fixed_vs_dynamic": compare_rows,
            "summary": {"auc_vs_all": med_all, "auc_vs_same_topic": med_topic,
                        "auc_topic_centered": med_cent},
        }, f, indent=2)
    with open(os.path.join(outdir, "failures.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["query", "kind", "score", "rank",
                                          "got_intent", "got_topic", "text", "url"])
        w.writeheader()
        for r in failures:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})

    _log(f"WROTE report.txt / metrics.json / failures.csv -> {outdir}")


# ═══════════════════════════════════════════════════════════════════════
# SELFTEST — verifies the metric math on vectors with known answers
# ═══════════════════════════════════════════════════════════════════════

def cmd_selftest(_args):
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond

    print("metric self-test")
    check("AUC perfect separation == 1.0", abs(roc_auc([3, 4, 5], [0, 1, 2]) - 1.0) < 1e-9)
    check("AUC inverted == 0.0", abs(roc_auc([0, 1, 2], [3, 4, 5]) - 0.0) < 1e-9)
    check("AUC identical == 0.5", abs(roc_auc([1, 1, 1], [1, 1, 1]) - 0.5) < 1e-9)
    check("AUC interleaved == 0.5", abs(roc_auc([1, 3], [2, 4]) - 0.25) < 1e-9)
    check("cohens_d sign", cohens_d([5, 6, 7], [1, 2, 3]) > 0)
    check("P@k all-hit == 1.0", precision_at_k(["a"] * 10, "a", 5) == 1.0)
    check("P@k no-hit == 0.0", precision_at_k(["b"] * 10, "a", 5) == 0.0)
    check("nDCG perfect == 1.0",
          abs(ndcg_at_k(["a", "a", "b", "b"], "a", 4) - 1.0) < 1e-9)
    check("jaccard identical == 1.0", _jaccard({"x", "y"}, {"x", "y"}) == 1.0)
    check("jaccard disjoint == 0.0", _jaccard({"x"}, {"y"}) == 0.0)

    rng = np.random.default_rng(0)
    a = _unit(rng.normal(size=(50, 32)))
    check("unit rows norm 1", np.allclose(np.linalg.norm(a, axis=1), 1.0))

    # separable planted signal must score high; pure noise must score ~0.5
    sig = np.zeros(32); sig[0] = 1.0
    pos = _unit(rng.normal(size=(60, 32)) * 0.3 + sig)
    neg = _unit(rng.normal(size=(60, 32)) * 0.3 - sig)
    q = sig / np.linalg.norm(sig)
    check("planted signal AUC > 0.95", roc_auc(pos @ q, neg @ q) > 0.95)
    n1 = _unit(rng.normal(size=(200, 32)))
    n2 = _unit(rng.normal(size=(200, 32)))
    check("pure noise AUC ~ 0.5", abs(roc_auc(n1 @ q, n2 @ q) - 0.5) < 0.12)

    print("\nALL PASS" if ok else "\nFAILURES PRESENT")
    sys.exit(0 if ok else 1)


# ═══════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="Flintel embedding / semantic-intent diagnostic (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="read-only random sample of real signals")
    s.add_argument("--outdir", default="./diag")
    s.add_argument("-n", type=int, default=1200, help="target sample size")
    s.set_defaults(func=cmd_sample)

    s = sub.add_parser("label", help="Claude-assisted labeling (resumable)")
    s.add_argument("--outdir", default="./diag")
    s.add_argument("--batch", type=int, default=15)
    s.set_defaults(func=cmd_label)

    s = sub.add_parser("measure", help="compute all metrics and write the report")
    s.add_argument("--outdir", default="./diag")
    s.add_argument("--examples", type=int, default=5)
    s.set_defaults(func=cmd_measure)

    s = sub.add_parser("selftest", help="verify the metric math (no data needed)")
    s.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
