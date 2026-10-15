"""
tests/test_realtime_streaming.py — regression tests for the REAL-TIME STREAMING
protocol in GET /chat/{id}/stream (routes.py).

What this locks down (all PRE-EXISTING behaviour; these tests are additive and
touch no implementation file):
  * The readable-prose extractor `_partial_json_string_value` and
    `_ReadableFieldStreamer` only ever surface the decoded `headline` /
    `executive_summary` string values — never raw JSON syntax — and emit
    forward-growing deltas from a still-incomplete buffer.
  * `_looks_like_answer_json` gates the blocking fallback.
  * The route streams `answer_text` events (readable prose) while the model
    writes, then delivers the authoritative, post-processed JSON in the final
    `done` event's `answer` field — source URLs patched, results included.
  * The blocking fallback runs exactly once, only when the stream fails or
    yields no usable JSON, with a single persistence and no duplicate results.

Offline: in-memory chats, stubbed LLM; no real Mongo / OpenAI / network.
"""
import json

import pytest

from tests.test_reliability_ux_perf import OWNER, _Req, app_env, env  # noqa: F401


# ── pure helpers: readable-field extraction ──────────────────────────────────
def test_partial_json_string_value_reads_a_growing_value(app_env):
    _lg, _idx, rt, _chats, _state = app_env
    f = rt._partial_json_string_value
    # value still being written (no closing quote yet)
    assert f('{"headline": "Reddit buyers looking', "headline") == "Reddit buyers looking"
    # complete value, stops at the unescaped closing quote
    assert f('{"headline": "Done", "executive_summary": "x"}', "headline") == "Done"
    # field not present yet
    assert f('{"form', "headline") == ""
    # escaped quote inside the value does not terminate it
    assert f('{"headline": "a \\"b\\" c', "headline") == 'a "b" c'
    # escaped newline decodes
    assert f('{"executive_summary": "line1\\nline2', "executive_summary") == "line1\nline2"


def test_partial_json_string_value_tolerates_dangling_escapes(app_env):
    _lg, _idx, rt, _chats, _state = app_env
    f = rt._partial_json_string_value
    assert f('{"headline": "abc\\', "headline") == "abc"          # dangling backslash dropped
    assert f('{"headline": "abc\\u00', "headline") == "abc"       # half-written \\uXXXX dropped
    assert f('{"headline": "abc\\u0041', "headline") == "abcA"    # complete \\u escape decodes


def test_readable_field_streamer_emits_forward_only_prose(app_env):
    _lg, _idx, rt, _chats, _state = app_env
    s = rt._ReadableFieldStreamer()
    out = []
    # feed a source_list answer token by token
    raw = '{"format": "source_list", "headline": "Buyers found", "executive_summary": "Two posts. Clear intent."}'
    for ch in raw:
        out += s.feed(ch)
    joined = "".join(out)
    # only the decoded readable fields, in order, never JSON punctuation
    assert joined == "Buyers found\n\nTwo posts. Clear intent."
    assert "{" not in joined and '"format"' not in joined and "headline" not in joined
    # every emitted piece is a forward extension (never a rewrite/backtrack)
    rebuilt = ""
    for piece in out:
        rebuilt += piece
    assert rebuilt == joined


def test_readable_field_streamer_ignores_internal_fields(app_env):
    _lg, _idx, rt, _chats, _state = app_env
    s = rt._ReadableFieldStreamer()
    raw = ('{"format":"source_list","headline":"H","research_objective":"SECRET internal",'
           '"executive_summary":"Shown"}')
    joined = "".join(p for ch in raw for p in s.feed(ch))
    assert joined == "H\n\nShown"
    assert "SECRET" not in joined           # research_objective is never streamed


def test_looks_like_answer_json(app_env):
    _lg, _idx, rt, _chats, _state = app_env
    assert rt._looks_like_answer_json('{"format": "source_list", "headline": "x"}') is True
    assert rt._looks_like_answer_json('{"headline": "x"}') is False        # no "format"
    assert rt._looks_like_answer_json('not json') is False
    assert rt._looks_like_answer_json('') is False


# ── route-level: the streaming protocol end to end ───────────────────────────
POST = {"title": "Need a CRM", "post_text": "Looking for a CRM for my team.",
        "post_url": "https://www.reddit.com/r/crm/comments/abc/need_a_crm/", "platform": "reddit", "subreddit": "crm"}

# a source_list answer with NO "link" on the post — so we can prove
# _patch_post_urls_into_answer injected the real URL on the final answer.
ANSWER = {"format": "source_list", "headline": "Buyers actively looking for a CRM",
          "executive_summary": "One post shows clear intent. Budget-conscious buyer.",
          "platforms": [{"platform": "reddit", "total_analyzed": 1, "shown_count": 1,
                         "posts": [{"index": 1, "source": "crm", "title": "Need a CRM",
                                    "summary": "Asks for a CRM."}]}]}


def _seed_pending(chats, chat_id="s1"):
    import datetime
    now = datetime.datetime.utcnow()
    msg = {"query": "Find CRM buyers", "topic_key": "find crm buyers", "keywords": ["crm"], "match_phrases": None,
           "targeting_platform": "reddit", "time_window_days": None, "time_range": None, "evidence_required": 1,
           "unfiltered": False, "website_only": False, "website_context": None, "google_fallback_triggered": True,
           "search_progress_generated": True, "message_type": "search", "requested_at": now,
           "results": None, "claude_answer": None}
    chats.docs[(chat_id, OWNER)] = {"chat_id": chat_id, "owner_key": OWNER, "owner_type": "guest", "title": "S",
                                    "messages": [msg], "summary": "", "created_at": now, "updated_at": now}
    return chat_id


def _drive_stream(rt, monkeypatch, chat_id, topic_key="find crm buyers"):
    import types
    monkeypatch.setattr(rt, "StreamingResponse", lambda gen, **kw: types.SimpleNamespace(body_iterator=gen))
    monkeypatch.setattr(rt.config, "NONBLOCKING_STREAM_ENABLED", False, raising=False)
    gen = iter(rt.stream_answer(_Req(), chat_id, topic_key).body_iterator)
    events = []
    for line in gen:
        if isinstance(line, str) and line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    return events


def _common_stubs(app_env, monkeypatch):
    lg, idx, rt, chats, state = app_env
    monkeypatch.setattr(idx, "chats_collection", chats)
    monkeypatch.setattr(rt, "get_evidence_with_topup", lambda **k: [dict(POST)])
    monkeypatch.setattr(rt, "get_matched_signals", lambda *a, **k: [dict(POST)])
    fl = __import__("sys").modules["flintel"]
    monkeypatch.setattr(fl, "format_google_stub_results", lambda d: [], raising=False)
    monkeypatch.setattr(fl, "merge_matched_and_google_results",
                        lambda m, g, max_total=None, **k: list(m)[:max_total] if max_total else list(m), raising=False)
    monkeypatch.setattr(fl, "build_combined_source_context", lambda a, b: "C", raising=False)
    monkeypatch.setattr(fl, "build_unfiltered_answer_context", lambda q, d: "U", raising=False)
    monkeypatch.setattr(rt, "append_to_chat_summary", lambda *a, **k: None, raising=False)
    saves = {"answer": 0, "results": 0}
    real_answer = idx.save_claude_answer_to_chat

    def save_answer(cid, owner, tk, ans):
        saves["answer"] += 1
        return real_answer(cid, owner, tk, ans)
    monkeypatch.setattr(rt, "save_claude_answer_to_chat", save_answer, raising=False)
    monkeypatch.setattr(rt, "save_signal_results_to_chat",
                        lambda *a, **k: saves.__setitem__("results", saves["results"] + 1), raising=False)
    return saves


def test_stream_emits_readable_prose_then_the_validated_answer(app_env, monkeypatch):
    lg, idx, rt, chats, state = app_env
    saves = _common_stubs(app_env, monkeypatch)
    calls = {"stream": 0, "blocking": 0}

    def fake_stream(query, pool, extra_context=None):
        calls["stream"] += 1
        for ch in json.dumps(ANSWER):          # token-by-token
            yield ch

    monkeypatch.setattr(rt, "analyze_with_claude_stream", fake_stream)
    monkeypatch.setattr(rt, "analyze_with_claude", lambda *a, **k: calls.__setitem__("blocking", calls["blocking"] + 1) or "{}")
    cid = _seed_pending(chats)
    events = _drive_stream(rt, monkeypatch, cid)

    texts = [e["answer_text"] for e in events if "answer_text" in e]
    done = [e for e in events if e.get("done")]
    assert calls["stream"] == 1 and calls["blocking"] == 0        # streamed, no fallback
    assert texts, "no answer_text events were emitted"
    joined = "".join(texts)
    assert joined == "Buyers actively looking for a CRM\n\nOne post shows clear intent. Budget-conscious buyer."
    assert "{" not in joined and '"format"' not in joined         # never raw JSON
    assert len(done) == 1
    final = json.loads(done[0]["answer"])                         # authoritative, parseable
    assert final["format"] == "source_list"
    # source URL was patched onto the post by the UNCHANGED post-processing
    assert final["platforms"][0]["posts"][0]["link"] == POST["post_url"]
    assert done[0]["results"] and done[0]["results"][0]["post_url"] == POST["post_url"]
    assert saves["answer"] == 1 and saves["results"] == 1         # persisted once


def test_blocking_fallback_runs_once_when_stream_fails(app_env, monkeypatch):
    lg, idx, rt, chats, state = app_env
    saves = _common_stubs(app_env, monkeypatch)
    calls = {"stream": 0, "blocking": 0}

    def boom(query, pool, extra_context=None):
        calls["stream"] += 1
        yield '{"format": "source_l'      # partial, then fail
        raise RuntimeError("stream dropped")

    def blocking(query, pool, extra_context=None):
        calls["blocking"] += 1
        return json.dumps(ANSWER)

    monkeypatch.setattr(rt, "analyze_with_claude_stream", boom)
    monkeypatch.setattr(rt, "analyze_with_claude", blocking)
    cid = _seed_pending(chats, "s2")
    events = _drive_stream(rt, monkeypatch, cid)

    done = [e for e in events if e.get("done")]
    assert calls["stream"] == 1 and calls["blocking"] == 1        # exactly one fallback call
    assert len(done) == 1
    final = json.loads(done[0]["answer"])
    assert final["format"] == "source_list"
    assert final["platforms"][0]["posts"][0]["link"] == POST["post_url"]
    assert saves["answer"] == 1 and saves["results"] == 1         # single persistence, no duplicate
    # any provisional text from the partial stream never contained raw JSON
    assert all("{" not in e.get("answer_text", "") for e in events)


def test_nonjson_stream_degrades_gracefully_without_duplicate_generation(app_env, monkeypatch):
    """A stream that yields prose with no JSON object is NOT retried via the
    blocking path — `_extract_json_object_from_text` wraps it into a valid,
    format-bearing `no_results` envelope, so exactly one answer is produced and
    persisted, no raw JSON leaks to the client, and nothing crashes. (The
    blocking fallback is reserved for an actual stream exception; see the test
    above.)"""
    lg, idx, rt, chats, state = app_env
    saves = _common_stubs(app_env, monkeypatch)
    calls = {"stream": 0, "blocking": 0}

    def not_json(query, pool, extra_context=None):
        calls["stream"] += 1
        for ch in "I think the answer is probably about CRMs.":   # prose, no JSON object
            yield ch

    monkeypatch.setattr(rt, "analyze_with_claude_stream", not_json)
    monkeypatch.setattr(rt, "analyze_with_claude",
                        lambda *a, **k: calls.__setitem__("blocking", calls["blocking"] + 1) or json.dumps(ANSWER))
    cid = _seed_pending(chats, "s3")
    events = _drive_stream(rt, monkeypatch, cid)

    done = [e for e in events if e.get("done")]
    assert calls["stream"] == 1 and calls["blocking"] == 0        # no duplicate generation
    assert len(done) == 1
    final = json.loads(done[0]["answer"])                         # always parseable, format-bearing
    assert final.get("format") == "no_results"
    assert saves["answer"] == 1 and saves["results"] == 1         # single persistence
    assert all("{" not in e.get("answer_text", "") for e in events)   # never leaks raw JSON
