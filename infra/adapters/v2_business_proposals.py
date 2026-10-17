"""Read-time projection of the model-suggested gaps and relations of one snapshot (KNO-06). Pure reads over the job table; nothing here writes.

Suggestions live on the completed `knowledge.gap_proposals` job of the snapshot people are working on. The gap queue shows the open ones labelled; the graph shows the
proposed edges; the answer path treats an open suggested gap like any other gap of the snapshot (once answered it is stored in the next snapshot by the existing merge).
When the snapshot moves the suggestions of the old one are no longer shown: a new knowledge version needs a new request, which is the intended revalidation.
"""
from __future__ import annotations

from infra.adapters.v2_postgres import TenantScopeError
from infra.worker.knowledge_gap_proposals import job_id_for

REASON_WORDS = {"authority_revoked": "the person who asked no longer has permission", "budget_exceeded": "the rules were too large to send",
                "schema_violation": "the answer could not be used", "snapshot_moved": "the knowledge changed after the request", "role_model_not_pinned": "no model is set for this step"}


def _state(rec) -> str:
    st = rec.get("execution_status")
    return {"COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "failed"}.get(st) or ("waiting_for_approval" if rec.get("status") == "PENDING_APPROVAL" else "running")


def latest_job(store, tenant_id, workspace_id, snapshot_id):
    """The newest proposal job of this snapshot (attempt 3 first), or None."""
    for attempt in (3, 2, 1):
        try:
            return store.job_record(tenant_id, workspace_id, job_id_for(snapshot_id, attempt))
        except TenantScopeError:
            continue
    return None


def proposals_for(store, tenant_id, workspace_id, snap) -> dict:
    """`{status, gaps, relations, coverage, reason, job_id}` for the snapshot view `snap` (None -> not_requested). Gaps already in the snapshot are left out."""
    none = {"status": "not_requested", "gaps": [], "relations": [], "coverage": None, "reason": None, "job_id": None,
            "statement": "No model suggestions were requested for this knowledge version."}
    if snap is None:
        return none
    rec = latest_job(store, tenant_id, workspace_id, snap["snapshot"]["snapshot_id"])
    if rec is None:
        return none
    status = _state(rec)
    out = dict(none, status=status, job_id=rec.get("job_id"))
    if status == "completed" and rec.get("execution_result"):
        res = rec["execution_result"]
        in_snapshot = {g["gap_id"] for g in snap["gaps"]}
        known = {e["edge_id"] for e in snap["relations"]}
        out["gaps"] = [dict(g, origin="model_proposal") for g in res["gaps"] if g["gap_id"] not in in_snapshot]
        out["relations"] = [dict(e, origin="model_proposal") for e in res["relations"] if e["edge_id"] not in known]
        out["coverage"] = {"model_status": res["model_status"], "rules_shown": res["rules_shown"], "rules_not_shown": res["rules_not_shown"], "rejected": len(res["rejected"]),
                           "notes": res["notes"], "complete": res["rules_not_shown"] == 0 and res["model_status"] != "UNSURE"}
        out["statement"] = ("A model looked at " + str(res["rules_shown"]) + " rule(s) and suggested " + str(len(res["gaps"])) + " question(s) and " + str(len(res["relations"])) +
                            " relation(s); " + (str(res["rules_not_shown"]) + " rule(s) were not shown, so their gaps are unknown. " if res["rules_not_shown"] else "") +
                            "Suggestions are not facts: a person answers or dismisses each one. Finding nothing is not proof that nothing is missing.")
    elif status == "failed":
        key = next((k for k in REASON_WORDS if (rec.get("execution_reason") or "").startswith(k)), None)
        out["reason"] = "The model suggestions did not finish" + (f": {REASON_WORDS[key]}" if key else "") + ". Gaps beyond the stored analysis are unknown, not absent."
        out["statement"] = out["reason"]
    else:
        out["statement"] = "The model suggestions are still being prepared."
    return out
