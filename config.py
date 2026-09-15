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
"""

import os

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

# ── Auth / session ────────────────────────────────────────────────────────
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "dev-only-change-me")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# ── Claude analysis layer config (v4) ────────────────────────────────────
CLAUDE_MAX_TOKENS       = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))
CLAUDE_MAP_MAX_TOKENS   = int(os.getenv("CLAUDE_MAP_MAX_TOKENS", "512"))
CLAUDE_POSTS_PER_CHUNK  = int(os.getenv("CLAUDE_POSTS_PER_CHUNK", "12"))
CLAUDE_TIMEOUT_SECONDS  = float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "30"))
CLAUDE_NOTES_PER_CHUNK  = int(os.getenv("CLAUDE_NOTES_PER_CHUNK", "8"))

# ── Router + chat-summary config (v5) ────────────────────────────────────
CLAUDE_ROUTER_MAX_TOKENS       = int(os.getenv("CLAUDE_ROUTER_MAX_TOKENS", "500"))
CHAT_SUMMARY_MAX_TURNS         = int(os.getenv("CHAT_SUMMARY_MAX_TURNS", "8"))
CHAT_SUMMARY_TURN_CHAR_LIMIT   = int(os.getenv("CHAT_SUMMARY_TURN_CHAR_LIMIT", "160"))

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
CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS  = int(os.getenv("CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS", "400"))

# ── Model config (ANTHROPIC — Claude Haiku) ──────────────────────────────
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL        = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_API_URL      = "https://api.anthropic.com/v1/messages"
CLAUDE_API_VERSION  = os.getenv("CLAUDE_API_VERSION", "2023-06-01")
