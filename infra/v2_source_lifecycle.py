"""U1: `GET /v2/sources/lifecycle` - what has really happened to each source file of the workspace head, read from the store.

One row per file of the CURRENT source revision, with the stages kept apart (never merged into one "done"):

* imported   - the bytes are in the store for this revision (every row here is).
* parsed     - observations exist for the file (a file the parser has no reader for shows `no_observations`, not "parsed").
* extraction - `not_analysed` | `pending_approval` | `queued` | `running` | `analysed` | `failed` | `cancelled`, plus `stale` when the
               only analysis was made on an OLDER revision of the same file. Taken from the real `extraction` jobs of that file.
* claims     - how many candidate rules the current analysis produced, how many a person has confirmed and how many are still pending.
* knowledge  - whether the workspace head's knowledge revision is built on this source revision (acceptance is a revision-level fact;
               it is never derived from an extraction job being COMPLETED).

Read only, tenant/workspace scoped, `project.read`. It returns counts, ids and names but NO evidence quotes, so it needs no extra
redaction. `provider_mode` is what the server operator declared (`RIPPLE_PROVIDER_MODE`); a model-shaped `model_id` in a job record is
NOT evidence of a live call and is reported as `model_recorded`, next to that declared mode.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require
from infra.adapters.v2_postgres import TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def provider_mode() -> dict:
    declared = (os.environ.get("RIPPLE_PROVIDER_MODE") or "").strip().upper()
    if declared == "SIMULATED":
        return {"mode": "SIMULATED", "note": "This server runs stub providers (rules, not a model). No model was called and nothing was spent."}
    if declared == "LIVE_DISARMED":
        return {"mode": "LIVE_DISARMED", "note": "Live-local server: data is kept between restarts and no stub is used, but provider calls are refused until the operator arms it with an approval file. Nothing was sent to an AI."}
    if declared == "LIVE":
        return {"mode": "LIVE", "note": "This server was declared to use the live provider. Per-run cost and model are recorded on each job."}
    return {"mode": "NOT_DECLARED", "note": "The operator did not declare whether this server uses a live or a simulated provider, so a recorded model name is not shown as proof of a live call."}


def _extraction_state(job: dict | None, *, current: bool) -> str:
    if job is None:
        return "not_analysed"
    es, st = job["execution_status"], job["status"]
    if es == "COMPLETED":
        return "analysed" if current else "stale"
    if es == "FAILED":
        return "failed" if current else "stale"
    if es == "CANCELLED" or st == "CANCELLED":
        return "cancelled" if current else "not_analysed"
    if st in ("PENDING_APPROVAL",):
        return "pending_approval" if current else "not_analysed"
    if es == "RUNNING":
        return "running" if current else "not_analysed"
    return "queued" if current else "not_analysed"


@router.get("/v2/sources/lifecycle")
def source_lifecycle(request: Request, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        head = store.head(tenant, workspace_id)
        rev = head.source_revision_id
        mode = provider_mode()
        if not rev:
            return JSONResponse({"workspace_id": workspace_id, "source_revision_id": None, "provider": mode, "artifacts": [],
                                 "totals": {"imported": 0}, "knowledge": {"state": "no_sources"}}, headers=_NO_STORE)
        with store._tx() as cur:
            cur.execute("""SELECT artifact_id, logical_path, media_type, byte_size, source_role, blob_hash
                FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                ORDER BY logical_path""", (tenant, workspace_id, rev))
            arts = [dict(zip(("artifact_id", "logical_path", "media_type", "byte_size", "source_role", "blob_hash"), r)) for r in cur.fetchall()]
            cur.execute("""SELECT source_revision_id, artifact_id, blob_hash FROM v2_artifact_revisions
                WHERE tenant_id=%s AND workspace_id=%s""", (tenant, workspace_id))
            hash_at = {(r[0], r[1]): r[2] for r in cur.fetchall()}
            cur.execute("""SELECT artifact_id, count(*) FROM v2_source_observations
                WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s GROUP BY artifact_id""", (tenant, workspace_id, rev))
            obs = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("""SELECT source_revision_id, artifact_id, logical_path FROM v2_artifact_revisions
                WHERE tenant_id=%s AND workspace_id=%s""", (tenant, workspace_id))
            path_of = {(r[0], r[1]): r[2] for r in cur.fetchall()}
            cur.execute("""SELECT j.job_id, j.status, j.execution_status, j.execution_reason, j.pinned_source_revision_id,
                       j.payload->>'artifact_id', j.execution_model_id, j.execution_cost_usd, j.execution_result,
                       COALESCE((SELECT max(o.id) FROM v2_outbox o WHERE o.tenant_id=j.tenant_id AND o.workspace_id=j.workspace_id
                                  AND o.kind='JOB_QUEUED' AND o.payload->>'job_id'=j.job_id), 9223372036854775807) AS ord,
                       (SELECT max(a.recorded_at) FROM v2_job_attempts a WHERE a.tenant_id=j.tenant_id AND a.workspace_id=j.workspace_id
                                  AND a.job_id=j.job_id) AS last_attempt
                FROM v2_jobs j WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind='extraction'""", (tenant, workspace_id))
            jobs = []
            for r in cur.fetchall():
                path = path_of.get((r[4], r[5]))
                jobs.append({"job_id": r[0], "status": r[1], "execution_status": r[2], "reason": r[3], "source_revision_id": r[4],
                             "artifact_id": r[5], "path": path, "model": r[6], "cost_usd": str(r[7]) if r[7] is not None else None,
                             "result": r[8] if isinstance(r[8], dict) else None, "ord": r[9],
                             "last_run_at": r[10].isoformat() if r[10] is not None else None})
            cur.execute("""SELECT target FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s AND action='confirm'
                AND superseded_by IS NULL AND revoked_by IS NULL""", (tenant, workspace_id))
            confirmed_ids = {r[0] for r in cur.fetchall()}
            cur.execute("""SELECT source_revision_id, decision_epoch FROM v2_knowledge_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s""", (tenant, workspace_id, head.knowledge_revision_id))
            kr = cur.fetchone()
        rows, totals = [], {"imported": len(arts), "parsed": 0, "analysed": 0, "stale": 0, "pending_approval": 0, "queued": 0, "running": 0,
                            "failed": 0, "not_analysed": 0, "cancelled": 0, "claims_total": 0, "claims_pending": 0, "claims_confirmed": 0}
        for a in arts:
            mine = sorted((j for j in jobs if j["path"] == a["logical_path"]), key=lambda j: j["ord"])
            current_jobs = [j for j in mine if j["source_revision_id"] == rev]
            job = current_jobs[-1] if current_jobs else None
            older = mine[-1] if (mine and not current_jobs) else None
            # An earlier completed analysis is still valid when the file's bytes are identical at the head (unknown hash = changed).
            unchanged = bool(older and older["execution_status"] == "COMPLETED" and a.get("blob_hash")
                             and hash_at.get((older["source_revision_id"], older["artifact_id"])) == a["blob_hash"])
            state = (_extraction_state(job, current=True) if job else
                     ("analysed" if unchanged else "stale" if older and older["execution_status"] == "COMPLETED" else "not_analysed"))
            shown = job or older
            cands = []
            src = job or (older if unchanged else None)
            if src and state == "analysed" and src["result"]:
                cands = [c.get("candidate_id") for c in (src["result"].get("candidates") or []) if isinstance(c, dict) and c.get("candidate_id")]
            conf = sum(1 for c in cands if c in confirmed_ids)
            n_obs = obs.get(a["artifact_id"], 0)
            row = {"artifact_id": a["artifact_id"], "logical_path": a["logical_path"], "media_type": a["media_type"],
                   "byte_size": a["byte_size"], "source_role": a["source_role"],
                   "parse": {"state": "parsed" if n_obs else "no_observations", "observations": n_obs},
                   "extraction": {"state": state, "job_id": shown["job_id"] if shown else None,
                                  "analysed_source_revision_id": shown["source_revision_id"] if shown else None,
                                  "on_current_revision": bool(job), "file_unchanged_since_analysis": unchanged and not job, "last_run_at": shown["last_run_at"] if shown else None,
                                  "model_recorded": shown["model"] if shown else None, "cost_usd": shown["cost_usd"] if shown else None,
                                  "reason": (shown["reason"] if shown and shown["execution_status"] == "FAILED" else None)},
                   "claims": {"total": len(cands), "confirmed": conf, "pending": len(cands) - conf}}
            rows.append(row)
            totals["parsed"] += 1 if n_obs else 0
            totals[state] = totals.get(state, 0) + 1
            totals["claims_total"] += len(cands); totals["claims_confirmed"] += conf; totals["claims_pending"] += len(cands) - conf
        if head.knowledge_revision_id and kr:
            knowledge = {"state": "built_on_current_sources" if kr[0] == rev else "built_on_older_sources",
                         "knowledge_revision_id": head.knowledge_revision_id, "knowledge_source_revision_id": kr[0],
                         "decision_epoch": head.decision_epoch}
        else:
            knowledge = {"state": "no_knowledge_revision", "decision_epoch": head.decision_epoch}
        return JSONResponse({"workspace_id": workspace_id, "source_revision_id": rev, "provider": mode, "artifacts": rows,
                             "totals": totals, "knowledge": knowledge}, headers=_NO_STORE)
    except IdentityDenied as exc:
        status = _identity_denied_status(exc)
        return _error("sign in required" if status == 401 else "permission denied", status)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
