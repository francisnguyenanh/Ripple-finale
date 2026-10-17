"""`tools.db_migrate` on a FRESH disposable database of the test server: showing the target applies nothing and never prints the password; `--apply` creates the v2 and identity
schema (including the extension tables), a second `--apply` is a no-op success; a missing DSN is exit 2. The database is created and dropped by the test."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid

import psycopg2
import psycopg2.extensions
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASE = os.environ.get("AI2_T02_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not BASE, reason="needs the disposable test PostgreSQL in AI2_T02_TEST_DSN")


def _dsn(dbname):
    p = psycopg2.extensions.parse_dsn(BASE)
    p["dbname"] = dbname
    return " ".join(f"{k}={v}" for k, v in p.items())


def _tool(*args, dsn=None):
    env = {k: v for k, v in os.environ.items() if k != "RIPPLE_V2_DSN"}
    if dsn:
        env["RIPPLE_V2_DSN"] = dsn
    return subprocess.run([sys.executable, "-m", "tools.db_migrate", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def _tables(dsn):
    with psycopg2.connect(dsn) as c, c.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        return {r[0] for r in cur.fetchall()}


@pytest.fixture
def fresh_db():
    name = "migrate_check_" + uuid.uuid4().hex[:10]
    admin = psycopg2.connect(_dsn("postgres"))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _dsn(name)
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_show_applies_nothing_apply_creates_the_schema_and_a_second_apply_is_a_no_op(fresh_db):
    hidden = psycopg2.extensions.parse_dsn(fresh_db).get("password")
    shown = _tool(dsn=fresh_db)
    assert shown.returncode == 0 and "nothing applied" in shown.stdout and "dbname=migrate_check_" in shown.stdout
    assert not hidden or hidden not in shown.stdout + shown.stderr
    assert _tables(fresh_db) == set()
    first = _tool("--apply", dsn=fresh_db)
    assert first.returncode == 0, first.stderr
    tables = _tables(fresh_db)
    assert {"v2_workspaces", "v2_jobs", "v2_outbox", "v2_projection_checkpoints", "v2_inbox_items", "v2_business_change_intents"} <= tables
    assert "v2_external_identities" in tables                                                                                # identity store's extension table (ext_t06)
    second = _tool("--apply", dsn=fresh_db)
    assert second.returncode == 0 and _tables(fresh_db) == tables


def test_a_missing_dsn_is_exit_2():
    r = _tool("--apply")
    assert r.returncode == 2 and "RIPPLE_V2_DSN is not set" in r.stderr
