-- v2_045 (REL-06 review fix, 2026-10-31): key material leaves the append-only history.
-- Review finding: the 'created' row of v2_subject_keys kept the wrapped subject key after a shred, so the deployment key could still unwrap it; an
-- append-only tombstone is not cryptographic erasure. From this migration on, the wrapped key lives only in v2_subject_key_material (mutable,
-- one row per key, DELETEd by the shred in the same transaction as the history row). v2_subject_keys stays append-only and carries no key
-- material (wrapped_key is '' on every row); it remains the auditable history and the replayable tombstone list.
-- Existing material (only possible in a deployment that ran C20 with the envelope on; none did) is moved, then blanked in the history under a
-- temporarily disabled immutability trigger: this is the one deliberate rewrite of that table, recorded here.
CREATE TABLE IF NOT EXISTS v2_subject_key_material (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    wrapped_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, key_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS v2_subject_key_material_one_live_key_per_subject ON v2_subject_key_material (tenant_id, workspace_id, subject_id);
INSERT INTO v2_subject_key_material (tenant_id, workspace_id, subject_id, key_id, wrapped_key, created_at)
    SELECT k.tenant_id, k.workspace_id, k.subject_id, k.key_id, k.wrapped_key, k.created_at FROM v2_subject_keys k
    WHERE k.action = 'created' AND k.wrapped_key <> ''
      AND NOT EXISTS (SELECT 1 FROM v2_subject_keys s WHERE s.tenant_id = k.tenant_id AND s.workspace_id = k.workspace_id AND s.key_id = k.key_id AND s.action = 'shredded')
    ON CONFLICT DO NOTHING;
ALTER TABLE v2_subject_keys DISABLE TRIGGER v2_subject_keys_immutable;
UPDATE v2_subject_keys SET wrapped_key = '' WHERE wrapped_key <> '';
ALTER TABLE v2_subject_keys ENABLE TRIGGER v2_subject_keys_immutable;
