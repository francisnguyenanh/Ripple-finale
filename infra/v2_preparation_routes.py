"""EUI-08 guided proposal preparation routes (contract docs/contracts/GUIDED_PREPARATION.md). Off unless RIPPLE_GUIDED_PREPARATION=on.

GET  /v2/intents/{intent_id}/preparation/manifest?workspace_id=&target=&accepted=   what would run, for this person, with their choices (no job, no call)
POST /v2/intents/{intent_id}/preparation        {workspace_id, manifest_hash, target_candidate_id, accepted_refs}   start with explicit choices
GET  /v2/intents/{intent_id}/preparation/runs?workspace_id=                         the caller's own runs of this request
GET  /v2/preparation/runs/{run_id}?workspace_id=                                    the caller's own run
POST /v2/preparation/runs/{run_id}/advance      {workspace_id}                      one idempotent step (the starter's page calls it while the run is active)
POST /v2/preparation/runs/{run_id}/cancel       {workspace_id}                      stop further steps
A request the caller may not read, another person's run and a missing one answer the same 404.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters import v2_preparation as prep
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require, require_human_actor
from infra.adapters.v2_postgres import TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok, _v2_store, submit_job

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _run(request: Request, workspace_id, action, *, mutating: bool):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    if mutating and not _origin_ok(request):
        return _error("origin refused", 403)
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if mutating:
            require_human_actor(authority)

        def submit(kind, job_id, payload):                                                # AS this person, through the normal job-creation contract
            return prep.response_parts(submit_job(store, workspace_id, kind, job_id, payload, lambda: authority))
        return JSONResponse(action(store, authority["tenant_id"], authority, submit), headers=_NO_STORE)
    except IdentityDenied as exc:
        status = _identity_denied_status(exc)
        return _error("sign in required" if status == 401 else "permission denied", status)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error(prep.NOT_FOUND, 404)
    except prep.PreparationRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=exc.status, headers=_NO_STORE)


@router.get("/v2/intents/{intent_id}/preparation/manifest")
def preparation_manifest(request: Request, intent_id: str, workspace_id: str, target: str | None = None, accepted: str | None = None):
    acc = [x for x in (accepted or "").split(",") if x] if accepted is not None else None
    return _run(request, workspace_id, lambda store, tenant, a, _s: prep.manifest(store, tenant, workspace_id, intent_id, a, target=target or None, accepted=acc), mutating=False)


@router.post("/v2/intents/{intent_id}/preparation")
async def preparation_start(request: Request, intent_id: str):
    body = await _json_body(request)
    ws = body.get("workspace_id")
    return _run(request, ws, lambda store, tenant, a, submit: prep.start(store, tenant, ws, intent_id, a, manifest_hash=body.get("manifest_hash"),
                                                                         target=body.get("target_candidate_id"), accepted=body.get("accepted_refs"), submit=submit),
                mutating=True)


@router.get("/v2/intents/{intent_id}/preparation/runs")
def preparation_runs(request: Request, intent_id: str, workspace_id: str):
    return _run(request, workspace_id, lambda store, tenant, a, _s: {"runs": prep.runs_for(store, tenant, workspace_id, intent_id, a), "enabled": prep.enabled()}, mutating=False)


@router.get("/v2/preparation/runs/{run_id}")
def preparation_status(request: Request, run_id: str, workspace_id: str):
    return _run(request, workspace_id, lambda store, tenant, a, _s: prep.status(store, tenant, workspace_id, run_id, a), mutating=False)


@router.post("/v2/preparation/runs/{run_id}/advance")
async def preparation_advance(request: Request, run_id: str):
    body = await _json_body(request)
    ws = body.get("workspace_id")
    return _run(request, ws, lambda store, tenant, a, submit: prep.advance(store, tenant, ws, run_id, a, submit=submit), mutating=True)


@router.post("/v2/preparation/runs/{run_id}/cancel")
async def preparation_cancel(request: Request, run_id: str):
    body = await _json_body(request)
    ws = body.get("workspace_id")
    return _run(request, ws, lambda store, tenant, a, _s: prep.cancel(store, tenant, ws, run_id, a), mutating=True)
