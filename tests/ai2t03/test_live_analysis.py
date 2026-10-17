"""Live analysis: real worker/store path, substituted network client."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from infra.worker import job_worker, live_analysis
from infra.worker.budget_config import BudgetConfig, ModelPrice


def _config(**overrides):
    values = dict(
        per_job_cap_usd=Decimal("1"), total_cap_usd=Decimal("50"),
        max_output_tokens=123, max_input_tokens_per_call=2000,
        max_retries=0, max_provider_calls_per_job=1,
        model_prices={"gemini-priced": ModelPrice("gemini-priced", Decimal("0.01"), Decimal("0.02"))},
        default_model_id="gemini-priced", fail_closed_on_missing_usage=True,
        latency_slo_p50_seconds=10.0, latency_slo_p95_seconds=30.0,
    )
    values.update(overrides)
    return BudgetConfig(**values)


def _analysis_job(scoped_store, text="A source record to analyze"):
    store, tenant, workspace, _ = scoped_store
    digest = store.stage_blob(text.encode())
    store.publish_source(tenant, workspace, "src-" + uuid4().hex,
                         {"artifact-a": digest}, expected_version=0,
                         operation="source.import.commit", idempotency_key=uuid4().hex)
    head = store.head(tenant, workspace)
    job_id = "job-" + uuid4().hex
    store.create_job(tenant, workspace, job_id, "analysis",
                     {"artifact_id": "artifact-a"}, expected_version=head.version,
                     idempotency_key="create-" + job_id)
    # The source publish event precedes the job event in the durable outbox.
    first = store.claim_outbox(tenant, workspace, "worker")
    assert first.kind == "SOURCE_PUBLISHED"
    store.ack_outbox(tenant, workspace, first.id, "worker", first.fencing_token)
    return store, tenant, workspace, job_id


def _run(scoped_store, monkeypatch, client, *, config=None, text="A source record to analyze"):
    store, tenant, workspace, job_id = _analysis_job(scoped_store, text)
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: config or _config())
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda model: client)
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    return store, tenant, workspace, job_id


def _client(response=None, error=None):
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return response

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content), calls=calls)


def _response(text='{"summary":"ok","findings":[]} ', model="gemini-priced", usage=True,
              thought_tokens=None, finish_reason="STOP", total_tokens=None):
    return SimpleNamespace(text=text, model_version=model,
                           candidates=[SimpleNamespace(finish_reason=finish_reason)],
                           usage_metadata=(SimpleNamespace(prompt_token_count=7,
                                                           candidates_token_count=3,
                                                           thoughts_token_count=thought_tokens,
                                                           total_token_count=total_tokens)
                                           if usage else None))


@pytest.mark.parametrize("backend", ["vertex", "aistudio"])
def test_live_client_factory_uses_configured_ai_module_account(monkeypatch, backend):
    from ai_module import accounts, router
    from google import genai

    monkeypatch.setattr(router, "AIRouter", lambda: SimpleNamespace(
        routing={"text_generation": {"backend": backend, "account_id": "configured-account"}}))
    monkeypatch.setattr(accounts, "get_account", lambda account_id:
                        {"account_id": account_id, "default_region": "us-central1"})
    monkeypatch.setattr(accounts, "resolve_project_id", lambda account: "configured-project")
    monkeypatch.setattr(accounts, "resolve_aistudio_api_key", lambda account: "test-key")
    captured = []
    fake_client = object()

    def make_client(**kwargs):
        captured.append(kwargs)
        return fake_client

    monkeypatch.setattr(genai, "Client", make_client)
    assert live_analysis._client_for_model("gemini-priced") is fake_client
    if backend == "vertex":
        assert captured == [{"vertexai": True, "project": "configured-project",
                             "location": "us-central1"}]
    else:
        assert captured == [{"api_key": "test-key"}]


def test_vertex_factory_uses_configured_service_account(monkeypatch):
    from ai_module import accounts, router
    from google import genai
    from google.oauth2 import service_account

    monkeypatch.setattr(router, "AIRouter", lambda: SimpleNamespace(
        routing={"text_generation": {"backend": "vertex", "account_id": "service-account"}}))
    monkeypatch.setattr(accounts, "get_account", lambda account_id: {
        "id": account_id, "auth": "service_account", "default_region": "us-central1",
        "service_account_json": "private/path.json",
    })
    monkeypatch.setattr(accounts, "resolve_project_id", lambda account: "configured-project")
    monkeypatch.setattr(accounts, "resolve_service_account_path", lambda account: "resolved/path.json")
    credential = object()
    monkeypatch.setattr(service_account.Credentials, "from_service_account_file",
                        lambda path, scopes: credential)
    captured = []
    monkeypatch.setattr(genai, "Client", lambda **kwargs: captured.append(kwargs) or object())
    live_analysis._client_for_model("gemini-priced")
    assert captured[0]["credentials"] is credential


def test_analysis_worker_passes_real_output_ceiling_and_persists_priced_usage(scoped_store, monkeypatch):
    client = _client(_response())
    store, tenant, workspace, job_id = _run(scoped_store, monkeypatch, client)
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED"
    assert record["execution_model_id"] == "gemini-priced"
    assert record["execution_usage"]["input_tokens"] == 7
    assert record["execution_usage"]["output_tokens"] == 3
    assert Decimal(record["execution_cost_usd"]) == Decimal("0.00013")
    assert record["execution_result"] == {"summary": "ok", "findings": []}
    assert client.calls[0]["config"]["max_output_tokens"] == 123
    assert client.calls[0]["model"] == "gemini-priced"
    assert "A source record" in client.calls[0]["contents"]
    attempts = store.job_attempts(tenant, workspace, job_id)
    assert len(attempts) == 1
    assert attempts[0]["request_excerpt"] is None
    assert attempts[0]["provider_calls"] == 1


@pytest.mark.parametrize("response, reason", [
    (_response(text="not JSON"), "ProviderInvalidResponseError"),
    (_response(text="{}"), "ProviderInvalidResponseError"),
    (_response(model="unpriced-model"), "unpriced_model"),
])
def test_analysis_invalid_response_fails_with_durable_reason(scoped_store, monkeypatch,
                                                             response, reason):
    client = _client(response)
    store, tenant, workspace, job_id = _run(scoped_store, monkeypatch, client)
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert reason in record["execution_reason"]
    assert record["execution_result"] in ({}, None)


def test_analysis_max_tokens_finish_never_reports_completed(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _run(
        scoped_store, monkeypatch, _client(_response(finish_reason="MAX_TOKENS")))
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert record["execution_reason"].startswith("provider_output_truncated")       # a cut-off answer is named as such, never a result
    assert "finish_reason=MAX_TOKENS" in record["execution_reason"]   # the reason names WHY the response did not finish


def test_analysis_missing_usage_is_charged_at_configured_ceiling(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _run(scoped_store, monkeypatch,
                                            _client(_response(usage=False)))
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED"
    expected = Decimal("0.01") * 2 + Decimal("0.02") * Decimal("0.123")
    assert Decimal(record["execution_cost_usd"]) == expected


def test_analysis_thought_tokens_are_billed_without_losing_raw_usage(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _run(scoped_store, monkeypatch,
                                            _client(_response(thought_tokens=5)))
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED"
    assert record["execution_usage"]["output_tokens"] == 3
    assert record["execution_request_context"]["thought_tokens"] == 5
    assert Decimal(record["execution_cost_usd"]) == Decimal("0.00023")


def test_analysis_total_usage_catches_missing_thought_breakdown(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _run(
        scoped_store, monkeypatch, _client(_response(total_tokens=15)))
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_request_context"]["thought_tokens"] == 5
    assert Decimal(record["execution_cost_usd"]) == Decimal("0.00023")


def test_analysis_storage_failure_reaches_explainable_terminal_state(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _analysis_job(scoped_store)
    monkeypatch.setattr(store.blobs, "read", lambda digest: (_ for _ in ()).throw(
        RuntimeError("storage unavailable")))
    monkeypatch.setattr(job_worker, "_load_config_if_configured", _config)
    client = _client(_response())
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda model: client)
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert "source storage unavailable" in record["execution_reason"]
    assert client.calls == []


@pytest.mark.parametrize("error, reason", [
    (TimeoutError("timeout"), "ProviderTimeoutError"),
    (type("RateLimited", (Exception,), {"code": 429})("rate limit"),
     "ProviderRateLimitError"),
])
def test_analysis_provider_failures_have_durable_retryable_reason(
        scoped_store, monkeypatch, error, reason):
    store, tenant, workspace, job_id = _run(scoped_store, monkeypatch,
                                            _client(error=error))
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert reason in record["execution_reason"]
    attempt = store.job_attempts(tenant, workspace, job_id)[0]
    assert attempt["retryable"] is True
    assert attempt["provider_calls"] == 1


def test_analysis_refuses_oversized_prompt_before_network(scoped_store, monkeypatch):
    client = _client(_response())
    store, tenant, workspace, job_id = _run(scoped_store, monkeypatch, client,
        config=_config(max_input_tokens_per_call=20))
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert "budget_exceeded" in record["execution_reason"]
    assert client.calls == []


def test_analysis_project_policy_denies_external_call(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _analysis_job(scoped_store)
    store.set_project_model_policy(tenant, "p1", external_provider="deny")
    client = _client(_response())
    monkeypatch.setattr(job_worker, "_load_config_if_configured", _config)
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda model: client)
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert "model_data_policy_denied" in record["execution_reason"]
    assert client.calls == []


def test_analysis_redacted_excerpt_never_persists_secret(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _analysis_job(
        scoped_store, 'config secret=topsecretvalue and hello')
    store.set_project_model_policy(tenant, "p1", prompt_storage="redacted_excerpt")
    monkeypatch.setattr(job_worker, "_load_config_if_configured", _config)
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda model: _client(_response()))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    attempt = store.job_attempts(tenant, workspace, job_id)[0]
    assert "[REDACTED]" in attempt["request_excerpt"]
    assert "topsecretvalue" not in attempt["request_excerpt"]
    assert "topsecretvalue" not in str(store.job_record(tenant, workspace, job_id))


def test_cancel_while_provider_runs_prevents_completed_result(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _analysis_job(scoped_store)

    def generate_content(**kwargs):
        store.request_cancel(tenant, workspace, job_id, "operator_cancelled")
        return _response()

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(job_worker, "_load_config_if_configured", _config)
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda model: client)
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "CANCELLED"
    assert record["status"] == "CANCELLED"
    assert record["execution_result"] in ({}, None)
    assert len(store.job_attempts(tenant, workspace, job_id)) == 1


def test_unregistered_kind_fails_closed_without_provider_call(scoped_store, monkeypatch):
    store, tenant, workspace, _ = _analysis_job(scoped_store)
    head = store.head(tenant, workspace)
    job_id = "unknown-" + uuid4().hex
    store.create_job(tenant, workspace, job_id, "unknown_kind", {},
                     expected_version=head.version, idempotency_key="create-" + job_id)
    monkeypatch.setattr(job_worker, "_load_config_if_configured", _config)
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda model: _client(_response()))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")  # analysis first
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")  # unknown kind
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert "no_executor_registered" in record["execution_reason"]
    assert record["execution_provider_calls"] == 0


def test_actual_call_site_ceiling_rejects_underdeclared_executor(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _analysis_job(scoped_store)
    attempted = []

    class Underdeclared:
        executor_id = "test.underdeclared"

        def prepare(self, ctx):
            return job_worker.ExecutionRequest(prompt="test", schema=None,
                                               provider_calls_planned=0, external=True)

        def execute(self, ctx, request, calls):
            calls.consume()
            attempted.append("first")
            calls.consume()  # Must raise before second network dispatch.
            attempted.append("second")

    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "analysis", Underdeclared())
    monkeypatch.setattr(job_worker, "_load_config_if_configured", _config)
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert "provider_call_ceiling_exceeded" in record["execution_reason"]
    assert attempted == ["first"]
    assert store.job_attempts(tenant, workspace, job_id)[0]["provider_calls"] == 1


def test_settlement_failure_rolls_back_attempt_and_terminal_state(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _analysis_job(scoped_store)
    monkeypatch.setattr(live_analysis, "_client_for_model", lambda model: _client(_response()))
    original = type(store)._accrue_ledger

    def fail_settlement(cur, tenant_id, workspace_id, recorded_job_id, *, settle):
        if settle and recorded_job_id == job_id:
            raise RuntimeError("settlement injection")
        return original(cur, tenant_id, workspace_id, recorded_job_id, settle=settle)

    monkeypatch.setattr(type(store), "_accrue_ledger", staticmethod(fail_settlement))
    claim = store.claim_outbox(tenant, workspace, "worker")
    with pytest.raises(RuntimeError, match="settlement injection"):
        job_worker.execute_admitted_claim(store, tenant, workspace, claim, "worker",
                                          config=_config())
    assert store.job_record(tenant, workspace, job_id)["execution_status"] == "RUNNING"
    # `analysis` is a provider-backed kind (provider_gate.AI_KINDS): its in-flight attempt is journaled durably BEFORE the call and booked at its
    # ceiling until usage is recorded, so a failed settlement never makes a possibly charged call vanish. No COMPLETED attempt exists.
    rows = store.job_attempts(tenant, workspace, job_id)
    assert [r["outcome"] for r in rows] == ["IN_FLIGHT"]
    assert rows[0]["provider_calls"] == 1 and rows[0]["request_context"]["cost_basis"] == "ceiling_until_usage_is_recorded"
    assert rows[0]["cost_usd"] > 0
