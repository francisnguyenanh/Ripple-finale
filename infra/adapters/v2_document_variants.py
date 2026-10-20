"""Audience variants of one generated document (extension AIX-D1): request (one provider-backed job per variant key), state, bytes, list.

A variant is requested only for a base document that is AVAILABLE (its bytes exist); the base artifact's permission and redaction scope are rechecked on every call (the audience
never widens what may be read). The variant key = (artifact, audience, locale, frozen config hash, pinned model): the same request again returns the stored variant and makes no
model call; a changed knowledge version makes the base and its variants `stale` on read (no eager regeneration).
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from psycopg2.extras import Json

from core.change_intelligence.v2 import document_ast as da, document_variant as dv
from infra.adapters import v2_business, v2_business_documents as docs
from infra.adapters.v2_business import BusinessKnowledgeRefused
from infra.adapters.v2_business import read_snapshot
from infra.adapters.v2_postgres import TenantScopeError, _as_object

KIND = "document.variant"
ROLE = "document.wording"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "document_wording.json"


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def pinned_model() -> str:
    from infra.worker import job_quota
    from infra.worker.budget_config import model_for_role
    config = job_quota.load_worker_budget_config()
    if ROLE not in (getattr(config, "role_model_ids", None) or {}):
        raise BusinessKnowledgeRefused("role_model_not_pinned", f"the budget config pins no model for {ROLE}")
    return model_for_role(config, ROLE)


def _row(store, tenant_id, workspace_id, variant_id):
    with read_snapshot(store) as cur:
        cur.execute("""SELECT artifact_id, snapshot_id, audience, locale, config_sha256, model_pinned, model_served, status, content_hash, blob_digest, fact_coverage, rejected, size_bytes,
                job_id, generated_by, created_at FROM v2_document_variants WHERE tenant_id=%s AND workspace_id=%s AND variant_id=%s""", (tenant_id, workspace_id, variant_id))
        r = cur.fetchone()
    if r is None:
        return None
    return {"variant_id": variant_id, "artifact_id": r[0], "snapshot_id": r[1], "audience": r[2], "locale": r[3], "config_sha256": r[4], "model_pinned": r[5], "model_served": r[6],
            "status": r[7], "content_hash": r[8], "blob_digest": r[9], "fact_coverage": _as_object(r[10]), "rejected": _as_object(r[11]) or [], "size_bytes": r[12], "job_id": r[13],
            "generated_by": r[14], "created_at": r[15].isoformat()}


def request_variant(store, tenant_id, workspace_id, *, artifact_id: str, audience: str, locale: str | None, requested_by: str, limit: int, reservation, attempt: int = 1) -> dict:
    """Create (or find) the variant job of one available base document. The caller has already checked the base permissions and `ai.use`."""
    if audience not in dv.AUDIENCES:
        raise BusinessKnowledgeRefused("invalid_audience", f"audience must be one of {', '.join(dv.AUDIENCES)}")
    base = docs._row(store, tenant_id, workspace_id, artifact_id)
    if base is None:
        state = docs.document_state(store, tenant_id, workspace_id, artifact_id)            # 404 when nothing of this id exists
        raise BusinessKnowledgeRefused("base_not_available", f"the base document is {state['freshness']}; request the variant once its bytes exist")
    frozen, sha = load_config()
    model_id = pinned_model()
    loc = locale or base["locale"]
    vid = dv.variant_id_for(artifact_id, audience, loc, sha, model_id, attempt=attempt)
    existing = _row(store, tenant_id, workspace_id, vid)
    if existing is None:
        try:
            store.job_record(tenant_id, workspace_id, vid)
        except TenantScopeError:
            head = store.head(tenant_id, workspace_id)
            payload = {"variant_id": vid, "artifact_id": artifact_id, "audience": audience, "locale": loc, "snapshot_id": base["snapshot_id"], "config_sha256": sha, "model_pinned": model_id,
                       "requested_by": requested_by}
            store.create_job(tenant_id, workspace_id, vid, KIND, payload, expected_version=head.version, idempotency_key=f"job-create-{vid}", max_concurrent_jobs=limit,
                             budget_reservation=reservation, requested_by=requested_by)
    return variant_state(store, tenant_id, workspace_id, vid)


def variant_state(store, tenant_id, workspace_id, variant_id) -> dict:
    row = _row(store, tenant_id, workspace_id, variant_id)
    try:
        job = store.job_record(tenant_id, workspace_id, variant_id)
    except TenantScopeError:
        job = None
    if row is None and (job is None or job.get("kind") != KIND):
        raise TenantScopeError("variant not found")
    payload = (job or {}).get("payload") or {}
    freshness, reason = docs._freshness(store, tenant_id, workspace_id, row, job)
    meta = row or {"variant_id": variant_id, "artifact_id": payload.get("artifact_id"), "snapshot_id": payload.get("snapshot_id"), "audience": payload.get("audience"), "locale": payload.get("locale"),
                   "config_sha256": payload.get("config_sha256"), "model_pinned": payload.get("model_pinned"), "model_served": None, "status": None, "content_hash": None, "fact_coverage": None,
                   "rejected": [], "size_bytes": None, "created_at": None}
    out = {k: meta.get(k) for k in ("variant_id", "artifact_id", "snapshot_id", "audience", "locale", "config_sha256", "model_pinned", "model_served", "status", "content_hash", "fact_coverage",
                                     "rejected", "size_bytes")}
    out.update(schema=dv.SCHEMA, freshness=freshness, failure_reason=reason, generated_at=meta.get("created_at"), role=ROLE,
               statement="An audience variant is a model's wording of one knowledge version, checked by the server against the facts; the canonical facts, unknowns and limits are "
                         "rendered by the server. It is not evidence and is never read back as a source; a fallback carries no model wording.")
    return out


def read_content(store, tenant_id, workspace_id, variant_id) -> tuple[bytes, dict]:
    row = _row(store, tenant_id, workspace_id, variant_id)
    if row is None:
        raise TenantScopeError("variant not available")
    content = store._verified_blob(row["blob_digest"])
    if da.content_hash(content.decode("utf-8")) != row["content_hash"]:
        raise BusinessKnowledgeRefused("integrity", "the stored variant does not match its recorded hash")
    return content, row


def list_variants(store, tenant_id, workspace_id, artifact_id) -> list:
    with read_snapshot(store) as cur:
        cur.execute("SELECT variant_id FROM v2_document_variants WHERE tenant_id=%s AND workspace_id=%s AND artifact_id=%s ORDER BY audience, created_at", (tenant_id, workspace_id, artifact_id))
        ids = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind=%s AND payload->>'artifact_id'=%s ORDER BY job_id", (tenant_id, workspace_id, KIND, artifact_id))
        ids += [r[0] for r in cur.fetchall() if r[0] not in ids]
    return [variant_state(store, tenant_id, workspace_id, v) for v in ids]


def generate(store, tenant_id, workspace_id, payload: dict, *, job_id: str, checked: dict, markdown: str, model_served: str | None) -> dict:
    """The worker's write: validated Markdown -> blob + row. Idempotent (an existing row is returned)."""
    existing = _row(store, tenant_id, workspace_id, payload["variant_id"])
    if existing is not None:
        return {**existing, "replayed": True}
    content = markdown.encode("utf-8")
    chash = da.content_hash(markdown)
    digest = store.stage_blob(content)
    status = "fallback" if checked["fallback"] else "generated"
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_document_variants (tenant_id, workspace_id, variant_id, artifact_id, snapshot_id, audience, locale, config_sha256, model_pinned, model_served, status,
                content_hash, blob_digest, fact_coverage, rejected, size_bytes, job_id, generated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id, workspace_id, variant_id) DO NOTHING""",
                    (tenant_id, workspace_id, payload["variant_id"], payload["artifact_id"], payload["snapshot_id"], payload["audience"], payload["locale"], payload["config_sha256"],
                     payload["model_pinned"], model_served, status, chash, digest, Json(checked["fact_coverage"]), Json(checked["rejected"]), len(content), job_id, payload["requested_by"]))
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'DOCUMENT_VARIANT_GENERATED',%s,%s)",
                    (tenant_id, workspace_id, payload["requested_by"], Json({"variant_id": payload["variant_id"], "artifact_id": payload["artifact_id"], "audience": payload["audience"], "status": status,
                                                                             "fact_coverage": checked["fact_coverage"], "rejected": len(checked["rejected"])})))
    return {**_row(store, tenant_id, workspace_id, payload["variant_id"]), "replayed": False}
