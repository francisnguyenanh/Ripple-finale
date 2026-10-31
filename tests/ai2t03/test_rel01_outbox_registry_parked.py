"""REL-01 (audit 2026-10-30): every outbox kind has an acknowledgement owner. Kinds registered as having no claim-consumer are acknowledged on first
claim; a kind outside the worker's registry is PARKED (payload kept, lease released, not polled) and returns to PENDING only through the operator
replay once a consumer exists; a real consumer that fails keeps its retry; readers that use the row id (the projection) are unaffected by DONE.
Real store on PostgreSQL, real claim path, deterministic (lease expiry set by SQL)."""
from __future__ import annotations

import json
from uuid import uuid4

import psycopg2
import pytest

from infra.adapters import v2_business_index, v2_projection
from infra.adapters.v2_postgres import LeaseLost, PostgresV2Store
from infra.worker import job_worker
from infra.worker.job_worker import claim_and_admit_one
from tools import ops_jobs

from .conftest import AI2_T03_TEST_DSN as DSN, MemoryBlobs

pytestmark = pytest.mark.skipif(not DSN, reason="needs a disposable PostgreSQL (AI2_T03_TEST_DSN)")


@pytest.fixture
def ws(monkeypatch):
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("test database not reachable")
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    nonce = uuid4().hex
    tenant, workspace = f"t-{nonce}", f"w-{nonce}"
    store = PostgresV2Store(DSN, MemoryBlobs())
    store.migrate()
    store.create_workspace(tenant, f"p-{nonce}", workspace)
    return {"tenant": tenant, "workspace": workspace, "store": store}


def _sql(query, params=()):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


def _insert_event(tenant, workspace, kind, payload):
    return _sql("INSERT INTO v2_outbox (tenant_id, workspace_id, kind, payload) VALUES (%s,%s,%s,%s) RETURNING id",
                (tenant, workspace, kind, json.dumps(payload)))[0][0]


def _row(event_id):
    r = _sql("SELECT status, attempts, lease_owner, payload FROM v2_outbox WHERE id=%s", (event_id,))[0]
    return {"status": r[0], "attempts": r[1], "lease_owner": r[2], "payload": r[3]}


def test_registered_no_consumer_kinds_are_acknowledged_once(ws):
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    ids = {k: _insert_event(tenant, workspace, k, {"n": k}) for k in sorted(job_worker.OUTBOX_NO_CLAIM_CONSUMER)}
    for _ in ids:
        assert claim_and_admit_one(store, tenant, workspace, "worker-1") is not None
    assert claim_and_admit_one(store, tenant, workspace, "worker-2") is None, "nothing is left to re-claim"
    for k, i in ids.items():
        assert _row(i) == {"status": "DONE", "attempts": 1, "lease_owner": None, "payload": {"n": k}}, k
    # the same for a SOURCE_PUBLISHED that is neither an apply nor a refresh (an import): acknowledged, not re-claimed five times
    i = _insert_event(tenant, workspace, "SOURCE_PUBLISHED", {"source_revision_id": "x", "version": 1})
    assert claim_and_admit_one(store, tenant, workspace, "worker-1") is not None
    assert _row(i)["status"] == "DONE" and _row(i)["attempts"] == 1


def test_unknown_kind_is_parked_with_its_payload_and_not_polled(ws):
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    i = _insert_event(tenant, workspace, "X_TEST_FUTURE_KIND", {"keep": "me", "n": 1})
    assert claim_and_admit_one(store, tenant, workspace, "worker-old") is not None
    assert _row(i) == {"status": "PARKED", "attempts": 0, "lease_owner": None, "payload": {"keep": "me", "n": 1}}
    assert claim_and_admit_one(store, tenant, workspace, "worker-old") is None, "a parked row is never polled again"
    audit = _sql("SELECT payload FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='OUTBOX_PARKED'", (tenant, workspace))
    assert len(audit) == 1 and audit[0][0]["event_id"] == i
    assert store.parked_outbox(tenant, workspace) and store.parked_outbox(tenant, workspace)[0]["kind"] == "X_TEST_FUTURE_KIND"


def test_rolling_version_old_worker_parks_new_worker_replays_and_consumes(ws, monkeypatch):
    """A worker of the previous version meets an event kind introduced after it: nothing is lost; once the registry knows the kind the operator
    replay returns the event and the new worker consumes it."""
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    i = _insert_event(tenant, workspace, "X_TEST_NEW_KIND", {"v": 2})
    assert claim_and_admit_one(store, tenant, workspace, "worker-v1") is not None
    assert _row(i)["status"] == "PARKED"
    # the operator tool refuses a replay of a kind the running worker does not know
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    assert ops_jobs.main(["replay-parked", "--tenant", tenant, "--workspace", workspace, "--kind", "X_TEST_NEW_KIND", "--reason", "deployed v2 worker", "--apply"]) == 2
    assert _row(i)["status"] == "PARKED"
    # the new worker version registers the kind (here: as a kind with no claim-consumer); the replay is audited and the event is consumed
    monkeypatch.setattr(job_worker, "OUTBOX_NO_CLAIM_CONSUMER", job_worker.OUTBOX_NO_CLAIM_CONSUMER | {"X_TEST_NEW_KIND"})
    assert ops_jobs.main(["replay-parked", "--tenant", tenant, "--workspace", workspace, "--kind", "X_TEST_NEW_KIND", "--reason", "deployed v2 worker"]) == 0
    assert _row(i)["status"] == "PARKED", "a dry run changes nothing"
    assert ops_jobs.main(["replay-parked", "--tenant", tenant, "--workspace", workspace, "--kind", "X_TEST_NEW_KIND", "--reason", "deployed v2 worker", "--apply"]) == 0
    assert _row(i)["status"] == "PENDING" and _row(i)["payload"] == {"v": 2}
    assert claim_and_admit_one(store, tenant, workspace, "worker-v2") is not None
    assert _row(i)["status"] == "DONE"
    replayed = _sql("SELECT payload FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='OUTBOX_REPLAYED'", (tenant, workspace))
    assert len(replayed) == 1 and replayed[0][0]["replayed"] == 1 and replayed[0][0]["kind"] == "X_TEST_NEW_KIND"


def test_parking_is_fenced(ws):
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    i = _insert_event(tenant, workspace, "X_TEST_FENCE", {})
    claim = store.claim_outbox(tenant, workspace, "worker-a", lease_seconds=30)
    with pytest.raises(LeaseLost):
        store.park_outbox(tenant, workspace, claim.id, "worker-a", claim.fencing_token + 1, reason="stale")
    assert _row(i)["status"] == "CLAIMED"
    store.park_outbox(tenant, workspace, claim.id, "worker-a", claim.fencing_token, reason="right owner")
    assert _row(i)["status"] == "PARKED"


def test_real_consumer_failure_keeps_the_retry(ws, monkeypatch):
    """BUSINESS_KNOWLEDGE_COMMITTED has a consumer (the index build): a failure leaves the row CLAIMED for a reclaim, never DONE, never PARKED."""
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    i = _insert_event(tenant, workspace, "BUSINESS_KNOWLEDGE_COMMITTED", {"lifecycle_state": "ACCEPTED", "snapshot_id": "snap-x"})
    monkeypatch.setattr(v2_business_index, "build_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("index build failed")))
    assert claim_and_admit_one(store, tenant, workspace, "worker-1") is not None
    assert _row(i)["status"] == "CLAIMED" and _row(i)["attempts"] == 1
    _sql("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE id=%s", (i,))
    assert claim_and_admit_one(store, tenant, workspace, "worker-2") is not None
    assert _row(i)["status"] == "CLAIMED" and _row(i)["attempts"] == 2
    monkeypatch.setattr(v2_business_index, "build_index", lambda *a, **k: {"state": "READY"})
    _sql("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE id=%s", (i,))
    assert claim_and_admit_one(store, tenant, workspace, "worker-3") is not None
    assert _row(i)["status"] == "DONE"


def test_projection_reads_an_acknowledged_change_completed_by_row_id(ws):
    tenant, workspace, store = ws["tenant"], ws["workspace"], ws["store"]
    i = _insert_event(tenant, workspace, "CHANGE_COMPLETED", {"decision_id": "d1"})
    assert claim_and_admit_one(store, tenant, workspace, "worker-1") is not None
    assert _row(i)["status"] == "DONE"
    puts = []

    class _Sink:
        def put(self, scope, key, hint):
            puts.append(hint)
            return True

    v2_projection.project_once(store, _Sink(), tenant, workspace)
    assert any(h["family"] == "change" for h in puts), "the projection still sees the event after the worker acknowledged it"
