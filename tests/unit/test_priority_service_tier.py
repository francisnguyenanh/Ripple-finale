"""Provider service tier for a configured role (R7, owner decision 2026-10-28: Priority PayGo tried for the request assistant). Pure: config validation, the
reservation at the priciest configured price, the request headers, the served tier read from the response and the price of the tier actually served."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml

from infra.worker import budget_invariants, explain, job_worker
from infra.worker.budget_config import BudgetConfigError, price_key_for_role, service_tier_for_role, worst_case_model_id
from infra.worker.execution_types import ExecutionOutcome

BASE = {
    "per_job_cap_usd": "0.47", "total_cap_usd": "10.00", "max_output_tokens": 12288, "max_input_tokens_per_call": 24000, "max_retries": 0,
    "max_provider_calls_per_job": 4, "default_model_id": "gemini-3.5-flash-lite", "fail_closed_on_missing_usage": True,
    "latency_slo_p50_seconds": 30, "latency_slo_p95_seconds": 90, "enabled_roles": "all",
    "model_prices": {"gemini-3.5-flash-lite": {"input_usd_per_1k": "0.0003", "output_usd_per_1k": "0.0025"},
                     "gemini-3.8-flash": {"input_usd_per_1k": "0.00075", "output_usd_per_1k": "0.00375"},
                     "gemini-3.8-flash@priority": {"input_usd_per_1k": "0.00135", "output_usd_per_1k": "0.00675"}},
    "role_model_ids": {"request.assistant": "gemini-3.8-flash", "request.qa": "gemini-3.8-flash"},
    "role_thinking_levels": {"request.assistant": "LOW"},
    "role_service_tiers": {"request.assistant": "priority", "request.qa": "priority"},
}


def load(tmp_path, monkeypatch, **changes):
    raw = {**BASE, **changes}
    raw = {k: v for k, v in raw.items() if v is not None}
    path = tmp_path / "b.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    from infra.worker.job_quota import load_worker_budget_config
    return load_worker_budget_config(str(path))


def test_a_tier_needs_its_own_price_and_the_reservation_takes_the_priority_price(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch)
    assert service_tier_for_role(c, "request.assistant") == "priority" and price_key_for_role(c, "request.assistant") == "gemini-3.8-flash@priority"
    assert service_tier_for_role(c, "knowledge.qa") is None and price_key_for_role(c, "knowledge.qa") == "gemini-3.5-flash-lite"
    assert worst_case_model_id(c) == "gemini-3.8-flash@priority"
    assert budget_invariants.job_reservation_usd(c) == Decimal("0.461376")                        # 4 x (24000 x 0.00135 + 12288 x 0.00675) / 1k
    assert not budget_invariants.problems(c)
    assert budget_invariants.problems(load(tmp_path, monkeypatch, per_job_cap_usd="0.46"))         # below the reservation: refused before arming
    prices = {k: v for k, v in BASE["model_prices"].items() if "@" not in k}
    with pytest.raises(BudgetConfigError, match="needs its own price"):
        load(tmp_path, monkeypatch, model_prices=prices)
    with pytest.raises(BudgetConfigError, match="tier must be one of"):
        load(tmp_path, monkeypatch, role_service_tiers={"request.assistant": "express"})
    plain = load(tmp_path, monkeypatch, role_service_tiers=None)
    assert worst_case_model_id(plain) == "gemini-3.8-flash" and service_tier_for_role(plain, "request.assistant") is None


def test_the_served_tier_is_read_from_the_response_and_a_missing_one_is_priced_as_priority():
    assert explain.served_service_tier("priority", "ON_DEMAND_PRIORITY") == "priority"
    assert explain.served_service_tier("priority", "ON_DEMAND") is None                            # downgraded: the provider bills standard
    assert explain.served_service_tier("priority", None) == "priority"                             # not reported: fail closed
    assert explain.served_service_tier(None, "ON_DEMAND_PRIORITY") is None
    enum_like = SimpleNamespace(name="ON_DEMAND_PRIORITY")
    assert explain._traffic_type(SimpleNamespace(traffic_type=enum_like)) == "ON_DEMAND_PRIORITY"
    assert explain._traffic_type(SimpleNamespace(traffic_type="TrafficType.ON_DEMAND")) == "ON_DEMAND"
    assert explain._traffic_type(SimpleNamespace(traffic_type=None)) is None


def test_the_attempt_is_priced_at_the_tier_actually_served(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch)
    served = ExecutionOutcome(result={}, model_id="gemini-3.8-flash", input_tokens=2400, output_tokens=220, thought_tokens=0, service_tier="priority")
    down = ExecutionOutcome(result={}, model_id="gemini-3.8-flash", input_tokens=2400, output_tokens=220, thought_tokens=0, service_tier=None)
    est, cost = job_worker._attempt_cost(c, 1, served, "request.assistant")
    assert est == Decimal("0.115344") and cost == Decimal("0.0047250")                             # 2400 x 0.00135 + 220 x 0.00675 (per 1k)
    assert job_worker._attempt_cost(c, 1, down, "request.assistant")[1] == Decimal("0.0026250")   # 2400 x 0.00075 + 220 x 0.00375


class _Capture:
    def __init__(self, traffic):
        self.configs, self.traffic = [], traffic
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.configs.append(kw["config"])
        usage = SimpleNamespace(prompt_token_count=2400, candidates_token_count=220, thoughts_token_count=0, total_token_count=2620, traffic_type=self.traffic)
        return SimpleNamespace(text='{"a": 1}', model_version="gemini-3.8-flash", candidates=[SimpleNamespace(finish_reason="STOP")], usage_metadata=usage)


@pytest.mark.parametrize("traffic, tier", [("ON_DEMAND_PRIORITY", "priority"), ("ON_DEMAND", None)])
def test_the_priority_headers_are_sent_and_the_served_tier_is_recorded(tmp_path, monkeypatch, traffic, tier):
    from infra.worker.execution_types import ExecutionRequest, ProviderCallBudget
    c = load(tmp_path, monkeypatch)
    cap = _Capture(traffic)
    monkeypatch.setattr(explain, "_client_for_model", lambda m: cap)
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    ctx = SimpleNamespace(config=c, max_output_tokens=6144, request_context={})
    out = explain.call_pinned_json(ctx, ExecutionRequest(prompt="p", schema=None, provider_calls_planned=1, external=True), ProviderCallBudget(1), "request.assistant", {"a"})
    assert out[0] == {"a": 1}
    assert cap.configs[0]["http_options"]["headers"] == {"X-Vertex-AI-LLM-Request-Type": "shared", "X-Vertex-AI-LLM-Shared-Request-Type": "priority"}
    row = ctx.request_context["provider_usage"][0]
    assert row["traffic_type"] == traffic and row["requested_tier"] == "priority" and row["service_tier"] == tier and explain.last_service_tier(ctx) == tier
    other = SimpleNamespace(config=load(tmp_path, monkeypatch, role_service_tiers=None), max_output_tokens=6144, request_context={})
    explain.call_pinned_json(other, ExecutionRequest(prompt="p", schema=None, provider_calls_planned=1, external=True), ProviderCallBudget(1), "request.assistant", {"a"})
    assert "headers" not in (cap.configs[1].get("http_options") or {})                            # no tier configured: no tier header


@pytest.mark.parametrize("traffic", ["ON_DEMAND_PRIORITY", "ON_DEMAND"])
def test_priority_headers_reach_actual_sdk_http_transport(tmp_path, monkeypatch, traffic):
    """Offline transport: real Google SDK serialization, anonymous credentials, no network/provider call."""
    import json
    import httpx
    from google import genai
    from google.genai import types
    from google.auth.credentials import AnonymousCredentials
    from infra.worker.execution_types import ExecutionRequest, ProviderCallBudget
    sent = []
    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": '{"a": 1}'}]}, "finishReason": "STOP"}],
            "modelVersion": "gemini-3.8-flash",
            "usageMetadata": {"promptTokenCount": 24, "candidatesTokenCount": 5, "totalTokenCount": 29, "trafficType": traffic}})
    credentials = AnonymousCredentials()
    credentials.token = "offline-test-only"  # SDK requires a token string even with an entirely mocked transport
    client = genai.Client(vertexai=True, project="offline-transport-test", location="global", credentials=credentials,
                         http_options=types.HttpOptions(httpx_client=httpx.Client(transport=httpx.MockTransport(respond)),
                                                        retry_options=types.HttpRetryOptions(attempts=1)))
    monkeypatch.setattr(explain, "_client_for_model", lambda m: client)
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)
    ctx = SimpleNamespace(config=load(tmp_path, monkeypatch), max_output_tokens=6144, request_context={})
    explain.call_pinned_json(ctx, ExecutionRequest(prompt="offline", schema=None, provider_calls_planned=1, external=True),
                            ProviderCallBudget(1), "request.assistant", {"a"})
    assert len(sent) == 1
    assert sent[0].headers["X-Vertex-AI-LLM-Request-Type"] == "shared"
    assert sent[0].headers["X-Vertex-AI-LLM-Shared-Request-Type"] == "priority"
    from infra.worker.budget_config import generation_cap_for_role
    assert json.loads(sent[0].content)["generationConfig"]["maxOutputTokens"] == generation_cap_for_role(ctx.config, "request.assistant")   # R23: the role's configured cap
    assert ctx.request_context["provider_usage"][0]["traffic_type"] == traffic
    assert ctx.request_context["provider_usage"][0]["service_tier"] == ("priority" if traffic == "ON_DEMAND_PRIORITY" else None)
