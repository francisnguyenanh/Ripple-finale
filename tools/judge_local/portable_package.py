"""Portable package of a LIVE-LOCAL workspace's valid outputs, for reuse by a candidate cloud environment. Offline tooling: it reads one
local workspace and writes a folder; it never writes to any database, never calls a provider and never touches a cloud.

  python -m tools.judge_local.portable_package export --out <dir>             # from the live-local database
  python -m tools.judge_local.portable_package verify <dir>                   # integrity + evidence re-check (no database needed)
  python -m tools.judge_local.portable_package plan <dir> --models role=model,... [--prompt-hashes a,b] [--schema-hashes c,d]

CONTENT (what a package holds): source bytes with hashes and revisions, observations with evidence locators, extraction outputs with the
model, prompt hash, schema hash, usage, cost and retries of each job, the decisions as PROVENANCE ONLY, and the cost ledger rows.
NEVER IN A PACKAGE: credentials, cookies, sessions, memberships, passwords, the database, prompts' text, or a simulated output. The export
refuses a database that has no live-local start record or any start record whose declared mode is not LIVE / LIVE_DISARMED, so stub and live
outputs are never mixed.

REUSE RULES (enforced by `plan`): an extraction is reusable only if its source bytes, model, prompt hash and schema hash match the target's
configuration; otherwise it must be regenerated (and that costs a call). Human confirmation or approval inside a package is provenance, not
authority: the target environment must check actor authority, target revision and digest itself. Importing is revalidation, not cloud generation:
the cloud still needs its own canary for wiring, model and ledger.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import sys

VERSION = 1
FILES = ("sources.json", "observations.jsonl", "extractions.jsonl", "decisions.jsonl", "ledger.jsonl")
FORBIDDEN_KEYS = {"credential", "credential_digest", "password", "cookie", "session", "token", "api_key", "request_context", "execution_request_context", "memberships"}


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _canon(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def _strip(obj):
    """Defence in depth: drop any forbidden key at any depth before anything is written."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k.lower() not in FORBIDDEN_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _write(out: pathlib.Path, name: str, rows) -> None:
    data = b"".join(_canon(_strip(r)) for r in rows) if name.endswith(".jsonl") else _canon(_strip(rows))
    (out / name).write_bytes(data)


def _integrity(out: pathlib.Path) -> dict:
    entries = {}
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            entries[p.relative_to(out).as_posix()] = _sha(p.read_bytes())
    root = _sha("".join(f"{k}={v}\n" for k, v in entries.items()).encode())
    return {"files": entries, "root": root}


# ------------------------------------------------------------------------------------------------------------------ export
def export(dsn: str, blobs, tenant: str, ws: str, out: pathlib.Path, *, declared_mode: str = "LIVE") -> dict:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    out.mkdir(parents=True, exist_ok=True)
    with psycopg2.connect(dsn) as cx, cx.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT payload FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='live_local.start'", (tenant, ws))
        starts = [r["payload"].get("mode") for r in cur.fetchall()]
        if not starts:
            raise SystemExit("refusing: no live-local start record: this database was not run by live_local, so its outputs may be simulated")
        if any(m not in ("LIVE", "LIVE_DISARMED") for m in starts):
            raise SystemExit("refusing: the database history contains a start that was not live")
        cur.execute("SELECT revision_id, parent_revision_id, manifest, created_at FROM v2_source_revisions WHERE tenant_id=%s AND workspace_id=%s ORDER BY created_at", (tenant, ws))
        revisions = cur.fetchall()
        cur.execute("SELECT source_revision_id, artifact_id, logical_path, blob_hash FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s ORDER BY source_revision_id, artifact_id", (tenant, ws))
        artifacts = cur.fetchall()
        cur.execute("""SELECT source_revision_id, artifact_id, observation_id, kind, locator, raw_value, fields, parser_version
                       FROM v2_source_observations WHERE tenant_id=%s AND workspace_id=%s ORDER BY source_revision_id, artifact_id, observation_id""", (tenant, ws))
        observations = cur.fetchall()
        cur.execute("""SELECT job_id, kind, pinned_source_revision_id, payload, execution_status, execution_result, execution_model_id, execution_prompt_hash,
                              execution_schema_hash, execution_usage, execution_cost_usd, execution_provider_calls, execution_duration_ms
                       FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction' AND execution_status='COMPLETED' ORDER BY job_id""", (tenant, ws))
        jobs = cur.fetchall()
        cur.execute("SELECT decision_id, epoch, actor_id, payload, created_at FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s ORDER BY epoch", (tenant, ws))
        decisions = cur.fetchall()
        cur.execute("SELECT job_id, entry_state, cost_usd, reserved_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s ORDER BY job_id", (tenant, ws))
        ledger = cur.fetchall()
    (out / "blobs").mkdir(exist_ok=True)
    for h in sorted({a["blob_hash"] for a in artifacts}):
        (out / "blobs" / h.removeprefix("sha256:")).write_bytes(blobs.read(h))
    _write(out, "sources.json", {"revisions": revisions, "artifacts": artifacts})
    _write(out, "observations.jsonl", observations)
    _write(out, "extractions.jsonl", [{**j, "payload": {k: v for k, v in (j["payload"] or {}).items() if k in ("artifact_id", "source_role")}} for j in jobs])
    _write(out, "decisions.jsonl", [{**d, "authority_transferable": False} for d in decisions])
    _write(out, "ledger.jsonl", ledger)
    cfg = os.environ.get("RIPPLE_BUDGET_CONFIG_PATH")
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, cwd=str(pathlib.Path(__file__).resolve().parents[2])).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        head = None
    manifest = {"package_version": VERSION, "code_head": head, "budget_config_sha256": _sha(pathlib.Path(cfg).read_bytes()) if cfg and pathlib.Path(cfg).is_file() else None, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "origin": {"mode": declared_mode, "tenant": tenant, "workspace": ws},
                "counts": {"revisions": len(revisions), "artifacts": len(artifacts), "observations": len(observations), "extractions": len(jobs), "decisions": len(decisions)},
                "never_included": sorted(FORBIDDEN_KEYS), "authority_transferable": False, "integrity": _integrity(out)}
    (out / "manifest.json").write_bytes(_canon(manifest))
    return manifest


# ------------------------------------------------------------------------------------------------------------------ verify
def _load(pkg: pathlib.Path):
    man = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    src = json.loads((pkg / "sources.json").read_text(encoding="utf-8"))
    rows = lambda n: [json.loads(x) for x in (pkg / n).read_text(encoding="utf-8").splitlines() if x.strip()]
    return man, src, rows("observations.jsonl"), rows("extractions.jsonl")


def package_keys(path: pathlib.Path):
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
    text = path.read_text(encoding="utf-8")
    for obj in ([json.loads(x) for x in text.splitlines() if x.strip()] if path.suffix == ".jsonl" else [json.loads(text)]):
        yield from walk(obj)


def _strings(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str):
                yield v
            else:
                yield from _strings(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, key)


def verify(pkg: pathlib.Path) -> dict:
    """Integrity of every file and blob, structural references, forbidden content, and an evidence re-check (every quote must occur in the
    observations of the same revision). Returns {'ok': bool, 'errors': [...], 'warnings': [...]}."""
    errors, warnings = [], []
    man, src, obs, ext = _load(pkg)
    if man.get("package_version") != VERSION:
        errors.append("unsupported package version")
    if man.get("origin", {}).get("mode") != "LIVE":
        errors.append("origin mode is not LIVE")
    now = _integrity(pkg)
    if now["root"] != man["integrity"]["root"]:
        for k in sorted(set(now["files"]) | set(man["integrity"]["files"])):
            if now["files"].get(k) != man["integrity"]["files"].get(k):
                errors.append(f"integrity: {k} differs from the manifest")
    for a in src["artifacts"]:
        p = pkg / "blobs" / a["blob_hash"].removeprefix("sha256:")
        if not p.is_file():
            errors.append(f"blob missing for {a['logical_path']}")
        elif _sha(p.read_bytes()) != a["blob_hash"]:
            errors.append(f"blob digest mismatch for {a['logical_path']}")
    revs = {r["revision_id"] for r in src["revisions"]}
    arts = {(a["source_revision_id"], a["artifact_id"]) for a in src["artifacts"]}
    for o in obs:
        if (o["source_revision_id"], o["artifact_id"]) not in arts:
            errors.append(f"observation {o['observation_id']} points to an unknown artifact")
    from infra.worker import extraction                      # the SAME quote rules the extraction executor used to accept a quote
    forms_by_art: dict = {}
    for o in obs:
        forms_by_art.setdefault((o["source_revision_id"], o["artifact_id"]), []).extend(extraction._quote_forms(o))
    for j in ext:
        rev = j["pinned_source_revision_id"]
        if rev not in revs:
            errors.append(f"extraction {j['job_id']} pins an unknown revision")
            continue
        for f in ("execution_model_id", "execution_prompt_hash", "execution_schema_hash"):
            if not j.get(f):
                errors.append(f"extraction {j['job_id']} has no {f}")
        forms = forms_by_art.get((rev, (j.get("payload") or {}).get("artifact_id")), [])
        quotes = list(_strings(j.get("execution_result"), "quote"))
        if not quotes:
            warnings.append(f"extraction {j['job_id']} carries no quote to re-check")
        for q in quotes:
            needle = extraction._normalise(q)
            if not needle or not any(needle in form for form in forms):
                errors.append(f"extraction {j['job_id']}: a quote is not found in the observations of its revision")
    for n in FILES:                                                    # forbidden KEYS (a document may legitimately mention the word password)
        for k in package_keys(pkg / n):
            if k.lower() in FORBIDDEN_KEYS:
                errors.append(f"forbidden key {k!r} in {n}")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ------------------------------------------------------------------------------------------------------------------ plan / remap
def plan(pkg: pathlib.Path, models: dict, prompt_hashes=None, schema_hashes=None) -> dict:
    """Per extraction: reuse or regenerate in a target. Nothing is imported; human decisions are listed as provenance only."""
    man, src, obs, ext = _load(pkg)
    out = {"reuse": [], "regenerate": [], "decisions_as_provenance_only": len(_read_rows(pkg / "decisions.jsonl")), "authority_transferred": False,
           "target_still_needs": ["its own canary for wiring, model and ledger", "its own actor authority, revision and digest checks before any apply"]}
    for j in ext:
        why = []
        want = models.get("extraction")
        if want is None:
            why.append("the target has no extraction model configured")
        elif j["execution_model_id"] != want:
            why.append(f"model differs ({j['execution_model_id']} vs {want})")
        if prompt_hashes is not None and j["execution_prompt_hash"] not in prompt_hashes:
            why.append("prompt hash differs")
        if schema_hashes is not None and j["execution_schema_hash"] not in schema_hashes:
            why.append("schema hash differs")
        (out["regenerate"] if why else out["reuse"]).append({"job_id": j["job_id"], "reasons": why} if why else {"job_id": j["job_id"]})
    return out


def _read_rows(p: pathlib.Path):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def remap(pkg: pathlib.Path, out: pathlib.Path, id_map: dict) -> dict:
    """Copy a package replacing every exact-match id (revision, artifact, job, decision ids) by its target id in EVERY file, then re-hash and
    re-verify. A reference left unmapped stays as it was, so verify() reports it rather than a silent dangling id."""
    import shutil
    if out.exists():
        raise SystemExit("refusing: the output folder already exists")
    shutil.copytree(pkg, out)

    def walk(o):
        if isinstance(o, dict):
            return {id_map.get(k, k): walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return id_map.get(o, o) if isinstance(o, str) else o
    for n in FILES:
        p = out / n
        if n.endswith(".jsonl"):
            p.write_bytes(b"".join(_canon(walk(json.loads(x))) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()))
        else:
            p.write_bytes(_canon(walk(json.loads(p.read_text(encoding="utf-8")))))
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    man["integrity"] = _integrity(out)
    man["remapped"] = True
    (out / "manifest.json").write_bytes(_canon(man))
    return verify(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", required=True)
    v = sub.add_parser("verify")
    v.add_argument("pkg")
    p = sub.add_parser("plan")
    p.add_argument("pkg")
    p.add_argument("--models", default="")
    p.add_argument("--prompt-hashes", default=None)
    p.add_argument("--schema-hashes", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "export":
        from tools.judge_local import live_local as L
        _, _, blobs = L.services()
        print(json.dumps(export(L.dsn(), blobs, L.TENANT, L.WS, pathlib.Path(a.out)), indent=2))
    elif a.cmd == "verify":
        r = verify(pathlib.Path(a.pkg))
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    else:
        models = dict(x.split("=", 1) for x in a.models.split(",") if "=" in x)
        sp = lambda s: None if s is None else set(s.split(","))
        print(json.dumps(plan(pathlib.Path(a.pkg), models, sp(a.prompt_hashes), sp(a.schema_hashes)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
