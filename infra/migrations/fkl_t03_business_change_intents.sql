-- Final knowledge loop: a confirmed business change intent (what a person asked to change, as validated operations over a pinned snapshot).
-- It is a REQUEST, not a decision about the rules: it changes no rule, knowledge head or source. Immutable once written. Additive only.
CREATE TABLE IF NOT EXISTS v2_business_change_intents (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    business_revision BIGINT NOT NULL,
    request_text TEXT NOT NULL,
    operations JSONB NOT NULL,
    write_scope JSONB NOT NULL,
    read_scope JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, intent_id),
    FOREIGN KEY (tenant_id, workspace_id, snapshot_id)
        REFERENCES v2_business_snapshots (tenant_id, workspace_id, snapshot_id)
);
DROP TRIGGER IF EXISTS v2_business_change_intent_immutable ON v2_business_change_intents;
CREATE TRIGGER v2_business_change_intent_immutable BEFORE UPDATE OR DELETE ON v2_business_change_intents
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
