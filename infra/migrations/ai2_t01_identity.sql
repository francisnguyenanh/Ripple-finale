-- Private account provisioning, revocable sessions and scoped membership.
-- Credentials are opaque server-provisioned tokens. Only their digests are stored.

CREATE TABLE IF NOT EXISTS v2_accounts (
    account_id TEXT PRIMARY KEY,
    credential_digest TEXT NOT NULL,
    credential_expires_at TIMESTAMPTZ,
    disabled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS v2_auth_sessions (
    token_digest TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES v2_accounts (account_id),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    selected_tenant_id TEXT,
    selected_project_id TEXT,
    selected_workspace_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (selected_tenant_id, selected_project_id, selected_workspace_id)
        REFERENCES v2_workspaces (tenant_id, project_id, workspace_id)
);
CREATE INDEX IF NOT EXISTS v2_auth_sessions_account_idx
    ON v2_auth_sessions (account_id, expires_at);

CREATE TABLE IF NOT EXISTS v2_memberships (
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES v2_accounts (account_id),
    roles TEXT[] NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, workspace_id, account_id),
    FOREIGN KEY (tenant_id, project_id, workspace_id)
        REFERENCES v2_workspaces (tenant_id, project_id, workspace_id),
    CHECK (cardinality(roles) > 0)
);
CREATE INDEX IF NOT EXISTS v2_memberships_account_idx
    ON v2_memberships (account_id, tenant_id, workspace_id);
