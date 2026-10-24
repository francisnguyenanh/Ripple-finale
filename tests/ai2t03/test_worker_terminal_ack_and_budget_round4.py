"""Real-boundary tests for executing admitted claims to a terminal ack,
enforcing the project budget, and closing the create-job race.

Exercises:
  (a) claimed job success -> outbox DONE, job terminal success state, no
      double-execution / no permanently-stuck CLAIMED row.
  (b) ProviderExecutionError with retries remaining stays retryable.
  (c) exhausted retries -> CANCELLED + acked, with a red-then-green
      non-vacuity proof reproducing the "admitted claim
      discarded" bug.
  (d) enforce_project_budget blocks POST /v2/jobs (409, zero rows) once
      accumulated ledger spend + worst-case estimate would exceed the
      project's total cap; admits when well under cap.
  (e) the create_job count-then-create race is closed: two real threads
      racing for the last free slot at a configured concurrency ceiling ->
      exactly one succeeds, one is refused with ConcurrencyLimitExceeded.

Every red/green proof below uses a manual save/restore of a module-level
function attribute (never `monkeypatch.setattr(...).undo()`, since this
package's fixtures share one function-scoped `monkeypatch` instance across a
test's whole fixture graph -- see `test_e2e_quota_and_worker.py`'s own
comment on this) or a local stand-in function that never touches
`infra/**` on disk. No production file is edited by this test file.
"""

from __future__ import annotations

import os
import threading
import time
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient

from infra import judge
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import ConcurrencyLimitExceeded, PostgresV2Store
from infra.worker import job_quota, job_worker
from infra.worker.budget_config import BudgetExceeded
from infra.worker.job_worker import (
    ProviderTimeoutError, claim_and_admit_one, execute_admitted_claim,
)


DSN = os.environ.get(
    "AI2_T03_TEST_DSN",
    os.environ.get("AI1_T03_TEST_DSN",
                   "host=127.0.0.1 port=55434 dbname=ai1t04 user=postgres password=localtest"),
)


class MemoryBlobs:
    def __init__(self):
        self.data = {}

    def put_immutable(self, digest, content):
        self.data.setdefault(digest, content)

    def read(self, digest):
        return self.data[digest]


@pytest.fixture
def project(monkeypatch):
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("AI2-T03 round-4 tests need a disposable PostgreSQL; set AI2_T03_TEST_DSN")

    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)

    nonce = uuid4().hex
    tenant, wsproject, workspace = f"tenant-{nonce}", f"project-{nonce}", f"workspace-{nonce}"
    store = PostgresV2Store(DSN, MemoryBlobs())
    store.migrate()
    store.create_workspace(tenant, wsproject, workspace)
    digest = store.stage_blob(b"seed artifact for AI2-T03 round-4")
    store.publish_source(tenant, workspace, "rev-0", {"artifact": digest},
                         expected_version=0, operation="source.import.commit",
                         idempotency_key=uuid4().hex)
    return {"tenant": tenant, "workspace": workspace, "store": store}


def _drain_outbox(store: PostgresV2Store, tenant: str, workspace: str) -> None:
    while True:
        claim = store.claim_outbox(tenant, workspace, "drain-worker", lease_seconds=5)
        if claim is None:
            return
        store.ack_outbox(tenant, workspace, claim.id, "drain-worker", claim.fencing_token)


def _create_job(store, tenant, workspace, job_id, kind="change"):
    head = store.head(tenant, workspace)
    store.create_job(tenant, workspace, job_id, kind, {},
                     expected_version=head.version, idempotency_key=f"create-{job_id}")


def _outbox_status(tenant, workspace, event_id) -> str:
    conn = psycopg2.connect(DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND id=%s",
                       (tenant, workspace, event_id))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- (a) success -> terminal ack

def test_successful_claim_reaches_outbox_done_and_a_second_claim_finds_nothing(project):
    """A claimed job that succeeds via the default provider path must reach
    outbox status DONE, and a second claim attempt on the same workspace must
    find nothing left claimable (no double-execution, no permanently-stuck
    CLAIMED row).

    This ALSO asserts the job itself reaches a terminal success state: the
    success path of `execute_admitted_claim` (`infra/worker/job_worker.py`)
    must not only ack the outbox event and record the job cost but also
    transition the job's execution status away from `QUEUED`, otherwise the
    job row would stay `QUEUED` forever even though the outbox event is `DONE`.
    """
    tenant, workspace, store = project["tenant"], project["workspace"], project["store"]
    _drain_outbox(store, tenant, workspace)
    job_id = f"job-success-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)

    claim = claim_and_admit_one(store, tenant, workspace, "worker-1")
    assert claim is not None
    assert claim.payload["job_id"] == job_id

    assert _outbox_status(tenant, workspace, claim.id) == "DONE", (
        "the outbox event for a successfully executed job must be acked (DONE)"
    )

    second = claim_and_admit_one(store, tenant, workspace, "worker-2")
    assert second is None, (
        "nothing should remain claimable after a successful execution -- a "
        "second non-None claim here would mean double-execution or a "
        "permanently-stuck CLAIMED row"
    )

    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED", (
        f"a successfully executed job must reach execution_status COMPLETED, "
        f"not stay {record['execution_status']!r} forever (round-4 IMPLEMENTER "
        f"added complete_job/finish_execution which sets this independently of "
        f"the human-decision `status` column)."
    )


# --------------------------------------------------------------------------- (b) retryable failure, retries remaining

def test_retryable_provider_failure_with_retries_remaining_stays_retryable(project, monkeypatch):
    """A claim whose executor raises `ProviderExecutionError`, with retries
    still remaining, must stay retryable: the outbox row stays CLAIMED (not
    acked, not DONE, not CANCELLED) and its `attempts` count increments on
    each reclaim -- not silently dropped."""
    tenant, workspace, store = project["tenant"], project["workspace"], project["store"]
    _drain_outbox(store, tenant, workspace)
    job_id = f"job-retry-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)

    def _always_times_out(job_kind, payload):
        raise ProviderTimeoutError("simulated provider timeout")

    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "change", _always_times_out)

    first_claim = store.claim_outbox(tenant, workspace, "worker-1", lease_seconds=1)
    assert first_claim is not None
    assert first_claim.attempts == 1

    outcome = execute_admitted_claim(store, tenant, workspace, first_claim, "worker-1",
                                     lease_seconds=1)
    assert outcome == "RETRY_SCHEDULED", (
        "with the default no-config max_retries fallback (_DEFAULT_MAX_RETRIES=2), "
        "attempts=1 must not yet be exhausted"
    )
    assert _outbox_status(tenant, workspace, first_claim.id) == "CLAIMED", (
        "a retryable failure with retries remaining must leave the outbox row "
        "CLAIMED, never acked/DONE and never CANCELLED -- silently dropping "
        "it would show up here as a status other than CLAIMED"
    )
    assert store.job_record(tenant, workspace, job_id)["status"] == "QUEUED", (
        "the job itself must not be cancelled while retries remain"
    )

    time.sleep(1.2)  # let the 1-second lease expire so the row is reclaimable
    second_claim = store.claim_outbox(tenant, workspace, "worker-2", lease_seconds=30)
    assert second_claim is not None, "the row must be reclaimable after its lease expires"
    assert second_claim.id == first_claim.id
    assert second_claim.attempts == 2, "attempts must increment on each reclaim"


# --------------------------------------------------------------------------- (c) exhausted retries -> CANCELLED, red-then-green

def test_exhausted_retries_end_cancelled_and_acked(project, monkeypatch):
    """A claim that exhausts `max_retries` must end CANCELLED with the
    outbox acked (DONE) -- terminal, never retried forever."""
    tenant, workspace, store = project["tenant"], project["workspace"], project["store"]
    _drain_outbox(store, tenant, workspace)
    job_id = f"job-exhausted-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)

    def _always_fails(job_kind, payload):
        raise ProviderTimeoutError("simulated persistent provider timeout")

    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "change", _always_fails)

    # _DEFAULT_MAX_RETRIES == 2 (no budget config active in this fixture):
    # exhausted once attempts > 2, i.e. on the 3rd attempt.
    claim = store.claim_outbox(tenant, workspace, "worker-1", lease_seconds=1)
    outcome = execute_admitted_claim(store, tenant, workspace, claim, "worker-1", lease_seconds=1)
    assert outcome == "RETRY_SCHEDULED"
    time.sleep(1.2)

    claim2 = store.claim_outbox(tenant, workspace, "worker-2", lease_seconds=1)
    assert claim2.attempts == 2
    outcome2 = execute_admitted_claim(store, tenant, workspace, claim2, "worker-2", lease_seconds=1)
    assert outcome2 == "RETRY_SCHEDULED"
    time.sleep(1.2)

    claim3 = store.claim_outbox(tenant, workspace, "worker-3", lease_seconds=1)
    assert claim3.attempts == 3
    outcome3 = execute_admitted_claim(store, tenant, workspace, claim3, "worker-3", lease_seconds=1)
    assert outcome3 == "CANCELLED"

    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert "provider_execution_failed" in (record["execution_reason"] or "")
    assert _outbox_status(tenant, workspace, claim3.id) == "DONE", (
        "an exhausted-retry job's outbox event must be acked (DONE), not left "
        "CLAIMED forever"
    )

    # Never retried forever: nothing left claimable for this job.
    assert store.claim_outbox(tenant, workspace, "worker-4", lease_seconds=30) is None


def test_terminal_ack_is_not_vacuous_reproduces_the_reviewer_round2_discard_bug(project):
    """Proves the (a)/(c) assertions above are sensitive to the EXACT
    bug ("Successful claims are discarded" --
    `run_worker_loop` claims an admitted job and simply drops it: no
    execution, no ack, no terminal transition). A local stand-in for
    `claim_and_admit_one`'s post-admission behaviour reproduces that literal
    shape (claim, then do nothing) without touching any file on
    disk; shows the outbox row is left permanently CLAIMED (the RED state
    this test suite would have caught); then shows the REAL, unmodified
    `claim_and_admit_one` does not reproduce that on a fresh job (GREEN).
    """
    tenant, workspace, store = project["tenant"], project["workspace"], project["store"]
    _drain_outbox(store, tenant, workspace)

    # --- RED: the literal round-2 shape (admit, then discard) ---
    red_job_id = f"job-discard-red-{uuid4().hex}"
    _create_job(store, tenant, workspace, red_job_id)

    def _round2_shaped_claim_and_discard(store, tenant_id, workspace_id, worker_id, *, lease_seconds=30):
        # This is the original bug: claim, admission
        # re-check only, and then nothing further -- no execute_admitted_claim
        # call at all.
        return store.claim_outbox(tenant_id, workspace_id, worker_id, lease_seconds=lease_seconds)

    red_claim = _round2_shaped_claim_and_discard(store, tenant, workspace, "worker-red", lease_seconds=1)
    assert red_claim is not None
    assert _outbox_status(tenant, workspace, red_claim.id) == "CLAIMED", (
        "sanity check: the round-2-shaped stand-in really does leave the row "
        "CLAIMED (never DONE) -- if this failed, the stand-in above would not "
        "be faithfully reproducing the original defect"
    )
    time.sleep(1.2)
    # Confirm it stays stuck: reclaimable forever, never reaches DONE, exactly
    # the round-2 complaint ("the job remains QUEUED").
    still_stuck = store.claim_outbox(tenant, workspace, "worker-red-2", lease_seconds=1)
    assert still_stuck is not None and still_stuck.id == red_claim.id
    assert store.job_record(tenant, workspace, red_job_id)["status"] == "QUEUED"
    _drain_outbox(store, tenant, workspace)  # clean up the stuck row for later tests

    # --- GREEN: the REAL, unmodified claim_and_admit_one on a fresh job ---
    green_job_id = f"job-discard-green-{uuid4().hex}"
    _create_job(store, tenant, workspace, green_job_id)
    green_claim = claim_and_admit_one(store, tenant, workspace, "worker-green")
    assert green_claim is not None
    assert _outbox_status(tenant, workspace, green_claim.id) == "DONE", (
        "the real claim_and_admit_one must reach a terminal ack, unlike the "
        "round-2-shaped stand-in above"
    )
    assert claim_and_admit_one(store, tenant, workspace, "worker-green-2") is None


# --------------------------------------------------------------------------- (d) project budget cap via the real route

def _budget_yaml(tmp_path, *, total_cap_usd: str, per_job_cap_usd: str = "1.00"):
    # `per_job_cap_usd` must be <= `total_cap_usd` (the shared, unchanged
    # loader's own consistency cross-check) -- pass an explicit, smaller
    # `per_job_cap_usd` when `total_cap_usd` itself is deliberately small.
    config_path = tmp_path / "budgets.yaml"
    config_path.write_text(
        f"""
default_model_id: test-model
model_prices:
  test-model:
    input_usd_per_1k: "0.01"
    output_usd_per_1k: "0.01"
max_output_tokens: 2048
max_input_tokens_per_call: 8192
max_retries: 1
max_provider_calls_per_job: 1
per_job_cap_usd: "{per_job_cap_usd}"
total_cap_usd: "{total_cap_usd}"
fail_closed_on_missing_usage: true
latency_slo_p50_seconds: 5
latency_slo_p95_seconds: 20
enabled_roles: all
""".strip(),
        encoding="utf-8",
    )
    return config_path


@pytest.fixture
def route_project(project, monkeypatch):
    tenant, workspace = project["tenant"], project["workspace"]
    nonce = uuid4().hex
    admin, requester, credential = f"admin-{nonce}", f"requester-{nonce}", f"cred-{nonce}"
    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.bootstrap_admin(tenant, workspace.replace("workspace", "project", 1), workspace, admin, credential)
    identities.create_account(requester, credential + "-req")
    identities.grant_membership(tenant, workspace.replace("workspace", "project", 1), workspace,
                                requester, ("requester", "change_approver"))

    def browser():
        client = TestClient(judge.app, base_url="https://judge.test")
        client.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
        return client

    client = browser()
    response = client.post("/v2/auth/login", json={"account_id": requester, "credential": credential + "-req"})
    assert response.status_code == 200, response.text
    project["client"] = client
    return project


def _row_count(tenant, workspace, job_id) -> int:
    conn = psycopg2.connect(DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s",
                (tenant, workspace, job_id),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def test_analysis_http_submit_and_cancel_during_live_call(route_project, monkeypatch, tmp_path):
    """The mounted API and worker share the pinned input and cancel checkpoint."""
    from types import SimpleNamespace
    from infra.worker import live_analysis

    project = route_project
    store, tenant, workspace, client = (project["store"], project["tenant"],
                                       project["workspace"], project["client"])
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(_budget_yaml(
        tmp_path, total_cap_usd="50.00")))
    job_id = "analysis-http-" + uuid4().hex
    submission = {
        "workspace_id": workspace, "job_id": job_id, "kind": "analysis",
        "payload": {"artifact_id": "artifact"},
    }
    monkeypatch.delenv("RIPPLE_V2_BLOB_BUCKET", raising=False)
    refused = client.post("/v2/jobs", json=submission)
    assert refused.status_code == 503
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s",
                    (tenant, workspace, job_id))
        assert cur.fetchone()[0] == 0
    monkeypatch.setenv("RIPPLE_V2_BLOB_BUCKET", "test-bucket")
    response = client.post("/v2/jobs", json=submission)
    assert response.status_code == 200, response.text
    source_event = store.claim_outbox(tenant, workspace, "analysis-drain")
    assert source_event.kind == "SOURCE_PUBLISHED"
    store.ack_outbox(tenant, workspace, source_event.id, "analysis-drain",
                     source_event.fencing_token)

    provider_calls = []

    def generate_content(**kwargs):
        provider_calls.append(kwargs)
        cancelled = client.post(f"/v2/jobs/{job_id}/cancel",
                                json={"workspace_id": workspace, "reason": "operator_cancelled"})
        assert cancelled.status_code == 200, cancelled.text
        return SimpleNamespace(
            text='{"summary":"review","findings":[]}', model_version="test-model",
            candidates=[SimpleNamespace(finish_reason="STOP")],
            usage_metadata=SimpleNamespace(prompt_token_count=12, candidates_token_count=4),
        )

    provider = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda model: provider)
    claim_and_admit_one(store, tenant, workspace, "analysis-worker")
    record = store.job_record(tenant, workspace, job_id)
    assert len(provider_calls) == 1
    assert provider_calls[0]["config"]["max_output_tokens"] == 2048
    assert record["execution_status"] == "CANCELLED"
    assert record["status"] == "CANCELLED"
    assert record["execution_result"] in ({}, None)


def test_project_budget_serializes_parallel_submits_across_workspaces(project):
    """A project lock, not either workspace lock, owns total admission."""
    tenant, workspace1, store = project["tenant"], project["workspace"], project["store"]
    project_id = workspace1.replace("workspace", "project", 1)
    workspace2 = "second-" + uuid4().hex
    store.create_workspace(tenant, project_id, workspace2)
    digest = store.stage_blob(b"second workspace source")
    store.publish_source(tenant, workspace2, "rev-0", {"artifact": digest},
                         expected_version=0, operation="source.import.commit",
                         idempotency_key=uuid4().hex)
    barrier = threading.Barrier(2)
    outcomes = []

    def submit(workspace):
        candidate = PostgresV2Store(DSN, MemoryBlobs())
        head = candidate.head(tenant, workspace)
        barrier.wait(timeout=5)
        try:
            candidate.create_job(tenant, workspace, "job-" + uuid4().hex,
                                 "change", {}, expected_version=head.version,
                                 idempotency_key=uuid4().hex,
                                 budget_reservation=(Decimal("0.60"), Decimal("1.00")))
            outcomes.append("admitted")
        except BudgetExceeded:
            outcomes.append("refused")

    threads = [threading.Thread(target=submit, args=(workspace,))
               for workspace in (workspace1, workspace2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["admitted", "refused"]
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(SUM(cost_usd), 0) FROM v2_job_cost_ledger
            WHERE tenant_id=%s AND workspace_id IN (%s,%s)""",
                    (tenant, workspace1, workspace2))
        assert cur.fetchone()[0] == Decimal("0.60")


def test_http_submit_reserves_all_provider_retry_attempts(route_project, monkeypatch, tmp_path):
    """A job cannot reserve one call then spend multiple retry attempts."""
    tenant, workspace, client = (route_project["tenant"], route_project["workspace"],
                                 route_project["client"])
    # The configured one-call estimate is 0.1024; two permitted attempts
    # cost 0.2048. A 0.15 cap must refuse before creating a job/outbox.
    config_path = _budget_yaml(tmp_path, total_cap_usd="0.15", per_job_cap_usd="0.15")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RIPPLE_V2_BLOB_BUCKET", "test-bucket")
    job_id = "retry-budget-" + uuid4().hex
    response = client.post("/v2/jobs", json={
        "workspace_id": workspace, "job_id": job_id, "kind": "analysis",
        "payload": {"artifact_id": "artifact"},
    })
    assert response.status_code == 409, response.text
    assert "budget_exceeded" in response.text
    assert _row_count(tenant, workspace, job_id) == 0


def test_project_budget_blocks_job_creation_once_accumulated_spend_plus_estimate_exceeds_cap(
    route_project, monkeypatch, tmp_path,
):
    """Once accumulated ledger spend + this job's worst-case estimate would
    exceed the project's configured total cap, `POST /v2/jobs` must return
    409 budget_exceeded and create ZERO rows for the refused job_id."""
    tenant, workspace, client = (
        route_project[k] for k in ("tenant", "workspace", "client")
    )
    config_path = _budget_yaml(tmp_path, total_cap_usd="0.20", per_job_cap_usd="0.11")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("RIPPLE_V2_DSN", raising=False)
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)

    # Seed prior accumulated spend for a DIFFERENT job in the same project:
    # worst-case estimate is (8192+2048)/1000 * 0.01 = 0.10240 (matching
    # _budget_yaml's fixture prices); seed 0.15 so 0.15 + 0.10240 > 0.20 cap.
    job_quota.record_job_cost(tenant_id=tenant, workspace_id=workspace,
                              job_id="prior-job", cost_usd="0.15")

    job_id = f"job-over-budget-{uuid4().hex}"
    response = client.post("/v2/jobs", json={"workspace_id": workspace, "kind": "change",
                                             "job_id": job_id, "payload": {}})
    assert response.status_code == 409, response.text
    assert "budget_exceeded" in response.json()["error"]
    assert _row_count(tenant, workspace, job_id) == 0, (
        "a project-budget refusal must create zero rows for the refused job, "
        "not merely reject the HTTP response"
    )


def test_retry_route_does_not_recheck_project_budget_cap_round7_open_item(
    route_project, monkeypatch, tmp_path,
):
    """Retry must not re-enqueue WITHOUT re-checking/reserving a fresh budget cap.

    Reads of `PostgresV2Store.retry_job` (infra/adapters/v2_postgres.py:980)
    and `retry_job_route` (infra/v2_policy_routes.py:321) confirm: retry_job
    only flips execution_status FAILED->QUEUED and inserts a fresh outbox
    row. It never calls `job_quota.check_job_admission` or
    `job_quota.enforce_project_budget` -- unlike `create_job`'s route
    (`infra/v2_policy_routes.py:120`, which does call `check_job_admission`
    on every fresh POST /v2/jobs). There is also no re-check against the
    project's accumulated ledger spend before re-enqueuing.

    This test uses the real HTTP route: a
    project already at/over its configured total budget cap can still have a
    genuinely-FAILED job retried through `POST /v2/jobs/{id}/retry` with a
    200, no budget refusal at all. This assertion is written to the SECURE
    expectation (retry should be refused once the project is over cap, same
    as a fresh create would be) and is deliberately not weakened to pass
    against a retry path that skips the budget check.
    """
    tenant, workspace, client = (
        route_project[k] for k in ("tenant", "workspace", "client")
    )
    config_path = _budget_yaml(tmp_path, total_cap_usd="50.00")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("RIPPLE_V2_DSN", raising=False)
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)

    store = route_project["store"]
    _drain_outbox(store, tenant, workspace)

    # Create a job within cap, then force it to genuinely FAIL (exhaust
    # retries via a permanently-failing executor), exactly like
    # test_exhausted_retries_end_cancelled_and_acked above -- except this
    # config's max_retries=1 (from _budget_yaml), so exhaustion happens on
    # the 2nd attempt.
    job_id = f"job-retry-budget-{uuid4().hex}"
    response = client.post("/v2/jobs", json={"workspace_id": workspace, "kind": "change",
                                             "job_id": job_id, "payload": {}})
    assert response.status_code == 200, response.text

    def _always_fails(job_kind, payload):
        raise ProviderTimeoutError("simulated persistent provider timeout")

    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "change", _always_fails)

    claim1 = store.claim_outbox(tenant, workspace, "worker-1", lease_seconds=1)
    outcome1 = execute_admitted_claim(store, tenant, workspace, claim1, "worker-1", lease_seconds=1)
    assert outcome1 == "RETRY_SCHEDULED"
    time.sleep(1.2)
    claim2 = store.claim_outbox(tenant, workspace, "worker-2", lease_seconds=1)
    outcome2 = execute_admitted_claim(store, tenant, workspace, claim2, "worker-2", lease_seconds=1)
    if outcome2 == "RETRY_SCHEDULED":
        time.sleep(1.2)
        claim3 = store.claim_outbox(tenant, workspace, "worker-3", lease_seconds=1)
        outcome2 = execute_admitted_claim(store, tenant, workspace, claim3, "worker-3", lease_seconds=1)
    assert outcome2 == "CANCELLED"
    assert store.job_record(tenant, workspace, job_id)["execution_status"] == "FAILED"

    # Now blow the project's total cap with unrelated recorded spend, far
    # beyond the 50.00 configured total_cap_usd -- a fresh POST /v2/jobs in
    # this project would be refused 409 budget_exceeded right now.
    job_quota.record_job_cost(tenant_id=tenant, workspace_id=workspace,
                              job_id="other-job-blowing-the-cap", cost_usd="49.99")
    control = client.post("/v2/jobs", json={"workspace_id": workspace, "kind": "change",
                                            "job_id": f"job-control-{uuid4().hex}", "payload": {}})
    assert control.status_code == 409, (
        "sanity check: the project really is over its total cap right now -- "
        "a fresh create_job is refused, confirming the retry call below is a "
        "fair comparison, not a fixture mistake"
    )

    retry_response = client.post(f"/v2/jobs/{job_id}/retry", json={"workspace_id": workspace})
    assert retry_response.status_code == 409, (
        f"GENUINE PRODUCT GAP (round-7 TESTER, recommend REWORK(IMPL)): "
        f"retry_job_route re-enqueues a job with NO re-check of the "
        f"project's accumulated budget at all -- got "
        f"{retry_response.status_code} {retry_response.text!r} while a fresh "
        f"create_job in the identical over-cap project is correctly refused "
        f"409 above. A workspace can therefore retry a failed job an "
        f"unbounded number of times even after the project has already "
        f"exceeded its configured total_cap_usd, silently accruing more "
        f"spend with no admission gate."
    )
    assert store.job_record(tenant, workspace, job_id)["execution_status"] == "FAILED"
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM v2_outbox
            WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED'
              AND payload->>'job_id'=%s""", (tenant, workspace, job_id))
        assert cur.fetchone()[0] == 1, "refused retry must not create another outbox event"


def test_retry_with_remaining_project_budget_requeues_and_reserves_atomically(
    route_project, monkeypatch, tmp_path,
):
    tenant, workspace, client = (
        route_project[k] for k in ("tenant", "workspace", "client")
    )
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH",
                       str(_budget_yaml(tmp_path, total_cap_usd="1.00")))
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    store = route_project["store"]
    job_id = f"job-retry-admitted-{uuid4().hex}"
    created = client.post("/v2/jobs", json={"workspace_id": workspace,
                                            "kind": "change", "job_id": job_id,
                                            "payload": {}})
    assert created.status_code == 200, created.text
    store.fail_execution(tenant, workspace, job_id, "transient_failure")

    retried = client.post(f"/v2/jobs/{job_id}/retry", json={"workspace_id": workspace})
    assert retried.status_code == 200, retried.text
    assert store.job_record(tenant, workspace, job_id)["execution_status"] == "QUEUED"
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""SELECT cost_usd, reserved_usd, entry_state
            FROM v2_job_cost_ledger
            WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
            (tenant, workspace, job_id))
        cost, reserved, state = cur.fetchone()
        assert cost > 0 and cost == reserved and state == "RESERVED"
        cur.execute("""SELECT count(*) FROM v2_outbox
            WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED'
              AND payload->>'job_id'=%s""", (tenant, workspace, job_id))
        assert cur.fetchone()[0] == 2


def test_retry_rechecks_current_per_job_cost_cap(route_project, monkeypatch, tmp_path):
    tenant, workspace, client = (
        route_project[k] for k in ("tenant", "workspace", "client")
    )
    config_path = _budget_yaml(tmp_path, total_cap_usd="1.00")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    store = route_project["store"]
    job_id = f"job-retry-per-job-{uuid4().hex}"
    created = client.post("/v2/jobs", json={"workspace_id": workspace,
                                            "kind": "change", "job_id": job_id,
                                            "payload": {}})
    assert created.status_code == 200, created.text
    store.fail_execution(tenant, workspace, job_id, "transient_failure")

    # Config can tighten after the original admission. Worst-case call cost
    # is 0.10240, so this cap must reject the next execution attempt.
    _budget_yaml(tmp_path, total_cap_usd="1.00", per_job_cap_usd="0.05")
    retried = client.post(f"/v2/jobs/{job_id}/retry", json={"workspace_id": workspace})
    assert retried.status_code == 409, retried.text
    assert store.job_record(tenant, workspace, job_id)["execution_status"] == "FAILED"
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM v2_outbox
            WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED'
              AND payload->>'job_id'=%s""", (tenant, workspace, job_id))
        assert cur.fetchone()[0] == 1


def test_two_retries_racing_for_one_remaining_budget_slot_admit_one(project):
    tenant, workspace, store = (
        project[k] for k in ("tenant", "workspace", "store")
    )
    job_ids = [f"retry-race-{uuid4().hex}" for _ in range(2)]
    for job_id in job_ids:
        _create_job(store, tenant, workspace, job_id)
        store.fail_execution(tenant, workspace, job_id, "transient_failure")

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def racer(job_id):
        try:
            barrier.wait(timeout=5)
            store.retry_job(tenant, workspace, job_id,
                            budget_reservation=(Decimal("0.10"), Decimal("0.15")))
            results.append(job_id)
        except BudgetExceeded:
            errors.append(job_id)

    threads = [threading.Thread(target=racer, args=(job_id,)) for job_id in job_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "retry admission must not deadlock"

    assert len(results) == len(errors) == 1
    assert store.job_record(tenant, workspace, results[0])["execution_status"] == "QUEUED"
    assert store.job_record(tenant, workspace, errors[0])["execution_status"] == "FAILED"
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(SUM(cost_usd), 0)
            FROM v2_job_cost_ledger
            WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s)""",
            (tenant, workspace, job_ids))
        assert cur.fetchone()[0] == Decimal("0.10")


def test_retry_reservation_settles_to_cumulative_attempt_cost(project):
    tenant, workspace, store = (
        project[k] for k in ("tenant", "workspace", "store")
    )
    job_id = f"retry-settlement-{uuid4().hex}"
    _create_job(store, tenant, workspace, job_id)
    store.finish_execution(tenant, workspace, job_id, "FAILED",
                           reason="provider_timeout",
                           attempt={"attempt_key": "first", "outcome": "FAILED",
                                    "request_context": {}, "cost_usd": Decimal("0.04")})
    store.retry_job(tenant, workspace, job_id,
                    budget_reservation=(Decimal("0.10"), Decimal("0.20")))
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""SELECT cost_usd, reserved_usd, entry_state
            FROM v2_job_cost_ledger
            WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
            (tenant, workspace, job_id))
        assert cur.fetchone() == (Decimal("0.14"), Decimal("0.14"), "RESERVED")

    store.finish_execution(tenant, workspace, job_id, "COMPLETED",
                           result={"ok": True},
                           attempt={"attempt_key": "second", "outcome": "COMPLETED",
                                    "request_context": {}, "cost_usd": Decimal("0.02")})
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""SELECT cost_usd, entry_state FROM v2_job_cost_ledger
            WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
            (tenant, workspace, job_id))
        assert cur.fetchone() == (Decimal("0.06"), "SETTLED")


def test_project_budget_admits_when_well_under_cap(route_project, monkeypatch, tmp_path):
    """Positive control: a generous total cap with no prior spend must still
    admit a normal job."""
    tenant, workspace, client = (
        route_project[k] for k in ("tenant", "workspace", "client")
    )
    config_path = _budget_yaml(tmp_path, total_cap_usd="50.00")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("RIPPLE_V2_DSN", raising=False)
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)

    job_id = f"job-under-budget-{uuid4().hex}"
    response = client.post("/v2/jobs", json={"workspace_id": workspace, "kind": "change",
                                             "job_id": job_id, "payload": {}})
    assert response.status_code == 200, response.text
    assert _row_count(tenant, workspace, job_id) == 1


# --------------------------------------------------------------------------- (e) concurrency race, real threads

def test_concurrency_ceiling_race_exactly_one_succeeds_with_one_free_slot(project):
    """Two real threads/connections both attempt `create_job` at a configured
    concurrency ceiling with exactly ONE free slot: exactly one must succeed
    and the other must be rejected with `ConcurrencyLimitExceeded` -- never
    both admitted (the count-then-create TOCTOU gap)."""
    tenant, workspace = project["tenant"], project["workspace"]

    # One job already active (fills the first of a 2-job ceiling), leaving
    # exactly one free slot for the two racing threads below.
    existing_job_id = f"job-existing-{uuid4().hex}"
    store0 = PostgresV2Store(DSN, MemoryBlobs())
    _create_job(store0, tenant, workspace, existing_job_id)
    assert store0.job_record(tenant, workspace, existing_job_id)["status"] == "QUEUED"

    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _racer(job_id: str):
        store = PostgresV2Store(DSN, MemoryBlobs())
        try:
            barrier.wait(timeout=5)
            head = store.head(tenant, workspace)
            store.create_job(tenant, workspace, job_id, "change", {},
                             expected_version=head.version,
                             idempotency_key=f"create-{job_id}",
                             max_concurrent_jobs=2)
            results.append("ADMITTED")
        except ConcurrencyLimitExceeded:
            results.append("REFUSED")
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    job_a, job_b = f"job-race-a-{uuid4().hex}", f"job-race-b-{uuid4().hex}"
    threads = [threading.Thread(target=_racer, args=(job_a,)),
              threading.Thread(target=_racer, args=(job_b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"a racing create_job call raised an unexpected exception: {errors!r}"
    assert len(results) == 2
    assert sorted(results) == ["ADMITTED", "REFUSED"], (
        f"with exactly one free slot at the ceiling, exactly one racer must be "
        f"admitted and the other refused -- got {results!r} (two ADMITTED would "
        f"reproduce the count-then-create TOCTOU race; two REFUSED would be an "
        f"over-refusal bug)"
    )


def test_concurrency_race_is_not_vacuous_a_separate_transaction_count_reproduces_the_toctou_race(project):
    """Proves the atomicity test above would have caught the
    bug: a stand-in that reads the active-job count in a SEPARATE
    transaction from the insert (the literal defect:
    "the count of active jobs is read separately from store.create_job") lets
    two racing callers both observe the count below the ceiling and both
    insert -- the exact TOCTOU race the atomic `max_concurrent_jobs` argument
    fixes. Defined entirely as a local stand-in; no production file is
    touched."""
    tenant, workspace = project["tenant"], project["workspace"]
    ceiling = 1

    def _racer_toctou(job_id: str, barrier: threading.Barrier, results: list, errors: list):
        store = PostgresV2Store(DSN, MemoryBlobs())
        try:
            barrier.wait(timeout=5)
            # SEPARATE transaction count read (the round-2 shape) ...
            currently_running = store.count_active_jobs(tenant, workspace)
            time.sleep(0.05)  # widen the TOCTOU window deterministically
            if currently_running >= ceiling:
                results.append("REFUSED")
                return
            # ... then a SEPARATE transaction insert, with no atomic guard.
            head = store.head(tenant, workspace)
            store.create_job(tenant, workspace, job_id, "change", {},
                             expected_version=head.version,
                             idempotency_key=f"create-{job_id}")
            results.append("ADMITTED")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []
    job_a, job_b = f"job-toctou-a-{uuid4().hex}", f"job-toctou-b-{uuid4().hex}"
    threads = [threading.Thread(target=_racer_toctou, args=(job_a, barrier, results, errors)),
              threading.Thread(target=_racer_toctou, args=(job_b, barrier, results, errors))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert results.count("ADMITTED") == 2, (
        "sanity check: the round-2-shaped separate-transaction stand-in really "
        "does let both racers through at a ceiling of 1 -- this is the RED "
        "state the real `max_concurrent_jobs` atomic path (test above) does "
        "not reproduce"
    )

    # GREEN: the REAL atomic path on the identical race shape refuses one.
    real_results: list[str] = []
    real_errors: list[BaseException] = []
    real_barrier = threading.Barrier(2)

    def _real_racer(job_id: str):
        store = PostgresV2Store(DSN, MemoryBlobs())
        try:
            real_barrier.wait(timeout=5)
            head = store.head(tenant, workspace)
            store.create_job(tenant, workspace, job_id, "change", {},
                             expected_version=head.version,
                             idempotency_key=f"create-{job_id}",
                             max_concurrent_jobs=ceiling + 2)  # +2 accounts for the 2 admitted above
            real_results.append("ADMITTED")
        except ConcurrencyLimitExceeded:
            real_results.append("REFUSED")
        except BaseException as exc:  # noqa: BLE001
            real_errors.append(exc)

    job_c, job_d = f"job-toctou-c-{uuid4().hex}", f"job-toctou-d-{uuid4().hex}"
    threads2 = [threading.Thread(target=_real_racer, args=(job_c,)),
               threading.Thread(target=_real_racer, args=(job_d,))]
    for t in threads2:
        t.start()
    for t in threads2:
        t.join(timeout=10)
    assert not real_errors, real_errors
    assert sorted(real_results) == ["ADMITTED", "REFUSED"], (
        f"the real atomic max_concurrent_jobs path must refuse exactly one "
        f"racer at the ceiling, unlike the TOCTOU stand-in above -- got {real_results!r}"
    )


def test_cancelling_a_job_that_never_ran_releases_its_reservation(project):
    """Cloud finding: the reservation of a cancelled, never-run job stayed RESERVED and kept counting against the cap."""
    tenant, workspace, store = (project[k] for k in ("tenant", "workspace", "store"))
    job_id = f"cancel-release-{uuid4().hex}"
    head = store.head(tenant, workspace)
    store.create_job(tenant, workspace, job_id, "change", {}, expected_version=head.version, idempotency_key=f"create-{job_id}",
                     budget_reservation=(Decimal("0.40"), Decimal("5.00")))
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT entry_state, cost_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (tenant, workspace, job_id))
        assert cur.fetchone() == ("RESERVED", Decimal("0.40"))
    store.request_cancel(tenant, workspace, job_id, "cancelled by a person before it ran")
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT entry_state, cost_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (tenant, workspace, job_id))
        assert cur.fetchone() == ("SETTLED", Decimal("0"))               # nothing held, nothing spent
