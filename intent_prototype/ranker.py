#!/usr/bin/env python3
"""
RANKER — hard filters, ranking, result assembly (spec 3, 5, 6, 7)
===========================================================================
PURE FUNCTION. No I/O, no network, no global state. Weights arrive as an
argument so Experiment 2's grid search can sweep thousands of combinations
without touching this file.

TWO-PHASE BY DESIGN
  Phase 1 HARD FILTERS  - binary pass/fail. A document that fails is GONE,
                          not demoted. Wrong intent, wrong actor direction,
                          below a requested strength floor, too old, junk.
  Phase 2 RANKING       - weighted score over the survivors.

That split is the whole point. In the current production system a
wrong-intent document can outrank a right-intent one purely on cosine
similarity; here it cannot be ranked at all, because it never reaches
phase 2. check_intent_dominance() below turns that into a measurable
constraint for Experiment 2.

NO SILENT FALLBACK. AND-logic queries that find too few strict matches
return strict and related matches in SEPARATE, LABELLED sections with
and_match_count / or_fallback_used set. The two sets are never merged.
"""

from datetime import datetime, timezone

from . import opportunity, schemas


# ═══════════════════════════════════════════════════════════════════════
# INTENT MATCHING
# ═══════════════════════════════════════════════════════════════════════

def _matched_intents(cls):
    """Intents this document actually holds, with their confidences."""
    held = {cls["intent"]: float(cls.get("intent_confidence") or 0.0)}
    sec = cls.get("secondary_intent")
    if sec:
        held[sec] = max(held.get(sec, 0.0), float(cls.get("secondary_confidence") or 0.0))
    return held


def intent_match(cls, include, logic="OR"):
    """Does this document satisfy the query's intent requirement?

    Returns (matched: bool, effective_confidence: float, via: str|None).
    effective_confidence is the confidence of the intent that MATCHED —
    secondary_confidence when the match came via the secondary label, per
    spec 3. A document is never credited with its primary confidence for
    an intent it only holds secondarily.
    """
    if not include:
        held = _matched_intents(cls)
        best = max(held.items(), key=lambda kv: kv[1]) if held else (None, 0.0)
        return True, best[1], best[0]

    held = _matched_intents(cls)

    if logic == "AND":
        if not all(i in held for i in include):
            return False, 0.0, None
        # An AND match is only as strong as its weakest required intent.
        weakest = min(include, key=lambda i: held[i])
        return True, held[weakest], weakest

    hits = [(i, held[i]) for i in include if i in held]
    if not hits:
        return False, 0.0, None
    best = max(hits, key=lambda kv: kv[1])
    return True, best[1], best[0]


# ═══════════════════════════════════════════════════════════════════════
# PHASE 1 — HARD FILTERS
# ═══════════════════════════════════════════════════════════════════════

def _age_days(created_utc):
    if not created_utc:
        return None
    try:
        if isinstance(created_utc, datetime):
            dt = created_utc
        else:
            dt = datetime.fromisoformat(str(created_utc).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def hard_filter(cls, doc, qi, opp_strength):
    """Return None if the document survives, else a reason string.

    Reasons are kept so failure analysis can show WHY a document was
    dropped rather than leaving it unexplained.
    """
    if cls.get("noise"):
        return "noise"

    if cls["intent"] in (qi.get("intent_exclude") or []):
        # An exclusion never overrides an explicit request for that intent;
        # normalize_query_intent() already removed such collisions.
        held = _matched_intents(cls)
        include = qi.get("intent_include") or []
        if not any(i in held for i in include):
            return f"intent_excluded:{cls['intent']}"

    include = qi.get("intent_include") or []
    if include:
        ok, _, _ = intent_match(cls, include, qi.get("intent_logic", "OR"))
        if not ok:
            return "intent_mismatch"

    adf = qi.get("actor_direction_filter")
    if adf and cls.get("actor_direction") not in adf:
        return f"actor_direction:{cls.get('actor_direction')}"

    atf = qi.get("actor_type_filter")
    if atf and cls.get("actor_type") not in atf:
        return f"actor_type:{cls.get('actor_type')}"

    for field, key in (("commercial_signal", "min_commercial_signal"),
                       ("pain_intensity", "min_pain_intensity"),
                       ("urgency", "min_urgency"),
                       ("specificity", "min_specificity")):
        floor = qi.get(key)
        if floor is not None and float(cls.get(field) or 0.0) < float(floor):
            return f"below_{key}"

    ts = qi.get("time_scope") or {}
    max_age = ts.get("max_age_days")
    if max_age is not None:
        age = _age_days(doc.get("created_utc"))
        if age is not None and age > float(max_age):
            return "too_old"

    oscan = qi.get("opportunity_scan")
    if oscan:
        held = _matched_intents(cls)
        prim = set(oscan.get("primary_intents") or [])
        sec = set(oscan.get("secondary_intents") or [])
        if held.keys() & prim:
            floor = float(oscan.get("min_opportunity_strength",
                                    schemas.OPPORTUNITY_MIN_PRIMARY))
            tier = "primary"
        elif held.keys() & sec:
            floor = float(oscan.get("min_opportunity_strength_secondary",
                                    schemas.OPPORTUNITY_MIN_SECONDARY))
            tier = "secondary"
        else:
            return "outside_opportunity_scan"
        if opp_strength < floor:
            return f"below_opportunity_floor_{tier}"

    return None


# ═══════════════════════════════════════════════════════════════════════
# PHASE 2 — RANKING
# ═══════════════════════════════════════════════════════════════════════

def ranking_score(cls, doc, topic_sim, opp_strength, eff_conf, matched, weights):
    """Weighted ranking score. All weights come from weights.json."""
    w = weights.get("ranking", {})
    terms = {
        "topic_sim": max(0.0, min(1.0, float(topic_sim or 0.0))),
        "intent_match_x_confidence": (1.0 if matched else 0.0) * float(eff_conf or 0.0),
        "opportunity_strength": float(opp_strength or 0.0),
        "recency": opportunity.recency_score(doc.get("created_utc"),
                                             weights.get("recency", {})),
        "source_quality": opportunity.source_quality_score(doc.get("source"),
                                                           weights.get("source_quality", {})),
    }
    score = sum(float(w.get(k, 0.0)) * v for k, v in terms.items())
    if cls.get("ambiguous"):
        score += float(w.get("ambiguous_penalty", 0.0))
    return score, terms


# ═══════════════════════════════════════════════════════════════════════
# THE FUNCTIONAL CONSTRAINT (spec 5)
# ═══════════════════════════════════════════════════════════════════════

def check_intent_dominance(results, expected_intent, topic_weight):
    """Measure the approved functional constraint (spec 5):

        a document with the WRONG intent must not rank above one with the
        CORRECT intent *solely* because its topic similarity is higher.

    "Solely" is the load-bearing word, and it has to be tested literally.
    Counting every (wrong above right) pair that also has higher similarity
    is far too loose: in any ranking, the lowest-ranked true positive sits
    below plenty of documents, for legitimate reasons — the wrong-intent doc
    scored higher on opportunity, or the right-intent doc was flagged
    ambiguous. Such a pair is not the failure the constraint describes, and
    scoring it as one makes the constraint unsatisfiable for good rankers as
    well as bad ones.

    So a violation here is the counterfactual: strip the topic_sim term from
    both documents' scores, and the ordering FLIPS. Topic similarity, and
    nothing else, is what put the wrong document on top. That is precisely
    the failure mode the diagnostic found in production, and it is the one
    thing this architecture must not reproduce.

    Returns:
      rescued_by_topic  - violations, pairs, rate   <- THE GATE
      any_inversion     - the loose count, reported for context only
    """
    right = [r for r in results if r["classification"]["intent"] == expected_intent]
    wrong = [r for r in results if r["classification"]["intent"] != expected_intent]
    if not right or not wrong:
        return {"violations": 0, "pairs": 0, "rate": 0.0,
                "any_inversion": {"violations": 0, "pairs": 0, "rate": 0.0}}

    def without_topic(row):
        return row["ranking_score"] - topic_weight * row["topic_sim"]

    rescued = 0
    loose = 0
    for w_doc in wrong:
        w_full, w_bare = w_doc["ranking_score"], without_topic(w_doc)
        for r_doc in right:
            if w_full <= r_doc["ranking_score"]:
                continue
            if w_doc["topic_sim"] > r_doc["topic_sim"]:
                loose += 1
                # counterfactual: does removing topic similarity flip it?
                if w_bare < without_topic(r_doc):
                    rescued += 1

    pairs = len(right) * len(wrong)
    return {
        "violations": rescued,
        "pairs": pairs,
        "rate": rescued / pairs if pairs else 0.0,
        "any_inversion": {"violations": loose, "pairs": pairs,
                          "rate": loose / pairs if pairs else 0.0},
    }


# ═══════════════════════════════════════════════════════════════════════
# RESULT ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════

def _score_all(candidates, qi, weights):
    """candidates: list of (doc, topic_sim, classification). Returns
    (kept, dropped) where kept rows carry every computed sub-score.

    CONTRACT: opportunity_strength is owned by opportunity.py. A
    classification arriving with it already set is trusted and reused —
    that is what lets Experiment 2's grid search sweep thousands of
    RANKING weight combinations without recomputing opportunity for every
    one. schemas.normalize_classification() always sets it to None, so a
    fresh classification is always computed here.
    """
    kept, dropped = [], []
    logic = qi.get("intent_logic", "OR")
    include = qi.get("intent_include") or []

    for doc, sim, cls in candidates:
        opp = cls.get("opportunity_strength")
        if opp is None:
            opp = opportunity.score(cls, sim, doc, qi, weights)
        cls = dict(cls, opportunity_strength=opp)

        reason = hard_filter(cls, doc, qi, opp)
        if reason:
            dropped.append({"doc": doc, "topic_sim": sim,
                            "classification": cls, "filtered_reason": reason})
            continue

        matched, eff_conf, via = intent_match(cls, include, logic)
        score, terms = ranking_score(cls, doc, sim, opp, eff_conf, matched, weights)
        kept.append({
            "doc": doc,
            "topic_sim": sim,
            "classification": cls,
            "matched_via": via,
            "effective_confidence": eff_conf,
            "opportunity_strength": opp,
            "ranking_score": score,
            "ranking_terms": terms,
        })

    kept.sort(key=lambda r: r["ranking_score"], reverse=True)
    return kept, dropped


def _interleave_by_intent(rows, order):
    """Round-robin across intent groups so an opportunity scan returns a
    diverse set rather than 20 buyer_demand posts and nothing else."""
    groups = {}
    for r in rows:
        groups.setdefault(r["classification"]["intent"], []).append(r)
    for g in groups.values():
        g.sort(key=lambda r: r["ranking_score"], reverse=True)
    ordered = [i for i in order if i in groups] + [i for i in groups if i not in order]
    out, idx = [], 0
    while any(idx < len(groups[i]) for i in ordered):
        for i in ordered:
            if idx < len(groups[i]):
                out.append(groups[i][idx])
        idx += 1
    return out


def rank(candidates, qi, weights, top_n=20):
    """Assemble the final, sectioned result set.

    candidates : list of (doc, topic_sim, classification)
    qi         : QueryIntent
    weights    : parsed weights.json
    """
    kept, dropped = _score_all(candidates, qi, weights)

    result = {
        "query_mode": qi.get("query_mode"),
        "intent_logic": qi.get("intent_logic", "OR"),
        "candidates_in": len(candidates),
        "candidates_kept": len(kept),
        "candidates_filtered": len(dropped),
        "filtered_reasons": _reason_counts(dropped),
        "and_match_count": None,
        "or_fallback_used": False,
        "sections": [],
        "dropped": dropped,
    }

    mode = qi.get("query_mode")
    include = qi.get("intent_include") or []

    # ── AND logic — strict and related NEVER merged ────────────────────
    if qi.get("intent_logic") == "AND" and len(include) > 1:
        strict = [r for r in kept
                  if all(i in _matched_intents(r["classification"]) for i in include)]
        result["and_match_count"] = len(strict)
        result["sections"].append({
            "key": "exact_matches",
            "title": f"Exact matches ({len(strict)} found)",
            "note": "Every post here expresses ALL requested intents.",
            "results": strict[:top_n],
        })
        if len(strict) < schemas.AND_STRICT_MIN_RESULTS:
            strict_ids = {id(r) for r in strict}
            related = [r for r in kept if id(r) not in strict_ids][:top_n]
            result["or_fallback_used"] = True
            result["sections"].append({
                "key": "related_matches",
                "title": f"Related matches ({len(related)} found)",
                "note": ("These posts match at least one of the requested intent "
                         "classes but not all simultaneously."),
                "results": related,
            })
        return result

    # ── opportunity_scan — two labelled tiers ──────────────────────────
    if mode == "opportunity_scan":
        oscan = qi.get("opportunity_scan") or {}
        prim = set(oscan.get("primary_intents") or [])
        direct, emerging = [], []
        for r in kept:
            held = set(_matched_intents(r["classification"]))
            (direct if held & prim else emerging).append(r)
        order = oscan.get("primary_intents") or []
        result["sections"].append({
            "key": "direct_opportunities",
            "title": f"Direct opportunities ({len(direct)} found)",
            "note": "Active demand, pain, evaluation or switching.",
            "results": _interleave_by_intent(direct, order)[:top_n],
        })
        result["sections"].append({
            "key": "emerging_signals",
            "title": f"Emerging signals ({len(emerging)} found)",
            "note": ("Questions, adoption notes and trends that carry real "
                     "opportunity evidence. Held to a higher bar than direct "
                     "opportunities."),
            "results": _interleave_by_intent(
                emerging, oscan.get("secondary_intents") or [])[:top_n],
        })
        return result

    # ── exploratory — grouped, nothing suppressed ──────────────────────
    if mode == "exploratory":
        groups = {}
        for r in kept:
            groups.setdefault(r["classification"]["intent"], []).append(r)
        for intent_name in sorted(groups, key=lambda k: -len(groups[k])):
            rows = sorted(groups[intent_name],
                          key=lambda r: r["ranking_score"], reverse=True)
            result["sections"].append({
                "key": f"intent:{intent_name}",
                "title": f"{intent_name} ({len(rows)} found)",
                "note": None,
                "results": rows[:max(3, top_n // 4)],
            })
        result["intent_breakdown"] = {k: len(v) for k, v in groups.items()}
        return result

    # ── explicit_intent / OR ───────────────────────────────────────────
    result["sections"].append({
        "key": "results",
        "title": f"Results ({len(kept)} found)",
        "note": None,
        "results": kept[:top_n],
    })
    return result


def _reason_counts(dropped):
    counts = {}
    for d in dropped:
        key = d["filtered_reason"].split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def flatten(result):
    """All ranked rows across sections, de-duplicated, in section order.
    Used by the metrics code, which scores one ordered list per query."""
    seen, out = set(), []
    for sec in result.get("sections", []):
        for r in sec.get("results", []):
            key = r["doc"].get("id")
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    return out
