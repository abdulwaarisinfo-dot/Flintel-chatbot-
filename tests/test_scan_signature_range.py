"""
tests/test_scan_signature_range.py — the incremental-rescan signature (logics._scan_signature) and explicit time ranges.
Pure functions plus one existing-harness integration check; no network, no database.

What this shows, and does not: the signature now distinguishes ranges (defence in depth). Today `get_evidence_with_topup`
returns BEFORE any scan state is built when a range is present (tests/test_time_ranges.py proves the cache is bypassed), so
the new argument is not reached on that path; the test below pins that fact so a future change to it is noticed.
"""
import hashlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

from timerange import TimeRange
from tests.test_time_ranges import Harness, _range_docs, lg  # noqa: F401  (the existing harness and fixture)

logics = None            # bound to the harness's logics module by the autouse fixture below
UTC = timezone.utc
END = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
ARGS = (["crm"], ["need a crm"], "reddit", None, "find people looking for a crm", True, "created_utc")


@pytest.fixture(autouse=True)
def _bind_logics(lg):
    global logics
    logics = lg


def window(minutes, end=END, tz="UTC"):
    return TimeRange(start=end - timedelta(minutes=minutes), end=end, tz_name=tz, kind="rolling", phrase=f"last {minutes} minutes")


def legacy(keywords, phrases, platform, since, query, strict, field):
    """The signature exactly as it was computed before this change (MySQL part off)."""
    base = logics._compute_query_signature(keywords, phrases)
    raw = f"{base}|{platform}|{since}|{(query or '').strip().lower()}|{int(bool(strict))}|{field}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_without_a_range_the_signature_is_exactly_what_it_was():
    assert logics._scan_signature(*ARGS) == legacy(*ARGS)
    assert logics._scan_signature(*ARGS, time_range=None) == legacy(*ARGS)
    assert logics._scan_signature(*ARGS, time_range={}) == legacy(*ARGS)           # an empty / unreadable doc is "no range"


def test_a_range_changes_the_signature():
    assert logics._scan_signature(*ARGS, time_range=window(30)) != logics._scan_signature(*ARGS)


def test_30_and_1500_minute_windows_never_share_a_signature():
    a, b = logics._scan_signature(*ARGS, time_range=window(30)), logics._scan_signature(*ARGS, time_range=window(1500))
    assert a != b


def test_same_query_same_boundaries_share_a_signature_in_any_representation():
    s = logics._scan_signature(*ARGS, time_range=window(30))
    assert s == logics._scan_signature(*ARGS, time_range=window(30))
    assert s == logics._scan_signature(*ARGS, time_range=window(30).to_doc(END))                  # the stored-message form
    other_tz = window(30, tz="Asia/Karachi")                                                       # same instants, other label
    assert s == logics._scan_signature(*ARGS, time_range=other_tz)


def test_a_one_second_shift_of_either_boundary_changes_the_signature():
    base = window(30)
    later_end = TimeRange(start=base.start, end=base.end + timedelta(seconds=1), tz_name="UTC", kind="rolling", phrase="x")
    later_start = TimeRange(start=base.start + timedelta(seconds=1), end=base.end, tz_name="UTC", kind="rolling", phrase="x")
    sigs = {logics._scan_signature(*ARGS, time_range=t) for t in (base, later_end, later_start)}
    assert len(sigs) == 3


def test_user_typed_ranges_are_distinguished_by_their_boundaries():
    pk = timezone(timedelta(hours=5))
    today = TimeRange(start=datetime(2026, 11, 1, 0, 0, tzinfo=pk), end=datetime(2026, 11, 2, 0, 0, tzinfo=pk), tz_name="Asia/Karachi",
                      kind="calendar", phrase="today")
    yesterday = TimeRange(start=datetime(2026, 10, 31, 0, 0, tzinfo=pk), end=datetime(2026, 11, 1, 0, 0, tzinfo=pk), tz_name="Asia/Karachi",
                          kind="calendar", phrase="yesterday")
    s_today = logics._scan_signature(*ARGS, time_range=today)
    assert s_today != logics._scan_signature(*ARGS, time_range=yesterday)
    assert s_today == logics._scan_signature(*ARGS, time_range=today.to_doc(END))


def test_other_signature_inputs_still_matter_with_a_range():
    r = window(30)
    assert logics._scan_signature(*ARGS, time_range=r) != logics._scan_signature(["other"], *ARGS[1:], time_range=r)


def test_prepare_scan_state_carries_the_range_into_the_signature(monkeypatch):
    monkeypatch.setattr(logics, "_cfg_value", lambda name, default=None: {"INCREMENTAL_RESCAN_ENABLED": True,
                                                                          "INCREMENTAL_FULL_SCAN_EVERY_N_POLLS": 0}.get(name, default))

    def matcher(topic_key, keywords, scan_state=None, **kw):
        return []
    call = lambda tr: logics._prepare_scan_state(None, "chat1", "topic1", ["crm"], ["need a crm"], "reddit", None, "q", True, matcher, False,
                                                 time_range=tr)
    s30, s1500, none = call(window(30))["sig"], call(window(1500))["sig"], call(None)["sig"]
    assert len({s30, s1500, none}) == 3 and call(window(30))["sig"] == s30


def test_the_explicit_range_path_still_never_builds_scan_state_or_reads_the_cache(lg, monkeypatch):
    """The reason the stale-carry risk is not reachable today: get_evidence_with_topup returns before either."""
    base = datetime.now(UTC).replace(microsecond=0)
    h = Harness(lg, monkeypatch, _range_docs(base), INCREMENTAL_RESCAN_ENABLED=True)
    monkeypatch.setattr(lg, "signals_collection_2", None)
    monkeypatch.setattr(lg, "signals_collection_4", None)

    def boom(*a, **k):
        raise AssertionError("scan state / cache must not be touched when a time range is present")
    monkeypatch.setattr(lg, "_prepare_scan_state", boom)
    monkeypatch.setattr(lg, "get_cached_topic_evidence", boom)
    seen = []
    for minutes in (30, 1500):
        tr = TimeRange(start=base - timedelta(minutes=minutes), end=base, tz_name="UTC", kind="rolling", phrase="t")
        lg.get_evidence_with_topup("chat1", "owner", "topic1", ["ai agents"], 50,
                                   lambda t, k, **kw: seen.append(kw) or [], match_phrases=["x"], time_range=tr.to_doc(base))
    assert [kw["time_range"].end - kw["time_range"].start for kw in seen] == [timedelta(minutes=30), timedelta(minutes=1500)]
