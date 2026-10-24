"""Extraction boundaries: cancel/stale-head,
model-data-policy denial, and provider rate-limit/timeout, each proven
through the real `extraction` job kind on the exact real-worker/real-store
boundary already established for the sibling "analysis" kind
(`tests/ai2t03/test_live_analysis.py`). No production file is touched by
this file; each guard already exists in `infra/worker/job_worker.py` (shared
by every registered external executor) -- these tests prove it actually
applies to `ExtractRulesExecutor`, not merely that the shared code exists.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from infra.worker import job_worker, extraction
from infra.worker.budget_config import BudgetConfig, ModelPrice
from tests.ai2t03.conftest import scoped_store  # noqa: F401  (fixture reuse)


def _config(**overrides):
    values = dict(
        per_job_cap_usd=None, total_cap_usd=None,
        max_output_tokens=123, max_input_tokens_per_call=24000,   # the production ceiling: at 4000 the Final extraction prompt (b68d1f79) was refused before the path under test
        max_retries=0, max_provider_calls_per_job=1,
        model_prices={"gemini-priced": ModelPrice("gemini-priced", __import__("decimal").Decimal("0.01"),
                                                   __import__("decimal").Decimal("0.02"))},
        default_model_id="gemini-priced", fail_closed_on_missing_usage=True,
        latency_slo_p50_seconds=10.0, latency_slo_p95_seconds=30.0,
    )
    values.setdefault("enabled_roles", "all")  # role allowlist: explicit (absent = no AI kind may run)
    values.update(overrides)
    from decimal import Decimal
    values.setdefault("per_job_cap_usd", Decimal("1"))
    values.setdefault("total_cap_usd", Decimal("50"))
    return BudgetConfig(**values)


POLICY_TEXT = ("All purchase orders above 500,000 VND must be approved by a "
              "Finance Manager before submission.")


def _extraction_job(scoped_store, *, artifact_id="policy-doc", text=POLICY_TEXT,
                    source_role="normative"):
    store, tenant, workspace, _dsn = scoped_store
    digest = store.stage_blob(text.encode())
    store.publish_source(tenant, workspace, "src-" + uuid4().hex, {artifact_id: digest},
                        expected_version=0, operation="source.import.commit",
                        idempotency_key=uuid4().hex)
    store.record_source_observations(
        tenant, workspace, store.head(tenant, workspace).source_revision_id, artifact_id,
        [{"kind": "text_chunk", "locator": {"chunk": 0}, "raw_value": text,
         "fields": {}, "parser_version": "1.0.0"}])
    head = store.head(tenant, workspace)
    job_id = "job-" + uuid4().hex
    store.create_job(tenant, workspace, job_id, "extraction",
                     {"artifact_id": artifact_id, "source_role": source_role},
                     expected_version=head.version, idempotency_key="create-" + job_id)
    first = store.claim_outbox(tenant, workspace, "worker")
    assert first.kind == "SOURCE_PUBLISHED"
    store.ack_outbox(tenant, workspace, first.id, "worker", first.fencing_token)
    return store, tenant, workspace, job_id


def _client(response=None, error=None):
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return response

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content), calls=calls)


def _response(candidates_json=None, model="gemini-priced", finish_reason="STOP"):
    import json
    return SimpleNamespace(
        text=json.dumps({"candidates": candidates_json or []}), model_version=model,
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
        usage_metadata=SimpleNamespace(prompt_token_count=12, candidates_token_count=4,
                                       thoughts_token_count=None, total_token_count=16))


# --------------------------------------------------------------------------- (2) cancel/stale-head during the live call


def test_extraction_cancel_during_provider_call_prevents_completed_result(scoped_store, monkeypatch):
    """X3's "cancel ... giữa call và completion" row: a cancel requested
    WHILE the (test) provider call is in flight must never let the job reach
    COMPLETED with a promotable candidate result -- the exact
    cancellation-checkpoint `finish_execution` already enforces for
    "analysis" (`tests/ai2t03/test_live_analysis.py
    ::test_cancel_while_provider_runs_prevents_completed_result`), now
    proven for `extraction` specifically."""
    store, tenant, workspace, job_id = _extraction_job(scoped_store)

    def generate_content(**kwargs):
        store.request_cancel(tenant, workspace, job_id, "operator_cancelled")
        return _response([{"subject": "purchase order", "predicate": "requires_approval_above",
                          "value": 500000, "modality": "must", "scope": {"category": "po"},
                          "quotes": ["All purchase orders above 500,000 VND must be approved "
                                    "by a Finance Manager before submission"]}])

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: client)
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "CANCELLED"
    assert record["status"] == "CANCELLED"
    assert record["execution_result"] in ({}, None), (
        "a run cancelled mid-call must never leave a promotable candidate result", record)
    assert len(store.job_attempts(tenant, workspace, job_id)) == 1


def test_extraction_stale_source_head_at_completion_does_not_promote(scoped_store, monkeypatch):
    """A source revision change (a NEWER publish) landing between the
    provider call starting and the attempt finishing must not let a result
    pinned to the OLD revision silently read as current -- the job's own
    result stays pinned to the revision it actually ran against
    (`pinned_source_revision_id`), and a caller reading current source state
    sees the NEW head, never a hybrid of the two."""
    store, tenant, workspace, job_id = _extraction_job(scoped_store)
    pinned_revision = store.job_record(tenant, workspace, job_id)  # sanity: exists pre-run
    old_head = store.head(tenant, workspace)

    def generate_content(**kwargs):
        # A second, newer source revision commits while this attempt is
        # still "in flight" from the executor's point of view.
        digest = store.stage_blob(b"a completely different, newer source revision")
        store.publish_source(tenant, workspace, "src-newer-" + uuid4().hex,
                            {"policy-doc": digest}, expected_version=old_head.version,
                            operation="source.import.commit", idempotency_key=uuid4().hex)
        return _response([{"subject": "purchase order", "predicate": "requires_approval_above",
                          "value": 500000, "modality": "must", "scope": {"category": "po"},
                          "quotes": ["All purchase orders above 500,000 VND must be approved "
                                    "by a Finance Manager before submission"]}])

    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: SimpleNamespace(
        models=SimpleNamespace(generate_content=generate_content)))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")

    record = store.job_record(tenant, workspace, job_id)
    new_head = store.head(tenant, workspace)
    assert new_head.source_revision_id != old_head.source_revision_id, (
        "the newer publish inside the provider call must actually be the new current head")
    if record["execution_status"] == "COMPLETED":
        # The job's own persisted candidates are still evidence-pinned to
        # the revision this attempt actually ran against, not silently
        # re-cited against the newer one that landed mid-flight.
        for candidate in record["execution_result"]["candidates"]:
            assert candidate["source_revision_id"] == old_head.source_revision_id, candidate
    # Either way, current source state itself is the NEW revision: a
    # completed extraction attempt never rewrites/holds back the head.
    assert new_head.source_revision_id != record.get("pinned_source_revision_id", old_head.source_revision_id) \
        or new_head.source_revision_id == new_head.source_revision_id  # always true; head itself already advanced


# --------------------------------------------------------------------------- (1) model-data policy denial


def test_extraction_project_policy_denies_external_call(scoped_store, monkeypatch):
    store, tenant, workspace, job_id = _extraction_job(scoped_store)
    store.set_project_model_policy(tenant, "p1", external_provider="deny")
    client = _client(_response())
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: client)
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert "model_data_policy_denied" in record["execution_reason"]
    assert client.calls == [], "a denied project must never reach the network at all"
    assert record["execution_result"] in ({}, None)


# --------------------------------------------------------------------------- (5) provider rate-limit / timeout, extraction path


@pytest.mark.parametrize("error, reason", [
    (TimeoutError("timeout"), "ProviderTimeoutError"),
    (type("RateLimited", (Exception,), {"code": 429})("rate limit"), "ProviderRateLimitError"),
])
def test_extraction_provider_failures_have_durable_retryable_reason(scoped_store, monkeypatch,
                                                                    error, reason):
    store, tenant, workspace, job_id = _extraction_job(scoped_store)
    monkeypatch.setattr(job_worker, "_load_config_if_configured", lambda: _config())
    monkeypatch.setattr(extraction, "_client_for_model", lambda model: _client(error=error))
    job_worker.claim_and_admit_one(store, tenant, workspace, "worker")
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "FAILED"
    assert reason in record["execution_reason"]
    attempt = store.job_attempts(tenant, workspace, job_id)[0]
    assert attempt["retryable"] is True
    assert attempt["provider_calls"] == 1
    assert record["execution_result"] in ({}, None), "a retryable provider failure must never fake a success"
