"""
tests/test_guest_gate_stream.py — the guest gate at the two answer-producing STREAMING entry points
(routes.stream_answer: the normal analysis and the website-only analysis), driven against the REAL gate on mongomock.

Reuses the offline stream harness of tests/test_scan_resilience.py (stubbed `index`, fake scan, no network).
For every exit of the generator the test checks the gate document AND what was saved / streamed to the browser:

  success                      counted before anything is shown; answer saved; `done` carries the sign-up flag
  analysis error               released, nothing counted, nothing saved
  scan deadline / timeout      answer saved (owner only), never counted, slot released
  superseded (before / during) no model call (before) or answer discarded (during); only the notice is saved / shown
  background retry overlap     a retry cannot run while another search is admitted; it can when the slot is free
  save failure after counting  the count stays (the user saw the answer) and a refresh re-delivers without recounting
  client disconnect            nothing counted

Mongo here is mongomock; live MongoDB is not covered.
"""
import datetime as dt
import json
import types
from unittest.mock import MagicMock

import pytest

from tests.test_scan_resilience import routes_env, _set_cfg  # noqa: F401  (fixtures + helper)
from tests.test_strict_intent import lg  # noqa: F401

mongomock = pytest.importorskip("mongomock")
import guest_gate as gg  # noqa: E402

GOOD_ANSWER = json.dumps({"format": "source_list", "headline": "Unique headline", "executive_summary": "s"})
OLD = dt.datetime(2020, 1, 1)


@pytest.fixture
def spy(routes_env, monkeypatch):
    env = routes_env
    env.scan_seconds = 0.05
    _set_cfg(monkeypatch, NONBLOCKING_STREAM_ENABLED=True, SINGLE_FLIGHT_SCAN_ENABLED=True, STRICT_INTENT_MODE=False)
    rt = env.routes
    env.coll = mongomock.MongoClient()["stream_gate"]["g"]
    env.gate = gg.GuestGate(env.coll, limit=1)
    monkeypatch.setattr(gg, "default_gate", lambda: env.gate)
    env.saved, env.analyze_calls = [], []
    monkeypatch.setattr(rt, "save_claude_answer_to_chat", lambda chat, owner, tk, ans: env.saved.append(ans))

    def cas(chat, owner, tk, ans, gate_key, token):
        """Stands in for index.save_answer_for_gate: compare-and-set on the message (first writer wins)."""
        if env.msg.get("claude_answer") is not None:
            return False
        env.saved.append(ans)
        env.msg["claude_answer"] = ans
        return True
    monkeypatch.setattr(rt, "save_answer_for_gate", cas)
    env.cas = cas
    monkeypatch.setattr(rt, "save_signal_results_to_chat", lambda *a, **k: None)
    monkeypatch.setattr(rt, "append_to_chat_summary", lambda *a, **k: None)
    monkeypatch.setattr(rt, "_finalize_answer_and_results", lambda ans, pool, seed="": (ans, pool))
    monkeypatch.setattr(rt, "enrich_no_results_answer", lambda ans, *a, **k: ans, raising=False)
    monkeypatch.setattr(rt, "_patch_post_urls_into_answer", lambda ans, *a, **k: ans, raising=False)

    def analyze(*a, **k):
        env.analyze_calls.append(1)
        return GOOD_ANSWER
    monkeypatch.setattr(rt, "analyze_with_claude", analyze)
    env.msg["request_id"] = "tok-a"
    env.reserve = lambda token="tok-a", chat="chat-g": env.gate.reserve("owner1", token, "tk", chat)
    return env


def run_stream(env, chat="chat-g"):
    gen = iter(env.routes.stream_answer(MagicMock(), chat, "tk").body_iterator)
    out = []
    try:
        for chunk in gen:
            out.append(chunk)
            if '"done": true' in chunk or '"error"' in chunk:
                break
    finally:
        gen.close()
    return out


def shown(out):
    """All answer text the browser received."""
    text = ""
    for c in out:
        if c.startswith("data: "):
            try:
                text += json.loads(c[6:]).get("delta", "")
            except ValueError:
                pass
    return text


def doc(env):
    return env.coll.find_one({"_id": "owner1"}) or {}


def age(env):
    env.coll.update_one({"_id": "owner1"}, {"$set": {"inflight.started_at": OLD}})


def take_over(env, new="tok-b"):
    age(env)
    assert env.gate.reserve("owner1", new, "tk2", "chat-2").reserved


def test_stream_success_is_counted_before_it_is_shown_and_saved(spy):
    assert spy.reserve().reserved
    out = run_stream(spy)
    assert GOOD_ANSWER in shown(out) and spy.saved == [GOOD_ANSWER]
    d = doc(spy)
    assert d["completed"] == 1 and d["inflight"] is None and d["last_chat_id"] == "chat-g" and d["completed_tokens"] == ["tok-a"]
    done = json.loads([c for c in out if '"done": true' in c][0][6:])
    assert done["guest_gate"] == "auth_required"                                       # the page opens the sign-up modal after


def test_stream_analysis_error_releases_and_counts_nothing(spy, monkeypatch):
    spy.reserve()

    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(spy.routes, "analyze_with_claude", boom)
    out = run_stream(spy)
    assert any('"error"' in c for c in out) and spy.saved == []
    d = doc(spy)
    assert d["completed"] == 0 and d["inflight"] is None


def test_stream_empty_answer_releases_and_counts_nothing(spy, monkeypatch):
    spy.reserve()
    monkeypatch.setattr(spy.routes, "analyze_with_claude", lambda *a, **k: "")
    run_stream(spy)
    d = doc(spy)
    assert spy.saved == [] and d["completed"] == 0 and d["inflight"] is None


def test_stream_scan_deadline_saves_the_owners_answer_but_never_counts_it(spy, monkeypatch):
    spy.reserve()
    spy.scan_seconds = 5                                                               # the scan is slower than the deadline
    monkeypatch.setattr(spy.routes, "RESPONSE_TIMEOUT", 0.3)
    out = run_stream(spy)
    assert len(spy.saved) == 1 and json.loads(spy.saved[0])["answer_status"] == "limited"   # labelled as limited results
    assert json.loads(spy.saved[0])["headline"] == "Unique headline" and '"limited"' in shown(out)
    d = doc(spy)
    assert d["completed"] == 0 and d["inflight"] is None                                # delivered, uncounted, slot freed


def test_stream_scan_deadline_of_a_superseded_search_shows_and_saves_nothing(spy, monkeypatch):
    spy.reserve()
    take_over(spy)
    spy.scan_seconds = 5
    monkeypatch.setattr(spy.routes, "RESPONSE_TIMEOUT", 0.3)
    out = run_stream(spy)
    assert spy.analyze_calls == [] and GOOD_ANSWER not in shown(out)                    # abandoned before any model call
    assert doc(spy)["inflight"]["token"] == "tok-b" and doc(spy)["completed"] == 0


def test_late_stream_after_takeover_makes_no_model_call_and_shows_only_the_notice(spy):
    spy.reserve()
    take_over(spy)
    out = run_stream(spy)
    assert spy.analyze_calls == []                                                      # nothing generated
    assert GOOD_ANSWER not in shown(out) and "replaced by a newer one" in shown(out)
    assert len(spy.saved) == 1 and json.loads(spy.saved[0])["answer_status"] == "incomplete"
    d = doc(spy)
    assert d["completed"] == 0 and d["inflight"]["token"] == "tok-b"                    # the newer search is untouched
    assert json.loads([c for c in out if '"done": true' in c][0][6:])["results"] == []


def test_supersession_during_the_analysis_discards_the_finished_answer(spy, monkeypatch):
    spy.reserve()

    def slow_model(*a, **k):
        spy.analyze_calls.append(1)
        take_over(spy)                                                                  # stale + a newer search takes the slot
        return GOOD_ANSWER
    monkeypatch.setattr(spy.routes, "analyze_with_claude", slow_model)
    out = run_stream(spy)
    assert spy.analyze_calls == [1]
    assert GOOD_ANSWER not in shown(out) and "replaced by a newer one" in shown(out)   # never shown ...
    assert len(spy.saved) == 1 and "Unique headline" not in spy.saved[0]                # ... never saved
    d = doc(spy)
    assert d["completed"] == 0 and d["inflight"]["token"] == "tok-b" and d.get("last_chat_id") is None


def test_background_retry_cannot_overlap_an_admitted_search(spy):
    spy.reserve()
    spy.gate.release("owner1", "tok-a")                                                 # A errored earlier and was released
    assert spy.gate.reserve("owner1", "tok-b", "tk2", "chat-2").reserved                # the guest started B
    out = run_stream(spy)                                                               # the worker's retry of A
    assert spy.analyze_calls == [] and GOOD_ANSWER not in shown(out)
    assert doc(spy)["inflight"]["token"] == "tok-b" and doc(spy)["completed"] == 0


def test_background_retry_with_a_free_slot_blocks_a_second_search_while_it_runs(spy, monkeypatch):
    spy.reserve()
    spy.gate.release("owner1", "tok-a")
    seen = {}

    def model(*a, **k):
        spy.analyze_calls.append(1)
        seen["b"] = spy.gate.reserve("owner1", "tok-b", "tk2", "chat-2").kind
        return GOOD_ANSWER
    monkeypatch.setattr(spy.routes, "analyze_with_claude", model)
    run_stream(spy)
    assert seen["b"] == gg.BUSY and doc(spy)["completed"] == 1 and spy.saved == [GOOD_ANSWER]


def test_save_failure_after_counting_shows_nothing_takes_the_count_back_and_a_refresh_redelivers_once(spy, monkeypatch):
    """The winning answer is SAVED before the first chunk is sent: if the save fails nothing reached the browser, so the
    count is taken back and the slot is free; a refresh regenerates and counts once."""
    spy.reserve()
    good_cas = spy.cas

    def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(spy.routes, "save_answer_for_gate", broken)
    out = run_stream(spy)
    assert GOOD_ANSWER not in shown(out) and any('"error"' in c for c in out)         # nothing was shown
    d = doc(spy)
    assert d["completed"] == 0 and d["inflight"] is None                                # count taken back, slot free
    monkeypatch.setattr(spy.routes, "save_answer_for_gate", good_cas)
    spy.analyze_calls.clear()
    out = run_stream(spy)                                                               # refresh: regenerate and deliver
    assert GOOD_ANSWER in shown(out) and spy.saved == [GOOD_ANSWER] and doc(spy)["completed"] == 1


def test_gate_store_outage_defers_without_a_model_call(spy):
    from tests.test_guest_gate import _BrokenColl
    spy.gate = gg.GuestGate(_BrokenColl(), limit=1)
    out = run_stream(spy)
    assert spy.analyze_calls == [] and spy.saved == [] and any('"error"' in c for c in out)


def test_stream_client_disconnect_counts_nothing(spy):
    spy.reserve()
    spy.scan_seconds = 10
    gen = iter(spy.routes.stream_answer(MagicMock(), "chat-d", "tk").body_iterator)
    next(gen); next(gen); next(gen)
    gen.close()                                                                         # the browser went away mid-scan
    d = doc(spy)
    assert d["completed"] == 0 and d["inflight"]["token"] == "tok-a"                    # held for the worker / stale recovery
    assert spy.saved == []


def test_website_only_stream_counts_once_discards_when_superseded_and_releases_on_failure(spy, monkeypatch):
    spy.msg.update(website_only=True, website_evidence={"url": "https://example.com", "structured_evidence": {}, "evidence_quality": "thin"})
    wi = types.SimpleNamespace(build_website_insight_answer=lambda **k: GOOD_ANSWER)
    monkeypatch.setattr(spy.routes, "website_intelligence", wi, raising=False)
    spy.reserve()
    out = run_stream(spy)
    assert GOOD_ANSWER in shown(out) and spy.saved == [GOOD_ANSWER] and doc(spy)["completed"] == 1
    # superseded before the model call
    spy.coll.delete_many({}); spy.saved.clear(); spy.msg["claude_answer"] = None
    spy.reserve(); take_over(spy)
    calls = []
    monkeypatch.setattr(spy.routes, "website_intelligence", types.SimpleNamespace(
        build_website_insight_answer=lambda **k: calls.append(1) or GOOD_ANSWER), raising=False)
    out = run_stream(spy, "chat-g2")
    assert calls == [] and GOOD_ANSWER not in shown(out) and "replaced by a newer one" in shown(out)
    assert doc(spy)["inflight"]["token"] == "tok-b" and doc(spy)["completed"] == 0
    # superseded during the model call
    spy.coll.delete_many({}); spy.saved.clear(); spy.msg["claude_answer"] = None
    spy.reserve()

    def slow(**k):
        take_over(spy)
        return GOOD_ANSWER
    monkeypatch.setattr(spy.routes, "website_intelligence", types.SimpleNamespace(build_website_insight_answer=slow), raising=False)
    out = run_stream(spy, "chat-g3")
    assert GOOD_ANSWER not in shown(out) and all("Unique headline" not in s for s in spy.saved)
    assert doc(spy)["completed"] == 0 and doc(spy)["inflight"]["token"] == "tok-b"
    # failure releases
    spy.coll.delete_many({}); spy.saved.clear(); spy.msg["claude_answer"] = None
    spy.reserve()

    def boom(**k):
        raise RuntimeError("model down")
    monkeypatch.setattr(spy.routes, "website_intelligence", types.SimpleNamespace(build_website_insight_answer=boom), raising=False)
    out = run_stream(spy, "chat-g4")
    assert any('"error"' in c for c in out) and doc(spy)["completed"] == 0 and doc(spy)["inflight"] is None


def test_stream_of_a_signed_in_guests_search_reports_to_the_guest_reservation(spy):
    """After sign-in the chat belongs to the account (owner1 here stands for it) but the message carries the guest id."""
    spy.msg["guest_gate_owner"] = "guest-77"
    assert spy.gate.reserve("guest-77", "tok-a", "tk", "chat-g").reserved
    run_stream(spy)
    d = spy.coll.find_one({"_id": "guest-77"})
    assert d["completed"] == 1 and d["inflight"] is None and spy.saved == [GOOD_ANSWER]
    assert spy.coll.find_one({"_id": "owner1"}) is None                                 # nothing recorded under the account
