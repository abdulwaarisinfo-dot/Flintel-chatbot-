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
import json
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


# ─────────────────────────────────────────────────────────────────────────────
# GOOGLE-FALLBACK FEATURE
# ─────────────────────────────────────────────────────────────────────────────
# Same self-contained, pure-function style as the rest of this file — no
# Mongo connection created here, no Claude call made here. index.py owns
# both (it holds the message doc / google_posts_collection, and its own
# analyze_with_claude()) and calls into these three functions with
# whatever plain data it already has.

GOOGLE_FALLBACK_TRIGGER_SECONDS = int(os.getenv("GOOGLE_FALLBACK_TRIGGER_SECONDS", "40"))
# Elapsed seconds since a search message's requested_at before the
# Google-search fallback is triggered, IF flintel_signals still has no
# match. Must be strictly less than RESPONSE_TIMEOUT (60) — index.py
# enforces this ordering, this module just holds the constant.


def should_trigger_google_fallback(elapsed_seconds: float, already_triggered: bool) -> bool:
    """Pure decision function, no side effects, no I/O. Returns True only
    if enough time has passed AND this hasn't already fired once for this
    message.

    `already_triggered` is read from the message's own
    google_fallback_triggered field (a new field on the message doc, set
    by index.py the first time this returns True and it acts on it) —
    checking it here is what stops this from re-triggering the Google API
    call on every single page-load poll once it's already run once for a
    given message."""
    if already_triggered:
        return False
    return elapsed_seconds >= GOOGLE_FALLBACK_TRIGGER_SECONDS


def should_trigger_immediately(already_triggered: bool) -> bool:
    """Returns True exactly once per message — the very first time this
    is checked (already_triggered=False) — so the Google search and the
    search-progress UI generation both fire in PARALLEL with the initial
    flintel_signals lookup, instead of waiting
    GOOGLE_FALLBACK_TRIGGER_SECONDS (40s) as should_trigger_google_
    fallback() above still does. Same fire-once contract as that
    function: once True is returned, the caller must mark its own flag
    True so this never fires twice for the same message. Pure decision
    function, no side effects, no I/O."""
    return not already_triggered


def format_google_stub_results(stub_docs: list) -> list:
    """Converts flintel_google_posts stub documents (see google.py) into
    the SAME {"title", "post_text", "post_url", "platform"} shape
    get_matched_signals() already returns, so the EXISTING post-card
    rendering code path in index.py/routes.py/templates needs zero
    changes to display them.

    post_text is always None — no real post content has been fetched yet
    for a discovery-only stub (that happens later, out of band, via
    Background Service #2), so this never fabricates text that isn't
    actually there.

    Also includes one additive "google_rank" field on each returned
    dict — ignored by any existing code that doesn't know about it, but
    available for a template to render "Google rank: N" if it chooses
    to.

    Skips any stub missing post_url. Never raises: a malformed/non-dict
    entry in `stub_docs` is skipped rather than crashing the whole
    conversion."""
    if not stub_docs or not isinstance(stub_docs, list):
        return []

    formatted = []
    for stub in stub_docs:
        if not isinstance(stub, dict):
            continue
        post_url = stub.get("post_url")
        if not post_url:
            continue
        subreddit = stub.get("subreddit")
        formatted.append({
            "title": f"r/{subreddit}" if subreddit else None,
            "post_text": None,
            "post_url": post_url,
            "platform": "reddit",
            "google_rank": stub.get("google_rank"),
        })
    return formatted


MAX_COMBINED_POSTS_FOR_CLAUDE = int(os.getenv("MAX_COMBINED_POSTS_FOR_CLAUDE", "7"))

# Analysis-time cap (kitna evidence Claude ko analysis ke liye milega) —
# ab dynamic hoga, ye sirf ABSOLUTE ceiling hai jo kabhi cross nahi hoga.
MAX_ANALYSIS_EVIDENCE = int(os.getenv("MAX_ANALYSIS_EVIDENCE", "100"))


def merge_matched_and_google_results(matched_signals: list, google_stub_results: list, max_total: int = None) -> list:
    """Combines matched_signals (grounded, real content) with
    google_stub_results (discovery-only, no content yet — see
    format_google_stub_results()) into one list, de-duplicated by
    post_url (a real signal always wins over a stub for the same URL,
    since it has actual text). Caps the combined total at max_total
    (default MAX_COMBINED_POSTS_FOR_CLAUDE = 7) — real signals are kept
    first (fully grounded), Google stubs fill any remaining room up to
    the cap, ordered by google_rank ascending (best Google rank first).
    Returns the same {"title", "post_text", "post_url", "platform"}
    shape get_matched_signals() already returns, with "google_rank"/
    "subreddit" additionally present on Google-sourced entries only.

    `max_total` can also be passed in dynamically by the caller (e.g.
    index.py's own evidence planner) rather than relying on the
    MAX_COMBINED_POSTS_FOR_CLAUDE default — 7 remains only the fallback
    value used when no explicit max_total is supplied.

    Never raises: bad/empty input on either side just means that side
    contributes nothing."""
    cap = max_total if isinstance(max_total, int) and max_total > 0 else MAX_COMBINED_POSTS_FOR_CLAUDE

    clean_signals = [s for s in (matched_signals or []) if isinstance(s, dict) and s.get("post_url")]
    clean_stubs = [s for s in (google_stub_results or []) if isinstance(s, dict) and s.get("post_url")]

    seen_urls = set()
    combined = []

    for signal in clean_signals:
        if len(combined) >= cap:
            break
        url = signal["post_url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        combined.append(signal)

    remaining_stubs = [s for s in clean_stubs if s["post_url"] not in seen_urls]
    remaining_stubs.sort(key=lambda s: s.get("google_rank") if isinstance(s.get("google_rank"), int) else float("inf"))

    for stub in remaining_stubs:
        if len(combined) >= cap:
            break
        url = stub["post_url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        combined.append(stub)

    return combined


def build_combined_source_context(signal_count: int, google_count: int) -> str:
    """Mirrors build_unfiltered_answer_context()/build_google_fallback_
    answer_context()'s existing pattern: a short instruction string
    index.py's analyze_with_claude() appends to its existing
    user-message context (NOT a new system prompt).

    Tells Claude it has been given a MIX of two kinds of posts in the
    SAME batch: fully-grounded posts from Flintel's own collected
    signals (real title + text), and discovery-only posts found via a
    supplementary Google search (title is just the subreddit name,
    google_rank is its search rank, post_text is intentionally empty
    because the actual content hasn't been fetched yet). Instructs
    Claude to NEVER invent or guess text for a discovery-only post — if
    it includes one in the final answer, its "summary" field must
    honestly say the content hasn't been fetched yet rather than
    fabricating one. Also instructs Claude to never surface more than
    MAX_COMBINED_POSTS_FOR_CLAUDE posts total in its final answer,
    freely choosing the best combination from either source based on
    genuine relevance — never forcing an even split, never padding with
    a low-quality post just to reach a count."""
    return (
        f"Note: you have been given a MIX of two kinds of posts in this "
        f"same batch — {signal_count} fully-grounded post(s) from "
        f"Flintel's own collected signals (real title and text, safe to "
        f"quote/summarize normally), and {google_count} discovery-only "
        f"post(s) found via a supplementary Google search (their "
        f"\"title\" is just the subreddit name, \"google_rank\" is their "
        f"search rank, and \"post_text\" is intentionally empty because "
        f"the actual content hasn't been fetched yet). NEVER invent or "
        f"guess what a discovery-only post actually says — if you "
        f"include one in your answer, its summary must honestly say the "
        f"content hasn't been fetched yet rather than fabricating one. "
        f"Never surface more than {MAX_COMBINED_POSTS_FOR_CLAUDE} posts "
        f"total in your final answer — freely choose the best "
        f"combination from either source based on genuine relevance, "
        f"never forcing an even split between the two, and never "
        f"padding with a low-quality post just to reach a count."
    )


def build_google_fallback_answer_context(query: str, stub_count: int) -> str:
    """Mirrors the EXACT pattern of build_unfiltered_answer_context()
    above: returns a short instruction string index.py's
    analyze_with_claude() appends to its existing user-message context
    (NOT a new system prompt) — safe to call unconditionally, including
    with stub_count == 0.

    Purpose: flintel_signals had no match yet for this topic, so no
    grounded post TEXT is available to answer from — but `stub_count`
    related Reddit threads (if any) were found via a Google-search
    fallback and will be shown to the user separately, as links only.
    Tells Claude to (a) answer the user's actual question from its own
    general knowledge, (b) NEVER invent or guess what those threads
    actually say, since only their URLs were found, not their content,
    and (c) if stub_count > 0, briefly and honestly mention that some
    related discussions were found and are shown below, without
    describing their content."""
    if not stub_count:
        return (
            "Note: no matching posts were found for this topic yet. Answer "
            "the user's actual question from your own general knowledge "
            "instead, and be honest that no specific posts were found for "
            "this search rather than inventing any."
        )

    plural = "s" if stub_count != 1 else ""
    return (
        f"Note: no matching posts with actual content were found for this "
        f"topic in the system yet — but a Google search turned up "
        f"{stub_count} related Reddit thread{plural}, shown to the user "
        f"below as links only (their post_text is intentionally empty — "
        f"their actual content hasn't been fetched yet). Answer the user's "
        f"actual question from your own general knowledge. You may briefly "
        f"and honestly mention that some related discussions were found and "
        f"are shown below, but NEVER invent, guess, or describe what those "
        f"threads actually say — only their links exist right now, not "
        f"their content."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH-PROGRESS UI CONTENT (query-aware "still searching" message)
# ─────────────────────────────────────────────────────────────────────────────
# Same self-contained, pure-function-plus-injected-Claude-call style as
# website_intelligence.py's summarize_website_structured() — this module
# never talks to the Anthropic API directly; index.py/routes.py pass in
# their own _call_claude() as `call_claude_fn`.

SEARCH_PROGRESS_MAX_TOKENS = int(os.getenv("SEARCH_PROGRESS_MAX_TOKENS", "300"))

SEARCH_PROGRESS_SYSTEM_PROMPT = """
You are writing a short, reassuring "still searching" status update shown
to a Flintel user while their search is still in progress (no matching
posts found yet, still looking). You are given the user's search query
and the keywords/platform being searched. Write copy that sounds like it
was written specifically for THIS search — never generic filler.

Produce exactly two short sentences ("intro" and "outro") plus a
checklist of exactly 4 short items describing what kinds of posts are
being looked for, all genuinely tailored to this specific query/topic —
infer closely related terms a person searching this topic would
recognize (e.g. a specific software category naturally implies related
adjacent terms), but never invent an unrelated tangent.

"intro" describes what's being searched and where (platform(s), topic) —
one sentence, plain and specific.
"outro" describes what the user will get back once results are ready —
one sentence, plain and confident, never apologetic.
"checklist" is exactly 4 short phrases (not full sentences, no trailing
period needed but fine either way), each describing a distinct kind of
signal being looked for — vary these by what's genuinely relevant to
this specific query (buying intent, recommendation requests, complaints
about alternatives, specific technical/feature asks, etc. — whichever 4
genuinely fit this topic best).

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{"intro": "<one sentence>", "outro": "<one sentence>", "checklist": ["<item 1>", "<item 2>", "<item 3>", "<item 4>"]}
"""


def _parse_json_object(raw: str):
    """Best-effort parse of a Claude text response into a JSON dict.
    Strips ```json fences if present, same tolerance already used
    elsewhere in this product. Returns None (never raises) on anything
    that isn't a well-formed JSON object."""
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def generate_search_progress_content(query: str, keywords: list, targeting_platform: str, call_claude_fn):
    """Makes ONE Claude call to produce query-aware "still searching"
    status copy — an intro sentence, an outro sentence, and a 4-item
    checklist of what's being looked for — used by the frontend's
    richer in-progress UI once a search has been running long enough
    that the plain spinner alone starts to feel uninformative.

    `call_claude_fn` must have the same signature as index.py's own
    _call_claude(system_prompt, user_message, max_tokens=None) -> str —
    this module never talks to the Anthropic API directly, same
    injection pattern website_intelligence.py's
    summarize_website_structured() already uses.

    Returns {"intro": str, "outro": str, "checklist": [str, str, str, str]}
    or None on any failure (missing query, call_claude_fn exception,
    unparseable JSON, or a response missing usable fields) — the caller
    falls back to the plain spinner state in that case, exactly as if
    this feature didn't exist. Never raises past this function."""
    if not query or not isinstance(query, str):
        return None
    if not callable(call_claude_fn):
        return None

    user_message = (
        f"Search query: {query}\n"
        f"Keywords being searched: {', '.join(k for k in (keywords or []) if isinstance(k, str))}\n"
        f"Platform(s): {targeting_platform or 'all'}"
    )
    try:
        raw = call_claude_fn(
            SEARCH_PROGRESS_SYSTEM_PROMPT,
            user_message,
            max_tokens=SEARCH_PROGRESS_MAX_TOKENS,
        )
    except Exception:
        return None

    data = _parse_json_object(raw)
    if not data:
        return None

    intro = data.get("intro")
    outro = data.get("outro")
    checklist = data.get("checklist")

    intro = intro.strip() if isinstance(intro, str) and intro.strip() else None
    outro = outro.strip() if isinstance(outro, str) and outro.strip() else None
    checklist = (
        [item.strip() for item in checklist if isinstance(item, str) and item.strip()]
        if isinstance(checklist, list) else []
    )

    if not intro or not outro or not checklist:
        return None

    return {"intro": intro, "outro": outro, "checklist": checklist}


def calculate_search_progress_percent(elapsed_seconds: float, trigger_seconds: float, timeout_seconds: float) -> int:
    """Pure calculation, no side effects: maps elapsed time within the
    [trigger_seconds, timeout_seconds] window onto a 0-100 display
    percentage for the search-progress bar, so the bar visibly advances
    as time passes rather than showing a static number. Clamped to
    [0, 100] so a caller passing a slightly-out-of-range elapsed value
    (e.g. right at the boundary) never produces a nonsensical display
    value. Returns 0 if trigger_seconds >= timeout_seconds (degenerate
    window, avoids a division by zero)."""
    if timeout_seconds <= trigger_seconds:
        return 0
    span = timeout_seconds - trigger_seconds
    progress = (elapsed_seconds - trigger_seconds) / span
    percent = round(progress * 100)
    return max(0, min(100, percent))
