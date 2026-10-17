"""Stored AI reading of a SIMULATION result, one per (simulation job, facts hash).

The facts are collected here, on the server, from stored records (`infra/worker/sim_analysis.py::build_facts`); a client only names the
simulation job. The analysis is produced by the `sim.analysis` job (role `simulation.analyze`) and stored as that job's result, so reading it
is a plain database read and never a model call:

  GET  /v2/simulation-analyses?simulation_job_id=..   -> what is stored for the current facts, or why nothing is
  POST /v2/simulation-analyses                        -> create the job (idempotent: same facts, same job); a DIFFERENT person
                                                         must allow the model call, like every model call.

The key does not include the head revision: the same stored simulation keeps its analysis. `currentness` says whether the facts come from
the head revision or an older one, so an old reading is never presented as current.
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters.v2_policy import require, require_human_actor
from infra.v2_explain import _handle, _quota, currentness
from infra.v2_policy_routes import _authority, _error, _identity_store, _json_body, _origin_ok, _v2_store
from infra.worker import sim_analysis
from infra.worker.change_draft import _canon_json

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
KIND = "sim.analysis"


def snapshot_of(simulation_job_id: str, facts: dict) -> dict:
    input_hash = hashlib.sha256(_canon_json(facts).encode("utf-8")).hexdigest()
    key = hashlib.sha256(f"sim_analysis|{simulation_job_id}|{input_hash}".encode()).hexdigest()[:20]
    return {"key": key, "simulation_job_id": simulation_job_id, "input_hash": input_hash,
            "source_revision_id": (facts.get("facts_origin") or {}).get("source_revision_id")}


def job_id_for(snapshot: dict) -> str:
    return f"simanalysis-{snapshot['key']}"


MAX_RETRIES = 5


def _retry_ids(snapshot: dict) -> list[str]:
    base = job_id_for(snapshot)
    return [base] + [f"{base}-r{n}" for n in range(1, MAX_RETRIES + 1)]


def _stored_for_simulation(store, tenant, ws, simulation_job_id):
    """(job_id, record) of the newest COMPLETED reading of this simulation, whatever facts it was built from; None if there is none."""
    if not simulation_job_id:
        return None
    with store._tx() as cur:
        cur.execute("""SELECT j.job_id FROM v2_jobs j
                        WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind=%s AND j.execution_status='COMPLETED'
                          AND j.payload->'snapshot'->>'simulation_job_id'=%s
                        ORDER BY (SELECT max(o.id) FROM v2_outbox o WHERE o.tenant_id=j.tenant_id AND o.workspace_id=j.workspace_id
                                   AND o.kind='JOB_QUEUED' AND o.payload->>'job_id'=j.job_id) DESC NULLS LAST, j.job_id DESC LIMIT 1""",
                    (tenant, ws, KIND, simulation_job_id))
        row = cur.fetchone()
    if not row:
        return None
    try:
        return row[0], store.job_record(tenant, ws, row[0])
    except Exception:
        return None


def _state_of(store, tenant, ws, snapshot: dict) -> dict:
    """The state of the LATEST job for this snapshot. A failed/cancelled job never blocks the snapshot: an explicit retry makes the next one."""
    last, count = None, 0
    for jid in _retry_ids(snapshot):
        try:
            rec = store.job_record(tenant, ws, jid)
        except Exception:
            break
        last, count = (jid, rec), count + 1
    if last is None:
        other = _stored_for_simulation(store, tenant, ws, snapshot.get("simulation_job_id"))
        if other is not None:
            # A finished reading of the SAME simulation exists but was made from other facts (for example without the impact analysis):
            # show it, say so, and do not offer another paid call for what is already stored.
            out = _state_of_job(*other)
            out.update(attempts=1, retryable=False, context_differs=True)
            return out
        return {"status": "none", "job_id": None, "analysis": None, "attempts": 0, "retryable": False}
    out = _state_of_job(*last)
    out["attempts"] = count
    out["retryable"] = out["status"] in ("failed", "cancelled") and count <= MAX_RETRIES
    return out


def _state_of_job(jid: str, rec: dict) -> dict:
    st = rec.get("execution_status")
    status = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled"}.get(st)
    if status is None:
        status = "waiting_for_approval" if rec.get("status") in ("PENDING_APPROVAL",) else "running"
    res = rec.get("execution_result") if st == "COMPLETED" else None
    return {"status": status, "job_id": jid, "analysis": res, "model": rec.get("execution_model_id"),
            "reason": rec.get("execution_reason") if st == "FAILED" else None}


@router.get("/v2/simulation-analyses")
def simulation_analysis(request: Request, workspace_id: str, simulation_job_id: str, impact_job_id: str | None = None):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        facts = sim_analysis.build_facts(store, tenant, workspace_id, simulation_job_id, impact_job_id)
        snap = snapshot_of(simulation_job_id, facts)
        out = {"simulation_job_id": simulation_job_id, "snapshot": snap, "currentness": currentness(store, tenant, workspace_id, facts),
               **_state_of(store, tenant, workspace_id, snap)}
        return JSONResponse(out, headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


@router.post("/v2/simulation-analyses")
async def request_simulation_analysis(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    ws, sim_id, impact_id = body.get("workspace_id"), body.get("simulation_job_id"), body.get("impact_job_id")
    try:
        authority = _authority(identity, request, ws if isinstance(ws, str) else "")      # sign-in and rights BEFORE the body is looked at
        require_human_actor(authority)
        require(authority, "ai.use")
        if not all(isinstance(x, str) and x for x in (ws, sim_id)) or (impact_id is not None and not isinstance(impact_id, str)):
            return _error("workspace_id and simulation_job_id required", 400)
        tenant = authority["tenant_id"]
        facts = sim_analysis.build_facts(store, tenant, ws, sim_id, impact_id)
        snap = snapshot_of(sim_id, facts)
        jid = job_id_for(snap)
        state = _state_of(store, tenant, ws, snap)
        if state["status"] == "none" or (state["retryable"] and body.get("retry") is True):
            jid = _retry_ids(snap)[state["attempts"]]
            from infra import provider_gate
            provider_gate.require_provider(KIND)
            limit, reservation = _quota()
            head = store.head(tenant, ws)
            store.create_job(tenant, ws, jid, KIND, {"facts": facts, "snapshot": snap},
                             expected_version=head.version, idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit,
                             budget_reservation=reservation, requested_by=authority["account_id"])
            from infra import ai_admission
            ai_admission.admit(store, tenant, ws, jid, KIND, authority)
            state = _state_of(store, tenant, ws, snap)
        return JSONResponse({"simulation_job_id": sim_id, "snapshot": snap, **state}, headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)
