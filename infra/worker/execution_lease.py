"""One worker execution at a time (REL-04 / plan A1.4, 2026-10-30).

A Cloud Run job's `tasks` / `parallelism` bound the tasks of ONE execution, not the number of executions a wake-up or a scheduler can start, and the
wake-up's own refusals (a fresh heartbeat, a recent kick) are approximate. This lease is the enforced bound: the worker holds a PostgreSQL
session-level advisory lock (`pg_try_advisory_lock`) on a dedicated connection for its whole life; a second execution that cannot take it exits at
once with `another_execution_active` (exit code 0: nothing to do, not a failure), so the connection envelope can count worker executions as one
plus the overlap of an exiting one. The lock is released by the server when the holder's session ends, so a killed execution never blocks the next.
No schema, no row. Disabled with RIPPLE_WORKER_EXECUTION_LEASE=0 (local tests that run several workers on purpose).
"""
from __future__ import annotations

import logging
import os

LOCK_ID = 0x52495057                                    # "RIPW": one lock per database
LOG = logging.getLogger("ripple.worker")


class ExecutionLease:
    def __init__(self, conn):
        self.conn = conn

    def release(self) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
            self.conn.commit()
        except Exception:                               # noqa: BLE001  the session end releases it anyway
            pass
        try:
            self.conn.close()
        except Exception:                               # noqa: BLE001
            pass


def enabled() -> bool:
    return os.environ.get("RIPPLE_WORKER_EXECUTION_LEASE", "1") != "0"


def acquire(dsn: str) -> ExecutionLease | None:
    """The lease, or None when another execution holds it. The dedicated connection is NOT taken from the worker's pool: it counts as one extra
    connection of the worker process in the envelope (`tools/db_envelope.py`)."""
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,))
        got = bool(cur.fetchone()[0])
    if not got:
        conn.close()
        LOG.warning("worker_execution_lease another_execution_active")
        return None
    return ExecutionLease(conn)
