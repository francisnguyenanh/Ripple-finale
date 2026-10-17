-- Extension FB-A1: links between an external identity (issuer + project + subject, optionally an identity tenant) and ONE Ripple account. Invitation/admin mapping only:
-- a link is created by a person with membership.manage, never by the sign-in itself; a revoked link stays as history (revoked_at). The exchange issues an ordinary Ripple
-- session for the linked account; authorization stays with v2_memberships (SQL), never with claims of the external token. No token, refresh token or key is stored.
CREATE TABLE IF NOT EXISTS v2_external_identities (
    issuer TEXT NOT NULL,
    project_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    identity_tenant TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL REFERENCES v2_accounts (account_id),
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    linked_by TEXT NOT NULL,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revoked_by TEXT,
    PRIMARY KEY (issuer, project_id, identity_tenant, subject)
);
CREATE INDEX IF NOT EXISTS v2_external_identities_account_idx ON v2_external_identities (account_id);
