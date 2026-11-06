#!/usr/bin/env python3
"""Staging preflight. READ-ONLY and offline: it inspects environment variables only. It opens no connection, writes nothing, and never
prints a secret, password, token or full connection string (hosts are masked; secrets are compared only by a 12-hex SHA-256 fingerprint).

Every check ends PASS, FAIL or SKIPPED. A check whose answer cannot be established is SKIPPED, never PASS. The overall verdict is
    PASSED      every check required in this phase passed
    FAILED      at least one check failed
    INCOMPLETE  nothing failed, but a required check was skipped / could not be established (isolation is NOT proven)

Phases (so isolation can be proven BEFORE any paid credential exists):
    --phase isolation   (default) proves the target is isolated and safe to START; paid credentials are reported SKIPPED ("not checked in
                        this phase") and do not count against this phase, but the verdict says so explicitly.
    --phase paid        everything above plus the paid-integration prerequisites (OPENAI_API_KEY, its spend-limit attestation).
Exit codes: 0 PASSED, 1 FAILED, 4 INCOMPLETE.

The operator must say what production looks like (STAGING_FORBIDDEN_HOSTS, STAGING_FORBIDDEN_KEY_SHA256_PREFIXES): isolation cannot be proven
from a database called "staging".
"""
import argparse
import hashlib
import os
import sys
from urllib.parse import urlsplit

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"
TRUE = ("1", "true", "yes", "on")
FALSE = ("0", "false", "no", "off")
INSECURE_SESSION = {"", "dev-only-change-me"}                       # the defaults the application itself refuses
# Predictable fragments: a secret containing any of them is rejected whatever else it contains (matched case-insensitively).
WEAK_FRAGMENTS = ("password", "passwd", "secret", "changeme", "change-me", "dev-only", "example", "sample", "default", "test", "staging", "flintel",
                  "admin", "qwerty", "letmein", "welcome", "token", "generate", "placeholder", "123456", "654321", "000000", "111111", "abcdef", "012345")
MIN_SECRET_LENGTH, MIN_DISTINCT_CHARS, MIN_ENTROPY_BITS, MAX_SEQUENCE_RUN = 32, 10, 3.0, 6
# Every variable whose value is a secret (compared by fingerprint, never printed).
SECRETS = ["SESSION_SECRET_KEY", "MONGODB_URI", "MONGODB2", "MONGODB3", "MONGODB4", "GOOGLE_CLIENT_SECRET", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
           "LEMONSQUEEZY_API_KEY", "LEMONSQUEEZY_WEBHOOK_SECRET", "MYSQL_PASSWORD", "RAPIDAPI_KEY", "SMTP_PASSWORD"]
# Feature flags that start paid work or other unreviewed behaviour: they must not be ON for the first staging round.
RISKY_FLAGS = ["INTENT_BRIDGE_ENABLED", "ACTIVE_INTENT_ENABLED", "REDDIT_LIVE_LINKS_ENABLED", "STRICT_INTENT_MODE", "TURN_FINISH_ENABLED"]
# Integrations that must be absent: paid discovery, outbound email, read-only database mirrors.
FORBIDDEN_PRESENT = [("RAPIDAPI_KEY", "paid Google-discovery service"), ("RAPIDAPI_GOOGLE_HOST", "paid Google-discovery service"),
                     ("SMTP_HOST", "outbound email"), ("EMAIL_BACKEND", "outbound email"),
                     ("MONGODB2", "read-only signals mirror (would add another database as a source)"),
                     ("MONGODB4", "read-only signals mirror (would add another database as a source)")]


# python-dotenv's load_dotenv() (called by config.py and database.py with no path) loads the FIRST `.env` found walking up from the directory of the
# calling module, and uses it to fill every variable that is NOT already in the real environment. This check reads only the real environment, so a
# leftover `.env` (for example one copied from production) could silently switch on MONGODB2/MONGODB4 mirrors, RapidAPI, SMTP, the scheduler or the
# credit kill switch without this tool seeing it. Staging configuration must therefore come ONLY from the host's environment: any `.env` the
# application could load is a FAIL. Only existence is checked; the file is never opened or read.
DOTENV_START = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # the repository root: where config.py and database.py live
DOTENV_STOP = None                                                              # None = walk to the filesystem root, as python-dotenv does


def dotenv_files(start=None, stop=None):
    """Every `.env` file from `start` upward (to `stop` inclusive, or the filesystem root). Existence only; never opened."""
    d = os.path.abspath(start or DOTENV_START)
    stop = os.path.abspath(stop or DOTENV_STOP) if (stop or DOTENV_STOP) else None
    found = []
    while True:
        p = os.path.join(d, ".env")
        if os.path.isfile(p):
            found.append(p)
        parent = os.path.dirname(d)
        if parent == d or d == stop:
            break
        d = parent
    return found


def _csv(v):
    return [x.strip().lower() for x in (v or "").split(",") if x.strip()]


def host_of(uri):
    try:
        return (urlsplit(uri).hostname or "").lower()
    except ValueError:
        return ""


def endpoint_text(value):
    """Everything in a connection string that can name a host, lower-cased, WITHOUT the credentials: all hosts of a multi-host (replica set)
    list and any query parameter, not just the first host (urlsplit().hostname sees only the first one, which let a production host listed
    second pass)."""
    v = (value or "").strip().lower()
    if "://" in v:
        v = v.split("://", 1)[1]
    authority, sep, rest = v.partition("/")
    if "@" in authority:                       # drop user:password (it may legitimately contain anything)
        authority = authority.rsplit("@", 1)[1]
    return authority + sep + rest


def matched_production(value, forb):
    t = endpoint_text(value)
    return [f for f in forb if f and f in t]


def secret_weaknesses(value):
    """Generic reasons (never the value, a fragment of it, or its fingerprint) why `value` cannot be a strong session secret. [] means no weakness found.
    Rejects: too short, a known default, a predictable fragment, too few distinct characters, low Shannon entropy per character, a short pattern
    repeated through the whole value (e.g. 'abcabc...' or 'aaaa...'), and long sequential runs ('...56789...', 'zyxwv...'). A value made by
    secrets.token_hex(32) or token_urlsafe(48) passes (property-tested)."""
    import math
    v = value or ""
    why = []
    if len(v) < MIN_SECRET_LENGTH:
        why.append(f"shorter than {MIN_SECRET_LENGTH} characters")
    if v.strip().lower() in INSECURE_SESSION:
        why.append("a known default")
    low = v.lower()
    if any(f in low for f in WEAK_FRAGMENTS):
        why.append("contains a predictable word or sequence")
    if v and len(set(v)) < MIN_DISTINCT_CHARS:
        why.append(f"fewer than {MIN_DISTINCT_CHARS} distinct characters")
    if v:
        counts = {}
        for ch in v:
            counts[ch] = counts.get(ch, 0) + 1
        h = -sum((c / len(v)) * math.log2(c / len(v)) for c in counts.values())
        if h < MIN_ENTROPY_BITS:
            why.append("low entropy (characters are too predictable)")
    n = len(v)
    if n >= 8 and any(all(v[i] == v[i + p] for i in range(n - p)) for p in range(1, n // 2 + 1)):
        why.append("repeats a short pattern")
    run_up = run_dn = best = 1
    for a, b in zip(v, v[1:]):
        run_up = run_up + 1 if ord(b) - ord(a) == 1 else 1
        run_dn = run_dn + 1 if ord(a) - ord(b) == 1 else 1
        best = max(best, run_up, run_dn)
    if best > MAX_SEQUENCE_RUN:
        why.append("contains a long sequential run")
    return why


def mask(h):
    return (h[:3] + "***" + h[-4:]) if len(h) > 8 else "***"


def fp(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _flag(env, name):
    """True / False / None (unset or unrecognised)."""
    v = (env.get(name) or "").strip().lower()
    return True if v in TRUE else False if v in FALSE else None


class Results:
    def __init__(self):
        self.rows = []          # dicts: name, status, msg, phase ("isolation" | "paid")

    def add(self, name, status, msg, phase="isolation"):
        self.rows.append({"name": name, "status": status, "msg": msg, "phase": phase})


def run(env, phase="isolation"):
    """All checks for `phase`. Returns [row dicts]. Messages never contain a value."""
    r = Results()
    forb = _csv(env.get("STAGING_FORBIDDEN_HOSTS"))
    fps = set(_csv(env.get("STAGING_FORBIDDEN_KEY_SHA256_PREFIXES")))

    # 0. configuration must come from the real environment only
    envfiles = dotenv_files()
    r.add("no .env file can be loaded by the application", FAIL if envfiles else PASS,
          (f"{len(envfiles)} .env file(s) found (e.g. {envfiles[0]}): the application would load it and fill unset variables from it, invisibly to this check. "
           "Remove it; staging must be configured only through the host's environment") if envfiles else "none found from the repository root upward")
    # 1. identity and the operator's description of production
    ident = (env.get("FLINTEL_ENV") or "").strip().lower()
    r.add("explicit staging identity (FLINTEL_ENV=staging)", PASS if ident == "staging" else FAIL, "ok" if ident == "staging" else "FLINTEL_ENV must be exactly 'staging'")
    r.add("production hosts described (STAGING_FORBIDDEN_HOSTS)", PASS if forb else FAIL,
          "supplied" if forb else "missing: isolation cannot be proven by names alone")
    r.add("production secret fingerprints supplied", PASS if fps else SKIPPED,
          "supplied" if fps else "not supplied: reuse of a production secret cannot be detected, so isolation is incomplete")

    # 2. databases: required ones exist, forbidden mirrors do not, and no URL-bearing variable points at a listed production host
    for n in ("MONGODB_URI", "MONGODB3", "MONGODB_DB"):
        r.add(f"required {n}", PASS if env.get(n) else FAIL, "set" if env.get(n) else "MISSING")
    for n, why in FORBIDDEN_PRESENT:
        if env.get(n):
            r.add(f"{n} is unset", FAIL, f"must be unset in staging: {why}")
    if forb:
        for n, v in sorted(env.items()):
            if v and "://" in v and not n.startswith(("STAGING_", "PATH")):
                h = host_of(v) or endpoint_text(v).split("/", 1)[0].split(",", 1)[0]
                if h:
                    hit = bool(matched_production(v, forb))          # every host in the string, plus query parameters
                    r.add(f"{n} host is not a listed production target", FAIL if hit else PASS, f"{mask(h)} (or another listed host) matches a production entry" if hit else f"{mask(h)} ok")
        for n in ("MYSQL_HOST", "MONGODB_DB", "MYSQL_READ_DATABASES"):
            v = (env.get(n) or "").lower()
            if v:
                hit = any(f in v for f in forb)
                r.add(f"{n} is not a listed production target", FAIL if hit else PASS, f"{mask(v)} matches a production entry" if hit else f"{mask(v)} ok")
    else:
        r.add("database targets checked against production", SKIPPED, "cannot be checked without STAGING_FORBIDDEN_HOSTS")
    mysql_on = _flag(env, "MYSQL_READ_ENABLED")
    if mysql_on:
        r.add("MySQL host is explicit", PASS if env.get("MYSQL_HOST") else FAIL, "set" if env.get("MYSQL_HOST") else "MYSQL_READ_ENABLED is on but MYSQL_HOST is missing")
    elif env.get("MYSQL_HOST") or env.get("MYSQL_USER"):
        r.add("MySQL settings consistent", FAIL, "MySQL variables are set while MYSQL_READ_ENABLED is not true")

    # 3. session secret and the secrets' fingerprints
    weak = secret_weaknesses(env.get("SESSION_SECRET_KEY") or "")
    r.add("SESSION_SECRET_KEY is strong", FAIL if weak else PASS, ("rejected: " + "; ".join(weak)) if weak else "ok")
    if fps:
        for n in SECRETS:
            v = env.get(n)
            if v:
                r.add(f"{n} is not a production secret", FAIL if fp(v) in fps else PASS, "its fingerprint matches a production secret" if fp(v) in fps else "no match")

    # 4. public URL
    su = env.get("SITE_URL") or ""
    ok, msg = check_base_url(su, env)
    r.add("SITE_URL is a staging https URL", PASS if ok else FAIL, msg)

    # 5. billing, flags, integrations, scheduler
    if any(k.startswith("LEMONSQUEEZY_") and v for k, v in env.items() if k != "LEMONSQUEEZY_TEST_MODE"):
        r.add("billing is in test mode", PASS if _flag(env, "LEMONSQUEEZY_TEST_MODE") else FAIL, "ok" if _flag(env, "LEMONSQUEEZY_TEST_MODE") else "LEMONSQUEEZY_TEST_MODE must be true")
    sc = _flag(env, "SESSION_COOKIE_SECURE")
    r.add("session cookie is Secure (SESSION_COOKIE_SECURE=true)", PASS if sc else FAIL,
          "ok" if sc else "set SESSION_COOKIE_SECURE=true: staging is served over HTTPS and the session cookie must not travel over plain HTTP")
    th = [h.strip().lower() for h in (env.get("TRUSTED_HOSTS") or "").split(",") if h.strip()]
    site_host = host_of(env.get("SITE_URL") or "")
    r.add("TRUSTED_HOSTS lists the staging host (Host-header hardening)", PASS if (th and site_host and site_host in th) else FAIL,
          "ok" if (th and site_host and site_host in th) else "set TRUSTED_HOSTS to the staging hostname(s), including the SITE_URL host: otherwise a hostile Host header steers the OAuth redirect and request.url")
    if th and forb:
        bad = [h for h in th if any(f in h for f in forb)]
        r.add("TRUSTED_HOSTS contains no production host", FAIL if bad else PASS, "a listed production host is trusted by the staging app" if bad else "ok")
    oc = _flag(env, "ORIGIN_CHECK_ENABLED")
    r.add("same-origin check on state-changing requests (ORIGIN_CHECK_ENABLED=true)", PASS if oc else FAIL, "ok" if oc else "set ORIGIN_CHECK_ENABLED=true")
    ce = (env.get("CREDIT_ENFORCEMENT_DISABLED") or "").strip().lower()
    r.add("credit enforcement is ON (kill switch absent)", FAIL if ce == "true" else PASS, "CREDIT_ENFORCEMENT_DISABLED=true disables credit limits" if ce == "true" else "ok")
    for n in RISKY_FLAGS:
        r.add(f"{n} is off", FAIL if _flag(env, n) else PASS, "enabled: it starts unreviewed or paid behaviour" if _flag(env, n) else "ok")
    t = _flag(env, "TRENDING_KEYWORDS_ENABLED")
    r.add("trending-keywords scheduler explicitly disabled", PASS if t is False else FAIL,
          "ok" if t is False else "set TRENDING_KEYWORDS_ENABLED=false explicitly (it makes a paid LLM web-search call and a Google call on a fresh database)")
    le = _flag(env, "LAZY_EMBED_ENABLED")
    r.add("lazy query-time embedding explicitly off", PASS if le is False else FAIL,
          "ok" if le is False else "set LAZY_EMBED_ENABLED=0 (default is on: unembedded posts are embedded at query time, paid)")
    pm = _flag(env, "ZERO_RESULT_MYSQL_PROBE_ENABLED")
    if pm:
        verified = _flag(env, "STAGING_MYSQL_PROBE_VERIFIED")
        r.add("MySQL probe only after verification", PASS if verified else FAIL, "ok" if verified else "probe is on but STAGING_MYSQL_PROBE_VERIFIED is not true")

    # 6. paid prerequisites (checked only in the paid phase)
    if phase == "paid":
        key = env.get("OPENAI_API_KEY")
        r.add("OPENAI_API_KEY is configured (the only LLM/embedding key used; ANTHROPIC_API_KEY is deprecated)", PASS if key else FAIL, "set" if key else "MISSING", "paid")
        conf = _flag(env, "STAGING_OPENAI_LIMIT_CONFIRMED")
        r.add("operator confirms a hard OpenAI budget limit", PASS if conf else FAIL, "confirmed" if conf else "set STAGING_OPENAI_LIMIT_CONFIRMED=true only after setting the limit in the OpenAI project", "paid")
    else:
        r.add("paid credentials (OPENAI_API_KEY, spend limit)", SKIPPED, "not checked in the isolation phase; run --phase paid before any paid verification", "paid")
    return r.rows


def verdict(rows, phase="isolation"):
    """PASSED / FAILED / INCOMPLETE. In the isolation phase the paid-credential row is out of scope; every other SKIPPED row makes it INCOMPLETE."""
    if any(x["status"] == FAIL for x in rows):
        return "FAILED"
    scoped = [x for x in rows if not (phase == "isolation" and x["phase"] == "paid")]
    if any(x["status"] == SKIPPED for x in scoped):
        return "INCOMPLETE"
    return "PASSED"


def check_base_url(url, env):
    """(ok, message) for a deployed-app URL: https (http only for localhost) and not a listed production host."""
    try:
        p = urlsplit(url or "")
    except ValueError:
        return False, "unparseable URL"
    h = (p.hostname or "").lower()
    local = h in ("localhost", "127.0.0.1", "::1")
    if not h or (p.scheme != "https" and not (local and p.scheme == "http")):
        return False, "must be https (http only for localhost)"
    forb = _csv(env.get("STAGING_FORBIDDEN_HOSTS"))
    if not forb:
        return False, "STAGING_FORBIDDEN_HOSTS is required"
    if matched_production(url, forb):
        return False, f"{mask(h)} matches a production entry"
    return True, f"{mask(h)} ok"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("isolation", "paid"), default="isolation")
    a = ap.parse_args(argv)
    rows = run(os.environ, a.phase)
    for x in rows:
        print(f"{x['status']:8} {x['name']} - {x['msg']}")
    v = verdict(rows, a.phase)
    c = {s: sum(1 for x in rows if x['status'] == s) for s in (PASS, FAIL, SKIPPED)}
    print(f"\nphase={a.phase}  PASS={c[PASS]} FAIL={c[FAIL]} SKIPPED={c[SKIPPED]}  verdict={v}")
    if v == "PASSED" and a.phase == "isolation":
        print("Isolation proven. Paid credentials were NOT checked; run with --phase paid before any paid verification.")
    return {"PASSED": 0, "FAILED": 1, "INCOMPLETE": 4}[v]


if __name__ == "__main__":
    sys.exit(main())
