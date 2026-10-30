"""R21 (owner approval 2026-10-29): a bounded per-role Vertex location — only the request assistant's recovery call may use a multi-region ("us" / "eu");
every other role stays on the global endpoint. Pure: config validation, the price key per location, the worst-case reservation, the per-call booking of a
recovery at its location (usage known or missing) and the call-site refusal of a location for a role that is not allow-listed."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml

from infra.worker import budget_invariants, explain, job_quota, job_worker
from infra.worker.budget_config import BudgetConfigError, LOCATION_ROLES, location_for_role, price_key, price_key_for_role, worst_case_model_id
from infra.worker.execution_types import ExecutionOutcome, NonRetryableExecutionError

BASE = {
    "per_job_cap_usd": "0.29", "total_cap_usd": "10.00", "max_output_tokens": 12288, "max_input_tokens_per_call": 24000, "max_retries": 0,
    "max_provider_calls_per_job": 2, "default_model_id": "gemini-3.5-flash-lite", "fail_closed_on_missing_usage": True,
    "latency_slo_p50_seconds": 30, "latency_slo_p95_seconds": 90, "enabled_roles": "all",
    "model_prices": {"gemini-3.5-flash-lite": {"input_usd_per_1k": "0.0003", "output_usd_per_1k": "0.0025"},
                     "gemini-3.8-flash": {"input_usd_per_1k": "0.00075", "output_usd_per_1k": "0.00375"},
                     "gemini-3.8-flash@us": {"input_usd_per_1k": "0.00165", "output_usd_per_1k": "0.00825"}},
    "role_model_ids": {"request.assistant": "gemini-3.8-flash", "request.assistant.recover": "gemini-3.8-flash", "request.qa": "gemini-3.8-flash"},
    "role_locations": {"request.assistant.recover": "us"},
}


def load(tmp_path, monkeypatch, **changes):
    raw = {**BASE, **changes}
    raw = {k: v for k, v in raw.items() if v is not None}
    path = tmp_path / "b.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    return job_quota.load_worker_budget_config(str(path))


def test_only_the_recovery_role_gets_a_location_every_other_role_stays_global(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch)
    assert LOCATION_ROLES == frozenset({"request.assistant", "request.assistant.recover"})   # R23: the answer call may be located too (prepared, not active)
    assert location_for_role(c, "request.assistant.recover") == "us" and price_key_for_role(c, "request.assistant.recover") == "gemini-3.8-flash@us"
    assert location_for_role(c, "request.assistant") is None and price_key_for_role(c, "request.assistant") == "gemini-3.8-flash"
    assert location_for_role(c, "request.qa") is None and location_for_role(load(tmp_path, monkeypatch, role_locations=None), "request.assistant.recover") is None


@pytest.mark.parametrize("bad, why", [
    ({"request.assistant.complete": "us"}, "only"),                              # the supplement stays global
    ({"request.qa": "eu"}, "only"),
    ({"request.assistant.recover": "us-central1"}, "location must be one of"),
    ({"request.assistant.recover": "global"}, "location must be one of"),      # global is the absence of a location
])
def test_a_location_outside_the_allow_list_is_refused(tmp_path, monkeypatch, bad, why):
    with pytest.raises(BudgetConfigError) as e:
        load(tmp_path, monkeypatch, role_locations=bad)
    assert why in str(e.value)


def test_a_location_needs_its_own_price_and_never_combines_with_a_tier(tmp_path, monkeypatch):
    prices = {k: v for k, v in BASE["model_prices"].items() if k != "gemini-3.8-flash@us"}
    with pytest.raises(BudgetConfigError) as e:
        load(tmp_path, monkeypatch, model_prices=prices)
    assert "gemini-3.8-flash@us" in str(e.value)
    with pytest.raises(BudgetConfigError) as e2:
        load(tmp_path, monkeypatch, model_prices={**BASE["model_prices"], "gemini-3.8-flash@priority": {"input_usd_per_1k": "0.00135", "output_usd_per_1k": "0.00675"}},
             role_service_tiers={"request.assistant.recover": "priority"})
    assert "cannot combine" in str(e2.value)


def test_the_reservation_takes_the_location_price_and_a_recovery_is_booked_at_its_location(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch)
    assert worst_case_model_id(c) == "gemini-3.8-flash@us"
    us_ceiling = job_quota.actual_call_cost_usd(c, model_id="gemini-3.8-flash@us", input_tokens=None, output_tokens=None)
    gl_ceiling = job_quota.actual_call_cost_usd(c, model_id="gemini-3.8-flash", input_tokens=None, output_tokens=None)
    assert us_ceiling == Decimal("0.140976") and gl_ceiling == Decimal("0.06408") and budget_invariants.job_reservation_usd(c) == 2 * us_ceiling
    assert not budget_invariants.problems(c)
    answered = ExecutionOutcome(result={}, priced_calls=({"price_key": "gemini-3.8-flash", "input_tokens": None, "output_tokens": None},
                                                         {"price_key": price_key("gemini-3.8-flash", None, "us"), "input_tokens": 1000, "output_tokens": 200}))
    est, cost = job_worker._attempt_cost(c, 2, answered, "request.qa")
    assert cost == gl_ceiling + (Decimal(1000) * Decimal("0.00165") + Decimal(200) * Decimal("0.00825")) / 1000
    missing = ExecutionOutcome(result={}, priced_calls=({"price_key": "gemini-3.8-flash", "input_tokens": None, "output_tokens": None},
                                                        {"price_key": "gemini-3.8-flash@us", "input_tokens": None, "output_tokens": None}))
    assert job_worker._attempt_cost(c, 2, missing, "request.qa")[1] == gl_ceiling + us_ceiling          # unknown usage at each call's own ceiling, never 0


def test_the_worker_books_a_refused_recovered_answer_per_call_from_the_executor_pricing():
    priced = ({"price_key": "gemini-3.8-flash", "input_tokens": None, "output_tokens": None}, {"price_key": "gemini-3.8-flash@us", "input_tokens": 900, "output_tokens": 150})
    ctx = SimpleNamespace(request_context={"rqa_priced_calls": priced, "provider_usage": [{"model_id": "gemini-3.8-flash", "input_tokens": 900, "output_tokens": 150}]})
    out = job_worker._answered_usage(ctx, 2)
    assert out is not None and out.priced_calls == priced                     # not the one usage row at the global key
    assert job_worker._answered_usage(SimpleNamespace(request_context={}), 2) is None   # nothing priced and no rows: fail closed (the ceiling decides)


def test_the_call_site_refuses_a_location_for_a_role_that_is_not_allow_listed(monkeypatch):
    cfg = SimpleNamespace(role_model_ids={"request.qa": "gemini-3.8-flash"}, role_locations={"request.qa": "us"}, max_input_tokens_per_call=24000)
    ctx = SimpleNamespace(config=cfg, max_output_tokens=1024, request_context={})
    request = SimpleNamespace(prompt="x", schema={"type": "object"})
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: (_ for _ in ()).throw(AssertionError("must not be reached")))
    with pytest.raises(NonRetryableExecutionError) as e:
        explain.call_pinned_json(ctx, request, SimpleNamespace(consume=lambda: None), "request.qa", {"a"})
    assert "endpoint_location_not_allowed" in str(e.value)


def test_r23_a_direct_us_answer_call_with_a_global_recovery_loads_and_reserves_two_us_ceilings(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch, role_locations={"request.assistant": "us"})
    assert location_for_role(c, "request.assistant") == "us" and price_key_for_role(c, "request.assistant") == "gemini-3.8-flash@us"
    assert location_for_role(c, "request.assistant.recover") is None and price_key_for_role(c, "request.assistant.recover") == "gemini-3.8-flash"
    assert worst_case_model_id(c) == "gemini-3.8-flash@us" and budget_invariants.job_reservation_usd(c) == Decimal("0.281952") and not budget_invariants.problems(c)


def test_r23_a_lower_generation_cap_keeps_the_2x_reservation_and_lowers_both_ceilings(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch, max_output_tokens=4096, generation_max_output_tokens=2048)
    assert job_quota.actual_call_cost_usd(c, model_id="gemini-3.8-flash", input_tokens=None, output_tokens=None) == Decimal("0.03336")
    assert job_quota.actual_call_cost_usd(c, model_id="gemini-3.8-flash@us", input_tokens=None, output_tokens=None) == Decimal("0.073392")
    with pytest.raises(BudgetConfigError):
        load(tmp_path, monkeypatch, max_output_tokens=3000, generation_max_output_tokens=2048)    # the reservation must cover a 2x overshoot
