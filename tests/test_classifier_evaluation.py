"""Pins the classifier's measured quality on the LABELED evaluation sets in evaluation/. Both sets are SYNTHETIC (hand-written by the assistant, not
real discussions): these floors prove the rules still do what they claim on varied phrasing; they are NOT real-world accuracy.
v1 is the development set (the rules-3 changes were derived from its error analysis); v2 was written BEFORE those changes and never used to tune them,
so it is the fairer generalisation check. Baselines before rules-3 (rules-2): v1 accuracy 0.632, v2 accuracy 0.542."""
import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("run_eval", ROOT / "evaluation" / "run_eval.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)

V1 = ROOT / "evaluation" / "classification_eval_v1.jsonl"
V2 = ROOT / "evaluation" / "classification_eval_v2_heldout.jsonl"
V3 = ROOT / "evaluation" / "classification_eval_v3_fresh.jsonl"


@pytest.fixture(scope="module")
def r1():
    return ev.evaluate(ev.load(str(V1)))


@pytest.fixture(scope="module")
def r2():
    return ev.evaluate(ev.load(str(V2)))


@pytest.mark.parametrize("path,n", [(V1, 114), (V2, 48), (V3, 48)])
def test_datasets_are_labelled_synthetic_complete_and_balanced_enough(path, n):
    rows = ev.load(str(path))
    assert len(rows) == n and all(r["synthetic"] is True and "NOT real data" in r["note"] for r in rows)
    assert len({r["id"] for r in rows}) == n
    counts = Counter(r["label"] for r in rows)
    assert set(counts) == set(ev.CLASSES) and min(counts.values()) >= 7      # small sets: 7-26 per class, so intervals are wide
    assert all(r["title"].strip() and r["text"].strip() for r in rows)


def test_the_three_sets_are_pairwise_disjoint():
    sets = [{(r["title"], r["text"]) for r in ev.load(str(p))} for p in (V1, V2, V3)]
    assert not (sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2])


def test_v3_is_the_fresh_untuned_benchmark_and_its_floor_is_honest():
    """v3 was written AFTER rules-3 was frozen and evaluated once without any rule change: accuracy 0.729 (95% CI 0.59-0.83), macro-F1 0.71. The floor
    below is that measurement minus a margin, a regression guard, NOT a target to tune toward (do not tune rules against v3)."""
    r = ev.evaluate(ev.load(str(V3)))
    assert r["accuracy"] >= 0.68 and r["false_buyer_count"] == 0 and r["quote_not_substring"] == 0 and r["classified_without_quote"] == 0
    assert r["accuracy_ci"][1] < 0.90                                         # the honest upper bound: the earlier 0.896 headline does not generalise to messy phrasing


def test_development_set_floor(r1):
    assert r1["accuracy"] >= 0.90 and r1["macro_f1"] >= 0.90
    for c in ev.CLASSES:
        assert r1["per_class"][c]["f1"] >= 0.85, c


def test_heldout_set_floor_and_improvement_over_the_previous_classifier(r2):
    assert r2["accuracy"] >= 0.85 and r2["macro_f1"] >= 0.80
    assert r2["accuracy"] > 0.542 + 0.25                                   # rules-2 scored 0.542 on this set before any rule was changed


def test_no_non_demand_post_is_ever_called_a_buyer(r1, r2):
    assert r1["false_buyer_count"] == 0 and r2["false_buyer_count"] == 0


def test_evidence_integrity_every_quote_is_verbatim_and_every_class_has_one(r1, r2):
    for r in (r1, r2):
        assert r["quote_not_substring"] == 0 and r["classified_without_quote"] == 0


def test_precision_of_the_commercial_classes_stays_high(r1, r2):
    for r in (r1, r2):
        for c in ("DIRECT_DEMAND", "COMPETITOR_SIGNAL", "PROBLEM_SIGNAL"):
            assert r["per_class"][c]["precision"] >= 0.85, c


def test_the_report_states_the_limits(capsys):
    ev.report(ev.evaluate(ev.load(str(V2))), V2.name)
    out = capsys.readouterr().out
    assert "SYNTHETIC" in out and "not real-world accuracy" in out and "95% CI" in out


def test_wilson_interval_is_sane():
    lo, hi = ev.wilson(9, 10)
    assert 0.5 < lo < 0.9 < hi <= 1.0 and ev.wilson(0, 0) == (0.0, 0.0)


# ── the evaluator's own arithmetic (independently recomputed) ─────────────────────────────────────────────────────────────────
import math


def _ref_wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


@pytest.mark.parametrize("k,n", [(9, 10), (0, 10), (10, 10), (1, 90), (43, 48), (35, 48)])
def test_wilson_matches_an_independent_implementation_and_the_textbook_value(k, n):
    lo, hi = ev.wilson(k, n)
    rlo, rhi = _ref_wilson(k, n)
    assert abs(lo - rlo) < 1e-9 and abs(hi - rhi) < 1e-9
    assert abs(ev.wilson(9, 10)[0] - 0.596) < 1e-3 and abs(ev.wilson(9, 10)[1] - 0.982) < 1e-3        # the published Wilson interval for 9/10


def _post(r):
    return {"title": r["title"], "post_text": r["text"], "post_url": "https://synthetic.invalid/" + r["id"], "platform": "hackernews"}


@pytest.mark.parametrize("path", [V1, V2])
def test_accuracy_precision_recall_f1_equal_an_independent_recomputation(path):
    import signal_intel as si
    rows = ev.load(str(path))
    pred = [si.classify_post(_post(r))["class"] for r in rows]
    res = ev.evaluate(rows)
    n = len(rows)
    assert abs(res["accuracy"] - sum(1 for r, p in zip(rows, pred) if r["label"] == p) / n) < 1e-12
    f1s = []
    for c in ev.CLASSES:
        tp = sum(1 for r, p in zip(rows, pred) if r["label"] == c and p == c)
        fp = sum(1 for r, p in zip(rows, pred) if r["label"] != c and p == c)
        fn = sum(1 for r, p in zip(rows, pred) if r["label"] == c and p != c)
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else 0.0
        got = res["per_class"][c]
        assert got["support"] == tp + fn and got["precision"] == prec and got["recall"] == rec and abs(got["f1"] - f1) < 1e-12
        f1s.append(f1)
    assert abs(res["macro_f1"] - sum(f1s) / len(f1s)) < 1e-12
    bad = sum(1 for r, p in zip(rows, pred) if r["label"] != "DIRECT_DEMAND" and p == "DIRECT_DEMAND")
    assert res["false_buyer_count"] == bad and res["false_buyer_rate"] == bad / sum(1 for r in rows if r["label"] != "DIRECT_DEMAND")
    for a in ev.CLASSES:                                                                          # the confusion matrix accounts for every example exactly once
        assert sum(res["confusion"][a].values()) == sum(1 for r in rows if r["label"] == a)


def test_a_class_that_is_never_predicted_counts_as_f1_zero_not_as_missing():
    rows = ([{"id": f"a{i}", "label": "PROBLEM_SIGNAL", "title": "A nice day", "text": "The weather is lovely today and I went for a walk."} for i in range(5)]
            + [{"id": f"b{i}", "label": "GENERAL_DISCUSSION", "title": "Garden", "text": "My tomatoes are ripening nicely."} for i in range(5)])
    r = ev.evaluate(rows)
    pc = r["per_class"]["PROBLEM_SIGNAL"]
    assert pc["precision"] is None and pc["recall"] == 0.0 and pc["f1"] == 0.0
    assert r["per_class"]["DIRECT_DEMAND"]["f1"] is None                                           # no examples at all: genuinely undefined
    assert abs(r["macro_f1"] - (0.0 + 2 * 0.5 * 1.0 / 1.5) / 2) < 1e-12                           # the mean covers the two classes that have examples


# ── questions and discussion are never purchase intent without the author's own request for a product ─────────────────────────────────────
import pytest as _pytest
import signal_intel as _si


@_pytest.mark.parametrize("q", ["What is a CRM and how does it work?", "Does anyone know how Stripe fees are calculated?", "Why is my invoice PDF blank?",
                                "How do I export data from Notion?", "Is Notion better than Obsidian?", "Any thoughts on the future of SaaS?",
                                "Who invented the spreadsheet?", "Has anyone used Zapier before?", "Can someone explain what an API is?"])
def test_a_question_without_a_request_for_a_product_is_not_direct_demand(q):
    c = _si.classify_post({"title": q, "post_text": "", "post_url": "https://x.test/1", "platform": "hackernews"}, "crm")
    assert c["class"] != "DIRECT_DEMAND", (q, c["class"])


def test_every_item_separates_what_the_post_says_from_what_is_inferred_and_what_is_unknown():
    r = _si.build_signal_report([{"title": "Anyone know a good invoicing tool?", "post_text": "I need something simple to send invoices and track who has paid.",
                                  "post_url": "https://news.ycombinator.com/item?id=1", "platform": "hackernews"}], query="invoicing")
    it = r["items"][0]
    assert it["confirmed"] and all(x.startswith("Stated in the post") for x in it["confirmed"])        # observation: quoted text
    assert "inferred" in it and "limitations" in it                                                      # inference and caveats kept apart
    assert it["opportunity"]["unknown"]                                                                  # what is NOT known is listed, not assumed
