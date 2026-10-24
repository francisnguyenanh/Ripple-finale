"""Ripple's single public service: browser UI + cookie-gated access to /rpc.

Ripple deploys as ONE Cloud Run service. This app is its entry point
(Dockerfile CMD). It serves the UI and runs the backend's /rpc handler
(`app.main.handle_rpc`) in-process. It never mounts the backend's raw /rpc
route, so every command passes the cookie, origin, session-binding and quota
checks below. Do not reintroduce a second backend service or an IAM hop.

RIPPLE_JUDGE_ACCESS_CODE and RIPPLE_JUDGE_COOKIE_KEY must be provisioned secrets.
"""
import hashlib
import hmac
import json
import os
import pathlib
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

# Imported at startup so a misconfigured backend fails the revision, not the
# first login.
from app import main as _backend

from fastapi import Depends
from infra.v2_sandbox import SandboxRefused, router as _v2_sandbox_router, sandbox_guard, sandbox_refused_handler

# J5: the sandbox write guard is a no-op unless RIPPLE_SANDBOX_CONFIG names a sandbox workspace.
app = FastAPI(title="Ripple", docs_url=None, redoc_url=None, openapi_url=None, dependencies=[Depends(sandbox_guard)])
app.add_exception_handler(SandboxRefused, sandbox_refused_handler)

from infra.db_pool import PoolBusy as _PoolBusy                                     # noqa: E402
import logging as _logging                                                         # noqa: E402

_capacity_log = _logging.getLogger("ripple.capacity")


async def _busy_handler(request: Request, exc: Exception):
    """AH-01: no database connection / work slot became free in time -> a bounded, explained 503 (no DSN, no internal message); the HTTP guard middleware still adds
    the request ID and security headers. Other database errors are NOT mapped here (they are not overload). One sanitized log line carries the counters only."""
    from infra import db_offload, db_pool
    _capacity_log.warning("service_busy kind=%s pool=%s work=%s", "work" if isinstance(exc, db_offload.DbWorkBusy) else "pool", db_pool.busy_counts(), db_offload.stats())
    return JSONResponse({"error": "The service is busy. Please try again in a moment.", "code": "service_busy"}, status_code=503,
                        headers={"Retry-After": "2", "Cache-Control": "no-store"})

app.add_exception_handler(_PoolBusy, _busy_handler)


@app.middleware("http")
async def _wake_worker_after_a_write(request: Request, call_next):
    """A successful write may have queued a job: start the batch worker on demand (no always-on poller). Background, best effort, never part of the answer (infra/worker/wake.py)."""
    response = await call_next(request)
    if request.method in ("POST", "PUT", "PATCH") and response.status_code < 400:
        from infra.worker import wake
        if wake.enabled():
            # Inside the request, not after it: Cloud Run throttles the CPU once the response is sent, so a background thread would not run until the next request. Bounded (5 s) and never fails the answer.
            import anyio
            from infra.v2_policy_routes import _v2_store
            store = _v2_store()
            if store is not None:
                try:
                    with anyio.fail_after(5):
                        await anyio.to_thread.run_sync(wake.kick, store)
                except BaseException as exc:        # noqa: BLE001 - the user's answer must not depend on the signal
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
    return response
from infra import v2_http_guard as _http_guard
_http_guard.install(app)          # nosniff on every response; request-rate limit on authenticated /v2 routes
_UI = pathlib.Path(__file__).resolve().parents[1] / "app" / "ui"

# The v2 auth namespace is separate from the legacy single-judge journey.
from infra.v2_auth import LoginThrottle, _source as _client_source, router as _v2_auth_router
app.include_router(_v2_auth_router)

# Centralized permission-enforced job/knowledge/membership/audit
# routes, built on the v2 identity authority above.
from infra.v2_policy_routes import router as _v2_policy_router
app.include_router(_v2_policy_router)

# Change read model, human result decision, manual-work proof.
from infra.v2_change_routes import router as _v2_change_router
app.include_router(_v2_change_router)
from infra.v2_work_routes import router as _v2_work_router
app.include_router(_v2_work_router)
from infra.v2_request_routes import router as _v2_request_router
app.include_router(_v2_request_router)
from infra.v2_request_assistant_routes import router as _v2_request_assistant_router
app.include_router(_v2_request_assistant_router)
from infra.v2_preparation_routes import router as _v2_preparation_router                    # EUI-08 guided preparation (RIPPLE_GUIDED_PREPARATION=on; off by default)
app.include_router(_v2_preparation_router)
from infra.v2_coverage_routes import router as _v2_coverage_router
app.include_router(_v2_coverage_router)
from infra.v2_operator_routes import router as _v2_operator_router
app.include_router(_v2_operator_router)

# History browser, restore preview, RESTORE approval.
from infra.v2_restore_routes import router as _v2_restore_router
app.include_router(_v2_restore_router)

# Operator-supplied verification contracts (immutable acceptance values).
from infra.v2_verification_routes import router as _v2_verification_router
app.include_router(_v2_verification_router)

# Authenticated upload/staging, manifest preview and import commit.
from infra.v2_source_routes import router as _v2_source_router
app.include_router(_v2_source_router)

# Additive read-only surfaces the /app shell needs (knowledge overview, proposal diff,
# approvals of a proposal); see infra/v2_ui_routes.py.
from infra.v2_ma_routes import router as _v2_ma_router
app.include_router(_v2_ma_router)
from infra.v2_interview_routes import router as _v2_interview_router
app.include_router(_v2_interview_router)
from infra.v2_document_variant_routes import router as _v2_document_variant_router
app.include_router(_v2_document_variant_router)
from infra.v2_mining_routes import router as _v2_mining_router
app.include_router(_v2_mining_router)
from infra.v2_firebase_routes import router as _v2_firebase_router
app.include_router(_v2_firebase_router)
from infra.v2_satellite_routes import router as _v2_satellite_router
app.include_router(_v2_satellite_router)
from infra.v2_ui_routes import router as _v2_ui_router
app.include_router(_v2_ui_router)
app.include_router(_v2_sandbox_router)
from infra.v2_gap_routes import router as _v2_gap_router
# Extension contract ext/1 (local only): candidate code patch, validation, exact approval, refused publish, mock promotion.
from infra.v2_code_change_routes import router as _v2_code_change_router
app.include_router(_v2_code_change_router)
# Governance (local build): typed policy decision over the accepted knowledge, scoped audit package.
from infra.v2_governance_routes import router as _v2_governance_router
app.include_router(_v2_governance_router)
app.include_router(_v2_gap_router)
from infra.v2_business_routes import router as _v2_business_router
app.include_router(_v2_business_router)
from infra.v2_document_routes import router as _v2_document_router  # noqa: E402
app.include_router(_v2_document_router)
from infra.v2_chat_routes import router as _v2_chat_router
app.include_router(_v2_chat_router)
from infra.v2_harbor_try import router as _v2_harbor_router     # thin Harbor page: pinned Harbor code, two configurations, no provider
app.include_router(_v2_harbor_router)


def _secret(name):
    # Secret Manager keeps whatever bytes were uploaded; a value created from a
    # Windows shell ends in CRLF, which no typed invitation code can match.
    return os.environ.get(name, "").strip()


def _sign(value):
    key = _secret("RIPPLE_JUDGE_COOKIE_KEY")
    if len(key) < 32:
        raise RuntimeError("configure a cookie signing key of at least 32 characters")
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def _identity(request):
    try:
        raw = request.cookies.get("ripple_judge", "")
        sid, actor, expires, signature = raw.split("|")
        value = f"{sid}|{actor}|{expires}"
        if not hmac.compare_digest(_sign(value), signature) or int(expires) <= time.time():
            return None
        return sid, actor
    except (ValueError, RuntimeError):
        return None


def _same_origin(request):
    expected = os.environ.get("RIPPLE_JUDGE_ORIGIN", str(request.base_url).rstrip("/"))
    return request.headers.get("origin") == expected and request.headers.get("x-ripple-judge") == "1"


def _forward(body):
    response = _backend.handle_rpc(body)
    return response.status_code, json.loads(response.body)


@app.get("/health")
async def health():                                   # AH-01: liveness needs no thread and no database, so it answers while DB work is saturated
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    """Readiness (AH-01): the in-process backend AND the Final database answer a trivial query, run off the event loop with the same bounded admission as every
    database unit. An unreachable database is an honest 503 `unavailable` (no DSN, no driver text); a saturated pool is the usual bounded 503 `service_busy`;
    without RIPPLE_V2_DSN the database is reported `not_configured`. Liveness stays `/health` (no thread, no database)."""
    from infra.db_offload import run_db
    from infra.v2_policy_routes import _v2_store

    def check():
        _backend.ready()
        store = _v2_store()
        if store is None:
            return "not_configured"
        with store._tx() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return "ok"

    try:
        database = await run_db(check)
    except _PoolBusy:
        raise                                                                        # -> the bounded 503 service_busy handler
    except Exception:                                                                # noqa: BLE001 - any failure to reach the database is "not ready"
        _capacity_log.warning("ready database=unavailable")
        return JSONResponse({"status": "unavailable", "database": "unavailable"}, status_code=503, headers={"Cache-Control": "no-store"})
    return JSONResponse({"status": "ok", "database": database}, headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_UI.joinpath("judge.html").read_text(encoding="utf-8"), headers={
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/favicon.ico")
def favicon():
    # Browsers ask for it on their own; a 404 shows as a console error to
    # anyone who opens DevTools. The page declares no icon.
    return Response(status_code=204)


@app.get("/judge.js")
def javascript():
    return Response(_UI.joinpath("judge.js").read_text(encoding="utf-8"), media_type="text/javascript",
                    headers={"X-Content-Type-Options": "nosniff"})


@app.get("/judge.css")
def stylesheet():
    return Response(_UI.joinpath("judge.css").read_text(encoding="utf-8"), media_type="text/css",
                    headers={"X-Content-Type-Options": "nosniff"})


@app.get("/sources", response_class=HTMLResponse)
def sources_page():
    """AI3-T04 T04-RV1: the served source workspace inspection surface --
    load a committed revision, see its coverage stage (parse/index/semantic),
    click an observation's evidence, and export an artifact's real bytes.
    Same CSP as the main journey page; all data on it comes from the
    already-ACL-gated v2 source routes, never fabricated client-side."""
    return HTMLResponse(_UI.joinpath("sources.html").read_text(encoding="utf-8"), headers={
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/sources.js")
def sources_javascript():
    return Response(_UI.joinpath("sources.js").read_text(encoding="utf-8"), media_type="text/javascript",
                    headers={"X-Content-Type-Options": "nosniff"})


# Legacy access-code login is throttled per source address only (there is no account id; a global
# counter would let one caller lock everyone out). Per-process state: with N instances the limit is about x N.
_LEGACY_LOGIN_THROTTLE = LoginThrottle()
_LEGACY_LOGIN_THROTTLE.account_max = 0
_LEGACY_LOGIN_THROTTLE.source_max = 20


@app.post("/login")
async def login(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "origin refused"}, status_code=403)
    source = _client_source(request)
    wait = _LEGACY_LOGIN_THROTTLE.retry_after(None, source)
    if wait:
        return JSONResponse({"error": "too many attempts"}, status_code=429,
                            headers={"Retry-After": str(wait), "Cache-Control": "no-store"})
    body = await request.json()
    code = _secret("RIPPLE_JUDGE_ACCESS_CODE")
    if len(code) < 24:
        return JSONResponse({"error": "judge access is not configured"}, status_code=503)
    if not isinstance(body.get("code"), str) or not hmac.compare_digest(body["code"], code):
        _LEGACY_LOGIN_THROTTLE.failure(None, source)
        return JSONResponse({"error": "invalid invitation code"}, status_code=401)
    actor = "browser-judge:" + uuid.uuid4().hex
    try:
        # Validate configuration before creating a backend session.
        _sign("configuration-check")
        status, result = _forward({"operation": "session.create", "actor": actor,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "request_id": uuid.uuid4().hex})
        if not result.get("ok"):
            return JSONResponse({"error": "could not start a session"}, status_code=status)
        expires = int(time.time()) + 3600
        value = f"{result['data']['session_id']}|{actor}|{expires}"
        response = JSONResponse({"ok": True})
        response.set_cookie("ripple_judge", value + "|" + _sign(value), max_age=3600,
                            httponly=True, secure=True, samesite="strict", path="/")
        return response
    except Exception:
        return JSONResponse({"error": "judge service unavailable"}, status_code=503)


@app.post("/rpc")
async def rpc(request: Request):
    identity = _identity(request)
    if not identity:
        return JSONResponse({"ok": False, "error": "sign in to start or resume a session"}, status_code=401)
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "origin refused"}, status_code=403)
    body = await request.json()
    from core.change_intelligence.public_surface import names
    if body.get("operation") not in names() or body["operation"] == "session.create":
        return JSONResponse({"ok": False, "error": "operation refused"}, status_code=403)
    # Ignore any client-supplied actor, session, timestamp or cloud credential.
    forwarded = {"operation": body["operation"], "payload": body.get("payload", {}),
        "session_id": identity[0], "actor": identity[1],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_id": body.get("request_id") or uuid.uuid4().hex}
    try:
        status, result = _forward(forwarded)
        return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})
    except Exception:
        return JSONResponse({"ok": False, "error": "backend unavailable; try again"}, status_code=503)


# The v2 browser shell. Static assets only; every datum is fetched from the ACL-gated
# v2 routes after sign-in. CSP forbids inline script/style and every foreign origin.
_APP_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
_APP_ASSETS = {"app.css": "text/css", "app_core.js": "text/javascript",
               "app_sources.js": "text/javascript", "app_knowledge.js": "text/javascript",
               "app_change.js": "text/javascript", "app_explorer.js": "text/javascript",
               "app_sandbox.js": "text/javascript",
               "app_support.js": "text/javascript",
               "app_present.js": "text/javascript"}


@app.get("/app", response_class=HTMLResponse)
def v2_app_shell(request: Request):
    html = _UI.joinpath("app.html").read_text(encoding="utf-8")
    if not request.cookies.get("ripple_v2"):
        # No session cookie at all: draw the sign-in form in the first paint. With a cookie the
        # form stays hidden until /v2/auth/me answers (it re-appears if that session is dead).
        html = html.replace('aria-labelledby="login-h" hidden>', 'aria-labelledby="login-h">', 1)
    notice = os.environ.get("RIPPLE_UI_NOTICE", "").strip()
    if notice:
        # Operator-set banner (e.g. "SIMULATED providers"); text only, escaped, no markup from the environment.
        import html as _html
        details = os.environ.get("RIPPLE_UI_NOTICE_DETAILS", "").strip()
        inner = ("<details><summary>" + _html.escape(notice[:300]) + "</summary><p data-testid=\"ui-notice-details\">"
                 + _html.escape(details[:1500]) + "</p></details>") if details else _html.escape(notice[:300])
        html = html.replace('<div id="notice" class="notice" role="note" hidden></div>',
                            '<div id="notice" class="notice" role="note" data-testid="ui-notice">' + inner + '</div>', 1)
    return HTMLResponse(html, headers={
        "Content-Security-Policy": _APP_CSP, "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


@app.get("/app-assets/{name}")
def v2_app_asset(name: str):
    media = _APP_ASSETS.get(name)
    if media is None:
        return Response(status_code=404)
    return Response(_UI.joinpath(name).read_text(encoding="utf-8"), media_type=media,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


# Slice A (product-uiux): the React build, served same-origin under the same strict CSP. Built from
# frontend/ into app/ui_next/ (hashed assets, self-hosted fonts, no inline script/style).
# RIPPLE_UI_NEXT_DIR points the SAME routes at another build directory (test builds); unset = the committed app/ui_next.
_NEXT = pathlib.Path(os.environ.get("RIPPLE_UI_NEXT_DIR") or (pathlib.Path(__file__).resolve().parents[1] / "app" / "ui_next"))
_NEXT_MEDIA = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"}


@app.get("/app-next", include_in_schema=False)
def v2_app_next_redirect():
    return Response(status_code=307, headers={"Location": "/app-next/"})


@app.get("/app-next/", response_class=HTMLResponse)
def v2_app_next_shell(request: Request):
    index = _NEXT / "index.html"
    if not index.is_file():
        return HTMLResponse("The new interface has not been built.", status_code=404)
    html = index.read_text(encoding="utf-8")
    notice = os.environ.get("RIPPLE_UI_NOTICE", "").strip()
    if notice:
        import html as _html
        # Operator-set banner text only; escaped, never markup.
        html = html.replace("<head>", '<head><meta name="ripple-notice" content="' + _html.escape(notice[:300], quote=True) + '">', 1)
    from infra import provider_gate
    if provider_gate.is_disarmed():
        # The operator declared the provider off: the page greys out AI actions and says why (the server refuses them too).
        html = html.replace("<head>", '<head><meta name="ripple-provider" content="LIVE_DISARMED">', 1)
    kinds = provider_gate.enabled_kinds_for_page()
    if kinds is not None:
        # The role allowlist: the page shows a kind that is not listed as not enabled (the server refuses it too). Names are fixed job kinds, never user text.
        html = html.replace("<head>", '<head><meta name="ripple-ai-kinds" content="' + kinds + '">', 1)
    return HTMLResponse(html, headers={
        "Content-Security-Policy": _APP_CSP, "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


@app.get("/app-next/assets/{name}", include_in_schema=False)
def v2_app_next_asset(name: str):
    media = _NEXT_MEDIA.get(pathlib.PurePath(name).suffix)
    path = _NEXT / "assets" / name
    if media is None or not re.fullmatch(r"[A-Za-z0-9._-]+", name) or not path.is_file():
        return Response(status_code=404)
    return Response(path.read_bytes(), media_type=media, headers={
        "Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})
from infra.v2_explain import router as _v2_explain_router
from infra.v2_wake_routes import router as _v2_wake_router
app.include_router(_v2_wake_router)
app.include_router(_v2_explain_router)
from infra.v2_sim_analysis import router as _v2_sim_analysis_router
app.include_router(_v2_sim_analysis_router)
from infra.v2_source_lifecycle import router as _v2_lifecycle_router
app.include_router(_v2_lifecycle_router)
from infra.v2_workspace_routes import router as _v2_workspace_router
app.include_router(_v2_workspace_router)
