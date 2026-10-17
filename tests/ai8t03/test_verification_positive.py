"""Verification positive half and pipeline wiring: a published apply is verified
from the ACTUAL published bytes by a post-publish worker fed by the durable
`SOURCE_PUBLISHED` outbox row; the report is immutable, digest-bound, and
read through the real API.

Entry points exercised (never a helper alone): `POST /v2/jobs {apply}` ->
`job_worker.claim_and_admit_one` (apply publish + outbox row) ->
`claim_and_admit_one` again (SOURCE_PUBLISHED consumer -> verification job ->
executor) -> `GET /v2/jobs/verify-<id>` / `GET /v2/changes/<id>` + SQL.
"""

from __future__ import annotations

from uuid import uuid4

from tests.ai8t03.conftest import (
    applied_change, artifact_bytes, as_reviewed_report, assert_head_unmoved, change_dto, get_job_status,
    install_provider, pump, reasons_of, report_of, sha256_hex, source_published_event,
    write_oracle,
)

REQUIRED_CHECKS = ("published_bytes_readback", "expected_edits", "parse_after",
                   "acceptance_tests")


def _run(setup, monkeypatch, owner, tmp_path, *, oracle=30, **kw):
    change = applied_change(setup, monkeypatch, owner, **kw)
    if oracle is not None:
        write_oracle(tmp_path, monkeypatch, artifact_id=change["target"], expected=oracle)
    change["provider"] = install_provider(monkeypatch, "ok")
    change["log"] = pump(setup)
    return change


def test_z0_before_any_worker_runs_change_is_source_published_and_not_eligible(
        setup, monkeypatch, owner):
    """Publish is durable on its own; nothing is verified or complete yet."""
    change = applied_change(setup, monkeypatch, owner)
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert dto["state"] == "SOURCE_PUBLISHED", dto
    assert dto["published_revision_id"] == change["s1"]
    assert not dto.get("verification"), "no report may exist before the worker ran"
    assert dto["completion"]["eligible"] is False
    assert dto["completion"]["blocked_reasons"], "a not-yet-verified change must say why"
    # the durable rebuild intent exists exactly once and is still pending
    event = source_published_event(setup["store"], setup["tenant"], setup["workspace"],
                                   change["apply_job_id"])
    assert event[2] == "PENDING"


def test_z1_pipeline_outbox_to_pass_report_with_independent_oracle(
        setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]

    # The dispatcher (not the test) consumed the durable event.
    event = source_published_event(store, tenant, workspace, change["apply_job_id"])
    assert event[2] == "DONE", ("SOURCE_PUBLISHED apply event must be consumed and acked "
                                f"by the production dispatcher, got {event[2]}")

    report = report_of(change, store, tenant)
    assert report["status"] == "PASSED", report
    assert report["reasons"] == []
    assert report["apply_job_id"] == change["apply_job_id"]
    assert report["after_source_revision_id"] == change["s1"]
    assert report["parent_revision_id"] == change["s0"]
    assert report["missing_edits"] == [] and report["unexpected_edits"] == []
    for name in REQUIRED_CHECKS:
        assert report["checks"][name]["status"] == "PASS", (name, report["checks"])
        assert report["checks"][name]["required"] is True
    assert isinstance(report["report_digest"], str) and len(report["report_digest"]) >= 32
    # consumed the approved chain: approval identity + the approved verification plan
    apply_result = change["apply_body"]["execution_result"]
    assert report["approval_job_id"] == change["ready"]["approval_job_id"]
    assert report["scope"]["verification_plan"] == apply_result["verification_scope"]["verification_plan"]
    # the report IS the schema VerificationReport (schema + C15 invariant)
    reviewed = as_reviewed_report(report)
    assert reviewed.status == "PASSED" and reviewed.after_source_revision_id == change["s1"]
    assert report["id"] == change["verify_job_id"] and report["schema_version"] == "1.0.0"
    assert report["after_knowledge_revision_id"] is None, "a KM is only a candidate at machine time"
    assert report["open_required_work"] == []
    for section in ("parsing_result", "tests_result", "simulation_result", "evidence_result"):
        assert isinstance(report[section], dict) and report[section].get("status"), section
    # yaml has no executable logic: reported as not applicable, never silently absent
    assert report["checks"]["simulation_exact_after"]["status"] == "SKIPPED_NOT_APPLICABLE"

    # The same report is what the change read surface exposes.
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["verification"]["job_id"] == change["verify_job_id"]
    assert dto["verification"]["status"] == "PASSED"
    assert dto["verification"]["report_digest"] == report["report_digest"]
    assert_head_unmoved(setup, change)


def test_z1_report_carries_exact_actual_bytes_hash_and_values(
        setup, monkeypatch, owner, tmp_path):
    """Z1/Z3: 'exact actual bytes/hash ... hien trong report'; findings read the
    ACTUAL after source, not the proposal's recorded after-value."""
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    report = report_of(change, store, tenant)
    target = change["target"]
    actual = artifact_bytes(store, tenant, workspace, change["s1"], target)
    entry = report["artifacts"][target]
    view = {a["artifact_id"]: a for a in
            store.source_revision_view(tenant, workspace, change["s1"])["artifacts"]}
    assert entry["blob_hash"] == view[target]["blob_hash"]
    assert entry["sha256"] == sha256_hex(actual)
    assert entry["readable"] is True and entry["checksum_ok"] is True
    assert report["actual_values"][target]["refund_window_days"] == change["proposed"]
    # expected vs actual edit lists are artifact-level and equal on a clean apply
    assert report["expected_edits"] and report["actual_edits"]
    assert [e["artifact_id"] for e in report["expected_edits"]] == [target]
    assert [e["artifact_id"] for e in report["actual_edits"]] == [target]


def test_z1_multi_artifact_head_siblings_are_verified_unchanged_not_unexpected(
        setup, monkeypatch, owner, tmp_path):
    """Multi-artifact head. Untouched artifacts appear in the report by
    their unchanged blob hash and are never reported as unexpected writes."""
    siblings = {"sibling-a.yaml": "keep_a: 1\n", "sibling-b.yaml": "keep_b: 2\n"}
    change = _run(setup, monkeypatch, owner, tmp_path, siblings=siblings)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    report = report_of(change, store, tenant)
    assert report["status"] == "PASSED", report
    assert report["unexpected_edits"] == [] and report["missing_edits"] == []
    for aid, body in siblings.items():
        entry = report["artifacts"][aid]
        assert entry["sha256"] == sha256_hex(body.encode("utf-8"))
        assert entry["readable"] is True and entry["checksum_ok"] is True
    assert set(report["artifacts"]) == {change["target"], *siblings}


def test_z1_no_independent_oracle_is_not_run_never_pass(
        setup, monkeypatch, owner, tmp_path):
    """C15/C20: a required check that never ran (no independent harness/oracle
    configured) can never be PASS; the AI's own edit matching its own plan is not
    enough."""
    from tests.ai8t03.conftest import review_without_contract_defect
    review_without_contract_defect(monkeypatch)  # W1: no operator contract either (see helper)
    change = _run(setup, monkeypatch, owner, tmp_path, oracle=None)
    store, tenant = setup["store"], setup["tenant"]
    report = report_of(change, store, tenant)
    assert report["status"] == "NOT_RUN", report
    assert report["checks"]["acceptance_tests"]["status"] == "NOT_RUN"
    assert "required_check_not_run:acceptance_tests" in reasons_of(report)
    # Everything derivable from the bytes still passes and is still reported.
    assert report["checks"]["published_bytes_readback"]["status"] == "PASS"
    dto = change_dto(change["client"], setup["workspace"], change["apply_job_id"])
    assert dto["state"] not in ("MACHINE_VERIFIED", "AWAITING_ACCEPTANCE", "COMPLETED")
    assert dto["completion"]["eligible"] is False
    assert_head_unmoved(setup, change)


def test_z1_independent_oracle_disagreeing_with_the_ai_edit_fails(
        setup, monkeypatch, owner, tmp_path):
    """Card: 'khong chi test AI vua sua'. The human-confirmed oracle expects 45;
    the AI applied 30 (matching its own proposal). Diff/parse checks pass, the
    independent oracle fails, so the report is FAIL."""
    change = _run(setup, monkeypatch, owner, tmp_path, oracle=45)
    store, tenant = setup["store"], setup["tenant"]
    report = report_of(change, store, tenant)
    assert report["status"] == "FAILED", report
    assert report["checks"]["acceptance_tests"]["status"] == "FAIL"
    assert report["checks"]["expected_edits"]["status"] == "PASS"
    assert report["checks"]["parse_after"]["status"] == "PASS"
    assert "required_check_failed:acceptance_tests" in reasons_of(report)
    dto = change_dto(change["client"], setup["workspace"], change["apply_job_id"])
    assert dto["state"] not in ("MACHINE_VERIFIED", "AWAITING_ACCEPTANCE", "COMPLETED")


def test_z1_report_digest_is_stable_on_reread_and_bound_to_the_change_view(
        setup, monkeypatch, owner, tmp_path):
    a = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    d1 = report_of(a, store, tenant)["report_digest"]
    again = get_job_status(a["client"], workspace, a["verify_job_id"])
    assert again["execution_result"]["report_digest"] == d1
    view = change_dto(a["client"], workspace, a["apply_job_id"])["verification"]
    assert view["report_digest"] == d1
    # more pumping (duplicate delivery, idle workers) never rewrites the report
    pump(setup)
    assert report_of(a, store, tenant)["report_digest"] == d1


def test_z1_verification_makes_no_provider_call_and_records_zero_calls(
        setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    attempts = store.job_attempts(tenant, workspace, change["verify_job_id"])
    assert attempts, "the verification execution must leave an attempt row"
    assert all(a["provider_calls"] == 0 for a in attempts), attempts
    rebuild = store.job_attempts(tenant, workspace, change["rebuild_job_id"])
    assert rebuild and all(a["provider_calls"] == 0 for a in rebuild), rebuild


def test_z1_users_cannot_forge_or_create_a_verification_result(
        setup, monkeypatch, owner, tmp_path):
    """A model/user cannot mint a report: `verification` / `km_rebuild` are not
    creatable through the public job route at all, for any role."""
    change = applied_change(setup, monkeypatch, owner)
    workspace, store, tenant = change["workspace"], setup["store"], setup["tenant"]
    approver_client = change["ready"]["approver"]["client"]
    for who, client in (("requester", change["client"]), ("approver", approver_client)):
        for kind in ("verification", "km_rebuild"):
            jid = f"job-forged-{uuid4().hex}"
            resp = client.post("/v2/jobs", json={
                "workspace_id": workspace, "kind": kind, "job_id": jid,
                "payload": {"apply_job_id": change["apply_job_id"],
                            "status": "PASS"}})
            assert resp.status_code == 400, (who, kind, resp.status_code, resp.text)
            with store._tx() as cur:
                cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND "
                            "workspace_id=%s AND job_id=%s", (tenant, workspace, jid))
                assert cur.fetchone()[0] == 0, "a forged job row was created"


def test_z1_retry_of_verification_needs_accept_result_permission_and_a_failed_job(
        setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner, tmp_path)
    workspace = change["workspace"]
    body = {"workspace_id": workspace}
    # requester (source.import only) may not retry a verification job
    denied = change["client"].post(f"/v2/jobs/{change['verify_job_id']}/retry", json=body)
    assert denied.status_code == 403, denied.text
    # the approver (change.accept_result) may ask, but a COMPLETED report is never
    # re-run or replaced: too-early/wrong-state refusal, report unchanged.
    approver_client = change["ready"]["approver"]["client"]
    before = report_of(change, setup["store"], setup["tenant"])["report_digest"]
    refused = approver_client.post(f"/v2/jobs/{change['verify_job_id']}/retry", json=body)
    assert refused.status_code == 409, refused.text
    assert report_of(change, setup["store"], setup["tenant"])["report_digest"] == before
