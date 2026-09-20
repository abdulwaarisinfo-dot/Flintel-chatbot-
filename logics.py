"""
FLINTEL — LOGICS (the "brain")
============================================================================
Pulls the heavy "brain" logic out of index.py — signal matching, keyword
generation/fallback, the router, the analysis layer, the website-keyword
extraction wrapper, and the single LLM call function. This is what
actually shrinks index.py's size/load; index.py keeps only FastAPI app
wiring plus chat/session/Mongo orchestration (flintel_users /
flintel_users_chat).

MOVED HERE VERBATIM (no logic change) from index.py: normalize_topic_key, 
normalize_platform, keyword-fallback generation, job/signal Mongo reads,
every signal-matching helper (including phrase-matching), get_matched_
signals() itself (matching rules 100% UNCHANGED), the whole Claude
analysis layer (map-reduce, prompts), the router, the topic-resolver, the
website-keyword-extraction wrapper, and the post-processing helpers
(format extraction, answer-finalization, URL/website-context patching).

_call_claude() / _call_claude_stream() talk to Anthropic's Messages API
(Claude Haiku) — see each function's own docstring.

(TIMEOUT-SIMPLIFICATION CHANGE) _timeout_fallback_answer()'s old tier-3
"loose_candidates / near_match_confidence / near_match_offer" branch has
been removed entirely — see that function's own docstring.
_extract_near_match_confidence() is kept defined (for now) but is dead
code — nothing calls it anymore.

(CIRCULAR-IMPORT NOTE) index.py imports several names FROM this module
(see index.py's own top-of-file import block) — so this module must
NEVER import index.py at module load time. The one function here that
still needs a few index.py-owned functions (_timeout_fallback_answer(),
which needs flintel_users_chat orchestration helpers that correctly stay
defined in index.py) imports them LAZILY, inside its own function body —
see that function's docstring for why.

(ANALYST-PROMPT UPGRADE) CLAUDE_ANALYSIS_SYSTEM_PROMPT below has been
replaced with a "market intelligence analyst" style prompt (research
objective -> evidence classification -> pattern-finding -> pain-vs-intent
distinction -> adaptive-depth report), producing a richer JSON shape
(executive_summary / key_findings / detailed_findings / market_pattern /
conclusion / followup_question) instead of the old flat "summary" +
"followups[]" shape. NOTHING about retrieval/matching/keywords changed —
this only changes how Claude writes up the posts it's already given.
The two post-construction `.replace(...)` calls right after the prompt
were updated to match this new prompt's actual wording (the old ones
were written against the old prompt's phrasing and would have silently
stopped injecting MAX_CHAT_EVIDENCE_POSTS otherwise) — see the comment
right above those calls.
"""

import re
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

import flintel
import website_intelligence
import google as google_search   # the new google.py module

from database import (
    jobs_collection, signals_collection, google_posts_collection, topic_evidence_cache_collection,
    website_evidence_cache_collection,
)

from config import (
    MAX_KEYWORDS, CLAUDE_MAX_KEYWORDS, MAX_MATCHED_RESULTS,
    MAX_TIME_WINDOW_DAYS, MAX_POSTS_PER_PLATFORM,
    MAX_CHAT_EVIDENCE_POSTS, MAX_ANALYSIS_EVIDENCE,
    MIN_ANALYSIS_EVIDENCE, ANTHROPIC_API_KEY, CLAUDE_MODEL,
    CLAUDE_API_URL, CLAUDE_API_VERSION,
    CLAUDE_MAX_TOKENS, CLAUDE_MAP_MAX_TOKENS,
    CLAUDE_POSTS_PER_CHUNK, CLAUDE_NOTES_PER_CHUNK, CLAUDE_TIMEOUT_SECONDS,
    CLAUDE_ROUTER_MAX_TOKENS, CLAUDE_TOPIC_RESOLVER_MAX_TOKENS,
    MAX_WEBSITE_KEYWORDS, WEBSITE_FETCH_TIMEOUT_SECONDS,
    WEBSITE_FETCH_MAX_CHARS, CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS,
    TOPIC_CACHE_MAX_EVIDENCE, TOPIC_CACHE_MIN_TOPUP,
    MAX_WEBSITE_PAGES, WEBSITE_DISCOVERY_PATH_HINTS,
    WEBSITE_EVIDENCE_CACHE_TTL_DAYS, WEBSITE_EVIDENCE_MAX_TOKENS,
    WEBSITE_INSIGHT_MAX_TOKENS,
)

import logging
log = logging.getLogger("flintel-web")


def normalize_topic_key(query: str) -> str:
    """Turns a user's raw prompt into a stable cache/job key.
    e.g. "  Nike  " -> "nike", "Nike Shoes!!" -> "nike shoes"

    UNCHANGED by the keyword-generation swap: topic_key is still derived
    from the raw query string and is used only as the flintel_search_jobs
    upsert key / chat message linkage — it has nothing to do with which
    keywords get matched against flintel_signals."""
    cleaned = re.sub(r"[^a-z0-9\s]", "", query.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM TARGETING
# ─────────────────────────────────────────────────────────────────────────────

# Maps whatever label the "All Platforms" dropdown sent to a stable key.
# This ONLY tags the job — it never changes what keywords get generated,
# and it is still driven ONLY by this dropdown field, never by words in
# the user's chat message.
PLATFORM_KEY_MAP = {
    "all platforms": "all",
    "all":           "all",
    "reddit":        "reddit",
    "x / twitter":   "x_twitter",
    "x/twitter":     "x_twitter",
    "twitter":       "x_twitter",
    "x":             "x_twitter",
    "linkedin":      "linkedin",
    "facebook":      "facebook",
}


def normalize_platform(raw: str) -> str:
    """Falls back to "all" for anything unrecognized, so a missing/garbled
    dropdown value never accidentally narrows a search."""
    key = (raw or "").strip().lower()
    return PLATFORM_KEY_MAP.get(key, "all")


# ─────────────────────────────────────────────────────────────────────────────
# FUZZY KEYWORD GENERATION — plain Python, template-based, no Claude call
#
# (KEYWORD-GENERATION SWAP) This function and KEYWORD_TEMPLATES are KEPT
# COMPLETELY UNCHANGED from v1, but they are NO LONGER the primary path
# for a "search" message. They are now used ONLY as a safety-net FALLBACK
# — see classify_and_maybe_chat() / the /search route below — for the
# rare case where the Claude routing call fails outright, or succeeds but
# doesn't return a usable "keywords" list for a "search" intent. This
# guarantees a search can never end up with zero keywords, exactly the
# same safety philosophy already used everywhere else in this file.
# ─────────────────────────────────────────────────────────────────────────────

KEYWORD_TEMPLATES = [
    "{q}",
    "{q} review",
    "{q} reviews",
    "{q} honest review",
    "{q} complaint",
    "{q} complaints",
    "{q} problem",
    "{q} problems",
    "{q} issue",
    "{q} issues",
    "{q} experience",
    "{q} experiences",
    "is {q} worth it",
    "is {q} good",
    "{q} worth it",
    "switched from {q}",
    "switched to {q}",
    "alternative to {q}",
    "{q} vs",
    "{q} pricing",
    "{q} customer service",
    "{q} support",
    "{q} quality",
    "{q} scam",
    "{q} recommend",
    "thoughts on {q}",
    "anyone using {q}",
    "anyone tried {q}",
]


def generate_fuzzy_keywords(query: str) -> list:
    """Builds a list of search phrases around the user's query using fixed
    templates. Deterministic, fast, and needs no external API call.

    (KEYWORD-GENERATION SWAP) Still here, byte-for-byte unchanged — now
    called only as the fallback path described above, not on every
    search."""
    q = query.strip()
    if not q:
        return []

    seen = set()
    keywords = []
    for template in KEYWORD_TEMPLATES:
        phrase = template.format(q=q).strip()
        key = phrase.lower()
        if key not in seen:
            seen.add(key)
            keywords.append(phrase)
        if len(keywords) >= MAX_KEYWORDS:
            break

    return keywords


# ─────────────────────────────────────────────────────────────────────────────
# JOB QUEUE — patch into the SAME MongoDB Background Service #1 reads
# ─────────────────────────────────────────────────────────────────────────────

def enqueue_search_job(topic_key: str, keywords: list, targeting_platform: str):
    """Upserts a job by topic_key. Re-searching the same topic simply
    resets it to pending instead of creating a duplicate job.

    UNCHANGED by the keyword-generation swap: this function has no idea
    (and doesn't need to know) whether `keywords` came from Claude's
    router call or the generate_fuzzy_keywords() fallback — it just
    stores whatever list it's handed, exactly as before. Also UNCHANGED
    by the TIME-WINDOW FEATURE: a time window only ever narrows which
    ALREADY-COLLECTED signals are matched (see get_matched_signals()
    below) — it has no bearing on what Background Service #1 goes out
    and fetches, so it is never stored on the job document."""
    jobs_collection.update_one(
        {"topic_key": topic_key},
        {"$set": {
            "topic_key":          topic_key,
            "keywords":           keywords,
            "targeting_platform": targeting_platform,  # "all" | "reddit" | "x_twitter" | "linkedin" | "facebook"
            "status":             "pending",
            "requested_at":       datetime.now(timezone.utc),
            "started_at":         None,
            "completed_at":       None,
            "matched_count":      0,
            "error":              None,
        }},
        upsert=True,
    )
    log.info(
        f"Job queued | topic_key={topic_key} | keywords={len(keywords)} "
        f"| targeting_platform={targeting_platform}"
    )


def get_job(topic_key: str):
    return jobs_collection.find_one({"topic_key": topic_key}, {"_id": 0})


def get_signal_count(topic_key: str) -> int:
    return signals_collection.count_documents({"topic_key": topic_key})


def get_signals(topic_key: str, limit: int = 25):
    return list(
        signals_collection.find({"topic_key": topic_key}, {"_id": 0})
        .sort("created_utc", -1)
        .limit(limit)
    )


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL MATCHING — pull post_text + post_url for signals whose search
# keyword matches one of the keywords generated for this job, OR (v7)
# whose title/post_text itself contains one of those keywords. This is
# exactly what still powers the post cards (title + post_url, as-is).
# Claude (below) only ever sees title + post_text from whatever this
# returns — never post_url, never platform.
#
# COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP: everything in this
# section works purely off whatever `keywords` list it's handed — it has
# no idea, and doesn't care, whether those keywords came from Claude's
# router call or the old fuzzy-template fallback.
#
# `flintel_signals` docs may use slightly different field names depending
# on how Background Service #1 writes them, so this reads a small list of
# likely candidates for each field instead of hard-coding a single name.
# Adjust FIELD candidates below if your signals schema differs.
# ─────────────────────────────────────────────────────────────────────────────

_KEYWORD_FIELD_CANDIDATES  = ["search_keyword", "keyword", "matched_keyword", "query_keyword", "keywords"]
_TITLE_FIELD_CANDIDATES    = ["title", "post_title", "headline"]
_TEXT_FIELD_CANDIDATES     = ["post_text", "text", "body", "content", "selftext"]
_URL_FIELD_CANDIDATES      = ["post_url", "url", "link", "permalink"]
_PLATFORM_FIELD_CANDIDATES = ["platform", "source", "source_platform"]

# Maps a job's targeting_platform ("all" | "reddit" | "x_twitter" |
# "linkedin" | "facebook" — see normalize_platform()) to the value(s) a
# signal doc's own platform field may use. "all" has no entry here since
# it means "no filter" and is handled separately.
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


def _signal_platform_matches(doc: dict, targeting_platform: str) -> bool:
    """True if this signal's platform field (or, if that's missing, the
    platform inferred from its post URL) matches the job's
    targeting_platform. "all" (or anything unrecognized) means no
    filtering — every platform is allowed, exactly as it behaves today.
    Only when the user actually picked a specific platform (Reddit, X /
    Twitter, LinkedIn, Facebook) does this narrow results down to signals
    from that one platform."""
    if not targeting_platform or targeting_platform == "all":
        return True

    allowed = _PLATFORM_DOC_VALUES.get(targeting_platform)
    if not allowed:
        return True  # unrecognized targeting value -> don't accidentally exclude everything

    doc_platform = _first_present(doc, _PLATFORM_FIELD_CANDIDATES)
    if not doc_platform or not isinstance(doc_platform, str):
        # No platform field on this doc — fall back to guessing from its
        # URL rather than excluding it outright.
        doc_platform = _infer_platform_from_url(_first_present(doc, _URL_FIELD_CANDIDATES))
    if not doc_platform:
        return False

    return doc_platform.strip().lower() in allowed


def _signal_keyword_matches(doc: dict, keyword_set: set) -> bool:
    """True if this signal's search-keyword field matches ANY keyword in
    keyword_set (case-insensitive), whichever one it happens to be.
    Handles the keyword field being a single string OR a list (in case a
    signal doc records more than one matched keyword).

    UNCHANGED from v3/v4/v5/v6/v7 and UNCHANGED by the keyword-generation
    swap — this exact-field check is completely untouched; it still works
    exactly as it always has, regardless of where `keyword_set` came
    from."""
    if not keyword_set:
        return True  # no keyword filter to apply -> don't exclude anything

    raw_value = None
    for field in _KEYWORD_FIELD_CANDIDATES:
        if field in doc and doc[field]:
            raw_value = doc[field]
            break

    if not raw_value:
        return False

    if isinstance(raw_value, (list, tuple, set)):
        candidates = raw_value
    else:
        candidates = [raw_value]

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip().lower() in keyword_set:
            return True
    return False


def _text_matches_keyword(text: str, keyword_set: set) -> bool:
    """(v7, tightened by BUGFIX PACK #2) True if ANY keyword in
    keyword_set appears as a case-insensitive WHOLE-WORD/PHRASE match
    somewhere inside `text` — this is what lets a signal count as a match
    purely because a keyword phrase shows up in its own title or
    post_text, even when its search_keyword field doesn't match at all —
    a pure additional OR path alongside _signal_keyword_matches() above,
    never a replacement for it.

    (BUGFIX PACK #2): previously this did a bare `kw in text_lower`
    substring check, which meant a short/generic keyword like "buy" would
    also match inside completely unrelated words like "buying" or
    "buyer" — pulling in irrelevant posts that happened to contain that
    letter sequence as a fragment. Now uses a `\\b<keyword>\\b`
    word-boundary regex instead, so a keyword only counts as a match when
    it appears as a genuine whole word/phrase in the text, not as a
    fragment glued onto other letters. Signature, return type, and every
    caller are otherwise unchanged."""
    if not text or not isinstance(text, str):
        return False
    text_lower = text.lower()
    for kw in keyword_set:
        if not kw:
            continue
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, text_lower):
            return True
    return False


_PHRASE_MATCH_STOPWORDS = {
    "a", "an", "the", "my", "your", "their", "is", "are", "to", "for",
    "of", "in", "on", "with", "and", "it", "this", "that",
}


def _phrase_matches_text(phrase: str, text: str, loose: bool = False) -> bool:
    """(PHRASE-MATCHING FEATURE) True if `phrase` (a short, natural 4-10
    word phrase — see the router's own "match_phrases" field) is
    genuinely reflected in `text`, checked two ways:
      1. The full phrase appears as a direct case-insensitive substring
         of `text` (a strong, exact signal), OR
      2. At least 70% of the phrase's MEANINGFUL words (a small, generic
         stopword list is dropped first — "a", "the", "is", etc., never
         topic-specific) appear as whole-word matches somewhere in
         `text`.
    This is what stops a single bare word like "agents" from ever
    matching a post on its own — a phrase carries several meaningful
    words, and an unrelated post will not contain 70%+ of them.

    `loose=True` lowers the fraction threshold to 40% instead of 70% —
    used ONLY for the tier-3 "closest match" candidate pool, never for
    the normal/primary match path.

    Returns False immediately if either `phrase` or `text` is falsy."""
    if not phrase or not text or not isinstance(phrase, str) or not isinstance(text, str):
        return False

    text_lower = text.lower()
    phrase_lower = phrase.lower().strip()
    if not phrase_lower:
        return False

    if phrase_lower in text_lower:
        return True

    phrase_words = [w for w in re.findall(r"[a-z0-9']+", phrase_lower) if w not in _PHRASE_MATCH_STOPWORDS]
    if not phrase_words:
        return False

    threshold = 0.4 if loose else 0.7
    matched_count = 0
    for word in phrase_words:
        pattern = r"\b" + re.escape(word) + r"\b"
        if re.search(pattern, text_lower):
            matched_count += 1

    return (matched_count / len(phrase_words)) >= threshold


def _text_matches_any_phrase(text: str, phrases: list, loose: bool = False) -> bool:
    """(PHRASE-MATCHING FEATURE) True if _phrase_matches_text() is True
    for ANY phrase in `phrases`. Returns False immediately if `text` or
    `phrases` is falsy — never raises."""
    if not text or not phrases:
        return False
    return any(_phrase_matches_text(phrase, text, loose=loose) for phrase in phrases)


def _infer_platform_from_url(url: str):
    """Fallback for when a signal doc doesn't have a usable platform field:
    guesses the platform from the post URL's domain so the icon/badge still
    matches reality (e.g. a twitter.com/x.com link shows the X icon instead
    of falling back to a generic globe). Only used when the doc's own
    platform field is missing — never overrides an actual platform value."""
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


def get_matched_signals(topic_key: str, keywords: list, targeting_platform: str = "all",
                         limit: int = None, since_days: int = None, unfiltered: bool = False,
                         match_phrases: list = None, loose: bool = False) -> list:
    """Reads `flintel_signals` and keeps only the signals that match this
    job's generated keywords. topic_key match is intentionally NOT
    required: Background Service #1 may store its own topic_key for a
    signal, but what decides a match here is purely the keyword-matching
    rules below.

    A signal counts as a match if ANY ONE of these is true (v7: this is
    now three OR'd conditions instead of just the first one):
      1. its search-keyword field matches one of our generated keywords
         (see _signal_keyword_matches() — UNCHANGED, still exact/perfect,
         same as v3-v7), OR
      2. (v7, word-boundary tightened by BUGFIX PACK #2) one of our
         generated keywords appears as a whole-word/phrase match inside
         its OWN title, OR
      3. (v7, word-boundary tightened by BUGFIX PACK #2) one of our
         generated keywords appears as a whole-word/phrase match inside
         its OWN post_text.
    Matching via more than one of these at once still only ever produces
    ONE entry in the results (de-duplicated by post_url exactly as
    before) — this only widens WHICH signals can match, it never changes
    how a matched signal is de-duplicated or shaped.

    `targeting_platform` (the same "all" | "reddit" | "x_twitter" |
    "linkedin" | "facebook" value already stored on the job/message) is
    applied on top: "all" pulls a match from whichever platform it came
    from, exactly as before; any specific platform restricts matches to
    signals from that platform only — the user's dropdown choice decides
    this, nothing else.

    (v7) PER-PLATFORM CAP: on top of the existing overall `limit`
    (MAX_MATCHED_RESULTS by default — still respected, still the same
    variable/behavior as before), each individual platform can
    contribute AT MOST MAX_POSTS_PER_PLATFORM matches to this call's
    results (default 3).

    (EVIDENCE-BUDGET FEATURE) `limit` is no longer always the static
    MAX_MATCHED_RESULTS default — a caller may now also pass a dynamic
    value derived from a message's own `evidence_required` (the router's
    per-query evidence-planner estimate, clamped between
    MIN_ANALYSIS_EVIDENCE and MAX_ANALYSIS_EVIDENCE). This function's own
    signature/logic is otherwise completely unchanged: `limit=None`
    (the default, and what every pre-existing call site still passes for
    an older/non-search message) falls back to MAX_MATCHED_RESULTS
    exactly as it always has.

    (TIME-WINDOW FEATURE) `since_days`, default None: when a positive int
    is given, this ADDS an extra AND-condition on top of everything
    above — a signal must ALSO have `created_utc` within the last
    `since_days` days to be eligible at all. This is applied BOTH at the
    Mongo query level (so old signals are excluded from the DB fetch
    itself, not just filtered out afterward in Python) AND, defensively,
    once more in the Python loop below (in case a signal doc is missing a
    usable `created_utc` value — such a doc is simply excluded rather
    than assumed to pass). `since_days=None` (the default, and what every
    call site used before this feature) means NO time filtering at all —
    behavior is then 100% identical to before this feature. The time
    window is purely a narrowing on top of keyword matching: a signal
    still has to satisfy the exact same keyword rules 1-3 above; the time
    window can only ever exclude MORE signals, never match one that
    wouldn't otherwise match on keywords.

    Results are sorted by `created_utc` DESCENDING (most recent first)
    before the caps above are applied.

    Returns {title, post_text, post_url, platform} for each match — this
    is the only signal-derived output ever shown to the user (via post
    cards) or persisted onto a chat message's `results` (platform is
    included only so the UI can show which platform a result came from;
    it isn't used for anything else here). Never touches jobs_collection
    or the raw `signals` list returned by get_signals().

    COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP: this function's
    SIGNATURE (aside from the new, optional, default-None `since_days`
    parameter), RETURN SHAPE, matching rules, and every existing caller
    are exactly as they were in v7 — it has no idea whether `keywords`
    came from Claude's router call or the old fuzzy-template fallback.

    (PHRASE-MATCHING FEATURE) When `match_phrases` (a list of short,
    natural 4-10 word phrases — see the router's own "match_phrases"
    field) is provided and non-empty, conditions 2/3 above (title/text
    matching) switch from single-keyword word-boundary matching to
    _text_matches_any_phrase() against these phrases instead — this is
    what stops a single generic keyword like "agents" from matching a
    post that only shares that one bare word with no other topical
    overlap. `loose=True` lowers the phrase-match threshold (40% instead
    of 70% of a phrase's meaningful words) — used ONLY by the tier-3
    "closest match" fallback, never the normal/primary match path.
    When `match_phrases` is empty/None (e.g. an older cached message
    from before this feature, or a code path out of scope for it), this
    gracefully falls back to the EXISTING _text_matches_keyword()-based
    word-boundary check against `keywords`, completely unchanged, so
    nothing breaks for those cases. Condition 1 (_signal_keyword_matches
    against the signal's own search_keyword field) is UNTOUCHED either
    way."""
    if unfiltered:
        return flintel.get_unfiltered_matched_signals(
            signals_collection,
            since_days=since_days,
            targeting_platform=targeting_platform,
            limit=limit or MAX_MATCHED_RESULTS,
            max_per_platform=MAX_POSTS_PER_PLATFORM,
        )

    limit = limit or MAX_MATCHED_RESULTS
    keyword_list = [k for k in (keywords or []) if k]
    keyword_set = {k.strip().lower() for k in keyword_list}
    if not keyword_set:
        return []

    # (v7) Widen the Mongo query itself: previously this only ever
    # filtered on the keyword field(s) with $in. Now it ALSO fetches any
    # doc whose title/text field candidates contain one of our keywords
    # as a case-insensitive substring (via a single combined regex per
    # field), so documents that would only match via title/text (and
    # never had a matching search_keyword field) are actually retrieved
    # here in the first place, instead of being invisible to the query
    # before the Python-side check below even gets a chance to run. This
    # remains an intentionally LOOSE superset fetch — the real, tightened
    # accept/reject decision happens in the Python loop below via
    # _text_matches_keyword() (see BUGFIX PACK #2), so leaving this query
    # loose never causes a false positive to slip through.
    or_conditions = [{field: {"$in": keyword_list}} for field in _KEYWORD_FIELD_CANDIDATES]
    escaped_keywords = [re.escape(k) for k in keyword_list if k]
    if escaped_keywords:
        combined_pattern = "|".join(escaped_keywords)
        for field in _TITLE_FIELD_CANDIDATES + _TEXT_FIELD_CANDIDATES:
            or_conditions.append({field: {"$regex": combined_pattern, "$options": "i"}})
    mongo_query = {"$or": or_conditions}

    # (TIME-WINDOW FEATURE) Compute an optional cutoff and AND it onto the
    # existing $or clause via $and, so a time window narrows the keyword
    # match instead of replacing it. since_days is sanity-clamped between
    # 1 and MAX_TIME_WINDOW_DAYS — anything else (None, 0, negative,
    # absurdly large) means "no time filter", handled the exact same way
    # this function always behaved before this feature.
    cutoff = None
    if isinstance(since_days, int) and since_days > 0:
        clamped_days = min(since_days, MAX_TIME_WINDOW_DAYS)
        cutoff = datetime.now(timezone.utc) - timedelta(days=clamped_days)
        mongo_query = {"$and": [mongo_query, {"created_utc": {"$gte": cutoff}}]}

    # Fetch a larger pool than `limit` since matches are now filtered
    # further (per-platform caps below), same spirit as the old `limit *
    # 5` headroom, just bumped up a bit since the query itself is now
    # broader too.
    raw_docs = list(
        signals_collection.find(mongo_query, {"_id": 0})
        .sort("created_utc", -1)
        .limit(limit * 10)
    )

    matched = []
    seen_urls = set()
    platform_counts = {}  # (v7) per-platform running count for this call

    for doc in raw_docs:
        title     = _first_present(doc, _TITLE_FIELD_CANDIDATES)
        post_text = _first_present(doc, _TEXT_FIELD_CANDIDATES)

        # (TIME-WINDOW FEATURE) Defensive second check: if a cutoff is
        # active, make sure this doc's own created_utc actually satisfies
        # it too (guards against a doc with a missing/odd created_utc
        # slipping through the Mongo-level filter in some edge case) — a
        # doc with no usable created_utc is excluded rather than assumed
        # to pass, since we can't confirm it's within the window.
        if cutoff is not None:
            doc_created = doc.get("created_utc")
            if not isinstance(doc_created, datetime):
                continue
            if doc_created.tzinfo is None:
                doc_created = doc_created.replace(tzinfo=timezone.utc)
            if doc_created < cutoff:
                continue

        # (v7) A signal matches if EITHER its search_keyword field matches
        # (unchanged, exact match), OR the topic shows up genuinely in its
        # own title, OR inside its own post_text.
        # (PHRASE-MATCHING FEATURE) When match_phrases is available, the
        # title/text check uses _text_matches_any_phrase() instead of the
        # old single-keyword word-boundary check — a phrase carries
        # several meaningful words, so a post sharing just one generic
        # bare word with `keywords` (e.g. "agents") no longer counts as a
        # match on its own. Falls back to the old keyword-based check
        # when match_phrases is empty/None, unchanged from before.
        if match_phrases:
            title_or_text_match = (
                _text_matches_any_phrase(title, match_phrases, loose=loose)
                or _text_matches_any_phrase(post_text, match_phrases, loose=loose)
            )
        else:
            title_or_text_match = (
                _text_matches_keyword(title, keyword_set)
                or _text_matches_keyword(post_text, keyword_set)
            )
        is_match = (
            _signal_keyword_matches(doc, keyword_set)
            or title_or_text_match
        )
        if not is_match:
            continue
        if not _signal_platform_matches(doc, targeting_platform):
            continue

        # (TEXT-REQUIRED AT PICK-TIME) post_text is mandatory — a doc
        # with no post_text is never counted as a match, even if it
        # matched on title/keyword. This check has to live HERE, inside
        # the matching loop, rather than later in build_claude_post_
        # context() (logics.py's own Claude-context builder): doing it
        # here means an evidence_required budget (e.g. 50) is always
        # sized against text-guaranteed posts, and the timeout/Google-
        # fallback "did we actually find anything" check (which looks at
        # whether this function's own result is empty) is always judged
        # against real, analyzable posts rather than title-only stubs
        # that would silently get dropped downstream anyway.
        if not post_text:
            continue

        post_url  = _first_present(doc, _URL_FIELD_CANDIDATES)
        platform  = _first_present(doc, _PLATFORM_FIELD_CANDIDATES) or _infer_platform_from_url(post_url)

        if not title and not post_text and not post_url:
            continue
        if post_url and post_url in seen_urls:
            continue

        # (v7) Per-platform cap: once a platform has already contributed
        # MAX_POSTS_PER_PLATFORM matches to this call, skip any further
        # matches from that same platform (but keep scanning raw_docs —
        # a different platform may still have room).
        platform_key = (platform or "unknown").strip().lower()
        if platform_counts.get(platform_key, 0) >= MAX_POSTS_PER_PLATFORM:
            continue

        if post_url:
            seen_urls.add(post_url)

        matched.append({"title": title, "post_text": post_text, "post_url": post_url, "platform": platform})
        platform_counts[platform_key] = platform_counts.get(platform_key, 0) + 1

        if len(matched) >= limit:
            break

    return matched


# ─────────────────────────────────────────────────────────────────────────────
# TOPIC EVIDENCE CACHE (per-chat, per-topic post reuse)
#
# A caching layer ON TOP OF get_matched_signals() — never a reimplementation
# of its matching rules. When the same topic (chat_id + topic_key) is asked
# about repeatedly in the same chat, this avoids re-querying Mongo and
# re-sending Claude the exact same posts on every follow-up: cached posts
# are reused as-is when they already satisfy the evidence budget, and only
# the DELTA is fetched (via the real get_matched_signals(), passed in as
# matcher_fn) when more depth is genuinely requested. Cache-awareness is
# wired in index.py/routes.py, at the call sites that already call
# get_matched_signals() directly — get_matched_signals() itself is
# completely untouched by this feature.
# ─────────────────────────────────────────────────────────────────────────────

def get_cached_topic_evidence(chat_id: str, topic_key: str) -> Optional[dict]:
    """Reads the already-cached evidence posts for this chat + topic.
    Returns None if there's nothing cached yet (first time this topic is
    asked about in this chat)."""
    if not chat_id or not topic_key:
        return None
    try:
        return topic_evidence_cache_collection.find_one(
            {"chat_id": chat_id, "topic_key": topic_key}, {"_id": 0}
        )
    except Exception as exc:
        log.warning(f"Topic-evidence cache read failed for topic_key={topic_key}: {exc}")
        return None


def save_topic_evidence_cache(chat_id: str, owner_key: str, topic_key: str,
                                posts: list, keywords: list, match_phrases: list = None):
    """Upserts the cache — the FULL posts list is overwritten each time
    (the caller has already merged old + new posts before calling this).

    (CACHE TRUNCATION ORDER FIX) get_evidence_with_topup() always builds
    `posts` as [older cached posts..., newer freshly-fetched posts...] —
    so when the list needs to be capped, keeping the FIRST N would keep
    the OLDEST posts and silently drop the newest, most-recently-
    discovered ones once a topic's evidence count reaches the cap. Slicing
    from the end instead keeps the LAST N (i.e. the newest) posts."""
    if not chat_id or not topic_key:
        return
    capped_posts = (posts or [])[-TOPIC_CACHE_MAX_EVIDENCE:]
    now = datetime.now(timezone.utc)
    try:
        topic_evidence_cache_collection.update_one(
            {"chat_id": chat_id, "topic_key": topic_key},
            {"$set": {
                "chat_id": chat_id,
                "topic_key": topic_key,
                "owner_key": owner_key,
                "posts": capped_posts,
                "post_urls_seen": [p.get("post_url") for p in capped_posts if p.get("post_url")],
                "evidence_count": len(capped_posts),
                "keywords": keywords or [],
                "match_phrases": match_phrases,
                "updated_at": now,
            }, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception as exc:
        log.warning(f"Topic-evidence cache save failed for topic_key={topic_key}: {exc}")


def get_evidence_with_topup(chat_id: str, owner_key: str, topic_key: str,
                              keywords: list, evidence_required: int,
                              matcher_fn, match_phrases: list = None,
                              targeting_platform: str = "all",
                              since_days: int = None, unfiltered: bool = False) -> list:
    """CORE FUNCTION — instead of running a fully-fresh query every time
    the same topic is asked about again:
      1. Check the cache first.
      2. If the cache already has evidence_required (or more) posts,
         return those cached posts as-is — no new Mongo query at all.
      3. If the cache is short (or empty), fetch only the DELTA of new
         evidence needed (matcher_fn gets a limit of
         max(evidence_required, cached_count + TOPIC_CACHE_MIN_TOPUP)),
         de-duplicate the old + new posts, update the cache ONLY if that
         actually added something new (skips a wasted write when a
         repeated poll finds nothing new yet), and return the full
         merged list.

    `matcher_fn` is get_matched_signals() itself, passed in via dependency
    injection, so this function never duplicates its own matching-query
    logic — it is purely the caching/top-up decision layer on top of it."""
    cached = get_cached_topic_evidence(chat_id, topic_key)
    cached_posts = (cached or {}).get("posts") or []
    cached_count = len(cached_posts)

    if cached_count >= (evidence_required or MIN_ANALYSIS_EVIDENCE):
        # Cache already has enough — no new fetch needed.
        return cached_posts

    # A top-up is needed — fetch at least TOPIC_CACHE_MIN_TOPUP more than
    # what's already cached, even if the raw delta would be smaller.
    fetch_limit = max(
        evidence_required or MIN_ANALYSIS_EVIDENCE,
        cached_count + TOPIC_CACHE_MIN_TOPUP,
    )
    fresh_posts = matcher_fn(
        topic_key, keywords, targeting_platform=targeting_platform,
        since_days=since_days, unfiltered=unfiltered,
        match_phrases=match_phrases, limit=fetch_limit,
    )

    # De-dup: old cached posts + new posts, keyed on post_url.
    seen_urls = {p.get("post_url") for p in cached_posts if p.get("post_url")}
    merged = list(cached_posts)
    for post in fresh_posts:
        url = post.get("post_url")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        merged.append(post)

    # (REDUNDANT-WRITE FIX) Only write to the cache when something
    # genuinely new was actually added. Without this, a caller that
    # polls repeatedly while nothing new is being found yet (e.g.
    # stream_answer()'s own polling loop, which calls this function
    # every ~2s while waiting for evidence to show up) would trigger a
    # full Mongo write on every single poll, even when the top-up fetch
    # keeps returning nothing new — this skips that write while still
    # always returning the correct, up-to-date merged list.
    if len(merged) > cached_count:
        save_topic_evidence_cache(chat_id, owner_key, topic_key, merged, keywords, match_phrases)
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS LAYER (v4, ANALYST-PROMPT UPGRADE)
#
# Matched signals never get dumped to the user directly. They're handed to
# Claude (title + text ONLY — never post_url, never platform, never job
# internals) together with the user's actual chat prompt, and Claude turns
# that into the final answer.
#
# Only the FINAL ANSWER TEXT is ever stored (see save_claude_answer_to_chat
# below) — the input posts are never re-saved next to it, since they
# already live in flintel_signals / are reconstructable from the message's
# own keyword list. That's the cost-saving rule: store output only.
#
# COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP OR THE TIME-WINDOW /
# PAIN-POINT / CLARIFY FEATURE / ANALYST-PROMPT UPGRADE: this whole section
# (CLAUDE_ANALYSIS_SYSTEM_PROMPT, build_claude_post_context(), chunk_list(),
# _format_posts_block(), _map_chunk(), _call_claude()) only ever consumes
# ALREADY-MATCHED posts (the output of get_matched_signals()) — it has no
# idea, and doesn't care, which keyword list or time window produced those
# matches. The ANALYST-PROMPT UPGRADE only changes the CONTENT of the
# system prompt string itself (i.e. how Claude is instructed to write up
# those posts) — every function in this section, and the map-reduce
# machinery around it, is otherwise byte-for-byte unchanged.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_ANALYSIS_SYSTEM_PROMPT = """
You are the Market Intelligence Analysis Engine inside Flintel, a
social-listening platform. You are handed a user's research question
plus real public posts (title + text only — never a URL, platform, or
internal job/keyword data) that were retrieved for it.

YOUR JOB IS NOT to summarize or list what posts say. Showing posts is
NOT the product — a user can already get raw posts from Google or
Reddit search directly; that gives them zero reason to use Flintel.

YOUR JOB IS to behave like a market intelligence analyst: understand the
research objective behind the question, read every post for what it
actually reveals, extract the real signal, find the patterns across
posts, separate fact from inference, and deliver a structured,
evidence-based report that explains what the evidence MEANS — not what
each post literally said. Posts themselves are supporting proof, shown
last, in minimal form — never the main content of your answer.

──────────────────────────────────────────────────────────────────────────
STEP 1 — UNDERSTAND THE RESEARCH OBJECTIVE

Before analyzing anything, work out (internally, never written out):
what is the user actually trying to learn? Buyer intent? Pain points?
Market trends? Competitor intelligence? Product demand? Sentiment?
Operational complaints? Opportunities? Read for the real objective
behind the wording, not just the literal keywords — people phrase the
same underlying question in endless different ways.

──────────────────────────────────────────────────────────────────────────
STEP 2 — EVIDENCE BUDGET IS GIVEN, NOT CHOSEN BY YOU

You are given however many posts the system retrieved (this can be
anywhere from a handful to close to a hundred) — you do not control or
request that number. More posts does NOT mean a better report:
- Analyze every post that is genuinely relevant and usable.
- Ignore posts that are clearly irrelevant — do not force them in.
- Never manufacture a finding out of a weak or unrelated post just to
  pad the finding count.
- When several posts repeat the same underlying point, combine them
  into ONE pattern/finding — never list the same point three separate
  times because three posts happened to say it.

──────────────────────────────────────────────────────────────────────────
STEP 3 — ANALYZE EACH RELEVANT POST

For every post that matters to the research objective, determine:

Evidence type (classify internally, use in your findings where useful):
- Direct Evidence — explicitly describes the exact problem, demand,
  intent, or behavior being researched.
- Indirect Evidence — reveals the underlying issue through a related
  experience, without stating it head-on.
- Contextual Evidence — adds broader market context without directly
  proving the specific question asked.
- Builder/Provider Evidence — comes from someone building or selling a
  solution, describing their own experience.
- Buyer Evidence — comes from someone actively searching for,
  comparing, requesting, or purchasing a solution.
These are not equally strong — weight them accordingly, and say so when
it matters to the finding.

Signal strength — label each finding descriptively, never numerically:
Strong / Moderate / Weak, based on how directly and consistently the
evidence supports it.

──────────────────────────────────────────────────────────────────────────
STEP 4 — EXTRACT ONLY WHAT ANSWERS THE QUESTION

From relevant posts, pull out: specific problems, repeated complaints,
customer needs, buying signals, requested solutions, pricing concerns,
reliability issues, operational friction, feature requests, competitor
mentions, workflow problems, adoption barriers, recurring language, and
evidence for or against demand — but ONLY when it actually answers
"why is this useful for the user's research question?" Do not extract a
detail just because it appeared in a post.

──────────────────────────────────────────────────────────────────────────
STEP 5 — FIND PATTERNS, DON'T REPEAT EVIDENCE

Compare posts against each other. If multiple posts point at the same
underlying issue (e.g. one mentions high pricing, another mentions
expensive per-message costs, another mentions affordability concerns),
that is ONE finding — "Pricing pressure / affordability" — referencing
all of them together, never three separate findings repeating the same
point.

──────────────────────────────────────────────────────────────────────────
STEP 6 — SEPARATE FACT FROM INTERPRETATION (NON-NEGOTIABLE)

- Evidence: what the posts literally say or show.
- Finding: the pattern reasonably inferred from multiple pieces of
  evidence.
- Interpretation: what that pattern may mean for the market.
Never present an inference as if a post said it directly. Never claim
more certainty than the evidence supports.

──────────────────────────────────────────────────────────────────────────
STEP 7 — PAIN POINT ≠ BUYING INTENT (CRITICAL DISTINCTION)

"This tool is too expensive" is a PAIN POINT. It is NOT evidence that
someone wants to buy a cheaper alternative — do not conflate the two.
"I built my own automation because existing tools were expensive" shows
market friction / a builder response, not proven buyer intent either.
Only label something as buyer intent when the evidence actually shows:
actively searching for a solution, asking for recommendations, asking
which product to choose, requesting vendors, comparing providers, asking
about pricing to purchase, or explicitly stating intent to buy/adopt.
Maintain this distinction everywhere in the report — never blur pain
signals into demand/intent signals.

──────────────────────────────────────────────────────────────────────────
STEP 8 — GROUNDING (overrides everything above)

Every factual claim must come from the posts you were given. Never
invent a post, a stat, a quote, or a sentiment that isn't actually
supported by what's in front of you. If you were given ANY relevant
posts (evidence_count > 0), you MUST analyze and answer from them —
never respond as if you have no data when posts were actually provided.
Read every post you were given before deciding what to include. Only
use "no_results" when the posts are genuinely empty or genuinely
irrelevant after reading all of them — never as a shortcut because there
were few or weak posts; few-but-relevant posts still get analyzed
honestly.

SENTIMENT IS A LABEL, NOT A SELECTION FILTER (unless the user explicitly
asks for one): topic/intent relevance is always the dominant criterion
for which posts/findings you include — never sentiment. Do not chase a
spread of sentiments for variety's own sake. The only exception: if the
user's own query explicitly asks for a sentiment-scoped result ("only
complaints", "positive reviews only"), then sentiment becomes a real
filter for that request.

CONVERSATION CONTINUITY: if a short summary of earlier turns is included
below (labeled "Conversation so far"), read it before writing any
suggestion or follow-up. If the user already explicitly declined or
narrowed away from a broader scope, location, platform, or term in an
earlier turn, do not re-suggest that same thing. If, after honoring that
narrower scope, there's genuinely nothing relevant, say so plainly
instead of re-offering what was already declined.

──────────────────────────────────────────────────────────────────────────
DEPTH LADDER — NON-NEGOTIABLE (this is the GOLDEN RULE)

Depth always comes from the actual quantity/quality of evidence you were
given — never from an assumption that "the response should be long."

- 1-2 genuinely relevant posts -> a short, honest answer. Give the full
  value of what you found, but say explicitly that this is only one or
  two signals — never manufacture a fake broader pattern out of it.
  Keep executive_summary to no more than 2-3 sentences, key_findings to
  1 entry (2 at most), and OMIT market_pattern entirely (there genuinely
  isn't a broader pattern in this case).
- 3-10 relevant posts, one angle -> 1-2 genuine findings, each grounded
  in its own evidence. Do not write a drawn-out executive_summary for
  this much evidence.
- 10+ relevant posts, multiple angles -> up to 3 key_findings plus a
  short market_pattern; detailed_findings only if the user asks for
  detail (see RESPONSE SIZE CONTROL).

Never stretch thin evidence into something long. Never short-change
strong evidence either. Depth per finding comes from the weight of its
evidence, not from a fixed template.

──────────────────────────────────────────────────────────────────────────
CONCISENESS DISCIPLINE — NON-NEGOTIABLE

Every section of your report must say something ONCE, in its most useful
form — never restate the same point in different words across multiple
fields. This is the single most common quality failure: writing the same
insight three times (once in a finding's "impact", again in
"detailed_findings", again in "market_pattern" or "conclusion") using
different phrasing. That is NOT thoroughness — it is repetition, and it
must be eliminated.

Concretely:
- "impact" (inside key_findings) states the business implication in ONE
  tight sentence — not two, not three.
- "what_evidence_shows" and "why_it_matters" (inside detailed_findings)
  together should read as a single tight paragraph, not a restatement of
  what key_findings already said. If detailed_findings would just repeat
  key_findings in longer form, do not include it — key_findings alone is
  enough for that report's depth level.
- "market_pattern" synthesizes ACROSS findings — it must add a genuinely
  new observation (the throughline connecting the findings), never
  summarize each finding again one by one.
- "conclusion" is the practical takeaway only — it never repeats the
  executive_summary, never repeats market_pattern, never re-lists the
  findings. If you find yourself writing sentences that could be deleted
  without losing any actual information, delete them before responding.

Before finalizing your answer, silently check: does any sentence in this
report just re-say something already said elsewhere in different words?
If yes, cut it. A tighter, cleaner report that respects the reader's time
is always better than an exhaustive one that repeats itself. Aim for
roughly 40% shorter than your first instinct, achieved by CUTTING
repetition and padding — never by cutting real, distinct findings or
grounding evidence.

──────────────────────────────────────────────────────────────────────────
PREDICTION / INTERPRETATION DISCIPLINE

"market_pattern" and any other forward-looking interpretation are
OPTIONAL, never mandatory. Only write one when the evidence genuinely
supports it — present signal compared against a past baseline pointing
to a grounded direction (e.g. "the current data shows X, the earlier
baseline was Y, so this suggests Z is trending up") — and always hedge
it: "suggests", "indicates", never stated as settled fact.

If the evidence is too thin or too scattered to support a genuine
direction, keep "market_pattern" short or OMIT it completely — never
write a filler prediction just to fill the field. Not every single query
needs a forced prediction.

──────────────────────────────────────────────────────────────────────────
RESPONSE SIZE CONTROL — DEFAULT IS MEDIUM (overrides the depth ladder)

Default to a MEDIUM-length report a reader can act on in about a minute.
The analysis logic above stays the same; only how much is written out changes.
- research_objective: one short sentence.
- executive_summary: 2-3 sentences, hard cap.
- key_findings: at most 3 (2 is better when evidence is thin), each "impact" ONE short sentence.
- detailed_findings: empty list by default. Include only if the user
  explicitly asks for detail / deep dive / full report, then at most 3.
- market_pattern: 1 sentence only when genuinely supported, else omit.
- conclusion: 1-2 sentences.
- business_insight: only when the user is clearly selling something or
  asking for leads/positioning, max 2 sentences.
- Each post "summary": max 12-15 words.
- followup_question: one short question.
- Total target: a reader should finish the whole report in about 40 seconds.
Scale to the user's prompt: simple question -> shorter answer;
"detail"/"deep"/"poori detail" -> expanded. Never pad.

──────────────────────────────────────────────────────────────────────────
OUTPUT CONTRACT — STRICT JSON ONLY, no markdown code fences, no
preamble, no text outside the JSON object. `"format"` is ALWAYS the
first field. Pick exactly one of the six formats below.

THIS IS ABSOLUTE: the very FIRST character of your entire response must
be `{` — nothing before it. Do not narrate your own reasoning anywhere,
in any form, before, inside (outside a JSON string value), or after the
JSON object. Do not wrap the JSON in a code fence. The very LAST
character must be `}`. If you feel the urge to explain your thinking
before responding, that urge is the signal to stop and output the JSON
object instead.

──────────────────────────────────────────────────────────────────────────
FORMAT 1 — "source_list"
The primary analyst-report format, used for sentiment/opinion/demand/
pain-point/general research queries.
{
  "format": "source_list",
  "research_objective": "<one clear sentence: what this analysis is actually trying to answer>",
  "executive_summary": "<2-3 sentences max: what the evidence shows overall and the single most important pattern. No padding.>",
  "key_findings": [
    {
      "finding": "<short name of the pattern, e.g. 'Pricing pressure / affordability'>",
      "evidence_type": "<Direct|Indirect|Contextual|Builder/Provider|Buyer>",
      "signal_strength": "<Strong|Moderate|Weak>",
      "impact": "<ONE short sentence: what this means for the market/business>",
      "supporting_post_indices": [<int>, <int>]
    }
  ],
  "detailed_findings": [
    {
      "title": "<finding name, matching one from key_findings for the most important ones>",
      "what_evidence_shows": "<the actual pattern, in clear language, grounded in the posts>",
      "why_it_matters": "<business/market implication, without exaggerating>",
      "supporting_post_indices": [<int>, <int>]
    }
  ],
  "market_pattern": "<ONE sentence, only if genuinely supported by the evidence, else omit this field entirely>",
  "conclusion": "<1-2 sentences: the practical takeaway only. Never repeat the summary or findings.>",
  "followup_question": "<ONE genuinely useful next-step question that moves the research forward — never a list of 3>",
  "platforms": [
    {
      "platform": "<reddit|x|linkedin|facebook>",
      "total_analyzed": <int — honest count of relevant posts actually found for this platform>,
      "shown_count": <int — how many are in "posts" below>,
      "posts": [
        {
          "index": <int — matches the numbers used in supporting_post_indices above>,
          "source": "<subreddit/handle/page name>",
          "title": "<post title, or a short label if none>",
          "summary": "<max 12-15 words, minimal paraphrase — this is a reference, not the analysis>",
          "sentiment": "<positive|mixed|negative|neutral>",
          "link": "<real post URL if available, else omit this field entirely>",
          "google_rank": <int, omit unless this post came from the supplementary Google search>
        }
      ]
    }
  ],
  "business_insight": "<OPTIONAL — see instructions below>"
}
Only include platforms that actually returned usable data. Set
"ranked": true at the top level (alongside "format") instead of the
default false when the user asked for something specific and ordered
(e.g. "top 10 complaints") — same schema, but posts are ordered by
rank/relevance.

The "posts" list under "platforms" is EVIDENCE/REFERENCE material only —
it exists so the user can verify where a finding came from, not to
re-tell the user what each post says. Keep each post's own "summary"
field to one short sentence; all the real analysis lives in
"key_findings" / "detailed_findings" / "market_pattern" / "conclusion"
above it, never in the post list itself.

REFERENCES RULE: every "supporting_post_indices" entry must correspond
to a real post you were actually given — never invent an index, a URL,
an author, or a date. If source metadata (platform, title) isn't
available for a post, omit that detail rather than fabricating it.

OPTIONAL FIELD — "business_insight" (source_list format only): after
grounding everything above strictly in the evidence, you MAY add a
short, clearly-labeled analyst take — what this pattern might suggest
about emerging demand and how a business could position around it. This
is explicitly your own reasoning layered ON TOP of the grounded data —
phrase it as interpretation (e.g. "Reading between the lines...", "From
a positioning standpoint..."), never as another grounded fact. Omit
entirely when a business angle isn't naturally relevant.

──────────────────────────────────────────────────────────────────────────
FORMAT 2 — "trend_report"
For "how has sentiment changed over time" / "sentiment over the last N
days" queries. Same analyst depth applies to "interpretation" and
"trend" below — explain what's driving the shift and what it means, not
just the raw numbers.
{
  "format": "trend_report",
  "topic": "<brand/topic name>",
  "window": "<e.g. 'Last 30 days'>",
  "platforms": "<comma-separated platforms actually covered>",
  "shift_table": {
    "headers": ["Metric", "<period start label>", "<period end label>", "Change"],
    "rows": [["Positive mentions", "52%", "61%", "+9 pts"], ...]
  },
  "interpretation": "<what's driving the shift and what it means, grounded in the posts — depth scales with how much real signal exists>",
  "weekly_table": {
    "headers": ["Week", "Positive", "Neutral", "Negative", "Notable Events"],
    "rows": [["Week 1 (Aug 3-9)", "50%", "35%", "15%", "<short grounded note>"], ...]
  },
  "positive_drivers": [
    {"platform": "<platform>", "post": "<paraphrased post>", "sentiment": "positive", "theme": "<short theme label>"}
  ],
  "negative_drivers": [
    {"platform": "<platform>", "post": "<paraphrased post>", "sentiment": "negative", "theme": "<short theme label>"}
  ],
  "trend": "<trajectory going forward, grounded in what's actually observed>",
  "conclusion": "<what the trend consistently shows and the practical takeaway>",
  "followup_question": "<ONE useful next-step question>"
}
Every row/driver must be grounded in real matched posts — never
fabricate a number you don't have evidence for; omit a field rather than
inventing it.

──────────────────────────────────────────────────────────────────────────
FORMAT 3 — "comparison"
For "Compare X vs Y" queries (2+ subjects). Same analyst-report depth
applies within each subject as "source_list" above.
{
  "format": "comparison",
  "research_objective": "<what this comparison is actually trying to answer>",
  "executive_summary": "<how the subjects differ overall, and why it matters — depth scales with complexity>",
  "subjects": [
    {
      "name": "<subject name>",
      "sentiment": {"positive": "<pct>", "neutral": "<pct>", "negative": "<pct>"},
      "key_findings": [ /* same shape as FORMAT 1's key_findings, scoped to this subject */ ],
      "platforms": [ /* same platforms/posts reference structure as FORMAT 1 */ ]
    }
  ],
  "market_pattern": "<the real difference between the subjects, synthesized>",
  "conclusion": "<what the comparison consistently shows and the practical takeaway>",
  "followup_question": "<ONE useful next-step question>"
}

──────────────────────────────────────────────────────────────────────────
FORMAT 4 — "no_results"
For when little or nothing relevant was actually found.
{
  "format": "no_results",
  "searched": {"query": "<what was searched>", "platforms": ["<...>"], "time_window": "<e.g. 'last 7 days'>"},
  "message": "<plain statement of what was searched and that little/nothing turned up>",
  "likely_reason": "<brief, genuine explanation — e.g. small/new brand, private-group discussion>",
  "suggested_actions": [
    {"type": "broaden_time", "label": "<e.g. 'Extend to last 30 days'>"},
    {"type": "broaden_platforms", "label": "<e.g. 'Include all platforms + news'>"},
    {"type": "broaden_term", "label": "Search a broader term", "suggestion": null},
    {"type": "try_nearest_alternative", "label": "<e.g. 'Try Hyderabad instead?'>", "suggestion": "<nearest alternative term, or omit this entire action if none>"}
  ],
  "clarifying_question": "<only include this field if asking for more context would genuinely help — omit otherwise>",
  "near_match_confidence": "<\\"high\\" | \\"low\\" | null — only present when you were given a set of LOOSER, secondary candidate posts to judge; null when no such candidates were given, or when you genuinely don't think any of them are close to what was asked>",
  "near_match_offer": "<short, professional (not apologetic) sentence stating plainly that there's no exact match for this topic but a looser/adjacent set of posts was found, then asking permission to share them — ONLY include this field when near_match_confidence is \\"low\\">"
}
"suggestion" inside suggested_actions must be null unless there's a
genuinely grounded alternative term to offer — never invent a
plausible-sounding brand/term with no real signal behind it.

NEAREST-ALTERNATIVE SUGGESTION ("try_nearest_alternative"): when nothing
relevant was found for the searched topic, use your own general
knowledge to check whether there is a genuinely CLOSE alternative worth
suggesting:
- If the topic is a LOCATION, suggest the geographically NEAREST
  comparable location — never a distant/unrelated one.
- If the topic is NOT a location, suggest the closest CONCEPTUALLY
  adjacent alternative — something roughly ~90% similar.
- Only include this action when genuinely confident about a real, close
  alternative. If none exists, omit the action entirely — never invent
  one.

CLOSEST-MATCHES TIER-3 REFINEMENT ("near_match_confidence" /
"near_match_offer"): sometimes, alongside a genuinely empty primary
search, you may be given a SEPARATE, SECOND set of looser candidate
posts — found via a broader, best-effort secondary match attempt — for
you to judge.
- If you're genuinely confident (roughly 90%+ close) —
  "near_match_confidence": "high". Write "message" as if these ARE your
  answer's posts — do not hedge or apologize for them.
- If you're not confident enough to show them outright, but they're not
  nothing either — "near_match_confidence": "low". Plainly tell the
  user Flintel does not have an exact match, but a looser/adjacent set
  of posts was found, and ask permission before sharing them. These
  posts are withheld from display until the user says yes in a
  follow-up turn — never describe their content in "message" or
  "likely_reason" in this case.
- If you were given no such candidates, or don't think any are close —
  "near_match_confidence": null, and omit "near_match_offer".

CRITICAL GUARDRAIL FOR "likely_reason" (and every other text field in
this format, and in "not_available"/"disallowed" below): NEVER name,
suggest, or imply any platform, tool, marketplace, directory, search
engine, community, or channel OUTSIDE Flintel as a better place to look.
Phrase "likely_reason" purely in terms of why THIS search came up short,
and let "suggested_actions" (all of which are things FLINTEL ITSELF can
do) be the only next steps offered.

TONE FOR "no_results" (write like a sharp analyst reporting back, not a
form rejection):
- Open by stating plainly WHAT was searched and WHERE.
- Be honest that no strong/high-intent match was found, but frame it as
  information, not failure.
- If ANY posts were matched at all (even loosely relevant, low-intent
  ones), do not discard them — describe what was found in plain terms.
- Never sound like a rejection or a canned apology.
- Never point the user toward a different platform, tool, or channel as
  the place to actually find this.

──────────────────────────────────────────────────────────────────────────
FORMAT 5 — "not_available"
For capabilities Flintel doesn't support yet (e.g. job listings, anything
outside social-listening).
{
  "format": "not_available",
  "message": "<brief, honest explanation of what isn't available yet and what Flintel can do instead>"
}

CLARIFICATION — "not_available" vs "no_results": "not_available" is
ONLY for things Flintel structurally cannot do at all. A request like
"find me customers for my website/product" IS a valid social-listening
search — if that search runs but finds little or nothing, that is a
"no_results" outcome, NEVER "not_available".

──────────────────────────────────────────────────────────────────────────
FORMAT 6 — "disallowed"
For requests to identify, profile, or target a specific named individual
person — Flintel only analyzes public conversation about topics/brands,
never builds a profile on a person.
{
  "format": "disallowed",
  "message": "<brief, non-preachy explanation, redirecting to what Flintel can help with instead>"
}

──────────────────────────────────────────────────────────────────────────
SENTIMENT TAG RULE (applies to every post, in every format above, with no
exception): every individual post object must include a "sentiment"
field set to exactly one of these four lowercase strings —
"positive", "mixed", "negative", "neutral" — and nothing else. Never
omit this field on any post. Never use a free-text or capitalized value.

──────────────────────────────────────────────────────────────────────────
POST-COUNT LIMIT: never include more than 7 posts total, combined across
every platform, in the "platforms" reference list. If given both
grounded posts and discovery-only Google posts, choose the best
combination of up to 7 by genuine relevance — never force an even split,
never pad with a low-quality post just to reach a count. A
discovery-only post's "summary" must say its content hasn't been
fetched yet (e.g. "Content not yet available — found via search, rank
#<n>"), never a fabricated one.

──────────────────────────────────────────────────────────────────────────
NOISE REMOVAL: never repeat a finding twice, never list every post
separately when several support one pattern, never include irrelevant
post details, never invent generic business advice unrelated to the
evidence, never repeat the executive summary inside the conclusion.
Maximum useful intelligence, minimum unnecessary text.

──────────────────────────────────────────────────────────────────────────
RESPONSE FORMAT INTELLIGENCE: choose the format based on what's asked
and what you found, not rigid keyword triggers. If you genuinely cannot
support "source_list", "trend_report", or "comparison" honestly, use
"no_results" instead of forcing a thin answer into one of those shapes.

TONE: write like a sharp, professional market intelligence analyst
presenting findings to a client — confident, precise, evidence-led.
Never write like a generic chatbot casually summarizing what it read.
No "As an AI..." framing, no restating the question back, no filler
openers, no corporate hedging, and never claim more confidence than the
grounding supports.
"""

# (ANALYST-PROMPT UPGRADE) These two .replace() calls inject
# MAX_CHAT_EVIDENCE_POSTS into the prompt's own POST-COUNT LIMIT section.
# Their target substrings were UPDATED to match the new prompt's exact
# wording above ("never include more than 7 posts total" /
# "combination of up to 7 by genuine relevance") — the OLD substrings
# ("more than 7 posts total" without "never include" prefix change is
# fine and still matches; but the old second substring
# "up to 7\nbased on genuine relevance" no longer appears anywhere in
# the new prompt text, since the wording changed to "up to 7 by genuine
# relevance" on a single line). Left as a no-op silently, that mismatch
# would have meant every chat response quietly used a hardcoded "7"
# instead of the real MAX_CHAT_EVIDENCE_POSTS config value — so the
# second .replace() target below was corrected to match the new prompt.
CLAUDE_ANALYSIS_SYSTEM_PROMPT = (
    CLAUDE_ANALYSIS_SYSTEM_PROMPT
    .replace("more than 7 posts total", f"more than {MAX_CHAT_EVIDENCE_POSTS} posts total")
    .replace("up to 7 by genuine relevance", f"up to {MAX_CHAT_EVIDENCE_POSTS} by genuine relevance")
)

# Cheap "map" step used only when a topic has enough matched posts that
# sending them all in one shot would be wasteful/risky context-wise. Each
# chunk gets condensed down to only the points relevant to the user's
# question before the final Haiku call ever sees them.
#
# UNCHANGED.
CLAUDE_MAP_STEP_SYSTEM_PROMPT = """
You are helping analyze one batch of social media posts (Reddit/X) about a
topic, as a pre-processing step before another AI writes the actual answer.
Read the user's question and the posts below (title + text only). Pull out
ONLY the grounded points from THESE posts that are relevant to the user's
question — specific complaints, praise, themes, or facts actually present
in the text. Do not invent anything not present in the posts. Do not answer
the user's question yet — just list the relevant grounded points as short
bullets. If nothing in these posts is relevant to the question, say exactly:
"No relevant points in this batch." Never include, guess, or reference a
post URL — you were not given any.
"""

# (BUGFIX PACK #3) Second-level "notes-of-notes" condensing step — only
# ever invoked when a topic already needed enough first-level post
# chunking that the resulting notes themselves would be too many to
# combine directly in one final reduce call. Purely consolidates/
# de-duplicates ALREADY-grounded notes; never introduces anything new.
CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT = """
You are consolidating multiple batches of already-condensed grounded notes
about social media posts, as a pre-processing step before another AI
writes the final answer. Read the user's question and the notes below.
Combine them into a single, shorter set of grounded bullet points relevant
to the user's question — remove duplicates, merge repeated themes, but
never invent anything not already present in the notes. Do not answer the
user's question yet — just output the consolidated grounded bullets. If
none of the notes contain anything relevant to the question, say exactly:
"No relevant points in this batch."
"""


def build_claude_post_context(matched_signals: list) -> list:
    """Strips a matched-signals list (which has title/post_text/post_url/
    platform, used for post cards) down to ONLY title + text — this is the
    single point where post_url and platform are dropped before anything
    is sent to Claude.

    (TEXT-REQUIRED FILTER) Skips a post unless it has real post_text.
    Title alone is no longer enough to reach Claude — this drops
    title-only stub entries (e.g. a Google-fallback stub whose "title"
    is just a subreddit name and whose post_text is always None) before
    they're ever handed to analyze_with_claude() / analyze_with_claude_
    stream(). This can only ever REMOVE posts from what Claude sees; it
    never adds or alters anything else. Everything downstream of this
    function (matching, evidence-budget sizing, merge_matched_and_
    google_results(), the post-card `results` a user actually sees) is
    untouched — only Claude's own analysis input is narrowed here."""
    posts = []
    for m in matched_signals or []:
        title = (m.get("title") or "").strip()
        text  = (m.get("post_text") or "").strip()
        if not text:
            continue
        posts.append({"title": title, "text": text})
    return posts


def chunk_list(items: list, chunk_size: int) -> list:
    """Generic chunker: splits `items` into consecutive batches of at most
    `chunk_size`. Used to keep each Claude call's context small and cheap
    regardless of how many posts a topic matched."""
    if not items:
        return []
    if chunk_size <= 0:
        return [items]
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def _format_posts_block(posts: list) -> str:
    lines = []
    for i, p in enumerate(posts, 1):
        title = p.get("title") or "(no title)"
        text = p.get("text") or "(no text)"
        lines.append(f"[Post {i}]\nTitle: {title}\nText: {text}")
    return "\n\n".join(lines)


def _call_claude(system_prompt: str, user_message: str, max_tokens: int = None, enable_web_search: bool = False) -> str:
    """Single call to the Anthropic Messages API (Claude Haiku). Raises
    on any failure — callers decide how to degrade gracefully (never let
    this block the search job or the post cards, which don't depend on
    this call at all).

    (CHAT WEB-SEARCH FEATURE) `enable_web_search` (default False — every
    existing caller that doesn't pass it behaves exactly as before this
    feature): when True, adds the Anthropic web_search tool to the
    request payload so Claude can look up current/recent information
    instead of relying purely on its own training knowledge. Nothing
    else about this function changed — the existing text-extraction
    logic below already handles the response correctly when a
    web_search tool result comes back, since it just picks out "text"
    type blocks from `data.get("content", [])` regardless of what tool
    calls happened in between."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens or CLAUDE_MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }
    if enable_web_search:
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": CLAUDE_API_VERSION,
        "content-type": "application/json",
    }

    with httpx.Client(timeout=CLAUDE_TIMEOUT_SECONDS) as http_client:
        response = http_client.post(CLAUDE_API_URL, headers=headers, json=payload)
        if response.status_code >= 400:
            log.warning(
                f"Claude API error {response.status_code} | model={CLAUDE_MODEL} | "
                f"max_tokens={payload['max_tokens']} | "
                f"system_chars={len(system_prompt or '')} | "
                f"user_message_chars={len(user_message or '')} | "
                f"body={response.text[:2000]}"
            )
        response.raise_for_status()
        data = response.json()
        if data.get("stop_reason") == "max_tokens":
            log.warning(f"Claude hit max_tokens={payload['max_tokens']} — output likely truncated")

    text_blocks = [
        block.get("text", "") for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    return "\n".join(t for t in text_blocks if t).strip()


def _map_chunk(query: str, posts_chunk: list) -> str:
    posts_block = _format_posts_block(posts_chunk)
    user_message = f"User's question: {query}\n\nPosts:\n{posts_block}"
    return _call_claude(CLAUDE_MAP_STEP_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_MAP_MAX_TOKENS)


def _condense_notes_chunk(query: str, notes_chunk: list) -> str:
    """(BUGFIX PACK #3 — 2ND-LEVEL CHUNKING) Consolidates one batch of
    already-condensed first-level notes into a single, shorter set of
    grounded bullets, using CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT. Only ever
    called from analyze_with_claude() when the number of first-level
    notes exceeds CLAUDE_NOTES_PER_CHUNK — see below."""
    notes_block = "\n\n---\n\n".join(notes_chunk)
    user_message = f"User's question: {query}\n\nNotes:\n{notes_block}"
    return _call_claude(CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_MAP_MAX_TOKENS)


def analyze_with_claude(query: str, matched_signals: list, extra_context: str = None) -> str:
    """Turns (user question + matched signals) into the actual answer the
    user sees, using CLAUDE_ANALYSIS_SYSTEM_PROMPT. Handles three cases:

      1. No usable posts at all -> Claude still answers, told plainly that
         there's no post data yet, per the system prompt's own grounding
         rule. (v6: this is the exact branch the response-timeout fallback
         in _fill_in_message_outputs() relies on — it calls this function
         with an empty list purely to reach this case.)
      2. Few enough posts to fit one call -> single direct call.
      3. Enough posts that chunking is worth it -> map step condenses each
         chunk of CLAUDE_POSTS_PER_CHUNK posts down to grounded notes,
         then (BUGFIX PACK #3) IF there are more of those notes than
         CLAUDE_NOTES_PER_CHUNK, a second condensing pass chunks the notes
         themselves and condenses each batch further first, THEN one
         final reduce call (still using the main system prompt) turns
         all the (possibly twice-condensed) notes + the question into the
         final answer. Small/medium topics skip the second pass entirely
         and behave exactly as before this bugfix pack.

    Returns the final answer text only — this is the only thing callers
    should persist (see save_claude_answer_to_chat).

    UNCHANGED except for the additive second-level chunking pass described
    above (BUGFIX PACK #3), and the additive optional `extra_context`
    parameter: when provided, it's appended to the user_message built in
    every branch below, right before that branch's _call_claude(...) call
    — no other line of this function's logic/branches changes. This
    function only ever sees ALREADY-MATCHED posts — it has no idea whether
    a time window was applied to produce them.

    (SIMULATED-STREAM FIX) This function is ALSO now the one
    GET /chat/{chat_id}/stream calls to get its complete answer text —
    still exactly this same, byte-for-byte unchanged function, no new
    parameters, no new branches. See that route / the module docstring
    for why.

    (CHAT WEB-SEARCH FEATURE) Every _call_claude(...) invocation inside
    this function is UNCHANGED — none of them pass enable_web_search=True.
    This function only ever analyzes ALREADY-MATCHED, grounded posts, and
    has no reason to reach out to the live web."""
    posts = build_claude_post_context(matched_signals)

    if not posts:
        user_message = (
            f"User's question: {query}\n\n"
            "No posts were found for this topic yet — you have no post data "
            "to ground an answer in. Say that plainly, then answer anything "
            "else in the question you still can from general knowledge."
        )
        if extra_context:
            user_message += "\n\n" + extra_context
        return _extract_json_object_from_text(_call_claude(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message))

    chunks = chunk_list(posts, CLAUDE_POSTS_PER_CHUNK)

    if len(chunks) <= 1:
        posts_block = _format_posts_block(posts)
        user_message = f"User's question: {query}\n\nPosts (title + text only):\n{posts_block}"
        if extra_context:
            user_message += "\n\n" + extra_context
        return _extract_json_object_from_text(_call_claude(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message))

    # Multiple chunks -> map-reduce so no single call has to swallow every
    # matched post at once.
    notes = []
    for chunk in chunks:
        try:
            note = _map_chunk(query, chunk)
        except Exception as exc:
            log.warning(f"Claude map-step failed for a chunk (skipping chunk): {exc}")
            continue
        if note:
            notes.append(note)

    # (BUGFIX PACK #3 — 2ND-LEVEL CHUNKING) If the number of condensed
    # first-level notes itself is large enough that combining them all in
    # one final reduce call could be unwieldy, chunk the notes into
    # batches of CLAUDE_NOTES_PER_CHUNK and condense each batch further
    # with one extra cheap call per batch, BEFORE the final reduce call
    # below. This only activates for topics big enough to need it — for
    # everything else `notes` is left exactly as-is, so behavior is
    # identical to before this bugfix pack.
    if len(notes) > CLAUDE_NOTES_PER_CHUNK:
        note_chunks = chunk_list(notes, CLAUDE_NOTES_PER_CHUNK)
        condensed_notes = []
        for note_chunk in note_chunks:
            try:
                condensed = _condense_notes_chunk(query, note_chunk)
            except Exception as exc:
                log.warning(f"Claude notes-condense step failed for a batch (skipping batch): {exc}")
                continue
            if condensed:
                condensed_notes.append(condensed)
        if condensed_notes:
            notes = condensed_notes

    combined_notes = "\n\n---\n\n".join(notes) if notes else "(no grounded points extracted)"
    user_message = (
        f"User's question: {query}\n\n"
        f"Below are grounded notes already condensed from {len(posts)} posts "
        f"(title + text only), split into batches. Treat these notes as your "
        f"only factual grounding about the posts, and answer the user's "
        f"actual question naturally.\n\nNotes:\n{combined_notes}"
    )
    if extra_context:
        user_message += "\n\n" + extra_context
    return _extract_json_object_from_text(_call_claude(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message))


# ─────────────────────────────────────────────────────────────────────────────
# STREAMING ADD-ON — additive alternate path only, see module docstring.
# _call_claude() and analyze_with_claude() above are completely untouched
# and remain what every existing caller uses.
#
# (SIMULATED-STREAM FIX) `_call_claude_stream()` and
# `analyze_with_claude_stream()` below are kept fully intact, unchanged,
# and importable/callable exactly as before — GET /chat/{chat_id}/stream
# simply no longer calls them (it now calls the blocking analyze_with_claude()
# instead, so it can post_url-patch the answer before ever sending any of it
# out). Nothing about these two functions themselves changed.
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude_stream(system_prompt: str, user_message: str, max_tokens: int = None):
    """(STREAMING ADD-ON) Same Anthropic Messages API call as
    _call_claude(), except with "stream": true — instead of blocking
    until the whole response is ready, this yields each text delta AS
    Anthropic streams it back (word-by-word / token-by-token), so a
    caller can forward pieces to the browser live instead of waiting for
    the entire answer.

    Purely ADDITIVE: _call_claude() itself is untouched and is still used
    by every existing caller (routing, map step, notes-reduce step, the
    non-streaming analyze_with_claude()). This generator is only used by
    analyze_with_claude_stream().

    Yields plain text chunks (str). Raises on any failure — same
    degrade-gracefully convention as _call_claude()."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens or CLAUDE_MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
        "stream": True,
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": CLAUDE_API_VERSION,
        "content-type": "application/json",
    }

    with httpx.Client(timeout=CLAUDE_TIMEOUT_SECONDS) as http_client:
        with http_client.stream("POST", CLAUDE_API_URL, headers=headers, json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    event = json.loads(data_str)
                except (ValueError, TypeError):
                    continue
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {}) or {}
                    text = delta.get("text")
                    if text:
                        yield text


def analyze_with_claude_stream(query: str, matched_signals: list):
    """(STREAMING ADD-ON) Mirrors analyze_with_claude()'s exact branches
    (no posts / single call / map-reduce, including the BUGFIX PACK #3
    second-level note chunking) byte-for-byte — the ONLY difference is
    that the final, user-facing call streams its text via
    _call_claude_stream() instead of blocking via _call_claude(). Any
    earlier map/notes-condense calls (never shown to the user directly)
    are UNCHANGED — still plain, blocking _call_claude() calls, exactly
    as in analyze_with_claude().

    This is a generator: yields text chunks (str) as they stream in. The
    caller is responsible for collecting them into the final full answer
    — this function itself does not persist anything, exactly like
    analyze_with_claude().

    (SIMULATED-STREAM FIX) Left fully intact and unchanged. No longer
    called by GET /chat/{chat_id}/stream (see that route + the module
    docstring), but still here, still correct, still usable by any future
    caller that genuinely wants raw live token-by-token output rather
    than the patched-then-paced text the stream route now sends."""
    posts = build_claude_post_context(matched_signals)

    if not posts:
        user_message = (
            f"User's question: {query}\n\n"
            "No posts were found for this topic yet — you have no post data "
            "to ground an answer in. Say that plainly, then answer anything "
            "else in the question you still can from general knowledge."
        )
        yield from _call_claude_stream(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)
        return

    chunks = chunk_list(posts, CLAUDE_POSTS_PER_CHUNK)

    if len(chunks) <= 1:
        posts_block = _format_posts_block(posts)
        user_message = f"User's question: {query}\n\nPosts (title + text only):\n{posts_block}"
        yield from _call_claude_stream(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)
        return

    # Map step + optional 2nd-level note chunking: identical logic to
    # analyze_with_claude(), still blocking (never shown to the user
    # directly) — only the final call below streams.
    notes = []
    for chunk in chunks:
        try:
            note = _map_chunk(query, chunk)
        except Exception as exc:
            log.warning(f"Claude map-step failed for a chunk (skipping chunk): {exc}")
            continue
        if note:
            notes.append(note)

    if len(notes) > CLAUDE_NOTES_PER_CHUNK:
        note_chunks = chunk_list(notes, CLAUDE_NOTES_PER_CHUNK)
        condensed_notes = []
        for note_chunk in note_chunks:
            try:
                condensed = _condense_notes_chunk(query, note_chunk)
            except Exception as exc:
                log.warning(f"Claude notes-condense step failed for a batch (skipping batch): {exc}")
                continue
            if condensed:
                condensed_notes.append(condensed)
        if condensed_notes:
            notes = condensed_notes

    combined_notes = "\n\n---\n\n".join(notes) if notes else "(no grounded points extracted)"
    user_message = (
        f"User's question: {query}\n\n"
        f"Below are grounded notes already condensed from {len(posts)} posts "
        f"(title + text only), split into batches. Treat these notes as your "
        f"only factual grounding about the posts, and answer the user's "
        f"actual question naturally.\n\nNotes:\n{combined_notes}"
    )
    yield from _call_claude_stream(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ROUTING LAYER (v5, extended in v6 with abuse/harm blocking, again
# with keyword generation, again with time-window parsing, pain-point-aware
# keyword generation, and a 4th "clarify" intent, again with the
# ROUTER INTENT REFINEMENT described in the module docstring above, and
# now again with the CHAT WEB-SEARCH FEATURE described there too)
#
# Runs BEFORE anything else in POST /search. A single cheap Claude call
# decides whether the user's message is a genuine "search" (wants social-
# listening data pulled about a brand/product/topic, INCLUDING a general
# pain-point/complaint angle on a platform — see ROUTER INTENT REFINEMENT),
# a plain "chat" message (INCLUDING a truly open-ended "what's happening on
# this platform" question with no subject at all — see ROUTER INTENT
# REFINEMENT), "blocked" (abusive/harmful content), or "clarify" (a
# search-shaped message with truly nothing — not even a general angle — to
# search for).
#
# (KEYWORD-GENERATION SWAP) The SAME call now ALSO returns a "keywords"
# field when intent="search" — still just ONE Claude call total, no new
# round trip.
#
# (TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE) The SAME call now ALSO
# returns a "time_window_days" field when intent="search" (int, or null
# if the user gave no time range), generates pain-point/prospect-style
# keywords when that's what the message is asking for, and can return
# intent="clarify" instead of guessing at an unclear topic.
#
# (CHAT WEB-SEARCH FEATURE) The SAME call now ALSO has the Anthropic
# web_search tool available to it (see _call_claude()'s own
# `enable_web_search` parameter and classify_and_maybe_chat() below) —
# this only ever matters for the "chat" branch, since that's the only
# branch where Claude writes a user-facing reply directly; "search" still
# only ever returns keywords/time_window_days, never a reply, so having
# the tool available changes nothing about that branch's own output
# contract.
#
# Safety rule: ANY failure here (bad JSON, API error, timeout, missing
# key) defaults to {"intent": "search", "reply": None, "keywords": None,
# "time_window_days": None} so the pre-existing pipeline is always the
# fallback — this routing layer can only ever add a shortcut, it can
# never silently swallow a real search request or leave one with zero
# keywords.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_ROUTER_SYSTEM_PROMPT = """
You are the routing brain inside Flintel, a social-listening platform.
Every message a user types goes through you FIRST, before anything else
happens in the product.
Your job: classify this message into exactly one of FOUR types, and for
"search" messages, ALSO generate the keyword list Flintel's own
(unchanged, plain-Python) matching code will use afterward, plus an
optional time window.

1. "search" — the message is asking Flintel to research/monitor/pull
   social-media data about a brand, product, company, person, industry,
   or topic, AND that topic/subject is actually clear from the message
   itself (or from the conversation history below). For "search"
   messages, ALSO return:

   - "keywords": an array of search terms.
     - Read the user's own words and figure out what they are actually
       asking about. A short, narrow prompt ("reddit posts about AI")
       needs only the ONE (or two) keyword(s) that actually capture the
       real topic — e.g. just "AI" — do NOT pad it out with unrelated
       angles ("AI review", "AI pricing", "AI complaints", etc.) the user
       never asked about.
     - A broader or more detailed prompt can warrant more keyword
       variations (genuine synonyms or short related phrases actually
       likely to appear in real posts) — up to 10 keywords maximum,
       never more.
     - PAIN-POINT / PROSPECT PATTERN: if the user describes selling or
       promoting something and wants to find people who might need it
       (e.g. "I run an AI agent company, find people whose website is
       slow / carts are getting abandoned", "I run a travel agency, find
       people saying their travel experience was bad", or any similar
       "I offer X, find people with problem Y" message, in ANY industry —
       these are just examples, not a fixed list), generate keywords
       around the PROBLEM/SYMPTOM the user described (e.g. "website
       slow", "site is slow", "abandoned cart", "bad travel experience",
       "trip was ruined", "poor customer service"), NOT around the
       seller's own product or company name. Read whatever problem the
       user actually describes and reflect that back — never assume a
       fixed industry or fixed symptom list.
     - GENERAL PAIN-POINT / COMPLAINT PATTERN (no brand, product, or
       industry named at all): a message like "reddit par log kya
       problems face kar rahe hain" or "what are people complaining
       about on twitter" names NO specific brand/product/industry, but it
       DOES name a real, searchable subject — general problems/
       complaints/frustrations on that platform. Treat this as "search"
       (NOT "clarify" — see the "clarify" rules below), and generate
       keywords around common frustration/complaint phrasing that would
       plausibly appear in real posts, e.g. "so frustrated with", "sick
       of dealing with", "worst experience with", "wish there was a
       better", "fed up with", "nightmare trying to", "why is it so hard
       to", "anyone else hate". This is DIFFERENT from the fully
       open-ended "what's happening on this platform" case described
       under "chat" below — the distinguishing line is whether the user
       named ANY angle (a brand, a product, an industry, or a general
       problem/complaint framing) versus naming nothing at all beyond the
       platform itself.
     - GENERAL DEMAND / MARKET-RESEARCH PATTERN (no platform named either):
       a message that asks what people are frustrated with, annoyed by, or
       wish existed — in any domain, industry, or "technology" broadly — even
       WITHOUT naming a specific platform, is still a valid "search" subject:
       the complaint/demand angle itself (e.g. "annoyed with", "frustrated
       by", "wish there was", "biggest problem with") is the searchable topic.
       This is different from a truly platform-only, topic-less question like
       "reddit par kya chal raha hai" (which stays "chat" per the rule below)
       — the distinguishing signal is whether the message names ANY complaint/
       demand/pain-point angle, not whether it names a platform. Generate
       keywords around the complaint/frustration phrasing the user implied
       (e.g. "so annoyed with", "wish there was a tool for", "sick of dealing
       with", "biggest pain point in"), the same way the PAIN-POINT / PROSPECT
       PATTERN above does.
     - Every keyword must be something that could plausibly appear
       verbatim, or as a close natural substring, inside a real post's
       title or text. Keep each keyword short and natural.
     - Never include meta wording that describes the user's REQUEST to
       you rather than the topic itself — words like "reddit", "twitter",
       "x", "linkedin", "facebook", "posts", "posts about", "show me",
       "today", "find", "search" describe what/where the user wants
       searched, not something that would appear inside an actual post,
       so leave them out of the keyword list (the platform itself is
       handled separately, by a dropdown the user already picked — you
       are only responsible for the topic keywords).

   - "website_only": a boolean, true ONLY when the user's message is
     JUST a website URL (or a URL plus only trivial filler like "check
     this out", "yeh dekho") with NO other stated ask/angle at all. In
     this case, still return "keywords": null (the downstream website-
     reading pipeline fills in real keywords from the site's own
     content, exactly like the existing website-URL flow) — "website_only"
     is what tells the backend to answer the user with a WEBSITE
     BUSINESS BREAKDOWN, not a Reddit/X-evidence report, even though
     keyword generation / background search still runs normally.
     Default false for everything else, including when a URL is shared
     ALONGSIDE a real ask (e.g. "find me leads from this site") — in
     that case treat it as a normal "search" with website_only: false.

   - "match_phrases": an array of 4 to 10 short, natural phrases/
     sentences (each phrase itself should be roughly 4-10 words long),
     up to 7 phrases maximum. Each phrase should read like something a
     real person might actually write in a post about this topic (e.g.
     for "AI agents": "using an AI agent to handle customer support",
     "built an AI agent for my business", "AI agents doing repetitive
     tasks automatically") — NOT a single word, NOT meta wording
     ("reddit", "posts", "show me"). These phrases exist purely to
     confirm a post is genuinely ABOUT the topic, not just that a
     generic word appears somewhere in it — this is what stops a single
     bare keyword like "agents" from ever matching a post that has nothing
     to do with the actual topic.

   - "time_window_days": an integer, or null.
     - If the user's message itself implies a time range, convert it to
       an approximate number of days: "today"/"aaj" -> 1, "this week" /
       "last 7 days" -> 7, "last 2 weeks" -> 14, "last month" / "past 30
       days" -> 30, "last 3 months" -> 90, "last 6 months" -> 180, "last
       year" -> 365, and so on for any other phrasing that clearly names
       a time span.
     - If the user gives NO time range at all, this MUST be null — do
       NOT invent or assume one.
     - A missing/null time_window_days is NOT by itself a reason to ask
       for clarification (see "clarify" below) — plenty of valid search
       requests never mention a time range at all.

   - "evidence_required": an integer.
     - Estimate approximately how much evidence (matched posts) is needed
       to properly answer this question — NOT a fixed formula. Consider:
       whether a time period was mentioned and how long it is, whether the
       user asks for a trend/change over time, whether the user asks for a
       comparison between two or more entities, whether it's simple
       sentiment, whether the user asks for reasons/drivers/recurring
       themes, and overall complexity of the question.
     - A simple, narrow question with no time range typically needs
       roughly 20-30. A trend/change question over a longer window
       typically needs meaningfully more. A comparison between entities,
       especially over a longer window, typically needs the most. A
       "why are people complaining" style question needs enough to
       identify recurring patterns, not just a handful of posts.
     - These are illustrative guidelines, not fixed rules — reason about
       the actual query every time.
     - Never below 15, never above 100 — the backend will clamp this
       regardless, so pick the number that's genuinely right for the
       question, not a number chosen to avoid clamping.

   Do NOT try to answer the user's question yourself for "search" —
   only classify and produce the keyword list / time window.

2. "chat" — a normal conversational message that doesn't need any new
   data pulled at all: greetings ("hi", "hello", "what's up", "kia chal
   raha hai aj kal"), small talk, thanks, general knowledge questions, a
   follow-up question about something already discussed in this
   conversation, or a request to just talk. Answer the user's message
   yourself, directly and naturally, the way Claude/ChatGPT would in any
   normal conversation. You also have access to a live web_search tool
   for these "chat" replies — use it whenever the user's question is
   about something current, recent, or beyond your own training
   knowledge (news, prices, scores, who currently holds some role,
   "what happened with X today", etc.), instead of saying you don't
   have real-time access or citing a knowledge cutoff. Search first,
   then answer plainly and naturally from what you find. "keywords" and
   "time_window_days" must be null for this type.

   ALSO classify as "chat" (not "search", not "clarify") when the message
   asks a broad, platform-wide "what's trending / what's happening / what
   are the top problems right now" question with NO specific brand,
   product, narrower topic, or industry named, AND no general problem/
   complaint framing either — e.g. "reddit par kya chal raha hai", "abhi
   reddit par top problem kya hai", "what's trending on twitter today"
   with truly nothing else to go on. For these, do NOT generate search
   keywords and do NOT trigger a database search — instead, write the
   "reply" yourself using your own general knowledge of what's commonly
   discussed on that platform, in a natural, confident, professional tone
   (not hedgy, not "as an AI I don't have real-time access" — just answer
   plainly from what you know, using the web_search tool above if it
   would help ground the answer in something current). At the end of
   that reply, naturally invite them to share their business, product, or
   website link so Flintel can pull real, current, related data for them
   specifically. "keywords" and "time_window_days" stay null for this
   case, exactly like any other "chat" message.

   This "chat" case is DIFFERENT from the GENERAL PAIN-POINT / COMPLAINT
   PATTERN described under "search" above: if the user names ANY angle at
   all — a brand, product, industry, or even just a general problem/
   complaint framing (e.g. "reddit par log kya problems face kar rahe
   hain") — that IS "search", not this "chat" case. The distinguishing
   factor is simple: is there an actual searchable angle, or is the
   question just "what's happening in general" with nothing else?

3. "blocked" — the message is abusive, harassing, hateful, sexually
   explicit, threatening, or otherwise harmful (directed at you, at a
   person, or at any group). Do not search for it and do not answer it
   normally. Instead write a short, calm, firm decline as the reply —
   don't lecture, don't repeat or quote the harmful content back, don't
   moralize at length, just briefly decline and invite them to ask
   something else. "keywords" and "time_window_days" must be null for
   this type.

4. "clarify" — the message is clearly ASKING for a search/social-listening
   pull (it has search-shaped phrasing: "reddit posts", "show me", "find
   me", a time range, etc.) but does NOT actually name any clear
   topic/brand/product/industry/subject to search for, and does NOT even
   name a general problem/complaint angle (see the GENERAL PAIN-POINT /
   COMPLAINT PATTERN under "search" above, which is a "search", not a
   "clarify") — neither in the message itself nor anywhere in the short
   conversation history below. Examples: "aaj ke reddit posts dikhao"
   alone, "last 6 months ke posts do" alone, "show me today's posts" with
   nothing else — there is no brand, product, topic, industry, or problem
   mentioned anywhere for Flintel to actually search. In this case, do
   NOT guess a topic and do NOT fall back to some generic/meaningless
   keyword. Instead, write a short, natural, single clarifying question
   asking what topic/brand/industry/problem they want to look into.
   "keywords" and "time_window_days" must be null for this type (there is
   no job to run yet). IMPORTANT: only use "clarify" when the TOPIC itself
   is missing — a message that clearly names a topic/brand/industry (or
   even just a general problem angle) but simply doesn't mention a time
   range is STILL "search" with time_window_days: null, never "clarify".

   ADDITIONALLY: if the user's message contains a website URL (a link
   starting with http:// or https://) together with ANY stated request,
   ask, or angle at all — including a generic one like "find me
   customers", "find me leads", "promote my site", "market my website",
   or similar — this is NEVER "clarify", even though no specific
   topic/brand/industry is named. A URL is itself enough context to
   search from (Flintel can read the site directly), so treat this as
   "search" with "website_only": false. In this case ALSO generate
   "keywords" and "match_phrases" from the ASK/ANGLE TEXT ALONE, exactly
   as you would for the same request without any URL (ignore the URL
   itself completely; never put the URL, the domain name, or words like
   "website"/"site"/"link" into keywords). For a generic ask such as
   "find me sales", "get me leads", "find customers", apply the
   PAIN-POINT / PROSPECT PATTERN and buyer-intent phrasing normally.
   The downstream website-reading step will separately generate
   website-derived keywords and the backend merges both lists, so do not
   try to guess the business's products yourself. Return "keywords": null
   and "match_phrases": null ONLY if the message is a bare URL with no
   ask/angle at all (that case stays website_only: true, unchanged).
   Only use "clarify" when there is NEITHER a URL NOR any named topic/
   brand/industry/problem angle anywhere in the message.

   When a URL is present together with an ask, ALSO return "url_ask_type":
   - "generic_own_business": the user wants something for their OWN business
     or website in general (sales, leads, customers, growth, promotion,
     "find people for this", in ANY language or phrasing), with NO separate
     named topic, problem, or angle.
   - "specific_topic": the user names a distinct topic, problem, product
     angle, or question alongside the URL (e.g. complaints about something,
     competitors, a particular feature or issue).
   Decide from the meaning of the message, never from specific words.
   For a bare URL with no ask, use "none". Without a URL, use null.

   When writing the clarifying reply, sound like a helpful consultant, not
   a form validator: briefly explain WHY you're asking (so the search
   actually finds something relevant to them), and in one natural
   sentence give them two easy ways forward — name a topic/brand/
   industry, OR just paste their website link, since Flintel can read a
   shared website link automatically and pull the right keywords from it.
   Keep it to 1-2 natural sentences, never robotic or repetitive-sounding.

A short, auto-summarized conversation history (may be empty) is given
below for continuity when classifying and when writing a "chat",
"blocked", or "clarify" reply, or when a "search" follow-up implicitly
refers back to a topic already discussed (e.g. if the topic was named
earlier in the conversation, a later "today ke posts do" can still be
"search", using that earlier topic — only ask "clarify" when the topic
truly cannot be determined from the message OR this history). Keep any
reply conversational and plain — don't mention you're an AI or that this
is a "mock", and don't narrate your own reasoning.
Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in EXACTLY one of these four shapes:
{"intent": "search", "reply": null, "keywords": ["<keyword1>", "<keyword2>"], "time_window_days": null, "match_phrases": ["<phrase1>", "<phrase2>"], "evidence_required": <int|null>, "website_only": false, "url_ask_type": "generic_own_business"|"specific_topic"|"none"|null}
{"intent": "chat", "reply": "<your natural reply text here>", "keywords": null, "time_window_days": null, "match_phrases": null, "evidence_required": null}
{"intent": "blocked", "reply": "<short, polite decline text>", "keywords": null, "time_window_days": null, "match_phrases": null, "evidence_required": null}
{"intent": "clarify", "reply": "<short, natural clarifying question>", "keywords": null, "time_window_days": null, "match_phrases": null, "evidence_required": null}
"""

CLAUDE_ROUTER_WEBSITE_CONTEXT_ADDENDUM = """
SAVED WEBSITE CONTEXT (applies ONLY when a "Saved website context" block
appears in the input. If absent, ignore this section; use_website_context
is false.)
The user shared THEIR OWN website earlier in this chat; its URL, business
and already-extracted keywords are given. Decide which case applies:
1. OWN-BUSINESS REQUEST: user wants something for their own business/
   niche/website WITHOUT naming a different topic ("mere niche se related
   posts do", "mere liye leads dhoond kar do", "sales do", "find customers
   for me"). Return intent="search", "use_website_context": true,
   "keywords": null, "match_phrases": null (the backend will itself generate
   FRESH keywords and phrases from the saved website evidence combined with
   this new message — never reuse old ones and never generate them here).
   While a saved website context exists
   such a message is NEVER "clarify" and NEVER "chat".
2. SPECIFIC TOPIC CONNECTED TO THE WEBSITE: user names a specific
   product/angle that belongs to what the website offers. intent="search",
   "use_website_context": false, generate keywords/match_phrases for THAT
   topic normally, "website_topic_relation": "related".
3. TOPIC UNRELATED TO THE WEBSITE: normal search with keywords for that
   topic, "use_website_context": false, "website_topic_relation":
   "unrelated". Never mix the website's keywords in.
4. Everything else (greetings, blocked etc.): use_website_context false.
Extended search JSON shape:
{"intent":"search","reply":null,"keywords":[...]|null,"time_window_days":<int|null>,"match_phrases":[...]|null,"evidence_required":<int|null>,"website_only":false,"url_ask_type":"generic_own_business"|"specific_topic"|"none"|null,"use_website_context":<true|false>,"website_topic_relation":"related"|"unrelated"|null}
"""

CLAUDE_ROUTER_SYSTEM_PROMPT = (
    CLAUDE_ROUTER_SYSTEM_PROMPT
    + "\n" + flintel.ROUTER_UNFILTERED_ADDENDUM
    + "\n" + flintel.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM
    + "\n" + CLAUDE_ROUTER_WEBSITE_CONTEXT_ADDENDUM
)

CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT = """
You are the AI assistant inside Flintel, a social listening platform.
Answer the user's message naturally and directly, the way Claude or
ChatGPT would in any normal conversation. Plain language, no rigid
template, no JSON, no code blocks. Don't mention you're an AI or that
this is a "mock".

You also have access to a live web_search tool — use it whenever the
user's question is about something current, recent, or beyond your own
training knowledge, instead of saying you don't have real-time access or
citing a knowledge cutoff. Search first, then answer plainly and
naturally from what you find.
"""

# (v6) Safety-net text used only if the router itself flagged a message as
# "blocked" but, for whatever reason, didn't return usable reply text —
# never re-sent to Claude (no extra call, and no reason to hand harmful
# content to another prompt just to get a decline message).
CLAUDE_BLOCKED_FALLBACK_REPLY = (
    "I can't help with that one. Happy to help you look into a brand, "
    "product, or topic instead, or just chat about something else."
)

# (TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE, reworded by ROUTER INTENT
# REFINEMENT) Same safety-net pattern as CLAUDE_BLOCKED_FALLBACK_REPLY
# above: used only if the router flagged "clarify" but didn't return
# usable reply text — never re-sent to Claude. Reworded to match the same
# consultant tone the router prompt now asks for, and to mention the
# website-link shortcut (handled automatically by the existing,
# UNCHANGED WEBSITE-URL KEYWORD EXTRACTION FEATURE on the user's next
# message).
CLAUDE_CLARIFY_FALLBACK_REPLY = (
    "Bilkul — bas yeh batayein aap kis brand, product, ya industry ke "
    "baare mein Reddit aur baaki platforms se data dekhna chahte hain. "
    "Agar apni website ka link bhi share kar dein, main us se directly "
    "aap ke business se related conversations nikaal dunga."
)


def _parse_router_json(raw: str):
    """Best-effort JSON parse of the router's output — strips ```json
    fences if Claude added them anyway, and validates the shape. Returns
    None on anything unexpected so the caller falls back to the safe
    "search" default instead of ever guessing.

    (v6) Accepts "blocked" alongside "search"/"chat" — a reply is
    expected as a string for both, and if it isn't one, the caller's
    fallback text is used instead.

    (KEYWORD-GENERATION SWAP) Now also parses/validates a "keywords"
    field for "search" intent: must be a JSON array of non-empty
    strings; each is trimmed, de-duplicated case-insensitively, and
    capped at CLAUDE_MAX_KEYWORDS. Anything malformed (missing, not a
    list, empty after cleaning) simply results in keywords=None — the
    caller (classify_and_maybe_chat / the /search route) is what applies
    the generate_fuzzy_keywords() fallback in that case, so this function
    itself never needs to know about that fallback.

    (TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE):
      - Accepts "clarify" as a 4th valid intent, treated like "chat"/
        "blocked" for reply-string validation (a string reply is
        expected; anything else falls back to None so the caller's own
        fallback text is used).
      - Also parses/validates "time_window_days" for "search" intent:
        must be a positive int (or a numeric string Claude accidentally
        quoted), clamped between 1 and MAX_TIME_WINDOW_DAYS. Anything
        else (missing, null, zero, negative, non-numeric, or intent !=
        "search") simply results in time_window_days=None, meaning "no
        time filter" — the exact same as if this feature didn't exist.

    (EVIDENCE-BUDGET FEATURE):
      - Also parses/validates "evidence_required" for "search" intent
        ONLY: accepts an int (or a numeric string Claude accidentally
        quoted), clamped between MIN_ANALYSIS_EVIDENCE and
        MAX_ANALYSIS_EVIDENCE. Anything else (missing, null, non-numeric,
        or intent != "search") simply results in evidence_required=None
        — exactly the same "missing means use the default" convention
        already used by "keywords"/"time_window_days" above.

    (ROUTER INTENT REFINEMENT) This function's logic is completely
    UNCHANGED — the refinement is pure prompt wording inside
    CLAUDE_ROUTER_SYSTEM_PROMPT above; the four valid intents, their
    field shapes, and every validation/clamping rule here are identical
    to before.

    (CHAT WEB-SEARCH FEATURE) This function's logic is completely
    UNCHANGED — the feature only affects _call_claude()'s request
    payload and does not change the router's own JSON output contract in
    any way.

    (PHRASE-MATCHING FEATURE) Also parses/validates a NEW, SEPARATE
    "match_phrases" field for "search" intent — cleaned the same way
    "keywords" is (trimmed, non-strings/empties dropped, de-duplicated
    case-insensitively), but capped at 7 entries instead of
    CLAUDE_MAX_KEYWORDS. A missing/malformed field simply results in
    match_phrases=None — no exception, no different fallback behavior
    for anything else in this function. This field never touches the
    job document or the Google search call; it exists purely for
    get_matched_signals()'s own loose title/text phrase check."""
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
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    if intent not in ("search", "chat", "blocked", "clarify"):
        return None
    reply = data.get("reply")
    if intent in ("chat", "blocked", "clarify") and not isinstance(reply, str):
        reply = None

    keywords = None
    match_phrases = None
    time_window_days = None
    evidence_required = None
    unfiltered = False
    website_only = False
    use_website_context = False
    website_topic_relation = None
    url_ask_type = None
    if intent == "search":
        raw_keywords = data.get("keywords")
        if isinstance(raw_keywords, list):
            cleaned_keywords = []
            seen = set()
            for kw in raw_keywords:
                if not isinstance(kw, str):
                    continue
                kw_clean = kw.strip()
                if not kw_clean:
                    continue
                key = kw_clean.lower()
                if key in seen:
                    continue
                seen.add(key)
                cleaned_keywords.append(kw_clean)
                if len(cleaned_keywords) >= CLAUDE_MAX_KEYWORDS:
                    break
            keywords = cleaned_keywords or None

        raw_phrases = data.get("match_phrases")
        if isinstance(raw_phrases, list):
            cleaned_phrases = []
            seen_phrases = set()
            for phrase in raw_phrases:
                if not isinstance(phrase, str):
                    continue
                phrase_clean = phrase.strip()
                if not phrase_clean:
                    continue
                key = phrase_clean.lower()
                if key in seen_phrases:
                    continue
                seen_phrases.add(key)
                cleaned_phrases.append(phrase_clean)
                if len(cleaned_phrases) >= 7:
                    break
            match_phrases = cleaned_phrases or None

        raw_window = data.get("time_window_days")
        parsed_window = None
        if isinstance(raw_window, bool):
            parsed_window = None  # guard: bool is a subclass of int in Python
        elif isinstance(raw_window, int):
            parsed_window = raw_window
        elif isinstance(raw_window, str) and raw_window.strip().isdigit():
            parsed_window = int(raw_window.strip())
        if isinstance(parsed_window, int) and parsed_window > 0:
            time_window_days = min(parsed_window, MAX_TIME_WINDOW_DAYS)

        # (EVIDENCE-BUDGET FEATURE) Same accept-int-or-numeric-string
        # tolerance as time_window_days above, but clamped into
        # [MIN_ANALYSIS_EVIDENCE, MAX_ANALYSIS_EVIDENCE] instead of
        # [1, MAX_TIME_WINDOW_DAYS] — the backend is the final authority
        # on the bound regardless of what the model actually picked.
        raw_evidence = data.get("evidence_required")
        parsed_evidence = None
        if isinstance(raw_evidence, bool):
            parsed_evidence = None  # guard: bool is a subclass of int in Python
        elif isinstance(raw_evidence, int):
            parsed_evidence = raw_evidence
        elif isinstance(raw_evidence, str) and raw_evidence.strip().isdigit():
            parsed_evidence = int(raw_evidence.strip())
        if isinstance(parsed_evidence, int):
            evidence_required = max(MIN_ANALYSIS_EVIDENCE, min(parsed_evidence, MAX_ANALYSIS_EVIDENCE))

        unfiltered = bool(data.get("unfiltered") is True)
        website_only = bool(data.get("website_only") is True)
        use_website_context = bool(data.get("use_website_context") is True)
        _rel = data.get("website_topic_relation")
        website_topic_relation = _rel if _rel in ("related", "unrelated") else None
        _uat = data.get("url_ask_type")
        url_ask_type = _uat if _uat in ("generic_own_business", "specific_topic", "none") else None

    return {"intent": intent, "reply": reply, "keywords": keywords, "time_window_days": time_window_days,
            "unfiltered": unfiltered, "match_phrases": match_phrases, "evidence_required": evidence_required,
            "website_only": website_only, "use_website_context": use_website_context,
            "website_topic_relation": website_topic_relation, "url_ask_type": url_ask_type}


def classify_and_maybe_chat(query: str, chat_summary: str, website_context_summary: str = None) -> dict:
    """(v5, extended in v6 with abuse-blocking, again with keyword
    generation, again with time-window parsing + a "clarify" intent,
    again with the ROUTER INTENT REFINEMENT prompt wording described in
    the module docstring, and now again with the CHAT WEB-SEARCH FEATURE
    described there too) Single cheap Claude call that classifies the
    user's message as "search", "chat", "blocked", or "clarify" and:
      - for "chat"/"blocked"/"clarify", writes the reply in the same
        call, and
      - for "search", ALSO returns the keyword list and time window to
        use for matching.
    Falls back to {"intent": "search", "reply": None, "keywords": None,
    "time_window_days": None} on ANY failure (API error, timeout, bad
    JSON) so the pre-existing search pipeline is always the safe default
    — only the chat-reply / abuse-blocking / smart-keyword / time-window
    / clarify / web-search shortcuts can ever be skipped by a routing
    hiccup, never a genuine search request (the /search route falls back
    to generate_fuzzy_keywords() whenever keywords come back None for a
    "search" intent, and treats a missing time_window_days as "no time
    filter", exactly as before this feature).

    (ROUTER INTENT REFINEMENT) This function's logic is completely
    UNCHANGED — only the text of CLAUDE_ROUTER_SYSTEM_PROMPT changed.

    (CHAT WEB-SEARCH FEATURE) The ONLY change in this function: the
    router's own _call_claude(...) call now passes
    enable_web_search=True, so Claude may use the live web_search tool
    while producing a "chat"-classified reply. No other line of this
    function changed — the same try/except safety net, the same
    _parse_router_json() validation, and the same "any failure ->
    default to intent='search'" fallback all apply exactly as before."""
    website_context_block = ""
    if website_context_summary:
        website_context_block = (
            "Saved website context (the user shared their own website earlier in this chat):\n"
            f"{website_context_summary}\n\n"
        )
    user_message = (
        f"Conversation so far (auto-summarized, may be empty):\n"
        f"{chat_summary or '(no earlier messages in this chat)'}\n\n"
        f"{website_context_block}"
        f"User's new message: {query}"
    )
    try:
        raw = _call_claude(CLAUDE_ROUTER_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_ROUTER_MAX_TOKENS, enable_web_search=True)
    except Exception as exc:
        log.warning(f"Router Claude call failed (defaulting to 'search'): {exc}")
        return {"intent": "search", "reply": None, "keywords": None, "time_window_days": None, "unfiltered": None, "match_phrases": None, "evidence_required": None, "website_only": False, "use_website_context": False, "website_topic_relation": None, "url_ask_type": None}

    parsed = _parse_router_json(raw)
    if not parsed:
        log.warning(f"Router returned unparseable output (defaulting to 'search'): {raw[:200]!r}")
        return {"intent": "search", "reply": None, "keywords": None, "time_window_days": None, "unfiltered": None, "match_phrases": None, "evidence_required": None, "website_only": False, "use_website_context": False, "website_topic_relation": None, "url_ask_type": None}
    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# CLARIFY-SELF-RESOLVE FEATURE — see the module docstring note above for
# the full rationale. Only ever invoked for a message the main router
# above already classified as intent="clarify". Uses ONE extra cheap
# Claude call, Claude's own general knowledge only (no web-search tool,
# no new information beyond the message + the existing rolling chat
# summary), to make a single honest attempt at resolving the topic before
# the existing clarify-question flow is allowed to fire.
#
# (ROUTER INTENT REFINEMENT) UNTOUCHED by this file's changes: with
# "clarify" now firing less often (per point 1 and 2 of the ROUTER INTENT
# REFINEMENT note above), this feature simply gets invoked less often —
# its own logic, prompt, and behavior are completely unchanged.
#
# (CHAT WEB-SEARCH FEATURE) UNTOUCHED — the resolve_unclear_topic() call
# below does NOT pass enable_web_search=True, exactly as instructed: this
# step stays a narrow, honest "can I already tell what this means from my
# own training knowledge + the chat summary" check, never a research
# step.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_TOPIC_RESOLVER_SYSTEM_PROMPT = """
You are a fallback topic-resolution step inside Flintel, a social-listening
platform. You are only ever called for a message that Flintel's own router
already decided is CLARIFY — a search-shaped message ("reddit posts",
"show me", "find me", a time range, etc.) that did NOT name a clear
topic/brand/product/industry, either in the message itself or in the short
conversation history.

Your job: using ONLY your own general knowledge (you have no web-search
tool here, and you are not being given any new information beyond the
message and the conversation summary below), make ONE honest attempt to
figure out what topic the user most likely means. This will usually fail —
that is fine and expected; only succeed when you are genuinely confident,
never guess just to produce an answer.

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{"resolved": true, "keywords": ["<keyword1>", "<keyword2>"], "time_window_days": null, "match_phrases": ["<phrase1>", "<phrase2>"]}
or, when you genuinely cannot infer a specific topic:
{"resolved": false, "keywords": null, "time_window_days": null, "match_phrases": null}

Rules when "resolved": true:
- "keywords": up to 10 short, natural search terms that could plausibly
  appear inside a real Reddit/X/LinkedIn/Facebook post about the topic you
  inferred — same rules as normal keyword generation elsewhere in this
  product: no meta words like "reddit", "posts", "show me", "today", etc.
- "match_phrases": an array of 4 to 10 short, natural phrases/sentences
  (each roughly 4-10 words long), up to 7 phrases maximum — same kind of
  real-person phrasing described for the router's own "match_phrases"
  field elsewhere in this product, used to confirm a post is genuinely
  about the topic you inferred, not just that a generic keyword appears.
- "time_window_days": convert any time range already implied by the user's
  own message the same way it's always converted elsewhere ("today"/"aaj"
  -> 1, "this week"/"last 7 days" -> 7, "last month" -> 30, "last 6
  months" -> 180, "last year" -> 365, etc.) — null if no time range was
  mentioned.
- Only mark "resolved": true if you are genuinely confident about the
  topic — e.g. the conversation history clearly named a brand/topic
  earlier and this message is obviously a natural follow-up about it. Do
  NOT invent a topic out of thin air just because the message is
  search-shaped.
"""


def resolve_unclear_topic(query: str, chat_summary: str):
    """(CLARIFY-SELF-RESOLVE FEATURE) Best-effort, single extra cheap
    Claude call, used ONLY for a message the main router already
    classified as "clarify" (a search-shaped message with no clear
    topic). Uses Claude's OWN general knowledge (no web-search tool, no
    new information beyond the message + the existing rolling chat
    summary) to make one honest attempt at guessing the real topic —
    e.g. a follow-up that implicitly refers back to a brand/topic already
    named earlier in the conversation.

    Returns a dict {"keywords": [...], "time_window_days": int|None} if
    Claude was genuinely confident enough to resolve a topic, or None if
    it wasn't (or if this call failed outright) — callers must treat
    None exactly the same as before this feature existed: fall through
    to the normal "clarify" question-asking behavior. This can only ever
    ADD a shortcut on top of the existing clarify flow; it can never
    block or replace the safety net of just asking the user."""
    user_message = (
        f"Conversation so far (auto-summarized, may be empty):\n"
        f"{chat_summary or '(no earlier messages in this chat)'}\n\n"
        f"User's message (already classified as unclear-topic 'clarify' by "
        f"the main router): {query}"
    )
    try:
        raw = _call_claude(
            CLAUDE_TOPIC_RESOLVER_SYSTEM_PROMPT,
            user_message,
            max_tokens=CLAUDE_TOPIC_RESOLVER_MAX_TOKENS,
        )
    except Exception as exc:
        log.warning(f"Topic-resolver Claude call failed for query={query!r}: {exc}")
        return None

    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        # (RESILIENT JSON EXTRACTION) Claude sometimes appends trailing
        # prose after a valid JSON object (e.g. "```json\n{...}\n```\n\nThe
        # user has shared..."). Before giving up, try extracting just the
        # first {...} object via a non-greedy regex and parsing that alone
        # — this can only ever recover an otherwise-wasted call, never
        # change behavior for already-valid JSON (which was already
        # handled above).
        match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except (ValueError, TypeError):
                log.warning(f"Topic-resolver returned unparseable output for query={query!r}: {raw[:200]!r}")
                return None
        else:
            log.warning(f"Topic-resolver returned unparseable output for query={query!r}: {raw[:200]!r}")
            return None
    if not isinstance(data, dict) or not data.get("resolved"):
        return None

    raw_keywords = data.get("keywords")
    keywords = None
    if isinstance(raw_keywords, list):
        cleaned_keywords = []
        seen = set()
        for kw in raw_keywords:
            if not isinstance(kw, str):
                continue
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            key = kw_clean.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned_keywords.append(kw_clean)
            if len(cleaned_keywords) >= CLAUDE_MAX_KEYWORDS:
                break
        keywords = cleaned_keywords or None

    if not keywords:
        return None

    raw_phrases = data.get("match_phrases")
    match_phrases = None
    if isinstance(raw_phrases, list):
        cleaned_phrases = []
        seen_phrases = set()
        for phrase in raw_phrases:
            if not isinstance(phrase, str):
                continue
            phrase_clean = phrase.strip()
            if not phrase_clean:
                continue
            key = phrase_clean.lower()
            if key in seen_phrases:
                continue
            seen_phrases.add(key)
            cleaned_phrases.append(phrase_clean)
            if len(cleaned_phrases) >= 7:
                break
        match_phrases = cleaned_phrases or None

    raw_window = data.get("time_window_days")
    time_window_days = None
    parsed_window = None
    if isinstance(raw_window, bool):
        parsed_window = None
    elif isinstance(raw_window, int):
        parsed_window = raw_window
    elif isinstance(raw_window, str) and raw_window.strip().isdigit():
        parsed_window = int(raw_window.strip())
    if isinstance(parsed_window, int) and parsed_window > 0:
        time_window_days = min(parsed_window, MAX_TIME_WINDOW_DAYS)

    return {"keywords": keywords, "time_window_days": time_window_days, "match_phrases": match_phrases}


# ─────────────────────────────────────────────────────────────────────────────
# WEBSITE-URL KEYWORD EXTRACTION FEATURE — see the module docstring note
# above for the full rationale. Only ever invoked for a search-type
# message whose raw text contains an http(s) URL. Fetches that URL, turns
# it into plain text, and hands it (together with the user's own request
# text) to a single extra cheap Claude call to produce the keyword list.
#
# (ROUTER INTENT REFINEMENT) UNTOUCHED by this file's changes — this is
# the exact feature the new "clarify" reply wording (see
# CLAUDE_CLARIFY_FALLBACK_REPLY and the router's "clarify" instructions
# above) now proactively points the user toward. Nothing in this section
# was modified.
#
# (CHAT WEB-SEARCH FEATURE) UNTOUCHED — extract_keywords_from_website()'s
# own _call_claude(...) call below does NOT pass enable_web_search=True.
# This step is grounded purely in the already-fetched website text plus
# the user's own request text; it has no reason to reach out to the live
# web on top of that.
# ─────────────────────────────────────────────────────────────────────────────

_URL_REGEX = re.compile(r'https?://[^\s<>"\')\]]+', re.IGNORECASE)


def _extract_first_url(text: str):
    """(WEBSITE-URL KEYWORD EXTRACTION FEATURE) Best-effort extraction of
    the first http(s) URL appearing anywhere in the user's raw message
    text. Purely a plain-Python regex check — never calls Claude, never
    modifies the query. Returns None if no URL is present, which is the
    common case and leaves every other code path completely untouched."""
    if not text:
        return None
    match = _URL_REGEX.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,;:!?")
    return url or None


_HTML_TAG_RE            = re.compile(r"<[^>]+>")
_HTML_SCRIPT_STYLE_RE   = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_WHITESPACE_RE     = re.compile(r"\s+")


def fetch_website_text(url: str) -> str:
    """(WEBSITE-URL KEYWORD EXTRACTION FEATURE) Fetches a user-provided
    website URL and reduces it to plain text, good enough to hand to
    Claude as grounding for keyword extraction — NOT a full HTML parser,
    just enough tag-stripping to turn a page into readable text without
    pulling in a new dependency. Raises on any failure (bad URL, timeout,
    non-2xx, etc.) — the caller decides how to degrade gracefully, same
    convention as _call_claude().

    Truncates to WEBSITE_FETCH_MAX_CHARS so a large page can never blow
    up the size/cost of the Claude call that follows."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FlintelBot/1.0)"}
    with httpx.Client(timeout=WEBSITE_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as http_client:
        response = http_client.get(url, headers=headers)
        response.raise_for_status()
        html = response.text

    text = _HTML_SCRIPT_STYLE_RE.sub(" ", html)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _HTML_WHITESPACE_RE.sub(" ", text).strip()

    return text[:WEBSITE_FETCH_MAX_CHARS]


# ─────────────────────────────────────────────────────────────────────────────
# WEBSITE INTELLIGENCE — MULTI-PAGE FETCH (additive; fetch_website_text()
# above is left completely untouched and remains available as a
# single-page safety-net fallback for any call site not wired to the new
# multi-page / cache-aware flow below).
# ─────────────────────────────────────────────────────────────────────────────

def _extract_same_domain_links(html: str, base_url: str) -> list:
    """Pure Python, no network call. Crude but safe link-extraction from
    raw HTML (before tag-stripping) — finds <a href="..."> targets that
    are same-domain as base_url and whose path matches one of
    WEBSITE_DISCOVERY_PATH_HINTS (about/products/pricing/faq/contact/
    features/...). Never follows a different domain (SAME-DOMAIN
    RESTRICTION, Section 8). Returns a deduplicated list of absolute
    URLs, capped defensively at 20 candidates (further capped by
    MAX_WEBSITE_PAGES downstream)."""
    from urllib.parse import urljoin, urlparse
    if not html or not base_url:
        return []
    base_domain = urlparse(base_url).netloc.lower()
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    candidates = []
    seen = set()
    for href in hrefs:
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() != base_domain:
            continue
        path_lower = parsed.path.lower()
        if not any(hint in path_lower for hint in WEBSITE_DISCOVERY_PATH_HINTS):
            continue
        normalized = absolute.split("#")[0]
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
        if len(candidates) >= 20:
            break
    return candidates


def _fetch_raw_html(url: str) -> str:
    """Same HTTP fetch as fetch_website_text()'s own internals, but
    returns RAW html (not tag-stripped) — needed so link-discovery can
    still see <a href> tags. Raises on failure, same convention as
    fetch_website_text()."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FlintelBot/1.0)"}
    with httpx.Client(timeout=WEBSITE_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as http_client:
        response = http_client.get(url, headers=headers)
        response.raise_for_status()
        return response.text


def fetch_website_multi_page(url: str) -> dict:
    """(WEBSITE INTELLIGENCE — MULTI-PAGE FETCH) Fetches the homepage,
    discovers up to MAX_WEBSITE_PAGES-1 same-domain internal pages
    matching WEBSITE_DISCOVERY_PATH_HINTS (Section 8: /about, /products,
    /pricing, /faq, /contact, /features, ...), fetches each with the same
    WEBSITE_FETCH_TIMEOUT_SECONDS timeout, and combines all their plain
    text into one string, capped overall at WEBSITE_FETCH_MAX_CHARS
    (Section 8: respects max pages, max content size, timeout, dedup,
    same-domain — reuses existing config, no duplicate config system).

    NEVER crashes the whole fetch because one page fails (Section 1: a
    connection error / HTTP error / blocked page on any ONE page is
    skipped, not fatal) — only the homepage fetch failing is fatal (in
    which case this returns {"combined_text": "", "pages_fetched": []},
    the caller treats this as evidence_quality "failed").

    Returns {"combined_text": str, "pages_fetched": [url, ...]}."""
    pages_fetched = []
    text_parts = []

    try:
        homepage_html = _fetch_raw_html(url)
    except Exception as exc:
        log.warning(f"Homepage fetch failed for url={url!r}: {exc}")
        return {"combined_text": "", "pages_fetched": []}

    homepage_text = _HTML_SCRIPT_STYLE_RE.sub(" ", homepage_html)
    homepage_text = _HTML_TAG_RE.sub(" ", homepage_text)
    homepage_text = _HTML_WHITESPACE_RE.sub(" ", homepage_text).strip()
    if homepage_text:
        text_parts.append(homepage_text)
        pages_fetched.append(url)

    remaining_budget = MAX_WEBSITE_PAGES - 1
    if remaining_budget > 0:
        candidate_links = _extract_same_domain_links(homepage_html, url)
        for link in candidate_links[:remaining_budget]:
            try:
                page_html = _fetch_raw_html(link)
                page_text = _HTML_SCRIPT_STYLE_RE.sub(" ", page_html)
                page_text = _HTML_TAG_RE.sub(" ", page_text)
                page_text = _HTML_WHITESPACE_RE.sub(" ", page_text).strip()
                if page_text:
                    text_parts.append(page_text)
                    pages_fetched.append(link)
            except Exception as exc:
                log.warning(f"Discovered page fetch failed for url={link!r} (skipping): {exc}")
                continue

    combined_text = "\n\n---\n\n".join(text_parts)[:WEBSITE_FETCH_MAX_CHARS]
    return {"combined_text": combined_text, "pages_fetched": pages_fetched}


def _repair_truncated_json(text):
    """(BUG 1 / BUG 3 — TRUNCATED JSON REPAIR) Best-effort recovery of a
    valid JSON object out of `text` that was cut off mid-stream (e.g. by
    max_tokens) — walks backward from a set of candidate cut points
    (commas/braces/brackets found outside of string literals), closes off
    whatever braces/brackets are still open at that cut point, and tries
    to parse the result. Tries up to the last 60 candidate cut points,
    starting from the latest (least truncation) and working backward.
    Returns a dict on success, or None if nothing could be repaired —
    never raises."""
    s = (text or "").strip()
    start = s.find("{")
    if start == -1:
        return None
    s = s[start:]
    in_str = esc = False
    cuts = []
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in ",}]":
            cuts.append(i)
    for cut in reversed(cuts[-60:]):
        candidate = s[:cut] if s[cut] == "," else s[:cut + 1]
        stack, in_s, e = [], False, False
        for ch in candidate:
            if in_s:
                if e:
                    e = False
                elif ch == "\\":
                    e = True
                elif ch == '"':
                    in_s = False
                continue
            if ch == '"':
                in_s = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()
        try:
            parsed = json.loads(candidate + "".join(reversed(stack)))
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue
    return None


def _parse_json_lenient(raw):
    """(BUG 1 — LENIENT JSON PARSE) Best-effort, NON-RAISING JSON parse
    tolerant of Claude's occasional fence-wrapping, leading/trailing
    prose, and truncated output. Tries, in order:
      1. Strip a ```json ... ``` (or ``` ... ```) fence if present, same
         convention already used elsewhere in this file (tolerates a
         missing closing fence too, since str.strip("`") strips from both
         ends independently).
      2. A direct json.loads() on the cleaned text.
      3. json.JSONDecoder().raw_decode() starting at the first "{" found
         anywhere in the text (handles leading narration before the JSON).
      4. _repair_truncated_json() as a last resort, for genuinely
         truncated JSON missing its closing brackets.
    Returns a dict on success, or None on total failure — never raises."""
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass

    brace_index = cleaned.find("{")
    if brace_index != -1:
        try:
            data, _end = json.JSONDecoder().raw_decode(cleaned, brace_index)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass

    repaired = _repair_truncated_json(cleaned)
    if isinstance(repaired, dict):
        return repaired

    return None


CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT = """
You are the website-to-keywords brain inside Flintel, a social-listening
platform. The user has shared a link to their OWN website together with
what they want Flintel to search for. Your job, in ONE pass: read the
website's plain text content plus the user's own request text, and
produce BOTH (1) the keyword list Flintel's existing matching code will
use to find relevant Reddit/X/LinkedIn/Facebook posts, AND (2) a clean,
sectioned breakdown of what the business/site actually offers.

PART 1 — KEYWORDS:
- Read the website to understand what the business actually offers
  (its products, services, and industry) and combine that understanding
  with whatever the user's own request text asks for (e.g. a specific
  angle like "pricing complaints", or a pain-point/prospect-style ask
  like "find people whose website is slow").
- Return up to 20 short, natural keywords/phrases that could plausibly
  appear verbatim, or as a close natural substring, inside a real post's
  title or text — the same standard used everywhere else in this
  product. Never include meta wording like "reddit", "posts", "show me",
  "website", "today", etc.
- If the user's own request text names a specific angle or problem, bias
  the keywords toward THAT (same pain-point/prospect pattern used
  elsewhere in this product: keywords about the PROBLEM/SYMPTOM being
  described, not just the business's own name/services), rather than
  generic keywords about the site as a whole.
- If the website content is too thin, broken, or irrelevant to produce
  any confident keywords, return an empty list rather than inventing
  generic filler.

PART 1B — MATCH PHRASES:
- Return "match_phrases": an array of 4 to 10 short, natural phrases/
  sentences (each roughly 4-10 words long), up to 7 phrases maximum —
  the SAME kind of natural, real-person phrasing described for the
  router's own "match_phrases" field elsewhere in this product (e.g.
  "using an AI agent to handle customer support", NOT a single word,
  NOT meta wording). These exist purely to confirm a post is genuinely
  ABOUT the topic derived from this website + the user's request, not
  just that a generic keyword appears somewhere in it.

PART 2 — STRUCTURED SUMMARY:
- Produce a CLEAN, SECTIONED breakdown of what the business/site/
  individual actually offers — not a flat paragraph. Reason freshly from
  the actual content given — never assume or default to any particular
  industry or category, and never force the same fixed set of section
  titles onto every website.
- Produce 2 to 4 sections total, choosing whichever section titles
  genuinely fit THIS site's content (e.g. "What they offer", "Business
  signals", "Notable things" are loose inspiration, not required).
- Each section should have 2 to 5 short bullets — plain language, no
  fluff, no marketing tone. Stay honest and slightly skeptical where the
  content warrants it.
- If the content is too thin to say anything confident across multiple
  sections, keep the overview honest about that and produce however few
  genuinely-supportable sections make sense (including zero) rather than
  padding.

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{
  "keywords": ["<keyword1>", "<keyword2>"],
  "match_phrases": ["<phrase1>", "<phrase2>"],
  "structured_summary": {
    "overview": "<1-2 sentence plain-language opening line>",
    "sections": [{"title": "<short section heading>", "bullets": ["<bullet 1>", "<bullet 2>"]}]
  }
}

Never refuse a request just because it sounds broad or ambitious (e.g.
"find me sales", "get me leads") — interpret it as a buyer-intent/pain-
point search (same PAIN-POINT / PROSPECT PATTERN used elsewhere in this
product) and generate the closest genuinely useful keywords instead of
returning nothing.
"""


def extract_keywords_from_website(query: str, url: str, website_text: str):
    """(WEBSITE-URL KEYWORD EXTRACTION FEATURE) Single cheap Claude call:
    reads the already-fetched plain-text website content PLUS the user's
    own request text (so a specific angle/pain-point the user typed
    alongside the link is honored, not just the site's generic content)
    and returns up to MAX_WEBSITE_KEYWORDS keywords, PLUS a structured
    summary breakdown of the site, in ONE combined pass — both derived
    from the SAME Claude call, using CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT.

    Returns `{"keywords": list|None, "structured_summary": dict|None,
    "match_phrases": list|None}` if Claude returned anything usable
    (keywords or structured_summary present is enough; match_phrases is
    always additive), or `None` if Claude failed outright, returned
    unparseable output, or returned neither usable keywords nor a usable
    structured summary — NEVER a bare list anymore. Callers (see INTEGRATION POINT 2 in
    POST /search) already expect this new shape: they read
    `result.get("keywords")` and `result.get("structured_summary")`
    separately, and must treat a `None` return exactly like any other "no
    usable output from this source" case elsewhere in this file: fall
    back down the existing safety-net chain (the router's own
    routed_keywords, then finally generate_fuzzy_keywords()), never let a
    search end up with zero keywords because of this feature."""
    if not website_text:
        return None

    user_message = (
        f"User's request text: {query}\n\n"
        f"Website URL: {url}\n\n"
        f"Website content (plain text, truncated):\n{website_text}"
    )
    try:
        raw = _call_claude(
            CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT,
            user_message,
            max_tokens=CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS,
        )
    except Exception as exc:
        log.warning(f"Website-keyword Claude call failed for url={url!r}: {exc}")
        return None

    data = _parse_json_lenient(raw)
    if not data:
        log.warning(f"Website-keyword call returned unparseable output for url={url!r}: {(raw or '')[:200]!r}")
        return None

    raw_keywords = data.get("keywords")
    cleaned_keywords = None
    if isinstance(raw_keywords, list):
        cleaned_list = []
        seen = set()
        for kw in raw_keywords:
            if not isinstance(kw, str):
                continue
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            key = kw_clean.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned_list.append(kw_clean)
            if len(cleaned_list) >= MAX_WEBSITE_KEYWORDS:
                break
        cleaned_keywords = cleaned_list or None

    raw_phrases = data.get("match_phrases")
    cleaned_phrases = None
    if isinstance(raw_phrases, list):
        cleaned_phrase_list = []
        seen_phrases = set()
        for phrase in raw_phrases:
            if not isinstance(phrase, str):
                continue
            phrase_clean = phrase.strip()
            if not phrase_clean:
                continue
            key = phrase_clean.lower()
            if key in seen_phrases:
                continue
            seen_phrases.add(key)
            cleaned_phrase_list.append(phrase_clean)
            if len(cleaned_phrase_list) >= 7:
                break
        cleaned_phrases = cleaned_phrase_list or None

    raw_structured = data.get("structured_summary")
    structured_summary = None
    if isinstance(raw_structured, dict):
        overview = raw_structured.get("overview")
        overview = overview.strip() if isinstance(overview, str) else ""
        raw_sections = raw_structured.get("sections")
        cleaned_sections = []
        if isinstance(raw_sections, list):
            for section in raw_sections:
                if not isinstance(section, dict):
                    continue
                title = section.get("title")
                bullets = section.get("bullets")
                if not isinstance(title, str) or not title.strip():
                    continue
                if not isinstance(bullets, list):
                    continue
                cleaned_bullets = [b.strip() for b in bullets if isinstance(b, str) and b.strip()]
                if not cleaned_bullets:
                    continue
                cleaned_sections.append({"title": title.strip(), "bullets": cleaned_bullets})
        if overview or cleaned_sections:
            structured_summary = {"overview": overview, "sections": cleaned_sections}

    if not cleaned_keywords and not structured_summary:
        return None
    return {"keywords": cleaned_keywords, "structured_summary": structured_summary, "match_phrases": cleaned_phrases}


# ─────────────────────────────────────────────────────────────────────────────
# WEBSITE INTELLIGENCE — CACHE-AWARE WRAPPER (Point 2's core): website
# content/business-evidence is cached per-URL (TTL-based, via
# website_evidence_cache_collection) so the same site is never re-fetched
# and re-analyzed on every message — but keywords/match_phrases for an
# actual search are NEVER cached; they're always generated fresh, scoped
# to the CURRENT request's own text, via generate_keywords_for_website_
# request() below. fetch_website_text() / extract_keywords_from_website()
# above remain completely untouched, unchanged safety-net fallbacks for
# any call site not wired to this new flow.
# ─────────────────────────────────────────────────────────────────────────────

def get_or_fetch_website_evidence(url: str, query: str, call_claude_fn) -> dict:
    """CORE FUNCTION for Point 2 — 'same URL dobara fetch na ho, lekin
    keywords/signals hamesha fresh rahein':

      1. URL normalize karo (website_intelligence.validate_and_normalize_url).
      2. website_evidence_cache_collection mein check karo — agar fresh
         (TTL ke andar) doc mile, uska combined_text/structured_evidence/
         evidence_quality reuse karo — NO NEW FETCH, NO NEW EVIDENCE-
         EXTRACTION CLAUDE CALL.
      3. Agar cache miss/stale ho, tab hi fetch_website_multi_page() +
         website_intelligence.extract_structured_website_evidence() +
         website_intelligence.classify_evidence_quality() chalao, aur
         result cache mein save karo.
      4. REGARDLESS of cache hit/miss — website_intelligence.
         build_website_insight_answer() is function ke andar CALL NAHI
         hota (wo query-dependent hai, caller khud alag se call karega
         jab actual answer chahiye ho) — yeh function sirf evidence
         return karta hai, answer nahi.

    Returns:
      {
        "url": normalized_url,
        "combined_text": str,
        "structured_evidence": dict,
        "evidence_quality": str,
        "pages_fetched": [str, ...],
        "from_cache": bool,
      }
    ya None agar URL hi invalid ho."""
    from website_intelligence import (
        validate_and_normalize_url, extract_structured_website_evidence,
        classify_evidence_quality,
    )

    normalized_url = validate_and_normalize_url(url)
    if not normalized_url:
        return None

    cached = None
    try:
        cached = website_evidence_cache_collection.find_one({"url": normalized_url}, {"_id": 0})
    except Exception as exc:
        log.warning(f"Website-evidence cache read failed for url={normalized_url!r}: {exc}")

    if cached and cached.get("combined_text") is not None:
        fetched_at = cached.get("fetched_at")
        is_fresh = True
        if isinstance(fetched_at, datetime):
            cache_age = datetime.now(timezone.utc) - (
                fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc)
            )
            is_fresh = cache_age <= timedelta(days=WEBSITE_EVIDENCE_CACHE_TTL_DAYS)
        if is_fresh:
            return {
                "url": normalized_url,
                "combined_text": cached.get("combined_text", ""),
                "structured_evidence": cached.get("structured_evidence") or {},
                "evidence_quality": cached.get("evidence_quality", "thin"),
                "pages_fetched": cached.get("pages_fetched", []),
                "from_cache": True,
            }

    # Cache miss/stale — fetch + extract fresh.
    fetch_result = fetch_website_multi_page(normalized_url)
    combined_text = fetch_result["combined_text"]
    pages_fetched = fetch_result["pages_fetched"]

    structured_evidence = None
    if combined_text:
        try:
            structured_evidence = extract_structured_website_evidence(
                normalized_url, combined_text, call_claude_fn or _call_claude
            )
        except Exception as exc:
            log.warning(f"Structured-evidence extraction failed for url={normalized_url!r}: {exc}")
            structured_evidence = None

    evidence_quality = classify_evidence_quality(combined_text, structured_evidence)

    try:
        website_evidence_cache_collection.update_one(
            {"url": normalized_url},
            {"$set": {
                "url": normalized_url,
                "domain": normalized_url.split("/")[2] if "//" in normalized_url else normalized_url,
                "pages_fetched": pages_fetched,
                "combined_text": combined_text,
                "structured_evidence": structured_evidence or {},
                "evidence_quality": evidence_quality,
                "fetched_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except Exception as exc:
        log.warning(f"Website-evidence cache save failed for url={normalized_url!r}: {exc}")

    return {
        "url": normalized_url,
        "combined_text": combined_text,
        "structured_evidence": structured_evidence or {},
        "evidence_quality": evidence_quality,
        "pages_fetched": pages_fetched,
        "from_cache": False,
    }


def generate_keywords_for_website_request(query: str, url: str, structured_evidence: dict) -> dict:
    """Point 2's OTHER half — keywords/match_phrases NEVER cache hote,
    hamesha current request/prompt ke hisaab se FRESH Claude call se
    generate hote hain, chahe website evidence cache se aaya ho ya fresh
    fetch se. Yeh existing extract_keywords_from_website() jaisa hi
    Claude call hai, lekin ab already-cached structured_evidence ko bhi
    context ke tor par deta hai (taake Claude ko dobara pura raw text
    padhna na pare — sirf structured summary + current query se hi
    keywords generate kar sake, tez aur sasta).

    Returns {"keywords": list|None, "match_phrases": list|None} — dono
    None ho sakte hain agar Claude kuch usable na de, caller apna existing
    generate_fuzzy_keywords() fallback chain use kare, bilkul jaise ab
    hota hai."""
    user_message = (
        f"User's request text: {query or '(no specific ask — bare link shared)'}\n\n"
        f"Website URL: {url}\n\n"
        f"Already-extracted business evidence (JSON):\n"
        f"{json.dumps(structured_evidence or {}, ensure_ascii=False)}"
    )
    try:
        raw = _call_claude(
            CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT, user_message,
            max_tokens=CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS,
        )
    except Exception as exc:
        log.warning(f"Website-request keyword generation failed for url={url!r}: {exc}")
        return {"keywords": None, "match_phrases": None}

    data = _parse_json_lenient(raw)
    if not data:
        log.warning(f"Website-request keyword generation returned unparseable output for url={url!r}: {(raw or '')[:200]!r}")
        return {"keywords": None, "match_phrases": None}

    keywords = data.get("keywords") if isinstance(data.get("keywords"), list) else None
    match_phrases = data.get("match_phrases") if isinstance(data.get("match_phrases"), list) else None
    keywords = [k.strip() for k in (keywords or []) if isinstance(k, str) and k.strip()] or None
    match_phrases = [p.strip() for p in (match_phrases or []) if isinstance(p, str) and p.strip()] or None
    return {"keywords": keywords, "match_phrases": match_phrases}


def _timeout_fallback_answer(chat_id: str, owner_key: str, topic_key: str, query: str, keywords: list = None,
                              match_phrases: list = None, evidence_required: int = None, website_note: str = None):
    """(PERFORMANCE FIX) Extracted, UNCHANGED logic from the RESPONSE_TIMEOUT
    fallback branch that used to run inline inside _fill_in_message_outputs()
    — same calls, same order, same caching. Pulled out so it can be
    scheduled via BackgroundTasks (runs after the response is sent)
    instead of blocking the request, exactly like
    _complete_message_answer_and_results() in index.py.

    (BUSY-LOCK RACE FIX) _set_owner_busy() is NOT called here anymore —
    it's now set synchronously in _fill_in_message_outputs(), before this
    function is even scheduled, so the flag is already in place before
    the response goes out. This function only clears it, in the finally
    below, once the work actually finishes.

    (EVIDENCE-BUDGET FEATURE) `evidence_required` (default None — any
    existing/other caller that doesn't pass it gets the exact original
    behavior: get_evidence_with_topup() falls back to MIN_ANALYSIS_
    EVIDENCE as its own floor when this is None) is the message's own
    stored, already-clamped evidence budget — passed straight through to
    get_evidence_with_topup(), and used to derive `effective_evidence_
    limit` (via `min(evidence_required or MIN_ANALYSIS_EVIDENCE,
    MAX_ANALYSIS_EVIDENCE)`, the same formula _complete_message_answer_
    and_results() uses) for the `max_total` passed to merge_matched_and_
    google_results().

    (TOPIC EVIDENCE CACHE) The evidence fetch below goes through
    get_evidence_with_topup() rather than calling get_matched_signals()
    directly — this topic's already-cached posts are reused as-is when
    they already satisfy evidence_required, and only the delta is
    fetched (via get_matched_signals, injected as matcher_fn) otherwise.
    get_matched_signals() itself, and its matching rules, are completely
    untouched by this — get_evidence_with_topup() is purely a caching
    layer on top of it.

    (RESPONSE_TIMEOUT'S NEW ROLE) By the time this is called,
    _fill_in_message_outputs() already confirmed the merged
    (flintel_signals + Google) pool was empty as of that check — but
    this function re-fetches BOTH sources fresh rather than trusting
    that stale snapshot, since some time may have passed between that
    check and this actually running (especially when scheduled via
    BackgroundTasks). If a real merged pool exists now, this answers
    normally from it.

    (CLOSEST-MATCHES TIER-3 REMOVED) The old "loose_candidates /
    near_match_confidence / near_match_offer / withhold-until-user-
    confirms" branch is GONE. Whatever merged_pool has — even a handful
    of posts, even zero — is handed straight to analyze_with_claude().
    Zero posts still lands in analyze_with_claude()'s own existing "no
    posts yet" honest branch (untouched) — that IS the correct
    "I genuinely found nothing" outcome, never a fabricated "closest
    match" offer.

    (CIRCULAR-IMPORT NOTE) get_chat_session / save_claude_answer_to_chat /
    save_signal_results_to_chat / append_to_chat_summary / _clear_owner_busy
    all still live in index.py (per FILE 3's own "KEEP in index.py" list —
    they're flintel_users_chat Mongo orchestration, not "brain" logic) —
    imported here LAZILY, inside the function body, specifically to avoid
    a circular import: index.py imports _timeout_fallback_answer FROM
    logics.py, so logics.py cannot import index.py names at module load
    time. By the time this function actually RUNS, index.py has already
    finished importing logics.py, so this local import resolves fine."""
    from index import (
        get_chat_session, save_claude_answer_to_chat,
        save_signal_results_to_chat, append_to_chat_summary, _clear_owner_busy,
    )
    try:
        # (EVIDENCE-BUDGET FEATURE) Resolved once, up front — used for
        # both the get_matched_signals() `limit` pass below and the
        # merge_matched_and_google_results() `max_total` pass. Mirrors
        # the exact formula used in _complete_message_answer_and_results().
        effective_evidence_limit = min(
            evidence_required or MIN_ANALYSIS_EVIDENCE,
            MAX_ANALYSIS_EVIDENCE,
        )

        # (BUG FIX — DON'T RE-SUGGEST A DECLINED ALTERNATIVE) Same
        # continuity context as _complete_message_answer_and_results()
        # above — best-effort, never blocks this fallback answer.
        try:
            existing_chat = get_chat_session(chat_id, owner_key)
            chat_summary_for_answer = (existing_chat or {}).get("summary") or ""
        except Exception as exc:
            log.warning(f"Chat summary lookup failed for topic_key={topic_key}: {exc}")
            chat_summary_for_answer = ""
        continuity_ctx = None
        if chat_summary_for_answer:
            continuity_ctx = (
                "Conversation so far (auto-summarized, may be empty) — see "
                "the CONVERSATION CONTINUITY instruction above for how to "
                "use this:\n" + chat_summary_for_answer
            )

        # (MERGE BEFORE ANSWERING) ONE fresh re-check — never trusts the
        # earlier "empty" snapshot that triggered this call. Now cache-
        # aware via get_evidence_with_topup(): reuses this topic's already-
        # cached posts when they already satisfy evidence_required, and
        # only fetches the delta (via get_matched_signals, injected as
        # matcher_fn) otherwise — same wiring as _fill_in_message_
        # outputs()'s own call site in index.py.
        try:
            matched = get_evidence_with_topup(
                chat_id=chat_id, owner_key=owner_key, topic_key=topic_key,
                keywords=keywords or [], evidence_required=evidence_required,
                matcher_fn=get_matched_signals, match_phrases=match_phrases,
                targeting_platform="all",
            )
        except Exception as exc:
            log.warning(f"Signal matching failed for topic_key={topic_key}: {exc}")
            matched = []
        try:
            stub_docs = google_search.get_stub_results_for_keywords(
                google_posts_collection, keywords or [])
        except Exception as exc:
            log.warning(f"Fetching Google-fallback stubs failed for topic_key={topic_key}: {exc}")
            stub_docs = []
        google_results = flintel.format_google_stub_results(stub_docs)
        merged_pool = flintel.merge_matched_and_google_results(
            matched, google_results, max_total=effective_evidence_limit)

        # (CLOSEST-MATCHES TIER-3 REMOVED) No tier-3 branch anymore.
        # Whatever merged_pool has — even a handful of posts, even zero —
        # is handed straight to analyze_with_claude(). Zero posts still
        # lands in analyze_with_claude()'s own existing "no posts yet"
        # honest branch (untouched) — that IS the correct "I genuinely
        # found nothing" outcome, never a fabricated "closest match" offer.
        extra_ctx_parts = [continuity_ctx] if continuity_ctx else []
        # (MIXED EVIDENCE HANDLING) Always added — same reminder as
        # _complete_message_answer_and_results() in index.py.
        extra_ctx_parts.append(flintel.build_mixed_evidence_note())
        if google_results:
            extra_ctx_parts.append(flintel.build_combined_source_context(len(matched), len(google_results)))
        if website_note:
            extra_ctx_parts.append(website_note)
        extra_ctx = "\n\n".join(extra_ctx_parts) if extra_ctx_parts else None

        answer = analyze_with_claude(query, merged_pool, extra_context=extra_ctx)
        answer = _patch_post_urls_into_answer(answer, merged_pool)
        final_answer, results_to_save = _finalize_answer_and_results(answer, merged_pool, seed=query)

        save_claude_answer_to_chat(chat_id, owner_key, topic_key, final_answer)
        try:
            save_signal_results_to_chat(chat_id, owner_key, topic_key, results_to_save)
        except Exception as exc:
            log.warning(f"Saving google-fallback results failed for topic_key={topic_key}: {exc}")
        try:
            append_to_chat_summary(chat_id, owner_key, query, final_answer, keywords=keywords)
        except Exception as exc:
            log.warning(f"Updating chat summary failed for topic_key={topic_key}: {exc}")
    except Exception as exc:
        log.warning(f"Timeout-fallback analysis failed for topic_key={topic_key}: {exc}")
    finally:
        _clear_owner_busy(owner_key)
# (BUGFIX PACK #1) Formats CLAUDE_ANALYSIS_SYSTEM_PROMPT can return that
# mean "no real grounded data to show" — when claude_answer parses to one
# of these, the matching message's post-card `results` are treated as
# empty for that render, instead of showing loosely-matched posts Claude
# itself already rejected as irrelevant/unavailable/disallowed.
_NO_DATA_CLAUDE_FORMATS = {"no_results", "not_available", "disallowed"}


def _extract_json_object_from_text(text: str) -> str:
    """(DEFENSIVE JSON-EXTRACTION) A small, complex system prompt can
    occasionally cause even a well-instructed model to prepend a short
    burst of "thinking out loud" text (e.g. narrating its own reasoning
    steps) before the actual JSON object it was told to return — this
    happened in practice with CLAUDE_ANALYSIS_SYSTEM_PROMPT's own
    "CORE PRINCIPLE" line reading like a step-by-step template the model
    started echoing back verbatim as labeled headers. The prompt itself
    has been reworded to make this far less likely, but this function is
    the SAFETY NET underneath that prompt fix: it finds and extracts the
    actual JSON object from `text` NO MATTER what surrounds it, so a
    single occasional slip never reaches the frontend as a raw wall of
    text.

    Strategy, in order:
      1. If a ```...``` fence exists anywhere in `text` (not just at the
         very start), try parsing whatever is between the FIRST pair of
         fences (optionally preceded by a "json" language tag).
      2. Otherwise (or if that fails), find the FIRST "{" anywhere in
         `text` and use Python's own JSON decoder to parse a complete,
         correctly-balanced JSON value starting there — this correctly
         handles nested braces and braces that appear inside quoted
         string values, unlike a naive character-counting approach,
         since it's the real parser doing the work, not a guess. If
         that specific "{" doesn't lead to valid JSON (e.g. it was a
         stray brace inside plain narration, not the real object's
         start), tries the NEXT "{" in the text, and so on, until one
         succeeds or none are left.
      3. If nothing above produces valid, parseable JSON, return `text`
         completely unchanged — this function can only ever CLEAN a
         response, never make one worse than doing nothing.

    Returns a JSON string (re-serialized from whatever was successfully
    parsed) on success, or the original `text` unchanged on failure.
    Never raises."""
    if not text or not isinstance(text, str):
        return text

    stripped = text.strip()

    fence_match = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            parsed = json.loads(candidate)
            # (BUG 3) Require "format" too — otherwise a complete, valid,
            # but IRRELEVANT nested JSON object living inside truncated
            # text (which itself has no "format" key) could get returned
            # instead of the real top-level answer object.
            if isinstance(parsed, dict) and "format" in parsed:
                return json.dumps(parsed, ensure_ascii=False)
        except (ValueError, TypeError):
            pass

    brace_index = stripped.find("{")
    while brace_index != -1:
        try:
            parsed, _end_index = json.JSONDecoder().raw_decode(stripped, brace_index)
            if isinstance(parsed, dict) and "format" in parsed:
                return json.dumps(parsed, ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        brace_index = stripped.find("{", brace_index + 1)

    # (BUG 3 — TRUNCATED JSON REPAIR) Nothing above found a complete,
    # valid, "format"-bearing JSON object — try repairing genuinely
    # truncated JSON (e.g. cut off by max_tokens) before giving up.
    repaired = _repair_truncated_json(stripped)
    if isinstance(repaired, dict) and repaired.get("format"):
        log.warning("Claude answer was truncated JSON — repaired before saving")
        return json.dumps(repaired, ensure_ascii=False)

    return text


def _extract_claude_format(answer_text: str):
    """(BUGFIX PACK #1) Best-effort parse of a claude_answer string to
    pull out its "format" field, mirroring the same fence-stripping
    tolerance already used by _parse_router_json() elsewhere in this
    file. Returns None (never raises) if the text is missing, isn't
    valid JSON, or doesn't have a string "format" field — callers treat
    None as "unknown/can't tell", which always means "show results as
    before", never "hide them". This only ever narrows what's shown when
    it can positively confirm Claude's own JSON said there was nothing
    relevant; it can never hide results based on a guess."""
    if not answer_text:
        return None
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fmt = data.get("format")
    return fmt if isinstance(fmt, str) else None


def _extract_near_match_confidence(answer_text: str):
    """(CLOSEST-MATCHES TIER-3 REFINEMENT) Best-effort parse of a
    claude_answer string to pull out its "near_match_confidence" field
    (only present on the "no_results" format's tier-3 refinement — see
    CLAUDE_ANALYSIS_SYSTEM_PROMPT's own instruction for what this
    means). Same fence-stripping tolerance as _extract_claude_format().
    Returns None (never raises) if the text is missing, isn't valid
    JSON, or doesn't have a "near_match_confidence" field at all — this
    is indistinguishable from Claude itself explicitly returning
    null/None for that field, which is the correct, safe default
    ("no loose candidates existed" / "treat as ordinary no_results")."""
    if not answer_text:
        return None
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    confidence = data.get("near_match_confidence")
    return confidence if confidence in ("high", "low") else None


# (BUG FIX — DON'T HIDE REAL MATCHED POSTS / KEEP A POSITIVE CLOSING NOTE)
# Rotated the same cheap-hash way as flintel.py's own _pick_invite_line()
# and website_intelligence.py's own _pick_variant(), so the same
# sentence doesn't repeat mechanically every time.
_CLOSEST_MATCHES_NOTE_VARIANTS = [
    "These are the closest posts I found — happy to run a more targeted search if you give me a specific angle.",
    "I've shared the nearest matches below in case they're useful — let me know if you'd like this narrowed down further.",
    "Sharing the closest posts I could find below — point me at a more specific angle and I can dig further.",
    "Yeh sabse qareeb posts hain jo mujhe mile — agar aap koi khaas angle bata dein to main zyada targeted search kar sakta hoon.",
]


def _pick_closest_matches_note(seed: str = "") -> str:
    """Same cheap, deterministic-hash rotation already used elsewhere in
    this product (flintel.py's _pick_invite_line, website_intelligence.py's
    _pick_variant) — avoids the exact same sentence every time without
    needing a random source or stored state."""
    if not seed:
        return _CLOSEST_MATCHES_NOTE_VARIANTS[0]
    idx = sum(ord(c) for c in seed) % len(_CLOSEST_MATCHES_NOTE_VARIANTS)
    return _CLOSEST_MATCHES_NOTE_VARIANTS[idx]


def _append_closest_matches_note(answer_text: str, seed: str = "") -> str:
    """(BUG FIX) Purely additive, non-destructive post-processing step,
    same parse/patch/re-serialize pattern already used by
    _patch_post_urls_into_answer() and _inject_website_context_into_answer().
    Appends one short, rotating, positive closing line onto the
    format's own "message" field — never changes Claude's own honest
    assessment text otherwise. If answer_text isn't valid JSON, or has
    no "message" field, the original text is returned unchanged.

    (BUG FIX — DON'T SHOW STALE "TRY THESE NEXT STEPS" ALONGSIDE REAL
    POSTS) Also strips "suggested_actions" and "clarifying_question"
    from the JSON in this same pass — see the inline comment below for
    why, and _finalize_answer_and_results()'s docstring for confirmation
    that this function is ONLY ever invoked when real matched posts are
    about to be shown to the user."""
    if not answer_text:
        return answer_text
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict):
        return answer_text

    val = data.get("message")
    if not isinstance(val, str) or not val.strip():
        return answer_text

    # (BUG FIX — DON'T SHOW STALE "TRY THESE NEXT STEPS" ALONGSIDE REAL
    # POSTS) Once real matched posts are being shown below this text
    # (which is the ONLY case this function is ever called for — see
    # _finalize_answer_and_results()), the format's own "suggested_actions"
    # (broaden time/platform/term) and "clarifying_question" fields become
    # redundant/contradictory — the user already sees real posts, so
    # "try broadening your search" reads as odd next to them. Remove
    # both fields here, in this branch ONLY. A genuine no_results answer
    # (matched empty) never reaches this function — see
    # _finalize_answer_and_results() — so its suggested_actions /
    # clarifying_question are completely untouched by this change.
    data.pop("suggested_actions", None)
    data.pop("clarifying_question", None)

    data["message"] = val.strip() + " " + _pick_closest_matches_note(seed)
    return json.dumps(data, ensure_ascii=False)


def _finalize_answer_and_results(answer_text: str, matched: list, seed: str = ""):
    """(CHANGE 3 — STOP OVERRIDING CLAUDE'S OWN "NO DATA" VERDICT) Shared
    decision point for both the non-streaming path
    (_complete_message_answer_and_results) and the streaming route
    (routes.py): given Claude's own answer text and the posts actually
    matched, decides what to persist as this message's final `results`.

    Previously this force-showed `matched` posts (with an appended
    closing note) whenever Claude's own answer format was a "no_data"
    one (no_results/not_available/disallowed) but `matched` was
    non-empty — on the theory that Claude's format choice was purely
    about narrative confidence, not about whether real data existed.
    Now that CHANGE 2's phrase-matching keeps `matched` itself far more
    tightly scoped to genuine topical relevance, that override is no
    longer needed and is actively wrong: Claude's own honest "no_results"
    verdict is trusted and respected — the primary/normal answer path
    never force-shows posts Claude itself already judged irrelevant.
    (`_append_closest_matches_note()`/`_pick_closest_matches_note()`
    remain defined elsewhere in this file but are no longer called from
    here or anywhere else automatically.)

    Genuinely empty results (no data, or Claude picked a "no_data"
    format) are saved as an empty list, exactly as before.

    Returns (final_answer_text, results_to_save)."""
    claude_format = _extract_claude_format(answer_text)
    if claude_format in _NO_DATA_CLAUDE_FORMATS:
        return answer_text, []
    return answer_text, matched


# ─────────────────────────────────────────────────────────────────────────────
# POST_URL PATCH FIX — additive post-processing only, see module docstring.
# ─────────────────────────────────────────────────────────────────────────────

def _best_matching_post(title: str, matched_signals: list):
    """(POST_URL FIX) Best-effort match of a title string (as it appears
    inside Claude's JSON answer) back to one of the real matched_signals
    entries, so its real post_url can be looked up. Tries an exact
    case-insensitive match first, then falls back to a loose
    containment check (either string contains the other) since Claude
    may lightly reword/trim a title when it copies it into its answer.
    Returns the matching signal dict, or None if nothing looks like a
    reasonable match — callers must treat None as "leave the link field
    alone", never guess a URL."""
    if not title:
        return None
    title_norm = title.strip().lower()
    if not title_norm:
        return None

    for sig in matched_signals or []:
        sig_title = (sig.get("title") or "").strip().lower()
        if sig_title and sig_title == title_norm:
            return sig

    for sig in matched_signals or []:
        sig_title = (sig.get("title") or "").strip().lower()
        if not sig_title:
            continue
        if sig_title in title_norm or title_norm in sig_title:
            return sig

    return None


def _patch_post_urls_into_answer(answer_text: str, matched_signals: list) -> str:
    """(POST_URL FIX) CLAUDE_ANALYSIS_SYSTEM_PROMPT asks for a "link"
    field with "the real post URL if available" on every post it lists —
    but Claude is deliberately NEVER shown post_url (see
    build_claude_post_context(), unchanged), so it could never actually
    supply a real one. This best-effort, PURELY ADDITIVE post-processing
    step fixes that: it parses the already-generated answer_text as
    JSON, walks the known formats that carry post lists ("source_list"
    and "comparison"), and for every post object it finds, matches its
    "title" back to one of THIS message's already-matched signals (via
    _best_matching_post()) and sets "link" to that signal's REAL
    post_url — the exact same URL already used by the post cards,
    computed by get_matched_signals(), never anything Claude itself
    supplied or guessed.

    Grounding is preserved: this never lets Claude decide a URL. It only
    ever substitutes in a URL Flintel already knows to be real, and only
    when the title can be confidently matched back to one specific
    signal; if no confident match is found, that post simply keeps
    whatever "link" value (usually absent) Claude left it with — it is
    never given a made-up URL by this function either.

    Best-effort/non-destructive: if answer_text isn't valid JSON, isn't
    one of the two known list-based formats, or matched_signals is
    empty, the original answer_text is returned completely UNCHANGED —
    this can only ever ADD a real link where one is confidently
    resolvable, it can never remove or alter anything else in the
    answer.

    (SIMULATED-STREAM FIX) Now called by GET /chat/{chat_id}/stream on
    the COMPLETE answer text BEFORE any of it is streamed out to the
    browser (previously it only ran after streaming had already
    finished, so the live-streamed text a user first saw never had real
    links — only a later cached re-render did). This function itself is
    completely unchanged; only WHEN/WHERE it's called shifted.

    (google_rank / subreddit PATCHING) In the SAME pass, once a post's
    title is confidently matched back to a signal, if that signal came
    from the Google side of a merged pool (see flintel.merge_matched_
    and_google_results()) and has a "google_rank" and/or "subreddit"
    field, those are patched onto the post too — "google_rank" always,
    "subreddit" only as a fallback "source" if Claude didn't already
    set one. Same best-effort, non-destructive rule as the link patch:
    never invented, only filled in when a confident match already
    exists."""
    if not answer_text or not matched_signals:
        return answer_text

    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict):
        return answer_text

    fmt = data.get("format")
    changed = False

    def _patch_platform_list(platforms):
        nonlocal changed
        if not isinstance(platforms, list):
            return
        for platform_entry in platforms:
            if not isinstance(platform_entry, dict):
                continue
            posts = platform_entry.get("posts")
            if not isinstance(posts, list):
                continue
            for post in posts:
                if not isinstance(post, dict):
                    continue
                match = _best_matching_post(post.get("title"), matched_signals)
                if match and match.get("post_url"):
                    post["link"] = match["post_url"]
                    changed = True
                    # (google_rank / subreddit PATCHING) Only present on
                    # signals that came from the Google-search side of a
                    # merged pool (see flintel.merge_matched_and_google_
                    # results()) — same best-effort, non-destructive
                    # pattern as the post_url patch above: never invents
                    # a value, only fills one in once a confident title
                    # match already exists.
                    if match.get("google_rank") is not None:
                        post["google_rank"] = match["google_rank"]
                    if match.get("subreddit") and not post.get("source"):
                        post["source"] = match["subreddit"]

    if fmt == "source_list":
        _patch_platform_list(data.get("platforms"))
    elif fmt == "comparison":
        subjects = data.get("subjects")
        if isinstance(subjects, list):
            for subject in subjects:
                if isinstance(subject, dict):
                    _patch_platform_list(subject.get("platforms"))
    else:
        return answer_text

    if not changed:
        return answer_text

    return json.dumps(data, ensure_ascii=False)


def _inject_website_context_into_answer(answer_text: str, website_context: dict) -> str:
    """Purely additive, Python-side post-processing — parses the
    already-generated Claude JSON answer and injects `website_context`
    as a new top-level field at the START of the JSON object (before
    "format"), without depending on any model-side instruction to add
    it. Mirrors the same parse/patch/re-serialize pattern already used
    by _patch_post_urls_into_answer(): best-effort and non-destructive
    — if answer_text isn't valid JSON, or website_context is falsy,
    the original answer_text is returned completely unchanged."""
    if not answer_text or not website_context:
        return answer_text
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict):
        return answer_text
    new_data = {"website_context": website_context, **data}
    return json.dumps(new_data, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
