"""Failure-injection tests for the
publish-race and self-approval-audit-atomicity defects, against
the fix (`PostgresV2Store.recheck_and_publish_job` and
`PostgresV2Store.approve_job(..., self_approval_audit=...)`,
`infra/adapters/v2_postgres.py`).

These are genuine concurrency/failure-injection tests, not unit tests: test 1
uses two real threads and two real PostgreSQL connections with a runtime hook
that pauses the publish transaction mid-flight so a concurrent revoke can be
issued while the publish transaction still holds its row locks (the same
technique used against the pre-fix code, applied here to
prove the fix closes the window). The hook is installed by wrapping
`psycopg2.connect` for the duration of one test via `monkeypatch` (psycopg2's
C `cursor`/`connection` types are immutable and cannot be patched directly);
it is automatically reverted at test teardown. No production file is edited
by this file.

Uses the same disposable PostgreSQL 16 server as the rest of the security tests.
"""

from __future__ import annotations

import os
import threading
from uuid import uuid4

import psycopg2
import pytest

from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import PostgresV2Store, StoreConflict
from infra.worker.job_worker import claim_and_admit_one


DSN = os.environ.get(
    "AI2_T02_TEST_DSN",
    "host=127.0.0.1 port=55434 dbname=ai1t04 user=postgres password=localtest",
)


class MemoryBlobs:
    def __init__(self):
        self.data = {}

    def put_immutable(self, digest, content):
        self.data.setdefault(digest, content)

    def read(self, digest):
        return self.data[digest]


# ---------------------------------------------------------------------------
# Runtime hook plumbing: wrap psycopg2.connect so every cursor.execute() call
# on every connection opened while the hook is installed passes through a
# test-supplied callback first. psycopg2's cursor/connection are C types and
# cannot be monkeypatched in place (`cannot set 'execute' attribute of
# immutable type`), so this proxies at the connect() boundary instead.
# ---------------------------------------------------------------------------

def _exec(real_cursor, query, vars):
    return real_cursor.execute(query) if vars is None else real_cursor.execute(query, vars)


class _CursorProxy:
    def __init__(self, real, hook):
        self._real = real
        self._hook = hook

    def execute(self, query, vars=None):
        return self._hook(self._real, query, vars)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _ConnProxy:
    def __init__(self, real, hook):
        self._real = real
        self._hook = hook

    def cursor(self, *args, **kwargs):
        return _CursorProxy(self._real.cursor(*args, **kwargs), self._hook)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def install_execute_hook(monkeypatch, hook):
    """Every `cursor.execute(query, vars)` call on any connection opened after
    this returns is routed through `hook(real_cursor, query, vars)`, which
    must itself call/forward to the real cursor (see `_exec`) unless it means
    to inject a failure instead."""
    real_connect = psycopg2.connect

    def connect(*args, **kwargs):
        return _ConnProxy(real_connect(*args, **kwargs), hook)

    monkeypatch.setattr(psycopg2, "connect", connect)


@pytest.fixture
def setup():
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("AI2-T02 needs disposable PostgreSQL; set AI2_T02_TEST_DSN")

    nonce = uuid4().hex
    tenant, project, workspace = f"tenant-{nonce}", f"project-{nonce}", f"workspace-{nonce}"
    admin = f"admin-{nonce}"
    requester, approver = f"requester-{nonce}", f"approver-{nonce}"
    credentials = {name: f"cred-{name}-{nonce}-distinct" for name in (admin, requester, approver)}

    storage = PostgresV2Store(DSN, MemoryBlobs())
    storage.migrate()
    storage.create_workspace(tenant, project, workspace)
    digest = storage.stage_blob(b"seed artifact")
    storage.publish_source(tenant, workspace, "rev-0", {"artifact": digest},
                           expected_version=0, operation="source.import.commit",
                           idempotency_key=uuid4().hex)

    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.bootstrap_admin(tenant, project, workspace, admin, credentials[admin])
    for account in (requester, approver):
        identities.create_account(account, credentials[account])
    identities.grant_membership(tenant, project, workspace, requester, ("requester",))
    identities.grant_membership(tenant, project, workspace, approver, ("change_approver",))

    return locals()


def _make_approved_job(setup, job_id: str) -> None:
    """Drive a job to APPROVED directly at the store level (no HTTP), so
    these tests exercise `PostgresV2Store` itself, matching the interface
    that is the permanent one."""
    storage, tenant, workspace = setup["storage"], setup["tenant"], setup["workspace"]
    requester, approver = setup["requester"], setup["approver"]
    head = storage.head(tenant, workspace)
    storage.create_job(tenant, workspace, job_id, "change", {},
                       expected_version=head.version, idempotency_key=f"create-{job_id}")
    storage.set_job_requester(tenant, workspace, job_id, requester)
    storage.approve_job(tenant, workspace, job_id, approver, ("change_approver",))


def _run_job_to_completion(store, tenant, workspace, job_id, *, max_claims=10):
    """Drive job_id through the real worker claim path until its
    execution_status is COMPLETED -- production now requires a completed
    execution result before recheck_and_publish_job will publish."""
    for _ in range(max_claims):
        record = store.job_record(tenant, workspace, job_id)
        if record["execution_status"] == "COMPLETED":
            return
        claim = claim_and_admit_one(store, tenant, workspace, "worker-" + uuid4().hex)
        if claim is None:
            break
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED", (
        f"test setup failed to drive job {job_id} to COMPLETED execution "
        f"(stuck at execution_status={record['execution_status']!r})"
    )


# ---------------------------------------------------------------------------
# 1. Publish race: a concurrent revoke lands INSIDE the publish transaction's
#    lock window, between its job-row lock and its final commit.
# ---------------------------------------------------------------------------

def test_publish_race_concurrent_revoke_mid_transaction_blocks_publish(setup, monkeypatch):
    identities = setup["identities"]
    tenant, project, workspace = setup["tenant"], setup["project"], setup["workspace"]
    approver = setup["approver"]
    job_id = f"job-{uuid4().hex}"
    _make_approved_job(setup, job_id)
    _run_job_to_completion(setup["storage"], tenant, workspace, job_id)

    reached_after_job_lock = threading.Event()
    revoke_done = threading.Event()

    def hook(real_cursor, query, vars):
        result = _exec(real_cursor, query, vars)
        if (isinstance(query, str) and "FROM v2_jobs" in query and "FOR UPDATE" in query
                and vars and vars[-1] == job_id and not reached_after_job_lock.is_set()):
            # The publish transaction has just locked the job row (step 1 of
            # `recheck_and_publish_job`) but has not yet locked the approver's
            # account/membership rows or written PUBLISHED. Signal the main
            # thread to issue a real concurrent revoke right now, on its own
            # separate connection, and wait for it to land before this
            # transaction proceeds -- this is the exact mid-transaction
            # window a concurrent revoke could exploit against the
            # pre-fix two-transaction publish (`identity.current_authority`
            # read, then a separate `mark_job_published` write).
            reached_after_job_lock.set()
            revoke_done.wait(timeout=10)
        return result

    install_execute_hook(monkeypatch, hook)

    store_for_thread = PostgresV2Store(DSN, MemoryBlobs())
    outcome: dict = {}

    def run_publish():
        try:
            store_for_thread.recheck_and_publish_job(tenant, workspace, job_id, "change.approve")
            outcome["result"] = "published"
        except StoreConflict as exc:
            outcome["result"] = "blocked"
            outcome["error"] = str(exc)
        except Exception as exc:  # pragma: no cover - would be a real defect
            outcome["result"] = "error"
            outcome["error"] = repr(exc)

    thread = threading.Thread(target=run_publish)
    thread.start()
    assert reached_after_job_lock.wait(timeout=10), "publish transaction never reached the hook"

    # Real concurrent revoke, issued from the main thread on its own
    # connection, while the publish transaction above still holds the job
    # row lock and has not yet locked/checked the approver's membership row.
    # `revoke_membership` opens a fresh, unhooked connection via the
    # (unpatched-in-this-thread) real psycopg2.connect it captured is not
    # relevant here: it goes through the same patched `psycopg2.connect`,
    # but its DELETE only touches v2_memberships, which the publish
    # transaction has not locked yet, so it completes immediately.
    identities.revoke_membership(tenant, project, workspace, approver)
    revoke_done.set()
    thread.join(timeout=15)
    assert not thread.is_alive(), "publish transaction hung instead of resolving"

    assert outcome.get("result") == "blocked", outcome
    assert "revoked" in outcome["error"] or "permission" in outcome["error"]

    status = setup["storage"].job_status(tenant, workspace, job_id)
    assert status["status"] == "APPROVED", "job must not have been silently PUBLISHED"


def test_publish_race_regression_revoke_strictly_before_check_still_blocks(setup):
    """Non-concurrent regression: a revoke fully committed before publish is
    attempted must still block it (the case the pre-fix two-transaction code
    also caught; proves the round-2 fix did not regress the simple case while
    closing the harder mid-transaction race above)."""
    identities = setup["identities"]
    tenant, project, workspace = setup["tenant"], setup["project"], setup["workspace"]
    approver = setup["approver"]
    job_id = f"job-{uuid4().hex}"
    _make_approved_job(setup, job_id)
    _run_job_to_completion(setup["storage"], tenant, workspace, job_id)

    identities.revoke_membership(tenant, project, workspace, approver)

    with pytest.raises(StoreConflict, match="revoked|permission"):
        setup["storage"].recheck_and_publish_job(tenant, workspace, job_id, "change.approve")
    status = setup["storage"].job_status(tenant, workspace, job_id)
    assert status["status"] == "APPROVED"


def test_publish_succeeds_when_approver_authority_is_never_touched(setup):
    """Control: with no revoke at all, the same atomic path still publishes."""
    tenant, workspace = setup["tenant"], setup["workspace"]
    job_id = f"job-{uuid4().hex}"
    _make_approved_job(setup, job_id)
    _run_job_to_completion(setup["storage"], tenant, workspace, job_id)
    setup["storage"].recheck_and_publish_job(tenant, workspace, job_id, "change.approve")
    status = setup["storage"].job_status(tenant, workspace, job_id)
    assert status["status"] == "PUBLISHED"


# ---------------------------------------------------------------------------
# 2. Self-approval audit atomicity: an injected failure during the audit
#    INSERT must roll back the ENTIRE approve_job transaction.
# ---------------------------------------------------------------------------

def test_self_approval_audit_write_failure_rolls_back_whole_approval(setup, monkeypatch):
    storage, tenant, workspace = setup["storage"], setup["tenant"], setup["workspace"]
    requester = setup["requester"]

    head = storage.head(tenant, workspace)
    job_id = f"job-{uuid4().hex}"
    storage.create_job(tenant, workspace, job_id, "change", {},
                       expected_version=head.version, idempotency_key=f"create-{job_id}")
    storage.set_job_requester(tenant, workspace, job_id, requester)

    def hook(real_cursor, query, vars):
        if isinstance(query, str) and "job.self_approval" in query:
            # Runtime-only injected failure inside the SAME transaction as
            # the state-transition UPDATE, right at the audit write step --
            # the exact spot that broke the
            # pre-fix two-transaction `approve_job` +
            # `_record_self_approval_audit` (a separate `store._tx()` call).
            raise psycopg2.OperationalError("injected: audit write failure")
        return _exec(real_cursor, query, vars)

    install_execute_hook(monkeypatch, hook)

    with pytest.raises(psycopg2.OperationalError):
        storage.approve_job(tenant, workspace, job_id, requester, ("requester", "change_approver"),
                            self_approval_audit={"job_id": job_id, "account_id": requester})

    # `monkeypatch` reverts `psycopg2.connect` at test teardown; the
    # assertions below intentionally still run with the hook installed (it
    # only reacts to the literal 'job.self_approval' INSERT, so ordinary
    # SELECTs pass straight through to `_exec`).
    status = storage.job_status(tenant, workspace, job_id)
    assert status["status"] == "PENDING_APPROVAL", (
        "approval state transition must have rolled back with the failed audit write")

    events = [e for e in storage.audit_events(tenant, workspace)
              if e["event_type"] == "job.self_approval" and e["payload"].get("job_id") == job_id]
    assert events == [], "no partial self_approval audit row may survive a rolled-back transaction"


def test_self_approval_audit_happy_path_records_exactly_one_row(setup):
    """Regression: the ordinary self-approval path still both approves the
    job and writes exactly one audit row per self-approval."""
    storage, tenant, workspace = setup["storage"], setup["tenant"], setup["workspace"]
    requester = setup["requester"]

    head = storage.head(tenant, workspace)
    job_id = f"job-{uuid4().hex}"
    storage.create_job(tenant, workspace, job_id, "change", {},
                       expected_version=head.version, idempotency_key=f"create-{job_id}")
    storage.set_job_requester(tenant, workspace, job_id, requester)

    storage.approve_job(tenant, workspace, job_id, requester, ("requester", "change_approver"),
                        self_approval_audit={"job_id": job_id, "account_id": requester})

    status = storage.job_status(tenant, workspace, job_id)
    assert status["status"] == "APPROVED"
    events = [e for e in storage.audit_events(tenant, workspace)
              if e["event_type"] == "job.self_approval" and e["payload"].get("job_id") == job_id]
    assert len(events) == 1, events


# ---------------------------------------------------------------------------
# 3. Deadlock-risk investigation: two concurrent publishes, involving the same
#    approver account/membership rows, in reversed job order across two
#    connections/threads. `recheck_and_publish_job` locks (job row) then
#    (account row) then (membership row) then updates -- since the job rows
#    differ but the account/membership rows are identical and locked in the
#    SAME order by both transactions, this must serialize cleanly (one waits
#    for the other's account/membership locks, never a lock-acquisition
#    cycle), not deadlock.
# ---------------------------------------------------------------------------

def test_concurrent_publishes_same_approver_different_jobs_do_not_deadlock(setup):
    tenant, workspace = setup["tenant"], setup["workspace"]
    job_a = f"job-{uuid4().hex}"
    job_b = f"job-{uuid4().hex}"
    _make_approved_job(setup, job_a)
    _make_approved_job(setup, job_b)
    _run_job_to_completion(setup["storage"], tenant, workspace, job_a)
    _run_job_to_completion(setup["storage"], tenant, workspace, job_b)

    store_a = PostgresV2Store(DSN, MemoryBlobs())
    store_b = PostgresV2Store(DSN, MemoryBlobs())
    outcomes: dict = {}

    def publish(name, store, job_id):
        try:
            store.recheck_and_publish_job(tenant, workspace, job_id, "change.approve")
            outcomes[name] = "published"
        except Exception as exc:  # pragma: no cover - would show a real conflict
            outcomes[name] = f"error: {exc!r}"

    # Reversed order across the two threads: thread 1 publishes job_a, thread
    # 2 publishes job_b, both concurrently, both against the SAME approver
    # account/membership rows (both jobs were approved by `setup["approver"]`).
    t1 = threading.Thread(target=publish, args=("a", store_a, job_a))
    t2 = threading.Thread(target=publish, args=("b", store_b, job_b))
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    assert not t1.is_alive() and not t2.is_alive(), (
        f"concurrent publish hung (possible deadlock): outcomes so far = {outcomes}")
    assert outcomes.get("a") == "published", outcomes
    assert outcomes.get("b") == "published", outcomes
