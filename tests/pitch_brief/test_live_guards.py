"""Guards that must hold BEFORE any live provider call of the review trial: the provider request cap vs the reservation ceiling, the durable
job ceilings (survive settlement, restart and duplicate submission, never raised silently), and the simulation bound to the revision of its
draft. Stub provider only."""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from infra.adapters.v2_postgres import JobCeilingExceeded, PostgresV2Store
from infra.worker import budget_config, job_quota
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import DSN, REAL_BUDGET_PATH, create_job, drain, job_body
from tests.pitch_brief.test_sim_analysis import STD, _get, _post
from tests.pitch_brief.test_sim_analysis_budget import FlakyThenOk, _cap_config, _simulation

import yaml


def _config_with(tmp_path, **over):
    raw = yaml.safe_load(REAL_BUDGET_PATH.read_text(encoding="utf-8"))
    raw.update(over)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_generation_cap_is_what_the_provider_is_asked_and_the_reservation_keeps_the_ceiling(tmp_path):
    cfg = job_quota.load_worker_budget_config(str(_config_with(tmp_path, generation_max_output_tokens=6144)))
    assert cfg.max_output_tokens == 12288 and cfg.generation_max_output_tokens == 6144
    from infra.worker.execution_types import ExecutionContext
    ctx = ExecutionContext(store=None, job={}, config=cfg)
    assert ctx.max_output_tokens == 6144, "the request carries the generation cap"
    reservation, _ = job_quota.compute_budget_reservation(cfg)
    assert reservation == Decimal("0.25632"), "the reservation still covers 12288 output+thinking per attempt (a 2x overshoot of 6144)"


def test_a_generation_cap_whose_double_exceeds_the_reservation_ceiling_is_refused(tmp_path):
    with pytest.raises(Exception) as e:
        job_quota.load_worker_budget_config(str(_config_with(tmp_path, generation_max_output_tokens=8192)))
    assert "generation_max_output_tokens" in str(e.value)
    default = job_quota.load_worker_budget_config(str(_config_with(tmp_path)))
    assert default.generation_max_output_tokens is None


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _ceiling(env, kind, n, label="trial"):
    with env.store._tx() as cur:
        cur.execute("INSERT INTO v2_job_ceilings (tenant_id, workspace_id, kind, label, max_total) VALUES (%s,%s,%s,%s,%s)",
                    (env.tenant, env.ws, kind, label, n))


def test_the_job_ceiling_survives_settlement_restart_and_duplicates_and_cannot_be_edited(env, ch, tmp_path, monkeypatch):
    from infra.worker import explain
    sims = [_simulation(env, ch, v) for v in (500000, 600000, 700000)]
    _cap_config(tmp_path, monkeypatch, "100.00")
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    stub = FlakyThenOk()
    monkeypatch.setattr(explain, "_client_for_model", lambda m: stub)
    _ceiling(env, "sim.analysis", 2)
    req = env.req_client()
    a = _post(req, env, sims[0])
    assert _post(req, env, sims[0])["job_id"] == a["job_id"], "a duplicate submission is the same job and does not use a second authorization"
    b = _post(req, env, sims[1])
    refused = req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[2]})
    assert refused.status_code == 409 and "authorized for 2 job" in refused.text
    for made in (a, b):                                           # admitted by policy at creation; run both to completion: reservations are settled and released
        assert made["status"] == "running"
    import time
    for _ in range(60):
        drain(env)
        if all(_get(req, env, s)["status"] == "done" for s in sims[:2]):
            break
        time.sleep(1)
    assert stub.calls == 4
    again = req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[2]})
    assert again.status_code == 409, "the released reservation does NOT open a third job"
    fresh = PostgresV2Store(DSN, env.blobs)                       # a restart: a new store object on the same database
    with pytest.raises(JobCeilingExceeded):
        fresh.create_job(env.tenant, env.ws, "job-third-by-hand", "sim.analysis", {"facts": {"a": 1}, "snapshot": {}},
                         expected_version=fresh.head(env.tenant, env.ws).version, idempotency_key="job-create-job-third-by-hand")
    for sql in ("UPDATE v2_job_ceilings SET max_total=99 WHERE tenant_id=%s", "DELETE FROM v2_job_ceilings WHERE tenant_id=%s"):
        with pytest.raises(Exception):
            with env.store._tx() as cur:
                cur.execute(sql, (env.tenant,))
    _ceiling(env, "sim.analysis", 9, label="a-later-looser-one")  # another row cannot loosen it: the smallest applies
    assert req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[2]}).status_code == 409


def test_a_failed_job_still_uses_its_authorization(env, ch, tmp_path, monkeypatch):
    sims = [_simulation(env, ch, v) for v in (500000, 600000)]
    _cap_config(tmp_path, monkeypatch, "100.00")
    _ceiling(env, "sim.analysis", 1)
    req = env.req_client()
    first = _post(req, env, sims[0])
    assert env.admin_client().post(f"/v2/jobs/{first['job_id']}/cancel", json={"workspace_id": env.ws}).status_code in (200, 404, 405, 409)
    assert req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[1]}).status_code == 409


def test_the_simulation_reads_the_revision_the_draft_was_made_on_and_records_it():
    from infra.worker import harbor_sim
    seen = []

    class Store:
        def job_record(self, tenant, ws, job_id):
            return {"kind": "change.draft", "execution_status": "COMPLETED", "execution_result": {
                "pinned_input": {"pinned_source_revision_id": "rev-draft"},
                "edits": [{"logical_path": "config/approval.yaml", "candidate_ids": ["c"],
                           "proposal_edit": {"path": STD, "proposed": 500000, "expected_current": 1000000}}]}}

        def source_revision_view(self, tenant, ws, rev):
            seen.append(rev)
            return {"artifacts": []}                               # no approval.yaml in it: the public seed config is the baseline
    ctx = SimpleNamespace(store=Store(), job={"tenant_id": "t", "workspace_id": "w", "pinned_source_revision_id": "rev-head-now"})
    r = harbor_sim.execute(ctx, {"adapter": "harbor_cli", "from_draft_job_id": "d"}, {})
    assert seen == ["rev-draft"], "baseline = the draft's own revision, not the head"
    assert r["built_from"]["baseline_source_revision_id"] == "rev-draft" and r["built_from"]["head_source_revision_id_at_run"] == "rev-head-now"
    assert r["status"] == "EXECUTED" and r["direction"] == "forward_requested_change"
    assert r["harbor"]["edits"][0]["from"] == 1000000 and r["harbor"]["edits"][0]["to"] == 500000


def test_a_proposal_cannot_rest_on_a_simulation_run_for_an_older_revision(env, ch, monkeypatch):
    """The simulation tests the draft on the revision the draft was made on. After an apply moved the head, that result is history: it
    cannot back a NEW proposal, and a newer-revision simulation must be run (server check, not a UI hint)."""
    from infra.worker import extraction
    from tests.pitch_brief.test_draft_to_proposal import _grant, _to_approval, chain_to_proposal
    from tests.pitch_brief.test_loop_feedback import PostApplyExtraction
    c = chain_to_proposal(env, ch, monkeypatch)
    req = env.req_client()
    sd, r = create_job(req, env, "simulation", {"adapter": "harbor_cli", "from_draft_job_id": c.draft})
    assert r.status_code == 200, r.text
    drain(env)
    res = job_body(ch.admin, env, sd)["execution_result"]
    assert res["status"] == "EXECUTED" and res["built_from"]["baseline_source_revision_id"] == env.store.head(env.tenant, env.ws).source_revision_id
    prev = job_body(ch.admin, env, c.proposal)["payload"]
    keys = ("intent_id", "impact_job_id", "target_artifact_id", "edit_kind", "draft_job_id")
    ok = create_job(req, env, "proposal", {**{k: prev[k] for k in keys}, "simulation_job_id": sd})
    assert ok[1].status_code == 200, "while the head is still the draft's revision the server-built simulation is accepted"
    op, review, aj, appr = _to_approval(env, ch, c)
    approver = _grant(env, "change_approver")
    assert approver.post(f"/v2/jobs/{aj}/decision", json={"workspace_id": env.ws, "action": "approve", "reason": "ok",
                                                         "expected_approval_digest": appr["approval_digest"]}).status_code == 200
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: PostApplyExtraction())
    apj, r = create_job(c.req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
    assert r.status_code == 200, r.text
    drain(env)
    drain(env)
    assert env.store.head(env.tenant, env.ws).source_revision_id != res["built_from"]["baseline_source_revision_id"]
    late = create_job(req, env, "proposal", {**{k: prev[k] for k in keys}, "simulation_job_id": sd})
    assert late[1].status_code == 409 and "older source revision" in late[1].text, late[1].text


def _window(env, kind, n, label):
    """What `live_local authorize-allocation` appends: a window row anchored at the newest JOB_QUEUED outbox id."""
    with env.store._tx() as cur:
        cur.execute("SELECT coalesce(max(id),0) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED'", (env.tenant, env.ws))
        mark = cur.fetchone()[0]
        cur.execute("INSERT INTO v2_job_ceilings (tenant_id, workspace_id, kind, label, max_total, window_after) VALUES (%s,%s,%s,%s,%s,%s)",
                    (env.tenant, env.ws, kind, label, n, mark))


def test_a_new_person_approved_allocation_replaces_the_used_up_one_without_editing_it(env, ch, tmp_path, monkeypatch):
    sims = [_simulation(env, ch, v) for v in (500000, 600000, 700000, 800000)]
    _cap_config(tmp_path, monkeypatch, "100.00")
    _ceiling(env, "sim.analysis", 1, label="first_allocation")
    req = env.req_client()
    def post_and_cancel(sim):                                             # a cancelled job still used its authorization; it frees the concurrency slot
        made = _post(req, env, sim)
        env.admin_client().post(f"/v2/jobs/{made['job_id']}/cancel", json={"workspace_id": env.ws})
        return made
    post_and_cancel(sims[0])
    used_up = req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[1]})
    assert used_up.status_code == 409 and used_up.json()["code"] == "allocation_exhausted"
    _window(env, "sim.analysis", 2, "alloc_second")
    post_and_cancel(sims[1])                                              # job 2 of the new allocation
    post_and_cancel(sims[2])                                              # job 3
    again = req.post("/v2/simulation-analyses", json={"workspace_id": env.ws, "simulation_job_id": sims[3]})
    assert again.status_code == 409 and "current allocation" in again.json()["detail"], "the new allocation counts only the jobs after it"
    with env.store._tx() as cur:
        cur.execute("SELECT label, max_total, window_after IS NOT NULL FROM v2_job_ceilings WHERE tenant_id=%s AND kind='sim.analysis' ORDER BY created_at",
                    (env.tenant,))
        assert cur.fetchall() == [("first_allocation", 1, False), ("alloc_second", 2, True)], "the old row is untouched (the audit trail)"
    _window(env, "sim.analysis", 5, "alloc_third")                       # a later, bigger one replaces the second only for jobs queued after it
    assert _post(req, env, sims[3])["status"] == "running"


def test_a_timed_arming_turns_the_provider_off_by_itself(monkeypatch):
    """No database is touched: the audit write is pointed at a failing DSN and must not undo the in-process disarm."""
    import threading
    from infra.worker import explain
    from tools.judge_local import live_local as L
    calls = []
    store = type("S", (), {"set_project_model_policy": lambda self, *a, **k: calls.append(k)})()
    monkeypatch.setattr(L, "dsn", lambda: "host=127.0.0.1 port=1 dbname=none connect_timeout=1")
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE")
    monkeypatch.setattr(explain, "_client_for_model", lambda m: "REAL")
    t = L.auto_disarm_after(store, 0.0005, threading.Event())
    t.join(10)
    import os
    assert os.environ["RIPPLE_PROVIDER_MODE"] == "LIVE_DISARMED" and calls == [{"external_provider": "deny"}]
    with pytest.raises(RuntimeError, match="provider refused"):
        explain._client_for_model("gemini-3.8-flash")
