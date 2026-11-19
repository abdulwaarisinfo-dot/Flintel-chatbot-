"""Resend API sending, delivery records and the signed webhook endpoint.

Real here: email_verification.send_code / send_account_exists_notice (the Resend SDK's own exception classes are raised), email_delivery
(state model, de-duplication, signature verification through the SDK's Webhooks.verify), and the /signup/code and /webhooks/resend routes
(TestClient + mongomock).
Replaced: resend.Emails.send (FakeResend) - NO real Resend call and NO real email is ever made. Signing secrets and API keys in this file are
throwaway placeholders generated for the test run.
NOT verified here: the live Resend API, real webhook deliveries, MongoDB's single-document update atomicity (mongomock), Gmail inbox placement.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
import resend.exceptions as rx

import email_delivery as ed
import email_verification as ev
from tests.otp_helpers import fresh_codes_collection
from tests.resend_helpers import PLACEHOLDER_KEY, configure, resend_env, sdk_error  # noqa: F401
from tests.test_guest_gate import auth, env, fake, gg, load  # noqa: F401  (shared fixtures)
from tests.test_guest_signup_flow import _key  # noqa: F401

JSON = {"Accept": "application/json"}


# ───────────────────────── Resend API: sending ─────────────────────────
def test_acceptance_returns_the_email_id_and_is_logged_as_not_delivery_without_secrets(resend_env, caplog):
    configure(resend_env)
    with caplog.at_level(logging.DEBUG):
        email_id = ev.send_code("person@gmail.com", "482913")
    assert email_id == "em_test_0001" and "em_test_0001" in caplog.text and "not proof of delivery" in caplog.text
    assert "482913" not in caplog.text and PLACEHOLDER_KEY not in caplog.text and "person@gmail.com" not in caplog.text


def test_a_response_without_an_id_is_still_an_acceptance(resend_env):
    configure(resend_env)
    resend_env.resend_fake.result = {}
    assert ev.send_code("a@example.com", "123456") is None


@pytest.mark.parametrize("error, config_problem, kind", [
    (sdk_error(rx.InvalidApiKeyError, "API key is invalid", "invalid_api_key", 403), True, "auth_failed"),
    (sdk_error(rx.MissingApiKeyError, "Missing API key", "missing_api_key", 401), True, "auth_failed"),
    (sdk_error(rx.ValidationError, "The mail.flintel.test domain is not verified.", "validation_error", 403), True, "invalid_sender"),
    (sdk_error(rx.ValidationError, "Invalid `from` field.", "validation_error", 422), True, "invalid_sender"),
    (sdk_error(rx.ValidationError, "Invalid `to` field. person@example.com", "validation_error", 422), False, "invalid_recipient"),
    (sdk_error(rx.RateLimitError, "Too many requests", "rate_limit_exceeded", 429), False, "rate_limited"),
    (sdk_error(rx.RateLimitError, "Quota", "daily_quota_exceeded", 429), False, "rate_limited"),
    (sdk_error(rx.ApplicationError, "Internal", "application_error", 500), False, "provider_error"),
    (rx.ResendError(code=500, error_type="HttpClientError", message="Read timed out", suggested_action=""), False, "network_error"),
    (rx.ResendError(code=400, error_type="something_new", message="odd", suggested_action=""), False, "rejected_request"),
])
def test_every_api_failure_is_classified_logged_safely_and_never_retried(resend_env, caplog, error, config_problem, kind):
    configure(resend_env)
    resend_env.resend_fake.result = error
    expected = ev.EmailNotConfigured if config_problem else ev.EmailSendFailed
    with caplog.at_level(logging.DEBUG), pytest.raises(expected) as info:
        ev.send_code("person@example.com", "735190")
    assert len(resend_env.resend_fake.calls) == 1                                   # no retry loop
    assert f"kind={kind}" in caplog.text and "NOT accepted" in caplog.text
    assert "735190" not in caplog.text and PLACEHOLDER_KEY not in caplog.text and "person@example.com" not in caplog.text
    if not config_problem:
        assert str(info.value) == kind


@pytest.mark.parametrize("error, status, reason", [
    (sdk_error(rx.InvalidApiKeyError, "bad key", "invalid_api_key", 403), 503, "unavailable"),
    (sdk_error(rx.ValidationError, "domain is not verified", "validation_error", 403), 503, "unavailable"),
    (sdk_error(rx.ValidationError, "Invalid `to` field.", "validation_error", 422), 502, "send_failed"),
    (sdk_error(rx.RateLimitError, "slow down", "rate_limit_exceeded", 429), 502, "send_failed"),
    (rx.ResendError(code=500, error_type="HttpClientError", message="timeout", suggested_action=""), 502, "send_failed"),
    (sdk_error(rx.ApplicationError, "boom", "application_error", 500), 502, "send_failed"),
])
def test_at_the_route_a_failed_send_is_never_reported_as_sent_and_leaves_no_code_behind(auth, resend_env, error, status, reason):
    configure(resend_env)
    resend_env.resend_fake.result = error
    coll = fresh_codes_collection()
    r = auth["client"].post("/signup/code", data={"email": "fail@example.com"}, headers=JSON)
    assert r.status_code == status and r.json()["ok"] is False and r.json()["reason"] == reason
    assert coll.count_documents({"_id": "fail@example.com"}) == 0 and auth["users"].count_documents({}) == 0
    assert len(resend_env.resend_fake.calls) == 1


def test_a_missing_api_key_makes_signup_unavailable_and_names_only_the_variable(auth, resend_env, caplog):
    resend_env.setenv("EMAIL_FROM", "no-reply@mail.flintel.test")
    fresh_codes_collection()
    with caplog.at_level(logging.ERROR):
        r = auth["client"].post("/signup/code", data={"email": "a@example.com"}, headers=JSON)
    assert r.status_code == 503 and r.json()["reason"] == "unavailable" and "RESEND_API_KEY" in caplog.text
    assert resend_env.resend_fake.calls == []


def test_config_problems_lists_names_never_values():
    assert ed.config_problems({}) == ["RESEND_API_KEY", "EMAIL_FROM", "RESEND_WEBHOOK_SECRET"]
    assert ed.config_problems({"RESEND_API_KEY": "re_x", "EMAIL_FROM": "a@b.c", "RESEND_WEBHOOK_SECRET": "whsec_x"}) == []
    assert ed.config_problems({"RESEND_API_KEY": "   ", "EMAIL_FROM": "a@b.c"}) == ["RESEND_API_KEY", "RESEND_WEBHOOK_SECRET"]


# ───────────────────────── accepted emails are recorded ─────────────────────────
@pytest.fixture
def deliveries(monkeypatch):
    coll = mongomock.MongoClient()["deliveries"]["d"]
    monkeypatch.setattr(sys.modules["database"], "email_deliveries_collection", coll, raising=False)
    return coll


def test_an_accepted_code_email_is_recorded_by_its_resend_id_with_a_hashed_recipient_and_no_code(auth, resend_env, deliveries):
    configure(resend_env)
    resend_env.resend_fake.result = {"id": "em_rec_1"}
    fresh_codes_collection()
    r = auth["client"].post("/signup/code", data={"email": "Recorded@Example.com"}, headers=JSON)
    assert r.status_code == 200
    doc = deliveries.find_one({"_id": "em_rec_1"})
    assert doc["state"] == "accepted" and doc["purpose"] == "signup_code" and doc["to_domain"] == "example.com"
    code = [c for c in resend_env.resend_fake.calls][0]["params"]["subject"].rsplit(" ", 1)[-1]
    assert "recorded@example.com" not in json.dumps(doc, default=str).lower() and code not in json.dumps(doc, default=str)
    assert doc["expires_at"] > datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1) or doc["expires_at"]


def test_a_recording_failure_never_breaks_signup(auth, resend_env, monkeypatch):
    configure(resend_env)
    monkeypatch.setattr(sys.modules["database"], "email_deliveries_collection", None, raising=False)
    fresh_codes_collection()
    r = auth["client"].post("/signup/code", data={"email": "ok@example.com"}, headers=JSON)
    assert r.status_code == 200 and r.json()["ok"] is True


# ───────────────────────── webhook endpoint ─────────────────────────
@pytest.fixture
def hook(auth, deliveries, monkeypatch):
    secret = "whsec_" + base64.b64encode(os.urandom(24)).decode()               # throwaway, generated per test
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", secret)
    store = ed.DeliveryStore(deliveries)
    store.record_accepted("em_1", "user@example.com", "signup_code")

    class H:
        pass
    h = H()
    h.coll, h.secret, h.client, h.store, h.n = deliveries, secret, auth["client"], store, 0

    def sign(body, svix_id, ts, key=None):
        raw = base64.b64decode((key or secret)[6:])
        return "v1," + base64.b64encode(hmac.new(raw, f"{svix_id}.{ts}.{body}".encode(), hashlib.sha256).digest()).decode()

    def post(etype="email.delivered", email_id="em_1", to=("user@example.com",), svix_id=None, created=None, data=None, body=None,
             ts=None, key=None, headers=None, signed=True):
        h.n += 1
        svix_id = svix_id or f"msg_{h.n}"
        ts = str(int(time.time()) if ts is None else ts)
        if body is None:
            d = {"email_id": email_id, "to": list(to), "subject": "Your Flintel verification code: 123456"}
            d.update(data or {})
            body = json.dumps({"type": etype, "created_at": created or datetime.now(timezone.utc).isoformat(), "data": d})
        hdr = {"content-type": "application/json"}
        if signed:
            hdr.update({"svix-id": svix_id, "svix-timestamp": ts, "svix-signature": sign(body, svix_id, ts, key)})
        hdr.update(headers or {})
        return h.client.post("/webhooks/resend", content=body, headers=hdr)

    h.post, h.sign = post, sign
    h.state = lambda eid="em_1": h.coll.find_one({"_id": eid})["state"]
    return h


def test_a_validly_signed_delivered_event_updates_the_state(hook):
    r = hook.post("email.delivered")
    assert r.status_code == 200 and r.json() == {"status": "applied"} and hook.state() == "delivered"


def test_an_invalid_signature_is_rejected_and_changes_nothing(hook):
    r = hook.post("email.delivered", key="whsec_" + base64.b64encode(os.urandom(24)).decode())
    assert r.status_code == 401 and hook.state() == "accepted"


def test_a_missing_signature_is_rejected_and_changes_nothing(hook):
    assert hook.post("email.delivered", signed=False).status_code == 401 and hook.state() == "accepted"


def test_a_tampered_body_is_rejected(hook):
    body = json.dumps({"type": "email.delivered", "created_at": datetime.now(timezone.utc).isoformat(), "data": {"email_id": "em_1"}})
    ts = str(int(time.time()))
    sig = hook.sign(body, "msg_x", ts)
    r = hook.client.post("/webhooks/resend", content=body.replace("delivered", "bounced"), headers={"svix-id": "msg_x", "svix-timestamp": ts, "svix-signature": sig})
    assert r.status_code == 401 and hook.state() == "accepted"


def test_a_stale_signed_request_is_rejected_as_a_replay(hook):
    assert hook.post("email.delivered", ts=int(time.time()) - 3600).status_code == 401 and hook.state() == "accepted"


def test_the_endpoint_refuses_everything_when_no_signing_secret_is_configured(hook, monkeypatch):
    monkeypatch.delenv("RESEND_WEBHOOK_SECRET")
    r = hook.post("email.delivered")
    assert r.status_code == 503 and r.json()["error"] == "webhook_not_configured" and hook.state() == "accepted"


def test_a_duplicate_delivery_of_the_same_event_applies_once(hook):
    assert hook.post("email.delivered", svix_id="msg_dup").json()["status"] == "applied"
    assert hook.post("email.delivered", svix_id="msg_dup").json()["status"] == "duplicate"
    doc = hook.coll.find_one({"_id": "em_1"})
    assert doc["state"] == "delivered" and doc["event_ids"] == ["msg_dup"]


@pytest.mark.parametrize("etype, state", [("email.sent", "sent"), ("email.delivery_delayed", "delayed"), ("email.delivered", "delivered"),
                                          ("email.bounced", "bounced"), ("email.complained", "complained"), ("email.failed", "failed"),
                                          ("email.suppressed", "suppressed")])
def test_each_supported_event_maps_to_its_state(hook, etype, state):
    assert hook.post(etype).status_code == 200 and hook.state() == state


def test_a_bounce_is_never_recorded_as_delivered_and_keeps_only_safe_fields(hook):
    r = hook.post("email.bounced", data={"bounce": {"type": "Permanent", "subType": "Suppressed", "message": "550 user@example.com secret text"}})
    doc = hook.coll.find_one({"_id": "em_1"})
    assert r.status_code == 200 and doc["state"] == "bounced" and doc["bounce_type"] == "Permanent" and doc["bounce_subtype"] == "Suppressed"
    assert "secret text" not in json.dumps(doc, default=str) and "123456" not in json.dumps(doc, default=str)


def test_a_late_delivered_event_cannot_undo_a_bounce(hook):
    hook.post("email.bounced")
    assert hook.post("email.delivered").json()["status"] == "stale" and hook.state() == "bounced"


def test_a_complaint_after_delivery_is_recorded_and_a_later_delivered_cannot_undo_it(hook):
    hook.post("email.delivered")
    assert hook.post("email.complained").json()["status"] == "applied" and hook.state() == "complained"
    assert hook.post("email.delivered", svix_id="again").json()["status"] == "stale" and hook.state() == "complained"


def test_a_bounce_that_follows_a_delivery_is_recorded(hook):
    hook.post("email.delivered")
    assert hook.post("email.bounced").json()["status"] == "applied" and hook.state() == "bounced"


def test_out_of_order_non_terminal_events_never_downgrade(hook):
    hook.post("email.delivery_delayed")
    assert hook.post("email.sent").json()["status"] == "stale" and hook.state() == "delayed"
    hook.post("email.delivered")
    assert hook.post("email.delivery_delayed", svix_id="late").json()["status"] == "stale" and hook.state() == "delivered"


@pytest.mark.parametrize("etype", ["email.opened", "email.clicked", "email.received", "suppression.added", "domain.updated", "totally.unknown"])
def test_unknown_or_untracked_event_types_are_acknowledged_and_ignored(hook, etype):
    r = hook.post(etype)
    assert r.status_code == 200 and r.json() == {"status": "ignored_event_type"} and hook.state() == "accepted"


def test_an_event_for_an_old_unknown_email_id_is_acknowledged_without_creating_anything(hook):
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = hook.post("email.delivered", email_id="em_not_ours", created=old)
    assert r.status_code == 200 and r.json() == {"status": "unknown_email"} and hook.coll.count_documents({}) == 1


def test_a_brand_new_event_for_an_unknown_id_asks_for_redelivery_in_case_it_beat_our_own_write(hook):
    r = hook.post("email.sent", email_id="em_not_yet_stored")
    assert r.status_code == 409 and r.json()["error"] == "retry_later" and hook.coll.count_documents({}) == 1
    hook.store.record_accepted("em_not_yet_stored", "late@example.com", "signup_code")                 # our write lands; the retry now matches
    assert hook.post("email.sent", email_id="em_not_yet_stored", to=("late@example.com",), svix_id="msg_retry").json()["status"] == "applied"


def test_an_event_whose_recipient_does_not_match_the_stored_record_is_ignored(hook):
    r = hook.post("email.delivered", to=("someone.else@example.com",))
    assert r.status_code == 200 and r.json()["status"] == "recipient_mismatch" and hook.state() == "accepted"


def test_the_payload_address_is_never_trusted_as_an_identity(hook):
    hook.store.record_accepted("em_2", "other@example.com", "signup_code")
    hook.post("email.bounced", email_id="em_1", to=("other@example.com",))                           # em_1 belongs to user@example.com
    assert hook.state("em_1") == "accepted" and hook.state("em_2") == "accepted"


@pytest.mark.parametrize("body", ["not json at all", "[1, 2, 3]", "{}", '{"type": "email.delivered"}', '{"type": "email.delivered", "data": []}',
                                  '{"type": 5, "data": {}}', '{"type": "email.delivered", "data": {}}', '{"type": "email.delivered", "data": {"email_id": 7}}',
                                  '{"type": "email.delivered", "data": {"email_id": ""}}'])
def test_malformed_but_correctly_signed_payloads_are_rejected_without_a_state_change(hook, body):
    r = hook.post(body=body)
    assert r.status_code == 400 and r.json()["error"] == "malformed_payload" and hook.state() == "accepted"


def test_an_oversized_body_is_refused(hook):
    r = hook.client.post("/webhooks/resend", content="x" * (ed.MAX_BODY_BYTES + 1), headers={"svix-id": "a", "svix-timestamp": "1", "svix-signature": "v1,x"})
    assert r.status_code == 413


def test_processing_a_webhook_logs_neither_the_secret_nor_the_code_nor_the_address(hook, caplog):
    with caplog.at_level(logging.DEBUG):
        hook.post("email.bounced", data={"bounce": {"type": "Permanent", "subType": "General", "message": "user@example.com"}})
        hook.post("email.delivered", key="whsec_" + base64.b64encode(os.urandom(24)).decode(), svix_id="bad")
    assert hook.secret not in caplog.text and "123456" not in caplog.text and "user@example.com" not in caplog.text
    assert "em_1" in caplog.text


def test_the_webhook_never_resends_and_never_touches_codes_or_users(hook, resend_env, auth):
    codes = fresh_codes_collection()
    auth["users"].insert_one({"email": "keep@example.com"})
    hook.post("email.bounced")
    hook.post("email.complained", svix_id="c1")
    assert resend_env.resend_fake.calls == [] and codes.count_documents({}) == 0 and auth["users"].count_documents({}) == 1


def test_the_webhook_needs_no_session_and_passes_the_origin_guard_without_an_origin_header(hook):
    assert hook.post("email.sent").status_code == 200
