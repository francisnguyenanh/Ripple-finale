"""SEC-02 schema path (migration v2_042): a fresh database gets the table from the migration; a database that holds the C13-shaped table (no
`locked_until`) keeps its rows and gains the column; the migration re-runs without effect; the code-side `ensure_schema` is the same statements;
old code (which never reads the column) keeps working on the new shape; and with RIPPLE_LOGIN_SCHEMA_AUTO=0 the login path issues no DDL."""
from __future__ import annotations

import os

import psycopg2
import pytest

DSN = os.environ.get("AI2_T02_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs a disposable PostgreSQL (AI2_T02_TEST_DSN)")


def _columns(cur):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='v2_login_failures' ORDER BY ordinal_position")
    return [r[0] for r in cur.fetchall()]


def test_upgrade_of_the_c13_shaped_table_keeps_rows_and_adds_the_lock_column():
    try:
        cx = psycopg2.connect(DSN + " connect_timeout=2")
    except psycopg2.OperationalError:
        pytest.skip("test database not reachable")
    from infra.adapters.v2_postgres import PostgresV2Store
    with cx, cx.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS v2_login_failures")
        cur.execute("CREATE TABLE v2_login_failures (key TEXT NOT NULL, window_start BIGINT NOT NULL, failures INTEGER NOT NULL, PRIMARY KEY (key, window_start))")
        cur.execute("INSERT INTO v2_login_failures VALUES ('a:old', 100, 3)")
    PostgresV2Store(DSN, None).migrate()
    with cx, cx.cursor() as cur:
        assert _columns(cur) == ["key", "window_start", "failures", "locked_until"]
        cur.execute("SELECT key, window_start, failures, locked_until FROM v2_login_failures")
        assert cur.fetchall() == [("a:old", 100, 3, 0)], "existing rows survive with a zero lock"
    PostgresV2Store(DSN, None).migrate()                                   # idempotent
    with cx, cx.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_login_failures")
        assert cur.fetchone()[0] == 1
        # the C13 code's statements (no lock column) still work on the upgraded shape: old code on the new schema
        cur.execute("INSERT INTO v2_login_failures (key, window_start, failures) VALUES ('s:x', 101, 1) ON CONFLICT (key, window_start) DO UPDATE SET failures = v2_login_failures.failures + 1")
        cur.execute("SELECT failures FROM v2_login_failures WHERE key='s:x' AND window_start=101")
        assert cur.fetchone()[0] == 1
    cx.close()


def test_fresh_database_and_code_side_ensure_agree():
    try:
        cx = psycopg2.connect(DSN + " connect_timeout=2")
    except psycopg2.OperationalError:
        pytest.skip("test database not reachable")
    from infra import login_protection
    from infra.adapters.v2_postgres import PostgresV2Store
    with cx, cx.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS v2_login_failures")
    PostgresV2Store(DSN, None).migrate()
    with cx, cx.cursor() as cur:
        fresh = _columns(cur)
    login_protection.ensure_schema(cx)
    with cx, cx.cursor() as cur:
        assert _columns(cur) == fresh == ["key", "window_start", "failures", "locked_until"]
    cx.close()


def test_login_path_issues_no_ddl_when_schema_auto_is_off(monkeypatch):
    try:
        cx = psycopg2.connect(DSN + " connect_timeout=2")
    except psycopg2.OperationalError:
        pytest.skip("test database not reachable")
    from infra import v2_http_guard
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    monkeypatch.setenv("RIPPLE_RATE_SHARED", "1")
    monkeypatch.setenv("RIPPLE_LOGIN_SCHEMA_AUTO", "0")
    v2_http_guard._login_ready = False
    with cx, cx.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS v2_login_failures")
    with pytest.raises(psycopg2.Error):
        v2_http_guard.login_wait("a", "b")                                  # the table is the migration's job: the request path creates nothing
    from infra.adapters.v2_postgres import PostgresV2Store
    PostgresV2Store(DSN, None).migrate()
    v2_http_guard._login_ready = False
    assert v2_http_guard.login_wait("a", "b") == 0
    cx.close()
