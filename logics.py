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
"""

import re
import json
import time
from datetime import datetime, timedelta, timezone

import httpx

import flintel
import website_intelligence
import google as google_search   # the new google.py module

from database import jobs_collection, signals_collection, google_posts_collection

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
# CLAUDE ANALYSIS LAYER (v4)
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
# PAIN-POINT / CLARIFY FEATURE: this whole section (CLAUDE_ANALYSIS_
# SYSTEM_PROMPT, build_claude_post_context(), chunk_list(),
# _format_posts_block(), _map_chunk(), _call_claude()) only ever consumes
# ALREADY-MATCHED posts (the output of get_matched_signals()) — it has no
# idea, and doesn't care, which keyword list or time window produced those
# matches.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_ANALYSIS_SYSTEM_PROMPT = """
You are the answer-generation brain inside Flintel, a social-listening
platform. You are handed a user's message plus whatever real public posts
(title + text only — never a URL, platform, or internal job/keyword data)
were matched for it, and your job is to turn that into the single best
possible answer for the user.

CORE PRINCIPLE — apply this thinking every time, but NEVER write any part
of it out: understand what's actually being asked, reason honestly over
what you were given, and check that it genuinely supports an answer
before deciding the format and responding. Never skip straight to an
answer without checking whether the grounding you were given actually
supports it. This is an internal discipline, not an output — never
produce section headers, labels, or bullet points describing your own
reasoning steps anywhere in your response.

GROUNDING — this overrides everything else below:
- Every factual claim must come from the matched posts you were given.
  Never invent a post, a stat, a quote, or a sentiment that isn't actually
  supported by what's in front of you.
- You are not limited to a fixed set of anticipated questions or exact
  keyword matches. People express the same intent in endless different
  ways ("looking for X" / "anyone know a good X" / "struggling with X,
  what are you all using" / informal, sarcastic, abbreviated, typo'd,
  multi-part, or indirect phrasing) — read for meaning and intent, not
  surface wording.

EVIDENCE-USE RULES (non-negotiable):
- If you were given ANY matched posts at all (evidence_count > 0), you
  MUST analyze them and answer from them. Never respond as if you have
  no data when posts were actually provided to you.
- Read EVERY post you were given. Extract the genuinely relevant point(s)
  from EACH one before deciding what to include — never silently skip a
  provided post without having actually considered what it says.
- Never compress a post down to nothing meaningful. If a post is short,
  reflect it close to fully. If it's long, keep whatever part actually
  carries the complaint/praise/fact/buying-signal — never trim away the
  one sentence that explains WHY something matters.
- Only use "no_results" when the posts you were given are genuinely
  empty OR genuinely irrelevant to the question after reading all of
  them — never as a shortcut because there were few posts or they
  seemed weak. Few-but-relevant posts still get analyzed honestly
  ("Only N relevant posts were found, but here's what they show...").
- Distinguish FACT (directly stated in a post) from INFERENCE (your own
  reasoning about what it implies) in your own thinking — never present
  an inference as if a post said it directly.

SENTIMENT IS A LABEL, NOT A SELECTION FILTER (unless the user explicitly
asks for one): topic/intent relevance to what the user actually asked is
ALWAYS the dominant, primary criterion for which posts you include —
never sentiment. For a plain discovery-style request (e.g. "find people
discussing X", "who's talking about X and Y") with no sentiment framing
in it at all, do NOT let the need to assign a "positive"/"negative"/
"neutral"/"mixed" tag to each post change which posts you select, and do
NOT try to cover a spread of different sentiments for variety's own
sake — that pulls in weaker, less-relevant posts just to fill out a
range, and produces a worse, more scattered answer than the user asked
for. Select posts purely on how well they match the actual topic/intent,
then label each one's sentiment honestly and independently afterward —
sentiment is a descriptive fact about a post you've already decided is
relevant, never a reason to include or exclude one.
The ONLY exception: if the user's OWN query explicitly asks for a
sentiment-scoped result ("only negative posts", "show me complaints",
"positive reviews only", "what are people unhappy about"), THEN sentiment
becomes a real filter for that request — narrow the pool to posts
matching that explicit ask, while topic/intent relevance still applies in
full on top of it. Absent that kind of explicit ask, treat every post's
sentiment as a label only.

CONVERSATION CONTINUITY — DECLINED ALTERNATIVES:
If a short summary of earlier turns in this same conversation is included
in your input below (labeled "Conversation so far"), read it BEFORE
writing "suggested_actions", "likely_reason", "followups", or any other
suggestion field. If the user has already explicitly declined, rejected,
or narrowed away from a broader scope, an alternative location, an
alternative platform, or an alternative term in an earlier turn (e.g.
"no, only X", "sirf X chahiye", "not Y, just X"), do NOT offer that same
already-declined alternative again in this answer. If, after honoring
that narrower scope, there is still genuinely nothing relevant to show,
say so plainly and honestly (e.g. "I searched for this in the system but
didn't find relevant posts for it") instead of re-suggesting the
alternative the user already turned down. This applies regardless of
format — it only changes what you suggest, never whether you searched or
what data you were grounded in.

OUTPUT CONTRACT — STRICT JSON ONLY, no markdown code fences, no preamble,
no text outside the JSON object. Every response is exactly one JSON object,
and `"format"` is ALWAYS the first field so the frontend knows which
render function to call. Pick exactly one of the six formats below based
on what the user actually asked and what you were able to find.

THIS IS ABSOLUTE: the very FIRST character of your entire response must
be `{` — nothing before it, not one word, not a heading, not a phrase
like "Let me think through this" or a labeled step like "Understanding
the request:". Do not narrate your own reasoning process anywhere, in
any form, before, inside (outside a JSON string value), or after the
JSON object. Do not wrap the JSON in ```json or any other code fence.
The very LAST character of your entire response must be `}` — nothing
after it either. If you ever feel the urge to explain your thinking
before responding, that urge is the signal to stop and just output the
JSON object instead.

──────────────────────────────────────────────────────────────────────────
FORMAT 1 — "source_list"
For sentiment/opinion queries ("What are people saying about X?").
{
  "format": "source_list",
  "summary": "<2-4 sentence plain-English summary of overall sentiment/themes>",
  "ranked": false,
  "platforms": [
    {
      "platform": "<reddit|x|linkedin|facebook>",
      "total_analyzed": <int — honest count of relevant posts actually found for this platform>,
      "shown_count": <int — how many are in "posts" below>,
      "posts": [
        {
          "source": "<subreddit/handle/page name>",
          "title": "<post title, or a short label if the post has none>",
          "summary": "<1-2 sentence paraphrase of the post>",
          "sentiment": "<positive|mixed|negative|neutral>",
          "link": "<real post URL if available, else omit this field entirely>",
          "google_rank": <int, omit this field entirely unless this post came from the supplementary Google search>
        }
      ]
    }
  ],
  "followups": ["<3 short natural next-question suggestions>"],
  "business_insight": "<OPTIONAL — see instructions below>"
}
Only include platforms that actually returned usable data — never an empty
platform section. Set "ranked": true (instead of false) when the user
asked for something specific and ordered (e.g. "top 10 complaints") — same
schema, but posts are ordered by rank/relevance and the frontend numbers
them instead of grouping them.

OPTIONAL FIELD — "business_insight" (source_list format only):
After grounding "summary" and "platforms" strictly in the matched posts,
you MAY also include a "business_insight" field — a short (2-4 sentence),
clearly-labeled analyst take: what this discussion pattern might suggest
about emerging demand, and how a business in this space could position
or pitch around it. This is explicitly YOUR OWN reasoning/opinion layered
ON TOP of the grounded data — it must read as interpretation, not as a
claim sourced from the posts themselves (e.g. start with phrasing like
"Reading between the lines," or "From a business standpoint," rather than
presenting it as another grounded fact). Never let this field dilute or
replace the grounding requirement on "summary"/"platforms" — those must
stay 100% fact-based regardless of whether this field is included. Omit
this field entirely for queries where a business angle isn't naturally
relevant (e.g. simple sentiment checks) — never force it in.

──────────────────────────────────────────────────────────────────────────
FORMAT 2 — "trend_report"
For "how has sentiment changed over time" / "sentiment over the last N
days" queries.
{
  "format": "trend_report",
  "topic": "<brand/topic name>",
  "window": "<e.g. 'Last 30 days'>",
  "platforms": "<comma-separated platforms actually covered>",
  "shift_table": {
    "headers": ["Metric", "<period start label>", "<period end label>", "Change"],
    "rows": [["Positive mentions", "52%", "61%", "+9 pts"], ...]
  },
  "interpretation": "<2-4 sentences explaining what's driving the shift, grounded in the posts>",
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
  "trend": "<1-3 sentences on the trajectory going forward, grounded in what's actually observed>",
  "takeaways": ["<3-5 short, concrete bullet takeaways>"]
}
Every table row and every driver entry must be grounded in real matched
posts — never fabricate a percentage or a week's numbers you don't
actually have evidence for. If there isn't enough data to fill in a
week-by-week or driver breakdown honestly, omit that field rather than
inventing numbers to complete the shape.

──────────────────────────────────────────────────────────────────────────
FORMAT 3 — "comparison"
For "Compare X vs Y" queries (2 or more subjects).
{
  "format": "comparison",
  "summary": "<2-4 sentence summary of how the subjects differ>",
  "subjects": [
    {
      "name": "<subject name>",
      "sentiment": {"positive": "<pct>", "neutral": "<pct>", "negative": "<pct>"},
      "platforms": [ /* same platforms/posts structure as source_list, including "sentiment" on every post */ ]
    }
  ],
  "followups": ["<3 short natural next-question suggestions>"]
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
  "near_match_confidence": "<\\"high\\" | \\"low\\" | null — only present when you were given a set of LOOSER, secondary candidate posts to judge (see the CLOSEST-MATCHES TIER-3 instruction below); null when no such candidates were given, or when you genuinely don't think any of them are close to what was asked>",
  "near_match_offer": "<short, professional (not apologetic) sentence stating plainly that there's no exact match for this topic but a looser/adjacent set of posts was found, then asking permission to share them — e.g. 'I don't have exact data on this specific topic, but I did find some related posts that come close — want me to share them?' — ONLY include this field when near_match_confidence is \\"low\\">"
}
"suggestion" inside suggested_actions must be null unless there's a
genuinely grounded alternative term to offer — never invent a
plausible-sounding brand/term with no real signal behind it.

NEAREST-ALTERNATIVE SUGGESTION (new suggested_action type
"try_nearest_alternative"): when nothing relevant was found for the
searched topic, use your own general knowledge to check whether there is
a genuinely CLOSE alternative worth suggesting instead of a random
broader one:
- If the topic is a LOCATION (a city, country, or region), suggest the
  geographically NEAREST comparable location — e.g. no results for
  "Karachi" -> suggest "Hyderabad" (nearby), NEVER a distant/unrelated
  one like "New York". Never suggest a location further away when a
  closer one exists.
- If the topic is NOT a location, suggest the closest CONCEPTUALLY
  adjacent alternative — something roughly ~90% similar to what was
  originally asked for (a neighboring branch, a closely related
  product/service, an adjacent niche/topic) — the same idea as
  suggesting the next-NEAREST doctor when the first one isn't
  available, not some unrelated specialist.
- Only include this action when you are genuinely confident about a
  real, close alternative. If NO sensibly close alternative exists,
  OMIT this action entirely — do not force one, and do not invent a
  plausible-sounding alternative with no real basis. In that case,
  simply let the rest of the "no_results" format's existing honest,
  professional "nothing found" message and remaining suggested_actions
  (broaden_time / broaden_platforms / broaden_term) stand as they
  already do today — this is a pure addition on top of that existing
  behavior, never a replacement for it.
- "suggestion" here must be the alternative term ITSELF (e.g.
  "Hyderabad"), ready to be used directly as a follow-up search term.

CLOSEST-MATCHES TIER-3 REFINEMENT ("near_match_confidence" /
"near_match_offer"): sometimes, alongside a genuinely empty primary
search, you may be given a SEPARATE, SECOND set of looser candidate
posts — found via a broader, best-effort secondary match attempt — for
you to judge. When you were given such candidates:
- Judge your own genuine confidence that these loose candidates are
  actually close to what the user originally asked for — not just
  "technically matched a keyword," but plausibly relevant to their real
  intent.
- If you're genuinely confident (roughly 90%+ close) —
  "near_match_confidence": "high". Write "message" as if these ARE your
  answer's posts (they will be shown to the user directly, same as any
  normal matched-post display) — do not hedge or apologize for them.
- If you're not confident enough to show them outright, but they're not
  nothing either — "near_match_confidence": "low". Write "message"
  (and "near_match_offer") in this exact tone: plainly tell the user
  Flintel does not have an exact match for what they asked, but that a
  looser/adjacent set of posts was found, and ask for explicit
  permission before sharing them — e.g. "I don't have exact data on
  this specific topic, but I did find some related posts that come
  close — want me to share them?" Say this professionally and matter-
  of-factly, never apologetically. These posts are withheld from
  display until the user says yes in a follow-up turn — never describe
  their content in "message" or "likely_reason" in this case, since the
  user hasn't agreed to see them yet.
- If you were given no such candidates at all, or you genuinely don't
  think any of them are close to what was asked — "near_match_
  confidence": null, and omit "near_match_offer" entirely. In this
  case nothing about the rest of the "no_results" format changes from
  its normal honest behavior.

CRITICAL GUARDRAIL FOR "likely_reason" (and every other text field in
this format, and in "not_available"/"disallowed" below): NEVER name,
suggest, or imply any platform, tool, marketplace, directory, search
engine, community, or channel OUTSIDE Flintel as a better place to look
— this includes but is not limited to Google, app/tool directories,
review sites, Slack, Discord, niche forums, or any other product. Doing
so tells the user to leave Flintel for something else, which this
product must never do, regardless of whether the observation is
factually true. If you genuinely believe the conversation is happening
somewhere Flintel doesn't cover, phrase "likely_reason" purely in terms
of why THIS search (these keywords, this time window, these platforms)
came up short — e.g. "the exact solution name isn't something people
usually type in complaint-style posts" or "this is a newer/niche term
that hasn't built up much public discussion yet" — and let
"suggested_actions" (broaden_time / broaden_platforms / broaden_term,
all of which are things FLINTEL ITSELF can do) be the only next steps
offered. Never write anything that reads as "go search somewhere else
instead."

TONE FOR "no_results" (write like a sharp analyst reporting back, not a
form rejection):
- Open by stating plainly WHAT was searched and WHERE (platforms,
  keyword theme) — e.g. "I searched X, Y, Z for people discussing <theme>."
- Be honest that no strong/high-intent match was found, but frame it as
  information, not failure — e.g. "This doesn't mean there's no demand —
  it likely means the search was too narrow, or people describe this
  differently than expected."
- If ANY posts were matched at all (even loosely relevant, low-intent
  ones), do not discard them — describe what was found in plain terms,
  optionally grouped by how relevant/strong the signal is, so the user
  sees real signal instead of a blank "nothing found."
- Never sound like a rejection or a canned apology.
- Never point the user toward a different platform, tool, or channel as
  the place to actually find this — Flintel's own suggested_actions are
  the only next steps to offer.

──────────────────────────────────────────────────────────────────────────
FORMAT 5 — "not_available"
For capabilities Flintel doesn't support yet (e.g. job listings, anything
outside social-listening).
{
  "format": "not_available",
  "message": "<brief, honest explanation of what isn't available yet and what Flintel can do instead>"
}

CLARIFICATION — "not_available" vs "no_results":
"not_available" is ONLY for things Flintel structurally cannot do at all
(e.g. job listings, building a dossier on a named individual, anything
outside social-listening entirely). A request like "find me customers for
my website/product" IS a valid social-listening search — Flintel searches
for relevant conversations using keywords derived from the site/topic.
If that search runs but finds little or nothing, that is a "no_results"
outcome, NEVER "not_available" — do not decline a legitimate lead-gen or
customer-discovery ask just because it's framed as "finding customers";
treat it exactly like any other search that came up empty.

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
exception): every individual post object must include a "sentiment" field
set to exactly one of these four lowercase strings — "positive", "mixed",
"negative", "neutral" — and nothing else. Never omit this field on any
post. Never use a free-text or capitalized value. The frontend maps these
four exact values to fixed colored tags — any other value fails to render.
This field is still always required on every post, for every query — but
see "SENTIMENT IS A LABEL, NOT A SELECTION FILTER" above for what it must
NOT be used for: choosing which posts to include in the first place.

──────────────────────────────────────────────────────────────────────────
POST-COUNT LIMIT: Never include more than 7 posts total, combined across
every platform, in a single answer. If you were given both grounded posts
(real text) and discovery-only posts (Google search, title/subreddit +
google_rank only, no text yet), choose the best combination of up to 7
based on genuine relevance - do not force an even split between the two
kinds, and never pad with a low-quality post just to reach a count. A
discovery-only post's "summary" must say its content hasn't been fetched
yet (e.g. "Content not yet available — found via search, rank #<n>"),
never a fabricated summary.

──────────────────────────────────────────────────────────────────────────
SUGGESTION/FOLLOW-UP LENGTH RULE (applies to "followups" in source_list
and comparison, and to every "label" and "clarifying_question" inside
suggested_actions in no_results):
- Keep every suggestion SHORT — roughly 4-8 words, one simple sentence
  or phrase, never a long or compound sentence.
- Use plain, everyday words a person would actually type or say — no
  formal, wordy, or corporate phrasing.
- The MEANING/INTENT of each suggestion must stay exactly the same as it
  would have been otherwise — this rule only shortens and simplifies the
  WORDING, it never changes what the suggestion is asking or offering.
- Example — too long: "Would you like me to extend the search window to
  the last 30 days to see if there's more relevant conversation?" —
  instead write: "Extend to last 30 days?"
- Example — too long: "You could try searching for a more specific or
  narrower term related to your brand or product." — instead write:
  "Try a more specific term?"

RESPONSE FORMAT INTELLIGENCE:
- The format above is chosen by what the user is asking and what you
  found — not by rigid keyword triggers. A comparison request gets
  "comparison" even if worded unusually; a request for "the top 10 X"
  still uses "source_list" with "ranked": true, not a new shape.
- If you genuinely cannot find enough to support "source_list",
  "trend_report", or "comparison" honestly, use "no_results" instead of
  forcing a thin answer into one of those shapes.

TONE (applies to every text field you write inside any format above):
- Plain, direct, conversational — the way a sharp analyst explains
  findings to a colleague. No "As an AI..." framing, no restating the
  question back, no filler openers, no corporate hedging.
- Never claim more confidence than the grounding supports.
"""

CLAUDE_ANALYSIS_SYSTEM_PROMPT = (
    CLAUDE_ANALYSIS_SYSTEM_PROMPT
    .replace("more than 7 posts total", f"more than {MAX_CHAT_EVIDENCE_POSTS} posts total")
    .replace("up to 7\nbased on genuine relevance", f"up to {MAX_CHAT_EVIDENCE_POSTS}\nbased on genuine relevance")
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
    is sent to Claude. Skips a post entirely if it has neither a title nor
    any text to offer."""
    posts = []
    for m in matched_signals or []:
        title = (m.get("title") or "").strip()
        text  = (m.get("post_text") or "").strip()
        if not title and not text:
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
   "search" with "keywords": null (the downstream website-reading step
   will fill in real keywords from the site's own content). Only use
   "clarify" when there is NEITHER a URL NOR any named topic/brand/
   industry/problem angle anywhere in the message.

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
{"intent": "search", "reply": null, "keywords": ["<keyword1>", "<keyword2>"], "time_window_days": null, "match_phrases": ["<phrase1>", "<phrase2>"], "evidence_required": <int|null>}
{"intent": "chat", "reply": "<your natural reply text here>", "keywords": null, "time_window_days": null, "match_phrases": null, "evidence_required": null}
{"intent": "blocked", "reply": "<short, polite decline text>", "keywords": null, "time_window_days": null, "match_phrases": null, "evidence_required": null}
{"intent": "clarify", "reply": "<short, natural clarifying question>", "keywords": null, "time_window_days": null, "match_phrases": null, "evidence_required": null}
"""

CLAUDE_ROUTER_SYSTEM_PROMPT = (
    CLAUDE_ROUTER_SYSTEM_PROMPT
    + "\n" + flintel.ROUTER_UNFILTERED_ADDENDUM
    + "\n" + flintel.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM
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

    return {"intent": intent, "reply": reply, "keywords": keywords, "time_window_days": time_window_days,
            "unfiltered": unfiltered, "match_phrases": match_phrases, "evidence_required": evidence_required}


def classify_and_maybe_chat(query: str, chat_summary: str) -> dict:
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
    user_message = (
        f"Conversation so far (auto-summarized, may be empty):\n"
        f"{chat_summary or '(no earlier messages in this chat)'}\n\n"
        f"User's new message: {query}"
    )
    try:
        raw = _call_claude(CLAUDE_ROUTER_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_ROUTER_MAX_TOKENS, enable_web_search=True)
    except Exception as exc:
        log.warning(f"Router Claude call failed (defaulting to 'search'): {exc}")
        return {"intent": "search", "reply": None, "keywords": None, "time_window_days": None, "unfiltered": None, "match_phrases": None, "evidence_required": None}

    parsed = _parse_router_json(raw)
    if not parsed:
        log.warning(f"Router returned unparseable output (defaulting to 'search'): {raw[:200]!r}")
        return {"intent": "search", "reply": None, "keywords": None, "time_window_days": None, "unfiltered": None, "match_phrases": None, "evidence_required": None}
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

    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        log.warning(f"Website-keyword call returned unparseable output for url={url!r}: {raw[:200]!r}")
        return None
    if not isinstance(data, dict):
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


def _timeout_fallback_answer(chat_id: str, owner_key: str, topic_key: str, query: str, keywords: list = None,
                              match_phrases: list = None, evidence_required: int = None):
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
    behavior: get_matched_signals() below is called with `limit=None`, so
    MAX_MATCHED_RESULTS' own default applies) is the message's own
    stored, already-clamped evidence budget — passed straight through as
    `limit` to get_matched_signals(), and used to derive
    `effective_evidence_limit` (via
    `min(evidence_required or MIN_ANALYSIS_EVIDENCE, MAX_ANALYSIS_EVIDENCE)`,
    the same formula _complete_message_answer_and_results() uses) for the
    `max_total` passed to merge_matched_and_google_results().

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
        # earlier "empty" snapshot that triggered this call.
        try:
            matched = get_matched_signals(
                topic_key, keywords or [], targeting_platform="all",
                match_phrases=match_phrases, limit=evidence_required,
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
        if google_results:
            extra_ctx_parts.append(flintel.build_combined_source_context(len(matched), len(google_results)))
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
            append_to_chat_summary(chat_id, owner_key, query, final_answer)
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
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=False)
        except (ValueError, TypeError):
            pass

    brace_index = stripped.find("{")
    while brace_index != -1:
        try:
            parsed, _end_index = json.JSONDecoder().raw_decode(stripped, brace_index)
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        brace_index = stripped.find("{", brace_index + 1)

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
