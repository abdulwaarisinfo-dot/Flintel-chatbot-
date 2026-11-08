# FLINTEL Test Report

Command (identical on both sides, pinned venv, Python 3.12):
`python -m pytest tests -q -p no:cacheprovider -W ignore --continue-on-collection-errors -rfE`

| Run | passed | failed | skipped | xfailed | collection errors |
|---|---|---|---|---|---|
| Baseline: clean `origin/main` `ac007f66` | 5022 | 40 | 81 | 10 | 3 |
| First milestone `460ac8a9` | 5182 | 40 | 81 | 10 | 3 |
| **This hardening pass (final run)** | **5304** | **40** | **81** | **10** | **3** |

The sorted list of the 43 failing/erroring test IDs is **identical** to the baseline (empty diff): no new failure, none fixed. Net +282 passing versus baseline.

An intermediate full run showed 3 failures (my own `test_coverage_mysql` probe-on tests; 43 failed). They passed in isolation and failed only in the full order, because other test modules replace `mysql_signals`; the tests now resolve the module at call time. The final run above confirms the fix. Nothing was weakened, and one earlier assertion was made more specific (`test_a_probe_failure_is_a_failure_not_an_empty_result` now expects `DATABASE_UNAVAILABLE` instead of `COLLECTION_FAILED`, per the requirement to distinguish them).

## Staging-preparation pass (commit after `30785f50`)
Only tests, tools and documents were added (no application code). Full run: **41 failed, 5321 passed, 81 skipped, 10 xfailed, 3 errors.** The failing IDs equal the baseline's 43 plus **one timing-flaky test**, `test_nonblocking_chat_page.py::test_concurrent_page_views_and_workers_process_the_turn_once`, which asserts that the slowest of 8 concurrent page views takes under 1.0 s. Investigated in the pre-staging review with an **interleaved A/B** (25 alternating runs per tree, so machine drift cancels): the asserted metric (slowest of 8 views) averages **0.877 s on both** baseline and branch (z = -0.01); medians 0.890 vs 0.885; failures 1/25 vs 2/25. The earlier "0/20 vs 4/20" and "3% slower" figures came from sequential batches and were drift, not a code effect. The test's own spread (sigma about 0.095 s on a 1.0 s limit with a 0.877 s mean) predicts roughly a 1-in-10 failure rate on this machine on either tree. A single serial page view costs about 1 ms, and the branch adds no database call, lock or computation to that path. The assertion was not changed. Remaining risk: the test is timing-borderline by construction (a simulated 0.8 s scan against a 1.0 s limit under 12 contending threads) and may fail intermittently on any tree.
New tests this pass: `tests/test_staging_tools.py` (13) and two Hacker News-only billing tests in `tests/test_search_mode_credits.py` (file now 21).

## Pre-staging review pass: final full run
**40 failed, 5337 passed, 81 skipped, 10 xfailed, 3 errors** (1120 s). The failing/erroring IDs are identical to the baseline's 43 (empty diff); the timing-borderline test passed in this run. `tests/test_staging_tools.py` now has 28 tests.

## Master-directive pass (final run)
**40 failed, 5406 passed, 81 skipped, 10 xfailed, 3 errors** (1144 s) versus baseline 40 / 5022 / 81 / 10 / 3. The 43 failing/erroring IDs are identical to the baseline (empty diff); the timing-borderline concurrency test passed this run, its assertion unchanged. New files this pass: `test_trending_flag.py` (22), `test_staging_seed.py` (26), and `test_staging_tools.py` rewritten for the three-state preflight (49). Only `config.py` and `scheduler.py` changed in application code (30 lines).

## Final independent release audit (this pass)
Full run on the audited tree: **41 failed, 5428 passed, 81 skipped, 10 xfailed, 3 errors**. The 43 baseline IDs are all still the only baseline failures; the one extra failure is `test_cancel_confirmation.py::test_after_an_unconfirmed_stop_a_reload_shows_the_real_state_without_restarting_the_run`, a real-browser test with a 15 s wait. It passes 15/15 in isolation on **both** the baseline and this branch, it passed in every earlier full run, and **no application or template file changed** between the last passing full run (`ebe68fd9`, 40 failed) and this one (only tools, tests and documents), so it is an environmental timing flake, not a regression. The known concurrency test passed this run (interleaved reruns: baseline failed 1 of 10, this branch 0 of 10, again showing it is flaky on both trees). Treat both as intermittent: a clean run is 40 failed / 3 errors with the identical 43 IDs. Relevant new test files alone: `test_staging_tools.py` 72, `test_staging_seed.py` 26, `test_trending_flag.py` 22.

## Final staging readiness hardening (this pass)
Full run: **40 failed, 5470 passed, 81 skipped, 10 xfailed, 3 errors** (1142 s) versus baseline 40 / 5022 / 81 / 10 / 3; the 43 failing/erroring IDs are identical to the baseline (empty diff) and both intermittent tests passed. Targeted files alone: `test_staging_tools.py` 105, `test_staging_seed.py` 34, `test_trending_flag.py` 22, `test_search_modes.py` 57, `test_provenance.py` 14, `test_search_mode_credits.py` 21, `test_coverage_mysql.py` 18, `test_mode_matrix.py` 33, `test_intel_reaudit.py` 27, `test_search_mode_ui.py` 5. Changed: only `tools/`, tests and documents (no application file).

## Controlled concurrency experiment (final engineering verification)
Test `test_concurrent_page_views_and_workers_process_the_turn_once` (assertion unchanged: slowest of 8 views < 1.0 s). Method: same machine and virtualenv, 60 runs per tree per experiment, trees interleaved with the order alternating each iteration, load average recorded (no correlation with the result, r = -0.06). Baseline = clean `origin/main` copy.
| Experiment | Tree | Failures | Slowest view mean / median / max (s) | Mean of views (s) | Wall per run (s) |
|---|---|---|---|---|---|
| 1, before the fix | baseline | 2/60 | 0.862 / 0.864 / 1.093 | 0.741 | 3.33 |
| 1, before the fix | branch | **9/60** | 0.883 / 0.902 / 1.101 | **0.794** | 3.22 |
| 2, after the fix | baseline | 0/60 | 0.807 / 0.830 / 0.953 | 0.693 | 3.19 |
| 2, after the fix | branch | **0/60** | 0.812 / 0.834 / 0.978 | 0.726 | 3.21 |
Experiment 1: failures 2 vs 9 (Fisher exact p = 0.053) and mean-of-views +0.054 s (z = +2.23): a plausible real effect, so it was investigated rather than dismissed. **Cause found:** `build_result_coverage()` (added for the signal report) was evaluated eagerly as an argument on every answer finalisation even with the report OFF (the default); instrumenting the contended finalisation path measured it at **48-79 ms** per answer. Page rendering was ruled out (0.92 ms vs 1.12 ms per render, +12 KB page; too small), `view_chat` is untouched, and no credit/lock/database call was added. **Fix:** `result_coverage_if_enabled()` builds it only when the report is enabled (identical behaviour; 4 regression tests).
Experiment 2: the asserted metric differs by +0.006 s (z = +0.30) and failures are 0/0. Mean-of-views still leans +0.033 s on the branch (z = +1.47, not significant); remaining contributors are plausibly the slightly larger page and small hooks, but this is not proven and not claimed to be zero. Absolute numbers drifted between the two experiments (the baseline itself went 0.862 to 0.807), so only within-experiment comparisons are valid. The test is still timing-borderline by construction (0.8 s simulated scan against a 1.0 s limit with 12 threads) and failed on the baseline too (2/60 in experiment 1, 1/10 earlier).

## Final engineering verification: full suite
**40 failed, 5479 passed, 81 skipped, 10 xfailed, 3 errors** (1104 s) versus baseline 40 / 5022 / 81 / 10 / 3; the 43 failing/erroring IDs are identical to the baseline.

## Master completion mission (final run)
**40 failed, 5577 passed, 81 skipped, 10 xfailed, 3 errors** (1082 s) versus baseline 40 / 5022 / 81 / 10 / 3; the 43 failing/erroring IDs are identical to the baseline (empty diff). New this pass: `test_classifier_evaluation.py` (10), `test_discovery_routing.py` (47), `test_zero_result_modes.py` (16), `test_user_journeys.py` (8, journeys A-F, offline), `test_ui_answer_blocks.py` (8, real browser, both templates, phone and desktop), `test_benchmarks.py` (2); `test_staging_tools.py` now 117 (session-cookie rules). Existing assertions changed: classifier version string (rules-3); a failure-plus-absence zero-result combination is now `PARTIAL_RETRIEVAL` instead of `MIXED` (refinement, `MIXED` retained for two absences); provenance tests count card chips (`.post-block .prov-tag`) because the mode note now has its own chip. No threshold or timeout was changed. Billing and credit files are byte-identical to `origin/main`.

## Independent runs (each file alone)
| File | Result |
|---|---|
| `tests/test_search_modes.py` | 57 passed (48 from the first milestone + 9 added) |
| `tests/test_search_mode_ui.py` (browser) | 5 passed (4 original + hint test) |
| `tests/test_provenance.py` | 14 passed |
| `tests/test_search_mode_credits.py` | 16 passed |
| `tests/test_coverage_mysql.py` | 18 passed |
| `tests/test_mode_matrix.py` | 33 passed |
| `tests/test_intel_reaudit.py` | 27 passed |

## The 43 remaining baseline failures (all pre-existing, none touched)
| File | Count | Cause found | Safely fixable in scope? |
|---|---|---|---|
| `test_keepalive.py` | 9 | endpoint returns 404 (`assert 404 == 200`) | not investigated further; unrelated |
| `test_realtime_streaming.py` | 8 | unrelated streaming expectations | no |
| `test_active_intent_frontend.py` | 8 | assert the Live Reddit block that the "historical-only" decision removed | no: fixing means reversing a product decision or rewriting tests |
| `test_keyword_chips.py` | 5 | unrelated template expectations | no |
| `test_reddit_live_endpoint.py` | 3 | legacy Live Reddit endpoint, disabled at source | no (same reason) |
| `test_historical_only.py` | 2 | expects no Live block; legacy block still rendered | no |
| `test_favicon.py`, `test_seo_basics.py` | 2 | `ModuleNotFoundError: PIL` (Pillow not installed in this environment) | environment only; installing a package was out of scope |
| `test_turn_queue_limits.py`, `test_smalltalk_fast_path.py`, `test_platform_multiselect.py` | 3 | unrelated assertions (queue limit, a reply string, a CSS rule) | no |
| collection errors: `test_option_a_guard.py`, `test_option_a_followup.py`, `test_lead_qualification.py` | 3 | `No module named 'tests.test_option_a_outreach'` (a test module missing from the repository) | no: needs the missing file from its owner |

No unrelated tests were modified to improve the numbers.

## Coverage of the requested areas
| Area | Tests |
|---|---|
| Modes, labels, default, invalid/hostile input | `test_search_modes.py` |
| Live never simulated; Historical never calls live; Combined merge, partial failure | `test_search_modes.py`, `test_mode_matrix.py` |
| Mode x (general discovery / topic / date window / empty / duplicates / partial failure) x (Reddit / HN / both) | `test_mode_matrix.py` |
| Dedup across URL forms; metadata not lost; no double count | `test_search_modes.py`, `test_provenance.py`, `test_mode_matrix.py` |
| Per-result provenance through retrieval, dedup, cache, ranking, classification, answer JSON, browser rendering (mobile and desktop, both templates) | `test_provenance.py` |
| Unsupported source/mode combinations and their explanation (server note and UI hint) | `test_search_modes.py`, `test_search_mode_ui.py` |
| Credits: same cost in every mode, once per search, retries, exhausted owner, hostile mode values | `test_search_mode_credits.py` (on top of the existing credit suites) |
| Zero-result: nothing matched / nothing collected / source unavailable / collection failure / database unavailable / unknown (MySQL) / filters; MySQL probe on a fake connection | `test_coverage_mysql.py`, `test_general_discovery.py` |
| Classification: five classes, the assignment's examples, negation, quotes, separate scores, path-independence | `test_intel_reaudit.py`, `test_signal_intel.py` |

## Not tested / not verified
- Real Hacker News Algolia, OpenAI, MongoDB, MySQL: no credentials or network. All live and database behaviour uses labelled fakes or mongomock. **NOT VERIFIED** against real services (not INTEGRATION VERIFIED).
- The MySQL probe SQL has never run against a real MySQL server; index use on the publication column is unverified, hence default off.
- Credit behaviour for a failed or cached search is the existing policy, covered by the existing credit suites; no new policy was added or tested beyond mode-independence.
- Classification accuracy on real discussions: not measured.
- Load/concurrency: none run; no scalability claim.

## Post-review baseline reconciliation (recorded 2026-11-05)
The original recorded baseline (24 failures + 3 collection errors) has no surviving log, so it cannot be reproduced or audited. Established independently instead:

| Run | Environment | Result |
|---|---|---|
| Full suite on commit `20dbfaf5`+review changes | Python 3.13.16, Starlette 1.6.0, FastAPI 0.143.3, pytest 9.1.1 | 39 failed, 5624 passed, 81 skipped, 10 xfailed, 3 collection errors |
| Same 39 test IDs on a clean worktree of `20dbfaf5`, `51737b65`, `b3bf27ad`, `origin/main` | pinned venv (Python 3.12.3, Starlette 0.38.6, FastAPI 0.115.2) | **38 fail on every one of them**; 1 passes |
| Full suite, working tree during this pass | pinned venv as above | 40 failed, 5642 passed, 81 skipped, 10 xfailed, 3 collection errors |

- **38 failures are pre-existing and environment-independent** (identical on both dependency sets and on `origin/main`). Causes seen: tests asserting code that no longer exists (`KEEPALIVE_ENABLED`, `analyze_with_claude_stream`, the streaming helper functions, "Live Reddit" labels), one expected text/CSS string that moved, a router smalltalk case (`salaam`), a turn-queue backfill bound, and `asyncio.run()` inside a running loop. They are stale tests or drifted behaviour, not something this branch's last commits introduced. They were not "fixed" by editing assertions.
- **Why 38 and not 24: unknown.** Not explained by Python/Starlette/FastAPI versions. Possible: the earlier baseline measured a different tree or selection. Do not treat 24 as established.
- **Pinned venv extras:** `test_favicon` and `test_seo_basics::test_og_card...` fail there only because Pillow is not installed in that venv (`ModuleNotFoundError: PIL`); both pass in the main environment (46 passed).
- **Concurrent page-view test** (`test_nonblocking_chat_page::test_concurrent_page_views_and_workers_process_the_turn_once`): failed once in the first full run (`max(times)` 1.75 s vs a 1.0 s bound, while other CPU-heavy work ran), passed in the pinned full run and 6/6 when run alone. Treated as a load-sensitive timing assertion; assertion and timeout untouched. Order dependence was not demonstrated.
- **3 collection errors** (`test_lead_qualification`, `test_option_a_followup`, `test_option_a_guard`): `ModuleNotFoundError: tests.test_option_a_outreach` (a missing test module); pre-existing.
- **Added in this pass, all passing:** `test_origin_guard` (17), `test_login_throttle_and_oauth` (19), `test_ui_accessibility` (19), additions to routing/evaluation tests.

## Final full run on commit `d02d70ab` (Python 3.13.16, Starlette 1.6.0)
40 failed, 5664 passed, 81 skipped, 10 xfailed, 3 collection errors (18m26s). Against the earlier run of the same environment (39 failed):
- **38 pre-existing** failures: unchanged (listed above).
- **`test_ui_accessibility::test_search_mode_is_a_keyboard_operable_radio_group[index.html]`**: a defect in the NEW test, not the app: it read the hint text before the page's `setTimeout(hint, 0)` ran. Fixed by waiting for the hint (`wait_for_function`); the assertion itself is unchanged; 5/5 passes afterwards.
- **`test_active_intent::test_finish_is_idempotent_and_never_makes_a_second_paid_call`** (`KeyError: 'live_candidates'`): failed once in the full run, **not** in the two earlier full runs, and passes in isolation (8/8 whole-file runs, and after the new test files). The cause is **not identified**; the module is untouched by this branch's diffs since `20dbfaf5`. Treat as an unexplained intermittent failure (order or time dependent), to be investigated by running the suite repeatedly; no assertion was changed.
- `test_concurrent_page_views...` passed in this run (timing-sensitive; see above).

## Final full run on commit `60e2e24b` (closure pass; Python 3.13.16, Starlette 1.6.0, FastAPI 0.143.3)
**38 failed, 5720 passed, 81 skipped, 10 xfailed, 3 collection errors (16m50s).** The failing IDs are **identical** to the 38 pre-existing failures reproduced on `origin/main` and earlier commits, plus the same 3 collection errors (`tests/test_lead_qualification.py`, `test_option_a_followup.py`, `test_option_a_guard.py`: missing module `tests.test_option_a_outreach`). **No new failures; no failure fixed; no assertion, timeout or skip changed.**
- The two intermittent failures seen in earlier runs (`test_nonblocking_chat_page::test_concurrent_page_views...` timing under load, and `test_active_intent::test_finish_is_idempotent...`) did **not** occur in this run. The `test_active_intent` cause remains **unidentified**: 172 isolated runs spanning minute boundaries, 12 runs with its preceding files, and 8 whole-file runs all passed. Treat both as intermittent until a repeated-run study explains them.
- The pinned-dependency venv (Python 3.12, Starlette 0.38.6) was last run before the closure commits (40 failed = the 38 + 2 caused only by Pillow missing in that venv).
- New tests in this pass: Fetch Metadata guard, logout, proxy hops, throttle store-failure fallback, routing sets v5/v6, router-keyword safety net (stubbed router), question-not-demand, billing failure states, search-mode hint.
- Unchanged by design: `billing.py` and billing policy (diff against `20dbfaf5` empty).
