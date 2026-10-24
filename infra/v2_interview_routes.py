"""Interview routes (AIX-K2, local build):

POST /v2/business/gaps/{gap_id}/interview            {workspace_id}         start (or return) the interview of this open question on the current knowledge version   knowledge.confirm + ai.use
GET  /v2/business/interviews/{session_id}            ?workspace_id=         state, transcript, open question, contradictions                                           knowledge.confirm
GET  /v2/business/interviews/{session_id}/vocabulary ?workspace_id=         wording help: words of OTHER accepted rules + blank starters (no model call)       knowledge.confirm
POST /v2/business/interviews/{session_id}/answer     {workspace_id, text}   the person's words for the open question; starts the KNO-07 reading (one model call)        knowledge.confirm + ai.use
POST /v2/business/interviews/{session_id}/refresh    {workspace_id}         pull readings, compute the next question / status (idempotent)                             knowledge.confirm
POST /v2/business/interviews/{session_id}/confirm    {workspace_id}         confirm the latest READY reading with the person's own words through the answer path          knowledge.confirm
POST /v2/business/interviews/{session_id}/stop       {workspace_id}         the person stops (skips / cancels) the interview: UNRESOLVED, nothing written, no model call   knowledge.confirm
The interview writes no knowledge itself; the structured controls always remain available.
"""
from __future__ import annotations

from infra.db_offload import db_route
from infra.v2_policy_routes import _json_body_sync
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters import v2_business, v2_gap_interview
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require, require_human_actor
from infra.adapters.v2_postgres import StaleVersion, StoreConflict, TenantScopeError
from infra.v2_business_routes import _denied, _refusal
from infra.v2_policy_routes import _authority, _error, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


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
    except (v2_gap_interview.InterviewRefused, v2_business.BusinessKnowledgeRefused) as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)
    except (StaleVersion, StoreConflict) as exc:
        return _error(str(exc), 409)
    except Exception as exc:                                                            # budget/provider refusals explained like the single-shot route; anything else raises
        return _refusal(exc)


@router.post("/v2/business/gaps/{gap_id}/interview")
@db_route
def start_interview(request: Request, gap_id: str):
    body = _json_body_sync(request)
    return _shell(request, body.get("workspace_id"), lambda store, tenant, authority: v2_gap_interview.start(store, tenant, body["workspace_id"], gap_id, actor_id=authority["account_id"]), needs_ai=True)


@router.get("/v2/business/interviews/{session_id}")
def read_interview(request: Request, session_id: str, workspace_id: str):
    return _shell(request, workspace_id, lambda store, tenant, authority: v2_gap_interview.refresh(store, tenant, workspace_id, session_id), mutating=False)


@router.get("/v2/business/interviews/{session_id}/vocabulary")
def interview_vocabulary(request: Request, session_id: str, workspace_id: str):
    return _shell(request, workspace_id, lambda store, tenant, a: v2_gap_interview.vocabulary(store, tenant, workspace_id, session_id), mutating=False)


@router.post("/v2/business/interviews/{session_id}/answer")
@db_route
def answer_interview(request: Request, session_id: str):
    body = _json_body_sync(request)
    return _shell(request, body.get("workspace_id"), lambda store, tenant, authority: v2_gap_interview.answer(store, tenant, body["workspace_id"], session_id, text=body.get("text"), authority=authority), needs_ai=True)


@router.post("/v2/business/interviews/{session_id}/refresh")
@db_route
def refresh_interview(request: Request, session_id: str):
    body = _json_body_sync(request)
    return _shell(request, body.get("workspace_id"), lambda store, tenant, authority: v2_gap_interview.refresh(store, tenant, body["workspace_id"], session_id))


@router.post("/v2/business/interviews/{session_id}/confirm")
@db_route
def confirm_interview(request: Request, session_id: str):
    body = _json_body_sync(request)
    return _shell(request, body.get("workspace_id"), lambda store, tenant, authority: v2_gap_interview.confirm(store, tenant, body["workspace_id"], session_id, authority=authority))


@router.post("/v2/business/interviews/{session_id}/stop")
@db_route
def stop_interview(request: Request, session_id: str):
    body = _json_body_sync(request)
    return _shell(request, body.get("workspace_id"), lambda store, tenant, authority: v2_gap_interview.stop(store, tenant, body["workspace_id"], session_id, authority=authority))
