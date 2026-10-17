"""Post-publish job id namespace and retry-route authority.

Stage-derived job ids (`verify-`, `rebuild-`, `inval-`,
`reanalysis-apply-`) are a squattable namespace. The public route now refuses
the prefixes (asserted below), so the squat is pre-created through
`store.create_job` to drive the DISPATCHER path (defense in depth). Plus the
retry-route authority cases and the ROUTE_PERMISSIONS registry assertions."""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.adapters.v2_policy import (
    JOB_APPROVE_PERMISSION, JOB_CREATE_PERMISSION, ROUTE_PERMISSIONS,
)
from tests.ai8t03.conftest import *  # noqa: F401,F403
from tests.ai8t03.conftest import (
    applied_change, change_dto, exceptions_of, head_tuple, install_provider, job_row,
    outbox_rows, pump, report_of, write_oracle,
)


def _squat(setup, job_id, kind="impact"):
    store, tenant, ws = setup["store"], setup["tenant"], setup["workspace"]
    head = store.head(tenant, ws)
    store.create_job(tenant, ws, job_id, kind, {"candidate_ids": []},
                     expected_version=head.version, idempotency_key=f"squat-{job_id}")


def _stuck(setup):
    return [r for r in outbox_rows(setup["store"], setup["tenant"], setup["workspace"])
            if r[2] != "DONE"]


def _audit_count(setup, event):
    with setup["store"]._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s "
                    "AND event_type=%s", (setup["tenant"], setup["workspace"], event))
        return cur.fetchone()[0]


@pytest.mark.parametrize("prefix", ["verify-", "rebuild-", "inval-", "reanalysis-apply-",
                                    "prop-revise-"])
def test_t1_public_route_refuses_reserved_stage_id_prefixes(
        setup, monkeypatch, owner, prefix):
    change = applied_change(setup, monkeypatch, owner)
    resp = change["client"].post("/v2/jobs", json={
        "workspace_id": change["workspace"], "kind": "impact",
        "job_id": f"{prefix}{change['apply_job_id']}", "payload": {"candidate_ids": []}})
    assert resp.status_code == 400, resp.text
    # control: an ordinary id is still accepted
    ok = change["client"].post("/v2/jobs", json={
        "workspace_id": change["workspace"], "kind": "impact",
        "job_id": f"job-ordinary-{uuid4().hex}", "payload": {"candidate_ids": []}})
    assert ok.status_code == 200, ok.text


@pytest.mark.parametrize("stage", ["verify_job_id", "rebuild_job_id"])
def test_t1_squatted_verify_or_rebuild_id_is_fail_closed_and_never_poisons(
        setup, monkeypatch, owner, tmp_path, stage):
    change = applied_change(setup, monkeypatch, owner)
    write_oracle(tmp_path, monkeypatch, artifact_id=change["target"], expected=30)
    install_provider(monkeypatch, "ok")
    _squat(setup, change[stage])
    log = pump(setup)
    assert not exceptions_of(log), ("dispatcher raised on a squatted stage id", log)
    assert not _stuck(setup), ("queue stuck behind the poisoned event", _stuck(setup))
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert dto["completion"]["eligible"] is False, dto
    assert "stage_job_id_conflict" in dto["completion"]["blocked_reasons"], dto
    assert dto["state"] not in ("MACHINE_VERIFIED", "AWAITING_ACCEPTANCE", "COMPLETED"), dto
    assert _audit_count(setup, "STAGE_JOB_ID_CONFLICT") == 1
    # the foreign row was neither rewritten nor treated as a report
    assert job_row(setup["store"], setup["tenant"], change["workspace"],
                   change[stage])["kind"] == "impact"
    # a human cannot complete it either
    resp = change["ready"]["approver"]["client"].post(
        f"/v2/changes/{change['apply_job_id']}/result-decision", json={
            "workspace_id": change["workspace"], "action": "accept", "reason": "try",
            "expected_report_digest": "sha256:none"})
    assert resp.status_code == 409 and "stage_job_id_conflict" in resp.text, resp.text
    h = change["head_after"]
    assert head_tuple(setup["store"], setup["tenant"], change["workspace"]) == (
        h.version, h.source_revision_id, h.knowledge_revision_id, h.decision_epoch)


def test_t1_squatted_reanalysis_id_fails_the_rebuild_terminally_and_blocks(
        setup, monkeypatch, owner, tmp_path):
    change = applied_change(setup, monkeypatch, owner)
    write_oracle(tmp_path, monkeypatch, artifact_id=change["target"], expected=30)
    install_provider(monkeypatch, "ok")
    _squat(setup, f"reanalysis-apply-{change['apply_job_id']}-{change['target']}")
    log = pump(setup)
    assert not exceptions_of(log), log
    assert not _stuck(setup), _stuck(setup)
    rebuild = job_row(setup["store"], setup["tenant"], change["workspace"],
                      change["rebuild_job_id"])
    assert rebuild["execution_status"] == "FAILED", rebuild
    assert "stage_job_id_conflict" in str(rebuild), rebuild
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert dto["completion"]["eligible"] is False and dto["state"] != "COMPLETED", dto
    assert dto["reanalysis"]["status"] == "FAILED", dto["reanalysis"]
    assert report_of(change, setup["store"], setup["tenant"])["status"] == "PASSED"


def test_t1_squatted_invalidation_id_fails_the_rebuild_terminally_and_blocks(
        setup, monkeypatch, owner, tmp_path):
    from tests.ai4t02.conftest import seed_extraction_candidates
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    target = f"config-t1inv-{uuid4().hex[:6]}.yaml"
    body = seed_extraction_candidates(
        setup, monkeypatch, artifact_id=target, text="refund_window_days: 14\n",
        candidates_json=[{"subject": "refund", "predicate": "window_days", "value": 14,
                          "modality": "must", "scope": {"category": "refund"},
                          "quotes": ["refund_window_days: 14"]}])
    candidate = body["execution_result"]["candidates"][0]["candidate_id"]
    change = applied_change(setup, monkeypatch, owner, target=target)
    write_oracle(tmp_path, monkeypatch, artifact_id=change["target"], expected=30)
    install_provider(monkeypatch, "ok")
    _squat(setup, f"inval-{change['apply_job_id']}-{candidate}")
    log = pump(setup)
    assert not exceptions_of(log), log
    assert not _stuck(setup), _stuck(setup)
    rebuild = job_row(store, tenant, workspace, change["rebuild_job_id"])
    assert rebuild["execution_status"] == "FAILED", rebuild
    assert "stage_job_id_conflict" in str(rebuild), rebuild
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["completion"]["eligible"] is False and dto["state"] != "COMPLETED", dto


def test_t2_retry_route_refuses_revoked_actor_and_never_rewrites_a_report(
        setup, monkeypatch, owner, tmp_path):
    from tests.ai8t02.conftest import revoke_approver
    change = applied_change(setup, monkeypatch, owner)
    write_oracle(tmp_path, monkeypatch, artifact_id=change["target"], expected=30)
    install_provider(monkeypatch, "ok")
    pump(setup)
    digest = report_of(change, setup["store"], setup["tenant"])["report_digest"]
    approver = change["ready"]["approver"]
    revoke_approver(setup, change["workspace"], approver)
    for job in (change["verify_job_id"], change["rebuild_job_id"]):
        resp = approver["client"].post(f"/v2/jobs/{job}/retry",
                                       json={"workspace_id": change["workspace"]})
        assert resp.status_code in (401, 403), (job, resp.status_code, resp.text)
    assert report_of(change, setup["store"], setup["tenant"])["report_digest"] == digest
    assert not _stuck(setup)


@pytest.mark.parametrize("kind", ["ai", "service"])
def test_t2_retry_by_non_human_actor_cannot_replace_a_completed_report(
        setup, monkeypatch, owner, tmp_path, kind):
    from tests.ai8t04.conftest import patch_authority_kind
    change = applied_change(setup, monkeypatch, owner)
    write_oracle(tmp_path, monkeypatch, artifact_id=change["target"], expected=30)
    install_provider(monkeypatch, "ok")
    pump(setup)
    digest = report_of(change, setup["store"], setup["tenant"])["report_digest"]
    patch_authority_kind(monkeypatch, kind)
    for job in (change["verify_job_id"], change["rebuild_job_id"]):
        resp = change["ready"]["approver"]["client"].post(
            f"/v2/jobs/{job}/retry", json={"workspace_id": change["workspace"]})
        assert resp.status_code in (403, 409), (kind, job, resp.status_code, resp.text)
    assert report_of(change, setup["store"], setup["tenant"])["report_digest"] == digest
    assert not _stuck(setup)


def test_t3_route_permissions_registry_and_job_permission_tables():
    assert ROUTE_PERMISSIONS[("GET", "/v2/changes/{apply_job_id}")] == "project.read"
    for path in ("result-decision", "work-proof"):
        assert ROUTE_PERMISSIONS[
            ("POST", f"/v2/changes/{{apply_job_id}}/{path}")] == "change.accept_result"
    for kind in ("verification", "km_rebuild"):
        assert kind not in JOB_CREATE_PERMISSION, "a user could mint a stage result"
        assert JOB_APPROVE_PERMISSION[kind] == "change.accept_result"
