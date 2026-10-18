"""
tests/test_mapreduce_identity.py — post identity in the map-reduce analysis path
(> CLAUDE_POSTS_PER_CHUNK posts).

SYNTHETIC FIXTURES; no real Claude call. `_call_claude` is replaced by a fake
that answers the map step and the final step. The map step's reply uses the
PROPOSED "POST RECORDS:" block (one JSON object per analysed post, written
while the model can see that post's title and text). The final step's reply
deliberately swaps the per-post content of two similar posts (7 and 19).
These tests check the server-side logic only; they do not exercise a real
Claude call, retrieval or the production deployment.

Required behaviour: each row's summary / classification comes from the map
record of THAT post, verified against its stored title and text; a row without
a verified record is unverified and can never qualify.
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_answer_persistence import env  # noqa: E402,F401

QUERY = ("I run a B2B SaaS company that helps businesses monitor Reddit conversations and find people "
         "actively looking for a solution like ours.")

SIMILAR = {   # two posts on the same topic: only post 7 states a purchase step
    7: ("SaaS", "Need a Reddit monitoring tool, budget $200 per month",
        "Looking for a Reddit monitoring tool for our SaaS. Budget is $200 per month and we can start a trial this week. "
        "The budget is approved."),
    19: ("SaaS", "Need a Reddit monitoring tool for our SaaS",
         "Looking for a Reddit monitoring tool for our SaaS team. No budget yet, just researching options."),
}
SHARED = "Looking for a Reddit monitoring tool for our SaaS"          # occurs in both 7 and 19
OWN_QUOTE = {7: "Budget is $200 per month and we can start a trial", 19: "No budget yet, just researching options"}
OWN_SUMMARY = {7: "Wants a Reddit monitoring tool with a $200 monthly budget and a trial this week.",
               19: "Researching Reddit monitoring tools for a SaaS team; no budget yet."}
OWN_LABELS = {7: {"role": "buyer", "purchase_intent": "explicit", "product_fit": "fits"},
              19: {"role": "buyer", "purchase_intent": "research", "product_fit": "fits"}}


def _post(n):
    if n in SIMILAR:
        sub, title, text = SIMILAR[n]
    else:
        sub, title, text = "cooking", f"Cast iron question {n}", f"How do I season cast iron pan number {n} at home?"
    return {"title": title, "post_text": text, "post_url": f"https://www.reddit.com/r/{sub}/comments/mr{n}/p{n}/",
            "platform": "reddit", "subreddit": sub, "created_utc": "2026-10-01T12:00:00+00:00"}


def _pool(n_posts):
    return [_post(n) for n in range(1, n_posts + 1)]


def _record(n, **override):
    rec = dict({"post": n, "title": SIMILAR[n][1], "quote": OWN_QUOTE[n], "summary": OWN_SUMMARY[n]},
               **OWN_LABELS[n])
    rec.update(override)
    return rec


def _map_reply(numbers, records):
    bullets = "\n".join(f"- point about post {n} (Post {n})" for n in numbers if n in SIMILAR) or \
        "No relevant points in this batch."
    block = "\n".join(json.dumps(r) for r in records)
    return f"{bullets}\n\nPOST RECORDS:\n{block}" if records else bullets


def _final_answer(entries, **extra):
    return json.dumps(dict({"format": "source_list", "headline": "Reddit monitoring demand",
                            "platforms": [{"platform": "reddit", "posts": entries}]}, **extra))


SWAPPED = [   # the final writer saw only notes: it put post 7's analysis on 19 and vice versa
    dict({"index": 19, "title": "Reddit tool request", "summary": OWN_SUMMARY[7], "evidence": OWN_QUOTE[7],
          "sentiment": "neutral"}, **OWN_LABELS[7]),
    dict({"index": 7, "title": "Reddit tool request", "summary": OWN_SUMMARY[19], "evidence": OWN_QUOTE[19],
          "sentiment": "neutral"}, **OWN_LABELS[19]),
]


@pytest.fixture
def lg(env):
    return env[0]


def _run(lg, monkeypatch, n_posts, final_entries, map_records=None, condense=None, map_text=None, **final_extra):
    """map_records(numbers_in_chunk) -> records for that chunk (default: the true records);
    map_text(numbers_in_chunk) -> a raw map reply, replacing the generated one."""
    calls = {"map": 0, "condense": 0, "final": 0}
    map_records = map_records or (lambda nums: [_record(n) for n in nums if n in SIMILAR])

    def fake_call(system, user, **kw):
        if system is lg.CLAUDE_MAP_STEP_SYSTEM_PROMPT:
            calls["map"] += 1
            nums = [int(n) for n in re.findall(r"\[Post (\d+)\]", user)]
            return map_text(nums) if map_text else _map_reply(nums, map_records(nums))
        if system is lg.CLAUDE_NOTES_REDUCE_SYSTEM_PROMPT:
            calls["condense"] += 1
            return condense(user) if condense else "- condensed points (Post 7) (Post 19)"
        calls["final"] += 1
        return _final_answer(final_entries, **final_extra)

    monkeypatch.setattr(lg, "_call_claude", fake_call)
    pool = _pool(n_posts)
    answer = lg.analyze_with_claude(QUERY, pool)
    out = json.loads(lg._patch_post_urls_into_answer(answer, pool, query=QUERY))
    return out, calls


def _rows(out):
    return [p for e in out.get("platforms", []) for p in e["posts"]]


def _row(out, n):
    url = _post(n)["post_url"]
    return next((p for e in out.get("platforms", []) for p in e["posts"] if p["link"] == url), None)


# ── > 12 posts: rows come from verified map records ─────────────────────────

def test_1_more_than_12_posts_rows_use_their_own_verified_map_record(lg, monkeypatch):
    out, calls = _run(lg, monkeypatch, 26, SWAPPED)
    assert calls["map"] == 3 and calls["final"] == 1                       # map-reduce path (12 + 12 + 2)
    r7, r19 = _row(out, 7), _row(out, 19)
    assert r7["analysis_identity"] == "map_record" and r19["analysis_identity"] == "map_record"
    assert r7["summary"] == OWN_SUMMARY[7] and r19["summary"] == OWN_SUMMARY[19]
    assert r7["evidence"] == OWN_QUOTE[7] and r19["evidence"] == OWN_QUOTE[19]


def test_2_similar_posts_are_not_confused(lg, monkeypatch):
    out, _ = _run(lg, monkeypatch, 26, SWAPPED)
    r7, r19 = _row(out, 7), _row(out, 19)
    assert r7["qualified"] is True and r7["purchase_intent"] == "explicit"
    assert r19["qualified"] is False and r19["purchase_intent"] == "research"
    assert "$200" not in json.dumps(r19) and "No budget yet" not in json.dumps(r7)
    assert out["counts"]["qualified"] == 1


def test_3_similar_posts_in_the_same_chunk_are_not_confused(lg, monkeypatch):
    # Move the second similar post from 19 to 9, so both are in the first chunk (posts 1-12).
    SIMILAR[9] = SIMILAR.pop(19)
    OWN_QUOTE[9], OWN_SUMMARY[9], OWN_LABELS[9] = OWN_QUOTE.pop(19), OWN_SUMMARY.pop(19), OWN_LABELS.pop(19)
    try:
        swapped = [dict(SWAPPED[0], index=9), SWAPPED[1]]
        out, _ = _run(lg, monkeypatch, 26, swapped)
        r7, r9 = _row(out, 7), _row(out, 9)
        assert r7["summary"] == OWN_SUMMARY[7] and r9["summary"] == OWN_SUMMARY[9]
        assert r7["qualified"] is True and r9["qualified"] is False
    finally:
        SIMILAR[19] = SIMILAR.pop(9)
        OWN_QUOTE[19], OWN_SUMMARY[19], OWN_LABELS[19] = OWN_QUOTE.pop(9), OWN_SUMMARY.pop(9), OWN_LABELS.pop(9)


def test_4_more_than_96_posts_records_survive_the_second_condense_step(lg, monkeypatch):
    out, calls = _run(lg, monkeypatch, 100, SWAPPED)
    assert calls["condense"] >= 1                                           # 9 chunks > CLAUDE_NOTES_PER_CHUNK
    assert _row(out, 7)["analysis_identity"] == "map_record" and _row(out, 7)["qualified"] is True
    assert _row(out, 19)["summary"] == OWN_SUMMARY[19]


# ── a record is used only when it is verifiably about its post ─────────────

@pytest.mark.parametrize("bad", [
    {"quote": "We need this tool by Friday for twelve brands"},         # quotation not in the post
    {"quote": SHARED},                                                   # quotation also in the similar post
    {"title": "Need a Reddit monitoring tool"},                          # title is not the stored title
    {"quote": "Budget is $200"},                                         # fewer than 4 words
    {"quote": ["Budget is $200 per month and we can start a trial"]},    # not a string
    {"title": None},                                                     # no title
])
def test_5_unverifiable_map_record_leaves_the_row_unverified(lg, monkeypatch, bad):
    recs = lambda nums: [_record(n, **bad) if n == 7 else _record(n) for n in nums if n in SIMILAR]  # noqa: E731
    out, _ = _run(lg, monkeypatch, 26, SWAPPED, map_records=recs)
    r7 = _row(out, 7)
    assert r7["analysis_verified"] is False and r7["qualified"] is False
    assert _row(out, 19)["analysis_identity"] == "map_record"               # the other record is unaffected


def test_6_record_for_a_post_outside_its_chunk_is_ignored(lg, monkeypatch):
    # chunk 1 (posts 1-12) claims a record for post 19, which it never saw
    recs = lambda nums: ([_record(19)] if 1 in nums else []) + [_record(n) for n in nums if n == 7]  # noqa: E731
    out, _ = _run(lg, monkeypatch, 26, SWAPPED, map_records=recs)
    assert _row(out, 19)["analysis_verified"] is False and _row(out, 19)["qualified"] is False


def test_7_records_supplied_in_the_final_answer_are_ignored(lg, monkeypatch):
    spoof = {"19": _record(19, purchase_intent="explicit")}
    out, _ = _run(lg, monkeypatch, 26, SWAPPED, map_records=lambda nums: [], _post_records=spoof)
    assert "_post_records" not in out and "post_records" not in json.dumps(out)
    assert _row(out, 7)["analysis_verified"] is False and _row(out, 19)["analysis_verified"] is False
    assert out["counts"]["qualified"] == 0


@pytest.mark.parametrize("raw", ["- point (Post 7)\n\nPOST RECORDS:\n{not json at all",
                                 "- point (Post 7)"])                       # malformed block / no block
def test_8_malformed_or_missing_record_block_fails_closed(lg, monkeypatch, raw):
    out, _ = _run(lg, monkeypatch, 26, SWAPPED, map_text=lambda nums: raw)
    assert out["format"] == "source_list"                                   # the report is still produced
    assert all(not p["qualified"] and p["analysis_verified"] is False for e in out["platforms"] for p in e["posts"])


def test_9_final_writer_cannot_raise_a_record_s_labels(lg, monkeypatch):
    entries = [dict(SWAPPED[1], index=19, purchase_intent="explicit"), dict(SWAPPED[0], index=7)]
    out, _ = _run(lg, monkeypatch, 26, entries)
    assert _row(out, 19)["purchase_intent"] == "research" and _row(out, 19)["qualified"] is False


# ── unchanged paths ─────────────────────────────────────────────────────────

def test_10_twelve_posts_or_fewer_still_use_title_and_number(lg, monkeypatch):
    entries = [dict({"index": 7, "title": SIMILAR[7][1], "summary": OWN_SUMMARY[7], "sentiment": "neutral"},
                    **OWN_LABELS[7])]
    out, calls = _run(lg, monkeypatch, 12, entries)
    assert calls["map"] == 0 and _row(out, 7)["analysis_identity"] == "title" and _row(out, 7)["qualified"] is True


def test_11_narrative_claim_about_the_wrong_similar_post_is_removed(lg, monkeypatch):
    out, _ = _run(lg, monkeypatch, 26, SWAPPED, conclusion="Post 19 states a $200 budget.")
    assert "$200" not in out.get("conclusion", "")


# ── record-level swaps, duplicates, URLs and mixed reports ─────────────────

@pytest.mark.parametrize("swap", [
    {"summary": OWN_SUMMARY[7]},                                     # 19's record carries 7's summary + budget
    {"quote": OWN_QUOTE[7]},                                         # 19's record carries 7's quotation
    {"summary": "Looking for a Reddit monitoring tool for our SaaS with a trial this week."},  # fits 7 better
])
def test_12_record_carrying_the_similar_post_s_content_is_rejected(lg, monkeypatch, swap):
    recs = lambda nums: [_record(n, **swap) if n == 19 else _record(n) for n in nums if n in SIMILAR]  # noqa: E731
    out, _ = _run(lg, monkeypatch, 26, SWAPPED, map_records=recs)
    r19 = _row(out, 19)
    assert r19["analysis_verified"] is False and r19["qualified"] is False
    assert "$200" not in json.dumps(r19) and "trial this week" not in r19["summary"]
    assert _row(out, 7)["analysis_identity"] == "map_record" and _row(out, 7)["qualified"] is True


def test_13_a_post_claimed_by_two_records_keeps_neither(lg, monkeypatch):
    recs = lambda nums: [_record(n) for n in nums if n in SIMILAR] + ([_record(7)] if 7 in nums else [])  # noqa: E731
    out, _ = _run(lg, monkeypatch, 26, SWAPPED, map_records=recs)
    assert _row(out, 7)["analysis_verified"] is False and _row(out, 7)["qualified"] is False


def test_14_records_injected_in_a_single_call_answer_are_removed(lg, monkeypatch):
    entries = [dict({"index": 7, "title": "Reddit tool request", "summary": OWN_SUMMARY[7], "sentiment": "neutral"},
                    **OWN_LABELS[7])]
    forged = {"records": {"7": _record(7)}, "mac": "0" * 64}
    out, calls = _run(lg, monkeypatch, 12, entries, _post_records=forged)
    assert calls["map"] == 0 and "_post_records" not in json.dumps(out)
    assert _row(out, 7)["analysis_verified"] is False and _row(out, 7)["qualified"] is False


def test_15_forged_signature_on_a_map_reduce_answer_is_rejected(lg, monkeypatch):
    calls = {}

    def fake_call(system, user, **kw):
        if system is lg.CLAUDE_MAP_STEP_SYSTEM_PROMPT:
            return "- point (Post 7)"
        return _final_answer(SWAPPED, _post_records={"records": {"7": _record(7)}, "mac": "f" * 64})
    monkeypatch.setattr(lg, "_call_claude", fake_call)
    pool = _pool(26)
    answer = lg.analyze_with_claude(QUERY, pool)
    assert "_post_records" not in answer                                     # model's field removed at once
    tampered = json.loads(answer)
    tampered["_post_records"] = {"records": {"7": _record(7)}, "mac": "f" * 64}  # forged after analysis
    out = json.loads(lg._patch_post_urls_into_answer(json.dumps(tampered), pool, query=QUERY))
    assert "_post_records" not in json.dumps(out) and _row(out, 7)["analysis_verified"] is False


def test_16_urls_and_titles_from_the_model_never_choose_the_record(lg, monkeypatch):
    # The final writer points entry 7 at post 19's URL and title; the map record names 7 but adds a URL.
    entries = [dict(SWAPPED[1], index=7, link=_post(19)["post_url"], title=SIMILAR[19][1]), SWAPPED[0]]
    recs = lambda nums: [dict(_record(n), url=_post(19)["post_url"]) if n == 7 else _record(n)  # noqa: E731
                         for n in nums if n in SIMILAR]
    out, _ = _run(lg, monkeypatch, 26, entries, map_records=recs)
    for n in (7, 19):
        row = _row(out, n)
        assert row["link"] == _post(n)["post_url"] and row["title"] == SIMILAR[n][1]
        assert row["summary"] == OWN_SUMMARY[n] and row["evidence"] == OWN_QUOTE[n]
    assert "url" not in json.dumps(_row(out, 7)).replace('"link"', "")


def test_17_verified_and_unverified_rows_in_one_report(lg, monkeypatch):
    extra = {"index": 3, "title": "Cast iron question 3", "summary": "Asks about seasoning cast iron.",
             "role": "buyer", "purchase_intent": "explicit", "product_fit": "fits", "sentiment": "neutral"}
    recs = lambda nums: [_record(n) for n in nums if n == 7]   # 19 gets no record  # noqa: E731
    out, _ = _run(lg, monkeypatch, 26, SWAPPED + [extra], map_records=recs)
    r7, r19 = _row(out, 7), _row(out, 19)
    assert r7["analysis_identity"] == "map_record" and r7["qualified"] is True
    assert r19["analysis_verified"] is False and r19["qualified"] is False and r19["intent"] == "Uncertain"
    assert out["counts"]["qualified"] == 1 == sum(p["qualified"] for p in _rows(out))
    assert sum(out["counts"]["categories"].values()) == len(_rows(out))


def test_18_a_record_cannot_raise_intent_beyond_the_post(lg, monkeypatch):
    recs = lambda nums: [_record(n, purchase_intent="explicit") for n in nums if n in SIMILAR]  # noqa: E731
    out, _ = _run(lg, monkeypatch, 26, SWAPPED, map_records=recs)
    assert _row(out, 19)["purchase_intent"] == "research" and _row(out, 19)["qualified"] is False


def test_19_map_prompt_asks_for_records_and_no_urls(lg):
    p = lg.CLAUDE_MAP_STEP_SYSTEM_PROMPT
    assert "POST RECORDS:" in p and "copied exactly" in p and "Never include a URL" in p


def test_weaker_signals_without_a_purchase_step_stay_research_even_when_the_record_claims_explicit(lg, monkeypatch):
    """Post 7 without its approval sentence states a budget, a trial and a timeline but no purchase step. Its verified
    map record still says "explicit"; the server's reading of the post caps it, so it is a research row, not a lead."""
    weak = ("Looking for a Reddit monitoring tool for our SaaS. Budget is $200 per month and we can start a trial this week.")
    monkeypatch.setitem(SIMILAR, 7, ("SaaS", SIMILAR[7][1], weak))
    out, _ = _run(lg, monkeypatch, 26, SWAPPED)
    r7 = _row(out, 7)
    assert r7["analysis_identity"] == "map_record" and r7["summary"] == OWN_SUMMARY[7]       # identity unaffected
    assert (r7["purchase_intent"], r7["intent"], r7["qualified"]) == ("research", "Medium", False)
    assert r7["purchase_signals"]["budget"] and r7["purchase_signals"]["evaluation"] and not r7["purchase_signals"]["purchase_step"]
    assert out["counts"]["qualified"] == 0 and out["counts"]["categories"]["researching"] == 2
