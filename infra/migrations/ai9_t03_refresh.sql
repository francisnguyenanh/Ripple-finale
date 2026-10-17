-- Manual external-source refresh. Additive only.
--
-- `v2_source_refreshes`: one row per refresh observation. PREPARED rows record the
-- diff a human reviewed and the digest they must commit against; a row never holds
-- raw bytes (bytes live in the immutable blob store). COMMITTED = a whole-snapshot
-- revision with trigger='refresh' was published in the same transaction that flipped
-- this row; NOOP = the upload was whole-content identical to the head (nothing else
-- was written). Provenance is ALWAYS `external_observation` (server-derived): a
-- refresh is not a ChangeSet and never carries or mints an approval.
--
-- `impact` is the worker's durable, write-once assessment of what the refresh
-- invalidated (affected candidates, reasons, scheduled reanalysis notes); it is what
-- the status route reports, so a redelivered event never recomputes a different answer.
CREATE TABLE IF NOT EXISTS v2_source_refreshes (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    refresh_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PREPARED', 'COMMITTED', 'NOOP')),
    base_version BIGINT NOT NULL CHECK (base_version >= 0),
    base_source_revision_id TEXT,
    noop BOOLEAN NOT NULL,
    language TEXT,
    report JSONB NOT NULL,
    report_hash TEXT NOT NULL,
    diff JSONB NOT NULL,
    refresh_digest TEXT NOT NULL,
    provenance_kind TEXT NOT NULL DEFAULT 'external_observation'
        CHECK (provenance_kind = 'external_observation'),
    expires_at TIMESTAMPTZ NOT NULL,
    committed_revision_id TEXT,
    committed_by TEXT,
    committed_at TIMESTAMPTZ,
    ingest_job_id TEXT,
    dropped_knowledge_revision_id TEXT,
    commit_result JSONB,
    impact JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, refresh_id),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id),
    CHECK ((status = 'COMMITTED') = (committed_revision_id IS NOT NULL))
);

-- A revision is produced by at most one refresh.
CREATE UNIQUE INDEX IF NOT EXISTS v2_source_refreshes_revision_uq
    ON v2_source_refreshes (tenant_id, workspace_id, committed_revision_id)
    WHERE committed_revision_id IS NOT NULL;
