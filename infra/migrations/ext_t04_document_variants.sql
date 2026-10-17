-- Extension AIX-D1: audience variants (executive | sop | audit) of one generated document. A row exists only for a SUCCESSFULLY produced variant (model wording checked by the
-- server, or the labelled deterministic fallback): immutable Markdown bytes (content-addressed blob) plus what identifies the generation (base artifact, audience, locale,
-- frozen config hash, pinned model) and what the server decided (status, fact coverage, rejected paragraphs). The variant id is the cache key; authorization is rechecked on read.
-- Derived view: never evidence, never re-imported. Additive only; immutable once written.
CREATE TABLE IF NOT EXISTS v2_document_variants (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    variant_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    audience TEXT NOT NULL CHECK (audience IN ('executive', 'sop', 'audit')),
    locale TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    model_pinned TEXT NOT NULL,
    model_served TEXT,
    status TEXT NOT NULL CHECK (status IN ('generated', 'fallback')),
    content_hash TEXT NOT NULL,
    blob_digest TEXT NOT NULL,
    fact_coverage JSONB NOT NULL,
    rejected JSONB NOT NULL DEFAULT '[]'::jsonb,
    size_bytes INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    generated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, variant_id),
    FOREIGN KEY (tenant_id, workspace_id, artifact_id) REFERENCES v2_business_documents (tenant_id, workspace_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS v2_document_variants_artifact_idx ON v2_document_variants (tenant_id, workspace_id, artifact_id, audience);
DROP TRIGGER IF EXISTS v2_document_variant_immutable ON v2_document_variants;
CREATE TRIGGER v2_document_variant_immutable BEFORE UPDATE OR DELETE ON v2_document_variants
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
