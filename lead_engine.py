"""
lead_engine.py — the Flintel lead-discovery core (pure: no database, no network, no model).

  business profile -> search concepts -> (connectors return posts) -> normalise -> de-duplicate -> qualify against the profile
  -> evidence check -> leads + an honest coverage / reconciliation record.

It REUSES the existing, tested pieces: `platforms.normalize_signal` (stable source ids, URL-host platform truth, validated publication time),
`signal_intel.classify_post` (rule-based intent with a quote that must be a substring of the post) and `opportunity_layer`'s age rule. The
dimensions are kept apart and never merged into one score:

  topic / business fit   do the user's offering, audience and problem terms appear in the post
  problem fit            does the post describe a problem the user's offer solves
  intent category        what the AUTHOR is doing (see INTENTS)
  recency                recent | historical | undated | invalid | future
  geography              match | unknown (only checked for a city / region target)
  evidence               an exact quote, copied from the post, plus its source URL

LABELS (transparent rules, not a probability of conversion):
  High potential   a demand intent (explicit request / recommendation request / provider search / buying statement), strong fit, recent,
                   and the location is not in doubt
  Relevant         a demand intent with at least partial fit and recent; or a strong problem signal / solution research with strong fit, recent
  Needs review     anything plausible that cannot be called current: historical, undated or invalid date, location unconfirmed for a local
                   business, or a weaker signal
  Not a lead       vendor promotion, general discussion, unrelated, excluded term, or no exact evidence
A post dated in the future is EXCLUDED (never a current lead). A record with no URL or no exact quote is never shown as a lead.
"""
import concurrent.futures
import re
import time
from datetime import datetime, timedelta, timezone

import lead_intent
import opportunity_layer
import platforms
import signal_intel

HISTORICAL_DAYS = opportunity_layer.HISTORICAL_DAYS
FUTURE_TOLERANCE = timedelta(minutes=10)
LABELS = ("High potential", "Relevant", "Needs review", "Not a lead")
INTENTS = ("DIRECT_BUYING_INTENT", "SERVICE_REQUEST", "RECOMMENDATION_REQUEST", "PROVIDER_SEARCH", "STRONG_PROBLEM_SIGNAL",
           "SOLUTION_RESEARCH", "GENERAL_DISCUSSION", "VENDOR_PROMOTION", "IRRELEVANT")
_DEMAND = frozenset({"DIRECT_BUYING_INTENT", "SERVICE_REQUEST", "RECOMMENDATION_REQUEST", "PROVIDER_SEARCH"})
_SOFT = frozenset({"STRONG_PROBLEM_SIGNAL", "SOLUTION_RESEARCH"})
_NOT_LEAD = frozenset({"GENERAL_DISCUSSION", "VENDOR_PROMOTION", "IRRELEVANT"})
_RULE_TO_INTENT = {"budget": "DIRECT_BUYING_INTENT", "would_pay": "DIRECT_BUYING_INTENT", "looking_to": "DIRECT_BUYING_INTENT",
                   "need_provider": "PROVIDER_SEARCH", "who_can": "PROVIDER_SEARCH",
                   "recommend_req": "RECOMMENDATION_REQUEST", "recommend": "RECOMMENDATION_REQUEST", "recommendations": "RECOMMENDATION_REQUEST",
                   "anyone_know": "RECOMMENDATION_REQUEST", "which_tool": "RECOMMENDATION_REQUEST", "is_there_tool": "RECOMMENDATION_REQUEST",
                   "alternatives": "RECOMMENDATION_REQUEST"}
_COMPARISON_RULES = frozenset({"versus", "compared", "switching"})
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="lead-source")
_STATE_OF_STATUS = {"timed_out": "failed", "ok": "searched", "empty": "searched", "partial": "partial", "failed": "failed", "error": "failed",
                    "rate_limited": "unavailable", "unavailable": "unavailable", "skipped": "not_searched", "not_requested": "not_searched",
                    "not_searched": "not_searched"}
_WORD = re.compile(r"[a-z][a-z0-9+&'-]{2,}")


# ── search concepts (deterministic; one engine for every business type) ──────────────────────────────────────────────────
_LEAD_IN = re.compile(r"^\s*(?:we|i)(?:'m|'re| am| are)?\s+(?:(?:also|mainly|primarily|currently)\s+)?(?:sell|selling|provide|providing|offer|offering|design(?:\s+and\s+develop)?|"
                      r"build|develop|create|make|run|do|deliver|specialize\s+in|specialise\s+in|help\s+\w+\s+with)\s+", re.I)


def offer_phrase(user: dict, max_words: int = 7) -> str:
    """The user's own offering phrase, lightly trimmed ("We provide X to Y" -> "X to Y"), at most `max_words` words. Not rewritten."""
    text = _LEAD_IN.sub("", str(user.get("sells") or "").strip().rstrip("."))
    text = re.sub(r"^(?:a|an|the|some)\s+", "", text, flags=re.I)
    return " ".join(text.split()[:max_words])


def search_concepts(profile: dict, limit: int = 12) -> list:
    """Business-specific search concepts of the eight kinds the product needs. Built only from the user's own words and the derived terms;
    no model is called. Each: {"id", "type", "query", "keywords"}. The query is what a buyer might write, not a keyword dump."""
    user = (profile or {}).get("user") or {}
    d = (profile or {}).get("derived") or {}
    offer = offer_phrase(user) or " ".join(d.get("offering_terms", [])[:3])
    audience = (str(user.get("ideal_customers") or "").split(",")[0].strip()[:50])
    problem = " ".join(str(user.get("problems_solved") or "").split(",")[0].split()[:6])
    geo = user.get("geography") or {}
    place = (geo.get("places") or [""])[0] if geo.get("scope") in ("city", "region", "country") else ""
    near = f" in {place}" if place else ""
    out = []

    def add(kind, query, keywords=None):
        q = re.sub(r"\s+", " ", query).strip()
        if q and q.lower() not in {c["query"].lower() for c in out}:
            out.append({"id": f"c{len(out) + 1}", "type": kind, "query": q, "keywords": keywords or [w for w in _WORD.findall(q.lower())][:6]})

    if offer:
        add("direct_request", f"looking for {offer}{near}")
        add("direct_request", f"need {offer}{near}")
        add("recommendation_request", f"recommend {offer}{near}")
        add("recommendation_request", f"best {offer}{near} recommendations")
        add("provider_search", f"hire {offer}{near}")
        add("provider_search", f"who can build {offer}{near}")
        add("hiring_request", f"looking to hire someone for {offer}{near}")
    if problem:
        add("problem_description", f"struggling with {problem}")
        add("problem_description", f"how do I fix {problem}")
    if offer and audience and audience.lower() not in offer.lower():
        add("industry_signal", f"{offer} for {audience}")
        add("buying_question", f"{audience} looking for {offer}")
    for c in (d.get("competitor_terms") or [])[:3]:
        add("alternative_request", f"alternatives to {c}", [c, "alternative"])
        add("comparison_request", f"{c} vs", [c, "vs"])
    return out[:limit]


# ── normalisation / identity / de-duplication ────────────────────────────────────────────────────────────────────────────
def _date_fields(rec: dict):
    return [rec.get(k) for k in ("created_utc", "published_at", "created_at", "date", "timestamp") if rec.get(k) not in (None, "")]


def _raw_future(raw: dict, now: datetime) -> bool:
    """True when the record's OWN date field is a readable time later than now (the validated `published_at` drops such a date)."""
    for v in _date_fields(raw if isinstance(raw, dict) else {}):
        try:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                dt = datetime.fromtimestamp(float(v), timezone.utc)
            elif isinstance(v, datetime):
                dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        if dt > now + FUTURE_TOLERANCE:
            return True
    return False


def normalize_post(raw: dict, retrieved_at: datetime, retrieval_path: str = None) -> dict:
    """One shared record shape for every connector. Never invents a field: a missing author stays None, a missing date stays None, and a
    record without a URL has no source_id and is refused downstream."""
    n = platforms.normalize_signal(raw if isinstance(raw, dict) else {}, now=retrieved_at)
    title = str(n.get("title") or "").strip()
    text = str(n.get("post_text") or n.get("text") or n.get("body") or "").strip()
    author = str(n.get("author") or "").strip()
    if author.lower() in ("", "[deleted]", "deleted", "unknown", "none", "null", "anonymous", "n/a"):
        author = None
    return {"platform": n.get("platform"), "source_id": n.get("source_id") or "", "url": n.get("url") or "", "author": author,
            "title": title, "text": text, "published_at": n.get("published_at"), "had_date_field": bool(_date_fields(raw if isinstance(raw, dict) else {})),
            "raw_future": _raw_future(raw, retrieved_at),
            "community": n.get("community"), "retrieved_at": retrieved_at.isoformat(), "retrieval_path": retrieval_path,
            "removed": bool(platforms.is_removed(n))}


def dedupe(records: list):
    """(unique records, duplicate count). Identity is the platform-specific source_id; the same text on two platforms stays two records and
    no author is merged across platforms. A record without a source_id cannot be identified and is kept apart (it is refused later)."""
    seen, out, dups = set(), [], 0
    for r in records:
        sid = r.get("source_id")
        if sid and sid in seen:
            dups += 1
            continue
        if sid:
            seen.add(sid)
        out.append(r)
    return out, dups


# ── qualification ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _stem(w):
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def _hits(text_words: set, terms) -> list:
    return sorted({_stem(t) for t in terms if _stem(t) in text_words})


def recency_of(rec: dict, now: datetime) -> str:
    when = rec.get("published_at")
    if not when:
        return "future" if rec.get("raw_future") else ("invalid" if rec.get("had_date_field") else "undated")
    try:
        dt = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return "invalid"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt > now + FUTURE_TOLERANCE:
        return "future"
    return "historical" if (now - dt) > timedelta(days=HISTORICAL_DAYS) else "recent"


def _geo(rec: dict, profile: dict):
    geo = ((profile or {}).get("user") or {}).get("geography") or {}
    if geo.get("scope") not in ("city", "region") or not geo.get("places"):
        return "not_required", None
    hay = " ".join(str(x or "") for x in (rec.get("title"), rec.get("text"), rec.get("community"), rec.get("url"))).lower()
    for place in geo["places"]:
        if place.lower() in hay:
            return "match", place
    return "unknown", None


def _intent(cls_rec: dict) -> str:
    c, rule = cls_rec.get("class"), cls_rec.get("rule")
    if c == signal_intel.DIRECT_DEMAND:
        return _RULE_TO_INTENT.get(rule, "SERVICE_REQUEST")
    if c == signal_intel.PROBLEM_SIGNAL:
        lim = " ".join(cls_rec.get("limitations") or [])
        return "STRONG_PROBLEM_SIGNAL" if ("No request for help" not in lim and "No first-person" not in lim) else "GENERAL_DISCUSSION"
    if c == signal_intel.COMPETITOR_SIGNAL:
        return "SOLUTION_RESEARCH" if rule in _COMPARISON_RULES else ("GENERAL_DISCUSSION" if rule == "switched" else "VENDOR_PROMOTION")
    return "GENERAL_DISCUSSION"


_GUARD_INTENT = {"vendor": "VENDOR_PROMOTION", "definition": "GENERAL_DISCUSSION", "tutorial": "GENERAL_DISCUSSION", "job_seeker": "GENERAL_DISCUSSION",
                 "validation": "GENERAL_DISCUSSION"}
_GUARD_REASON = {"vendor": "The author is promoting their own product or service, which is not a buyer.",
                 "definition": "The post asks for a definition or explanation, not for a provider or product.",
                 "tutorial": "The post teaches or shares how the author did something; it is not a request.",
                 "job_seeker": "The author is looking for work, not for a provider.",
                 "validation": "The author is testing their own idea, not asking to buy."}


def qualify(rec: dict, profile: dict, now: datetime, sig: dict = None) -> dict:
    """The lead record for ONE normalised post: the dimensions, the label, the exact evidence and the reasons. Never raises.
    Two independent signals decide (see lead_intent): the author's need FRAME and the alignment with THIS business's profile."""
    user = (profile or {}).get("user") or {}
    sig = sig or lead_intent.signature(profile)
    full = (rec["title"] + "\n" + rec["text"]).strip() if rec["text"] and rec["text"] != rec["title"] else rec["title"]
    a = lead_intent.analyze(rec["title"], rec["text"], profile, sig)
    fit_d = a["fit"]
    fit, problem_fit = fit_d["business_fit"], fit_d["problem_fit"]
    c = signal_intel.classify_post({"title": rec["title"], "post_text": rec["text"], "post_url": rec["url"], "author": rec["author"],
                                    "platform": rec["platform"], "published_at": rec["published_at"]}, query=str(user.get("sells") or ""))
    intent_c = _intent(c)
    quote, how = None, "classifier"
    if a["excluded_term"]:
        intent = "IRRELEVANT"
    elif a["guard"]:
        intent = _GUARD_INTENT[a["guard"]]
    elif intent_c == "VENDOR_PROMOTION" and not a["frames"]:
        intent = intent_c
    elif a["intent"] and a["quote"]:
        intent, quote, how = a["intent"], a["quote"], "frame:" + a["frame"]
        if intent_c in _DEMAND and intent not in _DEMAND and a["frame"] in ("PROBLEM", "WISH") and c.get("quote") and c["quote"] in full:
            intent, quote, how = intent_c, c["quote"], "classifier"                 # an explicit request outranks a problem frame in the same post
    else:
        intent = intent_c
        quote = c.get("quote") if (c.get("quote") and c["quote"] in full) else None
    if quote and quote not in full:
        quote = None
    recency = recency_of(rec, now)
    geo, place = _geo(rec, profile)
    if a["other_place"] and geo != "match":
        geo, place = "mismatch", a["other_place"]
    reasons = []
    if a["excluded_term"]:
        reasons.append(f"The post contains your exclusion term “{a['excluded_term']}”.")
    if a["guard"] and not a["excluded_term"]:
        reasons.append(_GUARD_REASON[a["guard"]])
    if fit == "none" and intent in _DEMAND | _SOFT:
        reasons.append("The post does not mention your offer, your customers or the problems you solve.")
    label = "Not a lead"
    if not rec["url"] or not rec["source_id"]:
        reasons.append("The post has no usable source link.")
    elif not quote:
        reasons.append("No exact statement of need could be quoted from the post.")
    elif recency == "future":
        reasons.append("The post is dated in the future, so it cannot be a current lead.")
    elif geo == "mismatch":
        reasons.append(f"The post is about a different place ({place}), and your business serves a specific area.")
    elif intent in _NOT_LEAD or fit == "none":
        if intent == "GENERAL_DISCUSSION" and not a["guard"]:
            reasons.append("The post shows no request or buying need.")
        elif intent == "VENDOR_PROMOTION" and not a["guard"]:
            reasons.append(_GUARD_REASON["vendor"])
        elif intent == "IRRELEVANT" and not a["excluded_term"]:
            reasons.append("The post is not about your offer.")
        elif fit == "none" and not reasons:
            reasons.append("The post is not related to your offer.")
    else:
        current = recency == "recent"
        geo_ok = geo != "unknown"
        if intent in _DEMAND:
            label = "High potential" if (current and fit == "strong" and geo_ok) else "Needs review"
        else:
            label = "Relevant" if (current and fit == "strong" and geo_ok) else "Needs review"
        if fit == "partial":
            reasons.append("Only part of what the author describes matches your offer, so it needs a human look.")
        if recency == "historical":
            reasons.append(f"The post is older than {HISTORICAL_DAYS} days: it is historical research, not a current lead.")
        elif recency == "undated":
            reasons.append("The post has no verified publication date.")
        elif recency == "invalid":
            reasons.append("The post's date could not be validated.")
        if geo == "unknown":
            reasons.append("Your business serves a specific place and the post does not mention it, so the location is not confirmed.")
        if intent in _SOFT:
            reasons.append("The author describes a problem or compares options without stating a request to buy or hire.")
        if a.get("replace"):
            reasons.append("The author is thinking of replacing a provider or product they already use.")
    return {"source_id": rec["source_id"], "platform": rec["platform"], "url": rec["url"], "author": rec["author"], "title": rec["title"],
            "text": rec["text"], "published_at": rec["published_at"], "retrieved_at": rec["retrieved_at"], "community": rec["community"],
            "label": label, "intent": intent, "recency": recency, "is_current": bool(label != "Not a lead" and recency == "recent"),
            "fit": {"business_fit": fit, "problem_fit": problem_fit, "offer_terms_found": fit_d["terms"], "audience_terms_found": fit_d["audience_terms"],
                    "problem_terms_found": fit_d["problem_terms"]},
            "geography": {"status": geo, "place": place}, "evidence": {"quote": quote, "kind": "exact_quote" if quote else None,
                                                                           "text_source": c.get("text_source"), "found_by": how},
            "explanation": _explain(intent, a, c), "reasons": reasons, "limitations": c.get("limitations") or [],
            "classifier": c.get("classifier")}


_FRAME_TEXT = {"REQUEST": "The author asks for a provider, product or service in their own words.",
               "RECOMMEND": "The author asks other people to recommend a provider, product or service.",
               "PRICE": "The author asks what this kind of provider or product costs, which means they are weighing a purchase.",
               "COMPARE": "The author is comparing providers, products or options.",
               "REPLACE": "The author wants to replace a provider or product they currently use.",
               "PROBLEM": "The author describes a problem that your business solves.",
               "WISH": "The author expresses a need they cannot meet themselves."}


def _explain(intent, a, c):
    if a.get("frame") and a.get("quote") and not a.get("guard"):
        return _FRAME_TEXT.get(a["frame"], "")
    return c.get("intent_explanation") or ""


_LABEL_RANK = {"High potential": 0, "Relevant": 1, "Needs review": 2, "Not a lead": 3}
_RECENCY_RANK = {"recent": 0, "historical": 1, "undated": 2, "invalid": 3, "future": 4}


def sort_key(lead: dict):
    """Ordering only (not a probability): label, then explicit demand before softer signals, then recency class; newest first inside a rank."""
    return (_LABEL_RANK[lead["label"]], 0 if lead["intent"] in _DEMAND else 1, _RECENCY_RANK.get(lead["recency"], 9))


def outreach_opener(lead: dict, profile: dict) -> str:
    """A short, editable opener built ONLY from the post and the user's own description. It never claims familiarity, results or features."""
    user = (profile or {}).get("user") or {}
    name = user.get("business_name") or "our team"
    where = {"reddit": f"on Reddit{(' (r/' + lead['community'] + ')') if lead.get('community') else ''}", "hackernews": "on Hacker News"}.get(lead.get("platform"), "online")
    topic = (lead.get("title") or "").strip().rstrip(".?!")[:110]
    sells = str(user.get("sells") or "").strip().rstrip(".")[:140]
    return (f"Hi, I came across your post {where}: “{topic}”. I'm with {name}. {sells[:1].upper() + sells[1:]}. "
            "If that is relevant to what you're working on, I'm happy to share how we would approach it. No pressure either way.")


# ── scan orchestration ───────────────────────────────────────────────────────────────────────────────────────────────────
def coverage_of(results: list) -> dict:
    """results: [{"key","label","status","detail","count"}]. Six states, the same vocabulary as the existing coverage panel. `complete` only
    when EVERY selected source was searched; unselected sources never appear here."""
    sources, reasons = [], []
    for r in results:
        state = _STATE_OF_STATUS.get(r.get("status"), "unconfirmed")
        sources.append({"key": r["key"], "label": r["label"], "state": state, "detail": r.get("detail") or "", "retrieved": int(r.get("count") or 0)})
        if state != "searched":
            reasons.append(f"{r['label']} {state.replace('_', ' ')}")
    if not sources:
        status = "unavailable"
        reasons.append("no source was selected")
    elif all(s["state"] == "searched" for s in sources):
        status = "complete"
    elif any(s["state"] in ("searched", "partial") for s in sources):
        status = "partial"
    else:
        status = "unavailable"
    return {"status": status, "sources": sources, "reasons": reasons}


def _call_connector(c, concepts, since_days, limit):
    try:
        out = c.fetch(concepts, since_days, limit) or {}
        return list(out.get("records") or []), out.get("status") or "unconfirmed", out.get("detail") or ""
    except Exception as exc:
        return [], "failed", type(exc).__name__


def run_scan(profile: dict, connectors: list, now: datetime = None, since_days: int = 30, limit: int = 100, concepts: list = None,
             source_deadline: float = 30.0) -> dict:
    """connectors: objects with `.key`, `.label` and `.fetch(concepts, since_days, limit) -> {"status", "records", "detail"}` (never raises is
    NOT assumed: an exception becomes a failed source). Returns the scan record: concepts, leads (all labels but 'Not a lead'), excluded with
    reasons, coverage and reconciliation, plus stage timings. Nothing here calls a model or the network by itself. Every source runs in a worker
    thread with ONE shared hard deadline (`source_deadline` seconds): a source that has not answered by then is reported as failed ("timed out") and
    its late result is ignored, so one slow source can never hold the scan, and a timed-out source never counts as searched."""
    now = now or datetime.now(timezone.utc)
    timings = {}
    t0 = time.perf_counter()
    concepts = concepts if concepts is not None else search_concepts(profile)
    timings["concepts_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    results, normalized = [], []
    t1 = time.perf_counter()
    futures = [(c, _POOL.submit(_call_connector, c, concepts, since_days, limit)) for c in connectors]
    done, _pending = concurrent.futures.wait([f for _, f in futures], timeout=source_deadline)       # all sources run at once, one shared deadline
    for c, fut in futures:
        if fut in done:
            raws, status, detail = fut.result()
        else:
            fut.cancel()                                                                              # a call already running cannot be killed; it is abandoned
            raws, status, detail = [], "timed_out", f"The search did not finish within {int(source_deadline)} seconds."
        for raw in raws:
            normalized.append(normalize_post(raw, now, getattr(c, "retrieval_path", None)))
        results.append({"key": c.key, "label": c.label, "status": status, "detail": detail, "count": len(raws)})
    timings["retrieval_ms"] = round((time.perf_counter() - t1) * 1000, 2)
    t2 = time.perf_counter()
    unique, dups = dedupe(normalized)
    timings["dedupe_ms"] = round((time.perf_counter() - t2) * 1000, 2)
    t3 = time.perf_counter()
    leads, excluded = [], []
    sig = lead_intent.signature(profile)
    for rec in unique:
        if rec["removed"]:
            excluded.append({"source_id": rec["source_id"], "url": rec["url"], "title": rec["title"], "platform": rec["platform"], "reason": "removed or deleted post"})
            continue
        q = qualify(rec, profile, now, sig)
        if q["recency"] == "future" or q["label"] == "Not a lead":
            excluded.append({"source_id": q["source_id"], "url": q["url"], "title": q["title"], "platform": q["platform"],
                             "reason": (q["reasons"] or ["not a lead"])[0], "intent": q["intent"]})
        else:
            q["suggested_opener"] = outreach_opener(q, profile)
            leads.append(q)
    leads.sort(key=lambda x: x["published_at"] or "", reverse=True)       # newest first within a rank (the next sort is stable)
    leads.sort(key=sort_key)
    timings["qualify_ms"] = round((time.perf_counter() - t3) * 1000, 2)
    cov = coverage_of(results)
    timings["total_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    counts = {"retrieved": len(normalized), "duplicates": dups, "evaluated": len(unique), "displayed": len(leads), "excluded": len(excluded),
              "high_potential": sum(1 for x in leads if x["label"] == "High potential"), "current_leads": sum(1 for x in leads if x["is_current"]),
              "historical_or_undated": sum(1 for x in leads if x["recency"] != "recent")}
    counts["reconciled"] = bool(counts["retrieved"] == counts["duplicates"] + counts["evaluated"] and counts["evaluated"] == counts["displayed"] + counts["excluded"])
    return {"created_at": now.isoformat(), "since_days": since_days, "concepts": concepts, "coverage": cov, "counts": counts, "leads": leads,
            "excluded": excluded, "timings": timings, "statement": scan_statement(counts, cov)}


def refresh_lead(lead: dict, now: datetime = None) -> dict:
    """A STORED lead re-aged at display time. Recency is computed when a scan runs, so a lead saved as "recent" would otherwise look current for ever.
    Only a lead with a verified publication date is re-aged (an undated or invalid one stays as stored). A High potential / Relevant lead that is no
    longer recent is shown as Needs review, with the reason first; the original label and recency are kept in `label_at_scan` / `recency_at_scan`."""
    out = dict(lead)
    if not lead.get("published_at") or lead.get("label") == "Not a lead":
        return out
    now = now or datetime.now(timezone.utc)
    new = recency_of({"published_at": lead["published_at"], "had_date_field": True}, now)
    if new == lead.get("recency"):
        return out
    out["recency_at_scan"], out["recency"] = lead.get("recency"), new
    out["is_current"] = bool(new == "recent")
    if new != "recent" and lead.get("label") in ("High potential", "Relevant"):
        out["label_at_scan"], out["label"] = lead["label"], "Needs review"
        out["reasons"] = [f"The post is now older than {HISTORICAL_DAYS} days: it was rated {lead['label']} when it was found, and is now historical research, not a current lead."] + list(lead.get("reasons") or [])
    return out


def scan_statement(counts: dict, cov: dict) -> str:
    """The one sentence about the outcome. Zero leads is reported as zero ONLY for a complete scan; otherwise it says the search was incomplete."""
    if counts["displayed"] > 0:
        base = f"{counts['displayed']} possible lead{'s' if counts['displayed'] != 1 else ''} found, {counts['current_leads']} of them current."
        return base + ("" if cov["status"] == "complete" else " The search was not complete, so more leads may exist.")
    if cov["status"] == "complete":
        return "No qualified leads were found in the posts retrieved from the sources searched. This is a result of this search, not proof that no demand exists."
    return "No qualified leads were found, but the search was not complete (" + "; ".join(cov["reasons"]) + "). This does not show that no demand exists."
