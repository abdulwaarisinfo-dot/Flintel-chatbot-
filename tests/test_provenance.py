"""Per-result provenance (LIVE / HISTORICAL / BOTH): kept through retrieval, dedup, evidence top-up + cache, ranking, classification, the
answer JSON, and rendered on every card in a real browser. Fakes are labelled; no network."""
import json
import re
from pathlib import Path

import pytest

import search_modes as sm
import signal_intel as si
from tests.test_search_modes import P, lg  # noqa: F401  (fixture + helper)
from tests.test_time_ranges import REF, app_env, env, fixed_clock  # noqa: F401,E402

ROOT = Path(__file__).resolve().parent.parent


def test_provenance_map_uses_only_real_metadata(lg):
    posts = [P("https://a.test/1", retrieval_path="live"), P("https://a.test/2", retrieval_path="both"),
             P("https://a.test/3", retrieval_path="historical"), P("https://a.test/4"),                 # a Google stub: no metadata
             P("https://a.test/5", retrieval_path="bogus"), {"retrieval_path": "live"}, None]
    assert lg.provenance_map(posts) == {"https://a.test/1": "live", "https://a.test/2": "both", "https://a.test/3": "historical"}


def test_answer_carries_the_per_result_map(lg):
    ans = json.dumps({"format": "x"})
    out = json.loads(lg.enrich_search_mode(ans, {"mode": "combined", "live": "ok", "historical": "ok", "stats": {}},
                                           [P("https://a.test/1", retrieval_path="both")]))
    assert out["search_mode"]["provenance"] == {"https://a.test/1": "both"}
    out = json.loads(lg.enrich_search_mode(ans, {"mode": "combined", "live": "ok", "historical": "ok", "stats": {}}, [P("https://a.test/9")]))
    assert "provenance" not in out["search_mode"]


def test_dedup_keeps_the_stored_record_metadata_and_marks_both():
    stored = P("http://news.ycombinator.com/item?id=1&ref=x", title="stored title", post_text="stored body", score=5, username="u")
    live = P("https://news.ycombinator.com/item?id=1", hn_id="1", title="live title", hn_points=9)
    out, st = sm.merge([live], [stored])
    assert len(out) == 1 and out[0]["retrieval_path"] == "both"
    assert out[0]["title"] == "stored title" and out[0]["post_url"] == stored["post_url"] and out[0]["username"] == "u"   # nothing lost, URL as stored
    assert st["duplicates_removed"] == 1


def test_dedup_does_not_drop_live_only_fields_needed_for_display():
    live = P("https://news.ycombinator.com/item?id=1", hn_id="1", hn_points=9, created_utc=1700000000.0)
    stored = P("https://news.ycombinator.com/item?id=1")                              # a sparse stored copy
    out, _ = sm.merge([live], [stored])
    assert out[0].get("hn_points") == 9 and out[0].get("created_utc") == 1700000000.0 and out[0]["hn_id"] == "1"


def test_merge_fills_missing_fields_only_never_overwrites():
    out, _ = sm.merge([{"post_url": "https://x.test/a", "title": "LIVE", "extra": 1}], [{"post_url": "https://x.test/a", "title": "STORED"}])
    assert out[0]["title"] == "STORED" and out[0]["extra"] == 1


def test_provenance_survives_the_evidence_topup_and_cache(lg, monkeypatch):
    mongomock = pytest.importorskip("mongomock")
    monkeypatch.setattr(lg, "topic_evidence_cache_collection", mongomock.MongoClient().db.cache)      # a real (in-memory) cache store
    """get_evidence_with_topup is the real function between the matcher and the answer; a fake matcher stands in for retrieval."""
    posts = [P("https://news.ycombinator.com/item?id=1", hn_id="1", retrieval_path="live"),
             P("https://reddit.com/r/x/comments/a1/t/", platform="reddit", retrieval_path="historical"),
             P("https://news.ycombinator.com/item?id=2", retrieval_path="both")]

    def matcher(topic_key, keywords, targeting_platform="all", limit=None, **kw):
        return [dict(p) for p in posts][: limit or 10]
    got = lg.get_evidence_with_topup(chat_id="c1", owner_key="o", topic_key="t", keywords=["k"], evidence_required=3, matcher_fn=matcher,
                                     targeting_platform="all")
    assert {p["post_url"]: p.get("retrieval_path") for p in got} == {p["post_url"]: p["retrieval_path"] for p in posts}
    lg.save_topic_evidence_cache("c1", "o", "t", got, ["k"], extra_fields={"platform_key": lg.platforms.selection_key("all")})            # what the route's top-up persists
    again = lg.get_evidence_with_topup(chat_id="c1", owner_key="o", topic_key="t", keywords=["k"], evidence_required=3,
                                       matcher_fn=lambda *a, **k: pytest.fail("a cache hit must not re-query"), targeting_platform="all")
    assert {p["post_url"]: p.get("retrieval_path") for p in again} == {p["post_url"]: p["retrieval_path"] for p in posts}   # from the cache


def test_ranking_hook_does_not_strip_provenance(lg, monkeypatch):
    recs = [P("https://news.ycombinator.com/item?id=%d" % i, hn_id=str(i), retrieval_path="live") for i in range(3)]
    ranked = lg._hn_rank_pool(recs, "t", "Hacker News", 3, "q", "c")
    assert ranked and all(r.get("hn_id") for r in ranked)            # ranking returns the same records (tagging happens after, in the mode layer)


def test_classification_is_identical_whatever_the_retrieval_path():
    base = {"title": "Looking for an invoicing tool", "post_text": "I need a tool that can automate my invoices. Budget is $50/month.",
            "post_url": "https://news.ycombinator.com/item?id=77", "platform": "hackernews", "created_utc": 1700000000.0}
    res = {path: si.classify_post(dict(base, retrieval_path=path), "invoicing tool") for path in ("live", "historical", "both", None)}
    ref = res[None]
    for path, c in res.items():
        assert {k: v for k, v in c.items() if k != "retrieval_path"} == {k: v for k, v in ref.items() if k != "retrieval_path"}
        assert c["retrieval_path"] == path
    assert ref["class"] == "DIRECT_DEMAND"


def test_signal_report_items_carry_provenance():
    rep = si.build_signal_report([{"title": "t", "post_text": "I need a tool for invoices", "post_url": "https://a.test/1", "platform": "hackernews",
                                   "retrieval_path": "both"}], "invoices", "topic", {})
    assert [i["retrieval_path"] for i in rep["items"]] == ["both"]


# ── rendering: the real appendSearchMode from the shipped templates, in a browser ─────────────────────────────────────────────
def _fn(template):
    src = (ROOT / "templates" / template).read_text()
    i = src.index("function appendSearchMode(")
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += (src[k] == "{") - (src[k] == "}")
        if depth == 0:
            return src[i:k + 1]
    raise AssertionError("function end not found")


@pytest.fixture
def page(integrity_browser):
    return integrity_browser            # session-scoped Chromium from conftest (starts safely even after other modules left an event loop)


@pytest.mark.parametrize("template", ["chat.html", "index.html"])
@pytest.mark.parametrize("width", [360, 1280])
def test_every_card_shows_its_label(page, template, width):
    pg = page.new_page(viewport={"width": width, "height": 800})
    cards = "".join(f'<div class="post-block"><p class="post-label">Post {i + 1}</p><div class="post-field"><a href="{u}">{u}</a></div></div>'
                    for i, u in enumerate(["https://a.test/live", "https://a.test/hist", "https://a.test/both", "https://a.test/stub"]))
    pg.set_content(f"<div id=c>{cards}</div><script>{_fn(template)}</script>")
    data = {"search_mode": {"mode": "combined", "live": "ok", "historical": "ok", "counts": {"live": 1, "historical": 1, "both": 1},
                            "provenance": {"https://a.test/live": "live", "https://a.test/hist": "historical", "https://a.test/both": "both"}}}
    pg.evaluate("d => appendSearchMode(d, document.getElementById('c'))", data)
    tags = pg.eval_on_selector_all(".post-block", "els => els.map(e => { const t = e.querySelector('.prov-tag'); return t ? t.textContent : null; })")
    assert tags == ["LIVE", "HISTORICAL", "BOTH", None]                      # the un-annotated card is NOT given a guessed label
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    pg.evaluate("d => appendSearchMode(d, document.getElementById('c'))", data)   # idempotent: no duplicate tags
    assert pg.locator(".post-block .prov-tag").count() == 3


def test_script_in_provenance_cannot_inject_html(page):
    pg = page.new_page()
    pg.set_content(f'<div id=c><div class="post-block"><p class="post-label">Post 1</p><div class="post-field"><a href="https://a.test/x">x</a></div></div></div><script>{_fn("chat.html")}</script>')
    pg.evaluate("d => appendSearchMode(d, document.getElementById('c'))",
                {"search_mode": {"mode": "combined", "provenance": {"https://a.test/x": "<img src=x onerror=alert(1)>"}, "notes": ["<b>n</b>"]}})
    assert pg.locator("#c img").count() == 0 and pg.locator(".post-block .prov-tag").count() == 0 and pg.locator("#c b").count() == 0
