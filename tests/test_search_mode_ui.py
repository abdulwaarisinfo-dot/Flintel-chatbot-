"""Browser check of the mode control (Playwright, offline): default visible, selection updates the form field, remembered, mobile width."""
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parent.parent


def _page():
    part = re.sub(r"\{#.*?#\}", "", (ROOT / "templates" / "_search_mode.html").read_text(), flags=re.S)
    return f"<!doctype html><meta name=viewport content='width=device-width'><form id=f>{part}</form>"


@pytest.fixture
def browser_page(integrity_browser):
    ctx = integrity_browser.new_context(viewport={"width": 360, "height": 640})
    page = ctx.new_page()
    page.route("https://app.test/**", lambda route: route.fulfill(body=_page(), content_type="text/html"))   # a real origin: localStorage works
    page.goto("https://app.test/")
    yield page
    ctx.close()


def test_default_is_combined_and_visible(browser_page):
    checked = browser_page.locator(".sm-opt[aria-checked=true]")
    assert checked.count() == 1 and checked.inner_text() == "Combined"
    assert browser_page.input_value("#search-mode-input") == "combined"


def test_choosing_a_mode_updates_the_submitted_field_and_labels(browser_page):
    for label, value in (("Live", "live"), ("Historical", "historical")):
        browser_page.get_by_role("radio", name=label).click()
        assert browser_page.input_value("#search-mode-input") == value
        assert browser_page.locator(".sm-opt[aria-checked=true]").inner_text() == label
    assert browser_page.evaluate("new FormData(document.getElementById('f')).get('search_mode')") == "historical"


def test_fits_a_phone_without_horizontal_scroll(browser_page):
    assert browser_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_invalid_stored_value_falls_back_to_default(browser_page):
    browser_page.evaluate("localStorage.setItem('flintel_search_mode','bogus')")
    browser_page.reload()
    assert browser_page.input_value("#search-mode-input") == "combined"


def test_hint_explains_each_mode_and_warns_for_an_unsupported_selection(browser_page):
    hint = lambda: browser_page.inner_text("#search-mode-hint")           # noqa: E731
    assert "Combined" in hint() and "Reddit is searched from stored conversations only" in hint()
    browser_page.evaluate("document.body.insertAdjacentHTML('beforeend','<input id=platform-input value=Reddit>')")
    browser_page.get_by_role("radio", name="Live").click()
    browser_page.wait_for_function("document.getElementById('search-mode-hint').textContent.includes('not available')")
    assert "Reddit has no live source" in hint()
    browser_page.evaluate("document.getElementById('platform-input').value='Hacker News'")
    browser_page.get_by_role("radio", name="Live").click()
    browser_page.wait_for_function("document.getElementById('search-mode-hint').textContent.includes('bounded fetch')")
    browser_page.get_by_role("radio", name="Historical").click()
    browser_page.wait_for_function("document.getElementById('search-mode-hint').textContent.includes('not a view of what was posted')")
