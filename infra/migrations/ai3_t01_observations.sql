-- Observations parsed from a committed source revision's artifacts.
-- Additive only. One row per parsed fact; immutable once written (a
-- revision's observations never change after the ingest job that wrote
-- them). A re-ingest of the SAME revision (parser_version bump) writes
-- observations under the artifact/kind but a fresh observation_id, so old
-- rows are never edited in place -- see PostgresV2Store.record_source_observations.

CREATE TABLE IF NOT EXISTS v2_source_observations (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    source_revision_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    locator JSONB NOT NULL,
    raw_value JSONB,
    fields JSONB NOT NULL DEFAULT '{}'::jsonb,
    parser_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, source_revision_id, artifact_id, observation_id),
    FOREIGN KEY (tenant_id, workspace_id, source_revision_id, artifact_id)
        REFERENCES v2_artifact_revisions (tenant_id, workspace_id, source_revision_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS v2_source_observations_scope_idx
    ON v2_source_observations (tenant_id, workspace_id, source_revision_id, kind);

-- A rolling deploy can have an old worker inserting the positional ID while
-- a new worker inserts the versioned ID for the same logical slot. The new
-- worker's SELECT cannot see an uncommitted old row. Both binaries, however,
-- pass through this trigger. Lock the logical slot at INSERT time, then check
-- both ID spellings after the preceding transaction has committed. Installing
-- the trigger also waits for in-flight table writers, providing a cutover.
CREATE OR REPLACE FUNCTION v2_source_observation_deduplicate() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    slot_index TEXT;
    legacy_id TEXT;
    versioned_id TEXT;
BEGIN
    slot_index := substring(NEW.observation_id FROM '-([0-9]{6,})$');
    IF slot_index IS NULL THEN
        RETURN NEW;
    END IF;
    legacy_id := 'obs-' || NEW.artifact_id || '-' || slot_index;
    versioned_id := 'obs-' || NEW.artifact_id || '-' || NEW.parser_version || '-' || slot_index;
    IF NEW.observation_id NOT IN (legacy_id, versioned_id) THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        concat_ws(E'\x1f', NEW.tenant_id, NEW.workspace_id,
                  NEW.source_revision_id, NEW.artifact_id,
                  NEW.parser_version, slot_index), 0));
    IF EXISTS (
        SELECT 1 FROM v2_source_observations
        WHERE tenant_id=NEW.tenant_id AND workspace_id=NEW.workspace_id
          AND source_revision_id=NEW.source_revision_id
          AND artifact_id=NEW.artifact_id AND parser_version=NEW.parser_version
          AND observation_id IN (legacy_id, versioned_id)
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS v2_source_observation_deduplicate_insert ON v2_source_observations;
CREATE TRIGGER v2_source_observation_deduplicate_insert
    BEFORE INSERT ON v2_source_observations
    FOR EACH ROW EXECUTE FUNCTION v2_source_observation_deduplicate();

DROP TRIGGER IF EXISTS v2_source_observation_immutable ON v2_source_observations;
CREATE TRIGGER v2_source_observation_immutable BEFORE UPDATE OR DELETE ON v2_source_observations
    FOR EACH ROW EXECUTE FUNCTION v2_reject_immutable_change();
