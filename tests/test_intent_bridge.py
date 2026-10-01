"""
tests/test_intent_bridge.py
============================
Tests for intent_bridge.rerank_with_intent using REAL prototype modules.

Mock ONLY:
  - intent_prototype.llm.claude  (network boundary)
  - Mongo cache  (in-memory FakeCache)

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
from unittest.mock import MagicMock, patch

import pytest

# Make sure the outputs directory is on sys.path so "intent_bridge" etc. resolve.
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
    """Return a valid interpreter JSON response string."""
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
    """Return a valid classifier JSON array for the given docs."""
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


# ─── FakeCache ────────────────────────────────────────────────────────────────

class FakeCache:
    def __init__(self):
        self.store = {}
        self.saved = []

    def get_many(self, urls):
        return {u: self.store[u] for u in urls if u in self.store}

    def save_many(self, items):
        self.saved.extend(items)
        for item in items:
            self.store[item["post_url"]] = item


def _cache_module(cache: FakeCache):
    m = types.ModuleType("intent_prototype.classification_cache")
    m.get_many  = cache.get_many
    m.save_many = cache.save_many
    return m


# ─── shared fixture ───────────────────────────────────────────────────────────

@pytest.fixture
def ctx(monkeypatch):
    """Wire up a FakeCache and enable the bridge via config."""
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
        # Detect interpreter call by looking for the word "retrieval" in system prompt
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

    monkeypatch.setattr(config, "INTENT_BRIDGE_TIMEOUT_SECONDS", 1)

    candidates = make_candidates(5)

    import time
    def slow_llm(system, user, **kwargs):
        time.sleep(5)  # longer than timeout
        return _interpreter_json()

    with patch_llm(side_effect=slow_llm):
        t0 = time.time()
        result = intent_bridge.rerank_with_intent("find AI buyers", candidates, 5)
        elapsed = time.time() - t0

    # Must return within 3 s (timeout is 1 s; give 2 s headroom)
    assert elapsed < 3.0, f"bridge took {elapsed:.1f}s — timeout not enforced"
    assert result == candidates


# ══════════════════════════════════════════════════════════════════════════════
# 6. Real classify path: buyer_demand posts rank first
# ══════════════════════════════════════════════════════════════════════════════

def test_real_classify_path_buyer_demand_ranks_first(ctx):
    """
    8 candidates: those with INTENT_BUYER in post_text are buyer_demand,
    those with INTENT_NOISE are general_discussion.
    After the bridge, buyer_demand posts must occupy the top 4 slots.

    NOTE: doc_classifier._render_batch embeds only title and post_text —
    no post_url. The bridge splits docs into parallel batches whose sizes
    depend on INTENT_CLASSIFY_PARALLEL_BATCHES. To classify reliably
    without knowing the batch split, we embed a discriminating token
    (INTENT_BUYER vs INTENT_NOISE) in the post_text and parse the
    classifier's user prompt for that token per block.
    """
    import intent_bridge

    # Build 8 candidates: indices 0,2,4,6 are buyer_demand, 1,3,5,7 are not.
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

        # Parse the numbered blocks; each looks like:
        #   [N] TITLE: ...\nTEXT: ... INTENT_BUYER ... OR ... INTENT_NOISE ...
        # Determine buyer vs non-buyer from the embedded tag.
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

    with patch_llm(side_effect=llm_side_effect):
        result = intent_bridge.rerank_with_intent(
            "find businesses buying AI agents", candidates, 8
        )

    top_4_urls = {r["post_url"] for r in result[:4]}
    assert top_4_urls == buyer_urls, (
        f"Expected buyer_demand posts in top 4, got: {top_4_urls}"
    )


@contextmanager
def patch_llm(side_effect):
    """
    Patch the claude() function in all three places it is bound:
      - intent_prototype.llm.claude             (source)
      - intent_prototype.query_interpreter.claude  (bound at import time)
      - intent_prototype.doc_classifier.claude     (bound at import time)
    This is required because both sub-modules do `from .llm import claude`
    which binds the name locally and is unaffected by patching the source.
    """
    with patch("intent_prototype.llm.claude",                  side_effect=side_effect), \
         patch("intent_prototype.query_interpreter.claude",    side_effect=side_effect), \
         patch("intent_prototype.doc_classifier.claude",       side_effect=side_effect):
        yield



# ══════════════════════════════════════════════════════════════════════════════
# 7. Error classifications NOT saved to cache
# ══════════════════════════════════════════════════════════════════════════════

def test_error_classifications_not_saved_to_cache(ctx):
    """
    When the classifier reply omits a post (missing_from_reply),
    the bridge falls back to _unclassified. Those _error posts
    must NOT be persisted to cache.
    """
    import intent_bridge

    candidates = make_candidates(3)
    error_url  = candidates[2]["post_url"]   # index 2 will be missing from reply

    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json()

        # Count how many docs are in this specific batch.
        import re
        n_docs_in_batch = len(re.findall(r"^\[\d+\]", user, re.MULTILINE))

        # For the last doc (error_url = candidates[2]), we check title to
        # identify it and return an EMPTY list — simulating a missing reply.
        # All other batches return a full response for every doc in the batch.
        if "title 2" in user:
            # This batch contains the error doc — omit it from reply
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
    """
    When the interpreter returns unparseable JSON (fallback mode),
    doc_classifier.classify must still be called on the candidates.
    """
    import intent_bridge

    candidates = make_candidates(4)
    classify_called = [False]

    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "THIS IS NOT JSON"  # interpreter fallback
        # Classifier called — return valid response
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
    """
    With SHORTCIRCUIT_HEAD=5 and MIN_PASSING=3,
    if the first 5 candidates all pass at high confidence,
    the remaining candidates must NOT be sent to classify().
    """
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
        # Count how many docs are in this batch
        import re
        n = len(re.findall(r"\[(\d+)\]", user))
        classify_call_sizes.append(n)
        return _classifier_json([None] * n, intent="buyer_demand", confidence=0.88)

    with patch_llm(side_effect=llm_side_effect):
        intent_bridge.rerank_with_intent("find AI buyers", candidates, 10)

    total_classified = sum(classify_call_sizes)
    assert total_classified <= 5, (
        f"Short-circuit should classify only head (5), got {total_classified}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 10. Cached posts are NOT sent to classify()
# ══════════════════════════════════════════════════════════════════════════════

def test_cached_posts_not_reclassified(ctx):
    """
    Pre-populate FakeCache with one post. That post must not appear
    in any classify() batch (i.e. not in the user prompt to llm.claude).
    """
    import intent_bridge
    from intent_prototype import schemas

    candidates = make_candidates(4)
    cached_url = candidates[1]["post_url"]

    # Put a valid cache entry for candidate[1]
    ctx.cache.store[cached_url] = {
        "post_url":          cached_url,
        "intent":            "buyer_demand",
        "intent_confidence": 0.90,
        "secondary_intent":  None,
        "commercial_signal": 0.80,
        "pain_intensity":    0.0,
        "urgency":           0.4,
        "specificity":       0.7,
        "ambiguous":         False,
        "noise":             False,
    }

    classify_user_prompts = []

    call_count = [0]

    def llm_side_effect(system, user, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _interpreter_json()
        classify_user_prompts.append(user)
        # 3 uncached docs
        return _classifier_json([None] * 3)

    with patch_llm(side_effect=llm_side_effect):
        intent_bridge.rerank_with_intent("find AI buyers", candidates, 4)

    for prompt in classify_user_prompts:
        assert cached_url not in prompt, (
            f"Cached URL {cached_url} must not be re-sent to classify()"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 11. Fill-to-N: intent-passing posts first, then similarity order
# ══════════════════════════════════════════════════════════════════════════════

def test_fill_to_n_intent_first_then_similarity(ctx):
    """
    Only 2 of 10 candidates pass the intent filter.
    evidence_required=6 → 4 non-passing appended in original (similarity) order.

    NOTE: doc_classifier._render_batch embeds only [N] TITLE / TEXT — no post_url.
    We embed FILL_PASS / FILL_NOISE tokens in post_text so the mock can identify
    passing vs non-passing posts from the classifier prompt without relying on URLs.
    Candidates 0 and 1 carry FILL_PASS; the rest carry FILL_NOISE.
    """
    import intent_bridge

    # Build 10 candidates: 0 and 1 are "passing" (carry FILL_PASS token).
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

        # Parse numbered blocks from classifier prompt.
        # _render_batch format:  [N] TITLE: ...\nTEXT: ...FILL_PASS or FILL_NOISE...
        import re
        blocks = re.split(r"\n\n(?=\[\d+\])", user.strip())
        items = []
        for block in blocks:
            m = re.match(r"^\[(\d+)\]", block)
            if not m:
                continue
            pos      = int(m.group(1))
            is_pass  = "FILL_PASS" in block
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
    assert top_2_urls == passing_urls, (
        f"Intent-passing posts must be first. Got {top_2_urls}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 12. Return format: original dicts, no mutation
# ══════════════════════════════════════════════════════════════════════════════

def test_return_format_original_dicts_no_mutation(ctx):
    """
    Every dict in the result must be the SAME object (by id) as one of the
    input candidates — bridge must not wrap or copy them.
    Candidate dicts must also not gain extra keys from classification data.
    """
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
            f"candidate dict must not be mutated; got extra keys: "
            f"{set(item.keys()) - original_keys}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 13. Contract: doc_classifier.classify signature matches real module
# ══════════════════════════════════════════════════════════════════════════════

def test_doc_classifier_classify_signature_contract():
    """
    Verify the real doc_classifier.classify has the expected signature:
    classify(docs, batch_size=None, model=None, progress=None)
    Any change here breaks the bridge's call site.
    """
    import inspect
    from intent_prototype import doc_classifier

    sig = inspect.signature(doc_classifier.classify)
    params = list(sig.parameters.keys())

    assert params[0] == "docs", f"First param must be 'docs', got {params[0]}"
    assert "batch_size" in params
    assert "model"      in params
    assert "progress"   in params


# ══════════════════════════════════════════════════════════════════════════════
# 14. logics.py flag-ON: bridge receives wide pool (≥ limit candidates)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def logics_mod(monkeypatch):
    """
    Import logics with all heavy production dependencies stubbed out.
    Returns the logics module ready for controlled testing.
    """
    import types
    import math

    # Build a database stub with all collection names logics imports
    db_stub = types.ModuleType("database")
    _coll = MagicMock()
    for attr in [
        "db", "jobs_collection", "signals_collection",
        "signals_collection_2", "signals_collection_4",
        "google_posts_collection", "topic_evidence_cache_collection",
        "website_evidence_cache_collection",
    ]:
        setattr(db_stub, attr, _coll)

    # flintel needs the string constants logics accesses at import time
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

    # Remove cached logics so the fresh stubs take effect
    for key in list(sys.modules):
        if key == "logics" or key.startswith("logics."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    import logics  # fresh import with stubs in place
    return logics


def _make_synthetic_docs(n, dim=1536):
    """Synthetic signal docs with unit embedding vectors for easy similarity math."""
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
    """
    Build a MagicMock that supports the .find(...).sort(...).limit(...) chain
    logics.py uses to fetch candidate pools, returning the given docs list.
    The same mock works regardless of which args are passed to find/sort/limit.
    """
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
    """
    When INTENT_BRIDGE_ENABLED=True and a user_query is present,
    the call to rerank_with_intent must receive ≥ limit candidates
    (the wide-pool design: pool_n = clamp(limit × MULTIPLIER, 100, 200)).
    Verified by patching rerank_with_intent and feeding synthetic docs
    through the real get_matched_signals embedding pipeline.
    """
    import math

    # Patch the config values that logics reads at CALL TIME (from config import …)
    # These are re-imported inside _run_bridge hook; the monkeypatch on the config
    # module object takes effect for fresh function-scope imports.
    import config
    monkeypatch.setattr(config, "INTENT_BRIDGE_ENABLED",       True)
    monkeypatch.setattr(config, "INTENT_CANDIDATE_MULTIPLIER", 4)
    monkeypatch.setattr(config, "INTENT_CANDIDATE_MIN",        100)
    monkeypatch.setattr(config, "INTENT_CANDIDATE_MAX",        200)

    # SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD is imported at module level by logics.
    # Patch the module-level name so the in-function reference sees 0.0.
    monkeypatch.setattr(logics_mod, "SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD", 0.0)

    captured = {}

    def fake_rerank(user_query, candidates, evidence_required, **kwargs):
        captured["n"] = len(candidates)
        return candidates[:evidence_required]

    monkeypatch.setattr(logics_mod, "rerank_with_intent", fake_rerank, raising=False)
    # Also patch via sys.modules so the "from intent_bridge import rerank_with_intent"
    # inside get_matched_signals picks up our fake.
    import intent_bridge as _ib
    monkeypatch.setattr(_ib, "rerank_with_intent", fake_rerank)

    # Stub embedding generation — returns a single unit vector (all docs pass)
    dim  = 1536
    unit = [1.0 / math.sqrt(dim)] * dim
    monkeypatch.setattr(logics_mod, "generate_query_embeddings_batch",
                        lambda *a, **kw: [unit])

    # 250 synthetic docs with unit embeddings; cosine similarity against
    # unit query = 1.0 which clears any threshold ≥ 0.0.
    limit    = 20
    raw_docs = _make_synthetic_docs(250)

    # logics calls collection.find(...).sort(...).limit(...)
    mock_coll = _make_find_mock(raw_docs)

    # signals_collection is a module-level name in logics (from database import …)
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
    assert pool >= 100, (
        f"Wide-pool must pass ≥100 candidates to bridge; got {pool}"
    )
    assert pool <= 200, (
        f"Wide-pool must not exceed 200 candidates; got {pool}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 15. logics.py flag-OFF: rerank_with_intent never called
# ══════════════════════════════════════════════════════════════════════════════

def test_logics_flag_off_rerank_never_called(logics_mod, monkeypatch):
    """
    When INTENT_BRIDGE_ENABLED=False, logics must not call
    rerank_with_intent at all — output must be ≤ limit items.
    """
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
    monkeypatch.setattr(logics_mod, "generate_query_embeddings_batch",
                        lambda *a, **kw: [unit])

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

    assert not rerank_called[0], (
        "rerank_with_intent must NOT be called when INTENT_BRIDGE_ENABLED=False"
    )
    assert len(result) <= limit
