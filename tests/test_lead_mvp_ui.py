"""Lead pages in real Chromium (network blocked): desktop and phone widths, no horizontal overflow, no script errors, readable controls."""
import pytest

from tests.test_lead_mvp import FORM, GOOD, post, web, lg, env, Fake  # noqa: F401

PAGES = ("overview", "profile", "find", "scan", "my", "detail")


def _html(web):
    c, who, state, lr = web
    state["records"] = [GOOD, post(2, "Losing track of candidates in spreadsheets",
                                   "We are a recruiting agency and our spreadsheets keep breaking, we lose track of candidates. Any ideas for tracking candidates?", days=500),
                        post(3, "Need a CRM for a recruitment agency", "We are a recruitment agency and need a CRM to track candidates. Recommendations please?", author="")]
    out = {"empty_overview": c.get("/leads").text}
    c.post("/leads/profile", data=dict(FORM, notes="A" * 300))
    loc = c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"]
    lead_id = lr._leads().list_leads("owner@x.com")[0]["lead_id"]
    out.update(overview=c.get("/leads").text, profile=c.get("/leads/profile").text, find=c.get("/leads/find").text, scan=c.get(loc).text,
               my=c.get("/leads/my").text, detail=c.get(f"/leads/item/{lead_id}").text)
    lr.CONNECTOR_FACTORY = lambda sel: [Fake("reddit", "Reddit", [], status="failed")]
    out["scan_incomplete"] = c.get(c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"]).text
    return out


@pytest.mark.parametrize("w,h", [(375, 812), (768, 1024), (1280, 800)])
def test_every_lead_page_fits_the_viewport_without_errors(integrity_browser, web, w, h):
    pages = _html(web)
    ctx = integrity_browser.new_context(viewport={"width": w, "height": h})
    try:
        for name, html in pages.items():
            pg = ctx.new_page()
            errors = []
            pg.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            pg.route("**/*", lambda route: route.abort())                                   # network blocked
            pg.set_content(html, wait_until="domcontentloaded")
            assert pg.evaluate("document.documentElement.scrollWidth") <= w + 1, (name, w)
            assert errors == [], (name, errors)
            small = pg.evaluate("""[...document.querySelectorAll('button, a.btn, input[type=submit]')].filter(e => e.offsetParent && e.getBoundingClientRect().height < 34).length""")
            assert small == 0, (name, "controls shorter than 34px")
            assert pg.evaluate("[...document.querySelectorAll('input:not([type=hidden]), textarea, select')].every(e => (e.id && document.querySelector('label[for=' + e.id + ']')) || e.getAttribute('aria-label') || e.type == 'checkbox')"), name
            assert pg.locator("h1").count() == 1, name
            pg.close()
    finally:
        ctx.close()


def test_the_results_page_shows_lead_cards_not_a_table_and_labels_historical_leads(integrity_browser, web):
    pages = _html(web)
    ctx = integrity_browser.new_context(viewport={"width": 375, "height": 812})
    pg = ctx.new_page()
    pg.route("**/*", lambda route: route.abort())
    pg.set_content(pages["scan"], wait_until="domcontentloaded")
    assert pg.locator("table").count() == 0 and pg.locator("article.lead").count() == 3
    text = " ".join(pg.inner_text("main").split())
    assert "Historical" in text and "High potential" in text and "Author unknown" in text and "Sources searched" in text
    assert pg.locator("a:has-text('Open original')").count() == 3
    assert all(h.startswith("https://") for h in pg.eval_on_selector_all("a:has-text('Open original')", "els => els.map(e => e.href)"))
    ctx.close()


def test_incomplete_scan_page_warns_in_the_browser(integrity_browser, web):
    pages = _html(web)
    ctx = integrity_browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    pg.route("**/*", lambda route: route.abort())
    pg.set_content(pages["scan_incomplete"], wait_until="domcontentloaded")
    text = " ".join(pg.inner_text("main").split())
    assert "This scan was not complete" in text and "Reddit failed" in text and "does not show that no demand exists" in text
    assert pg.locator("article.lead").count() == 0
    ctx.close()
