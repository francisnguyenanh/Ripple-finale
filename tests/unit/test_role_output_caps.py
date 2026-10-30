"""R23: per-role output caps in the budget config. Pure (no DB, no provider). Expected amounts are computed by hand from the prices in each config."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from infra.worker import budget_invariants, job_quota
from infra.worker.budget_config import (BudgetConfigError, actual_call_cost_usd, generation_cap_for_role, output_ceiling_for_role, worst_case_call_usd,
                                        worst_case_model_id)

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = ROOT / "config" / "ai_closed_loop" / "final_budgets.production.example.yaml"
BASE = """default_model_id: gemini-3.5-flash-lite
role_model_ids:
  request.assistant: gemini-3.8-flash
  request.assistant.recover: gemini-3.8-flash
  knowledge.gap_proposals: gemini-3.8-flash
model_prices:
  gemini-3.5-flash-lite: {input_usd_per_1k: "0.0003", output_usd_per_1k: "0.0025"}
  gemini-3.8-flash: {input_usd_per_1k: "0.0015", output_usd_per_1k: "0.0075"}
  gemini-3.8-flash@us: {input_usd_per_1k: "0.00165", output_usd_per_1k: "0.00825"}
role_locations:
  request.assistant.recover: us
per_job_cap_usd: "0.52"
total_cap_usd: "10.00"
max_input_tokens_per_call: 24000
max_output_tokens: 12288
generation_max_output_tokens: 6144
max_retries: 0
max_provider_calls_per_job: 4
fail_closed_on_missing_usage: true
latency_slo_p50_seconds: 30
latency_slo_p95_seconds: 90
enabled_roles: all
"""
CAPS = """role_max_output_tokens:
  request.assistant: 4096
  request.assistant.recover: 4096
role_generation_max_output_tokens:
  request.assistant: 2048
  request.assistant.recover: 2048
"""


def load(tmp_path, text):
    p = tmp_path / "b.yaml"
    p.write_text(text, encoding="utf-8")
    return job_quota.load_worker_budget_config(str(p))


def test_the_assistant_roles_get_their_own_caps_and_every_other_role_keeps_the_global_ones(tmp_path):
    c = load(tmp_path, BASE + CAPS)
    for role in ("request.assistant", "request.assistant.recover"):
        assert generation_cap_for_role(c, role) == 2048 and output_ceiling_for_role(c, role) == 4096
    for role in ("knowledge.gap_proposals", "extraction", "knowledge.qa", None):
        assert generation_cap_for_role(c, role) == 6144 and output_ceiling_for_role(c, role) == 12288


def test_without_role_caps_every_number_equals_the_earlier_formula(tmp_path):
    c = load(tmp_path, BASE)
    earlier = actual_call_cost_usd(c, model_id=worst_case_model_id(c), input_tokens=None, output_tokens=None)   # the priciest key at the global ceilings
    assert worst_case_call_usd(c) == earlier == Decimal("0.140976")                               # 24 x 0.00165 + 12.288 x 0.00825 (the us key)
    assert budget_invariants.job_reservation_usd(c) == earlier * 4                               # the reservation formula unchanged (admission refuses it at 0.52)


def test_with_role_caps_the_reservation_is_the_worst_call_of_any_role_at_its_own_ceiling(tmp_path):
    c = load(tmp_path, BASE + CAPS)
    assert worst_case_call_usd(c) == Decimal("0.12816")                                           # gap proposals: 24 x 0.0015 + 12.288 x 0.0075
    assert job_quota.compute_budget_reservation(c)[0] == Decimal("0.51264") == budget_invariants.job_reservation_usd(c)   # 4 calls x 1 attempt
    assert budget_invariants.problems(c) == []
    answer = actual_call_cost_usd(c, model_id="gemini-3.8-flash", input_tokens=None, output_tokens=None, output_ceiling=4096)
    recovery = actual_call_cost_usd(c, model_id="gemini-3.8-flash@us", input_tokens=None, output_tokens=None, output_ceiling=4096)
    assert answer == Decimal("0.06672") and recovery == Decimal("0.073392") and answer + recovery == Decimal("0.140112")   # the assistant's 2-call bound


def test_without_role_caps_the_us_route_would_not_fit_the_per_job_cap(tmp_path):
    c = load(tmp_path, BASE.replace('per_job_cap_usd: "0.52"', 'per_job_cap_usd: "0.60"'))
    assert job_quota.compute_budget_reservation(c)[0] == Decimal("0.563904")                    # 4 x 0.140976 > 0.52: why the role caps are needed
    c52 = load(tmp_path, BASE)
    with pytest.raises(job_quota.BudgetExceeded):
        job_quota.compute_budget_reservation(c52)                                                  # fail closed before any job is created


@pytest.mark.parametrize("extra, message", [
    ("role_max_output_tokens:\n  request.assistant: 0\n", "must be an int > 0"),
    ("role_generation_max_output_tokens:\n  request.assistant: two\n", "must be an int > 0"),
    ("role_max_output_tokens:\n  request.assistant: 4096\nrole_generation_max_output_tokens:\n  request.assistant: 3000\n", "2x overshoot"),
    ("role_max_output_tokens:\n  request.assistant: 4096\n", "2x overshoot"),                   # the global 6,144 sent would exceed half of 4,096
])
def test_a_role_cap_that_breaks_the_overshoot_rule_or_is_not_a_positive_int_is_refused(tmp_path, extra, message):
    with pytest.raises(BudgetConfigError) as e:
        load(tmp_path, BASE + extra)
    assert message in str(e.value)


def test_a_missing_output_side_is_booked_at_the_calls_role_ceiling_and_without_one_at_the_global_ceiling(tmp_path):
    c = load(tmp_path, BASE + CAPS)
    assert actual_call_cost_usd(c, model_id="gemini-3.8-flash", input_tokens=900, output_tokens=None, output_ceiling=4096) == Decimal("0.03207")   # 0.9 x 0.0015 + 4.096 x 0.0075
    assert actual_call_cost_usd(c, model_id="gemini-3.8-flash", input_tokens=900, output_tokens=None) == Decimal("0.093510")                       # 0.9 x 0.0015 + 12.288 x 0.0075
    assert actual_call_cost_usd(c, model_id="gemini-3.8-flash", input_tokens=900, output_tokens=150, output_ceiling=4096) == Decimal("0.002475")  # reported usage wins


def test_the_production_example_carries_the_accepted_route_and_fits_its_per_job_cap():
    c = job_quota.load_worker_budget_config(str(PRODUCTION))
    assert c.role_model_ids["request.assistant"] == c.role_model_ids["request.assistant.recover"] == c.role_model_ids["request.qa"] == "gemini-3.8-flash"
    assert c.role_locations == {"request.assistant.recover": "us"} and c.role_request_timeouts["request.assistant"] == 42.0
    assert generation_cap_for_role(c, "request.assistant") == 2048 and output_ceiling_for_role(c, "request.assistant.recover") == 4096
    assert generation_cap_for_role(c, "knowledge.gap_proposals") == 6144 and output_ceiling_for_role(c, "extraction") == 12288   # others unchanged
    assert worst_case_call_usd(c) == Decimal("0.12816") and job_quota.compute_budget_reservation(c)[0] == Decimal("0.51264") <= c.per_job_cap_usd == Decimal("0.52")
    job_quota.check_job_admission(c, job_kind="request.qa")                                        # 2 calls per attempt <= max_provider_calls_per_job 4
