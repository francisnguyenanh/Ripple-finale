"""KNO-01: upgrading a database that already holds data (schema before fkl_t01) keeps that data and is safe to repeat.

Runs on its own throwaway database inside the test PostgreSQL (created and dropped here). Needs the DB of tests/pitch_brief."""
from __future__ import annotations

import hashlib
import re
from uuid import uuid4

import psycopg2
import pytest

from infra.adapters.v2_postgres import PostgresV2Store
from tests.pitch_brief.conftest import DSN, MemoryBlobs

NEW_TABLES = ("v2_business_decisions", "v2_business_gaps", "v2_business_relations", "v2_business_rules", "v2_business_heads", "v2_business_snapshots")


def _dsn_for(db):
    return re.sub(r"dbname=\S+", f"dbname={db}", DSN)


def _digest(cur, tenant):
    out = []
    for table, order in (("v2_workspaces", "workspace_id"), ("v2_source_revisions", "revision_id"), ("v2_decisions", "decision_id"),
                         ("v2_audit", "id"), ("v2_outbox", "id")):
        cur.execute(f"SELECT t::text FROM {table} t WHERE tenant_id=%s ORDER BY {order}", (tenant,))
        out.append((table, hashlib.sha256(repr(cur.fetchall()).encode()).hexdigest()))
    return out


def test_upgrade_preserves_existing_data_and_is_repeatable():
    try:
        admin = psycopg2.connect(DSN + " connect_timeout=2")
    except psycopg2.OperationalError:
        pytest.skip("needs a disposable PostgreSQL; set PITCH_BRIEF_TEST_DSN")
    admin.autocommit = True
    db = "upg_" + uuid4().hex[:10]
    admin.cursor().execute(f"CREATE DATABASE {db}")
    try:
        dsn = _dsn_for(db)
        store = PostgresV2Store(dsn, MemoryBlobs())
        store.migrate()
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:       # return to the schema before fkl_t01
            for t in NEW_TABLES:
                cur.execute(f"DROP TABLE {t} CASCADE")
        store.create_workspace("t1", "p1", "w1")
        digest = store.stage_blob(b"legacy content")
        store.publish_source("t1", "w1", "src-1", {"a": digest}, expected_version=0, operation="source.import.commit", idempotency_key="i1")
        head = store.head("t1", "w1")
        store.record_decision("t1", "w1", "dec-1", "someone", {"action": "confirm"}, expected_version=head.version, idempotency_key="d1")
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            before = _digest(cur, "t1")
        for _ in range(2):                                              # upgrade, then upgrade again
            PostgresV2Store(dsn, MemoryBlobs()).migrate()
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                assert _digest(cur, "t1") == before                     # existing rows byte-identical
                for t in NEW_TABLES:
                    cur.execute(f"SELECT count(*) FROM {t}")
                    assert cur.fetchone()[0] == 0                       # new tables exist and are empty: nothing invented
        from infra.adapters.v2_business import business_view
        assert business_view(store, "t1", "w1")["accepted"] is None     # an upgraded workspace reads as "no business knowledge yet"
    finally:
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        admin.close()
