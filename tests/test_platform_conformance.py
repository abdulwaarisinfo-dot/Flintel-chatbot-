"""
tests/test_platform_conformance.py — runs the adapter contract (tests/platform_conformance.py) against every registered fixture.

Today: Reddit only. A new platform adds one AdapterFixture to ADAPTERS; it is NOT enabled by being listed here.
"""
import sys
from datetime import timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import platforms  # noqa: E402
from tests import platform_conformance as pc  # noqa: E402
from tests.test_strict_intent import lg  # noqa: E402,F401


def _reddit_record(n, when, **extra):
    rec = {"title": f"[synthetic] Looking for a CRM {n}", "post_text": "[synthetic] We need a CRM. The budget is approved by finance "
           "and we will subscribe this quarter.", "post_url": f"https://www.reddit.com/r/CRM/comments/abc{n}/post_{n}/",
           "platform": "reddit", "subreddit": "CRM"}
    if when is not None:
        rec["created_utc"] = when
    rec.update(extra)
    return rec


REDDIT = pc.AdapterFixture(
    key="reddit", make_record=_reddit_record,
    equivalent_urls=("https://www.reddit.com/r/CRM/comments/abc1/post_1/", "http://reddit.com/r/CRM/comments/ABC1/post_1",
                     "https://old.reddit.com/r/CRM/comments/abc1/post_1/?utm_source=x#top"),
    other_url="https://www.reddit.com/r/CRM/comments/zzz9/other/", expected_community="CRM", expected_source_id="reddit:abc1")

ADAPTERS = [REDDIT]
IDS = [a.key for a in ADAPTERS]


@pytest.mark.parametrize("fx", ADAPTERS, ids=IDS)
@pytest.mark.parametrize("check", pc.ALL_CHECKS)
def test_adapter_contract(fx, check):
    getattr(pc, check)(fx)


@pytest.mark.parametrize("fx", ADAPTERS, ids=IDS)
def test_adapter_contract_window_filtering(lg, fx):
    pc.check_window_filtering(lg, fx)


@pytest.mark.parametrize("fx", ADAPTERS, ids=IDS)
def test_adapter_contract_cache_isolation(lg, monkeypatch, fx):
    pc.check_cache_isolation(lg, monkeypatch, fx)


def test_only_reddit_is_enabled_and_listing_a_fixture_enables_nothing():
    assert platforms.enabled_keys() == ("reddit",)
    assert [k for k, p in platforms.REGISTRY.items() if p.enabled] == ["reddit"]
    assert set(platforms.REGISTRY) == {"x_twitter", "reddit", "linkedin", "facebook"}        # recognised, not enabled
    assert all(not platforms.REGISTRY[k].enabled for k in ("x_twitter", "linkedin", "facebook"))


def test_the_contract_detects_a_broken_adapter():
    """The checks are not vacuous: an adapter whose URLs do not normalise to one id fails them."""
    bad = pc.AdapterFixture(key="reddit", make_record=_reddit_record,
                            equivalent_urls=("https://www.reddit.com/r/CRM/comments/abc1/", "https://www.reddit.com/r/CRM/comments/abc2/"),
                            other_url="https://www.reddit.com/r/CRM/comments/zzz9/other/", expected_community="CRM")
    with pytest.raises(AssertionError):
        pc.check_source_id(bad)
    wrong_community = pc.AdapterFixture(key="reddit", make_record=_reddit_record, equivalent_urls=REDDIT.equivalent_urls,
                                        other_url=REDDIT.other_url, expected_community="Other")
    with pytest.raises(AssertionError):
        pc.check_community_and_author(wrong_community)
