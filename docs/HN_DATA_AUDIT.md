# Hacker News: collection, storage and retrieval audit
Evidence level: **code and configuration only.** This session had no database, OpenAI or hosting credentials (no MongoDB/MySQL/OpenAI variables in the environment), so
**no record counts, newest/oldest timestamps or collection-health numbers could be read from a real data source and none are given here.**
`tools/hn_data_status.py` is a read-only, counts-only tool that produces them when you run it against a database you choose (validated here on synthetic data only).

## The two Hacker News paths (they are different things)
| | Collector ("Background Service #1", repo `flintel-traveller-agent`, package `hackernews/`, commit a73dcea 2026-10-12) | Chatbot live fetch (`hackernews_live.py`, in this repository) |
|---|---|---|
| Endpoint | Algolia `https://hn.algolia.com/api/v1/search_by_date`, `tags=story` and `tags=comment` | Algolia `search` / `search_by_date`, `tags=story`; one `search_by_date` call with `hitsPerPage=1` to probe coverage on empty results |
| Post types | Stories (Ask HN / Show HN / link / text stories are all stories) **and comments** (stored as platform `hackernews_comment`) | Stories only |
| Selection | **Keyword-gated:** one query per keyword of a static list in the collector's `index.py`; with `HN_STRICT_MATCH` (default on) a hit is stored only if the keyword appears in its title/text | Router keywords of the user's own prompt, or (general discovery) no keyword |
| Storage | MongoDB `flintel_signals` (and optionally MySQL): `message_id=hackernews_<id>`, `platform`, `title`, `text` (title + story text), `post_url` (`news.ycombinator.com/item?id=`), `username`, `score`, `num_comments`, `created_utc`, `fetched_at`, `search_keyword`, embedding | Nothing is stored: results are used for that search (cached in process for 120 s) |
| Dedupe | existence check on `message_id` before insert (and a unique-key fallback) | by Algolia `objectID` |
| Frequency | poll interval 600 s; 100 keywords per cycle (rotating cursor in `flintel_hn_cursor`); 1.0 s between keywords; 2 pages x 50 hits per keyword and kind; backoff 300 s on HTTP 429; first run looks back 180 days, then incremental by `created_at_i > last seen` per (keyword, kind) in `flintel_hn_state` | per request, bounded by `HN_FETCH_TIMEOUT_SECONDS` / pages / hits (config) |
| Heartbeat | `flintel_service_status` document `service="hackernews_poller"` | none (no collector) |

## Findings (from the code; the first two explain "no stored post matched")
1. **The stored Hacker News corpus is keyword-targeted, not a general sample.** Only posts that matched a configured keyword are stored. A request for "a general unfiltered sample of recent threads" can therefore never be answered from stored Hacker News data, whatever the retrieval code does. (Fixed for the chatbot by fetching a general sample live; see the report.)
2. **The keyword list in the repository snapshot has 9,910 entries and looks e-commerce oriented** (e.g. "3PL", "BigCommerce", "Shopify", "SMS marketing", "WooCommerce"). The file's own header says the list is empty by default, so check what the deployed list actually is. At 100 keywords per cycle, about 15 minutes per cycle (600 s wait plus roughly 200-400 s of requests: 2 kinds x 1 s gap each), one full rotation is about 99 cycles, i.e. **roughly a day: each keyword is refreshed about once per day** (computed from configuration defaults, not measured). AI-agent / customer-support topics are only stored if those words are in the list.
3. **Stored comments were never retrievable under an HN selection:** the chatbot's platform filter listed only `hackernews`, not `hackernews_comment`. Fixed (`logics._PLATFORM_DOC_VALUES`).
4. The collector advances its checkpoint over posts skipped by the strict keyword match (by design), so skipped posts are not revisited.
5. Not verifiable here: whether the collector is running, its last successful cycle, failures, malformed or duplicate records, and how many records are embedded. Run `tools/hn_data_status.py`.

## Where data can be lost between the API and a result
Algolia -> collector fetch (2 pages cap, keyword gate, strict match) -> `_save_signal` (needs title; embedding step) -> `flintel_signals` -> chatbot retrieval (embedding similarity threshold, time window, platform filter, text-required rule) -> intent bridge / caps -> answer. The new zero-result explanation reports which stage emptied the result when it can know (see report).
