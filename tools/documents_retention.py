"""Retention of generated business documents by knowledge-version age (DOC-03, 2026-10-31): the operator runbook action the contract names.

    python -m tools.documents_retention list   --tenant T --workspace W --keep-versions N        # documents of accepted versions older than the newest N
    python -m tools.documents_retention retire --tenant T --workspace W --keep-versions N --reason "<why>" [--apply]

A document row is immutable (its hash, snapshot, template and generator versions stay as the record of what was generated); what retention removes
is the stored bytes of documents whose knowledge version is older than the newest N accepted versions. `retire --apply` deletes those blobs through
the blob store and writes one audit event `DOCUMENT_RETIRED` {artifact_id, snapshot_id, business_revision, blob_digest, content_hash, reason} per
document; bytes that any other row of the workspace still references (another unretired document, a document variant, a source blob in `v2_blob_refs`)
are kept and reported as `bytes_kept_shared`. From then on the document's content route answers 410 `retired` (not 409 `integrity`), its listing keeps the row with `freshness` as
recorded, and the bytes are not recreated (documents are generated on the current knowledge only; the kept hash verifies any copy). Nothing deletes
automatically; the policy text stays in `RETENTION_POLICY`. RIPPLE_V2_DSN names the database; RIPPLE_V2_BLOB_BUCKET or RIPPLE_V2_BLOB_DIR the store.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from psycopg2.extras import Json

AUDIT_EVENT = "DOCUMENT_RETIRED"


def _connect(dsn):
    import psycopg2
    return psycopg2.connect(dsn)


def _blob_store():
    bucket = os.environ.get("RIPPLE_V2_BLOB_BUCKET")
    if bucket:
        from google.cloud import storage
        from infra.adapters.v2_blobs import ImmutableGCSBlobs
        return ImmutableGCSBlobs(storage.Client().bucket(bucket))
    root = os.environ.get("RIPPLE_V2_BLOB_DIR")
    if root:
        from infra.adapters.v2_blobs_fs import FileBlobs
        return FileBlobs(root)
    return None


def candidates(conn, tenant, ws, keep_versions: int) -> list[dict]:
    """Documents whose snapshot's business_revision is older than the newest `keep_versions` accepted revisions, not yet retired."""
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT business_revision FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND lifecycle_state='ACCEPTED'
                       AND business_revision IS NOT NULL ORDER BY business_revision DESC""", (tenant, ws))
        revisions = [r[0] for r in cur.fetchall()]
        kept = set(revisions[:max(0, int(keep_versions))])
        cur.execute("""SELECT d.artifact_id, d.snapshot_id, s.business_revision, d.blob_digest, d.content_hash, d.mode, d.created_at
                       FROM v2_business_documents d JOIN v2_business_snapshots s ON s.tenant_id=d.tenant_id AND s.workspace_id=d.workspace_id AND s.snapshot_id=d.snapshot_id
                       WHERE d.tenant_id=%s AND d.workspace_id=%s
                         AND NOT EXISTS (SELECT 1 FROM v2_audit a WHERE a.tenant_id=d.tenant_id AND a.workspace_id=d.workspace_id AND a.event_type=%s
                                         AND a.payload->>'artifact_id'=d.artifact_id)
                       ORDER BY s.business_revision, d.artifact_id""", (tenant, ws, AUDIT_EVENT))
        rows = cur.fetchall()
    return [{"artifact_id": r[0], "snapshot_id": r[1], "business_revision": r[2], "blob_digest": r[3], "content_hash": r[4], "mode": r[5], "created_at": r[6].isoformat()}
            for r in rows if r[2] is not None and r[2] not in kept]


def retire(conn, tenant, ws, docs: list[dict], *, actor_id: str, reason: str, apply: bool) -> dict:
    """Delete the bytes of each document (shared digests are deleted only when no remaining, unretired document references them) and audit each one."""
    done, kept_shared = [], []
    store = _blob_store() if apply else None
    if apply and store is None:
        raise RuntimeError("no blob store configured (RIPPLE_V2_BLOB_BUCKET or RIPPLE_V2_BLOB_DIR)")
    with conn.cursor() as cur:
        for d in docs:
            cur.execute("""SELECT count(*) FROM v2_business_documents x WHERE x.tenant_id=%s AND x.workspace_id=%s AND x.blob_digest=%s AND x.artifact_id<>%s
                           AND NOT EXISTS (SELECT 1 FROM v2_audit a WHERE a.tenant_id=x.tenant_id AND a.workspace_id=x.workspace_id AND a.event_type=%s AND a.payload->>'artifact_id'=x.artifact_id)""",
                        (tenant, ws, d["blob_digest"], d["artifact_id"], AUDIT_EVENT))
            shared = cur.fetchone()[0] > 0
            if not shared:                                                                  # bytes referenced by anything else in the workspace stay (a source blob or
                cur.execute("""SELECT (SELECT count(*) FROM v2_blob_refs b WHERE b.tenant_id=%s AND b.workspace_id=%s AND b.blob_hash=%s)
                                    + (SELECT count(*) FROM v2_document_variants v WHERE v.tenant_id=%s AND v.workspace_id=%s AND v.blob_digest=%s)""",
                            (tenant, ws, d["blob_digest"], tenant, ws, d["blob_digest"]))     # a document variant with byte-identical content)
                shared = cur.fetchone()[0] > 0
            if apply:
                if not shared:
                    store.delete_immutable(d["blob_digest"])
                cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)",
                            (tenant, ws, AUDIT_EVENT, actor_id, Json({**{k: d[k] for k in ("artifact_id", "snapshot_id", "business_revision", "blob_digest", "content_hash")}, "reason": reason, "bytes_deleted": not shared})))
            (kept_shared if shared else done).append(d["artifact_id"])
        conn.commit() if apply else conn.rollback()
    return {"retired": len(docs) if apply else 0, "would_retire": 0 if apply else len(docs), "bytes_deleted": done, "bytes_kept_shared": kept_shared, "applied": apply}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Retention of generated documents by knowledge-version age (DOC-03).")
    ap.add_argument("command", choices=("list", "retire"))
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--keep-versions", type=int, default=3)
    ap.add_argument("--reason", default="")
    ap.add_argument("--actor", default="operator")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    if not dsn:
        print("RIPPLE_V2_DSN is not set", file=sys.stderr)
        return 2
    conn = _connect(dsn)
    try:
        docs = candidates(conn, a.tenant, a.workspace, a.keep_versions)
        if a.command == "list":
            print(json.dumps({"keep_versions": a.keep_versions, "candidates": docs}, indent=1)); return 0
        if len(a.reason.strip()) < 8:
            print("--reason (at least 8 characters) is required: it is written to the audit", file=sys.stderr); return 2
        print(json.dumps(retire(conn, a.tenant, a.workspace, docs, actor_id=a.actor, reason=a.reason.strip(), apply=a.apply), indent=1)); return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
