-- Extension contract ext/1 (FEX-05..08): the append-only ledger of human decisions and recorded external-action intents on a candidate code patch.
-- kind = approve | reject   : a human decision bound to the candidate's patch_hash AND validation_hash (both must be current when it is read)
-- kind = pr_intent          : what WOULD be published (branch, title, body); status REFUSED in this build (no publish grant exists)
-- kind = promotion          : a mock promotion of the validated artifact into a local test target (artifact_hash, environment_id)
-- kind = verification       : the result of the declared scenario checks against that target
-- kind = rollback           : the previous artifact restored in the mock target
-- The candidate, validation, promotion and verification jobs themselves are ordinary v2_jobs (deterministic ids). Immutable rows; additive only.
CREATE TABLE IF NOT EXISTS v2_ext_code_decisions (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    change_id TEXT NOT NULL,
    candidate_job_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('approve', 'reject', 'pr_intent', 'promotion', 'verification', 'rollback')),
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    patch_hash TEXT NOT NULL,
    validation_hash TEXT,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, decision_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS v2_ext_code_decisions_terminal_uq
    ON v2_ext_code_decisions (tenant_id, workspace_id, candidate_job_id, patch_hash, validation_hash)
    WHERE kind IN ('approve', 'reject');
CREATE INDEX IF NOT EXISTS v2_ext_code_decisions_change_idx
    ON v2_ext_code_decisions (tenant_id, workspace_id, change_id, created_at);
DROP TRIGGER IF EXISTS v2_ext_code_decision_immutable ON v2_ext_code_decisions;
CREATE TRIGGER v2_ext_code_decision_immutable BEFORE UPDATE OR DELETE ON v2_ext_code_decisions
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
