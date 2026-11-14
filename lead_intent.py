"""
lead_intent.py — profile-aware reading of ONE post for the lead engine (pure: no I/O, no model, no database).

A post becomes a lead only when TWO independent signals agree, and they are kept apart:

  1. A NEED FRAME: what the author is DOING, recognised from how the sentence is built, not from the topic. Frames are generic across industries:
        REQUEST      "I need / we're looking for / who can fix / where can I hire ..."
        RECOMMEND    "can anyone recommend / any suggestions / what do you use for ..."
        PRICE        "how much does ... cost / what do ... charge / is it worth paying ..."
        COMPARE      "X vs Y / alternatives to / should we use A or B / deciding between ..."
        REPLACE      "switching from / replacing / unhappy with our current / our usual X retired ..."
        PROBLEM      a first-person statement in which something is failing or painful ("our checkout is terrible", "my drain is blocked again")
        WISH         "I wish someone could / we'd pay for / time to find / can't fix this myself"
     A frame needs an author in the sentence (first person, or a question to the crowd). A third-person report is not a frame.
  2. PROFILE ALIGNMENT: do the words the author uses about WHAT they need match what THIS business sells, the problems it solves, who it serves
     (and, for a local business, where)? Alignment is weighted: a distinctive identifier (a product or brand name, an acronym of the offer, a competitor,
     a term the user listed) counts 2; an ordinary content word from the offer or the problems counts 1; generic business words count 0; the audience
     and the place only add to a match that already exists. weight >= 2 is "strong", 1 is "partial", 0 is "none". A topic match alone is never a lead.

GUARDS (applied first): a vendor or freelancer advertising themselves, a definition / "explain" question, a tutorial, a job seeker, a post that only
validates the author's own idea. These are never leads, whatever words they contain.

Everything is deterministic and explainable: the result names the frame, the exact sentence (a substring of the post, used as the quote), the matched
profile words and the reasons. Non-English: request/recommend/price frames exist for Spanish, German and French; alignment still needs the PROFILE to
contain terms in that language (the user's product terms), because nothing here translates.
"""
import re
import unicodedata

# ── tokens and stems ─────────────────────────────────────────────────────────────────────────────────────────────────────
_TOKEN = re.compile(r"[^\W\d_]{2,}(?:'[a-z]+)?", re.UNICODE)
_STOP = frozenset("""a an the and or of for to in on at by with from as is are was were be been being it its this that these those i we you he she they me us my our
your their his her not no yes do does did have has had can could would should will just so very really also than then there here what when where which who whom
how why if but about into over under again once more most some any each other such own same too only both few all up down out off s t don
el la los las de del y o un una para por con que en es se lo mi nuestro su al como pero muy ya mas
der die das und oder ein eine für mit ich wir ist sind nicht auch wie von zu auf im den dem des
le les du et ou une pour avec je nous est sont pas aussi comme dans sur par mon notre""".split())
# Generic business English that says nothing about WHICH business: weight 0 in the alignment (a language-level stop list, not an industry list).
_GENERIC = frozenset("""software service services product products solution solutions tool tools platform platforms online small business businesses company companies team teams
new good best great better help need needs want wants looking work works working make makes people thing things use using used year years time day days week
weeks month months customer customers client clients custom simple easy quality price prices cost costs local free top general issue issues problem problems
manage managing management system systems app apps someone anyone something anything everything recommend recommendation recommendations suggest
suggestion suggestions option options provider providers expert experts agency agencies freelancer freelancers professional professionals specialist
specialists firm firms hire hiring hired find finding get getting buy buying one ones lot lots way ways thought thoughts opinion opinions advice
looking currently right now soon today tomorrow week""".split())
_SUFFIXES = ("ments", "ment", "ings", "ing", "ers", "er", "ors", "or", "ies", "es", "ed", "ly", "s")


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii") if not s.isascii() else s


def stem(word: str) -> str:
    """A light, deterministic stem: plumbing / plumber / plumbers -> plumb, developer / development -> develop, designs / designer -> design.
    Applied identically to the profile and to the post, so only consistency matters."""
    w = _fold(word.lower())
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            if suf == "ies":
                w += "y"
            break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "ls":
        w = w[:-1]
    return w


def _is_generic(raw: str) -> bool:
    low = _fold(raw.lower())
    return low in _GENERIC or stem(raw) in _GENERIC


def stems(text: str, keep_generic: bool = False) -> dict:
    """{stem: first raw word} for the content words of `text` (stop words and generic business words removed)."""
    out = {}
    for m in _TOKEN.finditer(str(text or "")):
        raw = m.group(0).split("'")[0]
        low = _fold(raw.lower())
        if low in _STOP or len(low) < 3:
            continue
        s = stem(raw)
        if len(s) < 3 or (not keep_generic and (s in _GENERIC or low in _GENERIC)):
            continue
        out.setdefault(s, raw)
    return out


# ── the profile signature ────────────────────────────────────────────────────────────────────────────────────────────────
def _capitalised(text: str):
    """Words written with a capital in the MIDDLE of the user's text or as a whole token (Shopify, CeraVe, WooCommerce): names, not sentence starts."""
    out = []
    for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][A-Za-z0-9]{2,})\b", str(text or "")):
        out.append(m.group(1))
    for m in re.finditer(r"\b([A-Z]{2,6})\b", str(text or "")):            # acronyms the user wrote (CRM, ATS)
        out.append(m.group(1))
    for m in re.finditer(r"\b([A-Z][a-z]+[A-Z][A-Za-z]*)\b", str(text or "")):   # CamelCase
        out.append(m.group(1))
    return out


def signature(profile: dict) -> dict:
    """The business as alignment data, built ONLY from the user's own words (`profile["user"]`)."""
    u = (profile or {}).get("user") or {}
    sells = str(u.get("sells") or "")
    ident = []                                                           # each: frozenset(stems) that must ALL appear (a phrase) -> weight 2
    for phrase in list(u.get("product_terms") or []) + list(u.get("competitors") or []):
        st = frozenset(stem(w) for w in _TOKEN.findall(str(phrase)) if _fold(w.lower()) not in _STOP and len(w) >= 3)
        if st:
            ident.append(st)
    others = " ".join(str(x) for x in list(u.get("product_terms") or []) + list(u.get("competitors") or []) + [u.get("problems_solved") or "", u.get("ideal_customers") or "", u.get("notes") or ""])
    first = (re.findall(r"[A-Za-z][A-Za-z0-9]+", sells) or [""])[0]
    for w in _capitalised(sells) + _capitalised(others) + ([first] if first[:1].isupper() and (re.search(r"[a-z][A-Z]", first) or re.search(r"\b" + re.escape(first) + r"\b", others)) else []):
        if _fold(w.lower()) not in _STOP:
            ident.append(frozenset([stem(w)]))
    for phrase in list(u.get("product_terms") or []):                        # a capitalised word inside a listed product term is a name on its own ("Shopify developer")
        for w in re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", str(phrase)):
            ident.append(frozenset([stem(w)]))
    # acronym of the leading noun phrase of the offer: "applicant tracking software for ..." -> ATS
    lead = re.split(r"\b(?:for|to|of|in|that|which)\b", re.sub(r"^(?:we|i)\s+\w+\s+", "", sells, flags=re.I), maxsplit=1)[0]
    words = [w for w in _TOKEN.findall(lead) if _fold(w.lower()) not in _STOP]
    if len(words) == 3:
        acr = "".join(w[0] for w in words).lower()
        ident.append(frozenset([acr]))
    ident = list(dict.fromkeys(ident))
    ident_stems = set().union(*ident) if ident else set()
    offer = {s: w for s, w in stems(sells).items() if s not in ident_stems}
    problems = stems(u.get("problems_solved"))                               # generic words never count on their own, even when the user typed them
    audience = stems(u.get("ideal_customers"))
    geo = u.get("geography") or {}
    local = geo.get("scope") in ("city", "region") and bool(geo.get("places"))
    heads = set()                                                       # the last content word of each conjunct of the offer phrase ("boiler repair" -> repair)
    for conj in re.split(r"\band\b|,|/|&", lead):
        ws = [stem(w) for w in _TOKEN.findall(conj) if _fold(w.lower()) not in _STOP and not _is_generic(w) and len(stem(w)) >= 3]
        if ws:
            heads.add(ws[-1])
    return {"identifiers": ident, "heads": heads, "service": bool(_SERVICE_MARK.search(sells)), "offer": offer, "problems": problems, "audience": audience,
            "places": [p for p in (geo.get("places") or [])] if local else [], "local": local,
            "exclusions": [e.lower() for e in (u.get("exclusions") or [])]}


_ROLE = re.compile(r"\b(?:agency|agencies|developers?|designers?|freelancers?|consultants?|plumbers?|contractors?|experts?|specialists?|vendors?|providers?|firms?|studios?|engineers?|"
                   r"marketers?|technicians?|installers?)\b", re.IGNORECASE)
# A business that sells a SERVICE (not a product): the role bonus ("a marketing AGENCY") only makes sense for it.
_SERVICE_MARK = re.compile(r"\b(?:services?|agency|agencies|studio|consult\w*|freelanc\w*|contractor|design|development|marketing|repair|installation|maintenance|plumbing|cleaning|"
                           r"coaching|training|photography|writing|accounting|SEO)\b|^(?:we|i)\s+(?:design|develop|build|repair|install|clean|fix|teach|coach|write|photograph|consult)\b", re.IGNORECASE)


def align(text: str, sig: dict) -> dict:
    """Business fit and problem fit of ONE post against the signature. Each matched identifier counts 2; each distinct matched content stem of the
    offer or the problems counts 1 (a stem that is in both counts once); the place and the audience add 1 each, but only to a match that exists."""
    got = stems(text)
    low = str(text or "").lower()
    ident_hits, ident_words = 0, []
    for ident in sig["identifiers"]:
        if ident <= set(got):
            ident_hits += 1
            ident_words += [got[s] for s in ident if s in got]
    core = {**sig["offer"], **sig["problems"]}
    ident_stems = set().union(*sig["identifiers"]) if sig["identifiers"] else set()
    core_hits = [s for s in core if s in got and s not in ident_stems]
    p_hits = [got[s] for s in sig["problems"] if s in got]
    a_hits = [got[s] for s in sig["audience"] if s in got and s not in core_hits and s not in ident_stems]
    place = next((p for p in sig["places"] if p.lower() in low), None)
    base = ident_hits * 2 + len(core_hits)
    total = base + ((1 if place else 0) + (1 if a_hits else 0) if base else 0)
    role = sig.get("service", False) and bool(_ROLE.search(text or "")) and any(h in got for h in sig.get("heads", ()))     # "a marketing AGENCY", "a plumbing CONTRACTOR": the ask is for the provider
    if role and base:
        total += 1
    return {"business_fit": "strong" if total >= 2 else ("partial" if total == 1 else "none"), "weight": total,
            "problem_fit": "strong" if len(p_hits) >= 2 else ("partial" if p_hits else "none"),
            "terms": sorted(set(ident_words + [got[s] for s in core_hits if s in sig["offer"]])), "problem_terms": sorted(set(p_hits)),
            "audience_terms": sorted(set(a_hits)), "place": place}


# ── frames ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
_I = re.IGNORECASE
_FP = re.compile(r"\b(?:i|we|my|our|me|us|i'm|we're|i've|we've|i'd|we'd|i'll|we'll|mine|ours)\b", _I)
_CROWD = re.compile(r"\b(?:anyone|anybody|somebody|someone|who|where|which|what|how|any\s+(?:good|recommendations?|suggestions?|ideas?|advice)|can\s+you|could\s+you|do\s+you|does\s+anyone|do\s+people)\b", _I)
_FRAMES = [
    ("REQUEST", re.compile(r"\b(?:(?:i|we)(?:'d|'m|'re| would| am| are| really| urgently| currently| now| actively)*\s+(?:need|needs|want|require|looking|searching|hunting|seeking|hiring|trying to find|wanting)|"
                           r"(?:i|we)(?:'d| would)\s+(?:like|love)\s+(?:to\s+)?(?:find|hire|get|have|work with)|"
                           r"in\s+search\s+of|need\s+(?:to\s+(?:find|hire|get|replace|take over|sort|fix|organi[sz]e)|(?:a|an|some|someone|somebody|help)\b)|"
                           r"looking\s+(?:for|to\s+(?:hire|find|get|buy|work with|migrate|switch|move|redesign|rebuild|build|launch|start|set\s+up|outsource|replace|fix))|"
                           r"(?:who|where)\s+(?:can|could|should|do|does|handles?|offers?|provides?|specialis\w+)\s+(?:i|we|you|someone)?\s*(?:hire|find|get|buy|fix|build|do|call|use|talk to|go to|migrate|repair|design)|"
                           r"(?:who|which\s+\w+(?:\s+\w+)?)\s+should\s+(?:i|we)\s+(?:use|hire|call|talk to|go with|choose|pick|contact)|"
                           r"(?:anyone|anybody|somebody|someone)\s+(?:know|have|offer|do|provide|available)\b|"
                           r"(?:where|how)\s+(?:can|do)\s+(?:i|we)\s+(?:find|buy|get|hire)|"
                           r"(?:willing|ready)\s+to\s+pay|budget\s+(?:is|of|around)|hiring\s*:|^(?:need|looking for|seeking)\b)", _I), 0.9),
    ("RECOMMEND", re.compile(r"\b(?:recommend(?:ations?|ed|s)?|suggest(?:ions?|ed|s)?|any\s+(?:good|great|decent)\b|what\s+(?:do|did|would)\s+(?:you|y'all)\s+(?:all\s+)?(?:use|recommend|suggest)|"
                             r"who\s+do\s+you\s+(?:use|recommend|trust)|go-to|what(?:'s| is)\s+the\s+best\b|which\s+(?:one|brand|brands|agency|developer|designer|tool|option)\s+(?:is|are|would|do|should))", _I), 0.85),
    ("PRICE", re.compile(r"\bhow\s+much\b[^.?!\n]{0,70}\b(?:cost|costs|charge|charges|pay|spend|budget|price|expect|should)\b|\bwhat\s+(?:does|do|should|would)\b[^.?!\n]{0,60}\b(?:cost|charge|run)\b|"
                         r"\b(?:is\s+it|are\s+they|is\s+\w+)\s+worth\s+(?:it|paying|buying|the\s+(?:money|price|cost))\b|\bworth\s+(?:it|paying|buying)\b|\b(?:pricing|quote|rates)\s+(?:for|on)\b|\bwhat(?:'s|\s+is)\s+(?:a|the)\s+(?:realistic\s+|typical\s+|fair\s+|reasonable\s+|going\s+)?(?:price|cost|rate)\b|\bprice\s+range\b|\bballpark\b", _I), 0.7),
    ("COMPARE", re.compile(r"\b\w[\w.+-]*\s+(?:vs\.?|versus)\s+\w|\balternatives?\s+(?:to|for)\b|\b(?:deciding|choosing|torn|debating|decide)\s+(?:between|whether)\b|\bcompar(?:e|ing|ison)\b|"
                           r"\b(?:should|shall|would)\s+(?:i|we)\s+(?:use|hire|go|choose|pick|get|buy|repair|replace)\b[^?\n]{0,80}\bor\b|\bbetter\s+than\b|\binstead\s+of\b|"
                           r"\bor\s+(?:a|an|hire|use|just)\b[^?\n]{0,40}\?|\bwhich\s+is\s+better\b|\b\w+\s+or\s+(?:a|an|the)?\s*\w+\s+for\s+(?:a|an|my|our)\b", _I), 0.65),
    ("REPLACE", re.compile(r"\b(?:switch(?:ing|ed)?|mov(?:e|ing)|migrat(?:e|ing)|chang(?:e|ing))\s+(?:away\s+)?(?:from|off)\b|\breplac(?:e|ing|ement)\b|"
                           r"\b(?:unhappy|dissatisfied|fed\s+up|done|sick|disappointed)\s+with\s+(?:our|my|the|this)\b|\bthinking\s+of\s+(?:switching|leaving|changing|replacing|dropping)\b|"
                           r"\b(?:our|my)\s+(?:usual\s+|current\s+|old\s+|last\s+)?[\w'-]+(?:\s+[\w'-]+)?\s+(?:retired|quit|left|disappeared|went\s+quiet|gone\s+quiet|has\s+gone\s+quiet|isn't\s+working\s+out|hasn't\s+worked\s+out|was\s+discontinued|keeps?\s+missing)\b|"
                           r"\bwas\s+discontinued\b|\bkeeps?\s+missing\s+deadlines\b|\bisn't\s+working\s+out\b|\bnot\s+working\s+out\b", _I), 0.8),
    ("WISH", re.compile(r"\bwish\s+(?:someone|there|i|we)\b|\b(?:i|we)(?:'d| would)\s+(?:pay|love|like)\b|\b(?:time|need)\s+to\s+(?:find|get|sort|fix|organi[sz]e|hire)\b|"
                        r"\b(?:i|we)\s+(?:can't|cannot|couldn't)\s+(?:fix|do|handle|figure|solve)\b[^.!?\n]{0,40}\b(?:myself|ourselves|alone|this|it)\b|\bno\s+idea\s+(?:what|how|who|why)\b|"
                        r"\bdon't\s+know\s+(?:how|what|who)\b|\bwe\s+really\s+need\b|\bpanic\b|\bstarting\s+from\s+scratch\b|\bhelp\b[.!]?$", _I), 0.6),
]
_NEG = (r"terrible|awful|horrible|bad|broken|dead|slow|chaos|chaotic|mess|messy|stuck|failing|failed|crash\w*|leak\w*|block\w*|cold|crack\w*|dry|itchy|irritat\w*|red|"
        r"los(?:e|ing|t)|struggl\w*|can't|cannot|couldn't|doesn't|don't|won't|isn't|aren't|not\s+working|no\s+(?:engagement|customers|followers|sales|leads|growth|system|brand)|nothing\s+(?:i\s+\w+\s+)?(?:helps|works)|"
        r"drop[- ]?off|abandon\w*|unprofessional|inconsistent|dated|amateur|flat|poor|low|dropping|backing\s+up|burst|sting\w*|lost|hours|eating|buried|overwhelm\w*|drowning|"
        r"expensive|too\s+(?:slow|complex|heavy|much)|hate|frustrat\w*|painful|nightmare|burn\w*|bleed\w*|worse|bumpy|stagnant|no\s+idea")
_PROBLEM = re.compile(r"\b(?:our|my|we|i|the)\b[^.!?\n]{0,70}\b(?:" + _NEG + r")\b|\b(?:" + _NEG + r")\b[^.!?\n]{0,40}\b(?:our|my|we|i)\b", _I)
# Spanish / German / French: the request frames only (alignment still needs profile terms in that language).
_FRAMES_X = [
    ("REQUEST", re.compile(r"\b(?:necesito|busco|estoy buscando|buscamos|necesitamos|quiero contratar|me hace falta)\b|\b(?:ich|wir)\s+(?:brauche|suche|suchen|brauchen|benötige|benoetige)\b|\bsuche\b|"
                           r"\b(?:je cherche|nous cherchons|j'ai besoin|besoin d'|il me faut|nous avons besoin)\b|\bgesucht\b", _I), 0.9),
    ("RECOMMEND", re.compile(r"\b(?:recomienda|recomiendan|recomendar|recomendación|recomendacion|empfehlen|empfiehlt|empfehlung|recommandez|recommandation|conseillez|conseille|que me conseillez)\b", _I), 0.85),
    ("PRICE", re.compile(r"\b(?:cuánto cuesta|cuanto cuesta|cuánto cobra|wie viel kostet|was kostet|combien coûte|combien coute|quel prix)\b", _I), 0.7),
]
# GUARDS: a post that is only advertising, explaining, teaching, job hunting or validating is never a lead.
_VENDOR = re.compile(r"\b(?:dm|message|contact|call|email|text)\s+(?:us|me)\b|\b(?:i'?m|we'?re|i\s+am|we\s+are|now|currently)\s+available\b|\bavailable\s+for\s+(?:hire|new|freelance|projects?|work|booking|weekends?|clients?)\b|\b(?:i|we)\s+(?:just\s+)?(?:built|launched|made|created|released|started)\b|"
                     r"\bfree\s+(?:audit|trial|quote|consultation|estimate)\b|\bportfolio\b|\buse\s+code\b|\b\d+%\s+off\b|\bbooking\s+(?:is\s+)?open\b|\btaking\s+(?:on\s+)?(?:new\s+)?(?:clients|projects)\b|"
                     r"\bi'?m\s+a\s+(?:freelance|licen[sc]ed|qualified|certified)\b|\bwe\s+are\s+(?:a|an)\s+[\w\s-]{0,40}\b(?:agency|company|studio|firm)\b[^.!?\n]{0,60}\b(?:offer|offering|provide|providing|build|specializ|help)\w*|"
                     r"\b(?:offering|offers)\s+[\w\s,-]{0,40}\b(?:services?|repair|design|marketing|facials?)\b|\bcall\s+us\b|\byears?\s+of\s+experience\b|\bintroducing\b|\bcheck\s+out\s+(?:our|my)\b|\bour\s+(?:new\s+)?(?:line|collection|range)\b", _I)
_DEFINITION = re.compile(r"^\s*(?:what\s+(?:is|are)\b|what\s+does\s+[^?]{0,40}\b(?:mean|stand\s+for)\b|explain\b|how\s+(?:do|does)\b[^?]{0,50}\bwork|what\s+does\s+\w+\s+stand\s+for|why\s+(?:do|does|is|are)\b|eli5)|"
                         r"\b(?:can|could)\s+(?:someone|anyone)\s+explain\b|\bwhat\s+(?:does|do)\b[^?]{0,30}\bstand\s+for\b|\bhow\s+(?:does|do)\b[^?]{0,50}\bwork\b|\bwhat\s+(?:is|are)\s+(?:a|an|the)?\s*[\w'-]+\s+and\s+how\b", _I)
_FACTUAL = re.compile(r"\b(?:anyone|anybody|does\s+anyone|do\s+you)\s+know\s+(?:who|what|when|why|where)\b[^?\n]{0,80}\b(?:was|were|designed|made|invented|founded|built|created|wrote|played|won|started)\b", re.IGNORECASE)
_TUTORIAL = re.compile(r"\b(?:here(?:'s| is)\s+(?:how|my|a|the)|step[- ]by[- ]step|a\s+(?:short\s+)?guide|my\s+(?:nightly\s+|morning\s+)?routine|i\s+wrote\b|sharing\s+(?:the|my)|tips\s+for|how\s+i\s+(?:fixed|sped|improved|grew|made|replaced))\b", _I)
_JOB = re.compile(r"\b(?:i'?m|i\s+am|am)\s+(?:currently\s+)?looking\s+for\s+(?:a\s+|an\s+)?(?:(?:new|full[- ]time|part[- ]time|remote|junior|senior|entry[- ]level|summer|freelance)\s+)*[\w-]*\s*(?:job|position|role|work|internship|gig)s?\b(?!\s+applicants)|\bopen\s+to\s+(?:work|remote|offers)\b|\bresume\b|\bany\s+companies\s+hiring\b", _I)
_VALIDATE = re.compile(r"\b(?:validat(?:e|ing)\s+(?:my|an|the)|is\s+anyone\s+(?:actually\s+)?looking\s+for|would\s+(?:anyone|people|\w+s)\s+(?:actually\s+)?(?:pay|use|buy)|thinking\s+of\s+building|trying\s+to\s+validate)\b", _I)
_NEWS = re.compile(r"\b(?:announced|reported|according\s+to|analysts?|fined|unveiled|expected\s+to|study\s+(?:finds|shows))\b", _I)
_PLACE_AFTER_PREP = re.compile(r"\b(?:in|near|around)\s+((?:[A-Z][a-z]+)(?:\s+[A-Z][a-z]+)?)\b")
# Capitalised words after "in / near / around" that are NOT places: calendar words, seasons and holidays, and street names (a street says nothing about the city).
_NOT_PLACES = frozenset("""january february march april may june july august september october november december monday tuesday wednesday thursday friday saturday sunday
christmas easter halloween thanksgiving summer winter spring autumn fall ramadan eid diwali hanukkah new year today tomorrow yesterday""".split())
_STREET_WORD = frozenset("road street avenue lane drive close way court place square terrace crescent grove gardens park hill row".split())


def _sentences(full: str):
    pos = 0
    for part in re.split(r"(?<=[.!?])\s+|\n+", full):
        if not part.strip():
            pos += len(part)
            continue
        start = full.find(part, pos)
        if start < 0:
            start = pos
        pos = start + len(part)
        yield part.strip(), start


def _has_author(sentence: str) -> bool:
    return bool(_FP.search(sentence) or _CROWD.search(sentence) or sentence.rstrip().endswith("?") or re.match(r"^\s*(?:need|looking|seeking|hiring|wanted)\b", sentence, _I))


_ASKS = ("REQUEST", "RECOMMEND", "PRICE", "COMPARE", "REPLACE")


def guard_of(title: str, body: str) -> str:
    """'vendor' | 'definition' | 'tutorial' | 'job_seeker' | 'validation' | None for the post as a whole."""
    full = (title + "\n" + body).strip()
    if _JOB.search(full):
        return "job_seeker"
    if _VALIDATE.search(full):
        return "validation"
    if _VENDOR.search(full):
        return "vendor"
    if _FACTUAL.search(full):
        return "definition"
    soft = "definition" if (_DEFINITION.search(title) or _DEFINITION.search(body[:200])) else ("tutorial" if _TUTORIAL.search(full) else None)
    if soft and not any(f["frame"] in _ASKS for f in find_frames(title, body)):          # "what does a plumber CHARGE?" is a question about price, not a definition
        return soft
    return None


def find_frames(title: str, body: str) -> list:
    """Every frame found, strongest first: [{"frame", "strength", "sentence", "start"}]. A sentence is only read when it has an author."""
    full = (title + "\n" + body).strip()
    out = []
    title_end = len(title)
    for sent, start in _sentences(full):
        if len(sent) > 600:
            sent = sent[:600]
        author = _has_author(sent) or (start < title_end and not _NEWS.search(sent))      # a title is the poster's own words
        for name, rx, strength in _FRAMES + _FRAMES_X:
            if rx.search(sent) and (author or name in ("REQUEST", "RECOMMEND", "PRICE")):
                out.append({"frame": name, "strength": strength, "sentence": sent, "start": start})
        if author and _PROBLEM.search(sent) and not _NEWS.search(sent) and _FP.search(sent):
            out.append({"frame": "PROBLEM", "strength": 0.55, "sentence": sent, "start": start})
        elif _PROBLEM.search(sent) and re.search(r"\b(?:our|my)\b", sent, _I) and not _NEWS.search(sent):
            out.append({"frame": "PROBLEM", "strength": 0.5, "sentence": sent, "start": start})
    out.sort(key=lambda f: (-f["strength"], f["start"]))
    return out


_INTENT_OF = {"REQUEST": "SERVICE_REQUEST", "RECOMMEND": "RECOMMENDATION_REQUEST", "PRICE": "SOLUTION_RESEARCH", "COMPARE": "SOLUTION_RESEARCH",
              "REPLACE": "SOLUTION_RESEARCH", "PROBLEM": "STRONG_PROBLEM_SIGNAL", "WISH": "STRONG_PROBLEM_SIGNAL"}
_PROVIDER = re.compile(r"\b(?:who|where)\s+(?:can|could|should|do)\b|\bhire\b|\bhiring\b|\blooking\s+(?:for|to\s+hire)\s+(?:a|an|some)?\s*(?:\w+\s+){0,2}(?:agency|agencies|freelancer|developer|designer|consultant|contractor|plumber|vendor|provider|expert|engineer|manager|partner|firm|team)\b", _I)
_BUYING = re.compile(r"\bbudget\b|\bwilling\s+to\s+pay\b|\bready\s+to\s+(?:buy|pay|sign|hire)\b|\bhire\b|\bwe'?ll\s+pay\b|\bwe'?d\s+pay\b", _I)


def other_place(text: str, sig: dict):
    """For a LOCAL business only: a capitalised place after in / near / around that is not one of the business's places (None if there is none).
    Heuristic: a word like 'Excel' after 'in' is also read as a place, which can wrongly demote a lead (documented limitation)."""
    if not sig["local"]:
        return None
    low = text.lower()
    if any(p.lower() in low for p in sig["places"]):
        return None
    ours = set(sig["offer"]) | {s for i in sig["identifiers"] for s in i} | set(sig["problems"]) | set(sig["audience"])
    for m in _PLACE_AFTER_PREP.finditer(text):
        place = m.group(1)
        words = [w.lower() for w in place.split()]
        if words[0] in _NOT_PLACES or (len(words) > 1 and words[-1] in _STREET_WORD) or stem(place.split()[0]) in ours:
            continue
        return place
    return None


def analyze(title: str, body: str, profile: dict, sig: dict = None) -> dict:
    """The profile-aware reading of one post. Keys: guard, frame, intent, quote, fit (align()), other_place, reasons. Never raises."""
    title, body = str(title or ""), str(body or "")
    full = (title + "\n" + body).strip() if body and body != title else title
    sig = sig or signature(profile)
    out = {"guard": guard_of(title, body), "frame": None, "intent": None, "quote": None, "frames": [], "fit": align(full, sig), "other_place": other_place(full, sig),
           "excluded_term": next((e for e in sig["exclusions"] if e and e in full.lower()), None), "reasons": []}
    if out["guard"]:
        return out
    frames = find_frames(title, body)
    out["frames"] = [f["frame"] for f in frames]
    if not frames:
        return out
    # The sentence that carries the frame must also be about the business when the post is long: prefer a frame whose own sentence aligns.
    best = None
    for f in frames:
        sentence_fit = align(f["sentence"] + " " + title, sig)
        if best is None or (sentence_fit["weight"], f["strength"]) > (best[1]["weight"], best[0]["strength"]):
            best = (f, sentence_fit)
    f, sfit = best
    names = {x["frame"] for x in frames}
    intent = _INTENT_OF[f["frame"]]
    if f["frame"] in ("REQUEST", "RECOMMEND") and _PROVIDER.search(full) and f["frame"] == "REQUEST":
        intent = "PROVIDER_SEARCH"
    if f["frame"] == "REQUEST" and _BUYING.search(full):
        intent = "DIRECT_BUYING_INTENT"
    if intent == "SOLUTION_RESEARCH" and "REQUEST" in names and f["frame"] in ("PRICE", "COMPARE", "REPLACE"):
        intent = "DIRECT_BUYING_INTENT" if _BUYING.search(full) else "SOLUTION_RESEARCH"
    quote = f["sentence"] if f["sentence"] in full else None
    out.update(frame=f["frame"], intent=intent, quote=quote, frame_sentence_fit=sfit, replace="REPLACE" in names)
    return out
