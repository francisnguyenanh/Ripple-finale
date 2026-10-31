"""DOC-03 retention (2026-10-31): the operator tool retires the bytes of documents whose knowledge version is older than the newest N accepted
versions. The document row stays (hash, snapshot, listing), one audit event per document is written, the content route answers 410 `retired`
(not 409 `integrity`), a dry run changes nothing, a digest still referenced by an unretired document keeps its bytes, and a new request yields the
current version's document, never the retired bytes (documents are generated on the current knowledge only). Real routes, worker and store on the throwaway PostgreSQL."""
from __future__ import annotations

import psycopg2

from tests.final_loop.test_doc02_documents import ask, content, doc_jobs, state
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.pitch_brief.conftest import drain
from tools import documents_retention as ret


class _DeletingBlobs:
    """The test's in-memory blob store with the operator's delete (the GCS and file adapters implement the same method)."""
    def __init__(self, inner):
        self.inner, self.deleted = inner, []

    def delete_immutable(self, digest):
        self.deleted.append(digest)
        return self.inner.data.pop(digest, None) is not None


def _audit(q, artifact_id):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT actor_id, payload FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='DOCUMENT_RETIRED' AND payload->>'artifact_id'=%s",
                    (q.env.tenant, q.env.ws, artifact_id))
        return cur.fetchall()


def test_retention_retires_bytes_of_older_versions_keeps_rows_and_audits(qa_env, monkeypatch):
    q = qa_env
    old = ask(q, q.admin, modes=["business"]).json()["documents"][0]["artifact_id"]
    drain(q.env)
    with q.env.store._tx() as cur:
        cur.execute("SELECT decision_id FROM v2_business_decisions WHERE tenant_id=%s AND kind='answer_gap'", (q.env.tenant,))
        decision = cur.fetchone()[0]
    assert q.admin.post("/v2/business/decisions/revoke", json={"workspace_id": q.env.ws, "decision_id": decision, "reason": "changed"}).status_code == 200
    drain(q.env)
    new = ask(q, q.admin, modes=["business"]).json()["documents"][0]["artifact_id"]
    drain(q.env)
    assert state(q, q.admin, old).json()["freshness"] == "stale" and state(q, q.admin, new).json()["freshness"] == "available"
    blobs = _DeletingBlobs(q.env.blobs)
    monkeypatch.setattr(ret, "_blob_store", lambda: blobs)
    conn = psycopg2.connect(q.env.dsn)
    try:
        # keeping two versions retires nothing; keeping one names only the older document
        assert ret.candidates(conn, q.env.tenant, q.env.ws, 2) == []
        cands = ret.candidates(conn, q.env.tenant, q.env.ws, 1)
        assert [c["artifact_id"] for c in cands] == [old]
        # dry run: nothing deleted, nothing audited, the bytes still served
        dry = ret.retire(conn, q.env.tenant, q.env.ws, cands, actor_id="operator", reason="retention test dry", apply=False)
        assert dry["applied"] is False and dry["would_retire"] == 1 and blobs.deleted == [] and _audit(q, old) == []
        assert content(q, q.admin, old).status_code == 200
        # apply: bytes deleted, one audit row with the reason, the row and its listing kept, the content route says retired (410), not integrity
        done = ret.retire(conn, q.env.tenant, q.env.ws, cands, actor_id="operator", reason="retention test apply", apply=True)
        assert done["applied"] is True and done["retired"] == 1 and done["bytes_deleted"] == [old] and len(blobs.deleted) == 1
        audit = _audit(q, old)
        assert len(audit) == 1 and audit[0][0] == "operator" and audit[0][1]["reason"] == "retention test apply" and audit[0][1]["bytes_deleted"] is True
        st = state(q, q.admin, old)
        assert st.status_code == 200 and st.json()["freshness"] == "stale" and st.json()["content_hash"] == cands[0]["content_hash"]
        c = content(q, q.admin, old)
        assert c.status_code == 410 and c.json()["code"] == "retired" and c.json()["retired_at"]
        assert content(q, q.admin, new).status_code == 200                                        # the kept version is untouched
        assert ret.candidates(conn, q.env.tenant, q.env.ws, 1) == []                               # a retired document is not listed again
        # bytes still referenced elsewhere in the workspace are never deleted: a source blob with the same digest keeps the document's bytes
        with q.env.store._tx() as cur:
            cur.execute("SELECT blob_digest FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s AND artifact_id=%s", (q.env.tenant, q.env.ws, new))
            new_digest = cur.fetchone()[0]
            cur.execute("INSERT INTO v2_blob_refs (tenant_id, workspace_id, blob_hash) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (q.env.tenant, q.env.ws, new_digest))
        guarded = ret.retire(conn, q.env.tenant, q.env.ws, [c for c in ret.candidates(conn, q.env.tenant, q.env.ws, 0) if c["artifact_id"] == new],
                             actor_id="operator", reason="retention test shared bytes", apply=True)
        assert guarded["bytes_kept_shared"] == [new] and guarded["bytes_deleted"] == [] and content(q, q.admin, new).status_code == 410   # row retired, bytes kept
        assert new_digest in q.env.blobs.data
        # a viewer without the mode's permission is still refused before the retired answer
        if hasattr(q, "viewer"):
            assert content(q, q.viewer, old).status_code in (401, 403)
        # a new request finds the current version's document (same artifact key); the retired row is never rewritten and its bytes are not recreated
        again = ask(q, q.admin, modes=["business"]).json()["documents"][0]
        drain(q.env)
        assert again["artifact_id"] == new and doc_jobs(q)[1] == 2 and content(q, q.admin, old).status_code == 410
    finally:
        conn.close()
