"""Request and read the SUR-04 semantic drift advisory (`ci.semantic_drift`) over a declared repository.

One job per (repository, base SHA, head SHA, accepted snapshot, attempt, frozen config + pinned model); the same request again returns the same job and makes no new call; a
read never calls a model. The exact MON-CI result and the advisory findings are shown side by side and never merged; a failed or unfinished advisory concludes nothing.
"""
from __future__ import annotations

import hashlib
import json

from core.change_intelligence.v2 import semantic_drift as sd
from infra.adapters import v2_advisory_memo

KIND = "ci.semantic_drift"
_REASON_WORDS = {"authority_revoked": "the person who asked no longer has permission", "budget_exceeded": "the diff was too large to send",
                 "schema_violation": "the model's answer could not be used", "policy_moved": "the accepted knowledge changed after the request",
                 "repo_not_declared": "the repository is no longer declared", "git_failed": "a commit could not be read", "role_model_not_pinned": "no model is configured"}


def job_id(repo_id: str, base_sha: str, head_sha: str, snapshot_id: str, attempt: int) -> str:
    from infra.worker import job_binding
    return "csd-" + hashlib.sha256(json.dumps([repo_id, base_sha, head_sha, snapshot_id, attempt, job_binding.fingerprint(KIND)]).encode("utf-8")).hexdigest()[:24]


def state_of(rec) -> dict:
    st = rec.get("execution_status")
    status = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled"}.get(st)
    if status is None:
        status = "waiting_for_approval" if rec.get("status") == "PENDING_APPROVAL" else "running"
    p = rec.get("payload") or {}
    out = {"job_id": rec.get("job_id"), "attempt": p.get("attempt"), "repo_id": p.get("repo_id"), "base_sha": p.get("base_sha"), "head_sha": p.get("head_sha"),
           "status": status, "label": sd.LABEL, "never_a_pass": True}
    if status in ("failed", "cancelled"):
        code = rec.get("execution_reason") or ""
        key = next((k for k in _REASON_WORDS if code.startswith(k)), None)
        out.update(reason="The advisory did not finish" + (f": {_REASON_WORDS[key]}" if key else "") + ". Nothing is concluded about drift.", reason_code=key or "other")
    elif status == "done" and rec.get("execution_result"):
        res = rec["execution_result"]
        exact = dict(res["exact"])
        exact.pop("report", None)                                                                  # the full exact report stays on the job; the summary is shown
        out.update(exact=exact, advisory={"model_status": res["model_status"], "findings": res["semantic"]["kept"], "rejected": res["semantic"]["rejected"],
                                          "notes": res["semantic"]["notes"], "conclusion": res["semantic"]["conclusion"]},
                   coverage=res["coverage"], snapshot_id=res["snapshot_id"], model=res.get("model"), ai_outcome=res.get("ai_outcome"))
    else:
        out["reason"] = "The advisory is running; nothing is concluded yet."
    return out


def request(store, tenant, ws, jid, payload, authority, *, quota) -> dict:
    return v2_advisory_memo.request(store, tenant, ws, KIND, jid, payload, authority, quota=quota, render=state_of)
