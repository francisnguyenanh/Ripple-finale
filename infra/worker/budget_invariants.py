"""One statement of what `max_provider_calls_per_job` and `max_retries` mean at run time, and the invariants that make the reservation an upper bound.

Run-time semantics (the ones every product caller enforces; see `docs/contracts/AI_CLOSED_LOOP_BUDGETS.md` section 3a):
  * `max_provider_calls_per_job` = the provider-call ceiling of ONE ATTEMPT (`ProviderCallBudget(limit)` is created per attempt).
  * a job may run `max_retries + 1` attempts (`claim.attempts > max_retries` ends it).
  * therefore the most provider calls one job can make is `max_provider_calls_per_job * (max_retries + 1)`.
  * the reservation taken when the job is created is the worst-case price of ONE call on the most expensive configured model, times that total.
The shared budget loader reads the same key as the TOTAL (`== max_retries + 1`): that is the special case of one call per attempt and is used only by the
single-call spike worker. A config with 1 call per attempt and 1 retry (the Pitch candidate) is consistent under the per-attempt reading (2 calls in all) and
cannot be written under the total reading without removing the retry, so the per-attempt reading is the contract.
"""
from __future__ import annotations

from decimal import Decimal

from infra.worker.budget_config import worst_case_call_usd


def attempts(config) -> int:
    return config.max_retries + 1


def max_total_provider_calls(config) -> int:
    return config.max_provider_calls_per_job * attempts(config)


def worst_call_usd(config) -> Decimal:
    return worst_case_call_usd(config)                                                      # R23: each role at its own output ceiling


def job_reservation_usd(config) -> Decimal:
    """Equal to `job_quota.compute_budget_reservation(config)[0]` (asserted by a test): the amount held at creation."""
    return worst_call_usd(config) * max_total_provider_calls(config)


def problems(config) -> list[str]:
    """Why this config cannot safely back an approval; empty when the reservation bounds one job's spend and fits both caps."""
    out = []
    if not config.fail_closed_on_missing_usage:
        out.append("fail_closed_on_missing_usage must be true: a call without usage would be priced at zero and escape the ledger")
    reserve = job_reservation_usd(config)
    if reserve > config.per_job_cap_usd:
        out.append(f"one job reserves {reserve} USD, above the per-job cap {config.per_job_cap_usd}")
    if reserve > config.total_cap_usd:
        out.append(f"one job reserves {reserve} USD, above the total cap {config.total_cap_usd}")
    for role, model in config.role_model_ids.items():
        if model not in config.model_prices:
            out.append(f"role {role} names model {model} that has no price")
    return out


def attempts_over_estimate(dsn: str, tenant_id: str, workspace_id: str) -> list[tuple[str, int]]:
    """Read-only: jobs with an attempt whose booked cost exceeds its own per-call worst-case estimate (the provider reported more tokens than the
    configured ceilings). Live acceptance requires this list to be EMPTY; a non-empty list is a finding, never silently clamped."""
    import psycopg2
    with psycopg2.connect(dsn) as cx, cx.cursor() as cur:
        cur.execute("""SELECT job_id, count(*) FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s
                         AND cost_estimate_usd IS NOT NULL AND cost_usd > cost_estimate_usd GROUP BY job_id ORDER BY job_id""",
                    (tenant_id, workspace_id))
        return [(r[0], int(r[1])) for r in cur.fetchall()]
