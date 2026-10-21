"""Why an AI run is not running yet, who may allow it, and whether THIS reader may (F-ACC-2, 2026-10-21). Read-only: it states the rules the action routes already enforce
and decides nothing.

The rules are exactly those of `POST /v2/jobs/{job_id}/approve` and the sandbox write guard: the person must be able to read the job (`v2_job_visibility.readable`), hold the
kind's approve permission (`JOB_APPROVE_PERMISSION`), not be the person who asked when the workspace requires a distinct approver, and, in a sandbox workspace, be on the
active writing lease. The approve route re-checks every one of them when the person acts; this module only explains them. No account names of possible approvers are
listed (only the roles that carry the permission), so nothing about other members leaks.
"""
from __future__ import annotations

from infra import v2_job_visibility
from infra.adapters.v2_policy import JOB_APPROVE_PERMISSION, ROLE_PERMISSIONS, permissions_for

WHERE = "Questions & Decisions: \"AI runs waiting for a second person\""
TERMINAL = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled"}


def roles_with(permission: str) -> list[str]:
    return sorted(r for r, perms in ROLE_PERMISSIONS.items() if permission in perms)


def _sandbox_may_write(store, tenant: str, ws: str, account: str) -> tuple[bool, bool]:
    """(is a sandbox workspace, the reader is on its active lease)."""
    from infra import v2_sandbox
    cfg = v2_sandbox.config()
    if not cfg or ws not in cfg:
        return False, True
    with store._tx() as cur:
        lease = v2_sandbox._lease(cur, tenant, ws)
    return True, v2_sandbox._may_write(lease, account)


def describe(store, tenant: str, ws: str, rec: dict, authority: dict) -> dict:
    """{state, ...}: `done` / `failed` / `cancelled`; `running`; `waiting_for_slot` (allowed, queued for a worker); or `waiting_for_approval` with the rule and this
    reader's position (`you_may_allow`, `why_not`: requested_by_you | no_permission | not_readable | lease_required | unsupported_kind)."""
    ex, st = rec.get("execution_status"), rec.get("status")
    if ex in TERMINAL:
        return {"state": TERMINAL[ex]}
    if st != "PENDING_APPROVAL":
        return {"state": "running" if ex == "RUNNING" else "waiting_for_slot"}
    permission = JOB_APPROVE_PERMISSION.get(rec.get("kind"))
    distinct = bool(store.workspace_policy(tenant, ws)["require_distinct_approver"])
    sandbox, on_lease = _sandbox_may_write(store, tenant, ws, authority["account_id"])
    own = rec.get("requested_by") == authority["account_id"]
    why = None
    if permission is None:
        why = "unsupported_kind"
    elif not v2_job_visibility.readable(store, tenant, ws, rec, authority):
        why = "not_readable"
    elif permission not in permissions_for(authority.get("roles")):
        why = "no_permission"
    elif distinct and own:
        why = "requested_by_you"
    elif not on_lease:
        why = "lease_required"
    return {"state": "waiting_for_approval", "reason": "sandbox_second_person" if sandbox else "approval_required",
            "permission": permission, "allowed_roles": roles_with(permission) if permission else [], "distinct_required": distinct,
            "requested_by_you": own, "you_may_allow": why is None, "why_not": why, "where": WHERE, "job_id": rec.get("job_id")}


def awaiting(store, tenant: str, ws: str, authority: dict, limit: int = 50) -> list[dict]:
    """Runs of this workspace waiting for approval that THIS reader may allow now (same rules as `describe`), newest first. A short label only: the kind and, for a
    business question or change request, its text (both are readable to the reader by the job-visibility rule above)."""
    with store._tx() as cur:
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND status='PENDING_APPROVAL' AND execution_status='QUEUED' "
                    "ORDER BY job_id DESC LIMIT %s", (tenant, ws, int(limit)))
        ids = [r[0] for r in cur.fetchall()]
    out = []
    for job_id in ids:
        rec = store.job_record(tenant, ws, job_id)
        hold = describe(store, tenant, ws, rec, authority)
        if not hold.get("you_may_allow"):
            continue
        payload = rec.get("payload") or {}
        text = payload.get("question") or payload.get("request") or payload.get("text")
        out.append({"job_id": job_id, "kind": rec.get("kind"), "requested_by": rec.get("requested_by"),
                    "label": str(text)[:200] if isinstance(text, str) else None, "permission": hold["permission"]})
    return out
