"""Request and read the advisory memos (SUR-06 `change.risk_memo`, SUR-08 `ma.synthesis_memo`).

A request creates at most one job per (subject, attempt, frozen config + pinned model, board report): the same request again returns the same job and makes no new call;
reading never calls a model. Job states are shown as they are (waiting / running / failed with its reason / done); a failed or unfinished memo is never read as "no risk".
"""
from __future__ import annotations

import hashlib
import json

from core.change_intelligence.v2 import advisory_memo as am
from infra.adapters.v2_postgres import TenantScopeError

_REASON_WORDS = {"authority_revoked": "the person who asked no longer has permission", "budget_exceeded": "the facts were too large to send",
                 "schema_violation": "the model's answer could not be used", "board_report_moved": "the review-board report changed after the request",
                 "board_not_finished": "the review-board run has no final report", "intent_not_confirmed": "the request was not confirmed",
                 "role_model_not_pinned": "no model is configured for this memo"}


def job_id(kind: str, subject: str, attempt: int, board_report_hash: str | None = None) -> str:
    from infra.worker import job_binding
    prefix = {"change.risk_memo": "rmm-", "ma.synthesis_memo": "msm-"}[kind]
    return prefix + hashlib.sha256(json.dumps([subject, attempt, board_report_hash, job_binding.fingerprint(kind)]).encode("utf-8")).hexdigest()[:24]


def _record(store, tenant, ws, jid):
    try:
        return store.job_record(tenant, ws, jid)
    except TenantScopeError:
        return None


def request(store, tenant, ws, kind: str, jid: str, payload: dict, authority: dict, *, quota, render=None) -> dict:
    rec = _record(store, tenant, ws, jid)
    if rec is None:
        from infra import ai_admission, provider_gate
        provider_gate.require_provider(kind)
        limit, reservation = quota(kind)
        head = store.head(tenant, ws)
        store.create_job(tenant, ws, jid, kind, payload, expected_version=head.version, idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit,
                         budget_reservation=reservation, requested_by=authority["account_id"])
        ai_admission.admit(store, tenant, ws, jid, kind, authority)
        rec = store.job_record(tenant, ws, jid)
    return (render or state_of)(rec)


def state_of(rec) -> dict:
    st = rec.get("execution_status")
    status = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled"}.get(st)
    if status is None:
        status = "waiting_for_approval" if rec.get("status") == "PENDING_APPROVAL" else "running"
    out = {"job_id": rec.get("job_id"), "attempt": (rec.get("payload") or {}).get("attempt"), "status": status, "label": am.LABEL}
    if status in ("failed", "cancelled"):
        code = rec.get("execution_reason") or ""
        key = next((k for k in _REASON_WORDS if code.startswith(k)), None)
        out["reason"] = "The memo was not written" + (f": {_REASON_WORDS[key]}" if key else "") + ". Nothing is concluded about risk from a missing memo."
        out["reason_code"] = key or "other"
    elif status == "done" and rec.get("execution_result"):
        res = rec["execution_result"]
        out["memo"] = am.render_memo(res)
        out["binding"] = {k: res.get(k) for k in ("intent_id", "report_hash", "board_run_id", "board_report_hash", "run_id", "change_id", "board_status", "config_sha256") if k in res}
        out["model"] = res.get("model")
        out["ai_outcome"] = rec.get("execution_result", {}).get("ai_outcome")
    else:
        out["reason"] = "The memo is being written; nothing is concluded yet."
    return out


def latest(store, tenant, ws, kind: str, subject: str, board_report_hash: str | None) -> dict:
    """The memo of the highest attempt that exists for this subject and board report (reads only)."""
    for attempt in (3, 2, 1):
        rec = _record(store, tenant, ws, job_id(kind, subject, attempt, board_report_hash))
        if rec is not None:
            return state_of(rec)
    return {"status": "not_requested", "label": am.LABEL, "reason": "No memo was requested. The impact report and the review findings stand on their own."}
