"""
FLINTEL — TIME-ONLY / NO-TOPIC SEARCH ("unfiltered" mode)
============================================================================
Self-contained module. Nothing here imports from or modifies index.py —
index.py imports FROM this file only (one-way dependency).

WHAT THIS FEATURE IS FOR:
Some user messages give ONLY a time reference and/or an explicit "anything
goes" signal ("aaj ke posts do", "pichle 6 mahine ke posts do", "koi bhi
posts do") with NO brand/product/topic/industry/problem named anywhere.
Previously, index.py's router would send these to the "clarify" flow
(asking the user to name a topic) since there was never a keyword to
search with. This feature lets a message like that become a real,
time-scoped "search" instead — no keyword filtering at all, just whatever
was collected within the requested time window — closed out with a
natural invitation for the user to name a topic next time for more
targeted results.

Every function below is pure (no Mongo connection created here, no
FastAPI route, no direct Claude API call) — index.py owns all of that and
calls into this module with what it already has (its own signals_collection,
its own already-parsed router output, etc.), exactly as a drop-in.
"""

import os
import re
from datetime import datetime, timedelta, timezone


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# How many posts to return when no topic is given at all — deliberately
# small (product decision): this is meant to read as "here's a taste of
# what's out there," not a full dump, precisely because there's no topic
# scoping anyone anything relevant. index.py's own get_matched_signals()
# has a much larger MAX_MATCHED_RESULTS for genuine keyword searches; this
# is a separate, intentionally tighter cap just for the topic-less case.
UNFILTERED_DEFAULT_LIMIT = int(os.getenv("UNFILTERED_DEFAULT_LIMIT", "3"))

# Fallback per-platform cap used only if a caller doesn't pass its own
# max_per_platform — mirrors the spirit of index.py's MAX_POSTS_PER_PLATFORM
# without importing it (one-way dependency — see module docstring).
_DEFAULT_MAX_PER_PLATFORM = int(os.getenv("MAX_POSTS_PER_PLATFORM", "3"))

# Fallback time-window ceiling used only if a caller doesn't pass its own
# max_time_window_days — mirrors the spirit of index.py's MAX_TIME_WINDOW_DAYS.
_DEFAULT_MAX_TIME_WINDOW_DAYS = int(os.getenv("MAX_TIME_WINDOW_DAYS", "3650"))

# A few natural, non-robotic variants — rotated by a cheap hash of the
# query so the same wording doesn't repeat every single time, without
# needing any extra state/randomness source.
UNFILTERED_INVITE_LINE = (
    "These are general posts from the time window you asked about, not "
    "scoped to any particular topic — share a brand, product, or industry "
    "(or just drop your website link) and I can pull something a lot more "
    "targeted for you next time."
)

_UNFILTERED_INVITE_LINE_VARIANTS = [
    UNFILTERED_INVITE_LINE,
    (
        "Since no specific topic was named, this is just a general slice of "
        "what came in during that window — tell me a brand, product, or "
        "industry (or share your website link) and I'll narrow it down to "
        "something actually relevant to you."
    ),
    (
        "Ye general posts hain us time window ke, kisi khaas topic tak "
        "scoped nahi — agar aap koi brand, product, ya industry bata dein "
        "(ya apni website ka link share kar dein), to main aap ke liye "
        "zyada targeted results nikal sakta hoon."
    ),
]


def _pick_invite_line(seed: str = "") -> str:
    """Rotates through the natural-language variants above using a cheap,
    deterministic hash of the input — avoids the exact same sentence
    appearing every single time without needing a random source or any
    stored state."""
    if not seed:
        return UNFILTERED_INVITE_LINE
    idx = sum(ord(c) for c in seed) % len(_UNFILTERED_INVITE_LINE_VARIANTS)
    return _UNFILTERED_INVITE_LINE_VARIANTS[idx]


# ─────────────────────────────────────────────────────────────────────────────
# DECISION FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def is_time_only_request(router_output: dict) -> bool:
    """Pure decision function — no side effects, no API calls.

    Returns True only when ALL of these hold on the already-parsed router
    output dict (same shape classify_and_maybe_chat() returns: intent,
    reply, keywords, time_window_days, plus the "unfiltered" field the
    router may now set):
      - intent == "search"
      - keywords is None or empty
      - time_window_days is a positive int
      - router_output.get("unfiltered") is True

    This is the one and only gate index.py should ever trust before
    treating a message as a genuine unfiltered/topic-less search — a
    caller passing an unvalidated or partially-missing dict here simply
    gets False back, never an exception (a missing/malformed
    router_output — including {} or None — safely returns False)."""
    if not isinstance(router_output, dict):
        return False

    if router_output.get("intent") != "search":
        return False

    keywords = router_output.get("keywords")
    if keywords:  # a non-empty list/string means a real topic WAS given
        return False

    time_window_days = router_output.get("time_window_days")
    if isinstance(time_window_days, bool) or not isinstance(time_window_days, int):
        return False
    if time_window_days <= 0:
        return False

    if router_output.get("unfiltered") is not True:
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL MATCHING (no keyword filtering at all)
# ─────────────────────────────────────────────────────────────────────────────

_TITLE_FIELD_CANDIDATES    = ["title", "post_title", "headline"]
_TEXT_FIELD_CANDIDATES     = ["post_text", "text", "body", "content", "selftext"]
_URL_FIELD_CANDIDATES      = ["post_url", "url", "link", "permalink"]
_PLATFORM_FIELD_CANDIDATES = ["platform", "source", "source_platform"]

# Mirrors index.py's _PLATFORM_DOC_VALUES exactly (see module docstring for
# why this is duplicated rather than imported: index.py imports FROM this
# file, never the other way around, so a shared helper module would be the
# "correct" long-term fix, but isn't in scope for a self-contained
# single-file feature addition).
_PLATFORM_DOC_VALUES = {
    "reddit":    ["reddit"],
    "x_twitter": ["twitter", "x", "x_twitter", "x/twitter"],
    "linkedin":  ["linkedin"],
    "facebook":  ["facebook"],
}


def _first_present(doc: dict, candidates: list):
    for field in candidates:
        value = doc.get(field)
        if value:
            return value
    return None


def _infer_platform_from_url(url: str):
    """Same behavior as index.py's own helper of the same purpose —
    duplicated here (not imported) to keep this file's only dependency
    direction index.py -> flintel.py, never the reverse."""
    if not url or not isinstance(url, str):
        return None
    domain = url.lower()
    if "twitter.com" in domain or "x.com" in domain:
        return "twitter"
    if "reddit.com" in domain:
        return "reddit"
    if "facebook.com" in domain or "fb.com" in domain:
        return "facebook"
    if "linkedin.com" in domain:
        return "linkedin"
    return None


def _default_platform_matches(doc: dict, targeting_platform: str) -> bool:
    """Fallback platform-matching logic, used only when a caller doesn't
    supply its own platform_matcher_fn — mirrors index.py's
    _signal_platform_matches() exactly. "all" (or anything unrecognized)
    means no filtering."""
    if not targeting_platform or targeting_platform == "all":
        return True

    allowed = _PLATFORM_DOC_VALUES.get(targeting_platform)
    if not allowed:
        return True  # unrecognized targeting value -> don't accidentally exclude everything

    doc_platform = _first_present(doc, _PLATFORM_FIELD_CANDIDATES)
    if not doc_platform or not isinstance(doc_platform, str):
        doc_platform = _infer_platform_from_url(_first_present(doc, _URL_FIELD_CANDIDATES))
    if not doc_platform:
        return False

    return doc_platform.strip().lower() in allowed


def get_unfiltered_matched_signals(signals_collection, since_days, targeting_platform="all",
                                    limit=None, max_per_platform=None,
                                    max_time_window_days=None, platform_matcher_fn=None) -> list:
    """Mirrors the EXACT return shape of index.py's get_matched_signals():
        [{"title":..., "post_text":..., "post_url":..., "platform":...}, ...]

    Does NOT require or accept a `keywords` list — no keyword filtering is
    applied at all. Only a time-window cutoff and an optional platform
    filter narrow the results.

    `platform_matcher_fn`, if provided, should have the same signature as
    index.py's own _signal_platform_matches(doc, targeting_platform) — this
    lets a future caller inject that exact function to avoid any drift
    between the two implementations. If not provided (the common case,
    since keeping this module's only dependency direction as
    index.py -> flintel.py means index.py's private helper can't be
    imported here), a self-contained equivalent is used instead.

    `max_time_window_days`, if provided, overrides this module's own
    _DEFAULT_MAX_TIME_WINDOW_DAYS fallback for clamping since_days — lets a
    caller pass its own MAX_TIME_WINDOW_DAYS constant in directly.

    `limit` defaults to UNFILTERED_DEFAULT_LIMIT (intentionally small —
    see that constant's own comment) when not provided. `max_per_platform`
    defaults to this module's own _DEFAULT_MAX_PER_PLATFORM when not
    provided.

    Sorts by created_utc descending, applies the same per-platform cap +
    overall limit + dedup-by-post_url logic as index.py's
    get_matched_signals(), so post cards behave identically whether
    keyword-based or unfiltered.

    Accepts the already-connected `signals_collection` object as a
    parameter — never creates its own Mongo connection."""
    effective_limit = limit if isinstance(limit, int) and limit > 0 else UNFILTERED_DEFAULT_LIMIT
    effective_max_per_platform = (
        max_per_platform if isinstance(max_per_platform, int) and max_per_platform > 0
        else _DEFAULT_MAX_PER_PLATFORM
    )
    effective_max_window = (
        max_time_window_days if isinstance(max_time_window_days, int) and max_time_window_days > 0
        else _DEFAULT_MAX_TIME_WINDOW_DAYS
    )
    matcher = platform_matcher_fn or _default_platform_matches

    # since_days is required by this function's own contract (a caller is
    # only ever supposed to reach here after is_time_only_request() already
    # confirmed a positive int) — but this stays defensive rather than
    # assuming that was actually enforced, since this function could in
    # principle be called directly.
    if not isinstance(since_days, int) or since_days <= 0:
        return []
    clamped_days = min(since_days, effective_max_window)
    cutoff = datetime.now(timezone.utc) - timedelta(days=clamped_days)

    mongo_query = {"created_utc": {"$gte": cutoff}}

    # Same headroom spirit as index.py's own fetch-more-than-limit approach,
    # since per-platform caps and dedup still narrow this further below.
    raw_docs = list(
        signals_collection.find(mongo_query, {"_id": 0})
        .sort("created_utc", -1)
        .limit(effective_limit * 10)
    )

    matched = []
    seen_urls = set()
    platform_counts = {}

    for doc in raw_docs:
        title     = _first_present(doc, _TITLE_FIELD_CANDIDATES)
        post_text = _first_present(doc, _TEXT_FIELD_CANDIDATES)

        # Defensive second check, same reasoning as index.py's own version:
        # a doc with no usable created_utc is excluded rather than assumed
        # to pass, even though the Mongo query above should have already
        # excluded it.
        doc_created = doc.get("created_utc")
        if not isinstance(doc_created, datetime):
            continue
        if doc_created.tzinfo is None:
            doc_created = doc_created.replace(tzinfo=timezone.utc)
        if doc_created < cutoff:
            continue

        if not matcher(doc, targeting_platform):
            continue

        post_url = _first_present(doc, _URL_FIELD_CANDIDATES)
        platform = _first_present(doc, _PLATFORM_FIELD_CANDIDATES) or _infer_platform_from_url(post_url)

        if not title and not post_text and not post_url:
            continue
        if post_url and post_url in seen_urls:
            continue

        platform_key = (platform or "unknown").strip().lower()
        if platform_counts.get(platform_key, 0) >= effective_max_per_platform:
            continue

        if post_url:
            seen_urls.add(post_url)

        matched.append({"title": title, "post_text": post_text, "post_url": post_url, "platform": platform})
        platform_counts[platform_key] = platform_counts.get(platform_key, 0) + 1

        if len(matched) >= effective_limit:
            break

    return matched


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

def build_unfiltered_answer_context(query: str, time_window_days: int) -> str:
    """Returns a short instruction string index.py's analyze_with_claude()
    appends to its existing user-message context (NOT a new system prompt)
    when the message being answered was an unfiltered/topic-less pull.
    Kept short so it composes cleanly with CLAUDE_ANALYSIS_SYSTEM_PROMPT's
    own existing instructions without duplicating anything already said
    there.

    (BUGFIX 2a) Previously this only described the request as topic-less
    without addressing CLAUDE_ANALYSIS_SYSTEM_PROMPT's general "no
    coherent theme -> no_results" heuristic — so a genuinely successful
    unfiltered pull (e.g. a stock-trading post, a furniture post, and a
    festival-pass post, none related to each other) got misclassified as
    "no_results" simply because the posts didn't share a theme, even
    though real posts were found. This now explicitly overrides that
    heuristic for this specific case: no shared theme is expected and
    normal here, never a reason for "no_results" on its own."""
    window_desc = f"the last {time_window_days} day{'s' if time_window_days != 1 else ''}"
    return (
        f"Note: this request named no specific brand, product, or topic — "
        f"it's a general, time-scoped pull covering {window_desc}, not "
        f"filtered to anything in particular. Because of that, the matched "
        f"posts having NO common theme or topic with each other is EXPECTED "
        f"and NORMAL for this kind of request — it is NOT a valid reason to "
        f"choose the \"no_results\" format. If posts were matched, use the "
        f"\"source_list\" format (\"ranked\": false) and simply present them "
        f"grouped by platform, even though they're unrelated to one "
        f"another. Only use \"no_results\" if the post list you were given "
        f"is genuinely empty. After presenting whatever was found, close "
        f"with a natural, professional invitation for the user to name a "
        f"topic, brand, or industry (or share their website link) next "
        f"time for more targeted results — vary the wording, don't sound "
        f"like a canned disclaimer.\n\n{_pick_invite_line(query)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ROUTER PROMPT ADDENDUM
# ─────────────────────────────────────────────────────────────────────────────

ROUTER_UNFILTERED_ADDENDUM = """
Additionally: if the user's message gives ONLY a time reference and/or an
explicit "anything/whatever's available" signal (e.g. "aaj ke posts do",
"pichle 6 mahine ke posts do", "koi bhi posts do", "kuch bhi dikha do")
with NO brand/product/topic/industry/problem named anywhere — classify
this as intent="search", set "keywords": null, parse "time_window_days"
normally, and ALSO include a new field "unfiltered": true. This is
DIFFERENT from the existing "clarify" rule: "clarify" is for when NEITHER
a topic NOR a time/no-filter signal is given at all. If the user gives a
time window OR an explicit "anything/whatever" signal, that alone is
enough for "unfiltered" search — never ask a clarifying question in that
case.

When "unfiltered": true applies, the JSON response shapes above are
extended with that one extra field, e.g.:
{"intent": "search", "reply": null, "keywords": null, "time_window_days": <int|null>, "unfiltered": true}
"""

# ─────────────────────────────────────────────────────────────────────────
# GENERIC PRODUCT + VAGUE PAIN-POINT INFERENCE ADDENDUM
# ─────────────────────────────────────────────────────────────────────────

GENERIC_PAIN_POINT_INFERENCE_ADDENDUM = """
GENERIC PRODUCT + VAGUE PAIN-POINT INFERENCE (extends the PAIN-POINT /
PROSPECT PATTERN above): sometimes a user names their own product or
service — this could be ANYTHING (AI agents, chatbots, CRM software,
accounting tools, fitness coaching, legal consulting, cleaning services,
real estate, insurance, or literally any product/service in any industry
— these are just illustrative examples, never a fixed or limited list) —
but describes the customer's problem only VAGUELY or GENERICALLY — e.g.
"find people with pain points", "people who need this", "people facing
problems", "log jo struggle kar rahe hain" — with no concrete symptom,
task, or situation named. Most real users write exactly like this
regardless of what they sell; they know their own product but haven't
articulated their customer's specific day-to-day frustration in words.

In this case, do NOT generate keywords around the generic words
themselves (the product/category name itself, or generic words like
"problem", "pain point", "issue", "need") — these are too broad and will
match unrelated noise (ads, tutorials, portfolios, unrelated mentions)
instead of real buyer-intent conversations, REGARDLESS of what industry
or product this is.

Instead, use your own general knowledge of what THAT SPECIFIC
product/service category — whatever it happens to be — actually solves
in the real world for its typical customers, and infer 2-4 of the most
common, concrete use-cases or symptoms customers in THAT space typically
experience. Then generate keywords around THOSE specific symptoms/
situations — the same natural, complaint-shaped phrasing already
described in the PAIN-POINT / PROSPECT PATTERN above (e.g. "too many
support tickets", "doing this manually", "spending hours on", "sick of
following up with", "wish there was a faster way to") — always tailored
to whatever the user's ACTUAL product/industry is, never assuming it's
any one specific category.

This inference must be done FRESH for whatever product/industry the user
actually names — never apply a fixed or memorized set of keywords from
one example to a different product. A user selling "AI agents" and a
user selling "accounting software" and a user selling "pet grooming
services" each need their OWN distinct, category-appropriate inferred
use-cases and keywords — infer independently every time based on what
this specific user actually said they sell.

This inference is a best-effort judgment call, not a guess pulled from
nothing — base it on genuinely common, well-known use-cases for that
particular product category. If the product/category is too unfamiliar,
niche, or unusual to confidently infer common use-cases for, fall back to
the existing PAIN-POINT / PROSPECT PATTERN behavior (keywords around
whatever the user did describe, however generic) rather than inventing
implausible use-cases.
"""
