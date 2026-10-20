"""The judge profile (server side). A judge may prepare a confirmed request up to a proposal that WAITS for a reviewer/approver, and nothing else: no source import, no analysis /
extraction / re-extraction, no relation/finding/gap.answer jobs, no review/approval bundle, no apply, no membership or operator control. Enforced at the generic job route and the
direct routes, not only in the UI. Real caller, stub model."""
from __future__ import annotations

import pytest

from infra.adapters.v2_policy import ROLE_PERMISSIONS, permissions_for
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import create_job
from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal
from tests.pitch_brief.test_role_model import member


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_the_judge_bundle_is_exactly_the_prepare_up_to_proposal_set():
    p = permissions_for(("judge",))
    assert p == {"project.read", "intent.create", "intent.confirm_own", "ai.use", "change.prepare", "request.assistant.ask"}   # + asking the read-only request assistant (RQA)
    for forbidden in ("source.import", "change.apply", "change.approve", "change.review", "change.accept_result", "evidence.read", "membership.manage",
                      "knowledge.confirm", "knowledge.answer", "conflict.resolve", "source.restore", "verification.contract.set"):
        assert forbidden not in p
    assert "change.prepare" in permissions_for(("requester",)) and "change.prepare" in permissions_for(("admin",))     # nobody who could prepare before loses it
    assert "change.prepare" not in permissions_for(("viewer",)) and "change.prepare" not in permissions_for(("reviewer",))


def test_the_judge_is_refused_every_job_kind_outside_the_profile_and_is_not_refused_the_pipeline_kinds(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)                         # the API setup of a proposal by the requester
    j = member(env, "judge")
    art = next(iter(ch.arts.values()))
    denied = {"extraction": {"artifact_id": art}, "analysis": {"artifact_id": art}, "relation": {}, "finding": {}, "invalidation": {},
              "impact.judge": {}, "gap.answer": {}, "review": {"proposal_job_id": c.proposal}, "approval": {"proposal_job_id": c.proposal, "review_job_id": "x"},
              "apply": {"approval_job_id": "x", "expected_approval_digest": "0" * 64}, "restore": {}}
    for kind, payload in denied.items():
        _jid, r = create_job(j, env, kind, payload)
        assert r.status_code in (400, 403, 409), (kind, r.status_code)                                  # shape errors may come first, but never a created job
        assert r.status_code != 200, kind
    # the same kinds with a valid shape are refused for permission: the review of the real proposal
    _jid, r = create_job(j, env, "review", {"proposal_job_id": c.proposal})
    assert r.status_code == 403 and "permission" in r.text.lower()
    _jid, r = create_job(j, env, "apply", {"approval_job_id": "job-approval-nope", "expected_approval_digest": "0" * 64})
    assert r.status_code in (403, 404, 409) and r.status_code != 200
    # a pipeline kind is not refused for permission (a valid proposal over the existing reports)
    _jid, r = create_job(j, env, "proposal", {"intent_id": c.intent, "impact_job_id": c.impact, "simulation_job_id": c.simulation,
                                              "target_artifact_id": next(a for p, a in ch.arts.items() if p.endswith("approval.yaml")), "edit_kind": "yaml_typed_edit",
                                              "draft_job_id": c.draft})
    assert r.status_code != 403, r.text


def test_the_judge_cannot_import_or_change_operations_through_the_direct_routes(env, ch):
    j = member(env, "judge")
    for method, path, body in (("POST", "/v2/sources/import/prepare", {"workspace_id": env.ws}), ("POST", "/v2/sources/import/commit", {"workspace_id": env.ws}),
                               ("POST", "/v2/operator/flags", {"workspace_id": env.ws, "key": "ai_switch", "value": "on", "reason": "judge must not"})):
        r = j.request(method, path, json=body)
        assert r.status_code in (400, 403, 404, 405), (path, r.status_code)
        assert r.status_code != 200, path
    r = j.post("/v2/operator/flags", json={"workspace_id": env.ws, "key": "ai_switch", "value": "on", "reason": "judge must not do this"})
    assert r.status_code == 403
    assert j.get("/v2/work/inbox", params={"workspace_id": env.ws}).status_code == 200                  # reading and its own work list still work


def test_the_deterministic_coverage_check_is_allowed_for_a_judge_and_needs_no_ai(env, ch):
    j = member(env, "judge")
    r = j.post("/v2/change/coverage/ensure", json={"workspace_id": env.ws})
    assert r.status_code == 200, r.text                                        # computed over the stored extraction output; no provider is involved
    assert member(env, "viewer").post("/v2/change/coverage/ensure", json={"workspace_id": env.ws}).status_code == 403


def test_a_judge_walks_request_confirm_draft_impact_simulation_ai_reading_and_proposal_and_stops_there(env, ch, monkeypatch, tmp_path):
    """The whole judge path with the judge's own client (not the requester's): every step that the guide names is allowed, and the first step beyond the proposal is not."""
    from infra.worker import explain
    from tests.pitch_brief.chain import APPROVAL, S1, cand_id
    from tests.pitch_brief.conftest import drain, job_body
    from tests.pitch_brief.test_change_draft import Model, _res, run
    from tests.pitch_brief.test_sim_analysis import _get, _post
    from tests.pitch_brief.test_sim_analysis_budget import FlakyThenOk, STD
    j = member(env, "judge")
    env.req_client = lambda: j                                                   # every helper below now acts as the judge
    jid, body = run(env, ch, monkeypatch, Model())                              # intent parse + confirm_own are done by the admin helper; checked separately below
    d = _res(body)
    iid = d["intent"]["intent_id"]
    parsed = j.post("/v2/intent/parse", json={"workspace_id": env.ws, "structured_form": S1})
    assert parsed.status_code == 200, parsed.text                               # free-text-equivalent request creation by the judge
    assert j.post("/v2/intent/confirm", json={"workspace_id": env.ws, "intent_id": parsed.json()["intent_id"]}).status_code == 200      # confirms THEIR OWN request
    assert j.post("/v2/intent/confirm", json={"workspace_id": env.ws, "intent_id": iid}).status_code in (403, 409)                       # not somebody else's (already confirmed or foreign)
    tid_ = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    ij, r = create_job(j, env, "impact", {"intent_id": iid, "candidate_ids": [tid_], "target_candidate_id": tid_, "scope_artifact_ids": sorted(ch.arts.values())})
    assert r.status_code == 200, r.text
    sj, r = create_job(j, env, "simulation", {"adapter": "harbor_cli", "case": "route", "edits": [{"path": STD, "value": 500000, "expected_current": 1000000}]})
    assert r.status_code == 200, r.text
    drain(env)
    from tests.pitch_brief.test_sim_analysis_budget import _cap_config
    _cap_config(tmp_path, monkeypatch, "100.00")
    stub = FlakyThenOk()
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    monkeypatch.setattr(explain, "_client_for_model", lambda m: stub)
    made = _post(j, env, sj)                                                    # the AI reading of the simulation
    assert made["status"] == "running"
    import time
    for _ in range(60):
        drain(env)
        if _get(j, env, sj)["status"] == "done":
            break
        time.sleep(1)
    assert _get(j, env, sj)["status"] == "done"
    pj, r = create_job(j, env, "proposal", {"intent_id": iid, "impact_job_id": ij, "simulation_job_id": sj, "target_artifact_id": ch.arts[APPROVAL],
                                            "edit_kind": "yaml_typed_edit", "draft_job_id": jid})
    assert r.status_code == 200, r.text
    drain(env)
    assert job_body(ch.admin, env, pj)["execution_status"] == "COMPLETED"
    # the first step beyond the proposal is refused, and so is every sibling the guide does not use
    _x, r = create_job(j, env, "review", {"proposal_job_id": pj})
    assert r.status_code == 403
    for kind, payload in (("extraction", {"artifact_id": next(iter(ch.arts.values()))}), ("analysis", {"artifact_id": next(iter(ch.arts.values()))})):
        _x, r = create_job(j, env, kind, payload)
        assert r.status_code == 403, kind
    assert j.get("/v2/work/inbox", params={"workspace_id": env.ws}).status_code == 200
