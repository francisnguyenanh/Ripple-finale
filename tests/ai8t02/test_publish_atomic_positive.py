"""Atomic publish, positive half: a valid, decided, staged apply publishes
atomically through the ONE existing apply gate (route -> claim -> executor ->
`publish_source`) -- head advances, new bytes are the CURRENT artifact, and
revision / ledger / audit / outbox rows all land together.
"""

from __future__ import annotations

from tests.ai8t01.conftest import login, submit_claim_and_get_apply
from tests.ai8t02.conftest import (
    artifact_text, build_ready_to_publish_yaml, publish_footprint,
)


def test_u1_successful_apply_publishes_head_atomically(setup, monkeypatch, owner):
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])

    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    store, tenant = setup["store"], setup["tenant"]
    head_before = store.head(tenant, workspace)
    footprint_before = publish_footprint(store, tenant, workspace)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready["approval_job_id"],
        expected_approval_digest=ready["digest"])
    assert body["execution_status"] == "COMPLETED", body

    head_after = store.head(tenant, workspace)
    assert head_after.version == head_before.version + 1
    assert head_after.source_revision_id != head_before.source_revision_id
    text = artifact_text(store, tenant, workspace, ready["target_artifact_id"])
    assert str(ready["proposed"]) in text and str(ready["expected_current"]) not in text

    # The published blob is exactly the staged blob the executor reported.
    staged_hash = body["execution_result"]["staged_manifest"][ready["target_artifact_id"]]
    assert store._verified_blob(staged_hash) == text.encode("utf-8")
    assert body["execution_result"]["published_revision_id"] == head_after.source_revision_id

    # One transaction => every publish-side row appeared exactly once.
    footprint_after = publish_footprint(store, tenant, workspace)
    for key in ("revisions", "apply_publish_keys", "audit", "outbox"):
        assert footprint_after[key] == footprint_before[key] + 1, (key, footprint_before,
                                                                  footprint_after)


def test_u1_no_op_apply_does_not_publish_but_real_apply_does(setup, monkeypatch, owner):
    """W1: a true no-op bundle is BLOCKED (its acceptance check is UNAVAILABLE) so it can no longer
    be approved or applied; head and footprint stay untouched. Non-vacuous: the same footprint probe,
    in the same test, detects the publish made by a real (non-no-op) apply on a second bundle."""
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])

    # F7: an approval is bound to the head it was approved on, so any publish
    # between approve and apply stales it. Build the no-op bundle, apply it
    # (publishes nothing, so the head stays put), and only THEN build the
    # control bundle (its base publish is part of ITS own approval snapshot).
    target = "noop-control-t02.yaml"
    noop = build_ready_to_publish_yaml(
        setup, client, workspace, monkeypatch, owner, target_artifact_id=target,
        expected_current=14, proposed=14, record_approval=False)
    store, tenant = setup["store"], setup["tenant"]

    head_before = store.head(tenant, workspace)
    footprint_before = publish_footprint(store, tenant, workspace)
    from tests.ai8t02.conftest import assert_noop_bundle_is_unapprovable_and_unapplyable
    body = assert_noop_bundle_is_unapprovable_and_unapplyable(setup, client, workspace, noop)
    assert (body.get("execution_result") or {}).get("published") is not True
    from tests.ai8t02.conftest import head_core
    assert head_core(store.head(tenant, workspace)) == head_core(head_before)
    assert publish_footprint(store, tenant, workspace) == footprint_before
    assert str(noop["expected_current"]) in artifact_text(
        store, tenant, workspace, noop["target_artifact_id"])

    # Control: the identical probe DOES move for a real apply (fresh bundle,
    # approved on the current head).
    real = build_ready_to_publish_yaml(
        setup, client, workspace, monkeypatch, owner, target_artifact_id=target,
        expected_current=14, proposed=30)
    head_before = store.head(tenant, workspace)
    footprint_before = publish_footprint(store, tenant, workspace)
    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=real["approval_job_id"],
        expected_approval_digest=real["digest"], worker_id="t02-control-worker")
    assert body["execution_status"] == "COMPLETED", body
    assert body["execution_result"]["published"] is True
    assert store.head(tenant, workspace).version == head_before.version + 1
    assert publish_footprint(store, tenant, workspace) != footprint_before
