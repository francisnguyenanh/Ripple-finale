-- Human result acceptance + closed-loop completion.
--
-- `v2_change_result_decisions`: the ONE terminal human decision (accept /
-- request_changes / reject) on a published apply, bound to the exact machine
-- report digest it attested. Deliberately NOT `v2_decisions` /
-- `record_human_decision`: an attestation never bumps `decision_epoch`, never
-- nulls `knowledge_revision_id` and never moves the workspace head (it would
-- stale the very report it accepts). At most one row per apply job (DB-level
-- backstop behind the head-locked recheck-and-insert transaction in
-- `infra/adapters/v2_change.py::record_result_decision`).
CREATE TABLE IF NOT EXISTS v2_change_result_decisions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    apply_job_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('accept', 'request_changes', 'reject')),
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    report_digest TEXT NOT NULL,
    published_revision_id TEXT NOT NULL,
    knowledge_revision_id TEXT,          -- head KM at the decision (accept only)
    idempotency_key TEXT,
    new_proposal_job_id TEXT,            -- set only for request_changes
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS v2_change_result_decisions_apply_uq
    ON v2_change_result_decisions (tenant_id, workspace_id, apply_job_id);
CREATE UNIQUE INDEX IF NOT EXISTS v2_change_result_decisions_id_uq
    ON v2_change_result_decisions (tenant_id, workspace_id, decision_id);

-- Durable proof of REQUIRED manual work (the immutable machine report keeps
-- `proof: null`; the completion gate reads this table). One proof per
-- (apply job, manual item index); first writer wins (idempotent).
CREATE TABLE IF NOT EXISTS v2_change_work_proofs (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    apply_job_id TEXT NOT NULL,
    work_index INTEGER NOT NULL,
    evidence_ref TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, apply_job_id, work_index)
);
