"""AI-02 (audit 2026-10-30): what each provider executor really sends, read from the request that leaves the SDK client (a capturing client in
place of `genai.Client`; no provider), against the production budget example. This is the record of the three request shapes:
  * the generic pinned-role call (`explain.call_pinned_json`, 17 roles): the role's generation cap, its configured thinking level (only when
    configured), the response schema, its deadline, its location (only the allow-listed recovery role);
  * `extraction`: the global generation cap (ExecutionContext.max_output_tokens resolves to `generation_max_output_tokens`), the response
    schema, the shared timeout, and NO thinking level whatever the configuration declares (provider default, not pinned);
  * the semantic retrieval provider: the adapter's own cap (512, as `v2_business_index` constructs it) and NO thinking level.
Every sent cap is at or below the role's reservation ceiling and the per-call ceiling booked before the call covers the sent cap at the role's price
(the audit's claim that extraction sent the reservation ceiling 12,288 was a reading of the source text, not of the request: it sends 6,144).
"""
from __future__ import annotations

import pathlib
from decimal import Decimal
from types import SimpleNamespace

import pytest

from infra.worker import budget_config, explain, extraction, job_worker
from infra.worker.execution_types import ExecutionContext, ExecutionRequest, ProviderCallBudget
from infra.worker.job_quota import load_worker_budget_config

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "config" / "ai_closed_loop" / "final_budgets.production.example.yaml"


class _Captured(Exception):
    pass


class Capturing:
    """Records the keyword arguments of `models.generate_content` and the location the client was built for, then stops the call."""
    seen: list = []

    def __init__(self, location=None):
        self.location = location
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        Capturing.seen.append({"location": self.location, **kw})
        raise _Captured()

    def close(self):
        pass


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.delenv("RIPPLE_BUDGET_CONFIG_PATH", raising=False)
    Capturing.seen = []
    monkeypatch.setattr(explain, "_assert_global_endpoint", lambda: None)        # the routed account is not under test; the request shape is
    monkeypatch.setattr(explain, "_assert_vertex_backend", lambda: None)
    monkeypatch.setattr(explain, "_client_for_model", lambda m, location=None: Capturing(location))
    monkeypatch.setattr(extraction, "_client_for_model", lambda m, location=None: Capturing(location))
    return load_worker_budget_config(str(EXAMPLE))


def _ctx(cfg):
    return ExecutionContext(store=None, job={"job_id": "j", "kind": "x", "payload": {}}, config=cfg)


def _generic(cfg, role, prompt="question"):
    request = ExecutionRequest(prompt=prompt, schema={"type": "object", "properties": {"a": {"type": "string"}}}, provider_calls_planned=1, external=True)
    with pytest.raises(Exception):                                                  # the capturing client stops the call; explain wraps it as a provider failure
        explain.call_pinned_json(_ctx(cfg), request, ProviderCallBudget(2), role, key_set={"a"})
    return Capturing.seen[-1]


def _price_of(cfg, role, out_tokens: int) -> Decimal:
    p = cfg.model_prices[budget_config.price_key_for_role(cfg, role)]
    return (Decimal(cfg.max_input_tokens_per_call) * Decimal(p.input_usd_per_1k) + Decimal(out_tokens) * Decimal(p.output_usd_per_1k)) / 1000


def test_generic_roles_send_their_cap_their_thinking_when_configured_and_their_location(cfg):
    qa = _generic(cfg, "knowledge.qa")
    assert qa["config"]["max_output_tokens"] == 6144 == budget_config.generation_cap_for_role(cfg, "knowledge.qa")
    assert qa["config"]["thinking_config"] == {"thinking_level": "LOW"} and qa["config"]["response_schema"] and qa["location"] is None
    assert qa["config"]["http_options"]["timeout"] == 120000
    ra = _generic(cfg, "request.assistant")
    assert ra["config"]["max_output_tokens"] == 2048 and ra["config"]["thinking_config"] == {"thinking_level": "LOW"} and ra["location"] is None
    assert ra["config"]["http_options"]["timeout"] == 42000
    rec = _generic(cfg, "request.assistant.recover")
    assert rec["config"]["max_output_tokens"] == 2048 and rec["location"] == "us" and rec["model"] == "gemini-3.8-flash"
    roi = _generic(cfg, "roi.analyst")
    assert "thinking_config" not in roi["config"], "a role without a configured level sends none: the provider default, not pinned"
    assert roi["config"]["max_output_tokens"] == 6144 and roi["model"] == "gemini-3.5-flash-lite"


def test_extraction_sends_the_generation_cap_and_no_thinking_level(cfg):
    ex = extraction.ExtractRulesExecutor()
    request = ExecutionRequest(prompt="<source>\nx\n</source>", schema=extraction._OUTPUT_SCHEMA, provider_calls_planned=1, external=True)
    try:
        ex.execute(_ctx(cfg), request, ProviderCallBudget(1))
    except Exception:                                                               # noqa: BLE001  the capturing client stops the call; the outcome is not under test
        pass
    sent = Capturing.seen[-1]
    assert sent["model"] == "gemini-3.5-flash-lite" and sent["location"] is None
    assert sent["config"]["max_output_tokens"] == 6144 == cfg.generation_max_output_tokens, "the generation cap, not the 12,288 reservation ceiling"
    assert "thinking_config" not in sent["config"], "extraction never sends a thinking level (the example declares none for it since 2026-10-30)"
    assert sent["config"]["response_schema"] and sent["config"]["http_options"]["timeout"] == 120000


def test_semantic_retrieval_sends_its_own_cap_and_no_thinking_level(cfg):
    from infra.adapters import semantic_vertex
    provider = semantic_vertex.VertexSemanticProvider(budget_config.model_for_role(cfg, "retrieval.semantic"), client_factory=lambda m: Capturing())
    try:
        provider.call("expand", {"query": "q", "items": [], "candidates": []}, timeout=5)
    except Exception:                                                               # noqa: BLE001
        pass
    sent = Capturing.seen[-1]
    assert sent["model"] == "gemini-3.5-flash-lite" and sent["config"]["max_output_tokens"] == 512 and "thinking_config" not in sent["config"]
    assert budget_config.thinking_level_for_role(cfg, "retrieval.semantic") is None


def test_every_sent_cap_is_within_the_reservation_and_the_per_call_ceiling_covers_it(cfg):
    sent_caps = {"knowledge.qa": 6144, "roi.analyst": 6144, "request.assistant": 2048, "request.assistant.recover": 2048, "extraction": 6144, "retrieval.semantic": 512}
    for role, cap in sent_caps.items():
        assert cap <= budget_config.output_ceiling_for_role(cfg, role), role
        ceiling_usd = job_worker._attempt_cost(cfg, 1, None, role)[0]
        assert ceiling_usd >= _price_of(cfg, role, cap), (role, ceiling_usd)
    assert budget_config.thinking_level_for_role(cfg, "extraction") is None, "declared-but-not-sent levels were removed from the example (AI-02 option A)"
    assert not {"extraction", "retrieval.semantic"} & set(cfg.role_thinking_levels or {})
