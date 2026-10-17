-- Additive legacy import metadata and reversible app routing flag.
-- Apply after ai1_t03_v2.sql; never delete v1 rows or v2 decisions.

ALTER TABLE v2_source_revisions
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS v2_legacy_imports (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    legacy_session_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL,
    knowledge_revision_id TEXT,
    legacy_baseline_id TEXT NOT NULL,
    legacy_durable_version BIGINT NOT NULL CHECK (legacy_durable_version >= 0),
    legacy_session JSONB NOT NULL,
    legacy_work JSONB NOT NULL,
    lineage_entries JSONB NOT NULL,
    human_decisions JSONB NOT NULL DEFAULT '[]'::jsonb,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, legacy_session_id),
    FOREIGN KEY (tenant_id, workspace_id, source_revision_id)
        REFERENCES v2_source_revisions (tenant_id, workspace_id, revision_id),
    FOREIGN KEY (tenant_id, workspace_id, knowledge_revision_id)
        REFERENCES v2_knowledge_revisions (tenant_id, workspace_id, revision_id)
);

-- Safe when this migration was installed before the full-session snapshot
-- column was introduced; old imports retain their existing history fields.
ALTER TABLE v2_legacy_imports
    ADD COLUMN IF NOT EXISTS legacy_session JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE v2_legacy_imports
    ADD COLUMN IF NOT EXISTS human_decisions JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS v2_workspace_rollout (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    legacy_session_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id),
    FOREIGN KEY (tenant_id, workspace_id, legacy_session_id)
        REFERENCES v2_legacy_imports (tenant_id, workspace_id, legacy_session_id)
);

DROP TRIGGER IF EXISTS v2_legacy_import_immutable ON v2_legacy_imports;
CREATE TRIGGER v2_legacy_import_immutable
    BEFORE UPDATE OR DELETE ON v2_legacy_imports
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
