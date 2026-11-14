# Lead scans: billing readiness (assessment only; billing NOT activated, no prices changed)

Status: lead scans are not connected to credits or subscriptions. Today the only controls are `LEAD_SCANS_PER_DAY` (5) and `LEAD_PROFILE_FETCHES_PER_DAY` (10) per signed-in account.

## Existing system to reuse
`search_credits.CreditStore` ticket lifecycle (reserve, start, claim, mark_enqueued, commit, self_refund, record_*); plans Standard 400 and Business 1500 credits, free 40/month by default; Lemon Squeezy billing; `credit_cost.py` cost validation.

## Proposed mapping (to build, not built)
1. Entitlement check before the scan: reserve N credits with an idempotency key (the scan request id).
2. Run the scan. Commit on a completed or partial scan that returned a statement; `self_refund` when every source failed.
3. Per-account scan lock (already exists) prevents double-spend from double clicks; duplicate request ids must not reserve twice.
4. Quota claim stays non-refunded (abuse control), separate from credits.

## Cost per scan (unmeasured)
Cost = embedding tokens (about 6 queries, one batched call) x current provider price + intent-bridge model calls if `INTENT_BRIDGE_ENABLED` + Mongo read load. Hacker News is free. Measure in step B/E of `FLINTEL_REAL_VALIDATION_PLAN.md`; no number is claimed here.

## Failure handling
| Case | Credits | Quota |
|---|---|---|
| all sources failed | refund | consumed |
| partial (one source failed) | commit, label the scan partial (decision needed: partial discount) | consumed |
| timeout | refund if nothing returned | consumed |
| process killed mid-scan | reserved ticket must be reaped (needs a sweeper; not built) | consumed |

## Abuse controls
Daily cap, per-owner lock, website-fetch limit, SSRF-safe fetch, signed-in only. Missing: email-verified signup requirement for lead scans, IP-level limits, spending alarm.

## Must exist before selling
Credit reservation wired into the scan route with tests; reaper for orphan tickets; measured cost; pricing decision by the owner; refund policy copy. Do not activate until then.
