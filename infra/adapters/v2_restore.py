"""History browser, restore preview, RESTORE approval and the
restore-apply job creation.

Contract:

  * History / diff / preview are READ-ONLY. Every metadata read of one request
    happens in ONE `REPEATABLE READ READ ONLY` transaction (`read_snapshot`),
    so the `expected` tuple the digest is bound to is exactly the state the
    delta was computed from, and the database itself refuses any write.
  * A preview is a proposal, never an authorization. `preview_digest` is a
    canonical hash over workspace, the current tuple, the target id + manifest
    and the FULL untruncated collections; a size cap only shortens what is
    LISTED (`truncated` / `omitted`), never what the digest covers.
  * Approval (`approve_restore`) recomputes the preview against the CURRENT
    head, locked, and refuses a stale / forged / no-op / truncated preview. It
    writes one `v2_restore_approvals` row + one audit row and NOTHING else: no
    revision, decision, KM or head write.
  * The restore itself is NOT performed here: this module contains no
    publication code. `create_restore_apply_job` only creates the existing
    `apply` job (payload `restore_approval_id`); `infra/worker/apply.py::
    ApplyExecutor` publishes through the single reviewed gate.
  * Authority is the caller's (session-resolved, current) authority; no
    historical grant is ever consulted.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import Json

from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters.v2_change import _head
from infra.adapters.v2_policy import require_distinct_approver
from infra.adapters.v2_postgres import (
    BlobIntegrityError, StoreConflict, TenantScopeError, _as_object,
)

MAX_ITEMS_ENV = "RIPPLE_RESTORE_PREVIEW_MAX_ITEMS"
DEFAULT_MAX_ITEMS = 200
HARD_MAX_ITEMS = 1000  # tighten-only: the env can lower, never exceed, this ceiling
SUBJECT = "RESTORE"

LOSS_WARNING = (
    "Restoring publishes a NEW forward revision holding the content of the target "
    "revision. Edits made by the intervening revisions listed here will no longer be "
    "in the current source (they stay in history and can be restored again). Only the "
    "managed source revision is restored: decisions, audit and memberships are kept, "
    "the current knowledge revision becomes stale and must be rebuilt and accepted "
    "again, open changes pinned to the current head are invalidated, and NO external "
    "business transaction or system is rolled back.")


class RestoreConfigError(ValueError):
    """`RIPPLE_RESTORE_PREVIEW_MAX_ITEMS` is set but is not an integer >= 1."""


class RestoreRefused(Exception):
    """A named refusal (stable `reasons`) mapped to an HTTP status by the route."""

    def __init__(self, reasons, status=409):
        super().__init__(", ".join(reasons))
        self.reasons = list(reasons)
        self.status = status


class RestoreTargetUnavailable(RestoreRefused):
    """A blob of the target manifest is missing or fails its checksum: the
    target is not restorable (never a partial preview, never a fallback)."""

    def __init__(self, artifact_ids):
        super().__init__(["restore_target_unavailable"]
                         + [f"artifact_unavailable:{a}" for a in artifact_ids])
        self.artifact_ids = list(artifact_ids)


def max_items() -> int:
    raw = os.environ.get(MAX_ITEMS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_ITEMS
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise RestoreConfigError(f"{MAX_ITEMS_ENV} must be an integer >= 1") from exc
    if value < 1:
        raise RestoreConfigError(f"{MAX_ITEMS_ENV} must be an integer >= 1")
    return min(value, HARD_MAX_ITEMS)


@contextmanager
def read_snapshot(store):
    """One consistent, database-enforced read-only snapshot."""
    with store._tx() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield cur


def _iso(value):
    return value.isoformat() if value is not None else None


def manifest_hash(manifest) -> str:
    """CONTENT identity of a revision (artifact id -> blob hash). Distinct from
    the revision id / ordinal, which identify the publication EVENT."""
    return canonical_hash({"artifact_manifest_v1": dict(sorted(manifest.items()))})


def _manifests(cur, tenant_id, workspace_id, revision_ids):
    out = {r: {} for r in revision_ids}
    if not out:
        return out
    cur.execute("""SELECT source_revision_id, artifact_id, blob_hash FROM v2_artifact_revisions
        WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id = ANY(%s)""",
                (tenant_id, workspace_id, list(out)))
    for revision_id, artifact_id, blob_hash in cur.fetchall():
        out[revision_id][artifact_id] = blob_hash
    return out


def _lineage(cur, tenant_id, workspace_id, head_revision_id):
    """Ancestry of the current head, oldest -> newest, as
    (revision_id, parent_id, restored_from, trigger, created_at). The 1-based
    position is the ordinal `sequence`."""
    cur.execute("""SELECT revision_id, parent_revision_id, restored_from, trigger, created_at
        FROM v2_source_revisions WHERE tenant_id=%s AND workspace_id=%s""",
                (tenant_id, workspace_id))
    rows = {r[0]: r for r in cur.fetchall()}
    chain, seen, current = [], set(), head_revision_id
    while current and current in rows and current not in seen:
        seen.add(current)
        chain.append(rows[current])
        current = rows[current][1]
    chain.reverse()
    return chain


def delta_of(before: dict, after: dict) -> dict:
    """Whole-manifest delta from `before` to `after`."""
    changed = [{"artifact_id": a, "before_hash": before[a], "after_hash": after[a]}
               for a in sorted(before) if a in after and before[a] != after[a]]
    added = [{"artifact_id": a, "blob_hash": after[a]} for a in sorted(after) if a not in before]
    removed = [{"artifact_id": a, "blob_hash": before[a]} for a in sorted(before)
               if a not in after]
    unchanged = sum(1 for a in before if a in after and before[a] == after[a])
    return {"added": added, "removed": removed, "changed": changed,
            "unchanged_count": unchanged}


def _cap_delta(delta: dict, cap: int) -> tuple[dict, int]:
    flat = ([("changed", i) for i in delta["changed"]] + [("added", i) for i in delta["added"]]
            + [("removed", i) for i in delta["removed"]])
    kept = flat[:cap]
    out = {"added": [], "removed": [], "changed": [],
           "unchanged_count": delta["unchanged_count"]}
    for bucket, item in kept:
        out[bucket].append(item)
    return out, len(flat) - len(kept)


def _head_view(head) -> dict:
    return {"version": head.version, "source_revision_id": head.source_revision_id,
            "knowledge_revision_id": head.knowledge_revision_id,
            "decision_epoch": head.decision_epoch}


# ----------------------------------------------------------------------------- history

def history_view(store, tenant_id, workspace_id, *, limit=None) -> dict:
    limit = min(limit if limit is not None else max_items(), HARD_MAX_ITEMS)
    with read_snapshot(store) as cur:
        head = _head(cur, tenant_id, workspace_id)
        chain = _lineage(cur, tenant_id, workspace_id, head.source_revision_id)
        kept = chain[-limit:]
        manifests = _manifests(cur, tenant_id, workspace_id, [r[0] for r in kept])
        cur.execute("""SELECT revision_id, source_revision_id, decision_epoch, created_at
            FROM v2_knowledge_revisions WHERE tenant_id=%s AND workspace_id=%s
            ORDER BY created_at ASC, revision_id ASC""", (tenant_id, workspace_id))
        kms = cur.fetchall()
        cur.execute("""SELECT decision_id, epoch, created_at FROM v2_decisions
            WHERE tenant_id=%s AND workspace_id=%s ORDER BY epoch ASC""",
                    (tenant_id, workspace_id))
        decisions = cur.fetchall()
        cur.execute("""SELECT approval_id, target_revision_id, actor_id, created_at
            FROM v2_restore_approvals WHERE tenant_id=%s AND workspace_id=%s
            ORDER BY id ASC""", (tenant_id, workspace_id))
        approvals = cur.fetchall()
    ordinal = {r[0]: i + 1 for i, r in enumerate(chain)}
    revisions = []
    for rev_id, parent_id, restored_from, trigger, created_at in kept:
        manifest = manifests[rev_id]
        revisions.append({
            "revision_id": rev_id, "sequence": ordinal[rev_id],
            "manifest_hash": manifest_hash(manifest), "parent_revision_id": parent_id,
            "restored_from": restored_from, "trigger": trigger,
            "created_at": _iso(created_at), "is_current": rev_id == head.source_revision_id,
            "artifacts": [{"artifact_id": a, "blob_hash": manifest[a]} for a in sorted(manifest)]})
    kms_kept, decisions_kept, approvals_kept = kms[-limit:], decisions[-limit:], approvals[-limit:]
    knowledge = [{"revision_id": r[0], "source_revision_id": r[1], "decision_epoch": r[2],
                  "created_at": _iso(r[3])} for r in kms_kept]
    decision_rows = [{"decision_id": r[0], "epoch": r[1], "created_at": _iso(r[2])}
                     for r in decisions_kept]
    timeline = ([{"kind": "source_revision", "ref": r["revision_id"], "at": r["created_at"]}
                 for r in revisions]
                + [{"kind": "knowledge_revision", "ref": r["revision_id"], "at": r["created_at"]}
                   for r in knowledge]
                + [{"kind": "decision", "ref": r["decision_id"], "at": r["created_at"]}
                   for r in decision_rows]
                + [{"kind": "restore_approval", "ref": r[0], "at": _iso(r[3])}
                   for r in approvals_kept])
    timeline.sort(key=lambda e: (e["at"] or "", e["kind"], e["ref"]))
    return {"workspace_id": workspace_id, "head": _head_view(head), "revisions": revisions,
            "knowledge_revisions": knowledge, "decisions": decision_rows, "timeline": timeline,
            "omitted": {"revisions": len(chain) - len(kept),
                        "knowledge_revisions": len(kms) - len(kms_kept),
                        "decisions": len(decisions) - len(decisions_kept),
                        "restore_approvals": len(approvals) - len(approvals_kept)}}


def diff_view(store, tenant_id, workspace_id, from_revision_id, to_revision_id) -> dict:
    cap = max_items()
    with read_snapshot(store) as cur:
        _head(cur, tenant_id, workspace_id)
        ids = [from_revision_id, to_revision_id]
        cur.execute("""SELECT revision_id FROM v2_source_revisions
            WHERE tenant_id=%s AND workspace_id=%s AND revision_id = ANY(%s)""",
                    (tenant_id, workspace_id, ids))
        found = {r[0] for r in cur.fetchall()}
        if not set(ids) <= found:
            raise TenantScopeError("source revision not found")
        manifests = _manifests(cur, tenant_id, workspace_id, ids)
    delta = delta_of(manifests[from_revision_id], manifests[to_revision_id])
    capped, hidden = _cap_delta(delta, cap)
    return {"from_revision_id": from_revision_id, "to_revision_id": to_revision_id,
            "added": capped["added"], "removed": capped["removed"], "changed": capped["changed"],
            "unchanged_count": capped["unchanged_count"],
            "truncated": hidden > 0, "omitted": {"items": hidden}}


# ----------------------------------------------------------------------------- preview

def _tail(items, cap):
    """Keep the newest `cap` (list is oldest -> newest)."""
    kept = items[-cap:] if len(items) > cap else list(items)
    return kept, len(items) - len(kept)


def _head_slice(items, cap):
    kept = list(items[:cap])
    return kept, len(items) - len(kept)


def build_preview(cur, store, tenant_id, workspace_id, target_revision_id, cap, *, head=None):
    """The RecoveryProposal DTO for restoring `target_revision_id` over the
    CURRENT head, computed entirely through `cur` (one snapshot / one locked
    transaction). Raises TenantScopeError (unknown / foreign target),
    RestoreTargetUnavailable (damaged blob)."""
    head = head or _head(cur, tenant_id, workspace_id)
    cur.execute("""SELECT 1 FROM v2_source_revisions
        WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s""",
                (tenant_id, workspace_id, target_revision_id))
    if cur.fetchone() is None:
        raise TenantScopeError("source revision not found")
    current_id = head.source_revision_id
    chain = _lineage(cur, tenant_id, workspace_id, current_id)
    ordinal = {r[0]: i + 1 for i, r in enumerate(chain)}
    manifests = _manifests(cur, tenant_id, workspace_id,
                           list({r[0] for r in chain} | {target_revision_id}))
    target_manifest = manifests[target_revision_id]
    current_manifest = manifests.get(current_id, {})
    current_view = {"source_revision_id": current_id, "version": head.version,
                    "knowledge_revision_id": head.knowledge_revision_id,
                    "decision_epoch": head.decision_epoch,
                    "manifest_hash": manifest_hash(current_manifest)}
    target_view = {"source_revision_id": target_revision_id,
                   "manifest_hash": manifest_hash(target_manifest),
                   "sequence": ordinal.get(target_revision_id)}
    expected = _head_view(head)
    base = {"workspace_id": workspace_id, "restored_from": target_revision_id,
            "current": current_view, "target": target_view, "expected": expected,
            "loss_warning": LOSS_WARNING}
    if target_revision_id == current_id:
        empty = {"added": [], "removed": [], "changed": [], "unchanged_count": len(current_manifest)}
        return {**base,
                "proposal_id": "restore-noop-" + canonical_hash(
                    {"w": workspace_id, "t": target_revision_id})[:24],
                "noop": True, "delta": empty, "intervening_edits": [],
                "affected_decision_ids": [], "stale_knowledge_revision_ids": [],
                "open_changes": [], "planned_verification": [], "truncated": False,
                "omitted": {"delta": 0, "intervening_edits": 0, "open_changes": 0,
                            "affected_decision_ids": 0, "stale_knowledge_revision_ids": 0,
                            "planned_verification": 0},
                "preview_digest": None}
    if target_revision_id not in ordinal:
        raise RestoreRefused(["restore_target_not_in_lineage"])

    # Every artifact of the target must be restorable (also unchanged ones).
    bad = []
    for artifact_id in sorted(target_manifest):
        try:
            store._verified_blob(target_manifest[artifact_id])
        except BlobIntegrityError:
            bad.append(artifact_id)
    if bad or not target_manifest:
        raise RestoreTargetUnavailable(bad or ["<empty manifest>"])

    delta = delta_of(current_manifest, target_manifest)
    intervening, intervening_ids = [], set()
    for index in range(ordinal[target_revision_id], len(chain)):
        rev = chain[index]
        rev_manifest = manifests[rev[0]]
        prev_manifest = manifests[chain[index - 1][0]] if index > 0 else {}
        touched = sorted(a for a in set(rev_manifest) | set(prev_manifest)
                         if rev_manifest.get(a) != prev_manifest.get(a))
        intervening.append({"source_revision_id": rev[0], "sequence": index + 1,
                            "restored_from": rev[2], "artifact_ids": touched})
        intervening_ids.add(rev[0])

    cur.execute("""SELECT decision_id FROM v2_decisions
        WHERE tenant_id=%s AND workspace_id=%s AND superseded_by IS NULL AND revoked_by IS NULL
        ORDER BY epoch""", (tenant_id, workspace_id))
    affected = [r[0] for r in cur.fetchall()]

    cur.execute("""SELECT revision_id, source_revision_id FROM v2_knowledge_revisions
        WHERE tenant_id=%s AND workspace_id=%s ORDER BY created_at, revision_id""",
                (tenant_id, workspace_id))
    stale_ids = sorted({r[0] for r in cur.fetchall()
                        if r[1] in intervening_ids or r[1] == current_id
                        or r[0] == head.knowledge_revision_id}
                       | ({head.knowledge_revision_id} if head.knowledge_revision_id else set()))

    open_changes = _open_changes(cur, tenant_id, workspace_id, current_id)

    planned = ([{"artifact_id": i["artifact_id"], "check": "restored_content_equals_target"}
                for i in delta["changed"]]
               + [{"artifact_id": i["artifact_id"], "check": "restored_content_equals_target"}
                  for i in delta["added"]]
               + [{"artifact_id": i["artifact_id"], "check": "artifact_removed"}
                  for i in delta["removed"]])
    planned.sort(key=lambda p: (p["artifact_id"], p["check"]))

    digest = canonical_hash({"ai9_restore_preview_v1": {
        "tenant_id": tenant_id, "workspace_id": workspace_id,
        "current": {"source_revision_id": current_id, "version": head.version,
                    "decision_epoch": head.decision_epoch,
                    "knowledge_revision_id": head.knowledge_revision_id},
        "target": {"source_revision_id": target_revision_id, "manifest": target_manifest},
        "delta": delta, "intervening_edits": intervening, "affected_decision_ids": affected,
        "stale_knowledge_revision_ids": stale_ids, "open_changes": open_changes,
        "planned_verification": planned}})

    delta_out, hidden_delta = _cap_delta(delta, cap)
    intervening_out, hidden_int = _tail(intervening, cap)
    open_out, hidden_open = _head_slice(open_changes, cap)
    affected_out, hidden_aff = _head_slice(affected, cap)
    stale_out, hidden_stale = _head_slice(stale_ids, cap)
    planned_out, hidden_plan = _head_slice(planned, cap)
    omitted = {"delta": hidden_delta, "intervening_edits": hidden_int,
               "open_changes": hidden_open, "affected_decision_ids": hidden_aff,
               "stale_knowledge_revision_ids": hidden_stale, "planned_verification": hidden_plan}
    return {**base, "proposal_id": "restore-" + digest[:24], "noop": False,
            "delta": delta_out, "intervening_edits": intervening_out,
            "affected_decision_ids": affected_out, "stale_knowledge_revision_ids": stale_out,
            "open_changes": open_out, "planned_verification": planned_out,
            "truncated": any(v > 0 for v in omitted.values()), "omitted": omitted,
            "preview_digest": digest,
            "_full_delta_size": (len(delta["changed"]) + len(delta["added"])
                                 + len(delta["removed"]))}


def _open_changes(cur, tenant_id, workspace_id, current_revision_id):
    """Approved-but-unapplied changes pinned to the current head (a restore
    invalidates them: their approval snapshot no longer matches) plus apply
    jobs still in flight."""
    out = []
    cur.execute("""SELECT job_id, execution_result FROM v2_jobs
        WHERE tenant_id=%s AND workspace_id=%s AND kind='approval'
          AND execution_status='COMPLETED' ORDER BY job_id""", (tenant_id, workspace_id))
    approvals = cur.fetchall()
    cur.execute("""SELECT DISTINCT job_id FROM v2_change_decisions
        WHERE tenant_id=%s AND workspace_id=%s AND action='approve'""",
                (tenant_id, workspace_id))
    decided = {r[0] for r in cur.fetchall()}
    cur.execute("""SELECT DISTINCT payload->>'approval_job_id' FROM v2_jobs
        WHERE tenant_id=%s AND workspace_id=%s AND kind='apply'
          AND execution_status='COMPLETED' AND execution_result->>'published'='true'""",
                (tenant_id, workspace_id))
    applied = {r[0] for r in cur.fetchall()}
    for job_id, raw in approvals:
        result = _as_object(raw) or {}
        snap = result.get("snapshot") or {}
        if snap.get("source_revision_id") != current_revision_id or job_id in applied:
            continue
        out.append({"kind": "approved_change" if job_id in decided else "pending_approval",
                    "approval_job_id": job_id,
                    "proposal_job_id": result.get("proposal_job_id"),
                    "review_job_id": result.get("review_job_id"),
                    "snapshot_source_revision_id": snap.get("source_revision_id")})
    cur.execute("""SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s
        AND kind='apply' AND execution_status IN ('QUEUED','RUNNING') ORDER BY job_id""",
                (tenant_id, workspace_id))
    out.extend({"kind": "apply_in_flight", "apply_job_id": r[0]} for r in cur.fetchall())
    return out


def preview(store, tenant_id, workspace_id, target_revision_id) -> dict:
    with read_snapshot(store) as cur:
        dto = build_preview(cur, store, tenant_id, workspace_id, target_revision_id, max_items())
    dto.pop("_full_delta_size", None)
    return dto


# ----------------------------------------------------------------------------- approval

def approve_restore(store, tenant_id, workspace_id, *, actor_id, target_revision_id,
                    expected_preview_digest, reason, idempotency_key=None) -> dict:
    """Record the durable RESTORE approval. One transaction under the head lock:
    the preview is recomputed against the CURRENT locked state and must match
    the presented digest; nothing but the approval row + audit row is written."""
    with store._tx() as cur:
        head = store._lock_head(cur, tenant_id, workspace_id)
        dto = build_preview(cur, store, tenant_id, workspace_id, target_revision_id,
                            max_items(), head=head)
        if dto["noop"]:
            raise RestoreRefused(["noop_restore"])
        if dto["preview_digest"] != expected_preview_digest:
            raise RestoreRefused(["preview_stale"])
        if dto["truncated"]:
            raise RestoreRefused(["preview_truncated"])
        if dto["_full_delta_size"] == 0:
            raise RestoreRefused(["noop_restore"])
        cur.execute("""SELECT approval_id, approval_digest FROM v2_restore_approvals
            WHERE tenant_id=%s AND workspace_id=%s AND target_revision_id=%s
              AND preview_digest=%s AND actor_id=%s""",
                    (tenant_id, workspace_id, target_revision_id, expected_preview_digest,
                     actor_id))
        prior = cur.fetchone()
        if prior is not None:
            return {"restore_approval_id": prior[0], "subject": SUBJECT,
                    "approval_digest": prior[1], "preview_digest": expected_preview_digest,
                    "target_revision_id": target_revision_id, "actor_id": actor_id,
                    "replayed": True}
        snapshot = dto["expected"]
        approval_digest = canonical_hash({"ai9_restore_approval_v1": {
            "tenant_id": tenant_id, "workspace_id": workspace_id,
            "target_revision_id": target_revision_id,
            "preview_digest": expected_preview_digest, "expected": snapshot,
            "actor_id": actor_id}})
        approval_id = "restore-approval-" + uuid.uuid4().hex
        try:
            cur.execute("""INSERT INTO v2_restore_approvals
                (tenant_id, workspace_id, approval_id, target_revision_id, preview_digest,
                 approval_digest, actor_id, reason, snapshot, idempotency_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (tenant_id, workspace_id, approval_id, target_revision_id,
                         expected_preview_digest, approval_digest, actor_id, reason,
                         Json(snapshot), idempotency_key))
        except psycopg2.IntegrityError as exc:
            raise StoreConflict(f"restore approval conflict: {exc}") from exc
        cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload)
            VALUES (%s,%s,'RESTORE_APPROVED',%s,%s)""",
                    (tenant_id, workspace_id, actor_id, Json({
                        "restore_approval_id": approval_id, "subject": SUBJECT,
                        "target_revision_id": target_revision_id,
                        "preview_digest": expected_preview_digest,
                        "approval_digest": approval_digest, "expected": snapshot})))
        return {"restore_approval_id": approval_id, "subject": SUBJECT,
                "approval_digest": approval_digest, "preview_digest": expected_preview_digest,
                "target_revision_id": target_revision_id, "actor_id": actor_id,
                "replayed": False}


def _approval_row(cur, tenant_id, workspace_id, approval_id):
    cur.execute("""SELECT approval_id, target_revision_id, preview_digest, approval_digest,
            actor_id, snapshot FROM v2_restore_approvals
        WHERE tenant_id=%s AND workspace_id=%s AND approval_id=%s""",
                (tenant_id, workspace_id, approval_id))
    row = cur.fetchone()
    if row is None:
        return None
    return {"approval_id": row[0], "target_revision_id": row[1], "preview_digest": row[2],
            "approval_digest": row[3], "actor_id": row[4], "snapshot": _as_object(row[5])}


def load_restore_approval(store, tenant_id, workspace_id, approval_id):
    if not isinstance(approval_id, str) or not approval_id:
        return None
    with store._tx() as cur:
        return _approval_row(cur, tenant_id, workspace_id, approval_id)


def apply_subject(payload) -> str:
    """The approval subject an `apply` job carries (from its immutable payload)."""
    if isinstance(payload, dict) and payload.get("restore_approval_id") is not None:
        return SUBJECT
    return "CHANGE"


def create_restore_apply_job(store, tenant_id, workspace_id, job_id, payload, *,
                             submitter_id, max_concurrent_jobs=None,
                             budget_reservation=None) -> str:
    """Create the existing `apply` job for a RESTORE approval, in one
    transaction: approval exists + digest matches + distinct-approver policy
    (+ the audited `job.self_approval` when the workspace relaxed it) + the
    job row. Deliberately NO staleness check here: the shared gate's stale-tuple check
    on the row-locked head (`ApplyExecutor`) is the only staleness guard."""
    with store._tx() as cur:
        head = store._lock_head(cur, tenant_id, workspace_id)
        approval = _approval_row(cur, tenant_id, workspace_id,
                                 payload.get("restore_approval_id"))
        if approval is None:
            raise RestoreRefused(["restore_approval_not_found"], status=404)
        if approval["approval_digest"] != payload.get("expected_approval_digest"):
            raise RestoreRefused(["approval_digest_mismatch"])
        cur.execute("""SELECT require_distinct_approver FROM v2_workspaces
            WHERE tenant_id=%s AND workspace_id=%s""", (tenant_id, workspace_id))
        distinct_required = cur.fetchone()[0]
        require_distinct_approver(approval["actor_id"], submitter_id,
                                  require_distinct=distinct_required)
        result = store._create_job_core(
            cur, tenant_id, workspace_id, job_id, "apply", payload, head=head,
            expected_version=head.version, idempotency_key=f"job-create-{job_id}",
            max_concurrent_jobs=max_concurrent_jobs, budget_reservation=budget_reservation,
            requested_by=submitter_id)
        if not distinct_required and approval["actor_id"] == submitter_id:
            cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload)
                VALUES (%s,%s,'job.self_approval','policy',%s)""",
                        (tenant_id, workspace_id, Json({
                            "job_id": job_id, "account_id": submitter_id,
                            "subject": SUBJECT,
                            "restore_approval_id": approval["approval_id"]})))
        return result.revision_id


__all__ = ["RestoreConfigError", "RestoreRefused", "RestoreTargetUnavailable", "SUBJECT",
           "apply_subject", "approve_restore", "build_preview", "create_restore_apply_job",
           "delta_of", "diff_view", "history_view", "load_restore_approval", "manifest_hash",
           "max_items", "preview", "read_snapshot"]
