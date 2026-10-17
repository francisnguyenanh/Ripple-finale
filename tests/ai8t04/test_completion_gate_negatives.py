"""The completion gate. A model / unauthorized / revoked actor cannot
complete a change; a human cannot override a machine FAIL / NOT_RUN / PARTIAL;
pending KM, pending or failed index, incomplete reanalysis and missing required
work all block with a named reason; nothing is written when refused.

Each refusal asserts (a) the HTTP class, (b) the named reason code, and (c) the
whole `result_footprint` (audit / CHANGE_COMPLETED / decisions / revisions /
head) is unchanged -- so a refusal that still wrote something fails.
"""

from __future__ import annotations

import json
import threading

import pytest

from infra.adapters import v2_postgres
from tests.ai8t02.conftest import revoke_approver
from tests.ai8t03.test_verification_negatives import _add_manual_code_action
from tests.ai8t04.conftest import (
    accept_candidate_km, approver_client, awaiting_change, change_dto, install_provider, job_row,
    login, machine_verified_change, outbox_rows, patch_authority_kind, pump, reasons_in,
    report_digest, result_footprint, submit_result_decision, write_oracle,
)

Store = v2_postgres.PostgresV2Store


def _refused(setup, change, client, code, *, status=409, digest="report", **kw):
    fp = result_footprint(setup, change)
    d = report_digest(change, setup) if digest == "report" else digest
    resp = submit_result_decision(client, change["workspace"], change["apply_job_id"],
                                  digest=d, **kw)
    allowed = status if isinstance(status, tuple) else (status,)
    assert resp.status_code in allowed, (resp.status_code, resp.text)
    if code:
        assert code in resp.text, (code, resp.text)
    assert result_footprint(setup, change) == fp, "a refused decision wrote something"
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert dto["state"] != "COMPLETED"
    return resp


# ------------------------------------------------------------------ who may complete

@pytest.mark.parametrize("kind", ["ai", "service"])
def test_c2_model_or_service_actor_cannot_complete(setup, monkeypatch, owner, tmp_path, kind):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    patch_authority_kind(monkeypatch, kind)
    _refused(setup, change, approver_client(change), "human", status=403)


def test_c2_actors_without_accept_result_or_a_session_are_refused(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    # requester: creates changes, has change.approve, but NOT change.accept_result
    _refused(setup, change, change["client"], "permission", status=403)
    # knowledge owner: knowledge.confirm only
    _refused(setup, change, owner["client"], "permission", status=403)
    # no session at all
    _refused(setup, change, setup["browser"](), None, status=401)


def test_c2_revoked_approver_cannot_complete_current_permission_is_rechecked(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    revoke_approver(setup, change["workspace"], change["ready"]["approver"])
    # revocation surfaces as an authentication/authorization refusal, never a 200
    _refused(setup, change, approver_client(change), None, status=(401, 403))


# ------------------------------------------------------------------ machine result is not overridable

@pytest.mark.parametrize("oracle, status", [(45, "FAILED"), (None, "NOT_RUN")],
                         ids=["machine-FAIL", "machine-NOT_RUN"])
def test_c2_human_cannot_override_a_failed_or_not_run_machine_report(
        setup, monkeypatch, owner, tmp_path, oracle, status):
    if oracle is None:
        # W1: no oracle AND no contract is only reachable through a (simulated) review defect
        from tests.ai8t03.conftest import review_without_contract_defect
        review_without_contract_defect(monkeypatch)
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path, oracle=oracle)
    assert job_row(setup["store"], setup["tenant"], change["workspace"],
                   change["verify_job_id"])["execution_result"]["status"] == status
    # the KM candidate exists and a human accepted it: only the machine result blocks
    change["km"] = accept_candidate_km(setup, owner, change)
    resp = _refused(setup, change, approver_client(change), "machine_verification_not_pass")
    assert "machine_verification_not_pass" in resp.json()["reasons"]
    view = change_dto(change["client"], change["workspace"], change["apply_job_id"])["result_view"]
    assert "acceptance_tests" in json.dumps(view["failed_checks"]), \
        "the result view must show the failed / not-run check"
    # nothing the caller sends can flip it
    for extra in ({"override": True}, {"force": True, "report_status": "PASS"},
                  {"status": "PASS", "checks": {"acceptance_tests": {"status": "PASS"}}}):
        _refused(setup, change, approver_client(change), "machine_verification_not_pass",
                 extra=extra)
    # and request_changes is the only way forward: it does not turn the report green
    rc = submit_result_decision(approver_client(change), change["workspace"],
                                change["apply_job_id"], action="request_changes",
                                digest=report_digest(change, setup))
    assert rc.status_code == 200, rc.text
    assert job_row(setup["store"], setup["tenant"], change["workspace"],
                   change["verify_job_id"])["execution_result"]["status"] == status


def test_c2_partial_report_with_unproven_required_work_is_refused(
        setup, monkeypatch, owner, tmp_path):
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path,
                                     before_apply=_add_manual_code_action(setup))
    change["km"] = accept_candidate_km(setup, owner, change)
    resp = _refused(setup, change, approver_client(change), "manual_work_unproven")
    assert "manual_work_unproven" in resp.json()["reasons"]


# ------------------------------------------------------------------ knowledge / index / reanalysis

def test_c2_pending_knowledge_candidate_blocks_completion(setup, monkeypatch, owner, tmp_path):
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path)
    assert change_dto(change["client"], change["workspace"],
                      change["apply_job_id"])["state"] == "MACHINE_VERIFIED"
    resp = _refused(setup, change, approver_client(change), "knowledge_not_accepted")
    assert "knowledge_not_accepted" in resp.json()["reasons"]


def test_c2_failed_km_index_blocks_completion_even_after_the_km_was_accepted(
        setup, monkeypatch, owner, tmp_path):
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path)
    original = Store.record_index_manifest

    def _no_ready(self, t, w, rev, index_version, manifest_hash, status, **kw):
        if status == "READY":
            raise RuntimeError("index backend unavailable")
        return original(self, t, w, rev, index_version, manifest_hash, status, **kw)

    with monkeypatch.context() as mp:
        mp.setattr(Store, "record_index_manifest", _no_ready)
        change["km"] = accept_candidate_km(setup, owner, change)
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert dto["knowledge"]["accepted"] is True
    assert dto["knowledge"]["km_index_status"] != "READY"
    resp = _refused(setup, change, approver_client(change), "index_not_ready")
    assert "index_not_ready" in resp.json()["reasons"]


def test_c2_open_critical_gap_in_the_change_scope_blocks_completion(
        setup, monkeypatch, owner, tmp_path):
    """C15: 'critical GAP trong pham vi anh huong -> khong complete'. The open HIGH
    conflict involves a candidate extracted from the very artifact the change
    edited."""
    from uuid import uuid4

    from tests.ai8t04.conftest import seed_open_critical_conflict
    target = f"config-t04gap-{uuid4().hex[:6]}.yaml"
    finding = seed_open_critical_conflict(setup, monkeypatch, target)
    change = awaiting_change(setup, monkeypatch, owner, tmp_path, target=target)
    resp = _refused(setup, change, approver_client(change), "critical_gap_open")
    assert "critical_gap_open" in resp.json()["reasons"]
    assert finding in resp.text, "the blocking gap must be named"
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert "critical_gap_open" in " ".join(dto["completion"]["blocked_reasons"])


def test_c2_unrelated_open_critical_gap_does_not_block_completion(
        setup, monkeypatch, owner, tmp_path):
    """Control for the previous test ('Critical gap chan affected change, khong cam
    xem unrelated data'): an open HIGH conflict between OTHER artifacts is outside
    the change scope and must not block."""
    from tests.ai5t02.conftest import seed_conflict_finding
    seed_conflict_finding(setup, monkeypatch, owner["client"])
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    resp = submit_result_decision(approver_client(change), change["workspace"],
                                  change["apply_job_id"], digest=report_digest(change, setup))
    assert resp.status_code == 200, resp.text


def test_c2_incomplete_or_failed_reanalysis_blocks_completion_with_a_named_reason(
        setup, monkeypatch, owner, tmp_path):
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path, provider="outage")
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert dto["knowledge"]["candidate_revision_id"] is None
    resp = _refused(setup, change, approver_client(change), None)
    codes = resp.json()["reasons"]
    assert "reanalysis_incomplete" in codes or "knowledge_not_accepted" in codes, codes


def test_c2_manual_work_only_apply_has_no_report_and_can_never_be_completed(
        setup, monkeypatch, owner):
    from uuid import uuid4

    from tests.ai8t02.test_publish_f5_manual_work import _apply, _bundle, _client
    ws = setup["workspace"]
    client = _client(setup)
    b = _bundle(setup, client)
    job_id = f"job-apply-{uuid4().hex}"
    assert _apply(setup, client, b, job_id=job_id)["execution_status"] == "COMPLETED"
    install_provider(monkeypatch, "ok")
    pump(setup)
    approver = b["approver"]["client"]
    resp = submit_result_decision(approver, ws, job_id, digest="sha256:none")
    assert resp.status_code == 409 and "report_missing" in resp.text, resp.text


# ------------------------------------------------------------------ digest binding and malformed input

def test_c2_wrong_missing_or_forged_digest_and_malformed_bodies_are_refused(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, aid = change["workspace"], change["apply_job_id"]
    real = report_digest(change, setup)
    _refused(setup, change, approver_client(change), "report_digest_mismatch",
             digest="0" * len(real))
    # the APPLY job's own approval digest is not the report digest
    _refused(setup, change, approver_client(change), "report_digest_mismatch",
             digest=change["ready"]["digest"])
    fp = result_footprint(setup, change)
    client = approver_client(change)
    assert submit_result_decision(client, ws, aid, digest=None).status_code == 400
    assert submit_result_decision(client, ws, aid, digest=real, action="approve").status_code == 400
    assert submit_result_decision(client, ws, aid, digest=real, reason="   ").status_code == 400
    assert submit_result_decision(client, ws, "job-apply-does-not-exist",
                                  digest=real).status_code == 404
    assert result_footprint(setup, change) == fp


# ------------------------------------------------------------------ concurrency

def test_c2_two_simultaneous_accepts_produce_one_completion(setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, aid = change["workspace"], change["apply_job_id"]
    digest = report_digest(change, setup)
    approver = change["ready"]["approver"]
    clients = []
    for _ in range(2):
        c = setup["browser"]()
        login(c, approver["account"], approver["credential"])
        clients.append(c)
    barrier, results = threading.Barrier(2), []

    def _go(client, key):
        barrier.wait()
        results.append(submit_result_decision(client, ws, aid, digest=digest,
                                              idempotency_key=key))

    threads = [threading.Thread(target=_go, args=(c, f"race-{i}")) for i, c in enumerate(clients)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    codes = sorted(r.status_code for r in results)
    assert codes in ([200, 200], [200, 409]), [(r.status_code, r.text) for r in results]
    ids = {r.json()["decision_id"] for r in results if r.status_code == 200}
    assert len(ids) == 1
    fp = result_footprint(setup, change)
    assert fp["completed_events"] == 1 and fp["audit"] == 1
    assert len(outbox_rows(setup["store"], setup["tenant"], ws, "CHANGE_COMPLETED")) == 1


# W1: a BLOCKED review can no longer be approved; this module drives pre-W1 journeys whose subject is
# downstream of the approval, so the review stage states a legacy fixture contract (tests/conftest.py).
import pytest as _w1_pytest  # noqa: E402
pytestmark = _w1_pytest.mark.usefixtures("legacy_contract_injection")
