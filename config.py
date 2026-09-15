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

(MODEL SWAP) ANTHROPIC_API_KEY / CLAUDE_MODEL / CLAUDE_API_URL /
CLAUDE_API_VERSION have been REMOVED — the model this product calls out
to has moved from Claude to OpenAI (see logics.py for the actual call).
OPENAI_API_KEY / OPENAI_MODEL / OPENAI_API_URL / OPENAI_TIMEOUT_SECONDS
below are their replacements.

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

# ── Model config (OPENAI — replaces the removed Anthropic/Claude config) ──
OPENAI_API_KEY         = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL           = os.getenv("OPENAI_MODEL", "gpt-5-mini")
OPENAI_API_URL         = "https://api.openai.com/v1/responses"
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
