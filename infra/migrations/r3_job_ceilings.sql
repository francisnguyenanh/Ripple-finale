-- R3: durable, append-only ceilings on how many jobs of one kind a workspace may EVER have (all states counted).
-- A live trial authorizes N jobs; a ceiling here stays true after settlement, restart and duplicate submission.
-- Rows cannot be updated or deleted (a ceiling is never silently raised); with several rows the smallest applies.
CREATE TABLE IF NOT EXISTS v2_job_ceilings (
    tenant_id    TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    kind         TEXT NOT NULL,
    label        TEXT NOT NULL,
    max_total    INTEGER NOT NULL CHECK (max_total >= 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, kind, label),
    FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, workspace_id)
);

-- A NEW allocation (approved by a person) is an appended row with `window_after`: the id of the last JOB_QUEUED outbox row at the moment it was
-- granted. For a kind that has window rows, only the NEWEST window row applies and it counts only jobs queued after that point; older rows stay
-- as the audit trail and no longer bind. Rows without `window_after` keep the lifetime meaning above.
ALTER TABLE v2_job_ceilings ADD COLUMN IF NOT EXISTS window_after BIGINT;

DROP TRIGGER IF EXISTS v2_job_ceilings_immutable ON v2_job_ceilings;
CREATE TRIGGER v2_job_ceilings_immutable
    BEFORE UPDATE OR DELETE ON v2_job_ceilings
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
