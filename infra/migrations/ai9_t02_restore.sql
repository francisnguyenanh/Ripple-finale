-- History + restore. Additive only.
--
-- `restored_from`: provenance of a FORWARD revision produced by a restore (the
-- historic revision whose whole manifest it re-publishes). `trigger` already
-- exists; a restore writes trigger='restore'. NULL for every
-- revision that is not a restore. Written ONLY by `publish_source` (the single
-- publication primitive) when the apply gate passes `restored_from`.
ALTER TABLE v2_source_revisions
    ADD COLUMN IF NOT EXISTS restored_from TEXT;

-- `v2_restore_approvals`: the durable RESTORE-subject human approval, bound to
-- the exact preview digest and to the workspace tuple it was computed
-- over (`snapshot` = expected {version, source_revision_id, decision_epoch,
-- knowledge_revision_id}). It is NOT a source/KM/decision row: recording it
-- never moves the workspace head (`infra/adapters/v2_restore.py::approve_restore`).
-- One row per (target, preview digest, actor): the DB-level backstop behind
-- the head-locked recompute-and-insert transaction (idempotent replay).
CREATE TABLE IF NOT EXISTS v2_restore_approvals (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    target_revision_id TEXT NOT NULL,
    preview_digest TEXT NOT NULL,
    approval_digest TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    snapshot JSONB NOT NULL,
    idempotency_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS v2_restore_approvals_id_uq
    ON v2_restore_approvals (tenant_id, workspace_id, approval_id);
CREATE UNIQUE INDEX IF NOT EXISTS v2_restore_approvals_replay_uq
    ON v2_restore_approvals (tenant_id, workspace_id, target_revision_id, preview_digest, actor_id);
