"""
FLINTEL — WEB SERVICE (v7 + JSON-ANALYSIS-PROMPT SWAP + CLAUDE-KEYWORD SWAP
+ BUGFIX PACK: RESULTS/ANSWER SYNC + WORD-BOUNDARY MATCHING + 2ND-LEVEL CHUNKING
+ TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE
+ CLARIFY-SELF-RESOLVE + WEBSITE-URL KEYWORD EXTRACTION
+ ROUTER INTENT REFINEMENT)
============================================================================
Everything from v3 is UNCHANGED and still works exactly as before:
  1. Take a user prompt (brand/topic/product name) from a simple web form.
  2. Generate KEYWORDS (see KEYWORD-GENERATION SWAP note near the bottom
     of this docstring — this step's SOURCE changed, nothing downstream
     of it did).
  3. Patch (upsert) them into the SAME MongoDB used by Background Service #1
     — collection `flintel_search_jobs` — with status="pending".
  4. Every search redirects straight back to the chat page (index.html),
     where results from `flintel_signals` for that topic appear inline as
     part of the same chat turn — no separate status page.
  5. Auth (Google OAuth + email/password), accounts in `flintel_users`.
  6. Chat/session memory (like Claude / ChatGPT) in `flintel_users_chat`,
     keyed by email (signed in) or an anon UUID cookie (guest), with guest
     -> email migration on sign-up/login.
  7. Matched-signal output (title + post_text + post_url + platform),
     matched purely by search_keyword against the job's generated keyword
     list, saved onto the same chat message as the original prompt.

v4 — CLAUDE ANALYSIS LAYER:
  - The matched signals from step 7 are NOT shown to the user directly as
    raw dumped output. Instead, once matched, they (title + text ONLY —
    never the URL, never platform, never job internals) are handed to
    Claude as grounding context, together with the user's own chat
    prompt.
  - Model used: claude-haiku-4-5-20251001 (cheap + fast, configurable via
    CLAUDE_MODEL env var).
  - CHUNKING: if a topic's matched posts are too many to comfortably fit
    one call, they're split into batches (CLAUDE_POSTS_PER_CHUNK posts
    per batch). Each batch gets its own cheap Haiku call that extracts
    only the grounded points relevant to the user's question from that
    batch (a "map" step). Those batch notes are then combined into one
    final Haiku call, using the same analysis system prompt, to produce
    the actual answer (a "reduce" step). Small topics skip all of this
    and go straight to a single call.
  - COST: only Claude's OUTPUT (the final answer text) is ever stored on
    the chat message. The INPUT (the matched posts) is never re-saved
    alongside it — it's already sitting in `flintel_signals` /
    reconstructable from the job's keyword list, so storing it a second
    time would be pure waste. If the answer is ever missing (message
    predates this feature, or a first attempt failed), it's regenerated
    once matched posts are available and then cached the same way.
  - POST CARDS (title + post_url) are completely unaffected — the exact
    same `results` field (title, post_text, post_url, platform) computed
    by get_matched_signals() / saved by save_signal_results_to_chat()
    keeps working exactly as it did in v3. Claude never sees post_url and
    never produces it; the card UI renders the real one directly from
    `flintel_signals`, as-is, same as before.

NOTE ON PLATFORM TARGETING (v2, unchanged):
  The search form sends which platform the user picked in the
  "All Platforms" dropdown (All Platforms / Reddit / X / Twitter / LinkedIn /
  Facebook). This does NOT change keyword generation in any way — the exact
  same generated keywords are used either way. All that happens is a new
  field, `targeting_platform`, is added to the job document:
    - "All Platforms" selected  -> targeting_platform = "all"
    - "Reddit" selected         -> targeting_platform = "reddit"
    - "X / Twitter" selected    -> targeting_platform = "x_twitter"
    - "LinkedIn" selected       -> targeting_platform = "linkedin"
    - "Facebook" selected       -> targeting_platform = "facebook"
  Background Service #1 (or a later version of it) can read this field to
  decide whether to search everywhere or restrict itself to one platform.
  This service itself does no filtering — it only tags the job.
  (Still true after the KEYWORD-GENERATION SWAP below: platform targeting
  is driven ONLY by this dropdown field, never inferred from the words
  in the user's chat message.)

NOTE: This service sends KEYWORDS ONLY. Background Service #1 searches
Reddit SITE-WIDE per keyword (no subreddit restriction) — so there is no
subreddit targeting to generate here. Wherever a matching post actually
lives on Reddit, its real subreddit is captured automatically by the
background service.

This service does NOT talk to Background Service #1 directly. The only
connection between the two is the SAME MongoDB connection string
(MONGODB_URI / MONGODB_DB) and the SAME collection names. Claude is only
ever called by THIS service, purely to (a) route/classify + now generate
keywords for "search" messages, and (b) turn already-matched signals into
an answer for the user — it never touches jobs_collection or
signals_collection itself.

Stack: FastAPI + Jinja2 templates + pymongo + Authlib (Google OAuth) +
passlib (password hashing) + Starlette SessionMiddleware (login sessions
AND the anonymous chat-owner cookie) + httpx (Claude API calls + website
fetches for the WEBSITE-URL KEYWORD EXTRACTION feature below).

Run:
    pip install fastapi uvicorn jinja2 python-multipart pymongo python-dotenv \
                authlib httpx itsdangerous "passlib[bcrypt]"
    uvicorn index:app --reload --port 8080

Required env vars (add to .env):
    MONGODB_URI=...
    MONGODB_DB=flintel_bot
    SESSION_SECRET_KEY=some-long-random-string
    GOOGLE_CLIENT_ID=...
    GOOGLE_CLIENT_SECRET=...
    ANTHROPIC_API_KEY=...
    CLAUDE_MODEL=claude-haiku-4-5-20251001   # optional, this is the default
    RESPONSE_TIMEOUT=60                      # optional, this is the default (seconds)
    MAX_POSTS_PER_PLATFORM=3                 # optional, this is the default (v7, see below)
    CLAUDE_MAX_KEYWORDS=10                   # optional, this is the default (new, see KEYWORD-GENERATION SWAP)
    CLAUDE_NOTES_PER_CHUNK=8                 # optional, this is the default (new, see BUGFIX PACK #3 below)
    STREAM_CHUNK_CHARS=3                     # optional, this is the default (new, see SIMULATED-STREAM FIX below)
    STREAM_CHUNK_DELAY_SECONDS=0.02          # optional, this is the default (new, see SIMULATED-STREAM FIX below)
    MAX_TIME_WINDOW_DAYS=3650                # optional, this is the default (new, see TIME-WINDOW FEATURE below)
    CLAUDE_TOPIC_RESOLVER_MAX_TOKENS=400     # optional, this is the default (new, see CLARIFY-SELF-RESOLVE FEATURE below)
    MAX_WEBSITE_KEYWORDS=20                  # optional, this is the default (new, see WEBSITE-URL KEYWORD EXTRACTION FEATURE below)
    WEBSITE_FETCH_TIMEOUT_SECONDS=15         # optional, this is the default (new, see WEBSITE-URL KEYWORD EXTRACTION FEATURE below)
    WEBSITE_FETCH_MAX_CHARS=8000             # optional, this is the default (new, see WEBSITE-URL KEYWORD EXTRACTION FEATURE below)
    CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS=400    # optional, this is the default (new, see WEBSITE-URL KEYWORD EXTRACTION FEATURE below)

── v4.1 FIX ───────────────────────────────────────────────────────────────
Only ONE behavior changed from the v4 file above: the /search route used to
always `RedirectResponse(url="/")` no matter what, which bounced every
follow-up search back to the home screen instead of keeping the user on the
chat thread they were just talking in (like Claude/ChatGPT do). It now
redirects to `/chat/{chat_id}` — the same chat the message was just saved
into — falling back to "/" only if chat bookkeeping itself failed. Nothing
else in that update was touched.

── v4.2 FIX ───────────────────────────────────────────────────────────────
FastAPI's auto-generated API docs are now disabled: the FastAPI(...) app is
constructed with docs_url=None, redoc_url=None, openapi_url=None, so
/docs, /redoc, and /openapi.json all 404 instead of publicly exposing every
route, request/response shape, and internal field name. Nothing else in
this file was touched.

── v4.3 FIX ────────────────────────────────────────────────────────────────
Adds ONE new capability: deleting a single chat. A new
`POST /chat/{chat_id}/delete` route lets the current owner (signed-in
email, or guest UUID) remove exactly ONE of their own chats —
never every chat belonging to them, and never a chat belonging to a
different owner. It reuses the exact same owner-scoping pattern already
used by get_chat_session()/view_chat() (filtering by BOTH chat_id AND
owner_key on the Mongo query itself, not just checking after the fact),
so a guest or another account can't delete a chat by guessing its id —
identical protection to how a chat is already read. If the deleted chat
happened to be the currently active one, `active_chat_id` is cleared from
the session so the home page falls back to showing no active chat instead
of a stale/missing one. Nothing else in this file was touched.

── v4.4 FIX ───────────────────────────────────────────────────────────────
POST /chats/new now reuses the currently active chat instead of creating a
brand-new empty one if that active chat belongs to the same owner and has
zero messages yet — avoids piling up dead, message-less chats in the
sidebar from repeated "New chat" clicks. Nothing else in that update was
touched.

── v5 FEATURE ───────────────────────────────────────────────────────────
Adds a ROUTING step in front of everything else in POST /search. A single
cheap Claude call now decides whether the user's message is:

  - a genuine SEARCH request (wants social-listening data pulled about a
    brand/product/topic) -> the ENTIRE v1–v4.3 pipeline above runs exactly
    as it always has: a job is patched into flintel_search_jobs, and
    matched flintel_signals posts get analyzed by Claude — all as before,
    byte-for-byte the same functions (enqueue_search_job,
    add_search_to_chat, get_matched_signals, analyze_with_claude,
    save_signal_results_to_chat, save_claude_answer_to_chat). The ONLY
    thing that changed is WHERE the keyword list handed to
    enqueue_search_job()/get_matched_signals() comes from — see the
    KEYWORD-GENERATION SWAP note further down.

  - a plain CHAT message (a greeting, small talk, a general question,
    "what's up", a follow-up about something already discussed, etc.) ->
    NOTHING is generated or patched into flintel_search_jobs for it at
    all — no keywords, no job, no signal matching. Claude answers
    it directly (in the SAME routing call, to save a round trip) and the
    reply is saved onto the chat as a new lightweight message
    ("message_type": "chat"). The existing search pipeline is never
    touched or invoked for these messages.

Also adds a short, plain-PYTHON (no extra Claude call) rolling SUMMARY
kept on each chat doc — one condensed line per turn, capped to the last
CHAT_SUMMARY_MAX_TURNS turns — so Claude gets cheap conversational
continuity (for the routing decision, and for any chat-type reply)
without ever being sent the full raw message history for the chat.

Any failure anywhere in the new routing step (Claude API error, bad JSON,
session hiccup) safely falls back to the "search" path, so this new
feature can only ever add a shortcut — it can never break or block the
pre-existing search pipeline, which stays the safe default on any doubt.

── v6 FEATURE ─────────────────────────────────────────────────────────────
Adds TWO small, self-contained additions on top of v5. Nothing else in
this file — no other route, function, or behavior — was touched.

1. RESPONSE TIMEOUT (new `RESPONSE_TIMEOUT` env var, default 60 seconds):
   Previously, a search-type message with no matched signals yet just sat
   forever with `claude_answer = None` — home()/view_chat() would keep
   silently retrying `get_matched_signals()` on every page load, with no
   answer ever shown if the background service never found anything for
   that topic.
   Now, `_fill_in_message_outputs()` checks how long it's been since the
   message's own `requested_at` timestamp. Once that exceeds
   RESPONSE_TIMEOUT seconds with STILL no matched posts, it calls
   `analyze_with_claude(query, [])` exactly once — the SAME function
   already used for real answers, just handed an empty post list. That
   function already has a built-in "no posts were found, say that plainly"
   branch — so this reuses existing, already-reviewed prompting instead
   of adding a new one. That answer is cached via the existing
   save_claude_answer_to_chat()/append_to_chat_summary() calls, so it's
   generated once and never re-billed. If matched posts show up LATER
   (background service just took longer than 60s), that's fine too:
   `needs_answer` only re-triggers if `claude_answer` is still falsy, so
   once the timeout answer is cached, it stays as the final answer for
   that message, same caching rule as every other answer in this file.
   Before the timeout is reached, behavior is 100% unchanged: still just
   silently waits and retries on next page load, exactly like v1-v5.

2. ABUSE / HARMFUL CONTENT BLOCKING (folded into the existing v5 router):
   The SAME single cheap Claude call in classify_and_maybe_chat() now also
   screens for abusive, harassing, hateful, sexually explicit, threatening,
   or otherwise harmful messages — no second API call, no new service, it
   just adds a third possible classification alongside "search" and "chat":
   `{"intent": "blocked", "reply": "<short, polite decline>"}`.
   A "blocked" message never reaches the search pipeline (no keywords, no
   job) and never reaches the plain-chat fallback either — it's saved onto
   the chat as a normal lightweight "chat"-type message (same shape/field
   as v5's chat messages, so no template changes are needed anywhere) whose
   `claude_answer` is just the polite decline text, with CLAUDE_BLOCKED_
   FALLBACK_REPLY used as a safety-net string if the router flagged
   "blocked" but didn't return usable reply text.
   Same safety rule as the rest of the v5 router: ANY failure in this step
   (API error, bad JSON, timeout) still falls back to intent="search", so
   this is a best-effort courtesy layer on top of the product, not a
   guaranteed content filter — it can only ever add a shortcut/decline on
   top of the existing pipeline, it can never be the reason a genuine
   search silently fails to run.

── v7 FEATURE ──────────────────────────────────────────────────────────────
Adds TWO small, self-contained changes, BOTH scoped entirely inside
get_matched_signals() (the same function that has always powered post
cards / Claude's grounding data). Nothing else in this file — no other
route, function, template contract, or behavior — was touched.

1. PER-PLATFORM RESULT CAP (new `MAX_POSTS_PER_PLATFORM` env var, default
   3): each individual platform is capped at MAX_POSTS_PER_PLATFORM
   matches for that one search, on top of the existing overall
   MAX_MATCHED_RESULTS safety cap, so no one platform can crowd out the
   others in a single prompt's results.

2. BROADER KEYWORD MATCHING (title / post_text substring matching, ON TOP
   OF the existing search_keyword field matching): a signal now also
   counts as a match if any of the job's generated keywords appears
   (word-boundary matched — see BUGFIX PACK #2 below) inside that
   signal's OWN title or post_text, even if its search_keyword field
   doesn't match at all. Pure OR, never a replacement for the exact-field
   match. Everything else about get_matched_signals() (return shape,
   signature, de-duplication, every caller) is completely unchanged.

── JSON-ANALYSIS-PROMPT SWAP ────────────────────────────────────────────────
ONLY ONE THING was changed from the v7 file above at that point, and it
was scoped to a single constant: CLAUDE_ANALYSIS_SYSTEM_PROMPT. The old
plain-text, free-form analysis system prompt was replaced with the
strict-JSON, 6-format ("source_list" / "trend_report" / "comparison" /
"no_results" / "not_available" / "disallowed") analysis prompt. Nothing
else was touched by that swap: analyze_with_claude(), build_claude_post_
context(), chunk_list(), _format_posts_block(), _map_chunk(), and
_call_claude() are all otherwise identical to before that swap (see
BUGFIX PACK #3 below for the one additive change now inside
analyze_with_claude()). analyze_with_claude() still returns a plain
Python str exactly as before — it is simply that this str now, if Claude
follows the new prompt, contains a JSON document instead of free-form
prose. save_claude_answer_to_chat() still stores whatever string it's
given as-is — no parsing, no new schema. The CLAUDE_MAP_STEP_SYSTEM_PROMPT
(intermediate map/chunking step) is UNCHANGED — still plain bulleted
notes, not JSON.

CONSEQUENCE (logic-only, not a bug): because chat.html / index.html were
built to render `claude_answer` as plain markdown-ish text, and that
prompt returns a strict JSON object as that text, the JSON renders
VERBATIM in the chat UI unless/until a template-side parser and
per-format renderer are added. That template/rendering work remains
OUT OF SCOPE here, same as before.

── KEYWORD-GENERATION SWAP ──────────────────────────────────────────────────
ONLY the SOURCE of the keyword list used for a "search"-type message
changed. Concretely:

  - BEFORE: for every "search" message, generate_fuzzy_keywords(query)
    (plain-Python, fixed KEYWORD_TEMPLATES) mechanically produced up to
    MAX_KEYWORDS template variations of the raw query string itself
    (e.g. query "reddit posts about ai" -> keywords like "reddit posts
    about ai", "reddit posts about ai review", "reddit posts about ai
    complaint", ...) — completely oblivious to what the user actually
    meant, and it always ran as a SEPARATE step after the v5 router had
    already decided the message was "search".

  - NOW: the SAME single cheap Claude call already used for v5/v6
    routing (classify_and_maybe_chat() / CLAUDE_ROUTER_SYSTEM_PROMPT —
    still exactly one call, no new round trip) ALSO returns a
    "keywords" field whenever it classifies the message as "search".
    Claude reads the user's actual words and decides the real topic
    being asked about, then returns the keyword list the UNCHANGED
    Python matching code should use — as few as ONE keyword for a
    narrow prompt ("reddit posts about AI" -> just "AI", not "reddit
    posts about AI review", "reddit posts about AI pricing", etc.), up
    to CLAUDE_MAX_KEYWORDS (new env var, default 10) for a broader ask
    that genuinely has more angles worth searching. "chat" and "blocked"
    messages get "keywords": null, exactly as they get no job today.

  - SAFETY NET (matches the file's existing philosophy everywhere else —
    a routing/Claude hiccup can only ever fall back to the OLD safe
    behavior, never silently produce zero keywords for a real search):
    generate_fuzzy_keywords() and KEYWORD_TEMPLATES are KEPT, completely
    UNCHANGED, byte-for-byte. If the routing step fails outright (API
    error, bad JSON, session hiccup — same failure modes already handled
    everywhere else in this file) OR it succeeds but returns an intent of
    "search" with no usable keywords, the search pipeline falls back to
    calling generate_fuzzy_keywords(query) for that one request only,
    exactly as it always has. A search can therefore NEVER end up with
    zero keywords because of this change.

  - UNCHANGED BY THIS SWAP, EXPLICITLY: enqueue_search_job(),
    get_matched_signals() (search_keyword field matching, title/text
    substring matching, per-platform cap, de-duplication, return shape),
    build_claude_post_context(), analyze_with_claude(), every chat/session
    function, every route's control flow, platform targeting (still
    driven ONLY by the `platform` dropdown field, never by words in the
    chat message), and the chat UI/template contract. The keyword list is
    simply a different list of strings arriving at the exact same
    downstream functions that always consumed it.

  - NOTE ON "TODAY'S POSTS" STYLE PROMPTS (e.g. "today reddit posts about
    X"): this used to have no date/time filtering at all — see the
    TIME-WINDOW FEATURE note near the bottom of this docstring for how
    that changed.

── BUGFIX PACK — RESULTS/ANSWER SYNC + WORD-BOUNDARY MATCHING + 2ND-LEVEL
   CHUNKING ────────────────────────────────────────────────────────────────
Three small, targeted, purely-additive/tightening changes on top of
everything above.

1. RESULTS/ANSWER SYNC BUG FIX:
   Previously, `results` (post cards) and `claude_answer` (Claude's JSON
   text) were computed and saved completely independently of each other
   inside _fill_in_message_outputs(). This meant a message could end up
   with claude_answer being a "no_results" / "not_available" /
   "disallowed" JSON payload (Claude explicitly saying nothing relevant
   was found) while `results` still showed whatever get_matched_signals()
   loosely matched — visibly contradicting the answer text in the chat
   UI. Fix: a new tiny helper, _extract_claude_format(), best-effort
   parses whatever claude_answer text is available and reads its
   "format" field. If that format is one of _NO_DATA_CLAUDE_FORMATS
   ("no_results", "not_available", "disallowed"), the post-card `results`
   for that same message are treated as empty for this render/save
   instead of showing the loosely-matched posts Claude itself already
   rejected.

2. WORD-BOUNDARY KEYWORD MATCHING (v7 substring-matching tightened):
   _text_matches_keyword() used to do a bare Python `in` substring check,
   so a short/generic keyword like "buy" matched inside completely
   unrelated words like "buying" or "buyer". Fixed by switching to a
   regex `\b<keyword>\b` word-boundary match instead of plain substring
   containment.

3. SECOND-LEVEL NOTE CHUNKING (additive-only, inside
   analyze_with_claude()): if the number of condensed first-level notes
   itself grows past CLAUDE_NOTES_PER_CHUNK (default 8), those notes are
   themselves chunked and each batch gets one extra cheap Claude call
   (_condense_notes_chunk()) BEFORE the final reduce call, so the final
   reduce call always sees a bounded amount of text.

── STREAMING ADD-ON + POST_URL PATCH FIX + STREAMING WIRING FIX +
   HOME() CRASH FIX + SIMULATED-STREAM FIX ──────────────────────────────────
(All unchanged from the prior file — word-by-word simulated typing via
GET /chat/{chat_id}/stream, real post_url patching into Claude's JSON
answer via _patch_post_urls_into_answer(), the skip_topic_key hand-off
between _fill_in_message_outputs() and the streaming route, the
pending_stream_topic_key crash fix in home(), and the "assemble complete
answer -> patch links -> pace it out via SSE" rebuild of the streaming
route. See inline comments on each piece below for the full detail — none
of this was touched by the feature added in this file.)

── TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE ──────────────────────────────
THREE small, targeted, additive changes on top of everything above.
Nothing else in this file — no other route, function, constant, matching
rule, chunking rule, or caching rule — was touched.

1. TIME-WINDOW FILTERING (new, previously this file had NO date filtering
   at all):
   - CLAUDE_ROUTER_SYSTEM_PROMPT now ALSO returns a "time_window_days"
     field (an integer, or null) whenever the user's message itself
     implies a time range ("last 7 days" -> 7, "last 6 months" -> ~180,
     "today"/"aaj" -> 1, "this week" -> 7, "past month" -> 30, etc.). If
     the user gives no time range at all, this stays null and behavior is
     100% identical to before this feature — no filtering is applied.
   - This is stored on the message (add_search_to_chat() gained one new
     parameter, `time_window_days`) exactly like `keywords` already is,
     so it stays consistent across page reloads / re-renders of the same
     message.
   - get_matched_signals() gained ONE new optional parameter,
     `since_days: int = None` (default None = old behavior, unchanged).
     When provided, it adds an ADDITIONAL AND-condition on top of the
     existing keyword-matching logic: only signals whose `created_utc`
     falls within the last `since_days` days are eligible to match at
     all. This is a pure narrowing on top of keyword matching, never a
     replacement for it — a signal still has to pass the exact same
     search_keyword / title / post_text keyword checks as before; the
     time window just additionally excludes anything older than the
     window. The Mongo query itself gets a `created_utc: {"$gte": cutoff}`
     clause ANDed with the existing keyword `$or` clause, so old signals
     are excluded at the database level, not just in the Python loop.
   - `_fill_in_message_outputs()` and `GET /chat/{chat_id}/stream` both
     now pass `since_days=msg.get("time_window_days")` into
     get_matched_signals() — the ONLY change to either of those
     functions/routes. Every message that has no `time_window_days`
     (i.e. every message from before this feature, and every new message
     where the user didn't mention a time range) behaves EXACTLY as
     before: `since_days=None`, no filtering.
   - MAX_TIME_WINDOW_DAYS (new env var, default 3650 i.e. ~10 years) is a
     sanity ceiling applied when parsing the router's "time_window_days"
     output, so a malformed/huge value from Claude can never accidentally
     become a no-op filter or a nonsensical cutoff far in the future.

2. PAIN-POINT / PROSPECT KEYWORD GENERATION (prompt-only change, zero new
   code paths):
   CLAUDE_ROUTER_SYSTEM_PROMPT's keyword-generation instructions now
   explicitly cover the "I sell X, find people with problem Y" pattern
   (e.g. "I run an AI agent company, find people complaining their
   website is slow" / "I run a travel agency, find people saying their
   travel service was bad") — for these, Claude generates keywords around
   the PROBLEM/SYMPTOM being described (e.g. "website slow", "site is
   slow", "abandoned cart", "bad travel experience", "trip was ruined"),
   not around the seller's own product/company name, and this is
   explicitly NOT tied to any one fixed industry — whatever industry/
   problem the user actually describes is what Claude reads and reflects
   back into keywords. This is a pure prompt-instruction change: every
   downstream function (enqueue_search_job, get_matched_signals,
   analyze_with_claude, chat/session saving) is completely unaware of
   and unaffected by this — it just receives a different, better-targeted
   keyword list exactly like it always has.

3. "clarify" — a FOURTH router intent (alongside "search" / "chat" /
   "blocked"), for when a search-shaped message doesn't actually name a
   clear enough topic/subject/industry to search for at all (e.g. "aaj ke
   reddit posts dikhao" or "last 6 months ke posts do" with NO brand,
   product, topic, or industry mentioned anywhere in the message OR the
   short chat-summary context). Instead of guessing a topic or falling
   back to generate_fuzzy_keywords() on a near-empty/meaningless string,
   the router returns:
     {"intent": "clarify", "reply": "<short natural clarifying question>", "keywords": null, "time_window_days": null}
   A "clarify" message is handled by the SAME code path as "chat"/
   "blocked" in POST /search — saved as an ordinary "message_type": "chat"
   turn (query + claude_answer = the clarifying question, no post cards,
   no job enqueued) — so, exactly like "blocked" before it, NO template
   changes are needed anywhere. CLAUDE_CLARIFY_FALLBACK_REPLY is used as a
   safety-net string only if the router flagged "clarify" but didn't
   return usable reply text.
   IMPORTANT — this only ever fires when the TOPIC itself is unclear, not
   when merely the time-window is unclear/unspecified: a message like
   "AI agents ke baare mein reddit posts dikhao" (clear topic, no time
   range) is still a normal "search" with time_window_days: null, exactly
   as before this feature. A missing time range is never, by itself, a
   reason to ask for clarification.
   SAFETY NET (same philosophy as the rest of this router): if the
   routing step fails outright (API error, bad JSON, session hiccup) OR
   returns anything unparseable, this still falls back to intent="search"
   exactly as it always has — "clarify" can only ever ask a genuinely
   helpful follow-up question on top of the existing pipeline, it can
   never be the reason a real, clear search silently fails to run.

── CLARIFY-SELF-RESOLVE FEATURE ────────────────────────────────────────────
ONE small, targeted, additive change on top of everything above, scoped
entirely to the "clarify" branch of POST /search. Nothing else in this
file — no other route, function, constant, matching rule, or caching
rule — was touched.

Previously, whenever the router returned intent="clarify" (a search-
shaped message with no clear topic/brand/industry, e.g. "aaj ke reddit
posts dikhao" with nothing else), the user was ALWAYS asked a clarifying
question immediately — the ONLY way that ever changed to a real search
was the user typing a follow-up message that named the topic.

NOW: before falling back to asking the user, the /search route makes ONE
extra, best-effort, single cheap Claude call — resolve_unclear_topic() —
that tries to figure out the topic using ONLY Claude's own general
knowledge and the existing rolling chat-summary context (e.g. a topic
named earlier in the SAME conversation, or something Claude's own
training knowledge can confidently infer). This step has NO web-search
tool and is given NO new information beyond the message + the summary —
it is deliberately a narrow, honest "can I already tell what this means"
check, not a research step.

  - If resolve_unclear_topic() comes back with a confidently-resolved
    topic (keywords, and optionally a time_window_days), the message is
    switched from intent="clarify" to intent="search" and falls straight
    through into the EXACT SAME, byte-for-byte unchanged v1-v7 search
    pipeline below (enqueue_search_job, add_search_to_chat,
    get_matched_signals, analyze_with_claude, etc.) — using the resolved
    keywords/time_window_days exactly the same way routed_keywords/
    routed_time_window_days from the normal router already are. No
    user-facing clarifying question is ever shown in this case.
  - If it can't confidently resolve anything (the overwhelmingly more
    common case, by design — the resolver is instructed to only succeed
    when genuinely confident), OR if the call fails outright (API error,
    bad JSON, timeout), intent stays "clarify" and the EXISTING clarify-
    question flow (add_chat_message_to_chat with
    CLAUDE_CLARIFY_FALLBACK_REPLY as the safety net) runs completely
    unchanged, exactly as it did before this feature existed.

SAFETY NET (same philosophy as the rest of this file): this can only ever
ADD a shortcut on top of the existing "clarify" flow — it can never
replace the safety net of asking the user, and it can never affect a
message that was never classified as "clarify" in the first place (every
"search", "chat", and "blocked" message is completely untouched by this
feature).

── WEBSITE-URL KEYWORD EXTRACTION FEATURE ──────────────────────────────────
ONE small, targeted, additive change on top of everything above, scoped
entirely to the SEARCH-TYPE branch of POST /search (this includes a
message that started as "clarify" and was just switched to "search" by
the CLARIFY-SELF-RESOLVE feature above). Nothing else in this file — no
other route, function, constant, matching rule, or caching rule — was
touched.

If the user's message itself contains a website URL (e.g. "yeh meri
website https://example.com hai, is se related reddit posts dikhao"),
the /search route now:
  1. Detects the URL with a plain-Python regex check (_extract_first_url())
     — no Claude call needed for this part.
  2. Fetches that URL's page content and reduces it to plain text
     (fetch_website_text()) — a lightweight, dependency-free tag-strip,
     not a full HTML parser, truncated to WEBSITE_FETCH_MAX_CHARS so a
     large page can never blow up the size/cost of the next step.
  3. Makes ONE extra cheap Claude call (extract_keywords_from_website())
     that reads the website's plain text content TOGETHER WITH the
     user's own request text (so a specific angle the user typed
     alongside the link, e.g. "pricing complaints", is honored — not
     just the site's generic content on its own) and returns up to
     MAX_WEBSITE_KEYWORDS (default 20) keywords for the topic.
  4. If that succeeds with usable keywords, those keywords are used for
     THIS search instead of whatever the router (or the CLARIFY-SELF-
     RESOLVE step above) already produced — they are handed to the
     EXACT SAME, byte-for-byte unchanged downstream pipeline
     (enqueue_search_job, add_search_to_chat, get_matched_signals,
     analyze_with_claude, etc.) exactly like any other keyword source in
     this file already is.

SAFETY NET (same philosophy as the rest of this file): if the URL fetch
fails (site down, timeout, non-HTML, blocked, bad URL) OR the Claude
extraction call fails outright OR it returns no usable keywords, this
simply leaves the existing keyword source (the router's routed_keywords,
or the CLARIFY-SELF-RESOLVE result) untouched — the EXISTING safety-net
chain immediately below (routed_keywords -> generate_fuzzy_keywords())
still applies exactly as before this feature. A search can therefore
NEVER end up with zero keywords because of this feature, and a message
with no URL in it behaves 100% identically to before this feature — this
whole step is skipped entirely when _extract_first_url() finds nothing.

── ROUTER INTENT REFINEMENT ────────────────────────────────────────────────
THREE small, targeted, PROMPT-ONLY changes on top of everything above.
Zero new code paths, zero new functions, zero new routes, zero new Mongo
fields. Every function signature, matching rule, chunking rule, caching
rule, and template contract in this file is byte-for-byte unchanged. The
only things that changed are the text of CLAUDE_ROUTER_SYSTEM_PROMPT and
the text of CLAUDE_CLARIFY_FALLBACK_REPLY, described below. This addresses
three first-impression / product-quality gaps in how the router classifies
borderline messages:

1. GENERIC PAIN-POINT PROMPTS NO LONGER MISFIRE AS "clarify":
   Previously, a message like "reddit par log kya problems face kar rahe
   hain" (no brand, no product, no industry — just "problems" in
   general) could get classified as "clarify" and bounce the user with a
   follow-up question on their very first message, which is a poor first
   impression. The router prompt's "search" instructions now explicitly
   state that a broad-but-real pain-point/complaint question about a
   PLATFORM (not about "what's trending in general" — see point 2 below)
   IS a valid, searchable "search" subject on its own: "general
   problems/complaints/frustrations" is itself the topic. For these,
   Claude generates keywords around common frustration/complaint phrasing
   likely to appear in real posts (e.g. "so frustrated with", "sick of
   dealing with", "worst experience with", "wish there was a better",
   "fed up with") instead of refusing to search or asking a clarifying
   question. This only ever pulls a message OUT of "clarify" and INTO
   "search" — it never changes how an already-clear "search" message
   (e.g. "AI agents ke baare mein pichle 6 months mein kya baat hui") is
   classified or keyworded; those are untouched.

2. TRULY OPEN-ENDED "what's happening on this platform" PROMPTS ARE
   "chat", NOT A DATABASE SEARCH:
   A message that names a platform but NO subject whatsoever — not even
   a general problem/complaint angle — e.g. "reddit par kya chal raha
   hai", "abhi reddit par top problem kya hai" with truly nothing else to
   go on, would previously either misfire as "clarify" (asking an
   unhelpful follow-up on message #1) or, if forced through as "search",
   would only ever be able to generate near-meaningless keywords with
   nothing real to filter flintel_signals by. The router prompt's "chat"
   instructions now explicitly cover this case: classify it as "chat" (no
   flintel_signals query, no job enqueued, "keywords"/"time_window_days"
   stay null exactly like any other "chat" message), and have Claude
   answer directly from its own general knowledge of what's commonly
   discussed on that platform, in a natural, confident, professional
   tone — then naturally invite the user to share their business,
   product, or website link so Flintel can pull real, current, related
   data for them specifically. This is DELIBERATELY narrower than point 1
   above: the distinguishing line the prompt draws is "is there an actual
   angle/problem/subject named, even a general one (-> search), or is the
   question just 'what's happening in general' with nothing else (->
   chat)". A message that names ANY real subject alongside the platform —
   a brand, product, industry, or general problem/complaint angle — is
   "search", never this "chat" case.

3. THE "clarify" REPLY NOW SOUNDS LIKE A CONSULTANT, NOT A FORM
   VALIDATOR:
   With points 1 and 2 above narrowing when "clarify" fires at all (down
   to messages that are search-shaped but give NOTHING to work with —
   not even a general problem angle, not even a platform to riff on),
   the router's instructions for writing a "clarify" reply now ask
   Claude to briefly explain WHY it's asking (so the search actually
   finds something relevant) and to offer the user two ways forward in
   one natural sentence: name a topic/brand/industry, OR just paste
   their website link — which the EXISTING, UNCHANGED WEBSITE-URL
   KEYWORD EXTRACTION FEATURE above already knows how to read
   automatically on their very next message. CLAUDE_CLARIFY_FALLBACK_REPLY
   (the safety-net string used only if the router flagged "clarify" but
   didn't return usable reply text) was reworded to match this same
   consultant tone and to mention the website-link option, so the
   fallback and the router's own reply sound consistent.

SAFETY NET (same philosophy as the rest of this file): all three changes
are pure prompt wording inside CLAUDE_ROUTER_SYSTEM_PROMPT plus the
CLAUDE_CLARIFY_FALLBACK_REPLY string. The existing "ANY router failure ->
default to intent='search'" fallback in classify_and_maybe_chat(), the
existing generate_fuzzy_keywords() safety net, and every other safety net
already documented above are completely unaffected — a routing hiccup
still can never block or under-supply a genuine search, exactly as before.

── URL-HANDLING REFINEMENTS (THIS FILE) ────────────────────────────────────
Wires in three new URL-handling behaviors via the NEW, self-contained
website_intelligence.py module (imported as `website_intelligence`
alongside the existing `import flintel`). NOTHING from that module is
reimplemented here — this file only calls into it. Nothing about
_extract_first_url(), fetch_website_text(), or
extract_keywords_from_website() changed; all three are still reused
exactly as they were.

Scoped to exactly TWO integration points inside POST /search:

INTEGRATION POINT 1 (runs after classify_and_maybe_chat() /
CLARIFY-SELF-RESOLVE, BEFORE the existing chat/blocked/clarify handling
branch): if a URL is detected in the message, and the router's own
classification says "clarify"/"chat" (or, as a last-resort fallback when
that signal is ambiguous, website_intelligence.has_request_shaped_
language() finds no real request-shaped text beyond the link itself),
this is a bare URL with no real ask — BEHAVIOR 1. The site is summarized
via website_intelligence.summarize_website() and a natural, professional
reply built via website_intelligence.build_url_only_reply() invites the
user to say what they'd like looked into, saved via the EXISTING,
UNCHANGED add_chat_message_to_chat() (same message shape as any other
chat/blocked/clarify turn — no template changes needed) and the rest of
the search pipeline is skipped for this request. Any failure fetching or
summarizing the site simply falls through to the EXISTING clarify/chat
behavior — no new failure mode. Otherwise (a real, separately-stated ask
alongside the URL), intent is treated as "search" and INTEGRATION POINT 2
below runs instead of the plain WEBSITE-URL KEYWORD EXTRACTION call.

INTEGRATION POINT 2 (inside the SEARCH-TYPE branch, replacing the single
extract_keywords_from_website() call for the case where the message has
BOTH a URL and explicit topic/request text): the SAME
has_request_shaped_language() heuristic distinguishes BEHAVIOR 2 ("find
leads/customers/posts related to MY site" with no separately-named
topic — the EXISTING, UNCHANGED extract_keywords_from_website() path,
zero change) from a BEHAVIOR 3 candidate (a topic named alongside the
URL). For a BEHAVIOR 3 candidate,
website_intelligence.check_topic_matches_website() reads the topic and
the website together in ONE Claude call and returns both a match verdict
and (if it matches) the keyword list. An inconclusive/failed check falls
straight through to the EXISTING extract_keywords_from_website() path and
the EXISTING safety-net chain (routed_keywords -> generate_fuzzy_
keywords()) — completely intact. A confirmed match uses the returned
keywords exactly like extract_keywords_from_website()'s output is used
today. An explicit mismatch (BEHAVIOR 3's actual trigger) skips
enqueueing a job entirely, tells the user plainly via website_
intelligence.build_topic_mismatch_reply(), saved via the same EXISTING
add_chat_message_to_chat(), and redirects exactly like the chat/blocked/
clarify branch does.

Every message with NO URL in it, and every message that already behaved
as a normal "search"/"chat"/"blocked"/"clarify" before this feature, is
100% unchanged. Nothing here touches CLAUDE_ROUTER_SYSTEM_PROMPT,
CLAUDE_ANALYSIS_SYSTEM_PROMPT, get_matched_signals(), the per-user busy
lock, the results-recompute fix, or any other existing feature/route in
this file. GENERICITY: nothing added here hardcodes or special-cases any
specific topic, brand, or industry — website_intelligence.py's own
functions reason fresh from whatever content/topic they're given, exactly
as documented in that module.

EVERYTHING ELSE IN THIS FILE — every other route, function, constant,
prompt, matching rule, chunking rule, caching rule, and template contract
— is untouched and behaves exactly as documented above.
──────────────────────────────────────────────────────────────────────────────

── STRUCTURAL SPLIT + BUG FIX PASS ─────────────────────────────────────────
STRUCTURAL SPLIT: every FastAPI route handler (`home`, `search`,
`list_chats`, `new_chat`, `view_chat`, `stream_answer`, `delete_chat`,
`google_login`, `google_callback`, `signup`, `login`, `logout`) has moved
out, verbatim, into a new `routes.py` module — this file now holds zero
`@app.get`/`@app.post` decorators. `index.py` still owns `app`,
`templates`, `pwd_context`, `oauth`, every constant, and every piece of
business logic; `routes.py` imports all of that and registers its routes
on the SAME `app` object constructed here. `index.py` imports `routes` at
the very bottom, purely for the side effect of registering those routes
before the app is served — the `uvicorn.run("index:app", ...)` string
and the deployment/run command are unchanged.

BUG 1 (chat-summary context loss): `append_to_chat_summary()` used to
character-trim the raw `claude_answer` JSON string, slicing it mid-object
and discarding the exact fields (a suggested term, a followup, a
likely_reason) a later confirmation-style reply ("yes", "sure", "search
it") needed to resolve against. A new `_extract_summary_text_from_claude_
answer()` helper now pulls the human-readable fields out of the JSON
first, so the summary line stays useful after trimming.

BUG 2b (real matched posts hidden by a "no_data" format): `_complete_
message_answer_and_results()` used to discard real matched posts purely
because Claude's own written analysis picked "no_results"/
"not_available"/"disallowed" for the narrative text. A new shared
`_finalize_answer_and_results()` helper (also used by `routes.py`'s
streaming route) now keeps Claude's honest text intact but appends one
short, rotating positive closing note (`_pick_closest_matches_note()` /
`_append_closest_matches_note()`) whenever real posts were actually
matched, instead of hiding them.

BUG 3 (model suggesting competitors/other channels): `CLAUDE_ANALYSIS_
SYSTEM_PROMPT`'s "no_results" format previously let Claude fill
"likely_reason" with genuinely-true-but-harmful observations like "people
usually search Google/Discord/a directory instead" — a new CRITICAL
GUARDRAIL paragraph (plus one added TONE bullet) explicitly forbids
naming any platform, tool, or channel outside Flintel as a better place
to look, in this or any other format's text fields.

BUG 4 (stale "try these next steps" showing alongside real matched
posts): when `_finalize_answer_and_results()` routes a message through
`_append_closest_matches_note()` because Claude's own written analysis
picked a "no_data" format even though real posts were matched, the
format's own `suggested_actions` (the broaden_time / broaden_platforms /
broaden_term "Try one of these next steps" block) and
`clarifying_question` fields used to still be shown, directly underneath
the real posts already displayed for that same message — reading as
contradictory ("try broadening your search" right next to real results).
`_append_closest_matches_note()` now also strips both of those fields
from the JSON in that same pass, since real posts are ALWAYS being shown
below this text whenever this function is called (see
`_finalize_answer_and_results()`) — a genuine no_results answer (no
matched posts at all) never reaches this function, so its
`suggested_actions` / `clarifying_question` are completely unaffected by
this change.
──────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import json
import time
import uuid
import logging
from datetime import datetime, timedelta, timezone

import httpx
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from passlib.context import CryptContext
from authlib.integrations.starlette_client import OAuth

import flintel
import website_intelligence
import google as google_search   # the new google.py module
    # (renamed on import to `google_search` to avoid any ambiguity
    # with the unrelated third-party `google` package some
    # environments have installed — module FILE stays google.py, only
    # the import alias differs)
from database import (
    db,
    jobs_collection,
    signals_collection,
    users_collection,
    chats_collection,
    busy_owners_collection,
    google_posts_collection,
)

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
# logging.basicConfig() is now called once, in database.py (imported above)
# — calling it again here would just be a harmless no-op, but this module's
# own logger identity ("flintel-web") is still defined here, since that's
# unrelated to where the shared config lives.

log = logging.getLogger("flintel-web")

# Hard ceiling on how many keywords can ever reach a job, regardless of
# where they came from (Claude's router-generated list, or the
# generate_fuzzy_keywords() fallback below). UNCHANGED variable/behavior
# from before — just now also double-checked against Claude's output.
MAX_KEYWORDS = int(os.getenv("MAX_KEYWORDS", "20"))

# (KEYWORD-GENERATION SWAP) Separate, tighter cap specifically on how many
# keywords the Claude router call is allowed to hand back for a "search"
# message — default 10, per what was asked. Kept distinct from
# MAX_KEYWORDS (which remains the final, absolute safety ceiling applied
# right before a job is enqueued, covering both sources).
CLAUDE_MAX_KEYWORDS = int(os.getenv("CLAUDE_MAX_KEYWORDS", "10"))

# How many matched (post_text + url) results to surface per topic. Kept
# separate from MAX_KEYWORDS since it's about signal output, not keyword
# generation. (v7: this remains the OVERALL safety cap across every
# platform combined — see MAX_POSTS_PER_PLATFORM below for the new
# additional per-platform cap layered on top of this one.)
MAX_MATCHED_RESULTS = int(os.getenv("MAX_MATCHED_RESULTS", "25"))

# (v7) Caps how many matched posts ANY SINGLE platform can contribute
# to one search's results — e.g. with the default of 3, at most 3 Reddit
# posts AND at most 3 X/Twitter posts (etc.) show up for one prompt, even
# if many more than that actually matched, so no one platform can crowd
# out the others. Purely a config value — change it in .env and restart
# to use a different number later; see get_matched_signals() below for
# where it's applied.
MAX_POSTS_PER_PLATFORM = int(os.getenv("MAX_POSTS_PER_PLATFORM", "3"))

# (TIME-WINDOW FEATURE) Sanity ceiling on the "time_window_days" value the
# router is allowed to hand back — purely a safety clamp so a malformed or
# absurd value from Claude can never turn into a nonsensical cutoff.
# Default ~10 years, effectively "no meaningful limit" for any real query.
MAX_TIME_WINDOW_DAYS = int(os.getenv("MAX_TIME_WINDOW_DAYS", "3650"))

SESSION_SECRET_KEY  = os.getenv("SESSION_SECRET_KEY", "dev-only-change-me")
GOOGLE_CLIENT_ID    = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# ── Claude analysis layer config (v4) ──────────────────────────────────────
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL            = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_MAX_TOKENS       = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))
CLAUDE_MAP_MAX_TOKENS   = int(os.getenv("CLAUDE_MAP_MAX_TOKENS", "512"))
CLAUDE_POSTS_PER_CHUNK  = int(os.getenv("CLAUDE_POSTS_PER_CHUNK", "12"))
CLAUDE_TIMEOUT_SECONDS  = float(os.getenv("CLAUDE_TIMEOUT_SECONDS", "30"))
CLAUDE_API_URL          = "https://api.anthropic.com/v1/messages"
CLAUDE_API_VERSION      = "2023-06-01"

# (BUGFIX PACK #3) How many condensed first-level "notes" strings are
# allowed to be combined directly in the final reduce call before a
# SECOND level of chunking kicks in to condense them further first. Only
# matters for topics big enough to already need first-level chunking —
# see analyze_with_claude() below.
CLAUDE_NOTES_PER_CHUNK  = int(os.getenv("CLAUDE_NOTES_PER_CHUNK", "8"))

# ── Router + chat-summary config (v5, bumped slightly for the new
# "keywords"/"time_window_days" fields the router call can now also
# return) ───────────────────────────────────────────────────────────────
CLAUDE_ROUTER_MAX_TOKENS       = int(os.getenv("CLAUDE_ROUTER_MAX_TOKENS", "500"))
CHAT_SUMMARY_MAX_TURNS         = int(os.getenv("CHAT_SUMMARY_MAX_TURNS", "8"))
CHAT_SUMMARY_TURN_CHAR_LIMIT   = int(os.getenv("CHAT_SUMMARY_TURN_CHAR_LIMIT", "160"))

# ── Response-timeout config (v6) ────────────────────────────────────────────
# How long (seconds) a search-type message is allowed to sit with no
# matched flintel_signals before we stop silently waiting and instead give
# the user a plain, natural "nothing found yet" answer, the same way
# Claude/ChatGPT would rather than leaving them staring at a blank turn
# forever. See _fill_in_message_outputs() below.
RESPONSE_TIMEOUT = int(os.getenv("RESPONSE_TIMEOUT", "60"))

# (PER-USER BUSY LOCK) Same spirit as RESPONSE_TIMEOUT above: if a request
# crashes or the server restarts while an owner is marked busy, the flag
# must not get stuck forever. After this many seconds, a "busy" flag is
# treated as stale and cleared automatically the next time it's checked.
BUSY_FLAG_TIMEOUT_SECONDS = int(os.getenv("BUSY_FLAG_TIMEOUT_SECONDS", "90"))

# ── Simulated-stream config (SIMULATED-STREAM FIX) ──────────────────────────
# How the already-complete, already-post_url-patched answer text is sliced
# and paced back out to the browser over SSE inside GET /chat/{chat_id}/stream
# — see the SIMULATED-STREAM FIX note in the module docstring for the full
# rationale. Purely cosmetic/pacing values; changing them never changes what
# text is shown or cached, only how it visually arrives.
STREAM_CHUNK_CHARS         = int(os.getenv("STREAM_CHUNK_CHARS", "3"))
STREAM_CHUNK_DELAY_SECONDS = float(os.getenv("STREAM_CHUNK_DELAY_SECONDS", "0.02"))

# ── Clarify-self-resolve config ─────────────────────────────────────────────
# Max tokens for the single extra Claude call resolve_unclear_topic() makes
# — see that function and the CLARIFY-SELF-RESOLVE FEATURE module docstring
# note above.
CLAUDE_TOPIC_RESOLVER_MAX_TOKENS = int(os.getenv("CLAUDE_TOPIC_RESOLVER_MAX_TOKENS", "400"))

# ── Website-URL keyword extraction config ───────────────────────────────────
# See the WEBSITE-URL KEYWORD EXTRACTION FEATURE module docstring note
# above, and fetch_website_text() / extract_keywords_from_website() below.
MAX_WEBSITE_KEYWORDS               = int(os.getenv("MAX_WEBSITE_KEYWORDS", "20"))
WEBSITE_FETCH_TIMEOUT_SECONDS      = float(os.getenv("WEBSITE_FETCH_TIMEOUT_SECONDS", "15"))
WEBSITE_FETCH_MAX_CHARS            = int(os.getenv("WEBSITE_FETCH_MAX_CHARS", "8000"))
CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS  = int(os.getenv("CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS", "400"))

# db, jobs_collection, signals_collection, users_collection,
# chats_collection, busy_owners_collection are now all imported from
# database.py above — that module owns the Mongo client, every collection
# handle, and every index-creation call (same URI/DB env vars, same
# collection names, same indexes, same startup log lines as before this
# extraction).

# ─────────────────────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Flintel Web Service — v7",
    # v4.2: docs/redoc/openapi.json are internal implementation detail, not a
    # public product surface — block all three so /docs, /redoc, and
    # /openapi.json 404 instead of exposing every route + schema to anyone.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
templates = Jinja2Templates(directory="templates")

# Required for login sessions (stores a signed cookie, not the DB user doc).
# Also doubles as where we stash the anonymous chat-owner UUID and the
# currently active chat_id, exactly like a browser-local "current chat"
# pointer in Claude/ChatGPT.
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY)

# (NO-CACHE FIX) Every page this app serves is per-user, dynamic content
# (a specific user's chat history, their active chat, credits, etc.) — none
# of it should ever be cached by a shared intermediary (a CDN, a reverse
# proxy like Varnish, or the browser's own disk cache). Without this, a
# request that happens to be served from a stale cached copy can render an
# incomplete/older version of the page (e.g. missing the docked search box
# on a chat page that was cached before it was fully rendered) until the
# cache entry naturally expires or gets revalidated — which looks exactly
# like "it's broken the first time, then fixes itself" once a later
# request (to the same or a different page) causes a fresh fetch. This
# applies the standard "never cache this" header set to every response
# this app returns, closing off that entire class of stale-cache bugs at
# the source rather than trying to chase down each individual symptom.
@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# Password hashing context — bcrypt, industry-standard for this use case.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Google OAuth client.
oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ─────────────────────────────────────────────────────────────────────────────
# TOPIC NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────

def normalize_topic_key(query: str) -> str:
    """Turns a user's raw prompt into a stable cache/job key.
    e.g. "  Nike  " -> "nike", "Nike Shoes!!" -> "nike shoes"

    UNCHANGED by the keyword-generation swap: topic_key is still derived
    from the raw query string and is used only as the flintel_search_jobs
    upsert key / chat message linkage — it has nothing to do with which
    keywords get matched against flintel_signals."""
    cleaned = re.sub(r"[^a-z0-9\s]", "", query.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM TARGETING
# ─────────────────────────────────────────────────────────────────────────────

# Maps whatever label the "All Platforms" dropdown sent to a stable key.
# This ONLY tags the job — it never changes what keywords get generated,
# and it is still driven ONLY by this dropdown field, never by words in
# the user's chat message.
PLATFORM_KEY_MAP = {
    "all platforms": "all",
    "all":           "all",
    "reddit":        "reddit",
    "x / twitter":   "x_twitter",
    "x/twitter":     "x_twitter",
    "twitter":       "x_twitter",
    "x":             "x_twitter",
    "linkedin":      "linkedin",
    "facebook":      "facebook",
}


def normalize_platform(raw: str) -> str:
    """Falls back to "all" for anything unrecognized, so a missing/garbled
    dropdown value never accidentally narrows a search."""
    key = (raw or "").strip().lower()
    return PLATFORM_KEY_MAP.get(key, "all")


# ─────────────────────────────────────────────────────────────────────────────
# FUZZY KEYWORD GENERATION — plain Python, template-based, no Claude call
#
# (KEYWORD-GENERATION SWAP) This function and KEYWORD_TEMPLATES are KEPT
# COMPLETELY UNCHANGED from v1, but they are NO LONGER the primary path
# for a "search" message. They are now used ONLY as a safety-net FALLBACK
# — see classify_and_maybe_chat() / the /search route below — for the
# rare case where the Claude routing call fails outright, or succeeds but
# doesn't return a usable "keywords" list for a "search" intent. This
# guarantees a search can never end up with zero keywords, exactly the
# same safety philosophy already used everywhere else in this file.
# ─────────────────────────────────────────────────────────────────────────────

KEYWORD_TEMPLATES = [
    "{q}",
    "{q} review",
    "{q} reviews",
    "{q} honest review",
    "{q} complaint",
    "{q} complaints",
    "{q} problem",
    "{q} problems",
    "{q} issue",
    "{q} issues",
    "{q} experience",
    "{q} experiences",
    "is {q} worth it",
    "is {q} good",
    "{q} worth it",
    "switched from {q}",
    "switched to {q}",
    "alternative to {q}",
    "{q} vs",
    "{q} pricing",
    "{q} customer service",
    "{q} support",
    "{q} quality",
    "{q} scam",
    "{q} recommend",
    "thoughts on {q}",
    "anyone using {q}",
    "anyone tried {q}",
]


def generate_fuzzy_keywords(query: str) -> list:
    """Builds a list of search phrases around the user's query using fixed
    templates. Deterministic, fast, and needs no external API call.

    (KEYWORD-GENERATION SWAP) Still here, byte-for-byte unchanged — now
    called only as the fallback path described above, not on every
    search."""
    q = query.strip()
    if not q:
        return []

    seen = set()
    keywords = []
    for template in KEYWORD_TEMPLATES:
        phrase = template.format(q=q).strip()
        key = phrase.lower()
        if key not in seen:
            seen.add(key)
            keywords.append(phrase)
        if len(keywords) >= MAX_KEYWORDS:
            break

    return keywords


# ─────────────────────────────────────────────────────────────────────────────
# JOB QUEUE — patch into the SAME MongoDB Background Service #1 reads
# ─────────────────────────────────────────────────────────────────────────────

def enqueue_search_job(topic_key: str, keywords: list, targeting_platform: str):
    """Upserts a job by topic_key. Re-searching the same topic simply
    resets it to pending instead of creating a duplicate job.

    UNCHANGED by the keyword-generation swap: this function has no idea
    (and doesn't need to know) whether `keywords` came from Claude's
    router call or the generate_fuzzy_keywords() fallback — it just
    stores whatever list it's handed, exactly as before. Also UNCHANGED
    by the TIME-WINDOW FEATURE: a time window only ever narrows which
    ALREADY-COLLECTED signals are matched (see get_matched_signals()
    below) — it has no bearing on what Background Service #1 goes out
    and fetches, so it is never stored on the job document."""
    jobs_collection.update_one(
        {"topic_key": topic_key},
        {"$set": {
            "topic_key":          topic_key,
            "keywords":           keywords,
            "targeting_platform": targeting_platform,  # "all" | "reddit" | "x_twitter" | "linkedin" | "facebook"
            "status":             "pending",
            "requested_at":       datetime.now(timezone.utc),
            "started_at":         None,
            "completed_at":       None,
            "matched_count":      0,
            "error":              None,
        }},
        upsert=True,
    )
    log.info(
        f"Job queued | topic_key={topic_key} | keywords={len(keywords)} "
        f"| targeting_platform={targeting_platform}"
    )


def get_job(topic_key: str):
    return jobs_collection.find_one({"topic_key": topic_key}, {"_id": 0})


def get_signal_count(topic_key: str) -> int:
    return signals_collection.count_documents({"topic_key": topic_key})


def get_signals(topic_key: str, limit: int = 25):
    return list(
        signals_collection.find({"topic_key": topic_key}, {"_id": 0})
        .sort("created_utc", -1)
        .limit(limit)
    )


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL MATCHING — pull post_text + post_url for signals whose search
# keyword matches one of the keywords generated for this job, OR (v7)
# whose title/post_text itself contains one of those keywords. This is
# exactly what still powers the post cards (title + post_url, as-is).
# Claude (below) only ever sees title + post_text from whatever this
# returns — never post_url, never platform.
#
# COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP: everything in this
# section works purely off whatever `keywords` list it's handed — it has
# no idea, and doesn't care, whether those keywords came from Claude's
# router call or the old fuzzy-template fallback.
#
# `flintel_signals` docs may use slightly different field names depending
# on how Background Service #1 writes them, so this reads a small list of
# likely candidates for each field instead of hard-coding a single name.
# Adjust FIELD candidates below if your signals schema differs.
# ─────────────────────────────────────────────────────────────────────────────

_KEYWORD_FIELD_CANDIDATES  = ["search_keyword", "keyword", "matched_keyword", "query_keyword", "keywords"]
_TITLE_FIELD_CANDIDATES    = ["title", "post_title", "headline"]
_TEXT_FIELD_CANDIDATES     = ["post_text", "text", "body", "content", "selftext"]
_URL_FIELD_CANDIDATES      = ["post_url", "url", "link", "permalink"]
_PLATFORM_FIELD_CANDIDATES = ["platform", "source", "source_platform"]

# Maps a job's targeting_platform ("all" | "reddit" | "x_twitter" |
# "linkedin" | "facebook" — see normalize_platform()) to the value(s) a
# signal doc's own platform field may use. "all" has no entry here since
# it means "no filter" and is handled separately.
_PLATFORM_DOC_VALUES = {
    "reddit":    ["reddit"],
    "x_twitter": ["twitter", "x", "x_twitter", "x/twitter"],
    "linkedin":  ["linkedin"],
    "facebook":  ["facebook"],
}


def _first_present(doc: dict, candidates: list):
    for field in candidates:
        value = doc.get(field)
        if value:
            return value
    return None


def _signal_platform_matches(doc: dict, targeting_platform: str) -> bool:
    """True if this signal's platform field (or, if that's missing, the
    platform inferred from its post URL) matches the job's
    targeting_platform. "all" (or anything unrecognized) means no
    filtering — every platform is allowed, exactly as it behaves today.
    Only when the user actually picked a specific platform (Reddit, X /
    Twitter, LinkedIn, Facebook) does this narrow results down to signals
    from that one platform."""
    if not targeting_platform or targeting_platform == "all":
        return True

    allowed = _PLATFORM_DOC_VALUES.get(targeting_platform)
    if not allowed:
        return True  # unrecognized targeting value -> don't accidentally exclude everything

    doc_platform = _first_present(doc, _PLATFORM_FIELD_CANDIDATES)
    if not doc_platform or not isinstance(doc_platform, str):
        # No platform field on this doc — fall back to guessing from its
        # URL rather than excluding it outright.
        doc_platform = _infer_platform_from_url(_first_present(doc, _URL_FIELD_CANDIDATES))
    if not doc_platform:
        return False

    return doc_platform.strip().lower() in allowed


def _signal_keyword_matches(doc: dict, keyword_set: set) -> bool:
    """True if this signal's search-keyword field matches ANY keyword in
    keyword_set (case-insensitive), whichever one it happens to be.
    Handles the keyword field being a single string OR a list (in case a
    signal doc records more than one matched keyword).

    UNCHANGED from v3/v4/v5/v6/v7 and UNCHANGED by the keyword-generation
    swap — this exact-field check is completely untouched; it still works
    exactly as it always has, regardless of where `keyword_set` came
    from."""
    if not keyword_set:
        return True  # no keyword filter to apply -> don't exclude anything

    raw_value = None
    for field in _KEYWORD_FIELD_CANDIDATES:
        if field in doc and doc[field]:
            raw_value = doc[field]
            break

    if not raw_value:
        return False

    if isinstance(raw_value, (list, tuple, set)):
        candidates = raw_value
    else:
        candidates = [raw_value]

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip().lower() in keyword_set:
            return True
    return False


def _text_matches_keyword(text: str, keyword_set: set) -> bool:
    """(v7, tightened by BUGFIX PACK #2) True if ANY keyword in
    keyword_set appears as a case-insensitive WHOLE-WORD/PHRASE match
    somewhere inside `text` — this is what lets a signal count as a match
    purely because a keyword phrase shows up in its own title or
    post_text, even when its search_keyword field doesn't match at all —
    a pure additional OR path alongside _signal_keyword_matches() above,
    never a replacement for it.

    (BUGFIX PACK #2): previously this did a bare `kw in text_lower`
    substring check, which meant a short/generic keyword like "buy" would
    also match inside completely unrelated words like "buying" or
    "buyer" — pulling in irrelevant posts that happened to contain that
    letter sequence as a fragment. Now uses a `\\b<keyword>\\b`
    word-boundary regex instead, so a keyword only counts as a match when
    it appears as a genuine whole word/phrase in the text, not as a
    fragment glued onto other letters. Signature, return type, and every
    caller are otherwise unchanged."""
    if not text or not isinstance(text, str):
        return False
    text_lower = text.lower()
    for kw in keyword_set:
        if not kw:
            continue
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, text_lower):
            return True
    return False


def _infer_platform_from_url(url: str):
    """Fallback for when a signal doc doesn't have a usable platform field:
    guesses the platform from the post URL's domain so the icon/badge still
    matches reality (e.g. a twitter.com/x.com link shows the X icon instead
    of falling back to a generic globe). Only used when the doc's own
    platform field is missing — never overrides an actual platform value."""
    if not url or not isinstance(url, str):
        return None
    domain = url.lower()
    if "twitter.com" in domain or "x.com" in domain:
        return "twitter"
    if "reddit.com" in domain:
        return "reddit"
    if "facebook.com" in domain or "fb.com" in domain:
        return "facebook"
    if "linkedin.com" in domain:
        return "linkedin"
    return None


def get_matched_signals(topic_key: str, keywords: list, targeting_platform: str = "all",
                         limit: int = None, since_days: int = None, unfiltered: bool = False) -> list:
    """Reads `flintel_signals` and keeps only the signals that match this
    job's generated keywords. topic_key match is intentionally NOT
    required: Background Service #1 may store its own topic_key for a
    signal, but what decides a match here is purely the keyword-matching
    rules below.

    A signal counts as a match if ANY ONE of these is true (v7: this is
    now three OR'd conditions instead of just the first one):
      1. its search-keyword field matches one of our generated keywords
         (see _signal_keyword_matches() — UNCHANGED, still exact/perfect,
         same as v3-v7), OR
      2. (v7, word-boundary tightened by BUGFIX PACK #2) one of our
         generated keywords appears as a whole-word/phrase match inside
         its OWN title, OR
      3. (v7, word-boundary tightened by BUGFIX PACK #2) one of our
         generated keywords appears as a whole-word/phrase match inside
         its OWN post_text.
    Matching via more than one of these at once still only ever produces
    ONE entry in the results (de-duplicated by post_url exactly as
    before) — this only widens WHICH signals can match, it never changes
    how a matched signal is de-duplicated or shaped.

    `targeting_platform` (the same "all" | "reddit" | "x_twitter" |
    "linkedin" | "facebook" value already stored on the job/message) is
    applied on top: "all" pulls a match from whichever platform it came
    from, exactly as before; any specific platform restricts matches to
    signals from that platform only — the user's dropdown choice decides
    this, nothing else.

    (v7) PER-PLATFORM CAP: on top of the existing overall `limit`
    (MAX_MATCHED_RESULTS by default — still respected, still the same
    variable/behavior as before), each individual platform can
    contribute AT MOST MAX_POSTS_PER_PLATFORM matches to this call's
    results (default 3).

    (TIME-WINDOW FEATURE) `since_days`, default None: when a positive int
    is given, this ADDS an extra AND-condition on top of everything
    above — a signal must ALSO have `created_utc` within the last
    `since_days` days to be eligible at all. This is applied BOTH at the
    Mongo query level (so old signals are excluded from the DB fetch
    itself, not just filtered out afterward in Python) AND, defensively,
    once more in the Python loop below (in case a signal doc is missing a
    usable `created_utc` value — such a doc is simply excluded rather
    than assumed to pass). `since_days=None` (the default, and what every
    call site used before this feature) means NO time filtering at all —
    behavior is then 100% identical to before this feature. The time
    window is purely a narrowing on top of keyword matching: a signal
    still has to satisfy the exact same keyword rules 1-3 above; the time
    window can only ever exclude MORE signals, never match one that
    wouldn't otherwise match on keywords.

    Results are sorted by `created_utc` DESCENDING (most recent first)
    before the caps above are applied.

    Returns {title, post_text, post_url, platform} for each match — this
    is the only signal-derived output ever shown to the user (via post
    cards) or persisted onto a chat message's `results` (platform is
    included only so the UI can show which platform a result came from;
    it isn't used for anything else here). Never touches jobs_collection
    or the raw `signals` list returned by get_signals().

    COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP: this function's
    SIGNATURE (aside from the new, optional, default-None `since_days`
    parameter), RETURN SHAPE, matching rules, and every existing caller
    are exactly as they were in v7 — it has no idea whether `keywords`
    came from Claude's router call or the old fuzzy-template fallback."""
    if unfiltered:
        return flintel.get_unfiltered_matched_signals(
            signals_collection,
            since_days=since_days,
            targeting_platform=targeting_platform,
            limit=limit or MAX_MATCHED_RESULTS,
            max_per_platform=MAX_POSTS_PER_PLATFORM,
        )

    limit = limit or MAX_MATCHED_RESULTS
    keyword_list = [k for k in (keywords or []) if k]
    keyword_set = {k.strip().lower() for k in keyword_list}
    if not keyword_set:
        return []

    # (v7) Widen the Mongo query itself: previously this only ever
    # filtered on the keyword field(s) with $in. Now it ALSO fetches any
    # doc whose title/text field candidates contain one of our keywords
    # as a case-insensitive substring (via a single combined regex per
    # field), so documents that would only match via title/text (and
    # never had a matching search_keyword field) are actually retrieved
    # here in the first place, instead of being invisible to the query
    # before the Python-side check below even gets a chance to run. This
    # remains an intentionally LOOSE superset fetch — the real, tightened
    # accept/reject decision happens in the Python loop below via
    # _text_matches_keyword() (see BUGFIX PACK #2), so leaving this query
    # loose never causes a false positive to slip through.
    or_conditions = [{field: {"$in": keyword_list}} for field in _KEYWORD_FIELD_CANDIDATES]
    escaped_keywords = [re.escape(k) for k in keyword_list if k]
    if escaped_keywords:
        combined_pattern = "|".join(escaped_keywords)
        for field in _TITLE_FIELD_CANDIDATES + _TEXT_FIELD_CANDIDATES:
            or_conditions.append({field: {"$regex": combined_pattern, "$options": "i"}})
    mongo_query = {"$or": or_conditions}

    # (TIME-WINDOW FEATURE) Compute an optional cutoff and AND it onto the
    # existing $or clause via $and, so a time window narrows the keyword
    # match instead of replacing it. since_days is sanity-clamped between
    # 1 and MAX_TIME_WINDOW_DAYS — anything else (None, 0, negative,
    # absurdly large) means "no time filter", handled the exact same way
    # this function always behaved before this feature.
    cutoff = None
    if isinstance(since_days, int) and since_days > 0:
        clamped_days = min(since_days, MAX_TIME_WINDOW_DAYS)
        cutoff = datetime.now(timezone.utc) - timedelta(days=clamped_days)
        mongo_query = {"$and": [mongo_query, {"created_utc": {"$gte": cutoff}}]}

    # Fetch a larger pool than `limit` since matches are now filtered
    # further (per-platform caps below), same spirit as the old `limit *
    # 5` headroom, just bumped up a bit since the query itself is now
    # broader too.
    raw_docs = list(
        signals_collection.find(mongo_query, {"_id": 0})
        .sort("created_utc", -1)
        .limit(limit * 10)
    )

    matched = []
    seen_urls = set()
    platform_counts = {}  # (v7) per-platform running count for this call

    for doc in raw_docs:
        title     = _first_present(doc, _TITLE_FIELD_CANDIDATES)
        post_text = _first_present(doc, _TEXT_FIELD_CANDIDATES)

        # (TIME-WINDOW FEATURE) Defensive second check: if a cutoff is
        # active, make sure this doc's own created_utc actually satisfies
        # it too (guards against a doc with a missing/odd created_utc
        # slipping through the Mongo-level filter in some edge case) — a
        # doc with no usable created_utc is excluded rather than assumed
        # to pass, since we can't confirm it's within the window.
        if cutoff is not None:
            doc_created = doc.get("created_utc")
            if not isinstance(doc_created, datetime):
                continue
            if doc_created.tzinfo is None:
                doc_created = doc_created.replace(tzinfo=timezone.utc)
            if doc_created < cutoff:
                continue

        # (v7) A signal matches if EITHER its search_keyword field matches
        # (unchanged, exact match), OR the keyword shows up as a whole
        # word/phrase inside its own title, OR inside its own post_text.
        # Any one of the three is enough.
        is_match = (
            _signal_keyword_matches(doc, keyword_set)
            or _text_matches_keyword(title, keyword_set)
            or _text_matches_keyword(post_text, keyword_set)
        )
        if not is_match:
            continue
        if not _signal_platform_matches(doc, targeting_platform):
            continue

        post_url  = _first_present(doc, _URL_FIELD_CANDIDATES)
        platform  = _first_present(doc, _PLATFORM_FIELD_CANDIDATES) or _infer_platform_from_url(post_url)

        if not title and not post_text and not post_url:
            continue
        if post_url and post_url in seen_urls:
            continue

        # (v7) Per-platform cap: once a platform has already contributed
        # MAX_POSTS_PER_PLATFORM matches to this call, skip any further
        # matches from that same platform (but keep scanning raw_docs —
        # a different platform may still have room).
        platform_key = (platform or "unknown").strip().lower()
        if platform_counts.get(platform_key, 0) >= MAX_POSTS_PER_PLATFORM:
            continue

        if post_url:
            seen_urls.add(post_url)

        matched.append({"title": title, "post_text": post_text, "post_url": post_url, "platform": platform})
        platform_counts[platform_key] = platform_counts.get(platform_key, 0) + 1

        if len(matched) >= limit:
            break

    return matched


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS LAYER (v4)
#
# Matched signals never get dumped to the user directly. They're handed to
# Claude (title + text ONLY — never post_url, never platform, never job
# internals) together with the user's actual chat prompt, and Claude turns
# that into the final answer.
#
# Only the FINAL ANSWER TEXT is ever stored (see save_claude_answer_to_chat
# below) — the input posts are never re-saved next to it, since they
# already live in flintel_signals / are reconstructable from the message's
# own keyword list. That's the cost-saving rule: store output only.
#
# COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP OR THE TIME-WINDOW /
# PAIN-POINT / CLARIFY FEATURE: this whole section (CLAUDE_ANALYSIS_
# SYSTEM_PROMPT, build_claude_post_context(), chunk_list(),
# _format_posts_block(), _map_chunk(), _call_claude()) only ever consumes
# ALREADY-MATCHED posts (the output of get_matched_signals()) — it has no
# idea, and doesn't care, which keyword list or time window produced those
# matches.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_ANALYSIS_SYSTEM_PROMPT = """
You are the answer-generation brain inside Flintel, a social-listening
platform. You are handed a user's message plus whatever real public posts
(title + text only — never a URL, platform, or internal job/keyword data)
were matched for it, and your job is to turn that into the single best
possible answer for the user.

CORE PRINCIPLE — work through this every time, silently, before answering:
UNDERSTAND the request -> INTERPRET what's actually being asked ->
REASON over what you were given -> VERIFY it's enough to answer honestly ->
DECIDE the right format -> RESPOND. Never skip straight to an answer
without checking whether the grounding you were given actually supports it.

GROUNDING — this overrides everything else below:
- Every factual claim must come from the matched posts you were given.
  Never invent a post, a stat, a quote, or a sentiment that isn't actually
  supported by what's in front of you.
- You are not limited to a fixed set of anticipated questions or exact
  keyword matches. People express the same intent in endless different
  ways ("looking for X" / "anyone know a good X" / "struggling with X,
  what are you all using" / informal, sarcastic, abbreviated, typo'd,
  multi-part, or indirect phrasing) — read for meaning and intent, not
  surface wording.

CONVERSATION CONTINUITY — DECLINED ALTERNATIVES:
If a short summary of earlier turns in this same conversation is included
in your input below (labeled "Conversation so far"), read it BEFORE
writing "suggested_actions", "likely_reason", "followups", or any other
suggestion field. If the user has already explicitly declined, rejected,
or narrowed away from a broader scope, an alternative location, an
alternative platform, or an alternative term in an earlier turn (e.g.
"no, only X", "sirf X chahiye", "not Y, just X"), do NOT offer that same
already-declined alternative again in this answer. If, after honoring
that narrower scope, there is still genuinely nothing relevant to show,
say so plainly and honestly (e.g. "I searched for this in the system but
didn't find relevant posts for it") instead of re-suggesting the
alternative the user already turned down. This applies regardless of
format — it only changes what you suggest, never whether you searched or
what data you were grounded in.

OUTPUT CONTRACT — STRICT JSON ONLY, no markdown code fences, no preamble,
no text outside the JSON object. Every response is exactly one JSON object,
and `"format"` is ALWAYS the first field so the frontend knows which
render function to call. Pick exactly one of the six formats below based
on what the user actually asked and what you were able to find.

──────────────────────────────────────────────────────────────────────────
FORMAT 1 — "source_list"
For sentiment/opinion queries ("What are people saying about X?").
{
  "format": "source_list",
  "summary": "<2-4 sentence plain-English summary of overall sentiment/themes>",
  "ranked": false,
  "platforms": [
    {
      "platform": "<reddit|x|linkedin|facebook>",
      "total_analyzed": <int — honest count of relevant posts actually found for this platform>,
      "shown_count": <int — how many are in "posts" below>,
      "posts": [
        {
          "source": "<subreddit/handle/page name>",
          "title": "<post title, or a short label if the post has none>",
          "summary": "<1-2 sentence paraphrase of the post>",
          "sentiment": "<positive|mixed|negative|neutral>",
          "link": "<real post URL if available, else omit this field entirely>"
        }
      ]
    }
  ],
  "followups": ["<3 short natural next-question suggestions>"]
}
Only include platforms that actually returned usable data — never an empty
platform section. Set "ranked": true (instead of false) when the user
asked for something specific and ordered (e.g. "top 10 complaints") — same
schema, but posts are ordered by rank/relevance and the frontend numbers
them instead of grouping them.

──────────────────────────────────────────────────────────────────────────
FORMAT 2 — "trend_report"
For "how has sentiment changed over time" / "sentiment over the last N
days" queries.
{
  "format": "trend_report",
  "topic": "<brand/topic name>",
  "window": "<e.g. 'Last 30 days'>",
  "platforms": "<comma-separated platforms actually covered>",
  "shift_table": {
    "headers": ["Metric", "<period start label>", "<period end label>", "Change"],
    "rows": [["Positive mentions", "52%", "61%", "+9 pts"], ...]
  },
  "interpretation": "<2-4 sentences explaining what's driving the shift, grounded in the posts>",
  "weekly_table": {
    "headers": ["Week", "Positive", "Neutral", "Negative", "Notable Events"],
    "rows": [["Week 1 (Aug 3-9)", "50%", "35%", "15%", "<short grounded note>"], ...]
  },
  "positive_drivers": [
    {"platform": "<platform>", "post": "<paraphrased post>", "sentiment": "positive", "theme": "<short theme label>"}
  ],
  "negative_drivers": [
    {"platform": "<platform>", "post": "<paraphrased post>", "sentiment": "negative", "theme": "<short theme label>"}
  ],
  "trend": "<1-3 sentences on the trajectory going forward, grounded in what's actually observed>",
  "takeaways": ["<3-5 short, concrete bullet takeaways>"]
}
Every table row and every driver entry must be grounded in real matched
posts — never fabricate a percentage or a week's numbers you don't
actually have evidence for. If there isn't enough data to fill in a
week-by-week or driver breakdown honestly, omit that field rather than
inventing numbers to complete the shape.

──────────────────────────────────────────────────────────────────────────
FORMAT 3 — "comparison"
For "Compare X vs Y" queries (2 or more subjects).
{
  "format": "comparison",
  "summary": "<2-4 sentence summary of how the subjects differ>",
  "subjects": [
    {
      "name": "<subject name>",
      "sentiment": {"positive": "<pct>", "neutral": "<pct>", "negative": "<pct>"},
      "platforms": [ /* same platforms/posts structure as source_list, including "sentiment" on every post */ ]
    }
  ],
  "followups": ["<3 short natural next-question suggestions>"]
}

──────────────────────────────────────────────────────────────────────────
FORMAT 4 — "no_results"
For when little or nothing relevant was actually found.
{
  "format": "no_results",
  "searched": {"query": "<what was searched>", "platforms": ["<...>"], "time_window": "<e.g. 'last 7 days'>"},
  "message": "<plain statement of what was searched and that little/nothing turned up>",
  "likely_reason": "<brief, genuine explanation — e.g. small/new brand, private-group discussion>",
  "suggested_actions": [
    {"type": "broaden_time", "label": "<e.g. 'Extend to last 30 days'>"},
    {"type": "broaden_platforms", "label": "<e.g. 'Include all platforms + news'>"},
    {"type": "broaden_term", "label": "Search a broader term", "suggestion": null}
  ],
  "clarifying_question": "<only include this field if asking for more context would genuinely help — omit otherwise>"
}
"suggestion" inside suggested_actions must be null unless there's a
genuinely grounded alternative term to offer — never invent a
plausible-sounding brand/term with no real signal behind it.

CRITICAL GUARDRAIL FOR "likely_reason" (and every other text field in
this format, and in "not_available"/"disallowed" below): NEVER name,
suggest, or imply any platform, tool, marketplace, directory, search
engine, community, or channel OUTSIDE Flintel as a better place to look
— this includes but is not limited to Google, app/tool directories,
review sites, Slack, Discord, niche forums, or any other product. Doing
so tells the user to leave Flintel for something else, which this
product must never do, regardless of whether the observation is
factually true. If you genuinely believe the conversation is happening
somewhere Flintel doesn't cover, phrase "likely_reason" purely in terms
of why THIS search (these keywords, this time window, these platforms)
came up short — e.g. "the exact solution name isn't something people
usually type in complaint-style posts" or "this is a newer/niche term
that hasn't built up much public discussion yet" — and let
"suggested_actions" (broaden_time / broaden_platforms / broaden_term,
all of which are things FLINTEL ITSELF can do) be the only next steps
offered. Never write anything that reads as "go search somewhere else
instead."

TONE FOR "no_results" (write like a sharp analyst reporting back, not a
form rejection):
- Open by stating plainly WHAT was searched and WHERE (platforms,
  keyword theme) — e.g. "I searched X, Y, Z for people discussing <theme>."
- Be honest that no strong/high-intent match was found, but frame it as
  information, not failure — e.g. "This doesn't mean there's no demand —
  it likely means the search was too narrow, or people describe this
  differently than expected."
- If ANY posts were matched at all (even loosely relevant, low-intent
  ones), do not discard them — describe what was found in plain terms,
  optionally grouped by how relevant/strong the signal is, so the user
  sees real signal instead of a blank "nothing found."
- Never sound like a rejection or a canned apology.
- Never point the user toward a different platform, tool, or channel as
  the place to actually find this — Flintel's own suggested_actions are
  the only next steps to offer.

──────────────────────────────────────────────────────────────────────────
FORMAT 5 — "not_available"
For capabilities Flintel doesn't support yet (e.g. job listings, anything
outside social-listening).
{
  "format": "not_available",
  "message": "<brief, honest explanation of what isn't available yet and what Flintel can do instead>"
}

CLARIFICATION — "not_available" vs "no_results":
"not_available" is ONLY for things Flintel structurally cannot do at all
(e.g. job listings, building a dossier on a named individual, anything
outside social-listening entirely). A request like "find me customers for
my website/product" IS a valid social-listening search — Flintel searches
for relevant conversations using keywords derived from the site/topic.
If that search runs but finds little or nothing, that is a "no_results"
outcome, NEVER "not_available" — do not decline a legitimate lead-gen or
customer-discovery ask just because it's framed as "finding customers";
treat it exactly like any other search that came up empty.

──────────────────────────────────────────────────────────────────────────
FORMAT 6 — "disallowed"
For requests to identify, profile, or target a specific named individual
person — Flintel only analyzes public conversation about topics/brands,
never builds a profile on a person.
{
  "format": "disallowed",
  "message": "<brief, non-preachy explanation, redirecting to what Flintel can help with instead>"
}

──────────────────────────────────────────────────────────────────────────
SENTIMENT TAG RULE (applies to every post, in every format above, with no
exception): every individual post object must include a "sentiment" field
set to exactly one of these four lowercase strings — "positive", "mixed",
"negative", "neutral" — and nothing else. Never omit this field on any
post. Never use a free-text or capitalized value. The frontend maps these
four exact values to fixed colored tags — any other value fails to render.

──────────────────────────────────────────────────────────────────────────
SUGGESTION/FOLLOW-UP LENGTH RULE (applies to "followups" in source_list
and comparison, and to every "label" and "clarifying_question" inside
suggested_actions in no_results):
- Keep every suggestion SHORT — roughly 4-8 words, one simple sentence
  or phrase, never a long or compound sentence.
- Use plain, everyday words a person would actually type or say — no
  formal, wordy, or corporate phrasing.
- The MEANING/INTENT of each suggestion must stay exactly the same as it
  would have been otherwise — this rule only shortens and simplifies the
  WORDING, it never changes what the suggestion is asking or offering.
- Example — too long: "Would you like me to extend the search window to
  the last 30 days to see if there's more relevant conversation?" —
  instead write: "Extend to last 30 days?"
- Example — too long: "You could try searching for a more specific or
  narrower term related to your brand or product." — instead write:
  "Try a more specific term?"

RESPONSE FORMAT INTELLIGENCE:
- The format above is chosen by what the user is asking and what you
  found — not by rigid keyword triggers. A comparison request gets
  "comparison" even if worded unusually; a request for "the top 10 X"
  still uses "source_list" with "ranked": true, not a new shape.
- If you genuinely cannot find enough to support "source_list",
  "trend_report", or "comparison" honestly, use "no_results" instead of
  forcing a thin answer into one of those shapes.

TONE (applies to every text field you write inside any format above):
- Plain, direct, conversational — the way a sharp analyst explains
  findings to a colleague. No "As an AI..." framing, no restating the
  question back, no filler openers, no corporate hedging.
- Never claim more confidence than the grounding supports.
"""

# Cheap "map" step used only when a topic has enough matched posts that
# sending them all in one shot would be wasteful/risky context-wise. Each
# chunk gets condensed down to only the points relevant to the user's
# question before the final Haiku call ever sees them.
#
# UNCHANGED.
CLAUDE_MAP_STEP_SYSTEM_PROMPT = """
You are helping analyze one batch of social media posts (Reddit/X) about a
topic, as a pre-processing step before another AI writes the actual answer.
Read the user's question and the posts below (title + text only). Pull out
ONLY the grounded points from THESE posts that are relevant to the user's
question — specific complaints, praise, themes, or facts actually present
in the text. Do not invent anything not present in the posts. Do not answer
the user's question yet — just list the relevant grounded points as short
bullets. If nothing in these posts is relevant to the question, say exactly:
"No relevant points in this batch." Never include, guess, or reference a
post URL — you were not given any.
"""

# (BUGFIX PACK #3) Second-level "notes-of-notes" condensing step — only
# ever invoked when a topic already needed enough first-level post
# chunking that the resulting notes themselves would be too many to
# combine directly in one final reduce call. Purely consolidates/
# de-duplicates ALREADY-grounded notes; never introduces anything new.
CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT = """
You are consolidating multiple batches of already-condensed grounded notes
about social media posts, as a pre-processing step before another AI
writes the final answer. Read the user's question and the notes below.
Combine them into a single, shorter set of grounded bullet points relevant
to the user's question — remove duplicates, merge repeated themes, but
never invent anything not already present in the notes. Do not answer the
user's question yet — just output the consolidated grounded bullets. If
none of the notes contain anything relevant to the question, say exactly:
"No relevant points in this batch."
"""


def build_claude_post_context(matched_signals: list) -> list:
    """Strips a matched-signals list (which has title/post_text/post_url/
    platform, used for post cards) down to ONLY title + text — this is the
    single point where post_url and platform are dropped before anything
    is sent to Claude. Skips a post entirely if it has neither a title nor
    any text to offer."""
    posts = []
    for m in matched_signals or []:
        title = (m.get("title") or "").strip()
        text  = (m.get("post_text") or "").strip()
        if not title and not text:
            continue
        posts.append({"title": title, "text": text})
    return posts


def chunk_list(items: list, chunk_size: int) -> list:
    """Generic chunker: splits `items` into consecutive batches of at most
    `chunk_size`. Used to keep each Claude call's context small and cheap
    regardless of how many posts a topic matched."""
    if not items:
        return []
    if chunk_size <= 0:
        return [items]
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def _format_posts_block(posts: list) -> str:
    lines = []
    for i, p in enumerate(posts, 1):
        title = p.get("title") or "(no title)"
        text = p.get("text") or "(no text)"
        lines.append(f"[Post {i}]\nTitle: {title}\nText: {text}")
    return "\n\n".join(lines)


def _call_claude(system_prompt: str, user_message: str, max_tokens: int = None) -> str:
    """Single call to the Anthropic Messages API. Raises on any failure —
    callers decide how to degrade gracefully (never let this block the
    search job or the post cards, which don't depend on Claude at all)."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens or CLAUDE_MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": CLAUDE_API_VERSION,
        "content-type": "application/json",
    }

    with httpx.Client(timeout=CLAUDE_TIMEOUT_SECONDS) as http_client:
        response = http_client.post(CLAUDE_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    text_blocks = [
        block.get("text", "") for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    return "\n".join(t for t in text_blocks if t).strip()


def _map_chunk(query: str, posts_chunk: list) -> str:
    posts_block = _format_posts_block(posts_chunk)
    user_message = f"User's question: {query}\n\nPosts:\n{posts_block}"
    return _call_claude(CLAUDE_MAP_STEP_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_MAP_MAX_TOKENS)


def _condense_notes_chunk(query: str, notes_chunk: list) -> str:
    """(BUGFIX PACK #3 — 2ND-LEVEL CHUNKING) Consolidates one batch of
    already-condensed first-level notes into a single, shorter set of
    grounded bullets, using CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT. Only ever
    called from analyze_with_claude() when the number of first-level
    notes exceeds CLAUDE_NOTES_PER_CHUNK — see below."""
    notes_block = "\n\n---\n\n".join(notes_chunk)
    user_message = f"User's question: {query}\n\nNotes:\n{notes_block}"
    return _call_claude(CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_MAP_MAX_TOKENS)


def analyze_with_claude(query: str, matched_signals: list, extra_context: str = None) -> str:
    """Turns (user question + matched signals) into the actual answer the
    user sees, using CLAUDE_ANALYSIS_SYSTEM_PROMPT. Handles three cases:

      1. No usable posts at all -> Claude still answers, told plainly that
         there's no post data yet, per the system prompt's own grounding
         rule. (v6: this is the exact branch the response-timeout fallback
         in _fill_in_message_outputs() relies on — it calls this function
         with an empty list purely to reach this case.)
      2. Few enough posts to fit one call -> single direct call.
      3. Enough posts that chunking is worth it -> map step condenses each
         chunk of CLAUDE_POSTS_PER_CHUNK posts down to grounded notes,
         then (BUGFIX PACK #3) IF there are more of those notes than
         CLAUDE_NOTES_PER_CHUNK, a second condensing pass chunks the notes
         themselves and condenses each batch further first, THEN one
         final reduce call (still using the main system prompt) turns
         all the (possibly twice-condensed) notes + the question into the
         final answer. Small/medium topics skip the second pass entirely
         and behave exactly as before this bugfix pack.

    Returns the final answer text only — this is the only thing callers
    should persist (see save_claude_answer_to_chat).

    UNCHANGED except for the additive second-level chunking pass described
    above (BUGFIX PACK #3), and the additive optional `extra_context`
    parameter: when provided, it's appended to the user_message built in
    every branch below, right before that branch's _call_claude(...) call
    — no other line of this function's logic/branches changes. This
    function only ever sees ALREADY-MATCHED posts — it has no idea whether
    a time window was applied to produce them.

    (SIMULATED-STREAM FIX) This function is ALSO now the one
    GET /chat/{chat_id}/stream calls to get its complete answer text —
    still exactly this same, byte-for-byte unchanged function, no new
    parameters, no new branches. See that route / the module docstring
    for why."""
    posts = build_claude_post_context(matched_signals)

    if not posts:
        user_message = (
            f"User's question: {query}\n\n"
            "No posts were found for this topic yet — you have no post data "
            "to ground an answer in. Say that plainly, then answer anything "
            "else in the question you still can from general knowledge."
        )
        if extra_context:
            user_message += "\n\n" + extra_context
        return _call_claude(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)

    chunks = chunk_list(posts, CLAUDE_POSTS_PER_CHUNK)

    if len(chunks) <= 1:
        posts_block = _format_posts_block(posts)
        user_message = f"User's question: {query}\n\nPosts (title + text only):\n{posts_block}"
        if extra_context:
            user_message += "\n\n" + extra_context
        return _call_claude(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)

    # Multiple chunks -> map-reduce so no single call has to swallow every
    # matched post at once.
    notes = []
    for chunk in chunks:
        try:
            note = _map_chunk(query, chunk)
        except Exception as exc:
            log.warning(f"Claude map-step failed for a chunk (skipping chunk): {exc}")
            continue
        if note:
            notes.append(note)

    # (BUGFIX PACK #3 — 2ND-LEVEL CHUNKING) If the number of condensed
    # first-level notes itself is large enough that combining them all in
    # one final reduce call could be unwieldy, chunk the notes into
    # batches of CLAUDE_NOTES_PER_CHUNK and condense each batch further
    # with one extra cheap call per batch, BEFORE the final reduce call
    # below. This only activates for topics big enough to need it — for
    # everything else `notes` is left exactly as-is, so behavior is
    # identical to before this bugfix pack.
    if len(notes) > CLAUDE_NOTES_PER_CHUNK:
        note_chunks = chunk_list(notes, CLAUDE_NOTES_PER_CHUNK)
        condensed_notes = []
        for note_chunk in note_chunks:
            try:
                condensed = _condense_notes_chunk(query, note_chunk)
            except Exception as exc:
                log.warning(f"Claude notes-condense step failed for a batch (skipping batch): {exc}")
                continue
            if condensed:
                condensed_notes.append(condensed)
        if condensed_notes:
            notes = condensed_notes

    combined_notes = "\n\n---\n\n".join(notes) if notes else "(no grounded points extracted)"
    user_message = (
        f"User's question: {query}\n\n"
        f"Below are grounded notes already condensed from {len(posts)} posts "
        f"(title + text only), split into batches. Treat these notes as your "
        f"only factual grounding about the posts, and answer the user's "
        f"actual question naturally.\n\nNotes:\n{combined_notes}"
    )
    if extra_context:
        user_message += "\n\n" + extra_context
    return _call_claude(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)


# ─────────────────────────────────────────────────────────────────────────────
# STREAMING ADD-ON — additive alternate path only, see module docstring.
# _call_claude() and analyze_with_claude() above are completely untouched
# and remain what every existing caller uses.
#
# (SIMULATED-STREAM FIX) `_call_claude_stream()` and
# `analyze_with_claude_stream()` below are kept fully intact, unchanged,
# and importable/callable exactly as before — GET /chat/{chat_id}/stream
# simply no longer calls them (it now calls the blocking analyze_with_claude()
# instead, so it can post_url-patch the answer before ever sending any of it
# out). Nothing about these two functions themselves changed.
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude_stream(system_prompt: str, user_message: str, max_tokens: int = None):
    """(STREAMING ADD-ON) Same Anthropic Messages API call as
    _call_claude(), except with "stream": true — instead of blocking
    until the whole response is ready, this yields each text delta AS
    Anthropic streams it back (word-by-word / token-by-token), so a
    caller can forward pieces to the browser live instead of waiting for
    the entire answer.

    Purely ADDITIVE: _call_claude() itself is untouched and is still used
    by every existing caller (routing, map step, notes-reduce step, the
    non-streaming analyze_with_claude()). This generator is only used by
    analyze_with_claude_stream().

    Yields plain text chunks (str). Raises on any failure — same
    degrade-gracefully convention as _call_claude()."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens or CLAUDE_MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
        "stream": True,
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": CLAUDE_API_VERSION,
        "content-type": "application/json",
    }

    with httpx.Client(timeout=CLAUDE_TIMEOUT_SECONDS) as http_client:
        with http_client.stream("POST", CLAUDE_API_URL, headers=headers, json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    event = json.loads(data_str)
                except (ValueError, TypeError):
                    continue
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {}) or {}
                    text = delta.get("text")
                    if text:
                        yield text


def analyze_with_claude_stream(query: str, matched_signals: list):
    """(STREAMING ADD-ON) Mirrors analyze_with_claude()'s exact branches
    (no posts / single call / map-reduce, including the BUGFIX PACK #3
    second-level note chunking) byte-for-byte — the ONLY difference is
    that the final, user-facing call streams its text via
    _call_claude_stream() instead of blocking via _call_claude(). Any
    earlier map/notes-condense calls (never shown to the user directly)
    are UNCHANGED — still plain, blocking _call_claude() calls, exactly
    as in analyze_with_claude().

    This is a generator: yields text chunks (str) as they stream in. The
    caller is responsible for collecting them into the final full answer
    — this function itself does not persist anything, exactly like
    analyze_with_claude().

    (SIMULATED-STREAM FIX) Left fully intact and unchanged. No longer
    called by GET /chat/{chat_id}/stream (see that route + the module
    docstring), but still here, still correct, still usable by any future
    caller that genuinely wants raw live token-by-token output rather
    than the patched-then-paced text the stream route now sends."""
    posts = build_claude_post_context(matched_signals)

    if not posts:
        user_message = (
            f"User's question: {query}\n\n"
            "No posts were found for this topic yet — you have no post data "
            "to ground an answer in. Say that plainly, then answer anything "
            "else in the question you still can from general knowledge."
        )
        yield from _call_claude_stream(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)
        return

    chunks = chunk_list(posts, CLAUDE_POSTS_PER_CHUNK)

    if len(chunks) <= 1:
        posts_block = _format_posts_block(posts)
        user_message = f"User's question: {query}\n\nPosts (title + text only):\n{posts_block}"
        yield from _call_claude_stream(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)
        return

    # Map step + optional 2nd-level note chunking: identical logic to
    # analyze_with_claude(), still blocking (never shown to the user
    # directly) — only the final call below streams.
    notes = []
    for chunk in chunks:
        try:
            note = _map_chunk(query, chunk)
        except Exception as exc:
            log.warning(f"Claude map-step failed for a chunk (skipping chunk): {exc}")
            continue
        if note:
            notes.append(note)

    if len(notes) > CLAUDE_NOTES_PER_CHUNK:
        note_chunks = chunk_list(notes, CLAUDE_NOTES_PER_CHUNK)
        condensed_notes = []
        for note_chunk in note_chunks:
            try:
                condensed = _condense_notes_chunk(query, note_chunk)
            except Exception as exc:
                log.warning(f"Claude notes-condense step failed for a batch (skipping batch): {exc}")
                continue
            if condensed:
                condensed_notes.append(condensed)
        if condensed_notes:
            notes = condensed_notes

    combined_notes = "\n\n---\n\n".join(notes) if notes else "(no grounded points extracted)"
    user_message = (
        f"User's question: {query}\n\n"
        f"Below are grounded notes already condensed from {len(posts)} posts "
        f"(title + text only), split into batches. Treat these notes as your "
        f"only factual grounding about the posts, and answer the user's "
        f"actual question naturally.\n\nNotes:\n{combined_notes}"
    )
    yield from _call_claude_stream(CLAUDE_ANALYSIS_SYSTEM_PROMPT, user_message)


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE ROUTING LAYER (v5, extended in v6 with abuse/harm blocking, again
# with keyword generation, again with time-window parsing, pain-point-aware
# keyword generation, and a 4th "clarify" intent, and now again with the
# ROUTER INTENT REFINEMENT described in the module docstring above)
#
# Runs BEFORE anything else in POST /search. A single cheap Claude call
# decides whether the user's message is a genuine "search" (wants social-
# listening data pulled about a brand/product/topic, INCLUDING a general
# pain-point/complaint angle on a platform — see ROUTER INTENT REFINEMENT),
# a plain "chat" message (INCLUDING a truly open-ended "what's happening on
# this platform" question with no subject at all — see ROUTER INTENT
# REFINEMENT), "blocked" (abusive/harmful content), or "clarify" (a
# search-shaped message with truly nothing — not even a general angle — to
# search for).
#
# (KEYWORD-GENERATION SWAP) The SAME call now ALSO returns a "keywords"
# field when intent="search" — still just ONE Claude call total, no new
# round trip.
#
# (TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE) The SAME call now ALSO
# returns a "time_window_days" field when intent="search" (int, or null
# if the user gave no time range), generates pain-point/prospect-style
# keywords when that's what the message is asking for, and can return
# intent="clarify" instead of guessing at an unclear topic.
#
# Safety rule: ANY failure here (bad JSON, API error, timeout, missing
# key) defaults to {"intent": "search", "reply": None, "keywords": None,
# "time_window_days": None} so the pre-existing pipeline is always the
# fallback — this routing layer can only ever add a shortcut, it can
# never silently swallow a real search request or leave one with zero
# keywords.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_ROUTER_SYSTEM_PROMPT = """
You are the routing brain inside Flintel, a social-listening platform.
Every message a user types goes through you FIRST, before anything else
happens in the product.
Your job: classify this message into exactly one of FOUR types, and for
"search" messages, ALSO generate the keyword list Flintel's own
(unchanged, plain-Python) matching code will use afterward, plus an
optional time window.

1. "search" — the message is asking Flintel to research/monitor/pull
   social-media data about a brand, product, company, person, industry,
   or topic, AND that topic/subject is actually clear from the message
   itself (or from the conversation history below). For "search"
   messages, ALSO return:

   - "keywords": an array of search terms.
     - Read the user's own words and figure out what they are actually
       asking about. A short, narrow prompt ("reddit posts about AI")
       needs only the ONE (or two) keyword(s) that actually capture the
       real topic — e.g. just "AI" — do NOT pad it out with unrelated
       angles ("AI review", "AI pricing", "AI complaints", etc.) the user
       never asked about.
     - A broader or more detailed prompt can warrant more keyword
       variations (genuine synonyms or short related phrases actually
       likely to appear in real posts) — up to 10 keywords maximum,
       never more.
     - PAIN-POINT / PROSPECT PATTERN: if the user describes selling or
       promoting something and wants to find people who might need it
       (e.g. "I run an AI agent company, find people whose website is
       slow / carts are getting abandoned", "I run a travel agency, find
       people saying their travel experience was bad", or any similar
       "I offer X, find people with problem Y" message, in ANY industry —
       these are just examples, not a fixed list), generate keywords
       around the PROBLEM/SYMPTOM the user described (e.g. "website
       slow", "site is slow", "abandoned cart", "bad travel experience",
       "trip was ruined", "poor customer service"), NOT around the
       seller's own product or company name. Read whatever problem the
       user actually describes and reflect that back — never assume a
       fixed industry or fixed symptom list.
     - GENERAL PAIN-POINT / COMPLAINT PATTERN (no brand, product, or
       industry named at all): a message like "reddit par log kya
       problems face kar rahe hain" or "what are people complaining
       about on twitter" names NO specific brand/product/industry, but it
       DOES name a real, searchable subject — general problems/
       complaints/frustrations on that platform. Treat this as "search"
       (NOT "clarify" — see the "clarify" rules below), and generate
       keywords around common frustration/complaint phrasing that would
       plausibly appear in real posts, e.g. "so frustrated with", "sick
       of dealing with", "worst experience with", "wish there was a
       better", "fed up with", "nightmare trying to", "why is it so hard
       to", "anyone else hate". This is DIFFERENT from the fully
       open-ended "what's happening on this platform" case described
       under "chat" below — the distinguishing line is whether the user
       named ANY angle (a brand, a product, an industry, or a general
       problem/complaint framing) versus naming nothing at all beyond the
       platform itself.
     - Every keyword must be something that could plausibly appear
       verbatim, or as a close natural substring, inside a real post's
       title or text. Keep each keyword short and natural.
     - Never include meta wording that describes the user's REQUEST to
       you rather than the topic itself — words like "reddit", "twitter",
       "x", "linkedin", "facebook", "posts", "posts about", "show me",
       "today", "find", "search" describe what/where the user wants
       searched, not something that would appear inside an actual post,
       so leave them out of the keyword list (the platform itself is
       handled separately, by a dropdown the user already picked — you
       are only responsible for the topic keywords).

   - "time_window_days": an integer, or null.
     - If the user's message itself implies a time range, convert it to
       an approximate number of days: "today"/"aaj" -> 1, "this week" /
       "last 7 days" -> 7, "last 2 weeks" -> 14, "last month" / "past 30
       days" -> 30, "last 3 months" -> 90, "last 6 months" -> 180, "last
       year" -> 365, and so on for any other phrasing that clearly names
       a time span.
     - If the user gives NO time range at all, this MUST be null — do
       NOT invent or assume one.
     - A missing/null time_window_days is NOT by itself a reason to ask
       for clarification (see "clarify" below) — plenty of valid search
       requests never mention a time range at all.

   Do NOT try to answer the user's question yourself for "search" —
   only classify and produce the keyword list / time window.

2. "chat" — a normal conversational message that doesn't need any new
   data pulled at all: greetings ("hi", "hello", "what's up", "kia chal
   raha hai aj kal"), small talk, thanks, general knowledge questions, a
   follow-up question about something already discussed in this
   conversation, or a request to just talk. Answer the user's message
   yourself, directly and naturally, the way Claude/ChatGPT would in any
   normal conversation. "keywords" and "time_window_days" must be null
   for this type.

   ALSO classify as "chat" (not "search", not "clarify") when the message
   asks a broad, platform-wide "what's trending / what's happening / what
   are the top problems right now" question with NO specific brand,
   product, narrower topic, or industry named, AND no general problem/
   complaint framing either — e.g. "reddit par kya chal raha hai", "abhi
   reddit par top problem kya hai", "what's trending on twitter today"
   with truly nothing else to go on. For these, do NOT generate search
   keywords and do NOT trigger a database search — instead, write the
   "reply" yourself using your own general knowledge of what's commonly
   discussed on that platform, in a natural, confident, professional tone
   (not hedgy, not "as an AI I don't have real-time access" — just answer
   plainly from what you know). At the end of that reply, naturally
   invite them to share their business, product, or website link so
   Flintel can pull real, current, related data for them specifically.
   "keywords" and "time_window_days" stay null for this case, exactly
   like any other "chat" message.

   This "chat" case is DIFFERENT from the GENERAL PAIN-POINT / COMPLAINT
   PATTERN described under "search" above: if the user names ANY angle at
   all — a brand, product, industry, or even just a general problem/
   complaint framing (e.g. "reddit par log kya problems face kar rahe
   hain") — that IS "search", not this "chat" case. The distinguishing
   factor is simple: is there an actual searchable angle, or is the
   question just "what's happening in general" with nothing else?

3. "blocked" — the message is abusive, harassing, hateful, sexually
   explicit, threatening, or otherwise harmful (directed at you, at a
   person, or at any group). Do not search for it and do not answer it
   normally. Instead write a short, calm, firm decline as the reply —
   don't lecture, don't repeat or quote the harmful content back, don't
   moralize at length, just briefly decline and invite them to ask
   something else. "keywords" and "time_window_days" must be null for
   this type.

4. "clarify" — the message is clearly ASKING for a search/social-listening
   pull (it has search-shaped phrasing: "reddit posts", "show me", "find
   me", a time range, etc.) but does NOT actually name any clear
   topic/brand/product/industry/subject to search for, and does NOT even
   name a general problem/complaint angle (see the GENERAL PAIN-POINT /
   COMPLAINT PATTERN under "search" above, which is a "search", not a
   "clarify") — neither in the message itself nor anywhere in the short
   conversation history below. Examples: "aaj ke reddit posts dikhao"
   alone, "last 6 months ke posts do" alone, "show me today's posts" with
   nothing else — there is no brand, product, topic, industry, or problem
   mentioned anywhere for Flintel to actually search. In this case, do
   NOT guess a topic and do NOT fall back to some generic/meaningless
   keyword. Instead, write a short, natural, single clarifying question
   asking what topic/brand/industry/problem they want to look into.
   "keywords" and "time_window_days" must be null for this type (there is
   no job to run yet). IMPORTANT: only use "clarify" when the TOPIC itself
   is missing — a message that clearly names a topic/brand/industry (or
   even just a general problem angle) but simply doesn't mention a time
   range is STILL "search" with time_window_days: null, never "clarify".

   ADDITIONALLY: if the user's message contains a website URL (a link
   starting with http:// or https://) together with ANY stated request,
   ask, or angle at all — including a generic one like "find me
   customers", "find me leads", "promote my site", "market my website",
   or similar — this is NEVER "clarify", even though no specific
   topic/brand/industry is named. A URL is itself enough context to
   search from (Flintel can read the site directly), so treat this as
   "search" with "keywords": null (the downstream website-reading step
   will fill in real keywords from the site's own content). Only use
   "clarify" when there is NEITHER a URL NOR any named topic/brand/
   industry/problem angle anywhere in the message.

   When writing the clarifying reply, sound like a helpful consultant, not
   a form validator: briefly explain WHY you're asking (so the search
   actually finds something relevant to them), and in one natural
   sentence give them two easy ways forward — name a topic/brand/
   industry, OR just paste their website link, since Flintel can read a
   shared website link automatically and pull the right keywords from it.
   Keep it to 1-2 natural sentences, never robotic or repetitive-sounding.

A short, auto-summarized conversation history (may be empty) is given
below for continuity when classifying and when writing a "chat",
"blocked", or "clarify" reply, or when a "search" follow-up implicitly
refers back to a topic already discussed (e.g. if the topic was named
earlier in the conversation, a later "today ke posts do" can still be
"search", using that earlier topic — only ask "clarify" when the topic
truly cannot be determined from the message OR this history). Keep any
reply conversational and plain — don't mention you're an AI or that this
is a "mock", and don't narrate your own reasoning.
Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in EXACTLY one of these four shapes:
{"intent": "search", "reply": null, "keywords": ["<keyword1>", "<keyword2>"], "time_window_days": null}
{"intent": "chat", "reply": "<your natural reply text here>", "keywords": null, "time_window_days": null}
{"intent": "blocked", "reply": "<short, polite decline text>", "keywords": null, "time_window_days": null}
{"intent": "clarify", "reply": "<short, natural clarifying question>", "keywords": null, "time_window_days": null}
"""

CLAUDE_ROUTER_SYSTEM_PROMPT = (
    CLAUDE_ROUTER_SYSTEM_PROMPT
    + "\n" + flintel.ROUTER_UNFILTERED_ADDENDUM
    + "\n" + flintel.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM
)

CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT = """
You are the AI assistant inside Flintel, a social listening platform.
Answer the user's message naturally and directly, the way Claude or
ChatGPT would in any normal conversation. Plain language, no rigid
template, no JSON, no code blocks. Don't mention you're an AI or that
this is a "mock".
"""

# (v6) Safety-net text used only if the router itself flagged a message as
# "blocked" but, for whatever reason, didn't return usable reply text —
# never re-sent to Claude (no extra call, and no reason to hand harmful
# content to another prompt just to get a decline message).
CLAUDE_BLOCKED_FALLBACK_REPLY = (
    "I can't help with that one. Happy to help you look into a brand, "
    "product, or topic instead, or just chat about something else."
)

# (TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE, reworded by ROUTER INTENT
# REFINEMENT) Same safety-net pattern as CLAUDE_BLOCKED_FALLBACK_REPLY
# above: used only if the router flagged "clarify" but didn't return
# usable reply text — never re-sent to Claude. Reworded to match the same
# consultant tone the router prompt now asks for, and to mention the
# website-link shortcut (handled automatically by the existing,
# UNCHANGED WEBSITE-URL KEYWORD EXTRACTION FEATURE on the user's next
# message).
CLAUDE_CLARIFY_FALLBACK_REPLY = (
    "Bilkul — bas yeh batayein aap kis brand, product, ya industry ke "
    "baare mein Reddit aur baaki platforms se data dekhna chahte hain. "
    "Agar apni website ka link bhi share kar dein, main us se directly "
    "aap ke business se related conversations nikaal dunga."
)


def _parse_router_json(raw: str):
    """Best-effort JSON parse of the router's output — strips ```json
    fences if Claude added them anyway, and validates the shape. Returns
    None on anything unexpected so the caller falls back to the safe
    "search" default instead of ever guessing.

    (v6) Accepts "blocked" alongside "search"/"chat" — a reply is
    expected as a string for both, and if it isn't one, the caller's
    fallback text is used instead.

    (KEYWORD-GENERATION SWAP) Now also parses/validates a "keywords"
    field for "search" intent: must be a JSON array of non-empty
    strings; each is trimmed, de-duplicated case-insensitively, and
    capped at CLAUDE_MAX_KEYWORDS. Anything malformed (missing, not a
    list, empty after cleaning) simply results in keywords=None — the
    caller (classify_and_maybe_chat / the /search route) is what applies
    the generate_fuzzy_keywords() fallback in that case, so this function
    itself never needs to know about that fallback.

    (TIME-WINDOW / PAIN-POINT / CLARIFY FEATURE):
      - Accepts "clarify" as a 4th valid intent, treated like "chat"/
        "blocked" for reply-string validation (a string reply is
        expected; anything else falls back to None so the caller's own
        fallback text is used).
      - Also parses/validates "time_window_days" for "search" intent:
        must be a positive int (or a numeric string Claude accidentally
        quoted), clamped between 1 and MAX_TIME_WINDOW_DAYS. Anything
        else (missing, null, zero, negative, non-numeric, or intent !=
        "search") simply results in time_window_days=None, meaning "no
        time filter" — the exact same as if this feature didn't exist.

    (ROUTER INTENT REFINEMENT) This function's logic is completely
    UNCHANGED — the refinement is pure prompt wording inside
    CLAUDE_ROUTER_SYSTEM_PROMPT above; the four valid intents, their
    field shapes, and every validation/clamping rule here are identical
    to before."""
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    if intent not in ("search", "chat", "blocked", "clarify"):
        return None
    reply = data.get("reply")
    if intent in ("chat", "blocked", "clarify") and not isinstance(reply, str):
        reply = None

    keywords = None
    time_window_days = None
    unfiltered = False
    if intent == "search":
        raw_keywords = data.get("keywords")
        if isinstance(raw_keywords, list):
            cleaned_keywords = []
            seen = set()
            for kw in raw_keywords:
                if not isinstance(kw, str):
                    continue
                kw_clean = kw.strip()
                if not kw_clean:
                    continue
                key = kw_clean.lower()
                if key in seen:
                    continue
                seen.add(key)
                cleaned_keywords.append(kw_clean)
                if len(cleaned_keywords) >= CLAUDE_MAX_KEYWORDS:
                    break
            keywords = cleaned_keywords or None

        raw_window = data.get("time_window_days")
        parsed_window = None
        if isinstance(raw_window, bool):
            parsed_window = None  # guard: bool is a subclass of int in Python
        elif isinstance(raw_window, int):
            parsed_window = raw_window
        elif isinstance(raw_window, str) and raw_window.strip().isdigit():
            parsed_window = int(raw_window.strip())
        if isinstance(parsed_window, int) and parsed_window > 0:
            time_window_days = min(parsed_window, MAX_TIME_WINDOW_DAYS)

        unfiltered = bool(data.get("unfiltered") is True)

    return {"intent": intent, "reply": reply, "keywords": keywords, "time_window_days": time_window_days,
            "unfiltered": unfiltered}


def classify_and_maybe_chat(query: str, chat_summary: str) -> dict:
    """(v5, extended in v6 with abuse-blocking, again with keyword
    generation, again with time-window parsing + a "clarify" intent, and
    now again with the ROUTER INTENT REFINEMENT prompt wording described
    in the module docstring) Single cheap Claude call that classifies the
    user's message as "search", "chat", "blocked", or "clarify" and:
      - for "chat"/"blocked"/"clarify", writes the reply in the same
        call, and
      - for "search", ALSO returns the keyword list and time window to
        use for matching.
    Falls back to {"intent": "search", "reply": None, "keywords": None,
    "time_window_days": None} on ANY failure (API error, timeout, bad
    JSON) so the pre-existing search pipeline is always the safe default
    — only the chat-reply / abuse-blocking / smart-keyword / time-window
    / clarify shortcuts can ever be skipped by a routing hiccup, never a
    genuine search request (the /search route falls back to
    generate_fuzzy_keywords() whenever keywords come back None for a
    "search" intent, and treats a missing time_window_days as "no time
    filter", exactly as before this feature).

    (ROUTER INTENT REFINEMENT) This function's logic is completely
    UNCHANGED — only the text of CLAUDE_ROUTER_SYSTEM_PROMPT changed."""
    user_message = (
        f"Conversation so far (auto-summarized, may be empty):\n"
        f"{chat_summary or '(no earlier messages in this chat)'}\n\n"
        f"User's new message: {query}"
    )
    try:
        raw = _call_claude(CLAUDE_ROUTER_SYSTEM_PROMPT, user_message, max_tokens=CLAUDE_ROUTER_MAX_TOKENS)
    except Exception as exc:
        log.warning(f"Router Claude call failed (defaulting to 'search'): {exc}")
        return {"intent": "search", "reply": None, "keywords": None, "time_window_days": None, "unfiltered": None}

    parsed = _parse_router_json(raw)
    if not parsed:
        log.warning(f"Router returned unparseable output (defaulting to 'search'): {raw[:200]!r}")
        return {"intent": "search", "reply": None, "keywords": None, "time_window_days": None, "unfiltered": None}
    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# CLARIFY-SELF-RESOLVE FEATURE — see the module docstring note above for
# the full rationale. Only ever invoked for a message the main router
# above already classified as intent="clarify". Uses ONE extra cheap
# Claude call, Claude's own general knowledge only (no web-search tool,
# no new information beyond the message + the existing rolling chat
# summary), to make a single honest attempt at resolving the topic before
# the existing clarify-question flow is allowed to fire.
#
# (ROUTER INTENT REFINEMENT) UNTOUCHED by this file's changes: with
# "clarify" now firing less often (per point 1 and 2 of the ROUTER INTENT
# REFINEMENT note above), this feature simply gets invoked less often —
# its own logic, prompt, and behavior are completely unchanged.
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_TOPIC_RESOLVER_SYSTEM_PROMPT = """
You are a fallback topic-resolution step inside Flintel, a social-listening
platform. You are only ever called for a message that Flintel's own router
already decided is CLARIFY — a search-shaped message ("reddit posts",
"show me", "find me", a time range, etc.) that did NOT name a clear
topic/brand/product/industry, either in the message itself or in the short
conversation history.

Your job: using ONLY your own general knowledge (you have no web-search
tool here, and you are not being given any new information beyond the
message and the conversation summary below), make ONE honest attempt to
figure out what topic the user most likely means. This will usually fail —
that is fine and expected; only succeed when you are genuinely confident,
never guess just to produce an answer.

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{"resolved": true, "keywords": ["<keyword1>", "<keyword2>"], "time_window_days": null}
or, when you genuinely cannot infer a specific topic:
{"resolved": false, "keywords": null, "time_window_days": null}

Rules when "resolved": true:
- "keywords": up to 10 short, natural search terms that could plausibly
  appear inside a real Reddit/X/LinkedIn/Facebook post about the topic you
  inferred — same rules as normal keyword generation elsewhere in this
  product: no meta words like "reddit", "posts", "show me", "today", etc.
- "time_window_days": convert any time range already implied by the user's
  own message the same way it's always converted elsewhere ("today"/"aaj"
  -> 1, "this week"/"last 7 days" -> 7, "last month" -> 30, "last 6
  months" -> 180, "last year" -> 365, etc.) — null if no time range was
  mentioned.
- Only mark "resolved": true if you are genuinely confident about the
  topic — e.g. the conversation history clearly named a brand/topic
  earlier and this message is obviously a natural follow-up about it. Do
  NOT invent a topic out of thin air just because the message is
  search-shaped.
"""


def resolve_unclear_topic(query: str, chat_summary: str):
    """(CLARIFY-SELF-RESOLVE FEATURE) Best-effort, single extra cheap
    Claude call, used ONLY for a message the main router already
    classified as "clarify" (a search-shaped message with no clear
    topic). Uses Claude's OWN general knowledge (no web-search tool, no
    new information beyond the message + the existing rolling chat
    summary) to make one honest attempt at guessing the real topic —
    e.g. a follow-up that implicitly refers back to a brand/topic already
    named earlier in the conversation.

    Returns a dict {"keywords": [...], "time_window_days": int|None} if
    Claude was genuinely confident enough to resolve a topic, or None if
    it wasn't (or if this call failed outright) — callers must treat
    None exactly the same as before this feature existed: fall through
    to the normal "clarify" question-asking behavior. This can only ever
    ADD a shortcut on top of the existing clarify flow; it can never
    block or replace the safety net of just asking the user."""
    user_message = (
        f"Conversation so far (auto-summarized, may be empty):\n"
        f"{chat_summary or '(no earlier messages in this chat)'}\n\n"
        f"User's message (already classified as unclear-topic 'clarify' by "
        f"the main router): {query}"
    )
    try:
        raw = _call_claude(
            CLAUDE_TOPIC_RESOLVER_SYSTEM_PROMPT,
            user_message,
            max_tokens=CLAUDE_TOPIC_RESOLVER_MAX_TOKENS,
        )
    except Exception as exc:
        log.warning(f"Topic-resolver Claude call failed for query={query!r}: {exc}")
        return None

    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        # (RESILIENT JSON EXTRACTION) Claude sometimes appends trailing
        # prose after a valid JSON object (e.g. "```json\n{...}\n```\n\nThe
        # user has shared..."). Before giving up, try extracting just the
        # first {...} object via a non-greedy regex and parsing that alone
        # — this can only ever recover an otherwise-wasted call, never
        # change behavior for already-valid JSON (which was already
        # handled above).
        match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except (ValueError, TypeError):
                log.warning(f"Topic-resolver returned unparseable output for query={query!r}: {raw[:200]!r}")
                return None
        else:
            log.warning(f"Topic-resolver returned unparseable output for query={query!r}: {raw[:200]!r}")
            return None
    if not isinstance(data, dict) or not data.get("resolved"):
        return None

    raw_keywords = data.get("keywords")
    keywords = None
    if isinstance(raw_keywords, list):
        cleaned_keywords = []
        seen = set()
        for kw in raw_keywords:
            if not isinstance(kw, str):
                continue
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            key = kw_clean.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned_keywords.append(kw_clean)
            if len(cleaned_keywords) >= CLAUDE_MAX_KEYWORDS:
                break
        keywords = cleaned_keywords or None

    if not keywords:
        return None

    raw_window = data.get("time_window_days")
    time_window_days = None
    parsed_window = None
    if isinstance(raw_window, bool):
        parsed_window = None
    elif isinstance(raw_window, int):
        parsed_window = raw_window
    elif isinstance(raw_window, str) and raw_window.strip().isdigit():
        parsed_window = int(raw_window.strip())
    if isinstance(parsed_window, int) and parsed_window > 0:
        time_window_days = min(parsed_window, MAX_TIME_WINDOW_DAYS)

    return {"keywords": keywords, "time_window_days": time_window_days}


# ─────────────────────────────────────────────────────────────────────────────
# WEBSITE-URL KEYWORD EXTRACTION FEATURE — see the module docstring note
# above for the full rationale. Only ever invoked for a search-type
# message whose raw text contains an http(s) URL. Fetches that URL, turns
# it into plain text, and hands it (together with the user's own request
# text) to a single extra cheap Claude call to produce the keyword list.
#
# (ROUTER INTENT REFINEMENT) UNTOUCHED by this file's changes — this is
# the exact feature the new "clarify" reply wording (see
# CLAUDE_CLARIFY_FALLBACK_REPLY and the router's "clarify" instructions
# above) now proactively points the user toward. Nothing in this section
# was modified.
# ─────────────────────────────────────────────────────────────────────────────

_URL_REGEX = re.compile(r'https?://[^\s<>"\')\]]+', re.IGNORECASE)


def _extract_first_url(text: str):
    """(WEBSITE-URL KEYWORD EXTRACTION FEATURE) Best-effort extraction of
    the first http(s) URL appearing anywhere in the user's raw message
    text. Purely a plain-Python regex check — never calls Claude, never
    modifies the query. Returns None if no URL is present, which is the
    common case and leaves every other code path completely untouched."""
    if not text:
        return None
    match = _URL_REGEX.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,;:!?")
    return url or None


_HTML_TAG_RE            = re.compile(r"<[^>]+>")
_HTML_SCRIPT_STYLE_RE   = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_WHITESPACE_RE     = re.compile(r"\s+")


def fetch_website_text(url: str) -> str:
    """(WEBSITE-URL KEYWORD EXTRACTION FEATURE) Fetches a user-provided
    website URL and reduces it to plain text, good enough to hand to
    Claude as grounding for keyword extraction — NOT a full HTML parser,
    just enough tag-stripping to turn a page into readable text without
    pulling in a new dependency. Raises on any failure (bad URL, timeout,
    non-2xx, etc.) — the caller decides how to degrade gracefully, same
    convention as _call_claude().

    Truncates to WEBSITE_FETCH_MAX_CHARS so a large page can never blow
    up the size/cost of the Claude call that follows."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FlintelBot/1.0)"}
    with httpx.Client(timeout=WEBSITE_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as http_client:
        response = http_client.get(url, headers=headers)
        response.raise_for_status()
        html = response.text

    text = _HTML_SCRIPT_STYLE_RE.sub(" ", html)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _HTML_WHITESPACE_RE.sub(" ", text).strip()

    return text[:WEBSITE_FETCH_MAX_CHARS]


CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT = """
You are the website-to-keywords brain inside Flintel, a social-listening
platform. The user has shared a link to their OWN website together with
what they want Flintel to search for. Your job, in ONE pass: read the
website's plain text content plus the user's own request text, and
produce BOTH (1) the keyword list Flintel's existing matching code will
use to find relevant Reddit/X/LinkedIn/Facebook posts, AND (2) a clean,
sectioned breakdown of what the business/site actually offers.

PART 1 — KEYWORDS:
- Read the website to understand what the business actually offers
  (its products, services, and industry) and combine that understanding
  with whatever the user's own request text asks for (e.g. a specific
  angle like "pricing complaints", or a pain-point/prospect-style ask
  like "find people whose website is slow").
- Return up to 20 short, natural keywords/phrases that could plausibly
  appear verbatim, or as a close natural substring, inside a real post's
  title or text — the same standard used everywhere else in this
  product. Never include meta wording like "reddit", "posts", "show me",
  "website", "today", etc.
- If the user's own request text names a specific angle or problem, bias
  the keywords toward THAT (same pain-point/prospect pattern used
  elsewhere in this product: keywords about the PROBLEM/SYMPTOM being
  described, not just the business's own name/services), rather than
  generic keywords about the site as a whole.
- If the website content is too thin, broken, or irrelevant to produce
  any confident keywords, return an empty list rather than inventing
  generic filler.

PART 2 — STRUCTURED SUMMARY:
- Produce a CLEAN, SECTIONED breakdown of what the business/site/
  individual actually offers — not a flat paragraph. Reason freshly from
  the actual content given — never assume or default to any particular
  industry or category, and never force the same fixed set of section
  titles onto every website.
- Produce 2 to 4 sections total, choosing whichever section titles
  genuinely fit THIS site's content (e.g. "What they offer", "Business
  signals", "Notable things" are loose inspiration, not required).
- Each section should have 2 to 5 short bullets — plain language, no
  fluff, no marketing tone. Stay honest and slightly skeptical where the
  content warrants it.
- If the content is too thin to say anything confident across multiple
  sections, keep the overview honest about that and produce however few
  genuinely-supportable sections make sense (including zero) rather than
  padding.

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{
  "keywords": ["<keyword1>", "<keyword2>"],
  "structured_summary": {
    "overview": "<1-2 sentence plain-language opening line>",
    "sections": [{"title": "<short section heading>", "bullets": ["<bullet 1>", "<bullet 2>"]}]
  }
}
"""


def extract_keywords_from_website(query: str, url: str, website_text: str):
    """(WEBSITE-URL KEYWORD EXTRACTION FEATURE) Single cheap Claude call:
    reads the already-fetched plain-text website content PLUS the user's
    own request text (so a specific angle/pain-point the user typed
    alongside the link is honored, not just the site's generic content)
    and returns up to MAX_WEBSITE_KEYWORDS keywords, PLUS a structured
    summary breakdown of the site, in ONE combined pass — both derived
    from the SAME Claude call, using CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT.

    Returns `{"keywords": list|None, "structured_summary": dict|None}`
    if Claude returned anything usable (either piece present is enough),
    or `None` if Claude failed outright, returned unparseable output, or
    returned neither usable keywords nor a usable structured summary —
    NEVER a bare list anymore. Callers (see INTEGRATION POINT 2 in
    POST /search) already expect this new shape: they read
    `result.get("keywords")` and `result.get("structured_summary")`
    separately, and must treat a `None` return exactly like any other "no
    usable output from this source" case elsewhere in this file: fall
    back down the existing safety-net chain (the router's own
    routed_keywords, then finally generate_fuzzy_keywords()), never let a
    search end up with zero keywords because of this feature."""
    if not website_text:
        return None

    user_message = (
        f"User's request text: {query}\n\n"
        f"Website URL: {url}\n\n"
        f"Website content (plain text, truncated):\n{website_text}"
    )
    try:
        raw = _call_claude(
            CLAUDE_WEBSITE_KEYWORD_SYSTEM_PROMPT,
            user_message,
            max_tokens=CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS,
        )
    except Exception as exc:
        log.warning(f"Website-keyword Claude call failed for url={url!r}: {exc}")
        return None

    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        log.warning(f"Website-keyword call returned unparseable output for url={url!r}: {raw[:200]!r}")
        return None
    if not isinstance(data, dict):
        return None

    raw_keywords = data.get("keywords")
    cleaned_keywords = None
    if isinstance(raw_keywords, list):
        cleaned_list = []
        seen = set()
        for kw in raw_keywords:
            if not isinstance(kw, str):
                continue
            kw_clean = kw.strip()
            if not kw_clean:
                continue
            key = kw_clean.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned_list.append(kw_clean)
            if len(cleaned_list) >= MAX_WEBSITE_KEYWORDS:
                break
        cleaned_keywords = cleaned_list or None

    raw_structured = data.get("structured_summary")
    structured_summary = None
    if isinstance(raw_structured, dict):
        overview = raw_structured.get("overview")
        overview = overview.strip() if isinstance(overview, str) else ""
        raw_sections = raw_structured.get("sections")
        cleaned_sections = []
        if isinstance(raw_sections, list):
            for section in raw_sections:
                if not isinstance(section, dict):
                    continue
                title = section.get("title")
                bullets = section.get("bullets")
                if not isinstance(title, str) or not title.strip():
                    continue
                if not isinstance(bullets, list):
                    continue
                cleaned_bullets = [b.strip() for b in bullets if isinstance(b, str) and b.strip()]
                if not cleaned_bullets:
                    continue
                cleaned_sections.append({"title": title.strip(), "bullets": cleaned_bullets})
        if overview or cleaned_sections:
            structured_summary = {"overview": overview, "sections": cleaned_sections}

    if not cleaned_keywords and not structured_summary:
        return None
    return {"keywords": cleaned_keywords, "structured_summary": structured_summary}


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _extract_summary_text_from_claude_answer(answer: str) -> str:
    """(BUG FIX — CHAT-SUMMARY CONTEXT LOSS) append_to_chat_summary()
    used to blindly character-trim the raw claude_answer string to
    CHAT_SUMMARY_TURN_CHAR_LIMIT characters. Since the JSON-ANALYSIS-
    PROMPT-SWAP, claude_answer is a JSON document, so trimming it as raw
    text usually cuts the string off mid-object — discarding exactly the
    fields (a suggested next term, a followup, a likely_reason) a later
    confirmation-style reply ("yes", "sure", "search it", "haan karo")
    needs to resolve against. This is a purely additive, best-effort
    pre-processing step: it parses the answer as JSON and pulls out the
    human-readable parts most useful for conversational continuity,
    BEFORE the caller applies its own char-limit trim.

    Generic by design — reads whatever fields are present on whatever
    format the answer happens to be, never hardcodes a topic, keyword,
    or industry. Falls back to the original raw string unchanged if the
    answer isn't valid JSON (e.g. a plain "chat"/"blocked"/"clarify"
    reply, which is already plain text and doesn't need this treatment)
    or has none of the known fields — so this can only ever IMPROVE the
    summary's usefulness, never make it worse than doing nothing."""
    if not answer:
        return answer or ""

    cleaned = answer.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()

    data = None
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            data = parsed
    except (ValueError, TypeError):
        data = None

    if not data:
        # Not JSON (a plain chat/blocked/clarify reply, or unparseable
        # output) — nothing to extract, use the text exactly as given.
        return answer

    parts = []
    for key in ("summary", "message", "likely_reason", "interpretation", "trend"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())

    followups = data.get("followups")
    if isinstance(followups, list):
        parts.extend(f.strip() for f in followups if isinstance(f, str) and f.strip())

    suggested_actions = data.get("suggested_actions")
    if isinstance(suggested_actions, list):
        for action in suggested_actions:
            if not isinstance(action, dict):
                continue
            for field_name in ("suggestion", "label"):
                val = action.get(field_name)
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())

    clarifying_question = data.get("clarifying_question")
    if isinstance(clarifying_question, str) and clarifying_question.strip():
        parts.append(clarifying_question.strip())

    if not parts:
        return answer

    return " | ".join(parts)


def append_to_chat_summary(chat_id: str, owner_key: str, query: str, answer: str):
    """(v5) Keeps a short, PLAIN-PYTHON (no extra Claude call) running
    summary on the chat doc itself — one condensed line per turn. This is
    what classify_and_maybe_chat() reads for continuity, so passing
    conversation context to Claude stays cheap and small no matter how
    long a chat gets — Claude is never sent the full raw message history,
    only this rolling, auto-generated summary. Keeps only the last
    CHAT_SUMMARY_MAX_TURNS lines; older lines roll off automatically.
    Best-effort: never allowed to raise past its caller.

    (BUG FIX — CHAT-SUMMARY CONTEXT LOSS) `answer` is now run through
    _extract_summary_text_from_claude_answer() before trimming, so a
    JSON-format answer contributes its actual human-readable fields to
    the summary instead of getting sliced mid-object by the raw
    character trim — see that function's own docstring for why this
    matters for confirmation-style follow-up replies."""
    if not chat_id or not owner_key:
        return
    digest = _extract_summary_text_from_claude_answer(answer)
    line = f"User: {_trim(query, CHAT_SUMMARY_TURN_CHAR_LIMIT)} | Assistant: {_trim(digest, CHAT_SUMMARY_TURN_CHAR_LIMIT)}"

    chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key}, {"summary": 1})
    existing_summary = (chat or {}).get("summary") or ""
    existing_lines = [l for l in existing_summary.split("\n") if l.strip()]
    existing_lines.append(line)
    trimmed_lines = existing_lines[-CHAT_SUMMARY_MAX_TURNS:]

    chats_collection.update_one(
        {"chat_id": chat_id, "owner_key": owner_key},
        {"$set": {"summary": "\n".join(trimmed_lines)}},
    )


def _elapsed_seconds(dt) -> float:
    """(v6) Best-effort elapsed-seconds calculation from a stored
    `requested_at` timestamp. Mongo may hand this back as a naive
    datetime (it was still stored as UTC under the hood) or as an
    already-aware one — this normalizes either case to UTC before
    diffing against "now", so the RESPONSE_TIMEOUT comparison below never
    raises on a naive/aware mismatch. Returns 0.0 (i.e. "just requested,
    definitely not timed out") for anything that isn't a real datetime,
    so a missing/corrupt timestamp can never accidentally trigger the
    timeout fallback early."""
    if not isinstance(dt, datetime):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


# (BUGFIX PACK #1) Formats CLAUDE_ANALYSIS_SYSTEM_PROMPT can return that
# mean "no real grounded data to show" — when claude_answer parses to one
# of these, the matching message's post-card `results` are treated as
# empty for that render, instead of showing loosely-matched posts Claude
# itself already rejected as irrelevant/unavailable/disallowed.
_NO_DATA_CLAUDE_FORMATS = {"no_results", "not_available", "disallowed"}


def _extract_claude_format(answer_text: str):
    """(BUGFIX PACK #1) Best-effort parse of a claude_answer string to
    pull out its "format" field, mirroring the same fence-stripping
    tolerance already used by _parse_router_json() elsewhere in this
    file. Returns None (never raises) if the text is missing, isn't
    valid JSON, or doesn't have a string "format" field — callers treat
    None as "unknown/can't tell", which always means "show results as
    before", never "hide them". This only ever narrows what's shown when
    it can positively confirm Claude's own JSON said there was nothing
    relevant; it can never hide results based on a guess."""
    if not answer_text:
        return None
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fmt = data.get("format")
    return fmt if isinstance(fmt, str) else None


# (BUG FIX — DON'T HIDE REAL MATCHED POSTS / KEEP A POSITIVE CLOSING NOTE)
# Rotated the same cheap-hash way as flintel.py's own _pick_invite_line()
# and website_intelligence.py's own _pick_variant(), so the same
# sentence doesn't repeat mechanically every time.
_CLOSEST_MATCHES_NOTE_VARIANTS = [
    "These are the closest posts I found — happy to run a more targeted search if you give me a specific angle.",
    "I've shared the nearest matches below in case they're useful — let me know if you'd like this narrowed down further.",
    "Sharing the closest posts I could find below — point me at a more specific angle and I can dig further.",
    "Yeh sabse qareeb posts hain jo mujhe mile — agar aap koi khaas angle bata dein to main zyada targeted search kar sakta hoon.",
]


def _pick_closest_matches_note(seed: str = "") -> str:
    """Same cheap, deterministic-hash rotation already used elsewhere in
    this product (flintel.py's _pick_invite_line, website_intelligence.py's
    _pick_variant) — avoids the exact same sentence every time without
    needing a random source or stored state."""
    if not seed:
        return _CLOSEST_MATCHES_NOTE_VARIANTS[0]
    idx = sum(ord(c) for c in seed) % len(_CLOSEST_MATCHES_NOTE_VARIANTS)
    return _CLOSEST_MATCHES_NOTE_VARIANTS[idx]


def _append_closest_matches_note(answer_text: str, seed: str = "") -> str:
    """(BUG FIX) Purely additive, non-destructive post-processing step,
    same parse/patch/re-serialize pattern already used by
    _patch_post_urls_into_answer() and _inject_website_context_into_answer().
    Appends one short, rotating, positive closing line onto the
    format's own "message" field — never changes Claude's own honest
    assessment text otherwise. If answer_text isn't valid JSON, or has
    no "message" field, the original text is returned unchanged.

    (BUG FIX — DON'T SHOW STALE "TRY THESE NEXT STEPS" ALONGSIDE REAL
    POSTS) Also strips "suggested_actions" and "clarifying_question"
    from the JSON in this same pass — see the inline comment below for
    why, and _finalize_answer_and_results()'s docstring for confirmation
    that this function is ONLY ever invoked when real matched posts are
    about to be shown to the user."""
    if not answer_text:
        return answer_text
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict):
        return answer_text

    val = data.get("message")
    if not isinstance(val, str) or not val.strip():
        return answer_text

    # (BUG FIX — DON'T SHOW STALE "TRY THESE NEXT STEPS" ALONGSIDE REAL
    # POSTS) Once real matched posts are being shown below this text
    # (which is the ONLY case this function is ever called for — see
    # _finalize_answer_and_results()), the format's own "suggested_actions"
    # (broaden time/platform/term) and "clarifying_question" fields become
    # redundant/contradictory — the user already sees real posts, so
    # "try broadening your search" reads as odd next to them. Remove
    # both fields here, in this branch ONLY. A genuine no_results answer
    # (matched empty) never reaches this function — see
    # _finalize_answer_and_results() — so its suggested_actions /
    # clarifying_question are completely untouched by this change.
    data.pop("suggested_actions", None)
    data.pop("clarifying_question", None)

    data["message"] = val.strip() + " " + _pick_closest_matches_note(seed)
    return json.dumps(data, ensure_ascii=False)


def _finalize_answer_and_results(answer_text: str, matched: list, seed: str = ""):
    """(BUG FIX) Shared decision point for both the non-streaming path
    (_complete_message_answer_and_results) and the streaming route
    (routes.py): given Claude's own answer text and the posts actually
    matched by get_matched_signals()/get_unfiltered_matched_signals(),
    decides what to persist as this message's final `results` and
    `claude_answer`.

    Real matched posts are NEVER hidden just because Claude's own
    analysis chose a "no_data" format (no_results/not_available/
    disallowed) for the written response — that's a statement about how
    confidently Claude could ground a full narrative answer, not a
    statement about whether ANY real data exists. When that happens,
    this keeps Claude's own honest text intact but appends one short,
    positive closing note (see _append_closest_matches_note()) so the
    user still sees the real posts below and isn't left at a dead end.
    Genuinely empty results (no data AND nothing matched) are still
    saved as an empty list, exactly as before this fix.

    Returns (final_answer_text, results_to_save)."""
    claude_format = _extract_claude_format(answer_text)
    if claude_format in _NO_DATA_CLAUDE_FORMATS and matched:
        return _append_closest_matches_note(answer_text, seed=seed), matched
    if claude_format in _NO_DATA_CLAUDE_FORMATS:
        return answer_text, []
    return answer_text, matched


# ─────────────────────────────────────────────────────────────────────────────
# POST_URL PATCH FIX — additive post-processing only, see module docstring.
# ─────────────────────────────────────────────────────────────────────────────

def _best_matching_post(title: str, matched_signals: list):
    """(POST_URL FIX) Best-effort match of a title string (as it appears
    inside Claude's JSON answer) back to one of the real matched_signals
    entries, so its real post_url can be looked up. Tries an exact
    case-insensitive match first, then falls back to a loose
    containment check (either string contains the other) since Claude
    may lightly reword/trim a title when it copies it into its answer.
    Returns the matching signal dict, or None if nothing looks like a
    reasonable match — callers must treat None as "leave the link field
    alone", never guess a URL."""
    if not title:
        return None
    title_norm = title.strip().lower()
    if not title_norm:
        return None

    for sig in matched_signals or []:
        sig_title = (sig.get("title") or "").strip().lower()
        if sig_title and sig_title == title_norm:
            return sig

    for sig in matched_signals or []:
        sig_title = (sig.get("title") or "").strip().lower()
        if not sig_title:
            continue
        if sig_title in title_norm or title_norm in sig_title:
            return sig

    return None


def _patch_post_urls_into_answer(answer_text: str, matched_signals: list) -> str:
    """(POST_URL FIX) CLAUDE_ANALYSIS_SYSTEM_PROMPT asks for a "link"
    field with "the real post URL if available" on every post it lists —
    but Claude is deliberately NEVER shown post_url (see
    build_claude_post_context(), unchanged), so it could never actually
    supply a real one. This best-effort, PURELY ADDITIVE post-processing
    step fixes that: it parses the already-generated answer_text as
    JSON, walks the known formats that carry post lists ("source_list"
    and "comparison"), and for every post object it finds, matches its
    "title" back to one of THIS message's already-matched signals (via
    _best_matching_post()) and sets "link" to that signal's REAL
    post_url — the exact same URL already used by the post cards,
    computed by get_matched_signals(), never anything Claude itself
    supplied or guessed.

    Grounding is preserved: this never lets Claude decide a URL. It only
    ever substitutes in a URL Flintel already knows to be real, and only
    when the title can be confidently matched back to one specific
    signal; if no confident match is found, that post simply keeps
    whatever "link" value (usually absent) Claude left it with — it is
    never given a made-up URL by this function either.

    Best-effort/non-destructive: if answer_text isn't valid JSON, isn't
    one of the two known list-based formats, or matched_signals is
    empty, the original answer_text is returned completely UNCHANGED —
    this can only ever ADD a real link where one is confidently
    resolvable, it can never remove or alter anything else in the
    answer.

    (SIMULATED-STREAM FIX) Now called by GET /chat/{chat_id}/stream on
    the COMPLETE answer text BEFORE any of it is streamed out to the
    browser (previously it only ran after streaming had already
    finished, so the live-streamed text a user first saw never had real
    links — only a later cached re-render did). This function itself is
    completely unchanged; only WHEN/WHERE it's called shifted."""
    if not answer_text or not matched_signals:
        return answer_text

    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict):
        return answer_text

    fmt = data.get("format")
    changed = False

    def _patch_platform_list(platforms):
        nonlocal changed
        if not isinstance(platforms, list):
            return
        for platform_entry in platforms:
            if not isinstance(platform_entry, dict):
                continue
            posts = platform_entry.get("posts")
            if not isinstance(posts, list):
                continue
            for post in posts:
                if not isinstance(post, dict):
                    continue
                match = _best_matching_post(post.get("title"), matched_signals)
                if match and match.get("post_url"):
                    post["link"] = match["post_url"]
                    changed = True

    if fmt == "source_list":
        _patch_platform_list(data.get("platforms"))
    elif fmt == "comparison":
        subjects = data.get("subjects")
        if isinstance(subjects, list):
            for subject in subjects:
                if isinstance(subject, dict):
                    _patch_platform_list(subject.get("platforms"))
    else:
        return answer_text

    if not changed:
        return answer_text

    return json.dumps(data, ensure_ascii=False)


def _inject_website_context_into_answer(answer_text: str, website_context: dict) -> str:
    """Purely additive, Python-side post-processing — parses the
    already-generated Claude JSON answer and injects `website_context`
    as a new top-level field at the START of the JSON object (before
    "format"), without depending on any model-side instruction to add
    it. Mirrors the same parse/patch/re-serialize pattern already used
    by _patch_post_urls_into_answer(): best-effort and non-destructive
    — if answer_text isn't valid JSON, or website_context is falsy,
    the original answer_text is returned completely unchanged."""
    if not answer_text or not website_context:
        return answer_text
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return answer_text
    if not isinstance(data, dict):
        return answer_text
    new_data = {"website_context": website_context, **data}
    return json.dumps(new_data, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# USER ACCOUNTS — v2 (Google OAuth + email/password, `flintel_users`)
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_user(user: dict) -> dict:
    """Strips the password hash before a user doc ever reaches a template."""
    if not user:
        return None
    safe = dict(user)
    safe.pop("password_hash", None)
    safe["id"] = str(safe.pop("_id"))
    return safe


def get_current_user(request: Request):
    """Reads the logged-in user (if any) from the session cookie."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    try:
        user = users_collection.find_one({"_id": ObjectId(user_id)})
    except InvalidId:
        return None
    return _serialize_user(user)


def _log_user_in(request: Request, user_doc: dict):
    request.session["user_id"] = str(user_doc["_id"])
    request.session["email"] = user_doc.get("email")


def create_email_user(email: str, password_hash: str):
    now = datetime.now(timezone.utc)
    result = users_collection.insert_one({
        "email":          email,
        "name":           None,
        "google_id":      None,
        "password_hash":  password_hash,
        "provider":       "email",
        "created_at":     now,
        "last_login_at":  now,
    })
    return users_collection.find_one({"_id": result.inserted_id})


def upsert_google_user(google_id: str, email: str, name: str):
    """Logs in an existing Google user, links Google to a matching
    email/password account, or creates a brand-new account."""
    now = datetime.now(timezone.utc)

    user = users_collection.find_one({"google_id": google_id})
    if user:
        users_collection.update_one({"_id": user["_id"]}, {"$set": {"last_login_at": now}})
        return users_collection.find_one({"_id": user["_id"]})

    # Same email already registered manually -> link the Google identity
    # to that existing account instead of creating a duplicate.
    existing = users_collection.find_one({"email": email})
    if existing:
        users_collection.update_one(
            {"_id": existing["_id"]},
            {"$set": {"google_id": google_id, "name": name, "provider": "both", "last_login_at": now}},
        )
        return users_collection.find_one({"_id": existing["_id"]})

    result = users_collection.insert_one({
        "email":          email,
        "name":           name,
        "google_id":      google_id,
        "password_hash":  None,
        "provider":       "google",
        "created_at":     now,
        "last_login_at":  now,
    })
    return users_collection.find_one({"_id": result.inserted_id})


# ─────────────────────────────────────────────────────────────────────────────
# CHAT / SESSION MEMORY (`flintel_users_chat`)
#
# Mirrors how Claude/ChatGPT handle conversations:
#   - Every user (signed in or anonymous) gets a stable "owner_key".
#       * signed in  -> owner_key = their email
#       * anonymous  -> owner_key = a random UUID kept in the session cookie
#   - Searches happen inside a "chat" doc, auto-titled from the first
#     query, the same way a new Claude/ChatGPT conversation gets named
#     after your first message.
#   - Each search message holds:
#       * `results`      -> post cards data (title/post_text/post_url/
#                            platform), UNCHANGED from v3, shown as-is
#                            (BUGFIX PACK #1: except now suppressed for
#                            that render/save when claude_answer's own
#                            format says nothing relevant was found — see
#                            _fill_in_message_outputs() below).
#       * `claude_answer` -> (v4) Claude's answer text to the user's own
#                            prompt, grounded in those same matched posts
#                            (title + text only). Only this OUTPUT is
#                            stored — the posts fed in as input are never
#                            duplicated here, since they already live in
#                            flintel_signals. (v6: if no posts are ever
#                            matched within RESPONSE_TIMEOUT seconds, this
#                            instead ends up holding a plain "nothing
#                            found on this yet" answer.)
#       * `time_window_days` -> (NEW) the optional time window the router
#                            parsed out of the user's own message (int, or
#                            None if the user gave no time range). Stored
#                            on the message exactly like `keywords` is, so
#                            it stays consistent across page reloads and
#                            future re-matching of the same message.
#   - (v5) A message may instead be `"message_type": "chat"` — a plain
#     conversational turn the v5 router decided didn't need any data
#     pulled at all (including, as of v6, a polite decline for a
#     "blocked" message, and now also a clarifying question for a
#     "clarify" message). These have no topic_key/keywords/results, only
#     `query` + `claude_answer`, and never touch flintel_search_jobs or
#     flintel_signals in any way. Messages with no `message_type` (every
#     message from before this update, and every new search-type
#     message) are treated as ordinary search messages, exactly as before.
#   - (v5) `summary` — a short, plain-Python rolling digest of the chat
#     (see append_to_chat_summary above), used purely to give the
#     router/chat-reply calls cheap continuity without ever sending
#     Claude the full raw message history.
#   - When an anonymous user signs up / logs in, their guest chats are
#     re-keyed onto their email so nothing is lost.
#   - Because chats are always looked up by owner_key (the email, once
#     signed in), logging out and logging back in with the same email
#     brings the exact same chat history back — nothing is deleted on
#     logout.
#   - (v4.3) A single chat can be deleted by its owner — see
#     delete_chat_session() below — without touching any other chat, and
#     without letting anyone but that same owner_key delete it.
#
# COMPLETELY UNCHANGED BY THE KEYWORD-GENERATION SWAP — this entire
# section is untouched aside from the one new `time_window_days`
# parameter on add_search_to_chat() described above; `keywords` is stored
# on the message exactly as before, regardless of which source produced
# it.
# ─────────────────────────────────────────────────────────────────────────────

def get_anon_id(request: Request) -> str:
    """Stable random ID for a guest (not signed in) visitor, stored in
    their session cookie so their chats persist across requests."""
    anon_id = request.session.get("anon_id")
    if not anon_id:
        anon_id = uuid.uuid4().hex
        request.session["anon_id"] = anon_id
    return anon_id


def get_owner(request: Request):
    """Returns (owner_key, owner_type) for whoever is making the request.
    Signed-in users are keyed by email; guests are keyed by their anon
    UUID."""
    user = get_current_user(request)
    if user and user.get("email"):
        return user["email"], "email"
    return get_anon_id(request), "anon"


def generate_chat_title(query: str) -> str:
    """Auto-names a chat from its first query, the same way Claude/ChatGPT
    title a new conversation from your first message."""
    title = (query or "").strip()
    if len(title) > 60:
        title = title[:57].rstrip() + "..."
    return title or "New chat"


def create_chat_session(owner_key: str, owner_type: str, title: str = None) -> str:
    now = datetime.now(timezone.utc)
    chat_id = uuid.uuid4().hex
    chats_collection.insert_one({
        "chat_id":     chat_id,
        "owner_key":   owner_key,
        "owner_type":  owner_type,  # "email" | "anon"
        "title":       title or "New chat",
        "messages":    [],
        "summary":     "",  # (v5) rolling plain-Python conversation digest
        "created_at":  now,
        "updated_at":  now,
    })
    log.info(f"Chat created | chat_id={chat_id} | owner_key={owner_key} | owner_type={owner_type}")
    return chat_id


def get_chat_session(chat_id: str, owner_key: str):
    """Only returns the chat if it belongs to this owner_key — a guest or
    another account can never read someone else's chat by guessing an id."""
    if not chat_id:
        return None
    return chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key}, {"_id": 0})


def get_user_chats(owner_key: str):
    """All chats for this owner, most recently active first — the sidebar
    list, like Claude/ChatGPT's conversation history."""
    return list(
        chats_collection.find({"owner_key": owner_key}, {"_id": 0}).sort("updated_at", -1)
    )


def delete_chat_session(chat_id: str, owner_key: str) -> bool:
    """(v4.3) Deletes exactly ONE chat — the one identified by chat_id —
    and only if it belongs to owner_key. Uses the same
    {"chat_id": ..., "owner_key": ...} filter as get_chat_session(), so
    the scoping rule is identical to the one already trusted for reading
    a chat: a guest's anon_id or a signed-in user's email can only ever
    delete chats stamped with that exact owner_key, never another
    owner's chats, and never (by construction, since chat_id is always
    an exact single id) the owner's other chats.

    Returns True if a chat was actually deleted, False if no matching
    chat existed for this owner (already gone, wrong id, or belongs to
    someone else) — callers use this to decide whether to also clear
    `active_chat_id` from the session."""
    if not chat_id:
        return False
    result = chats_collection.delete_one({"chat_id": chat_id, "owner_key": owner_key})
    deleted = result.deleted_count > 0
    if deleted:
        log.info(f"Chat deleted | chat_id={chat_id} | owner_key={owner_key}")
    else:
        log.info(f"Chat delete no-op (not found for this owner) | chat_id={chat_id} | owner_key={owner_key}")
    return deleted


def add_search_to_chat(chat_id: str, owner_key: str, query: str, topic_key: str,
                        keywords: list, targeting_platform: str, time_window_days: int = None,
                        unfiltered: bool = False, website_context: dict = None):
    """Appends a search as a new message in the chat, and auto-titles the
    chat from the very first query if it hasn't been named yet.

    UNCHANGED from v4 (and by the keyword-generation swap) aside from the
    ONE new `time_window_days` parameter (default None, so any future
    caller that doesn't pass it behaves exactly as before this feature) —
    this is only ever called for messages the router classified as
    "search". `keywords` is still stored on the message purely for
    internal use (later matching/debugging) — it doesn't matter, and this
    function doesn't care, whether that list came from Claude's router
    call or the generate_fuzzy_keywords() fallback. `time_window_days` is
    stored the same way, purely so later re-matching of this same message
    (see _fill_in_message_outputs() / the streaming route) can pass it
    back into get_matched_signals() consistently. `results` starts as
    `None` (NOT an empty list — see the RESULTS-RECOMPUTE FIX below) and
    gets filled in later by save_signal_results_to_chat() once matching
    signals show up. `claude_answer` starts empty too and is filled in
    once by save_claude_answer_to_chat().

    `website_context` (NEW, optional, default None — every existing
    caller that doesn't pass it behaves exactly as before) stores the
    structured website breakdown (overview + sections) computed at
    INTEGRATION POINT 2, when this search was derived from a website URL
    the user shared alongside a real ask. None means "no website context
    for this message," exactly like `time_window_days=None` already
    means "no time range" for messages that don't have one.

    (GOOGLE-FALLBACK FEATURE) `google_fallback_triggered` always starts
    False on a new message — flipped to True exactly once, by
    mark_google_fallback_triggered(), the first time
    _fill_in_message_outputs() decides to fire the Google-search
    fallback for this message. Older messages (before this feature)
    simply don't have this field at all; every read of it elsewhere
    uses .get(..., False) so that's indistinguishable from False."""
    now = datetime.now(timezone.utc)
    message = {
        "query":              query,
        "topic_key":          topic_key,
        "keywords":           keywords,
        "targeting_platform": targeting_platform,
        "time_window_days":   time_window_days,  # (NEW) int or None — see get_matched_signals()
        "unfiltered":         unfiltered,
        "website_context":    website_context,
        "google_fallback_triggered": False,
        "requested_at":       now,
        # (RESULTS-RECOMPUTE FIX) `None` here, not `[]` — an empty list is
        # a legitimate, ALREADY-COMPUTED final value (e.g. a "no_results"
        # answer genuinely has zero results). Using None to mean "not
        # computed yet" lets _fill_in_message_outputs() tell "never
        # computed" apart from "computed and genuinely empty," so a
        # message that's truly done never gets its (slow, blocking)
        # signal-matching re-run on every later chat view.
        "results":            None,
        "claude_answer":      None, # filled in once: Claude's answer text, grounded in `results`
    }

    chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key})
    update = {"$push": {"messages": message}, "$set": {"updated_at": now}}
    if chat and not chat.get("messages"):
        update["$set"]["title"] = generate_chat_title(query)

    chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key}, update)


def add_chat_message_to_chat(chat_id: str, owner_key: str, query: str, answer: str):
    """(v5) Appends a plain conversational turn — NOT a search — to
    the chat. No topic_key/keywords/targeting_platform, no
    flintel_search_jobs entry, no post-card results, no signal matching
    ever happens for these. This is for messages classify_and_maybe_chat()
    decided were just normal chat (greetings, small talk, general
    questions, follow-ups, the open-ended "what's happening on this
    platform" case, etc.), (v6) a polite decline for a "blocked" message,
    or a clarifying question for a "clarify" message — all three are
    saved with the exact same shape, since all render identically (query
    + answer text, no post cards), so no template changes are needed.

    Completely separate from add_search_to_chat() above, which is
    untouched and still used for every actual search-type message exactly
    as before — the two message shapes coexist in the same `messages`
    array, distinguished by the `message_type` field ("chat" here; absent
    or "search" for ordinary search messages)."""
    now = datetime.now(timezone.utc)
    message = {
        "query":              query,
        "topic_key":          None,
        "keywords":           [],
        "targeting_platform": None,
        "time_window_days":   None,
        "requested_at":       now,
        "results":            [],
        "claude_answer":      answer,
        "message_type":       "chat",
    }

    chat = chats_collection.find_one({"chat_id": chat_id, "owner_key": owner_key})
    update = {"$push": {"messages": message}, "$set": {"updated_at": now}}
    if chat and not chat.get("messages"):
        update["$set"]["title"] = generate_chat_title(query)

    chats_collection.update_one({"chat_id": chat_id, "owner_key": owner_key}, update)


def save_signal_results_to_chat(chat_id: str, owner_key: str, topic_key: str, results: list):
    """Best-effort: writes the matched title/post_text/post_url/platform
    output onto the SAME chat message that holds the original user prompt
    for this topic, exactly as computed by get_matched_signals() — no
    keyword list, job status, or anything else about the job is written
    here. This is the post-cards data, unchanged from v3 in shape.

    Safe to call repeatedly (e.g. on every chat/home page load while the
    background job is still filling in signals) — it just overwrites
    `results` with the latest matched set for that message.

    (RESULTS-RECOMPUTE FIX) Previously a no-op on ANY falsy `results`
    (including a deliberate, final `[]`) — this silently swallowed the
    BUGFIX PACK #1 gate's own "final results are genuinely empty" write,
    meaning that value never actually reached Mongo, `results` stayed at
    its uncomputed initial value forever, and _fill_in_message_outputs()
    kept re-triggering a full re-match on every later chat view. Now only
    skips the write on `None` (a real "nothing to write" signal) — an
    explicit empty list `[]` is a legitimate final value and gets
    persisted like any other."""
    if results is None:
        return
    chats_collection.update_one(
        {"chat_id": chat_id, "owner_key": owner_key, "messages.topic_key": topic_key},
        {"$set": {
            "messages.$.results": results,
            "updated_at": datetime.now(timezone.utc),
        }},
    )


def save_claude_answer_to_chat(chat_id: str, owner_key: str, topic_key: str, answer: str):
    """(v4) Writes ONLY Claude's final answer text onto the same chat
    message — never the posts that were sent in as input, since those
    already live in flintel_signals and don't need duplicating here. This
    is what makes re-opening a chat later show the exact same answer
    again, as-is, with no need to re-call Claude. (v6: also used to cache
    the plain "nothing found yet" timeout answer — same function, same
    caching behavior.)

    UNCHANGED: this still stores whatever string `answer` is, verbatim,
    with no parsing/validation."""
    if not answer:
        return
    chats_collection.update_one(
        {"chat_id": chat_id, "owner_key": owner_key, "messages.topic_key": topic_key},
        {"$set": {
            "messages.$.claude_answer": answer,
            "updated_at": datetime.now(timezone.utc),
        }},
    )


def mark_google_fallback_triggered(chat_id: str, owner_key: str, topic_key: str):
    """(GOOGLE-FALLBACK FEATURE) Same targeted-update pattern as
    save_claude_answer_to_chat()/save_signal_results_to_chat() — flips
    this one message's google_fallback_triggered field to True so
    should_trigger_google_fallback() never fires the Google-search
    fallback more than once for the same message, even if it found zero
    Reddit results.

    Best-effort, never raises past itself: any failure here is logged
    and swallowed rather than breaking whatever background task called
    this."""
    try:
        chats_collection.update_one(
            {"chat_id": chat_id, "owner_key": owner_key, "messages.topic_key": topic_key},
            {"$set": {
                "messages.$.google_fallback_triggered": True,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
    except Exception as exc:
        log.warning(f"Marking google_fallback_triggered failed for topic_key={topic_key}: {exc}")


def migrate_anon_chats_to_owner(anon_id: str, new_owner_key: str):
    """When a guest signs up or logs in, re-key their guest chat history
    onto their account so it isn't lost — same pattern Claude/ChatGPT use
    to carry a guest conversation over after sign-in."""
    if not anon_id or anon_id == new_owner_key:
        return
    result = chats_collection.update_many(
        {"owner_key": anon_id, "owner_type": "anon"},
        {"$set": {"owner_key": new_owner_key, "owner_type": "email"}},
    )
    if result.modified_count:
        log.info(
            f"Migrated {result.modified_count} guest chat(s) | "
            f"anon_id={anon_id} -> owner_key={new_owner_key}"
        )


def _is_owner_busy(owner_key: str) -> bool:
    """(PER-USER BUSY LOCK) True only if THIS owner_key currently has an
    in-flight Claude call that hasn't finished yet, and hasn't gone stale.
    Never checks or affects any other owner_key — this is purely a
    per-user concern, exactly like the feature spec requires."""
    if not owner_key:
        return False
    doc = busy_owners_collection.find_one({"owner_key": owner_key})
    if not doc:
        return False
    if _elapsed_seconds(doc.get("started_at")) >= BUSY_FLAG_TIMEOUT_SECONDS:
        # Stale (a crash or restart left this behind) — clear it
        # defensively right now and treat this request as not busy,
        # rather than leaving the user locked out forever.
        busy_owners_collection.delete_one({"owner_key": owner_key})
        return False
    return True


def _set_owner_busy(owner_key: str):
    """(PER-USER BUSY LOCK) Marks THIS owner_key busy — called right
    before a blocking analyze_with_claude() call begins for a user-facing
    action. upsert=True so this is safe to call even if a stale doc
    somehow already exists for this owner_key."""
    if not owner_key:
        return
    try:
        busy_owners_collection.update_one(
            {"owner_key": owner_key},
            {"$set": {"owner_key": owner_key, "started_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception as exc:
        log.warning(f"Setting busy flag failed for owner_key={owner_key}: {exc}")


def _clear_owner_busy(owner_key: str):
    """(PER-USER BUSY LOCK) Clears THIS owner_key's busy flag — called
    once the in-flight Claude call finishes, success or failure, so the
    user's very next request goes through normally right away."""
    if not owner_key:
        return
    try:
        busy_owners_collection.delete_one({"owner_key": owner_key})
    except Exception as exc:
        log.warning(f"Clearing busy flag failed for owner_key={owner_key}: {exc}")


def _trigger_google_fallback_search(chat_id: str, owner_key: str, msg: dict):
    """(GOOGLE-FALLBACK FEATURE) Fires the Google-search-for-Reddit-posts
    fallback for a message that still has no flintel_signals match after
    GOOGLE_FALLBACK_TRIGGER_SECONDS — see
    flintel.should_trigger_google_fallback() for the timing decision this
    is called in response to.

    (RACE FIX) google_fallback_triggered is now marked SYNCHRONOUSLY by
    the caller (_fill_in_message_outputs()), BEFORE this function is
    scheduled/called — not here anymore — so a second concurrent request
    for the same message can never see the flag still False and schedule
    a duplicate search. This function's only job now is to actually run
    the search and store whatever stubs it finds.

    Wrapped in try/except, never raises — a failure here just means this
    message doesn't get any Google-sourced stub links, exactly like
    before this feature existed."""
    try:
        google_search.search_google_for_reddit_posts(
            msg.get("keywords", []),
            google_posts_collection,
            search_keyword_for_storage=msg.get("topic_key"),
        )
    except Exception as exc:
        log.warning(f"Google-fallback search failed for topic_key={msg.get('topic_key')}: {exc}")


def _timeout_fallback_answer(chat_id: str, owner_key: str, topic_key: str, query: str, keywords: list = None):
    """(PERFORMANCE FIX) Extracted, UNCHANGED logic from the RESPONSE_TIMEOUT
    fallback branch that used to run inline inside _fill_in_message_outputs()
    — same calls, same order, same caching. Pulled out so it can be
    scheduled via BackgroundTasks (runs after the response is sent)
    instead of blocking the request, exactly like
    _complete_message_answer_and_results() below.

    (BUSY-LOCK RACE FIX) _set_owner_busy() is NOT called here anymore —
    it's now set synchronously in _fill_in_message_outputs(), before this
    function is even scheduled, so the flag is already in place before
    the response goes out. This function only clears it, in the finally
    below, once the work actually finishes.

    (GOOGLE-FALLBACK FEATURE) New optional `keywords` parameter (default
    None, so the one call site that doesn't pass it — there isn't one
    left, but this keeps the signature change itself non-breaking) is
    used to pull back whatever Google-sourced Reddit stub links were
    already stored (by _trigger_google_fallback_search(), fired earlier
    at the 40s mark) for this same message, without calling the Google
    API a second time. Those stubs are reformatted into the same shape
    get_matched_signals() returns and run through the EXISTING
    _finalize_answer_and_results() helper — reusing BUG-2b's own
    "no_data format + real matched list -> keep text, append closing
    note, save the list as results" behavior with zero new branching
    logic here."""
    try:
        # (BUG FIX — DON'T RE-SUGGEST A DECLINED ALTERNATIVE) Same
        # continuity context as _complete_message_answer_and_results()
        # above — best-effort, never blocks this fallback answer.
        try:
            existing_chat = get_chat_session(chat_id, owner_key)
            chat_summary_for_answer = (existing_chat or {}).get("summary") or ""
        except Exception as exc:
            log.warning(f"Chat summary lookup failed for topic_key={topic_key}: {exc}")
            chat_summary_for_answer = ""
        extra_ctx_parts = []
        if chat_summary_for_answer:
            extra_ctx_parts.append(
                "Conversation so far (auto-summarized, may be empty) — see "
                "the CONVERSATION CONTINUITY instruction above for how to "
                "use this:\n" + chat_summary_for_answer
            )

        # (GOOGLE-FALLBACK FEATURE) Pull back any stubs already stored at
        # the 40s mark, so this can show them without a second Google
        # API call — safe to call even if none were ever stored (returns
        # []) or if the Google-fallback feature never fired for this
        # message at all.
        stub_docs = google_search.get_stub_results_for_keywords(
            google_posts_collection, keywords or [])
        google_results = flintel.format_google_stub_results(stub_docs)
        google_ctx = flintel.build_google_fallback_answer_context(query, len(stub_docs))
        extra_ctx_parts.append(google_ctx)

        extra_ctx = "\n\n".join(extra_ctx_parts) if extra_ctx_parts else None
        answer = analyze_with_claude(query, [], extra_context=extra_ctx)

        # (GOOGLE-FALLBACK FEATURE) Reuses the EXISTING BUG-2b decision
        # point instead of saving `answer`/no-results untouched — if
        # Claude's own analysis picked a "no_data" format but real
        # Google-sourced stub links exist, this keeps the honest text,
        # appends the usual closing note, and saves the stub links as
        # this message's results, exactly like a normal matched-signals
        # answer already does elsewhere in this file.
        final_answer, results_to_save = _finalize_answer_and_results(answer, google_results, seed=query)

        save_claude_answer_to_chat(chat_id, owner_key, topic_key, final_answer)
        try:
            save_signal_results_to_chat(chat_id, owner_key, topic_key, results_to_save)
        except Exception as exc:
            log.warning(f"Saving google-fallback results failed for topic_key={topic_key}: {exc}")
        try:
            append_to_chat_summary(chat_id, owner_key, query, final_answer)
        except Exception as exc:
            log.warning(f"Updating chat summary failed for topic_key={topic_key}: {exc}")
    except Exception as exc:
        log.warning(f"Timeout-fallback Claude analysis failed for topic_key={topic_key}: {exc}")
    finally:
        _clear_owner_busy(owner_key)


def _complete_message_answer_and_results(chat_id: str, owner_key: str, msg: dict, matched: list):
    """(PERFORMANCE FIX) Extracted, UNCHANGED logic — this is exactly what
    _fill_in_message_outputs() already did inline for the "needs_answer" +
    "needs_results" case, just pulled into its own function so it can be
    run either synchronously (existing behavior, when no background_tasks
    is available) or scheduled via BackgroundTasks (new: runs AFTER the
    response is already sent, so a slow Claude call never blocks the
    request). Not a single line of the actual logic below changed — same
    calls, same order, same caching, same BUGFIX PACK #1 results-gating.

    (BUSY-LOCK RACE FIX) _set_owner_busy() is NOT called here anymore —
    it's now set synchronously in _fill_in_message_outputs(), before this
    function is even scheduled, so the flag is already in place before
    the response goes out. This function only clears it, in the finally
    below, once the work actually finishes.

    (BUG FIX — DON'T HIDE REAL MATCHED POSTS) The results-gating decision
    now goes through _finalize_answer_and_results() instead of the old
    inline `[] if claude_format in _NO_DATA_CLAUDE_FORMATS else matched`
    — real matched posts are no longer discarded just because Claude's
    own written analysis picked a "no_data" format; see that function's
    own docstring for the full reasoning."""
    # (RESULTS-RECOMPUTE FIX, applied here too for the same reason) `is
    # None`, not falsy — closes a low-probability but real analogous gap:
    # if a Claude API call ever technically "succeeds" but returns zero
    # text content, analyze_with_claude() would return "" (empty string,
    # falsy), which `not msg.get(...)` would wrongly treat as "still
    # needs answering" forever, re-triggering Claude on every later view
    # exactly like the results-list bug did. claude_answer already
    # initializes to None (never "") at message creation, so this check
    # is the only piece that needed to change.
    needs_answer = msg.get("claude_answer") is None
    answer_for_format_check = msg.get("claude_answer")

    if needs_answer:
        try:
            extra_ctx_parts = []
            if msg.get("unfiltered"):
                extra_ctx_parts.append(
                    flintel.build_unfiltered_answer_context(
                        msg["query"], msg.get("time_window_days")
                    )
                )
            # (BUG FIX — DON'T RE-SUGGEST A DECLINED ALTERNATIVE) Pull this
            # chat's own rolling summary and hand it to analyze_with_claude()
            # as extra context, so Claude can see whether the user already
            # explicitly declined/narrowed away from an earlier suggested
            # alternative — see CLAUDE_ANALYSIS_SYSTEM_PROMPT's own
            # "CONVERSATION CONTINUITY" instruction for what it does with
            # this. Best-effort: a lookup failure here just means this
            # answer is generated without that continuity context, exactly
            # like before this fix — it never blocks or breaks the answer.
            try:
                existing_chat = get_chat_session(chat_id, owner_key)
                chat_summary_for_answer = (existing_chat or {}).get("summary") or ""
            except Exception as exc:
                log.warning(f"Chat summary lookup failed for topic_key={msg.get('topic_key')}: {exc}")
                chat_summary_for_answer = ""
            if chat_summary_for_answer:
                extra_ctx_parts.append(
                    "Conversation so far (auto-summarized, may be empty) — see "
                    "the CONVERSATION CONTINUITY instruction above for how to "
                    "use this:\n" + chat_summary_for_answer
                )
            extra_ctx = "\n\n".join(extra_ctx_parts) if extra_ctx_parts else None
            answer = analyze_with_claude(msg["query"], matched, extra_context=extra_ctx)
            answer = _patch_post_urls_into_answer(answer, matched)
            if msg.get("website_context"):
                answer = _inject_website_context_into_answer(answer, msg["website_context"])
            msg["claude_answer"] = answer
            save_claude_answer_to_chat(chat_id, owner_key, msg["topic_key"], answer)
            answer_for_format_check = answer
            try:
                append_to_chat_summary(chat_id, owner_key, msg["query"], answer)
            except Exception as exc:
                log.warning(f"Updating chat summary failed for topic_key={msg.get('topic_key')}: {exc}")
        except Exception as exc:
            log.warning(f"Claude analysis failed for topic_key={msg.get('topic_key')}: {exc}")
        finally:
            _clear_owner_busy(owner_key)

    # (RESULTS-RECOMPUTE FIX) `is None`, not falsy — a genuine, already-
    # saved empty list must not be treated as "still needs computing" and
    # get needlessly recomputed/overwritten here either.
    if msg.get("results") is None:
        final_answer, results_to_save = _finalize_answer_and_results(
            answer_for_format_check, matched, seed=msg.get("query", "")
        )
        if final_answer != answer_for_format_check:
            msg["claude_answer"] = final_answer
            try:
                save_claude_answer_to_chat(chat_id, owner_key, msg["topic_key"], final_answer)
            except Exception as exc:
                log.warning(f"Saving closing-note-patched answer failed for topic_key={msg.get('topic_key')}: {exc}")
        msg["results"] = results_to_save
        try:
            save_signal_results_to_chat(chat_id, owner_key, msg["topic_key"], results_to_save)
        except Exception as exc:
            log.warning(f"Saving matched results to chat failed for topic_key={msg.get('topic_key')}: {exc}")


def _fill_in_message_outputs(chat_id: str, owner_key: str, messages: list, skip_topic_key: str = None,
                              background_tasks: BackgroundTasks = None):
    """Shared by home() and view_chat(): for any SEARCH-type message in
    `messages` that's still missing its post-card `results` and/or its
    `claude_answer`, looks up matching signals ONCE and uses that single
    lookup for both:
      - post cards keep working exactly like v3 (results saved as-is,
        subject to the BUGFIX PACK #1 gate described below), and
      - Claude only gets called (and only gets billed) the first time real
        matched posts are actually available for that message, then the
        answer is cached forever after (only the answer, not the posts).
    Best-effort per message — one message failing must never block the
    rest of the page, and Claude failures must never affect post cards.

    (PERFORMANCE FIX) New optional `background_tasks` parameter (default
    None, fully backward-compatible — any existing/other caller that
    doesn't pass it gets the EXACT original synchronous behavior, nothing
    changes for it). When provided, the "generate answer via Claude, then
    save results" step for a message that needs a fresh answer is
    scheduled via `background_tasks.add_task(...)` instead of being run
    inline — the actual logic is byte-for-byte identical either way (see
    `_complete_message_answer_and_results()`), only WHEN it runs changes:
    after the response has already been sent, instead of blocking it.
    This is what makes opening/switching to a chat fast even when one of
    its messages still needs a fresh Claude call — that message simply
    shows its existing "still gathering results" loading state for this
    one page load, exactly like a genuinely brand-new message already
    does, and resolves on the next load once the background task finishes
    and caches it — rather than the whole request blocking on a live
    Claude call that can take several seconds.

    (v5) Messages with no topic_key are plain chat-type turns — skipped
    here immediately, since there is nothing to fill in for them.

    (v6) If a search-type message STILL has no matched posts, once
    RESPONSE_TIMEOUT seconds have passed since the message's own
    `requested_at`, this calls analyze_with_claude(query, []) exactly
    once — reusing that function's existing "no posts yet, say so
    plainly" branch unchanged — so the user gets a natural "sorry,
    nothing found on this yet" answer instead of a permanently blank
    turn. That answer is cached the same way every other answer in this
    file is, so it's generated (and billed) once.

    (BUGFIX PACK #1 — RESULTS/ANSWER SYNC): once the answer for this
    message is known (freshly generated this call, or already cached),
    its "format" is checked via _extract_claude_format(). If that format
    is one of _NO_DATA_CLAUDE_FORMATS ("no_results", "not_available",
    "disallowed"), the post-card `results` saved/rendered for this
    message are forced to an empty list instead of whatever
    get_matched_signals() loosely matched.

    (POST_URL FIX) Right after a fresh (non-streaming) answer is
    generated here, it is passed through _patch_post_urls_into_answer()
    before being cached/checked for format.

    (STREAMING WIRING FIX) `skip_topic_key` (default None) lets a caller
    reserve exactly ONE search-type message so this function leaves it
    completely untouched — used by view_chat() so the new
    `GET /chat/{chat_id}/stream` route, not this function, is what
    produces that one message's first answer.

    (TIME-WINDOW FEATURE) The ONLY change in this function: the
    get_matched_signals() call now also passes
    `since_days=msg.get("time_window_days")` — for every message that has
    no `time_window_days` stored on it (every message from before this
    feature, and every new message where the user gave no time range),
    this is None and behaves exactly as before. For a message that DOES
    have a stored time window, matching now also respects it, exactly the
    same way on every re-render of the same message (so a chat reopened
    later still shows results scoped to the same window it was originally
    asked for).

    OTHERWISE COMPLETELY UNCHANGED — this function calls
    get_matched_signals() and analyze_with_claude() exactly as before,
    using whatever `keywords` was already stored on the message by
    add_search_to_chat() at write time."""
    for msg in messages or []:
        if not msg.get("topic_key"):
            continue

        # (STREAMING WIRING FIX) Reserved for the streaming route to fill
        # in instead — leave this one message completely untouched here.
        if skip_topic_key and msg.get("topic_key") == skip_topic_key:
            continue

        # (RESULTS-RECOMPUTE FIX) `results is None` means "genuinely never
        # computed yet" — an already-saved empty list `[]` (a legitimate
        # "no_results"/"not_available"/"disallowed" outcome, see BUGFIX
        # PACK #1 below) is now correctly recognized as already-done,
        # instead of `not []` (True) wrongly triggering a full re-match
        # + re-answer on every single later chat view.
        needs_results = msg.get("results") is None
        # Same `is None` reasoning as needs_results above, applied to
        # claude_answer for full consistency (see the docstring note in
        # _complete_message_answer_and_results()).
        needs_answer = msg.get("claude_answer") is None
        if not needs_results and not needs_answer:
            continue

        try:
            matched = get_matched_signals(
                msg["topic_key"],
                msg.get("keywords", []),
                targeting_platform=msg.get("targeting_platform", "all"),
                since_days=msg.get("time_window_days"),
                unfiltered=msg.get("unfiltered", False),
            )
        except Exception as exc:
            log.warning(f"Signal matching failed for topic_key={msg.get('topic_key')}: {exc}")
            continue

        if not matched:
            # (v6) Nothing matched yet — before, this silently gave up for
            # this page load and just retried again next time. Now, only
            # once the message has been waiting longer than
            # RESPONSE_TIMEOUT seconds, give the user a plain "nothing
            # found" answer instead of leaving the turn blank forever.
            elapsed = _elapsed_seconds(msg.get("requested_at"))

            # (GOOGLE-FALLBACK FEATURE) Fires at 40s (only once per
            # message, tracked via the message's own
            # google_fallback_triggered field) — well before the 60s
            # RESPONSE_TIMEOUT below, so any stub links it finds are
            # already stored and ready by the time that fallback answer
            # gets generated. This block only ever fires the Google
            # search + stub storage — it never answers the user itself,
            # so it always falls through to the existing RESPONSE_TIMEOUT
            # check below regardless of what it does here.
            if flintel.should_trigger_google_fallback(
                    elapsed, msg.get("google_fallback_triggered", False)):
                # (RACE FIX) Mark synchronously, in THIS request, before
                # scheduling/running the search — mirrors the exact same
                # busy-lock race fix already applied to
                # _set_owner_busy()/_complete_message_answer_and_results()
                # elsewhere in this function. Closes the window where a
                # second concurrent reload for the same message could see
                # google_fallback_triggered still False and schedule a
                # duplicate Google-search call before the first background
                # task finishes.
                mark_google_fallback_triggered(chat_id, owner_key, msg["topic_key"])
                if background_tasks is not None:
                    background_tasks.add_task(_trigger_google_fallback_search, chat_id, owner_key, msg)
                else:
                    _trigger_google_fallback_search(chat_id, owner_key, msg)

            if needs_answer and elapsed >= RESPONSE_TIMEOUT:
                # (BUSY-LOCK RACE FIX) Set synchronously, in THIS request,
                # before scheduling/running the work — not inside the
                # scheduled function itself, which could run after the
                # response has already gone out to the browser.
                _set_owner_busy(owner_key)
                if background_tasks is not None:
                    background_tasks.add_task(_timeout_fallback_answer, chat_id, owner_key, msg["topic_key"], msg["query"], msg.get("keywords", []))
                else:
                    _timeout_fallback_answer(chat_id, owner_key, msg["topic_key"], msg["query"], msg.get("keywords", []))
            continue

        # (BUGFIX PACK #1) Track whatever answer text is/becomes available
        # for this message, so results can be gated on its format below —
        # starts as whatever's already cached (may be None).
        answer_for_format_check = msg.get("claude_answer")

        # (PERFORMANCE FIX) This used to run inline here, blocking the
        # request on a live Claude call whenever needs_answer was True.
        # The exact same logic now lives in
        # _complete_message_answer_and_results() — run inline (identical
        # behavior) when no background_tasks was given, or scheduled to
        # run AFTER the response is sent when it was.
        #
        # (BUSY-LOCK RACE FIX) Set synchronously, in THIS request, before
        # scheduling/running the work — closes the small window where a
        # second request from the same owner could arrive between "the
        # response is sent" and "the background task actually starts",
        # since the flag document wouldn't exist yet during that window.
        _set_owner_busy(owner_key)
        if background_tasks is not None:
            background_tasks.add_task(_complete_message_answer_and_results, chat_id, owner_key, msg, matched)
        else:
            _complete_message_answer_and_results(chat_id, owner_key, msg, matched)


import routes  # noqa: F401  (registers every route on `app`)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("index:app", host="0.0.0.0", port=8080, reload=True)
