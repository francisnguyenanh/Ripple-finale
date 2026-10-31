-- v2_043 (REL-03, 2026-10-31): the current-candidate index. The candidates an extraction job persisted inside its `execution_result` JSONB are also
-- written, in the same transaction as the job's completion, as relational rows keyed by (workspace, source revision, artifact, candidate), so the
-- readers that build the current knowledge (`current_candidates`, `extraction_candidate_index`) range-scan an index instead of reading every completed
-- extraction job's JSONB. Additive and idempotent; the JSONB stays the record of the job (the index is derived and can be rebuilt by
-- `python -m tools.ops_jobs backfill-candidates`). Old code ignores the table.
CREATE TABLE IF NOT EXISTS v2_extraction_candidates (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    candidate JSONB NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, job_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS v2_extraction_candidates_by_artifact
    ON v2_extraction_candidates (tenant_id, workspace_id, artifact_id, source_revision_id, completed_at);
