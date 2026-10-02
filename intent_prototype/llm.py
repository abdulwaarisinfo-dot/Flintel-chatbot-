#!/usr/bin/env python3
"""
LLM AND EMBEDDING TRANSPORT
===========================================================================
Thin transport shared by query_interpreter.py and doc_classifier.py.
Deliberately identical in behaviour to embedding_diagnostic.py's calls, so
prototype numbers stay comparable with the diagnostic's numbers.

SECURITY: keys are read from the environment and never logged, echoed or
included in any error message. check_keys() reports presence only.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # deprecated/unused
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5-mini")
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low")
LLM_REASONING_HEADROOM = int(os.getenv("LLM_REASONING_HEADROOM", "2048"))
OPENAI_RESPONSES_URL = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")  # deprecated/unused
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

_RETRY_CODES = (429, 500, 502, 503, 529)


class LLMError(RuntimeError):
    """Transport failure. Message never contains credential material."""


def check_keys():
    """Presence only. Never returns or logs a key value."""
    return {
        "OPENAI_API_KEY": "SET" if OPENAI_API_KEY else "MISSING",
        "ANTHROPIC_API_KEY": "SET" if ANTHROPIC_API_KEY else "MISSING",  # deprecated/unused
    }


def claude(system, user, max_tokens=4000, model=None, timeout=120):
    """Call the OpenAI Responses API (GPT-5 mini). Name and signature
    unchanged for backward compatibility with all callers."""
    if not OPENAI_API_KEY:
        raise LLMError("OPENAI_API_KEY not set")
    effective_model = model or LLM_MODEL
    payload: dict = {
        "model": effective_model,
        "max_output_tokens": max_tokens + LLM_REASONING_HEADROOM,
        "instructions": system,
        "input": user,
        "reasoning": {"effort": LLM_REASONING_EFFORT},
        "store": False,
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
    )
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            parts = []
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            text = part.get("text", "")
                            if text:
                                parts.append(text)
            return "\n".join(parts)
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            last = f"HTTP {e.code}: {detail}"
            if e.code in _RETRY_CODES and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise LLMError(f"LLM API error — {last}")
        except Exception as exc:                      # noqa: BLE001
            last = type(exc).__name__
            if attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise LLMError(f"LLM call failed ({last})")
    raise LLMError(f"LLM call failed ({last})")


def embed_texts(texts, model=None):
    """Embed with the SAME model the corpus was built with."""
    if not OPENAI_API_KEY:
        raise LLMError("OPENAI_API_KEY not set")
    mdl = model or EMBEDDING_MODEL
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.embeddings.create(model=mdl, input=texts)
        return [d.embedding for d in resp.data]
    except ImportError:
        pass
    body = json.dumps({"model": mdl, "input": texts}).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=body,
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {OPENAI_API_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LLMError(f"OpenAI embeddings error — HTTP {e.code}")
    return [d["embedding"] for d in payload["data"]]


def parse_json_block(raw, expect="object"):
    """Pull JSON out of an LLM reply that may be fenced or prose-wrapped.

    Returns None rather than raising, so a single malformed batch degrades
    to 'unclassified' instead of killing the run.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\n?|\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    pattern = r"\[.*\]" if expect == "array" else r"\{.*\}"
    m = re.search(pattern, s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None
