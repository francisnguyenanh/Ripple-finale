-- Final knowledge loop: retrieval index over a business snapshot (rules, decisions, open questions). A derived projection, rebuildable from the snapshot
-- tables alone, keyed by snapshot and index version so a different version or snapshot is never mistaken for the current one. Additive only.

CREATE TABLE IF NOT EXISTS v2_business_index_items (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    index_version TEXT NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    term_vector JSONB NOT NULL,
    body JSONB NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, snapshot_id, index_version, item_id),
    FOREIGN KEY (tenant_id, workspace_id, snapshot_id)
        REFERENCES v2_business_snapshots (tenant_id, workspace_id, snapshot_id)
);

-- Readiness marker, written in the same transaction as the items (READY) or on its own after a failed attempt (FAILED).
-- No marker means the index is not built: readers never serve rows without a READY marker of the current version.
CREATE TABLE IF NOT EXISTS v2_business_index_builds (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    index_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('READY', 'FAILED')),
    item_count INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    built_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, snapshot_id, index_version),
    FOREIGN KEY (tenant_id, workspace_id, snapshot_id)
        REFERENCES v2_business_snapshots (tenant_id, workspace_id, snapshot_id)
);
