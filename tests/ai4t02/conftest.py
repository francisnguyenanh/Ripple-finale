"""Relation test fixtures.

Reuses the extraction real-PostgreSQL / real-`infra.judge.app` / real-worker-claim
fixture (`tests.ai4t01.conftest.setup`) instead of re-inventing it, building on an already
reviewed real-boundary fixture rather than a fresh mock. Only NEW here:
`seed_extraction_candidates`, a helper that produces real, persisted
`RuleCandidate` rows through the REAL `extraction` job kind, so relation tests have real extraction output to consume instead of
hand-built fixture dicts that could silently drift from the real schema.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from infra.worker import job_worker, extraction

from tests.ai4t01.conftest import setup, drain_outbox, login, publish_with_chunk  # noqa: F401


def _client(response):
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        return response

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content), calls=calls)


def _response(candidates_json, model="test-model"):
    return SimpleNamespace(
        text=json.dumps({"candidates": candidates_json}),
        model_version=model,
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=30,
                                       thoughts_token_count=None, total_token_count=70))


def seed_extraction_candidates(setup_ctx, monkeypatch, *, artifact_id, text, candidates_json):
    """Publishes `text` as a source artifact, runs a REAL `extraction` job
    against it with a monkeypatched
    provider response, and returns the persisted job body
    (`GET /v2/jobs/{id}` response JSON) containing real `RuleCandidate` dicts
    under `execution_result.candidates`. This is the real relation
    input boundary (extraction candidates), not a hand-authored fixture shape.
    """
    tenant, workspace, requester, credential, browser, store = (
        setup_ctx[k] for k in ("tenant", "workspace", "requester", "credential", "browser", "store"))
    publish_with_chunk(store, tenant, workspace, artifact_id=artifact_id, text=text)
    drain_outbox(store, tenant, workspace)

    client = browser()
    login(client, requester, credential)
    job_id = f"job-extract-{uuid4().hex}"
    response = client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "extraction", "job_id": job_id,
        "payload": {"artifact_id": artifact_id, "source_role": "normative"}})
    assert response.status_code == 200, response.text

    monkeypatch.setattr(extraction, "_client_for_model",
                       lambda model: _client(_response(candidates_json)))
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "extraction-worker")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim

    status = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["execution_status"] == "COMPLETED", body
    return body
