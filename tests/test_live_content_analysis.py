"""
tests/test_live_content_analysis.py — the Live Reddit search analyses the posts' CONTENT, and every analysis is tied to the exact post it
came from. Mocked end to end: a fake Responses reply (as the web_search tool returns it: sources + url_citation annotations + the model's
final text), a fake Reddit metadata endpoint that returns each post's REAL title and text, no network. What this cannot show: how good the
real model's reading of a real post is.
"""
import copy
import json

import pytest

mongomock = pytest.importorskip("mongomock")
import reddit_live as rl  # noqa: E402
from tests.test_active_intent import (HURL, URL2, ai_on, answer_json, cls_resp, hrow, live_env, prepare, resp, stored_msg,  # noqa: F401,E402
                                      _no_network, _no_inflight_leak, _reset_factory)
from tests.test_reddit_live import URL1, on  # noqa: F401,E402

A_URL = "https://www.reddit.com/r/SaaS/comments/abc123/looking_for_an_ai_agent_builder/"        # == URL1's post id abc123
B_URL = "https://www.reddit.com/r/smallbusiness/comments/def456/which_support_bot/"              # == URL2's post id def456
A_TITLE, B_TITLE = "Looking for an AI agent builder", "Which support bot should we use?"
A_BODY = "Our store gets 200 tickets a day and we need someone to build an AI agent that answers the repetitive ones."
B_BODY = "We tried two support bots last quarter and both were frustrating. Anyone found one that actually works well?"


def entry(url, quote, summary="s", sentiment="neutral", relevance="r", title=None):
    return {"url": url, "title": title or "t", "quote": quote, "summary": summary, "sentiment": sentiment, "relevance": relevance}


def model_text(posts, **extra):
    return json.dumps(dict({"posts": posts, "main_finding": "People want help automating support.", "themes": [], "implications": []}, **extra))


def with_text(response, text):
    r = copy.deepcopy(response)
    for item in r["output"]:
        if item.get("type") == "message":
            item["content"][0]["text"] = text
    return r


def two_post_env(text, **meta):
    env = live_env(items=[(A_URL, A_TITLE), (B_URL, B_TITLE)])
    env.bodies = {"abc123": A_BODY, "def456": B_BODY}
    env.response = with_text(env.response, text)
    return env


def run_live(env):
    run = prepare(env)
    run.start()
    out = run.finish(answer_json(HURL), historical=[hrow(1, url=HURL)])
    return run, json.loads(out)


# ── what the API actually returns, and what the parser reads ──────────────────────────────────────────────────────────────────
def test_the_parser_reads_urls_from_sources_and_citations_only_and_never_keeps_the_models_prose():
    data = with_text(resp([(A_URL, A_TITLE)]), "free text with https://www.reddit.com/r/Evil/comments/zzz999/injected/ in it")
    links, calls, sources = rl.parse_response(data, 8)
    assert [l["url"] for l in links] == [A_URL] and calls == 1 and sources == 1


@pytest.mark.parametrize("url", ["https://www.reddit.com/r/SaaS/", "https://www.reddit.com/search/?q=ai+agent",
                                 "https://www.reddit.com/r/SaaS/s/AbCdEf", "https://i.redd.it/abc.jpg", "https://example.com/r/SaaS/comments/abc123/x/",
                                 "http://www.reddit.com/r/SaaS/comments/abc123/x/", "https://www.reddit.com/user/someone"])
def test_a_200_with_only_non_post_urls_is_not_a_successful_discovery(ai_on, url):
    env = live_env(items=[(url, "t")])
    run = prepare(env)
    run.start()
    out = json.loads(run.finish(answer_json(HURL), historical=[hrow(1, url=HURL)]))
    assert "live_candidates" not in out and env.meta_calls == []                  # nothing to verify, nothing shown
    assert env.chats.find_one({"chat_id": "c1"})["messages"][0]["live_reddit"]["state"] == rl.STATE_EMPTY


# ── the request asks for a reading of each post, not just links ─────────────────────────────────────────────────────────────────
def test_the_workflow_request_asks_for_a_json_reading_with_a_verbatim_quote_and_has_room_for_it():
    win = ("2026-11-01T11:30:00Z", "2026-11-01T12:00:00Z")
    p = rl.build_payload("q", win, {"query": "q", "keywords": ["a"], "mode": "demand"})
    ins = p["instructions"]
    assert "Reply with one short plain sentence" not in ins and "ONLY one JSON object" in ins
    for needle in ('"quote"', "copied word for word", '"summary"', '"sentiment"', '"themes"', "at most 3", "leave a post out rather than guess"):
        assert needle in ins
    assert p["max_output_tokens"] >= rl.WORKFLOW_MIN_OUTPUT_TOKENS and p["max_tool_calls"] == 1
    plain = rl.build_payload("q")
    assert "Reply with one short plain sentence" in plain["instructions"] and plain["max_output_tokens"] == 2000     # the ordinary request: unchanged


# ── real content retained, analysis tied to the right post ────────────────────────────────────────────────────────────────────
def test_the_real_title_text_subreddit_and_date_are_kept_and_the_analysis_is_attached_to_its_own_post(ai_on):
    text = model_text([entry(B_URL, "both were frustrating", "Frustrated with two support bots.", "negative", "Wants a working bot."),
                       entry(A_URL, "need someone to build an AI agent", "Store needs an AI agent for repetitive tickets.", "neutral", "Wants a builder.")])
    run, out = run_live(two_post_env(text))                                          # the model lists them in the OPPOSITE order
    items = {i["url"]: i for i in out["live_candidates"]["items"]}
    a, b = items[A_URL], items[B_URL]
    assert a["title"] == A_TITLE and a["body"] == A_BODY and a["subreddit"] == "SaaS" and a["published_at"] and a["date_verified"] is True
    assert b["title"] == B_TITLE and b["body"] == B_BODY and b["subreddit"] == "smallbusiness"
    assert a["analysis"]["summary"].startswith("Store needs") and a["analysis"]["sentiment"] == "neutral"            # A's reading is on A
    assert b["analysis"]["summary"].startswith("Frustrated") and b["analysis"]["sentiment"] == "negative"           # B's reading is on B
    assert a["content_status"] == b["content_status"] == "analysed" and a["analysis"]["quote"] in A_BODY
    assert a["discovered_by"] and "ai agent developer" not in json.dumps(a["analysis"])


def test_an_analysis_is_matched_by_post_id_never_by_position_title_or_neighbour(ai_on):
    # the model gives A's quote under B's URL and B's quote under A's URL: neither quote is in the post it was attached to
    text = model_text([entry(B_URL, "need someone to build an AI agent", "wrong"), entry(A_URL, "both were frustrating", "wrong")])
    run, out = run_live(two_post_env(text))
    assert all(i["analysis"] is None and i["content_status"] == "analysis_unavailable" for i in out["live_candidates"]["items"])
    assert out["live_candidates"]["report"] is None


@pytest.mark.parametrize("bad", [
    entry(A_URL, "this sentence is nowhere in the post at all"),                            # quote not in the post
    entry(A_URL, "short"),                                                                  # quote too short to prove anything
    entry(A_URL, "need someone to build an AI agent", sentiment="furious"),                  # not an allowed sentiment
    entry(A_URL, "need someone to build an AI agent", summary="   "),                        # empty summary
    entry("https://www.reddit.com/r/Other/comments/abc123/x/", "need someone to build an AI agent"),   # same id, other subreddit
    entry("https://www.reddit.com/r/SaaS/comments/zzz999/not_found/", "need someone to build an AI agent"),   # not an accepted post
    {"quote": "need someone to build an AI agent", "summary": "s", "sentiment": "neutral"},   # no url at all
])
def test_unsupported_analysis_is_dropped_not_fabricated(ai_on, bad):
    run, out = run_live(two_post_env(model_text([bad])))
    a = next(i for i in out["live_candidates"]["items"] if i["url"] == A_URL)
    assert a["analysis"] is None and a["content_status"] == "analysis_unavailable"
    assert a["title"] == A_TITLE and a["body"] == A_BODY                                   # the post itself is still shown, with its real text


@pytest.mark.parametrize("text", ["", "no json here", "{", '{"posts": [', '["a"]', "null", '{"posts": "x"}'])
def test_a_missing_or_malformed_reply_gives_posts_without_analysis_and_never_an_invented_one(ai_on, text):
    run, out = run_live(two_post_env(text))
    items = out["live_candidates"]["items"]
    assert {i["url"] for i in items} == {A_URL, B_URL} and all(i["analysis"] is None for i in items)


def test_a_json_fence_or_surrounding_words_are_tolerated_but_a_truncated_reply_is_not():
    obj = {"posts": [entry(A_URL, "need someone")]}
    assert rl.parse_analysis(with_text(resp([(A_URL, "t")]), "```json\n" + json.dumps(obj) + "\n```")) == obj
    assert rl.parse_analysis(with_text(resp([(A_URL, "t")]), "Here you go: " + json.dumps(obj) + " Done.")) == obj
    assert rl.parse_analysis(with_text(resp([(A_URL, "t")]), json.dumps(obj)[:-7])) is None


def test_a_link_post_without_body_text_gets_analysis_only_if_the_quote_is_in_its_title(ai_on):
    env = two_post_env(model_text([entry(A_URL, "looking for an ai agent builder", "Asks for a builder.", "neutral"),
                                   entry(B_URL, "both were frustrating", "invented body claim")]))
    env.bodies = {"abc123": "", "def456": ""}                                                # neither post has text
    run, out = run_live(env)
    items = {i["url"]: i for i in out["live_candidates"]["items"]}
    assert items[A_URL]["analysis"] and items[A_URL]["body"] is None                        # grounded in the real title
    assert items[B_URL]["analysis"] is None                                                  # the quote is not in the title: nothing invented


def test_a_post_whose_real_title_reddit_cannot_provide_is_not_accepted(ai_on):
    env = two_post_env(model_text([]))
    base = env.deps.reddit_info
    env.deps.reddit_info = lambda ids, t: {i: dict(v, title=None) if i == "def456" else v for i, v in base(ids, t).items()}
    run, out = run_live(env)
    assert [i["url"] for i in out["live_candidates"]["items"]] == [A_URL]


def test_out_of_window_and_unreachable_posts_are_still_rejected_before_any_analysis(ai_on):
    env = two_post_env(model_text([entry(A_URL, "need someone to build an AI agent"), entry(B_URL, "both were frustrating")]))
    env.ages.update({"abc123": 99999, "def456": None})
    run, out = run_live(env)
    assert "live_candidates" not in out


def test_duplicate_urls_and_posts_already_in_the_saved_results_stay_deduplicated(ai_on):
    env = live_env(items=[(A_URL, A_TITLE), (A_URL + "?utm_source=x", A_TITLE), (HURL, "saved already")])
    env.bodies = {"abc123": A_BODY}
    env.response = with_text(env.response, model_text([entry(A_URL, "need someone to build an AI agent")]))
    run, out = run_live(env)
    assert [i["url"] for i in out["live_candidates"]["items"]] == [A_URL] and "duplicate" in out["live_candidates"]["statement"]


# ── the concise report ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_the_report_has_at_most_three_themes_and_counts_only_verified_posts(ai_on):
    posts = [entry(A_URL, "need someone to build an AI agent", "a", "neutral"), entry(B_URL, "both were frustrating", "b", "negative")]
    themes = [{"theme": "Support automation", "why_it_matters": "Repetitive tickets", "post_urls": [A_URL, B_URL, "https://www.reddit.com/r/x_y/comments/nope99/ghost/"]},
              {"theme": "Bot disappointment", "why_it_matters": "Existing bots fall short", "post_urls": [B_URL]},
              {"theme": "Ghost theme", "why_it_matters": "x", "post_urls": ["https://www.reddit.com/r/x_y/comments/nope99/ghost/"]},   # supported by no accepted post
              {"theme": "Third", "why_it_matters": "y", "post_urls": [A_URL]},
              {"theme": "Fourth is cut", "why_it_matters": "z", "post_urls": [A_URL]}]
    run, out = run_live(two_post_env(model_text(posts, themes=themes, implications=["one", "two", "three", "four"])))
    rep = out["live_candidates"]["report"]
    assert rep["main_finding"] and len(rep["themes"]) <= 3 and len(rep["implications"]) <= 3
    by = {t["theme"]: t for t in rep["themes"]}
    assert by["Support automation"]["verified_post_count"] == 2 and by["Support automation"]["post_urls"] == [A_URL, B_URL]   # the ghost url is not counted
    assert by["Bot disappointment"]["verified_post_count"] == 1 and "Ghost theme" not in by and "Fourth is cut" not in by
    assert rep["limitations"] and len(rep["limitations"]) <= 3


def test_a_report_without_any_grounded_post_is_not_produced(ai_on):
    run, out = run_live(two_post_env(model_text([entry(A_URL, "never in the post at all", "x")], main_finding="Big claim", themes=[{"theme": "T", "post_urls": [A_URL]}])))
    assert out["live_candidates"]["report"] is None


def test_existing_historical_answer_keys_are_untouched(ai_on):
    env = two_post_env(model_text([entry(A_URL, "need someone to build an AI agent")]))
    run, out = run_live(env)
    assert {k: v for k, v in out.items() if k != "live_candidates"} == json.loads(answer_json(HURL))
