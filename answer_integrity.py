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

import bisect
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


SUMMARY_UNAVAILABLE = "Summary unavailable"
SUMMARY_MIN_OVERLAP = 0.5
# Words that say nothing about which post a summary describes.
_SUMMARY_STOP = set("""
a an and are as at be but by for from has have her his how in is it its of on or so that the their them they
this to was were what when which who why will with you your user users author poster post posts ask asks
asking says said want wants need needs looking seek seeks seeking someone people
""".split())


def _numbers(text) -> set:
    """Whole numbers in `text`, without currency signs or thousands commas."""
    return {re.sub(r"[$€£,]", "", n).lower() for n in _NUMBER.findall(str(text or ""))}


def _summary_words(summary) -> set:
    return {w for w in _tokens(summary) if len(w) >= 3 and not w.isdigit() and w not in _SUMMARY_STOP}


def _describes(summary) -> bool:
    """A summary with enough content words to say which post it is about."""
    return len(_summary_words(summary)) >= 3


def _summary_claims(summary, sig) -> bool:
    """A summary that says something checkable: it describes a post, or it
    states a number the stored post does not contain."""
    return isinstance(summary, str) and (_describes(summary) or not _numbers(summary) <= _numbers(
        f"{sig.get('title') or ''} {sig.get('post_text') or ''}"))


def summary_supported(summary, title, text) -> bool:
    """True when at least half of the summary's content words occur in the
    post's title or text and every number it states occurs there as a whole
    number. Decides only whether a VERIFIED row's summary is shown — word
    overlap is never used to decide which post an analysis belongs to."""
    words = _summary_words(summary)
    if not words:
        return False
    post = f"{title or ''} {text or ''}"
    stems = {_stem(t) for t in _tokens(post)}
    if sum(1 for w in words if _stem(w) in stems) / len(words) < SUMMARY_MIN_OVERLAP:
        return False
    return _numbers(summary) <= _numbers(post)


QUOTE_MIN_WORDS = 4


def _post_number(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def analysis_identity(sig: dict, model_post: dict, position, other_posts) -> str:
    """How the model's entry is tied to this stored post, or None when it
    cannot be established:
      "title" — the model's own title equals the stored title AND its post
                number is this post's number in the prompt;
      "quote" — its quotation (QUOTE_MIN_WORDS+ words) occurs in this post
                and in none of `other_posts` (the other retrieved posts,
                already normalised).
    The title and number are the model's own, kept by the patcher before it
    replaces them with stored values."""
    stored_title = _norm(sig.get("title"))
    number = _post_number(model_post.get("_model_index", model_post.get("index")))
    if stored_title and position is not None and number == position and \
            _norm(model_post.get("_model_title", model_post.get("title"))) == stored_title:
        return "title"
    quote = _norm(model_post.get("evidence"))
    if len(quote.split()) >= QUOTE_MIN_WORDS and quote in _norm(f"{sig.get('title') or ''} {sig.get('post_text') or ''}") \
            and not any(quote in other for other in other_posts):
        return "quote"
    return None


def post_fingerprint(title, text) -> tuple:
    """(normalised title + text, word stems) of one stored post, computed once
    and reused when a record is compared with every other post."""
    joined = f"{title or ''} {text or ''}"
    return _norm(joined), {_stem(t) for t in _tokens(joined)}


def _overlap(words: set, stems: set) -> float:
    return sum(1 for w in words if _stem(w) in stems) / len(words) if words else 0.0


_RECORD_LABELS = {"role": ROLES, "purchase_intent": tuple(_INTENT_ORDER), "product_fit": tuple(_FIT_ORDER)}


def check_post_record(record, title, text, others) -> dict:
    """A map-step record if it is consistent with THIS stored post, else None.
    `others` are post_fingerprint()s of every other analysed post. All must hold:
      - its title equals the stored title;
      - its quotation (QUOTE_MIN_WORDS+ words) occurs in this post and in no other post;
      - its summary is supported by this post (summary_supported) and no other
        post supports it better, so a valid quote cannot carry another post's summary.
    Labels outside the known values are dropped (the post's own text decides them)."""
    if not isinstance(record, dict) or not isinstance(record.get("title"), str) or not isinstance(record.get("quote"), str):
        return None
    if not _norm(title) or _norm(record["title"]) != _norm(title):
        return None
    own_norm, own_stems = post_fingerprint(title, text)
    quote = _norm(record.get("quote"))
    if len(quote.split()) < QUOTE_MIN_WORDS or quote not in own_norm or any(quote in o for o, _s in others):
        return None
    summary = record.get("summary")
    if not isinstance(summary, str) or not summary_supported(summary, title, text):
        return None
    words = _summary_words(summary)
    own = _overlap(words, own_stems)
    if any(_overlap(words, stems) > own for _o, stems in others):
        return None
    out = {"title": record["title"], "quote": record["quote"].strip(), "summary": summary.strip()}
    out.update({k: record[k] for k, allowed in _RECORD_LABELS.items() if record.get(k) in allowed})
    return out


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


# ── purchase signals ─────────────────────────────────────────────────────────
# A post can show five different things about buying. Only an explicit PURCHASE STEP qualifies a lead. The others are
# SIGNALS: recorded, shown in the row's reason, and never enough on their own or together.
#   budget      a budget or an amount ("budget is $200 a month"); NOT an approval
#   timing      asap, this quarter, a deadline, a quarter reference
#   evaluation  a trial, demo, pilot, shortlist, or a concrete quote request
#   willingness "would / will / happy to pay"
#   purchase step (qualifies): an approved / allocated / authorised budget, a procurement process under way, an
#     unconditional commitment to buy, sign, subscribe or switch, or a switch that is actually under way
# A step that is negated, hypothetical, exploratory, paused, still pending authorisation, or conditional (also when
# the condition is separated from it) is recorded as "unconfirmed" and does not qualify.
_BUDGET = re.compile(r"\bbudget\b|[$€£]\s?\d|\b(?:a|us|au|ca)\$\s?\d|\b\d[\d,.]*\s?(?:usd|aud|eur|gbp|dollars)\b|"
                     r"\b\d[\d,.]*\s?(?:/|per\s)\s?(?:mo|month|year|yr|seat|user)\b", re.I)
_TIMING = re.compile(r"\basap\b|\burgent(?:ly)?\b|\b(?:this|next) (?:week|month|quarter)\b|\b(?:for|by|in) Q[1-4]\b|"
                     r"\bdeadline\b|\bby (?:the )?end of\b|\bright away\b|\bimmediately\b", re.I)
_EVALUATION = re.compile(r"\bdemo\b|\btrial\b(?! and error)|\bpilot\b|\bproof of concept\b|\bpoc\b|\bshortlist\w*|\bevaluat\w+|"
                         r"\bcompar\w+|\btrying out\b|\b(?:send|get|need|want|request(?:ing)?) (?:me |us )?(?:a |an )?"
                         r"(?:quote|proposal|estimate)\b|\b(?:quote|proposal|estimate) for \d", re.I)
_WILLINGNESS = re.compile(r"(?:\b(?:would|could)|'d) (?:happily |gladly )?(?:pay|buy|purchase|subscribe)\b|"
                          r"\bwill (?:happily |gladly )?pay\b|'ll (?:happily |gladly )?pay\b|"
                          r"\b(?:willing|happy|glad) to (?:pay|buy|purchase|subscribe)\b|\bpay (?:for|after|if)\b", re.I)
_VERBS = r"(?:buy|purchase|sign|subscribe|switch|upgrade|replace|migrate|onboard)"
# explicit purchase steps, by kind. A bare budget amount is not in this list.
_STEP_KINDS = (
    ("approval", re.compile(
        r"\bbudget (?:is |has been |was |got |has |is now |is already )?(?:been )?(?:approved|allocated|signed off|secured|released|authori[sz]ed)\b|"
        r"\b(?:approved|allocated|authori[sz]ed|signed off on|released|secured) (?:the |our |a |an |my )?(?:\w+ )?budget\b|"
        r"\bfunding (?:is |has been |was )?(?:approved|secured|allocated)\b|"
        r"\b(?:approved|signed off|authori[sz]ed) by (?:finance|management|leadership|my boss|our boss|the board|the cfo|procurement)\b|"
        r"\bbudget approval (?:is |was |has been )?(?:granted|received|done|completed?)\b", re.I)),
    ("procurement", re.compile(
        r"\b(?:in|started|began|begun|initiated|launched) (?:the |our |a )?procurement (?:process|stage)\b|"
        r"\bprocurement (?:process|stage) (?:has |is )?(?:started|begun|underway|under way|in progress)\b|"
        r"\bpurchase order (?:is |has been |was )?(?:raised|issued|submitted|approved|ready)\b|"
        r"\b(?:raised|issued|submitted) (?:a |the |our )?purchase order\b", re.I)),
    ("commitment", re.compile(
        r"\b(?:ready|prepared|set|about|going|planning|plan|plans|intend|intends|intending|committed) to " + _VERBS.replace("|onboard", "|pay|onboard") + r"\b|"
        r"\b(?:we|i)(?:'ll| will|'re going to| are going to|'m going to| am going to|'re about to| are about to) (?:\w+ly )?" + _VERBS + r"\b|"
        r"\b(?:decided|chosen|agreed|resolved) to (?:buy|purchase|sign|subscribe|switch|upgrade|replace|migrate|go with)\b|"
        r"\bcan (?:buy|purchase) (?:it|this|one|that)? ?(?:today|now|right away|immediately)\b", re.I)),
    ("switch", re.compile(
        r"\b(?:we are|we're|i am|i'm|we have started|we've started) (?:currently )?(?:switching|migrating|replacing)\b", re.I)),
)
_STEP_NEG_BEFORE = re.compile(r"(?:\b(?:not|no|nobody|none|never|without|nothing|zero|cannot|neither|nor)\b|n't)"
                              r"(?:\s+(?!(?:and|so|then|but|because|while|although|though)\b)[\w']+){0,3}\s*$", re.I)
_EVAL_NEG_BEFORE = re.compile(r"\b(?:no|not|without any|skip)\s+(?:a |an |the |any )?$|\b(?:don't|do not) need (?:a |an |the )?$", re.I)
_YET_AFTER = re.compile(r"^[\w\s,'-]{0,12}\b(?:yet|any ?time soon)\b", re.I)
_CLAUSE_BREAK = re.compile(r"[;,]|\s[-–—]\s|[–—]|:|[()]|\bbut\b|\bhowever\b|\balthough\b|\bthough\b", re.I)
_HYPOTHETICAL = re.compile(r"\b(?:would|could|might|maybe|perhaps|someday|eventually|hypothetically|probably|possibly|potentially|"
                           r"thinking (?:about|of)|considering|in case)\b|\bif i were\b", re.I)
_MODAL_BEFORE = re.compile(r"(?:\b(?:would|could|might|may|maybe|perhaps|someday|eventually|hypothetically|probably|possibly|potentially)\b|"
                           r"\bthinking (?:about|of)\b|\bconsidering\b|\bin case\b)(?:\s+[\w']+){0,4}\s*$", re.I)
_EXPLORATORY = re.compile(r"\b(?:just|still|only) (?:exploring|looking|browsing|researching|evaluating|investigating)\b|\bexploring\b|"
                          r"\bwindow[- ]shopping\b|\bnot sure\b|\bundecided\b|\bno decision\b|\bno rush\b|\bearly stages?\b", re.I)
_PAUSED = re.compile(r"\b(?:on hold|paused?|frozen|freeze|postponed|delayed|stalled|suspended|cancel{1,2}ed|halted|shelved|put off|"
                     r"scrapped|slashed|already spent|used up|exhausted)\b", re.I)
_PENDING = re.compile(r"\b(?:pending|awaiting|waiting (?:for|on)|still (?:need|needs|require|requires|needed)|yet to|"
                      r"request(?:ed|ing) (?:the |our |a )?(?:budget )?(?:approval|sign[- ]?off|authori[sz]ation)|"
                      r"hoping|hope to|trying to get|tentative|proposed budget|in principle|"
                      r"not (?:yet|been) (?:authori[sz]ed|released|signed off|confirmed|approved|finali[sz]ed)|"
                      r"need(?:s)? (?:to )?(?:get )?(?:approval|sign[- ]?off|authori[sz]ation))\b", re.I)
_CONDITION = re.compile(r"\b(?:if|once|when|unless|provided|assuming|as long as|depending on|depends on|dependent on|contingent|"
                        r"subject to|only after|only when|after the (?:trial|pilot|demo))\b", re.I)
_BENIGN_IF = re.compile(r"\bif\s+(?:anyone|anybody|someone|somebody|you(?:'re| are)?\s+(?:know|have|can|could|are aware|happen)|"
                        r"there(?:'s| is| are)|any)\b", re.I)
_NEXT_CONDITION = re.compile(r"^(?:but |however,? |though |although |and |yet )?(?:only |just )?(?:if|once|when|unless|provided|"
                             r"assuming|as long as|depending on|(?:that|this|it) (?:all )?depends|contingent|subject to|pending|"
                             r"after the (?:trial|pilot|demo))\b", re.I)
_NEXT_REFERS = re.compile(r"^(?:it|this|that|they|everything|procurement|the (?:process|procurement|purchase|order|budget|approval|"
                          r"purchase order))\b", re.I)
# WHO a commitment is about. "me": the author or the author's own organisation (we / I, our team, our CTO, a department that is
# the author's); "other": customers, clients and other outsiders; "unclear": a named person, an unanchored role ("the CEO"), an
# unresolved pronoun. Only "other" and "unclear" stop a commitment from counting; "unclear" is never rated High on that basis.
# A commitment that is reported ("my boss told me we will buy", "I heard ...") or speculative ("I think we will buy", "we will
# probably buy", "someday") is unconfirmed whoever it names: first-person wording alone does not make it a verified commitment.
# KNOWN LIMIT: only buy / sign / switch commitments are attributed to an actor and checked for hearsay or speculation. An approved
# budget or a procurement step is not: "Our customers' budget was approved", "I heard the budget was approved" and "I think our
# budget is approved" still count, exactly as before. Attributing approvals changes the approval logic and is a separate stage.
# KNOWN LIMIT: a pronoun takes its actor from the same sentence only. "My boss wants a tool. He is ready to buy this month." leaves
# "He" unresolved (unclear, so never High). And the step is not tied to the product: in a post that does ask for the target
# product, "we will buy a new office chair" still counts. Both are left as they were (see the strict xfails in the tests).
_ROLES = (r"boss(?:es)?|managers?|supervisors?|directors?|ceo|cto|cfo|coo|cio|cmo|vp|head|founders?|co-?founders?|owners?|"
          r"colleagues?|co-?workers?|teammates?|employer|(?:team|tech|project|product|engineering|dev|design|data|marketing|ops) lead")
_ORG_NOUN = r"(?:company|team|org(?:ani[sz]ation)?|startup|agency|firm|business|department|dept|group|division|unit)"
_DEPT = (r"(?:marketing|sales|engineering|product|support|finance|legal|operations|ops|procurement|purchasing|security|design|"
         r"devops|accounting|(?-i:IT|HR))")
_EXT_NOUN = (r"(?:customers?(?!\s+(?:support|service|success|care|experience))|clients?|prospects?|buyers?|competitors?|vendors?|"
             r"leads?(?!\s+(?:gen|generation|developer|engineer|designer))|users?)")
# (pattern, class) in order; each match is masked so a later pattern cannot read it again
_ACTOR_TABLE = (
    (re.compile(r"\b" + _EXT_NOUN + r"(?:'s|s')\s+(?:[\w-]+\s+){0,2}[\w-]+", re.I), "other"),                # "my client's CEO"
    (re.compile(r"\b(?:we|i|us)\b", re.I), "self"),
    (re.compile(r"\b(?:our|my)\s+(?:[\w-]+\s+){0,2}(?:" + _ROLES + r")\b(?:\s+(?-i:[A-Z][a-z]{2,}))?", re.I), "org"),   # "my colleague Priya"
    (re.compile(r"\b(?:our|my)\s+(?:[\w-]+\s+){0,2}" + _ORG_NOUN + r"s?\b(?:\s+(?:of|with|from|at|in)\b.{0,80}?(?=\s(?:is|are|was|were|has|have|will)\b|$))?",
                re.I), "self"),
    (re.compile(r"\b(?:" + _EXT_NOUN + r"|people|folks|others|everyone|everybody|members|companies|businesses|startups|agencies|"
                r"firms|organi[sz]ations|teams|departments|groups)\b", re.I), "other"),
    (re.compile(r"\b(?:their|his|her)\s+(?:[\w-]+\s+){0,2}(?:" + _ROLES + r"|" + _ORG_NOUN + r")\b|\b(?:they|he|she|their|his|her)\b",
                re.I), "pronoun"),
    (re.compile(r"(?:\b(?:(?:the|this)\s+)?" + _DEPT + r"(?:\s+(?:team|department|dept|group))?(?:\s+(?:is|are|was|has|have|will|just|also|now|"
                r"finally|really|already)|'s|'ll)+|(?:^|\bthat\s+)(?:(?:the|this)\s+)?" + _DEPT + r"(?:\s+(?:team|department|dept|group))?)\s*$",
                re.I), "dept"),                                                                       # "Marketing is ..."
    (re.compile(r"\b(?:the|this)\s+(?:[\w-]+\s+)?" + _ORG_NOUN + r"\b", re.I), "generic"),            # "the company is ..."
    (re.compile(r"\b(?:a|an|another|other|different|some)\s+(?:[\w-]+\s+)?" + _ORG_NOUN + r"\b|\b(?:someone|somebody|anyone|anybody)\b|"
                r"\b(?:a|my)\s+(?:friend|guy|person|contact|acquaintance|wife|husband|brother|sister|dad|mom|father|mother|"
                r"neighbou?r|cousin)\b|\b(?:the |a |an )?(?:" + _ROLES + r")\b", re.I), "unclear"),
    (re.compile(r"\b(?!(?:Our|My|We|The|This|That|It|Its|They|Their|He|She|His|Her|Yes|Yeah|Yep|Ok|Okay|Hi|Hey|Hello|Thanks|Update|Edit|"
                r"Also|Now|Today|And|But|So|Then|Because|While|When|Once|If|Unless|Ready|Set|Going|Planning|Decided|Chosen|Agreed|"
                r"Budget|Still|Just|Really|Already|Finally|Actually|Soon|Almost|Always|Well|Both|All|Plus|Yet|Sure|Right|Reddit|"
                r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\b)(?![A-Za-z]+ly\b)[A-Z][a-z]{2,}(?:\s[A-Z][a-z]+)?"
                r"(?='s\b|\s+(?:and|is|was|has|had|said|says|told|wants?|wanted|will|just|also|now|finally|really|already|thinks?|asked|"
                r"mentioned|confirmed|keeps)\b)"), "unclear"),                                            # "Sarah is ..."
)
_NAME_ALONE = re.compile(r"^\s*(?![A-Za-z]+ly\b)(?!(?:Our|My|We|The|This|That|It|Its|Still|Just|Also|Now|Well|Yes|Ok|Okay|And|But|So)\b)"
                         r"[A-Z][a-z]{2,}(?:\s[A-Z][a-z]+)?\s*$")             # only before a finite step: "Sarah decided to buy"
_OWN_SUBJECT = re.compile(r"(?:we|i)\b", re.I)
_FINITE_STEP = re.compile(r"(?:decided|chosen|agreed|resolved|committed|plans?|intends?|will|'ll)\b", re.I)
# hearsay: the author did not see the commitment, they heard or read it. HEDGE is the author's own hedge ("I heard we will buy");
# REPORTED is someone else's word relayed ("my boss said he is ready to buy", "my boss told me we will buy", "a survey says ...").
# Both apply to every actor, "we will buy" included. A statement the author makes in their own voice is neither.
_HEDGE = re.compile(r"\bi (?:heard|hear|gather|understand|was told)\b|\b(?:apparently|reportedly|supposedly|rumou?r\w*|word is|word has it)\b", re.I)
_HEDGE_AFTER = re.compile(r"^[^.;!?]{0,25}\b(?:apparently|reportedly|supposedly|or so i (?:heard|hear)|i heard|so i hear|they say)\b", re.I)
_REPORTED = re.compile(r"\bi (?:read|saw|learned|asked|spoke (?:to|with)|talked (?:to|with)|checked with)\b|\baccording to\b|"
                       r"\bsurvey\w*|\breports?\b|\bstud(?:y|ies)\b|\banalysts?\b|\bforecasts?\b|\bpredict\w*|"
                       r"\b(?:they|people|experts) say\b|\b(?:he|she|they|boss(?:es)?|managers?|ceo|cto|cfo|coo|colleagues?|co-?workers?|"
                       r"teammates?|co-?founders?|(?-i:[A-Z][a-z]{2,})) (?:said|says|told me|tells me|mentioned|mentions|thinks|thought|"
                       r"believes|reckons|claims|claimed|announced)\b", re.I)
# uncertainty that qualifies THE STEP, whoever it names ("we will buy" included). G1's markers (would / maybe / probably / someday /
# eventually ...) stay: _MODAL_BEFORE for the words just before the step, and the same adverbs here when they sit in or right after
# it. The added markers (likely, hopefully, at some point, "I think / guess / hope ...") apply only in that same local window: up to
# four words before the step, inside it, or within six words after it, never across "and / so / but / because" or a clause break,
# and never to a different sentence. So "I think the tools are great and we will buy" and "we will buy. Hopefully it works" stand.
_NOT_A_BREAK = r"(?!(?:and|so|then|but|because|while|although|though)\b)"
_UNCERTAIN = (r"(?:hopefully|likely|at some point|sometime|one day|hope that|"
              r"i (?:think|believe|guess|suppose|assume|bet|reckon|imagine|expect|hope))")
_G1_ADVERB = r"(?:probably|possibly|potentially|maybe|perhaps|someday|eventually)"
_UNCERTAIN_BEFORE = re.compile(r"\b" + _UNCERTAIN + r"\b(?:\s+" + _NOT_A_BREAK + r"[\w']+){0,4}\s*$", re.I)
_UNCERTAIN_NEAR = re.compile(r"\b(?:" + _UNCERTAIN + "|" + _G1_ADVERB + r")\b", re.I)
# After the step, an adverb (likely, hopefully, ...) qualifies it as before. "I think / guess / hope ..." qualifies it only when it
# ends the clause (", I guess."); followed by more words (", I think it is great.") it is an opinion about something else.
_UNCERTAIN_AFTER_ADVERB = re.compile(r"\b(?:hopefully|likely|at some point|sometime|one day|"
                                     r"probably|possibly|potentially|maybe|perhaps|someday|eventually)\b", re.I)
_AFTER_WINDOW = 120
_COMMA_OR_PAREN = re.compile(r"[,(]")
_UNCERTAIN_AFTER_VERB = re.compile(r"\bi (?:think|believe|guess|suppose|assume|bet|reckon|imagine|expect|hope)\b[\s,.)]*$", re.I)
# departments the author names as their own or as another ("I work in sales", "our legal team"), to tell them apart from the subject
_DEPT_MENTION = re.compile(r"\b(?:(?:in|from|of|lead|leads|run|runs|head|manage|manages)\s+(?:the\s+)?(" + _DEPT + r")\b|(" + _DEPT +
                           r")\s+(?:team|department|dept|group)\b)", re.I)
_SUBJECT_BREAK = re.compile(r"\b(and|so|then|because|while)\b", re.I)
_AFTER_BREAK = re.compile(r"\b(?:and|so|then|because|while|but|however|although|though)\b|[.;!?]", re.I)
# the step clause has no subject of its own ("..., ready to buy this month"): it takes the previous clause's
_CLAUSE_VERB = re.compile(r"\b(?:is|are|was|were|has|have|had|wants?|wanted|needs?|needed|asked|asks|said|says|told|keeps?|looking|looks|"
                          r"mentioned|thinks?|thought|loves?|loved|likes?|liked|uses?|using|require[sd]?|expects?|insists?)\b", re.I)
_SELF_ROLE = re.compile(r"\bhere\b|^\s*as\b", re.I)                      # "CEO here, ready to buy" is the author speaking
_ELIDED = re.compile(r"^(?:\s*(?:and|so|also|just|now|then|finally|really|already|currently|all|is|are|was|were|be|been)\b)*\s*$", re.I)
_STALE = re.compile(r"\blast (?:quarter|year)\b|\bpreviously\b|\b\d+ (?:months?|years?) ago\b|\balready (?:bought|purchased|spent|used|signed)\b|"
                    r"\b(?:we|i) (?:bought|purchased)\b", re.I)
_QUESTION = re.compile(r"^(?:is|are|do|does|did|can|could|should|will|would|have|has)\b", re.I)


def _clause_spans(sentence: str) -> list:
    spans, pos = [], 0
    for m in _CLAUSE_BREAK.finditer(sentence):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(sentence)))
    return [(a, b) for a, b in spans if sentence[a:b].strip()]


def _actors(text: str) -> list:
    """[(position, class, text)] of the actors named in text, left to right."""
    toks = []
    for rx, cls in _ACTOR_TABLE:
        for m in rx.finditer(text):
            toks.append((m.start(), cls, m.group(0)))
        text = rx.sub(lambda m: " " * len(m.group(0)), text)
    return sorted(toks)


def _context_facts(previous: str, sentence: str) -> dict:
    """What the sentence and the one before it say about WHOSE organisation "the company" or "marketing" is."""
    toks = _actors(previous[-300:] + " " + sentence)
    cut = len(previous[-300:]) + 1
    return {"anchor": any(c in ("self", "org") for _p, c, _x in toks),
            "foreign": any(c in ("other", "unclear") and not re.fullmatch(_DEPT, x.strip(), re.I) for p, c, x in toks if p >= cut),
            "depts": {(a or b).lower() for a, b in _DEPT_MENTION.findall(previous[-300:] + " " + sentence)}}


def _class_of(toks: list, base: int, prefix: str, facts) -> tuple:
    """(class, via_pronoun) of the last actor: "me" (the author), "org" (the author's own boss, colleague, department or
    company), "other" or "unclear". A pronoun takes the class of the nearest actor named before it. A generic company, team or
    department is the author's only when the sentence or the one before anchors it to the author ("we", "our") and names no
    one else; a department must also be the only one named."""
    if not toks:
        return "unclear", False
    _pos, cls, word = toks[-1]
    pron = cls == "pronoun"
    if pron:
        before = [t for t in _actors(prefix[:base + _pos]) if t[1] != "pronoun"]
        _pos, cls, word = before[-1] if before else (0, "unclear", "")
    if cls in ("generic", "dept"):
        own = (re.search(_DEPT, word, re.I) or re.search("$", word)).group(0).lower()
        f = facts()                                            # computed only when a generic company or department needs it
        ok = f["anchor"] and not f["foreign"] and (cls == "generic" or f["depts"] <= {own})
        return ("org" if ok else "unclear"), pron
    return {"self": "me", "org": "org"}.get(cls, cls), pron


def _actor_of(before: str, previous: str = "", finite: bool = False, prefix: str = "", facts=None,
              own_subject: bool = False) -> tuple:
    """(whose commitment a "ready to buy"-type step is, whether it was found through a pronoun). "me": the author. "org":
    the author's own boss, colleague, cofounder, executive, department or company ("our CTO has decided to buy", "our
    marketing team is ready to buy"). "other": customers, clients and other outsiders. "unclear": a named person, an
    unanchored role, an unresolved pronoun, a generic company or department with nothing tying it to the author. A
    subject-less step ("Ready to buy this month") is the author's; one that continues a clause about someone else takes
    that someone. First person wins when both are present."""
    if own_subject:
        return "me", False                                     # the step names its own subject: "we will buy", "I'm switching"
    facts = facts or (lambda: {"anchor": False, "foreign": False, "depts": set()})
    parts = _SUBJECT_BREAK.split(before)                      # [text, break, text, break, text ...]
    if len(parts) >= 3 and parts[-2].lower() == "and" and re.fullmatch(r"\s*(?:i|we|me|us)\s*", parts[-3], re.I):
        return "me", False                                     # "I and my boss are ready to buy"
    last = parts[-1]
    toks = _actors(last)
    if toks:
        return _class_of(toks, len(prefix) - len(last), prefix, facts)
    if finite and _NAME_ALONE.match(last):
        return "unclear", False
    ctx = parts[-3] if len(parts) >= 3 else previous           # "My customers want a tool and are ready to buy"
    if _ELIDED.match(last if len(parts) >= 3 else before) and ctx and _CLAUSE_VERB.search(ctx) and not _SELF_ROLE.search(ctx):
        ctoks = _actors(ctx)
        if ctoks and not any(c == "self" for _p, c, _x in ctoks):
            return _class_of(ctoks, 0, ctx, facts)
    return "me", False


def _uncertain_after(tail: str) -> bool:
    """Uncertainty right after the step. An adverb counts within six words. "I think / guess ..." counts when it ends the clause
    after at most one comma or parenthesis, and the clause really ends there (not at the edge of the window). All of it stops at
    "and / so / but / because" and at the end of the sentence."""
    parts = _AFTER_BREAK.split(tail)
    near = " ".join(_AFTER_BREAK.split(tail[:60])[0].split()[:6])
    ends = len(parts) > 1 or len(tail) < _AFTER_WINDOW
    return bool(_UNCERTAIN_AFTER_ADVERB.search(near)
                or (ends and len(_COMMA_OR_PAREN.findall(parts[0])) <= 1 and _UNCERTAIN_AFTER_VERB.search(parts[0])))


def _is_condition(clause: str) -> bool:
    return bool(_CONDITION.search(clause)) and not _BENIGN_IF.search(clause)


def _sentence_facts(sentences: list, i: int, cache: dict) -> dict:
    """Everything about sentence i that does not depend on WHICH step matched, computed once per sentence."""
    c = cache.get(i)
    if c is None:
        s = sentences[i]
        nxt = sentences[i + 1] if i + 1 < len(sentences) else ""
        spans = _clause_spans(s)
        refers = bool(_NEXT_REFERS.search(nxt))
        c = cache[i] = {
            "spans": spans, "starts": [a for a, _b in spans], "expl": bool(_EXPLORATORY.search(s)),
            "paused": bool(_PAUSED.search(s)) or (refers and bool(_PAUSED.search(nxt))),
            "pending": bool(_PENDING.search(s)) or (refers and bool(_PENDING.search(nxt))),
            "cond": [_is_condition(s[a:b]) for a, b in spans],
            "next_cond": bool(_NEXT_CONDITION.search(nxt.strip())) and not _BENIGN_IF.search(nxt)}
    return c


def _why_unconfirmed(sentences: list, i: int, m, kind: str, cache: dict = None):
    """None when the step stands; otherwise why it does not count (see the rules above). Per-sentence facts are cached so
    the cost does not grow with the number of steps in one sentence."""
    s = sentences[i]
    c = _sentence_facts(sentences, i, cache if cache is not None else {})
    spans = c["spans"]
    k = max(bisect.bisect_right(c["starts"], m.start()) - 1, 0)
    start = spans[k][0] if spans else 0
    clause = s[start:spans[k][1]] if spans else s
    before = s[max(start, m.start() - 80):m.start()]                       # bounded look-behind
    if kind in ("commitment", "switch"):
        prefix = s[max(0, m.start() - 240):m.start()]
        who, pron = _actor_of(before, s[spans[k - 1][0]:spans[k - 1][1]][-120:] if spans and k else "",
                              bool(_FINITE_STEP.match(m.group(0))), prefix, lambda: c.get("who") or c.setdefault(
                                  "who", _context_facts(sentences[i - 1] if i else "", s)), bool(_OWN_SUBJECT.match(m.group(0))))
        if who in ("other", "unclear"):
            return "other" if who == "other" else "ambiguous"
        seen = prefix if pron else _SUBJECT_BREAK.split(before)[-1]
        if _HEDGE.search(seen) or _REPORTED.search(seen) or _HEDGE_AFTER.search(s[m.end():m.end() + 40]):
            return "hearsay"                                   # a reported statement, whoever it names, "we will buy" included
        if (_UNCERTAIN_BEFORE.search(before) or _UNCERTAIN_NEAR.search(m.group(0))
                or _uncertain_after(s[m.end():m.end() + _AFTER_WINDOW])):
            return "hypothetical"                              # the step itself is qualified as a guess or vague, whoever it names
    if _MODAL_BEFORE.search(before):
        return "hypothetical"
    if c["expl"]:
        return "exploratory"
    if c["paused"]:
        return "paused"
    if c["pending"] or _YET_AFTER.search(s[m.end():m.end() + 30]):
        return "pending"
    rel = m.start() - start
    if _STALE.search(clause[max(0, rel - 200):rel + 200]):
        return "past"
    if any(c["cond"][n] for n in (k - 1, k, k + 1) if 0 <= n < len(c["cond"])):
        return "conditional"
    if kind in ("commitment", "switch") and c["next_cond"]:
        return "conditional"
    return None


def purchase_signals(title, text) -> dict:
    """The separate buying signals in a post's own words (see the block comment above).
    {"request": the author asks for a tool or solution,
     "budget"/"timing"/"evaluation"/"willingness": [sentences],
     "purchase_step": [sentences of steps that stand],
     "unconfirmed": [{"sentence", "why"}] steps that were found but do not count}"""
    sents = _sentences(title) + _sentences(text)
    cache = {}
    out = {"request": any(_REQUEST.search(s) and _SOLUTION.search(s) for s in sents),
           "budget": [], "timing": [], "evaluation": [], "willingness": [], "purchase_step": [], "unconfirmed": []}

    def add(key, s):
        if s not in out[key]:
            out[key].append(s)
    for i, s in enumerate(sents):
        for m in _BUDGET.finditer(s):
            if not _NEGATION.search(s[max(0, m.start() - 30):m.start()]):
                add("budget", s)
                break
        for m in _TIMING.finditer(s):
            if not _STEP_NEG_BEFORE.search(s[max(0, m.start() - 30):m.start()]):
                add("timing", s)
                break
        for m in _EVALUATION.finditer(s):
            if not _EVAL_NEG_BEFORE.search(s[max(0, m.start() - 14):m.start()]):
                add("evaluation", s)
                break
        for m in _WILLINGNESS.finditer(s):
            if not _STEP_NEG_BEFORE.search(s[max(0, m.start() - 30):m.start()]):
                add("willingness", s)
                break
        if s.rstrip().endswith("?") and _QUESTION.search(s):
            continue                                              # "Is the budget approved?" states nothing
        for kind, rx in _STEP_KINDS:
            for m in rx.finditer(s):
                if _STEP_NEG_BEFORE.search(s[max(0, m.start() - 30):m.start()]):
                    continue
                why = _why_unconfirmed(sents, i, m, kind, cache)
                if why:
                    if not any(u["sentence"] == s and u["why"] == why for u in out["unconfirmed"]):
                        out["unconfirmed"].append({"sentence": s, "why": why})
                else:
                    add("purchase_step", s)
    return out


def _ceiling(sg: dict) -> str:
    if not sg["request"]:
        return "none"
    return "explicit" if sg["purchase_step"] else "research"


def _intent_ceiling(title: str, text: str) -> str:
    return _ceiling(purchase_signals(title, text))


_UNCONFIRMED_WORDS = {"conditional": "a purchase statement that is conditional",
                      "hypothetical": "a hypothetical purchase statement",
                      "exploratory": "a purchase statement from someone still exploring",
                      "paused": "a purchase that is on hold",
                      "other": "a purchase statement about someone else",
                      "ambiguous": "a purchase statement whose buyer is unclear",
                      "hearsay": "a purchase statement the author only heard or read about",
                      "past": "an approval or purchase that is already in the past",
                      "pending": "a purchase or budget that is not yet authorised"}


def _signal_reason(sg: dict, rated_down: bool = False):
    """What the post does state about buying; None when it states nothing. Without `rated_down` the post states no
    confirmed purchase step; with it the post does, but the analysis rated the post as research only."""
    bits = []
    if sg["budget"]:
        bits.append("a budget")
    if sg["timing"]:
        bits.append("a timeline")
    if sg["evaluation"]:
        bits.append("a trial, demo or quote request")
    if sg["willingness"]:
        bits.append("willingness to pay")
    for why in dict.fromkeys(u["why"] for u in sg["unconfirmed"]):
        bits.append(_UNCONFIRMED_WORDS[why])
    if rated_down:
        bits.append("an explicit purchase step")
    if not bits:
        return None
    joined = bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]
    if rated_down:
        return f"Asks for a solution and states {joined}, but the analysis rated this post as research only."
    return f"Asks for a solution and states {joined}, but no confirmed purchase step."


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


def _pick_evidence(title: str, text: str, role, trigger, sg=None):
    sents = _sentences(title) + _sentences(text)
    if role == "buyer":
        sg = sg if sg is not None else purchase_signals(title, text)
        signalled = [s for s in sents if any(s in sg[k] for k in ("budget", "timing", "evaluation", "willingness"))]
        both = [s for s in signalled if _REQUEST.search(s)]
        unconf = [u["sentence"] for u in sg["unconfirmed"]]
        req = [s for s in sents if _REQUEST.search(s) and _SOLUTION.search(s)]
        for group in (sg["purchase_step"], unconf, both, signalled, req):
            if group:
                return _clip(group[0])
    if trigger:
        return _clip(trigger)
    return _clip(sents[0]) if sents else None


def classify_post(sig: dict, model_post: dict, terms: set, icp: bool, identity: str = None) -> dict:
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
                "intent_reason": _STATUS_REASONS[status], "uncertainty": [], "analysis_identity": None,
                "analysis_verified": None, "purchase_signals": None, "_exclude": None}

    uncertainty = []
    # The model's entry is used only when it is provably about THIS post
    # (`identity`, see analysis_identity). Otherwise none of its labels
    # apply: the row is labelled from the post's own text and cannot qualify.
    matched = identity is not None
    if not matched:
        model_post = {}
        uncertainty.append("Flintel could not confirm that the analysis for this row was written about this post "
                           "(no matching title and post number, and no quotation unique to it), so its summary and "
                           "labels were not used; the labels shown come from the post's text alone.")
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

    sg = purchase_signals(title, text)
    ceiling = _ceiling(sg)
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
        evidence, evidence_source = _pick_evidence(title, text, role, trigger, sg), "excerpt"
    if not evidence:
        evidence_source = None

    if role == "competitor_promo":
        label, reason = "Low", "A vendor promoting their own tool — competitor activity, not buyer demand."
    elif role == "testimonial":
        label, reason = "Low", "A user describing a tool they already use — market proof, not buyer demand."
    elif role != "buyer":
        label, reason = "Low", ("The post asks for a tool, but it was judged general discussion rather than the "
                                "author's own need." if det_role == "buyer" else
                                "General discussion; the author is not asking for a tool or solution.")
    elif fit == "uncertain":
        label, reason = "Uncertain", "Asks for a solution, but its relevance to this request is unconfirmed."
    elif not evidence:
        label, reason = "Uncertain", "No supporting excerpt could be found in the post."
    elif intent == "explicit" and fit == "fits":
        label, reason = "High", ("Asks for a solution in this category and states an explicit purchase step (an approved "
                                 "budget, a committed decision to buy, sign, subscribe or switch, or an active procurement process).")
    elif intent == "explicit":
        label, reason = "Medium", "Ready to buy, but the fit with this request is only partly supported."
    elif intent == "research":
        label, reason = "Medium", (_signal_reason(sg, rated_down=ceiling == "explicit") or
                                   "Asks for recommendations; no budget, timeline or purchase step was recognised in the post.")
    else:
        label, reason = "Low", "No request for a solution is stated."
    if not matched and label in ("High", "Medium"):
        label, reason = "Uncertain", "Labels come from the post's text alone; the analysis for this row could not be verified."

    return {"role": role, "purchase_intent": intent, "product_fit": fit,
            "relevance": "uncertain" if fit == "uncertain" else "relevant",
            "intent": label, "qualified": label == "High", "evidence": evidence, "evidence_source": evidence_source,
            "verification_status": status, "intent_reason": reason, "uncertainty": uncertainty,
            "analysis_identity": identity, "analysis_verified": matched, "_exclude": None,
            "purchase_signals": {"budget": bool(sg["budget"]), "timing": bool(sg["timing"]), "evaluation": bool(sg["evaluation"]),
                                 "willingness": bool(sg["willingness"]), "unconfirmed": bool(sg["unconfirmed"]),
                                 "purchase_step": bool(sg["purchase_step"])}}


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


# "testimonial (Post 6)" / "Post 6 is a testimonial": a role given to a cited post.
_ROLE_WORDS = {"testimonial": {"testimonial"}, "buyer": {"buyer"},
               "vendor": set(NON_DEMAND_ROLES), "competitor": set(NON_DEMAND_ROLES)}
_ROLE_CITE = re.compile(
    r"\b(testimonial|buyer|vendor|competitor)s?\b(?:\s+[\w-]+){0,3}?\s*\((Posts? \d+(?:(?:, | and )\d+)*)\)|"
    r"\b(Posts? \d+(?:(?:, | and )\d+)*)\s+(?:is|are|was|were)\s+(?:an?\s+|the\s+)?(?:[\w-]+\s+){0,2}?"
    r"(testimonial|buyer|vendor|competitor)s?\b", re.I)
# Demand claimed for cited posts ("Posts 5 and 6 show related need").
_DEMAND_CLAIM = re.compile(r"\b(?:shows?|showing|signals?|indicates?)\s+(?:[\w-]+\s+){0,2}?(?:need|demand)\b", re.I)
_STRONG_CLAIM = re.compile(
    r"\b(?:high[- ](?:intent|probability)|strong,?\s+immediate|immediate\s+(?:outreach\s+)?(?:opportunit\w*|leads?|buyers?|targets?)|"
    r"ready[- ]to[- ]buy|qualified\s+(?:leads?|buyers?|prospects?)|"
    r"strong\s+(?:outreach\s+|buying\s+|purchase\s+)?(?:opportunit\w*|leads?|buyers?|prospects?|targets?))", re.I)
_POST_COUNT = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?:(?:relevant|reddit|matching|unique|analy[sz]ed|retrieved|listed|sampled)\s+){0,2}"
    r"(?:posts?|threads?|conversations?|results?)\b(?!\s*\()", re.I)
_WORD_NUM = {w: i for i, w in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
                                         "nine", "ten", "eleven", "twelve"))}


def _negated(text: str, pos: int) -> bool:
    return bool(_NEGATION.search(text[max(0, pos - 30):pos]))


def _clause_contradicts(clause: str, ctx) -> bool:
    rows = ctx["rows"]
    for m in _ROLE_CITE.finditer(clause):
        word = (m.group(1) or m.group(4)).lower()
        refs = [int(n) for n in re.findall(r"\d+", m.group(2) or m.group(3))]
        cited = [rows[i] for i in refs if i in rows]
        if cited and not _negated(clause, m.start()) and not any(r["role"] in _ROLE_WORDS[word] for r in cited):
            return True
    cited = [rows[i] for m in _CITED.finditer(clause) for i in map(int, re.findall(r"\d+", m.group(0))) if i in rows]
    m = _DEMAND_CLAIM.search(clause)
    if m and cited and not _negated(clause, m.start()) and all(r["role"] in NON_DEMAND_ROLES for r in cited):
        return True
    m = _STRONG_CLAIM.search(clause)
    if m and not _negated(clause, m.start()):
        if (cited and not any(r["qualified"] for r in cited)) or (not cited and not ctx["qualified"]):
            return True
    for m in _POST_COUNT.finditer(clause):
        word = m.group(1).lower()
        n = int(word) if word.isdigit() else _WORD_NUM[word]
        if n not in ctx["count_values"]:
            return True
    return False


def _drop_contradicted(text, ctx):
    """Removes a clause that contradicts the checked rows: it gives a cited
    post a role none of the cited rows has ("testimonial (Post 6)" on a vendor
    row), claims demand from vendor rows only, claims strong / high-intent /
    qualified opportunities without a qualified row behind it, or states a
    post count that is none of the server counts."""
    if not isinstance(text, str) or not text.strip():
        return text
    clauses = _CLAUSES.split(text)
    kept = [c for c in clauses if not _clause_contradicts(c, ctx)]
    return text if len(kept) == len(clauses) else _tidy(" ".join(kept))


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
        nums = _numbers(body)
        if nums:
            hay = " ".join(pool[i] for i in cited if i in pool) if cited else " ".join(pool.values())
            if not nums <= _numbers(hay):
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
    out = _drop_unsupported_numbers(_drop_uncited_vague(out), ctx["row_text"], allowed)
    return _drop_contradicted(out, ctx)


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

# The final writer's per-post fields that a verified map record replaces.
_RECORD_REPLACES = ("summary", "evidence", "role", "purchase_intent", "product_fit")


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
        allowed = set(supports) if require_support else None
        main = next((k for k in _FINDING_TEXT if isinstance(f.get(k), str) and f[k].strip()), None)
        for key in ("finding", "title") + _FINDING_TEXT:
            if isinstance(f.get(key), str):
                f[key] = _check_text(f[key], ctx, key, allowed)
        if main and not f[main].strip():
            continue                                           # its claim was not supported by its posts
        cited = {int(n) for key in ("finding", "title") + _FINDING_TEXT if isinstance(f.get(key), str)
                 for m in _CITED.finditer(f[key]) for n in re.findall(r"\d+", m.group(0))}
        if cited & set(supports):
            supports = [i for i in supports if i in cited]     # the count shown is the posts its text cites
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


def enforce_answer_integrity(data: dict, matched_signals: list, aligned: list, query, post_records=None) -> dict:
    """Applies the guardrails to a parsed source_list / comparison answer.
    `aligned` is the exact list whose position i was "[Post i+1]" in the prompt.
    `post_records` ({prompt number: map-step record}) comes only from the server
    (logics._take_post_records); a "_post_records" field in `data` is never used."""
    if isinstance(data, dict):
        data.pop("_post_records", None)
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

    fingerprints = {s["post_url"]: post_fingerprint(s.get("title"), s.get("post_text"))
                    for s in sigs if s.get("post_url")}
    post_norms = {u: fp[0] for u, fp in fingerprints.items()}
    post_records = post_records if isinstance(post_records, dict) else {}
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
                position = global_pos.get(sig["post_url"])
                # The map-step record of THIS stored post (looked up by its own prompt
                # number, never by the number the final writer gave), re-checked here.
                record = check_post_record(post_records.get(position), sig.get("title"), sig.get("post_text"),
                                           [fp for u, fp in fingerprints.items() if u != sig["post_url"]]) \
                    if position in post_records else None
                if record:
                    post = {k: v for k, v in post.items() if k not in _RECORD_REPLACES}
                    post.update(summary=record["summary"], evidence=record["quote"],
                                **{k: record[k] for k in _RECORD_LABELS if k in record})
                    identity = "map_record"
                else:
                    identity = analysis_identity(sig, post, position,
                                                 [t for u, t in post_norms.items() if u != sig["post_url"]])
                labels = classify_post(sig, post, terms, icp, identity)
                if labels.pop("_exclude"):
                    excluded["irrelevant"] += 1
                    excluded_posts.append({"link": sig["post_url"], "reason": "not relevant"})
                    continue
                new = dict(post, **labels)
                for key in ("_model_title", "_model_index"):
                    new.pop(key, None)
                summary = post.get("summary")
                if new["verification_status"] == "verified" and isinstance(summary, str) and summary.strip() and (
                        not new["analysis_verified"]
                        or (_summary_claims(summary, sig)
                            and not summary_supported(summary, sig.get("title"), sig.get("post_text")))):
                    new["summary"] = SUMMARY_UNAVAILABLE           # unverified analysis, or not supported by this post
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
        "categories": {k: sum(1 for p in rows if _row_category(p) == k) for k in CATEGORY_ORDER},
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
                        for p in rows if p["verification_status"] == "verified"},
           "rows": {p["index"]: {"role": p["role"], "qualified": p["qualified"]} for p in rows},
           "qualified": counts["qualified"],
           "count_values": ({counts[k] for k in ("shown", "analyzed", "retrieved", "unique", "relevant", "verified",
                                                 "qualified", "excluded")} | set(counts["categories"].values()))}
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


CATEGORY_ORDER = ("qualified", "researching", "unconfirmed", "discussion", "competitor", "unavailable")
_CATEGORY_WORDS = (("researching", "buyer still researching", "buyers still researching"),
                   ("unconfirmed", "unconfirmed post", "unconfirmed posts"),
                   ("discussion", "general discussion post", "general discussion posts"),
                   ("competitor", "competitor or vendor post", "competitor or vendor posts"),
                   ("unavailable", "post with an unavailable source", "posts with an unavailable source"))
QUALIFIED_DEFINITION = ("A qualified lead asks for a solution in this category and states an explicit purchase step (an "
                        "approved budget, a committed decision to buy, sign, subscribe or switch, or an active procurement "
                        "process). Budget, timing, trials, demos and willingness to pay are shown as signals but do not qualify "
                        "a lead on their own; no other row is counted as a lead.")


def _row_category(p: dict) -> str:
    """Exactly one category per shown row, so the categories add up to the table."""
    if p["verification_status"] != "verified":
        return "unavailable"
    if p["role"] in NON_DEMAND_ROLES:
        return "competitor"
    if p["qualified"]:
        return "qualified"
    if p["intent"] == "Uncertain":
        return "unconfirmed"
    if p["role"] == "buyer" and p["intent"] == "Medium":
        return "researching"
    return "discussion"


def verified_summary(counts: dict) -> str:
    """One server-written sentence stating the checked counts behind the report:
    one category per shown row, the excluded posts out of those the analysis
    listed, and what "qualified" means."""
    cats = counts.get("categories") or {}

    def n(v, one, many):
        return f"{v} {one if v == 1 else many}"
    parts = [n(cats.get("qualified", 0), "qualified lead", "qualified leads")]
    parts += [n(cats[k], one, many) for k, one, many in _CATEGORY_WORDS if cats.get(k)]
    text = "Checked against the stored posts: " + ", ".join(parts)
    if counts.get("excluded"):
        text += f"; {counts['excluded']} of {n(counts.get('listed', 0), 'listed post', 'listed posts')} excluded"
    return f"{text}. {QUALIFIED_DEFINITION}"


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
