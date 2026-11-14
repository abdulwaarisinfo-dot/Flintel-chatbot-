"""
opportunity_layer.py — strict, evidence-driven opportunity categories, entity grouping and recency for the rows of a CHECKED report
(pure; no I/O, no application imports).

It only reads what answer_integrity has already verified per row (role, purchase_intent, verification, evidence kind, date) and the
row's own source text. It can only keep or lower a category; nothing here upgrades a post.

Categories (a row has exactly one):
  confirmed_buyer      explicit purchase / hiring / vendor-request evidence in a verified, analysis-matched row
  strong_research      the author is evaluating or asking for recommendations (research intent)
  problem_signal       a real problem described without a request for a solution or vendor
  potential_partnership the post itself expresses partnership / integration / collaboration language (never inferred)
  competitor_signal    a vendor or builder promoting their own product, or a testimonial
  general_discussion   relevant conversation without evidence of commercial intent
  unverified           the source or its analysis could not be verified: it supports nothing

Entities: rows are ONE entity only on verified identity: the same author, or the same explicitly verified canonical company
(`canonical_company` with `company_identity_verified` True, supplied with the record). Titles, shared words and category terms never
establish identity; a record without identity evidence is its own entity. A shared leading product name in vendor titles is reported as a
`possible_relationships` label only: nothing is merged and no count changes.

Recency: a row older than HISTORICAL_DAYS is "historical" and is never a current opportunity, whatever its category.
"""

import re
from datetime import datetime, timezone

HISTORICAL_DAYS = 90

CONFIRMED, STRONG, PROBLEM, PARTNER, COMPETITOR, GENERAL, UNVERIFIED = (
    "confirmed_buyer", "strong_research", "problem_signal", "potential_partnership", "competitor_signal", "general_discussion", "unverified")
CATEGORIES = (CONFIRMED, STRONG, PROBLEM, PARTNER, COMPETITOR, GENERAL, UNVERIFIED)
LABELS = {CONFIRMED: "Confirmed buyer signal", STRONG: "Strong research signal", PROBLEM: "Problem signal",
          PARTNER: "Potential partnership", COMPETITOR: "Competitor signal", GENERAL: "General market discussion", UNVERIFIED: "Unverified"}

_I = re.IGNORECASE
_PARTNER_AUTHOR = re.compile(r"\b(?:looking\s+for|seeking|open\s+to|interested\s+in|want(?:ed)?\s+to)\b[^.]{0,60}\b(?:partner(?:s|ship|ships)?|integrat\w+|collaborat\w+|resell\w*|affiliate\w*|co-?market\w*)\b"
                             r"|\b(?:partner(?:ship|ships)?|integration|collaboration)\s+(?:opportunit\w+|request|inquir\w+)\b"
                             r"|\bpartners?\s+wanted\b", _I)
_PROBLEM = re.compile(r"\b(?:problem|issue|struggl\w+|frustrat\w+|painful|pain\s+point|broken|too\s+(?:expensive|slow|complex)|doesn'?t\s+work|keeps?\s+(?:crashing|failing)|nightmare|hate)\b", _I)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+&'-]{2,}")
_PLACEHOLDER = frozenset({"", "unknown", "[deleted]", "deleted", "none", "null", "anonymous", "someone", "n/a"})
_GENERIC_TITLE_WORDS = frozenset("""
the and for with how why what when where who which this that your you our new best top free open source show ask tell launch launching built building
build tool tools app apps platform software saas ai crm help need looking any anyone recommend recommendation recommendations alternative alternatives
""".split())


def _when(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def recency(published_at, now=None) -> str:
    """'current' (within HISTORICAL_DAYS), 'historical', or 'undated'."""
    when = _when(published_at)
    if when is None:
        return "undated"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return "current" if (now - when).days <= HISTORICAL_DAYS else "historical"


def categorize(row: dict, source_text: str = "") -> str:
    """One category for a displayed row. A row that is not an evidence row (source or analysis not verified) is `unverified`."""
    if row.get("verification_status") != "verified" or row.get("analysis_verified") is not True:
        return UNVERIFIED
    role, intent = row.get("role"), row.get("purchase_intent")
    if role in ("competitor_promo", "testimonial"):
        return PARTNER if _PARTNER_AUTHOR.search(source_text or "") else COMPETITOR
    if role == "buyer" and intent == "explicit":
        return CONFIRMED
    if role == "buyer" and intent == "research":
        return STRONG
    if _PARTNER_AUTHOR.search(source_text or ""):
        return PARTNER
    if row.get("relevance") == "relevant" and _PROBLEM.search(source_text or ""):
        return PROBLEM
    return GENERAL


def _author_key(row, post):
    a = str((post or {}).get("author") or row.get("author") or "").strip().lower()
    a = re.sub(r"^(?:/?u/|@)", "", a)
    return None if a in _PLACEHOLDER else a


_TITLE_PREFIX = re.compile(r"^\s*(?:(?:show|ask|tell|launch|introducing|new|announcing)\s*(?:hn)?\s*[:\-]\s*)+", _I)


def _leading_name(title: str):
    """The first word of a title after launch prefixes ("Show HN:", "Launch:"), when it looks like a product name. Used ONLY to LABEL a
    possible relationship; it never groups anything."""
    m = _WORD.search(_TITLE_PREFIX.sub("", title or ""))
    if not m:
        return None
    w = m.group(0)
    lw = w.lower()
    return lw if (w[0].isupper() or re.search(r"[a-z][A-Z]", w)) and lw not in _GENERIC_TITLE_WORDS and len(lw) >= 4 else None


def _company_key(row, post):
    """An explicitly verified canonical company identity supplied with the record (`canonical_company` + `company_identity_verified`)."""
    for src in (post or {}, row or {}):
        c = src.get("canonical_company")
        if c and src.get("company_identity_verified") is True:
            return re.sub(r"[^a-z0-9]+", " ", str(c).lower()).strip() or None
    return None


def group_entities(rows: list, posts_by_link: dict) -> dict:
    """{row index: entity id}. Identity is established ONLY by (1) the same verified author, or (2) the same explicitly verified canonical
    company. Similar titles, shared words, industry or product-category terms never establish identity. A record with no identity
    evidence is its own entity."""
    parent = {r["index"]: r["index"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    seen = {}
    for r in rows:
        post = posts_by_link.get(r.get("link")) or {}
        for kind, key in (("author", _author_key(r, post)), ("company", _company_key(r, post))):
            if not key:
                continue
            k = (kind, key)
            if k in seen:
                union(seen[k], r["index"])
            else:
                seen[k] = r["index"]
    return {i: f"e{find(i)}" for i in parent}


def possible_relationships(rows: list, posts_by_link: dict, ents: dict) -> list:
    """Vendor / builder rows in DIFFERENT entities whose titles open with the same product name. Reported as a possible relationship only;
    nothing is merged and no count changes."""
    by_name = {}
    for r in rows:
        if r.get("role") in ("competitor_promo", "testimonial"):
            post = posts_by_link.get(r.get("link")) or {}
            name = _leading_name(r.get("title") or post.get("title"))
            if name:
                by_name.setdefault(name, []).append(r["index"])
    out = []
    for name, idx in by_name.items():
        if len({ents[i] for i in idx}) > 1:
            out.append({"rows": idx, "basis": "the titles open with the same product name", "name": name, "verified": False})
    return out


def build(rows: list, posts_by_link: dict, now=None) -> dict:
    """Annotates each row in place (`opportunity_category`, `entity_id`, `recency`, `current_opportunity`) and returns the summary block.
    The metrics are DIFFERENT and are reported separately: listed posts, distinct sources, distinct known authors, posts with no known
    author, identity groups, records without a verified identity, buyer-signal rows and the groups behind them, current opportunity rows."""
    ents = group_entities(rows, posts_by_link)
    cats = {c: 0 for c in CATEGORIES}
    groups = {}
    authors, unknown_author, has_identity = set(), 0, {}
    for r in rows:
        post = posts_by_link.get(r.get("link")) or {}
        text = " ".join(str(x) for x in (post.get("title") or r.get("title"), post.get("post_text")) if x)
        cat = categorize(r, text)
        rec = recency(r.get("published_at") or post.get("published_at"), now)
        r["opportunity_category"] = cat
        r["entity_id"] = ents[r["index"]]
        r["recency"] = rec
        r["current_opportunity"] = bool(cat in (CONFIRMED, STRONG) and rec == "current")
        cats[cat] += 1
        groups.setdefault(ents[r["index"]], []).append(r)
        a = _author_key(r, post)
        has_identity[r["index"]] = bool(a or _company_key(r, post))
        if a:
            authors.add(a)
        else:
            unknown_author += 1
    confirmed = [r for r in rows if r["opportunity_category"] == CONFIRMED]
    confirmed_groups = {r["entity_id"] for r in confirmed}
    conf_current = sum(1 for r in confirmed if r["recency"] == "current")
    conf_historical = sum(1 for r in confirmed if r["recency"] == "historical")
    multi = [{"entity_id": k, "rows": [x["index"] for x in g], "source_ids": [x.get("source_id") for x in g], "posts": len(g)}
             for k, g in groups.items() if len(g) > 1]
    # A group has a verified identity only when its records share a known author or a verified company. A record with neither is a
    # singleton group: it is counted on its own and is NOT a verified identity.
    identity_groups = [g for g in groups.values() if any(has_identity[x["index"]] for x in g)]
    no_identity = [g for g in groups.values() if not any(has_identity[x["index"]] for x in g)]
    n_conf = len(confirmed)
    if n_conf == 0:
        statement = "No confirmed buyer signal was found in the listed posts."
    else:
        bits = []
        if conf_current:
            bits.append(f"{conf_current} current")
        if conf_historical:
            bits.append(f"{conf_historical} historical (older than {HISTORICAL_DAYS} days, not a current opportunity)")
        if n_conf - conf_current - conf_historical:
            bits.append(f"{n_conf - conf_current - conf_historical} without a verified date")
        statement = (f"{n_conf} confirmed buyer signal{'s' if n_conf != 1 else ''} in the listed posts: " + ", ".join(bits) + ".")
    return {"categories": cats, "labels": LABELS,
            "listed_posts": len(rows), "distinct_sources": len({r.get("source_id") or r.get("link") for r in rows}),
            "unique_authors": len(authors), "unknown_author_posts": unknown_author,
            "identity_groups": len(identity_groups), "records_without_identity": len(no_identity), "multi_post_entities": multi,
            "possible_relationships": possible_relationships(rows, posts_by_link, ents),
            "confirmed_buyer_signals": n_conf, "confirmed_buyer_groups": len(confirmed_groups),
            "confirmed_current": conf_current, "confirmed_historical": conf_historical,
            "current_opportunity_rows": sum(1 for r in rows if r["current_opportunity"]),
            "historical_rows": sum(1 for r in rows if r["recency"] == "historical"),
            "undated_rows": sum(1 for r in rows if r["recency"] == "undated"), "statement": statement,
            "historical_after_days": HISTORICAL_DAYS}
