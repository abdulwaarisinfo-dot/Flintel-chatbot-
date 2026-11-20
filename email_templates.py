"""
email_templates.py - how Flintel's sign-up emails LOOK. Presentation only: no sending, no database, no logging, no secrets.

    render_verification_email(code, expiry_seconds, site_url) -> (html, text)     the 6-digit sign-up code
    render_exists_notice(site_url)                            -> (html, text)     "you already have an account" (no code)

Both use one shell (header, content, footer) so the two emails look like the same product. Every dynamic value is HTML-escaped.

Built for the mail clients that matter, not for browsers:
  * Gmail strips or ignores much of <style>, so EVERYTHING that matters is an inline style; the one <style> block only tightens padding on
    phones (if a client drops it, the fluid layout still works).
  * Outlook for Windows renders with Word's engine: table layout only (role=presentation), no flex/grid/background images, a fixed-width
    wrapper in MSO conditional comments (it ignores max-width), mso-line-height-rule, a 96 dpi setting. It ignores border-radius, so the
    rounded panels and the circular badge degrade to square ones - by design.
  * Web fonts do not load in mail clients: a system font stack is used. No JavaScript, no external CSS, no remote fonts.
  * The logo is a light mark on a transparent PNG (made for the dark app), so it sits on a blue tile (bgcolor works everywhere). It is
    only included when site_url is an absolute https URL; with images blocked the blue tile and the "Flintel" wordmark text remain.
Whether it renders correctly in a real Gmail / Outlook is NOT something this module can prove: see tests/test_email_template.py.
"""
from html import escape
from urllib.parse import urlparse

SUPPORT_EMAIL = "support@getflintel.com"
LOGO_PATH = "/favicon-192x192.png"          # served by the app's existing icon route (routes.py); no new asset or route

# Palette (from the app UI). TEXT_COLORS are used as text on white or on PANEL; tests assert WCAG AA contrast (>= 4.5:1) for each.
BRAND_BLUE = "#2563eb"
INK = "#191817"
BODY = "#3d3d3b"
MUTED = "#6b6a68"
PAGE = "#ffffff"
PANEL = "#f4f4f3"
HAIRLINE = "#e4e4e2"
TEXT_COLORS = (INK, BODY, MUTED, BRAND_BLUE)

SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "'SF Mono',SFMono-Regular,Menlo,Consolas,'Courier New',monospace"


def expiry_phrase(seconds):
    """'10 minutes' / '1 minute' / '90 seconds' - always derived from the real expiry, never hard-coded."""
    seconds = int(seconds)
    if seconds % 60 == 0 and seconds >= 60:
        n = seconds // 60
        return f"{n} minute" + ("" if n == 1 else "s")
    return f"{seconds} second" + ("" if seconds == 1 else "s")


def _logo_url(site_url):
    """Absolute https URL of the logo, or None (then the email is text-only in its header). Rejects anything that is not a plain https origin."""
    if not isinstance(site_url, str):
        return None
    try:
        parts = urlparse(site_url.strip())
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or any(c in parts.netloc for c in " \"'<>"):
        return None
    return f"https://{parts.netloc}{LOGO_PATH}"


def _header(site_url):
    logo = _logo_url(site_url)
    badge = ""
    if logo:
        badge = (f'<td width="36" height="36" bgcolor="{BRAND_BLUE}" style="width:36px;height:36px;background-color:{BRAND_BLUE};border-radius:18px;">'
                 f'<img src="{escape(logo, quote=True)}" width="36" height="36" alt="" '
                 'style="display:block;width:36px;height:36px;border:0;outline:none;text-decoration:none;border-radius:18px;"></td>'
                 '<td width="10" style="width:10px;font-size:0;line-height:0;">&nbsp;</td>')
    return ('<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>' + badge +
            f'<td valign="middle" style="font-family:{SANS};font-size:20px;line-height:36px;font-weight:600;letter-spacing:-0.2px;color:{INK};mso-line-height-rule:exactly;">Flintel</td>'
            '</tr></table>')


def _footer(reason):
    return (f'<tr><td class="px" style="padding:24px 32px 32px 32px;border-top:1px solid {HAIRLINE};font-family:{SANS};font-size:12px;line-height:18px;color:{MUTED};">'
            f'<p style="margin:0 0 8px 0;">Questions? Contact <a href="mailto:{SUPPORT_EMAIL}" style="color:{BRAND_BLUE};text-decoration:underline;">{SUPPORT_EMAIL}</a>.</p>'
            f'<p style="margin:0;">{escape(reason)}</p></td></tr>')


def _security_block(lines):
    inner = "".join(f'<p style="margin:{"0" if i == len(lines) - 1 else "0 0 8px 0"};">{line}</p>' for i, line in enumerate(lines))
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td style="padding:16px 0 0 0;font-family:{SANS};font-size:13px;line-height:20px;color:{BODY};">{inner}</td></tr></table>')


def _shell(title, preheader, content_rows, footer_reason, site_url):
    pad = "&#847;&zwnj;&nbsp;" * 60                      # keeps the body text out of the inbox preview line
    return (
        '<!DOCTYPE html>'
        '<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">'
        '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge"><meta name="x-apple-disable-message-reformatting">'
        '<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">'
        f'<title>{escape(title)}</title>'
        '<!--[if mso]><noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript><![endif]-->'
        '<style>@media only screen and (max-width:480px){.px{padding-left:20px!important;padding-right:20px!important}'
        '.code{font-size:30px!important;letter-spacing:6px!important;padding-left:6px!important}}</style></head>'
        f'<body style="margin:0;padding:0;background-color:{PAGE};-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">'
        f'<div style="display:none;max-height:0;max-width:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:{PAGE};opacity:0;">{escape(preheader)}{pad}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{PAGE}" style="background-color:{PAGE};"><tr>'
        '<td align="center" style="padding:32px 16px;">'
        '<!--[if mso]><table role="presentation" width="480" align="center" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:480px;background-color:{PAGE};">'
        f'<tr><td class="px" style="padding:8px 32px 24px 32px;">{_header(site_url)}</td></tr>'
        + content_rows + _footer(footer_reason) +
        '</table>'
        '<!--[if mso]></td></tr></table><![endif]-->'
        '</td></tr></table></body></html>'
    )


def _row(inner):
    return f'<tr><td class="px" style="padding:0 32px 24px 32px;">{inner}</td></tr>'


def _heading(text):
    return (f'<h1 style="margin:0 0 12px 0;font-family:{SANS};font-size:24px;line-height:30px;font-weight:600;color:{INK};'
            f'mso-line-height-rule:exactly;">{escape(text)}</h1>')


def _paragraph(text, last=False):
    return (f'<p style="margin:{"0" if last else "0 0 16px 0"};font-family:{SANS};font-size:15px;line-height:24px;color:{BODY};'
            f'mso-line-height-rule:exactly;">{escape(text)}</p>')


_REASON_CODE = "You received this email because this address was entered to sign up for Flintel. If that wasn't you, no action is needed."
_REASON_NOTICE = "You received this email because this address was entered to sign up for Flintel."


def render_verification_email(code, expiry_seconds, site_url=None):
    """-> (html, text) for the sign-up verification email. `code` is shown once, large and centred; it is never part of the subject or preheader."""
    code = str(code)
    expiry = expiry_phrase(expiry_seconds)
    panel = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
             f'<td align="center" bgcolor="{PANEL}" style="background-color:{PANEL};border:1px solid {HAIRLINE};border-radius:8px;padding:20px 12px;">'
             f'<div class="code" style="font-family:{MONO};font-size:36px;line-height:44px;font-weight:700;letter-spacing:8px;padding-left:8px;color:{INK};'
             f'mso-line-height-rule:exactly;">{escape(code)}</div></td></tr></table>'
             f'<p style="margin:12px 0 0 0;font-family:{SANS};font-size:13px;line-height:20px;color:{MUTED};text-align:center;">'
             f'This code expires in {escape(expiry)} and can be used once.</p>')
    security = _security_block([
        f'<strong style="color:{INK};">Didn\u2019t request this?</strong> You can safely ignore this email. No account is created without this code.',
        'Keep this code private. Flintel will never ask you to share it by email, chat or phone.',
    ])
    rows = (_row(_heading("Verify your email") + _paragraph("Enter this code in the sign-up window to finish creating your Flintel account.", last=True))
            + _row(panel + security))
    html = _shell("Verify your email", f"Enter this code to finish creating your Flintel account. It expires in {expiry}.", rows, _REASON_CODE, site_url)
    text = (
        "Flintel\n\n"
        "Verify your email\n\n"
        "Enter this code in the sign-up window to finish creating your Flintel account:\n\n"
        f"    {code}\n\n"
        f"This code expires in {expiry} and can be used once.\n\n"
        "Didn't request this? You can safely ignore this email. No account is created without this code.\n"
        "Keep this code private. Flintel will never ask you to share it by email, chat or phone.\n\n"
        "--\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n"
        f"{_REASON_CODE}\n"
    )
    return html, text


def render_exists_notice(site_url=None):
    """-> (html, text) for the email sent INSTEAD of a code when the address already has an account. Contains no code."""
    security = _security_block([
        f'<strong style="color:{INK};">Wasn\u2019t you?</strong> You can safely ignore this email. No new account was created and nothing has changed.',
    ])
    rows = (_row(_heading("You already have a Flintel account")
                 + _paragraph("Someone (hopefully you) tried to create a Flintel account with this email address, but one already exists.")
                 + _paragraph("Sign in with the method you used before: Google, or your email and password.", last=True))
            + _row(security))
    html = _shell("You already have a Flintel account", "Sign in with the method you used before. No new account was created.", rows, _REASON_NOTICE, site_url)
    text = (
        "Flintel\n\n"
        "You already have a Flintel account\n\n"
        "Someone (hopefully you) tried to create a Flintel account with this email address, but one already exists.\n\n"
        "Sign in with the method you used before: Google, or your email and password.\n\n"
        "Wasn't you? You can safely ignore this email. No new account was created and nothing has changed.\n\n"
        "--\n"
        f"Questions? Contact {SUPPORT_EMAIL}.\n"
        f"{_REASON_NOTICE}\n"
    )
    return html, text
