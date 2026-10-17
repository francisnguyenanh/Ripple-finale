"""BAS-03: with the provider disarmed, no provider-backed job is accepted and no provider client is ever called, through the real route and worker.

Real path: POST /v2/jobs on a throwaway PostgreSQL, then the worker drain. The provider is a counting stub; its call count must stay 0.
Needs PITCH_BRIEF_TEST_DSN (skipped otherwise, which is NOT a pass).
"""
from __future__ import annotations

import pytest

from infra import provider_gate
from infra.worker import extraction, job_worker, live_analysis
from tests.pitch_brief.conftest import create_job, drain, import_harbor, job_body


class CountingClient:
    def __init__(self):
        self.calls = 0
        outer = self

        class _Models:
            def generate_content(self, **kw):
                outer.calls += 1
                raise AssertionError("provider called while disarmed")
        self.models = _Models()


def test_every_provider_backed_executor_is_covered_by_the_disarm_gate():
    """A new external executor that is missing from AI_KINDS would run (and spend) with the provider switched off."""
    external = {k for k, v in job_worker.BUILTIN_EXECUTORS.items() if getattr(v, "external", False)}
    assert external - provider_gate.AI_KINDS == set(), sorted(external - provider_gate.AI_KINDS)


@pytest.mark.parametrize("kind", ["extraction", "analysis"])
def test_disarmed_provider_refuses_the_job_and_makes_zero_calls(env, monkeypatch, kind):
    admin = env.admin_client()
    arts, _ = import_harbor(env, admin)
    drain(env)
    stub = CountingClient()
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: stub)
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda m: stub)
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    path = sorted(arts)[0]
    payload = {"artifact_id": arts[path], "source_role": "unknown"} if kind == "extraction" else {"artifact_id": arts[path]}
    job_id, r = create_job(admin, env, kind, payload)
    assert r.status_code == 409 and "provider_disarmed" in r.text, (r.status_code, r.text[:300])
    drain(env)
    assert stub.calls == 0


@pytest.mark.parametrize("kind", ["extraction", "analysis"])
def test_operator_switch_off_after_approval_keeps_the_job_queued_and_makes_zero_calls(env, monkeypatch, kind):
    """Armed at creation, approved, then the operator turns AI off: the worker must not claim the job (no provider call, no spend)."""
    from infra import operator_flags
    admin, req = env.admin_client(), env.req_client()
    arts, _ = import_harbor(env, admin)
    drain(env)
    stub = CountingClient()
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: stub)
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda m: stub)
    path = sorted(arts)[0]
    payload = {"artifact_id": arts[path], "source_role": "unknown"} if kind == "extraction" else {"artifact_id": arts[path]}
    job_id, r = create_job(req, env, kind, payload)
    assert r.status_code == 200, r.text
    assert admin.post(f"/v2/jobs/{job_id}/approve", json={"workspace_id": env.ws}).status_code == 200
    monkeypatch.setenv("RIPPLE_OPERATOR_FLAGS", "1")
    operator_flags.reset_cache()
    operator_flags.set_flag("ai_switch", "off", "test-operator", "BAS-03 disarmed zero-call check")
    operator_flags.reset_cache()
    try:
        drain(env)
        assert stub.calls == 0
        assert job_body(admin, env, job_id)["execution_status"] not in ("SUCCEEDED", "FAILED", "RUNNING")   # still waiting, not run
    finally:
        operator_flags.set_flag("ai_switch", "on", "test-operator", "restore after BAS-03 check")
        operator_flags.reset_cache()
