"""Live Reddit web-search links: a SEPARATE, UNVERIFIED, on-demand feature.

What this is
    After a search has been answered, a signed-in user's page may ask (POST /chat/{id}/live-links) for a handful of
    Reddit LINKS found by one OpenAI Responses-API `web_search` call restricted to reddit.com. The links are shown in their
    own section, labelled "Live web-search links - unverified".

What this is NOT
    * It never touches the answer, `results`, the historical evidence, the LLM context or answer-integrity processing.
      Nothing in this module is imported by logics.py, index.py, answer_integrity.py, flintel.py or google.py.
    * A link is only a URL (and a title when the API returned one). No post text, author, date, votes or any other
      metadata is invented; the model's own prose is discarded. Post dates and recency are NOT known.
    * No credit is reserved or charged, no price is assumed. Usage is recorded as the API reported it.

Safety design (see also the tests)
    * Inert unless REDDIT_LIVE_LINKS_ENABLED is true AND both daily caps are valid positive integers at or below a fixed
      ceiling AND the in-flight limit is valid (fails closed).
    * Eligibility is conservative and positive (see `eligible`): only a plain accepted search (or a mention search) with
      the fields the authoritative accepted-search path stores, never a time-bounded or website-derived one.
    * At most ONE API attempt per message, decided by an atomic database claim (the database is authoritative). A claim is
      never retried, re-claimed or released, even when it goes stale or fails.
    * Both daily caps (global and per owner) plus a minimum interval per owner are enforced by ONE atomic update on a
      single per-UTC-day document, so two workers can never both pass the same cap.
    * Accounting is "at most once called, at least once counted": the slot is taken before the call and is never given
      back, because a failed call may still be billed.
    * Any database error before the call means NO call.
    * A non-blocking IN-FLIGHT guard (below) limits simultaneous paid calls inside ONE process. It is process-local: it is
      NOT a distributed limit, it holds no state across workers or restarts, and it only ever REDUCES calls. The database
      budget is what bounds the total.
    * Every link is validated by `valid_reddit_url` when it is stored, again when it is served by the endpoint, and again
      when a stored record is rendered into a page (`render_links`).

Known coverage limitation
    Only a page whose answer-stream handler requests the links ever triggers this. templates/chat.html does. templates/index.html
    carries its OWN copy of the stream handler (by its code comments, the first search made on the home page is streamed there
    after a soft navigation; not exercised in a browser here) and was deliberately NOT changed, so searches streamed there never
    request links. Answers completed in the background and reopened later get none either. Both fail silent: nothing is shown.
"""

import hashlib
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

import config
import timerange

log = logging.getLogger(__name__)

LABEL = "Live web-search links — unverified"
NOTE = ("Returned by a live web search when this answer was generated. Post dates and recency are unverified, and "
        "Flintel has not checked that these posts exist or match your request.")

THRESHOLD = 3                                   # live discovery only when strictly fewer qualifying rows than this
ALLOWED_KINDS = (None, "mention_search")        # request_kind is stored only for non-lead requests (index.py:952)
BUDGET_COLLECTION = "live_reddit_budget"
ATTEMPTS_COLLECTION = "live_reddit_attempts"

# Fixed ceilings (code, not environment): a configured cap above these keeps the feature off / denies the attempt.
GLOBAL_CAP_CEILING = 100_000        # also keeps the one-document-per-day budget record far below the 16 MB limit
OWNER_CAP_CEILING = 1_000
INFLIGHT_CEILING = 16

STATE_CLAIMING, STATE_CALLING = "claiming", "calling"
STATE_OK, STATE_EMPTY, STATE_ERROR, STATE_SKIPPED = "ok", "empty", "error", "skipped"

INSTRUCTIONS = (
    "You find Reddit discussions. Use the web search tool exactly once, restricted to reddit.com, to find Reddit "
    "posts that match the user's request. Reply with one short plain sentence. Do not invent posts, quotes, authors, "
    "dates or numbers."
)

_MAX_URL_LEN = 500
_MAX_TITLE_LEN = 200
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_WS = re.compile(r"\s+")

# The only hosts and the only path shape a stored or shown link may have.
_HOSTS = frozenset({"reddit.com", "www.reddit.com", "old.reddit.com"})
_PRINTABLE_ASCII = re.compile(r"[\x21-\x7e]+")          # no space, no control character, nothing outside ASCII
_POST_PATH = re.compile(r"/r/[A-Za-z0-9_]{2,21}/comments/[a-z0-9]{4,10}(?:/[A-Za-z0-9_-]{1,300}){0,2}/?")


# ── configuration ────────────────────────────────────────────────────────────────────────────────────────────────────

def _pos_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _caps():
    """(global_cap, owner_cap) when both are valid positive integers within their ceilings, else None."""
    g = getattr(config, "REDDIT_LIVE_DAILY_CAP_GLOBAL", 0)
    o = getattr(config, "REDDIT_LIVE_DAILY_CAP_PER_OWNER", 0)
    if _pos_int(g) and _pos_int(o) and g <= GLOBAL_CAP_CEILING and o <= OWNER_CAP_CEILING:
        return g, o
    return None


def _inflight_limit():
    v = getattr(config, "REDDIT_LIVE_MAX_INFLIGHT", 0)
    return v if _pos_int(v) and v <= INFLIGHT_CEILING else None


def is_active() -> bool:
    """True only when the feature is switched on AND both daily caps AND the in-flight limit are valid. Read at call time."""
    try:
        return (getattr(config, "REDDIT_LIVE_LINKS_ENABLED", False) is True
                and _caps() is not None and _inflight_limit() is not None)
    except Exception:
        return False


def _cfg_int(name: str, default: int) -> int:
    value = getattr(config, name, default)
    return value if _pos_int(value) else default


class _InflightGuard:
    """Non-blocking, process-local count of paid calls in progress. NOT a distributed limit (see the module docstring)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._n = 0

    def try_enter(self, limit) -> bool:
        if not (_pos_int(limit) and limit <= INFLIGHT_CEILING):
            return False                        # fail closed on a missing / invalid / over-ceiling limit
        with self._lock:
            if self._n >= limit:
                return False
            self._n += 1
            return True

    def leave(self):
        with self._lock:
            self._n = max(0, self._n - 1)

    @property
    def count(self) -> int:
        with self._lock:
            return self._n


INFLIGHT = _InflightGuard()


# ── the threshold: how many persisted evidence rows does the saved answer carry? ─────────────────────────────────────────

def _parse_answer(answer_text):
    if not isinstance(answer_text, str) or not answer_text.strip():
        return None
    cleaned = answer_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        cleaned = re.sub(r"^json\s*", "", cleaned, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def count_evidence_rows(answer_text):
    """Number of persisted EVIDENCE rows in the saved answer, using the application's own rule unchanged
    (answer_integrity.is_evidence_row: source retrieved AND analysis matched to it AND, in a time-bounded search, inside
    the window). The rows live in the answer JSON (platforms[].posts[] and subjects[].platforms[].posts[]), not in the
    message's `results` list. Returns None when the answer cannot be read as a structured answer: the caller then treats the
    message as ineligible (fail closed). A structured answer without a post table (no_results, trend_report...) has 0."""
    data = _parse_answer(answer_text)
    if data is None:
        return None
    import answer_integrity            # read-only use of the existing rule; this module changes nothing there
    groups = [data.get("platforms")]
    for subject in data.get("subjects") or []:
        if isinstance(subject, dict):
            groups.append(subject.get("platforms"))
    if not any(isinstance(g, list) for g in groups) and not isinstance(data.get("format"), str):
        return None
    seen, count = set(), 0
    for platforms in groups:
        for entry in platforms if isinstance(platforms, list) else []:
            for post in (entry.get("posts") if isinstance(entry, dict) else None) or []:
                if not isinstance(post, dict) or not answer_integrity.is_evidence_row(post):
                    continue
                link = post.get("link")
                if isinstance(link, str) and link:
                    if link in seen:
                        continue
                    seen.add(link)
                count += 1
    return count


# ── links: validation and extraction (only what the API actually returned) ───────────────────────────────────────────

def clean_title(title):
    if not isinstance(title, str):
        return None
    title = _WS.sub(" ", _CTRL.sub(" ", title)).strip()
    return title[:_MAX_TITLE_LEN] if title else None


def valid_reddit_url(url):
    """The canonical URL (`https://<host><path>`, query and fragment dropped) if `url` is an https link to a Reddit POST on
    an exact permitted host, else None. Strict on purpose: ASCII only, no backslash, no whitespace or control character, no
    user information, no port, no percent-encoding anywhere in the authority, and a path of the form
    /r/<subreddit>/comments/<post-id>[/<slug>[/<comment-id>]]. Profile, search, share and other pages are refused."""
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LEN:
        return None
    if not _PRINTABLE_ASCII.fullmatch(url) or "\\" in url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or "@" in parts.netloc or "%" in parts.netloc:
        return None
    host = parts.netloc.lower()                   # a port or userinfo would make this differ from the bare host
    if host not in _HOSTS or not _POST_PATH.fullmatch(parts.path):
        return None
    return f"https://{host}{parts.path}"


def url_key(url) -> str:
    """Dedup key: host without 'www.'/'old.', path without trailing slash, lower case; query and fragment ignored."""
    try:
        parts = urlsplit(str(url))
        host = (parts.hostname or "").lower()
        for prefix in ("www.", "old."):
            if host.startswith(prefix):
                host = host[len(prefix):]
        return host + (parts.path or "").rstrip("/").lower()
    except ValueError:
        return str(url).lower()


def parse_response(data, max_links, known_urls=()):
    """(links, web_search_call_count, sources_count) from a Responses-API dict. Links come only from url_citation
    annotations and web_search_call sources; each is validated by `valid_reddit_url`; the model's text is never kept."""
    links, order, titles = {}, [], {}
    calls = sources = 0
    skip = {url_key(u) for u in known_urls or () if u}
    output = data.get("output") if isinstance(data, dict) else None
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            calls += 1
            action = item.get("action")
            for src in (action.get("sources") if isinstance(action, dict) else None) or []:
                sources += 1
                url = valid_reddit_url(src.get("url") if isinstance(src, dict) else None)
                if url and url_key(url) not in links:
                    links[url_key(url)] = url
                    order.append(url_key(url))
        elif item.get("type") == "message":
            for part in item.get("content") or []:
                for ann in (part.get("annotations") if isinstance(part, dict) else None) or []:
                    if not isinstance(ann, dict) or ann.get("type") != "url_citation":
                        continue
                    url = valid_reddit_url(ann.get("url"))
                    if not url:
                        continue
                    key = url_key(url)
                    if key not in links:
                        links[key] = url
                        order.append(key)
                    title = clean_title(ann.get("title"))
                    if title and key not in titles:
                        titles[key] = title
    out = []
    for key in order:
        if key in skip:
            continue
        out.append({"url": links[key], "title": titles.get(key)})
        if len(out) >= max_links:
            break
    return out, calls, sources


def completed_search_calls(data) -> int:
    """How many `web_search_call` output items the response carries with status "completed". A generic HTTP 200, or citations
    on their own, do not prove the search tool actually ran."""
    out = data.get("output") if isinstance(data, dict) else None
    return sum(1 for item in (out if isinstance(out, list) else [])
               if isinstance(item, dict) and item.get("type") == "web_search_call" and item.get("status") == "completed")


def _utc(t) -> str:
    """The exact instant as a UTC string, e.g. 2026-11-01T11:30:00Z (whole seconds)."""
    return timerange.to_utc(t).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(query: str, window=None, context=None) -> dict:
    """The single request. Mirrors the one-request benchmark's shape: reddit.com only, exactly one tool call, sources
    included, nothing stored. The model name comes from config; 'minimal' reasoning is raised to 'low' (web search is
    not documented to work with minimal reasoning - unverified).

    `window=(start, end)` (aware UTC, half-open [start, end)) is the active-intent workflow's shared time window: the exact
    boundaries are written into the instructions and the input. This only ASKS the model; the backend still checks every
    returned post's publication time itself (`verify_publication`) and drops anything it cannot confirm.

    `context` ({"query", "keywords"}; only used together with `window`) is the stored message's own wording and keywords, given
    to the model as supporting context. With `window` the model is also asked to look for posts that show genuine, current
    demand (judged from meaning, not exact words) and to leave out posts it is unsure about; this only ASKS - nothing here
    verifies demand, and no post is described as a buyer."""
    query = _WS.sub(" ", _CTRL.sub(" ", str(query or ""))).strip()[:300]
    effort = getattr(config, "LLM_REASONING_EFFORT", "low")
    if effort not in ("low", "medium", "high"):
        effort = "low"
    instructions, text = INSTRUCTIONS, f"Find Reddit posts for this request: {query}"
    if window is not None:
        start, end = _utc(window[0]), _utc(window[1])
        instructions = (INSTRUCTIONS + f" Only return Reddit posts that were published between {start} (inclusive) and {end} "
                        "(exclusive), both in UTC. Return only real Reddit post URLs that your search actually found; never invent "
                        "or guess a post, title, author, date or URL. If you find nothing in that period, return nothing.")
        ctx = context if isinstance(context, dict) else {}
        discussion = ctx.get("mode") == "discussion"      # the classifier's other outcome; anything else is the demand search
        kws = [k for k in (_WS.sub(" ", _CTRL.sub(" ", k)).strip()[:60] for k in (ctx.get("keywords") if isinstance(ctx.get("keywords"), list) else [])[:20]
                           if isinstance(k, str)) if k]
        if kws:       # the keywords generated for this prompt are the PRIMARY search terms; the wording only says what demand to look for
            text = ("Search reddit.com for posts matching these keywords: " + ", ".join(kws)
                    + f"\nThe user's request (context only - it defines what {'conversations' if discussion else 'demand'} to look for; it is not the search query): {query}")
            instructions += (" Use the given keywords, plus close synonyms or variants you judge appropriate, to build your search "
                             "query. You may run only ONE search, so combine them into one effective query instead of searching "
                             "once per keyword.")
        text += (f"\nPublication window (UTC): from {start} (inclusive) until {end} (exclusive). "
                 "Return only posts published inside it.")
        original = _WS.sub(" ", _CTRL.sub(" ", str(ctx.get("query") or ""))).strip()[:300]
        if original and original != query:
            text += f"\nThe user's original wording{' (context only)' if kws else ''}: {original}"
        if discussion:
            instructions += (" First work out what topic, tool or product the user wants to see discussed. Then look for posts in "
                             "which people are currently discussing it: sharing experiences, asking questions, comparing options or "
                             "talking about it. The authors do not need to be looking to buy or hire anything. People word this in "
                             "many different ways, so do not depend on exact keywords. Leave out posts that only advertise, only "
                             "repeat news without any discussion, or merely mention the topic in passing. If you are unsure whether "
                             "a post is a relevant discussion, leave it out.")
        else:
            instructions += (" First work out what the user actually wants. Then look for posts whose authors show genuine, current "
                             "demand for it: a real need, an active search, an intention to hire or buy, a request for recommendations, "
                             "or an attempt to find a solution. People word this in many different ways, so do not depend on exact "
                             "keywords. Leave out posts that only explain, teach, report news, advertise or merely mention the topic. "
                             "If you are unsure whether a post shows real demand, leave it out.")
    return {
        "model": getattr(config, "REDDIT_LIVE_MODEL", "") or getattr(config, "LLM_MODEL", ""),
        "instructions": instructions,
        "input": text,
        "tools": [{"type": "web_search", "filters": {"allowed_domains": ["reddit.com"]}}],
        "tool_choice": "required",
        "include": ["web_search_call.action.sources"],
        "max_tool_calls": 1,
        "max_output_tokens": _cfg_int("REDDIT_LIVE_MAX_OUTPUT_TOKENS", 2000),
        "reasoning": {"effort": effort},
        "store": False,
    }


# ── dependencies (injected, so tests use fakes and nothing here opens a connection at import) ─────────────────────────────

class Deps:
    def __init__(self, chats, budget, attempts, post, now, record_usage, reddit_info=None):
        self.chats, self.budget, self.attempts = chats, budget, attempts
        self.post, self.now, self.record_usage = post, now, record_usage
        self.reddit_info = reddit_info or _reddit_info      # per-post metadata lookup (publication-date check); injectable


def _http_post(url, headers, payload, timeout):
    """The one HTTP call: no retry, redirects are not followed (a 3xx raises), non-2xx raises. `timeout` is httpx's per-phase
    timeout (connect/read/write/pool each), NOT a total deadline."""
    import httpx
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


# ── publication-date verification (active-intent workflow only) ─────────────────────────────────────────────────────────
# Reddit's own public metadata for the posts the model found: ONE GET for all (at most REDDIT_LIVE_MAX_LINKS) candidates, to a
# constant host, no redirects, short timeout. It is a lookup of those specific posts, never a search of any database.
META_ENDPOINT = "https://api.reddit.com/api/info/"
META_TIMEOUT_SECONDS = 8
META_USER_AGENT = "Flintel/1.0 (checks the publication time of posts it links to; +https://getflintel.com)"
MAX_META_IDS = 25
_META_MAX_BYTES = 2_000_000
_POST_REF = re.compile(r"/r/([A-Za-z0-9_]{2,21})/comments/([a-z0-9]{4,10})(?:/|$)")


def post_ref(url):
    """(subreddit, post_id) of an already-validated post URL, else None."""
    try:
        m = _POST_REF.match(urlsplit(url).path)
    except Exception:       # a non-string or malformed value is simply 'no post reference'
        return None
    return (m.group(1), m.group(2)) if m else None


def _reddit_info(ids, timeout):
    """{post_id: {"created_utc", "subreddit", "removed"}} for the ids Reddit returned. Raises on any HTTP error (429, 403, 5xx,
    a redirect), timeout or unreadable body: the caller treats that as 'cannot verify' for every candidate."""
    import httpx
    params = {"id": ",".join("t3_" + i for i in ids), "raw_json": "1"}
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        r = client.get(META_ENDPOINT, params=params, headers={"User-Agent": META_USER_AGENT, "Accept": "application/json"})
        r.raise_for_status()
        if len(r.content) > _META_MAX_BYTES:
            raise ValueError("metadata response too large")
        data = r.json()
    out = {}
    children = ((data.get("data") or {}).get("children") or []) if isinstance(data, dict) else []
    for ch in children:
        d = ch.get("data") if isinstance(ch, dict) and ch.get("kind") == "t3" else None
        if isinstance(d, dict) and isinstance(d.get("id"), str):
            out[d["id"]] = {"created_utc": d.get("created_utc"), "subreddit": d.get("subreddit"),
                            "removed": bool(d.get("removed_by_category")) or d.get("author") == "[deleted]"
                            or d.get("selftext") in ("[removed]", "[deleted]")}
    return out


def verify_publication(deps, links, window):
    """(verified_links, stats). Keeps ONLY the links whose publication time Reddit's metadata confirms to lie inside the half-open
    window [start, end). Fail closed per candidate: a post that is missing from the reply (deleted / private / inaccessible), in
    another subreddit than its URL says, removed, without a readable or plausible timestamp, or outside the window is dropped
    and counted; if the lookup itself fails, every candidate is dropped. A model's date or a title is never evidence. Each kept
    link carries the date Reddit reported, as an ISO UTC string. Never raises."""
    start, end = timerange.to_utc(window[0]), timerange.to_utc(window[1])
    stats = {"checked": 0, "verified": 0, "outside_window": 0, "unverifiable": 0, "removed": 0, "lookup_failed": 0}
    refs = []
    for l in links or []:
        ref = post_ref(l.get("url")) if isinstance(l, dict) else None
        stats["checked"] += 1
        if ref is None:
            stats["unverifiable"] += 1
        else:
            refs.append((l, ref))
    refs = refs[:MAX_META_IDS]
    if not refs:
        return [], stats
    ids = sorted({r[1] for _l, r in refs})
    try:
        info = deps.reddit_info(ids, META_TIMEOUT_SECONDS)
        if not isinstance(info, dict):
            raise ValueError("unexpected metadata reply")
    except Exception as exc:
        stats["lookup_failed"] = len(refs)
        log.warning("Reddit metadata lookup failed (%s status=%s): no live candidate is verified", type(exc).__name__,
                    getattr(getattr(exc, "response", None), "status_code", None))
        return [], stats
    kept = []
    for l, (sub, pid) in refs:
        d = info.get(pid)
        if not isinstance(d, dict) or str(d.get("subreddit") or "").lower() != sub.lower():
            stats["unverifiable"] += 1
        elif d.get("removed"):
            stats["removed"] += 1
        else:
            when = timerange.publication_time(d.get("created_utc"), now=end, platform="reddit")
            if when is None:
                stats["unverifiable"] += 1
            elif not (start <= when < end):
                stats["outside_window"] += 1
            else:
                stats["verified"] += 1
                kept.append(dict(l, published_at=when.isoformat()))
    return kept, stats


def _record_usage(model, data, status):
    try:
        import usage_meter
        usage_meter.record_usage("llm", model, *usage_meter.extract_responses_usage(data), status=status,
                                 extra={"web_search_enabled": True, "response_id": (data or {}).get("id")})
    except Exception:
        pass


def default_deps() -> Deps:
    from database import chats_collection
    db = chats_collection.database
    return Deps(chats_collection, db[BUDGET_COLLECTION], db[ATTEMPTS_COLLECTION], _http_post,
                lambda: datetime.now(timezone.utc), _record_usage, _reddit_info)


# ── the single entry point ────────────────────────────────────────────────────────────────────────────────────────────

def _owner_hash(owner_key: str) -> str:
    return hashlib.sha256(str(owner_key).strip().lower().encode("utf-8")).hexdigest()[:32]


def _aware(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _find_message(chat, request_id, topic_key):
    msgs = (chat or {}).get("messages") or []
    if request_id:
        idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("request_id") == request_id), None)
    else:
        idx = next((i for i in range(len(msgs) - 1, -1, -1) if topic_key and msgs[i].get("topic_key") == topic_key), None)
    return (idx, msgs[idx]) if idx is not None else (None, None)


def _stale_after_seconds() -> int:
    return 2 * (_cfg_int("REDDIT_LIVE_TIMEOUT_SECONDS", 25) + 30)


def _safe_links(links):
    out = []
    for l in links if isinstance(links, list) else []:
        url = valid_reddit_url(l.get("url")) if isinstance(l, dict) else None
        if url:
            item = {"url": url, "title": clean_title(l.get("title"))}
            when = timerange.to_utc(l.get("published_at")) if isinstance(l.get("published_at"), str) else None
            if when is not None:
                item["published_at"] = when.isoformat()      # only ever set by verify_publication
            out.append(item)
    return out


def present(live, now, pre_answer=False) -> dict:
    """What the client may see of a stored live_reddit record. Links only for state 'ok', each re-validated.
    A record made by the active-intent workflow (`workflow` marker) is shown INSIDE that workflow's answer; the post-answer
    links endpoint therefore reports nothing for it (no second, duplicate links section)."""
    if not isinstance(live, dict):
        return {"state": "none"}
    if live.get("workflow") and not pre_answer:
        return {"state": "none"}
    state = live.get("state")
    if state == STATE_OK:
        links = _safe_links(live.get("links"))
        if links:
            return {"state": "ok", "label": LABEL, "note": NOTE, "links": links}
        return {"state": "none"}
    if state in (STATE_CLAIMING, STATE_CALLING):
        claimed = _aware(live.get("claimed_at"))
        if claimed is not None and (now - claimed).total_seconds() < _stale_after_seconds():
            return {"state": "pending"}
        return {"state": "unavailable"}          # a stale claim is final: it is never re-claimed or retried
    return {"state": "none"}


def render_links(live):
    """Template helper (registered as `live_links_for`): the validated links of a stored record, or []. Same validator as at
    write time, so a tampered or legacy stored URL can never reach a page. Nothing is rendered while the feature is off."""
    try:
        if is_active() and isinstance(live, dict) and live.get("state") == STATE_OK and not live.get("workflow"):
            return _safe_links(live.get("links"))
    except Exception:
        pass
    return []


def _nonempty_str_list(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(x, str) and x.strip() for x in value)


def _eligible_core(msg):
    """(ok, reason). Positive and conservative; every rule is checked on the stored message, nothing on the client.

    The rules about the SEARCH itself, shared by both workflows (fields verified against the accepted-search flow):
      * a search turn (not a chat turn) with a request_id, and `request_kind` ABSENT (a plain lead search) or exactly
        "mention_search" (the router stores research / provider_search / person_mentions explicitly);
      * keywords (a non-empty list of non-empty strings), `evidence_required` (a positive integer: set only when the router
        returned one) and `credit_request_id` (set only by the credited accept path, after the reservation);
      * NOT website-derived (`website_only`, `website_evidence`, `website_context`) and NOT time-bounded (`time_range`,
        `time_window_days`): web_search cannot enforce a requested period, so such a search never gets live links;
      * not cancelled or failed.
    The post-answer endpoint (`eligible`) adds: answered, answered recently, fewer than THRESHOLD evidence rows.
    `website_note` is deliberately NOT consulted: it also carries time-range and guidance notes."""
    if not isinstance(msg, dict) or msg.get("message_type") == "chat":
        return False, "not_a_search"
    if not isinstance(msg.get("request_id"), str) or not msg.get("request_id"):
        return False, "no_request_id"
    if msg.get("request_kind") not in ALLOWED_KINDS:
        return False, "kind"
    if msg.get("website_only") or msg.get("website_evidence") or msg.get("website_context"):
        return False, "website"
    if msg.get("time_range") or msg.get("time_window_days") is not None:
        return False, "time_bounded"
    if not _nonempty_str_list(msg.get("keywords")):
        return False, "no_keywords"
    if not isinstance(msg.get("credit_request_id"), str) or not msg.get("credit_request_id"):
        return False, "no_credit_request_id"
    if not _pos_int(msg.get("evidence_required")):
        return False, "no_evidence_required"
    if msg.get("cancelled_at") or msg.get("failed_at"):
        return False, "closed"
    return True, "ok"


# The only request kinds the ACTIVE-INTENT workflow will even put to its classifier: a plain lead search (no kind) and "research".
# The router files a request like "posts people are actively talking about" under "research" (it keeps the router's keywords); whether
# such a request really expresses active demand is the classifier's decision, by meaning, with its confidence requirement. Every
# other kind (provider_search, person_mentions, mention_search) is a different workflow and stays rejected here.
PRE_ANSWER_KINDS = (None, "research")


def eligible_pre_answer(msg):
    """(ok, reason) for the ACTIVE-INTENT workflow, which asks for live links BEFORE the answer exists: every rule of
    `eligible` that concerns the search itself (same function), none that needs an answer (no answer, no recency, no evidence
    count), and only a plain lead search or a "research" search (see PRE_ANSWER_KINDS): any other `request_kind` is rejected. A
    time range of the user's own does not block it (see below). The post-answer endpoint (`eligible`) is not affected: it still
    refuses "research" and time-bounded searches."""
    kind = msg.get("request_kind") if isinstance(msg, dict) else None
    # The Historical search's own time range / lookback (typed range, router period, default recency lookback) belongs to Historical
    # alone: it neither changes nor blocks the Live search, which has its own window (ACTIVE_INTENT_WINDOW_MINUTES). The other
    # rules of _eligible_core are applied as always. (The post-answer `eligible()` keeps its own time-bounded rule.)
    probe = dict(msg, time_range=None, time_window_days=None)
    if kind == "research":
        probe["request_kind"] = None
    ok, reason = _eligible_core(probe)
    if not ok:
        return False, reason
    if kind not in PRE_ANSWER_KINDS:
        return False, "kind"
    return True, "ok"


def eligible(msg, now):
    """(ok, reason) for the post-answer links endpoint: the core rules, then an answer that is recent and short of evidence."""
    ok, reason = _eligible_core(msg)
    if not ok:
        return False, reason
    answer = msg.get("claude_answer")
    if not isinstance(answer, str) or not answer.strip():
        return False, "no_answer"
    answered = _aware(msg.get("answered_at"))
    if answered is None or not (0 <= (now - answered).total_seconds() <= _cfg_int("REDDIT_LIVE_ELIGIBLE_WINDOW_SECONDS", 900)):
        return False, "not_recent"
    count = count_evidence_rows(answer)
    if count is None:
        return False, "unreadable_answer"
    if count >= THRESHOLD:
        return False, "enough_evidence"
    return True, "ok"


def _reserve_budget(deps, owner_hash, now) -> bool:
    """ONE atomic update on the day's document: global cap, per-owner cap and minimum interval together. True only when
    this call took a slot. Any error, a reached cap, a too-soon owner, or a cap that is missing / invalid / above its
    ceiling means False (fail closed)."""
    caps = _caps()
    if caps is None:
        return False
    cap_g, cap_o = caps
    interval = _cfg_int("REDDIT_LIVE_OWNER_MIN_INTERVAL_SECONDS", 30)
    day_id = "day:" + now.strftime("%Y-%m-%d")
    flt = {"_id": day_id, "count": {"$lt": cap_g}, f"owners.{owner_hash}": {"$not": {"$gte": cap_o}},
           f"last.{owner_hash}": {"$not": {"$gt": now - timedelta(seconds=interval)}}}
    update = {"$inc": {"count": 1, f"owners.{owner_hash}": 1}, "$set": {f"last.{owner_hash}": now}}
    for attempt in (1, 2):
        try:
            # upsert: the first request of a UTC day creates the document. When the document exists but the filter fails
            # (cap reached / too soon) the upsert collides on _id and raises DuplicateKeyError: that is a refusal. It is
            # retried once only because two first-of-the-day requests can collide on creation.
            doc = deps.budget.find_one_and_update(flt, update, upsert=True, return_document=True)
            return doc is not None
        except Exception as exc:
            if attempt == 1 and type(exc).__name__ == "DuplicateKeyError":
                continue
            if type(exc).__name__ != "DuplicateKeyError":
                log.warning("Live-links budget update failed (%s): no call is made", type(exc).__name__)
            return False
    return False


def _set_live(deps, chat_id, owner_key, idx, request_id, claim_id, fields, require_state=None) -> bool:
    flt = {"chat_id": chat_id, "owner_key": owner_key, f"messages.{idx}.request_id": request_id,
           f"messages.{idx}.live_reddit.claim_id": claim_id}
    if require_state:
        flt[f"messages.{idx}.live_reddit.state"] = {"$in": list(require_state)}
    res = deps.chats.update_one(flt, {"$set": {f"messages.{idx}.live_reddit.{k}": v for k, v in fields.items()}})
    return res.matched_count == 1


def process(deps, *, owner_key, owner_type, chat_id, request_id=None, topic_key=None, pre_answer=False, window=None, intent=None) -> dict:
    """Idempotent. Returns {"state": ...} and, for 'ok', the labelled links. Never raises. The caller has already
    authenticated the session and passes the session's owner (never a client-supplied one).
    States other than 'ok': disabled, ineligible, not_found, pending (another request holds the claim), busy (this process
    is at its in-flight limit; nothing was claimed or counted), unavailable, none.

    `pre_answer=True` is the ACTIVE-INTENT workflow (active_intent.py): the same claim, budget, in-flight guard, validator and
    idempotency, but asked for BEFORE the answer exists (no answer / recency / evidence-count rules; a plain lead search only),
    the record is marked `workflow`, and the result additionally carries `reason` (a safe diagnostic code) plus, when a call was
    made, `http_status`, `web_search_calls` (completed search calls) and `validated_urls`. Without it nothing here changes.

    `window=(start, end)` (aware UTC, half-open) is only used together with `pre_answer`: it goes into the model's instructions,
    every returned post is then checked against Reddit's own publication time (`verify_publication`) and only posts inside the
    window are kept, and the window is recorded on the claim so that a stored result is only ever reused for the SAME window."""
    entered = False
    wdoc = {"start": _utc(window[0]), "end": _utc(window[1])} if (pre_answer and window is not None) else None

    def R(out, reason, **extra):                     # pre-answer callers also get a reason; the endpoint's replies are unchanged
        return dict(out, reason=reason, **extra) if pre_answer else out
    try:
        if not is_active():
            return R({"state": "disabled"}, "live_inactive")
        if owner_type != "email" or not owner_key:
            return R({"state": "ineligible"}, "guest_or_no_owner")
        now = deps.now()
        chat = deps.chats.find_one({"chat_id": chat_id, "owner_key": owner_key})
        if not chat:
            return R({"state": "not_found"}, "chat_not_found")
        idx, msg = _find_message(chat, request_id, topic_key)
        if msg is None:
            return R({"state": "not_found"}, "message_not_found")
        if isinstance(msg.get("live_reddit"), dict):
            if wdoc is not None and msg["live_reddit"].get("window") != wdoc:
                return R({"state": "none"}, "window_mismatch")      # a result for another window is never reused
            return R(present(msg["live_reddit"], now, pre_answer=pre_answer), "already_decided")   # repeated asks only read
        ok, _reason = eligible_pre_answer(msg) if pre_answer else eligible(msg, now)
        if not ok:
            return R({"state": "ineligible"}, f"ineligible:{_reason}")
        if not getattr(config, "OPENAI_API_KEY", ""):
            return R({"state": "unavailable"}, "no_api_key")  # checked BEFORE the claim: a missing key burns no budget
        # in-flight guard: non-blocking, BEFORE the claim, so a refusal claims and counts nothing and can be asked again
        if not INFLIGHT.try_enter(_inflight_limit()):
            return R({"state": "busy"}, "in_flight_limit")
        entered = True
        rid = msg["request_id"]
        claim_id = uuid.uuid4().hex
        day = now.strftime("%Y-%m-%d")
        # 1. the authoritative claim: exactly one caller can ever set live_reddit on this message
        claim_filter = {"chat_id": chat_id, "owner_key": owner_key, f"messages.{idx}.request_id": rid,
                        f"messages.{idx}.live_reddit": {"$exists": False},
                        f"messages.{idx}.cancelled_at": {"$exists": False}, f"messages.{idx}.failed_at": {"$exists": False}}
        if not pre_answer:                                     # the endpoint only ever claims an ANSWERED message
            claim_filter[f"messages.{idx}.claude_answer"] = {"$nin": [None, ""]}
        record = {"claim_id": claim_id, "state": STATE_CLAIMING, "claimed_at": now, "day": day}
        if pre_answer:
            record["workflow"] = "active_intent"
            if wdoc is not None:
                record["window"] = wdoc
        res = deps.chats.update_one(claim_filter, {"$set": {f"messages.{idx}.live_reddit": record}})
        if res.matched_count != 1:
            fresh = deps.chats.find_one({"chat_id": chat_id, "owner_key": owner_key}) or {}
            _i, again = _find_message(fresh, rid, None)
            if wdoc is not None and again and isinstance(again.get("live_reddit"), dict) and again["live_reddit"].get("window") != wdoc:
                return R({"state": "none"}, "window_mismatch")
            return R(present((again or {}).get("live_reddit"), now, pre_answer=pre_answer) if again else {"state": "none"},
                     "claim_lost")
        # 2. the budget slot (both caps + interval, one atomic update). No slot = no call; the claim stays, terminal.
        if not _reserve_budget(deps, _owner_hash(owner_key), now):
            try:
                _set_live(deps, chat_id, owner_key, idx, rid, claim_id, {"state": STATE_SKIPPED, "finished_at": now})
            except Exception:
                pass
            return R({"state": "none"}, "budget_or_cooldown_refused")
        # 3. mark the call as started; if the database cannot record it, make NO call (the slot stays counted)
        if not _set_live(deps, chat_id, owner_key, idx, rid, claim_id, {"state": STATE_CALLING},
                         require_state=(STATE_CLAIMING,)):
            return R({"state": "unavailable"}, "could_not_record_call_start")
        # 4. the single attempt: no retry, no redirect, hard per-phase timeout
        payload = build_payload(msg.get("resolved_query") or msg.get("query"), window if wdoc is not None else None,
                                {"query": msg.get("query"), "keywords": msg.get("keywords"), "mode": intent} if wdoc is not None else None)
        timeout = _cfg_int("REDDIT_LIVE_TIMEOUT_SECONDS", 25)
        headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}", "content-type": "application/json"}
        started = deps.now()
        state, links, calls, sources, data, http_status = STATE_ERROR, [], 0, 0, None, None
        completed, outcome, vstats = 0, "api_error", None
        try:
            data = deps.post(config.OPENAI_RESPONSES_URL, headers, payload, timeout)
            if isinstance(data, dict):
                http_status = 200                              # the HTTP layer raises on any non-2xx
                known = [r.get("post_url") for r in (msg.get("results") or []) if isinstance(r, dict)]
                links, calls, sources = parse_response(data, _cfg_int("REDDIT_LIVE_MAX_LINKS", 8), known)
                completed = completed_search_calls(data)
                # SUCCESS needs a COMPLETED web_search_call as well as a validated Reddit post URL: HTTP 200, or citations
                # on their own, prove nothing about the search tool having run.
                if data.get("status") in ("failed", "cancelled"):
                    state, links, outcome = STATE_ERROR, [], "response_" + str(data.get("status"))
                elif links and completed >= 1:
                    state, outcome = STATE_OK, "ok"
                    if wdoc is not None:     # keep only posts whose PUBLICATION time Reddit confirms is inside the window
                        links, vstats = verify_publication(deps, links, window)
                        state, outcome = (STATE_OK, "ok") if links else (STATE_EMPTY, "no_verified_in_window")
                elif links:
                    state, links, outcome = STATE_ERROR, [], "no_completed_web_search_call"
                else:
                    state, outcome = STATE_EMPTY, ("no_valid_reddit_urls" if completed >= 1 else "no_completed_web_search_call")
            else:
                outcome = "response_not_an_object"
        except Exception as exc:
            http_status = getattr(getattr(exc, "response", None), "status_code", None)
            log.warning("Live Reddit links call failed: %s status=%s", type(exc).__name__, http_status)
        latency_ms = int((deps.now() - started).total_seconds() * 1000)
        # 5. record: tokens as reported (no tool fee, no dollar figure), the attempt, and the outcome
        try:
            if isinstance(data, dict):
                deps.record_usage(payload["model"], data, "incomplete" if data.get("status") == "incomplete" else "ok")
            usage = (data or {}).get("usage") if isinstance(data, dict) else None
            deps.attempts.insert_one({
                "ts": now, "day": day, "claim_id": claim_id, "request_id": rid, "owner": _owner_hash(owner_key),
                "state": state, "http_status": http_status, "latency_ms": latency_ms, "model": payload["model"],
                "input_tokens": (usage or {}).get("input_tokens"), "output_tokens": (usage or {}).get("output_tokens"),
                "web_search_calls": calls, "sources": sources, "links": len(links), "cost_usd": None,
                "completed_search_calls": completed, "outcome": outcome, "verification": vstats})
        except Exception as exc:
            log.warning("Recording a live-links attempt failed (%s)", type(exc).__name__)
        try:
            saved = _set_live(deps, chat_id, owner_key, idx, rid, claim_id,
                              {"state": state, "links": links, "finished_at": deps.now(), "latency_ms": latency_ms},
                              require_state=(STATE_CALLING,))
            if not saved:
                log.warning("Live-links result was not stored (claim no longer matches)")
        except Exception as exc:
            log.warning("Storing the live-links result failed (%s)", type(exc).__name__)
        info = {"http_status": http_status, "web_search_calls": completed, "validated_urls": len(links)}
        if vstats is not None:
            info["verification"] = vstats
        if state == STATE_OK:
            return R(present({"state": STATE_OK, "links": links}, now, pre_answer=True), outcome, **info)
        return R({"state": "none"}, outcome, **info)
    except Exception as exc:                                   # fail closed: never raise into the request
        log.warning("Live-links request failed closed (%s)", type(exc).__name__)
        return R({"state": "unavailable"}, "exception")
    finally:
        if entered:
            INFLIGHT.leave()
