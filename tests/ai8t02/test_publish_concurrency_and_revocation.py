"""Concurrency and revocation: exactly one of two competing applies wins and the loser leaves
no partial state; a revoked approver never publishes; a revoke racing the
publish transaction is serialized with it.
"""

from __future__ import annotations

import threading
import time

import pytest

from infra.adapters import v2_postgres
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.worker import job_worker
from tests.ai4t01.conftest import DSN
from tests.ai8t01.conftest import (
    get_job_status, login, submit_apply_job, submit_claim_and_get_apply,
)
from tests.ai8t02.conftest import (
    artifact_text, build_ready_bundles, build_ready_to_publish_yaml, drain_outbox,
    publish_footprint, reason_of, revoke_approver,
)


def test_u2_two_applies_over_same_target_only_one_wins_loser_leaves_no_state(
        setup, monkeypatch, owner):
    """Two decided bundles over the SAME artifact, both built against the
    same original content. A publishes; B (applied afterwards) must fail
    closed, and the DB must show exactly one apply publish."""
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    target = "shared-config-t02.yaml"
    store, tenant = setup["store"], setup["tenant"]
    # F7: both approvals must pin the SAME head, so the base is published once
    # and both bundles are decided over it (building B after A would stale A).
    ready_a, ready_b = build_ready_bundles(
        setup, client, workspace, monkeypatch, owner, target_artifact_id=target,
        expected_current=14, proposed_values=(30, 99))
    # The bundles' own publishes left SOURCE_PUBLISHED rows.
    drain_outbox(store, tenant, workspace)
    baseline = publish_footprint(store, tenant, workspace)

    body_a = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready_a["approval_job_id"],
        expected_approval_digest=ready_a["digest"])
    assert body_a["execution_status"] == "COMPLETED", body_a
    # A's publish enqueued SOURCE_PUBLISHED; drain it so B's JOB_QUEUED is next.
    drain_outbox(store, tenant, workspace)
    head_a = store.head(tenant, workspace)
    after_a = publish_footprint(store, tenant, workspace)
    assert after_a["apply_publish_keys"] == baseline["apply_publish_keys"] + 1

    body_b = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready_b["approval_job_id"],
        expected_approval_digest=ready_b["digest"], worker_id="t02-loser-worker")
    assert body_b["execution_status"] == "FAILED", body_b
    # A's publish moved the head B's approval was bound to: F7 stale tuple.
    assert "approval_stale_source" in reason_of(body_b), body_b

    # Loser left nothing: head, ledger, audit, outbox all as after A.
    assert store.head(tenant, workspace) == head_a
    assert publish_footprint(store, tenant, workspace) == after_a
    text = artifact_text(store, tenant, workspace, target)
    assert "30" in text and "99" not in text


@pytest.mark.parametrize("head_check_on", [True, False],
                         ids=["locked-head-check", "store-CAS-only"])
def test_u2_truly_concurrent_applies_cas_exactly_one_wins(
        setup, monkeypatch, owner, head_check_on):
    """Two workers in two threads reach `publish_source` holding the SAME
    snapshot and `expected_version` (a barrier inside the call forces the
    overlap). Exactly one commits; the other fails with zero partial state.
    F7 layering: with the in-tx `head_check` on, the loser is refused by the
    stale-tuple check on the row-locked head (`approval_stale_source`, which
    runs before the CAS); with `head_check` neutralised in-test the store CAS
    is the guard that must still hold exactly-one-winner (`publish_stale_head`)."""
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    target = "race-config-t02.yaml"
    ready_a, ready_b = build_ready_bundles(
        setup, client, workspace, monkeypatch, owner, target_artifact_id=target,
        expected_current=14, proposed_values=(30, 99))
    store, tenant = setup["store"], setup["tenant"]
    drain_outbox(store, tenant, workspace)

    job_ids = {}
    for name, ready in (("a", ready_a), ("b", ready_b)):
        resp = submit_apply_job(client, workspace, approval_job_id=ready["approval_job_id"],
                                expected_approval_digest=ready["digest"])
        assert resp.status_code == 200, resp.text
        job_ids[name] = resp.json().get("job_id") or resp.json().get("id")
    head_before = store.head(tenant, workspace)
    baseline = publish_footprint(store, tenant, workspace)

    original = v2_postgres.PostgresV2Store.publish_source
    barrier = threading.Barrier(2, timeout=30)

    def _overlap(self, *args, **kwargs):
        if kwargs.get("operation") == "apply.publish":
            barrier.wait()
            if not head_check_on:
                kwargs = {k: v for k, v in kwargs.items() if k != "head_check"}
        return original(self, *args, **kwargs)

    errors = []

    def _worker(name):
        try:
            job_worker.claim_and_admit_one(store, tenant, workspace, f"t02-race-{name}")
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", _overlap)
        threads = [threading.Thread(target=_worker, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    assert not errors, errors

    statuses = {n: get_job_status(client, workspace, j) for n, j in job_ids.items()}
    outcomes = sorted(s["execution_status"] for s in statuses.values())
    assert outcomes == ["COMPLETED", "FAILED"], statuses
    loser = next(s for s in statuses.values() if s["execution_status"] == "FAILED")
    expected_reason = "approval_stale_source" if head_check_on else "publish_stale_head"
    assert expected_reason in reason_of(loser), loser

    assert store.head(tenant, workspace).version == head_before.version + 1
    after = publish_footprint(store, tenant, workspace)
    for key in ("revisions", "apply_publish_keys", "audit", "outbox"):
        assert after[key] == baseline[key] + 1, (key, baseline, after)
    winner = next(n for n, s in statuses.items() if s["execution_status"] == "COMPLETED")
    winning_value = str({"a": 30, "b": 99}[winner])
    losing_value = str({"a": 99, "b": 30}[winner])
    text = artifact_text(store, tenant, workspace, target)
    assert winning_value in text and losing_value not in text, "hybrid/overwritten content"


def test_u2_revoked_approver_decision_never_publishes(setup, monkeypatch, owner):
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    store, tenant = setup["store"], setup["tenant"]
    head_before = store.head(tenant, workspace)
    footprint_before = publish_footprint(store, tenant, workspace)

    revoke_approver(setup, workspace, ready["approver"])

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready["approval_job_id"],
        expected_approval_digest=ready["digest"])
    assert body["execution_status"] == "FAILED", body
    assert "approver_authority_revoked" in reason_of(body), body
    head_after = store.head(tenant, workspace)  # revoke legitimately bumps policy_version
    assert (head_after.version, head_after.source_revision_id) == (
        head_before.version, head_before.source_revision_id)
    assert publish_footprint(store, tenant, workspace) == footprint_before
    assert str(ready["proposed"]) not in artifact_text(
        store, tenant, workspace, ready["target_artifact_id"])


def test_u2_revoke_racing_the_publish_transaction_is_serialized(setup, monkeypatch, owner):
    """"Revoke between authority check and commit": the revoke is started
    from INSIDE the publish transaction, after the authority check passed
    (at the apply-publish `_record_commit`). Because the check holds
    `FOR UPDATE` on the membership row, the revoke must BLOCK until the
    publish commits: the publish succeeds, and the revoke only completes
    afterwards (so a later apply by the same approver's decision would be
    refused, but this one was already authorized under the lock)."""
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    store, tenant = setup["store"], setup["tenant"]
    head_before = store.head(tenant, workspace)

    original = v2_postgres.PostgresV2Store._record_commit
    state = {"revoke_done_inside_tx": None}
    done = threading.Event()

    def _revoke():
        identities = PostgresIdentityStore(DSN)
        identities.revoke_membership(setup["tenant"], setup["project"], workspace,
                                     ready["approver"]["account"])
        done.set()

    thread = threading.Thread(target=_revoke)

    def _with_concurrent_revoke(self, cur, tenant_id, workspace_id, operation, *a, **kw):
        if operation == "apply.publish":
            thread.start()
            time.sleep(2.0)  # give the revoke ample time to (wrongly) finish
            state["revoke_done_inside_tx"] = done.is_set()
        return original(self, cur, tenant_id, workspace_id, operation, *a, **kw)

    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "_record_commit", _with_concurrent_revoke)
        body = submit_claim_and_get_apply(
            setup, client, workspace, approval_job_id=ready["approval_job_id"],
            expected_approval_digest=ready["digest"])
    thread.join(30)

    assert state["revoke_done_inside_tx"] is False, (
        "revoke completed while the publish transaction held its authority "
        "locks -- revocation is not serialized with publication")
    assert body["execution_status"] == "COMPLETED", body
    assert store.head(tenant, workspace).version == head_before.version + 1
    assert done.is_set(), "revoke never completed after the publish committed"
