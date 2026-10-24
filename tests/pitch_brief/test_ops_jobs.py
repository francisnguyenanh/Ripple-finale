"""tools/ops_jobs: queued jobs of an operating window are cancelled with a reason before the deployment is switched on again; nothing runs them later.
Needs PITCH_BRIEF_TEST_DSN (skipped otherwise, which is NOT a pass)."""
from __future__ import annotations

from tests.pitch_brief.conftest import create_job, drain, import_harbor, job_body
from tests.pitch_brief.test_final_disarmed_zero_calls import CountingClient
from tools import ops_jobs


def test_cancel_queued_needs_apply_and_a_reason_and_the_worker_never_runs_the_cancelled_job(env, monkeypatch, capsys):
    from infra.worker import extraction, live_analysis
    admin = env.admin_client()
    arts, _ = import_harbor(env, admin)
    drain(env)
    stub = CountingClient()
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: stub)
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda m: stub)
    job_id, r = create_job(env.req_client(), env, "analysis", {"artifact_id": arts[sorted(arts)[0]]})
    assert r.status_code == 200
    assert admin.post(f"/v2/jobs/{job_id}/approve", json={"workspace_id": env.ws}).status_code == 200      # queued, not yet claimed
    base = ["--tenant", env.tenant, "--workspace", env.ws]
    assert ops_jobs.main(["cancel-queued", *base]) == 2                                                   # no reason: refused
    assert ops_jobs.main(["cancel-queued", *base, "--reason", "end of operating window 1"]) == 0          # dry run
    assert job_body(admin, env, job_id)["execution_status"] == "QUEUED"
    assert ops_jobs.main(["cancel-queued", *base, "--reason", "end of operating window 1", "--apply"]) == 0
    body = job_body(admin, env, job_id)
    assert body["execution_status"] == "CANCELLED" and "operator_cancelled: end of operating window 1" in (body.get("execution_reason") or "")
    drain(env)                                                                                             # switched on again: the old queue event is acknowledged, not run
    assert job_body(admin, env, job_id)["execution_status"] == "CANCELLED" and stub.calls == 0
    assert ops_jobs.main(["list", *base]) == 0 and "queued 0, running 0" in capsys.readouterr().out
