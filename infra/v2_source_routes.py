"""Authenticated upload/staging, manifest preview and commit.

Flow: `POST /v2/sources/imports` admits an upload (pure checks in
`core/change_intelligence/v2/source_admission.py`), stages only admitted
bytes in the immutable blob store and records a PREPARED import. Nothing is
current yet. A human holding `source.import` then commits it with an exact
skip acknowledgement; `PostgresV2Store.commit_source_import` creates the
immutable revision and its durable `source_ingest` job in one transaction.

Authority always comes from `infra.v2_policy_routes._authority` (session
cookie + path/body workspace), never from body fields. The server never
fetches a URL or reads a client-named local path.
"""

from __future__ import annotations

import base64
import binascii
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.change_intelligence.v2.source_admission import (
    MAX_TOTAL_BYTES, AdmissionRejected, admit_files,
)
from infra.adapters.v2_identity import IdentityDenied, PostgresIdentityStore
from infra.adapters.v2_policy import PermissionDenied, require, require_human_actor
from infra.adapters.v2_source_archive import expand_archive
from infra.adapters.v2_postgres import (
    BlobIntegrityError, DeletionAcknowledgementRequired, ImportExpired, NoAdmittedItems,
    PostgresV2Store, RefreshDigestMismatch, RefreshExpired, SkipAcknowledgementRequired,
    StaleAuthorityError, StaleVersion, StoreConflict, TenantScopeError,
)
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _origin_ok


router = APIRouter()

#: Base64 JSON of a maximal (20 MiB) bundle plus headroom.
MAX_REQUEST_BYTES = MAX_TOTAL_BYTES * 2 + 1_048_576
#: Body fields that would make the server fetch or read something the client
#: names. Only upload/staging is implemented here.
_REFERENCE_FIELDS = ("url", "source_url", "local_path", "path", "registered_source_id")


def _blob_port():
    bucket_name = os.environ.get("RIPPLE_V2_BLOB_BUCKET")
    if not bucket_name:
        return None
    from google.cloud import storage

    from infra.adapters.v2_blobs import ImmutableGCSBlobs
    return ImmutableGCSBlobs(storage.Client().bucket(bucket_name))


def _services(blobs=None):
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    if not dsn:
        return None, None
    return PostgresIdentityStore(dsn), PostgresV2Store(dsn, blobs if blobs is not None
                                                       else _ReadOnlyNoBlobs())


class _ReadOnlyNoBlobs:
    """Read-only routes never touch bytes."""

    def put_immutable(self, digest, content):
        raise NotImplementedError

    def read(self, digest):
        raise NotImplementedError


def _ok(payload: dict):
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def _rejected(exc: AdmissionRejected):
    return JSONResponse({"error": exc.error_code, "reason": exc.reason},
                        status_code=422, headers={"Cache-Control": "no-store"})


def _decode(value) -> bytes:
    if not isinstance(value, str):
        raise AdmissionRejected("invalid_upload", "content_b64 must be a string")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AdmissionRejected("invalid_upload", "content_b64 is not valid base64") from exc


async def _bounded_json(request: Request):
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        return None
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        return None
    try:
        import json
        body = json.loads(raw)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _entries(body: dict) -> list[tuple]:
    """Upload entries `(path, bytes, role)` from `files` or one `archive`."""
    files, archive = body.get("files"), body.get("archive")
    if files is not None and archive is not None:
        raise AdmissionRejected("invalid_upload", "send files or archive, not both")
    if archive is not None:
        if not isinstance(archive, dict):
            raise AdmissionRejected("invalid_upload", "archive must be an object")
        return [(p, c, None) for p, c in
                expand_archive(archive.get("filename"), _decode(archive.get("content_b64")))]
    if files is None:
        files = []
    if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
        raise AdmissionRejected("invalid_upload", "files must be a list of objects")
    return [(f.get("path"), _decode(f.get("content_b64")), f.get("source_role"))
            for f in files]


@router.post("/v2/sources/imports")
async def prepare_import(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    body = await _bounded_json(request)
    if body is None:
        return _error("request body too large", 413)
    if any(field in body for field in _REFERENCE_FIELDS):
        return _error("unsupported_source_reference", 400)
    workspace_id, key = body.get("workspace_id"), body.get("idempotency_key")
    if not all(isinstance(x, str) and x for x in (workspace_id, key)):
        return _error("workspace_id and idempotency_key required", 400)
    identity, _ = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "source.import")
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    blobs = _blob_port()
    if blobs is None:
        return _error("source storage unavailable", 503)
    _, store = _services(blobs)
    try:
        report = admit_files(_entries(body), language=body.get("language"))
    except AdmissionRejected as exc:
        return _rejected(exc)
    except ValueError as exc:
        return _error(str(exc), 400)
    try:
        # Stage only admitted bytes, after every batch check has passed.
        for item in report.admitted:
            store.stage_blob(item.content)
        view = store.prepare_source_import(
            authority["tenant_id"], workspace_id, actor_id=authority["account_id"],
            idempotency_key=key, items=[i.to_dict() for i in report.items],
            language=body.get("language"))
    except StoreConflict as exc:
        return _error(str(exc), 409)
    except TenantScopeError:
        return _error("workspace not found", 404)
    except BlobIntegrityError:
        return _error("source storage unavailable", 503)
    return _ok(view)


@router.get("/v2/sources/imports/{import_id}")
def read_import(request: Request, import_id: str, workspace_id: str):
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        return _ok(store.source_import(authority["tenant_id"], workspace_id, import_id))
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("import not found", 404)


@router.post("/v2/sources/imports/{import_id}/commit")
async def commit_import(request: Request, import_id: str):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    body = await _bounded_json(request) or {}
    workspace_id = body.get("workspace_id")
    expected_version = body.get("expected_version")
    acks = body.get("acknowledged_skips", [])
    roles = body.get("source_roles")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    if type(expected_version) is not int:
        return _error("expected_version required", 400)
    if not isinstance(acks, list) or (roles is not None and not isinstance(roles, dict)):
        return _error("acknowledged_skips must be a list and source_roles an object", 400)
    identity, _ = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "source.import")
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    blobs = _blob_port()
    if blobs is None:
        return _error("source storage unavailable", 503)
    _, store = _services(blobs)
    try:
        result = store.commit_source_import(
            authority["tenant_id"], workspace_id, import_id,
            actor_id=authority["account_id"], expected_version=expected_version,
            acknowledged_skips=acks, source_roles=roles, required_permission="source.import")
    except TenantScopeError:
        return _error("import not found", 404)
    except SkipAcknowledgementRequired:
        return _error("skip_acknowledgement_required", 409)
    except ImportExpired:
        return _error("import_expired", 409)
    except NoAdmittedItems:
        return _error("no_admitted_items", 409)
    except StaleVersion:
        return _error("stale_revision", 409)
    except BlobIntegrityError:
        return _error("staged source bytes unavailable", 409)
    except StaleAuthorityError as exc:
        # Same 409 family as `recheck_and_publish_job`'s stale-
        # approver refusal -- a distinct branch only to keep this message
        # stable for callers/tests, since it is also a `StoreConflict`.
        return _error(str(exc), 409)
    except StoreConflict as exc:
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)
    return _ok(result)


# ---------------------------------------------------------------------------
# Manual external-source refresh (prepare -> commit -> status).
#
# A refresh is an OBSERVATION of source that changed outside Ripple. The upload is
# the WHOLE new snapshot (same admission as an import); the server diffs it against
# the current head, derives the provenance (`external_observation`) itself and
# publishes through the same kind of atomic, CAS-guarded transaction an import
# uses. It never creates an approval, a decision or a ChangeSet, and a client can
# never name provenance, approval or decision fields (strict body whitelist).
# ---------------------------------------------------------------------------

_REFRESH_PREPARE_FIELDS = frozenset({"workspace_id", "idempotency_key", "files", "archive",
                                     "language", "rename_proofs"})
_REFRESH_COMMIT_FIELDS = frozenset({"workspace_id", "refresh_id", "expected_version",
                                    "expected_refresh_digest", "acknowledged_deletions"})


def _identity_denied(exc: IdentityDenied):
    """No/invalid session -> 401; a signed-in account with no CURRENT membership in the
    requested workspace -> 403 (never reveals whether that workspace exists)."""
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


def _refresh_prepare_view(view: dict) -> dict:
    return {k: view[k] for k in ("refresh_id", "workspace_id", "status", "noop", "base",
                                 "provenance", "diff", "refresh_digest", "expires_at")}


@router.post("/v2/sources/refresh/prepare")
async def prepare_refresh(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    body = await _bounded_json(request)
    if body is None:
        return _error("request body too large", 413)
    unknown = sorted(set(body) - _REFRESH_PREPARE_FIELDS)
    if unknown:
        # provenance / approval / decision / subject fields are server-owned.
        return _error("unsupported_field: " + ",".join(str(u) for u in unknown[:5]), 400)
    workspace_id, key = body.get("workspace_id"), body.get("idempotency_key")
    if not all(isinstance(x, str) and x for x in (workspace_id, key)):
        return _error("workspace_id and idempotency_key required", 400)
    identity, _ = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "source.import")
    except IdentityDenied as exc:
        return _identity_denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    blobs = _blob_port()
    if blobs is None:
        return _error("source storage unavailable", 503)
    _, store = _services(blobs)
    try:
        report = admit_files(_entries(body), language=body.get("language"))
    except AdmissionRejected as exc:
        return _rejected(exc)
    except ValueError as exc:
        return _error(str(exc), 400)
    if not report.items or len(report.admitted) != len(report.items):
        return JSONResponse({"error": "refresh_requires_admitted_files",
                             "reason": "every uploaded file must be admitted; a refresh cannot skip items"},
                            status_code=422, headers={"Cache-Control": "no-store"})
    from core.change_intelligence.v2.source_refresh import RefreshProofError, normalise_proofs
    try:
        proofs = normalise_proofs(body.get("rename_proofs"))
    except RefreshProofError as exc:
        return JSONResponse({"error": exc.error_code, "reason": str(exc)}, status_code=400,
                            headers={"Cache-Control": "no-store"})
    try:
        for item in report.admitted:
            store.stage_blob(item.content)
        view = store.prepare_source_refresh(
            authority["tenant_id"], workspace_id, actor_id=authority["account_id"],
            idempotency_key=key, items=[i.to_dict() for i in report.items],
            rename_proofs=proofs, language=body.get("language"))
    except RefreshProofError as exc:
        return JSONResponse({"error": exc.error_code, "reason": str(exc)}, status_code=400,
                            headers={"Cache-Control": "no-store"})
    except StoreConflict as exc:
        return _error(str(exc), 409)
    except TenantScopeError:
        return _error("workspace not found", 404)
    except BlobIntegrityError:
        return _error("source storage unavailable", 503)
    return _ok(_refresh_prepare_view(view))


@router.post("/v2/sources/refresh/commit")
async def commit_refresh(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    body = await _bounded_json(request) or {}
    unknown = sorted(set(body) - _REFRESH_COMMIT_FIELDS)
    if unknown:
        return _error("unsupported_field: " + ",".join(str(u) for u in unknown[:5]), 400)
    workspace_id, refresh_id = body.get("workspace_id"), body.get("refresh_id")
    expected_version, digest = body.get("expected_version"), body.get("expected_refresh_digest")
    acks = body.get("acknowledged_deletions", [])
    if not all(isinstance(x, str) and x for x in (workspace_id, refresh_id)):
        return _error("workspace_id and refresh_id required", 400)
    if type(expected_version) is not int:
        return _error("expected_version required", 400)
    if not isinstance(digest, str) or not digest:
        return _error("expected_refresh_digest required", 400)
    if not isinstance(acks, list) or not all(isinstance(a, str) for a in acks):
        return _error("acknowledged_deletions must be a list of logical paths", 400)
    identity, _ = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "source.import")
    except IdentityDenied as exc:
        return _identity_denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    blobs = _blob_port()
    if blobs is None:
        return _error("source storage unavailable", 503)
    _, store = _services(blobs)
    try:
        result = store.commit_source_refresh(
            authority["tenant_id"], workspace_id, refresh_id,
            actor_id=authority["account_id"], expected_version=expected_version,
            expected_refresh_digest=digest, acknowledged_deletions=acks,
            required_permission="source.import")
    except TenantScopeError:
        return _error("refresh not found", 404)
    except DeletionAcknowledgementRequired:
        return _error("deletion_acknowledgement_required", 409)
    except RefreshExpired:
        return _error("refresh_expired", 409)
    except RefreshDigestMismatch:
        return _error("refresh_digest_mismatch", 409)
    except StaleVersion:
        return _error("stale_revision", 409)
    except BlobIntegrityError:
        return _error("staged source bytes unavailable", 409)
    except StoreConflict as exc:  # includes StaleAuthorityError
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)
    return _ok(result)


@router.get("/v2/sources/refresh/{refresh_id}")
def read_refresh(request: Request, refresh_id: str, workspace_id: str):
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        from infra.worker.refresh_impact import refresh_status
        return _ok(refresh_status(store, authority["tenant_id"], workspace_id, refresh_id))
    except IdentityDenied as exc:
        return _identity_denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("refresh not found", 404)


@router.get("/v2/sources/revisions/{revision_id}")
def read_revision(request: Request, revision_id: str, workspace_id: str):
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        return _ok(store.source_revision_view(authority["tenant_id"], workspace_id,
                                              revision_id))
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("source revision not found", 404)


@router.post("/v2/sources/revisions/{revision_id}/evidence:validate")
async def validate_evidence(request: Request, revision_id: str):
    """AI3-T02 `evidence.show`/validator: resolve and validate `EvidenceRef`s
    (typed locator + quote/raw_value, no client-supplied path/URL) against
    one already-committed, ACL-scoped source revision's real observations.
    Read-only; never persists or mutates a claim's `citation_valid`/support."""
    if not _origin_ok(request):                       # a read-only POST is still a cookie-authenticated POST: same Origin / custom-header gate as every other POST
        return _error("origin refused", 403)
    body = await _bounded_json(request) or {}
    workspace_id, refs = body.get("workspace_id"), body.get("refs")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    if not isinstance(refs, list) or not all(isinstance(r, dict) for r in refs):
        return _error("refs must be a list of objects", 400)
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "evidence.read")      # source CONTENT follows the same policy as rule evidence
        reports = store.validate_evidence(authority["tenant_id"], workspace_id,
                                          revision_id, refs)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("source revision not found", 404)
    return _ok({"revision_id": revision_id, "reports": reports})


@router.get("/v2/sources/revisions/{revision_id}/artifacts/{artifact_id}/observations")
def list_artifact_observations(request: Request, revision_id: str, artifact_id: str,
                               workspace_id: str):
    """T04-RV1 (REVIEWER round 2): the discovery step a source inspection
    UI needs so a person can find an evidence-clickable observation_id from
    an artifact, instead of already knowing one. Read-only, same
    `project.read` ACL as every sibling read route in this file."""
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        observations = store.list_artifact_observations(authority["tenant_id"], workspace_id,
                                                        revision_id, artifact_id)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("source revision not found", 404)
    return _ok({"revision_id": revision_id, "artifact_id": artifact_id,
               "observations": observations})


@router.get("/v2/sources/revisions/{revision_id}/artifacts/{artifact_id}/export")
def export_artifact(request: Request, revision_id: str, artifact_id: str, workspace_id: str):
    """AI3-T04: read-only, byte-exact re-serve of one committed artifact's
    immutable content, for independent-reader round-trip verification (a
    downloaded `.xlsx` opened by a different parser than the one Ripple
    ingested with). Same `project.read` ACL as every other read route in
    this file. Never writes; never accepts a caller-named path/hash."""
    identity, _ = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    blobs = _blob_port()
    if blobs is None:
        return _error("source storage unavailable", 503)
    _, store = _services(blobs)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "evidence.read")      # source CONTENT follows the same policy as rule evidence
        blob = store.read_artifact_blob(authority["tenant_id"], workspace_id,
                                        revision_id, artifact_id)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("artifact not found", 404)
    except BlobIntegrityError:
        return _error("source storage unavailable", 503)
    return _ok({"artifact_id": blob["artifact_id"], "logical_path": blob["logical_path"],
               "media_type": blob["media_type"], "byte_size": blob["byte_size"],
               "blob_hash": blob["blob_hash"],
               "content_b64": base64.b64encode(blob["content"]).decode("ascii")})


@router.post("/v2/retrieval:query")
async def retrieval_query(request: Request):
    """AI3-T03: scoped lexical/vector/structural retrieval over the CURRENT
    revision's durable index projection. Same `project.read` ACL as every
    other read route in this file; a not-ready index reports its status
    rather than ever answering from a stale or wrong revision."""
    if not _origin_ok(request):                       # a read-only POST is still a cookie-authenticated POST: same Origin / custom-header gate as every other POST
        return _error("origin refused", 403)
    body = await _bounded_json(request) or {}
    workspace_id, query = body.get("workspace_id"), body.get("query")
    top_k = body.get("top_k", 10)
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    if not isinstance(query, str) or not query.strip():
        return _error("query required", 400)
    if type(top_k) is not int or top_k < 1:
        return _error("top_k must be a positive integer", 400)
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "evidence.read")      # source CONTENT follows the same policy as rule evidence
        bundle = store.retrieve(authority["tenant_id"], workspace_id, query, top_k=top_k)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("workspace not found", 404)
    return _ok(bundle)


@router.get("/v2/sources/revisions/{revision_id}/evidence/{observation_id}")
def read_evidence(request: Request, revision_id: str, observation_id: str, workspace_id: str):
    """T02-RV3's direct evidence read/view: given an `observation_id` a
    caller already holds, return its full pinned evidence without requiring
    the whole ref to be resubmitted. Same `project.read` ACL as every other
    read route in this file."""
    identity, store = _services()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "evidence.read")      # source CONTENT follows the same policy as rule evidence
        return _ok(store.read_evidence(authority["tenant_id"], workspace_id,
                                       revision_id, observation_id))
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError:
        return _error("evidence not found", 404)
