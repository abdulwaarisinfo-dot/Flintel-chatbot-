"""
tests/test_reddit_live_endpoint.py — the live Reddit links endpoint, template and browser code. MOCKED ONLY.

Three layers, all offline:
  * the route handler called directly (stubbed app modules, as in the billing tests);
  * the REAL application (index.app: SessionMiddleware, the app's own middlewares, FastAPI routing and Form parsing) driven by
    Starlette's TestClient with a genuine signed session cookie;
  * the real chat.html (Jinja, autoescape as the app configures it) and the real shipped JavaScript in the preinstalled
    Chromium against a route-mocked fetch.
No OpenAI request, no network, no production database: the database is mongomock, the API call is a fake callable.
"""
import base64
import inspect
import json
import re
import socket
import sys
import types
from pathlib import Path

import jinja2
import pytest
from starlette.testclient import TestClient   # imported BEFORE any fixture stubs httpx (the old TestClient imports httpx on first import)

mongomock = pytest.importorskip("mongomock")
from tests.test_answer_persistence import env  # noqa: F401,E402  (fixture: stubbed app modules)
from tests.test_billing_lemonsqueezy import app, cfg, db, make_request  # noqa: F401,E402  (fixtures)
from tests.test_reddit_live import Env, answer, message, on, OWNER, NOW, URL1  # noqa: F401,E402
import reddit_live as rl  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HDR = {"x-flintel-live-links": "1"}
MALLORY = "mallory@example.com"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def _no_inflight_leak():
    assert rl.INFLIGHT.count == 0
    yield
    assert rl.INFLIGHT.count == 0, "an in-flight slot was leaked"


# ── the route handler, called directly ───────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def live(app, on, monkeypatch):
    """The route wired to a mongomock/fake-API Env as the signed-in OWNER."""
    e = Env()
    built = {"n": 0}

    def deps():
        built["n"] += 1
        return e.deps
    monkeypatch.setattr(app.rt.reddit_live, "default_deps", deps)
    who = {"v": (OWNER, "email")}
    monkeypatch.setattr(app.rt, "get_owner", lambda request: who["v"])
    credit_calls = []
    import search_credits
    monkeypatch.setattr(search_credits, "default_store", lambda *a, **k: credit_calls.append("default_store") or None)
    return types.SimpleNamespace(app=app, env=e, built=built, who=who, credit_calls=credit_calls)


def post(live, headers=HDR, chat_id="c1", request_id="r0", topic_key=None):
    req = make_request(live.app.idx, f"/chat/{chat_id}/live-links", headers=headers)
    resp = live.app.rt.live_links(req, chat_id=chat_id, request_id=request_id, topic_key=topic_key)
    return resp.status_code, json.loads(resp.body.decode())


def test_route_when_disabled_does_nothing(live, monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_LINKS_ENABLED", False, raising=False)
    assert post(live) == (200, {"state": "disabled"})
    assert live.built["n"] == 0 and live.env.calls == [] and live.env.stored() is None


def test_route_requires_the_custom_header(live):
    assert post(live, headers={})[0] == 403
    assert post(live, headers={"x-flintel-live-links": "0"})[0] == 403
    assert live.built["n"] == 0 and live.env.calls == []


def test_route_rejects_a_foreign_origin_and_accepts_the_same_site(live):
    assert post(live, headers={**HDR, "origin": "https://evil.example"})[0] == 403
    assert live.env.calls == []
    code, body = post(live, headers={**HDR, "origin": "http://testserver"})
    assert code == 200 and body["state"] == "ok"


def test_route_needs_a_message_identifier(live):
    assert post(live, request_id=None, topic_key=None)[0] == 400 and live.env.calls == []


def test_route_happy_path_returns_labelled_links_and_never_touches_credits(live):
    code, body = post(live)
    assert code == 200 and body["state"] == "ok" and body["label"] == "Live web-search links — unverified"
    assert body["note"] == rl.NOTE
    assert body["links"] == [{"url": URL1, "title": "A post title"}]
    assert set(body) == {"state", "label", "note", "links"}
    assert len(live.env.calls) == 1 and live.credit_calls == []


def test_repeated_and_reconnect_posts_cause_one_call(live):
    bodies = [post(live)[1] for _ in range(6)]
    assert all(b == bodies[0] for b in bodies) and len(live.env.calls) == 1


def test_guest_is_ineligible_and_nothing_is_called(live):
    live.who["v"] = ("anon-uuid", "anon")
    assert post(live) == (200, {"state": "ineligible"}) and live.env.calls == []


def test_another_users_chat_is_404(live):
    live.who["v"] = (MALLORY, "email")
    assert post(live)[0] == 404 and live.env.calls == [] and live.env.stored() is None


def test_route_signature_has_no_client_supplied_owner_field(live):
    params = set(inspect.signature(live.app.rt.live_links).parameters)
    assert params == {"request", "chat_id", "request_id", "topic_key"}


def test_route_fails_closed_when_the_store_cannot_be_built(live, monkeypatch):
    def boom():
        raise RuntimeError("no database")
    monkeypatch.setattr(live.app.rt.reddit_live, "default_deps", boom)
    assert post(live) == (200, {"state": "unavailable"})


def test_stream_and_background_source_does_not_mention_the_feature(env):
    """String check only; the diff itself (no hunk inside these functions) is what shows they are unchanged."""
    lg, idx, _ = env
    rt = sys.modules["routes"]
    for fn in (rt.stream_answer, idx._complete_message_answer_and_results, idx._fill_in_message_outputs, lg.analyze_with_claude,
               lg._finalize_answer_and_results):
        src = inspect.getsource(fn)
        assert "reddit_live" not in src and "live_links" not in src and "live_reddit" not in src, fn.__name__


# ── the REAL application: middleware, signed session cookie, routing and Form parsing ───────────────────────────────────
@pytest.fixture
def asgi(env, on, monkeypatch):
    from bson import ObjectId
    from itsdangerous import TimestampSigner
    lg, idx, _ = env
    rt = sys.modules["routes"]
    users = mongomock.MongoClient()["asgi"]["users"]
    alice = users.insert_one({"email": OWNER, "name": "Alice", "password_hash": "x"}).inserted_id
    mallory = users.insert_one({"email": MALLORY, "name": "M"}).inserted_id
    monkeypatch.setattr(idx, "users_collection", users)          # get_current_user reads this
    e = Env()
    built = {"n": 0}

    def deps():
        built["n"] += 1
        return e.deps
    monkeypatch.setattr(rt.reddit_live, "default_deps", deps)

    def cookie(data, secret=None):
        raw = base64.b64encode(json.dumps(data).encode("utf-8"))
        return TimestampSigner(str(secret or idx.SESSION_SECRET_KEY)).sign(raw).decode("utf-8")
    client = TestClient(idx.app, base_url="http://testserver")

    def call(user=None, headers=None, method="post", cookie_value=None, data=None, chat="c1"):
        h = dict(HDR if headers is None else headers)
        value = cookie_value if cookie_value is not None else (cookie({"user_id": str(user)}) if user else None)
        if value:
            h["cookie"] = f"session={value}"
        form = {"request_id": "r0"} if data is None else data
        return getattr(client, method)(f"/chat/{chat}/live-links", data=form, headers=h) if method == "post" \
            else client.get(f"/chat/{chat}/live-links", headers=h)
    return types.SimpleNamespace(call=call, client=client, env=e, built=built, alice=alice, mallory=mallory, cookie=cookie, idx=idx, rt=rt)


def test_asgi_signed_in_owner_gets_links_through_the_real_stack(asgi):
    r = asgi.call(user=asgi.alice)
    assert r.status_code == 200 and r.json()["state"] == "ok" and r.json()["links"][0]["url"] == URL1
    assert len(asgi.env.calls) == 1
    again = asgi.call(user=asgi.alice)
    assert again.json() == r.json() and len(asgi.env.calls) == 1


def test_asgi_guest_without_a_session_is_rejected_and_nothing_is_called(asgi):
    r = asgi.call()
    assert r.status_code == 200 and r.json() == {"state": "ineligible"} and asgi.env.calls == []
    assert asgi.env.stored() is None and asgi.env.counter() is None


@pytest.mark.parametrize("how", ["wrong_secret", "tampered", "garbage", "unknown_user"])
def test_asgi_forged_or_invalid_session_cookies_are_treated_as_guest_or_rejected(asgi, how):
    from bson import ObjectId
    if how == "wrong_secret":
        value = asgi.cookie({"user_id": str(asgi.alice)}, secret="some-other-secret-value-xxxxxxxxxxxxxxxx")
    elif how == "tampered":
        good = asgi.cookie({"user_id": str(asgi.mallory)})
        value = good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB")
    elif how == "garbage":
        value = "not-a-cookie"
    else:
        value = asgi.cookie({"user_id": str(ObjectId())})            # validly signed, but no such user: a guest
    r = asgi.call(cookie_value=value)
    assert r.status_code == 200 and r.json() == {"state": "ineligible"} and asgi.env.calls == []
    assert asgi.env.stored() is None


def test_asgi_ownership_is_isolated_between_signed_in_users(asgi):
    r = asgi.call(user=asgi.mallory)                                  # a valid, different user asks for alice's chat
    assert r.status_code == 404 and r.json() == {"state": "not_found"}
    assert asgi.env.calls == [] and asgi.env.stored() is None and asgi.env.counter() is None


def test_asgi_a_client_cannot_name_an_owner(asgi):
    r = asgi.call(user=asgi.mallory, data={"request_id": "r0", "owner_key": OWNER, "owner": OWNER, "email": OWNER, "user_id": str(asgi.alice)})
    assert r.status_code == 404 and asgi.env.calls == []


def test_asgi_missing_custom_header_and_cross_origin_are_forbidden_even_with_a_valid_session(asgi):
    assert asgi.call(user=asgi.alice, headers={}).status_code == 403
    assert asgi.call(user=asgi.alice, headers={"x-flintel-live-links": "yes"}).status_code == 403
    assert asgi.call(user=asgi.alice, headers={**HDR, "origin": "https://evil.example"}).status_code == 403
    assert asgi.call(user=asgi.alice, headers={**HDR, "origin": "null"}).status_code == 403
    assert asgi.env.calls == [] and asgi.env.stored() is None
    assert asgi.call(user=asgi.alice, headers={**HDR, "origin": "http://testserver"}).json()["state"] == "ok"


def test_asgi_only_post_is_accepted(asgi):
    headers = {**HDR, "cookie": f"session={asgi.cookie({'user_id': str(asgi.alice)})}"}
    for method in ("get", "put", "delete", "patch"):
        assert getattr(asgi.client, method)("/chat/c1/live-links", headers=headers).status_code == 405, method
    assert asgi.env.calls == []


def test_asgi_missing_identifier_is_a_bad_request(asgi):
    assert asgi.call(user=asgi.alice, data={}).status_code == 400 and asgi.env.calls == []


def test_asgi_feature_off_never_reaches_the_store(asgi, monkeypatch):
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_LINKS_ENABLED", False, raising=False)
    r = asgi.call(user=asgi.alice)
    assert r.json() == {"state": "disabled"} and asgi.built["n"] == 0 and asgi.env.calls == []


def test_asgi_session_cookie_attributes_set_by_the_real_middleware(asgi):
    """A guest request modifies the session (anon id), so the real SessionMiddleware emits its cookie: check how."""
    r = asgi.call()
    cookies = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    session = [c for c in cookies if c.startswith("session=")]
    assert session, cookies
    low = session[0].lower()
    assert "samesite=lax" in low and "httponly" in low and "domain=" not in low and "path=/" in low
    assert "secure" not in low.replace("samesite", "")            # https_only is False in this app: stated, not endorsed


# ── the template: persisted links ───────────────────────────────────────────────────────────────────────────────────────
def render(msg_extra=None, enabled=None, helper=True):
    # autoescape exactly as the app's Jinja2Templates configures it (starlette: select_autoescape())
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.ChainableUndefined,
                           autoescape=jinja2.select_autoescape())
    e.globals["url_for"] = lambda *a, **k: "#"
    if helper:
        e.globals["live_links_for"] = rl.render_links           # registered by routes.py in the app
    if enabled is not None:
        e.globals["live_links_enabled"] = lambda: enabled
    msg = {"topic_key": "t0", "request_id": "r0", "query": "q", "claude_answer": answer(2),
           "results": [{"post_url": "https://www.reddit.com/r/a/comments/1/x/", "title": "T"}],
           "requested_at": NOW, "answered_at": NOW}
    msg.update(msg_extra or {})
    chat = {"chat_id": "c1", "title": "t", "messages": [msg]}
    return e.get_template("chat.html").render(request=None, chat=chat, chats=[], user={"email": OWNER, "name": "A"},
                                              chat_id="c1", pending_stream_topic_key=None, messages=chat["messages"])


def _live_section(html):
    m = re.search(r'<section class="live-links".*?</section>', html, re.S)
    return m.group(0) if m else None


def _ok_record(links):
    return {"live_reddit": {"state": "ok", "claim_id": "x", "links": links}}


def test_persisted_ok_links_render_in_a_separate_labelled_section_and_are_escaped(on):
    html = render(_ok_record([
        {"url": "https://www.reddit.com/r/SaaS/comments/abc123/ok/", "title": "<script>alert(1)</script> \"q\""},
        {"url": "http://www.reddit.com/r/SaaS/comments/def456/plain-http/", "title": "must not appear"},
        {"url": "https://www.reddit.com/r/SaaS/comments/ghi789/no-title/", "title": None}]))
    sec = _live_section(html)
    assert sec and "Live web-search links — unverified" in sec
    assert "Post dates and recency are unverified" in sec and "live web search" in sec and "just now" not in sec.lower()
    assert "<script>alert(1)</script>" not in sec and "&lt;script&gt;alert(1)&lt;/script&gt;" in sec
    assert "plain-http" not in sec and "must not appear" not in sec
    assert 'href="https://www.reddit.com/r/SaaS/comments/ghi789/no-title/"' in sec and 'rel="noopener noreferrer nofollow ugc"' in sec
    wrap = re.search(r'<div class="answer-wrap".*?</div>\s*<div class="answer-rendered">\s*</div>\s*</div>', html, re.S)
    assert wrap and "live-links" not in wrap.group(0)                     # not inside the answer or its cards


def test_the_page_text_matches_the_server_constants(on):
    sec = _live_section(render(_ok_record([{"url": URL1, "title": "t"}])))
    assert rl.NOTE in sec and rl.LABEL in sec


def test_tampered_stored_links_are_filtered_by_the_same_validator_before_rendering(on):
    html = render(_ok_record([
        {"url": "https://evil.com\\.reddit.com/r/SaaS/comments/abc123/x/", "title": "backslash host"},
        {"url": "https://evıl.reddit.com/r/SaaS/comments/abc123/x/", "title": "lookalike host"},
        {"url": "https://www.reddit.com/user/someone", "title": "profile page"},
        {"url": "javascript:alert(1)", "title": "js"},
        {"url": URL1, "title": "good one"}]))
    sec = _live_section(html)
    assert sec and "good one" in sec
    for bad in ("backslash host", "lookalike host", "profile page", "evil.com", "javascript:"):
        assert bad not in sec, bad
    only_bad = render(_ok_record([{"url": "https://evil.com\\.reddit.com/r/SaaS/comments/abc123/x/", "title": "x"}]))
    assert _live_section(only_bad) is None


def test_nothing_renders_while_the_feature_is_off_or_the_helper_is_absent(on, monkeypatch):
    rec = _ok_record([{"url": URL1, "title": "t"}])
    assert _live_section(render(rec, helper=False)) is None
    monkeypatch.setattr(rl.config, "REDDIT_LIVE_LINKS_ENABLED", False, raising=False)
    assert _live_section(render(rec)) is None


@pytest.mark.parametrize("live_reddit", [None, {"state": "error"}, {"state": "empty", "links": []}, {"state": "skipped"},
                                         {"state": "claiming"}, {"state": "calling"}, {"state": "ok", "links": []}])
def test_nothing_renders_unless_there_are_stored_ok_links(on, live_reddit):
    base = render()
    html = render({"live_reddit": live_reddit} if live_reddit is not None else None)
    assert _live_section(html) is None and html == base                 # byte-identical to a message without the field


def test_matched_results_attribute_is_unchanged_by_live_links(on):
    plain = re.search(r'data-matched-results="([^"]*)"', render()).group(1)
    withlive = re.search(r'data-matched-results="([^"]*)"', render(_ok_record([{"url": URL1, "title": "x"}]))).group(1)
    assert plain == withlive and "abc123" not in withlive


def test_client_flag_follows_the_server_global_and_is_false_when_absent():
    assert "const LIVE_LINKS_ON = false;" in render()
    assert "const LIVE_LINKS_ON = false;" in render(enabled=False)
    assert "const LIVE_LINKS_ON = true;" in render(enabled=True)


# ── the shipped JavaScript, in a real browser, with a mocked fetch ────────────────────────────────────────────────────────
ORIGIN = "http://flintel.test"
PAGE = ("<!doctype html><html><body><div id='turn'><div id='wrap' class='answer-wrap'>answer</div>"
        "<div class='msg-actions-asst'>actions</div></div></body></html>")


def _shipped_js(enabled):
    html = render(enabled=enabled)
    start = html.index("const LIVE_LINKS_ON")
    end = html.index("function wireUpStream")
    return html[start:end]


def _page(browser, enabled, handler):
    page = browser.new_page()
    errors, requests = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("window.__st = window.setTimeout; window.setTimeout = (f, t) => window.__st(f, 0);")

    def route(r):
        url = r.request.url
        if url.endswith("/live-links"):
            requests.append({"method": r.request.method, "headers": r.request.headers, "body": r.request.post_data})
            return handler(r, len(requests))
        return r.fulfill(status=200, headers={"Content-Type": "text/html"}, body=PAGE)
    page.route("**/*", route)
    page.goto(ORIGIN + "/")
    page.add_script_tag(content=_shipped_js(enabled))
    return page, errors, requests


def _json(status, body):
    return lambda r, n: r.fulfill(status=status, headers={"Content-Type": "application/json"}, body=json.dumps(body))


ASK = "requestLiveLinks(document.getElementById('wrap'), 'c1', 't0', 'r0')"


def test_browser_renders_only_text_and_https_links_and_sends_the_protection_header(integrity_browser):
    body = {"state": "ok", "links": [
        {"url": "https://www.reddit.com/r/SaaS/comments/abc123/x/", "title": "<img src=x onerror=window.__pwn=1> Title"},
        {"url": "javascript:window.__pwn=2", "title": "js link"},
        {"url": "http://www.reddit.com/r/SaaS/comments/def456/y/", "title": "plain http"},
        {"url": "https://www.reddit.com/r/SaaS/comments/ghi789/z/", "title": None}]}
    page, errors, requests = _page(integrity_browser, True, _json(200, body))
    page.evaluate(ASK)
    page.wait_for_selector("section.live-links")
    assert page.evaluate("window.__pwn") is None and page.query_selector("section.live-links img") is None
    items = page.eval_on_selector_all("section.live-links li a", "els => els.map(e => [e.href, e.textContent, e.rel, e.target])")
    assert items == [["https://www.reddit.com/r/SaaS/comments/abc123/x/", "<img src=x onerror=window.__pwn=1> Title", "noopener noreferrer nofollow ugc", "_blank"],
                     ["https://www.reddit.com/r/SaaS/comments/ghi789/z/", "https://www.reddit.com/r/SaaS/comments/ghi789/z/", "noopener noreferrer nofollow ugc", "_blank"]]
    text = page.inner_text("section.live-links")
    assert "Live web-search links — unverified" in text and rl.NOTE in text and "just now" not in text.lower()
    assert page.evaluate("document.querySelector('.msg-actions-asst').nextElementSibling.className") == "live-links"
    assert page.evaluate("document.getElementById('wrap').querySelector('.live-links')") is None
    assert len(requests) == 1 and requests[0]["method"] == "POST"
    assert requests[0]["headers"].get("x-flintel-live-links") == "1" and "request_id=r0" in requests[0]["body"]
    assert errors == []
    page.close()


def test_browser_duplicate_done_events_send_one_request(integrity_browser):
    page, errors, requests = _page(integrity_browser, True, _json(200, {"state": "ok", "links": [{"url": URL1, "title": "t"}]}))
    page.evaluate(ASK)
    page.evaluate(ASK)
    page.evaluate(ASK)
    page.wait_for_selector("section.live-links")
    page.wait_for_timeout(200)
    assert len(requests) == 1 and page.eval_on_selector_all("section.live-links", "e => e.length") == 1 and errors == []
    page.close()


@pytest.mark.parametrize("waiting", ["pending", "busy"])
def test_browser_pending_and_busy_are_polled_then_shown(integrity_browser, waiting):
    def handler(r, n):
        state = {"state": waiting} if n < 3 else {"state": "ok", "links": [{"url": URL1, "title": "t"}]}
        return r.fulfill(status=200, headers={"Content-Type": "application/json"}, body=json.dumps(state))
    page, errors, requests = _page(integrity_browser, True, handler)
    page.evaluate(ASK)
    page.wait_for_selector("section.live-links")
    assert len(requests) == 3 and errors == []
    page.close()


def test_browser_gives_up_after_a_bounded_number_of_polls(integrity_browser):
    page, errors, requests = _page(integrity_browser, True, _json(200, {"state": "busy"}))
    page.evaluate(ASK)
    page.wait_for_timeout(500)
    assert len(requests) == 8 and page.query_selector("section.live-links") is None and errors == []
    page.close()


@pytest.mark.parametrize("status,body", [(200, {"state": "none"}), (200, {"state": "unavailable"}), (200, {"state": "ineligible"}),
                                         (200, {"state": "disabled"}), (200, {"state": "ok", "links": []}), (403, {"state": "forbidden"}),
                                         (404, {"state": "not_found"}), (500, {"x": 1})])
def test_browser_shows_nothing_for_every_non_success_outcome(integrity_browser, status, body):
    page, errors, requests = _page(integrity_browser, True, _json(status, body))
    page.evaluate(ASK)
    page.wait_for_timeout(300)
    assert len(requests) == 1 and page.query_selector("section.live-links") is None and errors == []
    page.close()


def test_browser_makes_no_request_when_the_feature_flag_is_off(integrity_browser):
    page, errors, requests = _page(integrity_browser, False, _json(200, {"state": "ok", "links": []}))
    page.evaluate(ASK)
    page.wait_for_timeout(300)
    assert requests == [] and page.query_selector("section.live-links") is None and errors == []
    page.close()
