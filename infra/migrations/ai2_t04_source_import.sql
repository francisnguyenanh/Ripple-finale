-- Source import staging, admission report and revision metadata.
-- Additive only; existing tables keep their meaning.

CREATE TABLE IF NOT EXISTS v2_source_imports (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    import_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PREPARED', 'COMMITTED')),
    base_version BIGINT NOT NULL CHECK (base_version >= 0),
    base_source_revision_id TEXT,
    language TEXT,
    -- Manifest preview items (paths, hashes, dispositions, reasons, roles);
    -- never raw bytes. Bytes live in the immutable blob store.
    report JSONB NOT NULL,
    report_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    committed_revision_id TEXT,
    committed_by TEXT,
    committed_at TIMESTAMPTZ,
    ingest_job_id TEXT,
    commit_result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, import_id),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id),
    CHECK ((status = 'COMMITTED') = (committed_revision_id IS NOT NULL))
);

-- One committed import per revision (a revision is never produced twice).
CREATE UNIQUE INDEX IF NOT EXISTS v2_source_imports_revision_uq
    ON v2_source_imports (tenant_id, workspace_id, committed_revision_id)
    WHERE committed_revision_id IS NOT NULL;

ALTER TABLE v2_source_revisions
    ADD COLUMN IF NOT EXISTS manifest_hash TEXT,
    ADD COLUMN IF NOT EXISTS created_by TEXT,
    ADD COLUMN IF NOT EXISTS trigger TEXT,
    ADD COLUMN IF NOT EXISTS source_connector_id TEXT;

ALTER TABLE v2_artifact_revisions
    ADD COLUMN IF NOT EXISTS logical_path TEXT,
    ADD COLUMN IF NOT EXISTS logical_path_key TEXT,
    ADD COLUMN IF NOT EXISTS media_type TEXT,
    ADD COLUMN IF NOT EXISTS byte_size BIGINT,
    ADD COLUMN IF NOT EXISTS source_role TEXT,
    ADD COLUMN IF NOT EXISTS source_role_origin TEXT,
    ADD COLUMN IF NOT EXISTS provenance TEXT;

-- Defense in depth for the case/Unicode collision rule: two artifacts of one
-- revision can never share a normalized (NFKC + casefold) logical path.
CREATE UNIQUE INDEX IF NOT EXISTS v2_artifact_revisions_path_key_uq
    ON v2_artifact_revisions (tenant_id, workspace_id, source_revision_id, logical_path_key)
    WHERE logical_path_key IS NOT NULL;
