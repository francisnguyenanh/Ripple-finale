"""Exception-completion is NOT supported.
Completion is blocked while ANY residual mismatch / exception remains, the
residual stays visible, there is no exception policy and `alignment.global` is
never `ALIGNED`. "Exception completion when policy allows" is
therefore unsupported."""

from __future__ import annotations

from uuid import uuid4

from tests.ai8t03.test_verification_negatives import _add_manual_code_action
from tests.ai8t04.conftest import (
    approver_client, awaiting_change, change_dto, machine_verified_change, accept_candidate_km,
    report_digest, result_footprint, seed_open_critical_conflict, submit_result_decision,
    submit_work_proof,
)

EXCEPTION_KEYS = {"exception": True, "allow_residual": True, "allow_exception": True,
                  "policy": "exception", "accept_residual": True, "scope_only": True,
                  "residual_ack": True, "override": True, "force": True}


def test_b1_open_critical_gap_blocks_and_stays_visible_despite_exception_fields(
        setup, monkeypatch, owner, tmp_path):
    target = f"config-b1gap-{uuid4().hex[:6]}.yaml"
    finding = seed_open_critical_conflict(setup, monkeypatch, target)
    change = awaiting_change(setup, monkeypatch, owner, tmp_path, target=target)
    ws, aid = change["workspace"], change["apply_job_id"]
    fp = result_footprint(setup, change)
    resp = submit_result_decision(approver_client(change), ws, aid,
                                  digest=report_digest(change, setup), extra=EXCEPTION_KEYS)
    assert resp.status_code == 409 and "critical_gap_open" in resp.text, resp.text
    assert result_footprint(setup, change) == fp
    dto = change_dto(change["client"], ws, aid)
    assert {"kind": "critical_gap", "finding_id": finding} in dto["alignment"]["residual"]
    assert dto["alignment"]["global"] != "ALIGNED"
    assert dto["state"] != "COMPLETED"


def test_b1_unproven_manual_work_blocks_and_stays_visible_despite_exception_fields(
        setup, monkeypatch, owner, tmp_path):
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path,
                                     before_apply=_add_manual_code_action(setup))
    change["km"] = accept_candidate_km(setup, owner, change)
    ws, aid = change["workspace"], change["apply_job_id"]
    fp = result_footprint(setup, change)
    resp = submit_result_decision(approver_client(change), ws, aid,
                                  digest=report_digest(change, setup), extra=EXCEPTION_KEYS)
    assert resp.status_code == 409 and "manual_work_unproven" in resp.text, resp.text
    assert result_footprint(setup, change) == fp
    dto = change_dto(change["client"], ws, aid)
    assert {"kind": "manual_work", "index": 0} in dto["alignment"]["residual"]
    assert dto["completion"]["eligible"] is False and dto["state"] != "COMPLETED"
    # once the residual is resolved by proof, the residual list empties and it completes
    assert submit_work_proof(approver_client(change), ws, aid).status_code == 200
    assert change_dto(change["client"], ws, aid)["alignment"]["residual"] == []
    ok = submit_result_decision(approver_client(change), ws, aid,
                                digest=report_digest(change, setup))
    assert ok.status_code == 200, ok.text
    assert change_dto(change["client"], ws, aid)["alignment"]["global"] != "ALIGNED"


def test_b1_failed_or_not_run_machine_result_cannot_be_completed_as_an_exception(
        setup, monkeypatch, owner, tmp_path):
    from tests.ai8t03.conftest import review_without_contract_defect
    review_without_contract_defect(monkeypatch)  # W1: no oracle and no contract (simulated review defect)
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path, oracle=None)
    ws, aid = change["workspace"], change["apply_job_id"]
    fp = result_footprint(setup, change)
    resp = submit_result_decision(approver_client(change), ws, aid,
                                  digest=report_digest(change, setup), extra=EXCEPTION_KEYS)
    assert resp.status_code == 409 and "machine_verification_not_pass" in resp.text, resp.text
    assert result_footprint(setup, change) == fp


def test_b1_no_exception_policy_route_or_permission_exists():
    from infra.adapters.v2_policy import ROLE_PERMISSIONS, ROUTE_PERMISSIONS
    assert not [k for k in ROUTE_PERMISSIONS if "exception" in k[1] or "residual" in k[1]], \
        "an exception route exists: W1 does not support exception completion"
    perms = set().union(*ROLE_PERMISSIONS.values())
    assert not [p for p in perms if "exception" in p], perms


def test_b1_alignment_global_is_never_aligned_in_any_reachable_state(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, aid = change["workspace"], change["apply_job_id"]
    seen = [change_dto(change["client"], ws, aid)["alignment"]["global"]]
    assert submit_result_decision(approver_client(change), ws, aid,
                                  digest=report_digest(change, setup)).status_code == 200
    seen.append(change_dto(change["client"], ws, aid)["alignment"]["global"])
    assert "ALIGNED" not in seen, seen


# W1: a BLOCKED review can no longer be approved; this module drives pre-W1 journeys whose subject is
# downstream of the approval, so the review stage states a legacy fixture contract (tests/conftest.py).
import pytest as _w1_pytest  # noqa: E402
pytestmark = _w1_pytest.mark.usefixtures("legacy_contract_injection")
