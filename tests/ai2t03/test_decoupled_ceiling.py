"""The provider-call ceiling is decoupled from the retry count (example
reproduced directly).

Also regression-checks that the coupled-default behavior on the SHARED `infra.worker.budget_config.load_budget_config`
is unchanged -- this suite must not imply the shared contract
itself changed.
"""

from __future__ import annotations

import yaml
import pytest

from infra.worker.budget_config import BudgetConfigError, load_budget_config
from infra.worker.job_quota import JobQuotaError, check_job_admission, load_worker_budget_config


def _write_config(tmp_path, **overrides):
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
    base.update(overrides)
    path = tmp_path / "budgets.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return str(path)


def test_reviewer_example_config_no_longer_raises_budget_config_error(tmp_path):
    """The original repro: max_retries=1, max_provider_calls_per_job=3
    (a three-call job with one retry). The bug: `load_worker_budget_config`
    delegated the whole file to the shared loader, whose own equality
    cross-check (`max_provider_calls_per_job == max_retries + 1`) rejected
    this as inconsistent (3 != 2), raising BudgetConfigError before admission
    was ever reached."""
    path = _write_config(tmp_path, max_retries=1, max_provider_calls_per_job=3)

    config = load_worker_budget_config(path)  # must NOT raise

    assert config.max_retries == 1
    assert config.max_provider_calls_per_job == 3, (
        "the real, independently-configured ceiling must be preserved, not "
        "silently coerced back to max_retries + 1 (2)"
    )


def test_decoupled_ceiling_accepts_within_and_rejects_over(tmp_path):
    """Both directions of the decoupling, using the exact
    example config: a job kind whose real per-attempt call count is <= 3
    must be admitted; one whose real call count is 4 (over the ceiling, even
    though 4 == max_retries(1) + 3, i.e. clearly not derived from
    max_retries + 1 either) must be refused."""
    path = _write_config(tmp_path, max_retries=1, max_provider_calls_per_job=3)
    config = load_worker_budget_config(path)

    # Within ceiling: 1, 2, and exactly 3 must all be admitted.
    for real_calls in (1, 2, 3):
        check_job_admission(config, job_kind="chunked_extraction", real_calls_per_attempt=real_calls)

    # Over ceiling: 4 must be refused.
    with pytest.raises(JobQuotaError):
        check_job_admission(config, job_kind="chunked_extraction", real_calls_per_attempt=4)


def test_ai0_t03_shared_loader_coupled_default_behavior_is_unchanged(tmp_path):
    """Regression: the shared `load_budget_config` must
    still behave exactly as its own tests expect --
    `max_provider_calls_per_job` stays OPTIONAL there, defaulting to
    `max_retries + 1` when absent, and its own consistency cross-check still
    rejects an explicit mismatched value. The decoupling lives
    entirely in the `load_worker_budget_config` wrapper and is
    not a change to the shared loader's contract.
    """
    dir_no_key = tmp_path / "no_key"
    dir_no_key.mkdir()
    dir_mismatch = tmp_path / "mismatch"
    dir_mismatch.mkdir()
    dir_consistent = tmp_path / "consistent"
    dir_consistent.mkdir()

    # (a) Key absent entirely -> the shared loader computes its own default
    # (max_retries + 1), unchanged.
    path_no_key = _write_config(dir_no_key, max_retries=2)
    config = load_budget_config(path_no_key)
    assert config.max_provider_calls_per_job == 3  # max_retries(2) + 1, the coupled default

    # (b) Key present but inconsistent with max_retries + 1 -> the shared
    # loader's own cross-check must still reject it (the shared
    # loader's behavior for ITS OWN callers, unaffected by the decoupled
    # wrapper).
    path_mismatch = _write_config(dir_mismatch, max_retries=1, max_provider_calls_per_job=3)
    with pytest.raises(BudgetConfigError, match="max_provider_calls_per_job"):
        load_budget_config(path_mismatch)

    # (c) Key present and consistent (== max_retries + 1) -> still accepted,
    # unchanged.
    path_consistent = _write_config(dir_consistent, max_retries=1, max_provider_calls_per_job=2)
    config_consistent = load_budget_config(path_consistent)
    assert config_consistent.max_provider_calls_per_job == 2
