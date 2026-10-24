"""Semantic conflict mining routes (extension AIX-K1):

POST /v2/business/mining                               {workspace_id, attempt?}            plan the rule groups of the current knowledge version and create one job per group   knowledge.confirm + ai.use
GET  /v2/business/mining                               ?workspace_id=&snapshot_id?&attempt? hypotheses, rejected items, coverage, job states, dismissals (stale flag)             knowledge.confirm
POST /v2/business/mining/hypotheses/{hypothesis_id}/dismiss {workspace_id, reason, attempt?} a person dismisses one hypothesis with a reason (pinned to the version)         knowledge.confirm
Nothing here changes the knowledge; raising a hypothesis as a question goes through the existing structured controls.
"""
from __future__ import annotations

from infra.db_offload import db_route
from infra.v2_policy_routes import _json_body_sync
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters import v2_business, v2_mining
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require, require_human_actor
from infra.adapters.v2_postgres import StaleVersion, StoreConflict, TenantScopeError
from infra.v2_business_routes import _denied, _refusal
from infra.v2_policy_routes import _authority, _error, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _attempt(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 3 else None


def _shell(request, workspace_id, action, *, mutating=True, needs_ai=False):
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
        require(authority, "knowledge.confirm")
        if needs_ai:
            require(authority, "ai.use")
        return JSONResponse(action(store, authority["tenant_id"], authority), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)
    except (StaleVersion, StoreConflict) as exc:
        return _error(str(exc), 409)
    except Exception as exc:
        return _refusal(exc)


@router.post("/v2/business/mining")
@db_route
def request_mining(request: Request):
    body = _json_body_sync(request)
    attempt = _attempt(body.get("attempt", 1))
    if attempt is None:
        return _error("attempt (1-3) required", 400)

    def action(store, tenant, authority):
        from infra import provider_gate
        from infra.v2_business_routes import _quota_for
        provider_gate.require_provider(v2_mining.KIND)
        limit, reservation = _quota_for(v2_mining.KIND)
        return v2_mining.request(store, tenant, body["workspace_id"], attempt=attempt, authority=authority, limit=limit, reservation=reservation)
    return _shell(request, body.get("workspace_id"), action, needs_ai=True)


@router.get("/v2/business/mining")
def read_mining(request: Request, workspace_id: str, snapshot_id: str | None = None, attempt: int = 1):
    if _attempt(attempt) is None:
        return _error("attempt (1-3) required", 400)
    return _shell(request, workspace_id, lambda store, tenant, authority: v2_mining.view(store, tenant, workspace_id, snapshot_id=snapshot_id, attempt=attempt, viewer=authority["account_id"]), mutating=False)


@router.post("/v2/business/mining/hypotheses/{hypothesis_id}/dismiss")
@db_route
def dismiss_hypothesis(request: Request, hypothesis_id: str):
    body = _json_body_sync(request)
    attempt = _attempt(body.get("attempt", 1))
    if attempt is None:
        return _error("attempt (1-3) required", 400)
    return _shell(request, body.get("workspace_id"), lambda store, tenant, authority: v2_mining.dismiss(store, tenant, body["workspace_id"], hypothesis_id=hypothesis_id, reason=body.get("reason"),
                                                                                                        actor_id=authority["account_id"], attempt=attempt))
