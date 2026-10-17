"""Dependency invalidation / reanalysis fixtures.

The tests in this package exercise:
  - a new "invalidation" job kind (`infra/worker/job_worker.py::BUILTIN_EXECUTORS`,
    `infra/adapters/v2_policy.py::JOB_CREATE_PERMISSION`),
  - a dependency/staleness marker reachable from `knowledge_draft`,
  - the invalidation planner / reanalysis scheduler / causation-chain module.

Reuses (not reinvented):
  - `tests.ai4t01.conftest.setup` — real Postgres, real `infra.judge.app`,
    real "requester" account/session, `drain_outbox`, `publish_with_chunk`.
  - `tests.ai4t02.conftest.seed_extraction_candidates` — real `extraction`
    job producing real `RuleCandidate`s.
  - `tests.ai5t02.conftest.owner` / `seed_conflict_finding` — the second
    "knowledge_owner" account (knowledge.confirm + conflict.resolve) and a
    real SOP-vs-code `finding` job whose `conflict.resolve` decision is the
    exact "committed decision event" invalidation consumes.

`seed_dependent_relation` additionally runs a real `relation` job
over the same two candidates so there is a real, persisted dependent (a
`proposed_edges` entry referencing both candidate ids) whose staleness after
the decision is what N1/the stale-protection probe must observe.
"""

from __future__ import annotations

from uuid import uuid4

from infra.worker import job_worker

from tests.ai4t01.conftest import DSN, login, setup  # noqa: F401
from tests.ai4t02.conftest import seed_extraction_candidates  # noqa: F401
from tests.ai5t02.conftest import owner, seed_conflict_finding  # noqa: F401


def seed_dependent_relation(setup, requester_client, candidate_ids):
    """Real `relation` job over `candidate_ids`; returns the
    persisted job body so a test can read `execution_result.proposed_edges`
    (the real "dependent" whose staleness after a decision must be
    detectable)."""
    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    job_id = f"job-relation-{uuid4().hex}"
    resp = requester_client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "relation", "job_id": job_id,
        "payload": {"candidate_ids": candidate_ids}})
    assert resp.status_code == 200, resp.text
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "relation-worker")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    status = requester_client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert status.status_code == 200, status.text
    return status.json()


def current_epoch(setup, requester_client=None):
    """Reads the workspace head's `decision_epoch` directly through the
    store (no route exposes the bare head today) -- the pin a
    stale-protection check compares against."""
    store = setup["store"]
    head = store.head(setup["tenant"], setup["workspace"])
    return head.decision_epoch


def current_head(setup):
    """Real, fresh `WorkspaceHead` (version + decision_epoch) -- callers
    building a `/v2/conflict/resolve` body need `head.version` for
    `expected_version` (there is no bare-head route, so tests read the
    store directly, exactly as `owner`/`seed_conflict_finding` already do
    elsewhere)."""
    store = setup["store"]
    return store.head(setup["tenant"], setup["workspace"])
