"""Connection envelope check for a deployment (REL-04, 2026-10-30; runbook FINAL_PRODUCTION_WINDOWS.md section 1a).

    python -m tools.db_envelope --max-connections 25 --reserved 3 --web-instances 3 --web-pool 4 --worker-executions 2 --worker-pool 3 \
        [--old-web-instances 3] [--migration 1] [--operators 2] [--margin 2] [--worker-executions-verified <evidence ref>]

Formula (every term counted, including the rollout overlap, maintenance and smoke):
    old_web x web_pool + new_web x web_pool + worker_executions x worker_pool + migration + operators + margin  <  max_connections - reserved
`worker_executions` is the number of worker EXECUTIONS that may run at once (a Cloud Run job's tasks/parallelism does not bound executions; the
in-database execution lease of infra/worker/execution_lease.py bounds it to one when enabled). Unless `--worker-executions-verified` names the
evidence, the value prints as UNVERIFIED and the exit code stays non-zero: the envelope is not accepted on an assumption.
Exit 0 when the inequality holds and the worker bound is verified; 2 when it does not hold; 3 when the worker bound is unverified.
Traffic at 0 % on the old revision is not proof that it holds no connections: on the day, read `pg_stat_activity` per application and the
platform's instance counts (`--observed-connections N` compares the observed total against the envelope).
"""
from __future__ import annotations

import argparse
import sys


def budget(a) -> dict:
    used = (a.old_web_instances * a.web_pool + a.web_instances * a.web_pool + a.worker_executions * a.worker_pool
            + a.migration + a.operators + a.margin)
    limit = a.max_connections - a.reserved
    return {"old_web": a.old_web_instances * a.web_pool, "new_web": a.web_instances * a.web_pool, "worker": a.worker_executions * a.worker_pool,
            "migration": a.migration, "operators": a.operators, "margin": a.margin, "used": used, "limit": limit, "holds": used < limit}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Connection envelope of a deployment (rollout overlap included).")
    ap.add_argument("--max-connections", type=int, required=True)
    ap.add_argument("--reserved", type=int, default=3)
    ap.add_argument("--web-instances", type=int, required=True, help="maximum instances of the NEW web revision")
    ap.add_argument("--old-web-instances", type=int, default=0, help="instances of the OLD web revision still alive during the rollout (0 only after scale-to-zero is observed)")
    ap.add_argument("--web-pool", type=int, required=True, help="RIPPLE_DB_POOL_MAX of a web process")
    ap.add_argument("--worker-executions", type=int, required=True, help="worker executions that may run at once")
    ap.add_argument("--worker-pool", type=int, required=True, help="RIPPLE_DB_POOL_MAX of a worker process")
    ap.add_argument("--worker-executions-verified", default="", help="evidence that bounds worker executions (e.g. the execution lease test run id)")
    ap.add_argument("--migration", type=int, default=1)
    ap.add_argument("--operators", type=int, default=2)
    ap.add_argument("--margin", type=int, default=2)
    ap.add_argument("--observed-connections", type=int, default=None, help="total connections read from pg_stat_activity on the day")
    a = ap.parse_args(argv)
    b = budget(a)
    verified = bool(a.worker_executions_verified.strip())
    print(f"old web {b['old_web']} + new web {b['new_web']} + worker {b['worker']} ({'verified: ' + a.worker_executions_verified if verified else 'UNVERIFIED'})"
          f" + migration {b['migration']} + operators {b['operators']} + margin {b['margin']} = {b['used']} < {b['limit']} ({a.max_connections} - {a.reserved}): "
          f"{'HOLDS' if b['holds'] else 'DOES NOT HOLD'}")
    if a.observed_connections is not None:
        print(f"observed {a.observed_connections} connections: {'within' if a.observed_connections < b['limit'] else 'ABOVE'} the limit")
        if a.observed_connections >= b["limit"]:
            return 2
    if not b["holds"]:
        return 2
    if not verified:
        print("worker executions bound UNVERIFIED: not accepted", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
