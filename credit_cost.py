"""
credit_cost.py — what one accepted search costs, in credits (no LLM, no I/O).

The router (the existing, single classification call) proposes an effort
category and a whole-number cost inside that category's range. This module only
VALIDATES that proposal; the model never touches a balance. Anything that is not
exactly right is charged the configured fallback instead, and the fallback is
the same for every failure mode, so a charge is always explainable:

    effort   "standard" | "complex" | "deep"      (config.CREDIT_COST_RANGES)
    cost     a real int inside that category's inclusive range
    reason   optional, <= 15 words (stored short; never the query text)

Valid  -> (cost, effort, reason) exactly as proposed.
Invalid, missing, a router failure, or a non-search intent
       -> (CREDIT_FALLBACK_COST, "standard", fallback code).
Never raises, never makes a second model call, never retries.
"""

import logging
import re
from collections import namedtuple

log = logging.getLogger("flintel.credits")

CostDecision = namedtuple("CostDecision", "cost effort reason")

REASON_MAX_WORDS = 15
REASON_MAX_CHARS = 120

# Fixed codes stored as the reason when the fallback was charged.
FALLBACK_NO_ROUTER = "fallback:no_router_result"
FALLBACK_NOT_SEARCH = "fallback:not_a_routed_search"
FALLBACK_NO_EFFORT = "fallback:missing_effort"
FALLBACK_BAD_EFFORT = "fallback:invalid_effort"
FALLBACK_BAD_COST = "fallback:invalid_cost"
FALLBACK_OUT_OF_RANGE = "fallback:cost_out_of_range"

_FALLBACK_CODES = frozenset({FALLBACK_NO_ROUTER, FALLBACK_NOT_SEARCH, FALLBACK_NO_EFFORT, FALLBACK_BAD_EFFORT,
                             FALLBACK_BAD_COST, FALLBACK_OUT_OF_RANGE})
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _cfg():
    import config   # read at call time: tests reload/patch config
    return config


def _ranges() -> dict:
    ranges = getattr(_cfg(), "CREDIT_COST_RANGES", None) or {"standard": (3, 5), "complex": (5, 8), "deep": (8, 14)}
    return ranges


def fallback_cost() -> int:
    """The configured fallback, re-checked here so a bad patched value can never
    produce a zero/negative/oversized charge."""
    low, high = _ranges()["standard"]
    value = getattr(_cfg(), "CREDIT_FALLBACK_COST", 4)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return 4
    return value


def max_cost() -> int:
    return max(high for _low, high in _ranges().values())


def _fallback(code: str) -> CostDecision:
    return CostDecision(fallback_cost(), "standard", code)


def clean_reason(raw):
    """A short, single-line, bounded reason or None. Control characters are
    dropped and the text is cut to REASON_MAX_WORDS words / REASON_MAX_CHARS."""
    if not isinstance(raw, str):
        return None
    text = " ".join(_CONTROL_RE.sub(" ", raw).split())
    if not text:
        return None
    words = text.split(" ")
    if len(words) > REASON_MAX_WORDS:
        text = " ".join(words[:REASON_MAX_WORDS])
    return text[:REASON_MAX_CHARS].rstrip() or None


def validate(effort, cost, reason=None) -> CostDecision:
    """Strict check of one proposal. Only an exact category name with a real int
    (not bool, float, str, None) inside its range is accepted."""
    if effort is None:
        return _fallback(FALLBACK_NO_EFFORT)
    if not isinstance(effort, str):
        return _fallback(FALLBACK_BAD_EFFORT)
    name = effort.strip().lower()
    bounds = _ranges().get(name)
    if bounds is None:
        return _fallback(FALLBACK_BAD_EFFORT)
    if isinstance(cost, bool) or not isinstance(cost, int):
        return _fallback(FALLBACK_BAD_COST)
    low, high = bounds
    if not low <= cost <= high:
        return _fallback(FALLBACK_OUT_OF_RANGE)
    return CostDecision(cost, name, clean_reason(reason))


def select_cost(routed) -> CostDecision:
    """The cost of the search described by the router result `routed` (the dict
    classify_and_maybe_chat() returned, or {} when routing failed). A cost is
    only taken from a result whose intent is "search"; every other situation
    that still ends as a search (router failure, an accepted offer, a vague
    website follow-up, a URL ask) is charged the fallback."""
    if not isinstance(routed, dict) or not routed:
        return _fallback(FALLBACK_NO_ROUTER)
    if routed.get("intent") != "search":
        return _fallback(FALLBACK_NOT_SEARCH)
    decision = validate(routed.get("effort"), routed.get("cost"), routed.get("reason"))
    if decision.reason in _FALLBACK_CODES:
        log.info(f"credit cost fallback used: {decision.reason}")
    return decision
