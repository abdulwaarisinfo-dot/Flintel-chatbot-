# Decision paper: charging for Hacker News-only searches

**Status: for your decision. Nothing in billing was changed, and nothing below is a recommendation to change code today.**
All behaviour statements were checked in the code and by tests (`tests/test_search_mode_credits.py`, the two Hacker News-only tests). Dollar figures are not
given because no per-search cost has been measured (no paid call has ever been made).

## 1. Current behaviour (verified)
- A search whose selected platforms are **only Hacker News** queues no collector job: nothing is collected for it (`routes.py`: `hackernews_only(...)` makes `expects_job=False`).
- It is nevertheless **charged exactly like any other search**: the same atomic reservation, the same router-proposed cost (3-5 standard, 5-8 complex, 8-14 deep credits; fallback 4), the same commit when the chat message is saved. The cost is the same for Live, Historical and Combined, and the same as a search that does enqueue.
- Charging happens **before retrieval**. If the Hacker News fetch then fails, is rate limited or finds nothing, the user still pays and receives an honest explanation (never a fake result). Retrieval outcome does not change the charge anywhere in the product: this is true of Reddit searches too.
- Refund rules are the existing ones (refundable only before the job is sent). A no-job entry that is stuck is not auto-refunded: if its chat message exists it is committed, if it is stale and the message is missing it is flagged `NEEDS_REC` (ambiguous) for reconciliation.
- A search that selects Reddit **and** Hacker News does enqueue a collector job and is charged the same.
- The same no-job path is used by website-URL searches, which are charged the usage-based cost by the existing suite. Hacker News-only is therefore consistent with an existing precedent, not a new anomaly.

## 2. Why it behaves this way (rationale, inferred from the code, not stated by the authors)
The credit system prices the **user's request** (what the router judges the effort to be), not the collector's work. The user-visible work is the same in both cases: a router call, query embedding and analysis/answer generation by the LLM (the dominant, metered cost), plus retrieval. Whether a collector job is also queued was never an input to the price. Hacker News-only was added as a platform without touching billing, so it inherited this rule.

## 3. What is different about Hacker News-only (the implications)
| Aspect | Reddit/normal search | Hacker News-only |
|---|---|---|
| LLM cost to Flintel (router, embedding, answer) | yes | **same** |
| Retrieval cost to Flintel | stored scans (database) + collector job for the topic | one bounded call to a free public API (Algolia); no collection, no storage growth |
| Benefit to the user's *future* searches | the queued job collects new posts for that topic | **none** (nothing is stored) |
| Dependence on a third party at answer time | no | yes: Algolia outage or rate limit means an empty answer that is still charged |
| Freshness | stored, hours-old at best | live (the point of the mode) |
Net: Flintel's marginal cost is probably close to equal (the LLM dominates), while the user receives a different product (live, bounded, non-archival) with extra failure modes. Whether that justifies the same price is a pricing judgement, not an engineering fact; there is no measured data yet.

## 4. Options
| | Option | What changes | Pros | Cons / risks |
|---|---|---|---|---|
| **A** | **Keep as is** (current) | nothing | consistent, simple, no billing code risk; precedent exists (website searches); LLM cost is the same | the user pays the full price when Algolia fails or returns nothing; may feel unfair for a "live" product |
| B | Lower flat cost for Hacker News-only | router-cost override for no-job searches in `_run_credited_search`/`credit_cost.select_cost` + tests + a pricing decision | reflects absence of collection | new pricing tier; the LLM cost is unchanged so it may under-recover cost; needs the cost data first |
| C | Do not charge (refund) when the live fetch **fails or is rate limited** (not when it finds nothing) | a new refund path from retrieval outcome to the credit store | fairness for outages | touches the refund state machine (the most safety-critical code); retrieval happens after the charge, so it needs a post-hoc refund transition that does not exist today; abuse/idempotency questions |
| D | Free (0 credits) while Hacker News is in beta | a zero-cost path | simple message | the cost model has no zero (minimum 3); needs a new bypass; spend is then unlimited per owner unless capped separately |
| E | Different price per mode (Live > Historical) | the cost would depend on `search_mode` | reflects live-fetch value | **breaks the verified property that mode cannot change cost** and re-opens the manipulation analysis; not recommended |
| F | Charge only if at least N results were returned | outcome-dependent charge | "pay for results" | contradicts the product-wide pre-charge design; large change |

## 5. Recommendation (yours to accept or reject)
Keep **A** for the first staging round and decide with data: measure the real per-search LLM cost on staging (a few authorised searches in each mode), and the Algolia failure rate. If outages prove frequent, C is the fairest targeted change; B only if the measured cost gap is real. Any of B-F is a billing change that needs your explicit approval, code, regression tests across the credit suites, and a full-suite run, and none is made here.

## 6. What I need from you
Pick one: **A (keep)**, or name the option to implement (B / C / D) with the rule you want. Until you decide, behaviour stays as documented in section 1.
