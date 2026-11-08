# FLINTEL Security Findings

Scope: code review plus local tests only. **No production system, credential or data was touched, nothing was deployed, and no external scan was run.** Findings are from reading the repository and from `pip-audit` output saved locally. "Active" below means enabled by default or enforced at startup; a protection that is off by default is **not** reported as protection.

Status vocabulary: IMPLEMENTED, LOCALLY TESTED, INTEGRATION VERIFIED, STAGING VERIFIED, PRODUCTION READY, PRODUCTION APPROVED. Every item below is at most LOCALLY TESTED.

## F1. Host header steers OAuth redirect_uri and other request-derived URLs
- **Severity:** Medium (High if the app is reachable with an attacker-controlled Host, e.g. no front-end proxy that pins it). **Exploitability:** needs a request carrying a hostile `Host` (or `X-Forwarded-Host`) that reaches the app; reproduced locally with a test client: `request.url_for` followed the header.
- **Evidence:** `routes.py` `google_login` built `redirect_uri` with `request.url_for(...)`. `tests/test_origin_guard.py::test_without_trusted_host_forged_host_steers_url_for` documents the behaviour.
- **Fix (IMPLEMENTED, LOCALLY TESTED):** `_oauth_redirect_uri` builds the URI from `SITE_URL` plus the route path whenever `SITE_URL` is set, so no header can influence it (`tests/test_login_throttle_and_oauth.py`). `TRUSTED_HOSTS` adds a Host allow-list (400 for others). Staging **refuses to start** unless `SITE_URL` is https, `TRUSTED_HOSTS` lists its host (no wildcard), `ORIGIN_CHECK_ENABLED` and `SESSION_COOKIE_SECURE` are true (`origin_guard.security_config_problems`, called from `index.py`).
- **Without `SITE_URL`** (local development, and any deployment that does not set it) the redirect_uri is still derived from the request Host, with a one-time warning logged for non-local hosts. **Not changed because refusing would break existing logins; production therefore stays exposed until `SITE_URL` and `TRUSTED_HOSTS` are set there.** Google accepts only redirect URIs registered in its console, which limits but does not remove the exposure for other `url_for` uses.

## F2. No CSRF defence on state-changing routes
- **Severity:** Medium before this work; Low now. **Exploitability:** the session cookie is `SameSite=Lax`, so cross-site POSTs do not carry it in modern browsers; the residual risk was older browsers, same-site sibling origins and login CSRF.
- **Evidence:** 12 state-changing routes (`/search`, `/chats/new`, `/chat/{id}/cancel|live-links|delete|message-feedback`, `/signup/code|verify`, `/signup`, `/login`, `/api/billing/checkout`, `/webhooks/lemonsqueezy`). Only two checked `Origin` themselves.
- **Fix (IMPLEMENTED, LOCALLY TESTED, ON by default):** `origin_guard.OriginGuard` covers every POST/PUT/PATCH/DELETE by method (including future routes). Browsers send `Sec-Fetch-Site` and scripts cannot forge it: `same-origin`/`none` pass, `cross-site`/`same-site` are blocked (unless the Origin host is in `TRUSTED_HOSTS`); without it, `Origin` must match `Host`; no Origin and no `Sec-Fetch-Site` (curl, the signed payment webhook) passes. Blocked requests get 403 `{"error":"forbidden"}`, which the pricing page already shows as "This request was blocked. Reload the page and try again."
- **Decision: no per-form CSRF tokens.** Fetch Metadata gives the same protection for browsers that send it without touching ~1 MB of inline templates and every `fetch`. **Residual:** a browser that sends neither `Sec-Fetch-Site` nor `Origin` on a cross-site POST is not blocked (all current mainstream browsers send one of them); the guard is only as strong as that. Not per-request token CSRF.
- **Logout (IMPLEMENTED, LOCALLY TESTED):** `POST /logout` added. `GET /logout` is kept so existing links work, but a request the browser marks `cross-site` or `same-site` is redirected home without clearing the session, so another site cannot log a user out with a link or `<img>`. A client that sends no `Sec-Fetch-Site` can still log out via GET (Low: nuisance only).

## F3. No login rate limiting
- **Severity:** Medium (password guessing; bcrypt cost slows it but nothing bounded it). **Exploitability:** unauthenticated, remote.
- **Fix (IMPLEMENTED, LOCALLY TESTED, ON by default):** `login_throttle.py`, wired into `POST /login`: 8 failures per (client address + account) and 40 per client address per 15 minutes, 429 with `Retry-After`. Counters live in the existing `flintel_email_codes` TTL collection (no new infrastructure); keys are hashes (no e-mail or IP stored); storage errors fail open; a success clears the pair counter. Per-pair keys mean a third party cannot lock a victim out.
- **Store failure (IMPLEMENTED, LOCALLY TESTED):** if the shared store errors, a per-process counter takes over, so brute force stays bounded per worker (effective ceiling up to `workers x limit` while the store is down) instead of becoming unlimited; a warning is logged.
- **Proxy handling (IMPLEMENTED, LOCALLY TESTED):** `TRUSTED_PROXY_HOPS` (default 1, the previous behaviour) selects the Nth `X-Forwarded-For` entry from the right; `0` ignores the header; a header shorter than the hop count falls back to the socket peer. **A wrong value is a real exposure:** too low behind several proxies shares one address across users; too high lets a client choose its own address. This repository cannot verify your proxy chain. Google sign-in, signup codes (already limited per address) and password reset (not present) are separate.

## F4. Starlette 0.38.6 advisories (the pinned version is `>=0.38.0,<0.39.0`)
`pip-audit` on the pinned set found only Starlette (7 distinct advisories). Whether each applies, from the code:

| Advisory | Fixed in | Applies here? | Severity for this app |
|---|---|---|---|
| PYSEC-2026-1943 / CVE-2024-47874 (multipart field without filename buffered without limit) | 0.40.0 | **Yes**: the app parses `Form(...)` bodies on unauthenticated routes (`/login`, `/signup`, `/signup/code`, `/signup/verify`). Denial of service by memory pressure. | Medium (availability) |
| PYSEC-2026-1941 / CVE-2025-54121 (large uploaded file blocks the event loop on rollover) | 0.47.2 | No file upload endpoint (`UploadFile`/`File(` not used). | None found |
| PYSEC-2026-249 / CVE-2026-54283 (urlencoded form ignores `max_fields`/`max_part_size`) | 1.3.1 | **Yes**, same routes. Denial of service. | Medium (availability) |
| PYSEC-2026-161 / CVE-2026-48710 (`request.url` rebuilt from unvalidated Host) | 1.0.1 | **Yes** where `request.url`/`url_for` are used (OAuth redirect, two same-origin checks, SEO base URL). Mitigated by F1 when `SITE_URL`/`TRUSTED_HOSTS` are set. | Medium |
| PYSEC-2026-248 / CVE-2026-54282 (request path not validated when rebuilding `request.url`) | 1.3.0 | Same code paths as above; an ASGI server normally supplies a path beginning with `/`, which limits it. | Low to Medium |
| PYSEC-2026-2280 / CVE-2026-48817 (`HTTPEndpoint` handler lookup) | 1.1.0 | No `HTTPEndpoint` in the code. | None found |
| PYSEC-2026-2281 / CVE-2026-48818 (Windows `StaticFiles` UNC path) | 1.1.0 | No `StaticFiles`; the deployment is Linux. | None found |

- **Upgrade candidate prepared, not adopted:** `requirements-upgrade-candidate.txt` (FastAPI 0.143.3, Starlette 1.6.0, Python 3.13) closes all seven by version (all fixed by 1.3.1) and the suite shows the same 38 pre-existing failures as the pinned set; it is **not** used by any deployment. Not verified: OAuth round trip, real cookies behind a proxy, streaming under uvicorn, the webhook, real bcrypt (the suite stubs passlib), Python 3.12 with that set, and any advisory published after 1.3.1.
- **Upgrade decision: NOT performed.** The two form-parsing DoS advisories and the Host advisories are only fully closed by Starlette >= 1.3.1, which requires a FastAPI major jump beyond the pinned `<0.116`. All 16 `TemplateResponse` call sites already use the new `(request, name, context)` signature, and the full suite shows the same failures on Starlette 0.38.6 / Python 3.12 and Starlette 1.6.0 / FastAPI 0.143.3 / Python 3.13 (see `FLINTEL_TEST_REPORT.md`), but that is a unit-test result: OAuth round trips, real session cookies behind a proxy, streaming responses and the payment webhook have not been exercised on the new version. A staged upgrade in staging is the recommended remediation; until then cap request size at the reverse proxy (not verifiable from this repository).

## F5. Session cookie
- `SessionMiddleware` with `SameSite=Lax`; `Secure` only when `SESSION_COOKIE_SECURE=true` (OFF by default; required in staging). `HttpOnly` is Starlette's default for the session cookie. **Severity:** Low where HTTPS is terminated upstream and the flag is set; Medium for an HTTPS deployment that never sets it.

## F6. OAuth redirect validation
- Google's `state` and PKCE are handled by Authlib (not changed). The post-login redirect target is a server-computed path (`_post_auth_target`), not a user-supplied URL. No open redirect found by reading.

## F7. Secrets, `.env` and environment isolation
- `load_dotenv()` loads the first `.env` found upward from the working directory for unset variables; the staging preflight fails when one exists in the search path, checks secret entropy, forbidden production-host and fingerprint matches, and never prints values. No secret was found in the repository by the checks run in earlier passes; this pass added no secrets. Residual: a developer machine can still pick up an unintended `.env`.

## F8. Not covered
Dependency, container, network, cloud-IAM, WAF, TLS and reverse-proxy configuration; the payment webhook's signature scheme beyond what earlier audits reviewed; any penetration test.
