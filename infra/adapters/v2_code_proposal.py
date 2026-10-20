"""Request and read the SUR-03 code proposals (`code.diff_proposal`, proposal-only) of a confirmed change.

One job per (change, repository, base commit, recommendation hash, attempt, frozen config + pinned model); the same request again makes no new call; a read never calls a model.
A proposal is shown with its static checks and the statement that nothing was applied or run.
"""
from __future__ import annotations

import hashlib
import json

from core.change_intelligence.v2 import code_proposal as cp
from infra.adapters import v2_advisory_memo
from infra.adapters.v2_postgres import TenantScopeError

KIND = "code.diff_proposal"
_REASON_WORDS = {"authority_revoked": "the person who asked no longer has permission", "budget_exceeded": "the files were too large to send",
                 "schema_violation": "the model's answer could not be used", "stale_binding": "the recommendations changed after the request",
                 "change_not_confirmed": "the change is not confirmed", "repo_not_declared": "the repository is no longer declared", "git_failed": "a file could not be read",
                 "role_model_not_pinned": "no model is configured"}


def job_id(change_id: str, repo_id: str, base_sha: str, recommendation_hash: str, attempt: int) -> str:
    from infra.worker import job_binding
    return "cdp-" + hashlib.sha256(json.dumps([change_id, repo_id, base_sha, recommendation_hash, attempt, job_binding.fingerprint(KIND)]).encode("utf-8")).hexdigest()[:24]


def state_of(rec) -> dict:
    st = rec.get("execution_status")
    status = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled"}.get(st)
    if status is None:
        status = "waiting_for_approval" if rec.get("status") == "PENDING_APPROVAL" else "running"
    p = rec.get("payload") or {}
    out = {"job_id": rec.get("job_id"), "attempt": p.get("attempt"), "repo_id": p.get("repo_id"), "base_sha": p.get("base_sha"), "status": status, "label": cp.LABEL,
           "applied": False, "executed": False}
    if status in ("failed", "cancelled"):
        code = rec.get("execution_reason") or ""
        key = next((k for k in _REASON_WORDS if code.startswith(k)), None)
        out.update(reason="No proposal was written" + (f": {_REASON_WORDS[key]}" if key else "") + ".", reason_code=key or "other")
    elif status == "done" and rec.get("execution_result"):
        res = rec["execution_result"]
        out.update({k: res[k] for k in ("model_status", "explanation", "files", "diff", "patch_hash", "kept_edits", "rejected", "suggested_tests", "notes",
                                        "static_checks_passed", "coverage", "recommendation_hash")}, model=res.get("model"), ai_outcome=res.get("ai_outcome"))
    else:
        out["reason"] = "The proposal is being written."
    return out


def latest(store, tenant, ws, change_id, repo_id, base_sha, recommendation_hash) -> dict:
    for attempt in (3, 2, 1):
        try:
            return state_of(store.job_record(tenant, ws, job_id(change_id, repo_id, base_sha, recommendation_hash, attempt)))
        except TenantScopeError:
            continue
    return {"status": "not_requested", "label": cp.LABEL, "applied": False, "executed": False,
            "reason": "No proposal was requested for this base commit and these recommendations."}


def request(store, tenant, ws, jid, payload, authority, *, quota) -> dict:
    return v2_advisory_memo.request(store, tenant, ws, KIND, jid, payload, authority, quota=quota, render=state_of)
