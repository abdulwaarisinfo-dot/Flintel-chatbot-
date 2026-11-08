"""
signal_intel.py — evidence-grounded classification of retrieved posts, coverage, and honest zero-result explanations.
==================================================================================================================

Pure functions only: no network, no database, no model call, no imports from the application. It runs AFTER retrieval, on the posts
the existing pipeline already selected, and never changes which posts are returned or in what order.

Principle: never manufacture an opportunity. A post is DIRECT_DEMAND only when its OWN words explicitly ask for a product, service,
tool or person to obtain/hire/replace something; every class is backed by an exact quotation from the post (a substring of the stored
title / text, verified) or the classifier abstains to GENERAL_DISCUSSION. Topic relevance and commercial intent are separate numbers:
a post that is on-topic is not thereby a buying signal. The rules are deliberately conservative and stated in the output
(`classifier: "rules-1"`, `limitations`). They are a safety net under, not a replacement for, the model-based intent bridge.

Classes
  DIRECT_DEMAND       the author explicitly wants to buy / hire / find / implement / obtain something
  PROBLEM_SIGNAL      the author describes a real problem or frustration (usually while seeking help)
  MARKET_SIGNAL       a discussion reveals needs, preferences, trends, risks or concerns, without the author asking to obtain anything
  COMPETITOR_SIGNAL   a founder / vendor presents or discusses their OWN product
  GENERAL_DISCUSSION  the topic is mentioned; no meaningful commercial intent is demonstrated
"""
import re
from datetime import datetime, timezone

import platforms   # pure registry: the platform of a post is read from its URL host, not from a stored label

CLASSIFIER_VERSION = "rules-3"
DIRECT_DEMAND, PROBLEM_SIGNAL, MARKET_SIGNAL = "DIRECT_DEMAND", "PROBLEM_SIGNAL", "MARKET_SIGNAL"
COMPETITOR_SIGNAL, GENERAL_DISCUSSION = "COMPETITOR_SIGNAL", "GENERAL_DISCUSSION"
CLASSES = (DIRECT_DEMAND, PROBLEM_SIGNAL, MARKET_SIGNAL, COMPETITOR_SIGNAL, GENERAL_DISCUSSION)
LABELS = {DIRECT_DEMAND: "Direct demand", PROBLEM_SIGNAL: "Problem signal", MARKET_SIGNAL: "Market signal",
          COMPETITOR_SIGNAL: "Competitor signal", GENERAL_DISCUSSION: "General discussion"}

_I = re.IGNORECASE

# ── rule sets (each rule: name, regex, strength 0..1) ─────────────────────────────────────────────────────────────────────
_DEMAND = [
    ("looking_for", re.compile(r"\b(?:i|we)(?:'m| am|'re| are)?\s+(?:currently\s+|actively\s+)?(?:looking|searching|hunting|shopping)\s+for\b", _I), 0.9),
    ("need_a", re.compile(r"\b(?:i|we)\s+(?:really\s+)?(?:need|want|require)\s+(?:a|an|some|to\s+(?:find|hire|buy|get|replace))\b", _I), 0.85),
    ("looking_for_bare", re.compile(r"(?:^|[.!?\n]\s*)(?:currently\s+|actively\s+)?(?:looking|searching)\s+for\s+(?:a|an|some|any|the\s+best|good)?\s*\w", _I), 0.8),
    ("recommend_req", re.compile(r"\b(?:please\s+recommend|(?:can|could|would)\s+you\s+(?:please\s+)?(?:recommend|suggest)|any\s+(?:good\s+)?(?:recommendations?|suggestions?)|recommendations?\s+(?:for|on|please|welcome)|suggestions?\s+(?:for|please|welcome))\b", _I), 0.8),
    ("anyone_know", re.compile(r"\b(?:anyone|anybody|somebody|someone)\s+know\s+(?:of\s+)?(?:a|an|any|the)\b|\bdoes\s+(?:a|an|anyone)\b[^.?]{0,40}\b(?:exist|know)\b|\bis\s+there\s+(?:a|an|any|some)\s+(?:simple\s+|good\s+|easy\s+)?(?:way|app|tool|software|service|product|platform|solution)\b", _I), 0.8),
    ("would_pay", re.compile(r"\b(?:i|we)(?:'d|\s+would|\s+will)\s+(?:happily\s+|gladly\s+|definitely\s+|totally\s+)?(?:pay|subscribe|buy|purchase)\b|\b(?:i|we)\s+want\s+to\s+(?:pay|buy|purchase)\b|\bwilling\s+to\s+pay\b", _I), 0.9),
    ("need_bare", re.compile(r"(?:^|[.!?\n]\s*)need\s+(?:a|an|some|something|help|to\s+find)\b", _I), 0.8),
    ("seeking", re.compile(r"\b(?:seeking|in\s+search\s+of)\b|(?:^|\n)\s*(?:wanted|request|quote\s+request)\s*:", _I), 0.8),
    ("which_tool", re.compile(r"\bwhich\s+(?:\w+\s+){0,2}(?:tool|product|software|app|service|vendor|platform|solution|crm)s?\s+(?:should|would|could|do|can|is)\b|\bwhat(?:'s|\s+is)?\s+(?:a\s+)?(?:good\s+|best\s+)?(?:tool|software|app|product|service|solution|alternative)s?\b[^.?]{0,25}\b(?:use|suggest|recommend|for)\b|\bbest\s+(?:tool|app|software|service|platform|crm)s?\s+(?:for|to)\b", _I), 0.75),
    ("looking_to", re.compile(r"\b(?:i|we)(?:'m| am|'re| are)?\s+looking\s+to\s+(?:hire|buy|find|get|outsource|purchase|replace|switch)\b", _I), 0.9),
    ("recommend", re.compile(r"\b(?:can|could|does|would)\s+(?:anyone|somebody|someone)\s+(?:please\s+)?(?:recommend|suggest|point\s+me)\b", _I), 0.85),
    ("recommendations", re.compile(r"\b(?:any|what)\s+(?:good\s+)?(?:recommendations?|suggestions?)\s+(?:for|on)\b", _I), 0.7),
    ("alternatives", re.compile(r"\balternatives?\s+(?:to|for)\b", _I), 0.65),
    ("who_can", re.compile(r"\b(?:who|where)\s+(?:can|do|should)\s+(?:i|we)\s+(?:hire|find|buy|get)\b", _I), 0.85),
    ("need_provider", re.compile(r"\b(?:need|want|looking\s+for|hiring)\s+(?:a|an|some)?\s*(?:good\s+|reliable\s+|experienced\s+)?"
                                 r"(?:developers?|agency|agencies|freelancers?|consultants?|contractors?|vendors?|providers?|engineers?)\b", _I), 0.85),
    ("is_there_tool", re.compile(r"\b(?:is|are)\s+there\s+(?:a|an|any)\s+(?:good\s+)?(?:tool|service|app|software|platform|product|saas|solution)s?\s+"
                                 r"(?:that|which|for|to)\b", _I), 0.75),
    ("budget", re.compile(r"\b(?:willing\s+to\s+pay|have\s+(?:a\s+)?budget|budget\s+(?:of|is|around|approved)|budget\s*[:$]|rfp|request\s+for\s+proposal)", _I), 0.9),
]
# The asker is PROBING demand for their own idea ("is anyone looking for X?"): a market signal, never a buyer.
_VALIDATION = re.compile(r"\b(?:is|are)\s+(?:anyone|anybody|people|there\s+(?:anyone|a\s+market))\s+(?:else\s+)?(?:actually\s+)?"
                         r"(?:looking\s+for|searching\s+for|interested\s+in|paying\s+for|willing\s+to\s+pay|using|buying|wanting|needing|seeing|noticing)\b"
                         r"|\bis\s+there\s+(?:any|actual|real)\s+(?:demand|market|appetite|interest)\b"
                         r"|\bwould\s+(?:anyone|you|people)\s+(?:actually\s+)?(?:pay|use|buy|want|need)\b"
                         r"|\bdo\s+(?:you|people)\s+(?:actually\s+)?(?:want|need|pay\s+for)\b|\bdo\s+(?:you|people)\s+actually\s+use\b|\bvalidat(?:e|ing)\s+(?:my|an|the)\s+idea\b", _I)
_COMPETITOR = [
    ("show_hn", re.compile(r"^\s*show\s+hn\b", _I), 0.95),
    ("built", re.compile(r"\b(?:i|we)(?:'ve|\s+have)?\s+(?:just\s+)?(?:built|made|created|launched|released|shipped|open[- ]sourced)\b", _I), 0.8),
    ("building", re.compile(r"\b(?:i|we)(?:'m| am|'re| are)\s+(?:currently\s+)?(?:building|launching|working\s+on)\b", _I), 0.7),
    ("announce", re.compile(r"\b(?:introducing|announcing|launch(?:ing|ed)\s+(?:our|my|a|the)|our\s+(?:new\s+)?(?:product|platform|tool|startup|company|app)|"
                            r"my\s+(?:new\s+)?(?:startup|product|saas|company|app|tool))\b", _I), 0.8),
    ("switched", re.compile(r"\b(?:i|we)(?:'ve|\s+have)?\s+(?:just\s+|recently\s+)?(?:switched|migrated|moved|changed)\s+(?:away\s+)?(?:from|to)\b", _I), 0.8),
    ("versus", re.compile(r"\b\w+\s+(?:vs\.?|versus)\s+\w+\b|\bcompared?\s+(?:to|with)\b", _I), 0.6),
    ("switching", re.compile(r"\b(?:switch(?:ed|ing)?|migrat(?:ed|ing)|mov(?:ed|ing)|chang(?:ed|ing)|leav(?:ing)?|left)\s+(?:(?:away\s+)?from|off|over\s+from)\b"
                             r"|\b(?:switch(?:ed|ing)?|mov(?:ed|ing)|chang(?:ed|ing))\s+(?:over\s+)?to\s+(?:a|an|the)?\s*(?:rival|competitor|cheaper|different|new|another)\b"
                             r"|\b(?:we|i)\s+(?:just\s+)?switch(?:ed)?\s+[\w ]{1,25}(?:providers?|vendors?|platforms?|tools?|apps?)\b"
                             r"|\breplac(?:ed|ing)\s+(?:our|my|the|it)\b[^.]{0,40}\bwith\b|\bdropp(?:ed|ing)\s+[^.]{0,30}\bfor\b|\b(?:leaving|left)\s+[^.]{0,25}\bfor\b"
                             r"|\bchose\s+[^.]{0,30}\bover\b|\bover\s+the\s+(?:market\s+leader|incumbent)\b", _I), 0.8),
    ("compared", re.compile(r"\bcompar(?:ing|ed|ison|e)\b[^.]{0,60}\b(?:tools?|apps?|platforms?|vendors?|providers?|products?|software|solutions?|systems?)\b"
                            r"|\b(?:evaluat(?:ed|ing))\b[^.]{0,30}\bagainst\b|\bcomparison\s+table\b", _I), 0.65),
    ("cta", re.compile(r"\b(?:feedback\s+(?:is\s+)?(?:welcome|appreciated)|free\s+trial|join\s+the\s+waitlist|sign\s+up\s+(?:for|at)|try\s+it\s+(?:out|at))\b", _I), 0.6),
]
_PROBLEM = [
    ("struggling", re.compile(r"\b(?:struggl\w+|frustrat\w+|annoy\w+|fed\s+up|nightmare|painful|pain\s+point|wast(?:e|ing)\s+(?:of\s+)?(?:time|money|hours))\b", _I), 0.7),
    ("breaking", re.compile(r"\b(?:keeps?\s+(?:breaking|failing|crashing)|doesn'?t\s+work|not\s+working|stopped\s+working|is\s+broken|can'?t\s+(?:figure|get|find|make))\b", _I), 0.65),
    ("complaint", re.compile(r"\b(?:too\s+(?:expensive|slow|complicated|costly|buggy|clunky|hard|difficult)|overpriced|(?:i|we)\s+(?:really\s+)?hate|terrible|awful|useless|sucks?)\b", _I), 0.65),
    ("costing", re.compile(r"\b(?:costing|cost)\s+(?:us|me|real|thousands|money|revenue)\b|\b(?:lose|losing|lost|wasted)\s+(?:another\s+)?(?:deals?|leads?|customers?|clients?|revenue|money|thousands|sales)\b", _I), 0.65),
    ("draining", re.compile(r"\b(?:eating|killing|draining|wasting)\s+(?:up\s+)?(?:our|my|the|a\s+whole)\b|\btime\s+sink\b|\bdrowning\s+in\b", _I), 0.65),
    ("is_a_mess", re.compile(r"\b(?:is|are|was|were)\s+(?:always\s+|such\s+|still\s+)?(?:a\s+|an\s+)?(?:mess|chaos|nightmare|disaster|headache|pain|painful|miserable|tedious|maddening|exhausting|broken|useless|unusable|out\s+of\s+date)\b", _I), 0.65),
    ("keeps_failing", re.compile(r"\b(?:keeps?|keep)\s+(?:happening|slipping|breaking|failing|crashing|going\s+down|timing\s+out)\b|\b(?:logs?|logging)\s+(?:me|us)\s+out\b|\b(?:crash(?:es|ed|ing)|times?\s+out|timed\s+out)\b|\btakes?\s+(?:forever|days|weeks)\b", _I), 0.65),
    ("errors_up", re.compile(r"\b(?:errors?|mistakes?|bugs?|double\s+bookings?|conflicts?|outages?)\b[^.]{0,40}\b(?:again|rising|every|keep|constant|costing|this\s+(?:month|week))\b|\b(?:again|constant)\b[^.]{0,30}\b(?:errors?|mistakes?|bugs?)\b", _I), 0.6),
    ("how_handle", re.compile(r"\bhow\s+(?:do|are|should)\s+(?:you|people|we|i)\s+(?:handle|deal\s+with|manage|solve|fix|cope)\b", _I), 0.6),
    ("issue_with", re.compile(r"\b(?:problem|issue|trouble|struggle)\s+(?:with|using)\b", _I), 0.5),
]
_HELP = re.compile(r"\b(?:any\s+(?:ideas|advice|tips|suggestions|help)|how\s+(?:do|can|should)\s+(?:i|we)|help\s+me|please\s+help|advice|anyone\s+(?:else\s+)?(?:know|have|had|dealt))\b", _I)
_FIRST_PERSON = re.compile(r"\b(?:i|we|my|our|i'm|we're|i've|we've)\b", _I)
_MARKET = [
    ("security", re.compile(r"\b(?:security\s+(?:incident|breach|issue|flaw)|breach(?:ed)?|data\s+leak|leak(?:ed|s)?|vulnerab\w+|exploit\w*|hack(?:ed)?|compromis\w+|cve-\d|unauthori[sz]ed\s+access)\b", _I), 0.6),
    ("privacy", re.compile(r"\b(?:privacy|surveillance|data\s+collection|gdpr|consent)\b", _I), 0.55),
    ("regulation", re.compile(r"\b(?:regulat\w+|compliance|lawsuit|ban(?:ned)?|legislation|policy|antitrust)\b", _I), 0.5),
    ("pricing", re.compile(r"\b(?:pricing|price\s+(?:increase|hike|change|cut)|subscription\s+cost|too\s+expensive)\b", _I), 0.5),
    ("change_statement", re.compile(r"\bincreasingly\b|\bmore\s+(?:\w+\s+){0,2}(?:are|now)\s+\w+|\b(?:are|is)\s+(?:now\s+)?(?:moving|switching|shifting|adopting|accepting|adding|dropping|consolidating|raising)\b|\bshift(?:ing)?\s+(?:from|to|toward)\b|\b(?:is|are)\s+changing\b|\bwill\s+(?:change|force|affect)\b|\braised\s+(?:their\s+)?prices?\b|\bprice\s+(?:hikes?|increases?)\b|\bkeep\s+asking\s+for\b|\badoption\b|\bdemand\s+for\b|\bappetite\s+for\b|\bmarket\s+for\b|\bnew\s+(?:law|regulation|rules?|requirement)s?\b|\bconsolidat\w+\b|\bacquisition\b", _I), 0.5),
    ("market_event", re.compile(r"\b(?:funding|raises?\s+\$|acquir\w+|layoffs?|outage|shut(?:s|ting)?\s+down|antitrust|market\s+(?:share|size|gap)|adoption\s+(?:rate|of))\b", _I), 0.45),
]
# A demand / competitor reading needs a product or service context somewhere in the post: "looking for reading recommendations" or "reflectors versus
# refractors" are not commercial signals. Without it the post falls back to GENERAL_DISCUSSION (with a limitation note), never to a buyer.
_PRODUCT_CTX = re.compile(r"\b(?:tool|tools|app|apps|software|service|services|platform|platforms|solution|solutions|product|products|system|systems|saas|plugin|"
                          r"integration|dashboard|crm|erp|api|template|vendor|vendors|provider|providers|agency|developer|freelancer|consultant|contractor|"
                          r"subscription|automat\w+|invoic\w+|payroll|booking|scheduling|accounting|bookkeep\w+|billing|inventory|help\s*desk|competitor|rival|"
                          r"alternative|alternatives|suite|checkout|workflow|portal)\b", _I)
_COMMERCIAL_CUES = re.compile(r"\b(?:budget|pay|paid|pricing|price|hire|hiring|vendor|agency|contract|quote|subscription|license|purchase|buy|buying|rfp|enterprise)\b", _I)
_CURRENTLY_USING = re.compile(r"\b(?i:currently\s+using|(?:we|i)\s+(?:currently\s+)?(?:use|used)|switch(?:ed|ing)\s+from|moving\s+off|migrat(?:e|ing)\s+from|stuck\s+with)\s+([A-Z][\w.+-]*(?:\s+[A-Z][\w.+-]*)?)")
_STOP = frozenset("""a an the of on in at for to from with and or but is are was were be been being it its this that these those i we you he she they
me my our your their do does did can could would should will just not no yes any anyone some more most very about into than then there here what which
who whom how why when where if so as by up out over also get got have has had using use used need needs want looking look find""".split())
# NOTE: "ai" / "agent" are NOT stop words. They were, which made a query such as "Find people looking for AI SaaS agents" score an on-topic post 0.0.

_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def _words(text):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9+#.-]*", (text or "").lower())]


def _terms(text):
    out = []
    for w in _words(text):
        w = w[:-1] if len(w) > 3 and w.endswith("s") else w
        if w not in _STOP and len(w) > 1:
            out.append(w)
    return out


def _sentence_around(text, start, end, limit=240):
    """The sentence of `text` containing [start, end), as an exact substring (trimmed), capped at `limit` characters."""
    lo = max(text.rfind(c, 0, start) for c in (".", "!", "?", "\n"))
    lo = 0 if lo < 0 else lo + 1
    hits = [i for i in (text.find(c, end) for c in (".", "!", "?", "\n")) if i >= 0]
    hi = (min(hits) + 1) if hits else len(text)
    quote = text[lo:hi].strip()
    if len(quote) > limit:                               # keep an exact substring around the match
        a = max(0, start - lo - limit // 2)
        quote = text[lo + a: lo + a + limit].strip()
    return quote


def _first_match(rules, text):
    best = None
    for name, rx, strength in rules:
        m = rx.search(text)
        if m and (best is None or strength > best[2]):
            best = (name, m, strength)
    return best


def _fmt_ts(post):
    for k in ("published_at", "created_utc"):
        v = post.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            try:
                return datetime.fromtimestamp(float(v), timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                continue
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, datetime):
            return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()
    return None


def topic_relevance(query, text):
    """0..1: the share of the query's content words found in the text, or None when the query has none (general discovery). This says
    NOTHING about intent: an on-topic post can be a rant, an announcement or a security incident."""
    q = set(_terms(query))
    if not q:
        return None
    have = set(_terms(text))
    return round(len(q & have) / len(q), 2)


def _use_case_relevance(query, text):
    """0..1 by query word pairs (at most two words apart) found in the text (the use case, not just the topic words), or None."""
    def skip_pairs(words):          # ordered pairs at most two words apart, so "AI agent" still matches the query "AI SaaS agent"
        return {(words[i], words[j]) for i in range(len(words)) for j in range(i + 1, min(i + 3, len(words)))}
    pairs = skip_pairs(_terms(query))
    if not pairs:
        return None
    return round(len(pairs & skip_pairs(_terms(text))) / len(pairs), 2)


def _intent_strength(cls, rule_strength):
    """How strongly the AUTHOR expresses intent to obtain a solution. This is not how well a pattern matched: a vendor announcement matches its
    pattern strongly but expresses no buyer intent (0.0), and a described problem is capped because it asks for nothing."""
    if cls == DIRECT_DEMAND:
        return round(rule_strength, 2)
    if cls == PROBLEM_SIGNAL:
        return round(min(rule_strength, 0.5), 2)
    return 0.0


def _platform_of(p):
    """Canonical platform key from the post's URL host (else its stored label, else None). Never guessed from text."""
    try:
        key = platforms.of(p)
        if key:
            return key
        raw = p.get("platform")
        return str(raw).strip().lower() if isinstance(raw, str) and raw.strip() else None
    except Exception:
        return None


# Rules that are requests for OPTIONS or opinions ("alternatives to", "any recommendations"): solution-seeking, not a stated purchase decision.
_OPTION_SEEKING = frozenset({"alternatives", "recommendations", "recommend_req", "recommend", "anyone_know", "which_tool", "is_there_tool"})


def classify_post(post, query=None):
    """The signal record for ONE retrieved post. Never raises; a post it cannot read is GENERAL_DISCUSSION with a limitation."""
    p = post if isinstance(post, dict) else {}
    title = str(p.get("title") or "")
    body = str(p.get("post_text") or "")
    title_only = p.get("text_source") == "title" or not body.strip() or body.strip() == title.strip()
    full = title if title_only else (title + "\n" + body)
    limitations, confirmed, inferred = [], [], []
    cls, rule, strength, quote = GENERAL_DISCUSSION, None, 0.0, None
    intent_expl = "No explicit request to obtain a product or service, no described problem and no commercial statement was found."
    if not full.strip():
        limitations.append("The stored post has no readable text.")
    else:
        val = _VALIDATION.search(full)
        comp = _first_match(_COMPETITOR, full)
        dem = _first_match(_DEMAND, full)
        prob = _first_match(_PROBLEM, full)
        mkt = _first_match(_MARKET, full)
        pctx = bool(_PRODUCT_CTX.search(full))
        # Product-context gate: a demand or competitor reading needs a product/service context (or an unambiguous commercial rule); otherwise it
        # is not a commercial signal and falls back to general discussion (never to a buyer).
        if dem and not pctx and dem[0] not in ("budget", "need_provider", "who_can"):
            limitations.append("A request-like phrase was found but the post names no product, service or provider, so it is not treated as demand.")
            dem = None
        if comp and comp[0] in ("switched", "switching", "versus", "compared") and not pctx:
            comp = None
        pick = None
        if val and not (dem and dem[2] >= 0.85 and not comp):
            cls, pick = MARKET_SIGNAL, ("validation", val, 0.5)
            intent_expl = ("The author asks whether others want this; that probes demand for the author's own idea and is not a request "
                           "to buy. It is not confirmed demand.")
            inferred.append("The author may be validating a product idea.")
        elif comp and comp[0] in ("switched", "switching", "versus", "compared") and not (dem and dem[2] >= 0.85):
            cls, pick = COMPETITOR_SIGNAL, comp
            intent_expl = "The author compares products or describes moving from one product to another; this is competitive context."
            limitations.append("A completed switch or a comparison is not a current purchase request.")
        elif comp and comp[2] >= 0.8 and not (dem and dem[2] >= 0.85 and comp[0] not in ("show_hn",)):
            cls, pick = COMPETITOR_SIGNAL, comp
            intent_expl = "The author presents or discusses their own product or project; that is a vendor or founder, not a customer."
            limitations.append("The author appears to be promoting their own work: this is not evidence of a buyer.")
        elif dem:
            cls, pick = DIRECT_DEMAND, dem
            intent_expl = "The author's own words explicitly ask for a product, service, tool or provider."
        elif comp:
            cls, pick = COMPETITOR_SIGNAL, comp
            intent_expl = "The author talks about building or promoting their own product; that is a vendor or founder, not a customer."
            limitations.append("The author appears to be promoting their own work: this is not evidence of a buyer.")
        elif prob and (_FIRST_PERSON.search(full) or _HELP.search(full)):
            cls, pick = PROBLEM_SIGNAL, prob
            intent_expl = "The author describes a problem or frustration" + (" and asks for help." if _HELP.search(full) else ".")
            if not _HELP.search(full):
                limitations.append("No request for help or for a solution is stated.")
        elif mkt:
            cls, pick = MARKET_SIGNAL, mkt
            intent_expl = ("The discussion reveals a concern, trend or market development; the author does not ask to obtain anything. "
                           "It is context, not a lead.")
        elif prob:
            cls, pick = PROBLEM_SIGNAL, prob
            intent_expl = "The post describes a problem or frustration, without a first-person statement or a request."
            limitations.append("No first-person statement: the problem may belong to someone other than the author, and no request for a solution is stated.")
        if pick:
            rule, m, strength = pick
            quote = _sentence_around(full, m.start(), m.end())
            if quote and quote in full:
                confirmed.append("Stated in the post: “" + quote + "”")
            else:
                quote = None
        if cls == DIRECT_DEMAND and rule in _OPTION_SEEKING:
            limitations.append("The author asks for options or recommendations; that is solution-seeking, not a stated purchase decision.")
        if cls == DIRECT_DEMAND:
            inferred.append("Whether the author has authority or budget to buy is not stated." if not _COMMERCIAL_CUES.search(full)
                            else "A commercial cue (budget, hiring, pricing or purchase) appears in the text.")
        if cls == GENERAL_DISCUSSION and query:
            limitations.append("The post mentions the topic but shows no commercial intent.")
    if title_only:
        limitations.append("Only the title is available as evidence (no body text).")
    elif len(body) < 80:
        limitations.append("The stored text is very short.")
    if not p.get("author"):
        limitations.append("The author is not recorded.")
    url = p.get("post_url") or p.get("url") or ""
    when = _fmt_ts(p)
    if not when:
        limitations.append("No valid publication time is stored.")
    tr, uc = topic_relevance(query, full), _use_case_relevance(query, full)
    commercial = {DIRECT_DEMAND: 0.9 if _COMMERCIAL_CUES.search(full) else 0.7, PROBLEM_SIGNAL: 0.5, COMPETITOR_SIGNAL: 0.35,
                  MARKET_SIGNAL: 0.3, GENERAL_DISCUSSION: 0.05}[cls]
    conf = 0.3 + (0.3 if quote else 0.0) + (0.2 if not title_only else 0.0) + (0.1 if (when and url) else 0.0)
    if cls == GENERAL_DISCUSSION:
        conf = min(conf, 0.6)
    rel = ("Topic relevance is not evaluated (general discovery: no topic was asked for)." if tr is None else
           f"{int(round(tr * 100))}% of the query's key terms appear in the post"
           + ("" if uc is None else f"; {int(round(uc * 100))}% of its term pairs (use case)") + ". Topic relevance is separate from intent.")
    return {
        "class": cls, "label": LABELS[cls], "rule": rule,
        "title": title, "url": url, "platform": _platform_of(p), "author": p.get("author") or None, "published_at": when,
        "retrieval_path": p.get("retrieval_path") if p.get("retrieval_path") in ("live", "historical", "both") else None,   # provenance only; never an input to the class
        "quote": quote, "text_source": "title" if title_only else "body",
        "scores": {"topic_relevance": tr, "use_case_relevance": uc, "intent_strength": _intent_strength(cls, strength),
                   "commercial_relevance": commercial, "evidence_confidence": round(min(conf, 0.9), 2)},
        "relevance_explanation": rel, "intent_explanation": intent_expl,
        "confirmed": confirmed, "inferred": inferred, "limitations": limitations,
        "classifier": CLASSIFIER_VERSION,
    }


def extract_opportunity(post, classified=None):
    """For DIRECT_DEMAND / PROBLEM_SIGNAL posts only: what the text itself supports about the opportunity. Every non-null value is an
    exact quotation; everything else is listed under `unknown`. Nothing is inferred about the author's identity or budget."""
    c = classified or classify_post(post)
    if c["class"] not in (DIRECT_DEMAND, PROBLEM_SIGNAL):
        return None
    p = post if isinstance(post, dict) else {}
    title, body = str(p.get("title") or ""), str(p.get("post_text") or "")
    full = title if c["text_source"] == "title" else title + "\n" + body
    using = _CURRENTLY_USING.search(full)
    out = {"problem": c["quote"] if c["class"] == PROBLEM_SIGNAL else None,
           "solution_sought": c["quote"] if c["class"] == DIRECT_DEMAND else None,
           "currently_using": using.group(0) if using and using.group(0) in full else None,
           "commercial_cue": None}
    cue = _COMMERCIAL_CUES.search(full)
    if cue:
        out["commercial_cue"] = _sentence_around(full, cue.start(), cue.end())
    out["unknown"] = ["the author's role or company", "budget", "timeline"] + (["what they use today"] if not out["currently_using"] else [])
    return out


def summarize(items, query=None, mode="topic", total=None):
    """{counts, direct_demand, statement}. The statement never claims demand that is not in `items`. `total` = how many posts were retrieved: when
    more were retrieved than classified, the statement says so (it must never present the classified subset as everything retrieved)."""
    counts = {c: 0 for c in CLASSES}
    for it in items:
        counts[it["class"]] += 1
    n = len(items)
    cut = (total - n) if isinstance(total, int) and total > n else 0
    scope = f"the first {n} of {total} retrieved posts" if cut else None
    adjacent = {c: counts[c] for c in (PROBLEM_SIGNAL, MARKET_SIGNAL, COMPETITOR_SIGNAL) if counts[c]}
    if n == 0:
        stmt = "No posts were retrieved, so nothing was classified."
    elif counts[DIRECT_DEMAND]:
        stmt = (f"{counts[DIRECT_DEMAND]} of {scope or (str(n) + ' retrieved post' + ('s' if n != 1 else ''))} explicitly ask for a product, service or provider"
                " (quoted below). The other posts are shown separately and are not counted as demand.")
    else:
        stmt = ("No confirmed direct buying demand was found in " + (scope if scope else "the retrieved discussions") + ".")
        if adjacent:
            stmt += " Adjacent signals are shown separately: " + ", ".join(f"{LABELS[c].lower()} ({v})" for c, v in adjacent.items()) + "."
    if cut:
        stmt += f" The remaining {cut} retrieved post{'s' if cut != 1 else ''} {'were' if cut != 1 else 'was'} not classified."
    if mode == "general" and n:
        stmt = "General discovery sample: these posts were not selected for buying intent. " + stmt
    return {"counts": counts, "direct_demand": counts[DIRECT_DEMAND], "statement": stmt}


_REVIEW_STATUSES = ("in_report", "excluded", "not_selected", "not_analyzed")


# Roles the checked report (answer_integrity) gives to posts that are NOT demand. Two classifiers must not tell the reader two different stories about one post.
_NON_DEMAND_REVIEW_ROLES = frozenset({"competitor_promo", "testimonial"})


def _reconcile_with_checked_report(c, post):
    """If the checked report rated this very post as a vendor promotion or a testimonial, this report may not call it direct demand: it becomes a
    competitor signal with the reason stated. (The checked report is stricter about authors promoting themselves; neither report is overruled
    in the other direction: a post the checked report calls a buyer can still be 'general' here.)"""
    role = post.get("report_role") if isinstance(post, dict) else None          # set by answer_integrity.annotate_results for in-report posts
    if c.get("class") == DIRECT_DEMAND and role in _NON_DEMAND_REVIEW_ROLES:
        c["class"], c["label"] = COMPETITOR_SIGNAL, LABELS[COMPETITOR_SIGNAL]
        c["scores"]["intent_strength"] = 0.0
        c["limitations"] = list(c.get("limitations") or []) + ["The checked report above classifies this post as a vendor or testimonial, not a buyer."]
        c["intent_explanation"] = "The checked report treats the author as a vendor or testimonial, which is competitive context, not demand."
        c["reconciled"] = True


def _review_of(p):
    """The server-set review of a stored result (answer_integrity.annotate_results), reduced to what the report may show."""
    rv = p.get("review") if isinstance(p, dict) else None
    if not isinstance(rv, dict) or rv.get("status") not in _REVIEW_STATUSES:
        return None
    out = {"status": rv["status"]}
    if isinstance(rv.get("index"), int):
        out["index"] = rv["index"]
    if isinstance(rv.get("reason"), str) and rv["reason"]:
        out["reason"] = rv["reason"]
    return out


def _accounting(posts, items):
    """One set of counts for the whole report. Every retrieved post is in exactly one bucket, so the numbers always add up:
    retrieved = in_report + excluded + not_selected + not_analyzed + unreviewed, and classified <= retrieved (the rest is stated)."""
    counts = {k: 0 for k in _REVIEW_STATUSES}
    unreviewed = 0
    for p in posts:
        rv = _review_of(p)
        if rv:
            counts[rv["status"]] += 1
        else:
            unreviewed += 1
    return {"retrieved": len(posts), "classified": len(items), "not_classified": len(posts) - len(items), **counts,
            "unreviewed": unreviewed, "reviewed": len(posts) - unreviewed}


def build_signal_report(posts, query=None, mode="topic", coverage=None, max_items=25):
    """The server-built `signal_report` for an answer: coverage, class breakdown and one evidence record per retrieved post."""
    posts = [p for p in (posts or []) if isinstance(p, dict)]
    items = []
    for i, p in enumerate(posts[:max_items], start=1):
        c = classify_post(p, query if mode != "general" else None)
        c["rank"] = i
        rv = _review_of(p)
        if rv:
            c["review"] = rv           # the post's place in the checked report ABOVE: the same record, not a second opinion
            _reconcile_with_checked_report(c, p)
        opp = extract_opportunity(p, c)
        if opp:
            c["opportunity"] = opp
        items.append(c)
    s = summarize(items, query, mode, total=len(posts))
    accounting = _accounting(posts, items)
    statement = s["statement"]
    if accounting["reviewed"]:
        a = accounting
        statement += (f" Of the {a['retrieved']} retrieved post{'s' if a['retrieved'] != 1 else ''}, {a['in_report']} "
                      f"{'appears' if a['in_report'] == 1 else 'appear'} in the checked report, {a['excluded']} "
                      f"{'was' if a['excluded'] == 1 else 'were'} excluded, {a['not_selected']} "
                      f"{'was' if a['not_selected'] == 1 else 'were'} retrieved but not selected for it"
                      + (f" and {a['not_analyzed']} had no readable text" if a["not_analyzed"] else "") + ".")
    return {"version": 1, "classifier": CLASSIFIER_VERSION, "mode": mode, "query": query if mode != "general" else None,
            "based_on": "stored or fetched posts only; no live web search", "coverage": dict(coverage or {}),
            "breakdown": s["counts"], "direct_demand_count": s["direct_demand"], "statement": statement, "items": items,
            "returned": len(posts), "classified": len(items), "accounting": accounting}


# ── general discovery: "show me recent threads" is a request for a SAMPLE, not a topic search ──────────────────────────
# The router may return no keywords AND no time_window_days for "run a general unfiltered sample of recent threads"
# ("recent" only becomes a default lookback later, in routes.py), so flintel.is_time_only_request() alone rejects it and the message used
# to degrade into a keyword search on the words of the sentence itself. This pure detector decides from the MESSAGE: it asks for
# discovery (a cue word) and names NO topic (every remaining word is generic). A topic anywhere ("... about CRM pain") makes it a
# normal topic search, whatever the router said, so a follow-up like "Yes, run a general sample" is never turned into the earlier
# topic's search.
_GD_CUE = re.compile(r"\b(?:unfiltered|general|generic|sample|samples|random|recent(?:ly)?|latest|newest|new|trending|popular|"
                     r"happening|going on|discussing|talking about|threads?|posts?|discussions?|stories|conversations?|submissions?)\b", re.I)
_GD_GENERIC = frozenset("""
yes yeah yep no nope ok okay sure please pls thanks thank go ahead do run show give get list see fetch pull find pick take bring me us my
i we you can could would will lets let s a an the of on in at for to from with and or but some any all more
general generic unfiltered unfilter filtered filter filters without topic topics keyword keywords sample samples random
recent recently latest newest new newly current currently today yesterday tonight week weeks month months day days hour hours
last past previous over within since
thread threads post posts discussion discussions story stories conversation conversations submission submissions item items
what whats is are was were be being been happening happen going goes people folks everyone users someone anyone talking discussing
saying posting about there out up now just only top trending popular front page hacker news hn ycombinator combinator reddit
platform platforms source sources both either right
this these those it its them they their our your his her not dont doesnt didnt cant wont isnt arent but yet still again always never ever
anything everything something nothing one ones every each other another such so too very really than then have has had having do does did doing
get got getting make made making use used using way ways thing things when while whose todays
running into mention mentions mentioned mentioning keep kept showing show shown lately away
moment tell where kinds attention peoples nobody offers among feed somebody build coming describing pointing difficulty hardest jobs fixed interesting surprise
say says said across main cannot cant live everybody anyones early based soon web internet across over
theyd happily adopters roundup switch love built dealing sick
""".split())


# MARKET-LEVEL DISCOVERY ("What problems are people discussing today?", "What are people willing to pay for?", "What unmet needs are appearing?"):
# a question starter, at least one core discovery term, and NOTHING else but a closed list of generic words. Any word outside the lists (a product,
# an industry, a brand, "invoicing", "healthcare", "Stripe") makes it a topic search, so topic search is never captured by this path.
_GD_QUESTION = re.compile(r"^\s*(?:yes[ ,]+|ok[ ,]+|please[ ,]+)?(?:what|which|find|show|list|tell|give|where|who|any|are\s+there)\b", re.I)
_GD_CORE = frozenset("""problem problems issue issues complaint complaints complain complaining complained requesting requested requests request struggling
struggle struggles opportunities opportunity unmet needs need help dissatisfied unhappy frustrated annoyed switching switched willing emerging pain
trends trend products tools competitors competing asking solve solving wish demand gaps gap excited concerned worried hoping begging abandoning
leaving replacing moving difficulties headaches interest traction rise rising growing heating alternatives frustrations frustration validating
searching hottest trending topics looking pay
grumbling gripes gripe annoying annoyed bothering wishing demanding disappointed desperate eager broken stuck failing underserved discussed
ask asking asked unhappy dissatisfied""".split())
_GD_BIZ = frozenset("""businesses business small companies company customers customer founders founder teams team owners consumers buyers professionals
market markets industry industries software saas startup startups operational actively repeatedly recurring commonly frequently often most biggest
common unhappy useful become could that which who how why when lately daily weekly right product tool services service solutions solution ideas idea
point points pay paying paid try trying appearing appear experiencing experience experienced finding found buy buying want wanting looking look
search ask asked unsatisfied dissatisfied emerge competitor needing needed
posting posted excited existed exist exists existing starting started hottest hot topics topic surprise vendors vendor tools areas area hard
heating heated markets validating concerned newly fresh begging gaps abandoning leaving replacing current difficult new""".split())


# TIER 2 (grammar, not vocabulary): the closed list above cannot enumerate every way to phrase "what are people complaining about lately?". An
# otherwise-generic market-level question may also contain words outside the list when ALL of them are verb/adverb forms (-ing, -ed, -en, -ly,
# -est: "grumbling", "spiking", "broken", "loudest") AND they stand BEFORE the first topic-introducing preposition (about, regarding, for, with, from,
# to, of, against, using, like, versus, in, on, at...). Topics are named after such prepositions ("complaints about Stripe", "alternative to Trello",
# "needs in dental billing"), so a word after one that is not in the closed lists is treated as a possible topic and rejects the shortcut. Any
# digit, any capitalised word that is not the first word or a known platform name, and any noun-like unknown word also reject it. Every rejection is
# safe: the message simply goes to the normal router.
_GD_TOPIC_PREP = frozenset("about regarding concerning for with from to of against using like vs versus in on at into onto within toward towards".split())
_GD_VERBAL = re.compile(r"^[a-z]{3,}(?:ing|ed|en|ly|est)$")
_GD_PLATFORM_CAPS = frozenset("hacker news hn reddit".split())


def _gd_only_verbal_before_any_topic_slot(text, words):
    try:
        if re.search(r"\d", text):
            return False
        raw = re.findall(r"[A-Za-z][A-Za-z']*", text)
        for i, w in enumerate(raw):
            if i > 0 and w[0].isupper() and w.lower().replace("'", "") not in _GD_PLATFORM_CAPS and w not in ("I", "I'm", "I'd", "I'll"):
                return False
        seen_prep = False
        for w in words:
            known = w in _GD_GENERIC or w in _GD_BIZ or w in _GD_CORE
            if w in _GD_TOPIC_PREP:
                seen_prep = True
            if known:
                continue
            if seen_prep or not _GD_VERBAL.match(w):
                return False
        return True
    except Exception:
        return False


def keywords_are_generic_discovery(keywords) -> bool:
    """Second safety net, applied to what the ROUTER extracted: true only when every word of every keyword is in the closed generic vocabulary and at
    least one is a core discovery term ("complaints", "unmet needs", "begging"). A router that turned "What are people begging the internet for?" into
    keywords like ["begging", "internet"] has then NOT found a topic, so the request is general discovery rather than a narrow keyword search for those
    words. A single topic-bearing keyword ("Notion", "invoicing") makes this False. Pure; never raises."""
    try:
        words = []
        for k in keywords or []:
            if not isinstance(k, str):
                return False
            words += [w.replace("'", "") for w in re.findall(r"[a-z][a-z']*", k.lower())]
        if not words or not any(w in _GD_CORE for w in words):
            return False
        return not [w for w in words if w not in _GD_GENERIC and w not in _GD_BIZ and w not in _GD_CORE]
    except Exception:
        return False


def is_general_discovery_request(query) -> bool:
    """True when the message asks for a general sample / recent threads and names no topic at all. Pure; never raises.
    "Yes, run a general unfiltered sample of recent threads." / "What are people discussing?" / "Give me recent Hacker News posts" /
    "Show me what is happening on Hacker News" -> True.  "Show recent threads about CRM pain" / "latest iPhone" /
    "Is anyone looking for an AI agent?" -> False (a topic, or no discovery cue)."""
    try:
        if not isinstance(query, str) or not query.strip():
            return False
        text = " ".join(query.split())
        words = [w.replace("'", "") for w in re.findall(r"[a-z][a-z']*", text.lower())]
        if _GD_CUE.search(text):
            if not [w for w in words if w not in _GD_GENERIC]:
                return True
        # market-level discovery (see the note above): starter + a core discovery term + only generic vocabulary
        if _GD_QUESTION.search(text) and any(w in _GD_CORE for w in words):
            unknown = [w for w in words if w not in _GD_GENERIC and w not in _GD_BIZ and w not in _GD_CORE]
            if not unknown:
                return True
            return _gd_only_verbal_before_any_topic_slot(text, words)
        return False
    except Exception:
        return False


# ── zero-result explanations ──────────────────────────────────────────────────────────────────────────────────────────────
DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
SOURCE_UNAVAILABLE, COLLECTION_FAILED, NO_POSTS_COLLECTED = "SOURCE_UNAVAILABLE", "COLLECTION_FAILED", "NO_POSTS_COLLECTED"
NONE_IN_RANGE, EXCLUDED_BY_FILTERS, NO_TOPIC_MATCH, UNKNOWN = "NONE_IN_RANGE", "EXCLUDED_BY_FILTERS", "NO_TOPIC_MATCH", "UNKNOWN"
UNSUPPORTED_MODE, PARTIAL_RETRIEVAL = "UNSUPPORTED_MODE", "PARTIAL_RETRIEVAL"
_FAILURE_CODES = (COLLECTION_FAILED, DATABASE_UNAVAILABLE)
ZERO_CODES = (SOURCE_UNAVAILABLE, COLLECTION_FAILED, NO_POSTS_COLLECTED, NONE_IN_RANGE, EXCLUDED_BY_FILTERS, NO_TOPIC_MATCH, UNKNOWN,
              DATABASE_UNAVAILABLE, UNSUPPORTED_MODE, PARTIAL_RETRIEVAL)


def _d(iso):
    return str(iso)[:16].replace("T", " ") + " UTC" if iso else None


def explain_zero_result(cov):
    """One of six DISTINCT explanations (plus an honest UNKNOWN) from real coverage numbers. `cov` keys (all optional):
        source, mode ("general"|"topic"), source_available, error, total (stored/available posts for the source, None = unknown),
        in_range (posts inside the requested window, None = unknown), examined (posts examined after the window), excluded_filters
        (posts dropped by retrieval filters such as text-required / platform), window_start, window_end, newest, oldest, topic.
    Returns {"code","headline","message","next_steps","facts"}. Never raises."""
    c = cov if isinstance(cov, dict) else {}
    src = c.get("source") or "the selected source"
    total, in_range, examined, excl = c.get("total"), c.get("in_range"), c.get("examined"), c.get("excluded_filters")
    mode, topic = c.get("mode") or "topic", c.get("topic")
    win = (f" between {_d(c.get('window_start'))} and {_d(c.get('window_end'))}" if c.get("window_start") and c.get("window_end")
           else (f" since {_d(c.get('window_start'))}" if c.get("window_start") else ""))
    facts = {k: c.get(k) for k in ("source", "mode", "total", "in_range", "examined", "excluded_filters", "window_start", "window_end", "newest", "oldest")
             if c.get(k) is not None}

    def out(code, headline, message, steps):
        return {"code": code, "headline": headline, "message": message, "next_steps": steps, "facts": facts}
    if c.get("source_available") is False:
        return out(SOURCE_UNAVAILABLE, f"{src} is not available", f"{src} is not enabled or not collected yet, so it was not searched.",
                   ["Select a source that is available (Reddit, or Hacker News when enabled) and search again."])
    if c.get("mode_unsupported"):
        lab = c.get("mode_label") or str(c["mode_unsupported"]).title()
        return out(UNSUPPORTED_MODE, f"{src} does not support {lab} search",
                   f"{src} has no {str(lab).lower()} source, so it was not searched in {lab} mode. This says nothing about whether discussions exist: "
                   "the source was simply not queried.",
                   ["Switch to Historical or Combined to search the stored conversations for this source.",
                    "Or select a source that supports this mode (Hacker News supports Live)."])
    if c.get("error") and c.get("db_unavailable"):
        return out(DATABASE_UNAVAILABLE, f"The {src} data store could not be reached",
                   f"The database holding {src} posts could not be queried ({c['error']}). This is an error, not an empty result: posts may exist.",
                   ["Run the same search again in a minute.", "If it keeps failing, the database connection needs attention."])
    if c.get("error"):
        return out(COLLECTION_FAILED, f"{src} could not be searched",
                   f"The search of {src} failed ({c['error']}). This is an error, not an empty result: posts may exist.",
                   ["Run the same search again in a minute.", "If it keeps failing, the data source or its connection needs attention."])
    if total == 0:
        return out(NO_POSTS_COLLECTED, f"No {src} posts have been collected",
                   f"{src} has no posts available to search yet, so nothing could match.",
                   ["Wait for collection to run, or check that collection is enabled for this source."])
    if total and in_range == 0:
        span = ""
        if c.get("newest") or c.get("oldest"):
            span = " Available posts run from " + (_d(c.get("oldest")) or "unknown") + " to " + (_d(c.get("newest")) or "unknown") + "."
        return out(NONE_IN_RANGE, f"No {src} posts in the requested date range",
                   f"{src} has posts, but none were published{win or ' in the requested period'}." + span,
                   ["Search a wider or different date range" + (f" (the newest available post is from {_d(c['newest'])})" if c.get("newest") else "") + "."])
    if in_range and (examined is None or examined > 0) and excl and (examined is not None and excl >= examined):
        return out(EXCLUDED_BY_FILTERS, f"{src} posts exist in range, but retrieval filters excluded them",
                   f"{examined} {src} post{'s' if examined != 1 else ''} fell in the period{win}, but all were excluded by retrieval rules "
                   "(for example: only posts with readable text are used as evidence).",
                   ["Ask for a wider period to get more posts.", "Link-only posts have no text to quote, so they cannot be used as evidence."])
    if in_range and mode == "general":
        return out(EXCLUDED_BY_FILTERS, f"{src} posts exist in range, but none could be returned",
                   f"{in_range} {src} post{'s' if in_range != 1 else ''} fell in the period{win}, but none passed the retrieval rules.",
                   ["Try a wider period."])
    if in_range:
        return out(NO_TOPIC_MATCH, f"{src} posts exist in range, but none match this topic",
                   f"{in_range} {src} post{'s' if in_range != 1 else ''} fell in the period{win}"
                   + (f", but none was about {topic!r}." if topic else ", but none matched the topic."),
                   ["Try different or broader wording.", "Ask for a general sample of recent threads to see what is available."])
    return out(UNKNOWN, f"No {src} posts matched", "No posts matched. Coverage numbers were not available for this source, so the reason cannot be narrowed down"
               + (f" ({c['note']})." if c.get("note") else "."),
               ["Try a wider period or different wording."])


def combine_zero_results(explanations):
    """Merge per-source explanations into one answer block without hiding any source's reason."""
    ex = [e for e in explanations if isinstance(e, dict)]
    if not ex:
        return None
    if len(ex) == 1:
        return ex[0]
    steps = []
    for e in ex:
        for s in e["next_steps"]:
            if s not in steps:
                steps.append(s)
    codes = [e["code"] for e in ex]
    if len(set(codes)) == 1:
        code = codes[0]
    elif any(c in _FAILURE_CODES for c in codes) and any(c not in _FAILURE_CODES for c in codes):
        code = PARTIAL_RETRIEVAL        # one source FAILED while another merely found nothing: absence cannot be concluded for the failed part
    else:
        code = "MIXED"
    return {"code": code,
            "headline": "; ".join(e["headline"] for e in ex), "message": " ".join(e["message"] for e in ex), "next_steps": steps,
            "facts": {"sources": [e["facts"] for e in ex]}}
