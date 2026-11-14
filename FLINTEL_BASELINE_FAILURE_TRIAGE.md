# Baseline test failures: triage (38 failures + 3 collection errors)

All 41 fail on the pre-existing baseline (`5f39c61`) and none was introduced by the lead-MVP work. No test was weakened, skipped or deleted to get this list. Nothing here was "fixed" by editing a test.
Owner decisions are needed because each group is either a feature that was removed/moved or an unfinished item, so the right fix is a product decision, not a test edit.

| Group | Tests | Observed cause | Recommended owner action |
|---|---|---|---|
| Keep-alive | `test_keepalive.py` (9) | `config.KEEPALIVE_ENABLED` and `_start_keepalive` do not exist; the endpoint returns 404 | Feature removed or never merged. Restore it, or delete the stale test file in a reviewed change |
| Realtime streaming | `test_realtime_streaming.py` (8) | `routes.analyze_with_claude_stream`, `_partial_json_string_value`, `_ReadableFieldStreamer` do not exist | Streaming code was moved or removed. Same decision |
| Live/saved wording | `test_active_intent_frontend.py` (8), `test_historical_only.py` (2), `test_reddit_live_endpoint.py` (3) | Templates say "Live Reddit..." where the tests expect "Previously saved..." wording | Wording drift against a "historical only" policy: confirm the policy and align the template (a product/copy decision) |
| Keyword chips | `test_keyword_chips.py` (5) | Tests look for markup substrings (`ValueError: substring not found`) that the templates no longer contain | Template refactor left tests stale; update tests to the new markup after review |
| Platform pill | `test_platform_multiselect.py` (1) | Same template-markup drift | As above |
| Small-talk | `test_smalltalk_fast_path.py::[salaam]` (1) | Code answers "salaam" with a reply; the test expects it to go to the router | Behaviour changed deliberately or not; decide and align |
| Turn queue | `test_turn_queue_limits.py` (1) | The deferred backfill bound is not enforced (6 extra rows beyond the limit) | Unimplemented governance item; real gap, implement or schedule it |
| Collection errors | `test_lead_qualification.py`, `test_option_a_guard.py`, `test_option_a_followup.py` (3) | They import `tests.test_option_a_outreach`, which does not exist | Missing module; restore it from history or remove those tests deliberately |

Totals: keepalive 9 + streaming 8 + live wording 13 + chips 5 + pill 1 + smalltalk 1 + queue 1 = 38 failures, plus 3 collection errors.
The failing ID list is kept in the session scratchpad (`m/base_sorted.txt`); the final run is compared against it by ID in the report.
