"""Acceptance tests against a real, disposable PostgreSQL database."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import time
from uuid import uuid4

import pytest
import psycopg2

from infra.adapters.v2_postgres import (
    BlobIntegrityError, LeaseLost, PostgresV2Store, StaleVersion, StoreConflict,
    TenantScopeError,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("AI1_T03_TEST_DSN"),
    reason="AI1-T03 integration needs a disposable PostgreSQL database",
)


class MemoryBlobs:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put_immutable(self, digest: str, content: bytes) -> None:
        self.data.setdefault(digest, content)

    def read(self, digest: str) -> bytes:
        return self.data[digest]


@pytest.fixture
def store():
    dsn = os.environ["AI1_T03_TEST_DSN"]
    blobs = MemoryBlobs()
    repository = PostgresV2Store(dsn, blobs)
    repository.migrate()
    tenant = "t-" + uuid4().hex
    workspace = "w-" + uuid4().hex
    repository.create_workspace(tenant, "p1", workspace)
    return repository, blobs, tenant, workspace


def _publish(store, tenant, workspace, *, key="k1", expected=0, revision="s1", digest=None,
             before_commit=None):
    digest = digest or store.stage_blob(b"original")
    return store.publish_source(
        tenant, workspace, revision, {"artifact-a": digest},
        expected_version=expected, operation="source.import.commit",
        idempotency_key=key, before_commit=before_commit,
    )


def test_stage_immutable_blob_and_publish_source_with_outbox(store):
    repo, blobs, tenant, workspace = store
    digest = repo.stage_blob(b"original")
    result = _publish(repo, tenant, workspace, digest=digest)
    assert result.version == 1
    assert repo.head(tenant, workspace).source_revision_id == "s1"
    assert repo.read_blob(tenant, workspace, "s1", "artifact-a") == b"original"
    assert repo.pending_outbox(tenant, workspace) == 1


def test_same_idempotency_key_replays_and_different_payload_conflicts(store):
    repo, _, tenant, workspace = store
    first = _publish(repo, tenant, workspace)
    second = _publish(repo, tenant, workspace)
    assert second.replayed and second.version == first.version
    assert repo.pending_outbox(tenant, workspace) == 1
    with pytest.raises(StoreConflict):
        _publish(repo, tenant, workspace, revision="other")


def test_competing_cas_commit_has_one_winner(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace, key="k1", revision="s1")
    _publish(repo, tenant, workspace, key="k2", revision="s2", expected=1)
    with pytest.raises(StaleVersion):
        _publish(repo, tenant, workspace, key="k3", revision="s3", expected=1)
    assert repo.head(tenant, workspace).source_revision_id == "s2"


def test_failure_before_commit_leaves_head_and_outbox_unchanged(store):
    repo, _, tenant, workspace = store
    def fail():
        raise RuntimeError("simulated crash")
    with pytest.raises(RuntimeError, match="simulated crash"):
        _publish(repo, tenant, workspace, before_commit=fail)
    assert repo.head(tenant, workspace).version == 0
    assert repo.pending_outbox(tenant, workspace) == 0
    assert _publish(repo, tenant, workspace).version == 1


def test_corrupted_blob_cannot_become_current(store):
    repo, blobs, tenant, workspace = store
    digest = repo.stage_blob(b"original")
    blobs.data[digest] = b"tampered"
    with pytest.raises(BlobIntegrityError):
        _publish(repo, tenant, workspace, digest=digest)
    assert repo.head(tenant, workspace).version == 0


def test_tenant_scope_and_restart_persist_head(store):
    repo, blobs, tenant, workspace = store
    _publish(repo, tenant, workspace)
    restarted = PostgresV2Store(repo.dsn, blobs)
    assert restarted.head(tenant, workspace).source_revision_id == "s1"
    with pytest.raises(TenantScopeError):
        restarted.head("foreign-tenant", workspace)
    with pytest.raises(TenantScopeError):
        restarted.read_blob("foreign-tenant", workspace, "s1", "artifact-a")


def test_outbox_lease_heartbeat_and_fencing(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    first = repo.claim_outbox(tenant, workspace, "worker-1", lease_seconds=30)
    assert first is not None and first.tenant_id == tenant
    assert repo.claim_outbox(tenant, workspace, "worker-2", lease_seconds=30) is None
    repo.heartbeat_outbox(tenant, workspace, first.id, "worker-1",
                          first.fencing_token, lease_seconds=30)
    with pytest.raises(LeaseLost):
        repo.ack_outbox(tenant, workspace, first.id, "worker-2", first.fencing_token)
    with pytest.raises(LeaseLost):
        repo.heartbeat_outbox("foreign-tenant", workspace, first.id,
                              "worker-1", first.fencing_token)
    with pytest.raises(LeaseLost):
        repo.ack_outbox("foreign-tenant", workspace, first.id,
                        "worker-1", first.fencing_token)
    repo.ack_outbox(tenant, workspace, first.id, "worker-1", first.fencing_token)
    assert repo.pending_outbox(tenant, workspace) == 0


def test_knowledge_promotion_checks_source_and_decision_epoch(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    with pytest.raises(StaleVersion):
        repo.publish_knowledge(tenant, workspace, "km1", "wrong-source", 0,
                               expected_version=1, idempotency_key="km-k")
    result = repo.publish_knowledge(tenant, workspace, "km1", "s1", 0,
                                    expected_version=1, idempotency_key="km-k")
    assert result.version == 2
    assert repo.head(tenant, workspace).knowledge_revision_id == "km1"


def test_real_concurrent_source_commits_only_one_wins(store):
    repo, _, tenant, workspace = store
    digest = repo.stage_blob(b"racing")
    def attempt(revision):
        try:
            return _publish(repo, tenant, workspace, key=revision,
                            revision=revision, digest=digest)
        except StaleVersion as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("race-a", "race-b")))
    assert sum(isinstance(result, StaleVersion) for result in results) == 1
    assert repo.head(tenant, workspace).version == 1
    assert repo.pending_outbox(tenant, workspace) == 1


def test_retry_after_commit_returns_original_result_without_second_event(store):
    repo, _, tenant, workspace = store
    original = _publish(repo, tenant, workspace)
    restarted = PostgresV2Store(repo.dsn, repo.blobs)
    replay = _publish(restarted, tenant, workspace)
    assert replay.replayed and replay.version == original.version
    assert restarted.pending_outbox(tenant, workspace) == 1


def test_decision_is_append_only_idempotent_and_stales_knowledge(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    repo.publish_knowledge(tenant, workspace, "km1", "s1", 0,
                           expected_version=1, idempotency_key="km-k")
    decision = repo.record_decision(tenant, workspace, "d1", "human-a",
                                    {"action": "confirm"}, expected_version=2,
                                    idempotency_key="decision-k")
    assert decision.version == 3
    assert repo.head(tenant, workspace).decision_epoch == 1
    assert repo.head(tenant, workspace).knowledge_revision_id is None
    assert repo.record_decision(tenant, workspace, "d1", "human-a",
                                {"action": "confirm"}, expected_version=2,
                                idempotency_key="decision-k").replayed
    with pytest.raises(StoreConflict):
        repo.record_decision(tenant, workspace, "d2", "human-a",
                             {"action": "reject"}, expected_version=2,
                             idempotency_key="decision-k")


def test_foreign_tenant_revision_cannot_be_promoted(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    foreign = "foreign-" + uuid4().hex
    repo.create_workspace(foreign, "p1", workspace)
    with pytest.raises(StaleVersion):
        repo.publish_knowledge(foreign, workspace, "km-foreign", "s1", 0,
                               expected_version=0, idempotency_key="k")


def test_composite_foreign_key_rejects_cross_tenant_artifact_reference(store):
    repo, _, tenant, workspace = store
    digest = repo.stage_blob(b"tenant-private")
    _publish(repo, tenant, workspace, digest=digest)
    foreign = "foreign-" + uuid4().hex
    repo.create_workspace(foreign, "p1", workspace)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with psycopg2.connect(repo.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO v2_artifact_revisions
                    (tenant_id,workspace_id,source_revision_id,artifact_id,blob_hash)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (foreign, workspace, "s1", "artifact-a", digest))


def test_job_pin_foreign_key_rejects_cross_tenant_source(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    foreign = "foreign-" + uuid4().hex
    repo.create_workspace(foreign, "p1", workspace)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with psycopg2.connect(repo.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO v2_jobs
                    (tenant_id,workspace_id,job_id,kind,status,
                     pinned_source_revision_id,pinned_decision_epoch,payload)
                    VALUES (%s,%s,%s,'ANALYSE','QUEUED',%s,0,'{}'::jsonb)""",
                    (foreign, workspace, "bad-job", "s1"))


def test_blob_retention_and_scoped_audit(store):
    repo, _, tenant, workspace = store
    digest = repo.stage_blob(b"retained")
    _publish(repo, tenant, workspace, digest=digest)
    repo.hold_blob(tenant, workspace, digest, "approval", "approval-1")
    assert repo.blob_is_retained(tenant, workspace, digest)
    assert repo.audit_events(tenant, workspace)[0]["event_type"] == "SOURCE_PUBLISHED"
    with pytest.raises(TenantScopeError):
        repo.hold_blob("foreign", workspace, digest, "approval", "approval-1")


def test_outbox_expired_lease_increments_fence_and_rejects_old_worker(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    first = repo.claim_outbox(tenant, workspace, "worker-1", lease_seconds=1)
    time.sleep(1.1)
    second = repo.claim_outbox(tenant, workspace, "worker-2", lease_seconds=30)
    assert second.fencing_token > first.fencing_token
    with pytest.raises(LeaseLost):
        repo.ack_outbox(tenant, workspace, first.id, "worker-1", first.fencing_token)
    repo.ack_outbox(tenant, workspace, second.id, "worker-2", second.fencing_token)


def test_exhausted_outbox_retry_becomes_failed_and_audited(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    claim = repo.claim_outbox(tenant, workspace, "worker-1", lease_seconds=30)
    with psycopg2.connect(repo.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE v2_outbox SET attempts=5,
                lease_until=now()-interval '1 second' WHERE id=%s""", (claim.id,))
    assert repo.reap_exhausted_outbox(tenant, workspace) == 1
    assert repo.outbox_status(tenant, workspace, claim.id) == "FAILED"
    assert repo.claim_outbox(tenant, workspace, "worker-2") is None
    assert any(event["event_type"] == "OUTBOX_FAILED"
               for event in repo.audit_events(tenant, workspace))


def test_job_creation_is_idempotent_and_pinned_to_current_head(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    created = repo.create_job(tenant, workspace, "job-1", "ANALYSE", {"scope": "all"},
                              expected_version=1, idempotency_key="job-key")
    assert created.revision_id == "job-1"
    assert repo.job_status(tenant, workspace, "job-1")["pinned_source_revision_id"] == "s1"
    assert repo.create_job(tenant, workspace, "job-1", "ANALYSE", {"scope": "all"},
                           expected_version=1, idempotency_key="job-key").replayed
    with pytest.raises(TenantScopeError):
        repo.job_status("foreign", workspace, "job-1")


def test_index_marker_is_exactly_versioned_and_optional(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    repo.publish_knowledge(tenant, workspace, "km1", "s1", 0,
                           expected_version=1, idempotency_key="km-k")
    assert not repo.current_index_ready(tenant, workspace, "v1")
    repo.record_index_manifest(tenant, workspace, "km1", "v1", "hash-1", "BUILDING",
                               idempotency_key="index-building")
    assert not repo.current_index_ready(tenant, workspace, "v1")
    repo.record_index_manifest(tenant, workspace, "km1", "v1", "hash-1", "READY",
                               idempotency_key="index-ready")
    assert repo.current_index_ready(tenant, workspace, "v1")
    with pytest.raises(StoreConflict):
        repo.record_index_manifest(tenant, workspace, "km1", "v1", "other-hash", "READY",
                                   idempotency_key="index-hash-conflict")
    with pytest.raises(StoreConflict):
        repo.record_index_manifest(tenant, workspace, "km1", "v1", "hash-1", "BUILDING",
                                   idempotency_key="index-regression")
    assert not repo.current_index_ready(tenant, workspace, "v2")
    repo.record_decision(tenant, workspace, "d1", "human-a", {"action": "confirm"},
                         expected_version=2, idempotency_key="d-k")
    assert not repo.current_index_ready(tenant, workspace, "v1")


def test_database_revisions_and_audit_are_immutable(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    for table, predicate in (
        ("v2_source_revisions", "revision_id='s1'"),
        ("v2_artifact_revisions", "artifact_id='artifact-a'"),
        ("v2_audit", "event_type='SOURCE_PUBLISHED'"),
    ):
        with pytest.raises(psycopg2.errors.RaiseException):
            with psycopg2.connect(repo.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table} WHERE tenant_id=%s AND workspace_id=%s AND {predicate}",
                                (tenant, workspace))
    assert repo.head(tenant, workspace).source_revision_id == "s1"


def test_stale_job_result_cannot_promote_knowledge(store):
    repo, _, tenant, workspace = store
    _publish(repo, tenant, workspace)
    repo.create_job(tenant, workspace, "job-1", "ANALYSE", {},
                    expected_version=1, idempotency_key="job-k")
    _publish(repo, tenant, workspace, key="next-source", expected=1, revision="s2")
    with pytest.raises(StaleVersion):
        repo.publish_knowledge(tenant, workspace, "km-stale", "s1", 0,
                               expected_version=2, idempotency_key="km-stale")
    assert repo.head(tenant, workspace).knowledge_revision_id is None
