"""DOC-05: exporting exactly the document being viewed (same bytes, same hash, generated safe name) and a bundle of the two modes of ONE knowledge version with a
manifest, through the real routes and the blob store. No model (documents are deterministic). What this proves: preview hash = downloaded hash; an export of an
older version stays that version after the knowledge moved; the latest export is explicit and generates for the new head; repeated exports are cache hits (no new
job); rights are rechecked (Technical refused without evidence rights); nothing is served for a missing/foreign artifact."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import psycopg2

from tests.final_loop.test_doc02_documents import ask, content, doc_jobs, state
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import drain


def download(q, client, aid):
    return client.get(f"/v2/business/documents/{aid}/download", params={"workspace_id": q.env.ws})


def export(q, client, **body):
    return client.post("/v2/business/documents/export", json={"workspace_id": q.env.ws, **body})


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_download_serves_the_viewed_bytes_with_a_generated_name_and_the_bundle_of_one_version_is_deterministic_and_cached(qa_env):
    q = qa_env
    out = ask(q, q.admin, modes=["business", "technical"]).json()
    drain(q.env)
    biz, tech = [d["artifact_id"] for d in out["documents"]]
    jobs_before = doc_jobs(q)
    shown = content(q, q.admin, biz)
    dl = download(q, q.admin, biz)
    assert dl.status_code == 200 and dl.content == shown.content and dl.headers["X-Content-Hash"] == shown.headers["X-Content-Hash"]
    name = dl.headers["Content-Disposition"]
    assert name.startswith('attachment; filename="knowledge-business-rev1-') and name.endswith('.md"') and "/" not in name and ".." not in name
    assert download(q, q.req, tech).status_code == 403 and download(q, q.admin, "doc-does-not-exist").status_code == 404
    r = export(q, q.admin, modes=["business", "technical"], snapshot_id=out["snapshot_id"])
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("application/zip") and r.headers["X-Snapshot-Id"] == out["snapshot_id"], r.text
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = sorted(z.namelist())
    assert "MANIFEST.json" in names and len(names) == 3 and all("/" not in n and ".." not in n for n in names)
    manifest = json.loads(z.read("MANIFEST.json"))
    assert manifest["snapshot_id"] == out["snapshot_id"] and manifest["business_revision"] == 1 and [d["mode"] for d in manifest["documents"]] == ["business", "technical"]
    for d in manifest["documents"]:
        body = z.read(d["file"])
        assert d["content_hash"] == state(q, q.admin, d["artifact_id"]).json()["content_hash"] and len(body) == d["size_bytes"]
        assert body == content(q, q.admin, d["artifact_id"]).content                                                     # exported bytes = viewed bytes
    again = export(q, q.admin, modes=["business", "technical"], snapshot_id=out["snapshot_id"])
    assert again.content == r.content and doc_jobs(q) == jobs_before                                                      # deterministic bundle; no job, no generation
    assert export(q, q.req, modes=["business", "technical"], snapshot_id=out["snapshot_id"]).status_code == 403            # the requester has no evidence rights: no technical bundle
    only_biz = export(q, q.req, modes=["business"], snapshot_id=out["snapshot_id"])
    assert only_biz.status_code == 200 and sorted(zipfile.ZipFile(io.BytesIO(only_biz.content)).namelist()) == sorted(["MANIFEST.json", manifest["documents"][0]["file"]])
    assert export(q, q.admin, modes=[]).status_code == 400 and export(q, q.admin, modes=["business"], snapshot_id="snap-unknown").status_code in (404, 409, 422)


def test_an_export_of_an_older_version_stays_that_version_and_the_latest_export_is_explicit(qa_env):
    q = qa_env
    out = ask(q, q.admin, modes=["business"]).json()
    drain(q.env)
    old_snapshot, biz = out["snapshot_id"], out["documents"][0]["artifact_id"]
    with q.env.store._tx() as cur:
        cur.execute("SELECT decision_id FROM v2_business_decisions WHERE tenant_id=%s AND kind='answer_gap'", (q.env.tenant,))
        decision = cur.fetchone()[0]
    assert q.admin.post("/v2/business/decisions/revoke", json={"workspace_id": q.env.ws, "decision_id": decision, "reason": "changed"}).status_code == 200   # the knowledge moves
    drain(q.env)
    assert state(q, q.admin, biz).json()["freshness"] == "stale"
    dl = download(q, q.admin, biz)
    assert dl.status_code == 200 and dl.headers["X-Freshness"] == "stale" and dl.headers["X-Snapshot-Id"] == old_snapshot                          # the viewed version, labelled
    old = export(q, q.admin, modes=["business"], snapshot_id=old_snapshot)
    assert old.status_code == 200 and old.headers["X-Snapshot-Id"] == old_snapshot
    assert json.loads(zipfile.ZipFile(io.BytesIO(old.content)).read("MANIFEST.json"))["business_revision"] == 1
    jobs = doc_jobs(q)
    latest = export(q, q.admin, modes=["business"])                                                                       # explicit latest: generates for the new head
    assert latest.status_code == 409 and latest.json()["code"] == "generating" and doc_jobs(q)[0] == jobs[0] + 1
    drain(q.env)
    latest = export(q, q.admin, modes=["business"])
    assert latest.status_code == 200 and latest.headers["X-Snapshot-Id"] != old_snapshot
    m2 = json.loads(zipfile.ZipFile(io.BytesIO(latest.content)).read("MANIFEST.json"))
    assert m2["business_revision"] == 2 and m2["documents"][0]["content_hash"] != json.loads(zipfile.ZipFile(io.BytesIO(old.content)).read("MANIFEST.json"))["documents"][0]["content_hash"]
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))
        assert cur.fetchone()[0] == 2                                                                                      # one per version; the old one is kept
