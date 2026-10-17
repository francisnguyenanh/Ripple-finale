"""Generated Business/Technical documents of an accepted business snapshot (contract fkl/1 sections 2.5, 6, 11; DOC-02).

A document is produced only on an explicit request (view or export): the request pins one snapshot, one mode, a scope, a locale and a template version, and is turned into a
deterministic job (`document.generate`, zero provider calls) whose id is the artifact key. The worker builds the AST from the stored snapshot (`document_ast.build_document`),
renders the canonical Markdown, validates the artifact limits, stages the bytes as an immutable content-addressed blob and writes one row. Both modes requested together use the
same snapshot id. Reading rechecks the caller's rights every time (Business: `project.read`; Technical: `evidence.read`); the redaction scope is part of the key, so a Technical
document without evidence rights is a different artifact than one with them. Freshness is derived on read: `generating`, `failed`, `available`, `stale` (snapshot is no longer
the accepted head) or `not_requested`. Nothing here changes a source, re-imports a document or calls a model.
"""
from __future__ import annotations

import pathlib

from psycopg2.extras import Json

from core.change_intelligence.v2 import business_knowledge as bk
from core.change_intelligence.v2 import document_ast as da
from infra.adapters import v2_business
from infra.adapters.v2_business import BusinessKnowledgeRefused, _as_object, _snapshot_view, read_snapshot
from infra.adapters.v2_postgres import TenantScopeError

KIND = "document.generate"
GENERATOR_VERSION = "fkl-docgen/1"
TEMPLATE_DIR = pathlib.Path(__file__).resolve().parents[2] / "config" / "document_templates"
DEFAULT_LOCALE = "en"
DEFAULT_TEMPLATE_VERSION = "1"


def template_for(mode: str, version: str) -> dict:
    path = TEMPLATE_DIR / f"{mode}.v{version}.json"
    if not path.is_file():
        raise bk.ContractError("invalid_field", f"no template {mode!r} version {version!r}")
    return da.template_from_text(path.read_text(encoding="utf-8"))


def redaction_scope(mode: str, can_read_evidence: bool) -> str:
    return "evidence" if (mode == "technical" and can_read_evidence) else "business"


def artifact_id_for(tenant_id, workspace_id, *, snapshot_id, mode, scope, locale, template_version, redaction) -> str:
    return "doc-" + bk.artifact_key(tenant_id=tenant_id, workspace_id=workspace_id, snapshot_id=snapshot_id, scope=scope, mode=mode, locale=locale, template_version=template_version,
                                    generator_version=GENERATOR_VERSION, schema_version=da.SCHEMA, redaction_scope=redaction)[:40]


def required_permission(mode: str) -> str:
    return "evidence.read" if mode == "technical" else "project.read"


def normalize_request(body: dict, *, default_snapshot_id: str | None) -> dict:
    """The typed request (contract 2.5), validated; unknown keys refused."""
    scope = body.get("scope") if body.get("scope") is not None else {}
    if not isinstance(scope, dict) or any(k not in ("concept_ids", "rule_ids") for k in scope) or any(not isinstance(v, list) or not all(isinstance(x, str) and x for x in v) for v in scope.values()):
        raise bk.ContractError("invalid_field", "scope may hold concept_ids and rule_ids lists only")
    req = {"snapshot_id": body.get("snapshot_id") or default_snapshot_id, "mode": body.get("mode"), "scope": {k: sorted(set(v)) for k, v in scope.items()},
           "locale": body.get("locale") or DEFAULT_LOCALE, "template_version": str(body.get("template_version") or DEFAULT_TEMPLATE_VERSION)}
    if not isinstance(req["snapshot_id"], str) or not req["snapshot_id"]:
        raise bk.ContractError("invalid_field", "snapshot_id required (no accepted knowledge to document)")
    bk.validate_document_request(req)
    template_for(req["mode"], req["template_version"])
    return req


def snapshot_for(store, tenant_id, workspace_id, snapshot_id):
    with read_snapshot(store) as cur:
        snap = _snapshot_view(cur, tenant_id, workspace_id, snapshot_id)
    if snap is None:
        raise TenantScopeError("snapshot not found")
    return snap


def request_document(store, tenant_id, workspace_id, *, req: dict, can_read_evidence: bool, requested_by: str) -> dict:
    """Create (or find) the generation job for this exact request. Idempotent per artifact id; no bytes are written here."""
    from infra.worker.verification import ensure_job
    snap = snapshot_for(store, tenant_id, workspace_id, req["snapshot_id"])
    if snap["snapshot"]["lifecycle_state"] != "ACCEPTED":
        raise BusinessKnowledgeRefused("not_accepted", "documents are generated for accepted business knowledge only")
    redaction = redaction_scope(req["mode"], can_read_evidence)
    aid = artifact_id_for(tenant_id, workspace_id, snapshot_id=req["snapshot_id"], mode=req["mode"], scope=req["scope"], locale=req["locale"], template_version=req["template_version"], redaction=redaction)
    row = _row(store, tenant_id, workspace_id, aid)
    if row is None:
        payload = {**req, "artifact_id": aid, "redaction_scope": redaction, "generator_version": GENERATOR_VERSION, "schema_version": da.SCHEMA, "requested_by": requested_by}
        ensure_job(store, tenant_id, workspace_id, aid, KIND, payload)
    return document_state(store, tenant_id, workspace_id, aid)


def _row(store, tenant_id, workspace_id, artifact_id):
    with read_snapshot(store) as cur:
        cur.execute("""SELECT snapshot_id, mode, scope, locale, template_version, generator_version, schema_version, redaction_scope, content_hash, blob_digest, rule_count, size_bytes,
                job_id, generated_by, created_at FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s AND artifact_id=%s""", (tenant_id, workspace_id, artifact_id))
        r = cur.fetchone()
    if r is None:
        return None
    return {"artifact_id": artifact_id, "snapshot_id": r[0], "mode": r[1], "scope": _as_object(r[2]) or {}, "locale": r[3], "template_version": r[4], "generator_version": r[5],
            "schema_version": r[6], "redaction_scope": r[7], "content_hash": r[8], "blob_digest": r[9], "rule_count": r[10], "size_bytes": r[11], "job_id": r[12], "generated_by": r[13],
            "created_at": r[14].isoformat()}


def _freshness(store, tenant_id, workspace_id, row, job) -> tuple[str, str | None]:
    if row is not None:
        head = v2_business.business_view(store, tenant_id, workspace_id)["business_head"]
        return ("available" if head["accepted_snapshot_id"] == row["snapshot_id"] else "stale"), None
    if job is None:
        return "not_requested", None
    st = job.get("execution_status")
    if st == "FAILED":
        return "failed", job.get("execution_reason")
    if st == "CANCELLED":
        return "failed", "cancelled"
    return "generating", None


def document_state(store, tenant_id, workspace_id, artifact_id) -> dict:
    """Metadata and freshness of one artifact (never the bytes). Raises TenantScopeError when nothing of this id exists in the workspace."""
    row = _row(store, tenant_id, workspace_id, artifact_id)
    try:
        job = store.job_record(tenant_id, workspace_id, artifact_id)
    except TenantScopeError:
        job = None
    if row is None and (job is None or job.get("kind") != KIND):
        raise TenantScopeError("document not found")
    payload = (job or {}).get("payload") or {}
    freshness, reason = _freshness(store, tenant_id, workspace_id, row, job)
    meta = row or {"artifact_id": artifact_id, "snapshot_id": payload.get("snapshot_id"), "mode": payload.get("mode"), "scope": payload.get("scope") or {}, "locale": payload.get("locale"),
                   "template_version": payload.get("template_version"), "redaction_scope": payload.get("redaction_scope"), "content_hash": None, "rule_count": None, "size_bytes": None}
    out = {k: meta.get(k) for k in ("artifact_id", "snapshot_id", "mode", "scope", "locale", "template_version", "redaction_scope", "content_hash", "rule_count", "size_bytes")}
    out.update(freshness=freshness, failure_reason=reason, generator_version=GENERATOR_VERSION, schema_version=da.SCHEMA, generated_at=(row or {}).get("created_at"),
               statement="A generated document is a view of one knowledge version; it is not evidence and is never read back as a source.")
    return out


def read_content(store, tenant_id, workspace_id, artifact_id) -> tuple[bytes, dict]:
    """The immutable Markdown bytes and their metadata; the digest is re-verified by the blob store on read."""
    row = _row(store, tenant_id, workspace_id, artifact_id)
    if row is None:
        raise TenantScopeError("document not available")
    content = store._verified_blob(row["blob_digest"])
    if da.content_hash(content.decode("utf-8")) != row["content_hash"]:
        raise BusinessKnowledgeRefused("integrity", "the stored document does not match its recorded hash")
    return content, row


LIST_LIMIT = 200
RETENTION_POLICY = ("Generated documents are immutable per knowledge version and are kept; nothing deletes them automatically in Final. Removal by knowledge version age is an "
                    "operator action (runbook), never a job. A revoked right stops serving a document; it does not delete it.")


def list_documents(store, tenant_id, workspace_id, *, snapshot_id: str | None = None) -> dict:
    """-> {documents, truncated, limit, retention}. Newest first; the listing is bounded and says when it is cut (never a silent partial list)."""
    with read_snapshot(store) as cur:
        if snapshot_id:
            cur.execute("""SELECT artifact_id FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s ORDER BY mode, artifact_id LIMIT %s""",
                        (tenant_id, workspace_id, snapshot_id, LIST_LIMIT + 1))
        else:
            cur.execute("""SELECT artifact_id FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s ORDER BY created_at DESC, artifact_id LIMIT %s""",
                        (tenant_id, workspace_id, LIST_LIMIT + 1))
        ids = [r[0] for r in cur.fetchall()]
    truncated = len(ids) > LIST_LIMIT
    return {"documents": [document_state(store, tenant_id, workspace_id, aid) for aid in ids[:LIST_LIMIT]], "truncated": truncated, "limit": LIST_LIMIT, "retention": RETENTION_POLICY}


def generate(store, tenant_id, workspace_id, payload: dict, *, job_id: str) -> dict:
    """The worker's work: AST -> canonical Markdown -> validated artifact -> blob + row, all from stored rows. Idempotent (an existing row is returned)."""
    existing = _row(store, tenant_id, workspace_id, payload["artifact_id"])
    if existing is not None:
        return {**existing, "replayed": True}
    snap = snapshot_for(store, tenant_id, workspace_id, payload["snapshot_id"])
    can_evidence = payload["redaction_scope"] == "evidence"
    doc = da.build_document(snap, snap["decisions"], mode=payload["mode"], scope=payload["scope"], can_read_evidence=can_evidence, locale=payload["locale"],
                            template_version=payload["template_version"])
    text = da.render_markdown(doc, template_for(payload["mode"], payload["template_version"]))
    content = text.encode("utf-8")
    chash = da.content_hash(text)
    art = {"artifact_id": payload["artifact_id"], "snapshot_id": payload["snapshot_id"], "mode": payload["mode"], "content_hash": chash, "template_version": payload["template_version"],
           "freshness": "available", "rule_count": len(doc["facts"]), "size_bytes": len(content)}
    bk.validate_document_artifact(art)
    digest = store.stage_blob(content)
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_business_documents (tenant_id, workspace_id, artifact_id, snapshot_id, mode, scope, locale, template_version, generator_version, schema_version,
                redaction_scope, content_hash, blob_digest, rule_count, size_bytes, job_id, generated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id, workspace_id, artifact_id) DO NOTHING""",
                    (tenant_id, workspace_id, payload["artifact_id"], payload["snapshot_id"], payload["mode"], Json(payload["scope"]), payload["locale"], payload["template_version"],
                     GENERATOR_VERSION, da.SCHEMA, payload["redaction_scope"], chash, digest, art["rule_count"], art["size_bytes"], job_id, payload.get("requested_by") or "system"))
    return {**art, "blob_digest": digest, "replayed": False}

# ---- export (DOC-05): the bytes a person is looking at, as a file or as a bundle of the two modes of ONE snapshot ------------------------------------------------

def download_name(row: dict, business_revision) -> str:
    """A generated, safe file name: mode, knowledge version, scope marker and the first 12 hex characters of the content hash. Never a caller-supplied name."""
    scope = "scoped-" if (row.get("scope") or {}) else ""
    return f"knowledge-{row['mode']}-rev{int(business_revision)}-{scope}{row['content_hash'][:12]}.md"


def bundle(store, tenant_id, workspace_id, artifact_ids: list) -> tuple[bytes, dict]:
    """A deterministic zip of the given AVAILABLE documents (all of one snapshot) plus MANIFEST.json (snapshot, business revision, per-file mode/scope/hash/size/name).
    Refuses when the documents belong to different snapshots or one of them is not generated. Bytes are the stored immutable bytes; nothing is regenerated."""
    import io
    import json
    import zipfile
    rows = []
    for aid in artifact_ids:
        content, row = read_content(store, tenant_id, workspace_id, aid)
        rows.append((content, row))
    snapshots = {r["snapshot_id"] for _, r in rows}
    if len(snapshots) != 1:
        raise BusinessKnowledgeRefused("mixed_snapshots", "a bundle holds the documents of exactly one knowledge version")
    snap = snapshot_for(store, tenant_id, workspace_id, next(iter(snapshots)))["snapshot"]
    manifest = {"schema": "ripple-knowledge-bundle/1", "snapshot_id": snap["snapshot_id"], "business_revision": snap["business_revision"], "lifecycle_state": snap["lifecycle_state"],
                "documents": [], "statement": "Generated documents are views of one accepted knowledge version; they are not evidence and are never read back as a source."}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        names = set()
        for content, row in sorted(rows, key=lambda cr: (cr[1]["mode"], cr[1]["artifact_id"])):
            name = download_name(row, snap["business_revision"])
            if name in names:
                raise BusinessKnowledgeRefused("duplicate_name", "two documents would share a file name")
            names.add(name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content)
            manifest["documents"].append({"file": name, "artifact_id": row["artifact_id"], "mode": row["mode"], "scope": row["scope"], "content_hash": row["content_hash"], "size_bytes": len(content),
                                          "template_version": row["template_version"], "redaction_scope": row["redaction_scope"]})
        info = zipfile.ZipInfo("MANIFEST.json", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False))
    return buf.getvalue(), manifest

