-- `v2_index_builds` gains the
-- revision's SOURCE parse/admission coverage (distinct from retrieval
-- readiness itself): a query against a READY index must still be able to
-- tell a reader the source it was built from was not fully parsed.
ALTER TABLE v2_index_builds ADD COLUMN IF NOT EXISTS
    source_coverage_complete BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE v2_index_builds ADD COLUMN IF NOT EXISTS
    source_coverage_reason TEXT;
