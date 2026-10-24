"""Run a synchronous database unit off the event loop, with bounded admission (AH-01).

Async route handlers parse their body with `await`, then call stores and identity lookups that are synchronous (psycopg2). Run on the event loop, one slow query
stalls every other request of the process. `run_db(fn, *args)` moves the WHOLE synchronous unit (authority lookup included) to a worker thread:

  * admission: a dedicated `anyio.CapacityLimiter` (per event loop) bounds how many DB units run at once (`RIPPLE_DB_WORK_SLOTS`; default = the pool size when the
    pool is on, else 16). Waiting for a slot happens on the event loop without holding a thread, and is bounded by `RIPPLE_DB_POOL_WAIT` (default 15 s); then
    `DbWorkBusy` (a `PoolBusy`, answered 503 + Retry-After by the app's handler).
  * the default thread pool is not used, so synchronous `def` routes and the no-DB liveness route keep their own threads.
  * cancellation: once started, the thread is not abandoned (anyio does not cancel a running thread); the slot is released only after the synchronous unit has
    really ended, so a cancelled request can never let more units run than admitted, and the unit's own transaction / idempotency rules decide its effect.
  * each unit opens, uses and closes its own connections inside the thread (the stores' `_tx`); no connection or cursor crosses threads.
"""
from __future__ import annotations

import asyncio
import functools
import os
import threading
import weakref

import anyio

from infra.db_pool import PoolBusy

_lock = threading.Lock()
_limiters: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple]" = weakref.WeakKeyDictionary()


class DbWorkBusy(PoolBusy):
    """No database work slot became free in time."""


def slots() -> int:
    try:
        n = int(os.environ.get("RIPPLE_DB_WORK_SLOTS", "0"))
    except ValueError:
        n = 0
    if n > 0:
        return n
    try:
        pool = int(os.environ.get("RIPPLE_DB_POOL_MAX", "0"))
    except ValueError:
        pool = 0
    return pool if pool > 0 else 16


def wait_seconds() -> float:
    try:
        return max(0.1, float(os.environ.get("RIPPLE_DB_POOL_WAIT", "15")))
    except ValueError:
        return 15.0


def _limits() -> tuple:
    """(admission limiter, thread limiter) of the running event loop; both sized `slots()` (rebuilt when the setting changes)."""
    loop = asyncio.get_running_loop()
    n = slots()
    with _lock:
        got = _limiters.get(loop)
        if got is None or got[0].total_tokens != n:
            got = (anyio.CapacityLimiter(n), anyio.CapacityLimiter(n))
            _limiters[loop] = got
        return got


def stats() -> dict:
    """Sanitized admission metrics of the current loop (no DSN, no identifiers)."""
    try:
        admission, _threads = _limits()
    except RuntimeError:
        return {}
    return {"slots": admission.total_tokens, "in_use": admission.borrowed_tokens, "waiting": admission.statistics().tasks_waiting}


async def run_db(fn, *args, **kwargs):
    admission, threads = _limits()
    loop = asyncio.get_running_loop()
    token = object()                                                  # the slot belongs to this unit, not to the awaiting task
    try:
        with anyio.fail_after(wait_seconds()):
            await admission.acquire_on_behalf_of(token)
    except TimeoutError as exc:
        raise DbWorkBusy(f"no database work slot free after {wait_seconds():.0f}s ({admission.total_tokens} slots)") from exc

    state = {"started": False, "abandoned": False}
    state_lock = threading.Lock()

    def release():
        try:
            loop.call_soon_threadsafe(admission.release_on_behalf_of, token)
        except RuntimeError:                                          # the loop is closed: nothing left to admit
            pass

    def unit():
        with state_lock:
            if state["abandoned"]:                                    # the caller was cancelled before this thread picked the work up: do not run it
                release()
                return None
            state["started"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            # released when the synchronous unit has really ended, also after the awaiting request was cancelled (anyio / asyncio return to a cancelled
            # caller while the thread keeps running: observed in the AH-01 unit test)
            release()

    try:
        return await anyio.to_thread.run_sync(unit, limiter=threads)
    except BaseException:
        with state_lock:
            if not state["started"]:
                state["abandoned"] = True
                admission.release_on_behalf_of(token)
        raise


# ---- whole-route offload (AH-01 siblings) ------------------------------------------------------------------------------------------------------------------
# A route written as a plain synchronous function (authority lookup, stores) decorated with `@db_route` is served as an async route that reads the request body
# on the event loop (the only part that needs it), then runs the whole function through `run_db` (same bounded, cancellation-safe admission). The function reads
# the body with `request_json` / `request_body` instead of `await request.json()`. A route with a size limit passes `max_bytes`: a declared length above it is
# not read (the function then sees `None`, as with the previous bounded reader).

def _find_request(args, kwargs):
    from starlette.requests import Request
    for v in list(args) + list(kwargs.values()):
        if isinstance(v, Request):
            return v
    return None


async def _read_body(request, max_bytes):
    declared = request.headers.get("content-length")
    if max_bytes is not None and declared is not None and declared.isdigit() and int(declared) > max_bytes:
        return None
    raw = await request.body()
    return None if max_bytes is not None and len(raw) > max_bytes else raw


def db_route(fn=None, *, max_bytes: int | None = None):
    def wrap(f):
        @functools.wraps(f)
        async def route(*args, **kwargs):
            request = _find_request(args, kwargs)
            if request is not None:
                request.state.ripple_body = await _read_body(request, max_bytes)
            return await run_db(f, *args, **kwargs)
        return route
    return wrap(fn) if fn is not None else wrap


def request_body(request) -> bytes | None:
    """The raw body read by `db_route` (None above the route's `max_bytes`)."""
    return getattr(request.state, "ripple_body", None)


def request_json(request):
    """`await request.json()` for a `db_route` function: same parse, same errors (ValueError on invalid or empty JSON)."""
    import json
    raw = request_body(request)
    if raw is None:
        raise ValueError("no request body")
    return json.loads(raw)
