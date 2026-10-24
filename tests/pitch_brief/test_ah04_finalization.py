"""AH-04.3 finalization crash matrix, the cases not already covered by test_worker_attempt_accounting.py (boundaries 1-5) and the lease-loss tests:
  * the result and its settlement were committed, but the acknowledgement was lost (the process died right after the commit): the redelivered event finds the
    terminal job, makes no provider call, books nothing again and acknowledges;
  * a duplicate delivery of the terminal transition (a late second finish) is refused without touching the settled ledger.
Stub provider only; throwaway PostgreSQL."""
from __future__ import annotations

import pytest

from infra.adapters.v2_postgres import StoreConflict
from infra.worker import job_worker
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import job_body
from tests.pitch_brief.test_change_draft import Model
from tests.pitch_brief.test_worker_attempt_accounting import _ai_job, _attempt_rows, _ledger, _run
from tests.pitch_brief.test_worker_failure_injection import WorkerDied, _expire_lease


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def test_a_lost_acknowledgement_after_the_commit_neither_calls_nor_books_again(env, ch, monkeypatch):
    model = Model()
    jid = _ai_job(env, ch, monkeypatch, model)
    real_ack = job_worker._ack
    state = {"died": False}

    def dying_ack(store, tenant, ws, claim, worker_id):
        if not state["died"] and job_body(env.admin_client(), env, jid)["execution_status"] == "COMPLETED":
            state["died"] = True
            raise WorkerDied("process gone after the commit, before the acknowledgement")
        return real_ack(store, tenant, ws, claim, worker_id)

    monkeypatch.setattr(job_worker, "_ack", dying_ack)
    with pytest.raises(WorkerDied):
        _run(env, "worker-A")
    assert state["died"] and model.calls == 1
    rows_before, ledger_before = _attempt_rows(env, jid), _ledger(env, jid)
    assert [r[0] for r in rows_before] == ["COMPLETED"] and all(r[0] == "SETTLED" for r in ledger_before)
    monkeypatch.setattr(job_worker, "_ack", real_ack)
    _expire_lease(env, jid)
    _run(env, "worker-B")                                                    # the redelivered event: already terminal
    assert model.calls == 1                                                  # no second dispatch
    assert _attempt_rows(env, jid) == rows_before and _ledger(env, jid) == ledger_before
    assert job_body(env.admin_client(), env, jid)["execution_status"] == "COMPLETED"


def test_a_late_second_terminal_transition_is_refused_and_the_ledger_is_unchanged(env, ch, monkeypatch):
    model = Model()
    jid = _ai_job(env, ch, monkeypatch, model)
    _run(env, "worker-A")
    ledger = _ledger(env, jid)
    with pytest.raises(StoreConflict):
        env.store.finish_execution(env.tenant, env.ws, jid, "FAILED", reason="late_old_worker", attempt={"attempt_key": "late", "outcome": "FAILED",
                                                                                                             "provider_calls": 1, "cost_usd": "9.99"})
    assert _ledger(env, jid) == ledger and all(r[5] != "late" for r in _attempt_rows(env, jid))
