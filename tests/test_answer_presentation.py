"""(ANSWER PRESENTATION) Presentation-only change: prompt wording + source_list/comparison
renderer. These checks pin that the evidence/grounding rules survived and that the renderer
shows the source fields as given, without the old report-label rows."""
import ast, pathlib, re
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _prompt():
    src = (ROOT / "logics.py").read_text(encoding="utf-8")
    for n in ast.parse(src).body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "CLAUDE_ANALYSIS_SYSTEM_PROMPT" \
                and isinstance(n.value, ast.Constant):
            return n.value.value
    raise AssertionError("prompt literal not found")


P = _prompt()


@pytest.mark.parametrize("must_keep", [
    "STEP 7 — PAIN POINT ≠ BUYING INTENT",
    "STEP 8 — GROUNDING",
    "invent a post, a stat, a quote",
    'PREFER RELATED SIGNALS OVER "no_results"',
    "POST-COUNT LIMIT: never include more than 7 posts total",
    "up to 7 by genuine relevance",
    "REFERENCES RULE",
    "SENTIMENT TAG RULE",
    '"supporting_post_indices"',
    '"link": "<real post URL if available',
])
def test_evidence_and_grounding_rules_unchanged(must_keep):
    assert must_keep in P


@pytest.mark.parametrize("rule", [
    '"headline"', "Lead with the answer", "Quantify honestly",
    'a single post asking something is "one user"', "No time claims",
    'Never write report labels in the text', "Avoid consultant phrasing",
    "Never write\ninvented slogans", "Adapt the length to the question",
])
def test_new_communication_rules_present(rule):
    assert rule in P


def test_json_field_names_backward_compatible():
    for f in ("executive_summary", "key_findings", "evidence_type", "signal_strength", "impact",
              "detailed_findings", "market_pattern", "conclusion", "business_insight", "followup_question",
              "platforms", "total_analyzed", "shown_count"):
        assert f'"{f}"' in P


@pytest.mark.parametrize("name", ["chat", "index"])
def test_renderer_layout(name):
    s = (ROOT / f"templates/{name}.html").read_text(encoding="utf-8")
    a = s.index("// ---- (ANSWER PRESENTATION) source_list / comparison layout")
    body = s[a:s.index("function renderTrendReport(")]
    code = re.sub(r"//.*", "", body)
    # no report-label rows, no restated request
    for label in ("'Evidence type'", "'Evidence Type'", "'Signal strength'", "'Signal Strength'",
                  "'Impact'", "'Conclusion'", "'Business Insight'", "'Market Pattern'", "research_objective"):
        assert label not in code, label
    # source fields rendered as given, link safe
    for f in ("post.title", "post.link", "post.summary", "post.sentiment", "post.google_rank",
              "p.shown_count", "p.total_analyzed"):
        assert f in code, f
    assert "a.rel = 'noopener noreferrer'" in code and "a.href = post.link" in code
    assert "innerHTML" not in code                      # text only; **bold** -> <strong> nodes
    assert "renderSourceList(" in code and "renderComparison(" in code
    assert "ap-table-wrap" in s and "overflow-x: auto" in s[s.index(".answer-rendered .ap-table-wrap"):][:200]
