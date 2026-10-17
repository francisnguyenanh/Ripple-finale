"""R2: the simulation is built by the SERVER from the stored draft (no client-supplied edits), runs every supported family,
lists what was not tested, and says NOT RUN instead of faking a result. Real pinned Harbor code; the store is a stand-in
that only serves the draft record (baseline = the fixture seed config)."""
from __future__ import annotations

from types import SimpleNamespace

from infra.worker import harbor_sim

STD = "routing_rules.standard_manager_threshold_jpy"
TERM = "matching_rules.payment_term_days"


def _edit(path, proposed, current, logical="config/approval.yaml"):
    return {"logical_path": logical, "candidate_ids": ["c1"], "proposal_edit": {"path": path, "proposed": proposed, "expected_current": current}}


def _ctx(draft):
    class Store:
        def job_record(self, tenant, ws, job_id):
            if draft is None or job_id != "d1":
                raise KeyError(job_id)
            return draft
    return SimpleNamespace(store=Store(), job={"tenant_id": "t", "workspace_id": "w", "pinned_source_revision_id": None})


def _run(edits, status="COMPLETED", kind="change.draft"):
    draft = {"kind": kind, "execution_status": status, "execution_result": {"edits": edits}}
    return harbor_sim.execute(_ctx(draft), {"adapter": "harbor_cli", "from_draft_job_id": "d1"}, {})


def test_forward_requested_direction_and_edit_hash_are_recorded():
    r = _run([_edit(STD, 500000, 1000000)])
    assert r["status"] == "EXECUTED" and r["direction"] == "forward_requested_change"
    assert r["harbor"]["edits"] == [{"path": STD, "from": 1000000, "to": 500000, "no_op": False}]
    assert r["harbor"]["changed_count"] > 0 and r["coverage"]["submitted"] == 240
    assert r["built_from"]["draft_job_id"] == "d1" and len(r["built_from"]["edit_hash"]) == 64


def test_both_families_are_simulated_not_a_silent_subset():
    r = _run([_edit(STD, 500000, 1000000), _edit(TERM, 45, None)])
    assert r["status"] == "EXECUTED" and sorted(f["case"] for f in r["families"]) == ["match", "route"]
    assert all(f["harbor"] for f in r["families"])


def test_unsupported_edits_are_listed_as_untested():
    r = _run([_edit(STD, 500000, 1000000), {"logical_path": "docs/policy.md", "candidate_ids": [], "proposal_edit": None}])
    assert r["status"] == "EXECUTED" and r["untested_edits"][0]["reason"] == "no_supported_simulation_for_this_edit"


def test_no_supported_edit_is_not_run_with_zero_cases_never_a_clean_result():
    r = _run([{"logical_path": "docs/policy.md", "candidate_ids": [], "proposal_edit": None}])
    assert r["status"] == "NOT_EXECUTABLE" and r["reason"] == "no_supported_edit_in_draft" and r["coverage"]["submitted"] == 0 and "harbor" not in r


def test_stale_baseline_is_reported_and_not_reused():
    r = _run([_edit(STD, 500000, 777)])
    assert r["status"] == "STALE_BASELINE" and "777" in r["reason"]


def test_missing_incomplete_or_wrong_kind_draft_is_not_run():
    assert harbor_sim.execute(_ctx(None), {"adapter": "harbor_cli", "from_draft_job_id": "d1"}, {})["reason"] == "draft_not_found_or_not_completed"
    assert _run([_edit(STD, 500000, 1000000)], status="FAILED")["status"] == "NOT_EXECUTABLE"
    assert _run([_edit(STD, 500000, 1000000)], kind="impact")["status"] == "NOT_EXECUTABLE"


def test_client_cannot_inject_edits_beside_the_draft_reference():
    assert harbor_sim.validate_payload({"adapter": "harbor_cli", "from_draft_job_id": "d1", "edits": []}) == "edits_and_case_are_built_by_the_server_from_the_draft"
    assert harbor_sim.validate_payload({"adapter": "harbor_cli", "from_draft_job_id": "d1", "case": "route"}) is not None
    assert harbor_sim.validate_payload({"adapter": "harbor_cli", "from_draft_job_id": ""}) is not None


import pytest
from tests.pitch_brief.chain import build


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


# ---- through the real job route (needs the disposable PostgreSQL the pitch_brief suite uses)
def test_route_rejects_client_edits_and_records_not_run_and_binds_the_proposal_to_its_draft(env, ch):
    from tests.pitch_brief.conftest import create_job, drain, job_body
    req = env.req_client()
    _, r = create_job(req, env, "simulation", {"adapter": "harbor_cli", "from_draft_job_id": "dA",
                                               "edits": [{"path": STD, "value": 1, "expected_current": 1}]})
    assert r.status_code == 400 and "built_by_the_server" in r.text
    sim_id, r = create_job(req, env, "simulation", {"adapter": "harbor_cli", "from_draft_job_id": "dA"})
    assert r.status_code == 200, r.text
    drain(env)
    body = job_body(ch.admin, env, sim_id)
    assert body["execution_status"] == "COMPLETED" and body["execution_result"]["status"] == "NOT_EXECUTABLE"
    assert body["execution_result"]["coverage"]["submitted"] == 0 and (body.get("execution_provider_calls") or 0) == 0
    rev = env.store.head(env.tenant, env.ws).source_revision_id
    art = next(a["artifact_id"] for a in env.store.source_revision_view(env.tenant, env.ws, rev)["artifacts"]
               if a["logical_path"].endswith("config/approval.yaml"))
    _, r = create_job(req, env, "proposal", {"intent_id": "i", "impact_job_id": "im", "simulation_job_id": sim_id,
                                             "target_artifact_id": art, "edit_kind": "yaml_typed_edit", "draft_job_id": "dB"})
    assert r.status_code == 400 and "different draft" in r.text, r.text
