"""
E-mail sign-up with an emailed 6-digit code (email_verification.py, routes /signup/code, /signup/verify, /signup) and the
redesigned sign-up popup (compact card, full-screen code / password / "account created" steps).

Real here: code generation, keyed hashing, expiry, attempt limit, resend cooldown, hourly caps, the signed session marker, account
creation and the guest-chat hand-over (mongomock + TestClient), and the browser behaviour (Chromium, network mocked).
NOT real here: any actual e-mail delivery - send_code is captured (or SMTP is replaced by a fake server object). Delivery through a
real provider, and the production proxy, are unverified.
"""
import datetime as dt
import json
import logging
import re
import sys

import pytest

import email_verification as ev
from tests.otp_helpers import fresh_codes_collection, request_code, signup_with_code
from tests.test_guest_gate import (  # noqa: F401  (fixtures and helpers shared with the main gate tests)
    CHAT, ORIGIN, auth, env, fake, gg, load, mongomock, needs_browser, page, render, visible, _guest_with_completed_chat,
)

UTC = dt.timezone.utc
JSON = {"Accept": "application/json"}


class Clk:
    def __init__(self):
        self.t = dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += dt.timedelta(seconds=seconds)


@pytest.fixture
def clk():
    return Clk()


@pytest.fixture
def store(clk):
    return ev.CodeStore(mongomock.MongoClient()["otp"]["codes"], clock=clk)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    import config
    monkeypatch.setattr(config, "SESSION_SECRET_KEY", "unit-test-key-not-secret", raising=False)


# ───────────────────────── the code store ───────────────────────────────────────────────────────────
def test_a_code_is_six_digits_and_only_its_keyed_hash_is_stored(store):
    status, code, _ = store.issue("a@example.com")
    assert status == ev.OK and re.fullmatch(r"\d{6}", code)
    doc = store.c.find_one({"_id": "a@example.com"})
    assert code not in json.dumps(doc, default=str) and doc["code_hash"] != code and len(doc["code_hash"]) == 64


def test_the_right_code_works_once_and_is_then_spent(store):
    _, code, _ = store.issue("a@example.com")
    assert store.verify("a@example.com", code) == ev.OK
    assert store.verify("a@example.com", code) == ev.INVALID
    assert store.c.find_one({"_id": "a@example.com"}) is None


def test_spaces_in_a_pasted_code_are_ignored_and_letters_never_match(store):
    _, code, _ = store.issue("a@example.com")
    assert store.verify("a@example.com", "abcdef") == ev.INVALID
    assert store.verify("a@example.com", f"{code[:3]} {code[3:]}") == ev.OK


def test_a_code_is_bound_to_its_own_address(store):
    _, code, _ = store.issue("a@example.com")
    assert store.verify("b@example.com", code) == ev.INVALID                            # b never asked for a code
    assert store.verify("a@example.com", code) == ev.OK


def test_a_code_still_works_just_before_ten_minutes_and_not_after(store, clk):
    _, code, _ = store.issue("a@example.com")
    clk.advance(ev.CODE_TTL_SECONDS - 1)
    assert store.verify("a@example.com", code) == ev.OK
    _, code2, _ = store.issue("b@example.com")
    clk.advance(ev.CODE_TTL_SECONDS + 1)
    assert store.verify("b@example.com", code2) == ev.EXPIRED


def test_five_wrong_guesses_lock_the_code_even_for_the_right_one(store):
    _, code, _ = store.issue("a@example.com")
    wrong = "000000" if code != "000000" else "111111"
    assert [store.verify("a@example.com", wrong) for _ in range(ev.MAX_ATTEMPTS)] == [ev.INVALID] * ev.MAX_ATTEMPTS
    assert store.verify("a@example.com", code) == ev.LOCKED
    assert store.verify("a@example.com", code) == ev.LOCKED


def test_asking_for_a_new_code_replaces_the_old_one_and_resets_attempts(store, clk):
    _, first, _ = store.issue("a@example.com")
    wrong = "000000" if first != "000000" else "111111"
    for _ in range(ev.MAX_ATTEMPTS):
        store.verify("a@example.com", wrong)
    clk.advance(ev.RESEND_COOLDOWN_SECONDS + 1)
    status, second, _ = store.issue("a@example.com")
    assert status == ev.OK
    if second != first:
        assert store.verify("a@example.com", first) == ev.INVALID                    # the old code is dead
    assert store.verify("a@example.com", second) == ev.OK


def test_resend_cooldown(store, clk):
    assert store.issue("a@example.com")[0] == ev.OK
    status, code, retry = store.issue("a@example.com")
    assert status == ev.COOLDOWN and code is None and 1 <= retry <= ev.RESEND_COOLDOWN_SECONDS
    clk.advance(ev.RESEND_COOLDOWN_SECONDS + 1)
    assert store.issue("a@example.com")[0] == ev.OK


def test_at_most_five_codes_per_address_per_hour(store, clk):
    for _ in range(ev.MAX_SENDS_PER_HOUR):
        assert store.issue("a@example.com")[0] == ev.OK
        clk.advance(ev.RESEND_COOLDOWN_SECONDS + 1)
    assert store.issue("a@example.com")[0] == ev.LIMITED
    clk.advance(3600)
    assert store.issue("a@example.com")[0] == ev.OK


def test_the_hourly_cap_survives_the_codes_own_expiry(store, clk):
    """The Mongo TTL purge must not be able to reset the hourly counter early: the document outlives the code."""
    store.issue("a@example.com")
    doc = store.c.find_one({"_id": "a@example.com"})
    assert (doc["expires_at"] - doc["code_expires_at"]).total_seconds() >= 3000


def test_a_client_address_may_request_at_most_twenty_codes_an_hour(store, clk):
    key = ev.client_key("203.0.113.9")
    results = []
    for i in range(ev.MAX_SENDS_PER_IP_PER_HOUR + 3):
        results.append(store.issue(f"user{i}@example.com", key)[0])
    assert results[:ev.MAX_SENDS_PER_IP_PER_HOUR] == [ev.OK] * ev.MAX_SENDS_PER_IP_PER_HOUR
    assert results[ev.MAX_SENDS_PER_IP_PER_HOUR:] == [ev.LIMITED] * 3
    assert store.issue("other@example.com", ev.client_key("198.51.100.1"))[0] == ev.OK     # another client is unaffected
    clk.advance(3601)
    assert store.issue("late@example.com", key)[0] == ev.OK


def test_a_discarded_code_does_not_burn_the_cooldown(store):
    assert store.issue("a@example.com")[0] == ev.OK
    store.discard("a@example.com")
    assert store.issue("a@example.com")[0] == ev.OK


def test_only_one_of_two_parallel_correct_checks_succeeds(store):
    _, code, _ = store.issue("a@example.com")
    results = sorted([store.verify("a@example.com", code), store.verify("a@example.com", code)])
    assert results == [ev.INVALID, ev.OK]


@pytest.mark.parametrize("bad", ["", "no-at-sign", "a@b", "a b@example.com", "a@example.com\nBcc: x@evil.example", "<x>@example.com",
                                  "a@example.com,b@example.com", "x" * 250 + "@example.com"])
def test_malformed_addresses_are_refused(bad):
    assert ev.normalise_email(bad) is None


def test_addresses_are_normalised():
    assert ev.normalise_email("  Person@Example.COM ") == "person@example.com"


# ───────────────────────── delivery ─────────────────────────────────────────────────────────────────
class FakeSMTP:
    instances = []
    fail = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls, self.sent = [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise OSError("550 mailbox unavailable for secret-password-value")
        self.sent.append(msg)


@pytest.fixture
def smtp_env(monkeypatch):
    for k in ("EMAIL_BACKEND", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_USE_SSL", "SMTP_USE_TLS", "EMAIL_FROM"):
        monkeypatch.delenv(k, raising=False)
    FakeSMTP.instances, FakeSMTP.fail = [], False
    monkeypatch.setattr(ev.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(ev.smtplib, "SMTP_SSL", type("FakeSSL", (FakeSMTP,), {}))
    return monkeypatch


def test_nothing_is_sent_when_no_provider_is_configured(smtp_env):
    with pytest.raises(ev.EmailNotConfigured):
        ev.send_code("a@example.com", "123456")
    assert FakeSMTP.instances == []


def test_smtp_needs_a_sender_address(smtp_env):
    smtp_env.setenv("SMTP_HOST", "smtp.example.test")
    with pytest.raises(ev.EmailNotConfigured):
        ev.send_code("a@example.com", "123456")


def test_smtp_delivery_uses_tls_login_and_carries_the_code(smtp_env):
    smtp_env.setenv("EMAIL_BACKEND", "smtp")
    smtp_env.setenv("SMTP_HOST", "smtp.example.test")
    smtp_env.setenv("SMTP_USERNAME", "mailer")
    smtp_env.setenv("SMTP_PASSWORD", "placeholder-not-real")
    smtp_env.setenv("EMAIL_FROM", "Flintel <no-reply@example.test>")
    ev.send_code("a@example.com", "482913")
    s = FakeSMTP.instances[0]
    assert (s.host, s.port) == ("smtp.example.test", 587) and s.calls == ["starttls", ("login", "mailer", "placeholder-not-real")]
    m = s.sent[0]
    assert m["To"] == "a@example.com" and m["From"].endswith("<no-reply@example.test>") and "482913" in m["Subject"]
    plain = m.get_body(preferencelist=("plain",)).get_content()
    assert "482913" in plain and "10 minutes" in plain


def test_port_465_uses_implicit_ssl_and_no_starttls(smtp_env):
    smtp_env.setenv("SMTP_HOST", "smtp.example.test")
    smtp_env.setenv("SMTP_PORT", "465")
    smtp_env.setenv("EMAIL_FROM", "no-reply@example.test")
    ev.send_code("a@example.com", "111111")
    assert FakeSMTP.instances[0].port == 465 and "starttls" not in FakeSMTP.instances[0].calls


def test_a_failed_send_is_reported_without_leaking_the_code_or_the_password(smtp_env, caplog):
    smtp_env.setenv("SMTP_HOST", "smtp.example.test")
    smtp_env.setenv("SMTP_PASSWORD", "secret-password-value")
    smtp_env.setenv("SMTP_USERNAME", "mailer")
    smtp_env.setenv("EMAIL_FROM", "no-reply@example.test")
    FakeSMTP.fail = True
    with caplog.at_level(logging.DEBUG), pytest.raises(ev.EmailSendFailed):
        ev.send_code("a@example.com", "735190")
    assert "735190" not in caplog.text and "secret-password-value" not in caplog.text


def test_the_dev_console_backend_logs_the_code_but_is_refused_on_an_https_site(smtp_env, caplog):
    import config
    smtp_env.setenv("EMAIL_BACKEND", "console")
    smtp_env.setattr(config, "SITE_URL", "", raising=False)
    with caplog.at_level(logging.WARNING):
        ev.send_code("a@example.com", "246810")
    assert "246810" in caplog.text and "DEV ONLY" in caplog.text
    smtp_env.setattr(config, "SITE_URL", "https://flintel.example", raising=False)
    with pytest.raises(ev.EmailNotConfigured):
        ev.send_code("a@example.com", "246810")


# ───────────────────────── the routes ───────────────────────────────────────────────────────────────
def _client2(a):
    return type(a["client"])(a["idx"].app, base_url="https://flintel.test")


def test_requesting_a_code_emails_it_and_creates_no_account(auth):
    fresh_codes_collection()
    resp, code = request_code(auth["client"], "New@Example.com")
    assert resp.status_code == 200 and resp.json()["ok"] is True and resp.json()["email"] == "new@example.com" and re.fullmatch(r"\d{6}", code)
    assert auth["users"].count_documents({}) == 0


@pytest.mark.parametrize("bad", ["", "nope", "a@b", "x@y@z.com"])
def test_a_bad_address_is_refused_and_nothing_is_sent(auth, bad):
    fresh_codes_collection()
    resp, code = request_code(auth["client"], bad)
    assert resp.status_code in (400, 422) and code is None


def test_an_existing_account_is_told_so_and_is_not_emailed(auth):
    fresh_codes_collection()
    auth["users"].insert_one({"email": "have@example.com", "password_hash": "h:x"})
    resp, code = request_code(auth["client"], "have@example.com")
    assert resp.status_code == 409 and resp.json()["reason"] == "exists" and code is None


def test_without_a_provider_the_signup_is_unavailable_and_stores_nothing(auth, monkeypatch):
    coll = fresh_codes_collection()
    for k in ("EMAIL_BACKEND", "SMTP_HOST", "EMAIL_FROM"):
        monkeypatch.delenv(k, raising=False)
    resp = auth["client"].post("/signup/code", data={"email": "a@example.com"}, headers=JSON)
    assert resp.status_code == 503 and resp.json()["reason"] == "unavailable" and "Google" in resp.json()["error"]
    assert coll.count_documents({"_id": "a@example.com"}) == 0 and auth["users"].count_documents({}) == 0


def test_a_send_failure_is_reported_and_can_be_retried_at_once(auth, monkeypatch):
    fresh_codes_collection()

    def boom(to, code):
        raise ev.EmailSendFailed("OSError")
    monkeypatch.setattr(ev, "send_code", boom)
    resp = auth["client"].post("/signup/code", data={"email": "a@example.com"}, headers=JSON)
    assert resp.status_code == 502 and resp.json()["reason"] == "send_failed"
    again, code = request_code(auth["client"], "a@example.com")
    assert again.status_code == 200 and code


def test_a_second_request_inside_the_cooldown_is_refused(auth):
    fresh_codes_collection()
    assert request_code(auth["client"], "a@example.com")[0].status_code == 200
    resp, code = request_code(auth["client"], "a@example.com")
    assert resp.status_code == 429 and resp.json()["reason"] == "cooldown" and 1 <= resp.json()["retry_after"] <= 30 and code is None


def test_a_wrong_code_is_refused_and_five_wrong_codes_lock_it(auth):
    fresh_codes_collection()
    _, code = request_code(auth["client"], "a@example.com")
    wrong = "000000" if code != "000000" else "111111"
    statuses = [auth["client"].post("/signup/verify", data={"email": "a@example.com", "code": wrong}, headers=JSON) for _ in range(5)]
    assert [r.status_code for r in statuses] == [400] * 5 and statuses[0].json()["reason"] == "invalid"
    locked = auth["client"].post("/signup/verify", data={"email": "a@example.com", "code": code}, headers=JSON)
    assert locked.status_code == 429 and locked.json()["reason"] == "locked"
    assert auth["client"].post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON).status_code == 403


def test_signup_without_a_verified_address_is_refused(auth):
    fresh_codes_collection()
    resp = auth["client"].post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON)
    assert resp.status_code == 403 and resp.json()["reason"] == "verification_required"
    assert auth["users"].count_documents({}) == 0


def test_the_account_is_created_for_the_verified_address_only_and_the_visitor_is_signed_in(auth):
    resp = signup_with_code(auth["client"], "real@example.com", extra={"email": "victim@example.com"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert auth["users"].count_documents({"email": "victim@example.com"}) == 0           # a forged address is ignored
    user = auth["users"].find_one({"email": "real@example.com"})
    assert user and user["password_hash"] == "h:correct-horse-1" and user["provider"] == "email"
    me = auth["client"].get("/api/me/usage")
    assert me.status_code == 200                                                          # automatically signed in


def test_verification_is_one_use_and_does_not_work_from_another_browser(auth):
    other = _client2(auth)
    other.get("/")
    resp = signup_with_code(auth["client"], "once@example.com")
    assert resp.status_code == 200
    again = auth["client"].post("/signup", data={"password": "another-pass-1", "confirm_password": "another-pass-1"}, headers=JSON)
    assert again.status_code == 403
    stolen = other.post("/signup", data={"password": "another-pass-1", "confirm_password": "another-pass-1"}, headers=JSON)
    assert stolen.status_code == 403
    assert auth["users"].count_documents({}) == 1


def test_a_verification_stops_working_after_fifteen_minutes(auth, monkeypatch):
    fresh_codes_collection()
    _, code = request_code(auth["client"], "slow@example.com")
    assert auth["client"].post("/signup/verify", data={"email": "slow@example.com", "code": code}, headers=JSON).status_code == 200
    real = ev.time.time
    monkeypatch.setattr(ev.time, "time", lambda: real() + ev.VERIFIED_SESSION_SECONDS + 5)
    resp = auth["client"].post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON)
    assert resp.status_code == 403 and auth["users"].count_documents({}) == 0


def test_a_new_code_request_withdraws_an_earlier_verification(auth):
    resp = signup_with_code(auth["client"], "first@example.com", password="short")           # verified, password rejected
    assert resp.status_code == 400
    fresh_codes_collection()
    request_code(auth["client"], "second@example.com")
    gone = auth["client"].post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON)
    assert gone.status_code == 403


def test_password_rules_still_apply_after_verification(auth):
    short = signup_with_code(auth["client"], "p@example.com", password="short")
    assert short.status_code == 400 and "8 characters" in short.json()["error"]
    mismatch = auth["client"].post("/signup", data={"password": "correct-horse-1", "confirm_password": "different-horse-1"}, headers=JSON)
    assert mismatch.status_code == 400 and "match" in mismatch.json()["error"]
    ok = auth["client"].post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON)
    assert ok.status_code == 200                                                          # the same verification covers the retry


def test_an_account_created_in_parallel_wins_cleanly(auth):
    fresh_codes_collection()
    _, code = request_code(auth["client"], "race@example.com")
    auth["client"].post("/signup/verify", data={"email": "race@example.com", "code": code}, headers=JSON)
    auth["users"].insert_one({"email": "race@example.com", "password_hash": "h:other"})
    resp = auth["client"].post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON)
    assert resp.status_code == 409 and auth["users"].count_documents({"email": "race@example.com"}) == 1


def test_a_signup_through_the_code_flow_resumes_the_guest_conversation(auth, gg):
    _guest_with_completed_chat(auth, gg)
    resp = signup_with_code(auth["client"], "guest@example.com")
    assert resp.json() == {"ok": True, "redirect": "/chat/c-guest"}
    assert auth["chats"].find_one({"chat_id": "c-guest"})["owner_key"] == "guest@example.com"


def test_the_login_route_is_unchanged(auth):
    auth["users"].insert_one({"email": "old@example.com", "password_hash": "h:correct-horse-1"})
    assert auth["client"].post("/login", data={"email": "old@example.com", "password": "correct-horse-1"}, headers=JSON).json()["ok"] is True


# ───────────────────────── the popup: size, scrolling, timing ───────────────────────────────────────
OPEN_GATE = "window.flintelGate.handleDenial(%s)" % fake({"X-Flintel-Denial": "guest-auth-required"})
TEMPLATES = [("index.html", {}), ("chat.html", {"chat": CHAT, "chat_id": "c1"})]
VIEWPORTS = [(1440, 900), (1280, 720), (1366, 640), (1024, 600), (768, 1024), (412, 915), (390, 740), (360, 640)]


def _metrics(pg):
    return pg.evaluate("""() => {
      const m = document.querySelector('#auth-overlay .auth-modal');
      const r = m.getBoundingClientRect(), cs = getComputedStyle(m);
      return {h: Math.round(r.height), w: Math.round(r.width), top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), right: Math.round(r.right),
              scroll: m.scrollHeight - m.clientHeight, overflowY: cs.overflowY, vh: innerHeight, vw: innerWidth,
              pageHScroll: document.documentElement.scrollWidth - innerWidth,
              overlayScrolls: (() => { const o = document.getElementById('auth-overlay'); return o.scrollHeight - o.clientHeight; })()};
    }""")


@needs_browser
@pytest.mark.parametrize("name,ctx", TEMPLATES)
@pytest.mark.parametrize("mode", ["signup", "login"])
@pytest.mark.parametrize("vw,vh", VIEWPORTS)
def test_the_guest_popup_is_compact_and_never_shows_a_scrollbar(page, name, ctx, mode, vw, vh):
    page.set_viewport_size({"width": vw, "height": vh})
    load(page, render(name, **ctx))
    page.evaluate(OPEN_GATE)
    page.wait_for_selector("#auth-overlay.visible")
    if mode == "login":
        page.click("#show-login-btn")
    page.wait_for_timeout(350)                                                           # the entrance animation
    m = _metrics(page)
    assert m["overflowY"] not in ("auto", "scroll"), m                                   # no scroll container at all
    assert m["scroll"] <= 0 and m["overlayScrolls"] <= 0, m                              # nothing to scroll to
    assert m["top"] >= 0 and m["bottom"] <= m["vh"], m                                   # fully inside the window
    assert m["left"] >= 0 and m["right"] <= m["vw"] and m["pageHScroll"] <= 0, m
    assert m["h"] <= 440, m                                                              # compact (the old card was taller than 560px)
    assert m["w"] <= 360 or vw <= 480, m
    assert page.errors == []


@needs_browser
def test_the_popup_keeps_the_required_content_and_hierarchy(page):
    load(page, render("index.html"))
    page.evaluate(OPEN_GATE)
    page.wait_for_selector("#auth-overlay.visible")
    dlg = page.locator("#auth-overlay [role=dialog]")
    assert page.locator("#auth-gate-title").inner_text() == "Your next insight starts here."
    assert page.locator("#signup-form").is_visible() and page.locator("#login-form").is_hidden()
    assert page.locator("#signup-form input").count() == 1                               # the e-mail address only - no passwords here
    assert page.locator("#signup-email").get_attribute("type") == "email"
    assert page.locator("#signup-form button[type=submit]").inner_text() == "Continue"
    assert page.locator("#auth-google-link").is_visible() and page.locator("#auth-apple-btn").is_hidden()
    assert "Already have an account?" in dlg.inner_text() and page.locator("#show-login-btn").is_visible()
    assert dlg.get_attribute("aria-modal") == "true"
    order = page.evaluate("""() => ['auth-gate-title','auth-google-link','signup-email','auth-gate-note','show-login-btn']
        .map(id => document.getElementById(id).getBoundingClientRect().top)""")
    assert order == sorted(order), order                                                 # title > Google > e-mail > note > sign-in link


@needs_browser
def test_a_very_short_window_may_scroll_but_never_shows_a_scrollbar(page):
    page.set_viewport_size({"width": 800, "height": 400})
    load(page, render("index.html"))
    page.evaluate(OPEN_GATE)
    page.wait_for_selector("#auth-overlay.visible")
    page.wait_for_timeout(350)
    sb = page.evaluate("(() => { const o = document.getElementById('auth-overlay'); return {w: getComputedStyle(o, '::-webkit-scrollbar').display, sw: getComputedStyle(o).scrollbarWidth}; })()")
    assert sb["w"] == "none" or sb["sw"] == "none", sb
    assert page.evaluate("document.querySelector('#auth-overlay .auth-modal').getBoundingClientRect().top") >= 0


# ───────────────────────── the full-screen steps ────────────────────────────────────────────────────
def mock_endpoints(pg, code=None, verify=None, signup=None, log=None):
    """Network mocks for the three sign-up calls. Each value is (status, body) or a list of them (consumed in order)."""
    state = {"code": code or (200, {"ok": True, "email": "a@example.com", "retry_after": 30}),
             "verify": verify or (200, {"ok": True}), "signup": signup or (200, {"ok": True, "redirect": "/chat/abc"})}

    def make(key):
        def handler(route):
            if log is not None:
                log.append((key, route.request.post_data))
            v = state[key]
            if isinstance(v, list):
                v = v.pop(0) if len(v) > 1 else v[0]
            route.fulfill(status=v[0], content_type="application/json", body=json.dumps(v[1]))
        return handler
    pg.route("**/signup/code", make("code"))
    pg.route("**/signup/verify", make("verify"))
    pg.route("**/signup", make("signup"))
    return state


def to_code_step(pg, email="a@example.com", name="index.html", ctx=None):
    load(pg, render(name, **(ctx or {})))
    pg.evaluate(OPEN_GATE)
    pg.fill("#signup-email", email)
    pg.click("#signup-form button[type=submit]")
    pg.wait_for_selector("#signup-flow.visible #flow-code.is-active")
    pg.wait_for_function("document.activeElement && document.activeElement.classList.contains('otp-box')")


def digits(pg):
    return pg.eval_on_selector_all(".otp-box", "els=>els.map(e=>e.value).join('')")


@needs_browser
@pytest.mark.parametrize("name,ctx", TEMPLATES)
def test_entering_an_email_opens_the_check_your_email_screen(page, name, ctx):
    log = []
    mock_endpoints(page, log=log)
    to_code_step(page, "a@example.com", name, ctx)
    assert not visible(page, "#auth-overlay")                                            # the compact card makes way for the full screen
    assert page.locator("#flow-code-title").inner_text() == "Check your email"
    assert "a@example.com" in page.locator("#flow-code-sub").inner_text() and "6-digit code" in page.locator("#flow-code-sub").inner_text()
    assert page.locator(".otp-box").count() == 6
    assert page.locator("#otp-confirm").inner_text() == "Confirm" and page.locator("#otp-confirm").is_disabled()
    assert page.locator("#flow-close-btn").is_visible()
    assert [k for k, _ in log] == ["code"] and "password" not in log[0][1]
    r = page.evaluate("""() => { const f = document.getElementById('signup-flow').getBoundingClientRect(), cs = getComputedStyle(document.getElementById('signup-flow')),
        h = getComputedStyle(document.getElementById('flow-code-title')).fontFamily;
        return {w: f.width, h: f.height, vw: innerWidth, vh: innerHeight, bg: cs.backgroundColor, serif: /Enriqueta|Georgia|serif/i.test(h)}; }""")
    assert r["w"] == r["vw"] and r["h"] == r["vh"] and r["bg"] == "rgb(24, 24, 24)" and r["serif"]
    assert page.errors == []


@needs_browser
def test_the_six_boxes_advance_accept_paste_and_confirm_only_when_full(page):
    mock_endpoints(page)
    to_code_step(page)
    page.keyboard.type("12")
    assert digits(page) == "12" and page.locator("#otp-confirm").is_disabled()
    page.keyboard.type("ab")                                                             # letters never enter a box
    assert digits(page) == "12"
    page.keyboard.press("Backspace")                                                     # on an empty box: removes the previous digit and steps back
    assert digits(page) == "1"
    page.keyboard.type("23456")
    assert digits(page) == "123456" and page.locator("#otp-confirm").is_enabled()
    page.keyboard.press("Backspace")
    assert page.locator("#otp-confirm").is_disabled()
    page.evaluate("""() => { const b = document.querySelector('.otp-box'); b.focus();
        const dt = new DataTransfer(); dt.setData('text', ' 98 76-54 ');
        b.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }""")
    assert digits(page) == "987654" and page.locator("#otp-confirm").is_enabled()
    page.evaluate("""() => { const b = document.querySelectorAll('.otp-box')[2]; b.focus();
        const dt = new DataTransfer(); dt.setData('text', 'no digits here');
        b.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }""")
    assert digits(page) == "987654"                                                      # a paste without digits changes nothing
    page.evaluate("""() => { const b = document.querySelector('.otp-box'); b.value = '246810'; b.dispatchEvent(new Event('input', {bubbles: true})); }""")
    assert digits(page) == "246810"                                                      # whole-code autofill (SMS / keychain) fills all boxes
    assert page.locator(".otp-box").first.get_attribute("autocomplete") == "one-time-code"
    assert page.locator(".otp-box").first.get_attribute("inputmode") == "numeric"


@needs_browser
def test_a_wrong_code_shakes_clears_and_explains(page):
    mock_endpoints(page, verify=(400, {"ok": False, "reason": "invalid", "error": "That code isn't right. Check it and try again."}))
    to_code_step(page)
    page.keyboard.type("111111")
    page.keyboard.press("Enter")                                                         # Enter confirms too
    page.wait_for_function("document.getElementById('otp-msg').textContent.length > 0")
    assert "isn't right" in page.locator("#otp-msg").inner_text()
    assert digits(page) == "" and page.locator("#otp-confirm").is_disabled()
    assert page.evaluate("document.getElementById('otp-row').classList.contains('is-error')")
    assert page.evaluate("document.activeElement === document.querySelector('.otp-box')")
    page.keyboard.type("2")
    assert page.locator("#otp-msg").inner_text() == "" or not page.evaluate("document.getElementById('otp-row').classList.contains('is-error')")


@needs_browser
def test_an_expired_or_locked_code_says_so_and_offers_a_new_one(page):
    mock_endpoints(page, verify=(400, {"ok": False, "reason": "expired", "error": "That code has expired. Request a new one."}),
                   code=[(200, {"ok": True, "email": "a@example.com", "retry_after": 1}), (200, {"ok": True, "email": "a@example.com", "retry_after": 30})])
    to_code_step(page)
    page.keyboard.type("111111")
    page.click("#otp-confirm")
    page.wait_for_function("document.getElementById('otp-msg').textContent.includes('expired')")
    page.wait_for_function("!document.getElementById('otp-resend').disabled", timeout=4000)
    page.click("#otp-resend")
    page.wait_for_function("document.getElementById('otp-msg').textContent.includes('new code')")
    assert page.locator("#otp-resend").is_disabled() and "Resend in" in page.locator("#otp-resend").inner_text()


@needs_browser
def test_resend_is_disabled_during_the_cooldown_and_counts_down(page):
    mock_endpoints(page, code=(200, {"ok": True, "email": "a@example.com", "retry_after": 3}))
    to_code_step(page)
    assert page.locator("#otp-resend").is_disabled() and re.fullmatch(r"Resend in [123]s", page.locator("#otp-resend").inner_text())
    page.wait_for_function("!document.getElementById('otp-resend').disabled", timeout=5000)
    assert page.locator("#otp-resend").inner_text() == "Resend code"


@needs_browser
def test_use_a_different_email_returns_to_the_compact_card_with_the_address_filled(page):
    mock_endpoints(page)
    to_code_step(page)
    page.click("#otp-change")
    page.wait_for_selector("#auth-overlay.visible")
    assert not visible(page, "#signup-flow") and page.input_value("#signup-email") == "a@example.com"
    assert page.evaluate("document.getElementById('auth-overlay').classList.contains('gate-mode')")        # still the guest gate


@needs_browser
def test_an_address_that_already_has_an_account_is_sent_to_sign_in(page):
    mock_endpoints(page, code=(409, {"ok": False, "reason": "exists", "error": "An account with that email already exists. Sign in instead."}))
    load(page, render("index.html"))
    page.evaluate(OPEN_GATE)
    page.fill("#signup-email", "have@example.com")
    page.click("#signup-form button[type=submit]")
    page.wait_for_selector("#login-form:not(.hidden)")
    assert page.input_value("#login-email") == "have@example.com" and "already have an account" in page.locator("#auth-error").inner_text()
    assert not visible(page, "#signup-flow")


@needs_browser
@pytest.mark.parametrize("bad", ["", "nope", "a@b"])
def test_an_invalid_address_is_stopped_in_the_card_before_any_request(page, bad):
    log = []
    mock_endpoints(page, log=log)
    load(page, render("index.html"))
    page.evaluate(OPEN_GATE)
    page.fill("#signup-email", bad)
    page.click("#signup-form button[type=submit]")
    page.wait_for_selector("#auth-error:not([hidden])")
    assert log == [] and not visible(page, "#signup-flow")


@needs_browser
def test_unavailable_email_signup_shows_the_server_message_and_keeps_google(page):
    mock_endpoints(page, code=(503, {"ok": False, "reason": "unavailable", "error": "Email sign-up is temporarily unavailable. Please use Google instead."}))
    load(page, render("index.html"))
    page.evaluate(OPEN_GATE)
    page.fill("#signup-email", "a@example.com")
    page.click("#signup-form button[type=submit]")
    page.wait_for_selector("#auth-error:not([hidden])")
    assert "Google" in page.locator("#auth-error").inner_text() and page.locator("#auth-google-link").is_visible()


@needs_browser
def test_password_step_validates_toggles_and_posts_the_resume_flag(page):
    log = []
    mock_endpoints(page, log=log)
    to_code_step(page)
    page.keyboard.type("123456")
    page.click("#otp-confirm")
    page.wait_for_selector("#flow-password.is-active")
    assert page.locator("#flow-password-title").inner_text() == "Create your password" and "a@example.com" in page.locator("#flow-password .flow-sub").inner_text()
    assert page.locator("#pw-submit").is_disabled()
    page.fill("#signup-password", "short")
    page.fill("#signup-confirm-password", "short")
    page.click("#pw-submit")
    assert "8 characters" in page.locator("#pw-msg").inner_text()
    page.fill("#signup-password", "correct-horse-1")
    page.fill("#signup-confirm-password", "different-horse-1")
    page.click("#pw-submit")
    assert "match" in page.locator("#pw-msg").inner_text()
    assert [k for k, _ in log] == ["code", "verify"]                                      # nothing was posted for the invalid attempts
    assert page.get_attribute("#signup-password", "type") == "password"
    page.click("#pw-toggle")
    assert page.get_attribute("#signup-password", "type") == "text" and page.get_attribute("#signup-confirm-password", "type") == "text"
    assert page.get_attribute("#pw-toggle", "aria-pressed") == "true"
    page.click("#pw-toggle")
    assert page.get_attribute("#signup-password", "type") == "password"
    page.fill("#signup-confirm-password", "correct-horse-1")
    page.click("#pw-submit")
    page.wait_for_selector("#flow-done.is-active")
    body = log[-1][1]
    assert log[-1][0] == "signup" and "correct-horse-1" in body and re.search(r'name="resume"\r\n\r\n1', body) and "a@example.com" not in body


@needs_browser
def test_a_normal_sign_up_from_the_sign_in_modal_uses_the_same_flow_without_the_resume_flag(page):
    log = []
    mock_endpoints(page, log=log)
    load(page, render("index.html"))
    page.evaluate("document.getElementById('auth-overlay').classList.add('visible')")
    assert page.evaluate("document.getElementById('auth-overlay').classList.contains('gate-mode')") is False
    page.click("#show-signup-btn")
    assert page.locator("#auth-modal-title").inner_text() == "Create your account"
    page.fill("#signup-email", "a@example.com")
    page.click("#signup-form button[type=submit]")
    page.wait_for_selector("#flow-code.is-active")
    page.wait_for_function("document.activeElement && document.activeElement.classList.contains('otp-box')")
    page.keyboard.type("123456")
    page.click("#otp-confirm")
    page.wait_for_selector("#flow-password.is-active")
    page.fill("#signup-password", "correct-horse-1")
    page.fill("#signup-confirm-password", "correct-horse-1")
    page.click("#pw-submit")
    page.wait_for_selector("#flow-done.is-active")
    assert re.search(r'name="resume"\r\n\r\n\r\n', log[-1][1])                              # not a guest hand-over


@needs_browser
def test_password_step_recovers_when_the_verification_has_expired_or_the_account_appeared(page):
    mock_endpoints(page, signup=[(403, {"ok": False, "reason": "verification_required", "error": "Verify your email first."})])
    to_code_step(page)
    page.keyboard.type("123456")
    page.click("#otp-confirm")
    page.wait_for_selector("#flow-password.is-active")
    page.fill("#signup-password", "correct-horse-1")
    page.fill("#signup-confirm-password", "correct-horse-1")
    page.click("#pw-submit")
    page.wait_for_selector("#auth-overlay.visible")
    assert "verification expired" in page.locator("#auth-error").inner_text() and page.input_value("#signup-email") == "a@example.com"
    assert not visible(page, "#signup-flow")


@needs_browser
def test_password_step_sends_an_existing_account_to_sign_in(page):
    mock_endpoints(page, signup=(409, {"ok": False, "error": "An account with that email already exists."}))
    to_code_step(page)
    page.keyboard.type("123456")
    page.click("#otp-confirm")
    page.wait_for_selector("#flow-password.is-active")
    page.fill("#signup-password", "correct-horse-1")
    page.fill("#signup-confirm-password", "correct-horse-1")
    page.click("#pw-submit")
    page.wait_for_selector("#auth-overlay.visible #login-form:not(.hidden)")
    assert page.input_value("#login-email") == "a@example.com" and "already exists" in page.locator("#auth-error").inner_text()


@needs_browser
def test_account_created_screen_animates_then_signs_the_visitor_into_their_conversation(page):
    mock_endpoints(page)
    page.route("**/chat/abc", lambda route: route.fulfill(status=200, content_type="text/html", body="<html><body id='landed'>chat</body></html>"))
    to_code_step(page)
    page.keyboard.type("123456")
    page.click("#otp-confirm")
    page.wait_for_selector("#flow-password.is-active")
    page.fill("#signup-password", "correct-horse-1")
    page.fill("#signup-confirm-password", "correct-horse-1")
    page.click("#pw-submit")
    page.wait_for_selector("#flow-done.is-active")
    assert page.locator("#flow-done-title").inner_text() == "Your account is ready"
    assert page.locator(".confetti i").count() >= 16 and page.locator(".done-ring").count() == 1 and page.locator(".done-check").count() == 1
    assert page.locator("#flow-close-btn").is_hidden()                                    # nothing to cancel any more
    assert page.evaluate("getComputedStyle(document.querySelector('.done-ring')).animationName") == "doneRing"
    assert page.evaluate("getComputedStyle(document.querySelector('.done-bar > span')).animationName") == "doneBar"
    page.wait_for_selector("#landed", state="attached", timeout=6500)                     # lands on the server's redirect after the animation
    assert page.url.endswith("/chat/abc")


@needs_browser
def test_continue_and_escape_skip_the_celebration(page):
    mock_endpoints(page)
    page.route("**/chat/abc", lambda route: route.fulfill(status=200, content_type="text/html", body="<html><body id='landed'>chat</body></html>"))
    to_code_step(page)
    page.keyboard.type("123456")
    page.click("#otp-confirm")
    page.wait_for_selector("#flow-password.is-active")
    page.fill("#signup-password", "correct-horse-1")
    page.fill("#signup-confirm-password", "correct-horse-1")
    page.click("#pw-submit")
    page.wait_for_selector("#flow-done.is-active")
    page.keyboard.press("Escape")                                                         # Escape cannot cancel a finished sign-up: it continues
    page.wait_for_selector("#landed", state="attached", timeout=3000)


@needs_browser
def test_reduced_motion_removes_the_animation_but_keeps_the_screen(page):
    page.emulate_media(reduced_motion="reduce")
    mock_endpoints(page)
    to_code_step(page)
    page.keyboard.type("123456")
    page.click("#otp-confirm")
    page.wait_for_selector("#flow-password.is-active")
    page.fill("#signup-password", "correct-horse-1")
    page.fill("#signup-confirm-password", "correct-horse-1")
    page.click("#pw-submit")
    page.wait_for_selector("#flow-done.is-active")
    assert page.evaluate("getComputedStyle(document.querySelector('.confetti')).display") == "none"
    assert page.evaluate("getComputedStyle(document.querySelector('.done-ring')).strokeDashoffset") in ("0px", "0")
    assert page.locator("#flow-done-title").is_visible()


@needs_browser
def test_closing_the_code_screen_closes_everything_and_never_unlocks_anything(page):
    mock_endpoints(page)
    to_code_step(page)
    page.click("#flow-close-btn")
    assert not visible(page, "#signup-flow") and not visible(page, "#auth-overlay")
    assert page.evaluate("document.getElementById('auth-overlay').classList.contains('gate-mode')") is False
    assert page.eval_on_selector_all("#auth-overlay input[name=resume], #signup-flow input[name=resume]", "els=>els.map(e=>e.value)") == ["", ""]
    page.evaluate(OPEN_GATE)                                                               # the next attempt reopens the sign-up card
    page.wait_for_selector("#auth-overlay.visible")
    assert digits(page) == ""                                                              # and no code survives


@needs_browser
def test_escape_closes_the_code_screen_and_tab_stays_inside_it(page):
    mock_endpoints(page)
    to_code_step(page)
    for _ in range(14):
        page.keyboard.press("Tab")
        assert page.evaluate("document.getElementById('signup-flow').contains(document.activeElement)")
    for _ in range(14):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("document.getElementById('signup-flow').contains(document.activeElement)")
    page.keyboard.press("Escape")
    assert not visible(page, "#signup-flow")


@needs_browser
def test_focus_returns_to_where_the_visitor_was_after_the_flow_closes(page):
    mock_endpoints(page)
    load(page, render("chat.html", chat=CHAT, chat_id="c1"))
    page.focus("#search-input")
    page.evaluate(OPEN_GATE)
    page.fill("#signup-email", "a@example.com")
    page.click("#signup-form button[type=submit]")
    page.wait_for_selector("#flow-code.is-active")
    page.keyboard.press("Escape")
    page.wait_for_function("document.activeElement && document.activeElement.id === 'search-input'")


@needs_browser
@pytest.mark.parametrize("vw,vh", [(1440, 900), (1366, 640), (412, 915), (360, 640), (320, 568)])
def test_the_full_screen_steps_fit_without_a_scrollbar_or_horizontal_overflow(page, vw, vh):
    page.set_viewport_size({"width": vw, "height": vh})
    mock_endpoints(page)
    to_code_step(page)
    def fits(step):
        return page.evaluate("""([id]) => { const f = document.getElementById('signup-flow'), s = document.getElementById(id);
            const r = s.getBoundingClientRect();
            return {over: f.scrollHeight - f.clientHeight, hover: f.scrollWidth - f.clientWidth, left: r.left, right: r.right, vw: innerWidth,
                    boxes: Array.from(document.querySelectorAll('.otp-box')).every(b => { const q = b.getBoundingClientRect(); return q.left >= 0 && q.right <= innerWidth; })}; }""", [step])
    m = fits("flow-code")
    assert m["hover"] <= 0 and m["left"] >= 0 and m["right"] <= m["vw"] and m["boxes"], m
    if vh >= 640:
        assert m["over"] <= 0, m
    page.keyboard.type("123456")
    page.click("#otp-confirm")
    page.wait_for_selector("#flow-password.is-active")
    m = fits("flow-password")
    assert m["hover"] <= 0 and m["left"] >= 0 and m["right"] <= m["vw"], m
    if vh >= 640:
        assert m["over"] <= 0, m


# ───────────────────────── template wiring ──────────────────────────────────────────────────────────
def test_the_new_popup_markup_is_identical_in_both_templates_and_has_no_scroll_rule():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "templates"
    a, b = (root / "index.html").read_text(encoding="utf-8"), (root / "chat.html").read_text(encoding="utf-8")
    blocks = ("<!-- GUEST-GATE-AUTH-MODAL-BEGIN", "<!-- GUEST-GATE-AUTH-MODAL-END -->")
    assert a[a.index(blocks[0]):a.index(blocks[1])] == b[b.index(blocks[0]):b.index(blocks[1])]
    css = a[a.index("/* GUEST-GATE-CSS-BEGIN"):a.index("/* GUEST-GATE-CSS-END */")]
    assert re.search(r"\.auth-overlay \.auth-modal \{[^}]*overflow: visible", css) and "overflow-y: auto" not in css.split("@media (max-height: 460px)")[0].split(".flow-overlay")[0]
    assert a.count('id="auth-overlay"') == 1 and a.count('id="signup-flow"') == 1
    for form in ('action="/signup/code"', 'action="/signup/verify"', 'action="/signup"'):
        assert form in a and form in b


def test_the_overlays_are_outside_main_so_soft_navigation_cannot_remove_them():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "templates"
    for name in ("index.html", "chat.html"):
        t = (root / name).read_text(encoding="utf-8")
        assert t.index('id="signup-flow"') > t.index("</main>"), name
