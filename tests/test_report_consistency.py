"""One report, one set of numbers: the checked answer (answer_integrity), the stored-result review (annotate_results) and the signal report
(signal_intel) must agree about which retrieved post is where, and the intent score must describe the AUTHOR's intent, not how well a
pattern matched. Source records are synthetic."""
import json

import pytest

import answer_integrity as ai
import signal_intel as si

HN = "https://news.ycombinator.com/item?id={}"
RD = "https://www.reddit.com/r/SaaS/comments/{}/t/"
Q = "Find people looking for an AI SaaS agent"


def _post(url, title, text, platform):
    return {"title": title, "post_text": text, "post_url": url, "platform": platform, "author": "someone"}


BUYER = _post(HN.format(1), "Ask HN: Which AI agent platform should we buy?", "We are looking for an AI agent platform for our support team. Any recommendations? Budget is $300/month.", "hackernews")
SHOW = _post(HN.format(2), "Show HN: AgentDesk - an AI SaaS agent for support", "Hi HN, I built AgentDesk, an AI agent SaaS for support teams. Free trial, feedback welcome.", "hackernews")
CHAT = _post(RD.format("c3"), "Do you run always-on AI agents?", "I am curious how people run always-on agents. What does your setup look like?", "reddit")
EMPTY = _post(RD.format("e4"), "[deleted]", "", "reddit")


def _checked(posts, listed):
    data = {"format": "source_list", "executive_summary": "x", "key_findings": [],
            "platforms": [{"platform": "hackernews", "posts": [{"index": i + 1, "link": p["post_url"], "title": p["title"], "role": "buyer", "purchase_intent": "explicit"}
                                                               for i, p in enumerate(listed)]}]}
    out = ai.enforce_answer_integrity(data, posts, posts, Q)
    return out, ai.annotate_results(posts, json.dumps(out))


def test_signal_report_accounts_for_every_retrieved_post_and_agrees_with_the_checked_answer():
    posts = [BUYER, SHOW, CHAT, EMPTY]
    answer, stored = _checked(posts, listed=[BUYER, SHOW])
    rep = si.build_signal_report(stored, Q, "topic", None)
    a = rep["accounting"]
    assert a["retrieved"] == rep["returned"] == len(posts) == 4
    assert a["in_report"] + a["excluded"] + a["not_selected"] + a["not_analyzed"] + a["unreviewed"] == a["retrieved"]
    assert a["in_report"] == answer["counts"]["shown"]                         # the SAME number the checked report shows
    assert a["unreviewed"] == 0 and a["not_selected"] >= 1 and a["not_analyzed"] == 1
    by_url = {it["url"]: it for it in rep["items"]}
    for url, review in ((BUYER["post_url"], "in_report"), (CHAT["post_url"], "not_selected"), (EMPTY["post_url"], "not_analyzed")):
        assert by_url[url]["review"]["status"] == review
    assert isinstance(by_url[BUYER["post_url"]]["review"].get("index"), int)
    assert "appear in the checked report" in rep["statement"] or "appears in the checked report" in rep["statement"]


def test_classified_count_never_exceeds_retrieved_and_the_cut_is_stated():
    posts = [dict(CHAT, post_url=RD.format(f"x{i}"), title=f"General chat {i}") for i in range(30)]
    rep = si.build_signal_report(posts, Q, "topic", None, max_items=25)
    a = rep["accounting"]
    assert a["retrieved"] == 30 and a["classified"] == 25 and a["not_classified"] == 5 == rep["returned"] - rep["classified"]
    assert "not classified" in rep["statement"]
    assert sum(rep["breakdown"].values()) == rep["classified"]


def test_without_a_checked_report_there_is_no_invented_review():
    rep = si.build_signal_report([BUYER, CHAT], Q, "topic", None)
    assert rep["accounting"]["reviewed"] == 0 and all("review" not in it for it in rep["items"])
    assert "checked report" not in rep["statement"]


def test_intent_strength_describes_the_authors_intent_not_pattern_strength():
    demand = si.classify_post(BUYER, Q)
    promo = si.classify_post(SHOW, Q)
    chat = si.classify_post(CHAT, Q)
    assert demand["class"] == "DIRECT_DEMAND" and demand["scores"]["intent_strength"] >= 0.7
    assert promo["class"] == "COMPETITOR_SIGNAL" and promo["scores"]["intent_strength"] == 0.0       # a vendor announcement is not buyer intent
    assert chat["class"] == "GENERAL_DISCUSSION" and chat["scores"]["intent_strength"] == 0.0
    problem = si.classify_post(_post(HN.format(7), "Our CRM keeps failing", "We are struggling: our CRM keeps breaking and we lose deals. Any advice?", "hackernews"), "CRM")
    assert problem["class"] == "PROBLEM_SIGNAL" and 0 < problem["scores"]["intent_strength"] <= 0.5


def test_a_promotion_is_never_reported_as_buyer_demand_or_recommended_for_contact():
    c = si.classify_post(SHOW, Q)
    assert c["class"] != "DIRECT_DEMAND" and any("not evidence of a buyer" in x for x in c["limitations"])
    assert si.extract_opportunity(SHOW, c) is None                                                    # no 'opportunity' record for a vendor post


def test_option_seeking_is_solution_seeking_not_a_purchase_decision():
    c = si.classify_post(_post(HN.format(8), "Ask HN: Alternatives to Zapier?", "Can anyone recommend alternatives to Zapier for our small team's automation?", "hackernews"), "Zapier")
    assert c["class"] == "DIRECT_DEMAND" and any("solution-seeking, not a stated purchase decision" in x for x in c["limitations"])


def test_item_platform_comes_from_the_url_not_the_stored_label():
    wrong = dict(BUYER, platform="reddit")
    c = si.classify_post(wrong, Q)
    assert c["platform"] == "hackernews"


def test_topic_relevance_counts_the_words_that_name_the_topic():
    """"ai" and "agent" used to be stop words, so a query about AI agents scored an on-topic post 0.0 and an unrelated 'SaaS' mention higher."""
    q = "Find people looking for AI SaaS Agent"
    on_topic = si.topic_relevance(q, "Looking for an AI agent to automate support")
    saas_only = si.topic_relevance(q, "We run a SaaS company selling invoicing software.")
    off = si.topic_relevance(q, "Best pizza recipe in town")
    assert on_topic > saas_only > off == 0.0
    assert si.topic_relevance(q, "AI SaaS agent platforms comparison") >= 0.75
    assert 0 < si._use_case_relevance(q, "Looking for an AI agent to automate support") < 1       # partial credit for "AI ... agent"
    assert si._use_case_relevance(q, "Best pizza recipe in town") == 0.0


TESTIMONIAL = _post(HN.format(31), "Our AI agent tool saved us a fortune", "We are looking for more people to try the AI agent tool we love. Testimonial: budget was $500 per month and it paid for itself. Great support agent tool.", "hackernews")


def test_a_post_the_checked_report_calls_a_vendor_or_testimonial_is_never_direct_demand_in_the_signal_report():
    answer, stored = _checked([BUYER, TESTIMONIAL], listed=[BUYER, TESTIMONIAL])
    roles = {p["link"]: p["role"] for g in answer["platforms"] for p in g["posts"]}
    rep = si.build_signal_report(stored, Q, "topic", None)
    for it in rep["items"]:
        if roles.get(it["url"]) in ("competitor_promo", "testimonial"):
            assert it["class"] != "DIRECT_DEMAND", (it["title"], roles)
    assert sum(rep["breakdown"].values()) == rep["classified"]


def test_reconciliation_downgrades_and_says_why():
    p = dict(TESTIMONIAL, review={"status": "in_report", "index": 1}, report_role="testimonial")
    item = si.build_signal_report([p], Q, "topic", None)["items"][0]
    assert item["class"] == "COMPETITOR_SIGNAL" and item["scores"]["intent_strength"] == 0.0 and item.get("reconciled") is True
    assert any("vendor or testimonial" in x for x in item["limitations"])
    buyer = dict(BUYER, review={"status": "in_report", "index": 1}, report_role="buyer")
    assert si.build_signal_report([buyer], Q, "topic", None)["items"][0]["class"] == "DIRECT_DEMAND"        # a buyer row is untouched


# ── source URL / title integrity, and empty or partial reports ─────────────────────────────────────────────────────────────────────────
def test_each_displayed_row_keeps_the_stored_url_and_title_not_the_models():
    posts = [BUYER, SHOW]
    data = {"format": "source_list", "executive_summary": "x", "key_findings": [],
            "platforms": [{"platform": "hackernews", "posts": [
                {"index": 1, "link": BUYER["post_url"], "title": "A DIFFERENT TITLE THE MODEL INVENTED", "role": "buyer", "purchase_intent": "explicit"},
                {"index": 2, "link": "https://news.ycombinator.com/item?id=999999", "title": "A post that was never retrieved", "role": "buyer"}]}]}
    out = ai.enforce_answer_integrity(data, posts, posts, Q)
    rows = [p for g in out["platforms"] for p in g["posts"]]
    assert [r["link"] for r in rows] == [BUYER["post_url"]]                      # the invented link is dropped, never shown
    assert out["counts"]["excluded_reasons"]["unverifiable"] == 1 and out["counts"]["listed"] == 2 and out["counts"]["shown"] == 1
    assert rows[0]["platform"] == "hackernews" and rows[0]["source_id"]          # attribution and identity come from the stored record
    stored_titles = {p["title"] for p in posts}
    assert {si_["title"] for si_ in si.build_signal_report(ai.annotate_results(posts, json.dumps(out)), Q, "topic", None)["items"]} == stored_titles


def test_when_every_listed_post_is_excluded_the_report_says_so_and_counts_still_add_up():
    off = _post(RD.format("o1"), "Pizza dough recipe", "How to make pizza dough with flour, water and yeast at home.", "reddit")
    data = {"format": "source_list", "executive_summary": "I found 1 relevant post.", "key_findings": [],
            "platforms": [{"platform": "reddit", "posts": [{"index": 1, "link": off["post_url"], "title": off["title"], "role": "buyer", "purchase_intent": "explicit"}]}]}
    out = ai.enforce_answer_integrity(data, [off], [off], Q)
    assert out["counts"]["shown"] == 0 and out["counts"]["listed"] == 1 and out["counts"]["excluded"] == 1
    assert not [p for g in out.get("platforms", []) for p in g.get("posts", [])]
    rep = si.build_signal_report(ai.annotate_results([off], json.dumps(out)), Q, "topic", None)
    assert rep["accounting"]["in_report"] == 0 and rep["accounting"]["excluded"] == 1 and rep["accounting"]["retrieved"] == 1


def test_no_retrieved_posts_produce_no_signal_report_items():
    rep = si.build_signal_report([], Q, "topic", None)
    assert rep["items"] == [] and rep["returned"] == 0 and rep["accounting"]["retrieved"] == 0
