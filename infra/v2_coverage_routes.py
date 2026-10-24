"""Read and ensure the coverage behind a change (relation + conflict check of the CURRENT revision). See `infra/v2_coverage.py`.
GET reads (project.read). POST creates the missing deterministic jobs (source.import, human actor, same Origin rule as every v2 mutation); it calls no model."""
from __future__ import annotations

from infra.db_offload import db_route
from infra.v2_policy_routes import _json_body_sync
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra import v2_coverage
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require, require_human_actor
from infra.adapters.v2_postgres import StoreConflict, TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _denied(exc: IdentityDenied):
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


@router.get("/v2/change/coverage")
def coverage(request: Request, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        out = v2_coverage.status(store, authority["tenant_id"], workspace_id)
        out.pop("candidate_ids", None)
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/change/coverage/ensure")
@db_route
def ensure_coverage(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "change.prepare")
        from infra import operator_flags as _flags
        if _flags.window_creation_closed():
            return _error("This evaluation window has ended (or is about to): no new work is accepted.", 409)        # deterministic over already-stored extraction output: no AI, no source read; a judge may need it before the first draft
        out = v2_coverage.ensure(store, authority["tenant_id"], workspace_id)
        out.pop("candidate_ids", None)
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)
