-- Operator-supplied
-- verification contract. Additive only. A row is the expected value an authenticated, human operator/approver
-- states for ONE locator of ONE logical path of a proposal, stored with provenance. Expected values never come
-- from an AI output, the proposal's own edits, a repo file, an env var or a held-out label.
--
-- Immutable once written (the same v2_reject_immutable_change() trigger as the revision/audit tables). A revision
-- is a NEW proposal job, so the unique key below never needs an UPDATE.
CREATE TABLE IF NOT EXISTS v2_verification_contracts (
    contract_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    proposal_job_id TEXT NOT NULL,
    logical_path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('yaml_value_equals', 'xlsx_cell_equals')),
    locator TEXT NOT NULL,
    expected JSONB NOT NULL,
    author_account_id TEXT NOT NULL,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    contract_sha256 TEXT NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, contract_id),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS v2_verification_contracts_scope_uq
    ON v2_verification_contracts (tenant_id, workspace_id, proposal_job_id, logical_path, locator);

DROP TRIGGER IF EXISTS v2_verification_contract_immutable ON v2_verification_contracts;
CREATE TRIGGER v2_verification_contract_immutable
    BEFORE UPDATE OR DELETE ON v2_verification_contracts
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
