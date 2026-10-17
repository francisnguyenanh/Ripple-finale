"""Controlled failure injection for the worker (the basis of the recovery plan for the always-on worker, option B). A "death" is a BaseException that no worker handler catches:
the process is gone in the middle of a job, nothing after that point runs. Lease expiry then lets a second worker reclaim. Deterministic jobs first (no provider); one stub-provider
case measures what a crash AFTER the provider answered costs in the ledger. Nothing here is a Cloud Run crash test and a cancel is not a crash: this proves the DATABASE side of recovery."""
from __future__ import annotations

import pytest

from infra.worker import change_draft, job_worker
from tests.pitch_brief.chain import APPROVAL, S1, build, intent
from tests.pitch_brief.conftest import create_job, job_body
from tests.pitch_brief.test_change_draft import Model
from tests.pitch_brief.test_draft_to_proposal import cand_id


class WorkerDied(BaseException):
    """The process vanished: not an Exception, so no `except Exception` in the worker sees it."""


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _impact_job(env, ch, intent_id):
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    jid, r = create_job(env.req_client(), env, "impact", {"intent_id": intent_id, "candidate_ids": [tid], "target_candidate_id": tid, "scope_artifact_ids": sorted(ch.arts.values())})
    assert r.status_code == 200, r.text
    return jid


def _confirmed_intent(env, ch):
    return intent(env, ch.admin, S1, confirm=True)["intent_id"]


def _outbox(env, jid):
    with env.store._tx() as cur:
        cur.execute("""SELECT id, attempts, lease_owner, fencing_token, status FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED'
                       AND payload->>'job_id'=%s ORDER BY id""", (env.tenant, env.ws, jid))
        return cur.fetchall()


def _lease_remaining_seconds(env, jid):
    with env.store._tx() as cur:
        cur.execute("""SELECT extract(epoch FROM (lease_until - now())) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED' AND payload->>'job_id'=%s""", (env.tenant, env.ws, jid))
        return float(cur.fetchone()[0])


def _expire_lease(env, jid):
    """Time passing: the dead worker's lease runs out (the real lease is the base 30 s, stretched to the provider timeout plus a margin when the budget config is loaded)."""
    with env.store._tx() as cur:
        cur.execute("""UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED' AND payload->>'job_id'=%s""", (env.tenant, env.ws, jid))


def _attempts(env, jid):
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (env.tenant, env.ws, jid))
        return cur.fetchone()[0]


def test_a_worker_dying_mid_job_loses_nothing_and_the_job_completes_once_after_the_lease_expires(env, ch, monkeypatch):
    jid = _impact_job(env, ch, _confirmed_intent(env, ch))
    real = job_worker.resolve_executor

    def dying(kind):
        ex = real(kind)
        if kind != "impact" or ex is None:
            return ex

        class Dying:
            def __getattr__(self, name):
                return getattr(ex, name)

            def execute(self, ctx, request, calls):
                raise WorkerDied("process gone while executing")
        return Dying()
    monkeypatch.setattr(job_worker, "resolve_executor", dying)
    with pytest.raises(WorkerDied):
        while job_worker.claim_and_admit_one(env.store, env.tenant, env.ws, "worker-A", lease_seconds=1) is not None:
            pass
    body = job_body(env.admin_client(), env, jid)
    assert body["execution_status"] not in ("COMPLETED", "FAILED", "CANCELLED") and not body.get("execution_result")      # nothing half-written
    rows = _outbox(env, jid)
    assert len(rows) == 1 and rows[0][4] != "DONE"                      # the event is still un-acked: the job is not lost
    token_a = rows[0][3]
    monkeypatch.setattr(job_worker, "resolve_executor", real)
    assert _lease_remaining_seconds(env, jid) > 5                       # a live lease: worker B cannot take the job yet (no double execution while A may still be working)
    assert job_worker.claim_and_admit_one(env.store, env.tenant, env.ws, "worker-B", lease_seconds=30) is None
    _expire_lease(env, jid)                                             # the dead worker never heartbeats; its lease runs out
    while job_worker.claim_and_admit_one(env.store, env.tenant, env.ws, "worker-B", lease_seconds=30) is not None:
        pass
    done = job_body(env.admin_client(), env, jid)
    assert done["execution_status"] == "COMPLETED" and done["execution_result"]
    rows = _outbox(env, jid)
    assert len(rows) == 1 and rows[0][4] == "DONE" and rows[0][3] > token_a         # acked once (status DONE), under a higher fencing token than the dead worker had
    assert _attempts(env, jid) == 1                                      # the dead attempt left no record; the reclaim recorded its own


def test_a_worker_dying_after_the_result_was_committed_but_before_the_ack_does_not_publish_twice(env, ch, monkeypatch):
    jid = _impact_job(env, ch, _confirmed_intent(env, ch))
    real_ack = env.store.ack_outbox
    state = {"died": False}

    def ack_then_die(tenant, ws, claim_id, worker, token, *a, **k):
        if not state["died"] and worker == "worker-A":
            state["died"] = True
            raise WorkerDied("process gone between the committed result and the ack")
        return real_ack(tenant, ws, claim_id, worker, token, *a, **k)
    monkeypatch.setattr(env.store, "ack_outbox", ack_then_die)
    with pytest.raises(WorkerDied):
        while job_worker.claim_and_admit_one(env.store, env.tenant, env.ws, "worker-A", lease_seconds=1) is not None:
            pass
    first = job_body(env.admin_client(), env, jid)
    assert first["execution_status"] == "COMPLETED"                     # the result IS committed
    result_before = first["execution_result"]
    _expire_lease(env, jid)
    while job_worker.claim_and_admit_one(env.store, env.tenant, env.ws, "worker-B", lease_seconds=30) is not None:
        pass
    after = job_body(env.admin_client(), env, jid)
    assert after["execution_result"] == result_before and after["execution_status"] == "COMPLETED"        # not recomputed, not overwritten
    rows = _outbox(env, jid)
    assert len(rows) == 1 and rows[0][4] == "DONE"                         # acked by the reclaiming worker, nothing delivered twice
    assert _attempts(env, jid) <= 1


