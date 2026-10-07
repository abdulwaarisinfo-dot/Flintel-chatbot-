"""
tests/test_index_ui_fixes.py
=============================
UI BUGS FIX: templates/index.html must carry (a) the turn-chip timer JS and
(b) the optimistic turn, because chat.html's own <script> never runs when a
chat's markup arrives through index.html's navigateTo() innerHTML swap.

Static checks on the template files (no browser). The timer helper block is
additionally EXECUTED with node when node is available.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


@pytest.fixture(scope="module")
def index_src():
    return (TEMPLATES / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chat_src():
    return (TEMPLATES / "chat.html").read_text(encoding="utf-8")


def _between(src, start, end):
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


# ── timer chip ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "parseRequestedAt", "turnElapsedSeconds", "formatTurnDuration", "formatTurnAgo",
    "runningTurnChips", "turnTimerTick", "initTurnTimers", "finishTurnTimer",
])
def test_index_defines_timer_function(index_src, name):
    assert len(re.findall(rf"function\s+{name}\s*\(", index_src)) == 1, name


def test_index_has_single_timer_interval_variable(index_src):
    assert len(re.findall(r"let\s+turnTimerInterval\b", index_src)) == 1


def test_timer_helper_block_identical_to_chat_html(index_src, chat_src):
    a = _between(index_src, "// TURN-TIMER-HELPERS-BEGIN", "// TURN-TIMER-HELPERS-END")
    b = _between(chat_src, "// TURN-TIMER-HELPERS-BEGIN", "// TURN-TIMER-HELPERS-END")
    assert a == b


def test_done_handler_freezes_chip_as_completed(index_src):
    done = _between(index_src, "if (payload.done) {", "if (payload.error) {")
    assert "finishTurnTimer(pendingTurn, 'completed', payload.requested_at)" in done
    # only AFTER the stale-navigation return, like chat.html
    assert done.index("myGen !== softNavGeneration") < done.index("finishTurnTimer(")


def test_error_and_onerror_mark_chip_failed(index_src):
    err = _between(index_src, "if (payload.error) {", "source.onerror")
    assert "finishTurnTimer(pendingTurn, 'failed', payload.requested_at)" in err
    onerr = _between(index_src, "source.onerror = () => {", "function wireUpInitialPendingStream")
    assert "finishTurnTimer(pendingTurn, 'failed')" in onerr


def test_init_turn_timers_called_after_swap_load_and_soft_submit(index_src):
    nav = _between(index_src, "async function navigateTo(", "window.addEventListener('popstate'")
    assert "initTurnTimers();" in nav and nav.index("initSearchProgressBlocks();") < nav.index("initTurnTimers();")
    soft = _between(index_src, "function wireUpSoftSearchSubmit()", "function wireUpHeroSoftSearchSubmit()")
    assert "initTurnTimers();" in soft
    tail = index_src[index_src.index("wireUpHeroSoftSearchSubmit();\n  // (PROGRESS-BAR FIX) Real initial-page-load"):]
    assert "initTurnTimers();" in tail[:600]


def _node_run(script):
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_timer_helpers_format_and_utc_parsing_in_node(index_src):
    block = _between(index_src, "// TURN-TIMER-HELPERS-BEGIN", "// TURN-TIMER-HELPERS-END")
    out = _node_run(block + """
    const f = formatTurnAgo, d = formatTurnDuration;
    // naive UTC string (no Z) must be read as UTC, not local time
    const naive = parseRequestedAt('2026-10-04T10:15:00.123456');
    const withZ = parseRequestedAt('2026-10-04T10:15:00.123Z');
    console.log(JSON.stringify({
      ago: [0, 1, 2, 12, 59, 60, 61, 125].map(f),
      dur: [14, 59, 60, 61, 125].map(d),
      naiveEqualsZ: naive === withZ,
      empty: [parseRequestedAt(''), parseRequestedAt(null), parseRequestedAt(undefined)].map(Number.isNaN),
      elapsedNaN: Number.isNaN(turnElapsedSeconds(NaN)),
      elapsed: turnElapsedSeconds(1000, 13500),
    }));
    """)
    assert out["ago"] == ["0 seconds ago", "1 second ago", "2 seconds ago", "12 seconds ago",
                          "59 seconds ago", "1m 0s ago", "1m 1s ago", "2m 5s ago"]
    assert out["dur"] == ["14s", "59s", "1m 0s", "1m 1s", "2m 5s"]
    assert out["naiveEqualsZ"] is True
    assert out["empty"] == [True, True, True]
    assert out["elapsedNaN"] is True and out["elapsed"] == 12


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_scripts_still_parse(index_src, tmp_path):
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", index_src, re.S)
    assert scripts
    for n, body in enumerate(scripts):
        if "{{" in body or "{%" in body:     # jinja inside the script: cannot be parsed raw
            continue
        f = tmp_path / f"s{n}.js"
        f.write_text(body, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, r.stderr


# ── optimistic turn ─────────────────────────────────────────────────────

def test_optimistic_turn_helpers_exist(index_src):
    for name in ("createOptimisticTurn", "showHeroOptimisticTurn", "clearHeroOptimisticTurn"):
        assert len(re.findall(rf"function\s+{name}\s*\(", index_src)) == 1, name
    body = _between(index_src, "function createOptimisticTurn(", "function showHeroOptimisticTurn(")
    assert "question.textContent = query" in body          # user text never goes through innerHTML
    assert "turn-question" in body and "loading-row" in body and "showNeutralWaitRow(" in body and "showSearchProgressPlaceholder(" not in body
    assert "Still gathering" not in body and "loading-dots" not in body   # PROGRESS-FIRST UI: no plain waiting row


def test_hero_submit_shows_optimistic_turn_and_restores_on_busy_decline(index_src):
    hero = _between(index_src, "if (e.target.id !== 'search-form') return;", "// Docked follow-up input lives OUTSIDE")
    assert "showHeroOptimisticTurn(query)" in hero
    # FormData is captured BEFORE the hero is hidden (a hidden field is still in FormData, but keep the order explicit)
    assert hero.index("new FormData(form)") < hero.index("showHeroOptimisticTurn(query)")
    assert hero.index("showHeroOptimisticTurn(query)") < hero.index("navigateTo(form.action")
    busy = _between(hero, "onBusyDecline:", "});")
    assert "clearHeroOptimisticTurn(heroOptimistic)" in busy
    assert "showSearchInlineError(errorText, query)" in busy
    assert "heroSubmitInFlight = 'false'" in busy
    assert "heroSubmitInFlight === 'true'" in hero          # double-submit guard unchanged


def test_hero_optimistic_wrapper_is_not_thread_inner(index_src):
    # .thread-inner would make the delegated hero handler skip the form (real submit)
    body = _between(index_src, "function showHeroOptimisticTurn(", "function clearHeroOptimisticTurn(")
    assert "optimistic-hero-thread" in body
    assert "'thread-inner'" not in body and '"thread-inner"' not in body


def test_follow_up_submit_has_optimistic_turn_and_cleanup(index_src):
    soft = _between(index_src, "function wireUpSoftSearchSubmit()", "function wireUpHeroSoftSearchSubmit()")
    assert "createOptimisticTurn(query)" in soft
    assert soft.index("createOptimisticTurn(query)") < soft.index("await fetch(form.action")
    # removed: stale response, busy-decline, real turn arrives, DOM-update failure
    assert soft.count("optimisticTurn.remove()") >= 4
    stale = _between(soft, "myGen !== softNavGeneration", "(BUSY-LOCK)")
    assert "optimisticTurn.remove()" in stale
    busy = _between(soft, "const errorCard", "history.pushState")
    assert busy.index("optimisticTurn.remove()") < busy.index("showSearchInlineError(")
    assert soft.index("optimisticTurn.remove();\n        liveThreadInner.appendChild(latestTurn)") > 0


def test_chat_html_is_untouched_by_this_fix(chat_src):
    # chat.html already had both (sanity: the reference implementation is still there)
    assert "TURN-TIMER-HELPERS-BEGIN" in chat_src and "optimisticTurn" in chat_src
