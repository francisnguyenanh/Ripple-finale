-- Extension AIX-K1: a person's dismissal of one mining hypothesis, pinned to the knowledge version it was found on. Hypotheses themselves live on the mining job results
-- (read-only findings); a dismissal is the only durable human act here and never touches the knowledge. Additive only; immutable once written.
CREATE TABLE IF NOT EXISTS v2_mining_dismissals (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, hypothesis_id)
);
DROP TRIGGER IF EXISTS v2_mining_dismissal_immutable ON v2_mining_dismissals;
CREATE TRIGGER v2_mining_dismissal_immutable BEFORE UPDATE OR DELETE ON v2_mining_dismissals
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
