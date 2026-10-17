# -*- coding: utf-8 -*-
"""Apply staging rejections (M2): wrong/stale hash-value, renamed/missing sheet,
corrupt input, parser failure, and out-of-scope/precondition-bypass attempts
must fail BEFORE any publication, with a named reason, never a fuzzy apply.

See `tests/ai8t01/conftest.py` module docstring for the apply contract.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.ai4t01.conftest import drain_outbox
from tests.ai5t02.conftest import owner  # noqa: F401
from tests.ai7t04.conftest import submit_decision
from tests.ai8t01.conftest import (
    _grant_change_approver,
    build_real_ready_xlsx_approval,
    build_real_ready_yaml_approval,
    login,
    publish_with_chunk,
    record_real_approve_decision,
    setup,  # noqa: F401
    submit_apply_job,
    submit_claim_and_get_apply,
)


def test_m2_wrong_expected_approval_digest_rejected_before_staging(setup, monkeypatch, owner):
    """A caller supplying a WRONG `expected_approval_digest` (forged or
    stale) must be rejected -- never silently applied against the real
    current bundle -- and the source head must be provably unchanged."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, _approval_body, _p, _r = build_real_ready_yaml_approval(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-apply-m2-digest.yaml",
        expected_current=14, proposed=30)
    before_head = setup["store"].head(setup["tenant"], workspace)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest="sha256:" + uuid4().hex)
    assert body["execution_status"] == "FAILED", body
    assert body.get("execution_reason"), "a durable, named reason is required"

    after_head = setup["store"].head(setup["tenant"], workspace)
    assert after_head.version == before_head.version
    assert after_head.source_revision_id == before_head.source_revision_id


def test_m2_source_changed_since_approval_is_stale_not_fuzzy_applied(setup, monkeypatch, owner):
    """The published source changes AFTER the proposal/approval bundle was
    built (e.g. another writer published a new revision of the same
    artifact). Apply-stage must detect the locator's `expected_current` no
    longer matches the FRESH current bytes and refuse -- never guess/
    fuzzy-apply against the drifted value."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r = build_real_ready_yaml_approval(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-apply-m2-stale.yaml",
        expected_current=14, proposed=30)
    digest = approval_body["execution_result"]["approval_digest"]
    # Record the REAL human approve decision BEFORE the source drifts, so
    # this test genuinely reaches the stale-content guard (the fresh-read
    # replay) rather than failing closed earlier on the decision-ledger
    # guard (`approval_not_decided`) -- those are two distinct guards.
    record_real_approve_decision(setup, workspace, approval_job_id=approval_job_id,
                                 digest=digest)

    # A second, independent writer republishes the SAME artifact with a
    # different current value after the proposal/review/approval chain was
    # built -- the real world moved out from under the approved patch.
    publish_with_chunk(setup["store"], setup["tenant"], workspace,
                       artifact_id="config-apply-m2-stale.yaml",
                       text="refund_window_days: 999\n")
    drain_outbox(setup["store"], setup["tenant"], workspace)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert body["execution_status"] == "FAILED", body
    assert "stale" in (body.get("execution_reason") or "").lower(), body


def test_m2_renamed_sheet_rejected_with_reason(setup):
    """The approved edit names `Sheet1`, but the CURRENT workbook (rewritten
    by an unrelated writer after approval) has renamed that sheet. Apply
    must fail closed with a specific reason -- never silently target a
    different sheet or fabricate the missing one."""
    import io
    import openpyxl

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r, _original = build_real_ready_xlsx_approval(
        setup, client, workspace, target_artifact_id="rates-apply-m2-renamed.xlsx",
        expected_current=14, proposed=30)
    digest = approval_body["execution_result"]["approval_digest"]
    # Record the real decision BEFORE the rename, so this test isolates the
    # renamed-sheet/fresh-read guard rather than being short-circuited by
    # the separate, earlier `approval_not_decided` decision-ledger guard.
    record_real_approve_decision(setup, workspace, approval_job_id=approval_job_id,
                                 digest=digest)

    workbook = openpyxl.Workbook()
    sheet1 = workbook.active
    sheet1.title = "RenamedSheet"
    sheet1["B2"] = 14
    buffer = io.BytesIO()
    workbook.save(buffer)
    revision_id = f"rev-{uuid4().hex}"
    digest_blob = setup["store"].stage_blob(buffer.getvalue())
    setup["store"].publish_source(
        setup["tenant"], workspace, revision_id,
        {"rates-apply-m2-renamed.xlsx": digest_blob},
        expected_version=setup["store"].head(setup["tenant"], workspace).version,
        operation="source.import.commit", idempotency_key=uuid4().hex)
    drain_outbox(setup["store"], setup["tenant"], workspace)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert body["execution_status"] == "FAILED", body
    assert body.get("execution_reason"), "a durable, named reason is required"


def test_m2_corrupt_workbook_parser_failure_rejected(setup):
    """The currently-published bytes for the target artifact are not a
    readable XLSX workbook at all (corrupted after approval). Apply must
    fail with a named parser-failure reason, never crash uncaught and never
    silently stage the corrupt bytes as-is."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r, _original = build_real_ready_xlsx_approval(
        setup, client, workspace, target_artifact_id="rates-apply-m2-corrupt.xlsx",
        expected_current=14, proposed=30)
    digest = approval_body["execution_result"]["approval_digest"]
    # Record the real decision BEFORE corrupting the bytes, so this test
    # isolates the parser-failure/fresh-read guard, not the separate,
    # earlier `approval_not_decided` decision-ledger guard.
    record_real_approve_decision(setup, workspace, approval_job_id=approval_job_id,
                                 digest=digest)

    revision_id = f"rev-{uuid4().hex}"
    corrupt_bytes = b"this is not a real xlsx workbook, just garbage bytes"
    digest_blob = setup["store"].stage_blob(corrupt_bytes)
    setup["store"].publish_source(
        setup["tenant"], workspace, revision_id,
        {"rates-apply-m2-corrupt.xlsx": digest_blob},
        expected_version=setup["store"].head(setup["tenant"], workspace).version,
        operation="source.import.commit", idempotency_key=uuid4().hex)
    drain_outbox(setup["store"], setup["tenant"], workspace)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert body["execution_status"] == "FAILED", body
    assert body.get("execution_reason"), "a durable, named reason is required"


def test_m2_apply_succeeds_only_after_a_real_recorded_approve_decision(
        setup, monkeypatch, owner):
    """Approval/decision-ledger bypass guard (`approval_not_decided`,
    `infra/worker/apply.py`). Attack reproduced: ONE
    account (`setup['requester']`) runs the entire proposal->review->
    approval chain, reads back the real `approval_digest`, and submits an
    apply job referencing it -- WITHOUT ever calling
    `POST /v2/jobs/{approval_job_id}/decision`. This must fail closed
    with `approval_not_decided`, never silently materialize. Only AFTER a
    real, distinct human actor records a genuine `approve` decision via the
    real decision route does a fresh apply job against the SAME approval
    succeed."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r = build_real_ready_yaml_approval(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-apply-m2-nodecision.yaml",
        expected_current=14, proposed=30)
    digest = approval_body["execution_result"]["approval_digest"]
    before_head = setup["store"].head(setup["tenant"], workspace)

    # Attack reproduction: no decision route ever called. A single account
    # holding `source.import` drove proposal/review/approval end-to-end and
    # now attempts to self-materialize the change.
    attack_body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert attack_body["execution_status"] == "FAILED", attack_body
    assert (attack_body.get("execution_reason") or "").startswith("approval_not_decided"), attack_body
    after_attack_head = setup["store"].head(setup["tenant"], workspace)
    assert after_attack_head.version == before_head.version, (
        "a self-approve+apply attempt with no recorded human decision must "
        "never stage/publish anything")

    # Now a REAL, distinct human actor records the approve decision via the
    # real decision route.
    record_real_approve_decision(setup, workspace, approval_job_id=approval_job_id,
                                 digest=digest)

    # A fresh apply job against the SAME approval/digest now succeeds.
    success_body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert success_body["execution_status"] == "COMPLETED", success_body
    manifest = success_body["execution_result"]["staged_manifest"]
    assert "config-apply-m2-nodecision.yaml" in manifest


def test_m2_matching_digest_but_no_approve_decision_is_rejected(setup, monkeypatch, owner):
    """Matching digest alone is never sufficient -- a `COMPLETED` approval
    job with the CORRECT digest but (a) no recorded decision at all, or (b)
    a recorded `reject`/`request_changes` decision instead of `approve`,
    must be rejected with `approval_not_decided` before any staging. Covers
    the negative, including the
    `request_changes` case: per the decision contract, `request_changes`
    spawns an entirely NEW proposal job id and never records an `approve`
    row against THIS approval_job_id, so "matching digest, no approve
    decision on THIS approval_job_id" is exactly what a `request_changes`
    history looks like too."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]

    # (a) No decision recorded at all.
    approval_job_id, approval_body, _p, _r = build_real_ready_yaml_approval(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-apply-m2-absent.yaml",
        expected_current=14, proposed=30)
    digest = approval_body["execution_result"]["approval_digest"]
    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert body["execution_status"] == "FAILED", body
    assert (body.get("execution_reason") or "").startswith("approval_not_decided"), body

    # (b) A recorded `reject` decision (matching digest) -- still refused.
    approval_job_id2, approval_body2, _p2, _r2 = build_real_ready_yaml_approval(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-apply-m2-reject.yaml",
        expected_current=14, proposed=30)
    digest2 = approval_body2["execution_result"]["approval_digest"]
    approver = _grant_change_approver(setup)
    resp = submit_decision(approver["client"], workspace, job_id=approval_job_id2,
                          action="reject", reason="rejecting for this negative test",
                          expected_approval_digest=digest2)
    assert resp.status_code == 200, resp.text
    body2 = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id2,
        expected_approval_digest=digest2)
    assert body2["execution_status"] == "FAILED", body2
    assert (body2.get("execution_reason") or "").startswith("approval_not_decided"), body2

    # (c) A recorded `request_changes` decision (matching digest) -- still
    # refused; `request_changes` spawns a NEW proposal job id and records no
    # `approve` row on THIS approval_job_id.
    approval_job_id3, approval_body3, _p3, _r3 = build_real_ready_yaml_approval(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-apply-m2-requestchanges.yaml",
        expected_current=14, proposed=30)
    digest3 = approval_body3["execution_result"]["approval_digest"]
    approver3 = _grant_change_approver(setup)
    resp3 = submit_decision(approver3["client"], workspace, job_id=approval_job_id3,
                           action="request_changes", reason="please revise this",
                           expected_approval_digest=digest3)
    assert resp3.status_code == 200, resp3.text
    # `request_changes` spawns a NEW proposal job (`new_proposal_job_id`),
    # which enqueues its own outbox event; drain it first so the apply
    # job's own claim isn't intercepted by that unrelated event (the same
    # un-drained-outbox hazard).
    drain_outbox(setup["store"], setup["tenant"], workspace)
    body3 = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id3,
        expected_approval_digest=digest3)
    assert body3["execution_status"] == "FAILED", body3
    assert (body3.get("execution_reason") or "").startswith("approval_not_decided"), body3


def test_m2_out_of_scope_approval_job_id_rejected(setup):
    """A missing/nonexistent `approval_job_id` (an attempt to bypass the
    approval precondition entirely, e.g. by pointing straight at a
    `proposal`/`review` job or a random string) must be a hard,
    non-retryable failure, exactly the same guard-precedent
    `infra/worker/approval.py::_resolve_job` already applies to a bad
    `proposal_job_id`/`review_job_id` -- never a fabricated readiness."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    # A job's generic admission (`_create_job_core`) requires a non-None
    # `head.source_revision_id` for ANY kind -- publish once first so this
    # test exercises the apply-specific out-of-scope-approval-job-id guard,
    # not the separate, already-known FOLLOW-UP-W2 StaleVersion/500 gap for
    # a workspace with no published revision at all.
    publish_with_chunk(setup["store"], setup["tenant"], workspace,
                       artifact_id="notes-apply-m2-scope.md", text="unrelated\n")
    drain_outbox(setup["store"], setup["tenant"], workspace)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=f"job-nonexistent-{uuid4().hex}",
        expected_approval_digest="sha256:" + uuid4().hex)
    assert body["execution_status"] == "FAILED", body
    assert body.get("execution_reason"), "a durable, named reason is required"


def test_m2_blocked_approval_is_never_applied(setup, monkeypatch, owner):
    """An approval bundle whose OWN `readiness` is `"BLOCKED"` (e.g. built
    over a `manual`-classification proposal) must never be materialized --
    precondition bypass attempts (submitting an apply job against a
    BLOCKED approval anyway) must be caught by a REAL guard, not merely
    the test's own assumption that it would be blocked."""
    from tests.ai7t01.conftest import submit_job, get_job_status
    from infra.worker import job_worker
    from tests.ai7t03.conftest import submit_approval_job

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    tenant, store = setup["tenant"], setup["store"]

    # A proposal with an unresolvable intent classifies "manual" -> BLOCKED
    # readiness on any approval built over it (the approval's
    # `classification == "manual"` blocker).
    publish_with_chunk(store, tenant, workspace, artifact_id="notes-apply-m2-blocked.md",
                       text="Some unrelated policy text.\n")
    drain_outbox(store, tenant, workspace)
    from tests.ai7t01.conftest import create_real_impact_job, create_real_simulation_job
    impact_job_id = create_real_impact_job(setup, client, workspace)
    simulation_job_id = create_real_simulation_job(setup, client, workspace)
    proposal_id = f"job-proposal-{uuid4().hex}"
    resp = submit_job(client, workspace, kind="proposal", job_id=proposal_id, payload={
        "intent_id": f"intent-nonexistent-{uuid4().hex}",
        "impact_job_id": impact_job_id, "simulation_job_id": simulation_job_id,
        "target_artifact_id": "notes-apply-m2-blocked.md", "edit_kind": "markdown"})
    assert resp.status_code == 200, resp.text
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "m2-proposal-worker")
    assert claim is not None
    proposal_body = get_job_status(client, workspace, proposal_id)
    assert proposal_body["execution_status"] == "COMPLETED", proposal_body
    assert proposal_body["execution_result"]["classification"] == "manual"

    from tests.ai7t02.conftest import submit_claim_and_get_review
    review_body = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_id,
        worker_id="m2-review-worker")
    review_id = review_body.get("job_id") or review_body.get("id")

    approval_id = f"job-approval-{uuid4().hex}"
    resp = submit_approval_job(client, workspace, proposal_job_id=proposal_id,
                               review_job_id=review_id, job_id=approval_id)
    assert resp.status_code == 200, resp.text
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "m2-approval-worker")
    assert claim is not None
    approval_body = get_job_status(client, workspace, approval_id)
    assert approval_body["execution_status"] == "COMPLETED", approval_body
    assert approval_body["execution_result"]["readiness"] == "BLOCKED", approval_body
    blocked_digest = approval_body["execution_result"]["approval_digest"]
    # Record a REAL human approve decision anyway (the real decide_job route
    # does not gate on readiness -- confirmed by direct read -- so a human
    # CAN approve a BLOCKED bundle; this test's own guard is the SEPARATE
    # `unsupported_edit_kind` structural rejection for `edit_kind="markdown"`,
    # not the decision-ledger guard). Recording the decision here isolates
    # exactly that guard instead of being short-circuited by
    # `approval_not_decided`.
    record_real_approve_decision(setup, workspace, approval_job_id=approval_id,
                                 digest=blocked_digest)

    from tests.ai8t02.conftest import publish_footprint
    head_before = store.head(tenant, workspace)
    footprint_before = publish_footprint(store, tenant, workspace)
    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_id,
        expected_approval_digest=blocked_digest)
    # External manual actions are never turned into source edits: the
    # apply gate COMPLETES with pending human work. The invariant this test
    # protects: the blocked/manual approval is never materialized (no head
    # move, no publish rows, nothing published). `unsupported_edit_kind` is
    # asserted for the non-manual shapes in
    # tests/ai8t02/test_publish_f5_manual_work.py.
    assert body["execution_status"] == "COMPLETED", body
    result = body["execution_result"]
    assert result["published"] is False and result["published_revision_id"] is None, result
    assert result["pending_human_work"], result
    assert store.head(tenant, workspace) == head_before, "manual work moved the head"
    assert publish_footprint(store, tenant, workspace) == footprint_before
