"""Runner-side adapter: builds the TRUSTED run context for the Pitch scorer from the product's own import result and
store records. No product schema change, no permission system. Values are NEVER taken from predictions.

Sources
  * tenant_id / workspace_id / source_revision_id: the identifiers the runner used for the real import
    (prepare -> commit -> source_ingest job); `store.source_revision_view(tenant, workspace, revision)` raises
    TenantScopeError when the revision does not belong to that tenant/workspace, so a foreign tenant/workspace/revision
    cannot be turned into a context.
  * artifact ids and content hashes: the store's `view["artifacts"]` (artifact_id, logical_path, blob_hash).
    blob_hash = "sha256:" + sha256(imported bytes) (core.change_intelligence.v2.source_admission._digest); if the runner
    passes the imported bytes they are re-hashed and compared.
Independent trusted hash (anti re-hash forgery): the digest of the context body must be obtained from a source
independent of the context file and of the prediction:
  (1) recompute it from the store with `trusted_hash_from_store(...)` (same builder, fresh store read), or
  (2) the run manifest written at import time by `record_manifest_hash` (a SEPARATE file from the context file).
A hash that the scorer or a forger recomputes from the context file proves nothing and is refused (state `unpinned`).
"""
from __future__ import annotations

import json
from pathlib import Path

from core.change_intelligence.v2.source_admission import _digest as product_blob_digest  # product hash, not re-invented
from pitch.eval.scorer import make_run_context


def build_run_context_from_import(store, tenant_id, workspace_id, source_revision_id, run_id,
                                  artifact_paths: dict, imported_files: dict | None = None):
    """artifact_paths: {label/scorer artifact id: logical_path as imported}. Returns (context, context_sha256).
    Raises TenantScopeError (store) for a revision foreign to tenant/workspace, ValueError for unknown paths or when
    imported bytes do not hash to the store's blob_hash."""
    view = store.source_revision_view(tenant_id, workspace_id, source_revision_id)
    by_path = {a["logical_path"]: a for a in view["artifacts"]}
    rev_ids, hashes = {}, {}
    for aid, path in artifact_paths.items():
        art = by_path.get(path)
        if art is None:
            raise ValueError(f"logical path {path!r} is not in revision {source_revision_id!r}")
        if imported_files is not None and product_blob_digest(imported_files[path]) != art["blob_hash"]:
            raise ValueError(f"imported bytes of {path!r} do not match the store blob_hash")
        rev_ids[aid] = [art["artifact_id"]]
        hashes[aid] = art["blob_hash"]  # keyed by the scorer's artifact id, like artifact_revision_ids
    ctx = make_run_context(tenant_id, workspace_id, [source_revision_id], rev_ids, run_id=run_id,
                           artifact_content_hashes=hashes)
    return ctx, ctx["sha256"]


def trusted_hash_from_store(store, tenant_id, workspace_id, source_revision_id, run_id, artifact_paths: dict) -> str:
    """Independent source (1): recompute the context digest from a fresh store read."""
    return build_run_context_from_import(store, tenant_id, workspace_id, source_revision_id, run_id,
                                         artifact_paths)[1]


def persist_context(directory, run_id, ctx) -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{run_id}.context.json"
    p.write_text(json.dumps(ctx, sort_keys=True, indent=1), encoding="utf-8")
    return p


def load_contexts(directory) -> dict:
    return {p.name[:-len(".context.json")]: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(Path(directory).glob("*.context.json"))}


def record_manifest_hash(manifest_path, run_id, sha256) -> None:
    """Independent source (2): run manifest entry written AT IMPORT TIME, kept apart from the context files."""
    p = Path(manifest_path)
    m = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    m[run_id] = sha256
    p.write_text(json.dumps(m, sort_keys=True, indent=1), encoding="utf-8")


def load_manifest_hashes(manifest_path) -> dict:
    return json.loads(Path(manifest_path).read_text(encoding="utf-8"))
