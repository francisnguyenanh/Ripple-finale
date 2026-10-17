"""MA-00 process kill (LOCAL, isolated test resources): a REAL worker subprocess claims a specialist child of a multi-agent run and is killed (TerminateProcess / SIGKILL) while
its provider call is in flight (the subprocess installs a stub client that never answers; no real provider). The durable attempt row written before the call must survive as
`USAGE_UNKNOWN` at its ceiling (never free, never released); the reclaim must respect the approved retry limit; the coordinator must learn about the child's outcome without an
operator step; the run's budget invariant must hold with the unknown liability booked.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import psycopg2
import pytest

from core.change_intelligence.v2 import ma_orchestration as ma
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401
from tests.final_loop.test_ma_review_board import _confirmed, board_stub, ma_env, start  # noqa: F401
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401
from tests.final_loop.test_rel02_process_kill import _kill_tree
from tests.pitch_brief.conftest import drain

WORKER_CODE = (
    "import os, time\n"
    "from infra.worker import explain\n"
    "class _Hang:\n"
    "    class models:\n"
    "        @staticmethod\n"
    "        def generate_content(**kw):\n"
    "            time.sleep(600)\n"
    "explain._client_for_model = lambda m: _Hang\n"
    "explain._assert_global_endpoint = lambda: None\n"
    "from infra.worker.job_worker import _make_store, claim_and_admit_one\n"
    "store = _make_store(os.environ['RIPPLE_V2_DSN'])\n"
    "t, w = os.environ['RIPPLE_WORKER_TENANT_ID'], os.environ['RIPPLE_WORKER_WORKSPACE_ID']\n"
    "while True:\n"
    "    if claim_and_admit_one(store, t, w, 'killed-worker', lease_seconds=20) is None:\n"
    "        time.sleep(0.2)\n"
)


def _inflight(q):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("""SELECT a.job_id FROM v2_job_attempts a JOIN v2_jobs j ON j.tenant_id=a.tenant_id AND j.workspace_id=a.workspace_id AND j.job_id=a.job_id
                       WHERE a.tenant_id=%s AND a.workspace_id=%s AND a.outcome='IN_FLIGHT' AND j.kind='ma.specialist'""", (q.env.tenant, q.env.ws))
        r = cur.fetchone()
    return r[0] if r else None


def _attempts(q, jid):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT outcome, cost_usd, input_tokens FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND outcome <> 'IN_FLIGHT_RESOLVED' ORDER BY recorded_at",
                    (q.env.tenant, q.env.ws, jid))
        return cur.fetchall()


def _kill_mid_call(q):
    child_env = dict(os.environ, RIPPLE_V2_DSN=q.env.dsn, RIPPLE_WORKER_TENANT_ID=q.env.tenant, RIPPLE_WORKER_WORKSPACE_ID=q.env.ws, PYTHONPATH=os.getcwd())
    child_env.pop("RIPPLE_V2_BLOB_BUCKET", None)
    proc = subprocess.Popen([sys.executable, "-c", WORKER_CODE], cwd=os.getcwd(), env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 90
        jid = _inflight(q)
        while time.time() < deadline and jid is None:
            if proc.poll() is not None:
                pytest.fail("the worker process died by itself: " + proc.stderr.read().decode("utf-8", "replace")[-2000:])
            time.sleep(0.3)
            jid = _inflight(q)
        assert jid, "no specialist call went in flight"
        time.sleep(0.5)
        _kill_tree(proc)                                                                       # no cleanup code of the worker runs
    finally:
        if proc.poll() is None:
            _kill_tree(proc)
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:                           # time passing: the dead worker's leases run out
        cur.execute("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'", (q.env.tenant, q.env.ws))
    return jid


def _settle(q, run_id, tries=80):
    for _ in range(tries):
        drain(q.env)
        got = q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
        if got["status"] in ma.TERMINAL:
            return got
        time.sleep(0.2)
    return got


def _no_retry(tmp_path, monkeypatch):
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read()
    assert "max_retries: 1\n" in cfg
    path = tmp_path / "budget_kill_no_retry.yaml"
    path.write_text(cfg.replace("max_retries: 1\n", "max_retries: 0\n", 1), encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))


def test_a_specialist_killed_mid_call_is_retried_within_the_limit_and_the_dead_attempt_stays_booked(ma_env, tmp_path, monkeypatch):
    q = ma_env
    # the test budget config approves max_retries 1 (two provider attempts per job, one call each): the reclaim may call once more
    cid, _ = _confirmed(q)
    run_id = start(q, q.admin, cid).json()["run_id"]
    jid = _kill_mid_call(q)
    model = use(q, board_stub)
    got = _settle(q, run_id)
    assert got["status"] == "REVIEW_READY", got["status"]
    rows = _attempts(q, jid)
    assert sorted(r[0] for r in rows) == ["COMPLETED", "USAGE_UNKNOWN"]
    unknown = next(r for r in rows if r[0] == "USAGE_UNKNOWN")
    assert float(unknown[1]) > 0 and unknown[2] is None                                         # estimated at the ceiling, no tokens, never free
    child = next(c for c in got["children"] if c["child_id"] == jid)
    assert child["status"] == "COMPLETED" and child["entry_state"] == "SETTLED" and float(child["cost_usd"]) >= float(unknown[1])
    assert got["budget"]["holds"] is True and model.calls >= 1


def test_with_no_retry_approved_a_killed_specialist_fails_once_and_the_coordinator_learns_it_without_an_operator(ma_env, tmp_path, monkeypatch):
    q = ma_env
    cid, _ = _confirmed(q)
    _no_retry(tmp_path, monkeypatch)                                                            # the mandate's real setting: retries off (set after the fixture's own AI steps)
    run_id = start(q, q.admin, cid).json()["run_id"]
    jid = _kill_mid_call(q)
    model = use(q, board_stub)
    got = _settle(q, run_id)                                                                    # drain only: no POST .../advance anywhere in this test
    assert got["status"] == "INCOMPLETE", got["status"]
    child = next(c for c in got["children"] if c["child_id"] == jid)
    assert child["status"] == "FAILED" and child["job_status"] == "FAILED" and "provider_attempt_limit_reached" in (child["job_reason"] or "")
    assert [r[0] for r in _attempts(q, jid)] == ["USAGE_UNKNOWN"]                               # no second call for that child: the dead attempt used the only approved one
    assert child["role"] in got["report"]["missing_required_roles"] or got["report"]["roles"][child["role"]]["status"] == "failed"
    assert got["budget"]["holds"] is True and float(child["cost_usd"] or 0) > 0                 # the unknown liability stays booked at its ceiling
    assert all(c["status"] in ("COMPLETED", "FAILED", "CANCELLED") for c in got["children"])


def test_when_the_last_pending_child_dies_mid_call_the_reclaim_still_wakes_the_coordinator(ma_env, tmp_path, monkeypatch):
    """The sharp case: every other child of the round has finished, so no later join job exists. The dead worker's writes are reproduced exactly (claim, begin_execution,
    durable IN_FLIGHT attempt at the ceiling); after its lease expires, the reclaim fails the job on the retry limit before any executor code runs. Without a hook the run would
    wait for an operator's advance forever."""
    q = ma_env
    cid, _ = _confirmed(q)
    _no_retry(tmp_path, monkeypatch)
    use(q, board_stub)
    run_id = start(q, q.admin, cid).json()["run_id"]
    from infra.worker.job_worker import _attempt_cost, _load_config_if_configured
    dead, held = None, []
    for _ in range(40):                                                                         # the dying worker takes the compliance child's event (others are held, then given back)
        c = q.env.store.claim_outbox(q.env.tenant, q.env.ws, "dying-worker", lease_seconds=600)
        if c is None:
            break
        if c.kind == "JOB_QUEUED" and (c.payload or {}).get("kind") == "ma.specialist" and q.env.store.job_record(q.env.tenant, q.env.ws, c.payload["job_id"])["payload"]["role"] == "compliance":
            dead = c
            break
        held.append(c)
    for c in held:
        q.env.store.release_outbox(q.env.tenant, q.env.ws, c.id, "dying-worker", c.fencing_token)
    assert dead is not None
    jid = dead.payload["job_id"]
    assert q.env.store.begin_execution(q.env.tenant, q.env.ws, jid) is not None
    ceiling = _attempt_cost(_load_config_if_configured(), 1, None, "ma.specialist")[0]
    q.env.store.begin_provider_attempt(q.env.tenant, q.env.ws, jid, f"outbox:{dead.id}:attempt:{dead.attempts}:token:{dead.fencing_token}:inflight:1", ceiling_usd=ceiling,
                                       context={"worker_id": "dying-worker", "call": 1, "cost_basis": "ceiling_until_usage_is_recorded"})
    for _ in range(40):                                                                         # everything else finishes; the run waits for the held child
        if not drain(q.env):
            break
    mid = q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()
    assert mid["status"] == "RUNNING" and [c["role"] for c in mid["children"] if c["status"] == "DISPATCHED"] == ["compliance"]
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:                           # the dying worker is gone; its lease runs out
        cur.execute("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE id=%s", (dead.id,))
    got = _settle(q, run_id)                                                                    # drain only, no advance call
    assert got["status"] in ma.TERMINAL, got["status"]
    child = next(c for c in got["children"] if c["child_id"] == jid)
    assert child["status"] == "FAILED" and "provider_attempt_limit_reached" in (child["job_reason"] or "")
    assert [r[0] for r in _attempts(q, jid)] == ["USAGE_UNKNOWN"] and got["report"]["roles"]["compliance"]["status"] == "failed"
    assert got["budget"]["holds"] is True
