"""B1: change.draft -> proposal through the real routes/worker/ProposalExecutor. The caller names the draft, the
target artifact and (optionally) which proposed edits to accept; it never types path/current/new."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from infra.worker import change_draft_binding as binding
from tests.pitch_brief.chain import APPROVAL, S1, build, cand_id, intent
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_change_draft import Model, run, _res

Y = "routing_rules.standard_manager_threshold_jpy"


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _reports(env, ch, intent_id):
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    req = env.req_client()
    ij, r = create_job(req, env, "impact", {"intent_id": intent_id, "candidate_ids": [tid], "target_candidate_id": tid,
                                            "scope_artifact_ids": sorted(ch.arts.values())})
    assert r.status_code == 200, r.text
    sj, r = create_job(req, env, "simulation", {})
    assert r.status_code == 200, r.text
    drain(env)
    return ij, sj


def _draft(env, ch, monkeypatch):
    jid, body = run(env, ch, monkeypatch, Model())
    r = _res(body)
    return jid, r


def _proposal(env, ch, payload):
    jid, r = create_job(env.req_client(), env, "proposal", payload)
    return jid, r


def test_draft_materializes_proposal_without_typed_path_current_new(env, ch, monkeypatch):
    djid, d = _draft(env, ch, monkeypatch)
    iid = d["intent"]["intent_id"]
    ij, sj = _reports(env, ch, iid)
    target = ch.arts[APPROVAL]
    pj, r = _proposal(env, ch, {"intent_id": iid, "impact_job_id": ij, "simulation_job_id": sj,
                                "target_artifact_id": target, "edit_kind": "yaml_typed_edit", "draft_job_id": djid})
    assert r.status_code == 200, r.text
    drain(env)
    p = _res(job_body(ch.admin, env, pj))
    assert [(e["path"], e["before"], e["after"]) for e in p["edits"]] == [(Y, 1000000, 500000)]
    assert p["is_no_op"] is False and p["staged_blob_hash"] and p["after_blob_hash"] != p["before_blob_hash"]
    b = p["draft_binding"]
    assert b["draft_job_id"] == djid and b["draft_hash"] == d["draft_hash"] and b["citations"]
    assert b["left_for_humans"], "manual/ambiguous/unaddressed draft items stay visible for the human"


def test_caller_typed_edits_with_draft_rejected_at_route(env, ch, monkeypatch):
    djid, d = _draft(env, ch, monkeypatch)
    iid = d["intent"]["intent_id"]
    ij, sj = _reports(env, ch, iid)
    _, r = _proposal(env, ch, {"intent_id": iid, "impact_job_id": ij, "simulation_job_id": sj,
                               "target_artifact_id": ch.arts[APPROVAL], "edit_kind": "yaml_typed_edit",
                               "draft_job_id": djid, "edits": [{"path": Y, "expected_current": 1, "proposed": 2}]})
    assert r.status_code == 400 and "edits must not be supplied" in r.text


def test_wrong_target_or_unknown_ref_or_unfinished_draft_fail_durably(env, ch, monkeypatch):
    djid, d = _draft(env, ch, monkeypatch)
    iid = d["intent"]["intent_id"]
    ij, sj = _reports(env, ch, iid)
    base = {"intent_id": iid, "impact_job_id": ij, "simulation_job_id": sj, "edit_kind": "yaml_typed_edit"}
    cases = [({"target_artifact_id": next(a for p, a in ch.arts.items() if p.endswith("workflow.yaml")),
               "draft_job_id": djid}, "draft_has_no_proposed_edit_for_target"),
             ({"target_artifact_id": ch.arts[APPROVAL], "draft_job_id": djid, "accepted_refs": ["E999"]},
              "accepted_ref_not_a_proposed_edit"),
             ({"target_artifact_id": ch.arts[APPROVAL], "draft_job_id": ij}, "draft_not_completed")]
    for extra, reason in cases:
        pj, r = _proposal(env, ch, {**base, **extra})
        assert r.status_code == 200, r.text
        drain(env)
        body = job_body(ch.admin, env, pj)
        assert body["execution_status"] == "FAILED" and reason in str(body["execution_reason"]), (reason, body)
        assert body["execution_result"] is None


def test_unit_stale_tamper_and_intent_guards():
    res = {"draft_hash": "x", "pinned_input": {"pinned_source_revision_id": "R1"}, "intent": {"intent_id": "I"},
           "edits": [], "items": [], "unaddressed": []}
    res["draft_hash"] = binding._hash(res)
    rec = {"kind": "change.draft", "execution_status": "COMPLETED", "execution_result": res}
    ctx = lambda rev: SimpleNamespace(job={"tenant_id": "t", "workspace_id": "w", "pinned_source_revision_id": rev},
                                       store=SimpleNamespace(job_record=lambda t, w, j: rec))
    pay = {"draft_job_id": "D", "intent_id": "I", "target_artifact_id": "A"}
    with pytest.raises(binding.NonRetryableExecutionError, match="draft_stale"):
        binding.resolve_draft_edits(ctx("R2"), pay)
    with pytest.raises(binding.NonRetryableExecutionError, match="draft_intent_mismatch"):
        binding.resolve_draft_edits(ctx("R1"), {**pay, "intent_id": "other"})
    res["summary"] = "tampered"
    with pytest.raises(binding.NonRetryableExecutionError, match="draft_hash_mismatch"):
        binding.resolve_draft_edits(ctx("R1"), pay)


# ----------------------------------------------------------------------------- B2: contract -> review -> approval -> apply
def _grant(env, role):
    from uuid import uuid4
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.pitch_brief.conftest import DSN
    from fastapi.testclient import TestClient
    from infra import judge
    ids = PostgresIdentityStore(DSN)
    acct, cred = f"{role[:3]}-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}"
    ids.create_account(acct, cred)
    project = env.tenant.replace("t-", "p-")
    ids.grant_membership(env.tenant, project, env.ws, acct, (role,))
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": acct, "credential": cred}).status_code == 200
    return c


def _job(env, client, kind, payload):
    jid, r = create_job(client, env, kind, payload)
    return jid, r


def chain_to_proposal(env, ch, monkeypatch):
    djid, d = _draft(env, ch, monkeypatch)
    iid = d["intent"]["intent_id"]
    ij, sj = _reports(env, ch, iid)
    req = env.req_client()
    pj, r = create_job(req, env, "proposal", {"intent_id": iid, "impact_job_id": ij, "simulation_job_id": sj,
                                              "target_artifact_id": ch.arts[APPROVAL], "edit_kind": "yaml_typed_edit",
                                              "draft_job_id": djid})
    assert r.status_code == 200, r.text
    drain(env)
    _res(job_body(ch.admin, env, pj))
    return SimpleNamespace(req=req, proposal=pj, intent=iid, draft=djid, impact=ij, simulation=sj)


def post_contract(env, client, proposal, expected):
    # the operator states the expected value from the confirmed REQUEST, never from the proposal or the model
    return client.post("/v2/verification/contracts", json={
        "workspace_id": env.ws, "proposal_job_id": proposal, "kind": "yaml_value_equals", "logical_path": APPROVAL,
        "locator": Y, "expected": expected,
        "provenance": {"execution": "automated approval simulation", "human_acceptance": "NOT_RUN"}})


def test_review_without_contract_is_blocked_not_bypassed(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    rj, r = create_job(c.req, env, "review", {"proposal_job_id": c.proposal})
    assert r.status_code == 200, r.text
    drain(env)
    res = _res(job_body(ch.admin, env, rj))
    assert res["readiness"] == "BLOCKED"
    assert res["checks"]["acceptance_tests"]["status"] != "PASS"


def _to_approval(env, ch, c, expected=500000):
    op = _grant(env, "change_approver")
    rc = post_contract(env, op, c.proposal, expected)
    assert rc.status_code == 200, rc.text
    rj, r = create_job(c.req, env, "review", {"proposal_job_id": c.proposal,
                                              "verification_contract_id": rc.json()["contract_id"]})
    assert r.status_code == 200, r.text
    drain(env)
    review = _res(job_body(ch.admin, env, rj))
    aj, r = create_job(c.req, env, "approval", {"proposal_job_id": c.proposal, "review_job_id": rj})
    assert r.status_code == 200, r.text
    drain(env)
    appr = _res(job_body(ch.admin, env, aj))
    return op, review, aj, appr


def test_full_yaml_chain_apply_readback_and_verification(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, review, aj, appr = _to_approval(env, ch, c)
    assert review["readiness"] == "READY_FOR_HUMAN_REVIEW", review["checks"]
    digest = appr["approval_digest"]
    # the REQUESTER cannot decide its own change
    dec = {"workspace_id": env.ws, "action": "approve", "reason": "ok", "expected_approval_digest": digest}
    assert c.req.post(f"/v2/jobs/{aj}/decision", json=dec).status_code in (403, 409)
    approver = _grant(env, "change_approver")
    assert approver.post(f"/v2/jobs/{aj}/decision", json={**dec, "expected_approval_digest": "0" * 64}).status_code == 409
    rd = approver.post(f"/v2/jobs/{aj}/decision", json=dec)
    assert rd.status_code == 200, rd.text
    apj, r = create_job(c.req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": digest})
    assert r.status_code == 200, r.text
    drain(env)
    ap = job_body(ch.admin, env, apj)
    assert ap["execution_status"] == "COMPLETED", ap
    # readback: the published revision holds exactly the new value, the sibling sources are untouched
    head = env.store.head(env.tenant, env.ws)
    blob = env.store.read_artifact_blob(env.tenant, env.ws, head.source_revision_id, ch.arts[APPROVAL])["content"]
    import yaml
    assert yaml.safe_load(blob)["routing_rules"]["standard_manager_threshold_jpy"] == 500000
    drain(env)
    v = job_body(ch.admin, env, f"verify-{apj}")
    assert v["execution_status"] == "COMPLETED" and v["execution_result"]["status"] == "PASSED", v
    # duplicate apply must not publish a second time
    apj2, r2 = create_job(c.req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": digest})
    drain(env)
    again = job_body(ch.admin, env, apj2) if r2.status_code == 200 else None
    assert r2.status_code != 200 or again["execution_status"] != "COMPLETED"
    assert env.store.head(env.tenant, env.ws).source_revision_id == head.source_revision_id


def test_wrong_expected_value_in_contract_blocks_review_and_apply(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, review, aj, appr = _to_approval(env, ch, c, expected=999)
    assert review["readiness"] == "BLOCKED"
    approver = _grant(env, "change_approver")
    r = approver.post(f"/v2/jobs/{aj}/decision", json={"workspace_id": env.ws, "action": "approve", "reason": "try",
                                                      "expected_approval_digest": appr["approval_digest"]})
    assert r.status_code == 409
    head0 = env.store.head(env.tenant, env.ws).source_revision_id
    apj, r = create_job(c.req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
    drain(env)
    body = job_body(ch.admin, env, apj) if r.status_code == 200 else {"execution_status": "REJECTED"}
    assert body["execution_status"] in ("FAILED", "REJECTED")
    assert env.store.head(env.tenant, env.ws).source_revision_id == head0


def test_contract_author_must_be_distinct_from_requester(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    r = post_contract(env, c.req, c.proposal, 500000)       # requester tries to author its own oracle
    assert r.status_code in (403, 409)


def test_accepted_refs_selects_which_proposed_edit_becomes_the_proposal(env, ch, monkeypatch):
    djid, d = _draft(env, ch, monkeypatch)
    ref = d["edits"][0]["ref"]
    iid = d["intent"]["intent_id"]
    ij, sj = _reports(env, ch, iid)
    pj, r = _proposal(env, ch, {"intent_id": iid, "impact_job_id": ij, "simulation_job_id": sj,
                                "target_artifact_id": ch.arts[APPROVAL], "edit_kind": "yaml_typed_edit",
                                "draft_job_id": djid, "accepted_refs": [ref]})
    assert r.status_code == 200, r.text
    drain(env)
    p = _res(job_body(ch.admin, env, pj))
    assert p["draft_binding"]["accepted_refs"] == [ref] and len(p["edits"]) == 1
