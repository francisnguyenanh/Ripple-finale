"""REL-02 (Final paths): concurrency, duplicate delivery, lost leases and tenant/rights boundaries on the knowledge loop, through the real store and workers.

What this proves: two concurrent commits of one approved change make ONE business revision; two concurrent accepts of one derived snapshot have one winner; a
duplicate delivery of the commit event builds the index once and a worker with a lost lease can neither acknowledge the event nor publish a source revision
(fenced); ids of another tenant are "not found" and a revoked membership stops every Final read. Kill-mid-commit (CHG-05), half-done document generation
(DOC-03), budget/unknown-usage fail-closed (BAS-03) and authority rechecks at run time (QRY-03, SYN-03) are reused from their own evidence. No model."""
from __future__ import annotations

import dataclasses
import threading

import psycopg2
import pytest
from fastapi.testclient import TestClient

from infra import judge
from infra.adapters import v2_business, v2_business_commit, v2_business_index
from infra.adapters.v2_business import BusinessKnowledgeRefused
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import LeaseLost, PostgresV2Store, StaleVersion, StoreConflict
from infra.worker import business_source_apply as bsa
from infra.worker.execution_types import ExecutionContext
from infra.worker.job_worker import _ack
from tests.final_loop.test_chg05_commit import approved
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.final_loop.test_syn02_reconcile import analyse, refresh_yaml
from tests.final_loop.test_syn03_source_apply import choose, committed_and_file_writes_approved, revisions
from tests.pitch_brief.conftest import drain


def _race(fn_a, fn_b):
    barrier, out = threading.Barrier(2), {}

    def run(name, fn):
        barrier.wait()
        try:
            out[name] = ("ok", fn())
        except (StaleVersion, StoreConflict, BusinessKnowledgeRefused) as exc:
            out[name] = ("refused", type(exc).__name__)
    ts = [threading.Thread(target=run, args=("a", fn_a)), threading.Thread(target=run, args=("b", fn_b))]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    return out


def test_two_concurrent_commits_of_one_approved_change_make_one_business_revision(qa_env):
    q = qa_env
    cid, approver, binding = approved(q)

    def committer():
        store = PostgresV2Store(q.env.dsn, q.env.blobs)                                                  # its own connections
        return v2_business_commit.commit_change(store, q.env.tenant, q.env.ws, cid, actor_id=q.env.admin, roles=("admin",), seen=binding)
    out = _race(committer, committer)
    fresh = [v for k, v in out.values() if k == "ok" and v["replayed"] is False]
    assert len(fresh) == 1, out                                                                           # one real commit; the other replayed or was refused
    view = v2_business.business_view(q.env.store, q.env.tenant, q.env.ws)
    assert view["business_head"]["business_revision"] == 2
    assert sum(1 for d in view["accepted"]["decisions"] if d["kind"] == "business_change") == 1
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND lifecycle_state='ACCEPTED'", (q.env.tenant, q.env.ws))
        assert cur.fetchone()[0] == 2


def test_two_concurrent_accepts_of_one_derived_snapshot_have_one_winner(qa_env):
    q = qa_env
    cid, approver, binding = approved(q)
    assert v2_business_commit.commit_change(q.env.store, q.env.tenant, q.env.ws, cid, actor_id=q.env.admin, roles=("admin",), seen=binding)["business_revision"] == 2
    drain(q.env)
    refresh_yaml(q, 1500000)
    assert analyse(q)["derived"]
    pending = v2_business.business_view(q.env.store, q.env.tenant, q.env.ws)["pending"]["snapshot"]["snapshot_id"]

    def acceptor():
        store = PostgresV2Store(q.env.dsn, q.env.blobs)
        return v2_business.accept_snapshot(store, q.env.tenant, q.env.ws, actor_id=q.env.admin, snapshot_id=pending, acknowledged_open_gaps=True)
    out = _race(acceptor, acceptor)
    kinds = sorted(k for k, _ in out.values())
    assert kinds in (["ok", "refused"], ["ok", "ok"]), out                                                 # a second identical accept may replay; never a second revision
    assert v2_business.business_view(q.env.store, q.env.tenant, q.env.ws)["business_head"]["business_revision"] == 3


def test_duplicate_delivery_builds_the_index_once_and_a_lost_lease_cannot_acknowledge(qa_env):
    q = qa_env
    cid, approver, binding = approved(q)
    out = v2_business_commit.commit_change(q.env.store, q.env.tenant, q.env.ws, cid, actor_id=q.env.admin, roles=("admin",), seen=binding)
    claim = q.env.store.claim_outbox(q.env.tenant, q.env.ws, "worker-1", lease_seconds=30)
    while claim is not None and claim.kind != "BUSINESS_KNOWLEDGE_COMMITTED":
        _ack(q.env.store, q.env.tenant, q.env.ws, claim, "worker-1")
        claim = q.env.store.claim_outbox(q.env.tenant, q.env.ws, "worker-1", lease_seconds=30)
    assert claim is not None and claim.payload["snapshot_id"] == out["snapshot_id"]
    first = v2_business_index.build_index(q.env.store, q.env.tenant, q.env.ws, out["snapshot_id"])
    second = v2_business_index.build_index(q.env.store, q.env.tenant, q.env.ws, out["snapshot_id"])          # the same event delivered twice
    assert first == second and first["status"] == "READY"
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_business_index_items WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s", (q.env.tenant, q.env.ws, out["snapshot_id"]))
        assert cur.fetchone()[0] == first["item_count"]
        cur.execute("SELECT count(*) FROM v2_business_index_builds WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s", (q.env.tenant, q.env.ws, out["snapshot_id"]))
        assert cur.fetchone()[0] == 1
    bumped = claim.fencing_token + 1                                                                             # a token the store never issued
    stale = dataclasses.replace(claim, fencing_token=bumped)
    assert _ack(q.env.store, q.env.tenant, q.env.ws, stale, "worker-1") is False                             # a worker that lost its lease cannot acknowledge
    assert _ack(q.env.store, q.env.tenant, q.env.ws, claim, "worker-2") is False                             # nor another worker without the lease
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM v2_outbox WHERE id=%s", (claim.id,))
        assert cur.fetchone()[0] != "DONE"
    assert _ack(q.env.store, q.env.tenant, q.env.ws, claim, "worker-1") is True
    assert q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["index"]["pending_events"] == 0


def test_a_source_apply_whose_lease_was_lost_publishes_nothing(qa_env):
    q = qa_env
    cid, approver, b = committed_and_file_writes_approved(q)
    r = choose(q, q.admin, cid, "ai", b)
    assert r.status_code == 200, r.text
    job_id = r.json()["job"]["job_id"]
    claim = q.env.store.claim_outbox(q.env.tenant, q.env.ws, "worker-1", lease_seconds=30)
    while claim is not None and not (claim.kind == "JOB_QUEUED" and claim.payload.get("job_id") == job_id):
        _ack(q.env.store, q.env.tenant, q.env.ws, claim, "worker-1")
        claim = q.env.store.claim_outbox(q.env.tenant, q.env.ws, "worker-1", lease_seconds=30)
    assert claim is not None
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, job_id)
    job = {**rec, "tenant_id": q.env.tenant, "workspace_id": q.env.ws}
    head, n = q.env.store.head(q.env.tenant, q.env.ws), revisions(q)
    ex = bsa.BusinessSourceApplyExecutor()
    ctx = ExecutionContext(store=q.env.store, job=job, config=None, lease=(claim.id, "worker-1", claim.fencing_token + 1))     # the lease moved on: this worker is fenced
    with pytest.raises(LeaseLost):
        ex.execute(ctx, ex.prepare(ctx), None)
    assert q.env.store.head(q.env.tenant, q.env.ws) == head and revisions(q) == n                            # nothing published by the fenced worker
    ctx_ok = ExecutionContext(store=q.env.store, job=job, config=None, lease=(claim.id, "worker-1", claim.fencing_token))
    out = ex.execute(ctx_ok, ex.prepare(ctx_ok), None)
    assert out.result["source_revision_id"] == f"src-apply-{job_id}" and revisions(q) == n + 1               # the lease holder publishes once
    assert _ack(q.env.store, q.env.tenant, q.env.ws, claim, "worker-1") is True


def test_another_tenant_cannot_see_final_ids_and_a_revoked_membership_stops_every_final_read(qa_env):
    q = qa_env
    cid, approver, b = committed_and_file_writes_approved(q)
    doc = q.admin.post("/v2/business/documents", json={"workspace_id": q.env.ws, "modes": ["business"]}).json()["documents"][0]["artifact_id"]
    drain(q.env)
    t2, p2, ws2, acct = f"t2-{q.env.nonce}", f"p2-{q.env.nonce}", f"ws2-{q.env.nonce}", f"admin2-{q.env.nonce}"
    q.env.store.create_workspace(t2, p2, ws2)
    PostgresIdentityStore(q.env.dsn).bootstrap_admin(t2, p2, ws2, acct, "c2")
    other = TestClient(judge.app, base_url="https://judge.test")
    other.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert other.post("/v2/auth/login", json={"account_id": acct, "credential": "c2"}).status_code == 200
    for path in (f"/v2/business/changes/{cid}/journey", f"/v2/business/changes/{cid}/source", f"/v2/business/changes/{cid}/review", f"/v2/business/documents/{doc}",
                 f"/v2/business/documents/{doc}/content", f"/v2/business/documents/{doc}/download"):
        r = other.get(path, params={"workspace_id": ws2})
        assert r.status_code == 404, (path, r.status_code, r.text)                                           # another tenant's ids do not exist here
        assert "1,000,000" not in r.text and "rule-" not in r.text
        assert other.get(path, params={"workspace_id": q.env.ws}).status_code in (401, 403, 404)             # and the first tenant's workspace is not theirs
    assert other.post("/v2/business/documents/export", json={"workspace_id": q.env.ws, "modes": ["business"]}).status_code in (401, 403, 404)
    PostgresIdentityStore(q.env.dsn).revoke_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, q.env.req)       # the requester loses the workspace
    for path in (f"/v2/business/changes/{cid}", f"/v2/business/changes/{cid}/journey", f"/v2/business/documents/{doc}/content", "/v2/business/knowledge"):
        r = q.req.get(path, params={"workspace_id": q.env.ws})
        assert r.status_code in (401, 403, 404), (path, r.status_code)
        assert "1,000,000" not in r.text and "rule-" not in r.text
