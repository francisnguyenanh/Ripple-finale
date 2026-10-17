-- Final knowledge loop: generated Business/Technical documents of one accepted business snapshot (DOC-02).
-- A row exists only for a SUCCESSFULLY generated artifact: immutable Markdown bytes (content-addressed blob) plus the metadata that identifies the generation
-- (snapshot, mode, scope, locale, template, generator, schema, redaction scope). The artifact id is the cache key (contract fkl/1 section 2.5 artifact_key):
-- authorization is NOT part of it and is rechecked on every read. Freshness is derived on read (available / stale against the current accepted snapshot).
-- Generated documents are derived views: never source evidence, never re-imported. Additive only; immutable once written.
CREATE TABLE IF NOT EXISTS v2_business_documents (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('business', 'technical')),
    scope JSONB NOT NULL,
    locale TEXT NOT NULL,
    template_version TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    redaction_scope TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    blob_digest TEXT NOT NULL,
    rule_count INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    generated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, artifact_id),
    FOREIGN KEY (tenant_id, workspace_id, snapshot_id)
        REFERENCES v2_business_snapshots (tenant_id, workspace_id, snapshot_id)
);
CREATE INDEX IF NOT EXISTS v2_business_documents_snapshot_idx
    ON v2_business_documents (tenant_id, workspace_id, snapshot_id, mode);
DROP TRIGGER IF EXISTS v2_business_document_immutable ON v2_business_documents;
CREATE TRIGGER v2_business_document_immutable BEFORE UPDATE OR DELETE ON v2_business_documents
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
