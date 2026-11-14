"""
report_claims.py — free-text claim checks for a report (pure; no I/O, no application imports).

The model writes an introduction, a summary and findings. Three kinds of sentence cannot be trusted just because the table beside them
is checked, and each is verified here against the source text of the rows that are actually displayed:

1. NAMES. A company / product name that appears in no displayed row (title, body, link, author) is not evidence of anything in this
   report ("SourceWhale and Loxo recruiters ..." when neither is in the source table). The sentence is removed.
2. REQUESTS. "requested a demo / trial / quote / pricing / proposal" must be backed by a request sentence in a cited (or, if none
   is cited, a displayed) post. A post that merely mentions a demo, or a vendor that offers one, does not support it.
3. PARTNERSHIPS. "partnership / integration opportunity / reseller / affiliate" must be backed by partnership language in a post.

It can only remove a sentence, never rewrite or add one. A sentence is removed whole; an empty result means every sentence failed.
"""

import re

_I = re.IGNORECASE
_SPLIT = re.compile(r"(?<=[.!?])\s+")
_CITE = re.compile(r"\b[Pp]osts?\s+#?(\d+(?:\s*(?:,|and|&|-|to)\s*\d+)*)")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+&'-]*")

_REQ_VERB = r"(?:request(?:s|ed|ing)?|ask(?:s|ed|ing)?\s+for|want(?:s|ed)?|seek(?:s|ing)?|need(?:s|ed)?|look(?:s|ing)?\s+for|inquir\w+\s+about|schedul\w+|sign(?:s|ed|ing)?\s+up\s+for)"
_REQ_TERMS = {
    "demo": r"\bdemos?\b|\bdemonstration",
    "trial": r"\b(?:free\s+)?trials?\b",
    "quote": r"\bquot(?:e|es|ation|ations)\b|\bestimates?\b",
    "pricing": r"\bpric(?:e|es|ing)\b",
    "proposal": r"\bproposals?\b|\brfps?\b",
}
_REQ_CLAIM = re.compile(r"\b" + _REQ_VERB + r"\b[^.;:]{0,50}?\b(demos?|demonstrations?|(?:free\s+)?trials?|quot(?:e|es|ation|ations)|pricing|proposals?|rfps?)\b", _I)
_PARTNER_CLAIM = re.compile(r"\b(?:partner(?:ship|ships|ing|s)?|integration\s+(?:opportunit\w+|partner\w*)|collaborat\w+|reseller|affiliate|co-?marketing)\b", _I)
_PARTNER_SRC = re.compile(r"\b(?:partner(?:ship|ships|ing|s)?|integrat\w+|collaborat\w+|resell\w*|affiliate\w*|white[- ]?label|co-?market\w*)\b", _I)
_ASK_CUE = re.compile(r"\b(?:looking|need|needs|needed|want|wanted|wants|require[sd]?|seeking|can\s+you|could\s+you|anyone|anybody|would\s+like|interested|reach\s+out\s+to\s+me)\b", _I)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
_NEG = re.compile(r"\b(?:no|not|never|none|neither|nor|without|cannot)\b|n't\b", _I)

# Capitalised words that are not names of companies or products.
_COMMON = frozenset("""
a an the this that these those it its in on at by for from of to and or but if as is are was were be been i we you he she they our your their his her
post posts source sources row rows table report summary finding findings key evidence signal signals buyer buyers lead leads prospect prospects
reddit hacker news hn twitter x linkedin youtube github stackoverflow quora facebook instagram tiktok discord slack medium
january february march april may june july august september october november december monday tuesday wednesday thursday friday saturday sunday
high low none unknown uncertain explicit research verified unverified qualified relevant partial strong moderate weak current historical
flintel gpt ai api crm saas b2b b2c erp roi kpi sdk ui ux seo sms usa uk eu us ceo cto cfo vp hr it
""".split())


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _corpus(rows_text) -> str:
    return " " + _norm(" ".join(rows_text)) + " "


def _name_tokens(sentence: str):
    """Candidate names in a sentence: CamelCase words (SourceWhale), or a capitalised word that is not the sentence's first word."""
    words = list(_WORD.finditer(sentence))
    for i, m in enumerate(words):
        w = m.group(0)
        if len(w) < 3 or w.lower() in _COMMON:
            continue
        camel = bool(re.search(r"[a-z][A-Z]", w))
        mid_cap = i > 0 and w[0].isupper() and not w.isupper()
        if camel or mid_cap:
            yield w


_STARTERS = frozenset("""several multiple many some most both each another other either neither various numerous every any all few one two three four five
six seven eight nine ten users people founders customers businesses companies teams buyers leads prospects authors posters commenters
overall however also additionally meanwhile finally first second third next later earlier recently currently generally""".split())


def _leading_name(sentence: str) -> bool:
    """A sentence that opens with a single capitalised word that is not an ordinary English opener or a plural noun ("Zoomly requested ...")."""
    m = _WORD.match(sentence.lstrip())
    if not m:
        return False
    w = m.group(0)
    return len(w) >= 4 and w[0].isupper() and w.lower() not in _COMMON and w.lower() not in _STARTERS and not w.lower().endswith("s")


def unsupported_names(sentence: str, corpus: str, extra_known=()):
    """Names in `sentence` that occur in no displayed row (`corpus` from `_corpus`) and are not otherwise known (the query, the platform)."""
    known = {_norm(k) for k in extra_known}
    out = []
    for w in _name_tokens(sentence):
        n = _norm(w)
        if not n or n in known or f" {n} " in corpus or any(n in k.split() for k in known):
            continue
        out.append(w)
    return out


def _dangling_citation(sentence: str, row_text: dict) -> bool:
    """The sentence cites a table row (a small "Post N") that does not exist. Years and "Washington Post 2024" are not citations."""
    for m in _CITE.finditer(sentence):
        before = sentence[:m.start()].rstrip().split()
        prev = before[-1] if before else ""
        if prev[:1].isupper() and prev[-1:].isalpha():
            continue
        if any(1 <= int(n) <= 99 and int(n) not in row_text for n in re.findall(r"\d+", m.group(1))):
            return True
    return False


def _cited_indexes(sentence: str):
    got = set()
    for m in _CITE.finditer(sentence):
        got.update(int(x) for x in re.findall(r"\d+", m.group(1)))
    return got


def _request_supported(term_key: str, source_text: str) -> bool:
    """The post itself contains a request sentence that names the term (an author asking, not a vendor offering)."""
    pat = re.compile(_REQ_TERMS[term_key], _I)
    for sent in _SENT.split(source_text or ""):
        if pat.search(sent) and _ASK_CUE.search(sent):
            return True
    return False


def _term_key(word: str) -> str:
    w = word.lower()
    return ("demo" if "demo" in w else "trial" if "trial" in w else "quote" if w.startswith("quot") else
            "pricing" if "pric" in w else "proposal")


def drop_unsupported_claims(text: str, row_text: dict, query: str = "", allowed=None, extra_known=()) -> str:
    """`text` without any sentence that (1) names a company/product absent from the displayed rows, (2) claims a request for a
    demo / trial / quote / pricing / proposal no cited (or displayed) post makes, or (3) claims a partnership no post expresses.
    `row_text`: {row index: that row's source text (title + body + link + author)}; `allowed`: the indexes a finding may cite."""
    if not isinstance(text, str) or not text.strip() or not row_text:
        return text
    corpus = _corpus(list(row_text.values()))
    known = list(extra_known) + [query or ""]
    kept = []
    for sent in _SPLIT.split(text.strip()):
        cited = {i for i in _cited_indexes(sent) if i in row_text and (allowed is None or i in allowed)}
        scope = [row_text[i] for i in sorted(cited)] or list(row_text.values())
        bad = _dangling_citation(sent, row_text)
        if not bad and unsupported_names(sent, corpus, known):
            bad = True
        negated = bool(_NEG.search(sent))                                      # "no buyer asked for a demo" claims no request
        attributed = bool(_cited_indexes(sent)) or (_leading_name(sent) or any(True for _ in _name_tokens(sent)))   # about a post or a named party, not an aggregate
        if not bad and attributed and not negated:
            for m in _REQ_CLAIM.finditer(sent):
                key = _term_key(m.group(1))
                if not any(_request_supported(key, t) for t in scope):
                    bad = True
                    break
        if not bad and attributed and not negated and _PARTNER_CLAIM.search(sent) and not any(_PARTNER_SRC.search(t or "") for t in scope):
            bad = True
        if not bad:
            kept.append(sent)
    return " ".join(kept)
