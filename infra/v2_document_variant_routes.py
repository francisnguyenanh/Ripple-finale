"""Audience variants of generated documents (extension AIX-D1):

POST /v2/business/documents/{artifact_id}/variants      {workspace_id, audience, locale?, attempt?}   request (or find) the variant; base permission + redaction rechecked, `ai.use`   human actor
GET  /v2/business/documents/{artifact_id}/variants      ?workspace_id=                      the variants of one base document (states)
GET  /v2/business/documents/variants/{variant_id}       ?workspace_id=                      state (never the bytes)
GET  /v2/business/documents/variants/{variant_id}/content ?workspace_id=                    the immutable Markdown bytes
Reading never generates; the same variant key never makes a second model call.
"""
from __future__ import annotations

from infra.db_offload import db_route
from infra.v2_policy_routes import _json_body_sync
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from core.change_intelligence.v2 import business_knowledge as bk
from infra.adapters import v2_business, v2_business_documents as docs, v2_document_variants as variants
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require, require_human_actor
from infra.adapters.v2_postgres import BlobIntegrityError, StaleVersion, StoreConflict, TenantScopeError
from infra.v2_business_routes import _refusal
from infra.v2_document_routes import _authorized_state, _blob_store, _denied, _refused
from infra.v2_policy_routes import _authority, _error, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _shell(request, workspace_id, action, *, mutating=False, store=None):
    if mutating and not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), store or _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        if mutating:
            require_human_actor(authority)
        require(authority, "project.read")
        return action(store, authority)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except bk.ContractError as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=422, headers=_NO_STORE)
    except v2_business.BusinessKnowledgeRefused as exc:
        return _refused(exc)
    except (StaleVersion, StoreConflict) as exc:
        return _error(str(exc), 409)
    except Exception as exc:
        return _refusal(exc)


@router.post("/v2/business/documents/{artifact_id}/variants")
@db_route
def request_variant(request: Request, artifact_id: str):
    body = _json_body_sync(request)
    audience = body.get("audience")
    if audience not in variants.dv.AUDIENCES:
        return _error(f"audience must be one of {', '.join(variants.dv.AUDIENCES)}", 400)
    locale = body.get("locale")
    if locale is not None and not (isinstance(locale, str) and 1 <= len(locale) <= 8):
        return _error("locale invalid", 400)
    attempt = body.get("attempt", 1)
    if not (isinstance(attempt, int) and not isinstance(attempt, bool) and 1 <= attempt <= 3):          # a new attempt after a failed variant (same bounds as mining)
        return _error("attempt must be 1, 2 or 3", 400)

    def action(store, authority):
        _authorized_state(store, authority, body["workspace_id"], artifact_id)
        require(authority, "ai.use")
        from infra import provider_gate
        from infra.v2_business_routes import _quota_for
        provider_gate.require_provider(variants.KIND)
        limit, reservation = _quota_for(variants.KIND)
        out = variants.request_variant(store, authority["tenant_id"], body["workspace_id"], artifact_id=artifact_id, audience=audience, locale=locale, requested_by=authority["account_id"],
                                       limit=limit, reservation=reservation, attempt=attempt)
        if out["freshness"] == "generating":
            from infra import ai_admission
            ai_admission.admit(store, authority["tenant_id"], body["workspace_id"], out["variant_id"], variants.KIND, authority)
        return JSONResponse(out, headers=_NO_STORE)
    return _shell(request, body.get("workspace_id"), action, mutating=True)


@router.get("/v2/business/documents/{artifact_id}/variants")
def list_variants(request: Request, artifact_id: str, workspace_id: str):
    def action(store, authority):
        _authorized_state(store, authority, workspace_id, artifact_id)
        return JSONResponse({"artifact_id": artifact_id, "variants": variants.list_variants(store, authority["tenant_id"], workspace_id, artifact_id)}, headers=_NO_STORE)
    return _shell(request, workspace_id, action)


@router.get("/v2/business/documents/variants/{variant_id}")
def read_variant(request: Request, variant_id: str, workspace_id: str):
    def action(store, authority):
        st = variants.variant_state(store, authority["tenant_id"], workspace_id, variant_id)
        _authorized_state(store, authority, workspace_id, st["artifact_id"])
        return JSONResponse(st, headers=_NO_STORE)
    return _shell(request, workspace_id, action)


@router.get("/v2/business/documents/variants/{variant_id}/content")
def read_variant_content(request: Request, variant_id: str, workspace_id: str):
    store = _blob_store()
    if store is None:
        return _error("document bytes unavailable: no blob store", 503)

    def action(store, authority):
        st = variants.variant_state(store, authority["tenant_id"], workspace_id, variant_id)
        _authorized_state(store, authority, workspace_id, st["artifact_id"])
        if st["freshness"] not in ("available", "stale"):
            return JSONResponse({"error": f"variant {st['freshness']}", "code": "not_available", "freshness": st["freshness"], "failure_reason": st["failure_reason"]}, status_code=409, headers=_NO_STORE)
        try:
            content, row = variants.read_content(store, authority["tenant_id"], workspace_id, variant_id)
        except BlobIntegrityError as exc:
            return _error(f"integrity: {exc}", 409)
        headers = {**_NO_STORE, "X-Content-Hash": row["content_hash"], "X-Variant-Status": row["status"], "X-Freshness": st["freshness"]}
        return Response(content=content, media_type="text/markdown; charset=utf-8", headers=headers)
    return _shell(request, workspace_id, action, store=store)
