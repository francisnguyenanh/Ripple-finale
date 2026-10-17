"""Fixtures for the review-bundle tests.

Reuses `tests.ai4t01.conftest.setup`/`login`/`publish_with_chunk`/
`drain_outbox` (real PostgreSQL, real `infra.judge.app`) (one shared
resolution path, not reinvented), and reuses `tests.ai7t01.conftest`'s real
"impact"/"simulation" job helpers verbatim, since the review's real upstream
input is an already-generated, COMPLETED `kind="proposal"` job -- not an
invented shape.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from infra.worker import job_worker

from tests.ai4t01.conftest import DSN, drain_outbox, login, publish_with_chunk, setup  # noqa: F401
from tests.ai7t01.conftest import (  # noqa: F401
    claim_proposal_job,
    create_confirmed_python_intent,
    create_real_impact_job,
    create_real_simulation_job,
    get_job_status,
    submit_claim_and_get,
    submit_job,
)
from tests.ai5t02.conftest import owner  # noqa: F401


PROPOSAL_KIND = "proposal"
REVIEW_KIND = "review"


def create_real_yaml_proposal_job(setup, client, workspace, *, target_artifact_id,
                                   impact_job_id, simulation_job_id,
                                   expected_current, proposed, path="refund_window_days",
                                   intent_id=None):
    """A real, COMPLETED `kind="proposal"` job with
    a real typed YAML edit -- reused here as the exact candidate under
    review, not re-tested. Mirrors
    `tests/ai7t01/test_patch_generation.py::test_c1_yaml_typed_edit_applies_real_locator_value`'s
    own real payload shape exactly (a fabricated `intent_id` is sufficient
    for the yaml_typed_edit branch, confirmed by that same precedent test)."""
    body = submit_claim_and_get(setup, client, workspace, kind=PROPOSAL_KIND, payload={
        "intent_id": intent_id or f"intent-{uuid4().hex}",
        "impact_job_id": impact_job_id, "simulation_job_id": simulation_job_id,
        "target_artifact_id": target_artifact_id, "edit_kind": "yaml_typed_edit",
        "edits": [{"path": path, "expected_current": expected_current, "proposed": proposed}]})
    assert body["execution_status"] == "COMPLETED", body
    return body["job_id"] if "job_id" in body else body.get("id"), body


def create_real_clean_yaml_proposal_job(setup, client, workspace, monkeypatch, owner, *,
                                        target_artifact_id, expected_current, proposed,
                                        path="refund_window_days"):
    """Same real chain as `create_real_yaml_proposal_job`, but with a REAL
    CONFIRMED Intent (`tests.ai7t01.conftest.create_confirmed_python_intent`,
    the real `/v2/intent/parse`->`/v2/intent/confirm` chain)
    instead of a fabricated `intent_id` string. A fabricated `intent_id`
    always resolves to a real `report_unavailable:intent_not_found` blocker
    on the proposal itself (`infra/worker/proposal.py::_resolve_intent`,
    confirmed by direct read and by this round's own first failed attempt at
    the "unanimous role PASS" test below) -- which would make BUSINESS_REVIEW
    FAIL for a real, unrelated-to-this-task reason, masking the actual
    property B2-a needs to prove (a required TOOL failure/unavailability
    still blocks even when every ROLE is unanimous PASS). Using a real
    confirmed intent here removes that confound; `edit_kind` does not need
    to match the intent's own confirmed subject for a `yaml_typed_edit`
    (verified: `_resolve_intent` only checks existence, never edit-kind
    correspondence)."""
    intent_id, _candidate_id = create_confirmed_python_intent(
        setup, monkeypatch, owner, raw_value=999)
    publish_with_chunk(setup["store"], setup["tenant"], workspace,
                       artifact_id=target_artifact_id,
                       text=f"{path}: {expected_current}\n")
    drain_outbox(setup["store"], setup["tenant"], workspace)
    impact_job_id = create_real_impact_job(setup, client, workspace)
    simulation_job_id = create_real_simulation_job(setup, client, workspace)
    body = submit_claim_and_get(setup, client, workspace, kind=PROPOSAL_KIND, payload={
        "intent_id": intent_id, "impact_job_id": impact_job_id,
        "simulation_job_id": simulation_job_id, "target_artifact_id": target_artifact_id,
        "edit_kind": "yaml_typed_edit",
        "edits": [{"path": path, "expected_current": expected_current, "proposed": proposed}]})
    assert body["execution_status"] == "COMPLETED", body
    assert body["execution_result"]["blockers"] == [], body["execution_result"]
    job_id = body["job_id"] if "job_id" in body else body.get("id")
    return job_id, body


def submit_review_job(client, workspace, *, proposal_job_id, evaluated_hash=None,
                       job_id=None, simulate_role_unavailable=None,
                       omit_evaluated_hash=False):
    """Submit a `kind="review"` job through the real `POST /v2/jobs` route.

    A forged random `evaluated_hash` (`f"sha256:{uuid4().hex}"`) is never
    supplied by default: the real executor independently recomputes and
    compares the evaluated hash (B3-b), so such a default would silently turn
    every "happy path" caller of this helper into an unintended
    bypass-attempt test. `evaluated_hash` is omitted from the payload entirely
    unless the caller explicitly supplies one (real product behavior: a
    caller MAY omit `evaluated_hash` altogether and let the executor report
    its own real, independently-computed value -- see
    `infra/worker/review.py::_real_evaluated_hash`)."""
    payload = {"proposal_job_id": proposal_job_id}
    if evaluated_hash is not None and not omit_evaluated_hash:
        payload["evaluated_hash"] = evaluated_hash
    if simulate_role_unavailable is not None:
        payload["simulate_role_unavailable"] = list(simulate_role_unavailable)
    return client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": REVIEW_KIND,
        "job_id": job_id or f"job-review-{uuid4().hex}",
        "payload": payload})


def store_fixture_contract(store, tenant, workspace, proposal_job_id):
    """Store-level core of `state_fixture_contract` (idempotent: an existing contract for the proposal
    is reused, so a second review of the same proposal does not hit the unique key)."""
    from infra.worker import verification_contract as vc
    existing = store.verification_contracts_for_proposal(tenant, workspace, proposal_job_id)
    if existing:
        return existing[0]["contract_id"]
    proposal = store.job_record(tenant, workspace, proposal_job_id)
    result = proposal.get("execution_result") or {}
    edit_kind, edits = result.get("edit_kind"), result.get("edits") or []
    kind = vc.EDIT_KIND_TO_CONTRACT_KIND.get(edit_kind)
    if kind is None or len(edits) != 1:
        return None
    locator = vc.locator_for_edit(edit_kind, edits[0])
    target = result.get("target_artifact_id")
    pinned = (result.get("pinned_input") or {}).get("pinned_source_revision_id")
    before = store.read_artifact_blob(tenant, workspace, pinned, target)
    row = store.record_verification_contract(
        tenant, workspace, proposal_job_id=proposal_job_id,
        logical_path=before.get("logical_path") or target, kind=kind, locator=locator,
        expected=edits[0]["after"], author_account_id="fixture-contract-author",
        provenance={"fixture": "legacy mechanics: expected taken from the recorded edit"})
    return row["contract_id"]


def state_fixture_contract(setup, workspace, proposal_job_id):
    """LEGACY-MECHANICS FIXTURE SHORTCUT (W1): store the verification contract an operator would
    state for a one-edit typed proposal, taking the expected value from the proposal's own recorded
    edit. This is exactly the circularity the production route design forbids (independence is
    proven in tests/w1_prod); it exists only so the pre-W1 apply/publish/verification mechanics
    tests can still reach a READY approval now that a BLOCKED review can no longer be approved or
    applied. Returns the contract id, or None when the proposal has no single typed edit."""
    return store_fixture_contract(setup["store"], setup["tenant"], workspace, proposal_job_id)


def submit_claim_and_get_review(setup, client, workspace, *, proposal_job_id,
                                 evaluated_hash=None, job_id=None,
                                 simulate_role_unavailable=None,
                                 worker_id="review-worker", with_contract=False):
    """Submit a real `kind="review"` job, expect real `200` at submission
    (the row IS created; the real accept/blocker/failure decision happens
    asynchronously at worker-claim time -- same pattern as for
    `kind="proposal"`), claim it
    through the real worker, and return the real persisted
    `GET /v2/jobs/{id}` body."""
    payload = {"proposal_job_id": proposal_job_id}
    if with_contract:
        contract_id = state_fixture_contract(setup, workspace, proposal_job_id)
        if contract_id is not None:
            payload["verification_contract_id"] = contract_id
    if evaluated_hash is not None:
        payload["evaluated_hash"] = evaluated_hash
    if simulate_role_unavailable is not None:
        payload["simulate_role_unavailable"] = list(simulate_role_unavailable)
    return submit_claim_and_get(setup, client, workspace, kind=REVIEW_KIND,
                                payload=payload, job_id=job_id, worker_id=worker_id)


def create_synthetic_python_proposal_job(setup, client, workspace, *,
                                         target_artifact_id, python_source,
                                         job_id=None):
    """Fabricates a real, COMPLETED `kind="proposal"` job whose
    `execution_result` stages the EXACT given Python source, WITHOUT going
    through `ProposalExecutor`'s own edit vocabulary.

    Why this exists: this repo's only real `ProposalExecutor` python/markdown edit path is a
    first-run-of-digits literal substitution; it cannot produce a staged
    `>`/`>=` COMPARISON-OPERATOR swap, so B1-a (the
    boundary test that must "catch `>`/`>=` errors") cannot be exercised
    through that executor's own real output. The candidate bytes are
    therefore constructed directly and staged/completed as a
    job against the REAL store (`store.stage_blob`, `store.complete_job`)
    instead -- this is real, product-store-backed state (a real row, a real
    content-addressed blob, readable back through the exact same
    `store._verified_blob` primitive `ReviewExecutor` itself uses), not a
    mock -- so `ReviewExecutor.execute()`'s real `simulation_exact_patch`
    check genuinely imports/executes THIS exact candidate inside the
    real isolated sandbox (`infra.worker.sandbox_runner.run_isolated`), the
    identical real mechanism a real `ProposalExecutor`-produced python
    candidate would go through. The ONLY thing bypassed is
    `ProposalExecutor`'s own limited edit-authoring vocabulary, not any part
    of `ReviewExecutor`'s real validation. `job_record`'s `kind` is real
    ("proposal", set by the real `POST /v2/jobs` route above), and
    `execution_status` is genuinely `COMPLETED` via the real
    `store.complete_job` primitive (interface docstring: "accepts
    QUEUED or RUNNING") -- not a fabricated/forged job-shape."""
    store, tenant = setup["store"], setup["tenant"]
    job_id = job_id or f"job-proposal-synth-{uuid4().hex}"
    # The route's real allowlist check (C2-a) requires
    # `target_artifact_id` to already exist in the CURRENT published source
    # -- publish a placeholder first so submission is accepted.
    publish_with_chunk(store, tenant, workspace, artifact_id=target_artifact_id,
                       text="# placeholder published content\n")
    drain_outbox(store, tenant, workspace)
    resp = submit_job(client, workspace, kind=PROPOSAL_KIND, job_id=job_id, payload={
        "intent_id": f"intent-{uuid4().hex}",
        "impact_job_id": f"job-impact-{uuid4().hex}",
        "simulation_job_id": f"job-sim-{uuid4().hex}",
        "target_artifact_id": target_artifact_id,
        "edit_kind": "python",
    })
    assert resp.status_code == 200, resp.text

    content = python_source.encode("utf-8")
    staged_hash = store.stage_blob(content)
    after_hash = hashlib.sha256(content).hexdigest()
    before_hash = hashlib.sha256(b"# synthetic prior version\n").hexdigest()
    result = {
        "intent_id": f"intent-{uuid4().hex}",
        "impact_job_id": f"job-impact-{uuid4().hex}",
        "simulation_job_id": f"job-sim-{uuid4().hex}",
        "target_artifact_id": target_artifact_id,
        "edit_kind": "python",
        "edits": [{
            "path": target_artifact_id,
            "rationale": "AI7-T02 TESTER TEST_DONE synthetic >/>= boundary fixture",
            "evidence_ref": {"kind": "synthetic_fixture", "id": "boundary-gt-vs-gte"},
        }],
        "classification": "auto",
        "is_no_op": False,
        "staged_blob_hash": staged_hash,
        "before_blob_hash": before_hash,
        "after_blob_hash": after_hash,
        "proposal_version": 1,
        "blockers": [],
    }
    store.complete_job(tenant, workspace, job_id, result=result)
    # FIFO-claim-ordering hazard: completing the job directly
    # (bypassing a real worker claim) leaves its own `JOB_QUEUED` outbox
    # event un-drained; a later `claim_and_admit_one` call (e.g. for the
    # review job submitted against this proposal) would otherwise claim
    # THIS stale event first, FIFO, and never reach the review job's own
    # event. Drain it now, exactly like every other fixture in this
    # directory drains after a side-effecting store call.
    drain_outbox(store, tenant, workspace)
    return job_id, result
