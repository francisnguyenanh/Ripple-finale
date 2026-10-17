-- Versioned Intent rows produced by `intent.parse` and
-- confirmed/rejected/marked stale by `intent.confirm`.
--
-- One row per minted `intent_id` (its OWN id space -- never the
-- same space as `claim_id`/`conflict_id` on `v2_decisions`). `parsed_*`
-- columns pin the exact source/knowledge/decision snapshot `intent.parse`
-- observed, so `intent.confirm` can recheck each field independently
-- against the CURRENT `v2_workspaces` head instead of trusting the
-- pinned snapshot as still current. Idempotency reuses the existing generic
-- `v2_idempotency`/`v2_audit`/`v2_outbox` machinery under operations
-- "intent.parse"/"intent.confirm", exactly like every other mutating v2
-- store method -- no separate idempotency table is introduced here.
CREATE TABLE IF NOT EXISTS v2_intents (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PARSED',
    raw_request TEXT,
    structured_form JSONB,
    target TEXT,
    value JSONB,
    scope JSONB,
    exclusions JSONB,
    effective_time JSONB,
    questions JSONB,
    unsupported_reason TEXT,
    injection_detected BOOLEAN NOT NULL DEFAULT false,
    parsed_source_revision_id TEXT,
    parsed_knowledge_revision_id TEXT,
    parsed_decision_epoch INTEGER NOT NULL,
    parsed_version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    confirmed_actor_id TEXT,
    confirmed_at TIMESTAMPTZ,
    digest TEXT,
    stale_reasons JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS v2_intents_intent_id_idx
    ON v2_intents (tenant_id, workspace_id, intent_id);
