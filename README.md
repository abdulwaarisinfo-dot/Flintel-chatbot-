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

## Guest search limit (sign-up gate)

A visitor who is not signed in gets **one genuinely completed search**. The count is enforced on the server
(`guest_gate.py`, Mongo collection `flintel_guest_gate`), so refreshing, opening another tab or restarting the
browser cannot reset it. Once that search's answer has finished displaying, a compact sign-up / sign-in modal opens about 1.5 s later; signing in
(email or Google) reopens the same conversation without re-running the search or spending the account's credits.

### Settings (all optional)

| Variable | Default | Meaning |
|---|---|---|
| `GUEST_CREDIT_ALLOWANCE` | `5` | Credits a guest may use in total (measured with the same per-search cost as signed-in users; a search that starts under the allowance may finish over it). `0` switches the gate off. **Takes precedence** over `GUEST_SEARCH_LIMIT`. |
| `GUEST_SEARCH_LIMIT` | `1` | Legacy rule, used only when `GUEST_CREDIT_ALLOWANCE` is unset **and** this is set explicitly: completed searches per guest (`0` = off). |
| `GUEST_INFLIGHT_STALE_SECONDS` | `900` | An unfinished reservation older than this stops blocking (never below `RESPONSE_TIMEOUT` + 60). |
| `GUEST_ID_COOKIE_MAX_AGE_DAYS` | `400` | Lifetime of the signed `fl_gid` identity cookie (browsers cap cookies at 400 days). |

### Guest credits, sign-up hand-over and first-paid carryover

- **Counting**: a guest's allowance is cumulative credits. Only a genuine answer that was delivered and saved adds its
  credits (the cost the credit store charged for that search); failed, timed-out, cancelled or released searches add 0.
  A search may start while the guest is still under the allowance and finish over it; once the total reaches the
  allowance the next search needs an account. A guest record written before credit mode is read through the existing
  chat/credit records; searches whose cost cannot be matched reliably count 0 credits (never invented), so such an old
  guest may receive a one-time extra allowance.
- **Hand-over**: when a visitor *creates* a new account (e-mail code sign-up or Google), the credits the guest already
  used are charged to the new account's first free month, once: starting credits = max(0, free grant - guest credits).
  Logging in to an existing account never hands anything over. A guest search still running at that moment is held
  and either committed (it completed) or released (it ended uncounted), decided once on the guest record. If fewer than
  72 hours remain in the UTC month, the charge goes to the next month, so credits are not burnt in a month that is
  about to end. The hand-over is best effort and never blocks sign-up; an interrupted one is finished on the account's
  next usage read.
- **First free -> paid carryover**: the first time an account with no earlier paid history activates a paid plan, its
  unused free credits are added once to that first billing period (limit = plan grant + carried credits). The free
  counter is frozen while this is decided so the amount cannot change under it. Upgrades/downgrades in that period
  keep the same carried amount; the next renewal resets to exactly the plan grant. Existing subscribers and
  re-subscriptions never receive it.
- **Cost with credit enforcement switched off**: a guest search is still counted at its real routed cost (the existing
  credit-cost rules; `credit_cost.py` is unchanged), bound to the guest's reservation before the job is queued. If the
  cost cannot be decided the search is refused as "temporarily unavailable" and nothing is reserved. Only a token that
  was reserved by older code has no bound cost; it is charged the deterministic fallback, logged
  ("no bound cost ... fallback") and listed in the record's `cost_fallback_tokens`.
- **A handed-over guest record is closed**: after the hand-over the allowance lives on the account, so the guest record
  accepts no new search; only the search that was running at sign-up (the *hold*) can finish and be counted, exactly
  once. A search that was stale or already released at that moment can neither restart nor count. Every guest identity
  that CREATES an account is closed this way - also one with zero credits that never searched - so a logout (the signed
  guest cookie survives it) does not hand the same browser a fresh guest allowance; logging in to an existing account does
  not close anything, and a new browser / cleared cookies is a new visitor (inherent to anonymous identities). With credit
  enforcement switched off (kill switch) there is no account ledger, nothing is claimed and the identity stays open.
- **Hand-over whose month is over (`pending_expired`)**: if the hand-over cannot be applied until after its target month
  ended (for example a long outage), nothing is deducted from any month and the hand-over is **not** marked complete: the
  account gets a `guest_handover_pending` record. It is never resolved automatically. An operator records the outcome
  once with `guest_gate.resolve_handover(guest_id, email, "apply_late" | "waive", reason)` (`apply_late` deducts from
  the ORIGINAL month only; `waive` deducts nothing; both store the reason; repeats return `duplicate`). Find pending
  ones with `db.flintel_credit_usage.find({"guest_handover_pending": {"$exists": true}})`. Until it is resolved a **first
  paid activation is answered "try again" (HTTP 409)** so the carryover cannot silently ignore the guest's usage. The
  subscription state is stored even when the plan write is deferred, and a deferred event is marked `failed` (re-processable).
  Lemon Squeezy's documentation (not independently verified from here) says only a 200 counts as delivered and that any
  other status gets up to three more attempts with exponential back-off (documented example 5 s, 25 s, 125 s) and then no
  more automatic attempts, so a deferral that outlasts that window is **not retried by the provider**. After resolving the
  cause, finish it by re-sending the event from the store's webhook settings (the dashboard offers re-sending recent
  webhooks; the exact UI is unverified) or with `billing.recompute(subscriptions, plans, owner_email, credits=search_credits.default_store())`
  from a maintenance shell (this exact call was run only against mongomock, where it completed the activation and a second call was a no-op).
  Alert on `flintel_billing_events` documents with `state: "failed"` that are older than a few minutes.
  **Accounting outcome:** the carryover is always the account's OWN unused free credits of the activation month. `apply_late`
  records the guest usage in the original month's ledger and `waive` records that nothing is deducted; neither changes the
  activation month's balance, so the carryover is identical for both, and the account keeps the full grant of the month
  that was missed. Recovering that usage from the activation month (or any other month) is deliberately NOT done: it
  would charge an unrelated period and needs an explicit business decision.
- **A counted answer whose delivery failed after the hand-over**: the count is kept (the account was charged), a warning
  ("... count is KEPT ...") is logged and the failure is appended to the guest record's `handover.delivery_failures`.
  The answer is owed and is re-delivered **without counting again** (a counted token skips `reserve` and `complete`
  answers `duplicate`; answer generation never touches the credit store) as soon as something re-runs that turn. In the code as
  it stands that is **user-driven or manual**: the account opening the chat (the chat page reserves the latest pending turn
  for `GET /chat/{id}/stream`; the browser side of that was not exercised here) or an operator (below). **The durable turn queue (`turn_finisher.py`, collection `flintel_turn_finishing`) and its 30-minute backfill
  are NOT operational in this code**: `database.py` does not define `turn_finishing_collection`, so queue writes, claims and
  the backfill are no-ops (they would only work in a deployment that defines that collection; the re-queue the gate attempts
  after a failed delivery is a harmless no-op until then). **An answer that is never re-delivered stays charged**; there is
  no timed refund. Operator procedure (mongosh syntax; the field names and query shapes were checked against the code and
  on mongomock, none was run against a live server):
  1. recorded failures: `db.flintel_guest_gate.find({"handover.delivery_failures.0": {$exists: true}}, {"handover.to": 1, "handover.delivery_failures": 1, "completed_tokens": 1})`;
  2. for a failure token `t` of account `a`: `db.flintel_users_chat.find({owner_key: a, messages: {$elemMatch: {request_id: t, claude_answer: null}}}, {chat_id: 1})`
     returns the chat whose message has no answer (the guest token is stored on the message as `request_id`; also try
     `owner_key` = the guest id). A crash that never recorded a failure is found by running step 2 for every completed token
     of every record that has a `handover`;
  3. re-run the turn: `index._finish_turn_job(chat_id, owner_key, topic_key)` (the worker's own function: it loads the chat,
     returns `busy` while the owner has another answer running, calls `_fill_in_message_outputs` and may call the model; the
     signature was verified, it has **not** been run end to end - try it on staging first), or ask the account to open the chat.
  Before a hand-over a failed delivery still gives the credit back.
- **Limits of this design**: the steps span several MongoDB documents and are fenced and eventually corrected, not one
  transaction. A first activation can be answered "try again" (HTTP 409) while a free reservation is still refundable,
  another activation attempt is live (stale after 120 s) or a guest hand-over is unresolved; the provider's own retries
  last only a short window (see above), so such an activation may need a manual re-send or `recompute`. Behaviour under
  real MongoDB concurrency and live Lemon Squeezy webhooks has only been tested against mocks.

### Required database indexes (fail closed)

Two UNIQUE indexes on `owner_key` are required for correctness, not for speed: `flintel_credit_usage` (only while credit
enforcement is on) and `flintel_plans` (when billing is configured or enforcement is on). Without them a lost insert/upsert
race silently creates a second document for an owner (split counters, two plan records). At startup `index_guard.py` does not
just call `create_index`: it **verifies** the result (`index_information()`: a single-field, unique, non-partial index on
`owner_key`). While a required index is `missing`, `not_unique`, has `duplicates`, or cannot be verified, every credit
operation that could create a document, a charge or an entitlement is refused (guest and member searches answer
"temporarily unavailable", activation/handover writes raise and are retried later). Reads, refunds, settlement and recovery keep
working, and the app stays up. A refused state is re-verified lazily (30 s; 5 min for duplicates), so creating the index heals it
without a restart. **Duplicates are only reported (count + hashed samples in the log, never raw keys) and are never merged or
deleted: an operator must resolve them, then create the index.** `GET /api/health/ready` always answers 200 and reports
`status: ok|degraded` with `credits` / `billing_plans` = `ok | not_required | unavailable` (+ the reason class).
Performance-only indexes (for example a future `flintel_guest_gate {handover.to}` or `flintel_turn_finishing {due_at}`) are
deliberately not part of this check. Verified on mongomock only; how a real server reports `index_information()` is a staging item.

### How it works

- **Identity**: a server-generated random id kept in a signed, `HttpOnly`, `SameSite=Lax` cookie (`fl_gid`, `Secure`
  over HTTPS, 400 days). An existing guest session id is adopted, so earlier guest chats keep working. No IP address,
  fingerprint or other device data is stored or used. The cookie survives logout.
- **Lifecycle**: `reserve` (atomic, before the search is accepted; two tabs or a double click admit exactly one)
  -> `complete` (only when the real answer was saved by a search that did not time out; counted once per request)
  or `release` (error, scan timeout, timeout fallback, refused credit reservation, cancelled; counts nothing).
  Completion and release are both **owned by the reservation's token** and each is a single atomic update (counting
  and freeing the slot are the same write), so the count can never exceed the limit and a crash can never leave a
  counted search holding the slot. **No over-delivery**: every path that produces an answer (blocking pipeline,
  background worker retries, both streams, the timeout fallback) first asks the gate (`on_work_start`: it re-takes the
  slot with the search's own token, or is *abandoned* without any model call when another search of that visitor is
  admitted or the allowance is used) and asks again right before the answer is saved or shown (`on_answer_ready`:
  counting is the commit; a superseded worker's answer is discarded and only an "answer unavailable / replaced by a
  newer search" notice is saved). A search whose first attempt was released and then retried while the slot is free
  re-takes the slot, blocks a second search while it runs, and counts when it succeeds. If the answer cannot be stored
  after it was counted, the blocking path takes the count back; a stream (the text was already shown) keeps the
  count and a refresh re-delivers it without counting again.
  A reservation that is never finished stops blocking after `GUEST_INFLIGHT_STALE_SECONDS`.
  There is deliberately **no cap on failed or incomplete attempts**.
- **What counts**: a real search whose analysis was delivered and saved (a genuine "no results" answer and a
  website-only analysis count). Small talk, clarifications, blocked requests, errors, timeouts, cancellations and
  answers the pipeline itself marks `incomplete` / `partial` (cut off) do not. A saved timeout-fallback answer alone
  is not proof of a completed search.
- **One model call per accepted search**: while a runner (first tab, retry, background worker, either stream) generates a
  search's answer it holds a short per-search work lease; a second runner makes no model call - the stream waits for the
  stored answer and replays it, the worker leaves the turn pending. The lease is given back in a `finally` (so a closed
  stream hands the turn over at once) and expires on its own after `RESPONSE_TIMEOUT` + 90 s (at most the stale limit
  minus 30 s) if the process dies.
- **Lease-expiry fence**: if a runner outlives its work lease and a second runner takes over the same search, the stale
  runner cannot count (the lease check and the count are one atomic update), cannot overwrite the stored answer (the
  answer is saved with a conditional update - the first writer wins) and cannot show conflicting text (for a guest the
  winning answer is chosen and stored *before* the first chunk is streamed; a runner that lost replays the stored one).
  Runners renew their lease while they wait and right before each model call; only the lease holder can renew. This
  stops stale delivery and persistence; it does **not** make model execution exactly-once - two model calls can still
  overlap after a lease expires (one wasted call).
- **Crash recovery**: (a) after the count but before the answer is stored, a refresh finishes the delivery - one model
  call, no second search, count, reservation or credit charge; (b) after the answer is stored but before the browser
  got it, the reconnect / refresh replays the stored answer with no model call. The sign-up modal is held back until
  the answer exists.
- **Limited results**: a guest's search that ran out of time (timeout / scan deadline) is answered from the evidence found
  so far, stays uncounted, and is stamped `answer_status: "limited"`; the page shows a "Limited results" chip and
  "this did not use up your free search - ask again" instead of presenting it as a finished search. (Registered users'
  answers are not stamped.)
- **Fail closed on identity**: a guest search is only admitted when the browser actually returned the signed `fl_gid`
  cookie for the id being used. A browser that refuses or drops cookies gets a 403 `guest-identity-required` message
  (no popup, nothing stored) instead of a fresh allowance per request; the refusal response sets the cookie, so a
  normal visitor succeeds on the next attempt. The cookie is `Secure` when the request came over HTTPS, when the proxy
  sends `X-Forwarded-Proto: https`, or when `SITE_URL` is an `https://` origin.
- **Signing in while a search is running**: the account is never charged for it (its credit was taken from the guest
  identity when it was accepted) and it is not run again. The conversation moves to the account and the running
  message is stamped with the guest id, so the search keeps reporting to its own reservation: if it completes it is
  counted against the guest identity (logging out is not a second free search); if it fails, that reservation — and only
  that one — is released and nothing is counted.
- **If the gate store is unreachable** the search is refused with a temporary error (HTTP 503), never with the sign-up popup.

### Server answers the browser reacts to (`X-Flintel-Denial`)

| Header value | Status | Browser |
|---|---|---|
| `guest-auth-required` | 403 | sign-up / sign-in modal |
| `credits-exhausted` + `X-Flintel-Audience: guest` | 429 | sign-up / sign-in modal |
| `credits-exhausted` (signed-in user) | 429 | "You've reached your search limit." upgrade modal (real `/api/me/usage` figures, link to `/billing/upgrade`) |
| `guest-search-in-progress` | 409 | inline message only |
| `guest-identity-required` | 403 | inline message only (cookies disabled / not returned) |
| `guest-gate-unavailable`, `credit-store-unavailable` | 503 | inline message only |

The modals are chosen from these headers, never from a status code alone. Closing a modal never grants another
search; the next attempt is denied by the server and reopens it.

### Known limitations (honest bypass surface)

The identity is a first-party cookie. **Clearing cookies, private/incognito windows, another browser or device**
produce a new identity and therefore one more guest search. Blocking cookies entirely gives a new identity per
request (the gate then cannot recognise the visitor). This is a conversion gate, not an abuse barrier: it
intentionally uses no fingerprinting and no IP hard cap. Per-IP rate limiting, if wanted, belongs at the edge.

### Tests

`tests/test_guest_gate.py` (gate state machine incl. the stale-takeover races, `/search` as a visitor, answer-pipeline
completion/release, identity cookie behaviour through the real middleware, email and Google continuation, entry-point
guards, and both modals in real Chromium) `tests/test_guest_gate_stream.py` (the two streaming entry points) and `tests/test_guest_gate_delivery.py`
(no over-delivery, sign-in during a running search, existing-guest migration, and a randomized model-based check of the
gate document's invariants). All of it runs on mongomock / stubs: live MongoDB is not covered. Flows that need a real Google
account or a real browser profile (cookie persistence across a full browser restart) are manual checks.

### E-mail sign-up (6-digit code)

E-mail sign-up is three steps and creates no account until the address is proven: `POST /signup/code` (address -> a 6-digit
code is e-mailed) -> `POST /signup/verify` (the code; the signed session then remembers "this browser verified this address"
for 15 minutes) -> `POST /signup` (password + confirmation; the address comes from the session, never from the request) ->
signed in automatically, guest chat moved and resumed as before. Codes expire after 10 minutes, are single-use, allow 5 wrong
guesses, and are stored only as a keyed HMAC. Limits: one code per address per 30 s, 5 per address per hour, 20 per client
address per hour. An address that already has an account is told so and is not e-mailed (as before).

| Variable | Meaning |
|---|---|
| `EMAIL_BACKEND` | `smtp` (default when `SMTP_HOST` is set) or `console` (LOCAL DEVELOPMENT ONLY: logs the code; refused when `SITE_URL` is https). |
| `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USERNAME`, `SMTP_PASSWORD` | Any SMTP provider. Port 465 = implicit SSL, otherwise STARTTLS (`SMTP_USE_SSL` / `SMTP_USE_TLS` override). |
| `EMAIL_FROM` | Sender address; its domain must be verified with the provider (SPF/DKIM) to reach inboxes. |

With no provider configured the e-mail sign-up answers "temporarily unavailable" (Google sign-in is unaffected). Delivery through a
real provider is **not verified** by the automated tests (SMTP is faked there); test it on staging before release. The new
`flintel_email_codes` collection has a TTL index on `expires_at`.
