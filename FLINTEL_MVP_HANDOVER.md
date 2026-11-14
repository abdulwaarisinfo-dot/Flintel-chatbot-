# FLINTEL — Lead-generation MVP handover

Status: **MVP PARTIALLY IMPLEMENTED, LOCALLY TESTED.** Nothing here is END-TO-END VERIFIED against real services, STAGING VERIFIED or PRODUCTION READY.
Not committed, not pushed, not deployed.

## What exists (the app is FastAPI + Jinja + MongoDB, not Flask)
Workflow: business profile -> Find Leads -> qualified, evidence-backed leads -> lead dashboard. Pages (all require a signed-in account): `/leads` (Overview),
`/leads/profile`, `/leads/find`, `/leads/scan/{id}`, `/leads/my`, `/leads/item/{id}`.

| Module | Role |
|---|---|
| `business_profile.py` | validation; user facts kept apart from derived terms and website facts; deterministic website reading (no model); owner-scoped `ProfileStore` |
| `safe_fetch.py` | SSRF-safe fetch of a user-supplied URL: http/https only, ports 80/443, every resolved address must be public, redirects re-validated per hop, size/time bounded, connects to the validated address |
| `lead_engine.py` | search concepts, normalisation (reuses `platforms.normalize_signal`), de-duplication, qualification (reuses `signal_intel.classify_post`), outreach opener, scan orchestration, coverage and reconciliation |
| `lead_connectors.py` | adapters: Hacker News live (`hackernews_live.fetch`), stored Reddit (existing matcher, injected) |
| `lead_store.py` | owner-scoped scans and leads; statuses new / saved / contacted / not_interested; re-scans keep status |
| `lead_routes.py` + `templates/leads_*.html`, `_lead_card.html` | the pages; same-origin POST guard applies |
| `database.py` (additive) | collections `flintel_business_profiles`, `flintel_lead_scans`, `flintel_leads` with owner indexes |

## Qualification rules (backend, not UI text)
Dimensions kept separate: business fit, problem fit, intent category (the nine in the brief), recency (recent / historical / undated / invalid / future), geography,
evidence (exact quote copied from the post + source URL). Labels: **High potential** = demand intent + strong fit + recent + location not in doubt;
**Relevant**; **Needs review** (historical, undated, invalid date, unconfirmed location for a local business, weaker signals); **Not a lead** (vendor promotion,
general discussion, unrelated, excluded term, no exact quote, no source link). Future-dated posts are excluded. There is no numeric score and no conversion claim.
Cross-platform usernames are never merged; duplicates are removed by platform-specific source id.

## Sources (verified from code; none verified against the live service in this environment)
| Source | Offered when | Kind | Content | Notes |
|---|---|---|---|---|
| Reddit | in the platform registry (default) | stored/historical | stored posts | needs production MongoDB + embedding service; failure is reported as `failed`; a time-out inside the matcher is NOT detectable as `partial` |
| Hacker News | `HACKERNEWS_ENABLED` and its guards | live | stories only (no comments) | honest per-request status (ok/empty/partial/failed/rate_limited) |
| X, LinkedIn, Facebook, Instagram, TikTok | never | - | - | no connector exists; not offered |

## Billing and cost
Existing billing (Lemon Squeezy, Standard $19 / Business $59, signed webhooks, test-mode flag) is untouched and its tests still run. **Lead scans are NOT metered by the
existing credit system**; the only limit is `LEAD_SCANS_PER_DAY` (default 5 per account), a safety cap, not a price. No pricing was changed or invented. Processing is model-free:
measured locally on synthetic posts, qualification is about 0.28 ms/post (1000 posts: ~300 ms total, linear to 2000). Retrieval time/cost (Algolia, Mongo, embeddings for the
Reddit matcher) is NOT measured here.

## Owner actions / blockers (ranked, after the reliability / intelligence phase)
1. Real-source verification: nothing has run against real Mongo, embeddings, Algolia or Reddit data. See `FLINTEL_LEAD_STAGING_VERIFICATION_PLAN.md` (not executed).
2. Real-world lead quality is unknown: all evaluation data is synthetic and written by the author of the engine (`evaluation/LEAD_EVAL_RESULTS.md`).
3. Scans are not metered by the credit system and have no price; the only limits are `LEAD_SCANS_PER_DAY` (5) and `LEAD_PROFILE_FETCHES_PER_DAY` (10) per account. Decide the entitlement before selling.
4. Concurrency has only been tested against in-memory doubles that mimic atomic single-document operations; the claim and lock are untested on real MongoDB / several workers.
5. No model-based business understanding (none authorised): search concepts and alignment are deterministic from the user's own words. Cross-language matching needs profile terms in that language.
6. Scans run on the request (worker thread, 30 s shared source deadline, 45 s hard limit); there is no background monitoring, notification or auto-refresh, and none is claimed. A source that is stuck cannot be killed; its thread is abandoned (pool of 8).
7. Recency is fixed when a scan runs. Local-business geography only checks place names in the post, and a capitalised word after "in/near/around" is read as a place.

## Phase update (reliability, intelligence, UX)
- Reliability: handlers no longer block the event loop (`run_in_threadpool`); every source shares one hard deadline; atomic per-account quota and scan lock in `lead_store` (single-document conditional updates plus unique indexes); failed scans count; website checks limited per day; `safe_fetch` has a hard DNS timeout and a total deadline; its real socket transport is tested against local servers including TLS verification; the chat flow's website fetch (`logics.fetch_website_text`, `_fetch_raw_html`) now uses `safe_fetch`.
- Intelligence: `lead_intent.py` reads each post with need frames (REQUEST, RECOMMEND, PRICE, COMPARE, REPLACE, PROBLEM, WISH) and aligns it with the profile (identifiers, offer and problem words, audience, place); guards stop vendors, definitions, tutorials, job seekers, idea validation. Synthetic evaluation (234 items): recall 0.32 -> 0.94, precision 0.93 -> 0.99; off-domain false surfacing 3.33% -> 0.27%.
- UX: "Find Leads" link in the sidebar of the home and chat pages; return to the requested /leads page after sign-in; scan progress and a disabled submit button; a plain-language "How Flintel rates a lead" panel; clearer busy / cap / failed messages.

## Tests
`tests/test_lead_mvp.py`, `tests/test_lead_mvp_ui.py`, `tests/test_lead_reliability.py`, `tests/test_lead_intent.py`, `tests/test_lead_ux.py`, `tests/test_safe_fetch_transport.py`, `tests/test_chat_fetcher_ssrf.py`: profile validation, SSRF refusals, concepts per business type, scenarios A-J, coverage
states, reconciliation, ownership isolation, status transitions, re-scan idempotence, XSS escaping, javascript: links, scan cap, full page flow. See the final report for the full-suite result of the latest run.

## Validation-phase additions (real-world readiness; nothing executed against real services)
- Plans/analysis: `FLINTEL_REAL_VALIDATION_PLAN.md` (supersedes the staging plan), `FLINTEL_LEAD_BILLING_READINESS.md`, `FLINTEL_BASELINE_FAILURE_TRIAGE.md`, `evaluation/INDIRECT_DEMAND_ANALYSIS.md`, `evaluation/real_eval/README.md`.
- Tooling (dry-run by default, guarded): `tools/lead_validation/` (verify_sources, mongo_concurrency), `evaluation/real_eval/`.
- Fixes with regression tests: calendar/holiday/street/brand words no longer read as places; stored leads re-aged at display time; `safe_fetch` decodes chunked/gzip safely with size caps; sign-in return verified through the login endpoint.
- Still unverified: real Mongo multi-worker behaviour, real Reddit/Algolia/embedding paths, real-data lead accuracy, billing.
