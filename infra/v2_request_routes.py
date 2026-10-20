"""Request routes (contract `ripple-request-task/1`, docs/contracts/REQUEST_TASKS_AND_ASSISTANT.md).

GET /v2/requests/{family}/{request_id}/task-view?workspace_id=   read-only task view of one request (RUX-01): who holds the next step, who waits, what blocks, what the
reader may do. The reader must be able to read the request under its family's existing rule; anyone else gets the generic "request not found" (no hint whether it exists).
Nothing is written, no job runs, no model is called.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters import v2_request_tasks
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require
from infra.adapters.v2_postgres import TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/v2/requests/{family}/{request_id}/task-view")
def request_task_view(request: Request, family: str, request_id: str, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        from infra import operator_flags
        out = v2_request_tasks.task_view(store, authority["tenant_id"], workspace_id, family, request_id, authority)
        out["features"] = {"request_assistant": "on" if operator_flags.assistant_on() else "off"}            # the page hides the launcher; the routes refuse by themselves
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        status = _identity_denied_status(exc)
        return _error("sign in required" if status == 401 else "permission denied", status)
    except PermissionDenied:
        return _error(v2_request_tasks.NOT_FOUND, 404)
    except TenantScopeError:
        return _error(v2_request_tasks.NOT_FOUND, 404)
