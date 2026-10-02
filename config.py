"""
FLINTEL — CONFIGURATION
============================================================================
Single place for every constant that used to live directly inside
index.py's own config block. flintel.py, google.py, and website_
intelligence.py stay fully self-contained exactly as they already are — 
each owns its own small config block, per their own module docstrings — 
so none of THEIR constants live here; this file is index.py's config  
only. 

CONSTRAINT: import-safe with zero side effects other than reading env 
vars — no Mongo connection, no HTTP call, nothing that can fail or block
at import time. Exactly the same spirit as the top of index.py's own
former config block.

(MODEL) LLM_MODEL / LLM_REASONING_EFFORT / LLM_REASONING_HEADROOM /
LLM_VERBOSITY / OPENAI_RESPONSES_URL configure the LLM this product
calls out to (GPT-5 mini, via OpenAI Responses API — see logics.py for
the actual call). ANTHROPIC_API_KEY / CLAUDE_MODEL / CLAUDE_API_URL /
CLAUDE_API_VERSION are kept as deprecated/unused constants for
backward-compatibility with existing .env files.

(TIMEOUT-SIMPLIFICATION CHANGE) No "loose match" / "near match
confidence" / threshold constant lives here — that entire mechanism is
being removed outright, not reconfigured, so no config value is needed
for it.

(NAMESPACE-HYGIENE FIX) index.py does `from config import *`. Without an
explicit `__all__`, Python's wildcard import exports every top-level name
that doesn't start with an underscore — including the `os` module itself,
imported below purely so this file can read env vars. That leaked `os`
name was harmless in practice (index.py never referenced a bare `os.`
anywhere), but it was still an accident waiting to happen: any future
top-level import added here (e.g. `import re`) would silently leak into
index.py's namespace too. `__all__` below makes the exported surface
explicit and limits it to the actual config constants — nothing else
changes; every existing name index.py already relies on is still here,
unchanged.

(EMBEDDING CONFIG) EMBEDDING_PROVIDER / EMBEDDING_MODEL / OPENAI_API_KEY /
EMBEDDING_TIMEOUT / EMBEDDING_MAX_CHARS mirror the embedding config already
used by the background-service files (index.py and both flintel.py's own
embedding config block) name-for-name and default-for-default, so both
sides of the system embed with the same model/settings. SIGNAL_EMBEDDING_
CANDIDATE_POOL, SIGNAL_EMBEDDING_RECENCY_POOL, SIGNAL_EMBEDDING_SIMILARITY_
THRESHOLD, and SIGNAL_EMBEDDING_MAX_SCAN are web-service-only constants for
the retrieval/scoring step.

CANDIDATE_POOL and RECENCY_POOL now default to 0 (unlimited mode):
  0 or negative  → unlimited: skip lexical Tier 1, open cursor with no
                   .limit(), score in batched numpy (2000 docs/batch).
  positive N     → legacy hybrid mode (Tier 1 lexical up to N docs +
                   Tier 2 recency up to RECENCY_POOL). Rollback: set
                   both to 500 in .env.
SIGNAL_EMBEDDING_MAX_SCAN (default 0 = no cap): optional hard upper
limit on docs scanned in unlimited mode; a warning is logged if hit.

(MONGODB4 NOTE) MONGODB4 is a second signals-only read mirror, same
role as MONGODB2 (flintel_signals only). MONGODB3 stays the tertiary
connection for every OTHER collection — jobs, users, chats, etc. —
untouched by this addition.
"""

import os

__all__ = [
    # Intent Bridge config (prototype ↔ production)
    "INTENT_BRIDGE_ENABLED",
    "INTENT_CANDIDATE_MULTIPLIER",
    "INTENT_CANDIDATE_MIN",
    "INTENT_CANDIDATE_MAX",
    "INTENT_SHORTCIRCUIT_HEAD",
    "INTENT_SHORTCIRCUIT_MIN_PASSING",
    "INTENT_SHORTCIRCUIT_MIN_CONFIDENCE",
    "INTENT_CLASSIFY_PARALLEL_BATCHES",
    "INTENT_BRIDGE_TIMEOUT_SECONDS",
    "INTENT_CACHE_ENABLED",
    "INTENT_CACHE_TTL_DAYS",
    "INTENT_CACHE_COLLECTION",
    # Keyword / matching limits
    "MAX_KEYWORDS",
    "CLAUDE_MAX_KEYWORDS",
    "MAX_MATCHED_RESULTS",
    "MAX_POSTS_PER_PLATFORM",
    "MAX_TIME_WINDOW_DAYS",
    # Evidence-count limits
    "MAX_ANALYSIS_EVIDENCE",
    "MIN_ANALYSIS_EVIDENCE",
    "MAX_CHAT_EVIDENCE_POSTS",
    # Topic evidence cache
    "TOPIC_CACHE_MAX_EVIDENCE",
    "TOPIC_CACHE_MIN_TOPUP",
    "TOPIC_CACHE_TTL_DAYS",
    # Auth / session
    "SESSION_SECRET_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    # Claude analysis layer config (v4)
    "CLAUDE_MAX_TOKENS",
    "CLAUDE_MAP_MAX_TOKENS",
    "CLAUDE_POSTS_PER_CHUNK",
    "CLAUDE_TIMEOUT_SECONDS",
    "CLAUDE_NOTES_PER_CHUNK",
    # Router + chat-summary config (v5)
    "CLAUDE_ROUTER_MAX_TOKENS",
    "CHAT_SUMMARY_MAX_TURNS",
    "CHAT_SUMMARY_TURN_CHAR_LIMIT",
    # Response-timeout config (v6)
    "RESPONSE_TIMEOUT",
    # Busy-lock config
    "BUSY_FLAG_TIMEOUT_SECONDS",
    # Simulated-stream config
    "STREAM_CHUNK_CHARS",
    "STREAM_CHUNK_DELAY_SECONDS",
    # Clarify-self-resolve config
    "CLAUDE_TOPIC_RESOLVER_MAX_TOKENS",
    # Website-URL keyword extraction config
    "MAX_WEBSITE_KEYWORDS",
    "WEBSITE_FETCH_TIMEOUT_SECONDS",
    "WEBSITE_FETCH_MAX_CHARS",
    "CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS",
    # Website Intelligence — multi-page discovery + evidence caching
    "MAX_WEBSITE_PAGES",
    "WEBSITE_DISCOVERY_PATH_HINTS",
    "WEBSITE_EVIDENCE_CACHE_TTL_DAYS",
    "WEBSITE_EVIDENCE_MAX_TOKENS",
    "WEBSITE_INSIGHT_MAX_TOKENS",
    # URL + prompt merge (BEHAVIOR 2)
    "URL_PROMPT_MERGE_ENABLED",
    "URL_PROMPT_MAX_KEYWORDS",
    "URL_PROMPT_MAX_PHRASES",
    "URL_MERGED_MAX_PHRASES",
    # Model config (ANTHROPIC — deprecated/unused; kept for .env compatibility)
    "ANTHROPIC_API_KEY",
    "CLAUDE_MODEL",
    "CLAUDE_API_URL",
    "CLAUDE_API_VERSION",
    # Model config (OpenAI Responses API — active LLM backend)
    "LLM_MODEL",
    "LLM_REASONING_EFFORT",
    "LLM_REASONING_HEADROOM",
    "LLM_VERBOSITY",
    "OPENAI_RESPONSES_URL",
    # Embedding config (mirrors background-service embedding config)
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
    "OPENAI_API_KEY",
    "EMBEDDING_TIMEOUT",
    "EMBEDDING_MAX_CHARS",
    # Signal-matching embedding config (web-service only)
    "ROUTER_MAX_MATCH_PHRASES",
    "SIGNAL_EMBEDDING_CANDIDATE_POOL",
    "SIGNAL_EMBEDDING_RECENCY_POOL",
    "SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD",
    "SIGNAL_EMBEDDING_MAX_SCAN",
    # Secondary MongoDB (signals mirror)
    "MONGODB2",
    # Tertiary MongoDB (everything except flintel_signals)
    "MONGODB3",
    # Quaternary MongoDB (signals mirror)
    "MONGODB4",
    # Lazy embedding backfill config
    "LAZY_EMBED_ENABLED",
    "LAZY_EMBED_MAX_DOCS_PER_QUERY",
    "LAZY_EMBED_BATCH_SIZE",
    "LAZY_EMBED_MIN_TEXT_CHARS",
]

# ── Keyword / matching limits ────────────────────────────────────────────
MAX_KEYWORDS = int(os.getenv("MAX_KEYWORDS", "20"))
CLAUDE_MAX_KEYWORDS = int(os.getenv("CLAUDE_MAX_KEYWORDS", "10"))
MAX_MATCHED_RESULTS = int(os.getenv("MAX_MATCHED_RESULTS", "25"))
MAX_POSTS_PER_PLATFORM = int(os.getenv("MAX_POSTS_PER_PLATFORM", "3"))
MAX_TIME_WINDOW_DAYS = int(os.getenv("MAX_TIME_WINDOW_DAYS", "3650"))

# ── Evidence-count limits (EVIDENCE-BUDGET FEATURE — used by logics.py's
#    router/analysis/timeout-fallback layer to decide how many matched+
#    Google posts get retrieved, merged, and handed to the LLM for one
#    answer) ─────────────────────────────────────────────────────────────
# MAX_ANALYSIS_EVIDENCE: absolute safety ceiling on how much evidence the
# router's evidence planner is ever allowed to request for analysis.
# Mirrors flintel.py's own MAX_ANALYSIS_EVIDENCE (kept as a separate,
# independently-configurable constant, same convention as every other
# mirrored constant in this file, e.g. MAX_TIME_WINDOW_DAYS).
MAX_ANALYSIS_EVIDENCE = int(os.getenv("MAX_ANALYSIS_EVIDENCE", "100"))
# MIN_ANALYSIS_EVIDENCE: floor — never retrieve fewer than this many, even
# for the simplest query, so a tiny/malformed planner value can't starve
# the analysis of evidence.
MIN_ANALYSIS_EVIDENCE = int(os.getenv("MIN_ANALYSIS_EVIDENCE", "15"))
# MAX_CHAT_EVIDENCE_POSTS: fixed cap on how many evidence posts are ever
# shown in the final chat response — completely separate from the
# analysis evidence budget above. Referenced by CLAUDE_ANALYSIS_SYSTEM_
# PROMPT's own "post-count limit" instruction instead of a bare literal.
MAX_CHAT_EVIDENCE_POSTS = int(os.getenv("MAX_CHAT_EVIDENCE_POSTS", "7"))

# ── Topic Evidence Cache (per-chat, per-topic post reuse) ────────────────
# When the same topic (chat_id + topic_key) is asked about repeatedly, the
# system reuses previously-cached posts instead of re-querying Mongo and
# re-sending the same posts to Claude — new posts are only fetched when
# evidence_required exceeds what's already cached.

# Absolute ceiling on how many posts get stored per topic in the cache
# collection. Mirrors MAX_ANALYSIS_EVIDENCE but stays a separate,
# independently-configurable constant, same convention as every other
# mirrored constant in this file, so cache and live-fetch limits never
# accidentally collide with each other.
TOPIC_CACHE_MAX_EVIDENCE = int(os.getenv("TOPIC_CACHE_MAX_EVIDENCE", "100"))

# When the user asks for more depth and the new evidence_required exceeds
# the already-cached count, the top-up fetch tries to bring in at least
# this many new posts (the evidence_required - cached_count delta gets
# clamped to this floor) — so a top-up is never a wasteful 1-2 post fetch
# unless that's genuinely all that's needed.
TOPIC_CACHE_MIN_TOPUP = int(os.getenv("TOPIC_CACHE_MIN_TOPUP", "5"))

# How many days a cache entry stays valid — lets old, stale topic-evidence
# caches automatically expire/get ignored when a very old chat is reopened
# (the posts landscape will have changed by then). 0 or negative means
# "never expire" (testing/debug only).
TOPIC_CACHE_TTL_DAYS = int(os.getenv("TOPIC_CACHE_TTL_DAYS", "7"))

# ── Auth / session ────────────────────────────────────────────────────────
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "dev-only-change-me")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# ── Claude analysis layer config (v4) ────────────────────────────────────
CLAUDE_MAX_TOKENS       = int(os.getenv("CLAUDE_MAX_TOKENS", "2500"))
CLAUDE_MAP_MAX_TOKENS   = int(os.getenv("CLAUDE_MAP_MAX_TOKENS", "512"))
CLAUDE_POSTS_PER_CHUNK  = int(os.getenv("CLAUDE_POSTS_PER_CHUNK", "12"))
CLAUDE_TIMEOUT_SECONDS  = float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "60"))
CLAUDE_NOTES_PER_CHUNK  = int(os.getenv("CLAUDE_NOTES_PER_CHUNK", "8"))

# ── Router + chat-summary config (v5) ────────────────────────────────────
CLAUDE_ROUTER_MAX_TOKENS       = int(os.getenv("CLAUDE_ROUTER_MAX_TOKENS", "800"))
CHAT_SUMMARY_MAX_TURNS         = int(os.getenv("CHAT_SUMMARY_MAX_TURNS", "8"))
CHAT_SUMMARY_TURN_CHAR_LIMIT   = int(os.getenv("CHAT_SUMMARY_TURN_CHAR_LIMIT", "400"))

# ── Response-timeout config (v6) ──────────────────────────────────────────
RESPONSE_TIMEOUT = int(os.getenv("RESPONSE_TIMEOUT", "180"))

# ── Busy-lock config ──────────────────────────────────────────────────────
BUSY_FLAG_TIMEOUT_SECONDS = int(os.getenv("BUSY_FLAG_TIMEOUT_SECONDS", "90"))

# ── Simulated-stream config ──────────────────────────────────────────────
STREAM_CHUNK_CHARS         = int(os.getenv("STREAM_CHUNK_CHARS", "3"))
STREAM_CHUNK_DELAY_SECONDS = float(os.getenv("STREAM_CHUNK_DELAY_SECONDS", "0.02"))

# ── Clarify-self-resolve config ──────────────────────────────────────────
CLAUDE_TOPIC_RESOLVER_MAX_TOKENS = int(os.getenv("CLAUDE_TOPIC_RESOLVER_MAX_TOKENS", "400"))

# ── Website-URL keyword extraction config ────────────────────────────────
MAX_WEBSITE_KEYWORDS               = int(os.getenv("MAX_WEBSITE_KEYWORDS", "20"))
WEBSITE_FETCH_TIMEOUT_SECONDS      = float(os.getenv("WEBSITE_FETCH_TIMEOUT_SECONDS", "15"))
WEBSITE_FETCH_MAX_CHARS            = int(os.getenv("WEBSITE_FETCH_MAX_CHARS", "8000"))
CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS  = int(os.getenv("CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS", "1200"))

# ── Website Intelligence — multi-page discovery + evidence caching ──────
# Kitne pages tak (homepage samet) discover/fetch karna hai — controlled,
# same-domain, koi uncontrolled crawling nahi.
MAX_WEBSITE_PAGES = int(os.getenv("MAX_WEBSITE_PAGES", "5"))

# Homepage ke internal links mein se kaunse paths follow karne layak hain
# — sirf yeh path-hints jin links mein match karein unhi ko follow karo.
WEBSITE_DISCOVERY_PATH_HINTS = [
    "about", "products", "product", "services", "service",
    "pricing", "price", "faq", "faqs", "contact", "features", "feature",
]

# Website evidence cache kitne din tak valid rahega — TTL index isi se
# banega (topic_evidence_cache ke TOPIC_CACHE_TTL_DAYS jaisa pattern).
# Website content occasionally badal sakta hai, is liye topic-evidence
# cache se chhota TTL rakha gaya hai.
WEBSITE_EVIDENCE_CACHE_TTL_DAYS = int(os.getenv("WEBSITE_EVIDENCE_CACHE_TTL_DAYS", "3"))

# Structured business-evidence + website-insight-answer Claude call ke
# liye max_tokens — CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS se thoda zyada,
# kyunke yeh call structured schema + insight-answer text dono produce
# karta hai.
WEBSITE_EVIDENCE_MAX_TOKENS = int(os.getenv("WEBSITE_EVIDENCE_MAX_TOKENS", "1200"))
WEBSITE_INSIGHT_MAX_TOKENS = int(os.getenv("WEBSITE_INSIGHT_MAX_TOKENS", "1200"))

# ── URL + prompt merge (BEHAVIOR 2) ──────────────────────────────────────
# Jab user ek hi message mein URL aur ask (jaise "sales la do") dono de,
# to website-derived keywords ke sath prompt ke apne keywords/phrases bhi
# merge hote hain.
URL_PROMPT_MERGE_ENABLED = os.getenv("URL_PROMPT_MERGE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
# Prompt ke maximum kitne keywords merged list mein pehle rakhe jayen.
URL_PROMPT_MAX_KEYWORDS = int(os.getenv("URL_PROMPT_MAX_KEYWORDS", "10"))
# Prompt ke maximum kitne match_phrases merged list mein pehle rakhe jayen.
URL_PROMPT_MAX_PHRASES = int(os.getenv("URL_PROMPT_MAX_PHRASES", "7"))
# Merge ke baad match_phrases ki total limit (website 7 + prompt 3 = 10).
URL_MERGED_MAX_PHRASES = int(os.getenv("URL_MERGED_MAX_PHRASES", "15"))

# ── Model config (ANTHROPIC — DEPRECATED / UNUSED) ───────────────────────
# These constants are kept so existing .env files with ANTHROPIC_API_KEY or
# CLAUDE_MODEL set do not cause import errors. They are NOT used by any
# active code path — the LLM backend is now OpenAI (see block below).
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL        = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_API_URL      = "https://api.anthropic.com/v1/messages"
CLAUDE_API_VERSION  = os.getenv("CLAUDE_API_VERSION", "2023-06-01")

# ── Embedding config (mirrors background-service embedding config —
#    index.py and both flintel.py's own embedding config block — name-for-
#    name and default-for-default, so both sides of the system embed with
#    the same model/settings) ──────────────────────────────────────────────
EMBEDDING_PROVIDER  = os.getenv("EMBEDDING_PROVIDER", "openai")
EMBEDDING_MODEL     = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_TIMEOUT   = int(os.getenv("EMBEDDING_TIMEOUT", "20"))
EMBEDDING_MAX_CHARS = int(os.getenv("EMBEDDING_MAX_CHARS", "8000"))

# ── Signal-matching embedding config (web-service only — the background
#    service has no equivalent matching step, so no mirrored constant is
#    needed here) ───────────────────────────────────────────────────────
# Mongo se time-window/platform filter ke baad kitne candidate docs fetch
# karke unka embedding compare karna hai — cosine similarity calculation
# ye size tak hi chalegi, is se bara pool kabhi nahi banega (cost/latency
# safety ceiling, MAX_MATCHED_RESULTS jaisa hi concept).
# (SEMANTIC QUERY REPRESENTATION FIX) How many match_phrases the router
# is allowed to contribute. match_phrases are the primary SEMANTIC search
# representation — each one is embedded and compared by MEANING against
# candidate posts — so this has to be large enough to cover every intent
# angle a request genuinely spans (buyer voice AND seller voice AND
# problem-signal voice, etc.). This was previously a bare literal 7
# inside logics.py's _parse_router_json(), which silently truncated whole
# intent angles off the end of the list.
ROUTER_MAX_MATCH_PHRASES = int(os.getenv("ROUTER_MAX_MATCH_PHRASES", "12"))

# (UNLIMITED CANDIDATE POOL) Default 0 = unlimited mode.
#   0 or negative  → unlimited: lexical Tier 1 is skipped, ALL docs in
#                     the time window with a valid embedding are scored via
#                     batched numpy (no .limit() on the cursor).
#   positive N     → legacy hybrid: Tier 1 lexical capped at N docs.
# Rollback: set SIGNAL_EMBEDDING_CANDIDATE_POOL=500 in .env.
SIGNAL_EMBEDDING_CANDIDATE_POOL = int(os.getenv("SIGNAL_EMBEDDING_CANDIDATE_POOL", "0"))

# (UNLIMITED CANDIDATE POOL) Default 0 = unlimited mode.
#   0 or negative  → unlimited: this constant is ignored; the single
#                     unlimited cursor already covers recency.
#   positive N     → legacy hybrid: Tier 2 recency pool capped at N docs.
# Rollback: set SIGNAL_EMBEDDING_RECENCY_POOL=500 in .env.
SIGNAL_EMBEDDING_RECENCY_POOL = int(os.getenv("SIGNAL_EMBEDDING_RECENCY_POOL", "0"))

# Minimum cosine similarity score jispar ek candidate document "match"
# count hota hai. Is se neeche wale docs discard honge, chahe wo pool
# mein aaye hi kyun na hon.
SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD = float(os.getenv("SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD", "0.35"))

# (UNLIMITED POOL SAFETY CAP) Hard upper limit on docs scanned in
# unlimited mode. 0 or negative = no cap. Positive N = stop after N docs
# and log a warning. Ignored in legacy mode (CANDIDATE_POOL > 0).
SIGNAL_EMBEDDING_MAX_SCAN = int(os.getenv("SIGNAL_EMBEDDING_MAX_SCAN", "0"))

# ── Secondary signals-only MongoDB (READ-ONLY mirror of flintel_signals) ──
MONGODB2 = os.getenv("MONGODB2", "")

# ── Tertiary MongoDB (primary home for every collection EXCEPT
#    flintel_signals — jobs, users, chats, busy-owners, google_posts,
#    topic_evidence_cache, website_evidence_cache) ─────────────────
MONGODB3 = os.getenv("MONGODB3", "")

# ── Quaternary signals-only MongoDB (READ-ONLY mirror of flintel_signals) ──
# flintel_bot database ka flintel_signals read karta hai, bilkul MONGODB2 ki tarah.
MONGODB4 = os.getenv("MONGODB4", "")

# ── LLM config (OpenAI Responses API — active backend) ───────────────────
# Primary model for all LLM calls (_call_claude / _call_claude_stream in
# logics.py, claude() in intent_prototype/llm.py). Use a separate env var
# (LLM_MODEL) rather than reusing CLAUDE_MODEL — deployments may still have
# CLAUDE_MODEL=claude-haiku-... set, and sending that to OpenAI would 404.
_LLM_REASONING_EFFORT_ALLOWED = {"minimal", "low", "medium", "high"}
_llm_reasoning_effort_raw = os.getenv("LLM_REASONING_EFFORT", "low")
if _llm_reasoning_effort_raw not in _LLM_REASONING_EFFORT_ALLOWED:
    import warnings as _warnings
    _warnings.warn(
        f"LLM_REASONING_EFFORT={_llm_reasoning_effort_raw!r} is not one of "
        f"{sorted(_LLM_REASONING_EFFORT_ALLOWED)}; falling back to 'low'.",
        RuntimeWarning,
        stacklevel=2,
    )
    _llm_reasoning_effort_raw = "low"

LLM_MODEL              = os.getenv("LLM_MODEL", "gpt-5-mini")
LLM_REASONING_EFFORT   = _llm_reasoning_effort_raw
LLM_REASONING_HEADROOM = int(os.getenv("LLM_REASONING_HEADROOM", "2048"))
LLM_VERBOSITY          = os.getenv("LLM_VERBOSITY", "")
OPENAI_RESPONSES_URL   = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses")

# ── Lazy embedding backfill config ────────────────────────────────────────
# When enabled, get_matched_signals() silently embeds any candidate docs
# that are missing their embedding vector, saves the vector to the primary
# collection, and includes those docs in the scoring pass — so they're not
# silently skipped.
LAZY_EMBED_ENABLED            = os.getenv("LAZY_EMBED_ENABLED", "1") in ("1", "true", "yes")
LAZY_EMBED_MAX_DOCS_PER_QUERY = int(os.getenv("LAZY_EMBED_MAX_DOCS_PER_QUERY", "100"))
LAZY_EMBED_BATCH_SIZE         = int(os.getenv("LAZY_EMBED_BATCH_SIZE", "50"))
LAZY_EMBED_MIN_TEXT_CHARS     = int(os.getenv("LAZY_EMBED_MIN_TEXT_CHARS", "20"))

# ── Intent Bridge config (intent_prototype ↔ production pipeline) ────────
# Master switch — False = production behaviour completely unchanged.
# Flag on = prototype intent-classification pipeline runs as an overlay on
# top of the existing keyword-matched candidates. Any failure inside the
# bridge falls back to original candidates — never crashes, never returns
# fewer posts than production would.
INTENT_BRIDGE_ENABLED           = os.getenv("INTENT_BRIDGE_ENABLED", "").lower() in ("1", "true", "yes")

# Candidate pool sizing: evidence_required * multiplier, clamped to [MIN, MAX].
# The bridge fetches a larger-than-needed pool so intent classification has
# enough material to select the best evidence_required posts from.
INTENT_CANDIDATE_MULTIPLIER     = int(os.getenv("INTENT_CANDIDATE_MULTIPLIER", "4"))
INTENT_CANDIDATE_MIN            = int(os.getenv("INTENT_CANDIDATE_MIN", "100"))
INTENT_CANDIDATE_MAX            = int(os.getenv("INTENT_CANDIDATE_MAX", "200"))

# Short-circuit: classify the top HEAD candidates first; if at least
# MIN_PASSING of them pass at confidence >= MIN_CONFIDENCE, skip classifying
# the rest (saves LLM calls when the top results are already high-quality).
INTENT_SHORTCIRCUIT_HEAD        = int(os.getenv("INTENT_SHORTCIRCUIT_HEAD", "50"))
INTENT_SHORTCIRCUIT_MIN_PASSING = int(os.getenv("INTENT_SHORTCIRCUIT_MIN_PASSING", "15")) 
INTENT_SHORTCIRCUIT_MIN_CONFIDENCE = float(os.getenv("INTENT_SHORTCIRCUIT_MIN_CONFIDENCE", "0.70"))

# Number of parallel batches for concurrent classification (ThreadPoolExecutor).
INTENT_CLASSIFY_PARALLEL_BATCHES = int(os.getenv("INTENT_CLASSIFY_PARALLEL_BATCHES", "3"))

# Total bridge timeout in seconds — if exceeded, return original candidates.
INTENT_BRIDGE_TIMEOUT_SECONDS   = int(os.getenv("INTENT_BRIDGE_TIMEOUT_SECONDS", "25"))

# Per-post classification cache (stored in MongoDB intent_classification_cache
# collection). Avoids re-classifying the same post on repeated queries.
INTENT_CACHE_ENABLED            = os.getenv("INTENT_CACHE_ENABLED", "true").lower() in ("1", "true", "yes")
INTENT_CACHE_TTL_DAYS           = int(os.getenv("INTENT_CACHE_TTL_DAYS", "30"))
INTENT_CACHE_COLLECTION         = os.getenv("INTENT_CACHE_COLLECTION", "intent_classification_cache")
