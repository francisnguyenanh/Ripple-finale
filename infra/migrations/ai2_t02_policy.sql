-- Central permission enforcement needs a per-workspace policy toggle
-- for the requester/approver distinctness rule. Default is the safer pilot
-- value (distinct approver required); demo workspaces may relax it and audit
-- every self-approval that results.

ALTER TABLE v2_workspaces
    ADD COLUMN IF NOT EXISTS require_distinct_approver BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE v2_jobs
    ADD COLUMN IF NOT EXISTS requested_by TEXT,
    ADD COLUMN IF NOT EXISTS approved_by TEXT,
    ADD COLUMN IF NOT EXISTS approved_with_roles TEXT[];
