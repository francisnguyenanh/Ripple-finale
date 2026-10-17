"""Atomic publish, negative half: a crash/failure between blob upload and SQL
commit must leave head/metadata untouched -- no partial publish.

Each test is a red -> restore -> green failure-injection triple: (red) the
injected fault makes the real apply job FAILED and the durable SQL state is
byte-for-byte the pre-apply state; (restore) the fault is removed; (green)
a fresh apply job over the same approval then publishes normally, proving the
fault -- not a leftover partial write -- was the only blocker.

Verification reads the DB (`publish_footprint`, `store.head`,
`read_artifact_blob`), never the job row alone.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.adapters import v2_postgres
from tests.ai8t01.conftest import login, submit_claim_and_get_apply
from tests.ai8t02.conftest import (
    artifact_text, build_ready_to_publish_yaml, publish_footprint, reason_of,
)


def _assert_untouched(store, tenant, workspace, head_before, footprint_before, ready):
    head_after = store.head(tenant, workspace)
    assert head_after.version == head_before.version, "head advanced (partial publish)"
    assert head_after.source_revision_id == head_before.source_revision_id
    assert publish_footprint(store, tenant, workspace) == footprint_before, (
        "publish-side rows (revision / apply.publish idempotency / audit / outbox) "
        "changed although the publish transaction failed")
    text = artifact_text(store, tenant, workspace, ready["target_artifact_id"])
    assert str(ready["expected_current"]) in text and str(ready["proposed"]) not in text, (
        "the staged (orphan) blob became visible as current bytes")


def _green_after_restore(setup, client, workspace, ready, store, tenant, head_before):
    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready["approval_job_id"],
        expected_approval_digest=ready["digest"], worker_id="t02-restore-worker")
    assert body["execution_status"] == "COMPLETED", body
    assert store.head(tenant, workspace).version == head_before.version + 1
    assert str(ready["proposed"]) in artifact_text(
        store, tenant, workspace, ready["target_artifact_id"])


def test_u1_publish_source_failure_after_staging_leaves_head_and_metadata_unchanged(
        setup, monkeypatch, owner):
    """"Blob staged, then the SQL publish transaction itself fails": force the
    store's atomic publish primitive to raise AFTER the blob was staged."""
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    store, tenant = setup["store"], setup["tenant"]
    head_before = store.head(tenant, workspace)
    footprint_before = publish_footprint(store, tenant, workspace)

    original = v2_postgres.PostgresV2Store.publish_source

    def _boom(self, *args, **kwargs):
        raise RuntimeError("simulated crash: SQL commit never happens")

    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", _boom)
        body = submit_claim_and_get_apply(
            setup, client, workspace, approval_job_id=ready["approval_job_id"],
            expected_approval_digest=ready["digest"])

    assert body["execution_status"] == "FAILED", body
    assert "publish_transaction_failed" in reason_of(body), body
    _assert_untouched(store, tenant, workspace, head_before, footprint_before, ready)

    assert v2_postgres.PostgresV2Store.publish_source is original  # restored
    _green_after_restore(setup, client, workspace, ready, store, tenant, head_before)


def test_u1_audit_and_outbox_write_failure_rolls_back_whole_metadata(setup, monkeypatch, owner):
    """A broken audit/outbox write INSIDE the apply-publish transaction must
    roll back the manifest rows and the head advance as well. The injection is
    scoped to `operation == "apply.publish"` so antecedent job creation /
    decision writes (which also use `_record_commit`) are unaffected. The
    fault fires AFTER the revision rows and the head UPDATE were already
    executed in that transaction (`_record_commit` is the last write), so
    the rollback assertions are non-trivial."""
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    store, tenant = setup["store"], setup["tenant"]
    head_before = store.head(tenant, workspace)
    footprint_before = publish_footprint(store, tenant, workspace)

    original = v2_postgres.PostgresV2Store._record_commit
    fired = []

    def _scoped_boom(self, cur, tenant_id, workspace_id, operation, *args, **kwargs):
        if operation == "apply.publish":
            fired.append(operation)
            raise RuntimeError("simulated crash: audit/outbox write fails")
        return original(self, cur, tenant_id, workspace_id, operation, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "_record_commit", _scoped_boom)
        body = submit_claim_and_get_apply(
            setup, client, workspace, approval_job_id=ready["approval_job_id"],
            expected_approval_digest=ready["digest"])

    assert fired == ["apply.publish"], "injection did not fire inside the apply-publish tx"
    assert body["execution_status"] == "FAILED", body
    assert "publish_transaction_failed" in reason_of(body), body
    _assert_untouched(store, tenant, workspace, head_before, footprint_before, ready)

    assert v2_postgres.PostgresV2Store._record_commit is original  # restored
    _green_after_restore(setup, client, workspace, ready, store, tenant, head_before)
    after = publish_footprint(store, tenant, workspace)
    assert after["audit"] == footprint_before["audit"] + 1
    assert after["outbox"] == footprint_before["outbox"] + 1
    assert after["apply_publish_keys"] == 1


@pytest.mark.parametrize("head_check_on", [True, False],
                         ids=["locked-head-check", "store-CAS-only"])
def test_u1_stale_head_cas_rejection_leaves_no_partial_state(
        setup, monkeypatch, owner, head_check_on):
    """Interleave: another publish (target re-published UNCHANGED, so the
    typed-edit replay cannot catch it) lands between the executor's fresh head
    read and the publish transaction. Two layers must each leave no partial
    state and keep the interloper's publish intact:
      * F7 on (`locked-head-check`): the stale-tuple check on the row-LOCKED
        head, which runs before the CAS, refuses -> FAILED `approval_stale_source`;
      * `head_check` neutralised in-test (`store-CAS-only`): the store CAS
        (`expected_version`) is the remaining guard -> `StaleVersion` -> FAILED
        `publish_stale_head`. (Keeps the store-level CAS/atomicity proof that
        F7's earlier check would otherwise shadow.)"""
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    store, tenant = setup["store"], setup["tenant"]

    original = v2_postgres.PostgresV2Store.publish_source
    interloper_result = []

    def _interleave(self, tenant_id, workspace_id, revision_id, manifest, **kwargs):
        if kwargs.get("operation") == "apply.publish" and not interloper_result:
            # Re-publish the target UNCHANGED: head advances, bytes do not, so
            # only the store CAS (not the typed-edit replay) can catch it.
            same = self.read_artifact_blob(
                tenant_id, workspace_id, self.head(tenant_id, workspace_id).source_revision_id,
                ready["target_artifact_id"])["blob_hash"]
            interloper_result.append(original(
                self, tenant_id, workspace_id, f"rev-interloper-{uuid4().hex}",
                {ready["target_artifact_id"]: same},
                expected_version=self.head(tenant_id, workspace_id).version,
                operation="interloper.publish", idempotency_key=uuid4().hex))
        if not head_check_on:
            kwargs = {k: v for k, v in kwargs.items() if k != "head_check"}
        return original(self, tenant_id, workspace_id, revision_id, manifest, **kwargs)

    head_before = store.head(tenant, workspace)
    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", _interleave)
        body = submit_claim_and_get_apply(
            setup, client, workspace, approval_job_id=ready["approval_job_id"],
            expected_approval_digest=ready["digest"])

    assert interloper_result, "interloper publish did not run"
    assert body["execution_status"] == "FAILED", body
    expected_reason = "approval_stale_source" if head_check_on else "publish_stale_head"
    assert expected_reason in reason_of(body), body
    head_after = store.head(tenant, workspace)
    assert head_after.version == head_before.version + 1, (
        "exactly the interloper's publish may have advanced head")
    assert head_after.source_revision_id == interloper_result[0].revision_id
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_idempotency WHERE tenant_id=%s AND "
                    "workspace_id=%s AND operation='apply.publish'", (tenant, workspace))
        assert cur.fetchone()[0] == 0, "stale apply left an apply.publish ledger row"
        cur.execute("SELECT count(*) FROM v2_source_revisions WHERE tenant_id=%s AND "
                    "workspace_id=%s AND revision_id LIKE 'rev-apply-%%'",
                    (tenant, workspace))
        assert cur.fetchone()[0] == 0, "stale apply left a revision row"
