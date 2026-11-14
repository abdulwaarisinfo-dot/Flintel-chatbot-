"""
run_lead_eval.py — scores a lead engine on evaluation/lead_eval_data.py (SYNTHETIC; not real-world accuracy).

    python evaluation/run_lead_eval.py                      # current lead_engine, all splits
    python evaluation/run_lead_eval.py --engine PATH.py     # another copy of the engine (e.g. the saved v1)
    python evaluation/run_lead_eval.py --split dev|held|baseline

"Surfaced" = label High potential or Relevant. expect=lead must surface; expect=review may be Needs review but must not surface; expect=none must
not be shown at all. Precision = TP/(TP+FP) over surfaced items, recall = TP/(TP+FN) over expect=lead items. No threshold is tuned here.
"""
import argparse
import importlib.util
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))
import business_profile as bp  # noqa: E402
import lead_eval_data as data  # noqa: E402

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
SURFACED = ("High potential", "Relevant")


def load_engine(path=None):
    if not path:
        import lead_engine
        return lead_engine
    spec = importlib.util.spec_from_file_location("lead_engine_alt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Conn:
    key, label, retrieval_path = "reddit", "Reddit", "historical"

    def __init__(self, recs):
        self.recs = recs

    def fetch(self, concepts, days, limit):
        return {"status": "ok", "records": self.recs, "detail": ""}


def evaluate(engine, split=None):
    rows = []
    for biz, spec in data.BUSINESSES.items():
        user, errors = bp.validate(spec)
        assert not errors, (biz, errors)
        profile = bp.build(user)
        its = [i for i in data.items() if i["biz"] == biz and (split is None or (i["baseline"] if split == "baseline" else i["split"] == split))]
        recs, by_url = [], {}
        for n, it in enumerate(its):
            url = f"https://www.reddit.com/r/t/comments/e{abs(hash(it['id'])) % 10**8}x{n}/x/"
            by_url[url] = it
            recs.append({"title": it["title"], "post_text": it["body"], "post_url": url, "platform": "reddit", "author": f"u{n}",
                         "published_at": (NOW - timedelta(days=it["days"])).isoformat()})
        scan = engine.run_scan(profile, [_Conn(recs)], now=NOW)
        shown = {l["url"]: l for l in scan["leads"]}
        for url, it in by_url.items():
            l = shown.get(url)
            rows.append(dict(it, label=l["label"] if l else None, intent=l["intent"] if l else None))
    return rows


def metrics(rows):
    tp = sum(1 for r in rows if r["expect"] == "lead" and r["label"] in SURFACED)
    fn = sum(1 for r in rows if r["expect"] == "lead" and r["label"] not in SURFACED)
    fp = sum(1 for r in rows if r["expect"] != "lead" and r["label"] in SURFACED)
    neg = [r for r in rows if r["expect"] == "none"]
    rev = [r for r in rows if r["expect"] == "review"]
    return {"tp": tp, "fn": fn, "fp": fp, "recall": tp / (tp + fn) if tp + fn else None, "precision": tp / (tp + fp) if tp + fp else None,
            "none_total": len(neg), "none_shown_any": sum(1 for r in neg if r["label"]), "none_surfaced": sum(1 for r in neg if r["label"] in SURFACED),
            "review_total": len(rev), "review_overconfident": sum(1 for r in rev if r["label"] in SURFACED), "review_hidden": sum(1 for r in rev if not r["label"])}


def fmt(m):
    f = lambda x: "  n/a" if x is None else f"{x:5.2f}"
    return (f"recall {f(m['recall'])} ({m['tp']}/{m['tp'] + m['fn']})  precision {f(m['precision'])} (FP {m['fp']})  "
            f"none shown-at-all {m['none_shown_any']}/{m['none_total']} surfaced {m['none_surfaced']}  review over-confident {m['review_overconfident']}/{m['review_total']}")


def report(rows, title):
    print(f"\n=== {title}  (n={len(rows)}) ===")
    print("overall      ", fmt(metrics(rows)))
    groups = defaultdict(list)
    for r in rows:
        groups[r["biz"]].append(r)
    print("-- by business")
    for k, v in groups.items():
        print(f"  {k:10s}", fmt(metrics(v)))
    print("-- by intent kind (positives: recall; negatives: shown)")
    kinds = defaultdict(list)
    for r in rows:
        kinds[r["kind"]].append(r)
    for k, v in kinds.items():
        m = metrics(v)
        if v[0]["expect"] == "lead":
            print(f"  {k:19s} recall {m['recall']:.2f} ({m['tp']}/{m['tp'] + m['fn']})")
        else:
            print(f"  {k:19s} expect={v[0]['expect']:6s} shown-at-all {m['none_shown_any'] if v[0]['expect']=='none' else sum(1 for r in v if r['label'])}/{len(v)} surfaced {sum(1 for r in v if r['label'] in SURFACED)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine")
    ap.add_argument("--split", choices=["dev", "held", "fresh", "baseline"])
    ap.add_argument("--misses", action="store_true", help="list misses and false positives (use on dev only while developing)")
    a = ap.parse_args()
    eng = load_engine(a.engine)
    rows = evaluate(eng, a.split)
    report(rows, f"{a.engine or 'current lead_engine'} / split={a.split or 'all'}")
    if a.misses:
        print("\n-- misses (expect lead, not surfaced) and false positives")
        for r in rows:
            if (r["expect"] == "lead" and r["label"] not in SURFACED) or (r["expect"] != "lead" and r["label"] in SURFACED):
                print(f"  {r['id']:34s} expect={r['expect']:6s} got={r['label']}/{r['intent']}  | {r['title'][:60]}")
