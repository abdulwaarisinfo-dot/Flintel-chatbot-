# Real-data validation of the lead engine: 100 conversations, two blind reviewers

STATUS: NOT DONE. This folder holds the protocol and the tooling (`real_eval.py`, tested on FAKE fixtures only). No real post has been collected and nobody has labelled
anything. Until the steps below are completed and the results file exists, there is NO real-world accuracy claim for Flintel. The 234-item set in
`evaluation/lead_eval_data.py` was written by the engine's author and used during development; it is engineering feedback, not validation.

## 1. What counts as a permitted real conversation
- Public posts only, from sources whose terms allow the collection method you use. Allowed here: Hacker News stories through the public Algolia API (what the product already
  uses), and Reddit posts already stored in the application's own archive (no new scraping). Nothing behind a login, nothing from private groups or messages.
- Keep the original `text`, `url`, `platform` and `published_at` EXACTLY as retrieved. Never edit, translate or "clean up" a post. Remove nothing but nothing may be invented.
- Personal data: keep the public username only if the source shows it; the review sheets need the text and URL, not names. Store the dataset in an access-controlled place and
  delete it when the evaluation is finished; do not publish raw posts in reports (publish URLs and ids).
- Record for each post who collected it, when, how (`collected_by`, `collected_at`, `permission_note`). A post whose permission is unclear is dropped, not kept.

## 2. Sampling (independent of Flintel)
The sample must not be chosen with Flintel's help, or the result is biased.
1. Pick business categories (the six used in development are a good start: Shopify development agency, SaaS, marketing agency, freelance designer, local service business,
   e-commerce product business) and write 4-5 plain search queries per category BY HAND, without looking at Flintel's generated concepts.
2. Retrieve candidates for each query from the permitted sources and keep the newest 50 per query.
3. Sample at random (seeded; record the seed) about 17 per category, to reach 100 or more in total. Aim for each category to have at least 15 items.
4. At least 20% of the sample must be irrelevant to the category and at least 10% ambiguous; draw those from the same sources at random so they look like real noise.
5. Include a spread of conversation types: direct requests, recommendations, problem descriptions, buying/price questions, comparisons, replacement of a provider, indirect demand,
   plus vendors advertising, definitions and chatter. Record how many of each you actually got; do not force a type that does not occur.
6. Write each post as one JSON line in `posts.jsonl` with: id, business, platform, url, published_at (or null), author (optional), title, text, collected_by, collected_at,
   permission_note. Write one profile per business in `profiles/<business>.json` using the fields a user would type in the Business Profile page.

## 3. Labelling (two people, blind)
1. `python evaluation/real_eval/real_eval.py sheets posts.jsonl --out round1/` makes identical blank sheets `reviewer_A.csv` and `reviewer_B.csv`. They contain the post and
   its metadata and nothing produced by Flintel. Give each reviewer a different copy; they work alone and must not run Flintel on these posts.
2. Each reviewer fills `label` (`lead` = a current, genuine customer opportunity for that business; `review` = plausible but not provable, e.g. ambiguous or historical;
   `not_lead`) and `intent_type` (direct_request, recommendation, problem, provider_search, buying_question, comparison, replace_provider, indirect, irrelevant, ambiguous).
   Written guidance for reviewers: judge the POST against the business profile; a vendor advertising, a definition question, news and chatter are `not_lead`; an old post
   is `review` at best.
3. `real_eval.py agreement reviewer_A.csv reviewer_B.csv --out round1/` reports agreement and Cohen's kappa and writes `disagreements.csv`. Report the kappa with the results.
4. A third person (or the two reviewers together) fills `final_label`, `final_intent_type`, `adjudicator` and `reason` for every disagreement.
5. `real_eval.py merge ... --out labels_final.jsonl`, then `real_eval.py lock labels_final.jsonl`. After the lock nothing may change; the tool detects any edit.

## 4. Measuring
1. Only now run Flintel: `real_eval.py predict posts.jsonl profiles/ labels_final.jsonl labels_final.lock.json` (local, no network, no model). It records which engine files produced
   the predictions. The engine must be the one under test, unmodified since the lock; do not tune it on these results. If you change the engine afterwards, this set is no longer
   independent and a NEW set is needed.
2. `real_eval.py score labels_final.jsonl labels_final.lock.json predictions.jsonl --out results.json` gives precision, recall, false positives and false negatives with 95% Wilson
   intervals overall, by business and by intent type, and lists every false positive and false negative with its URL, plus warnings for categories with too few items.
3. Read every false positive and false negative by hand and write down why. Report review-labelled items that were surfaced as leads separately.

## 5. What a result may claim
- "On N real public conversations, two blind reviewers (kappa K) labelled X leads; Flintel surfaced S of them (recall R, CI) and P% of what it surfaced were leads (precision, CI)."
- It says nothing about other sources, languages, business types, or later dates. With 100 items and about 30 leads the intervals are wide (a recall of 0.80 on 30 leads is
  roughly 0.63 to 0.90); report them. Do not claim the evaluation is complete before `results.json` exists with `reached_100: true`.
