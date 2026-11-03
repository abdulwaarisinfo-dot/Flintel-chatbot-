"""
tests/test_google_recent_demand.py — the Google (RapidAPI) Reddit discovery request carries `tbs=qdr:d` ONLY for a user prompt the
Active Intent classifier decided is "demand" (and only when the user gave no time range of their own). Everything else about the
request, the keyword batching, the request count and the `flintel_google_posts` documents is unchanged.

All mocked: no network, no RapidAPI request, no OpenAI call, no production database. What these tests can NOT show: that the real
`google-search116` host honours `tbs=qdr:d` (that is the provider's documented behaviour, taken as given) or what it returns.
"""
import copy
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

mongomock = pytest.importorskip("mongomock")
import active_intent as ain  # noqa: E402
import google  # noqa: E402
import intent_classifier as ic  # noqa: E402
import timerange  # noqa: E402
from tests.test_active_intent import cls_resp  # noqa: E402
from tests.test_active_intent_window import bg, END  # noqa: E402,F401  (fixture: the real background path)
from tests.test_active_intent_workflow import CRM, EMAIL, World, run_stream, wf  # noqa: E402,F401  (the real stream path)
from tests.test_reddit_live import on  # noqa: E402,F401
from tests.test_reliability_ux_perf import app_env, env  # noqa: E402,F401

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def _clean():
    yield
    ain.DEPS_FACTORY = None
    ic.DEPS_FACTORY = None


class FakeHttp:
    """Stands in for httpx.Client: records every GET (url, headers, params) and answers with `payload`."""
    def __init__(self, payload):
        self.payload, self.requests = payload, []

    def __call__(self, *a, **k):
        outer = self

        class _Client:
            def __enter__(self_):
                return self_

            def __exit__(self_, *exc):
                return False

            def get(self_, url, headers=None, params=None):
                outer.requests.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})

                class _R:
                    status_code = 200

                    def raise_for_status(_):
                        return None

                    def json(_):
                        return outer.payload
                return _R()
        return _Client()


def reddit(n):
    return {"link": f"https://www.reddit.com/r/crm/comments/id{n}/post_{n}/", "position": n}


PAYLOAD = {"results": [reddit(1), reddit(2), {"link": "https://example.com/x"}]}


@pytest.fixture
def http(monkeypatch):
    f = FakeHttp(PAYLOAD)
    monkeypatch.setattr(google, "RAPIDAPI_KEY", "test-key-not-real")
    monkeypatch.setattr(google.httpx, "Client", f)
    import usage_meter                                   # the real one would try to write a usage row to a database
    monkeypatch.setattr(usage_meter, "record_external", lambda *a, **k: None)
    return f


# ── the request itself ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_default_request_parameters_are_exactly_the_existing_ones(http):
    google._call_rapidapi_google_search("site:reddit.com crm")
    assert http.requests[0]["params"] == {"query": "site:reddit.com crm", "depth": google.GOOGLE_SEARCH_DEPTH}
    assert http.requests[0]["url"] == f"https://{google.RAPIDAPI_GOOGLE_HOST}/"


def test_recent_day_adds_only_tbs_and_changes_nothing_else(http):
    google._call_rapidapi_google_search("site:reddit.com crm")
    google._call_rapidapi_google_search("site:reddit.com crm", recent_day=True)
    base, recent = http.requests
    assert recent["params"] == dict(base["params"], tbs="qdr:d")
    assert recent["url"] == base["url"] and recent["headers"] == base["headers"]


@pytest.mark.parametrize("value", [False, None, 1, "true", "yes"])
def test_only_the_literal_true_adds_tbs(http, value):
    google._call_rapidapi_google_search("q", recent_day=value)
    assert "tbs" not in http.requests[0]["params"]


# ── batching, request count, stored documents ─────────────────────────────────────────────────────────────────────────────
KW = ["crm", "sales crm", "lead tool", "pipeline", "hubspot"]          # 5 keywords, 2 per batch -> 3 requests


def _run(http, recent):
    coll = mongomock.MongoClient()["t"]["flintel_google_posts"]
    http.requests.clear()
    kwargs = {"recent_day": True} if recent else {}
    stubs = google.search_google_for_reddit_posts(KW, coll, search_keyword_for_storage="find crm", **kwargs)
    return coll, stubs, list(http.requests)


def test_request_count_queries_and_batching_are_identical_with_and_without_the_filter(http):
    _c1, _s1, normal = _run(http, False)
    _c2, _s2, recent = _run(http, True)
    assert len(normal) == len(recent) == 3                                      # ONE call per keyword batch, no extra request for recency
    assert [r["params"]["query"] for r in normal] == [r["params"]["query"] for r in recent] == [
        "site:reddit.com crm sales crm", "site:reddit.com lead tool pipeline", "site:reddit.com hubspot"]
    assert all("tbs" not in r["params"] for r in normal)
    assert all(r["params"].get("tbs") == "qdr:d" for r in recent)
    assert [{k: v for k, v in r["params"].items() if k != "tbs"} for r in recent] == [r["params"] for r in normal]


def test_flintel_google_posts_documents_are_identical_with_and_without_the_filter(http):
    c1, s1, _ = _run(http, False)
    c2, s2, _ = _run(http, True)

    def shape(coll):
        docs = sorted(coll.find({}, {"_id": 0}), key=lambda d: d["post_url"])
        for d in docs:
            d.pop("discovered_at")
            d["fuzzy_keywords"] = sorted(d["fuzzy_keywords"])
        return docs
    assert shape(c1) == shape(c2) and c1.count_documents({}) == 2                # deduplicated by post_url across the three batches
    for d in c2.find({}, {"_id": 0}):
        assert d["reddit_fetched"] is False and d["fetched_at"] is None and d["next_retry_at"] is None
        assert set(d) == {"post_url", "discovered_at", "fetched_at", "fuzzy_keywords", "fuzzy_matched", "google_rank", "next_retry_at",
                          "reddit_fetched", "search_keyword", "subreddit"}        # no new field, and no publication timestamp invented
    assert [s["post_url"] for s in s1] == [s["post_url"] for s in s2]


def test_an_existing_stub_is_still_never_reset(http):
    coll = mongomock.MongoClient()["t"]["flintel_google_posts"]
    url = reddit(1)["link"]
    coll.insert_one({"post_url": url, "reddit_fetched": True, "fetched_at": "kept", "fuzzy_keywords": ["old"]})
    google.search_google_for_reddit_posts(KW, coll, recent_day=True)
    got = coll.find_one({"post_url": url}, {"_id": 0})
    assert got["reddit_fetched"] is True and got["fetched_at"] == "kept" and set(KW) <= set(got["fuzzy_keywords"]) and "old" in got["fuzzy_keywords"]


# ── the trigger: which messages get the filter ────────────────────────────────────────────────────────────────────────────
def _window_doc():
    return ain.recent_window(END, 1500).to_doc(END)


def _user_range_doc():
    return timerange.TimeRange(start=END - timedelta(days=3), end=END, tz_name="UTC", kind="rolling", phrase="last 3 days").to_doc(END)


@pytest.fixture
def spy(env, monkeypatch):
    lg, idx, _old = env
    calls = []
    monkeypatch.setattr(idx.google_search, "search_google_for_reddit_posts", lambda *a, **k: calls.append((a, k)) or [], raising=False)
    monkeypatch.setattr(idx, "_turn_stopped_now", lambda *a, **k: False, raising=False)
    return idx, calls


def _msg(time_range):
    return {"topic_key": "find crm", "keywords": ["crm"], "request_id": "r1", "time_range": copy.deepcopy(time_range)}


def test_the_trigger_adds_the_filter_only_for_demand_with_the_active_intent_window(spy):
    idx, calls = spy
    idx._trigger_google_fallback_search("c1", EMAIL, _msg(_window_doc()), True)
    assert calls[-1][1] == {"search_keyword_for_storage": "find crm", "recent_day": True}


@pytest.mark.parametrize("label,args,tr", [
    ("no flag (normal search / discussion / neither)", (), "window"),
    ("flag false", (False,), "window"),
    ("flag not literally True", ("yes",), "window"),
    ("demand but the user gave a range", (True,), "user"),
    ("demand but no range at all", (True,), None),
])
def test_every_other_case_calls_google_exactly_as_before(spy, label, args, tr):
    idx, calls = spy
    doc = {"window": _window_doc(), "user": _user_range_doc(), None: None}[tr]
    idx._trigger_google_fallback_search("c1", EMAIL, _msg(doc), *args)
    assert calls[-1][1] == {"search_keyword_for_storage": "find crm"}, label     # no recent_day key at all


# ── the classification really is passed through: background path ─────────────────────────────────────────────────────────
def _bg_run(bg, monkeypatch, decision, my_range=None):
    lg, idx, chats = bg
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", 1500, raising=False)
    monkeypatch.setattr(ic, "decide_full", lambda **kw: decision)
    fi = sys.modules["flintel"]
    firsts = iter([True])
    monkeypatch.setattr(fi, "should_trigger_immediately", lambda *a, **k: next(firsts, False), raising=False)   # the Google trigger only
    monkeypatch.setattr(idx, "mark_google_fallback_triggered", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(idx, "_turn_stopped_now", lambda *a, **k: False, raising=False)
    calls = []
    monkeypatch.setattr(idx.google_search, "search_google_for_reddit_posts", lambda *a, **k: calls.append((a, k)) or [], raising=False)
    monkeypatch.setattr(idx, "get_evidence_with_topup", lambda **kw: [])
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: "{}")
    monkeypatch.setattr(idx, "_complete_message_answer_and_results", lambda *a, **k: None)
    owner = EMAIL
    idx.add_search_to_chat("c1", owner, CRM, lg.normalize_topic_key(CRM), ["crm"], "reddit", time_range=my_range)
    chat = chats.find_one({"chat_id": "c1", "owner_key": owner})
    chat["messages"][-1].update(evidence_required=15, request_id="req-1", credit_request_id="cr-1")
    idx._fill_in_message_outputs("c1", owner, chat["messages"])
    return calls


def test_background_demand_gets_the_filter(bg, monkeypatch):
    calls = _bg_run(bg, monkeypatch, ("demand", "active_demand"))
    assert len(calls) == 1 and calls[0][1].get("recent_day") is True


@pytest.mark.parametrize("decision", [("discussion", "x"), (False, "not_detected")])
def test_background_discussion_and_neither_do_not_get_the_filter(bg, monkeypatch, decision):
    calls = _bg_run(bg, monkeypatch, decision)
    assert len(calls) == 1 and "recent_day" not in calls[0][1]


def test_background_demand_with_a_user_range_does_not_get_the_filter(bg, monkeypatch):
    calls = _bg_run(bg, monkeypatch, ("demand", "active_demand"), my_range=_user_range_doc())
    assert len(calls) == 1 and "recent_day" not in calls[0][1]


# ── the stream path ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _stream_run(wf, monkeypatch, intent, msg_over=None):
    lg, idx, rt, chats, state = wf
    world = World("s1")
    world.live.cls_override = cls_resp(intent=intent)
    triggered = []
    # the shared app_env fixture turns the immediate Google trigger off; here it fires once (the progress trigger's flag is already True)
    monkeypatch.setattr(sys.modules["flintel"], "should_trigger_immediately", lambda flag, *a, **k: not flag, raising=False)
    monkeypatch.setattr(rt, "_trigger_google_fallback_search", lambda *a, **k: triggered.append(a))
    monkeypatch.setattr(rt, "mark_google_fallback_triggered", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_WINDOW_MINUTES", 1500, raising=False)
    run_stream(wf, monkeypatch, world, msg_over=dict({"google_fallback_triggered": False}, **(msg_over or {})))
    deadline = time.monotonic() + 3
    while not triggered and time.monotonic() < deadline:
        time.sleep(0.05)
    return triggered


def test_stream_demand_passes_the_flag(wf, monkeypatch):
    triggered = _stream_run(wf, monkeypatch, "demand")
    assert len(triggered) == 1 and triggered[0][-1] is True and len(triggered[0]) == 4


@pytest.mark.parametrize("intent", ["discussion", "neither"])
def test_stream_discussion_and_neither_call_the_trigger_exactly_as_before(wf, monkeypatch, intent):
    triggered = _stream_run(wf, monkeypatch, intent)
    assert len(triggered) == 1 and len(triggered[0]) == 3                      # (chat_id, owner_key, msg): no extra argument


def test_stream_normal_search_is_unchanged_when_active_intent_is_off(wf, monkeypatch):
    monkeypatch.setattr(ain.config, "ACTIVE_INTENT_ENABLED", False, raising=False)
    triggered = _stream_run(wf, monkeypatch, "demand")
    assert len(triggered) == 1 and len(triggered[0]) == 3


# ── other callers ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_scheduler_call_does_not_pass_the_filter():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "scheduler.py").read_text(encoding="utf-8")
    assert "search_google_for_reddit_posts" in src and "recent_day" not in src      # the scheduler's search is exactly as before


def test_the_filter_is_requested_nowhere_else_in_the_source():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    users = {p.name for p in root.glob("*.py") if "recent_day" in p.read_text(encoding="utf-8", errors="ignore")}
    assert users == {"google.py", "index.py"}            # routes.py reaches it through the trigger's positional argument only
