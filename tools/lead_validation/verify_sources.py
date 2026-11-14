"""
verify_sources.py — one lead scan against the REAL sources, with the measurements the validation plan needs. NOT RUN: nothing here has been executed against a
real service.

    python -m tools.lead_validation.verify_sources --profile profile.json                      # dry run: prints the plan, contacts nothing
    python -m tools.lead_validation.verify_sources --profile profile.json --execute --approved-by "Owner Name" --sources hackernews,reddit --days 30

`profile.json` holds the business fields the user would type (business_name, sells, ideal_customers, problems_solved, geo_scope, geo_places, ...). Real mode needs the
approval conditions in _guard.py and the application's own staging environment variables for the sources you choose:
    hackernews   HACKERNEWS_ENABLED and its guards (public Algolia API, no key)
    reddit       the staging MongoDB URIs and OPENAI_API_KEY the application already reads (embeddings are a PAID call)
It records per source: status, records, wall time, the Algolia request count (Hacker News) and the number of embedding calls and input characters (Reddit; the
token cost is whatever the provider's usage page says, this script only counts calls). It writes `<out>/scan_report.json` and `<out>/manual_check.csv` (10 leads to
open by hand and compare with the stored quote). Failure paths: `--inject-deadline 0.5` (a tiny shared source deadline) and `--simulate-hn-down` (an injected failing
http_get, no network) exercise the timeout and partial-failure behaviour without breaking anything.
"""
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.lead_validation._guard import require_approval  # noqa: E402


def build_connectors(selected, simulate_hn_down=False, counters=None):
    import lead_connectors
    out = []
    if "hackernews" in selected:
        def counting_get(url, timeout, _real=None):
            counters["algolia_requests"] += 1
            if simulate_hn_down:
                raise OSError("simulated outage (injected, no network)")
            import hackernews_live
            return hackernews_live._default_http_get(url, timeout)
        out.append(lead_connectors.HackerNewsConnector(http_get=counting_get))
    if "reddit" in selected:
        def search(queries, since_days, limit):
            import logics
            _count_embeddings(logics, counters)
            return logics.get_matched_signals("lead-validation", queries, "reddit", limit=limit, since_days=since_days, user_query=" ".join(queries[:2]))
        out.append(lead_connectors.StoredRedditConnector(search))
    return out


def _count_embeddings(logics, counters):
    """Wraps the application's batch embedding function once so calls and input characters are counted. Counting only: behaviour is unchanged."""
    name = "generate_query_embeddings_batch"
    fn = getattr(logics, name, None)
    if fn is None or getattr(fn, "_counted", False):
        return

    def wrapper(items, *a, **k):
        counters["embedding_calls"] += 1
        counters["embedding_input_chars"] += sum(len(str(x)) for x in (items or []))
        return fn(items, *a, **k)
    wrapper._counted = True
    setattr(logics, name, wrapper)


def run(profile_path, sources, days, out_dir, inject_deadline=None, simulate_hn_down=False, connectors_factory=None):
    import business_profile
    import lead_engine
    user, errors = business_profile.validate(json.loads(Path(profile_path).read_text(encoding="utf-8")))
    if errors:
        raise SystemExit(f"profile.json is not valid: {errors}")
    profile = business_profile.build(user)
    counters = {"algolia_requests": 0, "embedding_calls": 0, "embedding_input_chars": 0}
    conns = (connectors_factory or build_connectors)(sources, simulate_hn_down, counters)
    t0 = time.perf_counter()
    scan = lead_engine.run_scan(profile, conns, since_days=days, source_deadline=inject_deadline or 30.0)
    wall = round((time.perf_counter() - t0) * 1000, 1)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = {"run_at": datetime.now(timezone.utc).isoformat(), "sources": sources, "days": days, "wall_ms": wall, "timings": scan["timings"],
              "coverage": scan["coverage"], "counts": scan["counts"], "provider_calls_counted": counters, "statement": scan["statement"],
              "concepts": [c["query"] for c in scan["concepts"]],
              "note": "Costs are NOT computed here. Read the provider usage pages for the exact spend; this report only counts calls."}
    (out / "scan_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    with open(out / "manual_check.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["platform", "url", "author_shown", "published_at_shown", "quote_shown", "label", "opened_by_hand(y/n)", "quote_found_in_post(y/n)", "author_matches(y/n)", "date_matches(y/n)"])
        for l in scan["leads"][:10]:
            w.writerow([l["platform"], l["url"], l["author"] or "", l["published_at"] or "", (l["evidence"]["quote"] or "")[:200], l["label"], "", "", "", ""])
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--sources", default="hackernews,reddit")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="validation_output")
    ap.add_argument("--inject-deadline", type=float)
    ap.add_argument("--simulate-hn-down", action="store_true")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--approved-by")
    a = ap.parse_args(argv)
    sources = [s.strip() for s in a.sources.split(",") if s.strip()]
    what = (f"run ONE scan of {sources} for the last {a.days} days using the application's own staging configuration; this contacts the public Algolia API and/or the staging "
            "MongoDB and the embedding provider (a paid call), then writes scan_report.json and manual_check.csv to " + a.out)
    if a.simulate_hn_down and sources == ["hackernews"]:
        a.execute = a.execute                                      # still guarded: building the real connector imports application code
    if not require_approval(a.approved_by, what, not a.execute):
        return 0
    rep = run(a.profile, sources, a.days, a.out, a.inject_deadline, a.simulate_hn_down)
    print(json.dumps({k: rep[k] for k in ("wall_ms", "coverage", "counts", "provider_calls_counted")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
