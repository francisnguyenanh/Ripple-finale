-- Final knowledge loop: human decisions on a CONFIRMED business change request (CHG-04).
-- kind = knowledge     : accept / reject / request_changes the semantic change (bound to the draft the person saw: semantic_diff_hash, draft_hash, pin)
-- kind = file_writes   : approve / reject the AI_EDIT_SUPPORTED proposal edits of that draft (bound to edits_hash); never implied by a knowledge decision
-- kind = clarification : a person's disposition or answer for one open item of the draft (ref, disposition, answer); it revises the draft (new draft_hash)
-- A decision changes no rule, snapshot, head, epoch or source: it is a ledger row plus an audit event. Immutable once written. Additive only.
-- Not v2_business_decisions (those belong to a snapshot and appear in its view) and not v2_change_decisions (keyed by a Pitch approval job, terminal once per job).
CREATE TABLE IF NOT EXISTS v2_business_change_decisions (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('knowledge', 'file_writes', 'clarification')),
    action TEXT NOT NULL CHECK (action IN ('approve', 'reject', 'request_changes', 'clarify')),
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    semantic_diff_hash TEXT NOT NULL,
    draft_hash TEXT NOT NULL,
    pin JSONB NOT NULL,
    binding JSONB NOT NULL,
    self_approval BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_key TEXT,
    payload_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, decision_id),
    FOREIGN KEY (tenant_id, workspace_id, intent_id)
        REFERENCES v2_business_change_intents (tenant_id, workspace_id, intent_id)
);
-- At most one TERMINAL decision (approve/reject) per intent, kind and draft version: a revised draft may be decided again, the same version may not.
CREATE UNIQUE INDEX IF NOT EXISTS v2_business_change_decisions_terminal_uq
    ON v2_business_change_decisions (tenant_id, workspace_id, intent_id, kind, draft_hash)
    WHERE action IN ('approve', 'reject');
CREATE UNIQUE INDEX IF NOT EXISTS v2_business_change_decisions_idem_uq
    ON v2_business_change_decisions (tenant_id, workspace_id, intent_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS v2_business_change_decisions_intent_idx
    ON v2_business_change_decisions (tenant_id, workspace_id, intent_id, created_at);
DROP TRIGGER IF EXISTS v2_business_change_decision_immutable ON v2_business_change_decisions;
CREATE TRIGGER v2_business_change_decision_immutable BEFORE UPDATE OR DELETE ON v2_business_change_decisions
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
