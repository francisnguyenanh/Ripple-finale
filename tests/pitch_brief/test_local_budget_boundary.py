"""The local live-local budget after the B6 truncation (2026-10-09, user-authorised for LOCAL fixing and rehearsal only).

The numbers are chosen from the contract, not guessed: the request cap (output + thinking) is 12288 tokens, the reservation ceiling is 2 x that
(a reported up to 2x thinking overshoot must still fit inside what is reserved), the per-job cap covers EVERY attempt the job may make, and the
total cap is the unchanged finite 6.00 USD of the project ledger. The production config and the Pitch candidate config are not touched.
This file checks the loader contract, `budget_invariants.problems()`, the worst case, and the behaviour exactly at and just beyond each boundary."""
from __future__ import annotations

from decimal import Decimal

import pytest

from infra.worker import budget_invariants, job_quota
from infra.worker.budget_config import BudgetExceeded, check_admission

TEMPLATE = """
default_model_id: gemini-3.5-flash-lite
role_model_ids:
  extraction: gemini-3.8-flash
  change.draft: gemini-3.8-flash
  intent.interpret: gemini-3.8-flash
  gap.answer: gemini-3.8-flash
  impact.judge: gemini-3.8-flash
  intent.explain: gemini-3.5-flash-lite
  simulation.analyze: gemini-3.8-flash
model_prices:
  gemini-3.5-flash-lite: {{input_usd_per_1k: "0.0003", output_usd_per_1k: "0.0025"}}
  gemini-3.8-flash: {{input_usd_per_1k: "0.0015", output_usd_per_1k: "0.0075"}}
price_snapshot_date: "2026-09-30"
price_snapshot_max_age_days: 30
request_timeout_seconds: {timeout}
per_job_cap_usd: "{per_job}"
total_cap_usd: "{total}"
max_input_tokens_per_call: 24000
max_output_tokens: {ceiling}
generation_max_output_tokens: {generation}
max_retries: 1
max_provider_calls_per_job: 1
max_concurrent_jobs_per_workspace: 1
fail_closed_on_missing_usage: true
latency_slo_p50_seconds: 30
latency_slo_p95_seconds: 90
"""
LOCAL = dict(timeout=180, per_job="0.50", total="6.00", ceiling=24576, generation=12288)
# worst case of ONE call on the most expensive pinned model: 24000 in x 0.0015/1k + 24576 out x 0.0075/1k; a job makes at most (max_retries + 1) = 2 attempts
CALL = Decimal(24000) / 1000 * Decimal("0.0015") + Decimal(24576) / 1000 * Decimal("0.0075")
JOB = CALL * 2


def load(tmp_path, **over):
    p = tmp_path / "budget.yaml"
    p.write_text(TEMPLATE.format(**{**LOCAL, **over}), encoding="utf-8")
    return job_quota.load_worker_budget_config(str(p))


def test_the_local_config_loads_and_the_reservation_bounds_one_job(tmp_path):
    cfg = load(tmp_path)
    assert cfg.generation_max_output_tokens == 12288 and cfg.max_output_tokens == 24576
    assert budget_invariants.problems(cfg) == []
    assert CALL == Decimal("0.22032") and JOB == Decimal("0.44064")
    assert budget_invariants.job_reservation_usd(cfg) == JOB and job_quota.compute_budget_reservation(cfg)[0] == JOB
    assert JOB <= cfg.per_job_cap_usd and JOB <= cfg.total_cap_usd
    assert budget_invariants.max_total_provider_calls(cfg) == 2


def test_the_per_job_cap_must_cover_every_attempt_exactly_at_the_boundary(tmp_path):
    assert budget_invariants.problems(load(tmp_path, per_job="0.44064")) == []                  # equal to the reservation: allowed
    low = load(tmp_path, per_job="0.44063")
    assert any("above the per-job cap" in p for p in budget_invariants.problems(low))           # one hundred-thousandth below: unsafe
    with pytest.raises(BudgetExceeded):
        job_quota.compute_budget_reservation(low)                                               # and a job is refused at creation


def test_the_total_cap_admits_exactly_one_reservation_and_refuses_one_unit_more(tmp_path):
    cfg = load(tmp_path)
    check_admission(cfg, scope="total", spent_usd=cfg.total_cap_usd - JOB, estimate_usd=JOB)      # fits exactly
    with pytest.raises(BudgetExceeded):
        check_admission(cfg, scope="total", spent_usd=cfg.total_cap_usd - JOB + Decimal("0.00001"), estimate_usd=JOB)


def test_a_thinking_overshoot_beyond_the_reservation_ceiling_is_refused_by_the_loader(tmp_path):
    with pytest.raises(Exception):
        load(tmp_path, ceiling=24575)                                                           # 2 x 12288 no longer fits inside what is reserved


def test_a_failed_call_is_priced_on_reported_usage_and_never_above_its_own_reservation(tmp_path):
    cfg = load(tmp_path)
    full = job_quota.actual_call_cost_usd(cfg, model_id="gemini-3.8-flash", input_tokens=None, output_tokens=None)
    assert full == CALL                                                                         # no usage: the whole ceiling (an estimate)
    reported = job_quota.actual_call_cost_usd(cfg, model_id="gemini-3.8-flash", input_tokens=7000, output_tokens=12288)
    assert reported == Decimal("0.09216") + Decimal("0.0105") and reported < CALL                # a call that hit the request cap
