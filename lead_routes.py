"""
lead_routes.py — the customer-facing lead-generation pages: Overview, Business Profile, Find Leads, scan results, My Leads, lead detail.

Exposes an APIRouter that index.py includes on the existing FastAPI `app` (at the bottom, next to routes.py). Every page requires a signed-in
account; every store call passes the signed-in e-mail as the owner, so one user can never read or change another user's profile, scans or
leads. State-changing requests are POSTs and pass the existing same-origin guard (origin_guard) like every other POST.

A scan is USER-TRIGGERED and synchronous: the connectors have their own deadlines (Hacker News: 12 s) and the page is only rendered after
the scan ends, so nothing hangs and nothing is claimed about background monitoring. A daily scan cap (LEAD_SCANS_PER_DAY, default 5) bounds
retrieval and model-free processing cost; it is a safety limit, not a pricing plan.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

import business_profile
import lead_connectors
import lead_engine
import lead_store
from index import get_current_user, templates, log

def _now():
    return datetime.now(timezone.utc)


def _aged(leads):
    """Stored leads re-aged at display time (see lead_engine.refresh_lead)."""
    now = _now()
    return [lead_engine.refresh_lead(l, now) for l in leads]


router = APIRouter()           # index.py includes this router on its app (so a re-imported app always gets the routes)
PERIODS = (7, 30, 90)
# Overridable in tests; production binds the real stores lazily so importing this module never needs the database.
_STORES = {"profiles": None, "leads": None}
_FETCH = None                 # website fetch override (tests); default is safe_fetch.fetch_public_page
CONNECTOR_FACTORY = None      # tests replace this: factory(selected_keys) -> [connector, ...]


def _profiles():
    if _STORES["profiles"] is None:
        from database import business_profiles_collection
        _STORES["profiles"] = business_profile.ProfileStore(business_profiles_collection)
    return _STORES["profiles"]


def _leads():
    if _STORES["leads"] is None:
        from database import lead_quota_collection, lead_scans_collection, leads_collection
        _STORES["leads"] = lead_store.LeadStore(lead_scans_collection, leads_collection, lead_quota_collection)
    return _STORES["leads"]


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _scan_cap() -> int:
    return _int_env("LEAD_SCANS_PER_DAY", 5)


def _profile_fetch_cap() -> int:
    return _int_env("LEAD_PROFILE_FETCHES_PER_DAY", 10)


SOURCE_DEADLINE = 30.0        # seconds every source shares inside one scan (lead_engine.run_scan)
SCAN_HARD_LIMIT = 45.0        # seconds the request waits for the whole scan before it is given up on
WEBSITE_HARD_LIMIT = 15.0     # seconds the request waits for a website analysis
LOCK_SECONDS = 90             # the per-owner scan lock expires on its own


def _owner(request: Request):
    user = get_current_user(request)
    return (user, user["email"]) if user and user.get("email") else (user, None)


def _render(request, name, user, status_code=200, **ctx):
    return templates.TemplateResponse(request, name, dict(ctx, user=user, nav=ctx.get("nav", "")), status_code=status_code)


def _need_login(request: Request):
    """Send a signed-out visitor to the sign-in page, and remember where they were going so the home page can bring them back after sign-in
    (routes.home reads `post_login_next`). Only a GET to a /leads path is remembered; nothing from the query string except that path."""
    try:
        if request.method == "GET" and request.url.path.startswith("/leads"):
            request.session["post_login_next"] = request.url.path
    except Exception:                                           # no session middleware (unit tests): just redirect
        pass
    return RedirectResponse("/", status_code=303)


def _default_connectors(selected):
    out = []
    if "hackernews" in selected:
        out.append(lead_connectors.HackerNewsConnector())
    if "reddit" in selected:
        def _reddit(queries, since_days, limit):
            import logics
            return logics.get_matched_signals("lead-scan", queries, "reddit", limit=limit, since_days=since_days, user_query=" ".join(queries[:2]))
        out.append(lead_connectors.StoredRedditConnector(_reddit))
    return out


# ── pages ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
@router.get("/leads")
def leads_overview(request: Request):
    user, owner = _owner(request)
    if not owner:
        return _need_login(request)
    profile = _profiles().get(owner)
    counts = _leads().counts(owner, _now()) if profile else None
    scans = _leads().list_scans(owner, limit=5) if profile else []
    return _render(request, "leads_overview.html", user, nav="overview", profile=profile, counts=counts, scans=scans)


@router.get("/leads/profile")
def leads_profile(request: Request, saved: int = 0):
    user, owner = _owner(request)
    if not owner:
        return _need_login(request)
    profile = _profiles().get(owner)
    return _render(request, "leads_profile.html", user, nav="profile", profile=profile, form=_form_values(profile), errors={}, saved=bool(saved))


def _form_values(profile):
    u = (profile or {}).get("user") or {}
    geo = u.get("geography") or {}
    return {"business_name": u.get("business_name", ""), "website": u.get("website") or "", "sells": u.get("sells", ""),
            "ideal_customers": u.get("ideal_customers", ""), "problems_solved": u.get("problems_solved", ""),
            "geo_scope": geo.get("scope", "worldwide"), "geo_places": ", ".join(geo.get("places") or []),
            "competitors": ", ".join(u.get("competitors") or []), "product_terms": ", ".join(u.get("product_terms") or []),
            "exclusions": ", ".join(u.get("exclusions") or []), "notes": u.get("notes", "")}


@router.post("/leads/profile")
async def leads_profile_save(request: Request):
    user, owner = _owner(request)
    if not owner:
        return _need_login(request)
    form = {k: str(v) for k, v in (await request.form()).items()}
    user_fields, errors = business_profile.validate(form)
    existing = await run_in_threadpool(_profiles().get, owner)
    if errors:
        return _render(request, "leads_profile.html", user, status_code=422, nav="profile", profile=existing, form=form, errors=errors, saved=False)
    website = None
    if user_fields["website"]:
        old = ((existing or {}).get("website") or {})
        if old.get("url") == user_fields["website"] and old.get("status") == "analyzed":
            website = old                                       # unchanged address already read: no second fetch
        elif not await run_in_threadpool(_leads().claim, owner, "profile_fetch", _profile_fetch_cap()):
            website = {"url": user_fields["website"], "status": "not_analyzed", "facts": {},
                       "message": "You have reached today's limit for website checks, so your website was saved but not read."}
        else:
            try:                                                # the fetch has its own deadlines; this is the request-level backstop
                website = await asyncio.wait_for(run_in_threadpool(business_profile.analyze_website, user_fields["website"], _FETCH), WEBSITE_HARD_LIMIT)
            except asyncio.TimeoutError:
                website = {"url": user_fields["website"], "status": "unavailable", "facts": {},
                           "message": "The website took too long to answer. Please describe your business below."}
    profile = business_profile.build(user_fields, website)
    if not user_fields["business_name"]:
        facts = (website or {}).get("facts") or {}
        if (website or {}).get("status") == "analyzed" and facts.get("title"):
            user_fields["business_name"] = facts["title"][:120]      # inferred from the verified page title, editable on the next visit
            profile = business_profile.build(user_fields, website)
        else:
            errors = {"business_name": "We could not read your website. Please enter your business name."}
            return _render(request, "leads_profile.html", user, status_code=422, nav="profile", profile=existing, form=form, errors=errors, saved=False)
    await run_in_threadpool(_profiles().save, owner, profile)
    return RedirectResponse("/leads/profile?saved=1" if existing else "/leads/find", status_code=303)


@router.get("/leads/find")
def leads_find(request: Request, error: str = ""):
    user, owner = _owner(request)
    if not owner:
        return _need_login(request)
    profile = _profiles().get(owner)
    if not profile:
        return RedirectResponse("/leads/profile", status_code=303)
    sources = lead_connectors.available_sources()
    concepts = lead_engine.search_concepts(profile)
    return _render(request, "leads_find.html", user, nav="find", profile=profile, sources=sources, periods=PERIODS, concepts=concepts[:6],
                   error=error, cap=_scan_cap())


@router.post("/leads/find")
async def leads_find_start(request: Request):
    user, owner = _owner(request)
    if not owner:
        return _need_login(request)
    profile = await run_in_threadpool(_profiles().get, owner)
    if not profile:
        return RedirectResponse("/leads/profile", status_code=303)
    form = await request.form()
    allowed = {s["key"] for s in lead_connectors.available_sources()}
    selected = [k for k in form.getlist("sources") if k in allowed]
    try:
        days = int(form.get("days") or 30)
    except ValueError:
        days = 30
    days = days if days in PERIODS else 30
    if not selected:
        return RedirectResponse("/leads/find?error=no_source", status_code=303)
    store = _leads()
    if not await run_in_threadpool(store.acquire_scan_lock, owner, LOCK_SECONDS):
        return RedirectResponse("/leads/find?error=busy", status_code=303)     # a scan of this account is already running (double click, second tab)
    try:
        if not await run_in_threadpool(store.claim, owner, "scan", _scan_cap()):
            return RedirectResponse("/leads/find?error=cap", status_code=303)  # the claim is atomic and is NOT given back after a failure
        factory = CONNECTOR_FACTORY or _default_connectors
        try:
            scan = await asyncio.wait_for(run_in_threadpool(lambda: lead_engine.run_scan(profile, factory(selected), since_days=days, source_deadline=SOURCE_DEADLINE)),
                                          SCAN_HARD_LIMIT)
            saved = await run_in_threadpool(store.save_scan, owner, scan)
        except Exception as exc:                                # a scan must end with a clear message, never a blank page or a hung request
            log.warning(f"Lead scan failed: {type(exc).__name__}")
            return RedirectResponse("/leads/find?error=failed", status_code=303)
        return RedirectResponse(f"/leads/scan/{saved['scan_id']}", status_code=303)
    finally:
        await run_in_threadpool(store.release_scan_lock, owner)


@router.get("/leads/scan/{scan_id}")
def leads_scan(request: Request, scan_id: str):
    user, owner = _owner(request)
    if not owner:
        return _need_login(request)
    scan = _leads().get_scan(owner, scan_id)
    if not scan:
        return RedirectResponse("/leads", status_code=303)
    leads = _aged(_leads().list_leads(owner, scan_id=scan_id))
    leads.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    leads.sort(key=lead_engine.sort_key)
    counts = dict(scan.get("counts") or {})                    # the header numbers follow the aged leads, not the values stored at scan time
    counts.update(high_potential=sum(1 for l in leads if l["label"] == "High potential"), current_leads=sum(1 for l in leads if l.get("is_current")),
                  historical_or_undated=sum(1 for l in leads if l.get("recency") != "recent"))
    scan = dict(scan, counts=counts, statement=lead_engine.scan_statement(counts, scan.get("coverage") or {"status": "unavailable", "reasons": []}))
    return _render(request, "leads_scan.html", user, nav="find", scan=scan, leads=leads, profile=_profiles().get(owner))


@router.get("/leads/my")
def leads_my(request: Request, status: str = ""):
    user, owner = _owner(request)
    if not owner:
        return _need_login(request)
    status = status if status in lead_store.STATUSES else ""
    leads = _aged(_leads().list_leads(owner, status=status or None))
    leads.sort(key=lambda x: x.get("last_seen_at") or "", reverse=True)
    leads.sort(key=lead_engine.sort_key)
    return _render(request, "leads_list.html", user, nav="my", leads=leads, status=status, counts=_leads().counts(owner, _now()))


@router.get("/leads/item/{lead_id}")
def leads_detail(request: Request, lead_id: str):
    user, owner = _owner(request)
    if not owner:
        return _need_login(request)
    lead = _leads().get_lead(owner, lead_id)
    if not lead:
        return RedirectResponse("/leads/my", status_code=303)
    lead = lead_engine.refresh_lead(lead, _now())
    return _render(request, "leads_detail.html", user, nav="my", lead=lead, statuses=lead_store.STATUSES)


@router.post("/leads/item/{lead_id}/status")
async def leads_set_status(request: Request, lead_id: str):
    user, owner = _owner(request)
    if not owner:
        return _need_login(request)
    form = await request.form()
    try:
        await run_in_threadpool(_leads().set_status, owner, lead_id, str(form.get("status") or ""))
    except ValueError:
        pass
    return RedirectResponse(f"/leads/item/{lead_id}", status_code=303)


@router.post("/leads/item/{lead_id}/notes")
async def leads_set_notes(request: Request, lead_id: str):
    user, owner = _owner(request)
    if not owner:
        return _need_login(request)
    form = await request.form()
    await run_in_threadpool(_leads().set_notes, owner, lead_id, str(form.get("notes") or ""))
    return RedirectResponse(f"/leads/item/{lead_id}", status_code=303)
