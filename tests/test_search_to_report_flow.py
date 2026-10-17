"""
tests/test_search_to_report_flow.py — the search-to-report flow end to end:
POST /search (real route) -> saved message -> GET /chat/{id}/stream (real
route: retrieval, prompt building, analysis, integrity checks, persistence)
-> saved report and cards -> rendered page (chat.html / index.html).

What is real: every Flintel function on the path, including _call_claude and
its metering hook. What is faked: retrieval returns POOL; the OpenAI HTTP
transport returns a scripted router reply and a deliberately FLAWED model
answer (inflated labels, a fabricated quote and contract value, citations of
excluded/removed/duplicate posts, "several others", a wrong count).

The posts in POOL are SYNTHETIC test data written for this file — not
retrieved Reddit content. Live-model behaviour is not exercised here.
"""
import copy
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("mongomock")
import answer_integrity as ai  # noqa: E402
from tests.test_answer_integrity import captured  # noqa: E402,F401  (fixture)
from tests.test_answer_persistence import TEMPLATES, _for_template, _reopen, env  # noqa: E402,F401
from tests.test_nonblocking_chat_page import _stream, app_env, fin  # noqa: E402,F401
from tests.test_reliability_ux_perf import _research_router  # noqa: E402

QUERY = ("Find SaaS founders and B2B marketing teams on Reddit who are looking for a Reddit monitoring or "
         "social listening tool to find leads.")


def _post(key, sub, pid, title, text, **extra):
    return dict({"key": key, "title": title, "post_text": text, "platform": "reddit", "subreddit": sub,
                 "post_url": f"https://www.reddit.com/r/{sub}/comments/{pid}/post/"}, **extra)


A_TEXT = ("We're a 4-person B2B SaaS team. Looking for a Reddit monitoring tool that flags threads where people ask "
          "for tools like ours. Budget is around $100/month and we'd start with a 2-week trial.")
POOL = [
    _post("A", "SaaS", "q1a001", "Looking for a Reddit monitoring tool, budget around $100/month", A_TEXT),
    _post("B", "startups", "q1b002", "What do you use to track Reddit mentions of your product?",
          "Early-stage SaaS founder here. What tools do people use to monitor Reddit for mentions and leads? "
          "Not buying yet, just comparing options."),
    _post("C", "personalfinance", "q1c003", "Best Reddit tool to track my spending threads?",
          "I keep saving budgeting tips from different subreddits. Is there a tool to bookmark them all in one place?"),
    _post("D", "gaming", "q1d004", "Any tool to monitor Reddit for new mod releases?",
          "Looking for a tool that watches a few gaming subreddits and pings me when a new mod drops. Free is fine."),
    _post("E", "marketing", "q1e005", "How do I get my first 100 customers for a B2B SaaS?",
          "Launched last month. Cold email, LinkedIn ads or content marketing: what worked for you?"),
    _post("F", "SaaSMarketing", "q1f006", "Is Reddit worth it for SaaS marketing?",
          "Thinking about posting on Reddit to promote our SaaS. Worth the time or a waste?"),
    _post("G", "SaaS", "q1g007", "I built a tool that alerts you when someone on Reddit asks for a product like yours",
          "ThreadScout watches subreddits, scores buying intent and drafts replies. Launch discount this week, try it free."),
    _post("H", "Entrepreneur", "q1h008", "Been using a Reddit lead tool for 3 months and it got us 12 customers",
          "We tried ThreadScout after a friend recommended it. It found threads where people wanted our kind of "
          "product and we closed 12 paying customers for our SaaS."),
    _post("I", "smallbusiness", "q1i009", "Need a social listening tool for our agency", "[removed]"),
    # Production signals carry no access-status field; http_status is set here only to exercise that path.
    _post("J", "SaaS", "q1j010", "Recommendations for Reddit monitoring software?",
          "Our B2B team needs Reddit monitoring software and the budget is approved for Q3.", http_status=404),
    _post("K", "startups", "q1k011", "Looking for a Reddit monitoring tool, budget around $100/month", A_TEXT),
    _post("L", "agency", "q1l012", "Any tool recommendations for client reporting?",
          "We're a B2B agency. Clients keep asking us to watch Reddit threads about their brand."),
]
BY_KEY = {p["key"]: p for p in POOL}


def _flawed_answer(prompt: str) -> dict:
    """What a careless model might return, using the [Post N] numbers it was given."""
    listed = re.findall(r"\[Post (\d+)\]\nTitle: (.+)", prompt)
    assert [t for _n, t in listed] == [p["title"] for p in POOL]          # prompt order == pool order
    n = {p["key"]: int(num) for p, (num, _t) in zip(POOL, listed)}
    A, B, C, D, E, F, G, H, I, J, K, L = (n[k] for k in "ABCDEFGHIJKL")
    inflated = {"role": "buyer", "purchase_intent": "explicit", "product_fit": "fits"}

    def row(key, **labels):
        p = BY_KEY[key]
        return dict({"index": n[key], "source": p["subreddit"], "title": p["title"],
                     "summary": "Asks about Reddit tools.", "sentiment": "neutral"}, **labels)
    return {
        "format": "source_list",
        "headline": "Reddit buyers looking for monitoring tools",
        "executive_summary": (f"I found 9 relevant Reddit posts. Post {A} and Post {J} are qualified buyers with "
                              f"approved budgets; several others are actively comparing tools. Post {C} shows demand "
                              f"from personal users too."),
        "key_findings": [
            {"finding": "Qualified buyers with budget", "evidence_type": "Buyer", "signal_strength": "Strong",
             "supporting_post_indices": [A, J, I, K], "impact": f"Posts {A}, {J}, {I} and {K} state a budget or urgent need."},
            {"finding": "Buyers comparing Reddit monitoring tools", "evidence_type": "Buyer", "signal_strength": "Strong",
             "supporting_post_indices": [B, D, F, G, H],
             "impact": f"Several posts (Posts {B}, {D}, {F}, {G} and {H}) are comparing tools."},
            {"finding": "Competitors validating the category", "evidence_type": "Buyer", "signal_strength": "Strong",
             "supporting_post_indices": [G, H], "impact": f"Posts {G} and {H} show buyers are paying for this."},
            {"finding": "Personal and gaming users want monitoring", "evidence_type": "Contextual",
             "signal_strength": "Moderate", "supporting_post_indices": [C, E], "impact": f"Posts {C} and {E} want it too."},
        ],
        "detailed_findings": [
            {"title": "Qualified buyer", "supporting_post_indices": [A],
             "what_evidence_shows": f"Post {A} will sign a $5k annual contract this week."},
            {"title": "Research-stage demand", "supporting_post_indices": [B, L],
             "what_evidence_shows": f"Post {B} and Post {L} ask for tool recommendations without a budget."},
        ],
        "business_insight_points": [f"Reply to Post {A} and Post {J} with a pilot offer.",
                                    f"Target personal finance users like Post {C}.",
                                    f"Offer Post {B} a $5k annual plan."],
        "platforms": [{"platform": "reddit", "total_analyzed": 12, "shown_count": 12, "posts": [
            row("A", evidence="We will sign a $5k annual contract this week.", **inflated),
            row("B", evidence="What tools do people use to monitor Reddit for mentions and leads?", **inflated),
            row("C", role="buyer", purchase_intent="research", product_fit="unrelated"),
            row("D", role="buyer", purchase_intent="research", product_fit="fits"),
            row("E"),
            row("F", role="buyer", purchase_intent="research", product_fit="partial"),
            row("G", **inflated), row("H", **inflated), row("I", **inflated), row("J", **inflated),
            row("K", **inflated),
            row("L", role="buyer", purchase_intent="research", product_fit="fits"),
        ]}],
    }


class _Resp:
    def __init__(self, data):
        self.status_code, self.text, self._data = 200, "", data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeOpenAI:
    """Replaces only the HTTP transport inside the real logics._call_claude."""
    def __init__(self, lg):
        self.lg, self.calls = lg, []

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):          # noqa: A002
        payload = json
        self.calls.append(payload)
        n = len(self.calls)
        if payload["instructions"] == self.lg.CLAUDE_ROUTER_SYSTEM_PROMPT:
            router = dict(_research_router(QUERY), evidence_required=len(POOL), keywords=["reddit monitoring"])
            text, usage = _json.dumps(router), (300 + n, 40)
        elif "Posts (title + text only)" in payload["input"]:
            text, usage = _json.dumps(_flawed_answer(payload["input"])), (5000, 900)
        else:
            text, usage = "{}", (10, 1)
        return _Resp({"id": f"resp_{n}", "status": "completed",
                      "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
                      "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]})


_json = json


@pytest.fixture
def flow(fin, monkeypatch, captured):
    """POST /search then the live stream, through the real routes."""
    um, docs = captured
    lg, rt = fin["lg"], fin["rt"]
    transport = FakeOpenAI(lg)
    # app_env replaced _call_claude with a fake; restore the real one so its metering hook runs
    real = next(v for o, name, v in monkeypatch._setattr if o is lg and name == "_call_claude")
    monkeypatch.setattr(lg, "_call_claude", real)
    monkeypatch.setattr(lg.httpx, "Client", transport, raising=False)

    # The router raises any evidence target to MIN_ANALYSIS_EVIDENCE (15); with this 12-post pool
    # the real stream would wait out RESPONSE_TIMEOUT first, so the minimum is lowered to the pool size.
    for mod in (lg, rt):
        monkeypatch.setattr(mod, "MIN_ANALYSIS_EVIDENCE", len(POOL), raising=False)
    monkeypatch.setattr(rt, "STREAM_CHUNK_DELAY_SECONDS", 0, raising=False)    # typing-effect pacing only

    def retrieve(**kw):
        um.record_usage("embedding", "text-embedding-3-small", 64, 0)
        return [{k: v for k, v in p.items() if k != "key"} for p in copy.deepcopy(POOL)]
    monkeypatch.setattr(rt, "get_evidence_with_topup", retrieve)
    resp = rt.search(type("R", (), {"session": {}})(), query=QUERY, platform="Reddit", chat_id=None)
    chat_id = resp.headers["location"].rsplit("/", 1)[-1]
    msg = fin["chats"].find_one({"chat_id": chat_id})["messages"][-1]
    events = []
    for chunk in _stream(fin, monkeypatch, chat_id, topic_key=msg["topic_key"]):
        events.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
        if '"done"' in events[-1] or '"error"' in events[-1]:
            break
    assert '"done"' in events[-1], events[-3:]
    saved = fin["chats"].find_one({"chat_id": chat_id})
    return {"chat": saved, "msg": saved["messages"][-1], "answer": json.loads(saved["messages"][-1]["claude_answer"]),
            "docs": docs, "um": um, "transport": transport, "done": json.loads(events[-1][len("data: "):])}


def _rows(answer):
    return [p for e in answer["platforms"] for p in e["posts"]]


def _texts(node, skip=("platforms", "counts", "excluded_posts")):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for x in node:
            yield from _texts(x, skip)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k not in skip:
                yield from _texts(v, skip)


def _cited(text):
    return [int(n) for m in re.finditer(r"\bPosts? (\d+(?:(?:, | and )\d+)*)", text) for n in re.findall(r"\d+", m.group(1))]


# ── the saved report ──────────────────────────────────────────────────────────
EXPECTED_ROWS = [   # key, rating, role, purchase_intent, product_fit, source status
    ("A", "High", "buyer", "explicit", "fits", "verified"),
    ("B", "Medium", "buyer", "research", "fits", "verified"),
    ("L", "Medium", "buyer", "research", "partial", "verified"),
    ("D", "Uncertain", "buyer", "research", "uncertain", "verified"),
    ("I", "Uncertain", None, "unknown", "unknown", "removed"),
    ("J", "Uncertain", None, "unknown", "unknown", "inaccessible"),
    ("F", "Low", "discussion", "none", "partial", "verified"),
    ("G", "Low", "competitor_promo", "none", "fits", "verified"),
    ("H", "Low", "testimonial", "none", "fits", "verified"),
]


def test_every_row_is_classified_from_its_own_record(flow):
    rows = _rows(flow["answer"])
    by_url = {p["post_url"]: p for p in POOL}
    got = [(by_url[r["link"]]["key"], r["intent"], r["role"], r["purchase_intent"], r["product_fit"],
            r["verification_status"]) for r in rows]
    assert got == EXPECTED_ROWS
    assert [r["index"] for r in rows] == list(range(1, 10))
    for r in rows:
        sig = by_url[r["link"]]
        assert r["source_id"] == "reddit:" + sig["post_url"].split("/comments/")[1].split("/")[0]
        if r["evidence"]:                                                  # every excerpt is verbatim
            assert ai.quote_in_post(r["evidence"], sig["title"], sig["post_text"]) and r["verification_status"] != "removed"
    a = rows[0]
    assert a["evidence"] == BY_KEY["A"]["title"] and "$5k" not in a["evidence"]          # fabricated quote replaced
    assert any("quote was not found" in u for u in a["uncertainty"])


def test_model_labels_cannot_create_qualified_leads(flow):
    rows = {next(p["key"] for p in POOL if p["post_url"] == r["link"]): r for r in _rows(flow["answer"])}
    c = flow["answer"]["counts"]
    # the model marked 8 posts buyer/explicit/fits; only A has a request, a purchase step, fit and a source
    assert c["qualified"] == 1 and [k for k, r in rows.items() if r["qualified"]] == ["A"]
    assert rows["D"]["intent"] == "Uncertain" and not rows["D"]["qualified"]   # model fit alone
    assert rows["B"]["purchase_intent"] == "research"                          # "Not buying yet" is not a purchase step
    assert rows["G"]["role"] == "competitor_promo" and rows["H"]["role"] == "testimonial"
    assert c["competitor"] == 2 and c["buyers"] == 3
    assert c == {"retrieved": 12, "unique": 12, "analyzed": 12, "listed": 12, "excluded": 3,
                 "excluded_reasons": {"irrelevant": 2, "duplicate": 1, "unverifiable": 0}, "shown": 9,
                 "relevant": 6, "uncertain": 1, "unverified": 2, "verified": 7, "buyers": 3, "qualified": 1,
                 "researching": 2, "competitor": 2}


def test_every_claim_cites_only_supported_rows(flow):
    ans = flow["answer"]
    rows = {r["index"]: r for r in _rows(ans)}
    verified = {i for i, r in rows.items() if r["verification_status"] == "verified"}
    for text in _texts(ans):
        assert set(_cited(text)) <= verified, text               # no excluded, removed or inaccessible citation
        assert "$5k" not in text and "several others" not in text.lower() and "not shown" not in text
    assert ans["executive_summary"] == "I found 6 relevant Reddit posts. Post 1 is qualified buyers with approved budgets."
    assert ans["business_insight_points"] == ["Reply to Post 1 with a pilot offer."]
    kf = {f["finding"]: f for f in ans["key_findings"]}
    assert set(kf) == {"Qualified buyers with budget", "Buyers comparing Reddit monitoring tools",
                       "Competitors validating the category"}
    assert kf["Qualified buyers with budget"]["impact"] == "Post 1 states a budget or urgent need."
    assert kf["Qualified buyers with budget"]["signal_strength"] == "Strong"
    comparing = kf["Buyers comparing Reddit monitoring tools"]
    assert comparing["impact"] == "One post (Post 2) is comparing tools." and comparing["signal_strength"] == "Moderate"
    vendors = kf["Competitors validating the category"]
    assert vendors["demand_signal"] is False and vendors["evidence_type"] == "Builder/Provider"
    for f in ans["key_findings"] + ans["detailed_findings"]:
        assert set(f["supporting_post_indices"]) <= verified
        if f["demand_signal"]:
            assert all(rows[i]["role"] == "buyer" and rows[i]["relevance"] == "relevant" for i in f["supporting_post_indices"])
    assert [d["title"] for d in ans["detailed_findings"]] == ["Research-stage demand"]     # the $5k claim is gone
    assert ans["verified_summary"] == ("Checked against the stored posts: 1 qualified lead, 2 buyers still researching, "
                                       "1 post with unconfirmed fit, 2 competitor or vendor posts, "
                                       "2 posts with an unavailable source; 3 posts excluded.")


def test_cards_match_the_report(flow):
    ans, results = flow["answer"], flow["msg"]["results"]
    assert flow["done"]["results"] == results                    # what the stream sent == what was saved
    status = {r["post_url"]: r["review"] for r in results}
    assert len(results) == ans["counts"]["retrieved"]
    for r in _rows(ans):
        assert status[r["link"]] == {"status": "in_report", "index": r["index"], "intent": r["intent"],
                                     "qualified": r["qualified"], "relevance": r["relevance"]}
    excluded = {p["key"]: status[p["post_url"]]["reason"] for p in POOL if status[p["post_url"]]["status"] == "excluded"}
    assert excluded == {"C": "not relevant", "E": "not relevant", "K": "duplicate"}
    assert sum(v["qualified"] for v in status.values()) == ans["counts"]["qualified"]


@pytest.mark.parametrize("name", TEMPLATES)
def test_rendered_page_shows_the_same_records(integrity_browser, flow, name):
    ans = flow["answer"]
    page, errors = _reopen(integrity_browser, name, _for_template(flow["chat"]))
    try:
        rows = page.evaluate("""() => [...document.querySelectorAll('.ap-sources-table tbody tr')].map(tr =>
            [...tr.querySelectorAll('td')].map(td => td.innerText.trim()))""")
        assert [(r[0], r[2], r[4].split("\n")[0].split(" · ")[0]) for r in rows] == \
            [(str(p["index"]), p["title"], p["intent"]) for p in _rows(ans)]
        text = page.inner_text(".answer-rendered")
        assert ans["verified_summary"] in text
        assert "6 relevant · 1 uncertain fit · 2 unverified · 7 verified · 1 qualified lead · 2 competitor/vendor · " \
               "3 excluded (of 12 retrieved)" in text
        assert set(_cited(text)) <= {p["index"] for p in _rows(ans) if p["verification_status"] == "verified"}
        assert "$5k" not in text and "several others" not in text.lower() and errors == []
    finally:
        page.close()


# ── usage: one request id from POST /search to the saved report ───────────────
def test_usage_from_post_search_to_report_shares_one_request_id(flow, monkeypatch):
    msg, docs, um = flow["msg"], flow["docs"], flow["um"]
    rid = msg["request_id"]
    assert rid and all(d["request_id"] == rid for d in docs)
    router = [d for d in docs if d["call_type"] == "llm" and d["call_site"] != "analyze_with_claude"]
    analysis = [d for d in docs if d.get("call_site") == "analyze_with_claude"]
    embed = [d for d in docs if d["call_type"] == "embedding"]
    assert len(router) == 1 and router[0]["chat_id"] is None                     # before the chat existed
    assert len(analysis) == 1
    assert all(d["chat_id"] == flow["chat"]["chat_id"] for d in analysis + embed)
    assert len({d["response_id"] for d in router + analysis}) == 2              # one row per model execution
    mongomock = pytest.importorskip("mongomock")
    import types
    db = mongomock.MongoClient()["usage"]
    db["FLINTEL_USAGE_IDEA"].insert_many(copy.deepcopy(docs))
    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace(db3=db))
    s = um.get_search_usage(None, None, request_id=rid)
    sent = [c for c in flow["transport"].calls]
    assert s["calls"] == len(sent) + len(embed)
    assert s["input_tokens"] == sum(d["input_tokens"] for d in docs)
    assert s["by_call_type"]["llm"]["calls"] == len(sent) and s["external"]["calls"] == 0


def test_same_response_is_never_counted_twice(captured, monkeypatch):
    um, docs = captured
    with um.attribution(request_id="r1"):
        um.record_usage("llm", "gpt-5-mini", 100, 10, extra={"response_id": "resp_x"})
        um.record_usage("llm", "gpt-5-mini", 100, 10, extra={"response_id": "resp_x"})   # replayed hook
        um.record_usage("llm", "gpt-5-mini", 100, 10, extra={"response_id": "resp_y"})   # a second execution
    s = um.summarize_search_usage(docs)
    assert s["calls"] == 2 and s["input_tokens"] == 200
    mongomock = pytest.importorskip("mongomock")
    import types
    db = mongomock.MongoClient()["usage"]
    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace(db3=db))
    monkeypatch.setattr(um, "_index_created", False)
    for d in docs:
        um._do_insert(copy.deepcopy(d))
    assert db["FLINTEL_USAGE_IDEA"].count_documents({}) == 2


def test_external_search_calls_are_counted_apart_from_model_usage(captured, monkeypatch):
    um, docs = captured
    spec = importlib.util.spec_from_file_location("google_real_for_test", ROOT / "google.py")
    google = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(google)
    monkeypatch.setattr(google, "RAPIDAPI_KEY", "test-key-not-real")
    outcomes = iter([200, 500])

    class _Http:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            status = next(outcomes)

            class R:
                status_code = status

                def raise_for_status(self):
                    if status >= 400:
                        raise RuntimeError("HTTP error")

                def json(self):
                    return {"results": []}
            return R()
    monkeypatch.setattr(google, "httpx", type("httpx_stub", (), {"Client": _Http}))
    with um.attribution(request_id="r2", chat_id="c2"):
        um.record_usage("llm", "gpt-5-mini", 1000, 100)
        assert google._call_rapidapi_google_search("reddit monitoring") == {"results": []}
        assert google._call_rapidapi_google_search("reddit monitoring") is None
    ext = [d for d in docs if d["call_type"] == "external"]
    assert [(d["call_site"], d["provider"], d["status"], d["request_id"]) for d in ext] == \
        [("google_search", "rapidapi", "ok", "r2"), ("google_search", "rapidapi", "http_500", "r2")]
    s = um.summarize_search_usage(docs)
    assert s["calls"] == 1 and s["cost_usd"] == pytest.approx(1000 * 0.25e-6 + 100 * 2e-6)
    assert s["external"] == {"calls": 2, "by_service": {"google_search": 2}, "cost_usd": None}
