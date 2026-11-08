"""Issue 2 (result completeness) and Issue 4 (evidence and quotations). Every retrieved post is accounted for; two relevant threads are two rows (or an
explicit exclusion with a reason); the quote shown as a quotation is always the original post's own words. Synthetic records; no model."""
import json

import pytest

import answer_integrity as ai

HN = "https://news.ycombinator.com/item?id={}"
Q = "Find people looking for AI development and automation services"


def post(i, title, text, platform="hackernews"):
    return {"title": title, "post_text": text, "post_url": HN.format(i), "platform": platform, "author": f"u{i}"}


A = post(1, "Ask HN: Who can build AI agents for our ops team?", "We are looking for an AI development agency to automate our operations. Budget is $8k a month. Any recommendations?")
B = post(2, "Looking for automation help for our logistics workflows", "Our team needs an automation partner to build AI workflows for logistics. Can anyone recommend a vendor?")
C = post(3, "Show HN: Flowbot, AI automation platform", "I built Flowbot, an AI automation SaaS. Free trial, feedback welcome.")
D = post(4, "Pizza dough hydration", "How much water should I use for 00 flour pizza dough at home?")


def listed(*ps, summary="x"):
    return {"format": "source_list", "executive_summary": summary, "key_findings": [],
            "platforms": [{"platform": "hackernews", "posts": [{"index": i + 1, "link": p["post_url"], "title": p["title"], "role": "buyer",
                                                                "purchase_intent": "explicit"} for i, p in enumerate(ps)]}]}


def run(pool, *listed_posts, extra_posts=None):
    data = listed(*listed_posts)
    if extra_posts:
        data["platforms"][0]["posts"] += extra_posts
    return ai.enforce_answer_integrity(data, pool, pool, Q)


def rows(out):
    return [p for g in out.get("platforms", []) for p in g["posts"]]


# ── Issue 2 ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_two_relevant_threads_listed_are_two_rows():
    out = run([A, B, C, D], A, B)
    assert {r["link"] for r in rows(out)} == {A["post_url"], B["post_url"]} and len(rows(out)) == 2       # (rows are ordered by strength, not by listing order)
    assert out["counts"]["shown"] == 2 and out["reconciliation"]["shown"] == 2 and out["reconciliation"]["listed_balanced"]


def test_a_relevant_thread_the_writer_did_not_list_is_named_not_dropped():
    """The reported failure: two threads discussed, one row displayed. The second is retrieved, checked, and shown in not_listed with its URL."""
    out = run([A, B, C, D], A)                                                  # the writer listed only A
    assert [r["link"] for r in rows(out)] == [A["post_url"]]
    nl = out["not_listed"]
    assert [x["link"] for x in nl] == [B["post_url"]] and nl[0]["title"] == B["title"] and nl[0]["platform"] == "hackernews"
    assert "not listed by the report writer" in nl[0]["reason"]
    assert C["post_url"] not in {x["link"] for x in nl} and D["post_url"] not in {x["link"] for x in nl}   # promotion / off-topic are not 'missing buyers'
    rec = out["reconciliation"]
    assert rec["retrieved"] == 4 and rec["shown"] == 1 and rec["not_listed"] == 3 and rec["not_listed_buyer_like"] == 1 and rec["pool_balanced"]


def test_nothing_is_invented_for_a_post_that_was_not_retrieved():
    ghost = {"index": 9, "link": HN.format(999), "title": "A thread that was never retrieved", "role": "buyer"}
    out = run([A, B], A, extra_posts=[ghost])
    assert [r["link"] for r in rows(out)] == [A["post_url"]]
    ex = [e for e in out["excluded_posts"] if e["reason"] == "unverifiable"]
    assert len(ex) == 1 and ex[0]["link"] == ghost["link"] and ex[0]["title_source"] == "model" and out["counts"]["excluded_reasons"]["unverifiable"] == 1
    assert out["reconciliation"]["listed_balanced"] and out["reconciliation"]["listed_by_writer"] == 2


def test_an_excluded_post_carries_its_title_platform_and_reason():
    out = run([A, D], A, D)                                                      # the writer listed an off-topic post too
    ex = out["excluded_posts"]
    assert [e["link"] for e in ex] == [D["post_url"]] and ex[0]["reason"] == "not relevant" and ex[0]["title"] == D["title"] and ex[0]["platform"] == "hackernews"


def test_the_same_post_listed_twice_is_one_row_and_one_duplicate():
    out = run([A, B], A, A)
    assert [r["link"] for r in rows(out)] == [A["post_url"]]
    assert out["counts"]["excluded_reasons"]["duplicate"] == 1 and out["reconciliation"]["listed_balanced"]
    assert out["excluded_posts"][0]["reason"] == "duplicate" and out["excluded_posts"][0]["title"] == A["title"]


@pytest.mark.parametrize("variant", ["http://news.ycombinator.com/item?id=1", "https://news.ycombinator.com/item?id=1&utm_source=x", "https://news.ycombinator.com/item?id=1#c"])
def test_the_writers_spelling_of_a_url_still_finds_the_stored_post(variant):
    data = listed(A)
    data["platforms"][0]["posts"][0]["link"] = variant
    out = ai.enforce_answer_integrity(data, [A, B], [A, B], Q)
    assert [r["link"] for r in rows(out)] == [A["post_url"]]                      # displayed with the STORED url
    assert out["counts"]["excluded"] == 0


@pytest.mark.parametrize("scenario", [([A, B, C, D], [A, B]), ([A, B, C, D], [A]), ([A, B, C, D], [C, D]), ([A, B, C, D], []), ([A, B], [A, A, B])])
def test_every_retrieved_post_is_accounted_for(scenario):
    pool, lst = scenario
    out = run(pool, *lst)
    rec = out["reconciliation"]
    assert rec["listed_balanced"] and rec["pool_balanced"], rec
    assert rec["listed_by_writer"] == rec["shown"] + rec["excluded"]
    assert rec["retrieved"] == len({p["post_url"] for p in pool})


def test_when_nothing_is_shown_the_no_results_answer_still_reconciles():
    out = run([D], D)
    assert out["format"] == "no_results" and out["reconciliation"]["excluded"] == 1 and out["excluded_posts"][0]["link"] == D["post_url"]


# ── Issue 4: quotations ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def with_quote(p, quote):
    data = listed(p)
    data["platforms"][0]["posts"][0]["evidence"] = quote
    return rows(ai.enforce_answer_integrity(data, [p, B], [p, B], Q))[0]


def test_locate_quote_returns_the_sources_own_characters():
    text = "We are looking for an AI development agency to automate our operations. Budget is $8k a month."
    assert ai.locate_quote("budget is $8k a MONTH", "t", text) == "Budget is $8k a month"
    assert ai.locate_quote("we are looking for an ai development agency", "t", text) == "We are looking for an AI development agency"
    assert ai.locate_quote("a different sentence entirely that is absent", "t", text) is None
    assert ai.locate_quote("too short", "t", text) is None and ai.locate_quote("", "t", text) is None


def test_a_verified_model_quote_is_shown_as_the_original_text_and_marked_exact():
    r = with_quote(A, "budget is $8K A MONTH.")                                   # case / punctuation differ from the source
    assert r["evidence_kind"] == "exact_quote" and r["evidence"] == "Budget is $8k a month"
    assert r["evidence"] in A["post_text"]                                        # a verbatim slice of the stored post


def test_a_fabricated_quote_is_never_shown_as_a_quote():
    r = with_quote(A, "We will sign a $50k annual contract today")
    assert r["evidence_kind"] == "source_excerpt" and "50k" not in r["evidence"]
    assert (r["evidence"] in A["post_text"]) or (r["evidence"] in A["title"])
    assert any("not found in the original post" in u and "excerpt from the post" in u for u in r["uncertainty"])


def test_no_model_quote_means_a_labelled_source_excerpt():
    r = with_quote(A, None)
    assert r["evidence_kind"] == "source_excerpt" and r["evidence_source"] == "excerpt"


def test_an_ai_summary_is_never_placed_in_the_evidence_field():
    data = listed(A)
    data["platforms"][0]["posts"][0].update(summary="The author wants an agency to automate their operations.", evidence="Our whole team loves the idea")
    r = rows(ai.enforce_answer_integrity(data, [A, B], [A, B], Q))[0]
    assert r["evidence"] != r["summary"] and r["evidence_kind"] == "source_excerpt"
    assert all(r["evidence"] in (A["post_text"] + " " + A["title"]) for _ in [0])


def test_every_displayed_row_has_an_evidence_kind_that_matches_where_its_text_came_from():
    out = run([A, B, C], A, B, C)
    for r in rows(out):
        assert r["evidence_kind"] in ("exact_quote", "source_excerpt")
        src = next(p for p in (A, B, C) if p["post_url"] == r["link"])
        assert ai.quote_in_post(r["evidence"], src["title"], src["post_text"])


# ── malformed source tables from the writer ───────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("platforms_value", [None, [], {}, "text", [None], [{"platform": "hackernews"}], [{"platform": "hackernews", "posts": None}],
                                             [{"platform": "hackernews", "posts": "nope"}], [{"platform": "hackernews", "posts": [None, 5, "x", {}]}]])
def test_malformed_platform_tables_never_crash_and_show_nothing_invented(platforms_value):
    data = {"format": "source_list", "executive_summary": "x", "key_findings": [], "platforms": platforms_value}
    out = ai.enforce_answer_integrity(data, [A, B], [A, B], Q)
    assert isinstance(out, dict)
    shown = [p for g in (out.get("platforms") or []) if isinstance(g, dict) for p in (g.get("posts") or []) if isinstance(p, dict)] \
        if isinstance(out.get("platforms"), list) else []
    assert shown == []                                                         # no row can be produced from a malformed table


def test_a_post_with_no_link_or_a_wrong_type_link_is_excluded_not_displayed():
    data = {"format": "source_list", "executive_summary": "x", "key_findings": [], "platforms": [{"platform": "hackernews", "posts": [
        {"index": 1, "title": "no link at all"}, {"index": 2, "link": 12345, "title": "numeric link"}, {"index": 3, "link": A["post_url"], "title": A["title"], "role": "buyer"}]}]}
    out = ai.enforce_answer_integrity(data, [A, B], [A, B], Q)
    assert [r["link"] for r in rows(out)] == [A["post_url"]]
    assert out["counts"]["excluded_reasons"]["unverifiable"] == 2 and out["reconciliation"]["listed_balanced"]


# ── all categories at once: retrieved = displayed + excluded (retrieved) + retrieved-but-not-listed, every post exactly once ───────────────
def test_every_category_at_once_adds_up_and_each_post_is_counted_exactly_once():
    buyer1 = A                                                                   # listed, shown
    buyer2 = B                                                                   # listed, shown
    promo = C                                                                    # listed, shown (as a competitor row)
    offtopic = D                                                                 # listed, excluded as not relevant
    missed = post(5, "We need an AI automation vendor for warehouse work", "We need an automation vendor for our warehouse. Please recommend one.")   # not listed, buyer-like
    chatter = post(6, "Weekend project: a tiny RSS reader", "Spent the weekend writing a small RSS reader in Rust. It was fun.")                           # not listed, other
    pool = [buyer1, buyer2, promo, offtopic, missed, chatter]
    data = listed(buyer1, buyer2, promo, offtopic)
    data["platforms"][0]["posts"] += [
        {"index": 5, "link": buyer1["post_url"], "title": buyer1["title"], "role": "buyer"},                      # the same post listed again: a duplicate LISTING
        {"index": 6, "link": HN.format(404), "title": "A thread that was never retrieved", "role": "buyer"}]       # unverifiable
    out = ai.enforce_answer_integrity(data, pool, pool, Q)
    rec = out["reconciliation"]
    shown = {r["link"] for r in rows(out)}
    excluded_ret = {e["link"] for e in out["excluded_posts"] if e["reason"] != "unverifiable"} - shown
    not_listed_all = {p["post_url"] for p in pool} - {p["post_url"] for p in (buyer1, buyer2, promo, offtopic)}
    # every retrieved post is in exactly one bucket
    assert shown == {buyer1["post_url"], buyer2["post_url"], promo["post_url"]}
    assert excluded_ret == {offtopic["post_url"]}
    assert not_listed_all == {missed["post_url"], chatter["post_url"]}
    assert shown.isdisjoint(excluded_ret) and shown.isdisjoint(not_listed_all) and excluded_ret.isdisjoint(not_listed_all)
    # the arithmetic: displayed + excluded(retrieved) + retrieved-but-not-listed == retrieved
    assert rec["retrieved"] == 6 == rec["shown"] + rec["excluded_retrieved"] + rec["not_listed"] == 3 + 1 + 2
    assert rec["pool_balanced"] is True
    # duplicates: a post listed twice is ONE retrieved post (shown once); the second listing is a duplicate listing, not a second post
    assert rec["duplicate_listings"] == 1 and rec["unverifiable_listings"] == 1
    assert rec["listed_by_writer"] == 6 == rec["shown"] + rec["excluded"] and rec["excluded"] == 3 and rec["listed_balanced"] is True
    assert out["counts"]["excluded_reasons"] == {"irrelevant": 1, "duplicate": 1, "unverifiable": 1}
    # the buyer-like missed post is named; the chatter is counted but not presented as a missing buyer
    assert rec["not_listed_buyer_like"] == 1 and [x["link"] for x in out["not_listed"]] == [missed["post_url"]]
    # every excluded entry says why, and the unverifiable one is marked as a model-written title
    reasons = {e["link"]: e["reason"] for e in out["excluded_posts"]}
    assert reasons[offtopic["post_url"]] == "not relevant" and reasons[HN.format(404)] == "unverifiable"
