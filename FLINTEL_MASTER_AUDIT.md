# FLINTEL Master Audit

Scope: repository audit of the search/retrieval/intelligence stack on branch `feature/search-modes-universal-intelligence`
(base `origin/main` `ac007f66`, plus the earlier local work on `feature/hn-general-discovery`). No database, OpenAI, hosting or
third-party credentials were available in the audit environment, and the sandbox cannot reach `hn.algolia.com`, `api.openai.com`
or `rapidapi.com`. Everything below is from code and offline tests unless it says "VERIFIED against a real service" (nothing does).

## 1. Repository state at start
- Stack: FastAPI/Starlette (not Flask), Jinja2 templates, MongoDB (`flintel_signals`, chats, jobs), optional MySQL sources, embeddings.
- `origin/main` = `ac007f66` (Hacker News platform). Local-only branches: `feature/hackernews-platform`, `feature/hn-general-discovery`.
- `historical-only-search` (other clone) was **not merged**: its idea (Live Reddit web search switched off at source) is already on main
  as `reddit_live.LIVE_SEARCH_EXECUTION_ENABLED = False`, and merging it would contradict the requirement that LIVE stays a mode.
- No search-mode control, mode parameter or provenance field existed anywhere before this branch.

## 2. Source capability matrix (Reddit and Hacker News) - from the code, not from hope
| Capability | Reddit | Hacker News |
|---|---|---|
| Live retrieval | **No.** A legacy GPT web-search path exists (`reddit_live.py`, paid model call) but it is switched off at source by an explicit product decision (`LIVE_SEARCH_EXECUTION_ENABLED = False`, "not an environment variable, so no deployment setting can switch it back on"). It was **not** re-enabled or integrated: that would reverse the decision and spend model budget. Missing capability for a real Reddit live mode: an approved retrieval API (official Reddit API credentials or an approved provider) | **Yes**, when `HACKERNEWS_ENABLED=true`: Algolia search API, **stories only** (`tags=story`), bounded by `HN_MAX_PAGES_PER_KEYWORD`, `HN_HITS_PER_PAGE`, `HN_FETCH_TIMEOUT_SECONDS`; 120 s in-process cache. Never exercised against the real API here |
| Historical retrieval | Yes: MongoDB `flintel_signals` (+ optional MySQL sources, embeddings) | Yes: stored by an external collector (not in this repo), stories and comments (`hackernews`, `hackernews_comment`) |
| General discovery | Yes, stored sample, capped at `UNFILTERED_DEFAULT_LIMIT` (3) | Live: yes (`general=True`, title-only stories allowed and labelled). Stored: only what the collector's keyword list captured |
| Topic search | Yes (keywords, phrases, embeddings, intent bridge) | Live: keyword queries to Algolia then the same intent bridge. Stored: yes |
| Comments | Not evidenced in this repository (stored platform value is `reddit`, posts only) - UNKNOWN | Stored: yes (`hackernews_comment`). Live: **no** (stories only) |
| Date filtering | Yes: half-open `[start,end)` publication time, `since_days`, typed ranges; Active Intent window | Live: Algolia `created_at_i` numeric filters from the same window, verified again client-side (`in_window`). Stored: same as Reddit |
| Pagination | Scan windows and batches (stored); no user pagination | Live: pages per keyword up to the configured cap. Stored: collector 2 pages x 50 per keyword per cycle |
| URL / timestamp | `post_url`, `created_utc` (validated; undated posts excluded from bounded windows) | `https://news.ycombinator.com/item?id=`, `created_at_i`; external link kept as `external_url` |
| Known limitations | No live path; stored corpus coverage unverified; MySQL coverage is UNKNOWN unless the optional probe is enabled | Live is a bounded sample, not an archive; Algolia rate limits are reported as `rate_limited`; stored corpus is keyword-gated (about 9.9k terms in the snapshot); flag off by default |
| Google/RapidAPI | discovery stubs only, Active Demand prompts only; not a search mode |  |

The UI explains unsupported combinations: the hint under the mode control, and an `unsupported_sources` list plus note in the answer ("Reddit does not support Live search and was not searched" / "Reddit has no live source; its results come from stored conversations only").

## 3. Pipeline audit (query -> UI) and findings
| Stage | Finding | Action |
|---|---|---|
| Mode | none existed | added `search_modes.py`, form field `search_mode`, stored on the message, scope in retrieval |
| Router -> keywords | topic-less requests ("show me recent discussions") were turned into keyword searches / dropped (fixed earlier: `signal_intel.is_general_discovery_request`) | kept; works in all three modes |
| Retrieval | HN merge dedups by exact URL only | canonical URL identity (www/old/m/np, tracking params, redd.it, HN item forms) |
| Provenance | none | `retrieval_path` = live / historical / both on every result in a mode scope |
| Cache | topic cache keyed by chat+topic would serve one mode's posts to another | HISTORICAL/LIVE cache under `topic::mode`; COMBINED key unchanged |
| Zero results | generic wording (fixed earlier: six codes from real coverage) | unchanged; mode coverage added (`not_requested` is never "empty") |
| UI | no control | segmented control, default visible, mode/coverage/provenance line in answers |

Not changed: per-card provenance badges (the summary line carries the counts; see handoff "known gaps").

## 4. Performance review (evidence-based only)
- Combined mode adds no extra network call: the live fetch already ran in parallel in the existing code; provenance tagging and the canonical
  merge are O(n) over at most `limit` posts (tests assert behaviour, not timing).
- The in-process Hacker News cache (120 s) is unchanged. No new cache was added.
- No load test was run. No claim is made about concurrency or user counts.

## 5. Security review (confirmed findings only)
- `search_mode` is an allow-listed enum (`normalize_mode`); unknown input becomes the default, so it cannot reach a query, a cache key or HTML
  unvalidated. The renderer uses `textContent`.
- Thread-local scope: no cross-request leakage (tested per-thread).
- No new route, no new outbound host, no new dependency, no secret in code or docs.
- Pre-existing issues were not hunted beyond the touched code; none were found in it.

## 6. Production-safety statement
Nothing was pushed, deployed, migrated or run against a real database. No environment variable or schedule was changed.

## 7. Per-result provenance (item 2)
`retrieval_path` is stamped on each result from actual retrieval metadata (live record = from the Hacker News fetch; stored record = from the stored path; both = the same canonical post was found by both). It is carried through dedup (the kept record is filled from the duplicate, never overwritten, so live-only fields such as points are not lost), ranking (tagging happens after ranking), the evidence cache (stored with the post), classification (copied to the signal item, never an input to the class), the answer JSON (`search_mode.provenance`, keyed by the post URL) and rendered as a LIVE / HISTORICAL / BOTH tag on each card. Cards with no metadata (Google stubs) get no label; nothing is guessed.

## 8. Credit and billing audit (item 4)
Read from `routes.py`, `credit_cost.py`, `search_credits.py`; verified by `tests/test_search_mode_credits.py` (16) on top of the existing credit suites.
- **Cost** comes only from the router's validated effort/cost proposal (`credit_cost.select_cost`). The search mode is not an input. Every mode value (valid, odd, hostile, 5000 characters) costs exactly what no mode costs.
- **Once per search**: the reservation happens in the route before retrieval, under `request_id(owner, submit_key)`. Combined = one charge, one job; Live and Historical are code paths inside the same request, with no access to the credit store (static test).
- **Retries**: the same submit key is a free redirect in every mode combination, even with a different mode.
- **Exhausted owner**: denied (429) before any job in every mode; small talk free. A mode cannot bypass the check because it is read after the charge decision and is an allow-listed enum.
- **New submit key** (a deliberate new search), even with the same text and a different mode, is a new charge (existing policy, unchanged).
- **Failure / partial failure / cache**: refund rules are the existing ones (a reservation is refundable only before its job is sent; nothing after that moves it back). The mode layer cannot trigger a second charge or a refund. Cache hits happen after the charge and are not discounted (existing).
- **Ambiguity to confirm with the owner (not changed)**: a Hacker News-only search enqueues no collector job (`hackernews_only`, from the earlier Hacker News feature) yet is charged like any search; whether that is intended pricing is a product decision. No amount, plan or rule was modified.

## 9. Coverage and zero-result verification (item 5)
Previously MySQL was never probed, so an empty MongoDB plus MySQL enabled produced UNKNOWN (never a false "nothing collected"), but "posts exist, none in range" could be asserted from MongoDB alone even though MySQL might hold in-range posts. Now:
- `mysql_signals.probe_coverage`: three read-only aggregate SELECTs with a server-side `MAX_EXECUTION_TIME` hint, parameterised values, validated database name; a failure raises and is treated as unknown. **Default off** (`ZERO_RESULT_MYSQL_PROBE_ENABLED=false`) because index coverage of the publication column on the real MySQL tables is unverified; it has only been tested against a fake connection.
- With MySQL enabled and not probed (or unreachable): totals and "none in range" are never asserted; the explanation is UNKNOWN and says why.
- New code `DATABASE_UNAVAILABLE` (the store itself could not be queried) is distinct from `COLLECTION_FAILED` (live fetch or collector failure).
- Distinctions covered by `tests/test_coverage_mysql.py` (18): no matching posts, nothing collected, source unavailable, collection failure, database unavailable, unknown coverage, filters excluded.
- A path that has no live source is `unavailable` (a capability gap), not `failed`; a bug that reported it as a partial failure was found by the new matrix test and fixed.

## 10. Business-intelligence re-audit (item 7)
Running the assignment's own examples through `rules-1` found defects: "My current invoicing software is too expensive" was MARKET (now PROBLEM_SIGNAL), "I switched from A to B" was MARKET (now COMPETITOR_SIGNAL), "Looking for a recommendation ... budget $50" was GENERAL (now DIRECT_DEMAND), and generic trend chat was MARKET (now GENERAL_DISCUSSION; the catch-all market rule was narrowed to concrete events). Classifier is now `rules-2`. Tests: `tests/test_intel_reaudit.py` (27). Classification ignores the retrieval path (tested). Still rule-based English; **no accuracy claim**: no representative real-data evaluation exists.

## 11. Security and performance re-review (item 10)
Final diff reviewed: no secrets, tokens, debug prints or TODOs; no new dependency; no new route or outbound host. `search_mode` is an allow-listed enum (hostile values tested); the renderer uses `textContent` only (an HTML-injection test passes); the provenance map is keyed by URLs the server produced and capped at 100. The MySQL probe is SELECT-only through the existing `_select` guard, identifiers from fixed lists, values parameterised, and off by default. Duplicate calls: Combined makes no extra network request; the HN pool is cached 120 s; no extra model calls. Not done: any load test, so there is no scalability claim.

## 13. Master completion mission (intelligence, UI, security, performance)
**Repository facts.** The application is FastAPI/Starlette (not Flask). Starting point `51737b65`; billing and credit files unchanged; the concurrency fix (`result_coverage_if_enabled`) and the `.env` preflight guard are preserved and re-tested.
**Security audit (verified findings).**
- *Dependencies* (`pip-audit` on `requirements.txt`, 42 packages): **only Starlette 0.38.6 is flagged, 7 distinct advisories** (fixes in 0.40.0 to 1.3.1): unbounded buffering of multipart text fields and large uploads, `request.url` built from an unvalidated Host header / request path, static-file path handling on Windows, handler-method dispatch. Relevance here: the app takes form posts (`/search`, login) and builds the Google OAuth redirect from `request.url_for` (Host-derived; Google also validates the registered redirect URI). Not fixed: Starlette is pinned below 0.39 and the repository is **mid-migration** (15 `TemplateResponse` call sites in `routes.py`, a mix of the old and the new signature), so an upgrade is a dedicated task with full-suite and staging validation. Interim mitigation: serve behind a proxy/host that enforces the Host header and request-size limits.
- *Session cookie*: Starlette's `https_only` was never set, so the session cookie lacked `Secure`. New `SESSION_COOKIE_SECURE` flag (default false: unchanged unless configured) wired to it; the staging preflight now requires `true`. The guest-identity cookie already sets `Secure` adaptively over HTTPS. Cookies are HttpOnly and SameSite=Lax (verified in a test).
- *CSRF*: no CSRF tokens exist; protection is SameSite=Lax session cookies (blocks cross-site form POSTs in current browsers). Not a complete defence for old browsers; documented.
- *Rate limiting*: none beyond credits, the busy lock and guest gate; login/registration endpoints have no throttle. Recommend host- or proxy-level rate limits for staging/production; not added (new behaviour/dependency).
- *Startup/production fallback*: re-verified (`.env` guard, mirrors, scheduler off in staging).
**Performance (measured, this machine, pure paths, per 1,000 items)**: classify 0.29 s (1.69x rules-2), report build 0.008 s, merge/dedup of 1,000+1,000 0.03 s, canonical URL 0.014 s, discovery detector 0.006 s; all linear (5x items, 5x time). Pinned by generous ceilings in `tests/test_benchmarks.py`. **Not measured**: database latency, provider latency, end-to-end search time, and the brief's "about 1,000 posts in about a minute" goal, which needs real sources (staging).
**UI/UX audit** (real stylesheet, real functions, phone 390 px and desktop 1280 px, screenshots reviewed): no horizontal overflow; findings fixed: provenance chips were 10 px (now 11 px), the search-mode note was unstyled text with jargon ("Live path: ok"), the report's metadata showed raw ids (`hackernews`) and a bare `LIVE` word. Now: a bordered "Search mode" block with a mode chip and plain wording ("Live source", "Stored conversations"), display names (Hacker News, Reddit) and provenance chips matching the card chips. Pinned by browser tests on both templates. Not audited (no running app with real data): login/onboarding, billing and settings pages, the home dashboard, loading/error states in context.

## 12. Master-directive pass (scheduler, isolation, seeding, startup writes)
**Dependency map (verified in code).** Implemented and locally tested: search modes, provenance, dedup, coverage, classifier `rules-2`, credits mode-independence, staging tools. Untested against real services: everything external. Broken: only the 43 baseline failures. Unsafe (now fixed): an uncontrolled startup scheduler; a preflight that treated missing isolation evidence as a pass; a verifier that reported UNKNOWN outcomes as PASS/FAIL. Needs real infrastructure and your authorisation: host, MongoDB, OpenAI key, Google OAuth, seeding writes, paid verification.
**Deployment/CI inspection.** The repository contains no CI workflow, `render.yaml`, Dockerfile or Procfile, so what a push can trigger is configured outside the code and **cannot be verified from here**. The only remote is `origin` on GitHub (`main` unchanged at `ac007f66`). Nothing was pushed.
**Scheduler.** `TRENDING_KEYWORDS_ENABLED` (+ `FLINTEL_ENV`) added; behaviour in `FLINTEL_OPERATIONS.md` section 4. Other startup jobs audited: the turn finisher is dry-run by default (no worker, no paid call); `keepalive` is never started; Hacker News and the signal report make no startup call. No other job makes a paid call or writes at startup; the DDL list is in `FLINTEL_OPERATIONS.md` section 3.
**Isolation.** Preflight rewritten (three-state verdict, phases, every URL-bearing variable, all secret variables by fingerprint, forbidden mirrors/integrations, risky flags, explicit scheduler/lazy-embed off, SITE_URL). Review of the code for bypasses found and fixed: a fixed variable list (now discovered), skipped checks counted as pass (now INCOMPLETE), `ANTHROPIC_API_KEY` accepted as the LLM key (deprecated), `MONGODB2/4` unchecked.
**Cost estimates (all ESTIMATED, none measured; no paid call was made).** Hosting: Render Starter $7/month (provider page, checked 2026-11-05). Database: Atlas M0 $0. UI search: router + answer LLM calls + one query embedding, an estimated few cents at most at `gpt-5-mini` rates. Query embedding: fractions of a cent (`text-embedding-3-small`). Seed embeddings: about 49 short records, roughly $0.001. Verification matrix: about 9 query embeddings, well under a cent. Scheduler cycle (only if enabled): one LLM web-search call plus a Google call, an estimated cents per cycle, two cycles a day. Validate any of these only after you authorise a spend.
**Billing.** Unchanged. Product decision left open: Hacker News-only searches are billed like normal ones though no collector job is queued.

## 14. Post-mission review and continuation
Repository: branch `feature/search-modes-universal-intelligence`; review commit `49731a96` (parent `20dbfaf5`); further local commits follow it. Nothing pushed, deployed or run against production or paid services.
- **Evaluation:** v2 was not an independent held-out set (same author). A fresh v3 gives accuracy 0.729 (95% CI 0.59-0.83), macro-F1 0.71, false-buyer 0/35, all synthetic. Full per-class tables: `FLINTEL_INTELLIGENCE_ENGINE.md`.
- **Routing:** a separate development set (v3) drove a second vocabulary extension; a new held-out set (v4) gives recall 31/50 and 0/40 false positives. Honest expectation 55-65% recall on unseen broad questions.
- **Security:** `FLINTEL_SECURITY_FINDINGS.md` (F1-F8). Implemented: OAuth redirect from `SITE_URL`, TrustedHost and Origin guard (OFF by default, required in staging), startup refusal in staging, failed-login throttle (ON). Not done: Starlette upgrade, per-form CSRF tokens, `GET /logout`.
- **UI/UX:** audited index, chat, pricing, discover and use-case pages at 375/768/1280 px in Chromium: no horizontal overflow, no script errors, no unnamed controls. Fixed: search-mode radio group keyboard model (roving tabindex, arrow/Home/End) and touch hit areas on coarse pointers; tests in `tests/test_ui_accessibility.py`.
- **Test discrepancy:** see `FLINTEL_TEST_REPORT.md`.

## 15. Closure pass
HEAD `60e2e24b`. See `FLINTEL_SECURITY_FINDINGS.md` (F1-F4 updated), the routing and classifier record in `FLINTEL_INTELLIGENCE_ENGINE.md`, the exact test results in `FLINTEL_TEST_REPORT.md` and the status table in `FLINTEL_RELEASE_AUDIT.md`. Key facts: cross-site guard ON by default; held-out discovery recall 57% with no topic leakage; full suite unchanged at 38 pre-existing failures + 3 collection errors; nothing integration or staging verified.
