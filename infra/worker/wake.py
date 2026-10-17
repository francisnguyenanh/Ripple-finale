"""On-demand wake-up of the batch worker (Cloud Run Job), so no worker polls while nobody uses the product.

The database stays the source of job state. This module only starts an execution of the existing batch worker when there is claimable work and no worker is alive:
  * `kick(store)` (web side, after a request that may have queued a job): counts the claimable outbox rows (the same predicate `claim_outbox` uses); if there are none, or a worker's heartbeat
    is fresh, or a kick was sent a moment ago (one atomic debounce row shared by all web instances), it does nothing; otherwise it asks Cloud Run to run the job. A FAILED signal never fails the
    user's request (the job is already stored): the failure is recorded and the debounce is released, so the next request or the scheduler backstop retries.
  * `beat(store, worker_id)` / `clear_beat(store, worker_id)` (worker side): the heartbeat that tells `kick` a worker is alive; cleared BEFORE the worker's final sweep, so a job queued while it exits
    either is seen by that sweep or finds no heartbeat and starts a new execution.
A duplicate or late signal can only start an extra execution that finds nothing to do and exits: claims, leases, fencing tokens, durable provider attempts, the ledger and the provider gate are exactly
the existing ones, so a wake-up never creates a second provider call or exceeds an approved attempt. Cloud Scheduler runs the same job every few minutes as the backstop for missed signals, expired
leases and retries that were waiting for their backoff. Nothing here calls a provider."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

#: a kick is not repeated within this time (the platform needs a few seconds to start the execution)
KICK_GAP_SECONDS = float(os.environ.get("RIPPLE_WAKE_GAP_SECONDS", "45"))
#: a worker whose last heartbeat is younger than this is alive
BEAT_FRESH_SECONDS = float(os.environ.get("RIPPLE_WAKE_BEAT_SECONDS", "30"))

_DDL = """CREATE TABLE IF NOT EXISTS v2_worker_wake (
    id int PRIMARY KEY CHECK (id = 1), last_kick_at timestamptz, last_ok boolean, last_error text, kicks bigint NOT NULL DEFAULT 0, failures bigint NOT NULL DEFAULT 0, worker_seconds double precision NOT NULL DEFAULT 0)"""
#: One row per kick = the seconds that kick reserved. `execution_name` is the Cloud Run execution that kick started (read from the answer of the run call); only THAT execution settles the row, found through
#: the platform-set `CLOUD_RUN_EXECUTION`. Additive table; code that does not know it is unaffected. A row never matched (lost execution, unreadable answer) keeps its seconds counted: the safe direction.
_RESERVATIONS = """CREATE TABLE IF NOT EXISTS v2_worker_reservations (id bigserial PRIMARY KEY, reserved_at timestamptz NOT NULL DEFAULT now(), execution_name text)"""
_RESERVATIONS_IDX = """CREATE UNIQUE INDEX IF NOT EXISTS v2_worker_reservations_exec ON v2_worker_reservations (execution_name) WHERE execution_name IS NOT NULL"""
_BEATS = """CREATE TABLE IF NOT EXISTS v2_worker_beats (worker_id text PRIMARY KEY, beat_at timestamptz NOT NULL DEFAULT now())"""

CLAIMABLE = """kind='JOB_QUEUED' AND (status='PENDING' OR (status='CLAIMED' AND lease_until < now())) AND attempts < 5"""


def workspace_scope() -> tuple[str, list]:
    """Only work this worker can claim wakes it: `RIPPLE_WAKE_WORKSPACES=tenant:workspace[,tenant:workspace]` (the worker's allowlist). Rows of other workspaces (orphans of old tests) would otherwise start an
    empty execution at every tick. Unset = every workspace (the old behaviour)."""
    raw = os.environ.get("RIPPLE_WAKE_WORKSPACES", "").strip()
    pairs = [tuple(p.strip().split(":", 1)) for p in raw.split(",") if ":" in p]
    if not pairs:
        return "", []
    return " AND (tenant_id, workspace_id) IN %s", [tuple(pairs)]


def _int_env(name: str) -> int:
    try:
        return max(0, int(float(os.environ.get(name, "0"))))
    except ValueError:
        return 0


def kick_budget_exhausted(store) -> bool:
    """The infrastructure self-limit on worker compute, two counters since the operator last reset them:
      * `RIPPLE_WAKE_MAX_SECONDS`: a cap on worker SECONDS. Every start of an execution RESERVES `RIPPLE_WAKE_TASK_SECONDS` (the task timeout, the longest an execution can bill); when the worker exits it
        replaces its reservation by its real duration. A killed or lost execution keeps its full reservation, so the counter never under-counts what was started through the wake-up: billed worker time
        <= counted seconds (executions started by other means, e.g. by hand, are not counted).
      * `RIPPLE_WAKE_MAX_KICKS`: a cap on the number of executions started.
    Unset or 0 = no cap. Fail open on a read error (the window and the provider cap still apply)."""
    max_s, max_k = _int_env("RIPPLE_WAKE_MAX_SECONDS"), _int_env("RIPPLE_WAKE_MAX_KICKS")
    if max_s <= 0 and max_k <= 0:
        return False
    try:
        with store._tx() as cur:
            _ensure(cur)
            cur.execute("SELECT kicks, worker_seconds FROM v2_worker_wake WHERE id = 1")
            kicks, secs = cur.fetchone()
            return (max_k > 0 and kicks >= max_k) or (max_s > 0 and secs >= max_s)
    except Exception:
        return False


def record_worker_exit(store, elapsed_seconds: float, execution_name: str | None, *, retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Called by the worker when it exits: replace the reservation of THIS execution (the row whose `execution_name` is its own) by what it really ran (never below zero). True = settled.
    An execution with no reservation (started by hand, or whose kick recorded no name) changes nothing: the seconds already counted belong to other executions, and the runtime of such an execution is
    NOT counted against `RIPPLE_WAKE_MAX_SECONDS` (that cap bounds executions started through the wake-up only). The row delete and the counter update are one transaction, so a repeated call is a no-op.
    The kick records the name right after the run call answers; the worker has run for at least its idle time by now, and retries a few times only to cover a slow write."""
    task = _int_env("RIPPLE_WAKE_TASK_SECONDS")
    if task <= 0 or not execution_name:
        return False
    for attempt in range(max(1, retries)):
        with store._tx() as cur:
            _ensure(cur)
            cur.execute("DELETE FROM v2_worker_reservations WHERE execution_name = %s", (execution_name,))
            if cur.rowcount == 1:
                cur.execute("UPDATE v2_worker_wake SET worker_seconds = greatest(0, worker_seconds - %s + %s) WHERE id = 1", (float(task), float(max(0.0, elapsed_seconds))))
                return True
        if attempt + 1 < retries:
            sleep(2.0)
    return False


def enabled() -> bool:
    return bool(os.environ.get("RIPPLE_WAKE_JOB"))


_ready = False


def _ensure(cur) -> None:
    global _ready
    if _ready:
        return
    cur.execute(_DDL)
    cur.execute("ALTER TABLE v2_worker_wake ADD COLUMN IF NOT EXISTS worker_seconds double precision NOT NULL DEFAULT 0")
    cur.execute(_BEATS)
    cur.execute(_RESERVATIONS)
    cur.execute(_RESERVATIONS_IDX)
    cur.execute("INSERT INTO v2_worker_wake (id) VALUES (1) ON CONFLICT DO NOTHING")
    _ready = True


def run_cloud_run_job(job_name: str, *, opener=urllib.request.urlopen, timeout: float = 2.0) -> str | None:
    """Ask Cloud Run to run the job (`projects/P/locations/R/jobs/NAME`) with the service account of the web service (metadata-server token; needs run.jobs.run on that job). Raises on any failure. Returns the short name of the execution it started (the answer's `metadata.name`), or None when the answer does not carry it."""
    tok_req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token", headers={"Metadata-Flavor": "Google"})
    with opener(tok_req, timeout=timeout) as r:
        token = json.loads(r.read().decode("utf-8"))["access_token"]
    req = urllib.request.Request(f"https://run.googleapis.com/v2/{job_name}:run", data=b"{}", method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with opener(req, timeout=timeout) as r:
        if r.status not in (200, 201):
            raise RuntimeError(f"run job answered {r.status}")
        try:
            name = (json.loads(r.read().decode("utf-8")).get("metadata") or {}).get("name")
            return name.rsplit("/", 1)[-1] if isinstance(name, str) and "/executions/" in name else None
        except Exception:
            return None


def kick(store, *, post: Callable[[str], None] | None = None, job_name: str | None = None) -> dict:
    """-> {"kicked": bool, "reason": ...}. Never raises."""
    name = job_name or os.environ.get("RIPPLE_WAKE_JOB")
    if not name:
        return {"kicked": False, "reason": "disabled"}
    from infra import operator_flags
    if operator_flags.window_over():                            # the evaluation window has ended: the worker claims nothing any more, so no execution is started
        return {"kicked": False, "reason": "window_ended"}
    if kick_budget_exhausted(store):
        return {"kicked": False, "reason": "kick_budget_exhausted"}
    if not operator_flags.claims_on():                          # claims are switched off: an execution would only claim nothing and exit. Jobs stay QUEUED and visible until claims are switched on.
        return {"kicked": False, "reason": "claims_off"}
    try:
        with store._tx() as cur:
            _ensure(cur)
            scope, args = workspace_scope()
            cur.execute(f"SELECT count(*) FROM v2_outbox WHERE {CLAIMABLE}{scope}", args)
            if cur.fetchone()[0] == 0:
                return {"kicked": False, "reason": "no_work"}
            cur.execute("SELECT count(*) FROM v2_worker_beats WHERE beat_at > now() - (%s * interval '1 second')", (BEAT_FRESH_SECONDS,))
            if cur.fetchone()[0] > 0:
                return {"kicked": False, "reason": "worker_alive"}
            cur.execute("""UPDATE v2_worker_wake SET last_kick_at = now(), kicks = kicks + 1, worker_seconds = worker_seconds + %s
                           WHERE id = 1 AND (last_kick_at IS NULL OR last_kick_at < now() - (%s * interval '1 second'))""", (float(_int_env("RIPPLE_WAKE_TASK_SECONDS")), KICK_GAP_SECONDS))
            if cur.rowcount != 1:
                return {"kicked": False, "reason": "recent_kick"}
            cur.execute("INSERT INTO v2_worker_reservations DEFAULT VALUES RETURNING id")      # the execution this kick starts will claim these seconds
            reservation_id = cur.fetchone()[0]
    except Exception as exc:                                    # the user's request is already answered; never fail it for the signal
        return {"kicked": False, "reason": "db_error", "error": type(exc).__name__}
    try:
        started = (post or run_cloud_run_job)(name)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {str(exc)[:160]}"
        try:
            with store._tx() as cur:                            # release the debounce so the next request / the scheduler retries at once
                # a 4xx answer means the execution was NOT created: give back THIS kick's reserved seconds, once (only if its row is still there and unowned); any other failure (timeout, 5xx) may have
                # started one, so the reservation stays (counted, never refunded)
                refund = 0.0
                if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                    cur.execute("DELETE FROM v2_worker_reservations WHERE id = %s AND execution_name IS NULL", (reservation_id,))
                    refund = float(_int_env("RIPPLE_WAKE_TASK_SECONDS")) if cur.rowcount == 1 else 0.0
                cur.execute("UPDATE v2_worker_wake SET last_kick_at = NULL, last_ok = false, last_error = %s, failures = failures + 1, worker_seconds = greatest(0, worker_seconds - %s) WHERE id = 1", (msg, refund))
        except Exception:
            pass
        return {"kicked": False, "reason": "signal_failed", "error": msg}
    try:
        with store._tx() as cur:
            if isinstance(started, str) and started:            # tie the reservation to the execution this kick started: only that execution may settle it
                cur.execute("UPDATE v2_worker_reservations SET execution_name = %s WHERE id = %s AND execution_name IS NULL", (started, reservation_id))
            cur.execute("UPDATE v2_worker_wake SET last_ok = true, last_error = NULL WHERE id = 1")
    except Exception:
        pass
    return {"kicked": True, "reason": "signal_sent"}


def kick_in_background(store_factory: Callable[[], object | None], **kw) -> None:
    """Fire and forget from the web request path."""
    if not enabled() and not kw.get("job_name"):
        return

    def _run():
        try:
            store = store_factory()
            if store is not None:
                kick(store, **kw)
        except Exception:
            pass
    threading.Thread(target=_run, name="wake-kick", daemon=True).start()


def beat(store, worker_id: str) -> None:
    with store._tx() as cur:
        _ensure(cur)
        cur.execute("""INSERT INTO v2_worker_beats (worker_id, beat_at) VALUES (%s, now())
                       ON CONFLICT (worker_id) DO UPDATE SET beat_at = now()""", (worker_id,))


def clear_beat(store, worker_id: str) -> None:
    with store._tx() as cur:
        _ensure(cur)
        cur.execute("DELETE FROM v2_worker_beats WHERE worker_id = %s", (worker_id,))
