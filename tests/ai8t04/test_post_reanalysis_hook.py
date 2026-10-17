"""The checks for the NEW readings after an apply (`job_worker._checks_for_new_readings`): only when the last reanalysis reading of THAT apply is finished (completed or failed), never early,
never for another job, harmless when it raises, and idempotent when repeated."""
from __future__ import annotations

import pytest

from infra import v2_coverage
from infra.worker import job_worker as JW
from tests.ai8t04.conftest import awaiting_change


class _Store:
    def __init__(self, statuses, rebuild_ids=("ra", "rb")):
        self.statuses, self.rebuild_ids = statuses, rebuild_ids

    def job_record(self, t, ws, job_id):
        if job_id.startswith("rebuild"):
            return {"execution_result": {"reanalysis": {"job_ids": list(self.rebuild_ids)}}}
        if job_id not in self.statuses:
            raise KeyError(job_id)
        return {"execution_status": self.statuses[job_id]}


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(v2_coverage, "ensure", lambda *a, **k: seen.append(a[1:]))
    monkeypatch.setattr("infra.worker.verification.rebuild_job_id", lambda apply_id: "rebuild-" + apply_id)
    return seen


JOB = {"kind": "extraction", "payload": {"caused_by_apply_job_id": "ap1"}}


def test_not_early_while_another_reanalysis_reading_is_still_to_come(calls):
    JW._checks_for_new_readings(_Store({"ra": "COMPLETED", "rb": "RUNNING"}), "t", "w", JOB)
    JW._checks_for_new_readings(_Store({"ra": "COMPLETED", "rb": "QUEUED"}), "t", "w", JOB)
    assert calls == []


def test_runs_when_the_last_reading_is_in_completed_or_failed_and_a_retry_that_succeeds_runs_it_again(calls):
    JW._checks_for_new_readings(_Store({"ra": "COMPLETED", "rb": "COMPLETED"}), "t", "w", JOB)
    assert len(calls) == 1
    JW._checks_for_new_readings(_Store({"ra": "COMPLETED", "rb": "FAILED"}), "t", "w", JOB)       # a failed last reading must not leave the others unchecked
    assert len(calls) == 2
    JW._checks_for_new_readings(_Store({"ra": "COMPLETED", "rb": "COMPLETED"}), "t", "w", JOB)    # the retry of that reading completes: checks again (deterministic ids make it idempotent)
    assert len(calls) == 3


def test_not_for_other_jobs_and_never_raises(calls, monkeypatch):
    s = _Store({"ra": "COMPLETED", "rb": "COMPLETED"})
    JW._checks_for_new_readings(s, "t", "w", {"kind": "finding", "payload": {"caused_by_apply_job_id": "ap1"}})
    JW._checks_for_new_readings(s, "t", "w", {"kind": "extraction", "payload": {}})                # an ordinary import extraction: no apply behind it
    JW._checks_for_new_readings(_Store({"ra": "COMPLETED"}), "t", "w", JOB)                         # a late/unknown reading of that apply: nothing is guessed
    assert calls == []
    monkeypatch.setattr(v2_coverage, "ensure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    JW._checks_for_new_readings(s, "t", "w", JOB)                                                  # a failing check never turns the finished job into a failure


def test_repeating_the_checks_creates_nothing_new(setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    store, t, ws = setup["store"], setup["tenant"], change["workspace"]
    count = lambda: _count(store, t, ws)
    before = count()
    v2_coverage.ensure(store, t, ws)
    v2_coverage.ensure(store, t, ws)
    assert count() == before


def _count(store, t, ws):
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s", (t, ws))
        return cur.fetchone()[0]
