"""Platform attribution: the platform of a result is what its URL host says, never a stored label or a model-written group name.
Reproduces the pattern seen in a real report (a Hacker News URL shown as Reddit, Show HN posts shown as X/Twitter) with synthetic records.
Source records here are invented for the tests; they do not reproduce the original report's posts."""
import json

import pytest

import answer_integrity as ai
import platforms

HN = "https://news.ycombinator.com/item?id={}"
RD = "https://www.reddit.com/r/SaaS/comments/{}/title/"


@pytest.mark.parametrize("record,expected", [
    ({"platform": "reddit", "post_url": HN.format(1)}, "hackernews"),             # stored label wrong, URL is Hacker News
    ({"platform": "x", "post_url": HN.format(2)}, "hackernews"),
    ({"platform": "twitter", "post_url": HN.format(3)}, "hackernews"),
    ({"platform": "hackernews", "post_url": RD.format("abc123")}, "reddit"),      # and the other way round
    ({"platform": "reddit", "post_url": RD.format("abc123")}, "reddit"),
    ({"platform": "x_twitter", "post_url": "https://x.com/u/status/1"}, "x_twitter"),
    ({"platform": "reddit", "post_url": "https://example.org/a"}, "reddit"),      # unknown host: the stored label is all there is
    ({"post_url": HN.format(4)}, "hackernews"),                                   # no label at all
    ({"platform": "weird"}, None),
])
def test_platform_of_a_record_follows_its_url_host(record, expected):
    assert platforms.of(record) == expected


def test_hacker_news_is_recognised_by_host_even_when_the_platform_is_not_enabled():
    platforms.apply_flags(hackernews=False)
    try:
        assert "hackernews" not in platforms.REGISTRY
        assert platforms.url_platform(HN.format(9)) == "hackernews"
        assert platforms.of({"platform": "reddit", "post_url": HN.format(9)}) == "hackernews"
    finally:
        platforms.apply_flags(hackernews=False)


def test_a_conflict_between_the_label_and_the_url_is_visible_not_silent():
    rec = {"platform": "reddit", "post_url": HN.format(5)}
    assert platforms.platform_conflict(rec) == ("reddit", "hackernews")
    out = platforms.normalize_signal(rec)
    assert out["platform"] == "hackernews" and out["raw_platform"] == "reddit" and out["platform_conflict"] == {"stored": "reddit", "from_url": "hackernews"}
    assert platforms.platform_conflict({"platform": "reddit", "post_url": RD.format("a1")}) is None
    assert "platform_conflict" not in platforms.normalize_signal({"platform": "reddit", "post_url": RD.format("a1")})


def test_integrity_platform_of_uses_the_host_and_keeps_x_naming():
    assert ai.platform_of({"platform": "reddit", "post_url": HN.format(6)}) == "hackernews"
    assert ai.platform_of({"platform": "twitter", "post_url": "https://twitter.com/u/status/2"}) == "x"
    assert ai.platform_of({"platform": "weird"}) == "weird" and ai.platform_of({}) == "unknown"


def _sig(url, title, text, platform):
    return {"title": title, "post_text": text, "post_url": url, "platform": platform, "subreddit": "SaaS" if platform == "reddit" else None}


def _answer(groups):
    return {"format": "source_list", "executive_summary": "Found posts.", "key_findings": [],
            "platforms": [{"platform": g, "total_analyzed": len(p), "shown_count": len(p), "posts": p} for g, p in groups]}


def test_posts_the_model_filed_under_the_wrong_platform_are_regrouped_by_their_real_platform():
    q = "Find people looking for an AI SaaS agent"
    hn1 = _sig(HN.format(11), "Ask HN: Which AI agent platform should we buy?", "We are looking for an AI agent platform for support. Any recommendations?", "reddit")
    hn2 = _sig(HN.format(12), "Show HN: My AI agent builder", "I built an AI agent builder and want feedback from users of agent tools.", "x")
    rd1 = _sig(RD.format("r1"), "Need an AI agent tool for my agency", "We are looking for an AI agent tool for our agency. Recommendations please?", "reddit")
    sigs = [hn1, hn2, rd1]
    posts = [{"index": i + 1, "link": s["post_url"], "title": s["title"], "role": "buyer", "purchase_intent": "explicit"} for i, s in enumerate(sigs)]
    out = ai.enforce_answer_integrity(_answer([("reddit", posts)]), sigs, sigs, q)
    groups = {g["platform"].lower(): [p["link"] for p in g["posts"]] for g in out["platforms"]}
    assert set(groups) == {"hackernews", "reddit"}, groups                      # no X group, no HN post under Reddit
    assert set(groups["hackernews"]) == {hn1["post_url"], hn2["post_url"]}
    assert groups["reddit"] == [rd1["post_url"]]
    for g in out["platforms"]:
        for p in g["posts"]:
            assert p["platform"] == ai.platform_of({"post_url": p["link"]})
            assert p["platform"] == g["platform"].lower()
    assert out["platform_corrections"] == 2                                       # visible, not silent
    assert out["counts"]["shown"] == sum(g["shown_count"] for g in out["platforms"]) == 3


def test_a_correctly_grouped_answer_is_left_exactly_as_it_was():
    s1 = _sig(RD.format("ok1"), "Looking for a CRM", "We are looking for a CRM for our team. Any recommendations?", "reddit")
    posts = [{"index": 1, "link": s1["post_url"], "title": s1["title"], "role": "buyer", "purchase_intent": "explicit"}]
    out = ai.enforce_answer_integrity(_answer([("Reddit", posts)]), [s1], [s1], "Find people looking for a CRM")
    assert [g["platform"] for g in out["platforms"]] == ["Reddit"] and "platform_corrections" not in out


# ── retrieval layer (logics): the pool a search is built from ─────────────────────────────────────────────────────────────────────────
from tests.test_mapreduce_identity import env, lg  # noqa: E402,F401


@pytest.mark.parametrize("stored,url,expected", [
    ("reddit", HN.format(1), "hackernews"), ("x", HN.format(2), "hackernews"), ("hackernews", RD.format("a1"), "reddit"),
    ("reddit", "https://x.com/u/status/1", "twitter"), ("reddit", "https://example.org/a", "reddit"), (None, "https://example.org/a", None),
    ("", HN.format(3), "hackernews"), ("linkedin", "", "linkedin"), (None, None, None)])
def test_retrieval_resolves_the_platform_from_the_url_host(lg, stored, url, expected):
    assert lg._resolve_platform(stored, url) == expected


def test_a_post_filed_under_the_wrong_platform_is_not_returned_as_a_reddit_post(lg):
    misfiled = {"title": "Ask HN: x", "post_text": "body", "post_url": HN.format(21), "platform": "reddit"}
    genuine = {"title": "r/SaaS post", "post_text": "body", "post_url": RD.format("g1"), "platform": "reddit"}
    assert lg._signal_platform_matches(genuine, "reddit") is True
    assert lg._signal_platform_matches(misfiled, "reddit") is False
