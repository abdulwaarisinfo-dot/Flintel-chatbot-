"""
tests/test_llm_json_guard.py
============================
Tests for the json_object input-guard in _call_claude().

OpenAI Responses API requires the word "json" to appear in `input`
when text.format.type == "json_object". These tests verify that
_call_claude() appends the hint when needed and leaves the input
alone when "json" is already present.

No real API calls are made — httpx.Client is patched directly.
"""
import json
import types
import importlib
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Module fixture — isolate logics from real env / network
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
    """Import logics with all external dependencies stubbed out."""
    env_patch = {
        "OPENAI_API_KEY": "test-key-json-guard",
        "MONGODB_URI": "mongodb://localhost:27017",
        "MONGODB_DB": "test_db",
    }
    old = {k: os.environ.get(k) for k in env_patch}
    os.environ.update(env_patch)

    db_stub = _make_db_stub()
    fi_stub = types.ModuleType("flintel")
    fi_stub.ROUTER_UNFILTERED_ADDENDUM = ""
    fi_stub.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM = ""
    fi_stub.build_google_fallback_answer_context = None

    wi_stub = types.ModuleType("website_intelligence")
    goog_stub = types.ModuleType("google")

    httpx_stub = types.ModuleType("httpx")
    httpx_stub.AsyncClient = MagicMock()
    httpx_stub.TimeoutException = Exception
    httpx_stub.HTTPStatusError = Exception
    # Client intentionally absent at import time; each test sets it directly.

    stubs = {
        "database": db_stub,
        "flintel": fi_stub,
        "website_intelligence": wi_stub,
        "google": goog_stub,
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

    # Restore env
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_client_factory(captured: dict, status: int = 200, resp_text: str = None):
    """Return a FakeClient class that records the last POST payload."""
    if resp_text is None:
        resp_text = json.dumps({
            "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok": true}'}]}]
        })

    class FakeResponse:
        status_code = status
        text = resp_text

        def json(self):
            return json.loads(self.text)

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

    class FakeClient:
        def __init__(self, *a, **kw): pass

        def __enter__(self): return self

        def __exit__(self, *a): pass

        def post(self, url, headers=None, json=None, **kw):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["json"] = json or {}
            return FakeResponse()

    return FakeClient


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestJsonObjectGuard:
    """_call_claude() — force_json_prefill input guard"""

    def test_appends_json_hint_when_no_json_in_input(self, logics_mod):
        """Input without 'json' → hint appended, text format set."""
        captured = {}
        logics_mod.httpx.Client = _fake_client_factory(captured)

        user_msg = "Analyse this company for buyer intent."
        logics_mod._call_claude("System.", user_msg, force_json_prefill=True)

        payload = captured["json"]
        assert "text" in payload, "text.format should be set"
        assert payload["text"] == {"format": {"type": "json_object"}}
        assert "json" in payload["input"].lower(), (
            "Hint should contain 'json' when original input did not"
        )
        assert payload["input"].startswith(user_msg), (
            "Original message should be preserved at the start"
        )

    def test_input_unchanged_when_json_already_present(self, logics_mod):
        """Input already containing 'json' → NOT modified."""
        captured = {}
        logics_mod.httpx.Client = _fake_client_factory(captured)

        user_msg = "Return the answer as json format please."
        logics_mod._call_claude("System.", user_msg, force_json_prefill=True)

        payload = captured["json"]
        assert payload["input"] == user_msg, (
            "Input must not be modified when 'json' is already present"
        )

    def test_input_unchanged_when_json_uppercase_already_present(self, logics_mod):
        """Case-insensitive check — 'JSON' in input → NOT modified."""
        captured = {}
        logics_mod.httpx.Client = _fake_client_factory(captured)

        user_msg = "Give me a JSON response."
        logics_mod._call_claude("System.", user_msg, force_json_prefill=True)

        payload = captured["json"]
        assert payload["input"] == user_msg

    def test_no_text_key_when_force_json_prefill_false(self, logics_mod):
        """force_json_prefill=False → no text key, input unchanged."""
        captured = {}
        logics_mod.httpx.Client = _fake_client_factory(captured)

        user_msg = "Tell me something."
        logics_mod._call_claude("System.", user_msg, force_json_prefill=False)

        payload = captured["json"]
        assert "text" not in payload, "text key must not appear when force_json_prefill=False"
        assert payload["input"] == user_msg, "input must not be modified"

    def test_hint_appended_when_user_message_is_empty(self, logics_mod):
        """Empty user message → hint appended (edge case)."""
        captured = {}
        logics_mod.httpx.Client = _fake_client_factory(captured)

        logics_mod._call_claude("System.", "", force_json_prefill=True)

        payload = captured["json"]
        assert "json" in payload["input"].lower()

    def test_force_json_prefill_false_with_no_json_in_input(self, logics_mod):
        """No force → no text key regardless of input content."""
        captured = {}
        logics_mod.httpx.Client = _fake_client_factory(captured)

        logics_mod._call_claude("System.", "No json here.", force_json_prefill=False)

        assert "text" not in captured["json"]
 
