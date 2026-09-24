# Calculator that stays on the line

An HTTP/1.1 calculator written against the raw `socket` module. No frameworks,
no `http.server`. One TCP connection serves any number of requests.

## Files

| File             | Purpose                                                        |
|------------------|----------------------------------------------------------------|
| `server.py`      | The server. Python 3.8+, standard library only.                |
| `test_client.py` | Grader-style checks. Also raw sockets, no `requests`/`urllib`. |

## Run

```
python server.py            # listens on 0.0.0.0:8080
python server.py 8090       # any other port
```

In a second terminal:

```
python test_client.py                 # against localhost:8080
python test_client.py localhost 8090  # against another port
python test_client.py localhost 8090 --quick   # skip the 10 s idle-timeout test
```

## Routes

| Request                 | Status | Body                            |
|-------------------------|--------|---------------------------------|
| `GET /add?a=2&b=3`      | 200    | `5`                             |
| `GET /sub?a=10&b=4`     | 200    | `6`                             |
| `GET /mul?a=6&b=7`      | 200    | `42`                            |
| `GET /div?a=9&b=3`      | 200    | `3`                             |
| `GET /div?a=7&b=2`      | 200    | `3.5` (only when not exact)     |
| `GET /div?a=1&b=0`      | 400    | `division by zero`              |
| `GET /add?a=x&b=3`      | 400    | `parameter a is not an integer` |
| `GET /add?a=2`          | 400    | `missing parameter b`           |
| `GET /add` without Host | 400    | `missing Host header`           |
| `GET /pow?a=2&b=8`      | 404    | `no such route: /pow`           |
| `POST /add`             | 405    | `Allow: GET` header included    |

`a` and `b` are base-10 integers, negatives allowed, arbitrary size.
Every response carries `Content-Type`, `Content-Length` and `Connection`.

Check order per request: Host present, then route exists (404), then method
is GET (405), then parameters parse (400), then divide by zero (400).

## Where one request ends and the next begins

`Connection` in `server.py` keeps
a byte buffer per socket and pulls out exactly one request at a time:

1. Read until the first `\r\n\r\n`. That is the request line and headers.
   Anything after it stays in the buffer.
2. Parse `Content-Length`. Read exactly that many more bytes as the body.
   Byte `n+1` is untouched and belongs to the next request.
3. If `Transfer-Encoding: chunked` is present instead, read hex-size lines
   and chunks until the `0` chunk, then skip trailers up to the blank line.
4. Hand the request to the calculator, write a response with its own
   `Content-Length`, loop back to step 1 with whatever is left in the buffer.

Because leftover bytes are kept rather than discarded, pipelining works for
free: the client can send all six requests in one `send()` and gets six
responses back in order.

`test_client.py` proves the framing with a hostile case: a GET whose body
contains the bytes of a *different* valid request. If the server read one
byte too few, the tail of the body would be parsed as garbage. If it read one
byte too many, the real next request would be corrupted. If it ignored the
body entirely, it would execute the decoy. It does none of these.

## When the server hangs up

The stream is only trusted while it can be parsed. The server closes after
responding in these cases, always saying `Connection: close` first:

- The request could not be parsed at all: bad request line, bad header
  syntax, unparseable or conflicting `Content-Length`, both `Content-Length`
  and `Transfer-Encoding` present, header block over 8 KiB, body over 1 MiB.
  After any of these the byte offset of the "next" request is unknowable,
  so resyncing would be guessing.
- The client asked for it with `Connection: close`, or spoke HTTP/1.0 without
  `Connection: keep-alive`.
- The connection has been idle for 10 seconds (see below).

Semantic errors (400 for bad numbers or missing Host, 404, 405) do **not**
close the connection. The request was well-formed, so the framing is intact
and the next request is answered normally.

## Stretch goals covered

- **Connection: close** honoured, and HTTP/1.0 defaults to close.
- **Idle timeout, 10 seconds.** Chosen because the grader sends six requests
  back to back with no think time, so anything over a second is generous for
  a script, while a human at `curl` or `telnet` can still type a request line
  without being cut off. Apache defaults to 5 s, nginx to 75 s; 10 s sits
  where a slow client survives but a forgotten socket does not pin a thread
  for long. If a partial request is in the buffer at timeout the server sends
  `408 Request Timeout` so the client knows why; otherwise it closes quietly.
- **Chunked encoding** on request bodies is decoded (with trailers skipped).
  Responses always use `Content-Length`, since the body is known up front.
- **Pipelining**: all six requests in one write are answered in order.

## Concurrency

One thread per connection. The grader only needs one socket, but this keeps
a second client from being blocked by the first one sitting idle.
