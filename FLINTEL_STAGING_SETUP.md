# FLINTEL Staging Setup and Verification Runbook

> **Read `FLINTEL_STAGING_AUTHORIZATION_CHECKLIST.md` first.** It supersedes this runbook where they differ: `ANTHROPIC_API_KEY` is unused (only `OPENAI_API_KEY` is needed), and the paid scheduler cycle is now controlled by `TRENDING_KEYWORDS_ENABLED` (off by default when `FLINTEL_ENV=staging`; the preflight requires it explicitly `false`). The preflight has phases and a PASSED/FAILED/INCOMPLETE verdict, and `tools/staging_seed.py` exists. See also `FLINTEL_OPERATIONS.md`.

**Status of this document: PREPARED. No staging resource was created or contacted.** The session that produced it had no staging
hosting, database, API key or OAuth credential, and no outbound access to `hn.algolia.com` or `api.openai.com` (both returned no
connection). Nothing below has been run against a real service, so nothing is INTEGRATION VERIFIED or STAGING VERIFIED.

## 1. Missing resources (the nine), classified
Nothing has been created. You never need to paste a secret into a chat: set each value directly in the provider's own secret store (see "Where to set the values").

| # | Resource | Class | Payment | Needs your authorisation to create |
|---|---|---|---|---|
| 1 | Separate app host (Python 3.12) with its own URL | **Required** | Likely paid (free tiers exist but may sleep/limit) | Yes: creating an account/service and any spend |
| 2 | Separate MongoDB cluster/database | **Required** | Free tier available (e.g. a shared free cluster) | Yes: account/resource creation |
| 3 | Seed data: a few hundred **synthetic** or sanitised Reddit and Hacker News signal documents (valid `created_utc`, 1536-dim embeddings) | **Required** (an empty database makes the verifier fail by design) | Embedding generation costs a few cents if produced with the LLM provider | Yes if it spends; none for hand-made fake vectors |
| 4 | Separate MySQL with a `flintel_signals` table | **Optional** (only for MySQL sources and the coverage probe) | Paid or free depending on host | Yes |
| 5 | Staging OpenAI project key with a hard budget limit (`OPENAI_API_KEY`; `ANTHROPIC_API_KEY` is deprecated and unused) | **Required** (router, analysis, query embeddings) | **Paid, metered** | Yes: it spends money |
| 6 | Separate Google OAuth client, redirect URI = the staging URL only | **Required** for login-based tests (credits need an authenticated owner) | Free | Yes: creating credentials in your Google project |
| 7 | Billing provider **test-mode** store and webhook secret | **Optional** (only plan-upgrade tests; searches and credits need none) | Free in test mode | Yes |
| 8 | Outbound HTTPS from the host to `hn.algolia.com` | **Required** for Live | Free (public API, no key) | No (a network-policy check, nothing to create) |
| 9 | A new random `SESSION_SECRET_KEY` | **Required** | Free | No (generate locally: `python3 -c "import secrets; print(secrets.token_hex(32))"`) |

Minimum viable staging = 1, 2, 3, 5, 6, 8, 9. Everything else is optional.

### Where to set the values (no secret ever goes into a chat or into git)
Use the host's environment-variable / secret settings for the staging service only (the dashboard section is usually called "Environment", "Variables" or "Secrets").
Mark each as secret/hidden where the provider offers it. For MongoDB, build the connection string inside the provider (Atlas "Connect" dialog) and paste it
**only** into the host's variable form. For the OpenAI key, create a new project and key in the OpenAI console with a hard monthly budget limit, name it "flintel-staging", and
paste it only into the host's variable form. For `STAGING_FORBIDDEN_KEY_SHA256_PREFIXES`, compute each production secret's fingerprint on your own machine
(`printf '%s' "$SECRET" | sha256sum | cut -c1-12`) and store only those 12 characters. Run the preflight and verifier in the host's shell or CI job so the values never leave it.

## 2. Deployment preparation (checked in the repository)
- **Start command:** `uvicorn index:app --host 0.0.0.0 --port $PORT` (the `__main__` block runs `reload=True` on port 8080; do not use it on staging). Python 3.12 (the version the tests ran on).
- **Dependencies:** `pip install -r requirements.txt` (FastAPI 0.115.x, Starlette 0.38.x pinned, PyMySQL, pymongo, openai, numpy, passlib/bcrypt). Pillow is **not** listed but two test files import it (`PIL`), which is why they fail in a clean environment; it is not needed at runtime for the search features.
- **Migrations:** none. New data is additive (`search_mode` on a message, `topic::mode` cache keys, `search_mode` block in answers). Required unique indexes are managed by `index_guard.py` at startup; `/api/health/ready` reports `degraded` while one is missing.
- **Health check:** `GET /api/health/ready` (always 200; `status` is `ok` or `degraded`, no data exposed). `GET /ping` is the keep-alive.
- **Logging/errors:** standard `logging`; the MySQL layer masks configured secrets (`safe_error`); the new verifier records error **types** only.
- **Environment:** copy `.env.staging.example` into the host's secret store (never into git).
- **No production credential is referenced anywhere in the repository**; `.env.example` holds placeholders only.

## 3. Isolation gate (run before anything else)
The gate also fails if a `.env` file exists at the app directory or any parent (the application would load it and silently fill unset variables from it, invisibly to an environment-only check). Configure staging only through the host's environment settings.

```
FLINTEL_ENV=staging STAGING_FORBIDDEN_HOSTS=<your production hostnames/db names> \
STAGING_FORBIDDEN_KEY_SHA256_PREFIXES=<first 12 hex of sha256 of each production secret> python3 tools/staging_preflight.py
```
It prints PASS/FAIL per check with masked hosts and no values, and exits non-zero unless: `FLINTEL_ENV=staging`; a production list was supplied;
no Mongo/MySQL host or database name matches it; no secret fingerprint matches a production secret; billing is in test mode if configured;
the MySQL probe is not enabled before its verification. A name like "staging" is never accepted as proof.

## 4. Real integration verification (run only after section 3 passes)
```
python3 tools/staging_verify.py --allow-index-creation --report staging_report.json --base-url https://<staging-app> [--mysql-probe] [--allow-paid-calls] [--max-seconds 300]
```
Safety properties (each is covered by `tests/test_staging_tools.py`):
- **Refuses to start** unless the preflight passes (exit 2), the `--base-url` is https (or localhost) and is not a listed production host (exit 2), and `--allow-index-creation` is given (exit 3). Without that flag the application is never imported, so no connection is opened.
- **Not strictly write-free, and it says so:** importing the application (`database.py`) creates the application's own required indexes on the target database, exactly as starting the app does. That is the only write. Retrieval itself performs none: an AST scan of every function reachable from `get_matched_signals`, plus a grep of the other retrieval modules, found no insert/update/delete/index call, and the tool source contains none (tested). The flag exists so this is a conscious choice for an isolated database.
- **Bounded:** Hacker News fetches use 1 page, 20 hits and a 12 s deadline; HTTP calls 10 s; the mode matrix has an overall cap (`--max-seconds`). The matrix runs 18 retrievals; each **topic** search embeds its query through the LLM provider (about 9 small paid calls); they run **only** with the explicit `--allow-paid-calls` flag (otherwise that check is SKIPPED, not passed).
- **Cannot report false success:** an empty database, a duplicate, a failed or rate-limited path, a live Hacker News fetch that returns nothing or malformed records, Reddit-only Live returning anything, Historical touching the live path, or an unexplained UNKNOWN zero-result are all FAIL; SKIPPED is never PASS; the report's `complete` flag is true only for a live run in which every required automated check is present and PASS, and the report states in `claims` that staging and production verification are never asserted by the tool (see `FLINTEL_OPERATIONS.md` section 8).
- **Prints** counts, timings and error TYPES only; never a credential, URL with credentials, post text or user data.

It checks: MongoDB ping; per-platform counts and oldest/newest publication time; two live Hacker News fetches; every platform {Reddit, Hacker News, all} x mode {live, historical, combined} x {topic, general}
with duplicate, provenance and class counts; the zero-result explanation for an empty window; the MySQL probe review; and the deployed app's readiness.

Not covered by the script (needs an authenticated staging session): **credit consumption**. Follow `FLINTEL_STAGING_CHECKLIST.md` step 4 with a staging
account and read the ledger through `/api/me/usage`. The same behaviours are proven locally by `tests/test_search_mode_credits.py` (21 tests); that is LOCALLY TESTED, not staging.

## 5. MySQL coverage probe: review before enabling (kept OFF)
SQL issued by `mysql_signals.probe_coverage` (SELECT only, through the module's `_select` guard; database name validated against `^[A-Za-z0-9_]+$`; column
names from fixed lists; values bound as parameters):
1. `information_schema.COLUMNS` lookup for the table's columns (cheap).
2. `SELECT /*+ MAX_EXECUTION_TIME(3000) */ COUNT(*), MIN(<pub>), MAX(<pub>) FROM db.flintel_signals WHERE <pub> IS NOT NULL`
3. When a window is requested: `SELECT /*+ MAX_EXECUTION_TIME(3000) */ COUNT(*) ... WHERE <pub> IS NOT NULL AND <pub> >= %s AND <pub> < %s`

Load assessment: it runs **only when a search returned nothing**, once per configured MySQL source, three bounded statements, no embeddings read. **Risk (unverified):**
`COUNT(*)`/`MIN`/`MAX` over a large table without an index on the publication column is a full scan up to the 3 s cap, per empty search. Required before enabling:
(a) confirm an index on `<pub>` (`created_utc`, or `posted_at` for Google-shaped tables) exists on the staging table: `SHOW INDEX FROM flintel_signals`;
(b) run `staging_verify.py --mysql-probe`: it times the queries and prints the optimiser's `EXPLAIN` key and row estimate, and reports FAIL ("do NOT enable") when the key is NULL
or a query exceeds 2 s; (c) only then set `STAGING_MYSQL_PROBE_VERIFIED=true` and `ZERO_RESULT_MYSQL_PROBE_ENABLED=true` on staging. If an index is missing, adding one
(`CREATE INDEX ... ON flintel_signals (<pub>)`) is a DDL change that needs the database owner's approval, on staging first; it was not done and is not part of this change.
`COUNT(*)` of a range is index-only when the index exists. The hint needs MySQL 5.7.8+ (it is ignored by MariaDB, where the time cap would not apply: another reason to verify on the real engine).

## 6. Hacker News-only billing (investigated; not changed)
A Hacker News-only search follows `expects_job=False`: nothing is collected for it, so no collector job is queued. It still goes through the **same** reservation,
claim and commit in `search_credits.CreditStore`, is charged the router-proposed cost, and is saved to the chat. This is the same path website-URL searches already use
(the existing suite shows those are charged the usage-based cost). Verified locally by `test_hn_only_search_is_charged_like_any_search_and_enqueues_no_collector_job` and
`test_hn_only_costs_the_same_as_a_search_that_does_enqueue`. Consequence: Live-only HN searches are billed like collector-backed ones even though no collection happens.
Whether that is the intended price is a product decision; no amount, plan or rule was changed.

## 7. Verification status board (all NOT RUN against real services)
| Item | Local | Staging |
|---|---|---|
| HN live stories | LOCALLY TESTED (fake HTTP) | NOT RUN |
| HN / Reddit historical | LOCALLY TESTED (mongomock) | NOT RUN |
| MongoDB / MySQL connection | n/a | NOT RUN |
| Mode selection, combined, dedup, provenance, classification, zero-result | LOCALLY TESTED | NOT RUN |
| Credit consumption (incl. HN-only) | LOCALLY TESTED | NOT RUN |
| MySQL probe | LOCALLY TESTED (fake connection) | NOT RUN, kept OFF |
