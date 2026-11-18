"""Regression tests: users.google_id unique index, the contradictory "already have an account" signup bug, and verification-email diagnostics.

Real here: create_email_user / upsert_google_user documents, the /signup/code -> /signup/verify -> /signup routes (mongomock + TestClient), the
index migration (user_indexes.ensure_google_id_index) and send_code's headers/logging against a fake SMTP object.
Emulated: MongoDB's rule that a unique index also indexes an explicit null (mongomock does NOT do this - see StrictUsers below).
NOT verified here: a real MongoDB server, a real Resend/SMTP relay, Gmail inbox placement, DNS (SPF/DKIM/DMARC).
"""
import datetime as dt
import logging
import re
import smtplib
from unittest import mock

import mongomock
import pytest
from pymongo.errors import DuplicateKeyError

import email_verification as ev
import user_indexes
from tests.otp_helpers import fresh_codes_collection, request_code, signup_with_code
from tests.test_guest_gate import auth, env, fake, gg, load  # noqa: F401  (shared fixtures)
from tests.test_guest_signup_flow import FakeSMTP, smtp_env, _key  # noqa: F401

JSON = {"Accept": "application/json"}
_MISSING = object()


class StrictUsers:
    """Wraps a mongomock collection and enforces MongoDB's REAL unique-index semantics for google_id:
    mode="legacy"  -> unique+sparse index: a stored google_id of null (or any value) counts, only a MISSING field is skipped;
    mode="partial" -> partial unique index on string values only."""

    def __init__(self, coll, mode):
        self._c, self.mode = coll, mode

    def __getattr__(self, name):
        return getattr(self._c, name)

    def insert_one(self, doc, *a, **k):
        gid = doc.get("google_id", _MISSING)
        applies = gid is not _MISSING if self.mode == "legacy" else isinstance(gid, str)
        if applies and self._c.count_documents({"google_id": gid}):
            raise DuplicateKeyError("E11000 duplicate key error collection: flintel_bot.flintel_users index: google_id_1 dup key: { google_id: null }",
                                    11000, {"keyPattern": {"google_id": 1}, "keyValue": {"google_id": gid}})
        return self._c.insert_one(doc, *a, **k)


def _use_strict_users(a, monkeypatch, mode):
    strict = StrictUsers(a["users"], mode)
    for mod in (a["idx"], a["rt"]):
        monkeypatch.setattr(mod, "users_collection", strict)
    return strict


# ───────────────────────── 1. google_id index ─────────────────────────
def test_the_harness_reproduces_the_original_bug_for_a_null_google_id():
    users = StrictUsers(mongomock.MongoClient().db.u, "legacy")
    users.insert_one({"email": "a@x.com", "google_id": None})
    with pytest.raises(DuplicateKeyError):
        users.insert_one({"email": "b@x.com", "google_id": None})


def test_an_email_user_is_created_without_a_google_id_field(auth):
    doc = auth["idx"].create_email_user("plain@example.com", "h:x")
    assert "google_id" not in doc and doc["provider"] == "email"


@pytest.mark.parametrize("mode", ["legacy", "partial"])
def test_two_email_users_without_google_ids_can_both_exist(auth, monkeypatch, mode):
    strict = _use_strict_users(auth, monkeypatch, mode)
    auth["idx"].create_email_user("one@example.com", "h:1")
    auth["idx"].create_email_user("two@example.com", "h:2")
    assert strict.count_documents({}) == 2


def test_two_email_signups_through_the_routes_both_succeed_on_the_legacy_index(auth, monkeypatch):
    _use_strict_users(auth, monkeypatch, "legacy")
    r1 = signup_with_code(auth["client"], "first@example.com")
    c2 = type(auth["client"])(auth["idx"].app, base_url="https://flintel.test")
    r2 = signup_with_code(c2, "second@example.com", fresh=False)
    assert r1.status_code == 200 and r1.json()["ok"] is True
    assert r2.status_code == 200 and r2.json()["ok"] is True, r2.text
    assert auth["users"].count_documents({"google_id": {"$exists": True}}) == 0


def test_google_users_get_real_unique_ids_and_coexist_with_email_users(auth, monkeypatch):
    strict = _use_strict_users(auth, monkeypatch, "partial")
    auth["idx"].create_email_user("mail@example.com", "h:1")
    g1 = auth["idx"].upsert_google_user("g-111", "g1@example.com", "G One")
    g2 = auth["idx"].upsert_google_user("g-222", "g2@example.com", "G Two")
    again = auth["idx"].upsert_google_user("g-111", "g1@example.com", "G One")           # existing Google login
    assert g1["google_id"] == "g-111" and g2["google_id"] == "g-222" and again["_id"] == g1["_id"]
    assert strict.count_documents({}) == 3
    with pytest.raises(DuplicateKeyError):                                                # a real google id stays unique
        strict.insert_one({"email": "dup@example.com", "google_id": "g-111"})


def test_google_login_links_to_an_existing_email_account(auth):
    auth["idx"].create_email_user("both@example.com", "h:1")
    linked = auth["idx"].upsert_google_user("g-333", "both@example.com", "Both")
    assert linked["google_id"] == "g-333" and linked["provider"] == "both" and auth["users"].count_documents({}) == 1


def test_index_migration_replaces_the_legacy_index_keeps_every_document_and_is_idempotent():
    c = mongomock.MongoClient().db.flintel_users
    c.create_index("email", unique=True, sparse=True)
    c.create_index("google_id", unique=True, sparse=True)
    c.insert_one({"email": "a@x.com", "google_id": None})
    c.insert_one({"email": "g@x.com", "google_id": "123"})
    before = list(c.find({}, {"_id": 0}))
    assert user_indexes.ensure_google_id_index(c) is True
    info = c.index_information()
    assert "google_id_1" not in info
    assert info["google_id_unique_partial"]["unique"] is True
    assert info["google_id_unique_partial"]["partialFilterExpression"] == {"google_id": {"$type": "string"}}
    assert list(c.find({}, {"_id": 0})) == before                                         # no document touched
    assert user_indexes.ensure_google_id_index(c) is True                                 # second startup: no-op, legacy never recreated
    assert "google_id_1" not in c.index_information() and c.count_documents({}) == 2


def test_index_migration_works_on_a_fresh_database_and_never_raises(caplog):
    assert user_indexes.ensure_google_id_index(mongomock.MongoClient().db.u) is True
    broken = mock.Mock()
    broken.index_information.side_effect = RuntimeError("boom")
    with caplog.at_level(logging.ERROR):
        assert user_indexes.ensure_google_id_index(broken) is False
    assert "NOT enforced" in caplog.text


# ───────────────────────── 2. contradictory signup flow ─────────────────────────
def test_a_non_email_unique_index_failure_is_never_reported_as_an_existing_account(auth, monkeypatch):
    fresh_codes_collection()
    _, code = request_code(auth["client"], "fault@example.com")
    auth["client"].post("/signup/verify", data={"email": "fault@example.com", "code": code}, headers=JSON)
    boom = DuplicateKeyError("E11000 ... google_id_1 dup key: { google_id: null }", 11000, {"keyPattern": {"google_id": 1}})
    monkeypatch.setattr(auth["rt"], "create_email_user", mock.Mock(side_effect=boom))
    resp = auth["client"].post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON)
    assert resp.status_code == 500 and "already" not in resp.json()["error"].lower()
    assert auth["users"].count_documents({}) == 0


def test_after_a_server_fault_the_verified_session_is_kept_so_the_user_can_retry(auth, monkeypatch):
    fresh_codes_collection()
    _, code = request_code(auth["client"], "retry@example.com")
    auth["client"].post("/signup/verify", data={"email": "retry@example.com", "code": code}, headers=JSON)
    real = auth["rt"].create_email_user
    calls = {"n": 0}

    def flaky(email, pw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real(email, pw)
    monkeypatch.setattr(auth["rt"], "create_email_user", flaky)
    form = {"password": "correct-horse-1", "confirm_password": "correct-horse-1"}
    assert auth["client"].post("/signup", data=form, headers=JSON).status_code == 500
    ok = auth["client"].post("/signup", data=form, headers=JSON)
    assert ok.status_code == 200 and auth["users"].count_documents({"email": "retry@example.com"}) == 1


def test_a_parallel_email_duplicate_still_answers_exists(auth, monkeypatch):
    fresh_codes_collection()
    _, code = request_code(auth["client"], "race2@example.com")
    auth["client"].post("/signup/verify", data={"email": "race2@example.com", "code": code}, headers=JSON)
    auth["users"].insert_one({"email": "race2@example.com", "password_hash": "h:someone-else", "provider": "email"})
    resp = auth["client"].post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON)
    assert resp.status_code == 409 and auth["users"].count_documents({"email": "race2@example.com"}) == 1


def test_a_repeated_identical_signup_request_creates_one_account_and_signs_in(auth):
    fresh_codes_collection()
    c = auth["client"]
    _, code = request_code(c, "twice@example.com")
    c.post("/signup/verify", data={"email": "twice@example.com", "code": code}, headers=JSON)
    stale = dict(c.cookies)                                                                # the browser's cookie BEFORE the first response lands
    form = {"password": "correct-horse-1", "confirm_password": "correct-horse-1"}
    first = c.post("/signup", data=form, headers=JSON)
    second_client = type(c)(auth["idx"].app, base_url="https://flintel.test", cookies=stale)
    second = second_client.post("/signup", data=form, headers=JSON)                        # the double click / retry
    assert first.status_code == 200 and second.status_code == 200 and second.json()["ok"] is True
    assert auth["users"].count_documents({"email": "twice@example.com"}) == 1
    assert second_client.get("/api/me/usage").status_code == 200


def test_a_repeat_with_a_different_password_is_not_signed_in(auth):
    fresh_codes_collection()
    c = auth["client"]
    _, code = request_code(c, "other@example.com")
    c.post("/signup/verify", data={"email": "other@example.com", "code": code}, headers=JSON)
    stale = dict(c.cookies)
    c.post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON)
    c2 = type(c)(auth["idx"].app, base_url="https://flintel.test", cookies=stale)
    resp = c2.post("/signup", data={"password": "attacker-guess-9", "confirm_password": "attacker-guess-9"}, headers=JSON)
    assert resp.status_code == 409 and auth["users"].count_documents({"email": "other@example.com"}) == 1


def test_an_old_account_is_not_treated_as_a_repeat(auth):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    auth["users"].insert_one({"email": "old@example.com", "password_hash": "h:correct-horse-1", "provider": "email", "created_at": old})
    fresh_codes_collection()
    _, code = request_code(auth["client"], "old@example.com")
    assert code is None                                                                    # step 1 refuses an existing address and sends nothing


def test_a_pending_signup_is_resumed_not_treated_as_registered_and_resend_creates_no_user(auth):
    fresh_codes_collection()
    c = auth["client"]
    r1, code1 = request_code(c, "pending@example.com")
    r2, code2 = request_code(c, "pending@example.com")                                     # immediate resend: cooldown, not "exists"
    assert r1.status_code == 200 and r2.status_code == 429 and r2.json()["reason"] == "cooldown" and code2 is None
    assert auth["users"].count_documents({}) == 0
    ok = c.post("/signup/verify", data={"email": "pending@example.com", "code": code1}, headers=JSON)
    assert ok.status_code == 200
    done = c.post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON)
    assert done.status_code == 200 and auth["users"].count_documents({}) == 1


def test_an_expired_code_is_refused_at_the_route_and_a_wrong_one_too(auth, monkeypatch):
    coll = fresh_codes_collection()
    t = {"now": dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc)}
    store = ev.CodeStore(coll, clock=lambda: t["now"])
    monkeypatch.setattr(ev, "default_store", lambda: store)
    _, code = request_code(auth["client"], "late@example.com")
    bad = auth["client"].post("/signup/verify", data={"email": "late@example.com", "code": "000000" if code != "000000" else "111111"}, headers=JSON)
    assert bad.status_code == 400 and bad.json()["reason"] == "invalid"
    t["now"] += dt.timedelta(seconds=ev.CODE_TTL_SECONDS + 1)
    late = auth["client"].post("/signup/verify", data={"email": "late@example.com", "code": code}, headers=JSON)
    assert late.status_code == 400 and late.json()["reason"] == "expired"
    assert auth["client"].post("/signup", data={"password": "correct-horse-1", "confirm_password": "correct-horse-1"}, headers=JSON).status_code == 403


def test_email_case_and_spaces_are_normalised_consistently(auth):
    r = signup_with_code(auth["client"], "  MiXed@Example.COM ")
    assert r.status_code == 200 and auth["users"].find_one({"email": "mixed@example.com"})


# ───────────────────────── 3. verification email delivery diagnostics ─────────────────────────
def _smtp_configured(smtp_env):
    smtp_env.setenv("SMTP_HOST", "smtp.resend.com")
    smtp_env.setenv("SMTP_USERNAME", "resend")
    smtp_env.setenv("SMTP_PASSWORD", "re_secret_api_key_value")
    smtp_env.setenv("EMAIL_FROM", "Flintel <no-reply@mail.flintel.test>")


def test_the_message_has_date_and_message_id_headers_and_the_acceptance_is_logged_without_the_code(smtp_env, caplog):
    _smtp_configured(smtp_env)
    with caplog.at_level(logging.INFO):
        ev.send_code("person@gmail.com", "482913")
    msg = FakeSMTP.instances[-1].sent[0]
    assert msg["Date"] and re.fullmatch(r"<.+@mail\.flintel\.test>", msg["Message-ID"])
    assert msg["Message-ID"] in caplog.text and "not proof of inbox delivery" in caplog.text
    assert "482913" not in caplog.text and "re_secret_api_key_value" not in caplog.text and "person@gmail.com" not in caplog.text


def test_a_refused_or_suppressed_recipient_is_reported_as_failed_never_as_sent(smtp_env, caplog):
    _smtp_configured(smtp_env)
    orig = FakeSMTP.send_message
    FakeSMTP.send_message = lambda self, msg: (_ for _ in ()).throw(smtplib.SMTPRecipientsRefused({"x@y.com": (550, b"5.1.1 rejected")}))
    try:
        with caplog.at_level(logging.INFO), pytest.raises(ev.EmailSendFailed):
            ev.send_code("x@y.com", "135790")
    finally:
        FakeSMTP.send_message = orig
    assert "kind=recipient_refused" in caplog.text and "550" in caplog.text
    assert "accepted by SMTP relay" not in caplog.text
    assert "135790" not in caplog.text and "re_secret_api_key_value" not in caplog.text and "rejected" not in caplog.text


def test_an_auth_failure_is_classified_and_leaks_nothing(smtp_env, caplog):
    _smtp_configured(smtp_env)
    orig = FakeSMTP.login
    FakeSMTP.login = lambda self, u, p: (_ for _ in ()).throw(smtplib.SMTPAuthenticationError(535, b"bad key re_secret_api_key_value"))
    try:
        with caplog.at_level(logging.INFO), pytest.raises(ev.EmailSendFailed):
            ev.send_code("x@y.com", "246802")
    finally:
        FakeSMTP.login = orig
    assert "kind=auth_failed" in caplog.text and "re_secret_api_key_value" not in caplog.text and "246802" not in caplog.text


def test_a_provider_failure_at_the_route_is_a_502_the_code_is_discarded_and_a_retry_is_allowed_at_once(auth, monkeypatch):
    coll = fresh_codes_collection()
    monkeypatch.setattr(ev, "send_code", mock.Mock(side_effect=ev.EmailSendFailed("SMTPRecipientsRefused")))
    r = auth["client"].post("/signup/code", data={"email": "bounce@example.com"}, headers=JSON)
    assert r.status_code == 502 and r.json()["ok"] is False and r.json()["reason"] == "send_failed"
    assert coll.count_documents({"_id": "bounce@example.com"}) == 0 and auth["users"].count_documents({}) == 0
    monkeypatch.setattr(ev, "send_code", mock.Mock())
    assert auth["client"].post("/signup/code", data={"email": "bounce@example.com"}, headers=JSON).status_code == 200
