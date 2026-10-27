"""
platforms.py — the one place that knows what a source platform is.
==================================================================

Phase 2.2. Before this module the same facts lived in several files: URL-host -> platform tables (logics.py, flintel.py,
answer_integrity.py), the Reddit-only publication-date floor (timerange.py, intent_prototype/opportunity.py), the "r/" and
"Subreddit" labels (templates), the Reddit post-id pattern (answer_integrity.py).

What this is: a small registry (`REGISTRY`) plus pure helpers. What it is NOT: a collector, a plugin framework, or a switch
that turns a platform on. Only platforms with `enabled=True` are searched; today that is Reddit alone. The other entries exist
because the application already recognises them (selector options marked "coming soon", icons for X / LinkedIn / Facebook
links); registering them here changes nothing about what is collected or offered.

Adding a platform later means: add a `Platform` entry (and its collector, outside this module) — the freshness, ranking and
evidence-integrity code reads platforms only through the helpers below.

No imports from the application, so every module (and the intent prototype) can import it.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# The earliest publication time ever accepted when a platform has no floor of its own. It is the value the application has
# always used (Reddit launched in June 2005); it is kept as the shared default so behaviour does not change, and no other
# platform is given an invented cutoff.
DEFAULT_EARLIEST_DATE = datetime(2005, 6, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Platform:
    key: str                              # canonical key; also what the selector / job `targeting_platform` stores
    label: str                            # user-facing name
    enabled: bool                         # searched today? (only these may be offered as selectable)
    aliases: tuple                        # lower-case spellings: stored `platform` values and selector strings
    hosts: tuple                          # URL hosts (a host matches itself and its subdomains)
    community_label: Optional[str] = None     # "Subreddit"; None = the generic "Source"
    community_prefix: Optional[str] = None    # "r/"
    earliest_date: Optional[datetime] = None  # platform-specific floor, only where one is justified
    removed_markers: tuple = ()           # post text / title values meaning "removed by the platform"


# Order matters: it is the fixed order of a multi-platform selection key (e.g. "x_twitter,linkedin").
REGISTRY = {
    "x_twitter": Platform("x_twitter", "X / Twitter", False,
                          ("x_twitter", "x / twitter", "x/twitter", "twitter", "x"), ("x.com", "twitter.com")),
    "reddit": Platform("reddit", "Reddit", True, ("reddit",), ("reddit.com", "redd.it"),
                       community_label="Subreddit", community_prefix="r/", earliest_date=DEFAULT_EARLIEST_DATE,
                       removed_markers=("[removed]", "[deleted]", "[deleted by user]", "[removed by reddit]",
                                        "[ removed by reddit ]")),
    "linkedin": Platform("linkedin", "LinkedIn", False, ("linkedin",), ("linkedin.com",)),
    "facebook": Platform("facebook", "Facebook", False, ("facebook",), ("facebook.com", "fb.com")),
}

_ALIASES = {alias: p.key for p in REGISTRY.values() for alias in p.aliases}
_URL_FIELDS = ("post_url", "url", "link", "permalink")
_PLATFORM_FIELDS = ("platform", "source_platform")


def get(key) -> Optional[Platform]:
    return REGISTRY.get(key)


def enabled_keys() -> tuple:
    return tuple(k for k, p in REGISTRY.items() if p.enabled)


def label(key, default=None):
    p = REGISTRY.get(key)
    return p.label if p else default


def canonical_key(raw) -> Optional[str]:
    """Registry key for a stored platform value or a selector spelling ("Twitter", "x / twitter", "reddit"), else None."""
    if not isinstance(raw, str):
        return None
    return _ALIASES.get(raw.strip().lower())


def _host(url) -> str:
    return re.sub(r"^[a-z][a-z0-9+.-]*://", "", str(url or "").strip().lower()).split("/")[0].split("?")[0].split("#")[0] \
        .split("@")[-1].split(":")[0]


def from_url(url) -> Optional[str]:
    """Registry key for the host of `url` (the host itself or a subdomain of it), else None."""
    host = _host(url)
    if not host:
        return None
    for p in REGISTRY.values():
        for h in p.hosts:
            if host == h or host.endswith("." + h):
                return p.key
    return None


def url_of(record) -> str:
    if not isinstance(record, dict):
        return ""
    for f in _URL_FIELDS:
        v = record.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def of(record) -> Optional[str]:
    """Registry key of a stored record: its own platform value, else the host of its URL, else None. An unrecognised stored
    value (a platform not in the registry) yields None here; callers that must keep the raw value read the record."""
    if not isinstance(record, dict):
        return None
    for f in _PLATFORM_FIELDS:
        k = canonical_key(record.get(f))
        if k:
            return k
    return from_url(url_of(record))


def earliest_date(key=None) -> datetime:
    """The earliest valid publication time for a platform (its own floor, else the shared default)."""
    p = REGISTRY.get(key)
    return (p.earliest_date if p and p.earliest_date else DEFAULT_EARLIEST_DATE)


def selection_key(raw) -> str:
    """Canonical form of a platform SELECTION: "all", or the chosen registry keys in registry order, comma separated.
    Accepts a stored key ("reddit", "x_twitter,linkedin"), selector strings ("X / Twitter, LinkedIn") and any alias, in any
    order. Empty, missing, "all", unrecognised or every platform -> "all" (the current request semantics: a garbled value
    never narrows a search)."""
    picked = set()
    for part in str(raw or "").split(","):
        token = part.strip().lower()
        if token in ("all", "all platforms"):
            return "all"
        key = canonical_key(token)
        if key:
            picked.add(key)
    ordered = [k for k in REGISTRY if k in picked]
    return "all" if not ordered or len(ordered) == len(REGISTRY) else ",".join(ordered)


# ── source identity, community, removed content ────────────────────────────────────────────────────────────────────────
_REDDIT_ID = re.compile(r"/comments/([a-z0-9]+)", re.I)
_REDDIT_SUB = re.compile(r"/r/([^/?#]+)", re.I)


def normalized_url(url) -> str:
    """The URL without scheme, "www.", query string, fragment or trailing slash (lower-case)."""
    u = re.sub(r"^https?://(www\.)?", "", str(url or "").strip().lower())
    return re.split(r"[?#]", u)[0].rstrip("/")


def source_id(record) -> str:
    """Stable identity of a stored record, or "" when it has no URL (never invented). Reddit: the post id; every other
    platform: the normalised URL."""
    url = url_of(record)
    if not url:
        return ""
    if of(record) == "reddit":
        found = _REDDIT_ID.findall(url)
        if found:
            return f"reddit:{found[0].lower()}"
    return normalized_url(url)


def community(record) -> Optional[str]:
    """The community / channel a post belongs to (a subreddit for Reddit), or None. Reads the canonical `community` field,
    then the legacy Reddit `subreddit` field, then the Reddit URL."""
    if not isinstance(record, dict):
        return None
    value = record.get("community")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if of(record) != "reddit":
        return None
    sub = record.get("subreddit")
    if not sub:
        found = _REDDIT_SUB.findall(url_of(record))
        sub = found[0] if found else None
    return sub or None


def is_removed(record) -> bool:
    p = REGISTRY.get(of(record))
    if not p or not p.removed_markers or not isinstance(record, dict):
        return False
    text = (record.get("post_text") or "").strip().lower()
    title = (record.get("title") or "").strip().lower()
    return bool(record.get("removed_by_category")) or text in p.removed_markers or title in p.removed_markers


def community_display(key) -> tuple:
    """(label, prefix) used to show a community: ("Subreddit", "r/") for Reddit, ("Source", "") otherwise."""
    p = REGISTRY.get(key)
    return ((p.community_label or "Source"), (p.community_prefix or "")) if p else ("Source", "")


# ── the canonical signal ─────────────────────────────────────────────────────────────────────────────────────────────────
REQUIRED_FIELDS = ("platform", "url")
OPTIONAL_FIELDS = ("community", "source_id", "published_at", "author_id")


def normalize_signal(record, now=None) -> dict:
    """The canonical view of a stored record, ADDITIVE and non-destructive: every original field is kept (legacy `subreddit`,
    `post_url`, `created_utc` ...), and the canonical ones are added when they can be derived:

        platform      registry key (the stored value stays in `raw_platform` when it was an alias, e.g. "twitter")
        url           the record's URL
        community     see `community()`; the legacy `subreddit` alias is kept for Reddit
        source_id     see `source_id()`
        published_at  ISO UTC string of the VALIDATED publication time (never ingestion / scan time)
        author_id     only if the record already carries one

    A field that cannot be derived is left out: nothing is fabricated. Required: platform + url. Optional: the rest."""
    import timerange   # local: timerange imports this module
    out = dict(record) if isinstance(record, dict) else {}
    key = of(out)
    if key:
        if out.get("platform") and out.get("platform") != key:
            out.setdefault("raw_platform", out["platform"])
        out["platform"] = key
    url = url_of(out)
    if url:
        out["url"] = url
        sid = source_id(out)
        if sid:
            out["source_id"] = sid
    comm = community(out)
    if comm:
        out["community"] = comm
        if key == "reddit":
            out.setdefault("subreddit", comm)
    when = timerange.post_published_at(out, now=now)
    if when is not None:
        out["published_at"] = when.isoformat()
    else:
        out.pop("published_at", None)
    return out
