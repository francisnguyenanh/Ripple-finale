-- AH-02: bounded review-note lookups (the request task view, the assistant context and the approval gate read a proposal's notes by `payload->>'proposal_job_id'`).
-- Measured on the isolated test database (2026-10-24): with 50,000 other audit events in the workspace the page query was a sequential scan (902 buffers,
-- ~5.8 ms, growing with the workspace's whole audit history); with this partial index, 5 buffers, ~0.06 ms. Partial: only review-note rows are indexed, so
-- the write cost falls on note inserts alone. Built by `migrate` before traffic (the operating-window runbook migrates first); rollback: DROP INDEX.
CREATE INDEX IF NOT EXISTS v2_audit_review_note_idx
    ON v2_audit (tenant_id, workspace_id, (payload->>'proposal_job_id'), created_at DESC, id DESC)
    WHERE event_type = 'CHANGE_REVIEW_NOTE';
