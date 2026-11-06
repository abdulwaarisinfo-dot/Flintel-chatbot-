"""Mode x query-shape x source-combination matrix at the retrieval layer (fakes for the two paths; labelled). Checks what each path was
asked (topic / unfiltered / date window pass through), that no path silently suppresses the other, that nothing is double counted, and
that evidence fields survive. Relevance filtering is NOT touched here: the stored path is a fake that returns what it is given."""
import itertools

import pytest

import search_modes as sm
from tests.test_search_modes import P
from tests.test_time_ranges import app_env, env, fixed_clock  # noqa: F401

H = lambda i, **k: P(f"https://reddit.com/r/x/comments/h{i}/t/", platform="reddit", title=f"hist {i}", post_text="body", created_utc=1700000000.0 + i, **k)   # noqa: E731
L = lambda i, **k: P(f"https://news.ycombinator.com/item?id={i}", hn_id=str(i), title=f"live {i}", post_text="body", created_utc=1700000100.0 + i, **k)    # noqa: E731


@pytest.fixture
def lg(fixed_clock):
    return fixed_clock[0]


def run(lg, monkeypatch, mode, *, sources, unfiltered=False, keywords=("crm",), since=7, hist=(), live=(), live_state="ok", hist_boom=False, live_boom=False):
    seen = {"hist": None, "live": None}
    wants = "hackernews" in sources
    hn_only = sources == ("hackernews",)
    monkeypatch.setattr(lg, "_hn_wanted", lambda p: (wants, hn_only))
    monkeypatch.setitem(lg._HN_STATUS, ("c", "t"), {"state": live_state})

    def core(*a, **k):
        seen["hist"] = {"unfiltered": a[5], "since": a[4], "kw": a[1]}
        if hist_boom:
            raise RuntimeError("stored path down")
        return [dict(p) for p in hist]

    def hn_wrapped(*a, **k):                     # stored + live merged by the existing code (combined path)
        seen["hist"] = {"unfiltered": a[5], "since": a[4], "kw": a[1]}
        seen["live"] = dict(seen["hist"])
        if hist_boom:
            raise RuntimeError("stored path down")
        lg._mode_scope_now()["_hn_box"] = {"recs": [dict(p) for p in live]}
        return [dict(p) for p in hist] + ([dict(p) for p in live] if wants and not live_boom else [])

    def hn_only_fn(topic, kw, plat, limit, since_days, uq, ss, tr, cid, unfiltered=False):
        seen["live"] = {"unfiltered": unfiltered, "since": since_days, "kw": kw}
        if live_boom:
            raise RuntimeError("live down")
        return [dict(p) for p in live]
    monkeypatch.setattr(lg, "_get_matched_signals_core", core)
    monkeypatch.setattr(lg, "_get_matched_signals_hn", hn_wrapped)
    monkeypatch.setattr(lg, "_hn_only_matched", hn_only_fn)
    plat = {("reddit",): "Reddit", ("hackernews",): "Hacker News", ("reddit", "hackernews"): "all"}[sources]
    with lg.search_mode_scope(mode) as info:
        try:
            res = lg.get_matched_signals("t", [] if unfiltered else list(keywords), plat, 10, since, unfiltered, chat_id="c")
            err = None
        except RuntimeError as exc:
            res, err = [], str(exc)
    return res, info, seen, err


SRC = [("reddit",), ("hackernews",), ("reddit", "hackernews")]


@pytest.mark.parametrize("sources,unfiltered", list(itertools.product(SRC, [False, True])))
def test_combined_keeps_both_paths_and_never_double_counts(lg, monkeypatch, sources, unfiltered):
    hist = [H(1), H(2), P("https://news.ycombinator.com/item?id=7", platform="hackernews", title="stored 7")] if "hackernews" in sources else [H(1), H(2)]
    live = [L(7), L(8)] if "hackernews" in sources else []
    res, info, seen, _ = run(lg, monkeypatch, "combined", sources=sources, unfiltered=unfiltered, hist=hist, live=live)
    urls = [sm.post_identity(p) for p in res]
    assert len(urls) == len(set(urls))                                                       # nothing counted twice
    assert {p["title"] for p in res if p["title"].startswith("hist")} == {"hist 1", "hist 2"}   # the stored path is not suppressed
    if "hackernews" in sources:
        assert any(p["retrieval_path"] in ("live", "both") for p in res)                     # the live path is not suppressed
        both = [p for p in res if sm.post_identity(p) == sm.canonical_url("https://news.ycombinator.com/item?id=7")]
        assert len(both) == 1 and both[0]["retrieval_path"] == "both" and both[0]["title"] == "stored 7"
    assert seen["hist"]["unfiltered"] is unfiltered and (unfiltered is False or seen["hist"]["kw"] == [])


@pytest.mark.parametrize("sources", SRC)
@pytest.mark.parametrize("unfiltered", [False, True])
def test_historical_is_stored_only_with_the_same_question(lg, monkeypatch, sources, unfiltered):
    res, info, seen, _ = run(lg, monkeypatch, "historical", sources=sources, unfiltered=unfiltered, hist=[H(1)], live=[L(9)])
    assert [p["title"] for p in res] == ["hist 1"] and seen["live"] is None and info["live"] == "not_requested"
    assert seen["hist"]["unfiltered"] is unfiltered


@pytest.mark.parametrize("sources", SRC)
@pytest.mark.parametrize("unfiltered", [False, True])
def test_live_is_live_only_and_honest_without_a_live_source(lg, monkeypatch, sources, unfiltered):
    res, info, seen, _ = run(lg, monkeypatch, "live", sources=sources, unfiltered=unfiltered, hist=[H(1)], live=[L(9)])
    if "hackernews" in sources:
        assert [p["title"] for p in res] == ["live 9"] and seen["hist"] is None and seen["live"]["unfiltered"] is unfiltered
    else:
        assert res == [] and info["live"] == "unavailable" and seen["hist"] is None and seen["live"] is None


@pytest.mark.parametrize("mode", ["live", "historical", "combined"])
def test_date_window_reaches_the_path_that_runs(lg, monkeypatch, mode):
    _, _, seen, _ = run(lg, monkeypatch, mode, sources=("reddit", "hackernews"), since=3, hist=[H(1)], live=[L(2)])
    assert [v["since"] for v in seen.values() if v] and all(v["since"] == 3 for v in seen.values() if v)


@pytest.mark.parametrize("mode", ["live", "historical", "combined"])
@pytest.mark.parametrize("sources", SRC)
def test_empty_results_are_empty_and_say_which_path_was_empty(lg, monkeypatch, mode, sources):
    res, info, _, _ = run(lg, monkeypatch, mode, sources=sources, hist=[], live=[], live_state="empty")
    assert res == []
    cov = sm.mode_coverage(mode, info["live"], info["historical"], info["stats"], info.get("unsupported"))
    assert cov["mode"] == mode and not cov["partial"]
    assert (cov["live"] == "not_requested") == (mode == "historical") and (cov["historical"] == "not_requested") == (mode == "live")


def test_partial_failure_live_down_historical_still_served(lg, monkeypatch):
    res, info, _, _ = run(lg, monkeypatch, "combined", sources=("reddit", "hackernews"), hist=[H(1)], live=[L(1)], live_state="failed", live_boom=True)
    assert [p["title"] for p in res] == ["hist 1"] and info["live"] == "failed" and info["historical"] == "ok"
    assert sm.mode_coverage("combined", info["live"], info["historical"])["partial"] is True


def test_stored_path_failure_is_reported_not_swallowed_as_empty(lg, monkeypatch):
    res, info, _, err = run(lg, monkeypatch, "historical", sources=("reddit",), hist=[H(1)], hist_boom=True)
    assert err == "stored path down" and info["historical"] == "failed"


def test_evidence_fields_survive_every_mode(lg, monkeypatch):
    keep = ("post_url", "title", "post_text", "platform", "created_utc")
    for mode, src in (("historical", ("reddit",)), ("live", ("hackernews",)), ("combined", ("reddit", "hackernews"))):
        res, *_ = run(lg, monkeypatch, mode, sources=src, hist=[H(3)] if mode != "live" else [], live=[L(4)] if "hackernews" in src else [])
        for p in res:
            assert all(p.get(k) not in (None, "") for k in keep), (mode, p)
            assert p["post_url"].startswith("https://")                                      # the original URL, not the canonical form
