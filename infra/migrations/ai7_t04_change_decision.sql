-- Dedicated, non-epoch-bumping human decision ledger for a
-- "kind=approval" job's content decision (approve/reject/request_changes).
-- Deliberately NOT the `v2_decisions`/`record_human_decision` table: that
-- shared, already-reviewed method unconditionally bumps `decision_epoch`
-- and nulls `knowledge_revision_id` on every call, which would immediately
-- stale the very approval_digest this decision approves (a narrower,
-- purpose-specific table, not a widened shared primitive).
CREATE TABLE IF NOT EXISTS v2_change_decisions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,               -- the "approval" job's OWN id (never proposal_job_id)
    decision_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('approve', 'reject', 'request_changes')),
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    approval_digest TEXT,
    self_approval BOOLEAN NOT NULL DEFAULT FALSE,
    high_risk BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_key TEXT,
    payload_hash TEXT,
    new_proposal_job_id TEXT,           -- set only for a successful request_changes
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one TERMINAL decision (approve/reject) may ever exist for a given
-- approval job: the DB-level backstop behind the row-locked
-- recheck-and-insert transaction in PostgresV2Store.record_change_decision.
CREATE UNIQUE INDEX IF NOT EXISTS v2_change_decisions_terminal_uq
    ON v2_change_decisions (tenant_id, workspace_id, job_id)
    WHERE action IN ('approve', 'reject');

-- Idempotent retry: same key -> at most one row.
CREATE UNIQUE INDEX IF NOT EXISTS v2_change_decisions_idem_uq
    ON v2_change_decisions (tenant_id, workspace_id, job_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS v2_change_decisions_job_idx
    ON v2_change_decisions (tenant_id, workspace_id, job_id);
