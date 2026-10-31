-- v2_044 (REL-06, 2026-10-31): the per-subject key history behind text envelopes. One row per event (a key created for a data subject; the same
-- key marked shredded). Append-only like the ledger tables it protects: a shred is a new row, never an update, so a restore of an older backup
-- followed by a replay of these rows reaches the same erased state. `wrapped_key` is the subject key encrypted by the deployment key
-- (RIPPLE_DATA_KEY); a shredded row carries an empty wrapped_key. Additive; unused while RIPPLE_TEXT_ENVELOPE is off.
CREATE TABLE IF NOT EXISTS v2_subject_keys (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    wrapped_key TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('created', 'shredded')),
    reason TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL DEFAULT 'system',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    shredded_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS v2_subject_keys_by_subject ON v2_subject_keys (tenant_id, workspace_id, subject_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS v2_subject_keys_by_key ON v2_subject_keys (tenant_id, workspace_id, key_id);
DROP TRIGGER IF EXISTS v2_subject_keys_immutable ON v2_subject_keys;
CREATE TRIGGER v2_subject_keys_immutable BEFORE UPDATE OR DELETE ON v2_subject_keys
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
