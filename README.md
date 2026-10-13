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
