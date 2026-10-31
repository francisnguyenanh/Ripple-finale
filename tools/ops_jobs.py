"""Operator view and cancel of queued jobs for an operating window (one-shot; reads RIPPLE_V2_DSN from the platform's secret store).

Used when a deployment is switched off between operating windows and before it is switched on again: jobs left QUEUED from the previous window must not run by
themselves when AI is switched back on (no silent retry of old work). RUNNING jobs are never touched here: they finish or fail under their lease; the operator waits
for them (the `list` count reaches 0) before taking the backup.

  python -m tools.ops_jobs list --tenant T --workspace W
  python -m tools.ops_jobs cancel-queued --tenant T --workspace W --reason "end of operating window 1"            # dry run: shows what would be cancelled
  python -m tools.ops_jobs cancel-queued --tenant T --workspace W --reason "end of operating window 1" --apply    # cancels: terminal CANCELLED with the reason

A cancelled job keeps its history and provenance; its queue event is acknowledged by the worker without running it (no provider call, nothing charged).

  python -m tools.ops_jobs trace --request-id R      # AH-05: the job(s) an HTTP request created or replayed, their status, sanitized reason and attempts

`trace` prints identifiers, states, typed reasons and attempt metadata only (no payload, prompt, answer or source text).
Exit codes: 0 done, 2 missing input, 3 database error. Output: counts and job ids only.
"""
from __future__ import annotations

import argparse
import os
import sys


def queued_and_running(dsn: str, tenant: str, workspace: str) -> list:
    import psycopg2
    with psycopg2.connect(dsn) as cx, cx.cursor() as cur:
        cur.execute("""SELECT job_id, kind, execution_status FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND execution_status IN ('QUEUED','RUNNING')
                       ORDER BY kind, job_id""", (tenant, workspace))
        return cur.fetchall()


def _trace(dsn: str, request_id: str) -> int:
    import psycopg2
    from infra.adapters.v2_postgres import PostgresV2Store

    class _NoBlobs:
        def __getattr__(self, name):
            raise RuntimeError("no blob is read")

    try:
        store = PostgresV2Store(dsn, _NoBlobs())
        links = store.jobs_for_request(request_id)
        print(f"request {request_id}: {len(links)} job link(s)")
        for link in links:
            rec = store.job_record(link["tenant_id"], link["workspace_id"], link["job_id"])
            print(f"  job {link['job_id']} ({link['tenant_id']}/{link['workspace_id']}) {'created' if link['created'] else 'replayed'} at {link['recorded_at']}: "
                  f"kind={rec.get('kind')} status={rec.get('execution_status')} reason={rec.get('execution_reason')}")
            for att in store.job_attempts(link["tenant_id"], link["workspace_id"], link["job_id"]):
                diag = (att.get("request_context") or {}).get("provider_diagnostics") or {}
                print(f"    attempt {att['attempt_key']}: {att['outcome']} reason={att.get('reason')} calls={att.get('provider_calls')} "
                      f"cost={att.get('cost_usd')} diagnostics={diag}")
    except psycopg2.Error as exc:
        print(f"database error: {type(exc).__name__}", file=sys.stderr)
        return 3
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="List or cancel queued jobs of one workspace.")
    ap.add_argument("command", choices=("list", "cancel-queued", "trace", "replay-parked"))
    ap.add_argument("--kind", default="")
    ap.add_argument("--tenant", default="")
    ap.add_argument("--workspace", default="")
    ap.add_argument("--request-id", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    if not dsn:
        print("RIPPLE_V2_DSN is not set", file=sys.stderr)
        return 2
    if a.command == "trace":
        if not a.request_id.strip():
            print("--request-id is required for trace", file=sys.stderr)
            return 2
        return _trace(dsn, a.request_id.strip())
    if not a.tenant or not a.workspace:
        print("--tenant and --workspace are required", file=sys.stderr)
        return 2
    if a.command == "cancel-queued" and len(a.reason.strip()) < 8:
        print("--reason (at least 8 characters) is required: it is stored on every cancelled job", file=sys.stderr)
        return 2
    import psycopg2
    try:
        rows = queued_and_running(dsn, a.tenant, a.workspace)
        queued = [r for r in rows if r[2] == "QUEUED"]
        running = [r for r in rows if r[2] == "RUNNING"]
        print(f"queued {len(queued)}, running {len(running)}")
        for jid, kind, status in rows:
            print(f"  {status:8} {kind:24} {jid}")
        from infra.adapters.v2_postgres import PostgresV2Store as _Store

        class _NB:
            def __getattr__(self, name):
                raise RuntimeError("no blob is read")

        parked = _Store(dsn, _NB()).parked_outbox(a.tenant, a.workspace)
        print(f"parked outbox events {len(parked)}" + ("".join(f"\n  PARKED   {p['kind']:24} id={p['id']}" for p in parked)))
        if a.command == "list":
            return 0
        if a.command == "replay-parked":
            from infra.worker import job_worker
            kind = a.kind.strip()
            if kind not in job_worker.OUTBOX_CLAIM_CONSUMERS and kind not in job_worker.OUTBOX_NO_CLAIM_CONSUMER:
                print(f"kind {kind!r} has no consumer in this worker's registry: nothing replayed", file=sys.stderr)
                return 2
            if len(a.reason.strip()) < 8:
                print("--reason (at least 8 characters) is required: it is written to the audit", file=sys.stderr)
                return 2
            n = len([p for p in parked if p["kind"] == kind])
            if not a.apply:
                print(f"dry run: {n} parked event(s) of kind {kind} would return to PENDING")
                return 0
            n = _Store(dsn, _NB()).replay_parked_outbox(a.tenant, a.workspace, kind, actor_id="operator", reason=a.reason.strip())
            print(f"replayed {n} parked event(s) of kind {kind}")
            return 0
        if not a.apply:
            print(f"dry run: {len(queued)} queued job(s) would be cancelled; running jobs are left to finish")
            return 0
        from infra.adapters.v2_postgres import PostgresV2Store, StoreConflict

        class _NoBlobs:
            def __getattr__(self, name):
                raise RuntimeError("no blob is read")

        store = PostgresV2Store(dsn, _NoBlobs())
        done = 0
        for jid, _kind, _status in queued:
            try:
                store.cancel_execution(a.tenant, a.workspace, jid, f"operator_cancelled: {a.reason.strip()}", queued_only=True)   # REL-02: never a leased job
                done += 1
            except StoreConflict:
                pass                                   # it started or finished meanwhile: left as it is
        print(f"cancelled {done} queued job(s)")
    except psycopg2.Error as exc:
        print(f"database error: {type(exc).__name__}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
