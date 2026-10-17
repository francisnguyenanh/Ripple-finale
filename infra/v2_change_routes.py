"""Routes for the change read model, the human result decision and the
manual-work proof. Authority follows the module invariant of
`infra/v2_policy_routes.py`: the caller's tenant / roles / actor kind come ONLY
from the server-side session (`_authority`); nothing in the body (`actor_id`,
`override`, `force`, `report_status`, `checks`, ...) is ever trusted. Both
mutating routes need a HUMAN actor holding `change.accept_result`
(`ROUTE_PERMISSIONS`). See `infra/adapters/v2_change.py` for the gate.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters.v2_change import (
    ACTIONS, GateRefused, InvalidWorkIndex, apply_subject, get_change_view,
    record_result_decision, record_work_proof,
)
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import (
    PermissionDenied, RESULT_ACCEPT_SUBJECT_PERMISSION, permissions_for, require,
    require_human_actor,
)
from infra.adapters.v2_postgres import StaleVersion, StoreConflict, TenantScopeError
from infra.v2_policy_routes import (
    _authority, _error, _identity_store, _json_body, _origin_ok, _v2_store,
)

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/v2/changes/{apply_job_id}")
def change_view(request: Request, apply_job_id: str, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        perms = permissions_for(authority.get("roles"))
        viewer = {"can_read_evidence": "evidence.read" in perms, "can_resolve": "conflict.resolve" in perms}
        return JSONResponse(get_change_view(store, authority["tenant_id"], workspace_id,
                                            apply_job_id, viewer=viewer), headers=_NO_STORE)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/changes/{apply_job_id}/result-decision")
async def result_decision(request: Request, apply_job_id: str):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    action = body.get("action")
    if action not in ACTIONS:
        return _error("action must be one of accept|request_changes|reject", 400)
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return _error("a non-empty reason is required", 400)
    digest = body.get("expected_report_digest")
    if not isinstance(digest, str) or not digest:
        return _error("expected_report_digest required", 400)
    key = body.get("idempotency_key")
    if key is not None and not isinstance(key, str):
        return _error("idempotency_key must be a string", 400)
    disp = body.get("item_dispositions")
    if disp is not None and not (isinstance(disp, list) and all(isinstance(x, dict) for x in disp)):
        return _error("item_dispositions must be a list of {item_id, disposition, reason}", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        # Subject-dependent permission (CHANGE -> change.accept_result,
        # RESTORE -> source.restore), resolved from the apply job's own payload.
        require(authority, RESULT_ACCEPT_SUBJECT_PERMISSION[
            apply_subject(store, authority["tenant_id"], workspace_id, apply_job_id)])
        result = record_result_decision(
            store, authority["tenant_id"], workspace_id, apply_job_id,
            actor_id=authority["account_id"], actor_kind=authority["actor_kind"],
            permissions=permissions_for(authority.get("roles")), action=action,
            reason=reason, expected_digest=digest, idempotency_key=key, dispositions=disp, decision_basis=body.get("expected_decision_basis") if isinstance(body.get("expected_decision_basis"), str) else None)
        return JSONResponse({k: v for k, v in result.items()
                             if v is not None or k != "new_proposal_job_id"},
                            headers=_NO_STORE)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except GateRefused as exc:
        payload = {"error": "completion gate refused", "reasons": exc.reasons}
        if exc.findings:
            payload["findings"] = exc.findings
        return JSONResponse(payload, status_code=409, headers=_NO_STORE)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except (StoreConflict, StaleVersion) as exc:
        return _error(str(exc), 409)


@router.post("/v2/changes/{apply_job_id}/work-proof")
async def work_proof(request: Request, apply_job_id: str):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    index = body.get("work_index")
    if type(index) is not int or index < 0:
        return _error("work_index must be a non-negative integer", 400)
    evidence = body.get("evidence_ref")
    if not isinstance(evidence, str) or not evidence.strip():
        return _error("a non-empty evidence_ref is required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, RESULT_ACCEPT_SUBJECT_PERMISSION[
            apply_subject(store, authority["tenant_id"], workspace_id, apply_job_id)])
        result = record_work_proof(store, authority["tenant_id"], workspace_id, apply_job_id,
                                   actor_id=authority["account_id"], work_index=index,
                                   evidence_ref=evidence.strip())
        return JSONResponse(result, headers=_NO_STORE)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except InvalidWorkIndex as exc:
        return _error(str(exc), 400)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except (StoreConflict, StaleVersion) as exc:
        return _error(str(exc), 409)
