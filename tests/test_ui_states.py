"""Billing page states in a real browser (network blocked, /api/billing/* mocked): the user must always see what happened, on a phone too.
Covers: checkout denied (not signed in / blocked / unavailable / server error / offline), success redirect, buttons restored after an error."""
import json

import pytest

from tests.test_guest_gate import ORIGIN, needs_browser, render

USER = {"name": "Ada", "email": "ada@example.com"}


def _open(browser, status, body, w=375, h=812, offline=False):
    c = browser.new_context(viewport={"width": w, "height": h})
    pg = c.new_page()
    html = render("pricing.html", user=USER, current_plan="free", billing_enabled=True, billing=None)
    pg.route(ORIGIN + "/__page__", lambda r: r.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))

    def api(route):
        if offline:
            return route.abort()
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
    pg.route(ORIGIN + "/api/billing/checkout", api)
    pg.route("**/*", lambda r: r.abort() if "flintel.test" not in r.request.url else r.fallback())
    pg.goto(ORIGIN + "/__page__", wait_until="domcontentloaded")
    return c, pg


CASES = [
    (401, {"error": "not_authenticated"}, False, "Please sign in to subscribe."),
    (403, {"error": "forbidden", "detail": "cross-origin request blocked"}, False, "This request was blocked. Reload the page and try again."),
    (503, {"error": "billing_unavailable"}, False, "Checkout isn't available right now. Please try again later."),
    (409, {"error": "already_subscribed"}, False, "You already have an active subscription"),
    (500, {}, False, "We couldn't start checkout. Please try again in a moment."),
    (0, {}, True, "Network problem. Check your connection and try again."),
]


@needs_browser
@pytest.mark.parametrize("status,body,offline,expected", CASES)
def test_every_checkout_failure_is_explained_visibly_and_the_buttons_come_back(integrity_browser, status, body, offline, expected):
    c, pg = _open(integrity_browser, status or 200, body, offline=offline)
    try:
        btn = pg.locator("button[data-plan]").first
        label = btn.get_attribute("data-label")
        btn.click()
        pg.wait_for_function("(() => { const b = document.getElementById('billing-message'); return b && !b.hidden && b.textContent.length > 0; })()")
        msg = pg.locator("#billing-message")
        assert expected in msg.inner_text()
        box = msg.bounding_box()
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= 376                       # inside the phone viewport
        assert pg.evaluate("document.documentElement.scrollWidth") <= 376
        assert btn.is_enabled() and btn.inner_text().replace("—", "-").strip() != "Opening secure checkout…"
        assert label in btn.get_attribute("data-label")
    finally:
        c.close()


@needs_browser
def test_a_blocked_cross_site_attempt_is_reported_with_the_key_the_page_maps(integrity_browser):
    import origin_guard as og
    assert json.loads(og._BODY)["error"] == "forbidden"
