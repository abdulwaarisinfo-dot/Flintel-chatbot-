"""
tests/test_independent_chats_stop_retry.py — P0: independent searches across chats, a real Stop, and retry.

Root causes covered (see routes.py / index.py):

  1. "Search failed — Please wait for your current request to finish first." came from a busy flag keyed by OWNER ONLY
     (busy_owners.owner_key, unique): while ANY stream of an owner was alive — including one the browser had already
     dropped but whose thread was still inside a blocking model call — POST /search of EVERY other chat of that owner was
     declined. The flag is now keyed by owner + chat (index.busy_scope_key), a duplicate submission in the SAME chat is
     still declined, and a safety cap (MAX_CONCURRENT_SEARCHES_PER_OWNER) bounds the owner's total.
  2. There was no Stop: closing the stream only happened on navigation, so a "stopped" UI would only have hidden the work.
     POST /chat/{chat_id}/cancel now marks the message stopped atomically (index.mark_turn_cancelled), signals the running
     stream (turn_control) and frees the chat's busy flag; the stream stops its scan, never calls the model after a stop
     and never saves or shows an answer for a stopped turn.

Everything runs against the REAL routes (routes.search / routes.stream_answer / routes.cancel_turn) and the REAL index
helpers over mongomock; only the scan, the model call and the router are stubs. The browser tests run the real app on a
real HTTP server with real SSE. Two chats are two independent server-side requests, never two panels over one request.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.requests import Request

from tests.test_answer_persistence import env  # noqa: F401  (fixture)
from tests.test_nonblocking_chat_page import POSTS, fin, live_server, _answer_for  # noqa: F401  (fixtures + helpers)
from tests.test_reliability_ux_perf import OWNER, app_env  # noqa: F401  (fixture)

mongomock = pytest.importorskip("mongomock")
ROOT = Path(__file__).resolve().parent.parent

Q_A = "Find people looking for a CRM for small teams"
Q_B = "Find people complaining about invoicing software"
Q_C = "Find people asking for a project management tool"
Q_D = "Find people looking for an email marketing tool"


# ── harness ──────────────────────────────────────────────────────────────────────────────────────────────────────────
class Ctl:
    """Per-topic gates for the stubbed scan and model call, plus what they observed."""
    def __init__(self):
        self.hold_scan, self.hold_analyze = {}, {}
        self.scan_started, self.scan_cancelled, self.analyze_started, self.analyze_done = [], [], [], []
        self.lock = threading.Lock()

    def hold(self, query, scan=False, analyze=False):
        tk = _tk(query)
        if scan:
            self.hold_scan[tk] = threading.Event()
        if analyze:
            self.hold_analyze[tk] = threading.Event()
        return tk

    def release(self, query, scan=True, analyze=True):
        tk = _tk(query)
        if scan and tk in self.hold_scan:
            self.hold_scan[tk].set()
        if analyze and tk in self.hold_analyze:
            self.hold_analyze[tk].set()


def _tk(query):
    return query.strip().lower().rstrip(".")


@pytest.fixture
def conc(fin, monkeypatch):
    """Real routes + real busy flags / chats (mongomock); the scan, the model and the router are stubs we can gate."""
    f = fin
    idx, rt = f["idx"], f["rt"]
    ctl = Ctl()
    owners = {"default": OWNER}
    # database.py creates this index in production (busy_owners_collection.create_index("owner_key", unique=True)); the
    # mongomock collection needs it declared the same way for the atomic slot / flag inserts to be meaningful
    idx.busy_owners_collection.create_index("owner_key", unique=True)

    def get_owner(request):
        try:
            hdr = dict(request.headers).get("x-test-owner")
        except Exception:
            hdr = None
        return (hdr or owners["default"], "guest")

    for mod in (rt, idx):
        monkeypatch.setattr(mod, "get_owner", get_owner, raising=False)
    monkeypatch.setattr(rt, "_is_owner_busy", idx._is_owner_busy)             # the REAL busy flag
    monkeypatch.setattr(rt, "_set_owner_busy", idx._set_owner_busy)
    monkeypatch.setattr(rt, "_clear_owner_busy", idx._clear_owner_busy)
    fl = sys.modules["flintel"]
    monkeypatch.setattr(fl, "calculate_search_progress_percent", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(fl, "is_time_only_request", lambda r: False, raising=False)
    monkeypatch.setattr(rt, "get_user_chat_list", lambda owner_key: [])
    monkeypatch.setattr(rt, "get_current_user", lambda request: None)

    def scan(cancel_event=None, **kw):
        tk = kw.get("topic_key")
        with ctl.lock:
            ctl.scan_started.append(tk)
        gate = ctl.hold_scan.get(tk)
        end = time.time() + 25
        while gate is not None and not gate.is_set() and time.time() < end:
            if cancel_event is not None and cancel_event.is_set():
                with ctl.lock:
                    ctl.scan_cancelled.append(tk)
                return []
            time.sleep(0.02)
        return list(POSTS)

    def analyze(query, pool, extra_context=None, **k):
        tk = _tk(query)
        with ctl.lock:
            ctl.analyze_started.append(tk)
        gate = ctl.hold_analyze.get(tk)
        if gate is not None:
            gate.wait(25)
        with ctl.lock:
            ctl.analyze_done.append(tk)
        return _answer_for(pool)

    monkeypatch.setattr(rt, "get_evidence_with_topup", scan)
    monkeypatch.setattr(rt, "analyze_with_claude", analyze)
    monkeypatch.setattr(rt, "append_to_chat_summary", lambda *a, **k: None)
    monkeypatch.setattr(rt, "STREAM_CHUNK_DELAY_SECONDS", 0)               # the typing-out pacing is not under test
    f.update({"ctl": ctl, "owners": owners, "streams": []})
    yield f
    for gate in list(ctl.hold_scan.values()) + list(ctl.hold_analyze.values()):    # no test leaves a stream thread running
        gate.set()
    for s_ in f["streams"]:
        s_.thread.join(10)


def _request(f, method="POST", path="/search", owner=None, session=None):
    headers = [(b"x-test-owner", owner.encode())] if owner else []
    return Request({"type": "http", "method": method, "path": path, "headers": headers, "query_string": b"",
                    "session": session if session is not None else {}, "app": f["idx"].app,
                    "router": f["idx"].app.router})


def send(f, query, chat_id="", owner=None, session=None, key=None):
    """POST /search through the real route. chat_id="" = the new-chat form."""
    import uuid
    return f["rt"].search(_request(f, owner=owner, session=session), query=query, platform="All Platforms",
                          chat_id=chat_id, submit_key=key or str(uuid.uuid4()))


def chat_of(resp):
    assert resp.status_code == 303 and resp.headers["location"].startswith("/chat/"), getattr(resp, "body", resp)
    return resp.headers["location"].rsplit("/", 1)[-1]


def msg_of(f, chat_id, owner=OWNER, i=-1):
    return f["chats"].find_one({"chat_id": chat_id, "owner_key": owner})["messages"][i]


def start_stream(f, chat_id, query, owner=OWNER):
    """The turn's SSE generator, driven on its own thread; returns an object with .events / .done / .join()."""
    resp = f["rt"].stream_answer(_request(f, "GET", f"/chat/{chat_id}/stream", owner=owner), chat_id, _tk(query))
    gen = resp._source_generator
    out = type("S", (), {})()
    out.events, out.error = [], None

    def run():
        try:
            for chunk in gen:
                out.events.append(chunk)
        except BaseException as exc:                                  # pragma: no cover
            out.error = exc
    out.thread = threading.Thread(target=run, daemon=True)
    f["streams"].append(out)
    out.thread.start()
    out.finished = lambda: not out.thread.is_alive()
    out.joined = lambda t=10: (out.thread.join(t), not out.thread.is_alive())[1]
    out.payloads = lambda: [json.loads(c[6:]) for c in out.events if isinstance(c, str) and c.startswith("data: ")]
    out.kinds = lambda: {k for p in out.payloads() for k in p}
    return out


def wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def cancel(f, chat_id, query=None, request_id=None, owner=None):
    resp = f["rt"].cancel_turn(_request(f, path=f"/chat/{chat_id}/cancel", owner=owner), chat_id,
                               topic_key=_tk(query) if query else None, request_id=request_id)
    return resp.status_code, json.loads(resp.body)


def busy_keys(f):
    """the per-chat busy flags (the owner's search-slot documents are listed by slot_holders)"""
    return sorted(d["owner_key"] for d in f["busy"].find({}) if "#slot" not in d["owner_key"])


def slot_holders(f, owner=OWNER):
    return sorted(d["holder"] for d in f["busy"].find({}) if d["owner_key"].startswith(owner + "#slot"))


# ═══ 1. independent searches across chats ════════════════════════════════════════════════════════════════════════════
def test_a_search_running_in_chat_a_does_not_block_a_search_in_a_new_chat(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)                       # chat A is genuinely searching
    assert busy_keys(f) == [f["idx"].busy_scope_key(OWNER, chat_a)]         # its flag is per chat, not per owner

    resp_b = send(f, Q_B)                                                    # the NEW chat posts while A runs
    chat_b = chat_of(resp_b)                                                 # NOT the "Please wait..." page
    assert chat_b != chat_a
    sb = start_stream(f, chat_b, Q_B)
    assert sb.joined(10) and "done" in sb.kinds()                           # B ran to completion while A is still running
    assert not sa.finished() and "done" not in sa.kinds()

    ctl.release(Q_A)
    assert sa.joined(10) and "done" in sa.kinds()
    assert busy_keys(f) == []                                               # every flag released


def test_messages_results_and_status_stay_in_their_own_chat(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    chat_b = chat_of(send(f, Q_B))
    sb = start_stream(f, chat_b, Q_B)
    assert sb.joined(10)
    docs = {c: f["chats"].find_one({"chat_id": c}) for c in (chat_a, chat_b)}
    assert [m["query"] for m in docs[chat_a]["messages"]] == [Q_A] and [m["query"] for m in docs[chat_b]["messages"]] == [Q_B]
    assert docs[chat_b]["messages"][0]["claude_answer"] and docs[chat_a]["messages"][0]["claude_answer"] is None   # A: still running
    assert not docs[chat_a]["messages"][0].get("cancelled_at") and not docs[chat_b]["messages"][0].get("cancelled_at")
    ctl.release(Q_A)
    assert sa.joined(10)
    assert msg_of(f, chat_a)["claude_answer"] and msg_of(f, chat_a)["request_id"] != msg_of(f, chat_b)["request_id"]


def test_a_failure_in_one_chat_does_not_touch_another_running_chat(conc, monkeypatch):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    real = f["rt"].analyze_with_claude

    def boom_for_b(query, pool, **k):
        if _tk(query) == _tk(Q_B):
            raise RuntimeError("model down")
        return real(query, pool, **k)
    monkeypatch.setattr(f["rt"], "analyze_with_claude", boom_for_b)
    chat_b = chat_of(send(f, Q_B))
    sb = start_stream(f, chat_b, Q_B)
    assert sb.joined(10) and "error" in sb.kinds()                           # B failed ...
    assert not sa.finished() and sa.kinds() <= {"retry", "progress_percent"} # ... A did not notice
    ctl.release(Q_A)
    assert sa.joined(10) and "done" in sa.kinds() and "error" not in sa.kinds()


def test_several_new_chats_run_side_by_side_up_to_the_owner_cap(conc, monkeypatch):
    f, ctl = conc, conc["ctl"]
    monkeypatch.setattr(f["rt"].config, "MAX_CONCURRENT_SEARCHES_PER_OWNER", 3, raising=False)
    queries = [Q_A, Q_B, Q_C]
    streams = []
    for q in queries:
        ctl.hold(q, scan=True)
        cid = chat_of(send(f, q))                                            # three NEW chats, none blocked by the others
        streams.append((cid, q, start_stream(f, cid, q)))
        assert wait(lambda q=q: _tk(q) in ctl.scan_started)
    assert len(busy_keys(f)) == 3
    declined = send(f, Q_D)                                                  # the 4th is over the safety cap
    assert declined.status_code == 200 and b"3 searches running" in declined.body
    assert f["chats"].count_documents({}) == 3                               # nothing was created, queued or saved for it
    ctl.release(Q_A)
    assert streams[0][2].joined(10)
    assert chat_of(send(f, Q_D))                                             # a slot is free again
    for q in (Q_B, Q_C):
        ctl.release(q)
    assert all(s.joined(10) for _c, _q, s in streams)


def test_the_cap_is_per_owner_not_global(conc, monkeypatch):
    f, ctl = conc, conc["ctl"]
    monkeypatch.setattr(f["rt"].config, "MAX_CONCURRENT_SEARCHES_PER_OWNER", 1, raising=False)
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A, owner="owner-1"))
    start_stream(f, chat_a, Q_A, owner="owner-1")
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    assert send(f, Q_B, owner="owner-1").status_code == 200                  # owner-1 is at its cap
    other = send(f, Q_B, owner="owner-2")                                    # another owner is unaffected
    assert other.status_code == 303
    ctl.release(Q_A)


def test_a_second_submission_in_the_same_chat_is_still_declined(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    dup = send(f, Q_B, chat_id=chat_a)                                       # same chat, previous request still running
    assert dup.status_code == 200 and b"Please wait for your current request to finish first." in dup.body
    assert len(f["chats"].find_one({"chat_id": chat_a})["messages"]) == 1    # no duplicate turn, no job, no charge
    ctl.release(Q_A)
    assert sa.joined(10)
    assert chat_of(send(f, Q_B, chat_id=chat_a)) == chat_a                   # once it finished the chat accepts the next one


def test_an_explicit_blank_chat_id_is_a_new_chat_not_the_session_active_chat(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))
    start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    # another tab of the same browser: its session still points at chat A, but its form posted chat_id=""
    session = {"active_chat_id": chat_a}
    chat_b = chat_of(send(f, Q_B, chat_id="", session=session))
    assert chat_b != chat_a and session["active_chat_id"] == chat_b
    assert [m["query"] for m in f["chats"].find_one({"chat_id": chat_a})["messages"]] == [Q_A]   # A did not swallow it
    ctl.release(Q_A)


def test_a_request_that_omits_chat_id_still_uses_the_session_chat_as_before(conc):
    f = conc
    chat_a = chat_of(send(f, Q_A))
    session = {"active_chat_id": chat_a}
    resp = f["rt"].search(_request(f, session=session), query=Q_B, platform="All Platforms", chat_id=None)
    assert chat_of(resp) == chat_a
    assert [m["query"] for m in f["chats"].find_one({"chat_id": chat_a})["messages"]] == [Q_A, Q_B]


# ═══ 2. Stop: real backend cancellation ══════════════════════════════════════════════════════════════════════════════
def test_stop_cancels_a_running_scan_and_no_model_call_is_ever_made(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    rid = msg_of(f, chat_a)["request_id"]
    t0 = time.perf_counter()
    status, body = cancel(f, chat_a, Q_A, request_id=rid)
    assert status == 200 and body["status"] == "cancelled"
    assert sa.joined(5) and time.perf_counter() - t0 < 3                      # the stream stopped promptly
    assert "cancelled" in sa.kinds() and "done" not in sa.kinds() and "delta" not in sa.kinds()
    assert wait(lambda: _tk(Q_A) in ctl.scan_cancelled, 5)                    # the SCAN was really told to stop (resources freed)
    assert ctl.analyze_started == []                                          # no model call after the stop
    m = msg_of(f, chat_a)
    assert m["cancelled_at"] and m["claude_answer"] is None and m["results"] is None   # nothing saved as if it were finished
    assert busy_keys(f) == []                                                 # the chat can take a retry at once


def test_stopping_chat_a_does_not_stop_chat_b(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, scan=True)
    ctl.hold(Q_B, scan=True)
    chat_a, chat_b = chat_of(send(f, Q_A)), chat_of(send(f, Q_B))
    sa, sb = start_stream(f, chat_a, Q_A), start_stream(f, chat_b, Q_B)
    assert wait(lambda: {_tk(Q_A), _tk(Q_B)} <= set(ctl.scan_started))
    assert cancel(f, chat_a, Q_A)[1]["status"] == "cancelled"
    assert sa.joined(5) and "cancelled" in sa.kinds()
    assert not sb.finished() and _tk(Q_B) not in ctl.scan_cancelled          # B is untouched and still scanning
    assert busy_keys(f) == [f["idx"].busy_scope_key(OWNER, chat_b)]
    ctl.release(Q_B)
    assert sb.joined(10) and "done" in sb.kinds() and "cancelled" not in sb.kinds()
    assert msg_of(f, chat_b)["claude_answer"] and not msg_of(f, chat_b).get("cancelled_at")


def test_stop_before_the_stream_starts_a_queued_turn_never_runs(conc):
    f, ctl = conc, conc["ctl"]
    chat_a = chat_of(send(f, Q_A))                                            # accepted + saved, stream not opened yet
    assert cancel(f, chat_a, Q_A)[1]["status"] == "cancelled"
    s = start_stream(f, chat_a, Q_A)                                          # a late stream / reload / second tab
    assert s.joined(5) and s.kinds() == {"retry", "cancelled"} or s.kinds() == {"cancelled"}
    assert ctl.scan_started == [] and ctl.analyze_started == []              # nothing was scanned or generated
    assert msg_of(f, chat_a)["claude_answer"] is None
    # the turn-finisher queue and the chat page treat it as finished, not as work to resume
    tf = f["tf"]
    assert tf.unfinished_turns(f["chats"].find_one({"chat_id": chat_a})["messages"]) == {}
    assert f["idx"]._finish_turn_job(chat_a, OWNER, _tk(Q_A)) == ("done", None)
    assert ctl.scan_started == []


def test_stop_during_the_model_call_discards_the_answer_and_does_not_free_a_newer_retrys_busy_flag(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, analyze=True)                                               # scan returns, the model call blocks
    chat_a = chat_of(send(f, Q_A))
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.analyze_started)
    assert cancel(f, chat_a, Q_A)[1]["status"] == "cancelled"
    assert busy_keys(f) == []                                                 # freed at once: the model call cannot be aborted
    # the user retries immediately: the new request owns the busy flag
    f["idx"]._set_owner_busy(f["idx"].busy_scope_key(OWNER, chat_a))
    ctl.release(Q_A)                                                          # the (un-abortable) model call now returns
    assert sa.joined(10) and "cancelled" in sa.kinds() and "done" not in sa.kinds() and "delta" not in sa.kinds()
    assert msg_of(f, chat_a)["claude_answer"] is None                         # its answer was discarded, never saved
    assert busy_keys(f) == [f["idx"].busy_scope_key(OWNER, chat_a)]           # the old generator did not clear the retry's flag


def test_stop_after_the_browser_already_disconnected_still_stops_the_turn_for_good(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, analyze=True)
    chat_a = chat_of(send(f, Q_A))
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.analyze_started)
    # the stop request arrives from a different connection than the stream (beacon / retry after a network drop)
    assert cancel(f, chat_a, Q_A)[1]["status"] == "cancelled"
    assert cancel(f, chat_a, Q_A)[1]["status"] == "already_cancelled"          # the retried/beaconed duplicate
    ctl.release(Q_A)
    assert sa.joined(10) and "done" not in sa.kinds()
    assert msg_of(f, chat_a)["claude_answer"] is None


def test_stop_is_idempotent_and_safe_after_completion_or_for_foreign_chats(conc):
    f, ctl = conc, conc["ctl"]
    chat_a = chat_of(send(f, Q_A))
    rid = msg_of(f, chat_a)["request_id"]
    s = start_stream(f, chat_a, Q_A)
    assert s.joined(10) and "done" in s.kinds()
    answer = msg_of(f, chat_a)["claude_answer"]
    assert cancel(f, chat_a, Q_A, request_id=rid)[1]["status"] == "already_complete"   # after completion: no-op
    assert msg_of(f, chat_a)["claude_answer"] == answer and not msg_of(f, chat_a).get("cancelled_at")

    chat_b = chat_of(send(f, Q_B))
    assert cancel(f, chat_b, Q_B)[1]["status"] == "cancelled"
    assert cancel(f, chat_b, Q_B)[1]["status"] == "already_cancelled"                  # twice: same outcome
    assert cancel(f, chat_b, Q_B)[1]["status"] == "already_cancelled"
    assert cancel(f, "no-such-chat", Q_B)[0] == 404
    # another owner can neither stop nor even see this chat
    code, body = cancel(f, chat_a, Q_A, owner="somebody-else")
    assert code == 404 and body["status"] == "not_found"
    chat_c = chat_of(send(f, Q_C))
    assert cancel(f, chat_c, Q_C, owner="somebody-else")[0] == 404
    assert not msg_of(f, chat_c).get("cancelled_at")


def test_cancel_and_completion_race_has_exactly_one_winner_in_either_order(conc, monkeypatch):
    monkeypatch.setattr(conc["rt"].config, "MAX_CONCURRENT_SEARCHES_PER_OWNER", 1000, raising=False)
    f = conc
    idx = f["idx"]
    # completion first -> the stop reports "already_complete" and changes nothing
    c1 = chat_of(send(f, Q_A))
    idx.save_claude_answer_to_chat(c1, OWNER, _tk(Q_A), "answer")
    assert idx.mark_turn_cancelled(c1, OWNER, topic_key=_tk(Q_A)) == idx.CANCEL_ALREADY_COMPLETE
    assert not msg_of(f, c1).get("cancelled_at") and msg_of(f, c1)["claude_answer"] == "answer"
    # stop first -> the late save (plain, results, and the compare-and-set form) is refused
    c2 = chat_of(send(f, Q_B))
    assert idx.mark_turn_cancelled(c2, OWNER, topic_key=_tk(Q_B)) == idx.CANCEL_CANCELLED
    idx.save_claude_answer_to_chat(c2, OWNER, _tk(Q_B), "late answer")
    idx.save_signal_results_to_chat(c2, OWNER, _tk(Q_B), POSTS[:2])
    assert idx.save_claude_answer_if_unset(c2, OWNER, _tk(Q_B), "late answer") is False
    m = msg_of(f, c2)
    assert m["cancelled_at"] and m["claude_answer"] is None and m["results"] is None
    # a hammer of concurrent stop/save pairs never leaves both an answer and a stop on one message
    both = 0
    for i in range(30):
        cid = chat_of(send(f, f"{Q_C} {i}"))
        t = _tk(f"{Q_C} {i}")
        th = [threading.Thread(target=lambda: idx.save_claude_answer_to_chat(cid, OWNER, t, "a")),
              threading.Thread(target=lambda: idx.mark_turn_cancelled(cid, OWNER, topic_key=t))]
        [x.start() for x in th]
        [x.join() for x in th]
        mm = msg_of(f, cid)
        both += bool(mm.get("cancelled_at") and mm.get("claude_answer"))
    assert both == 0


def test_a_cancelled_turn_cannot_keep_consuming_resources(conc):
    """Stop frees: the scan thread, the single-flight registry entry, the stop registry entry and the busy flag."""
    import turn_control
    f, ctl = conc, conc["ctl"]
    lg = sys.modules["logics"]
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    assert turn_control.active_count() == 1
    cancel(f, chat_a, Q_A)
    assert sa.joined(5)
    assert wait(lambda: _tk(Q_A) in ctl.scan_cancelled, 5)
    assert turn_control.active_count() == 0                                   # the stop registry does not leak
    assert wait(lambda: (chat_a, _tk(Q_A)) not in lg._SCAN_REGISTRY, 5)       # the single-flight scan entry is gone
    assert busy_keys(f) == []
    assert len(ctl.scan_started) == 1                                         # and nothing restarted it


def test_retry_after_stop_is_a_new_request_on_the_same_chat_and_the_old_turn_stays_stopped(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    old_rid = msg_of(f, chat_a)["request_id"]
    cancel(f, chat_a, Q_A, request_id=old_rid)
    assert sa.joined(5)
    ctl.hold_scan.pop(_tk(Q_A))                                               # the retry's scan may finish
    retry = send(f, Q_A, chat_id=chat_a)                                      # same text, same chat, new submit key
    assert chat_of(retry) == chat_a                                           # accepted at once: not "Please wait"
    msgs = f["chats"].find_one({"chat_id": chat_a})["messages"]
    assert len(msgs) == 2 and msgs[0]["request_id"] == old_rid and msgs[1]["request_id"] != old_rid
    s2 = start_stream(f, chat_a, Q_A)
    assert s2.joined(10) and "done" in s2.kinds()
    msgs = f["chats"].find_one({"chat_id": chat_a})["messages"]
    assert msgs[0]["cancelled_at"] and msgs[0]["claude_answer"] is None       # the stopped turn was not revived or answered
    assert msgs[1]["claude_answer"] and not msgs[1].get("cancelled_at")       # the answer landed on the NEW turn
    assert len(ctl.scan_started) == 2                                         # a fresh scan, not a resumed one


def test_the_chat_page_renders_a_stopped_turn_as_stopped_and_never_re_streams_it(conc):
    from fastapi import BackgroundTasks
    f = conc
    chat_a = chat_of(send(f, Q_A))
    cancel(f, chat_a, Q_A)
    page = f["rt"].view_chat(_request(f, "GET", f"/chat/{chat_a}"), chat_a, BackgroundTasks()).body.decode()
    assert "Stopped" in page and "turn-stopped-note" in page and "status-stopped" in page
    import re
    assert not re.search(r'<div class="turn-block"[^>]*data-pending-stream="true"', page)   # a reload never restarts a stopped turn
    assert 'data-request-id="' + msg_of(f, chat_a)["request_id"] + '"' in page


# ═══ 3. credits: a stop keeps what was already consumed ═══════════════════════════════════════════════════════════════
from tests.test_search_credit_routes import app, flat_cost, credits_doc, used, KEY1, KEY2  # noqa: E402,F401
import search_credits as sc  # noqa: E402


def _cancel_credit(a, chat_id, topic_key, request_id=None):
    rt = a["rt"]
    req = Request({"type": "http", "method": "POST", "path": f"/chat/{chat_id}/cancel", "headers": [], "query_string": b"",
                   "session": {}, "app": a["idx"].app, "router": a["idx"].app.router})
    resp = rt.cancel_turn(req, chat_id, topic_key=topic_key, request_id=request_id)
    return resp.status_code, json.loads(resp.body)


@pytest.mark.usefixtures("flat_cost")
def test_stop_does_not_refund_the_credit_already_charged_and_a_retry_is_charged_normally(app):
    a = app
    first = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    chat_id = first.headers["location"].rsplit("/", 1)[-1]
    rid = sc.request_id("o1", KEY1)
    assert used(a) == 1 and credits_doc(a)["res"][rid]["state"] == sc.COMMITTED
    msg = a["db"].chats.find_one({"chat_id": chat_id})["messages"][0]
    code, body = _cancel_credit(a, chat_id, msg["topic_key"], msg["request_id"])
    assert code == 200 and body["status"] == "cancelled"
    assert used(a) == 1                                                       # NOT refunded
    assert credits_doc(a)["res"][rid]["state"] == sc.COMMITTED                # the entry still holds its charge
    assert sc.check_invariant(credits_doc(a)) == []
    # retry = a new intentional submit: its own key, its own reservation, charged normally
    again = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY2, chat_id=chat_id)
    assert again.status_code == 303 and used(a) == 2
    assert sc.check_invariant(credits_doc(a)) == []
    msgs = a["db"].chats.find_one({"chat_id": chat_id})["messages"]
    assert len(msgs) == 2 and msgs[0].get("cancelled_at") and not msgs[1].get("cancelled_at")


@pytest.mark.usefixtures("flat_cost")
def test_a_repeated_submit_with_the_same_key_after_a_stop_does_not_duplicate_or_recharge(app):
    a = app
    first = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    chat_id = first.headers["location"].rsplit("/", 1)[-1]
    msg = a["db"].chats.find_one({"chat_id": chat_id})["messages"][0]
    _cancel_credit(a, chat_id, msg["topic_key"], msg["request_id"])
    again = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)      # a double click / replay
    assert again.status_code == 303 and again.headers["location"] == first.headers["location"]
    assert used(a) == 1 and len(a["db"].chats.find_one({"chat_id": chat_id})["messages"]) == 1


@pytest.mark.usefixtures("flat_cost")
def test_the_stop_route_never_calls_the_credit_store(app, monkeypatch):
    a = app
    first = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    chat_id = first.headers["location"].rsplit("/", 1)[-1]
    msg = a["db"].chats.find_one({"chat_id": chat_id})["messages"][0]
    before = credits_doc(a)

    def forbidden(*args, **kwargs):
        raise AssertionError("the stop route must not touch the credit store")
    monkeypatch.setattr(a["rt"], "_credit_store", forbidden)
    monkeypatch.setattr(sc, "default_store", forbidden)
    assert _cancel_credit(a, chat_id, msg["topic_key"], msg["request_id"])[1]["status"] == "cancelled"
    assert credits_doc(a) == before                                           # not one field of the usage document changed


# ═══ 4. the real browser: Stop control, stopped state, retry, two chats in one tab ═══════════════════════════════════
@pytest.fixture
def ui(conc, live_server, integrity_browser, monkeypatch):
    """The real app on a real HTTP server (real SSE) + a real Chromium. The sidebar uses the real chat list."""
    f = conc
    monkeypatch.setattr(f["rt"], "get_user_chat_list", f["idx"].get_user_chat_list)
    pages = []

    def open_page(path="/", width=1280, height=860):
        ctx = integrity_browser.new_context(viewport={"width": width, "height": height})
        ctx.add_init_script("document.addEventListener('DOMContentLoaded', () => { const s = document.createElement('style'); "
                            "s.textContent = '#welcome-overlay{display:none!important}'; document.head.appendChild(s); });")   # guest welcome card
        page = ctx.new_page()
        page._errors, page._posts = [], []
        page.on("pageerror", lambda e: page._errors.append(str(e)))
        page.on("request", lambda r: page._posts.append((r.url[len(live_server):].split("?")[0], r.post_data or ""))
                if r.method == "POST" and r.url.startswith(live_server) else None)

        def handle(route):
            if not route.request.url.startswith(live_server):
                return route.fulfill(status=200, body="")                 # fonts / CDNs: nothing outside the app
            return route.continue_()
        page.route("**/*", handle)
        page.goto(live_server + path)
        page.evaluate("() => { const o = document.getElementById('welcome-overlay'); if (o) o.classList.remove('visible'); }")   # the guest welcome card
        pages.append(ctx)
        return page
    f["open"] = open_page
    f["base"] = live_server
    yield f
    for ctx in pages:
        ctx.close()


def bwait(page, cond, timeout=20.0):
    """Like wait(), but keeps Playwright's event loop pumping (its route handlers only run inside Playwright calls)."""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        page.wait_for_timeout(50)
    return False


def click_stop(page):
    """A deliberate Stop: clicks inside the first STOP_ARM_MS after the control appears are ignored (double-click guard)."""
    page.wait_for_function("document.getElementById('submit-btn').classList.contains('is-stop')")
    page.wait_for_timeout(700)
    page.locator("#submit-btn").click()


def submit_from_home(page, query):
    page.fill("#search-input", query)
    page.locator("#submit-btn").click()


def composer(page):
    return page.evaluate("""() => { const b = document.getElementById('submit-btn'), i = document.getElementById('search-input');
      return {stop: b.classList.contains('is-stop'), disabled: b.disabled, label: b.getAttribute('aria-label'),
              value: i.value, focused: document.activeElement === i}; }""")


def turns(page):
    return page.evaluate("""() => [...document.querySelectorAll('.turn-block')].map(t => ({
      q: (t.querySelector('.turn-question') || {}).textContent || '',
      chip: (t.querySelector('.turn-chip .turn-status-label') || {}).textContent || '',
      stopped: !!t.querySelector('.turn-stopped-note'), requestId: t.dataset.requestId || '',
      answered: !!t.querySelector('.answer-wrap')}))""")


def test_browser_stop_control_stopped_state_and_retry(ui):
    f, ctl = ui, ui["ctl"]
    ctl.hold(Q_A, scan=True)
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1")
    assert bwait(page, lambda: _tk(Q_A) in ctl.scan_started, 25)
    page.wait_for_function("document.getElementById('submit-btn').classList.contains('is-stop')")
    running = composer(page)
    assert running["stop"] and not running["disabled"] and running["label"] == "Stop searching"   # a clickable Stop control

    click_stop(page)                                                           # Stop
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    st = composer(page)
    assert st["value"] == Q_A and st["focused"] and not st["stop"] and not st["disabled"]   # editable original prompt, normal send control
    assert turns(page)[0]["stopped"] and "Search failed" not in page.evaluate("document.body.innerText")   # "Stopped", not a failure
    assert bwait(page, lambda: any(p[0].endswith("/cancel") for p in page._posts), 15)
    chat_id = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")
    assert bwait(page, lambda: msg_of(f, chat_id).get("cancelled_at") is not None, 15)              # the server really stopped it
    assert bwait(page, lambda: _tk(Q_A) in ctl.scan_cancelled, 5)

    # edit + send again: a NEW request on the same chat
    ctl.hold_scan.pop(_tk(Q_A))
    page.fill("#search-input", Q_A + " for startups")
    page.press("#search-input", "Enter")
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 2")
    page.wait_for_function("document.querySelectorAll('.answer-wrap').length === 1", timeout=15000)
    ts = turns(page)
    assert ts[0]["stopped"] and ts[0]["chip"] == "Stopped" and ts[1]["answered"] and ts[0]["requestId"] != ts[1]["requestId"]
    assert page._errors == []
    docs = f["chats"].find_one({"chat_id": chat_id})["messages"]
    assert docs[0]["cancelled_at"] and docs[0]["claude_answer"] is None and docs[1]["claude_answer"]


def test_browser_two_chats_in_one_tab_run_independently_and_stop_only_the_open_one(ui):
    f, ctl = ui, ui["ctl"]
    ctl.hold(Q_A, scan=True)
    ctl.hold(Q_B, scan=True)
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1")
    assert bwait(page, lambda: _tk(Q_A) in ctl.scan_started, 25)
    chat_a = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")

    page.locator("form[action='/chats/new'] button").first.click()             # the sidebar "New" while A runs
    page.wait_for_function("!document.querySelector('.turn-block') && document.getElementById('search-input')")
    submit_from_home(page, Q_B)
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1 && "
                           "document.querySelector('.turn-question').textContent.includes('invoicing')", timeout=15000)
    assert bwait(page, lambda: _tk(Q_B) in ctl.scan_started, 25)
    visible = page.evaluate("document.body.innerText")                        # what the user sees, not the page source
    assert "Search failed" not in visible and "Please wait for your current request" not in visible   # the reported bug
    chat_b = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")
    assert chat_b and chat_b != chat_a
    assert _tk(Q_A) not in ctl.scan_cancelled and len(busy_keys(f)) == 2    # A kept searching while B started

    click_stop(page)                                                           # Stop on chat B only
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    assert bwait(page, lambda: msg_of(f, chat_b).get("cancelled_at") is not None, 15)
    assert _tk(Q_B) in ctl.scan_cancelled or bwait(page, lambda: _tk(Q_B) in ctl.scan_cancelled, 5)
    assert _tk(Q_A) not in ctl.scan_cancelled and not msg_of(f, chat_a).get("cancelled_at")     # A is not stopped
    ctl.release(Q_A)
    assert bwait(page, lambda: msg_of(f, chat_a)["claude_answer"] is not None, 15)                      # A finishes on its own
    page.goto(f["base"] + "/chat/" + chat_a)                                   # reopen A: its answer, not a failure
    page.wait_for_function("document.querySelectorAll('.answer-wrap').length === 1", timeout=15000)
    assert turns(page)[0]["q"].strip() == Q_A and page._errors == []


def test_browser_stop_pressed_while_the_request_is_still_being_routed_is_carried_out_when_the_turn_exists(ui):
    f, ctl = ui, ui["ctl"]
    f["state"]["router_delay"] = 2.0                                           # POST /search (the router call) takes 2 s
    ctl.hold(Q_A, scan=True)
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    page.wait_for_function("document.getElementById('submit-btn').classList.contains('is-stop')")
    page.wait_for_timeout(750)                                                 # past the double-click guard: a deliberate stop
    page.evaluate("document.getElementById('submit-btn').click()")           # Stop before any turn exists (returns at once)
    assert composer(page)["disabled"] and composer(page)["label"] == "Stopping"   # shown as "stopping", cannot be pressed twice
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label') && "
                           "document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'", timeout=20000)
    chat_id = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")
    assert bwait(page, lambda: msg_of(f, chat_id).get("cancelled_at") is not None, 15)
    st = composer(page)
    assert st["value"] == Q_A and not st["stop"] and not st["disabled"]
    assert _tk(Q_A) not in ctl.scan_started or bwait(page, lambda: _tk(Q_A) in ctl.scan_cancelled, 5)   # no search was left running
    assert page._errors == []


def test_browser_stop_does_not_overwrite_a_prompt_the_user_already_started_typing(ui):
    f, ctl = ui, ui["ctl"]
    ctl.hold(Q_A, scan=True)
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    page.wait_for_function("document.getElementById('submit-btn').classList.contains('is-stop')")
    page.fill("#search-input", "my next question")                             # typing while the search runs is allowed
    click_stop(page)
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    st = composer(page)
    assert st["value"] == "my next question"                                  # never overwritten ...
    assert Q_A[:30] in page.evaluate("document.querySelector('.search-inline-error').textContent")   # ... the stopped prompt is offered


# ═══ 5. units: config, busy keys, stop registry, templates ═══════════════════════════════════════════════════════════
def _cfg_cap(env_value):
    import os, subprocess
    env_ = {k: v for k, v in os.environ.items() if k != "MAX_CONCURRENT_SEARCHES_PER_OWNER"}
    env_.update({"SESSION_SECRET_KEY": "x" * 48, "OPENAI_API_KEY": "k", "PYTHONDONTWRITEBYTECODE": "1"})
    if env_value is not None:
        env_["MAX_CONCURRENT_SEARCHES_PER_OWNER"] = env_value
    out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); import config; "
                          "print(config.MAX_CONCURRENT_SEARCHES_PER_OWNER)" % str(ROOT)],
                         capture_output=True, text=True, env=env_, cwd="/", timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    return int(out.stdout.split()[-1]), out.stderr


@pytest.mark.parametrize("value,expected", [(None, 3), ("", 3), ("1", 1), ("3", 3), ("10", 10)])
def test_the_owner_cap_setting_default_and_valid_values(value, expected):
    assert _cfg_cap(value)[0] == expected


@pytest.mark.parametrize("bad", ["0", "-2", "abc", "2.5", "1e3"])
def test_an_invalid_owner_cap_falls_back_to_3_with_a_warning(bad):
    value, stderr = _cfg_cap(bad)
    assert value == 3 and "MAX_CONCURRENT_SEARCHES_PER_OWNER" in stderr and "not a positive integer" in stderr


def test_busy_scope_key_and_owner_count(conc):
    f = conc
    idx = f["idx"]
    assert idx.busy_scope_key("o1", "c9") == "o1::c9" and idx.busy_scope_key("o1") == "o1" and idx.busy_scope_key("o1", None) == "o1"
    for key in ("o1::a", "o1::b", "o10::a", "o2::a"):                          # "o10" must not be counted as "o1"
        idx._set_owner_busy(key)
    assert idx.count_owner_busy("o1") == 2 and idx.count_owner_busy("o10") == 1 and idx.count_owner_busy("nobody") == 0
    # a stale flag (a crash / restart left it behind) is not a running search
    f["busy"].update_one({"holder": "o1::a"}, {"$set": {"started_at": __import__("datetime").datetime(2020, 1, 1, tzinfo=__import__("datetime").timezone.utc)}})
    assert idx.count_owner_busy("o1") == 1
    # the unique index on owner_key keeps working: one flag per chat, set twice = still one
    idx._set_owner_busy("o1::b")
    assert f["busy"].count_documents({"owner_key": "o1::b"}) == 1


def test_turn_control_registry_semantics():
    import turn_control as tc
    e1 = tc.register("o", "c", "r1")
    assert tc.register("o", "c", "r1") is e1 and not e1.is_set()
    assert tc.signal("o", "c", "r1") is True and e1.is_set()
    assert tc.signal("o", "c", "nope") is False and tc.signal("other-owner", "c", "r1") is False   # per owner + chat + turn
    e2 = tc.register("o", "c", "r2")
    tc.discard("o", "c", "r1", e1)
    assert tc.signal("o", "c", "r1") is False and tc.signal("o", "c", "r2") is True
    # an older run's cleanup must not drop a newer run's registration of the same turn
    e3 = tc.register("o", "c", "r3")
    e3b = threading_event = tc.register("o", "c", "r3")
    tc.discard("o", "c", "r3", object())
    assert tc.signal("o", "c", "r3") is True
    tc.discard("o", "c", "r2", e2)
    tc.discard("o", "c", "r3", e3)
    assert tc.active_count() == 0


def test_mark_turn_cancelled_edge_cases(conc):
    f = conc
    idx = f["idx"]
    chat_a = chat_of(send(f, Q_A))
    assert idx.mark_turn_cancelled(chat_a, OWNER, request_id="no-such-request") == idx.CANCEL_NOT_FOUND
    assert idx.mark_turn_cancelled(chat_a, OWNER) == idx.CANCEL_NOT_FOUND                     # nothing to identify the turn by
    assert idx.mark_turn_cancelled("no-chat", OWNER, topic_key=_tk(Q_A)) == idx.CANCEL_NOT_FOUND
    assert idx.mark_turn_cancelled(chat_a, "somebody-else", topic_key=_tk(Q_A)) == idx.CANCEL_NOT_FOUND
    idx.add_chat_message_to_chat(chat_a, OWNER, "hello", "hi!")                               # a plain chat turn is never "stopped"
    assert idx.mark_turn_cancelled(chat_a, OWNER, request_id=None, topic_key=None) == idx.CANCEL_NOT_FOUND
    assert idx.is_turn_cancelled(chat_a, OWNER, topic_key=_tk(Q_A)) is False
    assert idx.mark_turn_cancelled(chat_a, OWNER, topic_key=_tk(Q_A)) == idx.CANCEL_CANCELLED
    assert idx.is_turn_cancelled(chat_a, OWNER, topic_key=_tk(Q_A)) is True
    assert idx.is_turn_cancelled(chat_a, "somebody-else", topic_key=_tk(Q_A)) is False


@pytest.mark.parametrize("name", ["chat.html", "index.html"])
def test_both_templates_carry_the_stop_retry_and_background_stream_code(name):
    src = (ROOT / "templates" / name).read_text(encoding="utf-8")
    for needle in ("function stopTurn(", "function applyStoppedState(", "function restorePromptIntoComposer(", "async function sendCancel(",
                   "function detachActiveStream()", "function releaseSource(", "if (payload.cancelled) {", "let currentRunning = null;",
                   "const MAX_BACKGROUND_STREAMS = 3;", "const STOP_ARM_MS = 600;", "if (Date.now() < stopArmedAt) return;", "'/chat/' + encodeURIComponent(stream.chatId) + '/cancel'",
                   ".submit-btn.is-stop", ".turn-stopped-note", "'status-stopped'", "await stopPromise"):
        assert needle in src, (name, needle)
    assert "activeEventSource.close();" not in src.split("async function navigateTo(")[1].split("try {")[0]   # a chat switch no longer ends that chat's stream


def test_the_stop_retry_script_blocks_are_identical_in_both_templates():
    def block(name):
        src = (ROOT / "templates" / name).read_text(encoding="utf-8")
        a = src.index("// ---- (STOP) Stop / stopped / retry")
        b = src.index("document.addEventListener('click', (e) => {\n    const btn = e.target.closest && e.target.closest('#submit-btn.is-stop');")
        return src[a:b]
    assert block("chat.html") == block("index.html")


def test_chat_html_renders_the_stopped_markup_and_the_request_id():
    src = (ROOT / "templates" / "chat.html").read_text(encoding="utf-8")
    assert 'data-request-id="{{ msg.request_id or \'\' }}"' in src
    assert "{% if msg.cancelled_at and msg.claude_answer is none %}" in src and "{% elif msg.cancelled_at %}" in src


def test_browser_switching_back_to_a_running_chat_shows_it_running_and_it_completes_in_place(ui):
    f, ctl = ui, ui["ctl"]
    ctl.hold(Q_A, scan=True)
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1")
    assert bwait(page, lambda: _tk(Q_A) in ctl.scan_started, 25)
    chat_a = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")
    page.locator("form[action='/chats/new'] button").first.click()             # leave chat A (it keeps running)
    page.wait_for_function("!document.querySelector('.turn-block')")
    assert not composer(page)["stop"]                                           # chat A's search does not lock this page's composer
    page.locator(f"a.session-item-link[href='/chat/{chat_a}']").first.click()  # and come back to it while it still runs
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1")
    page.wait_for_function("document.getElementById('submit-btn').classList.contains('is-stop')")
    assert turns(page)[0]["chip"] == "Researching"                              # the correct status, not "failed"
    assert not msg_of(f, chat_a).get("cancelled_at") and _tk(Q_A) not in ctl.scan_cancelled
    ctl.release(Q_A)                                                            # it finishes ...
    page.wait_for_function("document.querySelectorAll('.answer-wrap').length === 1", timeout=20000)   # ... in place, no reload
    assert not composer(page)["stop"] and page._errors == []
    assert len(ctl.scan_started) == 1                                           # one scan: coming back did not start a second one


def test_browser_a_double_click_on_send_does_not_stop_the_search_it_just_started(ui):
    f, ctl = ui, ui["ctl"]
    ctl.hold(Q_A, scan=True)
    page = f["open"]("/")
    page.fill("#search-input", Q_A)
    page.evaluate("() => { const b = document.getElementById('submit-btn'); b.click(); b.click(); b.click(); }")   # an impatient double/triple click
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1")
    assert bwait(page, lambda: _tk(Q_A) in ctl.scan_started, 25)
    page.wait_for_timeout(1500)
    chat_id = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")
    assert turns(page)[0]["chip"] == "Researching" and composer(page)["stop"]    # still running, not stopped
    assert not msg_of(f, chat_id).get("cancelled_at") and _tk(Q_A) not in ctl.scan_cancelled
    assert not any(p[0].endswith("/cancel") for p in page._posts)
    assert f["chats"].find_one({"chat_id": chat_id})["messages"].__len__() == 1   # and no duplicate request was created


# ═══ 8. the owner cap is ATOMIC (simultaneous submissions) ═══════════════════════════════════════════════════════════
def _race(n, fn):
    """Runs fn(i) on n threads released at the same instant; returns the results in thread order."""
    barrier, out = threading.Barrier(n), [None] * n

    def run(i):
        barrier.wait(10)
        try:
            out[i] = fn(i)
        except BaseException as exc:                                                  # pragma: no cover
            out[i] = exc
    ts = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(n)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    return out


def test_simultaneous_slot_reservations_never_exceed_the_cap(conc):
    idx = conc["idx"]
    for cap in (1, 3, 5):
        conc["busy"].delete_many({})
        got = _race(24, lambda i: idx.reserve_search_slot("race-owner", f"race-owner::chat{i}", cap))
        assert got.count(True) == cap and got.count(False) == 24 - cap, got
        assert len(slot_holders(conc, "race-owner")) == cap and idx.count_owner_busy("race-owner") == cap


def test_simultaneous_searches_at_the_limit_admit_exactly_the_cap(conc, monkeypatch):
    f = conc
    monkeypatch.setattr(f["rt"].config, "MAX_CONCURRENT_SEARCHES_PER_OWNER", 3, raising=False)
    queries = [f"find people complaining about invoice tool number {i} please" for i in range(8)]
    resp = _race(8, lambda i: send(f, queries[i]))
    accepted = [r for r in resp if r.status_code == 303]
    declined = [r for r in resp if r.status_code == 200]
    assert len(accepted) == 3 and len(declined) == 5, [r.status_code for r in resp]
    assert all(b"searches running" in r.body for r in declined)
    assert f["chats"].count_documents({}) == 3                                       # nothing saved for a declined one
    assert len(slot_holders(f)) == 3 and f["idx"].count_owner_busy(OWNER) == 3


def test_simultaneous_acquisitions_of_one_chats_busy_flag_admit_exactly_one(conc):
    idx = conc["idx"]
    got = _race(16, lambda i: idx.acquire_owner_busy(f"{OWNER}::same-chat"))
    assert got.count(True) == 1 and got.count(False) == 15, got
    assert busy_keys(conc) == [f"{OWNER}::same-chat"]
    idx._clear_owner_busy(f"{OWNER}::same-chat")
    assert idx.acquire_owner_busy(f"{OWNER}::same-chat") is True              # free again after the clear


def test_a_handed_over_slot_whose_stream_never_starts_expires_quickly(conc, monkeypatch):
    f = conc
    monkeypatch.setattr(f["rt"].config, "MAX_CONCURRENT_SEARCHES_PER_OWNER", 1, raising=False)
    chat_a = chat_of(send(f, Q_A))                                            # accepted; the browser never opens the stream
    assert send(f, Q_B).status_code == 200                                    # its slot is held ...
    f["busy"].update_one({"holder": f["idx"].busy_scope_key(OWNER, chat_a)},
                         {"$set": {"started_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                                   - __import__("datetime").timedelta(seconds=f["idx"].SLOT_HANDOVER_SECONDS + 1)}})
    assert send(f, Q_B).status_code == 303                                    # ... only for SLOT_HANDOVER_SECONDS


def test_the_slot_follows_the_accepted_search_into_its_stream_and_is_freed_at_the_end(conc):
    f, ctl = conc, conc["ctl"]
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))
    holder = f["idx"].busy_scope_key(OWNER, chat_a)
    assert slot_holders(f) == [holder] and busy_keys(f) == []                        # handed over: slot kept, flag cleared
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    assert slot_holders(f) == [holder] and busy_keys(f) == [holder]                  # one slot, not two
    ctl.release(Q_A)
    assert sa.joined(10)
    assert slot_holders(f) == [] and busy_keys(f) == []


def test_stop_frees_the_slot_and_a_stale_slot_is_reclaimed(conc, monkeypatch):
    f, ctl = conc, conc["ctl"]
    monkeypatch.setattr(f["rt"].config, "MAX_CONCURRENT_SEARCHES_PER_OWNER", 1, raising=False)
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))
    start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    assert send(f, Q_B).status_code == 200                                           # at the cap
    assert cancel(f, chat_a, Q_A)[1]["status"] == "cancelled"
    assert slot_holders(f) == []                                                     # Stop frees the slot at once
    chat_b = chat_of(send(f, Q_B))                                                   # ... so the next search is admitted
    assert chat_b
    # a crashed run's leftover slot is reclaimed instead of blocking the owner forever
    f["busy"].update_one({"holder": f["idx"].busy_scope_key(OWNER, chat_b)},
                         {"$set": {"started_at": __import__("datetime").datetime(2020, 1, 1, tzinfo=__import__("datetime").timezone.utc)}})
    assert f["idx"].reserve_search_slot(OWNER, OWNER + "::brand-new") is True
    assert slot_holders(f) == [OWNER + "::brand-new"]
    ctl.release(Q_A)


def test_database_py_declares_the_unique_index_the_atomic_cap_relies_on():
    src = open(os.path.join(os.path.dirname(__file__), "..", "database.py"), encoding="utf-8").read()
    assert 'busy_owners_collection.create_index("owner_key", unique=True)' in src


# ═══ 9. background work started by a request does not start (or save) after Stop ════════════════════════════════════
def test_background_google_fallback_progress_and_worker_answer_do_nothing_for_a_stopped_turn(conc, monkeypatch):
    f = conc
    idx = f["idx"]
    calls = {"google": 0, "progress": 0, "analyze": 0}
    monkeypatch.setattr(idx.google_search, "search_google_for_reddit_posts",
                        lambda *a, **k: calls.__setitem__("google", calls["google"] + 1), raising=False)
    monkeypatch.setattr(idx.flintel, "generate_search_progress_content",
                        lambda *a, **k: calls.__setitem__("progress", calls["progress"] + 1) or {"intro": "x", "outro": "y", "checklist": []})
    monkeypatch.setattr(idx, "analyze_with_claude",
                        lambda *a, **k: calls.__setitem__("analyze", calls["analyze"] + 1) or "answer")
    chat_a = chat_of(send(f, Q_A))
    chat_b = chat_of(send(f, Q_B))                                                  # a live turn: the control group
    assert cancel(f, chat_a, Q_A)[1]["status"] == "cancelled"
    ma, mb = msg_of(f, chat_a), msg_of(f, chat_b)
    idx._trigger_google_fallback_search(chat_a, OWNER, ma)
    idx._generate_search_progress(chat_a, OWNER, ma)
    idx._complete_message_answer_and_results(chat_a, OWNER, ma, POSTS[:2])
    assert calls == {"google": 0, "progress": 0, "analyze": 0}                      # nothing started for the stopped turn
    assert msg_of(f, chat_a).get("search_progress") is None and msg_of(f, chat_a)["claude_answer"] is None
    idx._trigger_google_fallback_search(chat_b, OWNER, mb)                          # ... while the other chat still works
    idx._generate_search_progress(chat_b, OWNER, mb)
    assert calls["google"] == 1 and calls["progress"] == 1
    assert msg_of(f, chat_b).get("search_progress")


def test_a_progress_copy_that_finishes_after_stop_is_not_saved(conc, monkeypatch):
    f = conc
    idx = f["idx"]
    chat_a = chat_of(send(f, Q_A))
    ma = msg_of(f, chat_a)

    def slow_progress(*a, **k):
        cancel(f, chat_a, Q_A)                                                      # the user stops while it is generating
        return {"intro": "x", "outro": "y", "checklist": []}
    monkeypatch.setattr(idx.flintel, "generate_search_progress_content", slow_progress)
    idx._generate_search_progress(chat_a, OWNER, ma)
    assert msg_of(f, chat_a).get("search_progress") is None and msg_of(f, chat_a)["cancelled_at"]


@pytest.mark.usefixtures("flat_cost")
def test_a_search_declined_by_the_owner_cap_is_never_reserved_charged_or_enqueued(app, monkeypatch):
    a = app
    idx = a["idx"]
    monkeypatch.setattr(idx, "busy_owners_collection", a["db"].busy)           # a real (mongomock) collection, not the stub
    idx.busy_owners_collection.create_index("owner_key", unique=True)
    monkeypatch.setattr(a["rt"].config, "MAX_CONCURRENT_SEARCHES_PER_OWNER", 1, raising=False)
    monkeypatch.setattr(a["rt"], "reserve_search_slot", idx.reserve_search_slot)
    monkeypatch.setattr(a["rt"], "acquire_owner_busy", idx.acquire_owner_busy)
    monkeypatch.setattr(a["rt"], "count_owner_busy", idx.count_owner_busy)
    assert idx.reserve_search_slot("o1", "o1::another-chat") is True            # the owner's one slot is taken
    declined = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert declined.status_code == 200 and b"searches running" in declined.body
    assert used(a) == 0 and (credits_doc(a) or {}).get("res", {}) == {}         # no reservation, no charge
    assert a["state"]["enqueues"] == [] and a["db"].chats.count_documents({}) == 0
    idx.busy_owners_collection.delete_many({})
    ok = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)   # the SAME key, once a slot is free
    assert ok.status_code == 303 and used(a) == 1                                     # charged exactly once


@pytest.mark.usefixtures("flat_cost")
def test_credit_recovery_after_a_stop_never_refunds_the_stopped_search(app):
    a = app
    first = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    chat_id = first.headers["location"].rsplit("/", 1)[-1]
    rid = sc.request_id("o1", KEY1)
    msg = a["db"].chats.find_one({"chat_id": chat_id})["messages"][0]
    assert _cancel_credit(a, chat_id, msg["topic_key"], msg["request_id"])[1]["status"] == "cancelled"
    store = a["store"]()
    for _ in range(3):
        store.recover("o1")                                                        # what the periodic recovery does
        store.recover_entry("o1", rid)
    assert used(a) == 1 and credits_doc(a)["res"][rid]["state"] == sc.COMMITTED
    assert sc.check_invariant(credits_doc(a)) == []


def test_browser_reload_and_navigate_away_while_running_then_stop_and_reload_again(ui):
    f, ctl = ui, ui["ctl"]
    ctl.hold(Q_A, scan=True)
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1")
    assert bwait(page, lambda: _tk(Q_A) in ctl.scan_started, 25)
    chat_a = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")

    # navigate away (a hard load of the home page) and come back: the chat is still running, not failed
    page.goto(f["base"] + "/")
    page.goto(f["base"] + f"/chat/{chat_a}")
    page.wait_for_function("document.getElementById('submit-btn').classList.contains('is-stop')", timeout=15000)
    assert "Search failed" not in page.evaluate("document.body.innerText")
    assert msg_of(f, chat_a).get("cancelled_at") is None and msg_of(f, chat_a)["claude_answer"] is None

    # reload mid-run: still running
    page.reload()
    page.wait_for_function("document.getElementById('submit-btn').classList.contains('is-stop')", timeout=15000)
    assert "Search failed" not in page.evaluate("document.body.innerText")

    click_stop(page)                                                           # Stop after the reloads
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    assert bwait(page, lambda: msg_of(f, chat_a).get("cancelled_at") is not None, 15)
    started_before = len(ctl.scan_started)

    page.reload()                                                              # reload after Stop: stays stopped, not restarted
    page.wait_for_function("document.querySelector('.turn-stopped-note') !== null", timeout=15000)
    st = composer(page)
    assert not st["stop"] and "Search failed" not in page.evaluate("document.body.innerText")   # (empty composer: send is disabled)
    page.fill("#search-input", "a new question")
    assert not composer(page)["disabled"]                                      # ... and enabled again as soon as text is typed
    page.fill("#search-input", "")
    page.wait_for_timeout(1500)
    assert len(ctl.scan_started) == started_before and msg_of(f, chat_a)["claude_answer"] is None
    assert page._errors == []


def test_browser_a_search_in_another_chat_survives_stopping_this_one_after_switching_back(ui):
    f, ctl = ui, ui["ctl"]
    ctl.hold(Q_A, scan=True)
    ctl.hold(Q_B, scan=True)
    page = f["open"]("/")
    submit_from_home(page, Q_A)
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1")
    assert bwait(page, lambda: _tk(Q_A) in ctl.scan_started, 25)
    chat_a = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")

    page.goto(f["base"] + "/")                                                 # a NEW chat while A runs on the server
    submit_from_home(page, Q_B)
    page.wait_for_function("document.querySelectorAll('.turn-block').length === 1")
    assert bwait(page, lambda: _tk(Q_B) in ctl.scan_started, 25)
    chat_b = page.evaluate("document.querySelector('#search-form input[name=chat_id]').value")
    assert chat_b != chat_a and "Please wait" not in page.evaluate("document.body.innerText")

    click_stop(page)                                                           # stop B (the open chat)
    page.wait_for_function("document.querySelector('.turn-chip .turn-status-label').textContent.trim() === 'Stopped'")
    assert bwait(page, lambda: msg_of(f, chat_b).get("cancelled_at") is not None, 15)
    assert msg_of(f, chat_a).get("cancelled_at") is None and _tk(Q_A) not in ctl.scan_cancelled   # A untouched

    page.goto(f["base"] + f"/chat/{chat_a}")                                   # back to A: still running, then it completes
    page.wait_for_function("document.getElementById('submit-btn').classList.contains('is-stop')", timeout=15000)
    ctl.release(Q_A)
    page.wait_for_function("document.querySelectorAll('.answer-wrap').length === 1", timeout=20000)
    assert msg_of(f, chat_a)["claude_answer"] and msg_of(f, chat_b)["claude_answer"] is None
    assert page._errors == []


# ═══ 10. FAILURE POLICY: MongoDB failing must not mean unbounded searches (nor a total lockout) ══════════════════════════
class Flaky:
    """A busy_owners collection that raises on every operation while .down is True (an unreachable MongoDB)."""
    def __init__(self, inner):
        self._inner, self.down = inner, False

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            if self.down:
                raise RuntimeError("MongoDB unreachable")
            return attr(*a, **k)
        return call


@pytest.fixture
def flaky(conc, monkeypatch):
    f = conc
    fl = Flaky(f["busy"])
    monkeypatch.setattr(f["idx"], "busy_owners_collection", fl)
    f["idx"]._local_reset_for_tests()
    yield fl
    f["idx"]._local_reset_for_tests()


def test_store_down_the_owner_cap_still_holds_in_process_and_recovery_returns_to_the_store(conc, flaky):
    f, idx = conc, conc["idx"]
    flaky.down = True
    got = _race(24, lambda i: idx.reserve_search_slot("flaky-owner", f"flaky-owner::c{i}", 3))
    assert got.count(True) == 3 and got.count(False) == 21, got               # bounded: NOT 24 (fail open), NOT 0 (lockout)
    assert idx.store_is_degraded() and idx.count_owner_busy("flaky-owner") == 3
    flaky.down = False                                                         # MongoDB comes back
    assert idx.reserve_search_slot("flaky-owner", "flaky-owner::new", 3) is False   # still 3 admitted locally: no overshoot
    for i in range(24):
        idx._release_search_slot(f"flaky-owner::c{i}")                         # the searches end
    assert idx.reserve_search_slot("flaky-owner", "flaky-owner::new", 3) is True
    assert [d["holder"] for d in f["busy"].find({"holder": "flaky-owner::new"})] == ["flaky-owner::new"]   # back on the store


def test_store_down_the_process_wide_ceiling_bounds_many_owners(conc, flaky, monkeypatch):
    f, idx = conc, conc["idx"]
    monkeypatch.setattr(f["rt"].config, "DEGRADED_MAX_CONCURRENT_SEARCHES", 5, raising=False)
    flaky.down = True
    got = _race(30, lambda i: idx.reserve_search_slot(f"owner-{i}", f"owner-{i}::c", 3))
    assert got.count(True) == 5 and got.count(False) == 25, got               # a total bound across owners too


def test_store_down_simultaneous_submissions_admit_exactly_the_cap_and_decline_the_rest(conc, flaky, monkeypatch):
    f = conc
    monkeypatch.setattr(f["rt"].config, "MAX_CONCURRENT_SEARCHES_PER_OWNER", 3, raising=False)
    flaky.down = True
    queries = [f"find people complaining about invoice tool number {i} please" for i in range(8)]
    resp = _race(8, lambda i: send(f, queries[i]))
    accepted = [r for r in resp if getattr(r, "status_code", None) == 303]
    declined = [r for r in resp if getattr(r, "status_code", None) == 200]
    assert len(accepted) == 3 and len(declined) == 5, [getattr(r, "status_code", r) for r in resp]
    assert f["chats"].count_documents({}) == 3
    # the declined ones are told to retry; nothing leaks: ending the searches frees the in-memory slots
    assert all(b"searches running" in r.body or b"try again" in r.body for r in declined)
    flaky.down = False


def test_store_down_a_second_submission_in_the_same_chat_is_still_declined_in_process(conc, flaky):
    idx = conc["idx"]
    flaky.down = True
    got = _race(16, lambda i: idx.acquire_owner_busy("flaky-owner::same-chat"))
    assert got.count(True) == 1 and got.count(False) == 15, got
    assert idx._is_owner_busy("flaky-owner::same-chat") is True               # the in-memory flag is visible to the check
    idx._clear_owner_busy("flaky-owner::same-chat")
    assert idx._is_owner_busy("flaky-owner::same-chat") is False and idx.acquire_owner_busy("flaky-owner::same-chat") is True


def test_store_failing_in_the_middle_of_a_search_does_not_leak_or_strand_the_chat(conc, flaky):
    f, ctl, idx = conc, conc["ctl"], conc["idx"]
    ctl.hold(Q_A, scan=True)
    chat_a = chat_of(send(f, Q_A))                                            # accepted while the store is healthy
    sa = start_stream(f, chat_a, Q_A)
    assert wait(lambda: _tk(Q_A) in ctl.scan_started)
    flaky.down = True                                                          # the store dies mid-search
    ctl.release(Q_A)
    assert sa.joined(10) and "done" in sa.kinds()                              # the search itself completes
    flaky.down = False
    assert msg_of(f, chat_a)["claude_answer"]
    idx._local_reset_for_tests()
    chat_b = chat_of(send(f, Q_B))                                            # and the owner is not locked out afterwards
    assert chat_b


def test_degraded_mode_expires_so_a_recovered_store_is_used_again(conc, flaky):
    idx = conc["idx"]
    flaky.down = True
    assert idx.reserve_search_slot("flaky-owner", "flaky-owner::a", 3) is True and idx.store_is_degraded()
    flaky.down = False
    idx._degraded["until"] = 0.0                                               # the 30 s window passed
    assert not idx.store_is_degraded()
    assert idx.reserve_search_slot("flaky-owner", "flaky-owner::b", 3) is True
    assert f_holders(conc) == ["flaky-owner::b"]                               # recorded in MongoDB again


def f_holders(f):
    return sorted(d["holder"] for d in f["busy"].find({}) if "holder" in d)


# ═══ 11. failure AFTER the credit reservation, and retries ═══════════════════════════════════════════════════════════════
@pytest.mark.usefixtures("flat_cost")
def test_a_collector_failure_after_the_reservation_keeps_the_charge_frees_the_slot_and_a_same_key_retry_never_double_charges(app, monkeypatch):
    a, idx = app, app["idx"]
    monkeypatch.setattr(idx, "busy_owners_collection", a["db"].busy)
    a["db"].busy.create_index("owner_key", unique=True)
    for name in ("reserve_search_slot", "acquire_owner_busy", "count_owner_busy", "store_is_degraded", "_is_owner_busy",
                 "_set_owner_busy", "_clear_owner_busy"):                      # the REAL busy functions on a real collection
        monkeypatch.setattr(a["rt"], name, getattr(idx, name))
    good = a["rt"].enqueue_search_job

    def boom(*args, **kwargs):
        raise RuntimeError("collector down")
    monkeypatch.setattr(a["rt"], "enqueue_search_job", boom)
    first = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    rid = sc.request_id("o1", KEY1)
    assert first.status_code == 503                                            # distinct "could not start" answer
    assert used(a) == 1 and credits_doc(a)["res"][rid]["state"] == sc.SENDING   # existing policy: the charge is held, not refunded
    assert a["db"].busy.count_documents({"owner_key": {"$not": {"$regex": "#slot"}}}) == 0    # no busy flag is stranded
    assert a["db"].busy.count_documents({}) <= 1       # at most the 60 s hand-over slot of the (saved, pending) message
    monkeypatch.setattr(a["rt"], "enqueue_search_job", good)                   # the collector recovers; the user retries
    again = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    assert used(a) == 1 and sc.check_invariant(credits_doc(a)) == []          # the same key is never charged twice
    assert again.status_code in (200, 303)
    assert len(a["db"].chats.find_one({})["messages"]) <= 1                    # and never duplicated into the chat


@pytest.mark.usefixtures("flat_cost")
def test_stop_does_not_claim_to_cancel_the_shared_collector_job_and_leaves_it_untouched(app):
    """The collector job (flintel_search_jobs) is ONE document per topic_key, shared by every chat/user searching that topic and
    consumed by an external service that is not in this repository. Stop therefore does not touch it: it keeps running."""
    a = app
    first = a["send"]("Find people looking for a CRM for small teams", submit_key=KEY1)
    chat_id = first.headers["location"].rsplit("/", 1)[-1]
    msg = a["db"].chats.find_one({"chat_id": chat_id})["messages"][0]
    job_before = a["db"].jobs.find_one({"topic_key": msg["topic_key"]})
    assert job_before and job_before["status"] == "pending"
    assert _cancel_credit(a, chat_id, msg["topic_key"], msg["request_id"])[1]["status"] == "cancelled"
    assert a["db"].jobs.find_one({"topic_key": msg["topic_key"]}) == job_before          # not cancelled, not edited, not deleted
