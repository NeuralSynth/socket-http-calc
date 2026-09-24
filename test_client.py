#!/usr/bin/env python3
"""
Grader-style checks for server.py, using nothing but the socket module.

    python test_client.py [host] [port]      # default localhost 8080

Every scenario opens exactly ONE socket and counts how many complete
responses it gets back before the socket is done.
"""

import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8080

# The six requests from the assignment sheet, with the expected results.
GRADER_SCRIPT = [
    ("GET", "/add?a=2&b=3", 200, "5"),
    ("GET", "/sub?a=10&b=4", 200, "6"),
    ("GET", "/mul?a=6&b=7", 200, "42"),
    ("GET", "/div?a=1&b=0", 400, None),
    ("GET", "/pow?a=2&b=8", 404, None),
    ("POST", "/add", 405, None),
]

EXTRA_CASES = [
    ("GET", "/div?a=9&b=3", 200, "3"),
    ("GET", "/add?a=x&b=3", 400, None),
    ("GET", "/add?a=2", 400, None),
    ("GET", "/sub?a=-5&b=-7", 200, "2"),
    ("GET", "/div?a=7&b=2", 200, "3.5"),
    ("GET", "/mul?a=99999999999&b=99999999999", 200, "9999999999800000000001"),
]


# --------------------------------------------------------------------------
# A tiny HTTP/1.1 response reader with the same framing discipline we
# demand of the server: consume exactly Content-Length bytes, keep the rest.
# --------------------------------------------------------------------------

class Reader:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def _fill(self):
        chunk = self.sock.recv(4096)
        if not chunk:
            raise EOFError("server closed the connection")
        self.buf += chunk

    def response(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        length = int(headers["content-length"])
        while len(self.buf) < length:
            self._fill()
        body, self.buf = self.buf[:length], self.buf[length:]
        return status, headers, body.decode("utf-8")


def request_bytes(method, target, version="HTTP/1.1", host="localhost", extra=""):
    lines = [f"{method} {target} {version}"]
    if host is not None:
        lines.append(f"Host: {host}")
    if extra:
        lines.append(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def socket_still_open(sock):
    """Peek without blocking. Open+idle -> BlockingIOError. Closed -> b''."""
    sock.setblocking(False)
    try:
        data = sock.recv(1, socket.MSG_PEEK)
        return data != b""
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        sock.setblocking(True)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def run_sequential(cases, label):
    """One socket; send a request, wait for its answer, repeat."""
    s = socket.create_connection((HOST, PORT))
    s.settimeout(5)
    r = Reader(s)
    for method, target, want_status, want_body in cases:
        s.sendall(request_bytes(method, target))
        status, headers, body = r.response()
        check(status == want_status,
              f"{method} {target}: expected {want_status}, got {status} ({body!r})")
        if want_body is not None:
            check(body == want_body,
                  f"{method} {target}: expected body {want_body!r}, got {body!r}")
        check(headers.get("connection") == "keep-alive",
              f"{method} {target}: server did not advertise keep-alive")
        print(f"   {method:<4} {target:<40} -> {status:<4} {body}")
    check(socket_still_open(s), "socket was closed before we were done")
    print(f"   socket still open: True")
    print(f"   1 TCP handshake, {len(cases)} responses")
    s.close()


def run_pipelined(cases, label):
    """One socket; blast every request in a single send(), then read all
    the answers in order. Nothing separates them but Content-Length."""
    s = socket.create_connection((HOST, PORT))
    s.settimeout(5)
    r = Reader(s)
    blob = b"".join(request_bytes(m, t) for m, t, _, _ in cases)
    s.sendall(blob)
    for method, target, want_status, want_body in cases:
        status, _, body = r.response()
        check(status == want_status,
              f"pipelined {method} {target}: expected {want_status}, got {status}")
        if want_body is not None:
            check(body == want_body,
                  f"pipelined {method} {target}: expected {want_body!r}, got {body!r}")
        print(f"   {method:<4} {target:<40} -> {status:<4} {body}")
    check(r.buf == b"", f"server sent {len(r.buf)} unexpected trailing bytes")
    check(socket_still_open(s), "socket was closed after pipelined batch")
    print(f"   sent {len(blob)} bytes in one write, got {len(cases)} responses, socket still open")
    s.close()


def run_missing_host():
    s = socket.create_connection((HOST, PORT))
    s.settimeout(5)
    r = Reader(s)
    s.sendall(request_bytes("GET", "/add?a=1&b=2", host=None))
    status, _, body = r.response()
    check(status == 400, f"no-Host request: expected 400, got {status}")
    print(f"   GET  /add (no Host)                       -> {status}  {body}")
    # The connection must survive a 400 that was a *semantic* error.
    s.sendall(request_bytes("GET", "/add?a=1&b=2"))
    status, _, body = r.response()
    check(status == 200 and body == "3", "connection did not survive the 400")
    print(f"   GET  /add?a=1&b=2  (same socket)          -> {status}  {body}")
    s.close()


def run_body_framing():
    """A GET with a body whose bytes look like another request. If the
    server reads one byte too many or too few, the next request breaks."""
    s = socket.create_connection((HOST, PORT))
    s.settimeout(5)
    r = Reader(s)
    decoy = b"GET /mul?a=9&b=9 HTTP/1.1\r\nHost: x\r\n\r\n"
    first = request_bytes("GET", "/add?a=1&b=1", extra=f"Content-Length: {len(decoy)}") + decoy
    second = request_bytes("GET", "/sub?a=5&b=3")
    s.sendall(first + second)
    st1, _, b1 = r.response()
    st2, _, b2 = r.response()
    check((st1, b1) == (200, "2"), f"first framed request wrong: {st1} {b1!r}")
    check((st2, b2) == (200, "2"), f"second framed request wrong: {st2} {b2!r}")
    check(r.buf == b"", "server answered the decoy inside the body")
    print(f"   body of {len(decoy)} bytes swallowed exactly; decoy not executed")

    # Same idea with chunked encoding.
    chunked = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    s.sendall(request_bytes("GET", "/add?a=20&b=22", extra="Transfer-Encoding: chunked") + chunked
              + request_bytes("GET", "/add?a=1&b=2"))
    st1, _, b1 = r.response()
    st2, _, b2 = r.response()
    check((st1, b1) == (200, "42"), f"chunked request wrong: {st1} {b1!r}")
    check((st2, b2) == (200, "3"), f"request after chunked wrong: {st2} {b2!r}")
    print(f"   chunked body decoded; following request unharmed")
    s.close()


def run_connection_close():
    s = socket.create_connection((HOST, PORT))
    s.settimeout(5)
    r = Reader(s)
    s.sendall(request_bytes("GET", "/add?a=1&b=2", extra="Connection: close"))
    status, headers, body = r.response()
    check(status == 200 and body == "3", "Connection: close request failed")
    check(headers.get("connection") == "close", "server did not echo Connection: close")
    check(s.recv(1) == b"", "server did not close after Connection: close")
    print(f"   Connection: close -> {status} {body}, then server closed")
    s.close()

    # HTTP/1.0 without keep-alive must also close.
    s = socket.create_connection((HOST, PORT))
    s.settimeout(5)
    r = Reader(s)
    s.sendall(request_bytes("GET", "/add?a=1&b=2", version="HTTP/1.0"))
    status, headers, body = r.response()
    check(status == 200 and body == "3", "HTTP/1.0 request failed")
    check(s.recv(1) == b"", "server kept an HTTP/1.0 connection open")
    print(f"   HTTP/1.0 request -> {status} {body}, then server closed")
    s.close()


def run_malformed():
    s = socket.create_connection((HOST, PORT))
    s.settimeout(5)
    r = Reader(s)
    s.sendall(b"this is not http\r\n\r\n")
    status, headers, _ = r.response()
    check(status == 400, f"garbage: expected 400, got {status}")
    check(headers.get("connection") == "close", "garbage should force close")
    check(s.recv(1) == b"", "server did not close after unparseable request")
    print(f"   garbage request -> {status}, connection closed (stream untrustworthy)")
    s.close()


def run_idle_timeout(limit=10.0):
    s = socket.create_connection((HOST, PORT))
    s.settimeout(limit + 5)
    t0 = time.time()
    data = s.recv(1)
    elapsed = time.time() - t0
    check(data == b"", "expected server to close the idle connection")
    check(elapsed >= limit - 1, f"closed too early: {elapsed:.1f}s")
    print(f"   idle socket closed by server after {elapsed:.1f}s")
    s.close()


SCENARIOS = [
    ("grader script, sequential", lambda: run_sequential(GRADER_SCRIPT, "grader")),
    ("grader script, pipelined", lambda: run_pipelined(GRADER_SCRIPT, "grader")),
    ("extra arithmetic and validation cases", lambda: run_sequential(EXTRA_CASES, "extra")),
    ("missing Host header", run_missing_host),
    ("Content-Length and chunked body framing", run_body_framing),
    ("Connection: close and HTTP/1.0", run_connection_close),
    ("malformed request", run_malformed),
]


def main():
    quick = "--quick" in sys.argv
    scenarios = list(SCENARIOS)
    if not quick:
        scenarios.append(("idle timeout (waits ~10s)", run_idle_timeout))
    failed = 0
    for name, fn in scenarios:
        print(f"\n== {name}")
        try:
            fn()
            print("   PASS")
        except Exception as e:
            failed += 1
            print(f"   FAIL: {e}")
    print(f"\n{len(scenarios) - failed}/{len(scenarios)} scenarios passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
