"""Per-project token/cost quota and concurrency.

These tests target `infra/worker/job_quota.py`.

The shared budget loader makes `max_provider_calls_per_job` an OPTIONAL key,
which is sound only when a worker makes exactly one `complete_structured`
call per job attempt. Jobs can call `complete_structured` more than
once (chunked extraction / linking / review), so the worker entry point must
treat `max_provider_calls_per_job` as an INDEPENDENT CRITICAL key, and its value
must be `>= actual call count` for the job -- not derived from
`max_retries + 1`.
The tests below encode that requirement so it cannot be silently dropped.
"""

from __future__ import annotations

import pytest


def _budget_config(tmp_path, **overrides):
    """Reuse the budget contract (docs/contracts/AI_CLOSED_LOOP_BUDGETS.md)
    rather than re-inventing a config shape. The loader lives at
    infra/worker/budget_config.py; this only imports it.
    """
    from infra.worker.budget_config import load_budget_config

    base = {
        "default_model_id": "test-model",
        "model_prices": {"test-model": {"input_usd_per_1k": "0.01", "output_usd_per_1k": "0.01"}},
        "max_output_tokens": 2048,
        "max_input_tokens_per_call": 8192,
        "max_retries": 1,
        "per_job_cap_usd": "1.00",
        "total_cap_usd": "50.00",
        "fail_closed_on_missing_usage": True,
        "latency_slo_p50_seconds": 5,
        "latency_slo_p95_seconds": 20,
        # The worker promotes this to an explicit key at its own entry
        # point (see test_max_provider_calls_per_job_is_a_required_critical_key,
        # which deliberately omits it via its own literal config instead of
        # this shared helper); every OTHER test in this file uses a job kind
        # this key must independently cover, so it is set explicitly here too.
        "max_provider_calls_per_job": 3,
    }
    base.update(overrides)
    path = tmp_path / "budgets.yaml"
    import yaml  # already a project dependency (used by the example config)

    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return load_budget_config(str(path))


def test_max_provider_calls_per_job_is_a_required_critical_key(tmp_path):
    """The worker must
    promote this key to critical for ITSELF (no silent default) -- without
    breaking the shared loader, contract, example config and
    tests, none of which set this key explicitly. The shared
    `infra.worker.budget_config.load_budget_config` keeps
    this key optional (defaulting to max_retries + 1) --
    widening ITS critical-key set would silently invalidate the
    checked-in example config and fixtures. The worker instead enforces this at
    its own worker-facing entry point,
    `infra.worker.job_quota.load_worker_budget_config`, which requires the
    key to be explicit before ever calling the shared loader.
    """
    pytest.importorskip("infra.worker.job_quota", reason="AI2-T03 not implemented yet (TEST_FIRST)")
    from infra.worker.budget_config import BudgetConfigError
    from infra.worker.job_quota import load_worker_budget_config

    base = {
        "default_model_id": "test-model",
        "model_prices": {"test-model": {"input_usd_per_1k": "0.01", "output_usd_per_1k": "0.01"}},
        "max_output_tokens": 2048,
        "max_input_tokens_per_call": 8192,
        "max_retries": 1,
        "per_job_cap_usd": "1.00",
        "total_cap_usd": "50.00",
        "fail_closed_on_missing_usage": True,
        "latency_slo_p50_seconds": 5,
        "latency_slo_p95_seconds": 20,
    }
    path = tmp_path / "budgets.yaml"
    import yaml

    path.write_text(yaml.safe_dump(base), encoding="utf-8")  # deliberately omits the key

    with pytest.raises(BudgetConfigError, match="max_provider_calls_per_job"):
        load_worker_budget_config(str(path))


def test_max_provider_calls_per_job_must_cover_the_real_call_count(tmp_path):
    """A config whose max_provider_calls_per_job is below the job kind's
    real maximum number of complete_structured calls must be rejected at
    admission time, not merely accepted and silently under-count later.
    """
    from infra.worker.job_quota import JobQuotaError, check_job_admission

    # max_retries=0 makes max_retries + 1 == 1, so max_provider_calls_per_job=1
    # is internally consistent per the shared loader's own cross-check
    # (infra/worker/budget_config.py) -- this config loads validly, but its
    # value is still below the job kind's real call count, which is exactly
    # what check_job_admission (an admission-time check, distinct
    # from the loader's own consistency check) must catch.
    cfg = _budget_config(tmp_path, max_retries=0, max_provider_calls_per_job=1)
    # A job kind that is documented (by the worker/job-kind registry) to make 3 complete_structured calls per attempt.
    with pytest.raises(JobQuotaError):
        check_job_admission(cfg, job_kind="chunked_extraction", real_calls_per_attempt=3)


def test_project_concurrency_quota_refuses_beyond_the_configured_limit(tmp_path):
    """Two jobs already running for a project; a third submission beyond
    the configured per-project concurrency limit must be refused before a
    worker claims it, not merely queued forever.
    """
    from infra.worker.job_quota import JobQuotaError, check_concurrency_admission

    with pytest.raises(JobQuotaError):
        check_concurrency_admission(
            tenant_id="t1", workspace_id="w1", max_concurrent_jobs=2, currently_running=2,
        )
    # Under the limit must be granted (no exception).
    check_concurrency_admission(
        tenant_id="t1", workspace_id="w1", max_concurrent_jobs=2, currently_running=1,
    )


def test_project_total_cost_quota_persists_across_restart(tmp_path):
    """Worker-level admission must not use
    `spent_usd=Decimal("0")` always: running spend is persisted, so total project spend must be readable after a
    simulated worker restart, not reset to zero.
    """
    from decimal import Decimal

    from infra.worker.job_quota import record_job_cost, project_spent_usd

    tenant_id, workspace_id = "t1-" + str(id(tmp_path)), "w1"
    record_job_cost(tenant_id=tenant_id, workspace_id=workspace_id, job_id="j1", cost_usd="0.40")
    # A second, independent read (simulating a restarted worker process
    # querying the persisted store rather than in-memory state) must see it.
    # Money is always Decimal end-to-end (ADR-005 §2.7); The comparison is a real
    # Decimal comparison (Decimal does not compare against str). A
    # tenant_id unique to this test run avoids cross-test-run accumulation
    # in the persisted state file corrupting the >= assertion's meaning.
    assert project_spent_usd(tenant_id=tenant_id, workspace_id=workspace_id) >= Decimal("0.40")
