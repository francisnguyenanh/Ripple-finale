"""Real-boundary tests for worker execution and enforced admission.

This file exercises the REAL product boundaries --
the deployed HTTP route (`fastapi.testclient.TestClient(infra.judge.app)`,
same convention as `tests/security/test_ai2_t02_permission.py`) and the real
claim path (`infra/worker/job_worker.py::claim_and_admit_one` against
`PostgresV2Store.claim_outbox`) -- not the internal `job_quota` helpers alone
(those are already covered by `tests/ai2t03/test_quota_and_provider_calls.py`).

Every red/green proof below uses `monkeypatch` to remove the wiring, rather
than hand-editing `infra/**` source and manually restoring it byte-for-byte
(an interrupted or mis-restored manual edit would leave a
production file mutated). `monkeypatch` reverts automatically and
deterministically at test teardown, which gives the same "prove the test is
not vacuous" evidence with none of that restoration risk. No production file
is edited by this test file at any point.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient

from infra import judge
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import PostgresV2Store
from infra.worker import job_quota
from infra.worker.job_worker import claim_and_admit_one


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
def setup(monkeypatch, tmp_path):
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("AI2-T03 e2e tests need a disposable PostgreSQL; set AI2_T03_TEST_DSN")

    nonce = uuid4().hex
    tenant, project, workspace = f"tenant-{nonce}", f"project-{nonce}", f"workspace-{nonce}"
    admin = f"admin-{nonce}"
    requester = f"requester-{nonce}"
    credential = f"cred-{nonce}-distinct"

    store = PostgresV2Store(DSN, MemoryBlobs())
    store.migrate()
    store.create_workspace(tenant, project, workspace)
    digest = store.stage_blob(b"seed artifact for AI2-T03 e2e")
    store.publish_source(tenant, workspace, "rev-0", {"artifact": digest},
                         expected_version=0, operation="source.import.commit",
                         idempotency_key=uuid4().hex)

    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.bootstrap_admin(tenant, project, workspace, admin, credential)
    identities.create_account(requester, credential + "-req")
    identities.grant_membership(tenant, project, workspace, requester, ("requester",))

    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)

    # A budget config with a low provider-call ceiling. "change" job kind's
    # real per-attempt call count defaults to 1 in
    # job_quota.JOB_KIND_REAL_CALLS_PER_ATTEMPT, which would always be
    # admissible on its own -- each test below explicitly monkeypatches that
    # registry entry (a temporary, auto-reverted override) to force the
    # scenario it needs, as an adversarial proof.
    config_path = tmp_path / "budgets.yaml"
    config_path.write_text(
        """
default_model_id: test-model
model_prices:
  test-model:
    input_usd_per_1k: "0.01"
    output_usd_per_1k: "0.01"
max_output_tokens: 2048
max_input_tokens_per_call: 8192
max_retries: 1
max_provider_calls_per_job: 1
per_job_cap_usd: "1.00"
total_cap_usd: "50.00"
fail_closed_on_missing_usage: true
latency_slo_p50_seconds: 5
latency_slo_p95_seconds: 20
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(config_path))

    def browser():
        client = TestClient(judge.app, base_url="https://judge.test")
        client.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
        return client

    return {
        "tenant": tenant, "workspace": workspace, "requester": requester,
        "credential": credential + "-req", "browser": browser, "store": store,
    }


def _login(client, account, credential):
    response = client.post("/v2/auth/login", json={"account_id": account, "credential": credential})
    assert response.status_code == 200, response.text
    return response


def _drain_outbox(store: PostgresV2Store, tenant: str, workspace: str) -> None:
    """Claim and ack every pending outbox event (e.g. the SOURCE_PUBLISHED
    event `setup`'s `publish_source` call leaves behind) so a later
    `claim_outbox`/`claim_and_admit_one` call in a test is guaranteed to see
    the JOB_QUEUED event this test itself creates next, not an older,
    unrelated event (outbox claims are FIFO by `id`)."""
    while True:
        claim = store.claim_outbox(tenant, workspace, "drain-worker", lease_seconds=5)
        if claim is None:
            return
        store.ack_outbox(tenant, workspace, claim.id, "drain-worker", claim.fencing_token)


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


# --------------------------------------------------------------------------- 1a. API create -> 409, zero rows


def test_over_quota_job_creation_returns_409_and_creates_zero_rows(setup, monkeypatch):
    """The adversarial shape: an authenticated
    requester posts a real job whose kind's real call count exceeds the
    configured `max_provider_calls_per_job` ceiling. The route must refuse
    with 409/`budget_exceeded` BEFORE any `v2_jobs` row is written -- not
    merely reject the HTTP response while a row still lands (the bug being guarded against:
    the route created the job regardless, returning 200).
    """
    tenant, workspace, requester, credential, browser = (
        setup[k] for k in ("tenant", "workspace", "requester", "credential", "browser")
    )
    client = browser()
    _login(client, requester, credential)

    # Force the "change" job kind to need 5 real provider calls per attempt,
    # against the fixture's configured ceiling of 1 -- an over-quota job.
    monkeypatch.setitem(job_quota.JOB_KIND_REAL_CALLS_PER_ATTEMPT, "change", 5)

    job_id = f"job-overquota-{uuid4().hex}"
    response = client.post("/v2/jobs", json={"workspace_id": workspace, "kind": "change",
                                             "job_id": job_id, "payload": {}})
    assert response.status_code == 409, response.text
    assert "budget_exceeded" in response.json()["error"]
    assert _row_count(tenant, workspace, job_id) == 0, (
        "the job must NOT be created when admission is refused -- a row here "
        "would reproduce the round-1 'refused in the HTTP response but still "
        "created' defect"
    )


def test_within_quota_job_creation_still_succeeds(setup):
    """Negative control: a job kind within the configured ceiling (the
    fixture's default -- 'change' defaults to 1 real call, ceiling is 1) must
    still be created normally. Quota enforcement must not over-refuse."""
    tenant, workspace, requester, credential, browser = (
        setup[k] for k in ("tenant", "workspace", "requester", "credential", "browser")
    )
    client = browser()
    _login(client, requester, credential)

    job_id = f"job-within-quota-{uuid4().hex}"
    response = client.post("/v2/jobs", json={"workspace_id": workspace, "kind": "change",
                                             "job_id": job_id, "payload": {}})
    assert response.status_code == 200, response.text
    assert _row_count(tenant, workspace, job_id) == 1


def test_route_test_is_not_vacuous_removing_enforcement_makes_it_go_red(setup, monkeypatch):
    """Proves the 409/zero-rows test above would have caught the ROUND-1
    defect ('the deployed POST /v2/jobs handler calls create_job without a
    budget or concurrency check'). Monkeypatch `check_job_admission` in the
    route module's own imported namespace to a no-op -- the exact shape of
    the round-1 bug (the admission function exists but the route never
    effectively enforces it) -- and confirm the job IS created and the route
    returns 200, i.e. this test's own assertions above would fail red against
    that code. Then (via monkeypatch's automatic teardown) the real
    enforcement is restored; a follow-up call in this same test confirms the
    fixed route is green again, with no manual file edit or restoration step
    at any point.
    """
    tenant, workspace, requester, credential, browser = (
        setup[k] for k in ("tenant", "workspace", "requester", "credential", "browser")
    )
    client = browser()
    _login(client, requester, credential)
    monkeypatch.setitem(job_quota.JOB_KIND_REAL_CALLS_PER_ATTEMPT, "change", 5)

    from infra import v2_policy_routes

    # Simulate "enforcement not wired" by making the admission check a no-op,
    # exactly the observable shape of the round-1 defect (the function
    # existed; the route's effective behavior was as if it were never
    # called). A plain manual save/restore (NOT monkeypatch.setattr/undo) is
    # used deliberately here: `monkeypatch.undo()` would revert EVERY patch
    # this test's shared `monkeypatch` fixture instance holds, including the
    # `setup` fixture's `RIPPLE_V2_DSN`/`RIPPLE_BUDGET_CONFIG_PATH` env vars
    # (pytest's `monkeypatch` is function-scoped and shared across a test's
    # whole fixture graph) -- which would break the "green" follow-up call
    # below. A local save/restore only touches this one attribute.
    _original_check = v2_policy_routes.job_quota.check_job_admission
    v2_policy_routes.job_quota.check_job_admission = lambda *a, **k: None
    try:
        red_job_id = f"job-red-{uuid4().hex}"
        red_response = client.post("/v2/jobs", json={"workspace_id": workspace, "kind": "change",
                                                      "job_id": red_job_id, "payload": {}})
        assert red_response.status_code == 200, (
            "with enforcement removed, the route admits the over-quota job -- this "
            "is the RED state: our 409/zero-rows assertions above would fail here"
        )
        assert _row_count(tenant, workspace, red_job_id) == 1, (
            "with enforcement removed, a row IS created -- reproducing the exact "
            "round-1 defect this test suite must catch"
        )
    finally:
        v2_policy_routes.job_quota.check_job_admission = _original_check  # restore, byte-clean

    green_job_id = f"job-green-{uuid4().hex}"
    green_response = client.post("/v2/jobs", json={"workspace_id": workspace, "kind": "change",
                                                    "job_id": green_job_id, "payload": {}})
    assert green_response.status_code == 409, green_response.text
    assert _row_count(tenant, workspace, green_job_id) == 0


# --------------------------------------------------------------------------- 1b. claim-time re-admission


def test_claim_time_admission_cancels_and_acks_an_unadmittable_job(setup, monkeypatch):
    """A job that is admissible at CREATION time (so it is queued normally)
    but becomes unadmittable by the time a worker claims it -- e.g. the
    registry's real-call-count for its kind is corrected upward, or the
    config changes -- must never be left claimable forever. By
    design (`infra/worker/job_worker.py::claim_and_admit_one`),
    the worker must `cancel_job` it and `ack_outbox` the event.
    """
    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    _drain_outbox(store, tenant, workspace)
    job_id = f"job-claim-fail-{uuid4().hex}"
    head = store.head(tenant, workspace)
    store.create_job(tenant, workspace, job_id, "change", {},
                     expected_version=head.version, idempotency_key=f"create-{job_id}")

    # Now simulate the ceiling tightening between creation and claim: "change"
    # jobs suddenly need 5 real calls, which exceeds the fixture's configured
    # ceiling of 1.
    monkeypatch.setitem(job_quota.JOB_KIND_REAL_CALLS_PER_ATTEMPT, "change", 5)

    claim = claim_and_admit_one(store, tenant, workspace, "worker-1")
    assert claim is None, "an unadmittable job must not be returned as claimable/processable"

    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "CANCELLED"
    assert "budget_exceeded" in (record["execution_reason"] or "")

    # The outbox event must be acked (DONE), not left CLAIMED/PENDING for a
    # future worker to pick up forever.
    second_claim = claim_and_admit_one(store, tenant, workspace, "worker-2")
    assert second_claim is None, (
        "nothing should remain claimable for this job after it was cancelled "
        "and acked -- an infinite-retry job would show up here again"
    )


def test_claim_time_admission_passes_an_admissible_job(setup):
    """Companion positive case: a job within its ceiling at claim time must
    be returned normally by `claim_and_admit_one` (not cancelled)."""
    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    _drain_outbox(store, tenant, workspace)
    job_id = f"job-claim-ok-{uuid4().hex}"
    head = store.head(tenant, workspace)
    store.create_job(tenant, workspace, job_id, "change", {},
                     expected_version=head.version, idempotency_key=f"create-{job_id}")

    claim = claim_and_admit_one(store, tenant, workspace, "worker-1")
    assert claim is not None
    assert claim.kind == "JOB_QUEUED"
    assert claim.payload["job_id"] == job_id

    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED", (
        "an admitted, successfully-executed claim must not itself cancel the "
        "job -- and, per IMPLEMENTER round 4's complete_job fix (L-013), it "
        "reaches the COMPLETED terminal state rather than staying QUEUED"
    )


def test_claim_time_check_is_not_vacuous_removing_it_leaves_job_claimable_forever(setup, monkeypatch):
    """Proves the claim-time test above would have caught a regression where
    the worker's claim path stops re-checking admission at all (the same
    class of gap that existed at the CREATE path, but at CLAIM
    time). Monkeypatch `check_job_admission` to a no-op inside the worker
    module's own imported namespace and confirm the unadmittable job is
    returned as claimable and NOT cancelled -- the red state -- then restore
    and confirm the real behavior (cancel + ack, never claimable again).
    """
    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    _drain_outbox(store, tenant, workspace)
    job_id = f"job-claim-redgreen-{uuid4().hex}"
    head = store.head(tenant, workspace)
    store.create_job(tenant, workspace, job_id, "change", {},
                     expected_version=head.version, idempotency_key=f"create-{job_id}")
    monkeypatch.setitem(job_quota.JOB_KIND_REAL_CALLS_PER_ATTEMPT, "change", 5)

    from infra.worker import job_worker

    # Manual save/restore, not monkeypatch.setattr/undo -- see the comment in
    # test_route_test_is_not_vacuous_removing_enforcement_makes_it_go_red
    # above for why (this test's shared monkeypatch fixture also holds the
    # setup fixture's env-var patches, which undo() would also revert).
    _original_check = job_worker.job_quota.check_job_admission
    job_worker.job_quota.check_job_admission = lambda *a, **k: None
    try:
        red_claim = claim_and_admit_one(store, tenant, workspace, "worker-red")
        assert red_claim is not None, (
            "with the claim-time re-check removed, the unadmittable job IS "
            "returned as claimable -- this is the RED state our positive "
            "assertion (claim is None) would fail against"
        )
        assert store.job_record(tenant, workspace, job_id)["execution_status"] == "COMPLETED", (
            "with the claim-time re-check removed, the unadmittable job is "
            "claimed and executed to success like any other admitted job -- "
            "per IMPLEMENTER round 4's complete_job fix (L-013), a "
            "successfully-executed claim now reaches COMPLETED rather than "
            "staying QUEUED; this red-state job is NOT cancelled either way, "
            "which is the actual property this test proves"
        )
    finally:
        job_worker.job_quota.check_job_admission = _original_check  # restore, byte-clean

    # This claim consumed the outbox row under the no-op path; re-queue a
    # fresh job to prove the real (restored) check now closes it correctly.
    job_id_2 = f"job-claim-redgreen-2-{uuid4().hex}"
    head2 = store.head(tenant, workspace)
    store.create_job(tenant, workspace, job_id_2, "change", {},
                     expected_version=head2.version, idempotency_key=f"create-{job_id_2}")
    monkeypatch.setitem(job_quota.JOB_KIND_REAL_CALLS_PER_ATTEMPT, "change", 5)
    green_claim = claim_and_admit_one(store, tenant, workspace, "worker-green")
    assert green_claim is None
    assert store.job_record(tenant, workspace, job_id_2)["execution_status"] == "CANCELLED"
