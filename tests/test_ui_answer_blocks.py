"""Real-browser regression for the answer blocks (search-mode note + signal report) rendered by the SHIPPED template functions, using the shipped
stylesheet: structure, plain-language wording, readable sizes, no horizontal overflow on a phone, and no HTML injection from data."""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = {
    "search_mode": {"mode": "combined", "live": "ok", "historical": "ok", "counts": {"live": 1, "historical": 1, "both": 1},
                    "notes": ["Reddit has no live source; its results come from stored conversations only."], "provenance": {}},
    "signal_report": {"mode": "topic", "statement": "1 of 3 retrieved posts is a direct request.", "coverage": {"sources": ["Reddit", "Hacker News"], "returned": 3},
                      "items": [
                          {"class": "DIRECT_DEMAND", "label": "Direct demand", "title": "Ask HN: best tool?", "url": "https://news.ycombinator.com/item?id=1",
                           "platform": "hackernews", "retrieval_path": "live", "author": "u1", "published_at": "2026-11-04T09:12:00Z", "quote": "I need a tool.",
                           "intent_explanation": "Explicit request.", "limitations": ["Budget not stated."]},
                          {"class": "PROBLEM_SIGNAL", "label": "Problem signal", "title": "Invoicing is a nightmare", "url": "https://www.reddit.com/r/a/comments/1/x/",
                           "platform": "reddit", "retrieval_path": "historical", "author": None, "published_at": "2026-10-20T13:00:00Z", "quote": "We lose track of invoices."},
                          {"class": "MARKET_SIGNAL", "label": "Market signal", "title": "<img src=x onerror=alert(1)>", "url": "javascript:alert(1)",
                           "platform": "<b>evil</b>", "retrieval_path": "both", "quote": "Prices rose."}]}}


def _block(src, start, end_marker):
    i = src.index(start)
    j = src.index("{", i)
    d = 0
    for k in range(j, len(src)):
        d += (src[k] == "{") - (src[k] == "}")
        if d == 0:
            return src[i:k + 1]
    raise AssertionError(end_marker)


def _page(template):
    src = (ROOT / "templates" / template).read_text()
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", src, flags=re.S))

    def const(n):
        i = src.index(f"const {n} =")
        return src[i:src.index(";", src.index("}", i) if "{" in src[i:i + 80] else i) + 1]
    js = const("SIGNAL_ORDER") + "\n" + const("SIGNAL_LABEL") + "\n" + _block(src, "function appendSearchMode(", "sm") + "\n" + _block(src, "function appendSignalReport(", "sr")
    return (f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><style>{css}</style>"
            f"<div class='answer-wrap'><div class='answer-rendered' id='r' style='display:block'></div></div>"
            f"<script>{js}\nconst d={json.dumps(DATA)};const c=document.getElementById('r');appendSearchMode(d,c);appendSignalReport(d,c);</script>")


@pytest.fixture(params=["chat.html", "index.html"])
def rendered(request, integrity_browser):
    out = {}

    def make(width):
        ctx = integrity_browser.new_context(viewport={"width": width, "height": 800})
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.set_content(_page(request.param))
        out.setdefault("ctx", []).append(ctx)
        return pg, errs
    yield make
    for c in out.get("ctx", []):
        c.close()


@pytest.mark.parametrize("width", [360, 1280])
def test_structure_wording_and_layout(rendered, width):
    pg, errs = rendered(width)
    assert errs == []
    assert pg.locator(".search-mode-note .sm-head strong").inner_text() == "Search mode"
    assert pg.locator(".search-mode-note .sm-mode").inner_text() == "COMBINED"
    text = pg.locator(".search-mode-note").inner_text()
    assert "Live source: ok" in text and "Stored conversations: ok" in text and "Live path" not in text and "found by both" in text
    meta = pg.locator(".signal-report .signal-meta").all_inner_texts()
    assert "Hacker News" in meta[0] and "Reddit" in meta[1] and not any(m.lower().startswith(("hackernews", "reddit")) for m in meta[:2])
    tags = pg.eval_on_selector_all(".signal-report .prov-tag", "els => els.map(e => e.textContent)")
    assert tags == ["LIVE", "HISTORICAL", "BOTH"]
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")           # no horizontal scroll, even on a phone


def test_every_text_is_at_least_11px(rendered):
    pg, _ = rendered(360)
    sizes = pg.evaluate("[...document.querySelectorAll('.answer-rendered *')].filter(e => e.children.length === 0 && e.textContent.trim())"
                        ".map(e => parseFloat(getComputedStyle(e).fontSize))")
    assert sizes and min(sizes) >= 11


def test_data_cannot_inject_html_or_a_javascript_link(rendered):
    pg, _ = rendered(360)
    assert pg.locator("#r img").count() == 0 and pg.locator("#r b").count() == 0
    hrefs = pg.eval_on_selector_all("#r a", "els => els.map(e => e.getAttribute('href'))")
    assert not any((h or "").lower().startswith("javascript:") for h in hrefs)
