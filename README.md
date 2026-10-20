# Flintel-chatbot-


# Flintel Intent Prototype

Standalone validation of the approved intent architecture. **Touches no production code.**

The diagnostic measured, on 866 real Flintel documents: topic retrieval AUC **0.929**,
same-topic intent AUC **0.542**, topic-centered intent AUC **0.523**, P@10 on
buyer_demand probes **0.10**. Raw embedding similarity is not reliable enough to
separate intent in this corpus. This prototype tests whether an explicit intent layer
between retrieval and ranking moves those numbers, and it is measured against the same
corpus with the same metric implementations so the two reports sit side by side.

## Isolation guarantees

- No module imports `flintel`, `logics`, `config`, `database` or `github_signals`
- MongoDB is opened for `find()` only — no insert, update, delete, drop or index call
- Everything is written under `--outdir`; nothing else on disk is touched
- `validate.py selftest` asserts both mechanically, across every file, on every run

## Order of operations

```bash
# 1. See inputs and exact cost. Spends nothing.
python intent_prototype/validate.py plan --diag-dir ./diag

# 2. Prove isolation and unit behaviour. Spends nothing.
python intent_prototype/validate.py selftest

# 3. Build the human labeling sheet. Spends nothing.
python intent_prototype/make_tier1_sample.py select --diag-dir ./diag --n 200
#    -> label diag/tier1_to_label.csv by hand (2-3 hours)
python intent_prototype/make_tier1_sample.py convert \
    --csv ./diag/tier1_to_label.csv --out ./diag/tier1_verified.jsonl

# 4. Classify the corpus ONCE and cache it. THIS IS THE ONLY PAID STAGE.
python intent_prototype/validate.py classify --diag-dir ./diag --yes

# 5. Run every experiment from the cache. Zero API calls.
python intent_prototype/validate.py run --diag-dir ./diag
```

Experiment 5 additionally needs real queries:

```bash
# put 30-50 REAL Flintel queries, one per line, in diag/real_queries.txt
python intent_prototype/make_tier3_template.py build --queries ./diag/real_queries.txt
#    -> judge diag/tier3_to_judge.csv by hand
python intent_prototype/make_tier3_template.py convert \
    --csv ./diag/tier3_to_judge.csv --out ./diag/tier3_queries.jsonl
```

## Cost

| Stage | Claude calls | OpenAI calls |
|---|---|---|
| `plan`, `selftest`, `make_tier1_sample` | 0 | 0 |
| `classify` (866 docs, batch 17) | ~51 | 0 |
| `run` | 0 | 1 |
| `make_tier3_template build` | 1 per query (0 with `--offline`) | 1 |

`classify` refuses to run without `--yes`. `run` re-reads the cache, so grid search
over 1,728 weight combinations costs nothing.

## Modules

| File | Role |
|---|---|
| `schemas.py` | taxonomy, actor model, all coercion. Single source of truth. |
| `query_interpreter.py` | natural language → `QueryIntent`. One Claude call. |
| `retriever.py` | topic-only candidate generation. Read-only Mongo or offline corpus. |
| `doc_classifier.py` | batch post → `DocClassification`. The layer the diagnostic proved necessary. |
| `opportunity.py` | `opportunity_strength`. Pure function. |
| `ranker.py` | hard filters, ranking, AND/OR sections. Pure function. |
| `pipeline.py` | wiring only. |
| `metrics.py` | metric implementations, identical to the diagnostic's. |
| `validate.py` | Experiments 1-6 + selftest + cost plan. |
| `make_tier1_sample.py` | human label set builder. |
| `make_tier3_template.py` | real-query judging set builder. |
| `weights.json` | every weight. Nothing is hard-coded in `.py`. |

## Gates

| Exp | Measures | Gate |
|---|---|---|
| 1 | classifier accuracy (Tier 1 human labels) | macro F1 ≥ 0.65, buyer_demand ≥ 0.55, complaint_pain ≥ 0.60 |
| 4 | opportunity_strength calibration | group separation ≥ 0.25 |
| 2 | end-to-end ranking | nDCG@10 ≥ 0.45, same-topic AUC ≥ 0.75, dominance violations < 5% |
| 3 | latency | measured, **not** a gate |
| 5 | real-query product evaluation | nDCG improvement ≥ 0.15, win rate ≥ 0.70 |
| 6 | AND-logic correctness | zero merged strict/related sets |

Experiments 1 and 4 must pass before 2 is meaningful. 2 and 5 both gate production.

## Ground-truth discipline

- **Tier 1** (human, ~200 posts) — the only ground truth. Never tuned on.
- **Tier 2** (866 model labels) — stratification and weight calibration only.
- **Tier 3** (30-50 judged real queries) — the only measure of product performance.

The Tier 1 labeling sheet deliberately does **not** show the model's label. Model-vs-human
agreement is reported afterwards as information about the labels, never as a pass/fail.

## Two decisions flagged for review

**1. The taxonomy carries 12 intents, not 11.** The spec says "keep the 11 intent classes"
and separately names `usage_adoption` as a secondary opportunity signal, but
`usage_adoption` is not among the enumerated 11. Dropping it would break the approved
`opportunity_scan` behaviour and would fold 24 labelled posts into the catch-all class.
Set `COLLAPSE_USAGE_ADOPTION = True` in `schemas.py` to go back to 11.

**2. The Tier 1 budget defaults to 200, not 150.** The spec's own priority list
(all 48 buyer_demand + all 60 complaint_pain + all 72 hiring) is 180 posts before any
other class or random stratum. At 150, seven classes receive no stratified coverage at
all, and macro F1 — the headline gate — averages over classes holding two or three
examples. At 200 with the floor-then-round-robin allocator, the three gated classes get
~31 each and every other class clears 5, except `alternative_switching`, which the
866-doc corpus only contains twice. `select` prints the full coverage table so the
trade-off is visible before anyone starts labeling.

## Keep-alive

`keepalive.py` stops free/hobby hosts (e.g. Render) from idling the web server to sleep.
On startup it starts one background task that sends `GET {KEEPALIVE_URL}/ping` every
`KEEPALIVE_INTERVAL_SECONDS` (10 s timeout, one reused `httpx.AsyncClient`).
`/ping` (GET and HEAD) just returns `{"status": "ok"}`.

| Env var | Default | Meaning |
|---|---|---|
| `KEEPALIVE_ENABLED` | `true` | `false`/`0`/`no` turns it off; nothing is started. |
| `KEEPALIVE_URL` | `RENDER_EXTERNAL_URL` | Public base URL of this app. If neither is set, keep-alive logs one line, `keepalive disabled: no URL configured`, and stays off. |
| `KEEPALIVE_INTERVAL_SECONDS` | `30` | Seconds between pings (minimum 5). |

The ping and `/ping` never touch MongoDB, chats, sessions, guest owners or any LLM code.
They create no session cookie and do not affect the busy lock or the trending-keywords
scheduler. Failures and timeouts are caught and logged at DEBUG only.

A self-ping only keeps an awake process awake. It cannot wake an instance that is already
asleep. Each worker process runs its own loop. Check your host's terms on keep-alive traffic.

## Billing (Lemon Squeezy)

Paid plans are monthly subscriptions sold through Lemon Squeezy's **hosted checkout** (Lemon Squeezy is the
merchant of record; Flintel never sees or stores card details).

| Plan | Price | Credits |
|---|---|---|
| Free | $0 | `FREE_MONTHLY_CREDITS` (default 40) |
| Standard | $19 / month | `PLAN_CREDITS_STANDARD` (default 400) |
| Business | $59 / month | `PLAN_CREDITS_BUSINESS` (default 1500) |

### Environment variables (all five are required; names only here, values in `.env` / the host's secret store)

| Variable | Meaning |
|---|---|
| `LEMONSQUEEZY_API_KEY` | API key used server-side to create checkouts |
| `LEMONSQUEEZY_STORE_ID` | numeric store id; events from any other store are ignored |
| `LEMONSQUEEZY_VARIANT_ID_STANDARD` | numeric variant id of the Standard monthly subscription |
| `LEMONSQUEEZY_VARIANT_ID_BUSINESS` | numeric variant id of the Business monthly subscription (must differ from Standard) |
| `LEMONSQUEEZY_WEBHOOK_SECRET` | the webhook's signing secret (HMAC-SHA256 of the raw body) |
| `LEMONSQUEEZY_TEST_MODE` | optional; exactly `true` = accept/create test-mode checkouts only, otherwise live only |

With any of the five missing, checkout answers 503, the pricing buttons are disabled and the webhook rejects every
delivery. See `.env.example` for placeholders.

### Lemon Squeezy dashboard setup (one-time, done by you)
1. Create the store; create two **subscription** products/variants billed monthly at $19 and $59; note both variant ids.
2. Create an API key (test mode while testing) and note the store id.
3. Add a webhook: URL `https://<your-site>/webhooks/lemonsqueezy`, a signing secret of your choice, and the events
   `subscription_created`, `subscription_updated`, `subscription_cancelled`, `subscription_resumed`, `subscription_expired`,
   `subscription_paused`, `subscription_unpaused`, `subscription_payment_success`, `subscription_payment_failed`,
   `subscription_payment_recovered`, `subscription_payment_refunded`.
4. Set the five variables above on the server and restart.

### Routes
* `GET /billing/upgrade` — pricing page (server-rendered prices/credits; the page never shows a plan as active before the webhook).
* `POST /api/billing/checkout` (form field `plan` = `standard` | `business`; signed-in users only) → `{"url": <hosted checkout>}`.
  The browser sends only the plan name. The variant, store, price and the account the subscription belongs to are chosen
  by the server; the owner is carried in the checkout's custom data together with a server-side HMAC that the webhook verifies.
* `GET /api/billing/status` — the signed-in user's own subscription state.
* `GET /api/billing/portal` — a **freshly fetched** customer-portal URL for the signed-in user's own subscription. Lemon
  Squeezy's portal links are pre-signed and expire after 24 hours, so they are requested on click and never stored.
* `POST /webhooks/lemonsqueezy` — signature-verified webhook (no session; the signature is the only credential).

### How a subscription becomes credits
`billing.py` verifies the signature, ignores deliveries of another store/mode/variant or without a verified owner,
de-duplicates by the SHA-256 of the raw body **and, for paid invoices, by invoice id** (`payment:<subscription>:<invoice>` in
`flintel_billing_events`, shared by `subscription_payment_success` and `_recovered`), stores the subscription (`flintel_subscriptions`,
ordered by Lemon Squeezy's `updated_at` so stale events are ignored) and then writes the owner's **existing** plan record
(`flintel_plans`, via `search_credits.set_plan_entitlement`). `limit_for()` already turns an active plan record into 400 / 1500
credits; usage counters are never touched, so a retried event cannot create credits twice. Only invoices with status `paid`
count; a payment event for a subscription not recorded yet is answered 409 so Lemon Squeezy retries it.
* Paid through: `renews_at` while `active`/`on_trial`; `cancelled` keeps access until `ends_at`; `expired` ends now;
  `past_due`/`unpaid`/`paused` never extend access beyond what was already paid for. A failed payment grants nothing.
* After a successful renewal payment the subscription is re-read from the Lemon Squeezy API, so a renewal is not lost if
  `subscription_updated` is late.
* **Credit period (billing-anchored):** a paid plan's 400 / 1500 credits are counted in the *billing period* that was paid for,
  not in the calendar month. The plan record carries `credit_period_key` (the UTC date the period ends) and the credit store
  counts usage under that key. A new counter opens only when the paid-through date jumps forward by at least 20 days (a
  renewal), so duplicated, replayed or re-ordered events, upgrades and downgrades inside the period never open a second
  counter or reset usage. Usage on the free plan (calendar-month counter) never carries into a paid period; an upgrade
  keeps the period's usage; a downgrade lowers the limit without erasing usage (searches are denied until the period ends);
  after expiry the free calendar-month allowance applies again. Two overlapping subscriptions each have their own period:
  the higher plan is counted while it is entitled, then the other one with its own counter.
* **Refunds (policy R2):** a **full** refund of the invoice that funded the *current* period closes that period immediately
  (`revoked_period_key`); the next separately paid period is unaffected. A **partial** refund is recorded and revokes nothing.
  A refund of an older invoice is recorded only, and a refund that cannot be matched to the funding invoice (or whose
  amounts cannot be classified) changes nothing and is logged at ERROR for manual review (`refund_review_at`).
* **Manual plans:** an operator-written plan record (`activate_plan`, unchanged) is replaced by the first billing write for that
  owner; if that record was still active a WARNING is logged.
* **Plan index:** `flintel_plans.owner_key` must be unique. If the index cannot be built (for example duplicate rows already
  exist) startup logs `BILLING ALERT`, modifies nothing, and **new checkouts are disabled**; webhooks and existing
  subscriptions keep being processed (the subscription state is always stored; if several plan rows exist for an owner the
  plan write is refused with a 500 so Lemon Squeezy retries after you clean up).

### Tests
`python -m pytest tests/test_billing_lemonsqueezy.py tests/test_billing_hardening.py` — mocked provider, mongomock, no network. Live provider behaviour
(real payload shapes, the checkout API) is **not** covered by automated tests; verify it once in Lemon Squeezy test mode.
