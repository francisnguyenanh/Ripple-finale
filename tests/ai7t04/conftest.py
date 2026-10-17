"""Fixtures for the human approve/reject/request-changes decision tests.

Contract under test (a human decision on a PatchProposal):

  POST /v2/jobs/{job_id}/decision
    body: {workspace_id, action: "approve"|"reject"|"request_changes",
           reason (required, non-empty), expected_approval_digest,
           idempotency_key, high_risk (bool, optional)}
    - `job_id` is the "approval" job's own id (id-space = the approval
      job that carries `approval_digest`/`freshness`/`snapshot`, NEVER the
      `proposal_job_id` or a synthetic decision target; two
      structurally similar decision routes must not be assumed to share an
      id space merely by analogy to `/v2/knowledge/confirm` or
      `/v2/conflict/resolve`).
    - Route recomputes/rechecks freshness against the CURRENT head at
      decision time (never trusts the approval job's cached execution_result
      alone) and refuses if `expected_approval_digest` does not match the
      CURRENT digest for that exact dependency snapshot.
    - Decisions are recorded in a dedicated ledger
      (`store.record_change_decision(...)`), NOT via
      `record_human_decision` reuse, because that shared method
      unconditionally bumps `decision_epoch` and nulls
      `knowledge_revision_id` on every call (`infra/adapters/
      v2_postgres.py` ~1421-1423) -- but an approval attestation must not
      advance the semantic epoch. Reusing it naively would
      immediately stale the very digest being approved.
    - `require_distinct_approver`/self-approval-relaxed-mode is enforced the
      same way `POST /v2/jobs/{job_id}/approve` does today, keyed to the
      proposal job's own `requested_by` vs the deciding actor -- EXCEPT that
      a `high_risk=true` decision must ALWAYS require a distinct approver,
      even when the workspace policy sets `require_distinct_approver=False`
      (a high-risk decision cannot be bypassed by the self-approval setting).
    - `action="request_changes"` triggers the existing
      `action="revise"` proposal-job mechanism (a NEW proposal job version,
      `previous_proposal_job_id=<the reviewed proposal_job_id>`), reusing
      that mechanism rather than inventing a second versioning scheme.
    - Idempotent retry: same `idempotency_key` + same payload -> same
      recorded decision, no duplicate row/transition. Same key + different
      payload -> conflict (409), never silently overwritten.
    - Body `actor_id`/`patch`/digest overrides are never trusted; only the
      server-resolved authority and the stored bundle are used (mirrors the
      route module's own docstring: "Nothing in the JSON body or headers
      ... is ever treated as authority").

The OBSERVABLE behavior asserted by the tests in this directory (correct
actor/reason/digest/audit; high-risk unbypassable; stale/forged/revoked/
wrong-role refusal with no partial write; idempotent retry; no duplicate
transition) is the contract.

Reuses `tests.ai4t01.conftest` (`setup`/`login`/`publish_with_chunk`/
`drain_outbox`) and `tests.ai7t03.conftest` (`build_real_ready_review_bundle`,
`submit_claim_and_get_approval`) verbatim (one shared
fixture-resolution path). No production module is touched by this file.
"""

from __future__ import annotations

from uuid import uuid4

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
from tests.ai7t03.conftest import (  # noqa: F401
    APPROVAL_KIND,
    build_real_ready_review_bundle,
    claim_approval_job,
    submit_approval_job,
    submit_claim_and_get_approval,
)


def build_real_ready_approval(setup, client, workspace, monkeypatch, owner, *,
                              target_artifact_id="config-approval-t04.yaml",
                              expected_current=14, proposed=30):
    """Builds a real, COMPLETED `kind="approval"` job over a real,
    COMPLETED proposal + unanimous-pass review pair. Returns
    `(approval_job_id, approval_body, proposal_job_id, review_job_id)`.
    `approval_body["execution_result"]["approval_digest"]` is the exact
    digest a decision route must be handed (`expected_approval_digest`)."""
    proposal_job_id, review_job_id, _proposal_body, _review_body = (
        build_real_ready_review_bundle(
            setup, client, workspace, monkeypatch, owner,
            target_artifact_id=target_artifact_id,
            expected_current=expected_current, proposed=proposed, with_contract=True))
    approval_body = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="t04-approval-fixture-worker")
    assert approval_body["execution_status"] == "COMPLETED", approval_body
    approval_job_id = approval_body.get("job_id") or approval_body.get("id")
    return approval_job_id, approval_body, proposal_job_id, review_job_id


def submit_decision(client, workspace, *, job_id, action, reason="looks good",
                    expected_approval_digest=None, idempotency_key=None,
                    high_risk=False, extra_body=None):
    """POST /v2/jobs/{job_id}/decision -- see module docstring.
    `job_id` is the APPROVAL job id, not the proposal job."""
    body = {"workspace_id": workspace, "action": action, "reason": reason,
            "high_risk": high_risk}
    if expected_approval_digest is not None:
        body["expected_approval_digest"] = expected_approval_digest
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    if extra_body:
        body.update(extra_body)
    return client.post(f"/v2/jobs/{job_id}/decision", json=body)


def get_decision_history(client, workspace, *, job_id):
    """PROPOSED read surface: GET /v2/jobs/{job_id}/decisions -- the
    "approval history panel" the task card's Outputs line names."""
    return client.get(f"/v2/jobs/{job_id}/decisions", params={"workspace_id": workspace})
