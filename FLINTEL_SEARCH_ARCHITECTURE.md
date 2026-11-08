# FLINTEL Search Architecture - Live / Historical / Combined

## Modes
| Mode (label) | Meaning | Retrieval |
|---|---|---|
| Live | newly available discussions from sources with a working live API | live sources only. Today that is Hacker News (Algolia) when the platform is registered and selected. With no live source the result is empty and coverage says `unavailable`; nothing is simulated |
| Historical | stored conversations | stored path only (Mongo / MySQL / embeddings). Never presented as current |
| Combined | both | stored path + live source in parallel, merged and de-duplicated, provenance per result |

**Default: Combined** (visible, pre-selected in the UI). It equals the application's behaviour before modes existed, so omitting the field
changes nothing. A mode the user picks is stored on the message as `search_mode`.

## Flow
```
UI control (#search-mode-input, localStorage-remembered)  -> POST /search  search_mode=live|historical|combined
routes.search: normalize_mode (allow-list) -> message.search_mode (only if the user sent one)
stream / background fill-in: with search_mode_scope(mode): get_evidence_with_topup -> get_matched_signals
logics.get_matched_signals (outermost, scope-aware)
   historical : _get_matched_signals_core            (HN live suppressed)
   live       : _hn_only_matched | unavailable       (stored path not touched)
   combined   : _get_matched_signals_hn  + search_modes.merge (canonical-URL dedup, provenance)
answer JSON: + search_mode {mode, label, live, historical, counts, partial, failed_paths, notes}   (only when a mode was chosen)
renderer: appendSearchMode (mode, per-path status, "N live, N historical, N found by both", notes)
```
The scope is thread-local and entered inside the code that runs the retrieval (so background scans see it); outside a scope the function is the
previous one, byte for byte.

## Deduplication
`search_modes.canonical_url`: lower-case host without `www.`/`old.`/`m.`/`np.`; Reddit posts by post id (comment permalinks stay distinct from the
post); `redd.it` short links; Hacker News by `item?id=`; tracking parameters and fragments removed. `post_identity` falls back to platform+id.
A post found by both paths is one result with `retrieval_path = both`. Counting is on the merged list, so no post is counted twice.

## Partial failure
Each path reports `ok | empty | partial | failed | rate_limited | unavailable | not_requested`. If one path fails in Combined the other's
results are returned and `partial=true` with `failed_paths`. A path that was not requested is `not_requested`, never `empty`.

## General discovery ("show me recent discussions")
`signal_intel.is_general_discovery_request` -> `unfiltered=True`, no keywords, default recent window. Honoured by every mode: Historical passes
`unfiltered` to the stored path, Live to the live sample (`general=True`), Combined to both (stored sample + live sample). Topic search is unchanged.
Stored Reddit samples stay capped (3) by the existing `UNFILTERED_DEFAULT_LIMIT`; HN stored data only covers collector keywords (see audit).

## Cache isolation
Evidence cache key = `topic_key` for Combined (unchanged), `topic_key::historical` / `topic_key::live` otherwise.

## Limits that remain
Live is a bounded fetch, not an archive. Reddit has no live path. Per-card provenance badges are not rendered; counts are in the summary line.

## Per-result provenance and unsupported sources (hardening)
- Every result in a mode scope has `retrieval_path` = `live | historical | both`; dedup fills missing fields on the kept record from the duplicate and never overwrites (the stored record's URL and fields win, live-only fields such as points are kept).
- The answer's `search_mode` block adds `provenance` (URL -> path, only for posts that carry metadata) and `unsupported_sources`. The page tags each rendered card LIVE / HISTORICAL / BOTH; cards without metadata are left unlabelled.
- Source/mode support: Reddit = Historical only; Hacker News = Live (stories) and Historical (stories and comments). The control's hint text explains the active mode and warns when Live is chosen with a platform that cannot serve it.
- `partial` means a path that ran **failed** (error or rate limit). A path with no live source is `unavailable` and is reported in the notes, not as a failure.
