-- EUI-08: guided proposal preparation runs (contract developing-side v2; product contract docs/contracts/GUIDED_PREPARATION.md). One run = one starter, one confirmed
-- request, one draft and one manifest hash the starter confirmed; the children are ordinary jobs created AS the starter through the normal job-creation contract.
-- Additive only; no existing table changes.
CREATE TABLE IF NOT EXISTS v2_preparation_runs (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    started_by TEXT NOT NULL,
    manifest JSONB NOT NULL,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'PAUSED', 'FAILED', 'PROPOSAL_READY', 'CANCELLED')),
    reason TEXT,
    children JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, run_id)
);
CREATE INDEX IF NOT EXISTS v2_preparation_runs_intent_idx ON v2_preparation_runs (tenant_id, workspace_id, intent_id, created_at);
