"""EUI-08 guided proposal preparation (contract docs/contracts/GUIDED_PREPARATION.md). Off by default (`RIPPLE_GUIDED_PREPARATION=on` enables it).

A finite, prepare-only run over a CONFIRMED intent_flow request that already has a completed draft: impact -> simulation -> proposal, all deterministic (0 paid
calls). The person sees a manifest (pins, stages, the inputs the server computed from what they may read, the draft's proposed edits with NONE preselected,
unknowns, what stays manual) and starts it with their own explicit choices and the manifest hash they saw. Every child is an ordinary job created AS the
starter through the normal job-creation contract (`infra.v2_policy_routes.submit_job`); nothing is approved, committed, applied, published or deployed here, and
no job approval is given (none is needed by these executors; if one ever is, the run pauses `needs_job_approval`). When a pin or an input moves, the run pauses
`inputs_changed` and a new manifest + start are required.
"""
from __future__ import annotations

import json
import os

from psycopg2.extras import Json

from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters.v2_policy import PermissionDenied, permissions_for
from infra.adapters.v2_postgres import TenantScopeError, _as_object

FLAG = "RIPPLE_GUIDED_PREPARATION"
SCHEMA = "ripple-preparation-manifest/1"
STAGES = ("impact", "simulation", "proposal")
MANUAL_AFTER = ("verification contract", "review", "approval", "apply")
NOT_FOUND = "request not found"


class PreparationRefused(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def enabled() -> bool:
    return (os.environ.get(FLAG) or "").strip().lower() == "on"


def _require_on():
    if not enabled():
        raise PreparationRefused("preparation_off", "Guided preparation is switched off on this server. The manual steps work as usual.")


def _audit(cur, tenant, ws, event, actor, payload):
    cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)", (tenant, ws, event, actor, Json(payload)))


def _latest_draft(store, tenant, ws, intent_id):
    from infra import v2_lineage
    from infra.adapters.v2_request_tasks import _jobs_where
    for d in _jobs_where(store, tenant, ws, "change.draft", "intent_id", [intent_id]):
        if d.get("execution_status") == "COMPLETED" and not v2_lineage.superseding_draft(store, tenant, ws, d["job_id"]):
            return d
    return None


def _impact_inputs(store, tenant, ws, target):
    """The impact job's inputs exactly as the manual page builds them (`/v2/change/impact-context`); no target = the page's own fallback."""
    if not target:
        return {"candidate_ids": [], "existing_finding_ids": []}, False
    from infra.v2_graph import impact_context
    cx = impact_context(store, tenant, ws, target)
    if cx is None:
        return None, False
    return ({"candidate_ids": cx["candidate_ids"], "target_candidate_id": target, "relation_job_ids": cx["relation_job_ids"], "existing_finding_ids": cx["existing_finding_ids"],
             "expected_decision_epoch": cx["expected_decision_epoch"], "context_truncated": cx.get("context_truncated", False)}, bool(cx["relation_job_ids"]))


def manifest(store, tenant, ws, intent_id, authority, *, target=None, accepted=None) -> dict:
    """Read-only manifest for THIS person (no job, no call). `target` / `accepted` are the person's choices; nothing is chosen for them except a single possible
    target rule, which is shown as proposed and still part of what they confirm."""
    _require_on()
    from infra.adapters import v2_request_tasks
    v2_request_tasks.task_view(store, tenant, ws, "intent_flow", intent_id, authority)    # the request's read rule first (generic 404)
    if "change.prepare" not in permissions_for(authority.get("roles")):
        raise PermissionDenied("change.prepare")
    intent = store.get_intent(tenant, ws, intent_id)
    if (intent or {}).get("status") != "CONFIRMED":
        raise PreparationRefused("not_confirmed", "The request has to be confirmed first.")
    draft = _latest_draft(store, tenant, ws, intent_id)
    if draft is None:
        raise PreparationRefused("no_draft", "A completed draft is needed first; asking for the draft stays a separate, paid step.")
    head = store.head(tenant, ws)
    edits = (draft.get("execution_result") or {}).get("edits") or []
    targets, seen = [], set()
    for e in edits:
        for cid in e.get("candidate_ids") or []:
            if cid not in seen:
                seen.add(cid)
                targets.append({"id": cid, "label": f"{e.get('logical_path') or 'edit'}" + (f" · {(e.get('proposal_edit') or {}).get('path')}" if (e.get("proposal_edit") or {}).get("path") else "")})
    ids = [t["id"] for t in targets]
    unknowns, choices = [], []
    if target is not None and target not in ids:
        raise PreparationRefused("target_not_offered", "That rule is not one of this draft's target rules.", 400)
    sel_target = target if target is not None else (ids[0] if len(ids) == 1 else None)
    if len(ids) > 1 and sel_target is None:
        choices.append("target_rule")
    if not ids:
        unknowns.append("no_target_rule: the impact analysis runs without a target, so related rules stay UNKNOWN")
    impact, covered = _impact_inputs(store, tenant, ws, sel_target) if (sel_target or not ids) else (None, False)
    if sel_target and impact is None:
        raise PreparationRefused("target_not_current", "The target rule is no longer a current rule; the draft has to be revised.")
    if sel_target and not covered:
        unknowns.append("no_relation_coverage: rules without relation evidence stay UNKNOWN in the impact analysis")
    proposed = [e for e in edits if e.get("status") == "PROPOSED"]
    offered = [{"ref": e.get("ref"), "artifact_id": e.get("artifact_id"), "logical_path": e.get("logical_path"), "path": (e.get("proposal_edit") or {}).get("path"),
                "expected_current": (e.get("proposal_edit") or {}).get("expected_current"), "proposed": (e.get("proposal_edit") or {}).get("proposed")} for e in proposed]
    if not offered:
        unknowns.append("no_proposed_edit: the draft has no edit a proposal can carry")
    by_ref = {o["ref"]: o for o in offered}
    acc = sorted(set(accepted or []))
    if accepted is not None and any(r not in by_ref for r in acc):
        raise PreparationRefused("edit_not_offered", "An accepted edit is not one of this draft's proposed edits.", 400)
    arts = {by_ref[r]["artifact_id"] for r in acc}
    if accepted is not None and len(arts) > 1:
        raise PreparationRefused("edits_of_several_files", "One proposal covers one file; keep edits of a single file.", 400)
    if not acc:
        choices.append("accepted_edits")
    body = {"schema": SCHEMA, "intent_id": intent_id,
            "pins": {"draft_job_id": draft["job_id"], "source_revision_id": head.source_revision_id, "decision_epoch": head.decision_epoch},
            "stages": [{"stage": "impact", "kind": "impact", "paid_calls": 0}, {"stage": "simulation", "kind": "simulation", "paid_calls": 0, "adapter": "harbor_cli"},
                       {"stage": "proposal", "kind": "proposal", "paid_calls": 0, "edit_kind": "yaml_typed_edit"}],
            "selected": {"target_candidate_id": sel_target, "target_proposed_by_server": target is None and sel_target is not None, "impact": impact,
                         "accepted_refs": acc, "target_artifact_id": next(iter(arts)) if arts else None},
            "target_options": targets, "edits_offered": offered, "unknowns": unknowns, "choices_needed": choices,
            "paid_calls": 0, "paid_cap_usd": 0, "manual_after": list(MANUAL_AFTER),
            "files_changed_by_run": "none: the proposal is a staged artifact; applying it needs its own review, approval and apply",
            "job_approval": "none given by the run (these jobs do not need one); if one is ever needed the run pauses for the right person"}
    hashed = {**body, "selected": {k: v for k, v in body["selected"].items() if k != "target_proposed_by_server"}}   # how the target is SHOWN is not an input
    return {**body, "manifest_hash": canonical_hash(hashed), "ready_to_start": not choices}


def _row(store, tenant, ws, run_id):
    with store._tx() as cur:
        cur.execute("""SELECT intent_id, started_by, manifest, manifest_hash, status, reason, children FROM v2_preparation_runs
                       WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s""", (tenant, ws, run_id))
        r = cur.fetchone()
    if r is None:
        return None
    return {"run_id": run_id, "intent_id": r[0], "started_by": r[1], "manifest": _as_object(r[2]), "manifest_hash": r[3], "status": r[4], "reason": r[5], "children": _as_object(r[6]) or {}}


def _save(store, tenant, ws, run, actor, event=None):
    with store._tx() as cur:
        cur.execute("""UPDATE v2_preparation_runs SET status=%s, reason=%s, children=%s, updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s""",
                    (run["status"], run["reason"], Json(run["children"]), tenant, ws, run["run_id"]))
        if event:
            _audit(cur, tenant, ws, event, actor, {"run_id": run["run_id"], "status": run["status"], "reason": run["reason"], "children": run["children"]})


def view(store, tenant, ws, run) -> dict:
    stages = []
    for s in STAGES:
        jid = run["children"].get(s)
        st = None
        if jid:
            try:
                st = store.job_record(tenant, ws, jid).get("execution_status")
            except TenantScopeError:
                st = "MISSING"
        stages.append({"stage": s, "job_id": jid, "state": (st or "not_started").lower()})
    m = run["manifest"]
    return {"run_id": run["run_id"], "intent_id": run["intent_id"], "status": run["status"], "reason": run["reason"], "stages": stages, "manifest_hash": run["manifest_hash"],
            "selected": m.get("selected"), "unknowns": m.get("unknowns"), "manual_after": m.get("manual_after"), "paid_calls": 0,
            "proposal_job_id": run["children"].get("proposal") if run["status"] == "PROPOSAL_READY" else None}


def _own(store, tenant, ws, run_id, authority):
    run = _row(store, tenant, ws, run_id)
    if run is None or run["started_by"] != authority["account_id"]:
        raise TenantScopeError("run not found")                                          # another person's run looks like a missing one
    return run


def start(store, tenant, ws, intent_id, authority, *, manifest_hash, target, accepted, submit) -> dict:
    if not isinstance(accepted, list) or not accepted:
        raise PreparationRefused("choices_needed", "Choose the edits the proposal should carry; none is chosen for you.", 400)
    m = manifest(store, tenant, ws, intent_id, authority, target=target, accepted=accepted)
    if not m["ready_to_start"]:
        raise PreparationRefused("choices_needed", "Some choices are still open: " + ", ".join(m["choices_needed"]), 400)
    if m["manifest_hash"] != manifest_hash:
        raise PreparationRefused("manifest_changed", "What would run changed since you looked at it; review the new manifest and start again.")
    run_id = "prep-" + canonical_hash({"t": tenant, "w": ws, "a": authority["account_id"], "i": intent_id, "d": m["pins"]["draft_job_id"], "m": manifest_hash})[:20]
    body = {k: v for k, v in m.items() if k not in ("manifest_hash", "ready_to_start")}
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_preparation_runs (tenant_id, workspace_id, run_id, intent_id, started_by, manifest, manifest_hash, status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'RUNNING') ON CONFLICT DO NOTHING""", (tenant, ws, run_id, intent_id, authority["account_id"], Json(body), manifest_hash))
        if cur.rowcount:
            _audit(cur, tenant, ws, "PREPARATION_RUN_STARTED", authority["account_id"], {"run_id": run_id, "intent_id": intent_id, "manifest_hash": manifest_hash, "paid_calls": 0})
    return advance(store, tenant, ws, run_id, authority, submit=submit)


def _pause(store, tenant, ws, run, actor, reason, status="PAUSED"):
    run["status"], run["reason"] = status, reason
    _save(store, tenant, ws, run, actor, "PREPARATION_RUN_" + status)
    return view(store, tenant, ws, run)


def advance(store, tenant, ws, run_id, authority, *, submit) -> dict:
    """One idempotent step: settle the current child, or start the next one AS the starter after rechecking pins and inputs. Called by the starter's page while
    the run is active; never by another person (their authority would make them the requester)."""
    _require_on()
    run = _own(store, tenant, ws, run_id, authority)
    if run["status"] != "RUNNING":
        return view(store, tenant, ws, run)
    m, actor = run["manifest"], authority["account_id"]
    from infra import v2_lineage
    for stage in STAGES:
        jid = run["children"].get(stage)
        if jid:
            rec = store.job_record(tenant, ws, jid)
            st = rec.get("execution_status")
            if st == "COMPLETED":
                continue
            if st in ("FAILED", "CANCELLED"):
                return _pause(store, tenant, ws, run, actor, f"{stage}_{st.lower()}: {rec.get('execution_reason') or 'no reason given'}", status="FAILED")
            return view(store, tenant, ws, run)                                           # still queued / running
        head = store.head(tenant, ws)
        pins = m["pins"]
        if head.source_revision_id != pins["source_revision_id"] or head.decision_epoch != pins["decision_epoch"] \
                or v2_lineage.superseding_draft(store, tenant, ws, pins["draft_job_id"]):
            return _pause(store, tenant, ws, run, actor, "inputs_changed: the sources, the decisions or the draft moved; review a new manifest and start again")
        sel = m["selected"]
        if stage == "impact":
            now, _cov = _impact_inputs(store, tenant, ws, sel["target_candidate_id"])
            if now != sel["impact"]:
                return _pause(store, tenant, ws, run, actor, "inputs_changed: the impact inputs moved since the start; review a new manifest and start again")
            payload = dict(sel["impact"], intent_id=run["intent_id"])
        elif stage == "simulation":
            payload = {"adapter": "harbor_cli", "from_draft_job_id": pins["draft_job_id"]}
        else:
            payload = {"intent_id": run["intent_id"], "impact_job_id": run["children"]["impact"], "simulation_job_id": run["children"]["simulation"],
                       "target_artifact_id": sel["target_artifact_id"], "edit_kind": "yaml_typed_edit", "draft_job_id": pins["draft_job_id"], "accepted_refs": sel["accepted_refs"]}
        jid = f"prep-{stage}-" + canonical_hash({"run": run_id, "stage": stage})[:16]
        code, body = submit(stage, jid, payload)
        if code != 200:
            try:
                existing = store.job_record(tenant, ws, jid)                             # a concurrent advance created it first: adopt the same job
            except TenantScopeError:
                existing = None
            if existing is None or (existing.get("payload") or {}) != payload:
                return _pause(store, tenant, ws, run, actor, f"{stage}_not_started: {body.get('error') or body}")
        elif body.get("status") == "PENDING_APPROVAL":
            from infra.worker import sandbox_hold
            if sandbox_hold.approval_hold(store, tenant, ws, jid, stage):
                run["children"][stage] = jid
                return _pause(store, tenant, ws, run, actor, "needs_job_approval: this job waits for a person's approval; the run gives none")
        run["children"][stage] = jid
        _save(store, tenant, ws, run, actor, "PREPARATION_CHILD_STARTED")
        return view(store, tenant, ws, run)
    run["status"], run["reason"] = "PROPOSAL_READY", None
    _save(store, tenant, ws, run, actor, "PREPARATION_RUN_PROPOSAL_READY")
    return view(store, tenant, ws, run)


def status(store, tenant, ws, run_id, authority) -> dict:
    return view(store, tenant, ws, _own(store, tenant, ws, run_id, authority))


def runs_for(store, tenant, ws, intent_id, authority) -> list:
    with store._tx() as cur:
        cur.execute("""SELECT run_id FROM v2_preparation_runs WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s AND started_by=%s ORDER BY created_at DESC LIMIT 5""",
                    (tenant, ws, intent_id, authority["account_id"]))
        ids = [r[0] for r in cur.fetchall()]
    return [view(store, tenant, ws, _row(store, tenant, ws, i)) for i in ids]


def cancel(store, tenant, ws, run_id, authority) -> dict:
    run = _own(store, tenant, ws, run_id, authority)
    if run["status"] in ("RUNNING", "PAUSED"):
        for jid in run["children"].values():
            try:
                store.request_cancel(tenant, ws, jid, reason="preparation run cancelled by its starter")
            except Exception:                                                             # noqa: BLE001 - finished or running: its result stays
                pass
        return _pause(store, tenant, ws, run, authority["account_id"], "cancelled by the starter; jobs that had finished keep their results", status="CANCELLED")
    return view(store, tenant, ws, run)


def response_parts(resp) -> tuple[int, dict]:
    try:
        return resp.status_code, json.loads(resp.body)
    except (ValueError, AttributeError):
        return getattr(resp, "status_code", 500), {}
