-- UX: free-text request conversations (chat -> clarification -> structured understanding -> confirmed intent).
-- ADDITIVE ONLY (CREATE TABLE IF NOT EXISTS); no existing table or row is touched. Conversations are scoped by
-- (tenant, workspace) and readable only by their creator and by accounts that may confirm knowledge (see infra/v2_chat_store.py).
-- Turn text is what the person typed and what the AI answered; it never contains evidence quotes.
CREATE TABLE IF NOT EXISTS v2_chat_conversations (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'OPEN',
    ai_allowance INTEGER NOT NULL DEFAULT 0,
    ai_used INTEGER NOT NULL DEFAULT 0,
    ai_approved_by TEXT,
    ai_approved_at TIMESTAMPTZ,
    pinned_source_revision_id TEXT,
    pinned_decision_epoch BIGINT,
    PRIMARY KEY (tenant_id, workspace_id, conversation_id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES v2_workspaces (tenant_id, workspace_id)
);

ALTER TABLE v2_chat_conversations ADD COLUMN IF NOT EXISTS intent_id TEXT;
ALTER TABLE v2_chat_conversations ADD COLUMN IF NOT EXISTS previous_intent_id TEXT;
ALTER TABLE v2_chat_conversations ADD COLUMN IF NOT EXISTS ai_expires_at TIMESTAMPTZ;
ALTER TABLE v2_chat_conversations ADD COLUMN IF NOT EXISTS ai_revoked_at TIMESTAMPTZ;
ALTER TABLE v2_chat_conversations ADD COLUMN IF NOT EXISTS ai_revoked_by TEXT;

CREATE TABLE IF NOT EXISTS v2_chat_turns (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    understanding JSONB,
    understanding_version INTEGER,
    job_id TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, workspace_id, conversation_id, seq),
    FOREIGN KEY (tenant_id, workspace_id, conversation_id)
        REFERENCES v2_chat_conversations (tenant_id, workspace_id, conversation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS v2_chat_turns_job_uq
    ON v2_chat_turns (tenant_id, workspace_id, conversation_id, job_id) WHERE job_id IS NOT NULL AND role = 'assistant';
