"""
tests/test_templates_render.py
================================
Smoke-tests for TemplateResponse signature migration.

Verifies that TemplateResponse calls in routes.py use the new
Starlette 0.38+ signature: TemplateResponse(request, name, context)
instead of the old TemplateResponse(name, {"request": request, ...}).

Strategy:
- Grep-based structural tests (no import of logics/routes needed) that
  confirm the old signature pattern is gone from routes.py.
- The actual runtime render path cannot be tested here without a full
  Starlette test client, which would require jinja2 templates on disk
  and a live Mongo connection. Those are integration-level concerns;
  the structural tests cover the migration correctness.
"""
import re
from pathlib import Path

import pytest

ROUTES_PY = Path(__file__).resolve().parent.parent / "routes.py"


def _read_routes():
    if not ROUTES_PY.exists():
        pytest.skip("routes.py not found — skip")
    return ROUTES_PY.read_text(encoding="utf-8")


class TestTemplateResponseMigration:
    """Structural checks — new Starlette 0.38 TemplateResponse signature."""

    def test_old_signature_absent(self):
        """No TemplateResponse("template.html", {"request": request, ...}) pattern."""
        src = _read_routes()
        # Old signature: first arg is a string literal (template name)
        old_pattern = re.compile(
            r'TemplateResponse\(\s*["\']',  # TemplateResponse("... or TemplateResponse('...
        )
        matches = old_pattern.findall(src)
        assert not matches, (
            f"Found {len(matches)} old-style TemplateResponse call(s) "
            f"(first arg is template name string). Migrate to "
            f"TemplateResponse(request, name, context)."
        )

    def test_new_signature_present(self):
        """At least one TemplateResponse call uses the new (request, name, ...) form."""
        src = _read_routes()
        # New signature: first arg is `request` (identifier, not string)
        new_pattern = re.compile(
            r'TemplateResponse\(\s*request\s*,',
        )
        matches = new_pattern.findall(src)
        assert matches, (
            "Expected at least one TemplateResponse(request, ...) call in routes.py"
        )

    def test_all_calls_use_new_signature(self):
        """Every TemplateResponse call uses request as first positional arg."""
        src = _read_routes()
        all_calls = re.findall(r'TemplateResponse\(', src)
        new_calls = re.findall(r'TemplateResponse\(\s*request\s*,', src)
        assert len(all_calls) == len(new_calls), (
            f"Mismatch: {len(all_calls)} total calls but only "
            f"{len(new_calls)} use the new (request, name, context) signature."
        )

    def test_no_request_key_in_context_dict(self):
        """Context dicts must not contain 'request': request after migration."""
        src = _read_routes()
        # Look for "request": request inside a TemplateResponse block
        pattern = re.compile(r'"request"\s*:\s*request')
        matches = pattern.findall(src)
        assert not matches, (
            f"Found {len(matches)} occurrence(s) of '\"request\": request' "
            f"inside TemplateResponse context. Remove it — new API injects "
            f"request automatically."
        )

    def test_nine_template_response_calls_migrated(self):
        """Exactly 9 TemplateResponse calls in routes.py (all migrated)."""
        src = _read_routes()
        all_calls = re.findall(r'templates\.TemplateResponse\(', src)
        assert len(all_calls) == 9, (
            f"Expected 9 TemplateResponse calls, found {len(all_calls)}."
        )
        new_calls = re.findall(r'templates\.TemplateResponse\(\s*request\s*,', src)
        assert len(new_calls) == 9, (
            f"Expected all 9 to use new signature, "
            f"only {len(new_calls)} do."
        )
