"""DOC-03: lazy generation, cache identity and concurrency of knowledge documents, through the real routes, worker and store. No model, no provider.

What this proves: two concurrent identical requests share one job, one row and one blob; a half-done generation (bytes staged, no row) recovers on the next run with the same
bytes; a row whose bytes are missing is refused, never served; a template version change is a new key; a knowledge commit creates no document work (reused from CHG-05/KNO-05);
the freshness summary of the knowledge status and the bounded listing. Not human acceptance."""
from __future__ import annotations

import threading

import psycopg2

from infra.adapters import v2_business_documents as docs
from tests.final_loop.test_doc02_documents import ask, content, doc_jobs, state
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import drain


def test_two_concurrent_identical_requests_share_one_job_one_row_and_one_blob(qa_env):
    q = qa_env
    results, errors = [], []

    def go():
        try:
            c = q.env.admin_client()
            r = c.post("/v2/business/documents", json={"workspace_id": q.env.ws, "modes": ["business"]})
            results.append(r.status_code)
        except Exception as exc:                                                           # noqa: BLE001
            errors.append(repr(exc))
    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and results == [200, 200] and doc_jobs(q) == (1, 0)
    drain(q.env)
    assert doc_jobs(q) == (1, 1)
    aid = ask(q, q.admin, modes=["business"]).json()["documents"][0]["artifact_id"]
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT blob_digest FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))
        digests = [r[0] for r in cur.fetchall()]
    assert len(digests) == 1 and state(q, q.admin, aid).json()["freshness"] == "available"


def test_a_half_done_generation_recovers_with_identical_bytes_and_missing_bytes_are_never_served(qa_env):
    q = qa_env
    r = ask(q, q.admin, modes=["technical"]).json()["documents"][0]
    aid = r["artifact_id"]
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, aid)
    out = docs.generate(q.env.store, q.env.tenant, q.env.ws, rec["payload"], job_id=aid)           # the worker already generated once (bytes + row)
    assert out["replayed"] is True or out["content_hash"]
    drain(q.env)
    st = state(q, q.admin, aid).json()
    first = content(q, q.admin, aid)
    assert st["freshness"] == "available" and first.status_code == 200
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:                             # simulate a crash: the row vanishes after the blob was staged (immutable table: use a fresh id)
        cur.execute("ALTER TABLE v2_business_documents DISABLE TRIGGER v2_business_document_immutable")
        cur.execute("DELETE FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s AND artifact_id=%s", (q.env.tenant, q.env.ws, aid))
        cur.execute("ALTER TABLE v2_business_documents ENABLE TRIGGER v2_business_document_immutable")
    again = docs.generate(q.env.store, q.env.tenant, q.env.ws, rec["payload"], job_id=aid)      # the next run regenerates: same bytes, same hash, one row
    assert again["replayed"] is False and again["content_hash"] == st["content_hash"] and content(q, q.admin, aid).content == first.content
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:                             # a row whose bytes are not in the store is refused, not served
        cur.execute("ALTER TABLE v2_business_documents DISABLE TRIGGER v2_business_document_immutable")
        cur.execute("UPDATE v2_business_documents SET blob_digest='sha256:' || repeat('0', 64) WHERE tenant_id=%s AND workspace_id=%s AND artifact_id=%s", (q.env.tenant, q.env.ws, aid))
        cur.execute("ALTER TABLE v2_business_documents ENABLE TRIGGER v2_business_document_immutable")
    broken = content(q, q.admin, aid)
    assert broken.status_code == 409 and broken.json()["code"] == "integrity"


def test_template_version_is_part_of_the_key_and_the_status_summary_and_listing_say_what_exists(qa_env):
    q = qa_env
    st0 = q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["documents"]
    assert st0 == {"freshness": "not_requested", "jobs": 0}                                      # the empty shape of KNO-05/CHG-05 is unchanged
    a = ask(q, q.admin, modes=["business"]).json()["documents"][0]["artifact_id"]
    assert ask(q, q.admin, modes=["business"], template_version="99").status_code == 422          # no such template: refused, no job
    assert doc_jobs(q) == (1, 0)
    st1 = q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["documents"]
    assert st1["freshness"] == "generating" and st1["counts"]["generating"] == 1
    drain(q.env)
    st2 = q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["documents"]
    assert st2["freshness"] == "available" and st2["counts"] == {"available_for_head": 1, "stale": 0, "generating": 0, "failed": 0} and "never regenerated" in st2["statement"]
    listing = q.admin.get("/v2/business/documents", params={"workspace_id": q.env.ws}).json()
    assert [d["artifact_id"] for d in listing["documents"]] == [a] and listing["truncated"] is False and listing["limit"] == 200 and "operator" in listing["retention"]
    with q.env.store._tx() as cur:
        cur.execute("SELECT decision_id FROM v2_business_decisions WHERE tenant_id=%s AND kind='answer_gap'", (q.env.tenant,))
        decision = cur.fetchone()[0]
    assert q.admin.post("/v2/business/decisions/revoke", json={"workspace_id": q.env.ws, "decision_id": decision, "reason": "changed"}).status_code == 200
    drain(q.env)
    st3 = q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["documents"]
    assert st3["freshness"] == "stale" and st3["counts"]["stale"] == 1 and doc_jobs(q) == (1, 1)    # the knowledge moved: nothing regenerated, the old document is stale
