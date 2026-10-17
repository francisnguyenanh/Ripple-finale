"""Independent attacks on the
publish gate (carry_forward / fence / prior_publish / audit_context and
the SHARED `job_worker` LeaseLost handling).

Sections
  A  job_worker LeaseLost is generic (non-apply executor, prepare stage and
     execute stage) and leaves the job recoverable by the next owner.
  B  carry_forward store-level correctness (metadata, multi-artifact, target
     absent from head).
  C  fence store-level (wrong worker, wrong token, expired-not-reclaimed,
     acked event, row-lock serialization).
  D  prior_publish isolation + F3 variants.
  E  durable audit/outbox identity (job_id / approver).
  F  card scope lines: no deploy/Git push, manual work not faked as edit.
  G  failure injection on the product path (neutralize guard -> violation is
     observable; guard on -> protected) for carry_forward, fence, prior_publish.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import Json

from infra.adapters import v2_postgres
from infra.adapters.v2_postgres import LeaseLost, StoreConflict
from infra.worker import job_worker
from infra.worker.job_worker import ExecutionOutcome, ExecutionRequest
from tests.ai4t01.conftest import DSN
from tests.ai8t01.conftest import get_job_status, login, submit_apply_job, submit_claim_and_get_apply
from tests.ai8t02.conftest import (
    artifact_text, build_ready_bundles, build_ready_to_publish_yaml, drain_outbox,
    publish_footprint,
)

META_COLS = ("logical_path", "logical_path_key", "media_type", "byte_size",
             "source_role", "source_role_origin", "provenance")


# ----------------------------------------------------------------------------- helpers

def _seed_base(store, tenant, ws, arts: dict):
    """Insert a base revision WITH artifact metadata (rows are immutable, so
    seed via raw SQL like the import boundary would). arts = {aid: (bytes, meta)}."""
    digests = {aid: store.stage_blob(b) for aid, (b, _m) in arts.items()}
    rev = f"rev-seed-{uuid4().hex}"
    with store._tx() as cur:
        head = store._lock_head(cur, tenant, ws)
        cur.execute("""INSERT INTO v2_source_revisions
            (tenant_id, workspace_id, revision_id, parent_revision_id, manifest)
            VALUES (%s,%s,%s,%s,%s)""",
            (tenant, ws, rev, head.source_revision_id, Json(dict(sorted(digests.items())))))
        for aid, (b, meta) in arts.items():
            cur.execute("""INSERT INTO v2_blob_refs (tenant_id, workspace_id, blob_hash)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""", (tenant, ws, digests[aid]))
            cur.execute("""INSERT INTO v2_artifact_revisions
                (tenant_id, workspace_id, source_revision_id, artifact_id, blob_hash,
                 logical_path, logical_path_key, media_type, byte_size, source_role,
                 source_role_origin, provenance)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tenant, ws, rev, aid, digests[aid], meta["logical_path"],
                 meta["logical_path"].lower(), meta["media_type"], len(b),
                 meta["source_role"], meta["source_role_origin"], meta["provenance"]))
        cur.execute("""UPDATE v2_workspaces SET version=%s, source_revision_id=%s
            WHERE tenant_id=%s AND workspace_id=%s""", (head.version + 1, rev, tenant, ws))
    return rev, digests


def _rows(store, tenant, ws, rev):
    with store._tx() as cur:
        cur.execute("""SELECT artifact_id, blob_hash, logical_path, logical_path_key, media_type,
                byte_size, source_role, source_role_origin, provenance
            FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s
              AND source_revision_id=%s ORDER BY artifact_id""", (tenant, ws, rev))
        return {r[0]: r[1:] for r in cur.fetchall()}


def _meta(name, role):
    return {"logical_path": f"dir/{name}", "media_type": "text/yaml",
            "source_role": role, "source_role_origin": "declared", "provenance": f"prov-{name}"}


def _seed_three(store, tenant, ws):
    return _seed_base(store, tenant, ws, {
        "a.yaml": (b"a: 1\n", _meta("A.yaml", "config")),
        "b.yaml": (b"b: 2\n", _meta("B.yaml", "config")),
        "c.yaml": (b"c: 3\n", _meta("C.yaml", "doc")),
    })


def _claim_event(store, tenant, ws, worker="fence-w"):
    """Make one outbox event and claim it -> (claim, worker)."""
    drain_outbox(store, tenant, ws)
    head = store.head(tenant, ws)
    store.publish_source(tenant, ws, f"rev-ev-{uuid4().hex}", {"e.yaml": store.stage_blob(uuid4().hex.encode())},
                         expected_version=head.version, operation="src.import.commit",
                         idempotency_key=uuid4().hex, carry_forward=True)
    claim = store.claim_outbox(tenant, ws, worker, lease_seconds=60)
    assert claim is not None
    return claim, worker


def _expire(store, tenant, ws, event_id):
    with store._tx() as cur:
        cur.execute("UPDATE v2_outbox SET lease_until=now()-interval '5 seconds' "
                    "WHERE tenant_id=%s AND workspace_id=%s AND id=%s", (tenant, ws, event_id))


# ----------------------------------------------------------------------------- B carry_forward

def test_b1_carry_forward_keeps_every_metadata_column_of_untouched_artifacts(setup):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    base_rev, digests = _seed_three(store, t, ws)
    base_rows = _rows(store, t, ws, base_rev)
    new_bytes = b"b: 22222\n"
    new_hash = store.stage_blob(new_bytes)
    res = store.publish_source(t, ws, f"rev-cf-{uuid4().hex}", {"b.yaml": new_hash},
                               expected_version=store.head(t, ws).version,
                               operation="cf.op", idempotency_key=uuid4().hex, carry_forward=True)
    rows = _rows(store, t, ws, res.revision_id)
    assert set(rows) == {"a.yaml", "b.yaml", "c.yaml"}
    for untouched in ("a.yaml", "c.yaml"):
        assert rows[untouched] == base_rows[untouched], f"{untouched} metadata drifted"
    b_new, b_old = rows["b.yaml"], base_rows["b.yaml"]
    assert b_new[0] == new_hash and b_new[0] != b_old[0]
    assert b_new[4] == len(new_bytes), "byte_size not refreshed for overridden artifact"
    # identity metadata of the overridden artifact preserved (logical_path .. provenance)
    assert (b_new[1:4] + b_new[5:]) == (b_old[1:4] + b_old[5:])
    with store._tx() as cur:
        cur.execute("SELECT manifest, parent_revision_id FROM v2_source_revisions "
                    "WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s",
                    (t, ws, res.revision_id))
        manifest, parent = cur.fetchone()
    assert parent == base_rev
    assert manifest == {"a.yaml": digests["a.yaml"], "b.yaml": new_hash, "c.yaml": digests["c.yaml"]}


def test_b2_carry_forward_multiple_overrides_and_target_absent_from_head(setup):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    base_rev, digests = _seed_three(store, t, ws)
    base_rows = _rows(store, t, ws, base_rev)
    h_a, h_new = store.stage_blob(b"a: 100\n"), store.stage_blob(b"brand: new\n")
    res = store.publish_source(t, ws, f"rev-cf-{uuid4().hex}",
                               {"a.yaml": h_a, "new.yaml": h_new},
                               expected_version=store.head(t, ws).version,
                               operation="cf.op", idempotency_key=uuid4().hex, carry_forward=True)
    rows = _rows(store, t, ws, res.revision_id)
    assert set(rows) == {"a.yaml", "b.yaml", "c.yaml", "new.yaml"}
    assert rows["b.yaml"] == base_rows["b.yaml"] and rows["c.yaml"] == base_rows["c.yaml"]
    assert rows["a.yaml"][0] == h_a and rows["a.yaml"][1:4] == base_rows["a.yaml"][1:4]
    assert rows["new.yaml"][0] == h_new
    # an artifact absent from the head has no parent metadata to carry
    assert rows["new.yaml"][1:] == (None,) * 7
    # and every blob is readable through the real read path
    for aid in rows:
        assert store.read_artifact_blob(t, ws, res.revision_id, aid)["blob_hash"] == rows[aid][0]


def test_b3_default_publish_is_unchanged_replace_all_semantics(setup):
    """Control: without carry_forward the manifest is the WHOLE snapshot (old
    behaviour every other caller relies on)."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    _seed_three(store, t, ws)
    res = store.publish_source(t, ws, f"rev-x-{uuid4().hex}", {"b.yaml": store.stage_blob(b"z\n")},
                               expected_version=store.head(t, ws).version,
                               operation="x.op", idempotency_key=uuid4().hex)
    assert set(_rows(store, t, ws, res.revision_id)) == {"b.yaml"}


# ----------------------------------------------------------------------------- C fence

def _publish_fenced(store, t, ws, fence, *, payload=b"f\n"):
    return store.publish_source(
        t, ws, f"rev-fence-{uuid4().hex}", {"f.yaml": store.stage_blob(payload)},
        expected_version=store.head(t, ws).version, operation="fence.op",
        idempotency_key=uuid4().hex, carry_forward=True, fence=fence)


@pytest.mark.parametrize("case", ["wrong_worker", "wrong_token", "expired_not_reclaimed",
                                  "acked_event", "unknown_event"])
def test_c1_fence_rejects_non_owner_and_writes_nothing(setup, case):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    claim, worker = _claim_event(store, t, ws)
    head_before, fp_before = store.head(t, ws), publish_footprint(store, t, ws)
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_idempotency WHERE tenant_id=%s AND workspace_id=%s",
                    (t, ws))
        idem_before = cur.fetchone()[0]
    fence = {
        "wrong_worker": (claim.id, "someone-else", claim.fencing_token),
        "wrong_token": (claim.id, worker, claim.fencing_token + 1),
        "expired_not_reclaimed": (claim.id, worker, claim.fencing_token),
        "acked_event": (claim.id, worker, claim.fencing_token),
        "unknown_event": (10 ** 12, worker, claim.fencing_token),
    }[case]
    if case == "expired_not_reclaimed":
        _expire(store, t, ws, claim.id)  # nobody re-claimed: owner/token unchanged
    if case == "acked_event":
        store.ack_outbox(t, ws, claim.id, worker, claim.fencing_token)
    with pytest.raises(LeaseLost):
        _publish_fenced(store, t, ws, fence)
    assert store.head(t, ws) == head_before
    assert publish_footprint(store, t, ws) == fp_before
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_idempotency WHERE tenant_id=%s AND workspace_id=%s",
                    (t, ws))
        assert cur.fetchone()[0] == idem_before
    # control: the true owner (fresh valid claim) succeeds
    if case in ("wrong_worker", "wrong_token", "unknown_event"):
        res = _publish_fenced(store, t, ws, (claim.id, worker, claim.fencing_token))
        assert store.head(t, ws).source_revision_id == res.revision_id


def test_c2_fence_takeover_after_expiry_invalidates_old_token(setup):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    claim, worker = _claim_event(store, t, ws, "old-owner")
    _expire(store, t, ws, claim.id)
    new = store.claim_outbox(t, ws, "new-owner", lease_seconds=60)
    assert new is not None and new.id == claim.id and new.fencing_token > claim.fencing_token
    v = store.head(t, ws).version
    with pytest.raises(LeaseLost):
        _publish_fenced(store, t, ws, (claim.id, "old-owner", claim.fencing_token))
    assert store.head(t, ws).version == v
    _publish_fenced(store, t, ws, (new.id, "new-owner", new.fencing_token))
    assert store.head(t, ws).version == v + 1


def test_c3_fence_row_lock_blocks_takeover_until_publish_commits(setup):
    """The fence row is locked FOR UPDATE inside the publish tx: an attempt to
    expire/steal the lease from another connection cannot interleave."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    claim, worker = _claim_event(store, t, ws)
    seen = {}

    def _before_commit():
        conn = psycopg2.connect(DSN)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET lock_timeout = '400ms'")
                try:
                    cur.execute("UPDATE v2_outbox SET lease_until=now()-interval '1 second' "
                                "WHERE tenant_id=%s AND workspace_id=%s AND id=%s",
                                (t, ws, claim.id))
                    seen["blocked"] = False
                except psycopg2.errors.LockNotAvailable:
                    seen["blocked"] = True
        finally:
            conn.close()

    store.publish_source(t, ws, f"rev-lk-{uuid4().hex}", {"f.yaml": store.stage_blob(b"lk\n")},
                         expected_version=store.head(t, ws).version, operation="fence.op",
                         idempotency_key=uuid4().hex, carry_forward=True,
                         fence=(claim.id, worker, claim.fencing_token),
                         before_commit=_before_commit)
    assert seen.get("blocked") is True, "lease could be mutated while the fenced publish was in flight"


# ----------------------------------------------------------------------------- A job_worker LeaseLost generic

class _LeaseLosingExecutor:
    """Non-apply executor. `stage` selects where LeaseLost is raised."""

    def __init__(self, stage):
        self.stage, self.calls = stage, 0
        self.executor_id = "t02.lease.v1"

    def prepare(self, ctx):
        if self.stage == "prepare" and self.calls == 0:
            self.calls += 1
            raise LeaseLost("lost in prepare")
        return ExecutionRequest(prompt=None, schema=None, provider_calls_planned=0, external=False)

    def execute(self, ctx, request, calls):
        if self.stage == "execute" and self.calls == 0:
            self.calls += 1
            raise LeaseLost("lost in execute")
        self.calls += 1
        return ExecutionOutcome(result={"ok": True, "lease": list(ctx.lease or ())})


def _job_row(store, t, ws, job_id):
    return store.job_record(t, ws, job_id)


@pytest.mark.parametrize("stage", ["prepare", "execute"])
def test_a1_non_apply_executor_lease_lost_writes_no_terminal_and_new_owner_recovers(
        setup, monkeypatch, stage):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    _seed_three(store, t, ws)
    drain_outbox(store, t, ws)
    kind = f"t02lease{stage}"
    ex = _LeaseLosingExecutor(stage)
    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, kind, ex)
    job_id = f"job-lease-{uuid4().hex}"
    store.create_job(t, ws, job_id, kind, {}, expected_version=store.head(t, ws).version,
                     idempotency_key=f"create-{job_id}")
    claim = store.claim_outbox(t, ws, "stale-w", lease_seconds=60)
    outcome = job_worker.execute_admitted_claim(store, t, ws, claim, "stale-w")
    assert outcome == "LEASE_LOST"
    rec = _job_row(store, t, ws, job_id)
    assert rec["execution_status"] == "RUNNING", "stale worker wrote a terminal state"
    with store._tx() as cur:
        cur.execute("SELECT status FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND id=%s",
                    (t, ws, claim.id))
        assert cur.fetchone()[0] == "CLAIMED", "stale worker acked/released the event"
        cur.execute("SELECT count(*) FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s "
                    "AND job_id=%s", (t, ws, job_id))
        assert cur.fetchone()[0] == 0, "stale worker recorded an attempt"

    # not stuck: after lease expiry a new owner reclaims and completes the job
    _expire(store, t, ws, claim.id)
    new_claim = store.claim_outbox(t, ws, "new-w", lease_seconds=60)
    assert new_claim is not None and new_claim.id == claim.id
    assert job_worker.execute_admitted_claim(store, t, ws, new_claim, "new-w") == "DONE"
    rec = _job_row(store, t, ws, job_id)
    assert rec["execution_status"] == "COMPLETED"
    assert rec["execution_result"]["lease"][1] == "new-w"
    assert rec["execution_result"]["lease"][2] == new_claim.fencing_token
    # and the old owner can no longer ack
    with pytest.raises(LeaseLost):
        store.ack_outbox(t, ws, claim.id, "stale-w", claim.fencing_token)


def test_a2_ctx_lease_is_the_claim_identity(setup, monkeypatch):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    _seed_three(store, t, ws)
    drain_outbox(store, t, ws)
    ex = _LeaseLosingExecutor("none")
    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "t02leaseid", ex)
    job_id = f"job-leaseid-{uuid4().hex}"
    store.create_job(t, ws, job_id, "t02leaseid", {}, expected_version=store.head(t, ws).version,
                     idempotency_key=f"create-{job_id}")
    claim = store.claim_outbox(t, ws, "w-id", lease_seconds=60)
    assert job_worker.execute_admitted_claim(store, t, ws, claim, "w-id") == "DONE"
    assert _job_row(store, t, ws, job_id)["execution_result"]["lease"] == [
        claim.id, "w-id", claim.fencing_token]


# ----------------------------------------------------------------------------- product-path scenarios

def _ready(setup, monkeypatch, owner):
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    return workspace, client, ready


@pytest.mark.parametrize("neutralize", [True, False], ids=["INJECT-red", "guard-green"])
def test_g1_carry_forward_guard_product_path(setup, monkeypatch, owner, neutralize):
    workspace = setup["workspace"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    store, t = setup["store"], setup["tenant"]
    # F7: the multi-artifact head (target + sibling) is published BEFORE the
    # bundle is approved, so the approval snapshot is still current at apply.
    ready = build_ready_bundles(
        setup, client, workspace, monkeypatch, owner,
        siblings={"sibling-t02.yaml": "sibling_setting: 7\n"})[0]
    sib = store.stage_blob(b"sibling_setting: 7\n")
    original = v2_postgres.PostgresV2Store.publish_source

    def _wrapped(self, *a, **k):
        if neutralize and k.get("operation") == "apply.publish":
            k["carry_forward"] = False
        return original(self, *a, **k)

    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", _wrapped)
        body = submit_claim_and_get_apply(
            setup, client, workspace, approval_job_id=ready["approval_job_id"],
            expected_approval_digest=ready["digest"])
    assert body["execution_status"] == "COMPLETED", body
    head = store.head(t, workspace)
    try:
        preserved = store.read_artifact_blob(
            t, workspace, head.source_revision_id, "sibling-t02.yaml")["blob_hash"] == sib
    except Exception:
        preserved = False
    assert preserved is (not neutralize), (
        "carry_forward neutralization must make the sibling vanish (red); guard on must keep it")


@pytest.mark.parametrize("neutralize", [True, False], ids=["INJECT-red", "guard-green"])
def test_g2_fence_guard_product_path(setup, monkeypatch, owner, neutralize):
    workspace, client, ready = _ready(setup, monkeypatch, owner)
    store, t = setup["store"], setup["tenant"]
    drain_outbox(store, t, workspace)
    assert submit_apply_job(client, workspace, approval_job_id=ready["approval_job_id"],
                            expected_approval_digest=ready["digest"]).status_code == 200
    before = store.head(t, workspace)
    original = v2_postgres.PostgresV2Store.publish_source

    def _wrapped(self, *a, **k):
        if k.get("operation") == "apply.publish":
            with store._tx() as cur:
                cur.execute("UPDATE v2_outbox SET lease_until=now()-interval '5 seconds' "
                            "WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'",
                            (t, workspace))
            if neutralize:
                k["fence"] = None
        return original(self, *a, **k)

    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", _wrapped)
        job_worker.claim_and_admit_one(store, t, workspace, "t02-g2-worker", lease_seconds=30)
    advanced = store.head(t, workspace).version == before.version + 1
    assert advanced is neutralize, (
        "without fence a worker with an expired lease publishes (red); with fence it must not")


def _crash_after_commit_then_retry(setup, monkeypatch, client, workspace, ready, neutralize_prior):
    store, t = setup["store"], setup["tenant"]
    drain_outbox(store, t, workspace)
    job_id = f"job-apply-{uuid4().hex}"
    assert submit_apply_job(client, workspace, approval_job_id=ready["approval_job_id"],
                            job_id=job_id, expected_approval_digest=ready["digest"]).status_code == 200
    original = v2_postgres.PostgresV2Store.publish_source

    class Died(BaseException):
        pass

    def _commit_then_die(self, *a, **k):
        original(self, *a, **k)
        raise Died()

    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", _commit_then_die)
        with pytest.raises(Died):
            job_worker.claim_and_admit_one(store, t, workspace, "t02-dying")
    committed = store.head(t, workspace)
    fp = publish_footprint(store, t, workspace)
    with store._tx() as cur:
        cur.execute("UPDATE v2_outbox SET lease_until=now()-interval '5 seconds' "
                    "WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'", (t, workspace))
    with monkeypatch.context() as mp:
        if neutralize_prior:
            mp.setattr(v2_postgres.PostgresV2Store, "prior_publish", lambda self, *a, **k: None)
        job_worker.claim_and_admit_one(store, t, workspace, "t02-recovery")
    return job_id, committed, fp


@pytest.mark.parametrize("neutralize", [True, False], ids=["INJECT-red", "guard-green"])
def test_g3_prior_publish_guard_product_path(setup, monkeypatch, owner, neutralize):
    workspace, client, ready = _ready(setup, monkeypatch, owner)
    store, t = setup["store"], setup["tenant"]
    job_id, committed, fp = _crash_after_commit_then_retry(
        setup, monkeypatch, client, workspace, ready, neutralize)
    body = get_job_status(client, workspace, job_id)
    assert store.head(t, workspace) == committed and publish_footprint(store, t, workspace) == fp
    if neutralize:
        assert body["execution_status"] == "FAILED", body  # F3 regression is observable
    else:
        assert body["execution_status"] == "COMPLETED", body
        res = body["execution_result"]
        assert res["recovered_prior_publish"] is True
        assert res["published_revision_id"] == committed.source_revision_id
        assert res["published"] is True


# ----------------------------------------------------------------------------- A3 / D variants

def test_a3_stale_worker_then_expired_unclaimed_lease_is_recoverable_apply(setup, monkeypatch, owner):
    """Apply path: lease expires (nobody took over) mid-execution -> stale
    worker returns LEASE_LOST, job stays RUNNING, publish did not happen; a
    later worker reclaims and the job completes with exactly one publish."""
    workspace, client, ready = _ready(setup, monkeypatch, owner)
    store, t = setup["store"], setup["tenant"]
    drain_outbox(store, t, workspace)
    job_id = f"job-apply-{uuid4().hex}"
    assert submit_apply_job(client, workspace, approval_job_id=ready["approval_job_id"],
                            job_id=job_id, expected_approval_digest=ready["digest"]).status_code == 200
    before = store.head(t, workspace)
    original = v2_postgres.PostgresV2Store.publish_source

    def _expire_then_publish(self, *a, **k):
        if k.get("operation") == "apply.publish":
            with store._tx() as cur:
                cur.execute("UPDATE v2_outbox SET lease_until=now()-interval '5 seconds' "
                            "WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'",
                            (t, workspace))
        return original(self, *a, **k)

    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", _expire_then_publish)
        job_worker.claim_and_admit_one(store, t, workspace, "t02-a3-stale")
    assert store.head(t, workspace).version == before.version
    body = get_job_status(client, workspace, job_id)
    assert body["execution_status"] == "RUNNING", body
    job_worker.claim_and_admit_one(store, t, workspace, "t02-a3-new")
    body = get_job_status(client, workspace, job_id)
    assert body["execution_status"] == "COMPLETED", body
    assert store.head(t, workspace).version == before.version + 1
    assert publish_footprint(store, t, workspace)["apply_publish_keys"] == 1


def test_d1_crash_after_commit_before_ack_keeps_outbox_and_retry_acks(setup, monkeypatch, owner):
    workspace, client, ready = _ready(setup, monkeypatch, owner)
    store, t = setup["store"], setup["tenant"]
    job_id, committed, fp = _crash_after_commit_then_retry(
        setup, monkeypatch, client, workspace, ready, False)
    with store._tx() as cur:
        cur.execute("SELECT kind, status FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s "
                    "ORDER BY id", (t, workspace))
        rows = cur.fetchall()
    assert ("JOB_QUEUED", "DONE") in rows, rows          # recovery acked the job event
    assert ("SOURCE_PUBLISHED", "PENDING") in rows, rows  # rebuild intent durable, single
    assert sum(1 for r in rows if r[0] == "SOURCE_PUBLISHED" and r[1] == "PENDING") == 1
    assert get_job_status(client, workspace, job_id)["execution_status"] == "COMPLETED"


def test_d2_same_job_id_with_different_approval_is_a_conflict_not_a_prior_result(
        setup, monkeypatch, owner):
    workspace, client, ready = _ready(setup, monkeypatch, owner)
    store, t = setup["store"], setup["tenant"]
    job_id = f"job-apply-{uuid4().hex}"
    v0 = store.head(t, workspace).version
    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready["approval_job_id"],
        job_id=job_id, expected_approval_digest=ready["digest"])
    assert body["execution_status"] == "COMPLETED", body
    head = store.head(t, workspace)
    fp = publish_footprint(store, t, workspace)
    with pytest.raises(StoreConflict):
        store.create_job(t, workspace, job_id, kind="apply",
                         payload={"approval_job_id": "job-approval-other",
                                  "expected_approval_digest": "0" * 64},
                         expected_version=v0, idempotency_key=f"job-create-{job_id}")
    assert store.head(t, workspace) == head and publish_footprint(store, t, workspace) == fp


def test_d3_prior_publish_is_scoped_by_tenant_workspace_operation_and_key(setup):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    proj = setup["project"]
    other_ws = f"{ws}-other"
    store.create_workspace(t, proj, other_ws)
    base_rev, _ = _seed_three(store, t, ws)
    key = f"job-apply-publish-{uuid4().hex}"
    res = store.publish_source(t, ws, "rev-apply-x1", {"a.yaml": store.stage_blob(b"a: 9\n")},
                               expected_version=store.head(t, ws).version, operation="apply.publish",
                               idempotency_key=key, carry_forward=True)
    got = store.prior_publish(t, ws, "apply.publish", key)
    assert got == {"version": res.version, "revision_id": "rev-apply-x1",
                   "parent_revision_id": base_rev}
    assert store.prior_publish(t, other_ws, "apply.publish", key) is None      # other workspace
    assert store.prior_publish(f"{t}-x", ws, "apply.publish", key) is None     # other tenant
    assert store.prior_publish(t, ws, "other.op", key) is None                  # other operation
    assert store.prior_publish(t, ws, "apply.publish", key + "z") is None       # other key
    # same key string in the other workspace is independent
    store.publish_source(t, other_ws, "rev-apply-y1", {"q.yaml": store.stage_blob(b"q\n")},
                         expected_version=0, operation="apply.publish", idempotency_key=key)
    assert store.prior_publish(t, other_ws, "apply.publish", key)["revision_id"] == "rev-apply-y1"
    assert store.prior_publish(t, ws, "apply.publish", key)["revision_id"] == "rev-apply-x1"


# ----------------------------------------------------------------------------- E audit identity

def test_e1_audit_and_outbox_payload_carry_job_and_approver_durably(setup, monkeypatch, owner):
    workspace, client, ready = _ready(setup, monkeypatch, owner)
    store, t = setup["store"], setup["tenant"]
    job_id = f"job-apply-{uuid4().hex}"
    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready["approval_job_id"],
        job_id=job_id, expected_approval_digest=ready["digest"])
    assert body["execution_status"] == "COMPLETED", body
    rev = body["execution_result"]["published_revision_id"]
    assert rev == f"rev-apply-{job_id}"
    with store._tx() as cur:
        cur.execute("""SELECT payload FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s
            AND event_type='SOURCE_PUBLISHED' AND payload->>'source_revision_id'=%s""",
                    (t, workspace, rev))
        audit = [r[0] for r in cur.fetchall()]
        cur.execute("""SELECT payload FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s
            AND kind='SOURCE_PUBLISHED' AND payload->>'source_revision_id'=%s""",
                    (t, workspace, rev))
        outbox = [r[0] for r in cur.fetchall()]
    assert len(audit) == 1 and len(outbox) == 1
    for p in (audit[0], outbox[0]):
        assert p["job_id"] == job_id and p["kind"] == "apply"
        assert p["approval_job_id"] == ready["approval_job_id"]
        assert p["approver_actor_id"] == ready["approver"]["account"]
        assert p["target_artifact_id"] == ready["target_artifact_id"]


# ----------------------------------------------------------------------------- F card scope lines

def test_f1_apply_publish_path_never_shells_out_or_pushes(setup, monkeypatch, owner):
    """Card: "No deployments/Git push tai day". Any subprocess / os.system
    during a full apply+publish would raise; the apply must still complete."""
    import os
    workspace, client, ready = _ready(setup, monkeypatch, owner)
    store, t = setup["store"], setup["tenant"]

    def _forbidden(*a, **k):
        raise AssertionError("apply path spawned a process (deploy / git push)")

    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _forbidden)
    monkeypatch.setattr(os, "system", _forbidden)
    before = store.head(t, workspace).version
    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready["approval_job_id"],
        expected_approval_digest=ready["digest"])
    assert body["execution_status"] == "COMPLETED", body
    assert store.head(t, workspace).version == before + 1
    root = Path(__file__).resolve().parents[2]
    for rel in ("infra/worker/apply.py",):
        src = (root / rel).read_text(encoding="utf-8")
        for token in ("import subprocess", "os.system", "git push", "import requests",
                      "urllib.request"):
            assert token not in src, f"{rel} references {token!r}"


def test_f2_manual_classified_proposal_is_never_published_as_a_source_edit(setup, monkeypatch, owner):
    """Card: "External manual actions khong gia thanh source edits". A
    `manual`-classified proposal (no resolvable edit) approved by a human must
    not advance head / write publish rows through the apply gate."""
    from tests.ai4t01.conftest import publish_with_chunk
    from tests.ai7t01.conftest import (
        create_real_impact_job, create_real_simulation_job, submit_job)
    from tests.ai7t02.conftest import submit_claim_and_get_review
    from tests.ai7t03.conftest import submit_approval_job
    from tests.ai8t01.conftest import record_real_approve_decision

    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    target = "notes-t02-manual.md"
    publish_with_chunk(store, t, workspace, artifact_id=target, text="unrelated: 1\n")
    drain_outbox(store, t, workspace)
    impact = create_real_impact_job(setup, client, workspace)
    sim = create_real_simulation_job(setup, client, workspace)
    proposal_id = f"job-proposal-{uuid4().hex}"
    _r = submit_job(client, workspace, kind="proposal", job_id=proposal_id, payload={
        "intent_id": f"intent-nonexistent-{uuid4().hex}", "impact_job_id": impact,
        "simulation_job_id": sim, "target_artifact_id": target,
        "edit_kind": "markdown"}); assert _r.status_code == 200, _r.text
    job_worker.claim_and_admit_one(store, t, workspace, "f2-proposal")
    pb = get_job_status(client, workspace, proposal_id)
    assert pb["execution_status"] == "COMPLETED", pb
    assert pb["execution_result"]["classification"] == "manual", pb
    review = submit_claim_and_get_review(setup, client, workspace, proposal_job_id=proposal_id,
                                         worker_id="f2-review")
    review_id = review.get("job_id") or review.get("id")
    approval_id = f"job-approval-{uuid4().hex}"
    assert submit_approval_job(client, workspace, proposal_job_id=proposal_id,
                               review_job_id=review_id, job_id=approval_id).status_code == 200
    job_worker.claim_and_admit_one(store, t, workspace, "f2-approval")
    ab = get_job_status(client, workspace, approval_id)
    digest = ab["execution_result"]["approval_digest"]
    assert ab["execution_result"]["required_action"] == "manual_work_required"
    record_real_approve_decision(setup, workspace, approval_job_id=approval_id, digest=digest)
    head, fp = store.head(t, workspace), publish_footprint(store, t, workspace)
    body = submit_claim_and_get_apply(setup, client, workspace, approval_job_id=approval_id,
                                      expected_approval_digest=digest)
    assert store.head(t, workspace) == head, "manual work was published as a source edit"
    assert publish_footprint(store, t, workspace) == fp
    # The contract requires COMPLETED
    # with pending human work (never a source edit); head/footprint/published
    # assertions above are kept verbatim.
    assert body["execution_status"] == "COMPLETED", body
    res = body["execution_result"]
    assert res["published"] is False and res["published_revision_id"] is None, res
    assert res["pending_human_work"], res
