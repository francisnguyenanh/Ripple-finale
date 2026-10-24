"""Extension contract ext/1 (FEX-03..08), LOCAL ONLY: routes of the candidate code patch of a committed business change.

GET  /v2/business/changes/{id}/code/candidate                  the read model (candidate, validation, approval, PR intent, mock promotions)      project.read (+ change visibility)
POST /v2/business/changes/{id}/code/candidate                  {repo_id}                      build a candidate on a disposable clone               code.patch, human actor
POST /v2/business/changes/{id}/code/candidate/validate         {candidate_job_id}             run the declared commands on a fresh checkout         code.patch, human actor
POST /v2/business/changes/{id}/code/candidate/decide           {candidate_job_id, action, reason, patch_hash, validation_hash}                   code.approve, distinct person
POST /v2/business/changes/{id}/code/candidate/publish          {candidate_job_id}             with a pull_request grant: code.publish job; else 409 no_publish_grant (intent recorded)   code.approve
POST /v2/business/changes/{id}/code/candidate/promote|verify|rollback  {candidate_job_id, environment_id}   mock target only (RIPPLE_EXTENSION_MOCK_TARGET=1)   code.promote
Nothing here pushes, merges or deploys; the Final routes (`.../code`, `.../code/apply` refused) are unchanged.
"""
from __future__ import annotations

from infra.db_offload import db_route
from infra.v2_policy_routes import _json_body_sync
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters import v2_code_change
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, permissions_for, require, require_human_actor
from infra.adapters.v2_postgres import StaleVersion, StoreConflict, TenantScopeError
from infra.v2_business_routes import _change_record_for, _denied
from infra.v2_policy_routes import _authority, _error, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _shell(request, workspace_id, change_id, permission, action, *, mutating=True):
    if mutating and not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    if not (isinstance(workspace_id, str) and workspace_id.strip()) or not str(change_id).startswith("chg-"):
        return _error("workspace_id and a change id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        if mutating:
            require_human_actor(authority)
        require(authority, permission)
        tenant = authority["tenant_id"]
        _rec, perms = _change_record_for(store, tenant, workspace_id, change_id, authority)
        return JSONResponse(action(store, tenant, authority, perms), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_code_change.CodeChangeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)
    except (StaleVersion, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.get("/v2/business/changes/{change_id}/code/candidate")
def read_code_candidate(request: Request, change_id: str, workspace_id: str, candidate_job_id: str | None = None, repo_id: str | None = None):
    def read(store, tenant, authority, perms):
        out = v2_code_change.state(store, tenant, workspace_id, change_id, candidate_job_id=candidate_job_id, can_read_evidence="evidence.read" in perms)
        out["can"] = v2_code_change.action_hints(store, tenant, workspace_id, change_id, authority=authority, perms=perms, state_out=out, repo_id=repo_id)   # additive hint, not a grant
        return out
    return _shell(request, workspace_id, change_id, "project.read", read, mutating=False)


@router.post("/v2/business/changes/{change_id}/code/candidate")
@db_route
def create_code_candidate(request: Request, change_id: str):
    body = _json_body_sync(request)
    repo_id = body.get("repo_id")
    if not isinstance(repo_id, str) or not repo_id:
        return _error("repo_id required", 400)
    def action(store, tenant, authority, perms):
        if not v2_code_change.repo_visible(body["workspace_id"], repo_id, authority.get("roles")):     # a hidden repository is refused exactly like an undeclared one
            raise v2_code_change.CodeChangeRefused("repo_not_declared", "no declared repository with that id for this workspace")
        return v2_code_change.request_candidate(store, tenant, body["workspace_id"], change_id, repo_id=repo_id, actor_id=authority["account_id"])
    return _shell(request, body.get("workspace_id"), change_id, "code.patch", action)


@router.post("/v2/business/changes/{change_id}/code/candidate/validate")
@db_route
def validate_code_candidate(request: Request, change_id: str):
    body = _json_body_sync(request)
    cid = body.get("candidate_job_id")
    if not isinstance(cid, str) or not cid:
        return _error("candidate_job_id required", 400)
    return _shell(request, body.get("workspace_id"), change_id, "code.patch",
                  lambda store, tenant, authority, perms: v2_code_change.request_validation(store, tenant, body["workspace_id"], change_id, candidate_job_id=cid, actor_id=authority["account_id"]))


@router.post("/v2/business/changes/{change_id}/code/candidate/decide")
@db_route
def decide_code_candidate(request: Request, change_id: str):
    body = _json_body_sync(request)
    cid = body.get("candidate_job_id")
    if not isinstance(cid, str) or not cid or not isinstance(body.get("patch_hash"), str) or not isinstance(body.get("validation_hash"), str):
        return _error("candidate_job_id, patch_hash and validation_hash required", 400)
    return _shell(request, body.get("workspace_id"), change_id, "code.approve",
                  lambda store, tenant, authority, perms: v2_code_change.decide(store, tenant, body["workspace_id"], change_id, candidate_job_id=cid, actor_id=authority["account_id"],
                                                                                action=body.get("action"), reason=body.get("reason"), patch_hash=body["patch_hash"], validation_hash=body["validation_hash"]))


@router.post("/v2/business/changes/{change_id}/code/candidate/publish")
@db_route
def publish_code_candidate(request: Request, change_id: str):
    body = _json_body_sync(request)
    cid = body.get("candidate_job_id")
    if not isinstance(cid, str) or not cid:
        return _error("candidate_job_id required", 400)

    def action(store, tenant, authority, perms):
        if v2_code_change.publish_grant(store, tenant, body["workspace_id"], change_id, cid):          # FEX-05: the repository declares a pull_request grant -> one publish job
            return v2_code_change.request_publish(store, tenant, body["workspace_id"], change_id, candidate_job_id=cid, actor_id=authority["account_id"])
        intent = v2_code_change.publish(store, tenant, body["workspace_id"], change_id, candidate_job_id=cid, actor_id=authority["account_id"])
        raise v2_code_change.CodeChangeRefused("no_publish_grant", "Nothing is published by this build: no push, pull request or merge grant exists. The intent was recorded. " + str({"branch": intent["branch"], "title": intent["title"]}))
    return _shell(request, body.get("workspace_id"), change_id, "code.approve", action)


def _mock(kind):
    async def route(request: Request, change_id: str):
        body = await _json_body(request)
        cid, env_id = body.get("candidate_job_id"), body.get("environment_id")
        if not isinstance(cid, str) or not cid or not isinstance(env_id, str) or not env_id:
            return _error("candidate_job_id and environment_id required", 400)
        return _shell(request, body.get("workspace_id"), change_id, "code.promote",
                      lambda store, tenant, authority, perms: v2_code_change.request_mock(store, tenant, body["workspace_id"], change_id, candidate_job_id=cid, environment_id=env_id,
                                                                                          actor_id=authority["account_id"], kind=kind))
    return route


router.add_api_route("/v2/business/changes/{change_id}/code/candidate/promote", _mock("code.promote"), methods=["POST"])
router.add_api_route("/v2/business/changes/{change_id}/code/candidate/verify", _mock("code.verify"), methods=["POST"])
router.add_api_route("/v2/business/changes/{change_id}/code/candidate/rollback", _mock("code.rollback"), methods=["POST"])


@router.post("/v2/sources/git/sync")
@db_route
def git_sync(request: Request):
    """FEX-01 (local only): read a declared LOCAL repository at its current commit into one source refresh revision through the existing refresh path. `source.import`, human
    actor. The same commit against the same source revision is one job; the result names the commit, the history shape and the files added/changed/deleted/skipped."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id, repo_id = body.get("workspace_id"), body.get("repo_id")
    if not (isinstance(workspace_id, str) and workspace_id.strip()) or not isinstance(repo_id, str) or not repo_id:
        return _error("workspace_id and repo_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "source.import")
        tenant = authority["tenant_id"]
        from infra.worker import code_change as cc, git_sync as gs
        from infra.worker.execution_types import NonRetryableExecutionError
        from infra.worker.verification import ensure_job
        from infra.worker import git_remote
        try:
            if not v2_code_change.repo_visible(workspace_id, repo_id, authority.get("roles")):         # hidden = undeclared for this member (no existence leak)
                raise NonRetryableExecutionError("repo_not_declared", f"no declared repository {repo_id!r} for this workspace")
            repo = cc.repo_config(workspace_id, repo_id)
            if git_remote.has_remote(repo):                                                    # FEX-01: a declared remote is fetched by a job (no network call in the request)
                if not git_remote.has_grant(repo, "read"):
                    raise NonRetryableExecutionError("no_read_grant", "the repository's remote has no read grant")
                head = store.head(tenant, workspace_id)
                import datetime
                bucket = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M")         # repeated requests within a minute are one fetch
                fid = git_remote.fetch_job_id(repo_id, head.source_revision_id, bucket)
                created = ensure_job(store, tenant, workspace_id, fid, git_remote.FETCH_KIND, {"repo_id": repo_id, "requested_by": authority["account_id"]})
                return JSONResponse({"job_id": fid, "kind": git_remote.FETCH_KIND, "created": created, "status": v2_code_change._state(store.job_record(tenant, workspace_id, fid))},
                                    headers=_NO_STORE)
            sha = gs.head_sha(repo["path"])
        except NonRetryableExecutionError as exc:
            return JSONResponse({"error": str(exc), "code": getattr(exc, "reason", "repo_not_declared")}, status_code=409, headers=_NO_STORE)
        head = store.head(tenant, workspace_id)
        jid = gs.job_id_for(repo_id, sha, head.source_revision_id)
        created = ensure_job(store, tenant, workspace_id, jid, gs.KIND, {"repo_id": repo_id, "requested_by": authority["account_id"], "sha": sha})
        rec = store.job_record(tenant, workspace_id, jid)
        return JSONResponse({"job_id": jid, "kind": gs.KIND, "created": created, "sha": sha, "status": v2_code_change._state(rec)}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/sources/repositories")
def list_repositories(request: Request, workspace_id: str):
    """The declared repositories this member may see (ids, display name, local/remote, declared grants, whether a sync is possible): `project.read`.
    Never a path, URL, remote repository name or credential; a repository outside the member's visibility is simply absent."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        return JSONResponse(v2_code_change.repositories(workspace_id, roles=authority.get("roles"), perms=permissions_for(authority.get("roles"))), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
