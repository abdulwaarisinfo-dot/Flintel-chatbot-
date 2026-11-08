# FLINTEL Final Independent Release Audit

Audited commit `ebe68fd9538508b7dfcff995ae61e0867d737c64`; fixes are in the commit that adds this file. Method: adversarial checks run against the committed code
(sockets blocked, write-spies on every collection, single-variable deletion/mutation of the preflight environment), not a re-run of the author's own tests.
No production access, no infrastructure, no paid call, no seeding, no push.

## Findings
| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | **Critical** | Preflight reported PASSED for a replica-set connection string that listed a production host **second** (the normal non-SRV Atlas format), or named it in a query parameter, because only `urlsplit().hostname` (the first host) was compared | Fixed: every host in the string and the query are compared (credentials excluded); same rule for `--base-url`. Regression tests added (5 URI shapes x 4 variables, credentials-only match, base-url tricks) |
| 2 | **Critical (spec/cost)** | The verifier ran the paid mode matrix **by default** whenever the paid preflight passed (opt-out `--skip-modes`) | Fixed: opt-in only (`--allow-paid-calls`, plus the paid-phase preflight); default SKIPS the matrix. Test proves the default run never calls retrieval even with every paid credential present |
| 3 | Low | A 40-identical-character `SESSION_SECRET_KEY` passed (length, defaults and reuse were enforced, entropy was not) | **Fixed in the follow-up hardening**: rejects short, default, predictable-word, low-distinct-character, low-entropy, repeated-pattern and sequential secrets; only generic reasons are printed (never the value or its fingerprint) |
| 4 | Medium | Two simultaneous seed runs could duplicate records (no unique index on `message_id`, and the tool must not create one) | **Fixed in the follow-up hardening** by an enforced single-run restriction: `--i-confirm-single-run`, a machine-local exclusive lock (second process exits 2), and a read-only post-run duplicate check (exit 1, nothing deleted) |
| 5 | Info | The seed's `$setOnInsert` upsert (query field also set by the operator) is accepted by mongomock; not verified on a real MongoDB | Staging item |

## Follow-up findings (final engineering verification)
| # | Severity | Finding | Resolution |
|---|---|---|---|
| 6 | **High (silent production fallback)** | `load_dotenv()` in `config.py`/`database.py` loads a `.env` found from the app directory upward and fills every variable absent from the real environment; the preflight read only the real environment, so a leftover `.env` could enable `MONGODB2/4`, RapidAPI, SMTP, the scheduler or the credit kill switch while the preflight said PASSED. Confirmed empirically with python-dotenv | The preflight now FAILS if any `.env` exists from the repository root to the filesystem root (existence only, never read); seed and verifier inherit it; 5 regression tests |
| 7 | **Medium (performance, mine)** | `build_result_coverage()` was evaluated eagerly on every answer finalisation even with the signal report OFF (default): 48-79 ms measured inside a contended finalisation path. Found by the controlled concurrency experiment | Fixed: coverage is built only when the report is enabled (`result_coverage_if_enabled`); behaviour identical; regression tests |

## Verified (evidence)
**1. Scheduler** - real `config.py` + real `scheduler.py` in a subprocess with sockets blocked and recording stubs: with `FLINTEL_ENV=staging` (flag unset, `false`, or junk) and with the flag `false` anywhere: **0 tasks created, 0 LLM calls, 0 Google calls, 0 jobs, 0 database accesses, no timer**. Enabled (`true`, or unset outside staging, including `production` and unset `FLINTEL_ENV`): the original loop starts (1 task, first action is the persisted-schedule read), i.e. unchanged production behaviour. Only literal `true`/`1`/`yes`/`on` enable; junk falls to the default.
**2. Preflight** - deleting each of the 10 inputs one at a time never leaves PASSED (9 FAILED, the optional fingerprint list gives INCOMPLETE); blanking any input never PASSED; a reused production secret is caught in all 13 secret variables; all 5 risky flags in 6 spellings fail; kill switch (`true`, ` TRUE `), `MONGODB2/4`, RapidAPI, SMTP, email backend, scheduler on/junk, lazy embedding `1`/`True`/blank all fail; production host/name rejected in `MONGODB_URI`, `MONGODB3`, `MONGODB_DB`, `MYSQL_HOST`, `MYSQL_READ_DATABASES`; weak/default/short session secrets fail. Skipped required checks give INCOMPLETE, never PASSED.
**3. Seed** - dry run: with sockets blocked and `pymongo.MongoClient` booby-trapped it runs and opens nothing (the preflight module is not even imported). `--apply` against a mongomock database that already held unrelated collections, a real-looking signal, and a record colliding with a seed id: exactly one collection written (`flintel_signals`), exactly one operation type (`update_one` with `$setOnInsert`), 48 inserted, the colliding record kept, other collections byte-identical, 49 labelled synthetic records (`.invalid` URLs, `synthetic_user_*`, `stg-seed-*`), a second run inserts 0 and changes nothing. Refused (exit 2, no connection) for every failing-isolation environment and for a wrong `--confirm-db`.
**4. Verifier** - `--offline` under blocked sockets: exit 5 (never complete), imported none of `database`, `pymongo`, `httpx`, `logics`, `config`, `openai`. The application is never imported without `--allow-index-creation` (tested for every refusal path). Paid calls: off by default (finding 2). Any SKIPPED/UNKNOWN/FAIL makes `complete` false (tested).
**5. Billing and search** - `search_credits.py`, `credit_cost.py`, `billing.py`, `guest_gate.py`, `index_guard.py`, `usage_meter.py`, `turn_finisher.py` are **byte-identical** to the original baseline `ac007f66`; no changed line in `routes.py`/`index.py` mentions credits, reservations, tickets or the credit store (57 changed lines, all about `search_mode`); 21 credit-mode tests, 14 provenance tests, 57 mode tests, 33 matrix tests pass; Combined dedup tests pass.
**6. Git** - baseline `ac007f66` to HEAD: 44 files, +5636/-31; no credential-shaped string in the whole diff; every `config.py` change is additive (no existing default changed); removed lines are signature/wrapper extensions of the already-reviewed Hacker News code; `origin/main` is still `ac007f66`, no push occurred, no CI/deploy configuration exists in the repository.

## Post-review continuation: status (evidence-limited)
| Area | Status |
|---|---|
| Security hardening (OAuth redirect from SITE_URL, TrustedHost, Origin guard, staging startup refusal, login throttle) | IMPLEMENTED, LOCALLY TESTED. TrustedHost and Origin guard are OFF unless configured; production is unchanged. |
| Classifier rules-3 (synthetic accuracy 0.729 on the fresh set) | IMPLEMENTED, LOCALLY TESTED. Not validated on real posts. |
| Discovery routing (held-out recall 62%, 0 false positives on 90 queries) | IMPLEMENTED, LOCALLY TESTED |
| UI accessibility / touch fixes | IMPLEMENTED, LOCALLY TESTED in Chromium with the network blocked; not run against a live server |
| Integration with real MongoDB, Google OAuth, OpenAI, HN API, payments | NOT INTEGRATION VERIFIED |
| Staging | NOT STAGING VERIFIED (nothing created) |
| Production | NOT PRODUCTION READY; NOT PRODUCTION APPROVED |
Open issues: 38 pre-existing failing tests and 3 collection errors (stale tests, see the test report); Starlette 0.38.6 advisories unpatched (severity Medium, availability and Host-derived URLs); no per-form CSRF tokens; `GET /logout`; HN billing decision pending; pricing copy mentions Reddit only.

## Closure pass (HEAD `60e2e24b`): release-readiness assessment
**Not production ready; not approved.** Everything below is LOCALLY TESTED at most. No staging or integration evidence exists.

| Area | Status | Open issue (severity) |
|---|---|---|
| Cross-site request guard (ON by default), POST logout, throttle + store fallback, proxy hops | IMPLEMENTED, LOCALLY TESTED | browsers sending neither Sec-Fetch-Site nor Origin are not covered (Low); wrong `TRUSTED_PROXY_HOPS` misattributes addresses (Medium if misconfigured) |
| OAuth redirect | Safe only when `SITE_URL` is set; otherwise request-derived | **Medium until `SITE_URL`/`TRUSTED_HOSTS` are set in production** |
| Starlette 0.38.6 advisories | Unpatched in the pinned set; candidate set prepared, not adopted | Medium (form-parsing DoS, Host-derived URLs); upgrade needs staged verification |
| Discovery routing | 57% held-out recall, 0 topic leakage (synthetic) | Medium product risk: ~40% of unseen broad questions run as ordinary topic searches |
| Classifier | 0.729 accuracy on the fresh synthetic set | Medium: not validated on real posts |
| UI/UX | accessibility, touch, billing error states, hint clamp done | account settings/onboarding not deeply audited (Low); pricing copy says Reddit only (Low, awaiting your decision) |
| Test suite | 38 pre-existing failures + 3 collection errors, identical to `origin/main` | Medium: stale tests hide real regressions; two intermittent tests unexplained (Low-Medium) |
| Real integrations (MongoDB, Google OAuth, OpenAI, HN API, payments, proxy) | NOT INTEGRATION VERIFIED | blocker for any claim above LOCALLY TESTED |
| Performance | no new measurements this pass; earlier local benchmarks only | unknown under production load |
