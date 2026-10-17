"""Fixtures for the durable-job tests.

Reused infrastructure:
`infra/adapters/v2_postgres.py::PostgresV2Store` already implements
`create_job`, `job_status`, `claim_outbox`, `heartbeat_outbox`, `ack_outbox`
with real fencing tokens and lease timestamps. The durability/
restart/retry/fencing/dedup tests exercise that real store directly (same
pattern as `core/tests/test_ai1_t03_storage.py`), because that is the
production path the worker sits on top of.

Quota (budget) tests target a worker-level admission hook on top of
`infra/worker/budget_config.py`: `max_provider_calls_per_job` must be an
independent critical key, `>= actual call count`, once one job can make
more than one `complete_structured` call.

Revoke-before-publish tests target the permission recheck hook: the approver's
permission is rechecked before publish.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest


AI2_T03_TEST_DSN = os.environ.get("AI2_T03_TEST_DSN") or os.environ.get("AI1_T03_TEST_DSN")

# NOTE: a `pytestmark` assigned in conftest.py does NOT skip tests collected
# from sibling test modules (only tests defined in this file itself, of
# which there are none) -- pytest does not propagate it that way. The real
# skip is applied at the `scoped_store` fixture below, which every DB-backed
# test in this package depends on, matching this project's existing
# convention in core/tests/test_ai1_t03_storage.py (its own per-file
# pytestmark, applied because ITS tests live in that same file).


class MemoryBlobs:
    """Same minimal fake used by core/tests/test_ai1_t03_storage.py."""

    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put_immutable(self, digest: str, content: bytes) -> None:
        self.data.setdefault(digest, content)

    def read(self, digest: str) -> bytes:
        return self.data[digest]


def _make_store():
    from infra.adapters.v2_postgres import PostgresV2Store

    blobs = MemoryBlobs()
    repository = PostgresV2Store(AI2_T03_TEST_DSN, blobs)
    repository.migrate()
    return repository, blobs


@pytest.fixture
def scoped_store():
    """A fresh (tenant, workspace) pair against a real store, plus the DSN.

    Returns (store, tenant, workspace, dsn) so a test can open a *second*,
    independent store instance against the same DSN to simulate a worker
    restart (a new process re-connecting), per the task card's "kill worker/
    restart" acceptance criterion.
    """
    if not AI2_T03_TEST_DSN:
        pytest.skip(
            "AI2-T03 durable-job tests need a disposable PostgreSQL database "
            "(AI2_T03_TEST_DSN, falling back to AI1_T03_TEST_DSN)"
        )
    store, _blobs = _make_store()
    tenant = "t-ai2t03-" + uuid4().hex
    workspace = "w-ai2t03-" + uuid4().hex
    store.create_workspace(tenant, "p1", workspace)
    return store, tenant, workspace, AI2_T03_TEST_DSN


def reopen_store(dsn: str):
    """Simulate a worker restart: a brand-new store object, same DSN."""
    from infra.adapters.v2_postgres import PostgresV2Store

    blobs = MemoryBlobs()
    store = PostgresV2Store(dsn, blobs)
    store.migrate()
    return store


def publish_pinned_head(store, tenant, workspace, *, key="src-1", revision="s1"):
    """Publish a minimal source revision so create_job has a head to pin."""
    digest = store.stage_blob(b"ai2t03-fixture-content")
    return store.publish_source(
        tenant, workspace, revision, {"artifact-a": digest},
        expected_version=0, operation="source.import.commit",
        idempotency_key=key,
    )
