"""
tests/test_post_enrichment.py
=============================
System-side enrichment of the posts list in Claude's answer: real Link,
Subreddit (source) and Title — never trusting the LLM for these.

No real Mongo / OpenAI / network.
"""
import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def lg():
    import os
    os.environ.setdefault("OPENAI_API_KEY", "test-key-enrich")
    os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
    os.environ.setdefault("MONGODB_DB", "test_db")

    db_stub = types.ModuleType("database")
    _coll = MagicMock()
    for attr in [
        "db", "jobs_collection", "signals_collection",
        "signals_collection_2", "signals_collection_4",
        "google_posts_collection", "topic_evidence_cache_collection",
        "website_evidence_cache_collection",
    ]:
        setattr(db_stub, attr, _coll)
    fi_stub = types.ModuleType("flintel")
    fi_stub.ROUTER_UNFILTERED_ADDENDUM = ""
    fi_stub.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM = ""
    fi_stub.build_google_fallback_answer_context = None
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.AsyncClient = MagicMock()
    httpx_stub.TimeoutException = Exception
    httpx_stub.HTTPStatusError = Exception

    for name, mod in {
        "database": db_stub,
        "flintel": fi_stub,
        "website_intelligence": types.ModuleType("website_intelligence"),
        "google": types.ModuleType("google"),
        "httpx": httpx_stub,
    }.items():
        sys.modules[name] = mod
    for key in list(sys.modules):
        if key == "logics" or key.startswith("logics."):
            del sys.modules[key]
    sys.modules.pop("config", None)
    yield importlib.import_module("logics")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

AGENTS_URL = "https://www.reddit.com/r/AI_Agents/comments/abc123/where_to_find/"
CRM_URL = "https://www.reddit.com/r/sales/comments/def456/crm_pain/"
LI_URL = "https://www.linkedin.com/posts/jane-doe_activity-123"


def _pool():
    return [
        {"title": "CRM tooling is painful", "post_text": "Our sales team hates the CRM, data entry takes hours every single week.",
         "post_url": CRM_URL, "platform": "reddit", "subreddit": "sales"},
        {"title": "", "post_text": "Where can I find reliable AI agents for company use? We want to automate support tickets and onboarding.",
         "post_url": AGENTS_URL, "platform": "reddit", "subreddit": None},
    ]


def _answer(posts, fmt="source_list"):
    return json.dumps({
        "format": fmt,
        "executive_summary": "x",
        "platforms": [{"platform": "reddit", "total_analyzed": len(posts), "shown_count": len(posts), "posts": posts}],
    })


def _posts_of(answer_text):
    return json.loads(answer_text)["platforms"][0]["posts"]


# ---------------------------------------------------------------------------
# a. _subreddit_from_url
# ---------------------------------------------------------------------------

class TestSubredditFromUrl:
    def test_normal_url(self, lg):
        assert lg._subreddit_from_url(AGENTS_URL) == "AI_Agents"

    def test_www_and_old_reddit(self, lg):
        assert lg._subreddit_from_url("https://old.reddit.com/r/SaaS/comments/x/y/") == "SaaS"
        assert lg._subreddit_from_url("http://reddit.com/r/startups/") == "startups"
        assert lg._subreddit_from_url("https://WWW.REDDIT.COM/r/Python/comments/1") == "Python"

    def test_url_without_r(self, lg):
        assert lg._subreddit_from_url("https://www.reddit.com/user/someone/comments/1/") is None
        assert lg._subreddit_from_url(LI_URL) is None

    def test_none_and_bad_input(self, lg):
        assert lg._subreddit_from_url(None) is None
        assert lg._subreddit_from_url("") is None
        assert lg._subreddit_from_url(123) is None


# ---------------------------------------------------------------------------
# b. _derive_post_title
# ---------------------------------------------------------------------------

class TestDerivePostTitle:
    def test_real_title_kept(self, lg):
        assert lg._derive_post_title("  Real Title ", "body") == "Real Title"

    @pytest.mark.parametrize("ph", [None, "", "  ", "(no title)", "No Title", "untitled"])
    def test_placeholder_uses_first_sentence(self, lg, ph):
        assert lg._derive_post_title(ph, "Need AI agents. Budget is 5k.") == "Need AI agents."

    def test_long_text_cut_on_word_boundary(self, lg):
        text = "word " * 50
        out = lg._derive_post_title("", text)
        assert out.endswith("...")
        assert len(out) <= 83
        assert "wor..." not in out and out[:-3].split()[-1] == "word"

    def test_empty_everything(self, lg):
        assert lg._derive_post_title(None, None) == ""


# ---------------------------------------------------------------------------
# c–g, i. _patch_post_urls_into_answer
# ---------------------------------------------------------------------------

class TestPatchEnrichment:
    def test_c_index_match_fills_link_source_title(self, lg):
        ans = _answer([{"index": 2, "source": "unknown", "title": "(no title)",
                        "summary": "Asks where to find AI agents for company use", "sentiment": "neutral"}])
        p = _posts_of(lg._patch_post_urls_into_answer(ans, _pool()))[0]
        assert p["link"] == AGENTS_URL
        assert p["source"] == "AI_Agents"
        assert p["title"] == "Where can I find reliable AI agents for company use?"
        assert p["summary"] == "Asks where to find AI agents for company use"
        assert p["sentiment"] == "neutral"

    def test_c_real_title_and_doc_subreddit_used(self, lg):
        ans = _answer([{"index": 1, "source": "reddit", "title": "CRM pain", "summary": "Sales team hates CRM data entry"}])
        p = _posts_of(lg._patch_post_urls_into_answer(ans, _pool()))[0]
        assert p["link"] == CRM_URL
        assert p["source"] == "sales"
        assert p["title"] == "CRM tooling is painful"

    def test_d_placeholder_title_unknown_source_no_index_uses_summary(self, lg):
        ans = _answer([{"source": "unknown", "title": "(no title)",
                        "summary": "Wants reliable AI agents to automate support tickets and onboarding"}])
        p = _posts_of(lg._patch_post_urls_into_answer(ans, _pool()))[0]
        assert p["link"] == AGENTS_URL
        assert p["source"] == "AI_Agents"
        assert p["title"] and p["title"] != "(no title)"

    def test_d_llm_link_is_overridden(self, lg):
        ans = _answer([{"index": 2, "title": "x", "summary": "AI agents for company support",
                        "link": "https://made-up.example/post"}])
        p = _posts_of(lg._patch_post_urls_into_answer(ans, _pool()))[0]
        assert p["link"] == AGENTS_URL

    def test_e_no_confident_match_no_link(self, lg):
        ans = _answer([{"source": "unknown", "title": "(no title)",
                        "summary": "Discusses kitchen renovation costs and tile choices"}])
        p = _posts_of(lg._patch_post_urls_into_answer(ans, _pool()))[0]
        assert "link" not in p
        assert p["source"] == "unknown"
        assert p["title"] != "(no title)"  # relabelled from Claude's own summary only

    def test_e_out_of_range_index_no_link(self, lg):
        ans = _answer([{"index": 99, "title": "(no title)", "summary": "Discusses kitchen renovation costs"}])
        p = _posts_of(lg._patch_post_urls_into_answer(ans, _pool()))[0]
        assert "link" not in p

    def test_e_index_rejected_when_summary_unrelated(self, lg):
        # index 1 is the CRM post, but the summary is clearly about AI agents
        ans = _answer([{"index": 1, "title": "(no title)",
                        "summary": "Wants reliable AI agents to automate support tickets and onboarding"}])
        p = _posts_of(lg._patch_post_urls_into_answer(ans, _pool()))[0]
        assert p["link"] == AGENTS_URL  # falls through to summary match, not the wrong index

    def test_f_tie_gives_no_link(self, lg):
        pool = [
            {"title": "", "post_text": "Looking for AI agents to automate customer support tickets.",
             "post_url": "https://www.reddit.com/r/A/comments/1/", "platform": "reddit"},
            {"title": "", "post_text": "Looking for AI agents to automate customer support tickets!",
             "post_url": "https://www.reddit.com/r/B/comments/2/", "platform": "reddit"},
        ]
        ans = _answer([{"title": "(no title)", "source": "unknown",
                        "summary": "AI agents to automate customer support tickets"}])
        p = _posts_of(lg._patch_post_urls_into_answer(ans, pool))[0]
        assert "link" not in p
        assert p["source"] == "unknown"

    def test_g_non_reddit_no_subreddit_invented(self, lg):
        pool = [{"title": "Hiring AI agent devs", "post_text": "We are hiring engineers to build AI agents.",
                 "post_url": LI_URL, "platform": "linkedin", "subreddit": None}]
        ans = _answer([{"index": 1, "source": "Jane Doe", "title": "(no title)", "summary": "Hiring AI agent engineers"}])
        p = _posts_of(lg._patch_post_urls_into_answer(ans, pool))[0]
        assert p["link"] == LI_URL
        assert p["source"] == "Jane Doe"
        assert p["title"] == "Hiring AI agent devs"

        ans2 = _answer([{"index": 1, "source": "unknown", "title": "x", "summary": "Hiring AI agent engineers"}])
        p2 = _posts_of(lg._patch_post_urls_into_answer(ans2, pool))[0]
        assert p2["source"] == "unknown"

    def test_index_skips_textless_google_stubs(self, lg):
        # A text-less stub is never shown to Claude, so it must not shift indices.
        pool = [
            {"title": "r/foo", "post_text": None, "post_url": "https://www.reddit.com/r/foo/comments/9/",
             "platform": "reddit", "google_rank": 1},
        ] + _pool()
        ans = _answer([{"index": 2, "title": "(no title)", "summary": "AI agents for company use"}])
        p = _posts_of(lg._patch_post_urls_into_answer(ans, pool))[0]
        assert p["link"] == AGENTS_URL

    def test_comparison_format_enriched(self, lg):
        ans = json.dumps({"format": "comparison", "subjects": [{"name": "A", "platforms": [
            {"platform": "reddit", "posts": [{"index": 2, "source": "unknown", "title": "(no title)",
                                              "summary": "AI agents for company use"}]}]}]})
        out = json.loads(lg._patch_post_urls_into_answer(ans, _pool()))
        p = out["subjects"][0]["platforms"][0]["posts"][0]
        assert p["link"] == AGENTS_URL and p["source"] == "AI_Agents"

    def test_i_invalid_json_unchanged(self, lg):
        bad = "not json {"
        assert lg._patch_post_urls_into_answer(bad, _pool()) is bad

    def test_i_unsupported_format_unchanged(self, lg):
        txt = json.dumps({"format": "trend_report", "topic": "x"})
        assert lg._patch_post_urls_into_answer(txt, _pool()) is txt

    def test_i_nothing_changed_returns_same_string(self, lg):
        txt = _answer([{"index": 1, "source": "sales", "title": "CRM tooling is painful",
                        "summary": "Sales team hates CRM", "link": CRM_URL}])
        assert lg._patch_post_urls_into_answer(txt, _pool()) is txt


# ---------------------------------------------------------------------------
# h. Prompt formatting: no "(no title)" and global numbering across chunks
# ---------------------------------------------------------------------------

class TestFormatPostsBlock:
    def test_no_placeholder_title_sent(self, lg):
        block = lg._format_posts_block([{"title": "", "text": "body text"}])
        assert "(no title)" not in block
        assert "Title:" not in block
        assert "[Post 1]" in block and "body text" in block

    def test_start_index(self, lg):
        block = lg._format_posts_block([{"title": "t", "text": "a"}, {"title": "u", "text": "b"}], start_index=13)
        assert "[Post 13]" in block and "[Post 14]" in block and "[Post 1]\n" not in block

    def test_h_multi_chunk_global_numbering(self, lg):
        signals = [{"title": f"T{i}", "post_text": f"text number {i}", "post_url": f"https://reddit.com/r/x/comments/{i}/",
                    "platform": "reddit"} for i in range(1, 26)]
        sent = []

        def fake_call(system_prompt, user_message, **kw):
            sent.append((system_prompt, user_message))
            if system_prompt is lg.CLAUDE_MAP_STEP_SYSTEM_PROMPT:
                return "- point (Post 1)"
            return '{"format": "source_list", "platforms": []}'

        with patch.object(lg, "CLAUDE_POSTS_PER_CHUNK", 12), \
             patch.object(lg, "CLAUDE_NOTES_PER_CHUNK", 100), \
             patch.object(lg, "_call_claude", side_effect=fake_call):
            lg.analyze_with_claude("q", signals)

        map_msgs = [m for sp, m in sent if sp is lg.CLAUDE_MAP_STEP_SYSTEM_PROMPT]
        assert len(map_msgs) == 3
        assert "[Post 1]" in map_msgs[0] and "[Post 12]" in map_msgs[0]
        assert "[Post 13]" in map_msgs[1] and "[Post 24]" in map_msgs[1]
        assert "[Post 1]\n" not in map_msgs[1]
        assert "[Post 25]" in map_msgs[2]
        final = [m for sp, m in sent if sp is lg.CLAUDE_ANALYSIS_SYSTEM_PROMPT][-1]
        assert "(Post N)" in final

    def test_build_claude_post_context_shape_unchanged(self, lg):
        posts = lg.build_claude_post_context(_pool() + [{"title": "x", "post_text": None, "post_url": "u"}])
        assert posts == [
            {"title": "CRM tooling is painful", "text": _pool()[0]["post_text"]},
            {"title": "", "text": _pool()[1]["post_text"]},
        ]


# ---------------------------------------------------------------------------
# A. Matched dict carries subreddit (from doc field or URL)
# ---------------------------------------------------------------------------

class TestMatchedDictSubreddit:
    def test_signal_subreddit_helper(self, lg):
        assert lg._signal_subreddit({"subreddit": "r/SaaS"}, AGENTS_URL) == "SaaS"
        assert lg._signal_subreddit({}, AGENTS_URL) == "AI_Agents"
        assert lg._signal_subreddit({}, LI_URL) is None

    def test_get_matched_signals_includes_subreddit(self, lg):
        class Cursor:
            def __init__(self, docs): self.docs = docs
            def sort(self, *a, **k): return self
            def batch_size(self, n): return self
            def limit(self, n): return Cursor(self.docs[:n])
            def __iter__(self): return iter(self.docs)

        docs = [
            {"title": "", "post_text": "Need AI agents for support", "post_url": AGENTS_URL,
             "platform": "reddit", "embedding": [0.1] * 8, "created_utc": 1_700_000_000},
            {"title": "CRM", "post_text": "CRM pain again", "post_url": CRM_URL, "platform": "reddit",
             "subreddit": "sales", "embedding": [0.1] * 8, "created_utc": 1_700_000_000},
        ]
        coll = MagicMock()
        coll.find.return_value = Cursor(docs)
        with patch.object(lg, "signals_collection", coll), \
             patch.object(lg, "SIGNAL_EMBEDDING_CANDIDATE_POOL", 0), \
             patch.object(lg, "generate_query_embeddings_batch", lambda t: [[0.1] * 8 for _ in t]):
            out = lg.get_matched_signals(topic_key="t", keywords=["ai agents"])
        by_url = {m["post_url"]: m for m in out}
        assert by_url, "expected matches from the fake collection"
        assert by_url[AGENTS_URL]["subreddit"] == "AI_Agents"
        assert by_url[CRM_URL]["subreddit"] == "sales"
