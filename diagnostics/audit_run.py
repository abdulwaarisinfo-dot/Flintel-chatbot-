#!/usr/bin/env python3
"""
diagnostics/audit_run.py  -  READ-ONLY audit of ONE saved chat message ("7 buyers" claim).
Run on YOUR machine with your own env (MONGODB3, MONGODB_URI, MONGODB2, MONGODB4, MONGODB_DB).
Never writes to Mongo, never imports database.py/logics.py (their import runs create_index),
never prints connection strings. Only: find / find_one / count_documents with projections.

USAGE
  python diagnostics/audit_run.py --list --query-prefix "Find 25 Reddit posts from business owners"
  python diagnostics/audit_run.py --chat-id <id> --topic-key <key> --out audit_evidence.json
  (--topic-key optional: without it every message of that chat is audited.)

OUTPUT  audit_evidence.json  (owner_key/user ids/emails redacted; Reddit usernames kept)
  per message: saved fields, claim-vs-listed count, unique-URL/title duplicate check,
  every listed post (url/title/post_text from the signal DB, NOT from the LLM summary),
  card list, topic evidence cache (posts + strict_mode), and an explicit "not_saved" list.
Verdict fields are left "UNREVIEWED": a human (or reviewer) fills GENUINE BUYER / SELLER /
JOB LISTING / GENERAL DISCUSSION / UNCLEAR with a quote. This script does not guess.
"""
import argparse, json, os, re, sys
from collections import Counter

NOT_SAVED = [
    "router output beyond message.keywords/match_phrases/time_window_days/unfiltered (only what is on the message)",
    "scan counts (pool / passed_threshold / scanned / kept) - Render logs only",
    "classifier/bridge per-post labels (intent, actor, specificity, confidence) - not persisted",
    "the exact [Post N] list sent to the LLM - not persisted",
    "raw LLM JSON (claude_answer is stored AFTER _strict_patch_answer)",
    "strict-patch kept/dropped counts and reasons - not logged, not persisted",
]
CLAIM_RE = re.compile(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b[^.\n]{0,40}"
                      r"(qualified|genuine|buyer|results?|posts?)", re.I)
WORDS = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12}
SECRET_KEYS = {"owner_key", "user_id", "email", "token", "password", "api_key"}


def _redact(o):
    if isinstance(o, dict):
        return {k: ("<redacted>" if k in SECRET_KEYS else _redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [_redact(x) for x in o]
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if type(o).__name__ == "ObjectId":
        return str(o)
    return o


def _client(var):
    uri = os.getenv(var)
    if not uri:
        return None
    from pymongo import MongoClient
    return MongoClient(uri, serverSelectionTimeoutMS=15000)


def _answer_posts(claude_answer):
    """Posts the SAVED answer lists + the opening text. Returns (opening, posts, parse_ok)."""
    if not isinstance(claude_answer, str) or not claude_answer.strip():
        return None, [], False
    s = claude_answer.strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    try:
        data = json.loads(s)
    except Exception:
        return claude_answer[:600], [], False
    posts = []
    for plat in (data.get("platforms") or []):
        for p in (plat.get("posts") or []):
            posts.append({"platform": plat.get("platform") or plat.get("name"), **p})
    # every top-level string field is scanned for the "N qualified/genuine" claim
    opening = " | ".join(f"{k}: {v}" for k, v in data.items() if isinstance(v, str))
    return opening, posts, True


def _claimed(text):
    if not isinstance(text, str):
        return None
    m = CLAIM_RE.search(text)
    if not m:
        return None
    t = m.group(1).lower()
    return int(t) if t.isdigit() else WORDS.get(t)


def _find_signal(clients, url):
    for label, db_name, cl in clients:
        coll = cl[db_name]["flintel_signals"]
        for field in ("post_url", "url", "link", "permalink"):
            d = coll.find_one({field: url}, {"embedding": 0, "embedding_vector": 0})
            if d:
                return label, d
    return None, None


def audit_message(msg, clients, cache_doc):
    opening, listed, ok = _answer_posts(msg.get("claude_answer"))
    cards = msg.get("results")
    urls = [p.get("link") or p.get("url") or p.get("post_url") for p in listed]
    titles = [(p.get("title") or "").strip().lower() for p in listed]
    rec = {
        "message_meta": {k: msg.get(k) for k in (
            "query", "topic_key", "keywords", "match_phrases", "time_window_days",
            "unfiltered", "targeting_platform", "evidence_required", "requested_at",
            "google_fallback_triggered", "strict_dropped_posts")},
        "claimed_count_in_text": _claimed(opening if opening else msg.get("claude_answer")),
        "answer_json_parsed": ok,
        "listed_post_entries": len(listed),
        "listed_unique_urls": len({u for u in urls if u}),
        "listed_unique_titles": len({t for t in titles if t}),
        "duplicate_urls": [u for u, c in Counter(urls).items() if u and c > 1],
        "duplicate_titles": [t for t, c in Counter(titles).items() if t and c > 1],
        "results_cards_count": None if cards is None else len(cards),
        "results_cards_unique_urls": None if cards is None else len({(c or {}).get("post_url") or (c or {}).get("url") for c in cards}),
        "claude_answer_saved_post_patch": msg.get("claude_answer"),
        "posts": [],
        "not_saved": NOT_SAVED,
    }
    for i, p in enumerate(listed, 1):
        url = p.get("link") or p.get("url") or p.get("post_url")
        label, sig = _find_signal(clients, url) if url else (None, None)
        rec["posts"].append({
            "n": i, "answer_entry": p, "found_in_collection": label,
            "db_record": None if not sig else {k: sig.get(k) for k in (
                "post_url", "url", "title", "post_text", "text", "body", "subreddit",
                "created_utc", "reddit_fetched", "author", "platform", "_id")},
            "verdict": "UNREVIEWED", "verdict_quote": None, "duplicate_of": None,
        })
    seen = {}
    for p in rec["posts"]:
        key = (p["answer_entry"].get("link") or p["answer_entry"].get("url") or "").strip()
        if key in seen:
            p["duplicate_of"] = seen[key]
        elif key:
            seen[key] = p["n"]
    if cache_doc:
        cp = cache_doc.get("posts") or []
        rec["topic_evidence_cache"] = {
            "evidence_count": cache_doc.get("evidence_count"), "strict_mode": cache_doc.get("strict_mode"),
            "updated_at": cache_doc.get("updated_at"), "scan_sims_present": bool(cache_doc.get("scan_sims")),
            "posts_in_cache": len(cp),
            "cache_unique_urls": len({(x or {}).get("post_url") or (x or {}).get("url") for x in cp}),
            "cache_posts": [{k: (x or {}).get(k) for k in ("post_url", "url", "title", "post_text", "subreddit", "created_utc")} for x in cp],
        }
    else:
        rec["topic_evidence_cache"] = "maloom nahi (no cache doc found)"
    return rec


def main():
    ap = argparse.ArgumentParser(description="READ-ONLY audit of a saved Flintel chat message")
    ap.add_argument("--chat-id"); ap.add_argument("--topic-key")
    ap.add_argument("--list", action="store_true"); ap.add_argument("--query-prefix")
    ap.add_argument("--out", default="audit_evidence.json")
    a = ap.parse_args()
    db_name = os.getenv("MONGODB_DB", "flintel_bot")
    c3 = _client("MONGODB3")
    if not c3:
        sys.exit("MONGODB3 env not set (needed to read chats). Nothing was read.")
    chats = c3[db_name]["flintel_users_chat"]
    cache = c3[db_name]["flintel_topic_evidence_cache"]
    clients = [(l, db_name, c) for l, c in (("primary", _client("MONGODB_URI")), ("mongo_2", _client("MONGODB2")), ("mongo_4", _client("MONGODB4"))) if c]

    if a.list:
        q = {"messages.query": {"$regex": "^" + re.escape(a.query_prefix or "Find 25 Reddit posts")}}
        n = 0
        for c in chats.find(q, {"chat_id": 1, "messages.query": 1, "messages.topic_key": 1, "messages.requested_at": 1, "messages.claude_answer": 1}):
            for m in c.get("messages", []):
                if (m.get("query") or "").startswith(a.query_prefix or "Find 25 Reddit posts"):
                    _o, lp, _ok = _answer_posts(m.get("claude_answer"))
                    print(json.dumps({"chat_id": c.get("chat_id"), "topic_key": m.get("topic_key"),
                                      "requested_at": str(m.get("requested_at")), "claimed": _claimed(_o), "listed": len(lp)}))
                    n += 1
        print(f"# {n} matching message(s)")
        return
    if not a.chat_id:
        sys.exit("--chat-id required (use --list first)")
    chat = chats.find_one({"chat_id": a.chat_id})
    if not chat:
        sys.exit("chat not found")
    out = []
    for m in chat.get("messages", []):
        if a.topic_key and m.get("topic_key") != a.topic_key:
            continue
        cd = cache.find_one({"chat_id": a.chat_id, "topic_key": m.get("topic_key")})
        out.append(audit_message(m, clients, cd))
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(_redact(out), f, ensure_ascii=False, indent=2)
    print(f"wrote {a.out} ({len(out)} message(s)); no Mongo writes performed")


if __name__ == "__main__":
    main()
