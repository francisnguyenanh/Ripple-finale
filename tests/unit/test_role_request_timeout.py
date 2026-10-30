"""A role's own request deadline (R11, owner decision 2026-10-28: request.assistant 45 s, no other role changed). Pure: config validation, the timeout sent with the
call of that role and of another role, and the worker lease that still covers the shared timeout. The deadline behaviour of a call through the real route and worker is
in tests/final_loop/test_request_assistant_jobs.py."""
from __future__ import annotations

import pytest
import yaml

from infra.worker import job_worker
from infra.worker.budget_config import BudgetConfigError, role_request_timeout

BASE = {
    "per_job_cap_usd": "0.16", "total_cap_usd": "10.00", "max_output_tokens": 12288, "max_input_tokens_per_call": 24000, "max_retries": 0,
    "max_provider_calls_per_job": 4, "default_model_id": "gemini-3.5-flash-lite", "fail_closed_on_missing_usage": True,
    "latency_slo_p50_seconds": 30, "latency_slo_p95_seconds": 90, "enabled_roles": "all",
    "model_prices": {"gemini-3.5-flash-lite": {"input_usd_per_1k": "0.0003", "output_usd_per_1k": "0.0025"}},
    "role_model_ids": {"request.assistant": "gemini-3.5-flash-lite", "request.qa": "gemini-3.5-flash-lite"},
    "request_timeout_seconds": 120, "role_request_timeouts": {"request.assistant": 45},
}


def load(tmp_path, monkeypatch, **changes):
    raw = {k: v for k, v in {**BASE, **changes}.items() if v is not None}
    path = tmp_path / "b.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    from infra.worker.job_quota import load_worker_budget_config
    return load_worker_budget_config(str(path))


def test_only_the_listed_role_gets_its_own_deadline_and_the_lease_still_covers_the_shared_timeout(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch)
    assert role_request_timeout(c, "request.assistant") == 45.0 and role_request_timeout(c, "knowledge.qa") is None and role_request_timeout(c, None) is None
    assert job_worker.effective_lease_seconds(c, 30) == 135                                          # 120 + 15: unchanged by the shorter role deadline
    assert role_request_timeout(load(tmp_path, monkeypatch, role_request_timeouts=None), "request.assistant") is None   # absent key: the earlier behaviour


@pytest.mark.parametrize("changes, message", [
    ({"role_request_timeouts": {"request.assistant": 150}}, "must not exceed"),                     # past the shared timeout the lease would not cover it
    ({"request_timeout_seconds": None}, "needs request_timeout_seconds"),
    ({"role_request_timeouts": {"request.assistant": 0}}, "finite number of seconds > 0"),
    ({"role_request_timeouts": {"request.assistant": "45"}}, "finite number of seconds > 0"),
    ({"role_request_timeouts": {"request.assistant": True}}, "finite number of seconds > 0"),
    ({"role_request_timeouts": ["request.assistant"]}, "must be a mapping"),
])
def test_a_role_deadline_is_refused_when_it_is_not_a_positive_number_within_the_shared_timeout(tmp_path, monkeypatch, changes, message):
    with pytest.raises(BudgetConfigError, match=message):
        load(tmp_path, monkeypatch, **changes)
