"""Whole-app UI accessibility / responsive behaviour that was audited and fixed in the post-review pass.
Real Chromium, network aborted, templates rendered with Jinja (no server). Covers: search-mode radio keyboard model, touch hit areas on
coarse pointers, and no horizontal scroll at phone / tablet / desktop widths."""
import pytest

from tests.test_guest_gate import CHAT, ORIGIN, load, render, needs_browser

PAGES = [("index.html", {}), ("chat.html", {"chat": CHAT, "chat_id": "c1"}),
         ("pricing.html", {}), ("discover.html", {}), ("find-buyers-on-reddit.html", {})]


def _ctx(browser, w, h, touch=False):
    c = browser.new_context(viewport={"width": w, "height": h}, has_touch=touch, is_mobile=touch)
    pg = c.new_page()
    pg.route("**/*", lambda r: r.abort())
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    return c, pg


def _open(pg, name, ctx):
    html = render(name, **ctx)
    pg.route(ORIGIN + "/__page__", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
    pg.goto(ORIGIN + "/__page__", wait_until="domcontentloaded")
    pg.wait_for_timeout(250)


USER = {"name": "Ada Lovelace", "email": "ada@example.com", "picture": None}      # signed in: no guest welcome overlay covering the composer


@needs_browser
@pytest.mark.parametrize("name,ctx", [("index.html", {"user": USER}), ("chat.html", {"chat": CHAT, "chat_id": "c1", "user": USER})])
def test_small_icon_buttons_get_a_larger_tap_area_on_touch_devices_only(integrity_browser, name, ctx):
    sel = "#attach-btn"
    # touch: a point 8px outside the visible button still resolves to it
    c, pg = _ctx(integrity_browser, 375, 812, touch=True)
    try:
        _open(pg, name, ctx)
        hit = pg.evaluate("""(s)=>{const b=document.querySelector(s);const r=b.getBoundingClientRect();
            const e=document.elementFromPoint(r.left-8,r.top+r.height/2);return !!e&&(e===b||b.contains(e))}""", sel)
        assert hit, "tap area not enlarged on a coarse pointer"
    finally:
        c.close()
    # mouse: unchanged (the visible control is exactly its own hit area)
    c, pg = _ctx(integrity_browser, 1200, 800)
    try:
        _open(pg, name, ctx)
        hit = pg.evaluate("""(s)=>{const b=document.querySelector(s);const r=b.getBoundingClientRect();
            const e=document.elementFromPoint(r.left-8,r.top+r.height/2);return !!e&&(e===b||b.contains(e))}""", sel)
        assert not hit
    finally:
        c.close()


@needs_browser
@pytest.mark.parametrize("w,h", [(375, 812), (768, 1024), (1280, 800)])
@pytest.mark.parametrize("name,ctx", PAGES)
def test_no_horizontal_scroll_and_no_script_errors_at_phone_tablet_and_desktop_widths(integrity_browser, name, ctx, w, h):
    c, pg = _ctx(integrity_browser, w, h)
    try:
        _open(pg, name, ctx)
        assert pg.evaluate("document.documentElement.scrollWidth") <= w + 1
        assert pg.errors == []
    finally:
        c.close()


@pytest.mark.parametrize("name,ctx", [("index.html", {"user": USER}), ("chat.html", {"chat": CHAT, "chat_id": "c1", "user": USER})])
def test_the_search_box_has_no_mode_selector_helper_text_or_field(name, ctx):
    """Search modes are internal: no selector, no explanatory text, no hidden mode field in the form (rendered HTML)."""
    html = render(name, **ctx)
    for gone in ("search-mode-input", "search-mode-group", "search-mode-hint", 'name="search_mode"', 'class="search-mode"', "sm-opt",
                 "stored conversations plus live Hacker News stories"):
        assert gone not in html, gone
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "templates"
    assert not (root / "_search_mode.html").exists()
    assert "search-mode" not in (root / "_touch_targets.html").read_text()


@needs_browser
@pytest.mark.parametrize("w,h", [(375, 812), (1280, 800)])
@pytest.mark.parametrize("name,ctx", [("index.html", {"user": USER}), ("chat.html", {"chat": CHAT, "chat_id": "c1", "user": USER})])
def test_the_search_form_submits_no_mode_and_the_composer_lays_out_cleanly(integrity_browser, name, ctx, w, h):
    c, pg = _ctx(integrity_browser, w, h)
    try:
        _open(pg, name, ctx)
        assert pg.evaluate("document.querySelectorAll('.search-mode,.sm-hint,.sm-opt,input[name=search_mode]').length") == 0
        form = pg.evaluate("""(()=>{const t=document.querySelector('textarea');const f=t&&t.form;if(!f)return null;
            return [...new FormData(f).keys()]})()""")
        assert form is not None and "search_mode" not in form
        assert pg.evaluate("document.documentElement.scrollWidth") <= w + 1
        assert pg.errors == []
    finally:
        c.close()
