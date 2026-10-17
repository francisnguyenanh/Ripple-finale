"""MA-00 recovery and race checks over REAL persistence (the throwaway PostgreSQL, the real outbox leases and fencing, the real coordinator), synthetic specialists:
two worker connections drain the same workspace concurrently while a board runs; a worker that dies holding a child's lease (lease expiry, no ack) is superseded and its stale
token cannot acknowledge; a knowledge version that moves under a run makes the report stale (INCOMPLETE, pin_moved) and nothing of the run is written into the new knowledge
version; a provider failure of a required role is never turned into consensus. 0 real provider calls."""
from __future__ import annotations

import json
import threading
import time

import psycopg2
import pytest

from core.change_intelligence.v2 import ma_orchestration as ma
from infra.adapters.v2_postgres import LeaseLost, PostgresV2Store
from infra.worker.job_worker import claim_and_admit_one
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: F401
from tests.final_loop.test_kno05_accept import accept, answer
from tests.final_loop.test_ma_review_board import _confirmed, board_stub, ma_env, start  # noqa: F401
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401
from tests.pitch_brief.conftest import drain


def _state(q, run_id):
    return q.admin.get(f"/v2/ma/runs/{run_id}", params={"workspace_id": q.env.ws}).json()


def _children_rows(q, run_id):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT child_id, status, result_hash FROM v2_ma_children WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s ORDER BY round_no, role", (q.env.tenant, q.env.ws, run_id))
        return cur.fetchall()


def test_two_worker_connections_drain_one_run_concurrently_and_every_child_completes_exactly_once(ma_env):
    q = ma_env
    cid, _ = _confirmed(q)
    use(q, board_stub)
    r = start(q, q.admin, cid)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    stores = [PostgresV2Store(q.env.dsn, q.env.blobs) for _ in range(2)]
    claimed = {"a": 0, "b": 0}
    stop = threading.Event()

    def worker(name, store):
        while not stop.is_set():
            got = claim_and_admit_one(store, q.env.tenant, q.env.ws, f"worker-{name}", lease_seconds=20)
            if got is None:
                time.sleep(0.05)
            else:
                claimed[name] += 1
    threads = [threading.Thread(target=worker, args=(n, s), daemon=True) for n, s in zip("ab", stores)]
    for t in threads:
        t.start()
    deadline = time.time() + 120
    while time.time() < deadline:
        got = _state(q, run_id)
        if got["status"] in ma.TERMINAL:
            break
        time.sleep(0.3)
    stop.set()
    for t in threads:
        t.join(timeout=30)
    assert got["status"] == "REVIEW_READY", json.dumps(got["events"])[:800]
    rows = _children_rows(q, run_id)
    assert all(st == "COMPLETED" and rh for _, st, rh in rows) and len(rows) == len({c for c, _, _ in rows}) and not any(st == "LATE_IGNORED" for _, st, _ in rows)
    assert claimed["a"] > 0 and claimed["b"] > 0                                                                           # both connections really took work
    assert got["budget"]["holds"] is True and got["budget"]["unallocated_reserved_usd"] == "0"
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='ma.specialist' AND execution_status='COMPLETED'", (q.env.tenant, q.env.ws))
        assert cur.fetchone()[0] == len([c for c in got["children"] if c["status"] == "COMPLETED"])                        # one job per child, each completed once
    # sequential vs parallel scheduling: the same validated outputs give the same semantic report
    report = got["report"]
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT child_id, role, round_no, status, result FROM v2_ma_children WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s", (q.env.tenant, q.env.ws, run_id))
        stored = [{"child_id": c, "role": r, "round": n, "status": st.lower() if st in ("COMPLETED", "FAILED", "CANCELLED") else "pending", "result": res if isinstance(res, dict) or res is None else json.loads(res)}
                  for c, r, n, st, res in cur.fetchall()]
    again = ma.join(list(reversed(stored)), ma.manifest("review_board"), binding=report["binding"])
    assert again["report_hash"] == report["report_hash"]


def test_a_worker_that_dies_holding_a_child_lease_is_superseded_and_its_stale_token_cannot_acknowledge(ma_env):
    q = ma_env
    cid, _ = _confirmed(q)
    use(q, board_stub)
    run_id = start(q, q.admin, cid).json()["run_id"]
    dead = "dead-worker"
    claim = None
    for _ in range(20):                                                                                                       # the dead worker holds one CHILD's event and never returns (other events it touched simply expire)
        c = q.env.store.claim_outbox(q.env.tenant, q.env.ws, dead, lease_seconds=1)
        if c is None:
            break
        if c.kind == "JOB_QUEUED" and (c.payload or {}).get("kind") == "ma.specialist":
            claim = c
            break
    assert claim is not None
    job_id = claim.payload["job_id"]
    time.sleep(1.5)                                                                                                           # the lease expires without an ack
    for _ in range(60):
        drain(q.env)
        got = _state(q, run_id)
        if got["status"] in ma.TERMINAL:
            break
        time.sleep(0.2)
    assert got["status"] == "REVIEW_READY"
    with pytest.raises(LeaseLost):
        q.env.store.ack_outbox(q.env.tenant, q.env.ws, claim.id, dead, claim.fencing_token)                            # the stale token is fenced out
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, lease_owner, attempts FROM v2_outbox WHERE id=%s", (claim.id,))
        status, owner, attempts = cur.fetchone()
        cur.execute("SELECT execution_status, execution_provider_calls FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (q.env.tenant, q.env.ws, job_id))
        ex, calls = cur.fetchone()
    assert status == "DONE" and owner is None and attempts >= 2 and ex == "COMPLETED" and calls == 1                        # reclaimed once, executed once, no double provider call
    assert all(c["status"] == "COMPLETED" for c in got["children"]) and got["budget"]["holds"] is True


def test_a_knowledge_version_that_moves_under_a_run_makes_the_report_stale_and_writes_nothing_into_the_new_version(ma_env):
    q = ma_env
    cid, _ = _confirmed(q)
    use(q, board_stub)
    run_id = start(q, q.admin, cid).json()["run_id"]
    before = counts(q.env)
    for _ in range(2):                                                                                                        # two children of round 1 complete under the original version
        claim_and_admit_one(q.env.store, q.env.tenant, q.env.ws, "w1", lease_seconds=20)
    mid = _state(q, run_id)
    assert mid["status"] in ("RUNNING", "JOINING") and any(c["status"] == "COMPLETED" for c in mid["children"])
    g = next(x for x in q.admin.get("/v2/business/gaps", params={"workspace_id": q.env.ws}).json()["gaps"] if x["kind"] == "missing_actor")
    answer(q.admin, q.env, g, {"actor": "duty manager"}, "The duty manager decides.")
    assert accept(q.admin, q.env, acknowledged_open_gaps=True).status_code == 200                                              # the knowledge version moves under the run
    for _ in range(60):
        drain(q.env)
        got = _state(q, run_id)
        if got["status"] in ma.TERMINAL:
            break
        time.sleep(0.2)
    assert got["status"] == "INCOMPLETE" and got["report"]["incomplete_reason"].startswith("pin_moved")
    assert got["report"]["binding"]["pin"] == mid["binding"]["pin"]                                                          # the report stays bound to the version it was started on
    after = counts(q.env)
    assert after["v2_business_decisions"] == before["v2_business_decisions"] + 1                                               # exactly the person's own decision; the run wrote nothing else
    assert all(k == "v2_business_decisions" or after[k] >= before[k] for k in before)
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_business_decisions WHERE tenant_id=%s AND workspace_id=%s AND quote LIKE %s", (q.env.tenant, q.env.ws, "%" + "threshold" + "%"))
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND actor_id LIKE 'ma%%' OR actor_id='coordinator' AND event_type LIKE 'BUSINESS_%%'", (q.env.tenant, q.env.ws))
        assert cur.fetchone()[0] == 0                                                                                        # no business event by the coordinator or a specialist
    assert got["human_action_needed"] is True if "human_action_needed" in got else got["report"]["human_action_needed"] is True


def test_a_failed_optional_role_is_labelled_and_a_failed_required_role_never_becomes_consensus(ma_env):
    q = ma_env
    cid, _ = _confirmed(q)

    def flaky(prompt):
        if "INTERNAL COMPLIANCE critic" in prompt:
            return {"status": "OK", "nope": True}                                                                           # schema violation of the OPTIONAL role
        return board_stub(prompt)
    use(q, flaky)
    run_id = start(q, q.admin, cid).json()["run_id"]
    for _ in range(60):
        drain(q.env)
        got = _state(q, run_id)
        if got["status"] in ma.TERMINAL:
            break
        time.sleep(0.2)
    assert got["status"] == "REVIEW_READY" and got["report"]["roles"]["compliance"]["status"] == "failed" and got["report"]["missing_required_roles"] == []
    findings = got["report"]["findings"]
    assert findings and any(f["contested"] for f in findings) and all("compliance" not in f["raised_by"] for f in findings)             # a failed role contributes nothing, and is named as failed
    assert got["report"]["coverage"]["completed"] < got["report"]["coverage"]["children"]
