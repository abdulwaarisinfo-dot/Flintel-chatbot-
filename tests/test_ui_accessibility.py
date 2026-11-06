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


@needs_browser
@pytest.mark.parametrize("name,ctx", PAGES[:2])
def test_search_mode_is_a_keyboard_operable_radio_group(integrity_browser, name, ctx):
    c, pg = _ctx(integrity_browser, 1200, 800)
    try:
        _open(pg, name, ctx)
        state = lambda: pg.evaluate("()=>[...document.querySelectorAll('.sm-opt')].map(b=>[b.dataset.mode,b.getAttribute('aria-checked'),b.tabIndex])")
        assert state() == [["live", "false", -1], ["historical", "false", -1], ["combined", "true", 0]]      # one tab stop: the checked radio
        pg.focus('.sm-opt[data-mode="combined"]')
        pg.keyboard.press("ArrowRight")                                                                    # wraps to the first
        assert pg.evaluate("document.getElementById('search-mode-input').value") == "live"
        assert pg.evaluate("document.activeElement.dataset.mode") == "live"
        pg.keyboard.press("ArrowRight")
        assert pg.evaluate("document.getElementById('search-mode-input').value") == "historical"
        pg.keyboard.press("End")
        assert pg.evaluate("document.getElementById('search-mode-input').value") == "combined"
        pg.keyboard.press("Home")
        assert pg.evaluate("document.getElementById('search-mode-input').value") == "live"
        pg.keyboard.press("ArrowLeft")
        assert pg.evaluate("document.getElementById('search-mode-input').value") == "combined"
        assert state() == [["live", "false", -1], ["historical", "false", -1], ["combined", "true", 0]]
        pg.wait_for_function("document.getElementById('search-mode-hint').textContent.startsWith('Combined')")   # the hint follows the keyboard change (updated on the next tick)
        assert pg.errors == []
    finally:
        c.close()


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
        modes = pg.evaluate("[...document.querySelectorAll('.sm-opt')].map(b=>b.getBoundingClientRect().height)")
        assert min(modes) >= 36
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


@needs_browser
@pytest.mark.parametrize("name,ctx", [("index.html", {"user": USER}), ("chat.html", {"chat": CHAT, "chat_id": "c1", "user": USER})])
def test_search_mode_hint_takes_at_most_two_lines_on_a_phone_and_keeps_the_full_text(integrity_browser, name, ctx):
    c, pg = _ctx(integrity_browser, 375, 812, touch=True)
    try:
        _open(pg, name, ctx)
        h = pg.evaluate("(()=>{const e=document.getElementById('search-mode-hint');const lh=parseFloat(getComputedStyle(e).lineHeight);return [e.getBoundingClientRect().height,lh,e.textContent,e.title]})()")
        height, line, text, title = h
        assert text and title == text                                         # nothing is lost: the whole sentence is in the tooltip
        assert height <= 2 * line + 1
    finally:
        c.close()
