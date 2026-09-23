"""
routes.py — all FastAPI route handlers for the Flintel web service,
extracted from index.py to keep that module to shared infrastructure
and business logic. Imports `app` from index.py and registers every
route on it; index.py imports this module once, at the bottom, purely 
for its side effect of registering these routes.  
"""

import json
import re
import time
import threading
import logging

from fastapi import Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, StreamingResponse

import flintel
import google as google_search   # the new google.py module — needed here
    # for the Google-fallback stub-results read-back at RESPONSE_TIMEOUT
    # (mirrors index.py's own `import google as google_search` alias)
import website_intelligence
from logics import get_or_fetch_website_evidence, generate_keywords_for_website_request

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
    MAX_KEYWORDS,                       # <-- FIX: was missing, caused NameError in search()
    MAX_ANALYSIS_EVIDENCE,              # <-- EVIDENCE-BUDGET: needed in stream_answer()
    MIN_ANALYSIS_EVIDENCE,              # <-- EVIDENCE-BUDGET: needed in stream_answer()
    # business logic
    normalize_topic_key,
    normalize_platform,
    generate_fuzzy_keywords,
    enqueue_search_job,
    get_matched_signals,
    get_evidence_with_topup,            # <-- TOPIC EVIDENCE CACHE: needed in stream_answer()
    analyze_with_claude,
    _call_claude,
    classify_and_maybe_chat,
    resolve_unclear_topic,
    _extract_first_url,
    fetch_website_text,
    extract_keywords_from_website,
    append_to_chat_summary,
    _extract_claude_format,
    _extract_near_match_confidence,     # <-- CLOSEST-MATCHES TIER-3 REFINEMENT: needed in stream_answer()
    _NO_DATA_CLAUDE_FORMATS,
    _patch_post_urls_into_answer,
    _inject_website_context_into_answer,
    _finalize_answer_and_results,       # <-- Bug 2b helper from index.py
    _elapsed_seconds,                   # <-- GOOGLE-FALLBACK POLLING FIX: needed in stream_answer()
    mark_google_fallback_triggered,     # <-- GOOGLE-FALLBACK POLLING FIX: needed in stream_answer()
    _trigger_google_fallback_search,    # <-- GOOGLE-FALLBACK POLLING FIX: needed in stream_answer()
    mark_search_progress_generated,     # <-- SEARCH-PROGRESS UI: needed in stream_answer()
    save_search_progress_to_chat,       # <-- SEARCH-PROGRESS UI: needed in stream_answer()
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
    save_last_website_context_to_chat,  # <-- WEBSITE INTELLIGENCE: needed in search()
    migrate_anon_chats_to_owner,
    _is_owner_busy,
    _set_owner_busy,                    # <-- FIX: was missing, used in stream_answer()
    _clear_owner_busy,                  # <-- FIX: was missing, used in stream_answer()
    _fill_in_message_outputs,
    CLAUDE_BLOCKED_FALLBACK_REPLY,
    CLAUDE_CLARIFY_FALLBACK_REPLY,
    CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT,
)
from database import users_collection  # <-- FIX: was missing, used in signup()/login()
from database import google_posts_collection  # <-- GOOGLE-FALLBACK POLLING FIX: needed in stream_answer()
from datetime import datetime, timezone
from config import URL_PROMPT_MERGE_ENABLED, URL_PROMPT_MAX_KEYWORDS, URL_PROMPT_MAX_PHRASES, URL_MERGED_MAX_PHRASES


# ─────────────────────────────────────────────────────────────────────────────
# WEBSITE INTELLIGENCE — module-level helpers/constants shared by both
# search() (BEHAVIOR 1/2/3 + the vague follow-up reuse feature) and
# stream_answer() (the website_only streaming branch). Additive only —
# nothing here changes any existing route's behavior on its own.
# ─────────────────────────────────────────────────────────────────────────────

# (VAGUE WEBSITE FOLLOW-UP REUSE — LOOSENED, FIX 3) Last-resort Python
# backup for a router "chat"/"clarify" turn with NO url in the message,
# used ONLY when the router itself didn't already flag reuse via
# use_website_context/website_topic_relation — that addendum rule in
# CLAUDE_ROUTER_WEBSITE_CONTEXT_ADDENDUM (logics.py) is now the PRIMARY
# signal, since it can actually understand arbitrary phrasing the way a
# fixed regex never can.
#
# This backup used to require a positive keyword match (reddit/posts/
# reviews/etc.) and so silently missed any other phrasing ("mujhe iske
# baare mein aur batao", "kya log ispar bura bol rahe hain"). It now
# DEFAULTS TO REUSE and only turns reuse OFF on two negative/"opposite"
# signals:
#   1. _WEBSITE_FOLLOWUP_SMALLTALK_RE — the message is pure small talk
#      with nothing else in it (a bare "hi"/"thanks"/"ok" etc.). Genuine
#      chit-chat must never silently reuse a saved website's keywords —
#      this preserves the original small-talk exclusion.
#   2. _WEBSITE_FOLLOWUP_NEW_TOPIC_RE — the message itself signals a
#      switch to something else ("instead", "a different topic", "alag
#      topic", "naya brand", "ignore that website", etc.).
# Anything else, in any phrasing/language, now defaults to reuse.
_WEBSITE_FOLLOWUP_SMALLTALK_RE = re.compile(
    r"^\s*(hi+|hello+|hey+|salaam\w*|assalam\w*|thanks?|thank\s*you|shukriya|"
    r"ok(?:ay)?|bye+|good\s*(?:morning|afternoon|evening|night)|yes|yeah|"
    r"no|haan|nahi|theek\s*hai)\s*[.,!?]*\s*$",
    re.IGNORECASE,
)
_WEBSITE_FOLLOWUP_NEW_TOPIC_RE = re.compile(
    r"\b(instead|different\s+(?:topic|brand|business|industry)|another\s+"
    r"(?:topic|brand|business)|switch(?:ing)?\s+to|not\s+about|forget\s+"
    r"(?:that|it|the\s+website)|ignore\s+(?:that|the)\s+website|alag\s+topic|"
    r"dusra\s+topic|dusri\s+company|kisi\s+aur|naya\s+topic|naye\s+topic|"
    r"new\s+topic|new\s+brand)\b",
    re.IGNORECASE,
)


def _evidence_to_structured_summary(se):
    """(WEBSITE INTELLIGENCE CACHE) Adapts the flat `structured_evidence`
    dict returned by get_or_fetch_website_evidence()/logics.py into the
    SAME {"overview": ..., "sections": [{"title": ..., "bullets": [...]}]}
    shape website_intelligence.format_structured_summary_for_answer() (and
    the chat.html frontend rendering website_context) already expect
    elsewhere in this file (from extract_keywords_from_website()'s own
    "structured_summary" field) — `sections` is a LIST of
    {title, bullets} dicts, not a dict keyed by title. No section is added
    unless it actually has at least one bullet to show. `pricing` is
    joined into a plain string if it comes back as a list, so it never
    renders as a Python repr. Returns None if nothing usable was found at
    all."""
    if not se:
        return None

    overview = se.get("value_proposition") or se.get("business") or ""
    sections = []

    products_services = (se.get("products_services") or [])[:5]
    if products_services:
        sections.append({"title": "What they offer", "bullets": products_services})

    who_bullets = []
    target_customer = se.get("target_customer")
    if target_customer:
        who_bullets.append(f"Target customer: {target_customer}")
    pricing = se.get("pricing")
    if isinstance(pricing, list):
        pricing = ", ".join(p for p in pricing if isinstance(p, str) and p.strip())
    if pricing:
        who_bullets.append(f"Pricing: {pricing}")
    if who_bullets:
        sections.append({"title": "Who it's for & pricing", "bullets": who_bullets})

    features = (se.get("features") or [])[:4]
    if features:
        sections.append({"title": "Notable", "bullets": features})

    if not overview and not sections:
        return None

    return {"overview": overview, "sections": sections}


def _build_website_context_summary(ctx):
    """Router ko dene ke liye saved website context ka chhota text."""
    if not ctx:
        return None
    parts = [f"URL: {ctx.get('url')}"]
    if ctx.get("business"):
        parts.append(f"Business: {ctx['business']}")
    kws = [k for k in (ctx.get("keywords") or []) if isinstance(k, str)][:8]
    if kws:
        parts.append("Saved keywords: " + ", ".join(kws))
    return "\n".join(parts)


def _build_website_note(ctx, mode):
    """Analysis ke extra_context ke liye note. mode: own | related | unrelated"""
    ctx = ctx or {}
    biz = ctx.get("business") or ctx.get("url") or "their website"
    if mode == "own":
        return (
            f"Note: the user previously shared their own website ({biz}). This request is about "
            f"their own business/niche, so read the posts as leads/market signals relevant to that "
            f"business. If genuinely supported, business_insight may explain how these posts relate to it."
        )
    if mode == "related":
        return (
            f"Note: the user's own website ({biz}) was shared earlier in this chat. This request is about "
            f"a specific topic connected to it — keep the analysis focused on that topic only."
        )
    return (
        f"Note: the user's own website ({biz}) was shared earlier in this chat, but THIS request's topic "
        f"has no direct connection to their niche. Begin the first sentence of the answer's "
        f"executive_summary (or the \"message\" field for no_results) with one short note, in the same "
        f"language style the user wrote in, saying: you checked their website, this topic isn't directly "
        f"connected to their niche, but you're sharing these posts anyway in case they're useful. Do not "
        f"treat the posts as leads for their business."
    )


def _merge_unique(primary, secondary, limit):
    """Do string lists ko merge karta hai: primary pehle, phir secondary;
    case-insensitive dedupe; limit tak."""
    out, seen = [], set()
    for lst in (primary, secondary):
        for item in (lst or []):
            if not isinstance(item, str):
                continue
            clean = item.strip()
            key = clean.lower()
            if not clean or key in seen:
                continue
            seen.add(key)
            out.append(clean)
            if len(out) >= limit:
                return out
    return out


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
    # (STALE-SESSION FIX) Mirrors new_chat()'s own
    # request.session.pop("active_chat_id", None) — visiting the bare
    # home URL must be a genuinely fresh start in BOTH senses: the
    # visual render (already handled below, unchanged) AND the
    # session-side chat pointer search() falls back to whenever the
    # submitted form's own chat_id is empty. Without this, a message
    # typed from this visually-empty home screen was silently
    # appended onto whatever chat was active before the user
    # navigated here (back button, logo click, typing "/" directly —
    # any path other than the "New" button), since search()'s own
    # `chat_id or request.session.get("active_chat_id")` fallback
    # would still resolve to that stale value.
    request.session.pop("active_chat_id", None)

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

    if busy_owner_key:
        _set_owner_busy(busy_owner_key)

    try:
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
        routed_match_phrases = None
        routed_time_window_days = None
        routed_unfiltered = False
        # (WEBSITE INTELLIGENCE — VAGUE FOLLOW-UP REUSE) True only when this
        # message's keywords were reused from a previously stored
        # last_website_context instead of being freshly generated — used
        # below purely to skip a redundant enqueue_search_job() call, since
        # the matching job for those keywords/topic is already collecting
        # data under the ORIGINAL message's topic_key.
        reuse_website_keywords = False
        # (BUG 1) Set once, inside the router try-block below, to the
        # chat's currently stored last_website_context (if any) — reused
        # by both the router's own website_ctx_summary and the VAGUE
        # WEBSITE FOLLOW-UP REUSE block right after it, so both read the
        # exact same snapshot instead of two separate Mongo reads.
        saved_website_ctx = None
        # (BUG 1) The short note appended to analyze_with_claude()'s extra
        # context (via stream_answer()'s extra_ctx_parts) whenever this
        # message reused a previously shared website's keywords, or the
        # router flagged this search as related/unrelated to it. None for
        # every other message — completely unused otherwise.
        website_note = None
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

            saved_website_ctx = (existing_chat or {}).get("last_website_context")
            website_ctx_summary = (
                _build_website_context_summary(saved_website_ctx)
                if saved_website_ctx and not _extract_first_url(query) else None
            )

            routed = classify_and_maybe_chat(query, chat_summary, website_ctx_summary)
            intent = routed.get("intent", "search")
            chat_reply = routed.get("reply")
            routed_keywords = routed.get("keywords")
            routed_match_phrases = routed.get("match_phrases")
            routed_time_window_days = routed.get("time_window_days")
            routed_unfiltered = routed.get("unfiltered") or False
        except Exception as exc:
            log.warning(f"v5 routing step failed for query={query!r} (defaulting to normal search pipeline): {exc}")
            intent = "search"

        # ─────────────────────────────────────────────────────────────────────
        # VAGUE WEBSITE FOLLOW-UP REUSE (WEBSITE INTELLIGENCE, Point 3) — a
        # message with NO url of its own, in a chat that already has a
        # stored `last_website_context` (saved by BEHAVIOR 1/2/3 below on an
        # earlier message in this SAME chat), reuses those stored keywords
        # instead of asking the user to clarify or answering generically.
        # Runs AFTER the v5 routing step above, BEFORE CLARIFY-SELF-RESOLVE
        # immediately below — so a successful reuse here switches `intent`
        # to "search" and CLARIFY-SELF-RESOLVE's own `if intent == "clarify"`
        # check simply never fires for this message, saving the extra
        # resolve_unclear_topic() Claude call entirely.
        #
        # (BUG 1) The router itself now sees the saved website context (via
        # website_ctx_summary above) and can classify this turn directly,
        # returning routed["use_website_context"] and/or
        # routed["website_topic_relation"] — this block's own
        # _WEBSITE_FOLLOWUP_RE regex check is kept ONLY as a backup for
        # "clarify"/"chat" turns the router didn't already flag for reuse.
        # last_website_context itself is deliberately NEVER overwritten
        # here — it stays exactly as BEHAVIOR 1/2/3 originally saved it, so
        # later follow-ups keep working off the same saved keywords.
        #
        # Best-effort only: any failure here simply leaves `intent`/
        # routed_* completely untouched, falling straight through to the
        # EXISTING clarify-self-resolve / chat / search handling below
        # exactly as it works today — no new failure mode is introduced.
        # ─────────────────────────────────────────────────────────────────────
        if not _extract_first_url(query):
            try:
                ctx = saved_website_ctx
                if ctx and ctx.get("keywords") and intent != "blocked":
                    should_reuse = bool(routed.get("use_website_context"))
                    if not should_reuse and intent in ("clarify", "chat"):
                        # (LOOSENED BACKUP, FIX 3) Default to reuse now —
                        # only skip it on genuine small talk or an explicit
                        # new-topic signal (see the regex definitions above).
                        stripped_query = query.strip()
                        is_smalltalk_only = bool(_WEBSITE_FOLLOWUP_SMALLTALK_RE.match(stripped_query))
                        names_new_topic = bool(_WEBSITE_FOLLOWUP_NEW_TOPIC_RE.search(query))
                        should_reuse = not is_smalltalk_only and not names_new_topic
                    if should_reuse:
                        intent = "search"
                        # (FRESH KEYWORDS FROM SAVED EVIDENCE) Purane saved
                        # keywords blindly reuse karne ke bajaye, saved
                        # structured_evidence + user ke NAYE prompt se naye
                        # keywords/match_phrases generate hote hain. Website
                        # dobara fetch NAHI hoti — sirf saved evidence use hota
                        # hai. reuse_website_keywords jaan-boojh kar False hi
                        # rehta hai, taake neeche enqueue_search_job() in naye
                        # keywords ke sath chale.
                        fresh = None
                        saved_evidence = ctx.get("structured_evidence")
                        if saved_evidence:
                            try:
                                fresh = generate_keywords_for_website_request(query, ctx.get("url"), saved_evidence)
                            except Exception as exc:
                                log.warning(f"Fresh keyword generation from saved website evidence failed: {exc}")
                                fresh = None
                        if fresh and fresh.get("keywords"):
                            routed_keywords = fresh["keywords"]
                            routed_match_phrases = fresh.get("match_phrases")
                            log.info(f"Fresh keywords from saved website evidence | chat_id={active_chat_id} | keywords={routed_keywords}")
                        else:
                            # fallback: purani chats (jin mein structured_evidence save nahi) ya generation fail
                            routed_keywords = ctx["keywords"]
                            routed_match_phrases = ctx.get("match_phrases")
                            log.info(f"Reusing last website context (fallback) | chat_id={active_chat_id} | keywords={routed_keywords}")
                        website_note = _build_website_note(ctx, "own")
                    elif intent == "search" and routed.get("website_topic_relation") in ("related", "unrelated"):
                        website_note = _build_website_note(ctx, routed["website_topic_relation"])
            except Exception as exc:
                log.warning(f"Website follow-up reuse check failed for query={query!r}: {exc}")

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
                routed_match_phrases = resolved.get("match_phrases")
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
            # PRIMARY SIGNAL: the router's own "website_only" flag, when
            # present, or its classification. "chat" already means "no
            # clear topic/request was found in the text" — no need to
            # second-guess that with the pure-Python helper below.
            if routed.get("website_only") is True or intent == "chat":
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
                # BEHAVIOR 1 — bare URL, no real ask: fetch/reuse cached
                # website evidence, generate website-derived keywords, and
                # kick off a real (website-scoped) search job instead of
                # just summarizing the site. Best-effort throughout: any
                # failure fetching evidence simply leaves this block a
                # no-op, and control falls straight through to the
                # EXISTING clarify/chat fallback-reply behavior immediately
                # below exactly as it works today — no new failure mode is
                # introduced.
                evidence = None
                try:
                    evidence = get_or_fetch_website_evidence(detected_url_for_routing, query, _call_claude)
                except Exception as exc:
                    log.warning(f"Website evidence fetch failed for url={detected_url_for_routing!r}: {exc}")
                    evidence = None

                if evidence:
                    if evidence.get("evidence_quality") != "failed":
                        try:
                            kw = generate_keywords_for_website_request(
                                query, evidence["url"], evidence["structured_evidence"]
                            )
                        except Exception as exc:
                            log.warning(f"Website keyword generation failed for url={evidence.get('url')!r}: {exc}")
                            kw = {"keywords": None, "match_phrases": None}
                    else:
                        kw = {"keywords": None, "match_phrases": None}

                    keywords = kw.get("keywords")
                    if keywords is None:
                        bare_structured_evidence = evidence.get("structured_evidence") or {}
                        bare_seed = bare_structured_evidence.get("title") or bare_structured_evidence.get("business")
                        keywords = generate_fuzzy_keywords(bare_seed) if bare_seed else []
                    keywords = keywords[:MAX_KEYWORDS]

                    if keywords:
                        enqueue_search_job(topic_key, keywords, targeting_platform)

                    redirect_chat_id = None
                    try:
                        if not owner_key:
                            owner_key, owner_type = get_owner(request)
                        if not active_chat_id or not get_chat_session(active_chat_id, owner_key):
                            active_chat_id = create_chat_session(owner_key, owner_type, title=generate_chat_title(query))
                        request.session["active_chat_id"] = active_chat_id

                        add_search_to_chat(
                            active_chat_id, owner_key, query, topic_key, keywords, targeting_platform,
                            match_phrases=kw.get("match_phrases"),
                            website_only=True,
                            website_evidence={
                                "url": evidence["url"],
                                "structured_evidence": evidence["structured_evidence"],
                                "evidence_quality": evidence["evidence_quality"],
                            },
                        )
                        if keywords:
                            try:
                                save_last_website_context_to_chat(
                                    active_chat_id, owner_key,
                                    {
                                        "url": evidence["url"],
                                        "topic_key": topic_key,
                                        "keywords": keywords,
                                        "match_phrases": kw.get("match_phrases"),
                                        "business": (
                                            (evidence.get("structured_evidence") or {}).get("business")
                                            or (evidence.get("structured_evidence") or {}).get("title")
                                        ),
                                        "structured_evidence": evidence.get("structured_evidence"),
                                    },
                                )
                            except Exception as exc:
                                log.warning(f"Saving last website context failed for chat_id={active_chat_id}: {exc}")
                        redirect_chat_id = active_chat_id
                    except Exception as exc:
                        log.warning(f"Saving website-only search failed for query={query!r}: {exc}")

                    if redirect_chat_id:
                        return RedirectResponse(url=f"/chat/{redirect_chat_id}", status_code=303)
                    return RedirectResponse(url="/", status_code=303)
                # else: evidence fetch failed — fall through to the EXISTING
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
                        answer = _call_claude(CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT, query, enable_web_search=True)
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
        # separately-named topic — cache-aware via get_or_fetch_website_
        # evidence(), falling back to the ORIGINAL, UNCHANGED
        # extract_keywords_from_website() call) and BEHAVIOR 3 (a SEPARATE
        # named topic alongside the URL, checked against the site's own
        # content via website_intelligence.check_topic_matches_website()).
        # Uses ONLY website_intelligence.py's/logics.py's own functions for
        # the new logic — no reimplementation here.
        #
        # `website_context_url` is set whenever BEHAVIOR 2 or BEHAVIOR 3
        # actually produced website-derived keywords for this message — used
        # further below (AFTER the main add_search_to_chat() call) to persist
        # a fresh `last_website_context` for the VAGUE WEBSITE FOLLOW-UP
        # REUSE feature above to pick up on a later message in this chat.
        # ─────────────────────────────────────────────────────────────────────
        website_context_url = None
        # (URL+PROMPT MERGE) Router ki di hui keywords/phrases, is se PEHLE ke
        # kisi bhi website-derived override se pehle capture ki gayi hain.
        prompt_keywords_from_router = list(routed_keywords) if routed_keywords else []
        prompt_phrases_from_router = list(routed_match_phrases) if routed_match_phrases else []
        behavior2_website_keywords = False
        # (FRESH KEYWORDS FROM SAVED EVIDENCE) Set only when BEHAVIOR 2's
        # cached-evidence branch, or BEHAVIOR 3's confirmed-match branch,
        # actually had a structured_evidence dict available — persisted
        # alongside last_website_context below so a later, URL-less
        # follow-up can generate fresh keywords from it without ever
        # re-fetching the website. Stays None on every other path.
        website_structured_evidence_for_save = None
        # (BUG 1) Set whenever BEHAVIOR 2/3 (or BEHAVIOR 1 above) manages to
        # pull a short business/title seed out of the website's own
        # evidence — used ONLY by the final generate_fuzzy_keywords()
        # safety-net fallback further below, so a fuzzy-keyword fallback
        # never has to fall back to seeding itself with the raw URL string.
        website_seed_for_fallback = None
        detected_url = _extract_first_url(query)
        if detected_url:
            # Same pure-Python heuristic used in INTEGRATION POINT 1 above,
            # reused here for the SAME underlying question: is there real
            # request-shaped language beyond just referencing/pasting the
            # link? No separately-named topic (BEHAVIOR 2) vs a separately
            # named topic alongside the URL (BEHAVIOR 3 candidate).
            try:
                uat = routed.get("url_ask_type")
                if uat == "generic_own_business":
                    has_named_topic = False
                elif uat == "specific_topic":
                    has_named_topic = True
                else:
                    # router ne nahi bataya (fail/None): purana heuristic backup
                    has_named_topic = (
                        website_intelligence.has_request_shaped_language(query, detected_url)
                        and not website_intelligence.is_generic_leadgen_ask(query)
                    )
            except Exception as exc:
                log.warning(f"has_request_shaped_language failed for url={detected_url!r}: {exc}")
                has_named_topic = False

            if not has_named_topic:
                # BEHAVIOR 2 — "find me leads/customers/posts related to my
                # site" with no separately-named topic. Cache-aware: tries
                # the shared website-evidence cache/fetch first (the SAME
                # get_or_fetch_website_evidence() BEHAVIOR 1 uses), so a
                # site already fetched earlier in this chat isn't fetched a
                # second time. Any failure, missing evidence, or a "failed"
                # evidence_quality falls straight through to the ORIGINAL
                # fetch_website_text() + extract_keywords_from_website()
                # path exactly as it worked before this feature — a pure
                # safety net, no new failure mode.
                website_evidence = None
                try:
                    website_evidence = get_or_fetch_website_evidence(detected_url, query, _call_claude)
                except Exception as exc:
                    log.warning(f"Website evidence fetch failed for url={detected_url!r}: {exc}")
                    website_evidence = None

                used_cached_evidence = False
                if website_evidence and website_evidence.get("evidence_quality") != "failed":
                    kw = None
                    try:
                        kw = generate_keywords_for_website_request(
                            query, website_evidence["url"], website_evidence["structured_evidence"]
                        )
                    except Exception as exc:
                        log.warning(f"Website keyword generation failed for url={website_evidence.get('url')!r}: {exc}")
                        kw = None
                    # (BUG 1) A usable business/title seed came out of this
                    # evidence regardless of whether keyword generation
                    # itself succeeded — captured here so the final
                    # generate_fuzzy_keywords() safety net below never has
                    # to seed itself with the raw URL string.
                    _se = website_evidence.get("structured_evidence") or {}
                    website_seed_for_fallback = (_se.get("business") or _se.get("title") or "")[:80] or None
                    if kw and kw.get("keywords"):
                        routed_keywords = kw["keywords"]
                        routed_match_phrases = kw.get("match_phrases")
                        log.info(f"Website-derived keywords used | url={detected_url!r} | keywords={routed_keywords}")
                        website_answer_context = website_intelligence.format_structured_summary_for_answer(
                            _evidence_to_structured_summary(website_evidence.get("structured_evidence"))
                        )
                        website_context_url = website_evidence.get("url") or detected_url
                        behavior2_website_keywords = True
                        website_structured_evidence_for_save = website_evidence.get("structured_evidence")
                        used_cached_evidence = True

                if not used_cached_evidence:
                    # PURANA path — safety net, unchanged, except the site
                    # is no longer re-fetched if we already have its text
                    # from the evidence cache above (BUG 1).
                    website_keywords = None
                    website_text = None
                    website_extraction_result = None
                    try:
                        website_text = (website_evidence or {}).get("combined_text") or fetch_website_text(detected_url)
                        website_extraction_result = extract_keywords_from_website(query, detected_url, website_text)
                    except Exception as exc:
                        log.warning(f"Website fetch/keyword-extraction failed for url={detected_url!r}: {exc}")
                        website_extraction_result = None
                    if website_extraction_result:
                        website_keywords = website_extraction_result.get("keywords")
                        if website_keywords:
                            routed_keywords = website_keywords
                            routed_match_phrases = website_extraction_result.get("match_phrases")
                            log.info(f"Website-derived keywords used | url={detected_url!r} | keywords={routed_keywords}")
                            website_context_url = detected_url
                            behavior2_website_keywords = True
                        website_answer_context = website_intelligence.format_structured_summary_for_answer(
                            website_extraction_result.get("structured_summary")
                        )
            else:
                # BEHAVIOR 3 candidate — a separately named topic alongside
                # the URL: check whether that topic genuinely connects to
                # what the website offers, via ONE combined Claude call that
                # produces both the match verdict and (if it matches) the
                # keyword list in one shot. The website text fed into that
                # check is now sourced from the shared evidence cache first
                # (get_or_fetch_website_evidence()'s own "combined_text"),
                # to avoid a redundant fetch — falling back to
                # fetch_website_text() exactly as before whenever the cache
                # doesn't have it.
                website_text_for_match = None
                topic_match_result = None
                # (FRESH KEYWORDS FROM SAVED EVIDENCE) Initialized BEFORE the
                # try below so it is always defined by the time the
                # confirmed-match branch further down reads it, even if the
                # try block raises before its own inner assignment runs.
                cached_evidence_for_match = None
                try:
                    cached_evidence_for_match = None
                    try:
                        cached_evidence_for_match = get_or_fetch_website_evidence(detected_url, query, _call_claude)
                    except Exception as exc:
                        log.warning(f"Website evidence fetch failed for url={detected_url!r}: {exc}")
                        cached_evidence_for_match = None

                    website_text_for_match = (
                        cached_evidence_for_match.get("combined_text")
                        if cached_evidence_for_match and cached_evidence_for_match.get("combined_text")
                        else fetch_website_text(detected_url)
                    )
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
                    # as before this feature.
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
                            routed_match_phrases = website_extraction_result.get("match_phrases")
                            log.info(f"Website-derived keywords used | url={detected_url!r} | keywords={routed_keywords}")
                            website_context_url = detected_url
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
                    # generate_keywords_for_website_request() is deliberately
                    # NOT called again here — the keywords from this SAME
                    # check_topic_matches_website() call are reused as-is.
                    # (PHRASE-MATCHING FEATURE) topic_match_result comes from
                    # website_intelligence.check_topic_matches_website(),
                    # which is out of scope for this feature and does not
                    # return match_phrases — routed_match_phrases simply
                    # stays None for this path, which get_matched_signals()
                    # already handles gracefully (falls back to the
                    # existing keyword-based check).
                    if topic_match_result.get("keywords"):
                        routed_keywords = topic_match_result["keywords"]
                        log.info(
                            f"Topic-vs-website match confirmed | url={detected_url!r} | "
                            f"keywords={routed_keywords}"
                        )
                        website_context_url = detected_url
                        website_structured_evidence_for_save = (cached_evidence_for_match or {}).get("structured_evidence")
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

        # (URL+PROMPT MERGE) BEHAVIOR 2 (URL + generic ask, no separately-named
        # topic) mein website-derived keywords ne router ki prompt-based keywords
        # ko override kar diya tha — yahan dono ko merge karte hain (prompt pehle,
        # phir website), taake user ka apna prompt bhi search mein reflect ho.
        # BEHAVIOR 1 / BEHAVIOR 3 / follow-up reuse mein behavior2_website_keywords
        # False rehta hai, isliye wahan yeh block kabhi nahi chalta.
        if URL_PROMPT_MERGE_ENABLED and behavior2_website_keywords and (prompt_keywords_from_router or prompt_phrases_from_router):
            merged_kw = _merge_unique(prompt_keywords_from_router[:URL_PROMPT_MAX_KEYWORDS], routed_keywords, MAX_KEYWORDS)
            merged_ph = _merge_unique(prompt_phrases_from_router[:URL_PROMPT_MAX_PHRASES], routed_match_phrases, URL_MERGED_MAX_PHRASES)
            if merged_kw:
                routed_keywords = merged_kw
            if merged_ph:
                routed_match_phrases = merged_ph
            log.info(f"URL+prompt keywords merged | prompt_kw={prompt_keywords_from_router[:URL_PROMPT_MAX_KEYWORDS]} | final_kw={routed_keywords}")

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
        # generator (or, per the features above, from the clarify
        # self-resolve step, the vague-website-follow-up reuse step, or the
        # website-URL extraction step). generate_fuzzy_keywords() is KEPT,
        # unchanged, purely as a safety-net fallback for when none of those
        # produced usable keywords.
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
            # (BUG 1) fuzzy_seed prefers a short business/title seed pulled
            # from the website's own evidence (website_seed_for_fallback)
            # over the raw query text whenever a URL is present — before
            # this, a fuzzy fallback for a URL-containing query fed the raw
            # URL string itself into generate_fuzzy_keywords(), producing
            # junk keywords.
            fuzzy_seed = query
            if detected_url:
                fuzzy_seed = website_seed_for_fallback or query.replace(detected_url, " ").strip() or query
            log.warning(f"No usable keywords for query={query!r} — fuzzy fallback with seed={fuzzy_seed!r}")
            keywords = generate_fuzzy_keywords(fuzzy_seed)
        keywords = keywords[:MAX_KEYWORDS]

        # (TIME-WINDOW FEATURE) time_window_days is purely a downstream
        # matching/filtering concern — see get_matched_signals() — so it is
        # NOT passed into enqueue_search_job() (the background service's job
        # doesn't change based on it); it's only carried along onto the chat
        # message below so re-matching this same message later stays scoped
        # to the same window the user actually asked for.
        time_window_days = routed_time_window_days

        # (VAGUE WEBSITE FOLLOW-UP REUSE) When this message's keywords were
        # reused from a stored last_website_context, the matching job for
        # them is already collecting data under the ORIGINAL message's
        # topic_key — enqueueing a second, duplicate job here for the SAME
        # keywords under this NEW topic_key would be redundant work with no
        # benefit, so it's skipped. Every other case enqueues exactly as
        # before this feature.
        if not reuse_website_keywords:
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
                match_phrases=routed_match_phrases,
                evidence_required=routed.get("evidence_required"),
                website_note=website_note,
            )
            # (WEBSITE INTELLIGENCE) Whenever this message's keywords came
            # from the website (BEHAVIOR 2, or a BEHAVIOR 3 confirmed
            # match), persist a fresh last_website_context on this chat —
            # AFTER add_search_to_chat() above, and deliberately WITHOUT
            # website_only (this is a normal Reddit-evidence search, not a
            # website-only reply) — so a later, vague follow-up message in
            # this same chat can reuse it (see the VAGUE WEBSITE FOLLOW-UP
            # REUSE block near the top of this function).
            if website_context_url and keywords:
                try:
                    save_last_website_context_to_chat(
                        active_chat_id, owner_key,
                        {
                            "url": website_context_url,
                            "topic_key": topic_key,
                            "keywords": keywords,
                            "match_phrases": routed_match_phrases,
                            "business": website_seed_for_fallback,
                            "structured_evidence": website_structured_evidence_for_save,
                        },
                    )
                except Exception as exc:
                    log.warning(f"Saving last website context failed for chat_id={active_chat_id}: {exc}")
            redirect_chat_id = active_chat_id
        except Exception as exc:
            log.warning(f"Chat bookkeeping failed for topic_key={topic_key} (job was still queued): {exc}")

        # Stay on the same chat thread — like Claude/ChatGPT keeping you in the
        # conversation you're in, instead of bouncing back to the home screen.
        if redirect_chat_id:
            return RedirectResponse(url=f"/chat/{redirect_chat_id}", status_code=303)
        return RedirectResponse(url="/", status_code=303)
    finally:
        if busy_owner_key:
            _clear_owner_busy(busy_owner_key)


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

    (WEBSITE INTELLIGENCE) Immediately after that cached-answer guard, and
    BEFORE the normal get_matched_signals()/get_evidence_with_topup() call
    below, a message saved with `website_only: True` (BEHAVIOR 1 in
    search()) is served by its OWN generator entirely — it never touches
    signal matching at all, and instead re-derives (or replays, if another
    request for the same owner is already in flight) a website-insight
    answer straight from the message's stored `website_evidence` via
    website_intelligence.build_website_insight_answer().

    (TIME-WINDOW FEATURE) The ONLY change in this route: the
    get_matched_signals() call now also passes
    `since_days=msg.get("time_window_days")` — for every message with no
    stored time window (every message from before this feature, and any
    new message where the user gave no time range) this is None and
    behaves exactly as before.

    (EVIDENCE-BUDGET FEATURE) The ONLY other change in this route:
    every get_matched_signals() call that decides the primary/polling
    matched-signal pool now also passes `limit=effective_evidence_limit`
    — computed once, right after `msg` is fetched, from
    `msg.get("evidence_required")`, clamped between MIN_ANALYSIS_EVIDENCE
    and MAX_ANALYSIS_EVIDENCE exactly like index.py's own
    `_complete_message_answer_and_results()` / `_timeout_fallback_answer()`
    do — and the merge_matched_and_google_results() call now also passes
    `max_total=effective_evidence_limit`, so a streamed answer's evidence
    pool can never exceed (or be capped differently than) the
    non-streaming path's. For every message with no stored
    `evidence_required` (every message from before this feature), this
    resolves to MIN_ANALYSIS_EVIDENCE exactly as before — zero behavior
    change for old/non-search messages.

    (BUG FIX 2b) The results-gating decision inside event_generator() now
    goes through the shared _finalize_answer_and_results() helper
    (imported from index.py) instead of its own separate inline
    "no_data format -> hide results" check, applied BEFORE the answer is
    paced out — so a streamed answer and the cached/re-rendered version
    of it can never disagree on whether real matched posts get shown.

    (GOOGLE-FALLBACK POLLING FIX) The very FIRST message of any new chat
    is always served by THIS route, never by _fill_in_message_outputs()
    (view_chat() reserves it via skip_topic_key) — so before this fix,
    if the initial get_matched_signals() call above found nothing, this
    route answered immediately from an empty list, with no waiting, no
    RESPONSE_TIMEOUT check, and no Google-search fallback ever
    triggered. Because the answer got cached right away,
    _fill_in_message_outputs() would see claude_answer already set on
    every later reload and skip the message entirely — so the Google
    fallback logic could never run for a chat's first message at all.
    Now, if the initial call finds nothing, event_generator() polls
    get_matched_signals() again every ~2 seconds (checking
    should_trigger_google_fallback() on each iteration, exactly like
    _fill_in_message_outputs() already does) until either real signals
    appear or RESPONSE_TIMEOUT is reached — at which point it falls back
    to whatever Google-search stub results are already stored, mirroring
    _timeout_fallback_answer()'s own behavior exactly, so the streamed
    answer stays consistent with the non-streaming path. If the initial
    call already found something, none of this polling ever runs — zero
    change to that already-working case.

    (DUPLICATE-REFRESH FIX) Before doing any of its own work,
    event_generator() now checks `_is_owner_busy(owner_key)`. If a
    Claude call for this SAME owner is already in flight — e.g. this
    exact SSE connection got dropped and the browser's EventSource
    reconnected, or the chat page was refreshed while the very first
    stream request for this message was still running — this does NOT
    start a second, independent `analyze_with_claude()` call. Instead it
    waits (bounded by RESPONSE_TIMEOUT) for the in-flight call to finish
    and cache the answer, then replays that cached answer exactly like
    the existing `claude_answer already truthy` branch above does. Only
    if the in-flight call never finishes within that window does this
    fall through to running its own call, so a genuine stall can never
    hang the page forever. See event_generator()'s own comment below for
    the full rationale."""
    owner_key, _owner_type = get_owner(request)
    chat = get_chat_session(chat_id, owner_key)

    if not chat:
        def _no_chat():
            yield f"data: {json.dumps({'error': 'chat not found'})}\n\n"
        return StreamingResponse(_no_chat(), media_type="text/event-stream")

    msg = next(
        (m for m in reversed(chat.get("messages") or []) if m.get("topic_key") == topic_key),
        None,
    )
    if not msg:
        def _no_msg():
            yield f"data: {json.dumps({'error': 'message not found'})}\n\n"
        return StreamingResponse(_no_msg(), media_type="text/event-stream")

    # (EVIDENCE-BUDGET FEATURE) Computed once, right after `msg` is
    # resolved, and reused for every get_matched_signals()/
    # merge_matched_and_google_results() call below — mirrors index.py's
    # own non-streaming _complete_message_answer_and_results()/
    # _timeout_fallback_answer() clamp exactly, so a streaming and a
    # non-streaming answer for the same message can never end up with a
    # different evidence budget. Messages with no stored
    # "evidence_required" (every message predating this feature) fall
    # back to MIN_ANALYSIS_EVIDENCE here, then get clamped against
    # MAX_ANALYSIS_EVIDENCE like any other value.
    effective_evidence_limit = min(
        msg.get("evidence_required") or MIN_ANALYSIS_EVIDENCE,
        MAX_ANALYSIS_EVIDENCE,
    )

    # Already generated/cached earlier (via the normal blocking path, or
    # a previous call to this same route) — replay it instead of ever
    # re-calling Claude for it again.
    if msg.get("claude_answer"):
        cached_answer = msg["claude_answer"]

        def _cached():
            # (SSE RECONNECT-LOOP FIX) Sent as the very first line, before any
            # data payload — tells the browser's EventSource to wait 24 hours
            # before attempting any reconnection to this URL, per the SSE
            # spec's "retry:" field. This does not change what data is sent or
            # how the client processes it (the client's own source.close() on
            # "done" is unchanged and still the primary mechanism) — it is a
            # pure safety net against the browser's native auto-reconnect
            # racing ahead of that client-side close() call, which is
            # especially likely for a response this fast (no Claude call
            # needed on this branch).
            yield "retry: 86400000\n\n"
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

    # (WEBSITE INTELLIGENCE) A website-only message (BEHAVIOR 1 in
    # search()) never goes through signal matching at all — it is served
    # entirely from its own stored `website_evidence`, via a dedicated
    # generator. Placed right after the cached-answer replay above and
    # BEFORE the normal get_evidence_with_topup()/`matched` computation
    # below, so this branch never triggers any of that normal-path work.
    if msg.get("website_only"):
        def _website_only_generator():
            # (SSE RECONNECT-LOOP FIX) Same safety net as the _cached()
            # branch above.
            yield "retry: 86400000\n\n"

            # (DUPLICATE-REFRESH GUARD) Same shape as the main
            # event_generator()'s own guard below: if a Claude call for
            # this SAME owner is already in flight (e.g. a reconnected
            # EventSource for the same message), don't start a second,
            # independent build_website_insight_answer() call — wait
            # (bounded by RESPONSE_TIMEOUT) for the in-flight call to
            # finish and cache the answer, then replay it.
            if _is_owner_busy(owner_key):
                wait_deadline = time.time() + RESPONSE_TIMEOUT
                resolved_answer = None
                resolved_results = None
                while time.time() < wait_deadline:
                    try:
                        fresh_chat = get_chat_session(chat_id, owner_key)
                        fresh_msg = next(
                            (m for m in reversed((fresh_chat or {}).get("messages") or []) if m.get("topic_key") == topic_key),
                            None,
                        )
                    except Exception as exc:
                        log.warning(f"Polling for in-flight website-only answer failed for topic_key={topic_key}: {exc}")
                        fresh_msg = None
                    if fresh_msg and fresh_msg.get("claude_answer"):
                        resolved_answer = fresh_msg["claude_answer"]
                        resolved_results = fresh_msg.get("results") or []
                        break
                    if not _is_owner_busy(owner_key):
                        # Owner no longer busy but still no answer saved —
                        # the in-flight call likely failed/cleared without
                        # saving; stop waiting and fall through to running
                        # this request's own call below.
                        break
                    time.sleep(1)

                if resolved_answer is not None:
                    yield f"data: {json.dumps({'delta': resolved_answer})}\n\n"
                    yield f"data: {json.dumps({'done': True, 'results': resolved_results})}\n\n"
                    return
                # else: fall through to the normal path below.

            _set_owner_busy(owner_key)
            try:
                # Fire the SAME Google-fallback pattern used elsewhere in
                # this route (fire-once guard + background thread), so
                # broader Reddit-evidence collection for this topic keeps
                # running in the background even though the website-insight
                # answer itself arrives immediately. Search-progress
                # generation is deliberately NOT triggered on this branch —
                # there's no waiting/loading period here for it to fill.
                if flintel.should_trigger_immediately(msg.get("google_fallback_triggered", False)):
                    mark_google_fallback_triggered(chat_id, owner_key, topic_key)
                    threading.Thread(
                        target=_trigger_google_fallback_search,
                        args=(chat_id, owner_key, msg),
                        daemon=True,
                    ).start()
                    msg["google_fallback_triggered"] = True

                ev = msg.get("website_evidence") or {}
                try:
                    answer = website_intelligence.build_website_insight_answer(
                        url=ev.get("url") or msg["query"],
                        query=msg["query"],
                        structured_evidence=ev.get("structured_evidence"),
                        evidence_quality=ev.get("evidence_quality", "thin"),
                        call_claude_fn=_call_claude,
                    )
                except Exception as exc:
                    log.warning(f"Website-insight answer generation failed for topic_key={topic_key}: {exc}")
                    yield f"data: {json.dumps({'error': 'analysis failed'})}\n\n"
                    return

                answer = (answer or "").strip()

                # (SIMULATED-STREAM FIX, same pattern) Pace the final
                # string out in small pieces to reproduce the live-typing
                # impression.
                if answer:
                    for i in range(0, len(answer), STREAM_CHUNK_CHARS):
                        piece = answer[i:i + STREAM_CHUNK_CHARS]
                        yield f"data: {json.dumps({'delta': piece})}\n\n"
                        if STREAM_CHUNK_DELAY_SECONDS > 0:
                            time.sleep(STREAM_CHUNK_DELAY_SECONDS)

                if answer:
                    try:
                        save_claude_answer_to_chat(chat_id, owner_key, topic_key, answer)
                        save_signal_results_to_chat(chat_id, owner_key, topic_key, [])
                        append_to_chat_summary(chat_id, owner_key, msg["query"], answer)
                    except Exception as exc:
                        log.warning(f"Caching website-only streamed answer failed for topic_key={topic_key}: {exc}")

                yield f"data: {json.dumps({'done': True, 'results': []})}\n\n"
            finally:
                _clear_owner_busy(owner_key)

        return StreamingResponse(_website_only_generator(), media_type="text/event-stream")

    try:
        matched = get_evidence_with_topup(
            chat_id=chat_id,
            owner_key=owner_key,
            topic_key=topic_key,
            keywords=msg.get("keywords", []),
            evidence_required=effective_evidence_limit,
            matcher_fn=get_matched_signals,
            match_phrases=msg.get("match_phrases"),
            targeting_platform=msg.get("targeting_platform", "all"),
            since_days=msg.get("time_window_days"),
            unfiltered=msg.get("unfiltered", False),
        )
    except Exception as exc:
        log.warning(f"Signal matching failed for streaming topic_key={topic_key}: {exc}")
        matched = []

    def event_generator():
        # (GOOGLE-FALLBACK POLLING FIX) This generator reassigns `matched`
        # (during the polling loop below, and again if the
        # RESPONSE_TIMEOUT/Google-stub-fallback branch runs) — `nonlocal`
        # is required so those reassignments update the SAME `matched`
        # from the enclosing stream_answer() scope (the one already set,
        # once, by the initial get_matched_signals() call above) instead
        # of Python treating it as a brand-new local variable for this
        # entire function, which would make the very first `if not
        # matched:` check below raise UnboundLocalError.
        nonlocal matched

        # (DUPLICATE-REFRESH FIX) If this SAME owner already has a
        # Claude call in flight (e.g. the original stream request that
        # opened this SSE connection is still running its
        # analyze_with_claude() call, and the browser reconnected/
        # refreshed and opened a SECOND stream for the exact same
        # topic_key), do NOT start a second, independent
        # analyze_with_claude() call here — that would double-bill
        # Claude and race with the original call over which one's
        # answer gets saved/cached last (last-write-wins on
        # save_claude_answer_to_chat()).
        #
        # Instead, this connection waits (bounded by RESPONSE_TIMEOUT,
        # same ceiling used everywhere else in this route) for the
        # in-flight call to finish and cache the answer, polling
        # get_chat_session() every ~1s. The moment claude_answer shows
        # up on the message (saved by whichever request actually owns
        # the busy flag), this replays it exactly like the existing
        # `claude_answer already truthy` branch above does — same
        # delta/done event shape, so the frontend needs no changes to
        # handle this path.
        #
        # If the in-flight call somehow never finishes within
        # RESPONSE_TIMEOUT (a genuine stall/crash on the owning
        # request), this falls through to running its OWN
        # analyze_with_claude() call below rather than hanging forever —
        # the busy flag itself is also self-healing via
        # BUSY_FLAG_TIMEOUT_SECONDS in _is_owner_busy(), so a truly
        # abandoned flag clears on its own regardless.
        if _is_owner_busy(owner_key):
            wait_deadline = time.time() + RESPONSE_TIMEOUT
            resolved_answer = None
            resolved_results = None
            while time.time() < wait_deadline:
                try:
                    fresh_chat = get_chat_session(chat_id, owner_key)
                    fresh_msg = next(
                        (m for m in reversed((fresh_chat or {}).get("messages") or []) if m.get("topic_key") == topic_key),
                        None,
                    )
                except Exception as exc:
                    log.warning(f"Polling for in-flight answer failed for topic_key={topic_key}: {exc}")
                    fresh_msg = None
                if fresh_msg and fresh_msg.get("claude_answer"):
                    resolved_answer = fresh_msg["claude_answer"]
                    resolved_results = fresh_msg.get("results") or []
                    break
                if not _is_owner_busy(owner_key):
                    # Owner is no longer busy but still no answer saved —
                    # the in-flight call likely failed/cleared without
                    # saving; stop waiting and fall through to running
                    # this request's own call below instead of waiting
                    # out the full deadline for nothing.
                    break
                time.sleep(1)

            if resolved_answer is not None:
                yield "retry: 86400000\n\n"
                yield f"data: {json.dumps({'delta': resolved_answer})}\n\n"
                yield f"data: {json.dumps({'done': True, 'results': resolved_results})}\n\n"
                return
            # else: fall through to the normal path below, which will
            # itself set the busy flag and run its own Claude call —
            # this only happens if the original in-flight call never
            # actually completed/saved anything within the wait window.

        # (PER-USER BUSY LOCK) Set right at the start of the whole
        # generator, cleared in the finally below — covers every exit
        # path (the early error-return, and the normal completion path
        # after the answer is saved) exactly once, so the flag is never
        # left set no matter how this generator ends.
        _set_owner_busy(owner_key)
        try:
            # (SSE RECONNECT-LOOP FIX) Same "retry:" safety net as the
            # _cached() branch above — see that branch's comment for the
            # full rationale. Sent once, immediately, before any other
            # SSE payload in this stream.
            yield "retry: 86400000\n\n"

            # (IMMEDIATE PARALLEL TRIGGERING) Both the Google search and
            # the search-progress UI generation now fire in PARALLEL with
            # this route's own initial get_matched_signals() call made
            # above — right away, on this very first check — instead of
            # waiting for elapsed time to reach any threshold. Each still
            # only ever fires ONCE per message (fire-once guards below,
            # marked synchronously before dispatch, unchanged). This runs
            # UNCONDITIONALLY, whether or not `matched` already has
            # something, mirroring index.py's own should_trigger_
            # immediately() usage in _fill_in_message_outputs().
            search_progress_holder = {}
            search_progress_sent = False

            def _generate_and_store_search_progress(holder, chat_id, owner_key, msg):
                # (RACE FIX) search_progress_generated is already
                # marked True synchronously by the caller below,
                # before this thread starts — this function's only
                # job is to actually generate and save the content,
                # exactly mirroring _trigger_google_fallback_search()'s
                # own post-race-fix shape.
                try:
                    content = flintel.generate_search_progress_content(
                        msg.get("query"),
                        msg.get("keywords", []),
                        msg.get("targeting_platform"),
                        _call_claude,
                    )
                    if content:
                        holder["content"] = content
                except Exception as exc:
                    log.warning(f"Search-progress generation failed for topic_key={topic_key}: {exc}")

            if flintel.should_trigger_immediately(msg.get("google_fallback_triggered", False)):
                mark_google_fallback_triggered(chat_id, owner_key, topic_key)
                threading.Thread(
                    target=_trigger_google_fallback_search,
                    args=(chat_id, owner_key, msg),
                    daemon=True,
                ).start()
                msg["google_fallback_triggered"] = True

            if flintel.should_trigger_immediately(msg.get("search_progress_generated", False)):
                mark_search_progress_generated(chat_id, owner_key, topic_key)
                threading.Thread(
                    target=_generate_and_store_search_progress,
                    args=(search_progress_holder, chat_id, owner_key, msg),
                    daemon=True,
                ).start()
                msg["search_progress_generated"] = True

            # (PROGRESS-PERCENTAGE STREAMING) Emitted once, immediately,
            # right here — before the bounded search-progress wait and
            # before the polling loop even starts — so the bar shows 0%
            # right away instead of only appearing after the first 2s
            # poll tick. Skipped when `matched` already has something,
            # since the `if not matched:` polling loop right below never
            # runs in that case — there is nothing to show progress for.
            if not matched:
                yield f"data: {json.dumps({'progress_percent': 0})}\n\n"

            # (SEARCH-PROGRESS UI FIX) Runs UNCONDITIONALLY — whether or
            # not `matched` was already found — giving the background
            # thread started just above a short, bounded window to
            # finish before this route moves on to answering. Before
            # this fix, the check-and-yield for search_progress lived
            # ONLY inside the `if not matched:` polling loop below, so
            # once Google-fallback/search-progress started firing
            # immediately (in parallel) instead of at the old 40s mark,
            # a message whose signals resolved quickly would skip that
            # loop entirely — the search-progress content, even once
            # generated by the thread, was never checked, never yielded
            # as an SSE event, and never saved, so the richer loading UI
            # could never actually be seen. This bounded wait (checked
            # every 0.3s, up to ~3s total) gives it a real chance to
            # complete and be shown even when signals are already
            # available, without meaningfully delaying the fast-match
            # case. The longer `if not matched:` polling loop below still
            # ALSO checks on each of its own iterations (every 2s,
            # spanning up to RESPONSE_TIMEOUT) — search_progress_sent
            # prevents this from ever being sent twice.
            search_progress_wait_deadline = time.time() + 3
            while not search_progress_sent and time.time() < search_progress_wait_deadline:
                if search_progress_holder.get("content"):
                    try:
                        save_search_progress_to_chat(chat_id, owner_key, topic_key, search_progress_holder["content"])
                    except Exception as exc:
                        log.warning(f"Saving search_progress failed for topic_key={topic_key}: {exc}")
                    yield f"data: {json.dumps({'search_progress': search_progress_holder['content']})}\n\n"
                    search_progress_sent = True
                    break
                time.sleep(0.3)

            # (GOOGLE-FALLBACK POLLING FIX) `matched` here starts as the
            # ONE initial get_matched_signals() call already made above,
            # outside this generator. If that already found something,
            # the polling loop below never runs at all — zero change to
            # the already-working "signals matched right away" case.
            # Only if it found nothing does this poll, re-checking
            # get_matched_signals() every ~2s until either something
            # appears or RESPONSE_TIMEOUT is reached.
            tier3_triggered = False
            if not matched:
                while True:
                    elapsed = _elapsed_seconds(msg.get("requested_at"))

                    # (PROGRESS-PERCENTAGE STREAMING) Placed BEFORE the
                    # search_progress check and BEFORE the RESPONSE_TIMEOUT
                    # break check below, so the numeric percentage and the
                    # richer intro/outro/checklist text can both be present
                    # in the same polling iteration without one blocking
                    # the other. trigger_seconds=0 means this starts
                    # counting from the moment the message was requested
                    # (0%), reaching 100% exactly when elapsed >=
                    # RESPONSE_TIMEOUT — the SAME threshold that sets
                    # tier3_triggered = True a few lines below. Rides along
                    # on this loop's own existing ~2s cadence (time.sleep(2)
                    # at the bottom) — no separate, faster timer needed.
                    progress_percent = flintel.calculate_search_progress_percent(
                        elapsed, trigger_seconds=0, timeout_seconds=RESPONSE_TIMEOUT
                    )
                    yield f"data: {json.dumps({'progress_percent': progress_percent})}\n\n"

                    # (SEARCH-PROGRESS UI) Once the background thread above
                    # has filled in a result, save it (so a later
                    # non-streaming page view sees it too) and yield it to
                    # the browser exactly once, as its own SSE event type —
                    # the client's EventSource handler renders this
                    # alongside the live elapsed-time-based progress bar.
                    if not search_progress_sent and search_progress_holder.get("content"):
                        try:
                            save_search_progress_to_chat(chat_id, owner_key, topic_key, search_progress_holder["content"])
                        except Exception as exc:
                            log.warning(f"Saving search_progress failed for topic_key={topic_key}: {exc}")
                        yield f"data: {json.dumps({'search_progress': search_progress_holder['content']})}\n\n"
                        search_progress_sent = True

                    # (RESPONSE_TIMEOUT'S NEW ROLE) Still the ultimate
                    # ceiling, but since Google search now starts
                    # immediately above (rather than at the old 40s mark),
                    # this loop typically exits much sooner now, once
                    # BOTH the signals query and the Google search attempt
                    # have resolved — this is expected, not a bug.
                    if elapsed >= RESPONSE_TIMEOUT:
                        tier3_triggered = True
                        break

                    time.sleep(2)

                    try:
                        matched = get_evidence_with_topup(
                            chat_id=chat_id,
                            owner_key=owner_key,
                            topic_key=topic_key,
                            keywords=msg.get("keywords", []),
                            evidence_required=effective_evidence_limit,
                            matcher_fn=get_matched_signals,
                            match_phrases=msg.get("match_phrases"),
                            targeting_platform=msg.get("targeting_platform", "all"),
                            since_days=msg.get("time_window_days"),
                            unfiltered=msg.get("unfiltered", False),
                        )
                    except Exception as exc:
                        log.warning(f"Signal matching failed while polling for streaming topic_key={topic_key}: {exc}")
                        matched = []

                    if matched:
                        break

            # (MERGE BEFORE ANSWERING) Pulls in whatever Google-search
            # stub results exist right now — combined with `matched` via
            # merge_matched_and_google_results(), capped at
            # effective_evidence_limit total — instead of Google only
            # ever being a last-resort replacement used when signals
            # were empty.
            try:
                stub_docs = google_search.get_stub_results_for_keywords(
                    google_posts_collection, msg.get("keywords", []))
            except Exception as exc:
                log.warning(f"Fetching Google-fallback stubs failed for topic_key={topic_key}: {exc}")
                stub_docs = []
            google_results = flintel.format_google_stub_results(stub_docs)
            merged_pool = flintel.merge_matched_and_google_results(
                matched, google_results, max_total=effective_evidence_limit
            )

            # (SIMULATED-STREAM FIX) Step 1: get the COMPLETE answer first,
            # via the same blocking function every other answer path in this
            # file already uses — no raw live Claude tokens are sent to the
            # browser anymore.
            try:
                # (BUG FIX — DON'T RE-SUGGEST A DECLINED ALTERNATIVE) Same
                # continuity context as the non-streaming path in index.py's
                # _complete_message_answer_and_results() — `chat` was already
                # fetched above in this route, so no extra Mongo lookup is
                # needed here.
                chat_summary_for_answer = (chat or {}).get("summary") or ""
                continuity_ctx = None
                if chat_summary_for_answer:
                    continuity_ctx = (
                        "Conversation so far (auto-summarized, may be empty) — "
                        "see the CONVERSATION CONTINUITY instruction above for "
                        "how to use this:\n" + chat_summary_for_answer
                    )

                # (CLOSEST-MATCHES TIER-3 REMOVED — mirrors logics.py's
                # own _timeout_fallback_answer() simplification, CHANGE C)
                # The old "if not merged_pool and tier3_triggered: ...
                # loose_candidates / near_match_confidence / near_match_
                # offer ... else: ..." branching is gone. Whatever
                # merged_pool holds right now — even a handful of posts,
                # even zero — is handed straight to analyze_with_claude()
                # below, unconditionally. Zero posts still lands in
                # analyze_with_claude()'s own existing "no posts yet"
                # honest branch (untouched) — that IS the correct
                # "genuinely found nothing" outcome, never a fabricated
                # "closest match" offer. `tier3_triggered` is still set
                # above (still useful for logging/observability) but no
                # longer gates which code path runs here.
                extra_ctx_parts = []
                if msg.get("unfiltered"):
                    extra_ctx_parts.append(
                        flintel.build_unfiltered_answer_context(
                            msg["query"], msg.get("time_window_days")
                        )
                    )
                if msg.get("website_note"):
                    extra_ctx_parts.append(msg["website_note"])
                if continuity_ctx:
                    extra_ctx_parts.append(continuity_ctx)
                # (MERGE BEFORE ANSWERING) Tells Claude it has a mix
                # of grounded signals and discovery-only Google posts
                # in the same batch.
                if google_results:
                    extra_ctx_parts.append(
                        flintel.build_combined_source_context(len(matched), len(google_results))
                    )
                extra_ctx = "\n\n".join(extra_ctx_parts) if extra_ctx_parts else None
                full_answer = analyze_with_claude(msg["query"], merged_pool, extra_context=extra_ctx)
                full_answer = _patch_post_urls_into_answer((full_answer or "").strip(), merged_pool) if full_answer else full_answer
                if full_answer and msg.get("website_context"):
                    full_answer = _inject_website_context_into_answer(full_answer, msg["website_context"])
                full_answer, results_to_save = (
                    _finalize_answer_and_results(full_answer, merged_pool, seed=msg.get("query", ""))
                    if full_answer else (full_answer, merged_pool)
                )
            except Exception as exc:
                log.warning(f"Streaming Claude analysis failed for topic_key={topic_key}: {exc}")
                yield f"data: {json.dumps({'error': 'analysis failed'})}\n\n"
                return

            full_answer = (full_answer or "").strip()

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
                    append_to_chat_summary(chat_id, owner_key, msg["query"], full_answer, keywords=msg.get("keywords"))
                except Exception as exc:
                    log.warning(f"Caching streamed answer failed for topic_key={topic_key}: {exc}")

                # (RESULTS-RECOMPUTE FIX, preserved) Save unconditionally,
                # not gated on `if matched:` — a genuinely empty result
                # set still needs its final `[]` persisted, or `results`
                # stays uncomputed forever. `results_to_save` was already
                # decided above.
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
