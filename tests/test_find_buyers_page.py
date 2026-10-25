"""
tests/test_find_buyers_page.py - /find-buyers-on-reddit, the public "Finding buyers and leads on Reddit" use-case page.

Rendering, metadata, sitemap, indexability, internal links, and the accuracy of its claims: the supported platforms
(Reddit only) and the intent labels / qualified-lead definition are checked against the real implementation
(answer_integrity.py), so the page cannot drift from what the product does. No network, no database.
"""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_seo_basics import SITEMAP_NS, _client, _head_of, _jsonld, _set_config, app_env  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parent.parent
PATH = "/find-buyers-on-reddit"


def _page(app_env):
    idx, _rts, _ = app_env
    r = _client(idx).get(PATH)
    assert r.status_code == 200
    return r


def _text(html):
    body = html[html.index("<body"):]
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def test_page_renders_publicly_without_identity_or_database(app_env):
    idx, _rts, db_names = app_env
    c = _client(idx)
    r = c.get(PATH, follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "set-cookie" not in r.headers and not c.cookies
    assert [n for n, m in db_names.items() if m.mock_calls] == []
    assert r.headers["cache-control"].startswith("public")
    assert c.head(PATH).status_code in (200, 405)


def test_metadata_title_description_canonical_and_open_graph(app_env):
    head = _head_of(_page(app_env).text)
    assert "<title>Find Buyers and Leads on Reddit — Flintel</title>" in head
    desc = re.search(r'<meta name="description" content="([^"]*)"', head).group(1)
    assert 50 <= len(desc) <= 160 and "Reddit" in desc and "No guaranteed leads" in desc
    assert '<link rel="canonical" href="http://testserver/find-buyers-on-reddit">' in head
    for frag in ('<meta property="og:url" content="http://testserver/find-buyers-on-reddit">',
                 '<meta property="og:title" content="Find Buyers and Leads on Reddit — Flintel">',
                 '<meta property="og:image" content="http://testserver/og-card.png">',
                 '<meta property="og:image:width" content="1200">', '<meta property="og:image:height" content="630">',
                 '<meta name="twitter:card" content="summary_large_image">', 'name="viewport"'):
        assert frag in head, frag
    og_desc = re.search(r'<meta property="og:description" content="([^"]*)"', head).group(1)
    assert og_desc == desc


def test_canonical_follows_the_production_site_url(app_env, monkeypatch):
    idx, _rts, _ = app_env
    c = _client(idx)
    _set_config(monkeypatch, SITE_URL="https://getflintel.com")
    head = _head_of(c.get(PATH).text)
    assert '<link rel="canonical" href="https://getflintel.com/find-buyers-on-reddit">' in head
    assert "testserver" not in head


def test_page_is_indexable_and_listed_in_the_sitemap_and_robots_allows_it(app_env):
    idx, _rts, _ = app_env
    c = _client(idx)
    r = c.get(PATH)
    assert "noindex" not in r.text.lower() and "x-robots-tag" not in {k.lower() for k in r.headers}
    locs = [e.text for e in ET.fromstring(c.get("/sitemap.xml").content).iter(f"{SITEMAP_NS}loc")]
    assert "http://testserver/find-buyers-on-reddit" in locs
    robots = c.get("/robots.txt").text
    assert not any(line.startswith("Disallow:") and PATH.startswith(line.split(":", 1)[1].strip()) for line in robots.splitlines())


def test_exactly_one_h1_and_a_sensible_heading_outline(app_env):
    html = _page(app_env).text
    assert len(re.findall(r"<h1\b", html)) == 1 and "Find buyers and leads on Reddit" in html
    assert len(re.findall(r"<h2\b", html)) >= 6
    assert html.index("<h1") < html.index("<h2")


def test_internal_links_to_and_from_the_page(app_env):
    idx, _rts, _ = app_env
    c = _client(idx)
    html = c.get(PATH).text
    hrefs = set(re.findall(r'href="(/[^"#?]*)', html))
    assert {"/", "/billing/upgrade"} <= hrefs                       # CTA, logo, pricing
    assert html.count('href="/"') >= 3                              # logo + top nav + CTAs
    for href in hrefs - {"/favicon.ico"}:                           # every internal link resolves to a public page
        if href.endswith((".png", ".ico")):
            continue
        assert c.get(href).status_code == 200, href
    # the page is linked from the home page About section (visitors and crawlers) and from the pricing footer
    pricing = c.get("/billing/upgrade").text
    assert 'href="/find-buyers-on-reddit"' in pricing
    home_tpl = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    about = home_tpl[home_tpl.index('class="seo-about"'):home_tpl.index("</section>", home_tpl.index('class="seo-about"'))]
    assert 'href="/find-buyers-on-reddit"' in about


def test_only_reddit_is_claimed_as_supported(app_env):
    text = _text(_page(app_env).text)
    assert "Reddit only" in text and "Other platforms are not available yet" in text
    for name in ("Twitter", "X (", "LinkedIn", "Facebook", "Instagram", "TikTok", "YouTube", "Hacker News", "Discord", "Slack"):
        assert name not in text, name
    assert not re.search(r"\b(Reddit|Flintel) (also )?(supports|searches|covers) (other|more) platforms", text, re.I)


def test_no_guarantees_or_invented_results(app_env):
    text = _text(_page(app_env).text)
    assert "No guaranteed leads" in text and "no qualified leads" in text           # zero-lead searches are stated as normal
    for banned in ("guaranteed leads!", "guarantee you", "10x", "double your", "increase your sales", "proven", "case study",
                   "testimonial from", "customers say", "% more"):
        assert banned.lower() not in text.lower(), banned
    assert "not real Flintel output" in text and "made up" in text           # the example is labelled as an illustration
    assert "Flintel does not contact anyone" in text


def test_page_matches_the_real_intent_categories_and_qualified_definition():
    import answer_integrity as ai
    text = _text((ROOT / "templates" / "find-buyers-on-reddit.html").read_text(encoding="utf-8"))
    # the four labels the product shows
    for label in ("High", "Medium", "Low", "Uncertain"):
        assert label in ai._LABEL_ORDER and f"{label} " in text
    # the roles behind Low
    assert "competitor_promo" in ai.NON_DEMAND_ROLES and "testimonial" in ai.NON_DEMAND_ROLES
    assert "vendor promoting their own product" in text and "describing a tool they already use" in text
    # the qualified-lead definition, phrase by phrase, from the implementation's own wording
    for phrase in ("explicit purchase step", "approved budget", "committed decision to buy, sign, subscribe or switch",
                   "active procurement process"):
        assert phrase in ai.QUALIFIED_DEFINITION and phrase in text, phrase
    assert "Budget, timing, trials, demos and willingness to pay are shown as signals but do not qualify" in ai.QUALIFIED_DEFINITION
    assert "Budget, timing, a trial or demo, and willingness to pay are shown as signals, but none of them qualifies" in text
    assert "Only a post labelled High is a qualified lead" in text
    # the labels the code assigns: qualified == High, and nothing else is a lead
    src = (ROOT / "answer_integrity.py").read_text(encoding="utf-8")
    assert '"qualified": label == "High"' in src


def test_mobile_viewport_and_responsive_rules_are_present():
    html = (ROOT / "templates" / "find-buyers-on-reddit.html").read_text(encoding="utf-8")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0" />' in html
    assert "@media (max-width: 640px)" in html


# ── real browser (Chromium when available): no console errors, no horizontal scroll, readable on a phone ───────────
def test_renders_cleanly_on_mobile_and_desktop(app_env):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    html = _page(app_env).text
    try:
        p = sync_playwright().start()
    except Exception as exc:                                                      # e.g. another test left an asyncio loop running
        pytest.skip(f"playwright unavailable: {str(exc)[:80]}")
    try:
        browser = p.chromium.launch()
    except Exception as exc:                                                      # no usable Chromium in this environment
        p.stop()                                  # always release Playwright, or later browser tests in the process skip
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
    try:
        for name, vp in (("mobile", {"width": 375, "height": 800}), ("desktop", {"width": 1280, "height": 900})):
            page = browser.new_page(viewport=vp)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/*", lambda route: route.fulfill(status=200, body=""))
            page.set_content(html)
            assert errors == [], (name, errors)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), name
            assert page.locator("h1").count() == 1 and page.locator("h1").is_visible()
            assert page.locator(".cta-btn").first.is_visible()
            assert page.evaluate("getComputedStyle(document.querySelector('.lead')).fontSize") in ("16px", "17px")
            page.close()
    finally:
        browser.close()
        p.stop()
