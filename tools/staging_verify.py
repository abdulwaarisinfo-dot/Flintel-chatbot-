#!/usr/bin/env python3
"""Staging integration verifier. Run ONLY against an isolated staging stack, after tools/staging_preflight.py passes.

It records what actually happened (status, timing, counts, error TYPES) in a JSON report. Statuses: PASS, FAIL, SKIPPED (did not run), UNKNOWN
(ran but could not decide). The report's `complete` flag is true only if EVERY check is PASS. It never prints a credential, document body, post
text or user data. The one database write is index creation caused by importing the application, which requires --allow-index-creation.

    python3 tools/staging_verify.py --offline --report offline.json               # no network, no database: repository/config checks only
    python3 tools/staging_verify.py --allow-index-creation --report staging_report.json   # Mongo, HN live, retrieval modes, classification
    python3 tools/staging_verify.py --report r.json --mysql-probe                 # also times the MySQL coverage queries + shows the plan
    python3 tools/staging_verify.py --report r.json --base-url https://STAGING    # also HTTP readiness of the deployed app
Credits per search need an authenticated staging session and are verified by the steps in FLINTEL_STAGING_CHECKLIST.md, not here.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# The exact checks a LIVE run must pass for its automated part to be complete. Names are shared by the run rows and the SKIPPED rows, so a check
# that was skipped is still the same required check (never silently absent).
CHK_PING = "mongodb ping"
CHK_COVERAGE = "mongodb coverage (reddit / hackernews)"
CHK_HN = "hacker news live stories (Algolia)"
CHK_MODES = "search modes x platforms x (topic, general)"
CHK_ZERO = "zero-result explanation (empty 1990 window)"
CHK_MYSQL = "mysql coverage probe review (timing + EXPLAIN)"
CHK_HTTP = "deployed app readiness (/api/health/ready)"
LIVE_REQUIRED = [CHK_PING, CHK_COVERAGE, CHK_HN, CHK_MODES, CHK_ZERO, CHK_HTTP]      # + CHK_MYSQL when MySQL is enabled for the deployment
MANUAL_STEPS = ["credit consumption with an authenticated staging account (checklist step 13)",
                "human review of this report against FLINTEL_STAGING_AUTHORIZATION_CHECKLIST.md"]
COMPLETE_MEANING = ("`complete` means ONLY: this was a live run and every automated check required for this deployment is present and PASS "
                    "(no FAIL, SKIPPED, UNKNOWN or missing check anywhere). It is not 'staging verified': manual steps always remain, and a "
                    "claim above 'integration' is never made by this tool.")


def finalize_report(checks, mode, mysql_required=False):
    """Pure function: the verdict fields of a report. `mode` is 'offline' or 'live'.
    complete            live AND every required check present exactly once with status PASS AND no check of any status other than PASS/SKIPPED-optional.
                        Always False offline, even if every row passed (offline contacts nothing, so it proves nothing about integrations).
    claims              integration_verified (live + complete), staging_verified (ALWAYS False here: it needs the manual steps and your review),
                        production_approved (ALWAYS False). Evidence is tiered: offline = repository checks only; live = integration against the
                        configured target; neither is production evidence."""
    required = list(LIVE_REQUIRED) + ([CHK_MYSQL] if mysql_required else [])
    by = {}
    for c in checks:
        by.setdefault(c["check"], []).append(c["status"])
    missing = [n for n in required if n not in by]
    not_pass = [n for n in required if n in by and by[n] != ["PASS"]]
    any_fail = any(st == "FAIL" for sts in by.values() for st in sts)
    complete = mode == "live" and not missing and not not_pass and not any_fail
    return {"mode": mode,
            "evidence_level": "offline: repository/configuration checks only (local evidence)" if mode == "offline"
                              else "live: integration checks against the configured target (not production evidence)",
            "required_checks": required, "missing_required": missing, "required_not_passed": not_pass,
            "complete": complete,
            "claims": {"integration_verified": bool(complete), "staging_verified": False, "production_approved": False},
            "manual_steps_pending": list(MANUAL_STEPS),
            "complete_meaning": COMPLETE_MEANING,
            "summary": {s: sum(1 for c in checks if c["status"] == s) for s in ("PASS", "FAIL", "SKIPPED", "UNKNOWN")}}


def out_skipped_or_failed(rep):
    return any(c["status"] != "PASS" for c in rep.checks)


class Report:
    def __init__(self):
        self.checks = []

    def run(self, name, fn):
        t0 = time.monotonic()
        try:
            status, detail = fn()
        except Exception as exc:                                    # error TYPE only: messages can echo hosts or credentials
            status, detail = "FAIL", {"error": type(exc).__name__}
        self.checks.append({"check": name, "status": status, "elapsed_ms": int((time.monotonic() - t0) * 1000), "detail": detail})
        print(f"{status:8} {name}  {json.dumps(detail, default=str)[:300]}")

    def summary(self):
        c = {}
        for k in self.checks:
            c[k["status"]] = c.get(k["status"], 0) + 1
        return c


def mongo_coverage(db, now=None):
    """Counts only. {platform: {total, last_7d, oldest, newest}} for the Reddit and Hacker News platform values."""
    now = now or datetime.now(timezone.utc)
    out = {}
    coll = db["flintel_signals"]
    for plat in ("reddit", "hackernews", "hackernews_comment"):
        q = {"platform": plat}
        total = coll.count_documents(q, limit=100000)
        hi = next(iter(coll.find(q, {"created_utc": 1}).sort("created_utc", -1).limit(1)), None)
        lo = next(iter(coll.find(q, {"created_utc": 1}).sort("created_utc", 1).limit(1)), None)
        out[plat] = {"total_capped_at_100000": total,
                     "last_7d": coll.count_documents(dict(q, created_utc={"$gte": now - timedelta(days=7)}), limit=100000),
                     "oldest": str((lo or {}).get("created_utc")), "newest": str((hi or {}).get("created_utc"))}
    return out


def hn_live(fetch, http_get=None):
    """One bounded general fetch and one topic fetch against the real Algolia API."""
    res = {}
    for label, kw, general in (("general", [], True), ("topic", ["crm"], False)):
        recs, st = fetch(kw, pool_n=10, start=datetime.now(timezone.utc) - timedelta(days=7), end=datetime.now(timezone.utc),
                         hits_per_page=20, max_pages=1, deadline_seconds=12.0, general=general, http_get=http_get)
        res[label] = {"state": st.get("state"), "records": len(recs), "requests": st.get("requests"), "failed": st.get("failed_requests"),
                      "reason": st.get("reason"),
                      "all_have_url_and_time": all(r.get("post_url", "").startswith("https://news.ycombinator.com/item?id=") and r.get("created_utc") for r in recs)}
    if any(v["state"] in ("failed", "rate_limited") or not v["all_have_url_and_time"] for v in res.values()):
        return "FAIL", res
    if any(v["records"] for v in res.values()):
        return "PASS", res
    return "UNKNOWN", dict(res, verdict="the API answered but returned nothing: retrieval is not demonstrated")      # reachable, nothing to verify


def judge_mongo(cov):
    """PASS only when the stores answered AND hold data to verify retrieval against. An empty database is a FAIL: nothing downstream means anything."""
    total = sum(x["total_capped_at_100000"] for x in cov.values())
    if total == 0:
        return "FAIL", dict(cov, verdict="no Reddit or Hacker News documents: seed the staging database first")
    return "PASS", cov


def judge_modes(res):
    """FAIL on a duplicate, a failed/rate-limited path, a capability violation (Reddit-only Live must be unavailable and empty), or no results anywhere."""
    problems = []
    for k, v in res.items():
        if v["duplicates"]:
            problems.append(f"{k}: duplicates")
        if "failed" in (v["live"], v["historical"]) or "rate_limited" in (v["live"], v["historical"]):
            problems.append(f"{k}: a path failed ({v['live']}/{v['historical']})")
        if k.startswith("Reddit|live") and (v["results"] or v["live"] != "unavailable"):
            problems.append(f"{k}: Reddit has no live source; expected unavailable and empty")
        if "|historical|" in k and v["live"] != "not_requested":
            problems.append(f"{k}: historical mode touched the live path")
    if not any(v["results"] for v in res.values()):
        problems.append("no combination returned any result")
    return ("FAIL" if problems else "PASS"), dict(res, problems=problems)


def judge_zero(cov, code):
    """The empty-window explanation must be a real, specific one. UNKNOWN passes only when it states why (an unprobed MySQL), never silently."""
    if code in ("NONE_IN_RANGE", "NO_POSTS_COLLECTED"):
        return "PASS", {"code": code}
    if code == "UNKNOWN" and cov.get("note"):
        return "PASS", {"code": code, "note": cov["note"]}
    if code == "UNKNOWN":
        return "UNKNOWN", {"code": code, "verdict": "no reason could be established"}
    return "FAIL", {"code": code, "note": cov.get("note"), "error": cov.get("error")}


def mysql_probe_review(connect, sources, columns_sql, run_explain=True):
    """Times the three bounded coverage queries and shows the optimiser's plan (EXPLAIN only; read-only)."""
    out = []
    for src in sources:
        conn = connect()
        try:
            cur = conn.cursor()
            t0 = time.monotonic()
            import mysql_signals as ms
            r = ms.probe_coverage(src, datetime.now(timezone.utc) - timedelta(days=1), datetime.now(timezone.utc), connect_fn=lambda: conn)
            entry = {"db_label": src.label, "total_nonempty": bool(r["total"]), "in_range": r["in_range"],
                     "elapsed_ms": int((time.monotonic() - t0) * 1000)}
            if run_explain:
                cur.execute(f"EXPLAIN SELECT COUNT(*) FROM `{src.db}`.`flintel_signals` WHERE `created_utc` >= %s",
                            (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),))
                rows = cur.fetchall()
                entry["explain_key_used"] = [str(x[6]) if len(x) > 6 else None for x in rows]      # the 'key' column: None = full scan
                entry["explain_rows_examined_estimate"] = [x[9] if len(x) > 9 else None for x in rows]
            out.append(entry)
        finally:
            conn.close()
    slow = [e for e in out if e["elapsed_ms"] > 2000 or any(k is None or k == "None" for k in e.get("explain_key_used", []))]
    return ("FAIL" if slow else "PASS"), {"sources": out, "verdict": "do NOT enable the probe: slow or full scan" if slow else "bounded and index-backed"}


def offline_checks(root):
    """Repository/configuration facts that need no service. Every service-dependent check is reported SKIPPED, so an offline report is never complete."""
    import re
    rows = []

    def add(name, ok, detail):
        rows.append({"check": name, "status": "PASS" if ok else "FAIL", "elapsed_ms": 0, "detail": detail})
    idx = open(os.path.join(root, "index.py")).read()
    add("startup hooks are exactly the two audited ones", len(re.findall(r'@app\.on_event\("startup"\)', idx)) == 2, {"count": len(re.findall(r'@app\.on_event\("startup"\)', idx))})
    sched = open(os.path.join(root, "scheduler.py")).read()
    add("trending scheduler is guarded by TRENDING_KEYWORDS_ENABLED", "TRENDING_KEYWORDS_ENABLED" in sched and "DISABLED" in sched, {})
    env_example = open(os.path.join(root, ".env.staging.example")).read()
    add("env template disables the scheduler and lazy embedding", "TRENDING_KEYWORDS_ENABLED=false" in env_example and "LAZY_EMBED_ENABLED=0" in env_example, {})
    add("env template holds no key-shaped value", not re.search(r"sk-[A-Za-z0-9]{10}|[0-9a-f]{32}", env_example), {})
    for name in (CHK_PING, CHK_COVERAGE, CHK_HN, CHK_MODES, CHK_ZERO, CHK_HTTP, CHK_MYSQL, "credit consumption (manual)"):
        rows.append({"check": name, "status": "SKIPPED", "elapsed_ms": 0, "detail": {"reason": "offline mode: no service is contacted"}})
    return rows


def run_offline(report_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rows = offline_checks(root)
    out = dict(finalize_report(rows, "offline"), generated_at=datetime.now(timezone.utc).isoformat(), checks=rows,
               note="Offline: nothing was contacted. Service checks are SKIPPED (NOT verified); complete is False and no integration claim is made.")
    for r in rows:
        print(f"{r['status']:8} {r['check']}")
    with open(report_path, "w") as f:
        json.dump(out, f, indent=2)
    print("\nsummary:", out["summary"], "complete:", out["complete"])
    return 1 if out["summary"]["FAIL"] else 5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="staging_report.json")
    ap.add_argument("--mysql-probe", action="store_true")
    ap.add_argument("--base-url")
    ap.add_argument("--offline", action="store_true", help="no network, no database, no application import: repository and configuration checks only")
    ap.add_argument("--allow-index-creation", action="store_true",
                    help="REQUIRED for any database check: importing the application (as starting it does) creates its own required indexes "
                         "on the target database. Retrieval itself performs no write; this is the only one.")
    ap.add_argument("--allow-paid-calls", action="store_true",
                    help="PAID and OFF by default: runs the mode matrix, which embeds each topic query through the LLM provider (about 9 small metered calls). "
                         "Also requires the paid-phase preflight (OPENAI_API_KEY + spend-limit attestation).")
    ap.add_argument("--max-seconds", type=int, default=300, help="overall cap for the mode matrix")
    a = ap.parse_args()
    started = time.monotonic()
    from tools import staging_preflight as pf
    if a.offline:
        return run_offline(a.report)
    phase = "paid" if a.allow_paid_calls else "isolation"   # no paid call is possible without the explicit flag AND the paid-phase preflight
    rows = pf.run(dict(os.environ), phase)
    pv = pf.verdict(rows, phase)
    if pv != "PASSED":
        print(f"Preflight (phase={phase}) verdict {pv}; refusing to touch any service:", *[x["name"] for x in rows if x["status"] != "PASS" and not (phase == "isolation" and x["phase"] == "paid")], sep="\n  ")
        return 2 if pv == "FAILED" else 5
    if a.base_url:
        ok, msg = pf.check_base_url(a.base_url, dict(os.environ))
        if not ok:
            print("--base-url refused:", msg)
            return 2
    if not a.allow_index_creation:
        print("Refusing to import the application without --allow-index-creation (it creates the app's indexes on the target database).")
        return 3
    rep = Report()
    import database
    rep.run(CHK_PING, lambda: ("PASS", {"ok": bool(database.client.admin.command("ping").get("ok"))}))
    rep.run(CHK_COVERAGE, lambda: judge_mongo(mongo_coverage(database.signals_collection.database)))
    import hackernews_live
    rep.run(CHK_HN, lambda: hn_live(hackernews_live.fetch))
    import logics
    import search_modes as sm
    import signal_intel as si

    def modes():
        res = {}
        for plat in ("Reddit", "Hacker News", "all"):
            for mode in ("live", "historical", "combined"):
                for unfiltered in (False, True):
                    if time.monotonic() - started > a.max_seconds:
                        raise TimeoutError("overall time cap reached")
                    with logics.search_mode_scope(mode) as info:
                        t0 = time.monotonic()
                        posts = logics.get_matched_signals("staging-verify", [] if unfiltered else ["crm"], plat, 10, 30, unfiltered, chat_id="staging-verify")
                    keys = [sm.post_identity(p) for p in posts]
                    res[f"{plat}|{mode}|{'general' if unfiltered else 'topic'}"] = {
                        "results": len(posts), "duplicates": len(keys) - len(set(keys)), "live": info["live"], "historical": info["historical"],
                        "paths": {k: sum(1 for p in posts if p.get("retrieval_path") == k) for k in ("live", "historical", "both")},
                        "classes": {c: sum(1 for p in posts if si.classify_post(p)["class"] == c) for c in si.CLASSES},
                        "elapsed_ms": int((time.monotonic() - t0) * 1000)}
        return judge_modes(res)
    if not a.allow_paid_calls:
        rep.checks.append({"check": CHK_MODES, "status": "SKIPPED", "elapsed_ms": 0,
                           "detail": {"reason": "paid calls are off by default: pass --allow-paid-calls (embeds each topic query through the LLM provider)"}})
    else:
        rep.run(CHK_MODES, modes)

    def zero():
        cov = logics.probe_stored_coverage("reddit", datetime(1990, 1, 1, tzinfo=timezone.utc), datetime(1990, 1, 2, tzinfo=timezone.utc))
        ex = si.explain_zero_result(dict(cov, mode="topic", topic="x"))
        return judge_zero(cov, ex["code"])
    rep.run(CHK_ZERO, zero)
    if a.mysql_probe:
        import mysql_signals as ms
        srcs = ms.configured_sources()
        rep.run(CHK_MYSQL,
                lambda: mysql_probe_review(ms.connect, srcs, None) if srcs else ("SKIPPED", {"reason": "no MYSQL_READ_DATABASES configured"}))
    else:
        rep.checks.append({"check": CHK_MYSQL, "status": "SKIPPED", "elapsed_ms": 0, "detail": {"reason": "--mysql-probe not given"}})
    if a.base_url:
        import httpx

        def http():
            r = httpx.get(a.base_url.rstrip("/") + "/api/health/ready", timeout=10)
            return ("PASS" if r.status_code == 200 and r.json().get("status") == "ok" else "FAIL"), {"status_code": r.status_code, "body": r.json()}
        rep.run(CHK_HTTP, http)
    else:
        rep.checks.append({"check": CHK_HTTP, "status": "SKIPPED", "elapsed_ms": 0, "detail": {"reason": "--base-url not given"}})
    mysql_required = str(os.environ.get("MYSQL_READ_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")
    out = dict(finalize_report(rep.checks, "live", mysql_required), generated_at=datetime.now(timezone.utc).isoformat(), checks=rep.checks,
               note="Values are counts, timings and error types only. A SKIPPED/UNKNOWN check was NOT verified.")
    with open(a.report, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("\nsummary:", out["summary"], "-> report written to", a.report)
    return 1 if out["summary"].get("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
