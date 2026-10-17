"""Fixtures for the readiness and approval-digest tests (`kind="approval"` jobs).

Approval jobs reuse the generic `POST /v2/jobs` / `GET /v2/jobs/{id}` route
pair (one dispatch mechanism, never a bespoke route). The helpers below
define the payload shape the tests use; the OBSERVABLE behavior the tests
assert (reproducible digest; per-dependency staleness; specific
machine-readable blocked reasons; no self-reference; snapshot-not-approval)
is the contract, not the exact field layout.

Reuses `tests.ai4t01.conftest` (`setup`/`login`/`publish_with_chunk`/
`drain_outbox`, real PostgreSQL, real `infra.judge.app`) and
`tests.ai7t01.conftest`/`tests.ai7t02.conftest`'s real "impact"/
"simulation"/"proposal"/"review" job helpers verbatim (one shared
fixture-resolution path), since the real upstream input of an approval is
a COMPLETED `kind="proposal"` job reviewed by a COMPLETED `kind="review"`
job.

No production module is touched by this file.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.worker import job_worker

from tests.ai4t01.conftest import DSN, drain_outbox, login, publish_with_chunk, setup  # noqa: F401
from tests.ai5t02.conftest import owner  # noqa: F401
from tests.ai7t01.conftest import (  # noqa: F401
    claim_proposal_job,
    create_confirmed_python_intent,
    create_real_impact_job,
    create_real_simulation_job,
    get_job_status,
    submit_claim_and_get,
    submit_job,
)
from tests.ai7t02.conftest import (  # noqa: F401
    create_real_clean_yaml_proposal_job,
    create_real_yaml_proposal_job,
    submit_claim_and_get_review,
)


PROPOSAL_KIND = "proposal"
REVIEW_KIND = "review"
#: Job kind name. Follows the "proposal"/"review" -> "approval" naming
#: convention, the next noun in the pipeline ("... -> canonical approval digest, immutable
#: approval view, and machine-readable readiness reasons").
APPROVAL_KIND = "approval"


def build_real_ready_review_bundle(setup, client, workspace, monkeypatch, owner, *,
                                    target_artifact_id="config-approval.yaml",
                                    expected_current=14, proposed=30, with_contract=False):
    """Builds a real, COMPLETED `kind="proposal"` job followed by a
    real, COMPLETED `kind="review"` job reviewing it -- the exact
    real upstream dependency pair an approval consumes ("consume exact
    proposal/reviews/tests"). Returns
    `(proposal_job_id, review_job_id, proposal_body, review_body)`.

    Reuses `tests.ai7t02.conftest.create_real_clean_yaml_proposal_job` (a
    real CONFIRMED intent, never a fabricated `intent_id`) so the resulting
    review board is unanimous PASS on every role -- isolates the approval's
    OWN readiness computation from an unrelated upstream `BUSINESS_REVIEW` FAIL."""
    proposal_job_id, proposal_body = create_real_clean_yaml_proposal_job(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id=target_artifact_id,
        expected_current=expected_current, proposed=proposed)
    review_body = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        worker_id="approval-fixture-review-worker", with_contract=with_contract)
    assert review_body["execution_status"] == "COMPLETED", review_body
    review_job_id = review_body["job_id"] if "job_id" in review_body else review_body.get("id")
    return proposal_job_id, review_job_id, proposal_body, review_body


def submit_approval_job(client, workspace, *, proposal_job_id, review_job_id,
                        job_id=None, expected_decision_epoch=None, extra_payload=None):
    """Submit a `kind="approval"` job through the real generic
    `POST /v2/jobs` route. The two identifiers the contract unambiguously
    requires ("consume exact proposal/reviews/tests") are `proposal_job_id`
    and `review_job_id`."""
    payload = {"proposal_job_id": proposal_job_id, "review_job_id": review_job_id}
    if expected_decision_epoch is not None:
        payload["expected_decision_epoch"] = expected_decision_epoch
    if extra_payload:
        payload.update(extra_payload)
    return client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": APPROVAL_KIND,
        "job_id": job_id or f"job-approval-{uuid4().hex}",
        "payload": payload})


def claim_approval_job(setup, workspace, *, worker_id="approval-worker"):
    store, tenant = setup["store"], setup["tenant"]
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, worker_id)
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    return claim


def submit_claim_and_get_approval(setup, client, workspace, *, proposal_job_id, review_job_id,
                                  job_id=None, expected_decision_epoch=None,
                                  extra_payload=None, worker_id="approval-worker"):
    """PROPOSED end-to-end helper mirroring
    `tests.ai7t01.conftest.submit_claim_and_get` /
    `tests.ai7t02.conftest.submit_claim_and_get_review`'s own exact
    submit->claim->GET convention. Callers that expect a REJECTED
    submission (400/409 at the route layer) must call `submit_approval_job`
    directly instead."""
    job_id = job_id or f"job-approval-{uuid4().hex}"
    resp = submit_approval_job(
        client, workspace, proposal_job_id=proposal_job_id, review_job_id=review_job_id,
        job_id=job_id, expected_decision_epoch=expected_decision_epoch,
        extra_payload=extra_payload)
    assert resp.status_code == 200, resp.text
    claim_approval_job(setup, workspace, worker_id=worker_id)
    return get_job_status(client, workspace, job_id)
