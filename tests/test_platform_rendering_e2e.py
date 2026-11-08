"""Whole page, real browser: a saved answer whose posts come from Hacker News and Reddit is shown with the right platform header, icon and per-post
Platform row, and a post the model put in the wrong group has already been regrouped by the server. Synthetic records."""
import json

import pytest

import answer_integrity as ai
from tests.test_answer_persistence import TEMPLATES, _for_template, _reopen, env  # noqa: F401

HN = "https://news.ycombinator.com/item?id={}"
RD = "https://www.reddit.com/r/SaaS/comments/{}/t/"
Q = "Find people looking for an AI SaaS agent"


def _answer():
    sigs = [
        {"title": "Ask HN: Which AI agent platform should we buy?", "post_text": "We are looking for an AI agent platform for our support team. Any recommendations? Budget is $300 a month.", "post_url": HN.format(1), "platform": "reddit", "author": "a"},
        {"title": "Show HN: AgentDesk, an AI SaaS agent", "post_text": "I built AgentDesk, an AI agent SaaS for support teams. Free trial, feedback welcome.", "post_url": HN.format(2), "platform": "x", "author": "b"},
        {"title": "Need an AI agent tool for my agency", "post_text": "We are looking for an AI agent tool for our agency. Recommendations please?", "post_url": RD.format("r1"), "platform": "reddit", "subreddit": "SaaS", "author": "c"},
    ]
    posts = [{"index": i + 1, "link": s["post_url"], "title": s["title"], "role": "buyer", "purchase_intent": "explicit"} for i, s in enumerate(sigs)]
    data = {"format": "source_list", "executive_summary": "Found posts.", "key_findings": [],
            "platforms": [{"platform": "reddit", "total_analyzed": 3, "shown_count": 3, "posts": posts}]}      # the model put everything under Reddit
    out = ai.enforce_answer_integrity(data, sigs, sigs, Q)
    return out, sigs


ROWS_JS = """() => [...document.querySelectorAll('.answer-rendered table tr')].slice(1).map(r => [...r.children].map(c => c.innerText.replace(/\\n/g, ' ').trim()))"""


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_row_of_the_report_table_names_its_real_platform(integrity_browser, name):
    answer, sigs = _answer()
    chat = {"chat_id": "c1", "title": "t", "messages": [{"query": Q, "topic_key": "q", "results": ai.annotate_results(sigs, json.dumps(answer)),
                                                         "claude_answer": json.dumps(answer)}]}
    page, errors = _reopen(integrity_browser, name, _for_template(chat))
    try:
        rows = page.evaluate(ROWS_JS)
        assert len(rows) == 3
        src = {r[2]: r[1] for r in rows}                                                                   # post title -> Source cell
        assert "Hacker News" in src["Ask HN: Which AI agent platform should we buy?"]
        assert "Hacker News" in src["Show HN: AgentDesk, an AI SaaS agent"]
        assert "X (Twitter)" not in " ".join(src.values())                                                 # the Show HN post was stored under X
        assert src["Need an AI agent tool for my agency"] != "" and "Hacker News" not in src["Need an AI agent tool for my agency"]
        assert errors == []
    finally:
        page.close()
