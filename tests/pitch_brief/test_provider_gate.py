"""A provider-backed action is refused EARLY and in plain words when the provider is off or its allocation is used up: nothing is created,
nothing is charged, and the answer carries a code and a next action the page shows. Deterministic actions and reads are not affected."""
from __future__ import annotations

import pytest

from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import create_job
from tests.pitch_brief.test_live_guards import _ceiling
from tests.pitch_brief.test_sim_analysis import _get
from tests.pitch_brief.test_sim_analysis_budget import _simulation


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _jobs(env):
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s", (env.tenant, env.ws))
        return cur.fetchone()[0]


def test_disarmed_provider_refuses_ai_actions_before_creating_anything_and_still_allows_reads_and_deterministic_jobs(env, ch, monkeypatch):
    sim = _simulation(env, ch, 500000)                                  # a deterministic job, made while armed/stub
    req = env.req_client()
    before = _jobs(env)
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    for kind, payload in (("extraction", {"artifact_id": next(iter(ch.arts.values())), "source_role": "unknown"}),
                          ("change.draft", {"intent_id": "intent-x"}), ("gap.answer", {"question": "q", "question_id": "q1", "artifact_ids": [next(iter(ch.arts.values()))]})):
        jid, r = create_job(req, env, kind, payload)
        body = r.json()
        assert r.status_code == 409 and body["code"] == "provider_disarmed", (kind, r.text)
        assert "switched off" in body["error"] and "nothing was charged" in body["next_action"].lower()
    refused = req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sim})
    assert refused.status_code == 409 and refused.json()["code"] == "provider_disarmed"
    assert _jobs(env) == before, "no job row was created by a refused action"
    assert _get(req, env, sim)["status"] == "none", "reading a stored/absent result still works"
    ok = create_job(req, env, "simulation", {"adapter": "harbor_cli", "case": "route",
                                             "edits": [{"path": "routing_rules.standard_manager_threshold_jpy", "value": 600000, "expected_current": 1000000}]})
    assert ok[1].status_code == 200, "a deterministic job is not an AI action"


def test_a_used_up_allocation_is_explained_with_a_code_and_keeps_the_exact_count(env, ch, monkeypatch):
    sims = [_simulation(env, ch, v) for v in (500000, 600000)]
    _ceiling(env, "sim.analysis", 1)
    req = env.req_client()
    assert req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[0]}).status_code in (200, 409)
    r = req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[1]})
    body = r.json()
    assert r.status_code == 409 and body["code"] == "allocation_exhausted"
    assert "allocation" in body["error"] and "new allocation" in body["next_action"] and "authorized for 1 job" in body["detail"]


def test_the_page_is_told_the_provider_is_off(env, ch, monkeypatch):
    on = env.admin_client().get("/app-next/")
    assert 'name="ripple-provider"' not in on.text
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    off = env.admin_client().get("/app-next/")
    assert 'name="ripple-provider" content="LIVE_DISARMED"' in off.text


def test_chat_can_be_started_but_asking_the_model_is_refused_while_the_provider_is_off(env, ch, monkeypatch):
    from tests.pitch_brief.test_intent_chat import start
    req, adm = env.req_client(), env.admin_client()
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    c = start(env, req)                                              # recording the person's own words calls no model and creates no job
    assert c["awaiting_ai"] is False
    before = _jobs(env)
    r = req.post(f"/v2/chat/{c['conversation_id']}/allow", json={"workspace_id": env.ws, "turns": 1})      # "Ask the AI" by the owner
    assert r.status_code == 409 and r.json()["code"] == "provider_disarmed", r.text
    assert _jobs(env) == before
    again = req.get(f"/v2/chat/{c['conversation_id']}", params={"workspace_id": env.ws}).json()
    assert not again.get("ai_approved_by"), "nothing was granted by the refused request"
