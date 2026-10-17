-- Extension contract ext/1 (FEX-05): a pull request opened by the product after approval is recorded in the append-only code-decision ledger.
-- kind = pr_published : branch, commit, pull request number/url of ONE pull request on the declared repository (never a merge). At most one per candidate + validation.
-- Additive: widens the kind check of ext_t01; existing rows are unchanged.
ALTER TABLE v2_ext_code_decisions DROP CONSTRAINT IF EXISTS v2_ext_code_decisions_kind_check;
ALTER TABLE v2_ext_code_decisions ADD CONSTRAINT v2_ext_code_decisions_kind_check
    CHECK (kind IN ('approve', 'reject', 'pr_intent', 'pr_published', 'promotion', 'verification', 'rollback'));
CREATE UNIQUE INDEX IF NOT EXISTS v2_ext_code_decisions_pr_published_uq
    ON v2_ext_code_decisions (tenant_id, workspace_id, candidate_job_id, patch_hash, validation_hash)
    WHERE kind = 'pr_published';
