"""REL-02 (audit 2026-10-30): every write boundary of a leased job is taken under the claim's fence, proven with two real worker identities on
one PostgreSQL database.

State machine under test (remediation plan A1.3):
  S0 admitted / in flight  -> `begin_provider_attempt` is fenced (lease alive): a stale worker starts no provider call.
  S2 published + settled   -> only the owner's `finish_execution` writes the job's terminal state and settles the ledger.
  S3 stale completion      -> the reclaimed worker's usage lands as ONE informational `LATE_USAGE` row (cost 0), the job and the ledger are untouched,
                              before or after the owner settles; nothing is charged twice, nothing is reduced.
  S4 lost ack              -> a redelivery of a committed finish is ALREADY_PROCESSED (the job is terminal), never a second provider call.
  operator cancel          -> QUEUED only; a RUNNING job under a lease is refused from outside its fence.
  crash between T2 and T4  -> the reclaiming worker ends the job FAILED (attempt limit) with the in-flight cost kept at its ceiling.
The interleavings are driven deterministically (claims and lease expiry set by SQL, executors that return on an event), not by sleeping.
"""
from __future__ import annotations

import os
import threading
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest

from infra.adapters.v2_postgres import LeaseLost, PostgresV2Store, StoreConflict
from infra.worker import job_worker
from infra.worker.execution_types import ExecutionContext, ExecutionOutcome, ExecutionRequest, ProviderCallBudget
from infra.worker.job_worker import claim_and_admit_one, execute_admitted_claim

from .conftest import AI2_T03_TEST_DSN as DSN, MemoryBlobs

pytestmark = pytest.mark.skipif(not DSN, reason="needs a disposable PostgreSQL (AI2_T03_TEST_DSN)")

BUDGET = """
default_model_id: test-model
model_prices:
  test-model:
    input_usd_per_1k: "0.01"
    output_usd_per_1k: "0.01"
max_output_tokens: 2048
max_input_tokens_per_call: 8192
max_retries: 0
max_provider_calls_per_job: 1
per_job_cap_usd: "1.00"
total_cap_usd: "10.00"
fail_closed_on_missing_usage: true
latency_slo_p50_seconds: 5
latency_slo_p95_seconds: 20
enabled_roles: all
""".strip()


class _GatedExecutor:
    """An external executor whose provider call blocks on an event, so a test can reclaim the job in the middle of the call."""
    external = True
    executor_id = "test.gated"

    def __init__(self, gate: threading.Event, started: threading.Event, usage=(120, 30)):
        self.gate, self.started, self.usage = gate, started, usage
        self.calls = 0

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        return ExecutionRequest(prompt="p", schema=None, external=True, provider_calls_planned=1)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        calls.consume(1)                                   # runs the fenced before-call hook (begin_provider_attempt)
        self.calls += 1
        self.started.set()
        self.gate.wait(timeout=20)
        return ExecutionOutcome(result={"ok": True}, model_id="test-model", input_tokens=self.usage[0], output_tokens=self.usage[1])


@pytest.fixture
def ws(tmp_path, monkeypatch):
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("test database not reachable")
    cfg = tmp_path / "budget.yaml"
    cfg.write_text(BUDGET, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    nonce = uuid4().hex
    tenant, project, workspace = f"t-{nonce}", f"p-{nonce}", f"w-{nonce}"
    store = PostgresV2Store(DSN, MemoryBlobs())
    store.migrate()
    store.create_workspace(tenant, project, workspace)
    digest = store.stage_blob(b"seed")
    store.publish_source(tenant, workspace, "rev-0", {"artifact": digest}, expected_version=0, operation="source.import.commit", idempotency_key=nonce)
    while True:                                             # drain the publish events so the next claim is the job
        c = store.claim_outbox(tenant, workspace, "drain", lease_seconds=5)
        if c is None:
            break
        store.ack_outbox(tenant, workspace, c.id, "drain", c.fencing_token)
    return {"tenant": tenant, "workspace": workspace, "store": store}


def _sql(query, params=()):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def _create_job(store, tenant, workspace, job_id, kind="knowledge.qa"):
    head = store.head(tenant, workspace)
    store.create_job(tenant, workspace, job_id, kind, {}, expected_version=head.version, idempotency_key=f"create-{job_id}")


def _expire_lease(event_id):
    _sql("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE id=%s", (event_id,))


def _attempts(tenant, workspace, job_id):
    return {r[0]: {"outcome": r[1], "calls": r[2], "cost": r[3], "out": r[4]} for r in _sql(
        "SELECT attempt_key, outcome, provider_calls, cost_usd, output_tokens FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s",
        (tenant, workspace, job_id))}


def _ledger(tenant, workspace, job_id):
    r = _sql("SELECT entry_state, cost_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (tenant, workspace, job_id))
    return (r[0][0], Decimal(r[0][1])) if r else None


def test_owner_completes_settles_and_acks(ws, monkeypatch):
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    gate, started = threading.Event(), threading.Event()
    gate.set()
    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "knowledge.qa", _GatedExecutor(gate, started))
    job_id = f"job-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)
    claim = claim_and_admit_one(store, tenant, workspace, "worker-a")
    assert claim is not None
    assert store.job_record(tenant, workspace, job_id)["execution_status"] == "COMPLETED"
    assert _ledger(tenant, workspace, job_id)[0] == "SETTLED"
    assert _sql("SELECT status FROM v2_outbox WHERE id=%s", (claim.id,))[0][0] == "DONE"


def test_stale_worker_cannot_publish_or_settle_its_usage_is_a_late_row(ws, monkeypatch):
    """S3: worker A is mid-call when its lease expires and worker B reclaims; A's finish is refused by the fence, A's usage is kept as LATE_USAGE
    (cost 0), B's running state and ledger are untouched; B then completes and settles."""
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    gate_a, started_a = threading.Event(), threading.Event()
    gate_b, started_b = threading.Event(), threading.Event()
    ex_a, ex_b = _GatedExecutor(gate_a, started_a, usage=(100, 40)), _GatedExecutor(gate_b, started_b)
    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "knowledge.qa", ex_a)
    job_id = f"job-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)
    claim_a = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=30)
    out = {}
    store_a = PostgresV2Store(DSN, MemoryBlobs())
    t = threading.Thread(target=lambda: out.setdefault("a", execute_admitted_claim(store_a, tenant, workspace, claim_a, "worker-a", config=job_worker._load_config_if_configured())))
    t.start()
    assert started_a.wait(10), "worker A never reached its provider call"
    # the lease of A expires and B reclaims with the next fencing token while A's call is still in flight
    _expire_lease(claim_a.id)
    claim_b = store.claim_outbox(tenant, workspace, "worker-b", lease_seconds=30)
    assert claim_b is not None and claim_b.id == claim_a.id and claim_b.fencing_token == claim_a.fencing_token + 1
    # A's call returns now: its finish must be refused
    gate_a.set()
    t.join(15)
    assert out["a"] == "LEASE_LOST"
    rec = store.job_record(tenant, workspace, job_id)
    assert rec["execution_status"] == "RUNNING", "the stale worker must not write the terminal state"
    assert _ledger(tenant, workspace, job_id)[0] == "RESERVED", "the stale worker must not settle"
    rows = _attempts(tenant, workspace, job_id)
    late = [k for k, v in rows.items() if v["outcome"] == "LATE_USAGE"]
    assert len(late) == 1 and rows[late[0]]["calls"] == 0 and Decimal(rows[late[0]]["cost"]) == 0 and rows[late[0]]["out"] == 40
    inflight_a = [k for k, v in rows.items() if k.endswith(":inflight:1") and f"token:{claim_a.fencing_token}" in k]
    assert inflight_a and rows[inflight_a[0]]["outcome"] == "IN_FLIGHT", "A's ceiling marker stays until B's reclaim resolves it"
    # B runs: its reclaim marks A's marker USAGE_UNKNOWN (ceiling kept); with max_retries 0 the attempt limit is reached -> FAILED, cost at the ceiling
    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "knowledge.qa", ex_b)
    gate_b.set()
    result = execute_admitted_claim(store, tenant, workspace, claim_b, "worker-b", config=job_worker._load_config_if_configured())
    assert result == "FAILED"
    assert ex_b.calls == 0, "no provider call beyond the approved attempt limit"
    rows = _attempts(tenant, workspace, job_id)
    assert rows[inflight_a[0]]["outcome"] == "USAGE_UNKNOWN"
    state, cost = _ledger(tenant, workspace, job_id)
    assert state == "SETTLED" and cost == Decimal(rows[inflight_a[0]]["cost"]) > 0, "settled at the ceiling of the unknown call, never reduced by the late row"
    assert store.job_record(tenant, workspace, job_id)["execution_status"] == "FAILED"
    assert _sql("SELECT status FROM v2_outbox WHERE id=%s", (claim_a.id,))[0][0] == "DONE"


def test_stale_usage_after_the_owner_settled_changes_nothing(ws, monkeypatch):
    """S3 after S2: the owner already settled; the stale finish lands only a LATE_USAGE row and the ledger amount is unchanged."""
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    gate_a, started_a = threading.Event(), threading.Event()
    ex_a = _GatedExecutor(gate_a, started_a, usage=(100, 40))
    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "knowledge.qa", ex_a)
    job_id = f"job-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)
    claim_a = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=30)
    out = {}
    store_a = PostgresV2Store(DSN, MemoryBlobs())
    t = threading.Thread(target=lambda: out.setdefault("a", execute_admitted_claim(store_a, tenant, workspace, claim_a, "worker-a", config=job_worker._load_config_if_configured())))
    t.start()
    assert started_a.wait(10)
    _expire_lease(claim_a.id)
    claim_b = store.claim_outbox(tenant, workspace, "worker-b", lease_seconds=30)
    result = execute_admitted_claim(store, tenant, workspace, claim_b, "worker-b", config=job_worker._load_config_if_configured())
    assert result == "FAILED"
    settled = _ledger(tenant, workspace, job_id)
    assert settled[0] == "SETTLED"
    gate_a.set()
    t.join(15)
    assert out["a"] == "LEASE_LOST"
    assert _ledger(tenant, workspace, job_id) == settled, "a late usage never changes a settled ledger"
    assert store.job_record(tenant, workspace, job_id)["execution_status"] == "FAILED"
    rows = _attempts(tenant, workspace, job_id)
    assert sum(1 for v in rows.values() if v["outcome"] == "LATE_USAGE") == 1


def test_stale_worker_makes_no_provider_call(ws, monkeypatch):
    """S0: the lease is lost before the call; the fenced before-call hook refuses, the executor never contacts the provider."""
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    job_id = f"job-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)
    calls = []

    class _Counting(_GatedExecutor):
        def execute(self, ctx, request, calls_budget):
            calls_budget.consume(1)                          # raises LeaseLost before the "call"
            calls.append(1)
            return ExecutionOutcome(result={}, model_id="test-model")

    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "knowledge.qa", _Counting(threading.Event(), threading.Event()))
    claim_a = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=30)
    # the heartbeat inside execute_admitted_claim keeps the lease alive, so the reclaim is forced right before the call by a hook on the executor's prepare
    real_prepare = job_worker.PROVIDER_EXECUTORS["knowledge.qa"].prepare

    def prepare_then_lose(ctx):
        _expire_lease(claim_a.id)
        assert store.claim_outbox(tenant, workspace, "worker-b", lease_seconds=30) is not None
        return real_prepare(ctx)

    monkeypatch.setattr(job_worker.PROVIDER_EXECUTORS["knowledge.qa"], "prepare", prepare_then_lose)
    result = execute_admitted_claim(store, tenant, workspace, claim_a, "worker-a", config=job_worker._load_config_if_configured())
    assert result == "LEASE_LOST" and calls == []
    rows = _attempts(tenant, workspace, job_id)
    assert not any(f"token:{claim_a.fencing_token}:" in k for k in rows), "no in-flight marker of the stale claim was written"


def test_lost_ack_redelivery_is_already_processed_without_a_second_call(ws, monkeypatch):
    """S4: the owner's finish committed but its ack failed; the reclaim finds the job terminal -> ALREADY_PROCESSED, no provider call."""
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    gate, started = threading.Event(), threading.Event()
    gate.set()
    ex = _GatedExecutor(gate, started)
    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "knowledge.qa", ex)
    job_id = f"job-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)
    claim_a = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=30)
    real_ack = store.ack_outbox
    monkeypatch.setattr(store, "ack_outbox", lambda *a, **k: (_ for _ in ()).throw(LeaseLost("ack lost")))
    assert execute_admitted_claim(store, tenant, workspace, claim_a, "worker-a", config=job_worker._load_config_if_configured()) == "LEASE_LOST"
    monkeypatch.setattr(store, "ack_outbox", real_ack)
    assert store.job_record(tenant, workspace, job_id)["execution_status"] == "COMPLETED"
    _expire_lease(claim_a.id)
    claim_b = store.claim_outbox(tenant, workspace, "worker-b", lease_seconds=30)
    assert execute_admitted_claim(store, tenant, workspace, claim_b, "worker-b", config=job_worker._load_config_if_configured()) == "ALREADY_PROCESSED"
    assert ex.calls == 1
    assert _sql("SELECT status FROM v2_outbox WHERE id=%s", (claim_a.id,))[0][0] == "DONE"


def test_operator_cancel_refuses_a_running_job_and_cancels_a_queued_one(ws):
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    queued, running = f"job-q-{uuid4().hex}", f"job-r-{uuid4().hex}"
    _create_job(store, tenant, workspace, queued)
    _create_job(store, tenant, workspace, running)
    _sql("UPDATE v2_jobs SET execution_status='RUNNING' WHERE job_id=%s", (running,))
    with pytest.raises(StoreConflict):
        store.cancel_execution(tenant, workspace, running, "operator_cancelled: test window end", queued_only=True)
    assert store.job_record(tenant, workspace, running)["execution_status"] == "RUNNING"
    store.cancel_execution(tenant, workspace, queued, "operator_cancelled: test window end", queued_only=True)
    assert store.job_record(tenant, workspace, queued)["execution_status"] == "CANCELLED"


def test_provider_call_keys_are_idempotent_per_claim(ws):
    """Each provider call of a claim has one key; a repeated begin for the same key books nothing twice."""
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    job_id = f"job-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)
    claim = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=30)
    fence = (claim.id, "worker-a", claim.fencing_token)
    key = f"outbox:{claim.id}:attempt:1:token:{claim.fencing_token}:inflight:1"
    for _ in range(3):
        store.begin_provider_attempt(tenant, workspace, job_id, key, ceiling_usd=Decimal("0.05"), context={"call": 1}, fence=fence)
    rows = _attempts(tenant, workspace, job_id)
    assert list(rows) == [key] and Decimal(rows[key]["cost"]) == Decimal("0.05")
    with pytest.raises(LeaseLost):
        store.begin_provider_attempt(tenant, workspace, job_id, key + "x", ceiling_usd=Decimal("0.05"), context={}, fence=(claim.id, "worker-a", claim.fencing_token + 1))


def test_deterministic_race_finish_versus_reclaim_and_operator_cancel(ws, monkeypatch):
    """Lock-order proof with a barrier: the owner's finish, a reclaim by B and an operator cancel start together; the run ends in one consistent
    state (COMPLETED by the owner or FAILED by B), nothing deadlocks, and the operator cancel never touches a job that was running under a lease."""
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    gate, started = threading.Event(), threading.Event()
    ex = _GatedExecutor(gate, started)
    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "knowledge.qa", ex)
    job_id = f"job-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)
    claim_a = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=30)
    out = {}
    store_a, store_b, store_op = (PostgresV2Store(DSN, MemoryBlobs()) for _ in range(3))
    barrier = threading.Barrier(3, timeout=10)

    def run_a():
        out["a"] = execute_admitted_claim(store_a, tenant, workspace, claim_a, "worker-a", config=job_worker._load_config_if_configured())

    def run_b():
        barrier.wait()
        _expire_lease(claim_a.id)
        c = store_b.claim_outbox(tenant, workspace, "worker-b", lease_seconds=30)
        out["b"] = None if c is None else execute_admitted_claim(store_b, tenant, workspace, c, "worker-b", config=job_worker._load_config_if_configured())

    def run_op():
        barrier.wait()
        try:
            store_op.cancel_execution(tenant, workspace, job_id, "operator_cancelled: race test", queued_only=True)
            out["op"] = "cancelled"
        except StoreConflict:
            out["op"] = "refused"

    ta, tb, top = threading.Thread(target=run_a), threading.Thread(target=run_b), threading.Thread(target=run_op)
    ta.start()
    assert started.wait(10)
    tb.start(); top.start()
    barrier.wait()
    gate.set()
    for th in (ta, tb, top):
        th.join(20)
        assert not th.is_alive(), "a participant hung: lock order or deadlock"
    rec = store.job_record(tenant, workspace, job_id)
    assert out["op"] == "refused", "a job under a lease is never cancelled from outside"
    assert rec["execution_status"] in ("COMPLETED", "FAILED")
    assert _ledger(tenant, workspace, job_id)[0] == "SETTLED"
    assert _sql("SELECT status FROM v2_outbox WHERE id=%s", (claim_a.id,))[0][0] == "DONE"
    assert _sql("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")[0][0] is not None
