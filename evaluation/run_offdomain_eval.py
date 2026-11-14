"""
run_offdomain_eval.py — an independent FALSE-POSITIVE stress test for the lead engine (SYNTHETIC).

The 245 posts in evaluation/classification_eval_v*.jsonl were written earlier for a different purpose (demand, problems and chatter about payroll,
salon booking, invoicing, travel, ...). For the six lead-eval businesses (Shopify agency, ATS SaaS, marketing agency, designer, plumber, skincare)
almost none of them is a lead, so every High potential / Relevant result here is a candidate false positive. It measures how often generic wording
(a "looking for a tool" post about something else) is mistaken for a lead.  python evaluation/run_offdomain_eval.py [--engine PATH]
"""
import argparse
import glob
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))
import business_profile as bp  # noqa: E402
import lead_eval_data as data  # noqa: E402
import run_lead_eval as R  # noqa: E402

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def main(engine_path=None):
    eng = R.load_engine(engine_path)
    posts = []
    for f in sorted(glob.glob(str(ROOT / "evaluation" / "classification_eval_v*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line:
                posts.append(json.loads(line))
    recs = [{"title": p["title"], "post_text": p["text"], "post_url": f"https://www.reddit.com/r/t/comments/n{i}x/x/", "platform": "reddit", "author": f"u{i}",
             "published_at": (NOW - timedelta(days=3)).isoformat()} for i, p in enumerate(posts)]
    tot_hi = tot_rev = 0
    print(f"off-domain posts: {len(recs)} x {len(data.BUSINESSES)} profiles = {len(recs) * len(data.BUSINESSES)} pairs")
    for biz, spec in data.BUSINESSES.items():
        user, _ = bp.validate(spec)
        scan = eng.run_scan(bp.build(user), [R._Conn(recs)], now=NOW)
        hi = [l for l in scan["leads"] if l["label"] in R.SURFACED]
        rev = [l for l in scan["leads"] if l["label"] == "Needs review"]
        tot_hi, tot_rev = tot_hi + len(hi), tot_rev + len(rev)
        print(f"  {biz:10s} High/Relevant {len(hi):2d}  Needs review {len(rev):2d}" + ("   e.g. " + "; ".join(l["title"][:36] for l in hi[:3]) if hi else ""))
    print(f"  TOTAL High/Relevant {tot_hi} ({tot_hi / (len(recs) * len(data.BUSINESSES)):.2%} of pairs), Needs review {tot_rev}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine")
    main(ap.parse_args().engine)
