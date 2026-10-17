"""Multi-agent runs (extension contract ma/1, local build with synthetic or pinned providers):

POST /v2/ma/runs                     {workspace_id, change_id, workflow, candidate_job_id?}   start a run over a CONFIRMED change (review_board | arbiter | coder_tester; same binding = same run)   ma.run + ai.use, human actor
GET  /v2/ma/runs/{run_id}            ?workspace_id=                          state, children, budget invariant, report (refs only with evidence rights)   project.read (+ change visibility)
POST /v2/ma/runs/{run_id}/advance    {workspace_id}                          idempotent coordinator step (recovery after a lost join job)                 ma.run
POST /v2/ma/runs/{run_id}/cancel     {workspace_id, reason}                  stop admissions, cancel pending children, release the root reservation      ma.run
GET  /v2/business/changes/{id}/ma    ?workspace_id=                          the runs of a change                                                         project.read (+ change visibility)
Nothing here approves, commits or applies; the report is read by a person in the review.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters import v2_ma
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, permissions_for, require, require_human_actor
from infra.adapters.v2_postgres import StaleVersion, StoreConflict, TenantScopeError
from infra.v2_business_routes import _change_record_for, _denied
from infra.v2_policy_routes import _authority, _error, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _shell(request, workspace_id, permission, action, *, mutating=True, needs_ai=False):
    if mutating and not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        if mutating:
            require_human_actor(authority)
        require(authority, permission)
        if needs_ai:
            require(authority, "ai.use")
        return JSONResponse(action(store, authority["tenant_id"], authority), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_ma.MaRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)
    except (StaleVersion, StoreConflict) as exc:
        return _error(str(exc), 409)


def _visible_run(store, tenant, workspace_id, run_id, authority):
    run = v2_ma._run(store, tenant, workspace_id, run_id)
    if run is None:
        raise TenantScopeError("run not found")
    _change_record_for(store, tenant, workspace_id, run["change_id"], authority)
    return run


@router.post("/v2/ma/runs")
async def start_run(request: Request):
    body = await _json_body(request)
    change_id, workflow = body.get("change_id"), body.get("workflow", "review_board")
    if not isinstance(change_id, str) or not change_id.startswith("chg-") or not isinstance(workflow, str):
        return _error("change_id (chg-…) and workflow required", 400)

    def action(store, tenant, authority):
        _change_record_for(store, tenant, body["workspace_id"], change_id, authority)
        from infra import provider_gate
        provider_gate.require_provider(v2_ma.KIND_SPECIALIST)
        return v2_ma.start_run(store, tenant, body["workspace_id"], change_id, workflow=workflow, authority=authority, candidate_job_id=body.get("candidate_job_id"), calibration=body.get("calibration"))
    try:
        return _shell(request, body.get("workspace_id"), v2_ma.CAPABILITY, action, needs_ai=True)
    except Exception as exc:                                                            # provider disarmed / allocation refusals carry their own explained 409
        from infra import provider_gate
        resp = provider_gate.refusal_for(exc)
        if resp is not None:
            return resp
        raise


@router.get("/v2/ma/runs/{run_id}")
def read_run(request: Request, run_id: str, workspace_id: str):
    def action(store, tenant, authority):
        _visible_run(store, tenant, workspace_id, run_id, authority)
        return v2_ma.state(store, tenant, workspace_id, run_id, can_read_evidence="evidence.read" in permissions_for(authority.get("roles")))
    return _shell(request, workspace_id, "project.read", action, mutating=False)


@router.post("/v2/ma/runs/{run_id}/advance")
async def advance_run(request: Request, run_id: str):
    body = await _json_body(request)

    def action(store, tenant, authority):
        _visible_run(store, tenant, body["workspace_id"], run_id, authority)
        return v2_ma.advance(store, tenant, body["workspace_id"], run_id, actor_id=authority["account_id"])
    return _shell(request, body.get("workspace_id"), v2_ma.CAPABILITY, action)


@router.post("/v2/ma/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str):
    body = await _json_body(request)
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return _error("reason required", 400)

    def action(store, tenant, authority):
        _visible_run(store, tenant, body["workspace_id"], run_id, authority)
        return v2_ma.cancel(store, tenant, body["workspace_id"], run_id, actor_id=authority["account_id"], reason=reason.strip())
    return _shell(request, body.get("workspace_id"), v2_ma.CAPABILITY, action)


@router.get("/v2/business/changes/{change_id}/ma")
def runs_of_change(request: Request, change_id: str, workspace_id: str):
    def action(store, tenant, authority):
        _change_record_for(store, tenant, workspace_id, change_id, authority)
        return {"change_id": change_id, "runs": v2_ma.runs_for_change(store, tenant, workspace_id, change_id)}
    return _shell(request, workspace_id, "project.read", action, mutating=False)
