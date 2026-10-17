-- Retrieval index -- a durable PROJECTION of v2_source_observations,
-- never the source of truth. Rebuildable from that table alone (no blob
-- reads, no process cache). Keyed by (tenant, workspace, source_revision_id)
-- so an older revision's rows are never mistaken for the current one; the
-- CALLER always resolves "current" via v2_workspaces.source_revision_id, and
-- readiness is checked per revision in v2_index_builds.

CREATE TABLE IF NOT EXISTS v2_retrieval_index (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    locator JSONB NOT NULL,
    parser_version TEXT NOT NULL,
    raw_value JSONB,
    -- Deterministic term-frequency vector over `raw_value`'s text, computed
    -- once at build time (ADR-005 defers the embedding technology choice; a
    -- real embedding provider needs shared budget/model policy wiring that is
    -- not present here). `embedding_version` lets a future real provider's rows
    -- coexist with, and be distinguished from, this deterministic baseline.
    term_vector JSONB NOT NULL,
    embedding_version TEXT NOT NULL DEFAULT 'termfreq-v1',
    -- 1-hop structural neighbors within the SAME artifact, precomputed so a
    -- structural query never needs an unbounded graph traversal (no cycle
    -- risk by construction).
    prev_observation_id TEXT,
    next_observation_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, source_revision_id, observation_id),
    FOREIGN KEY (tenant_id, workspace_id, source_revision_id, artifact_id, observation_id)
        REFERENCES v2_source_observations (tenant_id, workspace_id, source_revision_id,
                                           artifact_id, observation_id)
);
CREATE INDEX IF NOT EXISTS v2_retrieval_index_scope_idx
    ON v2_retrieval_index (tenant_id, workspace_id, source_revision_id);

-- Readiness marker: a build in progress is invisible to every reader until
-- it commits READY in the SAME transaction as the index rows themselves
-- (PostgresV2Store.build_retrieval_index), so a crash mid-build leaves no
-- partial index marked ready -- the prior READY revision (if any) is
-- untouched, and this revision's row (if any, from a prior failed attempt)
-- simply is not READY.
CREATE TABLE IF NOT EXISTS v2_index_builds (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('READY', 'FAILED')),
    observation_count INTEGER NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, source_revision_id)
);
