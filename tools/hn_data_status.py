#!/usr/bin/env python3
"""READ-ONLY Hacker News data status for a Flintel MongoDB (counts only; never prints a document body, a URL of a post, or a connection string).

Usage (staging or production env loaded by YOU; this tool writes nothing, builds no index, deletes nothing):
    python3 hn_data_status.py                 # reads MONGODB3 (or MONGODB_URI) and MONGODB_DB
    python3 hn_data_status.py --selftest      # runs against synthetic data (mongomock); no database needed

What it reports (from the collector's real storage shape: collection flintel_signals, platform "hackernews" = stories, "hackernews_comment" = comments;
state in flintel_hn_state / flintel_hn_cursor; heartbeat in flintel_service_status service="hackernews_poller"):
  totals per platform, oldest / newest publication time (created_utc), newest fetched_at, counts for the last 24h / 7d / 30d,
  records missing title / text / post_url / a valid created_utc / an embedding, duplicate message_id and post_url groups,
  distinct search keywords (the collector stores only posts that matched a configured keyword: this is NOT a general sample),
  collector heartbeat and checkpoint state.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

PLATFORMS = ["hackernews", "hackernews_comment"]


def utc(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    return None


def report(db, now=None):
    now = now or datetime.now(timezone.utc)
    sig = db["flintel_signals"]
    out = {"generated_at": now.isoformat(), "platforms": {}}
    base = {"platform": {"$in": PLATFORMS}}
    for plat in PLATFORMS:
        q = {"platform": plat}
        total = sig.count_documents(q)
        rec = {"total": total}
        if total:
            hi = sig.find(q, {"created_utc": 1, "fetched_at": 1}).sort("created_utc", -1).limit(1)
            lo = sig.find(q, {"created_utc": 1}).sort("created_utc", 1).limit(1)
            hi, lo = next(iter(hi), {}), next(iter(lo), {})
            rec["newest_created_utc"] = utc(hi.get("created_utc")).isoformat() if utc(hi.get("created_utc")) else None
            rec["oldest_created_utc"] = utc(lo.get("created_utc")).isoformat() if utc(lo.get("created_utc")) else None
            nf = next(iter(sig.find(q, {"fetched_at": 1}).sort("fetched_at", -1).limit(1)), {})
            rec["newest_fetched_at"] = utc(nf.get("fetched_at")).isoformat() if utc(nf.get("fetched_at")) else None
            for label, days in (("last_24h", 1), ("last_7d", 7), ("last_30d", 30)):
                rec[label] = sig.count_documents({**q, "created_utc": {"$gte": now - timedelta(days=days)}})
        out["platforms"][plat] = rec
    out["quality"] = {
        "missing_title": sig.count_documents({**base, "$or": [{"title": {"$exists": False}}, {"title": ""}, {"title": None}]}),
        "missing_text": sig.count_documents({**base, "$or": [{"text": {"$exists": False}}, {"text": ""}, {"text": None}]}),
        "missing_post_url": sig.count_documents({**base, "$or": [{"post_url": {"$exists": False}}, {"post_url": ""}, {"post_url": None}]}),
        "created_utc_not_a_date": sig.count_documents({**base, "created_utc": {"$not": {"$type": "date"}}}),
        "missing_embedding": sig.count_documents({**base, "$or": [{"embedding": {"$exists": False}}, {"embedding": None}]}),
    }
    dup = list(sig.aggregate([{"$match": base}, {"$group": {"_id": "$message_id", "n": {"$sum": 1}}}, {"$match": {"n": {"$gt": 1}}},
                              {"$group": {"_id": None, "groups": {"$sum": 1}, "extra_docs": {"$sum": {"$subtract": ["$n", 1]}}}}]))
    out["duplicates_by_message_id"] = {"groups": dup[0]["groups"], "extra_docs": dup[0]["extra_docs"]} if dup else {"groups": 0, "extra_docs": 0}
    kws = list(sig.aggregate([{"$match": base}, {"$group": {"_id": "$search_keyword", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}]))
    out["keywords"] = {"distinct": len(kws), "top": [{"keyword": k["_id"], "posts": k["n"]} for k in kws[:10]]}
    hb = db["flintel_service_status"].find_one({"service": "hackernews_poller"}, {"_id": 0})
    out["collector"] = {
        "heartbeat": ({"running": hb.get("running"), "updated_at": utc(hb.get("updated_at")).isoformat() if utc(hb.get("updated_at")) else None,
                       "age_minutes": round((now - utc(hb["updated_at"])).total_seconds() / 60, 1) if utc(hb.get("updated_at")) else None} if hb else None),
        "state_rows": db["flintel_hn_state"].count_documents({}),
        "cursor_present": db["flintel_hn_cursor"].count_documents({}) > 0,
    }
    return out


def verdict(r):
    notes = []
    tot = sum(p["total"] for p in r["platforms"].values())
    if tot == 0:
        notes.append("NO Hacker News posts are stored: the collector has stored nothing (check HACKERNEWS_ENABLED, its keyword list, and the heartbeat).")
    hb = r["collector"]["heartbeat"]
    if hb is None:
        notes.append("No collector heartbeat found (the poller has never reported, or uses another database).")
    elif hb["age_minutes"] is not None and hb["age_minutes"] > 60:
        notes.append(f"Collector heartbeat is {hb['age_minutes']} minutes old (poll interval default is 10 minutes).")
    if r["keywords"]["distinct"] and r["keywords"]["distinct"] <= 5:
        notes.append("Only a few search keywords have stored posts: the corpus is keyword-targeted, NOT a general sample of Hacker News.")
    q = r["quality"]
    if q["created_utc_not_a_date"] or q["missing_post_url"] or q["missing_title"]:
        notes.append("Some stored records are malformed (see quality counts).")
    if r["duplicates_by_message_id"]["extra_docs"]:
        notes.append("Duplicate message_id documents exist.")
    return notes


def selftest():
    import mongomock
    db = mongomock.MongoClient()["t"]
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    docs = [
        {"message_id": "hackernews_1", "platform": "hackernews", "title": "a", "text": "a", "post_url": "u1", "created_utc": now - timedelta(hours=2), "fetched_at": now, "search_keyword": "crm", "embedding": [0.1]},
        {"message_id": "hackernews_2", "platform": "hackernews", "title": "b", "text": "", "post_url": "u2", "created_utc": now - timedelta(days=3), "fetched_at": now, "search_keyword": "crm"},
        {"message_id": "hackernews_2", "platform": "hackernews", "title": "b", "text": "", "post_url": "u2", "created_utc": now - timedelta(days=3), "fetched_at": now, "search_keyword": "crm"},
        {"message_id": "hackernews_comment_3", "platform": "hackernews_comment", "title": "c", "text": "c", "post_url": "", "created_utc": "bad", "fetched_at": now, "search_keyword": "sales", "embedding": [0.2]},
    ]
    for d in docs:
        db["flintel_signals"].insert_one(d)
    db["flintel_service_status"].insert_one({"service": "hackernews_poller", "running": True, "updated_at": now - timedelta(minutes=5)})
    r = report(db, now)
    assert r["platforms"]["hackernews"]["total"] == 3 and r["platforms"]["hackernews_comment"]["total"] == 1
    assert r["platforms"]["hackernews"]["last_24h"] == 1 and r["platforms"]["hackernews"]["last_7d"] == 3
    assert r["quality"]["missing_text"] == 2 and r["quality"]["missing_post_url"] == 1 and r["quality"]["created_utc_not_a_date"] == 1
    assert r["quality"]["missing_embedding"] == 2 and r["duplicates_by_message_id"] == {"groups": 1, "extra_docs": 1}
    assert r["keywords"]["distinct"] == 2 and r["collector"]["heartbeat"]["age_minutes"] == 5.0
    assert any("keyword-targeted" in n for n in verdict(r))
    empty = report(mongomock.MongoClient()["e"], now)
    assert any("NO Hacker News posts" in n for n in verdict(empty))
    print("selftest OK (synthetic data only)")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    uri = os.getenv("MONGODB3") or os.getenv("MONGODB_URI")
    if not uri:
        sys.exit("Set MONGODB3 (or MONGODB_URI) and MONGODB_DB for the database you want to inspect. Nothing was contacted.")
    from pymongo import MongoClient
    db = MongoClient(uri, serverSelectionTimeoutMS=8000)[os.getenv("MONGODB_DB", "flintel_bot")]
    r = report(db)
    import json
    print(json.dumps(r, indent=2, default=str))
    print("\nFINDINGS:")
    for n in verdict(r) or ["No problems detected by these checks."]:
        print(" -", n)


if __name__ == "__main__":
    main()
