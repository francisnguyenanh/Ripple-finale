-- Authoritative v2 workspace state. Apply transactionally.
-- Legacy session tables are intentionally untouched.

CREATE TABLE IF NOT EXISTS v2_workspaces (
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    source_revision_id TEXT,
    knowledge_revision_id TEXT,
    decision_epoch BIGINT NOT NULL DEFAULT 0 CHECK (decision_epoch >= 0),
    policy_version TEXT NOT NULL DEFAULT '1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id),
    UNIQUE (tenant_id, project_id, workspace_id)
);

CREATE TABLE IF NOT EXISTS v2_source_revisions (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    parent_revision_id TEXT,
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, revision_id),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id),
    FOREIGN KEY (tenant_id, workspace_id, parent_revision_id)
        REFERENCES v2_source_revisions (tenant_id, workspace_id, revision_id)
);

CREATE TABLE IF NOT EXISTS v2_blob_refs (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    blob_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, blob_hash),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id)
);

CREATE TABLE IF NOT EXISTS v2_artifact_revisions (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    blob_hash TEXT NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, source_revision_id, artifact_id),
    FOREIGN KEY (tenant_id, workspace_id, source_revision_id)
        REFERENCES v2_source_revisions (tenant_id, workspace_id, revision_id),
    FOREIGN KEY (tenant_id, workspace_id, blob_hash)
        REFERENCES v2_blob_refs (tenant_id, workspace_id, blob_hash)
);

CREATE TABLE IF NOT EXISTS v2_knowledge_revisions (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL,
    decision_epoch BIGINT NOT NULL CHECK (decision_epoch >= 0),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, revision_id),
    FOREIGN KEY (tenant_id, workspace_id, source_revision_id)
        REFERENCES v2_source_revisions (tenant_id, workspace_id, revision_id)
);

CREATE TABLE IF NOT EXISTS v2_decisions (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    epoch BIGINT NOT NULL CHECK (epoch >= 0),
    actor_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, decision_id),
    UNIQUE (tenant_id, workspace_id, epoch),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id)
);

CREATE TABLE IF NOT EXISTS v2_jobs (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    pinned_source_revision_id TEXT,
    pinned_knowledge_revision_id TEXT,
    pinned_decision_epoch BIGINT NOT NULL,
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    payload JSONB NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, job_id),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id)
);

CREATE TABLE IF NOT EXISTS v2_audit (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id)
);
CREATE INDEX IF NOT EXISTS v2_audit_scope_idx
    ON v2_audit (tenant_id, workspace_id, id);

CREATE TABLE IF NOT EXISTS v2_outbox (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    fencing_token BIGINT NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id)
);
CREATE INDEX IF NOT EXISTS v2_outbox_claim_idx
    ON v2_outbox (status, lease_until, id);

CREATE TABLE IF NOT EXISTS v2_idempotency (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, operation, idempotency_key),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id)
);

CREATE TABLE IF NOT EXISTS v2_blob_holds (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    blob_hash TEXT NOT NULL,
    hold_kind TEXT NOT NULL,
    hold_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, blob_hash, hold_kind, hold_id),
    FOREIGN KEY (tenant_id, workspace_id, blob_hash)
        REFERENCES v2_blob_refs (tenant_id, workspace_id, blob_hash)
);

CREATE TABLE IF NOT EXISTS v2_index_manifests (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    knowledge_revision_id TEXT NOT NULL,
    index_version TEXT NOT NULL,
    status TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, knowledge_revision_id, index_version),
    FOREIGN KEY (tenant_id, workspace_id, knowledge_revision_id)
        REFERENCES v2_knowledge_revisions (tenant_id, workspace_id, revision_id)
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'v2_workspace_source_head_fk') THEN
        ALTER TABLE v2_workspaces ADD CONSTRAINT v2_workspace_source_head_fk
            FOREIGN KEY (tenant_id, workspace_id, source_revision_id)
            REFERENCES v2_source_revisions (tenant_id, workspace_id, revision_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'v2_workspace_knowledge_head_fk') THEN
        ALTER TABLE v2_workspaces ADD CONSTRAINT v2_workspace_knowledge_head_fk
            FOREIGN KEY (tenant_id, workspace_id, knowledge_revision_id)
            REFERENCES v2_knowledge_revisions (tenant_id, workspace_id, revision_id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'v2_job_source_fk') THEN
        ALTER TABLE v2_jobs ADD CONSTRAINT v2_job_source_fk
            FOREIGN KEY (tenant_id, workspace_id, pinned_source_revision_id)
            REFERENCES v2_source_revisions (tenant_id, workspace_id, revision_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'v2_job_knowledge_fk') THEN
        ALTER TABLE v2_jobs ADD CONSTRAINT v2_job_knowledge_fk
            FOREIGN KEY (tenant_id, workspace_id, pinned_knowledge_revision_id)
            REFERENCES v2_knowledge_revisions (tenant_id, workspace_id, revision_id);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION v2_reject_immutable_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'v2 immutable record cannot be updated or deleted';
END $$;

DROP TRIGGER IF EXISTS v2_source_revision_immutable ON v2_source_revisions;
CREATE TRIGGER v2_source_revision_immutable BEFORE UPDATE OR DELETE ON v2_source_revisions
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
DROP TRIGGER IF EXISTS v2_artifact_revision_immutable ON v2_artifact_revisions;
CREATE TRIGGER v2_artifact_revision_immutable BEFORE UPDATE OR DELETE ON v2_artifact_revisions
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
DROP TRIGGER IF EXISTS v2_knowledge_revision_immutable ON v2_knowledge_revisions;
CREATE TRIGGER v2_knowledge_revision_immutable BEFORE UPDATE OR DELETE ON v2_knowledge_revisions
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
DROP TRIGGER IF EXISTS v2_decision_immutable ON v2_decisions;
CREATE TRIGGER v2_decision_immutable BEFORE UPDATE OR DELETE ON v2_decisions
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
DROP TRIGGER IF EXISTS v2_audit_immutable ON v2_audit;
CREATE TRIGGER v2_audit_immutable BEFORE UPDATE OR DELETE ON v2_audit
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
