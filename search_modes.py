"""
search_modes.py — LIVE / HISTORICAL / COMBINED search modes (pure; no application imports).

LIVE        newly available discussions fetched from a source's live API at request time (today: Hacker News via Algolia).
            Reddit has no working live path (GPT web search is disabled at source), so a Reddit-only LIVE search is
            reported as SOURCE_UNAVAILABLE, never simulated.
HISTORICAL  stored conversations (MongoDB / MySQL) only. Never claimed to be current.
COMBINED    both paths, merged and deduplicated across retrieval methods and URL forms, provenance per result.

Provenance (`retrieval_path`): "live" | "historical" | "both". A post found by both paths is ONE result, marked "both".
"""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

LIVE, HISTORICAL, COMBINED = "live", "historical", "combined"
MODES = (LIVE, HISTORICAL, COMBINED)
DEFAULT_MODE = COMBINED          # = what the application already did: stored posts plus any live source that is selected
LABELS = {LIVE: "Live", HISTORICAL: "Historical", COMBINED: "Combined"}
PATH_LABELS = {"live": "Live", "historical": "Historical", "both": "Live + Historical"}

_TRACKING = re.compile(r"^(utm_.*|ref|ref_src|ref_url|source|share|context|sort|st|sh|fbclid|gclid)$", re.I)
_HN_ITEM = re.compile(r"news\.ycombinator\.com/item", re.I)


# What each source can do, from the code (not from hope). Used for the user-facing explanations and the capability matrix.
# Reddit has a legacy GPT web-search path (reddit_live.py) but it is switched off at source by an explicit product decision
# (reddit_live.LIVE_SEARCH_EXECUTION_ENABLED = False; paid model call), so it is NOT a live capability here.
SOURCE_MODES = {
    "reddit": {"live": False, "historical": True},
    "hackernews": {"live": True, "historical": True},
}
SOURCE_NAMES = {"reddit": "Reddit", "hackernews": "Hacker News"}


def unsupported_sources(mode, selected_keys):
    """Selected sources that cannot serve the mode (names), e.g. Reddit in LIVE. Unknown sources are not judged."""
    mode = normalize_mode(mode)
    need = "historical" if mode == HISTORICAL else "live"        # Combined also wants the live path where a source has one
    return [SOURCE_NAMES.get(k, k) for k in selected_keys or [] if k in SOURCE_MODES and not SOURCE_MODES[k][need]]


def normalize_mode(value, default=DEFAULT_MODE):
    """Any input -> one of MODES. Unknown / empty -> `default` (never raises, never an invalid mode)."""
    v = str(value or "").strip().lower()
    if v in MODES:
        return v
    return default if default in MODES else DEFAULT_MODE


def canonical_url(url):
    """One identity for one post across URL representations (www / old / m / np, http, trailing slash, tracking params,
    fragments, Reddit comment-slug and HN item forms). '' for anything that is not a usable URL."""
    if not isinstance(url, str) or not url.strip():
        return ""
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    try:
        p = urlsplit(u)
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if not host:
        return ""
    for pre in ("www.", "old.", "new.", "m.", "np.", "i."):
        if host.startswith(pre):
            host = host[len(pre):]
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if host in ("redd.it",):
        return "reddit.com/comments/" + path.strip("/").lower()
    if host.endswith("reddit.com"):
        m = re.search(r"/comments/([a-z0-9]+)(?:/[^/]*)?(?:/([a-z0-9]+))?", path, re.I)
        if m:
            post, comment = m.group(1).lower(), (m.group(2) or "").lower()
            return f"reddit.com/comments/{post}" + (f"/c/{comment}" if comment else "")
    q = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not _TRACKING.match(k))
    if _HN_ITEM.search(host + path):
        q = [(k, v) for k, v in q if k == "id"]
    path = path.rstrip("/") or "/"
    return urlunsplit(("", host, path.lower() if host.endswith("reddit.com") else path, urlencode(q), "")).lstrip("/")


def post_identity(post):
    """Stable key for dedup: canonical URL, else platform + id, else ''."""
    if not isinstance(post, dict):
        return ""
    c = canonical_url(post.get("post_url") or post.get("url"))
    if c:
        return c
    pid = post.get("hn_id") or post.get("message_id") or post.get("id")
    plat = str(post.get("platform") or "").replace("_comment", "")
    return f"{plat}:{pid}" if pid else ""


def tag(posts, path):
    """Stamp provenance on a copy of each post (does not mutate the inputs)."""
    out = []
    for p in posts or []:
        if isinstance(p, dict):
            q = dict(p)
            q["retrieval_path"] = path
            out.append(q)
    return out


def merge(live_posts, historical_posts, limit=None):
    """Merge the two paths into ONE ordered list without double counting.
    Order: the historical ranking is kept as is (it is the existing ranking), live-only posts are interleaved after it by their
    own order; a post found by both keeps the historical record (richer stored fields) with retrieval_path='both'.
    Returns (merged, stats) with stats = {live, historical, both, duplicates_removed}."""
    def _fill(kept, other):
        """A duplicate must not lose metadata: fields the kept record lacks are copied from the duplicate (never overwritten)."""
        for k, v in other.items():
            if k not in kept or kept[k] in (None, "", [], {}):
                if k not in ("retrieval_path",):
                    kept[k] = v

    seen, merged = {}, []
    dup = 0
    for p in tag(historical_posts, "historical"):
        k = post_identity(p)
        if k and k in seen:
            _fill(seen[k], p)
            dup += 1
            continue
        if k:
            seen[k] = p
        merged.append(p)
    live_only = []
    for p in tag(live_posts, "live"):
        k = post_identity(p)
        if k and k in seen:
            _fill(seen[k], p)
            seen[k]["retrieval_path"] = "both"
            dup += 1
            continue
        if k:
            seen[k] = p
        live_only.append(p)
    merged.extend(live_only)
    if limit is not None and limit >= 0:
        merged = merged[:limit]
    stats = {"live": 0, "historical": 0, "both": 0, "duplicates_removed": dup}
    for p in merged:
        stats[p.get("retrieval_path", "historical")] = stats.get(p.get("retrieval_path", "historical"), 0) + 1
    return merged, stats


def mode_coverage(mode, live_status=None, historical_status=None, stats=None, unsupported=None):
    """Honest coverage record for one search. Statuses are free-form dicts/strings from the paths; a path that was not run for
    the mode is 'not_requested', never 'empty'. A path that failed is 'failed', so a failure is not mistaken for 'no match'."""
    mode = normalize_mode(mode)

    def _st(requested, status):
        if not requested:
            return "not_requested"
        if isinstance(status, dict):
            status = status.get("state")
        return str(status or "unknown")

    cov = {"mode": mode, "label": LABELS[mode],
           "live": _st(mode in (LIVE, COMBINED), live_status),
           "historical": _st(mode in (HISTORICAL, COMBINED), historical_status),
           "counts": dict(stats or {})}
    # 'unavailable' is a capability gap (no live source for the selected platforms), NOT a failure; only real errors count as failed.
    failed = [k for k in ("live", "historical") if cov[k] in ("failed", "rate_limited")]
    ran = [k for k in ("live", "historical") if cov[k] != "not_requested"]
    cov["partial"] = bool(failed) and len(failed) < len(ran)
    cov["failed_paths"] = failed
    notes = []
    if mode == HISTORICAL:
        notes.append("Historical results come from stored conversations and are not a view of what was posted most recently.")
    if mode in (LIVE, COMBINED) and cov["live"] in ("unavailable", "source_unavailable", "skipped"):
        notes.append("No live source is available for the selected platforms, so no live results could be retrieved.")
    if cov["partial"]:
        notes.append("One retrieval path failed; the results shown come from the other path only.")
    if mode == LIVE:
        notes.append("Live results are a bounded fetch from the source's search API, not a complete archive.")
    uns = [str(u) for u in (unsupported or [])][:6]
    if uns:
        cov["unsupported_sources"] = uns
        if mode == COMBINED:
            notes.append(f"{', '.join(uns)} has no live source; its results come from stored conversations only.")
        elif mode == LIVE:
            notes.append(f"{', '.join(uns)} does not support Live search and was not searched.")
    cov["notes"] = notes
    return cov
