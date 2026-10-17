-- Extension contract ma/1 (MA-00): durable multi-agent runs. A run is the coordinator's state (manifest, pins, caps, root reservation, status, report); a child is one
-- specialist job of the run (role, round, attempt, the ordinary v2_jobs row it maps to); events are the append-only, sanitized orchestration trail.
-- Runs and children carry state and are updated with compare-and-set on `status`; events are immutable. Additive only.
CREATE TABLE IF NOT EXISTS v2_ma_runs (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    workflow TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    change_id TEXT NOT NULL,
    binding JSONB NOT NULL,
    context JSONB NOT NULL,
    context_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PLANNED', 'RUNNING', 'JOINING', 'REVIEW_READY', 'INCOMPLETE', 'FAILED', 'CANCELLED')),
    caps JSONB NOT NULL,
    root_reserved_usd NUMERIC NOT NULL DEFAULT 0,
    requested_by TEXT NOT NULL,
    deadline_at TIMESTAMPTZ,
    report JSONB,
    report_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, run_id)
);
CREATE INDEX IF NOT EXISTS v2_ma_runs_change_idx ON v2_ma_runs (tenant_id, workspace_id, change_id, created_at);

CREATE TABLE IF NOT EXISTS v2_ma_children (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    role TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PLANNED', 'DISPATCHED', 'COMPLETED', 'FAILED', 'CANCELLED', 'LATE_IGNORED')),
    result_hash TEXT,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, child_id),
    UNIQUE (tenant_id, workspace_id, run_id, role, round_no, attempt)
);
CREATE INDEX IF NOT EXISTS v2_ma_children_run_idx ON v2_ma_children (tenant_id, workspace_id, run_id);

CREATE TABLE IF NOT EXISTS v2_ma_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v2_ma_events_run_idx ON v2_ma_events (tenant_id, workspace_id, run_id, id);
DROP TRIGGER IF EXISTS v2_ma_events_immutable ON v2_ma_events;
CREATE TRIGGER v2_ma_events_immutable BEFORE UPDATE OR DELETE ON v2_ma_events
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
