#!/usr/bin/env python3
"""
PIPELINE — thin orchestrator (spec 10)
===========================================================================
    query -> interpret -> retrieve -> classify -> opportunity -> rank

Holds no logic of its own beyond wiring and the short-circuit rule from
spec 8. Every decision lives in the module that owns it, so an experiment
can swap one stage without touching the others.

Reads nothing from production. Writes nothing anywhere.
"""

import time

from . import doc_classifier, opportunity, query_interpreter, ranker, retriever, schemas


def _shortcircuit_eligible(qi):
    """Spec 8 — all three conditions must hold."""
    return (qi.get("query_mode") == "explicit_intent"
            and len(qi.get("intent_include") or []) == 1
            and qi.get("intent_logic", "OR") == "OR")


def run(query,
        corpus=None,
        corpus_matrix=None,
        weights=None,
        query_intent=None,
        pool=None,
        top_n=20,
        use_shortcircuit=True,
        classifier_model=None,
        interpreter_model=None,
        progress=None):
    """Run the full pipeline for one query.

    corpus  : pre-loaded sample.jsonl docs -> offline corpus retrieval.
              None -> live read-only MongoDB retrieval.
    query_intent : a pre-built QueryIntent skips the interpreter call.
                   validate.py uses this so ranking experiments are
                   deterministic and an interpreter change cannot silently
                   move ranking numbers.
    """
    weights = weights or opportunity.load_weights()
    timings = {}

    # ── 1. interpret ───────────────────────────────────────────────────
    t0 = time.perf_counter()
    qi = query_intent or query_interpreter.interpret(query, model=interpreter_model)
    timings["interpret_ms"] = (time.perf_counter() - t0) * 1000

    # ── 2. retrieve (topic only) ───────────────────────────────────────
    t0 = time.perf_counter()
    if corpus is not None:
        candidates, rmeta = retriever.retrieve_from_corpus(
            qi, corpus, matrix=corpus_matrix, pool=pool)
    else:
        candidates, rmeta = retriever.retrieve_from_mongo(qi, pool=pool)
    timings["retrieve_ms"] = (time.perf_counter() - t0) * 1000

    # ── 3. classify ────────────────────────────────────────────────────
    t0 = time.perf_counter()
    docs = [d for d, _ in candidates]
    sims = [s for _, s in candidates]
    shortcircuited = False

    if use_shortcircuit and _shortcircuit_eligible(qi) and len(docs) > schemas.SHORTCIRCUIT_HEAD:
        head_n = schemas.SHORTCIRCUIT_HEAD
        head_cls = doc_classifier.classify(docs[:head_n], model=classifier_model,
                                           progress=progress)
        include = qi["intent_include"]
        strong = sum(1 for c in head_cls
                     if ranker.intent_match(c, include, "OR")[0]
                     and (c.get("intent_confidence") or 0.0) >= schemas.SHORTCIRCUIT_MIN_CONFIDENCE)
        if strong >= schemas.SHORTCIRCUIT_MIN_PASSING:
            classifications = head_cls
            docs, sims = docs[:head_n], sims[:head_n]
            shortcircuited = True
        else:
            tail_cls = doc_classifier.classify(docs[head_n:], model=classifier_model,
                                               progress=progress)
            classifications = head_cls + tail_cls
    else:
        classifications = doc_classifier.classify(docs, model=classifier_model,
                                                  progress=progress)
    timings["classify_ms"] = (time.perf_counter() - t0) * 1000

    # ── 4 + 5. opportunity + rank ──────────────────────────────────────
    t0 = time.perf_counter()
    result = ranker.rank(list(zip(docs, sims, classifications)), qi, weights, top_n=top_n)
    timings["rank_ms"] = (time.perf_counter() - t0) * 1000

    timings["total_ms"] = sum(v for k, v in timings.items() if k.endswith("_ms"))
    result.update({
        "query": query,
        "query_intent": qi,
        "retrieval": rmeta,
        "shortcircuited": shortcircuited,
        "classifier_calls": doc_classifier.estimate_calls(len(docs)),
        "timings_ms": timings,
    })
    return result


def format_result(result, max_per_section=10, show_scores=True):
    """Human-readable rendering for manual review and shadow-mode diffing."""
    qi = result.get("query_intent") or {}
    lines = [
        "=" * 78,
        f"QUERY: {result.get('query')}",
        "=" * 78,
        f"  mode            : {qi.get('query_mode')}",
        f"  embedding query : {qi.get('topic_embedding_query')}",
        f"  intent_include  : {qi.get('intent_include') or '(none — all intents)'}",
        f"  intent_logic    : {qi.get('intent_logic')}",
        f"  actor filter    : {qi.get('actor_direction_filter') or '-'}",
        f"  candidates      : {result.get('candidates_in')} in, "
        f"{result.get('candidates_kept')} kept, {result.get('candidates_filtered')} filtered",
        f"  filtered by     : {result.get('filtered_reasons')}",
    ]
    if result.get("and_match_count") is not None:
        lines.append(f"  and_match_count : {result['and_match_count']}  "
                     f"or_fallback_used: {result.get('or_fallback_used')}")
    if result.get("shortcircuited"):
        lines.append("  shortcircuit    : yes (top-50 sufficed)")
    if (result.get("retrieval") or {}).get("low_confidence_retrieval"):
        lines.append("  ** LOW-CONFIDENCE RETRIEVAL — few documents cleared the "
                     "similarity floor **")

    for sec in result.get("sections", []):
        lines.append("")
        lines.append(f"  --- {sec['title']} ---")
        if sec.get("note"):
            lines.append(f"      {sec['note']}")
        for i, r in enumerate(sec.get("results", [])[:max_per_section], 1):
            c = r["classification"]
            title = (r["doc"].get("title") or r["doc"].get("post_text") or "")[:88]
            title = title.replace("\n", " ")
            head = f"   {i:>2}."
            if show_scores:
                head += (f" [rank {r['ranking_score']:.3f} | sim {r['topic_sim']:.3f}"
                         f" | opp {r['opportunity_strength']:.3f}]")
            lines.append(head)
            lines.append(f"       ({c['intent']}/{c['intent_confidence']:.2f}"
                         + (f" +{c['secondary_intent']}/{c['secondary_confidence']:.2f}"
                            if c.get("secondary_intent") else "")
                         + f" | {c.get('actor_direction') or c['actor_type']}"
                         + f" | cs {c['commercial_signal']:.2f}"
                         + f" sp {c['specificity']:.2f}"
                         + f" ur {c['urgency']:.2f}"
                         + (f" pn {c['pain_intensity']:.2f}" if c['pain_intensity'] else "")
                         + ") " + title)
    t = result.get("timings_ms", {})
    if t:
        lines.append("")
        lines.append(f"  timings(ms): interpret {t.get('interpret_ms', 0):.0f} | "
                     f"retrieve {t.get('retrieve_ms', 0):.0f} | "
                     f"classify {t.get('classify_ms', 0):.0f} | "
                     f"rank {t.get('rank_ms', 0):.0f} | "
                     f"TOTAL {t.get('total_ms', 0):.0f}")
    return "\n".join(lines)
