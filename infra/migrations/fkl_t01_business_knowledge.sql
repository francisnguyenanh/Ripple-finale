-- Final knowledge loop (contract fkl/1, docs/contracts/FINAL_KNOWLEDGE_LOOP.md): business snapshots with rules, relations, gaps and decisions.
-- Additive only: no existing table or column is changed. A business snapshot never writes v2_workspaces.source_revision_id, knowledge_revision_id
-- or decision_epoch; the workspace `version` moves with every commit like any other durable change.
-- Rows are immutable (same trigger as the other v2 records); only the head pointer v2_business_heads moves, by compare-and-set on business_revision.

CREATE TABLE IF NOT EXISTS v2_business_snapshots (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('PENDING_HUMAN', 'ACCEPTED')),
    business_revision BIGINT CHECK (business_revision IS NULL OR business_revision > 0),
    parent_snapshot_id TEXT,
    source_revision_id TEXT NOT NULL,
    km_revision_id TEXT,
    decision_epoch BIGINT NOT NULL CHECK (decision_epoch >= 0),
    schema_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
    committed_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, snapshot_id),
    -- an ACCEPTED snapshot carries a revision number, a pending one never does
    CHECK ((lifecycle_state = 'ACCEPTED') = (business_revision IS NOT NULL)),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES v2_workspaces (tenant_id, workspace_id),
    FOREIGN KEY (tenant_id, workspace_id, source_revision_id)
        REFERENCES v2_source_revisions (tenant_id, workspace_id, revision_id),
    FOREIGN KEY (tenant_id, workspace_id, km_revision_id)
        REFERENCES v2_knowledge_revisions (tenant_id, workspace_id, revision_id),
    FOREIGN KEY (tenant_id, workspace_id, parent_snapshot_id)
        REFERENCES v2_business_snapshots (tenant_id, workspace_id, snapshot_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS v2_business_snapshot_revision_uq
    ON v2_business_snapshots (tenant_id, workspace_id, business_revision) WHERE business_revision IS NOT NULL;

CREATE TABLE IF NOT EXISTS v2_business_heads (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    business_revision BIGINT NOT NULL DEFAULT 0 CHECK (business_revision >= 0),
    accepted_snapshot_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES v2_workspaces (tenant_id, workspace_id),
    FOREIGN KEY (tenant_id, workspace_id, accepted_snapshot_id)
        REFERENCES v2_business_snapshots (tenant_id, workspace_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS v2_business_rules (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    authority TEXT NOT NULL CHECK (authority IN ('OBSERVED', 'TARGET')),
    status TEXT NOT NULL,
    implementation_status TEXT NOT NULL,
    body JSONB NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, snapshot_id, rule_id),
    FOREIGN KEY (tenant_id, workspace_id, snapshot_id)
        REFERENCES v2_business_snapshots (tenant_id, workspace_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS v2_business_relations (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    edge_id TEXT NOT NULL,
    status TEXT NOT NULL,
    body JSONB NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, snapshot_id, edge_id),
    FOREIGN KEY (tenant_id, workspace_id, snapshot_id)
        REFERENCES v2_business_snapshots (tenant_id, workspace_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS v2_business_gaps (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    gap_id TEXT NOT NULL,
    status TEXT NOT NULL,
    body JSONB NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, snapshot_id, gap_id),
    FOREIGN KEY (tenant_id, workspace_id, snapshot_id)
        REFERENCES v2_business_snapshots (tenant_id, workspace_id, snapshot_id)
);

-- The user's own decision (kind, their words, normalized meaning). It does not move decision_epoch: observed knowledge stays valid when a target is decided.
CREATE TABLE IF NOT EXISTS v2_business_decisions (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    quote TEXT NOT NULL,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, decision_id),
    FOREIGN KEY (tenant_id, workspace_id, snapshot_id)
        REFERENCES v2_business_snapshots (tenant_id, workspace_id, snapshot_id)
);

DROP TRIGGER IF EXISTS v2_business_snapshot_immutable ON v2_business_snapshots;
CREATE TRIGGER v2_business_snapshot_immutable BEFORE UPDATE OR DELETE ON v2_business_snapshots
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
DROP TRIGGER IF EXISTS v2_business_rule_immutable ON v2_business_rules;
CREATE TRIGGER v2_business_rule_immutable BEFORE UPDATE OR DELETE ON v2_business_rules
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
DROP TRIGGER IF EXISTS v2_business_relation_immutable ON v2_business_relations;
CREATE TRIGGER v2_business_relation_immutable BEFORE UPDATE OR DELETE ON v2_business_relations
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
DROP TRIGGER IF EXISTS v2_business_gap_immutable ON v2_business_gaps;
CREATE TRIGGER v2_business_gap_immutable BEFORE UPDATE OR DELETE ON v2_business_gaps
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
DROP TRIGGER IF EXISTS v2_business_decision_immutable ON v2_business_decisions;
CREATE TRIGGER v2_business_decision_immutable BEFORE UPDATE OR DELETE ON v2_business_decisions
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
