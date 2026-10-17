"""SYN-03: the person's choice between the automatic file edit and a manual edit for a committed business change, and the state of that choice.

`mode = "ai"` creates (or finds) ONE deterministic `business.source_apply` job for the approved edits (`edits_hash`); the worker writes the new source revision
(see `infra/worker/business_source_apply.py`). `mode = "manual"` records nothing and returns the instructions a person follows; the existing source refresh and the
carry-over (SYN-02) read the files back afterwards. Reads only here, except the job row. Source edits are never required for the knowledge loop to be complete."""
from __future__ import annotations

from core.change_intelligence.v2 import business_draft as bd
from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters import v2_business_commit, v2_business_review
from infra.adapters.v2_business import BusinessKnowledgeRefused
from infra.adapters.v2_policy import PermissionDenied, permissions_for
from infra.adapters.v2_postgres import StaleVersion, TenantScopeError

KIND = "business.source_apply"
MODES = ("ai", "manual")
APPLY_PERMISSION = "change.apply"


def apply_job_id(change_id: str, edits_hash: str) -> str:
    return "srcapply-" + canonical_hash({"change": change_id, "edits": edits_hash})[:20]


def _job(store, tenant_id, workspace_id, job_id):
    try:
        rec = store.job_record(tenant_id, workspace_id, job_id)
    except TenantScopeError:
        return None
    return rec if rec.get("kind") == KIND else None


def _job_view(rec, can_read_evidence: bool):
    if rec is None:
        return None
    res = rec.get("execution_result") or {}
    out = {"job_id": rec["job_id"], "status": rec.get("execution_status"), "reason": rec.get("execution_reason"), "published": res.get("source_revision_id") is not None, "statement": res.get("statement")}
    if can_read_evidence:                                                         # ids, paths and hashes need evidence rights (business mode shows none)
        out.update(source_revision_id=res.get("source_revision_id"), readback=res.get("readback"), files=res.get("files"), edits=res.get("edits"), left_for_humans=res.get("left_for_humans"))
    return out


def manual_items(draft, can_read_evidence: bool) -> list:
    out = []
    for i in draft["file_actions"]:
        if i["role"] != "to_change" or i["action"] in ("NO_OP", "AI_EDIT_SUPPORTED"):
            continue                                                              # the automatic edit is not a manual item; a held edit (MANUAL by guidance) is
        row = {"ref": i["ref"], "action": i["action"], "reason": i["reason"], "kind": i["kind"]}
        if can_read_evidence:
            row.update(rule_ids=i["rule_ids"], path=i["path"], locator=i["locator"], instruction=i["instruction"], held_edit=i.get("held_edit"))
        out.append(row)
    return out


def source_state(store, tenant_id, workspace_id, change_id, draft, committed, *, perms) -> dict:
    """What the review page shows about the source files of this change: the choice available, the job if one exists, the manual list."""
    eh = bd.edits_hash(draft)
    can_read = "evidence.read" in perms
    rec = _job(store, tenant_id, workspace_id, apply_job_id(change_id, eh)) if eh else None
    approval = None
    if eh:
        from infra.worker.business_source_apply import approved_file_writes
        approval = approved_file_writes(store, tenant_id, workspace_id, change_id, draft)
    return {"edits_hash": eh, "automatic_edit_available": eh is not None, "file_writes_approved": approval is not None, "knowledge_committed": committed is not None,
            "job": _job_view(rec, can_read), "manual": manual_items(draft, can_read), "modes": list(MODES),
            "statement": "A person chooses: the approved scalar edits are written by the product into a new source revision, or a person edits the files and refreshes the sources. Neither is required for the knowledge change to count."}


def choose(store, tenant_id, workspace_id, change_id, *, actor_id: str, roles, mode: str, seen: dict) -> dict:
    """The person's choice. `ai` needs `change.apply`, a current `file_writes` approval, the committed knowledge and the binding the person saw; it creates one job per edits_hash.
    `manual` needs only the right to see the review and returns the instructions."""
    if mode not in MODES:
        raise BusinessKnowledgeRefused("invalid_field", "mode must be ai or manual")
    perms = permissions_for(roles)
    got = v2_business_review.current_draft(store, tenant_id, workspace_id, change_id)
    if got is None:
        raise BusinessKnowledgeRefused("not_confirmed", "the request is not confirmed")
    draft, _w, parts = got
    if not isinstance(seen, dict) or seen.get("semantic_diff_hash") != draft["semantic_diff_hash"]:
        raise BusinessKnowledgeRefused("diff_mismatch", "the change you are acting on is not the change as it is now; look at it again")
    if seen.get("draft_hash") != draft["draft_hash"]:
        raise BusinessKnowledgeRefused("draft_revised", "the draft was revised since you looked at it; look at the current version again")
    committed = v2_business_commit.committed_state(store, tenant_id, workspace_id, change_id)
    if mode == "manual":
        return {"mode": "manual", "applies_nothing": True, "manual": manual_items(draft, "evidence.read" in perms),
                "statement": "Nothing is written by the product. Edit the files, refresh the sources; the next analysis reads them back and recomputes the implementation status."}
    if APPLY_PERMISSION not in perms:
        raise PermissionDenied(APPLY_PERMISSION)
    eh = bd.edits_hash(draft)
    if eh is None:
        raise BusinessKnowledgeRefused("nothing_to_apply", "this draft proposes no automatic edit; the items are manual")
    if seen.get("edits_hash") != eh:
        raise BusinessKnowledgeRefused("edits_changed", "the proposed edits are not the ones you looked at")
    from infra.worker.business_source_apply import approved_file_writes
    decision = approved_file_writes(store, tenant_id, workspace_id, change_id, draft)
    if decision is None:
        raise BusinessKnowledgeRefused("not_approved", "the file edits need a current approval (file_writes) first")
    if committed is None:
        raise BusinessKnowledgeRefused("knowledge_not_committed", "commit the knowledge change first; the sources follow the decided knowledge")
    head = store.head(tenant_id, workspace_id)
    if draft["pin"].get("source_revision_id") != head.source_revision_id:
        raise StaleVersion("source_moved: the sources changed since the edits were reviewed")
    job_id = apply_job_id(change_id, eh)
    from infra.worker.verification import ensure_job
    created = ensure_job(store, tenant_id, workspace_id, job_id, KIND, {"change_id": change_id, "edits_hash": eh, "decision_id": decision["decision_id"], "source_revision_id": head.source_revision_id,
                                                                       "requested_by": actor_id})
    rec = _job(store, tenant_id, workspace_id, job_id)
    return {"mode": "ai", "applies_nothing": False, "created": created, "job": _job_view(rec, "evidence.read" in perms)}
