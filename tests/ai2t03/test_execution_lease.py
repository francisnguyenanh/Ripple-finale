"""REL-04 (2026-10-30): one worker execution at a time per database, enforced by a session advisory lock on a dedicated connection: a second
process cannot take it while the first lives, takes it as soon as the first session ends (release or death), and the service entry exits 0 with
`another_execution_active` instead of polling. Real PostgreSQL, two real processes for the entry test."""
from __future__ import annotations

import os
import subprocess
import sys

import psycopg2
import pytest

from infra.worker import execution_lease

from .conftest import AI2_T03_TEST_DSN as DSN

pytestmark = pytest.mark.skipif(not DSN, reason="needs a disposable PostgreSQL (AI2_T03_TEST_DSN)")


def _reachable():
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            return True
    except psycopg2.OperationalError:
        return False


def test_second_acquire_fails_until_the_first_session_ends():
    if not _reachable():
        pytest.skip("test database not reachable")
    first = execution_lease.acquire(DSN)
    assert first is not None
    assert execution_lease.acquire(DSN) is None, "a second execution is refused while the first holds the lease"
    first.conn.close()                                   # the holder dies without releasing: the server frees the lock with the session
    second = execution_lease.acquire(DSN)
    assert second is not None
    second.release()
    third = execution_lease.acquire(DSN)
    assert third is not None
    third.release()


def test_the_service_entry_exits_at_once_when_another_execution_holds_the_lease(tmp_path):
    if not _reachable():
        pytest.skip("test database not reachable")
    holder = execution_lease.acquire(DSN)
    assert holder is not None
    env = {**os.environ, "RIPPLE_V2_DSN": DSN, "RIPPLE_WORKER_TENANT_ID": "t-lease", "RIPPLE_WORKER_WORKSPACE_ID": "w-lease", "PORT": "0",
           "RIPPLE_WORKER_POLL_SECONDS": "1"}
    try:
        done = subprocess.run([sys.executable, "-m", "infra.worker.service"], env=env, capture_output=True, text=True, timeout=60)
    finally:
        holder.release()
    assert done.returncode == 0 and "another_execution_active" in done.stderr, (done.returncode, done.stderr[-300:])
