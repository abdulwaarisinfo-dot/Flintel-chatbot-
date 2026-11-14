"""The chat flow's website fetch (logics.fetch_website_text / _fetch_raw_html) now goes through safe_fetch: internal targets are refused before any
connection, and a normal page still returns the same text as before."""
import pytest

import safe_fetch as sf
from tests.test_mapreduce_identity import env, lg  # noqa: F401

INTERNAL = ["http://127.0.0.1/", "http://localhost/admin", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/", "http://[::1]/",
            "http://2130706433/", "file:///etc/passwd", "ftp://example.com/"]


@pytest.mark.parametrize("url", INTERNAL)
def test_the_chat_fetchers_refuse_internal_and_non_web_targets_without_connecting(lg, monkeypatch, url):
    calls = []
    monkeypatch.setattr(sf, "_default_transport", lambda *a, **k: calls.append(a))
    for fn in (lg.fetch_website_text, lg._fetch_raw_html):
        with pytest.raises(sf.UnsafeUrl):
            fn(url)
    assert calls == []


def test_a_public_page_is_still_fetched_reduced_to_text_and_truncated(lg, monkeypatch):
    page = "<html><head><title>Acme</title><style>x{}</style></head><body><script>1</script><h1>Hello</h1> <p>World</p></body></html>"
    monkeypatch.setattr(sf, "fetch_public_page", lambda url, **kw: {"text": page, "final_url": url, "status": 200, "truncated": False, "redirects": 0})
    text = lg.fetch_website_text("https://acme.example")
    assert "Hello" in text and "World" in text and "<h1>" not in text and "x{}" not in text and len(text) <= lg.WEBSITE_FETCH_MAX_CHARS
    assert lg._fetch_raw_html("https://acme.example") == page


def test_a_failed_fetch_still_raises_like_before(lg, monkeypatch):
    def fail(url, **kw):
        raise sf.FetchFailed("The website could not be reached.")
    monkeypatch.setattr(sf, "fetch_public_page", fail)
    with pytest.raises(Exception):
        lg.fetch_website_text("https://acme.example")
