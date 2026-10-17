"""A decision recorded AFTER the knowledge version was accepted makes that version stale (the head moves to a later decision epoch and no knowledge version is current). The old
version cannot be published again, so the change would be stuck: the candidate for the CURRENT epoch is a new id that a person can accept, and then the result can be decided."""
from __future__ import annotations

from tests.ai8t04.conftest import (
    accept_candidate_km, approver_client, awaiting_change, change_dto, report_digest, submit_result_decision,
)


def test_a_decision_after_the_knowledge_was_accepted_needs_the_knowledge_accepted_again_and_then_the_result_can_be_decided(setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, store, t = change["workspace"], setup["store"], setup["tenant"]
    before = change_dto(change["client"], ws, change["apply_job_id"])
    assert before["knowledge"]["accepted"] and before["completion"]["eligible"]
    base = before["knowledge"]["candidate_revision_id"]
    with store._tx() as cur:
        cur.execute("SELECT execution_result FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction' AND execution_status='COMPLETED' AND execution_result IS NOT NULL", (t, ws))
        claims = [c["candidate_id"] for (r,) in cur.fetchall() for c in (r.get("candidates") or [])]
    assert owner["client"].post("/v2/knowledge/confirm", json={"workspace_id": ws, "claim_id": claims[-1], "rationale": "a person decides something else"}).status_code == 200
    after = change_dto(change["client"], ws, change["apply_job_id"])
    assert after["knowledge"]["accepted"] is False and "knowledge_not_accepted" in after["completion"]["blocked_reasons"]
    assert after["knowledge"]["candidate_revision_id"] == f"{base}-e{store.head(t, ws).decision_epoch}"      # the new candidate names the epoch it stands for
    refused = submit_result_decision(approver_client(change), ws, change["apply_job_id"], digest=report_digest(change, setup))
    assert refused.status_code == 409 and "knowledge_not_accepted" in refused.text                         # not decided on knowledge that is out of date
    accept_candidate_km(setup, owner, change)                                                              # a person accepts the knowledge of the CURRENT epoch
    again = change_dto(change["client"], ws, change["apply_job_id"])
    assert again["knowledge"]["accepted"] and again["completion"]["eligible"] and again["knowledge"]["retrieval_index_status"] == "READY"
    done = submit_result_decision(approver_client(change), ws, change["apply_job_id"], digest=report_digest(change, setup))
    assert done.status_code == 200, done.text


def test_the_new_readings_of_the_changed_file_get_their_checks_without_anyone_asking(setup, monkeypatch, owner, tmp_path):
    """After the apply the changed file is read again; the checks (relation, finding) for THAT reading are created by the system, so the result decision does not find the edited rule unchecked."""
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, store, t = change["workspace"], setup["store"], setup["tenant"]
    from infra import v2_current_findings as CF
    with store._tx() as cur:
        m = CF.build(cur, t, ws)
        valid = [cid for cid, info in m.claims.items() if info["valid"]]
        assert valid and not [c for c in m.unchecked if c in set(valid) and m.claims[c]["artifact"] == change["target"]], "readings of the changed file were left unchecked"
