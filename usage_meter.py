"""
usage_meter.py — Fire-and-forget OpenAI API cost metering for Flintel.

Records one document per API call into db3["FLINTEL_USAGE_IDEA"].
Never raises, never blocks the caller. Import-safe with zero side effects.
"""

import os
import sys
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("flintel-usage")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="usage-meter")
_index_created = False

# Pricing table: model -> {side: (env_override_name, default_usd_per_1m)}
_RATES = {
    "gpt-5-mini": {
        "input":  ("PRICE_GPT_5_MINI_INPUT_PER_M",  0.25),
        "output": ("PRICE_GPT_5_MINI_OUTPUT_PER_M", 2.00),
    },
    "text-embedding-3-small": {
        "input":  ("PRICE_EMBED_INPUT_PER_M", 0.02),
        "output": (None,                      0.00),
    },
}


def _get_rates(model: str):
    """Returns (input_rate, output_rate) in USD per 1M tokens.
    Returns (None, None) for unknown models — tokens still stored, cost fields set to None."""
    entry = _RATES.get(model)
    if entry is None:
        return None, None
    in_env, in_default = entry["input"]
    out_env, out_default = entry["output"]
    try:
        input_rate = float(os.getenv(in_env, in_default)) if in_env else float(in_default)
    except (TypeError, ValueError):
        input_rate = in_default
    try:
        output_rate = float(os.getenv(out_env, out_default)) if out_env else float(out_default)
    except (TypeError, ValueError):
        output_rate = out_default
    return input_rate, output_rate


def extract_responses_usage(data: dict):
    """Extracts (input_tokens, output_tokens) from an OpenAI Responses API dict.
    output_tokens already includes reasoning tokens (billed as output).
    Returns (None, None) if fields are absent."""
    usage = (data or {}).get("usage") or {}
    return usage.get("input_tokens"), usage.get("output_tokens")


def _do_insert(doc: dict):
    global _index_created
    try:
        from database import db3
        col = db3["FLINTEL_USAGE_IDEA"]
        if not _index_created:
            try:
                col.create_index("ts")
                _index_created = True
            except Exception as exc:
                log.warning(f"usage_meter: ts index creation failed: {exc}")
        col.insert_one(doc)
    except Exception as exc:
        log.warning(f"usage_meter: insert failed: {exc}")


def record_usage(call_type: str, model: str, input_tokens, output_tokens,
                 status: str = "ok", extra: dict = None):
    """Records one OpenAI API call asynchronously. Never raises, never blocks."""
    try:
        # Reject non-numeric token values rather than inventing numbers
        if input_tokens is not None and not isinstance(input_tokens, (int, float)):
            input_tokens = None
        if output_tokens is not None and not isinstance(output_tokens, (int, float)):
            output_tokens = None
        if input_tokens is None and output_tokens is None:
            return

        # Frame 2 is the caller of the hook site (e.g. _map_chunk, analyze_with_claude)
        try:
            call_site = sys._getframe(2).f_code.co_name
        except Exception:
            call_site = None

        input_rate, output_rate = _get_rates(model)

        cost_input = (input_rate * input_tokens / 1_000_000
                      if (input_rate is not None and input_tokens is not None) else None)
        cost_output = (output_rate * output_tokens / 1_000_000
                       if (output_rate is not None and output_tokens is not None) else None)
        cost_total = (cost_input + cost_output
                      if (cost_input is not None and cost_output is not None) else None)

        total_tokens = (input_tokens + output_tokens
                        if (input_tokens is not None and output_tokens is not None) else None)

        extra = extra or {}
        web_search_enabled = (bool(extra.get("web_search_enabled", False))
                              if call_type in ("llm", "llm_stream") else False)

        doc = {
            "ts":                 datetime.utcnow(),
            "call_type":          call_type,
            "call_site":          call_site,
            "provider":           "openai",
            "model":              model,
            "input_tokens":       input_tokens,
            "output_tokens":      output_tokens,
            "total_tokens":       total_tokens,
            "cost_input_usd":     cost_input,
            "cost_output_usd":    cost_output,
            "cost_usd":           cost_total,
            "rates_per_m":        {"input": input_rate, "output": output_rate},
            "status":             status,
            "web_search_enabled": web_search_enabled,
            "chat_id":            None,
            "owner_key":          None,
        }

        _executor.submit(_do_insert, doc)
    except Exception as exc:
        log.warning(f"usage_meter: record_usage failed: {exc}")
