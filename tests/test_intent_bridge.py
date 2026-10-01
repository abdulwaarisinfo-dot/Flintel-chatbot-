"""
Tests for intent_bridge.py (+ one regression test for logics.get_matched_signals).

Koi asal Claude/Mongo call nahi hoti:
  * intent_prototype.{query_interpreter, doc_classifier, opportunity, ranker}
    FAKE modules se replace hote hain (sys.modules mein) — Claude kabhi nahi chalta.
  * classification_cache: zyada tar tests mein in-memory fake; test 10 mein
    ASAL cache module, lekin uska Mongo collection toota hua (mock) hai.
  * logics test: `database` module fake hai (MagicMock collections).

NOTE: fake prototype modules intent_bridge ke adapter contract par chalte hain
(interpret_query(q) -> intent, classify_batch(intent, docs) -> [cls],
cls = {"passes": bool, "confidence": float}). Asli intent_prototype ke
function naam/signature in tests mein verify NAHI hote.
"""
import importlib
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import intent_bridge as ib  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fakes
# ─────────────────────────────────────────────────────────────────────────────
def idx_of(doc):
    return int(doc["post_url"].rsplit("/", 1)[1])


def make_candidates(n):
    return [
        {"title": f"title {i}", "post_text": f"text {i}",
         "post_url": f"https://example.com/{i}", "platform": "reddit" if i % 2 else "x"}
        for i in range(n)
    ]


class Proto:
    """Controllable state behind the fake prototype modules."""
    def __init__(self):
        self.lock = threading.Lock()
        self.interpret_error = None
        self.classify_error = None
        self.classify_delay = 0.0
        self.classified_urls = []              # har doc jo classifier ko bheja gaya
        self.pass_fn = lambda i: True          # idx -> bool
        self.confidence_fn = lambda i: 0.9     # idx -> float


class FakeCache:
    def __init__(self):
        self.store = {}
        self.saved = []
        self.get_error = None

    def get_many(self, urls):
        if self.get_error:
            raise self.get_error
        return {u: self.store[u] for u in urls if u in self.store}

    def save_many(self, items):
        self.saved.extend(items)
        self.store.update(dict(items))


def _module(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    return m


@pytest.fixture
def proto(monkeypatch):
    p = Proto()

    def interpret_query(q):
        if p.interpret_error:
            raise p.interpret_error
        return {"goal": "find buyers", "query": q}

    def classify_batch(intent, docs):
        if p.classify_delay:
            time.sleep(p.classify_delay)
        if p.classify_error:
            raise p.classify_error
        with p.lock:
            p.classified_urls.extend(d["post_url"] for d in docs)
        return [{"passes": bool(p.pass_fn(idx_of(d))), "confidence": p.confidence_fn(idx_of(d))}
                for d in docs]

    cache = FakeCache()
    pkg = _module("intent_prototype")
    pkg.__path__ = []
    mods = {
        "intent_prototype": pkg,
        "intent_prototype.query_interpreter": _module("intent_prototype.query_interpreter", interpret_query=interpret_query),
        "intent_prototype.doc_classifier": _module("intent_prototype.doc_classifier", classify_batch=classify_batch),
        "intent_prototype.opportunity": _module("intent_prototype.opportunity"),   # koi function nahi -> bridge ka "passes" key fallback
        "intent_prototype.ranker": _module("intent_prototype.ranker"),             # koi function nahi -> built-in ranking
        "intent_prototype.classification_cache": cache_module(cache),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
        if name != "intent_prototype":
            setattr(pkg, name.split(".")[1], mod)

    monkeypatch.setattr(config, "INTENT_BRIDGE_ENABLED", True)
    monkeypatch.setattr(config, "INTENT_CACHE_ENABLED", True)
    ib._interpret.cache_clear()
    ib._warned_once.clear()
    p.cache = cache
    yield p
    ib._interpret.cache_clear()


def cache_module(cache):
    return _module("intent_prototype.classification_cache",
                   get_many=cache.get_many, save_many=cache.save_many)


FAST = {"timeout": 10}   # tests ko hang hone se bachane ke liye


# ─────────────────────────────────────────────────────────────────────────────
# 1. Flag off
# ─────────────────────────────────────────────────────────────────────────────
def test_flag_off_returns_candidates_untouched(proto, monkeypatch):
    monkeypatch.setattr(config, "INTENT_BRIDGE_ENABLED", False)
    cands = make_candidates(30)
    snapshot = list(cands)
    out = ib.rerank_with_intent("find buyers", cands, 10)
    assert out is cands
    assert [id(x) for x in out] == [id(x) for x in snapshot]
    assert proto.classified_urls == []          # classifier kabhi nahi chala


# ─────────────────────────────────────────────────────────────────────────────
# 2. Khali candidates
# ─────────────────────────────────────────────────────────────────────────────
def test_empty_candidates_returns_empty(proto):
    assert ib.rerank_with_intent("q", [], 10) == []
    assert proto.classified_urls == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. Interpreter fail
# ─────────────────────────────────────────────────────────────────────────────
def test_interpreter_failure_returns_candidates(proto):
    proto.interpret_error = RuntimeError("claude down")
    cands = make_candidates(20)
    out = ib.rerank_with_intent("q", cands, 10, config_overrides=FAST)   # exception bahar nahi aani chahiye
    assert out is cands
    assert proto.classified_urls == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. Classifier fail
# ─────────────────────────────────────────────────────────────────────────────
def test_classifier_failure_returns_candidates_in_original_order(proto):
    proto.classify_error = RuntimeError("classifier exploded")
    cands = make_candidates(20)
    out = ib.rerank_with_intent("q", cands, 10, config_overrides=FAST)
    assert out == cands[:10]                                   # purane matching jaisa: top-N, same order
    assert all(a is b for a, b in zip(out, cands[:10]))        # same objects


# ─────────────────────────────────────────────────────────────────────────────
# 5. Timeout
# ─────────────────────────────────────────────────────────────────────────────
def test_timeout_returns_candidates(proto):
    proto.classify_delay = 1.0
    cands = make_candidates(20)
    t0 = time.monotonic()
    out = ib.rerank_with_intent("q", cands, 10, config_overrides={"timeout": 0.2})
    elapsed = time.monotonic() - t0
    assert out is cands
    assert elapsed < 0.9, f"timeout par {elapsed:.2f}s lage — bridge ne intezar kiya"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Return format
# ─────────────────────────────────────────────────────────────────────────────
def test_return_items_keep_required_keys_and_identity(proto):
    proto.pass_fn = lambda i: i % 4 == 0
    cands = make_candidates(30)
    out = ib.rerank_with_intent("q", cands, 12, config_overrides=FAST)
    assert len(out) == 12
    by_url = {c["post_url"]: c for c in cands}
    for item in out:
        for key in ("title", "post_text", "post_url", "platform"):
            assert key in item and item[key] is not None
        assert item is by_url[item["post_url"]]                 # wahi dict object, copy nahi
        assert set(item) == {"title", "post_text", "post_url", "platform"}   # koi extra key nahi (default)


def test_attach_intent_is_opt_in(proto):
    proto.pass_fn = lambda i: i < 3
    cands = make_candidates(10)
    out = ib.rerank_with_intent("q", cands, 5, config_overrides={**FAST, "attach_intent": True})
    assert "_intent" in out[0]
    assert all(k in out[0] for k in ("title", "post_text", "post_url", "platform"))
    assert all("_intent" not in c for c in cands)               # input mutate nahi hua


# ─────────────────────────────────────────────────────────────────────────────
# 7. Fill to N
# ─────────────────────────────────────────────────────────────────────────────
def test_fill_to_n_intent_first_then_similarity(proto):
    passing = {5, 17, 20, 28}
    proto.pass_fn = lambda i: i in passing
    cands = make_candidates(30)
    out = ib.rerank_with_intent("q", cands, 10, config_overrides=FAST)
    assert len(out) == 10                                       # purane matching jitni (min(N, len(candidates)))
    assert [idx_of(x) for x in out[:4]] == [5, 17, 20, 28]      # intent wali pehle
    expected_fill = [c for c in cands if idx_of(c) not in passing][:6]
    assert out[4:] == expected_fill                             # phir similarity order
    assert len({x["post_url"] for x in out}) == 10              # koi duplicate nahi


def test_fill_to_n_when_fewer_candidates_than_n(proto):
    proto.pass_fn = lambda i: i == 1
    cands = make_candidates(6)
    out = ib.rerank_with_intent("q", cands, 10, config_overrides=FAST)
    assert len(out) == 6 and idx_of(out[0]) == 1


def test_no_intent_matches_equals_old_behavior(proto):
    proto.pass_fn = lambda i: False
    cands = make_candidates(30)
    assert ib.rerank_with_intent("q", cands, 10, config_overrides=FAST) == cands[:10]


def test_intent_posts_ranked_by_confidence(proto):
    proto.pass_fn = lambda i: i in (3, 7, 11)
    proto.confidence_fn = lambda i: {3: 0.75, 7: 0.95, 11: 0.85}.get(i, 0.1)
    out = ib.rerank_with_intent("q", make_candidates(20), 5, config_overrides=FAST)
    assert [idx_of(x) for x in out[:3]] == [7, 11, 3]


# ─────────────────────────────────────────────────────────────────────────────
# 8. Short-circuit
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pass_fn,conf,expected_classified", [
    (lambda i: i % 3 == 0, 0.9, 50),    # head mein 17 confident pass -> ruk jao
    (lambda i: i < 15, 0.9, 50),        # theek 15 pass -> ruk jao (>= min_passing)
    (lambda i: i < 14, 0.9, 120),       # 14 pass -> aage classify
    (lambda i: i % 10 == 0, 0.9, 120),  # sirf 5 pass -> sab classify
    (lambda i: i % 3 == 0, 0.5, 120),   # pass hain par confidence < 0.70 -> gine nahi jate
])
def test_short_circuit(proto, pass_fn, conf, expected_classified):
    proto.pass_fn = pass_fn
    proto.confidence_fn = lambda i: conf
    cands = make_candidates(120)
    ib.rerank_with_intent("q", cands, 20, config_overrides=FAST)
    assert len(proto.classified_urls) == expected_classified
    if expected_classified == 50:
        assert set(proto.classified_urls) == {c["post_url"] for c in cands[:50]}


# ─────────────────────────────────────────────────────────────────────────────
# 9. Cache hit => Claude/classifier call nahi
# ─────────────────────────────────────────────────────────────────────────────
def test_cached_posts_are_not_reclassified(proto):
    cands = make_candidates(40)
    cached = cands[:10]
    for c in cached:
        proto.cache.store[c["post_url"]] = {"passes": True, "confidence": 0.9}
    proto.pass_fn = lambda i: False
    out = ib.rerank_with_intent("q", cands, 12, config_overrides=FAST)
    assert not (set(proto.classified_urls) & {c["post_url"] for c in cached})
    assert set(proto.classified_urls) == {c["post_url"] for c in cands[10:]}
    assert [idx_of(x) for x in out[:10]] == list(range(10))     # cache wali (pass) posts upar
    assert {u for u, _ in proto.cache.saved} == set(proto.classified_urls)   # sirf nayi save hui


def test_second_run_is_fully_served_from_cache(proto):
    proto.pass_fn = lambda i: i % 5 == 0
    cands = make_candidates(40)
    first = ib.rerank_with_intent("q", cands, 10, config_overrides=FAST)
    calls_after_first = len(proto.classified_urls)
    assert calls_after_first == 40
    second = ib.rerank_with_intent("q", cands, 10, config_overrides=FAST)
    assert len(proto.classified_urls) == calls_after_first      # top-up/dobara search par Claude nahi
    assert second == first


# ─────────────────────────────────────────────────────────────────────────────
# 10. Cache fail (Mongo error) — ASAL classification_cache module, toota Mongo
# ─────────────────────────────────────────────────────────────────────────────
def _load_real_cache(monkeypatch):
    path = ROOT / "intent_prototype" / "classification_cache.py"
    if not path.exists():
        pytest.skip("intent_prototype/classification_cache.py repo mein nahi mili")
    spec = importlib.util.spec_from_file_location("intent_prototype.classification_cache", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._collection = None
    mod._index_ready = False
    return mod


@pytest.mark.parametrize("failure", ["no_connection", "ops_raise"])
def test_cache_mongo_failure_does_not_break_bridge(proto, monkeypatch, failure):
    cc = _load_real_cache(monkeypatch)
    if failure == "no_connection":
        def broken():
            raise ConnectionError("mongo unreachable")
        monkeypatch.setattr(cc, "_get_collection", broken)
    else:
        col = MagicMock()
        col.find.side_effect = RuntimeError("mongo down (find)")
        col.bulk_write.side_effect = RuntimeError("mongo down (write)")
        monkeypatch.setattr(cc, "_get_collection", lambda: col)

    # module level: get_many khali dict, save_many chupke se guzar jaye
    assert cc.get_many(["a", "b"]) == {}
    assert cc.save_many([("a", {"passes": True})]) is None

    # bridge level: asal (toota hua) cache install karo
    monkeypatch.setitem(sys.modules, "intent_prototype.classification_cache", cc)
    proto.pass_fn = lambda i: i in (4, 9)
    cands = make_candidates(20)
    out = ib.rerank_with_intent("q", cands, 6, config_overrides=FAST)
    assert [idx_of(x) for x in out[:2]] == [4, 9]               # bridge phir bhi sahi rerank karta hai
    assert len(out) == 6
    assert len(proto.classified_urls) == 20                     # cache na hone par sab classify hui


# ─────────────────────────────────────────────────────────────────────────────
# get_query_intent_summary
# ─────────────────────────────────────────────────────────────────────────────
def test_intent_summary(proto, monkeypatch):
    s = ib.get_query_intent_summary("find buyers")
    assert isinstance(s, dict) and s["goal"] == "find buyers"
    ib._interpret.cache_clear()
    proto.interpret_error = RuntimeError("down")
    assert ib.get_query_intent_summary("another query") is None
    monkeypatch.setattr(config, "INTENT_BRIDGE_ENABLED", False)
    assert ib.get_query_intent_summary("find buyers") is None


# ─────────────────────────────────────────────────────────────────────────────
# logics.get_matched_signals — flag off => purane aur naye signature par wahi natija
# ─────────────────────────────────────────────────────────────────────────────
def _stub_module(name, **attrs):
    try:
        return importlib.import_module(name)
    except Exception:
        return _module(name, **attrs)


@pytest.fixture
def logics_mod(monkeypatch):
    path = ROOT / "logics.py"
    if not path.exists():
        pytest.skip("logics.py repo root mein nahi mili — yeh test chala hi nahi")
    fake_db = _module("database", **{n: MagicMock(name=n) for n in (
        "jobs_collection", "signals_collection", "signals_collection_2", "signals_collection_4",
        "google_posts_collection", "topic_evidence_cache_collection", "website_evidence_cache_collection")})
    monkeypatch.setitem(sys.modules, "database", fake_db)           # asal Mongo kabhi nahi
    for name in ("logics",):
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name, attrs in (("flintel", {"ROUTER_UNFILTERED_ADDENDUM": "", "GENERIC_PAIN_POINT_INFERENCE_ADDENDUM": ""}),
                        ("website_intelligence", {}), ("google", {}), ("httpx", {})):
        monkeypatch.setitem(sys.modules, name, _stub_module(name, **attrs))
    mod = importlib.import_module("logics")
    yield mod
    sys.modules.pop("logics", None)


def test_get_matched_signals_flag_off_old_and_new_signature_identical(logics_mod, monkeypatch):
    monkeypatch.setattr(config, "INTENT_BRIDGE_ENABLED", False)
    monkeypatch.setattr(logics_mod, "INTENT_BRIDGE_ENABLED", False, raising=False)
    monkeypatch.setattr(logics_mod, "generate_query_embeddings_batch", lambda texts: [[1.0, 0.0]])

    docs = [{"title": f"t{i}", "post_text": f"body {i}", "post_url": f"https://example.com/{i}",
             "platform": "reddit" if i % 2 else "twitter", "embedding": [1.0, 0.001 * i]} for i in range(30)]
    col = MagicMock()
    col.find.return_value.sort.return_value.limit.return_value = docs
    monkeypatch.setattr(logics_mod, "signals_collection", col)

    rerank_calls = []
    monkeypatch.setitem(sys.modules, "intent_bridge",
                        _module("intent_bridge", rerank_with_intent=lambda *a, **k: rerank_calls.append(a)))

    kwargs = dict(targeting_platform="all", limit=12, match_phrases=["people looking for a solution like this"])

    col.find.reset_mock()
    old_style = logics_mod.get_matched_signals("topic", ["kw one", "kw two"], **kwargs)           # user_query nahi
    finds_old = col.find.call_count
    col.find.reset_mock()
    none_query = logics_mod.get_matched_signals("topic", ["kw one", "kw two"], user_query=None, **kwargs)
    finds_none = col.find.call_count
    col.find.reset_mock()
    with_query = logics_mod.get_matched_signals("topic", ["kw one", "kw two"], user_query="find buyers", **kwargs)
    finds_with = col.find.call_count

    assert old_style, "test data se kuch match hona chahiye tha"
    assert old_style == none_query == with_query
    assert finds_old == finds_none == finds_with                 # flag off: koi extra Mongo read nahi
    assert rerank_calls == []                                    # bridge kabhi call nahi hua
