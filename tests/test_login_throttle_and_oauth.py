"""Failed-login throttle (login_throttle.py + /login), OAuth redirect_uri construction, staging security-config validation."""
import datetime as dt
import sys

import mongomock
import pytest

import login_throttle as lt
import origin_guard as og
from tests.test_guest_gate import auth, env, fake, gg, load, mongomock as _mm  # noqa: F401  (shared fixtures)

UTC = dt.timezone.utc
JSON = {"Accept": "application/json"}


class Clk:
    def __init__(self):
        self.t = dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += dt.timedelta(seconds=s)


def _t(clk=None, **kw):
    return lt.LoginThrottle(mongomock.MongoClient()["t"]["c"], clock=clk or Clk(), **kw), clk


def test_blocks_after_the_pair_limit_and_only_for_that_pair():
    clk = Clk()
    t, _ = _t(clk, max_pair=3, max_ip=100)
    for _ in range(3):
        assert t.retry_after("1.1.1.1", "a@x.com") == 0
        t.record_failure("1.1.1.1", "a@x.com")
    assert t.retry_after("1.1.1.1", "a@x.com") > 0
    assert t.retry_after("2.2.2.2", "a@x.com") == 0        # another client cannot be locked out of / lock out the account
    assert t.retry_after("1.1.1.1", "b@x.com") == 0


def test_per_address_limit_bounds_one_client_trying_many_accounts():
    t, _ = _t(max_pair=50, max_ip=4)
    for i in range(4):
        t.record_failure("9.9.9.9", f"user{i}@x.com")
    assert t.retry_after("9.9.9.9", "fresh@x.com") > 0


def test_window_expires_and_success_clears():
    clk = Clk()
    t, _ = _t(clk, window_seconds=300, max_pair=2, max_ip=100)
    t.record_failure("1.1.1.1", "a@x.com"); t.record_failure("1.1.1.1", "a@x.com")
    assert t.retry_after("1.1.1.1", "a@x.com") > 0
    clk.advance(301)
    assert t.retry_after("1.1.1.1", "a@x.com") == 0
    t.record_failure("1.1.1.1", "a@x.com"); t.record_failure("1.1.1.1", "a@x.com")
    t.clear("1.1.1.1", "a@x.com")
    assert t.retry_after("1.1.1.1", "a@x.com") == 0


def test_stores_no_raw_email_or_ip_and_fails_open_on_storage_errors():
    coll = mongomock.MongoClient()["t"]["c"]
    t = lt.LoginThrottle(coll)
    t.record_failure("203.0.113.7", "secret.person@example.com")
    blob = repr(list(coll.find()))
    assert "203.0.113.7" not in blob and "secret.person" not in blob

    class Broken:
        def find_one(self, *a, **k): raise RuntimeError("down")
        update_one = delete_one = find_one
    b = lt.LoginThrottle(Broken())
    assert b.retry_after("1.1.1.1", "a@x.com") == 0
    b.record_failure("1.1.1.1", "a@x.com"); b.clear("1.1.1.1", "a@x.com")     # none raise


@pytest.fixture
def throttled(auth, monkeypatch):
    cfg = sys.modules["config"]
    monkeypatch.setattr(cfg, "LOGIN_RATE_LIMIT_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "LOGIN_RATE_WINDOW_SECONDS", 900, raising=False)
    monkeypatch.setattr(cfg, "LOGIN_MAX_FAILURES_PER_ACCOUNT_IP", 3, raising=False)
    monkeypatch.setattr(cfg, "LOGIN_MAX_FAILURES_PER_IP", 100, raising=False)
    sys.modules["database"].email_codes_collection = mongomock.MongoClient()["thr"]["codes"]
    auth["users"].insert_one({"email": "u@example.com", "password_hash": "h:right-password"})
    return auth


def _login(a, pw, email="u@example.com"):
    return a["client"].post("/login", data={"email": email, "password": pw}, headers=JSON)


def test_login_route_throttles_repeated_failures_with_429_and_retry_after(throttled):
    for _ in range(3):
        assert _login(throttled, "wrong").status_code == 401
    r = _login(throttled, "wrong")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0 and r.json()["ok"] is False
    assert _login(throttled, "right-password").status_code == 429          # blocked even with the right password until the window ends


def test_a_successful_login_clears_earlier_failures(throttled):
    assert _login(throttled, "wrong").status_code == 401
    assert _login(throttled, "wrong").status_code == 401
    assert _login(throttled, "right-password").json()["ok"] is True
    for _ in range(3):
        assert _login(throttled, "wrong").status_code == 401                  # counter restarted from zero


def test_disabled_flag_means_no_throttling(throttled, monkeypatch):
    monkeypatch.setattr(sys.modules["config"], "LOGIN_RATE_LIMIT_ENABLED", False, raising=False)
    for _ in range(8):
        assert _login(throttled, "wrong").status_code == 401


# ── OAuth redirect_uri ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_oauth_redirect_uses_site_url_and_ignores_hostile_headers(auth, monkeypatch):
    rt = auth["rt"]
    captured = {}

    class O:
        google = None
        async def authorize_redirect(self, request, redirect_uri):
            captured["uri"] = redirect_uri
            from starlette.responses import RedirectResponse
            return RedirectResponse("https://accounts.google.test/a", status_code=302)
    O.google = O()
    monkeypatch.setattr(rt, "oauth", O())
    monkeypatch.setattr(sys.modules["config"], "SITE_URL", "https://staging.flintel.example", raising=False)
    auth["client"].get("/auth/google/login", headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example",
                                                       "X-Forwarded-Proto": "http"}, follow_redirects=False)
    assert captured["uri"] == "https://staging.flintel.example/auth/google/callback"


def test_oauth_redirect_without_site_url_is_request_derived_for_local_dev(auth, monkeypatch):
    monkeypatch.setattr(sys.modules["config"], "SITE_URL", "", raising=False)
    class U:
        hostname = "localhost"
    class R:
        url = U()
        base_url = "http://localhost:8000/"
        def url_for(self, name): return "http://localhost:8000/auth/google/callback"
    assert auth["rt"]._oauth_redirect_uri(R()) == "http://localhost:8000/auth/google/callback"


# ── staging config validation ─────────────────────────────────────────────────────────────────────────────────────────────────────────
GOOD = dict(env="staging", trusted_hosts="staging.flintel.example", origin_check=True, cookie_secure=True,
            site_url="https://staging.flintel.example")


def test_staging_config_ok_when_complete():
    assert og.security_config_problems(**GOOD) == []


@pytest.mark.parametrize("change,needle", [
    ({"trusted_hosts": ""}, "TRUSTED_HOSTS is empty"),
    ({"trusted_hosts": "*"}, "wildcard"),
    ({"origin_check": False}, "ORIGIN_CHECK_ENABLED"),
    ({"cookie_secure": False}, "SESSION_COOKIE_SECURE"),
    ({"site_url": ""}, "SITE_URL"),
    ({"site_url": "http://staging.flintel.example"}, "SITE_URL"),
    ({"site_url": "https://other.example"}, "not listed in TRUSTED_HOSTS"),
])
def test_staging_config_failures(change, needle):
    probs = og.security_config_problems(**{**GOOD, **change})
    assert any(needle in p for p in probs), probs


def test_non_staging_is_not_judged():
    assert og.security_config_problems("", "", False, False, "") == []
    assert og.security_config_problems("production", "", False, False, "") == []


def test_index_refuses_to_start_in_staging_without_protection():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "index.py").read_text()
    assert "security_config_problems(" in src and "Refusing to start in staging" in src


# ── throttle during store failure, logout, proxy hops ───────────────────────────────────────────────────────────────────────────────────
def test_store_failure_falls_back_to_bounded_per_process_counters():
    class Broken:
        def find_one(self, *a, **k): raise RuntimeError("down")
        update_one = delete_one = find_one
    clk = Clk()
    t = lt.LoginThrottle(Broken(), clock=clk, max_pair=3, max_ip=100, window_seconds=300)
    for _ in range(3):
        assert t.retry_after("1.1.1.1", "a@x.com") == 0
        t.record_failure("1.1.1.1", "a@x.com")                       # never raises
    assert t.retry_after("1.1.1.1", "a@x.com") > 0                   # still limited while the shared store is down
    assert t.retry_after("2.2.2.2", "a@x.com") == 0
    clk.advance(301)
    assert t.retry_after("1.1.1.1", "a@x.com") == 0
    t.record_failure("1.1.1.1", "a@x.com"); t.clear("1.1.1.1", "a@x.com")
    assert t.retry_after("1.1.1.1", "a@x.com") == 0


def _signed_in(a):
    a["users"].insert_one({"email": "u@example.com", "password_hash": "h:right-password"})
    assert a["client"].post("/login", data={"email": "u@example.com", "password": "right-password"}, headers=JSON).json()["ok"] is True


def _session_user(a):
    """Is the browser session a signed-in one? Decoded from the signed cookie with the app's own key (no network)."""
    import base64, json as _j
    from itsdangerous import TimestampSigner
    ck = [c.value for c in a["client"].cookies.jar if c.name == "session"]
    if not ck:
        return None
    key = str(sys.modules["config"].SESSION_SECRET_KEY)
    data = TimestampSigner(key).unsign(ck[-1].encode(), max_age=3600)
    sess = _j.loads(base64.b64decode(data))
    return sess.get("user_email") or sess.get("email") or (sess or None)


def test_get_logout_still_works_for_same_origin_and_direct_navigation(auth):
    _signed_in(auth)
    assert _session_user(auth)
    r = auth["client"].get("/logout", headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert not _session_user(auth)                                          # signed out


def test_cross_site_get_logout_does_not_clear_the_session(auth):
    _signed_in(auth)
    before = _session_user(auth)
    assert before
    r = auth["client"].get("/logout", headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert _session_user(auth) == before                                   # unchanged: not logged out


def test_post_logout_clears_the_session_and_is_blocked_cross_site(auth):
    _signed_in(auth)
    blocked = auth["client"].post("/logout", headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert blocked.status_code == 403
    ok = auth["client"].post("/logout", headers={"Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/"


@pytest.mark.parametrize("hops,xff,peer,expected", [
    (1, "9.9.9.9, 1.2.3.4", "10.0.0.1", "1.2.3.4"),        # one proxy: the last entry (what it appended)
    (1, "", "10.0.0.1", "10.0.0.1"),                        # no header: socket peer
    (2, "6.6.6.6, 5.5.5.5, 10.9.9.9", "10.0.0.1", "5.5.5.5"),
    (3, "5.5.5.5", "10.0.0.1", "10.0.0.1"),                 # header shorter than the hop count: do not trust it
    (0, "6.6.6.6", "10.0.0.1", "10.0.0.1"),                 # directly exposed: forged header ignored
])
def test_client_ip_counts_trusted_proxy_hops_from_the_right(auth, monkeypatch, hops, xff, peer, expected):
    monkeypatch.setattr(sys.modules["config"], "TRUSTED_PROXY_HOPS", hops, raising=False)
    class C:
        host = peer
    class Rq:
        client = C()
        headers = {"x-forwarded-for": xff}
    assert auth["rt"]._client_ip(Rq()) == expected
