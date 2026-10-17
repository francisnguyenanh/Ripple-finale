"""REL-02 process-kill harness: a REAL worker process is killed (SIGKILL / TerminateProcess) while it executes a job; the lease expires, another worker reclaims the event with a
new fencing token, the dead worker's token can no longer acknowledge, the job completes exactly once and no duplicate side effect appears. Isolated test resources only: the throwaway
PostgreSQL, a fixed sandbox probe program (`infinite_loop_probe`, repo-authored, no model, no provider), a worker subprocess started by this test and killed by it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import psycopg2
import pytest

from infra.adapters.v2_postgres import LeaseLost
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.pitch_brief.conftest import create_job, job_body

WORKER_CODE = (
    "import os, time\n"
    "from infra.worker.job_worker import _make_store, claim_and_admit_one\n"
    "store = _make_store(os.environ['RIPPLE_V2_DSN'])\n"
    "t, w = os.environ['RIPPLE_WORKER_TENANT_ID'], os.environ['RIPPLE_WORKER_WORKSPACE_ID']\n"
    "while True:\n"
    "    if claim_and_admit_one(store, t, w, 'killed-worker', lease_seconds=20) is None:\n"
    "        time.sleep(0.2)\n"
)


def _row(env, job_id):
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("""SELECT o.id, o.status, o.lease_owner, o.fencing_token, o.attempts, (o.lease_until < now()) AS expired, j.execution_status, o.lease_until, now()
                       FROM v2_outbox o JOIN v2_jobs j ON j.tenant_id=o.tenant_id AND j.workspace_id=o.workspace_id AND j.job_id=o.payload->>'job_id'
                       WHERE o.tenant_id=%s AND o.workspace_id=%s AND o.kind='JOB_QUEUED' AND o.payload->>'job_id'=%s""", (env.tenant, env.ws, job_id))
        r = cur.fetchone()
    return None if r is None else {"id": r[0], "status": r[1], "owner": r[2], "token": r[3], "attempts": r[4], "expired": r[5], "execution": r[6], "lease_until": str(r[7]), "db_now": str(r[8])}


def _kill_tree(proc):
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            child.kill()
    except Exception:
        pass
    proc.kill()
    proc.wait(timeout=10)


def test_a_worker_killed_mid_execution_loses_its_lease_and_the_job_completes_exactly_once_elsewhere(env, ws, monkeypatch):
    admin = env.admin_client()
    job_id, r = create_job(admin, env, "simulation", {"candidate_kind": "infinite_loop_probe"})
    assert r.status_code == 200, r.text
    child_env = dict(os.environ, RIPPLE_V2_DSN=env.dsn, RIPPLE_WORKER_TENANT_ID=env.tenant, RIPPLE_WORKER_WORKSPACE_ID=env.ws, RIPPLE_SANDBOX_PROBES="1",
                     RIPPLE_SANDBOX_WALL_SECONDS="40", PYTHONPATH=os.getcwd())
    child_env.pop("RIPPLE_V2_BLOB_BUCKET", None)
    child_env.pop("RIPPLE_BUDGET_CONFIG_PATH", None)                                           # a deterministic job needs no budget config; with one, the lease is stretched to the provider timeout
    proc = subprocess.Popen([sys.executable, "-c", WORKER_CODE], cwd=os.getcwd(), env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 60
        row = _row(env, job_id)
        while time.time() < deadline and not (row and row["status"] == "CLAIMED" and row["owner"] == "killed-worker" and row["execution"] == "RUNNING"):
            if proc.poll() is not None:
                pytest.fail("the worker process died by itself: " + proc.stderr.read().decode("utf-8", "replace")[-2000:])
            time.sleep(0.3)
            row = _row(env, job_id)
        assert row and row["status"] == "CLAIMED" and row["execution"] == "RUNNING", row
        dead_token, event_id = row["token"], row["id"]
        time.sleep(1.0)                                                                       # the probe is running inside the killed worker's sandbox
        _kill_tree(proc)                                                                      # SIGKILL / TerminateProcess: no cleanup code runs
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
    after_kill = _row(env, job_id)
    assert after_kill["status"] == "CLAIMED" and after_kill["owner"] == "killed-worker" and after_kill["execution"] == "RUNNING" and after_kill["attempts"] == 1   # nothing was rolled back or finished
    # another worker cannot take the event while the lease is alive ...
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND status='PENDING'", (env.tenant, env.ws))
        pending_others = cur.fetchone()[0]
    claim = env.store.claim_outbox(env.tenant, env.ws, "rescuer", lease_seconds=20)
    assert claim is None or claim.id != event_id, "a live lease was taken over"
    if claim is not None:                                                                     # an unrelated pending event of the workspace: hand it back untouched
        env.store.release_outbox(env.tenant, env.ws, claim.id, "rescuer", claim.fencing_token)
    assert pending_others >= 0
    # ... and takes it once the lease expired, with a new fencing token; the dead worker's token can no longer acknowledge
    deadline = time.time() + 40
    while time.time() < deadline and not _row(env, job_id)["expired"]:
        time.sleep(0.5)
    assert _row(env, job_id)["expired"] is True, _row(env, job_id)
    reclaim = None
    while reclaim is None or reclaim.id != event_id:
        reclaim = env.store.claim_outbox(env.tenant, env.ws, "rescuer", lease_seconds=30)
        assert reclaim is not None, "the expired lease was not reclaimable"
    assert reclaim.fencing_token > dead_token and reclaim.attempts == 2
    with pytest.raises(LeaseLost):                                                           # a zombie with the old token is fenced out
        env.store.ack_outbox(env.tenant, env.ws, event_id, "killed-worker", dead_token)
    assert _row(env, job_id)["status"] == "CLAIMED" and _row(env, job_id)["owner"] == "rescuer"
    # the rescuer finishes the job through the ordinary executor path (the probe times out by its wall limit) and acknowledges with its own token
    monkeypatch.setenv("RIPPLE_SANDBOX_WALL_SECONDS", "6")
    from infra.worker import job_worker
    job_worker.execute_admitted_claim(env.store, env.tenant, env.ws, reclaim, "rescuer", config=None, lease_seconds=30, max_running=None)
    final = _row(env, job_id)
    assert final["status"] == "DONE" and final["owner"] is None and final["execution"] == "COMPLETED", final
    body = job_body(admin, env, job_id)
    assert body["execution_status"] == "COMPLETED" and body["execution_result"]["status"] in ("TIMEOUT", "EXECUTED") and body["execution_result"]["executor_id"] == "simulation.run.v1"
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND execution_result IS NOT NULL", (env.tenant, env.ws, job_id))
        assert cur.fetchone()[0] == 1                                                         # exactly one result row: the dead worker wrote nothing, the rescuer once
        cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED' AND payload->>'job_id'=%s", (env.tenant, env.ws, job_id))
        assert cur.fetchone()[0] == 1
    with pytest.raises(LeaseLost):                                                           # still fenced out after the job is done
        env.store.ack_outbox(env.tenant, env.ws, event_id, "killed-worker", dead_token)
    assert _row(env, job_id)["status"] == "DONE"
