#!/usr/bin/env python3
"""
QUERY INTERPRETER — natural language -> QueryIntent (spec 2a, 6, 7)
===========================================================================
One LLM call. Input: the raw query string a user typed. Output: a
structured QueryIntent that the rest of the pipeline acts on.

This module is where "search for what the user MEANS" is decided. Two
design points carry that requirement:

  1. topic_embedding_query is CONSTRUCTED, not copied. The interpreter
     writes a string aimed at the embedding space — synonyms, the vocabulary
     posts actually use, the problem as well as the product. The user's
     literal wording is an input to that, never the output itself.

  2. topic_keywords are CONTEXT ONLY. Nothing downstream requires a
     document to contain them. The approved spec removed lexical
     must-match precisely so conceptually equivalent language still
     retrieves — "conversational AI for messaging" must match a query
     about "WhatsApp chatbots" even with zero shared words.

One call, no paraphrase fan-out. The diagnostic measured the router's
paraphrase probes at a median -0.157 AUC against fixed probes, so
generating several probe strings and merging them is a known-negative
design and is not reproduced here.

State: none. Network: one Claude call.
"""

from . import schemas
from .llm import claude, parse_json_block

# Vocabulary is rendered from schemas.py, never duplicated here —
# see the note in schemas.INTENT_DEFINITIONS.

SYSTEM = f"""You translate a natural-language search request into a structured retrieval
plan for Flintel, a B2B social-listening tool that finds buyer-intent signals in
Reddit-style posts.

THE INTENT VOCABULARY — {len(schemas.INTENTS)} labels, use these exact strings:
{schemas.render_intent_definitions('  ')}

WHEN TWO LABELS BOTH SEEM RIGHT — ask what the author is DOING, not what they
are talking about:
{schemas.render_intent_tiebreaks('  ')}

ACTOR DIRECTIONS (use these exact strings):
  company_hiring, individual_seeking, company_buying,
  individual_buying, company_selling, individual_selling

YOUR THREE JOBS

1. WRITE topic_embedding_query.
   This string gets embedded and compared against post embeddings. Write it for the
   embedding space, not for a human. Include the vocabulary real posts would use:
   synonyms, the product AND the problem, adjacent phrasings. Do NOT simply echo the
   user's sentence, and do NOT include intent words like "looking for" or "complaining
   about" - those are handled separately and only pollute the topic vector.
     user: "Find businesses looking for WhatsApp AI agents"
       ->  "WhatsApp Business API chatbot automation conversational AI customer
            messaging agent integration"
     user: "Find founders hiring React developers"
       ->  "React frontend developer engineer JavaScript role position startup team"

2. DECIDE query_mode.
   explicit_intent  - the user named or clearly implied one specific kind of post.
                      e.g. "find people complaining about X", "who is hiring Y".
   exploratory      - the user wants the overall picture of a topic with no intent
                      constraint. e.g. "what are people saying about Shopify?".
                      Set intent_include to [].
   opportunity_scan - the user is asking where the OPPORTUNITIES, needs, problems or
                      prospects are. e.g. "what opportunities exist around X",
                      "find companies that might need Y", "where is the demand for Z".
                      Set intent_include to [] - the opportunity gate handles it.

3. FILL the constraints the query actually states. Leave everything else null.
   Do not invent a geography, a time window or a threshold the user did not ask for.
   Set min_* thresholds only when the user asked for strength ("serious complaints",
   "urgent", "ready to buy"), never by default.

MULTI-INTENT
  intent_logic "OR"  (default) - any listed intent qualifies.
  intent_logic "AND" - ONLY when the user requires both things of the SAME post,
                       e.g. "founders who are hiring AND unhappy with their stack".
                       "X or Y" and plain lists are OR.

OUTPUT
  A single JSON object. No prose, no code fence. Exactly these keys:

{{"topic_keywords": ["..."],
 "topic_embedding_query": "...",
 "intent_include": [],
 "intent_exclude": [],
 "actor_direction_filter": null,
 "actor_type_filter": null,
 "min_commercial_signal": null,
 "min_pain_intensity": null,
 "min_urgency": null,
 "min_specificity": null,
 "time_scope": null,
 "geography": null,
 "intent_logic": "OR",
 "query_mode": "explicit_intent"}}

WORKED EXAMPLES

"Find businesses looking for WhatsApp AI agents"
{{"topic_keywords": ["WhatsApp", "AI agent", "chatbot", "automation"],
 "topic_embedding_query": "WhatsApp Business API chatbot automation conversational AI customer messaging agent integration",
 "intent_include": ["buyer_demand"],
 "intent_exclude": ["provider_supply", "hiring", "irrelevant"],
 "actor_direction_filter": ["company_buying"],
 "actor_type_filter": ["company"],
 "min_commercial_signal": 0.4,
 "min_pain_intensity": null, "min_urgency": null, "min_specificity": null,
 "time_scope": null, "geography": null,
 "intent_logic": "OR", "query_mode": "explicit_intent"}}

"Find founders hiring React developers"
{{"topic_keywords": ["React", "developer", "frontend"],
 "topic_embedding_query": "React frontend developer engineer JavaScript role position startup team build",
 "intent_include": ["hiring"],
 "intent_exclude": ["irrelevant"],
 "actor_direction_filter": ["company_hiring"],
 "actor_type_filter": ["company"],
 "min_commercial_signal": null, "min_pain_intensity": null,
 "min_urgency": null, "min_specificity": null,
 "time_scope": null, "geography": null,
 "intent_logic": "OR", "query_mode": "explicit_intent"}}

"Find people complaining about Shopify checkout"
{{"topic_keywords": ["Shopify", "checkout", "payment"],
 "topic_embedding_query": "Shopify checkout cart payment gateway abandoned broken error conversion storefront",
 "intent_include": ["complaint_pain"],
 "intent_exclude": ["irrelevant"],
 "actor_direction_filter": null, "actor_type_filter": null,
 "min_commercial_signal": null, "min_pain_intensity": 0.35,
 "min_urgency": null, "min_specificity": null,
 "time_scope": null, "geography": null,
 "intent_logic": "OR", "query_mode": "explicit_intent"}}

"What are people saying about Shopify?"
{{"topic_keywords": ["Shopify"],
 "topic_embedding_query": "Shopify ecommerce store platform merchant online shop app theme",
 "intent_include": [],
 "intent_exclude": ["irrelevant"],
 "actor_direction_filter": null, "actor_type_filter": null,
 "min_commercial_signal": null, "min_pain_intensity": null,
 "min_urgency": null, "min_specificity": null,
 "time_scope": null, "geography": null,
 "intent_logic": "OR", "query_mode": "exploratory"}}

"What opportunities exist around WhatsApp automation?"
{{"topic_keywords": ["WhatsApp", "automation"],
 "topic_embedding_query": "WhatsApp Business API automation chatbot messaging customer support broadcast integration workflow",
 "intent_include": [],
 "intent_exclude": ["irrelevant"],
 "actor_direction_filter": null, "actor_type_filter": null,
 "min_commercial_signal": null, "min_pain_intensity": null,
 "min_urgency": null, "min_specificity": null,
 "time_scope": null, "geography": null,
 "intent_logic": "OR", "query_mode": "opportunity_scan"}}
"""


def interpret(query, model=None):
    """Interpret one natural-language query into a QueryIntent dict.

    Never raises on a malformed reply: falls back to an exploratory plan
    built from the raw query, which still retrieves on topic.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")

    raw = claude(SYSTEM, query.strip(), max_tokens=900, model=model)
    parsed = parse_json_block(raw, expect="object")

    if not isinstance(parsed, dict):
        parsed = {
            "topic_keywords": query.split(),
            "topic_embedding_query": query,
            "intent_include": [],
            "intent_exclude": ["irrelevant"],
            "query_mode": "exploratory",
            "intent_logic": "OR",
            "_interpreter_fallback": True,
        }

    qi = schemas.normalize_query_intent(parsed)
    qi["_raw_query"] = query.strip()
    qi["_interpreter_fallback"] = bool(parsed.get("_interpreter_fallback"))
    return qi


def interpret_offline(query, plan):
    """Build a QueryIntent from a hand-written plan — no network.

    Used by validate.py so ranking experiments can run deterministically
    without an interpreter call in the loop, and so an interpreter change
    cannot silently move ranking numbers.
    """
    qi = schemas.normalize_query_intent(plan)
    qi["_raw_query"] = query
    qi["_interpreter_fallback"] = False
    qi["_offline"] = True
    return qi
