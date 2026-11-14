"""Entry point, sign-in return, rating explanations and the progress UI in a real browser."""
import pytest

from tests.test_guest_gate import CHAT, render
from tests.test_lead_mvp import FORM, GOOD, Fake, web, lg, env  # noqa: F401
from tests.test_lead_mvp_ui import _html  # noqa: F401

USER = {"name": "Ada", "email": "ada@example.com", "picture": None}


@pytest.mark.parametrize("name,ctx", [("index.html", {"user": USER}), ("chat.html", {"chat": CHAT, "chat_id": "c1", "user": USER})])
def test_the_existing_application_links_to_find_leads(name, ctx):
    html = render(name, **ctx)
    assert 'id="leads-link"' in html and 'href="/leads"' in html and "Find Leads" in html


def test_a_signed_out_visitor_is_brought_back_to_the_page_they_wanted_after_signing_in(web, monkeypatch):
    c, who, state, lr = web
    import sys
    routes = sys.modules["routes"]
    who["email"] = None
    r = c.get("/leads/my", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    monkeypatch.setattr(routes, "get_current_user", lambda request: {"email": "owner@x.com", "name": "O"})      # the visitor has now signed in
    back = c.get("/", follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/leads/my"
    again = c.get("/", follow_redirects=False)                                                                    # remembered once, never again
    assert again.status_code == 200


def test_only_leads_paths_are_remembered_and_a_post_is_not(web, monkeypatch):
    c, who, state, lr = web
    import sys
    routes = sys.modules["routes"]
    who["email"] = None
    c.post("/leads/profile", data=FORM, follow_redirects=False)                 # a state-changing request is not replayed
    monkeypatch.setattr(routes, "get_current_user", lambda request: {"email": "owner@x.com", "name": "O"})
    assert c.get("/", follow_redirects=False).status_code == 200


def test_the_results_and_list_pages_explain_the_ratings_in_plain_words(web):
    c, who, state, lr = web
    c.post("/leads/profile", data=FORM)
    loc = c.post("/leads/find", data={"sources": ["reddit"], "days": "30"}, follow_redirects=False).headers["location"]
    for page in (c.get(loc).text, c.get("/leads/my").text):
        assert "How Flintel rates a lead" in page and "High potential" in page and "Needs review" in page and "never contacts anyone" in page


def test_the_find_button_is_disabled_after_one_click_and_progress_is_shown(integrity_browser, web):
    pages = _html(web)
    ctx = integrity_browser.new_context(viewport={"width": 375, "height": 812})
    pg = ctx.new_page()
    posts = []
    pg.on("request", lambda r: posts.append(r) if r.method == "POST" else None)
    pg.route("**/*", lambda route: route.abort())
    pg.set_content(pages["find"], wait_until="domcontentloaded")
    assert pg.locator("#scan-progress").is_hidden() and pg.locator("#scan-button").is_enabled()
    pg.locator("#scan-button").click()
    assert pg.locator("#scan-button").is_disabled() and pg.locator("#scan-progress").is_visible() and "Searching" in pg.inner_text("#scan-button")
    assert "usually takes under a minute" in pg.inner_text("#scan-progress")
    pg.locator("#scan-form").evaluate("f => f.requestSubmit()")                 # a second submit (Enter key, double click) is ignored
    assert len(posts) <= 1
    ctx.close()


def test_a_scan_form_with_no_source_chosen_does_not_get_stuck_disabled(integrity_browser, web):
    pages = _html(web)
    ctx = integrity_browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    pg.route("**/*", lambda route: route.abort())
    pg.set_content(pages["find"], wait_until="domcontentloaded")
    for cb in pg.locator("input[name=sources]").all():
        cb.uncheck()
    pg.locator("#scan-button").click()
    assert pg.locator("#scan-button").is_enabled()
    ctx.close()


# ── the REAL sign-in form endpoint (POST /login) brings a visitor back to the page they wanted ─────────────────────────────
from tests.test_guest_gate import auth, gg  # noqa: E402,F401
from tests.test_lead_mvp import AtomicColl, Coll  # noqa: E402
import business_profile as _bp  # noqa: E402
import lead_store as _ls  # noqa: E402

JSON_HDR = {"Accept": "application/json"}


@pytest.fixture
def lead_auth(auth, monkeypatch):
    import sys
    lr = sys.modules["lead_routes"]
    monkeypatch.setattr(lr, "get_current_user", auth["idx"].get_current_user)                 # the module may be bound to an earlier test's app instance
    monkeypatch.setattr(lr, "_STORES", {"profiles": _bp.ProfileStore(Coll()), "leads": _ls.LeadStore(Coll(), Coll(), AtomicColl())})
    auth["users"].insert_one({"email": "u@example.com", "password_hash": "h:right-password"})
    auth["lr"] = lr
    return auth


def _real_login(a, **extra):
    return a["client"].post("/login", data={"email": "u@example.com", "password": "right-password", **extra}, headers=JSON_HDR)


def test_the_real_login_endpoint_returns_the_leads_page_the_visitor_wanted(lead_auth):
    c = lead_auth["client"]
    assert c.get("/leads/my", follow_redirects=False).headers["location"] == "/"
    r = _real_login(lead_auth)
    assert r.json() == {"ok": True, "redirect": "/leads/my"}
    assert "My leads" in c.get(r.json()["redirect"]).text                                       # and that page opens for the now signed-in visitor
    again = c.post("/logout", follow_redirects=False)
    assert _real_login(lead_auth).json()["redirect"] == "/"                                       # remembered once, never again


def test_the_plain_form_login_redirects_back_too(lead_auth):
    c = lead_auth["client"]
    c.get("/leads/profile", follow_redirects=False)
    r = c.post("/login", data={"email": "u@example.com", "password": "right-password"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/leads/profile"


def test_a_failed_login_keeps_the_destination_and_a_later_success_uses_it(lead_auth):
    c = lead_auth["client"]
    c.get("/leads/find", follow_redirects=False)
    bad = c.post("/login", data={"email": "u@example.com", "password": "wrong"}, headers=JSON_HDR)
    assert bad.status_code == 401 and bad.json()["ok"] is False
    assert _real_login(lead_auth).json()["redirect"] == "/leads/find"


def test_a_visitor_who_never_asked_for_a_leads_page_still_lands_on_the_home_page(lead_auth):
    assert _real_login(lead_auth).json()["redirect"] == "/"


def test_only_a_leads_path_is_ever_used_as_the_destination(lead_auth, monkeypatch):
    c = lead_auth["client"]
    c.get("/leads/my", follow_redirects=False)                                                    # sets the session cookie
    import sys
    rt = sys.modules["routes"]
    for evil in ("//evil.example/leads", "https://evil.example/", "/billing/upgrade", "javascript:alert(1)"):
        class _Req:
            session = {"post_login_next": evil}
        assert rt._post_auth_target(_Req(), None, "u@example.com") == "/"
