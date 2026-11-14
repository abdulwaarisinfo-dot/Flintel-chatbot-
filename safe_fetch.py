"""
safe_fetch.py — fetch a USER-SUPPLIED public web page without letting it reach internal systems (SSRF-safe, bounded).

Why this exists: the older `logics.fetch_website_text` / `_fetch_raw_html` follow redirects to any host and accept any http(s) URL, so a
user-supplied URL can reach localhost, a private network or a cloud metadata endpoint. New code that fetches a user-supplied URL uses THIS
module instead.

Rules (all enforced here, none by caller convention):
  - only http / https, no credentials in the URL, only ports 80 / 443;
  - the host is resolved and EVERY resolved address must be globally routable (no loopback, private, link-local, multicast, reserved,
    unspecified, or the cloud-metadata ranges); a hostname that resolves to any non-public address is refused;
  - redirects are followed MANUALLY (at most MAX_REDIRECTS) and each hop is validated again;
  - the response body is read in chunks and stops at MAX_BYTES; the total time is bounded by TIMEOUT_SECONDS;
  - only text-like content types are accepted.
The connection goes to the validated address, not to a second DNS lookup, to close the DNS-rebinding window (the Host header and the TLS
server name stay the original hostname, and the certificate is verified). Pure stdlib.

`resolve` and `transport` are injectable so tests never touch the network.
"""
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

MAX_REDIRECTS = 3
MAX_BYTES = 1_000_000
TIMEOUT_SECONDS = 8.0
ALLOWED_PORTS = (80, 443)
_TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_METADATA_NETS = tuple(ipaddress.ip_network(n) for n in ("169.254.0.0/16", "100.64.0.0/10", "fd00:ec2::/32"))


class UnsafeUrl(ValueError):
    """The URL is refused before or during a fetch (the message is safe to show to the user)."""


class FetchFailed(RuntimeError):
    """The page could not be retrieved (network error, status, size, type). Safe to show to the user."""


def _is_public(ip: "ipaddress._BaseAddress") -> bool:
    if ip.version == 6 and getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if any(ip in net for net in _METADATA_NETS):
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_unspecified)


DNS_TIMEOUT_SECONDS = 3.0
_DNS_POOL = None


def default_resolve(host: str, port: int, timeout: float = None):
    """getaddrinfo with a hard deadline (the system call itself cannot be interrupted, so it runs in a small shared pool and the CALLER stops
    waiting after `timeout` seconds; a stuck lookup can occupy at most the pool's few threads)."""
    global _DNS_POOL
    import concurrent.futures
    if _DNS_POOL is None:
        _DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="safe-fetch-dns")
    fut = _DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    try:
        infos = fut.result(timeout=DNS_TIMEOUT_SECONDS if timeout is None else timeout)
    except concurrent.futures.TimeoutError:
        raise OSError("name lookup timed out")
    return sorted({i[4][0] for i in infos})


def check_url(url: str, resolve=default_resolve):
    """Validates `url` and returns (scheme, host, port, path_with_query, [addresses]). Raises UnsafeUrl."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise UnsafeUrl("Enter a valid website address.")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise UnsafeUrl("Enter a valid website address.")
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl("Only http and https website addresses are supported.")
    if parts.username or parts.password:
        raise UnsafeUrl("Website addresses with a user name or password are not supported.")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host or " " in host:
        raise UnsafeUrl("Enter a valid website address.")
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UnsafeUrl("Only the standard web ports (80 and 443) are supported.")
    try:                                                        # a literal IP is checked directly, no lookup
        literal = ipaddress.ip_address(host)
        addrs = [str(literal)]
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
            raise UnsafeUrl("That address is not a public website.")
        try:
            addrs = list(resolve(host, port))
        except OSError:
            raise UnsafeUrl("That website address could not be found.")
    if not addrs:
        raise UnsafeUrl("That website address could not be found.")
    for a in addrs:
        try:
            ok = _is_public(ipaddress.ip_address(a))
        except ValueError:
            ok = False
        if not ok:
            raise UnsafeUrl("That address is not a public website.")
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    return parts.scheme, host, port, path, addrs


def _default_transport(scheme, host, port, path, addr, timeout, max_bytes):
    """One GET straight to the VALIDATED address (no second DNS lookup, so a hostname cannot be re-pointed after the check). HTTPS verifies
    the certificate against the original hostname. HTTP/1.0 with `Connection: close` keeps the body un-chunked. The whole exchange is bounded
    by `timeout` seconds and `max_bytes`. Returns (status, lowercased headers, body bytes, truncated)."""
    import ssl
    import time
    deadline = time.monotonic() + timeout
    raw = socket.create_connection((addr, port), timeout=timeout)
    try:
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if scheme == "https" else raw
        request = (f"GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: Mozilla/5.0 (compatible; FlintelBot/1.0)\r\n"
                   "Accept: text/html,text/plain\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n")
        sock.sendall(request.encode("ascii", errors="ignore"))
        buf, truncated = b"", False
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("deadline")
            sock.settimeout(left)
            chunk = sock.recv(16384)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_bytes + 16384:
                truncated = True
                break
    finally:
        try:
            raw.close()
        except OSError:
            pass
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise FetchFailed("The website sent an unreadable answer.")
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    if len(body) > max_bytes:
        body, truncated = body[:max_bytes], True
    return status, headers, body, truncated


TOTAL_DEADLINE_SECONDS = 12.0


def _dechunk(body: bytes, limit: int) -> bytes:
    """Decodes a chunked body (some servers answer an HTTP/1.0 request that way). Stops at `limit` bytes; a malformed chunk ends the body."""
    out, pos = b"", 0
    while pos < len(body) and len(out) < limit:
        eol = body.find(b"\r\n", pos)
        if eol < 0:
            break
        try:
            size = int(body[pos:eol].split(b";")[0].strip() or b"0", 16)
        except ValueError:
            break
        if size == 0:
            break
        out += body[eol + 2: eol + 2 + size]
        pos = eol + 2 + size + 2
    return out[:limit]


def _decompress(body: bytes, encoding: str, limit: int):
    """(bytes, truncated). gzip / deflate only, with the OUTPUT capped at `limit` (a compressed bomb cannot exceed the size cap)."""
    import zlib
    enc = (encoding or "identity").strip().lower()
    if enc in ("", "identity"):
        return body, False
    if enc in ("gzip", "x-gzip"):
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif enc == "deflate":
        d = zlib.decompressobj(zlib.MAX_WBITS)
        try:
            out = d.decompress(body, limit + 1)
            return out[:limit], len(out) > limit
        except zlib.error:
            d = zlib.decompressobj(-zlib.MAX_WBITS)                 # some servers send raw deflate
    else:
        raise FetchFailed("The website sent a format we cannot read.")
    try:
        out = d.decompress(body, limit + 1)
    except zlib.error:
        raise FetchFailed("The website sent a damaged answer.")
    return out[:limit], len(out) > limit


def fetch_public_page(url: str, resolve=default_resolve, transport=None, max_bytes: int = MAX_BYTES, timeout: float = TIMEOUT_SECONDS,
                      total_deadline: float = TOTAL_DEADLINE_SECONDS) -> dict:
    """Fetches a public page. Returns {"final_url", "status", "text", "truncated", "redirects"}; raises UnsafeUrl or FetchFailed.
    Every hop (the first URL and each redirect target) passes `check_url`. The WHOLE call (lookups, connections, reads, every redirect) is bounded
    by `total_deadline` seconds; each hop gets at most the time that is left."""
    import time
    transport = transport or _default_transport
    current, hops = url, 0
    deadline = time.monotonic() + total_deadline
    while True:
        left = deadline - time.monotonic()
        if left <= 0.2:
            raise FetchFailed("The website took too long to answer.")
        scheme, host, port, path, addrs = check_url(current, resolve)
        left = deadline - time.monotonic()
        if left <= 0.2:
            raise FetchFailed("The website took too long to answer.")
        try:
            status, headers, body, truncated = transport(scheme, host, port, path, addrs[0], min(timeout, left), max_bytes)
        except UnsafeUrl:
            raise
        except Exception as exc:
            raise FetchFailed("The website could not be reached.") from exc
        if status in (301, 302, 303, 307, 308):
            loc = headers.get("location")
            hops += 1
            if not loc or hops > MAX_REDIRECTS:
                raise FetchFailed("The website redirected too many times.")
            current = urljoin(current, loc)
            continue
        if not (200 <= status < 300):
            raise FetchFailed(f"The website answered with status {status}.")
        ctype = (headers.get("content-type") or "text/html").split(";")[0].strip().lower()
        if ctype not in _TEXT_TYPES:
            raise FetchFailed("The address did not return a web page.")
        if "chunked" in (headers.get("transfer-encoding") or "").lower():
            body = _dechunk(body, max_bytes)
        body, more = _decompress(body, headers.get("content-encoding"), max_bytes)
        truncated = bool(truncated or more)
        charset = "utf-8"
        m = re.search(r"charset\s*=\s*([A-Za-z0-9_.:-]+)", headers.get("content-type") or "", re.I)
        if m:
            try:
                import codecs
                codecs.lookup(m.group(1))
                charset = m.group(1)
            except LookupError:
                pass
        text = body.decode(charset, errors="replace")
        return {"final_url": current, "status": status, "text": text, "truncated": bool(truncated), "redirects": hops}
