"""Public v2 authentication and scoped workspace reads.

This small entrypoint exposes no account creation or membership mutation.
Those remain private operator calls; policy enforcement lives in `infra/v2_policy_routes.py`.
"""

from __future__ import annotations

from infra.db_offload import db_route, request_json
import collections
import os
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters.v2_identity import IdentityDenied, PostgresIdentityStore
from infra.adapters.v2_policy import permissions_for


def session_lifetime_seconds() -> int:
    """How long a NEW sign-in lasts. Default 1 hour. The operator of a server may set RIPPLE_SESSION_LIFETIME_SECONDS (600..14400, i.e. at most 4 hours) for the sign-ins made after
    the setting; an invalid value falls back to the default. It never changes a session that already exists (that has a fixed expiry in the store), and it is not a way around
    authentication: every session still needs a real sign-in."""
    import os
    raw = os.environ.get("RIPPLE_SESSION_LIFETIME_SECONDS", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 3600
    return value if 600 <= value <= 14400 else 3600


router = APIRouter()
_COOKIE = "ripple_v2"


def _store():
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    if not dsn:
        return None
    return PostgresIdentityStore(dsn)


def _origin_ok(request: Request) -> bool:
    expected = os.environ.get("RIPPLE_JUDGE_ORIGIN", str(request.base_url).rstrip("/"))
    return (request.headers.get("origin") == expected
            and request.headers.get("x-ripple-judge") == "1")


def _error(message: str, status: int):
    return JSONResponse({"error": message}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _token(request: Request) -> str:
    return request.cookies.get(_COOKIE, "")


class LoginThrottle:
    """Bounded, in-process brute-force throttle for POST /v2/auth/login (the first, approximate line; the exact shared state lives in
    `v2_login_failures` through `infra/login_protection.py` when RIPPLE_RATE_SHARED=1).

    Three counters of FAILED attempts inside a sliding window (SEC-02, 2026-10-30): the (account, source) pair, the source address and the
    account from every source (any account string, whether or not it exists, so the response never reveals existence); each with its own limit
    and lock (`infra/login_protection.keys`). At the limit the key is locked for its lockout; while locked every attempt (including a correct
    credential) gets the same generic 429 + Retry-After, never reaches the credential check and changes no counter; after the lock the key counts
    again from zero. A successful login clears that account's own counters; a lock expires by itself. Memory is bounded (`max_keys`, expired
    entries pruned, oldest evicted). State is per process: with several instances each keeps its own counters (the shared table is exact).
    Config (env): RIPPLE_LOGIN_WINDOW_SECONDS (300), RIPPLE_LOGIN_MAX_TRACKED (10000) and the per-layer limits read by `login_protection`."""

    def __init__(self, clock=time.monotonic):
        def _i(name, default):
            try:
                return max(0, int(os.environ.get(name, default)))
            except ValueError:
                return default
        self.account_max = _i("RIPPLE_LOGIN_MAX_FAILURES", 5)
        self.source_max = _i("RIPPLE_LOGIN_SOURCE_MAX_FAILURES", 100)
        self.window = max(1, _i("RIPPLE_LOGIN_WINDOW_SECONDS", 300))
        self.lockout = max(1, _i("RIPPLE_LOGIN_LOCKOUT_SECONDS", 300))
        self.max_keys = max(10, _i("RIPPLE_LOGIN_MAX_TRACKED", 10000))
        self._clock = clock
        self._lock = threading.Lock()
        self._state = collections.OrderedDict()      # key -> [failure timestamps, locked_until]

    @staticmethod
    def _keys(account, source):
        """(key, limit, lockout) of the three layers (SEC-02): the pair, the source, the account from every source; see infra/login_protection.py."""
        from infra import login_protection
        return login_protection.keys(account, source)

    def retry_after(self, account, source) -> int:
        """Seconds until the next attempt is accepted, 0 when not locked. A locked key changes nothing (checked before the credential)."""
        now = self._clock()
        with self._lock:
            wait = 0.0
            for key, _limit, _lockout in self._keys(account, source):
                entry = self._state.get(key)
                if entry and entry[1] > now:
                    wait = max(wait, entry[1] - now)
            return int(wait) + 1 if wait > 0 else 0

    def failure(self, account, source) -> None:
        now = self._clock()
        with self._lock:
            for key, limit, lockout in self._keys(account, source):
                entry = self._state.pop(key, None) or [[], 0.0]
                entry[0] = [t for t in entry[0] if now - t < self.window] + [now]
                if len(entry[0]) >= limit:
                    entry[1] = now + lockout                        # LOCKED; the counter restarts from 0 after the lock (re-arm needs `limit` new failures)
                    entry[0] = []
                self._state[key] = entry
            while len(self._state) > self.max_keys:
                self._state.popitem(last=False)

    def success(self, account) -> None:
        """Clears the account's own counters (`a:` and every `as:` of it) that are not locked; a lock expires by itself."""
        now = self._clock()
        acct = account[:200] if isinstance(account, str) else ""
        with self._lock:
            for key, entry in self._state.items():
                if (key == "a:" + acct or key.startswith("as:" + acct + "|")) and entry[1] <= now:
                    entry[0] = []

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


_THROTTLE = LoginThrottle()


def _source(request: Request) -> str:
    """The caller's address under the ingress contract (RIPPLE_TRUSTED_PROXY_HOPS; `infra/login_protection.source_address`)."""
    from infra import login_protection
    return login_protection.source_address(request)


@router.post("/v2/auth/login")
@db_route
def login(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    store = _store()
    if store is None:
        return _error("v2 auth unavailable", 503)
    source = _source(request)
    try:
        body = request_json(request)
    except ValueError:
        body = None
    account = body.get("account_id") if isinstance(body, dict) else None
    wait = _THROTTLE.retry_after(account, source)
    if not wait:
        from infra import v2_http_guard as _G
        if _G.login_enabled():
            try:
                wait = _G.login_wait(account, source)        # shared by every instance
            except Exception:                                # noqa: BLE001 - a counter fault must not open the door: fail CLOSED with the same generic answer
                wait = 15
    if wait:
        return JSONResponse({"error": "too many attempts"}, status_code=429,
                            headers={"Cache-Control": "no-store", "Retry-After": str(wait)})
    try:
        if not isinstance(body, dict):
            raise IdentityDenied("invalid credentials")
        token = store.login(account, body.get("credential"), lifetime_seconds=session_lifetime_seconds())
    except (ValueError, IdentityDenied):
        _THROTTLE.failure(account, source)
        from infra import v2_http_guard as _G
        ceiling = False
        if _G.login_enabled():
            try:
                ceiling = _G.login_failure(account, source)
            except Exception:                                # noqa: BLE001 - the in-process counter above still counted it
                pass
        if ceiling:                                          # SEC-02: the shared table could not count this key (row ceiling): nothing is admitted
            return JSONResponse({"error": "too many attempts"}, status_code=429,
                                headers={"Cache-Control": "no-store", "Retry-After": "60"})
        return _error("invalid credentials", 401)
    _THROTTLE.success(account)
    from infra import v2_http_guard as _G
    if _G.login_enabled():
        try:
            _G.login_clear(account)
        except Exception:                                    # noqa: BLE001
            pass
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.set_cookie(_COOKIE, token, max_age=session_lifetime_seconds(), httponly=True,
                        secure=True, samesite="strict", path="/")
    return response


@router.post("/v2/auth/logout")
def logout(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    store = _store()
    if store is None:
        return _error("v2 auth unavailable", 503)
    token = _token(request)
    if not token:
        return _error("sign in required", 401)
    store.logout(token)
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(_COOKIE, path="/", secure=True, samesite="strict")
    return response


@router.get("/v2/auth/me")
def me(request: Request):
    store = _store()
    if store is None:
        return _error("v2 auth unavailable", 503)
    try:
        context = store.resolve_context(_token(request))
        return JSONResponse(context, headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)


@router.get("/v2/workspaces")
def workspaces(request: Request):
    store = _store()
    if store is None:
        return _error("v2 auth unavailable", 503)
    try:
        listing = store.list_workspaces(_token(request))
        # The UI capability map comes from the server's own role bundles (additive field;
        # the server still enforces every permission on every call).
        for item in listing:
            item["permissions"] = sorted(permissions_for(item.get("roles")))
        return JSONResponse({"workspaces": listing}, headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)


@router.post("/v2/workspaces/select")
@db_route
def select_workspace(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    store = _store()
    if store is None:
        return _error("v2 auth unavailable", 503)
    try:
        body = request_json(request)
        if not isinstance(body, dict) or not isinstance(body.get("workspace_id"), str):
            return _error("workspace_id required", 400)
        return JSONResponse(store.select_workspace(_token(request), body["workspace_id"]),
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("workspace unavailable", 404)


@router.get("/v2/workspaces/{workspace_id}/head")
def workspace_head(request: Request, workspace_id: str):
    store = _store()
    if store is None:
        return _error("v2 auth unavailable", 503)
    try:
        return JSONResponse(store.read_head(_token(request), workspace_id),
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("workspace unavailable", 404)
