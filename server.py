#!/usr/bin/env python3
"""
HTTP/1.1 calculator server on a raw socket. No frameworks.

    python server.py [port]        # default 8080

Routes (GET only):
    /add?a=2&b=3   -> 200  5
    /sub?a=10&b=4  -> 200  6
    /mul?a=6&b=7   -> 200  42
    /div?a=9&b=3   -> 200  3

Errors:
    400  bad/missing numbers, divide by zero, missing Host, malformed request
    404  unknown path
    405  method other than GET (Allow: GET)

The connection stays open across requests. Each request is delimited by
its own headers plus exactly Content-Length body bytes (or a chunked body),
so several pipelined requests arriving in one recv() are answered in order.
"""

import decimal
import socket
import sys
import threading
from email.utils import formatdate
from urllib.parse import urlsplit, parse_qsl

HOST = "0.0.0.0"
PORT = 8080
IDLE_TIMEOUT = 10.0       # seconds a connection may sit with no bytes arriving
MAX_HEADER_BYTES = 8192   # request line + headers; larger is rejected with 431
MAX_BODY_BYTES = 1 << 20  # 1 MiB; we never need a body, but we must consume it
RECV_SIZE = 4096

REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    505: "HTTP Version Not Supported",
}


class BadRequest(Exception):
    """Raised when a request cannot be parsed. After one of these the
    byte stream cannot be trusted, so the server replies and hangs up."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Request:
    __slots__ = ("method", "target", "version", "headers", "body")

    def __init__(self, method, target, version, headers, body):
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers  # dict, lower-cased names
        self.body = body


# --------------------------------------------------------------------------
# Wire format: building responses
# --------------------------------------------------------------------------

def build_response(status, body="", close=False, extra_headers=None):
    payload = body.encode("utf-8") if isinstance(body, str) else body
    lines = [
        f"HTTP/1.1 {status} {REASONS.get(status, 'Unknown')}",
        f"Date: {formatdate(usegmt=True)}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(payload)}",
        "Connection: " + ("close" if close else "keep-alive"),
    ]
    for name, value in (extra_headers or {}).items():
        lines.append(f"{name}: {value}")
    head = "\r\n".join(lines) + "\r\n\r\n"
    return head.encode("ascii") + payload


# --------------------------------------------------------------------------
# Wire format: reading exactly one request from a buffered connection
# --------------------------------------------------------------------------

class Connection:
    """Wraps a socket with a byte buffer so we can pull out exactly one
    request at a time and keep the leftover bytes for the next one."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.eof = False

    def _fill(self):
        """Read more bytes into the buffer. Returns False on EOF."""
        if self.eof:
            return False
        chunk = self.sock.recv(RECV_SIZE)
        if not chunk:
            self.eof = True
            return False
        self.buf += chunk
        return True

    def _read_until(self, marker, limit, too_big_status):
        """Return bytes up to and including marker. Returns None on a clean
        EOF with nothing buffered (the peer hung up between requests)."""
        while True:
            idx = self.buf.find(marker)
            if idx != -1:
                end = idx + len(marker)
                if end > limit:
                    raise BadRequest(too_big_status, "header block too large")
                data, self.buf = self.buf[:end], self.buf[end:]
                return data
            if len(self.buf) > limit:
                raise BadRequest(too_big_status, "header block too large")
            if not self._fill():
                if not self.buf:
                    return None
                raise BadRequest(400, "connection closed mid-request")

    def _read_exact(self, n):
        """Return exactly n bytes, leaving everything after them in place."""
        while len(self.buf) < n:
            if not self._fill():
                raise BadRequest(400, "connection closed mid-body")
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def read_request(self):
        """Parse one request. Returns None if the peer closed cleanly
        between requests. Raises BadRequest on garbage."""
        # Tolerate stray CRLFs before the request line (RFC 7230 section
        # 3.5). They may already be buffered from a previous request or may
        # still be on the wire, so strip and refill until real bytes appear.
        while True:
            while self.buf.startswith(b"\r\n"):
                self.buf = self.buf[2:]
            if self.buf and self.buf != b"\r":
                break
            if not self._fill():
                return None

        head = self._read_until(b"\r\n\r\n", MAX_HEADER_BYTES, 431)
        if head is None:
            return None

        text = head.decode("iso-8859-1")
        lines = text.split("\r\n")
        # The trailing blank line leaves ["", ""] at the end; drop them.
        request_line, header_lines = lines[0], lines[1:-2]

        parts = request_line.split(" ")
        if len(parts) != 3 or not parts[0] or not parts[1]:
            raise BadRequest(400, "malformed request line")
        method, target, version = parts
        if version not in ("HTTP/1.0", "HTTP/1.1"):
            raise BadRequest(505, "unsupported HTTP version")

        headers = {}
        for line in header_lines:
            if ":" not in line:
                raise BadRequest(400, f"malformed header line: {line!r}")
            name, value = line.split(":", 1)
            if not name or name != name.strip():
                # RFC 7230 section 3.2.4: whitespace before the colon is an error
                raise BadRequest(400, "malformed header name")
            name = name.lower()
            value = value.strip()
            # Fold duplicates; matters for Content-Length disagreement.
            headers[name] = value if name not in headers else headers[name] + ", " + value

        body = self._read_body(headers)
        # Methods are case-sensitive (RFC 7230 section 3.1.1): "get" is not GET.
        return Request(method, target, version, headers, body)

    def _read_body(self, headers):
        te = headers.get("transfer-encoding")
        cl = headers.get("content-length")

        if te is not None:
            if cl is not None:
                # Classic request-smuggling ambiguity; refuse and hang up.
                raise BadRequest(400, "both Transfer-Encoding and Content-Length")
            if te.lower() != "chunked":
                raise BadRequest(400, f"unsupported Transfer-Encoding {te!r}")
            return self._read_chunked()

        if cl is None:
            return b""
        values = {v.strip() for v in cl.split(",")}
        if len(values) != 1:
            raise BadRequest(400, "conflicting Content-Length values")
        raw = values.pop()
        if not raw.isdigit():
            raise BadRequest(400, "invalid Content-Length")
        length = int(raw)
        if length > MAX_BODY_BYTES:
            raise BadRequest(413, "body too large")
        # This is the line the assignment is about: exactly `length` bytes.
        # Byte length+1 stays in self.buf and belongs to the next request.
        return self._read_exact(length)

    def _read_chunked(self):
        body = b""
        while True:
            size_line = self._read_until(b"\r\n", MAX_HEADER_BYTES, 400)
            if size_line is None:
                raise BadRequest(400, "connection closed inside chunked body")
            size_text = size_line[:-2].split(b";", 1)[0].strip()  # drop extensions
            try:
                size = int(size_text, 16)
            except ValueError:
                raise BadRequest(400, "bad chunk size")
            if size == 0:
                break
            if len(body) + size > MAX_BODY_BYTES:
                raise BadRequest(413, "body too large")
            body += self._read_exact(size)
            if self._read_exact(2) != b"\r\n":
                raise BadRequest(400, "chunk not terminated by CRLF")
        # Trailers: consume header lines until the blank line.
        while True:
            line = self._read_until(b"\r\n", MAX_HEADER_BYTES, 400)
            if line is None:
                raise BadRequest(400, "connection closed inside trailers")
            if line == b"\r\n":
                return body


# --------------------------------------------------------------------------
# Application: the calculator
# --------------------------------------------------------------------------

def _div(a, b):
    # Caller has already rejected b == 0.
    if a % b == 0:
        return a // b
    try:
        return a / b
    except OverflowError:
        # Quotient exceeds float range (~1e308). Fall back to Decimal with
        # float-equivalent precision so arbitrary-size ints keep working.
        with decimal.localcontext() as ctx:
            ctx.prec = 17
            return decimal.Decimal(a) / decimal.Decimal(b)


OPERATIONS = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": _div,
}


def parse_int(params, key):
    if key not in params:
        raise ValueError(f"missing parameter {key}")
    raw = params[key].strip()
    try:
        return int(raw, 10)
    except ValueError:
        raise ValueError(f"parameter {key} is not an integer: {raw!r}")


def handle(req):
    """Turn a parsed Request into (status, body). Never raises."""
    if "host" not in req.headers:
        return 400, "missing Host header"

    try:
        url = urlsplit(req.target)
    except ValueError as e:
        # e.g. an unbalanced IPv6 bracket in an absolute-form target
        return 400, f"malformed request target: {e}"
    path = url.path

    if path not in OPERATIONS:
        return 404, f"no such route: {path}"

    if req.method != "GET":
        return 405, f"method {req.method} not allowed; use GET"

    params = dict(parse_qsl(url.query, keep_blank_values=True))
    try:
        a = parse_int(params, "a")
        b = parse_int(params, "b")
    except ValueError as e:
        return 400, str(e)

    if path == "/div" and b == 0:
        return 400, "division by zero"

    return 200, str(OPERATIONS[path](a, b))


def wants_close(req):
    """Close after this response? HTTP/1.0 defaults to close, HTTP/1.1
    defaults to keep-alive, and the Connection header overrides both."""
    conn = req.headers.get("connection", "").lower()
    tokens = {t.strip() for t in conn.split(",")}
    if req.version == "HTTP/1.0":
        return "keep-alive" not in tokens
    return "close" in tokens


# --------------------------------------------------------------------------
# Per-connection loop
# --------------------------------------------------------------------------

def serve_connection(sock, peer):
    sock.settimeout(IDLE_TIMEOUT)
    conn = Connection(sock)
    served = 0
    try:
        while True:
            try:
                req = conn.read_request()
            except socket.timeout:
                # Idle client. If a partial request is sitting in the
                # buffer, say why we are leaving; otherwise just hang up.
                if conn.buf:
                    sock.sendall(build_response(408, "request timed out", close=True))
                log(peer, f"idle timeout after {served} request(s)")
                return
            except BadRequest as e:
                sock.sendall(build_response(e.status, e.message, close=True))
                log(peer, f"{e.status} {e.message} (closing)")
                return

            if req is None:
                log(peer, f"client closed after {served} request(s)")
                return

            status, body = handle(req)
            close = wants_close(req)
            extra = {"Allow": "GET"} if status == 405 else None
            sock.sendall(build_response(status, body, close=close, extra_headers=extra))
            served += 1
            log(peer, f"{req.method} {req.target} -> {status} {body}")
            if close:
                log(peer, "Connection: close honoured")
                return
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        log(peer, "connection reset by peer")
    except Exception as e:  # last resort; never take the server down
        log(peer, f"unexpected error: {e!r}")
        try:
            sock.sendall(build_response(500, "internal error", close=True))
        except OSError:
            pass
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


def log(peer, msg):
    print(f"[{peer[0]}:{peer[1]}] {msg}", flush=True)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: SO_REUSEADDR would let a second server bind the same
            # port and silently steal connections. Exclusive is what we want;
            # Windows does not have the Unix TIME_WAIT rebind problem.
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, port))
        srv.listen(16)
        print(f"listening on {HOST}:{port}  (idle timeout {IDLE_TIMEOUT:.0f}s)", flush=True)
        while True:
            client, peer = srv.accept()
            threading.Thread(target=serve_connection, args=(client, peer), daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
