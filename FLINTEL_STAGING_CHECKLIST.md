# FLINTEL Staging Checklist (nothing here has been done; no staging resource was available or touched)

**Rule zero:** a staging stack must be a separate deployment with its own database, its own keys and its own billing sandbox. Do not trust the word "staging" in a hostname or database name; verify the connection strings point to non-production hosts before the first request. No production credential or database is used for any step.

## A. Isolated resources to create (all are NOT YET AVAILABLE; exact set required)
| Resource | What to create | Notes |
|---|---|---|
| Hosting | a second app instance (same build, own URL, own session secret) | deploys branch `feature/search-modes-universal-intelligence` only after your explicit approval |
| MongoDB | a separate database/cluster, empty or restored from a sanitised copy | needs the collections used by the app (`flintel_signals`, chats, jobs, credit usage, plans, cache). A few hundred synthetic Reddit and Hacker News signal documents with valid embeddings are enough |
| MySQL (only if you want to exercise MySQL sources) | a separate server/schema with a `flintel_signals` table (columns: id, post_url, text, embedding, created_utc/posted_at ...) | set `MYSQL_READ_ENABLED`, `MYSQL_READ_DATABASES`, host/user/password to the staging server; keep the probe off until its publication column is confirmed indexed, then set `ZERO_RESULT_MYSQL_PROBE_ENABLED=true` |
| API credentials | a staging OpenAI key with a low spend cap; a staging Anthropic key if used | needed for the router and analysis; never copy production keys |
| External services | outbound access to `hn.algolia.com` (public, no key) | needed for Live; if blocked, Live reports `failed`/`unavailable` honestly |
| Billing | the billing provider's **test mode** store and webhook secret (Lemon Squeezy test mode) | only if plan upgrades are to be tested; searches/credits need no provider |
| Collector | nothing (the external HN collector is out of scope) | stored HN data comes from your seeded documents |

## B. Environment (staging only)
`HACKERNEWS_ENABLED=true`, `SIGNAL_REPORT_ENABLED=true`, optionally `ZERO_RESULT_MYSQL_PROBE_ENABLED=true`. Everything else at its default. Do not change credit amounts, plans or schedules.

## C. Verification steps (record each result)
1. `python3 tools/hn_data_status.py` against the staging Mongo (read-only, counts only).
2. Matrix: platform {Reddit, Hacker News, All} x mode {Live, Historical, Combined} x query {topic, "show me recent discussions", date-limited}.
   - Live + Reddit only: empty result with "no live source" wording, not "no match".
   - Historical: no network call to Algolia (check the network log); cards tagged HISTORICAL.
   - Combined: cards tagged LIVE / HISTORICAL / BOTH; a post present in both appears once as BOTH.
   - Combined with Algolia blocked: stored results still returned, "live path failed" note.
3. Change mode on the same topic in the same chat: results must be per-mode (cache isolation).
4. Credits (staging owner with a known allowance): one search = one charge in every mode; retry with the same submit key = no second charge; exhausted owner = 429 in every mode.
5. Zero-result: an empty window on Mongo, on Mongo+MySQL (probe off: UNKNOWN wording; probe on: real counts), and with MySQL stopped (UNKNOWN, not "nothing collected").
6. Browser: phone width and desktop, keyboard focus on the mode control, light/dark, loading/error states.
7. Roll back by redeploying the previous build; the new message field, cache keys and answer block are additive.

## D. Status
IMPLEMENTED, LOCALLY TESTED. Not INTEGRATION VERIFIED, not STAGING VERIFIED, not PRODUCTION APPROVED.

## E. Tooling added for this phase
`.env.staging.example`, `tools/staging_preflight.py`, `tools/staging_verify.py`, and the full runbook in `FLINTEL_STAGING_SETUP.md` (resources, start command, probe review, HN-only billing finding). The tools are tested offline in `tests/test_staging_tools.py` (13); they have not been run against a real stack.
