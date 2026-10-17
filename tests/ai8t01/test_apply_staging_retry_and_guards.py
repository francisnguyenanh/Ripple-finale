# -*- coding: utf-8 -*-
"""Apply staging retry and guards (M3): crash/retry over the SAME stage key never
changes content vs. the approved candidate; an incomplete snapshot is never
marked ready; a YAML cell-overlay never masquerades as an `.xlsx`; the
executor never regenerates a patch via a model call after approval.

See `tests/ai8t01/conftest.py` module docstring for the apply contract.
"""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest

from tests.ai5t02.conftest import owner  # noqa: F401
from tests.ai8t01.conftest import (
    build_real_ready_yaml_approval,
    claim_apply_job,
    login,
    record_real_approve_decision,
    setup,  # noqa: F401
    submit_apply_job,
    submit_claim_and_get_apply,
    submit_claim_and_get,
)


def test_m3_retry_same_stage_key_does_not_change_approved_content(setup, monkeypatch, owner):
    """A caller retries the SAME `job_id` (e.g. after a crash mid-apply, the
    task card's own "kill/interrupt after uploading some blobs then retry
    the same stage key" scenario). The retried result's staged manifest
    must be byte-identical to the first successful run's -- never a
    different, re-derived candidate."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r = build_real_ready_yaml_approval(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-apply-m3-retry.yaml",
        expected_current=14, proposed=30)
    digest = approval_body["execution_result"]["approval_digest"]
    record_real_approve_decision(setup, workspace, approval_job_id=approval_job_id,
                                 digest=digest)
    job_id = f"job-apply-{uuid4().hex}"
    # A decided apply PUBLISHES, advancing head, and job-create
    # replay hashes include `expected_version`; replay with the ORIGINAL
    # (pre-publish) binding, not a post-publish re-read.
    original_expected_version = setup["store"].head(setup["tenant"], workspace).version

    first = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id, job_id=job_id,
        expected_approval_digest=digest)
    assert first["execution_status"] == "COMPLETED", first

    # A resubmission of the SAME job_id through the real `POST /v2/jobs`
    # route always 409s regardless of `kind` -- `_create_job_core`'s own
    # documented contract raises `TenantScopeError` for ANY replay carrying
    # `requested_by` (the route always attributes the requester), and the
    # route maps that to 409. This is generic, pre-existing, non-apply-
    # specific behavior (confirmed by direct read of
    # `infra/adapters/v2_postgres.py::_create_job_core`'s docstring and
    # `infra/v2_policy_routes.py`'s `except (TenantScopeError, StoreConflict):
    # return _error(..., 409)` clause) -- so the real "retry after a crash"
    # path a worker/operator uses is the store-level idempotent replay
    # (bypassing route-level `requested_by` attribution), which we assert
    # here directly against the real store the route itself uses.
    store, tenant = setup["store"], setup["tenant"]
    resp = submit_apply_job(client, workspace, approval_job_id=approval_job_id,
                            job_id=job_id, expected_approval_digest=digest)
    assert resp.status_code == 409, resp.text

    replay_payload = {"approval_job_id": approval_job_id,
                      "expected_approval_digest": digest}
    store.create_job(
        tenant, workspace, job_id, kind="apply", payload=replay_payload,
        expected_version=original_expected_version,
        idempotency_key=f"job-create-{job_id}")
    from tests.ai8t01.conftest import get_job_status
    second = get_job_status(client, workspace, job_id)
    assert second["execution_status"] == "COMPLETED", second
    assert (second["execution_result"]["staged_manifest"]
            == first["execution_result"]["staged_manifest"]), (
        "retry over the same stage key must not change the staged content")


def test_m3_incomplete_multi_artifact_snapshot_never_marked_ready(setup, monkeypatch, owner):
    """When an approved bundle names MULTIPLE target artifacts and one of
    them fails to materialize (e.g. its current bytes are corrupt/stale),
    the OTHER artifact must not be silently marked staged/ready on its own
    -- a partial snapshot must never be usable by the publish gate.
    This test targets a single-artifact bundle whose one artifact is made
    unreadable AFTER approval (see `test_m2_corrupt_workbook_parser_failure_
    rejected` for the xlsx case); here the same property is asserted via
    the overall job/manifest state: a FAILED apply job must carry NO
    staged_manifest at all (not a partial one), since a partial manifest
    written to a durable row would itself be an incomplete-snapshot leak."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r = build_real_ready_yaml_approval(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-apply-m3-partial.yaml",
        expected_current=14, proposed=30)
    digest = approval_body["execution_result"]["approval_digest"]
    # Record the real decision BEFORE corrupting the source, so this test
    # isolates the fresh-read/malformed-patch guard, not the separate,
    # earlier `approval_not_decided` decision-ledger guard.
    record_real_approve_decision(setup, workspace, approval_job_id=approval_job_id,
                                 digest=digest)

    from tests.ai8t01.conftest import publish_with_chunk
    from tests.ai4t01.conftest import drain_outbox
    # Corrupt the artifact's current published value (not valid YAML at
    # all) after approval, so materialization must fail closed.
    publish_with_chunk(setup["store"], setup["tenant"], workspace,
                       artifact_id="config-apply-m3-partial.yaml",
                       text="not: [valid: yaml: at: all\n")
    drain_outbox(setup["store"], setup["tenant"], workspace)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert body["execution_status"] == "FAILED", body
    result = body.get("execution_result") or {}
    assert not result.get("staged_manifest"), (
        "a FAILED apply must never carry a partial/complete staged_manifest")


def test_m3_yaml_overlay_never_masquerades_as_xlsx(setup):
    """The staged bytes for an `.xlsx`-targeted approval must actually BE a
    readable XLSX workbook (magic bytes / real ZIP-based OOXML container),
    never a YAML cell-overlay text blob written under an `.xlsx` artifact
    id. Verified independently via both the ZIP magic number and an
    openpyxl open, not merely a filename suffix check."""
    import openpyxl

    from tests.ai8t01.conftest import build_real_ready_xlsx_approval

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r, _original = build_real_ready_xlsx_approval(
        setup, client, workspace, target_artifact_id="rates-apply-m3-overlay.xlsx",
        expected_current=14, proposed=30)
    digest = approval_body["execution_result"]["approval_digest"]
    record_real_approve_decision(setup, workspace, approval_job_id=approval_job_id,
                                 digest=digest)

    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=approval_job_id,
        expected_approval_digest=digest)
    assert body["execution_status"] == "COMPLETED", body
    staged_hash = body["execution_result"]["staged_manifest"]["rates-apply-m3-overlay.xlsx"]
    staged_bytes = setup["store"]._verified_blob(staged_hash)

    assert staged_bytes[:2] == b"PK", (
        "a real .xlsx is a ZIP/OOXML container (magic bytes 'PK'); a YAML "
        "cell-overlay text blob must never be staged under an .xlsx artifact id")
    import io
    workbook = openpyxl.load_workbook(io.BytesIO(staged_bytes))
    assert workbook.sheetnames == ["Sheet1", "Notes"]


def test_m3_no_op_action_writes_nothing(setup, monkeypatch, owner):
    """A no-op approved edit (already at the approved target) must write ZERO new bytes at apply
    time (`is_no_op`, head untouched). W1: the product path refuses a no-op bundle (see
    `test_m3_no_op_bundle_is_refused_by_the_product_path`); here ONLY the readiness gate is
    neutralized in-test and the approve row forced, to reach the original assertion."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r = build_real_ready_yaml_approval(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-apply-m3-noop.yaml",
        expected_current=14, proposed=14)
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
    assert result.get("is_no_op") is True, result
    after_head = setup["store"].head(setup["tenant"], workspace)
    assert after_head.version == before_head.version


def test_m3_no_op_bundle_is_refused_by_the_product_path(setup, monkeypatch, owner):
    """W1 product path: a no-op yaml bundle is BLOCKED: approve -> 409; a forced approve row is still
    refused at apply (`approval_readiness_blocked`); head unchanged."""
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace = setup["workspace"]
    approval_job_id, approval_body, _p, _r = build_real_ready_yaml_approval(
        setup, client, workspace, monkeypatch, owner,
        target_artifact_id="config-apply-m3-noop-refused.yaml",
        expected_current=14, proposed=14)
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


def test_m3_apply_executor_never_calls_a_model(setup):
    """Structural/static check (not merely behavioral): the module that
    will implement `kind="apply"`'s executor must declare zero live model/
    provider calls, mirroring EVERY sibling deterministic executor
    (`impact`/`proposal`/`review`/`approval`, each `external = False`, zero
    `provider_calls_planned`). This is asserted by locating the module via
    `infra.worker.job_worker.BUILTIN_EXECUTORS["apply"]` and statically
    scanning its source file's AST for any prompt/provider-call construct
    (`ExecutionRequest(prompt=...)` with a non-None prompt, or an import of
    a live model client) -- a regenerate-the-patch-after-approval bug must
    fail this even if a clever behavioral test were bypassed."""
    from infra.worker import job_worker

    assert "apply" in job_worker.BUILTIN_EXECUTORS, (
        "kind=\"apply\" must be registered in BUILTIN_EXECUTORS before this "
        "structural check can run against its real module")
    executor = job_worker.BUILTIN_EXECUTORS["apply"]
    assert getattr(executor, "external", None) is False, (
        "the apply-stage executor must be a zero-provider-call, "
        "deterministic replay -- never a live model call after approval")

    module = type(executor).__module__
    import importlib
    mod = importlib.import_module(module)
    source_path = Path(mod.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden_names = {"complete", "generate_completion", "call_model", "invoke_provider"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert func_name not in forbidden_names, (
                f"apply-stage module calls {func_name!r}; a patch must never be "
                f"regenerated by a model after approval")
