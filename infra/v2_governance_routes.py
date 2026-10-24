"""Governance routes (MON-POL, MON-AUD; local build):

POST /v2/business/policy/decide   {workspace_id, facts}         typed decision ALLOW | DENY | REQUIRES_APPROVAL | INDETERMINATE over the ACCEPTED knowledge, audited   project.read
POST /v2/business/audit/export    {workspace_id, change_id?}    scoped audit package (zip: records, coverage, MANIFEST.json, standalone verifier)                    project.read (+ evidence rights for refs)
POST /v2/business/roi/replay      {workspace_id, records, baseline_revision, target_revision, assumptions, window_from?, window_to?, label?}   one scoped compare request (MON-ROI engine): the caller's records replayed through two ACCEPTED knowledge versions; report only, audited; no data is stored   project.read

POST /v2/business/ci/semantic-drift  {workspace_id, repo_id, base, head, attempt?}   SUR-04 semantic drift ADVISORY over a declared repository's diff next to the exact MON-CI check (one model job; never a PASS)   ai.use + evidence.read, human actor, visible repository
POST /v2/business/roi/analyst  {workspace_id, records, baseline_revision, target_revision, assumptions, label, synthetic: true, attempt?}   SUR-07 analyst memo over a SYNTHETIC replay (engine computes every number; real data refused)   project.read + ai.use, human actor
GET  /v2/business/roi/analyst/{job_id}  ?workspace_id=   one memo (records never returned)   project.read (asker or knowledge.confirm)
GET  /v2/business/ci/semantic-drift/{job_id}  ?workspace_id=                           state, exact result and advisory findings side by side (reads only)   project.read + evidence.read

The decision route never calls a model and never writes knowledge; it records one audit event (facts digest, decision, pin). The context route stays what it is (an evidence pack,
no ALLOW). An external agent must re-check the pin when it acts: the response is not a bearer authorization.
"""
from __future__ import annotations

from infra.db_offload import db_route
from infra.v2_policy_routes import _json_body_sync
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from psycopg2.extras import Json

from core.change_intelligence.v2 import policy_decision, roi_replay
from infra.adapters import v2_audit_pack, v2_business
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, permissions_for, require
from infra.adapters.v2_postgres import TenantScopeError
from infra.v2_business_routes import _denied
from infra.v2_policy_routes import _authority, _error, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
AUDIT_EVENT = "POLICY_DECISION"


@router.post("/v2/business/policy/decide")
@db_route
def policy_decide(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id, facts = body.get("workspace_id"), body.get("facts")
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if not isinstance(facts, dict) or not facts:
            return _error("facts (object) required", 400)
        tenant = authority["tenant_id"]
        view = v2_business.business_view(store, tenant, workspace_id)
        acc = view["accepted"]
        if acc is None:
            return JSONResponse({"decision": "INDETERMINATE", "indeterminate_reasons": ["no_accepted_knowledge"], "statement": "No business knowledge has been accepted; nothing can be decided."},
                                status_code=200, headers=_NO_STORE)
        pin = {"snapshot_id": acc["snapshot"]["snapshot_id"], "business_revision": acc["snapshot"]["business_revision"], "source_revision_id": acc["snapshot"]["source_revision_id"],
               "observed_stale": view["observed_stale"]}
        try:
            out = policy_decision.decide(acc["rules"], acc["gaps"], facts, pin=pin)
        except ValueError as exc:
            return _error(str(exc), 400)
        if view["observed_stale"]:
            out["indeterminate_reasons"].append("sources_moved_after_accepted_knowledge")
            out["decision"], out["statement"] = "INDETERMINATE", "The sources moved after the accepted knowledge; the knowledge must be refreshed and accepted again before a decision."
            out["decision_hash"] = policy_decision.canonical_hash({k: out[k] for k in ("decision", "obligations", "effects", "indeterminate_reasons", "facts_digest", "pin")})
        if "evidence.read" not in permissions_for(authority.get("roles")):
            for r in out["applicable_rules"]:
                r.pop("rule_id", None)
            out["superseded_observed_rules"] = len(out["superseded_observed_rules"])
            for o in out["obligations"] + out["effects"]:
                o.pop("rule_id", None)
        with store._tx() as cur:
            cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)",
                        (tenant, workspace_id, AUDIT_EVENT, authority["account_id"], Json({"decision": out["decision"], "facts_digest": out["facts_digest"], "decision_hash": out["decision_hash"], "pin": pin,
                                                                                            "obligations": len(out["obligations"]), "indeterminate": len(out["indeterminate_reasons"])})))
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/business/audit/export")
@db_route
def audit_export(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id, change_id = body.get("workspace_id"), body.get("change_id")
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    if change_id is not None and (not isinstance(change_id, str) or not change_id.startswith("chg-")):
        return _error("change_id must be a change id", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        if change_id:
            rec = store.job_record(tenant, workspace_id, change_id)
            if rec.get("kind") != "change.understand":
                raise TenantScopeError("change request not found")
        data, manifest = v2_audit_pack.build(store, tenant, workspace_id, change_id=change_id, can_read_evidence="evidence.read" in permissions_for(authority.get("roles")), requested_by=authority["account_id"])
        with store._tx() as cur:
            cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)",
                        (tenant, workspace_id, "AUDIT_PACK_EXPORTED", authority["account_id"], Json({"change_id": change_id, "files": len(manifest["files"]), "generated_at": manifest["generated_at"]})))
        name = f"ripple-audit-{workspace_id}" + (f"-{change_id}" if change_id else "") + ".zip"
        return Response(content=data, media_type="application/zip", headers={**_NO_STORE, "Content-Disposition": f'attachment; filename="{name}"', "X-Audit-Files": str(len(manifest["files"]))})
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)

MAX_REPLAY_RECORDS = 10_000


@router.post("/v2/business/roi/replay")
@db_route
def roi_replay_route(request: Request):
    """MON-ROI, one scoped compare request: the caller supplies the records (sanitised, authorised by them), names two ACCEPTED business revisions and the declared assumptions;
    the server replays the same cohort through both versions with the deterministic evaluator and returns the report. Nothing is stored except one audit row (hashes and
    denominators, never the records). Engineering path only: a real sample, its data-quality review and the data owner's acceptance are the package's completion gate."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    records, assumptions = body.get("records"), body.get("assumptions")
    if not isinstance(records, list) or not records or len(records) > MAX_REPLAY_RECORDS or not all(isinstance(r, dict) for r in records):
        return _error(f"records: a non-empty list of at most {MAX_REPLAY_RECORDS} objects is required", 400)
    if not isinstance(assumptions, dict):
        return _error("assumptions (object with minutes_per_approval) required", 400)
    revisions = body.get("baseline_revision"), body.get("target_revision")
    if not all(isinstance(r, int) and not isinstance(r, bool) and r >= 1 for r in revisions):
        return _error("baseline_revision and target_revision must be accepted business revision numbers", 400)
    for key in ("window_from", "window_to", "label"):
        if body.get(key) is not None and not isinstance(body.get(key), str):
            return _error(f"{key} must be a string", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        with v2_business.read_snapshot(store) as cur:
            base, target = (v2_business._accepted_by_revision(cur, tenant, workspace_id, n) for n in revisions)
        if base is None or target is None:
            return JSONResponse({"error": "both revisions must be accepted business knowledge versions of this workspace", "code": "unknown_reference"}, status_code=409, headers=_NO_STORE)
        policies = [{"label": f"business revision {n}", "rules": snap["rules"], "gaps": snap["gaps"]} for n, snap in zip(revisions, (base, target))]
        try:
            report = roi_replay.replay(records, policies[0], policies[1], assumptions=assumptions, window_from=body.get("window_from"), window_to=body.get("window_to"),
                                       dataset_label=str(body.get("label") or ""))
        except ValueError as exc:
            return _error(str(exc), 400)
        report["pin"] = {"workspace_id": workspace_id, "baseline": {"business_revision": revisions[0], "snapshot_id": base["snapshot"]["snapshot_id"]},
                         "target": {"business_revision": revisions[1], "snapshot_id": target["snapshot"]["snapshot_id"]}}
        with store._tx() as cur:
            cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)",
                        (tenant, workspace_id, "ROI_REPLAY_RUN", authority["account_id"],
                         Json({"baseline_revision": revisions[0], "target_revision": revisions[1], "dataset_hash": report["dataset"]["hash"], "report_hash": report["report_hash"],
                               "denominators": report["denominators"], "label": report["dataset"]["label"]})))
        return JSONResponse(report, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


# ---- SUR-04: semantic drift advisory next to the exact MON-CI check -------------------------------------------------------------------------------------------------

def _resolve_commit(repo_path: str, ref) -> str | None:
    import subprocess
    if not isinstance(ref, str) or not ref.strip() or ref.startswith("-") or len(ref) > 200:
        return None
    r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref.strip() + "^{commit}"], cwd=repo_path, capture_output=True, text=True, timeout=30)
    out = r.stdout.strip()
    return out if r.returncode == 0 and len(out) == 40 else None


@router.post("/v2/business/ci/semantic-drift")
@db_route
def request_semantic_drift(request: Request):
    """SUR-04: ask a model for a semantic drift ADVISORY over a declared repository's diff (base..head resolved to SHAs now) against the ACCEPTED knowledge; the exact MON-CI
    scalar check runs in the same job and is stored unchanged. Human actor, `ai.use` and `evidence.read` (the diff is evidence); the repository must be visible to the
    caller. One job per repository, SHAs, accepted snapshot, attempt and frozen config + pinned model. Never a PASS; nothing is written to the repository."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id, repo_id, attempt = body.get("workspace_id"), body.get("repo_id"), body.get("attempt", 1)
    if not (isinstance(workspace_id, str) and workspace_id.strip()) or not isinstance(repo_id, str) or isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 3:
        return _error("workspace_id, repo_id and attempt (1-3) required", 400)
    try:
        from infra.adapters.v2_policy import require_human_actor
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "ai.use")
        require(authority, "evidence.read")
        tenant = authority["tenant_id"]
        from infra.adapters import v2_code_change, v2_semantic_drift
        from infra.worker import code_change
        from infra.worker.execution_types import NonRetryableExecutionError
        if not v2_code_change.repo_visible(workspace_id, repo_id, authority.get("roles")):
            raise TenantScopeError("repository not found")
        try:
            repo = code_change.repo_config(workspace_id, repo_id)
        except NonRetryableExecutionError as exc:
            raise TenantScopeError("repository not found") from exc
        base, head = _resolve_commit(repo["path"], body.get("base")), _resolve_commit(repo["path"], body.get("head"))
        if base is None or head is None:
            return JSONResponse({"error": "base and head must name commits of the repository", "code": "unknown_reference"}, status_code=409, headers=_NO_STORE)
        acc = v2_business.business_view(store, tenant, workspace_id)["accepted"]
        if acc is None:
            return JSONResponse({"error": "No business knowledge has been accepted; there is nothing to compare against.", "code": "no_accepted_knowledge"}, status_code=409, headers=_NO_STORE)
        snapshot_id = acc["snapshot"]["snapshot_id"]
        jid = v2_semantic_drift.job_id(repo_id, base, head, snapshot_id, attempt)
        from infra.v2_business_routes import _quota_for
        out = v2_semantic_drift.request(store, tenant, workspace_id, jid, {"repo_id": repo_id, "base_sha": base, "head_sha": head, "snapshot_id": snapshot_id,
                                                                           "attempt": attempt}, authority, quota=_quota_for)
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        from infra.v2_business_routes import _refusal
        return _refusal(exc)


@router.get("/v2/business/ci/semantic-drift/{job_id}")
def read_semantic_drift(request: Request, job_id: str, workspace_id: str):
    """The advisory of one request: state, the exact MON-CI result and the advisory findings side by side. Reads only; `evidence.read` (witness lines are code)."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        require(authority, "evidence.read")
        tenant = authority["tenant_id"]
        if not job_id.startswith("csd-"):
            raise TenantScopeError("advisory not found")
        rec = store.job_record(tenant, workspace_id, job_id)
        from infra.adapters import v2_code_change, v2_semantic_drift
        if rec.get("kind") != v2_semantic_drift.KIND or not v2_code_change.repo_visible(workspace_id, (rec.get("payload") or {}).get("repo_id", ""), authority.get("roles")):
            raise TenantScopeError("advisory not found")
        return JSONResponse(v2_semantic_drift.state_of(rec), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


# ---- SUR-07: ROI analyst memo over a SYNTHETIC replay dataset ------------------------------------------------------------------------------------------------------------

@router.post("/v2/business/roi/analyst")
@db_route
def request_roi_analyst(request: Request):
    """SUR-07: an analyst memo (statements citing metric keys, assumption questions, sensitivity values the engine computes) over a replay of a dataset marked SYNTHETIC.
    Real data is refused (`real_data_not_granted`) until a data owner grants it. Human actor, `project.read` + `ai.use`. The records are kept in the job only to recompute
    the replay; they are never echoed."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    records, assumptions, attempt, label = body.get("records"), body.get("assumptions"), body.get("attempt", 1), body.get("label")
    revisions = body.get("baseline_revision"), body.get("target_revision")
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    from infra.worker import roi_analyst
    if body.get("synthetic") is not True or not isinstance(label, str) or not label.upper().startswith(roi_analyst.SYNTHETIC_PREFIX):
        return JSONResponse({"error": "Only a dataset marked synthetic (synthetic: true, label starting with SYNTHETIC) can be analysed; real data has not been granted by a data owner.",
                             "code": "real_data_not_granted"}, status_code=409, headers=_NO_STORE)
    limit = json.loads(roi_analyst.CONFIG_PATH.read_text(encoding="utf-8"))["limits"]["max_records"]
    if not isinstance(records, list) or not records or len(records) > limit or not all(isinstance(r, dict) for r in records) or not isinstance(assumptions, dict):
        return _error(f"records (1-{limit} objects) and assumptions required", 400)
    if not all(isinstance(r, int) and not isinstance(r, bool) and r >= 1 for r in revisions) or isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 3:
        return _error("baseline_revision, target_revision (accepted revision numbers) and attempt (1-3) required", 400)
    try:
        from infra.adapters.v2_policy import require_human_actor
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "project.read")
        require(authority, "ai.use")
        tenant = authority["tenant_id"]
        with v2_business.read_snapshot(store) as cur:
            if any(v2_business._accepted_by_revision(cur, tenant, workspace_id, n) is None for n in revisions):
                return JSONResponse({"error": "both revisions must be accepted business knowledge versions of this workspace", "code": "unknown_reference"}, status_code=409, headers=_NO_STORE)
        from infra.adapters import v2_roi_analyst
        from infra.v2_business_routes import _quota_for
        jid = v2_roi_analyst.job_id(records, revisions[0], revisions[1], assumptions, label, attempt)
        payload = {"records": records, "assumptions": assumptions, "baseline_revision": revisions[0], "target_revision": revisions[1], "label": label, "synthetic": True,
                   "window_from": body.get("window_from") if isinstance(body.get("window_from"), str) else None,
                   "window_to": body.get("window_to") if isinstance(body.get("window_to"), str) else None, "attempt": attempt}
        return JSONResponse(v2_roi_analyst.request(store, tenant, workspace_id, jid, payload, authority, quota=_quota_for), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        from infra.v2_business_routes import _refusal
        return _refusal(exc)


@router.get("/v2/business/roi/analyst/{job_id}")
def read_roi_analyst(request: Request, job_id: str, workspace_id: str):
    """One analyst memo (reads only; the records are not returned). `project.read`; visible to the person who asked and to holders of `knowledge.confirm`."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        if not job_id.startswith("roa-"):
            raise TenantScopeError("memo not found")
        rec = store.job_record(tenant, workspace_id, job_id)
        if rec.get("kind") != "roi.analyst" or (rec.get("requested_by") != authority["account_id"] and "knowledge.confirm" not in permissions_for(authority.get("roles"))):
            raise TenantScopeError("memo not found")
        from infra.adapters import v2_roi_analyst
        return JSONResponse(v2_roi_analyst.state_of(rec), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
