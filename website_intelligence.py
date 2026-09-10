"""
FLINTEL — WEBSITE INTELLIGENCE (URL-HANDLING REFINEMENTS)
============================================================================
Self-contained module. Nothing here imports from or modifies index.py or
flintel.py — both of those files may import FROM this file, never the
other way around (the exact same one-way-dependency pattern flintel.py
already uses relative to index.py).

WHAT THIS FILE IS FOR:
index.py already knows how to detect a URL inside a user's message
(_extract_first_url), fetch that URL's plain text (fetch_website_text),
and turn website content + a stated request into a keyword list
(extract_keywords_from_website). This file does NOT reimplement any of
that — it is handed whatever raw inputs index.py already has (the
already-fetched website_text, the user's query string, the detected url)
and returns plain data (strings / dicts) for index.py to act on. It never
touches Mongo, never registers a FastAPI route, and never writes to chat
storage itself — every "reply" this file builds is just a string that
index.py hands to its own existing add_chat_message_to_chat().

This file holds THREE small, independent pieces of logic:

  1. SHORT WEBSITE SUMMARY GENERATOR — summarize_website() turns raw
     website text into a 2-3 sentence plain-language summary of what the
     business/site appears to offer, plus build_url_only_reply(), which
     wraps that summary in a natural chat-style invitation for the user
     to name what they'd like to look into.

  2. TOPIC-VS-WEBSITE CONNECTION CHECK — check_topic_matches_website()
     reads the user's stated topic/request TOGETHER WITH the website's
     content in a single Claude call and returns both a match verdict
     ("does the stated topic actually connect to this website?") AND the
     keyword list for that topic in one shot — this is a genuine
     extension of the same idea as index.py's own
     extract_keywords_from_website(), not a second, separate keyword
     call, so cost/latency for the "topic + URL together" case stays the
     same as it is today. build_topic_mismatch_reply() turns a
     "doesn't match" verdict into a natural, honest chat reply.

  3. "IS THIS A REAL REQUEST, OR JUST A BARE URL?" HELPER —
     has_request_shaped_language() is a pure-Python (no Claude call, no
     network), instant heuristic that strips the detected URL and any
     trivial filler wording out of a message and checks whether anything
     meaningful is actually left. This is explicitly a LAST-RESORT/
     backup signal — index.py's own router classification
     (intent="search" / "chat" / "clarify" / "blocked") is the PRIMARY
     signal for whether a message is a genuine request versus a bare
     URL drop; this helper exists purely so index.py has an inexpensive
     fallback check available for the case where the router's own
     classification is missing or ambiguous (e.g. the router call itself
     failed and the existing safety net defaulted to intent="search").

GENERICITY: nothing in this file assumes, hardcodes, or pattern-matches
against any specific industry, product category, or business type.
Every Claude prompt below reasons fresh from whatever website text and
query text it is actually given — any example mentioned in a comment or
docstring here is illustrative only, never a fixed category this module
is limited to.

CLAUDE CALL PATTERN: this file does NOT talk to the Anthropic API
directly and does NOT reimplement any HTTP/SDK plumbing. Every function
that needs a Claude call accepts a `call_claude_fn` parameter — a
callable with the same signature as index.py's own _call_claude(system_
prompt, user_message, max_tokens=None) -> str — and index.py is expected
to pass its own existing _call_claude function in. This keeps this
file's only dependency direction "accepts what it's given," exactly like
flintel.py's existing get_unfiltered_matched_signals() accepts a
signals_collection rather than connecting to Mongo itself.

DEGRADE GRACEFULLY, EVERYWHERE: every function in this file returns None
(or an empty/safe default, for the pure-Python piece) on any failure —
a missing/empty input, a call_claude_fn exception, unparseable JSON, or
output that doesn't have the shape expected. Nothing in this file ever
raises past its own boundary; every caller in index.py can always fall
back to whatever behavior already exists today.
"""

import os
import re
import json


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — small, self-contained constants with sensible defaults, mirroring
# (never importing) the spirit of index.py's own equivalents, the exact
# same pattern flintel.py already uses for MAX_POSTS_PER_PLATFORM etc.
# ─────────────────────────────────────────────────────────────────────────────

# Piece 2's keyword cap — mirrors the SPIRIT of index.py's own
# MAX_WEBSITE_KEYWORDS (same default value), kept as this module's own,
# separately-configurable constant rather than importing index.py's.
MAX_WEBSITE_KEYWORDS = int(os.getenv("MAX_WEBSITE_KEYWORDS", "20"))

# Max tokens for the Piece 1 summary call — deliberately small since the
# output is meant to be a short 2-3 sentence summary, not a report.
WEBSITE_SUMMARY_MAX_TOKENS = int(os.getenv("WEBSITE_SUMMARY_MAX_TOKENS", "300"))

# Max tokens for the new structured (sectioned/bulleted) summary call —
# a bit higher than the flat summary's since this produces more content
# (an overview line plus several sections of bullets).
WEBSITE_STRUCTURED_SUMMARY_MAX_TOKENS = int(os.getenv("WEBSITE_STRUCTURED_SUMMARY_MAX_TOKENS", "500"))

# Max tokens for the Piece 2 topic-match + keyword call — similar
# ballpark to index.py's own CLAUDE_WEBSITE_KEYWORD_MAX_TOKENS, since
# this call produces a similarly-shaped JSON payload (keywords list) plus
# one extra boolean field.
TOPIC_MATCH_MAX_TOKENS = int(os.getenv("TOPIC_MATCH_MAX_TOKENS", "400"))

# (Piece 3) Below this many leftover characters (after stripping the URL
# and known filler phrasing), a message is treated as "just a bare URL
# drop" rather than a real request — short enough that a stray leftover
# punctuation mark or a one-word filler that wasn't in the known list
# can't accidentally flip this to True.
_MIN_MEANINGFUL_CHARS = 3


# ─────────────────────────────────────────────────────────────────────────────
# SHARED JSON-PARSING HELPER — same fence-stripping tolerance already
# used by every JSON-producing Claude call in index.py/flintel.py
# (_parse_router_json, extract_keywords_from_website, etc.), duplicated
# here rather than imported since this file's only dependency direction
# is "accepts what it's given," never "imports from index.py."
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json_object(raw: str):
    """Best-effort parse of a Claude text response into a JSON dict.
    Strips ```json fences if present, same tolerance already used
    elsewhere in this product. Returns None (never raises) on anything
    that isn't a well-formed JSON object."""
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
    return data if isinstance(data, dict) else None


def _clean_keyword_list(raw_keywords, max_keywords: int):
    """Shared cleaning/de-duplication logic for a Claude-returned keyword
    list — trims, drops non-strings/empties, de-dupes case-insensitively,
    caps at max_keywords. Mirrors the exact same cleaning rules already
    applied to keyword lists elsewhere in this product (index.py's
    _parse_router_json / extract_keywords_from_website). Returns None if
    nothing usable survives, so callers can treat "no keywords" uniformly
    with every other keyword source in this product."""
    if not isinstance(raw_keywords, list):
        return None
    cleaned = []
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
        cleaned.append(kw_clean)
        if len(cleaned) >= max_keywords:
            break
    return cleaned or None


# ─────────────────────────────────────────────────────────────────────────────
# ROTATING NATURAL-LANGUAGE PHRASING — same cheap, deterministic-hash
# rotation approach flintel.py's own _pick_invite_line() already uses, so
# a reply doesn't read as the exact same canned sentence every single
# time, without needing a random source or any stored state.
# ─────────────────────────────────────────────────────────────────────────────

def _pick_variant(variants: list, seed: str = "") -> str:
    """Deterministically rotates through `variants` based on a cheap hash
    of `seed` (e.g. the user's own query text) — same wording never
    repeats mechanically every single call, without needing randomness
    or stored state. Falls back to the first variant if seed is empty."""
    if not variants:
        return ""
    if not seed:
        return variants[0]
    idx = sum(ord(c) for c in seed) % len(variants)
    return variants[idx]


# ─────────────────────────────────────────────────────────────────────────────
# PIECE 1 — SHORT WEBSITE SUMMARY GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

WEBSITE_SUMMARY_SYSTEM_PROMPT = """
You are the website-summary brain inside Flintel, a social-listening
platform. You are given the plain text content of a website a user just
shared. Read it and write a SHORT summary — 2 to 3 sentences, plain
language, no corporate hedging or filler — of what the business/site
appears to offer: what it does, who it's likely for, and anything
distinctive that stands out from the text.

Reason freshly from the actual content you were given — never assume or
default to any particular industry or category. If the content is too
thin, broken, or generic to say anything confident, say so plainly and
briefly instead of guessing or padding.

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{"summary": "<2-3 sentence plain-language summary>"}
"""


def summarize_website(website_text: str, call_claude_fn) -> str:
    """Makes ONE cheap Claude call reading the website's plain text and
    returns a short (2-3 sentence), plain-language summary of what the
    business/site appears to offer.

    `call_claude_fn` must have the same signature as index.py's own
    _call_claude(system_prompt, user_message, max_tokens=None) -> str —
    this module never talks to the Anthropic API directly.

    Returns None if website_text is empty, the call fails outright, or
    the response is unparseable / has no usable "summary" string — never
    raises past this function. Callers should treat None exactly like
    any other "couldn't determine this" outcome elsewhere in this
    product: fall back to existing behavior."""
    if not website_text or not isinstance(website_text, str):
        return None
    if not callable(call_claude_fn):
        return None

    user_message = f"Website content (plain text):\n{website_text}"
    try:
        raw = call_claude_fn(WEBSITE_SUMMARY_SYSTEM_PROMPT, user_message, max_tokens=WEBSITE_SUMMARY_MAX_TOKENS)
    except Exception:
        return None

    data = _parse_json_object(raw)
    if not data:
        return None
    summary = data.get("summary")
    if not isinstance(summary, str):
        return None
    summary = summary.strip()
    return summary or None


_URL_ONLY_REPLY_VARIANTS = [
    "Here's what I found looking at your site: {summary} What would you like me to pull related posts about — a specific angle, problem, or audience?",
    "Took a look at your website — {summary} Let me know what you'd like me to search for (a topic, a common complaint, a specific audience) and I'll pull real posts around it.",
    "Checked out your site: {summary} Tell me what angle you want covered — a product, a problem your customers have, or anything else — and I'll get you related posts.",
    "Yeh raha aap ki website ka khulasa: {summary} Ab bataein aap kis cheez ke baare mein posts dekhna chahte hain — koi khaas topic, problem, ya audience?",
]


def build_url_only_reply(summary: str, query_seed: str = "") -> str:
    """Builds the natural chat reply text for the case where a user's
    message was essentially just a bare website link (see
    has_request_shaped_language() below) — combines the short summary
    from summarize_website() with a professional, varied invitation for
    the user to say what they'd actually like to look into.

    `query_seed`, if given (e.g. the user's own raw message text), is
    used purely to deterministically rotate which phrasing variant is
    used (see _pick_variant()) so the same sentence doesn't repeat
    mechanically every time — it has no other effect on the output.

    Returns a plain string ready to hand straight to index.py's own
    add_chat_message_to_chat(). If `summary` is falsy, returns a
    shorter, honest fallback that doesn't pretend to have read the site
    successfully, rather than fabricating a summary."""
    if not summary or not isinstance(summary, str):
        return (
            "I took a look at your website but couldn't pull together a "
            "confident summary from it — let me know what topic, brand, "
            "or problem you'd like me to search posts about instead."
        )
    template = _pick_variant(_URL_ONLY_REPLY_VARIANTS, seed=query_seed)
    return template.format(summary=summary.strip())


# ─────────────────────────────────────────────────────────────────────────────
# PIECE 1B — STRUCTURED (SECTIONED, BULLETED) WEBSITE SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
# Separate from the flat summarize_website()/build_url_only_reply() pair
# above, which stays completely unchanged and is still what BEHAVIOR 1
# (a bare URL, no real ask) uses. This is only ever called for the
# URL + real-ask flow, so Flintel can show the user a clean, professional
# breakdown of what it understood about their own site, above the
# matched-posts answer, in the same reply.
# ─────────────────────────────────────────────────────────────────────────────

WEBSITE_STRUCTURED_SUMMARY_SYSTEM_PROMPT = """
You are the website-summary brain inside Flintel, a social-listening
platform. You are given the plain text content of a website a user just
shared. Read it and produce a CLEAN, SECTIONED breakdown of what the
business/site/individual actually offers — not a flat paragraph.

Reason freshly from the actual content you were given — never assume or
default to any particular industry or category, and never force the same
fixed set of section titles onto every website. Produce 2 to 4 sections
total, choosing whichever section titles genuinely fit THIS site's
content. Natural groupings often look something like "What they offer",
"Business signals", or "Notable things" — treat these as loose
inspiration for the kind of grouping that tends to work, not a required
list; a different site may call for entirely different section titles.

Each section should have 2 to 5 short bullets — plain language, no
fluff, no marketing tone. Stay honest and slightly skeptical where the
content actually warrants it: if something on the site reads like a
pressure tactic, an inflated claim, or an odd/notable pattern, it's fine
to note that plainly and factually — never write in a promotional voice
on the business's behalf.

If the content is too thin, broken, or generic to say anything confident
across multiple sections, keep the overview honest about that and
produce however few genuinely-supportable sections make sense (including
possibly just one) rather than padding for the sake of hitting a count.

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{"overview": "<1-2 sentence plain-language opening line>", "sections": [{"title": "<short section heading>", "bullets": ["<bullet 1>", "<bullet 2>"]}]}
"""


def summarize_website_structured(website_text: str, call_claude_fn):
    """Makes ONE Claude call reading the website's plain text and returns
    a structured (sectioned, bulleted) breakdown as a Python dict:

        {"overview": "<1-2 sentence opening line>",
         "sections": [{"title": "...", "bullets": ["...", ...]}, ...]}

    `call_claude_fn` must have the same signature as index.py's own
    _call_claude(system_prompt, user_message, max_tokens=None) -> str —
    this module never talks to the Anthropic API directly, same
    injection pattern summarize_website() already uses.

    Degrades exactly like every other function in this file: empty/
    missing website_text -> None; call_claude_fn exception -> None;
    unparseable JSON -> None. Any section that doesn't have a string
    title and a list of string bullets is silently dropped rather than
    failing the whole call. If nothing usable survives cleaning (no
    overview AND no sections), returns None so the caller can fall back,
    same "couldn't determine this" contract used everywhere else here.
    Never raises past this function."""
    if not website_text or not isinstance(website_text, str):
        return None
    if not callable(call_claude_fn):
        return None

    user_message = f"Website content (plain text):\n{website_text}"
    try:
        raw = call_claude_fn(
            WEBSITE_STRUCTURED_SUMMARY_SYSTEM_PROMPT,
            user_message,
            max_tokens=WEBSITE_STRUCTURED_SUMMARY_MAX_TOKENS,
        )
    except Exception:
        return None

    data = _parse_json_object(raw)
    if not data:
        return None

    overview = data.get("overview")
    overview = overview.strip() if isinstance(overview, str) else ""

    raw_sections = data.get("sections")
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

    if not overview and not cleaned_sections:
        return None

    return {"overview": overview, "sections": cleaned_sections}


def format_structured_summary_for_answer(structured):
    """Pure Python, no Claude call. Takes the dict returned by
    summarize_website_structured() (or None) and returns a JSON-
    serializable dict ready to be embedded as a new top-level field
    inside index.py's CLAUDE_ANALYSIS_SYSTEM_PROMPT JSON answer:

        {"overview": "<...>", "sections": [{"title": "...", "bullets": [...]}]}

    Returns None if `structured` is None or has nothing usable in it, so
    the caller can simply omit the field rather than embedding an empty
    object. index.py never needs to know this module's internal shape
    details beyond calling this."""
    if not structured or not isinstance(structured, dict):
        return None
    overview = structured.get("overview")
    overview = overview.strip() if isinstance(overview, str) else ""
    sections = structured.get("sections")
    sections = sections if isinstance(sections, list) else []
    if not overview and not sections:
        return None
    return {"overview": overview, "sections": sections}


# ─────────────────────────────────────────────────────────────────────────────
# PIECE 2 — TOPIC-VS-WEBSITE CONNECTION CHECK
# ─────────────────────────────────────────────────────────────────────────────

TOPIC_MATCH_SYSTEM_PROMPT = """
You are the topic-vs-website connection brain inside Flintel, a
social-listening platform. A user has shared BOTH a stated topic/request
AND a link to a website, in the same message. Your job is two things at
once, in a single pass:

1. Decide whether the stated topic genuinely connects to what this
   website actually offers, based only on the website's own content —
   read for real substance, not superficial keyword overlap. A topic
   can still "match" even if it's phrased very differently from the
   site's own wording, as long as it's genuinely about what the site
   offers, a problem the site's offering would plausibly solve, or an
   audience/angle the site is clearly relevant to. Reason freshly from
   the actual content and the actual stated topic every time — never
   assume or default to any particular industry or category.

2. If (and only if) the topic DOES match, ALSO produce the keyword list
   Flintel's existing (unchanged) matching code will use to search
   Reddit/X/LinkedIn/Facebook — up to 20 short, natural keywords/phrases
   that could plausibly appear verbatim, or as a close natural
   substring, inside a real post's title or text. Bias these toward the
   user's STATED topic/angle (combined with what the website confirms
   about the business), not just generic keywords about the site as a
   whole. Never include meta wording like "reddit", "posts", "show me",
   "website", "today", etc. If the topic does NOT match, "keywords" must
   be an empty list — do not generate keywords for an unrelated topic
   just because the website itself might support some other keywords.

Respond with STRICT JSON ONLY — no markdown code fences, no preamble, no
text outside the JSON object — in exactly this shape:
{"topic_matches_website": true, "keywords": ["<keyword1>", "<keyword2>"]}
or, when the topic does not connect to the website:
{"topic_matches_website": false, "keywords": []}
"""


def check_topic_matches_website(query: str, url: str, website_text: str, call_claude_fn) -> dict:
    """Makes ONE Claude call reading BOTH the user's stated topic/request
    text AND the website's plain text content together, and returns a
    dict of the shape:
        {"topic_matches_website": bool, "keywords": [<str>, ...]}

    This is a genuine extension of the same idea as index.py's existing
    extract_keywords_from_website() — for the case where the user
    supplied a stated topic ALONGSIDE a URL, this single call produces
    BOTH the match verdict AND the keyword list at once, so cost/latency
    for that combined case stays the same as calling
    extract_keywords_from_website() alone would have been — it does not
    add a second Claude round trip on top of it.

    `call_claude_fn` must have the same signature as index.py's own
    _call_claude(system_prompt, user_message, max_tokens=None) -> str.

    Returns None if `query` or `website_text` is empty, the call fails
    outright, or the response is unparseable / missing a usable boolean
    "topic_matches_website" field — callers MUST treat None as "couldn't
    determine this, proceed as if this check doesn't exist" (i.e. fall
    back to whatever the existing pipeline would otherwise do), never as
    a false verdict either way.

    When parsing succeeds, "keywords" is always a list (possibly empty)
    — never None — cleaned/de-duplicated/capped at MAX_WEBSITE_KEYWORDS
    the same way every other keyword list in this product already is.
    A True verdict with no usable keywords still returns
    "keywords": [] rather than None, so callers can distinguish "we
    successfully determined this doesn't connect" (use this dict) from
    "we couldn't determine anything at all" (dict itself is None)."""
    if not query or not isinstance(query, str):
        return None
    if not website_text or not isinstance(website_text, str):
        return None
    if not callable(call_claude_fn):
        return None

    user_message = (
        f"User's stated topic/request: {query}\n\n"
        f"Website URL: {url or '(not provided)'}\n\n"
        f"Website content (plain text):\n{website_text}"
    )
    try:
        raw = call_claude_fn(TOPIC_MATCH_SYSTEM_PROMPT, user_message, max_tokens=TOPIC_MATCH_MAX_TOKENS)
    except Exception:
        return None

    data = _parse_json_object(raw)
    if not data:
        return None

    verdict = data.get("topic_matches_website")
    if not isinstance(verdict, bool):
        return None

    keywords = _clean_keyword_list(data.get("keywords"), MAX_WEBSITE_KEYWORDS) or []

    return {"topic_matches_website": verdict, "keywords": keywords}


_TOPIC_MISMATCH_REPLY_VARIANTS = [
    "I checked your website, and \"{query}\" doesn't really seem to connect to what it offers. {hint}Want me to pull posts related to what your site actually does instead?",
    "Looked at your site for this one — \"{query}\" doesn't look like it lines up with what's actually on there. {hint}I can search around what your website does offer instead, if that helps.",
    "Just checked your website against \"{query}\", and they don't seem related. {hint}Happy to search based on what your site actually offers instead — just say the word.",
    "Maine aap ki website check ki, lekin \"{query}\" us se connected nahi lag raha. {hint}Agar chahein to main us cheez ke baare mein posts nikal sakta hoon jo aap ki website actually offer karti hai.",
]


def build_topic_mismatch_reply(query: str, website_summary_or_text_hint: str = "") -> str:
    """Builds the natural chat reply text for the case where
    check_topic_matches_website() came back with "topic_matches_website":
    False — tells the user plainly that their website was checked, that
    the stated topic doesn't appear to connect to it, and offers to pull
    posts related to what the website actually does instead.

    `website_summary_or_text_hint`, if given (e.g. the short summary from
    summarize_website(), or any other short descriptive hint), is folded
    in as one extra grounding sentence so the reply doesn't sound like a
    generic template; safe to omit — the reply still reads naturally
    without it.

    Returns a plain string ready to hand straight to index.py's own
    add_chat_message_to_chat(). Never raises — a missing/odd `query`
    simply falls back to a generic "your request" phrasing rather than
    producing a broken sentence."""
    query_text = (query or "").strip() or "your request"
    hint = ""
    if website_summary_or_text_hint and isinstance(website_summary_or_text_hint, str):
        hint_text = website_summary_or_text_hint.strip()
        if hint_text:
            hint = f"From what I can tell, your site is about: {hint_text} "
    template = _pick_variant(_TOPIC_MISMATCH_REPLY_VARIANTS, seed=query_text)
    return template.format(query=query_text, hint=hint)


# ─────────────────────────────────────────────────────────────────────────────
# PIECE 3 — "IS THIS A REAL REQUEST, OR JUST A BARE URL?" HELPER
# ─────────────────────────────────────────────────────────────────────────────

# Trivial filler phrasing that, on its own, means "here's my link" and
# nothing more — NOT a real stated request/topic. Deliberately generic
# (no industry/category wording of any kind) — purely pointing-at-the-
# link phrasing in a few common languages/registers already used
# elsewhere in this product's own example messages (English, Urdu/Hindi
# romanized). Matched case-insensitively as substrings after the URL
# itself has already been stripped out.
_FILLER_PHRASES = [
    "yeh meri website hai",
    "yeh meri site hai",
    "meri website hai",
    "meri site hai",
    "yeh raha mera website",
    "yeh raha mera link",
    "here is my website",
    "here's my website",
    "here is my site",
    "here's my site",
    "this is my website",
    "this is my site",
    "my website is",
    "my site is",
    "my website link",
    "my site link",
    "check my website",
    "check my site",
    "website link",
    "site link",
    "website:",
    "site:",
    "link:",
]

# Punctuation-only / whitespace-only leftovers should never count as
# "meaningful" — stripped out entirely before the length check.
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def has_request_shaped_language(text: str, url: str) -> bool:
    """Pure-Python (no Claude call, no network), instant heuristic: strips
    the given `url` out of `text`, strips any known trivial filler
    phrasing (see _FILLER_PHRASES — purely "here's my link"-style
    wording, nothing industry- or category-specific), strips punctuation,
    and checks whether anything meaningful is actually left over.

    Returns True if there's real request-shaped language left (i.e. the
    user said something beyond just dropping a link) — False if the
    message is, in substance, just a bare URL (optionally wrapped in
    trivial "here's my site" filler).

    THIS IS A LAST-RESORT / BACKUP SIGNAL ONLY. index.py's own router
    classification (intent="search" vs "chat" vs "clarify" vs "blocked")
    is the PRIMARY signal for whether a message is a genuine request or
    just a bare URL drop — this pure-Python helper exists purely as an
    inexpensive fallback check index.py MAY optionally use when the
    router's own classification is missing or ambiguous (e.g. the router
    call itself failed and this product's existing safety net defaulted
    to intent="search" without any real classification having happened).

    Never raises: a missing/empty `text` is treated as "no request-shaped
    language" (False), and a missing/empty `url` simply means nothing is
    stripped for the URL step (the rest of the heuristic still runs
    normally on the full text)."""
    if not text or not isinstance(text, str):
        return False

    remaining = text

    if url and isinstance(url, str):
        remaining = remaining.replace(url, " ")

    remaining_lower = remaining.lower()
    for phrase in _FILLER_PHRASES:
        remaining_lower = remaining_lower.replace(phrase, " ")

    # Strip punctuation, then collapse whitespace, then measure what's
    # actually left — a leftover of just spaces/punctuation/emoji-less
    # noise never counts as a real request.
    remaining_clean = _PUNCTUATION_RE.sub(" ", remaining_lower)
    remaining_clean = _WHITESPACE_RE.sub("", remaining_clean)

    return len(remaining_clean) >= _MIN_MEANINGFUL_CHARS
