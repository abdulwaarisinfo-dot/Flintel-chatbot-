"""Test helpers for the Resend email path. NO real Resend call is ever made: resend.Emails.send is replaced by FakeResend, and the
API key used here is an obvious placeholder."""
import pytest
import resend
import resend.exceptions as rx

import email_verification as ev

PLACEHOLDER_KEY = "re_placeholder_not_a_real_key"


class FakeResend:
    """Stands in for resend.Emails.send. `result` is returned, or raised if it is an exception."""
    def __init__(self):
        self.calls, self.result = [], {"id": "em_test_0001"}

    def __call__(self, params, options=None):
        self.calls.append({"params": params, "api_key": resend.api_key, "timeout": getattr(resend.default_http_client, "_timeout", None)})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def sdk_error(cls, message, error_type, code):
    return cls(message=message, error_type=error_type, code=code)


@pytest.fixture
def resend_env(monkeypatch):
    for k in ("EMAIL_BACKEND", "SMTP_HOST", "RESEND_API_KEY", "EMAIL_FROM", "RESEND_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeResend()
    monkeypatch.setattr(resend.Emails, "send", staticmethod(fake))
    monkeypatch.setattr(resend, "api_key", None, raising=False)
    monkeypatch.resend_fake = fake
    return monkeypatch


def configure(env, sender="Flintel <no-reply@mail.flintel.test>"):
    env.setenv("RESEND_API_KEY", PLACEHOLDER_KEY)
    env.setenv("EMAIL_FROM", sender)
