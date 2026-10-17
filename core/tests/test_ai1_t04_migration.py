"""Legacy import and release rollback contract.

The reader represents the legacy persisted boundary, not a v2 writer.  Its
load_bundle(session_id) method returns session, baseline_artifacts,
knowledge_records, and lineage_entries.  Database cases require a disposable
PostgreSQL instance via AI1_T03_TEST_DSN.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import psycopg2
import pytest

from infra.adapters.v2_postgres import PostgresV2Store
from infra.migrations.legacy_v2 import LegacyV2Migrator


pytestmark = pytest.mark.skipif(
    not os.environ.get("AI1_T03_TEST_DSN"),
    reason="AI1-T04 integration requires disposable PostgreSQL DSN",
)


class MemoryBlobs:
    def __init__(self):
        self.data = {}

    def put_immutable(self, digest, content):
        if digest in self.data and self.data[digest] != content:
            raise ValueError("immutable blob changed")
        self.data[digest] = content

    def read(self, digest):
        return self.data[digest]


class LegacyReader:
    def __init__(self, bundle):
        self.bundle = bundle
        self.calls = []

    def load_bundle(self, session_id):
        self.calls.append(session_id)
        if session_id != self.bundle["session"]["session_id"]:
            raise KeyError(session_id)
        return self.bundle


@pytest.fixture
def legacy_bundle():
    return {
        "session": {
            "session_id": "SESSION-0042", "baseline_id": "baseline-legacy",
            "created_at": "2026-01-01T00:00:00Z", "durable_version": 7,
            "work": {"reset_epoch": 4, "_counter": 17},
            "source_revision_id": None, "knowledge_revision_id": None,
        },
        "baseline_artifacts": {
            "workflow/routes.yaml": b"approval: 500000\n",
            "docs/policy.txt": b"A human approves the threshold.\n",
        },
        "knowledge_records": [{
            "id": "legacy-rule-1", "knowledge_type": "rule",
            "semantic_id": "approval.threshold", "statement": "Approve above 500000",
            "evidence": [{"artifact": "docs/policy.txt", "quote": "A human approves"}],
            "derived_from": ["docs/policy.txt"], "state": "VERIFIED",
            "alignment": "ALIGNED", "representations": [], "gap_id": "",
            "reason": "inferred by legacy pipeline", "km_version": 3,
        }],
        "lineage_entries": [{
            "version": 1, "snapshot_id": "legacy-snapshot-1",
            "parent_snapshot_id": None, "changeset_id": "legacy-change-1",
            "verified_by": "legacy-reviewer", "verified_at": "2026-01-02T00:00:00Z",
            "artifacts": ["workflow/routes.yaml"],
        }],
    }


@pytest.fixture
def migration(legacy_bundle):
    dsn = os.environ["AI1_T03_TEST_DSN"]
    blobs = MemoryBlobs()
    store = PostgresV2Store(dsn, blobs)
    store.migrate()
    tenant, workspace = "t-" + uuid4().hex, "w-" + uuid4().hex
    store.create_workspace(tenant, "p1", workspace)
    reader = LegacyReader(legacy_bundle)
    return LegacyV2Migrator(store, reader), store, blobs, reader, tenant, workspace


def _digest(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _counts(dsn, tenant, workspace):
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM v2_source_revisions WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            sources = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            decisions = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            outbox = cur.fetchone()[0]
    return sources, decisions, outbox


def test_dry_run_reports_exact_manifest_and_does_not_write(migration, legacy_bundle):
    migrator, store, blobs, reader, tenant, workspace = migration
    plan = migrator.dry_run(tenant, workspace, "SESSION-0042")
    assert plan.artifact_count == 2
    assert plan.blob_hashes == {
        path: _digest(raw) for path, raw in legacy_bundle["baseline_artifacts"].items()
    }
    assert plan.knowledge_count == 1
    assert plan.lineage_version == 1
    assert plan.legacy_session_id == "SESSION-0042"
    assert store.head(tenant, workspace).version == 0
    assert _counts(store.dsn, tenant, workspace) == (0, 0, 0)
    assert blobs.data == {}
    assert reader.calls == ["SESSION-0042"]


def test_commit_is_idempotent_and_verify_checks_exact_blob_bytes(migration, legacy_bundle):
    migrator, store, blobs, _, tenant, workspace = migration
    first = migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                            idempotency_key="migration-42")
    replay = migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                             idempotency_key="migration-42")
    assert first.source_revision_id == replay.source_revision_id
    assert replay.replayed
    assert _counts(store.dsn, tenant, workspace)[0] == 1
    assert _counts(store.dsn, tenant, workspace)[1] == 0
    assert len(blobs.data) == 2
    assert migrator.verify(tenant, workspace, "SESSION-0042").ok
    for artifact_id, raw in legacy_bundle["baseline_artifacts"].items():
        assert store.read_blob(tenant, workspace, first.source_revision_id, artifact_id) == raw


def test_commit_marks_legacy_provenance_without_human_confirmation(migration):
    migrator, store, _, _, tenant, workspace = migration
    result = migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                             idempotency_key="migration-42")
    with psycopg2.connect(store.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT provenance FROM v2_source_revisions WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s", (tenant, workspace, result.source_revision_id))
            provenance = cur.fetchone()[0]
            cur.execute("SELECT payload FROM v2_knowledge_revisions WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            payloads = [row[0] for row in cur.fetchall()]
            cur.execute("SELECT payload FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            audit = [row[0] for row in cur.fetchall()]
    assert result.source_revision_id == store.head(tenant, workspace).source_revision_id
    assert provenance["kind"] == "legacy_baseline"
    assert provenance["legacy_session_id"] == "SESSION-0042"
    assert any(item.get("legacy_session_id") == "SESSION-0042" for item in audit)
    assert any("legacy-rule-1" in str(item) for item in payloads)
    assert all("human_confirmed" not in str(item).lower() for item in payloads)


def test_legacy_lineage_and_counters_are_preserved_as_history(migration):
    migrator, store, _, _, tenant, workspace = migration
    migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                    idempotency_key="migration-42")
    history = migrator.read_legacy_history(tenant, workspace, "SESSION-0042")
    assert history["lineage_entries"][0]["version"] == 1
    assert history["lineage_entries"][0]["verified_by"] == "legacy-reviewer"
    assert history["legacy_work"]["reset_epoch"] == 4
    assert history["legacy_work"]["_counter"] == 17
    assert store.head(tenant, workspace).decision_epoch == 0


def test_tampered_legacy_source_cannot_replay_as_same_import(migration, legacy_bundle):
    migrator, store, _, reader, tenant, workspace = migration
    migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                    idempotency_key="migration-42")
    reader.bundle["baseline_artifacts"]["docs/policy.txt"] = b"tampered\n"
    with pytest.raises(ValueError):
        migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                        idempotency_key="migration-42")
    assert _counts(store.dsn, tenant, workspace)[0] == 1


def test_verify_fails_when_blob_corrupts_after_commit(migration):
    migrator, store, blobs, _, tenant, workspace = migration
    migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                    idempotency_key="migration-42")
    digest = next(iter(blobs.data))
    blobs.data[digest] = b"corrupt"
    assert not migrator.verify(tenant, workspace, "SESSION-0042").ok


def test_v2_feature_flag_can_be_disabled_without_erasing_decisions(migration):
    migrator, store, _, _, tenant, workspace = migration
    migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                    idempotency_key="migration-42")
    head = store.head(tenant, workspace)
    store.record_decision(tenant, workspace, "d1", "human-1", {"decision": "approve"},
                          expected_version=head.version, idempotency_key="d1")
    assert migrator.is_v2_enabled(tenant, workspace)
    migrator.disable_v2(tenant, workspace)
    assert not migrator.is_v2_enabled(tenant, workspace)
    assert _counts(store.dsn, tenant, workspace)[1] == 1
    assert migrator.read_legacy("SESSION-0042")["session"]["work"]["reset_epoch"] == 4


def test_unknown_session_and_arbitrary_artifact_upload_are_refused(migration):
    migrator, store, _, _, tenant, workspace = migration
    with pytest.raises((KeyError, ValueError)):
        migrator.dry_run(tenant, workspace, "SESSION-attacker")
    with pytest.raises((TypeError, ValueError)):
        migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                        idempotency_key="evil", artifacts={"evil.py": b"print(1)"})
    assert store.head(tenant, workspace).version == 0


def test_knowledge_publish_rejects_only_stale_decision_epoch(migration):
    """Hold source ID and workspace version fixed while epoch changes."""
    _, store, _, _, tenant, workspace = migration
    digest = store.stage_blob(b"baseline")
    store.publish_source(tenant, workspace, "source-1", {"a": digest},
                         expected_version=0, operation="source.import.commit",
                         idempotency_key="source-1")
    head = store.head(tenant, workspace)
    with psycopg2.connect(store.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE v2_workspaces SET decision_epoch=decision_epoch+1 "
                        "WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
    assert store.head(tenant, workspace).version == head.version
    assert store.head(tenant, workspace).source_revision_id == head.source_revision_id
    from infra.adapters.v2_postgres import StaleVersion
    with pytest.raises(StaleVersion):
        store.publish_knowledge(tenant, workspace, "km-1", head.source_revision_id,
                                head.decision_epoch, expected_version=head.version,
                                idempotency_key="km-1")


def test_migration_command_rejects_arbitrary_artifact_argument():
    command = [sys.executable, "-m", "infra.migrations.legacy_v2",
               "--artifact", "evil.py"]
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "unrecognized" in (result.stdout + result.stderr).lower()


def test_foreign_tenant_cannot_read_imported_history_or_rollout(migration):
    migrator, store, _, _, tenant, workspace = migration
    migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                    idempotency_key="migration-42")
    from infra.adapters.v2_postgres import TenantScopeError
    with pytest.raises(TenantScopeError):
        migrator.read_legacy_history("foreign-tenant", workspace, "SESSION-0042")
    with pytest.raises(TenantScopeError):
        migrator.is_v2_enabled("foreign-tenant", workspace)
    with pytest.raises(TenantScopeError):
        migrator.disable_v2("foreign-tenant", workspace)
    assert migrator.is_v2_enabled(tenant, workspace)


def test_crash_during_sql_commit_rolls_back_all_authoritative_rows(migration, monkeypatch):
    migrator, store, blobs, _, tenant, workspace = migration
    original = store._record_commit

    def crash(*args, **kwargs):
        raise RuntimeError("crash before transaction commit")

    monkeypatch.setattr(store, "_record_commit", crash)
    with pytest.raises(RuntimeError, match="crash before transaction commit"):
        migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                        idempotency_key="migration-42")
    assert store.head(tenant, workspace).version == 0
    assert _counts(store.dsn, tenant, workspace) == (0, 0, 0)
    with psycopg2.connect(store.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM v2_legacy_imports WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM v2_workspace_rollout WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            assert cur.fetchone()[0] == 0
    assert len(blobs.data) == 2  # immutable staging before SQL is recoverable
    monkeypatch.setattr(store, "_record_commit", original)
    assert migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                           idempotency_key="migration-42").version == 1


def test_disable_then_retry_replays_without_reenabling_v2(migration):
    migrator, store, _, _, tenant, workspace = migration
    first = migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                            idempotency_key="migration-42")
    migrator.disable_v2(tenant, workspace)
    replay = migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                             idempotency_key="migration-42")
    assert replay.replayed and replay.version == first.version
    assert not migrator.is_v2_enabled(tenant, workspace)
    assert _counts(store.dsn, tenant, workspace)[0] == 1


def test_legacy_verified_word_does_not_promote_human_authority(migration):
    migrator, store, _, _, tenant, workspace = migration
    migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                    idempotency_key="migration-42")
    with psycopg2.connect(store.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM v2_knowledge_revisions WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            payload = cur.fetchone()[0]
    assert payload["authority"] == "legacy_inferred_unconfirmed"
    assert payload["legacy_records"][0]["state"] == "VERIFIED"
    assert store.head(tenant, workspace).knowledge_revision_id is None
    assert store.head(tenant, workspace).decision_epoch == 0
    assert _counts(store.dsn, tenant, workspace)[1] == 0


def test_zero_knowledge_bundle_imports_source_without_phantom_km(migration, legacy_bundle):
    migrator, store, _, reader, tenant, workspace = migration
    reader.bundle["knowledge_records"] = []
    plan = migrator.dry_run(tenant, workspace, "SESSION-0042")
    assert plan.knowledge_count == 0
    migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                    idempotency_key="migration-42")
    with psycopg2.connect(store.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM v2_knowledge_revisions WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            assert cur.fetchone()[0] == 0
    assert store.head(tenant, workspace).knowledge_revision_id is None
    assert migrator.verify(tenant, workspace, "SESSION-0042").ok


def test_explicit_legacy_human_decision_is_history_not_new_authority(migration):
    migrator, store, _, reader, tenant, workspace = migration
    reader.bundle["human_decisions"] = [{
        "decision_id": "legacy-human-1", "actor_id": "reviewer-7",
        "decision": "approve", "reason": "signed legacy record",
        "decided_at": "2026-01-03T00:00:00Z",
    }]
    assert migrator.dry_run(tenant, workspace, "SESSION-0042").decision_count == 1
    migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                    idempotency_key="migration-42")
    with psycopg2.connect(store.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT human_decisions FROM v2_legacy_imports WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            historical = cur.fetchone()[0]
    assert historical == reader.bundle["human_decisions"]
    assert _counts(store.dsn, tenant, workspace)[1] == 0
    assert store.head(tenant, workspace).decision_epoch == 0
    assert migrator.verify(tenant, workspace, "SESSION-0042").ok
    reader.bundle["human_decisions"][0]["reason"] = "changed after import"
    assert not migrator.verify(tenant, workspace, "SESSION-0042").ok


def test_full_legacy_session_snapshot_preserves_created_at_and_unknown_fields(migration):
    migrator, store, _, reader, tenant, workspace = migration
    reader.bundle["session"]["legacy_extension"] = {"reviewed_by": "old-service"}
    migrator.commit(tenant, workspace, "SESSION-0042", expected_version=0,
                    idempotency_key="migration-42")
    with psycopg2.connect(store.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT legacy_session FROM v2_legacy_imports WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
            saved = cur.fetchone()[0]
    assert saved == reader.bundle["session"]
    assert saved["created_at"] == "2026-01-01T00:00:00Z"
    assert saved["source_revision_id"] is None
    assert saved["knowledge_revision_id"] is None
    assert migrator.verify(tenant, workspace, "SESSION-0042").ok
