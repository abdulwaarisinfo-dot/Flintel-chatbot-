"""
routes.py — all FastAPI route handlers for the Flintel web service,
extracted from index.py to keep that module to shared infrastructure
and business logic. Imports `app` from index.py and registers every
route on it; index.py imports this module once, at the bottom, purely
for its side effect of registering these routes.
"""

import json
import time
import logging

from fastapi import Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, StreamingResponse

import flintel
import website_intelligence

from index import (
    app,
    templates,
    pwd_context,
    oauth,
    log,
    # config the routes read directly
    RESPONSE_TIMEOUT,
    STREAM_CHUNK_CHARS,
    STREAM_CHUNK_DELAY_SECONDS,
    # business logic
    normalize_topic_key,
    normalize_platform,
    generate_fuzzy_keywords,
    enqueue_search_job,
    get_matched_signals,
    analyze_with_claude,
    _call_claude,
    classify_and_maybe_chat,
    resolve_unclear_topic,
    _extract_first_url,
    fetch_website_text,
    extract_keywords_from_website,
    append_to_chat_summary,
    _extract_claude_format,
    _NO_DATA_CLAUDE_FORMATS,
    _patch_post_urls_into_answer,
    _inject_website_context_into_answer,
    _finalize_answer_and_results,       # <-- Bug 2b helper from index.py
    get_current_user,
    _log_user_in,
    create_email_user,
    upsert_google_user,
    get_owner,
    generate_chat_title,
    create_chat_session,
    get_chat_session,
    get_user_chats,
    delete_chat_session,
    add_search_to_chat,
    add_chat_message_to_chat,
    save_signal_results_to_chat,
    save_claude_answer_to_chat,
    migrate_anon_chats_to_owner,
    _is_owner_busy,
    _fill_in_message_outputs,
    CLAUDE_BLOCKED_FALLBACK_REPLY,
    CLAUDE_CLARIFY_FALLBACK_REPLY,
    CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT,
)
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — SEARCH / CHAT
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def home(request: Request):
    """(BARE-URL HOME FIX) Visiting the plain root URL directly (no
    chat_id anywhere in the request) must always show a fresh, empty
    home screen — exactly like visiting claude.ai or chatgpt.com
    directly always shows a new blank conversation, never whatever chat
    was last open in a previous session. This route no longer reads
    `active_chat_id` from the session to decide what to render, and
    never passes a populated `chat` back to the template — only the
    sidebar's chat list is still fetched, so existing chat history
    remains visible and clickable in the sidebar exactly as before.

    A specific chat is ONLY ever rendered by visiting its own URL,
    `GET /chat/{chat_id}` (see view_chat() below, completely UNCHANGED)
    — that route still sets `active_chat_id` in the session when opened,
    exactly as it always has; this fix only changes what the BARE root
    URL itself renders, never what a specific chat URL renders.

    UNCHANGED: chats list fetching, current-user lookup, and the
    template/response contract for index.html (request/user/chats/
    chat_id/chat/pending_stream_topic_key keys are still all passed, just
    with chat_id/chat/pending_stream_topic_key always at their empty
    defaults now instead of being conditionally populated)."""
    chats = []
    try:
        owner_key, _owner_type = get_owner(request)
        chats = get_user_chats(owner_key)
    except Exception as exc:
        log.warning(f"Chat lookup failed on home page: {exc}")

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": get_current_user(request),
            "chats": chats,
            "chat_id": None,
            "chat": None,
            "pending_stream_topic_key": None,
        },
    )


@app.post("/search")
def search(
    request: Request,
    query: str = Form(...),
    platform: str = Form("All Platforms"),
    chat_id: str = Form(None),
):
    # (PER-USER BUSY LOCK) Additive check at the very start, before any
    # existing logic below runs — declines a new request from THIS SAME
    # owner_key while their previous one is still being processed by
    # Claude. Never enqueues a job, never calls the router, never creates
    # a chat message for a declined request. Every other owner_key is
    # completely unaffected — this only ever reads/writes a document keyed
    # to the current request's own owner_key.
    busy_owner_key = None
    try:
        busy_owner_key, _busy_owner_type = get_owner(request)
    except Exception as exc:
        log.warning(f"Owner lookup failed during busy-check (treating as not busy): {exc}")

    if busy_owner_key and _is_owner_busy(busy_owner_key):
        chats_safe, chat_id_safe = [], None
        try:
            chats_safe = get_user_chats(busy_owner_key)
            chat_id_safe = request.session.get("active_chat_id")
        except Exception as exc:
            log.warning(f"Chat lookup failed while rendering busy-decline: {exc}")

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": "Please wait for your current request to finish first.",
                "query": query,
                "user": get_current_user(request),
                "chats": chats_safe,
                "chat_id": chat_id_safe,
            },
        )

    topic_key = normalize_topic_key(query)
    targeting_platform = normalize_platform(platform)

    if not topic_key:
        # Best-effort context for the error re-render only — never let a
        # chat-lookup hiccup get in the way of showing the validation error.
        chats_safe, chat_id_safe = [], None
        try:
            owner_key, _owner_type = get_owner(request)
            chats_safe = get_user_chats(owner_key)
            chat_id_safe = request.session.get("active_chat_id")
        except Exception as exc:
            log.warning(f"Chat lookup failed while rendering empty-query error: {exc}")

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "error": "Please enter a search term.",
                "query": query,
                "user": get_current_user(request),
                "chats": chats_safe,
                "chat_id": chat_id_safe,
            },
        )

    # ─────────────────────────────────────────────────────────────────────
    # v5 ROUTING STEP (v6: abuse/harm screening; KEYWORD-GENERATION SWAP:
    # keyword list; TIME-WINDOW/PAIN-POINT/CLARIFY FEATURE: time window +
    # pain-point-aware keywords + a 4th "clarify" intent; ROUTER INTENT
    # REFINEMENT: prompt-only tightening of when "chat"/"search"/"clarify"
    # each fire — see the module docstring) — runs BEFORE anything else
    # below. Decides whether this message is "search" (the pipeline below
    # runs), "chat" (Claude answers directly), "blocked" (Claude declines
    # directly), or "clarify" (Claude asks a short follow-up question
    # instead of guessing a topic).
    #
    # Owner/active-chat resolution + the router call itself are wrapped
    # in one try/except: ANY failure here falls back to intent="search"
    # with routed_keywords/routed_time_window_days left as None, and the
    # search pipeline below re-resolves owner/chat itself AND falls back
    # to generate_fuzzy_keywords() for the keyword list — so a routing
    # failure can NEVER block, skip, or under-supply a real search job.
    # ─────────────────────────────────────────────────────────────────────
    owner_key = owner_type = None
    active_chat_id = None
    intent = "search"
    chat_reply = None
    routed = {}
    routed_keywords = None
    routed_time_window_days = None
    routed_unfiltered = False
    # (STRUCTURED WEBSITE SUMMARY) Stays None for every case except the
    # two website-derived-keywords paths in INTEGRATION POINT 2 below
    # (BEHAVIOR 2, and the BEHAVIOR 3 confirmed-match branch) — never set
    # for a BEHAVIOR 3 mismatch, no URL at all, unfiltered mode, or a
    # plain keyword search with no URL.
    website_answer_context = None
    # (CLARIFY-SELF-RESOLVE FEATURE) Captured here (not just inside the
    # try block below) so it's still safely readable afterwards even if
    # something later in the try block raises — resolve_unclear_topic()
    # only ever needs this for extra continuity, so an empty string is a
    # completely safe default, identical to how classify_and_maybe_chat()
    # already treats an empty/missing summary.
    chat_summary_for_resolve = ""

    try:
        owner_key, owner_type = get_owner(request)
        active_chat_id = chat_id or request.session.get("active_chat_id")
        if not active_chat_id or not get_chat_session(active_chat_id, owner_key):
            active_chat_id = create_chat_session(owner_key, owner_type, title=generate_chat_title(query))
        request.session["active_chat_id"] = active_chat_id

        existing_chat = get_chat_session(active_chat_id, owner_key)
        chat_summary = (existing_chat or {}).get("summary") or ""
        chat_summary_for_resolve = chat_summary

        routed = classify_and_maybe_chat(query, chat_summary)
        intent = routed.get("intent", "search")
        chat_reply = routed.get("reply")
        routed_keywords = routed.get("keywords")
        routed_time_window_days = routed.get("time_window_days")
        routed_unfiltered = routed.get("unfiltered") or False
    except Exception as exc:
        log.warning(f"v5 routing step failed for query={query!r} (defaulting to normal search pipeline): {exc}")
        intent = "search"

    # ─────────────────────────────────────────────────────────────────────
    # CLARIFY-SELF-RESOLVE — before ever falling back to asking the user,
    # make ONE best-effort attempt to resolve the topic using Claude's own
    # knowledge (no web search, no new information beyond the message +
    # the existing rolling chat summary). If that succeeds, this message
    # is switched to a NORMAL "search" from here on — 100% as-is, using
    # whatever keywords/time_window_days it resolved, falling straight
    # through into the exact same pipeline below. If it can't confidently
    # resolve anything, or the call fails outright, intent stays
    # "clarify" and the EXISTING clarify-question flow immediately below
    # runs completely unchanged.
    # ─────────────────────────────────────────────────────────────────────
    if intent == "clarify":
        resolved = None
        try:
            resolved = resolve_unclear_topic(query, chat_summary_for_resolve)
        except Exception as exc:
            log.warning(f"Clarify self-resolve step failed for query={query!r}: {exc}")
            resolved = None
        if resolved and resolved.get("keywords"):
            intent = "search"
            routed_keywords = resolved["keywords"]
            routed_time_window_days = resolved.get("time_window_days")
            log.info(f"Clarify self-resolved to search | query={query!r} | keywords={routed_keywords}")

    # ─────────────────────────────────────────────────────────────────────
    # WEBSITE-URL BEHAVIOR ROUTING (INTEGRATION POINT 1) — additive only,
    # scoped entirely to this block, using ONLY website_intelligence.py's
    # own functions (no reimplementation of any of its logic here). Runs
    # AFTER the routing step / CLARIFY-SELF-RESOLVE above, BEFORE the
    # existing chat/blocked/clarify handling branch immediately below.
    #
    # "blocked" messages are left completely untouched by this block —
    # abusive/harmful content should never trigger a website fetch or
    # summary, so it falls straight through to the existing
    # chat/blocked/clarify branch below exactly as it always has.
    # ─────────────────────────────────────────────────────────────────────
    detected_url_for_routing = None
    if intent != "blocked":
        detected_url_for_routing = _extract_first_url(query)

    if detected_url_for_routing:
        # PRIMARY SIGNAL: the router's own classification. "chat" already
        # means "no clear topic/request was found in the text" — no need
        # to second-guess that with the pure-Python helper below.
        if intent == "chat":
            is_bare_url_request = True
        else:
            # intent == "clarify" (no explicit topic named) OR the
            # router-failure fallback "search" — in both cases, double-check
            # with the heuristic instead of assuming a bare URL, so a real
            # stated ask alongside the link (e.g. "...its my web so find me
            # for customers") is not incorrectly force-summarized.
            try:
                has_shaped_language = website_intelligence.has_request_shaped_language(
                    query, detected_url_for_routing
                )
                is_generic_leadgen = website_intelligence.is_generic_leadgen_ask(query)
                is_bare_url_request = (not has_shaped_language)
            except Exception as exc:
                log.warning(f"has_request_shaped_language failed for query={query!r}: {exc}")
                is_bare_url_request = False
                is_generic_leadgen = False

        if is_bare_url_request:
            # BEHAVIOR 1 — bare URL, no real ask: summarize the site and
            # invite the user to say what they'd like looked into,
            # instead of guessing a topic or asking a generic clarifying
            # question. Best-effort: any failure fetching or summarizing
            # the site simply leaves this block a no-op, and control
            # falls straight through to the EXISTING clarify/chat
            # fallback-reply behavior immediately below exactly as it
            # works today — no new failure mode is introduced.
            summary = None
            try:
                website_text_for_summary = fetch_website_text(detected_url_for_routing)
                summary = website_intelligence.summarize_website(
                    website_text_for_summary, call_claude_fn=_call_claude
                )
            except Exception as exc:
                log.warning(f"Website fetch/summary failed for url={detected_url_for_routing!r}: {exc}")
                summary = None

            if summary:
                url_only_answer = website_intelligence.build_url_only_reply(summary, query_seed=query)

                redirect_chat_id = None
                try:
                    if not owner_key:
                        owner_key, owner_type = get_owner(request)
                    if not active_chat_id or not get_chat_session(active_chat_id, owner_key):
                        active_chat_id = create_chat_session(owner_key, owner_type, title=generate_chat_title(query))
                    request.session["active_chat_id"] = active_chat_id

                    add_chat_message_to_chat(active_chat_id, owner_key, query, url_only_answer)
                    try:
                        append_to_chat_summary(active_chat_id, owner_key, query, url_only_answer)
                    except Exception as exc:
                        log.warning(f"Updating chat summary failed for chat_id={active_chat_id}: {exc}")
                    redirect_chat_id = active_chat_id
                except Exception as exc:
                    log.warning(f"Saving URL-only reply failed for query={query!r}: {exc}")

                if redirect_chat_id:
                    return RedirectResponse(url=f"/chat/{redirect_chat_id}", status_code=303)
                return RedirectResponse(url="/", status_code=303)
            # else: summarization failed — fall through to the EXISTING
            # clarify/chat fallback-reply behavior below exactly as it
            # works today (do not introduce a new failure mode).
        else:
            # Otherwise: intent is "search" (directly, or resolved to
            # "search" via CLARIFY-SELF-RESOLVE above) and there IS
            # request-shaped language alongside the URL — force intent to
            # "search" if it isn't already, and let INTEGRATION POINT 2
            # (inside the search-type branch below) handle the
            # topic-vs-website connection check instead of the plain
            # WEBSITE-URL KEYWORD EXTRACTION call that used to run there.
            intent = "search"

    # ── CHAT-TYPE, BLOCKED-TYPE, OR CLARIFY-TYPE MESSAGE: answer/decline/ ──
    # ── ask directly, never touch the keyword-generation / job-queue /   ──
    # ── signal-matching pipeline at all. (v6: "blocked" reuses the exact ──
    # ── same handling as "chat"; "clarify" reuses it too — same message  ──
    # ── shape, same redirect — only where the answer text comes from     ──
    # ── below differs. "clarify" only ever reaches here if the           ──
    # ── CLARIFY-SELF-RESOLVE step above couldn't resolve a topic, AND    ──
    # ── (if a URL was present) BEHAVIOR 1 above couldn't produce a       ──
    # ── summary reply.)                                                  ──
    if intent in ("chat", "blocked", "clarify"):
        if intent == "blocked":
            # Never re-sent to Claude for a fallback — a canned decline is
            # enough, and there's no reason to hand harmful content to
            # another prompt just to get a polite "no".
            answer = (chat_reply or "").strip() or CLAUDE_BLOCKED_FALLBACK_REPLY
        elif intent == "clarify":
            # Same pattern: never re-sent to Claude for a fallback — a
            # consultant-style clarifying question is enough if the
            # router's own reply text was missing/unusable for some
            # reason.
            answer = (chat_reply or "").strip() or CLAUDE_CLARIFY_FALLBACK_REPLY
        else:
            answer = (chat_reply or "").strip()
            if not answer:
                # Router classified this as chat but didn't return usable
                # reply text (e.g. truncated/odd output) — fall back to a
                # second, plain conversational call rather than showing
                # nothing.
                try:
                    answer = _call_claude(CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT, query)
                except Exception as exc:
                    log.warning(f"Chat fallback Claude call failed for query={query!r}: {exc}")
                    answer = "Sorry, I couldn't come up with a reply just now — please try again."

        redirect_chat_id = None
        try:
            if not owner_key:
                owner_key, owner_type = get_owner(request)
            if not active_chat_id or not get_chat_session(active_chat_id, owner_key):
                active_chat_id = create_chat_session(owner_key, owner_type, title=generate_chat_title(query))
            request.session["active_chat_id"] = active_chat_id

            add_chat_message_to_chat(active_chat_id, owner_key, query, answer)
            try:
                append_to_chat_summary(active_chat_id, owner_key, query, answer)
            except Exception as exc:
                log.warning(f"Updating chat summary failed for chat_id={active_chat_id}: {exc}")
            redirect_chat_id = active_chat_id
        except Exception as exc:
            log.warning(f"Saving {intent}-type message failed for query={query!r}: {exc}")

        if redirect_chat_id:
            return RedirectResponse(url=f"/chat/{redirect_chat_id}", status_code=303)
        return RedirectResponse(url="/", status_code=303)

    # ── SEARCH-TYPE MESSAGE: everything below is the v1-v7 pipeline,   ──
    # ── UNCHANGED except for WHERE `keywords` comes from and the new   ──
    # ── `time_window_days` value carried alongside it. (This branch is ──
    # ── now also reached by a "clarify" message the CLARIFY-SELF-      ──
    # ── RESOLVE step above successfully resolved — from this point on  ──
    # ── it is treated 100% identically to any other search message.)   ──

    # ─────────────────────────────────────────────────────────────────────
    # WEBSITE-URL KEYWORD EXTRACTION (INTEGRATION POINT 2) — if the user's
    # message itself contains a website URL, this decides between
    # BEHAVIOR 2 ("find leads/customers/posts related to MY site", no
    # separately-named topic — the ORIGINAL, UNCHANGED
    # extract_keywords_from_website() call) and BEHAVIOR 3 (a SEPARATE
    # named topic alongside the URL, checked against the site's own
    # content via website_intelligence.check_topic_matches_website()).
    # Uses ONLY website_intelligence.py's own functions for the new
    # logic — no reimplementation here.
    # ─────────────────────────────────────────────────────────────────────
    detected_url = _extract_first_url(query)
    if detected_url:
        # Same pure-Python heuristic used in INTEGRATION POINT 1 above,
        # reused here for the SAME underlying question: is there real
        # request-shaped language beyond just referencing/pasting the
        # link? No separately-named topic (BEHAVIOR 2) vs a separately
        # named topic alongside the URL (BEHAVIOR 3 candidate).
        try:
            has_named_topic = (
                website_intelligence.has_request_shaped_language(query, detected_url)
                and not website_intelligence.is_generic_leadgen_ask(query)
            )
        except Exception as exc:
            log.warning(f"has_request_shaped_language failed for url={detected_url!r}: {exc}")
            has_named_topic = False

        if not has_named_topic:
            # BEHAVIOR 2 — "find me leads/customers/posts related to my
            # site" with no separately-named topic: EXISTING behavior,
            # now sourced from the SAME combined Claude call that also
            # produces the structured website summary (see FIX D above)
            # instead of a separate summarize_website_structured() call.
            website_keywords = None
            website_text = None
            website_extraction_result = None
            try:
                website_text = fetch_website_text(detected_url)
                website_extraction_result = extract_keywords_from_website(query, detected_url, website_text)
            except Exception as exc:
                log.warning(f"Website fetch/keyword-extraction failed for url={detected_url!r}: {exc}")
                website_extraction_result = None
            if website_extraction_result:
                website_keywords = website_extraction_result.get("keywords")
                if website_keywords:
                    routed_keywords = website_keywords
                    log.info(f"Website-derived keywords used | url={detected_url!r} | keywords={routed_keywords}")
                website_answer_context = website_intelligence.format_structured_summary_for_answer(
                    website_extraction_result.get("structured_summary")
                )
        else:
            # BEHAVIOR 3 candidate — a separately named topic alongside
            # the URL: check whether that topic genuinely connects to
            # what the website offers, via ONE combined Claude call that
            # produces both the match verdict and (if it matches) the
            # keyword list in one shot.
            website_text_for_match = None
            topic_match_result = None
            try:
                website_text_for_match = fetch_website_text(detected_url)
                topic_match_result = website_intelligence.check_topic_matches_website(
                    query, detected_url, website_text_for_match, call_claude_fn=_call_claude
                )
            except Exception as exc:
                log.warning(f"Topic-vs-website check failed for url={detected_url!r}: {exc}")
                topic_match_result = None

            if not topic_match_result or not isinstance(topic_match_result.get("topic_matches_website"), bool):
                # Inconclusive/failure — fall straight through to the
                # EXISTING behavior as if this integration point didn't
                # exist: the plain extract_keywords_from_website() call,
                # feeding the EXISTING safety-net chain
                # (routed_keywords -> generate_fuzzy_keywords()) exactly
                # as before this feature, now also sourced from the SAME
                # combined Claude call for keywords + structured summary
                # (see FIX D above).
                website_extraction_result = None
                try:
                    website_text = (
                        website_text_for_match
                        if website_text_for_match is not None
                        else fetch_website_text(detected_url)
                    )
                    website_extraction_result = extract_keywords_from_website(query, detected_url, website_text)
                except Exception as exc:
                    log.warning(f"Website fetch/keyword-extraction failed for url={detected_url!r}: {exc}")
                    website_extraction_result = None
                if website_extraction_result:
                    website_keywords = website_extraction_result.get("keywords")
                    if website_keywords:
                        routed_keywords = website_keywords
                        log.info(f"Website-derived keywords used | url={detected_url!r} | keywords={routed_keywords}")
                    website_answer_context = website_intelligence.format_structured_summary_for_answer(
                        website_extraction_result.get("structured_summary")
                    )
            elif topic_match_result["topic_matches_website"] is True:
                # Topic genuinely connects to the website — use the
                # keywords produced by the SAME call, exactly like
                # extract_keywords_from_website()'s existing output is
                # used today. The structured summary also comes straight
                # from this SAME call's own "structured_summary" field —
                # no separate summarize_website_structured() call needed.
                if topic_match_result.get("keywords"):
                    routed_keywords = topic_match_result["keywords"]
                    log.info(
                        f"Topic-vs-website match confirmed | url={detected_url!r} | "
                        f"keywords={routed_keywords}"
                    )
                website_answer_context = website_intelligence.format_structured_summary_for_answer(
                    topic_match_result.get("structured_summary")
                )
            else:
                # BEHAVIOR 3's actual trigger — the stated topic does NOT
                # connect to this website: tell the user plainly instead
                # of enqueuing an irrelevant search, and skip the rest of
                # the search pipeline for this request entirely.
                mismatch_answer = website_intelligence.build_topic_mismatch_reply(query)

                redirect_chat_id = None
                try:
                    if not owner_key:
                        owner_key, owner_type = get_owner(request)
                    if not active_chat_id or not get_chat_session(active_chat_id, owner_key):
                        active_chat_id = create_chat_session(owner_key, owner_type, title=generate_chat_title(query))
                    request.session["active_chat_id"] = active_chat_id

                    add_chat_message_to_chat(active_chat_id, owner_key, query, mismatch_answer)
                    try:
                        append_to_chat_summary(active_chat_id, owner_key, query, mismatch_answer)
                    except Exception as exc:
                        log.warning(f"Updating chat summary failed for chat_id={active_chat_id}: {exc}")
                    redirect_chat_id = active_chat_id
                except Exception as exc:
                    log.warning(f"Saving topic-mismatch reply failed for query={query!r}: {exc}")

                if redirect_chat_id:
                    return RedirectResponse(url=f"/chat/{redirect_chat_id}", status_code=303)
                return RedirectResponse(url="/", status_code=303)

    # (unfiltered mode) routed_unfiltered as parsed from the router is NEVER
    # trusted blindly — flintel.is_time_only_request() requires a genuine
    # positive time_window_days before "unfiltered" is allowed to mean
    # anything. This re-assigns routed_unfiltered to the VALIDATED result,
    # so this same, now-safe value is what both the keywords decision below
    # AND the add_search_to_chat(...) call further down use — an
    # unvalidated flag is never allowed to reach either place.
    routed_unfiltered = bool(routed_unfiltered) and flintel.is_time_only_request(routed)

    # (KEYWORD-GENERATION SWAP) Keywords now come from the SAME Claude
    # routing call above instead of the old plain-Python template
    # generator (or, per the two features above, from the clarify
    # self-resolve step or the website-URL extraction step). generate_
    # fuzzy_keywords() is KEPT, unchanged, purely as a safety-net fallback
    # for when none of those produced usable keywords.
    if routed_keywords:
        keywords = routed_keywords
    elif routed_unfiltered:
        # (unfiltered mode) A validated unfiltered request skips keyword
        # generation entirely — get_matched_signals() takes the
        # unfiltered=True early-return path instead of ever needing a
        # keyword list. If routed_unfiltered is False (unvalidated, or the
        # router never set it), this branch is never taken and behavior
        # below is 100% identical to before this feature.
        keywords = []
    else:
        log.warning(
            f"No usable keywords from the Claude router for query={query!r} "
            f"— falling back to generate_fuzzy_keywords()"
        )
        keywords = generate_fuzzy_keywords(query)
    keywords = keywords[:MAX_KEYWORDS]

    # (TIME-WINDOW FEATURE) time_window_days is purely a downstream
    # matching/filtering concern — see get_matched_signals() — so it is
    # NOT passed into enqueue_search_job() (the background service's job
    # doesn't change based on it); it's only carried along onto the chat
    # message below so re-matching this same message later stays scoped
    # to the same window the user actually asked for.
    time_window_days = routed_time_window_days

    enqueue_search_job(topic_key, keywords, targeting_platform)

    # Chat/session bookkeeping is best-effort on top of the above: if
    # anything here fails — a stale/corrupt session cookie, a hiccup on the
    # flintel_users_chat collection, a Claude API error, etc. — it must
    # NEVER take down or skip the actual search job that was just queued.
    redirect_chat_id = None
    try:
        if not owner_key:
            owner_key, owner_type = get_owner(request)
        active_chat_id = active_chat_id or chat_id or request.session.get("active_chat_id")
        if not active_chat_id or not get_chat_session(active_chat_id, owner_key):
            active_chat_id = create_chat_session(owner_key, owner_type, title=generate_chat_title(query))
        request.session["active_chat_id"] = active_chat_id
        add_search_to_chat(
            active_chat_id, owner_key, query, topic_key, keywords, targeting_platform,
            time_window_days=time_window_days,
            unfiltered=routed_unfiltered,
            website_context=website_answer_context,
        )
        redirect_chat_id = active_chat_id
    except Exception as exc:
        log.warning(f"Chat bookkeeping failed for topic_key={topic_key} (job was still queued): {exc}")

    # Stay on the same chat thread — like Claude/ChatGPT keeping you in the
    # conversation you're in, instead of bouncing back to the home screen.
    if redirect_chat_id:
        return RedirectResponse(url=f"/chat/{redirect_chat_id}", status_code=303)
    return RedirectResponse(url="/", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — CHATS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/chats")
def list_chats(request: Request):
    """Sidebar-style list of every chat belonging to the current owner
    (signed-in email, or guest UUID)."""
    owner_key, _owner_type = get_owner(request)
    return {"chats": get_user_chats(owner_key)}


@app.post("/chats/new")
def new_chat(request: Request, title: str = Form(None)):
    """(NEW CHAT BEHAVIOR FIX) Clicking "New chat" now behaves exactly
    like Claude/ChatGPT: it does NOT create any new chat document in
    Mongo, and does NOT touch any existing chat. It only clears
    `active_chat_id` from the session, so the very next page load
    (home()) renders the empty/home search screen with no active chat
    selected — ready for the user to type their first message.

    The actual chat document is still created lazily, exactly as it
    already is: the moment the user's first message is sent via
    POST /search (chat, blocked, clarify, or search intent), that
    existing, UNCHANGED logic creates a new chat via
    create_chat_session() whenever active_chat_id is missing or invalid
    for the current owner. Nothing about that creation logic changed.

    REMOVED (no longer needed): the previous v4.4 FIX reuse-if-empty
    logic that checked whether the currently active chat had zero
    messages and reused it instead of creating a duplicate — that
    problem can no longer occur, since this route itself never creates
    an empty chat anymore for there to be a duplicate of.

    `title` is still accepted as a form parameter for backward-
    compatible form compatibility with any existing frontend that posts
    it, but it is no longer used for anything, since no chat is created
    here to title."""
    request.session.pop("active_chat_id", None)
    return RedirectResponse(url="/", status_code=303)


@app.get("/chat/{chat_id}")
def view_chat(request: Request, chat_id: str, background_tasks: BackgroundTasks):
    """Opens a specific past chat and makes it active again — this is how
    a returning user (or a user who just logged back in with their email)
    gets the same chat back, including any previously matched post cards
    AND Claude's previously generated answer, exactly as saved.

    Note for the template: render each message's `query`, `results`
    (title, post_text, post_url, platform) as post cards, and
    `claude_answer` as the actual answer text. `keywords` and
    `time_window_days` stay on the message purely for internal use and
    should not be displayed here. (v5) A message with
    `"message_type": "chat"` has no `results` to show (it's always an
    empty list) — just render `query` + `claude_answer` like a normal
    conversational turn, with no post cards underneath. (v6) A polite
    "blocked" decline, a "clarify" clarifying question, and a timeout
    "nothing found yet" answer all use this exact same rendering path
    already — no template changes needed for any of them.

    NOTE (JSON-ANALYSIS-PROMPT SWAP): `claude_answer` will now typically
    be a raw JSON string for search-type messages. This template contract
    note is left exactly as it was — no template/rendering changes were
    made as part of that swap, per what was asked.

    (STREAMING WIRING FIX) Before filling anything in, this now looks at
    the LAST message in the chat: if it has a `topic_key` and its
    `claude_answer` is still falsy, that message's `topic_key` is passed
    to `_fill_in_message_outputs()` as `skip_topic_key`, so a template's
    streaming JS is expected to open
    `GET /chat/{chat_id}/stream?topic_key=...` for that one message
    instead."""
    owner_key, _owner_type = get_owner(request)
    chat = get_chat_session(chat_id, owner_key)
    if not chat:
        return RedirectResponse(url="/", status_code=303)

    request.session["active_chat_id"] = chat_id

    # (STREAMING WIRING FIX) Identify the one freshly-added search-type
    # message (if any) still waiting on its very first answer, so it can
    # be reserved for the new streaming route instead of being filled in
    # here like every other message.
    pending_stream_topic_key = None
    messages = chat.get("messages") or []
    if messages:
        latest_msg = messages[-1]
        if latest_msg.get("topic_key") and latest_msg.get("claude_answer") is None:
            pending_stream_topic_key = latest_msg["topic_key"]

    # Same best-effort fill-in as home(): compute post cards + Claude's
    # answer for any SEARCH-type message that doesn't have them yet.
    if messages:
        _fill_in_message_outputs(chat_id, owner_key, messages, skip_topic_key=pending_stream_topic_key,
                                  background_tasks=background_tasks)

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user": get_current_user(request),
            "chat": chat,
            "chats": get_user_chats(owner_key),
            "pending_stream_topic_key": pending_stream_topic_key,
        },
    )


@app.get("/chat/{chat_id}/stream")
def stream_answer(request: Request, chat_id: str, topic_key: str):
    """(STREAMING ADD-ON, rebuilt by SIMULATED-STREAM FIX) Server-Sent-
    Events endpoint: delivers Claude's analysis answer for one specific
    search-type message (identified by `topic_key`, within this chat) as
    a sequence of small paced chunks, so a template can show it arriving
    with the same "typing" impression as Claude.ai/ChatGPT.

    Each SSE event is a JSON payload on a `data:` line:
      {"delta": "<next chunk of text>"}   -- zero or more, as text is paced out
      {"done": true}                       -- exactly once, when finished
      {"error": "<short reason>"}          -- instead of the above, on failure

    (SIMULATED-STREAM FIX) The text streamed out here is NOT Claude's raw
    token-by-token output. `event_generator()` first calls the existing,
    UNCHANGED, blocking `analyze_with_claude()` to get the COMPLETE
    answer, runs it through `_patch_post_urls_into_answer()`, THEN paces
    that final string out in small pieces (`STREAM_CHUNK_CHARS`
    characters, `STREAM_CHUNK_DELAY_SECONDS` pause between pieces) to
    reproduce the live-typing impression.

    On successful completion, this SAME already-patched string is saved
    via the EXACT SAME save_claude_answer_to_chat() /
    append_to_chat_summary() / save_signal_results_to_chat() calls
    already used by _fill_in_message_outputs().

    Unchanged guard: if the target message's `claude_answer` is already
    truthy, this immediately replays that cached text as a single `delta`
    event followed by `done`, and returns — it never redoes any
    matching/Claude work for a message that already has its answer.

    (TIME-WINDOW FEATURE) The ONLY change in this route: the
    get_matched_signals() call now also passes
    `since_days=msg.get("time_window_days")` — for every message with no
    stored time window (every message from before this feature, and any
    new message where the user gave no time range) this is None and
    behaves exactly as before.

    (BUG FIX 2b) The results-gating decision inside event_generator() now
    goes through the shared _finalize_answer_and_results() helper
    (imported from index.py) instead of its own separate inline
    "no_data format -> hide results" check, applied BEFORE the answer is
    paced out — so a streamed answer and the cached/re-rendered version
    of it can never disagree on whether real matched posts get shown."""
    owner_key, _owner_type = get_owner(request)
    chat = get_chat_session(chat_id, owner_key)

    if not chat:
        def _no_chat():
            yield f"data: {json.dumps({'error': 'chat not found'})}\n\n"
        return StreamingResponse(_no_chat(), media_type="text/event-stream")

    msg = next(
        (m for m in chat.get("messages", []) if m.get("topic_key") == topic_key),
        None,
    )
    if not msg:
        def _no_msg():
            yield f"data: {json.dumps({'error': 'message not found'})}\n\n"
        return StreamingResponse(_no_msg(), media_type="text/event-stream")

    # Already generated/cached earlier (via the normal blocking path, or
    # a previous call to this same route) — replay it instead of ever
    # re-calling Claude for it again.
    if msg.get("claude_answer"):
        cached_answer = msg["claude_answer"]

        def _cached():
            yield f"data: {json.dumps({'delta': cached_answer})}\n\n"
            # (FRONTEND CLOSEST-MATCHES FEATURE — additive only) Includes
            # this message's already-saved matched results in the "done"
            # payload, exactly like the main completion path below, so a
            # replayed/cached answer can render its "closest matches"
            # post cards the same way a freshly-streamed one does. New
            # field only — the existing "done": true contract is
            # untouched for any client that doesn't read it.
            yield f"data: {json.dumps({'done': True, 'results': msg.get('results') or []})}\n\n"
        return StreamingResponse(_cached(), media_type="text/event-stream")

    try:
        matched = get_matched_signals(
            topic_key,
            msg.get("keywords", []),
            targeting_platform=msg.get("targeting_platform", "all"),
            since_days=msg.get("time_window_days"),
            unfiltered=msg.get("unfiltered", False),
        )
    except Exception as exc:
        log.warning(f"Signal matching failed for streaming topic_key={topic_key}: {exc}")
        matched = []

    def event_generator():
        # (PER-USER BUSY LOCK) Set right at the start of the whole
        # generator, cleared in the finally below — covers every exit
        # path (the early error-return, and the normal completion path
        # after the answer is saved) exactly once, so the flag is never
        # left set no matter how this generator ends.
        _set_owner_busy(owner_key)
        try:
            # (SIMULATED-STREAM FIX) Step 1: get the COMPLETE answer first,
            # via the same blocking function every other answer path in this
            # file already uses — no raw live Claude tokens are sent to the
            # browser anymore.
            try:
                extra_ctx = None
                if msg.get("unfiltered"):
                    extra_ctx = flintel.build_unfiltered_answer_context(
                        msg["query"], msg.get("time_window_days")
                    )
                full_answer = analyze_with_claude(msg["query"], matched, extra_context=extra_ctx)
            except Exception as exc:
                log.warning(f"Streaming Claude analysis failed for topic_key={topic_key}: {exc}")
                yield f"data: {json.dumps({'error': 'analysis failed'})}\n\n"
                return

            full_answer = (full_answer or "").strip()

            # (SIMULATED-STREAM FIX) Step 2: patch real post_url values into
            # the complete answer BEFORE any of it is ever sent to the
            # browser.
            if full_answer:
                full_answer = _patch_post_urls_into_answer(full_answer, matched)
                if msg.get("website_context"):
                    full_answer = _inject_website_context_into_answer(full_answer, msg["website_context"])

            # (BUG FIX 2b) Decide the final answer text + final results
            # BEFORE pacing anything out, using the SAME shared decision
            # point the non-streaming path uses
            # (_complete_message_answer_and_results() in index.py), so a
            # streamed answer and a re-rendered/cached answer can never
            # disagree on whether real matched posts get shown. This
            # never hides posts that were actually matched just because
            # Claude's own analysis chose a "no_data" format — see
            # _finalize_answer_and_results()'s docstring in index.py.
            results_to_save = matched
            if full_answer:
                full_answer, results_to_save = _finalize_answer_and_results(
                    full_answer, matched, seed=msg.get("query", "")
                )

            # (SIMULATED-STREAM FIX) Step 3: pace the now-final string back out
            # in small pieces to reproduce the live-typing impression.
            if full_answer:
                for i in range(0, len(full_answer), STREAM_CHUNK_CHARS):
                    piece = full_answer[i:i + STREAM_CHUNK_CHARS]
                    yield f"data: {json.dumps({'delta': piece})}\n\n"
                    if STREAM_CHUNK_DELAY_SECONDS > 0:
                        time.sleep(STREAM_CHUNK_DELAY_SECONDS)

            # Caching — identical calls/behavior to before, just now operating
            # on the same already-patched, already-finalized string the user
            # just watched arrive.
            if full_answer:
                try:
                    save_claude_answer_to_chat(chat_id, owner_key, topic_key, full_answer)
                    append_to_chat_summary(chat_id, owner_key, msg["query"], full_answer)
                except Exception as exc:
                    log.warning(f"Caching streamed answer failed for topic_key={topic_key}: {exc}")

                # (RESULTS-RECOMPUTE FIX, preserved) Save unconditionally,
                # not gated on `if matched:` — a genuinely empty result
                # set still needs its final `[]` persisted, or `results`
                # stays uncomputed forever. `results_to_save` was already
                # decided above by _finalize_answer_and_results().
                try:
                    save_signal_results_to_chat(chat_id, owner_key, topic_key, results_to_save)
                except Exception as exc:
                    log.warning(f"Saving matched results failed for topic_key={topic_key}: {exc}")

            yield f"data: {json.dumps({'done': True, 'results': results_to_save})}\n\n"
        finally:
            _clear_owner_busy(owner_key)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/chat/{chat_id}/delete")
def delete_chat(request: Request, chat_id: str):
    """(v4.3) Deletes exactly ONE chat belonging to the current owner —
    never every chat for that owner, and never a chat belonging to a
    different owner (signed-in email or guest UUID)."""
    owner_key, _owner_type = get_owner(request)
    deleted = delete_chat_session(chat_id, owner_key)

    if deleted and request.session.get("active_chat_id") == chat_id:
        request.session.pop("active_chat_id", None)

    return RedirectResponse(url="/", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — AUTH: GOOGLE OAUTH
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/auth/google/login")
async def google_login(request: Request):
    redirect_uri = request.url_for("google_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback")
async def google_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as exc:
        log.warning(f"Google OAuth callback failed: {exc}")
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Google sign-in failed. Please try again.", "user": None},
        )

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.google.parse_id_token(request, token)

    google_id = userinfo["sub"]
    email = userinfo["email"]
    name = userinfo.get("name") or email

    # Capture the guest UUID (if any) BEFORE login overwrites how get_owner()
    # resolves this request, so we can migrate any guest chats onto the
    # account being signed into — same as v2's existing linking behavior,
    # just extended to chat history too.
    anon_id = request.session.get("anon_id")

    user_doc = upsert_google_user(google_id=google_id, email=email, name=name)
    _log_user_in(request, user_doc)
    try:
        migrate_anon_chats_to_owner(anon_id, email)
    except Exception as exc:
        log.warning(f"Guest chat migration failed for {email} (sign-in still succeeded): {exc}")
    log.info(f"Google sign-in | email={email}")

    return RedirectResponse(url="/")


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES — AUTH: EMAIL + PASSWORD
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/signup")
def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    email_norm = email.strip().lower()

    if password != confirm_password:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Passwords do not match.", "user": None},
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Password must be at least 8 characters.", "user": None},
        )

    if users_collection.find_one({"email": email_norm}):
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "An account with that email already exists.", "user": None},
        )

    anon_id = request.session.get("anon_id")

    password_hash = pwd_context.hash(password)
    user_doc = create_email_user(email_norm, password_hash)
    _log_user_in(request, user_doc)
    try:
        migrate_anon_chats_to_owner(anon_id, email_norm)
    except Exception as exc:
        log.warning(f"Guest chat migration failed for {email_norm} (signup still succeeded): {exc}")
    log.info(f"New email signup | email={email_norm}")

    return RedirectResponse(url="/", status_code=303)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email_norm = email.strip().lower()
    user = users_collection.find_one({"email": email_norm})

    if not user or not user.get("password_hash") or not pwd_context.verify(password, user["password_hash"]):
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Invalid email or password.", "query": None, "user": None},
        )

    anon_id = request.session.get("anon_id")

    users_collection.update_one(
        {"_id": user["_id"]}, {"$set": {"last_login_at": datetime.now(timezone.utc)}}
    )
    _log_user_in(request, user)
    try:
        migrate_anon_chats_to_owner(anon_id, email_norm)
    except Exception as exc:
        log.warning(f"Guest chat migration failed for {email_norm} (login still succeeded): {exc}")
    log.info(f"Email login | email={email_norm}")

    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    # Clearing the session only drops the login cookie and the guest UUID
    # for THIS browser session — nothing is deleted from Mongo. Because
    # chats are stored keyed by email, logging back in with the same email
    # looks the chats up again via get_owner() -> get_user_chats() and
    # they're right where they were, same as Claude/ChatGPT.
    request.session.clear()
    return RedirectResponse(url="/")

