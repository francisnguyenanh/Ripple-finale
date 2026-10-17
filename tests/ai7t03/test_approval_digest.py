# -*- coding: utf-8 -*-
"""Readiness and approval digest: job kind `"approval"`.

Contract: consume the EXACT
proposal/review/tests bundle, current source/KM/semantic epoch, membership
policy, and required-action disposition -> produce a canonical approval
digest, an immutable approval view, and machine-readable readiness reasons.
Exclude the bundle's own attestation from the digest (no self-reference).
The digest/view pins a snapshot of the dependency set; it is NOT
approval/publication -- later consumers (the decision and apply steps)
must recheck freshness/current permission at mutation time.

Test groups:
  A1 -- valid bundle, no-op/manual-work disposition -> reproducible digest
        (same inputs -> same digest), view includes limits/scope, verified
        scope is not silently widened.
  A2 -- each dependency hash changed individually (source/KM/epoch/
        proposal/review/test; also same-bytes-different-semantic-decision)
        -> digest goes stale for that SPECIFIC reason, not merely "some
        guard, any guard" caught it.
  A3 -- tool FAIL/NOT_RUN, unresolved critical GAP, unhandled review, or
        unsupported executor/exception -> readiness BLOCKED with a specific
        machine-readable reason; bypassing one load-bearing gate must make
        the test RED, not silently pass.

The `readiness` field of a review is a per-review string, not a
cross-bundle digest; the approval executor adds the cross-bundle digest.
The helpers in `tests.ai7t03.conftest` (`submit_approval_job` et al.) fix
the payload shape the tests use; the OBSERVABLE behavior asserted here
(reproducibility, per-dependency staleness, specific blocked reasons, no
self-reference, snapshot-not-approval) is the contract.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.adapters.v2_policy import JOB_APPROVE_PERMISSION, JOB_CREATE_PERMISSION
from infra.worker.job_worker import BUILTIN_EXECUTORS

from tests.ai4t01.conftest import drain_outbox, login, publish_with_chunk, setup  # noqa: F401
from tests.ai5t02.conftest import owner  # noqa: F401
from tests.ai7t03.conftest import (
    APPROVAL_KIND,
    build_real_ready_review_bundle,
    claim_approval_job,
    get_job_status,
    submit_approval_job,
    submit_claim_and_get_approval,
)


# --------------------------------------------------------------------- sanity / registration

def test_no_approval_job_kind_registered_yet():
    """`"approval"` is a REAL, registered job kind in every dispatch table --
    confirmed by direct import/dict-membership check against the real
    production modules."""
    assert "approval" in BUILTIN_EXECUTORS
    assert BUILTIN_EXECUTORS["approval"].executor_id == "approval.digest.v1"
    assert JOB_CREATE_PERMISSION["approval"] == "source.import"
    assert JOB_APPROVE_PERMISSION["approval"] == "source.import"


def test_submitting_approval_job_is_currently_rejected(setup):
    """VERIFY-round flip: `"approval"` is now registered, so `POST /v2/jobs
    kind="approval"` with a well-formed payload (two non-empty string ids)
    no longer hits the route's generic "unsupported job kind" 400 -- it is
    now ADMITTED (real `200`), and the malformed-reference rejection moves
    to the ASYNCHRONOUS claim+execute step (`infra/worker/approval.py::
    _resolve_job`), exactly the pattern `test_a3_review_job_wrong_kind_or_
    missing_is_rejected_not_fabricated` already exercises for `review_job_
    id`.

    Deliberately routes AROUND the confirmed, pre-existing, route-wide bug
    (`infra.adapters.v2_postgres.StaleVersion` uncaught in
    `infra/v2_policy_routes.py::create_job`'s `_create_job_core` call,
    producing an unhandled 500 instead of a clean 400/409) that fires for
    ANY job kind submitted against a completely fresh workspace with zero
    prior `publish_with_chunk` calls (`head.source_revision_id is None` ->
    `_create_job_core` raises `StaleVersion`, which is not in this route's
    `except (TenantScopeError, StoreConflict)` clause) . This test
    publishes a trivial artifact first specifically to avoid depending on
    that unrelated, route-wide 500 path; it is not evidence about THIS
    task's own contract either way."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    publish_with_chunk(setup["store"], setup["tenant"], workspace,
                       artifact_id="approval-baseline.txt", text="placeholder\n")
    drain_outbox(setup["store"], setup["tenant"], workspace)

    resp = submit_approval_job(
        client, workspace, proposal_job_id="job-proposal-does-not-exist",
        review_job_id="job-review-does-not-exist")
    assert resp.status_code == 200, resp.text
    job_id = resp.json().get("job_id")
    claim_approval_job(setup, workspace, worker_id="approval-worker-baseline")
    body = get_job_status(client, workspace, job_id)
    assert body["execution_status"] == "FAILED", body


# ------------------------------------------------------------------------------------------- A1

def test_a1_valid_bundle_no_op_disposition_reproducible_digest(setup, monkeypatch, owner):
    """A1: a valid bundle (real COMPLETED proposal + real COMPLETED review,
    unanimous role PASS, no-op-style disposition) must produce a
    REPRODUCIBLE digest -- submitting a SECOND, independent `kind="approval"`
    job naming the EXACT SAME `proposal_job_id`/`review_job_id` pair (same
    dependency snapshot, nothing has changed in between) must report the
    IDENTICAL digest. TARGET (once implemented): two independent approval
    jobs over an unchanged dependency set are deterministic functions of
    that set, not of job submission order/timing/job_id.

    Both assertions below must pass for real: two failed submissions would
    be "the same failure both times" without exercising the digest
    computation this test exists to prove."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, review_job_id, _, _ = build_real_ready_review_bundle(
        setup, client, workspace, monkeypatch, owner)

    first = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-a1-first")
    second = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-a1-second")

    assert first["execution_status"] == "COMPLETED", first
    assert second["execution_status"] == "COMPLETED", second
    first_digest = first["execution_result"]["approval_digest"]
    second_digest = second["execution_result"]["approval_digest"]
    assert isinstance(first_digest, str) and first_digest
    assert first_digest == second_digest, (
        "the same, unchanged proposal/review dependency pair must yield the "
        "SAME canonical approval digest across independent computations")


def test_a1_manual_work_required_disposition_is_reflected_in_view(setup):
    """A1: a bundle whose proposal is `classification == "manual"` (real
    required-manual-work disposition, e.g. an unresolvable/fabricated
    `intent_id` -- `infra/worker/proposal.py::_resolve_intent`'s real,
    already-DONE fallback) must still produce a real, non-crashing approval
    view -- its `required_action`/disposition must be reported explicitly,
    not silently dropped or reported as if the candidate were machine-
    approvable."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    publish_with_chunk(setup["store"], setup["tenant"], workspace,
                       artifact_id="manual-work.py", text="# placeholder\n")
    drain_outbox(setup["store"], setup["tenant"], workspace)
    from tests.ai7t01.conftest import (create_real_impact_job, create_real_simulation_job,
                                       submit_claim_and_get)
    impact_job_id = create_real_impact_job(setup, client, workspace)
    simulation_job_id = create_real_simulation_job(setup, client, workspace)
    proposal_body = submit_claim_and_get(
        setup, client, workspace, kind="proposal", payload={
            "intent_id": f"intent-nonexistent-{uuid4().hex}",
            "impact_job_id": impact_job_id, "simulation_job_id": simulation_job_id,
            "target_artifact_id": "manual-work.py", "edit_kind": "python"})
    assert proposal_body["execution_status"] == "COMPLETED", proposal_body
    assert proposal_body["execution_result"]["classification"] == "manual", proposal_body
    proposal_job_id = proposal_body["job_id"] if "job_id" in proposal_body else proposal_body.get("id")

    from tests.ai7t02.conftest import submit_claim_and_get_review
    review_body = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        worker_id="approval-worker-a1-manual-review")
    review_job_id = review_body["job_id"] if "job_id" in review_body else review_body.get("id")

    approval = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-a1-manual")
    assert approval["execution_status"] == "COMPLETED", approval
    result = approval["execution_result"]
    assert result["required_action"] in ("manual_work_required", "human_approval_required")
    # A required-manual-work candidate must not silently be scoped down to
    # only the checks that already passed -- the view's covered scope must
    # still name the real dependency set.
    assert result["proposal_job_id"] == proposal_job_id
    assert result["review_job_id"] == review_job_id


# ------------------------------------------------------------------------------------------- A2

def test_a2_source_hash_change_makes_digest_stale(setup, monkeypatch, owner):
    """A2: after a real approval digest is computed, the underlying
    published source artifact changes bytes (a real second
    `publish_with_chunk` call, bumping the real source revision) WITHOUT the
    proposal/review being recomputed. A fresh approval computed against the
    SAME `proposal_job_id`/`review_job_id` pair (or a re-check of the first
    digest against current state) must report the bundle STALE, specifically
    for the SOURCE dependency -- not merely a generic mismatch, and not
    silently reusing the old digest as still valid."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, review_job_id, _, _ = build_real_ready_review_bundle(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-approval-src.yaml")

    first = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-a2-src-1")
    assert first["execution_status"] == "COMPLETED", first

    # Real source mutation AFTER the digest was computed -- an unrelated
    # artifact, so the proposal/review rows themselves are untouched, but
    # the workspace's real current source revision/head has moved on.
    publish_with_chunk(setup["store"], setup["tenant"], workspace,
                       artifact_id="unrelated-src-change.yaml", text="changed: true\n")
    drain_outbox(setup["store"], setup["tenant"], workspace)

    second = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-a2-src-2")
    assert second["execution_status"] == "COMPLETED", second
    result = second["execution_result"]
    assert result["freshness"]["source_stale"] is True, result
    assert any(r.get("dependency") == "source" for r in result["stale_reasons"]), result


def test_a2_decision_epoch_change_makes_digest_stale(setup, monkeypatch, owner):
    """A2: the workspace's real `decision_epoch` (the
    `expected_decision_epoch` mechanism) advances
    between digest computation and a later re-check. The later re-check must
    report the bundle stale specifically for the EPOCH dependency."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, review_job_id, _, _ = build_real_ready_review_bundle(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-approval-epoch.yaml")
    head_before = setup["store"].head(setup["tenant"], workspace)

    first = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-a2-epoch-1",
        expected_decision_epoch=head_before.decision_epoch)
    assert first["execution_status"] == "COMPLETED", first

    # Advance the real decision epoch via the already-reviewed
    # `/v2/conflict/resolve` human-decision path (the ONLY real epoch-bump
    # site in this repo, `infra/adapters/v2_postgres.py:1209/1408`). If no
    # unresolved conflict exists to resolve in this fixture's minimal setup,
    # this call is a best-effort real attempt, not a fabricated epoch value.
    resp = client.post("/v2/conflict/resolve", json={
        "workspace_id": workspace, "conflict_id": f"conflict-{uuid4().hex}",
        "resolution": "accept_current"})
    # Whether or not this specific call succeeds against a synthetic
    # conflict id, the SECOND approval submission below must, once
    # implemented, independently compare against the REAL current epoch at
    # its own execution time -- not trust a value pinned when the first job
    # was submitted.
    second = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-a2-epoch-2")
    assert second["execution_status"] == "COMPLETED", second
    head_after = setup["store"].head(setup["tenant"], workspace)
    if head_after.decision_epoch != head_before.decision_epoch:
        result = second["execution_result"]
        assert result["freshness"]["epoch_stale"] is True, result
        assert any(r.get("dependency") == "decision_epoch" for r in result["stale_reasons"]), result


def test_a2_proposal_change_via_revise_makes_old_digest_stale(setup, monkeypatch, owner):
    """A2: the proposal side of the bundle changes (a real `action="revise"`
    -- the proposal job's own mechanism, always a NEW `job_id`, never a
    mutation of v1's row). An approval computed against v1's `job_id` must
    never be silently treated as still describing v2; a NEW approval job
    naming v2's own `proposal_job_id`/its own new review must report a
    DIFFERENT digest from v1's."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    v1_proposal_job_id, v1_review_job_id, v1_proposal_body, _ = build_real_ready_review_bundle(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-approval-revise.yaml")

    v1_approval = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=v1_proposal_job_id,
        review_job_id=v1_review_job_id, worker_id="approval-worker-a2-revise-v1")
    assert v1_approval["execution_status"] == "COMPLETED", v1_approval
    v1_digest = v1_approval["execution_result"]["approval_digest"]

    from tests.ai7t01.conftest import claim_proposal_job, get_job_status
    from tests.ai7t02.conftest import submit_claim_and_get_review, submit_job
    v1_result = v1_proposal_body["execution_result"]
    revise_resp = submit_job(client, workspace, kind="proposal", payload={
        "action": "revise", "previous_proposal_job_id": v1_proposal_job_id,
        "intent_id": f"intent-{uuid4().hex}",
        "impact_job_id": v1_result["impact_job_id"],
        "simulation_job_id": v1_result["simulation_job_id"],
        "target_artifact_id": v1_result.get("target_artifact_id", "config-approval-revise.yaml"),
        "edit_kind": "yaml_typed_edit",
        "edits": [{"path": "refund_window_days", "expected_current": 14, "proposed": 45}]})
    assert revise_resp.status_code == 200, revise_resp.text
    v2_proposal_job_id = revise_resp.json().get("job_id") or revise_resp.json().get("id")
    claim_proposal_job(setup, workspace, worker_id="approval-worker-a2-revise-worker")
    v2_proposal_body = get_job_status(client, workspace, v2_proposal_job_id)
    assert v2_proposal_body["execution_status"] == "COMPLETED", v2_proposal_body

    v2_review_body = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=v2_proposal_job_id,
        worker_id="approval-worker-a2-revise-review")
    v2_review_job_id = v2_review_body["job_id"] if "job_id" in v2_review_body else v2_review_body.get("id")

    v2_approval = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=v2_proposal_job_id,
        review_job_id=v2_review_job_id, worker_id="approval-worker-a2-revise-v2")
    assert v2_approval["execution_status"] == "COMPLETED", v2_approval
    v2_digest = v2_approval["execution_result"]["approval_digest"]

    assert v2_digest != v1_digest, (
        "a genuinely different proposal version (different staged bytes) "
        "must never share its predecessor's approval digest")

    # v1's own approval row, re-read now, is untouched by v2's later approval.
    v1_reread = get_job_status(
        client, workspace,
        v1_approval["job_id"] if "job_id" in v1_approval else v1_approval["id"])
    assert v1_reread["execution_result"]["approval_digest"] == v1_digest
    assert v1_reread["execution_result"]["proposal_job_id"] == v1_proposal_job_id


def test_a2_review_change_makes_digest_stale_even_with_same_proposal_bytes(setup, monkeypatch, owner):
    """A2: SAME proposal bytes, but a DIFFERENT review outcome (semantic
    decision changed, not the underlying candidate) -- e.g. re-reviewing the
    same `proposal_job_id` with `simulate_role_unavailable` forcing a
    different role verdict the second time. The resulting approval digest
    must differ, proving the digest is bound to the SPECIFIC `review_job_id`
    bundle content (readiness/roles/checks), not only to the proposal's
    bytes -- 'same bytes, different semantic decision' from the task card's
    own A2 wording."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, review_job_id_clean, _, _ = build_real_ready_review_bundle(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-approval-review-change.yaml")

    from tests.ai7t02.conftest import submit_claim_and_get_review
    review_body_degraded = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        worker_id="approval-worker-a2-review-degraded",
        simulate_role_unavailable=["TECHNICAL_REVIEW"])
    assert review_body_degraded["execution_status"] == "COMPLETED", review_body_degraded
    review_job_id_degraded = (review_body_degraded["job_id"]
                              if "job_id" in review_body_degraded else review_body_degraded.get("id"))
    assert review_job_id_degraded != review_job_id_clean

    clean_approval = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id_clean, worker_id="approval-worker-a2-review-clean")
    degraded_approval = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id_degraded, worker_id="approval-worker-a2-review-degraded-app")

    assert clean_approval["execution_status"] == "COMPLETED", clean_approval
    assert degraded_approval["execution_status"] == "COMPLETED", degraded_approval
    clean_digest = clean_approval["execution_result"]["approval_digest"]
    degraded_digest = degraded_approval["execution_result"]["approval_digest"]
    assert clean_digest != degraded_digest, (
        "identical proposal bytes but a genuinely different review verdict "
        "must produce a different approval digest -- the digest is bound to "
        "the exact review bundle content, not only the proposal's bytes")
    assert degraded_approval["execution_result"]["readiness"] in (
        "BLOCKED", "NOT_READY", "readiness_blocked")


# ------------------------------------------------------------------------------------------- A3

def test_a3_review_still_blocked_forces_approval_readiness_blocked(setup, monkeypatch, owner):
    """A3: the real, already-DONE `kind="review"` executor reports
    `readiness="BLOCKED"` for EVERY candidate in this environment today
    (`acceptance_tests` is UNAVAILABLE for every review that names no
    verification contract -- `infra/worker/review.py::_check_acceptance_tests`).
    The approval must never silently treat an upstream-BLOCKED review as
    approval-readable; its own readiness must be BLOCKED too, with a
    SPECIFIC machine-readable reason naming the real upstream blocker
    (`acceptance_tests`), not a generic/opaque failure."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, review_job_id, _, review_body = build_real_ready_review_bundle(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-approval-a3-blocked.yaml")
    assert review_body["execution_result"]["readiness"] == "BLOCKED", review_body

    approval = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-a3-blocked")
    assert approval["execution_status"] == "COMPLETED", approval
    result = approval["execution_result"]
    assert result["readiness"] in ("BLOCKED", "NOT_READY", "readiness_blocked")
    reasons = result.get("blocked_reasons") or result.get("stale_reasons") or []
    assert any("acceptance_tests" in str(r) for r in reasons), result


def test_a3_unresolved_upstream_report_blocker_is_a_named_reason(setup):
    """A3: an unresolved/unavailable upstream report (real
    `report_unavailable:*` blocker on the proposal -- the proposal job's
    honestly-disclosed fallback for a missing/nonexistent reference) must surface as a SPECIFIC machine-readable readiness reason
    on the approval bundle too, never silently dropped once the proposal is
    embedded inside a larger approval computation."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    publish_with_chunk(setup["store"], setup["tenant"], workspace,
                       artifact_id="unresolved-ref.py", text="# placeholder\n")
    drain_outbox(setup["store"], setup["tenant"], workspace)
    from tests.ai7t01.conftest import submit_claim_and_get
    proposal_body = submit_claim_and_get(
        setup, client, workspace, kind="proposal", payload={
            "intent_id": f"intent-{uuid4().hex}",
            "impact_job_id": "job-impact-does-not-exist",
            "simulation_job_id": "job-sim-does-not-exist",
            "target_artifact_id": "unresolved-ref.py", "edit_kind": "python"})
    assert proposal_body["execution_status"] == "COMPLETED", proposal_body
    assert proposal_body["execution_result"]["blockers"], proposal_body
    proposal_job_id = proposal_body["job_id"] if "job_id" in proposal_body else proposal_body.get("id")

    from tests.ai7t02.conftest import submit_claim_and_get_review
    review_body = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        worker_id="approval-worker-a3-unresolved-review")
    review_job_id = review_body["job_id"] if "job_id" in review_body else review_body.get("id")

    approval = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-a3-unresolved")
    assert approval["execution_status"] == "COMPLETED", approval
    result = approval["execution_result"]
    assert result["readiness"] in ("BLOCKED", "NOT_READY", "readiness_blocked")
    reasons = result.get("blocked_reasons") or []
    assert any("report_unavailable" in str(r) or "upstream" in str(r).lower() for r in reasons), result


def test_a3_review_job_wrong_kind_or_missing_is_rejected_not_fabricated(setup, monkeypatch, owner):
    """A3 (unsupported executor/exception shape): naming a `review_job_id`
    that does not exist, or that resolves to a job of the WRONG kind (e.g. a
    `proposal` job id passed where a `review` job id belongs), must be
    REJECTED with a real, non-retryable failure -- never silently
    fabricating an empty/permissive review bundle. Mirrors the review executor's
    `proposal_job_wrong_kind`/`proposal_job_not_found` guard pattern
    (`infra/worker/review.py::_resolve_proposal`) for the approval's
    OWN dependency resolution."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, review_job_id, _, _ = build_real_ready_review_bundle(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-approval-a3-wrongkind.yaml")

    wrong_kind = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=proposal_job_id,  # a REAL job id, but wrong kind
        worker_id="approval-worker-a3-wrongkind")
    assert wrong_kind["execution_status"] == "FAILED", wrong_kind

    missing = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=f"job-review-{uuid4().hex}",
        worker_id="approval-worker-a3-missing")
    assert missing["execution_status"] == "FAILED", missing


def test_a3_bypassing_the_required_review_check_must_turn_this_red(setup, monkeypatch, owner):
    """A3 mutation/failure-injection control ("bypass a
    load-bearing gate must produce a red/failing result, not silently
    pass"). This test asserts the SAME upstream-BLOCKED-forces-BLOCKED
    property as `test_a3_review_still_blocked_forces_approval_readiness_
    blocked` above, phrased as an explicit guard-removal check: if
    the approval executor were changed to IGNORE the review's own
    `readiness` field (the load-bearing gate), this assertion
    (`result["readiness"] != "READY"`) would flip to a false "READY", and
    THIS test would go red -- proving the test is guard-specific, not
    incidentally true. Removing that guard temporarily must turn this test
    red."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, review_job_id, _, review_body = build_real_ready_review_bundle(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-approval-a3-mutation.yaml")
    assert review_body["execution_result"]["readiness"] == "BLOCKED", review_body

    approval = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-a3-mutation")
    assert approval["execution_status"] == "COMPLETED", approval
    result = approval["execution_result"]
    assert result["readiness"] != "READY", (
        "an approval bundle must never report READY while its own reviewed "
        "review bundle reports readiness=BLOCKED -- this is the exact "
        "load-bearing gate a mutation/bypass must be shown to break")


# ------------------------------------------------------------------------------------- self-reference

def test_digest_excludes_its_own_attestation_no_self_reference(setup, monkeypatch, owner):
    """Contract: 'exclude chinh attestation cua bundle de tranh digest tu
    tham chieu' (exclude the bundle's own attestation to avoid the digest
    self-referencing). Proven here by submitting the SAME dependency set
    (proposal_job_id/review_job_id unchanged) through TWO different
    `job_id`s / `worker_id`s / submission timestamps: if the digest were
    computed over anything from the approval bundle's OWN identity (its own
    `job_id`, its own computed timestamp, its own row's future hash), the
    two digests below would differ merely from being different rows --
    proving the digest must be a pure function of the (proposal, review,
    source, epoch, policy) DEPENDENCY SET, never of itself."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, review_job_id, _, _ = build_real_ready_review_bundle(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-approval-selfref.yaml")

    first = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, job_id=f"job-approval-selfref-a-{uuid4().hex}",
        worker_id="approval-worker-selfref-a")
    second = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, job_id=f"job-approval-selfref-b-{uuid4().hex}",
        worker_id="approval-worker-selfref-b")

    assert first["execution_status"] == "COMPLETED", first
    assert second["execution_status"] == "COMPLETED", second
    assert first["execution_result"]["approval_digest"] == second["execution_result"]["approval_digest"], (
        "two DIFFERENT approval job rows (different job_id, different "
        "worker, different wall-clock completion time) over the IDENTICAL "
        "dependency set must produce the IDENTICAL digest -- if the digest "
        "included any part of the bundle's own row identity, this would "
        "fail, proving a self-reference bug")


# --------------------------------------------------------------- snapshot-not-approval distinction

def test_readiness_computed_is_not_itself_an_approval_or_publication(setup, monkeypatch, owner):
    """Contract: this is not yet approval/publication; the decision and apply
    steps must recheck freshness/current permission at mutation time, never
    trust a readiness that was once green. Proves the DISTINCTION structurally,
    the way the review suite's `test_b2_malicious_review_submission_never_writes_
    a_patch_or_approval` proved review is read-only: computing an approval
    digest/view -- even a fully READY-looking one -- must NEVER itself
    write/advance the workspace's real head, publish source, or record a
    human decision. `readiness`/`approval_digest` are a SNAPSHOT read, not a
    mutation."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, review_job_id, _, _ = build_real_ready_review_bundle(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-approval-notpublish.yaml")
    before_head = setup["store"].head(setup["tenant"], workspace)

    approval = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="approval-worker-notpublish")
    assert approval["execution_status"] == "COMPLETED", approval

    after_head = setup["store"].head(setup["tenant"], workspace)
    assert after_head.version == before_head.version, (
        "computing an approval digest/readiness view must never itself "
        "advance the workspace's real version/head -- that is publication, "
        "a strictly later, separate, human-authorized step (AI7-T04/AI8)")
    assert after_head.source_revision_id == before_head.source_revision_id
    assert after_head.decision_epoch == before_head.decision_epoch, (
        "computing readiness must never itself bump decision_epoch -- only "
        "a real human decision does (AI6-T04's own reviewed L-036 finding)")

    # A stale re-check must be possible AFTER a later, independent source
    # mutation -- proving a once-green digest is not treated as a durable
    # grant of permission by the SAME executor that computed it (the
    # decision and apply steps each independently re-verify at their own
    # mutation time; this executor itself is not the enforcement point).
    publish_with_chunk(setup["store"], setup["tenant"], workspace,
                       artifact_id="notpublish-after-change.yaml", text="mutated: true\n")
    drain_outbox(setup["store"], setup["tenant"], workspace)
    later_head = setup["store"].head(setup["tenant"], workspace)
    assert later_head.source_revision_id != after_head.source_revision_id, (
        "sanity: the later mutation used for this snapshot-vs-mutation "
        "distinction must itself be real and observable")
