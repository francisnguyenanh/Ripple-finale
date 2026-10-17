"""What the sources of THIS revision have been checked for, and the steps that get it there - one place, idempotent, no AI.

A change draft is only as good as what stands behind it: the extraction of the files, and (for conflicts and gaps between sources) a finding job.
Relation coverage feeds the impact analysis. Before this module those steps existed but nothing ran them for a person: relation jobs came from a
button, finding jobs only from the re-analysis after an apply, and a missing finding was invisible - it could be read as "no conflict".

* `status()` reads, per kind, what exists for the CURRENT source revision: none / running / done / failed, and what belongs to older revisions
  (reported, never used silently).
* `ensure()` creates the missing relation and finding jobs for the current revision with DETERMINISTIC ids (revision + the exact set of candidates), so a second
  call, another tab or a redelivery creates nothing. Both kinds are deterministic executors (zero provider calls). A job that failed is left for
  the existing retry route (`/v2/jobs/{id}/retry`, bounded by the worker's own retry limit); a new job is only created when the candidate set changed.
Nothing here writes knowledge, approves, applies, or calls a model."""

from __future__ import annotations

import hashlib
import json

KINDS = ("relation", "finding")


def _candidates(store, tenant: str, ws: str, rev: str) -> tuple[list[str], dict]:
    """Candidate ids of the COMPLETED extraction jobs pinned to `rev`, and which artifacts have one."""
    from infra.v2_current_extraction import valid_extraction_jobs
    ids, artifacts = [], set()
    for job in valid_extraction_jobs(store, tenant, ws, rev):
        artifacts.add((job["payload"] or {}).get("artifact_id"))
        ids += [c["candidate_id"] for c in (job["result"].get("candidates") or []) if isinstance(c, dict) and c.get("candidate_id")]
    return sorted(dict.fromkeys(ids)), artifacts


def _jobs(store, tenant: str, ws: str, kind: str) -> list[dict]:
    with store._tx() as cur:
        cur.execute("""SELECT job_id, execution_status, pinned_source_revision_id, payload, coalesce(execution_reason, '') FROM v2_jobs
            WHERE tenant_id=%s AND workspace_id=%s AND kind=%s""", (tenant, ws, kind))
        rows = cur.fetchall()
    out = []
    for job_id, state, pinned, payload, reason in rows:
        p = json.loads(payload) if isinstance(payload, str) else (payload or {})
        out.append({"job_id": job_id, "state": state, "pinned": pinned, "candidate_ids": set(p.get("candidate_ids") or []), "reanalysis": bool(p.get("refresh_reanalysis")),
                    "reason": reason})
    return out


def _kind_state(jobs: list[dict], rev: str, candidates: list[str]) -> dict:
    """none / running / done / failed for ONE kind on THIS revision; jobs of other revisions are counted, not used."""
    want = set(candidates)
    here = [j for j in jobs if j["pinned"] == rev and (j["reanalysis"] or want <= j["candidate_ids"] or not want)]
    older = [j for j in jobs if j["pinned"] != rev and j["state"] == "COMPLETED"]
    done = [j for j in here if j["state"] == "COMPLETED"]
    running = [j for j in here if j["state"] not in ("COMPLETED", "FAILED", "CANCELLED")]
    failed = [j for j in here if j["state"] in ("FAILED", "CANCELLED")]
    state = "done" if done else "running" if running else "failed" if failed else "none"
    pick = (done or running or failed or [None])[0]
    return {"state": state, "job_id": pick["job_id"] if pick else None, "reason": (failed[0]["reason"] if failed and state == "failed" else None),
            "older_revisions_ignored": len(older)}


def status(store, tenant: str, ws: str) -> dict:
    head = store.head(tenant, ws)
    rev = head.source_revision_id
    out = {"source_revision_id": rev, "uses_ai": False}
    if not rev:
        return {**out, "extraction": {"files_in_scope": 0, "files_with_extraction": 0, "files_without_extraction": []}, "relation": {"state": "none"}, "finding": {"state": "none"}, "ready": False}
    view = store.source_revision_view(tenant, ws, rev)
    arts = {a["artifact_id"]: (a.get("logical_path") or a["artifact_id"]) for a in view["artifacts"]}
    cands, extracted = _candidates(store, tenant, ws, rev)
    have = {a for a in extracted if a in arts}
    out["extraction"] = {"files_in_scope": len(arts), "files_with_extraction": len(have), "candidates": len(cands),
                         "files_without_extraction": sorted(p for a, p in arts.items() if a not in have)}
    for kind in KINDS:
        out[kind] = _kind_state(_jobs(store, tenant, ws, kind), rev, cands)
    findings = 0
    if out["finding"]["state"] == "done":
        from infra.worker.verification import _completed_jobs
        findings = sum(len((j["result"] or {}).get("findings") or []) for j in _completed_jobs(store, tenant, ws, "finding") if j["pinned_source_revision_id"] == rev)
    out["finding"]["findings"] = findings
    out["ready"] = bool(cands) and out["relation"]["state"] == "done" and out["finding"]["state"] == "done"
    out["candidate_ids"] = cands
    return out


def _job_id(kind: str, rev: str, cands: list[str]) -> str:
    digest = hashlib.sha256(json.dumps([rev, cands], separators=(",", ":")).encode()).hexdigest()[:10]
    return f"cov-{kind}-{str(rev)[-8:]}-{digest}"


def ensure(store, tenant: str, ws: str) -> dict:
    """Create the missing relation/finding jobs of the current revision (idempotent), then return the status. Never creates a job when a valid one exists
    or is running, and never re-creates a FAILED one (retry is its own bounded route)."""
    from infra.worker.verification import ensure_job
    st = status(store, tenant, ws)
    rev, cands = st["source_revision_id"], st.get("candidate_ids") or []
    if rev and cands:
        for kind in KINDS:
            if st[kind]["state"] == "none":
                ensure_job(store, tenant, ws, _job_id(kind, rev, cands), kind, {"candidate_ids": cands})
        st = status(store, tenant, ws)
    return st
