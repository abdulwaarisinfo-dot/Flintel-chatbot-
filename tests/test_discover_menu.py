"""
tests/test_discover_menu.py - the home page clean-up, the compact top-right menu and /discover.

What is covered
  * the home page states: logged-out and signed-in first visits (no long About text, search and Explore untouched),
    and both kinds of visitor after a conversation started; the About text is gone from the template, not CSS-hidden
  * the menu: five real destinations, open/close, keyboard, focus, desktop and mobile, search is not disturbed
  * /discover: public, no database, only written guides and example searches (no invented posts, links or numbers),
    editorial layout in a real browser at desktop and mobile widths
  * the "?q=" pre-fill used by Discover cards: fills the box, never submits

No network, no database, no model: collections are stubs and every browser request is answered locally.
"""
import html as _html
import json
import re
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, unquote, urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_seo_basics import SITEMAP_NS, _client, _head_of, _set_config, app_env  # noqa: E402,F401  (fixture)

ABOUT_TITLE = "Find out what Reddit is saying about your brand, product or market"
MENU_LINKS = [("Discover", "/discover"), ("Find Buyers", "/find-buyers-on-reddit"),
              ("Competitor Complaints", "/discover#competitor-complaints"),
              ("Pain Points", "/discover#pain-points")]


def _text(html):
    body = html[html.index("<body"):]
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()


def _logged_out(idx, rts, monkeypatch, started=False):
    monkeypatch.setattr(rts, "get_current_user", lambda request: None)
    monkeypatch.setattr(rts, "get_owner", lambda request: ("guest:fresh", "anonymous"))
    monkeypatch.setattr(rts, "owner_has_started_conversation", lambda owner_key, on_error=True: started)
    monkeypatch.setattr(rts, "get_user_chat_list", lambda owner_key: [])
    return _client(idx).get("/").text


def _signed_in(idx, rts, monkeypatch, started=False):
    monkeypatch.setattr(rts, "get_current_user", lambda request: {"name": "Test User", "email": "t@example.com"})
    monkeypatch.setattr(rts, "get_owner", lambda request: ("user:t@example.com", "user"))
    monkeypatch.setattr(rts, "owner_has_started_conversation", lambda owner_key, on_error=True: started)
    monkeypatch.setattr(rts, "get_user_chat_list", lambda owner_key: [])
    return _client(idx).get("/").text


def _page(app_env, path):
    idx, _rts, _ = app_env
    r = _client(idx).get(path)
    assert r.status_code == 200, path
    return r


# ═════════════════════════════════════════════════════════════════════════
# 1. home page states
# ═════════════════════════════════════════════════════════════════════════

def test_logged_out_first_visit_has_no_long_about_text(app_env, monkeypatch):
    idx, rts, _ = app_env
    html = _logged_out(idx, rts, monkeypatch)
    assert 'class="seo-about"' not in html and 'id="about-flintel"' not in html
    for sentence in (ABOUT_TITLE, "Flintel searches Reddit conversations that match a brand",
                     "Which platforms does Flintel search?", "Is Flintel free to try?", "Common questions"):
        assert sentence not in html, sentence                      # absent from the markup, not merely hidden


def test_logged_out_first_visit_keeps_the_search_box_and_explore_flintel(app_env, monkeypatch):
    idx, rts, _ = app_env
    html = _logged_out(idx, rts, monkeypatch)
    assert 'id="search-input"' in html and 'id="submit-btn"' in html
    assert html.count('<h1 class="headline">') == 1 and "What's the internet saying?" in html
    assert 'class="explore-panel"' in html and html.count('class="explore-row"') == 3
    for q in ("Find people looking for alternatives", "Find customer pain points",
              "Find opportunities your competitors miss"):
        assert f'data-query="{q}"' in html
    assert '<div class="content-wrap" id="hero-wrap">' in html                  # not hidden, not in the "started" layout


def test_logged_out_visitor_who_started_a_conversation_is_unchanged(app_env, monkeypatch):
    idx, rts, _ = app_env
    html = _logged_out(idx, rts, monkeypatch, started=True)
    assert "hero-started" in html and 'id="home-greeting"' in html
    assert 'class="explore-panel"' not in html and 'class="seo-about"' not in html


def test_signed_in_first_visit_also_has_no_long_about_text(app_env, monkeypatch):
    idx, rts, _ = app_env
    html = _signed_in(idx, rts, monkeypatch)
    assert 'class="seo-about"' not in html and ABOUT_TITLE not in html and 'id="about-flintel"' not in html
    assert 'class="explore-panel"' in html and html.count('class="explore-row"') == 3          # search and Explore unchanged
    assert 'id="search-input"' in html and 'href="/logout"' in html and 'id="mobile-acct-btn"' in html


def test_signed_in_owner_with_a_conversation_is_unchanged(app_env, monkeypatch):
    idx, rts, _ = app_env
    html = _signed_in(idx, rts, monkeypatch, started=True)
    assert "hero-started" in html and 'class="seo-about"' not in html and 'class="explore-panel"' not in html


@pytest.mark.parametrize("user,started", [(False, False), (False, True), (True, False), (True, True)])
def test_the_about_block_is_rendered_in_no_home_page_state(app_env, monkeypatch, user, started):
    idx, rts, _ = app_env
    html = (_signed_in if user else _logged_out)(idx, rts, monkeypatch, started=started)
    assert 'class="seo-about"' not in html and 'id="about-flintel"' not in html and ABOUT_TITLE not in html
    assert 'id="search-input"' in html and 'id="fl-menu-panel"' in html
    assert ('class="explore-panel"' in html) is (not started)                                  # Explore: first visits only, as before


def test_the_about_text_is_gone_from_the_template_not_hidden_by_css(app_env):
    tpl = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert '<section class="seo-about"' not in tpl and 'id="about-flintel"' not in tpl
    assert ABOUT_TITLE not in tpl and "Which platforms does Flintel search?" not in tpl and "Is Flintel free to try?" not in tpl
    assert 'class="seo-about"' not in tpl                                    # no element of that class is left in the markup
    assert not [ln for ln in tpl.splitlines() if "seo-about" in ln and ("display: none" in ln or "visibility" in ln) and "{" not in ln]


def test_the_homepage_head_and_seo_basics_are_unchanged(app_env, monkeypatch):
    idx, rts, _ = app_env
    head = _head_of(_logged_out(idx, rts, monkeypatch))
    assert '<link rel="canonical" href="http://testserver/">' in head
    assert '<meta name="description" content="Search a brand, product or topic and see what people say about it on Reddit' in head
    assert "application/ld+json" in head and "noindex" not in head.lower()


# ═════════════════════════════════════════════════════════════════════════
# 2. /how-it-works was removed: no route, no page, no link anywhere
# ═════════════════════════════════════════════════════════════════════════

def test_how_it_works_page_is_gone_and_nothing_links_to_it(app_env):
    idx, _rts, _ = app_env
    c = _client(idx)
    assert c.get("/how-it-works").status_code == 404
    assert not (ROOT / "templates" / "how-it-works.html").exists()
    for path in ("/", "/discover", "/find-buyers-on-reddit", "/billing/upgrade", "/sitemap.xml"):
        assert "how-it-works" not in c.get(path).text, path
    for name in ("routes.py", "discover_content.py", "templates/_site_menu.html", "templates/discover.html", "templates/index.html"):
        assert "how-it-works" not in (ROOT / name).read_text(encoding="utf-8"), name


# ═════════════════════════════════════════════════════════════════════════
# 3. sitemap, robots and internal links
# ═════════════════════════════════════════════════════════════════════════

def test_sitemap_lists_discover_and_robots_does_not_block_it(app_env):
    idx, _rts, _ = app_env
    c = _client(idx)
    locs = [e.text for e in ET.fromstring(c.get("/sitemap.xml").content).iter(f"{SITEMAP_NS}loc")]
    assert locs == ["http://testserver/", "http://testserver/billing/upgrade", "http://testserver/find-buyers-on-reddit",
                    "http://testserver/discover"]
    disallowed = [ln.split(":", 1)[1].strip() for ln in c.get("/robots.txt").text.splitlines() if ln.startswith("Disallow:")]
    for path in ("/discover", "/find-buyers-on-reddit"):
        assert not any(path.startswith(d) for d in disallowed), path


def test_existing_seo_pages_are_unchanged(app_env):
    idx, _rts, _ = app_env
    c = _client(idx)
    fb = c.get("/find-buyers-on-reddit")
    assert fb.status_code == 200 and "<title>Find Buyers and Leads on Reddit — Flintel</title>" in fb.text
    assert '<link rel="canonical" href="http://testserver/find-buyers-on-reddit">' in fb.text
    assert "An example workflow" in fb.text and "Limits to know about" in fb.text
    assert c.get("/billing/upgrade").status_code == 200


# ═════════════════════════════════════════════════════════════════════════
# 4. the menu (markup)
# ═════════════════════════════════════════════════════════════════════════

def _menu_links(html):
    nav = re.search(r'<nav class="fl-menu-panel".*?</nav>', html, re.S).group(0)
    return [(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip().replace("&amp;", "&"), m.group(1))
            for m in re.finditer(r'<a class="fl-menu-item" href="([^"]+)"[^>]*>(.*?)</a>', nav, re.S)]


@pytest.mark.parametrize("who", ["logged_out", "signed_in"])
def test_home_has_the_compact_menu_with_the_five_destinations(app_env, monkeypatch, who):
    idx, rts, _ = app_env
    html = (_logged_out if who == "logged_out" else _signed_in)(idx, rts, monkeypatch)
    assert _menu_links(html) == MENU_LINKS
    assert html.count('<button type="button" class="fl-menu-btn') == 2          # desktop top bar + mobile top bar
    assert 'class="fl-menu-panel" id="fl-menu-panel" aria-label="Flintel menu" hidden' in html
    assert html.count('id="fl-menu-panel"') == 1
    for attr in ('aria-haspopup="true"', 'aria-expanded="false"', 'aria-controls="fl-menu-panel"', 'aria-label="Open menu"'):
        assert attr in html


def test_existing_sign_in_and_account_entry_points_are_still_there(app_env, monkeypatch):
    idx, rts, _ = app_env
    out = _logged_out(idx, rts, monkeypatch)
    assert 'id="topbar-signup-btn"' in out and "Sign In" in out
    inn = _signed_in(idx, rts, monkeypatch)
    assert 'href="/logout"' in inn and 'id="mobile-acct-btn"' in inn


def test_every_menu_destination_resolves_and_its_anchor_exists(app_env):
    idx, _rts, _ = app_env
    c = _client(idx)
    discover = c.get("/discover").text
    for label, href in MENU_LINKS:
        path, _, frag = href.partition("#")
        r = c.get(path)
        assert r.status_code == 200, (label, href)
        if frag:
            assert f'id="{frag}"' in r.text, (label, href)                       # no dead in-page links
    assert 'aria-current="page"' in discover                                    # Discover marks itself


# ═════════════════════════════════════════════════════════════════════════
# 5. /discover and discover_content
# ═════════════════════════════════════════════════════════════════════════

def test_discover_is_public_static_and_needs_no_database(app_env):
    idx, _rts, db_names = app_env
    c = _client(idx)
    r = c.get("/discover", follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "set-cookie" not in r.headers and not c.cookies
    assert [n for n, m in db_names.items() if m.mock_calls] == []
    assert r.headers["cache-control"].startswith("public")
    assert c.head("/discover").status_code in (200, 405)


def test_discover_metadata_canonical_open_graph_and_indexing(app_env, monkeypatch):
    idx, _rts, _ = app_env
    c = _client(idx)
    r = c.get("/discover")
    head = _head_of(r.text)
    assert "<title>Discover — Flintel</title>" in head
    desc = re.search(r'<meta name="description" content="([^"]*)"', head).group(1)
    assert 50 <= len(desc) <= 160 and "Reddit" in desc
    for frag in ('<link rel="canonical" href="http://testserver/discover">', '<meta property="og:url" content="http://testserver/discover">',
                 '<meta property="og:title" content="Discover — Flintel">', '<meta property="og:image" content="http://testserver/og-card.png">',
                 '<meta name="twitter:card" content="summary_large_image">', 'name="viewport"'):
        assert frag in head, frag
    assert "noindex" not in r.text.lower() and "x-robots-tag" not in {k.lower() for k in r.headers}
    _set_config(monkeypatch, SITE_URL="https://getflintel.com")
    assert '<link rel="canonical" href="https://getflintel.com/discover">' in c.get("/discover").text


def test_discover_heading_outline_and_sections(app_env):
    html = _page(app_env, "/discover").text
    assert len(re.findall(r"<h1\b", html)) == 1 and "<h1>Discover</h1>" in html
    assert html.index("<h1") < html.index("<h2")
    assert html.count('<aside class="dsc-side"') == 1 and html.count('class="dsc-lead dsc-card"') == 1
    assert html.count('class="dsc-trio"') == 3 and html.count("dsc-wide dsc-card") == 4
    for cid in ("opportunity-signals", "competitor-complaints", "pain-points", "emerging-discussions"):
        assert html.count(f'id="{cid}"') == 1, cid                                # one target per category
        assert f'href="#{cid}"' in html                                            # and it is linked (tabs + sidebar)


def test_discover_cards_are_labelled_guide_or_search_idea_and_never_as_evidence(app_env):
    import discover_content as dc
    html = _page(app_env, "/discover").text
    cards = [c for _layout, group in dc.build_feed() for c in group]
    visible = _html.unescape(_text(html))
    assert len(cards) == 14 and len({c["slug"] for c in cards}) == 14
    for c in cards:
        assert c["kind"] in ("Guide", "Search idea") and c["category"] in {k["id"] for k in dc.CATEGORIES}
        assert c["title"] in visible, c["title"]                     # every card's title is really on the page, as text
    assert html.count('class="dsc-kind"') + html.count('class="dsc-kind is-search"') == 14
    text = _text(html)
    assert "These are written guides, not posts" in text and "each linked to its Reddit source" in text


def test_discover_has_no_invented_posts_links_metrics_or_live_claims(app_env):
    html = _page(app_env, "/discover").text
    text = _text(html).lower()
    assert "reddit.com" not in html and "redd.it" not in html                  # no post or user URLs
    assert not re.search(r"https?://(?!fonts\.googleapis\.com|www\.w3\.org)", html.replace("http://testserver", ""))
    for banned in ("real-time", "realtime", "real time", "live", "trending", "upvote", "engagement", "followers", "views",
                   "minutes ago", "hours ago", "days ago", "monitoring", "alerts", "guaranteed", "most popular", "top posts"):
        assert not re.search(rf"\b{re.escape(banned)}\b", text), banned
    assert "%" not in text and not re.search(r"\b\d{2,}\s*(posts|users|people|subreddits|threads|mentions)\b", text)
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
    assert re.findall(r"<img\b[^>]*>", body.split("<body")[1]) == ['<img src="/favicon-48x48.png?v=3" alt="" aria-hidden="true">']
    # (only the Flintel logo is an image; the artwork is decorative CSS/SVG, not photos)


def test_discover_claims_only_reddit_and_only_what_flintel_does(app_env):
    text = _text(_page(app_env, "/discover").text)
    assert "Flintel searches Reddit. Other platforms are not available yet." in text
    assert "not a complete view of Reddit" in text and "does not guarantee leads" in text
    for name in ("Twitter", "LinkedIn", "Facebook", "Instagram", "TikTok", "YouTube", "Hacker News", "Discord", "Slack"):
        assert name not in text, name


def test_discover_search_links_prefill_the_home_page_and_come_from_the_content_module(app_env):
    import discover_content as dc
    html = _page(app_env, "/discover").text
    hrefs = re.findall(r'href="(/\?q=[^"]+)"', html)
    assert len(hrefs) >= 12
    known = {c["query"] for _l, g in dc.build_feed() for c in g if c.get("query")} | set(dc.RELATED_SEARCHES)
    for href in hrefs:
        q = parse_qs(urlparse(href.replace("&amp;", "&")).query)["q"][0]
        assert q in known and 0 < len(q) <= 300, q
    assert dc.search_href("a b&c=d?") == "/?q=a%20b%26c%3Dd%3F"
    assert unquote(dc.search_href("Find posts about X from today")) == "/?q=Find posts about X from today"
    assert all('rel="nofollow"' in m for m in re.findall(r'<a [^>]*href="/\?q=[^>]*>', html))   # no crawl of ?q= variants


def test_discover_content_has_no_data_source_and_no_numbers_to_trust():
    src = (ROOT / "discover_content.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    assert not re.search(r"^\s*(import|from)\s+(database|index|logics|pymongo|requests|httpx)\b", code, flags=re.M)
    assert "find_one" not in code and "collection" not in code.lower()
    import discover_content as dc
    for layout, cards in dc.build_feed():
        assert {"lead": 1, "trio": 3, "wide": 1}[layout] == len(cards)
        for c in cards:
            for field in ("title", "summary"):
                assert not re.search(r"\d{3,}|\d+%|\$\d", c[field]), (c["slug"], field)
            assert not c.get("query") or len(c["query"]) <= 300
            assert (c.get("href") or "").startswith("/") if c.get("href") else True


def test_discover_content_internal_links_all_resolve(app_env):
    import discover_content as dc
    idx, _rts, _ = app_env
    c = _client(idx)
    paths = {x["href"] for x in dc.GUIDES} | {card["href"] for _l, g in dc.build_feed() for card in g if card.get("href")}
    assert {"/find-buyers-on-reddit", "/billing/upgrade"} <= paths and all(p.startswith("/") for p in paths)
    for path in paths:
        assert c.get(path).status_code == 200, path


# ═════════════════════════════════════════════════════════════════════════
# 6. in a real browser: menu, pre-fill, layout (skipped when Chromium cannot start)
# ═════════════════════════════════════════════════════════════════════════

ORIGIN = "http://flintel.test"
LOGO = ROOT / "logo"
ICONS = {"/favicon-48x48.png": "flintel-48.png", "/favicon-96x96.png": "flintel-96.png", "/favicon.ico": "flintel.ico"}


@pytest.fixture
def served(app_env, monkeypatch, integrity_browser):
    """{path: html} rendered by the real app; open(path, width, height) -> (page, posted_requests, errors)."""
    idx, rts, _ = app_env
    pages = {"/": _logged_out(idx, rts, monkeypatch)}
    c = _client(idx)
    for path in ("/discover", "/find-buyers-on-reddit", "/billing/upgrade"):
        pages[path] = c.get(path).text
    opened = []

    def open_page(path, width=1440, height=900, dismiss_welcome=True):
        ctx = integrity_browser.new_context(viewport={"width": width, "height": height})
        page = ctx.new_page()
        posts, errors = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)

        def handle(route):
            url = route.request.url
            if not url.startswith(ORIGIN):
                return route.fulfill(status=200, body="")
            p = url[len(ORIGIN):].split("?")[0].split("#")[0]
            if p in pages:
                return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[p])
            if p in ICONS:
                return route.fulfill(status=200, path=str(LOGO / ICONS[p]))
            return route.fulfill(status=200, body="")
        page.route("**/*", handle)
        page.goto(ORIGIN + path)
        page.wait_for_timeout(300)
        if dismiss_welcome and page.query_selector("#welcome-overlay.visible #welcome-close-btn") and width < 768:
            page.click("#welcome-close-btn")            # on mobile the existing welcome card is a modal
        opened.append(ctx)
        return page, posts, errors
    yield open_page
    for ctx in opened:
        ctx.close()


def _panel_state(page):
    return page.evaluate("""() => { const p = document.getElementById('fl-menu-panel'), r = p.getBoundingClientRect();
      return {hidden: p.hidden, x: r.x, y: r.y, w: r.width, h: r.height, right: r.right, bottom: r.bottom,
              vw: window.innerWidth, vh: window.innerHeight,
              expanded: [...document.querySelectorAll('[data-fl-menu-btn]')].map(b => b.getAttribute('aria-expanded')),
              active: document.activeElement.textContent.trim() || document.activeElement.id}; }""")


def test_browser_menu_opens_and_closes_on_desktop(served):
    page, _posts, errors = served("/")
    assert _panel_state(page)["hidden"] is True
    page.click(".topbar .fl-menu-btn")
    st = _panel_state(page)
    assert st["hidden"] is False
    assert st["expanded"] == ["false", "true"]                                   # [mobile button, desktop button]: only the clicked one
    assert 220 <= st["w"] <= 232 and 165 <= st["h"] <= 180                       # compact: 4 items of 40px + padding
    assert st["right"] <= st["vw"] and st["y"] > 50 and st["bottom"] < st["vh"]
    labels = page.evaluate("[...document.querySelectorAll('#fl-menu-panel a')].map(a => a.textContent.trim())")
    assert labels == [label for label, _ in MENU_LINKS]
    page.click(".topbar .fl-menu-btn")                                           # the button toggles
    assert _panel_state(page)["hidden"] is True
    page.click(".topbar .fl-menu-btn")
    page.mouse.click(600, 700)                                                   # outside click closes
    assert _panel_state(page)["hidden"] is True and errors == []


def test_browser_menu_keyboard_and_focus(served):
    page, _posts, errors = served("/")
    page.focus(".topbar .fl-menu-btn")
    page.keyboard.press("Enter")                                                 # keyboard open moves focus into the menu
    st = _panel_state(page)
    assert st["hidden"] is False and st["active"] == "Discover"
    page.keyboard.press("ArrowDown"); assert _panel_state(page)["active"] == "Find Buyers"
    page.keyboard.press("End"); assert _panel_state(page)["active"] == "Pain Points"
    page.keyboard.press("ArrowDown"); assert _panel_state(page)["active"] == "Discover"          # wraps
    page.keyboard.press("ArrowUp"); assert _panel_state(page)["active"] == "Pain Points"
    page.keyboard.press("Home"); assert _panel_state(page)["active"] == "Discover"
    page.keyboard.press("Escape")
    st = _panel_state(page)
    assert st["hidden"] is True and page.evaluate("document.activeElement.matches('.topbar .fl-menu-btn')")   # focus returns
    assert page.evaluate("getComputedStyle(document.querySelector('.fl-menu-btn')).outlineStyle") in ("none", "auto", "solid")
    page.focus(".topbar .fl-menu-btn"); page.keyboard.press("Enter"); page.keyboard.press("Tab")             # Tab walks the links
    assert _panel_state(page)["active"] == "Find Buyers"
    assert errors == []


def test_browser_menu_links_navigate(served):
    page, _posts, errors = served("/")
    page.click(".topbar .fl-menu-btn")
    page.click("#fl-menu-panel a:has-text('Find Buyers')")
    page.wait_for_url(ORIGIN + "/find-buyers-on-reddit")
    assert "Find Buyers and Leads on Reddit" in page.title()
    page.goto(ORIGIN + "/discover")
    page.click(".topbar-right .fl-menu-btn")
    assert page.evaluate("document.querySelector('#fl-menu-panel a[aria-current=page]').textContent.trim()") == "Discover"
    page.click("#fl-menu-panel a:has-text('Pain Points')")
    page.wait_for_url(ORIGIN + "/discover#pain-points")
    assert page.evaluate("document.getElementById('pain-points') !== null") and errors == []


def test_browser_menu_does_not_interfere_with_search(served):
    page, posts, errors = served("/")
    page.fill("#search-input", "crm tools")
    page.click(".topbar .fl-menu-btn")
    page.keyboard.press("Escape")
    assert page.input_value("#search-input") == "crm tools" and page.is_enabled("#submit-btn")
    page.click(".topbar .fl-menu-btn")
    page.click("#search-input")                                                  # clicking the search box closes the menu
    assert _panel_state(page)["hidden"] is True
    page.keyboard.type(" for startups")
    assert page.input_value("#search-input") == "crm tools for startups" and posts == [] and errors == []
    page.click(".explore-row[data-query='Find customer pain points']")           # Explore Flintel still works
    assert page.input_value("#search-input") == "Find customer pain points"


def test_browser_menu_on_mobile_fits_the_screen(served):
    page, _posts, errors = served("/", width=390, height=844)
    assert page.is_visible(".mobile-topbar .fl-menu-btn") and not page.is_visible(".topbar .fl-menu-btn")
    page.click(".mobile-topbar .fl-menu-btn")
    st = _panel_state(page)
    assert st["hidden"] is False and st["x"] >= 8 and st["right"] <= st["vw"] - 8 and st["bottom"] < st["vh"]
    page.click("#fl-menu-panel a[href='/discover']")
    page.wait_for_url(ORIGIN + "/discover")
    assert errors == []


def test_browser_menu_works_on_discover(served):
    page, _posts, errors = served("/discover")
    page.click(".topbar-right .fl-menu-btn")
    assert _panel_state(page)["hidden"] is False
    page.keyboard.press("Escape")
    assert _panel_state(page)["hidden"] is True and errors == []


def test_browser_query_parameter_prefills_the_search_box_and_never_submits(served):
    page, posts, errors = served("/?q=Find%20people%20looking%20for%20alternatives")
    assert page.input_value("#search-input") == "Find people looking for alternatives"
    assert page.is_enabled("#submit-btn") and page.evaluate("document.activeElement.id") == "search-input"
    page.wait_for_timeout(300)
    assert posts == [] and errors == []                                           # nothing was sent
    for raw, expected in (("?q=%20%20spaced%20%20out%20", "spaced out"), ("?q=", ""), ("?q=%3Cb%3Ebold%3C%2Fb%3E", "<b>bold</b>"),
                          ("?other=1", ""), ("?q=" + "x" * 500, "x" * 300)):
        p2, posts2, errs2 = served("/" + raw)
        assert p2.input_value("#search-input") == expected and posts2 == [] and errs2 == [], raw
    assert p2.evaluate("document.querySelectorAll('#search-input b').length") == 0


def test_browser_a_discover_card_opens_the_home_page_with_the_search_ready(served):
    page, posts, errors = served("/discover")
    page.click("#competitor-complaints a:has-text('Try this search')")
    page.wait_for_url(re.compile(r".*/\?q=.*"))
    assert page.input_value("#search-input") == "Find complaints about project management software"
    assert posts == [] and errors == []


@pytest.mark.parametrize("width,height", [(1440, 900), (1100, 900), (900, 900), (390, 844), (360, 740)])
def test_browser_discover_layout_has_no_overflow_and_keeps_the_editorial_structure(served, width, height):
    page, _posts, errors = served("/discover", width, height)
    m = page.evaluate("""() => { const q = s => document.querySelector(s), r = e => e.getBoundingClientRect();
      const feed = r(q('.dsc-feed')), side = r(q('.dsc-side')), lead = q('.dsc-lead');
      const trio = [...document.querySelectorAll('.dsc-trio')[0].children].map(r);
      return {sw: document.documentElement.scrollWidth, vw: window.innerWidth, feedW: feed.width, sideW: side.width,
              sideLeft: side.left, feedRight: feed.right, sideTop: side.top, feedTop: feed.top,
              leadCols: getComputedStyle(lead).gridTemplateColumns.split(' ').length,
              trioSameRow: trio.every(t => Math.abs(t.top - trio[0].top) < 4), trioW: trio.map(t => t.width),
              art: [...document.querySelectorAll('.dsc-art')].map(a => [r(a).width, r(a).height])}; }""")
    assert m["sw"] <= m["vw"] and errors == []                                   # no horizontal scrolling
    assert all(w > 40 and h > 40 for w, h in m["art"])                           # every artwork has a real size
    if width >= 981:
        assert m["sideLeft"] >= m["feedRight"] and abs(m["sideTop"] - m["feedTop"]) < 120    # two columns
        assert 280 <= m["sideW"] <= 320 and m["feedW"] > m["sideW"] * 2           # content wider than the sidebar
        assert m["leadCols"] == 2 and m["trioSameRow"] and all(w > 150 for w in m["trioW"])
    else:
        assert m["sideTop"] > m["feedTop"] + 400                                 # sidebar stacks below the feed
    if width <= 720:
        assert m["leadCols"] == 1 and not m["trioSameRow"] and all(w > 200 for w in m["trioW"])


def test_browser_discover_sections_scroll_to_their_anchors(served):
    page, _posts, errors = served("/discover", 1440, 900)
    page.click(".dsc-tab[href='#pain-points']")
    page.wait_for_timeout(1500)                                                  # the page scrolls smoothly
    top = page.evaluate("document.getElementById('pain-points').getBoundingClientRect().top")
    assert 0 <= top < 260 and errors == []
