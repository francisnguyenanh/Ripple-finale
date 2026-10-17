"""REL-02 pre-work: the whole migration chain (ai1 .. fkl_t05) re-run on a database that already holds Final data, and the index build at a larger size.

What this proves: re-running every migration on populated tables changes no byte of the existing rows (snapshots, rules, index, intents, change decisions,
documents, jobs, outbox, audit), keeps every immutability trigger, and the index of a 500-rule snapshot builds and answers a context query within the
budget the task card names. No model, no provider. Needs the throwaway DB of tests/pitch_brief. REL-02 itself (race tests, full release checks) stays open.
"""
from __future__ import annotations

import hashlib
import time

import psycopg2
import pytest

from infra.adapters import v2_business_index
from infra.adapters.v2_postgres import PostgresV2Store
from tests.final_loop.test_chg05_commit import approved, commit
from tests.final_loop.test_kno01_business_store import KNOWLEDGE, bind, commit as commit_knowledge, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import MemoryBlobs, drain

FINAL_TABLES = ("v2_business_snapshots", "v2_business_rules", "v2_business_relations", "v2_business_gaps", "v2_business_decisions", "v2_business_heads",
                "v2_business_index_items", "v2_business_index_builds", "v2_business_change_intents", "v2_business_change_decisions", "v2_business_documents")
SHARED_TABLES = ("v2_workspaces", "v2_source_revisions", "v2_jobs", "v2_outbox", "v2_audit")
IMMUTABLE_TRIGGERS = ("v2_business_snapshot_immutable", "v2_business_rule_immutable", "v2_business_relation_immutable", "v2_business_gap_immutable",
                      "v2_business_decision_immutable", "v2_business_change_intent_immutable", "v2_business_change_decision_immutable",
                      "v2_business_document_immutable", "v2_source_revision_immutable", "v2_decision_immutable", "v2_audit_immutable")


def _digest(cur, tenant):
    out = {}
    for table in FINAL_TABLES + SHARED_TABLES:
        cur.execute(f"SELECT t::text FROM {table} t WHERE tenant_id=%s ORDER BY t::text", (tenant,))
        rows = cur.fetchall()
        out[table] = (len(rows), hashlib.sha256(repr(rows).encode()).hexdigest())
    return out


def _triggers(cur):
    cur.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
    return {r[0] for r in cur.fetchall()}


def test_rerunning_the_whole_migration_chain_on_populated_final_tables_changes_no_row_and_keeps_every_trigger(qa_env):
    q = qa_env
    cid, approver, binding = approved(q)                                            # intent + knowledge decision rows
    assert commit(q, q.admin, cid, binding).status_code == 200                      # second accepted snapshot with a TARGET rule, outbox event
    r = q.admin.post("/v2/business/documents", json={"workspace_id": q.env.ws, "modes": ["business", "technical"]})
    assert r.status_code == 200, r.text
    drain(q.env)                                                                    # index rebuild + document generation
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        before = _digest(cur, q.env.tenant)
        triggers = _triggers(cur)
    assert all(before[t][0] > 0 for t in FINAL_TABLES if t not in ("v2_business_relations",)), {t: n for t, (n, _) in before.items()}
    assert set(IMMUTABLE_TRIGGERS) <= triggers
    for _ in range(2):                                                              # upgrade, then upgrade again
        PostgresV2Store(q.env.dsn, MemoryBlobs()).migrate()
        with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
            assert _digest(cur, q.env.tenant) == before
            assert set(IMMUTABLE_TRIGGERS) <= _triggers(cur)
            with pytest.raises(psycopg2.Error):                                       # the immutability trigger still fires after the upgrade
                cur.execute("UPDATE v2_business_documents SET rule_count = rule_count + 1 WHERE tenant_id=%s", (q.env.tenant,))
            conn.rollback()
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.Error):
            cur.execute("DELETE FROM v2_business_rules WHERE tenant_id=%s", (q.env.tenant,))
        conn.rollback()
    st = q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()
    assert st["index"]["pending_events"] == 0 and st["documents"]["freshness"] == "available"                          # the upgraded database still serves


def _synthetic_rules(head, n):
    base = bind(KNOWLEDGE["observed_rules"], head.source_revision_id)
    out = []
    for i in range(n):
        tmpl = base[i % len(base)]
        cond = tmpl.get("condition")
        if isinstance(cond, dict) and "value" in cond and "op" in cond:
            cond = dict(cond, value=1000 * (i + 1))                                   # only a plain comparison gets a new value; all/any/not nodes stay as they are
        rule = dict(tmpl, rule_id=f"rule-synthetic-{i:04d}", title=f"{tmpl['title']} (variant {i}, department {i % 17}, threshold {1000 * (i + 1)})", condition=cond)
        out.append(rule)
    return out


def test_an_index_of_five_hundred_rules_builds_and_answers_a_context_query_within_budget(env, ws):  # noqa: F811
    rules = _synthetic_rules(ws, 500)
    commit_knowledge(env, ws, rules=rules, decisions=[], snapshot_id="snap-500", key="k500")
    t0 = time.perf_counter()
    built = v2_business_index.build_index(env.store, env.tenant, env.ws, "snap-500")
    build_s = time.perf_counter() - t0
    assert built["status"] == "READY" and built["item_count"] >= 500
    t1 = time.perf_counter()
    pack = v2_business_index.context_pack(env.store, env.tenant, env.ws, "manager approval threshold purchases department 3", semantic=False)
    query_s = time.perf_counter() - t1
    assert pack["items"] and pack["pin"]["snapshot_id"] == "snap-500"
    assert build_s < 30 and query_s < 5, (build_s, query_s)                          # task card budget; the measured values are recorded in the evidence
    print(f"\nREL02 index-500 build={build_s:.2f}s query={query_s:.2f}s items={built['item_count']}")
