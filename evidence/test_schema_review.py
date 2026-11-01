"""Review probe (NOT part of the patch): eligibility against messages stored by the REAL index.add_search_to_chat."""
import json, sys
from datetime import datetime, timedelta, timezone
import mongomock, pytest
from tests.test_answer_persistence import env  # noqa: F401
import reddit_live as rl

NOW = datetime.now(timezone.utc)
OWNER = "alice@example.com"

@pytest.fixture
def store(env, monkeypatch):
    lg, idx, _ = env
    coll = mongomock.MongoClient()["rev"]["chats"]
    coll.insert_one({"chat_id": "c1", "owner_key": OWNER, "messages": []})
    monkeypatch.setattr(idx, "chats_collection", coll)
    monkeypatch.setattr(idx.turn_finisher, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(idx, "increment_message_count", lambda *a, **k: None)
    return idx, coll, lg

def saved(idx, coll, i, **kw):
    lg = sys.modules['logics']
    """Save exactly the way the credited accept path does (routes.py credit_message -> store.save_message -> add_search_to_chat)."""
    base = dict(time_window_days=None, unfiltered=False, website_context=None, match_phrases=["p"],
                evidence_required=20, website_note=None, resolved_query="reddit monitoring tool")
    base.update(kw)
    rid = kw.pop("credit_request_id", f"cr{i}") if "credit_request_id" in kw else f"cr{i}"
    base.pop("credit_request_id", None)
    with lg._usage_attribution(request_id=f"req{i}"):          # POST /search is wrapped by routes._usage_attributed exactly like this
        ok = idx.add_search_to_chat("c1", OWNER, "reddit monitoring tool", f"t{i}", ["reddit monitoring tool"], "reddit",
                                    credit_request_id=rid, **base)
    assert ok is True
    msg = coll.find_one({"chat_id": "c1"})["messages"][-1]
    msg["claude_answer"] = json.dumps({"format": "no_results"})
    msg["answered_at"] = NOW - timedelta(seconds=10)
    return msg

def test_real_stored_messages(store):
    idx, coll, lg = store
    # (a) the plain lead search exactly as stored
    m = saved(idx, coll, 0)
    assert m["request_id"] == "req0"
    assert "request_kind" not in m and m["credit_request_id"] == "cr0" and m["evidence_required"] == 20 and m["keywords"]
    assert rl.eligible(m, NOW) == (True, "ok")
    # (b) mention_search
    m = saved(idx, coll, 1, request_kind="mention_search", subject="Acme")
    assert m["request_kind"] == "mention_search" and rl.eligible(m, NOW) == (True, "ok")
    # (c) every other kind the router can store
    for i, kind in enumerate(("research", "provider_search", "person_mentions"), start=2):
        m = saved(idx, coll, i, request_kind=kind, subject="Acme")
        assert rl.eligible(m, NOW) == (False, "kind"), kind
    # (d) time-bounded two ways
    assert rl.eligible(saved(idx, coll, 5, time_window_days=7), NOW) == (False, "time_bounded")
    assert rl.eligible(saved(idx, coll, 6, time_range={"start": "2026-10-01", "end": "2026-10-08", "tz": "UTC"}), NOW) == (False, "time_bounded")
    # (e) website-derived
    assert rl.eligible(saved(idx, coll, 7, website_context={"url": "https://x.example"}), NOW) == (False, "website")
    assert rl.eligible(saved(idx, coll, 8, website_only=True), NOW) == (False, "website")
    assert rl.eligible(saved(idx, coll, 9, website_evidence={"url": "https://x.example"}), NOW) == (False, "website")
    # (f) evidence_required missing (router gave none) and keywords empty
    assert rl.eligible(saved(idx, coll, 10, evidence_required=None), NOW) == (False, "no_evidence_required")
    # (g) website_note alone (time-range/guidance note) does NOT change the outcome
    assert rl.eligible(saved(idx, coll, 11, website_note="Guidance note"), NOW) == (True, "ok")
    # (h) the legacy (non-credited) path stores no credit_request_id -> not eligible
    with lg._usage_attribution(request_id="req12"):
        ok = idx.add_search_to_chat("c1", OWNER, "q", "t12", ["k"], "reddit", evidence_required=20)
    m = coll.find_one({"chat_id": "c1"})["messages"][-1]; m["claude_answer"] = json.dumps({"format": "no_results"}); m["answered_at"] = NOW
    assert "credit_request_id" not in m and rl.eligible(m, NOW) == (False, "no_credit_request_id")
    # (i) the website-only message as routes.py:1837 creates it (no credit id, website_only, website_evidence)
    with lg._usage_attribution(request_id="req13"):
        idx.add_search_to_chat("c1", OWNER, "https://x.example", "t13", ["k"], "reddit", website_only=True,
                               website_evidence={"url": "https://x.example", "structured_evidence": {}, "evidence_quality": "ok"})
    m = coll.find_one({"chat_id": "c1"})["messages"][-1]; m["claude_answer"] = json.dumps({"format": "no_results"}); m["answered_at"] = NOW
    assert rl.eligible(m, NOW)[0] is False
    # (j) a chat (non-search) turn
    idx.add_chat_message_to_chat("c1", OWNER, "hi", "hello")
    m = coll.find_one({"chat_id": "c1"})["messages"][-1]
    assert m["message_type"] == "chat" and rl.eligible(dict(m, claude_answer="x", answered_at=NOW), NOW) == (False, "not_a_search")
