# Lead MVP: staging verification plan (NOT EXECUTED)
> SUPERSEDED by `FLINTEL_REAL_VALIDATION_PLAN.md` (more exact, with a runnable harness). Kept for history.

Nothing below has been run. Every step needs the owner's approval, a STAGING environment (never production), and its own credentials. Do not paste secrets
into logs or reports. A source counts as verified only when the step was actually executed and its output kept.

## 0. Preconditions
- A staging MongoDB (separate database), staging OpenAI key with a spending cap, `HACKERNEWS_ENABLED=true` with its guards, `SESSION_SECRET_KEY` set.
- A test account (email) that is not a customer. `LEAD_SCANS_PER_DAY` and `LEAD_PROFILE_FETCHES_PER_DAY` set low (3 and 3).
- Record: commit hash, date, who ran it. Keep the output of every command below.

## 1. MongoDB persistence and atomicity
1. Start the app; confirm the indexes exist: `flintel_business_profiles(owner_key unique)`, `flintel_lead_scans(owner_key, scan_id unique)`, `flintel_leads(owner_key, source_id unique)`, `flintel_leads(owner_key, lead_id unique)`, `flintel_lead_quota(owner_key, slot unique)`.
2. Save a profile, reopen it from a second browser session of the same account (persisted), and from a different account (must not see it).
3. Atomic cap: with the cap at 3, fire 20 concurrent `POST /leads/find` from 2 separate processes (two uvicorn workers). PASS = exactly 3 scans saved, the rest redirect to `error=cap` or `error=busy`; `flintel_lead_quota` shows `count == 3`.
4. Lock: two simultaneous scans of one account: one runs, one gets `error=busy`; the lock expires by itself after 90 s if a worker is killed mid-scan (kill it, wait, scan again).
5. Re-scan keeps status: mark a lead contacted, scan again, the lead keeps `contacted` and is not duplicated.

## 2. Hacker News live retrieval (public Algolia API, no key)
1. One scan with only Hacker News selected, 30 days. Record: requests made, hits fetched, records after filtering, status (`ok|empty|partial|failed|rate_limited`), wall time.
2. Open 10 returned leads' source links by hand: each must be a real story whose text contains the displayed quote and whose author/date match.
3. Failure path: block `hn.algolia.com` for the app only (network policy) and scan: source must show Failed, the page must say the scan was not complete.
4. Limits: confirm stories only (no comments) and note the date range the API can serve.

## 3. Reddit historical archive and embedding service
1. One scan with only Reddit selected. Record: stored posts matched, embedding calls made, total tokens reported by the provider, cost in USD from the provider's usage page (not estimated), wall time.
2. Open 10 returned links by hand: real posts, matching text, author and date. Check the archive's actual date range (oldest and newest stored post) and write it down.
3. Verify the time-out behaviour: with a deliberately small `source_deadline`, the source must show Failed ("did not finish"), never Searched.
4. Verify that a scan does NOT write to or modify the stored posts collection.

## 4. Latency and cost
For each of 10 scans (varied businesses) record: retrieval ms per source, qualify ms, total ms, records retrieved, leads shown, provider cost. Report median and worst case. A scan must finish under 45 s (the request hard limit) or be reported as failed.

## 5. Website analysis (safe fetch)
Against staging only: a normal public site (analysed), a site that times out (profile still saves, status unavailable), `http://169.254.169.254/`, `http://localhost`, a URL that redirects to an internal address (all refused, nothing fetched, profile still saves with status unsafe). Confirm the application logs show no secrets.

## 6. Lead quality on REAL posts (the most important check)
Take 100 real posts from the staging sources for one real business profile supplied by the owner. Two people label each post (lead / review / not a lead) WITHOUT seeing Flintel's output; then compare. Report precision and recall with the labelled counts and the disagreements. Until this is done the synthetic numbers in `evaluation/LEAD_EVAL_RESULTS.md` say nothing about real accuracy.

## 7. Sign-off
Each section: PASS / FAIL / NOT RUN with evidence attached. Only after sections 1-6 pass should anyone discuss metering scans in the credit system or charging for the feature.
