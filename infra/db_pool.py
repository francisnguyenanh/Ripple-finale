"""A small, bounded connection pool for the stores (found on the cloud candidate: a micro Cloud SQL instance allows 25 connections, and every request opened its own connection, so one page
that loads 17 jobs in parallel exhausted them: "remaining connection slots are reserved ..." and HTTP 500).

Behaviour, per process:
  * `RIPPLE_DB_POOL_MAX` (default 0 = off, the old connect-per-use behaviour, so local tests and tools are unchanged). N > 0 = at most N open connections per DSN in this process.
  * a caller waits for a free connection up to `RIPPLE_DB_POOL_WAIT` seconds (default 15), then gets `PoolBusy` (an OperationalError): overload becomes a bounded wait and then a clear
    refusal, not an unbounded number of connections.
  * a connection goes back to the pool only when it is healthy and idle (no open transaction); anything else is closed. A connection used by the stores has always committed or rolled back
    before it comes back (they use `with conn:`), so no transaction state leaks between requests.
  * sizing: (Cloud Run instances x pool max) + worker + operators must stay under the database's `max_connections` minus its reserved ones; the candidate is limited to 2 instances.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

import psycopg2
from psycopg2 import extensions

_lock = threading.Lock()
_pools: dict[str, "_Pool"] = {}


class PoolBusy(psycopg2.OperationalError):
    """No database connection became free in time."""


def _size() -> int:
    try:
        return max(0, int(os.environ.get("RIPPLE_DB_POOL_MAX", "0")))
    except ValueError:
        return 0


def _wait() -> float:
    try:
        return max(0.1, float(os.environ.get("RIPPLE_DB_POOL_WAIT", "15")))
    except ValueError:
        return 15.0


class _Pool:
    def __init__(self, dsn: str, size: int):
        self.dsn, self.size = dsn, size
        self.slots = threading.BoundedSemaphore(size)
        self.idle: list = []
        self.guard = threading.Lock()
        self.peak = 0
        self.in_use = 0
        self.timeouts = 0

    def acquire(self):
        if not self.slots.acquire(timeout=_wait()):
            with self.guard:
                self.timeouts += 1
            raise PoolBusy(f"no database connection free after {_wait():.0f}s (pool of {self.size})")
        try:
            with self.guard:
                self.in_use += 1
                self.peak = max(self.peak, self.in_use)
                conn = self.idle.pop() if self.idle else None
            while conn is not None and (conn.closed or conn.info.transaction_status != extensions.TRANSACTION_STATUS_IDLE):
                try:
                    conn.close()
                except Exception:      # noqa: BLE001
                    pass
                with self.guard:
                    conn = self.idle.pop() if self.idle else None
            return conn if conn is not None else psycopg2.connect(self.dsn, connect_timeout=10)
        except BaseException:
            with self.guard:
                self.in_use -= 1
            self.slots.release()
            raise

    def release(self, conn) -> None:
        try:
            healthy = (not conn.closed) and conn.info.transaction_status == extensions.TRANSACTION_STATUS_IDLE
        except Exception:              # noqa: BLE001
            healthy = False
        with self.guard:
            self.in_use -= 1
            if healthy and len(self.idle) < self.size:
                self.idle.append(conn)
                conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:          # noqa: BLE001
                pass
        self.slots.release()


def _pool_for(dsn: str, size: int) -> _Pool:
    with _lock:
        p = _pools.get(dsn)
        if p is None or p.size != size:
            p = _pools[dsn] = _Pool(dsn, size)
        return p


@contextmanager
def connection(dsn: str):
    """Yield a connection; close it at the end (pool off) or give it back (pool on). Callers keep using `with conn:` for the transaction."""
    size = _size()
    if size <= 0:
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()
        return
    pool = _pool_for(dsn, size)
    conn = pool.acquire()
    try:
        yield conn
    finally:
        pool.release(conn)


def stats(dsn: str) -> dict:
    p = _pools.get(dsn)
    return {"size": p.size, "in_use": p.in_use, "idle": len(p.idle), "peak": p.peak, "timeouts": p.timeouts} if p else {}


def busy_counts() -> dict:
    """AH-01.6: process-wide pool counters for the overload log line (no DSN, no identifiers)."""
    with _lock:
        pools = list(_pools.values())
    return {"pools": len(pools), "in_use": sum(p.in_use for p in pools), "peak": max((p.peak for p in pools), default=0),
            "timeouts": sum(p.timeouts for p in pools)}


def reset() -> None:
    with _lock:
        for p in _pools.values():
            for c in p.idle:
                try:
                    c.close()
                except Exception:      # noqa: BLE001
                    pass
        _pools.clear()
