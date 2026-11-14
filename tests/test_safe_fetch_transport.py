"""safe_fetch's REAL socket transport against real local servers (loopback, ephemeral ports; no external network). The loopback address is
normally refused by check_url, so tests that need to reach a local server patch `_is_public` to allow exactly 127.0.0.1; everything else (the
HTTP/1.0 request, parsing, the size cap, timeouts, TLS verification, redirect re-validation, DNS pinning) is the production code."""
import http.server
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest

import safe_fetch as sf


class _Handler(http.server.BaseHTTPRequestHandler):
    seen = []
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).seen.append({"path": self.path, "host": self.headers.get("Host"), "ua": self.headers.get("User-Agent")})
        if self.path == "/ok":
            body = b"<html><title>Local</title><h1>Hello</h1></html>"
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        elif self.path == "/big":
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"x" * 10000)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/redirect-internal":
            self.send_response(302); self.send_header("Location", "http://169.254.169.254/latest/meta-data/"); self.end_headers()
        elif self.path == "/redirect-ok":
            self.send_response(302); self.send_header("Location", "/ok"); self.end_headers()
        elif self.path == "/stall":
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.flush(); time.sleep(3)
        elif self.path == "/pdf":
            self.send_response(200); self.send_header("Content-Type", "application/pdf"); self.end_headers(); self.wfile.write(b"%PDF")
        else:
            self.send_response(404); self.end_headers()


@pytest.fixture
def server():
    _Handler.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def loopback_allowed(monkeypatch):
    monkeypatch.setattr(sf, "_is_public", lambda ip: str(ip) == "127.0.0.1")


def test_the_real_transport_reads_status_headers_and_body(server):
    status, headers, body, truncated = sf._default_transport("http", "localhost", server, "/ok", "127.0.0.1", 3.0, 100000)
    assert status == 200 and headers["content-type"].startswith("text/html") and b"<h1>Hello</h1>" in body and truncated is False
    assert _Handler.seen[-1]["host"] == "localhost" and "FlintelBot" in _Handler.seen[-1]["ua"]


def test_the_real_transport_enforces_the_size_cap(server):
    status, headers, body, truncated = sf._default_transport("http", "localhost", server, "/big", "127.0.0.1", 5.0, 50000)
    assert status == 200 and truncated is True and len(body) == 50000


def test_the_real_transport_stops_a_stalling_server_at_the_deadline(server):
    t0 = time.monotonic()
    with pytest.raises((TimeoutError, socket.timeout)):
        sf._default_transport("http", "localhost", server, "/stall", "127.0.0.1", 0.6, 100000)
    assert time.monotonic() - t0 < 2.0


def test_a_refused_connection_becomes_a_clean_fetch_failure(loopback_allowed):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()          # a port nobody listens on
    with pytest.raises(sf.UnsafeUrl):                                                           # port 80/443 rule fires first for an arbitrary port
        sf.fetch_public_page(f"http://127.0.0.1:{port}/", resolve=lambda h, p: ["127.0.0.1"])


def test_the_whole_fetch_connects_to_the_validated_address_and_keeps_the_original_host_header(server, loopback_allowed, monkeypatch):
    monkeypatch.setattr(sf, "ALLOWED_PORTS", (server,))                                          # the test server is on an ephemeral port
    out = sf.fetch_public_page(f"http://public.example:{server}/ok", resolve=lambda h, p: ["127.0.0.1"])      # "public.example" is NOT resolvable; the address is pinned
    assert out["status"] == 200 and "Hello" in out["text"]
    assert _Handler.seen[-1]["host"] in ("public.example", f"public.example:{server}")           # the name, never the address, is what the server is asked for


def test_a_redirect_to_the_metadata_address_is_refused_after_a_real_first_hop(server, loopback_allowed, monkeypatch):
    monkeypatch.setattr(sf, "ALLOWED_PORTS", (server, 80, 443))
    with pytest.raises(sf.UnsafeUrl):
        sf.fetch_public_page(f"http://public.example:{server}/redirect-internal", resolve=lambda h, p: ["127.0.0.1"])
    assert _Handler.seen[-1]["path"] == "/redirect-internal"                                      # the first hop really happened; the second was never requested


def test_a_same_site_redirect_is_followed_and_a_non_page_is_refused(server, loopback_allowed, monkeypatch):
    monkeypatch.setattr(sf, "ALLOWED_PORTS", (server,))
    out = sf.fetch_public_page(f"http://public.example:{server}/redirect-ok", resolve=lambda h, p: ["127.0.0.1"])
    assert out["redirects"] == 1 and "Hello" in out["text"]
    with pytest.raises(sf.FetchFailed):
        sf.fetch_public_page(f"http://public.example:{server}/pdf", resolve=lambda h, p: ["127.0.0.1"])


def test_the_total_deadline_covers_a_slow_server(server, loopback_allowed, monkeypatch):
    monkeypatch.setattr(sf, "ALLOWED_PORTS", (server,))
    t0 = time.monotonic()
    with pytest.raises(sf.FetchFailed):
        sf.fetch_public_page(f"http://public.example:{server}/stall", resolve=lambda h, p: ["127.0.0.1"], timeout=0.5, total_deadline=1.0)
    assert time.monotonic() - t0 < 2.5


def test_a_stuck_name_lookup_is_abandoned_at_its_deadline(monkeypatch):
    monkeypatch.setattr(sf.socket, "getaddrinfo", lambda *a, **k: time.sleep(3))
    t0 = time.monotonic()
    with pytest.raises(OSError):
        sf.default_resolve("slow.example", 443, timeout=0.3)
    assert time.monotonic() - t0 < 1.5
    with pytest.raises(sf.UnsafeUrl):                                                           # and check_url reports it as "could not be found", not a hang
        sf.check_url("https://slow.example/", resolve=lambda h, p: (_ for _ in ()).throw(OSError("timed out")))


# ── TLS: the certificate IS verified against the hostname ───────────────────────────────────────────────────────────────────
@pytest.fixture
def tls_server(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], str(crt)
    srv.shutdown()
    srv.server_close()


def test_an_untrusted_certificate_is_rejected(tls_server, monkeypatch):
    port, _crt = tls_server
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    with pytest.raises(ssl.SSLCertVerificationError):
        sf._default_transport("https", "localhost", port, "/ok", "127.0.0.1", 3.0, 100000)


def test_a_trusted_certificate_works_and_the_hostname_is_checked(tls_server, monkeypatch):
    port, crt = tls_server
    monkeypatch.setenv("SSL_CERT_FILE", crt)                                                     # trust exactly this self-signed certificate
    status, headers, body, truncated = sf._default_transport("https", "localhost", port, "/ok", "127.0.0.1", 3.0, 100000)
    assert status == 200 and b"Hello" in body
    with pytest.raises(ssl.SSLCertVerificationError):                                            # same server, wrong hostname: the name check is on
        sf._default_transport("https", "other.example", port, "/ok", "127.0.0.1", 3.0, 100000)


# ── servers that answer oddly, and connection / DNS failures (raw sockets, loopback) ─────────────────────────────────────────
import gzip
import struct
import zlib

BODY = b"<html><title>Hello World</title><h1>Real content here</h1></html>"


@pytest.fixture
def raw_server(monkeypatch):
    """serve(handler) -> port. `handler(conn)` is called after the request was read. Loopback is allowed and the port is whitelisted for the test."""
    servers = []
    monkeypatch.setattr(sf, "_is_public", lambda ip: str(ip) == "127.0.0.1")

    def serve(handler):
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5)
        port = srv.getsockname()[1]
        servers.append(srv)

        def loop():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=lambda c=c: (c.recv(4096), handler(c)), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()
        monkeypatch.setattr(sf, "ALLOWED_PORTS", (port,))
        return port
    yield serve
    for s in servers:
        s.close()


def _get(port, **kw):
    return sf.fetch_public_page(f"http://public.example:{port}/", resolve=lambda h, p: ["127.0.0.1"], timeout=2, total_deadline=4, **kw)


def test_a_chunked_answer_to_an_http_1_0_request_is_decoded_not_returned_as_chunk_markers(raw_server):
    def h(c):
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n" + hex(len(BODY))[2:].encode() + b"\r\n" + BODY + b"\r\n0\r\n\r\n"); c.close()
    assert _get(raw_server(h))["text"] == BODY.decode()


@pytest.mark.parametrize("enc,pack", [("gzip", gzip.compress), ("deflate", zlib.compress), ("deflate", lambda b: zlib.compress(b)[2:-4])])
def test_a_compressed_answer_is_decompressed_even_though_identity_was_requested(raw_server, enc, pack):
    z = pack(BODY)

    def h(c):
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: " + enc.encode() + b"\r\nContent-Length: " + str(len(z)).encode() + b"\r\n\r\n" + z); c.close()
    assert _get(raw_server(h))["text"] == BODY.decode()


def test_a_compression_bomb_cannot_exceed_the_size_cap(raw_server):
    bomb = gzip.compress(b"A" * 50_000_000)                                                   # 50 MB of one letter, a few KB on the wire

    def h(c):
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: gzip\r\n\r\n" + bomb); c.close()
    out = _get(raw_server(h), max_bytes=100_000)
    assert out["truncated"] is True and len(out["text"]) <= 100_000


def test_an_unreadable_encoding_is_refused_not_shown_as_text(raw_server):
    def h(c):
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: br\r\n\r\n\x8b\x01\x80garbage"); c.close()
    with pytest.raises(sf.FetchFailed):
        _get(raw_server(h))


def test_a_corrupt_gzip_body_is_a_clean_failure(raw_server):
    def h(c):
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: gzip\r\n\r\nthis is not gzip"); c.close()
    with pytest.raises(sf.FetchFailed):
        _get(raw_server(h))


def test_the_page_charset_is_honoured_and_a_missing_content_type_is_treated_as_html(raw_server):
    def latin(c):
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=iso-8859-1\r\n\r\n<title>Caf\xe9 cr\xe8me</title>"); c.close()
    assert "Café crème" in _get(raw_server(latin))["text"]

    def bare(c):
        c.sendall(b"HTTP/1.1 200 OK\r\n\r\n" + BODY); c.close()
    assert "Real content" in _get(raw_server(bare))["text"]


def _reset(c):
    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 5000\r\n\r\n<html>partial")
    c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)); c.close()


@pytest.mark.parametrize("handler", [_reset, lambda c: c.close(), lambda c: (c.sendall(b"not http at all\r\n\r\nxx"), c.close())],
                         ids=["reset-mid-body", "closes-at-once", "not-http"])
def test_connection_level_failures_are_clean_fetch_failures(raw_server, handler):
    with pytest.raises(sf.FetchFailed) as e:
        _get(raw_server(handler))
    assert "could not be reached" in str(e.value)


def test_https_to_a_server_that_only_speaks_http_is_a_clean_failure(raw_server, monkeypatch):
    port = raw_server(lambda c: (c.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n"), c.close()))
    with pytest.raises(sf.FetchFailed):
        sf.fetch_public_page(f"https://public.example:{port}/", resolve=lambda h, p: ["127.0.0.1"], timeout=2, total_deadline=4)


@pytest.mark.parametrize("answer,message", [(lambda h, p: (_ for _ in ()).throw(socket.gaierror("no such host")), "could not be found"),
                                            (lambda h, p: [], "could not be found"), (lambda h, p: (_ for _ in ()).throw(OSError("timed out")), "could not be found"),
                                            (lambda h, p: ["93.184.216.34", "10.0.0.1"], "not a public website"),
                                            (lambda h, p: ["93.184.216.34", "169.254.169.254"], "not a public website")])
def test_name_lookup_failures_and_mixed_answers_are_refused_with_a_clear_message(answer, message):
    with pytest.raises(sf.UnsafeUrl) as e:
        sf.check_url("https://x.example/", resolve=answer)
    assert message in str(e.value)


def test_a_redirect_to_a_name_that_does_not_resolve_is_refused_on_that_hop(raw_server, monkeypatch):
    def h(c):
        c.sendall(b"HTTP/1.1 302 Found\r\nLocation: http://gone.example/\r\n\r\n"); c.close()
    port = raw_server(h)
    monkeypatch.setattr(sf, "ALLOWED_PORTS", (port, 80))

    def resolve(host, p):
        if host == "gone.example":
            raise socket.gaierror("no such host")
        return ["127.0.0.1"]
    with pytest.raises(sf.UnsafeUrl) as e:
        sf.fetch_public_page(f"http://public.example:{port}/", resolve=resolve, timeout=2, total_deadline=4)
    assert "could not be found" in str(e.value)
