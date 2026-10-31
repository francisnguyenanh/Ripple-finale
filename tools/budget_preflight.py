"""Budget preflight before a deployment window (AI-03, 2026-10-30): the checks an operator runs on the production budget copy BEFORE arming.

    python -m tools.budget_preflight <budget.yaml> [--today YYYY-MM-DD] [--per-job-max 0.52]

Prints and checks: `budget_invariants.problems` (empty), the job reservation (<= the per-job cap), the request assistant's own bound, and the
price snapshot age (`price_snapshot_date` within `price_snapshot_max_age_days`). Exit 0 when every check passes; 2 when a check fails (stale prices,
a reservation above the cap, a problem); 3 when the file cannot be loaded. The runtime does not refuse to run on a stale snapshot (an operating
decision: AI must not switch itself off in the middle of a window); this preflight is the gate, run on the day of every window, and the price is
re-read from the official page before `price_snapshot_date` changes. No provider is called.
"""
from __future__ import annotations

import argparse
import datetime
import sys
from decimal import Decimal


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Budget preflight for one deployment window.")
    ap.add_argument("budget")
    ap.add_argument("--today", default=None, help="the day of the window (default: today)")
    ap.add_argument("--per-job-max", default=None, help="the approved per-job cap to compare against (default: the file's own)")
    a = ap.parse_args(argv)
    from infra.worker import budget_config, budget_invariants
    from infra.worker.job_quota import load_worker_budget_config
    try:
        cfg = load_worker_budget_config(a.budget)
    except Exception as exc:                                             # noqa: BLE001
        print(f"cannot load {a.budget}: {type(exc).__name__}", file=sys.stderr)
        return 3
    today = datetime.date.fromisoformat(a.today) if a.today else datetime.date.today()
    ok = True
    problems = budget_invariants.problems(cfg)
    print("problems:", problems or "none")
    ok &= not problems
    reserve = budget_invariants.job_reservation_usd(cfg)
    per_job = Decimal(a.per_job_max) if a.per_job_max else cfg.per_job_cap_usd
    print(f"job reservation {reserve} USD <= per-job cap {per_job}: {'ok' if reserve <= per_job else 'ABOVE'}")
    ok &= reserve <= per_job
    assistant = {r: str(budget_config.output_ceiling_for_role(cfg, r)) for r in ("request.assistant", "request.assistant.recover") if r in (cfg.role_model_ids or {})}
    print("assistant ceilings:", assistant or "not configured")
    status = budget_config.price_snapshot_status(cfg, today=today)
    print(f"price snapshot {cfg.price_snapshot_date} age {status.get('age_days')} days (max {status.get('max_age_days', getattr(cfg, 'price_snapshot_max_age_days', None) or 30)}): "
          f"{'STALE' if status['stale'] else 'fresh'}")
    ok &= not status["stale"]
    print("preflight:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
