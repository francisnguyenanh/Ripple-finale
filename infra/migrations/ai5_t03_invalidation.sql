-- Dependency invalidation / reanalysis after a committed decision.
--
-- Two small, append-only-by-key tables. Neither mutates `v2_jobs`'
-- `execution_result` (that column stays the executor's own untouched output
-- -- a durable "stale" flag must be a REAL, separate marker, not a
-- reinterpretation of an outbox/execution-status field the pipeline already
-- treats as terminal).
--
-- `v2_stale_markers`: one row per (dependent job, decision) that made it
-- stale. PK is (tenant_id, workspace_id, job_id, decision_id) so processing
-- the SAME decision event twice against the SAME dependent is a no-op
-- (`ON CONFLICT DO NOTHING`), never a duplicate marker.
CREATE TABLE IF NOT EXISTS v2_stale_markers (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    marked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, job_id, decision_id)
);

-- `v2_reanalysis_causation`: the durable decision event -> invalidation job
-- -> scheduled reanalysis job chain (the "event causation chain").
-- PK is (tenant_id, workspace_id, decision_id, dependent_job_id): at most one
-- causation row, and therefore at most one scheduled reanalysis job, per
-- (decision, dependent) pair -- the idempotent-scheduling boundary that
-- keeps processing the same decision event twice from double-scheduling.
CREATE TABLE IF NOT EXISTS v2_reanalysis_causation (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    invalidation_job_id TEXT NOT NULL,
    dependent_job_id TEXT NOT NULL,
    reanalysis_job_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, decision_id, dependent_job_id)
);

CREATE INDEX IF NOT EXISTS v2_stale_markers_job_idx
    ON v2_stale_markers (tenant_id, workspace_id, job_id);
