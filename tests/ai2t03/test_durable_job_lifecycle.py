"""Durable job persistence, restart/retry, fencing, dedup.

This file exercises the v2 store
(`infra/adapters/v2_postgres.py::PostgresV2Store`) directly, because durable
job persistence, restart/retry semantics, fencing and duplicate-outbox
handling are properties of that store that the worker relies on.

Scenarios covered: "kill worker/restart", "duplicate outbox", "lease
timeout", "two workers on the same job; only one publish".
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from tests.ai2t03.conftest import publish_pinned_head, reopen_store


# ---------------------------------------------------------------------------
# Durable job persistence + restart
# ---------------------------------------------------------------------------

def test_job_survives_worker_restart_same_dsn(scoped_store):
    """A job created before a simulated restart is still readable after it.

    "Restart" = a brand-new PostgresV2Store instance against the same DSN,
    the same shape of independence a real worker process restart would have
    (no shared in-memory state, only the database).
    """
    store, tenant, workspace, dsn = scoped_store
    head = publish_pinned_head(store, tenant, workspace)
    job_id = "job-" + uuid4().hex
    store.create_job(
        tenant, workspace, job_id, "analysis", {"note": "before restart"},
        expected_version=head.version, idempotency_key="create-" + job_id,
    )

    restarted = reopen_store(dsn)
    status = restarted.job_status(tenant, workspace, job_id)
    assert status["status"] == "QUEUED"
    assert status["payload"] == {"note": "before restart"}
    # The snapshot/KM/decision epoch pinned at submit must also survive.
    assert status["pinned_source_revision_id"] == "s1"


def test_job_create_is_idempotent_across_a_simulated_retry(scoped_store):
    """Resubmitting with the same idempotency key after a "client retry"
    (e.g. an API caller that timed out and resent the request) must not
    create a second job or move the workspace version twice.
    """
    store, tenant, workspace, dsn = scoped_store
    head = publish_pinned_head(store, tenant, workspace)
    job_id = "job-" + uuid4().hex
    key = "create-" + job_id

    first = store.create_job(
        tenant, workspace, job_id, "analysis", {"n": 1},
        expected_version=head.version, idempotency_key=key,
    )
    restarted = reopen_store(dsn)
    second = restarted.create_job(
        tenant, workspace, job_id, "analysis", {"n": 1},
        expected_version=head.version, idempotency_key=key,
    )
    assert second.replayed and second.version == first.version


# ---------------------------------------------------------------------------
# Lease timeout + retry
# ---------------------------------------------------------------------------

def test_lease_timeout_makes_the_outbox_row_reclaimable(scoped_store):
    """A worker that dies mid-lease (no heartbeat, no ack) must not block
    the row forever: once the lease expires, another worker can claim it
    with a fresh, higher fencing token.
    """
    store, tenant, workspace, dsn = scoped_store
    head = publish_pinned_head(store, tenant, workspace)
    assert store.pending_outbox(tenant, workspace) == 1

    first_claim = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=1)
    assert first_claim is not None
    stale_token = first_claim.fencing_token

    time.sleep(1.2)  # let the 1-second lease expire; the dead worker never heartbeats

    second_claim = store.claim_outbox(tenant, workspace, "worker-b", lease_seconds=30)
    assert second_claim is not None
    assert second_claim.id == first_claim.id
    assert second_claim.fencing_token > stale_token


def test_retry_is_bounded_exhausted_row_stops_being_claimable(scoped_store):
    """`claim_outbox` refuses rows with attempts >= 5 (bounded provider
    retries). Repeatedly letting the lease expire without ever acking must
    eventually stop producing a claim, not retry forever.
    """
    store, tenant, workspace, dsn = scoped_store
    publish_pinned_head(store, tenant, workspace)

    claims = 0
    for _ in range(8):
        claim = store.claim_outbox(tenant, workspace, "worker-" + uuid4().hex, lease_seconds=1)
        if claim is None:
            break
        claims += 1
        time.sleep(1.2)

    assert claims == 5, "attempts < 5 boundary must cap the number of claims"
    assert store.claim_outbox(tenant, workspace, "worker-final", lease_seconds=30) is None


# ---------------------------------------------------------------------------
# Fencing tokens: stale workers must not be able to act
# ---------------------------------------------------------------------------

def test_fencing_token_blocks_a_zombie_worker_heartbeat(scoped_store):
    """Worker A claims, its lease then expires (it stalled / GC pause /
    network partition), worker B claims the same row with a new token.
    Worker A, unaware it is a zombie, tries to heartbeat with its stale
    token -- this must be rejected (LeaseLost), never silently accepted.
    """
    from infra.adapters.v2_postgres import LeaseLost

    store, tenant, workspace, dsn = scoped_store
    publish_pinned_head(store, tenant, workspace)

    claim_a = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=1)
    time.sleep(1.2)
    claim_b = store.claim_outbox(tenant, workspace, "worker-b", lease_seconds=30)
    assert claim_b is not None and claim_b.fencing_token > claim_a.fencing_token

    with pytest.raises(LeaseLost):
        store.heartbeat_outbox(tenant, workspace, claim_a.id, "worker-a", claim_a.fencing_token)


def test_fencing_token_blocks_a_zombie_worker_ack_publish(scoped_store):
    """The load-bearing invariant: a stale worker must not be able to
    publish (ack) after another worker has taken over the row. This is the
    concrete mechanism behind "chỉ một publish" in the task card.
    """
    from infra.adapters.v2_postgres import LeaseLost

    store, tenant, workspace, dsn = scoped_store
    publish_pinned_head(store, tenant, workspace)

    claim_a = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=1)
    time.sleep(1.2)
    claim_b = store.claim_outbox(tenant, workspace, "worker-b", lease_seconds=30)

    with pytest.raises(LeaseLost):
        store.ack_outbox(tenant, workspace, claim_a.id, "worker-a", claim_a.fencing_token)

    # The legitimate current owner can still publish exactly once.
    store.ack_outbox(tenant, workspace, claim_b.id, "worker-b", claim_b.fencing_token)
    with pytest.raises(LeaseLost):
        store.ack_outbox(tenant, workspace, claim_b.id, "worker-b", claim_b.fencing_token)


def test_two_workers_racing_for_the_same_row_only_one_wins(scoped_store):
    """Two workers claim concurrently (simulated by claiming from the same
    live lease window, back to back): only one gets the row per claim call;
    `SKIP LOCKED` plus the PENDING/expired-CLAIMED predicate is exercised
    directly, matching the task card's "two workers cùng job" criterion.
    """
    store, tenant, workspace, dsn = scoped_store
    publish_pinned_head(store, tenant, workspace)

    claim_a = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=30)
    claim_b = store.claim_outbox(tenant, workspace, "worker-b", lease_seconds=30)
    assert claim_a is not None
    assert claim_b is None, "a second worker must not get a still-leased row"


# ---------------------------------------------------------------------------
# Duplicate outbox message handling (idempotency)
# ---------------------------------------------------------------------------

def test_duplicate_ack_delivery_does_not_double_publish(scoped_store):
    """A duplicate delivery of the same ack (e.g. a worker's ack response
    was lost, and it resends the exact same ack request against the same
    fencing token) must not be treated as a second successful publish.
    """
    from infra.adapters.v2_postgres import LeaseLost

    store, tenant, workspace, dsn = scoped_store
    publish_pinned_head(store, tenant, workspace)
    claim = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=30)

    store.ack_outbox(tenant, workspace, claim.id, "worker-a", claim.fencing_token)
    with pytest.raises(LeaseLost):
        store.ack_outbox(tenant, workspace, claim.id, "worker-a", claim.fencing_token)


def test_duplicate_claim_after_ack_finds_nothing_left_to_do(scoped_store):
    """Once a row is DONE, it must never be claimable again -- a duplicate
    "there is pending work" signal (e.g. a duplicated queue notification)
    must resolve to no-op, not a second delivery.
    """
    store, tenant, workspace, dsn = scoped_store
    publish_pinned_head(store, tenant, workspace)
    claim = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=30)
    store.ack_outbox(tenant, workspace, claim.id, "worker-a", claim.fencing_token)

    assert store.claim_outbox(tenant, workspace, "worker-b", lease_seconds=30) is None
