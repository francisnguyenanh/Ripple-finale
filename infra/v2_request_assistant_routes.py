"""Request assistant routes (RQA-02; contract docs/contracts/REQUEST_TASKS_AND_ASSISTANT.md section 5). Private threads, explicit asks, no model call on create / read.

POST /v2/requests/{family}/{request_id}/assistant/threads           {workspace_id}                                  create a private thread (no model call)
GET  /v2/requests/{family}/{request_id}/assistant/threads?workspace_id=                                              the caller's own threads of that request
GET  /v2/request-assistant/threads/{thread_id}?workspace_id=                                                        the caller's thread (answers published under the same access)
POST /v2/requests/{family}/{request_id}/assistant/ask               {workspace_id, question, idempotency_key}       first question: new private thread + ask, one idempotent action
POST /v2/request-assistant/threads/{thread_id}/messages             {workspace_id, question, idempotency_key, after_seq}   ask (one admitted `request.qa` job)
GET  /v2/request-assistant/threads/{thread_id}/messages/{message_id}/citations/{fact_ref}/source?workspace_id=   EUI-03 the cited fact's source (server-resolved; evidence.read)
POST /v2/request-assistant/threads/{thread_id}/messages/{message_id}/cancel  {workspace_id}                         stop a pending answer where possible
Another person's thread, a request the caller may not read and a missing one all answer the same generic 404.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters import v2_request_threads as rt
from infra.adapters import v2_request_tasks
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require, require_human_actor
from infra.adapters.v2_postgres import TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _run(request: Request, workspace_id, action, *, mutating: bool, not_found: str):
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
        return JSONResponse(action(store, authority["tenant_id"], authority), headers=_NO_STORE)
    except IdentityDenied as exc:
        status = _identity_denied_status(exc)
        return _error("sign in required" if status == 401 else "permission denied", status)
    except PermissionDenied as exc:
        if str(exc) == rt.CAPABILITY:
            return JSONResponse({"error": "Asking the request assistant needs the assistant permission (or it is suspended for you).", "code": "assistant_not_permitted"},
                                status_code=403, headers=_NO_STORE)
        return _error(not_found, 404)
    except TenantScopeError:
        return _error(not_found, 404)
    except rt.AssistantRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=exc.status, headers=_NO_STORE)
    except Exception as exc:                                                                    # provider disarmed / budget / concurrency: their explained refusal
        from infra import provider_gate
        resp = provider_gate.refusal_for(exc)
        if resp is not None:
            return resp
        raise


@router.post("/v2/requests/{family}/{request_id}/assistant/threads")
async def create_assistant_thread(request: Request, family: str, request_id: str):
    body = await _json_body(request)
    return _run(request, body.get("workspace_id"), lambda store, tenant, a: rt.create_thread(store, tenant, body["workspace_id"], family, request_id, a),
                mutating=True, not_found=v2_request_tasks.NOT_FOUND)


@router.post("/v2/requests/{family}/{request_id}/assistant/ask")
async def first_ask_assistant(request: Request, family: str, request_id: str):
    body = await _json_body(request)
    return _run(request, body.get("workspace_id"), lambda store, tenant, a: rt.first_ask(store, tenant, body["workspace_id"], family, request_id, a, question=body.get("question"),
                                                                                         idempotency_key=body.get("idempotency_key")),
                mutating=True, not_found=v2_request_tasks.NOT_FOUND)


@router.get("/v2/requests/{family}/{request_id}/assistant/threads")
def list_assistant_threads(request: Request, family: str, request_id: str, workspace_id: str):
    return _run(request, workspace_id, lambda store, tenant, a: rt.list_threads(store, tenant, workspace_id, family, request_id, a), mutating=False,
                not_found=v2_request_tasks.NOT_FOUND)


@router.get("/v2/request-assistant/threads/{thread_id}")
def read_assistant_thread(request: Request, thread_id: str, workspace_id: str):
    return _run(request, workspace_id, lambda store, tenant, a: rt.read_thread(store, tenant, workspace_id, thread_id, a), mutating=False, not_found=rt.THREAD_NOT_FOUND)


@router.post("/v2/request-assistant/threads/{thread_id}/messages")
async def ask_assistant(request: Request, thread_id: str):
    body = await _json_body(request)
    return _run(request, body.get("workspace_id"), lambda store, tenant, a: rt.ask(store, tenant, body["workspace_id"], thread_id, a, question=body.get("question"),
                                                                                   idempotency_key=body.get("idempotency_key"), after_seq=body.get("after_seq")),
                mutating=True, not_found=rt.THREAD_NOT_FOUND)


def _with_blobs(store):
    """The route store has no blob port (`_NullBlobs`); the YAML excerpt reads pinned bytes through the source blob seam. Without one, no excerpt (never a guess)."""
    from infra import v2_source_routes
    from infra.adapters.v2_postgres import PostgresV2Store
    blobs = v2_source_routes._blob_port()
    return PostgresV2Store(os.environ["RIPPLE_V2_DSN"], blobs) if blobs is not None else store


@router.get("/v2/request-assistant/threads/{thread_id}/messages/{message_id}/citations/{fact_ref}/source")
def citation_source(request: Request, thread_id: str, message_id: str, fact_ref: str, workspace_id: str):
    return _run(request, workspace_id, lambda store, tenant, a: rt.citation_source(_with_blobs(store), tenant, workspace_id, thread_id, message_id, fact_ref, a),
                mutating=False, not_found=rt.THREAD_NOT_FOUND)


@router.post("/v2/request-assistant/threads/{thread_id}/messages/{message_id}/cancel")
async def cancel_assistant_message(request: Request, thread_id: str, message_id: str):
    body = await _json_body(request)
    return _run(request, body.get("workspace_id"), lambda store, tenant, a: rt.cancel(store, tenant, body["workspace_id"], thread_id, message_id, a),
                mutating=True, not_found=rt.THREAD_NOT_FOUND)
