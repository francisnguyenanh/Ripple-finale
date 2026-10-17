"""Runtime operator switches (`infra/operator_flags.py`). GET: any signed-in member of the workspace sees the state (so a banner can say why AI or processing is paused).
POST: only an administrator (`membership.manage`), a human actor, same Origin rule as every v2 mutation, a reason of at least 10 characters; audited. No redeploy, no terminal."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra import operator_flags
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, permissions_for, require, require_human_actor
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _denied(exc: IdentityDenied):
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


@router.get("/v2/operator/flags")
def flags(request: Request, workspace_id: str):
    identity = _identity_store()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        out = {**operator_flags.status(), "can_change": "membership.manage" in permissions_for(authority.get("roles"))}
        if operator_flags.enabled():
            try:
                out["history"] = operator_flags.history(5)
            except Exception:                      # noqa: BLE001
                out["history"] = []
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)


@router.post("/v2/operator/flags")
async def set_flag(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity = _identity_store()
    if identity is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, key, value, reason = body.get("workspace_id"), body.get("key"), body.get("value"), body.get("reason")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)          # sign-in and rights BEFORE the rest of the body is looked at (an anonymous caller learns nothing of the schema)
        require_human_actor(authority)
        require(authority, "membership.manage")
        if not all(isinstance(x, str) and x for x in (key, value)) or not isinstance(reason, str):
            return _error("workspace_id, key, value and reason required", 400)
        if not operator_flags.enabled():
            return _error("the runtime switches are not active on this deployment", 409)
        out = operator_flags.set_flag(key, value, authority["account_id"], reason, authority["tenant_id"], workspace_id)
        return JSONResponse(out, headers=_NO_STORE)
    except ValueError as exc:
        return _error(str(exc), 400)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
