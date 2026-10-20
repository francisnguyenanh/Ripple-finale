-- RQA-02: private request-assistant threads and their immutable messages. A thread belongs to its creator for ONE request (family + id) in one tenant / workspace; nobody else
-- reads it (no role inherits another person's thread). A message stores the question, the context manifest hash and the reader's authorization fingerprint it was asked
-- under, the job that answers it and, once read back under the same authorization, the validated answer. Additive only; no existing table changes.
CREATE TABLE IF NOT EXISTS v2_request_threads (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    family TEXT NOT NULL CHECK (family IN ('intent_flow', 'business_change')),
    request_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, thread_id)
);
CREATE INDEX IF NOT EXISTS v2_request_threads_owner_idx ON v2_request_threads (tenant_id, workspace_id, created_by, family, request_id, created_at);

CREATE TABLE IF NOT EXISTS v2_request_messages (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    question TEXT NOT NULL,
    asked_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    job_id TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    auth JSONB NOT NULL,
    as_of TEXT NOT NULL,
    answer JSONB,
    answered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, message_id),
    UNIQUE (tenant_id, workspace_id, thread_id, seq),
    UNIQUE (tenant_id, workspace_id, thread_id, idempotency_key)
);
