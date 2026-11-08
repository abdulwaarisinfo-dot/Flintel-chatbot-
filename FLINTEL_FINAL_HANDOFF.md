# FLINTEL Final Handoff (hardening pass)

**Status: IMPLEMENTED, LOCALLY TESTED.** Not INTEGRATION VERIFIED, not STAGING VERIFIED, not PRODUCTION APPROVED.

- Branch `feature/search-modes-universal-intelligence`, based on `origin/main` `ac007f66`. First milestone commit `460ac8a9`; the hardening commit hash is in the chat report.
- Nothing was pushed or deployed; no production database, host, variable, schedule, price or plan was touched.

## Completion criteria
| # | Criterion | Evidence |
|---|---|---|
| 1 | All three modes work at code level | `test_search_modes.py`, `test_mode_matrix.py` (fakes for the two paths) |
| 2 | General discovery in all modes | matrix tests + `test_general_discovery.py` |
| 3 | Per-result provenance preserved and displayed | `test_provenance.py` (incl. browser, mobile + desktop) |
| 4 | Deduplication tested | URL-form, metadata-fill, no-double-count tests |
| 5 | Billing verified against the existing policy | `test_search_mode_credits.py`; one ambiguity reported (HN-only search charged with no job) |
| 6 | Coverage explanations accurate | `test_coverage_mysql.py`; MySQL probe default off, unknown never claimed as empty |
| 7 | No new test regressions | final full run: identical 43 failing IDs to baseline |
| 8 | UI behaviour tested | `test_search_mode_ui.py`, provenance rendering tests |
| 9 | Documentation matches the code | six `FLINTEL_*.md` updated in this pass |
| 10 | Production untouched | yes |

## What changed in this pass
Per-card LIVE / HISTORICAL / BOTH labels from real retrieval metadata; dedup no longer discards metadata; source capability matrix (Reddit has no live path: the legacy GPT web search is disabled by a product decision and was not re-enabled); unsupported-source explanations (server note + UI hint); a `partial` bug fixed (an unavailable path is not a failed path); billing audit and tests; zero-result coverage (new `DATABASE_UNAVAILABLE`, guarded MySQL probe, no false "none in range"); classifier `rules-2` after the re-audit found misclassified examples; test fixes for Playwright ordering.

## Known limitations / blockers
1. No real-service verification (Algolia, OpenAI, MongoDB, MySQL).
2. Reddit has no live source; a real one needs an approved retrieval API, which was not added.
3. Live Hacker News is stories only (no comments); stored HN is keyword-gated by the external collector.
4. MySQL probe unverified against a real server and off by default.
5. Classifier is rule-based English; accuracy unmeasured.
6. 43 pre-existing failures/errors remain (see the test report): missing Pillow, a missing test module, and tests that contradict the historical-only decision.
7. Product question: should a Hacker News-only search (no collector job) cost the same credits?
8. No load test; no scalability claim.

## Staging
See `FLINTEL_STAGING_CHECKLIST.md`: a separate host, MongoDB, optional MySQL, low-cap API keys, billing test mode, outbound access to the Hacker News API. None exists yet.

## Staging phase update
No staging resources or credentials existed, and the sandbox cannot reach Algolia or OpenAI, so **no real integration was verified** and the status remains IMPLEMENTED, LOCALLY TESTED. Prepared instead: `FLINTEL_STAGING_SETUP.md` (exact resource list, start command, health check, isolation gate, MySQL probe review, Hacker News-only billing finding), `tools/staging_preflight.py`, `tools/staging_verify.py`, `.env.staging.example`, and 15 new offline tests. Blocked on you: authorisation/credentials for items 1-9 in section 1 of the runbook.
