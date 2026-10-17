"""Offline importer / dry-run for a portable package into a LOCAL, ISOLATED store. It never contacts a cloud, never calls a provider and never writes outside
the one workspace it is given. A cloud import would be a cloud mutation and is NOT implemented here.

  dry run (default): verifies everything it can and writes NOTHING;  apply: writes only after every check for that extraction passed.

What is checked before an extraction output is written (any failure = that extraction is NOT reused and is listed with its reason, never silently accepted):
  1. the package: every file and blob hash, structure, forbidden keys, every quote found in the observations of its revision (`portable_package.verify`);
  2. the ACTOR: the account named by the operator must hold the admin role in the target workspace (a package carries no memberships, no credentials and no approval authority);
  3. the TARGET REVISION: the workspace is empty (the sources are then imported through the production import route) or already holds a revision with exactly the same
     logical paths and blob hashes; a different revision is refused and nothing is overwritten;
  4. COMPATIBILITY: the recorded model equals the target's configured model for the role, and the prompt hash and schema hash RECOMPUTED by the target's own extraction
     executor on the target revision equal the recorded ones (so source bytes, parser output, prompt template, config ceilings and schema all match);
  5. EVIDENCE: every candidate's quote is re-resolved against the TARGET's observations through the same resolver the extraction executor uses.
Reuse writes a COMPLETED `extraction` job per output (same kind, so the product reads it like any other) with executor `portable_import.v1`, provider calls 0, cost 0 and an
`imported_from` provenance block. It writes NO outbox event (no worker can claim it), NO decision and NO approval: human confirmations in the package stay provenance, the
target operator re-decides. Ids are remapped (revision, artifacts, jobs) and idempotent: a second run finds the deterministic job ids and writes nothing.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import secrets  # noqa: F401  (kept for CLI parity)
from psycopg2.extras import Json

from core.change_intelligence.v2.serialize import canonical_hash
from infra.worker import extraction
from infra.worker.budget_config import model_for_role
from infra.worker.execution_types import ExecutionContext
from tools.judge_local import portable_package as P

IMPORT_EXECUTOR = "portable_import.v1"


def remap_json(obj, id_map: dict):
    """Replace every old id token inside any string of `obj` (ids are unique prefixed tokens, so this also covers ids embedded in longer ids)."""
    if not id_map:
        return obj
    pattern = re.compile("|".join(re.escape(k) for k in sorted(id_map, key=len, reverse=True)))
    sub = lambda s: pattern.sub(lambda m: id_map[m.group(0)], s)

    def walk(o):
        if isinstance(o, dict):
            return {sub(k) if isinstance(k, str) else k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return sub(o) if isinstance(o, str) else o
    return walk(obj)


def _roles(store, tenant, ws, actor) -> list[str]:
    with store._tx() as cur:
        cur.execute("SELECT roles FROM v2_memberships WHERE tenant_id=%s AND workspace_id=%s AND account_id=%s", (tenant, ws, actor))
        row = cur.fetchone()
    return list(row[0]) if row else []


def _revalidate(store, tenant, ws, rev, artifact_id, candidates) -> list[str]:
    """Re-resolve each candidate's evidence on the TARGET revision with the extraction executor's own rules. Returns the reasons it fails (empty = valid)."""
    obs = [o for o in store.source_observations(tenant, ws, rev) if o["artifact_id"] == artifact_id]
    view = store.source_revision_view(tenant, ws, rev)
    art = next((a for a in view["artifacts"] if a["artifact_id"] == artifact_id), None)
    bad = []
    if art is None:
        return ["artifact missing in the target revision"]
    for c in candidates:
        for ev in c.get("evidence") or []:
            try:
                locator = json.loads(ev["locator"]) if isinstance(ev.get("locator"), str) else ev.get("locator")
            except ValueError:
                bad.append("evidence locator is not JSON")
                continue
            match = [o for o in obs if o["locator"] == locator and o["parser_version"] == ev.get("parser_version")]
            if len(match) != 1:
                bad.append("evidence locator not found once in the target observations")
                continue
            o = match[0]
            needle = extraction._normalise(ev.get("quote") or "")
            if not needle or not any(needle in form for form in extraction._quote_forms(o)):
                bad.append("quote is not in the target observation")
                continue
            ref = {"source_revision_id": rev, "artifact_id": artifact_id, "blob_hash": art.get("blob_hash"), "parser_version": o["parser_version"], "locator": o["locator"]}
            if extraction._observation_label(o) is None:
                ref["quote"] = ev["quote"]
            else:
                ref["raw_value"] = o["raw_value"]
            rep = store.validate_evidence(tenant, ws, rev, [ref])
            if not rep or not rep[0]["citation_valid"]:
                bad.append("the target's citation resolver rejects the evidence")
    return bad


def _scope(man: dict, src: dict, ext: list) -> dict:
    """What an import of this package keeps, what it only records and what it cannot carry. This is an EXTRACTION-REUSE package, not a workspace restore."""
    revs = {j["pinned_source_revision_id"] for j in ext}
    return {
        "kind": "reuse of AI extraction outputs (not a restore of the workspace)",
        "kept_when_applied": ["source bytes of ONE revision (hash-checked)", "extraction outputs of that revision, re-validated on the target: prompt hash, schema hash, served model, every quote re-resolved to the target's observations",
                              "citations (re-resolved, never trusted)", "revision/artifact/job id mapping (written into each imported job as imported_from)"],
        "package_revisions": len(src.get("revisions") or []), "revisions_with_outputs": len(revs),
        "not_imported_but_kept_in_the_package": ["the other revision(s): one revision is imported per run (outputs pinned to another revision are listed as regenerate)", "cost ledger rows (history only)",
                                                  f"knowledge decisions ({man['counts'].get('decisions', 0)}; provenance only, never authority)"],
        "not_carried_unsupported": ["confirmations of claims and every approval or decision as authority", "free-text conversations and the structured requests", "draft, impact, simulation, proposal, review, approval and apply jobs (the change history)",
                                    "AI explanations", "knowledge revisions, lifecycle/stale state, the audit trail", "memberships, credentials, sessions"],
        "claim_limit": "importing proves the reused outputs still match the sources, prompt and quotes of the target; it does not move the knowledge state, and the cloud still needs its own canary",
    }


def run(pkg: pathlib.Path, *, store, tenant: str, ws: str, actor: str, config, import_sources=None, apply: bool = False) -> dict:
    rep = {"ok": False, "dry_run": not apply, "errors": [], "warnings": [], "reuse": [], "regenerate": [], "written": [], "skipped_existing": [],
           "authority_transferred": False, "memberships_or_credentials_imported": False}
    v = P.verify(pkg)
    rep["warnings"] += v["warnings"]
    if not v["ok"]:
        rep["errors"] += [f"package: {e}" for e in v["errors"]]
        return rep
    man, src, obs, ext = P._load(pkg)
    if "admin" not in _roles(store, tenant, ws, actor):
        rep["errors"].append("the actor does not hold the admin role in the target workspace: nothing is imported on a package's say-so")
        return rep
    pinned = [j["pinned_source_revision_id"] for j in ext]
    rep["scope"] = _scope(man, src, ext)
    if not pinned:
        rep["errors"].append("the package has no extraction output to reuse")
        return rep
    rev = max(set(pinned), key=lambda r: (pinned.count(r), r))
    arts = [a for a in src["artifacts"] if a["source_revision_id"] == rev]
    want = {a["logical_path"]: a["blob_hash"] for a in arts}
    head = store.head(tenant, ws)
    target_rev = None
    if head.source_revision_id:
        have = {a["logical_path"]: a["blob_hash"] for a in store.source_revision_view(tenant, ws, head.source_revision_id)["artifacts"]}
        if have != want:
            rep["errors"].append("the target workspace holds a different source revision: refused, nothing is overwritten")
            return rep
        target_rev = head.source_revision_id
    elif apply:
        if import_sources is None:
            rep["errors"].append("an empty target needs the production import route (import_sources)")
            return rep
        target_rev = import_sources([(a["logical_path"], (pkg / "blobs" / a["blob_hash"].removeprefix("sha256:")).read_bytes()) for a in arts])
    else:
        rep["warnings"].append("the target is empty: apply would import the source files through the production route first; prompt/evidence checks on the target run then")
    art_map = {}
    if target_rev:
        by_path = {a["logical_path"]: a["artifact_id"] for a in store.source_revision_view(tenant, ws, target_rev)["artifacts"]}
        art_map = {a["artifact_id"]: by_path[a["logical_path"]] for a in arts if a["logical_path"] in by_path}
    root = man["integrity"]["root"]
    tag = root.removeprefix("sha256:")[:8]
    want_model = model_for_role(config, "extraction")
    for j in sorted(ext, key=lambda x: x["job_id"]):
        why = []
        if j["pinned_source_revision_id"] != rev:
            why.append("pinned to another package revision: one revision is imported per run")
        if j["execution_model_id"] != want_model:
            why.append(f"model differs ({j['execution_model_id']} vs {want_model})")
        aid = (j.get("payload") or {}).get("artifact_id")
        new_job = f"imp-{tag}-{hashlib.sha256(j['job_id'].encode()).hexdigest()[:8]}"
        if target_rev and not why:
            new_art = art_map.get(aid)
            if new_art is None:
                why.append("artifact not found in the target revision")
            else:
                job = {"tenant_id": tenant, "workspace_id": ws, "job_id": new_job, "kind": "extraction", "pinned_source_revision_id": target_rev,
                       "payload": {"artifact_id": new_art, "source_role": (j.get("payload") or {}).get("source_role") or "unknown"}}
                try:
                    request = extraction.ExtractRulesExecutor().prepare(ExecutionContext(store=store, job=job, config=config))
                    if canonical_hash({"prompt": request.prompt}) != j["execution_prompt_hash"]:
                        why.append("prompt hash differs on the target revision (source, parser, template or ceilings differ)")
                    if canonical_hash(dict(request.schema)) != j["execution_schema_hash"]:
                        why.append("schema hash differs")
                except Exception as exc:                      # an extraction the target cannot even prepare is not reusable
                    why.append(f"the target cannot prepare this extraction ({type(exc).__name__})")
                if not why:
                    why += _revalidate(store, tenant, ws, target_rev, new_art, (j.get("execution_result") or {}).get("candidates") or [])
        item = {"origin_job_id": j["job_id"], "job_id": new_job}
        (rep["regenerate"] if why else rep["reuse"]).append({**item, "reasons": why} if why else item)
    if not apply:
        rep["ok"] = True
        return rep
    id_map = {rev: target_rev, **art_map}
    for item in rep["reuse"]:
        j = next(x for x in ext if x["job_id"] == item["origin_job_id"])
        id_map_j = {**id_map, j["job_id"]: item["job_id"]}
        result = remap_json(j["execution_result"], id_map_j)
        payload = remap_json({"artifact_id": j["payload"]["artifact_id"], "source_role": j["payload"].get("source_role") or "unknown"}, id_map)
        payload["imported_from"] = {"package_root": root, "origin_job_id": j["job_id"], "origin_model": j["execution_model_id"], "origin_mode": man["origin"]["mode"]}
        result["imported_from"] = payload["imported_from"]
        if _write_job(store, tenant, ws, item["job_id"], target_rev, head, payload, result, j, actor, root):
            rep["written"].append(item["job_id"])
        else:
            rep["skipped_existing"].append(item["job_id"])
    rep["ok"] = True
    return rep


def _write_job(store, tenant, ws, job_id, rev, head, payload, result, j, actor, root) -> bool:
    with store._tx() as cur:
        cur.execute("SELECT 1 FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (tenant, ws, job_id))
        if cur.fetchone():
            return False
        cur.execute("""INSERT INTO v2_jobs (tenant_id, workspace_id, job_id, kind, status, pinned_source_revision_id, pinned_knowledge_revision_id, pinned_decision_epoch,
                           payload, requested_by, approved_by, approved_with_roles, execution_status, execution_result, execution_model_id, execution_prompt_hash,
                           execution_schema_hash, execution_usage, execution_cost_usd, execution_duration_ms, execution_executor_id, execution_provider_calls)
                       VALUES (%s,%s,%s,'extraction','APPROVED',%s,%s,%s,%s,'portable-import',%s,%s,'COMPLETED',%s,%s,%s,%s,%s,0,0,%s,0)""",
                    (tenant, ws, job_id, rev, head.knowledge_revision_id, head.decision_epoch, Json(payload), actor, ["admin"], Json(result),
                     j["execution_model_id"], j["execution_prompt_hash"], j["execution_schema_hash"], Json({"provider_calls": 0, "imported": True}), IMPORT_EXECUTOR))
        cur.execute("""INSERT INTO v2_job_attempts (tenant_id, workspace_id, job_id, attempt_key, outcome, executor_id, model_id, prompt_hash, schema_hash, request_context,
                           provider_calls, cost_estimate_usd, cost_usd)
                       VALUES (%s,%s,%s,%s,'COMPLETED',%s,%s,%s,%s,%s,0,0,0)""",
                    (tenant, ws, job_id, f"import:{job_id}", IMPORT_EXECUTOR, j["execution_model_id"], j["execution_prompt_hash"], j["execution_schema_hash"],
                     Json({"imported_from": {"package_root": root, "origin_job_id": j["job_id"]}, "note": "no provider call was made for this output"})))
        cur.execute("""INSERT INTO v2_job_cost_ledger (tenant_id, workspace_id, job_id, cost_usd, entry_state, settled_at) VALUES (%s,%s,%s,0,'SETTLED',now())
                       ON CONFLICT DO NOTHING""", (tenant, ws, job_id))
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'portable.import',%s,%s)",
                    (tenant, ws, actor, Json({"package_root": root, "job_id": job_id, "origin_job_id": j["job_id"]})))
    return True


def main(argv=None) -> int:
    """CLI for the LIVE-LOCAL instance (the local persistent database): `python -m tools.judge_local.portable_import <package dir> [--apply]`.
    Dry run by default. The importing account is `live-admin` (or --actor); its password comes from RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD and is never printed.
    It targets only the live-local database and workspace; there is no flag to point it anywhere else, and no cloud importer exists."""
    import argparse
    import base64
    import os
    import sys
    from fastapi.testclient import TestClient
    from infra.worker import job_quota
    from tools.judge_local import live_local as L
    ap = argparse.ArgumentParser(description=main.__doc__.split("\n", 1)[0])
    ap.add_argument("package")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--actor", default="live-admin")
    a = ap.parse_args(argv)
    L.ensure_database()
    store, ids, blobs = L.services()
    L.provision(store, ids)
    L.configure_env(blobs)
    config = job_quota.load_worker_budget_config()

    def import_sources(files):
        password = os.environ.get("RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD")
        if not password:
            raise SystemExit("set RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD (not printed or stored)")
        from infra import judge
        c = TestClient(judge.app, base_url="https://judge.test")
        c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
        if c.post("/v2/auth/login", json={"account_id": a.actor, "credential": password}).status_code != 200:
            raise SystemExit("login failed")
        body = {"workspace_id": L.WS, "idempotency_key": secrets.token_hex(8), "files": [{"path": p, "content_b64": base64.b64encode(b).decode()} for p, b in files]}
        r = c.post("/v2/sources/imports", json=body)
        if r.status_code != 200:
            raise SystemExit(f"import refused (HTTP {r.status_code})")
        r2 = c.post(f"/v2/sources/imports/{r.json()['import_id']}/commit", json={"workspace_id": L.WS, "expected_version": 0, "acknowledged_skips": []})
        if r2.status_code != 200:
            raise SystemExit(f"commit refused (HTTP {r2.status_code})")
        from infra.worker.job_worker import claim_and_admit_one
        claim_and_admit_one(store, L.TENANT, L.WS, "portable-import")           # the source_ingest job (parsing only)
        return store.head(L.TENANT, L.WS).source_revision_id
    rep = run(pathlib.Path(a.package), store=store, tenant=L.TENANT, ws=L.WS, actor=a.actor, config=config, import_sources=import_sources, apply=a.apply)
    print(json.dumps(rep, indent=2))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
