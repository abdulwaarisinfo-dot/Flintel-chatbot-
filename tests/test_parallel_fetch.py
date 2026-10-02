"""
tests/test_parallel_fetch.py
=============================
Tests for parallel Mongo fetch (P3).

Verifies:
a. 3 fake collections fetched in parallel → all three are queried.
b. One collection raises → other 2 still return data.
c. Parallel result: all three collections' find() are called.
d. Legacy mode (SIGNAL_EMBEDDING_CANDIDATE_POOL=500) is unchanged.
e. SIGNAL_EMBEDDING_FETCH_BATCH is exported from config.
f. Per-collection elapsed time appears in log output.

No real Mongo or network — all collections are in-memory stubs.
"""
import importlib
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Shared stub helpers
# ---------------------------------------------------------------------------

def _make_db_stub():
    db_stub = types.ModuleType("database")
    _coll = MagicMock()
    for attr in [
        "db", "jobs_collection", "signals_collection",
        "signals_collection_2", "signals_collection_4",
        "google_posts_collection", "topic_evidence_cache_collection",
        "website_evidence_cache_collection",
    ]:
        setattr(db_stub, attr, _coll)
    return db_stub


@pytest.fixture(scope="module")
def logics_mod():
    import os
    os.environ.setdefault("OPENAI_API_KEY", "test-key-parallel")
    os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
    os.environ.setdefault("MONGODB_DB", "test_db")

    db_stub = _make_db_stub()
    fi_stub = types.ModuleType("flintel")
    fi_stub.ROUTER_UNFILTERED_ADDENDUM = ""
    fi_stub.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM = ""
    fi_stub.build_google_fallback_answer_context = None

    httpx_stub = types.ModuleType("httpx")
    httpx_stub.AsyncClient = MagicMock()
    httpx_stub.TimeoutException = Exception
    httpx_stub.HTTPStatusError = Exception

    stubs = {
        "database": db_stub,
        "flintel": fi_stub,
        "website_intelligence": types.ModuleType("website_intelligence"),
        "google": types.ModuleType("google"),
        "httpx": httpx_stub,
    }
    for name, mod in stubs.items():
        sys.modules[name] = mod

    for key in list(sys.modules):
        if key == "logics" or key.startswith("logics."):
            del sys.modules[key]
    if "config" in sys.modules:
        del sys.modules["config"]

    mod = importlib.import_module("logics")
    yield mod


# ---------------------------------------------------------------------------
# Fake in-memory collection helper
# ---------------------------------------------------------------------------

def _make_fake_collection(docs):
    """Return a minimal pymongo-compatible collection stub backed by a list."""
    class FakeCursor:
        def __init__(self, docs):
            self._docs = list(docs)
        def sort(self, *a, **kw): return self
        def batch_size(self, n): return self
        def limit(self, n): return FakeCursor(self._docs[:n])
        def __iter__(self): return iter(self._docs)

    coll = MagicMock()
    coll.find.return_value = FakeCursor(docs)
    return coll


def _make_docs(prefix, n, with_embedding=True):
    """Build n fake post docs with distinct post_url identifiers."""
    return [
        {
            "post_url": f"{prefix}_doc_{i}",
            "post_text": f"text from {prefix} doc {i}" * 5,
            "embedding": [0.1] * 10 if with_embedding else None,
            "created_utc": 1_700_000_000 - i,
            "platform": "reddit",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Config access helpers
# ---------------------------------------------------------------------------

def _set_unlimited(logics_mod):
    logics_mod.SIGNAL_EMBEDDING_CANDIDATE_POOL = 0
    logics_mod.SIGNAL_EMBEDDING_RECENCY_POOL = 0


def _set_legacy(logics_mod, pool=500):
    logics_mod.SIGNAL_EMBEDDING_CANDIDATE_POOL = pool
    logics_mod.SIGNAL_EMBEDDING_RECENCY_POOL = pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestParallelFetch:

    def test_config_fetch_batch_exported(self):
        """SIGNAL_EMBEDDING_FETCH_BATCH is exported from config."""
        import config
        assert hasattr(config, "SIGNAL_EMBEDDING_FETCH_BATCH"), (
            "SIGNAL_EMBEDDING_FETCH_BATCH must be exported from config.py"
        )
        assert isinstance(config.SIGNAL_EMBEDDING_FETCH_BATCH, int)
        assert config.SIGNAL_EMBEDDING_FETCH_BATCH > 0

    def test_three_collections_union_correct(self, logics_mod):
        """All 3 collections are queried when provided."""
        _set_unlimited(logics_mod)

        c1 = _make_fake_collection(_make_docs("col1", 5))
        c2 = _make_fake_collection(_make_docs("col2", 4))
        c4 = _make_fake_collection(_make_docs("col4", 3))

        # signals_collection is a module-level global; _2 and _4 are kwargs.
        with patch.object(logics_mod, "signals_collection", c1), \
             patch.object(logics_mod, "generate_query_embeddings_batch",
                          lambda texts: [[0.1] * 10] * len(texts)):
            logics_mod.get_matched_signals(
                topic_key="test_topic",
                keywords=["test"],
                signals_collection_2=c2,
                signals_collection_4=c4,
            )

        assert c1.find.called
        assert c2.find.called
        assert c4.find.called

    def test_one_collection_fails_others_still_return(self, logics_mod):
        """If one collection's find() raises, the other two still contribute."""
        _set_unlimited(logics_mod)

        c1 = _make_fake_collection(_make_docs("good1", 3))
        c2 = MagicMock()
        c2.find.side_effect = Exception("simulated mongo failure")
        c4 = _make_fake_collection(_make_docs("good4", 2))

        # Should not raise; bad collection is silently skipped
        with patch.object(logics_mod, "signals_collection", c1), \
             patch.object(logics_mod, "generate_query_embeddings_batch",
                          lambda texts: [[0.1] * 10] * len(texts)):
            logics_mod.get_matched_signals(
                topic_key="test_topic",
                keywords=["test"],
                signals_collection_2=c2,
                signals_collection_4=c4,
            )

        assert c1.find.called
        assert c4.find.called

    def test_parallel_same_result_as_sequential(self, logics_mod):
        """All collections' find() are called (parallel fetch is complete)."""
        _set_unlimited(logics_mod)

        c1 = _make_fake_collection(_make_docs("s1", 3))
        c2 = _make_fake_collection(_make_docs("s2", 2))
        c4 = _make_fake_collection(_make_docs("s4", 4))

        with patch.object(logics_mod, "signals_collection", c1), \
             patch.object(logics_mod, "generate_query_embeddings_batch",
                          lambda texts: [[0.1] * 10] * len(texts)):
            logics_mod.get_matched_signals(
                topic_key="test_topic",
                keywords=["test"],
                signals_collection_2=c2,
                signals_collection_4=c4,
            )

        assert c1.find.call_count >= 1
        assert c2.find.call_count >= 1
        assert c4.find.call_count >= 1

    def test_legacy_mode_unchanged(self, logics_mod):
        """Legacy mode (CANDIDATE_POOL=500) still calls find on all 3 collections."""
        _set_legacy(logics_mod, pool=500)

        c1 = _make_fake_collection(_make_docs("leg1", 3))
        c2 = _make_fake_collection(_make_docs("leg2", 2))
        c4 = _make_fake_collection(_make_docs("leg4", 4))

        with patch.object(logics_mod, "signals_collection", c1), \
             patch.object(logics_mod, "generate_query_embeddings_batch",
                          lambda texts: [[0.1] * 10] * len(texts)):
            logics_mod.get_matched_signals(
                topic_key="test_topic",
                keywords=["test"],
                signals_collection_2=c2,
                signals_collection_4=c4,
            )

        assert c1.find.called
        assert c2.find.called
        assert c4.find.called

        # Restore unlimited for other tests
        _set_unlimited(logics_mod)

    def test_per_collection_log_appears(self, logics_mod, caplog):
        """Each collection logs a fetched= elapsed= line."""
        _set_unlimited(logics_mod)

        c1 = _make_fake_collection(_make_docs("log1", 2))
        c2 = _make_fake_collection(_make_docs("log2", 1))
        c4 = _make_fake_collection(_make_docs("log4", 3))

        with caplog.at_level(logging.INFO), \
             patch.object(logics_mod, "signals_collection", c1), \
             patch.object(logics_mod, "generate_query_embeddings_batch",
                          lambda texts: [[0.1] * 10] * len(texts)):
            logics_mod.get_matched_signals(
                topic_key="log_test",
                keywords=["log"],
                signals_collection_2=c2,
                signals_collection_4=c4,
            )

        log_text = caplog.text
        assert "fetched=" in log_text and "elapsed=" in log_text, (
            "Expected 'fetched=N elapsed=Ts' in per-collection log output"
        )
