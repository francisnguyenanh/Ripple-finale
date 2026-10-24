"""Role allowlist through the real route and worker (throwaway PostgreSQL; the provider is a counting stub whose call count must stay 0).

- enqueue: a kind not in `enabled_roles` (or a configuration without the key) is refused by the route with its reason; no job row, no
  reservation, no provider call; the allowlist never admits a job (a requester's job still waits for approval);
- revoked between enqueue and execution: the worker ends the job FAILED `role_not_enabled` before any provider attempt (0 calls, 0 cost);
- global switch: with AI switched off the revoked job is not even claimed; the switch decides first.
Needs PITCH_BRIEF_TEST_DSN (skipped otherwise, which is NOT a pass).
"""
from __future__ import annotations

import pytest

from infra.worker import extraction, live_analysis
from tests.pitch_brief.conftest import REAL_BUDGET_PATH, create_job, drain, import_harbor, job_body
from tests.pitch_brief.test_final_disarmed_zero_calls import CountingClient


def _config(tmp_path, monkeypatch, line):
    text = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("enabled_roles: all\n", "" if line is None else line + "\n")
    path = tmp_path / f"budget-{abs(hash(line))}.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))


def _setup(env, monkeypatch):
    admin = env.admin_client()
    arts, _ = import_harbor(env, admin)
    drain(env)
    stub = CountingClient()
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: stub)
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda m: stub)
    return admin, arts[sorted(arts)[0]], stub


def _rows(env, job_id):
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (env.tenant, env.ws, job_id))
        jobs = cur.fetchone()[0]
        cur.execute("SELECT count(*), COALESCE(sum(provider_calls),0), COALESCE(sum(cost_usd),0) FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s",
                    (env.tenant, env.ws, job_id))
        attempts, calls, cost = cur.fetchone()
    return jobs, attempts, int(calls), cost


@pytest.mark.parametrize("line,code", [("enabled_roles: [knowledge.qa]", "role_not_enabled"), (None, "ai_roles_not_configured")])
def test_enqueue_of_a_kind_not_enabled_is_refused_before_anything_is_created(env, monkeypatch, tmp_path, line, code):
    admin, art, stub = _setup(env, monkeypatch)
    _config(tmp_path, monkeypatch, line)
    job_id, r = create_job(admin, env, "analysis", {"artifact_id": art})
    assert r.status_code == 409 and r.json()["code"] == code and r.json()["kind"] == "analysis", r.text[:300]
    drain(env)
    jobs, attempts, calls, _cost = _rows(env, job_id)
    assert (jobs, attempts, calls, stub.calls) == (0, 0, 0, 0)


def test_an_enabled_kind_is_accepted_but_not_admitted_by_the_allowlist(env, monkeypatch, tmp_path):
    """The allowlist only removes capability: a requester's enabled job still waits for a second person (no permission widened)."""
    admin, art, stub = _setup(env, monkeypatch)
    _config(tmp_path, monkeypatch, "enabled_roles: [analysis]")
    job_id, r = create_job(env.req_client(), env, "analysis", {"artifact_id": art})
    assert r.status_code == 200 and r.json()["status"] == "PENDING_APPROVAL", r.text[:300]
    assert stub.calls == 0


def test_revoked_between_enqueue_and_execution_fails_before_any_provider_attempt(env, monkeypatch, tmp_path):
    admin, art, stub = _setup(env, monkeypatch)
    _config(tmp_path, monkeypatch, "enabled_roles: [analysis, extraction]")
    job_id, r = create_job(env.req_client(), env, "analysis", {"artifact_id": art})
    assert r.status_code == 200, r.text
    assert admin.post(f"/v2/jobs/{job_id}/approve", json={"workspace_id": env.ws}).status_code == 200
    _config(tmp_path, monkeypatch, "enabled_roles: [extraction]")          # the operator removes the kind while the job is queued
    drain(env)
    body = job_body(admin, env, job_id)
    assert body["execution_status"] == "FAILED", body
    assert str(body.get("execution_reason") or body.get("reason") or "").startswith("role_not_enabled"), body
    jobs, attempts, calls, cost = _rows(env, job_id)
    assert (calls, stub.calls) == (0, 0) and cost == 0 and attempts <= 1     # at most the zero-call FAILED record; no in-flight provider attempt


def test_with_ai_switched_off_a_revoked_job_is_not_claimed_and_the_switch_decides_first(env, monkeypatch, tmp_path):
    from infra import operator_flags
    admin, art, stub = _setup(env, monkeypatch)
    _config(tmp_path, monkeypatch, "enabled_roles: all")
    job_id, r = create_job(env.req_client(), env, "analysis", {"artifact_id": art})
    assert r.status_code == 200, r.text
    assert admin.post(f"/v2/jobs/{job_id}/approve", json={"workspace_id": env.ws}).status_code == 200
    _config(tmp_path, monkeypatch, "enabled_roles: [extraction]")
    monkeypatch.setenv("RIPPLE_OPERATOR_FLAGS", "1")
    operator_flags.reset_cache()
    operator_flags.set_flag("ai_switch", "off", "test-operator", "role allowlist check")
    operator_flags.reset_cache()
    try:
        drain(env)
        assert job_body(admin, env, job_id)["execution_status"] not in ("COMPLETED", "FAILED", "RUNNING")   # still waiting: the switch keeps AI jobs queued
        _, r2 = create_job(admin, env, "analysis", {"artifact_id": art})
        assert r2.status_code == 409 and r2.json()["code"] in ("ai_switched_off", "provider_disarmed"), r2.text[:300]   # the switch, not the role
    finally:
        operator_flags.set_flag("ai_switch", "on", "test-operator", "restore after the role allowlist check")
        operator_flags.reset_cache()
    drain(env)
    body = job_body(admin, env, job_id)
    assert body["execution_status"] == "FAILED" and str(body.get("execution_reason") or body.get("reason") or "").startswith("role_not_enabled"), body
    assert stub.calls == 0


# --------------------------------------------------------------------------- chat: the interpreter role not enabled
from tests.pitch_brief.test_intent_chat import READY, Interp, allow, ch, kinds, settle, start, use  # noqa: E402,F401  (fixtures ch / use)


def test_chat_keeps_the_conversation_and_sends_nothing_when_the_interpreter_role_is_not_enabled(env, ch, use, monkeypatch, tmp_path):
    m = use(Interp(READY))
    _config(tmp_path, monkeypatch, "enabled_roles: [knowledge.qa]")
    req = env.req_client()
    c = start(env, req)
    assert c["awaiting_ai"] is False                                         # held, exactly like a disarmed provider: no job, no allowance granted
    c = settle(env, req, c["conversation_id"])
    assert m.calls == 0 and "intent.interpret" not in kinds(env)
    r = allow(env, req, c["conversation_id"])                               # "Ask the AI" says why
    assert r.status_code == 409 and r.json()["code"] == "role_not_enabled", r.text[:300]
    assert "intent.interpret" not in kinds(env) and m.calls == 0
