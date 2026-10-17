"""Provider-attempt accounting at every crash boundary of a worker (the gap found by failure injection: a provider call made by a worker that died was not in the ledger).
Boundaries: claim -> reserve -> [crash 1] -> build request -> durable attempt row IN_FLIGHT at the attempt ceiling -> [crash 2] -> provider call -> response -> [crash 3] ->
store result + settle -> [crash 4] -> ack. Stub provider only. Nothing here claims exactly-once provider billing: it proves that an attempt that MAY have been charged is never
free, never released for reuse, never counted twice, never allowed to widen the approved number of provider attempts, and can be reconciled with an audit."""
from __future__ import annotations

import pytest

from infra.worker import change_draft, job_worker
from tests.pitch_brief.chain import S1, build, intent
from tests.pitch_brief.conftest import create_job, job_body
from tests.pitch_brief.test_change_draft import Model
from tests.pitch_brief.test_worker_failure_injection import WorkerDied, _expire_lease


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _attempt_rows(env, jid):
    with env.store._tx() as cur:
        cur.execute("""SELECT outcome, provider_calls, cost_usd, cost_estimate_usd, input_tokens, attempt_key FROM v2_job_attempts
                       WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND outcome <> 'IN_FLIGHT_RESOLVED' ORDER BY recorded_at, attempt_key""", (env.tenant, env.ws, jid))
        return cur.fetchall()


def _ledger(env, jid):
    with env.store._tx() as cur:
        cur.execute("SELECT entry_state, reserved_usd, cost_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (env.tenant, env.ws, jid))
        return cur.fetchall()


def _ai_job(env, ch, monkeypatch, model):
    iid = intent(env, ch.admin, S1, confirm=True)["intent_id"]
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: model)
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    jid, r = create_job(env.req_client(), env, "change.draft", {"intent_id": iid, "extraction_job_ids": list(ch.jobs.values())})
    assert r.status_code == 200, r.text
    return jid


def _run(env, worker, lease=30):
    while job_worker.claim_and_admit_one(env.store, env.tenant, env.ws, worker, lease_seconds=lease) is not None:
        pass


def test_boundary_1_a_crash_before_any_provider_attempt_books_nothing_and_the_reclaim_calls_the_provider_once(env, ch, monkeypatch):
    model = Model()
    jid = _ai_job(env, ch, monkeypatch, model)
    real = job_worker.resolve_executor

    def dying(kind):
        ex = real(kind)
        if kind != "change.draft":
            return ex

        class Dying:
            def __getattr__(self, name):
                return getattr(ex, name)

            def prepare(self, ctx):
                raise WorkerDied("process gone after the claim, before the request was built")
        return Dying()
    monkeypatch.setattr(job_worker, "resolve_executor", dying)
    with pytest.raises(WorkerDied):
        _run(env, "worker-A")
    assert model.calls == 0 and _attempt_rows(env, jid) == []
    assert all(r[0] == "RESERVED" for r in _ledger(env, jid))
    monkeypatch.setattr(job_worker, "resolve_executor", real)
    _expire_lease(env, jid)
    _run(env, "worker-B")
    assert model.calls == 1 and [r[0] for r in _attempt_rows(env, jid)] == ["COMPLETED"]
    assert all(r[0] == "SETTLED" for r in _ledger(env, jid))


def test_boundary_2_a_crash_with_the_provider_attempt_in_flight_is_booked_as_usage_unknown_at_its_ceiling(env, ch, monkeypatch):
    model = Model(fail=WorkerDied("process gone while the provider call was in flight"))
    jid = _ai_job(env, ch, monkeypatch, model)
    with pytest.raises(WorkerDied):
        _run(env, "worker-A")
    assert model.calls == 1
    rows = _attempt_rows(env, jid)
    assert [r[0] for r in rows] == ["IN_FLIGHT"] and rows[0][1] == 1 and float(rows[0][2]) > 0          # durable BEFORE the provider answered, booked at the ceiling
    assert all(r[0] == "RESERVED" for r in _ledger(env, jid))
    ceiling = float(rows[0][2])
    healthy = Model()
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: healthy)
    _expire_lease(env, jid)
    _run(env, "worker-B")
    rows = _attempt_rows(env, jid)
    assert sorted(r[0] for r in rows) == ["COMPLETED", "USAGE_UNKNOWN"]
    unknown = next(r for r in rows if r[0] == "USAGE_UNKNOWN")
    done = next(r for r in rows if r[0] == "COMPLETED")
    assert float(unknown[2]) == ceiling and unknown[4] is None          # estimated (no tokens), never free, never released
    assert done[4] is not None                                           # the second call's real usage, kept apart from the estimate
    ledger = _ledger(env, jid)
    assert all(r[0] == "SETTLED" for r in ledger) and abs(float(ledger[0][2]) - (ceiling + float(done[2]))) < 1e-9       # settled once: unknown estimate + actual
    assert float(ledger[0][2]) <= float(ledger[0][1])                    # inside what was reserved for the approved attempts
    assert model.calls == 1 and healthy.calls == 1                       # the provider was contacted twice for one job and BOTH are in the ledger


def test_boundary_3_a_crash_after_the_answer_but_before_the_result_is_stored_books_both_calls(env, ch, monkeypatch):
    model = Model()
    jid = _ai_job(env, ch, monkeypatch, model)
    real_finish = env.store.finish_execution
    state = {"n": 0}

    def finish_then_die(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise WorkerDied("process gone after the provider answered, before the result was stored")
        return real_finish(*a, **k)
    monkeypatch.setattr(env.store, "finish_execution", finish_then_die)
    with pytest.raises(WorkerDied):
        _run(env, "worker-A")
    assert model.calls == 1 and [r[0] for r in _attempt_rows(env, jid)] == ["IN_FLIGHT"]          # the answer was never stored: still "usage unknown"
    assert all(r[0] == "RESERVED" for r in _ledger(env, jid))
    _expire_lease(env, jid)
    monkeypatch.setattr(env.store, "finish_execution", real_finish)
    _run(env, "worker-B")
    assert job_body(env.admin_client(), env, jid)["execution_status"] == "COMPLETED" and model.calls == 2
    assert sorted(r[0] for r in _attempt_rows(env, jid)) == ["COMPLETED", "USAGE_UNKNOWN"]
    assert all(r[0] == "SETTLED" for r in _ledger(env, jid))


def test_boundary_4_repeated_crashes_never_widen_the_approved_provider_attempts(env, ch, monkeypatch):
    """The outbox allows up to 5 claims (crashes of any kind); the retry limit approves 2 provider attempts. Two crashes in flight use both: the third claim must NOT call the provider."""
    dead = Model(fail=WorkerDied("process gone in flight"))
    jid = _ai_job(env, ch, monkeypatch, dead)
    for worker in ("worker-A", "worker-B"):
        with pytest.raises(WorkerDied):
            _run(env, worker)
        _expire_lease(env, jid)
    assert dead.calls == 2
    healthy = Model()
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: healthy)
    _run(env, "worker-C")
    body = job_body(env.admin_client(), env, jid)
    assert healthy.calls == 0                                            # no third provider attempt
    assert body["execution_status"] in ("FAILED", "CANCELLED") and "provider_attempt_limit_reached" in str(body["execution_reason"])
    rows = _attempt_rows(env, jid)
    assert [r[0] for r in rows] == ["USAGE_UNKNOWN", "USAGE_UNKNOWN"]
    ledger = _ledger(env, jid)
    assert all(r[0] == "SETTLED" for r in ledger)                        # settled once, not left reserved, not released as free
    assert abs(float(ledger[0][2]) - sum(float(r[2]) for r in rows)) < 1e-9 and float(ledger[0][2]) <= float(ledger[0][1]) + 1e-9


def test_boundary_5_an_unknown_attempt_is_reconciled_once_with_an_audit_and_the_ledger_follows(env, ch, monkeypatch):
    dead = Model(fail=WorkerDied("process gone in flight"))
    jid = _ai_job(env, ch, monkeypatch, dead)
    with pytest.raises(WorkerDied):
        _run(env, "worker-A")
    healthy = Model()
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: healthy)
    _expire_lease(env, jid)
    _run(env, "worker-B")
    unknown = next(r for r in _attempt_rows(env, jid) if r[0] == "USAGE_UNKNOWN")
    before = float(_ledger(env, jid)[0][2])
    out = env.store.reconcile_unknown_attempt(env.tenant, env.ws, jid, unknown[5], actual_cost_usd="0.004", actor_id="operator", reason="provider billing shows 0.004 USD for this call")
    assert float(out["booked_usd"]) == float(unknown[2])
    after = float(_ledger(env, jid)[0][2])
    assert after < before and abs((before - after) - (float(unknown[2]) - 0.004)) < 1e-9
    assert [r[0] for r in _attempt_rows(env, jid)].count("USAGE_UNKNOWN_RECONCILED") == 1
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='job_attempt.reconciled'", (env.tenant, env.ws))
        assert cur.fetchone()[0] == 1
    with pytest.raises(Exception):                                       # not twice
        env.store.reconcile_unknown_attempt(env.tenant, env.ws, jid, unknown[5], actual_cost_usd="0.001", actor_id="operator", reason="a second attempt to reconcile")


def test_a_live_claim_during_a_long_provider_call_cannot_be_taken_by_another_worker(env, ch, monkeypatch):
    seen = {}

    def during_call(obj, prompt):
        seen["other"] = job_worker.claim_and_admit_one(env.store, env.tenant, env.ws, "worker-B", lease_seconds=30)     # a second worker polls while the call is running
        with env.store._tx() as cur:
            cur.execute("SELECT extract(epoch FROM (lease_until - now())) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED' AND status='CLAIMED'", (env.tenant, env.ws))
            seen["lease"] = [float(r[0]) for r in cur.fetchall()]
    model = Model(mutate=during_call)
    jid = _ai_job(env, ch, monkeypatch, model)
    _run(env, "worker-A", lease=30)
    assert seen["other"] is None                                          # nothing reclaimable: the lease covers the call (provider timeout plus margin)
    assert seen["lease"] and max(seen["lease"]) > 100                    # base 30 s stretched to the provider timeout (180 s in the approved config) plus the 15 s margin
    assert job_body(env.admin_client(), env, jid)["execution_status"] == "COMPLETED" and model.calls == 1
