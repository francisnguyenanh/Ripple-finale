"""CHG-02 / R06 (F-07 c): the numeric change of the business flow is MEASURED through the existing Harbor simulation when the draft's automatic YAML edit is a key the adapter
supports; everything else stays NOT_SIMULATED with a reason. Real routes, real deterministic job through the worker (0 provider calls; the pinned public Harbor copy runs in the
resource-limited runner), real store. No model is involved in the measurement; the understanding step uses the STUB model as in every CHG test.
"""
from __future__ import annotations

import time

import pytest

from infra.adapters import v2_business_simulation as sim
from infra.worker import harbor_sim
from tests.final_loop.test_chg01_change_request import ACTOR_REQ, NUM_REQ, OP_ACTOR, OP_VALUE
from tests.final_loop.test_chg02_impact import confirmed, impact
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import drain

KEY = "routing_rules.standard_manager_threshold_jpy"


def simulate(q, client, cid):
    return client.post(f"/v2/business/changes/{cid}/impact/simulate", json={"workspace_id": q.env.ws})


def settle(q, client, cid, mode="technical", tries=80):
    for _ in range(tries):
        drain(q.env)
        got = impact(q, client, cid, mode).json()
        if got["simulation_state"]["status"] in ("completed", "failed", "partial"):
            return got
        time.sleep(0.5)
    return got


# ---- pure planning over a draft ----------------------------------------------------------------------------------------------------------------

def test_plan_measures_only_a_supported_automatic_yaml_edit_and_explains_every_other_operation():
    draft = {"operations": [{"op_id": "op-1", "kind": "set_value"}, {"op_id": "op-2", "kind": "set_actor"}, {"op_id": "op-3", "kind": "set_value"}, {"op_id": "op-4", "kind": "set_value"}],
             "file_actions": [{"op_ids": ["op-1"], "action": "AI_EDIT_SUPPORTED", "resolved_artifact_id": "cfg", "path": "approval.yaml", "proposal_edit": {"key_path": KEY, "expected_current": 1000000, "proposed": 1500000}},
                              {"op_ids": ["op-3"], "action": "AI_EDIT_SUPPORTED", "resolved_artifact_id": "wf", "path": "workflow.yaml", "proposal_edit": {"key_path": "steps.max", "expected_current": 3, "proposed": 4}},
                              {"op_ids": ["op-4"], "action": "MANUAL", "reason": "stored_value_differs_from_confirmed_before_value", "resolved_artifact_id": "cfg", "path": "approval.yaml", "proposal_edit": None}]}
    p = sim.plan(draft, {"cfg": "harbor_demo/config/approval.yaml", "wf": "harbor_demo/config/workflow.yaml"})
    assert p["families"] == {"route": [{"path": KEY, "value": 1500000, "expected_current": 1000000, "op_id": "op-1"}]}
    assert p["ops"]["op-1"] == {"case": "route", "reason": None}
    assert "set_actor" in p["ops"]["op-2"]["reason"] and "not the Harbor approval configuration" in p["ops"]["op-3"]["reason"] and "manual" in p["ops"]["op-4"]["reason"]
    unsupported_key = {"operations": [{"op_id": "op-1", "kind": "set_value"}], "file_actions": [{"op_ids": ["op-1"], "action": "AI_EDIT_SUPPORTED", "resolved_artifact_id": "cfg", "path": "approval.yaml",
                                                                                                    "proposal_edit": {"key_path": "routing_rules.high_risk_route", "expected_current": 1, "proposed": 2}}]}
    assert "not one the Harbor simulation supports" in sim.plan(unsupported_key, {"cfg": "x/config/approval.yaml"})["ops"]["op-1"]["reason"]
    assert sim.plan(draft, {})["families"] == {} and "not the Harbor approval configuration" in sim.plan(draft, {})["ops"]["op-1"]["reason"]      # an unreadable revision never measures


# ---- real route, worker and report -------------------------------------------------------------------------------------------------------------

def test_a_supported_numeric_change_is_measured_on_the_public_harbor_sample_and_the_report_keeps_the_claim_limited(qa_env):
    q = qa_env
    cid, model = confirmed(q, NUM_REQ, [OP_VALUE])
    before = impact(q, q.admin, cid, "technical").json()
    assert before["simulation"][0]["status"] == "NOT_SIMULATED" and "not requested yet" in before["simulation"][0]["reason"] and before["measured"] == []
    assert before["simulation_state"] == {"status": "not_requested", "edit_hash": before["simulation_state"]["edit_hash"], "cases": {"route": {"status": "not_requested", "job_id": None, "reason": None}}, "measurable": True}
    knowledge_before = counts(q.env)
    r = simulate(q, q.req, cid)                                                                               # the requester asked for the change and may prepare it
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jobs"][0]["case"] == "route" and body["jobs"][0]["created"] is True and body["jobs"][0]["job_id"].startswith("bsim-") and body["not_simulated"] == {}
    again = simulate(q, q.req, cid)
    assert again.status_code == 200 and again.json()["jobs"][0]["created"] is False and again.json()["jobs"][0]["job_id"] == body["jobs"][0]["job_id"]   # one job per change and edit set
    got = settle(q, q.admin, cid)
    assert got["simulation_state"]["status"] == "completed", got["simulation_state"]
    assert got["simulation"][0]["status"] == "MEASURED" and model.calls == 1                                 # the measurement asked no model
    m = got["measured"][0]
    assert m["case"] == "route" and m["transactions"] > 0 and isinstance(m["changed_count"], int) and m["edits"] == [{"path": KEY, "from": 1000000, "to": 1500000, "no_op": False}]
    assert m["baseline_source"]["kind"] == "workspace_revision" and m["integrity"]["seed_equals_shipped"] is True
    assert m["by_route_before"] != m["by_route_after"] or m["changed_count"] == 0
    assert any("not a prediction" in l or "not a prediction" in m["statement"] for l in m["limits"] + [m["statement"]])
    assert any(n["key"] == KEY for n in m["not_controlled"]) and "forecast" in got["measured_statement"]      # the emergency path is hard-coded: said, not hidden
    assert counts(q.env) == knowledge_before                                                                  # measuring writes nothing to the knowledge
    plain = impact(q, q.req, cid, "business").json()
    pm = plain["measured"][0]
    assert "edits" not in pm and "config_hashes" not in pm and pm["changed_count"] == m["changed_count"] and pm["not_controlled"][0]["reason"]   # counts for everyone, keys/files for evidence readers
    rec = q.admin.get(f"/v2/jobs/{body['jobs'][0]['job_id']}", params={"workspace_id": q.env.ws}).json()
    assert rec["execution_provider_calls"] in (0, None) and rec["execution_result"]["adapter"] == "harbor_cli" and rec["execution_result"]["pinned_input"]
    review = q.admin.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws, "mode": "technical"}).json()
    assert review["impact"]["simulation"][0]["status"] == "MEASURED" and review["impact"]["measured"][0]["changed_count"] == m["changed_count"]


def test_a_nonnumeric_change_has_nothing_to_measure_and_says_why(qa_env):
    q = qa_env
    cid, _ = confirmed(q, ACTOR_REQ, [OP_ACTOR])
    r = simulate(q, q.req, cid)
    assert r.status_code == 409 and r.json()["code"] == "nothing_to_simulate" and "set_actor" in list(r.json()["reasons"].values())[0]
    got = impact(q, q.admin, cid, "technical").json()
    assert got["simulation"][0]["status"] == "NOT_SIMULATED" and got["simulation_state"] == {"status": "not_requested", "edit_hash": got["simulation_state"]["edit_hash"], "cases": {}, "measurable": False}
    assert got["measured"] == []


def test_a_failed_or_non_executed_run_claims_no_count(qa_env, monkeypatch):
    q = qa_env
    cid, _ = confirmed(q, NUM_REQ, [OP_VALUE])

    def broken(case, **kw):
        raise harbor_sim.HarborSimError("adapter_integrity_failed", "the seed config did not reproduce Harbor's shipped output")
    monkeypatch.setattr(harbor_sim, "simulate", broken)
    assert simulate(q, q.admin, cid).status_code == 200
    got = settle(q, q.admin, cid)
    assert got["simulation_state"]["status"] == "completed" and got["simulation"][0]["status"] == "NOT_SIMULATED" and "ADAPTER_INTEGRITY_FAILED" in got["simulation"][0]["reason"]
    assert got["measured"] == [] and "measured_statement" not in got


def test_permissions_and_preconditions_of_the_simulate_route(qa_env):
    q = qa_env
    from tests.final_loop.test_chg01_change_request import model_obj, understand
    open_req, _ = understand(q, NUM_REQ, model_obj(ops=[OP_VALUE]))
    assert simulate(q, q.req, open_req["change_id"]).status_code in (404, 409)                                 # not confirmed
    cid, _ = confirmed(q, NUM_REQ, [OP_VALUE])
    from infra.adapters.v2_identity import PostgresIdentityStore
    ids = PostgresIdentityStore(q.env.dsn)
    ids.create_account("viewer-s3-" + q.env.nonce, "cv")
    ids.grant_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, "viewer-s3-" + q.env.nonce, ("viewer",))
    from fastapi.testclient import TestClient
    from infra import judge
    v = TestClient(judge.app, base_url="https://judge.test")
    v.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert v.post("/v2/auth/login", json={"account_id": "viewer-s3-" + q.env.nonce, "credential": "cv"}).status_code == 200
    assert simulate(q, v, cid).status_code in (403, 404)
    anonymous = q.env.admin_client()
    anonymous.cookies.clear()
    assert simulate(q, anonymous, cid).status_code in (401, 403)
    assert q.admin.post("/v2/business/changes/not-a-change/impact/simulate", json={"workspace_id": q.env.ws}).status_code in (400, 404)
