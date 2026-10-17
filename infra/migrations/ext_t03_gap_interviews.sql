-- Extension AIX-K2: one finite interview per (workspace, open question, knowledge version). Mutable session state (status, turns) with compare-and-set on status;
-- the person's confirmation goes through the existing decision path (v2_business_decisions), never through this table. Additive only.
CREATE TABLE IF NOT EXISTS v2_gap_interviews (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    gap_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'READY_TO_CONFIRM', 'UNRESOLVED', 'CONFIRMED', 'STALE', 'CLOSED')),
    turns JSONB NOT NULL DEFAULT '[]'::jsonb,
    cap INTEGER NOT NULL DEFAULT 5,
    requested_by TEXT NOT NULL,
    decision_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, session_id)
);
CREATE INDEX IF NOT EXISTS v2_gap_interviews_gap_idx ON v2_gap_interviews (tenant_id, workspace_id, gap_id, created_at);
