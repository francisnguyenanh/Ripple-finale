-- Per-attempt job execution records, cost-ledger settlement and per-project model policy.
--
-- 1. Per-attempt execution records ("Persist model/prompt/schema
--    hash, request context IDs, usage/cost estimate and duration").
--    One row per (job, attempt_key); attempt_key is derived from the outbox
--    event id + attempt number + fencing token, so a redelivered/replayed
--    write of the SAME attempt is a no-op (ON CONFLICT DO NOTHING) and a
--    genuinely new attempt is always a new row. Raw prompt text is NEVER a
--    column here: only `request_excerpt`, which is NULL unless the project's
--    model-data policy (v2_project_model_policy below) explicitly opts in, and
--    even then holds only the secret-redacted excerpt.
CREATE TABLE IF NOT EXISTS v2_job_attempts (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    attempt_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT,
    retryable BOOLEAN,
    executor_id TEXT,
    model_id TEXT,
    prompt_hash TEXT,
    schema_hash TEXT,
    request_context JSONB NOT NULL,
    request_excerpt TEXT,
    provider_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_estimate_usd NUMERIC,
    cost_usd NUMERIC NOT NULL DEFAULT 0,
    duration_ms BIGINT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, job_id, attempt_key)
);

ALTER TABLE v2_jobs
    ADD COLUMN IF NOT EXISTS execution_schema_hash TEXT,
    ADD COLUMN IF NOT EXISTS execution_request_context JSONB,
    ADD COLUMN IF NOT EXISTS execution_executor_id TEXT,
    ADD COLUMN IF NOT EXISTS execution_provider_calls INTEGER;

-- 2. Reservation vs settlement in the cost ledger. A reservation row is
--    written by create_job (worst-case estimate, entry_state='RESERVED');
--    the worker's terminal transition settles it (entry_state='SETTLED',
--    cost_usd = sum of the job's real per-attempt costs) IN THE SAME
--    TRANSACTION as the execution_status change, so a job can never be
--    COMPLETED/FAILED with its cost unaccounted. Pre-existing rows (written
--    by record_job_cost) default to SETTLED, preserving their meaning.
ALTER TABLE v2_job_cost_ledger
    ADD COLUMN IF NOT EXISTS entry_state TEXT NOT NULL DEFAULT 'SETTLED',
    ADD COLUMN IF NOT EXISTS reserved_usd NUMERIC,
    ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ;

-- An earlier migration declared a FOREIGN KEY from the ledger to
-- v2_workspaces. On a database where the ledger was first created lazily by
-- job_quota.record_job_cost the FK never
-- existed; on a FRESH database it does, and it rejects the ledger's
-- documented use for spend recorded against a (tenant, workspace) scope that
-- has no v2_workspaces row. Drop it so fresh and long-lived databases have
-- the SAME schema (clean-clone reproducibility).
ALTER TABLE v2_job_cost_ledger
    DROP CONSTRAINT IF EXISTS v2_job_cost_ledger_tenant_id_workspace_id_fkey;

-- 3. Project-scoped model-data / redaction policy ("Redaction/
--    model data policy configured per project"). Absent row == the safe
--    default: store hashes only (never raw or excerpted prompts), external
--    provider calls allowed only because none is wired yet; an operator can
--    set external_provider='deny' to block any executor that would send
--    job content to an external model.
CREATE TABLE IF NOT EXISTS v2_project_model_policy (
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    prompt_storage TEXT NOT NULL DEFAULT 'hash_only'
        CHECK (prompt_storage IN ('hash_only', 'redacted_excerpt')),
    external_provider TEXT NOT NULL DEFAULT 'allow'
        CHECK (external_provider IN ('allow', 'deny')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id)
);
