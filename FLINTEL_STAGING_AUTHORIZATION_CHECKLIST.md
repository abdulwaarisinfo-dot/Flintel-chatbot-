# FLINTEL Staging Authorization Checklist

> Companion documents: `FLINTEL_ENV_REFERENCE.md` (every variable), `FLINTEL_OPERATIONS.md` (how each step works and what it writes).

**Status: PLAN ONLY. Nothing has been created, purchased, deployed, seeded or called.** Review it and authorize one resource at a time (section 8).
Prices below were checked against the providers' public pages on 2026-11-05 where noted and can change; confirm on the provider's pricing page before you click "create".
You never paste a secret into this chat. Values go only into the provider dashboard fields described in section 5.

## 0. Two findings that change the plan (read first)
1. **`ANTHROPIC_API_KEY` is deprecated and unused** (config comments; every LLM call and embedding goes through OpenAI). Earlier documents listed it as required; that was wrong and is corrected. Only `OPENAI_API_KEY` is needed.
2. **A fresh database used to trigger a paid background cycle at startup. This is now controlled.** `scheduler.py` runs a trending-keywords loop (one LLM web-search call + one Google-discovery call + a search job + a schedule document, then every 12 hours). It is now gated by the new **`TRENDING_KEYWORDS_ENABLED`** flag: **disabled by default when `FLINTEL_ENV=staging`**, explicit `false` always disables it, and with it disabled no task, paid call, job, schedule document or timer is created. Unset outside staging keeps today's production behaviour. The preflight **requires** `TRENDING_KEYWORDS_ENABLED=false` to be set explicitly. (Implemented and tested locally; not run on a real host.)

## 1. Environment variables, grouped by service
Legend: **R** required, **O** optional, **U** leave unset (setting it is a mistake or a cost), **S** secret (enter only as a hidden/secret value).

**Application**
| Variable | | Staging value |
|---|---|---|
| `SESSION_SECRET_KEY` | R, S | new random value (app exits at startup without it): `python3 -c "import secrets; print(secrets.token_hex(32))"` run locally |
| `SITE_URL` | R | `https://<your-staging-host>` (OAuth redirect and https detection behind the host's proxy) |
| `PYTHON_VERSION` | R (Render) | `3.12.x` |
| `PORT` | provided by the host | do not set |
| `FLINTEL_ENV` | R | `staging` (also makes the scheduler default off) |
| `TRENDING_KEYWORDS_ENABLED` | **R: explicit `false`** | see section 0 |

**MongoDB**
| `MONGODB_URI` | R, S | staging cluster connection string (signals live here) |
|---|---|---|
| `MONGODB3` | R, S | the **same** staging string is fine (users, chats, jobs, credits, caches live here) |
| `MONGODB_DB` | R | `flintel_staging` |
| `MONGODB2`, `MONGODB4` | **U** | read-only signals *mirrors*; if set they would add another database as a source. The preflight fails if either is set |
| `MONGO_*_TIMEOUT_MS` | O | defaults are fine |

**OpenAI (LLM + embeddings)**
| `OPENAI_API_KEY` | R, S | key from a **new OpenAI project with a hard budget limit** |
|---|---|---|
| `LLM_MODEL`, `EMBEDDING_MODEL` | O | defaults (`gpt-5-mini`, `text-embedding-3-small`); do not change |
| `LAZY_EMBED_ENABLED` | **R: explicit `0`** (the preflight requires it) | default is on: stored posts without an embedding are embedded **at query time** (paid). Seed with embeddings and switch it off to keep spend predictable |
| `ANTHROPIC_API_KEY` | **U** | deprecated |

**Google sign-in**
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (S) | R for login-based tests (credits need an owner) | a **separate** OAuth client; redirect URI exactly `https://<staging-host>/auth/google/callback` |
|---|---|---|

**Feature flags under test**
| `HACKERNEWS_ENABLED=true`, `SIGNAL_REPORT_ENABLED=true` | R for the verification | |
|---|---|---|
| `ZERO_RESULT_MYSQL_PROBE_ENABLED=false` | R | stays false until verified; `STAGING_MYSQL_PROBE_VERIFIED` only after that |
| `CREDIT_ENFORCEMENT_DISABLED` | **U** | it is the emergency kill switch; it must be absent so credits are really enforced |
| `INTENT_BRIDGE_ENABLED`, `ACTIVE_INTENT_ENABLED`, `REDDIT_LIVE_LINKS_ENABLED`, `STRICT_INTENT_MODE` | **U** | all default off; each would add paid model calls |

**Third-party discovery, email, billing, MySQL**
| `RAPIDAPI_KEY`, `RAPIDAPI_GOOGLE_HOST` | **U** | paid Google-discovery service; also stops the scheduler's Google step |
|---|---|---|
| `EMAIL_BACKEND`, `SMTP_*` | **U** | email sign-up then answers "unavailable"; use Google login |
| `LEMONSQUEEZY_*` | **U** except `LEMONSQUEEZY_TEST_MODE=true` | plan-upgrade tests are out of scope |
| `MYSQL_READ_ENABLED=false` | O | if you later add MySQL: `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD` (S), `MYSQL_READ_DATABASES`, `MYSQL_SSL` |

**Isolation proof (read by the preflight)**
| `STAGING_FORBIDDEN_HOSTS` | R | comma list of your production database hostnames / db names / app hostnames |
|---|---|---|
| `STAGING_FORBIDDEN_KEY_SHA256_PREFIXES` | R (recommended) | 12-hex fingerprints of each production secret, computed on your own machine; never the secrets |

## 2. Resource cards
**R1. Application host.** *Why:* runs the app, workers and the scheduler; the credit and login tests need the deployed app. *Free tier:* Render's free web service sleeps after 15 minutes idle (750 free hours a month shared), which breaks background workers and the 12-hour scheduler assumptions, so it is not suitable beyond a smoke test. *Cost:* **Starter $7/month** (512 MB, always on, billed by the second; Hobby workspace $0). *Paid subscription:* yes for always-on. *Authorize:* **yes** (account/service creation and spend).
**R2. MongoDB.** *Why:* signals, users, chats, jobs, credits, caches. *Free tier:* **Atlas M0, $0**: 512 MB including indexes, 500 connections, about 100 operations/second. *Recurring cost:* $0 (a paid tier only if you outgrow it). *Paid subscription:* no. *Authorize:* **yes** (account/cluster creation). Fits the plan: about 300 seed posts with 1536-number embeddings is a few MB.
**R3. Seed data.** *Tool:* `tools/staging_seed.py` now exists (dry-run by default, synthetic, insert-only, idempotent, gated; see `FLINTEL_OPERATIONS.md` section 7). *Why:* an empty database makes every retrieval check meaningless (the verifier fails it on purpose). *Free tier:* n/a. *Cost:* synthetic posts are free; **real embeddings** for about 300 short posts is roughly 45k tokens, about $0.001 at `text-embedding-3-small` rates (verify the current price); random vectors would be free but cannot produce meaningful topic matches. *Authorize:* **yes**, because it writes to the database and spends a fraction of a cent. The tool is written and tested offline; running it is what needs your authorisation (it writes to the staging database; real embeddings are a separate paid flag).
**R4. MySQL (optional).** *Why:* only to verify MySQL sources and the coverage probe. *Free tier:* varies by host. *Cost:* host-dependent. *Paid:* possibly. *Authorize:* yes; **skip it for the first staging round.**
**R5. OpenAI project and key.** *Why:* the router, answers and query embeddings. *Free tier:* none. *Cost:* no subscription, pay-as-you-go; an estimated cents per search (not measured) plus the scheduler cycles above. Set a **hard budget limit (suggest $5)** on the project. *Authorize:* **yes**, it spends money.
**R6. Google OAuth client.** *Why:* login is how credits are tested. *Free:* yes. *Cost:* $0. *Authorize:* **yes** (creating credentials in your Google Cloud project; use a separate project if possible). Needs R1's URL first.
**R7. Billing test-mode store (optional).** *Why:* plan upgrades only. *Cost:* free in test mode. *Authorize:* yes; **skip for now.**
**R8. Outbound access to `hn.algolia.com`.** *Why:* Live Hacker News. *Free:* public API, no key. *Authorize:* no. Render allows outbound HTTPS by default.
**R9. New `SESSION_SECRET_KEY`.** *Why:* sessions. *Free.* *Authorize:* no (you generate it locally).
**Minimum viable staging: R1, R2, R3, R5, R6, R8, R9.** Expected spend: $7/month plus a few dollars of OpenAI at most under the $5 limit.

## 3. Recommended provider and configuration
**Host: Render, one Web Service.** Chosen because it is Python-native, deploys straight from a git branch, has an environment-variable/secret panel, a health-check setting, a shell for running the tools, and flat $7 pricing. Alternatives (Railway, Fly.io) would also work; I recommend Render only for simplicity, not as a verified superiority claim.
- Instance: **Starter**, **1 instance** (the app keeps scan state and caches in-process, so do not scale out), region = the Atlas region.
- Runtime Python, `PYTHON_VERSION` 3.12.x. Build: `pip install -r requirements.txt`. Start: `uvicorn index:app --host 0.0.0.0 --port $PORT`.
- Health check path: `/api/health/ready`. **Auto-deploy: off.** Deploy only a dedicated non-production branch, never the production branch.
- Use a **separate Render workspace/project from production** if production is on Render, so no variable group, domain or service is shared.
**MongoDB: Atlas M0 in its own Atlas project** (e.g. `flintel-staging`), same cloud/region as the host, database `flintel_staging`.
- Database user: one new user, `readWrite` on `flintel_staging` only, password generated by Atlas, used by nothing else.
- Network access: prefer the host's published outbound IPs in Atlas "Network Access". If the host cannot give stable IPs, `0.0.0.0/0` is the fallback and is acceptable **only** with that scoped user and a strong password; remove it at teardown.
- Do **not** reuse a production project, user or cluster.

## 4. How to set secrets without exposing them in chat
0. **Never upload or create a `.env` file on the staging host** (not in the app directory or above it). The application loads one silently and the preflight fails if it finds one.
1. Create the value inside the provider (Atlas shows the connection string and password once in its own UI; OpenAI shows the key once; Google shows the client secret in its console). Copy it only from that page.
2. Paste it only into the host's **Environment** tab for the staging service (Render: Dashboard -> your service -> Environment -> Add Environment Variable). Secrets are not shown again after saving; do not put them in `render.yaml`, git, issues or chat.
3. Locally generated values (`SESSION_SECRET_KEY`, fingerprints) are made in your terminal; only the 12-character fingerprints (not the secrets) go into `STAGING_FORBIDDEN_KEY_SHA256_PREFIXES`: `printf '%s' "$PROD_SECRET" | sha256sum | cut -c1-12`.
4. Run the preflight and verifier in the host's own **Shell** tab (Render: service -> Shell; available on paid instances), so values stay on the host. Send me only the resulting `staging_report.json` (counts and timings) if you want me to interpret it.
5. After testing: revoke the OpenAI key, delete the Atlas user, delete the OAuth client or its redirect URI.

## 5. Safe sequence (stop and re-authorize at each gate)
| # | Step | Gate before it |
|---|---|---|
| 1 | Decide the branch to deploy. **The branch is only local; I will not push.** You push a dedicated non-production branch (for example `staging/search-modes`) yourself, and confirm your production host does not auto-deploy other branches | you |
| 2 | Generate `SESSION_SECRET_KEY` and the production-secret fingerprints locally | none |
| 3 | **R2:** create the Atlas project, M0 cluster, scoped user, network rule | authorize R2 |
| 4 | **R5:** create the OpenAI project with a hard budget limit and a key (do **not** enter it into the host yet) | authorize R5 |
| 5 | **R1:** create the Render service from the branch, auto-deploy off, with env vars from section 1 **except** `OPENAI_API_KEY` and the Google pair (and never `RAPIDAPI_*`). With `TRENDING_KEYWORDS_ENABLED=false` no paid call can start; leaving the key out as well is a second, independent safeguard | authorize R1 |
| 6 | First deploy. Expect: startup writes the app's indexes into the empty staging database; `/api/health/ready` returns `ok` | none (writes only to staging) |
| 7 | **R6:** create the Google OAuth client with the staging callback URL; add the Google pair; redeploy | authorize R6 |
| 8 | Run `python3 tools/staging_preflight.py` (isolation phase) in the host Shell. The verdict must be **PASSED** (not FAILED, not INCOMPLETE); it needs no paid credential. **Stop otherwise** | none |
| 9 | **R3 seeding:** `python3 tools/staging_seed.py` (dry run), then `--apply --i-confirm-single-run --confirm-db <MONGODB_DB>` (only one seed run at a time; see operations guide); refuses unless the isolation preflight is PASSED; inserts only into `flintel_signals`; labelled synthetic records; `--embed` is a separate paid flag | authorize R3 |
| 10 | Add `OPENAI_API_KEY`, set `STAGING_OPENAI_LIMIT_CONFIRMED=true` after creating the budget limit, run `staging_preflight.py --phase paid` (must be PASSED); redeploy | authorize spend |
| 11 | `python3 tools/staging_verify.py --allow-index-creation --report r.json --base-url https://<staging>` (the default: no paid call) | none |
| 12 | Same plus `--allow-paid-calls` (about 9 small embedding calls; needs the paid-phase preflight) | authorize spend |
| 13 | Manual credit test with a staging Google account: a handful of real searches in each mode (each makes router + answer LLM calls); read `/api/me/usage` | authorize spend |
| 14 | MySQL probe review only if you chose R4 | authorize R4 |
| 15 | Teardown: suspend or delete the service, revoke keys, delete the Atlas user and cluster | you |

## 6. Everything that writes to a database or can incur charges
| Step | Writes to a database? | Charges? |
|---|---|---|
| Creating provider accounts/resources (3, 4, 5, 7) | no | R1 $7/month; others free until used |
| Any app start (6, 10) | **yes**: creates the app's indexes in every collection (users, chats, credit usage, plans, caches, jobs, signals) on the staging database | host hours |
| Scheduler cycle on first boot with no saved schedule | **yes**: a schedule document and a search job | **LLM web-search call + RapidAPI call if keyed**; repeats every 12 h while running |
| `staging_preflight.py` | no (reads environment only) | no |
| `staging_seed.py` (if approved) | **yes**: inserts seed posts into `flintel_signals` | embeddings if `--embed` (about $0.001) |
| `staging_verify.py` | importing the app creates the indexes above (the flag `--allow-index-creation` is required); retrieval itself writes nothing | no by default; about 9 embedding calls only with `--allow-paid-calls`. Hacker News fetches are free |
| Logging in with Google | **yes**: a user document | no |
| Each search through the UI | **yes**: chat message, credit reservation and commit, a search job (none for Hacker News alone), topic cache | router + answer LLM calls and a query embedding; an estimated cents each (not measured) |
| `LAZY_EMBED_ENABLED` left on with unembedded posts | **yes**: embeddings written back | embeddings per query |
| MySQL probe (optional) | no (SELECT only) | no |

## 7. Remaining risks to accept or decide
- The scheduler is now controlled by the flag; the risk remaining is operator error (leaving it enabled), which the preflight blocks.
- The branch must be pushed by you; I will not push it.
- M0's 100 operations/second cap may slow the concurrency-heavy checks; that is a limit of the free tier, not a defect.
- Seeded data cannot stand in for real corpus coverage; coverage numbers will describe the seed only.
- No staging resource exists yet, so nothing is INTEGRATION VERIFIED.

## 8. Authorization sheet (tick one at a time)
- [ ] R2 MongoDB Atlas M0 (free)
- [ ] R5 OpenAI project, hard limit $____ (spend)
- [ ] R1 Render Starter service, $7/month (spend)
- [ ] R6 Google OAuth client (staging project)
- [ ] R3 seeding with `tools/staging_seed.py` (writes to staging; free placeholder vectors, or paid embeddings about $0.001, estimated)
- [ ] Optional: R4 MySQL, R7 billing test store
- [ ] Branch name for staging: ________ (you push it)

## Addendum: security prerequisites to confirm before authorizing staging (post-review)
Nothing here has been created or run. Before any staging resource is authorized, confirm that the staging environment file sets (values are yours; the preflight checks them without printing secrets):
- [ ] `FLINTEL_ENV=staging`, `SITE_URL=https://<staging host>`, `TRUSTED_HOSTS=<staging host>` (no wildcard, no production host)
- [ ] `ORIGIN_CHECK_ENABLED=true`, `SESSION_COOKIE_SECURE=true`
- [ ] the staging host (and only it) is registered as the Google OAuth redirect URI
- [ ] the reverse proxy passes `Host` through unchanged, appends exactly one `X-Forwarded-For` hop, and limits request body size (the pinned Starlette has form-parsing DoS advisories; see F4)
- [ ] `LOGIN_RATE_LIMIT_ENABLED` left at its default `true`
- [ ] you accept that a Starlette/FastAPI upgrade has **not** been validated outside unit tests and should be a separate staged step
The app will not start in staging without the first five items; `tools/staging_preflight.py` reports them as FAIL beforehand.
