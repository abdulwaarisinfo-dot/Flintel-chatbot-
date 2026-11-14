# Lead engine evaluation (SYNTHETIC)

All items were written by the same author as the engine. The numbers describe behaviour on THESE phrasings; they are not real-world accuracy.
`v1` = the engine before the intent layer (saved copy); `v2` = current. Surfaced = High potential or Relevant. Regenerate: `python evaluation/run_lead_eval.py [--engine PATH] [--split dev|held|fresh|baseline]`.

## By split

| split | n | positives | v1 recall | v2 recall | v1 precision | v2 precision | v1 negatives shown at all | v2 negatives shown at all |
|---|---|---|---|---|---|---|---|---|
| baseline | 60 | 36 | 0.47 (17/36) | 1.00 (36/36) | 0.94 | 1.00 | 1/24 | 3/24 |
| dev | 93 | 51 | 0.37 (19/51) | 0.92 (47/51) | 0.95 | 1.00 | 1/24 | 3/24 |
| held | 87 | 51 | 0.31 (16/51) | 0.92 (47/51) | 0.94 | 0.98 | 0/24 | 0/24 |
| fresh | 54 | 30 | 0.23 (7/30) | 1.00 (30/30) | 0.88 | 1.00 | 1/18 | 2/18 |
| ALL | 234 | 132 | 0.32 (42/132) | 0.94 (124/132) | 0.93 | 0.99 | 2/66 | 5/66 |

## By business (ALL items)

| business | v1 recall | v2 recall | v1 precision | v2 precision | v2 false positives | v2 negatives shown at all |
|---|---|---|---|---|---|---|
| shopify | 0.35 (8/23) | 1.00 (23/23) | 1.00 | 0.96 | 1 | 0/11 |
| saas | 0.57 (12/21) | 0.86 (18/21) | 0.80 | 1.00 | 0 | 2/11 |
| marketing | 0.43 (9/21) | 0.95 (20/21) | 1.00 | 1.00 | 0 | 1/11 |
| designer | 0.24 (5/21) | 0.95 (20/21) | 1.00 | 1.00 | 0 | 0/11 |
| plumber | 0.22 (5/23) | 0.96 (22/23) | 1.00 | 1.00 | 0 | 0/11 |
| skincare | 0.13 (3/23) | 0.91 (21/23) | 1.00 | 1.00 | 0 | 2/11 |

## By intent kind (positives: recall; others: shown)

| kind | expected | v1 | v2 |
|---|---|---|---|
| direct_request | lead | 10/18 surfaced | 18/18 surfaced |
| recommendation | lead | 10/18 surfaced | 17/18 surfaced |
| problem | lead | 0/17 surfaced | 16/17 surfaced |
| provider_search | lead | 8/12 surfaced | 12/12 surfaced |
| comparison | lead | 7/15 surfaced | 15/15 surfaced |
| buying_question | lead | 1/17 surfaced | 17/17 surfaced |
| replace_provider | lead | 6/17 surfaced | 17/17 surfaced |
| indirect | lead | 0/12 surfaced | 6/12 surfaced |
| definition | none | shown 0/7, surfaced 0 | shown 0/7, surfaced 0 |
| vendor_promo | none | shown 0/11, surfaced 0 | shown 0/11, surfaced 0 |
| offtopic_lookalike | none | shown 2/9, surfaced 2 | shown 5/9, surfaced 0 |
| chat | none | shown 0/8, surfaced 0 | shown 0/8, surfaced 0 |
| news | none | shown 0/9, surfaced 0 | shown 0/9, surfaced 0 |
| validation_probe | none | shown 0/6, surfaced 0 | shown 0/6, surfaced 0 |
| offering_services | none | shown 0/5, surfaced 0 | shown 0/5, surfaced 0 |
| tutorial | none | shown 0/8, surfaced 0 | shown 0/8, surfaced 0 |
| ambiguous | review | shown 1/21, surfaced 1 | shown 1/21, surfaced 1 |
| historical | review | shown 10/15, surfaced 0 | shown 15/15, surfaced 0 |
| non_english | lead | 0/6 surfaced | 6/6 surfaced |
| job_seeker | none | shown 0/3, surfaced 0 | shown 0/3, surfaced 0 |

## Remaining misses (v2, all items)

- saas-recommendation-2 (held): got Needs review | What do you use to track candidates?
- saas-indirect-1 (dev): got excluded | Offer letters getting lost
- saas-indirect-2 (held): got excluded | Growing fast, hiring 10 people
- marketing-indirect-2 (held): got excluded | Competitors have more followers
- designer-indirect-1 (dev): got excluded | Launching in 6 weeks with no brand
- plumber-indirect-1 (dev): got excluded | Cold radiators all winter
- skincare-problem-2 (held): got Needs review | Face stings after every product
- skincare-indirect-1 (dev): got excluded | Winter is coming and my hands crack

## Non-surfaced-as-expected items shown as Needs review (v2): negatives shown

- saas-offtopic_lookalike-1: Needs review / SERVICE_REQUEST | Track my package
- saas-offtopic_lookalike-2-f: Needs review / SERVICE_REQUEST | Parcel tracking app
- marketing-offtopic_lookalike-1: Needs review / PROVIDER_SEARCH | Media coverage
- skincare-offtopic_lookalike-1: Needs review / SERVICE_REQUEST | Dry January
- skincare-offtopic_lookalike-2-f: Needs review / SERVICE_REQUEST | Dry cleaners nearby

## Off-domain false-positive stress (245 posts written for other businesses x 6 profiles = 1470 pairs)

v1 surfaced 49 (3.33%); v2 surfaced 4 (0.27%) and shows 45 as Needs review (v1: 12). Regenerate: `python evaluation/run_offdomain_eval.py [--engine PATH]`.

## Caveats

- The `fresh` split was authored after the engine design and scored once (first look: recall 0.87 = 26/30, precision 1.00); four general bugs it exposed were then fixed (vendor guard on "available", price phrasing, REQUEST verbs, title authorship) and it now scores 30/30 - it is no longer an unseen set.
- `dev` was used while developing; `held` was scored after a first round of work (recall 0.86 then) and then used to find two general bugs (definition guard on "what does X cost", job-seeker pattern) - treat it as seen.
- Remaining misses are indirect expressions (a symptom with no ask, e.g. "cold radiators all winter"): the engine needs the author's problem to share words with the profile.
- Non-English works only when the profile has terms in that language; 6 items, 3 businesses.
- Real-world precision and recall are unknown.

