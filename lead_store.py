"""
lead_store.py — owner-scoped persistence of scans and leads (pure except for the injected collections).

`scans` and `leads` are pymongo-style collections (or test doubles) offering find_one / find / insert_one / update_one / count_documents.
EVERY read and write filters on `owner_key`; there is no function that takes a lead or scan id without the owner. A signed-in account
(an e-mail owner key) is required: guests have no leads.

Lead identity is (owner_key, source_id): scanning the same conversation again updates the stored lead's evidence and last-seen time but
KEEPS its status and notes, so a lead the user already marked "contacted" never returns as "new" and is never counted twice.
"""
import uuid
from datetime import datetime, timedelta, timezone

try:
    from pymongo.errors import DuplicateKeyError
except Exception:                                           # pymongo is a hard dependency of the app; this keeps the module importable without it
    class DuplicateKeyError(Exception):
        pass

STATUSES = ("new", "saved", "contacted", "not_interested")
_ALLOWED = {"new": {"saved", "contacted", "not_interested"}, "saved": {"new", "contacted", "not_interested"},
            "contacted": {"saved", "not_interested", "new"}, "not_interested": {"new", "saved"}}
MAX_NOTE = 2000
_SCAN_SUMMARY_KEYS = ("scan_id", "created_at", "since_days", "counts", "coverage", "statement", "timings", "concepts", "excluded")


def _owner(owner_key):
    if not isinstance(owner_key, str) or "@" not in owner_key:
        raise PermissionError("A signed-in account is required.")
    return owner_key


def _clean(doc):
    if not doc:
        return None
    d = dict(doc)
    d.pop("_id", None)
    return d


class LeadStore:
    def __init__(self, scans, leads, quota=None):
        self.scans, self.leads, self.quota = scans, leads, quota

    # ── atomic quotas and the per-owner scan lock ──
    # Both are single-document operations on `quota` (unique index on (owner_key, slot)): a conditional find_one_and_update ($lt / $lte guard plus
    # $inc / $set) and an insert that the unique index rejects when two workers race. No read-then-write gap exists, so the cap holds across threads
    # and across worker processes. A claim is taken BEFORE the work and never given back: a failed scan still counts ("at most once called, at least
    # once counted"), so failing requests cannot be repeated without limit.
    def claim(self, owner_key, kind, cap, now=None) -> bool:
        owner = _owner(owner_key)
        if self.quota is None or not isinstance(cap, int) or cap <= 0:
            return False
        now = now or datetime.now(timezone.utc)
        key = {"owner_key": owner, "slot": f"{kind}:{now.strftime('%Y-%m-%d')}"}
        guard = dict(key, count={"$lt": cap})
        if self.quota.find_one_and_update(guard, {"$inc": {"count": 1}}) is not None:
            return True
        try:
            self.quota.insert_one(dict(key, count=1, created_at=now.isoformat()))
            return True
        except DuplicateKeyError:
            pass                                            # the day's document exists (full, or another worker just created it): one more try
        return self.quota.find_one_and_update(guard, {"$inc": {"count": 1}}) is not None

    def acquire_scan_lock(self, owner_key, seconds=120, now=None) -> bool:
        """At most one scan per owner at a time (double submits, two tabs, two workers). The lease expires on its own, so a crashed worker cannot
        lock an owner out."""
        owner = _owner(owner_key)
        if self.quota is None:
            return False
        now = now or datetime.now(timezone.utc)
        key = {"owner_key": owner, "slot": "scan_lock"}
        until = (now + timedelta(seconds=seconds)).isoformat()
        if self.quota.find_one_and_update(dict(key, until={"$lte": now.isoformat()}), {"$set": {"until": until}}) is not None:
            return True
        try:
            self.quota.insert_one(dict(key, until=until))
            return True
        except DuplicateKeyError:
            return False

    def release_scan_lock(self, owner_key):
        if self.quota is not None:
            self.quota.update_one({"owner_key": _owner(owner_key), "slot": "scan_lock"}, {"$set": {"until": "1970-01-01T00:00:00+00:00"}})

    # ── scans ──
    def save_scan(self, owner_key, scan: dict) -> dict:
        owner = _owner(owner_key)
        scan_id = uuid.uuid4().hex
        now = scan.get("created_at") or datetime.now(timezone.utc).isoformat()
        summary = {k: scan.get(k) for k in _SCAN_SUMMARY_KEYS if k != "scan_id"}
        summary.update(scan_id=scan_id, owner_key=owner, created_at=now, lead_source_ids=[l["source_id"] for l in scan.get("leads", [])])
        self.scans.insert_one(dict(summary))
        for lead in scan.get("leads", []):
            self._upsert_lead(owner, scan_id, now, lead)
        return {k: v for k, v in summary.items() if k != "owner_key"}

    def _upsert_lead(self, owner, scan_id, now, lead):
        key = {"owner_key": owner, "source_id": lead["source_id"]}
        existing = self.leads.find_one(key)
        fresh = {k: v for k, v in lead.items() if k not in ("status", "notes", "lead_id")}
        fresh.update(last_seen_scan=scan_id, last_seen_at=now)
        if existing:
            self.leads.update_one(key, {"$set": fresh})
        else:
            self.leads.insert_one(dict(fresh, owner_key=owner, lead_id=uuid.uuid4().hex, status="new", notes="", first_seen_scan=scan_id,
                                       first_seen_at=now))

    def get_scan(self, owner_key, scan_id):
        return _clean(self.scans.find_one({"owner_key": _owner(owner_key), "scan_id": scan_id}))

    def list_scans(self, owner_key, limit=20):
        cur = self.scans.find({"owner_key": _owner(owner_key)})
        rows = sorted((_clean(d) for d in cur), key=lambda d: d.get("created_at") or "", reverse=True)
        return rows[:limit]

    def scans_since(self, owner_key, since_iso: str) -> int:
        return sum(1 for d in self.scans.find({"owner_key": _owner(owner_key)}) if (d.get("created_at") or "") >= since_iso)

    # ── leads ──
    def get_lead(self, owner_key, lead_id):
        return _clean(self.leads.find_one({"owner_key": _owner(owner_key), "lead_id": lead_id}))

    def list_leads(self, owner_key, status=None, scan_id=None):
        flt = {"owner_key": _owner(owner_key)}
        if status:
            if status not in STATUSES:
                raise ValueError("unknown status")
            flt["status"] = status
        rows = [_clean(d) for d in self.leads.find(flt)]
        if scan_id:
            sc = self.get_scan(owner_key, scan_id)
            ids = set((sc or {}).get("lead_source_ids") or [])
            rows = [r for r in rows if r["source_id"] in ids]
        return rows

    def set_status(self, owner_key, lead_id, status):
        owner = _owner(owner_key)
        if status not in STATUSES:
            raise ValueError("unknown status")
        lead = self.get_lead(owner, lead_id)
        if lead is None:
            return None
        if status != lead["status"] and status not in _ALLOWED[lead["status"]]:
            raise ValueError(f"cannot move a lead from {lead['status']} to {status}")
        self.leads.update_one({"owner_key": owner, "lead_id": lead_id}, {"$set": {"status": status, "status_changed_at": datetime.now(timezone.utc).isoformat()}})
        return self.get_lead(owner, lead_id)

    def set_notes(self, owner_key, lead_id, notes):
        owner = _owner(owner_key)
        if self.get_lead(owner, lead_id) is None:
            return None
        self.leads.update_one({"owner_key": owner, "lead_id": lead_id}, {"$set": {"notes": str(notes or "")[:MAX_NOTE]}})
        return self.get_lead(owner, lead_id)

    def counts(self, owner_key, now=None) -> dict:
        import lead_engine                                       # local: lead_engine does not import this module
        rows = [lead_engine.refresh_lead(r, now) for r in self.list_leads(owner_key)]
        out = {s: 0 for s in STATUSES}
        for r in rows:
            out[r.get("status", "new")] = out.get(r.get("status", "new"), 0) + 1
        out["total"] = len(rows)
        out["high_potential_new"] = sum(1 for r in rows if r.get("status") == "new" and r.get("label") == "High potential")
        return out
