"""HTTP guard for the whole app: `X-Content-Type-Options: nosniff` on EVERY response (JSON and error bodies included) and a request-rate limit on authenticated /v2 routes.

The rate limit is a token bucket per session cookie (hash only, the token is never stored or logged): a burst of `RIPPLE_RATE_BURST` (default 150) requests, refilled at
`RIPPLE_RATE_PER_SEC` (default 10/s, 600 per minute sustained). A page that polls and loads several panels stays far below it; a script that hammers an authenticated route gets 429 with
`Retry-After`. Limits: per process (a second worker process has its own buckets), and keyed by session, so it bounds one session, not a distributed flood (that belongs to the front door).
Login has its own throttle and is not changed here.

Several instances (Cloud Run scales out) or processes would each keep their own bucket, so one session could use N times the limit. With `RIPPLE_RATE_SHARED=1` a SHARED fixed-window
counter in the database is applied as well, per session (`RIPPLE_RATE_SESSION_PER_MIN`, default 900) and per source address (`RIPPLE_RATE_SOURCE_PER_MIN`, default 3000: judges behind one
office address are many people). The source address is the one the Cloud Run front end appended last to X-Forwarded-For (never a value the client can choose). Several judges sharing ONE demo
account have separate sessions, so they do not consume each other's session limit. If the database cannot be reached the shared counter fails OPEN (the local bucket still applies): a
database outage must not lock everybody out of a demo, and it is stated here, not hidden.

Failure behaviour of the shared counter (decided per kind of request, so a counter fault never becomes unlimited power):
  * reads (GET/HEAD/OPTIONS): fail OPEN (the local bucket still applies);
  * every write (POST/PUT/PATCH/DELETE: approve, apply, accept, ...) and every AI start (POST /v2/jobs, /v2/chat...): fail CLOSED with 503 + Retry-After;
  * the login throttle (`login_*` below, used by /v2/auth/login): fail CLOSED with the same generic 429 (login needs the database anyway).
Extra shared windows per session for writes (`RIPPLE_RATE_WRITE_PER_MIN`, default 180) and AI starts (`RIPPLE_RATE_AI_PER_MIN`, default 30).
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import os
import random
import time
import uuid

from starlette.responses import JSONResponse

_COOKIE = "ripple_v2"
_log = logging.getLogger("ripple.http")
# The id of the request being served (set by the request-id middleware; empty outside a request). Server-generated, random, never derived from the client.
from infra.correlation import REQUEST_ID                                      # noqa: E402  (AH-05: the one context variable the stores read for correlation)


def current_request_id() -> str:
    return REQUEST_ID.get()
_MAX_BUCKETS = 20000
_buckets: dict[str, list[float]] = {}                 # key -> [tokens, last_refill]


def _cfg() -> tuple[float, float]:
    def num(name: str, default: float) -> float:
        try:
            v = float(os.environ.get(name, ""))
            return v if v > 0 else default
        except ValueError:
            return default
    return num("RIPPLE_RATE_BURST", 150.0), num("RIPPLE_RATE_PER_SEC", 10.0)


def reset() -> None:
    _buckets.clear()


def take(key: str, now: float | None = None) -> float:
    """0.0 = allowed; otherwise the seconds to wait."""
    burst, rate = _cfg()
    now = time.monotonic() if now is None else now
    b = _buckets.get(key)
    if b is None:
        if len(_buckets) >= _MAX_BUCKETS:
            _buckets.clear()                          # bounded memory: a reset only ever LOOSENS one session's bucket, never blocks anyone
        b = _buckets[key] = [burst, now]
    b[0] = min(burst, b[0] + (now - b[1]) * rate)
    b[1] = now
    if b[0] >= 1.0:
        b[0] -= 1.0
        return 0.0
    return (1.0 - b[0]) / rate


_shared_ready = False


def _shared_dsn() -> str | None:
    return os.environ.get("RIPPLE_V2_DSN") if os.environ.get("RIPPLE_RATE_SHARED") == "1" else None


def _per_min(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


def shared_hit(dsn: str, keys: list[tuple[str, int]], now: float | None = None) -> int:
    """Count one request against each (key, limit) in the current one-minute window of the shared table. Returns the seconds to wait (0 = allowed). Raises on database errors (caller fails open)."""
    global _shared_ready
    import psycopg2
    window = int((time.time() if now is None else now) // 60)
    from infra import db_pool
    with db_pool.connection(dsn) as conn:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                if not _shared_ready:
                    if os.environ.get("RIPPLE_LOGIN_SCHEMA_AUTO", "1") != "0":      # the table is migration v2_042's; production (0) issues no DDL on a request
                        cur.execute("CREATE TABLE IF NOT EXISTS v2_rate_windows (key TEXT NOT NULL, window_start BIGINT NOT NULL, hits INTEGER NOT NULL, PRIMARY KEY (key, window_start))")
                    _shared_ready = True
                wait = 0
                for key, limit in keys:
                    cur.execute("INSERT INTO v2_rate_windows (key, window_start, hits) VALUES (%s,%s,1) ON CONFLICT (key, window_start) DO UPDATE SET hits = v2_rate_windows.hits + 1 RETURNING hits", (key, window))
                    if cur.fetchone()[0] > limit:
                        wait = max(wait, int(60 - ((time.time() if now is None else now) % 60)) + 1)
                if random.random() < 0.01:
                    cur.execute("DELETE FROM v2_rate_windows WHERE window_start < %s", (window - 10,))
                return wait
        finally:
            conn.autocommit = False


def _is_ai_start(request) -> bool:
    path = request.url.path
    return request.method == "POST" and (path == "/v2/jobs" or path.startswith("/v2/chat"))


def login_enabled() -> bool:
    return _shared_dsn() is not None


def _login_limits() -> tuple[int, int, int]:
    return _per_min("RIPPLE_LOGIN_MAX_FAILURES", 5), _per_min("RIPPLE_LOGIN_SOURCE_MAX_FAILURES", 100), _per_min("RIPPLE_LOGIN_WINDOW_SECONDS", 300)


@contextlib.contextmanager
def _login_conn():
    """A transaction-mode connection for the login counters (SEC-02: every decision and write is one transaction in `infra/login_protection.py`).
    The table is owned by the schema (migration v2_042); RIPPLE_LOGIN_SCHEMA_AUTO=0 (production) makes this path issue no DDL."""
    global _login_ready
    from infra import db_pool, login_protection
    with db_pool.connection(_shared_dsn()) as conn:
        conn.autocommit = False
        try:
            if not _login_ready:
                if os.environ.get("RIPPLE_LOGIN_SCHEMA_AUTO", "1") != "0":
                    login_protection.ensure_schema(conn)
                _login_ready = True
            yield conn
        finally:
            try:
                conn.rollback()
            except Exception:                                   # noqa: BLE001
                pass


_login_ready = False


def login_wait(account, source, now: float | None = None) -> int:
    """Shared (all instances) lock state: seconds to wait, 0 = not locked. RAISES on a database error: the caller must treat that as locked (fail closed)."""
    from infra import login_protection
    with _login_conn() as conn:
        return login_protection.shared_wait(conn, account, source, now)


def login_failure(account, source, now: float | None = None) -> bool:
    """Record a failed credential check in the shared counters; True when the row ceiling refused a new key (answer 429)."""
    from infra import login_protection
    with _login_conn() as conn:
        return login_protection.shared_failure(conn, account, source, now)


def login_clear(account) -> None:
    from infra import login_protection
    with _login_conn() as conn:
        login_protection.shared_clear(conn, account)


def _source(request) -> str:
    """The caller's address under the ingress contract (RIPPLE_TRUSTED_PROXY_HOPS; `infra/login_protection.source_address`)."""
    from infra import login_protection
    return login_protection.source_address(request)


def install(app) -> None:
    @app.middleware("http")
    async def _guard(request, call_next):
        path = request.url.path
        token = request.cookies.get(_COOKIE)
        if token and path.startswith("/v2/") and path != "/v2/auth/login":
            wait = take(hashlib.sha256(token.encode()).hexdigest())
            if not wait and _shared_dsn():
                write = request.method not in ("GET", "HEAD", "OPTIONS")
                keys = [("s:" + hashlib.sha256(token.encode()).hexdigest(), _per_min("RIPPLE_RATE_SESSION_PER_MIN", 900)), ("a:" + _source(request), _per_min("RIPPLE_RATE_SOURCE_PER_MIN", 3000))]
                sess = keys[0][0][2:]
                if write:
                    keys.append(("w:" + sess, _per_min("RIPPLE_RATE_WRITE_PER_MIN", 180)))
                if _is_ai_start(request):
                    keys.append(("ai:" + sess, _per_min("RIPPLE_RATE_AI_PER_MIN", 30)))
                try:
                    from starlette.concurrency import run_in_threadpool
                    wait = await run_in_threadpool(shared_hit, _shared_dsn(), keys)
                except Exception:                      # noqa: BLE001
                    if write:                          # a counter fault must never become unlimited writes or AI starts: fail CLOSED
                        return JSONResponse({"error": "rate limiter unavailable, try again shortly"}, status_code=503,
                                            headers={"Retry-After": "15", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
                    wait = 0                           # reads fail open (the local bucket still applies)
            if wait:
                return JSONResponse({"error": "too many requests"}, status_code=429,
                                    headers={"Retry-After": str(max(1, int(wait + 0.999))), "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        response = await call_next(request)
        if "x-content-type-options" not in response.headers:
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    # Registered last, so it wraps everything above (including the 429/503 answers of the rate guard). The id lets a person quote a failed request and an
    # operator find its log line; it is random, says nothing about the caller and grants nothing. An incoming X-Request-Id is ignored, never echoed.
    @app.middleware("http")
    async def _request_id(request, call_next):
        rid = uuid.uuid4().hex
        previous = REQUEST_ID.set(rid)
        try:
            try:
                response = await call_next(request)
            except Exception:
                _log.exception("unhandled error request_id=%s method=%s path=%s", rid, request.method, request.url.path)
                raise
            response.headers["X-Request-Id"] = rid
            if response.status_code >= 400:
                _log.warning("request_id=%s method=%s path=%s status=%s", rid, request.method, request.url.path, response.status_code)
            return response
        finally:
            REQUEST_ID.reset(previous)
