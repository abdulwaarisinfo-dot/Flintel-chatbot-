"""
business_profile.py — the user's business profile: validation, a structured representation, deterministic website understanding and an
owner-scoped store (pure except for the store, which takes an injected collection).

Facts the USER supplied are kept apart from anything DERIVED:
  profile["user"]      exactly what the user typed (trimmed, length-limited), never rewritten
  profile["website"]   {"url", "status", "facts"}: facts read from the public page ("analyzed" only when a page was really retrieved)
  profile["derived"]   deterministic search terms computed from the user's own words (no model call); labelled as derived
No model is called here: website understanding is a bounded, deterministic reading of the page (title, description, headings, visible text).
If the page cannot be retrieved, the status says so and nothing is invented.
"""
import re
from datetime import datetime, timezone
from html import unescape

MAX_TEXT = 2000
MAX_LIST_ITEMS = 12
MAX_ITEM = 80
GEO_SCOPES = ("worldwide", "country", "region", "city")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"\s+")
_STOP = frozenset("""a an the and or of for to in on at by with from our we us you your they their it its is are was were be been i my me
that this these those as it's who whom which what when where why how not no yes can will would should could do does did have has had
sell sells selling provide provides providing offer offers offering help helps helping""".split())
_SPLIT = re.compile(r"[,\n;]+")


def _clean(value, limit=MAX_TEXT) -> str:
    return _WS.sub(" ", _CONTROL.sub(" ", unescape(str(value or "")))).strip()[:limit]


def _items(value, limit=MAX_LIST_ITEMS, item_limit=MAX_ITEM) -> list:
    raw = value if isinstance(value, (list, tuple)) else _SPLIT.split(str(value or ""))
    out, seen = [], set()
    for x in raw:
        t = _clean(x, item_limit)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= limit:
            break
    return out


def normalize_website_url(raw):
    """http(s) URL string or None. Adds https:// to a bare domain. Does NOT contact the site (see safe_fetch)."""
    t = _clean(raw, 300)
    if not t:
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", t, re.I):
        t = "https://" + t
    m = re.match(r"^(https?)://([^/\s?#@]+)([^\s]*)$", t, re.I)
    return t if m and "." in m.group(2) else None


def validate(raw: dict):
    """(user_fields, errors). `errors` maps field -> message. Required: what the business sells (>= 10 characters). The name is required
    unless a website is given (it can then be confirmed from the site)."""
    raw = raw if isinstance(raw, dict) else {}
    errors = {}
    user = {
        "business_name": _clean(raw.get("business_name"), 120),
        "sells": _clean(raw.get("sells")),
        "ideal_customers": _clean(raw.get("ideal_customers")),
        "problems_solved": _clean(raw.get("problems_solved")),
        "competitors": _items(raw.get("competitors")),
        "product_terms": _items(raw.get("product_terms")),
        "exclusions": _items(raw.get("exclusions")),
        "notes": _clean(raw.get("notes")),
    }
    scope = _clean(raw.get("geo_scope"), 20).lower() or "worldwide"
    places = _items(raw.get("geo_places"), limit=8)
    if scope not in GEO_SCOPES:
        errors["geo_scope"] = "Choose worldwide, country, region or city."
        scope = "worldwide"
    if scope != "worldwide" and not places:
        errors["geo_places"] = "Tell us which place you serve, or choose worldwide."
    user["geography"] = {"scope": scope, "places": places if scope != "worldwide" else []}
    website = None
    if _clean(raw.get("website"), 300):
        website = normalize_website_url(raw.get("website"))
        if website is None:
            errors["website"] = "Enter a valid website address, or leave it empty."
    user["website"] = website
    if len(user["sells"]) < 10:
        errors["sells"] = "Describe what your business sells in a sentence."
    if not user["business_name"] and not website:
        errors["business_name"] = "Enter your business name."
    return user, errors


def derive_terms(user: dict) -> dict:
    """Deterministic search terms from the user's OWN words (no model). Labelled `derived`; never shown as something the user wrote."""
    def words(text):
        return [w for w in re.findall(r"[a-z][a-z0-9+&'-]{2,}", str(text or "").lower()) if w not in _STOP]

    def top(texts, n):
        counts = {}
        for t in texts:
            for w in words(t):
                counts[w] = counts.get(w, 0) + 1
        return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:n]

    offering = top([user.get("sells"), " ".join(user.get("product_terms") or [])], 10)
    audience = top([user.get("ideal_customers")], 8)
    problems = top([user.get("problems_solved")], 10)
    return {"offering_terms": offering, "audience_terms": audience, "problem_terms": problems,
            "competitor_terms": [c.lower() for c in user.get("competitors") or []],
            "exclusion_terms": [e.lower() for e in user.get("exclusions") or []], "source": "derived from the user's own text; no model used"}


# ── website understanding (deterministic) ─────────────────────────────────────────────────────────────────────────────────
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_DESC = re.compile(r"<meta[^>]+name=[\"']description[\"'][^>]*content=[\"'](.*?)[\"']", re.I | re.S)
_META_DESC2 = re.compile(r"<meta[^>]+content=[\"'](.*?)[\"'][^>]*name=[\"']description[\"']", re.I | re.S)
_HEAD = re.compile(r"<h([12])[^>]*>(.*?)</h\1>", re.I | re.S)
_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def extract_website_facts(html: str) -> dict:
    """Facts that are literally on the page: title, meta description, h1/h2 headings and a text excerpt. Nothing is inferred."""
    html = str(html or "")
    title = _clean(_TAG.sub(" ", (_TITLE.search(html) or [None, ""])[1]), 160)
    m = _META_DESC.search(html) or _META_DESC2.search(html)
    description = _clean(m.group(1), 300) if m else ""
    headings = []
    for _lvl, inner in _HEAD.findall(html):
        h = _clean(_TAG.sub(" ", inner), 140)
        if h and h not in headings:
            headings.append(h)
        if len(headings) >= 12:
            break
    text = _clean(_TAG.sub(" ", _SCRIPT.sub(" ", html)), 1500)
    return {"title": title, "description": description, "headings": headings, "text_excerpt": text}


def analyze_website(url: str, fetch=None) -> dict:
    """{"url", "status": "analyzed" | "unavailable" | "unsafe", "facts", "message"}. `fetch(url)` must return {"text": html} or raise;
    default: safe_fetch.fetch_public_page. A failure is reported, never guessed around."""
    import safe_fetch
    fetch = fetch or safe_fetch.fetch_public_page
    out = {"url": url, "status": "unavailable", "facts": {}, "message": "", "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        page = fetch(url)
        facts = extract_website_facts(page.get("text"))
        if not (facts["title"] or facts["description"] or facts["headings"] or facts["text_excerpt"]):
            out["message"] = "The page had no readable text. Please describe your business below."
            return out
        out.update(status="analyzed", facts=facts, message="We read your public website. Check the details below.")
    except safe_fetch.UnsafeUrl as exc:
        out.update(status="unsafe", message=str(exc))
    except Exception as exc:
        out["message"] = (str(exc) if isinstance(exc, safe_fetch.FetchFailed) else "The website could not be read.") + " Please describe your business below."
    return out


def build(user: dict, website: dict = None) -> dict:
    return {"user": user, "website": website or {"url": user.get("website"), "status": "not_provided" if not user.get("website") else "not_analyzed", "facts": {}},
            "derived": derive_terms(user), "version": 1}


# ── persistence: owner-scoped, one profile per signed-in owner ────────────────────────────────────────────────────────────
class ProfileStore:
    """`collection` offers find_one(filter) and update_one(filter, {"$set": ...}, upsert=True) (a pymongo Collection, or a test double).
    Every call filters on `owner_key`; there is no call that reads another owner's profile."""

    def __init__(self, collection):
        self.c = collection

    @staticmethod
    def _owner(owner_key):
        if not isinstance(owner_key, str) or "@" not in owner_key:
            raise PermissionError("A signed-in account is required.")
        return owner_key

    def get(self, owner_key):
        doc = self.c.find_one({"owner_key": self._owner(owner_key)})
        if not doc:
            return None
        doc = dict(doc)
        doc.pop("_id", None)
        return doc

    def save(self, owner_key, profile: dict):
        owner = self._owner(owner_key)
        now = datetime.now(timezone.utc).isoformat()
        existing = self.c.find_one({"owner_key": owner})
        doc = dict(profile, owner_key=owner, updated_at=now, created_at=(existing or {}).get("created_at") or now)
        self.c.update_one({"owner_key": owner}, {"$set": doc}, upsert=True)
        return doc
