#!/usr/bin/env python3
"""
VALIDATION HARNESS — Experiments 1-6 (spec 9, 10)
===========================================================================
Runs the approved gates against the SAME 866-document real-data corpus
embedding_diagnostic.py measured, and writes a report in the same shape so
the two sit side by side without an asterisk.

    python intent_prototype/validate.py plan     --diag-dir ./diag
    python intent_prototype/validate.py selftest
    python intent_prototype/validate.py classify --diag-dir ./diag   # the paid step
    python intent_prototype/validate.py run      --diag-dir ./diag

GATES (approved)
  1 classifier accuracy       macro F1 >= 0.65, buyer_demand >= 0.55,
                              complaint_pain >= 0.60          [Tier 1 only]
  4 opportunity calibration   group separation >= 0.25
  2 end-to-end ranking        mean nDCG@10 >= 0.45, same-topic AUC >= 0.75,
                              intent-dominance violations < 5%
  3 latency                   MEASURED, not gated
  5 query-level product eval  nDCG improvement >= 0.15, win rate >= 0.70
  6 AND-logic correctness     zero merged strict/related sets

Order: 1 and 4 must pass before 2 is trusted; 2 and 5 both gate production.

GROUND TRUTH DISCIPLINE
  Every accuracy number is computed against Tier 1 HUMAN labels only. The
  866 model-generated labels are used for STRATIFICATION and for weight
  calibration (Tier 2), never as ground truth. Model-vs-human agreement is
  reported as information about the labels, never as a pass/fail.

COST
  `classify` is the only stage that spends money, and it is a separate
  subcommand on purpose. It classifies the corpus ONCE and caches to
  classifications.jsonl; `run` then does grid search, ranking and every
  experiment with zero further API calls. Run `plan` first — it prints the
  exact call count and spends nothing.
"""

import argparse
import itertools
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                                    # noqa: E402

from intent_prototype import (doc_classifier, metrics, opportunity,    # noqa: E402
                              pipeline, probe_queries, query_interpreter,
                              ranker, retriever, schemas)
from intent_prototype.llm import check_keys, embed_texts              # noqa: E402

GATES = {
    "exp1_macro_f1": 0.65,
    "exp1_buyer_demand_f1": 0.55,
    "exp1_complaint_pain_f1": 0.60,
    "exp2_ndcg10": 0.45,
    "exp2_same_topic_auc": 0.75,
    "exp2_dominance_violation_rate": 0.05,
    "exp4_group_separation": 0.25,
    "exp5_ndcg_improvement": 0.15,
    "exp5_win_rate": 0.70,
}

MIN_POSITIVES_FOR_PROBE = 5


def _log(msg=""):
    print(msg, flush=True)


def _die(msg, code=2):
    print(f"FATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


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


def _fmt(v, nd=3):
    if v is None:
        return "  -  "
    if isinstance(v, float) and v != v:
        return " nan "
    return f"{v:.{nd}f}"


def _verdict(value, gate, higher_is_better=True):
    if value is None or (isinstance(value, float) and value != value):
        return "SKIP"
    ok = value >= gate if higher_is_better else value <= gate
    return "PASS" if ok else "FAIL"


# ═══════════════════════════════════════════════════════════════════════
# SHARED LOADING
# ═══════════════════════════════════════════════════════════════════════

def _load_corpus(diag_dir):
    path = os.path.join(diag_dir, "sample.jsonl")
    if not os.path.exists(path):
        _die(f"{path} not found — run `python embedding_diagnostic.py sample` first.")
    docs = retriever.load_corpus(path)
    return docs, {d["id"]: d for d in docs}


def _load_model_labels(diag_dir, by_id):
    path = os.path.join(diag_dir, "labels.jsonl")
    if not os.path.exists(path):
        return {}
    out = {}
    for r in _read_jsonl(path):
        if r.get("id") in by_id:
            out[r["id"]] = {"intent": schemas.map_legacy_intent(r.get("intent")),
                            "intent_raw": r.get("intent"),
                            "topic": r.get("topic") or "other",
                            "ambiguous": bool(r.get("ambiguous"))}
    return out


def _load_tier1(diag_dir):
    path = os.path.join(diag_dir, "tier1_verified.jsonl")
    if not os.path.exists(path):
        return {}
    return {r["id"]: r for r in _read_jsonl(path) if r.get("id") and r.get("intent")}


def _load_tier3(diag_dir):
    path = os.path.join(diag_dir, "tier3_queries.jsonl")
    if not os.path.exists(path):
        return []
    return [r for r in _read_jsonl(path) if r.get("query") and r.get("judgements")]


def _load_classifications(outdir):
    path = os.path.join(outdir, "classifications.jsonl")
    if not os.path.exists(path):
        return {}
    return {r["id"]: r["classification"] for r in _read_jsonl(path) if r.get("id")}


# ═══════════════════════════════════════════════════════════════════════
# PLAN — costs nothing
# ═══════════════════════════════════════════════════════════════════════

def cmd_plan(args):
    _log("=" * 78)
    _log("VALIDATION PLAN — no API calls made by this command")
    _log("=" * 78)

    keys = check_keys()
    _log(f"  ANTHROPIC_API_KEY : {keys['ANTHROPIC_API_KEY']}")
    _log(f"  OPENAI_API_KEY    : {keys['OPENAI_API_KEY']}")
    _log("")

    docs, by_id = _load_corpus(args.diag_dir)
    model_labels = _load_model_labels(args.diag_dir, by_id)
    tier1 = _load_tier1(args.diag_dir)
    tier3 = _load_tier3(args.diag_dir)
    cached = _load_classifications(args.outdir)

    todo = [d for d in docs if d["id"] not in cached]
    calls = doc_classifier.estimate_calls(len(todo))

    _log("INPUTS")
    _log(f"  corpus (sample.jsonl)      : {len(docs):,} docs")
    _log(f"  model labels (labels.jsonl): {len(model_labels):,}  [Tier 2 — NOT ground truth]")
    _log(f"  Tier 1 human labels        : {len(tier1):,}" +
         ("" if tier1 else "   <-- MISSING: Experiments 1, 2, 4 cannot run"))
    _log(f"  Tier 3 judged queries      : {len(tier3):,}" +
         ("" if tier3 else "   <-- MISSING: Experiment 5 cannot run"))
    _log(f"  cached classifications     : {len(cached):,}")
    _log("")
    _log("COST OF `classify`")
    _log(f"  documents needing classification : {len(todo):,}")
    _log(f"  batch size                       : {schemas.CLASSIFIER_BATCH_SIZE}")
    _log(f"  Claude calls                     : {calls:,}")
    _log(f"  approx input tokens              : {len(todo) * 300:,}")
    _log(f"  OpenAI embedding calls           : 1  ({len(probe_queries.PROBES)} probe strings)")
    if args.live_interpreter:
        _log(f"  live interpreter Claude calls    : {len(probe_queries.PROBES)}")
    _log("")
    _log("COST OF `run`")
    _log("  0 API calls — grid search and all experiments reuse the cache.")
    _log("")

    blockers = []
    if keys["ANTHROPIC_API_KEY"] == "MISSING":
        blockers.append("ANTHROPIC_API_KEY not set (needed by `classify`)")
    if keys["OPENAI_API_KEY"] == "MISSING":
        blockers.append("OPENAI_API_KEY not set (needed to embed probe queries)")
    if not tier1:
        blockers.append("tier1_verified.jsonl missing — build it with make_tier1_sample.py")
    if not tier3:
        blockers.append("tier3_queries.jsonl missing — Experiment 5 will be skipped")

    if blockers:
        _log("BLOCKERS")
        for b in blockers:
            _log(f"  - {b}")
    else:
        _log("No blockers. Ready to run.")
    _log("")
    _log("ORDER:  plan -> selftest -> (label Tier 1) -> classify -> run")


# ═══════════════════════════════════════════════════════════════════════
# SELFTEST — costs nothing, proves isolation
# ═══════════════════════════════════════════════════════════════════════

PRODUCTION_MODULES = ("flintel", "logics", "config", "database", "github_signals",
                      "app", "main", "routes", "models")
WRITE_CALLS = ("insert_one", "insert_many", "update_one", "update_many",
               "delete_one", "delete_many", "replace_one", "drop",
               "create_index", "bulk_write", "find_one_and_update",
               "find_one_and_replace", "find_one_and_delete")


def cmd_selftest(args):
    import re
    here = os.path.dirname(os.path.abspath(__file__))
    failures, checks = [], 0

    def check(name, cond, detail=""):
        nonlocal checks
        checks += 1
        if cond:
            _log(f"  ok    {name}")
        else:
            _log(f"  FAIL  {name}  {detail}")
            failures.append(name)

    _log("=" * 78)
    _log("SELFTEST — isolation, read-only guarantee, unit behaviour")
    _log("=" * 78)
    _log("")
    _log("ISOLATION")

    py_files = sorted(f for f in os.listdir(here) if f.endswith(".py"))
    for fname in py_files:
        src = open(os.path.join(here, fname), encoding="utf-8").read()
        bad = []
        for mod in PRODUCTION_MODULES:
            if re.search(rf"^\s*(from\s+{mod}\s+import|import\s+{mod})\b", src, re.M):
                bad.append(mod)
        check(f"{fname} imports no production module", not bad, f"found: {bad}")

    _log("")
    _log("READ-ONLY GUARANTEE")
    for fname in py_files:
        src = open(os.path.join(here, fname), encoding="utf-8").read()
        found = [c for c in WRITE_CALLS if re.search(rf"\.{c}\s*\(", src)]
        check(f"{fname} makes no Mongo write call", not found, f"found: {found}")

    _log("")
    _log("TAXONOMY")
    check("intent vocabulary is non-empty", len(schemas.INTENTS) > 0)
    check("every legacy label maps into the taxonomy",
          all(v in schemas.INTENT_SET for v in schemas.LEGACY_INTENT_MAP.values()))
    check("comparison maps to solution_evaluation",
          schemas.map_legacy_intent("comparison") == "solution_evaluation")

    _log("")
    _log("ACTOR DERIVATION")
    check("company+seeker on hiring -> company_hiring",
          schemas.derive_actor_direction("hiring", "company", "seeker") == "company_hiring")
    check("individual+seeker on hiring -> individual_seeking",
          schemas.derive_actor_direction("hiring", "individual", "seeker") == "individual_seeking")
    check("general_discussion has no direction",
          schemas.derive_actor_direction("general_discussion", "company", "buyer") is None)

    _log("")
    _log("SCHEMA COERCION")
    junk = schemas.normalize_classification({"intent": "nonsense", "intent_confidence": 9})
    check("unknown intent falls back safely", junk["intent"] == "general_discussion")
    check("an unrecognised label keeps no confidence", junk["intent_confidence"] == 0.0)
    check("an unrecognised label is flagged ambiguous", junk["ambiguous"] is True)
    hi = schemas.normalize_classification({"intent": "hiring", "intent_confidence": 9})
    check("confidence is clamped to [0,1]", hi["intent_confidence"] == 1.0)
    check("a confident real label is not ambiguous", hi["ambiguous"] is False)
    lo = schemas.normalize_classification({"intent": "hiring", "intent_confidence": 0.3})
    check("low confidence forces ambiguous", lo["ambiguous"] is True)
    nonpain = schemas.normalize_classification(
        {"intent": "buyer_demand", "intent_confidence": 0.9, "pain_intensity": 0.8})
    check("pain_intensity zeroed off complaint_pain", nonpain["pain_intensity"] == 0.0)
    dup = schemas.normalize_classification(
        {"intent": "hiring", "intent_confidence": 0.9,
         "secondary_intent": "hiring", "secondary_confidence": 0.5})
    check("secondary equal to primary is dropped", dup["secondary_intent"] is None)

    qi = schemas.normalize_query_intent({"intent_include": ["buyer_demand"],
                                         "intent_exclude": ["buyer_demand", "hiring"]})
    check("an included intent is never also excluded",
          "buyer_demand" not in qi["intent_exclude"] and "hiring" in qi["intent_exclude"])
    check("irrelevant is always excluded", "irrelevant" in qi["intent_exclude"])
    oscan = schemas.normalize_query_intent({"query_mode": "opportunity_scan"})
    check("opportunity_scan gets its default tiers",
          bool(oscan["opportunity_scan"])
          and "buyer_demand" in oscan["opportunity_scan"]["primary_intents"])

    _log("")
    _log("INTENT MATCHING")
    cls = schemas.normalize_classification(
        {"intent": "complaint_pain", "intent_confidence": 0.85,
         "secondary_intent": "buyer_demand", "secondary_confidence": 0.60})
    ok, conf, via = ranker.intent_match(cls, ["buyer_demand"], "OR")
    check("secondary match uses SECONDARY confidence",
          ok and abs(conf - 0.60) < 1e-9 and via == "buyer_demand", f"got {conf}")
    ok, conf, _ = ranker.intent_match(cls, ["complaint_pain"], "OR")
    check("primary match uses primary confidence", ok and abs(conf - 0.85) < 1e-9)
    ok, conf, _ = ranker.intent_match(cls, ["complaint_pain", "buyer_demand"], "AND")
    check("AND match is as strong as its weakest intent",
          ok and abs(conf - 0.60) < 1e-9, f"got {conf}")
    ok, _, _ = ranker.intent_match(cls, ["complaint_pain", "hiring"], "AND")
    check("AND fails when one intent is absent", not ok)

    _log("")
    _log("OPPORTUNITY STRENGTH")
    w = opportunity.load_weights()
    doc = {"created_utc": None, "source": "mongo_primary"}
    strong = schemas.normalize_classification(
        {"intent": "buyer_demand", "intent_confidence": 0.9, "commercial_signal": 0.9,
         "specificity": 0.9, "urgency": 0.8, "actor_type": "company", "actor_role": "buyer"})
    chatter = schemas.normalize_classification(
        {"intent": "general_discussion", "intent_confidence": 0.9,
         "commercial_signal": 0.9, "specificity": 0.9, "urgency": 0.9})
    s_strong = opportunity.score(strong, 0.55, doc, None, w)
    s_chat = opportunity.score(chatter, 0.90, doc, None, w)
    check("real demand outscores chatter", s_strong > s_chat,
          f"{s_strong:.3f} vs {s_chat:.3f}")
    check("general_discussion is capped at 0.40", s_chat <= 0.40 + 1e-9, f"{s_chat:.3f}")
    noise = schemas.normalize_classification(
        {"intent": "buyer_demand", "intent_confidence": 0.9, "noise": True})
    check("noise scores zero opportunity",
          opportunity.score(noise, 0.9, doc, None, w) == 0.0)

    _log("")
    _log("HARD FILTERS")
    qi_b = schemas.normalize_query_intent(
        {"intent_include": ["buyer_demand"], "query_mode": "explicit_intent"})
    wrong = schemas.normalize_classification(
        {"intent": "general_discussion", "intent_confidence": 0.95})
    check("wrong intent is filtered, not demoted",
          ranker.hard_filter(wrong, doc, qi_b, 0.9) == "intent_mismatch")
    check("noise is filtered",
          ranker.hard_filter(
              schemas.normalize_classification({"intent": "buyer_demand",
                                                "intent_confidence": 0.9, "noise": True}),
              doc, qi_b, 0.9) == "noise")
    qi_cs = schemas.normalize_query_intent(
        {"intent_include": ["buyer_demand"], "min_commercial_signal": 0.5,
         "query_mode": "explicit_intent"})
    weak = schemas.normalize_classification(
        {"intent": "buyer_demand", "intent_confidence": 0.9, "commercial_signal": 0.2})
    check("below min_commercial_signal is filtered",
          ranker.hard_filter(weak, doc, qi_cs, 0.5) == "below_min_commercial_signal")

    _log("")
    _log("INTENT-DOMINANCE CONSTRAINT")
    # right-intent doc loses on the full score, but wins once the topic term
    # is removed -> topic similarity ALONE rescued the wrong doc. Violation.
    rescued = [
        {"classification": {"intent": "general_discussion"},
         "topic_sim": 0.90, "ranking_score": 0.50},
        {"classification": {"intent": "buyer_demand"},
         "topic_sim": 0.40, "ranking_score": 0.48},
    ]
    d = ranker.check_intent_dominance(rescued, "buyer_demand", topic_weight=0.30)
    check("a wrong doc rescued by topic alone is a violation",
          d["violations"] == 1 and d["rate"] == 1.0, str(d))

    # wrong doc still wins with the topic term removed -> it won on other
    # merits, not on topic. Loose inversion, but NOT a violation.
    earned = [
        {"classification": {"intent": "general_discussion"},
         "topic_sim": 0.90, "ranking_score": 0.80},
        {"classification": {"intent": "buyer_demand"},
         "topic_sim": 0.40, "ranking_score": 0.48},
    ]
    d2 = ranker.check_intent_dominance(earned, "buyer_demand", topic_weight=0.30)
    check("a wrong doc winning on other merits is not a violation",
          d2["violations"] == 0, str(d2))
    check("the loose inversion is still counted for context",
          d2["any_inversion"]["violations"] == 1, str(d2))

    _log("")
    _log("AND LOGIC — never silently merged")
    cands = []
    for i in range(3):
        c = schemas.normalize_classification(
            {"intent": "hiring", "intent_confidence": 0.8,
             "secondary_intent": "complaint_pain", "secondary_confidence": 0.7,
             "actor_type": "company", "actor_role": "seeker"})
        cands.append(({"id": f"both{i}", "created_utc": None,
                       "source": "mongo_primary", "title": "", "post_text": "x"}, 0.5, c))
    for i in range(6):
        c = schemas.normalize_classification(
            {"intent": "hiring", "intent_confidence": 0.8,
             "actor_type": "company", "actor_role": "seeker"})
        cands.append(({"id": f"one{i}", "created_utc": None,
                       "source": "mongo_primary", "title": "", "post_text": "x"}, 0.5, c))
    qi_and = schemas.normalize_query_intent(
        {"intent_include": ["hiring", "complaint_pain"], "intent_logic": "AND",
         "query_mode": "explicit_intent"})
    res = ranker.rank(cands, qi_and, w)
    keys_ = [s["key"] for s in res["sections"]]
    check("strict and related are separate sections",
          keys_ == ["exact_matches", "related_matches"], f"got {keys_}")
    check("and_match_count is reported", res["and_match_count"] == 3)
    check("or_fallback_used is flagged", res["or_fallback_used"] is True)
    exact_ids = {r["doc"]["id"] for r in res["sections"][0]["results"]}
    rel_ids = {r["doc"]["id"] for r in res["sections"][1]["results"]}
    check("no document appears in both sections", not (exact_ids & rel_ids))

    _log("")
    _log("METRICS")
    check("roc_auc of perfect separation is 1.0",
          abs(metrics.roc_auc([3, 4, 5], [0, 1, 2]) - 1.0) < 1e-9)
    check("roc_auc of identical distributions is 0.5",
          abs(metrics.roc_auc([1, 2, 3], [1, 2, 3]) - 0.5) < 1e-9)
    check("macro_f1 of a perfect prediction is 1.0",
          abs(metrics.macro_f1(["a", "b", "a"], ["a", "b", "a"]) - 1.0) < 1e-9)
    check("macro_f1 ignores classes absent from truth",
          abs(metrics.macro_f1(["a", "a"], ["a", "b"]) - 0.6666666) < 1e-3)

    _log("")
    _log("=" * 78)
    _log(f"{checks - len(failures)}/{checks} checks passed")
    if failures:
        _log(f"FAILURES: {failures}")
        _log("=" * 78)
        sys.exit(1)
    _log("SELFTEST PASSED — prototype is isolated and behaves as specified.")
    _log("=" * 78)


# ═══════════════════════════════════════════════════════════════════════
# CLASSIFY — the only paid stage
# ═══════════════════════════════════════════════════════════════════════

def cmd_classify(args):
    docs, by_id = _load_corpus(args.diag_dir)
    os.makedirs(args.outdir, exist_ok=True)
    cached = _load_classifications(args.outdir)

    todo = [d for d in docs if d["id"] not in cached]
    if not todo:
        _log(f"All {len(docs):,} documents already classified. Nothing to do.")
        return

    calls = doc_classifier.estimate_calls(len(todo))
    _log(f"Classifying {len(todo):,} documents "
         f"({calls:,} Claude calls, batch {schemas.CLASSIFIER_BATCH_SIZE})")
    if not args.yes:
        _die("refusing to spend without --yes (run `plan` first to see the cost)")

    path = os.path.join(args.outdir, "classifications.jsonl")
    done = 0

    def flush():
        with open(path, "w", encoding="utf-8") as f:
            for doc_id, cls in cached.items():
                f.write(json.dumps({"id": doc_id, "classification": cls}) + "\n")

    batch = schemas.CLASSIFIER_BATCH_SIZE
    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        results = doc_classifier.classify(chunk, batch_size=batch, model=args.model)
        for d, c in zip(chunk, results):
            cached[d["id"]] = c
        done += len(chunk)
        flush()
        _log(f"  {done:,}/{len(todo):,}")

    dist = Counter(c["intent"] for c in cached.values())
    _log(f"WROTE {len(cached):,} classifications -> {path}")
    _log(f"  intent distribution: {dict(dist.most_common())}")
    _log(f"  ambiguous: {sum(1 for c in cached.values() if c['ambiguous']):,}  "
         f"noise: {sum(1 for c in cached.values() if c['noise']):,}")
    _log("")
    _log(f"NEXT: python intent_prototype/validate.py run --diag-dir {args.diag_dir}")


# ═══════════════════════════════════════════════════════════════════════
# EXPERIMENT 1 — classifier accuracy on Tier 1
# ═══════════════════════════════════════════════════════════════════════

def experiment_1(tier1, classifications, model_labels):
    ids = [i for i in tier1 if i in classifications]
    out = {"n": len(ids), "skipped": not ids}
    if not ids:
        out["reason"] = "no overlap between tier1_verified.jsonl and classifications"
        return out

    y_true = [tier1[i]["intent"] for i in ids]
    y_pred = [classifications[i]["intent"] for i in ids]

    per = metrics.per_class_f1(y_true, y_pred, schemas.INTENTS)
    out.update({
        "macro_f1": metrics.macro_f1(y_true, y_pred, schemas.INTENTS),
        "accuracy": metrics.accuracy(y_true, y_pred),
        "per_class": per,
        "confusion": metrics.confusion(y_true, y_pred, schemas.INTENTS),
    })

    # Secondary intent gives partial credit: a post whose human primary
    # label appears as the classifier's SECONDARY label is not a clean
    # miss, and reporting it as one would understate usable accuracy.
    lenient = sum(1 for i in ids
                  if tier1[i]["intent"] in
                  {classifications[i]["intent"], classifications[i].get("secondary_intent")})
    out["accuracy_incl_secondary"] = lenient / len(ids)

    # Model-vs-human agreement. REPORTED ONLY — the model labels are not
    # ground truth, so disagreement describes the labels, not an error.
    shared = [i for i in ids if i in model_labels]
    if shared:
        out["model_vs_human"] = {
            "n": len(shared),
            "agreement": metrics.accuracy([tier1[i]["intent"] for i in shared],
                                          [model_labels[i]["intent"] for i in shared]),
            "cohen_kappa": metrics.cohen_kappa([tier1[i]["intent"] for i in shared],
                                               [model_labels[i]["intent"] for i in shared]),
        }

    out["gates"] = {
        "macro_f1": (out["macro_f1"], GATES["exp1_macro_f1"],
                     _verdict(out["macro_f1"], GATES["exp1_macro_f1"])),
        "buyer_demand_f1": (per.get("buyer_demand", {}).get("f1"),
                            GATES["exp1_buyer_demand_f1"],
                            _verdict(per.get("buyer_demand", {}).get("f1"),
                                     GATES["exp1_buyer_demand_f1"])),
        "complaint_pain_f1": (per.get("complaint_pain", {}).get("f1"),
                              GATES["exp1_complaint_pain_f1"],
                              _verdict(per.get("complaint_pain", {}).get("f1"),
                                       GATES["exp1_complaint_pain_f1"])),
    }
    out["passed"] = all(v[2] == "PASS" for v in out["gates"].values())
    thin = [c for c, v in per.items() if 0 < v["support"] < 5]
    if thin:
        out["thin_classes"] = thin
    return out


# ═══════════════════════════════════════════════════════════════════════
# EXPERIMENT 4 — opportunity strength calibration
# ═══════════════════════════════════════════════════════════════════════

def experiment_4(tier1, classifications, by_id, weights):
    """Uses the human `opportunity` 0-3 judgement as the reference.

    Group A = human opportunity 3 (clear opportunity)
    Group B = human opportunity 0 (pure discussion)
    Borderline (1-2) is reported but excluded from the separation test,
    exactly as the approved 10/10/10 design intends.
    """
    scored = {i: r for i, r in tier1.items()
              if r.get("opportunity") is not None and i in classifications}
    out = {"n": len(scored), "skipped": len(scored) < 6}
    if out["skipped"]:
        out["reason"] = ("fewer than 6 posts carry a human opportunity_0_3 score; "
                         "fill that column in the Tier 1 sheet")
        return out

    strong_ids = [i for i, r in scored.items() if r["opportunity"] >= 0.99]
    weak_ids = [i for i, r in scored.items() if r["opportunity"] <= 0.01]
    mid_ids = [i for i in scored if i not in strong_ids and i not in weak_ids]

    def score_of(doc_id):
        return opportunity.score(classifications[doc_id], 0.5, by_id[doc_id], None, weights)

    strong = [score_of(i) for i in strong_ids]
    weak = [score_of(i) for i in weak_ids]
    mid = [score_of(i) for i in mid_ids]

    out.update({
        "group_strong": {"n": len(strong), **(metrics.describe(strong) if strong else {})},
        "group_weak": {"n": len(weak), **(metrics.describe(weak) if weak else {})},
        "group_borderline": {"n": len(mid), **(metrics.describe(mid) if mid else {})},
    })

    if not strong or not weak:
        out["skipped"] = True
        out["reason"] = ("need both opportunity=3 and opportunity=0 posts in Tier 1; "
                         f"have {len(strong)} and {len(weak)}")
        return out

    sep = float(np.mean(strong) - np.mean(weak))
    out.update({
        "separation": sep,
        "auc": metrics.roc_auc(strong, weak),
        "cohens_d": metrics.cohens_d(strong, weak),
        "gates": {"separation": (sep, GATES["exp4_group_separation"],
                                 _verdict(sep, GATES["exp4_group_separation"]))},
    })
    out["passed"] = out["gates"]["separation"][2] == "PASS"

    # correlation against the full 0-3 human scale, as a sanity check that
    # the ordering is right and not just the two extremes
    hs = [scored[i]["opportunity"] for i in scored]
    ms = [score_of(i) for i in scored]
    if len(set(hs)) > 1:
        out["correlation_with_human_scale"] = float(np.corrcoef(hs, ms)[0, 1])
    return out


# ═══════════════════════════════════════════════════════════════════════
# EXPERIMENT 2 — end-to-end ranking quality + weight calibration
# ═══════════════════════════════════════════════════════════════════════

def _truth_intent(doc_id, tier1, model_labels):
    """Tier 1 human label when present, else the Tier 2 model label.
    Which source was used is tracked per probe and reported."""
    if doc_id in tier1:
        return tier1[doc_id]["intent"], "human"
    if doc_id in model_labels:
        return model_labels[doc_id]["intent"], "model"
    return None, None


def _build_probe_pools(docs, by_id, classifications, model_labels, live_interpreter,
                       pool_size, sim_floor):
    """Embed every probe once, retrieve once, attach cached classifications.

    The heavy work happens here exactly once; the grid search then re-ranks
    these pools thousands of times with no further embedding or classifying.
    """
    plans = probe_queries.offline_plans()
    if live_interpreter:
        live = []
        for p in probe_queries.PROBES:
            try:
                live.append((p["q"], query_interpreter.interpret(p["q"])))
            except Exception as exc:                        # noqa: BLE001
                _log(f"  interpreter failed on {p['q'][:40]!r} ({type(exc).__name__}); "
                     f"using the offline plan")
                live.append(plans[len(live)])
        plans = live

    queries = [qi["topic_embedding_query"] for _, qi in plans]
    vecs = embed_texts(queries)
    matrix = retriever.corpus_matrix(docs)

    pools = []
    for probe, (q, qi), vec in zip(probe_queries.PROBES, plans, vecs):
        cands, meta = retriever.retrieve_from_corpus(
            qi, docs, matrix=matrix, query_vec=vec, pool=pool_size, sim_floor=sim_floor)
        rows = [(d, s, classifications[d["id"]]) for d, s in cands
                if d["id"] in classifications]
        pools.append({"probe": probe, "query": q, "qi": qi,
                      "candidates": rows, "retrieval": meta,
                      "expect": probe_queries.resolved_expect(probe)})
    return pools


def _precompute_opportunity(pools, weights):
    """Freeze opportunity_strength into each candidate so ranking grid
    search never recomputes it (ranker._score_all honours a preset value)."""
    for p in pools:
        frozen = []
        for doc, sim, cls in p["candidates"]:
            opp = opportunity.score(cls, sim, doc, p["qi"], weights)
            frozen.append((doc, sim, dict(cls, opportunity_strength=opp)))
        p["candidates"] = frozen


def _eval_probe(p, weights, tier1, model_labels, k=10):
    """Rank one probe's pool and score it against the best available truth."""
    result = ranker.rank(p["candidates"], p["qi"], weights, top_n=200)
    rows = ranker.flatten(result)

    truths, sources = [], []
    for r in rows:
        t, src = _truth_intent(r["doc"]["id"], tier1, model_labels)
        truths.append(t)
        sources.append(src)

    expect = p["expect"]
    ranked = [t for t in truths if t is not None]
    n_pos = sum(1 for t in ranked if t == expect)

    out = {
        "query": p["query"], "expect": expect,
        "kept": result["candidates_kept"], "filtered": result["candidates_filtered"],
        "positives_in_pool": n_pos,
        "human_truth_fraction": (sum(1 for s in sources if s == "human") / len(sources)
                                 if sources else 0.0),
        "thin": n_pos < MIN_POSITIVES_FOR_PROBE,
    }
    if out["thin"]:
        return out, result

    out.update({
        "ndcg@10": metrics.ndcg_at_k(ranked, expect, k),
        "p@5": metrics.precision_at_k(ranked, expect, 5),
        "p@10": metrics.precision_at_k(ranked, expect, 10),
        "p@20": metrics.precision_at_k(ranked, expect, 20),
    })

    # same-topic AUC: does a right-intent doc outrank a wrong-intent doc
    # WITHIN the same topic? This is the number the diagnostic measured at
    # 0.542 and the number this whole architecture exists to move.
    topic = p["probe"].get("topic")
    pos, neg = [], []
    for r, t in zip(rows, truths):
        if t is None:
            continue
        doc_topic = (model_labels.get(r["doc"]["id"]) or {}).get("topic")
        if topic and doc_topic != topic:
            continue
        (pos if t == expect else neg).append(r["ranking_score"])
    out["same_topic_auc"] = metrics.roc_auc(pos, neg) if pos and neg else float("nan")
    out["same_topic_n"] = {"pos": len(pos), "neg": len(neg)}

    # the functional constraint from spec 5, measured against TRUTH intents
    # rather than predicted ones — a weight set must not be able to pass by
    # having the classifier agree with itself.
    for r, t in zip(rows, truths):
        r["classification"] = dict(r["classification"], intent=t or r["classification"]["intent"])
    out["dominance"] = ranker.check_intent_dominance(
        rows, expect, float(weights.get("ranking", {}).get("topic_sim", 0.0)))
    return out, result


def _grid_combos(grid):
    keys = [k for k in grid if not k.startswith("_")]
    for values in itertools.product(*[grid[k] for k in keys]):
        yield dict(zip(keys, values))


def _mean(vals):
    vals = [v for v in vals if v is not None and v == v]
    return float(np.mean(vals)) if vals else float("nan")


def experiment_2(pools, weights, tier1, model_labels, do_grid=True, folds=5):
    base = {"ranking": dict(weights.get("ranking", {})), **{k: v for k, v in weights.items()
                                                            if k != "ranking"}}

    # ── grid search (Tier 2 labels; Tier 1 is never tuned on) ──────────
    grid_out = {"ran": False}
    best_ranking = dict(weights.get("ranking", {}))
    if do_grid:
        grid = weights.get("ranking_grid", {})
        combos = list(_grid_combos(grid))
        tunable = [p for p in pools if p["expect"]]
        idx = list(range(len(tunable)))
        fold_of = {i: i % folds for i in idx}

        scored = []
        for combo in combos:
            w = dict(weights)
            w["ranking"] = combo
            per_fold = defaultdict(list)
            viol = []
            for i, p in enumerate(tunable):
                res, _ = _eval_probe(p, w, {}, model_labels)   # Tier 2 only
                if res.get("thin"):
                    continue
                per_fold[fold_of[i]].append(res["ndcg@10"])
                d = res.get("dominance") or {}
                if d.get("pairs"):
                    viol.append(d["rate"])
            fold_means = [_mean(v) for v in per_fold.values() if v]
            if not fold_means:
                continue
            scored.append({
                "weights": combo,
                "ndcg_mean": float(np.mean(fold_means)),
                "ndcg_fold_std": float(np.std(fold_means)) if len(fold_means) > 1 else 0.0,
                "dominance_rate": _mean(viol),
            })

        # The functional constraint is a REJECTION rule, not a tiebreak:
        # a weight set that lets topic similarity rescue wrong-intent
        # results fails regardless of its nDCG.
        legal = [s for s in scored
                 if not (s["dominance_rate"] == s["dominance_rate"])
                 or s["dominance_rate"] <= GATES["exp2_dominance_violation_rate"]]
        pool_for_best = legal or scored
        if pool_for_best:
            best = max(pool_for_best, key=lambda s: s["ndcg_mean"])
            best_ranking = best["weights"]
            grid_out = {
                "ran": True,
                "combinations": len(combos),
                "evaluated": len(scored),
                "legal_under_constraint": len(legal),
                "rejected_by_constraint": len(scored) - len(legal),
                "best": best,
                "top_5": sorted(pool_for_best, key=lambda s: -s["ndcg_mean"])[:5],
                "fold_variance_warning": best["ndcg_fold_std"] > 0.15,
                "tuned_on": "Tier 2 model labels (noisy by design)",
            }

    # ── final evaluation with the chosen weights, on the best truth ────
    final_w = dict(weights)
    final_w["ranking"] = best_ranking

    per_probe, results = [], {}
    for p in pools:
        res, full = _eval_probe(p, final_w, tier1, model_labels)
        per_probe.append(res)
        results[p["query"]] = full

    usable = [r for r in per_probe if not r.get("thin")]
    out = {
        "n_probes": len(per_probe),
        "n_scored": len(usable),
        "n_thin": len(per_probe) - len(usable),
        "per_probe": per_probe,
        "grid": grid_out,
        "chosen_weights": best_ranking,
        "baseline_weights": base["ranking"],
    }
    if not usable:
        out["skipped"] = True
        out["reason"] = "no probe had enough positives in the corpus to score"
        return out, results, final_w

    ndcgs = [r["ndcg@10"] for r in usable]
    aucs = [r["same_topic_auc"] for r in usable]
    rates = [(r.get("dominance") or {}).get("rate") for r in usable
             if (r.get("dominance") or {}).get("pairs")]

    loose = [(r.get("dominance") or {}).get("any_inversion", {}).get("rate")
             for r in usable if (r.get("dominance") or {}).get("pairs")]
    out.update({
        "any_inversion_rate": _mean(loose),
        "mean_ndcg@10": _mean(ndcgs),
        "median_ndcg@10": float(np.median([v for v in ndcgs if v == v])) if ndcgs else float("nan"),
        "mean_p@5": _mean([r["p@5"] for r in usable]),
        "mean_p@10": _mean([r["p@10"] for r in usable]),
        "mean_p@20": _mean([r["p@20"] for r in usable]),
        "median_same_topic_auc": (float(np.median([v for v in aucs if v == v]))
                                  if any(v == v for v in aucs) else float("nan")),
        "dominance_violation_rate": _mean(rates),
        "human_truth_fraction": _mean([r["human_truth_fraction"] for r in usable]),
    })
    out["gates"] = {
        "mean_ndcg@10": (out["mean_ndcg@10"], GATES["exp2_ndcg10"],
                         _verdict(out["mean_ndcg@10"], GATES["exp2_ndcg10"])),
        "median_same_topic_auc": (out["median_same_topic_auc"], GATES["exp2_same_topic_auc"],
                                  _verdict(out["median_same_topic_auc"],
                                           GATES["exp2_same_topic_auc"])),
        "dominance_violation_rate": (out["dominance_violation_rate"],
                                     GATES["exp2_dominance_violation_rate"],
                                     _verdict(out["dominance_violation_rate"],
                                              GATES["exp2_dominance_violation_rate"], False)),
    }
    out["passed"] = all(v[2] == "PASS" for v in out["gates"].values())
    return out, results, final_w


# ═══════════════════════════════════════════════════════════════════════
# EXPERIMENT 3 — latency (measured, never gated)
# ═══════════════════════════════════════════════════════════════════════

def experiment_3(pools, weights, classifications):
    """Measures the stages that can be measured without re-spending.

    Retrieval and ranking are timed for real. Classification is REPORTED
    as call count and per-call cost from the cached run rather than
    re-timed, because re-classifying to time it would spend the budget
    twice for a number the cache already implies. A real end-to-end wall
    clock comes from the live demo, and spec 8 is explicit that a slow
    result is answered with caching or parallelism, never with a smaller
    pool.
    """
    per_query = []
    for p in pools:
        t0 = time.perf_counter()
        ranker.rank(p["candidates"], p["qi"], weights, top_n=20)
        rank_ms = (time.perf_counter() - t0) * 1000
        n = len(p["candidates"])
        per_query.append({
            "query": p["query"],
            "candidates": n,
            "rank_ms": rank_ms,
            "classifier_calls": doc_classifier.estimate_calls(n),
        })

    rank_ms = [q["rank_ms"] for q in per_query]
    calls = [q["classifier_calls"] for q in per_query]
    return {
        "n_queries": len(per_query),
        "rank_ms": {"p50": metrics.percentile(rank_ms, 50),
                    "p95": metrics.percentile(rank_ms, 95)},
        "classifier_calls_per_query": {"p50": metrics.percentile(calls, 50),
                                       "p95": metrics.percentile(calls, 95)},
        "note": ("Classification latency is not re-timed here — it would double the "
                 "API spend for a number the cached run already determines. Measure "
                 "true end-to-end wall clock with `pipeline.run` against live Mongo. "
                 "Spec 8: a slow pipeline is fixed with caching or parallelism, never "
                 "by shrinking the candidate pool."),
        "gated": False,
        "per_query": per_query,
    }


# ═══════════════════════════════════════════════════════════════════════
# EXPERIMENT 5 — query-level product evaluation (Tier 3)
# ═══════════════════════════════════════════════════════════════════════

GAIN = {"relevant": 1.0, "partially_relevant": 0.5, "irrelevant": 0.0}


def experiment_5(tier3, docs, by_id, classifications, weights, model_labels,
                 pool_size, sim_floor, k=10):
    out = {"n_queries": len(tier3), "skipped": not tier3}
    if not tier3:
        out["reason"] = ("tier3_queries.jsonl is missing. Experiment 5 needs 30-50 REAL "
                         "Flintel queries with hand-judged results — it is the only "
                         "experiment that measures product performance rather than "
                         "classifier agreement, and it cannot be synthesised.")
        return out

    matrix = retriever.corpus_matrix(docs)
    plans, queries = [], []
    for rec in tier3:
        qi = (query_interpreter.interpret_offline(rec["query"], rec["plan"])
              if rec.get("plan") else query_interpreter.interpret(rec["query"]))
        plans.append(qi)
        queries.append(qi["topic_embedding_query"])
    vecs = embed_texts(queries)

    rows = []
    for rec, qi, vec in zip(tier3, plans, vecs):
        judged = rec["judgements"]
        cands, _ = retriever.retrieve_from_corpus(
            qi, docs, matrix=matrix, query_vec=vec, pool=pool_size, sim_floor=sim_floor)

        # baseline = today's behaviour: cosine similarity order, no
        # classification, no intent gate.
        base_ids = [d["id"] for d, _ in cands]
        base_gains = [GAIN.get(judged.get(i), 0.0) for i in base_ids]

        proto_rows = ranker.flatten(ranker.rank(
            [(d, s, classifications[d["id"]]) for d, s in cands if d["id"] in classifications],
            qi, weights, top_n=200))
        proto_gains = [GAIN.get(judged.get(r["doc"]["id"]), 0.0) for r in proto_rows]

        all_gains = [GAIN.get(v, 0.0) for v in judged.values()]
        b = metrics.ndcg_graded(base_gains, k, all_gains)
        p = metrics.ndcg_graded(proto_gains, k, all_gains)
        rows.append({
            "query": rec["query"],
            "judged": len(judged),
            "baseline_ndcg@10": b,
            "prototype_ndcg@10": p,
            "delta": (p - b) if (b == b and p == p) else float("nan"),
            "baseline_p@5": float(np.mean([1.0 if g >= 1.0 else 0.0 for g in base_gains[:5]]))
                            if base_gains else float("nan"),
            "prototype_p@5": float(np.mean([1.0 if g >= 1.0 else 0.0 for g in proto_gains[:5]]))
                             if proto_gains else float("nan"),
        })

    deltas = [r["delta"] for r in rows if r["delta"] == r["delta"]]
    wins = sum(1 for d in deltas if d > 0)
    out.update({
        "per_query": rows,
        "mean_baseline_ndcg@10": _mean([r["baseline_ndcg@10"] for r in rows]),
        "mean_prototype_ndcg@10": _mean([r["prototype_ndcg@10"] for r in rows]),
        "mean_improvement": _mean(deltas),
        "win_rate": wins / len(deltas) if deltas else float("nan"),
        "wins": wins, "losses": sum(1 for d in deltas if d < 0),
        "ties": sum(1 for d in deltas if d == 0),
    })
    out["gates"] = {
        "mean_improvement": (out["mean_improvement"], GATES["exp5_ndcg_improvement"],
                             _verdict(out["mean_improvement"], GATES["exp5_ndcg_improvement"])),
        "win_rate": (out["win_rate"], GATES["exp5_win_rate"],
                     _verdict(out["win_rate"], GATES["exp5_win_rate"])),
    }
    out["passed"] = all(v[2] == "PASS" for v in out["gates"].values())
    return out


# ═══════════════════════════════════════════════════════════════════════
# EXPERIMENT 6 — AND-logic correctness (functional, no cost)
# ═══════════════════════════════════════════════════════════════════════

def _mk(doc_id, intent, conf, sec=None, sec_conf=None):
    cls = schemas.normalize_classification({
        "intent": intent, "intent_confidence": conf,
        "secondary_intent": sec, "secondary_confidence": sec_conf,
        "actor_type": "company", "actor_role": "buyer",
        "commercial_signal": 0.6, "specificity": 0.5, "urgency": 0.4,
    })
    doc = {"id": doc_id, "title": doc_id, "post_text": doc_id,
           "created_utc": None, "source": "mongo_primary"}
    return doc, 0.5, cls


def experiment_6(weights):
    cases, failures = [], []

    def case(name, cond, detail=""):
        cases.append({"name": name, "passed": bool(cond), "detail": detail})
        if not cond:
            failures.append(name)

    inc = ["hiring", "complaint_pain"]
    qi = schemas.normalize_query_intent(
        {"intent_include": inc, "intent_logic": "AND", "query_mode": "explicit_intent"})

    # ── sparse strict matches -> labelled fallback, never merged ───────
    cands = [_mk(f"both{i}", "hiring", 0.8, "complaint_pain", 0.7) for i in range(2)]
    cands += [_mk(f"one{i}", "hiring", 0.8) for i in range(8)]
    r = ranker.rank(cands, qi, weights)
    keys = [s["key"] for s in r["sections"]]
    case("sparse AND returns two labelled sections",
         keys == ["exact_matches", "related_matches"], f"got {keys}")
    case("and_match_count is exact", r["and_match_count"] == 2, str(r["and_match_count"]))
    case("or_fallback_used is true when sparse", r["or_fallback_used"] is True)
    ex = {x["doc"]["id"] for x in r["sections"][0]["results"]}
    rel = {x["doc"]["id"] for x in r["sections"][1]["results"]}
    case("sections share no document", not (ex & rel), str(ex & rel))
    case("related section is explicitly labelled",
         "not all" in (r["sections"][1].get("note") or "").lower())
    case("every exact match holds BOTH intents", ex == {"both0", "both1"}, str(ex))

    # ── ample strict matches -> no fallback at all ─────────────────────
    cands = [_mk(f"both{i}", "hiring", 0.8, "complaint_pain", 0.7) for i in range(7)]
    cands += [_mk(f"one{i}", "hiring", 0.8) for i in range(5)]
    r2 = ranker.rank(cands, qi, weights)
    case("ample AND returns exact matches only",
         [s["key"] for s in r2["sections"]] == ["exact_matches"],
         str([s["key"] for s in r2["sections"]]))
    case("or_fallback_used is false when ample", r2["or_fallback_used"] is False)
    case("and_match_count reported when ample", r2["and_match_count"] == 7)

    # ── OR must not acquire AND's structure ────────────────────────────
    qi_or = schemas.normalize_query_intent(
        {"intent_include": inc, "intent_logic": "OR", "query_mode": "explicit_intent"})
    r3 = ranker.rank(cands, qi_or, weights)
    case("OR returns a single unsectioned result set",
         [s["key"] for s in r3["sections"]] == ["results"])
    case("OR leaves and_match_count null", r3["and_match_count"] is None)

    # ── zero strict matches still reports honestly ─────────────────────
    r4 = ranker.rank([_mk(f"one{i}", "hiring", 0.8) for i in range(6)], qi, weights)
    case("zero strict matches is reported as zero", r4["and_match_count"] == 0)
    case("zero strict matches still labels the related set",
         any(s["key"] == "related_matches" for s in r4["sections"]))

    # ── opportunity_scan tiers stay separate ───────────────────────────
    qi_scan = schemas.normalize_query_intent({"query_mode": "opportunity_scan"})
    scan_c = [_mk("buy", "buyer_demand", 0.9), _mk("pain", "complaint_pain", 0.85),
              _mk("q", "question_info", 0.8), _mk("chat", "general_discussion", 0.9)]
    r5 = ranker.rank(scan_c, qi_scan, weights)
    case("opportunity_scan returns two labelled tiers",
         [s["key"] for s in r5["sections"]] == ["direct_opportunities", "emerging_signals"],
         str([s["key"] for s in r5["sections"]]))
    direct = {x["doc"]["id"] for x in r5["sections"][0]["results"]}
    case("direct tier holds the demand and pain posts",
         {"buy", "pain"} <= direct, str(direct))
    case("general_discussion is not a direct opportunity", "chat" not in direct)

    return {"n_cases": len(cases), "failures": failures,
            "passed": not failures, "cases": cases}


# ═══════════════════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════════════════

def _gate_lines(gates, nd=3):
    out = []
    for name, (val, gate, verdict) in gates.items():
        out.append(f"     {verdict:<5} {name:<32} {_fmt(val, nd)}  (gate {gate})")
    return out


def write_report(path, ctx):
    L = []
    A = L.append
    A("=" * 78)
    A("FLINTEL — INTENT PROTOTYPE VALIDATION")
    A("=" * 78)
    A(f"generated      : {ctx['generated']}")
    A(f"corpus         : {ctx['corpus_size']:,} docs  (same sample.jsonl the "
      f"diagnostic measured)")
    A(f"classified     : {ctx['classified']:,}")
    A(f"Tier 1 human   : {ctx['tier1']:,}   [ground truth]")
    A(f"Tier 2 model   : {ctx['tier2']:,}   [tuning only — NOT ground truth]")
    A(f"Tier 3 queries : {ctx['tier3']:,}   [product evaluation]")
    A(f"taxonomy       : {len(schemas.INTENTS)} intents"
      f"{' (usage_adoption retained — see schemas.py)' if 'usage_adoption' in schemas.INTENT_SET else ''}")
    A("")

    # ── 1 ──
    e = ctx["exp1"]
    A("-" * 78)
    A("EXPERIMENT 1 — CLASSIFIER ACCURACY  (Tier 1 human labels only)")
    A("-" * 78)
    if e.get("skipped"):
        A(f"   SKIPPED — {e.get('reason')}")
    else:
        A(f"   n = {e['n']}   accuracy {_fmt(e['accuracy'])}   "
          f"(incl. secondary: {_fmt(e['accuracy_incl_secondary'])})")
        A("")
        A("   class                     prec    rec     F1   support")
        for c in schemas.INTENTS:
            v = e["per_class"].get(c)
            if not v or not v["support"]:
                continue
            A(f"   {c:<24} {v['precision']:.2f}   {v['recall']:.2f}   "
              f"{v['f1']:.2f}   {v['support']:>4}")
        A("")
        for line in _gate_lines(e["gates"]):
            A(line)
        if e.get("thin_classes"):
            A(f"     NOTE  under 5 Tier-1 examples for {e['thin_classes']} — "
              f"those F1s are too noisy to act on.")
        mv = e.get("model_vs_human")
        if mv:
            A("")
            A(f"   model-vs-human agreement : {_fmt(mv['agreement'])} "
              f"(kappa {_fmt(mv['cohen_kappa'])}, n={mv['n']})")
            A("   Reported only. The model labels are not ground truth, so a "
              "disagreement")
            A("   describes the labels — it is never scored as a classifier error.")
        A("")
        A(f"   RESULT: {'PASS' if e['passed'] else 'FAIL'}")
    A("")

    # ── 4 ──
    e = ctx["exp4"]
    A("-" * 78)
    A("EXPERIMENT 4 — OPPORTUNITY STRENGTH CALIBRATION")
    A("-" * 78)
    if e.get("skipped"):
        A(f"   SKIPPED — {e.get('reason')}")
    else:
        A(f"   clear opportunity (human 3): n={e['group_strong']['n']:>3}  "
          f"mean {_fmt(e['group_strong'].get('mean'))}")
        A(f"   pure discussion   (human 0): n={e['group_weak']['n']:>3}  "
          f"mean {_fmt(e['group_weak'].get('mean'))}")
        A(f"   borderline        (human 1-2): n={e['group_borderline']['n']:>3}  "
          f"mean {_fmt(e['group_borderline'].get('mean'))}   [excluded from the test]")
        A("")
        A(f"   separation {_fmt(e['separation'])}   AUC {_fmt(e['auc'])}   "
          f"d {_fmt(e['cohens_d'], 2)}")
        if e.get("correlation_with_human_scale") is not None:
            A(f"   correlation with the full human 0-3 scale: "
              f"{_fmt(e['correlation_with_human_scale'])}")
        A("")
        for line in _gate_lines(e["gates"]):
            A(line)
        A("")
        A(f"   RESULT: {'PASS' if e['passed'] else 'FAIL'}")
    A("")

    # ── 2 ──
    e = ctx["exp2"]
    A("-" * 78)
    A("EXPERIMENT 2 — END-TO-END RANKING QUALITY")
    A("-" * 78)
    if e.get("skipped"):
        A(f"   SKIPPED — {e.get('reason')}")
    else:
        g = e["grid"]
        if g.get("ran"):
            A(f"   grid search: {g['evaluated']:,} of {g['combinations']:,} combinations "
              f"evaluated")
            A(f"     rejected by the intent-dominance constraint: "
              f"{g['rejected_by_constraint']:,}")
            A(f"     tuned on: {g['tuned_on']}")
            A(f"     best nDCG@10 {_fmt(g['best']['ndcg_mean'])} "
              f"(fold sd {_fmt(g['best']['ndcg_fold_std'])})")
            if g.get("fold_variance_warning"):
                A("     ** fold sd > 0.15 — the tuning labels are too noisy to trust "
                  "these")
                A("        weights. Expand Tier 1 before acting on them. **")
        else:
            A("   grid search: not run (--no-grid)")
        A("")
        A("   chosen ranking weights:")
        for k, v in e["chosen_weights"].items():
            A(f"     {k:<30} {v:+.3f}")
        A("")
        A("   query                                            nDCG@10  P@5  P@10  stAUC")
        for r in e["per_probe"]:
            q = r["query"][:46]
            if r.get("thin"):
                A(f"   {q:<46}   (only {r['positives_in_pool']} positives — skipped)")
            else:
                A(f"   {q:<46}   {_fmt(r['ndcg@10']):>6} {_fmt(r['p@5'], 2):>5} "
                  f"{_fmt(r['p@10'], 2):>5} {_fmt(r['same_topic_auc']):>6}")
        A("")
        A(f"   scored {e['n_scored']} of {e['n_probes']} probes "
          f"({e['n_thin']} had too few positives)")
        A(f"   human-labelled fraction of scored documents: "
          f"{_fmt(e['human_truth_fraction'])}")
        if e["human_truth_fraction"] < 0.5:
            A("     ** most ranked documents fall back to Tier 2 model labels. These")
            A("        ranking numbers are indicative, not verified. Expand Tier 1. **")
        A("")
        for line in _gate_lines(e["gates"]):
            A(line)
        A(f"           (for context, the loose any-inversion rate is "
          f"{_fmt(e.get('any_inversion_rate'))} — not a gate;")
        A("            the gate counts only pairs where removing topic similarity")
        A("            flips the order, i.e. topic alone rescued a wrong result.)")
        A("")
        A("   BASELINE FOR COMPARISON — embedding_diagnostic.py, same corpus:")
        A("     topic retrieval AUC vs all docs      0.929")
        A("     intent separation, same-topic        0.542   <- what this must beat")
        A("     intent separation, topic-centered    0.523")
        A("     P@10 on buyer_demand probes          0.100")
        A("")
        A(f"   RESULT: {'PASS' if e['passed'] else 'FAIL'}")
    A("")

    # ── 3 ──
    e = ctx["exp3"]
    A("-" * 78)
    A("EXPERIMENT 3 — LATENCY  (measured, NOT a gate)")
    A("-" * 78)
    A(f"   ranking stage        p50 {_fmt(e['rank_ms']['p50'], 1)} ms   "
      f"p95 {_fmt(e['rank_ms']['p95'], 1)} ms")
    A(f"   classifier calls/query  p50 {_fmt(e['classifier_calls_per_query']['p50'], 0)}   "
      f"p95 {_fmt(e['classifier_calls_per_query']['p95'], 0)}")
    A("")
    for line in _wrap(e["note"], 72):
        A(f"   {line}")
    A("")

    # ── 5 ──
    e = ctx["exp5"]
    A("-" * 78)
    A("EXPERIMENT 5 — QUERY-LEVEL PRODUCT EVALUATION  (Tier 3)")
    A("-" * 78)
    if e.get("skipped"):
        for line in _wrap(e.get("reason", ""), 72):
            A(f"   SKIPPED — {line}" if line == _wrap(e.get("reason", ""), 72)[0]
              else f"             {line}")
    else:
        A(f"   {e['n_queries']} real queries judged by hand")
        A("")
        A(f"   mean nDCG@10   current system {_fmt(e['mean_baseline_ndcg@10'])}"
          f"   prototype {_fmt(e['mean_prototype_ndcg@10'])}")
        A(f"   improvement    {_fmt(e['mean_improvement'])}")
        A(f"   win / loss / tie   {e['wins']} / {e['losses']} / {e['ties']}")
        A("")
        for line in _gate_lines(e["gates"]):
            A(line)
        A("")
        A(f"   RESULT: {'PASS' if e['passed'] else 'FAIL'}")
    A("")

    # ── 6 ──
    e = ctx["exp6"]
    A("-" * 78)
    A("EXPERIMENT 6 — AND-LOGIC AND SECTION CORRECTNESS")
    A("-" * 78)
    A(f"   {e['n_cases'] - len(e['failures'])}/{e['n_cases']} functional cases passed")
    for c in e["cases"]:
        if not c["passed"]:
            A(f"     FAIL  {c['name']}  {c['detail']}")
    A("")
    A(f"   RESULT: {'PASS' if e['passed'] else 'FAIL'}")
    A("")

    # ── verdict ──
    A("=" * 78)
    A("GATE SUMMARY")
    A("=" * 78)
    order = [("1", "classifier accuracy", ctx["exp1"]),
             ("4", "opportunity calibration", ctx["exp4"]),
             ("2", "end-to-end ranking", ctx["exp2"]),
             ("5", "query-level product eval", ctx["exp5"]),
             ("6", "AND-logic correctness", ctx["exp6"])]
    blocking = []
    for num, name, e in order:
        if e.get("skipped"):
            state = "SKIP"
        elif e.get("passed"):
            state = "PASS"
        else:
            state = "FAIL"
        A(f"  {state:<5} Experiment {num} — {name}")
        if state != "PASS" and num in ("1", "2", "4", "5", "6"):
            blocking.append(num)
    A("  n/a   Experiment 3 — latency (measured, not gated)")
    A("")
    if blocking:
        A(f"  NOT READY FOR PRODUCTION — Experiments {', '.join(blocking)} did not pass.")
        A("  Experiments 1 and 4 must pass before 2 is meaningful; 2 and 5 both gate")
        A("  any production change.")
    else:
        A("  ALL GATES PASSED — the implementation plan can go to review.")
    A("=" * 78)

    text = "\n".join(str(x) for x in L)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return text


def _wrap(text, width):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


# ═══════════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════════

def cmd_run(args):
    from datetime import datetime, timezone

    os.makedirs(args.outdir, exist_ok=True)
    docs, by_id = _load_corpus(args.diag_dir)
    model_labels = _load_model_labels(args.diag_dir, by_id)
    tier1 = _load_tier1(args.diag_dir)
    tier3 = _load_tier3(args.diag_dir)
    classifications = _load_classifications(args.outdir)
    weights = opportunity.load_weights(args.weights)

    if not classifications:
        _die("no classifications.jsonl — run `validate.py classify --yes` first "
             "(see `validate.py plan` for the cost).")
    if not tier1:
        _log("WARNING: no tier1_verified.jsonl — Experiments 1, 4 will be skipped and")
        _log("         Experiment 2 will fall back to Tier 2 model labels throughout.")
        _log("")

    _log(f"corpus {len(docs):,} | classified {len(classifications):,} | "
         f"tier1 {len(tier1):,} | tier2 {len(model_labels):,} | tier3 {len(tier3):,}")

    _log("Building probe pools (1 embedding call)")
    pools = _build_probe_pools(docs, by_id, classifications, model_labels,
                               args.live_interpreter, args.pool, args.sim_floor)
    _precompute_opportunity(pools, weights)

    _log("Experiment 1 — classifier accuracy")
    exp1 = experiment_1(tier1, classifications, model_labels)
    _log("Experiment 4 — opportunity calibration")
    exp4 = experiment_4(tier1, classifications, by_id, weights)
    _log("Experiment 2 — ranking quality + weight calibration")
    exp2, results, final_w = experiment_2(pools, weights, tier1, model_labels,
                                          do_grid=not args.no_grid, folds=args.folds)
    _log("Experiment 3 — latency")
    exp3 = experiment_3(pools, final_w, classifications)
    _log("Experiment 5 — query-level product evaluation")
    exp5 = experiment_5(tier3, docs, by_id, classifications, final_w, model_labels,
                        args.pool, args.sim_floor)
    _log("Experiment 6 — AND-logic correctness")
    exp6 = experiment_6(final_w)

    ctx = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "corpus_size": len(docs), "classified": len(classifications),
        "tier1": len(tier1), "tier2": len(model_labels), "tier3": len(tier3),
        "exp1": exp1, "exp2": exp2, "exp3": exp3,
        "exp4": exp4, "exp5": exp5, "exp6": exp6,
    }

    report_path = os.path.join(args.outdir, "prototype_report.txt")
    text = write_report(report_path, ctx)
    _log("")
    print(text)

    with open(os.path.join(args.outdir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(ctx, f, indent=2, default=str)

    # Persist calibrated weights SEPARATELY. weights.json is never
    # overwritten by a run: a calibration must be reviewed before it
    # becomes the configuration anything else reads.
    if exp2.get("grid", {}).get("ran"):
        cal = dict(weights)
        cal["ranking"] = exp2["chosen_weights"]
        cal["calibrated"] = True
        cal["calibrated_at"] = ctx["generated"]
        cal["calibrated_by"] = "validate.py Experiment 2 grid search"
        cal_path = os.path.join(args.outdir, "weights.calibrated.json")
        with open(cal_path, "w", encoding="utf-8") as f:
            json.dump(cal, f, indent=2)
        _log(f"WROTE {cal_path}  (review, then copy over weights.json to adopt)")

    for name, e in (("exp1", exp1), ("exp2", exp2), ("exp4", exp4),
                    ("exp5", exp5), ("exp6", exp6)):
        with open(os.path.join(args.outdir, f"experiment_{name[-1]}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(e, f, indent=2, default=str)

    failures = os.path.join(args.outdir, "failures.csv")
    _write_failures(failures, results, tier1, model_labels)
    _log(f"WROTE {report_path} / metrics.json / experiment_*.json / failures.csv")


def _write_failures(path, results, tier1, model_labels):
    import csv as _csv
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["query", "rank", "doc_id", "truth_intent", "truth_source",
                    "pred_intent", "pred_conf", "secondary", "actor_direction",
                    "topic_sim", "opportunity", "ranking_score", "correct", "title"])
        for query, res in results.items():
            for i, r in enumerate(ranker.flatten(res)[:20], 1):
                doc_id = r["doc"]["id"]
                t, src = _truth_intent(doc_id, tier1, model_labels)
                c = r["classification"]
                w.writerow([query, i, doc_id, t or "", src or "",
                            c["intent"], f"{c['intent_confidence']:.2f}",
                            c.get("secondary_intent") or "",
                            c.get("actor_direction") or "",
                            f"{r['topic_sim']:.4f}",
                            f"{r['opportunity_strength']:.4f}",
                            f"{r['ranking_score']:.4f}",
                            "", (r["doc"].get("title") or "")[:120]])


def main():
    ap = argparse.ArgumentParser(description="Flintel intent prototype validation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--diag-dir", default="./diag")
        p.add_argument("--outdir", default="./diag/prototype_results")

    p = sub.add_parser("plan", help="show inputs and cost; spends nothing")
    common(p)
    p.add_argument("--live-interpreter", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("selftest", help="isolation and unit checks; spends nothing")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("classify", help="classify the corpus once and cache it (COSTS MONEY)")
    common(p)
    p.add_argument("--yes", action="store_true", help="required; confirms the spend")
    p.add_argument("--model", default=None)
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("run", help="run Experiments 1-6 from the cache (no API cost)")
    common(p)
    p.add_argument("--weights", default=None)
    p.add_argument("--pool", type=int, default=schemas.TARGET_CANDIDATE_POOL)
    p.add_argument("--sim-floor", type=float, default=schemas.LOW_CONFIDENCE_SIM_FLOOR)
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--no-grid", action="store_true")
    p.add_argument("--live-interpreter", action="store_true",
                   help="use the real interpreter instead of the frozen offline plans")
    p.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
