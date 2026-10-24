"""PostgreSQL workspace authority with external immutable blobs.

The database holds metadata, decisions, jobs and audit; blob bytes remain in
the injected object store. Each publish is one CAS transaction that includes
the new revision, head, idempotency result, audit and outbox event.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import replace as _dc_replace
import hashlib
import os
import json
from pathlib import Path
from typing import Callable, Iterator, Mapping
import uuid

import psycopg2
from psycopg2.extras import Json

from core.change_intelligence.v2.lifecycle import (
    LifecycleError, TransitionContext, authorize_transition,
)
from core.change_intelligence.v2.intent import parse_v2_intent
from core.change_intelligence.v2.serialize import canonical_hash
from core.change_intelligence.v2.store_ports import (
    ImmutableBlobPort, OutboxClaim, PublishResult, WorkspaceHead,
)
from infra.worker.safe_diagnostics import safe_diagnostics, sanitize_reason
from infra.adapters.v2_policy import permissions_for



def _safe_attempt(attempt: Mapping[str, object]) -> dict:
    """AH-05: an attempt row's reason and provider diagnostics in their typed, bounded form (no raw exception / provider text)."""
    a = dict(attempt)
    if "reason" in a:
        a["reason"] = sanitize_reason(a.get("reason"))
    rc = a.get("request_context")
    if isinstance(rc, Mapping) and "provider_diagnostics" in rc:
        a["request_context"] = dict(rc, provider_diagnostics=safe_diagnostics(rc.get("provider_diagnostics")))
    return a

class StoreConflict(ValueError):
    """An idempotency key was reused for a different payload."""


class JobAttemptsExhausted(StoreConflict):
    """The job already made every provider attempt the approved retry limit allows; running it again would only be refused by the worker.
    Raised by `retry_job` BEFORE anything is reserved, queued or booked."""

    def __init__(self, used: int, limit: int):
        super().__init__(f"{used} provider attempt(s) already made (limit {limit})")
        self.used, self.limit = used, limit


class StaleVersion(ValueError):
    """The caller's pinned workspace/source/decision version is no longer current."""


class TenantScopeError(LookupError):
    """The scoped workspace or resource is absent; do not reveal foreign IDs."""


class BlobIntegrityError(ValueError):
    """Object bytes do not match their immutable digest."""


class LeaseLost(ValueError):
    """A worker no longer owns the leased outbox row/fencing token."""


class ConcurrencyLimitExceeded(ValueError):
    """Raised by `create_job` when an optional per-workspace
    concurrency ceiling is already met. Distinct from `StoreConflict`/
    `StaleVersion` so callers can translate it to a quota-style refusal
    (409 budget_exceeded), not a generic conflict."""


class PersonQuotaExceeded(ConcurrencyLimitExceeded):
    """The ACCOUNT already started its allowed number of AI jobs today (`RIPPLE_AI_JOBS_PER_ACCOUNT_DAY`). It is per ACCOUNT, not per human: people who share an account share it."""


class JobCeilingExceeded(ConcurrencyLimitExceeded):
    """R3: the workspace already has the authorized total number of jobs of this kind (every state counts: a failed or cancelled job
    still used its authorization). Mapped to the same 409 budget_exceeded refusal."""


class SkipAcknowledgementRequired(StoreConflict):
    """The commit's acknowledged skips differ from the import's
    `skip_required` items."""


class ImportExpired(StoreConflict):
    """A prepared import passed its expiry before commit."""


class NoAdmittedItems(StoreConflict):
    """An import whose items are all skipped cannot form a revision."""


class RefreshExpired(StoreConflict):
    """A prepared refresh passed its expiry before commit."""


class RefreshDigestMismatch(StoreConflict):
    """The commit's `expected_refresh_digest` is not the digest of the
    diff the human reviewed (or the head no longer yields that diff)."""


class DeletionAcknowledgementRequired(StoreConflict):
    """The acknowledged deletions differ from the refresh's deleted set."""


class HitlVersionConflict(StoreConflict):
    """A `/v2/hitl/answer` request's `expected_version` no longer
    matches the question's real current version (the count of previously
    committed answer rows for it). Distinct from the generic idempotency-hash
    `StoreConflict` so the route can carry the real current version back to
    the caller, so a stale question is rejected."""

    def __init__(self, current_version: int):
        super().__init__("stale_version")
        self.current_version = current_version


class StaleAuthorityError(StoreConflict):
    """The committing actor no
    longer holds the required permission at commit time (revoked/disabled/
    expired account, missing membership, or a role set that no longer grants
    it), rechecked under the same row locks the commit write uses."""


class IntentNotFound(LookupError):
    """No `v2_intents` row for this (tenant, workspace, intent_id)."""


class IntentStale(StoreConflict):
    """`intent.confirm`'s per-field recheck of the CURRENT
    `v2_workspaces` head against the pinned `intent.parse` snapshot found at
    least one of decision_epoch/knowledge_revision_id/source_revision_id has
    moved. The pending intent must be treated as stale and rejected, never
    silently confirmed against outdated state."""

    def __init__(self, stale_fields: tuple[str, ...]):
        super().__init__("intent_stale:" + ",".join(stale_fields))
        self.stale_fields = stale_fields


#: A prepared import that is not committed within this window can
#: no longer become current.
SOURCE_IMPORT_TTL_HOURS = 24


#: The ONE definition of "project spend". The budget
#: contract's total cap is per PROJECT, and several workspaces can belong to
#: one project (`v2_workspaces.project_id`), so spend is summed over every
#: workspace of the scoped workspace's project -- never just the one
#: workspace. A (tenant, workspace) scope with no `v2_workspaces` row (the
#: ledger's documented standalone use by `job_quota.record_job_cost`) falls
#: back to that workspace alone. Parameters: `project_spend_params(...)`.
PROJECT_SPEND_SQL = """SELECT COALESCE(SUM(l.cost_usd), 0) FROM v2_job_cost_ledger AS l
    WHERE l.tenant_id=%s AND (
        l.workspace_id=%s
        OR l.workspace_id IN (
            SELECT w.workspace_id FROM v2_workspaces AS w
            WHERE w.tenant_id=%s AND w.project_id=(
                SELECT s.project_id FROM v2_workspaces AS s
                WHERE s.tenant_id=%s AND s.workspace_id=%s)))"""


def project_spend_params(tenant_id: str, workspace_id: str) -> tuple:
    return (tenant_id, workspace_id, tenant_id, tenant_id, workspace_id)


#: Execution states from which a worker may still move a job's execution
#: axis to a terminal state (RUNNING = claimed by `begin_execution`).
_LIVE_EXECUTION = ("QUEUED", "RUNNING")


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _as_object(value):
    return json.loads(value) if isinstance(value, str) else value


class PostgresV2Store:
    """One repository instance per DSN; safe to recreate after process restart."""

    def __init__(self, dsn: str, blobs: ImmutableBlobPort):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PostgreSQL DSN required")
        self.dsn = dsn
        self.blobs = blobs

    @contextmanager
    def _tx(self) -> Iterator:
        from infra import db_pool
        with db_pool.connection(self.dsn) as conn:            # bounded pool when RIPPLE_DB_POOL_MAX is set, else connect-per-use (unchanged)
            with conn:
                with conn.cursor() as cur:
                    yield cur

    def migrate(self) -> None:
        migrations_dir = Path(__file__).resolve().parents[1] / "migrations"
        sql = (migrations_dir / "ai1_t03_v2.sql").read_text(encoding="utf-8")
        # The per-workspace approver-distinctness policy column and job
        # requester/approver attribution are added to the same tables.
        sql += "\n" + (migrations_dir / "ai2_t02_policy.sql").read_text(encoding="utf-8")
        # Independent worker-owned execution_status/result
        # columns and the durable per-job cost ledger table (see that file's
        # own header comment for why these are a schema migration now,
        # rather than lazily created out of band by infra/worker/job_quota.py).
        sql += "\n" + (migrations_dir / "ai2_t03_execution_status.sql").read_text(encoding="utf-8")
        # Per-attempt execution records, reservation/
        # settlement ledger states, project-scoped model-data policy.
        sql += "\n" + (migrations_dir / "ai2_t03_round6.sql").read_text(encoding="utf-8")
        # Import staging/admission report and revision metadata.
        sql += "\n" + (migrations_dir / "ai2_t04_source_import.sql").read_text(encoding="utf-8")
        # Parsed observations per artifact.
        sql += "\n" + (migrations_dir / "ai3_t01_observations.sql").read_text(encoding="utf-8")
        # Retrieval index projection + build readiness marker.
        sql += "\n" + (migrations_dir / "ai3_t03_retrieval_index.sql").read_text(encoding="utf-8")
        # Source coverage columns on the
        # build readiness marker.
        sql += "\n" + (migrations_dir / "ai3_t03_round2.sql").read_text(encoding="utf-8")
        # Human work queue answer-draft records.
        sql += "\n" + (migrations_dir / "ai5_t01_hitl.sql").read_text(encoding="utf-8")
        # Fuller append-only HumanDecision ledger columns.
        sql += "\n" + (migrations_dir / "ai5_t02_decision_ledger.sql").read_text(encoding="utf-8")
        # Stale markers on dependent jobs + decision->invalidation->
        # reanalysis causation chain.
        sql += "\n" + (migrations_dir / "ai5_t03_invalidation.sql").read_text(encoding="utf-8")
        # Versioned Intent rows (intent.parse / intent.confirm).
        sql += "\n" + (migrations_dir / "ai6_t01_intent.sql").read_text(encoding="utf-8")
        # Dedicated, non-epoch-bumping change-decision ledger for a
        # "kind=approval" job's human approve/reject/request_changes.
        sql += "\n" + (migrations_dir / "ai7_t04_change_decision.sql").read_text(encoding="utf-8")
        # Human result decision + durable manual-work proof.
        sql += "\n" + (migrations_dir / "ai8_t04_result_decision.sql").read_text(encoding="utf-8")
        # `restored_from` provenance + durable RESTORE approvals.
        sql += "\n" + (migrations_dir / "ai9_t02_restore.sql").read_text(encoding="utf-8")
        # Manual external-source refresh observations.
        sql += "\n" + (migrations_dir / "ai9_t03_refresh.sql").read_text(encoding="utf-8")
        # Operator-supplied, immutable verification contracts.
        sql += "\n" + (migrations_dir / "w1_verification_contracts.sql").read_text(encoding="utf-8")
        # UX: free-text request conversations (additive tables only).
        sql += "\n" + (migrations_dir / "ux_intent_chat.sql").read_text(encoding="utf-8")
        # UX: more free-text purposes on the same conversations (additive columns).
        sql += "\n" + (migrations_dir / "ux_free_text_purposes.sql").read_text(encoding="utf-8")
        sql += "\n" + (migrations_dir / "ux_workspace_profile.sql").read_text(encoding="utf-8")
        # R3: durable append-only ceilings on the number of jobs of one kind (live trial authorizations).
        sql += "\n" + (migrations_dir / "r3_job_ceilings.sql").read_text(encoding="utf-8")
        sql += "\n" + (migrations_dir / "result_dispositions.sql").read_text(encoding="utf-8")
        # Final knowledge loop: business snapshots, rules, relations, gaps, decisions (additive tables only).
        sql += "\n" + (migrations_dir / "fkl_t01_business_knowledge.sql").read_text(encoding="utf-8")
        sql += "\n" + (migrations_dir / "fkl_t02_business_index.sql").read_text(encoding="utf-8")
        sql += "\n" + (migrations_dir / "fkl_t03_business_change_intents.sql").read_text(encoding="utf-8")
        # Final: human decisions on a confirmed business change (knowledge / file_writes / clarification), bound to the draft seen; no epoch or version bump.
        sql += "\n" + (migrations_dir / "fkl_t04_business_change_decisions.sql").read_text(encoding="utf-8")
        # Final: generated Business/Technical documents of an accepted snapshot (immutable Markdown bytes + metadata; derived views, never evidence).
        sql += "\n" + (migrations_dir / "fkl_t05_business_documents.sql").read_text(encoding="utf-8")
        # extension contract ext/1 (FEX-05..08): the append-only code-change decision ledger (additive)
        sql += "\n" + (migrations_dir / "ext_t01_code_change.sql").read_text(encoding="utf-8")
        # extension contract ma/1 (MA-00/MA-R1): multi-agent runs, children and the immutable orchestration events (additive)
        sql += "\n" + (migrations_dir / "ext_t02_ma_runs.sql").read_text(encoding="utf-8")
        # extension AIX-K2: interview sessions over one open question (additive)
        sql += "\n" + (migrations_dir / "ext_t03_gap_interviews.sql").read_text(encoding="utf-8")
        # extension AIX-D1: audience variants of generated documents (additive, immutable rows)
        sql += "\n" + (migrations_dir / "ext_t04_document_variants.sql").read_text(encoding="utf-8")
        # extension AIX-K1: dismissals of mining hypotheses (additive, immutable rows)
        sql += "\n" + (migrations_dir / "ext_t05_mining_dismissals.sql").read_text(encoding="utf-8")
        # extensions FB-R1 / FB-N1 (satellite, local build): projector checkpoint, notification intents/tokens/deliveries, inbox items
        sql += "\n" + (migrations_dir / "ext_t07_projection_checkpoints.sql").read_text(encoding="utf-8")
        sql += "\n" + (migrations_dir / "ext_t08_notifications.sql").read_text(encoding="utf-8")
        sql += "\n" + (migrations_dir / "ext_t09_code_pr_published.sql").read_text(encoding="utf-8")
        sql += "\n" + (migrations_dir / "ext_t10_request_assistant.sql").read_text(encoding="utf-8")   # RQA-02 private assistant threads
        sql += "\n" + (migrations_dir / "ext_t11_preparation_runs.sql").read_text(encoding="utf-8")    # EUI-08 guided preparation runs (feature off by default)
        sql += "\n" + (migrations_dir / "ah05_job_correlation.sql").read_text(encoding="utf-8")      # AH-05 HTTP -> job correlation (additive)
        sql += "\n" + (migrations_dir / "ah02_review_note_index.sql").read_text(encoding="utf-8")    # AH-02 measured partial index for review-note lookups
        with self._tx() as cur:
            cur.execute(sql)

    def create_workspace(self, tenant_id: str, project_id: str,
                         workspace_id: str) -> WorkspaceHead:
        if not all(isinstance(x, str) and x.strip()
                   for x in (tenant_id, project_id, workspace_id)):
            raise ValueError("tenant, project and workspace IDs required")
        with self._tx() as cur:
            cur.execute("""INSERT INTO v2_workspaces
                (tenant_id, project_id, workspace_id) VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id, workspace_id) DO NOTHING""",
                (tenant_id, project_id, workspace_id))
            cur.execute("""SELECT project_id FROM v2_workspaces
                WHERE tenant_id=%s AND workspace_id=%s""", (tenant_id, workspace_id))
            if cur.fetchone()[0] != project_id:
                raise StoreConflict("workspace project cannot change")
        return self.head(tenant_id, workspace_id)

    def head(self, tenant_id: str, workspace_id: str) -> WorkspaceHead:
        with self._tx() as cur:
            cur.execute("""SELECT version, source_revision_id, knowledge_revision_id,
                decision_epoch, policy_version FROM v2_workspaces
                WHERE tenant_id=%s AND workspace_id=%s""", (tenant_id, workspace_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("workspace not found")
        return WorkspaceHead(tenant_id, workspace_id, *row)

    def stage_blob(self, content: bytes) -> str:
        if not isinstance(content, bytes):
            raise TypeError("blob content must be bytes")
        digest = _digest(content)
        self.blobs.put_immutable(digest, content)
        self._verified_blob(digest)
        return digest

    def _verified_blob(self, digest: str) -> bytes:
        try:
            content = self.blobs.read(digest)
        except (KeyError, FileNotFoundError) as exc:
            raise BlobIntegrityError("staged blob missing") from exc
        if not isinstance(content, bytes) or _digest(content) != digest:
            raise BlobIntegrityError("staged blob checksum mismatch")
        return content

    def _lock_head(self, cur, tenant_id: str, workspace_id: str) -> WorkspaceHead:
        cur.execute("""SELECT version, source_revision_id, knowledge_revision_id,
            decision_epoch, policy_version FROM v2_workspaces
            WHERE tenant_id=%s AND workspace_id=%s FOR UPDATE""",
            (tenant_id, workspace_id))
        row = cur.fetchone()
        if row is None:
            raise TenantScopeError("workspace not found")
        return WorkspaceHead(tenant_id, workspace_id, *row)

    @staticmethod
    def _link_job(cur, tenant_id: str, workspace_id: str, job_id: str, *, created: bool) -> None:
        """AH-05: record which HTTP request (or parent job) created or replayed this job, in the same transaction. Metadata only: nothing here changes the job's
        identity, idempotency or authorization. Without a request or a parent (a tool or test outside both) nothing is written."""
        from infra.correlation import current
        request_id, parent = current()
        if not request_id and not parent:
            return
        cur.execute("""INSERT INTO v2_job_correlation (tenant_id, workspace_id, job_id, request_id, parent_job_id, created) VALUES (%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, job_id, request_id, parent, created))

    def link_job_request(self, tenant_id: str, workspace_id: str, job_id: str) -> None:
        """AH-05: an idempotent replay answered without touching the job (the caller found the existing logical job) still records the request that replayed it."""
        with self._tx() as cur:
            self._link_job(cur, tenant_id, workspace_id, job_id, created=False)

    def job_correlation(self, tenant_id: str, workspace_id: str, job_id: str) -> list[dict]:
        with self._tx() as cur:
            cur.execute("""SELECT request_id, parent_job_id, created, recorded_at FROM v2_job_correlation WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                           ORDER BY recorded_at, id""", (tenant_id, workspace_id, job_id))
            return [{"request_id": r[0], "parent_job_id": r[1], "created": r[2], "recorded_at": r[3].isoformat()} for r in cur.fetchall()]

    def jobs_for_request(self, request_id: str) -> list[dict]:
        """Operator lookup by a request ID a person quoted (operator tools only; the ID is random and server-generated)."""
        with self._tx() as cur:
            cur.execute("""SELECT tenant_id, workspace_id, job_id, created, recorded_at FROM v2_job_correlation WHERE request_id=%s ORDER BY recorded_at, id LIMIT 50""",
                        (request_id,))
            return [{"tenant_id": r[0], "workspace_id": r[1], "job_id": r[2], "created": r[3], "recorded_at": r[4].isoformat()} for r in cur.fetchall()]

    def _replay(self, cur, tenant_id: str, workspace_id: str, operation: str,
                key: str, payload_hash: str) -> PublishResult | None:
        cur.execute("""SELECT payload_hash, result FROM v2_idempotency
            WHERE tenant_id=%s AND workspace_id=%s AND operation=%s
              AND idempotency_key=%s""", (tenant_id, workspace_id, operation, key))
        row = cur.fetchone()
        if row is None:
            return None
        if row[0] != payload_hash:
            raise StoreConflict("idempotency_conflict")
        result = _as_object(row[1])
        return PublishResult(result["version"], result["revision_id"], True)

    def _record_commit(self, cur, tenant_id: str, workspace_id: str, operation: str,
                       key: str, payload_hash: str, result: PublishResult,
                       event_type: str, payload: Mapping[str, object]) -> None:
        cur.execute("""INSERT INTO v2_idempotency
            (tenant_id, workspace_id, operation, idempotency_key, payload_hash, result)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (tenant_id, workspace_id, operation, key, payload_hash,
             Json({"version": result.version, "revision_id": result.revision_id})))
        cur.execute("""INSERT INTO v2_audit
            (tenant_id, workspace_id, event_type, actor_id, payload)
            VALUES (%s,%s,%s,%s,%s)""",
            (tenant_id, workspace_id, event_type, "service", Json(dict(payload))))
        cur.execute("""INSERT INTO v2_outbox
            (tenant_id, workspace_id, kind, payload) VALUES (%s,%s,%s,%s)""",
            (tenant_id, workspace_id, event_type, Json(dict(payload))))

    def _recheck_actor_authority(self, cur, tenant_id: str, workspace_id: str,
                                 actor_id: str, required_permission: str) -> None:
        """Shared current-authority recheck, extracted from
        `commit_source_import`'s own inline block and
        `recheck_and_publish_job`'s equivalent -- the SAME
        "actor may have been revoked since an earlier snapshot was taken"
        guard, reused by `publish_source`'s own `authority_check` hook
        so a THIRD publish gate does not reimplement this check a
        third time. Must be called under the SAME transaction as the write
        it guards, with `cur` from that transaction -- never a separate
        connection/transaction (that would not be atomic with the write).

        Lock order: account, then membership, then (by the caller) the
        workspace head row -- matching `commit_source_import`'s own
        documented ABBA-deadlock-avoidance ordering against
        `PostgresIdentityStore.revoke_membership`'s (membership, then
        workspace) lock order."""
        cur.execute("""SELECT disabled_at,
                (credential_expires_at IS NOT NULL AND credential_expires_at <= now())
            FROM v2_accounts WHERE account_id=%s FOR UPDATE""", (actor_id,))
        account_row = cur.fetchone()
        if account_row is None or account_row[0] is not None or account_row[1]:
            raise StaleAuthorityError("actor authority revoked; publish blocked")
        cur.execute("""SELECT roles FROM v2_memberships
            WHERE tenant_id=%s AND workspace_id=%s AND account_id=%s FOR UPDATE""",
            (tenant_id, workspace_id, actor_id))
        membership_row = cur.fetchone()
        if membership_row is None:
            raise StaleAuthorityError("actor authority revoked; publish blocked")
        if required_permission not in permissions_for(membership_row[0]):
            raise StaleAuthorityError(
                "actor no longer holds the required permission; publish blocked")

    def prior_publish(self, tenant_id: str, workspace_id: str, operation: str,
                      idempotency_key: str) -> dict | None:
        """Retry recovery: the committed result of an earlier
        `publish_source` under this idempotency key (ledger read), with its
        parent revision, or None. Lets a retried apply return the prior
        committed attempt instead of replaying edits against already
        published bytes."""
        with self._tx() as cur:
            cur.execute("""SELECT i.result, r.parent_revision_id
                FROM v2_idempotency i
                LEFT JOIN v2_source_revisions r ON r.tenant_id=i.tenant_id
                  AND r.workspace_id=i.workspace_id
                  AND r.revision_id=(i.result->>'revision_id')
                WHERE i.tenant_id=%s AND i.workspace_id=%s AND i.operation=%s
                  AND i.idempotency_key=%s""",
                (tenant_id, workspace_id, operation, idempotency_key))
            row = cur.fetchone()
        if row is None:
            return None
        res = _as_object(row[0])
        return {"version": res["version"], "revision_id": res["revision_id"],
                "parent_revision_id": row[1]}

    def publish_source(self, tenant_id: str, workspace_id: str, revision_id: str,
                       manifest: Mapping[str, str], *, expected_version: int,
                       operation: str, idempotency_key: str,
                       before_commit: Callable[[], None] | None = None,
                       authority_check: Callable[[object], None] | None = None,
                       carry_forward: bool = False,
                       fence: tuple[int, str, int] | None = None,
                       audit_context: Mapping[str, object] | None = None,
                       head_check: Callable[[WorkspaceHead], None] | None = None,
                       locked_check: Callable[[object, WorkspaceHead], None] | None = None,
                       restored_from: str | None = None,
                       trigger: str | None = None
                       ) -> PublishResult:
        """Atomic CAS publication of a source revision (one SQL transaction).

        `restored_from=<historic revision id>` marks a
        FORWARD restore revision (`trigger`, e.g. "restore", is stored with
        it). The store itself re-reads the target's whole manifest under the
        head lock and refuses (StoreConflict, nothing written) unless
        `manifest` equals it exactly (a revision is the whole snapshot,
        see the restore contract); artifact metadata is carried from the TARGET rows. Only
        the apply gate (`infra/worker/apply.py`) calls this with it.

        Optional parameters (existing callers unaffected):
        `authority_check(cur)` current-authority recheck; `fence=(outbox
        event id, worker id, fencing token)` re-validates, under a row lock
        in this SAME transaction, that the caller still owns its outbox lease
        (same predicate `ack_outbox` uses) else `LeaseLost` and nothing is
        written; `carry_forward=True` treats `manifest` as OVERRIDES on top of
        the current head's full manifest (a revision is the whole snapshot;
        other artifacts and their metadata are carried unchanged);
        `audit_context` adds job/approver identity to the audit+outbox
        payload; `head_check(locked_head)` validates the
        row-LOCKED head after the idempotent-replay lookup (so a committed
        retry still returns its prior result) and before the CAS; whatever
        it raises aborts and rolls back the transaction."""
        if not all(isinstance(x, str) and x.strip()
                   for x in (tenant_id, workspace_id, revision_id, operation, idempotency_key)):
            raise ValueError("scoped revision, operation and idempotency key required")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if not isinstance(manifest, Mapping) or not manifest:
            raise ValueError("non-empty artifact manifest required")
        if restored_from is not None:
            if not isinstance(restored_from, str) or not restored_from.strip():
                raise ValueError("restored_from must be a non-empty revision id")
            if carry_forward:
                raise ValueError("a restore publishes the whole target manifest")
        sizes: dict[str, int] = {}
        for artifact_id, digest in manifest.items():
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                raise ValueError("artifact ID required")
            # stage/verify outside the SQL transaction
            sizes[artifact_id] = len(self._verified_blob(digest))
        content = dict(sorted(manifest.items()))
        payload = {"revision_id": revision_id, "manifest": content,
                   "expected_version": expected_version}
        payload_hash = canonical_hash(payload)
        with self._tx() as cur:
            # An optional current-authority recheck, run FIRST
            # (before the workspace head lock) inside this SAME transaction
            # -- never a separate call/transaction before or after
            # `publish_source`, which would not be
            # atomic. Locking account/membership before the workspace head
            # row matches `commit_source_import`'s documented lock order.
            if authority_check is not None:
                authority_check(cur)
            if fence is not None:
                # Fencing (card: late worker must not commit): same ownership
                # predicate as `ack_outbox`, row-locked so a takeover cannot
                # interleave before this transaction commits.
                event_id, fence_worker, fence_token = fence
                cur.execute("""SELECT 1 FROM v2_outbox
                    WHERE tenant_id=%s AND workspace_id=%s AND id=%s
                      AND lease_owner=%s AND fencing_token=%s
                      AND status='CLAIMED' AND lease_until > now() FOR UPDATE""",
                    (tenant_id, workspace_id, event_id, fence_worker, fence_token))
                if cur.fetchone() is None:
                    raise LeaseLost("outbox lease lost; publish fenced")
            head = self._lock_head(cur, tenant_id, workspace_id)
            prior = self._replay(cur, tenant_id, workspace_id, operation,
                                 idempotency_key, payload_hash)
            if prior is not None:
                return prior
            if head_check is not None:
                head_check(head)
            if locked_check is not None:
                locked_check(cur, head)       # on THIS cursor, after the workspace row is locked: nothing can change between the check and the write
            if head.version != expected_version:
                raise StaleVersion("stale_revision")
            base: dict[str, str] = {}
            meta_revision_id = head.source_revision_id
            if carry_forward:
                cur.execute("""SELECT artifact_id, blob_hash FROM v2_artifact_revisions
                    WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s""",
                    (tenant_id, workspace_id, head.source_revision_id))
                base = {r[0]: r[1] for r in cur.fetchall()}
            elif restored_from is not None:
                if restored_from == head.source_revision_id:
                    raise StoreConflict("restore_target_is_current")
                cur.execute("""SELECT artifact_id, blob_hash FROM v2_artifact_revisions
                    WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s""",
                    (tenant_id, workspace_id, restored_from))
                base = {r[0]: r[1] for r in cur.fetchall()}
                if not base:
                    raise StoreConflict("restore_target_missing")
                if dict(sorted(base.items())) != content:
                    raise StoreConflict("restore_manifest_mismatch")
                meta_revision_id = restored_from
            full = dict(sorted({**base, **content}.items()))
            cur.execute("""INSERT INTO v2_source_revisions
                (tenant_id, workspace_id, revision_id, parent_revision_id, manifest,
                 restored_from, trigger)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, workspace_id, revision_id, head.source_revision_id,
                 Json(full), restored_from, trigger))
            for artifact_id, digest in full.items():
                cur.execute("""INSERT INTO v2_blob_refs
                    (tenant_id, workspace_id, blob_hash) VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING""", (tenant_id, workspace_id, digest))
                if (carry_forward or restored_from is not None) and artifact_id in base:
                    # Carry the parent's row metadata (a restore: the TARGET's) (logical_path, media
                    # type, role, ...); an overridden artifact keeps its
                    # identity metadata but gets the new hash and byte size.
                    cur.execute("""INSERT INTO v2_artifact_revisions
                        (tenant_id, workspace_id, source_revision_id, artifact_id,
                         blob_hash, logical_path, logical_path_key, media_type,
                         byte_size, source_role, source_role_origin, provenance)
                        SELECT tenant_id, workspace_id, %s, artifact_id, %s,
                            logical_path, logical_path_key, media_type,
                            COALESCE(%s, byte_size), source_role, source_role_origin,
                            provenance
                        FROM v2_artifact_revisions
                        WHERE tenant_id=%s AND workspace_id=%s
                          AND source_revision_id=%s AND artifact_id=%s""",
                        (revision_id, digest, sizes.get(artifact_id), tenant_id,
                         workspace_id, meta_revision_id, artifact_id))
                else:
                    cur.execute("""INSERT INTO v2_artifact_revisions
                        (tenant_id, workspace_id, source_revision_id, artifact_id, blob_hash)
                        VALUES (%s,%s,%s,%s,%s)""",
                        (tenant_id, workspace_id, revision_id, artifact_id, digest))
            version = head.version + 1
            cur.execute("""UPDATE v2_workspaces SET version=%s,
                source_revision_id=%s, knowledge_revision_id=NULL
                WHERE tenant_id=%s AND workspace_id=%s""",
                (version, revision_id, tenant_id, workspace_id))
            result = PublishResult(version, revision_id)
            self._record_commit(cur, tenant_id, workspace_id, operation,
                                idempotency_key, payload_hash, result,
                                "SOURCE_PUBLISHED",
                                {"source_revision_id": revision_id, "version": version,
                                 **dict(audit_context or {})})
            if before_commit is not None:
                before_commit()
            return result

    # ------------------------------------------------------------------ source import

    def prepare_source_import(self, tenant_id: str, workspace_id: str, *, actor_id: str,
                              idempotency_key: str, items: list[dict],
                              language: str | None) -> dict:
        """Record an admitted, staged import as PREPARED. Never touches the
        workspace head, revisions or blob refs: an uncommitted import is not
        current. `items` are admission preview dicts whose blobs the caller
        has already staged. A retried key with the same payload returns the
        same import; a different payload raises `StoreConflict`."""
        if not all(isinstance(x, str) and x.strip()
                   for x in (tenant_id, workspace_id, actor_id, idempotency_key)):
            raise ValueError("scope, actor and idempotency key required")
        if not items:
            raise ValueError("non-empty admission report required")
        for item in items:
            if item["disposition"] == "admitted":
                self._verified_blob(item["blob_hash"])
        report = {"items": list(items), "language": language}
        report_hash = canonical_hash(report)
        operation = "source.import.prepare"
        with self._tx() as cur:
            head = self._lock_head(cur, tenant_id, workspace_id)
            cur.execute("""SELECT payload_hash, result FROM v2_idempotency
                WHERE tenant_id=%s AND workspace_id=%s AND operation=%s
                  AND idempotency_key=%s""",
                (tenant_id, workspace_id, operation, idempotency_key))
            row = cur.fetchone()
            if row is not None:
                if row[0] != report_hash:
                    raise StoreConflict("idempotency_conflict")
                import_id = _as_object(row[1])["revision_id"]
            else:
                import_id = "imp-" + uuid.uuid4().hex
                cur.execute("""INSERT INTO v2_source_imports
                    (tenant_id, workspace_id, import_id, created_by, status, base_version,
                     base_source_revision_id, language, report, report_hash, expires_at)
                    VALUES (%s,%s,%s,%s,'PREPARED',%s,%s,%s,%s,%s,
                            now() + make_interval(hours => %s))""",
                    (tenant_id, workspace_id, import_id, actor_id, head.version,
                     head.source_revision_id, language, Json(report), report_hash,
                     SOURCE_IMPORT_TTL_HOURS))
                cur.execute("""INSERT INTO v2_idempotency
                    (tenant_id, workspace_id, operation, idempotency_key, payload_hash, result)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, operation, idempotency_key, report_hash,
                     Json({"version": head.version, "revision_id": import_id})))
                skipped = sum(1 for i in items if i["disposition"] != "admitted")
                cur.execute("""INSERT INTO v2_audit
                    (tenant_id, workspace_id, event_type, actor_id, payload)
                    VALUES (%s,%s,'SOURCE_IMPORT_PREPARED',%s,%s)""",
                    (tenant_id, workspace_id, actor_id,
                     Json({"import_id": import_id, "report_hash": report_hash,
                           "admitted": len(items) - skipped, "skip_required": skipped,
                           "base_version": head.version})))
        return self.source_import(tenant_id, workspace_id, import_id)

    @staticmethod
    def _index_build_status(cur, tenant_id: str, workspace_id: str,
                            source_revision_id: str | None) -> tuple[str, str | None] | None:
        """T04-RV2 (REVIEWER round 2): the durable `v2_index_builds` row for
        THIS revision is the CURRENT source of truth for index readiness --
        `build_retrieval_index` is a valid recovery entry point that can
        rebuild a previously-failed index to `READY` without touching the
        job's own (immutable, historical) `execution_result` snapshot. A
        caller must never keep reporting a stale `failed` reason from that
        old snapshot once a later rebuild has actually succeeded. Returns
        `None` when no row exists yet (caller falls back to the job
        snapshot to distinguish `not_built` from a crash that left no row).
        """
        if source_revision_id is None:
            return None
        cur.execute("""SELECT status FROM v2_index_builds
            WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s""",
            (tenant_id, workspace_id, source_revision_id))
        row = cur.fetchone()
        if row is None:
            return None
        return ("ready", None) if row[0] == "READY" else ("failed", None)

    @staticmethod
    def _ingest_coverage(cur, tenant_id: str, workspace_id: str, job_id: str | None, *,
                         source_revision_id: str | None = None) -> dict:
        """T04-RV2 (REVIEWER round 1): the real ANALYSIS/parser coverage
        claim, read from the `source_ingest` job's own terminal result --
        never derived from the admission report and never defaulted to
        `True`. Distinct from admission completeness (no item required a
        skip acknowledgement), which says nothing about whether any bytes
        have actually been parsed yet.

        T04-RV2 (REVIEWER round 1, closure): `status` now distinguishes
        `running` (claimed, `execution_status='RUNNING'`) from the
        already-reviewed `pending` value (AI2-T04's own contract,
        `tests/ai2t04/test_import_prepare_commit.py`'s
        `coverage_status == "pending"` for a freshly queued job -- kept
        unchanged, not renamed) -- the source inspection surface must be
        able to show a caller whether a job is still waiting or actively
        being worked, without breaking the already-reviewed `pending`
        value for the not-yet-claimed case.
        Also surfaces the INDEX build's own terminal state (`index_status`/
        `index_reason`, from `SourceIngestExecutor`'s `retrieval_index`
        result) separately from parser/analysis coverage: a job can be
        `COMPLETED` (parser coverage `complete`) while its index build
        failed, and the inspection surface must not call that `ready`
        without saying so. `semantic_stage` is always `not_run` -- AI4's
        provider semantic stage does not exist yet; this task never fakes
        findings for it (task card, explicit)."""
        base_semantic = {"semantic_stage": "not_run"}
        if job_id is None:
            return {"complete": False, "status": "not_started",
                    "index_status": "not_built", "index_reason": None, **base_semantic}
        cur.execute("""SELECT execution_status, execution_result FROM v2_jobs
            WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
            (tenant_id, workspace_id, job_id))
        row = cur.fetchone()
        if row is None:
            return {"complete": False, "status": "not_started",
                    "index_status": "not_built", "index_reason": None, **base_semantic}
        execution_status, execution_result = row
        if execution_status == "COMPLETED":
            result = _as_object(execution_result) or {}
            complete = bool(result.get("coverage_complete"))
            durable = PostgresV2Store._index_build_status(cur, tenant_id, workspace_id,
                                                          source_revision_id)
            if durable is not None:
                # The durable v2_index_builds row (possibly updated by a
                # LATER successful rebuild) always wins over this job's own
                # historical execution_result snapshot.
                index_status, index_reason = durable
                if index_status == "failed":
                    index = result.get("retrieval_index") or {}
                    index_reason = index.get("reason") if index.get("status") == "FAILED" else None
            else:
                index = result.get("retrieval_index") or {}
                index_status_raw = index.get("status")
                index_status = ({"READY": "ready", "FAILED": "failed"}.get(index_status_raw)
                                or "not_built")
                index_reason = index.get("reason") if index_status == "failed" else None
            return {"complete": complete,
                    "status": "complete" if complete else "parsed_incomplete",
                    "index_status": index_status,
                    "index_reason": index_reason,
                    **base_semantic}
        if execution_status == "FAILED":
            return {"complete": False, "status": "failed",
                    "index_status": "not_built", "index_reason": None, **base_semantic}
        if execution_status == "RUNNING":
            return {"complete": False, "status": "running",
                    "index_status": "not_built", "index_reason": None, **base_semantic}
        return {"complete": False, "status": "pending",
                "index_status": "not_built", "index_reason": None, **base_semantic}

    def source_import(self, tenant_id: str, workspace_id: str, import_id: str) -> dict:
        """Manifest preview DTO of one import, scoped to (tenant, workspace)."""
        with self._tx() as cur:
            cur.execute("""SELECT i.status, i.base_version, i.report, i.expires_at,
                    i.committed_revision_id, i.ingest_job_id, w.project_id
                FROM v2_source_imports AS i JOIN v2_workspaces AS w
                  ON w.tenant_id=i.tenant_id AND w.workspace_id=i.workspace_id
                WHERE i.tenant_id=%s AND i.workspace_id=%s AND i.import_id=%s""",
                (tenant_id, workspace_id, import_id))
            row = cur.fetchone()
            if row is None:
                raise TenantScopeError("import not found")
            policy = self._model_policy(cur, tenant_id, row[6])
            ingest_coverage = self._ingest_coverage(cur, tenant_id, workspace_id, row[5],
                                                   source_revision_id=row[4])
        items = _as_object(row[2])["items"]
        skipped = [i["logical_path"] for i in items if i["disposition"] != "admitted"]
        return {
            "import_id": import_id, "status": row[0], "base_version": row[1],
            "expires_at": row[3].isoformat(),
            "items": items,
            "summary": {"file_count": len(items),
                        "total_bytes": sum(i["byte_size"] for i in items),
                        "admitted": len(items) - len(skipped),
                        "skip_required": len(skipped)},
            "acknowledgement_required": skipped,
            # Admission completeness (no skip required) is NOT
            # analysis coverage. The latter comes from the ingest job's own
            # recorded result and is never inferred from admission alone.
            "admission_complete": not skipped,
            "coverage_complete": ingest_coverage["complete"],
            "coverage_status": ingest_coverage["status"],
            "model_policy": {"configured": policy["configured"],
                             "external_provider": (policy["external_provider"]
                                                   if policy["configured"] else None)},
            "source_revision_id": row[4], "ingest_job_id": row[5],
        }

    def commit_source_import(self, tenant_id: str, workspace_id: str, import_id: str, *,
                             actor_id: str, expected_version: int,
                             acknowledged_skips, source_roles: Mapping[str, str] | None,
                             required_permission: str = "source.import",
                             before_commit: Callable[[], None] | None = None) -> dict:
        """Turn a PREPARED import into an immutable current revision plus a
        durable `source_ingest` job, in ONE transaction under the workspace
        row lock: revision, artifact metadata, blob refs, head CAS, import
        status, audit, job row and its JOB_QUEUED outbox event. A retry of an
        already-committed import returns the recorded result (`replayed`);
        it never creates a second revision.

        `actor_id`'s CURRENT authority is
        rechecked here, under the same row locks as the write, exactly like
        `recheck_and_publish_job` -- not merely trusted from the route's
        earlier, now-possibly-stale, authority snapshot. A revoke that
        commits before this transaction starts is observed directly; one
        that commits concurrently is forced to block on the locked account/
        membership rows until this transaction resolves, so no window lets a
        stale authority snapshot and this write straddle an in-flight
        revoke."""
        from core.change_intelligence.v2.schema import (
            SOURCE_ROLES, ManifestEntry, SourceRevision, _compute_manifest_hash,
        )
        from core.change_intelligence.v2.source_admission import collision_key

        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if not isinstance(acknowledged_skips, (list, tuple, set, frozenset)) or \
                not all(isinstance(p, str) for p in acknowledged_skips):
            raise ValueError("acknowledged_skips must be a list of logical paths")
        roles = dict(source_roles or {})
        with self._tx() as cur:
            cur.execute("""SELECT report FROM v2_source_imports
                WHERE tenant_id=%s AND workspace_id=%s AND import_id=%s""",
                (tenant_id, workspace_id, import_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("import not found")
        # Blob stage/verify outside the SQL transaction (as publish_source).
        for item in _as_object(row[0])["items"]:
            if item["disposition"] == "admitted":
                self._verified_blob(item["blob_hash"])

        with self._tx() as cur:
            # Recheck the committing actor's CURRENT authority under
            # `FOR UPDATE`, in the same transaction as the write, exactly
            # like `recheck_and_publish_job` -- before any revision/head/job
            # write below, and (deliberately) before the `_lock_head`
            # workspace lock. `PostgresIdentityStore.revoke_membership`/
            # `_grant` lock (membership, then workspace) in that order; this
            # method must lock account/membership STRICTLY BEFORE workspace
            # to keep a single, consistent lock order across both modules --
            # locking workspace first here (the reverse order) would let a
            # concurrent revoke and this commit hold one row each and wait
            # on the other (ABBA deadlock), confirmed by an earlier version
            # of this fix under the exact concurrent-revoke test below.
            cur.execute("""SELECT disabled_at,
                    (credential_expires_at IS NOT NULL AND credential_expires_at <= now())
                FROM v2_accounts WHERE account_id=%s FOR UPDATE""", (actor_id,))
            account_row = cur.fetchone()
            if account_row is None or account_row[0] is not None or account_row[1]:
                raise StaleAuthorityError("actor authority revoked; commit blocked")
            cur.execute("""SELECT roles FROM v2_memberships
                WHERE tenant_id=%s AND workspace_id=%s AND account_id=%s FOR UPDATE""",
                (tenant_id, workspace_id, actor_id))
            membership_row = cur.fetchone()
            if membership_row is None:
                raise StaleAuthorityError("actor authority revoked; commit blocked")
            if required_permission not in permissions_for(membership_row[0]):
                raise StaleAuthorityError(
                    "actor no longer holds the required permission; commit blocked")
            head = self._lock_head(cur, tenant_id, workspace_id)
            cur.execute("""SELECT status, base_version, report, expires_at < now(),
                    commit_result
                FROM v2_source_imports
                WHERE tenant_id=%s AND workspace_id=%s AND import_id=%s FOR UPDATE""",
                (tenant_id, workspace_id, import_id))
            status, base_version, report, expired, commit_result = cur.fetchone()
            if status == "COMMITTED":
                return {**_as_object(commit_result), "replayed": True}
            if expired:
                raise ImportExpired("import_expired")
            if head.version != expected_version or head.version != base_version:
                raise StaleVersion("stale_revision")
            items = _as_object(report)["items"]
            admitted = [i for i in items if i["disposition"] == "admitted"]
            skipped = [i for i in items if i["disposition"] != "admitted"]
            if set(acknowledged_skips) != {i["logical_path"] for i in skipped} or \
                    len(set(acknowledged_skips)) != len(acknowledged_skips):
                raise SkipAcknowledgementRequired("skip_acknowledgement_required")
            if not admitted:
                raise NoAdmittedItems("no_admitted_items")
            admitted_paths = {i["logical_path"] for i in admitted}
            for path, role in roles.items():
                if path not in admitted_paths:
                    raise ValueError(f"source_roles names a path that is not admitted: {path!r}")
                if role not in SOURCE_ROLES:
                    raise ValueError(f"invalid source_role {role!r}")

            # Artifact identity: the exact same normalized logical path in the
            # parent revision keeps its artifact_id; nothing is guessed from
            # similar names (contracts §2.1).
            parent_ids: dict[str, str] = {}
            if head.source_revision_id is not None:
                cur.execute("""SELECT logical_path_key, artifact_id FROM v2_artifact_revisions
                    WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                      AND logical_path_key IS NOT NULL""",
                    (tenant_id, workspace_id, head.source_revision_id))
                parent_ids = dict(cur.fetchall())
            revision_id = "src-" + import_id
            artifacts = []
            for item in admitted:
                key = collision_key(item["logical_path"])
                artifact_id = parent_ids.get(key) or "art-" + hashlib.sha256(
                    f"{import_id}\0{key}".encode("utf-8")).hexdigest()[:32]
                if item["logical_path"] in roles:
                    role, origin = roles[item["logical_path"]], "human_confirmed"
                else:
                    role, origin = item["source_role"], item["source_role_origin"]
                artifacts.append({**item, "artifact_id": artifact_id, "logical_path_key": key,
                                  "source_role": role, "source_role_origin": origin})
            artifacts.sort(key=lambda a: a["artifact_id"])
            entries = tuple(ManifestEntry(a["artifact_id"], f"{revision_id}/{a['artifact_id']}",
                                          a["blob_hash"]) for a in artifacts)
            manifest_hash = _compute_manifest_hash(entries)
            cur.execute("SELECT now()")
            created_at = cur.fetchone()[0]
            SourceRevision(id=revision_id, parent_id=head.source_revision_id, manifest=entries,
                           manifest_hash=manifest_hash, created_by=actor_id,
                           created_at=created_at.isoformat(), trigger="import",
                           restored_from=None, source_connector_id=import_id)

            manifest = {a["artifact_id"]: a["blob_hash"] for a in artifacts}
            cur.execute("""INSERT INTO v2_source_revisions
                (tenant_id, workspace_id, revision_id, parent_revision_id, manifest,
                 manifest_hash, created_by, trigger, source_connector_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'import',%s)""",
                (tenant_id, workspace_id, revision_id, head.source_revision_id,
                 Json(manifest), manifest_hash, actor_id, import_id))
            for a in artifacts:
                cur.execute("""INSERT INTO v2_blob_refs
                    (tenant_id, workspace_id, blob_hash) VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING""", (tenant_id, workspace_id, a["blob_hash"]))
                cur.execute("""INSERT INTO v2_artifact_revisions
                    (tenant_id, workspace_id, source_revision_id, artifact_id, blob_hash,
                     logical_path, logical_path_key, media_type, byte_size, source_role,
                     source_role_origin, provenance)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'original_upload')""",
                    (tenant_id, workspace_id, revision_id, a["artifact_id"], a["blob_hash"],
                     a["logical_path"], a["logical_path_key"], a["media_type"],
                     a["byte_size"], a["source_role"], a["source_role_origin"]))
            version = head.version + 1
            cur.execute("""UPDATE v2_workspaces SET version=%s,
                source_revision_id=%s, knowledge_revision_id=NULL
                WHERE tenant_id=%s AND workspace_id=%s""",
                (version, revision_id, tenant_id, workspace_id))

            job_id = "ingest-" + import_id
            job_payload = {"import_id": import_id, "source_revision_id": revision_id,
                           "manifest_hash": manifest_hash}
            cur.execute("""INSERT INTO v2_jobs
                (tenant_id, workspace_id, job_id, kind, status, execution_status,
                 pinned_source_revision_id, pinned_knowledge_revision_id,
                 pinned_decision_epoch, payload, requested_by)
                VALUES (%s,%s,%s,'source_ingest','NOT_REQUIRED','QUEUED',%s,NULL,%s,%s,%s)""",
                (tenant_id, workspace_id, job_id, revision_id, head.decision_epoch,
                 Json(job_payload), actor_id))
            self._link_job(cur, tenant_id, workspace_id, job_id, created=True)
            result = {"import_id": import_id, "source_revision_id": revision_id,
                      "version": version, "manifest_hash": manifest_hash,
                      "ingest_job_id": job_id,
                      # Admission completeness (no skip required) is
                      # a distinct claim from analysis coverage. The
                      # `source_ingest` job has just been queued, not run, so
                      # coverage is always "pending" in this synchronous
                      # response -- poll GET .../imports/{id} or
                      # GET .../revisions/{id} for the job's real result.
                      "admission_complete": not skipped,
                      "coverage_complete": False, "coverage_status": "pending"}
            cur.execute("""UPDATE v2_source_imports SET status='COMMITTED',
                committed_revision_id=%s, committed_by=%s, committed_at=now(),
                ingest_job_id=%s, commit_result=%s
                WHERE tenant_id=%s AND workspace_id=%s AND import_id=%s""",
                (revision_id, actor_id, job_id, Json(result), tenant_id, workspace_id,
                 import_id))
            for event_type, payload in (
                    ("SOURCE_PUBLISHED", {"source_revision_id": revision_id,
                                          "version": version, "import_id": import_id,
                                          "manifest_hash": manifest_hash,
                                          "skipped": len(skipped)}),
                    ("JOB_QUEUED", {"job_id": job_id, "kind": "source_ingest",
                                    "source_revision_id": revision_id,
                                    "decision_epoch": head.decision_epoch})):
                cur.execute("""INSERT INTO v2_audit
                    (tenant_id, workspace_id, event_type, actor_id, payload)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, event_type, actor_id, Json(payload)))
            # The ingest job is the durable reanalysis link for this source
            # publish (contracts C13): one outbox event, consumed by the worker.
            cur.execute("""INSERT INTO v2_outbox
                (tenant_id, workspace_id, kind, payload) VALUES (%s,%s,'JOB_QUEUED',%s)""",
                (tenant_id, workspace_id,
                 Json({"job_id": job_id, "kind": "source_ingest",
                       "source_revision_id": revision_id,
                       "decision_epoch": head.decision_epoch})))
            if before_commit is not None:
                before_commit()
            return {**result, "replayed": False}

    # ------------------------------------------------------------------ manual source refresh
    #
    # A refresh is an external OBSERVATION of the source, not a ChangeSet: it reuses
    # the source admission and the import commit's transaction shape (authority recheck ->
    # head lock -> CAS -> revision + artifacts + head + audit + outbox + ingest job),
    # but it never touches `publish_source` (the apply gate's primitive), the decision
    # ledger or any approval table. The pure diff / proof rules live in
    # `core/change_intelligence/v2/source_refresh.py`.

    @staticmethod
    def _head_artifact_rows(cur, tenant_id: str, workspace_id: str,
                            revision_id: str | None) -> list[dict]:
        if revision_id is None:
            return []
        cur.execute("""SELECT artifact_id, logical_path, blob_hash, source_role,
                source_role_origin, media_type
            FROM v2_artifact_revisions
            WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
            ORDER BY artifact_id""", (tenant_id, workspace_id, revision_id))
        return [dict(zip(("artifact_id", "logical_path", "blob_hash", "source_role",
                          "source_role_origin", "media_type"), r)) for r in cur.fetchall()]

    def prepare_source_refresh(self, tenant_id: str, workspace_id: str, *, actor_id: str,
                               idempotency_key: str, items: list[dict], rename_proofs,
                               language: str | None) -> dict:
        """Record an admitted, staged upload as a PREPARED refresh against the CURRENT
        head: computes the diff, validates rename proofs (a forged / unknown proof
        raises `RefreshProofError` and NOTHING is written), and stores the digest the
        commit must echo. Never touches the head, revisions, jobs, outbox, decisions or
        approvals (only a PREPARED row + audit + idempotency record). A `noop` refresh
        (whole-content identical to the head) is recorded but can never publish."""
        from core.change_intelligence.v2 import source_refresh as sr

        if not all(isinstance(x, str) and x.strip()
                   for x in (tenant_id, workspace_id, actor_id, idempotency_key)):
            raise ValueError("scope, actor and idempotency key required")
        if not items or any(i["disposition"] != "admitted" for i in items):
            raise ValueError("a refresh needs a non-empty, fully admitted upload")
        proofs = sr.normalise_proofs(rename_proofs)
        for item in items:
            self._verified_blob(item["blob_hash"])
        report = {"items": list(items), "rename_proofs": proofs, "language": language}
        report_hash = canonical_hash(report)
        operation = "source.refresh.prepare"
        with self._tx() as cur:
            head = self._lock_head(cur, tenant_id, workspace_id)
            cur.execute("""SELECT payload_hash, result FROM v2_idempotency
                WHERE tenant_id=%s AND workspace_id=%s AND operation=%s
                  AND idempotency_key=%s""",
                (tenant_id, workspace_id, operation, idempotency_key))
            row = cur.fetchone()
            if row is not None:
                if row[0] != report_hash:
                    raise StoreConflict("idempotency_conflict")
                refresh_id = _as_object(row[1])["revision_id"]
            else:
                if head.source_revision_id is None:
                    raise StoreConflict("no_current_revision")
                rows = self._head_artifact_rows(cur, tenant_id, workspace_id,
                                                head.source_revision_id)
                diff = sr.compute_diff(rows, items, proofs)
                digest = sr.refresh_digest(
                    {"version": head.version, "source_revision_id": head.source_revision_id},
                    diff, items, proofs)
                refresh_id = "rfr-" + uuid.uuid4().hex
                cur.execute("""INSERT INTO v2_source_refreshes
                    (tenant_id, workspace_id, refresh_id, created_by, status, base_version,
                     base_source_revision_id, noop, language, report, report_hash, diff,
                     refresh_digest, expires_at)
                    VALUES (%s,%s,%s,%s,'PREPARED',%s,%s,%s,%s,%s,%s,%s,%s,
                            now() + make_interval(hours => %s))""",
                    (tenant_id, workspace_id, refresh_id, actor_id, head.version,
                     head.source_revision_id, diff["noop"], language, Json(report),
                     report_hash, Json(sr.public_diff(diff)), digest,
                     SOURCE_IMPORT_TTL_HOURS))
                cur.execute("""INSERT INTO v2_idempotency
                    (tenant_id, workspace_id, operation, idempotency_key, payload_hash, result)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, operation, idempotency_key, report_hash,
                     Json({"version": head.version, "revision_id": refresh_id})))
                if not diff["noop"]:
                    # A whole-content identical upload observed nothing: it records
                    # its (non-publishable) PREPARED row but writes no audit event,
                    # so repeated identical refreshes leave no audit noise.
                    cur.execute("""INSERT INTO v2_audit
                        (tenant_id, workspace_id, event_type, actor_id, payload)
                        VALUES (%s,%s,'SOURCE_REFRESH_PREPARED',%s,%s)""",
                        (tenant_id, workspace_id, actor_id,
                         Json({"refresh_id": refresh_id, "refresh_digest": digest,
                               "provenance": {"kind": sr.PROVENANCE_KIND, "actor_id": actor_id},
                               "noop": False, "base_version": head.version,
                               "summary": {k: len(diff[k]) for k in
                                           ("added", "changed", "deleted", "renamed")}})))
        return self.source_refresh(tenant_id, workspace_id, refresh_id)

    def source_refresh(self, tenant_id: str, workspace_id: str, refresh_id: str) -> dict:
        """Durable view of one refresh row, scoped to (tenant, workspace)."""
        with self._tx() as cur:
            cur.execute("""SELECT r.status, r.base_version, r.base_source_revision_id, r.noop,
                    r.diff, r.refresh_digest, r.created_by, r.expires_at,
                    r.committed_revision_id, r.committed_by, r.ingest_job_id,
                    r.dropped_knowledge_revision_id, r.commit_result, r.impact, r.report,
                    (SELECT v.manifest_hash FROM v2_source_revisions AS v
                      WHERE v.tenant_id=r.tenant_id AND v.workspace_id=r.workspace_id
                        AND v.revision_id=r.base_source_revision_id)
                FROM v2_source_refreshes AS r
                WHERE r.tenant_id=%s AND r.workspace_id=%s AND r.refresh_id=%s""",
                (tenant_id, workspace_id, refresh_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("refresh not found")
        return {"refresh_id": refresh_id, "workspace_id": workspace_id, "status": row[0],
                "noop": row[3],
                "base": {"version": row[1], "source_revision_id": row[2],
                         "manifest_hash": row[15]},
                "provenance": {"kind": "external_observation", "actor_id": row[6]},
                "diff": _as_object(row[4]), "refresh_digest": row[5],
                "expires_at": row[7].isoformat(), "source_revision_id": row[8],
                "committed_by": row[9], "ingest_job_id": row[10],
                "dropped_knowledge_revision_id": row[11],
                "commit_result": _as_object(row[12]), "impact": _as_object(row[13]),
                "rename_proofs": (_as_object(row[14]) or {}).get("rename_proofs") or []}

    def commit_source_refresh(self, tenant_id: str, workspace_id: str, refresh_id: str, *,
                              actor_id: str, expected_version: int,
                              expected_refresh_digest: str, acknowledged_deletions,
                              required_permission: str = "source.import",
                              before_commit: Callable[[], None] | None = None) -> dict:
        """Turn a PREPARED refresh into an immutable current revision
        (`trigger="refresh"`, parent = head, WHOLE snapshot) plus a durable
        `source_ingest` job, in ONE transaction under the workspace row lock: revision,
        artifact rows, blob refs, head CAS (KM head -> NULL, decision epoch untouched),
        refresh status, audit (SOURCE_REFRESH_COMMITTED with `external_observation`
        provenance + SOURCE_PUBLISHED), the SOURCE_PUBLISHED outbox event the
        invalidation consumer reads, and the ingest job's JOB_QUEUED event.

        `actor_id`'s CURRENT authority is rechecked under the same locks (account,
        membership, then workspace: the shared lock order). A whole-content identical
        refresh (`noop`) writes NO revision / job / outbox / audit and never bumps the
        version. A committed refresh replays its recorded result. The diff is
        RE-COMPUTED under the head lock and must reproduce the reviewed digest, so the
        bytes that go live are exactly what the human confirmed. Never writes
        decisions, change decisions or approvals."""
        from core.change_intelligence.v2 import source_refresh as sr
        from core.change_intelligence.v2.schema import (
            ManifestEntry, SourceRevision, _compute_manifest_hash,
        )
        from core.change_intelligence.v2.source_admission import collision_key

        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if not isinstance(expected_refresh_digest, str) or not expected_refresh_digest:
            raise ValueError("expected_refresh_digest required")
        if not isinstance(acknowledged_deletions, (list, tuple, set, frozenset)) or \
                not all(isinstance(p, str) for p in acknowledged_deletions):
            raise ValueError("acknowledged_deletions must be a list of logical paths")
        with self._tx() as cur:
            cur.execute("""SELECT report, status FROM v2_source_refreshes
                WHERE tenant_id=%s AND workspace_id=%s AND refresh_id=%s""",
                (tenant_id, workspace_id, refresh_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("refresh not found")
        if row[1] == "PREPARED":
            # Blob stage/verify outside the SQL transaction (as import).
            for item in _as_object(row[0])["items"]:
                self._verified_blob(item["blob_hash"])

        with self._tx() as cur:
            self._recheck_actor_authority(cur, tenant_id, workspace_id, actor_id,
                                          required_permission)
            head = self._lock_head(cur, tenant_id, workspace_id)
            cur.execute("""SELECT status, base_version, base_source_revision_id, report,
                    refresh_digest, expires_at < now(), commit_result, noop
                FROM v2_source_refreshes
                WHERE tenant_id=%s AND workspace_id=%s AND refresh_id=%s FOR UPDATE""",
                (tenant_id, workspace_id, refresh_id))
            (status, base_version, base_revision, report, stored_digest, expired,
             commit_result, _noop) = cur.fetchone()
            if status in ("COMMITTED", "NOOP"):
                return {**_as_object(commit_result), "replayed": True}
            if expired:
                raise RefreshExpired("refresh_expired")
            if stored_digest != expected_refresh_digest:
                raise RefreshDigestMismatch("refresh_digest_mismatch")
            if (head.version != expected_version or head.version != base_version
                    or head.source_revision_id != base_revision):
                raise StaleVersion("stale_revision")
            report = _as_object(report)
            items, proofs = report["items"], report["rename_proofs"]
            rows = self._head_artifact_rows(cur, tenant_id, workspace_id, head.source_revision_id)
            diff = sr.compute_diff(rows, items, proofs)
            recomputed = sr.refresh_digest(
                {"version": head.version, "source_revision_id": head.source_revision_id},
                diff, items, proofs)
            if recomputed != stored_digest:
                raise RefreshDigestMismatch("refresh_digest_mismatch")
            deleted_paths = [d["logical_path"] for d in diff["deleted"]]
            if set(acknowledged_deletions) != set(deleted_paths) or \
                    len(set(acknowledged_deletions)) != len(acknowledged_deletions):
                raise DeletionAcknowledgementRequired("deletion_acknowledgement_required")
            provenance = {"kind": sr.PROVENANCE_KIND, "actor_id": actor_id}
            if diff["noop"]:
                result = {"refresh_id": refresh_id, "noop": True,
                          "source_revision_id": head.source_revision_id,
                          "version": head.version, "ingest_job_id": None,
                          "provenance": provenance, "reanalysis": {"state": "complete"}}
                cur.execute("""UPDATE v2_source_refreshes SET status='NOOP', committed_by=%s,
                    committed_at=now(), commit_result=%s
                    WHERE tenant_id=%s AND workspace_id=%s AND refresh_id=%s""",
                    (actor_id, Json({**result, "replayed": False}), tenant_id, workspace_id,
                     refresh_id))
                return {**result, "replayed": False}

            head_by_id = {r["artifact_id"]: r for r in rows}
            artifacts = []
            for item in items:
                key = collision_key(item["logical_path"])
                artifact_id = diff["plan"][key] or "art-" + hashlib.sha256(
                    f"{refresh_id}\0{key}".encode("utf-8")).hexdigest()[:32]
                prior = head_by_id.get(artifact_id)
                if prior is not None and prior.get("source_role_origin") == "human_confirmed":
                    role, origin = prior["source_role"], "human_confirmed"
                else:
                    role, origin = item["source_role"], item["source_role_origin"]
                artifacts.append({**item, "artifact_id": artifact_id, "logical_path_key": key,
                                  "source_role": role, "source_role_origin": origin})
            artifacts.sort(key=lambda a: a["artifact_id"])
            revision_id = "src-" + refresh_id
            entries = tuple(ManifestEntry(a["artifact_id"], f"{revision_id}/{a['artifact_id']}",
                                          a["blob_hash"]) for a in artifacts)
            manifest_hash = _compute_manifest_hash(entries)
            cur.execute("SELECT now()")
            created_at = cur.fetchone()[0]
            SourceRevision(id=revision_id, parent_id=head.source_revision_id, manifest=entries,
                           manifest_hash=manifest_hash, created_by=actor_id,
                           created_at=created_at.isoformat(), trigger="refresh",
                           restored_from=None, source_connector_id=refresh_id)
            manifest = {a["artifact_id"]: a["blob_hash"] for a in artifacts}
            cur.execute("""INSERT INTO v2_source_revisions
                (tenant_id, workspace_id, revision_id, parent_revision_id, manifest,
                 manifest_hash, created_by, trigger, source_connector_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'refresh',%s)""",
                (tenant_id, workspace_id, revision_id, head.source_revision_id, Json(manifest),
                 manifest_hash, actor_id, refresh_id))
            for a in artifacts:
                cur.execute("""INSERT INTO v2_blob_refs
                    (tenant_id, workspace_id, blob_hash) VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING""", (tenant_id, workspace_id, a["blob_hash"]))
                cur.execute("""INSERT INTO v2_artifact_revisions
                    (tenant_id, workspace_id, source_revision_id, artifact_id, blob_hash,
                     logical_path, logical_path_key, media_type, byte_size, source_role,
                     source_role_origin, provenance)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, revision_id, a["artifact_id"], a["blob_hash"],
                     a["logical_path"], a["logical_path_key"], a["media_type"],
                     a["byte_size"], a["source_role"], a["source_role_origin"],
                     sr.PROVENANCE_KIND))
            version = head.version + 1
            cur.execute("""UPDATE v2_workspaces SET version=%s,
                source_revision_id=%s, knowledge_revision_id=NULL
                WHERE tenant_id=%s AND workspace_id=%s""",
                (version, revision_id, tenant_id, workspace_id))
            job_id = "ingest-" + refresh_id
            cur.execute("""INSERT INTO v2_jobs
                (tenant_id, workspace_id, job_id, kind, status, execution_status,
                 pinned_source_revision_id, pinned_knowledge_revision_id,
                 pinned_decision_epoch, payload, requested_by)
                VALUES (%s,%s,%s,'source_ingest','NOT_REQUIRED','QUEUED',%s,NULL,%s,%s,%s)""",
                (tenant_id, workspace_id, job_id, revision_id, head.decision_epoch,
                 Json({"refresh_id": refresh_id, "source_revision_id": revision_id,
                       "manifest_hash": manifest_hash}), actor_id))
            self._link_job(cur, tenant_id, workspace_id, job_id, created=True)
            result = {"refresh_id": refresh_id, "noop": False, "source_revision_id": revision_id,
                      "version": version, "manifest_hash": manifest_hash,
                      "ingest_job_id": job_id, "provenance": provenance,
                      "reanalysis": {"state": "pending"}}
            cur.execute("""UPDATE v2_source_refreshes SET status='COMMITTED',
                committed_revision_id=%s, committed_by=%s, committed_at=now(),
                ingest_job_id=%s, dropped_knowledge_revision_id=%s, commit_result=%s
                WHERE tenant_id=%s AND workspace_id=%s AND refresh_id=%s""",
                (revision_id, actor_id, job_id, head.knowledge_revision_id,
                 Json({**result, "replayed": False}), tenant_id, workspace_id, refresh_id))
            summary = {k: len(diff[k]) for k in ("added", "changed", "deleted", "renamed")}
            for event_type, payload in (
                    ("SOURCE_REFRESH_COMMITTED", {"refresh_id": refresh_id,
                                                  "provenance": provenance,
                                                  "source_revision_id": revision_id,
                                                  "version": version, "summary": summary,
                                                  "deleted": deleted_paths}),
                    ("SOURCE_PUBLISHED", {"source_revision_id": revision_id, "version": version,
                                          "trigger": "refresh", "refresh_id": refresh_id,
                                          "manifest_hash": manifest_hash}),
                    ("JOB_QUEUED", {"job_id": job_id, "kind": "source_ingest",
                                    "source_revision_id": revision_id,
                                    "decision_epoch": head.decision_epoch})):
                cur.execute("""INSERT INTO v2_audit
                    (tenant_id, workspace_id, event_type, actor_id, payload)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, event_type, actor_id, Json(payload)))
            # One SOURCE_PUBLISHED outbox row (trigger=refresh) for the invalidation
            # consumer, then the ingest job's own JOB_QUEUED row (ordered by id, so the
            # ingest runs before any reanalysis the consumer schedules).
            cur.execute("""INSERT INTO v2_outbox
                (tenant_id, workspace_id, kind, payload) VALUES (%s,%s,'SOURCE_PUBLISHED',%s)""",
                (tenant_id, workspace_id,
                 Json({"source_revision_id": revision_id, "version": version,
                       "trigger": "refresh", "refresh_id": refresh_id,
                       "manifest_hash": manifest_hash})))
            cur.execute("""INSERT INTO v2_outbox
                (tenant_id, workspace_id, kind, payload) VALUES (%s,%s,'JOB_QUEUED',%s)""",
                (tenant_id, workspace_id,
                 Json({"job_id": job_id, "kind": "source_ingest",
                       "source_revision_id": revision_id,
                       "decision_epoch": head.decision_epoch})))
            if before_commit is not None:
                before_commit()
            return {**result, "replayed": False}

    def set_refresh_impact(self, tenant_id: str, workspace_id: str, refresh_id: str,
                           impact: Mapping) -> dict:
        """Write-once durable impact assessment of a committed refresh: the FIRST
        writer wins, every later call (redelivered event, second worker) gets the
        recorded assessment back unchanged."""
        with self._tx() as cur:
            cur.execute("""UPDATE v2_source_refreshes SET impact=%s
                WHERE tenant_id=%s AND workspace_id=%s AND refresh_id=%s
                  AND impact IS NULL""", (Json(dict(impact)), tenant_id, workspace_id, refresh_id))
            cur.execute("""SELECT impact FROM v2_source_refreshes
                WHERE tenant_id=%s AND workspace_id=%s AND refresh_id=%s""",
                (tenant_id, workspace_id, refresh_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("refresh not found")
        return _as_object(row[0])

    def add_refresh_impact_notes(self, tenant_id: str, workspace_id: str, refresh_id: str,
                                 notes: list) -> None:
        """Append explicit non-convergence notes (deduplicated) to the recorded impact."""
        if not notes:
            return
        with self._tx() as cur:
            cur.execute("""SELECT impact FROM v2_source_refreshes
                WHERE tenant_id=%s AND workspace_id=%s AND refresh_id=%s FOR UPDATE""",
                (tenant_id, workspace_id, refresh_id))
            row = cur.fetchone()
            impact = _as_object(row[0]) if row is not None else None
            if impact is None:
                return
            merged = list(dict.fromkeys([*impact.get("notes", []), *notes]))
            if merged == impact.get("notes", []):
                return
            impact["notes"] = merged
            cur.execute("""UPDATE v2_source_refreshes SET impact=%s
                WHERE tenant_id=%s AND workspace_id=%s AND refresh_id=%s""",
                (Json(impact), tenant_id, workspace_id, refresh_id))

    def extraction_candidate_index(self, tenant_id: str, workspace_id: str) -> list[dict]:
        """Every COMPLETED `extraction` job with its persisted candidates (identity,
        artifact, quotes). Read-only; the candidates live inside each job's own
        `execution_result`, never in a table of their own."""
        with self._tx() as cur:
            cur.execute("""SELECT job_id, payload, execution_result FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'
                  AND execution_status='COMPLETED' AND execution_result IS NOT NULL
                ORDER BY job_id""", (tenant_id, workspace_id))
            rows = cur.fetchall()
        out = []
        for job_id, payload, result in rows:
            payload, result = _as_object(payload) or {}, _as_object(result) or {}
            candidates = []
            for cand in (result.get("candidates") or []):
                if not isinstance(cand, dict) or not cand.get("candidate_id"):
                    continue
                candidates.append({
                    "candidate_id": cand["candidate_id"],
                    "artifact_id": cand.get("artifact_revision_id"),
                    "quotes": [e.get("quote") for e in (cand.get("evidence") or [])
                               if isinstance(e, dict) and isinstance(e.get("quote"), str)]})
            out.append({"job_id": job_id, "artifact_id": payload.get("artifact_id"),
                        "source_role": payload.get("source_role"), "candidates": candidates})
        return out

    def active_decisions_for_targets(self, tenant_id: str, workspace_id: str,
                                     targets) -> list[dict]:
        """Read-only: the ACTIVE ledger decisions (not superseded / revoked by a later
        row -- the same derivation `active_decision_action` uses) whose target is in
        `targets`. Never writes."""
        targets = sorted(set(targets or ()))
        if not targets:
            return []
        with self._tx() as cur:
            cur.execute("""SELECT d.decision_id, d.target, d.action FROM v2_decisions AS d
                WHERE d.tenant_id=%s AND d.workspace_id=%s AND d.target = ANY(%s)
                  AND NOT EXISTS (
                    SELECT 1 FROM v2_decisions AS s
                    WHERE s.tenant_id=d.tenant_id AND s.workspace_id=d.workspace_id
                      AND (s.supersedes=d.decision_id OR s.revokes=d.decision_id))
                ORDER BY d.epoch""", (tenant_id, workspace_id, targets))
            return [{"decision_id": r[0], "target": r[1], "action": r[2]}
                    for r in cur.fetchall()]

    def decided_findings(self, tenant_id: str, workspace_id: str) -> list[dict]:
        """Every persisted finding that carries an ACTIVE human decision
        (`resolve` for a conflict / insufficient-context finding, `accepted_unknown` for a
        gap; a decision superseded or revoked by a later row is not active -- the same
        derivation `active_decision_action` uses). Read-only. Returns
        `{finding_id, kind, involved_claim_ids, decision_id, action}`."""
        with self._tx() as cur:
            cur.execute("""SELECT execution_result FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND kind='finding'
                  AND execution_status='COMPLETED' AND execution_result IS NOT NULL""",
                (tenant_id, workspace_id))
            findings = [f for (raw,) in cur.fetchall()
                        for f in ((_as_object(raw) or {}).get("findings") or [])
                        if isinstance(f, dict) and f.get("finding_id")]
            out = []
            for f in findings:
                cur.execute("""SELECT d.decision_id, d.action, d.payload FROM v2_decisions AS d
                    WHERE d.tenant_id=%s AND d.workspace_id=%s AND d.target=%s
                      AND d.action IN ('resolve','accepted_unknown')
                      AND NOT EXISTS (
                        SELECT 1 FROM v2_decisions AS s
                        WHERE s.tenant_id=d.tenant_id AND s.workspace_id=d.workspace_id
                          AND (s.supersedes=d.decision_id OR s.revokes=d.decision_id))
                    ORDER BY d.epoch DESC LIMIT 1""",
                    (tenant_id, workspace_id, f["finding_id"]))
                row = cur.fetchone()
                if row is not None:
                    out.append({"finding_id": f["finding_id"], "kind": f.get("kind"),
                                "involved_claim_ids": list(f.get("involved_claim_ids") or []),
                                "decision_id": row[0], "action": row[1],
                                "binding": (_as_object(row[2]) or {}).get("evidence_binding")})
        return out

    @staticmethod
    def _decision_binding(cur, tenant_id: str, workspace_id: str, finding_id: str,
                          head: WorkspaceHead) -> dict | None:
        """AI9 G-AI9-D2: the evidence state a resolve / accepted_unknown decision is
        taken on: for every claim of the decided finding, the artifact it was extracted
        from and that artifact's bytes (blob hash) in the CURRENT head, read under the
        same lock as the decision insert. None when the target is not a persisted
        finding (nothing to bind)."""
        cur.execute("""SELECT execution_result FROM v2_jobs
            WHERE tenant_id=%s AND workspace_id=%s AND kind='finding'
              AND execution_status='COMPLETED' AND execution_result IS NOT NULL""",
            (tenant_id, workspace_id))
        claim_ids = None
        for (raw,) in cur.fetchall():
            for finding in ((_as_object(raw) or {}).get("findings") or []):
                if isinstance(finding, dict) and finding.get("finding_id") == finding_id:
                    claim_ids = list(finding.get("involved_claim_ids") or [])
                    break
            if claim_ids is not None:
                break
        if claim_ids is None:
            return None
        cur.execute("""SELECT execution_result FROM v2_jobs
            WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'
              AND execution_status='COMPLETED' AND execution_result IS NOT NULL""",
            (tenant_id, workspace_id))
        artifact_of = {}
        for (raw,) in cur.fetchall():
            for cand in ((_as_object(raw) or {}).get("candidates") or []):
                if isinstance(cand, dict) and cand.get("candidate_id") in claim_ids:
                    artifact_of.setdefault(cand["candidate_id"], cand.get("artifact_revision_id"))
        claims = {}
        for cid in claim_ids:
            artifact_id, blob_hash = artifact_of.get(cid), None
            if artifact_id and head.source_revision_id:
                cur.execute("""SELECT blob_hash FROM v2_artifact_revisions
                    WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                      AND artifact_id=%s""",
                    (tenant_id, workspace_id, head.source_revision_id, artifact_id))
                row = cur.fetchone()
                blob_hash = row[0] if row else None
            claims[cid] = {"artifact_id": artifact_id, "blob_hash": blob_hash}
        return {"head_source_revision_id": head.source_revision_id, "claims": claims}

    def artifact_head_hash(self, tenant_id: str, workspace_id: str,
                           artifact_id: str) -> str | None:
        """Blob hash of `artifact_id` in the CURRENT head (None when absent). Read-only."""
        head = self.head(tenant_id, workspace_id)
        if head.source_revision_id is None:
            return None
        with self._tx() as cur:
            cur.execute("""SELECT blob_hash FROM v2_artifact_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                  AND artifact_id=%s""",
                (tenant_id, workspace_id, head.source_revision_id, artifact_id))
            row = cur.fetchone()
        return row[0] if row else None

    def claim_binding_stale(self, tenant_id: str, workspace_id: str,
                            candidate: Mapping) -> str | None:
        """Is the source binding a human decision rested on for this claim
        stale NOW? Stale when the claim's artifact exists in the current head with
        DIFFERENT bytes than in the revision the claim was extracted from (an edit, a
        refresh or a restore changed it), or when a committed refresh deleted it. A claim
        whose artifact is simply not in the head for any other reason (fixture heads
        replace the manifest) is not judged stale. Returns a reason or None."""
        artifact_id = candidate.get("artifact_revision_id")
        pinned = candidate.get("source_revision_id")
        if not artifact_id or not pinned:
            return None
        head = self.head(tenant_id, workspace_id)
        with self._tx() as cur:
            hashes = []
            for revision_id in (pinned, head.source_revision_id):
                cur.execute("""SELECT blob_hash FROM v2_artifact_revisions
                    WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                      AND artifact_id=%s""", (tenant_id, workspace_id, revision_id, artifact_id))
                row = cur.fetchone()
                hashes.append(row[0] if row else None)
            pinned_hash, head_hash = hashes
            if head_hash is not None:
                return ("artifact_changed" if pinned_hash is not None and head_hash != pinned_hash
                        else None)
            cur.execute("""SELECT 1 FROM v2_source_refreshes
                WHERE tenant_id=%s AND workspace_id=%s AND status='COMMITTED'
                  AND diff->'deleted' @> %s::jsonb LIMIT 1""",
                (tenant_id, workspace_id, json.dumps([{"artifact_id": artifact_id}])))
            return "artifact_deleted_by_refresh" if cur.fetchone() else None

    def stale_marked_jobs(self, tenant_id: str, workspace_id: str,
                          decision_id: str) -> list[str]:
        with self._tx() as cur:
            cur.execute("""SELECT job_id FROM v2_stale_markers
                WHERE tenant_id=%s AND workspace_id=%s AND decision_id=%s ORDER BY job_id""",
                (tenant_id, workspace_id, decision_id))
            return [r[0] for r in cur.fetchall()]

    def jobs_by_prefix(self, tenant_id: str, workspace_id: str, prefix: str) -> list[dict]:
        """Jobs whose id starts with `prefix` (system-derived ids), with terminal state."""
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._tx() as cur:
            cur.execute("""SELECT job_id, kind, execution_status, execution_reason, execution_result
                FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id LIKE %s
                ORDER BY job_id""", (tenant_id, workspace_id, like))
            return [{"job_id": r[0], "kind": r[1], "execution_status": r[2],
                     "execution_reason": sanitize_reason(r[3]), "execution_result": _as_object(r[4])}
                    for r in cur.fetchall()]

    def approval_jobs_pinned_to(self, tenant_id: str, workspace_id: str,
                                revision_id: str | None, limit: int = 50) -> list[str]:
        if revision_id is None:
            return []
        with self._tx() as cur:
            cur.execute("""SELECT job_id FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND kind='approval'
                  AND pinned_source_revision_id=%s ORDER BY job_id LIMIT %s""",
                (tenant_id, workspace_id, revision_id, limit))
            return [r[0] for r in cur.fetchall()]

    def refresh_event_pending(self, tenant_id: str, workspace_id: str,
                              refresh_id: str) -> bool:
        with self._tx() as cur:
            cur.execute("""SELECT count(*) FROM v2_outbox
                WHERE tenant_id=%s AND workspace_id=%s AND kind='SOURCE_PUBLISHED'
                  AND payload->>'refresh_id'=%s AND status <> 'DONE'""",
                (tenant_id, workspace_id, refresh_id))
            return cur.fetchone()[0] > 0

    def source_revision_view(self, tenant_id: str, workspace_id: str,
                             revision_id: str) -> dict:
        """Committed revision with artifact metadata and import coverage."""
        with self._tx() as cur:
            cur.execute("""SELECT parent_revision_id, manifest_hash, restored_from, trigger
                FROM v2_source_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s""",
                (tenant_id, workspace_id, revision_id))
            row = cur.fetchone()
            if row is None:
                raise TenantScopeError("source revision not found")
            cur.execute("""SELECT artifact_id, blob_hash, logical_path, media_type, byte_size,
                    source_role, source_role_origin, provenance
                FROM v2_artifact_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                ORDER BY artifact_id""", (tenant_id, workspace_id, revision_id))
            artifacts = [dict(zip(("artifact_id", "blob_hash", "logical_path", "media_type",
                                   "byte_size", "source_role", "source_role_origin",
                                   "provenance"), r)) for r in cur.fetchall()]
            cur.execute("""SELECT import_id, report, ingest_job_id FROM v2_source_imports
                WHERE tenant_id=%s AND workspace_id=%s AND committed_revision_id=%s""",
                (tenant_id, workspace_id, revision_id))
            imp = cur.fetchone()
            coverage = None
            if imp is not None:
                skipped = [{"logical_path": i["logical_path"], "reason": i["reason"]}
                           for i in _as_object(imp[1])["items"] if i["disposition"] != "admitted"]
                ingest_coverage = self._ingest_coverage(cur, tenant_id, workspace_id, imp[2],
                                                       source_revision_id=revision_id)
                # `admission_complete` is the admission-report claim
                # (no skip required); `complete`/`status` are the REAL
                # analysis coverage, read from the ingest job's own result,
                # never inferred from admission alone.
                coverage = {"admission_complete": not skipped,
                            "complete": ingest_coverage["complete"],
                            "status": ingest_coverage["status"], "skipped": skipped,
                            "index_status": ingest_coverage["index_status"],
                            "index_reason": ingest_coverage["index_reason"],
                            "semantic_stage": ingest_coverage["semantic_stage"]}
            refresh = None
            if imp is None:
                # A revision produced by a manual refresh. Provenance is
                # an external OBSERVATION (never a ChangeSet); admission of a
                # refresh never skips an item, so `skipped` is always empty.
                cur.execute("""SELECT refresh_id, created_by, committed_by, ingest_job_id
                    FROM v2_source_refreshes
                    WHERE tenant_id=%s AND workspace_id=%s AND committed_revision_id=%s""",
                    (tenant_id, workspace_id, revision_id))
                rf = cur.fetchone()
                if rf is not None:
                    refresh = {"refresh_id": rf[0],
                               "provenance": {"kind": "external_observation",
                                              "actor_id": rf[2] or rf[1]}}
                    ingest_coverage = self._ingest_coverage(
                        cur, tenant_id, workspace_id, rf[3], source_revision_id=revision_id)
                    coverage = {"admission_complete": True,
                                "complete": ingest_coverage["complete"],
                                "status": ingest_coverage["status"], "skipped": [],
                                "index_status": ingest_coverage["index_status"],
                                "index_reason": ingest_coverage["index_reason"],
                                "semantic_stage": ingest_coverage["semantic_stage"]}
        return {"revision_id": revision_id, "parent_revision_id": row[0],
                "manifest_hash": row[1], "restored_from": row[2], "trigger": row[3],
                "import_id": imp[0] if imp else None, "refresh": refresh,
                "artifacts": artifacts, "coverage": coverage}

    def read_artifact_blob(self, tenant_id: str, workspace_id: str, revision_id: str,
                           artifact_id: str) -> dict:
        """Export: return one committed artifact's exact immutable
        bytes plus its recorded metadata, scoped to `(tenant, workspace,
        revision)` the same way every other read route in this file is
        scoped -- never a caller-named path or hash. Reuses `_verified_blob`
        so a checksum mismatch on the stored
        blob raises the same `BlobIntegrityError` any other read path would.
        """
        with self._tx() as cur:
            cur.execute("""SELECT 1 FROM v2_source_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s""",
                (tenant_id, workspace_id, revision_id))
            if cur.fetchone() is None:
                raise TenantScopeError("source revision not found")
            cur.execute("""SELECT artifact_id, blob_hash, logical_path, media_type, byte_size
                FROM v2_artifact_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                    AND artifact_id=%s""",
                (tenant_id, workspace_id, revision_id, artifact_id))
            row = cur.fetchone()
            if row is None:
                raise TenantScopeError("artifact not found")
        _, blob_hash, logical_path, media_type, byte_size = row
        content = self._verified_blob(blob_hash)
        return {"artifact_id": artifact_id, "logical_path": logical_path,
                "media_type": media_type, "byte_size": byte_size,
                "blob_hash": blob_hash, "content": content}

    # ------------------------------------------------------------------ observations

    def record_source_observations(self, tenant_id: str, workspace_id: str,
                                   source_revision_id: str, artifact_id: str,
                                   observations: list[Mapping[str, object]]) -> None:
        """Persist one artifact's parsed observations for a committed,
        immutable source revision. Rows are append-only (the immutability
        trigger rejects UPDATE/DELETE). `observation_id` is keyed by
        `(artifact_id, parser_version, index)`: a re-ingest under the SAME
        parser_version replays idempotently (`ON CONFLICT DO NOTHING` on the
        same id), while a parser_version bump addresses a disjoint set of
        rows and always writes them as new, independent, immutable rows --
        it never collides with and so never discards the prior version's
        evidence. `source_observations` below then reports only the
        highest parser_version present per artifact as the active read, so a
        version bump is visible to callers without deleting the old rows.

        Compatibility: the earlier writer used
        the id `obs-{artifact_id}-{index:06d}` (no parser_version segment).
        A row already durable under that legacy id for this exact logical
        slot (same artifact + parser_version + index) is NOT a different
        observation -- it is the same one, from an earlier worker attempt,
        and the immutability trigger means it can never be edited or
        deleted. Before writing the new-format id, this checks for that
        legacy id at the SAME parser_version and skips the insert if it is
        already there, so a retry/lease-reclaim replay never produces two
        rows for one logical chunk. A legacy row can only ever exist at the
        version the earlier writer always used; any other parser_version is
        unaffected by this check and still gets its own new, distinct row
        (a real version bump is never mistaken for this compatibility case)."""
        if not observations:
            return
        with self._tx() as cur:
            for index, obs in enumerate(observations):
                parser_version = obs["parser_version"]
                legacy_id = f"obs-{artifact_id}-{index:06d}"
                cur.execute("""SELECT 1 FROM v2_source_observations
                    WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                        AND artifact_id=%s AND observation_id=%s AND parser_version=%s""",
                    (tenant_id, workspace_id, source_revision_id, artifact_id,
                     legacy_id, parser_version))
                if cur.fetchone() is not None:
                    continue
                observation_id = f"obs-{artifact_id}-{parser_version}-{index:06d}"
                cur.execute("""INSERT INTO v2_source_observations
                        (tenant_id, workspace_id, source_revision_id, artifact_id,
                         observation_id, kind, locator, raw_value, fields, parser_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (tenant_id, workspace_id, source_revision_id, artifact_id,
                                 observation_id) DO NOTHING""",
                    (tenant_id, workspace_id, source_revision_id, artifact_id, observation_id,
                     obs["kind"], Json(obs["locator"]), Json(obs.get("raw_value")),
                     Json(dict(obs.get("fields") or {})), parser_version))

    def _all_observation_rows(self, tenant_id: str, workspace_id: str,
                              source_revision_id: str) -> list[dict]:
        """Every durable, immutable observation row for one revision, ALL
        parser versions, unfiltered. Internal helper shared by
        `source_observations` (active-only) and `all_source_observations`
        (every version, for a caller that pins its own
        `parser_version`/`observation_id` and must resolve against exactly
        that historical row, not whichever version is currently "active")."""
        with self._tx() as cur:
            cur.execute("""SELECT observation_id, artifact_id, kind, locator, raw_value,
                    fields, parser_version
                FROM v2_source_observations
                WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                ORDER BY artifact_id, observation_id""",
                (tenant_id, workspace_id, source_revision_id))
            rows = cur.fetchall()
        # psycopg2 decodes JSONB columns to native Python values automatically
        # (str/int/float/bool/None/dict/list), unlike the other JSONB columns
        # in this module that are always objects. `raw_value` can legitimately
        # BE a plain string (e.g. a markdown chunk's text); `_as_object`'s
        # "if it's a str, json.loads it again" heuristic would corrupt that,
        # so these three columns are used as psycopg2 already decoded them.
        return [{"observation_id": r[0], "artifact_id": r[1], "kind": r[2],
                 "locator": r[3], "raw_value": r[4],
                 "fields": r[5], "parser_version": r[6]} for r in rows]

    def source_observations(self, tenant_id: str, workspace_id: str,
                            source_revision_id: str) -> list[dict]:
        """The ACTIVE observations of one committed source revision, scoped
        by (tenant, workspace): for each artifact, only the rows written
        under that artifact's highest recorded `parser_version` are
        returned. Rows from an earlier parser_version remain durable and
        immutable in the table but are not part of this "current"
        read; a caller that needs every historical version can call
        `all_source_observations` instead."""
        def _version_key(v: str) -> tuple:
            try:
                return tuple(int(p) for p in v.split("."))
            except ValueError:
                return (v,)

        rows = self._all_observation_rows(tenant_id, workspace_id, source_revision_id)
        latest_version: dict[str, str] = {}
        for r in rows:
            current = latest_version.get(r["artifact_id"])
            if current is None or _version_key(r["parser_version"]) > _version_key(current):
                latest_version[r["artifact_id"]] = r["parser_version"]
        return [r for r in rows if r["parser_version"] == latest_version.get(r["artifact_id"])]

    def all_source_observations(self, tenant_id: str, workspace_id: str,
                                source_revision_id: str) -> list[dict]:
        """Every durable observation row for one revision, across
        ALL parser versions -- not just the "active" (highest-version) rows
        `source_observations` returns. A caller resolving an already-pinned
        `EvidenceRef` (which carries its OWN exact `parser_version`) needs
        this: an immutable row written under an OLDER parser_version must
        still resolve by its own pinned identity even after a newer version
        has been parsed for the same artifact/revision, exactly as any other
        immutable evidence remains readable after a later, unrelated write."""
        return self._all_observation_rows(tenant_id, workspace_id, source_revision_id)

    def list_artifact_observations(self, tenant_id: str, workspace_id: str,
                                   revision_id: str, artifact_id: str) -> list[dict]:
        """The discovery step a source
        inspection UI needs before an evidence click is possible at all --
        given a revision + artifact a caller already has ACL-scoped access
        to, list that artifact's own ACTIVE observations (id/kind/locator
        only, no raw_value; a caller who wants the value clicks through to
        `read_artifact_blob`'s sibling `read_evidence` next). Raises
        `TenantScopeError` if the revision itself is not found in this
        tenant/workspace, matching every other read method in this class."""
        with self._tx() as cur:
            cur.execute("""SELECT 1 FROM v2_source_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s""",
                (tenant_id, workspace_id, revision_id))
            if cur.fetchone() is None:
                raise TenantScopeError("source revision not found")
        rows = self.source_observations(tenant_id, workspace_id, revision_id)
        return [{"observation_id": r["observation_id"], "kind": r["kind"], "locator": r["locator"]}
                for r in rows if r["artifact_id"] == artifact_id]

    # ------------------------------------------------------------------ evidence validation

    def validate_evidence(self, tenant_id: str, workspace_id: str,
                          revision_id: str, refs: list[Mapping[str, object]]) -> list[dict]:
        """Validate a list of caller-submitted `EvidenceRef`s against one
        committed, tenant/workspace-scoped source revision's real artifacts
        and observations. Read-only: never mutates a source/observation row
        or a claim. Raises `TenantScopeError` if the revision itself is not
        found in this tenant/workspace, before any per-ref check runs."""
        from core.change_intelligence.v2.evidence_validator import validate_refs

        view = self.source_revision_view(tenant_id, workspace_id, revision_id)
        artifacts_by_id = {a["artifact_id"]: a for a in view["artifacts"]}
        # Every ref is REQUIRED to pin its own exact
        # `parser_version`, so resolution must match against that historical
        # row directly -- ALL versions, not just the currently "active" one
        # `source_observations` returns, or a still-valid old-version ref
        # would wrongly report `locator_not_found` after a newer version is
        # parsed for the same artifact/revision.
        observations = self.all_source_observations(tenant_id, workspace_id, revision_id)
        reports = validate_refs(refs, revision_id=revision_id,
                                artifacts_by_id=artifacts_by_id, observations=observations)
        return [r.to_dict() for r in reports]

    def read_evidence(self, tenant_id: str, workspace_id: str,
                      revision_id: str, observation_id: str) -> dict:
        """The direct "evidence read/view" path: given an
        `observation_id` a caller already holds (e.g. from a prior
        `validate_evidence` report or an evidence list in a UI), return its
        full pinned evidence -- source revision, artifact id + current blob
        hash, typed locator, parser version and the exact raw value/quote --
        without requiring the caller to resubmit the whole ref. Read-only,
        ACL-scoped exactly like `validate_evidence`/`source_revision_view`;
        raises `TenantScopeError` for an unknown revision OR an
        observation_id absent from it (never leaks whether it exists in a
        DIFFERENT tenant/workspace/revision)."""
        view = self.source_revision_view(tenant_id, workspace_id, revision_id)
        artifacts_by_id = {a["artifact_id"]: a for a in view["artifacts"]}
        # An `observation_id` is immutable and durable across a
        # later parser-version bump; a caller re-fetching evidence
        # by an OLD id it already holds must still find it, so this reads
        # every version, not only the currently active one.
        observations = self.all_source_observations(tenant_id, workspace_id, revision_id)
        match = next((o for o in observations if o["observation_id"] == observation_id), None)
        if match is None:
            raise TenantScopeError("evidence not found")
        artifact = artifacts_by_id.get(match["artifact_id"], {})
        return {"source_revision_id": revision_id, "artifact_id": match["artifact_id"],
                "blob_hash": artifact.get("blob_hash"), "locator": match["locator"],
                "parser_version": match["parser_version"], "raw_value": match["raw_value"],
                "observation_id": observation_id}

    # ------------------------------------------------------------------ retrieval index

    def build_retrieval_index(self, tenant_id: str, workspace_id: str,
                              source_revision_id: str, *,
                              source_coverage_complete: bool | None = None,
                              source_coverage_reason: str | None = None) -> dict:
        """Rebuild the retrieval projection for one committed revision from
        its durable, immutable ACTIVE observations (T01's `source_observations`
        -- the "current" read). Idempotent and deterministic: since every
        input row is immutable and the transformation is pure, running this
        twice (a retry, a lease-reclaim, two workers racing on the SAME
        revision) always produces byte-identical rows -- there is no old-vs-
        new ordering to arbitrate, so a "late" duplicate build is harmless.
        DELETE-then-INSERT-then-mark-READY happens in ONE transaction: a
        crash or exception anywhere in this method rolls back everything,
        leaving any PRIOR successful build (a different revision, or an
        earlier attempt at this one that never reached commit) completely
        untouched -- never a partially-written index marked ready."""
        from core.change_intelligence.v2.retrieval import term_vector

        observations = self.source_observations(tenant_id, workspace_id, source_revision_id)
        by_artifact: dict[str, list] = {}
        for o in observations:
            by_artifact.setdefault(o["artifact_id"], []).append(o)

        rows = []
        for artifact_id, obs_list in by_artifact.items():
            for i, o in enumerate(obs_list):
                raw = o["raw_value"]
                text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
                rows.append({
                    "artifact_id": artifact_id, "observation_id": o["observation_id"],
                    "locator": o["locator"], "parser_version": o["parser_version"],
                    "raw_value": raw, "term_vector": term_vector(text),
                    "prev_observation_id": obs_list[i - 1]["observation_id"] if i > 0 else None,
                    "next_observation_id": (obs_list[i + 1]["observation_id"]
                                            if i + 1 < len(obs_list) else None),
                })

        with self._tx() as cur:
            # Recovery callers have only a revision ID. Once its ingest job
            # has finished, its durable result is authoritative over caller
            # hints; rebuilding must never promote partial coverage to true.
            # The running worker supplies its freshly computed result before
            # that result can be committed on the job row.
            cur.execute("""SELECT execution_status, execution_result FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s
                  AND pinned_source_revision_id=%s AND kind='source_ingest'
                FOR UPDATE""", (tenant_id, workspace_id, source_revision_id))
            job_row = cur.fetchone()
            if job_row is not None and job_row[0] == "COMPLETED":
                job_result = _as_object(job_row[1]) or {}
                source_coverage_complete = job_result.get("coverage_complete") is True
                source_coverage_reason = job_result.get("coverage_reason")
                if not source_coverage_reason:
                    source_coverage_reason = ("all admitted artifacts parsed with no admission skips"
                                              if source_coverage_complete else
                                              "ingest job has no complete source coverage")
            elif source_coverage_complete is not True:
                source_coverage_complete = False
                source_coverage_reason = (source_coverage_reason or
                                          "source coverage not yet confirmed by ingest job")
            cur.execute("""DELETE FROM v2_retrieval_index
                WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s""",
                (tenant_id, workspace_id, source_revision_id))
            for r in rows:
                cur.execute("""INSERT INTO v2_retrieval_index
                        (tenant_id, workspace_id, source_revision_id, artifact_id, observation_id,
                         locator, parser_version, raw_value, term_vector,
                         prev_observation_id, next_observation_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, source_revision_id, r["artifact_id"],
                     r["observation_id"], Json(r["locator"]), r["parser_version"],
                     Json(r["raw_value"]), Json(r["term_vector"]),
                     r["prev_observation_id"], r["next_observation_id"]))
            cur.execute("""INSERT INTO v2_index_builds
                    (tenant_id, workspace_id, source_revision_id, status, observation_count,
                     source_coverage_complete, source_coverage_reason)
                VALUES (%s,%s,%s,'READY',%s,%s,%s)
                ON CONFLICT (tenant_id, workspace_id, source_revision_id)
                DO UPDATE SET status='READY', observation_count=EXCLUDED.observation_count,
                              source_coverage_complete=EXCLUDED.source_coverage_complete,
                              source_coverage_reason=EXCLUDED.source_coverage_reason,
                              built_at=now()""",
                (tenant_id, workspace_id, source_revision_id, len(rows),
                 source_coverage_complete, source_coverage_reason))
        return {"source_revision_id": source_revision_id, "status": "READY",
                "observation_count": len(rows),
                "source_coverage_complete": source_coverage_complete,
                "source_coverage_reason": source_coverage_reason}

    def retrieval_status(self, tenant_id: str, workspace_id: str) -> dict:
        """The CURRENT index's readiness: current is always whatever revision
        the workspace head actually points at right now, never whichever
        revision last finished building -- so an old, slow build finishing
        after a newer revision is already current can never make a reader
        see stale data as current (R3)."""
        head = self.head(tenant_id, workspace_id)
        if head.source_revision_id is None:
            return {"source_revision_id": None, "status": "NONE",
                    "source_coverage_complete": None, "source_coverage_reason": None}
        with self._tx() as cur:
            cur.execute("""SELECT status, observation_count, source_coverage_complete,
                    source_coverage_reason
                FROM v2_index_builds
                WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s""",
                (tenant_id, workspace_id, head.source_revision_id))
            row = cur.fetchone()
        if row is None:
            return {"source_revision_id": head.source_revision_id, "status": "REBUILDING",
                    "source_coverage_complete": None, "source_coverage_reason": None}
        return {"source_revision_id": head.source_revision_id, "status": row[0],
                "observation_count": row[1], "source_coverage_complete": row[2],
                "source_coverage_reason": row[3]}

    def retrieve(self, tenant_id: str, workspace_id: str, query_text: str,
                *, top_k: int = 10) -> dict:
        """Scoped retrieval: read-only, resolves the CURRENT revision via
        `retrieval_status` (never an explicit caller-named revision -- this
        is "what does the workspace currently know", not a historical
        evidence lookup like T02's). A not-`READY` index returns its status
        and empty results rather than ever falling back to stale/wrong-
        revision data."""
        from core.change_intelligence.v2.retrieval import rank

        status = self.retrieval_status(tenant_id, workspace_id)
        if status["status"] != "READY":
            return {**status, "results": [],
                    "coverage": {"total_candidates": 0, "ranked_returned": 0, "returned": 0,
                                "structural_added": 0, "omitted_count": 0, "truncated": False},
                    "embedding_version": "termfreq-v1"}
        revision_id = status["source_revision_id"]
        with self._tx() as cur:
            cur.execute("""SELECT artifact_id, observation_id, locator, parser_version, raw_value,
                    term_vector, prev_observation_id, next_observation_id
                FROM v2_retrieval_index
                WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s""",
                (tenant_id, workspace_id, revision_id))
            db_rows = cur.fetchall()
        view = self.source_revision_view(tenant_id, workspace_id, revision_id)
        blob_hash_by_artifact = {a["artifact_id"]: a["blob_hash"] for a in view["artifacts"]}
        index_rows = [{"artifact_id": r[0], "observation_id": r[1], "locator": r[2],
                       "parser_version": r[3], "raw_value": r[4], "term_vector": r[5],
                       "prev_observation_id": r[6], "next_observation_id": r[7],
                       "blob_hash": blob_hash_by_artifact.get(r[0])} for r in db_rows]
        bundle = rank(index_rows, query_text, top_k=top_k)
        return {**status, **bundle}

    def publish_knowledge(self, tenant_id: str, workspace_id: str,
                          revision_id: str, source_revision_id: str,
                          decision_epoch: int, *, expected_version: int,
                          idempotency_key: str,
                          actor_id: str | None = None) -> PublishResult:
        # `actor_id` is optional/keyword-only and defaults to `None`
        # so the store-primitive callers (positional args only,
        # no `actor_id`) keep behaving exactly as before -- this is a
        # backward-compatible addition, not a signature break. When the new
        # `POST /v2/knowledge/accept` route passes a real human actor id, it
        # rides inside the SAME transaction/CAS guard as the publish itself
        # (folded into the `KNOWLEDGE_PUBLISHED` audit/outbox payload below),
        # matching the requirement that the acceptance attestation, KM head and
        # audit/outbox stay consistent under CAS, without widening
        # `_record_commit`'s shared, hardcoded 'service' audit actor column
        # (which every other publish_* caller also relies on).
        operation = "knowledge.publish"
        payload = {"revision_id": revision_id,
                   "source_revision_id": source_revision_id,
                   "decision_epoch": decision_epoch,
                   "expected_version": expected_version}
        payload_hash = canonical_hash(payload)
        with self._tx() as cur:
            head = self._lock_head(cur, tenant_id, workspace_id)
            prior = self._replay(cur, tenant_id, workspace_id, operation,
                                 idempotency_key, payload_hash)
            if prior is not None:
                return prior
            if (head.version != expected_version
                    or head.source_revision_id != source_revision_id
                    or head.decision_epoch != decision_epoch):
                raise StaleVersion("stale source, decision epoch or workspace version")
            try:
                cur.execute("""INSERT INTO v2_knowledge_revisions
                    (tenant_id, workspace_id, revision_id, source_revision_id,
                     decision_epoch) VALUES (%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, revision_id, source_revision_id,
                     decision_epoch))
            except psycopg2.IntegrityError as exc:
                if getattr(exc, "pgcode", None) != "23505":
                    raise
                # A published knowledge revision is immutable and can
                # never be re-elected as current (e.g. an OLD KM id offered
                # after a source restore): a named conflict, not a 500.
                raise StoreConflict(
                    "knowledge_revision_exists: an existing knowledge revision "
                    "cannot be re-published as current") from exc
            version = head.version + 1
            cur.execute("""UPDATE v2_workspaces SET version=%s,
                knowledge_revision_id=%s WHERE tenant_id=%s AND workspace_id=%s""",
                (version, revision_id, tenant_id, workspace_id))
            result = PublishResult(version, revision_id)
            self._record_commit(cur, tenant_id, workspace_id, operation,
                                idempotency_key, payload_hash, result,
                                "KNOWLEDGE_PUBLISHED", {"knowledge_revision_id": revision_id,
                                                        "source_revision_id": source_revision_id,
                                                        "decision_epoch": decision_epoch,
                                                        "accepted_by": actor_id})
            return result

    def record_decision(self, tenant_id: str, workspace_id: str,
                        decision_id: str, actor_id: str,
                        payload: Mapping[str, object], *, expected_version: int,
                        idempotency_key: str) -> PublishResult:
        if not all(isinstance(x, str) and x.strip()
                   for x in (decision_id, actor_id, idempotency_key)):
            raise ValueError("decision, actor and idempotency key required")
        if not isinstance(payload, Mapping) or not payload:
            raise ValueError("decision payload required")
        operation = "decision.record"
        request_hash = canonical_hash({"decision_id": decision_id,
                                       "actor_id": actor_id, "payload": dict(payload),
                                       "expected_version": expected_version})
        with self._tx() as cur:
            head = self._lock_head(cur, tenant_id, workspace_id)
            prior = self._replay(cur, tenant_id, workspace_id, operation,
                                 idempotency_key, request_hash)
            if prior is not None:
                return prior
            if head.version != expected_version:
                raise StaleVersion("stale_revision")
            epoch = head.decision_epoch + 1
            cur.execute("""INSERT INTO v2_decisions
                (tenant_id, workspace_id, decision_id, epoch, actor_id, payload)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (tenant_id, workspace_id, decision_id, epoch, actor_id,
                 Json(dict(payload))))
            version = head.version + 1
            cur.execute("""UPDATE v2_workspaces SET version=%s,
                decision_epoch=%s, knowledge_revision_id=NULL
                WHERE tenant_id=%s AND workspace_id=%s""",
                (version, epoch, tenant_id, workspace_id))
            result = PublishResult(version, decision_id)
            self._record_commit(cur, tenant_id, workspace_id, operation,
                                idempotency_key, request_hash, result,
                                "DECISION_RECORDED", {"decision_id": decision_id,
                                                      "epoch": epoch,
                                                      "version": version})
            return result

    def _transition_conflict_finding(self, cur, tenant_id: str, workspace_id: str,
                                     conflict_finding_id: str, *, actor_id: str,
                                     actor_kind: str, actor_permissions: frozenset[str],
                                     revision: str, policy_version: str,
                                     decision_epoch: int) -> str:
        """Drive the ALREADY-REVIEWED
        `core/change_intelligence/v2/lifecycle.py` `conflict` machine's
        `DECISION_RECORDED`/`REMEDIATION_PENDING` edges as a real, durable
        side effect of `/v2/conflict/resolve`, inside the SAME transaction
        as the decision-ledger insert -- never a second, unguarded write.

        Worked example: after a human picks an authority (e.g.
        SOP over code), the CONFLICT finding's `lifecycle_state` must become
        `REMEDIATION_PENDING`. This is modeled as two edges, not one, because
        the lifecycle table only grants `conflict.resolve` (a human
        permission) for `OPEN -> DECISION_RECORDED`; `DECISION_RECORDED ->
        REMEDIATION_PENDING` is declared a service-owned edge
        (`_SERVICE_TARGETS`) -- the system's own immediate follow-on once a
        decision is durably recorded, not a second human action. Both edges
        are authorized here, in order, using the SAME already-reviewed guard
        -- no parallel authority source.

        A "finding" job's persisted findings live inside that job's own
        `execution_result` JSONB (there is no dedicated findings
        table) -- this locks every COMPLETED "finding" job row in the
        workspace, finds the one containing `conflict_finding_id`, and
        rewrites just that one finding's `lifecycle_state` in place before
        writing the whole `execution_result` back. Raises `TenantScopeError`
        if no such finding exists (never silently no-ops)."""
        cur.execute("""SELECT job_id, execution_result FROM v2_jobs
            WHERE tenant_id=%s AND workspace_id=%s AND kind='finding'
              AND execution_status='COMPLETED' AND execution_result IS NOT NULL
            FOR UPDATE""", (tenant_id, workspace_id))
        rows = cur.fetchall()
        target_job_id = None
        target_result = None
        target_finding = None
        for job_id, raw_result in rows:
            result = _as_object(raw_result) or {}
            for finding in (result.get("findings") or []):
                if isinstance(finding, dict) and finding.get("finding_id") == conflict_finding_id:
                    target_job_id, target_result, target_finding = job_id, result, finding
                    break
            if target_finding is not None:
                break
        if target_finding is None:
            raise TenantScopeError("conflict finding not found")
        current_state = target_finding.get("lifecycle_state") or "OPEN"
        base_ctx = dict(
            expected_revision=revision, current_revision=revision,
            policy_version=policy_version, current_policy_version=policy_version,
            decision_epoch=decision_epoch, current_decision_epoch=decision_epoch)
        authorize_transition("conflict", current_state, "DECISION_RECORDED",
            TransitionContext(actor_id=actor_id, actor_kind=actor_kind,
                permissions=actor_permissions, current_permissions=actor_permissions,
                **base_ctx))
        authorize_transition("conflict", "DECISION_RECORDED", "REMEDIATION_PENDING",
            TransitionContext(actor_id=actor_id, actor_kind="service",
                permissions=frozenset(), current_permissions=frozenset(),
                **base_ctx))
        target_finding["lifecycle_state"] = "REMEDIATION_PENDING"
        cur.execute("""UPDATE v2_jobs SET execution_result=%s
            WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
            (Json(target_result), tenant_id, workspace_id, target_job_id))
        return "REMEDIATION_PENDING"

    def record_human_decision(self, tenant_id: str, workspace_id: str, *,
                              actor_id: str, current_permission: str,
                              target: str, action: str,
                              expected_version: int, idempotency_key: str,
                              rationale: str | None = None,
                              evidence_ref: str | None = None,
                              scope: Mapping[str, object] | None = None,
                              effective_from: str | None = None,
                              effective_until: str | None = None,
                              supersedes: str | None = None,
                              revokes: str | None = None,
                              conflict_finding_id: str | None = None,
                              actor_kind: str = "human",
                              actor_permissions: frozenset[str] = frozenset(),
                              schedule_invalidation_target: str | None = None) -> dict:
        """The fuller append-only HumanDecision ledger write.

        Extends (does not replace) `record_decision`'s already-reviewed
        transaction/audit/idempotency machinery -- same `_tx`/`_lock_head`/
        `_replay`/`_record_commit` calls, same single epoch-increment owner
        (`decision_epoch = head.decision_epoch + 1`, the ONLY site that bumps
        it; no other path may add a second).

        Provenance: a decision with no `evidence_ref` is persisted with
        `provenance="human_assertion"` -- an explicit marker that this is a
        human's assertion, never promoted to look like a sourced quote. One
        with an `evidence_ref` gets `provenance="evidence_ref"`.

        Conflict semantics (two contradictory decisions must surface as a
        conflict, never a silent last-write-wins): for a
        confirm/resolve/accepted_unknown action, if an existing decision on
        the SAME `target` is still active (no `superseded_by`/`revoked_by`)
        and was authored by a DIFFERENT actor, this raises `StoreConflict`
        instead of inserting a second, silently-competing row -- the two
        conflicting human decisions are surfaced as a real conflict response
        (409) for a human to resolve via an explicit supersede/revoke, not
        auto-picked. `supersede`/`revoke` instead require the referenced
        decision to exist and set its `superseded_by`/`revoked_by` back-link
        -- the ORIGINAL row's own columns are never edited, only this
        additional nullable back-link, so history stays append-only and
        provable (`test_supersede_and_revoke_preserve_append_only_history`
        reads the original row directly).

        `schedule_invalidation_target`, when given
        (confirm/resolve callers only), creates the real `invalidation` job
        for this decision INSIDE this SAME transaction (via
        `_create_job_core`, reusing this method's already-locked `head`/
        `cur`) instead of the caller making a separate `store.create_job(...)`
        call after this transaction has already committed. A failure
        creating that job rolls back the whole decision -- so a reanalysis
        can never be silently lost after the decision committed."""
        if not all(isinstance(x, str) and x.strip() for x in
                  (tenant_id, workspace_id, actor_id, current_permission,
                   target, action, idempotency_key)):
            raise ValueError("actor, permission, target, action and "
                            "idempotency key required")
        if action not in ("confirm", "resolve", "supersede", "revoke",
                          "accepted_unknown"):
            raise ValueError(f"unsupported decision action: {action!r}")
        provenance = "human_assertion" if not evidence_ref else "evidence_ref"
        decision_id = f"{action}-{target}-{actor_id}-{uuid.uuid4().hex[:12]}"
        scope_obj = dict(scope) if scope is not None else None
        payload = {"target": target, "action": action, "rationale": rationale,
                  "evidence_ref": evidence_ref, "provenance": provenance,
                  "scope": scope_obj, "effective_from": effective_from,
                  "effective_until": effective_until, "supersedes": supersedes,
                  "revokes": revokes}
        operation = "decision.record.v2"
        request_hash = canonical_hash({"decision_id_prefix": f"{action}-{target}-{actor_id}",
                                       "actor_id": actor_id, "payload": payload,
                                       "expected_version": expected_version})
        with self._tx() as cur:
            head = self._lock_head(cur, tenant_id, workspace_id)
            prior = self._replay(cur, tenant_id, workspace_id, operation,
                                 idempotency_key, request_hash)
            if prior is not None:
                return {"decision_id": prior.revision_id, "provenance": provenance,
                       "version": prior.version, "action": action,
                       "decision_epoch": head.decision_epoch}
            if head.version != expected_version:
                raise StaleVersion("stale_revision")
            if action in ("supersede", "revoke"):
                referenced = supersedes if action == "supersede" else revokes
                if not referenced:
                    raise ValueError(f"{action} requires the prior decision_id")
                cur.execute("""SELECT decision_id FROM v2_decisions
                    WHERE tenant_id=%s AND workspace_id=%s AND decision_id=%s""",
                    (tenant_id, workspace_id, referenced))
                if cur.fetchone() is None:
                    raise TenantScopeError("referenced decision not found")
            else:
                # `v2_decisions` is append-only at the DATABASE level (a
                # trigger rejects any UPDATE/DELETE) -- "active" cannot be a
                # mutated flag on the original row. Instead, a decision is
                # inactive when some LATER decision's `supersedes`/`revokes`
                # names it; this correlated NOT EXISTS derives the same
                # "still active" answer purely by insertion order, never by
                # editing history.
                cur.execute("""SELECT d.decision_id, d.actor_id FROM v2_decisions AS d
                    WHERE d.tenant_id=%s AND d.workspace_id=%s AND d.target=%s
                      AND NOT EXISTS (
                        SELECT 1 FROM v2_decisions AS s
                        WHERE s.tenant_id=d.tenant_id AND s.workspace_id=d.workspace_id
                          AND (s.supersedes=d.decision_id OR s.revokes=d.decision_id))""",
                    (tenant_id, workspace_id, target))
                for existing_decision_id, existing_actor in cur.fetchall():
                    if existing_actor != actor_id:
                        raise StoreConflict(
                            "decision_conflict: target already carries an "
                            f"active decision ({existing_decision_id}) by a "
                            "different actor; authority resolution required, "
                            "no auto-pick winner")
            if action in ("resolve", "accepted_unknown"):
                # The evidence state this human decision is taken ON
                # (server-derived, never client-supplied), added to the payload AFTER
                # the idempotency hash was computed.
                binding = self._decision_binding(cur, tenant_id, workspace_id, target, head)
                if binding is not None:
                    payload["evidence_binding"] = binding
            epoch = head.decision_epoch + 1
            cur.execute("""INSERT INTO v2_decisions
                (tenant_id, workspace_id, decision_id, epoch, actor_id, payload,
                 target, action, rationale, evidence_ref, provenance, scope,
                 effective_from, effective_until, supersedes, revokes,
                 current_permission)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, workspace_id, decision_id, epoch, actor_id,
                 Json(payload), target, action, rationale, evidence_ref,
                 provenance, Json(scope_obj) if scope_obj is not None else None,
                 effective_from, effective_until, supersedes, revokes,
                 current_permission))
            version = head.version + 1
            cur.execute("""UPDATE v2_workspaces SET version=%s,
                decision_epoch=%s, knowledge_revision_id=NULL
                WHERE tenant_id=%s AND workspace_id=%s""",
                (version, epoch, tenant_id, workspace_id))
            if action == "resolve" and conflict_finding_id:
                self._transition_conflict_finding(
                    cur, tenant_id, workspace_id, conflict_finding_id,
                    actor_id=actor_id, actor_kind=actor_kind,
                    actor_permissions=actor_permissions,
                    revision=str(version), policy_version=str(head.policy_version),
                    decision_epoch=epoch)
            result = PublishResult(version, decision_id)
            self._record_commit(cur, tenant_id, workspace_id, operation,
                                idempotency_key, request_hash, result,
                                "DECISION_RECORDED",
                                {"decision_id": decision_id, "epoch": epoch,
                                 "version": version, "action": action,
                                 "target": target, "provenance": provenance})
            if schedule_invalidation_target is not None:
                # Same transaction, same cur, same already-held FOR UPDATE
                # lock on the workspace row -- a failure here raises out of
                # this `with self._tx()` block and rolls back the decision
                # insert/epoch bump/audit event above too (see docstring).
                post_head = _dc_replace(head, version=version, decision_epoch=epoch,
                                        knowledge_revision_id=None)
                invalidation_job_id = f"invalidation-{decision_id}"
                self._create_job_core(
                    cur, tenant_id, workspace_id, invalidation_job_id, "invalidation",
                    {"decision_id": decision_id, "target": schedule_invalidation_target},
                    head=post_head, expected_version=version,
                    idempotency_key=f"job-create-{invalidation_job_id}")
            # The real, committed epoch this decision landed at,
            # read back from the SAME row/transaction that bumped it (never
            # recomputed) -- lets a caller (route response, invalidation
            # scheduler) pin the exact epoch a dependent's reanalysis must be
            # judged against.
            return {"decision_id": decision_id, "provenance": provenance,
                   "version": version, "action": action, "decision_epoch": epoch}

    def read_blob(self, tenant_id: str, workspace_id: str,
                  source_revision_id: str, artifact_id: str) -> bytes:
        with self._tx() as cur:
            cur.execute("""SELECT blob_hash FROM v2_artifact_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s
                  AND artifact_id=%s""",
                (tenant_id, workspace_id, source_revision_id, artifact_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("artifact not found")
        return self._verified_blob(row[0])

    def pending_outbox(self, tenant_id: str, workspace_id: str) -> int:
        with self._tx() as cur:
            cur.execute("""SELECT count(*) FROM v2_outbox
                WHERE tenant_id=%s AND workspace_id=%s
                  AND status IN ('PENDING','CLAIMED')""",
                (tenant_id, workspace_id))
            return cur.fetchone()[0]

    def outbox_status(self, tenant_id: str, workspace_id: str, event_id: int) -> str:
        with self._tx() as cur:
            cur.execute("""SELECT status FROM v2_outbox WHERE tenant_id=%s
                AND workspace_id=%s AND id=%s""",
                (tenant_id, workspace_id, event_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("outbox event not found")
        return row[0]

    def reap_exhausted_outbox(self, tenant_id: str, workspace_id: str) -> int:
        """Escalate exhausted, expired work into a durable auditable failure."""
        with self._tx() as cur:
            self._lock_head(cur, tenant_id, workspace_id)
            cur.execute("""UPDATE v2_outbox SET status='FAILED',
                lease_owner=NULL, lease_until=NULL
                WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'
                  AND lease_until < now() AND attempts >= 5
                RETURNING id, kind""", (tenant_id, workspace_id))
            failed = cur.fetchall()
            for event_id, kind in failed:
                cur.execute("""INSERT INTO v2_audit
                    (tenant_id, workspace_id, event_type, actor_id, payload)
                    VALUES (%s,%s,'OUTBOX_FAILED','service',%s)""",
                    (tenant_id, workspace_id,
                     Json({"outbox_id": event_id, "kind": kind})))
            return len(failed)

    def hold_blob(self, tenant_id: str, workspace_id: str, digest: str,
                  hold_kind: str, hold_id: str) -> None:
        self.head(tenant_id, workspace_id)
        with self._tx() as cur:
            cur.execute("""SELECT 1 FROM v2_blob_refs WHERE tenant_id=%s
                AND workspace_id=%s AND blob_hash=%s""",
                (tenant_id, workspace_id, digest))
            if cur.fetchone() is None:
                raise TenantScopeError("blob not found")
            cur.execute("""INSERT INTO v2_blob_holds
                (tenant_id, workspace_id, blob_hash, hold_kind, hold_id)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (tenant_id, workspace_id, digest, hold_kind, hold_id))
            if cur.rowcount == 1:
                cur.execute("""INSERT INTO v2_audit
                    (tenant_id, workspace_id, event_type, actor_id, payload)
                    VALUES (%s,%s,'BLOB_HELD','service',%s)""",
                    (tenant_id, workspace_id,
                     Json({"blob_hash": digest, "hold_kind": hold_kind,
                           "hold_id": hold_id})))

    def blob_is_retained(self, tenant_id: str, workspace_id: str, digest: str) -> bool:
        self.head(tenant_id, workspace_id)
        with self._tx() as cur:
            cur.execute("""SELECT EXISTS(SELECT 1 FROM v2_artifact_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND blob_hash=%s)
                OR EXISTS(SELECT 1 FROM v2_blob_holds
                WHERE tenant_id=%s AND workspace_id=%s AND blob_hash=%s)""",
                (tenant_id, workspace_id, digest,
                 tenant_id, workspace_id, digest))
            return bool(cur.fetchone()[0])

    def audit_events(self, tenant_id: str, workspace_id: str) -> list[dict]:
        self.head(tenant_id, workspace_id)
        with self._tx() as cur:
            cur.execute("""SELECT id, event_type, actor_id, payload
                FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s
                ORDER BY id""", (tenant_id, workspace_id))
            return [{"id": row[0], "event_type": row[1], "actor_id": row[2],
                     "payload": _as_object(row[3])} for row in cur.fetchall()]

    def create_job(self, tenant_id: str, workspace_id: str, job_id: str,
                   kind: str, payload: Mapping[str, object], *,
                   expected_version: int, idempotency_key: str,
                   max_concurrent_jobs: int | None = None,
                   budget_reservation: tuple[object, object] | None = None,
                   requested_by: str | None = None,
                   locked_check: Callable[[object, WorkspaceHead], None] | None = None) -> PublishResult:
        """When `max_concurrent_jobs` is given (guarding against a
        "count-then-create race"), the active-job count is
        read and compared INSIDE this same transaction, after `_lock_head`
        has already taken `FOR UPDATE` on the workspace row. Every
        `create_job` call for the same `(tenant_id, workspace_id)` serializes
        on that row lock (the same mechanism `recheck_and_publish_job` uses
        for its own atomicity), so two concurrent requests can no longer both
        observe a count below the ceiling and both insert -- the second
        request's count read blocks until the first's transaction commits (or
        rolls back) and therefore sees the first request's own inserted row.

        `budget_reservation`,
        when given, is `(estimate_usd, total_cap_usd)` (both `Decimal`). The
        project's already-recorded spend (`v2_job_cost_ledger`) is summed and
        compared against `total_cap_usd` INSIDE this same transaction, under
        the SAME workspace-row lock `max_concurrent_jobs` already uses --
        never a separate, earlier transaction (a "reservation is a
        separate transaction from admission" gap would let two requests pass).
        Two concurrent submissions when only enough budget remains for one
        therefore serialize on the same lock and cannot both pass.

        (a) the total cap is PROJECT-scoped (budget contract
        §5/§9): the reservation sum covers every workspace of this
        workspace's project (`PROJECT_SPEND_SQL`), and concurrent creates in
        DIFFERENT workspaces of the same project serialize on a
        transaction-scoped advisory lock keyed by (tenant, project) -- the
        workspace row lock alone never serialized them. (b) `requested_by`,
        when given, attributes the requester and moves the approval axis to
        `PENDING_APPROVAL` in THIS transaction, so the route can no longer
        leave a committed QUEUED job + outbox event behind when the separate
        `set_job_requester` step fails. A replay of an already-created job
        with `requested_by` raises the same `TenantScopeError` the separate
        `set_job_requester` call raised before (unchanged route behaviour).
        """
        if not all(isinstance(x, str) and x.strip()
                   for x in (job_id, kind, idempotency_key)):
            raise ValueError("job ID, kind and idempotency key required")
        if not isinstance(payload, Mapping):
            raise ValueError("job payload must be a mapping")
        with self._tx() as cur:
            head = self._lock_head(cur, tenant_id, workspace_id)
            if locked_check is not None:
                locked_check(cur, head)       # e.g. the draft this job builds on was not revised: same lock, same transaction as the insert
            return self._create_job_core(
                cur, tenant_id, workspace_id, job_id, kind, payload, head=head,
                expected_version=expected_version, idempotency_key=idempotency_key,
                max_concurrent_jobs=max_concurrent_jobs,
                budget_reservation=budget_reservation, requested_by=requested_by)

    def _create_job_core(self, cur, tenant_id: str, workspace_id: str, job_id: str,
                         kind: str, payload: Mapping[str, object], *,
                         head: WorkspaceHead, expected_version: int,
                         idempotency_key: str,
                         max_concurrent_jobs: int | None = None,
                         budget_reservation: tuple[object, object] | None = None,
                         requested_by: str | None = None) -> PublishResult:
        """The actual INSERT/idempotency/quota body `create_job` used to run
        inside its OWN `_tx()`. Factored out so
        `record_human_decision` can call it with the CALLER'S already-open
        `cur`/already-locked `head` -- folding an invalidation job's creation
        into the SAME database transaction as the decision commit it is
        caused by, instead of a separate `store.create_job(...)` call made
        after that transaction has already committed. This closes the
        atomicity gap where a decision is committed but its invalidation job
        is lost: a failure here rolls back the WHOLE decision
        (same `with self._tx()` block, same connection), never leaves a
        durably-committed decision with a silently-dropped invalidation job,
        matching the rule that an audit/event write failure rolls back the
        decision, extended to this write."""
        operation = "job.create"
        request_hash = canonical_hash({"job_id": job_id, "kind": kind,
                                       "payload": dict(payload),
                                       "expected_version": expected_version})
        prior = self._replay(cur, tenant_id, workspace_id, operation,
                             idempotency_key, request_hash)
        if prior is not None:
            if requested_by is not None:
                raise TenantScopeError("job not found or already attributed")
            self._link_job(cur, tenant_id, workspace_id, job_id, created=False)       # AH-05: this request replayed the same logical job
            return prior
        if head.version != expected_version or head.source_revision_id is None:
            raise StaleVersion("job input head is stale or absent")
        cur.execute("""SELECT max_total, window_after FROM v2_job_ceilings WHERE tenant_id=%s AND workspace_id=%s AND kind=%s
                          AND window_after IS NOT NULL ORDER BY window_after DESC, created_at DESC LIMIT 1""", (tenant_id, workspace_id, kind))
        window = cur.fetchone()
        if window is not None:
            # a person-approved NEW allocation: it replaces the older rows of this kind and counts only the jobs queued after it was granted
            ceiling, since = window
            cur.execute("""SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED'
                              AND payload->>'kind'=%s AND id > %s""", (tenant_id, workspace_id, kind, since))
            used = cur.fetchone()[0]
            if used >= ceiling:
                raise JobCeilingExceeded(f"{kind}: this workspace is authorized for {ceiling} job(s) of this kind under the current allocation "
                                         f"and has used {used}")
        else:
            cur.execute("SELECT min(max_total) FROM v2_job_ceilings WHERE tenant_id=%s AND workspace_id=%s AND kind=%s",
                        (tenant_id, workspace_id, kind))
            ceiling = cur.fetchone()[0]
            if ceiling is not None:
                cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind=%s", (tenant_id, workspace_id, kind))
                if cur.fetchone()[0] >= ceiling:
                    raise JobCeilingExceeded(f"{kind}: this workspace is authorized for {ceiling} job(s) of this kind in total and has used them")
        self._account_day_quota(cur, tenant_id, kind, requested_by)
        if max_concurrent_jobs is not None:
            cur.execute("""SELECT count(*) FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s
                  AND execution_status IN ('QUEUED','RUNNING')""",
                (tenant_id, workspace_id))
            currently_running = cur.fetchone()[0]
            if currently_running >= max_concurrent_jobs:
                raise ConcurrencyLimitExceeded(
                    f"{tenant_id}/{workspace_id}: {currently_running} jobs already "
                    f"running, at or above the configured concurrency limit "
                    f"{max_concurrent_jobs}")
        if budget_reservation is not None:
            estimate_usd, total_cap_usd = budget_reservation
            cur.execute("""SELECT project_id FROM v2_workspaces
                WHERE tenant_id=%s AND workspace_id=%s""", (tenant_id, workspace_id))
            project_id = cur.fetchone()[0]
            # Project-wide serialization point: the workspace row lock
            # taken by `_lock_head` only serializes creates in the SAME
            # workspace; two workspaces of one project would otherwise
            # both read the project sum before either reservation is
            # visible. Transaction-scoped, released at commit/rollback.
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"v2-project-budget:{tenant_id}:{project_id}",))
            # Reserve THIS job's worst-case estimate inside the same
            # transaction, then re-sum the whole project's ledger
            # (reservations + settlements). A refused reservation rolls
            # back with the transaction -- never a phantom row.
            cur.execute("""INSERT INTO v2_job_cost_ledger
                    (tenant_id, workspace_id, job_id, cost_usd, entry_state, reserved_usd)
                VALUES (%s,%s,%s,%s,'RESERVED',%s)
                ON CONFLICT (tenant_id, workspace_id, job_id) DO NOTHING""",
                (tenant_id, workspace_id, job_id, str(estimate_usd), str(estimate_usd)))
            cur.execute(PROJECT_SPEND_SQL, project_spend_params(tenant_id, workspace_id))
            spent = cur.fetchone()[0]
            if spent > total_cap_usd:
                from infra.worker.budget_config import BudgetExceeded
                raise BudgetExceeded(scope=f"{tenant_id}/{project_id}:total",
                                    cap=total_cap_usd, spent=spent - estimate_usd,
                                    estimate=estimate_usd)
        cur.execute("""INSERT INTO v2_jobs
            (tenant_id, workspace_id, job_id, kind, status, execution_status,
             pinned_source_revision_id, pinned_knowledge_revision_id,
             pinned_decision_epoch, payload)
            VALUES (%s,%s,%s,%s,'QUEUED','QUEUED',%s,%s,%s,%s)""",
            (tenant_id, workspace_id, job_id, kind, head.source_revision_id,
             head.knowledge_revision_id, head.decision_epoch, Json(dict(payload))))
        if requested_by is not None:
            if not isinstance(requested_by, str) or not requested_by.strip():
                raise ValueError("requested_by must be a non-empty account id")
            cur.execute("""UPDATE v2_jobs SET requested_by=%s, status='PENDING_APPROVAL'
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND status='QUEUED'""",
                (requested_by, tenant_id, workspace_id, job_id))
        result = PublishResult(head.version, job_id)
        self._record_commit(cur, tenant_id, workspace_id, operation,
                            idempotency_key, request_hash, result,
                            "JOB_QUEUED", {"job_id": job_id, "kind": kind,
                                           "source_revision_id": head.source_revision_id,
                                           "decision_epoch": head.decision_epoch})
        self._link_job(cur, tenant_id, workspace_id, job_id, created=True)
        return result

    def job_status(self, tenant_id: str, workspace_id: str, job_id: str) -> dict:
        with self._tx() as cur:
            cur.execute("""SELECT kind, status, pinned_source_revision_id,
                pinned_knowledge_revision_id, pinned_decision_epoch, payload,
                execution_status, execution_result, execution_reason
                FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
                (tenant_id, workspace_id, job_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("job not found")
        return {"kind": row[0], "status": row[1],
                "pinned_source_revision_id": row[2],
                "pinned_knowledge_revision_id": row[3],
                "pinned_decision_epoch": row[4], "payload": _as_object(row[5]),
                "execution_status": row[6], "execution_result": _as_object(row[7]),
                "execution_reason": sanitize_reason(row[8])}

    def find_finding_by_id(self, tenant_id: str, workspace_id: str,
                          finding_id: str) -> dict | None:
        """Read-only lookup of a single
        persisted finding (by `finding_id`) across every COMPLETED
        `kind='finding'` job in this tenant/workspace, mirroring
        `_transition_conflict_finding`'s scan of the same
        `execution_result.findings[]` JSONB (no dedicated findings table
        exists). Unlike `_transition_conflict_finding`, this never mutates
        anything and takes no row lock -- it exists purely so a caller (the
        `/v2/gap/accepted_unknown` critical-gate) can read a finding's real,
        persisted `severity`/`authority_required` fields instead of
        trusting a caller-supplied assertion. Returns `None` if no finding
        with that id is found (the caller must decide how to treat a
        not-found finding; this method does not raise)."""
        with self._tx() as cur:
            cur.execute("""SELECT execution_result FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND kind='finding'
                  AND execution_status='COMPLETED' AND execution_result IS NOT NULL""",
                (tenant_id, workspace_id))
            rows = cur.fetchall()
        for (raw_result,) in rows:
            result = _as_object(raw_result) or {}
            for finding in (result.get("findings") or []):
                if isinstance(finding, dict) and finding.get("finding_id") == finding_id:
                    return finding
        return None

    def job_record(self, tenant_id: str, workspace_id: str, job_id: str) -> dict:
        """Full job row, including requester/approver attribution for policy checks
        and the independent worker-owned execution axis."""
        with self._tx() as cur:
            cur.execute("""SELECT kind, status, requested_by, approved_by,
                approved_with_roles, payload, execution_status, execution_result,
                execution_reason, cancel_requested_at, execution_model_id,
                execution_prompt_hash, execution_schema_hash, execution_request_context,
                execution_executor_id, execution_usage, execution_provider_calls,
                execution_cost_usd, execution_duration_ms FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
                (tenant_id, workspace_id, job_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("job not found")
        return {"job_id": job_id, "kind": row[0], "status": row[1], "requested_by": row[2],
                "approved_by": row[3], "approved_with_roles": row[4],
                "payload": _as_object(row[5]), "execution_status": row[6],
                "execution_result": _as_object(row[7]), "execution_reason": sanitize_reason(row[8]),
                # isoformat: this dict is returned verbatim by GET /v2/jobs/{id}
                # (JSONResponse cannot serialize a datetime).
                "cancel_requested_at": row[9].isoformat() if row[9] is not None else None,
                # Execution metadata (model/prompt/schema hash, request context
                # IDs, usage/cost, duration). Hashes only -- never raw prompt text.
                "execution_model_id": row[10], "execution_prompt_hash": row[11],
                "execution_schema_hash": row[12],
                "execution_request_context": _as_object(row[13]),
                "execution_executor_id": row[14], "execution_usage": _as_object(row[15]),
                "execution_provider_calls": row[16],
                "execution_cost_usd": str(row[17]) if row[17] is not None else None,
                "execution_duration_ms": row[18]}

    def find_candidates_by_id(self, tenant_id: str, workspace_id: str,
                              candidate_ids: list) -> tuple[dict, list]:
        """Resolve `candidate_ids` (RuleCandidate ids emitted by
        an "extraction" job's `execution_result.candidates`/`.rejected`
        buckets) to their real, persisted candidate dicts, tenant/workspace
        scoped. There is no separate candidates table (they are persisted
        inside the owning job's `execution_result` JSONB); this scans every
        COMPLETED "extraction" job in this workspace once, never trusting a
        caller-supplied candidate blob. Returns `(found_by_id, missing_ids)`
        -- a ref that resolves to no real candidate is reported as missing,
        never silently treated as a confirmed edge/support."""
        wanted = set(candidate_ids or [])
        found: dict = {}
        if wanted:
            with self._tx() as cur:
                cur.execute("""SELECT execution_result FROM v2_jobs
                    WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'
                    AND execution_status='COMPLETED' AND execution_result IS NOT NULL""",
                    (tenant_id, workspace_id))
                rows = cur.fetchall()
            for (raw,) in rows:
                result = _as_object(raw) or {}
                for candidate in (result.get("candidates") or []):
                    cid = candidate.get("candidate_id") if isinstance(candidate, dict) else None
                    if cid in wanted and cid not in found:
                        found[cid] = candidate
        missing = [cid for cid in candidate_ids if cid not in found]
        return found, missing

    def knowledge_draft(self, tenant_id: str, workspace_id: str, artifact_id: str) -> dict:
        """The pending-human draft read surface. Chains the
        latest "extraction" job for `artifact_id` with every "relation"/
        "finding" job whose `payload.candidate_ids` overlaps that
        extraction's own candidate ids -- there is no dedicated chain-id
        column (candidates/relations/findings
        all live inside each job's own `execution_result` JSONB), so this
        reconstructs the chain by real candidate-id membership, never by
        trusting a caller-supplied correlation.

        A chain whose extraction has not reached `COMPLETED` (still
        `QUEUED`/`RUNNING`, or a terminal `FAILED`/`CANCELLED`) NEVER
        surfaces candidates/relations/findings/questions -- an incomplete or
        failed run must never look like a confirmable draft (V2 "no fake
        findings" boundary). `status` is `PENDING_HUMAN` only once the
        extraction stage is real and COMPLETED; it is never `CONFIRMED` here
        -- only `POST /v2/knowledge/confirm`'s own, separate decision axis
        can ever record a human confirmation, and this read never fabricates
        one.
        """
        # `decision_epoch` is read fresh from `v2_workspaces` on
        # EVERY call to this method, on every branch (including
        # NO_EXTRACTION and not-yet-COMPLETED) -- this is the smallest
        # correct staleness marker that lets a caller detect a stale read
        # even in the window before any invalidation worker has run (no
        # worker dependency at all: this is a plain, uncached read of the
        # SAME `decision_epoch` column `record_human_decision` bumps inside
        # its own transaction). It intentionally does not attempt to
        # recompute or cache an epoch value -- it always reflects whatever
        # is currently committed for this tenant/workspace at read time.
        current_decision_epoch = self.head(tenant_id, workspace_id).decision_epoch
        with self._tx() as cur:
            # "Latest" = the most recently QUEUED job (its JOB_QUEUED outbox row has a serial id).
            # job_id is random hex, so ordering by it picked an arbitrary run, not the newest one.
            cur.execute("""SELECT j.job_id, j.execution_status, j.execution_result FROM v2_jobs j
                WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind='extraction'
                  AND j.payload->>'artifact_id'=%s
                ORDER BY (SELECT max(o.id) FROM v2_outbox o
                           WHERE o.tenant_id=j.tenant_id AND o.workspace_id=j.workspace_id
                             AND o.kind='JOB_QUEUED' AND o.payload->>'job_id'=j.job_id) DESC NULLS LAST,
                         j.job_id DESC""", (tenant_id, workspace_id, artifact_id))
            extraction_rows = cur.fetchall()
        if not extraction_rows:
            return {"artifact_id": artifact_id, "extraction_job_id": None,
                    "execution_status": None, "status": "NO_EXTRACTION",
                    "decision_epoch": current_decision_epoch,
                    "candidates": [], "relations": [], "findings": [], "questions": []}

        job_id, execution_status, raw_result = extraction_rows[0]
        result = _as_object(raw_result) or {}
        candidates = list(result.get("candidates") or []) if execution_status == "COMPLETED" else []
        candidate_ids = {c.get("candidate_id") for c in candidates if isinstance(c, dict)}

        relations: list = []
        findings: list = []
        questions: list = []
        if execution_status == "COMPLETED" and candidate_ids:
            with self._tx() as cur:
                cur.execute("""SELECT execution_status, execution_result, payload FROM v2_jobs
                    WHERE tenant_id=%s AND workspace_id=%s AND kind='relation'""",
                    (tenant_id, workspace_id))
                relation_rows = cur.fetchall()
            for r_status, r_result, r_payload in relation_rows:
                payload = _as_object(r_payload) or {}
                ids = set(payload.get("candidate_ids") or [])
                if not (ids & candidate_ids) or r_status != "COMPLETED":
                    continue
                relations.extend((_as_object(r_result) or {}).get("proposed_edges") or [])

            with self._tx() as cur:
                cur.execute("""SELECT execution_status, execution_result, payload FROM v2_jobs
                    WHERE tenant_id=%s AND workspace_id=%s AND kind='finding'""",
                    (tenant_id, workspace_id))
                finding_rows = cur.fetchall()
            for f_status, f_result, f_payload in finding_rows:
                payload = _as_object(f_payload) or {}
                ids = set(payload.get("candidate_ids") or [])
                if not (ids & candidate_ids) or f_status != "COMPLETED":
                    continue
                fr = _as_object(f_result) or {}
                findings.extend(fr.get("findings") or [])
                questions.extend(fr.get("questions") or [])

            # Every RECOGNIZED candidate not already covered by a finding's
            # own question still needs an explicit human decision before it
            # can become current knowledge -- the pending-human surface must
            # never let a candidate reach the human silently unasked.
            covered = {cid for finding in findings
                      for cid in (finding.get("involved_claim_ids") or [])}
            for candidate in candidates:
                cid = candidate.get("candidate_id")
                if candidate.get("applicability") == "RECOGNIZED" and cid not in covered:
                    questions.append({
                        "id": f"question-confirm-{cid}", "finding_ids": [],
                        "candidate_ids": [cid],
                        "prompt": "Human confirmation required before this "
                                 "candidate becomes current knowledge."})

        if execution_status != "COMPLETED":
            # Fail-closed: a not-yet-complete or failed chain surfaces no
            # candidates/relations/findings/questions at all.
            candidates, relations, findings, questions = [], [], [], []
            status = "PENDING_HUMAN"
        else:
            status = "PENDING_HUMAN"
        return {"artifact_id": artifact_id, "extraction_job_id": job_id,
                "execution_status": execution_status, "status": status,
                "decision_epoch": current_decision_epoch,
                "candidates": candidates, "relations": relations,
                "findings": findings, "questions": questions}

    # ------------------------------------------------------------ invalidation

    def find_dependent_jobs_for_candidate(self, tenant_id: str, workspace_id: str,
                                          candidate_id: str,
                                          kinds: tuple[str, ...] = ("relation", "finding")
                                          ) -> list[dict]:
        """Real dependency-graph scan: every COMPLETED `relation`/`finding`
        job whose `payload.candidate_ids` names `candidate_id` -- the SAME
        candidate-id-membership reconstruction `knowledge_draft` already uses
        (no dedicated chain-id column exists). Returns dependents
        across ALL kinds in `kinds`, not just one, since a decision on a
        candidate can affect both a relation edge and a finding at once."""
        with self._tx() as cur:
            cur.execute("""SELECT job_id, kind, payload FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND kind = ANY(%s)
                  AND execution_status='COMPLETED'""",
                (tenant_id, workspace_id, list(kinds)))
            rows = cur.fetchall()
        dependents = []
        for job_id, kind, raw_payload in rows:
            payload = _as_object(raw_payload) or {}
            ids = set(payload.get("candidate_ids") or [])
            if candidate_id in ids:
                dependents.append({"job_id": job_id, "kind": kind, "payload": payload})
        return dependents

    def mark_job_stale(self, tenant_id: str, workspace_id: str, job_id: str,
                       decision_id: str) -> bool:
        """Durable stale marker (`v2_stale_markers`), never a mutation of the
        dependent job's own `execution_result` (that column stays the
        executor's untouched output; a job already-DONE at the outbox layer
        is not a terminal domain fact once a new decision makes its content
        stale). Idempotent: marking the same (job_id, decision_id) pair twice
        is a no-op (`ON CONFLICT DO NOTHING`); returns True only the first
        time (so a caller can tell "newly marked" from "already marked" for
        the required-idempotent-scheduling boundary)."""
        with self._tx() as cur:
            cur.execute("""INSERT INTO v2_stale_markers
                (tenant_id, workspace_id, job_id, decision_id)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (tenant_id, workspace_id, job_id, decision_id) DO NOTHING""",
                (tenant_id, workspace_id, job_id, decision_id))
            return cur.rowcount == 1

    def is_job_stale(self, tenant_id: str, workspace_id: str, job_id: str) -> bool:
        with self._tx() as cur:
            cur.execute("""SELECT count(*) FROM v2_stale_markers
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
                (tenant_id, workspace_id, job_id))
            return cur.fetchone()[0] > 0

    def causation_for_decision(self, tenant_id: str, workspace_id: str,
                               decision_id: str) -> dict:
        """Existing causation rows for this decision (`dependent_job_id ->
        reanalysis_job_id`) -- read BEFORE scheduling more, so re-processing
        the SAME decision event (duplicate/out-of-order invalidation job)
        never schedules a second reanalysis job for a dependent already
        covered, and the budget ceiling counts only genuinely NEW work."""
        with self._tx() as cur:
            cur.execute("""SELECT dependent_job_id, reanalysis_job_id
                FROM v2_reanalysis_causation
                WHERE tenant_id=%s AND workspace_id=%s AND decision_id=%s""",
                (tenant_id, workspace_id, decision_id))
            return dict(cur.fetchall())

    def record_reanalysis_causation(self, tenant_id: str, workspace_id: str, *,
                                    decision_id: str, invalidation_job_id: str,
                                    dependent_job_id: str, reanalysis_job_id: str) -> bool:
        """Durable decision -> invalidation job -> reanalysis job link (task
        card: "event causation chain"). PK is (tenant_id, workspace_id,
        decision_id, dependent_job_id) so at most one causation row -- and
        therefore at most one scheduled reanalysis job -- ever exists per
        (decision, dependent) pair; a duplicate insert is a no-op."""
        with self._tx() as cur:
            cur.execute("""INSERT INTO v2_reanalysis_causation
                (tenant_id, workspace_id, decision_id, invalidation_job_id,
                 dependent_job_id, reanalysis_job_id)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, workspace_id, decision_id, dependent_job_id)
                DO NOTHING""",
                (tenant_id, workspace_id, decision_id, invalidation_job_id,
                 dependent_job_id, reanalysis_job_id))
            return cur.rowcount == 1

    def hitl_answer_current_version(self, tenant_id: str, workspace_id: str,
                                    question_id: str) -> int:
        """Read-only current version for a queue/answer-draft caller (e.g. to
        show it in a UI before submitting) -- NOT locked, so it must never be
        used as the authoritative check inside `hitl_record_answer` itself
        (that method re-reads it under its own advisory lock)."""
        with self._tx() as cur:
            cur.execute("""SELECT count(*) FROM v2_hitl_answers
                WHERE tenant_id=%s AND workspace_id=%s AND question_id=%s""",
                (tenant_id, workspace_id, question_id))
            return cur.fetchone()[0]

    def hitl_record_answer(self, tenant_id: str, workspace_id: str,
                           question_id: str, actor_id: str, answer_text: str,
                           expected_version: int | None, *,
                           idempotency_key: str) -> dict:
        """Record a PENDING answer draft for `question_id`.

        This NEVER itself resolves/closes the underlying GAP/question -- the
        question/finding chain lives entirely in `knowledge_draft`'s own
        read (untouched by this method); only a separate
        `knowledge.confirm`/`conflict.resolve` action (the decision ledger)
        may ever do that. The "version" a caller answers against is derived
        here, not stored anywhere upstream: it is the count of previously
        committed answer rows for this exact (tenant, workspace,
        question_id) -- 0 the first time a question is ever answered.

        Concurrency: a transaction-scoped advisory lock keyed by
        (tenant, workspace, question_id) is taken before the version is
        counted, so two concurrent requests against the same question
        serialize on it and cannot both observe the same "current" count
        (the same pattern `create_job`'s project-budget reservation uses).

        Idempotency reuses the store's own generic `_replay`/`_record_commit`
        (operation "hitl.answer"): the SAME (idempotency_key, payload) pair
        replays the original committed answer_id unchanged; the SAME key
        with a DIFFERENT payload raises `StoreConflict` (409) -- neither
        double-transitions nor silently overwrites.
        """
        if not all(isinstance(x, str) and x.strip()
                   for x in (question_id, actor_id, answer_text, idempotency_key)):
            raise ValueError("question, actor, answer text and idempotency key required")
        operation = "hitl.answer"
        request_hash = canonical_hash({"question_id": question_id, "actor_id": actor_id,
                                       "answer": answer_text,
                                       "expected_version": expected_version})
        with self._tx() as cur:
            self._lock_head(cur, tenant_id, workspace_id)
            prior = self._replay(cur, tenant_id, workspace_id, operation,
                                 idempotency_key, request_hash)
            if prior is not None:
                return self._hitl_answer_row(cur, tenant_id, workspace_id, prior.revision_id)
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"v2-hitl-question:{tenant_id}:{workspace_id}:{question_id}",))
            cur.execute("""SELECT count(*) FROM v2_hitl_answers
                WHERE tenant_id=%s AND workspace_id=%s AND question_id=%s""",
                (tenant_id, workspace_id, question_id))
            current_version = cur.fetchone()[0]
            normalized_expected = 0 if expected_version is None else expected_version
            if normalized_expected != current_version:
                raise HitlVersionConflict(current_version)
            answer_id = uuid.uuid4().hex
            version_after = current_version + 1
            cur.execute("""INSERT INTO v2_hitl_answers
                (tenant_id, workspace_id, question_id, answer_id, actor_id,
                 answer_text, status, version_after, idempotency_key)
                VALUES (%s,%s,%s,%s,%s,%s,'PENDING',%s,%s)""",
                (tenant_id, workspace_id, question_id, answer_id, actor_id,
                 answer_text, version_after, idempotency_key))
            result = PublishResult(version_after, answer_id)
            self._record_commit(cur, tenant_id, workspace_id, operation,
                                idempotency_key, request_hash, result,
                                "HITL_ANSWER_RECORDED",
                                {"question_id": question_id, "answer_id": answer_id,
                                 "actor_id": actor_id, "version": version_after})
            return {"answer_id": answer_id, "status": "PENDING", "actor": actor_id,
                    "version": version_after}

    def _hitl_answer_row(self, cur, tenant_id: str, workspace_id: str,
                         answer_id: str) -> dict:
        cur.execute("""SELECT actor_id, status, version_after FROM v2_hitl_answers
            WHERE tenant_id=%s AND workspace_id=%s AND answer_id=%s""",
            (tenant_id, workspace_id, answer_id))
        row = cur.fetchone()
        if row is None:
            raise TenantScopeError("answer not found")
        return {"answer_id": answer_id, "status": row[1], "actor": row[0],
                "version": row[2]}

    def hitl_queue(self, tenant_id: str, workspace_id: str, *,
                   redact_evidence: bool) -> dict:
        """The human work queue read surface. Groups pending
        questions (reusing `knowledge_draft`'s own real chain-reconstruction
        -- one shared resolution path, never a second one) by the
        `artifact_id` extraction jobs in this workspace actually produced
        them for, and attaches the current answer-draft status/version for
        each. When `redact_evidence` is True (the caller lacks the
        evidence-read permission), every candidate's raw `quotes` field is
        stripped from the DTO entirely -- never merely hidden client-side
        (snippets must not leak through queue serialization)."""
        with self._tx() as cur:
            cur.execute("""SELECT DISTINCT payload->>'artifact_id' FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'""",
                (tenant_id, workspace_id))
            artifact_ids = [r[0] for r in cur.fetchall() if r[0]]

        items: list[dict] = []
        for artifact_id in artifact_ids:
            draft = self.knowledge_draft(tenant_id, workspace_id, artifact_id)
            candidates_by_id = {c.get("candidate_id"): c for c in draft["candidates"]
                               if isinstance(c, dict)}
            for question in draft["questions"]:
                candidate_ids = question.get("candidate_ids") or []
                related = []
                for cid in candidate_ids:
                    candidate = candidates_by_id.get(cid)
                    if candidate is None:
                        continue
                    if redact_evidence:
                        # Raw source-quote text lives under the candidate's
                        # "evidence" list (`{"locator","quote","parser_version"}`
                        # per `infra.worker.extraction`), not a top-level
                        # "quotes" field -- strip the actual field the real
                        # extraction pipeline populates.
                        candidate = {k: v for k, v in candidate.items()
                                    if k != "evidence"}
                    related.append(candidate)
                version = self.hitl_answer_current_version(tenant_id, workspace_id,
                                                           question["id"])
                items.append({
                    "question_id": question["id"], "artifact_id": artifact_id,
                    "prompt": question.get("prompt"),
                    "candidates": related, "version": version,
                    "status": "PENDING" if version == 0 else "ANSWERED"})
        # The same read model as the completion gate: a question about a finding that is history, a repeat of another run's finding, or already decided is not an open question.
        from infra import v2_current_findings as CF
        with self._tx() as cur:
            model = CF.build(cur, tenant_id, workspace_id)
        kept, hidden_ids = [], {"history": set(), "repeat": set(), "resolved": set()}
        for it in items:
            qid = it["question_id"]
            info = model.by_finding_id.get(qid[len("question-"):]) if qid.startswith("question-") else None
            if info is None:
                kept.append(it)
            elif info["role"] == "history":
                hidden_ids["history"].add(qid)
            elif info["role"] == "repeat":
                hidden_ids["repeat"].add(qid)
            elif info["resolved"]:
                hidden_ids["resolved"].add(qid)
            else:
                kept.append(it)
        return {"items": kept, "hidden": {k: len(v) for k, v in hidden_ids.items()}, "head_source_revision_id": model.head}

    def set_job_requester(self, tenant_id: str, workspace_id: str,
                          job_id: str, requested_by: str) -> None:
        with self._tx() as cur:
            cur.execute("""UPDATE v2_jobs SET requested_by=%s, status='PENDING_APPROVAL'
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND status='QUEUED'""",
                (requested_by, tenant_id, workspace_id, job_id))
            if cur.rowcount != 1:
                raise TenantScopeError("job not found or already attributed")

    def approve_job(self, tenant_id: str, workspace_id: str, job_id: str,
                    approved_by: str, approved_with_roles: tuple[str, ...], *,
                    self_approval_audit: Mapping[str, object] | None = None) -> None:
        """Only a job still PENDING_APPROVAL can be approved; re-entry is rejected.

        When `self_approval_audit` is given (the workspace relaxed the
        distinct-approver policy and this specific approval is self-approval),
        the required `job.self_approval` audit event is written IN THE SAME
        transaction as the approval state transition. If the audit insert
        fails for any reason, the whole transaction rolls back: the job stays
        `PENDING_APPROVAL` with no partial approval and no orphaned audit
        state -- never a durably `APPROVED` job with zero audit events for a
        self-approval the policy required to be logged.
        """
        with self._tx() as cur:
            cur.execute("""UPDATE v2_jobs SET status='APPROVED', approved_by=%s,
                approved_with_roles=%s
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                  AND status='PENDING_APPROVAL'""",
                (approved_by, list(approved_with_roles), tenant_id, workspace_id, job_id))
            if cur.rowcount != 1:
                raise StoreConflict("job is not pending approval")
            if self_approval_audit is not None:
                cur.execute("""INSERT INTO v2_audit
                    (tenant_id, workspace_id, event_type, actor_id, payload)
                    VALUES (%s,%s,'job.self_approval','policy',%s)""",
                    (tenant_id, workspace_id, Json(dict(self_approval_audit))))

    def count_active_jobs(self, tenant_id: str, workspace_id: str) -> int:
        """Count of jobs not yet in a terminal state, for the
        per-workspace concurrency admission (`infra/worker/job_quota.py::
        check_concurrency_admission`). Purely additive: no existing method's
        behaviour changes."""
        with self._tx() as cur:
            cur.execute("""SELECT count(*) FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s
                  AND execution_status IN ('QUEUED','RUNNING')""",
                (tenant_id, workspace_id))
            return cur.fetchone()[0]

    # ------------------------------------------------------------------ change decision

    def record_change_decision(self, tenant_id: str, workspace_id: str, job_id: str, *,
                               actor_id: str, action: str, reason: str,
                               approval_digest: str | None, self_approval: bool,
                               high_risk: bool, idempotency_key: str | None,
                               payload_hash: str | None,
                               new_proposal_job_id: str | None = None,
                               new_proposal_payload: Mapping[str, object] | None = None,
                               new_proposal_idempotency_key: str | None = None,
                               locked_check: Callable[[object, WorkspaceHead], None] | None = None) -> dict:
        """A human approve/reject/request_changes decision on a `kind=approval`
        job (`job_id` is that approval job's own id, never `proposal_job_id`). Deliberately NOT `record_human_decision`: this never
        bumps `decision_epoch` or nulls `knowledge_revision_id` -- the whole
        point of a dedicated ledger (see the migration file's own header).

        Everything below runs in ONE transaction, under a `FOR UPDATE` lock
        on the approval job's own `v2_jobs` row -- mirroring the
        publish-race pattern: the recheck (idempotency
        replay, "already has a terminal decision") and the write happen
        under the SAME lock, so two concurrent decisions on the same job_id
        serialize here; the second transaction's SELECT only proceeds once
        the first has committed (or rolled back), and therefore observes the
        first's own already-inserted row. A `request_changes` action creates
        its NEW proposal-job version (via the same `_create_job_core` used
        by `create_job`/`record_human_decision`) in this SAME transaction --
        never a separate, later `create_job` call that could commit a
        decision with no matching new proposal version if the second step
        failed.
        """
        with self._tx() as cur:
            cur.execute("""SELECT 1 FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s FOR UPDATE""",
                (tenant_id, workspace_id, job_id))
            if cur.fetchone() is None:
                raise TenantScopeError("approval job not found")
            if idempotency_key:
                cur.execute("""SELECT decision_id, payload_hash, new_proposal_job_id
                    FROM v2_change_decisions
                    WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                      AND idempotency_key=%s""",
                    (tenant_id, workspace_id, job_id, idempotency_key))
                prior = cur.fetchone()
                if prior is not None:
                    if prior[1] != payload_hash:
                        raise StoreConflict("idempotency_conflict")
                    return {"decision_id": prior[0], "new_proposal_job_id": prior[2],
                            "replay": True}
            if action in ("approve", "reject"):
                cur.execute("""SELECT 1 FROM v2_change_decisions
                    WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                      AND action IN ('approve','reject')""",
                    (tenant_id, workspace_id, job_id))
                if cur.fetchone() is not None:
                    raise StoreConflict(
                        "a terminal decision has already been recorded for this approval job")
            if action == "approve" and locked_check is not None:
                # lock order: the approval job row (above), then the workspace row (as request_changes below and every job creation). An approve is never
                # recorded for work whose draft was revised: the check and the insert are one transaction under the workspace-row lock.
                locked_check(cur, self._lock_head(cur, tenant_id, workspace_id))
            resolved_new_proposal_job_id = None
            if action == "request_changes" and new_proposal_payload is not None:
                head = self._lock_head(cur, tenant_id, workspace_id)
                new_job_result = self._create_job_core(
                    cur, tenant_id, workspace_id, new_proposal_job_id, "proposal",
                    new_proposal_payload, head=head, expected_version=head.version,
                    idempotency_key=new_proposal_idempotency_key, requested_by=actor_id)
                resolved_new_proposal_job_id = new_job_result.revision_id
            decision_id = "dec-" + uuid.uuid4().hex
            try:
                cur.execute("""INSERT INTO v2_change_decisions
                    (tenant_id, workspace_id, job_id, decision_id, action, actor_id, reason,
                     approval_digest, self_approval, high_risk, idempotency_key, payload_hash,
                     new_proposal_job_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, job_id, decision_id, action, actor_id, reason,
                     approval_digest, self_approval, high_risk, idempotency_key, payload_hash,
                     resolved_new_proposal_job_id))
            except psycopg2.IntegrityError as exc:
                raise StoreConflict(f"decision conflict: {exc}") from exc
            cur.execute("""INSERT INTO v2_audit
                (tenant_id, workspace_id, event_type, actor_id, payload)
                VALUES (%s,%s,'CHANGE_DECISION_RECORDED',%s,%s)""",
                (tenant_id, workspace_id, actor_id,
                 Json({"job_id": job_id, "decision_id": decision_id, "action": action,
                       "new_proposal_job_id": resolved_new_proposal_job_id})))
            return {"decision_id": decision_id,
                    "new_proposal_job_id": resolved_new_proposal_job_id, "replay": False}

    def change_decisions(self, tenant_id: str, workspace_id: str, job_id: str) -> list[dict]:
        """The approval history panel read surface: every decision recorded against this approval job, oldest
        first."""
        with self._tx() as cur:
            cur.execute("""SELECT decision_id, action, actor_id, reason, approval_digest,
                    self_approval, high_risk, new_proposal_job_id, created_at
                FROM v2_change_decisions
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s ORDER BY id""",
                (tenant_id, workspace_id, job_id))
            rows = cur.fetchall()
        return [{"decision_id": r[0], "action": r[1], "actor_id": r[2], "reason": r[3],
                "approval_digest": r[4], "self_approval": r[5], "high_risk": r[6],
                "new_proposal_job_id": r[7], "created_at": r[8].isoformat()} for r in rows]

    # ------------------------------------------------------------------ verification contract

    _CONTRACT_COLUMNS = ("contract_id", "tenant_id", "project_id", "workspace_id",
                         "proposal_job_id", "logical_path", "kind", "locator", "expected",
                         "author_account_id", "provenance", "created_at", "contract_sha256")

    @classmethod
    def _contract_row(cls, row) -> dict:
        out = dict(zip(cls._CONTRACT_COLUMNS, row))
        out["expected"] = _as_object(out["expected"])
        out["provenance"] = _as_object(out["provenance"]) or {}
        out["created_at"] = out["created_at"].isoformat()
        return out

    def record_verification_contract(self, tenant_id: str, workspace_id: str, *,
                                     proposal_job_id: str, logical_path: str, kind: str,
                                     locator: str, expected, author_account_id: str,
                                     provenance: Mapping[str, str] | None = None) -> dict:
        """Persist ONE immutable, operator-supplied verification contract and its
        audit row in the SAME transaction. `contract_sha256` is computed here over the
        stored fields (never taken from a caller). A second contract for the same
        (workspace, proposal_job_id, logical_path, locator) is a `StoreConflict` -- rows are
        never updated; a revision is a new proposal job."""
        from infra.worker.verification_contract import contract_fields_hash
        contract_id = "vc-" + uuid.uuid4().hex
        provenance = dict(provenance or {})
        with self._tx() as cur:
            cur.execute("""SELECT project_id FROM v2_workspaces
                WHERE tenant_id=%s AND workspace_id=%s""", (tenant_id, workspace_id))
            row = cur.fetchone()
            if row is None:
                raise TenantScopeError("workspace not found")
            project_id = row[0]
            sha = contract_fields_hash(
                contract_id=contract_id, tenant_id=tenant_id, project_id=project_id,
                workspace_id=workspace_id, proposal_job_id=proposal_job_id,
                logical_path=logical_path, kind=kind, locator=locator, expected=expected,
                author_account_id=author_account_id, provenance=provenance)
            try:
                cur.execute("""INSERT INTO v2_verification_contracts
                    (contract_id, tenant_id, project_id, workspace_id, proposal_job_id,
                     logical_path, kind, locator, expected, author_account_id, provenance,
                     contract_sha256)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING created_at""",
                    (contract_id, tenant_id, project_id, workspace_id, proposal_job_id,
                     logical_path, kind, locator, Json(expected), author_account_id,
                     Json(provenance), sha))
            except psycopg2.IntegrityError as exc:
                raise StoreConflict(
                    "a verification contract already exists for this proposal, path and "
                    "locator (contracts are immutable)") from exc
            created_at = cur.fetchone()[0]
            cur.execute("""INSERT INTO v2_audit
                (tenant_id, workspace_id, event_type, actor_id, payload)
                VALUES (%s,%s,'VERIFICATION_CONTRACT_SET',%s,%s)""",
                (tenant_id, workspace_id, author_account_id,
                 Json({"contract_id": contract_id, "proposal_job_id": proposal_job_id,
                       "logical_path": logical_path, "kind": kind, "locator": locator,
                       "contract_sha256": sha, "provenance": provenance})))
        return {"contract_id": contract_id, "tenant_id": tenant_id, "project_id": project_id,
                "workspace_id": workspace_id, "proposal_job_id": proposal_job_id,
                "logical_path": logical_path, "kind": kind, "locator": locator,
                "expected": expected, "author_account_id": author_account_id,
                "provenance": provenance, "created_at": created_at.isoformat(),
                "contract_sha256": sha}

    def verification_contract(self, tenant_id: str, workspace_id: str,
                              contract_id: str) -> dict | None:
        """One contract scoped to (tenant, workspace); `None` when absent (also for a
        contract id that only exists in another tenant/workspace)."""
        with self._tx() as cur:
            cur.execute(f"""SELECT {', '.join(self._CONTRACT_COLUMNS)}
                FROM v2_verification_contracts
                WHERE tenant_id=%s AND workspace_id=%s AND contract_id=%s""",
                (tenant_id, workspace_id, contract_id))
            row = cur.fetchone()
        return None if row is None else self._contract_row(row)

    def verification_contracts_for_proposal(self, tenant_id: str, workspace_id: str,
                                            proposal_job_id: str) -> list[dict]:
        with self._tx() as cur:
            cur.execute(f"""SELECT {', '.join(self._CONTRACT_COLUMNS)}
                FROM v2_verification_contracts
                WHERE tenant_id=%s AND workspace_id=%s AND proposal_job_id=%s
                ORDER BY created_at, contract_id""",
                (tenant_id, workspace_id, proposal_job_id))
            rows = cur.fetchall()
        return [self._contract_row(r) for r in rows]

    def cancel_job(self, tenant_id: str, workspace_id: str, job_id: str,
                   reason: str) -> None:
        with self._tx() as cur:
            cur.execute("""UPDATE v2_jobs SET status='CANCELLED',
                payload = payload || %s
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                  AND status IN ('QUEUED','PENDING_APPROVAL','APPROVED')""",
                (Json({"cancel_reason": reason}), tenant_id, workspace_id, job_id))
            if cur.rowcount != 1:
                raise StoreConflict("job cannot be cancelled from its current status")

    # ------------------------------------------------------------------ execution axis

    def begin_execution(self, tenant_id: str, workspace_id: str, job_id: str, *,
                        max_running: int | None = None) -> dict | None:
        """The worker's claim-time admission + start
        transition, in ONE transaction under the workspace row lock (the same
        lock `create_job`'s concurrency count uses).

        Returns the job snapshot the executor needs (kind, payload, pinned
        revision IDs, project_id, cancel flag, the project's model-data
        policy) with `execution_status` moved `QUEUED`->`RUNNING` (a reclaim
        after a crashed worker finds it already `RUNNING`, which is accepted:
        the fencing token, not this column, decides ownership). Returns
        `None` WITHOUT changing anything if `max_running` is given and that
        many OTHER jobs of this workspace are already `RUNNING` (real
        claim-time concurrency admission, not a prediction-table lookup).
        Raises `StoreConflict` if the execution is already terminal.
        """
        with self._tx() as cur:
            self._lock_head(cur, tenant_id, workspace_id)
            cur.execute("""SELECT j.kind, j.payload, j.execution_status, j.cancel_requested_at,
                    j.pinned_source_revision_id, j.pinned_knowledge_revision_id,
                    j.pinned_decision_epoch, w.project_id
                FROM v2_jobs AS j JOIN v2_workspaces AS w
                  ON w.tenant_id=j.tenant_id AND w.workspace_id=j.workspace_id
                WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.job_id=%s FOR UPDATE OF j""",
                (tenant_id, workspace_id, job_id))
            row = cur.fetchone()
            if row is None:
                raise TenantScopeError("job not found")
            (kind, payload, execution_status, cancel_requested_at, pinned_source,
             pinned_knowledge, pinned_epoch, project_id) = row
            if execution_status not in _LIVE_EXECUTION:
                raise StoreConflict(
                    f"job execution already terminal (execution_status={execution_status!r})")
            if max_running is not None:
                cur.execute("""SELECT count(*) FROM v2_jobs
                    WHERE tenant_id=%s AND workspace_id=%s AND job_id<>%s
                      AND execution_status='RUNNING'""", (tenant_id, workspace_id, job_id))
                if cur.fetchone()[0] >= max_running:
                    return None
            cur.execute("""UPDATE v2_jobs SET execution_status='RUNNING'
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                  AND execution_status IN ('QUEUED','RUNNING')""",
                (tenant_id, workspace_id, job_id))
            policy = self._model_policy(cur, tenant_id, project_id)
            # Provenance of the pinned source. A revision committed
            # through the import boundary is private uploaded data, which the
            # worker may send to an external model only after the project's
            # model-data policy has been explicitly admitted.
            cur.execute("""SELECT import_id FROM v2_source_imports
                WHERE tenant_id=%s AND workspace_id=%s AND committed_revision_id=%s""",
                (tenant_id, workspace_id, pinned_source))
            import_row = cur.fetchone()
            if import_row is None:
                # Bytes uploaded through a manual refresh are exactly as
                # private as an import's; the same model-data admission applies.
                cur.execute("""SELECT refresh_id FROM v2_source_refreshes
                    WHERE tenant_id=%s AND workspace_id=%s AND committed_revision_id=%s""",
                    (tenant_id, workspace_id, pinned_source))
                import_row = cur.fetchone()
        return {"job_id": job_id, "kind": kind, "payload": _as_object(payload),
                "source_import_id": import_row[0] if import_row else None,
                "cancel_requested": cancel_requested_at is not None,
                "tenant_id": tenant_id, "workspace_id": workspace_id,
                "project_id": project_id, "pinned_source_revision_id": pinned_source,
                "pinned_knowledge_revision_id": pinned_knowledge,
                "pinned_decision_epoch": pinned_epoch, "model_policy": policy}

    def source_revision_exists(self, tenant_id: str, workspace_id: str,
                               revision_id: str) -> bool:
        with self._tx() as cur:
            cur.execute("""SELECT 1 FROM v2_source_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s""",
                (tenant_id, workspace_id, revision_id))
            return cur.fetchone() is not None

    @staticmethod
    def _model_policy(cur, tenant_id: str, project_id: str) -> dict:
        cur.execute("""SELECT prompt_storage, external_provider FROM v2_project_model_policy
            WHERE tenant_id=%s AND project_id=%s""", (tenant_id, project_id))
        row = cur.fetchone()
        if row is None:
            return {"prompt_storage": "hash_only", "external_provider": "allow",
                    "configured": False}
        return {"prompt_storage": row[0], "external_provider": row[1], "configured": True}

    def set_project_model_policy(self, tenant_id: str, project_id: str, *,
                                 prompt_storage: str = "hash_only",
                                 external_provider: str = "allow") -> None:
        """Project-scoped redaction/model-data policy. Values are
        validated here AND by the table's CHECK constraints."""
        if prompt_storage not in ("hash_only", "redacted_excerpt"):
            raise ValueError("prompt_storage must be 'hash_only' or 'redacted_excerpt'")
        if external_provider not in ("allow", "deny"):
            raise ValueError("external_provider must be 'allow' or 'deny'")
        with self._tx() as cur:
            cur.execute("""INSERT INTO v2_project_model_policy
                    (tenant_id, project_id, prompt_storage, external_provider)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (tenant_id, project_id) DO UPDATE SET
                    prompt_storage=EXCLUDED.prompt_storage,
                    external_provider=EXCLUDED.external_provider, updated_at=now()""",
                (tenant_id, project_id, prompt_storage, external_provider))

    def project_model_policy(self, tenant_id: str, project_id: str) -> dict:
        with self._tx() as cur:
            return self._model_policy(cur, tenant_id, project_id)

    @staticmethod
    def _insert_attempt(cur, tenant_id: str, workspace_id: str, job_id: str,
                        attempt: Mapping[str, object]) -> None:
        attempt = _safe_attempt(attempt)
        cur.execute("""INSERT INTO v2_job_attempts
                (tenant_id, workspace_id, job_id, attempt_key, outcome, reason, retryable,
                 executor_id, model_id, prompt_hash, schema_hash, request_context,
                 request_excerpt, provider_calls, input_tokens, output_tokens,
                 cost_estimate_usd, cost_usd, duration_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id, workspace_id, job_id, attempt_key) DO NOTHING""",
            (tenant_id, workspace_id, job_id, attempt["attempt_key"], attempt["outcome"],
             attempt.get("reason"), attempt.get("retryable"), attempt.get("executor_id"),
             attempt.get("model_id"), attempt.get("prompt_hash"), attempt.get("schema_hash"),
             Json(dict(attempt.get("request_context") or {})), attempt.get("request_excerpt"),
             int(attempt.get("provider_calls") or 0), attempt.get("input_tokens"),
             attempt.get("output_tokens"),
             (str(attempt["cost_estimate_usd"]) if attempt.get("cost_estimate_usd") is not None
              else None),
             str(attempt.get("cost_usd") if attempt.get("cost_usd") is not None else "0"),
             attempt.get("duration_ms")))
        # the durable "provider call in flight" marker rows of THIS claim are replaced by the recorded attempt (the recorded row carries the same facts with the usage): never counted twice
        cur.execute("""DELETE FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND outcome='IN_FLIGHT' AND attempt_key LIKE %s""",
            (tenant_id, workspace_id, job_id, str(attempt["attempt_key"]) + ":inflight:%"))

    @staticmethod
    def _accrue_ledger(cur, tenant_id: str, workspace_id: str, job_id: str, *,
                       settle: bool) -> object:
        """Bring the job's ledger row in line with the SUM of its recorded
        attempt costs, inside the caller's transaction. While RESERVED the
        row never drops below the reservation (so admission stays
        conservative); on `settle` it becomes exactly the real sum."""
        cur.execute("""SELECT COALESCE(SUM(cost_usd), 0) FROM v2_job_attempts
            WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
            (tenant_id, workspace_id, job_id))
        actual = cur.fetchone()[0]
        if settle:
            cur.execute("""UPDATE v2_job_cost_ledger SET cost_usd=%s, entry_state='SETTLED',
                    settled_at=now()
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
                (actual, tenant_id, workspace_id, job_id))
            if cur.rowcount == 0:
                cur.execute("""INSERT INTO v2_job_cost_ledger
                        (tenant_id, workspace_id, job_id, cost_usd, entry_state, settled_at)
                    VALUES (%s,%s,%s,%s,'SETTLED',now())""",
                    (tenant_id, workspace_id, job_id, actual))
        else:
            cur.execute("""UPDATE v2_job_cost_ledger SET cost_usd=CASE
                    WHEN entry_state='RESERVED'
                        THEN GREATEST(COALESCE(reserved_usd, cost_usd), %s)
                    ELSE %s END
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
                (actual, actual, tenant_id, workspace_id, job_id))
            if cur.rowcount == 0:
                cur.execute("""INSERT INTO v2_job_cost_ledger
                        (tenant_id, workspace_id, job_id, cost_usd, entry_state)
                    VALUES (%s,%s,%s,%s,'RESERVED')""",
                    (tenant_id, workspace_id, job_id, actual))
        return actual

    ADJUSTMENT_PREFIX = "adjustment:"

    def record_cost_adjustment(self, tenant_id: str, workspace_id: str, adjustment_id: str, cost_usd, *, reason: str) -> dict:
        """Book a cost the provider charged that no attempt recorded (e.g. usage lost before it was booked, priced fail-closed) into THE ledger that every
        reservation sums (`create_job` re-sums the whole project under the project lock), so every live entry point is held to the allowance net of it.
        One SETTLED row `adjustment:<id>`; idempotent: the same id and amount again changes nothing, a different amount is refused (history is never
        rewritten). Returns {"created", "cost_usd", "job_id"}."""
        import re
        from decimal import Decimal
        if not re.fullmatch(r"ADJ-[0-9]{8}-[A-Za-z0-9-]{1,40}", str(adjustment_id)):
            raise ValueError("adjustment id must look like ADJ-YYYYMMDD-<tag>")
        amount = Decimal(str(cost_usd))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("an adjustment is a positive, finite amount")
        if not str(reason or "").strip():
            raise ValueError("an adjustment needs a reason")
        job_id = self.ADJUSTMENT_PREFIX + adjustment_id
        with self._tx() as cur:
            cur.execute("SELECT project_id FROM v2_workspaces WHERE tenant_id=%s AND workspace_id=%s", (tenant_id, workspace_id))
            row = cur.fetchone()
            if row is None:
                raise TenantScopeError(f"{tenant_id}/{workspace_id}: unknown workspace")
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"v2-project-budget:{tenant_id}:{row[0]}",))
            cur.execute("""INSERT INTO v2_job_cost_ledger (tenant_id, workspace_id, job_id, cost_usd, entry_state, settled_at)
                VALUES (%s,%s,%s,%s,'SETTLED',now()) ON CONFLICT (tenant_id, workspace_id, job_id) DO NOTHING""", (tenant_id, workspace_id, job_id, str(amount)))
            created = cur.rowcount == 1
            if not created:
                cur.execute("SELECT cost_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (tenant_id, workspace_id, job_id))
                existing = Decimal(cur.fetchone()[0])
                if existing != amount:
                    raise StoreConflict(f"adjustment {adjustment_id} is already booked at {existing}; it is never rewritten")
        return {"created": created, "cost_usd": str(amount), "job_id": job_id}

    def begin_provider_attempt(self, tenant_id: str, workspace_id: str, job_id: str, attempt_key: str, *,
                               ceiling_usd, context: Mapping[str, object]) -> None:
        """Write, BEFORE the provider is contacted, a durable attempt row (`IN_FLIGHT`) booked at the attempt's budget ceiling, in the same transaction that
        keeps the job's ledger row at or above it. If the worker dies after this point nobody knows whether the provider charged: the row stays booked."""
        with self._tx() as cur:
            cur.execute("""INSERT INTO v2_job_attempts
                    (tenant_id, workspace_id, job_id, attempt_key, outcome, reason, retryable, request_context, provider_calls, cost_estimate_usd, cost_usd)
                VALUES (%s,%s,%s,%s,'IN_FLIGHT',%s,NULL,%s,1,%s,%s)
                ON CONFLICT (tenant_id, workspace_id, job_id, attempt_key) DO NOTHING""",
                (tenant_id, workspace_id, job_id, attempt_key, "provider call about to be sent; usage unknown until the attempt is recorded",
                 Json(dict(context)), str(ceiling_usd), str(ceiling_usd)))
            self._accrue_ledger(cur, tenant_id, workspace_id, job_id, settle=False)

    def mark_orphaned_provider_attempts(self, tenant_id: str, workspace_id: str, job_id: str, live_claim_prefix: str) -> dict:
        """On a reclaim: every `IN_FLIGHT` row that does not belong to the claim now running belongs to a worker that died after the provider attempt started.
        It becomes `USAGE_UNKNOWN` (potentially charged), stays booked at its ceiling (estimated, not actual) and counts as a provider attempt. Returns the
        number of provider attempts the job has used so far and the cost booked."""
        with self._tx() as cur:
            cur.execute("""UPDATE v2_job_attempts SET outcome='USAGE_UNKNOWN',
                    reason='worker_died_after_provider_attempt_started: potentially charged, usage unknown; booked at the attempt ceiling'
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND outcome='IN_FLIGHT' AND attempt_key NOT LIKE %s""",
                (tenant_id, workspace_id, job_id, live_claim_prefix + "%"))
            marked = cur.rowcount
            cur.execute("""SELECT count(*), COALESCE(sum(cost_usd), 0) FROM v2_job_attempts
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND provider_calls > 0""", (tenant_id, workspace_id, job_id))
            used, cost = cur.fetchone()
            self._accrue_ledger(cur, tenant_id, workspace_id, job_id, settle=False)
        return {"marked": marked, "provider_attempts": int(used), "cost_usd": cost}

    def provider_attempts_used(self, tenant_id: str, workspace_id: str, job_ids) -> dict[str, int]:
        """Read-only: provider attempts each job has used (the same rows the worker counts against the retry limit). No row is marked or booked."""
        ids = list(job_ids)
        if not ids:
            return {}
        with self._tx() as cur:
            cur.execute("""SELECT job_id, count(*) FROM v2_job_attempts
                WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s) AND provider_calls > 0 GROUP BY job_id""", (tenant_id, workspace_id, ids))
            used = {r[0]: int(r[1]) for r in cur.fetchall()}
        return {j: used.get(j, 0) for j in ids}

    def reconcile_unknown_attempt(self, tenant_id: str, workspace_id: str, job_id: str, attempt_key: str, *,
                                  actual_cost_usd, actor_id: str, reason: str) -> dict:
        """An operator replaces the conservative booking of a `USAGE_UNKNOWN` attempt with the cost the provider's billing shows (never raises it above nothing: any
        amount >= 0). Audited, once per attempt; the job's ledger row follows."""
        if not reason or len(reason.strip()) < 10:
            raise ValueError("a reason of at least 10 characters is required")
        with self._tx() as cur:
            cur.execute("""SELECT outcome, cost_usd FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND attempt_key=%s FOR UPDATE""",
                        (tenant_id, workspace_id, job_id, attempt_key))
            row = cur.fetchone()
            if row is None or row[0] != "USAGE_UNKNOWN":
                raise StoreConflict("only an attempt in state USAGE_UNKNOWN can be reconciled")
            cur.execute("""UPDATE v2_job_attempts SET outcome='USAGE_UNKNOWN_RECONCILED', cost_usd=%s,
                    request_context = request_context || %s::jsonb
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND attempt_key=%s""",
                (str(actual_cost_usd), Json({"reconciled_from_booked_usd": str(row[1]), "reconciled_by": actor_id, "reconcile_reason": reason}),
                 tenant_id, workspace_id, job_id, attempt_key))
            cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'job_attempt.reconciled',%s,%s)""",
                        (tenant_id, workspace_id, actor_id, Json({"job_id": job_id, "attempt_key": attempt_key, "booked_usd": str(row[1]),
                                                                   "actual_usd": str(actual_cost_usd), "reason": reason})))
            cur.execute("SELECT entry_state FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (tenant_id, workspace_id, job_id))
            state = cur.fetchone()
            self._accrue_ledger(cur, tenant_id, workspace_id, job_id, settle=bool(state and state[0] == "SETTLED"))
        return {"booked_usd": row[1], "actual_usd": actual_cost_usd}

    def record_attempt(self, tenant_id: str, workspace_id: str, job_id: str,
                       attempt: Mapping[str, object]) -> None:
        """A NON-terminal attempt (retryable provider failure, retries
        remaining): its row and its cost land in the ledger in one
        transaction, idempotent per `attempt_key`."""
        with self._tx() as cur:
            self._insert_attempt(cur, tenant_id, workspace_id, job_id, attempt)
            self._accrue_ledger(cur, tenant_id, workspace_id, job_id, settle=False)

    def finish_execution(self, tenant_id: str, workspace_id: str, job_id: str,
                         terminal: str, *, reason: str | None = None,
                         result: Mapping[str, object] | None = None,
                         attempt: Mapping[str, object] | None = None) -> str:
        """The ONE terminal transition of the execution axis.

        In a single transaction: records the attempt row (if any), applies
        the guarded `execution_status IN ('QUEUED','RUNNING')` -> `terminal`
        update, copies the attempt's metadata (model id, prompt/schema hash,
        request context, usage, cost, duration) onto the job, and SETTLES the
        job's cost-ledger row to the real sum of its attempt costs. If any
        statement fails (including the ledger settlement) nothing commits:
        the job can never be `COMPLETED`/`FAILED` with its cost unaccounted.

        Cancellation checkpoint: a `COMPLETED` request for a job whose
        cancel was requested while it was RUNNING ends `CANCELLED` on both
        axes instead (cost still settled). Returns the terminal state
        actually written; raises `StoreConflict` if already terminal.
        """
        if terminal not in ("COMPLETED", "FAILED", "CANCELLED"):
            raise ValueError("terminal must be COMPLETED, FAILED or CANCELLED")
        attempt = _safe_attempt(attempt) if attempt is not None else None
        reason = sanitize_reason(reason)                                    # AH-05: typed, bounded reason; no raw exception text on the job row
        with self._tx() as cur:
            cur.execute("""SELECT execution_status, cancel_requested_at FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s FOR UPDATE""",
                (tenant_id, workspace_id, job_id))
            row = cur.fetchone()
            if row is None:
                raise TenantScopeError("job not found")
            if row[0] not in _LIVE_EXECUTION:
                raise StoreConflict(
                    f"job execution cannot be {terminal.lower()} from its current state "
                    f"(execution_status={row[0]!r})")
            written = terminal
            if terminal == "COMPLETED" and row[1] is not None:
                written, reason = "CANCELLED", "cancel_requested_during_execution"
            if attempt is not None:
                self._insert_attempt(cur, tenant_id, workspace_id, job_id, attempt)
            usage = None
            if attempt is not None:
                usage = {"provider_calls": int(attempt.get("provider_calls") or 0),
                         "input_tokens": attempt.get("input_tokens"),
                         "output_tokens": attempt.get("output_tokens"),
                         "cost_estimate_usd": (str(attempt["cost_estimate_usd"])
                                               if attempt.get("cost_estimate_usd") is not None
                                               else None)}
            actual = self._accrue_ledger(cur, tenant_id, workspace_id, job_id, settle=True)
            a = attempt or {}
            cur.execute("""UPDATE v2_jobs SET execution_status=%s, execution_reason=%s,
                    execution_result=COALESCE(%s, execution_result),
                    execution_model_id=COALESCE(%s, execution_model_id),
                    execution_prompt_hash=COALESCE(%s, execution_prompt_hash),
                    execution_schema_hash=COALESCE(%s, execution_schema_hash),
                    execution_request_context=COALESCE(%s, execution_request_context),
                    execution_executor_id=COALESCE(%s, execution_executor_id),
                    execution_usage=COALESCE(%s, execution_usage),
                    execution_provider_calls=COALESCE(%s, execution_provider_calls),
                    execution_cost_usd=%s,
                    execution_duration_ms=COALESCE(%s, execution_duration_ms)
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                  AND execution_status IN ('QUEUED','RUNNING')""",
                (written, reason,
                 Json(dict(result)) if result is not None and written == "COMPLETED" else None,
                 a.get("model_id"), a.get("prompt_hash"), a.get("schema_hash"),
                 Json(dict(a["request_context"])) if a.get("request_context") else None,
                 a.get("executor_id"), Json(usage) if usage is not None else None,
                 a.get("provider_calls"), actual, a.get("duration_ms"),
                 tenant_id, workspace_id, job_id))
            if cur.rowcount != 1:
                raise StoreConflict("job execution changed concurrently")
            if written == "CANCELLED" and row[1] is not None:
                cur.execute("""UPDATE v2_jobs SET status='CANCELLED'
                    WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                      AND status IN ('QUEUED','PENDING_APPROVAL','APPROVED')""",
                    (tenant_id, workspace_id, job_id))
        return written

    def job_attempts(self, tenant_id: str, workspace_id: str, job_id: str) -> list[dict]:
        with self._tx() as cur:
            cur.execute("""SELECT attempt_key, outcome, reason, retryable, executor_id,
                    model_id, prompt_hash, schema_hash, request_context, request_excerpt,
                    provider_calls, input_tokens, output_tokens, cost_estimate_usd,
                    cost_usd, duration_ms
                FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                  AND outcome <> 'IN_FLIGHT_RESOLVED'   -- bookkeeping of the "provider call in flight" marker: replaced by the recorded attempt, cost 0
                ORDER BY recorded_at, attempt_key""", (tenant_id, workspace_id, job_id))
            cols = ("attempt_key", "outcome", "reason", "retryable", "executor_id",
                    "model_id", "prompt_hash", "schema_hash", "request_context",
                    "request_excerpt", "provider_calls", "input_tokens", "output_tokens",
                    "cost_estimate_usd", "cost_usd", "duration_ms")
            return [_safe_attempt(dict(zip(cols, r))) for r in cur.fetchall()]       # rows written before AH-05 are reduced on read too

    def release_outbox(self, tenant_id: str, workspace_id: str, event_id: int,
                       worker_id: str, token: int) -> None:
        """Hand a claimed event back WITHOUT consuming one of its bounded
        attempts (claim-time concurrency backpressure, not a failure)."""
        with self._tx() as cur:
            cur.execute("""UPDATE v2_outbox SET status='PENDING', lease_owner=NULL,
                lease_until=NULL, attempts=GREATEST(attempts-1, 0)
                WHERE tenant_id=%s AND workspace_id=%s AND id=%s AND lease_owner=%s
                  AND fencing_token=%s AND status='CLAIMED' AND lease_until > now()""",
                (tenant_id, workspace_id, event_id, worker_id, token))
            if cur.rowcount != 1:
                raise LeaseLost("outbox lease lost")

    def defer_outbox(self, tenant_id: str, workspace_id: str, event_id: int, worker_id: str, token: int, *,
                     seconds: int) -> None:
        """Keep a claimed event leased for `seconds` more WITHOUT consuming one of its bounded attempts (an approval
        hold, not a failure). Other events stay claimable meanwhile; when the lease expires the event is reclaimed, which
        gives the attempt back (net zero)."""
        with self._tx() as cur:
            cur.execute("""UPDATE v2_outbox SET lease_until=now()+(%s * interval '1 second'),
                attempts=GREATEST(attempts-1, 0)
                WHERE tenant_id=%s AND workspace_id=%s AND id=%s AND lease_owner=%s
                  AND fencing_token=%s AND status='CLAIMED' AND lease_until > now()""",
                (seconds, tenant_id, workspace_id, event_id, worker_id, token))
            if cur.rowcount != 1:
                raise LeaseLost("outbox lease lost")

    def complete_job(self, tenant_id: str, workspace_id: str, job_id: str, *,
                     result: Mapping[str, object] | None = None,
                     model_id: str | None = None, prompt_hash: str | None = None,
                     usage: Mapping[str, object] | None = None,
                     cost_usd: object | None = None,
                     duration_ms: int | None = None) -> None:
        """Round-5 interface, kept for callers/tests: now a thin wrapper over
        `finish_execution` (accepts `QUEUED` or `RUNNING`). The worker
        itself calls `finish_execution` with a full attempt record."""
        attempt = None
        if any(v is not None for v in (model_id, prompt_hash, usage, cost_usd, duration_ms)):
            u = dict(usage or {})
            attempt = {"attempt_key": f"direct:{job_id}", "outcome": "COMPLETED",
                       "model_id": model_id, "prompt_hash": prompt_hash,
                       "request_context": {"job_id": job_id, "caller": "complete_job"},
                       "provider_calls": u.get("provider_calls", 0),
                       "input_tokens": u.get("input_tokens"),
                       "output_tokens": u.get("output_tokens"),
                       "cost_usd": cost_usd if cost_usd is not None else 0,
                       "duration_ms": duration_ms}
        written = self.finish_execution(tenant_id, workspace_id, job_id, "COMPLETED",
                                        result=result, attempt=attempt)
        if written != "COMPLETED":
            raise StoreConflict("job execution was cancelled before completion")

    def fail_execution(self, tenant_id: str, workspace_id: str, job_id: str,
                       reason: str) -> None:
        """Round-5 interface: terminal `FAILED` (distinct from `CANCELLED`)."""
        self.finish_execution(tenant_id, workspace_id, job_id, "FAILED", reason=reason)

    def cancel_execution(self, tenant_id: str, workspace_id: str, job_id: str,
                         reason: str) -> None:
        """Round-5 interface: terminal `CANCELLED` on the execution axis only
        (the approval `status` is left untouched unless a cancel was
        requested, see `finish_execution`)."""
        self.finish_execution(tenant_id, workspace_id, job_id, "CANCELLED", reason=reason)

    _quota_table_ready = False

    @classmethod
    def _account_day_quota(cls, cur, tenant_id: str, kind: str, account: str | None) -> None:
        """Daily AI-job allowance per ACCOUNT (0 / unset = no limit, so local tools and tests are unchanged). Counted inside the job-creation transaction: a refused or rolled-back
        creation never uses the allowance. System-created stage jobs have no requester and are not counted. Fail closed: a counter error refuses the AI start (the transaction fails)."""
        try:
            limit = int(os.environ.get("RIPPLE_AI_JOBS_PER_ACCOUNT_DAY", "0"))
        except ValueError:
            limit = 0
        if limit <= 0 or not account:
            return
        from infra.provider_gate import AI_KINDS
        if kind not in AI_KINDS:
            return
        if not cls._quota_table_ready:
            cur.execute("""CREATE TABLE IF NOT EXISTS v2_account_ai_quota (tenant_id TEXT NOT NULL, account_id TEXT NOT NULL, day DATE NOT NULL, jobs INTEGER NOT NULL,
                PRIMARY KEY (tenant_id, account_id, day))""")
            cls._quota_table_ready = True
        cur.execute("""INSERT INTO v2_account_ai_quota (tenant_id, account_id, day, jobs) VALUES (%s,%s,(now() AT TIME ZONE 'UTC')::date,1)
            ON CONFLICT (tenant_id, account_id, day) DO UPDATE SET jobs = v2_account_ai_quota.jobs + 1 RETURNING jobs""", (tenant_id, account))
        used = cur.fetchone()[0]
        if used > limit:
            raise PersonQuotaExceeded(f"this account started {used - 1} of {limit} AI jobs for today (UTC day)")

    def request_cancel(self, tenant_id: str, workspace_id: str, job_id: str,
                       reason: str) -> None:
        """An operator-issued cancel request (via authenticated,
        server-side permission/scope-checked routes). Allowed only while the job has not yet published and
        has not yet finished executing (`status` still pre-publish AND
        `execution_status` still `QUEUED`) -- a job that already committed/
        published must never be cancelled as if it had never executed. Sets a checkpoint (`cancel_requested_at`) the
        worker consults before any costly action, AND immediately marks the
        job cancelled on both axes when it has not started executing yet, so
        a cancel of a job still waiting in the queue is observable right
        away rather than only at the worker's next poll.
        """
        with self._tx() as cur:
            cur.execute("""UPDATE v2_jobs SET cancel_requested_at=now()
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                  AND status IN ('QUEUED','PENDING_APPROVAL','APPROVED')
                  AND execution_status IN ('QUEUED','RUNNING')""",
                (tenant_id, workspace_id, job_id))
            if cur.rowcount != 1:
                raise StoreConflict(
                    "job cannot be cancelled: already published, or execution already finished")
            cur.execute("""UPDATE v2_jobs SET status='CANCELLED',
                execution_status='CANCELLED', execution_reason=%s
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                  AND status IN ('QUEUED','PENDING_APPROVAL','APPROVED')
                  AND execution_status='QUEUED'""",
                (reason, tenant_id, workspace_id, job_id))
            if cur.rowcount == 1:
                # found on the cloud candidate: a job cancelled before it ever ran kept its worst-case RESERVATION open (0.44 USD held against the cap for nothing).
                # It has no attempts, so settling it records its real cost: 0.
                self._accrue_ledger(cur, tenant_id, workspace_id, job_id, settle=True)

    def reconcile_terminal_reservations(self, tenant_id: str, workspace_id: str, *, actor_id: str, approval_ref: str = "", apply: bool = False) -> list[dict]:
        """Operator reconcile of RESERVATIONS that no longer have a reason to exist: a ledger row still RESERVED although its job is terminal (CANCELLED / FAILED / COMPLETED).
        It only SETTLES the row to the real sum of the job's recorded attempt costs (the same routine every normal finish uses): a charge that happened is kept to the cent, only
        the unused part of the reservation is released. Nothing is deleted and no cost is typed in by hand. A job still QUEUED/RUNNING is never touched. Idempotent: a second run
        finds nothing. `apply=False` only reports. An applied run writes ONE audit row naming the jobs and the before/after amounts."""
        out: list[dict] = []
        with self._tx() as cur:
            cur.execute("""SELECT l.job_id, j.kind, j.execution_status, l.cost_usd, l.reserved_usd,
                           (SELECT COALESCE(SUM(a.cost_usd),0) FROM v2_job_attempts a WHERE a.tenant_id=l.tenant_id AND a.workspace_id=l.workspace_id AND a.job_id=l.job_id)
                FROM v2_job_cost_ledger l JOIN v2_jobs j ON j.tenant_id=l.tenant_id AND j.workspace_id=l.workspace_id AND j.job_id=l.job_id
                WHERE l.tenant_id=%s AND l.workspace_id=%s AND l.entry_state='RESERVED' AND j.execution_status IN ('CANCELLED','FAILED','COMPLETED')
                ORDER BY l.job_id FOR UPDATE OF l""", (tenant_id, workspace_id))
            for job_id, kind, status, held, reserved, actual in cur.fetchall():
                out.append({"job_id": job_id, "kind": kind, "execution_status": status, "held_usd": str(held), "settles_to_usd": str(actual)})
            if apply and out:
                for row in out:
                    self._accrue_ledger(cur, tenant_id, workspace_id, row["job_id"], settle=True)
                cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'ledger.reconcile_reservations',%s,%s)""",
                            (tenant_id, workspace_id, actor_id, Json({"approval_ref": approval_ref[:300], "jobs": out})))
        return out

    def retry_job(self, tenant_id: str, workspace_id: str, job_id: str, *,
                  max_concurrent_jobs: int | None = None,
                  budget_reservation: tuple[object, object] | None = None,
                  max_provider_attempts: int | None = None) -> None:
        """Retry via authenticated,
        server-side permission/scope-checked routes. Only a job whose
        EXECUTION genuinely `FAILED` (bounded retries already exhausted by the
        worker) may be retried -- never a `CANCELLED` (refused, not a
        transient failure) or already-`COMPLETED`/still-`QUEUED` job (retry
        of a job that never reached a terminal execution outcome is refused
        as "too early": the mirror image of an "operation attempted too late"
        error, i.e. the symmetric "too early/wrong state" refusal). Re-enqueuing means
        resetting `execution_status` back to `QUEUED` and inserting a FRESH
        `JOB_QUEUED` outbox event so the worker's own idempotency/fencing
        machinery claims it exactly like any other queued job -- never
        re-using the original, already-acked outbox row.
        """
        with self._tx() as cur:
            # Match create_job's lock order. The state change, fresh budget
            # reservation, and outbox event either all commit or all roll back.
            self._lock_head(cur, tenant_id, workspace_id)
            cur.execute("""SELECT kind, status, execution_status, cancel_requested_at
                FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                FOR UPDATE""", (tenant_id, workspace_id, job_id))
            row = cur.fetchone()
            if row is None:
                raise TenantScopeError("job not found")
            kind, status, execution_status, cancel_requested_at = row
            if (execution_status != "FAILED" or status not in
                    ("QUEUED", "PENDING_APPROVAL", "APPROVED")
                    or cancel_requested_at is not None):
                raise StoreConflict("job can only be retried while its execution has genuinely FAILED")

            if max_provider_attempts is not None:
                # Refuse here, before any reservation, outbox row or queue state: the worker would refuse the same job at once
                # (`provider_attempt_limit_reached`), so accepting the retry would only promise something that cannot happen.
                cur.execute("""SELECT count(*) FROM v2_job_attempts
                    WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND provider_calls > 0""", (tenant_id, workspace_id, job_id))
                used = int(cur.fetchone()[0])
                if used >= max_provider_attempts:
                    raise JobAttemptsExhausted(used, max_provider_attempts)

            if max_concurrent_jobs is not None:
                cur.execute("""SELECT count(*) FROM v2_jobs
                    WHERE tenant_id=%s AND workspace_id=%s
                      AND execution_status IN ('QUEUED','RUNNING')""",
                    (tenant_id, workspace_id))
                running = cur.fetchone()[0]
                if running >= max_concurrent_jobs:
                    raise ConcurrencyLimitExceeded(
                        f"{tenant_id}/{workspace_id}: {running} jobs already running, "
                        f"at or above the configured concurrency limit {max_concurrent_jobs}")

            if budget_reservation is not None:
                estimate_usd, total_cap_usd = budget_reservation
                cur.execute("""SELECT project_id FROM v2_workspaces
                    WHERE tenant_id=%s AND workspace_id=%s""", (tenant_id, workspace_id))
                project_id = cur.fetchone()[0]
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                            (f"v2-project-budget:{tenant_id}:{project_id}",))
                cur.execute(PROJECT_SPEND_SQL, project_spend_params(tenant_id, workspace_id))
                spent = cur.fetchone()[0]
                if spent + estimate_usd > total_cap_usd:
                    from infra.worker.budget_config import BudgetExceeded
                    raise BudgetExceeded(scope=f"{tenant_id}/{project_id}:total",
                                        cap=total_cap_usd, spent=spent,
                                        estimate=estimate_usd)
                # A failed execution's previous attempts were already settled.
                # Hold one NEW attempt budget above that settled amount; the
                # existing ledger key still deduplicates all of this job's
                # attempts. _accrue_ledger preserves reserved_usd as the floor
                # until the next terminal transition settles their real sum.
                cur.execute("""UPDATE v2_job_cost_ledger SET
                        cost_usd=cost_usd+%s, reserved_usd=cost_usd+%s,
                        entry_state='RESERVED', settled_at=NULL
                    WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                      AND entry_state='SETTLED'""",
                    (str(estimate_usd), str(estimate_usd), tenant_id, workspace_id, job_id))
                if cur.rowcount != 1:
                    raise StoreConflict("failed job has no settled cost ledger row")

            cur.execute("""UPDATE v2_jobs SET execution_status='QUEUED',
                execution_reason=NULL
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                  AND execution_status='FAILED'""",
                (tenant_id, workspace_id, job_id))
            if cur.rowcount != 1:
                raise StoreConflict("job execution changed during retry")
            cur.execute("""INSERT INTO v2_outbox
                (tenant_id, workspace_id, kind, payload)
                VALUES (%s,%s,'JOB_QUEUED',%s)""",
                (tenant_id, workspace_id, Json({"job_id": job_id, "kind": kind})))

    def recheck_and_publish_job(self, tenant_id: str, workspace_id: str, job_id: str,
                                required_permission: str | None) -> None:
        """Recheck the recorded approver's CURRENT authority and publish, atomically.

        This is the permanent publish/authority-recheck interface:
        the job row,
        the approver's account row and the approver's membership row are all
        locked with `FOR UPDATE` inside ONE transaction that also performs the
        final `PUBLISHED` write. This makes the transaction conflict, at the
        row level, with a concurrent `PostgresIdentityStore.grant_membership`,
        `revoke_membership` or `revoke_credential` on the same account or
        membership row:

        - A revoke that already committed before this transaction starts is
          observed directly here (missing/disabled/expired account, missing
          membership, or a role set that no longer grants `required_permission`
          -> `StoreConflict`, job stays `APPROVED`, never `PUBLISHED`).
        - A revoke issued concurrently, after this transaction has taken its
          row locks, is forced to BLOCK on those locks until this transaction
          commits or rolls back -- there is no window in which a stale
          authority snapshot and the final publish write can straddle an
          in-flight revoke, unlike the previous separate-transaction publish.

        Callers: `infra/v2_policy_routes.py::publish_job`.
        """
        if required_permission is None:
            raise StoreConflict("unsupported job kind")
        with self._tx() as cur:
            cur.execute("""SELECT status, approved_by, execution_status FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s FOR UPDATE""",
                (tenant_id, workspace_id, job_id))
            row = cur.fetchone()
            if row is None:
                raise TenantScopeError("job not found")
            status, approved_by, execution_status = row
            if status != "APPROVED":
                raise StoreConflict("job is not approved")
            # Publish must check that a valid execution result exists and a
            # cancellation checkpoint. A job whose worker execution never
            # completed (still QUEUED/RUNNING), genuinely FAILED, or was
            # CANCELLED (budget/quota refusal, or an operator cancel) must
            # never be published -- a human approving an INTENT to run a job
            # is not the same as the job having actually produced a valid,
            # completed result to publish.
            if execution_status != "COMPLETED":
                raise StoreConflict(
                    f"job execution has not completed (execution_status={execution_status!r}); "
                    f"publish refused")
            cur.execute("""SELECT disabled_at,
                    (credential_expires_at IS NOT NULL AND credential_expires_at <= now())
                FROM v2_accounts WHERE account_id=%s FOR UPDATE""", (approved_by,))
            account_row = cur.fetchone()
            if account_row is None or account_row[0] is not None or account_row[1]:
                raise StoreConflict("approver authority revoked; publish blocked")
            cur.execute("""SELECT roles FROM v2_memberships
                WHERE tenant_id=%s AND workspace_id=%s AND account_id=%s FOR UPDATE""",
                (tenant_id, workspace_id, approved_by))
            membership_row = cur.fetchone()
            if membership_row is None:
                raise StoreConflict("approver authority revoked; publish blocked")
            if required_permission not in permissions_for(membership_row[0]):
                raise StoreConflict(
                    "approver no longer holds the required permission; publish blocked")
            cur.execute("""UPDATE v2_jobs SET status='PUBLISHED'
                WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND status='APPROVED'""",
                (tenant_id, workspace_id, job_id))
            if cur.rowcount != 1:
                raise StoreConflict("job is not approved")

    def set_workspace_policy(self, tenant_id: str, workspace_id: str, *,
                             require_distinct_approver: bool) -> None:
        with self._tx() as cur:
            cur.execute("""UPDATE v2_workspaces SET require_distinct_approver=%s
                WHERE tenant_id=%s AND workspace_id=%s""",
                (require_distinct_approver, tenant_id, workspace_id))
            if cur.rowcount != 1:
                raise TenantScopeError("workspace not found")

    def workspace_policy(self, tenant_id: str, workspace_id: str) -> dict:
        with self._tx() as cur:
            cur.execute("""SELECT require_distinct_approver FROM v2_workspaces
                WHERE tenant_id=%s AND workspace_id=%s""", (tenant_id, workspace_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("workspace not found")
        return {"require_distinct_approver": row[0]}

    def record_index_manifest(self, tenant_id: str, workspace_id: str,
                              knowledge_revision_id: str, index_version: str,
                              manifest_hash: str, status: str, *,
                              idempotency_key: str) -> PublishResult:
        if status not in {"BUILDING", "READY", "FAILED"}:
            raise ValueError("invalid index status")
        operation = "index.record"
        payload = {"knowledge_revision_id": knowledge_revision_id,
                   "index_version": index_version, "manifest_hash": manifest_hash,
                   "status": status}
        request_hash = canonical_hash(payload)
        with self._tx() as cur:
            head = self._lock_head(cur, tenant_id, workspace_id)
            prior = self._replay(cur, tenant_id, workspace_id, operation,
                                 idempotency_key, request_hash)
            if prior is not None:
                return prior
            if head.knowledge_revision_id != knowledge_revision_id:
                raise StaleVersion("index targets stale knowledge")
            cur.execute("""SELECT status, manifest_hash FROM v2_index_manifests
                WHERE tenant_id=%s AND workspace_id=%s
                  AND knowledge_revision_id=%s AND index_version=%s""",
                (tenant_id, workspace_id, knowledge_revision_id, index_version))
            existing = cur.fetchone()
            if existing is None:
                cur.execute("""INSERT INTO v2_index_manifests
                    (tenant_id, workspace_id, knowledge_revision_id,
                     index_version, status, manifest_hash)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, knowledge_revision_id,
                     index_version, status, manifest_hash))
            else:
                if existing[1] != manifest_hash:
                    raise StoreConflict("index version manifest hash cannot change")
                if existing[0] == "READY" and status != "READY":
                    raise StoreConflict("ready index version cannot regress")
                cur.execute("""UPDATE v2_index_manifests SET status=%s
                    WHERE tenant_id=%s AND workspace_id=%s
                      AND knowledge_revision_id=%s AND index_version=%s""",
                    (status, tenant_id, workspace_id,
                     knowledge_revision_id, index_version))
            result = PublishResult(head.version, knowledge_revision_id)
            cur.execute("""INSERT INTO v2_idempotency
                (tenant_id, workspace_id, operation, idempotency_key,
                 payload_hash, result) VALUES (%s,%s,%s,%s,%s,%s)""",
                (tenant_id, workspace_id, operation, idempotency_key,
                 request_hash, Json({"version": result.version,
                                     "revision_id": result.revision_id})))
            cur.execute("""INSERT INTO v2_audit
                (tenant_id, workspace_id, event_type, actor_id, payload)
                VALUES (%s,%s,'INDEX_RECORDED','service',%s)""",
                (tenant_id, workspace_id, Json(payload)))
            return result

    def current_index_ready(self, tenant_id: str, workspace_id: str,
                            index_version: str) -> bool:
        with self._tx() as cur:
            cur.execute("""SELECT w.knowledge_revision_id, i.status
                FROM v2_workspaces AS w LEFT JOIN v2_index_manifests AS i
                  ON i.tenant_id=w.tenant_id AND i.workspace_id=w.workspace_id
                 AND i.knowledge_revision_id=w.knowledge_revision_id
                 AND i.index_version=%s
                WHERE w.tenant_id=%s AND w.workspace_id=%s""",
                (index_version, tenant_id, workspace_id))
            row = cur.fetchone()
            if row is None:
                raise TenantScopeError("workspace not found")
            return row[0] is not None and row[1] == "READY"

    def active_decision_action(self, tenant_id: str, workspace_id: str,
                               target: str) -> str | None:
        """Read-only helper for the acceptance-gate blocker check.
        Reuses `record_human_decision`'s own append-only "active decision"
        derivation (a decision is active unless some LATER decision's
        `supersedes`/`revokes` names it -- `v2_decisions` never mutates rows,
        by the append-only design) instead of re-implementing an
        equivalent query. Returns the active decision's `action`
        ('confirm'/'resolve'/'accepted_unknown'/...) for `target`, or `None`
        if no active decision exists for it yet."""
        with self._tx() as cur:
            cur.execute("""SELECT d.action FROM v2_decisions AS d
                WHERE d.tenant_id=%s AND d.workspace_id=%s AND d.target=%s
                  AND NOT EXISTS (
                    SELECT 1 FROM v2_decisions AS s
                    WHERE s.tenant_id=d.tenant_id AND s.workspace_id=d.workspace_id
                      AND (s.supersedes=d.decision_id OR s.revokes=d.decision_id))
                ORDER BY d.epoch DESC LIMIT 1""",
                (tenant_id, workspace_id, target))
            row = cur.fetchone()
        return row[0] if row is not None else None

    def list_critical_findings(self, tenant_id: str, workspace_id: str) -> list[dict]:
        """Reuses the SAME scan `find_finding_by_id` already does
        (every COMPLETED 'finding' job's `execution_result.findings[]`), but
        returns every finding whose real, persisted `severity` is
        HIGH/CRITICAL and `authority_required` is true -- the source list
        for `GET /v2/knowledge/blockers`. Never a second, parallel
        criticality mechanism (as noted on `find_finding_by_id`)."""
        from infra import v2_current_findings as CF
        with self._tx() as cur:
            model = CF.build(cur, tenant_id, workspace_id)
            cur.execute("""SELECT execution_result FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND kind='finding'
                  AND execution_status='COMPLETED' AND execution_result IS NOT NULL""",
                (tenant_id, workspace_id))
            rows = cur.fetchall()
        out = []
        for (raw_result,) in rows:
            result = _as_object(raw_result) or {}
            for finding in (result.get("findings") or []):
                if not isinstance(finding, dict):
                    continue
                info = model.by_finding_id.get(finding.get("finding_id"))
                if info is not None and info["role"] != "representative":
                    continue                                       # a repeat of another run's finding, or history: counted once, as the shared read model says
                if finding.get("severity") in ("HIGH", "CRITICAL") and finding.get(
                        "authority_required"):
                    out.append(finding)
        return out

    SCOPE_MISMATCH_RECORD_CAP = 200

    def list_scope_comparisons(self, tenant_id: str, workspace_id: str) -> dict:
        """Read-only copy of the `scope_comparisons` records (non-conflict scope_mismatch notes) stored in every
        COMPLETED 'finding' job of the workspace, for the verification report. Same scan as `list_critical_findings`;
        no lifecycle, not a finding, never a blocker. Deterministic order (sorted claim ids, then job id), bounded to
        SCOPE_MISMATCH_RECORD_CAP records (`total`/`truncated` say so). Only ids and relation labels are copied."""
        with self._tx() as cur:
            cur.execute("""SELECT job_id, execution_result FROM v2_jobs
                WHERE tenant_id=%s AND workspace_id=%s AND kind='finding'
                  AND execution_status='COMPLETED' AND execution_result IS NOT NULL
                ORDER BY job_id""", (tenant_id, workspace_id))
            rows = cur.fetchall()
        records = []
        for job_id, raw_result in rows:
            for rec in ((_as_object(raw_result) or {}).get("scope_comparisons") or []):
                if isinstance(rec, dict) and isinstance(rec.get("claim_ids"), list):
                    records.append({"finding_job_id": job_id, "kind": rec.get("kind"),
                                    "claim_ids": sorted(str(c) for c in rec["claim_ids"]),
                                    "scope_relation": rec.get("scope_relation"), "reason": rec.get("reason"),
                                    "conflict": bool(rec.get("conflict"))})
        records.sort(key=lambda r: (r["claim_ids"], r["finding_job_id"], str(r["scope_relation"])))
        cap = self.SCOPE_MISMATCH_RECORD_CAP
        return {"records": records[:cap], "total": len(records), "truncated": len(records) > cap}

    def active_gap_clarifications(self, tenant_id: str, workspace_id: str, intent_id: str,
                                  max_epoch: int | None) -> list[dict]:
        """Active (not superseded/revoked) human CONFIRM decisions on `gap:` targets whose scope names `intent_id`, recorded
        at or before `max_epoch`. Read-only; the draft executor gives exactly these to the model as stated assumptions."""
        with self._tx() as cur:
            cur.execute("""SELECT decision_id, epoch, actor_id, target, rationale, scope FROM v2_decisions
                WHERE tenant_id=%s AND workspace_id=%s AND action='confirm' AND target LIKE 'gap:%%'
                  AND NOT EXISTS (SELECT 1 FROM v2_decisions AS s WHERE s.tenant_id=v2_decisions.tenant_id AND s.workspace_id=v2_decisions.workspace_id
                                  AND (s.supersedes=v2_decisions.decision_id OR s.revokes=v2_decisions.decision_id))
                  AND scope->>'kind'='draft_gap_answer' AND scope->>'intent_id'=%s
                  AND (%s::bigint IS NULL OR epoch <= %s::bigint)
                ORDER BY epoch""", (tenant_id, workspace_id, intent_id, max_epoch, max_epoch))
            rows = cur.fetchall()
        return [{"decision_id": r[0], "epoch": r[1], "actor_id": r[2], "target": r[3], "rationale": r[4],
                 "scope": r[5] if isinstance(r[5], dict) else json.loads(r[5])} for r in rows]

    def active_knowledge_confirmations(self, tenant_id: str, workspace_id: str, targets, max_epoch) -> list[dict]:
        """Read-only: ACTIVE human `confirm` decisions (not superseded, not revoked) on the given knowledge targets (candidate ids), recorded at or
        before `max_epoch`, with who/when. Used to tell the Q&A model what people have confirmed; it never writes."""
        targets = sorted(set(targets or ()))
        if not targets:
            return []
        with self._tx() as cur:
            cur.execute("""SELECT decision_id, epoch, actor_id, target, rationale, created_at FROM v2_decisions
                WHERE tenant_id=%s AND workspace_id=%s AND action='confirm' AND target = ANY(%s) AND target NOT LIKE 'gap:%%'
                  AND NOT EXISTS (SELECT 1 FROM v2_decisions AS s WHERE s.tenant_id=v2_decisions.tenant_id AND s.workspace_id=v2_decisions.workspace_id
                                  AND (s.supersedes=v2_decisions.decision_id OR s.revokes=v2_decisions.decision_id))
                  AND (%s::bigint IS NULL OR epoch <= %s::bigint)
                ORDER BY epoch""", (tenant_id, workspace_id, targets, max_epoch, max_epoch))
            return [{"decision_id": r[0], "epoch": r[1], "actor_id": r[2], "target": r[3], "rationale": r[4],
                     "created_at": r[5].isoformat() if r[5] is not None else None} for r in cur.fetchall()]

    def source_revision_ids(self, tenant_id: str, workspace_id: str) -> list[dict]:
        """Read-only: the source revisions of a workspace, oldest first (id, parent, created_at)."""
        with self._tx() as cur:
            cur.execute("""SELECT revision_id, parent_revision_id, created_at FROM v2_source_revisions
                WHERE tenant_id=%s AND workspace_id=%s ORDER BY created_at ASC, revision_id ASC""", (tenant_id, workspace_id))
            return [{"source_revision_id": r[0], "parent_revision_id": r[1], "created_at": r[2].isoformat() if r[2] is not None else None}
                    for r in cur.fetchall()]

    def knowledge_revisions(self, tenant_id: str, workspace_id: str) -> list[dict]:
        """Revision-comparison/history read surface. INSERT-only
        `v2_knowledge_revisions` (no UPDATE/DELETE path anywhere in
        `publish_knowledge`), ordered oldest-first by `created_at`, so a
        caller can reconstruct real ancestry/epoch/source-per-revision
        without trusting a caller-supplied correlation."""
        with self._tx() as cur:
            cur.execute("""SELECT revision_id, source_revision_id, decision_epoch,
                    created_at FROM v2_knowledge_revisions
                WHERE tenant_id=%s AND workspace_id=%s
                ORDER BY created_at ASC, revision_id ASC""",
                (tenant_id, workspace_id))
            rows = cur.fetchall()
        return [{"revision_id": r[0], "source_revision_id": r[1],
                "decision_epoch": r[2],
                "created_at": r[3].isoformat() if r[3] is not None else None}
               for r in rows]

    def claim_outbox(self, tenant_id: str, workspace_id: str, worker_id: str, *,
                     lease_seconds: int = 30, skip_job_kinds: tuple = ()) -> OutboxClaim | None:
        if not all(isinstance(x, str) and x.strip()
                   for x in (tenant_id, workspace_id, worker_id)) or lease_seconds <= 0:
            raise ValueError("scope, worker and positive lease required")
        with self._tx() as cur:
            cur.execute("""SELECT id, tenant_id, workspace_id, kind, payload,
                fencing_token, attempts FROM v2_outbox
                WHERE tenant_id=%s AND workspace_id=%s
                  AND (status='PENDING' OR (status='CLAIMED' AND lease_until < now()))
                  AND attempts < 5
                  AND NOT (kind='JOB_QUEUED' AND payload->>'kind' = ANY(%s))
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1""",
                (tenant_id, workspace_id, list(skip_job_kinds)))
            row = cur.fetchone()
            if row is None:
                return None
            token = row[5] + 1
            attempts_after = row[6] + 1
            cur.execute("""UPDATE v2_outbox SET status='CLAIMED', lease_owner=%s,
                lease_until=now()+(%s * interval '1 second'),
                fencing_token=%s, attempts=attempts+1 WHERE id=%s""",
                (worker_id, lease_seconds, token, row[0]))
            return OutboxClaim(row[0], row[1], row[2], row[3],
                               _as_object(row[4]), token, attempts_after)

    def heartbeat_outbox(self, tenant_id: str, workspace_id: str,
                         event_id: int, worker_id: str, token: int, *,
                         lease_seconds: int = 30) -> None:
        with self._tx() as cur:
            cur.execute("""UPDATE v2_outbox SET
                lease_until=now()+(%s * interval '1 second')
                WHERE tenant_id=%s AND workspace_id=%s AND id=%s
                  AND lease_owner=%s AND fencing_token=%s
                  AND status='CLAIMED' AND lease_until > now()""",
                (lease_seconds, tenant_id, workspace_id, event_id, worker_id, token))
            if cur.rowcount != 1:
                raise LeaseLost("outbox lease lost")

    def ack_outbox(self, tenant_id: str, workspace_id: str,
                   event_id: int, worker_id: str, token: int) -> None:
        with self._tx() as cur:
            cur.execute("""UPDATE v2_outbox SET status='DONE', lease_owner=NULL,
                lease_until=NULL WHERE tenant_id=%s AND workspace_id=%s
                  AND id=%s AND lease_owner=%s
                  AND fencing_token=%s AND status='CLAIMED'
                  AND lease_until > now()""",
                (tenant_id, workspace_id, event_id, worker_id, token))
            if cur.rowcount != 1:
                raise LeaseLost("outbox lease lost")

    # ------------------------------------------------------------------ intent

    def parse_intent(self, tenant_id: str, workspace_id: str, *, actor_id: str,
                     idempotency_key: str, raw_request: str | None = None,
                     structured_form: Mapping | None = None,
                     target_hint: str | None = None) -> dict:
        """`intent.parse`: pins the CURRENT source/knowledge/decision snapshot
        (`self.head`, read inside this same transaction -- not locked, since
        parse never mutates the workspace head) alongside a deterministic
        parse of `raw_request` or `structured_form`
        (`core.change_intelligence.v2.intent.parse_v2_intent` -- never both
        a model call AND a store write in the same step; this store method
        never calls a provider). Writes exactly one new `v2_intents` row in
        status PARSED/NEEDS_CLARIFICATION; never touches `v2_workspaces`,
        `v2_source_revisions` or `v2_knowledge_revisions` (source/KM are never modified by an
        intent). Idempotent by (tenant,
        workspace, "intent.parse", idempotency_key) via the same generic
        `v2_idempotency` table every other mutating method uses -- a retried
        key with the SAME request payload returns the SAME intent_id; a
        different payload under the same key is `StoreConflict`.
        """
        if not all(isinstance(x, str) and x.strip()
                  for x in (tenant_id, workspace_id, actor_id, idempotency_key)):
            raise ValueError("tenant, workspace, actor and idempotency key required")
        if not raw_request and not structured_form:
            raise ValueError("raw_request or structured_form required")
        structured_dict = dict(structured_form) if structured_form else None
        parsed = parse_v2_intent(raw_request=raw_request,
                                 structured_form=structured_dict,
                                 target_hint=target_hint)
        payload = {"raw_request": raw_request, "structured_form": structured_dict,
                  "target_hint": target_hint}
        payload_hash = canonical_hash(payload)
        operation = "intent.parse"
        with self._tx() as cur:
            cur.execute("""SELECT payload_hash, result FROM v2_idempotency
                WHERE tenant_id=%s AND workspace_id=%s AND operation=%s
                  AND idempotency_key=%s""",
                (tenant_id, workspace_id, operation, idempotency_key))
            row = cur.fetchone()
            if row is not None:
                if row[0] != payload_hash:
                    raise StoreConflict("idempotency_conflict")
                return _as_object(row[1])
            head = self.head(tenant_id, workspace_id)
            intent_id = "intent-" + uuid.uuid4().hex
            cur.execute("""INSERT INTO v2_intents
                (tenant_id, workspace_id, intent_id, status, raw_request,
                 structured_form, target, value, scope, exclusions,
                 effective_time, questions, unsupported_reason,
                 injection_detected, parsed_source_revision_id,
                 parsed_knowledge_revision_id, parsed_decision_epoch,
                 parsed_version, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, workspace_id, intent_id, parsed["status"], raw_request,
                 Json(structured_dict) if structured_dict is not None else None,
                 parsed["target"],
                 Json(parsed["value"]) if parsed["value"] is not None else None,
                 Json(parsed["scope"]) if parsed["scope"] is not None else None,
                 Json(parsed["exclusions"]),
                 Json(parsed["effective_time"]) if parsed["effective_time"] else None,
                 Json(parsed["questions"]), parsed["unsupported_reason"],
                 parsed["injection_detected"], head.source_revision_id,
                 head.knowledge_revision_id, head.decision_epoch, head.version,
                 actor_id))
            result = {
                "intent_id": intent_id, "status": parsed["status"],
                "target": parsed["target"], "value": parsed["value"],
                "currency": parsed["currency"], "scope": parsed["scope"],
                "exclusions": parsed["exclusions"],
                "effective_time": parsed["effective_time"],
                "questions": parsed["questions"],
                "unsupported_reason": parsed["unsupported_reason"],
                "injection_detected": parsed["injection_detected"],
                "parsed_source_revision_id": head.source_revision_id,
                "parsed_knowledge_revision_id": head.knowledge_revision_id,
                "parsed_decision_epoch": head.decision_epoch,
            }
            cur.execute("""INSERT INTO v2_idempotency
                (tenant_id, workspace_id, operation, idempotency_key, payload_hash, result)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (tenant_id, workspace_id, operation, idempotency_key, payload_hash,
                 Json(result)))
            cur.execute("""INSERT INTO v2_audit
                (tenant_id, workspace_id, event_type, actor_id, payload)
                VALUES (%s,%s,'INTENT_PARSED',%s,%s)""",
                (tenant_id, workspace_id, actor_id,
                 Json({"intent_id": intent_id, "status": parsed["status"]})))
            return result

    def get_intent(self, tenant_id: str, workspace_id: str, intent_id: str) -> dict:
        """Stored-intent read.
        Raises `IntentNotFound` for an absent/foreign intent_id -- never
        reveals a cross-tenant/cross-workspace row (the query is itself
        scoped by tenant_id/workspace_id)."""
        with self._tx() as cur:
            cur.execute("""SELECT intent_id, status, raw_request, target, value,
                scope, exclusions, effective_time, questions, unsupported_reason,
                injection_detected, parsed_source_revision_id,
                parsed_knowledge_revision_id, parsed_decision_epoch,
                confirmed_actor_id, digest, stale_reasons, structured_form, created_by
                FROM v2_intents WHERE tenant_id=%s AND workspace_id=%s
                  AND intent_id=%s""",
                (tenant_id, workspace_id, intent_id))
            row = cur.fetchone()
        if row is None:
            raise IntentNotFound(intent_id)
        (iid, status, raw_request, target, value, scope, exclusions, effective_time,
         questions, unsupported_reason, injection_detected, parsed_source, parsed_km,
         parsed_epoch, confirmed_actor_id, digest, stale_reasons, structured_form, created_by) = row
        return {
            "intent_id": iid, "status": status, "raw_request": raw_request,
            "target": target, "value": _as_object(value), "scope": _as_object(scope),
            "exclusions": _as_object(exclusions) or [],
            "effective_time": _as_object(effective_time),
            "questions": _as_object(questions) or [],
            "unsupported_reason": unsupported_reason,
            "injection_detected": injection_detected,
            "parsed_source_revision_id": parsed_source,
            "parsed_knowledge_revision_id": parsed_km,
            "parsed_decision_epoch": parsed_epoch,
            "confirmed_actor_id": confirmed_actor_id, "digest": digest,
            "stale_reasons": _as_object(stale_reasons),
            "structured_form": _as_object(structured_form),  # pitch change.brief: the typed form, as stored
            "created_by": created_by,
        }

    def confirm_intent(self, tenant_id: str, workspace_id: str, intent_id: str, *,
                       actor_id: str, idempotency_key: str,
                       expected_decision_epoch: int | None = None,
                       expected_knowledge_revision_id: str | None = None,
                       expected_source_revision_id: str | None = None) -> dict:
        """`intent.confirm`: rechecks decision_epoch/knowledge_revision_id/
        source_revision_id INDEPENDENTLY (each field its own check,
        never one combined guard) against the CURRENT `v2_workspaces` head,
        both the caller's own `expected_*` pins (when given) and the
        intent's OWN `parsed_*` snapshot taken at `intent.parse` time -- a
        head/semantic decision change on EITHER axis makes the pending
        intent `IntentStale` (a parse result is only history, never an implicit
        confirmation). The status/digest write and the audit/
        idempotency inserts below are all inside this ONE `_tx()`
        transaction, so any exception raised anywhere in this method
        (including a failed audit insert) rolls back every write here --
        never a partially-confirmed row (an audit failure never leaves a
        partial confirmation). Idempotent by (tenant, workspace,
        "intent.confirm", idempotency_key): a duplicate request with the
        SAME key replays the SAME stored result rather than re-applying or
        re-auditing a second confirm."""
        if not all(isinstance(x, str) and x.strip()
                  for x in (tenant_id, workspace_id, intent_id, actor_id, idempotency_key)):
            raise ValueError("tenant, workspace, intent_id, actor and "
                            "idempotency key required")
        operation = "intent.confirm"
        payload_hash = canonical_hash({"intent_id": intent_id})
        with self._tx() as cur:
            cur.execute("""SELECT payload_hash, result FROM v2_idempotency
                WHERE tenant_id=%s AND workspace_id=%s AND operation=%s
                  AND idempotency_key=%s""",
                (tenant_id, workspace_id, operation, idempotency_key))
            row = cur.fetchone()
            if row is not None:
                if row[0] != payload_hash:
                    raise StoreConflict("idempotency_conflict")
                return _as_object(row[1])
            cur.execute("""SELECT status, parsed_source_revision_id,
                parsed_knowledge_revision_id, parsed_decision_epoch, target,
                value, scope, exclusions, effective_time
                FROM v2_intents WHERE tenant_id=%s AND workspace_id=%s
                  AND intent_id=%s FOR UPDATE""",
                (tenant_id, workspace_id, intent_id))
            intent_row = cur.fetchone()
            if intent_row is None:
                raise IntentNotFound(intent_id)
            (status, parsed_source, parsed_km, parsed_epoch, target, value,
             scope, exclusions, effective_time) = intent_row
            head = self._lock_head(cur, tenant_id, workspace_id)
            stale: set[str] = set()
            if (expected_decision_epoch is not None
                    and head.decision_epoch != expected_decision_epoch):
                stale.add("decision_epoch")
            if (expected_knowledge_revision_id is not None
                    and head.knowledge_revision_id != expected_knowledge_revision_id):
                stale.add("knowledge_revision_id")
            if (expected_source_revision_id is not None
                    and head.source_revision_id != expected_source_revision_id):
                stale.add("source_revision_id")
            if head.decision_epoch != parsed_epoch:
                stale.add("decision_epoch")
            if head.knowledge_revision_id != parsed_km:
                stale.add("knowledge_revision_id")
            if head.source_revision_id != parsed_source:
                stale.add("source_revision_id")
            if stale:
                raise IntentStale(tuple(sorted(stale)))
            if status != "PARSED":
                raise StoreConflict(f"intent not confirmable from status {status}")
            digest = canonical_hash({
                "intent_id": intent_id, "target": target,
                "value": _as_object(value), "scope": _as_object(scope),
                "exclusions": _as_object(exclusions),
                "effective_time": _as_object(effective_time),
                "actor_id": actor_id})
            cur.execute("""UPDATE v2_intents SET status='CONFIRMED',
                confirmed_actor_id=%s, digest=%s, confirmed_at=now(),
                updated_at=now()
                WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s""",
                (actor_id, digest, tenant_id, workspace_id, intent_id))
            result = {"intent_id": intent_id, "status": "CONFIRMED",
                     "digest": digest, "actor_id": actor_id,
                     "decision_epoch": head.decision_epoch}
            cur.execute("""INSERT INTO v2_idempotency
                (tenant_id, workspace_id, operation, idempotency_key, payload_hash, result)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (tenant_id, workspace_id, operation, idempotency_key, payload_hash,
                 Json(result)))
            cur.execute("""INSERT INTO v2_audit
                (tenant_id, workspace_id, event_type, actor_id, payload)
                VALUES (%s,%s,'INTENT_CONFIRMED',%s,%s)""",
                (tenant_id, workspace_id, actor_id,
                 Json({"intent_id": intent_id, "digest": digest})))
            return result
