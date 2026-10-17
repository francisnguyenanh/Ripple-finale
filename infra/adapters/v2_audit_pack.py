"""Audit evidence package (MON-AUD, local): a scoped, deterministic zip of what people decided and what the system recorded, with a manifest of file hashes and a standalone verifier.

Scope = one workspace, optionally one business change. Contents are projections of the stored rows (knowledge versions, decisions, change decisions, extension code decisions, the
change's jobs without prompts, audit events, policy decisions) plus `coverage.json` (what is in, what is capped, what is redacted, what is `not_available`). SHA-256 hashes detect a
changed byte against the manifest; they are **not a signature**: a party that rewrites payload and manifest together is not detected (recorded in the manifest). Generated documents
are not included as evidence. Reads only.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import zipfile

from infra.adapters.v2_postgres import TenantScopeError, _as_object
from infra.adapters.v2_restore import read_snapshot

MAX_ROWS = 2000
VERIFIER = r'''#!/usr/bin/env python3
"""Standalone verifier for a Ripple audit package (stdlib only). Usage: python verify_audit_pack.py <package.zip>
Exit 0 when every file listed in MANIFEST.json is present with the recorded SHA-256 and no unlisted file exists; 1 otherwise.
A hash match means the bytes equal the manifest; it is not a signature and does not prove who produced the package."""
import hashlib, json, sys, zipfile

def main(path):
    problems = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        for n in names:
            if n.startswith("/") or ".." in n.split("/") or "\\" in n:
                problems.append(f"unsafe name: {n}")
        manifest = json.loads(z.read("MANIFEST.json").decode("utf-8"))
        listed = {f["file"]: f["sha256"] for f in manifest["files"]}
        for name, sha in listed.items():
            if name not in names:
                problems.append(f"missing: {name}"); continue
            got = hashlib.sha256(z.read(name)).hexdigest()
            if got != sha:
                problems.append(f"hash differs: {name}")
        for n in names - set(listed) - {"MANIFEST.json", "verify_audit_pack.py"}:
            problems.append(f"unlisted file: {n}")
    for p in problems:
        print("PROBLEM", p)
    print("OK" if not problems else "FAILED", len(listed), "files checked")
    return 0 if not problems else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
'''


def _rows(cur, sql, params):
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        row = {}
        for c, v in zip(cols, r):
            if isinstance(v, (dt.datetime, dt.date)):
                v = v.isoformat()
            elif isinstance(v, (dict, list)):
                pass
            elif isinstance(v, str) and c in ("body", "payload", "coverage", "binding", "pin", "execution_result") and v[:1] in "{[":
                v = _as_object(v)
            row[c] = v
        out.append(row)
    return out


def _redact_job(rec: dict) -> dict:
    res = rec.get("execution_result") or {}
    keep = {k: res.get(k) for k in ("executor_id", "status", "model_status", "pin", "applies_nothing", "provider_calls", "model", "config_sha256", "patch_hash", "validation_hash", "artifact_hash", "report_hash") if k in res}
    return {"job_id": rec["job_id"], "kind": rec["kind"], "status": rec["status"], "execution_status": rec["execution_status"], "requested_by": rec["requested_by"], "execution_reason": rec.get("execution_reason"),
            "result_summary": keep}


def build(store, tenant_id, workspace_id, *, change_id: str | None, can_read_evidence: bool, requested_by: str) -> tuple[bytes, dict]:
    """(zip bytes, manifest). Deterministic for the same stored rows (fixed zip timestamps; the generation time is recorded in the manifest only)."""
    with read_snapshot(store) as cur:
        versions = _rows(cur, """SELECT snapshot_id, business_revision, lifecycle_state, committed_by, created_at, content_hash, coverage FROM v2_business_snapshots
                                 WHERE tenant_id=%s AND workspace_id=%s AND lifecycle_state='ACCEPTED' ORDER BY business_revision LIMIT %s""", (tenant_id, workspace_id, MAX_ROWS))
        decisions = _rows(cur, """SELECT decision_id, snapshot_id, actor_id, kind, quote, body, created_at FROM v2_business_decisions WHERE tenant_id=%s AND workspace_id=%s
                                  ORDER BY created_at, decision_id LIMIT %s""", (tenant_id, workspace_id, MAX_ROWS))
        change_sql = """SELECT decision_id, intent_id AS change_id, kind, action, actor_id, reason, semantic_diff_hash, draft_hash, self_approval, created_at FROM v2_business_change_decisions
                        WHERE tenant_id=%s AND workspace_id=%s""" + (" AND intent_id=%s" if change_id else "") + " ORDER BY created_at, decision_id LIMIT %s"
        change_decisions = _rows(cur, change_sql, (tenant_id, workspace_id, change_id, MAX_ROWS) if change_id else (tenant_id, workspace_id, MAX_ROWS))
        code_sql = """SELECT decision_id, change_id, candidate_job_id, kind, actor_id, reason, patch_hash, validation_hash, body, created_at FROM v2_ext_code_decisions
                      WHERE tenant_id=%s AND workspace_id=%s""" + (" AND change_id=%s" if change_id else "") + " ORDER BY created_at, decision_id LIMIT %s"
        code_decisions = _rows(cur, code_sql, (tenant_id, workspace_id, change_id, MAX_ROWS) if change_id else (tenant_id, workspace_id, MAX_ROWS))
        audit_sql = "SELECT id, event_type, actor_id, payload, created_at FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s" + (
            " AND (payload->>'intent_id'=%s OR payload->>'change_id'=%s)" if change_id else "") + " ORDER BY id LIMIT %s"
        audit = _rows(cur, audit_sql, (tenant_id, workspace_id, change_id, change_id, MAX_ROWS + 1) if change_id else (tenant_id, workspace_id, MAX_ROWS + 1))
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s", (tenant_id, workspace_id))
        audit_total = cur.fetchone()[0]
        jobs = []
        if change_id:
            cur.execute("""SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND (job_id=%s OR payload->>'intent_id'=%s OR payload->>'change_id'=%s
                           OR payload->'built_from'->>'business_change_id'=%s) ORDER BY job_id LIMIT %s""", (tenant_id, workspace_id, change_id, change_id, change_id, change_id, MAX_ROWS))
            ids = [r[0] for r in cur.fetchall()]
    if change_id:
        for jid in ids:
            try:
                jobs.append(_redact_job(store.job_record(tenant_id, workspace_id, jid)))
            except TenantScopeError:
                continue
    if not can_read_evidence:
        for d in decisions:                                                   # evidence references and raw values stay with evidence readers
            body = d.get("body") or {}
            d["body"] = {k: v for k, v in body.items() if k not in ("evidence_refs", "quote_refs", "file_writes", "edits")}
        for c in code_decisions:
            c["body"] = {k: v for k, v in (c.get("body") or {}).items() if k not in ("diff", "files")}
    audit_truncated = len(audit) > MAX_ROWS
    audit = audit[:MAX_ROWS]
    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    coverage = {"scope": {"workspace_id": workspace_id, "change_id": change_id}, "requested_by": requested_by, "evidence_rights": can_read_evidence,
                "included": {"knowledge_versions": len(versions), "decisions": len(decisions), "change_decisions": len(change_decisions), "code_decisions": len(code_decisions), "jobs": len(jobs), "audit_events": len(audit)},
                "caps": {"rows_per_file": MAX_ROWS, "audit_events_truncated": audit_truncated, "audit_events_total_in_workspace": audit_total},
                "redactions": [] if can_read_evidence else ["evidence references, file edits and diffs are omitted for a reader without evidence rights"],
                "not_available": {"git_commit": "no real repository binding exists in this build", "deployment": "no real deployment exists in this build; mock promotions are listed in code_decisions"},
                "excluded_by_design": ["generated documents (views, not evidence)", "model prompts and raw model output", "source file bytes"],
                "integrity": "SHA-256 per file in MANIFEST.json; detects a changed byte against the manifest; NOT a digital signature: a party rewriting payload and manifest together is not detected",
                "statement": "This package lists what people decided and what the system recorded for the scope above. It is not a certification and does not replace an auditor."}
    files = {
        "knowledge_versions.json": versions, "decisions.json": decisions, "change_decisions.json": change_decisions, "code_decisions.json": code_decisions, "jobs.json": jobs,
        "audit_events.json": audit, "coverage.json": coverage,
    }
    index = ["# Ripple audit package", "", f"Workspace: {workspace_id}" + (f"  Change: {change_id}" if change_id else ""), f"Generated: {generated_at}", "",
             "| File | Rows | What it holds |", "|---|---|---|",
             f"| knowledge_versions.json | {len(versions)} | accepted business knowledge versions (id, revision, who, when, content hash, coverage) |",
             f"| decisions.json | {len(decisions)} | human decisions on knowledge (answers, acceptances, revocations, change commits) with the person's own words |",
             f"| change_decisions.json | {len(change_decisions)} | approvals / rejections / clarifications of business changes, bound to the reviewed hashes |",
             f"| code_decisions.json | {len(code_decisions)} | extension: code patch approvals, refused publish intents, mock promotions, verifications, rollbacks |",
             f"| jobs.json | {len(jobs)} | the change's jobs (kind, status, requester, result summary; no prompts) |",
             f"| audit_events.json | {len(audit)} | server audit events{' (truncated)' if audit_truncated else ''} |",
             "| coverage.json | 1 | scope, caps, redactions, what is not available, integrity limits |", "",
             "Verify: `python verify_audit_pack.py <this zip>`. " + coverage["integrity"] + ".", "", coverage["statement"]]
    buf = io.BytesIO()
    manifest = {"schema": "ripple-audit-pack/1", "generated_at": generated_at, "scope": coverage["scope"], "files": [], "integrity": coverage["integrity"]}
    import hashlib
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        payloads = {"index.md": "\n".join(index).encode("utf-8")}
        for name, obj in files.items():
            payloads[name] = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        for name in sorted(payloads):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, payloads[name])
            manifest["files"].append({"file": name, "sha256": hashlib.sha256(payloads[name]).hexdigest(), "size_bytes": len(payloads[name])})
        for name, text in (("MANIFEST.json", json.dumps(manifest, indent=2, sort_keys=True)), ("verify_audit_pack.py", VERIFIER)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text)
    return buf.getvalue(), manifest
