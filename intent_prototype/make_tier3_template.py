#!/usr/bin/env python3
"""
TIER 3 BUILDER — the query-level product evaluation set (spec 9, Exp 5)
===========================================================================
Tier 1 asks "is the classifier right about this post?". Tier 3 asks the
question that actually decides whether to ship: "for a query a real user
typed, are the results better?". Those are different questions and a good
answer to the first does not imply a good answer to the second.

This tool CANNOT invent the queries. It takes a plain text file of REAL
Flintel queries — one per line, from query logs, user interviews or
support threads — and builds a judging sheet from them. A synthetic query
set would measure nothing, so the file ships empty and this step blocks
until real queries exist.

WHAT IT DOES
  For each query it retrieves the union of
      top-20 by the CURRENT behaviour (cosine similarity, no intent gate)
      top-20 by the PROTOTYPE ranking
  and writes one judging row per (query, document). Judging the union
  rather than each system's own list is what makes the comparison fair:
  the judge never sees which system surfaced a result, so neither can be
  flattered by the judging order.

COST
  One OpenAI embedding call per batch of queries. No Claude calls — it
  reuses the cached classifications. Run `validate.py classify` first.

    python intent_prototype/make_tier3_template.py build \\
        --queries ./diag/real_queries.txt --diag-dir ./diag
    # judge tier3_to_judge.csv by hand, then:
    python intent_prototype/make_tier3_template.py convert \\
        --csv ./diag/tier3_to_judge.csv --out ./diag/tier3_queries.jsonl
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intent_prototype import (opportunity, query_interpreter,          # noqa: E402
                              ranker, retriever, schemas)
from intent_prototype.llm import check_keys, embed_texts               # noqa: E402

JUDGE_COLUMNS = ["query_no", "query", "doc_id", "title", "text",
                 "judgement", "notes"]

INSTRUCTIONS = """\
TIER 3 JUDGING INSTRUCTIONS
==============================================================================
{n_rows} rows across {n_queries} real queries.

For each row, answer ONE question:

    Reading this post, would it be useful EVIDENCE for the query above it?

Put one of these in the `judgement` column:

    relevant             yes — this is the kind of post the query asked for
    partially_relevant   on topic and somewhat useful, but not really what
                         was asked (right subject, wrong kind of post; or
                         right kind but too vague to act on)
    irrelevant           no

WHAT TO IGNORE
  The row order means nothing. Results from both the current system and the
  prototype are mixed together on purpose and are not marked, so you cannot
  tell which produced what. That is deliberate — judge the post against the
  query, not against a system.

THE TEST THAT MATTERS
  For an opportunity query, the split is between someone who HAS the need
  and someone who is merely TALKING about the subject. A thoughtful essay
  on why businesses need AI agents is `irrelevant` to "find businesses
  looking for AI agents" — it is the right topic and the wrong thing. That
  distinction is the entire point of the experiment; if you soften it, the
  experiment measures nothing.

Leave a row blank to skip it. Blank rows are dropped, not counted as
irrelevant.
"""


def _log(msg=""):
    print(msg, flush=True)


def _clean(t, n):
    return " ".join(str(t or "").split())[:n]


def cmd_build(args):
    if not os.path.exists(args.queries):
        sys.exit(
            f"FATAL: {args.queries} not found.\n"
            "  Create it with one REAL Flintel query per line — 30-50 of them,\n"
            "  taken from query logs or user interviews. This set cannot be\n"
            "  invented: Experiment 5 is the only gate that measures product\n"
            "  performance rather than the classifier agreeing with itself."
        )

    queries = [q.strip() for q in open(args.queries, encoding="utf-8")
               if q.strip() and not q.startswith("#")]
    if not queries:
        sys.exit(f"FATAL: {args.queries} contains no queries")
    if len(queries) < 30:
        _log(f"WARNING: only {len(queries)} queries. The spec asks for 30-50; "
             f"fewer makes the win rate noisy.")

    keys = check_keys()
    if keys["OPENAI_API_KEY"] == "MISSING":
        sys.exit("FATAL: OPENAI_API_KEY not set — needed to embed the queries.")

    docs = retriever.load_corpus(os.path.join(args.diag_dir, "sample.jsonl"))
    by_id = {d["id"]: d for d in docs}

    cls_path = os.path.join(args.outdir, "classifications.jsonl")
    if not os.path.exists(cls_path):
        sys.exit(f"FATAL: {cls_path} not found — run `validate.py classify --yes` first.")
    classifications = {}
    with open(cls_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                classifications[r["id"]] = r["classification"]

    weights = opportunity.load_weights(args.weights)
    matrix = retriever.corpus_matrix(docs)

    _log(f"Interpreting {len(queries)} queries "
         f"({'live interpreter' if not args.offline else 'offline, topic-only'})")
    plans = []
    for q in queries:
        if args.offline:
            plans.append(query_interpreter.interpret_offline(q, {
                "topic_keywords": q.split(), "topic_embedding_query": q,
                "intent_include": [], "query_mode": "exploratory",
                "intent_logic": "OR"}))
        else:
            plans.append(query_interpreter.interpret(q))

    _log("Embedding query strings")
    vecs = embed_texts([p["topic_embedding_query"] for p in plans])

    rows, per_query_plans = [], []
    for n, (q, qi, vec) in enumerate(zip(queries, plans, vecs), 1):
        cands, _ = retriever.retrieve_from_corpus(
            qi, docs, matrix=matrix, query_vec=vec,
            pool=args.pool, sim_floor=args.sim_floor)

        baseline_ids = [d["id"] for d, _ in cands[:args.k]]
        proto_rows = ranker.flatten(ranker.rank(
            [(d, s, classifications[d["id"]]) for d, s in cands
             if d["id"] in classifications],
            qi, weights, top_n=args.k))
        proto_ids = [r["doc"]["id"] for r in proto_rows[:args.k]]

        union, seen = [], set()
        for doc_id in baseline_ids + proto_ids:
            if doc_id not in seen and doc_id in by_id:
                seen.add(doc_id)
                union.append(doc_id)
        union.sort()      # kill any ordering cue about which system ranked it

        for doc_id in union:
            d = by_id[doc_id]
            rows.append({"query_no": n, "query": q, "doc_id": doc_id,
                         "title": _clean(d.get("title"), 200),
                         "text": _clean(d.get("post_text"), args.text_chars),
                         "judgement": "", "notes": ""})
        per_query_plans.append({"query": q, "plan": {
            k: qi[k] for k in ("topic_keywords", "topic_embedding_query",
                               "intent_include", "intent_exclude",
                               "actor_direction_filter", "actor_type_filter",
                               "min_commercial_signal", "min_pain_intensity",
                               "min_urgency", "min_specificity",
                               "intent_logic", "query_mode")}})

    os.makedirs(args.outdir, exist_ok=True)
    csv_path = os.path.join(args.diag_dir, "tier3_to_judge.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JUDGE_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    plans_path = os.path.join(args.diag_dir, "tier3_plans.json")
    with open(plans_path, "w", encoding="utf-8") as f:
        json.dump(per_query_plans, f, indent=2)

    ins_path = os.path.join(args.diag_dir, "tier3_INSTRUCTIONS.txt")
    with open(ins_path, "w", encoding="utf-8") as f:
        f.write(INSTRUCTIONS.format(n_rows=len(rows), n_queries=len(queries)))

    per_q = defaultdict(int)
    for r in rows:
        per_q[r["query_no"]] += 1
    _log("")
    _log(f"WROTE {csv_path}   ({len(rows):,} rows, "
         f"{min(per_q.values())}-{max(per_q.values())} per query)")
    _log(f"WROTE {plans_path}  (frozen interpretations — keeps judging reproducible)")
    _log(f"WROTE {ins_path}")
    _log("")
    _log("NEXT: judge the CSV, then:")
    _log(f"  python intent_prototype/make_tier3_template.py convert "
         f"--csv {csv_path} --out {args.diag_dir}/tier3_queries.jsonl")


VALID = {"relevant", "partially_relevant", "irrelevant"}


def cmd_convert(args):
    if not os.path.exists(args.csv):
        sys.exit(f"FATAL: {args.csv} not found")

    plans = {}
    plans_path = os.path.join(os.path.dirname(args.csv), "tier3_plans.json")
    if os.path.exists(plans_path):
        plans = {p["query"]: p["plan"] for p in json.load(open(plans_path, encoding="utf-8"))}

    judged, errors, blank = defaultdict(dict), [], 0
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            q = (row.get("query") or "").strip()
            doc_id = (row.get("doc_id") or "").strip()
            j = (row.get("judgement") or "").strip().lower().replace(" ", "_")
            if not q or not doc_id:
                continue
            if not j:
                blank += 1
                continue
            if j not in VALID:
                errors.append(f"  line {n}: unknown judgement {j!r}")
                continue
            judged[q][doc_id] = j

    if errors:
        _log("VALIDATION ERRORS:")
        for e in errors[:30]:
            _log(e)
    if not judged:
        sys.exit("FATAL: no judged rows. Fill the `judgement` column first.")

    out = []
    for q, js in judged.items():
        rec = {"query": q, "source": "flintel_query_log", "judgements": js}
        if q in plans:
            rec["plan"] = plans[q]
        out.append(rec)

    with open(args.out, "w", encoding="utf-8") as f:
        for rec in out:
            f.write(json.dumps(rec) + "\n")

    counts = defaultdict(int)
    for rec in out:
        for v in rec["judgements"].values():
            counts[v] += 1
    _log(f"WROTE {len(out)} judged queries -> {args.out}")
    _log(f"  judgements: {dict(counts)}")
    if blank:
        _log(f"  {blank} rows skipped (blank judgement)")
    thin = [r["query"] for r in out if not any(v == "relevant"
                                               for v in r["judgements"].values())]
    if thin:
        _log(f"  NOTE: {len(thin)} queries have no `relevant` result at all. nDCG is")
        _log(f"        undefined for those and they will be skipped in Experiment 5.")


def main():
    ap = argparse.ArgumentParser(description="Tier 3 product-evaluation set builder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="real queries -> judging sheet")
    b.add_argument("--queries", default="./diag/real_queries.txt")
    b.add_argument("--diag-dir", default="./diag")
    b.add_argument("--outdir", default="./diag/prototype_results")
    b.add_argument("--weights", default=None)
    b.add_argument("--k", type=int, default=20)
    b.add_argument("--pool", type=int, default=schemas.TARGET_CANDIDATE_POOL)
    b.add_argument("--sim-floor", type=float, default=schemas.LOW_CONFIDENCE_SIM_FLOOR)
    b.add_argument("--text-chars", type=int, default=900)
    b.add_argument("--offline", action="store_true",
                   help="skip the interpreter (topic-only plans, no Claude calls)")
    b.set_defaults(func=cmd_build)

    c = sub.add_parser("convert", help="judged CSV -> tier3_queries.jsonl")
    c.add_argument("--csv", required=True)
    c.add_argument("--out", default="./diag/tier3_queries.jsonl")
    c.set_defaults(func=cmd_convert)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
