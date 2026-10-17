"""B1: bind a proposal to a COMPLETED, still-current `change.draft` job. The proposal's typed edits are the server's own
materialized `proposal_edit`s from the draft (current value from the stored observation, proposed value from the
confirmed intent); the caller only chooses WHICH proposed edits to accept (`accepted_refs`) and the target artifact.
Anything the draft left manual/ambiguous/unaddressed is carried as `left_for_humans`, never silently dropped."""
from __future__ import annotations

import hashlib
import json

from infra.worker.execution_types import ExecutionContext, NonRetryableExecutionError


def _hash(result: dict) -> str:
    body = {k: v for k, v in result.items() if k != "draft_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def resolve_draft_edits(ctx: ExecutionContext, payload: dict) -> dict:
    job = ctx.job
    tenant, ws = job["tenant_id"], job["workspace_id"]
    try:
        rec = ctx.store.job_record(tenant, ws, payload["draft_job_id"])
    except Exception as exc:
        raise NonRetryableExecutionError("draft_not_found") from exc
    res = rec.get("execution_result") or {}
    if rec.get("kind") != "change.draft" or rec.get("execution_status") != "COMPLETED":
        raise NonRetryableExecutionError("draft_not_completed")
    if res.get("draft_hash") != _hash(res):
        raise NonRetryableExecutionError("draft_hash_mismatch")
    pin = res.get("pinned_input") or {}
    if pin.get("pinned_source_revision_id") != job.get("pinned_source_revision_id"):
        raise NonRetryableExecutionError("draft_stale", "source revision moved since the draft was produced")
    if pin.get("pinned_decision_epoch") != job.get("pinned_decision_epoch"):
        raise NonRetryableExecutionError(
            "draft_stale", "a decision was recorded after this draft (decision epoch moved); generate a new draft")
    if (res.get("intent") or {}).get("intent_id") != payload.get("intent_id"):
        raise NonRetryableExecutionError("draft_intent_mismatch")
    target = payload.get("target_artifact_id")
    proposed = [e for e in res.get("edits") or [] if e.get("status") == "PROPOSED" and e.get("kind") == "yaml_edit"
                and e.get("artifact_id") == target]
    if not proposed:
        raise NonRetryableExecutionError("draft_has_no_proposed_edit_for_target")
    accepted = payload.get("accepted_refs")
    if accepted is not None:
        unknown = [a for a in accepted if a not in {e["ref"] for e in proposed}]
        if unknown:
            raise NonRetryableExecutionError("accepted_ref_not_a_proposed_edit", ",".join(unknown))
        proposed = [e for e in proposed if e["ref"] in accepted]
    left = [{"ref": x["ref"], "logical_path": x["logical_path"], "status": x["status"], "reason": x.get("reason"),
             **({"disposition": x.get("disposition"), "carried_from": x.get("carried_from"), "disposition_basis": x.get("disposition_basis")} if (x.get("carried_from") or x.get("disposition") not in (None, "unresolved")) else {})}
            for x in res.get("items") or [] if x.get("status") in ("MANUAL_CHANGE", "AMBIGUITY", "UNSUPPORTED")]
    left += [{"ref": u["ref"], "logical_path": u.get("logical_path"), "status": "UNADDRESSED",
              **({"disposition": u.get("disposition"), "carried_from": u.get("carried_from"), "disposition_basis": u.get("disposition_basis")} if (u.get("carried_from") or u.get("disposition") not in (None, "unresolved")) else {})}
             for u in res.get("unaddressed") or []]
    return {"edits": [dict(e["proposal_edit"]) for e in proposed], "draft_job_id": payload["draft_job_id"],
            "draft_hash": res["draft_hash"], "accepted_refs": [e["ref"] for e in proposed],
            "citations": [e["citation"] for e in proposed], "left_for_humans": left}
