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


# ─────────────────────────────────────────────────────────────────────────────
# Turn status chip: running timer (templates/chat.html)
# ─────────────────────────────────────────────────────────────────────────────
import json
import shutil
import subprocess
from datetime import datetime

CHAT_HTML = Path(__file__).resolve().parent.parent / "templates" / "chat.html"


def _chat_src():
    if not CHAT_HTML.exists():
        pytest.skip("templates/chat.html not found — skip")
    return CHAT_HTML.read_text(encoding="utf-8")


def _render_chip_block(msg):
    """Render ONLY the status-chip fragment of chat.html with real Jinja."""
    jinja2 = pytest.importorskip("jinja2")
    src = _chat_src()
    start = src.index("{% if msg.claude_answer is none %}")
    end = src.index("{% endif %}", src.index("status-completed\"><span class=\"status-dot\">", start)) + len("{% endif %}")
    return jinja2.Template(src[start:end]).render(msg=msg)


class _Msg:
    def __init__(self, claude_answer, requested_at):
        self.claude_answer = claude_answer
        self.requested_at = requested_at


class TestTurnTimerChipJinja:
    def test_pending_chip_has_timer_and_requested_at(self):
        html = _render_chip_block(_Msg(None, datetime(2026, 10, 4, 10, 15, 0)))
        assert "turn-timer" in html
        assert 'data-requested-at="2026-10-04T10:15:00"' in html
        assert "status-pending" in html and "Searching…" in html   # server-rendered, no blank flash
        assert 'aria-live="off"' in html

    def test_pending_chip_without_requested_at_still_renders_searching(self):
        html = _render_chip_block(_Msg(None, None))
        assert 'data-requested-at=""' in html and "Searching…" in html

    def test_answered_chip_is_plain_completed(self):
        html = _render_chip_block(_Msg("{}", datetime(2026, 10, 4, 10, 15, 0)))
        assert "status-completed" in html and "Completed" in html
        assert "turn-timer" not in html and "data-requested-at" not in html

    def test_chat_and_index_wiring_present(self):
        # (UI BUGS FIX) index.html used to be asserted "untouched" here. That
        # was the bug: chat.html's script never runs after a soft-nav swap
        # from the home page, so index.html now carries the same timer JS.
        src = _chat_src()
        assert "finishTurnTimer(pendingTurn, 'completed'" in src
        assert src.count("finishTurnTimer(pendingTurn, 'failed'") == 2    # payload.error + onerror
        index = CHAT_HTML.parent / "index.html"
        if index.exists():
            isrc = index.read_text(encoding="utf-8")
            assert "finishTurnTimer(pendingTurn, 'completed'" in isrc
            assert isrc.count("finishTurnTimer(pendingTurn, 'failed'") == 2
            assert "function initTurnTimers" in isrc


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestTurnTimerHelpersJs:
    """Runs the real helper block extracted from chat.html in node."""

    def _run(self, expr):
        src = _chat_src()
        a = src.index("// TURN-TIMER-HELPERS-BEGIN")
        b = src.index("// TURN-TIMER-HELPERS-END")
        code = src[a:b] + f"\nconsole.log(JSON.stringify({expr}));"
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_format_ago(self):
        got = self._run("[0,1,2,12,59,60,61,72,125,120].map(formatTurnAgo)")
        assert got == ["0 seconds ago", "1 second ago", "2 seconds ago", "12 seconds ago",
                       "59 seconds ago", "1m 0s ago", "1m 1s ago", "1m 12s ago", "2m 5s ago", "2m 0s ago"]
        assert self._run("formatTurnAgo(-5)") == "0 seconds ago"          # clamped

    def test_format_total(self):
        assert self._run("[14,59,60,72].map(formatTurnDuration)") == ["14s", "59s", "1m 0s", "1m 12s"]

    def test_naive_iso_is_treated_as_utc(self):
        # naive string must equal the explicit-Z one, regardless of the machine's timezone
        for tz in ("Asia/Karachi", "America/Los_Angeles", "UTC"):
            src = _chat_src()
            a = src.index("// TURN-TIMER-HELPERS-BEGIN"); b = src.index("// TURN-TIMER-HELPERS-END")
            code = src[a:b] + ("\nconsole.log(JSON.stringify([parseRequestedAt('2026-10-04T10:15:00')===Date.UTC(2026,9,4,10,15,0),"
                               "parseRequestedAt('2026-10-04T10:15:00.123456')===Date.UTC(2026,9,4,10,15,0,123),"
                               "parseRequestedAt('2026-10-04T10:15:00Z')===Date.UTC(2026,9,4,10,15,0),"
                               "parseRequestedAt('2026-10-04T15:15:00+05:00')===Date.UTC(2026,9,4,10,15,0),"
                               "parseRequestedAt('2026-10-04 10:15:00')===Date.UTC(2026,9,4,10,15,0)]));")
            import os
            out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=20,
                                 env={**os.environ, "TZ": tz})
            assert out.returncode == 0, out.stderr
            assert json.loads(out.stdout) == [True] * 5, tz

    def test_bad_input_is_nan_and_elapsed_never_negative(self):
        assert self._run("[parseRequestedAt(''), parseRequestedAt(null), parseRequestedAt('garbage')].map(Number.isNaN)") == [True, True, True]
        assert self._run("turnElapsedSeconds(NaN, 1000)") is None           # NaN -> null in JSON
        assert self._run("turnElapsedSeconds(5000, 1000)") == 0             # future start clamps to 0
        assert self._run("turnElapsedSeconds(0, 61500)") == 61
