"""Regression ceilings for the pure critical paths. Ceilings are deliberately generous (about 4x the measured values on the development machine) so
they catch an accidental quadratic or a 10x slowdown, not machine noise. Measured at 1000 items (this machine): classify 0.29 s, merge 0.03 s,
canonical_url 0.014 s, discovery detector 0.006 s. These say NOTHING about database, provider or production latency (unmeasured)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bench", ROOT / "evaluation" / "benchmark.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_ceilings_for_one_thousand_items():
    r = bench.run(1000)
    assert r["classify_posts"]["median_s"] < 1.2
    assert r["build_signal_report"]["median_s"] < 0.2
    assert r["merge_live_historical"]["median_s"] < 0.15
    assert r["canonical_url"]["median_s"] < 0.1
    assert r["discovery_detector"]["median_s"] < 0.05


def test_classification_and_merge_scale_roughly_linearly():
    small, big = bench.run(500), bench.run(2000)
    for k in ("classify_posts", "merge_live_historical"):
        ratio = big[k]["median_s"] / max(small[k]["median_s"], 1e-9)
        assert ratio < 8, (k, ratio)                           # 4x the items must not cost 8x the time (a quadratic would be about 16x)
