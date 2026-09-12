"""
FLINTEL — GOOGLE SEARCH (RAPIDAPI) REDDIT-POST DISCOVERY
============================================================================
Self-contained module, following the EXACT same one-way-dependency
pattern as website_intelligence.py and flintel.py: nothing here imports
from or modifies index.py, flintel.py, or website_intelligence.py — those
files may import FROM this file, never the other way around. This file
owns no Mongo connection of its own — every function that needs to read
or write signals takes a `google_posts_collection` parameter, exactly
like flintel.py's get_unfiltered_matched_signals() accepts a
signals_collection rather than connecting to Mongo itself. No FastAPI
routes live here either.

WHAT THIS FILE IS FOR: a user's search keywords sometimes turn up little
or nothing in Flintel's own already-collected flintel_signals. This file
gives index.py a way to ALSO ask Google (via a RapidAPI Google-search
proxy, since a raw Google API key/quota isn't part of this project) for
Reddit posts matching those same keywords, and store lightweight "stub"
documents for whatever Reddit URLs Google surfaces — real content for
those URLs is fetched later, out of band, by Background Service #2 (not
part of this file), which is why a stub only ever records post_url,
discovered_at, and search metadata, and starts with reddit_fetched=False.

DEGRADE GRACEFULLY, EVERYWHERE: every function in this file returns None
/ [] on any failure — a missing/invalid RAPIDAPI_KEY, a timeout, a
non-200 response, unparseable JSON, an unexpected response shape, or a
Mongo write failure. Nothing in this file ever raises past its own
boundary — this can only ever ADD stub links for the caller to show,
never break or change any existing behavior if the Google/RapidAPI side
is down, slow, or misconfigured.

RESPONSE-SHAPE CAVEAT: _extract_reddit_results() parses the RapidAPI
"google-search116" host's response defensively (tries a small set of
plausible key names for the result list and for each item's link/rank
fields) since this was written without a live sample response to
confirm against — see that function's own docstring for exactly which
shapes it tries. If the real provider response uses different field
names than these, adjust the small set of candidate keys there; nothing
elsewhere in this file needs to change.
"""

import os
import re
from datetime import datetime, timezone

import httpx


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — small, self-contained constants with sensible defaults, mirroring
# (never importing) the spirit of index.py's/flintel.py's own equivalents.
# ─────────────────────────────────────────────────────────────────────────────

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_GOOGLE_HOST = os.getenv("RAPIDAPI_GOOGLE_HOST", "google-search116.p.rapidapi.com")

# How many Reddit stub results to ask the API for at most, per keyword
# search — kept modest since these are just lightweight discovery stubs,
# not the final content shown to the user.
GOOGLE_SEARCH_MAX_RESULTS_PER_KEYWORD = int(os.getenv("GOOGLE_SEARCH_MAX_RESULTS_PER_KEYWORD", "10"))

# Network timeout for the RapidAPI call itself.
GOOGLE_SEARCH_TIMEOUT_SECONDS = int(os.getenv("GOOGLE_SEARCH_TIMEOUT_SECONDS", "15"))

# (2-KEYWORD BATCHING) How many keywords are folded into ONE RapidAPI
# query. Lowered from the old "combine-up-to-5-into-one-query" behavior
# to 2, based on real-world background-service experience: bundling many
# keywords into a single query returns FEWER/weaker results than running
# several tighter, smaller-keyword-count queries and combining their
# results. ALL of the caller's keywords are now covered (not just the
# first N and silently dropping the rest) — see
# search_google_for_reddit_posts() below, which now loops over
# consecutive batches of this size instead of using only the first
# GOOGLE_QUERY_MAX_KEYWORDS keywords.
GOOGLE_QUERY_KEYWORDS_PER_BATCH = int(os.getenv("GOOGLE_QUERY_KEYWORDS_PER_BATCH", "2"))

# (DEPTH PARAMETER) Sent on every RapidAPI call as the "depth" query
# param, asking the provider to search deeper into Google's own result
# set. Purely a request-side parameter — does NOT change
# GOOGLE_SEARCH_MAX_RESULTS_PER_KEYWORD below, which remains the
# separate, LOCAL, post-fetch cap on how many results any single call's
# response is allowed to contribute (still 10 by default, completely
# unchanged).
GOOGLE_SEARCH_DEPTH = int(os.getenv("GOOGLE_SEARCH_DEPTH", "200"))

# Matches a normal Reddit post/subreddit URL path to pull out the
# subreddit name — e.g. "https://www.reddit.com/r/webdev/comments/..."
# -> "webdev". Returns no match (None) for a URL shape that doesn't
# follow this pattern (a user profile link, a reddit.com root URL, etc.)
# rather than guessing.
_SUBREDDIT_RE = re.compile(r"reddit\.com/r/([^/]+)/", re.IGNORECASE)


def _call_rapidapi_google_search(query: str):
    """Calls the RapidAPI "google-search116" host with `query` and
    returns the raw, unparsed JSON response — or None on any failure
    (missing/invalid RAPIDAPI_KEY, timeout, non-200 status, a response
    body that isn't valid JSON). Deliberately does NOT interpret the
    response shape here — that's _extract_reddit_results()'s job, kept
    separate so this function stays swappable if the RapidAPI provider
    ever changes without needing to touch the parsing logic too.

    Never raises: every failure path returns None instead."""
    if not query or not isinstance(query, str):
        return None
    if not RAPIDAPI_KEY:
        return None

    url = f"https://{RAPIDAPI_GOOGLE_HOST}/"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_GOOGLE_HOST,
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=GOOGLE_SEARCH_TIMEOUT_SECONDS) as client:
            response = client.get(
                url, headers=headers,
                params={"query": query, "depth": GOOGLE_SEARCH_DEPTH},
            )
            response.raise_for_status()
            return response.json()
    except Exception:
        return None


def _extract_reddit_results(raw_response) -> list:
    """Best-effort parse of whatever _call_rapidapi_google_search()
    returned. Tries a small set of plausible shapes for the
    "google-search116" RapidAPI response, since this was written without
    a live sample to confirm the exact field names against:
      - the result list itself under one of: "results",
        "organic_results", "organic", "items", "data"
      - each item's URL under one of: "link", "url"
      - each item's rank/position under one of: "position", "rank",
        "google_rank" — falls back to the item's 1-based index in the
        list if none of those are present.

    Only keeps items whose URL host is reddit.com / www.reddit.com.
    Skips any item with no usable URL at all. Returns [] (never raises)
    if raw_response is falsy, isn't a dict, or has none of the list
    keys tried above.

    Returns a list of dicts: [{"post_url", "google_rank", "subreddit"}, ...]
    in the same relative order the API returned them."""
    if not raw_response or not isinstance(raw_response, dict):
        return []

    result_list = None
    for key in ("results", "organic_results", "organic", "items", "data"):
        candidate = raw_response.get(key)
        if isinstance(candidate, list):
            result_list = candidate
            break
    if result_list is None:
        return []

    extracted = []
    for i, item in enumerate(result_list):
        if not isinstance(item, dict):
            continue

        post_url = None
        for url_key in ("link", "url"):
            val = item.get(url_key)
            if isinstance(val, str) and val.strip():
                post_url = val.strip()
                break
        if not post_url:
            continue

        host_match = re.search(r"://(?:www\.)?([^/]+)", post_url, re.IGNORECASE)
        host = host_match.group(1).lower() if host_match else ""
        if host not in ("reddit.com", "www.reddit.com"):
            continue

        google_rank = None
        for rank_key in ("position", "rank", "google_rank"):
            val = item.get(rank_key)
            if isinstance(val, int):
                google_rank = val
                break
        if google_rank is None:
            google_rank = i + 1

        subreddit_match = _SUBREDDIT_RE.search(post_url)
        subreddit = subreddit_match.group(1) if subreddit_match else None

        extracted.append({
            "post_url": post_url,
            "google_rank": google_rank,
            "subreddit": subreddit,
        })

    return extracted


def search_google_for_reddit_posts(keywords: list, google_posts_collection, search_keyword_for_storage: str = None) -> list:
    """Public entry point index.py calls. Splits ALL of the caller's
    non-empty string keywords (not just a fixed first-N subset) into
    consecutive batches of GOOGLE_QUERY_KEYWORDS_PER_BATCH keywords each
    (default 2), and makes ONE separate RapidAPI call per batch (each a
    Reddit-site-restricted search, e.g.
    "site:reddit.com keyword1 keyword2") — see GOOGLE_QUERY_KEYWORDS_PER_BATCH's
    own comment for why several smaller-keyword-count queries replaced
    the old single-combined-query approach. Keeps only the Reddit results
    from each batch, de-duplicates by post_url across all batches, and
    upserts a lightweight stub document per remaining result into
    `google_posts_collection` — real content for each URL is fetched
    later, out of band, by Background Service #2 (not part of this
    file), which is why a stub only ever records discovery metadata and
    starts with reddit_fetched=False.

    Upserts with $setOnInsert (never a plain $set) keyed by post_url, so
    a pre-existing stub — possibly already reddit_fetched=True from a
    prior background fetch — is never overwritten or reset back to
    False by a later, repeated search that happens to surface the same
    URL again. fuzzy_keywords still gets the caller's FULL original
    `keywords` list merged in via $addToSet on every result (not just
    that result's own 2-keyword batch), exactly as before this change.

    A batch whose own RapidAPI call fails, or whose response has no
    usable Reddit results, is simply skipped — the loop moves on to the
    next batch rather than aborting the whole function.

    Returns the list of stub dicts that now exist for this search
    (freshly inserted OR already-existing ones matching post_url), so
    the caller can show them immediately without a second query.

    NEVER raises: any failure at any step (bad/missing API key, network
    failure, unexpected response shape, a Mongo write failure) returns
    [] — this can only ever ADD stub links to what the user sees, never
    break or change the caller's existing behavior (showing a general-
    knowledge-only answer) if the Google/RapidAPI side is unavailable."""
    if not keywords or not isinstance(keywords, list):
        return []
    if google_posts_collection is None:
        return []

    try:
        clean_keywords = [k for k in keywords if isinstance(k, str) and k.strip()]
        if not clean_keywords:
            return []
        default_search_keyword = search_keyword_for_storage or clean_keywords[0]

        batches = [
            clean_keywords[i:i + GOOGLE_QUERY_KEYWORDS_PER_BATCH]
            for i in range(0, len(clean_keywords), GOOGLE_QUERY_KEYWORDS_PER_BATCH)
        ]

        now = datetime.now(timezone.utc)
        stubs = []
        seen_post_urls = set()

        for batch_keywords in batches:
            query = "site:reddit.com " + " ".join(batch_keywords)
            raw_response = _call_rapidapi_google_search(query)
            extracted = _extract_reddit_results(raw_response)
            if not extracted:
                continue

            extracted = extracted[:GOOGLE_SEARCH_MAX_RESULTS_PER_KEYWORD]

            for result in extracted:
                if result["post_url"] in seen_post_urls:
                    continue
                seen_post_urls.add(result["post_url"])

                doc = {
                    "post_url": result["post_url"],
                    "discovered_at": now,
                    "fetched_at": None,
                    "fuzzy_keywords": keywords,
                    "fuzzy_matched": True,
                    "google_rank": result["google_rank"],
                    "next_retry_at": None,
                    "reddit_fetched": False,
                    "search_keyword": default_search_keyword,
                    "subreddit": result["subreddit"],
                }
                try:
                    # (KEYWORD-MERGE FIX) Split off fuzzy_keywords so it can
                    # go through $addToSet/$each instead of $setOnInsert —
                    # $setOnInsert only ever applies on the FIRST insert, so
                    # a later search that rediscovers this same post_url
                    # under different keywords would otherwise have its
                    # keywords silently dropped. $addToSet/$each merges them
                    # into the existing array (deduplicated) on every call,
                    # insert or not, while every other field below still
                    # only ever gets set once, on first insert, exactly as
                    # before.
                    doc_without_fuzzy_keywords = {k: v for k, v in doc.items() if k != "fuzzy_keywords"}
                    google_posts_collection.update_one(
                        {"post_url": doc["post_url"]},
                        {
                            "$setOnInsert": doc_without_fuzzy_keywords,
                            "$addToSet": {"fuzzy_keywords": {"$each": doc["fuzzy_keywords"]}},
                        },
                        upsert=True,
                    )
                    # Read back whatever now actually exists for this
                    # post_url (freshly inserted, or a pre-existing stub
                    # that $setOnInsert correctly left untouched) so the
                    # caller always gets the real, current stored state —
                    # not just the doc this call attempted to insert.
                    stored = google_posts_collection.find_one({"post_url": doc["post_url"]}, {"_id": 0})
                    stubs.append(stored if stored else doc)
                except Exception:
                    continue

        return stubs
    except Exception:
        return []


def get_stub_results_for_keywords(google_posts_collection, keywords: list, limit: int = 10) -> list:
    """Reads back already-stored stubs from google_posts_collection
    whose fuzzy_keywords overlap with `keywords`, OR whose
    search_keyword matches one of `keywords`, sorted by discovered_at
    descending, capped at `limit`.

    Used at a later checkpoint (e.g. a 60-second mark) to pick up
    whatever stubs are already there — stored earlier by a prior call to
    search_google_for_reddit_posts() — without re-calling the Google API
    a second time for the same message.

    Returns [] on any failure (bad collection, query error) or if
    nothing matches — never raises."""
    if not keywords or not isinstance(keywords, list):
        return []
    if google_posts_collection is None:
        return []

    try:
        clean_keywords = [k for k in keywords if isinstance(k, str) and k.strip()]
        if not clean_keywords:
            return []

        query = {
            "$or": [
                {"fuzzy_keywords": {"$in": clean_keywords}},
                {"search_keyword": {"$in": clean_keywords}},
            ]
        }
        cursor = (
            google_posts_collection.find(query, {"_id": 0})
            .sort("discovered_at", -1)
            .limit(limit)
        )
        return list(cursor)
    except Exception:
        return []
