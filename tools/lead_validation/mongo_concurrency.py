"""
mongo_concurrency.py — multi-PROCESS verification of the lead scan quota and scan lock (lead_store.LeadStore.claim / acquire_scan_lock).

    python -m tools.lead_validation.mongo_concurrency                     # dry run: prints the plan
    python -m tools.lead_validation.mongo_concurrency --execute --approved-by "Owner Name" --processes 6        # REAL staging MongoDB

Real mode needs FLINTEL_STAGING_MONGO_URI (a staging cluster the owner approved), FLINTEL_STAGING_APPROVED=yes and FLINTEL_STAGING_ENV_NAME=...staging...
It creates a throw-away database `flintel_leadtest_<random>`, runs the scenarios below with separate OS processes (multiprocessing "spawn"), prints a JSON
report (no secrets) and drops the database.

`--backend fake-manager` is a SELF-TEST of the harness only (a manager-backed in-memory double shared by processes). It proves the orchestration works; it proves
NOTHING about MongoDB, and its report says so (`"mongo": false`). Multi-worker safety is verified only by a real run.

Scenarios (each reports PASS/FAIL with the observed numbers):
  quota_exact              N processes x M claims each against cap C: exactly C succeed
  lock_exclusive           N processes race for the scan lock: exactly one wins
  lock_expiry_after_crash  a child takes a 3 s lock and dies with os._exit(1); another process acquires it after the lease expires, not before
  failed_scan_consumes     a child claims, then "fails" before saving anything: the claim is not given back (count stays)
  duplicate_requests       the same owner submits K identical scan requests at once under a high cap and ONE lock: exactly one scan runs
  daily_boundary           claims just before and just after 00:00 UTC use different counters; the cap applies per UTC day
  other_owner_isolated     one owner exhausting the cap does not affect another owner
"""
import argparse
import json
import multiprocessing as mp
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.lead_validation._guard import redact, require_approval  # noqa: E402


# ── backends ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def make_collection(backend, uri, dbname, shared):
    if backend == "mongo":
        import pymongo
        coll = pymongo.MongoClient(uri, serverSelectionTimeoutMS=8000)[dbname]["flintel_lead_quota"]
        coll.create_index([("owner_key", 1), ("slot", 1)], unique=True)
        return coll
    return _ManagerColl(shared)


class _ManagerColl:
    """Harness self-test double: documents live in a multiprocessing.Manager dict, one lock makes single-document operations atomic."""
    def __init__(self, shared):
        self.docs, self.lock = shared["docs"], shared["lock"]

    def _match(self, d, f):
        for k, v in f.items():
            if isinstance(v, dict):
                if "$lt" in v and not (d.get(k) is not None and d[k] < v["$lt"]):
                    return False
                if "$lte" in v and not (d.get(k) is not None and d[k] <= v["$lte"]):
                    return False
            elif d.get(k) != v:
                return False
        return True

    def find_one_and_update(self, f, upd):
        with self.lock:
            for key in list(self.docs.keys()):
                d = dict(self.docs[key])
                if self._match(d, f):
                    for k, v in upd.get("$inc", {}).items():
                        d[k] = d.get(k, 0) + v
                    d.update(upd.get("$set", {}))
                    self.docs[key] = d
                    return d
        return None

    def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError
        with self.lock:
            key = (doc["owner_key"], doc["slot"])
            if key in self.docs:
                raise DuplicateKeyError("dup")
            self.docs[key] = dict(doc)

    def update_one(self, f, upd, upsert=False):
        with self.lock:
            for key in list(self.docs.keys()):
                d = dict(self.docs[key])
                if self._match(d, f):
                    d.update(upd["$set"])
                    self.docs[key] = d
                    return


def _store(ctx):
    import lead_store
    return lead_store.LeadStore(None, None, make_collection(ctx["backend"], ctx["uri"], ctx["db"], ctx.get("shared")))


# ── worker entry points (module level so "spawn" can import them) ────────────────────────────────────────────────────────────
def w_claims(ctx, owner, cap, n, kind="scan", now_iso=None):
    st = _store(ctx)
    now = datetime.fromisoformat(now_iso) if now_iso else None
    return sum(1 for _ in range(n) if st.claim(owner, kind, cap, now))


def w_lock(ctx, owner, seconds, barrier_ts):
    st = _store(ctx)
    while time.time() < barrier_ts:
        time.sleep(0.001)
    return st.acquire_scan_lock(owner, seconds)


def w_crash_holding_lock(ctx, owner, seconds):
    st = _store(ctx)
    assert st.acquire_scan_lock(owner, seconds)
    os._exit(1)                                   # the process dies without releasing the lock


def w_claim_then_fail(ctx, owner, cap):
    st = _store(ctx)
    assert st.claim(owner, "scan", cap)           # the claim is taken first ...
    raise RuntimeError("simulated scan failure")  # ... and the scan fails: the claim must stay consumed


def w_one_scan(ctx, owner, barrier_ts):
    """What the route does for one request: lock, claim, 'scan' (sleep), unlock. Returns 'ran' | 'busy' | 'cap'."""
    st = _store(ctx)
    while time.time() < barrier_ts:
        time.sleep(0.001)
    if not st.acquire_scan_lock(owner, 30):
        return "busy"
    try:
        if not st.claim(owner, "scan", 1000):
            return "cap"
        time.sleep(0.4)
        return "ran"
    finally:
        st.release_scan_lock(owner)


def _pool_map(n, fn, args_list):
    ctx = mp.get_context("spawn")
    with ctx.Pool(n) as pool:
        return pool.starmap(fn, args_list)


# ── scenarios ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def run_all(ctx, procs):
    out = []
    owner = lambda: f"{uuid.uuid4().hex[:8]}@test.invalid"   # noqa: E731
    uidx = lambda: _store(ctx)                                # noqa: E731

    def rec(name, ok, **detail):
        out.append({"scenario": name, "result": "PASS" if ok else "FAIL", **detail})

    o, cap, per = owner(), 7, 10
    wins = sum(_pool_map(procs, w_claims, [(ctx, o, cap, per)] * procs))
    rec("quota_exact", wins == cap, processes=procs, claims_attempted=procs * per, cap=cap, succeeded=wins)

    o = owner()
    barrier = time.time() + 2.0
    got = _pool_map(procs, w_lock, [(ctx, o, 60, barrier)] * procs)
    rec("lock_exclusive", sum(got) == 1, processes=procs, winners=sum(got))

    o = owner()
    mpctx = mp.get_context("spawn")
    p = mpctx.Process(target=w_crash_holding_lock, args=(ctx, o, 3))
    p.start(); p.join()
    t0 = time.time()
    early = uidx().acquire_scan_lock(o, 30)                   # lease still running: must be refused
    while time.time() - t0 < 6 and not early:
        time.sleep(0.25)
        early = uidx().acquire_scan_lock(o, 30)
    waited = round(time.time() - t0, 2)
    rec("lock_expiry_after_crash", p.exitcode == 1 and 1.5 <= waited <= 6, child_exit_code=p.exitcode, acquired_after_seconds=waited, lease_seconds=3)

    o, cap = owner(), 5
    ps = [mpctx.Process(target=w_claim_then_fail, args=(ctx, o, cap)) for _ in range(3)]
    [x.start() for x in ps]; [x.join() for x in ps]
    left = sum(1 for _ in range(10) if uidx().claim(o, "scan", cap))
    rec("failed_scan_consumes", left == cap - 3 and all(x.exitcode != 0 for x in ps), failed_scans=3, cap=cap, claims_still_available=left)

    o = owner()
    barrier = time.time() + 2.0
    res = _pool_map(procs, w_one_scan, [(ctx, o, barrier)] * procs)
    rec("duplicate_requests", res.count("ran") == 1 and res.count("busy") == procs - 1, outcomes={k: res.count(k) for k in ("ran", "busy", "cap")})

    o, cap = owner(), 3
    before = datetime(2026, 6, 1, 23, 59, 59, tzinfo=timezone.utc)
    after = before + timedelta(seconds=1)
    st = uidx()
    day1 = sum(1 for _ in range(6) if st.claim(o, "scan", cap, before))
    day2 = sum(1 for _ in range(6) if st.claim(o, "scan", cap, after))
    rec("daily_boundary", (day1, day2) == (cap, cap), day1_claims=day1, day2_claims=day2, cap=cap)

    a, b, cap = owner(), owner(), 2
    wa = sum(_pool_map(procs, w_claims, [(ctx, a, cap, 5)] * procs))
    wb = sum(_pool_map(1, w_claims, [(ctx, b, cap, 5)]))
    rec("other_owner_isolated", (wa, wb) == (cap, cap), owner_a=wa, owner_b=wb, cap=cap)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--approved-by")
    ap.add_argument("--processes", type=int, default=6)
    ap.add_argument("--backend", choices=["mongo", "fake-manager"], default="mongo")
    a = ap.parse_args(argv)
    what = (f"create a throw-away database on the staging MongoDB at FLINTEL_STAGING_MONGO_URI, run {a.processes}-process scenarios "
            "(quota, lock, crash recovery, failed scans, duplicates, day boundary, isolation), print the report and drop the database")
    if a.backend == "fake-manager":
        a.execute = True                                         # a self-test touches no real system
    elif not require_approval(a.approved_by, what, not a.execute):
        return 0
    uri = os.getenv("FLINTEL_STAGING_MONGO_URI", "")
    if a.backend == "mongo" and not uri:
        print("REFUSED: FLINTEL_STAGING_MONGO_URI is not set", file=sys.stderr)
        return 2
    dbname = f"flintel_leadtest_{uuid.uuid4().hex[:10]}"
    mgr = mp.get_context("spawn").Manager() if a.backend == "fake-manager" else None
    shared = {"docs": mgr.dict(), "lock": mgr.Lock()} if mgr else None
    ctx = {"backend": a.backend, "uri": uri, "db": dbname, "shared": shared}
    report = {"harness": "mongo_concurrency", "backend": a.backend, "mongo": a.backend == "mongo", "approved_by": a.approved_by, "database": dbname,
              "uri": redact(uri) if uri else None, "processes": a.processes, "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        report["scenarios"] = run_all(ctx, a.processes)
    finally:
        if a.backend == "mongo":
            import pymongo
            pymongo.MongoClient(uri, serverSelectionTimeoutMS=8000).drop_database(dbname)
            report["dropped_database"] = True
    report["all_passed"] = all(s["result"] == "PASS" for s in report["scenarios"])
    report["verified_multi_worker_mongo"] = bool(report["all_passed"] and a.backend == "mongo")
    if a.backend != "mongo":
        report["note"] = "harness self-test with an in-memory double: NOT evidence about MongoDB"
    print(json.dumps(report, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
