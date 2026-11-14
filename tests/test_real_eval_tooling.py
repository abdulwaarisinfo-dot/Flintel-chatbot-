"""The real-data evaluation TOOLING (evaluation/real_eval/real_eval.py), tested on tiny SYNTHETIC FAKE fixtures. These tests say nothing about lead quality: the real
evaluation (100 collected, independently labelled public conversations) has not been done."""
import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evaluation" / "real_eval"))
import real_eval as R  # noqa: E402

FAKE_NOTE = "FAKE synthetic fixture for tooling tests; not a real conversation"


def fake_posts():
    rows = []
    for i in range(12):
        rows.append({"id": f"p{i:02d}", "business": "plumber" if i % 2 else "saas", "platform": "reddit", "url": f"https://www.reddit.com/r/fake/comments/f{i}/x/",
                     "published_at": "2026-11-01T10:00:00+00:00", "title": f"fake title {i}", "text": ("I need a plumber for a leaking pipe. " if i % 2 else "just chatting. ") + FAKE_NOTE})
    return rows


def write_posts(tmp):
    p = tmp / "posts.jsonl"
    R.write_jsonl(p, fake_posts())
    return p


def fill(sheet_path, labels):
    rows = list(csv.DictReader(open(sheet_path, newline="", encoding="utf-8")))
    with open(sheet_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=R.SHEET_COLUMNS)
        w.writeheader()
        for r in rows:
            r["label"], r["intent_type"] = labels[r["id"]]
            w.writerow(r)


def label_maps(disagree_on=()):
    a, b = {}, {}
    for i in range(12):
        pid = f"p{i:02d}"
        lab = ("lead", "direct_request") if i % 2 else ("not_lead", "irrelevant")
        a[pid] = lab
        b[pid] = ("review", "ambiguous") if pid in disagree_on else lab
    return a, b


def test_wilson_and_kappa_on_known_values():
    assert R.wilson(0, 10) == (0.0, 0.278) and R.wilson(10, 10) == (0.722, 1.0) and R.wilson(0, 0) is None
    assert R.cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    assert R.cohen_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == 0.0
    assert R.cohen_kappa([], []) is None and R.cohen_kappa(["a", "a"], ["a", "a"]) is None


def test_label_sheets_contain_the_post_and_no_flintel_output(tmp_path):
    posts = write_posts(tmp_path)
    out = R.cmd_sheets(posts, tmp_path)
    for s in out["sheets"]:
        text = Path(s).read_text(encoding="utf-8")
        header = text.splitlines()[0]
        assert header.split(",") == R.SHEET_COLUMNS
        assert not any(w in header.lower() for w in ("prediction", "flintel", "score", "high potential", "intent_pred"))
        assert "reddit.com/r/fake/comments/f1/" in text
    assert Path(out["sheets"][0]).read_text() == Path(out["sheets"][1]).read_text()                 # the two reviewers get identical, blank sheets


def test_posts_without_required_fields_or_with_duplicate_ids_are_refused(tmp_path):
    bad = fake_posts(); bad[3]["url"] = ""
    R.write_jsonl(tmp_path / "bad.jsonl", bad)
    with pytest.raises(SystemExit):
        R.cmd_sheets(tmp_path / "bad.jsonl", tmp_path)
    dup = fake_posts(); dup[2]["id"] = dup[1]["id"]
    R.write_jsonl(tmp_path / "dup.jsonl", dup)
    with pytest.raises(SystemExit):
        R.cmd_sheets(tmp_path / "dup.jsonl", tmp_path)


def test_incomplete_labelling_is_refused(tmp_path):
    R.cmd_sheets(write_posts(tmp_path), tmp_path)
    with pytest.raises(SystemExit):
        R.cmd_agreement(tmp_path / "reviewer_A.csv", tmp_path / "reviewer_B.csv", tmp_path)            # nothing filled in yet


def pipeline(tmp_path, disagree=()):
    posts = write_posts(tmp_path)
    R.cmd_sheets(posts, tmp_path)
    a, b = label_maps(disagree)
    fill(tmp_path / "reviewer_A.csv", a); fill(tmp_path / "reviewer_B.csv", b)
    return posts, R.cmd_agreement(tmp_path / "reviewer_A.csv", tmp_path / "reviewer_B.csv", tmp_path)


def test_agreement_report_and_the_disagreement_sheet(tmp_path):
    _, rep = pipeline(tmp_path, disagree=("p01", "p03"))
    assert rep["items"] == 12 and rep["disagreements"] == 2 and rep["label_agreement"] == round(10 / 12, 3) and rep["label_kappa"] is not None
    rows = list(csv.DictReader(open(tmp_path / "disagreements.csv", newline="", encoding="utf-8")))
    assert {r["id"] for r in rows} == {"p01", "p03"} and all(r["final_label"] == "" for r in rows)


def test_merge_refuses_unresolved_disagreements_then_records_the_adjudication(tmp_path):
    pipeline(tmp_path, disagree=("p01",))
    args = (tmp_path / "reviewer_A.csv", tmp_path / "reviewer_B.csv", tmp_path / "disagreements.csv", tmp_path / "labels_final.jsonl")
    with pytest.raises(SystemExit):
        R.cmd_merge(*args)
    rows = list(csv.DictReader(open(tmp_path / "disagreements.csv", newline="", encoding="utf-8")))
    rows[0].update(final_label="lead", final_intent_type="direct_request", adjudicator="", reason="")
    with open(tmp_path / "disagreements.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    with pytest.raises(SystemExit):                                                                    # adjudicator and reason are mandatory
        R.cmd_merge(*args)
    rows[0].update(adjudicator="Reviewer C", reason="the author asks for a plumber")
    with open(tmp_path / "disagreements.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    assert R.cmd_merge(*args) == {"items": 12, "adjudicated": 1}
    final = {r["id"]: r for r in R.read_jsonl(tmp_path / "labels_final.jsonl")}
    assert final["p01"]["label"] == "lead" and final["p01"]["adjudicated"] is True and final["p01"]["reviewers"] == {"A": "lead", "B": "review"}


def locked(tmp_path):
    pipeline(tmp_path)
    R.cmd_merge(tmp_path / "reviewer_A.csv", tmp_path / "reviewer_B.csv", tmp_path / "disagreements.csv", tmp_path / "labels_final.jsonl")
    R.cmd_lock(tmp_path / "labels_final.jsonl", tmp_path / "lock.json")
    prof = tmp_path / "profiles"; prof.mkdir()
    (prof / "plumber.json").write_text(json.dumps({"business_name": "P", "sells": "plumbing and boiler repair services", "ideal_customers": "homeowners",
                                                    "problems_solved": "leaking pipes, blocked drains"}))
    (prof / "saas.json").write_text(json.dumps({"business_name": "S", "sells": "applicant tracking software for small companies", "ideal_customers": "small business owners"}))
    return prof


def test_a_changed_label_file_after_the_lock_voids_prediction_and_scoring(tmp_path):
    prof = locked(tmp_path)
    R.cmd_predict(tmp_path / "posts.jsonl", prof, tmp_path / "labels_final.jsonl", tmp_path / "lock.json", tmp_path / "predictions.jsonl")
    final = R.read_jsonl(tmp_path / "labels_final.jsonl")
    final[0]["label"] = "lead"
    R.write_jsonl(tmp_path / "labels_final.jsonl", final)                                              # someone edits a label after seeing results
    with pytest.raises(SystemExit):
        R.cmd_score(tmp_path / "labels_final.jsonl", tmp_path / "lock.json", tmp_path / "predictions.jsonl", tmp_path / "r.json")
    with pytest.raises(SystemExit):
        R.cmd_predict(tmp_path / "posts.jsonl", prof, tmp_path / "labels_final.jsonl", tmp_path / "lock.json", tmp_path / "p2.jsonl")


def test_predictions_without_the_matching_lock_hash_are_refused(tmp_path):
    prof = locked(tmp_path)
    R.cmd_predict(tmp_path / "posts.jsonl", prof, tmp_path / "labels_final.jsonl", tmp_path / "lock.json", tmp_path / "predictions.jsonl")
    preds = R.read_jsonl(tmp_path / "predictions.jsonl")
    preds[0]["generated_after_lock_sha256"] = "0" * 64
    R.write_jsonl(tmp_path / "predictions.jsonl", preds)
    with pytest.raises(SystemExit):
        R.cmd_score(tmp_path / "labels_final.jsonl", tmp_path / "lock.json", tmp_path / "predictions.jsonl", tmp_path / "r.json")


def test_the_whole_flow_scores_with_confusion_counts_intervals_and_warnings(tmp_path):
    prof = locked(tmp_path)
    R.cmd_predict(tmp_path / "posts.jsonl", prof, tmp_path / "labels_final.jsonl", tmp_path / "lock.json", tmp_path / "predictions.jsonl")
    res = R.cmd_score(tmp_path / "labels_final.jsonl", tmp_path / "lock.json", tmp_path / "predictions.jsonl", tmp_path / "results.json")
    o = res["overall"]
    assert o["n"] == 12 and o["tp"] + o["fn"] == 6 and o["tp"] + o["fp"] + o["fn"] + o["tn"] == 12
    assert res["reached_100"] is False and any("only 12 items" in w for w in res["warnings"]) and any("only 6 items" in w for w in res["warnings"])
    assert set(res["by_business"]) == {"plumber", "saas"} and set(res["by_intent_type"]) == {"direct_request", "irrelevant"}
    assert o["recall_ci95"] is not None and (json.loads((tmp_path / "results.json").read_text())["overall"] == json.loads(json.dumps(o)))
    assert res["engine"] and set(res["engine"]) == {"lead_engine.py", "lead_intent.py", "business_profile.py"}


def test_scoring_math_on_a_crafted_prediction_file(tmp_path):
    labels = [{"id": f"x{i}", "label": lab, "intent_type": "direct_request" if lab == "lead" else "irrelevant", "business": "b", "platform": "reddit", "url": f"https://u/{i}"}
              for i, lab in enumerate(["lead"] * 4 + ["not_lead"] * 4 + ["review"] * 2)]
    R.write_jsonl(tmp_path / "labels.jsonl", labels)
    lock = R.cmd_lock(tmp_path / "labels.jsonl", tmp_path / "lock.json")
    surfaced = {"x0": "High potential", "x1": "Relevant", "x2": "Needs review", "x3": None, "x4": "Relevant", "x5": None, "x6": None, "x7": None, "x8": "Relevant", "x9": None}
    R.write_jsonl(tmp_path / "pred.jsonl", [{"id": k, "label": v, "intent": None, "quote": None, "generated_after_lock_sha256": lock["sha256"], "engine": {}} for k, v in surfaced.items()])
    o = R.cmd_score(tmp_path / "labels.jsonl", tmp_path / "lock.json", tmp_path / "pred.jsonl", tmp_path / "r.json")["overall"]
    assert (o["tp"], o["fn"], o["fp"], o["tn"]) == (2, 2, 2, 4) and o["precision"] == 0.5 and o["recall"] == 0.5 and o["review_items_surfaced_as_lead"] == 1
