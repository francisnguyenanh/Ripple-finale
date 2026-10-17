"""J5: judge sandbox operation - lease (one writing session at a time per sandbox workspace), write guard, bounded
worker pump, status. OFF unless `RIPPLE_SANDBOX_CONFIG` names the sandbox workspace(s); every other workspace and
every deployment without that env var behaves exactly as before.

Design decisions (see JUDGE_EXECUTION_STATUS.md):
* The lease is an append-only chain of `v2_audit` events (SANDBOX_LEASE_ACQUIRED/RENEWED/RELEASED) decided under a
  Postgres advisory lock - no new table, no schema change on a shared database.
* The lease names a holder and optional participants (e.g. the judge's own requester and approver accounts). Everyone
  with project.read may READ; only holder/participants may WRITE (POST/PUT/PATCH/DELETE under /v2/), enforced by a
  server-side dependency, not by hiding buttons. Approval rules are untouched: a requester still cannot approve.
* The pump runs already-admitted jobs inside the request (bounded by job count and seconds, one pump at a time per
  process). It never approves, never creates jobs, never raises a budget; per-job/total caps stay in the worker.
* Reset is NOT a bypass: it is the existing history restore (preview -> distinct approval) toward the recorded seed
  revision. Ledger, audit and history are never deleted; spend does not reset with a session.
"""

from __future__ import annotations

import json
import os
import threading

import psycopg2
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg2.extras import Json

from infra import v2_source_routes
from infra.worker import sandbox_hold
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require
from infra.adapters.v2_postgres import PROJECT_SPEND_SQL, PostgresV2Store, TenantScopeError, project_spend_params
from infra.v2_policy_routes import _authority, _error, _identity_store, _json_body, _origin_ok, _token

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_PUMP_LOCK = threading.Lock()
_DEFAULTS = sandbox_hold.DEFAULTS
_EV = ("SANDBOX_LEASE_ACQUIRED", "SANDBOX_LEASE_RENEWED", "SANDBOX_LEASE_RELEASED")


class SandboxRefused(Exception):
    def __init__(self, status: int, body: dict):
        self.status, self.body = status, body


async def sandbox_refused_handler(request: Request, exc: SandboxRefused):
    return JSONResponse(exc.body, status_code=exc.status, headers=_NO_STORE)


config = sandbox_hold.config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease(cur, tenant: str, workspace_id: str) -> dict | None:
    """The CURRENT lease row (latest lease event), with `active` computed against the clock; None when never leased."""
    cur.execute("""SELECT id, event_type, actor_id, payload FROM v2_audit
        WHERE tenant_id=%s AND workspace_id=%s AND event_type = ANY(%s) ORDER BY id DESC LIMIT 1""",
                (tenant, workspace_id, list(_EV)))
    row = cur.fetchone()
    if row is None:
        return None
    event_id, kind, actor, payload = row
    payload = payload if isinstance(payload, dict) else json.loads(payload)
    expires = datetime.fromisoformat(payload["expires_at"])
    active = kind != "SANDBOX_LEASE_RELEASED" and expires > _now()
    return {"event_id": event_id, "active": active, "holder": payload.get("holder"),
            "participants": payload.get("participants") or [], "expires_at": payload["expires_at"],
            "jobs_at_start": payload.get("jobs_at_start", 0),
            "provider_jobs_at_start": payload.get("provider_jobs_at_start", 0), "last_actor": actor, "kind": kind}


def _job_count(cur, tenant: str, workspace_id: str) -> int:
    cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace_id))
    return cur.fetchone()[0]


def _provider_job_count(cur, tenant: str, workspace_id: str) -> int:
    cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind = ANY(%s)",
                (tenant, workspace_id, list(sandbox_hold.PROVIDER_JOB_KINDS)))
    return cur.fetchone()[0]


def _account(identity, request: Request) -> str | None:
    try:
        return identity.resolve_context(_token(request))["account_id"]
    except IdentityDenied:
        return None


def _may_write(lease: dict | None, account: str) -> bool:
    return bool(lease and lease["active"] and account in ([lease["holder"]] + list(lease["participants"])))


async def sandbox_guard(request: Request) -> None:
    """Global dependency: refuse WRITES to a sandbox workspace unless the caller is on the active lease. No-op when the
    feature is off, for reads, for auth/sandbox routes and for requests that name no sandbox workspace."""
    cfg = config()
    if not cfg or request.method not in _WRITE_METHODS:
        return
    path = request.url.path
    if not path.startswith("/v2/") or path.startswith(("/v2/auth/", "/v2/sandbox/")):
        return
    workspace_id = request.query_params.get("workspace_id")
    if workspace_id is None:
        body = await _json_body(request)
        workspace_id = body.get("workspace_id")
    if workspace_id not in cfg:
        return
    identity = _identity_store()
    account = _account(identity, request) if identity else None
    if account is None:
        return                                    # not signed in: the route itself answers 401
    store = PostgresV2Store(os.environ["RIPPLE_V2_DSN"], None)
    try:
        ctx = _authority(identity, request, workspace_id)
    except IdentityDenied:
        return                                    # no membership: the route itself answers 403
    with store._tx() as cur:
        lease = _lease(cur, ctx["tenant_id"], workspace_id)
        if not _may_write(lease, account):
            msg = ("This sandbox is read-only for you: take the lease on the Sandbox page first"
                   + (f" (another session holds it until {lease['expires_at']})" if lease and lease["active"] else ""))
            raise SandboxRefused(409, {"error": msg, "code": "sandbox_lease_required",
                                       "held_by_other": bool(lease and lease["active"])})
        if path == "/v2/jobs" and request.method == "POST":
            limit = cfg[workspace_id]["max_jobs_per_lease"]
            if _job_count(cur, ctx["tenant_id"], workspace_id) - lease["jobs_at_start"] >= limit:
                raise SandboxRefused(429, {"error": f"This lease allows at most {limit} jobs; release and re-take it.",
                                           "code": "sandbox_job_limit"})
            kind = (await _json_body(request)).get("kind")
            plimit = cfg[workspace_id]["max_provider_jobs_per_lease"]
            if (kind in sandbox_hold.PROVIDER_JOB_KINDS
                    and _provider_job_count(cur, ctx["tenant_id"], workspace_id) - lease["provider_jobs_at_start"] >= plimit):
                raise SandboxRefused(429, {"error": f"This lease allows at most {plimit} model-calling jobs "
                                                    "(re-extraction after an apply counts); release and re-take it.",
                                           "code": "sandbox_provider_job_limit"})


def _spend(cur, tenant: str, workspace_id: str) -> float:
    cur.execute(PROJECT_SPEND_SQL, project_spend_params(tenant, workspace_id))
    return float(cur.fetchone()[0])


def _cap() -> float | None:
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        return None
    try:
        from infra.worker import job_quota
        return float(job_quota.load_worker_budget_config().total_cap_usd)
    except Exception:
        return None


def _status(store, ctx: dict, workspace_id: str, account: str) -> dict:
    cfg = config()[workspace_id]
    head = store.head(ctx["tenant_id"], workspace_id)
    with store._tx() as cur:
        lease = _lease(cur, ctx["tenant_id"], workspace_id)
        cur.execute("""SELECT execution_status, count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s
            AND execution_status IN ('QUEUED','RUNNING') GROUP BY 1""", (ctx["tenant_id"], workspace_id))
        live = dict(cur.fetchall())
        cur.execute("""SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s
            AND status='PENDING_APPROVAL'""", (ctx["tenant_id"], workspace_id))
        waiting = cur.fetchone()[0]
        jobs = _job_count(cur, ctx["tenant_id"], workspace_id)
        pjobs = _provider_job_count(cur, ctx["tenant_id"], workspace_id)
        spent = _spend(cur, ctx["tenant_id"], workspace_id)
    cap = _cap()
    return {"enabled": True, "workspace_id": workspace_id, "account_id": account,
            "seed_revision_id": cfg["seed_revision_id"], "head_source_revision_id": head.source_revision_id,
            "at_seed": bool(cfg["seed_revision_id"]) and head.source_revision_id == cfg["seed_revision_id"],
            "lease": None if lease is None else {k: lease[k] for k in ("active", "holder", "participants", "expires_at")},
            "i_can_write": _may_write(lease, account),
            "jobs": {"queued": live.get("QUEUED", 0), "running": live.get("RUNNING", 0), "waiting_for_approval": waiting,
                     "used_in_lease": (jobs - lease["jobs_at_start"]) if lease and lease["active"] else 0,
                     "limit_per_lease": cfg["max_jobs_per_lease"],
                     "provider_used_in_lease": (pjobs - lease["provider_jobs_at_start"]) if lease and lease["active"] else 0,
                     "provider_limit_per_lease": cfg["max_provider_jobs_per_lease"]},
            "spend": {"settled_usd": round(spent, 4), "cap_usd": cap,
                      "remaining_usd": None if cap is None else round(cap - spent, 4),
                      "note": "project-wide; a session reset or a new lease never resets spend"},
            "limits": {k: cfg[k] for k in ("lease_minutes", "max_jobs_per_lease", "max_provider_jobs_per_lease",
                                           "pump_max_jobs", "pump_seconds", "min_remaining_usd")},
            "reset": {"how": "history restore toward the seed revision (preview, then a distinct approver)",
                      "seed_revision_id": cfg["seed_revision_id"]}}


def _ctx(request: Request, workspace_id: str):
    identity = _identity_store()
    if identity is None:
        return None, None, None, _error("v2 unavailable", 503)
    try:
        # sign-in and membership first: an anonymous caller must not learn whether a sandbox exists for a workspace
        ctx = _authority(identity, request, workspace_id)
        require(ctx, "project.read")
        if workspace_id not in config():
            return None, None, None, _error("no sandbox is configured for this workspace", 404)
    except IdentityDenied as exc:
        return None, None, None, _error("sign in required" if "sign in" in str(exc) else "permission denied",
                                        401 if "sign in" in str(exc) else 403)
    except PermissionDenied as exc:
        return None, None, None, _error(f"permission denied: {exc}", 403)
    return identity, ctx, PostgresV2Store(os.environ["RIPPLE_V2_DSN"], None), None


@router.get("/v2/sandbox/status")
def sandbox_status(request: Request, workspace_id: str):
    identity, ctx, store, err = _ctx(request, workspace_id)
    if err is not None:
        return err
    try:
        return JSONResponse(_status(store, ctx, workspace_id, ctx["account_id"]), headers=_NO_STORE)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/sandbox/lease")
async def sandbox_lease(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    body = await _json_body(request)
    workspace_id, action = body.get("workspace_id"), body.get("action")
    identity, ctx, store, err = _ctx(request, workspace_id if isinstance(workspace_id, str) else "")
    if err is not None:
        return err
    if action not in ("acquire", "renew", "release"):
        return _error("action must be acquire, renew or release", 400)
    account, cfg = ctx["account_id"], config()[workspace_id]
    participants = [p for p in (body.get("participants") or []) if isinstance(p, str) and p and p != account][:4]
    tenant = ctx["tenant_id"]
    with store._tx() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"sandbox:{tenant}:{workspace_id}",))
        lease = _lease(cur, tenant, workspace_id)
        mine = bool(lease and lease["active"] and lease["holder"] == account)
        if action == "release":
            if lease and lease["active"] and not mine:
                return _error("only the holder can release an active lease", 403)
            expires = _now()
        else:
            if lease and lease["active"] and not mine:
                return _error(f"sandbox is leased by another session until {lease['expires_at']}", 409)
            if action == "renew" and not mine:
                return _error("no active lease to renew", 409)
            expires = _now() + timedelta(minutes=cfg["lease_minutes"])
        if participants:
            cur.execute("SELECT account_id FROM v2_memberships WHERE tenant_id=%s AND workspace_id=%s AND account_id = ANY(%s)",
                        (tenant, workspace_id, participants))
            known = {r[0] for r in cur.fetchall()}
            unknown = [p for p in participants if p not in known]
            if unknown:
                return _error("participants must be members of this workspace: " + ", ".join(unknown), 400)
        keep = (lease["participants"] if (mine and action == "renew" and not participants) else participants)
        payload = {"holder": account if action != "release" else (lease or {}).get("holder", account),
                   "participants": keep if action != "release" else [],
                   "expires_at": expires.isoformat(),
                   "jobs_at_start": (lease["jobs_at_start"] if mine and action == "renew" else _job_count(cur, tenant, workspace_id)),
                   "provider_jobs_at_start": (lease["provider_jobs_at_start"] if mine and action == "renew"
                                              else _provider_job_count(cur, tenant, workspace_id))}
        event = {"acquire": "SANDBOX_LEASE_ACQUIRED", "renew": "SANDBOX_LEASE_RENEWED", "release": "SANDBOX_LEASE_RELEASED"}[action]
        cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload)
            VALUES (%s,%s,%s,%s,%s)""", (tenant, workspace_id, event, account, Json(payload)))
    return JSONResponse(_status(store, ctx, workspace_id, account), headers=_NO_STORE)


@router.post("/v2/sandbox/pump")
async def sandbox_pump(request: Request):
    """Run already-admitted jobs of the sandbox workspace, bounded. Needs the active lease. Never approves/creates."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    identity, ctx, store, err = _ctx(request, workspace_id if isinstance(workspace_id, str) else "")
    if err is not None:
        return err
    cfg, tenant = config()[workspace_id], ctx["tenant_id"]
    with store._tx() as cur:
        lease = _lease(cur, tenant, workspace_id)
        spent = _spend(cur, tenant, workspace_id)
    if not _may_write(lease, ctx["account_id"]):
        return _error("take the sandbox lease before running jobs", 409)
    cap = _cap()
    if cap is None:
        return _error("budget configuration is not available; jobs are not run", 503)
    if cap - spent < cfg["min_remaining_usd"]:
        return _error(f"remaining budget {cap - spent:.2f} USD is below the reserve {cfg['min_remaining_usd']}; no job is run", 429)
    if not _PUMP_LOCK.acquire(blocking=False):
        return JSONResponse({"processed": 0, "busy": True, "message": "another pump is running in this instance"},
                            status_code=202, headers=_NO_STORE)
    # One pump per sandbox workspace ACROSS instances: a session-level advisory lock in the shared database.
    lock_conn = psycopg2.connect(os.environ["RIPPLE_V2_DSN"])
    lock_conn.autocommit = True
    lock_cur = lock_conn.cursor()
    lock_cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"sandbox-pump:{tenant}:{workspace_id}",))
    if not lock_cur.fetchone()[0]:
        lock_conn.close()
        _PUMP_LOCK.release()
        return JSONResponse({"processed": 0, "busy": True, "message": "another pump is running (another instance)"},
                            status_code=202, headers=_NO_STORE)
    processed, events, held, started = 0, 0, 0, time.monotonic()
    worker_id = f"sandbox-pump-{ctx['account_id']}"
    try:
        from infra.worker.job_worker import claim_and_admit_one
        run_store = PostgresV2Store(os.environ["RIPPLE_V2_DSN"], v2_source_routes._blob_port())
        while (processed < cfg["pump_max_jobs"] and events < 50
               and time.monotonic() - started < cfg["pump_seconds"]):
            claim = claim_and_admit_one(run_store, tenant, workspace_id, worker_id,
                                        lease_seconds=int(cfg["pump_seconds"]) + 120)
            if claim is None:
                break
            events += 1
            if claim.kind == "JOB_QUEUED":
                payload = claim.payload if isinstance(claim.payload, dict) else {}
                # The approval hold lives in the worker's execute_admitted_claim (every consumer of the queue gets it);
                # here it is only read back to tell "held for approval" from "run".
                if sandbox_hold.approval_hold(run_store, tenant, workspace_id, payload.get("job_id"), payload.get("kind")):
                    held += 1
                else:
                    processed += 1
    finally:
        try:
            lock_cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"sandbox-pump:{tenant}:{workspace_id}",))
        finally:
            lock_conn.close()
            _PUMP_LOCK.release()
    return JSONResponse({"processed": processed, "events": events, "waiting_for_approval": held, "busy": False,
                         "bounded_by": {"max_jobs": cfg["pump_max_jobs"], "seconds": cfg["pump_seconds"]}},
                        headers=_NO_STORE)
