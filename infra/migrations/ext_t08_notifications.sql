-- Extension FB-N1 / FS-I1: durable notification intents derived from committed business events (a confirmed change request waiting for review), the per-recipient inbox items
-- and read receipts (the full path when push is refused or absent), registered device/browser tokens (routing data: never returned by any API, never logged) and the
-- per-(intent, token) delivery records (idempotent sends with bounded attempts). A read receipt is a UX fact, never an approval. Additive only.
CREATE TABLE IF NOT EXISTS v2_notification_intents (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENT', 'STALE', 'FAILED')),
    recipients JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, workspace_id, intent_id)
);
CREATE TABLE IF NOT EXISTS v2_notification_tokens (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    token_digest TEXT NOT NULL,
    token TEXT NOT NULL,
    platform TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    invalid_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, workspace_id, token_digest)
);
CREATE INDEX IF NOT EXISTS v2_notification_tokens_account_idx ON v2_notification_tokens (tenant_id, workspace_id, account_id);
CREATE TABLE IF NOT EXISTS v2_notification_deliveries (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    token_digest TEXT NOT NULL,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENT', 'INVALID_TOKEN', 'FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, intent_id, token_digest)
);
CREATE TABLE IF NOT EXISTS v2_inbox_items (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, workspace_id, item_id)
);
CREATE INDEX IF NOT EXISTS v2_inbox_items_account_idx ON v2_inbox_items (tenant_id, workspace_id, account_id, created_at);
