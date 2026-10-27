"""
tests/test_seo_basics.py — /robots.txt, /sitemap.xml, /og-image.png, the home page's
description / canonical / Open Graph / JSON-LD / Search Console tags, and noindex on chat.html.

No real Mongo / MySQL / LLM / network: index is imported against stub modules (same
pattern as tests/test_favicon.py). Every request is made WITHOUT logging in. The site URL
is never hardcoded: it comes from config.SITE_URL, else from the request host.
"""
import importlib
import json
import re
import struct
import subprocess
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODS = ("logics", "config", "index", "routes")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
EXPECTED_DISALLOW = ["/chat/", "/chats", "/search", "/auth/", "/login", "/logout", "/signup", "/api/"]


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-seo")
    monkeypatch.delenv("SITE_URL", raising=False)
    monkeypatch.delenv("GOOGLE_SITE_VERIFICATION", raising=False)
    db_names = {}
    db = types.ModuleType("database")

    def _db_attr(name):
        if name.startswith("__"):
            raise AttributeError(name)
        return db_names.setdefault(name, MagicMock(name=f"database.{name}"))
    db.__getattr__ = _db_attr
    fi = types.ModuleType("flintel")
    for name in ("ROUTER_UNFILTERED_ADDENDUM", "GENERIC_PAIN_POINT_INFERENCE_ADDENDUM"):
        setattr(fi, name, "")
    fi.__getattr__ = lambda name: MagicMock(name=f"flintel.{name}")
    gg = types.ModuleType("google")
    gg.__getattr__ = lambda name: MagicMock(name=f"google.{name}")
    plc = types.ModuleType("passlib.context")
    plc.CryptContext = MagicMock()
    aus = types.ModuleType("authlib.integrations.starlette_client")
    aus.OAuth = MagicMock()
    sch = types.ModuleType("scheduler")
    sch.start_trending_keywords_scheduler = MagicMock(name="scheduler.start")
    stubs = {"database": db, "flintel": fi, "google": gg,
             "website_intelligence": types.ModuleType("website_intelligence"),
             "passlib": types.ModuleType("passlib"), "passlib.context": plc,
             "authlib": types.ModuleType("authlib"), "authlib.integrations": types.ModuleType("authlib.integrations"),
             "authlib.integrations.starlette_client": aus, "scheduler": sch}
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)
    for name in MODS:
        monkeypatch.delitem(sys.modules, name, raising=False)
    idx = importlib.import_module("index")
    rts = importlib.import_module("routes")
    # a guest on the bare home page: no user, no chat history (nothing else about home() is faked)
    monkeypatch.setattr(rts, "get_current_user", lambda request: None)
    monkeypatch.setattr(rts, "get_user_chat_list", lambda owner_key: [])
    yield idx, rts, db_names
    for name in MODS:
        sys.modules.pop(name, None)


def _client(idx):
    from fastapi.testclient import TestClient
    return TestClient(idx.app)


def _set_config(monkeypatch, **values):
    cfg = sys.modules["config"]
    for key, value in values.items():
        monkeypatch.setattr(cfg, key, value)


def _head_of(html):
    return html[:html.index("</head>")]


def _jsonld(html):
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert m, "no JSON-LD script in the page"
    return m.group(1)


# 1 ─ robots.txt
def test_robots_txt_public_200_with_rules_and_sitemap(app_env):
    idx, _rts, db_names = app_env
    c = _client(idx)
    r = c.get("/robots.txt", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    lines = r.text.splitlines()
    assert lines[:2] == ["User-agent: *", "Allow: /"]
    assert [ln.split(": ", 1)[1] for ln in lines if ln.startswith("Disallow: ")] == EXPECTED_DISALLOW
    for path in ("/chat/", "/chats", "/search"):
        assert f"Disallow: {path}" in lines
    assert lines[-1] == "Sitemap: http://testserver/sitemap.xml"
    assert "/ping" not in r.text and "/billing" not in r.text            # not a route we disallow; pricing stays allowed
    cache = r.headers["cache-control"]
    assert cache.startswith("public") and "no-store" not in cache
    assert "set-cookie" not in r.headers and not c.cookies               # no session created
    assert [n for n, m in db_names.items() if m.mock_calls] == []        # no DB touched


def test_robots_txt_head(app_env):
    idx, _rts, _ = app_env
    h = _client(idx).head("/robots.txt", follow_redirects=False)
    assert h.status_code == 200 and h.content == b""
    assert h.headers["cache-control"].startswith("public")


def test_every_disallowed_prefix_matches_a_real_route_and_no_allowed_page_is_blocked(app_env):
    idx, _rts, _ = app_env
    paths = {getattr(r, "path", "") for r in idx.app.routes}
    blocked = ("/chat/{chat_id}", "/chat/{chat_id}/stream", "/chat/{chat_id}/delete", "/chat/{chat_id}/message-feedback",
               "/chats", "/chats/new", "/search", "/auth/google/login", "/auth/google/callback", "/signup", "/login",
               "/logout", "/api/me/usage")
    for p in blocked:
        assert p in paths, f"{p} is no longer a route"
        assert any(p.startswith(d) for d in EXPECTED_DISALLOW), f"{p} is not covered by a Disallow rule"
    for d in EXPECTED_DISALLOW:
        assert any(p.startswith(d) for p in paths), f"Disallow {d} matches no route"
    for allowed in ("/", "/billing/upgrade", "/find-buyers-on-reddit", "/favicon.ico", "/og-image.png", "/og-card.png", "/robots.txt", "/sitemap.xml"):
        assert not any(allowed.startswith(d) for d in EXPECTED_DISALLOW), f"{allowed} must stay crawlable"


# 2 ─ sitemap.xml
def test_sitemap_xml_valid_and_lists_only_public_pages(app_env):
    idx, _rts, db_names = app_env
    c = _client(idx)
    r = c.get("/sitemap.xml", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(r.content)                                       # valid XML
    assert root.tag == SITEMAP_NS + "urlset"
    urls = [u.find(SITEMAP_NS + "loc").text for u in root.findall(SITEMAP_NS + "url")]
    assert urls == ["http://testserver/", "http://testserver/billing/upgrade", "http://testserver/find-buyers-on-reddit",
                    "http://testserver/discover"]
    assert "/chat/" not in r.text and "lastmod" not in r.text
    assert r.headers["cache-control"].startswith("public") and "no-store" not in r.headers["cache-control"]
    assert "set-cookie" not in r.headers and not c.cookies
    assert [n for n, m in db_names.items() if m.mock_calls] == []
    h = c.head("/sitemap.xml", follow_redirects=False)
    assert h.status_code == 200 and h.content == b""


# 3 ─ SITE_URL vs the request host
def test_site_url_env_wins_when_set_and_request_host_is_used_when_empty(app_env, monkeypatch):
    idx, _rts, _ = app_env
    c = _client(idx)
    assert "http://testserver/sitemap.xml" in c.get("/robots.txt").text                  # empty -> request host
    _set_config(monkeypatch, SITE_URL="https://www.example.test/")                         # trailing slash is tolerated
    assert "Sitemap: https://www.example.test/sitemap.xml" in c.get("/robots.txt").text
    loc = ET.fromstring(c.get("/sitemap.xml").content).find(f"{SITEMAP_NS}url/{SITEMAP_NS}loc").text
    assert loc == "https://www.example.test/"
    html = c.get("/").text
    assert '<link rel="canonical" href="https://www.example.test/">' in html
    assert 'content="https://www.example.test/og-card.png"' in html
    assert json.loads(_jsonld(html))["url"] == "https://www.example.test/"
    _set_config(monkeypatch, SITE_URL="")
    assert '<link rel="canonical" href="http://testserver/">' in c.get("/").text


def _config_values(env_updates):
    import os
    env = {k: v for k, v in os.environ.items() if k not in ("SITE_URL", "GOOGLE_SITE_VERIFICATION")}
    env.update(env_updates)
    out = subprocess.run(
        [sys.executable, "-c", "import config; print(repr(config.SITE_URL), repr(config.GOOGLE_SITE_VERIFICATION))"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_config_defaults_are_empty_and_values_are_stripped():
    assert _config_values({}) == "'' ''"
    assert _config_values({"SITE_URL": "  https://x.test/  ", "GOOGLE_SITE_VERIFICATION": " tok_123 "}) == "'https://x.test' 'tok_123'"
    exported = (ROOT / "config.py").read_text(encoding="utf-8").split("__all__ = [")[1].split("\n]")[0]
    assert '"SITE_URL"' in exported and '"GOOGLE_SITE_VERIFICATION"' in exported       # `from config import *` exports them


# 4 ─ the home page's <head>
def test_home_page_has_description_canonical_og_and_valid_jsonld(app_env):
    idx, _rts, _ = app_env
    r = _client(idx).get("/")
    assert r.status_code == 200
    head = _head_of(r.text)
    assert '<meta name="description" content="Search a brand, product or topic and see what people say about it on Reddit' in head
    assert '<link rel="canonical" href="http://testserver/">' in head
    assert '<meta property="og:title" content="Flintel — Find Reddit conversations about your brand, product or market">' in head
    assert '<meta property="og:image" content="http://testserver/og-card.png">' in head
    assert '<meta property="og:url" content="http://testserver/">' in head
    assert '<meta property="og:type" content="website">' in head and '<meta name="twitter:card" content="summary_large_image">' in head
    assert '<meta property="og:image:width" content="1200">' in head and '<meta property="og:image:height" content="630">' in head
    data = json.loads(_jsonld(head))                                       # valid JSON
    assert data == {"@context": "https://schema.org", "@type": "Organization", "name": "Flintel",
                    "url": "http://testserver/", "logo": "http://testserver/og-image.png"}
    assert "potentialAction" not in head and "sameAs" not in head and "SearchAction" not in head
    assert head.index('rel="apple-touch-icon"') < head.index('name="description"')   # after the favicon links


def test_description_names_only_platforms_flintel_really_searches(app_env):
    idx, _rts, _ = app_env
    html = _client(idx).get("/").text
    head = _head_of(html)
    for tag in (r'<meta name="description" content="([^"]*)"', r'<meta property="og:description" content="([^"]*)"'):
        desc = re.search(tag, head).group(1)
        assert len(desc) <= 160 and "Reddit" in desc
        for name in ("X / Twitter", "Twitter", "LinkedIn", "Facebook", "Hacker News"):     # only Reddit is live
            assert name not in desc, (tag, name)
    title = re.search(r"<title>([^<]*)</title>", head).group(1)
    assert "Reddit" in title and not any(n in title for n in ("Twitter", "LinkedIn", "Facebook"))


def _fresh_visitor_home(idx, rts, monkeypatch):
    """The home page as a first-time visitor (or crawler) gets it: no conversation started."""
    monkeypatch.setattr(rts, "get_owner", lambda request: ("guest:fresh", "anonymous"))
    monkeypatch.setattr(rts, "owner_has_started_conversation", lambda owner_key, on_error=True: False)
    monkeypatch.setattr(rts, "get_user_chat_list", lambda owner_key: [])
    return _client(idx).get("/").text


def _signed_in_home_without_conversation(idx, rts, monkeypatch):
    """A signed-in owner who has not started a conversation yet (a first visit, like the logged-out one)."""
    monkeypatch.setattr(rts, "get_current_user", lambda request: {"name": "Test User", "email": "t@example.com"})
    monkeypatch.setattr(rts, "get_owner", lambda request: ("user:t@example.com", "user"))
    monkeypatch.setattr(rts, "owner_has_started_conversation", lambda owner_key, on_error=True: False)
    monkeypatch.setattr(rts, "get_user_chat_list", lambda owner_key: [])
    return _client(idx).get("/").text


def test_logged_out_home_has_no_long_about_text_but_keeps_the_search_and_explore(app_env, monkeypatch):
    idx, rts, _ = app_env
    html = _fresh_visitor_home(idx, rts, monkeypatch)
    assert 'class="seo-about"' not in html and 'id="about-flintel"' not in html           # not rendered at all (not CSS-hidden)
    assert "Find out what Reddit is saying about your brand, product or market" not in html
    assert html.count('<h1 class="headline">') == 1 and "What's the internet saying?" in html   # the hero keeps the only H1
    assert 'id="search-input"' in html and 'class="explore-panel"' in html and html.count('class="explore-row"') == 3


def test_first_visit_home_claims_only_reddit(app_env, monkeypatch):
    """The first-visit home (logged out or signed in) has no long marketing text any more; what it does show names only Reddit."""
    idx, rts, _ = app_env
    for html in (_fresh_visitor_home(idx, rts, monkeypatch), _signed_in_home_without_conversation(idx, rts, monkeypatch)):
        assert 'class="seo-about"' not in html and html.count('<h1 class="headline">') == 1     # the hero keeps the only H1
        explore = html[html.index('class="explore-panel"'):][:5000]
        assert 'alt="Reddit"' in explore and 'alt="LinkedIn"' not in explore and 'alt="Facebook"' not in explore
        assert 'alt="X (Twitter)"' not in explore and html.count('class="explore-row"') == 3


def test_pricing_page_has_metadata_and_is_in_the_sitemap(app_env, monkeypatch):
    idx, _rts, _ = app_env
    c = _client(idx)
    r = c.get("/billing/upgrade")
    assert r.status_code == 200
    head = _head_of(r.text)
    assert "<title>Pricing — Flintel</title>" in head
    desc = re.search(r'<meta name="description" content="([^"]*)"', head).group(1)
    assert 0 < len(desc) <= 160 and "Reddit" in desc and "LinkedIn" not in desc and "Twitter" not in desc
    assert '<link rel="canonical" href="http://testserver/billing/upgrade">' in head
    assert '<meta property="og:url" content="http://testserver/billing/upgrade">' in head
    assert "noindex" not in head
    locs = [e.text for e in ET.fromstring(c.get("/sitemap.xml").content).iter(f"{SITEMAP_NS}loc")]
    assert locs == ["http://testserver/", "http://testserver/billing/upgrade", "http://testserver/find-buyers-on-reddit",
                    "http://testserver/discover"]


def test_production_site_url_drives_every_absolute_url(app_env, monkeypatch):
    idx, _rts, _ = app_env
    c = _client(idx)
    _set_config(monkeypatch, SITE_URL="https://getflintel.com")
    assert "Sitemap: https://getflintel.com/sitemap.xml" in c.get("/robots.txt").text
    locs = [e.text for e in ET.fromstring(c.get("/sitemap.xml").content).iter(f"{SITEMAP_NS}loc")]
    assert locs == ["https://getflintel.com/", "https://getflintel.com/billing/upgrade",
                    "https://getflintel.com/find-buyers-on-reddit", "https://getflintel.com/discover"]
    home = c.get("/").text
    assert '<link rel="canonical" href="https://getflintel.com/">' in home
    assert 'content="https://getflintel.com/og-card.png"' in home and "testserver" not in _head_of(home)
    pricing = c.get("/billing/upgrade").text
    assert '<link rel="canonical" href="https://getflintel.com/billing/upgrade">' in pricing
    assert "testserver" not in _head_of(pricing)
    assert "SITE_URL=https://getflintel.com" in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_google_site_verification_only_when_set_and_escaped(app_env, monkeypatch):
    idx, _rts, _ = app_env
    c = _client(idx)
    assert "google-site-verification" not in c.get("/").text               # empty -> no tag at all
    _set_config(monkeypatch, GOOGLE_SITE_VERIFICATION="tok_ABC-123")
    assert '<meta name="google-site-verification" content="tok_ABC-123">' in c.get("/").text
    _set_config(monkeypatch, GOOGLE_SITE_VERIFICATION='"><script>alert(1)</script>')
    html = c.get("/").text
    assert '"><script>alert(1)</script>' not in html                       # autoescaped
    assert 'content="&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in html


def test_jsonld_cannot_close_its_script_element(app_env, monkeypatch):
    idx, _rts, _ = app_env
    _set_config(monkeypatch, SITE_URL="https://x.test/</script><script>alert(1)</script>&")
    html = _client(idx).get("/").text
    block = _jsonld(html)
    assert "<" not in block and ">" not in block and "&" not in block       # escaped as < > &
    assert json.loads(block)["url"].startswith("https://x.test/</script><script>alert(1)</script>&")   # round-trips
    assert html.count("<script type=\"application/ld+json\">") == 1


def test_home_context_gets_exactly_four_new_keys(app_env):
    idx, rts, _ = app_env
    from starlette.requests import Request
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"host", b"testserver")],
                   "scheme": "http", "server": ("testserver", 80), "query_string": b""})
    assert set(rts._seo_context(req)) == {"canonical_url", "og_image_url", "google_site_verification", "org_jsonld"}


# 5 ─ noindex
def test_chat_html_is_noindex_and_index_html_is_not():
    chat_head = _head_of((ROOT / "templates" / "chat.html").read_text(encoding="utf-8"))
    index_src = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert chat_head.count('<meta name="robots" content="noindex, nofollow">') == 1
    assert "noindex" not in index_src.lower()


def test_rendered_home_page_is_not_noindex(app_env):
    idx, _rts, _ = app_env
    assert "noindex" not in _client(idx).get("/").text.lower()


# 6 ─ og-image
def test_og_image_png_public_512(app_env):
    idx, _rts, db_names = app_env
    c = _client(idx)
    r = c.get("/og-image.png", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content[:8] == PNG_MAGIC
    assert struct.unpack(">II", r.content[16:24]) == (512, 512)             # the square 512 logo
    assert r.headers["cache-control"].startswith("public")
    assert "set-cookie" not in r.headers and not c.cookies
    assert [n for n, m in db_names.items() if m.mock_calls] == []
    assert c.head("/og-image.png").status_code == 200


# 7 ─ cache headers: only the public routes are public
def test_only_the_public_routes_are_cacheable(app_env):
    idx, _rts, _ = app_env
    c = _client(idx)
    for path in ("/robots.txt", "/sitemap.xml", "/og-image.png", "/og-card.png", "/favicon.ico"):
        assert c.get(path).headers["cache-control"].startswith("public"), path
    for path in ("/", "/does-not-exist-xyz"):
        assert "no-store" in c.get(path).headers["cache-control"], path


# 8 ─ index.html rendered by a route that does not pass the new keys
def test_index_render_without_the_new_keys_still_200_and_has_no_seo_tags(app_env, monkeypatch):
    idx, rts, _ = app_env
    monkeypatch.setattr(rts, "users_collection", MagicMock(find_one=MagicMock(return_value=None)))
    r = _client(idx).post("/login", data={"email": "nobody@example.test", "password": "wrong-password"}, follow_redirects=False)
    assert r.status_code == 200 and "Invalid email or password." in r.text
    for frag in ('rel="canonical"', "og:title", "og:image", 'name="description"', "application/ld+json",
                 "google-site-verification", 'name="twitter:card"'):
        assert frag not in r.text, frag
    assert 'rel="icon" href="/favicon.ico?v=3"' in r.text                       # the rest of the head is unchanged


def test_about_section_is_hidden_once_a_conversation_has_started(app_env, monkeypatch):
    idx, rts, _ = app_env
    c = _client(idx)
    c.get("/")                                                      # a returning browser: it now holds its identity cookies
    monkeypatch.setattr(rts, "owner_has_started_conversation", lambda owner_key, on_error=True: True)
    monkeypatch.setattr(rts, "get_user_chat_list", lambda owner_key: [])
    assert 'class="seo-about"' not in c.get("/").text
    tpl = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'class="seo-about"' not in tpl                                        # the block no longer exists in the template


# 9 ─ og-card: the 1200x630 social preview, same logo artwork on the app's dark background; /og-image.png is untouched
def test_og_card_png_is_public_1200x630_and_og_image_is_unchanged(app_env):
    idx, _rts, db_names = app_env
    c = _client(idx)
    r = c.get("/og-card.png", follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/png")
    assert r.content[:8] == PNG_MAGIC and struct.unpack(">II", r.content[16:24]) == (1200, 630)
    assert r.headers["cache-control"].startswith("public") and "set-cookie" not in r.headers and not c.cookies
    assert [n for n, m in db_names.items() if m.mock_calls] == []
    assert c.head("/og-card.png").status_code == 200
    old = c.get("/og-image.png")
    assert old.status_code == 200 and struct.unpack(">II", old.content[16:24]) == (512, 512)   # existing route intact
    assert old.content == (ROOT / "logo" / "flintel-512.png").read_bytes()
    home = _head_of(c.get("/").text)
    assert json.loads(_jsonld(home))["logo"] == "http://testserver/og-image.png"              # the Organization logo stays square


def test_og_card_keeps_the_logo_artwork_exactly(app_env):
    import io
    from PIL import Image
    idx, _rts, _ = app_env
    card = Image.open(io.BytesIO(_client(idx).get("/og-card.png").content)).convert("RGB")
    logo = Image.open(ROOT / "logo" / "flintel-512.png").convert("RGBA")
    expected = Image.new("RGBA", (512, 512), (23, 22, 21, 255))
    expected.alpha_composite(logo)                                                            # the logo, unscaled, on #171615
    box = ((1200 - 512) // 2, (630 - 512) // 2)
    assert card.crop(box + (box[0] + 512, box[1] + 512)).tobytes() == expected.convert("RGB").tobytes()
    assert card.getpixel((5, 5)) == (23, 22, 21) and card.getpixel((1194, 624)) == (23, 22, 21)   # plain dark elsewhere


def test_pricing_page_uses_the_og_card(app_env):
    idx, _rts, _ = app_env
    head = _head_of(_client(idx).get("/billing/upgrade").text)
    assert '<meta property="og:image" content="http://testserver/og-card.png">' in head
    assert 'content="1200"' in head and 'content="630"' in head and 'content="summary_large_image"' in head


# 10 ─ a database failure must not hide the public home content from a visitor with no identity (first visit / crawler)
def _failing_chats(idx, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(idx, "chats_collection", MagicMock(find_one=boom, find=boom, aggregate=boom))
    return calls


def test_lookup_failure_keeps_the_first_visit_home_for_a_visitor_without_cookies(app_env, monkeypatch):
    idx, _rts, _ = app_env
    calls = _failing_chats(idx, monkeypatch)
    html = _client(idx).get("/").text                       # a brand-new client: no cookie of any kind
    assert 'class="seo-about"' not in html and "What's the internet saying?" in html
    assert 'class="explore-panel"' in html                    # the first-visit hero and Explore stay (fails open to "not started")
    assert 'id="home-greeting"' not in html                   # the stable H1, not the returning-user greeting
    assert calls                                              # the lookup was attempted (once) and its failure fails open
    assert len(calls) == 1                                    # and not repeated within one render


def test_lookup_failure_keeps_the_existing_behaviour_for_a_returning_visitor(app_env, monkeypatch):
    idx, _rts, _ = app_env
    c = _client(idx)
    assert c.get("/").status_code == 200 and c.cookies           # the first visit hands out the session + guest cookies
    calls = _failing_chats(idx, monkeypatch)
    html = c.get("/").text                                    # same browser again: now it HAS an identity
    assert calls and 'class="seo-about"' not in html          # lookup attempted; the failure still counts as "started"


def test_a_returning_logged_out_visitor_with_no_chats_gets_the_first_visit_home_without_about(app_env, monkeypatch):
    idx, _rts, _ = app_env
    c = _client(idx)
    c.get("/")
    monkeypatch.setattr(idx, "chats_collection", MagicMock(find_one=lambda *a, **k: None))
    html = c.get("/").text
    assert 'class="seo-about"' not in html and 'class="explore-panel"' in html


def test_a_signed_in_owner_with_no_conversation_does_not_see_the_about_section(app_env, monkeypatch):
    idx, rts, _ = app_env
    html = _signed_in_home_without_conversation(idx, rts, monkeypatch)
    assert 'class="seo-about"' not in html and 'class="explore-panel"' in html and 'id="search-input"' in html


def test_fresh_visitor_detection_never_assumes_an_identity(app_env, monkeypatch):
    idx, rts, _ = app_env

    class Req:
        def __init__(self, session, cookie_ok=False):
            self.session, self._ok = session, cookie_ok
    real = rts._read_guest_cookie
    try:
        rts._read_guest_cookie = lambda request: "a" * 32 if request._ok else None
        assert rts._is_fresh_visitor(Req({})) is True
        assert rts._is_fresh_visitor(Req({"active_chat_id": "x"})) is True                     # unrelated session keys
        assert rts._is_fresh_visitor(Req({"user_id": "u1"})) is False                         # signed in
        assert rts._is_fresh_visitor(Req({"anon_id": "b" * 32})) is False                     # returning guest (session)
        assert rts._is_fresh_visitor(Req({}, cookie_ok=True)) is False                        # returning guest (signed cookie)
        assert rts._is_fresh_visitor(object()) is False                                       # anything unexpected -> old behaviour
    finally:
        rts._read_guest_cookie = real
    monkeypatch.setattr(idx, "chats_collection", MagicMock(find_one=lambda *a, **k: None))   # the fixture DB is a stub
    forged = _client(idx)
    forged.cookies.set("fl_gid", "not-a-valid-signature")
    page = forged.get("/").text
    assert rts._read_guest_cookie.__name__ and "What's the internet saying?" in page and 'class="seo-about"' not in page
    # a forged cookie proves nothing: it is still a first-time visit (hero + Explore, no About)


# --- six cases of the About-section rule (fresh / existing owner / lookup failure) ---------------------------------

def test_case1_fresh_visitor_healthy_db_gets_the_first_visit_home_without_about(app_env, monkeypatch):
    idx, _rts, _ = app_env
    monkeypatch.setattr(idx, "chats_collection", MagicMock(find_one=lambda *a, **k: None))
    html = _client(idx).get("/").text
    assert 'class="seo-about"' not in html and 'class="explore-panel"' in html


def test_case2_fresh_visitor_lookup_failure_gets_the_first_visit_home_without_about(app_env, monkeypatch):
    idx, _rts, _ = app_env
    _failing_chats(idx, monkeypatch)
    html = _client(idx).get("/").text
    assert 'class="seo-about"' not in html and 'class="explore-panel"' in html


def test_case3_existing_owner_with_conversations_does_not_see_about(app_env, monkeypatch):
    idx, _rts, _ = app_env
    c = _client(idx)
    c.get("/")
    monkeypatch.setattr(idx, "chats_collection", MagicMock(find_one=lambda *a, **k: {"_id": 1}))
    assert 'class="seo-about"' not in c.get("/").text


def test_case4_existing_owner_lookup_failure_still_counts_as_started(app_env, monkeypatch):
    idx, _rts, _ = app_env
    c = _client(idx)
    c.get("/")
    _failing_chats(idx, monkeypatch)
    assert 'class="seo-about"' not in c.get("/").text


def test_case5_get_owner_raising_follows_the_visitor_kind(app_env, monkeypatch):
    idx, rts, _ = app_env
    def boom(request): 
        raise RuntimeError("secret-detail")
    monkeypatch.setattr(rts, "get_owner", boom)
    ctx = lambda fresh: {"request": types.SimpleNamespace(state=types.SimpleNamespace(fresh_visitor=fresh))}
    assert rts._tpl_conversation_started(ctx(True)) is False       # first-time visitor: About stays
    assert rts._tpl_conversation_started(ctx(False)) is True       # existing owner: unchanged


def test_case6_on_error_parameter_and_sanitized_logging(app_env, monkeypatch, caplog):
    idx, rts, _ = app_env
    _failing_chats(idx, monkeypatch)
    with caplog.at_level("WARNING"):
        assert rts.owner_has_started_conversation("o1") is True                    # default: treated as started
        assert rts.owner_has_started_conversation("o1", on_error=False) is False
    assert "lookup failed" in caplog.text
    assert "database unavailable" not in caplog.text                                      # no raw exception text is logged
