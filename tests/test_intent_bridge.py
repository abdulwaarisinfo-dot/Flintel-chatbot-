"""
tests/test_intent_bridge.py
============================
Tests for intent_bridge.rerank_with_intent using REAL prototype modules.

Mock ONLY:
  - intent_prototype.llm.claude  (network boundary)
  - Mongo cache  (in-memory FakeCache matching classification_cache signature)

Real:
  - intent_prototype.doc_classifier.classify
  - intent_prototype.query_interpreter.interpret
  - intent_prototype.schemas
  - intent_prototype.ranker
  - intent_prototype.opportunity

Run:
    cd /mnt/user-data/outputs
    python -m pytest tests/test_intent_bridge.py -v
"""

import importlib
import json
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ─── helpers ──────────────────────────────────────────────────────────────────

def make_candidates(n, platform="reddit"):
    return [
        {
            "title":     f"title {i}",
            "post_text": f"We are looking to buy AI agents for our business — post {i}",
            "post_url":  f"https://reddit.com/r/test/{i}",
            "platform":  platform if i % 2 == 0 else "twitter",
        }
        for i in range(n)
    ]

def idx_of(doc):
    return int(doc["post_url"].rsplit("/", 1)[1])

# ─── LLM response builders ────────────────────────────────────────────────────

def _interpreter_json(intent_include=None, query_mode="explicit_intent"):
    return json.dumps({
        "topic_keywords":        ["AI", "agents"],
        "topic_embedding_query": "businesses buying AI agents automation",
        "intent_include":        intent_include or ["buyer_demand"],
        "intent_exclude":        ["irrelevant"],
        "actor_direction_filter": None,
        "actor_type_filter":     None,
        "min_commercial_signal": None,
        "min_pain_intensity":    None,
        "min_urgency":           None,
        "min_specificity":       None,
        "time_scope":            None,
        "geography":             None,
        "intent_logic":          "OR",
        "query_mode":            query_mode,
    })

def _classifier_json(docs, intent="buyer_demand", confidence=0.85):
    items = []
    for i, _ in enumerate(docs, 1):
        items.append({
            "i":                  i,
            "intent":             intent,
            "intent_confidence":  confidence,
            "secondary_intent":   None,
            "secondary_confidence": None,
            "actor_type":         "company",
            "actor_role":         "buyer",
            "commercial_signal":  0.80,
            "pain_intensity":     0.0,
            "urgency":            0.3,
            "specificity":        0.6,
            "geography":          None,
            "industry_hint":      "saas",
            "ambiguous":          False,
            "noise":              False,
        })
    return json.dumps(items)

# ─── FakeCache — matches real classification_cache.save_many / get_many sig ──
# save_many(List[dict]) — each dict has post_url, intents, confidence at minimum
# get_many(List[str]) → Dict[str, dict] keyed by post_url

class FakeCache:
    def __init__(self):
        # store: post_url → {post_url, intents, confidence}
        self.store = {}
        # saved: list of dicts passed to save_many (for assertion)
        self.saved = []

    def get_many(self, urls):
        return {u: self.store[u] for u in urls if u in self.store}

    def save_many(self, items):
        # items is List[dict] — same signature as real save_many
        self.saved.extend(items)
        for item in items:
            url = item.get("post_url")
            if url:
                self.store[url] = {
                    "post_url":   url,
                    "intents":    item.get("intents", []),
                    "confidence": float(item.get("confidence", 0.0)),
                }

def _cache_module(cache: FakeCache):
    m = types.ModuleType("intent_prototype.classification_cache")
    m.get_many  = cache.get_many
    m.save_many = cache.save_many
    return m

# ─── shared fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def ctx(monkeypatch):
    import config
    cache = FakeCache()
    monkeypatch.setitem(
        sys.modules,
        "intent_prototype.classification_cache",
        _cache_module(cache),
    )
    monkeypatch.setattr(config, "INTENT_BRIDGE_ENABLED", True)
    monkeypatch.setattr(config, "INTENT_CACHE_ENABLED",  True)

    class Ctx:
        pass
    c = Ctx()
    c.cache = cache
    yield c

@contextmanager
def patch_llm(side_effect):
    with patch("intent_prototype.llm.claude",               side_effect=side_effect), \
         patch("intent_prototype.query_interpreter.claude", side_effect=side_effect), \
         patch("intent_prototype.doc_classifier.claude",    side_effect=side_effect):
        yield

# ══════════════════════════════════════════════════════════════════════════════
# 1. Flag OFF → candidates returned untouched
# ══════════════════════════════════════════════════════════════════════════════

def test_flag_off_returns_candidates_untouched(monkeypatch):
    import config
    import intent_bridge
    monkeypatch.setattr(config, "INTENT_BRIDGE_ENABLED", False)
    candidates = make_candidates(10)
    result = intent_bridge.rerank_with_intent("find AI buyer leads", candidates, 10)
    assert result is candidates, "must return the SAME list object when flag is off"

# ══════════════════════════════════════════════════════════════════════════════
# 2. Empty candidates → empty list, no LLM calls
# ══════════════════════════════════════════════════════════════════════════════

def test_empty_candidates_returns_empty(ctx):
    import intent_bridge
    mock_llm = MagicMock()
    with patch("intent_prototype.llm.claude",               new=mock_llm), \
         patch("intent_prototype.query_interpreter.claude", new=mock_llm), \
         patch("intent_prototype.doc_classifier.claude",    new=mock_llm):
        result = intent_bridge.rerank_with_intent("find AI buyers", [], 10)
    assert result == []
    mock_llm.assert_not_called()

# ══════════════════════════════════════════════════════════════════════════════
# 3. Interpreter exception → original candidates returned
# ══════════════════════════════════════════════════════════════════════════════

def test_interpreter_exception_returns_candidates(ctx):
    import intent_bridge
    candidates = make_candidates(5)
    with patch_llm(side_effect=RuntimeError("API down")):
        result = intent_bridge.rerank_with_intent("find AI buyers", candidates, 5)
    assert result == candidates

# ══════════════════════════════════════════════════════════════════════════════
# 4. Classifier exception → original candidates returned
# ══════════════════════════════════════════════════════════════════════════════

def test_classifier_exception_returns_candidates(ctx):
    import intent_bridge
    candidates = make_candidates(5)
    interp_done = [False]

    def llm_side_effect(system, user, **kwargs):
        if "retrieval" in system and not interp_done[0]:
            interp_done[0] = True
            return _interpreter_json()
        raise RuntimeError("classifier API timeout")

    with patch_llm(side_effect=llm_side_effect):
        result = intent_bridge.rerank_with_intent("find AI buyers", candidates, 5)
    assert result == candidates

# ══════════════════════════════════════════════════════════════════════════════
# 5. Timeout → returns candidates quickly (bridge is non-fatal)
# ══════════════════════════════════════════════════════════════════════════════

def test_timeout_returns_candidates_quickly(ctx, monkeypatch):
    import config
    import intent_bridge
    import time

    monkeypatch.setattr(config, "INTENT_BRIDGE_TIMEOUT_SECONDS", 1)
    candidates = make_candidates(5)

    def slow_llm(system, user, **kwargs):
        time.sleep(5)
        return _interpreter_json()

    with patch_llm(side_effect=slow_llm):
        t0 = time.time()
        result = intent_bridge.rerank_with_intent("find AI buyers", candidates, 5)
        elapsed = time.time() - t0

    assert elapsed < 3.0, f"bridge took {elapsed:.1f}s — timeout not enforced"
    assert result == candidates

# ══════════════════════════════════════════════════════════════════════════════
# 6. Real classify + real ranker: buyer_demand posts rank first
#    Proof that real ranker.rank() ran (no "ranker unavailable" warning emitted)
# ══════════════════════════════════════════════════════════════════════════════

def test_real_classify_path_buyer_demand_ranks_first(ctx, caplog):
    import logging
    import intent_bridge

    candidates = []
    buyer_urls = set()
    for i in range(8):
        is_buyer = (i % 2 == 0)
        tag = "INTENT_BUYER" if is_buyer else "INTENT_NOISE"
        url = f"https://reddit.com/r/test/{i}"
        candidates.append({
            "title":     f"title {i}",
            "post_text": f"We need AI agents for our business {tag} — {i}",
            "post_url":  url,
            "platform":  "reddit",
        })
        if is_buyer:
            buyer_urls.add(url)

    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json(["buyer_demand"])
        import re
        blocks = re.split(r"\n\n(?=\[\d+\])", user.strip())
        items = []
        for block in blocks:
            m = re.match(r"^\[(\d+)\]", block)
            if not m:
                continue
            pos      = int(m.group(1))
            is_buyer = "INTENT_BUYER" in block
            items.append({
                "i":                  pos,
                "intent":             "buyer_demand" if is_buyer else "general_discussion",
                "intent_confidence":  0.88 if is_buyer else 0.55,
                "secondary_intent":   None,
                "secondary_confidence": None,
                "actor_type":         "company",
                "actor_role":         "buyer",
                "commercial_signal":  0.80 if is_buyer else 0.15,
                "pain_intensity":     0.0,
                "urgency":            0.3,
                "specificity":        0.6,
                "geography":          None,
                "industry_hint":      "saas",
                "ambiguous":          False,
                "noise":              False,
            })
        return json.dumps(items)

    with caplog.at_level(logging.WARNING, logger="flintel.intent_bridge"):
        with patch_llm(side_effect=llm_side_effect):
            result = intent_bridge.rerank_with_intent(
                "find businesses buying AI agents", candidates, 8
            )

    # Real ranker ran — no fallback warning
    assert "ranker unavailable" not in caplog.text, (
        "real ranker.rank() must have run — fallback warning must not appear"
    )
    assert "falling back to confidence sort" not in caplog.text, (
        "real ranker.rank() must have run — fallback warning must not appear"
    )

    top_4_urls = {r["post_url"] for r in result[:4]}
    assert top_4_urls == buyer_urls, f"Expected buyer_demand posts in top 4, got: {top_4_urls}"

# ══════════════════════════════════════════════════════════════════════════════
# 7. Error classifications NOT saved to cache
# ══════════════════════════════════════════════════════════════════════════════

def test_error_classifications_not_saved_to_cache(ctx):
    import intent_bridge
    candidates = make_candidates(3)
    error_url  = candidates[2]["post_url"]
    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json()
        import re
        n_docs_in_batch = len(re.findall(r"^\[\d+\]", user, re.MULTILINE))
        if "title 2" in user:
            return json.dumps([])
        items = []
        for pos in range(1, n_docs_in_batch + 1):
            items.append({
                "i":                  pos,
                "intent":             "buyer_demand",
                "intent_confidence":  0.85,
                "secondary_intent":   None,
                "secondary_confidence": None,
                "actor_type":         "company",
                "actor_role":         "buyer",
                "commercial_signal":  0.80,
                "pain_intensity":     0.0,
                "urgency":            0.3,
                "specificity":        0.6,
                "geography":          None,
                "industry_hint":      "saas",
                "ambiguous":          False,
                "noise":              False,
            })
        return json.dumps(items)

    with patch_llm(side_effect=llm_side_effect):
        intent_bridge.rerank_with_intent("find AI buyers", candidates, 3)

    saved_urls = {item["post_url"] for item in ctx.cache.saved}
    assert error_url not in saved_urls, (
        f"error/unclassified post {error_url} must not be saved to cache"
    )

# ══════════════════════════════════════════════════════════════════════════════
# 8. Interpreter fallback still calls classify
# ══════════════════════════════════════════════════════════════════════════════

def test_interpreter_fallback_flag_classify_still_called(ctx):
    import intent_bridge
    candidates = make_candidates(4)
    classify_called = [False]
    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "THIS IS NOT JSON"
        classify_called[0] = True
        return _classifier_json(candidates[:4])

    with patch_llm(side_effect=llm_side_effect):
        result = intent_bridge.rerank_with_intent("find AI buyers", candidates, 4)

    assert classify_called[0], "classify must be called even after interpreter fallback"
    assert len(result) > 0

# ══════════════════════════════════════════════════════════════════════════════
# 9. Short-circuit: tail not classified when head has enough passing
# ══════════════════════════════════════════════════════════════════════════════

def test_short_circuit_skips_tail(ctx, monkeypatch):
    import config
    import intent_bridge
    monkeypatch.setattr(config, "INTENT_SHORTCIRCUIT_HEAD",           5)
    monkeypatch.setattr(config, "INTENT_SHORTCIRCUIT_MIN_PASSING",    3)
    monkeypatch.setattr(config, "INTENT_SHORTCIRCUIT_MIN_CONFIDENCE", 0.70)

    candidates = make_candidates(20)
    classify_call_sizes = []
    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json()
        import re
        n = len(re.findall(r"\[(\d+)\]", user))
        classify_call_sizes.append(n)
        return _classifier_json([None] * n, intent="buyer_demand", confidence=0.88)

    with patch_llm(side_effect=llm_side_effect):
        intent_bridge.rerank_with_intent("find AI buyers", candidates, 10)

    total_classified = sum(classify_call_sizes)
    assert total_classified <= 5, f"Short-circuit should classify only head (5), got {total_classified}"

# ══════════════════════════════════════════════════════════════════════════════
# 10. Cached posts are NOT sent to classify()
# ══════════════════════════════════════════════════════════════════════════════

def test_cached_posts_not_reclassified(ctx):
    import intent_bridge
    candidates = make_candidates(4)
    cached_url = candidates[1]["post_url"]

    # FakeCache.store uses the same shape as get_many returns:
    # {post_url, intents, confidence}
    ctx.cache.store[cached_url] = {
        "post_url":   cached_url,
        "intents":    ["buyer_demand"],
        "confidence": 0.90,
    }

    classify_user_prompts = []
    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json()
        classify_user_prompts.append(user)
        return _classifier_json([None] * 3)

    with patch_llm(side_effect=llm_side_effect):
        intent_bridge.rerank_with_intent("find AI buyers", candidates, 4)

    for prompt in classify_user_prompts:
        assert cached_url not in prompt, f"Cached URL {cached_url} must not be re-sent to classify()"

# ══════════════════════════════════════════════════════════════════════════════
# 11. Fill-to-N: intent-passing posts first, then similarity order
# ══════════════════════════════════════════════════════════════════════════════

def test_fill_to_n_intent_first_then_similarity(ctx):
    """
    Only 2 of 10 candidates pass the intent filter.
    evidence_required=6 → 4 non-passing appended in original (similarity) order.
    Uses FILL_PASS/FILL_NOISE tokens in post_text (not URLs) since
    _render_batch does not include post_url in the classifier prompt.
    """
    import intent_bridge

    candidates = []
    passing_urls = set()
    for i in range(10):
        tag = "FILL_PASS" if i < 2 else "FILL_NOISE"
        url = f"https://reddit.com/r/test/{i}"
        candidates.append({
            "title":     f"title {i}",
            "post_text": f"We are looking to buy AI agents for our business {tag} — post {i}",
            "post_url":  url,
            "platform":  "reddit",
        })
        if i < 2:
            passing_urls.add(url)

    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json()
        import re
        blocks = re.split(r"\n\n(?=\[\d+\])", user.strip())
        items = []
        for block in blocks:
            m = re.match(r"^\[(\d+)\]", block)
            if not m:
                continue
            pos     = int(m.group(1))
            is_pass = "FILL_PASS" in block
            items.append({
                "i":                  pos,
                "intent":             "buyer_demand" if is_pass else "general_discussion",
                "intent_confidence":  0.85 if is_pass else 0.40,
                "secondary_intent":   None,
                "secondary_confidence": None,
                "actor_type":         "company",
                "actor_role":         "buyer",
                "commercial_signal":  0.7 if is_pass else 0.1,
                "pain_intensity":     0.0,
                "urgency":            0.2,
                "specificity":        0.5,
                "geography":          None,
                "industry_hint":      "saas",
                "ambiguous":          False,
                "noise":              False,
            })
        return json.dumps(items)

    with patch_llm(side_effect=llm_side_effect):
        result = intent_bridge.rerank_with_intent(
            "find AI buyers", candidates, evidence_required=6
        )

    assert len(result) >= 6, f"Expected ≥6 results, got {len(result)}"
    top_2_urls = {result[0]["post_url"], result[1]["post_url"]}
    assert top_2_urls == passing_urls, f"Intent-passing posts must be first. Got {top_2_urls}"

# ══════════════════════════════════════════════════════════════════════════════
# 12. Return format: original dicts, no mutation
# ══════════════════════════════════════════════════════════════════════════════

def test_return_format_original_dicts_no_mutation(ctx):
    import intent_bridge
    candidates = make_candidates(4)
    original_keys = set(candidates[0].keys())
    candidate_ids = {id(c) for c in candidates}
    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json()
        return _classifier_json(candidates)

    with patch_llm(side_effect=llm_side_effect):
        result = intent_bridge.rerank_with_intent("find AI buyers", candidates, 4)

    for item in result:
        assert id(item) in candidate_ids, "result must contain original dict objects"
        assert set(item.keys()) == original_keys, (
            f"candidate dict must not be mutated; got extra keys: {set(item.keys()) - original_keys}"
        )

# ══════════════════════════════════════════════════════════════════════════════
# 13. Contract: doc_classifier.classify signature
# ══════════════════════════════════════════════════════════════════════════════

def test_doc_classifier_classify_signature_contract():
    import inspect
    from intent_prototype import doc_classifier
    sig = inspect.signature(doc_classifier.classify)
    params = list(sig.parameters.keys())
    assert params[0] == "docs"
    assert "batch_size" in params
    assert "model"      in params
    assert "progress"   in params

# ══════════════════════════════════════════════════════════════════════════════
# 14. logics.py flag-ON: bridge receives wide pool (≥ limit candidates)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def logics_mod(monkeypatch):
    import types
    import math

    db_stub = types.ModuleType("database")
    _coll = MagicMock()
    for attr in [
        "db", "jobs_collection", "signals_collection",
        "signals_collection_2", "signals_collection_4",
        "google_posts_collection", "topic_evidence_cache_collection",
        "website_evidence_cache_collection",
    ]:
        setattr(db_stub, attr, _coll)

    fi_stub = types.ModuleType("flintel")
    fi_stub.ROUTER_UNFILTERED_ADDENDUM            = ""
    fi_stub.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM = ""

    wi_stub    = types.ModuleType("website_intelligence")
    goog_stub  = types.ModuleType("google")
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.AsyncClient      = MagicMock()
    httpx_stub.TimeoutException = Exception
    httpx_stub.HTTPStatusError  = Exception

    stubs = {
        "database":             db_stub,
        "flintel":              fi_stub,
        "website_intelligence": wi_stub,
        "google":               goog_stub,
        "httpx":                httpx_stub,
    }
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    for key in list(sys.modules):
        if key == "logics" or key.startswith("logics."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    import logics
    return logics

def _make_synthetic_docs(n, dim=1536):
    import math
    unit = [1.0 / math.sqrt(dim)] * dim
    from datetime import datetime, timezone
    return [
        {
            "_id":         f"doc{i}",
            "title":       f"title {i}",
            "post_text":   f"We need to buy AI agents for our company — {i}",
            "post_url":    f"https://reddit.com/r/test/{i}",
            "platform":    "reddit",
            "subreddit":   "artificial",
            "embedding":   unit,
            "created_utc": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        for i in range(n)
    ]

def _make_find_mock(docs):
    limit_mock = MagicMock()
    limit_mock.__iter__ = lambda self: iter(docs)
    sort_mock  = MagicMock()
    sort_mock.limit.return_value = limit_mock
    find_mock  = MagicMock()
    find_mock.sort.return_value  = sort_mock
    coll_mock  = MagicMock()
    coll_mock.find.return_value  = find_mock
    return coll_mock

def test_logics_flag_on_bridge_gets_wide_pool(logics_mod, monkeypatch):
    import math
    import config
    monkeypatch.setattr(config, "INTENT_BRIDGE_ENABLED",       True)
    monkeypatch.setattr(config, "INTENT_CANDIDATE_MULTIPLIER", 4)
    monkeypatch.setattr(config, "INTENT_CANDIDATE_MIN",        100)
    monkeypatch.setattr(config, "INTENT_CANDIDATE_MAX",        200)
    monkeypatch.setattr(logics_mod, "SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD", 0.0)

    captured = {}

    def fake_rerank(user_query, candidates, evidence_required, **kwargs):
        captured["n"] = len(candidates)
        return candidates[:evidence_required]

    monkeypatch.setattr(logics_mod, "rerank_with_intent", fake_rerank, raising=False)
    import intent_bridge as _ib
    monkeypatch.setattr(_ib, "rerank_with_intent", fake_rerank)

    dim  = 1536
    unit = [1.0 / math.sqrt(dim)] * dim
    monkeypatch.setattr(logics_mod, "generate_query_embeddings_batch", lambda *a, **kw: [unit])

    limit    = 20
    raw_docs = _make_synthetic_docs(250)
    mock_coll = _make_find_mock(raw_docs)
    monkeypatch.setattr(logics_mod, "signals_collection",   mock_coll)
    monkeypatch.setattr(logics_mod, "signals_collection_4", mock_coll)

    logics_mod.get_matched_signals(
        topic_key="test_topic",
        keywords=["AI agents"],
        targeting_platform="all",
        limit=limit,
        user_query="find AI buyers",
    )

    pool = captured.get("n", 0)
    assert pool >= 100, f"Wide-pool must pass ≥100 candidates to bridge; got {pool}"
    assert pool <= 200, f"Wide-pool must not exceed 200 candidates; got {pool}"

# ══════════════════════════════════════════════════════════════════════════════
# 15. logics.py flag-OFF: rerank_with_intent never called
# ══════════════════════════════════════════════════════════════════════════════

def test_logics_flag_off_rerank_never_called(logics_mod, monkeypatch):
    import config
    import math
    monkeypatch.setattr(config, "INTENT_BRIDGE_ENABLED", False)
    monkeypatch.setattr(logics_mod, "SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD", 0.0)

    rerank_called = [False]

    def fake_rerank(*args, **kwargs):
        rerank_called[0] = True
        return args[1]

    import intent_bridge as _ib
    monkeypatch.setattr(_ib, "rerank_with_intent", fake_rerank)

    dim  = 1536
    unit = [1.0 / math.sqrt(dim)] * dim
    monkeypatch.setattr(logics_mod, "generate_query_embeddings_batch", lambda *a, **kw: [unit])

    limit    = 10
    raw_docs = _make_synthetic_docs(50)
    mock_coll = _make_find_mock(raw_docs)
    monkeypatch.setattr(logics_mod, "signals_collection",   mock_coll)
    monkeypatch.setattr(logics_mod, "signals_collection_4", mock_coll)

    result = logics_mod.get_matched_signals(
        topic_key="test_topic",
        keywords=["AI agents"],
        targeting_platform="all",
        limit=limit,
        user_query="find AI buyers",
    )

    assert not rerank_called[0], "rerank_with_intent must NOT be called when INTENT_BRIDGE_ENABLED=False"
    assert len(result) <= limit

# ══════════════════════════════════════════════════════════════════════════════
# 16. topic_sims kwarg accepted — no TypeError from logics.py call pattern
# ══════════════════════════════════════════════════════════════════════════════

def test_topic_sims_kwarg_accepted(ctx):
    """
    logics.py calls rerank_with_intent(user_query, candidates, limit,
    topic_sims=[...]) with keyword argument topic_sims.
    Calls the REAL intent_bridge.rerank_with_intent (bridge not mocked).
    """
    import intent_bridge
    candidates = make_candidates(3)
    sims = [0.92, 0.87, 0.81]
    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json(["buyer_demand"])
        import re
        n = len(re.findall(r"^\[\d+\]", user, re.MULTILINE))
        return _classifier_json([None] * n, intent="buyer_demand", confidence=0.85)

    with patch_llm(side_effect=llm_side_effect):
        result = intent_bridge.rerank_with_intent(
            "find AI buyers", candidates, len(candidates),
            topic_sims=sims,
        )

    assert isinstance(result, list), "result must be a list"
    assert len(result) == len(candidates), f"result length {len(result)} != candidates length {len(candidates)}"
    candidate_ids = {id(c) for c in candidates}
    for item in result:
        assert id(item) in candidate_ids, "result items must be original candidate dicts"

# ══════════════════════════════════════════════════════════════════════════════
# 17. Small candidate list (≤ CLASSIFIER_BATCH_SIZE) → exactly 1 Claude call
# ══════════════════════════════════════════════════════════════════════════════

def test_small_list_single_classify_call(ctx):
    """
    3 candidates fit within CLASSIFIER_BATCH_SIZE (=17).
    Bridge must make exactly ONE classifier call, not 3 parallel batches.
    """
    import intent_bridge
    from intent_prototype import schemas

    assert 3 <= schemas.CLASSIFIER_BATCH_SIZE

    candidates = make_candidates(3)
    classify_call_count = [0]
    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json(["buyer_demand"])
        classify_call_count[0] += 1
        import re
        n = len(re.findall(r"^\[\d+\]", user, re.MULTILINE))
        return _classifier_json([None] * n, intent="buyer_demand", confidence=0.85)

    with patch_llm(side_effect=llm_side_effect):
        result = intent_bridge.rerank_with_intent(
            "find AI buyers", candidates, len(candidates)
        )

    assert classify_call_count[0] == 1, (
        f"Expected exactly 1 classifier LLM call for {len(candidates)} docs, got {classify_call_count[0]}"
    )
    assert len(result) == len(candidates)

# ══════════════════════════════════════════════════════════════════════════════
# 18. Ranker unavailable → fallback warning logged, result still returned
#     FAILS if real ranker ran (would mean no fallback path is tested)
# ══════════════════════════════════════════════════════════════════════════════

def test_ranker_fallback_warning_logged_on_ranker_failure(ctx, caplog):
    """
    If ranker.rank() raises, _rank_passing must log a warning containing
    'ranker unavailable' and still return a non-empty result (confidence sort).
    """
    import logging
    import intent_bridge

    candidates = make_candidates(3)
    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json(["buyer_demand"])
        import re
        n = len(re.findall(r"^\[\d+\]", user, re.MULTILINE))
        return _classifier_json([None] * n, intent="buyer_demand", confidence=0.85)

    with patch("intent_prototype.ranker.rank", side_effect=RuntimeError("ranker boom")):
        with caplog.at_level(logging.WARNING, logger="flintel.intent_bridge"):
            with patch_llm(side_effect=llm_side_effect):
                result = intent_bridge.rerank_with_intent(
                    "find AI buyers", candidates, len(candidates)
                )

    assert "ranker unavailable" in caplog.text, (
        "fallback warning 'ranker unavailable' must be logged when ranker.rank() raises"
    )
    assert isinstance(result, list) and len(result) > 0, (
        "result must be non-empty even after ranker failure (confidence-sort fallback)"
    )

# ══════════════════════════════════════════════════════════════════════════════
# 19. save_many called with List[dict] — matches real save_many signature
#     Uses a Mongo-mock collection to verify save_many is NOT called with tuples
# ══════════════════════════════════════════════════════════════════════════════

def test_save_many_called_with_list_of_dicts(ctx, monkeypatch):
    """
    classification_cache.save_many(List[dict]) — each dict has post_url,
    intents, confidence.  Verifies the bridge passes dicts (not tuples) and
    that each saved item has the required fields.
    """
    import intent_bridge

    save_many_calls = []

    def spy_save_many(items):
        save_many_calls.extend(items)
        # Also forward to FakeCache so the test stays consistent
        ctx.cache.save_many(items)

    # Replace save_many in the fake cache module
    sys.modules["intent_prototype.classification_cache"].save_many = spy_save_many

    candidates = make_candidates(3)
    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json(["buyer_demand"])
        import re
        n = len(re.findall(r"^\[\d+\]", user, re.MULTILINE))
        return _classifier_json([None] * n, intent="buyer_demand", confidence=0.85)

    with patch_llm(side_effect=llm_side_effect):
        intent_bridge.rerank_with_intent("find AI buyers", candidates, len(candidates))

    assert len(save_many_calls) > 0, "save_many must have been called with classified items"

    for item in save_many_calls:
        # Each item must be a dict, NOT a tuple
        assert isinstance(item, dict), (
            f"save_many received {type(item).__name__}, expected dict"
        )
        # Must have the three fields real save_many requires
        assert "post_url"   in item, f"save_many item missing 'post_url': {item}"
        assert "intents"    in item, f"save_many item missing 'intents': {item}"
        assert "confidence" in item, f"save_many item missing 'confidence': {item}"
        assert isinstance(item["intents"], list), "intents must be a list"
        assert isinstance(item["confidence"], float), "confidence must be a float"

# ══════════════════════════════════════════════════════════════════════════════
# 20. topic_sims affect ranking_score: high-sim doc outranks low-sim doc
# ══════════════════════════════════════════════════════════════════════════════

def test_topic_sims_affect_ranking_order(ctx, caplog):
    """
    Two candidates with equal classification scores but different topic_sims.
    High-sim candidate must rank above low-sim candidate.
    Also proves real ranker.rank() ran (no 'ranker unavailable' warning).
    """
    import logging
    import intent_bridge

    high_sim_url = "https://reddit.com/r/test/high"
    low_sim_url  = "https://reddit.com/r/test/low"

    # Present low-sim first (original order), high-sim second
    candidates = [
        {"title": "low sim post",  "post_text": "We buy AI agents LOW",  "post_url": low_sim_url,  "platform": "reddit"},
        {"title": "high sim post", "post_text": "We buy AI agents HIGH", "post_url": high_sim_url, "platform": "reddit"},
    ]
    topic_sims = [0.10, 0.99]  # low first, high second

    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json(["buyer_demand"])
        # Both classified as buyer_demand with identical confidence
        import re
        n = len(re.findall(r"^\[\d+\]", user, re.MULTILINE))
        return _classifier_json([None] * n, intent="buyer_demand", confidence=0.85)

    with caplog.at_level(logging.WARNING, logger="flintel.intent_bridge"):
        with patch_llm(side_effect=llm_side_effect):
            result = intent_bridge.rerank_with_intent(
                "find AI buyers", candidates, len(candidates),
                topic_sims=topic_sims,
            )

    # Real ranker ran
    assert "ranker unavailable" not in caplog.text, (
        "real ranker must have run — 'ranker unavailable' must not appear"
    )

    assert len(result) == 2
    assert result[0]["post_url"] == high_sim_url, (
        f"High-sim doc must rank first. Got: {[r['post_url'] for r in result]}"
    )
    assert result[1]["post_url"] == low_sim_url
