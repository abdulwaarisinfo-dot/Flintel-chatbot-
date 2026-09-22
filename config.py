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

(MODEL) ANTHROPIC_API_KEY / CLAUDE_MODEL / CLAUDE_API_URL /
CLAUDE_API_VERSION configure the LLM this product calls out to (Claude
Haiku, via Anthropic's Messages API — see logics.py for the actual
call).

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
"""

import os

__all__ = [
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
    # Model config (ANTHROPIC — Claude Haiku)
    "ANTHROPIC_API_KEY",
    "CLAUDE_MODEL",
    "CLAUDE_API_URL",
    "CLAUDE_API_VERSION",
    # Secondary MongoDB (signals mirror)
    "MONGODB2",
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

# ── Model config (ANTHROPIC — Claude Haiku) ──────────────────────────────
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL        = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_API_URL      = "https://api.anthropic.com/v1/messages"
CLAUDE_API_VERSION  = os.getenv("CLAUDE_API_VERSION", "2023-06-01")

# ── Secondary signals-only MongoDB (READ-ONLY mirror of flintel_signals) ──
MONGODB2 = os.getenv("MONGODB2", "")
