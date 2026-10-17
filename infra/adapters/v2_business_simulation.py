"""Measured impact of a CONFIRMED business change through the existing Harbor simulation (CHG-02 / R06, F-07 c): the numeric path of the business flow.

A `set_value` operation whose draft action is an automatic YAML edit of `config/approval.yaml` on a key the Harbor adapter supports (`harbor_sim.ALLOWED_EDITS`) can be MEASURED:
one deterministic `simulation` job per Harbor case (0 provider calls, the pinned public Harbor copy run in the resource-limited runner, config as validated data) compares the
baseline and the proposed configuration on the public sample. Every other operation stays `NOT_SIMULATED` with its reason; a result is never invented. The job is bound to the
change, the draft's edit hash and the pinned source revision; a moved draft is a new job. The report merges the measured counts at read time (`measured`), labelled as a result on
the public Harbor sample, never as a prediction. Reads only, except `request_simulation`, which creates the job through `ensure_job`.
"""
from __future__ import annotations

from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters import v2_business_change
from infra.adapters.v2_postgres import TenantScopeError
from infra.worker import harbor_sim

KIND = "simulation"
CONFIG_SUFFIX = harbor_sim.CONFIG_PATH_SUFFIX


class NothingToSimulate(ValueError):
    def __init__(self, reasons):
        super().__init__("nothing_to_simulate")
        self.reasons = reasons


def job_id_for(change_id: str, case: str, edit_hash: str) -> str:
    return "bsim-" + canonical_hash({"change": change_id, "case": case, "edits": edit_hash})[:20]


def plan(draft: dict, artifacts: dict) -> dict:
    """`{families: {case: [edit]}, ops: {op_id: {case|None, reason}}, edit_hash}` from the draft's file actions. `artifacts` = {artifact_id: logical_path} of the pinned revision."""
    families: dict = {}
    ops: dict = {o["op_id"]: {"case": None, "reason": None} for o in draft.get("operations", [])}
    for o in draft.get("operations", []):
        if o["kind"] != "set_value":
            ops[o["op_id"]]["reason"] = f"no simulation adapter supports a “{o['kind']}” change"
    for it in draft.get("file_actions", []):
        for op_id in it.get("op_ids") or []:
            if op_id not in ops or ops[op_id]["case"] is not None:
                continue
            pe = it.get("proposal_edit")
            if it.get("action") != "AI_EDIT_SUPPORTED" or not isinstance(pe, dict):
                ops[op_id]["reason"] = ops[op_id]["reason"] or f"the file edit is {it.get('action', 'unknown').lower()} ({it.get('reason')}); only an automatic edit of {CONFIG_SUFFIX} can be measured"
                continue
            logical = str(artifacts.get(it.get("resolved_artifact_id") or it.get("artifact_id")) or it.get("path") or "")
            key = pe.get("key_path")
            case = next((c for c, keys in harbor_sim.ALLOWED_EDITS.items() if key in keys), None)
            if not logical.endswith(CONFIG_SUFFIX):
                ops[op_id]["reason"] = f"the edited file ({logical or it.get('path')}) is not the Harbor approval configuration; nothing runs it"
            elif case is None:
                ops[op_id]["reason"] = f"the key {key} is not one the Harbor simulation supports"
            elif isinstance(pe.get("proposed"), bool) or not isinstance(pe.get("proposed"), int):
                ops[op_id]["reason"] = "the proposed value is not a whole number; the Harbor simulation takes integers only"
            else:
                ec = pe.get("expected_current")
                families.setdefault(case, []).append({"path": key, "value": pe["proposed"], "expected_current": ec if isinstance(ec, int) and not isinstance(ec, bool) else None, "op_id": op_id})
                ops[op_id] = {"case": case, "reason": None}
    for op_id, st in ops.items():
        if st["case"] is None and st["reason"] is None:
            st["reason"] = "no file of the pinned sources carries this value as an automatic edit"
    edit_hash = canonical_hash({c: [{k: v for k, v in e.items() if k != "op_id"} for e in es] for c, es in families.items()})[:24]
    return {"families": families, "ops": ops, "edit_hash": edit_hash}


def _artifacts(store, tenant_id, workspace_id, pin) -> dict:
    try:
        view = store.source_revision_view(tenant_id, workspace_id, pin["source_revision_id"])
    except TenantScopeError:
        return {}
    return {a["artifact_id"]: a.get("logical_path") or a["artifact_id"] for a in view["artifacts"]}


def _record(store, tenant_id, workspace_id, job_id):
    try:
        return store.job_record(tenant_id, workspace_id, job_id)
    except TenantScopeError:
        return None


def request_simulation(store, tenant_id, workspace_id, change_id, *, actor_id) -> dict:
    """Create (idempotently) one deterministic Harbor simulation job per supported case of the confirmed change's draft. Raises NothingToSimulate with the per-operation reasons."""
    got = v2_business_change.draft_for_intent(store, tenant_id, workspace_id, change_id)
    if got is None:
        raise TenantScopeError("change request not confirmed")
    draft = got["draft"]
    artifacts = _artifacts(store, tenant_id, workspace_id, draft["pin"])
    p = plan(draft, artifacts)
    if not p["families"]:
        raise NothingToSimulate({op: st["reason"] for op, st in p["ops"].items()})
    from infra.worker.verification import ensure_job
    jobs = []
    for case in sorted(p["families"]):
        edits = [{k: v for k, v in e.items() if k != "op_id"} for e in p["families"][case]]
        err = harbor_sim.validate_payload({"adapter": "harbor_cli", "case": case, "edits": edits})
        jid = job_id_for(change_id, case, p["edit_hash"])
        if err:
            jobs.append({"case": case, "job_id": jid, "created": False, "status": "refused", "reason": err})
            continue
        payload = {"adapter": "harbor_cli", "case": case, "edits": edits, "built_from": {"business_change_id": change_id, "edit_hash": p["edit_hash"], "pin": draft["pin"],
                                                                                            "op_ids": [e["op_id"] for e in p["families"][case]], "requested_by": actor_id}}
        created = ensure_job(store, tenant_id, workspace_id, jid, KIND, payload)
        rec = _record(store, tenant_id, workspace_id, jid)
        jobs.append({"case": case, "job_id": jid, "created": created, "status": _state(rec), "reason": None})
    return {"change_id": change_id, "edit_hash": p["edit_hash"], "jobs": jobs, "not_simulated": {op: st["reason"] for op, st in p["ops"].items() if st["case"] is None}}


def _state(rec) -> str:
    if rec is None:
        return "not_requested"
    st = rec.get("execution_status")
    return {"COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "failed"}.get(st) or "pending"


def _measured(case: str, res: dict, op_ids: list) -> dict:
    h = res.get("harbor") or {}
    return {"op_ids": op_ids, "case": case, "status": res.get("status"), "transactions": res.get("coverage", {}).get("submitted", 0), "changed_count": h.get("changed_count"),
            "unexplained_changes": h.get("unexplained_changes"), "by_route_before": h.get("by_route_before"), "by_route_after": h.get("by_route_after"),
            "emergency_rows_changed": h.get("emergency_rows_changed"), "not_controlled": h.get("not_controlled") or [], "inert_edits": h.get("inert_edits") or [],
            "no_change_edits": h.get("no_change_edits") or [], "edits": h.get("edits") or [], "baseline_source": h.get("baseline_source"), "integrity": h.get("integrity"),
            "limits": h.get("limits") or [], "input_data": h.get("input_data"), "config_hashes": {k: (h.get("snapshot") or {}).get(k) for k in ("config_baseline_sha256", "config_proposed_sha256")},
            "statement": "Measured by running the pinned public Harbor copy over its public sample with the baseline and the proposed configuration; it describes this sample, not a prediction for real orders."}


def merge_simulation(out: dict, store, tenant_id, workspace_id, change_id, draft: dict, *, can_read_evidence: bool) -> None:
    """Replace the report's static `simulation` / `measured` with the state of the Harbor simulation jobs of this change. Readers without evidence rights get counts but no file/key detail."""
    artifacts = _artifacts(store, tenant_id, workspace_id, draft["pin"])
    p = plan(draft, artifacts)
    rows, measured, overall = [], [], {"status": "not_requested", "edit_hash": p["edit_hash"], "cases": {}}
    by_case = {}
    for case, edits in sorted(p["families"].items()):
        rec = _record(store, tenant_id, workspace_id, job_id_for(change_id, case, p["edit_hash"]))
        st = _state(rec)
        by_case[case] = (rec, st)
        overall["cases"][case] = {"status": st, "job_id": rec.get("job_id") if rec else None, "reason": rec.get("execution_reason") if rec and st == "failed" else None}
    for op_id, s in p["ops"].items():
        if s["case"] is None:
            rows.append({"op_id": op_id, "status": "NOT_SIMULATED", "reason": s["reason"]})
            continue
        rec, st = by_case[s["case"]]
        if st == "completed" and rec.get("execution_result"):
            res = rec["execution_result"]
            if res.get("status") == "EXECUTED":
                rows.append({"op_id": op_id, "status": "MEASURED", "reason": None})
            else:
                rows.append({"op_id": op_id, "status": "NOT_SIMULATED", "reason": f"the simulation did not execute ({res.get('status')}: {res.get('reason')}); no count is claimed"})
        elif st == "failed":
            rows.append({"op_id": op_id, "status": "NOT_SIMULATED", "reason": f"the simulation failed ({rec.get('execution_reason')}); no count is claimed"})
        elif st == "pending":
            rows.append({"op_id": op_id, "status": "PENDING", "reason": "the simulation is queued or running"})
        else:
            rows.append({"op_id": op_id, "status": "NOT_SIMULATED", "reason": "measurable by the Harbor simulation; not requested yet"})
    for case, (rec, st) in by_case.items():
        if st == "completed" and rec.get("execution_result") and rec["execution_result"].get("status") == "EXECUTED":
            m = _measured(case, rec["execution_result"], [e["op_id"] for e in p["families"][case]])
            if not can_read_evidence:
                m = {k: v for k, v in m.items() if k not in ("edits", "config_hashes", "inert_edits", "no_change_edits", "input_data")}
                m["not_controlled"] = [{"behaviour": n.get("behaviour"), "reason": n.get("reason")} for n in m["not_controlled"]]
            measured.append(m)
    states = {st for _, st in by_case.values()}
    overall["status"] = ("not_requested" if not by_case or states == {"not_requested"} else "completed" if states == {"completed"} else "failed" if "failed" in states
                         else "pending" if "pending" in states else "partial")
    overall["measurable"] = bool(by_case)
    out["simulation"], out["measured"], out["simulation_state"] = rows, measured, overall
    if measured:
        out["measured_statement"] = "Measured rows come from running the pinned public Harbor copy on its public sample; they are facts about that sample under the proposed configuration, not a forecast."
