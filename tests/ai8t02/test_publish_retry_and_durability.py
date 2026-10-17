"""Retry and durability: retrying the same apply key never re-publishes/double-writes;
a crash after commit but before ack keeps the source published and the
rebuild-intent/outbox durable.
"""

from __future__ import annotations

from uuid import uuid4

from tests.ai8t01.conftest import login, submit_claim_and_get_apply
from tests.ai8t02.conftest import build_ready_to_publish_yaml


def test_u3_retry_same_apply_job_id_returns_prior_result_no_double_publish(
        setup, monkeypatch, owner):
    """Retry of the same apply key never re-runs/re-publishes, at BOTH
    idempotency layers: (1) job creation replay (`store.create_job`, same
    ORIGINAL `expected_version`), (2) `publish_source`'s own
    `apply.publish` ledger. A payload that differs (wrong expected_version /
    different manifest) is an `idempotency_conflict`, never silently
    accepted."""
    import pytest
    from infra.adapters.v2_postgres import StoreConflict
    from tests.ai8t02.conftest import publish_footprint

    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    store, tenant = setup["store"], setup["tenant"]

    job_id = f"job-apply-{uuid4().hex}"
    from tests.ai8t01.conftest import submit_apply_job, claim_apply_job
    from tests.ai7t01.conftest import get_job_status

    # The ORIGINAL request bound this (pre-publish) head version; a replay
    # hash includes it, so the replay must reuse it, not re-read post-publish.
    original_expected_version = store.head(tenant, workspace).version
    resp = submit_apply_job(
        client, workspace, approval_job_id=ready["approval_job_id"], job_id=job_id,
        expected_approval_digest=ready["digest"])
    assert resp.status_code == 200, resp.text
    claim_apply_job(setup, workspace, worker_id="t02-retry-worker-1")
    first = get_job_status(client, workspace, job_id)
    assert first["execution_status"] == "COMPLETED", first
    assert first["execution_result"]["published"] is True

    head_after_first = store.head(tenant, workspace)
    assert head_after_first.version == original_expected_version + 1
    footprint_after_first = publish_footprint(store, tenant, workspace)
    payload = {"approval_job_id": ready["approval_job_id"],
               "expected_approval_digest": ready["digest"]}

    # (1) job-create replay with the original binding: prior job, no re-run.
    store.create_job(
        tenant, workspace, job_id, kind="apply", payload=payload,
        expected_version=original_expected_version,
        idempotency_key=f"job-create-{job_id}")
    # ... and a replay bound to a different head version is a conflict.
    with pytest.raises(StoreConflict):
        store.create_job(
            tenant, workspace, job_id, kind="apply", payload=payload,
            expected_version=head_after_first.version,
            idempotency_key=f"job-create-{job_id}")

    second = get_job_status(client, workspace, job_id)
    assert second["execution_result"] == first["execution_result"]

    # (2) publish-ledger replay: same key + same payload => the FIRST commit's
    # result, no new head; different payload under the key => conflict.
    result = first["execution_result"]
    staged = result["staged_manifest"]
    publish_key = f"job-apply-publish-{job_id}"
    replayed = store.publish_source(
        tenant, workspace, result["published_revision_id"], staged,
        expected_version=original_expected_version, operation="apply.publish",
        idempotency_key=publish_key)
    assert replayed.revision_id == result["published_revision_id"]
    assert replayed.version == head_after_first.version
    with pytest.raises(StoreConflict):
        store.publish_source(
            tenant, workspace, "rev-apply-" + uuid4().hex, staged,
            expected_version=original_expected_version, operation="apply.publish",
            idempotency_key=publish_key)

    assert store.head(tenant, workspace) == head_after_first, "double publish"
    assert publish_footprint(store, tenant, workspace) == footprint_after_first


def test_u3_crash_after_commit_before_ack_keeps_source_published_and_outbox_durable(
        setup, monkeypatch, owner):
    """Once the publish transaction has genuinely committed, a crash before
    any downstream ack/rebuild-consumer runs must NOT roll back or lose the
    publish, and the durable outbox event this publish enqueues (for T03's
    rebuild worker) must still be claimable afterwards -- "state pending
    verification ro rang", not silently dropped."""
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])

    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    store, tenant = setup["store"], setup["tenant"]
    head_before = store.head(tenant, workspace)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready["approval_job_id"],
        expected_approval_digest=ready["digest"])
    assert body["execution_status"] == "COMPLETED", body

    head_after = store.head(tenant, workspace)
    assert head_after.version == head_before.version + 1, (
        "publish did not happen at all -- cannot test post-commit crash "
        "durability without a real commit having occurred first")

    # "Crash before ack" == nothing ever calls drain_outbox/claim_outbox for
    # this event; the durable outbox row must still be there, claimable,
    # on a later restart-equivalent claim attempt (never lost, never
    # silently pre-acked by the publish transaction itself).
    claim = store.claim_outbox(tenant, workspace, "t02-rebuild-worker", lease_seconds=5)
    assert claim is not None, (
        "no durable outbox event survives past the apply/publish commit -- "
        "a crash before the rebuild consumer acks would lose the publish "
        "notification entirely")
    store.ack_outbox(tenant, workspace, claim.id, "t02-rebuild-worker", claim.fencing_token)

    # The source itself must remain published regardless of the outbox/ack
    # outcome -- never rolled back by a downstream consumer's own failure.
    head_final = store.head(tenant, workspace)
    assert head_final.source_revision_id == head_after.source_revision_id
