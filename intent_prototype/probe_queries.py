#!/usr/bin/env python3
"""
PROBE QUERIES
===========================================================================
The 16 probes from embedding_diagnostic.py, carried over VERBATIM so
prototype numbers sit beside diagnostic numbers without an asterisk. Ten of
them had enough positives in the 866-doc corpus to produce a per-query row
in the diagnostic report; the rest are kept and skipped automatically when
positives are too thin, exactly as the diagnostic did.

`expect` is translated through schemas.map_legacy_intent, because the
diagnostic's 12-category measurement instrument is not the prototype
taxonomy: its `comparison` is this taxonomy's `solution_evaluation`.

`plan` is a hand-written QueryIntent used by validate.py in OFFLINE mode.
It exists so ranking experiments are deterministic and reproducible: an
interpreter prompt change must not be able to move Experiment 2's ranking
numbers. Experiment 2 also runs a LIVE pass through the real interpreter
and reports both, so interpreter drift is visible rather than hidden.
"""

from . import schemas

PROBES = [
    # ── same topic, six different meanings — the core discrimination test
    {"q": "Find businesses looking for AI agents",
     "expect": "buyer_demand", "topic": "ai_agents",
     "plan": {"topic_keywords": ["AI agent", "chatbot", "automation"],
              "topic_embedding_query": "AI agent chatbot automation assistant for business "
                                       "workflow customer support deployment",
              "intent_include": ["buyer_demand"],
              "intent_exclude": ["provider_supply", "hiring", "irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    {"q": "Find people who sell AI agents",
     "expect": "provider_supply", "topic": "ai_agents",
     "plan": {"topic_keywords": ["AI agent", "agency", "services"],
              "topic_embedding_query": "AI agent chatbot development agency services offering "
                                       "building custom automation solutions clients",
              "intent_include": ["provider_supply"],
              "intent_exclude": ["buyer_demand", "irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    {"q": "Find companies building AI agents",
     "expect": "provider_supply", "topic": "ai_agents",
     "plan": {"topic_keywords": ["AI agent", "building", "developing"],
              "topic_embedding_query": "building developing AI agents autonomous systems "
                                       "framework platform product launch startup",
              "intent_include": ["provider_supply"],
              "intent_exclude": ["buyer_demand", "irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    {"q": "Find people complaining about a problem AI agents could solve",
     "expect": "complaint_pain", "topic": "ai_agents",
     "plan": {"topic_keywords": ["manual work", "repetitive", "automation"],
              "topic_embedding_query": "manual repetitive tedious workflow wasting time "
                                       "overwhelmed support tickets backlog cannot keep up",
              "intent_include": ["complaint_pain"],
              "intent_exclude": ["provider_supply", "irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    {"q": "Find people asking how AI agents work",
     "expect": "question_info", "topic": "ai_agents",
     "plan": {"topic_keywords": ["AI agent", "how", "explain"],
              "topic_embedding_query": "how do AI agents work explain architecture memory "
                                       "tools orchestration beginner getting started",
              "intent_include": ["question_info"],
              "intent_exclude": ["irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    {"q": "Find emerging demand for AI automation",
     "expect": "trend_signal", "topic": "ai_agents",
     "plan": {"topic_keywords": ["AI automation", "adoption", "market"],
              "topic_embedding_query": "AI automation adoption growth market shift industry "
                                       "trend enterprise rollout momentum",
              "intent_include": ["trend_signal"],
              "intent_exclude": ["irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    # ── hiring
    {"q": "Find founders hiring product designers",
     "expect": "hiring", "topic": "hiring",
     "plan": {"topic_keywords": ["product designer", "UX", "design"],
              "topic_embedding_query": "product designer UX UI design role position startup "
                                       "founder team join early stage",
              "intent_include": ["hiring"],
              "intent_exclude": ["irrelevant"],
              "actor_direction_filter": ["company_hiring"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    {"q": "Find companies hiring developers",
     "expect": "hiring", "topic": "hiring",
     "plan": {"topic_keywords": ["developer", "engineer", "hiring"],
              "topic_embedding_query": "software developer engineer backend frontend role "
                                       "position job opening team remote",
              "intent_include": ["hiring"],
              "intent_exclude": ["irrelevant"],
              "actor_direction_filter": ["company_hiring"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    # ── CRM / SaaS switching cluster
    {"q": "Find people complaining about their CRM",
     "expect": "complaint_pain", "topic": "crm",
     "plan": {"topic_keywords": ["CRM", "HubSpot", "Salesforce"],
              "topic_embedding_query": "CRM HubSpot Salesforce pipeline contacts sync broken "
                                       "clunky expensive support unusable",
              "intent_include": ["complaint_pain"],
              "intent_exclude": ["irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    {"q": "Find companies using HubSpot",
     "expect": "usage_adoption", "topic": "crm",
     "plan": {"topic_keywords": ["HubSpot", "CRM"],
              "topic_embedding_query": "HubSpot CRM marketing hub workflows our stack we use "
                                       "setup onboarding team",
              "intent_include": ["usage_adoption"],
              "intent_exclude": ["irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    {"q": "Find people looking for a HubSpot alternative",
     "expect": "alternative_switching", "topic": "crm",
     "plan": {"topic_keywords": ["HubSpot alternative", "migrate", "switch"],
              "topic_embedding_query": "HubSpot alternative switching migrating away replace "
                                       "CRM moved off cheaper option",
              "intent_include": ["alternative_switching"],
              "intent_exclude": ["irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    # ── ecommerce
    {"q": "Find the most common complaints about Shopify",
     "expect": "complaint_pain", "topic": "ecommerce",
     "plan": {"topic_keywords": ["Shopify", "store", "checkout"],
              "topic_embedding_query": "Shopify store checkout app theme dashboard support "
                                       "fees broken slow frustrating merchant",
              "intent_include": ["complaint_pain"],
              "intent_exclude": ["irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    # ── brand monitoring
    {"q": "Find what people are saying about OpenAI this week",
     "expect": "general_discussion", "topic": "openai",
     "plan": {"topic_keywords": ["OpenAI", "ChatGPT", "GPT"],
              "topic_embedding_query": "OpenAI ChatGPT GPT model release pricing opinion "
                                       "reaction community take",
              "intent_include": ["general_discussion"],
              "intent_exclude": ["irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    # ── competitor research
    {"q": "Find competitors offering WhatsApp automation",
     "expect": "competitor_research", "topic": "whatsapp",
     "plan": {"topic_keywords": ["WhatsApp automation", "vendors", "tools"],
              "topic_embedding_query": "WhatsApp Business API automation providers vendors "
                                       "tools compared reviewed best list",
              "intent_include": ["competitor_research"],
              "intent_exclude": ["irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    {"q": "Find businesses looking for WhatsApp AI agents",
     "expect": "buyer_demand", "topic": "whatsapp",
     "plan": {"topic_keywords": ["WhatsApp", "AI agent", "chatbot"],
              "topic_embedding_query": "WhatsApp Business API chatbot automation conversational "
                                       "AI customer messaging agent integration",
              "intent_include": ["buyer_demand"],
              "intent_exclude": ["provider_supply", "hiring", "irrelevant"],
              "actor_direction_filter": ["company_buying"],
              "min_commercial_signal": 0.4,
              "query_mode": "explicit_intent", "intent_logic": "OR"}},

    # ── comparison  ->  solution_evaluation in this taxonomy
    {"q": "Compare what customers complain about across two products",
     "expect": "comparison", "topic": "crm",
     "plan": {"topic_keywords": ["compare", "versus", "products"],
              "topic_embedding_query": "comparing two products versus which is better "
                                       "pros cons switching decision evaluation",
              "intent_include": ["solution_evaluation"],
              "intent_exclude": ["irrelevant"],
              "query_mode": "explicit_intent", "intent_logic": "OR"}},
]


# Mode-coverage probes for spec 6 and 7. These have no single `expect`
# label, so they are exercised for BEHAVIOUR (does the right section
# structure come back) rather than scored for ranking quality.
BEHAVIOUR_PROBES = [
    {"q": "What are people saying about Shopify?",
     "expect_mode": "exploratory"},
    {"q": "Find the biggest problems with HubSpot",
     "expect_mode": "explicit_intent", "expect_intents": ["complaint_pain"]},
    {"q": "What opportunities exist around WhatsApp automation?",
     "expect_mode": "opportunity_scan"},
    {"q": "Find companies that might need AI agents",
     "expect_mode": "opportunity_scan"},
    {"q": "Find founders who are hiring and also complaining about their current stack",
     "expect_mode": "explicit_intent", "expect_logic": "AND"},
]


def resolved_expect(probe):
    """The probe's expected intent, in the prototype taxonomy."""
    return schemas.map_legacy_intent(probe["expect"])


def offline_plans():
    """(query, QueryIntent) for every probe, without any interpreter call."""
    from . import query_interpreter
    return [(p["q"], query_interpreter.interpret_offline(p["q"], p["plan"])) for p in PROBES]


# ── Tier 3: real-query product evaluation set ─────────────────────────
# Populated by make_tier3_template.py from Flintel's own query logs, then
# judged by hand. Nothing here is invented: the file ships empty on
# purpose, because a made-up query set would measure nothing real.
TIER3_SCHEMA = {
    "query": "the real user query, verbatim",
    "source": "where it came from (query log / user interview / support)",
    "judgements": {
        "<doc_id>": "relevant | partially_relevant | irrelevant",
    },
    "notes": "optional judge notes",
}
