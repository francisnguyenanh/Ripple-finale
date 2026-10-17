"""Edge / contract tests for result acceptance (reject action, accept-vs-reject race, completed change
survives a later head move, request_changes on a stale change, non-human actor
on the work-proof route)."""

from __future__ import annotations

import threading

from tests.ai4t01.conftest import publish_with_chunk
from tests.ai8t04.conftest import (
    approver_client, awaiting_change, change_dto, job_row, login, machine_verified_change,
    accept_candidate_km, outbox_rows, patch_authority_kind, report_digest, result_footprint,
    submit_result_decision, submit_work_proof,
)
from tests.ai8t03.test_verification_negatives import _add_manual_code_action


def _verified_version(change):
    return change_dto(change["client"], change["workspace"],
                      change["apply_job_id"])["lineage"]["verified_version"]


def test_r1_reject_is_a_terminal_report_bound_attestation_that_never_completes(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, aid, store, tenant = change["workspace"], change["apply_job_id"], setup["store"], \
        setup["tenant"]
    digest = report_digest(change, setup)
    report_before = job_row(store, tenant, ws, change["verify_job_id"])["execution_result"]
    fp0 = result_footprint(setup, change)
    # guards first: requester, wrong digest
    assert submit_result_decision(change["client"], ws, aid, action="reject",
                                  digest=digest).status_code == 403
    wrong = submit_result_decision(approver_client(change), ws, aid, action="reject",
                                   digest="sha256:" + "0" * 64)
    assert wrong.status_code == 409 and "report_digest_mismatch" in wrong.text
    assert result_footprint(setup, change) == fp0
    resp = submit_result_decision(approver_client(change), ws, aid, action="reject",
                                  digest=digest, reason="not what was requested")
    assert resp.status_code == 200, resp.text
    dto = change_dto(change["client"], ws, aid)
    assert dto["state"] != "COMPLETED" and dto["completion"]["status"] == "REJECTED", dto
    fp1 = result_footprint(setup, change)
    assert fp1["completed_events"] == 0 and fp1["audit"] == fp0["audit"] + 1
    assert fp1["head"] == fp0["head"] and fp1["semantic_decisions"] == fp0["semantic_decisions"]
    assert _verified_version(change) == 0
    assert job_row(store, tenant, ws, change["verify_job_id"])["execution_result"] == report_before
    # terminal: replay is idempotent, the opposite action is refused
    again = submit_result_decision(approver_client(change), ws, aid, action="reject",
                                   digest=digest, reason="not what was requested")
    assert again.status_code == 200 and again.json()["replayed"] is True
    accept = submit_result_decision(approver_client(change), ws, aid, digest=digest)
    assert accept.status_code == 409 and "already_decided" in accept.text
    assert result_footprint(setup, change) == fp1


def test_r2_concurrent_accept_and_reject_produce_exactly_one_terminal_decision(
        setup, monkeypatch, owner, tmp_path):
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

    def _go(client, action):
        barrier.wait()
        results.append((action, submit_result_decision(client, ws, aid, action=action,
                                                       digest=digest)))

    threads = [threading.Thread(target=_go, args=(c, a))
               for c, a in zip(clients, ("accept", "reject"))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    codes = sorted(r.status_code for _, r in results)
    assert codes == [200, 409], [(a, r.status_code, r.text) for a, r in results]
    winner = next(a for a, r in results if r.status_code == 200)
    fp = result_footprint(setup, change)
    assert fp["audit"] == 1
    assert fp["completed_events"] == (1 if winner == "accept" else 0)
    dto = change_dto(change["client"], ws, aid)
    assert dto["completion"]["decision"]["action"] == winner


def test_r3_completed_change_survives_a_later_head_move_and_replays(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, aid = change["workspace"], change["apply_job_id"]
    digest = report_digest(change, setup)
    first = submit_result_decision(approver_client(change), ws, aid, digest=digest)
    assert first.status_code == 200, first.text
    decision = change_dto(change["client"], ws, aid)["completion"]["decision"]
    publish_with_chunk(setup["store"], setup["tenant"], ws, artifact_id="later-import.yaml",
                       text="later: 1\n")
    dto = change_dto(change["client"], ws, aid)
    assert dto["state"] == "COMPLETED" and dto["completion"]["status"] == "COMPLETED", dto
    assert dto["completion"]["decision"] == decision
    replay = submit_result_decision(approver_client(change), ws, aid, digest=digest)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["decision_id"] == first.json()["decision_id"]
    assert result_footprint(setup, change)["completed_events"] == 1
    assert _verified_version(change) == 1


def test_r4_request_changes_on_a_stale_change_is_allowed_but_accept_is_not(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, aid = change["workspace"], change["apply_job_id"]
    digest = report_digest(change, setup)
    publish_with_chunk(setup["store"], setup["tenant"], ws, artifact_id="refresh-r4.yaml",
                       text="fresh: 1\n")
    assert change_dto(change["client"], ws, aid)["state"] == "STALE"
    accept = submit_result_decision(approver_client(change), ws, aid, digest=digest)
    assert accept.status_code == 409 and "report_stale" in accept.text
    fp = result_footprint(setup, change)
    resp = submit_result_decision(approver_client(change), ws, aid, action="request_changes",
                                  digest=digest, reason="source moved, please re-propose")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("new_proposal_job_id"), body
    assert job_row(setup["store"], setup["tenant"], ws, body["new_proposal_job_id"])["kind"] \
        == "proposal"
    after = result_footprint(setup, change)
    assert after["completed_events"] == 0 and after["audit"] == fp["audit"] + 1
    assert _verified_version(change) == 0


def test_r5_non_human_actor_cannot_record_work_proof(setup, monkeypatch, owner, tmp_path):
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path,
                                     before_apply=_add_manual_code_action(setup))
    ws, aid = change["workspace"], change["apply_job_id"]
    for kind in ("ai", "service"):
        with monkeypatch.context() as mp:
            patch_authority_kind(mp, kind)
            resp = submit_work_proof(approver_client(change), ws, aid)
        assert resp.status_code == 403 and "human" in resp.text, (kind, resp.text)
    assert change_dto(change["client"], ws, aid)["manual_work"][0]["proof"] is None
    # control: the human approver can
    assert submit_work_proof(approver_client(change), ws, aid).status_code == 200
