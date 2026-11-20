"""Tests for the sign-up email design (email_templates.py) and how email_verification builds the Resend payload from it.

Real here: the HTML and plain-text output, the Resend request parameters, the (replaced) Resend send path and its logging, and - for the layout
test - the HTML rendered in headless Chromium at phone and desktop widths with ALL network blocked (so the images-blocked fallback is what is checked).
NOT verifiable here: how Gmail (web / Android / iOS) or Outlook (Windows / Mac / web) actually render it. Those clients are not available in this
environment, so the Gmail/Outlook compatibility tests check the known-safe construction rules (inline styles, tables, no unsupported CSS, no scripts,
no remote resources apart from the logo), not the pixels. Send real test emails to Gmail and Outlook accounts (or use Litmus / Email on Acid) before
relying on it.
"""
import logging
import re

import pytest

import email_templates as et
import email_verification as ev
from tests.resend_helpers import PLACEHOLDER_KEY, configure, resend_env  # noqa: F401

SITE = "https://getflintel.example"
CODE = "482913"
LOGO = f"{SITE}/favicon-192x192.png"


def code_email(code=CODE, seconds=600, site=SITE):
    return et.render_verification_email(code, seconds, site)


def notice(site=SITE):
    return et.render_exists_notice(site)


BOTH = pytest.mark.parametrize("render", [code_email, notice], ids=["code_email", "exists_notice"])


def _hidden_preheader(html):
    return re.search(r'<div style="display:none;[^"]*">(.*?)</div>', html, re.S).group(1)


def _luminance(hex_color):
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ───────────────────────── content: the verification email ─────────────────────────
def test_the_code_is_shown_once_large_and_centred_in_one_unbroken_element():
    html, text = code_email()
    assert html.count(CODE) == 1 and text.count(CODE) == 1
    m = re.search(r'<td align="center"[^>]*>\s*<div class="code" style="([^"]*)">' + CODE + r'</div>', html)
    assert m, "the code must sit alone, in a centred cell"
    style = m.group(1)
    assert int(re.search(r"font-size:(\d+)px", style).group(1)) >= 32 and "font-weight:700" in style and "letter-spacing" in style
    assert re.search(r">" + CODE + "<", html) and " " not in CODE                     # contiguous digits: a double tap selects the whole code


def test_the_code_never_appears_in_the_subject_the_preheader_or_the_title():
    html, _ = code_email()
    assert CODE not in _hidden_preheader(html) and CODE not in re.search(r"<title>(.*?)</title>", html).group(1)
    params = ev.build_message("a@example.com", CODE, "Flintel <no-reply@mail.flintel.test>")
    assert params["subject"] == "Your Flintel verification code" and CODE not in params["subject"]


@pytest.mark.parametrize("seconds, phrase", [(600, "10 minutes"), (300, "5 minutes"), (60, "1 minute"), (90, "90 seconds"), (120, "2 minutes")])
def test_the_expiry_is_derived_from_the_real_ttl_in_html_preheader_and_text(seconds, phrase):
    html, text = code_email(seconds=seconds)
    assert f"expires in {phrase}" in html and f"expires in {phrase}" in text and phrase in _hidden_preheader(html)


def test_build_message_uses_the_configured_code_ttl(monkeypatch):
    monkeypatch.setattr(ev, "CODE_TTL_SECONDS", 300)
    p = ev.build_message("a@example.com", CODE, "x@y.z")
    assert "expires in 5 minutes" in p["html"] and "expires in 5 minutes" in p["text"]
    assert ev.CODE_TTL_SECONDS == 300 and "10 minutes" not in p["html"]


def test_the_default_expiry_still_matches_the_real_ten_minutes():
    p = ev.build_message("a@example.com", CODE, "x@y.z")
    assert ev.CODE_TTL_SECONDS == 600 and "10 minutes" in p["text"] and "10 minutes" in p["html"]


def test_the_security_disclaimer_is_in_both_versions():
    html, text = code_email()
    for fragment in ("You can safely ignore this email", "No account is created without this code",
                     "Keep this code private", "will never ask you to share it by email, chat or phone"):
        assert fragment in html and fragment in text


@BOTH
def test_the_support_contact_and_footer_are_in_both_versions(render):
    html, text = render()
    assert f'href="mailto:{et.SUPPORT_EMAIL}"' in html and "support@getflintel.com" in html and "support@getflintel.com" in text
    assert "You received this email because this address was entered to sign up for Flintel" in html
    assert "You received this email because this address was entered to sign up for Flintel" in text


@BOTH
def test_the_branding_is_in_both_versions(render):
    html, text = render()
    assert ">Flintel</td>" in html and text.startswith("Flintel\n")


# ───────────────────────── the "you already have an account" email ─────────────────────────
def test_the_exists_notice_contains_no_code_and_says_to_sign_in():
    html, text = notice()
    assert not re.search(r"(?<![#\w&])\d{6}(?![\w;])", text + re.sub(r"&#847;", "", html))
    assert "You already have a Flintel account" in html and "You already have a Flintel account" in text
    assert "Sign in with the method you used before" in html and "Sign in with the method you used before" in text
    assert "No new account was created and nothing has changed" in html and "No new account was created and nothing has changed" in text


def test_both_emails_use_the_same_design_shell():
    code_html, _ = code_email()
    notice_html, _ = notice()
    assert et._header(SITE) in code_html and et._header(SITE) in notice_html
    support = f'Questions? Contact <a href="mailto:{et.SUPPORT_EMAIL}"'
    assert support in code_html and support in notice_html
    for marker in ('<meta name="x-apple-disable-message-reformatting">', "<!--[if mso]>", 'bgcolor="#ffffff"', "max-width:480px"):
        assert marker in code_html and marker in notice_html


def test_the_exists_notice_payload_keeps_its_subject_tag_and_shape():
    p = ev.build_exists_notice("a@example.com", "Flintel <no-reply@mail.flintel.test>")
    assert set(p) == {"from", "to", "subject", "text", "html", "tags"}
    assert p["subject"] == "You already have a Flintel account" and p["tags"] == [{"name": "purpose", "value": "signup_exists_notice"}]
    assert p["to"] == ["a@example.com"] and p["from"] == "Flintel <no-reply@mail.flintel.test>"


# ───────────────────────── Gmail / Outlook construction rules ─────────────────────────
@BOTH
def test_the_document_has_the_structure_mail_clients_need(render):
    html, _ = render()
    assert html.startswith("<!DOCTYPE html>") and '<html lang="en"' in html and '<meta charset="utf-8">' in html
    assert 'name="viewport"' in html and 'name="x-apple-disable-message-reformatting"' in html
    assert "<!--[if mso]>" in html and "<![endif]-->" in html and "PixelsPerInch" in html          # Outlook: fixed-width wrapper + 96 dpi
    assert 'width="480"' in html and "mso-line-height-rule:exactly" in html


@BOTH
def test_layout_is_tables_only_and_every_table_is_presentational(render):
    html, _ = render()
    visible = re.sub(r"<!--\[if mso\]>.*?<!\[endif\]-->", "", html, flags=re.S)
    tables = re.findall(r"<table\b[^>]*>", visible)
    assert tables and all('role="presentation"' in t for t in tables)
    assert all(('cellpadding="0"' in t and 'cellspacing="0"' in t and 'border="0"' in t) for t in tables)


@BOTH
def test_no_css_or_markup_that_gmail_or_outlook_drop_or_break(render):
    html, _ = render()
    lowered = html.lower()
    for forbidden in ("<script", "<link", "@import", "url(", "display:flex", "display:grid", "position:", "float:", "<form", "<iframe", "<video", "<svg",
                      "javascript:", "background-image", "calc(", "var(--", "@font-face", "<object", "<embed", "onerror=", "onclick="):
        assert forbidden not in lowered, forbidden


@BOTH
def test_the_only_remote_resources_are_the_logo_and_the_support_mailto(render):
    html, _ = render()
    urls = re.findall(r'(?:src|href)="([^"]*)"', html)
    assert sorted(urls) == sorted([LOGO, f"mailto:{et.SUPPORT_EMAIL}"])


@BOTH
def test_styles_are_inline_and_the_style_block_is_only_a_phone_tweak(render):
    html, _ = render()
    for tag in re.findall(r"<(?:p|h1)\b[^>]*>", html):
        assert 'style="' in tag
    blocks = re.findall(r"<style>(.*?)</style>", html, re.S)
    assert len(blocks) == 1 and len(blocks[0]) < 600 and blocks[0].startswith("@media only screen and (max-width:480px)")


@BOTH
def test_the_message_is_far_below_gmails_102kb_clipping_limit(render):
    html, text = render()
    assert len(html.encode()) < 20_000 and len(text.encode()) < 2_000


@BOTH
def test_text_sizes_are_readable_and_colors_pass_wcag_aa(render):
    html, _ = render()
    visible = re.sub(r'<div style="display:none;[^"]*">.*?</div>', "", html, flags=re.S)
    sizes = [int(s) for s in re.findall(r"font-size:(\d+)px", re.sub(r"<style>.*?</style>", "", visible, flags=re.S))]
    assert sizes and min(sizes) >= 12
    used = set(re.findall(r"(?<![-\w])color:(#[0-9a-fA-F]{6})", re.sub(r"<style>.*?</style>", "", visible, flags=re.S)))
    assert used and used <= set(et.TEXT_COLORS)
    for color in et.TEXT_COLORS:
        assert _contrast(color, et.PAGE) >= 4.5, color
    for color in (et.INK, et.BODY, et.MUTED):                                  # the text colors that also sit on the grey code panel
        assert _contrast(color, et.PANEL) >= 4.5, color


def test_the_code_panel_and_the_wordmark_have_dark_text_on_a_light_ground():
    html, _ = code_email()
    assert f"background-color:{et.PANEL}" in html and f"color:{et.INK}" in html
    assert _contrast(et.INK, et.PANEL) >= 12


# ───────────────────────── logo badge ─────────────────────────
def test_the_logo_badge_is_the_existing_icon_on_a_blue_tile_with_safe_attributes():
    html, _ = code_email()
    imgs = re.findall(r"<img\b[^>]*>", html)
    assert len(imgs) == 1
    img = imgs[0]
    assert f'src="{LOGO}"' in img and 'alt=""' in img and 'width="36"' in img and 'height="36"' in img and "display:block" in img
    assert re.search(r'<td width="36" height="36" bgcolor="#2563eb"[^>]*>\s*<img', html)       # the tile stays blue if images are blocked


def test_the_logo_url_uses_only_the_origin_of_site_url():
    html, _ = code_email(site="https://getflintel.example/some/path/?q=1#x")
    assert f'src="{LOGO}"' in html and "some/path" not in html


@pytest.mark.parametrize("site", [None, "", "   ", "http://getflintel.example", "ftp://x.example", "https://", "javascript:alert(1)", "//x.example",
                                  "https://user:pw@getflintel.example", "https://a b.example", "not a url", 5, b"https://x.example", ["https://x.example"]])
def test_without_a_plain_https_site_url_there_is_no_image_and_the_wordmark_remains(site):
    for render in (lambda: et.render_verification_email(CODE, 600, site), lambda: et.render_exists_notice(site)):
        html, text = render()
        assert "<img" not in html and ">Flintel</td>" in html and text.startswith("Flintel")


@pytest.mark.parametrize("site", ['https://evil.example/"onerror="x', "https://evil.example/<b>x</b>", "https://evil.example/path'quote"])
def test_junk_after_the_origin_is_dropped_so_nothing_can_be_injected_through_the_url(site):
    html, _ = code_email(site=site)
    assert 'src="https://evil.example/favicon-192x192.png"' in html
    assert "onerror" not in html and "<b>" not in html and "path'quote" not in html and html.count("<img") == 1


def test_site_url_is_read_from_config_by_the_payload_builder(monkeypatch):
    import config
    monkeypatch.setattr(config, "SITE_URL", SITE, raising=False)
    assert f'src="{LOGO}"' in ev.build_message("a@example.com", CODE, "x@y.z")["html"]
    monkeypatch.setattr(config, "SITE_URL", "", raising=False)
    monkeypatch.delenv("SITE_URL", raising=False)
    assert "<img" not in ev.build_message("a@example.com", CODE, "x@y.z")["html"]


# ───────────────────────── escaping ─────────────────────────
def test_dynamic_values_are_html_escaped():
    evil = '<script>alert(1)</script>&"\''
    html, _ = code_email(code=evil)
    assert "<script" not in html and "&lt;script&gt;" in html and "&amp;" in html
    html2, _ = code_email(site='https://x.example"><script>alert(1)</script>')
    assert "<script" not in html2 and "<img" not in html2


# ───────────────────────── plain-text fallback ─────────────────────────
@BOTH
def test_the_plain_text_version_is_clean_readable_and_ascii(render):
    _, text = render()
    assert not re.search(r"</?[a-zA-Z]", text) and "&nbsp;" not in text and "&#" not in text
    assert text.isascii() and text.endswith("\n") and all(len(line) <= 130 for line in text.splitlines())
    assert text.splitlines()[0] == "Flintel" and "\n--\n" in text


def test_the_text_version_carries_the_same_facts_as_the_html():
    html, text = code_email()
    for fragment in ("Verify your email", CODE, "expires in 10 minutes and can be used once", "No account is created without this code",
                     "Keep this code private", "support@getflintel.com"):
        assert fragment in html and fragment in text, fragment


# ───────────────────────── the Resend payload and the send path ─────────────────────────
def test_the_payload_has_exactly_the_resend_fields_with_both_html_and_text():
    p = ev.build_message("a@example.com", CODE, "Flintel <no-reply@mail.flintel.test>")
    assert set(p) == {"from", "to", "subject", "text", "html", "tags"}
    assert p["to"] == ["a@example.com"] and p["from"] == "Flintel <no-reply@mail.flintel.test>"
    assert p["tags"] == [{"name": "purpose", "value": "signup_code"}] and p["html"].startswith("<!DOCTYPE html>") and CODE in p["text"]


def test_send_code_still_goes_through_resend_with_the_new_template_and_logs_no_secrets(resend_env, caplog):
    configure(resend_env)
    with caplog.at_level(logging.DEBUG):
        email_id = ev.send_code("person@example.com", CODE)
    call = resend_env.resend_fake.calls[0]
    assert email_id == "em_test_0001" and call["api_key"] == PLACEHOLDER_KEY and call["timeout"] == 10
    assert call["params"]["subject"] == "Your Flintel verification code" and CODE in call["params"]["text"] and CODE in call["params"]["html"]
    assert CODE not in caplog.text and PLACEHOLDER_KEY not in caplog.text and "<table" not in caplog.text and "person@example.com" not in caplog.text


def test_the_exists_notice_still_goes_through_resend_with_the_new_template(resend_env, caplog):
    configure(resend_env)
    with caplog.at_level(logging.DEBUG):
        assert ev.send_account_exists_notice("person@example.com") == "em_test_0001"
    p = resend_env.resend_fake.calls[0]["params"]
    assert p["subject"] == "You already have a Flintel account" and p["html"].startswith("<!DOCTYPE html>") and "already have" in p["text"]
    assert "person@example.com" not in caplog.text and PLACEHOLDER_KEY not in caplog.text


def test_rendering_logs_nothing(caplog):
    with caplog.at_level(logging.DEBUG):
        code_email()
        notice()
        ev.build_message("a@example.com", CODE, "x@y.z")
        ev.build_exists_notice("a@example.com", "x@y.z")
    assert caplog.text == ""


def test_the_console_dev_backend_is_unchanged_and_still_the_only_place_a_code_is_logged(resend_env, caplog):
    import config
    resend_env.setenv("EMAIL_BACKEND", "console")
    resend_env.setattr(config, "SITE_URL", "", raising=False)
    with caplog.at_level(logging.WARNING):
        assert ev.send_code("a@example.com", CODE) is None
    assert CODE in caplog.text and "DEV ONLY" in caplog.text and resend_env.resend_fake.calls == []


# ───────────────────────── real-browser layout (network blocked) ─────────────────────────
@pytest.fixture
def chromium(integrity_browser):
    """The repo's shared session Chromium (tests/conftest.py): it handles Playwright refusing to start inside another module's event loop, and
    skips (never fails) when no browser is available."""
    return integrity_browser


@pytest.mark.parametrize("width", [320, 375, 480, 640, 1024])
@BOTH
def test_layout_has_no_horizontal_overflow_at_phone_and_desktop_widths_with_images_blocked(chromium, render, width):
    html, _ = render()
    page = chromium.new_page(viewport={"width": width, "height": 800})
    try:
        page.route("**/*", lambda route: route.abort())                                  # all network blocked: the logo cannot load
        page.set_content(html)
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        assert page.evaluate("document.body.scrollWidth") <= width
        assert "Flintel" in page.inner_text("body")
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
    finally:
        page.close()


@pytest.mark.parametrize("width", [320, 375, 640])
def test_the_code_stays_on_one_line_inside_the_screen_and_selectable_as_one_token(chromium, width):
    html, _ = code_email()
    page = chromium.new_page(viewport={"width": width, "height": 800})
    try:
        page.route("**/*", lambda route: route.abort())
        page.set_content(html)
        box = page.locator(".code").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width and box["height"] < 60          # one line, never wrapped
        assert page.locator(".code").text_content() == CODE
        assert float(page.evaluate("parseFloat(getComputedStyle(document.querySelector('.code')).fontSize)")) >= 28
    finally:
        page.close()
