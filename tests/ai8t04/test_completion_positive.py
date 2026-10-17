"""Valid human acceptance of the exact machine report, duplicate /
idempotent retry, request_changes, and the lineage/timeline read surface after a
restart.

Entry points: real session -> `POST /v2/changes/{apply_job_id}/result-decision`
(permission `change.accept_result`) -> SQL (decision + audit + outbox) ->
`GET /v2/changes/{apply_job_id}`; the machine report, KM candidate and index come
from the verification chain (real apply gate -> SOURCE_PUBLISHED outbox -> dispatcher).
"""

from __future__ import annotations

import pytest

from tests.ai8t03.test_verification_negatives import _add_manual_code_action
from tests.ai8t04.conftest import (
    accept_candidate_km, approver_client, awaiting_change, change_dto, head_tuple, job_row,
    login, machine_verified_change, outbox_rows, pinned, report_digest, restarted,
    result_footprint, submit_result_decision, submit_work_proof,
)


def _decisions_of(change, setup, job_id):
    resp = change["client"].get(f"/v2/jobs/{job_id}/decisions",
                                params={"workspace_id": change["workspace"]})
    assert resp.status_code == 200, resp.text
    return resp.json()["decisions"]


def test_c1_ready_state_is_awaiting_acceptance_with_a_full_result_view(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert dto["state"] == "AWAITING_ACCEPTANCE", dto
    assert dto["completion"]["status"] == "AWAITING_ACCEPTANCE"
    assert dto["completion"]["eligible"] is True and dto["completion"]["blocked_reasons"] == []
    view = dto["result_view"]
    assert set(view) >= {"expected", "actual", "simulation", "conflicts", "scope",
                         "failed_checks", "knowledge_delta"}, view
    assert view["failed_checks"] == []
    assert set(view["simulation"]) >= {"before", "after"}
    assert set(view["conflicts"]) >= {"resolved", "residual"}
    assert change["target"] in " ".join(map(str, view["scope"]))
    assert view["knowledge_delta"], "knowledge delta (S0->S1, K candidate) must be shown"
    assert dto["knowledge"]["accepted"] is True


def test_c1_human_accept_completes_once_bound_to_the_exact_report_digest(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    store, tenant, ws = setup["store"], setup["tenant"], change["workspace"]
    digest = report_digest(change, setup)
    report_before = job_row(store, tenant, ws, change["verify_job_id"])
    fp0 = result_footprint(setup, change)
    approver = change["ready"]["approver"]

    resp = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                  digest=digest, idempotency_key="accept-1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "accept" and body["report_digest"] == digest
    assert body["replayed"] is False and body["decision_id"]

    dto = change_dto(change["client"], ws, change["apply_job_id"])
    assert dto["state"] == "COMPLETED" and dto["completion"]["status"] == "COMPLETED"
    decision = dto["completion"]["decision"]
    assert decision["decision_id"] == body["decision_id"] and decision["action"] == "accept"
    assert decision["actor_id"] == approver["account"]
    assert decision["report_digest"] == digest

    fp1 = result_footprint(setup, change)
    assert fp1["audit"] == fp0["audit"] + 1 and fp1["completed_events"] == fp0["completed_events"] + 1
    # attestation only: no head movement, no source/KM revision, no semantic decision
    for key in ("head", "semantic_decisions", "source_revisions", "km_revisions"):
        assert fp1[key] == fp0[key], key
    events = [r for r in outbox_rows(store, tenant, ws, "CHANGE_COMPLETED")]
    assert len(events) == 1
    payload = events[0][3]
    assert payload["apply_job_id"] == change["apply_job_id"] and payload["report_digest"] == digest
    assert payload["decision_id"] == body["decision_id"]
    assert payload["published_revision_id"] == change["s1"]
    assert payload["knowledge_revision_id"] == change["km"]
    # the machine report is exactly as the machine left it
    assert job_row(store, tenant, ws, change["verify_job_id"]) == report_before


def test_c1_duplicate_accept_is_idempotent_and_adds_nothing(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws = change["workspace"]
    digest = report_digest(change, setup)
    # legacy verified-lineage semantics: nothing before the attested completion
    assert change_dto(change["client"], ws, change["apply_job_id"])["lineage"]["verified_version"] == 0
    first = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                   digest=digest, idempotency_key="k-dup")
    assert first.status_code == 200, first.text
    assert change_dto(change["client"], ws, change["apply_job_id"])["lineage"]["verified_version"] == 1
    fp = result_footprint(setup, change)
    for key in ("k-dup", "another-key", None):
        again = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                       digest=digest, idempotency_key=key)
        assert again.status_code == 200, (key, again.text)
        assert again.json()["decision_id"] == first.json()["decision_id"]
        assert again.json()["replayed"] is True
    assert result_footprint(setup, change) == fp, "a replayed attestation wrote something"
    # ... and a retried attestation never inflates the verified counter
    assert change_dto(change["client"], ws, change["apply_job_id"])["lineage"]["verified_version"] == 1
    # a different terminal action after the completion is refused, not recorded
    other = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                   action="request_changes", digest=digest)
    assert other.status_code == 409 and "already_decided" in other.text
    assert result_footprint(setup, change) == fp


def test_c1_request_changes_opens_a_followup_proposal_and_never_edits_the_report(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    store, tenant, ws = setup["store"], setup["tenant"], change["workspace"]
    digest = report_digest(change, setup)
    report_before = job_row(store, tenant, ws, change["verify_job_id"])
    fp0 = result_footprint(setup, change)
    resp = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                  action="request_changes", reason="window should be 45",
                                  digest=digest)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    new_job = body["new_proposal_job_id"]
    rec = job_row(store, tenant, ws, new_job)
    assert rec["kind"] == "proposal"
    assert rec["payload"]["action"] == "revise"
    assert rec["payload"]["previous_proposal_job_id"] == change["ready"]["proposal_job_id"]
    assert rec["requested_by"] == change["ready"]["approver"]["account"]
    # the next intent starts from what is ACTUALLY current (S1), not S0
    assert pinned(store, tenant, ws, new_job)[0] == change["s1"]
    dto = change_dto(change["client"], ws, change["apply_job_id"])
    assert dto["state"] != "COMPLETED"
    assert dto["completion"]["status"] == "CHANGES_REQUESTED"
    assert dto["completion"]["decision"]["action"] == "request_changes"
    assert dto["lineage"]["verified_version"] == 0, "request_changes verifies nothing"
    fp1 = result_footprint(setup, change)
    assert fp1["completed_events"] == fp0["completed_events"] == 0
    assert fp1["head"] == fp0["head"] and fp1["semantic_decisions"] == fp0["semantic_decisions"]
    assert job_row(store, tenant, ws, change["verify_job_id"]) == report_before
    # history: the change cannot be accepted afterwards
    late = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                  digest=digest)
    assert late.status_code == 409 and "already_decided" in late.text


def test_c1_lineage_chain_and_decision_survive_a_restart(setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws = change["workspace"]
    digest = report_digest(change, setup)
    resp = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                  digest=digest)
    assert resp.status_code == 200, resp.text
    approval_decision = [d for d in _decisions_of(change, setup, change["ready"]["approval_job_id"])
                         if d["action"] == "approve"][0]["decision_id"]
    expected = [("source_before", change["s0"]),
                ("proposal", change["ready"]["proposal_job_id"]),
                ("review", change["ready"]["review_job_id"]),
                ("approval", change["ready"]["approval_job_id"]),
                ("approval_decision", approval_decision),
                ("apply", change["apply_job_id"]),
                ("source_after", change["s1"]),
                ("verification", digest),
                ("knowledge", change["km"]),
                ("completion", resp.json()["decision_id"])]

    def chain(dto):
        return [(e["kind"], e["ref"]) for e in dto["lineage"]["chain"]]

    first = change_dto(change["client"], ws, change["apply_job_id"])
    assert chain(first) == expected, chain(first)
    # restart: fresh session + fresh store object => identical durable answer
    fresh_client = setup["browser"]()
    login(fresh_client, setup["requester"], setup["credential"])
    second = change_dto(fresh_client, ws, change["apply_job_id"])
    assert chain(second) == expected
    assert second["completion"]["decision"] == first["completion"]["decision"]
    assert second["state"] == "COMPLETED"
    fresh = restarted(setup)
    assert head_tuple(fresh["store"], setup["tenant"], ws) == head_tuple(
        setup["store"], setup["tenant"], ws)


def test_c1_completed_change_reports_bounded_scope_never_global_alignment(
        setup, monkeypatch, owner, tmp_path):
    """'Exception van hien thi residual alignment ... khong global ALIGNED': a
    completion is scoped to the applied artifacts; it never claims the whole
    workspace is aligned."""
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    digest = report_digest(change, setup)
    assert submit_result_decision(approver_client(change), change["workspace"],
                                  change["apply_job_id"], digest=digest).status_code == 200
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert dto["alignment"]["global"] != "ALIGNED", dto["alignment"]
    assert dto["alignment"]["scope"] == [change["target"]]
    assert "residual" in dto["alignment"]


# ------------------------------------------------------------------ required manual work needs proof

def _partial_change(setup, monkeypatch, owner, tmp_path):
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path,
                                     before_apply=_add_manual_code_action(setup))
    change["km"] = accept_candidate_km(setup, owner, change)
    return change


def test_c1_manual_work_needs_durable_proof_before_the_partial_report_can_be_accepted(
        setup, monkeypatch, owner, tmp_path):
    change = _partial_change(setup, monkeypatch, owner, tmp_path)
    ws, store, tenant = change["workspace"], setup["store"], setup["tenant"]
    digest = report_digest(change, setup)
    blocked = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                     digest=digest)
    assert blocked.status_code == 409 and "manual_work_unproven" in blocked.text, blocked.text

    # proof endpoint guards
    assert submit_work_proof(change["client"], ws, change["apply_job_id"]).status_code == 403
    assert submit_work_proof(approver_client(change), ws, change["apply_job_id"],
                             evidence_ref="  ").status_code == 400
    assert submit_work_proof(approver_client(change), ws, change["apply_job_id"],
                             index=7).status_code in (400, 404)
    assert change_dto(change["client"], ws, change["apply_job_id"])["manual_work"][0]["proof"] is None

    ok = submit_work_proof(approver_client(change), ws, change["apply_job_id"],
                           evidence_ref="PR-4711 merged: refund_service.py")
    assert ok.status_code == 200, ok.text
    again = submit_work_proof(approver_client(change), ws, change["apply_job_id"],
                              evidence_ref="PR-4711 merged: refund_service.py")
    assert again.status_code == 200, "proof is idempotent per index"
    proof = change_dto(change["client"], ws, change["apply_job_id"])["manual_work"][0]["proof"]
    assert proof["evidence_ref"].startswith("PR-4711") and proof["actor_id"] == \
        change["ready"]["approver"]["account"]

    resp = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                  digest=digest)
    assert resp.status_code == 200, resp.text
    assert change_dto(change["client"], ws, change["apply_job_id"])["state"] == "COMPLETED"
    # the immutable report still says PARTIAL: the proof did not rewrite the machine result
    assert job_row(store, tenant, ws, change["verify_job_id"])["execution_result"]["status"] == "PARTIAL"
    assert resp.json()["report_digest"] == digest
