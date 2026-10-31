"""SEC-02: two real server processes (two uvicorn processes, one database) share the failed-login lock: failures counted by one process lock the pair
in the other; the lock answers 429 before the credential check in both."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from uuid import uuid4

import psycopg2
import pytest
import requests

DSN = os.environ.get("AI2_T02_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs a disposable PostgreSQL (AI2_T02_TEST_DSN)")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(port: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "uvicorn", "infra.judge:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait(port: int, proc: subprocess.Popen, seconds: int = 60) -> None:
    for _ in range(seconds * 4):
        if proc.poll() is not None:
            raise RuntimeError("server exited")
        try:
            if requests.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise RuntimeError("server did not start")


def test_failures_counted_by_one_process_lock_the_pair_in_the_other():
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("test database not reachable")
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store
    PostgresV2Store(DSN, None).migrate()
    ids = PostgresIdentityStore(DSN)
    ids.migrate()
    acct, cred = f"p-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}-good"
    ids.create_account(acct, cred)
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("DELETE FROM v2_login_failures")
    p1, p2 = _free_port(), _free_port()
    env = {**os.environ, "RIPPLE_V2_DSN": DSN, "RIPPLE_RATE_SHARED": "1", "RIPPLE_TRUSTED_PROXY_HOPS": "1", "RIPPLE_LOGIN_SCHEMA_AUTO": "0",
           "PYTHONIOENCODING": "utf-8"}
    procs = [_start(p1, {**env, "RIPPLE_JUDGE_ORIGIN": f"http://127.0.0.1:{p1}"}), _start(p2, {**env, "RIPPLE_JUDGE_ORIGIN": f"http://127.0.0.1:{p2}"})]
    try:
        for port, proc in zip((p1, p2), procs):
            _wait(port, proc)

        def login(port, credential, source):
            return requests.post(f"http://127.0.0.1:{port}/v2/auth/login", json={"account_id": acct, "credential": credential},
                                 headers={"Origin": f"http://127.0.0.1:{port}", "X-Ripple-Judge": "1", "X-Forwarded-For": source}, timeout=10)

        for _ in range(3):
            assert login(p1, "wrong", "203.0.113.80").status_code == 401
        for _ in range(2):
            assert login(p2, "wrong", "203.0.113.80").status_code == 401
        assert login(p1, cred, "203.0.113.80").status_code == 429, "process 1 sees the lock built by both"
        assert login(p2, cred, "203.0.113.80").status_code == 429, "process 2 as well"
        assert login(p2, cred, "203.0.113.81").status_code == 200, "another source signs in"
        with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
            cur.execute("SELECT failures, locked_until FROM v2_login_failures WHERE key=%s", ("as:" + acct + "|203.0.113.80",))
            failures, locked_until = cur.fetchone()
            assert failures == 0 and locked_until > time.time(), "the lock lives in the shared table with its counter reset"
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
