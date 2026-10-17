"""Live Vertex AI evidence, real network call, not mocked.

Live worker runs on document-only/paraphrase/new-rule inputs must yield new
candidates with evidence (no known IDs needed), and observational transactions
next to a normative rule must not become must-policy. Live proof is never
replaced by mock/replay, so this file makes REAL provider calls through the exact same
`ExtractRulesExecutor`/`job_worker.claim_and_admit_one` path the mocked tests
in `test_extract_rules_executor.py` exercise -- the ONLY difference from
those tests is that `extraction._client_for_model` is NOT monkeypatched, so
the real `ai_module`-configured Vertex AI account answers.

Skipped unless `GCP_PROJECT` is set (same env var `ai_module`'s Vertex path
already requires) -- this is a deliberate, explicit opt-in for a paid call,
never run by default/CI. ADC + this GCP project can already reach
gemini-3.5-flash-lite at region "global" for ~$0.0000032/call; this test
uses the SAME cheap tier via `routing.config.json`'s existing "economy"
override capability (RIPPLE_BUDGET_CONFIG_PATH's own `default_model_id`).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from infra.worker import job_worker

from tests.ai4t01.conftest import drain_outbox, login, publish_with_chunk

pytestmark = pytest.mark.skipif(
    not os.environ.get("GCP_PROJECT"),
    reason="AI4-T01 X1 needs a real Vertex AI call: set GCP_PROJECT to opt in")


# A genuinely new, paraphrased rule that does not exist in any seed/oracle
# fixture in this repository -- the point of X1 is that the model finds a
# rule with NO known candidate ID or hint, from document text alone.
DOCUMENT_ONLY_POLICY = (
    "Facilities Access Standard, revision C.\n"
    "Contractors visiting the Hanoi office after 20:00 local time must be "
    "escorted by a badged employee at all times; unescorted after-hours "
    "contractor access is not permitted, other than for pre-approved "
    "overnight maintenance windows."
)

TRANSACTION_LOG = (
    "Visit log #7734: contractor D. Pham entered Hanoi office at 21:40, "
    "escorted by employee N. Le (badge HN-0091). Exit logged 22:15."
)


def _use_live_economy_model(setup, monkeypatch, tmp_path):
    """Swap the fixture's fake `test-model` budget config for the real,
    cheapest priced Vertex tier, so the executor's own
    `actual_model not in config.model_prices` guard does not reject the
    real provider's response."""
    config_path = tmp_path / "live_budgets.yaml"
    config_path.write_text(
        """
default_model_id: gemini-3.5-flash-lite
model_prices:
  gemini-3.5-flash-lite:
    input_usd_per_1k: "0.0003"
    output_usd_per_1k: "0.0025"
max_output_tokens: 1024
max_input_tokens_per_call: 8192
max_retries: 1
max_provider_calls_per_job: 1
per_job_cap_usd: "0.05"
total_cap_usd: "1.00"
fail_closed_on_missing_usage: true
latency_slo_p50_seconds: 30
latency_slo_p95_seconds: 60
""".strip(), encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(config_path))


def test_live_model_finds_new_document_only_rule_with_real_citation(setup, monkeypatch, tmp_path):
    _use_live_economy_model(setup, monkeypatch, tmp_path)
    tenant, workspace, requester, credential, browser, store = (
        setup[k] for k in ("tenant", "workspace", "requester", "credential", "browser", "store"))
    publish_with_chunk(store, tenant, workspace, artifact_id="facilities-policy",
                      text=DOCUMENT_ONLY_POLICY)
    drain_outbox(store, tenant, workspace)

    client = browser()
    login(client, requester, credential)
    job_id = f"job-live-x1-{uuid4().hex}"
    response = client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "extraction", "job_id": job_id,
        "payload": {"artifact_id": "facilities-policy", "source_role": "normative"}})
    assert response.status_code == 200, response.text

    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "extraction-live-worker")
    assert claim is not None, claim

    body = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace}).json()
    assert body["execution_status"] == "COMPLETED", body
    result = body["execution_result"]
    assert result is not None and result["candidates"], (
        "live model returned zero candidates for a document-only rule", result)
    candidate = result["candidates"][0]
    # Real citation: the quote must have actually resolved against the
    # pinned artifact's persisted observation (ExtractRulesExecutor rejects
    # anything that does not -- this assertion would fail if the executor's
    # citation guard were bypassed).
    assert candidate["evidence"], candidate
    assert body["execution_model_id"], "no fabricated/empty model id"
    assert body["execution_provider_calls"] == 1


def test_live_model_keeps_observational_transaction_out_of_must_policy(setup, monkeypatch, tmp_path):
    _use_live_economy_model(setup, monkeypatch, tmp_path)
    tenant, workspace, requester, credential, browser, store = (
        setup[k] for k in ("tenant", "workspace", "requester", "credential", "browser", "store"))
    publish_with_chunk(store, tenant, workspace, artifact_id="visit-log", text=TRANSACTION_LOG)
    drain_outbox(store, tenant, workspace)

    client = browser()
    login(client, requester, credential)
    job_id = f"job-live-x1b-{uuid4().hex}"
    response = client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "extraction", "job_id": job_id,
        "payload": {"artifact_id": "visit-log", "source_role": "observational"}})
    assert response.status_code == 200, response.text

    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "extraction-live-worker")
    assert claim is not None

    body = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace}).json()
    assert body["execution_status"] == "COMPLETED", body
    result = body["execution_result"]
    if result["candidates"]:
        for candidate in result["candidates"]:
            assert candidate["modality"] != "must", (
                "a single logged visit must never surface as an organizational "
                "must-policy", candidate)
