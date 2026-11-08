# FLINTEL Environment Variable Reference

Generated from the code on branch `feature/search-modes-universal-intelligence`: an AST scan of every non-test module for `os.getenv` / `os.environ` reads and the config helper wrappers (191 names found), plus a manual supplement for variables read through other helpers (`LEMONSQUEEZY_*` in `billing.py`, `KEEPALIVE_*`). A variable read through some unusual mechanism could still be missing; if you find one, add it here. "Default" is the literal default in the code (`<expr>` = computed). **Sensitive = never log, never commit; set only in the provider's secret store.** The first table is hand-verified; the second lists the remaining tuning variables, which should stay at their defaults in staging.

## A. Variables that matter for staging

| Variable | Group | Purpose | Required? | Default | Staging setting | Cost risk | Sensitive? |
|---|---|---|---|---|---|---|---|
| `DEFAULT_USER_TIMEZONE` | 1 Application | Fallback timezone for date parsing | No | unset | default | none | no |
| `FLINTEL_DISABLE_LOADER` | 1 Application | Disables the front-end loader | No | unset | default | none | no |
| `FLINTEL_ENV` | 1 Application | Deployment identity; only `staging` changes behaviour (scheduler default off) | Staging: yes (tooling + default) | unset | staging | none | no |
| `GOOGLE_SITE_VERIFICATION` | 1 Application | SEO verification meta tag | No | unset | unset | none | no |
| `PORT` | 1 Application | Listen port, provided by the host (not read by the app: the start command uses $PORT) | host | unset | leave to host | none | no |
| `SITE_URL` | 1 Application | Public base URL (OAuth redirect, https detection behind a proxy) | Staging: yes | unset | https://<staging-host> | none | no |
| `ACTIVE_INTENT_ENABLED` | 10 Feature flags | Active-lead-intent workflow | No | `false` | UNSET/false | PAID (extra model calls) | no |
| `INTENT_BRIDGE_ENABLED` | 10 Feature flags | LLM intent re-ranking of candidates | No | unset | UNSET/false | PAID (extra model calls) | no |
| `OPTION_A_ENABLED` | 10 Feature flags | Option A outreach flow | No | `false` | default | varies | no |
| `REDDIT_LIVE_LINKS_ENABLED` | 10 Feature flags | Legacy GPT web-search Reddit links (also disabled in source) | No | `false` | UNSET/false | PAID | no |
| `SIGNAL_REPORT_ENABLED` | 10 Feature flags | Add the five-class signal report to answers | No (default off) | `false` | true | none | no |
| `STRICT_INTENT_MODE` | 10 Feature flags | Strict intent filtering | No | `false` | UNSET/false | PAID (extra model calls) | no |
| `EMAIL_BACKEND` | 11 Optional services | Email delivery backend (`smtp`/`console`) | No | unset | UNSET (email sign-up answers unavailable) | outbound email | no |
| `EMAIL_FROM` | 11 Optional services | Sender address | No | unset | UNSET | none | no |
| `SMTP_HOST` | 11 Optional services | SMTP server | No | unset | UNSET | outbound email | no |
| `SMTP_PASSWORD` | 11 Optional services | SMTP password | No | unset | UNSET | none | YES |
| `SMTP_PORT` | 11 Optional services | SMTP port | No | unset | UNSET | none | no |
| `SMTP_USERNAME` | 11 Optional services | SMTP user | No | unset | UNSET | none | no |
| `MONGODB2` | 2 MongoDB | READ-ONLY signals mirror (extra source of posts) | No | unset | UNSET (preflight fails if set) | none | YES |
| `MONGODB3` | 2 MongoDB | Cluster for every collection except signals (users, chats, jobs, credits, caches); required | Yes | unset | same staging string is fine | none | YES |
| `MONGODB4` | 2 MongoDB | READ-ONLY signals mirror (extra source of posts) | No | unset | UNSET (preflight fails if set) | none | YES |
| `MONGODB_DB` | 2 MongoDB | Database name used on every cluster | Yes (default `flintel_bot`) | `flintel_bot` | flintel_staging | none | no |
| `MONGODB_URI` | 2 MongoDB | Primary cluster: `flintel_signals` | Yes | unset | staging cluster string | none (host/DB cost) | YES |
| `MONGO_CONNECT_TIMEOUT_MS` | 2 MongoDB | Driver connect timeout | No | `15000` | default | none | no |
| `MONGO_FIND_MAX_TIME_MS` | 2 MongoDB | Server-side time cap on finds | No | `120000` | default | none | no |
| `MONGO_SERVER_SELECTION_TIMEOUT_MS` | 2 MongoDB | Driver server-selection timeout | No | `15000` | default | none | no |
| `MONGO_SOCKET_TIMEOUT_MS` | 2 MongoDB | Driver socket timeout (primary only) | No | `120000` | default | none | no |
| `MYSQL_CONNECT_TIMEOUT` | 3 MySQL | connect timeout (s) | No | `10` | default | none | no |
| `MYSQL_HOST` | 3 MySQL | MySQL host | Only if enabled | unset | unset | none | no |
| `MYSQL_PASSWORD` | 3 MySQL | MySQL password | Only if enabled | unset | unset | none | YES |
| `MYSQL_PORT` | 3 MySQL | MySQL port | Only if enabled | `3306` | 3306 | none | no |
| `MYSQL_READ_DATABASES` | 3 MySQL | Comma list of schemas read as signal sources | Only if enabled | `flintel,flintel_static,flintel_google` | unset | none | no |
| `MYSQL_READ_ENABLED` | 3 MySQL | Read extra signal sources from MySQL | No (default off) | `false` | false (round one) | none | no |
| `MYSQL_READ_TIMEOUT` | 3 MySQL | read timeout (s) | No | `60` | default | none | no |
| `MYSQL_SSL` | 3 MySQL | TLS to MySQL | Only if enabled | `false` | true if remote | none | no |
| `MYSQL_SSL_CA` | 3 MySQL | CA bundle path | No | unset | unset | none | no |
| `MYSQL_USER` | 3 MySQL | MySQL user | Only if enabled | unset | unset | none | no |
| `ZERO_RESULT_MYSQL_PROBE_ENABLED` | 3 MySQL | Bounded read-only MySQL coverage probe for zero-result explanations | No | `false` | false until verified | none (DB load) | no |
| `ANTHROPIC_API_KEY` | 4 OpenAI and embeddings | DEPRECATED, read but unused (verified: config comments; every call goes through OpenAI) | No | unset | UNSET | none | YES |
| `CLAUDE_MODEL` | 4 OpenAI and embeddings | Legacy name kept for compatibility; not a live provider | No | `claude-haiku-4-5-20251001` | leave default | none | no |
| `EMBEDDING_MODEL` | 4 OpenAI and embeddings | Embedding model | No | `text-embedding-3-small` | default `text-embedding-3-small` | per-token | no |
| `EMBEDDING_PROVIDER` | 4 OpenAI and embeddings | Embedding provider | No | `openai` | default `openai` | per-token | no |
| `LAZY_EMBED_ENABLED` | 4 OpenAI and embeddings | Embed stored posts that lack an embedding at query time | No (default ON) | `1` | 0 (explicit) | PAID per query | no |
| `LAZY_EMBED_MAX_DOCS_PER_QUERY` | 4 OpenAI and embeddings | Cap on lazy embeddings per query | No | `100` | default | bounds cost | no |
| `LLM_MODEL` | 4 OpenAI and embeddings | Chat/answer model | No | `gpt-5-mini` | default `gpt-5-mini` | per-token | no |
| `OPENAI_API_KEY` | 4 OpenAI and embeddings | THE LLM and embedding key (router, answers, query embeddings) | Yes for any search | unset | staging project key with a hard budget limit | PAID, metered | YES |
| `OPENAI_RESPONSES_URL` | 4 OpenAI and embeddings | Responses API endpoint override | No | `https://api.openai.com/v1/responses` | default | none | no |
| `GOOGLE_CLIENT_ID` | 5 Google OAuth | OAuth client id | Staging: for login tests | unset | separate staging client | none | no |
| `GOOGLE_CLIENT_SECRET` | 5 Google OAuth | OAuth client secret | Staging: for login tests | unset | separate staging client | none | YES |
| `SESSION_SECRET_KEY` | 6 Session and security | Signs session and guest cookies. The app EXITS at startup if missing or `dev-only-change-me`; the staging preflight additionally rejects weak values (short, repeated, sequential, predictable, low entropy) | Yes | unset | new random 64-hex, never reused | none | YES |
| `HACKERNEWS_ENABLED` | 7 Search integrations | Register Hacker News (live fetch via Algolia) as a platform | No (default off) | `false` | true | none (free API) | no |
| `HN_FETCH_TIMEOUT_SECONDS` | 7 Search integrations | Overall deadline of one Hacker News fetch | No | `12` | default 12 | none | no |
| `HN_HITS_PER_PAGE` | 7 Search integrations | Hits per page | No | `50` | default | none | no |
| `HN_MAX_PAGES_PER_KEYWORD` | 7 Search integrations | Pages per keyword | No | `3` | default | none | no |
| `RAPIDAPI_GOOGLE_HOST` | 7 Search integrations | Paid Google-discovery host | No | `google-search116.p.rapidapi.com` | UNSET | PAID | no |
| `RAPIDAPI_KEY` | 7 Search integrations | Paid Google-discovery service key | No | unset | UNSET | PAID | YES |
| `RECENT_DEFAULT_WINDOW_DAYS` | 7 Search integrations | Default recency window | No | `30` | default 30 | none | no |
| `CREDIT_ENFORCEMENT_DISABLED` | 8 Billing and credits | EMERGENCY kill switch: only the literal `true` turns credit limits off | No | unset | UNSET (enforcement ON) | removes spend protection | no |
| `CREDIT_FALLBACK_COST` | 8 Billing and credits | Cost charged when the router proposal is invalid | No | unset | default (do not change: billing) | billing | no |
| `FREE_MONTHLY_CREDITS` | 8 Billing and credits | Free monthly credit allowance | No | unset | default | affects spend cap | no |
| `GUEST_CREDIT_ALLOWANCE` | 8 Billing and credits | Guest allowance | No | `-1` | default | affects spend cap | no |
| `GUEST_SEARCH_LIMIT` | 8 Billing and credits | Legacy guest search count | No | `1` | default | affects spend cap | no |
| `JOB_ATTRIBUTION_MODE` | 8 Billing and credits | Whether collector jobs carry credit request ids | No | unset | default off | none | no |
| `LEMONSQUEEZY_API_KEY` | 8 Billing and credits | Billing provider API key | No (upgrades only) | unset | UNSET for round one | none | YES |
| `LEMONSQUEEZY_STORE_ID` | 8 Billing and credits | Billing store id | No | unset | UNSET | none | no |
| `LEMONSQUEEZY_TEST_MODE` | 8 Billing and credits | Billing provider test mode | If billing is configured | unset | true | none | no |
| `LEMONSQUEEZY_VARIANT_ID_BUSINESS` | 8 Billing and credits | Plan variant id | No | unset | UNSET | none | no |
| `LEMONSQUEEZY_VARIANT_ID_STANDARD` | 8 Billing and credits | Plan variant id | No | unset | UNSET | none | no |
| `LEMONSQUEEZY_WEBHOOK_SECRET` | 8 Billing and credits | Webhook signing secret | No | unset | UNSET | none | YES |
| `PLAN_CREDITS_BUSINESS` | 8 Billing and credits | Credits of the Business plan | No | unset | default | billing | no |
| `PLAN_CREDITS_STANDARD` | 8 Billing and credits | Credits of the Standard plan | No | unset | default | billing | no |
| `INCREMENTAL_RESCAN_ENABLED` | 9 Background jobs | Incremental rescans of stored posts | No | `true` | default | none | no |
| `KEEPALIVE_ENABLED` | 9 Background jobs | Self-ping loop (defined but NOT started by index.py) | No | unset | n/a | none | no |
| `KEEPALIVE_URL` | 9 Background jobs | Self-ping URL | No | unset | n/a | none | no |
| `NONBLOCKING_STREAM_ENABLED` | 9 Background jobs | Non-blocking chat page streaming | No | `true` | default | none | no |
| `SEARCH_WEBSITE_KEYWORDS` | 9 Background jobs | Cap of keywords written into the cycle's search job (0 = no job; the LLM + Google steps still run) | No | `200` | default | none | no |
| `TRENDING_KEYWORDS_ENABLED` | 9 Background jobs | The 12-hourly trending-keywords cycle (LLM web-search + Google call + a search job). NEW | No | unset | false (explicit) | PAID + DB writes while on | no |
| `TURN_FINISH_DRY_RUN` | 9 Background jobs | Dry-run switch of the finisher | No | unset | default | none | no |
| `TURN_FINISH_ENABLED` | 9 Background jobs | Durable turn-finisher worker; default off = dry-run, records only | No | unset | UNSET/false | PAID when on | no |

### Tooling-only variables (read by `tools/`, never by the application)

| Variable | Meaning |
|---|---|
| `STAGING_FORBIDDEN_HOSTS` | comma list of production hostnames/db names/app hostnames (tooling only; the app ignores it) |
| `STAGING_FORBIDDEN_KEY_SHA256_PREFIXES` | 12-hex SHA-256 fingerprints of production secrets (tooling only) |
| `STAGING_OPENAI_LIMIT_CONFIRMED` | operator attestation that the OpenAI project has a hard budget limit (tooling only) |
| `STAGING_MYSQL_PROBE_VERIFIED` | set true only after `staging_verify.py --mysql-probe` passed (tooling only) |

| `SESSION_COOKIE_SECURE` | 6 Session and security | Mark the session cookie Secure (HTTPS only) | No | `false` | `true` (the staging preflight requires it) | none | no |
| `TRUSTED_HOSTS` | 6 Session and security | Comma list of hostnames the app answers to; any other Host header gets 400 (Starlette TrustedHostMiddleware) | No | empty (OFF: the Host header is trusted) | the staging host only, no wildcard. **The app refuses to start in staging when empty** (`FLINTEL_ENV=staging`) | none | no |
| `CLAUDE_MAP_CONCURRENCY` | 4 Model calls | Concurrent map-step / notes-condense model requests (one per batch of 12 posts). Previously strictly sequential. `1` = sequential. Results keep their order; a failed batch is retried once; batches that still fail are reported in the answer (`analysis_coverage`) | No | `4` (max 16) | `4` | none | **yes: more requests in flight at once (same number of requests; failed batches add one retry each)** |
| `ORIGIN_CHECK_ENABLED` | 6 Session and security | Reject cross-site state-changing requests (POST/PUT/PATCH/DELETE): browsers' `Sec-Fetch-Site` decides when present (`same-origin`/`none` pass, `cross-site`/`same-site` blocked unless the Origin host is in `TRUSTED_HOSTS`); otherwise `Origin` must match `Host`; no Origin/Sec-Fetch-Site (curl, webhooks) passes (origin_guard.py) | No | `true` (**ON**) | `true`. Required in staging | none | no |
| `TRUSTED_PROXY_HOPS` | 6 Session and security | Reverse proxies YOU control in front of the app; the client address is the Nth `X-Forwarded-For` entry from the right. `0` ignores the header (app directly exposed) | No | `1` | the real number of proxies | none | no |
| `LOGIN_RATE_LIMIT_ENABLED` | 6 Session and security | Throttle repeated FAILED e-mail logins (login_throttle.py) in the existing `flintel_email_codes` collection; storage errors fail open | No | `true` (ON) | `true` | none | no |
| `LOGIN_RATE_WINDOW_SECONDS` | 6 Session and security | Window for the failed-login counters | No | `900` | `900` | none | no |
| `LOGIN_MAX_FAILURES_PER_ACCOUNT_IP` | 6 Session and security | Failed logins allowed per (client address + account) per window | No | `8` | `8` | none | no |
| `LOGIN_MAX_FAILURES_PER_IP` | 6 Session and security | Failed logins allowed per client address per window (behind a proxy the address is the last X-Forwarded-For hop) | No | `40` | `40` | none | no |

## B. Remaining tuning variables (112): leave at their defaults in staging

Group 11/others: these tune limits, timeouts, token budgets and caches. Variables containing `TOKENS`, `EVIDENCE`, `KEYWORDS`, `POSTS` or `BATCH` change how much text is sent to the paid model (indirect cost); none is sensitive.

| Variable | Default | Defined in | Indirect cost? |
|---|---|---|---|
| `ACTIVE_INTENT_WINDOW_MINUTES` | `unset` | config.py | no |
| `BUSY_FLAG_TIMEOUT_SECONDS` | `400` | config.py | no |
| `CHAT_SUMMARY_MAX_TURNS` | `8` | config.py | no |
| `CHAT_SUMMARY_TURN_CHAR_LIMIT` | `400` | config.py | no |
| `CLAUDE_API_VERSION` | `2023-06-01` | config.py | no |
| `CLAUDE_MAP_MAX_TOKENS` | `512` | config.py | indirect |
| `CLAUDE_MAX_KEYWORDS` | `10` | config.py | indirect |
| `CLAUDE_MAX_TOKENS` | `2500` | config.py | indirect |
| `CLAUDE_NOTES_PER_CHUNK` | `8` | config.py | no |
| `CLAUDE_POSTS_PER_CHUNK` | `12` | config.py | indirect |
| `CLAUDE_ROUTER_MAX_TOKENS` | `800` | config.py | indirect |
| `CLAUDE_TIMEOUT_SECONDS` | `60` | config.py | no |
| `CLAUDE_TOPIC_RESOLVER_MAX_TOKENS` | `400` | config.py | indirect |
| `CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS` | `1200` | config.py | indirect |
| `EMBEDDING_MAX_CHARS` | `8000` | config.py | no |
| `EMBEDDING_TIMEOUT` | `20` | config.py | no |
| `FLINTEL_SIGNALS_COLLECTION` | `flintel_signals` | intent_prototype/retriever.py | no |
| `GOOGLE_FALLBACK_TRIGGER_SECONDS` | `40` | flintel.py | no |
| `GOOGLE_QUERY_KEYWORDS_PER_BATCH` | `2` | google.py | indirect |
| `GOOGLE_SEARCH_DEPTH` | `200` | google.py | no |
| `GOOGLE_SEARCH_MAX_RESULTS_PER_KEYWORD` | `10` | google.py | no |
| `GOOGLE_SEARCH_TIMEOUT_SECONDS` | `15` | google.py | no |
| `GUEST_ID_COOKIE_MAX_AGE_DAYS` | `400` | config.py | no |
| `GUEST_INFLIGHT_STALE_SECONDS` | `900` | config.py | no |
| `INCREMENTAL_FULL_SCAN_EVERY_N_POLLS` | `10` | config.py | indirect |
| `INCREMENTAL_OVERLAP_SECONDS` | `90` | config.py | no |
| `INCREMENTAL_WATERMARK_FIELD` | `` | config.py | no |
| `INTENT_BRIDGE_TIMEOUT_SECONDS` | `25` | config.py | no |
| `INTENT_CACHE_COLLECTION` | `intent_classification_cache` | config.py | no |
| `INTENT_CACHE_ENABLED` | `true` | config.py | no |
| `INTENT_CACHE_TTL_DAYS` | `30` | config.py | no |
| `INTENT_CANDIDATE_MAX` | `200` | config.py | no |
| `INTENT_CANDIDATE_MIN` | `100` | config.py | no |
| `INTENT_CANDIDATE_MULTIPLIER` | `4` | config.py | no |
| `INTENT_CLASSIFY_PARALLEL_BATCHES` | `3` | config.py | indirect |
| `INTENT_SHORTCIRCUIT_HEAD` | `50` | config.py | no |
| `INTENT_SHORTCIRCUIT_MIN_CONFIDENCE` | `0.70` | config.py | no |
| `INTENT_SHORTCIRCUIT_MIN_PASSING` | `15` | config.py | no |
| `KEEPALIVE_INTERVAL_SECONDS` | `unset` | keepalive.py/config.py | no |
| `KEEPALIVE_TIMEOUT_SECONDS` | `unset` | keepalive.py/config.py | no |
| `LAZY_EMBED_BATCH_SIZE` | `50` | config.py | indirect |
| `LAZY_EMBED_MIN_TEXT_CHARS` | `20` | config.py | no |
| `LLM_REASONING_EFFORT` | `low` | config.py, intent_prototype/llm.py | no |
| `LLM_REASONING_HEADROOM` | `2048` | config.py, intent_prototype/llm.py | no |
| `LLM_VERBOSITY` | `` | config.py | no |
| `MAX_ANALYSIS_EVIDENCE` | `100` | config.py, flintel.py | indirect |
| `MAX_CHAT_EVIDENCE_POSTS` | `7` | config.py | indirect |
| `MAX_COMBINED_POSTS_FOR_CLAUDE` | `7` | flintel.py | indirect |
| `MAX_KEYWORDS` | `20` | config.py | indirect |
| `MAX_MATCHED_RESULTS` | `25` | config.py | no |
| `MAX_POSTS_PER_PLATFORM` | `3` | config.py, flintel.py | indirect |
| `MAX_TIME_WINDOW_DAYS` | `3650` | config.py, flintel.py | no |
| `MAX_WEBSITE_KEYWORDS` | `20` | config.py, website_intelligence.py | indirect |
| `MAX_WEBSITE_PAGES` | `5` | config.py | no |
| `MIN_ANALYSIS_EVIDENCE` | `15` | config.py | indirect |
| `RECENCY_HALF_LIFE_DAYS` | `30` | config.py | no |
| `RECENCY_RANK_WEIGHT` | `0.05` | config.py | no |
| `REDDIT_LIVE_MAX_INFLIGHT` | `` | config.py | no |
| `REDDIT_LIVE_MODEL` | `` | config.py | no |
| `RESPONSE_TIMEOUT` | `360` | config.py | no |
| `ROUTER_MAX_MATCH_PHRASES` | `12` | config.py | no |
| `SCAN_DEADLINE_SECONDS` | `90` | config.py | indirect |
| `SCAN_DISCONNECT_GRACE_SECONDS` | `10` | config.py | indirect |
| `SCAN_RESULT_TTL_SECONDS` | `120` | config.py | indirect |
| `SEARCH_PROGRESS_MAX_TOKENS` | `300` | flintel.py | indirect |
| `SIGNAL_EMBEDDING_CANDIDATE_POOL` | `0` | config.py | indirect |
| `SIGNAL_EMBEDDING_FETCH_BATCH` | `5000` | config.py | indirect |
| `SIGNAL_EMBEDDING_MAX_SCAN` | `0` | config.py | indirect |
| `SIGNAL_EMBEDDING_RECENCY_POOL` | `0` | config.py | indirect |
| `SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD` | `0.35` | config.py | no |
| `SINGLE_FLIGHT_SCAN_ENABLED` | `true` | config.py | indirect |
| `SINGLE_FLIGHT_STALE_SECONDS` | `600` | config.py | no |
| `STREAMING_SCORE_ENABLED` | `true` | config.py | no |
| `STREAM_CHUNK_CHARS` | `3` | config.py | no |
| `STREAM_CHUNK_DELAY_SECONDS` | `0.02` | config.py | no |
| `STRICT_SUMMARY_MIN_OVERLAP` | `0.5` | config.py | no |
| `STRICT_TITLE_MATCH_MIN_JACCARD` | `0.6` | config.py | no |
| `STRICT_WAIT_SECONDS` | `75` | config.py | no |
| `TOPIC_CACHE_MAX_EVIDENCE` | `100` | config.py | indirect |
| `TOPIC_CACHE_MIN_TOPUP` | `5` | config.py | no |
| `TOPIC_CACHE_TTL_DAYS` | `7` | config.py | no |
| `TOPIC_MATCH_MAX_TOKENS` | `400` | website_intelligence.py | indirect |
| `TURN_FINISH_ATTEMPTS_WARN` | `8` | config.py | no |
| `TURN_FINISH_BACKFILL_MAX_AGE_DAYS` | `3` | config.py | no |
| `TURN_FINISH_BACKFILL_MAX_CHATS` | `20` | config.py, turn_finisher.py | no |
| `TURN_FINISH_BACKFILL_MAX_TURNS` | `50` | config.py | no |
| `TURN_FINISH_BACKFILL_SECONDS` | `1800` | turn_finisher.py | no |
| `TURN_FINISH_BACKOFF_BASE_SECONDS` | `15` | turn_finisher.py | no |
| `TURN_FINISH_BACKOFF_MAX_SECONDS` | `600` | turn_finisher.py | no |
| `TURN_FINISH_BUSY_RETRY_SECONDS` | `10` | turn_finisher.py | no |
| `TURN_FINISH_CONCURRENCY` | `2` | turn_finisher.py | no |
| `TURN_FINISH_LEASE_SECONDS` | `900` | turn_finisher.py | no |
| `TURN_FINISH_MAX_AGE_DAYS` | `30` | config.py | no |
| `TURN_FINISH_MAX_PASSES_PER_HOUR` | `20` | config.py | no |
| `TURN_FINISH_NEW_TURN_DELAY_SECONDS` | `60` | turn_finisher.py | no |
| `TURN_FINISH_ONDEMAND_MAX_AGE_HOURS` | `0` | config.py | no |
| `TURN_FINISH_ONDEMAND_MAX_TURNS` | `5` | config.py | no |
| `TURN_FINISH_PENDING_RETRY_SECONDS` | `20` | turn_finisher.py | no |
| `TURN_FINISH_SUMMARY_SECONDS` | `300` | config.py | no |
| `TURN_FINISH_SWEEP_SECONDS` | `15` | turn_finisher.py | no |
| `UNFILTERED_DEFAULT_LIMIT` | `3` | flintel.py | no |
| `URL_MERGED_MAX_PHRASES` | `15` | config.py | no |
| `URL_PROMPT_MAX_KEYWORDS` | `10` | config.py | indirect |
| `URL_PROMPT_MAX_PHRASES` | `7` | config.py | no |
| `URL_PROMPT_MERGE_ENABLED` | `true` | config.py | no |
| `WEBSITE_EVIDENCE_CACHE_TTL_DAYS` | `3` | config.py | indirect |
| `WEBSITE_EVIDENCE_MAX_TOKENS` | `1200` | config.py | indirect |
| `WEBSITE_FETCH_MAX_CHARS` | `8000` | config.py | no |
| `WEBSITE_FETCH_TIMEOUT_SECONDS` | `15` | config.py | no |
| `WEBSITE_INSIGHT_MAX_TOKENS` | `1200` | config.py | indirect |
| `WEBSITE_STRUCTURED_SUMMARY_MAX_TOKENS` | `500` | website_intelligence.py | indirect |
| `WEBSITE_SUMMARY_MAX_TOKENS` | `300` | website_intelligence.py | indirect |

## C. Verified findings
- `ANTHROPIC_API_KEY` is deprecated and unused (comment in `config.py`; `intent_prototype/llm.py` marks it unused); **OpenAI is the only LLM and embedding provider.**
- `CREDIT_ENFORCEMENT_DISABLED` defaults to enforcement ON; only the literal `true` disables it.
- `TURN_FINISH_ENABLED` defaults off and the finisher runs in dry-run (records only), so it makes no paid call at startup.
- `KEEPALIVE_*` exist but `start_keepalive` is not called from `index.py`: no self-ping runs.
- Startup order that makes the first boot free of paid calls: `TRENDING_KEYWORDS_ENABLED=false` (and `FLINTEL_ENV=staging`) means the scheduler never starts; no other startup hook calls a paid API.


## Security configuration: what is actually active
| Protection | Default | State when unset | Staging |
|---|---|---|---|
| Failed-login throttle (`LOGIN_RATE_LIMIT_ENABLED`) | ON | **active** | active |
| Google OAuth `redirect_uri` from `SITE_URL` | uses `SITE_URL` when set | **active only when `SITE_URL` is set**; otherwise derived from the request Host (local development) | `SITE_URL` required (https) |
| `TRUSTED_HOSTS` Host allow-list | OFF | **not active** | required; startup refuses when empty |
| `ORIGIN_CHECK_ENABLED` cross-site request check | ON | **active** (a Host-rewriting proxy only matters for browsers that send no `Sec-Fetch-Site`) | required; startup refuses when off |
| `POST /logout`; cross-site `GET /logout` ignored | n/a | **active** | active |
| Failed-login in-process fallback when the store is down | n/a | **active** (per worker) | active |
| `TRUSTED_PROXY_HOPS` | 1 | **active**; wrong value = throttle/limits keyed on the wrong address | set to the real proxy count |
| `SESSION_COOKIE_SECURE` | OFF | **not active** | required; startup refuses when off |
A deployment that sets none of the OFF-by-default variables has none of those protections. Production is not changed by this repository.
