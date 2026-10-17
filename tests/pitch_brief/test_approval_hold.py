"""Approval-before-provider on EVERY consumer of the queue (stock worker, bounded worker loop, concurrent workers), not just
the web pump; and the unchanged semantics outside the sandbox. Provider = counting stub (0 calls is the assertion)."""
from __future__ import annotations

import threading

import pytest

from infra.worker import job_worker
from tests.pitch_brief.chain import APPROVAL, build
from tests.pitch_brief.conftest import create_job, job_body, sandbox_config


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _lease(client, env):
    r = client.post("/v2/sandbox/lease", json={"workspace_id": env.ws, "action": "acquire", "participants": [env.req]})
    assert r.status_code == 200, r.text


def _stock_worker_rounds(env, n):
    """The stock dispatcher exactly as a worker process calls it (no sandbox-specific caller)."""
    out = []
    for i in range(n):
        out.append(job_worker.claim_and_admit_one(env.store, env.tenant, env.ws, f"stock-{i}"))
    return out


def test_unapproved_provider_job_gets_zero_calls_on_the_stock_worker_and_the_bounded_loop(env, ch, monkeypatch):
    monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(env.ws, ch.rev))
    _lease(ch.admin, env)
    jid, r = create_job(env.req_client(), env, "extraction", {"artifact_id": ch.arts[APPROVAL], "source_role": "unknown"})
    assert r.status_code == 200, r.text
    before = ch.stub.calls
    # (1) the stock dispatcher, several times
    _stock_worker_rounds(env, 5)
    # (2) the bounded worker loop used by the deployed worker job (it delegates to the same dispatcher)
    job_worker.run_bounded_worker(env.store, [(env.tenant, env.ws)], idle_exit_seconds=2, poll_seconds=0.2)
    assert ch.stub.calls == before                                        # ZERO provider calls
    body = job_body(ch.admin, env, jid)
    assert body["status"] == "PENDING_APPROVAL" and body["execution_status"] == "QUEUED"
    # a different person approves; ONLY then does the same stock worker make the call (exactly one)
    assert ch.admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
    import time
    deadline = time.time() + 40                   # the held claim is deferred HOLD_SECONDS; the stock worker reclaims it after
    while ch.stub.calls == before and time.time() < deadline:
        _stock_worker_rounds(env, 1)
        time.sleep(0.5)
    assert ch.stub.calls == before + 1
    assert job_body(ch.admin, env, jid)["execution_status"] == "COMPLETED"


def test_hold_does_not_depend_on_the_worker_having_the_sandbox_env_var(env, ch, monkeypatch):
    """The marker is shared database state (a SANDBOX_LEASE_* audit event), so a worker process started WITHOUT
    RIPPLE_SANDBOX_CONFIG is still held back."""
    monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(env.ws, ch.rev))
    _lease(ch.admin, env)
    jid, r = create_job(env.req_client(), env, "extraction", {"artifact_id": ch.arts[APPROVAL], "source_role": "unknown"})
    assert r.status_code == 200, r.text
    monkeypatch.delenv("RIPPLE_SANDBOX_CONFIG")                            # the "other worker" has no sandbox config at all
    before = ch.stub.calls
    _stock_worker_rounds(env, 5)
    assert ch.stub.calls == before and job_body(ch.admin, env, jid)["execution_status"] == "QUEUED"


def test_semantics_outside_a_sandbox_are_unchanged(env, ch):
    """A workspace that never had a sandbox lease keeps the stock behaviour (the worker runs a queued job; approval gates
    publishing). Stated so a reader knows the hold is scoped to sandbox workspaces only."""
    jid, r = create_job(env.req_client(), env, "extraction", {"artifact_id": ch.arts[APPROVAL], "source_role": "unknown"})
    assert r.status_code == 200, r.text
    before = ch.stub.calls
    _stock_worker_rounds(env, 3)
    assert ch.stub.calls == before + 1                                     # ran without approval: unchanged production semantics
    body = job_body(ch.admin, env, jid)
    assert body["status"] == "PENDING_APPROVAL" and body["execution_status"] == "COMPLETED"


def test_two_workers_never_run_one_approved_job_twice(env, ch, monkeypatch):
    monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(env.ws, ch.rev))
    _lease(ch.admin, env)
    jid, r = create_job(env.req_client(), env, "extraction", {"artifact_id": ch.arts[APPROVAL], "source_role": "unknown"})
    assert ch.admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
    before = ch.stub.calls
    errors = []

    def worker(i):
        try:
            for _ in range(3):
                job_worker.claim_and_admit_one(env.store, env.tenant, env.ws, f"w-{i}")
        except Exception as exc:                 # noqa: BLE001
            errors.append(repr(exc))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert not errors, errors
    assert ch.stub.calls == before + 1                                     # exactly one provider call
    assert job_body(ch.admin, env, jid)["execution_status"] == "COMPLETED"
