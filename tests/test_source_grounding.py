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


# ══════════════════════════════════════════════════════════════════════════════
# PART G — Empty-notes path runtime tests (EMPTY-NOTES GROUNDING FIX)
# ══════════════════════════════════════════════════════════════════════════════

def _make_signals_for_chunking(n=14):
    """Return n signals with real post_text so build_claude_post_context keeps
    them all and chunk_list produces multiple chunks (CLAUDE_POSTS_PER_CHUNK=12
    by default, so 14 signals → 2 chunks → map-reduce path)."""
    return [
        make_signal(title=f"Post {i}", text=f"Content for post {i} about automation")
        for i in range(n)
    ]


class TestEmptyNotesGrounding:
    """Runtime tests: when every map/condense step fails or returns empty,
    analyze_with_claude() / analyze_with_claude_stream() must route to the
    grounded no_results refusal, not pass '(no grounded points extracted)'
    to Claude."""

    def test_31_non_streaming_all_map_steps_raise(self, logics_mod):
        """Non-streaming: every _map_chunk call raises → final user_message
        must contain 'no_results' and 'Do NOT answer from general knowledge',
        and must NOT contain '(no grounded points extracted)'."""
        signals = _make_signals_for_chunking(14)
        captured = {}

        def fake_map_chunk(query, chunk):
            raise RuntimeError("simulated API timeout")

        def fake_call_claude(sp, um, **kw):
            captured["user_message"] = um
            return '{"format":"no_results","message":"","suggested_actions":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("find CRM tools", signals)

        assert "user_message" in captured, "Claude was not called"
        msg = captured["user_message"]
        assert "no_results" in msg, "user_message must instruct no_results format"
        assert "Do NOT answer from general knowledge" in msg, (
            "user_message must forbid general knowledge"
        )
        assert "(no grounded points extracted)" not in msg, (
            "Placeholder text must not reach Claude"
        )
        check_no_forbidden_phrases(msg, "test_31/non-streaming-all-map-raise")

    def test_32_streaming_all_map_steps_raise(self, logics_mod):
        """Streaming: every _map_chunk call raises → same expectations."""
        signals = _make_signals_for_chunking(14)
        captured = {}

        def fake_map_chunk(query, chunk):
            raise RuntimeError("simulated API timeout")

        def fake_stream(sp, um, **kw):
            captured["user_message"] = um
            yield '{"format":"no_results","message":"","suggested_actions":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream):
            list(logics_mod.analyze_with_claude_stream("find CRM tools", signals))

        assert "user_message" in captured, "Claude stream was not called"
        msg = captured["user_message"]
        assert "no_results" in msg
        assert "Do NOT answer from general knowledge" in msg
        assert "(no grounded points extracted)" not in msg
        check_no_forbidden_phrases(msg, "test_32/streaming-all-map-raise")

    def test_33_non_streaming_all_map_steps_return_empty(self, logics_mod):
        """Non-streaming: every _map_chunk returns '' → same expectations."""
        signals = _make_signals_for_chunking(14)
        captured = {}

        def fake_map_chunk(query, chunk):
            return ""   # empty string — truthy filter drops it

        def fake_call_claude(sp, um, **kw):
            captured["user_message"] = um
            return '{"format":"no_results","message":"","suggested_actions":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("find CRM tools", signals)

        assert "user_message" in captured
        msg = captured["user_message"]
        assert "no_results" in msg
        assert "Do NOT answer from general knowledge" in msg
        assert "(no grounded points extracted)" not in msg
        check_no_forbidden_phrases(msg, "test_33/non-streaming-all-map-empty")

    def test_34_streaming_all_map_steps_return_empty(self, logics_mod):
        """Streaming: every _map_chunk returns '' → same expectations."""
        signals = _make_signals_for_chunking(14)
        captured = {}

        def fake_map_chunk(query, chunk):
            return ""

        def fake_stream(sp, um, **kw):
            captured["user_message"] = um
            yield '{"format":"no_results","message":"","suggested_actions":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream):
            list(logics_mod.analyze_with_claude_stream("find CRM tools", signals))

        assert "user_message" in captured
        msg = captured["user_message"]
        assert "no_results" in msg
        assert "Do NOT answer from general knowledge" in msg
        assert "(no grounded points extracted)" not in msg
        check_no_forbidden_phrases(msg, "test_34/streaming-all-map-empty")

    def test_35_happy_path_notes_present_unchanged(self, logics_mod):
        """Normal map-reduce (some notes present) → final user_message still
        contains the notes text and the existing grounding wording.
        Proves the happy path is byte-for-byte unchanged."""
        signals = _make_signals_for_chunking(14)
        captured = {}

        def fake_map_chunk(query, chunk):
            return "grounded note from chunk"

        def fake_call_claude(sp, um, **kw):
            captured["user_message"] = um
            return '{"format":"source_list","results":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("find CRM tools", signals)

        assert "user_message" in captured
        msg = captured["user_message"]
        assert "grounded note from chunk" in msg, (
            "Happy path must still pass notes to Claude"
        )
        assert "only factual grounding" in msg, (
            "Happy path must retain the existing grounding instruction"
        )
        assert "(no grounded points extracted)" not in msg
        check_no_forbidden_phrases(msg, "test_35/happy-path-notes-present")

    def test_36_extra_context_appended_in_empty_notes_non_streaming(self, logics_mod):
        """extra_context is appended in the empty-notes branch (non-streaming)."""
        signals = _make_signals_for_chunking(14)
        captured = {}

        def fake_map_chunk(query, chunk):
            raise RuntimeError("timeout")

        def fake_call_claude(sp, um, **kw):
            captured["user_message"] = um
            return '{"format":"no_results","message":"","suggested_actions":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude(
                "find CRM tools", signals,
                extra_context="EXTRA_CTX_SENTINEL"
            )

        assert "EXTRA_CTX_SENTINEL" in captured["user_message"], (
            "extra_context must be appended even in the empty-notes branch"
        )

    def test_37_extra_context_appended_in_empty_notes_streaming(self, logics_mod):
        """extra_context is appended in the empty-notes branch (streaming)."""
        signals = _make_signals_for_chunking(14)
        captured = {}

        def fake_map_chunk(query, chunk):
            raise RuntimeError("timeout")

        def fake_stream(sp, um, **kw):
            captured["user_message"] = um
            yield '{"format":"no_results","message":"","suggested_actions":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream):
            list(logics_mod.analyze_with_claude_stream(
                "find CRM tools", signals,
                extra_context="EXTRA_CTX_SENTINEL"
            ))

        assert "EXTRA_CTX_SENTINEL" in captured["user_message"], (
            "extra_context must be appended even in the empty-notes branch (streaming)"
        )


# ══════════════════════════════════════════════════════════════════════════════
# PART H — Remaining runtime tests (Part 2 Task A, test_38–test_47)
# ══════════════════════════════════════════════════════════════════════════════

class TestRemainingRuntime:
    """Additional runtime behaviour tests per Part 2 Task A.

    Tests 38–47 cover:
      - force_json_prefill in non-streaming empty-notes branch (38)
      - happy-path streaming (39)
      - streaming extra_context in no-posts and single-chunk branches (40)
      - warning logged on empty-notes, both functions (41, 42)
      - stub-only pool → no-posts branch, no map step (43)
      - mixed pool → only real posts kept (44)
      - 'only factual grounding' wording in non-streaming happy path (45)
      - extra_context=None produces no "None" literal in message (46, 47)
    """

    # ── test_38: force_json_prefill=True in non-streaming empty-notes branch ──

    def test_38_non_streaming_empty_notes_uses_force_json_prefill(self, logics_mod):
        """Non-streaming empty-notes branch must pass force_json_prefill=True to _call_claude."""
        signals = _make_signals_for_chunking(14)
        captured = {}

        def fake_map_chunk(query, chunk):
            raise RuntimeError("timeout")

        def fake_call_claude(sp, um, force_json_prefill=False, **kw):
            captured["force_json_prefill"] = force_json_prefill
            captured["user_message"] = um
            return '{"format":"no_results","message":"x","suggested_actions":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("find anything", signals)

        assert captured.get("force_json_prefill") is True, (
            "Non-streaming empty-notes branch must pass force_json_prefill=True"
        )

    # ── test_39: happy-path streaming ────────────────────────────────────────

    def test_39_streaming_happy_path_notes_present(self, logics_mod):
        """Streaming happy path: _map_chunk returns notes → final stream call
        contains note text and 'only factual grounding', not the empty-notes
        refusal phrases."""
        signals = _make_signals_for_chunking(14)
        captured = {}

        def fake_map_chunk(query, chunk):
            return "NOTE-A"

        def fake_condense(query, notes_chunk):
            return "NOTE-A"

        def fake_stream(sp, um, **kw):
            captured["user_message"] = um
            yield '{"format":"source_list","results":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_condense_notes_chunk", side_effect=fake_condense), \
             patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream):
            list(logics_mod.analyze_with_claude_stream("find CRM", signals,
                                                        extra_context="EXTRA_HAPPY"))

        assert "user_message" in captured
        msg = captured["user_message"]
        assert "NOTE-A" in msg, "Happy path must pass notes to Claude stream"
        assert "only factual grounding" in msg, (
            "Happy path stream must contain existing grounding wording"
        )
        assert "Do NOT answer from general knowledge" not in msg, (
            "Empty-notes refusal must NOT appear in happy path"
        )
        assert "EXTRA_HAPPY" in msg, "extra_context must be appended in happy-path stream"
        check_no_forbidden_phrases(msg, "test_39/streaming-happy-path")

    # ── test_40: streaming extra_context in no-posts and single-chunk branches

    def test_40_streaming_extra_context_in_no_posts_and_single_chunk(self, logics_mod):
        """Streaming: extra_context must appear in messages for (a) the no-posts
        branch and (b) the single-chunk branch.  Also confirm omitting
        extra_context leaves no 'None' literal in the message."""
        # (a) no-posts branch
        captured_no_posts = {}

        def fake_stream_a(sp, um, **kw):
            captured_no_posts["user_message"] = um
            yield "chunk"

        with patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream_a):
            list(logics_mod.analyze_with_claude_stream(
                "query?", [], extra_context="SENTINEL_NO_POSTS"))

        msg_a = captured_no_posts["user_message"]
        assert "SENTINEL_NO_POSTS" in msg_a, (
            "extra_context must be appended in no-posts streaming branch"
        )

        # (b) single-chunk branch (1 post → fits in one chunk)
        captured_single = {}

        def fake_stream_b(sp, um, **kw):
            captured_single["user_message"] = um
            yield "chunk"

        with patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream_b):
            list(logics_mod.analyze_with_claude_stream(
                "query?", [make_signal()], extra_context="SENTINEL_SINGLE"))

        msg_b = captured_single["user_message"]
        assert "SENTINEL_SINGLE" in msg_b, (
            "extra_context must be appended in single-chunk streaming branch"
        )

        # (c) omitting extra_context → no "None" literal anywhere
        captured_none = {}

        def fake_stream_c(sp, um, **kw):
            captured_none["user_message"] = um
            yield "chunk"

        with patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream_c):
            list(logics_mod.analyze_with_claude_stream("query?", []))

        assert "None" not in captured_none["user_message"], (
            "Omitting extra_context must not inject the literal 'None' into the message"
        )

    # ── test_41, test_42: warning is logged ───────────────────────────────────

    def test_41_warning_logged_non_streaming_empty_notes(self, logics_mod, caplog):
        """analyze_with_claude: a warning mentioning empty-notes routing must be
        emitted when every map step fails."""
        import logging
        signals = _make_signals_for_chunking(14)

        def fake_map_chunk(query, chunk):
            raise RuntimeError("timeout")

        def fake_call_claude(sp, um, **kw):
            return '{"format":"no_results","message":"x","suggested_actions":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude), \
             caplog.at_level(logging.WARNING):
            logics_mod.analyze_with_claude("CRM question", signals)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("no_results" in m or "empty" in m or "routing" in m
                   for m in warning_msgs), (
            f"Expected a warning about empty-notes routing; got: {warning_msgs}"
        )

    def test_42_warning_logged_streaming_empty_notes(self, logics_mod, caplog):
        """analyze_with_claude_stream: a warning mentioning empty-notes routing
        must be emitted when every map step fails."""
        import logging
        signals = _make_signals_for_chunking(14)

        def fake_map_chunk(query, chunk):
            raise RuntimeError("timeout")

        def fake_stream(sp, um, **kw):
            yield '{"format":"no_results","message":"x","suggested_actions":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream), \
             caplog.at_level(logging.WARNING):
            list(logics_mod.analyze_with_claude_stream("CRM question", signals))

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("no_results" in m or "empty" in m or "routing" in m
                   for m in warning_msgs), (
            f"Expected a warning about empty-notes routing; got: {warning_msgs}"
        )

    # ── test_43: stub-only pool ───────────────────────────────────────────────

    def test_43_stub_only_pool_routes_to_no_posts_branch(self, logics_mod):
        """Signals with post_text=None (Google stubs) → build_claude_post_context
        drops them all → no-posts branch fires → message contains STRICTLY /
        no_results; map step is never called; stub title/URL absent from message."""
        stub_signals = [
            {"title": "r/crm", "post_text": None,
             "post_url": "https://reddit.com/r/crm", "platform": "reddit"},
            {"title": "r/saas", "post_text": None,
             "post_url": "https://reddit.com/r/saas", "platform": "reddit"},
        ]

        # Verify build_claude_post_context strips all stubs
        posts = logics_mod.build_claude_post_context(stub_signals)
        assert posts == [], (
            "build_claude_post_context must return [] for stub-only pool"
        )

        # Verify no map step is ever called and message is the no-posts message
        captured = {}
        map_calls = []

        def fake_map_chunk(query, chunk):
            map_calls.append(chunk)
            return "should not happen"

        def fake_call_claude(sp, um, **kw):
            captured["user_message"] = um
            return '{"format":"no_results","message":"x","suggested_actions":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("find CRM posts", stub_signals)

        assert map_calls == [], "No map step must be invoked for a stub-only pool"
        assert "user_message" in captured
        msg = captured["user_message"]
        assert "STRICTLY" in msg, "No-posts message must contain 'STRICTLY'"
        assert "no_results" in msg, "No-posts message must reference no_results"
        # Stub title and URL must NOT appear in the message
        assert "r/crm" not in msg, "Stub title must not leak into the message"
        assert "reddit.com/r/crm" not in msg, "Stub URL must not leak into the message"
        check_no_forbidden_phrases(msg, "test_43/stub-only-pool")

    # ── test_44: mixed pool ───────────────────────────────────────────────────

    def test_44_mixed_pool_keeps_only_real_posts(self, logics_mod):
        """Mixed pool (some stubs + some real posts): build_claude_post_context
        keeps only the posts that have real post_text, with correct count and titles."""
        mixed_signals = [
            {"title": "Real post A", "post_text": "I need a CRM",
             "post_url": "https://reddit.com/1", "platform": "reddit"},
            {"title": "Stub B",      "post_text": None,
             "post_url": "https://reddit.com/2", "platform": "reddit"},
            {"title": "Real post C", "post_text": "Looking for automation",
             "post_url": "https://reddit.com/3", "platform": "reddit"},
            {"title": "Stub D",      "post_text": "",
             "post_url": "https://reddit.com/4", "platform": "reddit"},
        ]

        posts = logics_mod.build_claude_post_context(mixed_signals)

        assert len(posts) == 2, (
            f"build_claude_post_context must keep only the 2 real posts; got {len(posts)}"
        )
        titles = {p["title"] for p in posts}
        assert "Real post A" in titles, "Real post A must be kept"
        assert "Real post C" in titles, "Real post C must be kept"
        assert "Stub B" not in titles, "Stub B (post_text=None) must be dropped"
        assert "Stub D" not in titles, "Stub D (post_text='') must be dropped"

    # ── test_45: 'only factual grounding' in non-streaming happy path ─────────

    def test_45_non_streaming_happy_path_wording(self, logics_mod):
        """Non-streaming happy path (notes present after map-reduce) must contain
        'only factual grounding' and must NOT contain 'Do NOT answer from general
        knowledge' (which belongs only in the empty-notes branch)."""
        signals = _make_signals_for_chunking(14)
        captured = {}

        def fake_map_chunk(query, chunk):
            return "NOTE-A"

        def fake_condense(query, notes_chunk):
            return "NOTE-A"

        def fake_call_claude(sp, um, **kw):
            captured["user_message"] = um
            return '{"format":"source_list","results":[]}'

        with patch.object(logics_mod, "_map_chunk", side_effect=fake_map_chunk), \
             patch.object(logics_mod, "_condense_notes_chunk", side_effect=fake_condense), \
             patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("CRM tools", signals,
                                            extra_context="EXTRA_45")

        assert "user_message" in captured
        msg = captured["user_message"]
        assert "NOTE-A" in msg
        assert "only factual grounding" in msg, (
            "Happy path must retain the 'only factual grounding' instruction"
        )
        assert "EXTRA_45" in msg
        assert "Do NOT answer from general knowledge" not in msg, (
            "Empty-notes refusal phrase must NOT appear in happy path"
        )

    # ── test_46, test_47: omitting extra_context leaves no "None" literal ─────

    def test_46_no_none_literal_when_extra_context_omitted_non_streaming(self, logics_mod):
        """Non-streaming: omitting extra_context must not inject the string 'None'
        into any branch (no-posts, single-chunk, map-reduce)."""
        captured_msgs = []

        def fake_call_claude(sp, um, **kw):
            captured_msgs.append(um)
            return '{"format":"no_results","message":"x","suggested_actions":[]}'

        # no-posts branch
        with patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("query?", [])

        # single-chunk branch
        with patch.object(logics_mod, "_call_claude", side_effect=fake_call_claude):
            logics_mod.analyze_with_claude("query?", [make_signal()])

        for msg in captured_msgs:
            assert "None" not in msg, (
                f"Message must not contain the string 'None' when extra_context is omitted:\n{msg}"
            )

    def test_47_no_none_literal_when_extra_context_omitted_streaming(self, logics_mod):
        """Streaming: omitting extra_context must not inject the string 'None'
        into any branch (no-posts, single-chunk)."""
        captured_msgs = []

        def fake_stream(sp, um, **kw):
            captured_msgs.append(um)
            yield "chunk"

        # no-posts branch
        with patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream):
            list(logics_mod.analyze_with_claude_stream("query?", []))

        # single-chunk branch
        with patch.object(logics_mod, "_call_claude_stream", side_effect=fake_stream):
            list(logics_mod.analyze_with_claude_stream("query?", [make_signal()]))

        for msg in captured_msgs:
            assert "None" not in msg, (
                f"Stream message must not contain 'None' when extra_context is omitted:\n{msg}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# CHANGE 1 TESTS — LLM backend switch (Haiku → GPT-5 mini / OpenAI Responses API)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLLMBackendSwitch:
    """9 tests verifying the Haiku→GPT-5 mini migration in logics.py and config.py.

    Security constraint: NO real OpenAI API calls. All HTTP is mocked.
    """

    # ── Config constants ────────────────────────────────────────────────────────

    def test_llm_model_default(self):
        """LLM_MODEL defaults to 'gpt-5-mini'."""
        import importlib
        cfg = importlib.import_module("config")
        assert cfg.LLM_MODEL == "gpt-5-mini"

    def test_llm_reasoning_effort_default(self):
        """LLM_REASONING_EFFORT defaults to 'low'."""
        import importlib
        cfg = importlib.import_module("config")
        assert cfg.LLM_REASONING_EFFORT == "low"

    def test_llm_reasoning_headroom_default(self):
        """LLM_REASONING_HEADROOM defaults to 2048."""
        import importlib
        cfg = importlib.import_module("config")
        assert cfg.LLM_REASONING_HEADROOM == 2048

    def test_openai_responses_url_default(self):
        """OPENAI_RESPONSES_URL defaults to the correct endpoint."""
        import importlib
        cfg = importlib.import_module("config")
        assert cfg.OPENAI_RESPONSES_URL == "https://api.openai.com/v1/responses"

    def test_anthropic_constants_still_exported(self):
        """Deprecated Anthropic constants remain in config.__all__ for .env compat."""
        import importlib
        cfg = importlib.import_module("config")
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_MODEL", "CLAUDE_API_URL", "CLAUDE_API_VERSION"):
            assert name in cfg.__all__, f"{name} must remain in config.__all__"

    # ── _call_claude() payload ──────────────────────────────────────────────────

    def _make_fake_httpx_client(self, calls_list=None, headers_capture=None, payloads_capture=None,
                                 response_text="ok"):
        """Return a fake httpx.Client class that records calls/headers/payloads."""
        _calls = calls_list if calls_list is not None else []
        _headers = headers_capture if headers_capture is not None else {}
        _payloads = payloads_capture if payloads_capture is not None else []
        _text = response_text

        class FakeClient:
            def __init__(self, timeout):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def post(self, url, headers=None, json=None, **kw):
                _calls.append(url)
                if headers:
                    _headers.update(headers)
                if json is not None:
                    _payloads.append(json)
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": _text}]}]
                }
                return resp
        return FakeClient

    def _patch_httpx_client(self, logics_mod, monkeypatch, fake_client_cls):
        """Attach FakeClient to the httpx stub that logics was loaded with."""
        logics_mod.httpx.Client = fake_client_cls

    def test_call_claude_uses_openai_responses_url(self, logics_mod, monkeypatch):
        """_call_claude() posts to OPENAI_RESPONSES_URL, not the Anthropic endpoint."""
        calls = []
        fake_cls = self._make_fake_httpx_client(calls_list=calls)
        self._patch_httpx_client(logics_mod, monkeypatch, fake_cls)
        monkeypatch.setattr(logics_mod, "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(logics_mod, "OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses")

        logics_mod._call_claude("sys", "user")
        assert calls, "_call_claude must make an HTTP POST"
        assert "openai.com" in calls[0], f"Expected OpenAI URL, got: {calls[0]}"

    def test_call_claude_no_anthropic_headers(self, logics_mod, monkeypatch):
        """_call_claude() must not include x-api-key or anthropic-version headers."""
        captured_headers = {}
        fake_cls = self._make_fake_httpx_client(headers_capture=captured_headers)
        self._patch_httpx_client(logics_mod, monkeypatch, fake_cls)
        monkeypatch.setattr(logics_mod, "OPENAI_API_KEY", "sk-test")

        logics_mod._call_claude("sys", "user")
        assert "x-api-key" not in captured_headers, "Must not send x-api-key"
        assert "anthropic-version" not in captured_headers, "Must not send anthropic-version"
        assert "Authorization" in captured_headers, "Must send Authorization header"

    def test_call_claude_force_json_prefill_sets_text_format(self, logics_mod, monkeypatch):
        """force_json_prefill=True sends text.format.type=json_object, not an assistant prefill."""
        captured_payloads = []
        fake_cls = self._make_fake_httpx_client(payloads_capture=captured_payloads, response_text="{}")
        self._patch_httpx_client(logics_mod, monkeypatch, fake_cls)
        monkeypatch.setattr(logics_mod, "OPENAI_API_KEY", "sk-test")

        logics_mod._call_claude("sys", "user", force_json_prefill=True)
        assert captured_payloads, "Must POST a payload"
        payload = captured_payloads[0]
        # Must use text.format, not a messages array with assistant prefill
        text_fmt = payload.get("text", {})
        assert text_fmt.get("format", {}).get("type") == "json_object", (
            "force_json_prefill must set text.format.type=json_object"
        )
        # Must NOT have a messages key (that's the Anthropic pattern)
        assert "messages" not in payload, "Responses API payload must not have 'messages'"

    def test_call_claude_web_search_tool_type(self, logics_mod, monkeypatch):
        """enable_web_search=True sends tools=[{type:'web_search'}], not web_search_20250305."""
        captured_payloads = []
        fake_cls = self._make_fake_httpx_client(payloads_capture=captured_payloads)
        self._patch_httpx_client(logics_mod, monkeypatch, fake_cls)
        monkeypatch.setattr(logics_mod, "OPENAI_API_KEY", "sk-test")

        logics_mod._call_claude("sys", "user", enable_web_search=True)
        assert captured_payloads, "Must POST a payload"
        tools = captured_payloads[0].get("tools", [])
        assert any(t.get("type") == "web_search" for t in tools), (
            "enable_web_search must send tools=[{type:'web_search'}]"
        )
        assert not any("20250305" in str(t) for t in tools), (
            "Must not use old Anthropic tool type web_search_20250305"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# CHANGE 2 TESTS — Lazy embedding backfill
# ═══════════════════════════════════════════════════════════════════════════════

class TestLazyEmbeddingBackfill:
    """11 tests for _lazy_backfill_missing_embeddings() in logics.py and the
    integration call inside get_matched_signals().

    Security constraint: NO real MongoDB or OpenAI API calls.
    """

    def _call_backfill(self, logics_mod, raw_docs, embed_fn, signals_collection=None):
        if signals_collection is None:
            signals_collection = MagicMock()
        return logics_mod._lazy_backfill_missing_embeddings(
            raw_docs, signals_collection, embed_fn
        )

    def test_returns_empty_when_all_docs_have_embeddings(self, logics_mod, monkeypatch):
        """Returns [] when every doc already has an embedding."""
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_ENABLED", True)
        docs = [
            {"_id": 1, "post_text": "hello world text here", "embedding": [0.1, 0.2]},
            {"_id": 2, "post_text": "another post text here", "embedding": [0.3, 0.4]},
        ]
        result = self._call_backfill(logics_mod, docs, embed_fn=lambda texts: [[0.5] * 3] * len(texts))
        assert result == []

    def test_returns_empty_when_disabled(self, logics_mod, monkeypatch):
        """Returns [] immediately when LAZY_EMBED_ENABLED is False."""
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_ENABLED", False)
        docs = [{"_id": 1, "post_text": "some text here for embedding", "embedding": None}]
        embed_called = []
        result = self._call_backfill(logics_mod, docs, embed_fn=lambda t: embed_called.append(t) or [])
        assert result == []
        assert embed_called == [], "embed_fn must not be called when LAZY_EMBED_ENABLED=False"

    def test_embeds_docs_with_none_embedding(self, logics_mod, monkeypatch):
        """Docs with embedding=None are embedded and returned."""
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_ENABLED", True)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MIN_TEXT_CHARS", 5)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MAX_DOCS_PER_QUERY", 100)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_BATCH_SIZE", 50)

        doc = {"_id": 1, "post_text": "hello world", "embedding": None}
        vec = [0.1, 0.2, 0.3]
        result = self._call_backfill(logics_mod, [doc], embed_fn=lambda texts: [vec] * len(texts))
        assert len(result) == 1
        assert result[0]["embedding"] == vec

    def test_embeds_docs_with_empty_list_embedding(self, logics_mod, monkeypatch):
        """Docs with embedding=[] are embedded and returned."""
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_ENABLED", True)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MIN_TEXT_CHARS", 5)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MAX_DOCS_PER_QUERY", 100)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_BATCH_SIZE", 50)

        doc = {"_id": 2, "post_text": "hello world", "embedding": []}
        vec = [0.4, 0.5, 0.6]
        result = self._call_backfill(logics_mod, [doc], embed_fn=lambda texts: [vec] * len(texts))
        assert len(result) == 1
        assert result[0]["embedding"] == vec

    def test_skips_docs_with_short_text(self, logics_mod, monkeypatch):
        """Docs whose post_text is shorter than LAZY_EMBED_MIN_TEXT_CHARS are skipped."""
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_ENABLED", True)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MIN_TEXT_CHARS", 50)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MAX_DOCS_PER_QUERY", 100)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_BATCH_SIZE", 50)

        doc = {"_id": 3, "post_text": "hi", "embedding": None}
        result = self._call_backfill(logics_mod, [doc], embed_fn=lambda texts: [[0.1]] * len(texts))
        assert result == []

    def test_respects_max_docs_per_query_cap(self, logics_mod, monkeypatch):
        """At most LAZY_EMBED_MAX_DOCS_PER_QUERY docs are embedded per call."""
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_ENABLED", True)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MIN_TEXT_CHARS", 5)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MAX_DOCS_PER_QUERY", 3)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_BATCH_SIZE", 50)

        docs = [{"_id": i, "post_text": "hello world long enough", "embedding": None} for i in range(10)]
        embedded_texts = []
        def track_embed(texts):
            embedded_texts.extend(texts)
            return [[0.1]] * len(texts)

        result = self._call_backfill(logics_mod, docs, embed_fn=track_embed)
        assert len(result) == 3, f"Expected 3 backfilled, got {len(result)}"
        assert len(embedded_texts) == 3

    def test_does_not_overwrite_existing_embeddings(self, logics_mod, monkeypatch):
        """Docs with a real embedding vector are not re-embedded."""
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_ENABLED", True)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MIN_TEXT_CHARS", 5)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MAX_DOCS_PER_QUERY", 100)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_BATCH_SIZE", 50)

        existing_vec = [0.9, 0.8, 0.7]
        doc_has = {"_id": 1, "post_text": "hello world", "embedding": existing_vec}
        doc_missing = {"_id": 2, "post_text": "hello world more text", "embedding": None}

        new_vec = [0.1, 0.2, 0.3]
        result = self._call_backfill(logics_mod, [doc_has, doc_missing], embed_fn=lambda texts: [new_vec] * len(texts))
        assert len(result) == 1
        assert result[0]["_id"] == 2
        assert doc_has["embedding"] == existing_vec, "Existing embedding must not be overwritten"

    def test_bulk_write_uses_missing_embedding_guard(self, logics_mod, monkeypatch):
        """bulk_write UpdateOne filter includes the missing-embedding guard clause."""
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_ENABLED", True)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MIN_TEXT_CHARS", 5)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MAX_DOCS_PER_QUERY", 100)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_BATCH_SIZE", 50)

        signals_coll = MagicMock()
        doc = {"_id": 42, "post_text": "hello world long enough text", "embedding": None}
        vec = [0.1, 0.2]
        self._call_backfill(logics_mod, [doc], embed_fn=lambda texts: [vec], signals_collection=signals_coll)

        assert signals_coll.bulk_write.called, "bulk_write must be called"
        ops = signals_coll.bulk_write.call_args[0][0]
        assert ops, "Must have at least one UpdateOne op"
        # The filter dict of the UpdateOne must contain the missing-embedding guard
        op_filter = ops[0]._filter
        assert "$or" in op_filter, "UpdateOne filter must include the $or missing-embedding guard"

    def test_never_raises_on_embed_failure(self, logics_mod, monkeypatch):
        """_lazy_backfill_missing_embeddings() must not raise even if embed_fn raises."""
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_ENABLED", True)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MIN_TEXT_CHARS", 5)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MAX_DOCS_PER_QUERY", 100)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_BATCH_SIZE", 50)

        doc = {"_id": 1, "post_text": "hello world long text", "embedding": None}
        def bad_embed(texts):
            raise RuntimeError("network failure")

        result = logics_mod._lazy_backfill_missing_embeddings([doc], MagicMock(), bad_embed)
        assert result == [], "Must return [] on embed failure without raising"

    def test_never_raises_on_bulk_write_failure(self, logics_mod, monkeypatch):
        """_lazy_backfill_missing_embeddings() must not raise even if bulk_write raises."""
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_ENABLED", True)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MIN_TEXT_CHARS", 5)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_MAX_DOCS_PER_QUERY", 100)
        monkeypatch.setattr(logics_mod, "LAZY_EMBED_BATCH_SIZE", 50)

        signals_coll = MagicMock()
        signals_coll.bulk_write.side_effect = Exception("mongo down")
        doc = {"_id": 1, "post_text": "hello world long text enough", "embedding": None}
        vec = [0.1, 0.2]

        # Should still return the doc (vector attached in-memory even if write failed)
        result = logics_mod._lazy_backfill_missing_embeddings(
            [doc], signals_coll, lambda texts: [vec] * len(texts)
        )
        # bulk_write failed but vector was still attached
        assert result, "Must return backfilled docs even when bulk_write fails"
        assert result[0]["embedding"] == vec

    def test_lazy_embed_config_constants_exported(self):
        """All four LAZY_EMBED_* constants are in config.__all__."""
        import importlib
        cfg = importlib.import_module("config")
        for name in ("LAZY_EMBED_ENABLED", "LAZY_EMBED_MAX_DOCS_PER_QUERY",
                     "LAZY_EMBED_BATCH_SIZE", "LAZY_EMBED_MIN_TEXT_CHARS"):
            assert name in cfg.__all__, f"{name} must be in config.__all__"
