"""Lead-generation MVP: business profile, safe website fetch, search concepts, normalisation, de-duplication, qualification (scenarios A-J),
coverage / reconciliation, persistence and ownership, and the real pages through the FastAPI app (fake collections, fake connectors, no network,
no model, no database)."""
import importlib
from datetime import datetime, timedelta, timezone

import pytest

import business_profile as bp
import lead_connectors as lc
import lead_engine as le
import lead_store as ls
import safe_fetch as sf
from tests.test_mapreduce_identity import env, lg  # noqa: F401

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


class Coll:
    """Minimal pymongo-style collection: equality filters, $set, upsert."""
    def __init__(self):
        self.docs = []

    def _m(self, d, f):
        return all(d.get(k) == v for k, v in f.items())

    def find_one(self, f):
        return next((dict(d) for d in self.docs if self._m(d, f)), None)

    def find(self, f=None):
        return [dict(d) for d in self.docs if self._m(d, f or {})]

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def update_one(self, f, upd, upsert=False):
        for d in self.docs:
            if self._m(d, f):
                d.update(upd["$set"])
                return
        if upsert:
            self.docs.append(dict(f, **upd["$set"]))


class AtomicColl(Coll):
    """Quota collection double: single-document operations are atomic (one lock), the unique key is (owner_key, slot), and the conditional
    find_one_and_update supports $lt / $lte guards with $inc / $set, like MongoDB. It proves the LOGIC under atomic single-document semantics; it
    is not a MongoDB test."""
    def __init__(self):
        super().__init__()
        import threading
        self.lock = threading.Lock()

    def _m(self, d, f):
        for k, v in f.items():
            if isinstance(v, dict):
                if "$lt" in v and not (d.get(k) is not None and d[k] < v["$lt"]):
                    return False
                if "$lte" in v and not (d.get(k) is not None and d[k] <= v["$lte"]):
                    return False
            elif d.get(k) != v:
                return False
        return True

    def find_one_and_update(self, f, upd):
        with self.lock:
            for d in self.docs:
                if self._m(d, f):
                    for k, v in upd.get("$inc", {}).items():
                        d[k] = d.get(k, 0) + v
                    d.update(upd.get("$set", {}))
                    return dict(d)
        return None

    def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError
        with self.lock:
            if any(d.get("owner_key") == doc.get("owner_key") and d.get("slot") == doc.get("slot") for d in self.docs):
                raise DuplicateKeyError("dup")
            self.docs.append(dict(doc))

    def update_one(self, f, upd, upsert=False):
        with self.lock:
            for d in self.docs:
                if self._m(d, f):
                    d.update(upd["$set"])
                    return


def profile_for(sells, customers="", problems="", competitors="", geo="worldwide", places="", name="Acme", exclusions=""):
    user, errors = bp.validate({"business_name": name, "sells": sells, "ideal_customers": customers, "problems_solved": problems,
                                "competitors": competitors, "geo_scope": geo, "geo_places": places, "exclusions": exclusions})
    assert not errors, errors
    return bp.build(user)


def post(i, title, text, platform="reddit", days=3, author="u1", url=None, **extra):
    base = {"title": title, "post_text": text, "platform": platform, "author": author,
            "post_url": url or (f"https://www.reddit.com/r/test/comments/p{i}/x/" if platform == "reddit" else f"https://news.ycombinator.com/item?id={i}"),
            "published_at": (NOW - timedelta(days=days)).isoformat()}
    base.update(extra)
    return base


class Fake:
    def __init__(self, key, label, records=(), status="ok", boom=False, path="historical"):
        self.key, self.label, self.retrieval_path, self.records, self.status, self.boom = key, label, path, list(records), status, boom

    def fetch(self, concepts, since_days, limit):
        if self.boom:
            raise RuntimeError("down")
        return {"status": self.status, "records": self.records, "detail": ""}


def scan(profile, *connectors):
    return le.run_scan(profile, list(connectors), now=NOW)


def labels(s):
    return {l["title"]: l["label"] for l in s["leads"]}


# ── business profile ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_profile_requires_what_the_business_sells_and_a_name_or_website():
    _, e = bp.validate({"sells": "x"})
    assert "sells" in e and "business_name" in e
    _, e = bp.validate({"sells": "We sell CRM software for recruiters", "website": "acme.com"})
    assert e == {}                                           # a website can stand in for the name


def test_profile_keeps_user_text_apart_from_derived_terms_and_trims_it():
    user, e = bp.validate({"business_name": "  Acme  ", "sells": "We sell\x00 CRM software for recruitment agencies", "competitors": "Bullhorn, bullhorn, Loxo"})
    p = bp.build(user)
    assert p["user"]["business_name"] == "Acme" and "\x00" not in p["user"]["sells"] and p["user"]["competitors"] == ["Bullhorn", "Loxo"]
    assert "crm" in p["derived"]["offering_terms"] and p["derived"]["source"].endswith("no model used")


@pytest.mark.parametrize("scope,places,ok", [("city", "", False), ("city", "Leeds", True), ("worldwide", "", True), ("planet", "", False)])
def test_geography_rules(scope, places, ok):
    _, e = bp.validate({"business_name": "A", "sells": "plumbing services for homes", "geo_scope": scope, "geo_places": places})
    assert (not any(k.startswith("geo") for k in e)) is ok


@pytest.mark.parametrize("raw,ok", [("acme.com", True), ("https://acme.com/about", True), ("javascript:alert(1)", False), ("ftp://x.com", False), ("not a site", False)])
def test_website_url_normalisation(raw, ok):
    assert (bp.normalize_website_url(raw) is not None) is ok


def test_website_facts_are_read_from_the_page_only():
    html = "<html><head><title>Acme CRM</title><meta name='description' content='CRM for recruiters'></head><body><h1>Track candidates</h1><script>x=1</script></body></html>"
    f = bp.extract_website_facts(html)
    assert f["title"] == "Acme CRM" and f["description"] == "CRM for recruiters" and f["headings"] == ["Track candidates"] and "x=1" not in f["text_excerpt"]


def test_website_analysis_reports_failure_instead_of_inventing():
    def boom(url):
        raise sf.FetchFailed("The website could not be reached.")
    out = bp.analyze_website("https://acme.com", boom)
    assert out["status"] == "unavailable" and out["facts"] == {} and "describe your business" in out["message"]
    empty = bp.analyze_website("https://acme.com", lambda u: {"text": "   "})
    assert empty["status"] == "unavailable" and empty["facts"] == {}
    bad = bp.analyze_website("http://127.0.0.1", lambda u: sf.check_url(u))
    assert bad["status"] == "unsafe"


def test_profile_store_is_owner_scoped_and_needs_a_signed_in_owner():
    s = bp.ProfileStore(Coll())
    s.save("a@x.com", {"user": {"business_name": "A"}})
    s.save("b@x.com", {"user": {"business_name": "B"}})
    assert s.get("a@x.com")["user"]["business_name"] == "A" and s.get("b@x.com")["user"]["business_name"] == "B"
    assert s.get("c@x.com") is None
    for guest in ("anon-uuid-123", "", None):
        with pytest.raises(PermissionError):
            s.get(guest)


# ── SSRF-safe fetch ──────────────────────────────────────────────────────────────────────────────────────────────────────
PUB = lambda host, port: ["93.184.216.34"]


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/", "http://[::1]/", "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
                                 "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/", "http://[::ffff:127.0.0.1]/", "http://metadata.internal/",
                                 "http://x.localhost/", "ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)", "http://user:pw@example.com/",
                                 "https://example.com:8443/", "http://100.64.0.1/"])
def test_unsafe_urls_are_refused_before_any_request(url):
    calls = []
    with pytest.raises(sf.UnsafeUrl):
        sf.fetch_public_page(url, resolve=PUB, transport=lambda *a: calls.append(a))
    assert calls == []


def test_a_hostname_that_resolves_to_a_private_address_is_refused():
    with pytest.raises(sf.UnsafeUrl):
        sf.check_url("https://evil.example/", resolve=lambda h, p: ["93.184.216.34", "10.0.0.8"])


def test_a_redirect_to_an_internal_address_is_refused_on_the_second_hop():
    hops = []

    def transport(scheme, host, port, path, addr, timeout, mx):
        hops.append(host)
        return 302, {"location": "http://169.254.169.254/latest/"}, b"", False

    def resolve(h, p):
        return ["93.184.216.34"]
    with pytest.raises(sf.UnsafeUrl):
        sf.fetch_public_page("https://example.com/", resolve=resolve, transport=transport)
    assert hops == ["example.com"]


def test_redirect_loops_and_big_or_non_page_responses_are_bounded():
    with pytest.raises(sf.FetchFailed):
        sf.fetch_public_page("https://example.com/", resolve=PUB, transport=lambda *a: (302, {"location": "/again"}, b"", False))
    with pytest.raises(sf.FetchFailed):
        sf.fetch_public_page("https://example.com/", resolve=PUB, transport=lambda *a: (200, {"content-type": "application/pdf"}, b"x", False))
    with pytest.raises(sf.FetchFailed):
        sf.fetch_public_page("https://example.com/", resolve=PUB, transport=lambda *a: (500, {}, b"", False))
    ok = sf.fetch_public_page("https://example.com/", resolve=PUB, transport=lambda *a: (200, {"content-type": "text/html; charset=utf-8"}, b"<title>x</title>", True))
    assert ok["truncated"] is True and "<title>" in ok["text"]


def test_the_transport_receives_the_validated_address_not_a_second_lookup():
    seen = {}

    def transport(scheme, host, port, path, addr, timeout, mx):
        seen.update(host=host, addr=addr, port=port, mx=mx)
        return 200, {"content-type": "text/html"}, b"hi", False
    sf.fetch_public_page("https://Example.com/x?y=1", resolve=PUB, transport=transport, max_bytes=1000)
    assert seen == {"host": "example.com", "addr": "93.184.216.34", "port": 443, "mx": 1000}


# ── search concepts: one engine, many businesses ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("sells,customers,problems,must", [
    ("We provide social media marketing services to small businesses", "small businesses", "not enough customers online", "social media marketing"),
    ("CRM software for recruitment agencies", "recruitment agencies", "tracking candidates", "CRM software"),
    ("I design and develop websites for SaaS founders", "SaaS founders", "landing page that does not convert", "websites"),
    ("We sell skincare products online", "people with dry skin", "dry skin", "skincare products"),
    ("We provide plumbing services", "homeowners", "leaking pipes", "plumbing services"),
])
def test_concepts_are_specific_to_the_business_and_cover_the_request_kinds(sells, customers, problems, must):
    cs = le.search_concepts(profile_for(sells, customers, problems))
    assert cs and len(cs) <= 12 and any(must.lower() in c["query"].lower() for c in cs)
    types = {c["type"] for c in cs}
    assert {"direct_request", "recommendation_request", "provider_search", "problem_description"} <= types
    assert len({c["query"].lower() for c in cs}) == len(cs)
    assert not any(c["query"].lower().startswith(("we ", "i ")) for c in cs)              # the lead-in is trimmed, the offer is not rewritten


def test_local_business_concepts_carry_the_place_and_competitors_add_alternative_requests():
    cs = le.search_concepts(profile_for("We provide plumbing services", "homeowners", "leaking pipes", competitors="Pimlico Plumbers", geo="city", places="Leeds"))
    assert any("Leeds" in c["query"] for c in cs if c["type"] in ("direct_request", "provider_search"))
    assert any(c["type"] == "alternative_request" for c in cs)


# ── normalisation, identity, de-duplication ────────────────────────────────────────────────────────────────────────────────
def test_normalisation_preserves_unknown_and_never_invents():
    n = le.normalize_post({"title": "t", "post_text": "b", "post_url": "https://www.reddit.com/r/a/comments/abc/x/", "author": "[deleted]"}, NOW)
    assert n["author"] is None and n["published_at"] is None and n["source_id"] == "reddit:abc" and n["platform"] == "reddit"
    noloc = le.normalize_post({"title": "t", "post_text": "b"}, NOW)
    assert noloc["source_id"] == "" and noloc["url"] == ""


def test_duplicates_are_removed_by_platform_specific_source_id_and_authors_are_not_merged_across_platforms():
    a = le.normalize_post(post(1, "same", "x", author="admin"), NOW)
    b = le.normalize_post(post(1, "same", "x", author="admin", url="https://www.reddit.com/r/other/comments/p1/y/"), NOW)
    h = le.normalize_post(post(1, "same", "x", platform="hackernews", author="admin"), NOW)
    uniq, dups = le.dedupe([a, b, h])
    assert dups == 1 and len(uniq) == 2 and {u["platform"] for u in uniq} == {"reddit", "hackernews"}


# ── qualification: scenarios A-E (what is and is not a lead) ──────────────────────────────────────────────────────────────
def test_scenario_a_saas_crm_for_recruiters():
    p = profile_for("CRM software for recruitment agencies", "recruitment agencies, recruiters", "tracking candidates, spreadsheets")
    s = scan(p, Fake("reddit", "Reddit", [
        post(1, "Looking for a CRM for our recruitment agency", "We are a small recruitment agency and are looking for a CRM to track candidates. Any recommendations? Budget is $100/month."),
        post(2, "What is a CRM?", "Can someone explain what a CRM is in general?"),
        post(3, "Show HN: HireTrack, a CRM for recruiters", "I built HireTrack, a CRM for recruitment agencies. Free trial, feedback welcome."),
    ]))
    lab = labels(s)
    assert lab == {"Looking for a CRM for our recruitment agency": "High potential"}
    assert {e["title"] for e in s["excluded"]} == {"What is a CRM?", "Show HN: HireTrack, a CRM for recruiters"}
    top = s["leads"][0]
    assert top["evidence"]["quote"] in (top["title"] + "\n" + top["text"]) and top["intent"] in ("DIRECT_BUYING_INTENT", "RECOMMENDATION_REQUEST")


def test_scenario_b_agency_requests_are_leads_and_another_agency_advertising_is_not():
    p = profile_for("We provide social media marketing services to small businesses", "small businesses", "not enough customers online")
    s = scan(p, Fake("reddit", "Reddit", [
        post(1, "Need a social media marketing agency", "We are a small business and need a social media marketing agency to grow our customers online. Can anyone recommend one?"),
        post(2, "Our agency offers social media marketing", "We are a marketing agency offering social media marketing services to small businesses. DM us for a free audit."),
    ]))
    assert labels(s).get("Need a social media marketing agency") in ("High potential", "Relevant")
    assert "Our agency offers social media marketing" not in labels(s)


def test_scenario_c_freelancer_website_design():
    p = profile_for("I design and develop websites for SaaS founders", "SaaS founders", "landing page that does not convert")
    s = scan(p, Fake("reddit", "Reddit", [post(1, "Looking for a web developer for our SaaS landing page", "I'm a SaaS founder looking for a freelance web developer to redesign our landing page. Budget is $2k.")]))
    assert list(labels(s).values()) == ["High potential"]


def test_scenario_d_skincare_not_every_discussion_is_a_buyer():
    p = profile_for("We sell skincare products online", "people with dry skin", "dry skin")
    s = scan(p, Fake("reddit", "Reddit", [
        post(1, "Can anyone recommend a moisturizer for dry skin?", "I have very dry skin in winter and I'm looking for a good moisturizer. Any recommendations for skincare products?"),
        post(2, "Dry skin science", "Here is an article about why skin gets dry in winter. Interesting reading about skincare research."),
    ]))
    assert labels(s).get("Can anyone recommend a moisturizer for dry skin?") in ("High potential", "Relevant")
    assert "Dry skin science" not in labels(s)


def test_scenario_e_local_business_respects_geography():
    p = profile_for("We provide plumbing services", "homeowners", "leaking pipes", geo="city", places="Leeds")
    s = scan(p, Fake("reddit", "Reddit", [
        post(1, "Need a plumber in Leeds", "I need a plumber in Leeds for a leaking pipe under my sink. Can anyone recommend a plumbing service?"),
        post(2, "Need a plumber", "I need a plumber for a leaking pipe under my sink. Can anyone recommend a plumbing service?"),
    ]))
    lab = labels(s)
    assert lab["Need a plumber in Leeds"] == "High potential"
    assert lab["Need a plumber"] == "Needs review"                        # location not confirmed: never presented as a local lead
    assert next(l for l in s["leads"] if l["title"] == "Need a plumber")["geography"]["status"] == "unknown"


# ── scenarios F-J: coverage, zero, unknown author, history, identity ─────────────────────────────────────────────────────
GOOD = post(1, "Looking for a CRM for our recruitment agency", "We are a small recruitment agency and are looking for a CRM to track candidates. Any recommendations? Budget is $100/month.")
P_CRM = profile_for("CRM software for recruitment agencies", "recruitment agencies", "tracking candidates")


@pytest.mark.parametrize("status,boom,state", [("failed", False, "failed"), ("rate_limited", False, "unavailable"), ("unavailable", False, "unavailable"),
                                                ("partial", False, "partial"), ("skipped", False, "not_searched"), ("weird", False, "unconfirmed"), ("ok", True, "failed")])
def test_scenario_f_a_source_that_did_not_complete_never_makes_the_scan_complete(status, boom, state):
    s = scan(P_CRM, Fake("reddit", "Reddit", [GOOD]), Fake("hackernews", "Hacker News", [], status=status, boom=boom, path="live"))
    assert {x["key"]: x["state"] for x in s["coverage"]["sources"]}["hackernews"] == state
    assert s["coverage"]["status"] == "partial" and "complete" not in s["statement"].lower().replace("not complete", "")
    s0 = scan(P_CRM, Fake("hackernews", "Hacker News", [], status=status, boom=boom))
    assert s0["counts"]["displayed"] == 0 and "does not show that no demand exists" in s0["statement"]


def test_scenario_g_a_complete_scan_with_no_leads_reports_zero_honestly():
    s = scan(P_CRM, Fake("reddit", "Reddit", [post(1, "What is a CRM?", "Can someone explain what a CRM is?")]), Fake("hackernews", "Hacker News", [], status="empty", path="live"))
    assert s["coverage"]["status"] == "complete" and s["leads"] == []
    assert "No qualified leads were found" in s["statement"] and "not proof that no demand exists" in s["statement"]


def test_an_unselected_source_never_appears_and_cannot_make_the_scan_incomplete():
    s = scan(P_CRM, Fake("reddit", "Reddit", [GOOD]))
    assert [x["key"] for x in s["coverage"]["sources"]] == ["reddit"] and s["coverage"]["status"] == "complete"


def test_scenario_h_unknown_author_stays_unknown():
    s = scan(P_CRM, Fake("reddit", "Reddit", [dict(GOOD, author="[deleted]")]))
    assert s["leads"][0]["author"] is None
    s2 = scan(P_CRM, Fake("reddit", "Reddit", [{k: v for k, v in GOOD.items() if k != "author"}]))
    assert s2["leads"][0]["author"] is None


def test_scenario_i_historical_undated_invalid_and_future_posts_are_never_current_leads():
    old = dict(post(1, "Looking for a CRM for our recruitment agency", GOOD["post_text"], days=400))
    undated = {k: v for k, v in post(2, "Need a CRM for recruitment agency", "We are a recruitment agency and are looking for a CRM to track candidates. Recommendations?") .items() if k != "published_at"}
    bad = post(3, "Seeking a CRM for recruitment agency", "We are a recruitment agency and are looking for a CRM to track candidates. Recommendations?", published_at="garbage")
    future = post(4, "Looking for CRM recruitment agency now", GOOD["post_text"], days=-20)
    s = scan(P_CRM, Fake("reddit", "Reddit", [old, undated, bad, future]))
    by = {l["source_id"]: l for l in s["leads"]}
    assert all(l["label"] == "Needs review" and l["is_current"] is False for l in s["leads"])
    assert {l["recency"] for l in s["leads"]} == {"historical", "undated", "invalid"}
    assert [e["reason"] for e in s["excluded"] if "future" in e["reason"]] and s["counts"]["current_leads"] == 0 and len(by) == 3


def test_scenario_j_identical_usernames_on_different_platforms_are_two_authors_and_two_leads():
    hn = post(7, "Ask HN: CRM for a recruitment agency?", GOOD["post_text"], platform="hackernews", author="admin")
    rd = post(8, "Looking for a CRM for our recruitment agency", GOOD["post_text"], author="admin")
    s = scan(P_CRM, Fake("reddit", "Reddit", [rd]), Fake("hackernews", "Hacker News", [hn], path="live"))
    assert s["counts"]["displayed"] == 2 and {l["platform"] for l in s["leads"]} == {"reddit", "hackernews"}


def test_exclusion_terms_and_missing_urls_and_quotes_never_become_leads():
    p = profile_for("CRM software for recruitment agencies", "recruitment agencies", "tracking candidates", exclusions="enterprise")
    s = scan(p, Fake("reddit", "Reddit", [dict(GOOD, title="Looking for an enterprise CRM for our recruitment agency", post_text="We are looking for an enterprise CRM for recruitment agency."),
                                         {k: v for k, v in post(2, "x", GOOD["post_text"]).items() if k != "post_url"}]))
    assert s["leads"] == []


def test_every_displayed_lead_has_a_source_link_and_an_exact_quote_from_the_post():
    s = scan(P_CRM, Fake("reddit", "Reddit", [GOOD, post(2, "Losing track of candidates in spreadsheets", "We are a recruiting agency and our spreadsheets keep breaking, we lose track of candidates. Any ideas for tracking candidates?")]))
    assert s["leads"]
    for l in s["leads"]:
        assert l["url"].startswith("http") and l["source_id"] and l["evidence"]["quote"] in (l["title"] + "\n" + l["text"]) and l["suggested_opener"]


def test_the_opener_uses_only_the_post_and_the_users_own_words():
    s = scan(P_CRM, Fake("reddit", "Reddit", [GOOD]))
    o = s["leads"][0]["suggested_opener"]
    assert "Looking for a CRM for our recruitment agency" in o and "CRM software for recruitment agencies" in o
    assert not any(w in o.lower() for w in ("guarantee", "increase", "results", "we know you", "saw you"))


# ── reconciliation and coverage arithmetic ───────────────────────────────────────────────────────────────────────────────
def test_counts_reconcile_for_a_mixed_scan():
    recs = [GOOD, GOOD, post(2, "What is a CRM?", "Can someone explain what a CRM is?"), post(3, "Show HN: X, a CRM for recruiters", "I built X, a CRM for recruitment agencies. Free trial."),
            post(4, "Looking CRM recruitment agency tomorrow", GOOD["post_text"], days=-9), post(5, "Old need for CRM recruitment agency", GOOD["post_text"], days=500)]
    s = scan(P_CRM, Fake("reddit", "Reddit", recs))
    c = s["counts"]
    assert c["retrieved"] == 6 and c["duplicates"] == 1 and c["evaluated"] == 5 and c["displayed"] + c["excluded"] == c["evaluated"] and c["reconciled"] is True
    assert len(s["excluded"]) == c["excluded"] and len(s["leads"]) == c["displayed"]


def test_a_failing_connector_cannot_break_the_scan_or_hide_the_others():
    s = scan(P_CRM, Fake("hackernews", "Hacker News", boom=True), Fake("reddit", "Reddit", [GOOD]))
    assert s["counts"]["displayed"] == 1 and s["coverage"]["status"] == "partial"


def test_timings_are_recorded_per_stage():
    t = scan(P_CRM, Fake("reddit", "Reddit", [GOOD]))["timings"]
    assert set(t) >= {"concepts_ms", "retrieval_ms", "dedupe_ms", "qualify_ms", "total_ms"} and t["total_ms"] >= 0


# ── connectors (the real adapters, with injected I/O) ────────────────────────────────────────────────────────────────────
def test_the_hacker_news_adapter_reports_the_honest_status_of_the_live_fetch():
    def get_fail(url, timeout):
        raise OSError("down")
    out = lc.HackerNewsConnector(http_get=get_fail, now=NOW).fetch([{"query": "crm for recruiters", "keywords": []}], 30, 20)
    assert out["status"] == "failed" and out["records"] == []
    # The production http_get returns PARSED JSON (a dict), so the fixture is a dict; the expected record is REQUIRED, not merely allowed.
    payload = {"hits": [{"objectID": "99", "title": "Ask HN: CRM for recruiters?", "story_text": "We are looking for a CRM for our recruitment agency. Any recommendations?",
                         "author": "dave", "created_at_i": int((NOW - timedelta(days=2)).timestamp()), "url": None}], "nbPages": 1}
    ok = lc.HackerNewsConnector(http_get=lambda url, timeout: payload, now=NOW).fetch([{"query": "crm for recruiters", "keywords": []}], 30, 20)
    assert ok["status"] == "ok" and len(ok["records"]) == 1 and ok["records"][0]["post_url"] == "https://news.ycombinator.com/item?id=99"


def test_the_reddit_adapter_turns_an_exception_into_a_failed_source_not_an_empty_one():
    def boom(q, d, l):
        raise RuntimeError("db")
    assert lc.StoredRedditConnector(boom).fetch([{"query": "q"}], 30, 5)["status"] == "failed"
    assert lc.StoredRedditConnector(lambda q, d, l: []).fetch([{"query": "q"}], 30, 5)["status"] == "empty"
    assert lc.StoredRedditConnector(lambda q, d, l: [GOOD]).fetch([{"query": "q"}], 30, 5)["status"] == "ok"


# ── persistence and ownership ────────────────────────────────────────────────────────────────────────────────────────────
def _store():
    return ls.LeadStore(Coll(), Coll(), AtomicColl())


def test_leads_are_owner_scoped_and_ids_do_not_cross_owners():
    st = _store()
    st.save_scan("a@x.com", scan(P_CRM, Fake("reddit", "Reddit", [GOOD])))
    lead = st.list_leads("a@x.com")[0]
    assert st.list_leads("b@x.com") == [] and st.get_lead("b@x.com", lead["lead_id"]) is None
    assert st.set_status("b@x.com", lead["lead_id"], "saved") is None and st.set_notes("b@x.com", lead["lead_id"], "x") is None
    assert st.get_lead("a@x.com", lead["lead_id"])["status"] == "new"
    for guest in ("anon-1", None, ""):
        with pytest.raises(PermissionError):
            st.list_leads(guest)


def test_status_transitions_and_unknown_status():
    st = _store()
    st.save_scan("a@x.com", scan(P_CRM, Fake("reddit", "Reddit", [GOOD])))
    lid = st.list_leads("a@x.com")[0]["lead_id"]
    assert st.set_status("a@x.com", lid, "saved")["status"] == "saved"
    assert st.set_status("a@x.com", lid, "contacted")["status"] == "contacted"
    assert st.set_status("a@x.com", lid, "not_interested")["status"] == "not_interested"
    with pytest.raises(ValueError):
        st.set_status("a@x.com", lid, "won")
    assert st.counts("a@x.com")["not_interested"] == 1 and st.counts("a@x.com")["total"] == 1


def test_rescanning_the_same_conversation_keeps_its_status_and_does_not_double_count():
    st = _store()
    st.save_scan("a@x.com", scan(P_CRM, Fake("reddit", "Reddit", [GOOD])))
    lid = st.list_leads("a@x.com")[0]["lead_id"]
    st.set_status("a@x.com", lid, "contacted")
    st.set_notes("a@x.com", lid, "emailed")
    st.save_scan("a@x.com", scan(P_CRM, Fake("reddit", "Reddit", [GOOD])))
    rows = st.list_leads("a@x.com")
    assert len(rows) == 1 and rows[0]["status"] == "contacted" and rows[0]["notes"] == "emailed" and rows[0]["lead_id"] == lid
    assert len(st.list_scans("a@x.com")) == 2 and st.counts("a@x.com")["new"] == 0


# ── the real pages ───────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def web(lg, monkeypatch):
    from starlette.testclient import TestClient
    import sys
    index = importlib.import_module("index")
    lr = sys.modules["lead_routes"]              # index includes lead_routes.router on whichever app instance it builds
    who = {"email": "owner@x.com"}
    monkeypatch.setattr(lr, "get_current_user", lambda request: ({"email": who["email"]} if who["email"] else None))
    monkeypatch.setattr(lr, "_STORES", {"profiles": bp.ProfileStore(Coll()), "leads": ls.LeadStore(Coll(), Coll(), AtomicColl())})
    monkeypatch.setattr(lc, "available_sources", lambda: [{"key": "reddit", "label": "Reddit", "kind": "Stored conversations (historical)"},
                                                          {"key": "hackernews", "label": "Hacker News", "kind": "Live stories (comments not searched)"}])
    state = {"records": [GOOD], "calls": []}

    def factory(selected):
        state["calls"].append(list(selected))
        return [Fake(k, k.capitalize(), state["records"] if k == "reddit" else [], path="historical") for k in selected]
    monkeypatch.setattr(lr, "CONNECTOR_FACTORY", factory)
    monkeypatch.setattr(lr, "_FETCH", lambda url: (_ for _ in ()).throw(sf.FetchFailed("The website could not be reached.")))
    c = TestClient(index.app, base_url="https://testserver")
    return c, who, state, lr


FORM = {"business_name": "RecruitFlow", "sells": "CRM software for recruitment agencies", "ideal_customers": "recruitment agencies",
        "problems_solved": "tracking candidates", "geo_scope": "worldwide"}


def test_anonymous_visitors_are_sent_to_sign_in_on_every_lead_page(web):
    c, who, *_ = web
    who["email"] = None
    for path in ("/leads", "/leads/profile", "/leads/find", "/leads/my", "/leads/scan/x", "/leads/item/x"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", path
    assert c.post("/leads/profile", data=FORM, follow_redirects=False).headers["location"] == "/"
    assert c.post("/leads/find", data={"sources": "reddit"}, follow_redirects=False).headers["location"] == "/"


def test_full_flow_profile_scan_results_detail_status_notes(web):
    c, who, state, lr = web
    r = c.get("/leads", follow_redirects=True)
    assert "Set up your business" in r.text and "Find leads on social media" in r.text
    r = c.post("/leads/profile", data=FORM, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/leads/find"
    page = c.get("/leads/find").text
    assert "Reddit" in page and "Hacker News" in page and "What Flintel will look for" in page
    r = c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/leads/scan/")
    res = c.get(r.headers["location"]).text
    assert "High potential" in res and "Looking for a CRM for our recruitment agency" in res and "Reddit" in res and "Sources searched" in res
    assert "Open original" in res and "https://www.reddit.com/r/test/comments/p1/x/" in res
    lead_id = lr._leads().list_leads("owner@x.com")[0]["lead_id"]
    detail = c.get(f"/leads/item/{lead_id}").text
    assert "Budget is $100/month." in detail and "Suggested opener" in detail and "Open original conversation" in detail and "Author unknown" not in detail
    c.post(f"/leads/item/{lead_id}/status", data={"status": "saved"})
    c.post(f"/leads/item/{lead_id}/notes", data={"notes": "call Monday"})
    assert "call Monday" in c.get(f"/leads/item/{lead_id}").text
    assert lead_id in c.get("/leads/my?status=saved").text and lead_id not in c.get("/leads/my?status=new").text
    over = c.get("/leads").text
    assert "Recent scans" in over and "Saved" in over


def test_the_find_page_offers_only_available_sources_and_a_posted_unavailable_source_is_ignored(web):
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)
    c.post("/leads/find", data={"sources": ["reddit", "x_twitter", "linkedin"], "days": "30"})
    assert state["calls"] == [["reddit"]]
    r = c.post("/leads/find", data={"sources": ["linkedin"], "days": "30"}, follow_redirects=False)
    assert r.headers["location"] == "/leads/find?error=no_source"
    assert "Choose at least one source" in c.get("/leads/find?error=no_source").text


def test_a_failed_source_is_shown_as_incomplete_on_the_results_page(web):
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)
    lr.CONNECTOR_FACTORY = lambda sel: [Fake("reddit", "Reddit", [GOOD]), Fake("hackernews", "Hacker News", [], status="failed", path="live")]
    loc = c.post("/leads/find", data={"sources": ["reddit", "hackernews"], "days": "30"}, follow_redirects=False).headers["location"]
    res = c.get(loc).text
    assert "This scan was not complete" in res and "Hacker News failed" in res and "Failed" in res and "more leads may exist" in res


def test_zero_leads_page_distinguishes_a_complete_search_from_an_incomplete_one(web):
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)
    state["records"] = []
    lr.CONNECTOR_FACTORY = lambda sel: [Fake("reddit", "Reddit", [], status="empty")]
    ok = c.get(c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"]).text
    assert "No leads found" in ok and "not proof that no demand exists" in ok and "was not complete" not in ok
    lr.CONNECTOR_FACTORY = lambda sel: [Fake("reddit", "Reddit", [], status="failed")]
    bad = c.get(c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"]).text
    assert "Nothing found, but the scan was incomplete" in bad and "does not show that no demand exists" in bad


def test_user_b_cannot_see_or_change_user_as_profile_scans_or_leads(web):
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)
    loc = c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"]
    lead_id = lr._leads().list_leads("owner@x.com")[0]["lead_id"]
    who["email"] = "intruder@x.com"
    assert "RecruitFlow" not in c.get("/leads").text and "Set up your business" in c.get("/leads").text
    assert "RecruitFlow" not in c.get("/leads/profile").text
    assert c.get(loc, follow_redirects=False).headers["location"] == "/leads"
    assert c.get(f"/leads/item/{lead_id}", follow_redirects=False).headers["location"] == "/leads/my"
    c.post(f"/leads/item/{lead_id}/status", data={"status": "not_interested"})
    c.post(f"/leads/item/{lead_id}/notes", data={"notes": "hacked"})
    who["email"] = "owner@x.com"
    lead = lr._leads().get_lead("owner@x.com", lead_id)
    assert lead["status"] == "new" and lead["notes"] == ""


def test_the_daily_scan_cap_is_enforced(web, monkeypatch):
    c, who, state, lr = web
    monkeypatch.setenv("LEAD_SCANS_PER_DAY", "1")
    c.post("/leads/profile", data=FORM)
    assert c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"].startswith("/leads/scan/")
    assert c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"] == "/leads/find?error=cap"
    assert len(state["calls"]) == 1


def test_profile_validation_errors_are_shown_and_nothing_is_saved(web):
    c, who, state, lr = web
    r = c.post("/leads/profile", data={"business_name": "", "sells": "x", "geo_scope": "city"})
    assert r.status_code == 422 and "Describe what your business sells" in r.text and "Tell us which place you serve" in r.text
    assert lr._profiles().get("owner@x.com") is None


def test_an_unsafe_website_is_never_fetched_and_the_profile_still_saves(web):
    c, who, state, lr = web
    called = []
    lr._FETCH = lambda url: called.append(url) or (_ for _ in ()).throw(AssertionError("must not fetch"))
    import business_profile as b
    orig = b.analyze_website
    b.analyze_website = lambda url, fetch=None: orig(url, lambda u: sf.fetch_public_page(u, resolve=lambda h, p: ["10.0.0.1"]))
    try:
        r = c.post("/leads/profile", data=dict(FORM, website="http://internal.example"), follow_redirects=False)
    finally:
        b.analyze_website = orig
    assert r.status_code == 303 and called == []
    prof = lr._profiles().get("owner@x.com")
    assert prof["website"]["status"] == "unsafe" and prof["website"]["facts"] == {}
    assert "could not be read" in c.get("/leads/profile").text or "not a public website" in c.get("/leads/profile").text


def test_user_text_is_escaped_in_every_page(web):
    c, who, state, lr = web
    evil = '<script>window.__pwn=1</script>'
    c.post("/leads/profile", data=dict(FORM, business_name=evil, sells="CRM software for <img src=x onerror=alert(1)> recruitment agencies"))
    state["records"] = [dict(GOOD, title="Looking for a CRM for our recruitment agency " + evil, author=evil)]
    loc = c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"]
    lead_id = lr._leads().list_leads("owner@x.com")[0]["lead_id"]
    for path in ("/leads", "/leads/profile", "/leads/find", loc, "/leads/my", f"/leads/item/{lead_id}"):
        body = c.get(path).text
        assert "<script>window.__pwn" not in body and "<img src=x onerror" not in body, path


def test_a_javascript_link_in_a_post_is_never_rendered_as_an_anchor(web):
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)
    state["records"] = [dict(GOOD, post_url="https://www.reddit.com/r/test/comments/p1/x/")]
    loc = c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"]
    lr._leads().leads.docs[0]["url"] = "javascript:alert(1)"
    lead_id = lr._leads().list_leads("owner@x.com")[0]["lead_id"]
    assert 'href="javascript:' not in c.get(loc).text and 'href="javascript:' not in c.get(f"/leads/item/{lead_id}").text


def test_the_profile_page_round_trips_and_does_not_refetch_an_analyzed_website(web):
    c, who, state, lr = web
    fetches = []
    lr._FETCH = lambda url: fetches.append(url) or {"text": "<title>Acme CRM</title><h1>Track candidates</h1>"}
    c.post("/leads/profile", data=dict(FORM, website="acme.com"))
    assert fetches == ["https://acme.com"] and lr._profiles().get("owner@x.com")["website"]["status"] == "analyzed"
    page = c.get("/leads/profile").text
    assert "RecruitFlow" in page and "CRM software for recruitment agencies" in page and "Acme CRM" in page
    c.post("/leads/profile", data=dict(FORM, website="acme.com", sells="CRM software for recruitment agencies and staffing firms"))
    assert fetches == ["https://acme.com"]                                   # unchanged address already read: no second fetch
    assert "staffing firms" in c.get("/leads/profile").text
