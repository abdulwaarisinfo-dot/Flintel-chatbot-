#!/usr/bin/env python3
"""Micro-benchmarks of the pure, offline critical paths (no database, no network, no model call). Wall-clock on THIS machine: use for before/after
comparisons of the same machine only; they say nothing about production throughput, database latency or provider latency (those are unmeasured)."""
import json
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import search_modes as sm  # noqa: E402
import signal_intel as si  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def timed(fn, repeat=5):
    ts = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return {"median_s": st.median(ts), "min_s": min(ts), "max_s": max(ts)}


def corpus(n):
    rows = [json.loads(l) for l in open(os.path.join(HERE, "classification_eval_v1.jsonl"), encoding="utf-8")]
    return [{"title": rows[i % len(rows)]["title"], "post_text": rows[i % len(rows)]["text"] + f" #{i}", "post_url": f"https://news.ycombinator.com/item?id={i}",
             "platform": "hackernews", "created_utc": 1700000000.0 + i} for i in range(n)]


def run(n=1000):
    posts = corpus(n)
    live = [dict(p, hn_id=str(i)) for i, p in enumerate(posts)]
    hist = [dict(p, post_url=p["post_url"].replace("https://", "http://") + "&ref=x") for p in posts[: n // 2]] + [dict(p, post_url=p["post_url"] + "x") for p in posts[n // 2:]]
    queries = ["What problems are people discussing today?", "Find people looking for a CRM for small teams", "Show me recent discussions", "best CRM for real estate"] * (n // 4)
    out = {"n": n, "classifier": si.CLASSIFIER_VERSION}
    out["classify_posts"] = timed(lambda: [si.classify_post(p, "invoicing tool") for p in posts])
    out["build_signal_report"] = timed(lambda: si.build_signal_report(posts, "invoicing tool", "topic", {}))
    out["merge_live_historical"] = timed(lambda: sm.merge(live, hist))
    out["canonical_url"] = timed(lambda: [sm.canonical_url(p["post_url"]) for p in hist])
    out["discovery_detector"] = timed(lambda: [si.is_general_discovery_request(q) for q in queries])
    for k, v in out.items():
        if isinstance(v, dict):
            v["per_item_us"] = round(v["median_s"] / n * 1e6, 2)
    return out


if __name__ == "__main__":
    r = run(int(sys.argv[1]) if len(sys.argv) > 1 else 1000)
    for k, v in r.items():
        print(f"{k:24} {v}" if isinstance(v, dict) else f"{k}: {v}")
