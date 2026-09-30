#!/usr/bin/env python3
"""
METRICS
===========================================================================
roc_auc, cohens_d, precision_at_k, ndcg_at_k and describe are reproduced
here with IDENTICAL semantics to embedding_diagnostic.py — same tie
handling, same ideal-DCG definition, same NaN conventions. They are copied
rather than imported so the prototype stays standalone, and so prototype
numbers are directly comparable with the diagnostic's numbers rather than
merely similar.

Added here, not in the diagnostic: macro_f1 / per_class_f1 for the
classifier experiment, and cohen_kappa for human-vs-Claude label agreement.
"""

import math
from collections import Counter

import numpy as np


def roc_auc(pos_scores, neg_scores):
    """P(random positive outranks random negative). Ties credited 0.5."""
    pos = np.asarray(pos_scores, dtype=float)
    neg = np.asarray(neg_scores, dtype=float)
    if pos.size == 0 or neg.size == 0:
        return float("nan")
    allv = np.concatenate([pos, neg])
    order = allv.argsort()
    ranks = np.empty(allv.size, dtype=float)
    ranks[order] = np.arange(1, allv.size + 1, dtype=float)
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


def ndcg_graded(gains_ranked, k, all_gains=None):
    """nDCG for graded relevance — used by the Tier 3 query evaluation,
    where a judge rates relevant / partially_relevant / irrelevant."""
    g = list(gains_ranked)[:k]
    dcg = sum(v / math.log2(i + 2) for i, v in enumerate(g))
    pool = sorted(all_gains if all_gains is not None else gains_ranked, reverse=True)[:k]
    ideal = sum(v / math.log2(i + 2) for i, v in enumerate(pool))
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


def percentile(values, p):
    a = np.asarray([v for v in values if v == v], dtype=float)
    if a.size == 0:
        return float("nan")
    return float(np.percentile(a, p))


# ── classification metrics ────────────────────────────────────────────

def per_class_f1(y_true, y_pred, classes=None):
    """Per-class precision / recall / F1 / support."""
    classes = classes or sorted(set(y_true) | set(y_pred))
    out = {}
    for c in classes:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == c and p == c)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != c and p == c)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == c and p != c)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        out[c] = {"precision": prec, "recall": rec, "f1": f1,
                  "support": sum(1 for t in y_true if t == c)}
    return out


def macro_f1(y_true, y_pred, classes=None):
    """Unweighted mean F1 over classes PRESENT IN y_true. Classes with no
    true examples are excluded — averaging in a 0.0 for a class the held-out
    set never contains would understate accuracy rather than measure it."""
    per = per_class_f1(y_true, y_pred, classes)
    present = [c for c, v in per.items() if v["support"] > 0]
    if not present:
        return float("nan")
    return float(sum(per[c]["f1"] for c in present) / len(present))


def accuracy(y_true, y_pred):
    if not y_true:
        return float("nan")
    return sum(1 for t, p in zip(y_true, y_pred) if t == p) / len(y_true)


def confusion(y_true, y_pred, classes=None):
    classes = classes or sorted(set(y_true) | set(y_pred))
    idx = {c: i for i, c in enumerate(classes)}
    m = [[0] * len(classes) for _ in classes]
    for t, p in zip(y_true, y_pred):
        if t in idx and p in idx:
            m[idx[t]][idx[p]] += 1
    return {"classes": classes, "matrix": m}


def cohen_kappa(a, b):
    """Agreement between two labelers, corrected for chance.

    Used for human-vs-Claude label agreement on Tier 1. It is REPORTED,
    never used to pass or fail anything: Claude's labels are not ground
    truth, so disagreement is information about the labels, not an error.
    """
    if not a or len(a) != len(b):
        return float("nan")
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(a) | set(b))
    if pe >= 1.0:
        return float("nan")
    return float((po - pe) / (1 - pe))
