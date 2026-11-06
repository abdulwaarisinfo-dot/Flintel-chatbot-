"""
origin_guard.py - same-origin enforcement for state-changing requests (a small, dependency-free ASGI middleware).

WHY: the application's cookies are SameSite=Lax, which stops browsers sending them on cross-site POSTs, but only two endpoints (billing checkout and
live-links) also check the Origin header themselves. This applies the same rule to EVERY unsafe method (POST/PUT/PATCH/DELETE) as defence in depth
(older browsers, same-site siblings). ON by default (ORIGIN_CHECK_ENABLED=false disables it); see request_allowed() for the exact rule.

RULE: when the request carries an `Origin` header, its host[:port] must equal the request's Host header, or its hostname must be in `trusted_hosts`.
A request with NO Origin header is allowed (server-to-server callers such as payment webhooks, curl, health checks; browsers always send Origin on
cross-origin POSTs). `Origin: null` is treated as a mismatch. Safe methods (GET/HEAD/OPTIONS) are never blocked.
"""
from urllib.parse import urlsplit

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_BODY = b'{"error":"forbidden","detail":"cross-origin request blocked"}'   # "forbidden" is the key the pricing page already maps to a message


def origin_allowed(origin, host, trusted_hosts=()):
    if origin is None:
        return True
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return False
    if not parts.scheme or not parts.netloc:
        return False                                              # "null", garbage, or a non-URL
    netloc = parts.netloc.lower()
    if host and netloc == host.strip().lower():
        return True
    return (parts.hostname or "").lower() in {h.strip().lower() for h in trusted_hosts if h}


def request_allowed(headers, trusted_hosts=()):
    """Decision for one unsafe request. `headers` is a dict of lower-case header names.
    1) Fetch Metadata: browsers send `Sec-Fetch-Site` on every request and scripts cannot forge it. `same-origin` and `none` (user-initiated) pass;
       `cross-site` and `same-site` are blocked unless the Origin hostname is in `trusted_hosts`. This works behind a proxy that rewrites Host.
    2) No Sec-Fetch-Site (old browsers, curl, server-to-server): fall back to the Origin-vs-Host rule above (no Origin = allowed)."""
    sfs = (headers.get("sec-fetch-site") or "").strip().lower()
    if sfs:
        if sfs in ("same-origin", "none"):
            return True
        origin = headers.get("origin")
        try:
            h = (urlsplit((origin or "").strip()).hostname or "").lower()
        except ValueError:
            h = ""
        return bool(h) and h in {t.strip().lower() for t in trusted_hosts if t}
    return origin_allowed(headers.get("origin"), headers.get("host"), trusted_hosts)


class OriginGuard:
    def __init__(self, app, trusted_hosts=()):
        self.app = app
        self.trusted_hosts = tuple(trusted_hosts or ())

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if not request_allowed(headers, self.trusted_hosts):
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(_BODY)).encode())]})
                await send({"type": "http.response.body", "body": _BODY})
                return
        await self.app(scope, receive, send)


def security_config_problems(env, trusted_hosts, origin_check, cookie_secure, site_url):
    """Problems that must stop a STAGING process from starting (empty list = fine). Names only; never prints values that could be secret.
    Non-staging environments are not judged here (local development and the existing production deployment are unchanged)."""
    if str(env or "").strip().lower() != "staging":
        return []
    problems = []
    hosts = [h.strip() for h in str(trusted_hosts or "").split(",") if h.strip()]
    if not hosts:
        problems.append("TRUSTED_HOSTS is empty (Host-header hardening is required in staging)")
    if "*" in hosts:
        problems.append("TRUSTED_HOSTS contains a wildcard")
    if not origin_check:
        problems.append("ORIGIN_CHECK_ENABLED is not true (same-origin check required in staging)")
    if not cookie_secure:
        problems.append("SESSION_COOKIE_SECURE is not true")
    site = str(site_url or "").strip()
    if not site.lower().startswith("https://"):
        problems.append("SITE_URL must be set to the https:// staging origin (it fixes the OAuth redirect_uri)")
    else:
        from urllib.parse import urlsplit
        host = (urlsplit(site).hostname or "").lower()
        if hosts and host and host not in {h.lower() for h in hosts}:
            problems.append("SITE_URL host is not listed in TRUSTED_HOSTS")
    return problems
