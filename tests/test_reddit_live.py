"""
tests/test_reddit_live.py — the live Reddit links feature (reddit_live.py), UNIT level.

EVERY test here is mocked: mongomock for the database, a fake callable (or httpx.MockTransport) for the HTTP call, a fixed
clock, a made-up API key. No network, no real OpenAI request, no production database. An autouse fixture makes any socket
connection fail loudly.

What these tests can NOT show: that the real web_search tool accepts this request, which Reddit URLs it returns, how
fresh they are, what a call costs, or MongoDB's behaviour on a real server (mongomock is not MongoDB).
"""
import json
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

mongomock = pytest.importorskip("mongomock")
import httpx as _REAL_HTTPX   # bound at import time, before any fixture of another test module can stub sys.modules['httpx']
import answer_integrity as ai  # noqa: E402
import reddit_live as rl  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
OWNER = "alice@example.com"
URL1 = "https://www.reddit.com/r/SaaS/comments/abc123/some_post/"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def _no_inflight_leak():
    assert rl.INFLIGHT.count == 0
    yield
    assert rl.INFLIGHT.count == 0, "an in-flight slot was leaked"


@pytest.fixture
def on(monkeypatch):
    """Feature on, caps and in-flight limit valid, a made-up key (the config module reddit_live is bound to)."""
    c = rl.config
    monkeypatch.setattr(c, "REDDIT_LIVE_LINKS_ENABLED", True, raising=False)
    monkeypatch.setattr(c, "REDDIT_LIVE_DAILY_CAP_GLOBAL", 100, raising=False)
    monkeypatch.setattr(c, "REDDIT_LIVE_DAILY_CAP_PER_OWNER", 10, raising=False)
    monkeypatch.setattr(c, "REDDIT_LIVE_MAX_INFLIGHT", 2, raising=False)
    monkeypatch.setattr(c, "REDDIT_LIVE_OWNER_MIN_INTERVAL_SECONDS", 30, raising=False)
    monkeypatch.setattr(c, "REDDIT_LIVE_TIMEOUT_SECONDS", 25, raising=False)
    monkeypatch.setattr(c, "OPENAI_API_KEY", "test-key-not-real", raising=False)
    return c


# ── builders ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _url(n):
    return f"https://www.reddit.com/r/x/comments/p{n}/post_{n}/"


def _sig(n, text="We need a tool to monitor Reddit for our SaaS and our budget is approved for this quarter."):
    return {"title": f"Looking for a Reddit monitoring tool {n}", "post_text": text, "post_url": _url(n),
            "platform": "reddit", "subreddit": "SaaS"}


def answer(n_ok=0, n_unverified=0, fmt="source_list"):
    """A REAL enforced answer (answer_integrity.enforce_answer_integrity, offline): n_ok evidence rows, n_unverified rows
    whose stored post has no text."""
    sigs = [_sig(i + 1) for i in range(n_ok)] + [_sig(100 + i, "") for i in range(n_unverified)]
    posts = [{"index": i + 1, "link": s["post_url"], "title": s["title"], "role": "buyer", "purchase_intent": "explicit",
              "product_fit": "fits"} for i, s in enumerate(sigs)]
    data = {"format": fmt, "executive_summary": "x", "key_findings": [],
            "platforms": [{"platform": "reddit", "total_analyzed": len(posts), "shown_count": len(posts), "posts": posts}]}
    return json.dumps(ai.enforce_answer_integrity(data, sigs, sigs, "Find SaaS founders looking for a Reddit monitoring tool"))


def message(i=0, _drop=(), **over):
    """A plain accepted search as add_search_to_chat stores it through the credited accept path (verified fields)."""
    m = {"topic_key": f"t{i}", "request_id": f"r{i}", "query": "reddit monitoring tool", "claude_answer": answer(0),
         "keywords": ["reddit monitoring tool"], "credit_request_id": f"cr{i}", "evidence_required": 15,
         "time_window_days": None, "time_range": None, "website_only": False, "website_evidence": None,
         "website_context": None, "website_note": None,
         "answered_at": NOW - timedelta(seconds=20), "results": []}
    m.update(over)
    for k in _drop:
        m.pop(k, None)
    return m


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class Env:
    def __init__(self, messages=None, owner=OWNER):
        client = mongomock.MongoClient()["live"]
        self.chats, self.budget, self.attempts = client["chats"], client["budget"], client["attempts"]
        self.owner = owner
        self.chats.insert_one({"chat_id": "c1", "owner_key": owner, "messages": messages or [message(0)]})
        self.clock = Clock()
        self.calls = []
        self.usage = []
        self.response = good_response()
        self.raises = None
        self.deps = rl.Deps(self.chats, self.budget, self.attempts, self._post, self.clock,
                            lambda model, data, status: self.usage.append((model, data, status)))

    def _post(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        if self.raises:
            raise self.raises
        return self.response

    def add_chat(self, owner, chat_id, msgs=None):
        self.chats.insert_one({"chat_id": chat_id, "owner_key": owner, "messages": msgs or [message(0)]})

    def run(self, idx=0, owner_type="email", owner=None, chat_id="c1", **kw):
        return rl.process(self.deps, owner_key=owner or self.owner, owner_type=owner_type, chat_id=chat_id,
                          request_id=kw.pop("request_id", f"r{idx}"), topic_key=kw.pop("topic_key", None))

    def stored(self, idx=0, chat_id="c1"):
        return self.chats.find_one({"chat_id": chat_id})["messages"][idx].get("live_reddit")

    def counter(self):
        return self.budget.find_one({"_id": "day:" + self.clock().strftime("%Y-%m-%d")})


def good_response(urls=(URL1,), title="A post title", usage=True):
    out = [{"type": "web_search_call", "id": "ws_1", "status": "completed",
            "action": {"type": "search", "sources": [{"type": "url", "url": u} for u in urls]}},
           {"type": "message", "content": [{"type": "output_text", "text": "MODEL PROSE THAT MUST NEVER BE STORED OR SHOWN",
                                            "annotations": [{"type": "url_citation", "url": u, "title": title,
                                                             "start_index": 0, "end_index": 3} for u in urls]}]}]
    resp = {"id": "resp_1", "status": "completed", "output": out}
    if usage:
        resp["usage"] = {"input_tokens": 1200, "output_tokens": 300}
    return resp


# ── configuration: inert unless enabled AND caps / in-flight limit are valid and within their ceilings ──────────────────
@pytest.mark.parametrize("enabled,g,o,infl,expected", [
    (False, 10, 5, 2, False), (True, 0, 5, 2, False), (True, 10, 0, 2, False), (True, -1, 5, 2, False), (True, 10, -3, 2, False),
    (True, "10", 5, 2, False), (True, 10, "5", 2, False), (True, True, 5, 2, False), (True, 10.0, 5, 2, False),
    (True, None, None, 2, False), (True, 10, 5, 2, True), ("true", 10, 5, 2, False),
    # ceilings: exactly at the ceiling is fine, one above keeps the feature off
    (True, rl.GLOBAL_CAP_CEILING, rl.OWNER_CAP_CEILING, 2, True), (True, rl.GLOBAL_CAP_CEILING + 1, 5, 2, False),
    (True, 10, rl.OWNER_CAP_CEILING + 1, 2, False),
    # the in-flight limit
    (True, 10, 5, 0, False), (True, 10, 5, -1, False), (True, 10, 5, None, False), (True, 10, 5, "2", False),
    (True, 10, 5, True, False), (True, 10, 5, 2.0, False), (True, 10, 5, rl.INFLIGHT_CEILING, True),
    (True, 10, 5, rl.INFLIGHT_CEILING + 1, False)])
def test_is_active_requires_flag_valid_caps_and_inflight_limit(monkeypatch, enabled, g, o, infl, expected):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_LINKS_ENABLED", enabled, raising=False)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_GLOBAL", g, raising=False)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_PER_OWNER", o, raising=False)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", infl, raising=False)
    assert rl.is_active() is expected


def _clean_env_config(extra=None):
    """Import config in a fresh interpreter whose environment sets NOTHING about this feature (except `extra`)."""
    code = ("import sys, json; sys.path.insert(0, %r); import config as c; "
            "print(json.dumps([c.REDDIT_LIVE_LINKS_ENABLED, c.REDDIT_LIVE_DAILY_CAP_GLOBAL, c.REDDIT_LIVE_DAILY_CAP_PER_OWNER, "
            "c.REDDIT_LIVE_MAX_INFLIGHT]))" % str(ROOT))
    env = {"PATH": os.environ.get("PATH", ""), "SESSION_SECRET_KEY": "x" * 40, "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra or {})
    out = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_committed_defaults_with_a_clean_environment_are_off_with_no_caps():
    assert _clean_env_config() == [False, 0, 0, 2]


@pytest.mark.parametrize("env,expected", [
    ({"REDDIT_LIVE_LINKS_ENABLED": "true", "REDDIT_LIVE_DAILY_CAP_GLOBAL": "abc", "REDDIT_LIVE_DAILY_CAP_PER_OWNER": "5"}, [True, 0, 5, 2]),
    ({"REDDIT_LIVE_DAILY_CAP_GLOBAL": "-4", "REDDIT_LIVE_DAILY_CAP_PER_OWNER": "0"}, [False, 0, 0, 2]),
    ({"REDDIT_LIVE_DAILY_CAP_GLOBAL": "7", "REDDIT_LIVE_DAILY_CAP_PER_OWNER": "3", "REDDIT_LIVE_MAX_INFLIGHT": "4"}, [False, 7, 3, 4]),
    ({"REDDIT_LIVE_MAX_INFLIGHT": "abc"}, [False, 0, 0, 0]), ({"REDDIT_LIVE_MAX_INFLIGHT": "0"}, [False, 0, 0, 0]),
    ({"REDDIT_LIVE_MAX_INFLIGHT": "-1"}, [False, 0, 0, 0]), ({"REDDIT_LIVE_MAX_INFLIGHT": "  "}, [False, 0, 0, 2])])
def test_config_parsing_never_turns_an_invalid_value_into_a_valid_one(env, expected):
    assert _clean_env_config(env) == expected


def test_disabled_makes_no_database_access_and_no_call(monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_LINKS_ENABLED", False, raising=False)
    env = Env()

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("database touched while the feature is off")
    env.deps.chats = env.deps.budget = env.deps.attempts = Boom()
    assert rl.process(env.deps, owner_key=OWNER, owner_type="email", chat_id="c1", request_id="r0") == {"state": "disabled"}
    assert env.calls == []


@pytest.mark.parametrize("g,o", [(0, 5), (5, 0), (-1, 5), (5, "x"), (rl.GLOBAL_CAP_CEILING + 1, 5), (5, rl.OWNER_CAP_CEILING + 1)])
def test_an_invalid_or_over_ceiling_cap_means_no_call_and_no_claim(on, monkeypatch, g, o):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_GLOBAL", g, raising=False)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_PER_OWNER", o, raising=False)
    env = Env()
    assert env.run()["state"] == "disabled"
    assert env.calls == [] and env.stored() is None and env.counter() is None


@pytest.mark.parametrize("g,o", [(0, 5), (5, 0), (rl.GLOBAL_CAP_CEILING + 1, 5), (5, rl.OWNER_CAP_CEILING + 1), (None, None)])
def test_the_budget_itself_denies_when_a_cap_is_invalid_or_over_its_ceiling(on, monkeypatch, g, o):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_GLOBAL", g, raising=False)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_PER_OWNER", o, raising=False)
    env = Env()
    assert rl._reserve_budget(env.deps, "h" * 32, NOW) is False and env.counter() is None


# ── the threshold: persisted evidence rows, existing verification and time-window rules ─────────────────────────────────
@pytest.mark.parametrize("n_ok,n_bad,expected", [(0, 0, 0), (1, 0, 1), (2, 0, 2), (3, 0, 3), (4, 0, 4), (2, 3, 2), (0, 5, 0)])
def test_count_uses_the_existing_evidence_rule(n_ok, n_bad, expected):
    assert rl.count_evidence_rows(answer(n_ok, n_bad)) == expected


def test_out_of_window_rows_do_not_count():
    data = json.loads(answer(3))
    data["platforms"][0]["posts"][0]["in_window"] = False          # the field enforce_answer_integrity sets for a bounded search
    assert rl.count_evidence_rows(json.dumps(data)) == 2
    assert ai.is_evidence_row(data["platforms"][0]["posts"][1]) is True


def test_rows_with_analysis_not_matched_do_not_count():
    data = json.loads(answer(3))
    data["platforms"][0]["posts"][0]["analysis_verified"] = False
    assert rl.count_evidence_rows(json.dumps(data)) == 2


def test_subject_rows_count_and_duplicate_links_count_once():
    data = json.loads(answer(2))
    data["subjects"] = [{"platforms": json.loads(answer(2))["platforms"]}]       # the same two links again
    assert rl.count_evidence_rows(json.dumps(data)) == 2


@pytest.mark.parametrize("text", [None, "", "plain text answer", "[1, 2]", json.dumps({"x": 1}), "{not json"])
def test_unreadable_answers_are_unknown_not_zero(text):
    assert rl.count_evidence_rows(text) is None


def test_a_structured_answer_without_a_post_table_counts_zero():
    assert rl.count_evidence_rows(json.dumps({"format": "no_results", "message": "nothing"})) == 0


@pytest.mark.parametrize("n_ok,calls", [(0, 1), (1, 1), (2, 1), (3, 0), (4, 0)])
def test_live_search_only_below_three_rows(on, n_ok, calls):
    env = Env([message(0, claude_answer=answer(n_ok))])
    env.run()
    assert len(env.calls) == calls


def test_unreadable_saved_answer_makes_no_call(on):
    env = Env([message(0, claude_answer="plain text")])
    assert env.run()["state"] == "ineligible" and env.calls == [] and env.stored() is None and env.counter() is None


# ── eligibility (Option A): positive, conservative, nothing inferred from website_note ─────────────────────────────────
INELIGIBLE = [
    ("chat turn", {"message_type": "chat"}, ()),
    ("no request id", {"request_id": None}, ()),
    ("research", {"request_kind": "research"}, ()), ("person_mentions", {"request_kind": "person_mentions"}, ()),
    ("provider_search", {"request_kind": "provider_search"}, ()),
    ("explicit lead_discovery is never stored, so it is not accepted", {"request_kind": "lead_discovery"}, ()),
    ("empty kind", {"request_kind": ""}, ()), ("unknown kind", {"request_kind": "whatever"}, ()),
    ("website_only", {"website_only": True}, ()),
    ("website_evidence", {"website_evidence": {"url": "https://x.example", "structured_evidence": {}}}, ()),
    ("website_context", {"website_context": {"url": "https://x.example"}}, ()),
    ("time_range", {"time_range": {"start": "2026-10-01", "end": "2026-10-08"}}, ()),
    ("time_window_days 7", {"time_window_days": 7}, ()), ("time_window_days 0", {"time_window_days": 0}, ()),
    ("keywords None", {"keywords": None}, ()), ("keywords empty", {"keywords": []}, ()), ("keywords blank", {"keywords": [""]}, ()),
    ("keywords mixed", {"keywords": ["a", 3]}, ()), ("keywords a string", {"keywords": "abc"}, ()),
    ("keywords missing", {}, ("keywords",)),
    ("no credit_request_id", {"credit_request_id": None}, ()), ("empty credit_request_id", {"credit_request_id": ""}, ()),
    ("credit_request_id missing", {}, ("credit_request_id",)),
    ("evidence_required None", {"evidence_required": None}, ()), ("evidence_required 0", {"evidence_required": 0}, ()),
    ("evidence_required negative", {"evidence_required": -1}, ()), ("evidence_required bool", {"evidence_required": True}, ()),
    ("evidence_required str", {"evidence_required": "15"}, ()), ("evidence_required float", {"evidence_required": 15.5}, ()),
    ("evidence_required missing", {}, ("evidence_required",)),
    ("cancelled", {"cancelled_at": NOW}, ()), ("failed", {"failed_at": NOW}, ()),
    ("unanswered", {"claude_answer": None}, ()), ("empty answer", {"claude_answer": ""}, ()),
    ("answered_at missing", {"answered_at": None}, ()), ("answered 3 h ago", {"answered_at": NOW - timedelta(hours=3)}, ()),
    ("answered in the future", {"answered_at": NOW + timedelta(minutes=5)}, ())]


@pytest.mark.parametrize("name,over,drop", INELIGIBLE, ids=[c[0] for c in INELIGIBLE])
def test_ineligible_messages_never_call_and_write_nothing(on, name, over, drop):
    env = Env([message(0, _drop=drop, **over)])
    res = env.run(request_id=over.get("request_id", "r0") or "r0", topic_key="t0")
    assert res["state"] in ("ineligible", "not_found")
    assert env.calls == [] and env.stored() is None and env.counter() is None


@pytest.mark.parametrize("over", [{}, {"request_kind": "mention_search", "subject": "Acme"}], ids=["plain lead search", "mention_search"])
def test_the_two_allowed_kinds_are_eligible(on, over):
    env = Env([message(0, **over)])
    assert env.run()["state"] == "ok" and len(env.calls) == 1


def test_website_note_alone_no_longer_decides_anything(on):
    """website_note also carries time-range and guidance notes; it is not a search-type signal and is not consulted."""
    env = Env([message(0, website_note="Guidance: keep it short.")])
    assert env.run()["state"] == "ok" and len(env.calls) == 1


def test_guest_is_excluded_before_any_database_read(on):
    env = Env()

    class Spy:
        reads = 0

        def find_one(self, *a, **k):
            Spy.reads += 1

        update_one = find_one
    env.deps.chats = Spy()
    assert env.run(owner_type="anon", owner="anon-uuid") == {"state": "ineligible"}
    assert Spy.reads == 0 and env.calls == []


def test_another_owners_chat_is_not_found_and_never_called(on):
    env = Env()
    res = rl.process(env.deps, owner_key="mallory@example.com", owner_type="email", chat_id="c1", request_id="r0")
    assert res == {"state": "not_found"} and env.calls == [] and env.stored() is None and env.counter() is None


def test_unknown_message_is_not_found(on):
    env = Env()
    assert env.run(request_id="nope")["state"] == "not_found" and env.calls == []


# ── URL validation: adversarial ──────────────────────────────────────────────────────────────────────────────────────────
VALID_URLS = [
    ("https://www.reddit.com/r/SaaS/comments/abc123/some_post/", "https://www.reddit.com/r/SaaS/comments/abc123/some_post/"),
    ("https://www.reddit.com/r/SaaS/comments/abc123/some_post", "https://www.reddit.com/r/SaaS/comments/abc123/some_post"),
    ("https://old.reddit.com/r/startups/comments/1abcd2/", "https://old.reddit.com/r/startups/comments/1abcd2/"),
    ("https://reddit.com/r/Entrepreneur/comments/zz99aa/title_here/def456/", "https://reddit.com/r/Entrepreneur/comments/zz99aa/title_here/def456/"),
    ("https://www.reddit.com/r/SaaS/comments/abc123/", "https://www.reddit.com/r/SaaS/comments/abc123/"),
    ("https://www.reddit.com/r/SaaS/comments/abc123/some_post/?utm_source=x&a=b#frag", "https://www.reddit.com/r/SaaS/comments/abc123/some_post/"),
    ("HTTPS://WWW.REDDIT.COM/r/SaaS/comments/abc123/x/", "https://www.reddit.com/r/SaaS/comments/abc123/x/"),
    ("https://www.reddit.com/r/Ask_Reddit_2/comments/9a9a9a/my-slug_1/", "https://www.reddit.com/r/Ask_Reddit_2/comments/9a9a9a/my-slug_1/")]

P = "/r/SaaS/comments/abc123/x/"
INVALID_URLS = [
    # backslash tricks (browsers read "\" as "/" in https URLs)
    "https://evil.com\\.reddit.com" + P, "https://evil.com\\@www.reddit.com" + P, "https://www.reddit.com\\.evil.com" + P,
    "https://www.reddit.com/r/SaaS\\comments/abc123/x/", "https://evil.com:443\\.reddit.com" + P,
    # percent-encoded host / authority tricks
    "https://evil.com%2f.reddit.com" + P, "https://www.reddit.com%2f@evil.com/", "https://www%2ereddit.com" + P,
    "https://www.reddit.com%00.evil.com" + P, "https://www.reddit.com/r/SaaS/comments/abc123/%2e%2e/x/",
    # user information
    "https://user@www.reddit.com" + P, "https://user:pw@www.reddit.com" + P, "https://www.reddit.com@evil.com" + P,
    "https://www.reddit.com:pw@evil.com" + P,
    # whitespace and control characters
    "https://www.reddit.com/r/SaaS/comments/abc123/x y/", "https://www.reddit.com/r/SaaS/comments/abc123/x\t/",
    "https://www.reddit.com/r/SaaS/comments/abc123/x\n/", "https://www.reddit.com/r/SaaS/comments/abc123/x\x00/",
    " https://www.reddit.com" + P, "https://www.reddit.com" + P + " ", "https://www.reddit.com " + P,
    "https://www.red\ndit.com" + P,
    # unicode / lookalike hosts and paths
    "https://evıl.reddit.com" + P, "https://www.rеddit.com" + P, "https://ｗｗｗ.reddit.com" + P,
    "https://www.reddit.com/r/SaaS/comments/abc123/ünï/", "https://xn--e.reddit.com" + P,
    "https://www.reddit。com" + P,
    # hosts that are not exactly a permitted Reddit host
    "https://evil.com" + P, "https://reddit.com.evil.com" + P, "https://notreddit.com" + P, "https://www.reddit.com." + P,
    "https://i.reddit.com" + P, "https://redd.it/abc123", "https://np.reddit.com" + P, "https://evil.reddit.com" + P,
    "https://sub.www.reddit.com" + P, "https://m.reddit.com" + P, "https://[::1]" + P, "https://[bad/",
    "https://evil.com#.reddit.com" + P, "https://evil.com?.reddit.com" + P, "https://evil.com/.reddit.com" + P,
    # ports
    "https://www.reddit.com:443" + P, "https://www.reddit.com:8443" + P, "https://www.reddit.com:80" + P,
    # schemes
    "http://www.reddit.com" + P, "javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "ftp://www.reddit.com" + P,
    "//www.reddit.com" + P, "www.reddit.com" + P, "file:///etc/passwd",
    # reddit pages that are not a post
    "https://www.reddit.com/user/someone", "https://www.reddit.com/u/someone", "https://www.reddit.com/search/?q=x",
    "https://www.reddit.com/r/SaaS/s/AbCdEf", "https://www.reddit.com/r/SaaS/", "https://www.reddit.com/r/SaaS/comments/",
    "https://www.reddit.com/r/SaaS/comments/ab/x/", "https://www.reddit.com/r/s/comments/abc123/x/",
    "https://www.reddit.com/r/SaaS/comments/ABC123/x/", "https://www.reddit.com/r/SaaS/comments/abc123/x/y/z/",
    "https://www.reddit.com/r/SaaS/comments/abc123//", "https://www.reddit.com/", "https://www.reddit.com", "",
    "https://www.reddit.com/r/SaaS/comments/abc123/x/" + "a" * 600,
    # not strings
    None, 5, [], b"https://www.reddit.com/r/SaaS/comments/abc123/x/", {"u": 1}]


@pytest.mark.parametrize("url,canonical", VALID_URLS)
def test_valid_reddit_post_urls_are_accepted_in_canonical_form(url, canonical):
    assert rl.valid_reddit_url(url) == canonical


@pytest.mark.parametrize("url", INVALID_URLS, ids=[repr(u)[:60] for u in INVALID_URLS])
def test_hostile_or_non_post_urls_are_rejected(url):
    assert rl.valid_reddit_url(url) is None


def test_every_reported_malicious_url_is_rejected_by_name():
    for url in ("https://evil.com\\.reddit.com/x", "https://evil.com%2f.reddit.com/", "https://evıl.reddit.com/",
                "https://www.reddit.com/user/someone", "https://www.reddit.com/search/?q=x", "https://www.reddit.com/r/a/s/AbCdEf"):
        assert rl.valid_reddit_url(url) is None, url


def test_the_same_validator_guards_storage_serving_and_rendering(on):
    hostile = ["https://evil.com\\.reddit.com/r/SaaS/comments/abc123/x/", "https://evıl.reddit.com/r/SaaS/comments/abc123/x/",
               "https://www.reddit.com/user/someone", "javascript:alert(1)"]
    # 1. storage: nothing hostile survives parse_response (and therefore nothing is persisted)
    assert rl.parse_response(good_response(tuple(hostile) + (URL1,)), 8)[0] == [{"url": URL1, "title": "A post title"}]
    env = Env()
    env.response = good_response(tuple(hostile))
    assert env.run() == {"state": "none"} and env.stored()["state"] == "empty" and env.stored()["links"] == []
    # 2. serving + 3. rendering: a record that was tampered with after the fact is cleaned on the way out
    tampered = {"state": "ok", "claim_id": "x", "links": [{"url": u, "title": "t"} for u in hostile]
                + [{"url": URL1 + "?x=1#f", "title": "ok"}]}
    env2 = Env([message(0, live_reddit=tampered)])
    served = env2.run()
    assert served["state"] == "ok" and [l["url"] for l in served["links"]] == [URL1]
    assert rl.render_links(tampered) == [{"url": URL1, "title": "ok"}]


def test_saved_links_still_render_while_the_feature_is_off_but_only_validated_ones(monkeypatch):
    # HISTORICAL-ONLY SEARCH (changed contract): links SAVED with an older answer are shown (the page labels them as saved), whatever the
    # Live settings are; every URL still passes the same validator.
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_LINKS_ENABLED", False, raising=False)
    assert rl.render_links({"state": "ok", "links": [{"url": URL1, "title": "t"}, {"url": "javascript:1", "title": "x"}]}) == [{"url": URL1, "title": "t"}]


@pytest.mark.parametrize("live", [None, "x", [], {"state": "error", "links": [{"url": URL1}]}, {"state": "ok", "links": "x"},
                                  {"state": "ok", "links": [None, 3, {"url": None}]}])
def test_render_links_is_total(on, live):
    assert rl.render_links(live) == []


# ── the request and the response ─────────────────────────────────────────────────────────────────────────────────────────
def test_request_shape_is_restricted_to_reddit_one_tool_call_nothing_stored(on):
    env = Env()
    env.run()
    c = env.calls[0]
    p = c["payload"]
    assert p["tools"] == [{"type": "web_search", "filters": {"allowed_domains": ["reddit.com"]}}]
    assert p["tool_choice"] == "required" and p["max_tool_calls"] == 1 and p["store"] is False
    assert p["include"] == ["web_search_call.action.sources"] and p["max_output_tokens"] > 0
    assert "test-key-not-real" not in json.dumps(p) and c["headers"]["Authorization"] == "Bearer test-key-not-real"
    assert c["timeout"] == 25 and len(env.calls) == 1


def test_payload_cleans_the_query_and_raises_minimal_reasoning(on, monkeypatch):
    monkeypatch.setattr(rl.config, "LLM_REASONING_EFFORT", "minimal", raising=False)
    p = rl.build_payload("find\x00 me\n\n a   tool " + "x" * 1000)
    assert "\x00" not in p["input"] and "\n" not in p["input"] and len(p["input"]) < 400
    assert p["reasoning"]["effort"] == "low"


def test_parse_keeps_only_returned_valid_links_and_never_prose():
    urls = ("https://www.reddit.com/r/SaaS/comments/abc123/one/", "https://evil.com/x",
            "http://www.reddit.com/r/SaaS/comments/def456/two/", "https://old.reddit.com/r/SaaS/comments/abc123/one/?utm=1")
    links, calls, sources = rl.parse_response(good_response(urls, title="  A <b>title</b>\n"), 8)
    assert links == [{"url": urls[0], "title": "A <b>title</b>"}]       # kept as text; the page escapes it. old.reddit duplicate dropped
    assert calls == 1 and sources == 4
    assert "PROSE" not in json.dumps(links)


def test_parse_has_no_invented_fields_and_title_may_be_absent():
    resp = {"output": [{"type": "web_search_call", "action": {"sources": [{"type": "url", "url": "https://www.reddit.com/r/SaaS/comments/abc999/z/"}]}}]}
    links, calls, sources = rl.parse_response(resp, 8)
    assert links == [{"url": "https://www.reddit.com/r/SaaS/comments/abc999/z/", "title": None}]
    assert set(links[0]) == {"url", "title"}


def test_parse_respects_max_links_known_urls_and_garbage():
    urls = tuple(f"https://www.reddit.com/r/SaaS/comments/abc{i:03d}/p/" for i in range(12))
    assert len(rl.parse_response(good_response(urls), 5)[0]) == 5
    assert [l["url"] for l in rl.parse_response(good_response(urls), 20, known_urls=[urls[0], urls[1]])[0]][0] == urls[2]
    for junk in (None, [], "x", {"output": "x"}, {"output": [None, 3, {"type": "message", "content": "x"}]}, {}):
        assert rl.parse_response(junk, 8) == ([], 0, 0)


# ── the single attempt, persistence and what is shown ───────────────────────────────────────────────────────────────────
def test_success_persists_links_only_and_second_post_reads_only(on):
    env = Env()
    first = env.run()
    assert first["state"] == "ok" and first["label"] == rl.LABEL
    assert first["links"] == [{"url": URL1, "title": "A post title"}]
    stored = env.stored()
    assert stored["state"] == "ok" and set(stored) == {"claim_id", "state", "claimed_at", "day", "links", "finished_at", "latency_ms"}
    assert "PROSE" not in json.dumps(stored, default=str) and "monitoring tool" not in json.dumps(stored, default=str)
    for _ in range(5):
        again = env.run()
        assert again["state"] == "ok" and again["links"] == first["links"]
    assert len(env.calls) == 1 and env.counter()["count"] == 1


def test_the_note_says_live_search_and_unverified_dates_and_never_just_now():
    assert "live web search" in rl.NOTE and "dates and recency are unverified" in rl.NOTE
    assert "just now" not in rl.NOTE.lower() and "current" not in rl.NOTE.lower()
    assert rl.LABEL == "Live web-search links — unverified"


@pytest.mark.parametrize("how", ["timeout", "http429", "http500", "bad_json", "no_sources", "non_reddit", "not_a_dict"])
def test_failures_make_exactly_one_attempt_show_nothing_and_are_final(on, how):
    env = Env()

    class Err(Exception):
        def __init__(self, code):
            self.response = type("R", (), {"status_code": code})()
    if how == "timeout":
        env.raises = TimeoutError("slow")
    elif how == "http429":
        env.raises = Err(429)
    elif how == "http500":
        env.raises = Err(500)
    elif how == "bad_json":
        env.raises = ValueError("not json")
    elif how == "no_sources":
        env.response = {"id": "r", "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}]}
    elif how == "non_reddit":
        env.response = good_response(("https://evil.com/x", "http://www.reddit.com/r/SaaS/comments/abc123/x/"))
    else:
        env.response = ["x"]
    assert env.run() == {"state": "none"}
    assert env.stored()["state"] in ("error", "empty") and not env.stored().get("links")
    for _ in range(3):
        assert env.run() == {"state": "none"}
    assert len(env.calls) == 1 and env.counter()["count"] == 1          # a failed call stays counted, never retried


def test_accounting_records_what_the_response_reports_and_no_money(on):
    env = Env()
    env.run()
    assert len(env.usage) == 1 and env.usage[0][0] and env.usage[0][2] == "ok"
    att = env.attempts.find_one({})
    assert att["web_search_calls"] == 1 and att["sources"] == 1 and att["links"] == 1
    assert att["input_tokens"] == 1200 and att["output_tokens"] == 300 and att["cost_usd"] is None
    assert att["owner"] != OWNER and OWNER not in json.dumps(att, default=str) and "monitoring" not in json.dumps(att, default=str)


def test_missing_usage_is_recorded_as_unknown_not_invented(on):
    env = Env()
    env.response = good_response(usage=False)
    env.run()
    att = env.attempts.find_one({})
    assert att["input_tokens"] is None and att["output_tokens"] is None and att["cost_usd"] is None


def test_failed_call_has_an_attempt_record_but_no_usage_record(on):
    env = Env()
    env.raises = TimeoutError("slow")
    env.run()
    assert env.usage == [] and env.attempts.count_documents({}) == 1


# ── _http_post, exercised with httpx.MockTransport (no socket is ever opened) ──────────────────────────────────────────
@pytest.fixture
def mock_httpx(monkeypatch):
    """Route reddit_live._http_post through httpx.MockTransport. Other test modules stub sys.modules['httpx'] and may leave it
    stubbed, so the REAL module (bound at import) is put back for the duration of the test."""
    real_client = _REAL_HTTPX.Client
    monkeypatch.setitem(sys.modules, "httpx", _REAL_HTTPX)
    state = {"requests": [], "client_kwargs": []}

    def install(handler):
        def factory(**kw):
            state["client_kwargs"].append(dict(kw))
            kw = {k: v for k, v in kw.items() if k != "transport"}
            return real_client(transport=_REAL_HTTPX.MockTransport(lambda req: (state["requests"].append(req), handler(req))[1]), **kw)
        monkeypatch.setattr(_REAL_HTTPX, "Client", factory)
    state["install"] = install
    return state


def test_http_post_sends_one_json_post_without_following_redirects(mock_httpx):
    httpx = _REAL_HTTPX
    mock_httpx["install"](lambda req: httpx.Response(200, json={"id": "resp_1", "output": []}))
    out = rl._http_post("https://api.example.test/v1/responses", {"Authorization": "Bearer k"}, {"model": "m"}, 25)
    assert out == {"id": "resp_1", "output": []}
    (req,) = mock_httpx["requests"]
    assert req.method == "POST" and str(req.url) == "https://api.example.test/v1/responses"
    assert req.headers["authorization"] == "Bearer k" and json.loads(req.content) == {"model": "m"}
    assert mock_httpx["client_kwargs"] == [{"timeout": 25, "follow_redirects": False}]


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 429, 500, 503])
def test_http_post_raises_on_redirects_and_errors_and_never_retries(mock_httpx, status):
    httpx = _REAL_HTTPX
    mock_httpx["install"](lambda req: httpx.Response(status, headers={"location": "https://evil.example/x"}, json={"e": 1}))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        rl._http_post("https://api.example.test/v1/responses", {}, {}, 25)
    assert ei.value.response.status_code == status and len(mock_httpx["requests"]) == 1


def test_http_post_propagates_timeouts_and_bad_bodies_once(mock_httpx):
    httpx = _REAL_HTTPX

    def boom(req):
        raise httpx.ReadTimeout("slow", request=req)
    mock_httpx["install"](boom)
    with pytest.raises(httpx.ReadTimeout):
        rl._http_post("https://api.example.test/v1/responses", {}, {}, 25)
    assert len(mock_httpx["requests"]) == 1
    mock_httpx["requests"].clear()
    mock_httpx["install"](lambda req: httpx.Response(200, content=b"<html>not json</html>"))
    with pytest.raises(ValueError):
        rl._http_post("https://api.example.test/v1/responses", {}, {}, 25)
    assert len(mock_httpx["requests"]) == 1


def test_full_flow_through_the_real_http_wrapper_makes_exactly_one_request(on, mock_httpx):
    httpx = _REAL_HTTPX
    mock_httpx["install"](lambda req: httpx.Response(200, json=good_response()))
    env = Env()
    env.deps.post = rl._http_post
    res = env.run()
    assert res["state"] == "ok" and res["links"][0]["url"] == URL1
    (req,) = mock_httpx["requests"]
    assert str(req.url) == rl.config.OPENAI_RESPONSES_URL and req.headers["authorization"] == "Bearer test-key-not-real"
    body = json.loads(req.content)
    assert body["tools"][0]["filters"] == {"allowed_domains": ["reddit.com"]} and body["max_tool_calls"] == 1
    assert "test-key-not-real" not in json.dumps(env.stored(), default=str)
    assert "test-key-not-real" not in json.dumps(env.attempts.find_one({}), default=str)
    env.run()
    assert len(mock_httpx["requests"]) == 1


# ── the in-flight guard (process-local) ────────────────────────────────────────────────────────────────────────────────
def test_guard_unit_semantics():
    g = rl._InflightGuard()
    for bad in (0, -1, None, "2", True, 2.0, rl.INFLIGHT_CEILING + 1):
        assert g.try_enter(bad) is False and g.count == 0
    assert g.try_enter(2) and g.try_enter(2) and g.count == 2
    assert g.try_enter(2) is False and g.count == 2
    g.leave()
    assert g.try_enter(2) and g.count == 2
    g.leave(); g.leave(); g.leave()
    assert g.count == 0


def test_second_concurrent_call_is_refused_before_claiming_or_counting_then_works_later(on, monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", 1, raising=False)
    env = Env()
    env.add_chat("bob@example.com", "c2")
    entered, release = threading.Event(), threading.Event()
    first_post = env._post

    def slow(url, headers, payload, timeout):
        entered.set()
        assert release.wait(10)
        return first_post(url, headers, payload, timeout)
    env.deps.post = slow
    results = []
    t = threading.Thread(target=lambda: results.append(env.run()))
    t.start()
    try:
        assert entered.wait(10)
        busy = rl.process(env.deps, owner_key="bob@example.com", owner_type="email", chat_id="c2", request_id="r0")
        assert busy == {"state": "busy"}
        # the first call is still in flight (its request is not recorded until it returns); the refused one claimed and counted nothing
        assert env.stored(chat_id="c2") is None and env.counter()["count"] == 1 and len(env.calls) == 0
    finally:
        release.set()
        t.join(10)
    assert results[0]["state"] == "ok" and rl.INFLIGHT.count == 0 and len(env.calls) == 1
    again = rl.process(env.deps, owner_key="bob@example.com", owner_type="email", chat_id="c2", request_id="r0")
    assert again["state"] == "ok" and len(env.calls) == 2 and env.counter()["count"] == 2


@pytest.mark.parametrize("limit", [0, -1, None, "2", True, 2.0, rl.INFLIGHT_CEILING + 1])
def test_an_invalid_inflight_limit_keeps_the_feature_off(on, monkeypatch, limit):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", limit, raising=False)
    env = Env()
    assert env.run()["state"] == "disabled" and env.calls == [] and env.stored() is None and env.counter() is None


def test_the_slot_is_given_back_on_every_exit_path(on):
    class Err(Exception):
        pass
    # the API call raises
    env = Env()
    env.raises = Err("boom")
    env.run()
    assert rl.INFLIGHT.count == 0
    # the budget refuses
    env = Env()
    env.deps.budget = type("B", (), {"find_one_and_update": lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("db"))})()
    env.run()
    assert rl.INFLIGHT.count == 0
    # the claim write raises
    env = Env()
    real = env.chats
    env.deps.chats = type("C", (), {"find_one": lambda self, *a, **k: real.find_one(*a, **k),
                                    "update_one": lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("db"))})()
    env.run()
    assert rl.INFLIGHT.count == 0
    # ineligible and not-found never take a slot
    env = Env([message(0, request_kind="research")])
    env.run(); env.run(request_id="nope")
    assert rl.INFLIGHT.count == 0


# ── concurrency, duplicates, stale claims, budget ───────────────────────────────────────────────────────────────────────
def test_many_concurrent_requests_for_one_message_make_one_call(on, monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", rl.INFLIGHT_CEILING, raising=False)
    env = Env()
    results = []
    barrier = threading.Barrier(24)

    def worker():
        barrier.wait()
        results.append(env.run()["state"])
    threads = [threading.Thread(target=worker) for _ in range(24)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(env.calls) == 1 and env.counter()["count"] == 1
    assert set(results) <= {"ok", "pending", "none", "busy"} and "ok" in results


def test_global_cap_holds_under_concurrency(on, monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_GLOBAL", 5, raising=False)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_MAX_INFLIGHT", rl.INFLIGHT_CEILING, raising=False)
    env = Env()
    for i in range(1, 30):                                   # 30 different owners, one eligible message each
        env.add_chat(f"user{i}@example.com", f"c{i}")
    barrier = threading.Barrier(30)

    def worker(i):
        barrier.wait()
        owner, chat = (OWNER, "c1") if i == 0 else (f"user{i}@example.com", f"c{i}")
        for _ in range(40):                                  # a refused 'busy' is simply asked again, as the page does
            if rl.process(env.deps, owner_key=owner, owner_type="email", chat_id=chat, request_id="r0")["state"] != "busy":
                break
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(env.calls) == 5 and env.counter()["count"] == 5


def test_per_owner_cap_and_interval(on, monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_PER_OWNER", 2, raising=False)
    env = Env([message(i) for i in range(4)])
    assert env.run(idx=0)["state"] == "ok"
    assert env.run(idx=1)["state"] == "none"                 # inside the 30 s minimum interval
    env.clock.t = NOW + timedelta(seconds=31)
    assert env.run(idx=2)["state"] == "ok"
    env.clock.t = NOW + timedelta(seconds=90)
    assert env.run(idx=3)["state"] == "none"                 # per-owner cap of 2 reached
    assert len(env.calls) == 2 and env.counter()["count"] == 2
    assert env.stored(1)["state"] == "skipped" and env.stored(3)["state"] == "skipped"


def test_a_refused_budget_is_final_for_that_message(on, monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_GLOBAL", 1, raising=False)
    env = Env([message(0), message(1)])
    env.run(idx=0)
    env.run(idx=1)
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_GLOBAL", 50, raising=False)
    env.clock.t = NOW + timedelta(minutes=2)
    assert env.run(idx=1) == {"state": "none"} and len(env.calls) == 1


def test_utc_day_boundary_resets_the_cap_and_counts_against_the_reservation_day(on, monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_DAILY_CAP_GLOBAL", 1, raising=False)
    late = datetime(2026, 11, 1, 23, 59, 50, tzinfo=timezone.utc)
    env = Env([message(0, answered_at=late - timedelta(seconds=5)), message(1, answered_at=late + timedelta(seconds=20))])
    env.clock.t = late
    assert env.run(idx=0)["state"] == "ok"
    env.clock.t = late + timedelta(seconds=40)                       # 2026-11-02 00:00:30 UTC
    assert env.run(idx=1)["state"] == "ok"
    assert env.budget.find_one({"_id": "day:2026-11-01"})["count"] == 1
    assert env.budget.find_one({"_id": "day:2026-11-02"})["count"] == 1
    assert len(env.calls) == 2


def test_budget_database_failure_fails_closed_without_a_call(on):
    env = Env()

    class Broken:
        def find_one_and_update(self, *a, **k):
            raise RuntimeError("db down")
    env.deps.budget = Broken()
    assert env.run() == {"state": "none"} and env.calls == []
    assert env.stored()["state"] == "skipped"
    assert env.run() == {"state": "none"} and env.calls == []


def test_claim_database_failure_fails_closed_without_a_call_or_budget(on):
    env = Env()
    real = env.chats

    class Flaky:
        def find_one(self, *a, **k):
            return real.find_one(*a, **k)

        def update_one(self, *a, **k):
            raise RuntimeError("db down")
    env.deps.chats = Flaky()
    assert env.run()["state"] == "unavailable" and env.calls == [] and env.counter() is None


def test_cannot_record_the_call_start_means_no_call_and_the_slot_stays_counted(on):
    env = Env()
    real = env.chats
    seen = {"n": 0}

    class FailSecondWrite:
        def find_one(self, *a, **k):
            return real.find_one(*a, **k)

        def update_one(self, flt, upd, *a, **k):
            seen["n"] += 1
            if seen["n"] == 2:                              # the claim (1) lands, the "calling" mark (2) does not
                raise RuntimeError("db down")
            return real.update_one(flt, upd, *a, **k)
    env.deps.chats = FailSecondWrite()
    assert env.run()["state"] == "unavailable" and env.calls == []
    assert env.counter()["count"] == 1                      # at-least-once accounting: the slot is never given back
    env.deps.chats = real
    assert env.run()["state"] in ("pending", "unavailable") and env.calls == []     # the claim is never re-taken


def test_pending_then_stale_claims_are_never_retaken(on):
    old = NOW - timedelta(minutes=10)
    live = {"claim_id": "x", "state": "calling", "claimed_at": NOW - timedelta(seconds=5), "day": "2026-11-01"}
    env = Env([message(0, live_reddit=live), message(1, live_reddit=dict(live, claimed_at=old)),
               message(2, live_reddit=dict(live, state="claiming", claimed_at=old))])
    assert env.run(idx=0) == {"state": "pending"}
    assert env.run(idx=1) == {"state": "unavailable"}
    assert env.run(idx=2) == {"state": "unavailable"}
    env.clock.t = NOW + timedelta(hours=1)
    assert env.run(idx=1) == {"state": "unavailable"} and env.calls == [] and env.counter() is None


def test_missing_api_key_makes_no_claim_and_uses_no_budget(on, monkeypatch):
    monkeypatch.setattr(rl.config, "OPENAI_API_KEY", "", raising=False)
    env = Env()
    assert env.run()["state"] == "unavailable"
    assert env.calls == [] and env.stored() is None and env.counter() is None


def test_result_not_stored_if_claim_changed_underneath(on):
    env = Env()
    first_post = env._post

    def post_then_steal(url, headers, payload, timeout):
        out = first_post(url, headers, payload, timeout)
        env.chats.update_one({"chat_id": "c1"}, {"$set": {"messages.0.live_reddit.claim_id": "someone-else"}})
        return out
    env.deps.post = post_then_steal
    env.run()
    assert env.stored()["claim_id"] == "someone-else" and env.stored()["state"] == "calling"
    assert len(env.calls) == 1


# ── isolation: nothing here reaches the answer, the evidence, credits or integrity ──────────────────────────────────────
def test_only_routes_imports_reddit_live_and_the_module_imports_no_billing_or_answer_code():
    importers = []
    for py in ROOT.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        if py.name != "reddit_live.py" and ("import reddit_live" in text or "from reddit_live" in text):
            importers.append(py.name)
    # routes.py serves the endpoint; active_intent.py is the active-lead-intent workflow that reuses the same safety controls
    assert sorted(importers) == ["active_intent.py", "routes.py"]
    src = (ROOT / "reddit_live.py").read_text(encoding="utf-8")
    for forbidden in ("search_credits", "credit_cost", "billing", "import logics", "import index", "import flintel",
                      "google_search", "analyze_with_claude", "save_signal_results", "enforce_answer_integrity"):
        assert forbidden not in src, forbidden


def test_protected_modules_source_never_mentions_the_feature():
    for name in ("answer_integrity.py", "search_credits.py", "credit_cost.py", "google.py", "flintel.py", "usage_meter.py",
                 "logics.py", "index.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "reddit_live" not in text and "live_reddit" not in text, name
