"""Revoke-before-publish.

The permanent interface is
`infra.adapters.v2_postgres.PostgresV2Store.recheck_and_publish_job(
tenant_id, workspace_id, job_id, required_permission)`, which recheck the
recorded approver's CURRENT authority (account liveness + membership +
permission) and performs the publish write in one atomic transaction. This
file now imports and exercises that real interface directly, the same way
`tests/security/test_ai2_t02_atomicity.py` does.

Scenarios covered (the approver's permission is rechecked before publish; a
job service identity does not replace the human decider): revoking the
approver's permission after approval but before publish must block the
publish (job stays APPROVED, never silently PUBLISHED); a job-service
identity must never substitute for the human decider whose authority is
rechecked; a still-valid approver's publish must still succeed (negative
control).

"""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import PostgresV2Store, StoreConflict
from infra.worker.job_worker import claim_and_admit_one

from tests.ai2t03.conftest import publish_pinned_head


def _run_job_to_completion(store, tenant, workspace, job_id, *, max_claims=10):
    """Drive `job_id` through the REAL worker claim path until its
    execution_status reaches COMPLETED (production now REQUIRES a completed
    execution result before publish -- recheck_and_publish_job raises
    StoreConflict on any non-COMPLETED execution_status). Other queued jobs
    in the same workspace outbox may be claimed first; keep claiming until
    this job_id's own record is COMPLETED or the claim well runs dry."""
    for _ in range(max_claims):
        record = store.job_record(tenant, workspace, job_id)
        if record["execution_status"] == "COMPLETED":
            return
        claim = claim_and_admit_one(store, tenant, workspace, "worker-" + uuid4().hex)
        if claim is None:
            break
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED", (
        f"test setup failed to drive job {job_id} to COMPLETED execution "
        f"(stuck at execution_status={record['execution_status']!r}); the real "
        f"production publish path requires this before publish is even attempted"
    )


class MemoryBlobs:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put_immutable(self, digest, content):
        self.data.setdefault(digest, content)

    def read(self, digest):
        return self.data[digest]


def _identities_for(dsn: str) -> PostgresIdentityStore:
    identities = PostgresIdentityStore(dsn)
    identities.migrate()
    return identities


def _approved_change_job(store, identities, tenant, project, workspace, *,
                         requester: str, approver: str) -> str:
    """Provision a requester/approver pair and drive one job to APPROVED,
    at the store level, exactly matching the real /v2/jobs/*/approve path's
    end state (PostgresV2Store.set_job_requester + approve_job)."""
    identities.create_account(requester, f"cred-{requester}-{uuid4().hex}")
    identities.create_account(approver, f"cred-{approver}-{uuid4().hex}")
    identities.grant_membership(tenant, project, workspace, requester, ("requester",))
    identities.grant_membership(tenant, project, workspace, approver, ("change_approver",))

    head = store.head(tenant, workspace)
    job_id = "job-" + uuid4().hex
    store.create_job(tenant, workspace, job_id, "change", {},
                     expected_version=head.version, idempotency_key=f"create-{job_id}")
    store.set_job_requester(tenant, workspace, job_id, requester)
    store.approve_job(tenant, workspace, job_id, approver, ("change_approver",))
    return job_id


def test_revoke_after_approval_before_publish_blocks_the_publish(scoped_store):
    """The core scenario: an approver
    approves a change while a job is in flight; before the worker actually
    calls `recheck_and_publish_job`, the approver's membership is revoked.
    The publish must be blocked -- not merely the *next* approval request,
    but this *already-approved, not-yet-published* action -- and the job
    must remain durably APPROVED, not silently PUBLISHED.
    """
    store, tenant, workspace, dsn = scoped_store
    publish_pinned_head(store, tenant, workspace)
    project = "p1"
    identities = _identities_for(dsn)

    requester, approver = "requester-" + uuid4().hex, "approver-" + uuid4().hex
    job_id = _approved_change_job(store, identities, tenant, project, workspace,
                                  requester=requester, approver=approver)

    _run_job_to_completion(store, tenant, workspace, job_id)
    identities.revoke_membership(tenant, project, workspace, approver)

    with pytest.raises(StoreConflict, match="revoked|permission"):
        store.recheck_and_publish_job(tenant, workspace, job_id, "change.approve")

    status = store.job_status(tenant, workspace, job_id)
    assert status["status"] == "APPROVED", "job must not have been silently PUBLISHED"


def test_job_service_identity_cannot_stand_in_for_the_revoked_decider(scoped_store):
    """"Job service identity không thay người quyết định": the worker
    process that calls `recheck_and_publish_job` has its own service
    identity/DB connection, but the recheck is always evaluated against the
    job's RECORDED human approver (`v2_jobs.approved_by`), never against the
    calling worker's own identity -- there is no parameter through which a
    worker could substitute itself as the decider, and this test proves a
    revoked approver still blocks publish even though the worker itself
    (calling this method) was never granted or revoked anything.
    """
    store, tenant, workspace, dsn = scoped_store
    publish_pinned_head(store, tenant, workspace)
    project = "p1"
    identities = _identities_for(dsn)

    requester, approver = "requester-" + uuid4().hex, "approver-" + uuid4().hex
    job_id = _approved_change_job(store, identities, tenant, project, workspace,
                                  requester=requester, approver=approver)

    _run_job_to_completion(store, tenant, workspace, job_id)
    identities.revoke_membership(tenant, project, workspace, approver)

    # A second, independent store instance stands in for "a different worker
    # process" (its own connection, no shared in-memory state) -- it still
    # cannot publish, because the recheck is keyed off `approved_by`, not off
    # anything this instance/process itself is or claims to be.
    worker_store = PostgresV2Store(dsn, MemoryBlobs())
    with pytest.raises(StoreConflict, match="revoked|permission"):
        worker_store.recheck_and_publish_job(tenant, workspace, job_id, "change.approve")

    status = store.job_status(tenant, workspace, job_id)
    assert status["status"] == "APPROVED"


def test_recheck_passes_for_a_still_valid_approver(scoped_store):
    """Negative control: an approver whose membership was never revoked
    must still be allowed to publish. Without this, the two tests above
    could pass trivially by refusing everything.
    """
    store, tenant, workspace, dsn = scoped_store
    publish_pinned_head(store, tenant, workspace)
    project = "p1"
    identities = _identities_for(dsn)

    requester, approver = "requester-" + uuid4().hex, "approver-" + uuid4().hex
    job_id = _approved_change_job(store, identities, tenant, project, workspace,
                                  requester=requester, approver=approver)

    _run_job_to_completion(store, tenant, workspace, job_id)
    store.recheck_and_publish_job(tenant, workspace, job_id, "change.approve")  # must not raise
    status = store.job_status(tenant, workspace, job_id)
    assert status["status"] == "PUBLISHED"
