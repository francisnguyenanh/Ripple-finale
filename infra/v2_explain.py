"""Stored AI wording of verified results, one per SNAPSHOT.

A snapshot = (subject, source revision, decision epoch, hash of the verified facts). The facts are collected here, on the server, from
stored records the caller may read; a client never supplies them. The wording is produced by the `explain` job (role `intent.explain`)
and stored as that job's result, so reading it is a plain database read:

  GET  /v2/explanations?subject=..&subject_id=..   -> what is stored for the CURRENT snapshot, or why nothing is (never a model call)
  POST /v2/explanations                            -> create the job for the current snapshot (idempotent: same snapshot, same job);
                                                      a DIFFERENT person must allow the model call, like every model call.
"""
from __future__ import annotations

from infra.db_offload import db_route
from infra.v2_policy_routes import _json_body_sync
import hashlib
import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require, require_human_actor
from infra.adapters.v2_postgres import ConcurrencyLimitExceeded, StaleVersion, StoreConflict, TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok, _v2_store
from infra.worker import job_quota
from infra.worker.change_draft import _canon_json

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
KIND = "explain"
SUBJECTS = ("approval", "change_result")


def _basename(p) -> str:
    return str(p or "").replace("\\", "/").split("/")[-1]


def _rec(store, tenant, ws, job_id, kind=None):
    try:
        rec = store.job_record(tenant, ws, job_id)
    except Exception as exc:
        raise TenantScopeError("subject not found") from exc
    if kind and rec.get("kind") != kind:
        raise TenantScopeError("subject not found")
    return rec


def approval_facts(store, tenant, ws, approval_job_id: str) -> dict:
    appr = _rec(store, tenant, ws, approval_job_id, "approval")
    if appr.get("execution_status") != "COMPLETED":
        raise StoreConflict("the approval bundle is not finished")
    ar = appr.get("execution_result") or {}
    pay = appr.get("payload") or {}
    prop = _rec(store, tenant, ws, pay.get("proposal_job_id", ""), "proposal")
    pr = prop.get("execution_result") or {}
    ppay = prop.get("payload") or {}
    facts: dict = {"subject": "A prepared change waiting for an approver's decision.",
                   "facts_origin": {"approval_job_id": approval_job_id, "proposal_job_id": pay.get("proposal_job_id"),
                                    "simulation_job_id": ppay.get("simulation_job_id"), "review_job_id": pay.get("review_job_id"),
                                    "source_revision_id": ((ar.get("pinned_input") or pr.get("pinned_input") or {}).get("pinned_source_revision_id"))}}
    try:
        it = store.get_intent(tenant, ws, ppay.get("intent_id", ""))
        sf = it.get("structured_form") or {}
        facts["request"] = {"what": sf.get("subject"), "from": sf.get("old_value"), "to": sf.get("new_value"), "unit": sf.get("unit"),
                            "currency": sf.get("currency"), "applies_to": sf.get("applies_to"), "scope": sf.get("scope")}
    except Exception:
        facts["request"] = None
    facts["edit"] = {"kind": pr.get("edit_kind"), "files_changed": 1 if pr.get("after_blob_hash") else 0}
    sim_id = ppay.get("simulation_job_id")
    sim = None
    if sim_id:
        try:
            s = (_rec(store, tenant, ws, sim_id, "simulation").get("execution_result") or {})
            h = s.get("harbor")
            if isinstance(h, dict) and s.get("status") in ("EXECUTED", "PARTIAL"):
                sim = {"cases_simulated": h.get("transactions"), "cases_that_change": h.get("changed_count"), "changes_not_explained": h.get("unexplained_changes")}
                if s.get("status") == "PARTIAL":
                    sim["partial"] = True
                    sim["not_simulated"] = str(s.get("reason") or "part of the change")
                if s.get("untested_edits"):
                    sim["edits_not_simulated"] = len(s["untested_edits"])
            else:
                sim = {"ran": False, "why": str(s.get("reason") or s.get("status") or "not available")}
        except TenantScopeError:
            sim = None
    facts["simulation"] = sim
    review = {}
    try:
        rr = (_rec(store, tenant, ws, pay.get("review_job_id", ""), "review").get("execution_result") or {})
        checks = rr.get("checks") or {}
        review = {"checks_passed": sum(1 for c in checks.values() if c.get("status") == "PASS"),
                  "checks_total": len(checks),
                  "checks_not_available": sorted(k for k, c in checks.items() if c.get("status") in ("UNAVAILABLE", "NOT_RUN"))}
    except TenantScopeError:
        review = {}
    facts["review"] = review
    blockers = ar.get("blocked_reasons") or []
    facts["blockers"] = [str(b.get("name") or b.get("kind") or "blocker") for b in blockers if isinstance(b, dict)]
    facts["manual_items_left_for_people"] = sum(1 for s in (pr.get("verification_plan") or []) if isinstance(s, dict) and s.get("action") == "human_authoring_required")
    facts["readiness"] = ar.get("readiness")
    facts["next"] = "An approver who did not request the change decides. Approving applies nothing by itself."
    return facts


def change_result_facts(store, tenant, ws, apply_job_id: str) -> dict:
    from infra.adapters.v2_change import get_change_view
    dto = get_change_view(store, tenant, ws, apply_job_id)
    re_ = dto.get("reanalysis") or {}
    facts = {"subject": "The result of an applied change.", "state": dto.get("state"),
             "machine_verification": (dto.get("verification") or {}).get("status", "NOT_STARTED"),
             "reanalysis": re_.get("status"), "knowledge": (dto.get("knowledge") or {}).get("status"),
             "completion": (dto.get("completion") or {}).get("status"),
             "waiting_for_human_result_decision": not bool((dto.get("completion") or {}).get("decision")),
             "manual_items_open": sum(1 for m in dto.get("manual_work") or [] if not m.get("proof")),
             "alignment": (dto.get("alignment") or {}).get("global", "NOT_ASSESSED"),
             "reopened_decisions": len((dto.get("carry_forward") or {}).get("reopened") or [])}
    return facts


def build_facts(store, tenant, ws, subject: str, subject_id: str) -> dict:
    if subject == "approval":
        return approval_facts(store, tenant, ws, subject_id)
    if subject == "change_result":
        return change_result_facts(store, tenant, ws, subject_id)
    raise TenantScopeError("unknown subject")


def snapshot_of(store, tenant, ws, subject: str, subject_id: str, facts: dict) -> dict:
    head = store.head(tenant, ws)
    input_hash = hashlib.sha256(_canon_json(facts).encode("utf-8")).hexdigest()
    key = hashlib.sha256(f"{subject}|{subject_id}|{head.source_revision_id}|{head.decision_epoch}|{input_hash}".encode()).hexdigest()[:20]
    return {"key": key, "source_revision_id": head.source_revision_id, "decision_epoch": head.decision_epoch, "input_hash": input_hash}


def currentness(store, tenant, ws, facts: dict) -> dict:
    """Where the facts come from versus the head now: an explanation of an older state is HISTORICAL, never relabelled current."""
    origin = (facts.get("facts_origin") or {}) if isinstance(facts, dict) else {}
    head = store.head(tenant, ws).source_revision_id
    rev = origin.get("source_revision_id")
    return {"facts_source_revision_id": rev, "head_source_revision_id": head,
            "status": "UNKNOWN" if not rev else ("CURRENT" if rev == head else "HISTORICAL"),
            "fact_jobs": {k: v for k, v in origin.items() if k.endswith("_job_id") and v}}


def job_id_for(snapshot: dict) -> str:
    return f"explain-{snapshot['key']}"


def _quota():
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        raise job_quota.BudgetConfigError("live explain requires a budget config")
    config = job_quota.load_worker_budget_config()
    job_quota.check_job_admission(config, job_kind=KIND)
    return job_quota.load_concurrency_limit(), job_quota.compute_budget_reservation(config)


def _handle(exc):
    if isinstance(exc, IdentityDenied):
        return _error("sign in required" if _identity_denied_status(exc) == 401 else "permission denied", _identity_denied_status(exc))
    if isinstance(exc, PermissionDenied):
        return _error(f"permission denied: {exc}", 403)
    if isinstance(exc, TenantScopeError):
        return _error(str(exc), 404)
    if isinstance(exc, (job_quota.JobQuotaError, job_quota.BudgetExceeded, ConcurrencyLimitExceeded)):
        from infra import provider_gate
        return provider_gate.refusal_for(exc) or _error(f"budget_exceeded: {exc}", 409)
    if isinstance(exc, job_quota.BudgetConfigError):
        return _error(f"budget config error: {exc}", 500)
    if isinstance(exc, (StaleVersion, StoreConflict)):
        return _error(str(exc), 409)
    raise exc


def _state_of(store, tenant, ws, snapshot: dict) -> dict:
    jid = job_id_for(snapshot)
    try:
        rec = store.job_record(tenant, ws, jid)
    except Exception:
        return {"status": "none", "job_id": None, "text": None}
    st = rec.get("execution_status")
    status = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled"}.get(st)
    if status is None:
        status = "waiting_for_approval" if rec.get("status") in ("PENDING_APPROVAL",) else "running"
    res = rec.get("execution_result") if st == "COMPLETED" else None
    return {"status": status, "job_id": jid, "text": (res or {}).get("text"), "label": (res or {}).get("label"),
            "model": rec.get("execution_model_id"), "reason": rec.get("execution_reason") if st == "FAILED" else None}


def _earlier(store, tenant, ws, subject: str, subject_id: str, current_key: str) -> dict | None:
    """The newest stored wording of this subject for an OLDER snapshot (shown as out of date, never as current)."""
    best = None
    for j in store.jobs_by_prefix(tenant, ws, "explain-"):
        res = j.get("execution_result") or {}
        snap = res.get("snapshot") or {}
        if j.get("execution_status") == "COMPLETED" and res.get("subject") == subject and snap.get("subject_id") == subject_id and snap.get("key") != current_key:
            best = {"text": res.get("text"), "snapshot": snap}
    return best


@router.get("/v2/explanations")
def explanation(request: Request, workspace_id: str, subject: str, subject_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if subject not in SUBJECTS:
            return _error("unknown subject", 400)
        tenant = authority["tenant_id"]
        facts = build_facts(store, tenant, workspace_id, subject, subject_id)
        snap = snapshot_of(store, tenant, workspace_id, subject, subject_id, facts)
        out = {"subject": subject, "subject_id": subject_id, "snapshot": snap, "currentness": currentness(store, tenant, workspace_id, facts),
               **_state_of(store, tenant, workspace_id, snap)}
        sim = facts.get("simulation") if isinstance(facts, dict) else None
        if isinstance(sim, dict) and sim.get("ran") is False:
            # the wording was written from facts in which no simulation ran: say so next to the text (server-derived from the same facts, never from the text)
            out["caveats"] = [{"code": "simulation_not_run", "why": str(sim.get("why") or "not available")}]
        earlier = _earlier(store, tenant, workspace_id, subject, subject_id, snap["key"])
        if earlier and out["status"] != "done":
            out["out_of_date"] = earlier
        return JSONResponse(out, headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


@router.post("/v2/explanations")
@db_route
def request_explanation(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    ws, subject, subject_id = body.get("workspace_id"), body.get("subject"), body.get("subject_id")
    try:
        authority = _authority(identity, request, ws if isinstance(ws, str) else "")      # sign-in and rights BEFORE the body is looked at
        require_human_actor(authority)
        require(authority, "ai.use")
        if not all(isinstance(x, str) and x for x in (ws, subject, subject_id)) or subject not in SUBJECTS:
            return _error("workspace_id, subject and subject_id required", 400)
        tenant = authority["tenant_id"]
        facts = build_facts(store, tenant, ws, subject, subject_id)
        snap = snapshot_of(store, tenant, ws, subject, subject_id, facts)
        jid = job_id_for(snap)
        state = _state_of(store, tenant, ws, snap)
        if state["status"] == "none":
            limit, reservation = _quota()
            head = store.head(tenant, ws)
            from infra import provider_gate
            provider_gate.require_provider("explain")
            store.create_job(tenant, ws, jid, KIND, {"subject": subject, "facts": facts, "snapshot": {**snap, "subject_id": subject_id}},
                             expected_version=head.version, idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit,
                             budget_reservation=reservation, requested_by=authority["account_id"])
            from infra import ai_admission
            ai_admission.admit(store, tenant, ws, jid, KIND, authority)
            state = _state_of(store, tenant, ws, snap)
        return JSONResponse({"subject": subject, "subject_id": subject_id, "snapshot": snap, **state}, headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)
