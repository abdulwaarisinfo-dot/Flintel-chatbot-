"""Test helper: drive the REAL three-step e-mail sign-up (code -> verify -> password) against a TestClient.

The only thing stubbed is e-mail delivery (email_verification.send_code), which captures the code the server
generated; everything else (code hashing, expiry, attempts, the signed "verified" session marker, account creation) runs
for real on a mongomock collection."""
import sys
from unittest import mock

import mongomock

import email_verification as ev


def fresh_codes_collection():
    coll = mongomock.MongoClient()["otp"]["codes"]
    sys.modules["database"].email_codes_collection = coll
    return coll


def request_code(client, email, headers=None):
    """POST /signup/code with delivery captured. -> (response, code or None)."""
    sent = []
    with mock.patch.object(ev, "send_code", side_effect=lambda to, code: sent.append((to, code))):
        resp = client.post("/signup/code", data={"email": email}, headers=headers or {"Accept": "application/json"})
    return resp, (sent[0][1] if sent else None)


def signup_with_code(client, email, password="correct-horse-1", confirm=None, resume="1", extra=None, headers=None, fresh=True):
    """Verify `email` with a real emailed code, then POST /signup. -> the /signup response."""
    hdr = {"Accept": "application/json"} if headers is None else headers      # headers for the final /signup post
    if fresh:
        fresh_codes_collection()
    resp, code = request_code(client, email)
    assert resp.status_code == 200 and code, resp.text
    ok = client.post("/signup/verify", data={"email": email, "code": code}, headers={"Accept": "application/json"})
    assert ok.status_code == 200 and ok.json()["ok"] is True, ok.text
    data = {"password": password, "confirm_password": password if confirm is None else confirm, "resume": resume}
    data.update(extra or {})
    return client.post("/signup", data=data, headers=hdr, follow_redirects=False)
