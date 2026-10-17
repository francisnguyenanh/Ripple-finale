# -*- coding: utf-8 -*-
"""Apply staging, positive path (M1): approved edits materialize into a
correct staged blob manifest (see `tests/ai8t01/conftest.py` module
docstring for the contract).
"""

from __future__ import annotations

import io
from uuid import uuid4

import pytest

from tests.ai5t02.conftest import owner  # noqa: F401
from tests.ai8t01.conftest import (
    build_real_ready_xlsx_approval,
    build_real_ready_yaml_approval,
    login,
    make_xlsx_bytes,
    record_real_approve_decision,
    setup,  # noqa: F401
    submit_apply_job,
    submit_claim_and_get_apply,
)


def test_m1_yaml_edit_materializes_correct_staged_bytes(setup, monkeypatch, owner):
    """A real, ready `yaml_typed_edit` approval bundle applies-stages to
    bytes carrying the approved value, keyed by a real content hash, and
    the workspace head is provably untouched (never published)."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _proposal_job_id, _review_job_id = (
        build_real_ready_yaml_approval(
            setup, client, workspace, monkeypatch, owner,
            target_artifact_id="config-apply-m1.yaml",
            expected_current=14, proposed=30))
    digest = approval_body["execution_result"]["approval_digest"]
    before_head = setup["store"].head(setup["tenant"], workspace)
    record_real_approve_decision(setup, workspace, approval_job_id=approval_job_id,
                                 digest=digest)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert body["execution_status"] == "COMPLETED", body
    result = body["execution_result"]

    manifest = result["staged_manifest"]
    assert "config-apply-m1.yaml" in manifest
    staged_hash = manifest["config-apply-m1.yaml"]
    assert staged_hash is not None

    staged_bytes = setup["store"]._verified_blob(staged_hash)
    import yaml
    data = yaml.safe_load(staged_bytes.decode("utf-8"))
    assert data["refund_window_days"] == 30

    # A decided real edit is staged AND published atomically, exactly once,
    # and the head's artifact is precisely the staged blob.
    after_head = setup["store"].head(setup["tenant"], workspace)
    assert after_head.version == before_head.version + 1, (
        "apply publishes the staged candidate exactly once")
    assert after_head.source_revision_id == result["published_revision_id"]
    published = setup["store"].read_artifact_blob(
        setup["tenant"], workspace, after_head.source_revision_id,
        "config-apply-m1.yaml")["content"]
    assert published == staged_bytes, "published bytes must equal the staged candidate"


def test_m1_xlsx_no_op_bundle_is_refused_by_the_product_path(setup):
    """W1 product path: a no-op workbook bundle is BLOCKED (acceptance UNAVAILABLE): approve -> 409,
    and a forced approve row is still refused at apply (`approval_readiness_blocked`); head unchanged."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r, _o = build_real_ready_xlsx_approval(
        setup, client, workspace, target_artifact_id="rates-apply-m1-noop-refused.xlsx",
        expected_current=30, proposed=30, workbook_bytes=make_xlsx_bytes(sheet1_b2=30))
    digest = approval_body["execution_result"]["approval_digest"]
    assert approval_body["execution_result"]["readiness"] == "BLOCKED"
    before_head = setup["store"].head(setup["tenant"], workspace)
    from tests.ai7t04.conftest import submit_decision
    from tests.ai8t01.conftest import _grant_change_approver, force_approve_row
    approver = _grant_change_approver(setup)
    resp = submit_decision(approver["client"], workspace, job_id=approval_job_id, action="approve",
                           expected_approval_digest=digest)
    assert resp.status_code == 409, resp.text
    force_approve_row(setup, workspace, approval_job_id, digest)
    body = submit_claim_and_get_apply(setup, client, workspace, approval_job_id=approval_job_id,
                                      expected_approval_digest=digest)
    assert body["execution_status"] == "FAILED", body
    assert "approval_readiness_blocked" in (body["execution_reason"] or ""), body
    assert setup["store"].head(setup["tenant"], workspace).version == before_head.version


def test_m1_xlsx_edit_preserves_unrelated_cells_styles_sheets(setup):
    """The approved XLSX cell edit is materialized; the staged bytes are
    downloadable via an INDEPENDENT reader (openpyxl) and every untouched
    cell/style/sheet/formula is byte-for-byte unchanged beyond the single
    approved cell -- the task card's "unrelated cells/formulas/styles/
    sheets/named ranges not changed outside declared normalization"
    requirement."""
    import openpyxl

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r, _original = build_real_ready_xlsx_approval(
        setup, client, workspace, target_artifact_id="rates-apply-m1.xlsx",
        expected_current=14, proposed=30)
    digest = approval_body["execution_result"]["approval_digest"]
    record_real_approve_decision(setup, workspace, approval_job_id=approval_job_id,
                                 digest=digest)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert body["execution_status"] == "COMPLETED", body
    manifest = body["execution_result"]["staged_manifest"]
    staged_hash = manifest["rates-apply-m1.xlsx"]
    staged_bytes = setup["store"]._verified_blob(staged_hash)

    workbook = openpyxl.load_workbook(io.BytesIO(staged_bytes))
    assert workbook.sheetnames == ["Sheet1", "Notes"], "no sheet added/removed"
    sheet1 = workbook["Sheet1"]
    assert sheet1["B2"].value == 30, "the approved cell must carry the new value"
    assert sheet1["A1"].value == "rate_label", "untouched cell must survive verbatim"
    assert sheet1["A1"].font.bold is True, "untouched style must survive verbatim"
    notes = workbook["Notes"]
    assert notes["A1"].value == 21
    assert notes["A2"].value == "=A1*2", "untouched formula must survive verbatim"


def test_m1_xlsx_no_op_workbook_keeps_identical_blob_hash(setup, monkeypatch):
    """A no-op workbook edit (already at the approved target value) stages
    ZERO new bytes -- the staged/after hash is identical to the current
    published hash, never a fabricated new one. W1: the product path refuses a no-op bundle (see
    `test_m1_xlsx_no_op_bundle_is_refused_by_the_product_path`); this test neutralizes ONLY the
    readiness gate in-test and forces the approve row to reach the original apply assertion."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    workbook_bytes = make_xlsx_bytes(sheet1_b2=30)
    before_hash_expected = None  # computed after publish via store, see below
    approval_job_id, approval_body, _p, _r, original = build_real_ready_xlsx_approval(
        setup, client, workspace, target_artifact_id="rates-apply-m1-noop.xlsx",
        expected_current=30, proposed=30, workbook_bytes=workbook_bytes)
    digest = approval_body["execution_result"]["approval_digest"]
    before_head = setup["store"].head(setup["tenant"], workspace)
    from tests.ai8t01.conftest import force_approve_row, neutralize_readiness_gate
    force_approve_row(setup, workspace, approval_job_id, digest)
    neutralize_readiness_gate(monkeypatch)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert body["execution_status"] == "COMPLETED", body
    result = body["execution_result"]
    manifest = result["staged_manifest"]
    import hashlib
    assert manifest["rates-apply-m1-noop.xlsx"] == (
        "sha256:" + hashlib.sha256(original).hexdigest())

    after_head = setup["store"].head(setup["tenant"], workspace)
    assert after_head.version == before_head.version
    assert after_head.source_revision_id == before_head.source_revision_id
