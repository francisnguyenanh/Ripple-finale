-- Extension FB-R1: the projector's durable checkpoint per (workspace, projector). The projection itself (minimal opaque hints) lives in the sink (Firestore when activated;
-- a memory sink locally) and is disposable: it can always be rebuilt from this checkpoint and the committed SQL rows. Mutable row, advanced only after the sink accepted the batch.
CREATE TABLE IF NOT EXISTS v2_projection_checkpoints (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    projector_id TEXT NOT NULL,
    outbox_watermark BIGINT NOT NULL DEFAULT 0,
    documents_watermark TIMESTAMPTZ NOT NULL DEFAULT 'epoch',
    revision BIGINT NOT NULL DEFAULT 0,
    projected_count BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, projector_id)
);
