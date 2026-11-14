"""
real_eval.py — tooling for the 100-conversation, double-blind validation of the lead engine on REAL public posts.

This file contains NO data. It was tested only on tiny synthetic fixtures (tests/test_real_eval_tooling.py, labelled FAKE). The evaluation has NOT been done: the posts
have not been collected and nobody has labelled them. See evaluation/real_eval/README.md for the protocol.

Subcommands (all local, no network, no model):
  sheets     posts.jsonl  -> reviewer_A.csv, reviewer_B.csv   (the post text and metadata only: NO Flintel output of any kind)
  agreement  reviewer_A.csv reviewer_B.csv -> agreement.json + disagreements.csv (to adjudicate)
  merge      reviewer_A.csv reviewer_B.csv disagreements.csv -> labels_final.jsonl (refuses while any disagreement has no final label)
  lock       labels_final.jsonl -> labels_final.lock.json (sha256 + time): from here the labels may not change
  predict    posts.jsonl profiles/ labels_final.lock.json -> predictions.jsonl (refuses without a lock that matches the labels)
  score      labels_final.jsonl predictions.jsonl -> results.json (+ a readable summary); refuses predictions made without the matching lock

Labels: lead | review | not_lead.  A lead = a current, genuine customer opportunity for that business; review = plausible but not provable (ambiguous or historical);
not_lead = anything else. Intent types: direct_request, recommendation, problem, provider_search, buying_question, comparison, replace_provider, indirect,
irrelevant, ambiguous.  "Surfaced" = Flintel label High potential or Relevant.
"""
import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABELS = ("lead", "review", "not_lead")
INTENT_TYPES = ("direct_request", "recommendation", "problem", "provider_search", "buying_question", "comparison", "replace_provider", "indirect", "irrelevant", "ambiguous")
SURFACED = ("High potential", "Relevant")
SHEET_COLUMNS = ["id", "business", "platform", "url", "published_at", "title", "text", "label", "intent_type", "notes"]
REQUIRED_POST_FIELDS = ("id", "business", "platform", "url", "text")


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def write_jsonl(path, rows):
    Path(path).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def read_sheet(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["id"]: r for r in csv.DictReader(fh)}


# ── statistics ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def wilson(k: int, n: int, z: float = 1.96):
    """95% Wilson score interval for a proportion k/n (None when n == 0)."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


def cohen_kappa(a, b):
    """Cohen's kappa for two raters over the same items (lists of labels). None if undefined."""
    n = len(a)
    if n == 0:
        return None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[l] / n) * (cb[l] / n) for l in set(ca) | set(cb))
    return None if pe == 1 else round((po - pe) / (1 - pe), 3)


# ── commands ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def cmd_sheets(posts_path, out_dir):
    posts = read_jsonl(posts_path)
    bad = [p.get("id") for p in posts if any(not p.get(k) for k in REQUIRED_POST_FIELDS)]
    if bad:
        raise SystemExit(f"posts missing required fields {REQUIRED_POST_FIELDS}: {bad[:5]}")
    if len({p["id"] for p in posts}) != len(posts):
        raise SystemExit("duplicate post ids")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for who in ("A", "B"):
        with open(out / f"reviewer_{who}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS)
            w.writeheader()
            for p in posts:
                w.writerow({"id": p["id"], "business": p["business"], "platform": p["platform"], "url": p["url"], "published_at": p.get("published_at") or "",
                            "title": p.get("title") or "", "text": p["text"], "label": "", "intent_type": "", "notes": ""})
    return {"posts": len(posts), "sheets": [str(out / "reviewer_A.csv"), str(out / "reviewer_B.csv")]}


def _checked(sheet, who):
    errs = []
    for i, r in sheet.items():
        if r["label"] not in LABELS:
            errs.append(f"{who}:{i}: label {r['label']!r} is not one of {LABELS}")
        if r["intent_type"] not in INTENT_TYPES:
            errs.append(f"{who}:{i}: intent_type {r['intent_type']!r} is not one of {INTENT_TYPES}")
    return errs


def cmd_agreement(a_path, b_path, out_dir):
    A, B = read_sheet(a_path), read_sheet(b_path)
    if set(A) != set(B):
        raise SystemExit("the two sheets do not contain the same post ids")
    errs = _checked(A, "A") + _checked(B, "B")
    if errs:
        raise SystemExit("incomplete labelling:\n" + "\n".join(errs[:20]))
    ids = sorted(A)
    la, lb = [A[i]["label"] for i in ids], [B[i]["label"] for i in ids]
    both_lead = [i for i in ids if A[i]["label"] == "lead" and B[i]["label"] == "lead"]
    rep = {"items": len(ids), "label_agreement": round(sum(1 for x, y in zip(la, lb) if x == y) / len(ids), 3), "label_kappa": cohen_kappa(la, lb),
           "intent_agreement_on_agreed_leads": (round(sum(1 for i in both_lead if A[i]["intent_type"] == B[i]["intent_type"]) / len(both_lead), 3) if both_lead else None),
           "disagreements": sum(1 for x, y in zip(la, lb) if x != y)}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "agreement.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    with open(out / "disagreements.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "url", "A_label", "A_intent_type", "B_label", "B_intent_type", "final_label", "final_intent_type", "adjudicator", "reason"])
        for i in ids:
            if A[i]["label"] != B[i]["label"] or (A[i]["label"] == "lead" and A[i]["intent_type"] != B[i]["intent_type"]):
                w.writerow([i, A[i]["url"], A[i]["label"], A[i]["intent_type"], B[i]["label"], B[i]["intent_type"], "", "", "", ""])
    return rep


def cmd_merge(a_path, b_path, disagreements_path, out_path):
    A, B = read_sheet(a_path), read_sheet(b_path)
    with open(disagreements_path, newline="", encoding="utf-8") as fh:
        dis = {r["id"]: r for r in csv.DictReader(fh)}
    rows, missing = [], []
    for i in sorted(A):
        if i in dis:
            d = dis[i]
            if d["final_label"] not in LABELS or d["final_intent_type"] not in INTENT_TYPES or not d["adjudicator"].strip() or not d["reason"].strip():
                missing.append(i)
                continue
            rows.append({"id": i, "label": d["final_label"], "intent_type": d["final_intent_type"], "business": A[i]["business"], "platform": A[i]["platform"], "url": A[i]["url"],
                         "adjudicated": True, "reviewers": {"A": A[i]["label"], "B": B[i]["label"]}})
        else:
            rows.append({"id": i, "label": A[i]["label"], "intent_type": A[i]["intent_type"], "business": A[i]["business"], "platform": A[i]["platform"], "url": A[i]["url"],
                         "adjudicated": False, "reviewers": {"A": A[i]["label"], "B": B[i]["label"]}})
    if missing:
        raise SystemExit(f"{len(missing)} disagreements are not resolved (final_label, final_intent_type, adjudicator and reason are all required): {missing[:10]}")
    write_jsonl(out_path, rows)
    return {"items": len(rows), "adjudicated": sum(1 for r in rows if r["adjudicated"])}


def cmd_lock(labels_path, lock_path):
    lock = {"labels_file": str(labels_path), "sha256": sha256_file(labels_path), "locked_at": datetime.now(timezone.utc).isoformat(), "items": len(read_jsonl(labels_path))}
    Path(lock_path).write_text(json.dumps(lock, indent=2), encoding="utf-8")
    return lock


def _verify_lock(labels_path, lock_path):
    lock = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    if lock["sha256"] != sha256_file(labels_path):
        raise SystemExit("labels_final.jsonl changed after it was locked: the evaluation is void")
    return lock


def _engine_fingerprint():
    return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest()[:16] for f in ("lead_engine.py", "lead_intent.py", "business_profile.py")}


def cmd_predict(posts_path, profiles_dir, labels_path, lock_path, out_path, engine=None):
    lock = _verify_lock(labels_path, lock_path)
    sys.path.insert(0, str(ROOT))
    import business_profile
    import lead_engine as default_engine
    eng = engine or default_engine
    posts = read_jsonl(posts_path)
    now = datetime.now(timezone.utc)
    by_biz = defaultdict(list)
    for p in posts:
        by_biz[p["business"]].append(p)
    rows = []
    for biz, plist in by_biz.items():
        user, errors = business_profile.validate(json.loads((Path(profiles_dir) / f"{biz}.json").read_text(encoding="utf-8")))
        if errors:
            raise SystemExit(f"profile {biz}: {errors}")
        profile = business_profile.build(user)

        class _C:
            key, label, retrieval_path = "reddit", "Reddit", "historical"

            def fetch(self, concepts, days, limit, _recs=[{"title": p.get("title") or "", "post_text": p["text"], "post_url": p["url"], "platform": p["platform"],
                                                           "author": p.get("author"), "published_at": p.get("published_at")} for p in plist]):
                return {"status": "ok", "records": _recs, "detail": ""}
        scan = eng.run_scan(profile, [_C()], now=now, limit=len(plist) + 10)
        shown = {l["url"]: l for l in scan["leads"]}
        for p in plist:
            l = shown.get(p["url"])
            rows.append({"id": p["id"], "label": l["label"] if l else None, "intent": l["intent"] if l else None, "quote": l["evidence"]["quote"] if l else None,
                         "generated_after_lock_sha256": lock["sha256"], "engine": _engine_fingerprint()})
    write_jsonl(out_path, rows)
    return {"predictions": len(rows)}


def _confusion(rows):
    tp = sum(1 for r in rows if r["expected"] == "lead" and r["surfaced"])
    fn = sum(1 for r in rows if r["expected"] == "lead" and not r["surfaced"])
    fp = sum(1 for r in rows if r["expected"] != "lead" and r["surfaced"])
    tn = sum(1 for r in rows if r["expected"] != "lead" and not r["surfaced"])
    prec_n, rec_n = tp + fp, tp + fn
    return {"n": len(rows), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": None if not prec_n else round(tp / prec_n, 3), "precision_ci95": wilson(tp, prec_n),
            "recall": None if not rec_n else round(tp / rec_n, 3), "recall_ci95": wilson(tp, rec_n),
            "review_items_surfaced_as_lead": sum(1 for r in rows if r["expected"] == "review" and r["surfaced"])}


def cmd_score(labels_path, lock_path, predictions_path, out_path):
    lock = _verify_lock(labels_path, lock_path)
    labels = {r["id"]: r for r in read_jsonl(labels_path)}
    preds = {r["id"]: r for r in read_jsonl(predictions_path)}
    if set(preds) != set(labels):
        raise SystemExit("predictions and labels do not cover the same post ids")
    if any(p["generated_after_lock_sha256"] != lock["sha256"] for p in preds.values()):
        raise SystemExit("some predictions were not generated after the labels were locked: the evaluation is void")
    rows = [{"id": i, "business": labels[i]["business"], "intent_type": labels[i]["intent_type"], "expected": labels[i]["label"], "surfaced": preds[i]["label"] in SURFACED,
             "predicted": preds[i]["label"], "url": labels[i]["url"]} for i in sorted(labels)]
    res = {"items": len(rows), "reached_100": len(rows) >= 100, "label_counts": dict(Counter(r["expected"] for r in rows)), "overall": _confusion(rows),
           "by_business": {b: _confusion([r for r in rows if r["business"] == b]) for b in sorted({r["business"] for r in rows})},
           "by_intent_type": {t: _confusion([r for r in rows if r["intent_type"] == t]) for t in sorted({r["intent_type"] for r in rows})},
           "false_positives": [{"id": r["id"], "url": r["url"], "expected": r["expected"], "predicted": r["predicted"]} for r in rows if r["expected"] != "lead" and r["surfaced"]],
           "false_negatives": [{"id": r["id"], "url": r["url"], "predicted": r["predicted"]} for r in rows if r["expected"] == "lead" and not r["surfaced"]],
           "warnings": [], "engine": next(iter(preds.values()))["engine"] if preds else None}
    if len(rows) < 100:
        res["warnings"].append(f"only {len(rows)} items: the protocol requires at least 100")
    for b, m in res["by_business"].items():
        if m["n"] < 10:
            res["warnings"].append(f"business {b!r} has only {m['n']} items: its numbers are not meaningful")
    Path(out_path).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheets"); s.add_argument("posts"); s.add_argument("--out", default=".")
    s = sub.add_parser("agreement"); s.add_argument("a"); s.add_argument("b"); s.add_argument("--out", default=".")
    s = sub.add_parser("merge"); s.add_argument("a"); s.add_argument("b"); s.add_argument("disagreements"); s.add_argument("--out", default="labels_final.jsonl")
    s = sub.add_parser("lock"); s.add_argument("labels"); s.add_argument("--out", default="labels_final.lock.json")
    s = sub.add_parser("predict"); s.add_argument("posts"); s.add_argument("profiles"); s.add_argument("labels"); s.add_argument("lock"); s.add_argument("--out", default="predictions.jsonl")
    s = sub.add_parser("score"); s.add_argument("labels"); s.add_argument("lock"); s.add_argument("predictions"); s.add_argument("--out", default="results.json")
    a = ap.parse_args(argv)
    if a.cmd == "sheets":
        print(json.dumps(cmd_sheets(a.posts, a.out), indent=2))
    elif a.cmd == "agreement":
        print(json.dumps(cmd_agreement(a.a, a.b, a.out), indent=2))
    elif a.cmd == "merge":
        print(json.dumps(cmd_merge(a.a, a.b, a.disagreements, a.out), indent=2))
    elif a.cmd == "lock":
        print(json.dumps(cmd_lock(a.labels, a.out), indent=2))
    elif a.cmd == "predict":
        print(json.dumps(cmd_predict(a.posts, a.profiles, a.labels, a.lock, a.out), indent=2))
    else:
        r = cmd_score(a.labels, a.lock, a.predictions, a.out)
        print(json.dumps({k: r[k] for k in ("items", "reached_100", "overall", "warnings")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
