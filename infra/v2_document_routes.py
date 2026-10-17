"""Generated knowledge documents (DOC-02): request a Business/Technical document of an accepted snapshot (explicit view or export), read its state, read its bytes.

Business documents need `project.read`, Technical ones `evidence.read` (contract fkl/1 section 6); rights are rechecked on every call. Requesting creates one deterministic job per
(snapshot, mode, scope, locale, template, redaction) and never a model call; reading never generates. Nothing here touches a source or the knowledge."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from core.change_intelligence.v2 import business_knowledge as bk
from infra.adapters import v2_business, v2_business_documents as docs
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, permissions_for, require, require_human_actor
from infra.adapters.v2_postgres import BlobIntegrityError, StaleVersion, StoreConflict, TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _denied(exc: IdentityDenied):
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


def _refused(exc):
    return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)


def _blob_store():
    """Serving bytes needs the real blob port (the same seam the source routes use); no port means 503, never unverified bytes."""
    import os
    from infra import v2_source_routes
    from infra.adapters.v2_postgres import PostgresV2Store
    blobs = v2_source_routes._blob_port()
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    if blobs is None or not dsn:
        return None
    return PostgresV2Store(dsn, blobs)


@router.post("/v2/business/documents")
async def request_business_documents(request: Request):
    """Ask for the document(s) of one accepted snapshot: `modes` (one or both; both use the SAME snapshot), optional `snapshot_id` (default: the accepted head), `scope`, `locale`,
    `template_version`. Returns one state per mode; a document already generated is returned as is (no new job, no bytes written)."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "project.read")
        modes = body.get("modes") or ([body["mode"]] if isinstance(body.get("mode"), str) else None)
        if not isinstance(modes, list) or not modes or any(m not in docs.da.MODES for m in modes) or len(set(modes)) != len(modes):
            return _error("modes must be a non-empty list of business and/or technical", 400)
        perms = permissions_for(authority.get("roles"))
        for m in modes:
            require(authority, docs.required_permission(m))
        tenant = authority["tenant_id"]
        head = v2_business.business_view(store, tenant, workspace_id)["business_head"]
        out = []
        for m in modes:
            req = docs.normalize_request({**body, "mode": m}, default_snapshot_id=head["accepted_snapshot_id"])
            out.append(docs.request_document(store, tenant, workspace_id, req=req, can_read_evidence="evidence.read" in perms, requested_by=authority["account_id"]))
        return JSONResponse({"documents": out, "snapshot_id": out[0]["snapshot_id"]}, headers=_NO_STORE)
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


def _authorized_state(store, authority, workspace_id, artifact_id):
    state = docs.document_state(store, authority["tenant_id"], workspace_id, artifact_id)
    require(authority, docs.required_permission(state["mode"]))
    if state["redaction_scope"] == "evidence":
        require(authority, "evidence.read")
    return state


@router.get("/v2/business/documents")
def list_business_documents(request: Request, workspace_id: str, snapshot_id: str | None = None):
    """The generated documents this reader may see (never the bytes). Technical ones are listed only with `evidence.read`."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        perms = permissions_for(authority.get("roles"))
        listing = docs.list_documents(store, authority["tenant_id"], workspace_id, snapshot_id=snapshot_id)
        rows = [r for r in listing["documents"] if docs.required_permission(r["mode"]) in perms and (r["redaction_scope"] != "evidence" or "evidence.read" in perms)]
        return JSONResponse({"documents": rows, "truncated": listing["truncated"], "limit": listing["limit"], "retention": listing["retention"]}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/business/documents/{artifact_id}")
def read_business_document(request: Request, artifact_id: str, workspace_id: str):
    """State and freshness of one document (generating / available / stale / failed). Never generates."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        return JSONResponse(_authorized_state(store, authority, workspace_id, artifact_id), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/business/documents/{artifact_id}/content")
def read_business_document_content(request: Request, artifact_id: str, workspace_id: str):
    """The immutable Markdown bytes of an available document (text/markdown; the hash is in `X-Content-Hash`). Rights rechecked; a stale document is served with `X-Freshness: stale`."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        state = _authorized_state(store, authority, workspace_id, artifact_id)
        if state["freshness"] not in ("available", "stale"):
            return JSONResponse({"error": "the document is not available", "code": state["freshness"]}, status_code=409, headers=_NO_STORE)
        blob_store = _blob_store()
        if blob_store is None:
            return _error("document bytes unavailable: no blob store is configured", 503)
        content, row = docs.read_content(blob_store, authority["tenant_id"], workspace_id, artifact_id)
        return Response(content=content, media_type="text/markdown; charset=utf-8",
                        headers={**_NO_STORE, "X-Content-Hash": row["content_hash"], "X-Freshness": state["freshness"], "X-Snapshot-Id": row["snapshot_id"]})
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return _refused(exc)
    except BlobIntegrityError as exc:
        return JSONResponse({"error": f"the stored document bytes are missing or corrupt: {exc}", "code": "integrity"}, status_code=409, headers=_NO_STORE)


# ---- export (DOC-05): the bytes being viewed, as a file or as a bundle of one snapshot ------------------------------------------------------------------------

@router.get("/v2/business/documents/{artifact_id}/download")
def download_business_document(request: Request, artifact_id: str, workspace_id: str):
    """The same immutable bytes as `/content`, served as a file download with a generated safe name (mode, knowledge version, content hash). Rights rechecked; a stale
    document downloads as the version being viewed, never silently as the latest."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        state = _authorized_state(store, authority, workspace_id, artifact_id)
        if state["freshness"] not in ("available", "stale"):
            return JSONResponse({"error": "the document is not available", "code": state["freshness"]}, status_code=409, headers=_NO_STORE)
        blob_store = _blob_store()
        if blob_store is None:
            return _error("document bytes unavailable: no blob store is configured", 503)
        content, row = docs.read_content(blob_store, authority["tenant_id"], workspace_id, artifact_id)
        snap = docs.snapshot_for(store, authority["tenant_id"], workspace_id, row["snapshot_id"])["snapshot"]
        name = docs.download_name(row, snap["business_revision"])
        return Response(content=content, media_type="text/markdown; charset=utf-8",
                        headers={**_NO_STORE, "Content-Disposition": f'attachment; filename="{name}"', "X-Content-Hash": row["content_hash"], "X-Freshness": state["freshness"],
                                 "X-Snapshot-Id": row["snapshot_id"]})
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return _refused(exc)
    except BlobIntegrityError as exc:
        return JSONResponse({"error": f"the stored document bytes are missing or corrupt: {exc}", "code": "integrity"}, status_code=409, headers=_NO_STORE)


@router.post("/v2/business/documents/export")
async def export_business_documents(request: Request):
    """A zip bundle of the requested modes of ONE snapshot (`snapshot_id` explicit: the version being viewed; omitted: the accepted head), with MANIFEST.json. Documents not
    generated yet are requested through the same lazy path and the call answers 409 `generating` with their states until they exist (no bytes, no model). Repeated
    exports of the same version are cache hits: no job, no generation."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "project.read")
        modes = body.get("modes")
        if not isinstance(modes, list) or not modes or any(m not in docs.da.MODES for m in modes) or len(set(modes)) != len(modes):
            return _error("modes must be a non-empty list of business and/or technical", 400)
        perms = permissions_for(authority.get("roles"))
        for m in modes:
            require(authority, docs.required_permission(m))
        tenant = authority["tenant_id"]
        head = v2_business.business_view(store, tenant, workspace_id)["business_head"]
        states = []
        for m in modes:
            req = docs.normalize_request({**{k: v for k, v in body.items() if k != "modes"}, "mode": m}, default_snapshot_id=head["accepted_snapshot_id"])
            states.append(docs.request_document(store, tenant, workspace_id, req=req, can_read_evidence="evidence.read" in perms, requested_by=authority["account_id"]))
        if any(s["freshness"] not in ("available", "stale") for s in states):
            code = "generating" if all(s["freshness"] in ("available", "stale", "generating") for s in states) else "failed"
            return JSONResponse({"error": "not every document of this version exists yet", "code": code, "documents": states}, status_code=409, headers=_NO_STORE)
        blob_store = _blob_store()
        if blob_store is None:
            return _error("document bytes unavailable: no blob store is configured", 503)
        content, manifest = docs.bundle(blob_store, tenant, workspace_id, [s["artifact_id"] for s in states])
        name = f"knowledge-rev{manifest['business_revision']}-{manifest['snapshot_id'][:12]}.zip"
        return Response(content=content, media_type="application/zip", headers={**_NO_STORE, "Content-Disposition": f'attachment; filename="{name}"', "X-Snapshot-Id": manifest["snapshot_id"]})
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
    except BlobIntegrityError as exc:
        return JSONResponse({"error": f"the stored document bytes are missing or corrupt: {exc}", "code": "integrity"}, status_code=409, headers=_NO_STORE)

