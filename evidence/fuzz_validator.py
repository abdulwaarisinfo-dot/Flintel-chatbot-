"""Review probe (NOT part of the patch): fuzz valid_reddit_url / parse_response / present / render_links."""
import random, re, sys, json, unicodedata
from datetime import datetime, timezone
sys.path.insert(0, "/tmp/pchk2")
import reddit_live as rl

STRICT = re.compile(r"^https://(reddit\.com|www\.reddit\.com|old\.reddit\.com)/r/[A-Za-z0-9_]{2,21}/comments/[a-z0-9]{4,10}(/[A-Za-z0-9_-]{1,300}){0,2}/?$")
HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com"}

def browser_view(s):
    """Independent, WHATWG-style reading of an https URL as a browser would navigate it: strip tab/newline, treat '\\' as '/',
    host = authority after the last '@', before ':', percent-decoded, IDNA-lowercased."""
    from urllib.parse import unquote
    s = re.sub(r"[\t\n\r]", "", s.strip("\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f "))
    m = re.match(r"^(https?):[/\\]*([^/\\?#]*)", s, re.I)
    if not m: return None
    auth = m.group(2)
    host = auth.rsplit("@", 1)[-1]
    host = re.sub(r":\d*$", "", host)
    host = unquote(host)
    try:
        host = host.encode("idna").decode("ascii").lower()
    except Exception:
        host = unicodedata.normalize("NFKC", host).lower()
    return m.group(1).lower(), host

SEEDS = ["https://www.reddit.com/r/SaaS/comments/abc123/some_post/", "https://old.reddit.com/r/startups/comments/1abcd2/",
         "https://reddit.com/r/Entrepreneur/comments/zz99aa/title_here/def456/", "https://www.reddit.com/r/SaaS/comments/abc123"]
CHARS = ["\\", "%", "%2f", "%2F", "%40", "%00", "@", "#", "?", ":", "/", "//", ".", "..", " ", "\t", "\n", "\r", "\x00", "\x7f",
         " ", "ı", "е", "。", "．", "／", "＼", "∕", "‮", "​", "﻿", "а",
         "evil.com", "@evil.com", "\\evil.com", "x", "-", "_", "[", "]", "'", "\"", "<", ">", ";", "&", "=", "+", "~", "!", "*", "(", ")", ",", "ß", "İ", "K"]
random.seed(20261101)
accepted = rejected = 0
bad = []
def check(u):
    global accepted, rejected
    out = rl.valid_reddit_url(u)
    if out is None:
        rejected += 1; return
    accepted += 1
    if not STRICT.match(out): bad.append(("canonical-not-strict", u, out)); return
    bv = browser_view(u)
    if bv is None or bv[0] != "https" or bv[1] not in HOSTS: bad.append(("browser-view-mismatch", u, bv, out))

for u in SEEDS: check(u)
N = 300000
for _ in range(N):
    u = random.choice(SEEDS)
    for _ in range(random.randint(1, 3)):
        pos = random.randint(0, len(u)); op = random.random()
        c = random.choice(CHARS)
        if op < 0.5: u = u[:pos] + c + u[pos:]
        elif op < 0.8 and u: u = u[:pos] + c + u[pos+1:]
        elif u: u = u[:pos] + u[pos+1:]
    check(u)
# targeted hostile families around the host
for pre in ["https://", "https:\\\\", "https:/\\", "https:///", "HTTPS://", " https://", "https:\t//", "https://​"]:
    for host in ["www.reddit.com", "evil.com\\.reddit.com", "evil.com\\@www.reddit.com", "www.reddit.com\\@evil.com", "www.reddit.com%2f@evil.com",
                 "www.reddit.com%5c@evil.com", "www%2ereddit.com", "www.reddit.com:443", "www.reddit.com.", "www.reddit.com..", "WWW.REDDIT.COM",
                 "www.reddit.com", "ｗｗｗ.reddit.com", "www.reddit.com．evil.com", "www.reddit.com@", "@www.reddit.com",
                 "evil.com#@www.reddit.com", "evil.com?@www.reddit.com", "www.reddit.com∕evil.com", "www.reddit.com%E2%88%95"]:
        for path in ["/r/SaaS/comments/abc123/x/", "\\r\\SaaS\\comments\\abc123\\x\\", "/r/SaaS/comments/abc123/x/../../../evil", "/r/SaaS/comments/abc123/%2e%2e/x"]:
            check(pre + host + path)
print(f"validator fuzz: {accepted} accepted, {rejected} rejected, violations={len(bad)}")
for b in bad[:10]: print("  VIOLATION", b)

# tampered persisted records / junk responses through parse_response, present, render_links
def junk(depth=0):
    r = random.random()
    if depth > 3 or r < 0.3:
        return random.choice([None, True, 0, -1, 3.5, "", "x", "javascript:alert(1)", "https://evil.com\\.reddit.com/r/SaaS/comments/abc123/x/",
                              "https://www.reddit.com/r/SaaS/comments/abc123/x/", "https://www.reddit.com/user/x", "\x00", "<script>", [], {}])
    if r < 0.65: return [junk(depth+1) for _ in range(random.randint(0, 3))]
    return {random.choice(["url", "title", "state", "links", "type", "sources", "action", "output", "content", "annotations", "text"]): junk(depth+1) for _ in range(random.randint(0, 4))}
viol = 0; exc = 0
for _ in range(60000):
    live = {"state": random.choice(["ok", "ok", "ok", "error", None, 5]), "links": junk(), "claimed_at": junk()}
    resp = {"output": junk()} if random.random() < .5 else junk()
    try:
        outs = []
        outs += rl.present(live, datetime.now(timezone.utc)).get("links", [])
        outs += rl.render_links(live) if rl.is_active() else rl._safe_links(live.get("links"))
        outs += rl.parse_response(resp, 8)[0]
        for l in outs:
            if set(l) != {"url", "title"} or not STRICT.match(l["url"]) or not (l["title"] is None or (isinstance(l["title"], str) and len(l["title"]) <= 200 and not re.search(r"[\x00-\x1f\x7f]", l["title"]))):
                viol += 1
    except Exception as e:
        exc += 1; print("EXC", type(e).__name__, e); break
print(f"record/response fuzz: violations={viol} exceptions={exc}")
