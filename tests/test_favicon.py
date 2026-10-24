"""
tests/test_favicon.py — public brand-icon routes + <link rel="icon"> tags.

No real Mongo / LLM / network: index is imported against stub modules (same
pattern as tests/test_keepalive.py). Every request is made WITHOUT logging in.
"""
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODS = ("logics", "config", "index", "routes")
PNG_ROUTES = ["/favicon-48x48.png", "/favicon-96x96.png", "/favicon-192x192.png", "/apple-touch-icon.png"]
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-favicon")
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
    importlib.import_module("routes")
    yield idx, db_names
    for name in MODS:
        sys.modules.pop(name, None)


def _client(idx):
    from fastapi.testclient import TestClient
    return TestClient(idx.app)


def test_favicon_ico_public_200(app_env):
    idx, db_names = app_env
    for m in db_names.values():
        m.reset_mock()
    c = _client(idx)
    r = c.get("/favicon.ico", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/x-icon")
    assert len(r.content) > 0 and r.content[:4] == b"\x00\x00\x01\x00"      # real ICO header
    assert "public" in r.headers["cache-control"] and "no-store" not in r.headers["cache-control"]
    assert "set-cookie" not in r.headers and not c.cookies                    # no session created
    assert [n for n, m in db_names.items() if m.mock_calls] == []             # no DB touched


def test_favicon_ico_head(app_env):
    idx, _ = app_env
    h = _client(idx).head("/favicon.ico", follow_redirects=False)
    assert h.status_code == 200 and h.content == b""


@pytest.mark.parametrize("path", PNG_ROUTES)
def test_png_icons_public_200(app_env, path):
    idx, _ = app_env
    r = _client(idx).get(path, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content[:8] == PNG_MAGIC
    assert "public" in r.headers["cache-control"]


def test_other_routes_still_no_store(app_env):
    idx, _ = app_env
    r = _client(idx).get("/does-not-exist-xyz")
    assert "no-store" in r.headers.get("cache-control", "")


@pytest.mark.parametrize("tpl", ["index.html", "chat.html"])
def test_templates_carry_icon_links(tpl):
    src = (ROOT / "templates" / tpl).read_text(encoding="utf-8")
    head = src[:src.index("</head>")]
    for frag in ('rel="icon" href="/favicon.ico?v=3"', 'href="/favicon-48x48.png?v=3"',
                 'href="/favicon-96x96.png?v=3"', 'href="/favicon-192x192.png?v=3"',
                 'rel="apple-touch-icon" href="/apple-touch-icon.png?v=3"'):
        assert frag in head, f"{tpl} missing {frag}"


def test_icon_files_exist_and_square_multiple_of_48():
    from PIL import Image
    for name, size in (("flintel-48.png", 48), ("flintel-96.png", 96), ("flintel-192.png", 192)):
        im = Image.open(ROOT / "logo" / name)
        assert im.size == (size, size) and size % 48 == 0
    assert (ROOT / "logo" / "flintel.ico").stat().st_size > 0
