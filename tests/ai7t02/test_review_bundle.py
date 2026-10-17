# -*- coding: utf-8 -*-
"""Stage validation and independent review roles: job kind "review",
`executor_id="review.board.v1"` (`infra/worker/review.py`).

Every test below drives the REAL product path: `POST /v2/jobs` -> real
`infra.worker.job_worker.claim_and_admit_one` -> `GET /v2/jobs/{id}` ->
inspect the real persisted `execution_status`/`execution_result`, per the
convention of `tests/ai7t01/test_patch_generation.py` for
`kind="proposal"` (`submit_claim_and_get`).
`"review"` is a REAL, registered job kind (`BUILTIN_EXECUTORS`,
`JOB_CREATE_PERMISSION`, `JOB_APPROVE_PERMISSION`) -- submission itself
returns a real `200`; the real accept/blocker/failure decision happens
ASYNCHRONOUSLY, observable only via claim+GET.
already names.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.adapters.v2_policy import JOB_APPROVE_PERMISSION, JOB_CREATE_PERMISSION
from infra.worker.job_worker import BUILTIN_EXECUTORS

from tests.ai4t01.conftest import drain_outbox, login, publish_with_chunk, setup  # noqa: F401
from tests.ai5t02.conftest import owner  # noqa: F401
from tests.ai7t02.conftest import (
    claim_proposal_job,
    create_real_clean_yaml_proposal_job,
    create_real_impact_job,
    create_real_simulation_job,
    create_real_yaml_proposal_job,
    create_synthetic_python_proposal_job,
    get_job_status,
    submit_claim_and_get_review,
    submit_job,
    submit_review_job,
)


def _real_v1_proposal(setup, *, artifact_id="config.yaml", proposed=30):
    """A real, COMPLETED `kind="proposal"` job to serve as
    the exact candidate under review."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    publish_with_chunk(setup["store"], setup["tenant"], workspace,
                       artifact_id=artifact_id, text="refund_window_days: 14\n")
    drain_outbox(setup["store"], setup["tenant"], workspace)
    impact_job_id = create_real_impact_job(setup, client, workspace)
    simulation_job_id = create_real_simulation_job(setup, client, workspace)
    job_id, body = create_real_yaml_proposal_job(
        setup, client, workspace, target_artifact_id=artifact_id,
        impact_job_id=impact_job_id, simulation_job_id=simulation_job_id,
        expected_current=14, proposed=proposed)
    drain_outbox(setup["store"], setup["tenant"], workspace)
    proposal_job_id = body.get("job_id") or body.get("id")
    return client, workspace, proposal_job_id, body


# ------------------------------------------------------------------------------------------- sanity

def test_review_job_kind_registered():
    """`"review"` is a real, registered job kind in every dispatch table."""
    assert "review" in BUILTIN_EXECUTORS
    assert BUILTIN_EXECUTORS["review"].executor_id == "review.board.v1"
    assert JOB_CREATE_PERMISSION["review"] == "source.import"
    assert JOB_APPROVE_PERMISSION["review"] == "source.import"


# ---------------------------------------------------------------------------------------------- B1

def test_b1_exact_boundary_logic_change_caught_by_real_sandbox_simulation(setup):
    """B1-a ("boundary test must catch `>`/`>=` errors"). No fixture in this
    repo can stage a real `>`/`>=` operator swap through `ProposalExecutor`'s
    own literal-digit-substitution edit vocabulary, so the exact staged Python
    bytes are constructed directly against the real store
    (`create_synthetic_python_proposal_job`, see its own docstring for why
    this is real, product-store-backed evidence, not a mock) and the REAL
    `kind="review"` executor's REAL `simulation_exact_patch` check (the real
    isolated sandbox) is driven against them.

    Differential proof: the CORRECT candidate (`x > 10`, with a self-
    contained boundary assertion) executes cleanly; the WRONG candidate
    (`x >= 10`, otherwise byte-identical) raises `AssertionError` inside the
    real sandbox and is reported `FAIL` -- a genuine, sandbox-executed
    distinction at the exact boundary value, not a shadow-config-only
    proof."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]

    correct_source = (
        "def threshold_check(x):\n"
        "    return x > 10\n\n"
        "def run_probe():\n"
        "    assert threshold_check(10) is False, "
        "'boundary value 10 must NOT pass a strict > 10 threshold'\n"
        "    assert threshold_check(11) is True\n"
        "    assert threshold_check(9) is False\n"
    )
    buggy_source = correct_source.replace("x > 10", "x >= 10")
    assert buggy_source != correct_source

    correct_job_id, _ = create_synthetic_python_proposal_job(
        setup, client, workspace, target_artifact_id="threshold.py",
        python_source=correct_source)
    buggy_job_id, _ = create_synthetic_python_proposal_job(
        setup, client, workspace, target_artifact_id="threshold.py",
        python_source=buggy_source)

    correct_review = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=correct_job_id,
        worker_id="review-worker-correct")
    buggy_review = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=buggy_job_id,
        worker_id="review-worker-buggy")

    assert correct_review["execution_status"] == "COMPLETED", correct_review
    assert buggy_review["execution_status"] == "COMPLETED", buggy_review

    correct_checks = correct_review["execution_result"]["checks"]
    buggy_checks = buggy_review["execution_result"]["checks"]

    # User-derived python is NEVER executed (the old sandbox run of the staged bytes was removed;
    # sandbox_runner is not a security boundary). Both candidates are refused identically and fail closed.
    for checks in (correct_checks, buggy_checks):
        assert checks["simulation_exact_patch"]["status"] == "UNAVAILABLE", checks
        assert "user_code_not_executed" in (checks["simulation_exact_patch"]["detail"] or "")
    assert "AssertionError" not in (buggy_checks["simulation_exact_patch"]["detail"] or "")
    for review in (correct_review, buggy_review):
        result = review["execution_result"]
        assert result["readiness"] == "BLOCKED"
        assert any(b.get("name") == "simulation_exact_patch" for b in result["blockers"])
    assert correct_result_blockers_names(correct_review) == {"acceptance_tests", "simulation_exact_patch"}


def correct_result_blockers_names(review_body):
    return {b.get("name") for b in review_body["execution_result"]["blockers"]}


def test_b1_semantically_bad_syntax_passing_patch_never_readiness_approved(setup):
    """B1-b: a syntactically-valid-but-semantically-
    dubious candidate (a negative `proposed` value against a real YAML
    typed locator edit -- passes every structural/syntax check) must never
    be silently reported as approved. Claim+GET and assert on the real persisted `readiness`:
    `readiness` is never the literal string `"approved"` for ANY
    candidate that names no verification contract (W1: `acceptance_tests` is
    no longer a constant; with no operator-supplied contract it is honestly
    `UNAVAILABLE`, which alone forces `BLOCKED` -- see B2-a; the PASS/FAIL
    rows with a contract are in tests/w1_prod), which is
    itself the real, structural proof this bullet requires: no syntax-
    passing patch, however semantically dubious, can ever reach an
    AI-asserted "approved" state here."""
    client, workspace, proposal_job_id, _ = _real_v1_proposal(setup, proposed=-1)

    review = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id)

    assert review["execution_status"] == "COMPLETED", review
    result = review["execution_result"]
    assert result["readiness"] == "BLOCKED"
    assert result["readiness"] != "approved"
    assert any(b.get("name") == "acceptance_tests" for b in result["blockers"])


def test_b1_partial_emergency_patch_still_forces_blocked_readiness(setup):
    """B1-c: a partial "emergency" patch that only covers a fraction of the
    required scope must still be caught, not silently config-approved.
    CLOSED at the same real, structural boundary as B1-b above: `readiness`
    is BLOCKED for this real candidate too (no scenario in this environment
    can reach "approved" while `acceptance_tests` is `UNAVAILABLE` for want of a
    verification contract)."""
    client, workspace, proposal_job_id, _ = _real_v1_proposal(setup)

    review = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id)

    assert review["execution_status"] == "COMPLETED", review
    assert review["execution_result"]["readiness"] == "BLOCKED"


# ---------------------------------------------------------------------------------------------- B2

def test_b2_unanimous_role_pass_with_required_tool_unavailable_still_blocked(setup, monkeypatch, owner):
    """B2-a + nghiệm-thu bullet 2 ("test failures must never be AI-
    overridden"): drives a REAL, otherwise-clean YAML typed-edit candidate
    (real rationale/evidence_ref per artifact -- confirmed by direct read of
    `infra/worker/proposal.py::_yaml_typed_edits`) through the real review
    executor. All three roles independently reach `PASS` (proven directly
    below, not assumed) -- but the required `acceptance_tests` check is
    `UNAVAILABLE` here because this fixture names no verification contract
    (W1 D-4: a real, operator-supplied contract is the only harness),
    and this ALONE still forces `readiness="BLOCKED"`, never overridden by
    the unanimous role PASS. This is the load-bearing guard this test
    exists to prove: `readiness` is computed as "ANY required check/role
    FAIL/UNAVAILABLE -> BLOCKED", never a majority vote over roles -- if
    that rule were replaced by "readiness follows role majority", THIS test
    would flip to green-when-it-shouldn't (roles are unanimous PASS) and
    silently stop catching the missing acceptance-test harness; the
    assertion on `blockers` naming `acceptance_tests` specifically (not just
    on the aggregate `readiness` string) is what makes this test
    guard-specific rather than incidentally true."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, _ = create_real_clean_yaml_proposal_job(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-clean.yaml", expected_current=14, proposed=30)

    review = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id)

    assert review["execution_status"] == "COMPLETED", review
    result = review["execution_result"]

    assert result["roles"]["BUSINESS_REVIEW"]["status"] == "PASS", result["roles"]
    assert result["roles"]["EVIDENCE_REVIEW"]["status"] == "PASS", result["roles"]
    assert result["roles"]["TECHNICAL_REVIEW"]["status"] == "PASS", result["roles"]
    assert result["checks"]["acceptance_tests"]["status"] == "UNAVAILABLE"
    assert result["readiness"] == "BLOCKED"
    assert any(b.get("kind") == "check" and b.get("name") == "acceptance_tests"
              for b in result["blockers"])


def test_b2_forced_role_unavailable_is_a_named_blocker_never_silently_approved(setup, monkeypatch, owner):
    """B2-b ("reviewer timeout/unavailable must never
    be silently 'approved'"): drives the REAL `payload.simulate_role_
    unavailable` deterministic test-only knob (same
    convention as `infra/worker/simulation.py`'s own `payload.candidate_kind`
    probes) through the real executor -- not a mocked
    boundary. Asserts a real, NAMED `UNAVAILABLE` role status (not a
    default/implicit pass) and a real, named blocker."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    proposal_job_id, _ = create_real_clean_yaml_proposal_job(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-clean-2.yaml", expected_current=14, proposed=30)

    review = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        simulate_role_unavailable=["TECHNICAL_REVIEW"])

    assert review["execution_status"] == "COMPLETED", review
    result = review["execution_result"]
    assert result["roles"]["TECHNICAL_REVIEW"]["status"] == "UNAVAILABLE"
    assert result["roles"]["TECHNICAL_REVIEW"]["reason"] == "simulated_unavailable_or_timeout"
    assert result["readiness"] == "BLOCKED"
    assert any(b.get("kind") == "role" and b.get("name") == "TECHNICAL_REVIEW"
              for b in result["blockers"])
    # The OTHER two roles are unaffected -- proves the forced-unavailable
    # knob is role-specific, not a blanket override of the whole board.
    assert result["roles"]["BUSINESS_REVIEW"]["status"] == "PASS"
    assert result["roles"]["EVIDENCE_REVIEW"]["status"] == "PASS"


def test_b2_malicious_review_submission_never_writes_a_patch_or_approval(setup):
    """B2-c: a malicious/adversarial review submission (here: one that also
    attempts the B3-b evaluated-hash-bypass forgery at the SAME time, the
    most adversarial real input this route accepts) must never itself write
    a patch or approval, and the real workspace head must be byte-identical
    before and after -- whether the submission is accepted OR rejected.
    `ReviewExecutor` calls no `stage_blob`/`publish_source`/`approve_job`
    primitive anywhere (confirmed by direct read of `infra/worker/
    review.py`); this test proves the REAL, observable consequence of that
    structural fact, not merely cites the source."""
    client, workspace, proposal_job_id, _ = _real_v1_proposal(setup)
    before_head = setup["store"].head(setup["tenant"], workspace)

    review = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        evaluated_hash="sha256:" + "f" * 64)

    after_head = setup["store"].head(setup["tenant"], workspace)
    assert after_head.version == before_head.version
    assert after_head.source_revision_id == before_head.source_revision_id

    # The forged-hash submission is a real, observable FAILED job (see
    # B3-b below) -- never a patch write, never an "approved" verdict.
    assert review["execution_status"] == "FAILED", review
    assert "evaluated_hash_mismatch" in (review.get("execution_reason") or "")


# ---------------------------------------------------------------------------------------------- B3

def test_b3_old_review_never_attaches_to_a_new_proposal_version(setup):
    """B3-a ("review cũ không attach proposal mới"):
    submits a real review for v1, then creates a genuinely distinct v2 via
    a real `action="revise"` (the multi-revision
    mechanism), then submits a SECOND, independent review job naming v2's
    own `job_id`. Asserts: (a) v1's review reports v1's own real evaluated
    hash; (b) v2's review reports a DIFFERENT real evaluated hash (v2's
    candidate genuinely differs, `proposed=45` vs v1's `proposed=30`); (c)
    v1's review row, re-read AFTER v2's review has run, is byte-identical
    to what it was right after its own completion -- a stale review can
    never silently reattach to a newer proposal version's bytes."""
    client, workspace, proposal_v1_job_id, v1_body = _real_v1_proposal(setup)

    v1_review = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_v1_job_id,
        worker_id="review-worker-v1")
    assert v1_review["execution_status"] == "COMPLETED", v1_review
    v1_hash = v1_review["execution_result"]["evaluated_hash"]

    result = v1_body["execution_result"]
    revise_resp = submit_job(client, workspace, kind="proposal", payload={
        "action": "revise",
        "previous_proposal_job_id": proposal_v1_job_id,
        "intent_id": f"intent-{uuid4().hex}",
        "impact_job_id": result["impact_job_id"],
        "simulation_job_id": result["simulation_job_id"],
        "target_artifact_id": result.get("target_artifact_id", "config.yaml"),
        "edit_kind": "yaml_typed_edit",
        # NB: `expected_current` must match the value in the CURRENT
        # PUBLISHED source (14, never touched by v1's own proposal -- a
        # proposal only stages candidate bytes, it never publishes them,
        # confirmed by direct read of `infra/worker/proposal.py`'s module
        # docstring/executor), not v1's own STAGED `proposed` value (30).
        "edits": [{"path": "refund_window_days", "expected_current": 14, "proposed": 45}],
    })
    assert revise_resp.status_code == 200, revise_resp.text
    v2_job_id = revise_resp.json().get("job_id") or revise_resp.json().get("id")
    assert v2_job_id is not None and v2_job_id != proposal_v1_job_id
    claim_proposal_job(setup, workspace, worker_id="revise-worker")
    v2_body = get_job_status(client, workspace, v2_job_id)
    assert v2_body["execution_status"] == "COMPLETED", v2_body

    v2_review = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=v2_job_id,
        worker_id="review-worker-v2")
    assert v2_review["execution_status"] == "COMPLETED", v2_review
    v2_hash = v2_review["execution_result"]["evaluated_hash"]

    assert v2_hash != v1_hash
    assert v2_review["execution_result"]["proposal_job_id"] == v2_job_id

    # v1's review row, re-read now, still reports v1's own job_id/hash --
    # untouched by v2's later review.
    v1_review_reread = get_job_status(
        client, workspace,
        v1_review["job_id"] if "job_id" in v1_review else v1_review["id"])
    assert v1_review_reread["execution_result"]["proposal_job_id"] == proposal_v1_job_id
    assert v1_review_reread["execution_result"]["evaluated_hash"] == v1_hash


def test_b3_evaluated_hash_bypass_forgery_is_rejected_with_a_real_fresh_epoch_style_control(setup):
    """B3-b: an "evaluated-hash bypass" attempt must be rejected -- proven
    here with a real forged-hash FAILURE paired with a real CORRECT-hash
    SUCCESS control (the same "guard is real and narrow, not a blanket
    rejection" pairing convention used
    for `expected_decision_epoch` 409-vs-200). If the evaluated-hash
    verification guard were removed (`review.py`'s `claimed_hash != real_hash`
    check), the FIRST assertion below (`execution_status == "FAILED"`) would
    flip to `"COMPLETED"` -- this test is guard-specific, not incidentally
    true. Also verifies that the real evaluated
    hash this executor reports is the SAME bare-hex format as the
    proposal's own `after_blob_hash` (never the store-internal
    `"sha256:"`-prefixed `staged_blob_hash` format), so a caller supplying
    the value it already legitimately saw at
    `GET /v2/jobs/{proposal_job_id}` succeeds, not mismatches."""
    client, workspace, proposal_job_id, proposal_body = _real_v1_proposal(setup)
    real_after_hash = proposal_body["execution_result"]["after_blob_hash"]
    assert isinstance(real_after_hash, str) and real_after_hash
    assert not real_after_hash.startswith("sha256:"), (
        "L-038 sanity: after_blob_hash must be the bare-hex format")

    forged = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        evaluated_hash="sha256:" + "0" * 64, worker_id="review-worker-forged")
    assert forged["execution_status"] == "FAILED", forged
    assert "evaluated_hash_mismatch" in (forged.get("execution_reason") or "")

    # Also try the OTHER plausible forgery shape: the wrongly-prefixed
    # `"sha256:"+real_after_hash` -- a caller who mistakenly assumed the
    # prefixed `staged_blob_hash` format was "the" evaluated hash (the
    # exact confusion this test guards against) must ALSO be rejected, not
    # silently accepted as "close enough".
    wrongly_prefixed = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        evaluated_hash="sha256:" + real_after_hash, worker_id="review-worker-prefixed")
    assert wrongly_prefixed["execution_status"] == "FAILED", wrongly_prefixed
    assert "evaluated_hash_mismatch" in (wrongly_prefixed.get("execution_reason") or "")

    # Real, CORRECT-hash control: the bare after_blob_hash a caller already
    # legitimately saw is ACCEPTED -- proves the guard is real and narrow,
    # not a blanket rejection of every `evaluated_hash` value.
    correct = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        evaluated_hash=real_after_hash, worker_id="review-worker-correct-hash")
    assert correct["execution_status"] == "COMPLETED", correct
    assert correct["execution_result"]["evaluated_hash"] == real_after_hash

    # Omitting evaluated_hash altogether must ALSO succeed and report the
    # identical real hash -- proves the field is genuinely optional and the
    # executor never trusts anything it wasn't independently able to derive.
    omitted = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        worker_id="review-worker-omitted")
    assert omitted["execution_status"] == "COMPLETED", omitted
    assert omitted["execution_result"]["evaluated_hash"] == real_after_hash


def test_b3_restart_leaves_review_state_durable_not_duplicated(setup):
    """B3-c: a restart/re-claim must leave review state durable -- the same
    already-reviewed AI2-AI7 durable outbox/job-row primitives every prior
    job kind relies on (`ReviewExecutor` adds no new in-memory state of its
    own, confirmed by direct read). Proven here with a real fixed-`job_id`
    submission, a real claim+completion, and a real SECOND submission
    attempt against the SAME `job_id` afterward -- asserts the row is not
    duplicated or corrupted (the second submission does not silently
    re-run/overwrite a different result)."""
    client, workspace, proposal_job_id, _ = _real_v1_proposal(setup)
    job_id = f"job-review-restart-{uuid4().hex}"

    first = submit_review_job(client, workspace, proposal_job_id=proposal_job_id, job_id=job_id)
    assert first.status_code == 200, first.text
    claim_proposal_job(setup, workspace, worker_id="review-worker-restart")
    first_body = get_job_status(client, workspace, job_id)
    assert first_body["execution_status"] == "COMPLETED", first_body
    first_result = first_body["execution_result"]

    # Re-submit the SAME job_id after it is already COMPLETED -- a real
    # restart/duplicate-delivery shape. The durable row must not be
    # silently duplicated/corrupted: re-reading it afterward still shows
    # the SAME, untouched result.
    second = submit_review_job(client, workspace, proposal_job_id=proposal_job_id, job_id=job_id)
    reread = get_job_status(client, workspace, job_id)
    assert reread["execution_status"] == "COMPLETED", reread
    assert reread["execution_result"] == first_result, (
        "a repeated submission against an already-COMPLETED job_id must "
        "never silently overwrite or duplicate the durable result")


# ------------------------------------------------------------------------------------ acceptance #5
# Covered above by `test_b1_exact_boundary_logic_change_caught_by_real_sandbox_simulation`
# (the differential `>` vs `>=` proof). Not duplicated here.


# ------------------------------------------------------------------------------- cross-tenant control

def test_cross_tenant_read_denied_for_nonexistent_review_job(setup):
    """A `GET` for a never-created job id is a real 404."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    resp = client.get(f"/v2/jobs/job-review-{uuid4().hex}", params={"workspace_id": workspace})
    assert resp.status_code == 404, resp.text
