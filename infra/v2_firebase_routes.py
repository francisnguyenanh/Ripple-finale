"""External identity routes (extension FB-A1, local build):

POST /v2/auth/firebase/exchange   {id_token}                                            verified external token -> ordinary Ripple session cookie (same cookie, flags and lifetime rules as /v2/auth/login)
POST /v2/auth/external/links      {workspace_id, issuer, project_id, subject, identity_tenant?, account_id}   link a subject to an account (invitation/admin mapping)   membership.manage
POST /v2/auth/external/links/revoke {workspace_id, issuer, project_id, subject, identity_tenant?}            revoke a link                                              membership.manage
GET  /v2/auth/external/links      ?workspace_id=                                        links of this workspace (subjects hashed)                                       membership.manage
GET  /v2/auth/firebase/config                                                            public, credential-free client config (mode, project id) for the sign-in page
Mode `off` answers 503 on the exchange; `local-mock` accepts mock tokens only; `admin-sdk` needs the Admin SDK installed and configured. A failed exchange is a generic 401.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters import v2_external_identity as ext
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require
from infra.v2_auth import _COOKIE
from infra.v2_policy_routes import _authority, _error, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/v2/auth/firebase/config")
def firebase_config():
    cfg = ext.settings()
    return JSONResponse({"schema": ext.SCHEMA, "mode": cfg["mode"], "project_id": cfg["project_id"] if cfg["mode"] != "off" else None, "identity_tenant": cfg["identity_tenant"] or None,
                         "exchange_path": "/v2/auth/firebase/exchange", "statement": "Sign-in happens with the external provider; the exchange issues the ordinary Ripple session; roles come from the workspace membership, never from the token."},
                        headers=_NO_STORE)


@router.post("/v2/auth/firebase/exchange")
async def firebase_exchange(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 auth unavailable", 503)
    cfg = ext.settings()
    try:
        verifier = ext.verifier_for(cfg)
    except ext.VerifierUnavailable as exc:
        return JSONResponse({"error": "external sign-in unavailable", "code": str(exc)}, status_code=503, headers=_NO_STORE)
    body = await _json_body(request)
    try:
        out = ext.exchange(store, identity, body.get("id_token"), cfg=cfg, verifier=verifier)
    except ext.ExchangeDenied:
        return _error("sign-in refused", 401)                                       # one generic answer for every reason; the reason is in the audit, never in the response
    except ext.VerifierUnavailable as exc:
        return JSONResponse({"error": "external sign-in unavailable", "code": str(exc)}, status_code=503, headers=_NO_STORE)
    response = JSONResponse({"ok": True, "account_id": out["account_id"]}, headers=_NO_STORE)
    response.set_cookie(_COOKIE, out["token"], max_age=cfg["session_lifetime_seconds"], httponly=True, secure=True, samesite="strict", path="/")
    return response


def _links_shell(request, workspace_id, action, *, mutating=True):
    if mutating and not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "membership.manage")
        return JSONResponse(action(store, authority), headers=_NO_STORE)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc), "code": "link_refused"}, status_code=409, headers=_NO_STORE)


@router.post("/v2/auth/external/links")
async def link_external(request: Request):
    body = await _json_body(request)
    return _links_shell(request, body.get("workspace_id"), lambda store, authority: ext.link(store, authority["tenant_id"], body["workspace_id"], issuer=body.get("issuer"), project_id=body.get("project_id"),
                                                                                           subject=body.get("subject"), identity_tenant=body.get("identity_tenant") or "", account_id=body.get("account_id"), actor_id=authority["account_id"]))


@router.post("/v2/auth/external/links/revoke")
async def revoke_external(request: Request):
    body = await _json_body(request)
    return _links_shell(request, body.get("workspace_id"), lambda store, authority: ext.revoke_link(store, authority["tenant_id"], body["workspace_id"], issuer=body.get("issuer") or "", project_id=body.get("project_id") or "",
                                                                                                  subject=body.get("subject") or "", identity_tenant=body.get("identity_tenant") or "", actor_id=authority["account_id"]))


@router.get("/v2/auth/external/links")
def list_external(request: Request, workspace_id: str):
    return _links_shell(request, workspace_id, lambda store, authority: {"links": ext.list_links(store, authority["tenant_id"], workspace_id)}, mutating=False)
