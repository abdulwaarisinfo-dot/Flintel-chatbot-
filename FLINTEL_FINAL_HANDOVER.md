# FLINTEL — Final Handover (master execution)

Branch `work/final-master-execution`, created from `origin/main` at `6f0a95c` (the repository state after the owner's manual upload of the previous
phase). Nothing was pushed, deployed, or run against production, a real database, or a paid service. The whole session used synthetic records, simulated
model calls and a blocked-network Chromium. **Nothing here is integration verified, staging verified, production ready or production approved.**

## A. Executive summary
What was actually improved (each item has a test and a root cause below):
1. **Platform attribution** was wrong at four independent layers; it now follows the URL host everywhere (retrieval pool, integrity layer, signal report,
   page renderer) and a post the model filed under the wrong platform is regrouped by the server.
2. **The report no longer contradicts itself**: the signal report carries the checked report's review status for every retrieved post, one accounting block
   adds up, a vendor/testimonial row can never be "direct demand", and `intent_strength` now means the author's intent (it used to be pattern strength, so a
   Show HN announcement scored 0.95).
3. **Topic relevance** for queries about AI agents was broken ("ai", "agent" were stop words, so an on-topic post scored 0.0).
4. **Performance**: the dominant cost of a long search (one model request per batch of 12 posts, strictly one after another) now runs with bounded
   concurrency, same number of requests, results in the same order, one retry, and a visible "may be incomplete" marker for batches that still fail.
   Measured locally: scheduling speedup 4.0x for 84 simulated batches; **the real end-to-end latency was not measured (no model access).**
5. **Repository defects introduced by the manual upload** were repaired: `templates/_touch_targets.html` held a copy of the search-mode control (duplicate
   element ids on two pages); the evaluation datasets and `FLINTEL_*.md` documents were missing (two test files could not even be collected).

What remains broken or unverified (see I): real-model query understanding, real retrieval coverage and relevance on real data, the integrity layer's
any-term relevance gate, HN comments are not searched, 38 pre-existing failing tests, Starlette advisories, and every external integration.

## B. Bugs fixed (root cause → fix → verification)
| # | Issue observed | Root cause (verified in code) | Fix | Tests |
|---|---|---|---|---|
| 1 | Hacker News URL labelled Reddit; Show HN labelled X/Twitter | (a) retrieval pools used the stored `platform` label and consulted the URL only when the label was empty (`logics.py`); (b) `platforms.of` and `answer_integrity.platform_of` trusted the stored label; (c) the model's group name (`platforms[].platform`) was kept even when its posts belonged elsewhere; (d) the page used `s.includes('x')` for X and had no Hacker News entry, so HN fell to "Web" | URL host wins (`platforms.url_platform`, `_resolve_platform`, `platform_of`); conflicts kept visible (`raw_platform`, `platform_conflict`); posts regrouped by real platform (`platform_corrections`); exact platform matching and HN icon/name/labels in both templates; `Source` cell never blank | `tests/test_platform_truth.py` (24), `test_platform_labels_ui.py`, `test_platform_rendering_e2e.py` (real browser, both templates) |
| 2 | Summary and classification disagree; "strong signal" while intent "none" | The signal report classified **all retrieved posts** while the checked report shows a subset; two classifiers, two stories; `intent_strength` was the matched pattern's confidence | Every signal item carries the checked report's `review` (in_report / excluded / not_selected / not_analyzed); `accounting` buckets add up; statement states how many appear in the checked report; a checked-report vendor/testimonial role downgrades `DIRECT_DEMAND` to `COMPETITOR_SIGNAL`; `intent_strength` is 0 for non-buyers, ≤0.5 for problems | `tests/test_report_consistency.py` (10) |
| 3 | Report claims seven posts, five shown | Counts came from different sets (retrieved vs listed vs shown); the checked report already reconciles its own counts (existing tests) — the gap was the signal report | One accounting block (above). The existing integrity layer already rewrites a model-written count to the displayed count (verified with a direct run: "seven" → "2"); I could **not** reproduce the original report (its records are not in the repository) | `test_report_consistency.py`, existing `test_answer_integrity.py` (166 pass) |
| 4 | Topic relevance 0.0 for on-topic posts | `_STOP` in `signal_intel.py` contained "ai", "agent", "agents" | Removed; use-case relevance credits word pairs ≤2 apart | `test_report_consistency.py::test_topic_relevance_*` |
| 5 | Search takes ~3 min | Map step: one model request per 12 posts, strictly sequential (`logics.analyze_with_claude`); deterministic stages measure < 0.4 s per 1000 posts | `_concurrent_calls`, `CLAUDE_MAP_CONCURRENCY` (default 4; 1 = old behaviour), order preserved, one retry, `analysis_coverage` marker + page note | `tests/test_map_concurrency.py` (8), `test_partial_note_ui.py` |
| 6 | Partial results presented as complete | Failed batches were skipped silently | `analysis_coverage` in the answer + "this analysis may be incomplete" note; all-batches-failed stays a `no_results` answer | same |
| 7 | Hacker News described as "discussions" | The integration fetches **stories only** (`tags=story`; most link stories dropped; Ask HN / Show HN / text posts kept) | Live hint and coverage note say "Hacker News stories … comments are not searched" | `test_search_mode_ui.py`, `test_search_modes.py` |
| 8 | Duplicate element ids for the mode control | Upload put the control in `_touch_targets.html` as well | Restored the partial | `test_ui_accessibility.py` |
| 9 | Documentation/evaluation missing from `main` | Not part of the manual upload | Restored from the previous phase's commit `f7a79ea7` (identical content) | collection of `test_classifier_evaluation.py` |

## C. Search quality
- **Query understanding**: unchanged model router (not testable without a key). Deterministic part checked on the product-vision examples
  (`tests/test_query_understanding_examples.py`): topic requests are never captured as broad discovery; broad ones are recognised in 3 of 5; the 2 misses
  ("…evaluating, comparing, or seeking solutions", "…emerging problems, market discussions…") are documented, safe, and were not tuned because the routing
  held-out set is already used.
- **Retrieval**: the pools now carry the true platform; HN retrieval limits (story-only, text-required, window re-verification) are as before.
  **Not changed and not verified on real data**: recall/precision of retrieval, the integrity layer's `product_terms` (any-term relevance: for the query
  "AI SaaS agent" the only term is "agent", so "real-estate agent" can pass the gate — a known precision risk I did not change blindly).
- **Classification**: rules-3 class decisions unchanged. New probe set `classification_eval_v4_platformfix.jsonl` (35 synthetic items, same author as the rules,
  evaluated once, no tuning): accuracy 0.943 (CI 0.81–0.98), macro-F1 0.95, false-buyer rate 0/27. Honest estimate remains v3: 0.729 (CI 0.59–0.83).
  Synthetic data; not real-world accuracy.
- **Separate dimensions** (topic, use case, intent, commercial, evidence confidence) were already separate; the page now shows intent/commercial only for
  demand and problem classes, and in words.
- **Evidence**: every quote is verified as an exact substring (existing check, tested); a promotion is never given an opportunity record.

## D. Performance (measured)
| Stage | Measurement | Where |
|---|---|---|
| `build_signal_report` (classifies 25) over a 1000-post pool | 0.04 s | local script |
| classify all 1000 posts | 1.4 s | local script |
| `enforce_answer_integrity`, 25 listed over 1000 retrieved | 0.33 s (fingerprinting dominates) | local script |
| `search_modes.merge` of 1000 posts | 0.014 s | local script |
| Map step scheduling, 84 simulated 20 ms batches | 1.69 s sequential → 0.43 s at concurrency 4 (**4.0x**) | `tests/test_map_concurrency.py` |
**Not measured**: real model latency, Algolia/MongoDB latency, end-to-end search time. The "about one minute for 1000 posts" target is **not claimed**;
the structural bottleneck was removed, the number must be measured against the real model.

## E. Frontend
Fixed: platform headers/icons/names for Hacker News and unknown platforms, per-post Platform row, report-table Source cell, signal-card review line and
banded intent/commercial/confidence, "may be incomplete" note, duplicated mode control. Earlier phase (already in the code): keyboard radio group, touch
hit areas, billing error states. Not audited again: account settings, onboarding (see I).

## F. Security
No new security code in this pass; the previous phase's work is on `main` and its tests pass (origin guard, login throttle, OAuth redirect from
`SITE_URL`, staging startup check). Still open, unchanged: Starlette 0.38.6 advisories (candidate upgrade `requirements-upgrade-candidate.txt` never run
in staging), OAuth `redirect_uri` is request-derived without `SITE_URL`, no per-form CSRF tokens, `GET /logout` for clients without Fetch Metadata. See
`FLINTEL_SECURITY_FINDINGS.md`. A secret scan of the diff found nothing.

## G. Tests
Commands (all local, no network, no key): `python3 -m pytest -q -p no:cacheprovider --no-header --continue-on-collection-errors -rfE` (Python 3.13.16,
Starlette 1.6.0, FastAPI 0.143.3; about 18 minutes).

| Run | Tree | Result |
|---|---|---|
| Clean baseline | `origin/main` `6f0a95c` (worktree) | **52 failed, 5673 passed, 81 skipped, 10 xfailed, 5 collection errors** |
| Previously established baseline | the previous phase's tree `f7a79ea7` | 38 failed + 3 collection errors (same IDs as below) |
| First branch run | commit `4e6206a`.. (before the regression fixes) | 43 failed, 5778 passed, 3 errors: **5 new failures, all caused by this branch** |
| Second branch run | after the fixes (`1f5e801`) | 38 failed, 5783 passed, 81 skipped, 10 xfailed, 3 errors |
| **Final run on the last code commit `da69e8c`** | `work/final-master-execution` | **38 failed, 5795 passed, 81 skipped, 10 xfailed, 3 collection errors (1093 s)** |

**Comparison of failing IDs (final run, 41 IDs = 38 failures + 3 errors):** identical to the previously established 38+3 set (empty diff both ways);
no ID is new relative to the clean `origin/main` run; 16 IDs that fail on `origin/main` pass on this branch (the 8 routing tests and 2 collection errors
that needed the omitted `evaluation/` files, the 4 `test_ui_accessibility` and 2 `test_home_ui` failures caused by the duplicated mode control).
**None of the 41 pre-existing IDs is fixed or hidden.**

The 41 pre-existing IDs by file: test_keepalive 9 (feature removed from the code, tests remain: stale), test_realtime_streaming 8 (helpers no longer in
`routes.py`: stale/drifted), test_active_intent_frontend 8 + test_historical_only 2 + test_reddit_live_endpoint 3 (**tests ahead of the code**: they expect
"Previously saved Reddit results" wording the templates never implemented; a product decision), test_keyword_chips 5, test_turn_queue_limits 1,
test_smalltalk_fast_path 1, test_platform_multiselect 1, and 3 collection errors (`tests.test_option_a_outreach` module missing from the repository).
Environment note: `test_favicon`/`test_seo_basics` pass here because Pillow is installed; without it 2 more fail. Intermittent in earlier phases and not
explained: `test_active_intent::test_finish_is_idempotent...` and a 1.0 s timing assertion in `test_nonblocking_chat_page`; both passed in all runs above.

**The five regression failures this branch introduced (4 causes; found by the first branch run) and how they were resolved**
1. `test_active_intent_frontend::test_both_templates_render_the_block_right_after_the_freshness_note` x2: my note call sat inside an asserted call order; moved ahead of it.
2. `test_search_to_report_flow::test_cards_match_the_report`: I had added `role` inside the review dict that the test compares exactly; the role now travels as a separate `report_role` field.
3. `test_phase22_platform_registry::test_a_stored_platform_value_still_wins_over_the_url`: asserted the OLD precedence (stored label over URL host), which is the very bug fixed. **Rewritten, not weakened**, with the reason in the docstring and the stored-label-when-URL-unknown case still asserted.
4. `test_request_kinds::test_literal_matcher_honours_platform_window_limit_and_dedupe`: the fixture modelled an X post with a reddit.com URL (an impossible record under host-decides); the fixture now carries an X URL, assertion unchanged.
All five were re-verified by the **full** final run (not only the targeted run). No assertion was loosened to obtain them.

Focused suites run on the final tree: platform truth/labels/rendering (`test_platform_truth`, `test_platform_labels_ui`, `test_platform_rendering_e2e`),
report consistency (`test_report_consistency`, 13), concurrency (`test_map_concurrency`, 13; `test_partial_note_ui`), classifier (`test_signal_intel`,
`test_intel_reaudit`, `test_classifier_evaluation`, `test_user_journeys`), query understanding, all three search modes (`test_search_modes`,
`test_mode_matrix`, `test_provenance`, `test_search_mode_credits`, `test_zero_result_modes`: 146 pass), accessibility/frontend (`test_ui_accessibility`,
`test_search_mode_ui`, `test_home_ui`, `test_ui_states`, `test_ui_answer_blocks`: 151 pass together), answer integrity (166 pass).
Regression coverage requested: incorrect platform labels, platform grouping, vendor promotion vs direct demand, author intent strength, topic relevance,
consistent counts, source URL/title integrity, missing/partial/empty results, batch failures and retries, all three modes, duplicated mode control: **all present** (files above).
Application start: with an **in-memory mock database only** (clearly not a real database), the app imports and serves `/`, `/discover`, `/billing/upgrade`,
`/find-buyers-on-reddit`, `/robots.txt`, `/sitemap.xml` with HTTP 200. That shows pages load, not that the product works.

### G2. Review of the bounded-concurrency map step (`logics._concurrent_calls`)
Verified by reading and by tests: at most `CLAUDE_MAP_CONCURRENCY` (default 4, clamp 1-16) calls in flight; results returned in input order; a call that raises
is retried exactly once, sequentially, after the parallel pass (so 12 calls for 6 persistent failures, never more); only `Exception` is contained;
failed-twice batches are skipped, counted, and reported as `analysis_coverage` (map and condense batches both counted, `failed_batches <= batches`);
notes from healthy batches still reach the final reduce and nothing is invented for a failed one; all batches failing still yields a `no_results` answer;
the caller's contextvars (e.g. usage attribution) reach the workers; `CLAUDE_MAP_CONCURRENCY=1` is the previous sequential behaviour (without the retry).
Risks not measurable here: parallel requests may hit provider rate limits that sequential requests did not (mitigated by the single retry, unmeasured); a
failing provider costs one extra request per batch; billing/credit code was not changed and I did not verify credit accounting under concurrency against
real usage. **The speedup (4.0x) is a scheduling measurement with simulated 20 ms calls; no real-world latency improvement is claimed.**

## H. Changed files (`origin/main` → `work/final-master-execution`; no file deleted or renamed by this branch)
36 added, 15 modified, 0 deleted, 0 renamed (51 files). Code/config: `answer_integrity.py`, `config.py`, `logics.py`, `platforms.py`, `search_modes.py`, `signal_intel.py`, `requirements.txt` (comment only), templates `chat.html`, `index.html`, `_search_mode.html`, `_touch_targets.html`. New tests: `test_platform_truth`, `test_platform_labels_ui`, `test_platform_rendering_e2e`, `test_report_consistency`, `test_map_concurrency`, `test_partial_note_ui`, `test_query_understanding_examples`; changed tests: `test_classifier_evaluation` (added), `test_phase22_platform_registry` (intentional precedence change), `test_request_kinds` (fixture), `test_ui_accessibility` (added). Restored unchanged from the previous phase's commit `f7a79ea7` because the manual upload omitted them: `evaluation/` (11 files) and `FLINTEL_*.md` + `docs/`. New: `evaluation/classification_eval_v4_platformfix.jsonl`. Not touched: `billing.py`, billing policy, `.env*`, production configuration. `tests/staging_preflight.py` (a stray copy of `tools/staging_preflight.py` that arrived with the upload) was left alone.

| Status | Path |
|---|---|
| NEW | `FLINTEL_ENV_REFERENCE.md` |
| NEW | `FLINTEL_FINAL_HANDOFF.md` |
| NEW | `FLINTEL_FINAL_HANDOVER.md` |
| NEW | `FLINTEL_HN_BILLING_DECISION.md` |
| NEW | `FLINTEL_INTELLIGENCE_ENGINE.md` |
| NEW | `FLINTEL_MASTER_AUDIT.md` |
| NEW | `FLINTEL_OPERATIONS.md` |
| NEW | `FLINTEL_RELEASE_AUDIT.md` |
| NEW | `FLINTEL_SEARCH_ARCHITECTURE.md` |
| NEW | `FLINTEL_SECURITY_FINDINGS.md` |
| NEW | `FLINTEL_STAGING_AUTHORIZATION_CHECKLIST.md` |
| NEW | `FLINTEL_STAGING_CHECKLIST.md` |
| NEW | `FLINTEL_STAGING_SETUP.md` |
| NEW | `FLINTEL_TEST_REPORT.md` |
| MODIFIED | `answer_integrity.py` |
| MODIFIED | `config.py` |
| NEW | `docs/HN_DATA_AUDIT.md` |
| NEW | `docs/STAGING_NOTES.md` |
| NEW | `evaluation/README.md` |
| NEW | `evaluation/benchmark.py` |
| NEW | `evaluation/classification_eval_v1.jsonl` |
| NEW | `evaluation/classification_eval_v2_heldout.jsonl` |
| NEW | `evaluation/classification_eval_v3_fresh.jsonl` |
| NEW | `evaluation/classification_eval_v4_platformfix.jsonl` |
| NEW | `evaluation/discovery_routing_dev_v3.json` |
| NEW | `evaluation/discovery_routing_dev_v5.json` |
| NEW | `evaluation/discovery_routing_heldout_v4.json` |
| NEW | `evaluation/discovery_routing_heldout_v6.json` |
| NEW | `evaluation/discovery_routing_queries.json` |
| NEW | `evaluation/discovery_routing_queries_v2_fresh.json` |
| NEW | `evaluation/run_eval.py` |
| MODIFIED | `logics.py` |
| MODIFIED | `platforms.py` |
| MODIFIED | `requirements.txt` |
| MODIFIED | `search_modes.py` |
| MODIFIED | `signal_intel.py` |
| MODIFIED | `templates/_search_mode.html` |
| MODIFIED | `templates/_touch_targets.html` |
| MODIFIED | `templates/chat.html` |
| MODIFIED | `templates/index.html` |
| MODIFIED | `tests/test_classifier_evaluation.py` |
| NEW | `tests/test_map_concurrency.py` |
| NEW | `tests/test_partial_note_ui.py` |
| MODIFIED | `tests/test_phase22_platform_registry.py` |
| NEW | `tests/test_platform_labels_ui.py` |
| NEW | `tests/test_platform_rendering_e2e.py` |
| NEW | `tests/test_platform_truth.py` |
| NEW | `tests/test_query_understanding_examples.py` |
| NEW | `tests/test_report_consistency.py` |
| MODIFIED | `tests/test_request_kinds.py` |
| MODIFIED | `tests/test_ui_accessibility.py` |

**Local commits on `work/final-master-execution` (oldest first, all unpushed):**

1. `d790184` Platform truth: attribute results by URL host, regroup mislabelled posts, exact platform names in the UI
2. `601d53e` Report integrity: one set of counts, review status on every signal item, intent strength = author intent
3. `6033cd5` Performance + honesty: bounded-concurrency map step, one retry, partial-analysis marker
4. `c172735` Topic relevance counts the words that name the topic; Live hint states Hacker News stories only
5. `c9b1ad6` Keep the freshness-note call site unchanged; add the partial-analysis note on its own line
6. `526f6c2` Retrieval: resolve a record's platform from its URL host before the stored label
7. `4e6206a` Reconcile the signal report with the checked report: a vendor/testimonial row is never direct demand
8. `826f10b` Add classification probe set v4 (35 synthetic items, evaluated once, no rule change)
9. `fe8ab84` Report table: every row names its real platform (no blank Source cell)
10. `5f89506` requirements: correct the stale TemplateResponse comment (call sites already use the new signature)
11. `9d814e8` Tests: the vision's example requests against deterministic query understanding (two known misses documented)
12. `cedccf2` Draft final handover (test results and file inventory to be filled in after the final run)
13. `1f5e801` Fix five regressions found by the full-suite comparison
14. `da69e8c` Review pass: coverage counts consistent, regression tests for batch failure, context propagation, URL/title integrity and the duplicated control

## I. Remaining blockers / needs your action
1. A real run with a development OpenAI key and a real MongoDB (nothing in this report touched either) — to measure latency, retrieval quality, and the
   router on real queries; **the original "AI SaaS agent" report cannot be reproduced** without its records.
2. Product decision: Hacker News comments are not searched (stories only) — most buyer statements on HN are comments.
3. Integrity-layer relevance gate (any-term) for multi-word product queries.
4. 38 pre-existing failing tests and 3 collection errors (see G) — unchanged, not hidden.
5. Starlette upgrade in staging; `SITE_URL`/`TRUSTED_HOSTS` for any deployed environment.
6. Two broad-discovery phrasings are not recognised (documented).

## J. Readiness (accurate labels)
| Area | Status |
|---|---|
| Platform truth (retrieval, integrity, signal report, page) | IMPLEMENTED, LOCALLY TESTED |
| Report consistency / accounting / classifier reconciliation | IMPLEMENTED, LOCALLY TESTED |
| Bounded-concurrency map step + partial marker | IMPLEMENTED, LOCALLY TESTED (simulated model calls only) |
| Real model, MongoDB, Hacker News API, OAuth, payments, proxy | NOT INTEGRATION VERIFIED |
| Staging | NOT STAGING VERIFIED (nothing created) |
| Production | NOT PRODUCTION READY, NOT PRODUCTION APPROVED |

## K. How to run
Local, free, no keys: see `FLINTEL_OPERATIONS.md` and the previous handover. Short form: Python 3.12, `pip install -r requirements.txt`, a local MongoDB on
127.0.0.1 (dev database only), a `.env` with `SESSION_SECRET_KEY`, `MONGODB_URI`, `MONGODB3`, `MONGODB_DB=<dev name>`, `TRENDING_KEYWORDS_ENABLED=false`,
`LAZY_EMBED_ENABLED=0`, `EMAIL_BACKEND=console`, no `OPENAI_API_KEY`; `uvicorn index:app --host 127.0.0.1 --port 8080`. Tests:
`python3 -m pytest tests -q -p no:cacheprovider --continue-on-collection-errors` (about 17 minutes).

## L. Recommended next action
Run one real search against a development database with a spend-capped development key, with `CLAUDE_MAP_CONCURRENCY=1` then `4`, and compare the answer's
platform labels, counts and latency with the output of this branch's tests.

## M. Report fixes after the owner's lead-discovery test (date range, completeness, table, evidence, coverage)
These were found by the owner running a real AI-development / automation lead-discovery search. Everything below is **LOCALLY TESTED with synthetic records and
simulated routers/models only**; the original report's data and the real router were not available, so root causes are those verified in the code and
reproduced by tests, not a replay of that exact search.

| # | Issue | Root cause (verified) | Fix |
|---|---|---|---|
| 1 | 30 days requested, ~24 h searched | (a) the period parser had **no month/year units and no hyphenated forms** ("past month", "last 3 months", "30-day window" returned nothing); (b) with several period phrases the **first** one won, so "last 24 hours ... last 30 days" searched 24 h; (c) when a stated period was not parsed, the **Active Intent window (~25 h, `ACTIVE_INTENT_WINDOW_MINUTES` = 1500)** was applied as if the user had given no period; (d) the report never compared requested with applied | `timerange`: months/years, hyphenated and abbreviated forms, the longest period wins (with a note), `mentions_period()`; `active_intent.historical_window_applies` never replaces a period the user wrote; coverage block states requested vs applied and **warns when the applied period is shorter** |
| 2 | Two threads discussed, one row shown | the writer listed one post and the server had no list of the others; a link spelled differently (scheme/`www`/query/fragment) matched no stored post and was silently counted "unverifiable" without naming it | `reconciliation` (shown / excluded with reasons / not listed, all adding up), `not_listed` (retrieved posts a text-only check reads as requests for a solution, with title, URL, platform), `excluded_posts` now carry title/platform/reason (a model-titled "unverifiable" entry says so), the writer's URL is resolved by canonical source id; page lists both under "Retrieved but not listed" / "Excluded" |
| 3 | Misaligned, hard-to-read table | intent, purchase status, fit and source verification were packed into one `·`-joined line, evidence/summary/uncertainty into the Summary cell; columns appeared conditionally; no per-cell labels, no phone layout | one schema (`# / Source / Post / Evidence / Assessment / Source check / Posted`), each dimension a labelled line, `data-label` on every cell, stacked card layout under 720 px; only http(s) links become anchors |
| 4 | "The model's quote was not found... an excerpt is shown" | the verification was a *normalised* comparison and the **model's rewrite was displayed** when it matched; kinds were not distinguished in the page | `locate_quote` returns the **source's own characters**; evidence kind is `exact_quote` (verified, shown as the original text) or `source_excerpt` (chosen by Flintel when the quote is not in the post); the AI summary is a separate labelled line; the inference is labelled "Why (inference)" |
| 5 | No coverage statement | none existed | server-built `search_coverage`: mode, requested period, applied period + origin, each selected source **searched / unavailable / not searched**, one set of counts (retrieved, duplicates, excluded, relevant, displayed, not listed), complete/partial with reasons; rendered as a "Search coverage" panel |

New tests: `test_period_and_coverage.py` (50), `test_report_completeness.py` (30), `test_report_table_ui.py` (17). Existing tests that asserted the old one-line
table cell format were updated to assert the same facts by label (columns, intent, purchase, fit, source verification).
Limitations: the live router's own period interpretation, real Hacker News/MySQL/MongoDB retrieval and the model's writing were not exercised; `not_listed`
uses a text-only check (it can miss a relevant post or list a weak one); "last month" still means the previous calendar month (documented behaviour);
`mentions_period` also matches phrases like "next day", which only means an automatic window is not applied there; an unparsed stated period results in an
unbounded search that the coverage panel flags, not a guessed range.

### M-results. Final full-suite result for the report fixes (HEAD 29ae35d)
Command: `python3 -m pytest -q -p no:cacheprovider -rfE --continue-on-collection-errors`; exit status read from the exit file (1 = tests failed), not a launcher notification.
Result: 38 failed, 5902 passed, 81 skipped, 10 xfailed, 3 collection errors (tests/test_lead_qualification.py, tests/test_option_a_followup.py, tests/test_option_a_guard.py).
Comparison with the 38 + 3 baseline ID list: NEW 0, FIXED 0, UNCHANGED 41/41 (IDs compared one by one).
Status: IMPLEMENTED and LOCALLY TESTED only. INTEGRATION VERIFIED: none (real router, MongoDB, OpenAI, Hacker News API and MySQL were unavailable).

### N. Claim checks, opportunity categories, entity grouping (reliability mission)
- `report_claims.py`: free-text sentences are removed when they name a company/product absent from every displayed row, cite a table row that does not exist, claim a named party requested a demo/trial/quote/pricing/proposal that no cited (or displayed) post's author requested, or claim a partnership/integration no post expresses. Aggregate and negated sentences are left to the existing checks. A summary emptied by this gets the server's `verified_summary`.
- `opportunity_layer.py`: per displayed row one of confirmed_buyer / strong_research / problem_signal / potential_partnership / competitor_signal / general_discussion / unverified; entity grouping on verified identity only (same author, or the same explicitly verified `canonical_company`; titles/shared words/category terms never group; a shared leading product name in vendor titles is only a `possible_relationships` label and changes no count); recency (older than 90 days = historical, never a current opportunity). Output: row fields `opportunity_category`, `entity_id`, `recency`, `current_opportunity` and a top-level `opportunities` block, shown as an "Opportunity summary" box in both templates. Metrics are separate: `listed_posts`, `distinct_sources`, `unique_authors` (known only), `unknown_author_posts`, `identity_groups` (groups with a known author or verified company), `records_without_identity` (each counted individually, never called verified), `confirmed_buyer_signals` / `confirmed_buyer_groups` / `confirmed_current` / `confirmed_historical`. The box reads `search_coverage`: it says "no confirmed buyer signal" without caveat only when every selected source was searched; otherwise it says the search was incomplete (naming each source's state) and that this does not show that no demand exists; historical signals are labelled not-current.
- Tests: `tests/test_opportunity_integrity.py` (33) + 2 browser tests in `tests/test_report_table_ui.py`.
- Full suite (exit status read from the exit file): 38 failed, 5929 passed, 81 skipped, 10 xfailed, 3 collection errors; failing IDs identical to the 38 + 3 baseline (new 0, fixed 0). LOCALLY TESTED only. Real-world validation: NOT PERFORMED (no real integrations available).
