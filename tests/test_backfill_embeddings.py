"""
tests/test_backfill_embeddings.py
==================================
Unit tests for backfill_embeddings.py CLI script.

No real Mongo or OpenAI calls — all collections are in-memory stubs.
"""
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

def _make_docs_with_embedding(n, empty_embed=False):
    return [
        {
            "_id": f"id_{i}",
            "post_url": f"url_{i}",
            "post_text": "A " * 60,  # long enough for min_text_chars check
            "embedding": [] if empty_embed else [0.1] * 10,
        }
        for i in range(n)
    ]


def _make_docs_missing(n):
    """Docs with no embedding field — simulates what _missing_filter would return."""
    return [
        {
            "_id": f"missing_{i}",
            "post_url": f"url_missing_{i}",
            "post_text": "B " * 60,
        }
        for i in range(n)
    ]


def _make_collection_stub(missing_docs, count=None):
    """Stub a pymongo collection for backfill tests."""
    coll = MagicMock()
    coll.count_documents.return_value = count if count is not None else len(missing_docs)
    # find().limit() chain
    cursor = MagicMock()
    cursor.__iter__ = MagicMock(return_value=iter(missing_docs))
    cursor.limit.return_value = cursor
    coll.find.return_value = cursor
    return coll


@pytest.fixture(scope="module")
def bf_mod():
    """Import backfill_embeddings with database stubbed out."""
    import os
    os.environ.setdefault("OPENAI_API_KEY", "test-key-bf")
    os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
    os.environ.setdefault("MONGODB_DB", "test_db")

    db_stub = types.ModuleType("database")
    _coll = MagicMock()
    for attr in [
        "db", "jobs_collection", "signals_collection",
        "signals_collection_2", "signals_collection_4",
        "google_posts_collection", "topic_evidence_cache_collection",
        "website_evidence_cache_collection",
    ]:
        setattr(db_stub, attr, _coll)

    sys.modules["database"] = db_stub

    # Remove cached module so we get a clean import
    for key in list(sys.modules):
        if key == "backfill_embeddings":
            del sys.modules[key]

    mod = importlib.import_module("backfill_embeddings")
    yield mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCountMissing:

    def test_count_calls_count_documents(self, bf_mod):
        coll = MagicMock()
        coll.count_documents.return_value = 7
        result = bf_mod._count_missing(coll, "test_label")
        assert result == 7
        coll.count_documents.assert_called_once()

    def test_count_returns_minus_one_on_error(self, bf_mod):
        coll = MagicMock()
        coll.count_documents.side_effect = Exception("mongo error")
        result = bf_mod._count_missing(coll, "test_label")
        assert result == -1


class TestFetchMissing:

    def test_fetch_returns_docs(self, bf_mod):
        docs = _make_docs_missing(3)
        coll = _make_collection_stub(docs)
        result = bf_mod._fetch_missing(coll, "test_label", limit=0)
        assert len(result) == 3

    def test_fetch_applies_limit(self, bf_mod):
        docs = _make_docs_missing(10)
        coll = _make_collection_stub(docs)
        # limit > 0 should call cursor.limit()
        bf_mod._fetch_missing(coll, "test_label", limit=5)
        coll.find.return_value.limit.assert_called_once_with(5)

    def test_fetch_returns_empty_on_error(self, bf_mod):
        coll = MagicMock()
        coll.find.side_effect = Exception("mongo error")
        result = bf_mod._fetch_missing(coll, "test_label", limit=0)
        assert result == []


class TestBackfill:

    def _fake_embed(self, texts):
        return [[0.5] * 10 for _ in texts]

    def test_dry_run_does_not_call_bulk_write(self, bf_mod):
        docs = _make_docs_missing(3)
        coll = MagicMock()
        bf_mod._backfill(
            collection=coll,
            label="test",
            docs=docs,
            embed_fn=self._fake_embed,
            dry_run=True,
            min_text_chars=10,
        )
        coll.bulk_write.assert_not_called()

    def test_apply_calls_bulk_write(self, bf_mod):
        docs = _make_docs_missing(3)
        coll = MagicMock()
        coll.bulk_write.return_value.modified_count = 3
        written = bf_mod._backfill(
            collection=coll,
            label="test",
            docs=docs,
            embed_fn=self._fake_embed,
            dry_run=False,
            min_text_chars=10,
        )
        coll.bulk_write.assert_called_once()
        assert written == 3

    def test_short_text_docs_skipped(self, bf_mod):
        """Docs whose post_text is shorter than min_text_chars are skipped."""
        docs = [
            {"_id": "x1", "post_text": "hi", "post_url": "u1"},  # too short
        ]
        coll = MagicMock()
        written = bf_mod._backfill(
            collection=coll,
            label="test",
            docs=docs,
            embed_fn=self._fake_embed,
            dry_run=False,
            min_text_chars=50,
        )
        coll.bulk_write.assert_not_called()
        assert written == 0

    def test_embed_fn_error_skips_batch(self, bf_mod):
        """If embed_fn raises, that batch is skipped; no crash."""
        docs = _make_docs_missing(3)
        coll = MagicMock()

        def bad_embed(texts):
            raise RuntimeError("embed failed")

        written = bf_mod._backfill(
            collection=coll,
            label="test",
            docs=docs,
            embed_fn=bad_embed,
            dry_run=False,
            min_text_chars=10,
        )
        coll.bulk_write.assert_not_called()
        assert written == 0
