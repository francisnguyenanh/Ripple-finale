"""Request and read the SUR-05 proposed correspondence (`source.correspondence`) of a source refresh.

One job per (refresh, refresh digest, attempt, frozen config + pinned model); the same request again makes no new call; a read never calls a model. Proposals are shown as
PROPOSED with the server's facts (bytes identical, which proof the refresh would accept); they are never a rename proof and change nothing.
"""
from __future__ import annotations

import hashlib
import json

from core.change_intelligence.v2 import source_correspondence as sc
from infra.adapters import v2_advisory_memo
from infra.adapters.v2_postgres import TenantScopeError

KIND = "source.correspondence"
_REASON_WORDS = {"authority_revoked": "the person who asked no longer has permission", "budget_exceeded": "the excerpts were too large to send",
                 "schema_violation": "the model's answer could not be used", "refresh_moved": "the refresh is not the one requested",
                 "refresh_not_open": "the refresh is no longer open", "role_model_not_pinned": "no model is configured"}


def job_id(refresh_id: str, refresh_digest: str, attempt: int) -> str:
    from infra.worker import job_binding
    return "scr-" + hashlib.sha256(json.dumps([refresh_id, refresh_digest, attempt, job_binding.fingerprint(KIND)]).encode("utf-8")).hexdigest()[:24]


def state_of(rec) -> dict:
    st = rec.get("execution_status")
    status = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled"}.get(st)
    if status is None:
        status = "waiting_for_approval" if rec.get("status") == "PENDING_APPROVAL" else "running"
    p = rec.get("payload") or {}
    out = {"job_id": rec.get("job_id"), "attempt": p.get("attempt"), "refresh_id": p.get("refresh_id"), "status": status, "label": sc.LABEL}
    if status in ("failed", "cancelled"):
        code = rec.get("execution_reason") or ""
        key = next((k for k in _REASON_WORDS if code.startswith(k)), None)
        out.update(reason="No correspondence was proposed" + (f": {_REASON_WORDS[key]}" if key else "") + ". Nothing is concluded about where the content went.",
                   reason_code=key or "other")
    elif status == "done" and rec.get("execution_result"):
        res = rec["execution_result"]
        out.update({k: res[k] for k in ("model_status", "proposals", "rejected", "notes", "unmatched_old", "coverage", "refresh_status_at_run")},
                   model=res.get("model"), ai_outcome=res.get("ai_outcome"))
    else:
        out["reason"] = "The proposal is being written; nothing is concluded yet."
    return out


def latest(store, tenant, ws, refresh_id: str, refresh_digest: str) -> dict:
    for attempt in (3, 2, 1):
        try:
            return state_of(store.job_record(tenant, ws, job_id(refresh_id, refresh_digest, attempt)))
        except TenantScopeError:
            continue
    return {"status": "not_requested", "refresh_id": refresh_id, "label": sc.LABEL,
            "reason": "No correspondence was requested. The refresh's own diff and rename proofs stand on their own."}


def request(store, tenant, ws, jid, payload, authority, *, quota) -> dict:
    return v2_advisory_memo.request(store, tenant, ws, KIND, jid, payload, authority, quota=quota, render=state_of)
