#!/usr/bin/env python3
"""
OPPORTUNITY STRENGTH — derived signal (spec 9)
===========================================================================
Answers one question per document:

    How likely is this post to represent a real, actionable commercial
    opportunity — as opposed to someone merely discussing the same topic?

This is deliberately NOT a rename of any single input. A post can hold the
right intent label and still be a weak opportunity (vague buyer_demand, no
stake, no specifics). A post can hold a secondary intent label and still be
a strong opportunity (a question_info post that reveals a real unsolved
need with budget implications). opportunity_strength is what separates the
two, and it is what lets opportunity_scan admit secondary intents without
flooding the result set with commentary.

PURE FUNCTION. No I/O, no network, no global state. Weights arrive as an
argument so Experiment 4 can sweep them without touching this file.
"""

import json
import math
import os
from datetime import datetime, timezone

_WEIGHTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weights.json")


def load_weights(path=None):
    """Read weights.json. Never cached — Experiment 4 rewrites it between runs."""
    with open(path or _WEIGHTS_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── shared sub-scores (also used by ranker.py) ────────────────────────

def recency_score(created_utc, cfg):
    """Exponential decay on document age. 1.0 today, 0.5 at one half-life."""
    half = float(cfg.get("half_life_days", 30)) or 30.0
    if not created_utc:
        return float(cfg.get("missing_date_score", 0.35))
    try:
        if isinstance(created_utc, datetime):
            dt = created_utc
        else:
            s = str(created_utc).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return float(cfg.get("missing_date_score", 0.35))
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    if age_days < 0:
        age_days = 0.0
    return max(float(cfg.get("floor", 0.0)), 0.5 ** (age_days / half))


def source_quality_score(source, cfg):
    """Per-source prior. Placeholder until a real study exists — see weights.json."""
    if source in cfg:
        return float(cfg[source])
    return float(cfg.get("_default", 0.5))


# ── the derived signal ────────────────────────────────────────────────

def intent_match_score(cls, query_intent, weights):
    """How much opportunity this document's intent can carry.

    Uses the better of primary and secondary intent, each discounted by
    its own confidence, then scaled by that intent's opportunity tier.
    A document is never credited for an intent it does not actually hold.
    """
    tiers = weights.get("opportunity_intent_tier", {})

    primary = cls.get("intent")
    p_tier = float(tiers.get(primary, 0.2))
    p_score = p_tier * float(cls.get("intent_confidence") or 0.0)

    secondary = cls.get("secondary_intent")
    s_score = 0.0
    if secondary:
        s_tier = float(tiers.get(secondary, 0.2))
        s_score = s_tier * float(cls.get("secondary_confidence") or 0.0)

    best = max(p_score, s_score)

    # When the query names intents, an on-target intent is worth full
    # credit and an off-target one is discounted rather than zeroed —
    # opportunity_strength describes the DOCUMENT, and the hard intent
    # gate lives in ranker.py, not here.
    include = (query_intent or {}).get("intent_include") or []
    if include:
        on_target = primary in include or (secondary in include if secondary else False)
        if not on_target:
            best *= 0.45

    return max(0.0, min(1.0, best))


def score(cls, topic_sim, doc, query_intent, weights):
    """Compute opportunity_strength in [0, 1] for one classified document.

    cls         : DocClassification dict (from doc_classifier / schemas)
    topic_sim   : cosine similarity of this doc to the query embedding
    doc         : the raw normalized doc (needs created_utc, source)
    query_intent: QueryIntent dict, or None
    weights     : parsed weights.json
    """
    if cls.get("noise"):
        return 0.0

    w = weights.get("opportunity", {})
    rec_cfg = weights.get("recency", {})
    src_cfg = weights.get("source_quality", {})

    parts = {
        "intent_match_score": intent_match_score(cls, query_intent, weights),
        "commercial_signal": float(cls.get("commercial_signal") or 0.0),
        "specificity": float(cls.get("specificity") or 0.0),
        "urgency": float(cls.get("urgency") or 0.0),
        "topic_relevance": max(0.0, min(1.0, float(topic_sim or 0.0))),
        "recency": recency_score(doc.get("created_utc"), rec_cfg),
        "source_quality": source_quality_score(doc.get("source"), src_cfg),
    }

    total_w = sum(abs(float(w.get(k, 0.0))) for k in parts)
    if total_w <= 0:
        return 0.0
    raw = sum(float(w.get(k, 0.0)) * v for k, v in parts.items()) / total_w
    raw = max(0.0, min(1.0, raw))

    # Hard ceilings. general_discussion cannot present as an opportunity
    # however strong its other signals look — by definition it lacks the
    # commercial or pain evidence an opportunity requires.
    ceilings = weights.get("ceilings", {})
    for intent_name, cap in ceilings.items():
        if intent_name.startswith("_"):
            continue
        if cls.get("intent") == intent_name:
            raw = min(raw, float(cap))

    return raw


def explain(cls, topic_sim, doc, query_intent, weights):
    """Per-term breakdown, for failure analysis and Experiment 4 review."""
    w = weights.get("opportunity", {})
    parts = {
        "intent_match_score": intent_match_score(cls, query_intent, weights),
        "commercial_signal": float(cls.get("commercial_signal") or 0.0),
        "specificity": float(cls.get("specificity") or 0.0),
        "urgency": float(cls.get("urgency") or 0.0),
        "topic_relevance": max(0.0, min(1.0, float(topic_sim or 0.0))),
        "recency": recency_score(doc.get("created_utc"), weights.get("recency", {})),
        "source_quality": source_quality_score(doc.get("source"), weights.get("source_quality", {})),
    }
    total_w = sum(abs(float(w.get(k, 0.0))) for k in parts) or 1.0
    return {
        "terms": {k: {"value": round(v, 4),
                      "weight": float(w.get(k, 0.0)),
                      "contribution": round(float(w.get(k, 0.0)) * v / total_w, 4)}
                  for k, v in parts.items()},
        "opportunity_strength": round(score(cls, topic_sim, doc, query_intent, weights), 4),
    }
