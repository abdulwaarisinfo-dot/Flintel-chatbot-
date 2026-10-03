#!/usr/bin/env python3
"""
diagnostics/bridge_rank_probe.py  —  READ-ONLY, OFFLINE (no network, no Mongo)
=============================================================================
Question: inside intent_bridge._rank_passing(), do the prototype ranker's HARD
FILTERS (actor_direction / actor_type / min_commercial_signal /
min_specificity) actually run on real classifier output, or on the zeroed-out
placeholders _rank_passing() builds?

How: feed _rank_passing() a handful of fake "passing" candidates whose
similarity order is KNOWN and whose commercial_signal differs a lot, using
the query plan the interpreter prompt itself gives as the worked example for
"Find businesses looking for WhatsApp AI agents". If the ranker's filters /
scoring were effective, the output order would change and some candidates
would be dropped. If the output order == input order, ranking had no effect.

Run from the repo root:   python diagnostics/bridge_rank_probe.py
Imports only intent_bridge + intent_prototype (reads weights.json). Writes nothing.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import intent_bridge                                   # noqa: E402
from intent_prototype import ranker, schemas, opportunity   # noqa: E402

PLANS = {
    # interpreter SYSTEM prompt's own worked example (query_interpreter.py:107-117)
    "with actor filters (interpreter's own example)": {
        "topic_keywords": ["WhatsApp", "AI agent"],
        "topic_embedding_query": "whatsapp ai agent chatbot automation",
        "intent_include": ["buyer_demand"],
        "intent_exclude": ["provider_supply", "hiring", "irrelevant"],
        "actor_direction_filter": ["company_buying"],
        "actor_type_filter": ["company"],
        "min_commercial_signal": 0.4,
        "query_mode": "explicit_intent", "intent_logic": "OR",
    },
    # probe_queries.py PROBES[0]["plan"]: no actor / strength filters
    "no actor filters (probe_queries plan)": {
        "topic_keywords": ["AI agent"],
        "topic_embedding_query": "ai agent chatbot automation for business",
        "intent_include": ["buyer_demand"],
        "intent_exclude": ["provider_supply", "hiring", "irrelevant"],
        "query_mode": "explicit_intent", "intent_logic": "OR",
    },
}

# (url, topic_sim, commercial_signal, confidence, from_cache)
# Input order == descending similarity, as get_matched_signals() would pass it.
# Candidate "weak" has the HIGHEST similarity and ZERO real buying signal.
CANDS = [
    ("weak_high_sim",  0.80, 0.05, 0.60, False),
    ("mid_a",          0.70, 0.50, 0.70, False),
    ("strong_buyer",   0.55, 0.95, 0.90, False),
    ("cached_buyer",   0.52, 0.00, 0.90, True),    # cache hit => commercial_signal forced 0.0
    ("mid_b",          0.50, 0.40, 0.65, False),
]


def build_passing():
    out = []
    for url, sim, comm, conf, cached in CANDS:
        cand = {"title": url, "post_text": "x " * 20, "post_url": url, "platform": "reddit"}
        cls = {"intents": ["buyer_demand"], "intent": "buyer_demand", "confidence": conf,
               "from_cache": cached, "commercial_signal": comm,
               "urgency": 0.0, "pain_intensity": 0.0}
        out.append((cand, sim, cls))
    return out


def main():
    weights = opportunity.load_weights()
    print(f"weights.json calibrated flag: {weights.get('calibrated')!r}\n")
    for name, plan in PLANS.items():
        qi = schemas.normalize_query_intent(plan)
        passing = build_passing()
        in_order = [c["post_url"] for c, _, _ in passing]

        # 1) what the bridge actually returns
        ranked = intent_bridge._rank_passing(passing, qi)
        out_order = [c["post_url"] for c in ranked]

        # 2) why: replay the exact triples _rank_passing builds
        triples = []
        for cand, sim, cls in passing:
            doc = {"id": cand["post_url"], "title": cand["title"],
                   "post_text": cand["post_text"], "post_url": cand["post_url"],
                   "platform": "reddit"}
            c = schemas.normalize_classification({
                "intent": cls["intent"], "intent_confidence": cls["confidence"],
                "actor_type": None, "actor_role": None,
                "commercial_signal": cls["commercial_signal"],
                "pain_intensity": 0.0, "urgency": cls["urgency"],
                "specificity": 0.0, "ambiguous": False, "noise": False})
            triples.append((doc, sim, c))
        res = ranker.rank(triples, qi, weights, top_n=len(triples))

        print("=" * 78)
        print(name)
        print("  input  order (similarity):", in_order)
        print("  output order (bridge)    :", out_order)
        print("  ranker candidates kept   :", res["candidates_kept"], "of", res["candidates_in"])
        print("  ranker filtered_reasons  :", res["filtered_reasons"])
        print("  order unchanged?         :", in_order == out_order)
    print()


if __name__ == "__main__":
    main()
