#!/usr/bin/env python3
"""
FLINTEL INTENT PROTOTYPE — SCHEMAS AND VOCABULARIES
===========================================================================
Single source of truth for every allowed value in the prototype. Every
other module imports from here so a taxonomy change is a one-file change.

ISOLATION CONTRACT (enforced by validate.py --selftest):
    This package imports NOTHING from flintel.py, logics.py, config.py,
    database.py or any other production module. It opens MongoDB for
    READS ONLY and never calls insert/update/delete/create_index.

TAXONOMY NOTE — READ THIS BEFORE CHANGING INTENTS
-------------------------------------------------------------------------
The approved spec says "keep the 11 intent classes" and separately names
`usage_adoption` as a secondary opportunity signal. Those two statements
cannot both hold with 11 classes, because `usage_adoption` is not in the
enumerated 11. This module resolves the conflict by carrying 12 classes:
the enumerated 11 plus `usage_adoption`. Rationale:

  * Dropping it would break the approved opportunity_scan behaviour,
    which explicitly admits usage_adoption as a secondary signal.
  * The existing labelled corpus already contains 24 usage_adoption
    posts; folding them into general_discussion would destroy real
    signal and inflate the catch-all class.

This is flagged for an explicit decision. To collapse back to 11, set
COLLAPSE_USAGE_ADOPTION = True below; usage_adoption then maps into
general_discussion everywhere, including the legacy-label mapping.
"""

# ── switchable taxonomy decision ──────────────────────────────────────
COLLAPSE_USAGE_ADOPTION = False

# ── the intent vocabulary ─────────────────────────────────────────────
INTENTS = [
    "buyer_demand",           # needs it, is shopping / deciding
    "provider_supply",        # sells / builds / offers it
    "hiring",                 # a role is being filled, either direction
    "complaint_pain",         # stuck with a problem, no solution in hand
    "solution_evaluation",    # has a shortlist, gathering evidence to choose
    "alternative_switching",  # committed to leaving, or just migrated
    "competitor_research",    # studying the market from outside
    "trend_signal",           # observing market movement, not acting
    "usage_adoption",         # states they use / have adopted something
    "question_info",          # asking how something works
    "general_discussion",     # on-topic, none of the above
    "irrelevant",             # off-topic / spam
]
if COLLAPSE_USAGE_ADOPTION:
    INTENTS = [i for i in INTENTS if i != "usage_adoption"]

INTENT_SET = set(INTENTS)

# ── legacy mapping: embedding_diagnostic.py labels -> prototype ───────
# The diagnostic's 12-category instrument is NOT the prototype taxonomy.
# `comparison` there means "weighing two or more named options", which is
# exactly solution_evaluation here.
LEGACY_INTENT_MAP = {
    "buyer_demand": "buyer_demand",
    "provider_supply": "provider_supply",
    "hiring": "hiring",
    "complaint_pain": "complaint_pain",
    "alternative_switching": "alternative_switching",
    "usage_adoption": "general_discussion" if COLLAPSE_USAGE_ADOPTION else "usage_adoption",
    "comparison": "solution_evaluation",
    "question_info": "question_info",
    "competitor_research": "competitor_research",
    "trend_signal": "trend_signal",
    "general_discussion": "general_discussion",
    "irrelevant": "irrelevant",
}


def map_legacy_intent(intent):
    """Map a diagnostic-era label onto the prototype taxonomy."""
    return LEGACY_INTENT_MAP.get(intent, "general_discussion")


# ── actor model (orthogonal to intent) ────────────────────────────────
ACTOR_TYPES = ["company", "individual", "analyst", "unknown"]
ACTOR_ROLES = ["buyer", "seller", "seeker", "provider", "observer", "unknown"]
ACTOR_DIRECTIONS = [
    "company_hiring",
    "individual_seeking",
    "company_buying",
    "individual_buying",
    "company_selling",
    "individual_selling",
]

ACTOR_TYPE_SET = set(ACTOR_TYPES)
ACTOR_ROLE_SET = set(ACTOR_ROLES)
ACTOR_DIRECTION_SET = set(ACTOR_DIRECTIONS)

# Derivation table for actor_direction. Key: (intent, actor_type, actor_role).
# Anything not covered derives to None, which means "direction not
# applicable to this intent" — NOT "unknown direction".
_DIRECTION_RULES = {
    ("hiring", "company", "seeker"): "company_hiring",
    ("hiring", "company", "buyer"): "company_hiring",
    ("hiring", "individual", "seeker"): "individual_seeking",
    ("buyer_demand", "company", "buyer"): "company_buying",
    ("buyer_demand", "individual", "buyer"): "individual_buying",
    ("provider_supply", "company", "seller"): "company_selling",
    ("provider_supply", "company", "provider"): "company_selling",
    ("provider_supply", "individual", "seller"): "individual_selling",
    ("provider_supply", "individual", "provider"): "individual_selling",
}


def derive_actor_direction(intent, actor_type, actor_role):
    """Derive actor_direction from the orthogonal actor fields.

    Returns None when the intent has no directional relationship
    (general_discussion, trend_signal, ...) or when the actor fields are
    too uncertain to commit. None is a valid, meaningful value.
    """
    return _DIRECTION_RULES.get((intent, actor_type, actor_role))


# ── query modes ───────────────────────────────────────────────────────
QUERY_MODES = ["explicit_intent", "exploratory", "opportunity_scan"]
INTENT_LOGIC = ["OR", "AND"]

# opportunity_scan defaults, per the approved spec
OPPORTUNITY_PRIMARY_INTENTS = [
    "buyer_demand",
    "complaint_pain",
    "solution_evaluation",
    "alternative_switching",
]
OPPORTUNITY_SECONDARY_INTENTS = [
    i for i in ["question_info", "usage_adoption", "trend_signal"] if i in INTENT_SET
]
OPPORTUNITY_MIN_PRIMARY = 0.40
OPPORTUNITY_MIN_SECONDARY = 0.55

# Classifier commits to a primary label only above this confidence;
# below it the document is flagged ambiguous (ranked down, never dropped).
AMBIGUITY_CONFIDENCE_FLOOR = 0.55

# Retrieval pool sizing (see spec §8). These are floors and targets, not
# latency levers — see the latency note in validate.py.
MIN_CANDIDATE_POOL = 100
TARGET_CANDIDATE_POOL = 200
LOW_CONFIDENCE_SIM_FLOOR = 0.20
CLASSIFIER_BATCH_SIZE = 17

# Short-circuit (spec §8). All three conditions must hold.
SHORTCIRCUIT_HEAD = 50
SHORTCIRCUIT_MIN_PASSING = 15
SHORTCIRCUIT_MIN_CONFIDENCE = 0.70

# AND-logic: below this many strict matches, related matches are ALSO
# returned — in a separate, explicitly labelled section. Never merged.
AND_STRICT_MIN_RESULTS = 5


# ── field specs, used by the validators below and by the LLM prompts ──
QUERY_INTENT_FIELDS = {
    "topic_keywords": "required",
    "topic_embedding_query": "required",
    "intent_include": "required",
    "intent_exclude": "required",
    "actor_direction_filter": "optional",
    "actor_type_filter": "optional",
    "min_commercial_signal": "optional",
    "min_pain_intensity": "optional",
    "min_urgency": "optional",
    "min_specificity": "optional",
    "time_scope": "optional",
    "geography": "optional",
    "intent_logic": "required",
    "query_mode": "required",
    "opportunity_scan": "optional",
}

DOC_CLASSIFICATION_FIELDS = {
    "intent": "required",
    "intent_confidence": "required",
    "secondary_intent": "optional",
    "secondary_confidence": "optional",
    "actor_type": "required",
    "actor_role": "required",
    "actor_direction": "derived",
    "commercial_signal": "required",
    "pain_intensity": "required",
    "urgency": "required",
    "specificity": "required",
    "opportunity_strength": "derived",
    "geography": "optional",
    "industry_hint": "optional",
    "ambiguous": "required",
    "noise": "required",
}


# ── coercion helpers — LLM output is never trusted verbatim ───────────

def clamp01(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(0.0, min(1.0, f))


def coerce_intent(v, default="general_discussion"):
    return v if v in INTENT_SET else default


def coerce_optional_intent(v):
    return v if v in INTENT_SET else None


def coerce_enum(v, allowed, default):
    return v if v in allowed else default


def normalize_query_intent(raw):
    """Coerce raw interpreter output into a valid QueryIntent.

    Never raises: a malformed interpretation degrades to a safe
    exploratory query rather than failing the request.
    """
    raw = raw if isinstance(raw, dict) else {}

    kws = raw.get("topic_keywords")
    kws = [str(k) for k in kws if str(k).strip()] if isinstance(kws, list) else []

    emb_q = raw.get("topic_embedding_query")
    if not isinstance(emb_q, str) or not emb_q.strip():
        emb_q = " ".join(kws)

    inc = raw.get("intent_include")
    inc = [i for i in inc if i in INTENT_SET] if isinstance(inc, list) else []

    exc = raw.get("intent_exclude")
    exc = [i for i in exc if i in INTENT_SET] if isinstance(exc, list) else []
    if "irrelevant" not in exc:
        exc.append("irrelevant")
    # An explicitly requested intent always wins over the exclusion list.
    exc = [i for i in exc if i not in inc]

    mode = coerce_enum(raw.get("query_mode"), set(QUERY_MODES), "exploratory")
    logic = coerce_enum(raw.get("intent_logic"), set(INTENT_LOGIC), "OR")

    adf = raw.get("actor_direction_filter")
    adf = [a for a in adf if a in ACTOR_DIRECTION_SET] if isinstance(adf, list) else None
    adf = adf or None

    atf = raw.get("actor_type_filter")
    atf = [a for a in atf if a in ACTOR_TYPE_SET] if isinstance(atf, list) else None
    atf = atf or None

    def opt01(key):
        v = raw.get(key)
        return None if v is None else clamp01(v)

    ts = raw.get("time_scope")
    if isinstance(ts, dict):
        rel = ts.get("relative")
        rel = rel if rel in ("today", "this_week", "this_month", "recent") else None
        try:
            mad = int(ts["max_age_days"]) if ts.get("max_age_days") is not None else None
        except (TypeError, ValueError):
            mad = None
        time_scope = {"relative": rel, "max_age_days": mad} if (rel or mad) else None
    else:
        time_scope = None

    oscan = None
    if mode == "opportunity_scan":
        given = raw.get("opportunity_scan") if isinstance(raw.get("opportunity_scan"), dict) else {}
        prim = given.get("primary_intents")
        prim = [i for i in prim if i in INTENT_SET] if isinstance(prim, list) else []
        for i in OPPORTUNITY_PRIMARY_INTENTS:
            if i not in prim:
                prim.append(i)
        sec = given.get("secondary_intents")
        sec = [i for i in sec if i in INTENT_SET] if isinstance(sec, list) else []
        for i in OPPORTUNITY_SECONDARY_INTENTS:
            if i not in sec:
                sec.append(i)
        sec = [i for i in sec if i not in prim]
        oscan = {
            "primary_intents": prim,
            "secondary_intents": sec,
            "min_opportunity_strength": clamp01(
                given.get("min_opportunity_strength"), OPPORTUNITY_MIN_PRIMARY
            ) or OPPORTUNITY_MIN_PRIMARY,
            "min_opportunity_strength_secondary": clamp01(
                given.get("min_opportunity_strength_secondary"),
                OPPORTUNITY_MIN_SECONDARY,
            ) or OPPORTUNITY_MIN_SECONDARY,
        }
        # The scan gate is opportunity_strength, not an intent whitelist,
        # so intent_include stays empty and filtering happens in ranker.py.
        inc = []

    return {
        "topic_keywords": kws,
        "topic_embedding_query": emb_q,
        "intent_include": inc,
        "intent_exclude": exc,
        "actor_direction_filter": adf,
        "actor_type_filter": atf,
        "min_commercial_signal": opt01("min_commercial_signal"),
        "min_pain_intensity": opt01("min_pain_intensity"),
        "min_urgency": opt01("min_urgency"),
        "min_specificity": opt01("min_specificity"),
        "time_scope": time_scope,
        "geography": raw.get("geography") if isinstance(raw.get("geography"), str) else None,
        "intent_logic": logic,
        "query_mode": mode,
        "opportunity_scan": oscan,
    }


def normalize_classification(raw):
    """Coerce raw classifier output into a valid DocClassification.

    opportunity_strength is NOT set here — opportunity.py owns it, because
    it needs the query-side topic similarity as an input.
    """
    raw = raw if isinstance(raw, dict) else {}

    # An unrecognised label is a fallback, not a classification. It keeps
    # neither its claimed confidence nor a clean bill of health, or a
    # garbled reply would rank as a confident general_discussion.
    raw_intent = raw.get("intent")
    intent = coerce_intent(raw_intent)
    unrecognised = raw_intent not in INTENT_SET
    conf = 0.0 if unrecognised else clamp01(raw.get("intent_confidence"), 0.0)

    sec = coerce_optional_intent(raw.get("secondary_intent"))
    sec_conf = clamp01(raw.get("secondary_confidence"), 0.0) if sec else None
    if sec == intent:              # a secondary equal to the primary is noise
        sec, sec_conf = None, None
    if sec and (sec_conf or 0.0) <= 0.0:
        sec, sec_conf = None, None

    atype = coerce_enum(raw.get("actor_type"), ACTOR_TYPE_SET, "unknown")
    arole = coerce_enum(raw.get("actor_role"), ACTOR_ROLE_SET, "unknown")

    pain = clamp01(raw.get("pain_intensity"))
    if intent != "complaint_pain" and sec != "complaint_pain":
        pain = 0.0

    ambiguous = (bool(raw.get("ambiguous"))
                 or unrecognised
                 or conf < AMBIGUITY_CONFIDENCE_FLOOR)

    return {
        "intent": intent,
        "intent_confidence": conf,
        "secondary_intent": sec,
        "secondary_confidence": sec_conf,
        "actor_type": atype,
        "actor_role": arole,
        "actor_direction": derive_actor_direction(intent, atype, arole),
        "commercial_signal": clamp01(raw.get("commercial_signal")),
        "pain_intensity": pain,
        "urgency": clamp01(raw.get("urgency")),
        "specificity": clamp01(raw.get("specificity")),
        "opportunity_strength": None,   # filled by opportunity.score()
        "geography": raw.get("geography") if isinstance(raw.get("geography"), str) else None,
        "industry_hint": raw.get("industry_hint") if isinstance(raw.get("industry_hint"), str) else None,
        "ambiguous": ambiguous,
        "noise": bool(raw.get("noise")),
    }
