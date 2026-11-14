# Flintel lead MVP: real-source, MongoDB and lead-quality validation plan

STATUS: PREPARED, NOT EXECUTED. No real service was contacted, no credential was used, no money was spent. Everything marked "unverified" stays unverified until a step
below is run with the owner's written approval, in a STAGING environment, and its output is kept. Supersedes `FLINTEL_LEAD_STAGING_VERIFICATION_PLAN.md`.

What exists to run it (all dry-run by default, all refuse to touch anything real without the approval conditions in `tools/lead_validation/_guard.py`):
| Tool | Purpose | Tested on |
|---|---|---|
| `tools/lead_validation/verify_sources.py` | one real scan with timings, call counts, a 10-lead manual check sheet | injected fake connectors (report format only) |
| `tools/lead_validation/mongo_concurrency.py` | multi-PROCESS quota / lock / crash-recovery / duplicate / day-boundary tests on a throw-away database | an in-memory double shared by processes (orchestration only; the report says `"mongo": false`) |
| `evaluation/real_eval/real_eval.py` | the 100-conversation blind evaluation (see its README) | fake fixtures (tooling only) |

## 0. Approvals the owner must give (each is a separate yes)
1. A STAGING environment exists, separate from production, with its own database cluster and its own secrets. Production is never touched.
2. Permission to call the public Hacker News Algolia API from the staging host (no key, no cost).
3. Permission to read the staging copy of the Reddit archive and to make embedding calls with a staging OpenAI key that has a hard monthly spending cap set at the provider.
4. Permission to create and drop throw-away databases named `flintel_leadtest_*` on the staging MongoDB.
5. Permission to collect and keep the evaluation posts (see data handling) and two named reviewers for the labelling.

## 1. Environment variables (names only; never put values in a report or a chat)
| Variable | Used for | Notes |
|---|---|---|
| `MONGODB_URI`, `MONGODB_DB` | primary cluster: the stored Reddit signals (`flintel_signals`) | staging copy only; read access is enough for the scan |
| `MONGODB3` | cluster where the lead collections live (`flintel_business_profiles`, `flintel_lead_scans`, `flintel_leads`, `flintel_lead_quota`) | needs createIndex; the unique indexes are required for correctness |
| `OPENAI_API_KEY`, `EMBEDDING_MODEL` (default `text-embedding-3-small`) | query embeddings for the Reddit matcher | PAID. Use a capped staging key |
| `INTENT_BRIDGE_ENABLED` | an LLM re-rank inside the matcher | OFF unless set. If it is on, every Reddit scan makes extra paid model calls: record its value before testing and measure them |
| `HACKERNEWS_ENABLED`, `HN_FETCH_TIMEOUT_SECONDS`, `HN_MAX_PAGES_PER_KEYWORD`, `HN_HITS_PER_PAGE` | live Hacker News | defaults are validated in `config.py` |
| `SESSION_SECRET_KEY` | sessions | a strong random value, staging only |
| `LEAD_SCANS_PER_DAY` (5), `LEAD_PROFILE_FETCHES_PER_DAY` (10) | per-account limits | set to 3 and 3 for testing |
| `FLINTEL_STAGING_APPROVED=yes`, `FLINTEL_STAGING_ENV_NAME=<name containing "staging">`, `FLINTEL_STAGING_MONGO_URI` | the harness guard and the harness database | the harness creates its own `flintel_leadtest_<random>` database and drops it |

Database permissions needed: `readWrite` on the staging lead database and on `flintel_leadtest_*`; `dropDatabase` only on `flintel_leadtest_*`; read-only on the signals database.
Network: egress to `hn.algolia.com:443`, the staging MongoDB, and `api.openai.com:443` from the staging host only.

## 2. Data handling
- Evaluation posts and `scan_report.json` / `manual_check.csv` contain public text, URLs and public usernames. Keep them in an access-controlled folder, share URLs and ids rather than text,
  and delete the raw posts when the evaluation is finished. Never store credentials in any file the harness writes (it redacts connection strings; check anyway).
- Test accounts use addresses on a domain the owner controls or `*.invalid`. No customer account, profile or lead is read or modified. The staging database is wiped afterwards.
- Application logs must not contain secrets or full post text; check this once by searching the staging logs for the key prefixes and a sample sentence.

## 3. Steps
Every step: record date, commit hash (`git rev-parse HEAD` plus the uncommitted-diff hash), who ran it and keep the raw output. PASS/FAIL/NOT RUN per step.

### A. Hacker News live retrieval (no key, free)
1. `python -m tools.lead_validation.verify_sources --profile profile.json --sources hackernews --days 30 --execute --approved-by "<owner>" --out out_hn/`
2. Record: `coverage.sources[0].state`, `counts`, `provider_calls_counted.algolia_requests`, `wall_ms`. Expected: `searched`, requests <= (queries x pages), wall time well under the 12 s the fetch allows.
3. Open the 10 rows of `manual_check.csv` by hand. PASS = in 10/10 the stored quote appears in the real story, the author and the date match, and the link opens the right item.
4. Failure path with no network change: re-run with `--simulate-hn-down`: PASS = the source shows `failed`, the scan says it was not complete, nothing is reported as searched.
5. Limits to write down: stories only (no comments), the oldest story the API serves, the rate limit behaviour (a 429 must show `rate_limited`).

### B. Reddit historical archive, embeddings and cost
1. Confirm `INTENT_BRIDGE_ENABLED` and note it. `python -m tools.lead_validation.verify_sources --profile profile.json --sources reddit --days 30 --execute --approved-by "<owner>" --out out_rd/`
2. Record: state, counts, `embedding_calls`, `embedding_input_chars`, `wall_ms`. Then read the provider's usage page for the exact tokens and dollars of that window; this is the only valid cost number.
3. Hand-check 10 leads as in A. Also record the archive's real date range: newest and oldest stored post for the platform (a Mongo query on `flintel_signals`, read-only).
4. Verify nothing is written: compare the document count and the newest `_id` of `flintel_signals` before and after the scan.
5. Expected usage per scan (to size the budget, not a price): at most 6 query strings per scan (`lead_connectors._queries`), roughly 90 input tokens in one batched embedding call. Multiply by the
   provider's current per-token price at test time; if `INTENT_BRIDGE_ENABLED` is on, add the model calls it makes (measure, do not assume).

### C. Source timeouts and partial failures
1. Tiny deadline: `... --sources hackernews,reddit --inject-deadline 0.5 --execute ...`: PASS = at least one source `failed` with "did not finish within", the others `searched`, `coverage.status == partial`, the statement says the scan was not complete.
2. Block `hn.algolia.com` for the app host only (egress rule) and scan: PASS = Hacker News `failed`, Reddit unaffected, the page shows a partial scan and never says "searched" for Hacker News.
3. Point the app at an unreachable Mongo (staging misconfiguration) and scan Reddit: PASS = `failed`, a clear message, no 500 page.

### D. MongoDB persistence and multi-process concurrency
1. Start the staging app with TWO worker processes (for example `uvicorn index:app --workers 2`). Confirm the five unique indexes exist (`flintel_business_profiles(owner_key)`, `flintel_lead_scans(owner_key, scan_id)`, `flintel_leads(owner_key, source_id)`, `flintel_leads(owner_key, lead_id)`, `flintel_lead_quota(owner_key, slot)`).
2. Persistence: save a profile in browser session 1, open it in session 2 of the same account (present) and in another account (absent). Scan; mark a lead contacted; re-scan: the lead keeps `contacted` and is not duplicated.
3. Harness: `python -m tools.lead_validation.mongo_concurrency --execute --approved-by "<owner>" --processes 6` with `FLINTEL_STAGING_MONGO_URI` set. Required: all seven scenarios PASS and the report says `verified_multi_worker_mongo: true`:
   `quota_exact`, `lock_exclusive`, `lock_expiry_after_crash`, `failed_scan_consumes`, `duplicate_requests`, `daily_boundary`, `other_owner_isolated`.
4. End to end through HTTP with the two real workers: with `LEAD_SCANS_PER_DAY=3`, send 20 concurrent `POST /leads/find` for one account from a load tool. PASS = exactly 3 scans saved, the rest `error=cap` or `error=busy`, `flintel_lead_quota` count == 3.
5. Kill a worker (`kill -9`) in the middle of a scan; PASS = the account can scan again after the 90 s lease, and the quota for that scan stays consumed.
6. Run each scenario at least 3 times; a single pass is not evidence for a race condition.

### E. Latency and cost
For 10 scans over varied profiles record per source ms, qualification ms, total ms, records, leads, and the provider cost from the usage page. Report median and worst case. A scan must end within the 45 s request limit or be reported failed.

### F. Website analysis and sign-in return on a real browser
1. Profile with a normal site (analysed), a site that stalls (profile still saves, status unavailable), `http://169.254.169.254/`, `http://localhost`, a URL that redirects to an internal address: all refused, nothing fetched, profile still saves.
2. Sign-in return in a real browser: signed out, open `/leads/my`, sign in through the actual form (email) and through Google: PASS = you land on `/leads/my`. (Server side verified locally for the email form; the browser step and Google are NOT verified.)

### G. Lead quality on real posts
Follow `evaluation/real_eval/README.md` (100 collected, two blind reviewers, adjudication, lock, then predict, then score). No tuning on the result.

## 4. Expected expenses (to be confirmed, not promised)
| Item | Basis | Cost |
|---|---|---|
| Hacker News Algolia | public API, no key | $0 (rate limited) |
| Query embeddings, Reddit scan | tokens per scan x provider price per token | tiny per scan; read the usage page; cap the key |
| Intent bridge (only if enabled) | model calls per scan | unmeasured: measure before any estimate |
| Staging MongoDB | whatever the owner already pays for the staging cluster | unchanged; throw-away databases are dropped |
| Human labelling | two reviewers x about 100 posts | staff time, no cash cost |
No other paid service is used by these steps.

## 5. Sign-off table (fill in; leave NOT RUN until run)
| Step | Result | Evidence file | Date | Who |
|---|---|---|---|---|
| A HN live | NOT RUN | | | |
| B Reddit + embeddings + cost | NOT RUN | | | |
| C timeouts / partial failure | NOT RUN | | | |
| D MongoDB persistence + multi-process | NOT RUN | | | |
| E latency and cost over 10 scans | NOT RUN | | | |
| F real-browser sign-in return, website SSRF on staging | NOT RUN | | | |
| G 100-conversation blind evaluation | NOT RUN | | | |
