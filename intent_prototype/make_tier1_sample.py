#!/usr/bin/env python3
"""
TIER 1 SELECTION — build the human-verification set (spec 9)
===========================================================================
Selects the posts a human must label by hand, and writes them in a form
that is actually labelable: a spreadsheet, a readable markdown sheet, and
an empty JSONL in exactly the shape validate.py consumes.

NO API CALLS. NO COST. Reads ./diag/sample.jsonl + ./diag/labels.jsonl and
writes local files. Run it first, label at leisure, then run validate.py.

TWO RULES THIS TOOL ENFORCES
----------------------------------------------------------------------
1. THE CLAUDE LABEL IS NEVER SHOWN TO THE LABELER.
   It is written to a separate manifest file that the labeling sheet does
   not contain. Showing it would anchor the human to it, and the whole
   point of Tier 1 is an INDEPENDENT judgement. Agreement between the two
   is measured afterwards and reported as information about the labels —
   never as a pass/fail, because Claude's labels are not ground truth.

2. THE SAMPLE IS NOT DRAWN FROM CLAUDE'S LABELS ALONE.
   Stratifying purely by Claude's label would only ever measure its
   PRECISION: a post Claude wrongly filed as general_discussion when it is
   really buyer_demand could never appear in the buyer_demand stratum, so
   its RECALL error would be invisible. A random stratum is therefore
   mixed in, drawn uniformly from the whole corpus regardless of label, so
   Claude's misses can surface.

3. THE POST IS SHOWN EXACTLY AS STORED.
   No truncation, no whitespace collapsing, no summarising. Intent often
   turns on the last line of a long post, and collapsing newlines destroys
   the structure that separates a job ad from a job hunt. Every sheet this
   writes carries the stored text byte for byte.

ALLOCATION NOTE
----------------------------------------------------------------------
The budget is 150. The three gated classes alone (buyer_demand,
complaint_pain, hiring) hold 180 posts in the corpus, so full coverage of
all three does not fit and no allocation can make it fit. Instead:

  pass 1  every class gets --floor picks, so each is scoreable at all
  pass 2  the remainder is split ROUND-ROBIN across the three gated
          classes, so none is measured on a thinner sample than the others

A strict priority-ordered fill was tried first and rejected: it let
buyer_demand swallow the whole remainder and left seven classes with no
stratified coverage, which matters because macro F1 — the headline gate —
averages over every class present in the truth set.

The coverage table is printed before any labeling starts, so the trade-off
at this budget is visible rather than discovered later.
"""

import argparse
import csv
import json
import os
import random
import sys
from collections import Counter, OrderedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intent_prototype import schemas   # noqa: E402

RANDOM_SEED = 1337

# Priority order for the stratified portion. Highest product value and
# hardest to classify first; these are the classes whose F1 gates the
# whole project.
_PRIORITY_ORDER = [
    "buyer_demand",
    "complaint_pain",
    "hiring",
    "solution_evaluation",
    "alternative_switching",
    "provider_supply",
    "competitor_research",
    "usage_adoption",
    "trend_signal",
    "question_info",
    "general_discussion",
    "irrelevant",
]
# Filtered against the live taxonomy, so flipping COLLAPSE_USAGE_ADOPTION
# cannot leave this list allocating budget to a class that no longer exists.
PRIORITY_CLASSES = [c for c in _PRIORITY_ORDER if c in schemas.INTENT_SET]
PRIORITY_CLASSES += [c for c in schemas.INTENTS if c not in PRIORITY_CLASSES]

RANDOM_STRATUM_FRACTION = 0.30

# The three classes with their own F1 gate in Experiment 1. They share the
# leftover budget round-robin so none is measured on a thinner sample than
# the others.
GATED_CLASSES = ["buyer_demand", "complaint_pain", "hiring"]

LABEL_SHEET_COLUMNS = [
    "row",
    "doc_id",
    "source",
    "platform",
    "created_utc",
    "post_url",
    "title",
    "text",
    "intent",
    "secondary_intent",
    "actor_type",
    "actor_role",
    "commercial_signal_0_3",
    "pain_intensity_0_3",
    "urgency_0_3",
    "specificity_0_3",
    "opportunity_0_3",
    "ambiguous_yn",
    "noise_yn",
    "notes",
]

INSTRUCTIONS = """\
TIER 1 LABELING INSTRUCTIONS
==============================================================================
{n} posts. Budget roughly 45-75 seconds each; 2-3 hours total. Label in one
or two sittings if you can — consistency matters more than speed.

You are the ground truth. There is no model label in this sheet on purpose.
Do not look one up. If you are unsure, mark ambiguous and move on; "unsure"
is real data, a guess is not.

FILL THESE COLUMNS
------------------------------------------------------------------------------
intent            REQUIRED. Exactly one, from the list below.
secondary_intent  Only when the post genuinely expresses a SECOND intent that
                  would stand on its own. Leave EMPTY if you are merely unsure
                  which single label fits — use ambiguous_yn for that.
actor_type        company | individual | analyst | unknown
actor_role        buyer | seller | seeker | provider | observer | unknown
                  A company job ad -> company + seeker.
                  A person wanting a job -> individual + seeker.
                  A vendor pitching -> company + seller.
                  Someone ranking tools for others -> analyst + observer.

THE FOUR 0-3 SCORES. They measure DIFFERENT things — score each on its own.
  commercial_signal_0_3  How REAL and ACTIVE is the commercial activity?
      0 none / pure opinion / third-person generality
      1 hypothetical or vague personal interest
      2 real situation, some stake
      3 concrete: budget, deadline, named requirement, decision being made
  pain_intensity_0_3     How SEVERE is the problem? 0 unless it is a complaint.
      0 no complaint   1 mild annoyance
      2 real obstruction   3 money lost / production broken / stuck for days
  urgency_0_3            How much TIME PRESSURE?
      0 none stated   1 loose intent   2 soon / this quarter   3 hard deadline
  specificity_0_3        How CONCRETE is the requirement or situation?
      0 vague   1 general direction   2 some specifics   3 named numbers,
      integrations, constraints
  opportunity_0_3        YOUR OVERALL CALL: would a vendor reading this find a
                         real opportunity here, or is this just discussion?
      0 pure discussion   1 weak signal   2 worth a look   3 clear opportunity
      This one is deliberately a holistic judgement, not a formula. It is what
      Experiment 4 calibrates opportunity_strength against.

ambiguous_yn      y when you could not commit to the intent with confidence.
noise_yn          y ONLY for spam, referral/invite codes, link dumps, scraped
                  boilerplate. This is a confident "this is junk", not "unsure".
notes             Anything odd. Especially useful when you disagree with the
                  taxonomy itself — that is a finding, not a nuisance.

THE INTENT VOCABULARY — {n_intents} labels, use these exact strings
------------------------------------------------------------------------------
{vocab}

WHEN TWO LABELS BOTH SEEM RIGHT
------------------------------------------------------------------------------
Ask what the author is DOING, not what they are talking about.

{tiebreaks}

  hiring is either direction. A company hiring and a person job-hunting are
  BOTH `hiring`. The direction goes in actor_type/actor_role, never in intent.

WHEN YOU ARE DONE
------------------------------------------------------------------------------
Save the CSV, keep the column names, and hand it back. It converts to
tier1_verified.jsonl with:

    python intent_prototype/make_tier1_sample.py convert \\
        --csv {outdir}/tier1_to_label.csv \\
        --out {outdir}/tier1_verified.jsonl
"""


def _log(msg):
    print(msg, flush=True)


def _load(path, what):
    if not os.path.exists(path):
        sys.exit(f"FATAL: {path} not found — {what}")
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    if not rows:
        sys.exit(f"FATAL: {path} is empty")
    return rows


def _exact(value):
    """Pass the stored value through UNCHANGED.

    The labeling sheet must show the post exactly as the corpus holds it —
    no truncation, no whitespace collapsing, no summarising. A labeler
    judging intent needs the real thing: a buying signal often sits in the
    last line of a long post, and collapsed newlines destroy the structure
    that separates a job ad from a job hunt. The csv module quotes embedded
    newlines and commas correctly, so a multi-line post survives the round
    trip intact.
    """
    return value if isinstance(value, str) else ("" if value is None else str(value))


# ═══════════════════════════════════════════════════════════════════════
# SELECT
# ═══════════════════════════════════════════════════════════════════════

def cmd_select(args):
    docs = _load(os.path.join(args.diag_dir, "sample.jsonl"),
                 "run `python embedding_diagnostic.py sample` first")
    labels = _load(os.path.join(args.diag_dir, "labels.jsonl"),
                   "run `python embedding_diagnostic.py label` first")

    by_id = {d["id"]: d for d in docs}
    lbl = {r["id"]: r for r in labels if r.get("id") in by_id}
    _log(f"corpus: {len(docs):,} docs, {len(lbl):,} with a model label")

    rng = random.Random(RANDOM_SEED)
    n_random = int(round(args.n * RANDOM_STRATUM_FRACTION))
    n_strat = args.n - n_random

    # ── stratum A: random, label-blind. Catches the model's MISSES. ────
    all_ids = sorted(by_id)
    rng.shuffle(all_ids)
    random_ids = all_ids[:n_random]
    chosen = OrderedDict((i, "random") for i in random_ids)

    # ── stratum B: stratified by model label. Guarantees coverage of
    #    the rare, high-value classes the F1 gate depends on. ──────────
    pools = {}
    for doc_id, rec in lbl.items():
        mapped = schemas.map_legacy_intent(rec.get("intent"))
        pools.setdefault(mapped, []).append(doc_id)
    for c in pools:
        pools[c].sort()
        rng.shuffle(pools[c])

    # Two passes, because a single priority-ordered fill starves the tail.
    # macro F1 averages over every class present in the truth set, so a
    # class with two examples does not merely go unmeasured — it injects
    # noise into the headline number the gate is set on.
    #
    #   pass 1  every class gets a FLOOR, so each one is scoreable at all
    #   pass 2  whatever is left goes to the priority classes, which are
    #           the ones with their own individual gates
    shortfall = {}
    taken = Counter()
    remaining = n_strat

    for cls in PRIORITY_CLASSES:
        pool = [i for i in pools.get(cls, []) if i not in chosen]
        want = min(len(pool), args.floor, max(0, remaining))
        for i in pool[:want]:
            chosen[i] = cls
        taken[cls] += want
        remaining -= want

    # Pass 2 is round-robin, not a priority-ordered fill. Filling in order
    # lets the first class swallow the whole remainder — buyer_demand would
    # take all 48 and leave hiring on its floor — even though all three of
    # these classes carry their own individual F1 gate and need comparable
    # coverage to be measured fairly.
    def _round_robin(classes, budget):
        nonlocal remaining
        while budget > 0:
            progressed = False
            for cls in classes:
                if budget <= 0:
                    break
                pool = [i for i in pools.get(cls, []) if i not in chosen]
                cap = (args.per_class_cap - taken[cls]) if args.per_class_cap else len(pool)
                if not pool or cap <= 0:
                    continue
                chosen[pool[0]] = cls
                taken[cls] += 1
                budget -= 1
                remaining -= 1
                progressed = True
            if not progressed:
                break

    _round_robin(GATED_CLASSES, remaining)
    _round_robin([c for c in PRIORITY_CLASSES if c not in GATED_CLASSES], remaining)

    # Record the shortfall for EVERY class, including ones the budget never
    # reached — a class with no stratified picks is exactly the one whose
    # per-class F1 will be unusable, so it has to be visible.
    for cls in PRIORITY_CLASSES:
        avail = len(pools.get(cls, []))
        if avail > taken[cls]:
            shortfall[cls] = {"missed": avail - taken[cls],
                              "taken": taken[cls], "available": avail}

    # top up from anything still unpicked if priority classes ran dry
    if remaining > 0:
        for i in all_ids:
            if remaining <= 0:
                break
            if i not in chosen:
                chosen[i] = "topup"
                remaining -= 1

    selected = list(chosen)
    rng.shuffle(selected)      # kill label-order effects in the sheet

    os.makedirs(args.outdir, exist_ok=True)

    # ── the labeling sheet — NO model label anywhere in it ────────────
    csv_path = os.path.join(args.outdir, "tier1_to_label.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_SHEET_COLUMNS)
        w.writeheader()
        for n, doc_id in enumerate(selected, 1):
            d = by_id[doc_id]
            w.writerow({
                "row": n,
                "doc_id": doc_id,
                "source": _exact(d.get("source")),
                "platform": _exact(d.get("platform")),
                "created_utc": _exact(d.get("created_utc")),
                "post_url": _exact(d.get("post_url")),
                "title": _exact(d.get("title")),
                "text": _exact(d.get("post_text")),
                **{c: "" for c in LABEL_SHEET_COLUMNS[8:]},
            })

    # ── readable sheet, for labeling away from a spreadsheet ──────────
    md_path = os.path.join(args.outdir, "tier1_to_label.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Tier 1 — {len(selected)} posts to label by hand\n\n")
        f.write("Post text is reproduced EXACTLY as stored — nothing truncated, "
                "nothing reworded.\n")
        f.write("See `tier1_INSTRUCTIONS.txt`. Record answers in "
                "`tier1_to_label.csv`.\n\n---\n\n")
        for n, doc_id in enumerate(selected, 1):
            d = by_id[doc_id]
            f.write(f"## {n}. `{doc_id}`\n\n")
            meta = [f"row **{n}**",
                    f"source: `{d.get('source') or '-'}`",
                    f"platform: `{d.get('platform') or '-'}`",
                    f"posted: `{d.get('created_utc') or '-'}`"]
            f.write(f"{' | '.join(meta)}\n\n")
            if d.get("post_url"):
                f.write(f"<{d['post_url']}>\n\n")
            if d.get("title"):
                f.write(f"**TITLE:** {_exact(d['title'])}\n\n")
            # fenced so the original line breaks and markdown characters in
            # the post cannot be swallowed by the renderer
            f.write("```text\n")
            f.write(_exact(d.get("post_text")).replace("```", "``​`"))
            f.write("\n```\n\n---\n\n")

    # ── the selected documents, verbatim, for anything else ───────────
    raw_path = os.path.join(args.outdir, "tier1_posts.jsonl")
    with open(raw_path, "w", encoding="utf-8") as f:
        for n, doc_id in enumerate(selected, 1):
            d = dict(by_id[doc_id])
            d.pop("embedding", None)       # 1536 floats, useless to a human
            f.write(json.dumps({"row": n, **d}, ensure_ascii=False) + "\n")

    # ── manifest: model labels, kept OUT of the labeling sheet ────────
    manifest_path = os.path.join(args.outdir, "tier1_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "_warning": ("Model labels, for AGREEMENT MEASUREMENT ONLY after "
                         "human labeling is complete. Do not show this file to "
                         "the labeler and do not use it as ground truth."),
            "seed": RANDOM_SEED,
            "n": len(selected),
            "random_stratum": n_random,
            "stratified": len(selected) - n_random,
            "selection": {i: chosen[i] for i in selected},
            "model_labels": {i: schemas.map_legacy_intent(lbl[i]["intent"])
                             for i in selected if i in lbl},
            "model_labels_raw": {i: lbl[i]["intent"] for i in selected if i in lbl},
        }, f, indent=2)

    # ── empty target file in the exact shape validate.py reads ────────
    tmpl_path = os.path.join(args.outdir, "tier1_verified_TEMPLATE.jsonl")
    with open(tmpl_path, "w", encoding="utf-8") as f:
        for doc_id in selected[:2]:
            f.write(json.dumps({
                "id": doc_id, "intent": "", "secondary_intent": None,
                "actor_type": "", "actor_role": "",
                "commercial_signal": None, "pain_intensity": None,
                "urgency": None, "specificity": None, "opportunity": None,
                "ambiguous": False, "noise": False, "notes": "",
            }) + "\n")

    vocab = schemas.render_intent_definitions("  ")
    tiebreaks = schemas.render_intent_tiebreaks("  ")
    ins_path = os.path.join(args.outdir, "tier1_INSTRUCTIONS.txt")
    with open(ins_path, "w", encoding="utf-8") as f:
        f.write(INSTRUCTIONS.format(n=len(selected), vocab=vocab,
                                    tiebreaks=tiebreaks, n_intents=len(schemas.INTENTS),
                                    outdir=args.outdir))

    picked = Counter(chosen[i] for i in selected)
    _log("")
    _log(f"SELECTED {len(selected)} posts")
    _log(f"  random stratum (label-blind) : {picked.get('random', 0)}")
    _log("  stratified by model label:")
    for cls in PRIORITY_CLASSES:
        if picked.get(cls):
            avail = len(pools.get(cls, []))
            _log(f"    {cls:<24} {picked[cls]:>3} of {avail} available")
    if picked.get("topup"):
        _log(f"    {'(top-up)':<24} {picked['topup']:>3}")
    # A class needs a handful of examples before its F1 means anything.
    # Count what each class will actually have, random stratum included.
    final_counts = Counter()
    for doc_id in selected:
        if doc_id in lbl:
            final_counts[schemas.map_legacy_intent(lbl[doc_id]["intent"])] += 1
    thin_budget, thin_corpus = {}, {}
    for c in schemas.INTENTS:
        got, avail = final_counts.get(c, 0), len(pools.get(c, []))
        if got >= 5:
            continue
        (thin_corpus if avail < 5 else thin_budget)[c] = (got, avail)

    _log("")
    _log("  expected per-class coverage (stratified + random):")
    for c in schemas.INTENTS:
        got, avail = final_counts.get(c, 0), len(pools.get(c, []))
        flag = "" if got >= 5 else ("   <- corpus limit" if avail < 5 else "   <- THIN")
        _log(f"    {c:<24} {got:>3} of {avail:>4}{flag}")

    if thin_budget:
        _log("")
        _log("  THIN AT THIS BUDGET — under 5 examples although the corpus has more:")
        for c, (got, avail) in sorted(thin_budget.items(), key=lambda kv: kv[1][0]):
            _log(f"    {c:<24} {got} selected, {avail} available")
        _log("    Their per-class F1 will be noise, and macro F1 averages over every")
        _log("    class present — so this moves the headline gate too. Raise --n.")
    if thin_corpus:
        _log("")
        _log("  CORPUS LIMIT — the 866-doc sample simply does not contain 5 of these:")
        for c, (got, avail) in sorted(thin_corpus.items(), key=lambda kv: kv[1][1]):
            _log(f"    {c:<24} {avail} in the whole corpus")
        _log("    No budget fixes this. Either accept that these classes go unmeasured,")
        _log("    or sample more documents targeted at them before labeling.")
    _log("")
    _log("WROTE")
    _log(f"  {csv_path}")
    _log("      ^-- LABEL IN THIS FILE. Post text is exactly as stored.")
    _log(f"  {md_path}")
    _log("      readable version of the same 150 posts")
    _log(f"  {raw_path}")
    _log("      the same posts as JSONL, verbatim (embeddings stripped)")
    _log(f"  {ins_path}")
    _log("      read this first")
    _log(f"  {tmpl_path}")
    _log("      shape the convert step produces")
    _log(f"  {manifest_path}")
    _log("      model labels — DO NOT OPEN until labeling is finished")
    _log("")
    _log("NO API CALLS WERE MADE. This step is free.")
    _log("")
    _log("NEXT: label the CSV, then:")
    _log(f"  python intent_prototype/make_tier1_sample.py convert "
         f"--csv {csv_path} --out {args.outdir}/tier1_verified.jsonl")


# ═══════════════════════════════════════════════════════════════════════
# CONVERT
# ═══════════════════════════════════════════════════════════════════════

def _score01(v):
    """0-3 bucket -> 0.0-1.0. Blank stays None so 'not scored' is
    distinguishable from 'scored zero'."""
    s = str(v).strip()
    if s == "":
        return None
    try:
        n = int(float(s))
    except ValueError:
        return None
    return max(0.0, min(1.0, n / 3.0))


def _yn(v):
    return str(v).strip().lower() in ("y", "yes", "true", "1")


def cmd_convert(args):
    if not os.path.exists(args.csv):
        sys.exit(f"FATAL: {args.csv} not found")

    out, errors, blank = [], [], 0
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            doc_id = (row.get("doc_id") or "").strip()
            intent = (row.get("intent") or "").strip()
            if not doc_id:
                continue
            if not intent:
                blank += 1
                continue
            if intent not in schemas.INTENT_SET:
                errors.append(f"  line {n}: unknown intent {intent!r}")
                continue
            sec = (row.get("secondary_intent") or "").strip() or None
            if sec and sec not in schemas.INTENT_SET:
                errors.append(f"  line {n}: unknown secondary_intent {sec!r}")
                sec = None
            if sec == intent:
                sec = None
            atype = (row.get("actor_type") or "").strip() or "unknown"
            arole = (row.get("actor_role") or "").strip() or "unknown"
            if atype not in schemas.ACTOR_TYPE_SET:
                errors.append(f"  line {n}: unknown actor_type {atype!r}")
                atype = "unknown"
            if arole not in schemas.ACTOR_ROLE_SET:
                errors.append(f"  line {n}: unknown actor_role {arole!r}")
                arole = "unknown"
            out.append({
                "id": doc_id,
                "intent": intent,
                "secondary_intent": sec,
                "actor_type": atype,
                "actor_role": arole,
                "actor_direction": schemas.derive_actor_direction(intent, atype, arole),
                "commercial_signal": _score01(row.get("commercial_signal_0_3")),
                "pain_intensity": _score01(row.get("pain_intensity_0_3")),
                "urgency": _score01(row.get("urgency_0_3")),
                "specificity": _score01(row.get("specificity_0_3")),
                "opportunity": _score01(row.get("opportunity_0_3")),
                "ambiguous": _yn(row.get("ambiguous_yn")),
                "noise": _yn(row.get("noise_yn")),
                "notes": (row.get("notes") or "").strip(),
            })

    if errors:
        _log("VALIDATION ERRORS — fix these in the CSV and rerun:")
        for e in errors[:40]:
            _log(e)
        if len(errors) > 40:
            _log(f"  ... and {len(errors) - 40} more")

    if not out:
        sys.exit("FATAL: no labeled rows found. Fill the `intent` column first.")

    with open(args.out, "w", encoding="utf-8") as f:
        for rec in out:
            f.write(json.dumps(rec) + "\n")

    dist = Counter(r["intent"] for r in out)
    _log(f"WROTE {len(out)} verified labels -> {args.out}")
    if blank:
        _log(f"  {blank} rows skipped (intent column still blank)")
    _log(f"  distribution: {dict(dist.most_common())}")
    _log(f"  ambiguous: {sum(1 for r in out if r['ambiguous'])}  "
         f"noise: {sum(1 for r in out if r['noise'])}")
    thin = [c for c, n in dist.items() if n < 5]
    if thin:
        _log(f"  NOTE: under 5 examples for {thin} — per-class F1 for these "
             f"will be too noisy to act on.")


def main():
    ap = argparse.ArgumentParser(description="Tier 1 human-verification set builder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("select", help="pick the posts and write the labeling sheets")
    s.add_argument("--diag-dir", default="./diag")
    s.add_argument("--outdir", default="./diag")
    s.add_argument("--n", type=int, default=150,
                   help="labeling budget (default 150); the coverage table this "
                        "prints shows what each class actually gets")
    s.add_argument("--floor", type=int, default=8,
                   help="minimum stratified picks per class before the priority "
                        "classes take the remainder")
    s.add_argument("--per-class-cap", type=int, default=0,
                   help="max per stratified class; 0 = no cap")
    s.set_defaults(func=cmd_select)

    c = sub.add_parser("convert", help="labeled CSV -> tier1_verified.jsonl")
    c.add_argument("--csv", required=True)
    c.add_argument("--out", default="./diag/tier1_verified.jsonl")
    c.set_defaults(func=cmd_convert)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
