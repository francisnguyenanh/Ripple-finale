"""Read-only server surfaces the browser shell (`/app`) needs and that no
reviewed route provided. Every route is ADDITIVE, GET-only, tenant/workspace scoped and uses the
same authority model as its neighbours in `infra/v2_policy_routes.py`: the caller's identity comes
only from the session (`_authority`), `project.read` is required, and evidence quotes are removed
server-side for a caller without `evidence.read` (same rule as `GET /v2/hitl/queue`).

Why each exists:

* `GET /v2/knowledge/overview` - claims, findings, current answers and the ACTIVE human decisions
  of the workspace. `GET /v2/knowledge/draft` and `GET /v2/hitl/queue` keep showing a claim as
  pending after `POST /v2/knowledge/confirm`, and a conflict finding produces no queue question at
  all, so without this read a reload could never show "human-confirmed" or list a conflict.
* `GET /v2/proposals/{id}/diff` - the exact before/after of a proposal (server-computed from the
  pinned source blob and the staged blob, hash-checked against the proposal result), so the
  approver reads the real diff and not a client reconstruction.
* `GET /v2/proposals/{id}/approvals` - approval jobs prepared for one proposal, in creation order,
  so a stale approval tab can find the newer bundle when the person asks it to reload.

None of these writes, none accepts an actor, and none changes the semantics of an existing route.
"""

from __future__ import annotations

import difflib
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra import v2_source_routes
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, permissions_for, require
from infra.adapters.v2_postgres import BlobIntegrityError, PostgresV2Store, TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
_MAX_DIFF_LINES = 400


def _denied(exc: IdentityDenied):
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


def _claim_view(candidate: dict, paths: dict, *, with_evidence: bool) -> dict:
    artifact_id = candidate.get("artifact_revision_id")
    source_revision = candidate.get("source_revision_id")
    view = {
        "candidate_id": candidate.get("candidate_id"), "subject": candidate.get("subject"),
        "predicate": candidate.get("predicate"), "value": candidate.get("value"),
        "unit": candidate.get("unit"), "modality": candidate.get("modality"),
        "scope": candidate.get("scope"), "outcome": candidate.get("outcome"),
        "applicability": candidate.get("applicability"),
        "applicability_reason": candidate.get("applicability_reason"),
        "artifact_id": artifact_id, "source_revision_id": source_revision,
        "logical_path": paths.get((source_revision, artifact_id)),
        "evidence_redacted": not with_evidence,
    }
    if with_evidence:
        view["evidence"] = [{"locator": e.get("locator"), "quote": e.get("quote")}
                            for e in (candidate.get("evidence") or []) if isinstance(e, dict)]
    return view


@router.get("/v2/knowledge/overview")
def knowledge_overview(request: Request, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        with_evidence = "evidence.read" in permissions_for(authority.get("roles"))
        tenant = authority["tenant_id"]
        with store._tx() as cur:
            cur.execute("""SELECT DISTINCT payload->>'artifact_id' FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'""",
                        (tenant, workspace_id))
            artifact_ids = sorted(r[0] for r in cur.fetchall() if r[0])
            cur.execute("""SELECT source_revision_id, artifact_id, logical_path
                FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s""",
                        (tenant, workspace_id))
            paths = {(r[0], r[1]): r[2] for r in cur.fetchall()}
            cur.execute("""SELECT decision_id, epoch, actor_id, target, action, provenance, scope,
                    superseded_by, revoked_by FROM v2_decisions
                WHERE tenant_id=%s AND workspace_id=%s AND action IS NOT NULL
                ORDER BY epoch""", (tenant, workspace_id))
            decision_rows = cur.fetchall()
            cur.execute("""SELECT DISTINCT ON (question_id) question_id, actor_id, answer_text,
                    version_after FROM v2_hitl_answers
                WHERE tenant_id=%s AND workspace_id=%s
                ORDER BY question_id, id DESC""", (tenant, workspace_id))
            answer_rows = cur.fetchall()
        claims, raw_findings, seen_claims, seen_findings = [], [], set(), set()
        for artifact_id in artifact_ids:
            draft = store.knowledge_draft(tenant, workspace_id, artifact_id)
            for candidate in draft["candidates"]:
                cid = candidate.get("candidate_id") if isinstance(candidate, dict) else None
                if cid and cid not in seen_claims:
                    seen_claims.add(cid)
                    claims.append(_claim_view(candidate, paths, with_evidence=with_evidence))
            for finding in draft["findings"]:
                fid = finding.get("finding_id") if isinstance(finding, dict) else None
                if fid and fid not in seen_findings:
                    seen_findings.add(fid)
                    raw_findings.append(finding)
        # one read model for "what is in force" (same as the Questions queue and the completion gate): repeats of one problem are one row, history is counted, not listed
        from infra import v2_current_findings as CF
        with store._tx() as cur:
            fmodel = CF.build(cur, tenant, workspace_id)
        shown = {v["finding_id"]: v for v in fmodel.current}
        raw_findings = [f for f in raw_findings if f["finding_id"] in shown or f["finding_id"] not in fmodel.by_finding_id]
        decisions = []
        for did, epoch, actor, target, action, provenance, scope, sup, rev in decision_rows:
            decisions.append({"decision_id": did, "epoch": epoch, "actor_id": actor,
                              "target": target, "action": action, "provenance": provenance,
                              "scope": scope if isinstance(scope, dict) else None,
                              "active": sup is None and rev is None})
        resolve_by_target = {d["target"]: d for d in decisions
                             if d["active"] and d["action"] == "resolve"}
        by_id = {c["candidate_id"]: c for c in claims}
        findings = []
        for finding in raw_findings:
            resolved = resolve_by_target.get(finding["finding_id"])
            findings.append({
                "finding_id": finding["finding_id"], "kind": finding.get("kind"),
                "severity": finding.get("severity"), "reason": finding.get("reason"),
                "authority_required": finding.get("authority_required"),
                "lifecycle_state": finding.get("lifecycle_state"),
                "involved_claim_ids": list(finding.get("involved_claim_ids") or []),
                "claims": [{k: by_id[cid].get(k) for k in
                            ("candidate_id", "subject", "predicate", "value", "unit", "scope",
                             "outcome", "artifact_id", "logical_path")}
                           for cid in (finding.get("involved_claim_ids") or []) if cid in by_id],
                "decision": None if resolved is None else {
                    "decision_id": resolved["decision_id"], "actor_id": resolved["actor_id"],
                    "authoritative_claim_id": (resolved["scope"] or {}).get("authoritative_claim_id")}})
        answers = [{"question_id": r[0], "actor_id": r[1], "answer_text": r[2], "version": r[3]}
                   for r in answer_rows]
        for item in findings:
            v = shown.get(item["finding_id"])
            if v:
                item.update({"runs": v["runs"], "plain": v["plain"], "todo": v["todo"], "current": True})
        return JSONResponse({"workspace_id": workspace_id, "claims": claims, "findings": findings, "finding_counts": fmodel.counts,
                             "answers": answers, "decisions": decisions}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/knowledge/graph")
def knowledge_graph(request: Request, workspace_id: str):
    """J1: read-only graph projection (see infra/v2_graph.py); no model call, no write."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        with_evidence = "evidence.read" in permissions_for(authority.get("roles"))
        from infra.v2_graph import build_graph
        return JSONResponse(build_graph(store, authority["tenant_id"], workspace_id, with_evidence=with_evidence),
                            headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/knowledge/timeline")
def knowledge_timeline(request: Request, workspace_id: str, node_id: str):
    """J2: per-rule timeline (analysis / knowledge decision / source change), lineage-aware; read-only."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        with_evidence = "evidence.read" in permissions_for(authority.get("roles"))
        from infra.v2_timeline import build_timeline
        result = build_timeline(store, authority["tenant_id"], workspace_id, node_id, with_evidence=with_evidence)
        if result is None:
            return _error("no timeline for this node", 404)
        return JSONResponse(result, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/support/matrix")
def support_matrix(request: Request, workspace_id: str):
    """The registered Harbor support matrix with live code evidence (read-only; see infra/v2_support.py)."""
    identity = _identity_store()
    if identity is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        from infra.v2_support import build_matrix
        return JSONResponse(build_matrix(), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)


@router.get("/v2/change/impact-context")
def change_impact_context(request: Request, workspace_id: str, target_candidate_id: str):
    """J3: one-snapshot inputs for an impact job (see infra/v2_graph.py::impact_context); read-only."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        from infra.v2_graph import impact_context
        result = impact_context(store, authority["tenant_id"], workspace_id, target_candidate_id)
        if result is None:
            return _error("target is not a current rule of this workspace", 404)
        return JSONResponse(result, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


def _proposal_job(store, tenant, workspace_id, job_id):
    job = store.job_record(tenant, workspace_id, job_id)
    if job["kind"] != "proposal":
        raise TenantScopeError("proposal not found")
    return job


@router.get("/v2/proposals/{proposal_job_id}/diff")
def proposal_diff(request: Request, proposal_job_id: str, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        job = _proposal_job(store, tenant, workspace_id, proposal_job_id)
        result = job.get("execution_result") or {}
        if job.get("execution_status") != "COMPLETED" or not result:
            return _error("proposal has not completed yet", 409)
        blobs = v2_source_routes._blob_port()
        dsn = os.environ.get("RIPPLE_V2_DSN", "")
        if blobs is None or not dsn:
            return _error("source storage unavailable", 503)
        blob_store = PostgresV2Store(dsn, blobs)
        pinned = (result.get("pinned_input") or {}).get("pinned_source_revision_id")
        target = result.get("target_artifact_id")
        try:
            before = blob_store.read_artifact_blob(tenant, workspace_id, pinned, target)
        except (TenantScopeError, BlobIntegrityError):
            return _error("proposal source unavailable", 409)
        is_no_op = bool(result.get("is_no_op"))
        after_bytes = before["content"]
        if not is_no_op:
            try:
                after_bytes = blob_store._verified_blob(result.get("staged_blob_hash"))
            except BlobIntegrityError:
                return _error("staged proposal bytes unavailable", 409)
        edit_kind = result.get("edit_kind")
        binary = edit_kind == "xlsx_typed_edit"
        lines, truncated = [], False
        if binary:
            for edit in result.get("edits") or []:
                where = f"{edit.get('sheet')}!{edit.get('cell')}"
                lines.append({"kind": "remove", "text": f"{where}: {edit.get('before')}"})
                lines.append({"kind": "add", "text": f"{where}: {edit.get('after')}"})
        elif not is_no_op:
            try:
                before_text = before["content"].decode("utf-8").splitlines()
                after_text = after_bytes.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                return _error("proposal is not a text diff", 409)
            for raw in difflib.unified_diff(before_text, after_text, n=2, lineterm=""):
                if raw.startswith(("---", "+++")):
                    continue
                if raw.startswith("@@"):
                    lines.append({"kind": "hunk", "text": raw})
                elif raw.startswith("+"):
                    lines.append({"kind": "add", "text": raw[1:]})
                elif raw.startswith("-"):
                    lines.append({"kind": "remove", "text": raw[1:]})
                else:
                    lines.append({"kind": "context", "text": raw[1:] if raw.startswith(" ") else raw})
                if len(lines) >= _MAX_DIFF_LINES:
                    truncated = True
                    break
        return JSONResponse({
            "proposal_job_id": proposal_job_id, "target_artifact_id": target,
            "logical_path": before.get("logical_path"), "edit_kind": edit_kind,
            "is_no_op": is_no_op, "binary": binary,
            "before_blob_hash": result.get("before_blob_hash"),
            "after_blob_hash": result.get("after_blob_hash"),
            "classification": result.get("classification"), "lines": lines,
            "truncated": truncated}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/proposals/{proposal_job_id}/approvals")
def proposal_approvals(request: Request, proposal_job_id: str, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        _proposal_job(store, tenant, workspace_id, proposal_job_id)
        with store._tx() as cur:
            # Creation order = the JOB_QUEUED outbox row (BIGSERIAL id) written in the same
            # transaction as the job row; v2_jobs itself has no timestamp.
            cur.execute("""SELECT j.job_id, j.execution_status,
                    j.execution_result->>'approval_digest',
                    (SELECT min(o.id) FROM v2_outbox o
                      WHERE o.tenant_id=j.tenant_id AND o.workspace_id=j.workspace_id
                        AND o.kind='JOB_QUEUED' AND o.payload->>'job_id'=j.job_id) AS seq
                FROM v2_jobs j
                WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind='approval'
                  AND j.payload->>'proposal_job_id'=%s
                ORDER BY seq NULLS LAST, j.job_id""", (tenant, workspace_id, proposal_job_id))
            rows = cur.fetchall()
        approvals = [{"job_id": r[0], "execution_status": r[1], "approval_digest": r[2]}
                     for r in rows]
        return JSONResponse({"proposal_job_id": proposal_job_id, "approvals": approvals,
                             "latest_job_id": approvals[-1]["job_id"] if approvals else None},
                            headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
