"""
tests/test_hackernews_registry.py — Hacker News as a selectable platform: registry, selection keys, config flag, dropdown.
Flag OFF must leave every registry / selection / dropdown fact exactly as before. All offline.
"""
import copy
import os
import re
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest

import platforms

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def hn_on():
    """Hacker News registered (flag on) for one test; always restored."""
    before = dict(platforms.REGISTRY)
    platforms.apply_flags(hackernews=True)
    try:
        yield platforms
    finally:
        platforms.apply_flags(hackernews=False)
        assert dict(platforms.REGISTRY) == before


# ── flag off = unchanged ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_flag_off_registry_and_selection_are_exactly_the_old_ones():
    assert list(platforms.REGISTRY) == ["x_twitter", "reddit", "linkedin", "facebook"]
    assert platforms.enabled_keys() == ("reddit",)
    assert platforms.selection_key("Hacker News") == "all" and platforms.selection_key("hn") == "all"       # unrecognised, as before
    assert platforms.selection_key("x_twitter,reddit,linkedin,facebook") == "all"
    assert platforms.expand_selection("all") == ("reddit",)
    assert platforms.canonical_key("hackernews") is None and platforms.from_url("https://news.ycombinator.com/item?id=1") is None


def test_apply_flags_is_idempotent_and_reversible():
    before = dict(platforms.REGISTRY)
    platforms.apply_flags(hackernews=False)
    assert dict(platforms.REGISTRY) == before
    platforms.apply_flags(hackernews=True)
    platforms.apply_flags(hackernews=True)
    assert list(platforms.REGISTRY) == ["x_twitter", "reddit", "hackernews", "linkedin", "facebook"]
    platforms.apply_flags(hackernews=False)
    assert dict(platforms.REGISTRY) == before and list(platforms.REGISTRY) == list(before)


# ── flag on ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_registered_platform_facts(hn_on):
    p = platforms.get("hackernews")
    assert p.label == "Hacker News" and p.enabled is True and p.aliases == ("hackernews", "hacker news", "hn")
    assert p.hosts == ("news.ycombinator.com",) and p.community_label is None and p.community_prefix is None
    assert p.earliest_date is None and platforms.earliest_date("hackernews") == platforms.DEFAULT_EARLIEST_DATE   # no invented floor
    assert platforms.community_display("hackernews") == ("Source", "")
    assert platforms.label("hackernews") == "Hacker News"
    assert platforms.enabled_keys() == ("reddit", "hackernews")


@pytest.mark.parametrize("raw,want", [
    ("Hacker News", "hackernews"), ("hn", "hackernews"), ("hackernews", "hackernews"),
    ("Reddit, Hacker News", "reddit,hackernews"), ("hackernews,reddit", "reddit,hackernews"), ("reddit", "reddit"),
    ("X / Twitter, Hacker News", "x_twitter,hackernews"), ("LinkedIn", "linkedin"),
    ("All Platforms", "all"), ("", "all"), (None, "all"), ("garbage", "all"),
    ("x_twitter,reddit,hackernews,linkedin,facebook", "all"),
])
def test_selection_keys(hn_on, raw, want):
    assert platforms.selection_key(raw) == want


def test_all_means_every_enabled_platform_and_never_a_disabled_one(hn_on):
    assert platforms.expand_selection("all") == ("reddit", "hackernews")
    assert platforms.expand_selection("") == ("reddit", "hackernews")
    assert platforms.expand_selection("reddit,hackernews") == ("reddit", "hackernews")
    assert platforms.expand_selection("hackernews") == ("hackernews",)
    for disabled in ("x_twitter", "linkedin", "facebook"):
        assert disabled not in platforms.expand_selection("all")
        assert platforms.expand_selection(disabled) == ()            # a disabled platform alone selects nothing searchable


def test_identity_helpers(hn_on):
    a = {"platform": "hackernews", "post_url": "https://news.ycombinator.com/item?id=111", "title": "t"}
    b = dict(a, post_url="https://news.ycombinator.com/item?id=222")
    assert platforms.of(a) == "hackernews" and platforms.from_url(a["post_url"]) == "hackernews"
    assert platforms.source_id(a) == "hackernews:111" and platforms.source_id(b) == "hackernews:222"       # not collapsed by the query string
    assert platforms.community(a) is None and platforms.is_removed(a) is False
    n = platforms.normalize_signal(dict(a, created_utc=1793102400.0))
    assert n["platform"] == "hackernews" and n["url"] == a["post_url"] and "community" not in n and n["published_at"]
    assert platforms.source_id({"platform": "reddit", "post_url": "https://www.reddit.com/r/x/comments/abc123/t/"}) == "reddit:abc123"


# ── config flag: explicit true AND valid numbers, else fail closed ────────────────────────────────────────────────────────
def _config_flags(env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HN_") and k != "HACKERNEWS_ENABLED"}
    env.update({"SESSION_SECRET_KEY": "test-only-session-key-not-for-deployment"})
    env.update(env_extra)
    code = ("import config; print(config.HACKERNEWS_ENABLED, config.HN_FETCH_TIMEOUT_SECONDS, config.HN_MAX_PAGES_PER_KEYWORD, config.HN_HITS_PER_PAGE)")
    out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); " + code], env=env, cwd=str(ROOT),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-400:]
    return out.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("env,want", [
    ({}, "False 12 3 50"),
    ({"HACKERNEWS_ENABLED": "true"}, "True 12 3 50"),
    ({"HACKERNEWS_ENABLED": "1", "HN_FETCH_TIMEOUT_SECONDS": "20", "HN_MAX_PAGES_PER_KEYWORD": "5", "HN_HITS_PER_PAGE": "30"}, "True 20 5 30"),
    ({"HACKERNEWS_ENABLED": "maybe"}, "False 12 3 50"),
    ({"HACKERNEWS_ENABLED": "true", "HN_FETCH_TIMEOUT_SECONDS": "0"}, "False 12 3 50"),
    ({"HACKERNEWS_ENABLED": "true", "HN_FETCH_TIMEOUT_SECONDS": "abc"}, "False 12 3 50"),
    ({"HACKERNEWS_ENABLED": "true", "HN_MAX_PAGES_PER_KEYWORD": "11"}, "False 12 3 50"),
    ({"HACKERNEWS_ENABLED": "true", "HN_HITS_PER_PAGE": "101"}, "False 12 3 50"),
    ({"HACKERNEWS_ENABLED": "true", "HN_HITS_PER_PAGE": "2.5"}, "False 12 3 50"),
    ({"HN_FETCH_TIMEOUT_SECONDS": "5"}, "False 5 3 50"),
])
def test_the_flag_fails_closed(env, want):
    assert _config_flags(env) == want


def test_env_example_documents_every_new_variable():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("HACKERNEWS_ENABLED", "HN_FETCH_TIMEOUT_SECONDS", "HN_MAX_PAGES_PER_KEYWORD", "HN_HITS_PER_PAGE"):
        assert name in text


# ── the dropdown (both templates) ────────────────────────────────────────────────────────────────────────────────────────
def _options(template, enabled):
    src = (ROOT / "templates" / template).read_text(encoding="utf-8")
    m = re.search(r'<ul id="platforms-menu".*?</ul>', src, re.S)
    assert m, template
    env = jinja2.Environment(autoescape=True, loader=jinja2.FileSystemLoader(str(ROOT / "templates")))      # the option is a shared partial
    html = env.from_string(m.group(0)).render(platform_enabled=(lambda k: bool(enabled and k == "hackernews")))
    return {re.search(r'data-value="([^"]+)"', li).group(1): li for li in re.findall(r"<li role=\"option\".*?</li>", html, re.S)}


@pytest.mark.parametrize("template", ["chat.html", "index.html"])
def test_dropdown_offers_hacker_news_when_enabled(template):
    opts = _options(template, True)
    hn = opts["Hacker News"]
    assert 'aria-disabled' not in hn and "Coming soon" not in hn and 'tabindex="0"' in hn and "<svg" in hn      # selectable, with a logo
    assert list(opts) == ["All Platforms", "X / Twitter", "Reddit", "Hacker News", "LinkedIn", "Facebook"]
    for name in ("X / Twitter", "LinkedIn", "Facebook"):
        assert 'aria-disabled="true"' in opts[name] and "Coming soon" in opts[name]                              # still unselectable
    assert opts["Reddit"] == _options(template, False)["Reddit"]                                                  # Reddit's entry is untouched


@pytest.mark.parametrize("template", ["chat.html", "index.html"])
def test_dropdown_shows_coming_soon_when_disabled(template):
    hn = _options(template, False)["Hacker News"]
    assert 'aria-disabled="true"' in hn and "Coming soon" in hn and 'tabindex="-1"' in hn


def test_the_two_templates_carry_the_same_option_markup():
    for flag in (True, False):
        assert _options("chat.html", flag)["Hacker News"] == _options("index.html", flag)["Hacker News"]


def test_a_template_without_the_global_renders_coming_soon():
    src = (ROOT / "templates" / "chat.html").read_text(encoding="utf-8")
    m = re.search(r'<ul id="platforms-menu".*?</ul>', src, re.S)
    html = jinja2.Environment(autoescape=True, undefined=jinja2.ChainableUndefined,
                              loader=jinja2.FileSystemLoader(str(ROOT / "templates"))).from_string(m.group(0)).render()
    assert "Coming soon" in re.search(r'<li[^>]*data-value="Hacker News".*?</li>', html, re.S).group(0)


def test_both_templates_include_the_one_shared_partial_and_carry_no_inline_copy():
    for t in ("chat.html", "index.html"):
        src = (ROOT / "templates" / t).read_text(encoding="utf-8")
        assert src.count('{% include "_platform_hackernews.html" %}') == 1 and 'data-value="Hacker News"' not in src
    assert 'data-value="Hacker News"' in (ROOT / "templates" / "_platform_hackernews.html").read_text(encoding="utf-8")
