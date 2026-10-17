-- Human work queue answer-draft records.
--
-- One durable row per committed answer draft, keyed by (tenant_id,
-- workspace_id, question_id, version_after) so the count of prior rows for a
-- (tenant, workspace, question_id) triple IS the question's current version
-- (0 = never answered). Concurrency is enforced by a transaction-scoped
-- advisory lock keyed by (tenant, workspace, question_id) in
-- `PostgresV2Store.hitl_record_answer`, taken before the count read, so two
-- concurrent requests against the same question serialize and cannot both
-- observe the same "current" count.
--
-- Idempotency reuses the existing generic `v2_idempotency`/`v2_audit`/
-- `v2_outbox` machinery (`_replay`/`_record_commit`) under operation
-- "hitl.answer", exactly like every other mutating v2 store method --
-- no separate idempotency table is introduced here.
CREATE TABLE IF NOT EXISTS v2_hitl_answers (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    answer_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    answer_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    version_after INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS v2_hitl_answers_question_idx
    ON v2_hitl_answers (tenant_id, workspace_id, question_id);

CREATE UNIQUE INDEX IF NOT EXISTS v2_hitl_answers_answer_id_idx
    ON v2_hitl_answers (tenant_id, workspace_id, answer_id);
