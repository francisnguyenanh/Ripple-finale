-- Separate execution status: the human
-- approval/publish workflow ("status": QUEUED -> PENDING_APPROVAL -> APPROVED
-- -> PUBLISHED, or CANCELLED) and the worker's own EXECUTION of a job are two
-- independent axes that a single "status" column could not express. The
-- deployed POST /v2/jobs route moves a job from QUEUED to PENDING_APPROVAL
-- (via set_job_requester) immediately after creation, so a worker's
-- complete_job guarded on status='QUEUED' could never match on the real
-- product path -- it always raised StoreConflict, which the worker silently
-- swallowed and acked anyway (a job's outbox event disappeared with the job
-- itself never observably executed). This migration adds a second,
-- worker-owned status column so approval/publish state and execution state
-- can each progress independently and be checked independently before a
-- job is allowed to publish.

ALTER TABLE v2_jobs
    ADD COLUMN IF NOT EXISTS execution_status TEXT NOT NULL DEFAULT 'QUEUED',
    ADD COLUMN IF NOT EXISTS execution_result JSONB,
    ADD COLUMN IF NOT EXISTS execution_reason TEXT,
    ADD COLUMN IF NOT EXISTS execution_model_id TEXT,
    ADD COLUMN IF NOT EXISTS execution_prompt_hash TEXT,
    ADD COLUMN IF NOT EXISTS execution_usage JSONB,
    ADD COLUMN IF NOT EXISTS execution_cost_usd NUMERIC,
    ADD COLUMN IF NOT EXISTS execution_duration_ms BIGINT,
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;

-- execution_status values the code writes/reads:
--   QUEUED     -- created, not yet claimed/executed by a worker (initial value)
--   RUNNING    -- claimed and heartbeating (best-effort visibility only)
--   COMPLETED  -- a real terminal success: a provider attempt returned
--                without raising, AND the transition itself committed
--                (PostgresV2Store.complete_job's guarded UPDATE)
--   FAILED     -- bounded retries exhausted on a genuine provider failure
--   CANCELLED  -- refused at admission/claim time (budget/quota), or an
--                operator-issued cancel before execution completed
--
-- Nothing in this migration touches the existing "status" column's own
-- values or the routes/methods that already manage it
-- (create_job/set_job_requester/approve_job/cancel_job/
-- recheck_and_publish_job); publishing additionally adds a
-- publish-time check that execution_status='COMPLETED' before a job may be
-- published, on top of the unchanged approval-status guard.

-- The per-job cost ledger previously created its
-- own table lazily, out of band, from `infra/worker/job_quota.py` on first
-- use. Moving it into this schema migration lets
-- `PostgresV2Store.create_job` read/reserve against it INSIDE the same
-- transaction/lock it already uses for concurrency admission (the
-- workspace row's FOR UPDATE lock taken by `_lock_head`), closing the
-- "budget reservation is a separate transaction from admission" gap.
CREATE TABLE IF NOT EXISTS v2_job_cost_ledger (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    cost_usd NUMERIC NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, job_id),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id)
);
