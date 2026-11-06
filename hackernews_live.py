"""
hackernews_live.py — Hacker News as a LIVE-fetched source (public Algolia HN Search API, no key).
============================================================================================

What this is: pure helpers + one bounded fetch. Hacker News has no embeddings and no saved corpus, so it is never read from MongoDB
or MySQL; the router's own keywords are searched on Algolia (`/search`, or `/search_by_date` for a recent window), the hits are
normalised into the SAME record shape the Reddit wide pool uses ({"title","post_text","post_url","platform", ...}) and everything
downstream (intent bridge, ranking, evidence, integrity, credits) stays platform-agnostic.

Rules this module keeps:
  * Only the public API; no HTML scraping. One request per keyword, run in parallel, de-duplicated by Algolia `objectID`.
  * The pool is filled up to `pool_n` (the caller sizes it exactly like the Reddit wide pool) or until the page cap, the hard
    overall deadline, or a rate limit stops it. Each keyword contributes up to `pool_n` and the keywords are merged round-robin, so
    one fast keyword cannot crowd the others out.
  * TEXT-REQUIRED AT PICK-TIME stays in force: a story with no body text (most link stories) is not a candidate. Ask HN / Show HN /
    text stories qualify. Nothing is invented: no text, dates or scores are made up.
  * The time window is the repo's half-open [start, end): it goes to Algolia as `numericFilters=created_at_i>=START,created_at_i<END`
    AND every returned item is re-verified against it with `timerange.publication_time`; anything outside, future-dated or without a
    date is dropped and counted.
  * Fail-closed: `fetch` never raises. Any failure returns what was collected plus an honest status; the caller decides.
  * The HTTP layer is injectable (`http_get`) so tests never touch the network.
"""
import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import timerange

log = logging.getLogger("flintel.hackernews")

ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
USER_AGENT = "Flintel-HN-Search/1.0 (public Algolia HN API client)"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
PLATFORM = "hackernews"
MAX_QUERIES = 12                  # hard cap on keyword queries per fetch (the router already bounds its keywords)
MAX_WORKERS = 6                   # parallel Algolia requests
PER_REQUEST_TIMEOUT_CAP = 8.0     # seconds; each request is also bounded by the time left of the overall deadline

_TAG = re.compile(r"<[^>]+>")
_P_BREAK = re.compile(r"</?p\s*/?>|<br\s*/?>", re.I)
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


# ── pure helpers ──────────────────────────────────────────────────────────────────────────────────────────────────────
def pool_size(limit, multiplier, minimum, maximum) -> int:
    """The Reddit wide-pool rule, unchanged: max(min, min(limit x multiplier, max))."""
    return max(minimum, min(limit * multiplier, maximum))


def window_bounds(since_days=None, time_range=None, now=None, max_days=3650):
    """(start, end) aware UTC, half-open, or (None, None) when the search is unbounded. Mirrors get_matched_signals exactly: an
    explicit `time_range` wins; else the router's `since_days` means [now - days, now + FUTURE_TOLERANCE)."""
    now = timerange.to_utc(now) or datetime.now(timezone.utc)
    tr = time_range if isinstance(time_range, timerange.TimeRange) else timerange.range_from_doc(time_range)
    if tr is not None:
        return tr.start, tr.end
    if isinstance(since_days, int) and not isinstance(since_days, bool) and since_days > 0:
        return now - timedelta(days=min(since_days, max_days)), now + timerange.FUTURE_TOLERANCE
    return None, None


def clean_queries(keywords) -> list:
    """Distinct, non-empty keyword strings in order, capped at MAX_QUERIES."""
    out, seen = [], set()
    for k in keywords or []:
        if isinstance(k, str):
            q = " ".join(k.split())
            if q and q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
        if len(out) >= MAX_QUERIES:
            break
    return out


def build_url(query, *, start=None, end=None, page=0, hits_per_page=50, by_date=False) -> str:
    """The Algolia HN search URL for one keyword. Stories only. The window is [start, end): `>=` start, `<` end (epoch seconds)."""
    params = [("query", query), ("tags", "story"), ("hitsPerPage", int(hits_per_page)), ("page", int(page))]
    filters = []
    if start is not None:
        filters.append(f"created_at_i>={int(timerange.to_utc(start).timestamp())}")
    if end is not None:
        filters.append(f"created_at_i<{int(timerange.to_utc(end).timestamp())}")
    if filters:
        params.append(("numericFilters", ",".join(filters)))
    return f"{ALGOLIA_BASE}/{'search_by_date' if by_date else 'search'}?{urlencode(params)}"


def strip_html(text) -> str:
    """Plain text of an Algolia `story_text` (HTML with entities). Paragraph/line breaks become newlines; tags and entities go."""
    if not isinstance(text, str) or not text:
        return ""
    t = _P_BREAK.sub("\n", text)
    t = _TAG.sub("", t)
    t = html.unescape(t)
    t = "\n".join(_WS.sub(" ", line).strip() for line in t.split("\n"))
    return _NL.sub("\n\n", t).strip()


def normalize_hit(hit, allow_title_only=False):
    """One Algolia hit -> the Reddit wide-pool record shape, or None when it has no usable id / title / integer date.
    post_url = the HN item URL (identity for dedup / source_id); the external article link is kept in `external_url`. The record
    may have an empty post_text — `is_candidate` is the text-required rule.
    `allow_title_only` (general discovery only): a story with no body keeps its TITLE as its text (nothing is invented: it is the
    post's own title) and says so in `text_source` / `limitations`, so the evidence can never be mistaken for a full post body."""
    if not isinstance(hit, dict):
        return None
    oid = str(hit.get("objectID") or "").strip()
    title = hit.get("title")
    ts = hit.get("created_at_i")
    if not oid.isdigit() or not isinstance(title, str) or not title.strip() or isinstance(ts, bool) or not isinstance(ts, int):
        return None
    rec = {"title": title.strip(), "post_text": strip_html(hit.get("story_text")), "post_url": ITEM_URL.format(oid),
           "platform": PLATFORM, "created_utc": float(ts), "hn_id": oid}
    rec["text_source"] = "story_text" if rec["post_text"] else None
    if allow_title_only and not rec["post_text"]:
        rec["post_text"] = rec["title"]
        rec["text_source"] = "title"
        rec["limitations"] = ["Link story without body text: only the title is available as evidence."]
    ext = hit.get("url")
    if isinstance(ext, str) and ext.strip():
        rec["external_url"] = ext.strip()
    for src, dst in (("author", "author"), ("points", "hn_points"), ("num_comments", "hn_comments")):
        v = hit.get(src)
        if isinstance(v, (str, int)) and not isinstance(v, bool) and v != "":
            rec[dst] = v                                   # only what the API returned; nothing is derived or invented
    return timerange.with_published_at(rec, rec)       # published_at only when the date passes the repo's publication-time rules


def is_candidate(rec) -> bool:
    """TEXT-REQUIRED AT PICK-TIME: a record with no post_text is never a match."""
    return bool(isinstance(rec, dict) and (rec.get("post_text") or "").strip())


def in_window(rec, start, end, now=None) -> bool:
    """True when the record's validated publication time lies in [start, end); a missing / invalid / future date is False."""
    t = timerange.post_published_at(rec, now=now)
    if t is None:
        return False
    return (start is None or t >= start) and (end is None or t < end)


def _default_http_get(url, timeout):
    import httpx                                      # already a dependency (google.py)
    r = httpx.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=timeout)
    if r.status_code == 429:
        raise RateLimited()
    r.raise_for_status()
    return r.json()


class RateLimited(Exception):
    """The API asked us to slow down: stop querying for this fetch (never retried, never hidden)."""


# ── the bounded fetch ─────────────────────────────────────────────────────────────────────────────────────────────────────
def fetch(keywords, *, pool_n, start=None, end=None, by_date=False, hits_per_page=50, max_pages=3, deadline_seconds=12.0,
          http_get=None, now=None, general=False):
    """(records, status). `records`: up to `pool_n` unique candidates (text present, inside [start, end)), merged round-robin over
    the keyword queries so no single keyword fills the pool. `status` is an honest summary:
        state: "ok" (>=1 candidate) | "empty" (searched fine, nothing qualified) | "partial" (some requests failed or the deadline
               hit, with candidates) | "failed" (no request succeeded) | "rate_limited" | "skipped" (no usable keyword / pool)
        requests, failed_requests, fetched_hits, no_text, outside_window, duplicates, reason.
    `general=True` is GENERAL DISCOVERY: no keyword at all (an empty query on `search_by_date`: the newest stories first), title-only
    link stories are kept (see normalize_hit) and the date window / de-duplication / verification are exactly the same.
    Never raises."""
    status = {"state": "skipped", "requests": 0, "failed_requests": 0, "fetched_hits": 0, "no_text": 0, "outside_window": 0,
              "duplicates": 0, "reason": None}
    try:
        queries = [""] if general else clean_queries(keywords)
        by_date = bool(by_date or general)
        if not queries or not isinstance(pool_n, int) or pool_n <= 0:
            status["reason"] = "no_keywords" if not queries else "no_pool"
            return [], status
        http_get = http_get or _default_http_get
        clock = timerange.to_utc(now) or datetime.now(timezone.utc)
        t0 = time.monotonic()
        deadline = t0 + max(0.5, float(deadline_seconds))
        lock, stop = threading.Lock(), threading.Event()
        per_query = [[] for _ in queries]
        seen = set()
        counters = {"requests": 0, "failed": 0, "hits": 0, "no_text": 0, "outside": 0, "dup": 0, "rate": 0, "timeout": 0}

        def work(i, q):
            for page in range(max(1, int(max_pages))):
                if stop.is_set():
                    return
                left = deadline - time.monotonic()
                if left <= 0:
                    with lock:
                        counters["timeout"] += 1
                    return
                url = build_url(q, start=start, end=end, page=page, hits_per_page=hits_per_page, by_date=by_date)
                data = None
                for attempt in (1, 2):                    # one retry on a transient failure, only while time remains
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    with lock:
                        counters["requests"] += 1
                    try:
                        data = http_get(url, min(PER_REQUEST_TIMEOUT_CAP, left))
                        break
                    except RateLimited:
                        with lock:
                            counters["rate"] += 1
                            counters["failed"] += 1
                        stop.set()
                        return
                    except Exception:
                        with lock:
                            counters["failed"] += 1
                        if attempt == 1 and deadline - time.monotonic() > 1.0:
                            time.sleep(min(0.4, max(0.0, deadline - time.monotonic() - 0.5)))
                if not isinstance(data, dict):
                    return
                hits = data.get("hits")
                if not isinstance(hits, list) or not hits:
                    return
                for hit in hits:
                    rec = normalize_hit(hit, allow_title_only=general)
                    with lock:
                        counters["hits"] += 1
                        if rec is None:
                            continue
                        if rec["hn_id"] in seen:
                            counters["dup"] += 1
                            continue
                        if not is_candidate(rec):
                            counters["no_text"] += 1
                            continue
                        if not in_window(rec, start, end, now=clock):
                            counters["outside"] += 1
                            continue
                        seen.add(rec["hn_id"])
                        per_query[i].append(rec)
                        if len(per_query[i]) >= pool_n:      # this keyword alone could fill the pool: it needs no more pages
                            return
                nb = data.get("nbPages")
                if isinstance(nb, int) and page >= nb - 1:
                    return

        ex = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(queries)))
        futures = [ex.submit(work, i, q) for i, q in enumerate(queries)]
        wait(futures, timeout=max(0.1, deadline - time.monotonic() + 0.25))
        stop.set()                                          # whatever has not finished is abandoned (requests are time-bounded)
        ex.shutdown(wait=False)

        with lock:
            lists = [list(x) for x in per_query]
            snap = dict(counters)
        merged, depth = [], 0
        while len(merged) < pool_n and any(depth < len(x) for x in lists):
            for x in lists:
                if depth < len(x) and len(merged) < pool_n:
                    merged.append(x[depth])
            depth += 1
        status.update(requests=snap["requests"], failed_requests=snap["failed"], fetched_hits=snap["hits"], no_text=snap["no_text"],
                      outside_window=snap["outside"], duplicates=snap["dup"])
        if snap["rate"]:
            status["state"], status["reason"] = ("partial" if merged else "rate_limited"), "rate_limited"
        elif merged:
            degraded = snap["failed"] > 0 or snap["timeout"] > 0 or any(not f.done() for f in futures)
            status["state"], status["reason"] = ("partial" if degraded else "ok"), ("some_requests_failed" if degraded else None)
        elif snap["requests"] and snap["failed"] >= snap["requests"]:
            status["state"], status["reason"] = "failed", "all_requests_failed"
        elif snap["timeout"] or any(not f.done() for f in futures):
            status["state"], status["reason"] = "failed", "timeout"
        else:
            status["state"], status["reason"] = "empty", None
        return merged, status
    except Exception as exc:                                # fail closed: never raise into the request path
        log.warning("hackernews fetch failed closed (%s)", type(exc).__name__)
        status.update(state="failed", reason="exception:" + type(exc).__name__)
        return [], status


def probe_newest(http_get=None, timeout=6.0):
    """(state, iso) — one cheap request for the single newest story, to tell "Hacker News has stories, just none in your window"
    from "nothing came back". state: "ok" (iso = its UTC time), "empty" (the API answered with no stories), "failed". Never raises."""
    try:
        data = (http_get or _default_http_get)(build_url("", by_date=True, hits_per_page=1), timeout)
        hits = data.get("hits") if isinstance(data, dict) else None
        if not isinstance(hits, list):
            return "failed", None
        if not hits:
            return "empty", None
        ts = hits[0].get("created_at_i") if isinstance(hits[0], dict) else None
        if isinstance(ts, int) and not isinstance(ts, bool):
            return "ok", datetime.fromtimestamp(ts, timezone.utc).isoformat()
        return "failed", None
    except Exception:
        return "failed", None
