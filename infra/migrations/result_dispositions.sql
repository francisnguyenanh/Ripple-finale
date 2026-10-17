-- A person who accepts the result of a change decides, ITEM BY ITEM, what happens to the problems that may affect that change (a finding, or a reading elsewhere that states a limit of the
-- same kind). Written ONLY inside the transaction that records the result decision (so a decision never exists without its dispositions), one row per item, never updated.
--   accepted_limit    the person accepts the limitation / risk for this request, with a reason
--   judged_unrelated  the person states, with a reason, that the item is a different control or not part of this request
--   unhandled         the item is left open on purpose; the acceptance is then NARROW and says so
-- "resolved" is not a disposition: it is derived (a finding that has a resolve decision stops being listed).
CREATE TABLE IF NOT EXISTS v2_change_item_dispositions (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    apply_job_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_kind TEXT NOT NULL CHECK (item_kind IN ('finding', 'reading')),
    disposition TEXT NOT NULL CHECK (disposition IN ('accepted_limit', 'judged_unrelated', 'unhandled')),
    reason TEXT,
    actor_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, apply_job_id, item_id)
);
ALTER TABLE v2_change_result_decisions ADD COLUMN IF NOT EXISTS acceptance_scope TEXT NOT NULL DEFAULT 'FULL';
ALTER TABLE v2_change_item_dispositions ADD COLUMN IF NOT EXISTS decision_basis TEXT;
ALTER TABLE v2_change_item_dispositions ADD COLUMN IF NOT EXISTS basis JSONB;
