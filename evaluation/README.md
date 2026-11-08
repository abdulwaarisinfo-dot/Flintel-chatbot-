# Flintel intelligence evaluation

Offline, deterministic, no database / network / model call. `python3 evaluation/run_eval.py [--data FILE] [--show-errors] [--json out.json]` and
`python3 evaluation/benchmark.py [N]`.

| File | What it is |
|---|---|
| `classification_eval_v1.jsonl` | 114 hand-written, **synthetic** labeled posts (24 / 22 / 20 / 22 / 26 across the five classes). The **development** set: the rules-3 changes were derived from its error analysis. |
| `classification_eval_v2_heldout.jsonl` | 48 hand-written, **synthetic** labeled posts written **before** any rules-3 change and never used to tune it. Same author and session as the rules-3 work, so it is **not truly independent** (see v3). |
| `classification_eval_v3_fresh.jsonl` | 48 hand-written, **synthetic** posts written after rules-3 was frozen and evaluated **once** with no rule change. The most honest estimate available here. |
| `run_eval.py` | per-class precision / recall / F1, accuracy and macro-F1 with 95% Wilson intervals, confusion matrix, false-buyer rate, quote-integrity checks |
| `benchmark.py` | micro-benchmarks of the pure paths (classification, report, merge/dedup, canonical URL, discovery detector) |

## What the numbers are NOT
Both sets were written by the same author as the rules, are small, and are synthetic. They show the rules behave as designed on varied phrasing; they are
**not** an accuracy measurement on real Reddit or Hacker News discussions, and no real-world accuracy is claimed. Wide intervals reflect the small samples.
To measure real accuracy, label a random sample of real stored posts (at least a few hundred, two independent labelers, agreement reported) and add it as a
third file; `run_eval.py` accepts any file in this format.

## Results (classifier rules-3)
| Set | rules-2 (before) | rules-3 (after) |
|---|---|---|
| v1 development: accuracy (95% CI) / macro-F1 | 0.632 (0.54-0.71) / 0.64 | 0.947 (0.89-0.98) / 0.95 |
| v2 held-out: accuracy (95% CI) / macro-F1 | 0.542 (0.40-0.67) / 0.51 | **0.896 (0.78-0.95) / 0.89** |
| false-buyer rate (non-demand labeled DIRECT_DEMAND) v1 / v2 | 1/90 / 0/36 | 0/90 / 0/36 |
| quotes that are not exact substrings; classified without any quote | 0; 0 | 0; 0 |
Held-out v2 per class after the change: DIRECT_DEMAND F1 0.96, PROBLEM_SIGNAL 0.67 (recall 0.50, the weakest), MARKET_SIGNAL 1.00, COMPETITOR_SIGNAL 1.00,
GENERAL_DISCUSSION 0.83 (precision 0.71). The remaining v2 errors were deliberately **not** fixed, to keep v2 held out.


## Post-mission review addendum (v3, fresh set)

v2 was authored by the same hand as the rules, so its 0.896 overstates generalisation. On the fresh v3 set (evaluated once, no tuning):
accuracy **0.729 (95% CI 0.59-0.83)**, macro-F1 0.71, false-buyer rate 0/35 (95% CI 0.00-0.10).
Per class F1: DIRECT_DEMAND 0.87, PROBLEM_SIGNAL 0.75, MARKET_SIGNAL 0.67, COMPETITOR_SIGNAL 0.60 (recall 0.43), GENERAL_DISCUSSION 0.69 (precision 0.52).
The dominant error is a real signal being demoted to GENERAL_DISCUSSION (missed recall), not a non-buyer being promoted: the classifier is conservative.
These are synthetic, hand-written sets; none of these numbers is real-world accuracy. Use v1/v2/v3 as a regression benchmark (floors in tests/test_classifier_evaluation.py), and do not tune against v3.
The evaluator now scores a never-predicted class with support as F1 = 0 (previously it was dropped from macro-F1).

## v4 (platform-fix probe set, added in the final master execution)
`classification_eval_v4_platformfix.jsonl`: 35 synthetic items written to probe the patterns seen in the "AI SaaS agent" report (vendor announcements,
open discussion, research versus request, switching/comparison). **Written by the same author as the rules, so optimistic;** evaluated once, no rule
change. Accuracy 0.943 (95% CI 0.81-0.98), macro-F1 0.95, false-buyer rate 0/27. Errors: "We switched from HubSpot to Pipedrive" (no product word, the
product-context gate keeps it general) and "Our company culture post-mortem" (read as an announcement). v3 (accuracy 0.729) remains the best estimate; this
set does not replace it. Not real-world accuracy.
