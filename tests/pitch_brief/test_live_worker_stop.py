"""The test LiveWorker stops before the next test starts: `stop.set()` waits for a job still in flight (no database needed).

Regression for a fixture error seen in an in-image run: a job of the previous test was still querying while the next test's `store.migrate()` took its
table locks, and PostgreSQL reported `deadlock detected`."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from infra.worker import job_worker
from tests.pitch_brief.conftest import LiveWorker


def test_stop_waits_for_the_job_in_flight(monkeypatch):
    started, finished = threading.Event(), threading.Event()

    def slow_job(store, tenant, ws, worker_id):
        started.set()
        time.sleep(1.0)                                          # a job still running when the test ends
        finished.set()
        return {"job_id": "j"}

    monkeypatch.setattr(job_worker, "claim_and_admit_one", slow_job)
    worker = LiveWorker(SimpleNamespace(store=None, tenant="t", ws="w")).start()
    assert started.wait(5)
    worker.stop.set()
    assert finished.is_set() and not worker._t.is_alive()       # nothing of this worker runs after stop returns
    assert worker.stop.is_set()


def test_an_idle_worker_stops_at_once(monkeypatch):
    monkeypatch.setattr(job_worker, "claim_and_admit_one", lambda *a: None)
    worker = LiveWorker(SimpleNamespace(store=None, tenant="t", ws="w")).start()
    time.sleep(0.1)
    t0 = time.monotonic()
    worker.stop.set()
    assert not worker._t.is_alive() and time.monotonic() - t0 < 2
