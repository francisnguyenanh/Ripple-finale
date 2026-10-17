"""Routes for the history browser, restore preview and RESTORE approval.

Authority follows the module invariant of `infra/v2_policy_routes.py`: the
caller's tenant / roles / actor kind come ONLY from the server-side session
(`_authority`, current membership, re-resolved on every call); nothing in a
body or query (`actor_id`, `role`, `tenant_id`, ...) is trusted. History and
diff need `project.read`; preview needs `source.restore` (read-only, no human
requirement); approve needs a HUMAN actor holding the RESTORE approval
permission (`APPROVAL_SUBJECT_PERMISSION`). The restore itself is applied by
the existing `apply` job (`POST /v2/jobs`), never from here.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters import v2_restore
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import (
    APPROVAL_SUBJECT_PERMISSION, PermissionDenied, require, require_human_actor,
)
from infra.adapters.v2_postgres import (
    PostgresV2Store, StaleVersion, StoreConflict, TenantScopeError,
)
from infra import v2_source_routes
from infra.v2_policy_routes import (
    _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok,
    _v2_store,
)

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _refused(exc: v2_restore.RestoreRefused):
    return JSONResponse({"error": exc.reasons[0], "reasons": exc.reasons},
                        status_code=exc.status, headers=_NO_STORE)


def _services():
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return None, None
    return identity, store


def _blob_store():
    """Preview and approval must prove that EVERY artifact of the target is
    restorable, which needs the real blob port (the same `_blob_port` seam the
    source routes use). No port => fail closed (503), never an unverified
    preview."""
    blobs = v2_source_routes._blob_port()
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    if blobs is None or not dsn:
        return None
    return PostgresV2Store(dsn, blobs)


def _nonblank(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


@router.get("/v2/history/{workspace_id}")
def history(request: Request, workspace_id: str):
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    raw = request.query_params.get("limit")
    limit = None
    if raw is not None:
        if not raw.strip().isdigit() or int(raw) < 1:
            return _error("limit must be an integer >= 1", 400)
        limit = int(raw)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        return JSONResponse(v2_restore.history_view(store, authority["tenant_id"], workspace_id,
                                                    limit=limit), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _error("sign in required" if _identity_denied_status(exc) == 401
                      else "permission denied", _identity_denied_status(exc))
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("not found", 404)
    except v2_restore.RestoreConfigError as exc:
        return _error(f"restore_config_invalid: {exc}", 500)


@router.get("/v2/history/{workspace_id}/diff")
def history_diff(request: Request, workspace_id: str):
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    frm = request.query_params.get("from_revision_id")
    to = request.query_params.get("to_revision_id")
    if not _nonblank(frm) or not _nonblank(to):
        return _error("from_revision_id and to_revision_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        return JSONResponse(v2_restore.diff_view(store, authority["tenant_id"], workspace_id,
                                                 frm, to), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _error("sign in required" if _identity_denied_status(exc) == 401
                      else "permission denied", _identity_denied_status(exc))
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("not found", 404)
    except v2_restore.RestoreConfigError as exc:
        return _error(f"restore_config_invalid: {exc}", 500)


@router.post("/v2/restore/preview")
async def restore_preview(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, target = body.get("workspace_id"), body.get("target_revision_id")
    if not _nonblank(workspace_id):
        return _error("workspace_id required", 400)
    if not _nonblank(target):
        return _error("target_revision_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "source.restore")
        store = _blob_store()
        if store is None:
            return _error("restore_storage_unavailable", 503)
        return JSONResponse(v2_restore.preview(store, authority["tenant_id"], workspace_id,
                                               target), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _error("sign in required" if _identity_denied_status(exc) == 401
                      else "permission denied", _identity_denied_status(exc))
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("not found", 404)
    except v2_restore.RestoreRefused as exc:
        return _refused(exc)
    except v2_restore.RestoreConfigError as exc:
        return _error(f"restore_config_invalid: {exc}", 500)


@router.post("/v2/restore/approve")
async def restore_approve(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, target = body.get("workspace_id"), body.get("target_revision_id")
    digest, reason = body.get("expected_preview_digest"), body.get("reason")
    key = body.get("idempotency_key")
    if not _nonblank(workspace_id):
        return _error("workspace_id required", 400)
    if not _nonblank(target):
        return _error("target_revision_id required", 400)
    if not _nonblank(digest):
        return _error("expected_preview_digest required", 400)
    if not _nonblank(reason):
        return _error("a non-empty reason is required", 400)
    if key is not None and not isinstance(key, str):
        return _error("idempotency_key must be a string", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, APPROVAL_SUBJECT_PERMISSION["RESTORE"])
        store = _blob_store()
        if store is None:
            return _error("restore_storage_unavailable", 503)
        result = v2_restore.approve_restore(
            store, authority["tenant_id"], workspace_id, actor_id=authority["account_id"],
            target_revision_id=target, expected_preview_digest=digest, reason=reason,
            idempotency_key=key)
        return JSONResponse(result, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _error("sign in required" if _identity_denied_status(exc) == 401
                      else "permission denied", _identity_denied_status(exc))
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("not found", 404)
    except v2_restore.RestoreRefused as exc:
        return _refused(exc)
    except v2_restore.RestoreConfigError as exc:
        return _error(f"restore_config_invalid: {exc}", 500)
    except (StoreConflict, StaleVersion) as exc:
        return _error(str(exc), 409)
