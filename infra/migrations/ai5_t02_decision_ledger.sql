-- Fuller append-only HumanDecision ledger columns on the existing
-- `v2_decisions` table (created by ai1_t03_v2.sql, written to today only by
-- the legacy, narrow `PostgresV2Store.record_decision`).
--
-- These columns are ADDITIVE (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) so
-- the pre-existing narrow `record_decision` path (still used verbatim by
-- `test_audit_and_decision_write_share_one_transaction_boundary`) keeps
-- working unchanged -- rows it writes simply leave the new columns NULL.
-- The new `PostgresV2Store.record_human_decision` populates them.
--
-- `target` identifies what the decision is about (a claim_id/candidate_id
-- for confirm/resolve, a gap_id for accepted_unknown). `superseded_by`/
-- `revoked_by` are back-links set on the ORIGINAL row when a LATER decision
-- supersedes/revokes it -- the original row's `payload`/`action`/`rationale`
-- etc. are never edited; only these two nullable back-link columns are ever
-- updated post-insert, which is how append-only history stays provable
-- (`test_supersede_and_revoke_preserve_append_only_history` reads `payload`
-- for the original decision_id and expects it still present/unmodified).
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS target TEXT;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS action TEXT;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS rationale TEXT;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS evidence_ref TEXT;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS provenance TEXT;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS scope JSONB;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS effective_from TIMESTAMPTZ;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS effective_until TIMESTAMPTZ;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS supersedes TEXT;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS revokes TEXT;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS superseded_by TEXT;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS revoked_by TEXT;
ALTER TABLE v2_decisions ADD COLUMN IF NOT EXISTS current_permission TEXT;

CREATE INDEX IF NOT EXISTS v2_decisions_target_idx
    ON v2_decisions (tenant_id, workspace_id, target);
