# Staging notes (nothing was deployed; no staging environment was available)
Missing access (unchanged from earlier sessions): an isolated staging host, MongoDB/MySQL, an OpenAI key, OAuth/billing test credentials; this sandbox cannot reach api.openai.com or hn.algolia.com.

New / relevant settings (names only):
- `SIGNAL_REPORT_ENABLED` (default **false**): adds the five-class signal report to answers. Turn on in staging to review it.
- `HACKERNEWS_ENABLED` (default false) plus `HN_FETCH_TIMEOUT_SECONDS`, `HN_MAX_PAGES_PER_KEYWORD`, `HN_HITS_PER_PAGE`: Hacker News live fetch (already on `main`).
- `RECENT_DEFAULT_WINDOW_DAYS` (existing, default 30): the window a general-discovery request gets when it names no period.

Staging checks to run (a real browser, signed in):
1. With `HACKERNEWS_ENABLED=true`, select Hacker News and send "Yes, run a general unfiltered sample of recent threads." Expect a sample of recent stories with source links (not "No stored post matched this topic"), search status line saying it was sampled live.
2. Same with Reddit selected: expect Reddit's own unfiltered sample (3 posts by default: `UNFILTERED_DEFAULT_LIMIT`).
3. Ask for a period with no stories (for example a day far in the past): expect "No Hacker News posts in the requested date range" with the newest available date.
4. With `SIGNAL_REPORT_ENABLED=true`: answers show "What the retrieved posts are" with a class label and an exact quotation per post.
5. Run `tools/hn_data_status.py` against the staging database (read-only) and compare with the collector.
Rollback: revert the feature commits on the staging branch, or set `HACKERNEWS_ENABLED=false` / `SIGNAL_REPORT_ENABLED=false` (both fail closed). No schema change was made.
