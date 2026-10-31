"""The worker as a long-running Cloud Run SERVICE (option B of the serving options; NOT deployed, NOT enabled).

Cloud Run services must listen on $PORT and are restarted when the container exits or a probe fails. This entry point adds exactly that around the existing loop:
  * one daemon thread answers `GET /healthz` (200 only while the claim loop made a pass recently; 503 otherwise, so a stuck loop fails the liveness probe and is restarted);
  * the main thread runs `run_bounded_worker` with NO runtime/idle bound (it polls the runtime switches itself: `worker_claims` OFF = it claims nothing and keeps polling);
  * any exception that escapes the loop ends the process with a non-zero code (Cloud Run starts a fresh container; the DB lease/fencing then lets the new worker reclaim what the dead one held).
It adds no new claim, charge or budget path: claims, leases, fencing, reservations and the provider gate are exactly `claim_and_admit_one`'s.
Run: `python -m infra.worker.service` (same environment variables as the job: RIPPLE_V2_DSN, RIPPLE_WORKER_TENANT_ID/WORKSPACE_ID or RIPPLE_WORKER_WORKSPACES, RIPPLE_WORKER_POLL_SECONDS)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from infra.worker import job_worker

STALE_AFTER_SECONDS = 60.0
#: A claim that is executing (a provider call can last up to the request timeout, 180 s in the approved config, plus the lease margin) is NOT a stuck loop: the liveness
#: probe must not kill a job that is working normally. Beyond this limit the claim is considered hung and the container is restarted (the DB lease then lets another worker reclaim it).
BUSY_LIMIT_SECONDS = float(os.environ.get("RIPPLE_WORKER_BUSY_LIMIT_SECONDS", "0")) or None


def busy_limit_for(config) -> float:
    """How long ONE claim may legitimately run, derived from the approved configuration instead of a guess. A claim is: build the request, ONE provider call (at most the request
    timeout, 180 s in the approved config), validate, commit. A retry is a SEPARATE claim (it waits its backoff in the queue, not inside the claim). The claim's lease is the base 30 s
    stretched to the provider timeout plus a 15 s margin (195 s): beyond it the claim is already reclaimable by another worker, so a claim still running then is abnormal.
    The probe tolerates twice the lease (390 s: slow commits and queries are inside this) and no more: a hung call (an SDK that ignores its timeout) is caught within about 6.5 minutes."""
    return float(2 * job_worker.effective_lease_seconds(config, 30))


class Liveness:
    """What the health endpoint needs to tell three situations apart:
    * POLLING / PAUSED - the loop keeps completing passes (with `worker_claims` OFF every pass is a quick no-op, so a deliberately paused worker stays healthy: no restart loop);
    * BUSY - one claim is executing: healthy while it is younger than the busy limit (`busy_limit_for(config)`: twice the lease, 390 s in the approved config);
    * STUCK - no completed pass for STALE_AFTER_SECONDS while not busy, or a claim older than the busy limit: unhealthy, the platform restarts the container."""

    def __init__(self, clock=time.monotonic, busy_limit: float | None = None):
        self.clock, self.busy_limit = clock, busy_limit or BUSY_LIMIT_SECONDS or 390.0
        self.last_pass, self.passes, self.busy_since = clock(), 0, None

    def touch(self) -> None:
        self.last_pass = self.clock()
        self.passes += 1

    def begin(self) -> None:
        self.busy_since = self.clock()

    def end(self) -> None:
        self.busy_since = None
        self.touch()

    def state(self, stale_after: float = STALE_AFTER_SECONDS) -> str:
        now = self.clock()
        if self.busy_since is not None:
            return "busy" if now - self.busy_since < self.busy_limit else "stuck"
        return "polling" if now - self.last_pass < stale_after else "stuck"

    def healthy(self, stale_after: float = STALE_AFTER_SECONDS) -> bool:
        return self.state(stale_after) != "stuck"


def make_handler(liveness: Liveness, stale_after: float = STALE_AFTER_SECONDS):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):                               # noqa: N802
            ok = self.path.split("?")[0] in ("/healthz", "/") and liveness.healthy(stale_after)
            body = json.dumps({"ok": ok, "state": liveness.state(stale_after), "passes": liveness.passes}).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):                      # no request logging: nothing here is worth a line
            return
    return Handler


def serve_health(liveness: Liveness, port: int, stale_after: float = STALE_AFTER_SECONDS) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(liveness, stale_after))
    threading.Thread(target=server.serve_forever, daemon=True, name="healthz").start()
    return server


class Deadline:
    """An optional serving deadline (`RIPPLE_WORKER_SERVICE_DEADLINE_UTC`, ISO time). When it passes the service CLOSES ITSELF TO WORK: it switches `worker_claims` OFF once (audited, reason
    "service deadline reached") and keeps polling (healthy, paused). It cannot delete itself (that needs an IAM role nobody approved), so the idle instance keeps costing
    0.0821 USD/h until the operator deletes the service; what stops by itself is every claim, hence every provider call."""

    def __init__(self, iso: str | None, set_flag=None, now=None):
        from datetime import datetime, timezone
        self.at = datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None
        self.set_flag, self.fired = set_flag, False
        self._now = now or (lambda: datetime.now(timezone.utc))

    def check(self) -> bool:
        if self.at is None or self.fired or self._now() < self.at:
            return False
        self.fired = True
        if self.set_flag is not None:
            self.set_flag("worker_claims", "off", "service", "service deadline reached: claims stopped by the service itself; the instance must be deleted by the operator")
        return True


def run_service(store, workspaces, *, poll_seconds: float, liveness: Liveness, claim_fn=None, sleep=time.sleep, stop=None, deadline: "Deadline | None" = None) -> dict:
    """The claim loop with a liveness mark per pass. `stop()` (tests only) ends it; in production nothing does: it runs until the process is stopped."""
    base = claim_fn or job_worker.claim_and_admit_one

    def marked(*a, **k):
        liveness.touch()
        if stop is not None and stop():
            raise StopIteration
        if deadline is not None:
            deadline.check()
        liveness.begin()                                # a claim may run a whole provider call: BUSY, not stuck
        try:
            return base(*a, **k)
        finally:
            liveness.end()
    try:
        return job_worker.run_bounded_worker(store, workspaces, poll_seconds=poll_seconds, claim_fn=marked, sleep=sleep)
    except StopIteration:
        return {"stopped": "test_stop"}


def main() -> int:
    dsn = os.environ.get("RIPPLE_V2_DSN")
    if not dsn:
        print("RIPPLE_V2_DSN required", file=sys.stderr)
        return 2
    try:
        settings = job_worker.load_worker_settings()
    except job_worker.WorkerConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    config = None
    if os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        config = job_worker._load_config_if_configured()
    from infra.worker import execution_lease
    lease = None
    if execution_lease.enabled():
        lease = execution_lease.acquire(dsn)                 # REL-04: one worker execution at a time per database (enforced, not assumed)
        if lease is None:
            print("another_execution_active: this execution exits, the running one keeps the work", file=sys.stderr)
            return 0
    liveness = Liveness(busy_limit=busy_limit_for(config))
    serve_health(liveness, int(os.environ.get("PORT", "8080")))
    from infra import operator_flags
    deadline = Deadline(os.environ.get("RIPPLE_WORKER_SERVICE_DEADLINE_UTC"), set_flag=lambda k, v, actor, reason: operator_flags.set_flag(k, v, actor, reason))
    try:
        run_service(job_worker._make_store(dsn), settings["workspaces"], poll_seconds=settings["poll_seconds"], liveness=liveness, deadline=deadline)
    finally:
        if lease is not None:
            lease.release()
    return 1                                            # the loop must never return: if it does, exit non-zero so the platform restarts the container


if __name__ == "__main__":
    raise SystemExit(main())
