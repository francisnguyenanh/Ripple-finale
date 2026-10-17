"""B3: read-only loop status for ONE change: draft job -> apply job -> post-apply stages -> residual sources.

It never marks the request COMPLETE because a single stage finished: COMPLETE needs verification PASSED, the knowledge
rebuild and every re-extraction job COMPLETED, no critical knowledge blocker, and no draft item left manual /
ambiguous / unsupported / unaddressed that still holds the old value (or cannot be read) in the CURRENT revision.
Otherwise the status is PARTIAL (supported scope done) or PENDING, with the reasons and the residual counterevidence
(each stale source with its current value read from the head revision)."""
from __future__ import annotations

from infra.adapters.v2_postgres import TenantScopeError
from infra.worker.change_brief import _lockey, _num, _numbers_in
from infra.worker import extraction

_OPEN = ("MANUAL_CHANGE", "AMBIGUITY", "UNSUPPORTED")


def _job(store, tenant, ws, job_id):
    try:
        return store.job_record(tenant, ws, job_id)
    except TenantScopeError:
        return None


def _lineage(store, tenant, ws, draft_job_id, apply_) -> str:
    """apply -> approval -> proposal -> draft, read from the stored payloads. 'ok' | 'mismatch' | 'unproven'."""
    approval = _job(store, tenant, ws, (apply_.get("payload") or {}).get("approval_job_id") or "")
    proposal = _job(store, tenant, ws, ((approval or {}).get("payload") or {}).get("proposal_job_id") or "")
    named = ((proposal or {}).get("payload") or {}).get("draft_job_id")
    if not named:
        return "unproven"
    return "ok" if named == draft_job_id else "mismatch"


def loop_status(store, tenant: str, ws: str, draft_job_id: str, apply_job_id: str) -> dict:
    draft = _job(store, tenant, ws, draft_job_id)
    apply_ = _job(store, tenant, ws, apply_job_id)
    if not draft or draft.get("kind") != "change.draft" or draft.get("execution_status") != "COMPLETED":
        raise TenantScopeError("draft job not found or not completed")
    if not apply_ or apply_.get("kind") != "apply":
        raise TenantScopeError("apply job not found")
    d = draft["execution_result"]
    lineage = _lineage(store, tenant, ws, draft_job_id, apply_)
    if lineage == "mismatch":
        raise TenantScopeError("apply job does not belong to this draft")
    fields = (d.get("intent") or {}).get("fields") or {}
    new_value, old_value = fields.get("new_value"), fields.get("old_value")
    ares = apply_.get("execution_result") or {}
    published = ares.get("published_revision_id")
    stages, reasons = {}, []

    def st(name, rec, ok=("COMPLETED",)):
        state = (rec or {}).get("execution_status") if rec else "MISSING"
        stages[name] = state
        if state not in ok:
            reasons.append(f"{name}:{state}")

    st("apply", apply_)
    ver = _job(store, tenant, ws, f"verify-{apply_job_id}")
    st("verification_job", ver)
    vstatus = ((ver or {}).get("execution_result") or {}).get("status") if ver else None
    stages["verification"] = vstatus
    if vstatus != "PASSED":
        reasons.append(f"verification:{vstatus}")
    rebuild = _job(store, tenant, ws, f"rebuild-{apply_job_id}")
    st("knowledge_rebuild", rebuild)
    rean = store.jobs_by_prefix(tenant, ws, f"reanalysis-apply-{apply_job_id}-")
    done = sum(1 for j in rean if j.get("execution_status") == "COMPLETED")
    # Artifacts whose re-extraction could not be reserved under the project cost cap are part of the TOTAL (they are unfinished work),
    # so a capped wave never reads as complete.
    deferred = (((rebuild or {}).get("execution_result") or {}).get("reanalysis") or {}).get("budget_deferred_artifact_ids") or []
    total = len(rean) + len(deferred)
    stages["reanalysis"] = {"completed": done, "total": total}
    if deferred:
        stages["reanalysis"]["deferred_for_budget"] = len(deferred)
        reasons.append(f"reanalysis_deferred_for_budget:{len(deferred)}")
    if not rean or done != total:
        reasons.append(f"reanalysis:{done}/{total}")
    blockers = [f for f in store.list_critical_findings(tenant, ws)
                if f.get("finding_id") and store.active_decision_action(tenant, ws, f["finding_id"]) is None]
    stages["knowledge_blockers"] = len(blockers)
    if blockers:
        reasons.append(f"knowledge_blockers:{len(blockers)}")

    # residual: what the draft left for humans, re-read in the CURRENT revision
    residual = []
    head = store.head(tenant, ws)
    head_rev = head.source_revision_id
    if published and head_rev != published:
        reasons.append("head_moved_after_apply")
    arts = {a["artifact_id"]: a for a in store.source_revision_view(tenant, ws, head_rev)["artifacts"]}
    obs = {}
    for o in store.source_observations(tenant, ws, head_rev):
        obs[(o["artifact_id"], _lockey(o.get("locator")))] = o
    items = [x for x in d.get("items") or [] if x.get("status") in _OPEN]
    applied_refs = {e["ref"] for e in d.get("edits") or []}
    for x in items:
        o = obs.get((x["artifact_id"], _lockey(x.get("locator"))))
        if o is None:
            state = "unreadable_in_current_revision"
        else:
            text = extraction._observation_text(o)
            raw = o.get("raw_value")
            scalar = _num(raw) is not None
            nums = [_num(raw)] if scalar else _numbers_in(text)
            if scalar:
                state = "at_new_value" if new_value in nums else "still_old_value" if old_value in nums else "other_value"
            else:
                # a number somewhere in prose or code is not proof that the intended rule was updated: it stays unresolved
                state = ("still_old_value" if old_value in nums else
                         "new_value_present_but_not_verified_as_the_rule" if new_value in nums else "other_value")
        residual.append({"ref": x["ref"], "logical_path": x["logical_path"], "draft_status": x["status"],
                         "current_state": state, "reason": x.get("reason"),
                         **({"disposition": x.get("disposition"), "carried_from": x.get("carried_from"), "disposition_basis": x.get("disposition_basis")} if (x.get("carried_from") or x.get("disposition") not in (None, "unresolved")) else {})})
    for u in d.get("unaddressed") or []:
        residual.append({"ref": u["ref"], "logical_path": u.get("logical_path"), "draft_status": "UNADDRESSED",
                         "current_state": "not_classified_by_the_draft", "reason": None,
                         **({"disposition": u.get("disposition"), "carried_from": u.get("carried_from"), "disposition_basis": u.get("disposition_basis")} if (u.get("carried_from") or u.get("disposition") not in (None, "unresolved")) else {})})
    unresolved = [r for r in residual if r["current_state"] != "at_new_value"]
    if unresolved:
        reasons.append(f"residual_sources:{len(unresolved)}")
    open_items = d.get("open_items") or []
    for key in ("not_covered", "unsupported", "rejected", "questions_for_human", "open_items"):
        if d.get(key):
            reasons.append(f"draft_{key}:{len(d[key])}")
    if lineage == "unproven":
        reasons.append("lineage_not_proven")
    supported_done = bool(applied_refs) and vstatus == "PASSED"
    if not reasons:
        status = "COMPLETE"
    elif supported_done:
        status = "PARTIAL"
    else:
        status = "PENDING"
    return {"status": status, "supported_edit_status": "VERIFIED" if supported_done else "NOT_VERIFIED",
            "request_status": status, "lineage": lineage, "reasons": reasons, "stages": stages, "published_revision_id": published,
            "current_revision_id": head_rev, "supported_scope_applied": sorted(applied_refs),
            "residual_counterevidence": residual, "open_findings_unchanged": len(open_items),
            "note": "COMPLETE requires every stage done and no residual source; a finished rebuild alone is not completion."}
