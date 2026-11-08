#!/usr/bin/env python3
"""Classifier evaluation on a LABELED set. Pure, offline, deterministic: imports only signal_intel (no database, no network, no model call).

    python3 evaluation/run_eval.py [--data evaluation/classification_eval_v1.jsonl] [--json out.json] [--show-errors]

IMPORTANT HONESTY NOTES (also printed in the report):
  * The shipped dataset is SYNTHETIC: hand-written by the assistant, not collected from real users. It measures whether the rules do what they
    claim on varied phrasing; it does NOT measure accuracy on real Reddit/Hacker News discussions, and no real-world accuracy is claimed.
  * Small samples give wide uncertainty; a 95% Wilson interval is reported next to every rate.
  * The set was written before the classifier was first run on it (v1 baseline). Fixes made afterwards were derived from the error analysis,
    so v1 is a development set; claims about generalisation need a fresh, untouched set (v2 is provided as a held-out check).
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_intel as si  # noqa: E402

CLASSES = list(si.CLASSES)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def evaluate(rows, query=None):
    conf = {a: {b: 0 for b in CLASSES} for a in CLASSES}
    errors, quote_bad, no_quote_nonGD = [], 0, 0
    for r in rows:
        post = {"title": r["title"], "post_text": r["text"], "post_url": "https://synthetic.invalid/" + r["id"], "platform": "hackernews"}
        c = si.classify_post(post, query)
        pred = c["class"]
        conf[r["label"]][pred] += 1
        if pred != r["label"]:
            errors.append({"id": r["id"], "label": r["label"], "pred": pred, "rule": c.get("rule"), "title": r["title"]})
        q = c.get("quote")
        full = r["title"] + "\n" + r["text"]
        if q and q not in full:
            quote_bad += 1
        if pred != "GENERAL_DISCUSSION" and not q:
            no_quote_nonGD += 1
    n = len(rows)
    correct = sum(conf[c][c] for c in CLASSES)
    per = {}
    for c in CLASSES:
        tp = conf[c][c]
        fp = sum(conf[o][c] for o in CLASSES if o != c)
        fn = sum(conf[c][o] for o in CLASSES if o != c)
        p = tp / (tp + fp) if tp + fp else None
        rc = tp / (tp + fn) if tp + fn else None
        # F1 is 0 whenever the class has examples but precision or recall is 0 or undefined (a class that is NEVER predicted has undefined precision and
        # recall 0: that is a total failure, not a missing value). It is None only for a class with no examples at all.
        if tp + fn == 0:
            f1 = None
        elif p and rc:
            f1 = 2 * p * rc / (p + rc)
        else:
            f1 = 0.0
        per[c] = {"support": tp + fn, "precision": p, "recall": rc, "f1": f1,
                  "precision_ci": wilson(tp, tp + fp) if tp + fp else None, "recall_ci": wilson(tp, tp + fn) if tp + fn else None}
    non_demand = [r for r in rows if r["label"] != "DIRECT_DEMAND"]
    false_buyer = sum(conf[c]["DIRECT_DEMAND"] for c in CLASSES if c != "DIRECT_DEMAND")
    macro = [v["f1"] for v in per.values() if v["f1"] is not None]
    return {"n": n, "accuracy": correct / n, "accuracy_ci": wilson(correct, n), "macro_f1": sum(macro) / len(macro) if macro else None,
            "false_buyer_rate": false_buyer / len(non_demand) if non_demand else None, "false_buyer_count": false_buyer,
            "false_buyer_ci": wilson(false_buyer, len(non_demand)), "per_class": per, "confusion": conf, "errors": errors,
            "quote_not_substring": quote_bad, "classified_without_quote": no_quote_nonGD, "classifier": si.CLASSIFIER_VERSION}


def fmt(x):
    return "n/a" if x is None else f"{x:.2f}"


def report(res, name):
    print(f"\nClassifier {res['classifier']}  dataset={name}  n={res['n']}  (SYNTHETIC, hand-written; not real-world accuracy)")
    lo, hi = res["accuracy_ci"]
    print(f"accuracy {res['accuracy']:.3f} (95% CI {lo:.2f}-{hi:.2f})   macro-F1 {fmt(res['macro_f1'])}")
    lo, hi = res["false_buyer_ci"]
    print(f"false-buyer rate (non-demand labelled DIRECT_DEMAND): {res['false_buyer_count']}/{res['n'] - res['per_class']['DIRECT_DEMAND']['support']} = {fmt(res['false_buyer_rate'])} (95% CI {lo:.2f}-{hi:.2f})")
    print(f"quotes that are not exact substrings: {res['quote_not_substring']}   classified (non-general) without any quote: {res['classified_without_quote']}")
    print(f"\n{'class':20} {'support':>7} {'prec':>6} {'recall':>7} {'F1':>6}")
    for c in CLASSES:
        v = res["per_class"][c]
        print(f"{c:20} {v['support']:7d} {fmt(v['precision']):>6} {fmt(v['recall']):>7} {fmt(v['f1']):>6}")
    print("\nconfusion (rows = label, cols = predicted):")
    print(" " * 20 + "".join(f"{c[:8]:>10}" for c in CLASSES))
    for a in CLASSES:
        print(f"{a:20}" + "".join(f"{res['confusion'][a][b]:10d}" for b in CLASSES))
    print("\nrow-normalised confusion (share of each true class; each row sums to 1.00):")
    print(" " * 20 + "".join(f"{c[:8]:>10}" for c in CLASSES))
    for a in CLASSES:
        n = sum(res["confusion"][a].values())
        print(f"{a:20}" + "".join(f"{(res['confusion'][a][b] / n if n else 0.0):10.2f}" for b in CLASSES))
    errs = sorted(((res["confusion"][a][b], a, b) for a in CLASSES for b in CLASSES if a != b and res["confusion"][a][b]), reverse=True)
    print("\nmost frequent errors (true -> predicted: count):")
    for n, a, b in errs[:5]:
        print(f"  {a} -> {b}: {n}")
    if not errs:
        print("  none")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "classification_eval_v1.jsonl"))
    ap.add_argument("--json")
    ap.add_argument("--show-errors", action="store_true")
    a = ap.parse_args(argv)
    rows = load(a.data)
    res = evaluate(rows)
    report(res, os.path.basename(a.data))
    if a.show_errors:
        print("\nerrors:")
        for e in res["errors"]:
            print(f"  {e['id']} label={e['label']} pred={e['pred']} rule={e['rule']} | {e['title']}")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
