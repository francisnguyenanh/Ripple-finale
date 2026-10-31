"""REL-03 (2026-10-31): the current-candidate index. An extraction job's candidates are written to `v2_extraction_candidates` in the transaction that
completes the job; the readers (`current_candidates`, `extraction_candidate_index`) read the index by an index range scan and give exactly what the
JSONB scan gave; a database whose jobs predate the index is read from the JSONB until `backfill-candidates` ran, and the backfill reproduces the
same reading; the audit page query is an index range scan too. Real import, real worker-run stub extraction (no model), throwaway PostgreSQL."""
from __future__ import annotations

import json

from infra.adapters import v2_business
from infra.adapters.v2_postgres import _as_object
from tests.pitch_brief.chain import build


def _scan_current_candidates(store, tenant, ws) -> tuple:
    """The pre-index reading (the JSONB scan of every completed extraction job), kept here as the oracle the index must equal."""
    head = store.head(tenant, ws)
    with store._tx() as cur:
        cur.execute("SELECT source_revision_id, artifact_id, blob_hash FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s", (tenant, ws))
        blobs = {(r[0], r[1]): r[2] for r in cur.fetchall()}
        cur.execute("""SELECT j.execution_result FROM v2_jobs j WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind='extraction'
              AND j.execution_status='COMPLETED' AND j.execution_result IS NOT NULL
            ORDER BY (SELECT max(a.recorded_at) FROM v2_job_attempts a WHERE a.tenant_id=j.tenant_id AND a.workspace_id=j.workspace_id AND a.job_id=j.job_id)
                     ASC NULLS FIRST, j.job_id ASC""", (tenant, ws))
        results = [_as_object(r[0]) or {} for r in cur.fetchall()]
    current = {aid: h for (rev, aid), h in blobs.items() if rev == head.source_revision_id}
    per_file: dict = {}
    for result in results:
        by_file: dict = {}
        for cand in result.get("candidates") or []:
            if isinstance(cand, dict) and cand.get("candidate_id") and cand.get("artifact_revision_id") in current:
                by_file.setdefault((cand["artifact_revision_id"], cand.get("source_revision_id")), {})[cand["candidate_id"]] = cand
        for (aid, rev), cands in by_file.items():
            same = rev == head.source_revision_id or blobs.get((rev, aid)) == current[aid]
            if not same:
                continue
            prev = per_file.get(aid)
            if prev is None or not prev[0] or rev == head.source_revision_id:
                per_file[aid] = (rev == head.source_revision_id, rev, cands)
    out, carried = [], []
    for aid in sorted(per_file):
        is_current, rev, cands = per_file[aid]
        for cid in sorted(cands):
            out.append(cands[cid] if is_current else dict(cands[cid], source_revision_id=head.source_revision_id, carried_from_revision=rev))
        if not is_current:
            carried.append({"artifact_id": aid, "from_revision": rev})
    return out, carried


def _index_rows(store, tenant, ws):
    with store._tx() as cur:
        cur.execute("SELECT job_id, artifact_id, candidate_id FROM v2_extraction_candidates WHERE tenant_id=%s AND workspace_id=%s ORDER BY 1, 3", (tenant, ws))
        return cur.fetchall()


def test_the_index_is_written_with_the_job_and_reads_exactly_as_the_scan_and_by_an_index_range_scan(env, monkeypatch):
    ch = build(env, monkeypatch)                                                              # real import + worker-run stub extraction
    store, tenant, ws = env.store, env.tenant, env.ws
    expected = _scan_current_candidates(store, tenant, ws)
    rows = _index_rows(store, tenant, ws)
    assert rows and len(rows) == sum(len(cs) for cs in ch.cands.values())                     # one index row per persisted candidate
    assert v2_business.current_candidates(store, tenant, ws) == expected
    assert [c["candidate_id"] for c in expected[0]]                                            # the fixture has candidates on the current revision
    idx = store.extraction_candidate_index(tenant, ws)
    assert sorted((r[0], r[2]) for r in rows) == sorted((j["job_id"], c["candidate_id"]) for j in idx for c in j["candidates"])
    # the reads are index range scans, never a sequential scan of the candidate table or of v2_jobs' JSONB
    head = store.head(tenant, ws)
    current = sorted({r[1] for r in rows})
    with store._tx() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("""EXPLAIN (FORMAT JSON) SELECT c.job_id, c.candidate FROM v2_extraction_candidates c
            JOIN v2_jobs j ON j.tenant_id=c.tenant_id AND j.workspace_id=c.workspace_id AND j.job_id=c.job_id AND j.execution_status='COMPLETED'
            WHERE c.tenant_id=%s AND c.workspace_id=%s AND c.artifact_id = ANY(%s) ORDER BY c.completed_at ASC, c.job_id ASC, c.candidate_id ASC""", (tenant, ws, current))
        plan = json.dumps(cur.fetchone()[0])
        assert "v2_extraction_candidates_by_artifact" in plan or "v2_extraction_candidates_pkey" in plan, plan
        cur.execute("""EXPLAIN (FORMAT JSON) SELECT id FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND id < %s ORDER BY id DESC LIMIT 201""", (tenant, ws, 10**9))
        plan2 = json.dumps(cur.fetchone()[0])
        assert "Index" in plan2, plan2
        cur.execute("RESET enable_seqscan")
    assert head.source_revision_id


def test_jobs_that_predate_the_index_are_read_from_their_record_and_the_backfill_reproduces_the_same_reading(env, monkeypatch):
    build(env, monkeypatch)
    store, tenant, ws = env.store, env.tenant, env.ws
    before = v2_business.current_candidates(store, tenant, ws)
    idx_before = store.extraction_candidate_index(tenant, ws)
    with store._tx() as cur:                                                                  # test-only: simulate a database migrated with existing jobs
        cur.execute("DELETE FROM v2_extraction_candidates WHERE tenant_id=%s AND workspace_id=%s", (tenant, ws))
    assert _index_rows(store, tenant, ws) == []
    assert v2_business.current_candidates(store, tenant, ws) == before                        # the JSONB fallback gives the same reading
    assert store.extraction_candidate_index(tenant, ws) == idx_before
    done = store.backfill_candidate_index(tenant, ws)
    assert done["jobs"] >= 1 and done["candidates"] == sum(len(j["candidates"]) for j in idx_before)
    assert v2_business.current_candidates(store, tenant, ws) == before
    assert store.extraction_candidate_index(tenant, ws) == idx_before
    assert store.backfill_candidate_index(tenant, ws) == {"jobs": 0, "candidates": 0}            # idempotent
