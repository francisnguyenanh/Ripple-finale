"""Per-project token/cost quota and concurrency admission.

Builds on the budget contract
(`docs/contracts/AI_CLOSED_LOOP_BUDGETS.md`, `infra/worker/budget_config.py`)
without re-inventing a config shape. This module adds three things the shared
budget loader leaves open:

1. `max_provider_calls_per_job` becomes an INDEPENDENT, worker-enforced
   critical requirement (`check_job_admission`) once a job kind can make
   more than one `complete_structured` call per attempt. This is enforced
   HERE, at job admission, rather than by widening
   `infra/worker/budget_config.py::_CRITICAL_KEYS` (that loader is shared
   with the contract and example config, none of which set this key
   explicitly; making it globally critical there would silently break every
   one of those, which the loader's own docstring flags as the reason it
   stays optional/defaulted at that layer). Workers that submit jobs whose
   kind can make more than one provider call per attempt MUST load their
   config through `load_worker_budget_config` (below), which requires the
   key to be explicit in the YAML for exactly that reason, and MUST
   additionally call `check_job_admission` before claiming/running such a job.

   The shared loader's own internal consistency cross-check
   (`infra/worker/budget_config.py`'s `_validate`,
   "max_provider_calls_per_job must equal max_retries + 1") is sound for a
   worker that makes exactly one provider call per attempt, but wrong once a
   job kind legitimately needs MORE calls than retries (e.g. 3 calls, 1
   retry). `load_worker_budget_config` therefore decouples from that
   cross-check entirely (the coupling is fixed in this narrower wrapper
   rather than by widening the shared loader) by validating every OTHER key
   through the shared, UNCHANGED `_validate`/`_apply_env_overrides` helpers
   with this one key removed from the raw mapping first (so the shared
   validator computes its own default internally and never runs the equality
   check against our real, independent value), then overriding the returned
   `BudgetConfig.max_provider_calls_per_job` with the real, explicitly
   configured ceiling. `infra/worker/budget_config.py` itself is not edited
   by this module.
2. Per-project concurrency admission (`check_concurrency_admission`), wired
   at job-creation time (`infra/v2_policy_routes.py::create_job`) via the
   optional `max_concurrent_jobs_per_workspace` sidecar key
   (`load_concurrency_limit`, below) -- deliberately NOT added to the shared
   `BudgetConfig` dataclass (same reasoning as #1: this is an opt-in
   concurrency ceiling, not part of the shared budget contract).
3. Persisted (restart-surviving), idempotent running total spend
   (`record_job_cost`/`project_spent_usd`), closing the budget contract §9
   open dependency ("spent_usd is always Decimal('0')").

   These two functions read/write a PostgreSQL table
   (`v2_job_cost_ledger`), not an OS-temp JSON file, and
   `record_job_cost` is an idempotent upsert keyed by
   `(tenant_id, workspace_id, job_id)` -- recording the same `job_id`'s cost
   twice is a no-op, never an accumulating sum. The DSN is resolved from
   `RIPPLE_V2_DSN` (the same production Postgres `PostgresV2Store` already
   uses) or, for dev-loop/test convenience, `AI2_T03_TEST_DSN` /
   `AI1_T03_TEST_DSN` (a disposable-database convention).

`check_job_admission` and `check_concurrency_admission` are CALLED from the
real product path -- `infra/v2_policy_routes.py::create_job` (job-creation-time
admission) and `infra/worker/job_worker.py::claim_and_admit_one` (claim-time
re-admission, defense in depth against a job created before quota was
configured, or a config change between creation and claim).
"""

from __future__ import annotations

import dataclasses
import os
from decimal import Decimal

import yaml

from infra.adapters.v2_postgres import PROJECT_SPEND_SQL, project_spend_params
from infra.worker.budget_config import (
    BudgetConfig, BudgetConfigError, BudgetExceeded,
    actual_call_cost_usd, check_admission, worst_case_call_usd, worst_case_model_id,
    _apply_env_overrides as _shared_apply_env_overrides,
    _validate as _shared_validate,
)


class JobQuotaError(Exception):
    """Raised when a job/project would exceed a configured quota or
    concurrency limit -- refused before a worker claims or runs the job,
    same "refuse before claim" convention as `BudgetExceeded` (contract §5).
    """


#: Registry of job kinds that make more than one `complete_structured` call
#: per attempt. The `change`/`restore` job kinds make exactly one call
#: each; kinds like chunked, multi-stage extraction call the provider more
#: than once per attempt. A kind absent from this table defaults to 1 call.
JOB_KIND_REAL_CALLS_PER_ATTEMPT: dict[str, int] = {
    # RequestAssistantExecutor (R17): the answer call plus at most one supplementary call for the parts the first pass left open.
    "request.qa": 2,
    "change": 1,
    "restore": 1,
    "analysis": 1,
    # ExtractRulesExecutor makes one provider call per attempt
    # (single prompt over one artifact's pinned bytes).
    "extraction": 1,
    # GapAnswerExecutor, one call per attempt over the whole authorized scope.
    "gap.answer": 1,
    # KnowledgeQaExecutor, one call per attempt (business question over a context pack).
    "knowledge.qa": 1,
    # ChangeUnderstandExecutor, one call per attempt (business change request -> validated operations).
    "change.understand": 1,
    # ImpactDependencyExecutor, one call per attempt (AI-proposed dependencies of a confirmed change; always PROPOSED).
    "change.impact_deps": 1,
    # GapProposalExecutor, one call per attempt (gaps/relations a model suggests over one knowledge version; always PROPOSED).
    "knowledge.gap_proposals": 1,
    # GapAnswerInterpretExecutor, one call per attempt (a free-text gap answer read into the structured form; the person confirms separately).
    "gap.answer_interpret": 1,
    # DraftGuidanceExecutor, one call per attempt (model guidance over the server-built draft of a confirmed change; downgrade-only).
    "change.draft_guidance": 1,
    # ChangeDraftExecutor, one call per attempt (zero when the request needs confirmation first).
    "change.draft": 1,
    # IntentInterpretExecutor, one call per attempt.
    "intent.interpret": 1,
    # ExplainExecutor, one call per attempt (wording of verified facts).
    "explain": 1,
    # SimAnalysisExecutor, one call per attempt (role simulation.analyze, pinned separately; absent role fails closed).
    "sim.analysis": 1,
    # ImpactJudgeExecutor, exactly one call per attempt.
    "impact.judge": 1,
    "chunked_extraction": 3,
    # Import ingest verifies blobs; no model call.
    "source_ingest": 0,
    # DocumentGenerateExecutor: deterministic rendering of an accepted snapshot; no model call.
    "document.generate": 0,
    # Extension ext/1: deterministic code-change jobs on a disposable clone / mock target (0 provider calls).
    "code.candidate": 0, "code.validate": 0, "code.promote": 0, "code.verify": 0, "code.rollback": 0,
    # Extension ext/1 FEX-01: local repository read into a source refresh revision (0 provider calls).
    "git.sync": 0,
    # Extension ext/1 FEX-01/05: remote fetch and pull request (0 provider calls).
    "git.fetch": 0, "code.publish": 0,
    # BusinessSourceApplyExecutor: approved scalar edits written into a source revision; no model call.
    "business.source_apply": 0,
    # Deterministic post-publish stages, no model call.
    "verification": 0,
    "km_rebuild": 0,
}


def _read_raw_yaml(path: str | None) -> tuple[dict, str]:
    """Same path resolution as the shared loader (env wins over argument);
    returns the raw parsed mapping and the effective path used, or raises
    `BudgetConfigError` -- identical error surface as `load_budget_config`."""
    env_path = os.environ.get("RIPPLE_BUDGET_CONFIG_PATH")
    effective_path = env_path if env_path else path
    if not effective_path:
        raise BudgetConfigError(
            "no config path given (neither an argument nor RIPPLE_BUDGET_CONFIG_PATH)"
        )
    try:
        with open(effective_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f.read())
    except OSError as exc:
        raise BudgetConfigError(f"cannot read budget config at {effective_path!r}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise BudgetConfigError(f"budget config at {effective_path!r} is not valid YAML: {exc}") from exc
    return raw, effective_path


def load_worker_budget_config(path: str | None = None) -> BudgetConfig:
    """Load a worker's budget config: every key the shared
    `BudgetConfig` already validates, PLUS `max_provider_calls_per_job` made
    critical (must be explicit in the YAML) AND independent of `max_retries`
    (never forced to equal `max_retries + 1`).

    The shared loader/contract/example config are untouched by this
    function -- it is a stricter, decoupled wrapper a worker opts into, not a
    change to the shared critical-key set or its consistency cross-check.

    The shared loader's `_validate`
    rejects any `max_provider_calls_per_job` that is not exactly
    `max_retries + 1` -- correct for a single-call-per-attempt worker,
    wrong for a job kind that legitimately needs MORE calls than
    retries (3 calls, 1 retry). This function validates every key OTHER than
    `max_provider_calls_per_job` through the shared, unmodified `_validate`/
    `_apply_env_overrides` (with that one key removed from the raw mapping
    first, so the shared validator computes its own unused default instead
    of running the equality cross-check against our real value), then
    overrides the returned config's `max_provider_calls_per_job` with the
    real, independently-validated ceiling from the YAML.
    """
    raw, effective_path = _read_raw_yaml(path)

    if not isinstance(raw, dict) or "max_provider_calls_per_job" not in raw:
        raise BudgetConfigError(
            "max_provider_calls_per_job: missing (critical key for an AI2-T03 worker -- a "
            "job kind here may make more than one complete_structured call per attempt, so "
            "this key can no longer be silently defaulted from max_retries + 1)"
        )

    real_max_calls = raw["max_provider_calls_per_job"]
    if not isinstance(real_max_calls, int) or isinstance(real_max_calls, bool) or real_max_calls <= 0:
        raise BudgetConfigError(
            f"max_provider_calls_per_job: must be a positive int, got {real_max_calls!r} "
            f"({type(real_max_calls).__name__})"
        )

    # Decoupled validation: the shared validator never sees this key, so it
    # cannot run its max_retries+1 equality cross-check against our real
    # value (that cross-check belongs to the shared loader and stays fully intact for
    # direct callers of `load_budget_config`).
    sanitized = {k: v for k, v in raw.items() if k != "max_provider_calls_per_job"}
    config, errors = _shared_validate(sanitized)
    if errors:
        raise BudgetConfigError(
            f"budget config at {effective_path!r} is invalid ({len(errors)} problem(s)): "
            + "; ".join(errors)
        )
    config = _shared_apply_env_overrides(config)
    return dataclasses.replace(config, max_provider_calls_per_job=real_max_calls)


#: The shared budget/call-limit ceiling for one invalidation job's
#: dependency traversal + reanalysis scheduling. Reuses this module's existing optional-sidecar-key
#: pattern (`load_concurrency_limit`, above): never raises, defaults when the
#: key/file is absent, so no pre-existing caller of `load_worker_budget_config`
#: is affected by this key's mere existence.
REANALYSIS_JOB_BUDGET_DEFAULT = 20


def load_reanalysis_budget(path: str | None = None) -> int:
    """Optional sidecar key, `max_reanalysis_jobs_per_decision`, read from the
    same YAML file `load_concurrency_limit` reads. Bounds how many reanalysis
    jobs ONE invalidation job may schedule for a single decision event -- the
    non-convergence ceiling (an explicit human issue is raised
    once exhausted). Never raises; falls back to
    `REANALYSIS_JOB_BUDGET_DEFAULT` when unset/invalid, same fail-safe shape
    as `load_concurrency_limit`."""
    try:
        raw, _effective_path = _read_raw_yaml(path)
    except BudgetConfigError:
        return REANALYSIS_JOB_BUDGET_DEFAULT
    if not isinstance(raw, dict):
        return REANALYSIS_JOB_BUDGET_DEFAULT
    value = raw.get("max_reanalysis_jobs_per_decision")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return REANALYSIS_JOB_BUDGET_DEFAULT
    return value


def provider_attempt_limit() -> int:
    """Provider attempts one AI job may make: the approved retry limit + 1, exactly as the worker enforces it (`job_worker`: `provider_attempt_limit_reached`).
    Without a budget config the worker's own default applies."""
    import os
    if os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        return load_worker_budget_config().max_retries + 1
    from infra.worker.job_worker import _DEFAULT_MAX_RETRIES
    return _DEFAULT_MAX_RETRIES + 1


def load_concurrency_limit(path: str | None = None) -> int | None:
    """Optional sidecar key, `max_concurrent_jobs_per_workspace`, read from the
    same YAML file. Not part of the shared `BudgetConfig` (an
    opt-in ceiling). Returns `None` (no concurrency ceiling enforced)
    when the file/key is absent or the value is not a positive int -- this
    function never raises, since an operator who has not configured a
    concurrency ceiling yet must not have every job creation start failing.
    """
    try:
        raw, _effective_path = _read_raw_yaml(path)
    except BudgetConfigError:
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("max_concurrent_jobs_per_workspace")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


def check_job_admission(config, *, job_kind: str, real_calls_per_attempt: int | None = None) -> None:
    """Refuse admission unless `config.max_provider_calls_per_job` covers the
    job kind's real maximum number of provider calls per attempt. Never
    derives the ceiling from `max_retries + 1` implicitly here -- the
    configured value is checked as an explicit, independent bound.
    """
    if real_calls_per_attempt is None:
        real_calls_per_attempt = JOB_KIND_REAL_CALLS_PER_ATTEMPT.get(job_kind, 1)
    if real_calls_per_attempt > config.max_provider_calls_per_job:
        raise JobQuotaError(
            f"job kind {job_kind!r} can make {real_calls_per_attempt} provider calls per "
            f"attempt, which exceeds max_provider_calls_per_job="
            f"{config.max_provider_calls_per_job}"
        )


def compute_budget_reservation(config) -> tuple[Decimal, Decimal] | None:
    """Returns
    `(estimate_usd, total_cap_usd)` for `PostgresV2Store.create_job`'s own
    `budget_reservation` parameter, so the admission check happens INSIDE
    `create_job`'s transaction/workspace-lock rather than in a separate,
    earlier transaction (avoiding a separate-transaction gap between reservation and
    admission: two concurrent submits
    could both read a stale spent total and both pass). Returns `None` when
    the config has no total cap configured (nothing to reserve against).
    """
    if config.total_cap_usd is None:
        return None
    # The call ceiling is per ATTEMPT. A job may consume max_retries + 1
    # attempts without returning through HTTP admission, so the reservation
    # must cover the whole bounded execution, including its retry window.
    estimate = (worst_case_call_usd(config)                                                 # R23: each role at its own output ceiling
                * config.max_provider_calls_per_job * (config.max_retries + 1))
    check_admission(config, scope="per_job", spent_usd=Decimal("0"),
                    estimate_usd=estimate)
    return (estimate, config.total_cap_usd)


def reservation_for_system_job(kind: str):
    """Budget reservation for a job the SYSTEM creates (re-extraction after an apply, reanalysis after a decision). `(estimate, total cap)` for
    `create_job(budget_reservation=...)` when a budget config is in force AND the kind calls a provider; `None` otherwise (no config, or a kind that
    makes no provider call, so reserving would only block unrelated work). Without this, a wave of N system-created extraction jobs spent against the
    cap without ever being checked against it (only user-created jobs reserved)."""
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        return None
    from infra.worker.sandbox_hold import PROVIDER_JOB_KINDS
    if kind not in PROVIDER_JOB_KINDS:
        return None
    return compute_budget_reservation(load_worker_budget_config())


def enforce_project_budget(config, *, tenant_id: str, workspace_id: str) -> Decimal:
    """Refuse admission if committing to this job would risk
    exceeding the project's TOTAL cost cap, given the accumulated spend
    already durably recorded in `v2_job_cost_ledger`.

    Admission happens before a live provider response can be known, so this
    reuses the shared "missing usage" fail-closed pricing
    (`budget_config.actual_call_cost_usd` with `input_tokens=None,
    output_tokens=None`) as the worst-case cost estimate for this job: when
    `config.fail_closed_on_missing_usage` is True, that is
    `max_input_tokens_per_call` + `max_output_tokens` priced at
    `config.default_model_id`'s rate; when the operator has explicitly chosen
    fail-open, it is `Decimal('0')` -- the same explicit contract choice
    the shared budget contract defines.

    Raises `infra.worker.budget_config.BudgetExceeded` (never `JobQuotaError`
    -- a distinct, already-defined exception type callers must handle
    separately, same principle as `BudgetConfigError` vs `JobQuotaError`).
    Returns the worst-case estimate used, so a caller does not have to
    recompute it.
    """
    estimate = worst_case_call_usd(config)
    spent = project_spent_usd(tenant_id=tenant_id, workspace_id=workspace_id)
    check_admission(config, scope="total", spent_usd=spent, estimate_usd=estimate)
    return estimate


def check_concurrency_admission(*, tenant_id: str, workspace_id: str,
                                max_concurrent_jobs: int, currently_running: int) -> None:
    """Refuse a new job before it is claimed/queued if the project is
    already at its configured concurrency ceiling."""
    if not isinstance(max_concurrent_jobs, int) or max_concurrent_jobs <= 0:
        raise ValueError("max_concurrent_jobs must be a positive int")
    if currently_running >= max_concurrent_jobs:
        raise JobQuotaError(
            f"{tenant_id}/{workspace_id}: {currently_running} jobs already running, "
            f"at or above the configured concurrency limit {max_concurrent_jobs}"
        )


# --------------------------------------------------------------------------------- persisted spend
#
# A PostgreSQL table, not an OS-temp
# JSON file protected only by a process-local `threading.Lock` -- the prior
# shape could not be shared across independent worker processes/instances
# and was not idempotent (re-recording the same job_id's cost doubled the
# total). `v2_job_cost_ledger` has ONE row per (tenant_id, workspace_id,
# job_id); `record_job_cost` is an idempotent upsert (`ON CONFLICT ... DO
# NOTHING`) -- recording the same job_id's cost twice is a no-op, never an
# accumulating sum. Money is always Decimal end-to-end (ADR-005 §2.7): the
# column is SQL `NUMERIC`, never a float/JSON-number round-trip, and
# psycopg2 adapts NUMERIC to Python `Decimal` natively.

_LEDGER_TABLE_SQL = """CREATE TABLE IF NOT EXISTS v2_job_cost_ledger (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    cost_usd NUMERIC NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id, job_id)
)"""


def _ledger_dsn() -> str | None:
    """Same production DSN `PostgresV2Store` uses (`RIPPLE_V2_DSN`), falling
    back to the disposable-test-database env vars used for dev-loop/CI runs (`AI2_T03_TEST_DSN`, then
    `AI1_T03_TEST_DSN`) -- the same convention
    applied for `PostgresV2Store` itself, so the ledger lives in the exact
    same disposable database as the jobs it accounts for.
    """
    return (os.environ.get("RIPPLE_V2_DSN")
            or os.environ.get("AI2_T03_TEST_DSN")
            or os.environ.get("AI1_T03_TEST_DSN"))


def _ledger_connect():
    dsn = _ledger_dsn()
    if not dsn:
        raise JobQuotaError(
            "no PostgreSQL DSN configured for the durable job-cost ledger "
            "(set RIPPLE_V2_DSN, or AI2_T03_TEST_DSN/AI1_T03_TEST_DSN for a "
            "disposable test database)"
        )
    import psycopg2  # local import: mirrors infra/adapters/v2_postgres.py's own lazy driver use

    return psycopg2.connect(dsn)


def _execute_project_sum(cur, tenant_id: str, workspace_id: str) -> None:
    """Round 6: PROJECT-scoped sum (every workspace of this workspace's
    project), the same definition `PostgresV2Store.create_job`'s reservation
    check uses. On a database where the v2 schema has not been migrated yet
    (the ledger's documented standalone use), there is no project mapping
    at all, so the sum is necessarily the workspace's own rows."""
    cur.execute("SELECT to_regclass('v2_workspaces') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute(PROJECT_SPEND_SQL, project_spend_params(tenant_id, workspace_id))
    else:
        cur.execute("""SELECT COALESCE(SUM(cost_usd), 0) FROM v2_job_cost_ledger
                       WHERE tenant_id=%s AND workspace_id=%s""", (tenant_id, workspace_id))


def record_job_cost(*, tenant_id: str, workspace_id: str, job_id: str,
                    cost_usd: str | Decimal) -> Decimal:
    """Idempotently record `job_id`'s cost against the project's running
    total. Recording the SAME `job_id` a second time (any cost value) is a
    no-op -- the first recorded cost for a job_id sticks; it is never added
    again. Returns the project's new running total (all jobs recorded so
    far), always a `Decimal`.
    """
    amount = cost_usd if isinstance(cost_usd, Decimal) else Decimal(str(cost_usd))
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"cost_usd must be a finite amount >= 0, got {cost_usd!r}")
    conn = _ledger_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(_LEDGER_TABLE_SQL)
                cur.execute(
                    """INSERT INTO v2_job_cost_ledger
                        (tenant_id, workspace_id, job_id, cost_usd)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (tenant_id, workspace_id, job_id) DO NOTHING""",
                    (tenant_id, workspace_id, job_id, str(amount)),
                )
                _execute_project_sum(cur, tenant_id, workspace_id)
                total = cur.fetchone()[0]
        return Decimal(total)
    finally:
        conn.close()


def project_spent_usd(*, tenant_id: str, workspace_id: str) -> Decimal:
    """The persisted running total spend for this project (sum of every
    distinct job_id's recorded cost), `Decimal("0")` if nothing has ever been
    recorded. Reads the ledger table fresh every call -- no in-memory cache
    to go stale across a worker restart or a second, independent worker
    process/instance.
    """
    conn = _ledger_connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(_LEDGER_TABLE_SQL)
                _execute_project_sum(cur, tenant_id, workspace_id)
                total = cur.fetchone()[0]
        return Decimal(total)
    finally:
        conn.close()
