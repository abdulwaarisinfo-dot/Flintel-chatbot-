"""
answer_integrity.py — deterministic guardrails on a structured research answer.

The model writes the report; this module checks it against the stored posts
before anyone sees it. It can only remove, downgrade or relabel — it never
upgrades a post or a finding. Pure Python, no I/O, safe to import anywhere.

Per listed post it reports separate dimensions — role, purchase_intent,
product_fit, verification_status (source confidence), evidence — plus an
overall rating (High / Medium / Low / Uncertain) derived from them. It drops
posts that cannot be tied to a stored post, duplicates, and posts unrelated
to the request; renumbers the table 1..k (strongest first); rewrites every
"Post N" reference to those numbers; drops findings left with no supporting
post; and writes server-side counts for the same rows the table shows.

Platform-specific rules (removal markers, community metadata such as a
subreddit) live in PlatformRules subclasses; everything else is shared.
"""

import json
import re

ROLES = ("buyer", "discussion", "competitor_promo", "testimonial", "irrelevant")
NON_DEMAND_ROLES = ("competitor_promo", "testimonial")
_INTENT_ORDER = {"none": 0, "research": 1, "explicit": 2}
_FIT_ORDER = {"unrelated": 0, "partial": 1, "fits": 2}
_LABEL_ORDER = {"High": 3, "Medium": 2, "Uncertain": 1, "Low": 0}


# ── platform-specific rules ──────────────────────────────────────────────────

class PlatformRules:
    """Generic rules: no platform-specific removal markers or community field."""
    name = "generic"

    def community(self, sig):
        return sig.get("community") or None

    def is_removed(self, sig) -> bool:
        return False

    def source_id(self, sig) -> str:
        """Stable identity of the stored record: its URL without scheme, www,
        query string, fragment or trailing slash."""
        url = re.sub(r"^https?://(www\.)?", "", str(sig.get("post_url") or "").strip().lower())
        return re.split(r"[?#]", url)[0].rstrip("/")


class RedditRules(PlatformRules):
    name = "reddit"
    _REMOVED = {"[removed]", "[deleted]", "[deleted by user]", "[removed by reddit]", "[ removed by reddit ]"}

    def community(self, sig):
        sub = sig.get("subreddit")
        if not sub:
            found = re.findall(r"/r/([^/?#]+)", str(sig.get("post_url") or ""))
            sub = found[0] if found else None
        return sub

    def source_id(self, sig) -> str:
        found = re.findall(r"/comments/([a-z0-9]+)", str(sig.get("post_url") or "").lower())
        return f"reddit:{found[0]}" if found else super().source_id(sig)

    def is_removed(self, sig) -> bool:
        text = (sig.get("post_text") or "").strip().lower()
        title = (sig.get("title") or "").strip().lower()
        return bool(sig.get("removed_by_category")) or text in self._REMOVED or title in self._REMOVED


PLATFORM_RULES = {"reddit": RedditRules()}
_GENERIC_RULES = PlatformRules()
_HOSTS = (("reddit.com", "reddit"), ("redd.it", "reddit"), ("x.com", "x"), ("twitter.com", "x"),
          ("linkedin.com", "linkedin"), ("facebook.com", "facebook"))


def platform_of(sig) -> str:
    """Stored platform, else inferred from the URL host, else "unknown"."""
    plat = str((sig or {}).get("platform") or "").strip().lower()
    if plat:
        return "x" if plat == "twitter" else plat
    host = re.sub(r"^https?://(www\.)?", "", str((sig or {}).get("post_url") or "").lower()).split("/")[0]
    for suffix, name in _HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return name
    return "unknown"


def rules_for(sig) -> PlatformRules:
    return PLATFORM_RULES.get(platform_of(sig), _GENERIC_RULES)


# ── shared text rules ────────────────────────────────────────────────────────

_SOLUTION_NOUN = r"(?:tools?|apps?|software|platforms?|services?|solutions?|products?|vendors?|saas|extensions?|bots?|crm|plugins?)"

_REQUEST = re.compile(
    r"\b(?:looking for|in search of|searching for|need(?:ing)?|want(?:ing)? to (?:buy|find|try)|"
    r"recommend\w*|suggest\w*|any (?:good |decent )?" + _SOLUTION_NOUN + r"|"
    r"what (?:\w+ ){0,3}" + _SOLUTION_NOUN + r"|which (?:\w+ ){0,3}" + _SOLUTION_NOUN + r"|"
    r"best (?:[\w-]+ ){0,4}" + _SOLUTION_NOUN + r"|alternatives? (?:to|for)|vs\.?|versus|compar\w+|"
    r"is there (?:a|an|any)|does anyone (?:know|use)|who (?:sells|offers))\b", re.I)
_SOLUTION = re.compile(r"\b" + _SOLUTION_NOUN + r"\b", re.I)
_COMMITMENT = re.compile(
    r"(?:\bbudget\b|[$€£]\s?\d|\b(?:a|us|au|ca)\$\s?\d|\b\d[\d,.]*\s?(?:usd|aud|eur|gbp|dollars)\b|"
    r"\b\d[\d,.]*\s?(?:/|per\s)\s?(?:mo|month|year|yr|seat|user)\b|"
    r"\b(?:willing|happy|ready|able) to (?:pay|buy|purchase|sign|spend)\b|\bwill pay\b|\bpay (?:for|after)\b|"
    r"\bpilot\b|\btrial\b|\bdemo\b|\bquote\b|\bpricing\b|\bpurchas\w*\b|\bbuy(?:ing)?\b|\bsubscribe\b|"
    r"\bswitch(?:ing)? (?:from|to|away)\b|\breplac\w+ (?:our|my)\b|\basap\b|\bthis (?:week|month|quarter)\b)",
    re.I)
_PROMO = re.compile(
    r"\b(?:i|we)\s+(?:just\s+|recently\s+|finally\s+|have\s+|'ve\s+)?(?:built|made|created|launched|developed|shipped|released|coded)\b|"
    r"\b(?:i|we)'ve\s+(?:built|made|created|launched|developed)\b|\bintroducing\b|"
    r"\b(?:check|try)\s+(?:out\s+)?(?:my|our)\b|"
    r"\b(?:my|our)\s+(?:new\s+)?(?:tool|app|saas|product|platform|extension)\s+(?:that|which|helps|finds|lets|can|is)\b|"
    r"\bwe(?:'re| are)\s+(?:launching|offering|giving away)\b|\b(?:founder|creator|maker) of\b",
    re.I)
_TESTIMONIAL = re.compile(
    r"\b(?:tried|been using|started using|switched to|using)\b[^.!?\n]{0,90}?"
    r"\b(?:helped|worked|game[- ]?changer|saved|paid off|landed|got (?:me |us )?(?:[\w-]+ ){0,2}(?:customers|clients|leads|users|sales))",
    re.I)

# Query words that describe the request or a generic business context, not
# the user's product category. A post sharing only these with the query is
# not evidence of fit.
_GENERIC = set("""
a about above across actively actual actually add after again against ai all also always an and any are as
ask asking at author authors available b2b b2c based be because been before being best between brief business
businesses but by can cannot case claims classification classify clear clearly company companies comparing
concise conversation conversations could customer customers date dates did discover discovering distinguish
do does done each engagement every evaluate evaluating evidence exact exclude excerpt existing explanation
expressing explicit faithful find finding finish first for found founders from general genuine give good
growth has have help helpful helps hidden high how hypothetical i ideal identify if in include including
information instead intelligence into invent is it its just likely limitation limitations link low main
make many marketers marketing matching may medium merely metrics missing more most much must my need needs
no non not of on one only opportunities opportunity or original other our out over pain pains patterns people
persona platform please points possible post posts potential powered prioritize problem problems product
products provide publication quality quotation quotations reason reasoning recent recommend recommendations
relevant reply request requests require requirements research response result results return saas score
search searching should show similar since small so solution solutions some source sources spammy specific
strongest suggested summary supported suitable support teams than that the their them then there these they
this those through title to tool tools topics truly unsupported up url urls use used user users using valuable
very via want was way we what when where whether which while who why will with within without working would
you your yours
access accessed address addresses alternative alternatives analysis analyze answer back backed buy buying
compare comparison comparisons complaint complaints contain contains detail detection discussion discussions
duplicate duplicates evaluation explain fabricate fabricated include intent irrelevant keyword keywords label
labelled list look looking making question questions quality quote recommendation represent report short
software solve support supported table task uncertain uncertainty verified verify volume
""".split())
_AUDIENCE = ("saas", "b2b", "startup", "founder", "business", "company", "client", "customer", "lead",
             "sales", "marketing", "marketer", "agency", "product", "brand", "ecommerce", "shopify",
             "revenue", "mrr", "users", "entrepreneur", "smallbusiness", "growth")
# Only an explicitly named business audience turns on the audience check —
# "for my business" describes the user, not who they are looking for.
_ICP_MARKERS = re.compile(r"\b(?:b2b|saas (?:founders?|companies|teams|businesses)|startups|founders|"
                          r"small businesses|agencies|companies|marketers|marketing teams|sales teams)\b", re.I)
_COUNT_CLAIM = re.compile(
    r"\b(found|identified|surfaced|returned|located|analy[sz]ed|reviewed)\s+(\d+|one|two|three|four|five|six|seven|"
    r"eight|nine|ten|eleven|twelve)\s+((?:relevant|matching|qualified|recent)\s+)?((?:reddit|linkedin|x|twitter|facebook)\s+)?"
    r"(posts?|conversations?|threads?|results?|discussions?)\b", re.I)
_STATUS_REASONS = {
    "removed": "The post was removed or deleted, so nothing in it can be verified.",
    "unverified": "The post's text was never retrieved, so the source is unverified.",
    "inaccessible": "The source could not be accessed, so its current content is not verified.",
}


# ── text helpers ─────────────────────────────────────────────────────────────

def _norm(text) -> str:
    return " ".join(re.findall(r"[a-z0-9$€£]+(?:[.'][a-z0-9]+)*", str(text or "").lower()))


def _tokens(text) -> set:
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _stem(tok: str) -> str:
    for suf in ("ings", "ing", "ers", "er", "ed", "es", "s"):
        if len(tok) > len(suf) + 3 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def _sentences(text) -> list:
    parts = re.split(r"(?<=[.!?])\s+|\n+", str(text or ""))
    return [p.strip() for p in parts if p and p.strip()]


_GENERIC_STEMS = {_stem(w) for w in _GENERIC}


def product_terms(query) -> set:
    """Stemmed words of the query that name the product/category (not the request)."""
    out = set()
    for t in _tokens(query):
        st = _stem(t)
        if len(t) >= 3 and not t.isdigit() and t not in _GENERIC and st not in _GENERIC_STEMS:
            out.add(st)
    return out


def _has_term(text, terms: set) -> bool:
    return bool(terms) and any(_stem(t) in terms for t in _tokens(text))


def _clip(sentence: str, limit: int = 220) -> str:
    """Shorten at a word boundary; the result is still a verbatim substring."""
    s = sentence.strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    return cut[: cut.rfind(" ")] if " " in cut else cut


def quote_in_post(quote, title, text) -> bool:
    """True only when `quote` occurs in the post (case/punctuation-insensitive)."""
    q = _norm(quote)
    return len(q) >= 8 and q in _norm(f"{title or ''} {text or ''}")


def verification_status(sig) -> str:
    """Source confidence from the stored record only:
    verified | unverified | inaccessible | removed."""
    if not isinstance(sig, dict):
        return "unverified"
    if sig.get("removed") or sig.get("is_removed") or sig.get("deleted") or rules_for(sig).is_removed(sig):
        return "removed"
    if sig.get("fetch_error") or sig.get("http_status") in (401, 403, 404, 410, 429, 500, 502, 503):
        return "inaccessible"
    if not (sig.get("post_text") or "").strip() or not sig.get("post_url"):
        return "unverified"
    return "verified"


# ── per-post classification (shared across platforms) ───────────────────────

def _detect_role(title: str, text: str):
    """(role, triggering sentence) from the post's own words."""
    title_sents = _sentences(title)
    body_sents = _sentences(text)
    title_request = any(_REQUEST.search(s) and _SOLUTION.search(s) for s in title_sents)
    for s in title_sents + ([] if title_request else body_sents):
        if _PROMO.search(s):
            return "competitor_promo", s
    for s in title_sents + body_sents:
        if _TESTIMONIAL.search(s) and not (_REQUEST.search(s) and _SOLUTION.search(s)):
            return "testimonial", s
    for s in title_sents + body_sents:
        if _REQUEST.search(s) and _SOLUTION.search(s):
            return "buyer", s
    return "discussion", None


_NEGATION = re.compile(r"\b(?:not|no|don'?t|do not|isn'?t|aren'?t|without|never|zero|nothing)\b[\w\s'-]{0,20}$", re.I)


def _has_commitment(text: str) -> bool:
    """A purchase step (budget, trial, price…) that is not negated ("not buying yet", "no budget")."""
    return any(not _NEGATION.search(text[max(0, m.start() - 30):m.start()]) for m in _COMMITMENT.finditer(text or ""))


def _intent_ceiling(title: str, text: str) -> str:
    sents = _sentences(title) + _sentences(text)
    if not any(_REQUEST.search(s) and _SOLUTION.search(s) for s in sents):
        return "none"
    return "explicit" if any(_has_commitment(s) for s in sents) else "research"


def _detect_fit(title, text, community, terms: set, icp: bool):
    """(fit, reason) from the post's own words. Category words must appear in
    the post; the post must also show the declared audience (e.g. a business)
    when the request names one."""
    if not terms:
        return "partial", "The product category could not be read from the request."
    if icp and not any(a in f"{community or ''} {title} {text}".lower() for a in _AUDIENCE):
        return "unrelated", "Nothing in the post shows the business audience the request targets."
    request_text = " ".join([title] + [s for s in _sentences(text) if _REQUEST.search(s)])
    if _has_term(request_text, terms):
        return "fits", None
    if _has_term(text, terms) or _has_term(community, terms):
        return "partial", "The topic is only implied by the post's body or community, not by what the author asks for."
    return "unrelated", "The post does not mention the product category the request is about."


def _pick_evidence(title: str, text: str, role, trigger):
    sents = _sentences(title) + _sentences(text)
    if role == "buyer":
        both = [s for s in sents if _REQUEST.search(s) and _has_commitment(s)]
        commit = [s for s in sents if _has_commitment(s)]
        req = [s for s in sents if _REQUEST.search(s) and _SOLUTION.search(s)]
        for group in (both, commit, req):
            if group:
                return _clip(group[0])
    if trigger:
        return _clip(trigger)
    return _clip(sents[0]) if sents else None


def classify_post(sig: dict, model_post: dict, terms: set, icp: bool) -> dict:
    """Final labels for one listed post. Each dimension is computed from the
    stored post; the model's own labels (if any) can only lower it. A model
    label alone never makes a post relevant or high intent."""
    title = (sig.get("title") or "").strip()
    text = (sig.get("post_text") or "").strip()
    status = verification_status(sig)

    if status != "verified":
        # Nothing reliable to classify: kept and labelled, never judged on content we do not have.
        evidence = _pick_evidence(title, text, None, None) if (status == "inaccessible" and text) else None
        return {"role": None, "purchase_intent": "unknown", "product_fit": "unknown", "relevance": "unverified",
                "intent": "Uncertain", "qualified": False, "evidence": evidence,
                "evidence_source": "excerpt" if evidence else None, "verification_status": status,
                "intent_reason": _STATUS_REASONS[status], "uncertainty": [], "_exclude": None}

    uncertainty = []
    det_role, trigger = _detect_role(title, text)
    m_role = model_post.get("role") if model_post.get("role") in ROLES else None
    if m_role == "irrelevant":
        return {"_exclude": "irrelevant"}
    if det_role in NON_DEMAND_ROLES:
        role = det_role
    elif m_role == "buyer" and det_role != "buyer":
        role = "discussion"
        uncertainty.append("Labelled a buyer, but the post asks for no tool or solution.")
    else:
        role = m_role or det_role

    ceiling = _intent_ceiling(title, text)
    m_intent = model_post.get("purchase_intent")
    intent = min(m_intent, ceiling, key=_INTENT_ORDER.get) if m_intent in _INTENT_ORDER else ceiling
    if role != "buyer":
        intent = "none"

    det_fit, fit_note = _detect_fit(title, text, rules_for(sig).community(sig), terms, icp)
    m_fit = model_post.get("product_fit")
    if m_fit == "unrelated":
        return {"_exclude": "irrelevant"}
    if m_fit in ("fits", "partial"):
        if det_fit == "unrelated":
            fit = "uncertain"
            uncertainty.append("Relevance is unconfirmed: the model judged it relevant, but " + fit_note[0].lower() + fit_note[1:])
        else:
            fit = min(m_fit, det_fit, key=_FIT_ORDER.get)
    elif det_fit == "unrelated":
        return {"_exclude": "irrelevant"}
    else:
        fit = det_fit
    if fit_note and fit == "partial":
        uncertainty.append(fit_note)

    evidence, evidence_source = model_post.get("evidence"), "model_quote"
    if not isinstance(evidence, str) or not evidence.strip():
        evidence = None
    elif not quote_in_post(evidence, title, text):
        uncertainty.append("The model's quote was not found in the post; an excerpt is shown instead.")
        evidence = None
    if not evidence:
        evidence, evidence_source = _pick_evidence(title, text, role, trigger), "excerpt"
    if not evidence:
        evidence_source = None

    if role == "competitor_promo":
        label, reason = "Low", "A vendor promoting their own tool — competitor activity, not buyer demand."
    elif role == "testimonial":
        label, reason = "Low", "A user describing a tool they already use — market proof, not buyer demand."
    elif role != "buyer":
        label, reason = "Low", "General discussion; the author is not asking for a tool or solution."
    elif fit == "uncertain":
        label, reason = "Uncertain", "Asks for a solution, but its relevance to this request is unconfirmed."
    elif not evidence:
        label, reason = "Uncertain", "No supporting excerpt could be found in the post."
    elif intent == "explicit" and fit == "fits":
        label, reason = "High", "Asks for a solution in this category and states a budget, trial or purchase step."
    elif intent == "explicit":
        label, reason = "Medium", "Ready to buy, but the fit with this request is only partly supported."
    elif intent == "research":
        label, reason = "Medium", "Asks for recommendations, but states no budget, timeline or purchase step."
    else:
        label, reason = "Low", "No request for a solution is stated."

    return {"role": role, "purchase_intent": intent, "product_fit": fit,
            "relevance": "uncertain" if fit == "uncertain" else "relevant",
            "intent": label, "qualified": label == "High", "evidence": evidence, "evidence_source": evidence_source,
            "verification_status": status, "intent_reason": reason, "uncertainty": uncertainty, "_exclude": None}


# ── citation rewriting ──────────────────────────────────────────────────────

_REF = re.compile(r"\b(Posts?|posts?)\s*#?\s*(\d+)((?:\s*(?:,|and|&|or|/)\s*#?\s*\d+)*)")
# marker: \x01 citable rows \x02 original count \x02 rows cited only with an unavailable source \x03
_MARK = re.compile("\x01([\\d,]*)\x02(\\d+)\x02([\\d,]*)\x03")
_JOINED = re.compile("\x01([\\d,]*)\x02(\\d+)\x02([\\d,]*)\x03(\\s*(?:,|and|&)\\s*)\x01([\\d,]*)\x02(\\d+)\x02([\\d,]*)\x03")
_NUM_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
_QUANT = re.compile(
    r"\b(?P<q>several|multiple|many|a few|some|both|all|one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
    r"(?P<mid>(?:[\w/'-]+\s+){0,3}?)(?P<noun>posts?|users?|buyers?|people|person|threads?|authors?|vendors?|"
    r"conversations?|discussions?|founders?|teams?|others?)\s*\((?P<inner>[^()]*\x01[^()]*)\)", re.I)
_SINGULAR_NOUN = {"people": "person", "person": "person"}
_PLURAL_NOUN = {"person": "people", "people": "people"}
_LEAD_QUANT = re.compile(r"^(?:several|multiple|many|various)\s+(people|users|buyers|posts|threads|authors|founders|teams|vendors)\b", re.I)
_VERB_SINGULAR = {"ask": "asks", "show": "shows", "mention": "mentions", "request": "requests", "describe": "describes",
                  "want": "wants", "need": "needs", "discuss": "discusses", "compare": "compares", "express": "expresses",
                  "say": "says", "use": "uses", "seek": "seeks", "look": "looks", "are": "is", "have": "has",
                  "do": "does", "point": "points", "come": "comes", "highlight": "highlights", "suggest": "suggests",
                  "state": "states"}
_VAGUE = re.compile(
    r"\b(?:several|multiple|many|numerous|various|a number of|a few|a handful of|dozens of)\s+(?:[\w/-]+\s+){0,2}?"
    r"(?:others?|posts?|users?|buyers?|people|threads?|authors?|vendors?|competitors?|tools?|founders?|teams?|"
    r"requests?|questions?|queries|conversations?|discussions?|of them)\b", re.I)
_CITED = re.compile(r"\bPosts? \d+(?:(?:, | and )\d+)*")
_NUMBER = re.compile(r"[$€£]?\d[\d,]*(?:\.\d+)?(?:k|m|%)?", re.I)
_CLAUSES = re.compile(r"(?<=[.;!?])\s+")
_DROPPED_REF = re.compile("(?:\\s*(?:,|\\band\\b|\\bor\\b|&)\\s*)?\\[?(?:\x05|\x06)\\]?")


def _ints(s: str) -> list:
    return [int(x) for x in s.split(",") if x]


def _join_refs(nums: list) -> str:
    if len(nums) == 1:
        return f"Post {nums[0]}"
    return "Posts " + ", ".join(str(n) for n in nums[:-1]) + f" and {nums[-1]}"


def _fix_verb(text: str, pos: int) -> str:
    """Singularise a plural verb right after `pos` ("Post 2 ask" -> "Post 2 asks")."""
    m = re.match(r"(\)?\s+)([a-z]+)\b", text[pos:])
    if m and m.group(2) in _VERB_SINGULAR:
        a = pos + m.start(2)
        return text[:a] + _VERB_SINGULAR[m.group(2)] + text[a + len(m.group(2)):]
    return text


def _tidy(text: str) -> str:
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+([,.;)])", r"\1", re.sub(r"\s{2,}", " ", text)).strip()
    return re.sub(r";$", ".", text)


def rewrite_post_refs(text, gmap: dict, max_ref: int = None, unavailable=frozenset(), allowed=None):
    """Rewrites "Post N" / "Posts N, M and K" from prompt numbers to table
    numbers, in one pass (markers prevent re-mapping a rewritten number).
    Only rows in `allowed` (default: every table row) with an available
    source can be cited. A clause whose citations all point elsewhere — an
    excluded post, an unavailable source, a row outside `allowed` — is
    removed; such a citation inside an otherwise valid clause is dropped.
    A quantity in front of a citation ("Several posts (…)") and the verb
    after it are corrected to the remaining count."""
    if not isinstance(text, str) or not text or not _REF.search(text):
        return text

    def mark(m):
        nums = [int(n) for n in re.findall(r"\d+", m.group(2) + (m.group(3) or ""))]
        before = text[:m.start()].rstrip()
        prev = before.split()[-1] if before.split() else ""
        if (max_ref is not None and any(n > max_ref for n in nums)) or (
                prev[:1].isupper() and prev[-1:].isalpha() and not before.endswith((".", "!", "?"))):
            return m.group(0)                                 # "Washington Post 2024" is not a citation
        ok, unav = [], []
        for n in nums:
            i = gmap.get(n)
            if i is None or (allowed is not None and i not in allowed):
                continue
            bucket = unav if i in unavailable else ok
            if i not in bucket:
                bucket.append(i)
        return "\x01" + ",".join(map(str, ok)) + "\x02" + str(len(nums)) + "\x02" + ",".join(map(str, unav)) + "\x03"

    out = _REF.sub(mark, text)
    while _JOINED.search(out):                                # "Post 22 and Post 13" is one citation
        out = _JOINED.sub(lambda m: "\x01" + ",".join(map(str, _ints(m.group(1)) + _ints(m.group(5))))
                          + "\x02" + str(int(m.group(2)) + int(m.group(6)))
                          + "\x02" + ",".join(map(str, _ints(m.group(3)) + _ints(m.group(7)))) + "\x03", out, count=1)

    def quant(m):
        kept = sum(len(_ints(mk.group(1))) for mk in _MARK.finditer(m.group("inner")))
        if kept == 0:
            return m.group(0)
        q, noun = m.group("q"), m.group("noun")
        word = _NUM_WORDS.get(kept, str(kept))
        word = word.capitalize() if q[0].isupper() else word
        low = noun.lower()
        if kept == 1:
            noun = _SINGULAR_NOUN.get(low, noun[:-1] if low.endswith("s") else noun)
        else:
            noun = _PLURAL_NOUN.get(low, noun if low.endswith("s") else noun + "s")
        return f"{word} {m.group('mid')}{noun} ({m.group('inner')})" + ("\x04" if kept == 1 else "")

    out = _QUANT.sub(quant, out)

    pieces, pos = [], 0
    for mk in _MARK.finditer(out):
        ok, orig = _ints(mk.group(1)), int(mk.group(2))
        if ok:
            rep = _join_refs(ok) + ("\x04" if len(ok) == 1 and orig > 1 else "")
        elif _ints(mk.group(3)):
            rep = "\x05"                                      # only unavailable sources
        else:
            rep = "\x06"                                      # nothing citable
        pieces.append(out[pos:mk.start()] + rep)
        pos = mk.end()
    out = "".join(pieces) + out[pos:]
    while "\x04" in out:                                      # number became singular: fix the verb after it
        i = out.index("\x04")
        out = _fix_verb(out[:i] + out[i + 1:], i)
    if "\x05" not in out and "\x06" not in out:
        return out
    keep = []
    for clause in _CLAUSES.split(out):
        if "\x05" in clause or "\x06" in clause:
            clause = _DROPPED_REF.sub("", clause)
            if not _CITED.search(clause):
                continue                                      # it only described posts that cannot be cited
        keep.append(clause)
    return _tidy(" ".join(keep))


def _drop_uncited_vague(text: str) -> str:
    """A vague quantity ("several others are …") that cites no post cannot be
    checked against the stored posts, so that clause is removed."""
    if not isinstance(text, str) or not _VAGUE.search(text):
        return text

    def paren(m):
        parts = [p for p in m.group(1).split(";") if not (_VAGUE.search(p) and not _CITED.search(p))]
        return "(" + ";".join(parts).strip() + ")" if parts else ""

    out = re.sub(r"\(([^()]*)\)", paren, text)
    return _tidy(" ".join(c for c in _CLAUSES.split(out) if not (_VAGUE.search(c) and not _CITED.search(c))))


def _drop_unsupported_numbers(text: str, row_text: dict, allowed=None) -> str:
    """A clause stating a number (a budget, a count, a price) is kept only when
    that number occurs in the posts it cites — or, when it cites none, in some
    verified post. Restated server counts and post numbers are not checked."""
    if not isinstance(text, str) or not re.search(r"\d", text):
        return text
    pool = {i: t for i, t in row_text.items() if allowed is None or i in allowed}
    keep = []
    for clause in _CLAUSES.split(text):
        cited = [int(n) for m in _CITED.finditer(clause) for n in re.findall(r"\d+", m.group(0))]
        body = _COUNT_CLAIM.sub(" ", _CITED.sub(" ", clause))
        body = re.sub(r"\(\d{1,2}\)|(?:^|\s)\d{1,2}[.)](?=\s)", " ", body)    # list enumerators "(1)", "2."
        nums = [n.replace(",", "").lower() for n in _NUMBER.findall(body)]
        if nums:
            hay = " ".join(pool[i] for i in cited if i in pool) if cited else " ".join(pool.values())
            hay = hay.replace(",", "").lower()
            if any(n not in hay for n in nums):
                continue
        keep.append(clause)
    return _tidy(" ".join(keep))


_NO_REWRITE_KEYS = {"platforms", "counts", "excluded_posts", "website_context", "searched", "format",
                    "verified_summary", "key_findings", "detailed_findings"}
# Names and labels never cite posts, so only citation mapping applies to them.
_NAME_KEYS = {"headline", "finding", "title", "findings_heading", "details_heading", "business_insight_heading",
              "evidence_type", "signal_strength", "research_objective", "followup_question"}


def _check_text(text, ctx, key, allowed=None):
    out = rewrite_post_refs(text, ctx["gmap"], ctx["max_ref"], ctx["unavailable"], allowed)
    if key in _NAME_KEYS:
        return out
    return _drop_unsupported_numbers(_drop_uncited_vague(out), ctx["row_text"], allowed)


def _rewrite_tree(node, ctx, key=None):
    """Every string in the answer except post rows, findings (checked per
    finding) and server metadata. A list item left empty is removed."""
    if isinstance(node, str):
        return _check_text(node, ctx, key)
    if isinstance(node, list):
        out = [_rewrite_tree(x, ctx, key) for x in node]
        return [x for x, orig in zip(out, node) if not (isinstance(orig, str) and orig.strip() and not x)]
    if isinstance(node, dict):
        return {k: (v if k in _NO_REWRITE_KEYS else _rewrite_tree(v, ctx, k)) for k, v in node.items()}
    return node


# ── answer-level enforcement ────────────────────────────────────────────────

def _fingerprint(sig: dict) -> str:
    return _norm(f"{sig.get('title') or ''} {(sig.get('post_text') or '')[:300]}")


def _map_indices(values, gmap):
    out = []
    for v in values if isinstance(values, list) else []:
        n = v if isinstance(v, int) and not isinstance(v, bool) else (int(v) if isinstance(v, str) and v.strip().isdigit() else None)
        if n in gmap and gmap[n] not in out:
            out.append(gmap[n])
    return out


_FINDING_TEXT = ("impact", "what_evidence_shows", "why_it_matters")


def _enforce_findings(items, by_index, require_support: bool, ctx):
    """Each finding keeps only supporting rows that can carry it: available
    sources, and — for a buyer-demand finding — relevant buyer rows. Its text
    may cite only those rows; a finding whose claim is left unsupported is
    removed."""
    if not isinstance(items, list):
        return items
    out = []
    for f in items:
        if not isinstance(f, dict):
            continue
        f = dict(f)
        supports = [i for i in _map_indices(f.get("supporting_post_indices"), ctx["gmap"]) if i not in ctx["unavailable"]]
        buyers = [i for i in supports if by_index[i].get("role") == "buyer" and by_index[i].get("relevance") == "relevant"]
        if f.get("evidence_type") == "Buyer" and buyers:
            supports = buyers                                  # demand rests on buyers only
        if require_support and not supports:
            continue
        posts = [by_index[i] for i in supports]
        demand = any(p.get("role") == "buyer" and p.get("relevance") == "relevant" for p in posts)
        f["supporting_post_indices"] = supports
        f["supporting_count"] = len(supports)
        f["demand_signal"] = demand
        if posts and not demand:
            f["evidence_type"] = ("Builder/Provider" if any(p.get("role") in NON_DEMAND_ROLES for p in posts)
                                  else "Contextual")
        if f.get("signal_strength") == "Strong" and not any(p.get("qualified") for p in posts):
            f["signal_strength"] = "Moderate" if demand else f["signal_strength"]
        allowed = set(supports) if require_support else None
        main = next((k for k in _FINDING_TEXT if isinstance(f.get(k), str) and f[k].strip()), None)
        for key in ("finding", "title") + _FINDING_TEXT:
            if isinstance(f.get(key), str):
                f[key] = _check_text(f[key], ctx, key, allowed)
        if main and not f[main].strip():
            continue                                           # its claim was not supported by its posts
        if len(supports) == 1:                                 # "Multiple users …" resting on one post
            for key in ("finding", "title"):
                if isinstance(f.get(key), str):
                    f[key] = _LEAD_QUANT.sub(lambda m: "One " + _SINGULAR_NOUN.get(m.group(1).lower(), m.group(1)[:-1]), f[key], count=1)
        out.append(f)
    return out


def _restate_count(m, counts: dict) -> str:
    """A count claim gets the server count it describes; anything ambiguous is left as written."""
    qual = (m.group(3) or "").strip().lower()
    if qual in ("relevant", "matching"):
        n = counts["relevant"]
    elif qual == "qualified":
        n = counts["qualified"]
    elif not qual and m.group(1).lower() in ("analyzed", "analysed", "reviewed"):
        n = counts["analyzed"]
    else:
        return m.group(0)
    noun = m.group(5)
    noun = noun[:-1] if n == 1 and noun.endswith("s") else (noun if noun.endswith("s") or n == 1 else noun + "s")
    return f"{m.group(1)} {n} {m.group(3) or ''}{m.group(4) or ''}{noun}"


def enforce_answer_integrity(data: dict, matched_signals: list, aligned: list, query) -> dict:
    """Applies the guardrails to a parsed source_list / comparison answer.
    `aligned` is the exact list whose position i was "[Post i+1]" in the prompt."""
    if not isinstance(data, dict) or data.get("format") not in ("source_list", "comparison"):
        return data
    sigs = [s for s in (matched_signals or []) if isinstance(s, dict)]
    by_url = {s["post_url"]: s for s in sigs if s.get("post_url")}
    global_pos = {}
    for i, s in enumerate(aligned or [], 1):
        if isinstance(s, dict) and s.get("post_url"):
            global_pos.setdefault(s["post_url"], i)
    terms = product_terms(query)
    icp = bool(_ICP_MARKERS.search(str(query or "")))

    excluded = {"irrelevant": 0, "duplicate": 0, "unverifiable": 0}
    excluded_posts = []
    listed = 0
    seen = set()
    entries = []

    def collect(platforms):
        nonlocal listed
        if not isinstance(platforms, list):
            return platforms
        out = []
        for entry in platforms:
            if not isinstance(entry, dict) or not isinstance(entry.get("posts"), list):
                out.append(entry)
                continue
            kept = []
            for pos, post in enumerate(entry["posts"]):
                if not isinstance(post, dict):
                    continue
                listed += 1
                sig = by_url.get(post.get("link"))
                if sig is None:
                    excluded["unverifiable"] += 1
                    continue
                keys = {rules_for(sig).source_id(sig), _fingerprint(sig)}
                if keys & seen:
                    excluded["duplicate"] += 1
                    excluded_posts.append({"link": sig["post_url"], "reason": "duplicate"})
                    continue
                seen.update(keys)
                labels = classify_post(sig, post, terms, icp)
                if labels.pop("_exclude"):
                    excluded["irrelevant"] += 1
                    excluded_posts.append({"link": sig["post_url"], "reason": "not relevant"})
                    continue
                new = dict(post, **labels)
                new["platform"] = platform_of(sig)
                new["source_id"] = rules_for(sig).source_id(sig)
                if new["verification_status"] == "removed":
                    new["summary"] = "Content removed — not verifiable."
                elif new["verification_status"] == "unverified":
                    new["summary"] = "Source text unavailable — not verified."
                new["_global"] = global_pos.get(sig["post_url"])
                new["_pos"] = pos
                kept.append(new)
            kept.sort(key=lambda p: (-_LABEL_ORDER[p["intent"]], p["verification_status"] != "verified",
                                     p["role"] in NON_DEMAND_ROLES, p["_pos"]))
            entry = dict(entry, posts=kept)
            entries.append(entry)
            if kept:
                out.append(entry)
        return out

    if data["format"] == "source_list":
        data["platforms"] = collect(data.get("platforms"))
        subjects = []
    else:
        subjects = [s for s in (data.get("subjects") or []) if isinstance(s, dict)]
        for subj in subjects:
            subj["platforms"] = collect(subj.get("platforms"))

    gmap, by_index, n = {}, {}, 0
    for entry in entries:
        for post in entry["posts"]:
            n += 1
            g = post.pop("_global", None)
            post.pop("_pos", None)
            post["index"] = n
            if g is not None:
                gmap.setdefault(g, n)
            by_index[n] = post
    rows = list(by_index.values())

    analyzed_by_platform = {}
    for s in aligned or []:
        analyzed_by_platform[platform_of(s)] = analyzed_by_platform.get(platform_of(s), 0) + 1
    for entry in entries:
        entry["shown_count"] = len(entry["posts"])
        plat = str(entry.get("platform") or "").strip().lower()
        plat = "x" if plat == "twitter" else plat
        entry["total_analyzed"] = analyzed_by_platform.get(plat, len(aligned or []))

    verified_rows = [p for p in rows if p["verification_status"] == "verified"]
    counts = {
        "retrieved": len(sigs),
        "unique": len({s.get("post_url") or _fingerprint(s) for s in sigs}),
        "analyzed": len(aligned or []),
        "listed": listed,
        "excluded": sum(excluded.values()),
        "excluded_reasons": excluded,
        "shown": len(rows),
        "relevant": sum(1 for p in verified_rows if p["relevance"] == "relevant"),
        "uncertain": sum(1 for p in verified_rows if p["relevance"] == "uncertain"),
        "unverified": len(rows) - len(verified_rows),
        "verified": len(verified_rows),
        "buyers": sum(1 for p in verified_rows if p["role"] == "buyer" and p["relevance"] == "relevant"),
        "qualified": sum(1 for p in rows if p["qualified"]),
        "researching": sum(1 for p in rows if p["role"] == "buyer" and p["intent"] == "Medium"),
        "competitor": sum(1 for p in rows if p["role"] in NON_DEMAND_ROLES),
    }

    if listed and not rows:
        return {
            "format": "no_results",
            "no_results_reason": "empty",
            "searched": {"query": "", "platforms": [], "time_window": ""},
            "message": (f"This search surfaced {listed} post{'s' if listed != 1 else ''}, but none was "
                        "relevant to the request with supporting evidence, so none are shown as results."),
            "likely_reason": "The posts found matched keywords but were not about what the request is looking for.",
            "suggested_actions": [],
            "counts": counts,
            "excluded_posts": excluded_posts,
        }

    require_support = bool(rows)
    ctx = {"gmap": gmap, "max_ref": max(len(aligned or []), listed),
           "unavailable": frozenset(p["index"] for p in rows if p["verification_status"] != "verified"),
           "row_text": {p["index"]: " ".join(str(x) for x in (by_url[p["link"]].get("title"), by_url[p["link"]].get("post_text"),
                                                              p.get("published_at")) if x)
                        for p in rows if p["verification_status"] == "verified"}}
    for field in ("executive_summary", "summary"):
        if isinstance(data.get(field), str):
            data[field] = _COUNT_CLAIM.sub(lambda m: _restate_count(m, counts), data[field])
    for scope in [data] + subjects:
        for key in ("key_findings", "detailed_findings"):
            if key in scope:
                scope[key] = _enforce_findings(scope[key], by_index, require_support, ctx)
    data = _rewrite_tree(data, ctx)
    data["counts"] = counts
    data["excluded_posts"] = excluded_posts
    data["verified_summary"] = verified_summary(counts)
    return data


def verified_summary(counts: dict) -> str:
    """One server-written sentence stating the checked counts behind the report."""
    def n(k, one, many):
        v = counts.get(k) or 0
        return f"{v} {one if v == 1 else many}"
    parts = [n("qualified", "qualified lead", "qualified leads")]
    for key, one, many in (("researching", "buyer still researching", "buyers still researching"),
                           ("uncertain", "post with unconfirmed fit", "posts with unconfirmed fit"),
                           ("competitor", "competitor or vendor post", "competitor or vendor posts"),
                           ("unverified", "post with an unavailable source", "posts with an unavailable source")):
        if counts.get(key):
            parts.append(n(key, one, many))
    text = "Checked against the stored posts: " + ", ".join(parts)
    if counts.get("excluded"):
        text += f"; {n('excluded', 'post excluded', 'posts excluded')}"
    return text + "."


# ── stored results (the "closest matches" cards) ────────────────────────────

def annotate_results(results: list, answer_text) -> list:
    """Marks each stored result with its place in the checked report, so no
    card can pass for a reviewed result: in_report (with its table number and
    rating), excluded (with the reason), not_selected (analysed, not listed),
    or not_analyzed (no post text was available). Report rows come first.
    Returns `results` unchanged when the answer was not checked."""
    try:
        data = json.loads(answer_text) if isinstance(answer_text, str) else None
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("counts"), dict) or not isinstance(results, list):
        return results
    groups = [data.get("platforms")] + [s.get("platforms") for s in (data.get("subjects") or []) if isinstance(s, dict)]
    in_report = {}
    for platforms in groups:
        for entry in platforms if isinstance(platforms, list) else []:
            for p in (entry.get("posts") if isinstance(entry, dict) else None) or []:
                if isinstance(p, dict) and p.get("link"):
                    in_report[p["link"]] = {"status": "in_report", "index": p.get("index"), "intent": p.get("intent"),
                                            "qualified": bool(p.get("qualified")), "relevance": p.get("relevance")}
    excluded = {e["link"]: e.get("reason") for e in data.get("excluded_posts") or [] if isinstance(e, dict) and e.get("link")}
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("post_url")
        if url in in_report:
            review = in_report[url]
        elif url in excluded:
            review = {"status": "excluded", "reason": excluded[url], "qualified": False}
        elif (r.get("post_text") or "").strip():
            review = {"status": "not_selected", "qualified": False}
        else:
            review = {"status": "not_analyzed", "qualified": False}
        out.append(dict(r, review=review))
    out.sort(key=lambda r: (r["review"]["status"] != "in_report", r["review"].get("index") or 0))
    return out
