# FLINTEL Operations Guide

Everything here matches the code on branch `feature/search-modes-universal-intelligence`. Nothing described has been run against a real staging
environment (none exists yet); commands marked **[staging]** are for the future isolated stack only. Variable details: `FLINTEL_ENV_REFERENCE.md`.
Resources, costs and authorisations: `FLINTEL_STAGING_AUTHORIZATION_CHECKLIST.md`.

## 1. Local development (free, no external service)
- Python 3.12, `pip install -r requirements.txt` (Pillow is not in the file; two baseline tests need it).
- Tests: `python -m pytest tests -q -p no:cacheprovider -W ignore --continue-on-collection-errors`. About 19 minutes. Expected on this branch: the same 43 failing/erroring IDs as clean `main` (see `FLINTEL_TEST_REPORT.md`).
- All tests use mocks (mongomock, fake HTTP, fake LLM). No test calls a paid service.
- Starting the app locally needs `SESSION_SECRET_KEY` (it exits otherwise) and a reachable MongoDB; do not point a local run at production.

## 2. Environment configuration
- Set variables only in the host's secret/environment panel; never in git or chat. **Do not put a `.env` file on the staging host or in any parent directory of the app**: `config.py` and `database.py` call `load_dotenv()`, which loads the first `.env` found walking upward and uses it to fill every variable missing from the real environment (a `.env` copied from production could silently enable `MONGODB2`/`MONGODB4`, RapidAPI, SMTP, the scheduler or the credit kill switch). The preflight cannot read it, so it **fails** if one exists (existence check only; the file is never opened). Template with placeholders: `.env.staging.example` (force-tracked).
- Staging must set explicitly: `FLINTEL_ENV=staging`, `TRENDING_KEYWORDS_ENABLED=false`, `LAZY_EMBED_ENABLED=0`, `SITE_URL`, and leave **unset**: `MONGODB2`, `MONGODB4`, `RAPIDAPI_*`, `SMTP_*`, `EMAIL_BACKEND`, `CREDIT_ENFORCEMENT_DISABLED`, `ANTHROPIC_API_KEY`.

## 3. Startup behaviour: every write and every paid call (audited)
Start command: `uvicorn index:app --host 0.0.0.0 --port $PORT` (one instance; the `__main__` block uses `reload=True` and is for local use only).

**Database writes at import/startup (all on the database named by `MONGODB_DB`, MongoDB only; MySQL is read-only):**
| Where | Write |
|---|---|
| `database.py` import | `create_index` on: users (`email`, `google_id`, unique sparse), busy_owners (`owner_key` unique), chats (`chat_id` unique, `owner_key`), signals (`topic_key`, `created_utc`), google_posts (`post_url` unique, `search_keyword`, `reddit_fetched`, `discovered_at`), topic_evidence_cache, website_evidence_cache (`url` unique + TTL), credit_usage (`owner_key` unique), plans (`owner_key` unique), subscriptions (`owner_key`), email_codes (TTL); `index_guard` then verifies the required unique indexes |
| `index.py` startup hook 1 | trending scheduler: **only if `TRENDING_KEYWORDS_ENABLED`** (see 4) |
| `index.py` startup hook 2 | `turn_finisher.start()`: default (`TURN_FINISH_ENABLED` off) is dry-run: it starts no worker and no backfill; only when enabled and governed does it create a `due_at` index and start workers |
| first metered LLM call | `usage_meter` creates its indexes lazily (`ts`, `chat_id+topic_key`, `request_id`, `response_id` unique) |
No file writes and no external write at startup. Index creation is idempotent and changes no existing data, but it **is** a write, which is why every tool that imports the application requires `--allow-index-creation`.

**Paid calls at startup:** none, provided `TRENDING_KEYWORDS_ENABLED=false`. With the flag on, a fresh database runs the first cycle immediately (LLM web search + Google discovery + a search job + a schedule document) and then every 12 hours.
**Per user action (not startup):** each search writes chat, credit reservation/commit, a job (none for Hacker News alone), topic cache; Google login writes a user; each LLM call writes a usage record.

## 4. Trending-keywords scheduler (`TRENDING_KEYWORDS_ENABLED`)
- **Disabled** (explicit `false`/`0`/`no`/`off`, or unset while `FLINTEL_ENV=staging`): `start_trending_keywords_scheduler()` logs one line and returns; no task, no LLM call, no Google call, no search job, no schedule document, no timer.
- **Enabled** (`true`/`1`/`yes`/`on`, or unset while `FLINTEL_ENV` is anything else): the original behaviour, unchanged: first cycle immediately if no schedule is saved, then every 12 h on the persisted schedule.
- An unrecognised value is ignored with a warning and the default applies. Production, which does not set `FLINTEL_ENV=staging`, is unchanged.
- Shutdown: nothing to cancel when disabled; when enabled the task ends with the process.

## 5. Isolation preflight
Checks start with: no `.env` file can be loaded by the application (searched from the repository root to the filesystem root, as python-dotenv does).
The session secret must be strong: at least 32 characters, not a known default, no predictable word or sequence (`password`, `secret`, `staging`, `qwerty`, `123456`, `abcdef`, ...), at least 10 distinct characters, at least 3.0 bits of Shannon entropy per character, no short pattern repeated through the value, and no sequential run longer than 6. Generate one with `python3 -c "import secrets; print(secrets.token_hex(32))"` (about 0.015% of random values can trip a rule by chance; just generate again). The preflight prints only generic reasons, never the value or its fingerprint.

`python3 tools/staging_preflight.py [--phase isolation|paid]`: read-only, offline, prints no secrets. Verdict **PASSED / FAILED / INCOMPLETE** (exit 0 / 1 / 4). Every URL-bearing variable, `MYSQL_HOST`, `MONGODB_DB` and every secret (by fingerprint) is checked against the production description you supply; a missing description makes the result INCOMPLETE, never PASSED. The isolation phase proves safety to start **without any paid credential**; the paid phase adds `OPENAI_API_KEY` and the spend-limit attestation.

## 6. Database initialisation
There is no migration step. Starting the app once against the empty staging database creates the indexes in section 3. Confirm with `GET /api/health/ready` (`status: ok`; `degraded` names the subsystem whose required unique index is missing).

## 7. Synthetic seeding [staging]
`python3 tools/staging_seed.py` (dry run; prints the plan, opens no connection), then
`python3 tools/staging_seed.py --apply --i-confirm-single-run --confirm-db <MONGODB_DB>` after the isolation preflight passes. About 49 labelled synthetic records (`synthetic: true`, `_seed`, `message_id stg-seed-*`, `.invalid` URLs); insert-only, idempotent, never overwrites or deletes, uses plain pymongo (no app import, no index creation). **One seed run at a time:** the tool creates no unique index (and must not), so two overlapping runs could both insert the same record. Enforced restriction: `--apply` needs `--i-confirm-single-run` (your attestation that no other seed process runs against this database on any machine); a lock file in the system temp directory refuses a second process on the same machine immediately (exit 2); after writing, a read-only check reports any duplicated `message_id` (exit 1; nothing is deleted or repaired). A database-level lock was rejected because it would need a marker document, another collection or an index. Free placeholder vectors: `--synthetic-vectors`. Real embeddings: `--embed --i-authorize-paid-embeddings` (paid; needs the paid preflight; **not run by the author**). Limits: synthetic text cannot prove real corpus coverage; placeholder vectors do not match real query embeddings; the `.invalid` URLs may be filtered by link-integrity checks when answers are generated, which must be checked in the deployed app.

## 8. Verification [staging]
- `python3 tools/staging_verify.py --offline --report offline.json`: no network or database; repository/config checks; every service check is SKIPPED, so `complete` is false by design.
- `python3 tools/staging_verify.py --allow-index-creation --report r.json --base-url https://<staging>` (the default): isolation preflight, Mongo ping and coverage, two live Hacker News fetches, zero-result explanation, readiness. **No paid call: the mode matrix is SKIPPED unless you opt in.**
- With `--allow-paid-calls` it also runs the 18-retrieval matrix (about 9 query embeddings: PAID), and only after the **paid**-phase preflight passes. `--mysql-probe` adds the MySQL timing/EXPLAIN review.
- Statuses PASS / FAIL / SKIPPED / UNKNOWN. **`complete` means only**: a *live* run in which every automated check required for this deployment (Mongo ping, Mongo coverage, Hacker News live, the mode matrix, the zero-result explanation, app readiness, plus MySQL when `MYSQL_READ_ENABLED` is on) is present exactly once with status PASS and no check anywhere FAILED. A skipped, unknown, failed or missing required check makes it `false`; an offline run is always `false`, even if every row passed.
- **Evidence levels are never merged.** Every report carries `evidence_level` and `claims`: offline = repository checks only (local evidence; `integration_verified` is false); live and complete = `integration_verified` true against the configured target; `staging_verified` and `production_approved` are **always false** in the tool's output, because staging verification also needs the manual steps listed in `manual_steps_pending` (credit consumption with a staging login, your review). Local tests, integration verification, staging verification and production approval remain four separate statuses.

## 9. Logs
Python `logging` to stdout (configured in `database.py`); the host's log viewer is the log store. Messages carry error types, not secrets (`mysql_signals.safe_error` masks the MySQL password). Useful lines: `Trending-keywords scheduler DISABLED`, `Turn finisher: dry run`, `hackernews fetch | state=...`, `credit denial`.

## 10. Failure recovery and safe restart
- Stateless except MongoDB: restart is safe at any time; in-flight searches are recovered by the existing lease/turn logic. A restart re-runs idempotent index creation.
- If `/api/health/ready` is `degraded`: a required unique index could not be built (usually duplicate legacy data); credit operations refuse until fixed. Fix the data on staging, restart.
- If startup exits with the `SESSION_SECRET_KEY` message: set a strong secret and restart.
- Seed failure midway: re-run the same command (idempotent).
- Hacker News unreachable: Live reports `failed`/`unavailable`; Historical and Combined still return stored results.

## 11. Cost controls
Staging: hard OpenAI budget limit on the project; scheduler off; lazy embedding off; `RAPIDAPI_*` unset; suspend the service when idle; delete the cluster after testing. Estimated (not measured) costs are in the authorization checklist and section 12 of the audit.

## 12. Secret rotation
Create the new value in the provider, update the host's variable, redeploy/restart, then revoke the old one. `SESSION_SECRET_KEY` rotation signs everyone out. Never reuse a staging secret in production or the reverse; the preflight detects production reuse by fingerprint.

## 13. Rollback
Redeploy the previous build. The branch's data changes are additive (`search_mode` on messages, `topic::mode` cache keys, `search_mode` in answers, seed records labelled `synthetic`). To remove seed data on staging, delete `{synthetic: true, _seed: "flintel-staging-seed-v1"}` documents manually in the staging database; the tool never deletes.

## 14. Production release gates (all required, none satisfied yet)
1. Real staging verification passed: a live report with `complete: true` (that is integration evidence only), the manual credit steps done, and your review. The tool never states 'staging verified' itself.
2. Decision on Hacker News-only billing and on the open risks in `FLINTEL_MASTER_AUDIT.md`.
3. Confirmed that the deploy path for the production branch cannot be triggered by the staging branch (no CI/deploy config exists in this repository, so this must be checked in the host's settings).
4. `TRENDING_KEYWORDS_ENABLED` decided explicitly for production (unset keeps today's behaviour).
5. Your explicit approval to push, merge and deploy.

## Security configuration and login throttling (post-review)
- **Staging refuses to start** (`RuntimeError` at import, names only, no values) when `FLINTEL_ENV=staging` and any of these is missing: `TRUSTED_HOSTS` (non-empty, no wildcard, containing the `SITE_URL` host), `ORIGIN_CHECK_ENABLED=true`, `SESSION_COOKIE_SECURE=true`, `SITE_URL=https://...`. Other environments are not judged, so local development and the existing production deployment behave as before **and do not gain TRUSTED_HOSTS / Origin protection until you set them**. See `FLINTEL_SECURITY_FINDINGS.md` and the "what is actually active" table in `FLINTEL_ENV_REFERENCE.md`.
- **Behind a proxy:** the cross-site request check is ON by default and uses the browser's `Sec-Fetch-Site`, so a Host-rewriting proxy does not break current browsers. For a client that sends no `Sec-Fetch-Site`, `Origin` is compared with the `Host` the app sees: list the public hostname in `TRUSTED_HOSTS` or pass Host through, otherwise those requests get 403. Set `TRUSTED_PROXY_HOPS` to the real number of proxies you control (default 1).
- **Failed-login throttle** is ON by default: HTTP 429 with `Retry-After` after 8 failed attempts for one (address, account) pair or 40 for one address in 15 minutes. To investigate a lock-out, wait out the window or delete the `login:*` documents from `flintel_email_codes`. To disable in an emergency: `LOGIN_RATE_LIMIT_ENABLED=false`.
- **Dependency pins:** Starlette stays at `<0.39` (see F4 in the findings). Request body size limits must be applied at the reverse proxy.
