"""U1/U5: what a workspace is, who can do what in it, and an admin-only way to start another one.

* `GET  /v2/workspace/profile?workspace_id=`  - readable name/description, project, file count, what the caller may do, and what each role
  bundle means in plain words. Member names are shown only to a caller who can manage access (`membership.manage`).
* `GET  /v2/workspace/create-options`          - only for a caller who administers a project: the projects they may add a workspace to,
  the accounts that already belong to those projects (the directory; nobody types an arbitrary id) and the grantable role bundles.
* `POST /v2/workspaces`                        - create a workspace in a project the caller administers. The server resolves actor and
  tenant from the session, checks project access, grants the creator `admin` of the NEW workspace only, grants the chosen existing
  accounts roles from the existing grantable bundles, and audits it. It creates no budget, no lease and no account, and a model never
  decides membership or rights. Replaying the same `idempotency_key` returns the same workspace.

Everything here is additive and uses the reviewed identity store and role bundles; it is not an organization/IAM/billing platform.
"""

from __future__ import annotations

import hashlib
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg2.extras import Json

from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import GRANTABLE_ROLES, PermissionDenied, ROLE_PERMISSIONS, permissions_for, require, require_human_actor
from infra.adapters.v2_postgres import TenantScopeError
from infra.v2_policy_routes import (_authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok, _token, _v2_store)

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}

CAPABILITY_WORDS = {
    "project.read": "Read sources, knowledge, decisions and results",
    "evidence.read": "Read the original quotes from source files",
    "source.import": "Add or update source files and start AI analysis",
    "change.prepare": "Prepare a requested change up to a proposal (draft, impact, simulation); it then waits for a reviewer and an approver",
    "knowledge.answer": "Answer open questions",
    "intent.create": "Propose a change",
    "knowledge.confirm": "Confirm knowledge",
    "conflict.resolve": "Resolve conflicts between sources",
    "change.approve": "Approve or reject a proposed change",
    "change.accept_result": "Accept the result of an applied change",
    "verification.contract.set": "Set what a change must achieve",
    "source.restore": "Restore an earlier version of the sources",
    "membership.manage": "Manage who has access to this workspace",
    "ai.use": "Use the AI (chat, drafts, questions, readings) within the workspace limits",
    "intent.confirm_own": "Confirm the understanding of your own request",
    "change.review": "Review a proposal: comment, ask for changes, mark it ready for approval",
    "change.apply": "Start applying an approved change",
}
ROLE_WORDS = {
    "requester": "Requester", "knowledge_owner": "Knowledge owner", "change_approver": "Change approver",
    "restore_operator": "Restore operator", "reviewer": "Reviewer", "ai_suspended": "AI use switched off for this person", "viewer": "Viewer (read only)", "judge": "Judge (state a change, prepare it up to a proposal; no import, extraction or apply)", "admin": "Administrator",
}
# the four steps of the flow, so the page can say who may do which (the server still checks every call)
STEPS = (("read", "Read", ("project.read",)), ("propose", "Propose a change", ("intent.create",)),
         ("confirm", "Confirm knowledge", ("knowledge.confirm",)), ("approve", "Approve and apply", ("change.approve",)))
_NAME = re.compile(r"^[^\x00-\x1f]{3,80}$")


def _denied(exc: IdentityDenied):
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


def _roles_explained() -> list[dict]:
    return [{"role": r, "label": ROLE_WORDS.get(r, r), "grantable": r in GRANTABLE_ROLES,
             "can": [CAPABILITY_WORDS[p] for p in CAPABILITY_WORDS if p in ROLE_PERMISSIONS[r]]} for r in sorted(ROLE_PERMISSIONS)]


@router.get("/v2/workspace/profile")
def workspace_profile(request: Request, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant, project = authority["tenant_id"], authority["project_id"]
        mine = permissions_for(authority.get("roles"))
        with store._tx() as cur:
            cur.execute("""SELECT display_name, description, created_at, created_by, source_revision_id FROM v2_workspaces
                WHERE tenant_id=%s AND workspace_id=%s""", (tenant, workspace_id))
            name, desc, created_at, created_by, rev = cur.fetchone()
            cur.execute("SELECT count(*) FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s",
                        (tenant, workspace_id, rev or ""))
            files = cur.fetchone()[0]
            cur.execute("SELECT workspace_id, display_name FROM v2_workspaces WHERE tenant_id=%s AND project_id=%s ORDER BY workspace_id", (tenant, project))
            siblings = [{"workspace_id": r[0], "display_name": r[1]} for r in cur.fetchall()]
        out = {"workspace_id": workspace_id, "display_name": name, "description": desc, "project_id": project,
               "created_at": created_at.isoformat() if created_at else None, "created_by": created_by,
               "source_files": files, "my_roles": sorted(authority.get("roles") or []),
               "my_capabilities": [CAPABILITY_WORDS[p] for p in CAPABILITY_WORDS if p in mine],
               "steps": [{"id": sid, "label": label, "allowed": any(p in mine for p in perms)} for sid, label, perms in STEPS],
               "roles": _roles_explained(), "projects_workspaces": len(siblings),
               "same_project": [w for w in siblings if w["workspace_id"] != workspace_id]}
        if "membership.manage" in mine:
            with identity._tx() as cur:
                cur.execute("SELECT account_id, roles FROM v2_memberships WHERE tenant_id=%s AND project_id=%s AND workspace_id=%s ORDER BY account_id",
                            (tenant, project, workspace_id))
                out["members"] = [{"account_id": r["account_id"], "roles": list(r["roles"])} for r in cur.fetchall()]
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


def _admin_projects(identity, request: Request) -> dict:
    """{(tenant, project): workspace_id the caller administers there}, from the caller's own memberships (never from the request)."""
    out = {}
    for m in identity.list_workspaces(_token(request)):
        if "membership.manage" in permissions_for(m.get("roles")):
            out.setdefault((m["tenant_id"], m["project_id"]), m["workspace_id"])
    return out


@router.get("/v2/workspace/create-options")
def create_options(request: Request):
    identity = _identity_store()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        admin = _admin_projects(identity, request)
        if not admin:
            return _error("permission denied: you do not administer any project", 403)
        projects = []
        with identity._tx() as cur:
            for (tenant, project), via in sorted(admin.items()):
                cur.execute("""SELECT DISTINCT account_id FROM v2_memberships WHERE tenant_id=%s AND project_id=%s ORDER BY account_id""", (tenant, project))
                accounts = [r["account_id"] for r in cur.fetchall()]
                cur.execute("SELECT workspace_id, display_name FROM v2_workspaces WHERE tenant_id=%s AND project_id=%s ORDER BY workspace_id", (tenant, project))
                projects.append({"project_id": project, "workspaces": [{"workspace_id": r["workspace_id"], "display_name": r["display_name"]} for r in cur.fetchall()],
                                 "accounts": accounts})
        return JSONResponse({"projects": projects, "roles": [r for r in _roles_explained() if r["grantable"]],
                             "note": "A new workspace does not create a budget, a demo session or an account."}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)


@router.post("/v2/workspaces")
async def create_workspace(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    try:
        admin = _admin_projects(identity, request)          # authentication first: an anonymous caller never reaches validation
        project_id, name, desc, key = body.get("project_id"), body.get("name"), body.get("description") or "", body.get("idempotency_key")
        members = body.get("members") or []
        if not (isinstance(project_id, str) and project_id and isinstance(name, str) and isinstance(key, str) and key):
            return _error("project_id, name and idempotency_key are required", 400)
        name = name.strip()
        if not _NAME.match(name):
            return _error("the name must be 3 to 80 characters", 400)
        if not isinstance(desc, str) or len(desc) > 500:
            return _error("the description must be at most 500 characters", 400)
        if not isinstance(members, list) or len(members) > 20 or any(not isinstance(m, dict) for m in members):
            return _error("members must be a list of at most 20 {account_id, roles}", 400)
        scope = next((k for k in admin if k[1] == project_id), None)
        if scope is None:
            raise PermissionDenied("you do not administer this project")
        tenant = scope[0]
        authority = _authority(identity, request, admin[scope])
        require_human_actor(authority)
        require(authority, "membership.manage")
        actor = authority["account_id"]
        wid = "ws-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] + "-" + hashlib.sha256(f"{tenant}|{project_id}|{actor}|{key}".encode()).hexdigest()[:10]
        with identity._tx() as cur:
            cur.execute("SELECT DISTINCT account_id FROM v2_memberships WHERE tenant_id=%s AND project_id=%s", (tenant, project_id))
            directory = {r["account_id"] for r in cur.fetchall()}
            cur.execute("SELECT workspace_id FROM v2_workspaces WHERE tenant_id=%s AND project_id=%s AND lower(display_name)=lower(%s)", (tenant, project_id, name))
            taken = cur.fetchone()
        if taken and taken["workspace_id"] != wid:
            return _error("a workspace with this name already exists in the project", 409)
        grants = []
        for m in members:
            acc, roles = m.get("account_id"), m.get("roles")
            if acc not in directory:
                return _error("members must be accounts that already belong to this project", 400)
            if acc == actor:
                continue
            if not isinstance(roles, list) or not roles or any(r not in GRANTABLE_ROLES for r in roles):
                return _error("roles must be a non-empty list of grantable roles", 400)
            grants.append((acc, tuple(roles)))
        store.create_workspace(tenant, project_id, wid)
        with identity._tx() as cur:
            cur.execute("""UPDATE v2_workspaces SET display_name=%s, description=%s, created_by=COALESCE(created_by, %s)
                WHERE tenant_id=%s AND workspace_id=%s""", (name, desc.strip(), actor, tenant, wid))
            identity._grant(cur, tenant, project_id, wid, actor, ("admin",))
            for acc, roles in grants:
                identity._grant(cur, tenant, project_id, wid, acc, roles)
            cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'workspace.created',%s,%s)""",
                        (tenant, wid, actor, Json({"project_id": project_id, "display_name": name, "members": [a for a, _ in grants]})))
        return JSONResponse({"workspace_id": wid, "display_name": name, "project_id": project_id, "creator_roles": ["admin"],
                             "members": [{"account_id": a, "roles": sorted(r)} for a, r in grants]}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
