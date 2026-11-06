"""Host-header / same-origin hardening (flag-off by default). No network, no production config."""
import re
import pathlib
import pytest
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import origin_guard as og

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _app(trusted=(), guard=True):
    async def ok(request):
        return JSONResponse({"ok": True, "url": str(request.url_for("ok"))})
    app = Starlette(routes=[Route("/x", ok, methods=["GET", "POST", "PUT", "DELETE", "HEAD"], name="ok")])
    if guard:
        app.add_middleware(og.OriginGuard, trusted_hosts=list(trusted))
    if trusted:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(trusted))
    return app


@pytest.mark.parametrize("origin,host,expected", [
    (None, "a.example", True),
    ("https://a.example", "a.example", True),
    ("https://A.EXAMPLE", "a.example", True),
    ("https://evil.example", "a.example", False),
    ("null", "a.example", False),
    ("", "a.example", False),
    ("not a url", "a.example", False),
    ("https://a.example.evil.example", "a.example", False),
    ("https://a.example:8443", "a.example", False),
])
def test_origin_allowed(origin, host, expected):
    assert og.origin_allowed(origin, host) is expected


def test_trusted_host_list_allows_listed_origin():
    assert og.origin_allowed("https://b.example", "a.example", ["b.example"]) is True
    assert og.origin_allowed("https://c.example", "a.example", ["b.example"]) is False


def test_cross_origin_unsafe_methods_blocked():
    c = TestClient(_app(), base_url="https://a.example")
    for m in ("post", "put", "delete"):
        r = getattr(c, m)("/x", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and r.json() == {"error": "forbidden", "detail": "cross-origin request blocked"}
    assert c.post("/x", headers={"Origin": "null"}).status_code == 403


def test_same_origin_and_no_origin_allowed():
    c = TestClient(_app(), base_url="https://a.example")
    assert c.post("/x", headers={"Origin": "https://a.example"}).status_code == 200
    assert c.post("/x").status_code == 200


def test_safe_methods_never_blocked():
    c = TestClient(_app(), base_url="https://a.example")
    assert c.get("/x", headers={"Origin": "https://evil.example"}).status_code == 200
    assert c.head("/x", headers={"Origin": "https://evil.example"}).status_code == 200


def test_trusted_host_rejects_forged_host_header():
    c = TestClient(_app(trusted=["a.example"], guard=False), base_url="https://a.example")
    assert c.get("/x").status_code == 200
    assert c.get("/x", headers={"Host": "evil.example"}).status_code == 400


def test_without_trusted_host_forged_host_steers_url_for():
    """Documents the finding: absent TRUSTED_HOSTS, the Host header controls generated absolute URLs."""
    c = TestClient(_app(guard=False), base_url="https://a.example")
    r = c.get("/x", headers={"Host": "evil.example"})
    assert "evil.example" in r.json()["url"]


def test_config_defaults(monkeypatch):
    """TRUSTED_HOSTS is unset by default (no Host allow-list); the cross-site request check is ON by default and can be switched off."""
    src = (ROOT / "config.py").read_text()
    assert re.search(r'TRUSTED_HOSTS\s*=\s*os\.getenv\("TRUSTED_HOSTS",\s*""\)', src)
    assert re.search(r'ORIGIN_CHECK_ENABLED\s*=\s*os\.getenv\("ORIGIN_CHECK_ENABLED",\s*"true"\)', src)


def test_index_wiring_is_conditional():
    src = (ROOT / "index.py").read_text()
    assert "if _trusted_hosts:" in src and "TrustedHostMiddleware" in src
    assert 'getattr(_cookie_cfg, "ORIGIN_CHECK_ENABLED", False)' in src


# ── Fetch Metadata layer ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("hdrs,expected", [
    ({"sec-fetch-site": "same-origin"}, True),
    ({"sec-fetch-site": "same-origin", "origin": "https://proxy-rewritten.internal"}, True),     # Host rewriting proxies do not matter
    ({"sec-fetch-site": "none"}, True),
    ({"sec-fetch-site": "cross-site", "origin": "https://evil.example"}, False),
    ({"sec-fetch-site": "same-site", "origin": "https://sibling.a.example"}, False),
    ({"sec-fetch-site": "cross-site"}, False),
    ({"origin": "https://evil.example", "host": "a.example"}, False),                              # no Sec-Fetch-Site: Origin rule
    ({"origin": "https://a.example", "host": "a.example"}, True),
    ({"host": "a.example"}, True),                                                                  # curl / webhook
])
def test_request_allowed(hdrs, expected):
    assert og.request_allowed(hdrs) is expected


def test_trusted_host_origin_can_be_same_site():
    assert og.request_allowed({"sec-fetch-site": "same-site", "origin": "https://app.a.example"}, ["app.a.example"]) is True


def test_guard_uses_fetch_metadata_end_to_end():
    c = TestClient(_app(), base_url="https://a.example")
    assert c.post("/x", headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://a.example"}).status_code == 403
    assert c.post("/x", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
