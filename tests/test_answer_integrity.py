"""
tests/test_answer_integrity.py — regression tests for the guardrails in
answer_integrity.py.

RECONSTRUCTION, NOT THE ORIGINAL CASE. The "seven-post" fixtures below model
the failure PATTERN visible in the report the user pasted (prompt numbers
such as "Post 2", "Posts 6, 10, 5, 22", "Posts 3, 13, 14, 15" cited while the
table was numbered 1-7; competitor/testimonial posts rated "Strong"; "7 of 7"
copied from the model). The original retrieved records — post URLs, ids and
bodies — were never available in this project or conversation. Post bodies,
URLs, the 15 filler posts, which prompt number belongs to which marketing
post, and the expected per-post outcomes are invented for these tests.
They do NOT verify the original seven-post example.
"""
import copy
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import answer_integrity as ai  # noqa: E402
from tests.test_answer_persistence import (  # noqa: E402,F401  (fixtures + helpers)
    TEMPLATES, _for_template, _reopen, env,
)
try:   # the live-stream test needs mongomock; everything else runs without it
    from tests.test_nonblocking_chat_page import app_env, fin  # noqa: E402,F401
except Exception:  # pragma: no cover
    pass

QUERY = (
    "I want to evaluate the quality of Flintel's Reddit monitoring and high-intent opportunity detection for a "
    "B2B SaaS business. **Business:** Flintel — an AI-powered Reddit intelligence tool that helps B2B SaaS "
    "companies discover relevant conversations, identify potential customers, and find high-intent opportunities. "
    "**Ideal customers:** SaaS founders, growth marketers, product marketers, and small B2B marketing teams. "
    "**Relevant keywords and topics:** Reddit monitoring, social listening, customer discovery, competitor "
    "monitoring, alternatives to existing SaaS products, software recommendations, buying intent, product "
    "comparisons, pain points, and requests for tools or solutions. **Task:** Find recent, relevant Reddit "
    "conversations that could represent genuine opportunities for a B2B SaaS company. Prioritize people actively "
    "looking for a solution, comparing products, asking for recommendations, expressing a problem a tool could "
    "solve, or evaluating alternatives. For every result, provide: 1. The original Reddit post title and a working "
    "source URL. 2. The subreddit and publication date, when available. 3. A concise summary of the author's actual "
    "problem or request. 4. The exact evidence supporting the opportunity, using a short quotation or faithful "
    "excerpt when available. 5. An intent classification: High, Medium, or Low. 6. A brief explanation of the "
    "intent score based on the evidence. 7. The likely customer persona and why the conversation is relevant. "
    "8. A suggested helpful, non-spammy response that addresses the author's problem without making unsupported "
    "claims. 9. Any uncertainty, missing information, or reason the result may not be a genuine opportunity. "
    "**Quality requirements:** - Prioritize source-backed evidence over volume. - Do not invent posts, quotations, "
    "dates, URLs, authors, or engagement metrics. - Do not classify a conversation as high intent merely because it "
    "contains a matching keyword. - Distinguish explicit buying intent from general discussion, research, "
    "complaints, and hypothetical questions. - Exclude irrelevant posts, duplicate conversations, and opportunities "
    "that cannot be supported by the source. - If a source cannot be accessed or verified, clearly label that "
    "limitation. - Do not fabricate results if no suitable conversations are found. - Finish with a concise summary "
    "of the strongest opportunities, the main patterns, and the limitations of the search. Return the strongest "
    "evidence-backed opportunities first, with clear reasoning for every High-intent classification."
)

SEVEN = {   # prompt number ("[Post N]") -> (subreddit, title, text)
    2: ("micro_saas", "I need a good reddit tool",
        "Hi all, I'm looking for reddit tools (budget at A$150 per month) to find threads where people ask for "
        "software like ours. Please share recommendations, or I'm open to a free pilot and happy to pay after the trial."),
    6: ("leadsfinder", "What are the best lead gen tools for reddit?",
        "I run a small agency and want to find people on Reddit who are asking for the kind of service we offer. "
        "What do you all use?"),
    10: ("onlyfansadvice", "What Reddit tools do you use for analytics, posting, and subreddit research?",
         "I post in a lot of subreddits to promote my page. Curious what everyone uses to schedule posts, check "
         "stats and find new subs."),
    5: ("SaaSMarketing",
        "I’m currently building a SaaS for my own engineering business, and was hoping to try to put it out there "
        "for other businesses to use. What’s the best way to market / promote it to try get some active users. "
        "Any recommendation would be appreciated.",
        "It started as an internal tool for scheduling site inspections. Not sure where to start with getting users."),
    22: ("SaaS", "I'm actually developing a software and my question is:",
         "Should I market it with cold calling, paid ads, or something else entirely? What channels got your first "
         "customers?"),
    3: ("microsaas", "Tried this Reddit SaaS tool for a month and it actually helped my business",
        "RedShip filtered Reddit threads and scored them by buying intent. I replied to the best ones and closed "
        "three paying customers in a month."),
    13: ("SaaS", "I built a tool that finds Reddit threads where people are already asking for a product like yours",
         "ThreadPilot monitors subreddits, scores intent and drafts replies you can edit. There's a free tier if you "
         "want to try it."),
}
FILLER = ["Best hiking boots for a wet winter?", "How do you store sourdough starter?",
          "Favourite sci-fi novels of the decade", "Tips for a first marathon", "Fixing a squeaky door hinge",
          "Is this mushroom edible?", "Learning piano as an adult", "Cheap flights to Lisbon in March",
          "My cat ignores the new scratching post", "Repotting a fiddle leaf fig",
          "What board games work for two players?", "Painting a rental apartment", "Old laptop for a student",
          "Brewing cold coffee at home", "Choosing a beginner telescope"]


def _url(n):
    return f"https://www.reddit.com/r/x/comments/p{n}/post_{n}/"


def _pool():
    """22 analyzed posts; position i is "[Post i+1]" in the prompt."""
    fillers = iter(FILLER)
    pool = []
    for n in range(1, 23):
        if n in SEVEN:
            sub, title, text = SEVEN[n]
        else:
            sub, title = "misc", next(fillers)
            text = f"{title} I'd love some advice from people who have done this before."
        pool.append({"title": title, "post_text": text, "post_url": _url(n).replace("/x/", f"/{sub}/"),
                     "platform": "reddit", "subreddit": sub})
    return pool


POOL = _pool()
# Post 2 as reconstructed from the pasted report states a budget amount, a free pilot and conditional willingness to pay:
# signals only, no purchase step. POOL_APPROVED is the same pool where post 2 also states an approval, to keep the
# qualified-lead path of the same report covered. The approval sentence is added by this test, it is not from the paste.
APPROVAL = " Our budget has been approved by finance."
POOL_APPROVED = copy.deepcopy(POOL)
POOL_APPROVED[1]["post_text"] += APPROVAL
VARIANTS = [pytest.param(False, id="as-reconstructed:budget+pilot"), pytest.param(True, id="with-approval")]
BAD_ANSWER = {
    "format": "source_list",
    "headline": "Reddit buyers researching monitoring tools",
    "executive_summary": (
        "I found 7 relevant Reddit posts (one shows explicit purchase intent with a stated budget; several others "
        "are active queries about Reddit/lead-gen tools or marketing help). The clearest immediate opportunity is one "
        "user explicitly seeking a paid Reddit tool with a stated monthly budget (A$150/mo) — a high-intent lead."),
    "findings_heading": "What the posts show",
    "key_findings": [
        {"finding": "Explicit buyer with a budget for a Reddit tool", "evidence_type": "Buyer",
         "signal_strength": "Strong", "supporting_post_indices": [2],
         "impact": "One post (Post 2) explicitly asks for a Reddit tool and states a monthly budget of A$150."},
        {"finding": "Multiple users actively researching Reddit/lead-gen tools", "evidence_type": "Buyer",
         "signal_strength": "Moderate", "supporting_post_indices": [6, 10, 5, 22],
         "impact": "Several posts (Posts 6, 10, 5, 22) ask for recommendations or how to market on Reddit."},
        {"finding": "Vendors and competitors promoting monitoring/lead tools", "evidence_type": "Contextual",
         "signal_strength": "Strong", "supporting_post_indices": [3, 13, 14, 15],
         "impact": "Multiple posts (Posts 3, 13, 14, 15 included in the broader set) show existing tools."},
    ],
    "details_heading": "Details behind the strongest findings",
    "detailed_findings": [
        {"title": "Explicit buyer with a budget", "supporting_post_indices": [2],
         "what_evidence_shows": "Post 2 (I need a good reddit tool) states a budget at A$150 per month."},
        {"title": "Research-stage demand across subreddits", "supporting_post_indices": [6, 10, 5, 22],
         "what_evidence_shows": "Post 6 asks for lead gen tools; Post 10 asks about analytics and posting; "
                                "Posts 5 and 22 ask how to market SaaS."},
        {"title": "Active competing offerings", "supporting_post_indices": [3, 13],
         "what_evidence_shows": "Post 3 is a testimonial about RedShip; Post 13 (ThreadPilot) scores intent."},
    ],
    "business_insight": ("These posts suggest a two-track approach: (1) convert the explicit buyer (Post 2); "
                         "(2) capture research-stage users (Posts 6,10,5,22)."),
    "business_insight_points": [
        "Reply to Post 2 with a clear A$150 pilot option and one short case example.",
        "Monitor competitor claims (ThreadPilot, RedShip) from Posts 3 and 13.",
    ],
    "platforms": [{"platform": "reddit", "total_analyzed": 7, "shown_count": 7, "posts": [
        {"index": 2, "source": "micro_saas", "title": "I need a good reddit tool",
         "summary": "User asks for Reddit tools and offers A$150/month budget and pilot.", "sentiment": "neutral"},
        {"index": 6, "source": "leadsfinder", "title": "What are the best lead gen tools for reddit?",
         "summary": "Direct request for recommendations on Reddit lead-gen tools.", "sentiment": "neutral"},
        {"index": 10, "source": "onlyfansadvice", "title": SEVEN[10][1],
         "summary": "User asks which tools people use for analytics, posting, and research.", "sentiment": "neutral"},
        {"index": 5, "source": "SaaSMarketing", "title": SEVEN[5][1],
         "summary": "Founder asks best ways to market/promote a SaaS and get active users.", "sentiment": "neutral"},
        {"index": 22, "source": "SaaS", "title": SEVEN[22][1],
         "summary": "Author asks whether to use cold calling, ads, or other channels for SaaS marketing.",
         "sentiment": "neutral"},
        {"index": 3, "source": "microsaas", "title": SEVEN[3][1],
         "summary": "Testimonial: tool filtered and scored threads and produced paying customers.",
         "sentiment": "positive"},
        {"index": 13, "source": "SaaS", "title": SEVEN[13][1],
         "summary": "Vendor describes ThreadPilot monitoring, scoring intent, and drafting replies.",
         "sentiment": "neutral"},
    ]}],
}
REF = re.compile(r"\bPosts?\s+(\d+(?:\s*(?:,|and)\s*\d+)*)")


def _prose(data):
    texts = [data.get("executive_summary"), data.get("business_insight")] + list(data.get("business_insight_points") or [])
    for f in (data.get("key_findings") or []) + (data.get("detailed_findings") or []):
        texts += [f.get("finding"), f.get("title"), f.get("impact"), f.get("what_evidence_shows")]
    return [t for t in texts if isinstance(t, str)]


def _cited(text):
    return [int(n) for m in REF.finditer(text) for n in re.findall(r"\d+", m.group(1))]


def _rows(data):
    return [p for e in data["platforms"] for p in e["posts"]]


def _assert_report_is_consistent(data, approved=False):
    """Every acceptance criterion, checked on one finished answer. `approved`: post 2 also states an approval."""
    rows = _rows(data)
    by_title = {p["title"]: p for p in rows}
    by_url = {s["post_url"]: s for s in (POOL_APPROVED if approved else POOL)}

    # 1. references: rows numbered 1..k; every citation names a row in the table
    assert [p["index"] for p in rows] == list(range(1, len(rows) + 1))
    shown = {p["index"] for p in rows}
    for text in _prose(data):
        assert set(_cited(text)) <= shown, text
    for f in data["key_findings"] + data["detailed_findings"]:
        assert f["supporting_post_indices"] and set(f["supporting_post_indices"]) <= shown
    buyer_row = by_title["I need a good reddit tool"]["index"]
    assert _cited(data["key_findings"][0]["impact"]) == [buyer_row]
    assert f"Post {buyer_row}" in data["business_insight_points"][0]

    # 3. relevance: off-topic tool question + general marketing questions are gone
    assert set(by_title) == {SEVEN[2][1], SEVEN[6][1], SEVEN[3][1], SEVEN[13][1]}

    # 2 + 6. intent and roles
    lead = by_title[SEVEN[2][1]]
    if approved:                                                  # an approval is a purchase step
        assert lead["intent"] == "High" and lead["qualified"] is True and lead["purchase_intent"] == "explicit"
    else:                                                         # a budget amount + a pilot + conditional willingness is not
        assert lead["intent"] == "Medium" and lead["qualified"] is False and lead["purchase_intent"] == "research"
        assert lead["purchase_signals"] == {"budget": True, "timing": False, "evaluation": True, "willingness": True,
                                            "unconfirmed": False, "purchase_step": False}
        assert "no confirmed purchase step" in lead["intent_reason"] and "states no budget" not in lead["intent_reason"]
        assert data["key_findings"][0]["signal_strength"] != "Strong"                # nothing qualified backs a strong claim
        assert data["verified_summary"].startswith("Checked against the stored posts: 0 qualified leads, 2 buyers")
    assert by_title[SEVEN[6][1]]["intent"] == "Medium" and by_title[SEVEN[6][1]]["qualified"] is False
    assert by_title[SEVEN[6][1]]["purchase_intent"] == "research"
    assert by_title[SEVEN[3][1]]["role"] == "testimonial" and by_title[SEVEN[3][1]]["qualified"] is False
    assert by_title[SEVEN[13][1]]["role"] == "competitor_promo" and by_title[SEVEN[13][1]]["qualified"] is False
    assert [p["title"] for p in rows][:2] == [SEVEN[2][1], SEVEN[6][1]]          # strongest first
    vendor = data["key_findings"][-1]
    assert vendor["demand_signal"] is False and vendor["evidence_type"] == "Builder/Provider"

    # 4. evidence: verbatim from the stored post, never invented
    for p in rows:
        sig = by_url[p["link"]]
        assert p["evidence"] and ai.quote_in_post(p["evidence"], sig["title"], sig["post_text"]), p
        assert p["verification_status"] == "verified"
    if approved:
        assert by_title[SEVEN[2][1]]["evidence"] == "Our budget has been approved by finance."     # the step is the evidence
    else:
        assert "A$150" in by_title[SEVEN[2][1]]["evidence"]

    # 7. counts: one set of records behind table, summary and totals
    c = data["counts"]
    q = 1 if approved else 0                                      # qualified leads; the other buyer is still researching
    assert c == {"retrieved": 22, "unique": 22, "analyzed": 22, "listed": 7, "excluded": 3,
                 "excluded_reasons": {"irrelevant": 3, "duplicate": 0, "unverifiable": 0}, "shown": 4,
                 "relevant": 4, "uncertain": 0, "unverified": 0, "verified": 4, "buyers": 2, "qualified": q,
                 "researching": 2 - q, "competitor": 2,
                 "categories": {"qualified": q, "researching": 2 - q, "unconfirmed": 0, "discussion": 0,
                                "competitor": 2, "unavailable": 0}}
    assert c["shown"] == c["relevant"] + c["uncertain"] + c["unverified"] == c["listed"] - c["excluded"]
    assert {e["reason"] for e in data["excluded_posts"]} == {"not relevant"} and len(data["excluded_posts"]) == 3
    assert data["platforms"][0]["shown_count"] == 4 == len(rows)
    assert data["platforms"][0]["total_analyzed"] == 22
    assert data["executive_summary"].startswith("I found 4 relevant Reddit posts")
    assert c["qualified"] == sum(p["qualified"] for p in rows)


# ── the seven-post report, through the real patcher ──────────────────────────
@pytest.fixture
def lg(env):
    return env[0]


@pytest.mark.parametrize("approved", VARIANTS)
def test_reconstructed_seven_post_pattern_is_reconciled(lg, approved):
    out = json.loads(lg._patch_post_urls_into_answer(json.dumps(BAD_ANSWER), POOL_APPROVED if approved else POOL, query=QUERY))
    _assert_report_is_consistent(out, approved)
    assert out["key_findings"][1]["impact"] == "One post (Post 2) asks for recommendations or how to market on Reddit."
    assert out["key_findings"][1]["finding"] == "One user actively researching Reddit/lead-gen tools"
    assert out["key_findings"][2]["impact"] == "Two posts (Posts 3 and 4 included in the broader set) show existing tools."
    assert out["detailed_findings"][1]["what_evidence_shows"] == "Post 2 asks for lead gen tools."
    assert out["detailed_findings"][2]["what_evidence_shows"] == (
        "Post 3 is a testimonial about RedShip; Post 4 (ThreadPilot) scores intent.")


@pytest.mark.parametrize("approved", VARIANTS)
def test_reconstructed_seven_post_pattern_strict_mode(lg, monkeypatch, approved):
    monkeypatch.setattr(lg, "strict_intent_mode", lambda: True)
    out = json.loads(lg._patch_post_urls_into_answer(json.dumps(BAD_ANSWER), POOL_APPROVED if approved else POOL, query=QUERY))
    _assert_report_is_consistent(out, approved)


def test_without_query_patcher_behaviour_is_unchanged(lg):
    out = json.loads(lg._patch_post_urls_into_answer(json.dumps(BAD_ANSWER), POOL))
    assert [p["index"] for p in _rows(out)] == [2, 6, 10, 5, 22, 3, 13] and "counts" not in out


# ── focused failure cases ────────────────────────────────────────────────────
def _answer(posts, findings=None, summary="I found 2 relevant posts."):
    return {"format": "source_list", "executive_summary": summary, "key_findings": findings or [],
            "platforms": [{"platform": "reddit", "total_analyzed": len(posts), "shown_count": len(posts),
                           "posts": posts}]}


def _sig(n, title, text, sub="SaaS", **extra):
    return dict({"title": title, "post_text": text, "post_url": _url(n), "platform": "reddit", "subreddit": sub},
                **extra)


def _run(sigs, posts, findings=None, query="Find SaaS founders looking for a Reddit monitoring tool"):
    # Each entry models an analysis written about the post its number names (it carries that
    # post's title); unverified identity is covered in tests/test_report_validation.py.
    posts = [dict(p, title=sigs[p["index"] - 1]["title"])
             if isinstance(p, dict) and "title" not in p and isinstance(p.get("index"), int)
             and 1 <= p["index"] <= len(sigs) else p for p in posts]
    return ai.enforce_answer_integrity(_answer(posts, findings), sigs, sigs, query)


def test_budget_alone_is_not_buying_intent():
    s = _sig(1, "Our Reddit ads budget", "We spent $500 per month on Reddit ads for our SaaS and it flopped.")
    out = _run([s], [{"index": 1, "link": s["post_url"], "role": "buyer", "purchase_intent": "explicit"}])
    p = _rows(out)[0]
    assert p["role"] == "discussion" and p["purchase_intent"] == "none" and p["intent"] == "Low" and not p["qualified"]


def test_tool_request_alone_is_research_not_a_qualified_lead():
    s = _sig(1, "Any good Reddit monitoring tool?", "Can anyone recommend a Reddit monitoring tool for our SaaS?")
    p = _rows(_run([s], [{"index": 1, "link": s["post_url"], "purchase_intent": "explicit", "product_fit": "fits"}]))[0]
    assert p["purchase_intent"] == "research" and p["intent"] == "Medium" and not p["qualified"]


def test_buyer_with_budget_but_unverified_fit_is_not_qualified():
    s = _sig(1, "Need a tool asap", "Need a scheduling tool for my SaaS team, budget $200/month, want a trial.")
    p = _rows(_run([s], [{"index": 1, "link": s["post_url"], "product_fit": "fits"}]))
    assert p == [] or not p[0]["qualified"]


def test_model_labels_cannot_promote_a_competitor_or_testimonial():
    promo = _sig(1, "I built a Reddit monitoring tool for SaaS founders", "Launch week! Budget-friendly pricing, try it.")
    testi = _sig(2, "Been using RedShip for Reddit leads", "Been using RedShip for a month and it helped our SaaS a lot.")
    inflated = {"role": "buyer", "purchase_intent": "explicit", "product_fit": "fits"}
    out = _run([promo, testi], [dict(inflated, index=1, link=promo["post_url"]),
                                dict(inflated, index=2, link=testi["post_url"])],
               findings=[{"finding": "Strong demand", "evidence_type": "Buyer", "signal_strength": "Strong",
                          "supporting_post_indices": [1, 2], "impact": "Posts 1 and 2 want to buy."}])
    assert {p["role"] for p in _rows(out)} == {"competitor_promo", "testimonial"}
    assert out["counts"]["qualified"] == 0 and out["counts"]["buyers"] == 0 and out["counts"]["competitor"] == 2
    f = out["key_findings"][0]
    assert f["demand_signal"] is False and f["evidence_type"] == "Builder/Provider"


def test_strong_demand_finding_needs_a_qualified_post():
    s = _sig(1, "Which Reddit monitoring tool?", "Which Reddit monitoring tool do SaaS teams use?")
    out = _run([s], [{"index": 1, "link": s["post_url"]}],
               findings=[{"finding": "Demand", "signal_strength": "Strong", "supporting_post_indices": [1]}])
    assert out["key_findings"][0]["signal_strength"] == "Moderate"


def test_fabricated_quote_is_replaced_by_a_real_excerpt():
    s = _sig(1, "Looking for a Reddit monitoring tool", "Looking for a Reddit monitoring tool for my SaaS. Budget is $99/mo.")
    p = _rows(_run([s], [{"index": 1, "link": s["post_url"], "evidence": "We will sign a $10k annual contract today"}]))[0]
    assert "10k" not in p["evidence"] and ai.quote_in_post(p["evidence"], s["title"], s["post_text"])
    assert any("quote was not found" in u for u in p["uncertainty"])


@pytest.mark.parametrize("extra,status", [
    ({"post_text": "[removed]"}, "removed"),
    ({"post_text": "[deleted]"}, "removed"),
    ({"removed_by_category": "moderator"}, "removed"),
    ({"http_status": 403}, "inaccessible"),
    ({"post_text": None}, "unverified"),
])
def test_unavailable_sources_are_never_shown_as_verified(extra, status):
    s = _sig(1, "Need a Reddit monitoring tool", "Need a Reddit monitoring tool for my SaaS, budget $100/month.")
    s.update(extra)
    out = _run([s], [{"index": 1, "link": s["post_url"], "summary": "Wants a tool."}])
    p = _rows(out)[0]
    assert p["verification_status"] == status and p["intent"] == "Uncertain" and not p["qualified"]
    assert p["role"] is None and p["purchase_intent"] == "unknown" == p["product_fit"]   # nothing invented
    assert out["counts"]["verified"] == 0 and out["counts"]["buyers"] == 0
    if status in ("removed", "unverified"):
        assert p["summary"] != "Wants a tool." and p["evidence"] is None
    if status == "inaccessible":                                        # stored text: excerpt stays verbatim
        assert ai.quote_in_post(p["evidence"], s["title"], s["post_text"])


def test_unverified_stub_is_labelled_not_dropped_as_irrelevant():
    stub = {"title": "r/onlyfansadvice", "post_text": None, "post_url": _url(9), "platform": "reddit"}
    out = _run([stub], [{"index": 1, "link": stub["post_url"], "summary": "Asks for a tool."}])
    assert _rows(out)[0]["verification_status"] == "unverified" and out["counts"]["excluded"] == 0


def test_unresolvable_and_duplicate_posts_are_excluded_and_counted():
    a = _sig(1, "Need a Reddit monitoring tool", "Need a Reddit monitoring tool for my SaaS.")
    crosspost = dict(a, post_url=_url(2))
    out = _run([a, crosspost], [{"index": 1, "link": a["post_url"]}, {"index": 2, "link": crosspost["post_url"]},
                                {"index": 3, "title": "Invented post", "summary": "Not a stored post."}])
    assert len(_rows(out)) == 1
    assert out["counts"]["excluded_reasons"] == {"irrelevant": 0, "duplicate": 1, "unverifiable": 1}


def test_nothing_relevant_becomes_an_honest_no_results():
    s = _sig(1, "Best way to market my SaaS?", "Cold calling or paid ads for my SaaS?")
    out = _run([s], [{"index": 1, "link": s["post_url"]}])
    assert out["format"] == "no_results" and out["counts"]["relevant"] == 0 and out["counts"]["listed"] == 1


def test_reference_rewriting():
    gmap = {6: 2, 2: 1}
    assert ai.rewrite_post_refs("Posts 6, 10, 5, 22 ask", gmap) == "Post 2 asks"
    assert ai.rewrite_post_refs("see [Post 2] and Post 14", gmap) == "see [Post 1]"
    assert ai.rewrite_post_refs("Posts 2 and 6", gmap) == "Posts 1 and 2"
    assert ai.rewrite_post_refs("Post 14 is a vendor.", gmap) == ""                 # nothing citable is left


# ── usage measurement ────────────────────────────────────────────────────────
@pytest.fixture
def captured(monkeypatch):
    import usage_meter
    docs = []
    monkeypatch.setattr(usage_meter._executor, "submit", lambda fn, doc: docs.append(doc))
    return usage_meter, docs


def test_usage_is_attributed_to_the_search_that_spent_it(captured):
    um, docs = captured
    um.record_usage("llm", "gpt-5-mini", 1000, 200)
    with um.attribution(chat_id="c1", owner_key="o@x.com", topic_key="t1"):
        um.record_usage("embedding", "text-embedding-3-small", 50, 0)   # as logics.py passes it
        um.record_usage("llm", "gpt-5-mini", 2000, 400)
    um.record_usage("llm", "gpt-5-mini", None, None)          # no usage reported -> nothing recorded
    assert [d["chat_id"] for d in docs] == [None, "c1", "c1"]
    assert all(d["topic_key"] == "t1" for d in docs[1:])
    s = um.summarize_search_usage(docs[1:])
    assert s["calls"] == 2 and s["input_tokens"] == 2050 and s["output_tokens"] == 400 and s["total_tokens"] == 2450
    assert s["cost_usd"] == pytest.approx(2000 * 0.25e-6 + 400 * 2e-6 + 50 * 0.02e-6)


def test_unpriced_usage_is_not_reported_as_a_cost(captured):
    um, docs = captured
    um.record_usage("llm", "unknown-model", 10, 10)
    assert um.summarize_search_usage(docs)["cost_usd"] is None


# ── complete search-to-report flow ───────────────────────────────────────────
def _fake_llm(*args, **kwargs):
    """Map steps get notes; the final (reduce) call gets the seven-post report."""
    user = args[1] if len(args) > 1 else kwargs.get("user_message", "")
    if "grounded notes already condensed" in user or "Posts (title + text only)" in user:
        return json.dumps(BAD_ANSWER)
    return "- Asks for a Reddit tool with a budget (Post 2)"


def _search_to_saved_answer(lg, idx, chats, monkeypatch, pool=POOL):
    calls = []
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: calls.append(a) or _fake_llm(*a, **k))
    idx.add_search_to_chat("c1", "o1", QUERY, lg.normalize_topic_key(QUERY), ["reddit monitoring"], "reddit")
    msg = chats.find_one({"chat_id": "c1", "owner_key": "o1"})["messages"][-1]
    msg["evidence_required"] = len(pool)          # the original run analyzed 22+ posts
    idx._complete_message_answer_and_results("c1", "o1", msg, pool)
    return chats.find_one({"chat_id": "c1", "owner_key": "o1"}), calls


@pytest.mark.parametrize("approved", VARIANTS)
def test_search_to_report_flow_persists_a_consistent_report(env, monkeypatch, approved):
    lg, idx, chats = env
    doc, calls = _search_to_saved_answer(lg, idx, chats, monkeypatch, POOL_APPROVED if approved else POOL)
    assert len(calls) == 3                        # 22 posts -> two map chunks + one reduce
    _assert_report_is_consistent(json.loads(doc["messages"][-1]["claude_answer"]), approved)


ROWS_JS = """() => [...document.querySelectorAll('.ap-sources-table tbody tr')].map(tr =>
  [...tr.querySelectorAll('td')].map(td => td.innerText.trim()))"""


@pytest.mark.parametrize("approved", VARIANTS)
@pytest.mark.parametrize("name", TEMPLATES)
def test_search_to_report_flow_renders_the_same_records(integrity_browser, env, monkeypatch, name, approved):
    lg, idx, chats = env
    doc, _ = _search_to_saved_answer(lg, idx, chats, monkeypatch, POOL_APPROVED if approved else POOL)
    page, errors = _reopen(integrity_browser, name, _for_template(doc))
    try:
        rows = page.evaluate(ROWS_JS)
        assert [r[0] for r in rows] == ["1", "2", "3", "4"]
        assert rows[0][2] == "I need a good reddit tool" and "A$150" in rows[0][3]
        if approved:
            assert rows[0][4].startswith("High · Prospective buyer")
            assert "Purchase: explicit · Fit: fits · Source: verified" in rows[0][4]
        else:
            assert rows[0][4].startswith("Medium · Prospective buyer")
            assert "Purchase: research · Fit: fits · Source: verified" in rows[0][4]
            assert "no confirmed purchase step" in rows[0][3]            # the reason lists what the post does state
        assert rows[1][4].startswith("Medium") and "Purchase: research" in rows[1][4]
        assert {rows[2][4].split("\n")[0], rows[3][4].split("\n")[0]} == {"Low · Testimonial", "Low · Competitor promotion"}
        shown = {int(r[0]) for r in rows}
        assert set(_cited(page.inner_text(".answer-rendered"))) <= shown            # UI citation audit
        text = page.inner_text(".answer-rendered")
        assert "Reddit · 4 of 22" not in text and "qualified lead" not in text and "listed excluded" not in text   # classification counts are not shown
        assert "onlyfans" not in text.lower() and "Post 22" not in text and "Post 13" not in text
        assert "not buyer demand" in text and errors == []
    finally:
        page.close()


@pytest.mark.parametrize("approved", VARIANTS)
def test_stream_route_saves_the_reconciled_report(request, monkeypatch, approved):
    """The live SSE route (routes.stream_answer) saves the same reconciled report."""
    nb = pytest.importorskip("tests.test_nonblocking_chat_page")
    f = request.getfixturevalue("fin")
    rt = f["rt"]
    pool = POOL_APPROVED if approved else POOL
    nb._seed(f["chats"], "c20", [dict(nb._turn(QUERY, 1), evidence_required=len(pool))])
    monkeypatch.setattr(rt, "get_evidence_with_topup", lambda **kw: copy.deepcopy(pool))
    monkeypatch.setattr(rt, "analyze_with_claude", lambda *a, **k: json.dumps(BAD_ANSWER), raising=False)
    events = []
    for chunk in nb._stream(f, monkeypatch, "c20", topic_key=QUERY.lower()):
        events.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
        if '"done"' in events[-1] or '"error"' in events[-1]:
            break
    assert '"done"' in events[-1], events[-3:]
    saved = f["chats"].find_one({"chat_id": "c20"})["messages"][-1]["claude_answer"]
    _assert_report_is_consistent(json.loads(saved), approved)


# ── 1. citation audit: every string the report carries ───────────────────────
def _strings(node, skip=("platforms", "counts", "excluded_posts")):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for x in node:
            yield from _strings(x, skip)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k not in skip:
                yield from _strings(v, skip)


ANY_REF = re.compile(r"\b[Pp]osts?\s*#?\s*\d+(?:\s*(?:,|and|&|or|/)\s*#?\s*\d+)*")


def test_citation_audit_covers_every_field(lg):
    bad = copy.deepcopy(BAD_ANSWER)
    bad["conclusion"] = "Post 22 and Post 13 show the space is crowded."
    bad["followup_question"] = "Want a reply drafted for Post 2?"
    bad["market_pattern"] = "Posts #6 and #10 suggest research-stage demand."
    out = json.loads(lg._patch_post_urls_into_answer(json.dumps(bad), POOL, query=QUERY))
    shown = {p["index"] for p in _rows(out)}
    for text in _strings(out):
        for m in ANY_REF.finditer(text):
            assert {int(n) for n in re.findall(r"\d+", m.group(0))} <= shown, text
        assert not re.search("[\x01-\x04]", text)
    assert out["conclusion"] == "Post 4 shows the space is crowded."
    assert out["followup_question"] == "Want a reply drafted for Post 1?"
    assert out["market_pattern"] == "Post 2 suggests research-stage demand."


@pytest.mark.parametrize("text,expected", [
    ("Several posts (Posts 6, 10, 5, 22) ask for tools.", "One post (Post 2) asks for tools."),
    ("Two buyers (Posts 2 and 6) want a trial.", "Two buyers (Posts 1 and 2) want a trial."),
    ("Several people (Posts 2, 22) are asking.", "One person (Post 1) is asking."),
    ("Posts 6, 10 ask for tools; Post 14 is a vendor.", "Post 2 asks for tools."),
    ("Post 14 is a vendor.", ""),
])
def test_reference_and_quantity_rewriting(text, expected):
    assert ai.rewrite_post_refs(text, {2: 1, 6: 2}) == expected


# ── 2. cards versus report ───────────────────────────────────────────────────
def test_stored_results_carry_their_report_status(env, monkeypatch):
    lg, idx, chats = env
    doc, _ = _search_to_saved_answer(lg, idx, chats, monkeypatch)
    msg = doc["messages"][-1]
    answer, results = json.loads(msg["claude_answer"]), msg["results"]
    statuses = [r["review"]["status"] for r in results]
    assert len(results) == answer["counts"]["retrieved"] == 22                       # nothing silently dropped
    assert statuses[:4] == ["in_report"] * 4                                         # report rows first, in order
    assert [r["review"]["index"] for r in results[:4]] == [1, 2, 3, 4]
    assert statuses.count("in_report") == answer["counts"]["shown"]
    assert statuses.count("excluded") == len(answer["excluded_posts"]) == answer["counts"]["excluded"]
    assert sum(r["review"]["qualified"] for r in results) == answer["counts"]["qualified"]
    excluded_titles = {r["title"] for r in results if r["review"]["status"] == "excluded"}
    assert excluded_titles == {SEVEN[10][1], SEVEN[5][1], SEVEN[22][1]}
    assert all(not r["review"]["qualified"] for r in results if r["review"]["status"] != "in_report")


def test_unchecked_answers_keep_their_results_unchanged():
    res = [{"title": "t", "post_url": "u"}]
    assert ai.annotate_results(res, json.dumps({"format": "source_list"})) is res


CARDS_JS = """() => [...document.querySelectorAll('.answer-rendered')].map(el => el.innerText)"""


@pytest.mark.parametrize("name", TEMPLATES)
def test_closest_match_cards_are_labelled_and_separated(integrity_browser, name):
    results = ai.annotate_results(copy.deepcopy(POOL[:3]), json.dumps({
        "format": "source_list", "counts": {"shown": 0},
        "excluded_posts": [{"link": POOL[0]["post_url"], "reason": "not relevant"}]}))
    chat = {"chat_id": "c1", "title": "t", "messages": [{
        "query": "q", "topic_key": "q", "targeting_platform": "reddit", "results": results,
        "claude_answer": json.dumps({"format": "no_results", "message": "No qualifying posts."})}]}
    page, errors = _reopen(integrity_browser, name, _for_template(chat))
    try:
        text = page.evaluate(CARDS_JS)[0]
        assert "Closest matches — similarity matches, not reviewed as results or leads" in text
        assert "Excluded from the report (not relevant)" in text
        assert text.count("Not selected for the report") == 2 and errors == []
    finally:
        page.close()


# ── 3. separate dimensions; uncertain stays uncertain ────────────────────────
Q = "Find SaaS founders looking for a Reddit monitoring tool"


def test_model_fit_label_alone_does_not_make_a_post_relevant():
    # explicit purchase intent (a stated approval) with an off-topic category: intent does not make the post relevant
    s = _sig(1, "Need a scheduling tool for my SaaS",
             "Need a scheduling tool for my SaaS team, budget $200/month, want a trial. The budget is approved.")
    out = _run([s], [{"index": 1, "link": s["post_url"], "role": "buyer", "purchase_intent": "explicit",
                      "product_fit": "fits"}], query=Q)
    p = _rows(out)[0]
    assert (p["purchase_intent"], p["product_fit"], p["verification_status"]) == ("explicit", "uncertain", "verified")
    assert p["intent"] == "Uncertain" and p["relevance"] == "uncertain" and not p["qualified"]
    assert out["counts"]["relevant"] == 0 and out["counts"]["uncertain"] == 1 and out["counts"]["buyers"] == 0


def test_without_a_model_label_the_same_post_is_excluded():
    s = _sig(1, "Need a scheduling tool for my SaaS", "Need a scheduling tool for my SaaS team, budget $200/month.")
    out = _run([s], [{"index": 1, "link": s["post_url"]}], query=Q)
    assert out["format"] == "no_results" and out["counts"]["excluded_reasons"]["irrelevant"] == 1


@pytest.mark.parametrize("text,model,expected", [
    # (purchase_intent, product_fit, intent) vary independently
    ("Looking for a Reddit monitoring tool for my SaaS, budget $99/mo, approved by finance.", {}, ("explicit", "fits", "High")),
    ("Looking for a Reddit monitoring tool for my SaaS.", {}, ("research", "fits", "Medium")),
    ("Looking for a Reddit monitoring tool for my SaaS, budget $99/mo, approved by finance.", {"product_fit": "partial"}, ("explicit", "partial", "Medium")),
    ("Looking for a Reddit monitoring tool for my SaaS, budget $99/mo, approved by finance.", {"purchase_intent": "research"}, ("research", "fits", "Medium")),
    # a budget amount alone is a signal, not a purchase step (also when the model claims explicit)
    ("Looking for a Reddit monitoring tool for my SaaS, budget $99/mo.", {}, ("research", "fits", "Medium")),
    ("Looking for a Reddit monitoring tool for my SaaS, budget $99/mo.", {"purchase_intent": "explicit"}, ("research", "fits", "Medium")),
])
def test_purchase_intent_and_fit_are_separate(text, model, expected):
    s = _sig(1, "Tool question", text)
    p = _rows(_run([s], [dict({"index": 1, "link": s["post_url"]}, **model)], query=Q))[0]
    assert (p["purchase_intent"], p["product_fit"], p["intent"]) == expected


# ── 4. platform rules are separate from shared logic ─────────────────────────
def test_cross_platform_rows_use_their_own_rules():
    rd = _sig(1, "Need a Reddit monitoring tool", "Need a Reddit monitoring tool for my SaaS.", sub="SaaS")
    li = {"title": "Reddit monitoring tool for B2B?", "post_text": "Any Reddit monitoring tool our SaaS sales team likes?",
          "post_url": "https://www.linkedin.com/posts/abc_123", "subreddit": None}           # no platform field
    x = {"title": "", "post_text": "[removed]", "post_url": "https://x.com/u/status/9", "platform": "twitter"}
    assert ai.verification_status(x) == "verified"       # Reddit's "[removed]" marker is Reddit-only
    gone = dict(li, post_url="https://www.linkedin.com/posts/gone_1", removed=True,
                post_text="Need a Reddit monitoring tool for our SaaS")
    sigs = [rd, li, x, gone]
    answer = {"format": "source_list", "key_findings": [], "platforms": [
        {"platform": "reddit", "posts": [{"index": 1, "link": rd["post_url"]}]},
        {"platform": "linkedin", "posts": [{"index": 2, "link": li["post_url"]}, {"index": 4, "link": gone["post_url"]}]}]}
    out = ai.enforce_answer_integrity(answer, sigs, sigs, Q)
    rows = {p["link"]: p for p in _rows(out)}
    assert ai.platform_of(li) == "linkedin" and ai.platform_of(x) == "x"
    assert ai.rules_for(li).community(li) is None and ai.rules_for(rd).community(rd) == "SaaS"
    assert rows[li["post_url"]]["platform"] == "linkedin" and rows[li["post_url"]]["verification_status"] == "verified"
    assert rows[gone["post_url"]]["verification_status"] == "removed"   # generic removal flag works everywhere
    assert {e["platform"]: e["total_analyzed"] for e in out["platforms"]} == {"reddit": 1, "linkedin": 2}
    assert ai.verification_status(dict(rd, post_text="[removed]")) == "removed"


# ── 5. usage attribution through the real metering hook ──────────────────────
class _FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, "error body"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class _FakeHTTP:
    """Stands in for httpx.Client inside logics._call_claude; replies from a script."""
    def __init__(self, script):
        self.script = list(script)

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        return _FakeResponse(*self.script.pop(0))


def _ok(n_in, n_out, text="{}"):
    return (200, {"status": "completed", "usage": {"input_tokens": n_in, "output_tokens": n_out},
                  "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]})


def test_real_llm_hook_attribution_retries_and_failures(lg, captured, monkeypatch):
    um, docs = captured
    monkeypatch.setattr(lg.httpx, "Client", _FakeHTTP([(500, {}), _ok(100, 10), _ok(200, 20)]), raising=False)
    with um.attribution(chat_id="c1", owner_key="o1", topic_key="t1"):
        with pytest.raises(Exception):
            lg._call_claude("sys", "user")                     # failed attempt: billed nothing, records nothing
        lg._call_claude("sys", "user")                         # the retry
    lg._call_claude("sys", "user")                             # outside any search
    assert [(d["chat_id"], d["topic_key"], d["input_tokens"]) for d in docs] == [("c1", "t1", 100), (None, None, 200)]


def test_concurrent_searches_and_threads_keep_their_own_attribution(captured):
    import threading
    um, docs = captured
    barrier = threading.Barrier(6)

    def search(i):
        with um.attribution(chat_id=f"c{i}", owner_key="o", topic_key=f"t{i}"):
            for _ in range(5):
                barrier.wait()                                 # interleave all six searches
                um.record_usage("llm", "gpt-5-mini", 10 * (i + 1), 1)
    threads = [threading.Thread(target=search, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    for d in docs:
        i = int(d["chat_id"][1:])
        assert d["topic_key"] == f"t{i}" and d["input_tokens"] == 10 * (i + 1)
    for i in range(6):
        s = um.summarize_search_usage([d for d in docs if d["chat_id"] == f"c{i}"])
        assert s["calls"] == 5 and s["input_tokens"] == 50 * (i + 1)


def test_plain_background_threads_do_not_inherit_attribution(captured):
    """Why the wraps live INSIDE thread bodies (_topup, the progress threads)."""
    import contextvars
    import threading
    um, docs = captured
    with um.attribution(chat_id="c1", owner_key="o", topic_key="t1"):
        t = threading.Thread(target=um.record_usage, args=("llm", "gpt-5-mini", 1, 1))
        t.start()
        t.join()
        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(um.record_usage, "llm", "gpt-5-mini", 2, 2))
        t.start()
        t.join()
    assert [d["chat_id"] for d in docs] == [None, "c1"]


def test_worker_path_attributes_retrieval_and_analysis(env, captured, monkeypatch):
    """index._fill_in_message_outputs -> retrieval + analysis, as the turn-finisher runs it."""
    lg, idx, chats = env
    um, docs = captured
    fi = sys.modules["flintel"]
    monkeypatch.setattr(fi, "should_trigger_immediately", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(idx, "append_to_chat_summary", lambda *a, **k: None, raising=False)
    for name, fn in (("_is_owner_busy", lambda *a: False), ("_set_owner_busy", lambda *a: None),
                     ("_clear_owner_busy", lambda *a: None)):
        monkeypatch.setattr(idx, name, fn, raising=False)
    monkeypatch.setattr(idx, "get_evidence_with_topup",
                        lambda **kw: um.record_usage("embedding", "text-embedding-3-small", 30, 0) or copy.deepcopy(POOL))
    monkeypatch.setattr(lg, "_call_claude", lambda *a, **k: um.record_usage("llm", "gpt-5-mini", 500, 50) or _fake_llm(*a, **k))
    idx.add_search_to_chat("c9", "o9", QUERY, lg.normalize_topic_key(QUERY), ["kw"], "reddit")
    chat = chats.find_one({"chat_id": "c9", "owner_key": "o9"})
    chat["messages"][-1]["evidence_required"] = len(POOL)
    idx._fill_in_message_outputs("c9", "o9", chat["messages"])
    tk = lg.normalize_topic_key(QUERY)
    assert docs and all((d["chat_id"], d["topic_key"]) == ("c9", tk) for d in docs)
    s = um.summarize_search_usage(docs)
    assert s["by_call_type"]["embedding"]["calls"] >= 1 and s["by_call_type"]["llm"]["calls"] == 3
    assert s["cost_usd"] == pytest.approx(sum(d["cost_usd"] for d in docs))


def test_stream_route_attributes_its_usage(request, monkeypatch, captured):
    nb = pytest.importorskip("tests.test_nonblocking_chat_page")
    f = request.getfixturevalue("fin")
    um, docs = captured
    rt = f["rt"]
    nb._seed(f["chats"], "c21", [dict(nb._turn(QUERY, 1), evidence_required=len(POOL))])
    monkeypatch.setattr(rt, "get_evidence_with_topup",
                        lambda **kw: um.record_usage("embedding", "text-embedding-3-small", 40, 0) or copy.deepcopy(POOL))
    monkeypatch.setattr(rt, "analyze_with_claude",
                        lambda *a, **k: um.record_usage("llm", "gpt-5-mini", 900, 90) or json.dumps(BAD_ANSWER), raising=False)
    for chunk in nb._stream(f, monkeypatch, "c21", topic_key=QUERY.lower()):
        if '"done"' in (chunk.decode() if isinstance(chunk, bytes) else str(chunk)):
            break
    mine = [d for d in docs if d["call_type"] in ("embedding", "llm") and d["input_tokens"] in (40, 900)]
    assert mine and all((d["chat_id"], d["topic_key"]) == ("c21", QUERY.lower()) for d in mine)


def test_search_usage_query_reconciles_with_stored_rows(captured, monkeypatch):
    mongomock = pytest.importorskip("mongomock")
    um, docs = captured
    with um.attribution(chat_id="c1", owner_key="o", topic_key="t1"):
        um.record_usage("llm", "gpt-5-mini", 1000, 100)
        um.record_usage("embedding", "text-embedding-3-small", 20, 0)
    with um.attribution(chat_id="c1", owner_key="o", topic_key="t2"):
        um.record_usage("llm", "gpt-5-mini", 7, 7)
    db = mongomock.MongoClient()["usage"]
    db["FLINTEL_USAGE_IDEA"].insert_many(copy.deepcopy(docs))
    import types
    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace(db3=db))
    s = um.get_search_usage("c1", "t1")
    assert s["calls"] == 2 and s["input_tokens"] == 1020 and s["output_tokens"] == 100
    assert s["cost_usd"] == pytest.approx(docs[0]["cost_usd"] + docs[1]["cost_usd"])


def test_rewriting_leaves_non_citations_and_unrelated_counts_alone():
    gmap = {2: 1}
    assert ai.rewrite_post_refs("The Washington Post 2024 survey agrees.", gmap, max_ref=22) == \
        "The Washington Post 2024 survey agrees."
    assert ai.rewrite_post_refs("See Post 2024 and Post 2.", gmap, max_ref=22) == "See Post 2024 and Post 1."
    counts = {"relevant": 1, "qualified": 0, "analyzed": 22}
    claim = "We analyzed 30 posts and found 7 relevant posts and 3 qualified leads; we found 5 threads."
    assert ai._COUNT_CLAIM.sub(lambda m: ai._restate_count(m, counts), claim) == \
        "We analyzed 22 posts and found 1 relevant post and 3 qualified leads; we found 5 threads."


def test_my_business_does_not_switch_on_the_b2b_audience_check():
    s = _sig(1, "Need a plumber app recommendation", "Can anyone recommend an app to book a plumber?", sub="homeowners")
    out = _run([s], [{"index": 1, "link": s["post_url"]}], query="Find homeowners who need a plumber app for my business")
    assert len(_rows(out)) == 1



# ── Phase 1: an analysis that is not matched to its source supports nothing ───────────────────────────────────────
# SYNTHETIC FIXTURES. Every post below is invented for these tests (labelled "[synthetic]"); none is a real Reddit post and
# none reproduces the original CRM report, whose records are not available in this project.
BUYER_A = ("[synthetic] Commercial brokerage needs a CRM",
           "[synthetic] We are a small commercial brokerage. We need a CRM for deals and contacts. Our budget is approved "
           "and we will sign with a vendor this month.")
BUYER_B = ("[synthetic] Property managers need a CRM for tenants",
           "[synthetic] Our property management firm needs a CRM for tenant follow-ups. The budget is approved by finance "
           "and we will subscribe this quarter.")
VENDOR = ("[synthetic] I built a CRM for brokerages",
          "[synthetic] Launch week! I built a CRM for small brokerages. Budget-friendly pricing, try it today.")
CRM_QUERY = "Find people looking for a CRM"


def _sigs(*pairs):
    return [_sig(i + 1, t, x, sub="CRM") for i, (t, x) in enumerate(pairs)]


def _own(sig, **extra):
    """An analysis entry written about this very post (matched by title and number)."""
    return dict({"link": sig["post_url"], "role": "buyer", "purchase_intent": "explicit", "product_fit": "fits"}, **extra)


def _foreign(sig, **extra):
    """An analysis entry whose title and quotation do not tie it to this post (a mismatched analysis)."""
    return dict({"link": sig["post_url"], "title": "[synthetic] A different post entirely", "role": "buyer",
                 "purchase_intent": "explicit", "product_fit": "fits", "summary": "Budget approved and ready to sign."}, **extra)


def _go(sigs, posts, findings=None, summary="", **top):
    posts = [dict(p, index=i + 1) if "index" not in p else p for i, p in enumerate(posts)]
    posts = [dict(p, title=sigs[p["index"] - 1]["title"]) if "title" not in p else p for p in posts]
    data = _answer(posts, findings, summary)
    data.update(top)
    return ai.enforce_answer_integrity(data, sigs, sigs, CRM_QUERY)


def _finding(indices, strength="Strong", text=None):
    refs = ", ".join(f"Post {i}" for i in indices)
    return {"finding": "Brokerages are ready to buy a CRM", "evidence_type": "Buyer", "signal_strength": strength,
            "supporting_post_indices": list(indices), "impact": text or f"{refs} are ready to buy."}


def test_matched_buyer_still_qualifies():
    s = _sigs(BUYER_A)
    out = _go(s, [_own(s[0])], summary="I found 1 qualified post.")
    p = _rows(out)[0]
    assert p["analysis_status"] == "matched" and p["analysis_verified"] is True
    assert p["qualified"] and p["intent"] == "High"
    c = out["counts"]
    assert (c["qualified"], c["buyers"], c["relevant"], c["uncertain"]) == (1, 1, 1, 0)
    assert out["analysis_check"] == {"source_verified": 1, "analysis_verified": 1, "analysis_unverified": 0}


def test_unmatched_analysis_contributes_zero_but_keeps_its_source_record():
    s = _sigs(BUYER_A, BUYER_B)
    out = _go(s, [_own(s[0]), _foreign(s[1])])
    matched, unmatched = _rows(out)
    assert matched["analysis_status"] == "matched" and matched["qualified"]
    # the source record is kept and still traceable ...
    assert unmatched["link"] == s[1]["post_url"] and unmatched["verification_status"] == "verified"
    assert unmatched["analysis_status"] == "unmatched" and unmatched["analysis_verified"] is False
    assert unmatched["summary"] == ai.SUMMARY_UNAVAILABLE and "could not confirm" in " ".join(unmatched["uncertainty"])
    # ... but nothing it says counts: not a lead, not relevant, not a buyer, not demand
    assert unmatched["intent"] == "Uncertain" and unmatched["qualified"] is False and unmatched["relevance"] == "uncertain"
    c = out["counts"]
    assert (c["shown"], c["qualified"], c["buyers"], c["relevant"], c["uncertain"], c["researching"]) == (2, 1, 1, 1, 1, 0)
    assert c["shown"] == c["relevant"] + c["uncertain"] + c["unverified"]            # existing invariant
    assert out["analysis_check"] == {"source_verified": 2, "analysis_verified": 1, "analysis_unverified": 1}
    assert sum(c["categories"].values()) == c["shown"] and c["categories"]["unconfirmed"] == 1
    assert "kept with a source link only" in out["verified_summary"]


def test_unmatched_row_alone_supports_no_finding_and_no_headline():
    s = _sigs(BUYER_A)
    out = _go(s, [_foreign(s[0])], findings=[_finding([1])],
              summary="Flintel identified seven high-intent buyers. Posts 1 are ready to buy.", headline="7 high-intent buyers found")
    assert out["counts"]["qualified"] == 0 and out["counts"]["buyers"] == 0 and out["counts"]["relevant"] == 0
    assert out["key_findings"] == []                                   # the only support was an unmatched analysis: removed
    text = out["executive_summary"] + " " + out["headline"]
    assert "seven" not in text and "7 " not in text and "ready to buy" not in text
    assert _rows(out)[0]["link"] == s[0]["post_url"]                   # the source record is still listed


def test_mixed_rows_keep_only_the_matched_support_in_a_finding():
    s = _sigs(BUYER_A, BUYER_B)
    out = _go(s, [_own(s[0]), _foreign(s[1])], findings=[_finding([1, 2], text="Posts 1 and 2 are ready to buy.")])
    f = out["key_findings"][0]
    assert f["supporting_post_indices"] == [1] and f["supporting_count"] == 1 and f["demand_signal"] is True
    assert "Post 2" not in f["impact"] and "Posts 1 and 2" not in f["impact"]


def test_unmatched_row_cannot_back_a_market_wide_claim():
    s = _sigs(BUYER_A, BUYER_B)
    out = _go(s, [_foreign(s[0]), _foreign(s[1])], findings=[_finding([1, 2], text="Posts 1 and 2 show strong demand.")])
    assert out["key_findings"] == [] and out["counts"]["qualified"] == out["counts"]["buyers"] == 0


def test_headline_with_zero_qualifying_rows_never_claims_buyers():
    s = _sigs(VENDOR)
    out = _go(s, [_own(s[0])], summary="There are 4 high-intent buyers here.", headline="Seven high-intent buyers found")
    assert out["counts"]["qualified"] == 0 and out["counts"]["buyers"] == 0 and out["counts"]["competitor"] == 1
    assert "4 high-intent" not in out["executive_summary"] and not out["headline"].lower().startswith("seven")
    assert _rows(out)[0]["role"] == "competitor_promo"                  # vendor promotion is not buyer demand


def test_two_matched_rows_state_two():
    s = _sigs(BUYER_A, BUYER_B)
    out = _go(s, [_own(s[0]), _own(s[1])], summary="We found seven qualified leads.")
    assert out["counts"]["qualified"] == 2 and out["executive_summary"] == "We found two qualified leads."


def test_vendor_with_a_matched_analysis_is_not_demand_in_a_finding():
    s = _sigs(VENDOR)
    out = _go(s, [_own(s[0])], findings=[_finding([1], text="Post 1 is ready to buy.")])
    assert out["counts"]["buyers"] == 0 and (not out["key_findings"] or out["key_findings"][0]["demand_signal"] is False)


def test_contract_fields_are_unchanged_and_additions_are_additive():
    s = _sigs(BUYER_A)
    out = _go(s, [_own(s[0])])
    assert set(out["counts"]) == {"retrieved", "unique", "analyzed", "listed", "excluded", "excluded_reasons", "shown",
                                  "relevant", "uncertain", "unverified", "verified", "buyers", "qualified", "researching",
                                  "competitor", "categories"}
    assert out["format"] == "source_list" and "verified_summary" in out and "excluded_posts" in out


# ── Phase 1 correction: one test per safeguard (each neutralises the OTHER layers, so it can only fail for its own rule) ──
def _S(word):
    """The server's own sentence for the qualifying count (used when a summary was emptied by claim removal)."""
    return f"Posts shown with a qualifying purchase step: {word}."


def _force(monkeypatch, **labels):
    """Simulates a regression in the labelling layer: an unmatched analysis comes back with these labels."""
    real = ai.classify_post

    def wrapped(sig, model_post, terms, icp, identity=None):
        out = real(sig, model_post, terms, icp, identity)
        return out if (out.get("_exclude") or identity is not None) else dict(out, **labels)
    monkeypatch.setattr(ai, "classify_post", wrapped)


BUYER_C = ("[synthetic] Looking for a CRM for tenants",
           "[synthetic] Looking for a CRM for our property management firm. The budget is approved by finance and we "
           "will subscribe this quarter.")


def _scenario(n):
    """(sigs, posts) with exactly n qualified rows: n=0 one vendor; n=1 matched buyer + unmatched buyer + vendor; n=2 two matched + vendor."""
    s = _sigs(BUYER_A, BUYER_C, VENDOR)
    if n == 0:
        return [s[2]], [_own(s[2])]
    if n == 1:
        return s, [_own(s[0]), _foreign(s[1]), _own(s[2])]
    return s, [_own(s[0]), _own(s[1]), _own(s[2])]


@pytest.mark.parametrize("key,labels,expected", [
    ("relevant", dict(relevance="relevant", role="buyer"), 1),
    ("buyers", dict(relevance="relevant", role="buyer"), 1),
    ("qualified", dict(relevance="relevant", role="buyer", qualified=True, intent="High"), 1),
    ("researching", dict(relevance="relevant", role="buyer", intent="Medium", qualified=False), 0),
    ("uncertain", dict(relevance="relevant", role="buyer"), 1),
])
def test_counts_use_only_analysis_matched_rows(monkeypatch, key, labels, expected):
    _force(monkeypatch, **labels)
    s = _sigs(BUYER_A, BUYER_C)
    out = _go(s, [_own(s[0]), _foreign(s[1])])
    assert out["counts"][key] == expected
    c = out["counts"]
    assert c["shown"] == c["relevant"] + c["uncertain"] + c["unverified"]


@pytest.mark.parametrize("evidence_type", ["Buyer", "Contextual"])
def test_a_finding_is_not_supported_by_an_unmatched_row(monkeypatch, evidence_type):
    _force(monkeypatch, relevance="relevant", role="buyer", qualified=True, intent="High")
    s = _sigs(BUYER_A, BUYER_C)
    f = {"finding": "Teams want a CRM", "evidence_type": evidence_type, "signal_strength": "Moderate",
         "supporting_post_indices": [1, 2], "impact": "Teams in two posts want a CRM."}
    out = _go(s, [_own(s[0]), _foreign(s[1])], findings=[f])
    assert out["key_findings"][0]["supporting_post_indices"] == [1]
    only_unmatched = dict(f, supporting_post_indices=[2])
    assert _go(s, [_own(s[0]), _foreign(s[1])], findings=[only_unmatched])["key_findings"] == []


def test_a_strong_finding_needs_a_qualified_supporting_row():
    s = _sigs(("[synthetic] Any good CRM?", "[synthetic] Can anyone recommend a CRM for our small team?"))
    out = _go(s, [_own(s[0])], findings=[_finding([1], strength="Strong", text="Post 1 asks for a CRM.")])
    row = _rows(out)[0]
    assert row["intent"] == "Medium" and not row["qualified"]
    assert out["key_findings"][0]["signal_strength"] != "Strong"


def test_unmatched_analysis_gets_unconfirmed_relevance():
    s = _sigs(BUYER_A)[0]
    terms = ai.product_terms(CRM_QUERY)
    assert ai.classify_post(s, {}, terms, False, identity=None)["relevance"] == "uncertain"
    assert ai.classify_post(s, {}, terms, False, identity="title")["relevance"] == "relevant"


@pytest.mark.parametrize("status,analysis,expected", [
    ("verified", True, True), ("verified", False, False), ("verified", None, False),
    ("unverified", True, False), ("removed", True, False), ("inaccessible", True, False)])
def test_only_a_retrieved_source_with_a_matched_analysis_is_evidence(status, analysis, expected):
    assert ai.is_evidence_row({"verification_status": status, "analysis_verified": analysis}) is expected


def test_source_and_analysis_verification_are_reported_separately():
    s = _sigs(BUYER_A, BUYER_C)
    out = _go(s, [_own(s[0]), _foreign(s[1])])
    assert out["analysis_check"] == {"source_verified": 2, "analysis_verified": 1, "analysis_unverified": 1}
    assert out["counts"]["verified"] == 2                                  # the existing key still means: source retrieved
    assert [p["verification_status"] for p in _rows(out)] == ["verified", "verified"]
    assert [p["analysis_status"] for p in _rows(out)] == ["matched", "unmatched"]
    assert "kept with a source link only" in out["verified_summary"]


# numeric evidence: a figure that appears only in an unmatched row supports nothing
FIGURE_POST = ("[synthetic] Looking for a CRM with a big budget",
               "[synthetic] Looking for a CRM. Our approved budget is $5000 per month and we will sign this week.")


@pytest.mark.parametrize("make_row,kept", [(_foreign, False), (_own, True)])
def test_a_figure_only_in_an_unmatched_row_cannot_support_a_claim(make_row, kept):
    s = _sigs(BUYER_A, FIGURE_POST)
    out = _go(s, [_own(s[0]), make_row(s[1])], summary="Buyers mention a $5000 budget.")
    assert ("$5000" in out["executive_summary"]) is kept
    assert _rows(out)[1]["link"] == s[1]["post_url"]                       # the source record is kept either way


def test_a_figure_that_no_row_states_is_still_dropped():
    s = _sigs(BUYER_A)
    out = _go(s, [_own(s[0])], summary="Buyers mention a $9000 budget.")
    assert "$9000" not in out["executive_summary"]


# claim reconciliation: exact expected sentences, zero / one / several qualifying rows
CLAIMS = [   # (claim, expected for 0 / 1 / 2 qualifying rows)
    ("Flintel found 7 high-intent buyers.", ("Flintel found no high-intent buyers.", _S("one"), "Flintel found two high-intent buyers.")),
    ("Seven hot leads were identified.", ("No hot leads were identified.", _S("one"), "Two hot leads were identified.")),
    ("7 buyers are high-intent.", (_S("none"), _S("one"), _S("two"))),
    ("High-intent buyers: 7.", (_S("none"), _S("one"), _S("two"))),
    ("Seven potential buyers are ready to purchase.", (_S("none"), _S("one"), _S("two"))),
    ("7 of the 10 posts are high-intent.", (_S("none"), _S("one"), _S("two"))),
    ("All 3 posts are qualified leads.", (_S("none"), _S("one"), _S("two"))),
    ("The top 3 high-intent buyers are listed below.", (_S("none"), _S("one"), _S("two"))),
    ("There are no high-intent buyers here.", ("There are no high-intent buyers here.", _S("one"), _S("two"))),
    ("Not seven qualified leads.", ("Not seven qualified leads.", _S("one"), _S("two"))),
    ("Several high-intent buyers appear.", ("No high-intent buyers appear.", _S("one"), "Several high-intent buyers appear.")),
    ("Qualified leads appear in r/CRM threads.", (_S("none"), "Qualified leads appear in r/CRM threads.",
                                                  "Qualified leads appear in r/CRM threads.")),     # no number: needs >= 1 row
    # unrelated narrative is never touched
    ("Buyers in 2026 prefer simple CRMs.", ("Buyers in 2026 prefer simple CRMs.",) * 3),
    ("We reviewed the last 3 days of posts.", ("We reviewed the last 3 days of posts.",) * 3),
]


@pytest.mark.parametrize("n", [0, 1, 2])
@pytest.mark.parametrize("claim,expected", CLAIMS, ids=[c[0] for c in CLAIMS])
def test_headline_claims_match_the_qualifying_rows(n, claim, expected):
    sigs, posts = _scenario(n)
    out = _go(sigs, posts, summary="", headline=claim)
    assert out["counts"]["qualified"] == n
    assert out["headline"] == expected[n]


@pytest.mark.parametrize("n,expected", [(0, "No hot leads were found."), (1, _S("one")), (2, "Two hot leads were found.")])
def test_claims_in_nested_fields_and_lists_are_reconciled(n, expected):
    sigs, posts = _scenario(n)
    out = _go(sigs, posts, summary="", overview={"summary": "Seven hot leads were found.", "note": "Keep this note."},
              highlights=["Seven hot leads were found.", "Keep this line."],
              sections=[{"title": "Demand", "body": "Seven hot leads were found.", "other": "Keep this too."}])
    assert out["overview"] == {"summary": expected, "note": "Keep this note."}
    kept = [] if n == 1 else [expected]                              # a list item is dropped, never replaced by new text
    assert out["highlights"] == kept + ["Keep this line."]
    assert out["sections"][0]["body"] == ("" if n == 1 else expected) and out["sections"][0]["other"] == "Keep this too."


def test_a_negated_claim_is_kept_only_when_it_is_true_and_never_rewritten():
    zero, two = _scenario(0), _scenario(2)
    claim = "There are no qualified leads among the vendor posts."
    assert _go(*zero, summary="", headline=claim)["headline"] == claim                 # true with 0 qualifying rows
    assert _go(*two, summary="", headline=claim)["headline"] == _S("two")             # false with 2: removed, not inverted
    assert _go(*zero, summary="", headline="Not seven qualified leads.")["headline"] == "Not seven qualified leads."
    assert "Not no" not in _go(*two, summary="", headline="Not seven qualified leads.")["headline"]


def test_a_finding_cannot_claim_more_buyers_than_its_own_supporting_rows():
    s = _sigs(BUYER_A, BUYER_C, VENDOR)
    posts = [_own(s[0]), _own(s[1]), _own(s[2])]

    def finding(indices, claim):
        return {"finding": "Teams want a CRM", "evidence_type": "Buyer", "signal_strength": "Moderate",
                "supporting_post_indices": indices, "impact": claim}
    out = _go(s, posts, findings=[finding([1], "Two high-intent buyers want a CRM."),
                                  finding([1, 2], "Seven high-intent buyers want a CRM.")])
    assert out["counts"]["qualified"] == 2                               # two qualify overall ...
    assert len(out["key_findings"]) == 1                                  # ... but the finding resting on ONE of them is removed
    f = out["key_findings"][0]
    assert f["supporting_post_indices"] == [1, 2] and f["impact"] == "Two high-intent buyers want a CRM."


def test_finding_title_text_is_reconciled_too():
    s = _sigs(BUYER_A, BUYER_C)
    f = {"finding": "Seven high-intent buyers want a CRM", "evidence_type": "Buyer", "signal_strength": "Moderate",
         "supporting_post_indices": [1, 2], "impact": "They ask for a CRM."}
    out = _go(s, [_own(s[0]), _own(s[1])], findings=[f])
    assert out["key_findings"][0]["finding"] == "Two high-intent buyers want a CRM"


def test_comparison_subjects_are_reconciled_against_their_own_rows():
    s = _sigs(BUYER_A, BUYER_C)
    posts = [dict(_own(s[0]), index=1), dict(_own(s[1]), index=2)]
    posts = [dict(p, title=s[i]["title"]) for i, p in enumerate(posts)]
    data = {"format": "comparison", "executive_summary": "",
            "subjects": [{"name": "A", "summary": "Seven hot leads were found.",
                          "platforms": [{"platform": "reddit", "total_analyzed": 1, "shown_count": 1, "posts": posts[:1]}]},
                         {"name": "B", "summary": "Seven hot leads were found.",
                          "platforms": [{"platform": "reddit", "total_analyzed": 1, "shown_count": 1, "posts": posts[1:]}]}]}
    out = ai.enforce_answer_integrity(data, s, s, CRM_QUERY)
    assert [sj["summary"] for sj in out["subjects"]] == [_S("one")] * 2


def test_no_results_output_is_compatible():
    s = _sigs(BUYER_A)
    out = _go(s, [_own(s[0], role="irrelevant")])
    assert out["format"] == "no_results" and "analysis_check" not in out and "verified_summary" not in out
    assert set(out["counts"]) == {"retrieved", "unique", "analyzed", "listed", "excluded", "excluded_reasons", "shown",
                                  "relevant", "uncertain", "unverified", "verified", "buyers", "qualified", "researching",
                                  "competitor", "categories"}


def test_non_report_answers_are_left_alone():
    plain = {"format": "plain", "executive_summary": "Seven hot leads were found."}
    assert ai.enforce_answer_integrity(dict(plain), [], [], CRM_QUERY) == plain


def test_a_summary_claim_is_corrected_before_the_generic_number_check_can_blank_it():
    # "7" is not a figure in any post, so the generic number check alone would blank the whole sentence
    for n, expected in ((1, _S("one")), (2, "Flintel found two high-intent buyers.")):
        sigs, posts = _scenario(n)
        assert _go(sigs, posts, summary="Flintel found 7 high-intent buyers.")["executive_summary"] == expected


def test_a_claim_citing_posts_is_checked_against_those_posts_only():
    sigs, posts = _scenario(2)                                    # two qualify overall; Post 3 is a vendor
    claim = "Two high-intent buyers (Posts 1 and 3) are listed."
    assert _go(sigs, posts, summary=claim)["executive_summary"] == _S("two")
    ok = "Two high-intent buyers (Posts 1 and 2) are listed."
    assert _go(sigs, posts, summary=ok)["executive_summary"] == ok


def test_all_N_claims_are_checked_but_a_plain_all_is_left_alone():
    s = _sigs(BUYER_A, BUYER_C)
    rows = [_own(s[0]), _own(s[1])]
    assert _go(s, rows, summary="", headline="All 2 posts are qualified leads.")["headline"] == "All two posts are qualified leads."
    assert _go(s, rows, summary="", headline="All 3 posts are qualified leads.")["headline"] == _S("two")
    sigs, posts = _scenario(2)                                    # three rows shown, two qualify
    assert _go(sigs, posts, summary="", headline="All 3 posts are qualified leads.")["headline"] == _S("two")
    plain = "All qualified leads are listed in the table."          # no number: a statement about the list, not a count
    assert _go(sigs, posts, summary="", headline=plain)["headline"] == plain


# ── final Phase 1 correction ──────────────────────────────────────────────────────────────────────────────────────────
# SYNTHETIC FIXTURES again: every post is invented and labelled "[synthetic]".
def _subject(name, posts, summary):
    return {"name": name, "summary": summary,
            "platforms": [{"platform": "reddit", "total_analyzed": len(posts), "shown_count": len(posts), "posts": posts}]}


def test_a_numeric_claim_in_a_comparison_subject_is_reconciled_before_the_number_check():
    # "7" is a figure no post contains, so the generic number check alone would blank this sentence
    s, rows = _many(3)
    p = [dict(r, index=i + 1, title=s[i]["title"]) for i, r in enumerate(rows)]
    data = {"format": "comparison", "executive_summary": "",
            "subjects": [_subject("A", p[:1], "We found 7 hot leads."), _subject("B", p[1:], "We found 7 hot leads.")]}
    out = ai.enforce_answer_integrity(data, s, s, CRM_QUERY)
    assert [sj["summary"] for sj in out["subjects"]] == [_S("one"), "We found two hot leads."]


def test_a_citing_claim_in_a_comparison_subject_is_checked_against_the_cited_rows():
    s, rows = _many(3)
    p = [dict(r, index=i + 1, title=s[i]["title"]) for i, r in enumerate(rows)]
    data = {"format": "comparison", "executive_summary": "",
            "subjects": [_subject("A", p[:1], "Seven hot leads (Post 1) were found."),
                         _subject("B", p[1:], "Seven hot leads (Posts 2 and 3) were found.")]}
    out = ai.enforce_answer_integrity(data, s, s, CRM_QUERY)
    assert [sj["summary"] for sj in out["subjects"]] == [_S("one"), "Two hot leads (Posts 2 and 3) were found."]


# counts of eleven or more: spelled in words, so they are neither blanked nor validated by a figure in some post
_NAMES = ("alpha", "bravo", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper", "kestrel", "lagoon")


def _many(count, unmatched_text=None):
    sigs = [sig_ for sig_ in _sigs(*[(f"[synthetic] Team {nm} needs a CRM",
                                      f"[synthetic] Looking for a CRM for team {nm}. The budget is approved by finance and we "
                                      "will subscribe this quarter.") for nm in _NAMES[:count]])]
    posts = [_own(x) for x in sigs]
    if unmatched_text:
        extra = _sig(count + 1, "[synthetic] Another team needs a CRM", unmatched_text, sub="CRM")
        sigs, posts = sigs + [extra], posts + [_foreign(extra)]
    return sigs, posts


@pytest.mark.parametrize("count,claim,expected", [
    (11, "We found 11 high-intent buyers.", "We found eleven high-intent buyers."),         # correct claim: preserved (in words)
    (11, "We found 7 high-intent buyers.", "We found eleven high-intent buyers."),          # wrong claim: corrected
    (12, "We found 12 hot leads.", "We found twelve hot leads."),
])
def test_claims_about_eleven_or_more_qualified_rows(count, claim, expected):
    sigs, posts = _many(count)
    out = _go(sigs, posts, summary=claim)
    assert out["counts"]["qualified"] == count and out["executive_summary"] == expected


def test_an_unmatched_row_cannot_validate_a_large_count():
    # the unmatched row's text contains "12": it must not make a wrong "12" claim look supported
    sigs, posts = _many(11, unmatched_text="[synthetic] Looking for a CRM for 12 agents. Budget approved, we will sign this week.")
    out = _go(sigs, posts, summary="We found 12 high-intent buyers.")
    assert out["counts"]["qualified"] == 11 and out["counts"]["shown"] == 12
    assert out["executive_summary"] == "We found eleven high-intent buyers."
    assert _rows(out)[-1]["analysis_status"] == "unmatched" and _rows(out)[-1]["link"] == _url(12)


def test_a_count_in_words_above_twelve_is_read_and_a_large_server_count_is_spelled():
    sigs, posts = _many(12)
    assert _go(sigs, posts, summary="We found fifteen hot leads.")["executive_summary"] == "We found twelve hot leads."
    assert ai._spell(21) == "twenty-one" and ai._spell(40) == "forty" and ai._spell(100) == "100"


# negation: only a negation right in front of the claim counts, and "no" only counts as zero directly in front of it
def test_an_unrelated_no_does_not_turn_a_claim_into_zero():
    text = "There is no doubt that hot leads were found. Keep this sentence."
    sigs, posts = _scenario(2)
    assert _go(sigs, posts, summary="", headline=text)["headline"] == text                  # read as a plain claim: two qualify
    sigs, posts = _scenario(0)
    assert _go(sigs, posts, summary="", headline=text)["headline"] == "Keep this sentence."   # no row qualifies: the claim goes


@pytest.mark.parametrize("n,expected", [
    (0, "I did not expect it, but no hot leads were found."),
    (2, "I did not expect it, but two hot leads were found."),
])
def test_an_unrelated_not_earlier_in_the_sentence_is_not_a_negated_claim(n, expected):
    sigs, posts = _scenario(n)
    assert _go(sigs, posts, summary="", headline="I did not expect it, but seven hot leads were found.")["headline"] == expected


def test_a_negation_directly_in_front_of_the_claim_still_counts():
    zero, two = _scenario(0), _scenario(2)
    claim = "The search does not show seven hot leads."
    assert _go(*zero, summary="", headline=claim)["headline"] == claim
    assert _go(*two, summary="", headline=claim)["headline"] == _S("two")


# aggregate claims about the share of posts that are high-intent: removed unless the shown rows support them
def _four(n_qualified):
    s = _sigs(BUYER_A, BUYER_C, VENDOR, ("[synthetic] CRM chat", "[synthetic] People talk about CRMs a lot these days."))
    rows = [_own(s[0]), _own(s[1]) if n_qualified >= 2 else _foreign(s[1]), _own(s[2]), _own(s[3], role="discussion")]
    return s, rows


AGGREGATES = [   # (claim, kept when 0 / 2 of 4 / 4 of 4 shown rows qualify)  -- "4 of 4" uses a separate all-qualified fixture
    ("Most posts are high-intent.", (False, False, True)),
    ("The majority of these posts are qualified leads.", (False, False, True)),
    ("Nearly all posts are qualified leads.", (False, False, True)),
    ("Half of the posts are high-intent.", (False, True, False)),
    ("All posts are qualified leads.", (False, False, True)),
]


@pytest.mark.parametrize("claim,kept", AGGREGATES, ids=[a[0] for a in AGGREGATES])
def test_share_of_posts_claims_need_the_shown_rows_to_support_them(claim, kept):
    s0, r0 = _scenario(0)                                        # one row shown, none qualifies
    out0 = _go(s0, r0, summary="", headline=claim)["headline"]
    assert out0 == (claim if kept[0] else _S("none"))
    s2, r2 = _four(2)                                            # four rows shown, two qualify (and one is unmatched-free)
    out2 = _go(s2, r2, summary="", headline=claim)
    assert out2["counts"]["qualified"] == 2 and out2["counts"]["shown"] == 4
    assert out2["headline"] == (claim if kept[1] else _S("two"))
    s4 = _sigs(BUYER_A, BUYER_C)                                  # two rows shown, both qualify
    out4 = _go(s4, [_own(s4[0]), _own(s4[1])], summary="", headline=claim)
    assert out4["counts"]["qualified"] == out4["counts"]["shown"] == 2
    assert out4["headline"] == (claim if kept[2] else _S("two"))


def test_a_share_claim_cannot_count_an_unmatched_row_as_qualified():
    s = _sigs(BUYER_A, BUYER_C)
    out = _go(s, [_own(s[0]), _foreign(s[1])], summary="", headline="All posts are qualified leads.")
    assert out["counts"]["qualified"] == 1 and out["counts"]["shown"] == 2
    assert out["headline"] == _S("one")                            # the unmatched row does not make it "all"


def test_a_plain_all_in_a_list_statement_and_unrelated_narrative_are_kept():
    for n in (1, 2):
        sigs, posts = _scenario(n)
        for text in ("All qualified leads are listed in the table.", "Some posts are high-intent.",
                     "Buyers in 2026 prefer simple CRMs.", "We reviewed the last 3 days of posts."):
            assert _go(sigs, posts, summary="", headline=text)["headline"] == text


# ── universal and "none of" claims about the shown posts (synthetic fixtures) ───────────────────────────────────────
UNIVERSAL = ["Every post here is a hot lead.", "Each post is a qualified lead.", "All of the posts are hot leads."]
NONE_OF = ["None of the posts are high-intent.", "Not one of the posts is a qualified lead.", "None of these posts are hot leads."]


def _all_qualified():
    s = _sigs(BUYER_A, BUYER_C)
    return s, [_own(s[0]), _own(s[1])]


@pytest.mark.parametrize("claim", UNIVERSAL)
def test_a_universal_claim_survives_only_when_every_shown_post_qualifies(claim):
    for n, expected in ((0, _S("none")), (1, _S("one")), (2, _S("two"))):          # 0 of 1, 1 of 3, 2 of 3 shown rows qualify
        sigs, posts = _scenario(n)
        assert _go(sigs, posts, summary="", headline=claim)["headline"] == expected, n
    sigs, posts = _all_qualified()                                                  # 2 of 2 shown rows qualify
    out = _go(sigs, posts, summary="", headline=claim)
    assert out["counts"]["qualified"] == out["counts"]["shown"] == 2 and out["headline"] == claim


@pytest.mark.parametrize("claim", NONE_OF)
def test_a_none_of_claim_survives_only_when_no_shown_post_qualifies(claim):
    sigs, posts = _scenario(0)
    assert _go(sigs, posts, summary="", headline=claim)["headline"] == claim       # true: nothing qualifies
    for n, expected in ((1, _S("one")), (2, _S("two"))):
        sigs, posts = _scenario(n)
        assert _go(sigs, posts, summary="", headline=claim)["headline"] == expected, n
    sigs, posts = _all_qualified()
    assert _go(sigs, posts, summary="", headline=claim)["headline"] == _S("two")


def test_a_universal_claim_in_nested_fields_and_findings_is_checked_too():
    sigs, posts = _scenario(2)                                                      # three rows shown, two qualify
    out = _go(sigs, posts, summary="Every post here is a hot lead. Pricing is the main concern.",
              overview={"summary": "None of the posts are high-intent.", "note": "Keep this note."},
              highlights=["Each post is a qualified lead.", "Keep this line."])
    assert out["executive_summary"] == "Pricing is the main concern."
    assert out["overview"] == {"summary": _S("two"), "note": "Keep this note."}
    assert out["highlights"] == ["Keep this line."]


def test_a_share_claim_cannot_count_an_unmatched_row_as_qualifying():
    s = _sigs(BUYER_A, BUYER_C)
    out = _go(s, [_own(s[0]), _foreign(s[1])], summary="", headline="Each post is a qualified lead.")
    assert out["counts"]["qualified"] == 1 and out["counts"]["shown"] == 2 and out["headline"] == _S("one")


def test_universal_and_none_wording_without_a_high_intent_term_is_left_alone():
    for n in (0, 1, 2):
        sigs, posts = _scenario(n)
        for text in ("Every post here mentions pricing.", "None of the posts mention a competitor.",
                     "Each post is from a different subreddit.", "Not one of the posts is older than a month."):
            assert _go(sigs, posts, summary="", headline=text)["headline"] == text
