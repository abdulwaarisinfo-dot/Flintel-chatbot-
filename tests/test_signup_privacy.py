"""Signup privacy: the sign-up endpoints must not reveal whether an email address already has an account.

Real here: /signup/code, /signup/verify, /signup (TestClient + mongomock), the real CodeStore (decoy records), the real Resend-sending code with
resend.Emails.send replaced by FakeResend (no real email is ever sent).
NOT verifiable here: real response timing over a network (a parity check on the work done is used instead), MongoDB's real unique-index errors
(mongomock's are used), Gmail.
"""
import logging
import threading
from unittest import mock

import pytest
from pymongo.errors import DuplicateKeyError

import email_verification as ev
from tests.otp_helpers import NOTICES, fresh_codes_collection, request_code, signup_with_code
from tests.resend_helpers import configure, resend_env  # noqa: F401
from tests.test_guest_gate import auth, env, fake, gg, load  # noqa: F401  (shared fixtures)
from tests.test_guest_signup_flow import Clk, _key, clk, store  # noqa: F401

JSON = {"Accept": "application/json"}
FORM = {"password": "correct-horse-1", "confirm_password": "correct-horse-1"}


def _shape(resp, address):
    """The response with the address blanked, so two responses can be compared byte for byte."""
    return resp.status_code, resp.text.replace(address, "<addr>")


def _real_send(auth, resend_env, email):
    """POST /signup/code through the REAL sending code (only the Resend HTTP call is replaced)."""
    return auth["client"].post("/signup/code", data={"email": email}, headers=JSON)


# ───────────────────────── step 1: identical answers ─────────────────────────
def test_a_new_and_an_existing_address_get_byte_identical_responses(auth, resend_env):
    configure(resend_env)
    fresh_codes_collection()
    auth["users"].insert_one({"email": "taken@example.com", "password_hash": "h:x", "provider": "email"})
    new = _real_send(auth, resend_env, "fresh@example.com")
    old = _real_send(auth, resend_env, "taken@example.com")
    assert new.status_code == old.status_code == 200
    assert _shape(new, "fresh@example.com") == _shape(old, "taken@example.com")
    assert 409 not in (new.status_code, old.status_code) and "exists" not in old.text.lower() and "reason" not in old.json()


def test_a_google_only_account_is_indistinguishable_too(auth, resend_env):
    configure(resend_env)
    fresh_codes_collection()
    auth["users"].insert_one({"email": "goog@example.com", "google_id": "g-1", "provider": "google"})
    g = _real_send(auth, resend_env, "goog@example.com")
    n = _real_send(auth, resend_env, "newbie@example.com")
    assert _shape(g, "goog@example.com") == _shape(n, "newbie@example.com") and g.status_code == 200


def test_both_paths_do_the_same_work_exactly_one_email_through_the_provider(auth, resend_env):
    configure(resend_env)
    fresh_codes_collection()
    auth["users"].insert_one({"email": "taken@example.com", "password_hash": "h:x", "provider": "email"})
    _real_send(auth, resend_env, "fresh@example.com")
    _real_send(auth, resend_env, "taken@example.com")
    calls = resend_env.resend_fake.calls
    assert len(calls) == 2
    new_mail, notice = calls[0]["params"], calls[1]["params"]
    assert new_mail["tags"] == [{"name": "purpose", "value": "signup_code"}] and notice["tags"] == [{"name": "purpose", "value": "signup_exists_notice"}]
    import re
    code_like = r"(?<![#\w])\d{6}(?!\w)"                                            # a standalone 6-digit number (not a #191817 CSS colour)
    assert re.search(code_like, new_mail["subject"]) and not re.search(code_like, notice["subject"] + notice["text"] + notice["html"])


def test_an_existing_address_receives_a_notice_that_names_no_code(auth, resend_env):
    configure(resend_env)
    fresh_codes_collection()
    auth["users"].insert_one({"email": "taken@example.com", "password_hash": "h:x"})
    _real_send(auth, resend_env, "taken@example.com")
    p = resend_env.resend_fake.calls[0]["params"]
    assert p["to"] == ["taken@example.com"] and "already have" in p["subject"].lower() and "sign in" in p["text"].lower()


def test_the_cooldown_looks_the_same_for_new_and_existing_addresses(auth, resend_env):
    configure(resend_env)
    fresh_codes_collection()
    auth["users"].insert_one({"email": "taken@example.com", "password_hash": "h:x"})
    for addr in ("fresh@example.com", "taken@example.com"):
        _real_send(auth, resend_env, addr)
    a = _real_send(auth, resend_env, "fresh@example.com")
    b = _real_send(auth, resend_env, "taken@example.com")
    assert a.status_code == b.status_code == 429 and a.json()["reason"] == b.json()["reason"] == "cooldown"
    assert a.json()["retry_after"] == b.json()["retry_after"]


def test_a_failed_send_looks_the_same_for_new_and_existing_addresses(auth, resend_env):
    configure(resend_env)
    import resend.exceptions as rx
    resend_env.resend_fake.result = rx.RateLimitError(message="slow", error_type="rate_limit_exceeded", code=429)
    fresh_codes_collection()
    auth["users"].insert_one({"email": "taken@example.com", "password_hash": "h:x"})
    a = _real_send(auth, resend_env, "fresh@example.com")
    b = _real_send(auth, resend_env, "taken@example.com")
    assert _shape(a, "fresh@example.com") == _shape(b, "taken@example.com") and a.status_code == 502


def test_the_unusable_decoy_record_never_verifies_and_counts_attempts_like_a_real_one(store, clk):
    status, code, _ = store.issue("taken@example.com", decoy=True)
    assert status == ev.OK
    assert store.verify("taken@example.com", code) == ev.INVALID                  # even the code that was generated cannot work
    real_status, real_code, _ = store.issue("fresh@example.com")
    assert store.verify("fresh@example.com", "000000" if real_code != "000000" else "111111") == ev.INVALID
    for _ in range(ev.MAX_ATTEMPTS - 1):
        assert store.verify("taken@example.com", "123456") == ev.INVALID
        assert store.verify("fresh@example.com", "654321") == ev.INVALID
    assert store.verify("taken@example.com", "123456") == store.verify("fresh@example.com", "654321") == ev.LOCKED
    assert store.issue("taken@example.com", decoy=True)[0] == ev.COOLDOWN


# ───────────────────────── step 2/3: no way through for an existing address ─────────────────────────
def _guess_until_locked(client, address):
    seen = []
    for _ in range(ev.MAX_ATTEMPTS + 1):
        r = client.post("/signup/verify", data={"email": address, "code": "000001"}, headers=JSON)
        seen.append((r.status_code, r.json().get("reason")))
    return seen


def test_wrong_code_attempts_behave_identically_for_new_and_existing_addresses(auth):
    fresh_codes_collection()
    auth["users"].insert_one({"email": "taken@example.com", "password_hash": "h:x"})
    request_code(auth["client"], "fresh@example.com")
    request_code(auth["client"], "taken@example.com")
    assert _guess_until_locked(auth["client"], "fresh@example.com") == _guess_until_locked(auth["client"], "taken@example.com")


def test_an_existing_address_cannot_reach_the_password_step(auth):
    fresh_codes_collection()
    auth["users"].insert_one({"email": "taken@example.com", "password_hash": "h:real", "provider": "email"})
    request_code(auth["client"], "taken@example.com")
    r = auth["client"].post("/signup/verify", data={"email": "taken@example.com", "code": "123456"}, headers=JSON)
    assert r.status_code == 400 and r.json()["reason"] == "invalid"
    assert auth["client"].post("/signup", data=FORM, headers=JSON).status_code == 403


def test_nothing_in_the_signup_flow_answers_409_for_an_existing_account(auth, monkeypatch):
    fresh_codes_collection()
    auth["users"].insert_one({"email": "taken@example.com", "password_hash": "h:x", "provider": "email"})
    statuses = [request_code(auth["client"], "taken@example.com")[0].status_code]
    # a verified session for an address that gained an account in the meantime (the old 409 path)
    fresh_codes_collection()
    _, code = request_code(auth["client"], "late@example.com")
    auth["client"].post("/signup/verify", data={"email": "late@example.com", "code": code}, headers=JSON)
    auth["users"].insert_one({"email": "late@example.com", "password_hash": "h:someone", "provider": "email"})
    statuses.append(auth["client"].post("/signup", data=FORM, headers=JSON).status_code)
    assert 409 not in statuses and statuses == [200, 400]


def test_the_password_step_never_signs_in_or_modifies_an_account_that_already_exists(auth):
    fresh_codes_collection()
    _, code = request_code(auth["client"], "owner@example.com")
    auth["client"].post("/signup/verify", data={"email": "owner@example.com", "code": code}, headers=JSON)
    auth["users"].insert_one({"email": "owner@example.com", "google_id": "g-9", "provider": "google"})       # a Google account appears first
    r = auth["client"].post("/signup", data=FORM, headers=JSON)
    user = auth["users"].find_one({"email": "owner@example.com"})
    assert r.status_code == 400 and r.json()["reason"] == "signup_failed" and "password_hash" not in user and user["google_id"] == "g-9"
    assert "redirect" not in r.json()
    assert auth["users"].count_documents({"email": "owner@example.com"}) == 1


def test_the_generic_message_does_not_state_that_an_account_exists(auth):
    fresh_codes_collection()
    _, code = request_code(auth["client"], "m@example.com")
    auth["client"].post("/signup/verify", data={"email": "m@example.com", "code": code}, headers=JSON)
    auth["users"].insert_one({"email": "m@example.com", "password_hash": "h:x", "provider": "email"})
    err = auth["client"].post("/signup", data=FORM, headers=JSON).json()["error"]
    assert "already exists" not in err.lower() and "sign in" in err.lower()


# ───────────────────────── duplicate-key race and repeat submit ─────────────────────────
def test_a_duplicate_key_error_on_email_is_generic_and_creates_no_session(auth, monkeypatch):
    fresh_codes_collection()
    _, code = request_code(auth["client"], "dk@example.com")
    auth["client"].post("/signup/verify", data={"email": "dk@example.com", "code": code}, headers=JSON)
    boom = DuplicateKeyError("E11000 duplicate key error index: email_1 dup key: { email: \"dk@example.com\" }", 11000, {"keyPattern": {"email": 1}})
    monkeypatch.setattr(auth["rt"], "create_email_user", mock.Mock(side_effect=boom))
    r = auth["client"].post("/signup", data=FORM, headers=JSON)
    assert r.status_code == 400 and r.json()["reason"] == "signup_failed" and auth["users"].count_documents({}) == 0


def test_two_simultaneous_password_submits_create_one_account_and_never_say_exists(auth):
    auth["users"].create_index("email", unique=True)
    fresh_codes_collection()
    c1 = auth["client"]
    _, code = request_code(c1, "twin@example.com")
    c1.post("/signup/verify", data={"email": "twin@example.com", "code": code}, headers=JSON)
    c2 = type(c1)(auth["idx"].app, base_url="https://flintel.test", cookies=dict(c1.cookies))   # a second tab holding the same verification
    results, barrier = [], threading.Barrier(2)

    def go(c):
        barrier.wait()
        results.append(c.post("/signup", data=FORM, headers=JSON))
    threads = [threading.Thread(target=go, args=(c,)) for c in (c1, c2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert auth["users"].count_documents({"email": "twin@example.com"}) == 1
    assert all(r.status_code in (200, 400, 500) and "already exists" not in r.text.lower() and r.status_code != 409 for r in results)
    assert any(r.status_code == 200 for r in results)


def test_a_repeated_submit_still_signs_in_only_with_the_matching_password(auth):
    fresh_codes_collection()
    c = auth["client"]
    _, code = request_code(c, "again@example.com")
    c.post("/signup/verify", data={"email": "again@example.com", "code": code}, headers=JSON)
    stale = dict(c.cookies)
    assert c.post("/signup", data=FORM, headers=JSON).status_code == 200
    same = type(c)(auth["idx"].app, base_url="https://flintel.test", cookies=stale).post("/signup", data=FORM, headers=JSON)
    other = type(c)(auth["idx"].app, base_url="https://flintel.test", cookies=stale).post(
        "/signup", data={"password": "a-different-pass-9", "confirm_password": "a-different-pass-9"}, headers=JSON)
    assert same.status_code == 200 and same.json()["ok"] is True
    assert other.status_code == 400 and auth["users"].count_documents({"email": "again@example.com"}) == 1


def test_a_fresh_signup_still_works_end_to_end_through_the_real_sender(auth, resend_env):
    configure(resend_env)
    fresh_codes_collection()
    r = _real_send(auth, resend_env, "brandnew@example.com")
    import re
    code = re.search(r"(\d{6})", resend_env.resend_fake.calls[0]["params"]["subject"]).group(1)
    assert auth["client"].post("/signup/verify", data={"email": "brandnew@example.com", "code": code}, headers=JSON).status_code == 200
    done = auth["client"].post("/signup", data=FORM, headers=JSON)
    assert r.status_code == 200 and done.status_code == 200 and auth["users"].count_documents({"email": "brandnew@example.com"}) == 1


def test_login_and_google_behaviour_are_untouched_by_the_privacy_change(auth):
    auth["users"].insert_one({"email": "li@example.com", "password_hash": "h:correct-horse-1", "provider": "email"})
    ok = auth["client"].post("/login", data={"email": "li@example.com", "password": "correct-horse-1"}, headers=JSON)
    bad = auth["client"].post("/login", data={"email": "li@example.com", "password": "wrong-password-1"}, headers=JSON)
    assert ok.status_code == 200 and bad.status_code in (400, 401)
    g = auth["idx"].upsert_google_user("g-777", "gg@example.com", "G")
    assert g["google_id"] == "g-777" and auth["idx"].upsert_google_user("g-777", "gg@example.com", "G")["_id"] == g["_id"]
