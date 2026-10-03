#!/usr/bin/env python3
"""
diagnostics/bridge_padding_probe.py  —  READ-ONLY, OFFLINE (no network, no Mongo)
================================================================================
Question: if only 8 of 100 candidates genuinely pass the intent filter and the
caller asks for 25, what does intent_bridge._run_bridge() hand back?

It patches the three network/Mongo-touching helpers (_interpret, _cache_get,
_cache_save, _classify_parallel) with in-memory fakes and calls the REAL
_run_bridge() / rerank_with_intent() code, so the padding path under test
(_fill_to_n, timeout fallback) is the production code, not a copy.

Run from the repo root:   python diagnostics/bridge_padding_probe.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import intent_bridge as ib                      # noqa: E402
from intent_prototype import schemas            # noqa: E402

N_CAND, N_GENUINE, WANT = 100, 8, 25


def make_candidates():
    return [{"title": f"t{i}", "post_text": f"text {i} " * 10,
             "post_url": f"https://reddit.com/r/x/comments/{i}/", "platform": "reddit"}
            for i in range(N_CAND)]


def fake_classify(posts, qi, cfg):
    out = []
    for p in posts:
        i = int(p["post_url"].rstrip("/").split("/")[-1])
        genuine = i < N_GENUINE            # first 8 are real buyers, rest are chatter
        out.append({"post_url": p["post_url"],
                    "intents": ["buyer_demand" if genuine else "general_discussion"],
                    "confidence": 0.9, "intent": "buyer_demand" if genuine else "general_discussion",
                    "intent_confidence": 0.9, "secondary_intent": None,
                    "commercial_signal": 0.9 if genuine else 0.1, "urgency": 0.0,
                    "pain_intensity": 0.0})
    return out


def main():
    qi = schemas.normalize_query_intent({
        "topic_keywords": ["AI agent"], "topic_embedding_query": "ai agent for business",
        "intent_include": ["buyer_demand"], "intent_exclude": ["provider_supply", "irrelevant"],
        "query_mode": "explicit_intent", "intent_logic": "OR"})

    ib._interpret = lambda q: qi
    ib._cache_get = lambda urls, cfg: {}
    ib._cache_save = lambda res, cfg: None
    ib._classify_parallel = fake_classify
    cands = make_candidates()
    sims = [0.9 - i * 0.004 for i in range(N_CAND)]

    cfg = {"INTENT_BRIDGE_ENABLED": True, "INTENT_BRIDGE_TIMEOUT_SECONDS": 25}

    # 1) normal path: 8 genuine, caller wants 25
    res = ib._run_bridge("find buyers", cands, WANT, ib._load_config(cfg), sims)
    genuine = sum(1 for c in res if int(c["post_url"].rstrip("/").split("/")[-1]) < N_GENUINE)
    print(f"[normal] asked={WANT} genuine_available={N_GENUINE} returned={len(res)} genuine_in_result={genuine}")
    print(f"         => {len(res) - genuine} of {len(res)} returned posts did NOT pass the intent filter (padding)")

    # 2) timeout path: classifier slower than INTENT_BRIDGE_TIMEOUT_SECONDS
    def slow_classify(posts, qi_, cfg_):
        time.sleep(3)
        return fake_classify(posts, qi_, cfg_)
    ib._classify_parallel = slow_classify
    res2 = ib.rerank_with_intent("find buyers", cands, WANT,
                                 topic_sims=sims,
                                 config_overrides={"INTENT_BRIDGE_ENABLED": True,
                                                   "INTENT_BRIDGE_TIMEOUT_SECONDS": 1})
    print(f"[timeout] bridge timeout=1s, classifier=3s -> returned={len(res2)} "
          f"(== all {N_CAND} unfiltered candidates: {len(res2) == N_CAND})")


if __name__ == "__main__":
    main()
