"""
tests/test_source_grounding.py
===============================
Regression tests for Flintel's strict source-grounding rule.

Spec reference: attachment 7bf54e9d (16-part source-grounding specification)

What is tested:
  1. analyze_with_claude()       — non-streaming path
  2. analyze_with_claude_stream() — streaming path
  3. build_google_fallback_answer_context() — flintel.py fallback builder
  4. Router / topic-resolver paths (query understanding vs factual answering)
  5. Research-style answer structure (intro / summaries / synthesis / numbers / URLs)

Mocked: _call_claude, _call_claude_stream, all external services (Mongo, Anthropic SDK)
Real:   analyze_with_claude, analyze_with_claude_stream, build_google_fallback_answer_context,
        the user_message strings they construct, and the prompt instructions sent to Claude

Prohibited in tests: real Anthropic API calls, real MongoDB writes, git operations.

Run:
    cd /mnt/user-data/outputs
    python -m pytest tests/test_source_grounding.py -v
"""

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ─── Forbidden phrases ────────────────────────────────────────────────────────
# Any user_message sent to Claude that contains these phrases is a grounding
# violation — it gives Claude permission to use outside knowledge.
#
# IMPORTANT: these are matched as affirmative GRANTS, not as prohibitions.
# A phrase like "Do NOT answer from general knowledge" is CORRECT and must
# NOT trigger a false positive.  check_no_forbidden_phrases() strips leading
# prohibition prefixes before testing.
FORBIDDEN_PHRASES = [
    "from your own general knowledge",
    "from general knowledge",
    "answer anything else",
    "answer from general knowledge",
    "use general knowledge",
    "use your general knowledge",
    "use your own knowledge",
    "answer from your own knowledge",
    "from outside knowledge",
    "from outside web",
    "use web knowledge",
]

# Prohibition prefixes — if a line starts with one of these (case-insensitive),
# then any forbidden phrase on that same line is a correct prohibition, not a
# grounding violation.
_PROHIBITION_PREFIXES = (
    "do not ",
    "do not:",
    "never ",
    "must not ",
    "cannot ",
    "not allowed to ",
    "prohibit",
    "forbid",
    "strictly in",  # "Respond STRICTLY in the no_results JSON format"
)

# ─── Required phrases (no-evidence paths) ────────────────────────────────────
# When there is no retrieved evidence, the user_message MUST instruct Claude
# to respond in the no_results JSON format and must NOT allow free-text / prose.
# These phrases match the actual wording used in logics.py and flintel.py.
REQUIRED_NO_EVIDENCE_PHRASES = [
    "no_results JSON format",
    "STRICTLY",
    # logics.py uses this phrasing:  "never switch to free-text/general-knowledge prose"
    # flintel.py uses:               "Do NOT answer from general knowledge"
    # At least one of the two must appear — checked via check_has_required_no_evidence_phrases
]

# Additional per-file phrase sets (used by the specific path tests):
REQUIRED_LOGICS_NO_EVIDENCE_PHRASES = [
    "no_results JSON format",
    "STRICTLY",
    "never switch",         # "never switch to free-text/general-knowledge prose"
]

REQUIRED_FLINTEL_NO_EVIDENCE_PHRASES = [
    "no_results JSON format",
    "STRICTLY",
    "Do NOT answer from general knowledge",
]

# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _make_db_stub():
    db_stub = types.ModuleType("database")
    _coll = MagicMock()
    for attr in [
        "db", "jobs_collection", "signals_collection",
        "signals_collection_2", "signals_collection_4",
        "google_posts_collection", "topic_evidence_cache_collection",
        "website_evidence_cache_collection",
    ]:
        setattr(db_stub, attr, _coll)
    return db_stub


def _make_flintel_stub():
    """Minimal flintel stub for logics.py import — gives addenda strings."""
    fi_stub = types.ModuleType("flintel")
    fi_stub.ROUTER_UNFILTERED_ADDENDUM = ""
    fi_stub.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM = ""
    fi_stub.build_google_fallback_answer_context = None  # will be set from real module
    return fi_stub


@pytest.fixture
def logics_mod(monkeypatch):
    """Load logics.py with all external dependencies stubbed out."""
    db_stub = _make_db_stub()
    fi_stub = _make_flintel_stub()

    wi_stub = types.ModuleType("website_intelligence")
    goog_stub = types.ModuleType("google")
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.AsyncClient = MagicMock()
    httpx_stub.TimeoutException = Exception
    httpx_stub.HTTPStatusError = Exception

    stubs = {
        "database": db_stub,
        "flintel": fi_stub,
        "website_intelligence": wi_stub,
        "google": goog_stub,
        "httpx": httpx_stub,
    }
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    for key in list(sys.modules):
        if key == "logics" or key.startswith("logics."):
            monkeypatch.delitem(sys.modules, key, raising=False)

    import logics
    return logics


@pytest.fixture
def real_flintel():
    """Import the real flintel module (it only needs os/re/stdlib)."""
    for key in list(sys.modules):
        if key == "flintel" or key.startswith("flintel."):
            del sys.modules[key]
    import flintel as f
    return f


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_signal(title="Test post", text="Some discussion text", url="https://reddit.com/r/test/1"):
    """Minimal matched signal that build_claude_post_context accepts."""
    return {
        "title": title,
        "post_text": text,
        "post_url": url,
        "platform": "reddit",
    }


def _line_is_prohibition(line: str) -> bool:
    """Return True if the line is a prohibition rather than a permission grant."""
    stripped = line.strip().lower()
    return any(stripped.startswith(pfx) for pfx in _PROHIBITION_PREFIXES)


def check_no_forbidden_phrases(text: str, label: str = ""):
    """Assert none of the general-knowledge permission phrases appear as GRANTS.

    Lines that start with a prohibition prefix (Do NOT, never, must not, …) are
    excluded — e.g. "Do NOT answer from general knowledge" is correct behavior,
    not a violation.
    """
    lines = text.splitlines()
    # Also check the full text split by sentence for multi-sentence single lines
    for phrase in FORBIDDEN_PHRASES:
        phrase_lower = phrase.lower()
        for line in lines:
            if phrase_lower in line.lower() and not _line_is_prohibition(line):
                # Extra check: maybe the phrase appears inside a prohibition
                # embedded within the line (e.g. "… kabhi bhi Do NOT answer from …")
                line_lower = line.lower()
                idx = line_lower.find(phrase_lower)
                if idx != -1:
                    # Look at the 50 chars before the phrase for prohibition words
                    pre = line_lower[max(0, idx - 50):idx]
                    prohibited = any(
                        pfx in pre for pfx in ("do not", "never ", "must not", "cannot", "kabhi")
                    )
                    if not prohibited:
                        raise AssertionError(
                            f"GROUNDING VIOLATION in {label!r}: "
                            f"user_message contains forbidden phrase {phrase!r}.\n"
                            f"Offending line: {line!r}\n"
                            f"Full text:\n{text}"
                        )


def check_has_required_no_evidence_phrases(text: str, label: str = "",
                                           phrases: list = None):
    """Assert that insufficient-evidence instructions are present.

    If `phrases` is None, uses REQUIRED_NO_EVIDENCE_PHRASES (the common set).
    Pass REQUIRED_LOGICS_NO_EVIDENCE_PHRASES or REQUIRED_FLINTEL_NO_EVIDENCE_PHRASES
    for path-specific checks.
    """
    if phrases is None:
        phrases = REQUIRED_NO_EVIDENCE_PHRASES
    for phrase in phrases:
        assert phrase in text, (
            f"MISSING REQUIRED PHRASE in {label!r}: "
            f"user_message for no-evidence path is missing {phrase!r}.\n"
            f"Full text:\n{text}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# PART A — Non-streaming path: analyze_with_claude()
# ══════════════════════════════════════════════════════════════════════════════

class TestAnalyzeWithClaude:
    """Non-streaming answer generation path."""

    def test_1_sufficient_evidence_calls_claude_with_posts(self, logics_mod):
        """Case A: real posts → Claude receives post content, not a knowledge fallback."""
        signals = [make_signal(text="We need WhatsApp automation urgently")]
        captured = {}

        def fake_call_claude(system_prompt, user_message, **kwargs):
            captured["user_message"] = user_message
            return json.dumps({"format": "source_list", "results": []})

        with patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("find WhatsApp bots", signals)

        assert "user_message" in captured, "Claude was not called"
        msg = captured["user_message"]
        assert "We need WhatsApp automation urgently" in msg, (
            "Post text must be present in the user_message"
        )
        check_no_forbidden_phrases(msg, "analyze_with_claude/sufficient-evidence")

    def test_2_zero_evidence_returns_no_results_format(self, logics_mod):
        """Case B: zero posts → user_message must instruct no_results JSON, no general knowledge."""
        captured = {}

        def fake_call_claude(system_prompt, user_message, **kwargs):
            captured["user_message"] = user_message
            return json.dumps({
                "format": "no_results",
                "message": "No relevant posts found.",
                "suggested_actions": []
            })

        with patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            result = logics_mod.analyze_with_claude("find obscure niche topic", [])

        msg = captured["user_message"]
        check_no_forbidden_phrases(msg, "analyze_with_claude/zero-evidence")
        check_has_required_no_evidence_phrases(
            msg, "analyze_with_claude/zero-evidence",
            phrases=REQUIRED_LOGICS_NO_EVIDENCE_PHRASES
        )

    def test_3_partial_evidence_passes_only_retrieved_posts(self, logics_mod):
        """Case C: some posts → user_message contains exactly the post text, nothing extra."""
        signals = [make_signal(text="Looking for CRM software")]
        captured = {}

        def fake_call_claude(system_prompt, user_message, **kwargs):
            captured["user_message"] = user_message
            return json.dumps({"format": "source_list", "results": []})

        with patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("what CRM do people use", signals)

        msg = captured["user_message"]
        assert "Looking for CRM software" in msg
        check_no_forbidden_phrases(msg, "analyze_with_claude/partial-evidence")

    def test_4_irrelevant_evidence_no_knowledge_injection(self, logics_mod):
        """Case D: posts present but completely off-topic → posts passed through; no knowledge grant."""
        signals = [make_signal(title="Dog food review", text="My dog loves this kibble")]
        captured = {}

        def fake_call_claude(system_prompt, user_message, **kwargs):
            captured["user_message"] = user_message
            return json.dumps({"format": "source_list", "results": []})

        with patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("WhatsApp AI agents", signals)

        msg = captured["user_message"]
        assert "My dog loves this kibble" in msg
        check_no_forbidden_phrases(msg, "analyze_with_claude/irrelevant-evidence")

    def test_5_analysis_system_prompt_no_knowledge_grant(self, logics_mod):
        """The CLAUDE_ANALYSIS_SYSTEM_PROMPT itself must not contain general-knowledge grants."""
        sp = logics_mod.CLAUDE_ANALYSIS_SYSTEM_PROMPT
        # These phrases would give Claude permission to answer from pretrained knowledge
        explicit_grants = [
            "answer from general knowledge",
            "use your training",
            "from your own knowledge",
        ]
        for phrase in explicit_grants:
            assert phrase.lower() not in sp.lower(), (
                f"CLAUDE_ANALYSIS_SYSTEM_PROMPT contains knowledge grant: {phrase!r}"
            )

    def test_6_research_style_system_prompt_has_grounding_instruction(self, logics_mod):
        """The analysis system prompt must contain GROUNDING (Step 8) instruction."""
        sp = logics_mod.CLAUDE_ANALYSIS_SYSTEM_PROMPT
        # Step 8 should mention grounding or source-only
        grounding_indicators = ["GROUNDING", "source-grounded", "grounded", "retrieved"]
        found = any(ind in sp for ind in grounding_indicators)
        assert found, (
            "CLAUDE_ANALYSIS_SYSTEM_PROMPT appears to lack any grounding instruction "
            f"(searched for: {grounding_indicators})"
        )


# ══════════════════════════════════════════════════════════════════════════════
# PART B — Streaming path: analyze_with_claude_stream()
# ══════════════════════════════════════════════════════════════════════════════

class TestAnalyzeWithClaudeStream:
    """Streaming answer generation — must obey the same grounding rules."""

    def test_7_streaming_sufficient_evidence_no_knowledge_grant(self, logics_mod):
        """Case A streaming: posts present → user_message contains post text, no knowledge grant."""
        signals = [make_signal(text="Need WhatsApp chatbot for support")]
        captured = {}

        def fake_stream(system_prompt, user_message, **kwargs):
            captured["user_message"] = user_message
            yield '{"format": "source_list", "results": []}'

        with patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream):
            chunks = list(logics_mod.analyze_with_claude_stream("WhatsApp bot", signals))

        assert "user_message" in captured
        msg = captured["user_message"]
        assert "Need WhatsApp chatbot for support" in msg
        check_no_forbidden_phrases(msg, "analyze_with_claude_stream/sufficient-evidence")

    def test_8_streaming_zero_evidence_no_general_knowledge(self, logics_mod):
        """Case B streaming: CRITICAL — zero posts must NOT instruct general-knowledge answer."""
        captured = {}

        def fake_stream(system_prompt, user_message, **kwargs):
            captured["user_message"] = user_message
            yield '{"format": "no_results", "message": "No posts.", "suggested_actions": []}'

        with patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream):
            chunks = list(logics_mod.analyze_with_claude_stream("unknown niche topic", []))

        assert "user_message" in captured, "Claude stream was not called"
        msg = captured["user_message"]
        # This was the core violation — must now be fixed
        check_no_forbidden_phrases(msg, "analyze_with_claude_stream/zero-evidence")
        check_has_required_no_evidence_phrases(
            msg, "analyze_with_claude_stream/zero-evidence",
            phrases=REQUIRED_LOGICS_NO_EVIDENCE_PHRASES
        )

    def test_9_streaming_consistent_with_non_streaming(self, logics_mod):
        """Streaming and non-streaming zero-evidence user_messages must be equivalent."""
        stream_msg = {}
        non_stream_msg = {}

        def fake_stream(sp, um, **kw):
            stream_msg["user_message"] = um
            yield '{"format":"no_results","message":"","suggested_actions":[]}'

        def fake_call(sp, um, **kw):
            non_stream_msg["user_message"] = um
            return '{"format":"no_results","message":"","suggested_actions":[]}'

        with patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream):
            list(logics_mod.analyze_with_claude_stream("some query", []))

        with patch.object(logics_mod, "_call_claude", side_effect=fake_call):
            logics_mod.analyze_with_claude("some query", [])

        sm = stream_msg["user_message"]
        nm = non_stream_msg["user_message"]

        # Both must forbid general knowledge
        check_no_forbidden_phrases(sm, "stream/zero-evidence")
        check_no_forbidden_phrases(nm, "non-stream/zero-evidence")

        # Both must instruct no_results JSON using path-appropriate phrase sets
        check_has_required_no_evidence_phrases(
            sm, "stream/zero-evidence", phrases=REQUIRED_LOGICS_NO_EVIDENCE_PHRASES
        )
        check_has_required_no_evidence_phrases(
            nm, "non-stream/zero-evidence", phrases=REQUIRED_LOGICS_NO_EVIDENCE_PHRASES
        )

    def test_10_streaming_no_enable_web_search(self, logics_mod):
        """analyze_with_claude_stream must never call _call_claude_stream with enable_web_search=True."""
        signals = [make_signal()]
        call_args_list = []

        def fake_stream(system_prompt, user_message, **kwargs):
            call_args_list.append(kwargs)
            yield '{"format":"source_list","results":[]}'

        with patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream):
            list(logics_mod.analyze_with_claude_stream("test query", signals))

        for kwargs in call_args_list:
            assert not kwargs.get("enable_web_search", False), (
                "analyze_with_claude_stream called _call_claude_stream with enable_web_search=True"
            )


# ══════════════════════════════════════════════════════════════════════════════
# PART C — Google fallback path: build_google_fallback_answer_context()
# ══════════════════════════════════════════════════════════════════════════════

class TestGoogleFallbackAnswerContext:
    """flintel.build_google_fallback_answer_context() — must not allow general-knowledge answers."""

    def test_11_zero_stubs_no_general_knowledge(self, real_flintel):
        """Case B (zero evidence): stub_count=0 must return a no_results instruction, not a knowledge grant."""
        ctx = real_flintel.build_google_fallback_answer_context("WhatsApp bots", 0)
        check_no_forbidden_phrases(ctx, "build_google_fallback_answer_context/stub_count=0")
        check_has_required_no_evidence_phrases(
            ctx, "build_google_fallback_answer_context/stub_count=0",
            phrases=REQUIRED_FLINTEL_NO_EVIDENCE_PHRASES
        )

    def test_12_nonzero_stubs_no_general_knowledge(self, real_flintel):
        """Case D (links only, no body): stub_count=3 must still NOT grant general knowledge."""
        ctx = real_flintel.build_google_fallback_answer_context("travel software", 3)
        check_no_forbidden_phrases(ctx, "build_google_fallback_answer_context/stub_count=3")
        check_has_required_no_evidence_phrases(
            ctx, "build_google_fallback_answer_context/stub_count=3",
            phrases=REQUIRED_FLINTEL_NO_EVIDENCE_PHRASES
        )

    def test_13_stub_count_mentioned_in_context(self, real_flintel):
        """When stubs exist, the count should appear in the instruction so Claude knows links are shown."""
        ctx = real_flintel.build_google_fallback_answer_context("some topic", 5)
        assert "5" in ctx, "stub_count should be mentioned in the fallback context"

    def test_14_stub_count_singular_vs_plural(self, real_flintel):
        """Grammatical check: singular/plural thread label is correct."""
        ctx1 = real_flintel.build_google_fallback_answer_context("topic", 1)
        ctx5 = real_flintel.build_google_fallback_answer_context("topic", 5)
        assert "1 related thread " in ctx1 or "1 related thread\n" in ctx1, (
            f"Expected singular 'thread' for stub_count=1, got: {ctx1[:200]}"
        )
        assert "5 related threads" in ctx5, (
            f"Expected plural 'threads' for stub_count=5, got: {ctx5[:200]}"
        )

    def test_15_fallback_context_does_not_narrate_internal_pipeline(self, real_flintel):
        """Claude must not be instructed to USE Flintel's internal architecture terms.

        The instruction may MENTION these terms only inside a prohibition such as
        'never use wording like "discovery-only"' — that is the correct behavior.
        The test checks that, for each internal term, every line that contains it
        also contains a prohibition prefix (never, do not, etc.).
        """
        internal_terms = ["discovery-only", "stub", "supplementary Google search", "not fetched yet"]
        prohibition_words = ("never", "do not", "don't", "must not", "kabhi", "mat")

        for count in (0, 2):
            ctx = real_flintel.build_google_fallback_answer_context("test", count)
            for term in internal_terms:
                for line in ctx.splitlines():
                    if term.lower() in line.lower():
                        line_lower = line.lower()
                        assert any(pw in line_lower for pw in prohibition_words), (
                            f"build_google_fallback_answer_context/{count}: "
                            f"line containing internal term {term!r} is not wrapped in a prohibition.\n"
                            f"Line: {line!r}"
                        )

    def test_16_fallback_context_no_results_json_format_instruction(self, real_flintel):
        """Both branches must produce an instruction that routes Claude to no_results JSON."""
        for count in (0, 4):
            ctx = real_flintel.build_google_fallback_answer_context("AI agents", count)
            assert "no_results JSON format" in ctx, (
                f"build_google_fallback_answer_context stub_count={count} missing no_results JSON instruction"
            )


# ══════════════════════════════════════════════════════════════════════════════
# PART D — Router / topic-resolver (query understanding only)
# ══════════════════════════════════════════════════════════════════════════════

class TestRouterGrounding:
    """Router classifies intent and generates keywords — it does NOT generate factual answers.
    Web search is allowed for query understanding per spec Part 5."""

    def test_17_map_step_prompt_no_knowledge_grant(self, logics_mod):
        """CLAUDE_MAP_STEP_SYSTEM_PROMPT must not grant general knowledge."""
        sp = logics_mod.CLAUDE_MAP_STEP_SYSTEM_PROMPT
        check_no_forbidden_phrases(sp, "CLAUDE_MAP_STEP_SYSTEM_PROMPT")
        # Map step must explicitly restrict to post content
        assert any(phrase in sp for phrase in [
            "not present in the posts",
            "only from the posts",
            "grounded",
            "do not invent",
            "Do not invent",
        ]), "CLAUDE_MAP_STEP_SYSTEM_PROMPT must forbid inventing information"

    def test_18_notes_reduce_prompt_no_knowledge_grant(self, logics_mod):
        """CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT must not grant general knowledge."""
        sp = logics_mod.CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT
        check_no_forbidden_phrases(sp, "CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT")

    def test_19_analyze_with_claude_no_web_search(self, logics_mod):
        """analyze_with_claude() must never pass enable_web_search=True to _call_claude."""
        signals = [make_signal()]
        call_kwargs_list = []

        def fake_call(sp, um, **kwargs):
            call_kwargs_list.append(kwargs)
            return '{"format":"source_list","results":[]}'

        with patch.object(logics_mod, "_call_claude", side_effect=fake_call):
            logics_mod.analyze_with_claude("test", signals)

        for kwargs in call_kwargs_list:
            assert not kwargs.get("enable_web_search", False), (
                "analyze_with_claude passed enable_web_search=True to _call_claude — "
                "this would allow web knowledge into factual answers"
            )

    def test_20_topic_resolver_prompt_no_factual_answer(self, logics_mod):
        """CLAUDE_TOPIC_RESOLVER_SYSTEM_PROMPT must not produce user-visible factual claims."""
        sp = logics_mod.CLAUDE_TOPIC_RESOLVER_SYSTEM_PROMPT
        # Resolver uses training knowledge for interpretation only — must not answer facts
        assert "general knowledge" in sp.lower() or "own knowledge" in sp.lower(), (
            "Topic resolver should acknowledge it uses training knowledge for interpretation"
        )
        # Must not produce factual answer output formats
        assert "source_list" not in sp, (
            "Topic resolver prompt should not reference source_list answer format — "
            "it is for topic interpretation, not factual answer generation"
        )


# ══════════════════════════════════════════════════════════════════════════════
# PART E — Research-style answer structure grounding
# ══════════════════════════════════════════════════════════════════════════════

class TestResearchStyleGrounding:
    """The analysis system prompt must instruct research-style output grounded in evidence."""

    def test_21_analysis_prompt_has_research_structure(self, logics_mod):
        """CLAUDE_ANALYSIS_SYSTEM_PROMPT should define structured output formats."""
        sp = logics_mod.CLAUDE_ANALYSIS_SYSTEM_PROMPT
        # Must define the output formats specified in Part 8 of the spec
        assert "source_list" in sp, "Must define source_list output format"
        assert "no_results" in sp, "Must define no_results output format"

    def test_22_analysis_prompt_requires_source_urls(self, logics_mod):
        """The analysis system prompt must preserve source URLs (spec Part 8 §4)."""
        sp = logics_mod.CLAUDE_ANALYSIS_SYSTEM_PROMPT
        url_indicators = ["url", "source", "link", "post_url"]
        found = any(ind in sp.lower() for ind in url_indicators)
        assert found, (
            "CLAUDE_ANALYSIS_SYSTEM_PROMPT must reference URL/source preservation "
            f"(searched for: {url_indicators})"
        )

    def test_23_stats_numbers_must_come_from_evidence(self, logics_mod):
        """The analysis system prompt must not instruct Claude to invent statistics."""
        sp = logics_mod.CLAUDE_ANALYSIS_SYSTEM_PROMPT
        invention_prohibitions = [
            "not invent",
            "never invent",
            "do not invent",
            "don't invent",
            "no invented",
            "grounded",
            "retrieved",
        ]
        found = any(phrase in sp.lower() for phrase in invention_prohibitions)
        assert found, (
            "CLAUDE_ANALYSIS_SYSTEM_PROMPT must explicitly prohibit inventing facts/stats"
        )

    def test_24_post_context_strips_to_title_and_text(self, logics_mod):
        """build_claude_post_context must strip to title+text only — no injection of outside data."""
        signals = [
            {
                "title": "My CRM Post",
                "post_text": "We need better CRM tooling",
                "post_url": "https://reddit.com/r/sales/123",
                "platform": "reddit",
                "score": 99,
                "some_internal_field": "internal value",
            }
        ]
        posts = logics_mod.build_claude_post_context(signals)
        assert len(posts) == 1
        assert posts[0]["title"] == "My CRM Post"
        # build_claude_post_context strips to {"title", "text"} — key is "text" not "post_text"
        assert posts[0]["text"] == "We need better CRM tooling"
        # Should NOT carry through injection vectors
        assert "score" not in posts[0], "score field must be stripped"
        assert "post_url" not in posts[0], "post_url must be stripped"
        assert "platform" not in posts[0], "platform must be stripped"

    def test_25_format_posts_block_contains_only_post_data(self, logics_mod):
        """_format_posts_block must produce text derived only from post titles and bodies.

        build_claude_post_context strips to {"title", "text"} so _format_posts_block
        receives dicts with key "text", not "post_text".
        """
        posts = [{"title": "Automation needed", "text": "We want to automate billing"}]
        block = logics_mod._format_posts_block(posts)
        assert "Automation needed" in block
        assert "We want to automate billing" in block
        check_no_forbidden_phrases(block, "_format_posts_block output")


# ══════════════════════════════════════════════════════════════════════════════
# PART F — Codebase-wide grep for remaining violations
# ══════════════════════════════════════════════════════════════════════════════

class TestCodebaseWideGrep:
    """Static checks — grep actual source files for remaining general-knowledge grants."""

    SOURCES = [
        ROOT / "logics.py",
        ROOT / "flintel.py",
    ]

    # These patterns are AFFIRMATIVE GRANTS — they appear only in violation code, not in
    # correct prohibition text like "Do NOT answer from general knowledge".
    # Keep them specific enough to never match a prohibition.
    VIOLATION_PATTERNS = [
        "answer the user's actual question from your own general knowledge",
        "answer anything else in the question you still can from general knowledge",
        "answer from your own general knowledge",
        "from your own general knowledge instead",
        # NOTE: "answer from general knowledge" is intentionally excluded here because
        # flintel.py correctly uses it inside the prohibition string
        # "Do NOT answer from general knowledge" — that is NOT a violation.
        # test_30 covers the specific old violation strings that were removed.
    ]

    def _check_file_no_grant_patterns(self, filepath, label):
        """For each violation pattern, verify no line contains it as an affirmative grant."""
        content = (ROOT / filepath).read_text(encoding="utf-8")
        prohibition_words = ("do not", "never", "don't", "must not", "cannot", "kabhi", "mat")
        for pattern in self.VIOLATION_PATTERNS:
            pattern_lower = pattern.lower()
            for line in content.splitlines():
                if pattern_lower in line.lower():
                    line_lower = line.lower()
                    idx = line_lower.find(pattern_lower)
                    pre = line_lower[max(0, idx - 80):idx]
                    if not any(pw in pre for pw in prohibition_words):
                        raise AssertionError(
                            f"{label} still contains general-knowledge grant: {pattern!r}\n"
                            f"Offending line: {line!r}"
                        )

    def test_26_logics_py_no_general_knowledge_grant(self):
        """logics.py must contain no general-knowledge grant instructions after fixes."""
        self._check_file_no_grant_patterns("logics.py", "logics.py")

    def test_27_flintel_py_no_general_knowledge_grant(self):
        """flintel.py must contain no general-knowledge grant instructions after fixes."""
        self._check_file_no_grant_patterns("flintel.py", "flintel.py")

    def test_28_routes_py_no_factual_knowledge_grant_in_search_paths(self):
        """routes.py must not contain general-knowledge grants in search/analysis paths.
        Note: CLAUDE_CHAT_FALLBACK_SYSTEM_PROMPT usage in chat-only path is intentionally excluded."""
        content = (ROOT / "routes.py").read_text(encoding="utf-8").lower()
        for pattern in self.VIOLATION_PATTERNS:
            assert pattern.lower() not in content, (
                f"routes.py still contains general-knowledge grant: {pattern!r}"
            )

    def test_29_stream_path_no_general_knowledge_remaining(self):
        """Double-check: the exact old violation string is gone from logics.py."""
        content = (ROOT / "logics.py").read_text(encoding="utf-8")
        old_violation = (
            "you have no post data "
            "to ground an answer in. Say that plainly, then answer anything "
            "else in the question you still can from general knowledge."
        )
        assert old_violation not in content, (
            "analyze_with_claude_stream still contains the original general-knowledge violation"
        )

    def test_30_flintel_old_violation_removed(self):
        """Double-check: both old flintel.py violation strings are gone."""
        content = (ROOT / "flintel.py").read_text(encoding="utf-8")
        old_v1 = "Answer the user's actual question from your own general"
        old_v2 = "Answer the user's actual question from your own general knowledge."
        assert old_v1 not in content, "flintel.py stub_count=0 violation still present"
        assert old_v2 not in content, "flintel.py stub_count>0 violation still present"
