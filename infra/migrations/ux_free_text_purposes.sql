-- UX: the same conversation machinery serves more free-text surfaces (knowledge question, GAP reply, draft feedback, what-if).
-- ADDITIVE ONLY: nullable/defaulted columns on the existing conversation table; no row is rewritten.
ALTER TABLE v2_chat_conversations ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'change_request';
ALTER TABLE v2_chat_conversations ADD COLUMN IF NOT EXISTS context JSONB;
ALTER TABLE v2_chat_conversations ADD COLUMN IF NOT EXISTS outcome JSONB;
