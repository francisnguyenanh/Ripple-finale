-- AH-05: durable HTTP -> job correlation. Append-only, additive; no existing table changes. One row per (job, request) that created the job or replayed it
-- idempotently, or per background creation (parent job). Job identity, idempotency and authorization do not read this table; old jobs simply have no row.
CREATE TABLE IF NOT EXISTS v2_job_correlation (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    request_id TEXT,
    parent_job_id TEXT,
    created BOOLEAN NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (request_id IS NOT NULL OR parent_job_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS v2_job_correlation_job_idx ON v2_job_correlation (tenant_id, workspace_id, job_id, recorded_at);
CREATE INDEX IF NOT EXISTS v2_job_correlation_request_idx ON v2_job_correlation (request_id) WHERE request_id IS NOT NULL;
