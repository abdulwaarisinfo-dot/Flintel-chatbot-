# FLINTEL Intelligence Engine

One classifier: `signal_intel.classify_post` (`CLASSIFIER_VERSION = "rules-2"`), a deterministic rule engine - not a model. It works on the
retrieved posts of any mode (Live, Historical, Combined); provenance (`retrieval_path`) does not affect classification.

## Classes (conservative)
| Class | Rule of thumb | Example |
|---|---|---|
| DIRECT_DEMAND | the author asks for / seeks a solution, recommendation, alternative, developer, or states a budget | "I need a tool that can automate my invoices" |
| PROBLEM_SIGNAL | first-person pain or a request for help, no explicit ask to buy | "My current invoicing software is too expensive" |
| MARKET_SIGNAL | a question to the crowd or a market/pricing/security observation; **not** confirmed individual intent | "Is anyone else looking for an affordable invoicing tool?" |
| COMPETITOR_SIGNAL | a vendor/product launch, migration or comparison | "I switched from Product A to Product B because ..." |
| GENERAL_DISCUSSION | everything without evidence for the above (the fallback) | generic business-software chat |

Precedence: validation probe -> MARKET; launch/competitor patterns -> COMPETITOR; demand patterns -> DIRECT_DEMAND; problem patterns with
first person/help -> PROBLEM; market patterns -> MARKET; else GENERAL_DISCUSSION. A complaint is never promoted to a buyer, and a question is
never treated as purchase intent.

## Per-result output
`class`, `label`, `rule`, `quote` (an exact substring of the post text, verified with `in`; empty if none), `explanation`s, `confirmed` vs
`inferred` lists, `limitations`, `title`, `url`, `platform`, `author`, `published_at`, and five **separate** scores: `topic_relevance`,
`use_case_relevance`, `intent_strength`, `commercial_relevance`, `evidence_confidence` (capped at 0.9). Scores are shown with their names, never as
one unlabeled number. Quotes are never invented; revenue, buying readiness and competitor success are never asserted.

## Report
`build_signal_report` (flag `SIGNAL_REPORT_ENABLED`, default off) adds `signal_report` to the answer: per-class items, the exact statement
"No confirmed direct buying demand was found in the retrieved discussions." when none, and coverage (sources, period, counts, oldest/newest).
`extract_opportunity` separates facts, inferences and missing information.

## Zero results
`explain_zero_result` -> `SOURCE_UNAVAILABLE`, `COLLECTION_FAILED`, `DATABASE_UNAVAILABLE`, `NO_POSTS_COLLECTED`, `NONE_IN_RANGE`, `EXCLUDED_BY_FILTERS`, `NO_TOPIC_MATCH`, `UNKNOWN`,
built from real probes (MongoDB; MySQL only when `ZERO_RESULT_MYSQL_PROBE_ENABLED` is on, otherwise reported as unknown). `NO_TOPIC_MATCH` is never reported when the source was
not successfully queried; unknown coverage is stated as unknown. In a mode, `not_requested` paths are excluded from the explanation.

## Model failure
There is no model call in the classifier, so a model outage cannot change a class. Any exception while building the report leaves the answer
unchanged (`enrich_signal_report` fails safe).

## Limits
Rule-based English patterns: recall on indirect phrasing is limited; non-English text falls to GENERAL_DISCUSSION. Precision on real data has **not** been measured
(no labelled production sample was available).

## rules-3: measured improvement, discovery routing, honest report (master completion mission)
**Measured on labeled synthetic sets** (`evaluation/`, see its README for exactly what this does and does not prove): held-out accuracy 0.542 to 0.896 (95% CI 0.78-0.95), development 0.632 to 0.947; false buyers 0 in 126 non-demand posts; every quote verbatim. **No real-world accuracy is claimed.** The recall problem was the weakness (rules-2 was precise but missed most genuine demand and problems); rules-3 adds recommendation requests ("please recommend", "any recommendations for", "anyone know a ... ?", "I'd happily pay for"), switching and comparison language, concrete problem phrasing ("costing us", "keeps crashing", "is a mess"), market-change statements ("increasingly", "price increases", "demand for"), and a **product-context gate**: a demand or competitor reading needs a product/service context in the post ("What books changed how you think?" and "reflectors versus refractors" are no longer commercial signals). A third-person problem with no request is now a PROBLEM_SIGNAL (with a limitation noting no first-person statement) instead of a MARKET_SIGNAL. The generic phrase "the market is growing" stays GENERAL_DISCUSSION (no evidence).
**General-discovery routing** (`is_general_discovery_request`): besides the "sample / recent threads" cues, market-level questions are now recognised ("What problems are people discussing today?", "What are people willing to pay for?", "What unmet needs are appearing?") when the message has a question starter, a core discovery term and **only** words from a closed generic vocabulary. Any topic word (a product, industry, brand: "invoicing", "healthcare", "Stripe") keeps it a topic search. Measured: 20/20 of the brief's discovery questions recognised (these were used to design the rule), 0/20 topic searches captured (10 adversarial). Before: 2/20.
**Report honesty**: the signal report classifies at most 25 posts. Previously its statement said "14 of 25 retrieved posts" when 47 were retrieved. It now says "first 25 of 47 retrieved posts ... the remaining 22 were not classified".
**Zero-result situations** now also include `UNSUPPORTED_MODE` (Live on a source with no live path: not searched, not probed) and `PARTIAL_RETRIEVAL` (one source failed while another merely found nothing: absence cannot be concluded). Historical mode on Hacker News reports its stored posts, not a live fetch that never ran. "Insufficient evidence" is expressed by GENERAL_DISCUSSION, the per-item limitations and the report statement, not by a zero-result code.
Cost: classification is 1.69x slower than rules-2 (170 to 286 ms per 1,000 posts on the development machine), about 0.29 ms per post.

## rules-2 changes (re-audit)
Added: price/quality complaints and "I hate ..." (PROBLEM), completed switches and "A vs B" comparisons (COMPETITOR, with its own explanation: competitive context, not a purchase request), a sentence-initial "Looking for ..." (DIRECT_DEMAND). Narrowed: the catch-all "market/trend/industry/report" rule no longer makes generic chat a MARKET_SIGNAL (only concrete events do). Provenance (`retrieval_path`) is copied to each item for display and is never an input to the class.

## Post-mission review: evaluation record and discovery routing (all SYNTHETIC; none of it is real-world accuracy)

Classifier rules-3, hand-written labelled posts, Wilson 95% intervals. `python evaluation/run_eval.py --data <file>` reproduces every number.

| Set | n | Accuracy (95% CI) | Macro-F1 | False-buyer rate | Status |
|---|---|---|---|---|---|
| v1 development | 114 | 0.956 (0.90-0.98) | 0.96 | 0/90 | rules were tuned on it: optimistic |
| v2 "held-out" | 48 | 0.896 (0.78-0.95) | 0.89 | 0/36 | same author and session as the rules: **not independent** |
| v3 fresh | 48 | **0.729 (0.59-0.83)** | 0.71 | 0/35 (CI 0-0.10) | written after the rules were frozen, evaluated once, never tuned on: the most honest estimate here |

v3 per class: DIRECT_DEMAND P 1.00 / R 0.77 / F1 0.87; PROBLEM_SIGNAL 0.86 / 0.67 / 0.75; MARKET_SIGNAL 0.80 / 0.57 / 0.67; COMPETITOR_SIGNAL 1.00 / 0.43 / 0.60; GENERAL_DISCUSSION 0.52 / 1.00 / 0.69. Confusion (rows label, cols predicted, order DIRECT, PROBLEM, MARKET, COMPETITOR, GENERAL): DIRECT 10-1-0-0-2; PROBLEM 0-6-0-0-3; MARKET 0-0-4-0-3; COMPETITOR 0-0-1-3-3; GENERAL 0-0-0-0-12. The dominant error is a real signal falling back to GENERAL_DISCUSSION (lost recall), not a non-buyer promoted to a buyer. v1 and v2 per-class tables and confusion matrices are printed by the command above. With 7-13 items per class the per-class intervals are wide; do not read the third decimal.

Do not tune against v3 or the held-out routing set v4; they are regression benchmarks (floors in `tests/test_classifier_evaluation.py`, `tests/test_discovery_routing.py`). The evaluator scores a class that is never predicted but has support as F1 = 0 (it used to be dropped from macro-F1).

### Discovery routing (is the message a broad request with no topic?)
`signal_intel.is_general_discovery_request`: a closed vocabulary; any word outside it makes the message a normal topic search. A miss is safe (the normal router handles it); a false positive would drop the user's topic, so precision is what is protected.

| Set | Role | Recall | False positives |
|---|---|---|---|
| `discovery_routing_queries.json` (113) | development, tuned | 51/53 | 1/60 (`Who needs software?`, genuinely ambiguous) |
| `discovery_routing_queries_v2_fresh.json` (67) | first fresh set, then used for one tuning round | 17/31 before, 21/31 after | 0 |
| `discovery_routing_dev_v3.json` (86) | **separate development set for the second extension** | 29/49 before, 46/49 after | 0/37 |
| `discovery_routing_heldout_v4.json` (90) | **new held-out set, evaluated once** | **31/50 (62%)** | **0/40** |

The honest expectation for an unseen broad question is therefore roughly 55-65% recall with no topic captured in the 40-80 topic-bearing and adversarial queries tried, not the 90%+ the development sets show. The remaining misses go to the ordinary router and still return results. The vocabulary approach will keep missing new phrasings; the structural alternative (a model-based router) costs a paid call per message and was not built.

### Closure pass: third routing extension, a grammar tier, a router-assisted safety net (all synthetic)
- **Why:** held-out v4 showed 62% and a new, more varied development set (v5) showed only **7/50 (14%)** with the closed vocabulary alone, i.e. the vocabulary approach was fitted to the phrasings its author tends to write.
- **Change (written from dev v5 misses only; no held-out query was read):** (1) a grammar tier: an otherwise-generic market-level question may contain words outside the vocabulary when every one is a verb/adverb form (-ing/-ed/-en/-ly/-est) standing before the first topic-introducing preposition (about, for, with, from, to, of, in, on...); digits, mid-sentence capitalised words and any non-verbal unknown word still reject it; (2) more generic and core words (grumbling, wishing, demanding, stuck, ...); (3) `keywords_are_generic_discovery`: when the router's own keywords are all generic discovery words and at least one is a core discovery term, the request is treated as general discovery instead of a narrow keyword search for those words (never when a website context is in use; one topic-bearing keyword prevents it).
- **Results:** dev v5 7/50 -> 41/50 (optimistic: tuning set). **Held-out v6 (written after the change was frozen, evaluated once): recall 28/49 (57%), false positives 0/41.** v4 31/50 before and 36/50 after the change; because it was run a second time it is retired as a held-out set. Topic leakage (a topic or non-discovery message routed as broad) is 0 across the 41 + 40 + 37 negatives of v6, v4 and v5 and 0 of the older negatives except the genuinely ambiguous "Who needs software?".
- **Honest expectation:** roughly 55-60% of unseen broad questions are recognised by the message detector alone; the router-assisted net adds some on top but is **not measured** (the router is a paid model; only the pure function and a stubbed-router route test were run). A miss still returns ordinary results, but it can run as a narrow keyword search; the user-visible mitigation is the answer's own statement of the keywords/mode used. Recall will keep depending on phrasing until a model-based classifier is used (a paid call per message; not built, needs your approval).
- **Evaluator output** now also prints the row-normalised confusion matrix and the most frequent errors. v3 example: PROBLEM, MARKET and COMPETITOR signals fall back to GENERAL_DISCUSSION 3 times each and DIRECT_DEMAND twice; no non-demand post became DIRECT_DEMAND.
- **Questions are not purchase intent:** tests assert that "What is a CRM?", "Does anyone know how Stripe fees are calculated?", "Is Notion better than Obsidian?" and similar are never DIRECT_DEMAND, and that every report item keeps the quoted text (`confirmed`), the interpretation (`inferred`), the caveats (`limitations`) and the unknowns (`opportunity.unknown`) apart.
