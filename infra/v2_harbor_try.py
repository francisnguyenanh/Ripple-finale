"""Thin Harbor page backend (read-only, no provider): run Harbor's OWN routing code for ONE requisition under the approval configuration of two
workspace source revisions and return both results side by side.

What runs: the pinned, hash-verified public Harbor copy (`fixtures/harbor_public`) through Harbor's own CLI (`route`), in the resource-limited runner used
by the simulation (`infra/worker/harbor_sim.py`): standard requisitions go to `route_request`, emergency ones to `route_emergency`, exactly as `cli.handle_route`
does. What does not run: any code from a workspace or an upload, any model. The only workspace-derived input is DATA: the `approval.yaml` bytes of a revision
(parsed with `yaml.safe_load`, allowlisted keys, bounded integers, then re-validated by Harbor's own loader inside the runner). The runner is advisory isolation,
not a security boundary; it is used because the executed code is the pinned public copy. Nothing here writes to the database.
"""
from __future__ import annotations

from infra.db_offload import db_route, request_json
import hashlib
import json
import re
import threading

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require
from infra.adapters.v2_postgres import BlobIntegrityError, TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _origin_ok, _v2_store
from infra.worker import harbor_sim

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
_BUSY = threading.Semaphore(1)                     # one runner at a time: a person trying a case, not a batch service
_ID = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
FIELDS = ("request_id", "requester_id", "supplier_id", "process", "category", "net_amount_jpy", "currency", "requested_at", "quantity", "item_description")
CODE_FILES = ("app/routing.py", "app/emergency.py", "app/config_loader.py")


def validate_requisition(req) -> dict:
    """Bounded, typed copy of the requisition, or HarborSimError. Harbor itself decides every business outcome (a negative amount is Harbor's REJECTED, not ours)."""
    bad = harbor_sim.HarborSimError
    if not isinstance(req, dict) or set(req) != set(FIELDS):
        raise bad("requisition_fields", "exactly these fields are required: " + ", ".join(FIELDS))
    for k in ("request_id", "requester_id", "supplier_id"):
        if not isinstance(req[k], str) or not _ID.match(req[k]):
            raise bad("requisition_field", f"{k} must be 1-40 letters, digits, dot, dash or underscore")
    if req["process"] not in ("STANDARD", "EMERGENCY") or req["category"] not in ("OPEX", "CAPEX"):
        raise bad("requisition_field", "process must be STANDARD or EMERGENCY and category OPEX or CAPEX")
    for k, lo, hi in (("net_amount_jpy", -10**10, 10**10), ("quantity", -10**6, 10**6)):
        if isinstance(req[k], bool) or not isinstance(req[k], int) or not lo <= req[k] <= hi:
            raise bad("requisition_field", f"{k} must be a whole number between {lo} and {hi}")
    if not isinstance(req["currency"], str) or not re.match(r"^[A-Z]{3}$", req["currency"]):
        raise bad("requisition_field", "currency must be a 3-letter code")
    if not isinstance(req["requested_at"], str) or not _ISO.match(req["requested_at"]):
        raise bad("requisition_field", "requested_at must look like 2026-09-01T09:00:00")
    if not isinstance(req["item_description"], str) or not 0 < len(req["item_description"]) <= 200 or not req["item_description"].isprintable():
        raise bad("requisition_field", "item_description must be 1-200 printable characters")
    return {k: req[k] for k in FIELDS}


def _blob_store():
    """A store that can read blobs (the request-scoped store has a null blob port), as the proposal-diff route builds it."""
    import os
    from infra import v2_source_routes
    from infra.adapters.v2_postgres import PostgresV2Store
    blobs, dsn = v2_source_routes._blob_port(), os.environ.get("RIPPLE_V2_DSN", "")
    return PostgresV2Store(dsn, blobs) if blobs is not None and dsn else None


def _config_of(store, tenant, ws, revision_id):
    view = store.source_revision_view(tenant, ws, revision_id)
    art = next((a for a in view["artifacts"] if str(a.get("logical_path") or "").endswith(harbor_sim.CONFIG_PATH_SUFFIX)), None)
    if art is None:
        return None
    blob_store = _blob_store()
    if blob_store is None:
        raise KeyError("source storage unavailable")
    data = blob_store.read_artifact_blob(tenant, ws, revision_id, art["artifact_id"])["content"]
    return {"source_revision_id": revision_id, "artifact_id": art["artifact_id"], "logical_path": art["logical_path"],
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": data}


def _numbers(data: bytes) -> dict:
    doc = yaml.safe_load(data)
    return {"standard_manager_threshold_jpy": harbor_sim._get(doc, "routing_rules.standard_manager_threshold_jpy", None),
            "capex_finance_threshold_jpy": harbor_sim._get(doc, "routing_rules.capex_finance_threshold_jpy", None)}


def code_revision() -> dict:
    manifest = json.loads((harbor_sim.FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
    return {"pinned_copy": "fixtures/harbor_public (hash-verified before every run)", "files": {f: manifest["files"][f] for f in CODE_FILES},
            "claim": "pinned public Harbor code, not a deployed Harbor application"}


def try_requisition(requisition: dict, before_yaml: bytes, after_yaml: bytes) -> dict:
    req = validate_requisition(requisition)
    for label, raw in (("before", before_yaml), ("after", after_yaml)):
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise harbor_sim.HarborSimError("config_invalid", f"{label}: not valid YAML") from exc
        harbor_sim._validate_config(doc, label)
    files = dict(harbor_sim.load_fixture())
    files["data/requisitions.jsonl"] = (json.dumps(req) + "\n").encode("utf-8")      # ONE requisition; the pinned suppliers file stays as it is
    out = {}
    for label, raw in (("before", before_yaml), ("after", after_yaml)):
        rows, meta = harbor_sim._run(files, "route", raw, f"try-{label}")
        if len(rows) != 1:
            raise harbor_sim.HarborSimError("runner_no_output", f"{len(rows)} rows")
        out[label] = {"decision": rows[0], "run": meta}
    b, a = out["before"]["decision"], out["after"]["decision"]
    out["changed"] = {k: [b.get(k), a.get(k)] for k in sorted(set(b) | set(a)) if k not in ("applied_config_version",) and b.get(k) != a.get(k)}
    return out


def _denied(exc):
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


@router.get("/v2/harbor/configs")
def harbor_configs(request: Request, workspace_id: str):
    """The source revisions of this workspace that hold Harbor's approval.yaml, with the two thresholds and the file hash (read-only)."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        head = store.head(tenant, workspace_id).source_revision_id
        ids = [r["source_revision_id"] for r in store.source_revision_ids(tenant, workspace_id)]
        out = []
        for i, rid in enumerate(ids, 1):
            cfg = _config_of(store, tenant, workspace_id, rid)
            if cfg is None:
                continue
            out.append({"source_revision_id": rid, "order": i, "is_head": rid == head, "config_sha256": cfg["sha256"], **_numbers(cfg["bytes"])})
        return JSONResponse({"revisions": out, "code": code_revision()}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, BlobIntegrityError, KeyError) as exc:
        return _error(str(exc), 404)


@router.post("/v2/harbor/try")
@db_route
def harbor_try(request: Request):
    if not _origin_ok(request):                      # same CSRF rule as every other POST: the exact configured origin and the custom header
        return _error("origin not allowed", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        body = request_json(request)
    except ValueError:
        return _error("a JSON body is required", 400)
    if not isinstance(body, dict) or not isinstance(body.get("workspace_id"), str):
        return _error("workspace_id is required", 400)
    ws = body["workspace_id"]
    try:
        authority = _authority(identity, request, ws)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        refs = {}
        for label in ("before", "after"):
            rid = body.get(f"{label}_revision_id")
            if not isinstance(rid, str) or not rid:
                return _error(f"{label}_revision_id is required", 400)
            cfg = _config_of(store, tenant, ws, rid)
            if cfg is None:
                return _error(f"revision {label} holds no approval.yaml", 404)
            refs[label] = cfg
        if not _BUSY.acquire(blocking=False):
            return _error("another case is running; try again in a moment", 503)
        try:
            res = try_requisition(body.get("requisition"), refs["before"]["bytes"], refs["after"]["bytes"])
        finally:
            _BUSY.release()
        res["input"] = {k: body["requisition"][k] for k in FIELDS}
        res["revisions"] = {k: {"source_revision_id": v["source_revision_id"], "logical_path": v["logical_path"], "config_sha256": v["sha256"], **_numbers(v["bytes"])}
                            for k, v in refs.items()}
        res["code"] = code_revision()
        return JSONResponse(res, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except harbor_sim.HarborSimError as exc:
        return _error(str(exc), 400 if exc.code.startswith(("requisition", "config")) else 503)
    except (TenantScopeError, BlobIntegrityError, KeyError) as exc:
        return _error(str(exc), 404)
