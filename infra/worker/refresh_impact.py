"""What a committed manual source refresh invalidates, and the status view.

Consumer of the durable `SOURCE_PUBLISHED{trigger:"refresh"}` outbox event written by
`PostgresV2Store.commit_source_refresh`. It runs inside the existing dispatcher
(`job_worker.claim_and_admit_one`); it never publishes a source revision, never
moves the head, and never writes a decision, an approval or a ChangeSet.

Conservative stale policy (`STALE_POLICY`, stated in the status view):

* dependents (`relation` / `finding` jobs) of a candidate whose evidence artifact was
  CHANGED or DELETED by the refresh are stale, whether or not the cited text
  survived -- Ripple cannot prove a changed artifact still means the same thing;
* dependents of an artifact the refresh left UNCHANGED stay valid (their binding is
  still valid); the head knowledge revision is always dropped by any source publish;
* an active human decision stays KEPT only when its candidate's binding is valid:
  the cited quote still exists in the current bytes. A deleted artifact or a missing
  quote invalidates the binding (reported, never rewritten: the decision ledger is
  append-only), and a binding Ripple cannot verify (binary artifact, no quote) is
  reported as `unknown_dependency` and not kept.

Work is idempotent and bounded by deterministic ids: one `inval-...` invalidation job
per affected candidate (its dependents are marked in `v2_stale_markers` under
`decision_id = "refresh-<revision>"` and re-analysed within the shared
`max_reanalysis_jobs_per_decision` budget, causation recorded), at most ONE
re-extraction per changed artifact and ONE findings job per refresh. A conflicting
external edit therefore opens a finding through the ordinary findings executor; it is
never auto-accepted and never reverted.
"""

from __future__ import annotations

import hashlib
import os
import re

from infra.adapters.v2_postgres import (
    BlobIntegrityError, ConcurrencyLimitExceeded, StoreConflict, TenantScopeError,
)
from infra.worker import job_quota
from infra.worker.verification import StageIdConflict, ensure_job

STALE_POLICY = "conservative"
#: Upper bound of candidates a single refresh invalidates / decisions it reports.
MAX_TARGETS = 200
_TERMINAL_BAD = ("FAILED", "CANCELLED")
_LIVE = ("QUEUED", "RUNNING")


def refresh_decision_id(revision_id: str) -> str:
    """The id stamped on stale markers / causation of this refresh (no decision row)."""
    return f"refresh-{revision_id}"


def _short(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


# ----------------------------------------------------------------------------- assessment

def assess(store, tenant_id: str, workspace_id: str, refresh: dict, head) -> dict:
    """Read-only assessment of the affected candidates against the CURRENT head bytes."""
    diff = refresh["diff"]
    deleted_ids = {d["artifact_id"] for d in diff["deleted"]}
    affected = {c["artifact_id"] for c in diff["changed"]} | deleted_ids
    index = store.extraction_candidate_index(tenant_id, workspace_id)
    texts: dict = {}

    def current_text(artifact_id: str):
        if artifact_id not in texts:
            try:
                raw = store.read_blob(tenant_id, workspace_id, head.source_revision_id,
                                      artifact_id)
                texts[artifact_id] = _norm(raw.decode("utf-8"))
            except UnicodeDecodeError:
                texts[artifact_id] = None  # binary artifact: the quote cannot be re-read
            except (TenantScopeError, KeyError, FileNotFoundError, BlobIntegrityError):
                texts[artifact_id] = False  # not in the current head / unreadable
        return texts[artifact_id]

    targets, seen, all_candidates = [], set(), []
    for job in index:
        for cand in job["candidates"]:
            cid, artifact_id = cand["candidate_id"], cand["artifact_id"]
            if cid in seen:
                continue
            seen.add(cid)
            all_candidates.append(cid)
            if artifact_id not in affected:
                continue
            if artifact_id in deleted_ids:
                verdict, reason, missing = "orphaned", "the source artifact was deleted", []
            else:
                text = current_text(artifact_id)
                if text is False:
                    verdict, reason, missing = ("orphaned",
                                                "the source artifact is not in the current source", [])
                elif text is None or not cand["quotes"]:
                    verdict, reason, missing = (
                        "unverifiable",
                        "the cited evidence cannot be re-checked against the changed source", [])
                else:
                    missing = [q for q in cand["quotes"] if _norm(q) not in text]
                    if missing:
                        verdict, reason = "orphaned", "a cited quote no longer exists in the current source"
                    else:
                        verdict, reason = ("changed",
                                           "the source artifact changed; the cited text is still present")
            targets.append({"candidate_id": cid, "artifact_id": artifact_id, "verdict": verdict,
                            "reason": reason, "missing_quotes": missing[:5]})
    targets.sort(key=lambda t: t["candidate_id"])
    truncated = len(targets) > MAX_TARGETS
    targets = targets[:MAX_TARGETS]

    reextract = {}
    target_artifacts = {t["artifact_id"] for t in targets}
    for job in index:
        aid = job["artifact_id"]
        if aid in target_artifacts and aid not in deleted_ids and aid in affected:
            reextract[aid] = {"artifact_id": aid, "source_role": job["source_role"]}
    decisions = store.active_decisions_for_targets(
        tenant_id, workspace_id, all_candidates[:MAX_TARGETS * 5])
    decisions = [d for d in decisions if d["action"] in ("confirm", "resolve", "accepted_unknown")]
    return {"schema": 1, "computed_for_revision": head.source_revision_id,
            "targets": targets, "truncated": truncated,
            "reextract": [reextract[k] for k in sorted(reextract)],
            "decisions": decisions[:MAX_TARGETS],
            "bound_candidate_ids": sorted({d["target"] for d in decisions})[:MAX_TARGETS],
            "notes": (["more than %d affected candidates: the remainder was not scheduled "
                       "(reported, not silently dropped)" % MAX_TARGETS] if truncated else [])}


# ----------------------------------------------------------------------------- consumer

def _audit_conflict(store, tenant_id, workspace_id, refresh_id, job_id, kind, error) -> None:
    from psycopg2.extras import Json
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload)
            VALUES (%s,%s,'STAGE_JOB_ID_CONFLICT','service',%s)""",
                    (tenant_id, workspace_id,
                     Json({"refresh_id": refresh_id, "conflicts": [
                         {"job_id": job_id, "kind": kind, "error": error}]})))


def _create_reextraction(store, tenant_id, workspace_id, job_id: str, payload: dict) -> str | None:
    """Create the (single, deterministic) re-extraction job through the same admission
    (`check_job_admission`, project budget reservation, concurrency ceiling) the public
    route applies to an extraction. Returns a non-convergence note or None."""
    try:
        existing = store.job_record(tenant_id, workspace_id, job_id)
    except TenantScopeError:
        existing = None
    if existing is not None:
        if existing["kind"] != "extraction":
            raise StageIdConflict(f"job {job_id!r} exists with kind {existing['kind']!r}")
        return None
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        return "re-extraction not scheduled: no worker budget config is configured"
    try:
        config = job_quota.load_worker_budget_config()
        job_quota.check_job_admission(config, job_kind="extraction")
        limit, reservation = job_quota.load_concurrency_limit(), job_quota.compute_budget_reservation(config)
    except job_quota.JobQuotaError as exc:
        return f"re-extraction not scheduled: {exc}"
    from infra.worker.budget_config import BudgetExceeded
    for _ in range(5):
        head = store.head(tenant_id, workspace_id)
        try:
            store.create_job(tenant_id, workspace_id, job_id, "extraction", payload,
                             expected_version=head.version,
                             idempotency_key=f"job-create-{job_id}",
                             max_concurrent_jobs=limit, budget_reservation=reservation)
            return None
        except BudgetExceeded as exc:
            return f"re-extraction not scheduled: project budget exhausted ({exc})"
        except ConcurrencyLimitExceeded:
            raise  # transient: the event is redelivered and retried (bounded by attempts)
        except StoreConflict:
            try:
                store.job_record(tenant_id, workspace_id, job_id)
                return None
            except TenantScopeError:
                continue
        except ValueError:
            continue  # StaleVersion: the head moved, re-read it
    raise StoreConflict(f"could not ensure job {job_id!r}")


def schedule_reanalysis(store, tenant_id: str, workspace_id: str, refresh: dict,
                        impact: dict) -> list[str]:
    """Create-then-ack idempotent scheduling from the RECORDED impact. Returns notes."""
    refresh_id, revision_id = refresh["refresh_id"], refresh["source_revision_id"]
    decision_id = refresh_decision_id(revision_id)
    notes: list[str] = []
    for target in impact["targets"]:
        job_id = f"inval-{decision_id}-{target['candidate_id']}"
        try:
            ensure_job(store, tenant_id, workspace_id, job_id, "invalidation",
                       {"decision_id": decision_id, "target": target["candidate_id"],
                        "refresh_id": refresh_id, "verdict": target["verdict"]})
        except StageIdConflict as exc:
            _audit_conflict(store, tenant_id, workspace_id, refresh_id, job_id,
                            "invalidation", str(exc))
            notes.append(f"invalidation job id {job_id} is squatted by another kind")
    extraction_ids = []
    for item in impact["reextract"]:
        job_id = f"reanalysis-{decision_id}-extract-{_short(item['artifact_id'])}"
        extraction_ids.append(job_id)
        payload = {"artifact_id": item["artifact_id"],
                   "source_role": item["source_role"] or "unknown",
                   "caused_by_decision_id": decision_id, "refresh_id": refresh_id}
        try:
            note = _create_reextraction(store, tenant_id, workspace_id, job_id, payload)
        except StageIdConflict as exc:
            _audit_conflict(store, tenant_id, workspace_id, refresh_id, job_id, "extraction", str(exc))
            note = f"re-extraction job id {job_id} is squatted by another kind"
        if note:
            notes.append(note)
    if extraction_ids and impact["bound_candidate_ids"] is not None:
        findings_id = f"reanalysis-{decision_id}-findings"
        try:
            ensure_job(store, tenant_id, workspace_id, findings_id, "finding",
                       {"candidate_ids": [],
                        "refresh_reanalysis": {"extraction_job_ids": extraction_ids,
                                               "bound_candidate_ids": impact["bound_candidate_ids"]},
                        "caused_by_decision_id": decision_id, "refresh_id": refresh_id})
        except StageIdConflict as exc:
            _audit_conflict(store, tenant_id, workspace_id, refresh_id, findings_id, "finding", str(exc))
            notes.append(f"findings job id {findings_id} is squatted by another kind")
    return notes


def consume_refresh_published(store, tenant_id: str, workspace_id: str, claim, worker_id: str,
                              ack) -> bool:
    """Assess (once, write-once) and schedule the bounded reanalysis for a committed
    refresh, THEN ack (create-then-ack: a crash between the two redelivers the event and
    the deterministic ids make the second pass a no-op). A transient failure raises so
    the event is redelivered; a forged / foreign event (unknown refresh, not committed,
    revision mismatch) is acked without any work. Returns True if acked."""
    payload = claim.payload if isinstance(claim.payload, dict) else {}
    refresh_id, published = payload.get("refresh_id"), payload.get("source_revision_id")
    if not (isinstance(refresh_id, str) and refresh_id and isinstance(published, str) and published):
        return ack(store, tenant_id, workspace_id, claim, worker_id)
    try:
        refresh = store.source_refresh(tenant_id, workspace_id, refresh_id)
    except TenantScopeError:
        return ack(store, tenant_id, workspace_id, claim, worker_id)
    if refresh["status"] != "COMMITTED" or refresh["source_revision_id"] != published:
        return ack(store, tenant_id, workspace_id, claim, worker_id)
    impact = refresh["impact"]
    if impact is None:
        impact = store.set_refresh_impact(tenant_id, workspace_id, refresh_id,
                                          assess(store, tenant_id, workspace_id, refresh,
                                                 store.head(tenant_id, workspace_id)))
    notes = schedule_reanalysis(store, tenant_id, workspace_id, refresh, impact)
    store.add_refresh_impact_notes(tenant_id, workspace_id, refresh_id, notes)
    return ack(store, tenant_id, workspace_id, claim, worker_id)


# ----------------------------------------------------------------------------- status view

def refresh_status(store, tenant_id: str, workspace_id: str, refresh_id: str) -> dict:
    """The durable, read-only status of one refresh (raises TenantScopeError -> 404)."""
    r = store.source_refresh(tenant_id, workspace_id, refresh_id)
    head = store.head(tenant_id, workspace_id)
    committed = r["status"] == "COMMITTED"
    revision_id = r["source_revision_id"] if committed else (
        r["base"]["source_revision_id"] if r["status"] == "NOOP" else None)
    out = {"refresh_id": refresh_id, "workspace_id": workspace_id, "status": r["status"],
           "noop": r["noop"], "base": r["base"], "provenance": r["provenance"],
           "diff": r["diff"], "refresh_digest": r["refresh_digest"],
           "source_revision_id": revision_id,
           "is_current": revision_id is not None and head.source_revision_id == revision_id,
           "stale_policy": STALE_POLICY, "pending_worker": False, "stale_reasons": [],
           "decisions": {"kept": [], "invalidated": []}, "conflicts_opened": [],
           "reanalysis": {"state": "complete", "job_ids": []}}
    if not committed:
        return out
    decision_id = refresh_decision_id(revision_id)
    ingest = store.jobs_by_prefix(tenant_id, workspace_id, f"ingest-{refresh_id}")
    inval = store.jobs_by_prefix(tenant_id, workspace_id, f"inval-{decision_id}-")
    reana = store.jobs_by_prefix(tenant_id, workspace_id, f"reanalysis-{decision_id}-")
    pending = (store.refresh_event_pending(tenant_id, workspace_id, refresh_id)
               or any(j["execution_status"] in _LIVE for j in ingest + inval + reana))
    out["pending_worker"] = bool(pending)
    impact = r["impact"]
    causation = store.causation_for_decision(tenant_id, workspace_id, decision_id)
    reasons = []
    if impact is not None:
        bad = {}
        for t in impact["targets"]:
            if t["verdict"] in ("orphaned", "unverifiable"):
                bad[t["candidate_id"]] = t
                reasons.append({
                    "kind": "evidence_orphaned" if t["verdict"] == "orphaned" else "unknown_dependency",
                    "ref": t["candidate_id"], "artifact_id": t["artifact_id"],
                    "reason": t["reason"], "next_owner": "knowledge_owner"})
        for d in impact["decisions"]:
            t = bad.get(d["target"])
            if t is None:
                out["decisions"]["kept"].append(d["decision_id"])
            else:
                why = ("cited evidence no longer exists in the current source" if t["verdict"] == "orphaned"
                       else "the binding cannot be verified against the changed source")
                out["decisions"]["invalidated"].append(
                    {"decision_id": d["decision_id"], "target": d["target"], "reason": why})
                reasons.append({"kind": "decision_binding_invalid", "ref": d["decision_id"],
                                "reason": why, "next_owner": "knowledge_owner"})
    for job_id in store.stale_marked_jobs(tenant_id, workspace_id, decision_id):
        reasons.append({"kind": "job_stale", "ref": job_id,
                        "reason": "depends on a candidate whose source artifact changed in this refresh",
                        "next_owner": "reanalysis_worker" if job_id in causation else "requester"})
    if r["dropped_knowledge_revision_id"]:
        reasons.append({"kind": "knowledge_stale", "ref": r["dropped_knowledge_revision_id"],
                        "reason": "a source publish never carries a knowledge revision forward",
                        "next_owner": "knowledge_owner"})
    for job_id in store.approval_jobs_pinned_to(tenant_id, workspace_id, r["base"]["source_revision_id"]):
        reasons.append({"kind": "approval_stale", "ref": job_id,
                        "reason": "the approval readiness snapshot was computed against the previous revision",
                        "next_owner": "requester"})
    out["stale_reasons"] = reasons
    findings = [j for j in reana if j["job_id"] == f"reanalysis-{decision_id}-findings"]
    for job in findings:
        if job["execution_status"] == "COMPLETED":
            out["conflicts_opened"] = [f["finding_id"] for f in
                                       (job["execution_result"] or {}).get("findings", [])
                                       if f.get("kind") == "CONFLICT"]
    reason = None
    for job in inval:
        result = job["execution_result"] or {}
        if result.get("non_convergence_reason"):
            reason = result["non_convergence_reason"]
            break
    if reason is None and impact is not None and impact.get("notes"):
        reason = "; ".join(impact["notes"])
    if reason is None:
        for job in reana + inval:
            if job["execution_status"] in _TERMINAL_BAD:
                reason = f"reanalysis job {job['job_id']} {job['execution_status']}: {job['execution_reason']}"
                break
    out["reanalysis"] = {"state": "exhausted" if reason else ("pending" if pending else "complete"),
                         "job_ids": sorted(set(causation.values()))}
    if reason:
        out["reanalysis"]["non_convergence_reason"] = reason
    return out
