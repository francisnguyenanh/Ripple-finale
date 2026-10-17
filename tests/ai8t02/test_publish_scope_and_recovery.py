"""Additional publish requirements:

  * publication must not drop the OTHER artifacts of the current head
    ("Toan bo managed workspace publish hoac khong publish");
  * fencing: a worker that lost its lease mid-execution must not commit
    (a worker uses a fencing token so a late worker cannot commit;
    expired lease / late worker);
  * crash right after the publish commit, before the job is finalized: the
    retry must return the prior committed attempt/result and not fail or
    re-publish (retrying the same apply key returns the earlier attempt/result).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.adapters import v2_postgres
from infra.worker import job_worker
from tests.ai8t01.conftest import (
    get_job_status, login, submit_apply_job, submit_claim_and_get_apply,
)
from tests.ai8t02.conftest import (
    artifact_text, build_ready_bundles, build_ready_to_publish_yaml, drain_outbox,
    publish_footprint, reason_of,
)


def _expire_apply_leases(store, tenant, workspace):
    with store._tx() as cur:
        cur.execute("UPDATE v2_outbox SET lease_until = now() - interval '5 seconds' "
                    "WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'",
                    (tenant, workspace))


def test_publish_preserves_other_artifacts_of_the_current_head(setup, monkeypatch, owner):
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    store, tenant = setup["store"], setup["tenant"]
    # A head with TWO artifacts (same shape as a multi-file import), published
    # BEFORE the bundle is approved: F7 binds the approval to the head it was
    # approved on, so the sibling cannot be added after approval.
    sibling_hash = store.stage_blob(b"sibling_setting: 7\n")
    ready = build_ready_bundles(
        setup, client, workspace, monkeypatch, owner,
        siblings={"sibling-t02.yaml": "sibling_setting: 7\n"})[0]
    target = ready["target_artifact_id"]

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready["approval_job_id"],
        expected_approval_digest=ready["digest"])
    assert body["execution_status"] == "COMPLETED", body
    new_head = store.head(tenant, workspace)
    assert str(ready["proposed"]) in artifact_text(store, tenant, workspace, target)
    sibling = store.read_artifact_blob(
        tenant, workspace, new_head.source_revision_id, "sibling-t02.yaml")
    assert sibling["blob_hash"] == sibling_hash, (
        "apply published a single-artifact manifest and dropped the other "
        "artifacts of the current head")


def test_late_worker_that_lost_its_lease_cannot_publish(setup, monkeypatch, owner):
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    store, tenant = setup["store"], setup["tenant"]
    drain_outbox(store, tenant, workspace)
    resp = submit_apply_job(client, workspace, approval_job_id=ready["approval_job_id"],
                            expected_approval_digest=ready["digest"])
    assert resp.status_code == 200, resp.text
    head_before = store.head(tenant, workspace)
    footprint_before = publish_footprint(store, tenant, workspace)

    original = v2_postgres.PostgresV2Store.publish_source
    stolen = []

    def _lose_lease_then_publish(self, *args, **kwargs):
        if kwargs.get("operation") == "apply.publish" and not stolen:
            _expire_apply_leases(store, tenant, workspace)
            stolen.append(store.claim_outbox(tenant, workspace, "t02-new-owner",
                                             lease_seconds=60))
        return original(self, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", _lose_lease_then_publish)
        job_worker.claim_and_admit_one(store, tenant, workspace, "t02-late-worker",
                                       lease_seconds=30)

    assert stolen and stolen[0] is not None, "lease takeover did not happen"
    assert store.head(tenant, workspace).version == head_before.version, (
        "a worker whose lease/fencing token was superseded committed a publish")
    assert publish_footprint(store, tenant, workspace) == footprint_before


class _SimulatedProcessDeath(BaseException):
    pass


def test_crash_after_publish_commit_retry_returns_prior_attempt(setup, monkeypatch, owner):
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    store, tenant = setup["store"], setup["tenant"]
    drain_outbox(store, tenant, workspace)
    job_id = f"job-apply-{uuid4().hex}"
    resp = submit_apply_job(client, workspace, approval_job_id=ready["approval_job_id"],
                            job_id=job_id, expected_approval_digest=ready["digest"])
    assert resp.status_code == 200, resp.text
    head_before = store.head(tenant, workspace)

    original = v2_postgres.PostgresV2Store.publish_source

    def _commit_then_die(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        raise _SimulatedProcessDeath()

    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", _commit_then_die)
        with pytest.raises(_SimulatedProcessDeath):
            job_worker.claim_and_admit_one(store, tenant, workspace, "t02-dying-worker")

    head_committed = store.head(tenant, workspace)
    assert head_committed.version == head_before.version + 1, "commit did not happen"
    footprint_committed = publish_footprint(store, tenant, workspace)

    _expire_apply_leases(store, tenant, workspace)
    job_worker.claim_and_admit_one(store, tenant, workspace, "t02-recovery-worker")

    body = get_job_status(client, workspace, job_id)
    assert store.head(tenant, workspace) == head_committed, "retry re-published"
    assert publish_footprint(store, tenant, workspace) == footprint_committed
    assert body["execution_status"] == "COMPLETED", (
        "source is published but the retried apply did not return the prior "
        f"committed attempt/result: {body!r}")
    assert body["execution_result"]["published_revision_id"] == head_committed.source_revision_id
