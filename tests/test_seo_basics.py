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
    for allowed in ("/", "/billing/upgrade", "/favicon.ico", "/og-image.png", "/robots.txt", "/sitemap.xml"):
        assert not any(allowed.startswith(d) for d in EXPECTED_DISALLOW), f"{allowed} must stay crawlable"


# 2 ─ sitemap.xml
def test_sitemap_xml_valid_and_only_the_home_page(app_env):
    idx, _rts, db_names = app_env
    c = _client(idx)
    r = c.get("/sitemap.xml", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(r.content)                                       # valid XML
    assert root.tag == SITEMAP_NS + "urlset"
    urls = [u.find(SITEMAP_NS + "loc").text for u in root.findall(SITEMAP_NS + "url")]
    assert urls == ["http://testserver/"]
    assert "/chat/" not in r.text and "/billing/upgrade" not in r.text and "lastmod" not in r.text
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
    assert 'content="https://www.example.test/og-image.png"' in html
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
    assert '<meta name="description" content="Search a brand, product or topic' in head
    assert '<link rel="canonical" href="http://testserver/">' in head
    assert '<meta property="og:title" content="Flintel — What\'s the internet saying?">' in head
    assert '<meta property="og:image" content="http://testserver/og-image.png">' in head
    assert '<meta property="og:url" content="http://testserver/">' in head
    assert '<meta property="og:type" content="website">' in head and '<meta name="twitter:card" content="summary">' in head
    data = json.loads(_jsonld(head))                                       # valid JSON
    assert data == {"@context": "https://schema.org", "@type": "Organization", "name": "Flintel",
                    "url": "http://testserver/", "logo": "http://testserver/og-image.png"}
    assert "potentialAction" not in head and "sameAs" not in head and "SearchAction" not in head
    assert head.index('rel="apple-touch-icon"') < head.index('name="description"')   # after the favicon links


def test_description_names_only_platforms_flintel_really_searches(app_env):
    idx, _rts, _ = app_env
    head = _head_of(_client(idx).get("/").text)
    desc = re.search(r'<meta name="description" content="([^"]*)"', head).group(1)
    assert len(desc) <= 160
    for name in ("Reddit", "X / Twitter", "LinkedIn", "Facebook"):
        assert name in desc
    assert "Hacker News" not in desc                                       # not a source Flintel has


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
    for path in ("/robots.txt", "/sitemap.xml", "/og-image.png", "/favicon.ico"):
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
    assert 'rel="icon" href="/favicon.ico"' in r.text                       # the rest of the head is unchanged
