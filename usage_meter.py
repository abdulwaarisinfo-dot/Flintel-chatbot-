"""
usage_meter.py — Fire-and-forget OpenAI API cost metering for Flintel.

Records one document per API call into db3["FLINTEL_USAGE_IDEA"].
Never raises, never blocks the caller. Import-safe with zero side effects.
"""

import os
import sys
import logging
import contextvars
from contextlib import contextmanager
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("flintel-usage")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="usage-meter")
_index_created = False

# Which search an API call belongs to. Read in the CALLER's thread inside
# record_usage(), before the insert is handed to the executor.
_attribution = contextvars.ContextVar("flintel_usage_attribution", default=None)


_ATTRIBUTION_KEYS = ("chat_id", "owner_key", "topic_key", "request_id")


@contextmanager
def attribution(chat_id=None, owner_key=None, topic_key=None, request_id=None):
    """Tags every API call made inside this block with the search it serves.
    Values left as None are inherited from an enclosing block."""
    outer = _attribution.get() or {}
    ids = dict(chat_id=chat_id, owner_key=owner_key, topic_key=topic_key, request_id=request_id)
    token = _attribution.set({k: ids[k] if ids[k] is not None else outer.get(k) for k in _ATTRIBUTION_KEYS})
    try:
        yield
    finally:
        _attribution.reset(token)


def current_request_id():
    """The request id of the search this code is running for, if any."""
    return (_attribution.get() or {}).get("request_id")

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
                col.create_index([("chat_id", 1), ("topic_key", 1)])
                col.create_index("request_id")
                # One row per provider response: the same execution can never be counted twice.
                col.create_index("response_id", unique=True, sparse=True)
                _index_created = True
            except Exception as exc:
                log.warning(f"usage_meter: index creation failed: {exc}")
        col.insert_one(doc)
    except Exception as exc:
        if type(exc).__name__ == "DuplicateKeyError":
            log.info(f"usage_meter: response {doc.get('response_id')} already recorded; duplicate skipped")
        else:
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
        }
        if extra.get("response_id"):
            doc["response_id"] = str(extra["response_id"])
        attrs = _attribution.get() or {}
        doc.update({k: attrs.get(k) for k in _ATTRIBUTION_KEYS})

        _executor.submit(_do_insert, doc)
    except Exception as exc:
        log.warning(f"usage_meter: record_usage failed: {exc}")


def record_external(service: str, provider: str, status: str = "ok", units: int = 1):
    """Records one paid external (non-model) API request, e.g. a search API
    call. Counted separately from model tokens; never priced here."""
    try:
        attrs = _attribution.get() or {}
        doc = {"ts": datetime.utcnow(), "call_type": "external", "call_site": service, "provider": provider,
               "model": None, "units": units, "input_tokens": None, "output_tokens": None, "total_tokens": None,
               "cost_usd": None, "status": status}
        doc.update({k: attrs.get(k) for k in _ATTRIBUTION_KEYS})
        _executor.submit(_do_insert, doc)
    except Exception as exc:
        log.warning(f"usage_meter: record_external failed: {exc}")


def summarize_search_usage(rows) -> dict:
    """Totals the metered calls of ONE search. Model usage (LLM + embedding
    tokens, priced) is kept apart from external requests (counted, unpriced).
    cost_usd is the model cost, None when any model call could not be priced.
    A provider response recorded twice is counted once."""
    seen, unique = set(), []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        rid = r.get("response_id")
        if rid and rid in seen:
            continue
        seen.add(rid)
        unique.append(r)
    external = [r for r in unique if r.get("call_type") == "external"]
    rows = [r for r in unique if r.get("call_type") != "external"]
    summary = {"calls": len(rows), "input_tokens": 0, "output_tokens": 0,
               "cost_usd": 0.0, "by_call_type": {}, "unpriced_calls": 0,
               "external": {"calls": sum(r.get("units") or 1 for r in external),
                            "by_service": {}, "cost_usd": None}}
    for r in external:
        svc = summary["external"]["by_service"]
        svc[r.get("call_site") or "unknown"] = svc.get(r.get("call_site") or "unknown", 0) + (r.get("units") or 1)
    for r in rows:
        summary["input_tokens"] += r.get("input_tokens") or 0
        summary["output_tokens"] += r.get("output_tokens") or 0
        if r.get("cost_usd") is None:
            summary["unpriced_calls"] += 1
        else:
            summary["cost_usd"] += r["cost_usd"]
        kind = summary["by_call_type"].setdefault(r.get("call_type") or "unknown", {"calls": 0, "tokens": 0})
        kind["calls"] += 1
        kind["tokens"] += (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
    summary["total_tokens"] = summary["input_tokens"] + summary["output_tokens"]
    if summary["unpriced_calls"]:
        summary["cost_usd"] = None
    return summary


def get_search_usage(chat_id: str, topic_key: str, request_id: str = None) -> dict:
    """Actual metered consumption attributed to one search. With the
    message's request_id this covers the whole request, from routing to the
    answer; without it (older messages) it falls back to chat_id + topic_key,
    which merges repeated runs of the same query in a chat."""
    flt = {"request_id": request_id} if request_id else {"chat_id": chat_id, "topic_key": topic_key}
    try:
        from database import db3
        rows = list(db3["FLINTEL_USAGE_IDEA"].find(flt, {"_id": 0}))
    except Exception as exc:
        log.warning(f"usage_meter: get_search_usage failed: {exc}")
        rows = []
    return summarize_search_usage(rows)
