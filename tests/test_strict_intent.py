"""
tests/test_strict_intent.py
============================
STRICT_INTENT_MODE (accuracy over quantity) and INCREMENTAL_RESCAN_ENABLED.

Offline: no real Mongo, OpenAI/Anthropic or network. Every LLM call is a fake,
every collection an in-memory stub.

A  seller post is excluded on a buyer query
B  mention-only chatter excluded; a specific business request is included
   and ranked on top
C  8 qualified -> exactly 8 returned (no padding)
D  flag off -> old behaviour (padding included) and the prompts are
   byte-identical to the pre-strict ones (sha256 snapshot)
E  summary that does not match the post text is removed
F  URL / title / subreddit / summary come from ONE post; two look-alike
   titles never produce a wrong patch
G  bridge timeout never hands unfiltered posts onward
H  _rank_passing hands the REAL actor/specificity fields to the ranker
I  "we need an agency" stays a buyer, "we offer / DM me" is vetoed
J  incremental: 2nd poll fetches only docs after the watermark
K  incremental: empty delta -> no scoring, no bridge, no interpreter
L  incremental result == full-rescan result (parity), also with new docs
M  a late-ingested old post is caught (by _id watermark, or by the
   periodic full scan for a created_utc watermark)
N  flag off / missing watermark / cache errors -> full scan, never a crash
plus: wait/target rule, stubs never pad, single-platform cap relaxation,
strict-only cache reuse.
"""
import hashlib
import importlib
import importlib.util
import json
import math
import re
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Imported BEFORE any fixture installs module stubs, so they bind the real deps.
import intent_bridge as ib                                  # noqa: E402
from intent_prototype import doc_classifier, schemas        # noqa: E402

# sha256 of the pre-strict prompts (snapshot taken from the code before this
# change). Flag off must reproduce them byte for byte.
ORIG_ANALYSIS_PROMPT_SHA = "7e0ed41d35c2b9ed541cdf02a133f89eb56201ed9561c486cb07472f50b5b1b4"
ORIG_ANALYSIS_PROMPT_LEN = 29993
ORIG_CLASSIFIER_PROMPT_SHA = "6c7adb5977c34d9520e5a1c1abd41bce782d620f1358936c7f21c21cb7278be4"


# ═════════════════════════════════════════════════════════════════════════
# fixtures
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def lg():
    """logics imported with Mongo/HTTP stubs; sys.modules restored afterwards."""
    import os
    os.environ.setdefault("OPENAI_API_KEY", "test-key-strict")
    os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
    os.environ.setdefault("MONGODB_DB", "test_db")

    names = ["database", "flintel", "website_intelligence", "google", "httpx",
             "logics", "config"]
    saved = {n: sys.modules.get(n) for n in names}

    db_stub = types.ModuleType("database")
    for attr in ["db", "jobs_collection", "signals_collection", "signals_collection_2",
                 "signals_collection_4", "google_posts_collection",
                 "topic_evidence_cache_collection", "website_evidence_cache_collection"]:
        setattr(db_stub, attr, MagicMock())
    fi_stub = types.ModuleType("flintel")
    fi_stub.ROUTER_UNFILTERED_ADDENDUM = ""
    fi_stub.GENERIC_PAIN_POINT_INFERENCE_ADDENDUM = ""
    fi_stub.build_google_fallback_answer_context = None
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.AsyncClient = MagicMock()
    httpx_stub.TimeoutException = Exception
    httpx_stub.HTTPStatusError = Exception
    for name, mod in {
        "database": db_stub, "flintel": fi_stub,
        "website_intelligence": types.ModuleType("website_intelligence"),
        "google": types.ModuleType("google"), "httpx": httpx_stub,
    }.items():
        sys.modules[name] = mod
    sys.modules.pop("logics", None)
    sys.modules.pop("config", None)

    mod = importlib.import_module("logics")
    yield mod

    for n, old in saved.items():
        if old is None:
            sys.modules.pop(n, None)
        else:
            sys.modules[n] = old


def _set_cfg(monkeypatch, **kw):
    import config
    for k, v in kw.items():
        monkeypatch.setattr(config, k, v, raising=False)


@pytest.fixture
def real_flintel():
    spec = importlib.util.spec_from_file_location("flintel_real", ROOT / "flintel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═════════════════════════════════════════════════════════════════════════
# bridge helpers (A, B, C, D, G, H, I)
# ═════════════════════════════════════════════════════════════════════════

def _qi(**over):
    plan = {
        "topic_keywords": ["AI agent"], "topic_embedding_query": "ai agent for business",
        "intent_include": ["buyer_demand"],
        "intent_exclude": ["provider_supply", "hiring", "irrelevant"],
        "query_mode": "explicit_intent", "intent_logic": "OR",
    }
    plan.update(over)
    return schemas.normalize_query_intent(plan)


# marker word in the post text -> what the (fake) LLM classifier says
CLASSIFY = {
    "BUYERSTRONG":   dict(intent="buyer_demand", intent_confidence=0.9, actor_type="company",
                          actor_role="buyer", commercial_signal=0.95, specificity=0.9, urgency=0.5),
    "BUYERWEAK":     dict(intent="buyer_demand", intent_confidence=0.9, actor_type="company",
                          actor_role="buyer", commercial_signal=0.45, specificity=0.3, urgency=0.0),
    "SELLERPITCH":   dict(intent="provider_supply", intent_confidence=0.9, actor_type="company",
                          actor_role="seller", commercial_signal=0.8, specificity=0.6),
    "MENTIONONLY":   dict(intent="general_discussion", intent_confidence=0.8, actor_type="unknown",
                          actor_role="unknown", commercial_signal=0.1, specificity=0.1),
    "HIRINGPOST":    dict(intent="hiring", intent_confidence=0.9, actor_type="company",
                          actor_role="seeker", commercial_signal=0.6, specificity=0.5),
    # classifier WRONGLY says buyer for a seller pitch -> deterministic veto must catch it
    "WRONGBUYER":    dict(intent="buyer_demand", intent_confidence=0.9, actor_type="company",
                          actor_role="buyer", commercial_signal=0.7, specificity=0.5),
    # classifier says buyer_demand but the actor is a seller -> actor gate must catch it
    "SELLERACTOR":   dict(intent="buyer_demand", intent_confidence=0.9, actor_type="company",
                          actor_role="seller", commercial_signal=0.7, specificity=0.5),
}


def _fake_claude(system, user, **kw):
    out = []
    for m in re.finditer(r"\[(\d+)\] TITLE: (.*?)\nTEXT: (.*?)(?=\n\n\[\d+\] TITLE:|\Z)", user, re.S):
        idx, title, text = int(m.group(1)), m.group(2), m.group(3)
        blob = f"{title} {text}"
        spec = next((v for k, v in CLASSIFY.items() if k in blob), CLASSIFY["MENTIONONLY"])
        row = dict(spec)
        row.update({"i": idx, "secondary_intent": None, "secondary_confidence": None,
                    "pain_intensity": 0.0, "urgency": spec.get("urgency", 0.0),
                    "geography": None, "industry_hint": None, "ambiguous": False, "noise": False})
        out.append(row)
    return json.dumps(out)


def _post(i, marker, text="", platform="reddit"):
    return {"title": f"{marker} title {i}",
            "post_text": f"{marker} {text} body number {i} lorem ipsum dolor sit amet",
            "post_url": f"https://www.reddit.com/r/x/comments/{i}/p/", "platform": platform,
            "subreddit": "x"}


def _run(monkeypatch, cands, want, *, strict, qi=None, sims=None, timeout=25, extra=None):
    monkeypatch.setattr(ib, "_interpret", lambda q, plan_key=None: qi or _qi())
    cfg = {"INTENT_BRIDGE_ENABLED": True, "STRICT_INTENT_MODE": strict,
           "INTENT_CACHE_ENABLED": False, "INTENT_BRIDGE_TIMEOUT_SECONDS": timeout}
    cfg.update(extra or {})
    with patch("intent_prototype.doc_classifier.claude", side_effect=_fake_claude):
        return ib.rerank_with_intent("find AI agent buyers", cands, want,
                                     topic_sims=sims or [0.8 - 0.001 * i for i in range(len(cands))],
                                     config_overrides=cfg)


# ── A ───────────────────────────────────────────────────────────────────
def test_a_seller_post_excluded_on_buyer_query(monkeypatch):
    cands = [_post(1, "BUYERSTRONG"), _post(2, "SELLERPITCH", "We build AI agents for SMBs"),
             _post(3, "SELLERACTOR"), _post(4, "HIRINGPOST")]
    res = _run(monkeypatch, cands, 10, strict=True)
    urls = [c["post_url"] for c in res]
    assert urls == [cands[0]["post_url"]], urls


# ── B ───────────────────────────────────────────────────────────────────
def test_b_mention_only_excluded_specific_request_included_and_on_top(monkeypatch):
    cands = [_post(1, "MENTIONONLY", "Anyone tried AI agents? Thoughts?"),
             _post(2, "BUYERWEAK", "looking for an AI agent for my shop"),
             _post(3, "BUYERSTRONG", "need WhatsApp AI agent by Q4 budget 10k")]
    sims = [0.9, 0.5, 0.5]            # the mention has the HIGHEST similarity
    res = _run(monkeypatch, cands, 10, strict=True, sims=sims)
    urls = [c["post_url"] for c in res]
    assert cands[0]["post_url"] not in urls                      # chatter gone
    assert urls == [cands[2]["post_url"], cands[1]["post_url"]]  # specific one first


# ── C ───────────────────────────────────────────────────────────────────
def test_c_eight_qualified_returns_eight_without_padding(monkeypatch):
    cands = [_post(i, "BUYERSTRONG" if i < 8 else "MENTIONONLY") for i in range(100)]
    res = _run(monkeypatch, cands, 25, strict=True)
    assert len(res) == 8
    assert all("BUYERSTRONG" in c["title"] for c in res)


# ── D ───────────────────────────────────────────────────────────────────
def test_d_flag_off_keeps_old_padding_behaviour(monkeypatch):
    cands = [_post(i, "BUYERSTRONG" if i < 8 else "MENTIONONLY") for i in range(100)]
    res = _run(monkeypatch, cands, 25, strict=False)
    assert len(res) == 25                     # padded up to the target, as before
    assert sum("BUYERSTRONG" in c["title"] for c in res) == 8


def test_d_flag_off_prompts_are_byte_identical(lg, monkeypatch):
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=False)
    p = lg._analysis_system_prompt()
    assert p is lg.CLAUDE_ANALYSIS_SYSTEM_PROMPT
    assert len(p) == ORIG_ANALYSIS_PROMPT_LEN
    assert hashlib.sha256(p.encode()).hexdigest() == ORIG_ANALYSIS_PROMPT_SHA
    assert doc_classifier.system_prompt(False) == doc_classifier.SYSTEM
    assert hashlib.sha256(doc_classifier.SYSTEM.encode()).hexdigest() == ORIG_CLASSIFIER_PROMPT_SHA
    assert lg._strict_count_note(8) == ""


def test_d_strict_prompt_differs_only_where_intended(lg, monkeypatch):
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=True)
    p = lg._analysis_system_prompt()
    assert p is lg.CLAUDE_ANALYSIS_SYSTEM_PROMPT_STRICT
    assert 'PREFER RELATED SIGNALS OVER "no_results"' in lg.CLAUDE_ANALYSIS_SYSTEM_PROMPT
    assert 'PREFER RELATED SIGNALS OVER "no_results"' not in p
    assert "NO RELATED-SIGNAL PADDING" in p
    assert "never include more than" not in p
    assert "STRICT RULES" in doc_classifier.system_prompt(True)
    assert "qualified results" in lg._strict_count_note(8)


def test_d_config_defaults(monkeypatch):
    import config
    for k in ("STRICT_INTENT_MODE",):
        monkeypatch.delenv(k, raising=False)
    assert "STRICT_INTENT_MODE" in config.__all__
    assert "INCREMENTAL_RESCAN_ENABLED" in config.__all__
    for name in ("STRICT_WAIT_SECONDS", "INCREMENTAL_WATERMARK_FIELD", "STRICT_SUMMARY_MIN_OVERLAP",
                 "STRICT_TITLE_MATCH_MIN_JACCARD", "INCREMENTAL_OVERLAP_SECONDS",
                 "INCREMENTAL_FULL_SCAN_EVERY_N_POLLS"):
        assert name in config.__all__
    assert config.STRICT_WAIT_SECONDS == 75


# ── G ───────────────────────────────────────────────────────────────────
def test_g_timeout_never_forwards_unfiltered_posts(monkeypatch):
    cands = [_post(i, "BUYERSTRONG" if i < 3 else "MENTIONONLY") for i in range(100)]

    def slow_classify(posts, qi, cfg):
        first = posts[:5]
        items = [{"post_url": p["post_url"], "intents": ["buyer_demand"] if i < 3 else ["general_discussion"],
                  "confidence": 0.9, "intent": "buyer_demand" if i < 3 else "general_discussion",
                  "commercial_signal": 0.9, "urgency": 0.0, "pain_intensity": 0.0,
                  "actor_type": "company", "actor_role": "buyer", "specificity": 0.8,
                  "ambiguous": False, "noise": False}
                 for i, p in enumerate(first)]
        cfg["_progress"](items)          # these 5 were classified before the timeout
        time.sleep(3)
        return items

    monkeypatch.setattr(ib, "_classify_parallel", slow_classify)
    res = _run(monkeypatch, cands, 25, strict=True, timeout=1)
    assert [c["post_url"] for c in res] == [c["post_url"] for c in cands[:3]]
    assert len(res) < 100


def test_g_timeout_with_nothing_classified_returns_empty(monkeypatch):
    cands = [_post(i, "MENTIONONLY") for i in range(100)]

    def very_slow(posts, qi, cfg):
        time.sleep(3)
        return []

    monkeypatch.setattr(ib, "_classify_parallel", very_slow)
    assert _run(monkeypatch, cands, 25, strict=True, timeout=1) == []


def test_g_flag_off_timeout_still_returns_originals(monkeypatch):
    cands = [_post(i, "MENTIONONLY") for i in range(100)]
    monkeypatch.setattr(ib, "_classify_parallel", lambda p, q, c: (time.sleep(3), [])[1])
    assert len(_run(monkeypatch, cands, 25, strict=False, timeout=1)) == 100


def test_g_interpreter_failure_returns_empty_in_strict(monkeypatch):
    cands = [_post(i, "BUYERSTRONG") for i in range(5)]
    monkeypatch.setattr(ib, "_interpret", lambda q, plan_key=None: ib._FAIL)
    cfg = {"STRICT_INTENT_MODE": True, "INTENT_CACHE_ENABLED": False}
    assert ib.rerank_with_intent("find buyers", cands, 5, config_overrides=cfg) == []


# ── H ───────────────────────────────────────────────────────────────────
def test_h_rank_passing_passes_real_actor_and_specificity_fields():
    qi = _qi(actor_direction_filter=["company_buying"], actor_type_filter=["company"],
             min_commercial_signal=0.4)

    def cls(comm, spec, role="buyer"):
        return {"intent": "buyer_demand", "intents": ["buyer_demand"], "confidence": 0.9,
                "commercial_signal": comm, "urgency": 0.0, "pain_intensity": 0.0,
                "actor_type": "company", "actor_role": role, "specificity": spec,
                "secondary_intent": None, "secondary_confidence": None,
                "ambiguous": False, "noise": False}

    def cand(u):
        return {"title": u, "post_text": "x " * 20, "post_url": u, "platform": "reddit"}

    passing = [(cand("weak_high_sim"), 0.80, cls(0.05, 0.1)),
               (cand("mid_a"), 0.70, cls(0.50, 0.4)),
               (cand("strong_buyer"), 0.55, cls(0.95, 0.9)),
               (cand("cached_buyer"), 0.52, cls(0.70, 0.6)),
               (cand("seller_actor"), 0.51, cls(0.90, 0.8, role="seller"))]
    inp = [c["post_url"] for c, _, _ in passing]

    strict_out = [c["post_url"] for c in ib._rank_passing(passing, qi, strict=True)]
    assert len(strict_out) > 0                                  # kept > 0 (was 0 of 5)
    assert "weak_high_sim" not in strict_out                    # below min_commercial_signal
    assert "seller_actor" not in strict_out                     # actor_direction gate is final
    assert strict_out[0] == "strong_buyer"                      # order changed vs similarity order
    assert strict_out != inp

    legacy_out = [c["post_url"] for c in ib._rank_passing(passing, qi)]
    assert legacy_out == inp                                    # old behaviour: gates undone


# ── I ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,vetoed", [
    ("We build AI agents for SMBs, DM me", True),
    ("I help businesses with AI automation. Looking for clients", True),
    ("Our agency offers AI automation. Book a free call", True),
    ("we need an agency", False),
    ("We need an agency to build an AI agent, DM me recommendations", False),
    ("looking for a developer to build an AI agent for our clinic", False),
    ("Anyone tried AI agents? Thoughts?", False),
])
def test_i_seller_marker_veto_rules(text, vetoed):
    assert schemas.seller_marker_veto("", text) is vetoed


def test_i_pipeline_veto_only_hits_sellers(monkeypatch):
    cands = [
        {**_post(1, "WRONGBUYER", "We build AI agents for SMBs, DM me")},
        {**_post(2, "WRONGBUYER", "We need an agency to build an AI agent for our store")},
    ]
    res = _run(monkeypatch, cands, 10, strict=True)
    assert [c["post_url"] for c in res] == [cands[1]["post_url"]]


# ═════════════════════════════════════════════════════════════════════════
# result quality (E, F)
# ═════════════════════════════════════════════════════════════════════════

URL_A = "https://www.reddit.com/r/AI_Agents/comments/aaa111/need_ai_agent_for_clinic_bookings/"
URL_B = "https://www.reddit.com/r/AI_Agents/comments/bbb222/need_ai_agent_for_clinic_billing/"
URL_C = "https://www.reddit.com/r/sales/comments/ccc333/crm_pain/"


def _signals():
    return [
        {"title": "Need AI agent for clinic bookings",
         "post_text": "Our dental clinic needs an AI agent to handle bookings and reminders by phone. Budget around 500 per month.",
         "post_url": URL_A, "platform": "reddit", "subreddit": "AI_Agents"},
        {"title": "Need AI agent for clinic billing",
         "post_text": "Looking for an AI agent that automates invoice follow-ups and billing questions for our clinic.",
         "post_url": URL_B, "platform": "reddit", "subreddit": "AI_Agents"},
        {"title": "CRM pain",
         "post_text": "Our sales team hates the CRM, data entry takes hours every week.",
         "post_url": URL_C, "platform": "reddit", "subreddit": "sales"},
    ]


def _answer(posts):
    return json.dumps({"format": "source_list", "executive_summary": "x",
                       "platforms": [{"platform": "reddit", "total_analyzed": len(posts),
                                      "shown_count": len(posts), "posts": posts}]})


def _posts_of(answer):
    return json.loads(answer)["platforms"][0]["posts"]


def test_e_summary_that_does_not_match_the_post_is_removed(lg, monkeypatch):
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=True)
    ans = _answer([
        {"index": 1, "source": "unknown", "title": "(no title)",
         "summary": "Dental clinic needs AI agent for phone bookings and reminders", "sentiment": "neutral"},
        {"index": 2, "source": "unknown", "title": "Need AI agent for clinic billing",
         "summary": "Startup Zentora raised 12 million for crypto trading bots", "sentiment": "neutral"},
    ])
    posts = _posts_of(lg._patch_post_urls_into_answer(ans, _signals()))
    assert posts[0]["summary"].startswith("Dental clinic")
    assert posts[1]["summary"] == "Summary unavailable"
    assert posts[1]["link"] == URL_B                       # identity kept, bad summary dropped


def test_e_invented_number_or_name_in_summary_is_removed(lg, monkeypatch):
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=True)
    ans = _answer([{"index": 1, "title": "Need AI agent for clinic bookings",
                    "summary": "Clinic needs AI agent for bookings with a 50000 dollar budget at Acme",
                    "sentiment": "neutral"}])
    posts = _posts_of(lg._patch_post_urls_into_answer(ans, _signals()))
    assert posts[0]["summary"] == "Summary unavailable"


# ── F ───────────────────────────────────────────────────────────────────
def test_f_fields_come_from_one_post_and_lookalike_titles_dont_cross(lg, monkeypatch):
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=True)
    # the model's own link is wrong and the index is missing: only the title
    # distinguishes "bookings" from "billing"
    ans = _answer([
        {"title": "Need AI agent for clinic billing", "link": "https://evil.example/x",
         "source": "wrongsub", "summary": "Looking for AI agent automating invoice follow-ups and billing for clinic",
         "sentiment": "neutral"},
        {"title": "Need AI agent for clinic bookings", "source": "x",
         "summary": "Dental clinic needs AI agent for phone bookings and reminders", "sentiment": "neutral"},
    ])
    posts = _posts_of(lg._patch_post_urls_into_answer(ans, _signals()))
    assert [p["link"] for p in posts] == [URL_B, URL_A]
    assert [p["source"] for p in posts] == ["AI_Agents", "AI_Agents"]
    assert posts[0]["title"] == "Need AI agent for clinic billing"
    assert "billing" in posts[0]["summary"] and "bookings" in posts[1]["summary"]


def test_f_conflicting_index_and_title_resolved_by_summary_or_dropped(lg, monkeypatch):
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=True)
    # index 1 (bookings) but title says billing and the summary is about billing
    ans = _answer([{"index": 1, "title": "Need AI agent for clinic billing",
                    "summary": "Looking for AI agent automating invoice follow-ups and billing for clinic",
                    "sentiment": "neutral"}])
    p = _posts_of(lg._patch_post_urls_into_answer(ans, _signals()))
    assert p[0]["link"] == URL_B
    # nothing supports either -> the post is not shown at all
    ans2 = _answer([{"index": 1, "title": "Need AI agent for clinic billing",
                     "summary": "Quantum widgets are trending among teenagers", "sentiment": "neutral"}])
    out = lg._patch_post_urls_into_answer(ans2, _signals())
    assert json.loads(out)["format"] == "no_results"


def test_f_unconfident_post_is_removed_not_linkless(lg, monkeypatch):
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=True)
    ans = _answer([
        {"index": 1, "title": "Need AI agent for clinic bookings",
         "summary": "Dental clinic needs AI agent for phone bookings", "sentiment": "neutral"},
        {"title": "A post that does not exist anywhere", "summary": "completely unrelated topic words here",
         "sentiment": "neutral"},
    ])
    out = json.loads(lg._patch_post_urls_into_answer(ans, _signals()))
    posts = out["platforms"][0]["posts"]
    assert len(posts) == 1 and posts[0]["link"] == URL_A
    assert out["platforms"][0]["shown_count"] == 1


def test_f_cards_equal_the_posts_in_the_answer(lg, monkeypatch):
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=True)
    signals = _signals()
    ans = _answer([{"index": 2, "title": "Need AI agent for clinic billing",
                    "summary": "Looking for AI agent automating invoice follow-ups and billing", "sentiment": "neutral"}])
    patched = lg._patch_post_urls_into_answer(ans, signals)
    _text, cards = lg._finalize_answer_and_results(patched, signals)
    assert [c["post_url"] for c in cards] == [URL_B]


def test_f_flag_off_patching_is_the_old_permissive_one(lg, monkeypatch):
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=False)
    ans = _answer([{"index": 1, "title": "(no title)", "summary": "clinic owner wants Kubernetes migration with 40 engineers",
                    "sentiment": "neutral"}])
    out = lg._patch_post_urls_into_answer(ans, _signals())
    assert _posts_of(out)[0].get("link") == URL_A      # old behaviour: index accepted as-is
    _t, cards = lg._finalize_answer_and_results(out, _signals())
    assert len(cards) == 3                              # all matched posts, as before


# ═════════════════════════════════════════════════════════════════════════
# wait rule, stubs, cap
# ═════════════════════════════════════════════════════════════════════════

def test_wait_rule_w2_w3(lg, monkeypatch):
    _set_cfg(monkeypatch, STRICT_WAIT_SECONDS=75)
    f = lg.strict_wait_done
    assert f(8, 25, 2.0, True, 360) is True          # W2: scan complete, 8 qualify -> answer now
    assert f(8, 25, 2.0, False, 360) is False        # scan still running -> keep waiting
    assert f(8, 25, 76.0, False, 360) is True        # W3: hard cap
    assert f(0, 25, 10.0, True, 360) is False        # nothing yet: wait for ingestion
    assert f(0, 25, 75.0, True, 360) is True
    assert f(25, 25, 0.1, False, 360) is True        # target reached
    assert f(8, 25, 50.0, False, 40) is True         # RESPONSE_TIMEOUT stays the ceiling


def test_stubs_never_pad_in_strict(real_flintel):
    sig = [{"title": "t", "post_text": "x", "post_url": "u1", "platform": "reddit"}]
    stubs = [{"title": "r/a", "post_text": None, "post_url": f"s{i}", "platform": "reddit", "google_rank": i}
             for i in range(5)]
    assert len(real_flintel.merge_matched_and_google_results(sig, stubs, max_total=25)) == 6
    out = real_flintel.merge_matched_and_google_results(sig, stubs, max_total=25, fill_with_stubs=False)
    assert [m["post_url"] for m in out] == ["u1"]


def test_single_platform_detection(lg):
    f = lg._single_platform_query
    assert f("Find 25 Reddit posts from people looking for AI agents", "all") is True
    assert f("find leads", "reddit") is True
    assert f("find leads on reddit and linkedin", "all") is False
    assert f("find leads", "all") is False


# ═════════════════════════════════════════════════════════════════════════
# retrieval harness (strict cap, J-N)
# ═════════════════════════════════════════════════════════════════════════

def _vec(sim):
    return [sim, math.sqrt(max(0.0, 1 - sim * sim)), 0.0, 0.0]


def _oid_at(dt):
    from bson import ObjectId
    return ObjectId.from_datetime(dt)


def _mkdoc(n, sim, *, inserted, created=None, platform="reddit"):
    return {"_id": _oid_at(inserted), "post_url": f"https://www.reddit.com/r/t/comments/{n}/p/",
            "title": f"post {n}", "post_text": f"body of post {n} about ai agents",
            "platform": platform, "subreddit": "t", "embedding": _vec(sim),
            "created_utc": created or inserted}


def _collect_clauses(q, out):
    if isinstance(q, dict):
        for k, v in q.items():
            if k == "$and":
                for sub in v:
                    _collect_clauses(sub, out)
            else:
                out.append((k, v))


class _Cursor:
    def __init__(self, docs): self._d = list(docs)
    def sort(self, *a, **k): return self
    def batch_size(self, n): return self
    def limit(self, n): return _Cursor(self._d[:n])
    def __iter__(self): return iter(self._d)


class FakeSignals:
    """Evaluates the clauses get_matched_signals() actually sends."""
    def __init__(self, docs): self.docs = list(docs); self.returned = []; self.calls = 0

    def find(self, query, projection=None):
        self.calls += 1
        clauses = []
        _collect_clauses(query, clauses)
        sel = list(self.docs)
        for k, v in clauses:
            if k == "_id" and isinstance(v, dict) and "$gt" in v:
                sel = [d for d in sel if d["_id"] > v["$gt"]]
            elif k in ("created_utc", "ingested_at") and isinstance(v, dict) and "$gte" in v:
                sel = [d for d in sel if d.get(k) is not None and d[k] >= v["$gte"]]
        sel = [d for d in sel if d.get("embedding")]
        sel.sort(key=lambda d: d["created_utc"], reverse=True)
        self.returned.append(len(sel))
        out = []
        for d in sel:
            d = dict(d)
            if projection is not None and projection.get("_id") == 0:
                d.pop("_id", None)
            out.append(d)
        return _Cursor(out)


class FakeCache:
    def __init__(self): self.docs = {}; self.writes = 0; self.fail_find = False; self.fail_update = False

    def find_one(self, flt, proj=None):
        if self.fail_find:
            raise RuntimeError("cache read boom")
        d = self.docs.get((flt["chat_id"], flt["topic_key"]))
        return json.loads(json.dumps(d, default=_jsonable), object_hook=_unjson) if d else None

    def update_one(self, flt, upd, upsert=False):
        if self.fail_update:
            raise RuntimeError("cache write boom")
        key = (flt["chat_id"], flt["topic_key"])
        if key not in self.docs:
            if not upsert:
                return
            self.docs[key] = dict(upd.get("$setOnInsert", {}))
        self.docs[key].update(upd.get("$set", {}))
        self.writes += 1


def _jsonable(o):
    if isinstance(o, datetime):
        return {"__dt__": o.isoformat()}
    raise TypeError(type(o))


def _unjson(d):
    if "__dt__" in d:
        return datetime.fromisoformat(d["__dt__"])
    return d


class Harness:
    def __init__(self, lg, monkeypatch, docs, **cfg):
        self.lg = lg
        self.coll = FakeSignals(docs)
        self.cache = FakeCache()
        self.embed_calls = 0
        self.rerank_calls = 0
        monkeypatch.setattr(lg, "signals_collection", self.coll)
        monkeypatch.setattr(lg, "topic_evidence_cache_collection", self.cache)
        monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_CANDIDATE_POOL", 0)
        monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_RECENCY_POOL", 0)
        monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_MAX_SCAN", 0)
        monkeypatch.setattr(lg, "SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD", 0.35)
        monkeypatch.setattr(lg, "LAZY_EMBED_ENABLED", False)

        def _embed(texts):
            self.embed_calls += 1
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]
        monkeypatch.setattr(lg, "generate_query_embeddings_batch", _embed)
        base = {"STRICT_INTENT_MODE": False, "INCREMENTAL_RESCAN_ENABLED": True,
                "INCREMENTAL_WATERMARK_FIELD": "", "INTENT_BRIDGE_ENABLED": False,
                "INCREMENTAL_FULL_SCAN_EVERY_N_POLLS": 0, "INCREMENTAL_OVERLAP_SECONDS": 90}
        base.update(cfg)
        _set_cfg(monkeypatch, **base)
        # polls are counted per (chat, topic) in-process: start clean
        lg._INC_POLLS.clear()

    def install_bridge(self, monkeypatch, key=lambda c: c["title"]):
        def fake_rerank(user_query, candidates, limit, topic_sims=None, plan_key=None):
            self.rerank_calls += 1
            return sorted(candidates, key=key)
        monkeypatch.setattr(ib, "rerank_with_intent", fake_rerank)

    def poll(self, chat="chat1", topic="topic1", need=50, query="find ai agent buyers"):
        return self.lg.get_evidence_with_topup(
            chat, "owner", topic, ["ai agents"], need, self.lg.get_matched_signals,
            match_phrases=["need an ai agent"], user_query=query,
        )


NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(hours=3)


def _initial_docs():
    return [_mkdoc(i, s, inserted=OLD - timedelta(minutes=10 * i))
            for i, s in enumerate([0.95, 0.90, 0.80, 0.70, 0.60, 0.50], start=1)]


def _urls(posts):
    return [p["post_url"] for p in posts]


# ── cap relaxation (A8) ─────────────────────────────────────────────────
def test_strict_single_platform_cap_relaxed(lg, monkeypatch):
    docs = [_mkdoc(i, 0.9 - i * 0.001, inserted=OLD - timedelta(seconds=i)) for i in range(60)]
    h = Harness(lg, monkeypatch, docs, INCREMENTAL_RESCAN_ENABLED=False)
    h.install_bridge(monkeypatch)
    q = "Find 25 Reddit posts from people looking for AI agents"

    _set_cfg(monkeypatch, STRICT_INTENT_MODE=False, INTENT_BRIDGE_ENABLED=True)
    off = lg.get_matched_signals("t", ["ai agents"], limit=25, user_query=q, chat_id="c",
                                 match_phrases=["x"])
    assert len(off) == 12                                   # historical per-platform cap

    _set_cfg(monkeypatch, STRICT_INTENT_MODE=True)
    on = lg.get_matched_signals("t", ["ai agents"], limit=25, user_query=q, chat_id="c",
                                match_phrases=["x"])
    assert len(on) == 25


# ── J ───────────────────────────────────────────────────────────────────
def test_j_second_poll_fetches_only_docs_after_watermark(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _initial_docs())
    first = h.poll()
    assert h.coll.returned == [6]                           # full scan
    new = [_mkdoc(100, 0.99, inserted=NOW), _mkdoc(101, 0.75, inserted=NOW)]
    h.coll.docs += new
    second = h.poll()
    assert h.coll.returned == [6, 3]                        # delta only (2 new + 1 overlap re-read)
    assert set(_urls(second)) == set(_urls(first)) | set(_urls(new))


# ── K ───────────────────────────────────────────────────────────────────
def test_k_empty_delta_skips_scoring_bridge_and_interpreter(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _initial_docs(), STRICT_INTENT_MODE=True)
    h.install_bridge(monkeypatch)
    interp = MagicMock(side_effect=AssertionError("interpreter must not run on an empty delta"))
    monkeypatch.setattr(ib, "_interpret", interp)
    first = h.poll()
    assert h.rerank_calls == 1 and h.embed_calls == 1
    writes = h.cache.writes

    second = h.poll()                                       # nothing new arrived
    assert h.coll.returned == [6, 1]                        # only the overlap re-read
    assert h.embed_calls == 1                               # no query embedding either
    assert h.rerank_calls == 1                              # bridge not run
    assert interp.call_count == 0
    assert _urls(second) == _urls(first)
    assert h.cache.writes == writes                         # no needless write


def test_k_new_docs_below_threshold_also_skip_bridge(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _initial_docs(), STRICT_INTENT_MODE=True)
    h.install_bridge(monkeypatch)
    first = h.poll()
    h.coll.docs.append(_mkdoc(200, 0.10, inserted=NOW))     # below the 0.35 threshold
    second = h.poll()
    assert h.rerank_calls == 1
    assert _urls(second) == _urls(first)


# ── L ───────────────────────────────────────────────────────────────────
def _scenario(lg, monkeypatch, *, incremental, bridge):
    h = Harness(lg, monkeypatch, _initial_docs(), INCREMENTAL_RESCAN_ENABLED=incremental,
                INTENT_BRIDGE_ENABLED=bridge)
    if bridge:
        h.install_bridge(monkeypatch, key=lambda c: c["title"])
    h.poll()
    h.coll.docs += [_mkdoc(300, 0.99, inserted=NOW), _mkdoc(301, 0.36, inserted=NOW),
                    _mkdoc(302, 0.20, inserted=NOW), _mkdoc(303, 0.72, inserted=NOW)]
    res = h.poll()
    return h, res


@pytest.mark.parametrize("bridge", [False, True])
def test_l_incremental_equals_full_rescan(lg, monkeypatch, bridge):
    h_inc, inc = _scenario(lg, monkeypatch, incremental=True, bridge=bridge)
    assert h_inc.coll.returned[-1] == 5                     # really was a delta
    h_full, full = _scenario(lg, monkeypatch, incremental=False, bridge=bridge)
    assert h_full.coll.returned[-1] == 10                   # really was a full scan
    assert _urls(inc) == _urls(full)                        # same posts, same order
    # and a brand-new chat that never saw the cache sees the same SET of posts
    h_new = Harness(lg, monkeypatch, h_full.coll.docs, INCREMENTAL_RESCAN_ENABLED=False,
                    INTENT_BRIDGE_ENABLED=bridge)
    if bridge:
        h_new.install_bridge(monkeypatch)
    assert set(_urls(h_new.poll("other_chat"))) == set(_urls(inc))


# ── M ───────────────────────────────────────────────────────────────────
def test_m_late_ingested_old_post_caught_by_id_watermark(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _initial_docs())
    h.poll()
    late = _mkdoc(400, 0.97, inserted=NOW, created=NOW - timedelta(days=9))   # old post, inserted just now
    h.coll.docs.append(late)
    res = h.poll()
    assert late["post_url"] in _urls(res)
    assert h.coll.returned[-1] == 2                         # late doc + overlap re-read


def test_m_created_utc_watermark_misses_it_but_periodic_full_scan_catches_it(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _initial_docs(), INCREMENTAL_WATERMARK_FIELD="created_utc",
                INCREMENTAL_FULL_SCAN_EVERY_N_POLLS=3)
    h.poll()                                                           # poll 1: full
    late = _mkdoc(401, 0.97, inserted=NOW, created=NOW - timedelta(days=9))
    h.coll.docs.append(late)
    second = h.poll()                                                  # poll 2: delta by created_utc
    assert late["post_url"] not in _urls(second)                       # documented limitation
    third = h.poll()                                                   # poll 3: forced full scan
    assert late["post_url"] in _urls(third)
    assert h.coll.returned[-1] == 7


# ── N ───────────────────────────────────────────────────────────────────
def test_n_flag_off_always_full_scan(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _initial_docs(), INCREMENTAL_RESCAN_ENABLED=False)
    h.poll(); h.poll()
    assert h.coll.returned == [6, 6]
    assert "scan_watermark" not in next(iter(h.cache.docs.values()))


def test_n_missing_watermark_falls_back_to_full_scan(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _initial_docs())
    h.poll()
    for d in h.cache.docs.values():
        d.pop("scan_watermark", None)
    res = h.poll()
    assert h.coll.returned == [6, 6] and len(res) == 6


def test_n_changed_query_invalidates_watermark(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _initial_docs())
    h.poll(query="find ai agent buyers")
    h.poll(query="a completely different request")
    assert h.coll.returned == [6, 6]


def test_n_cache_errors_never_crash(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _initial_docs())
    h.cache.fail_update = True
    assert len(h.poll()) == 6                                           # writes fail: still answers
    h.cache.fail_update = False
    h.cache.fail_find = True
    assert len(h.poll("chat2")) == 6                                    # reads fail: full scan


def test_n_matcher_without_scan_state_still_works(lg, monkeypatch):
    h = Harness(lg, monkeypatch, [])
    calls = []

    def old_matcher(topic_key, keywords, targeting_platform="all", since_days=None, unfiltered=False,
                    match_phrases=None, limit=None, signals_collection_2=None, signals_collection_4=None,
                    chat_id=None, user_query=None, strict=False):
        calls.append(1)
        return [{"title": "t", "post_text": "x", "post_url": "u", "platform": "reddit"}]

    out = lg.get_evidence_with_topup("c", "o", "t", ["k"], 5, old_matcher)
    assert calls == [1] and len(out) == 1      # ran without scan_state, no TypeError


def test_n_failed_collection_never_advances_watermark(lg, monkeypatch):
    h = Harness(lg, monkeypatch, _initial_docs())
    h.poll()
    wm_before = next(iter(h.cache.docs.values()))["scan_watermark"]
    h.coll.docs.append(_mkdoc(500, 0.9, inserted=NOW))
    real_find = h.coll.find

    def boom(*a, **k):
        raise RuntimeError("mongo down")
    h.coll.find = boom
    h.poll()
    h.coll.find = real_find
    assert next(iter(h.cache.docs.values()))["scan_watermark"] == wm_before
    res = h.poll()                                                      # recovers, sees the doc
    assert _mkdoc(500, 0.9, inserted=NOW)["post_url"] in _urls(res)


# ── strict cache hygiene ────────────────────────────────────────────────
def test_strict_ignores_cache_written_before_strict_mode(lg, monkeypatch):
    h = Harness(lg, monkeypatch, [], INCREMENTAL_RESCAN_ENABLED=False)
    h.cache.docs[("chat1", "topic1")] = {
        "posts": [{"title": "old", "post_text": "unfiltered", "post_url": "u-old", "platform": "reddit"}] * 60}
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=True)
    matcher = MagicMock(return_value=[])
    out = lg.get_evidence_with_topup("chat1", "o", "topic1", ["k"], 25, matcher)
    assert out == [] and matcher.called                                 # stale unfiltered cache not served
    _set_cfg(monkeypatch, STRICT_INTENT_MODE=False)
    out2 = lg.get_evidence_with_topup("chat1", "o", "topic1", ["k"], 25, matcher)
    assert len(out2) == 60                                              # flag off: legacy cache reuse unchanged
