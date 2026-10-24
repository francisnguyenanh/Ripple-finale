"""Extraction test fixtures: real PostgreSQL, real `infra.judge.app`, real
worker claim path -- same convention as `tests/ai2t03/test_e2e_quota_and_worker.py`
and `tests/ai2t04/conftest.py` (reused, not reinvented). The only non-real
piece is the network client itself, monkeypatched exactly like
`tests/ai2t03/test_live_analysis.py` does for the sibling "analysis" kind.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg2
import pytest


DSN = os.environ.get(
    "AI4_T01_TEST_DSN",
    os.environ.get("AI2_T03_TEST_DSN",
                   os.environ.get("AI1_T03_TEST_DSN",
                                  "host=127.0.0.1 port=55434 dbname=ai1t04 "
                                  "user=postgres password=localtest")),
)


class MemoryBlobs:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put_immutable(self, digest, content):
        self.data.setdefault(digest, content)

    def read(self, digest):
        return self.data[digest]


@pytest.fixture
def setup(monkeypatch, tmp_path):
    try:
        with psycopg2.connect(DSN, connect_timeout=2):
            pass
    except (psycopg2.OperationalError, psycopg2.ProgrammingError):
        pytest.skip("AI4-T01 tests need a disposable PostgreSQL; set AI4_T01_TEST_DSN")

    from fastapi.testclient import TestClient

    from infra import judge
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store

    nonce = uuid4().hex
    tenant, project, workspace = f"tenant-{nonce}", f"project-{nonce}", f"workspace-{nonce}"
    admin, requester = f"admin-{nonce}", f"requester-{nonce}"
    credential = f"cred-{nonce}"

    blobs = MemoryBlobs()
    store = PostgresV2Store(DSN, blobs)
    store.migrate()
    store.create_workspace(tenant, project, workspace)

    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.bootstrap_admin(tenant, project, workspace, admin, credential)
    identities.create_account(requester, credential + "-req")
    # Extraction is gated by source.import, same as analysis; the
    # requester role must already carry it (checked, not assumed).
    identities.grant_membership(tenant, project, workspace, requester, ("requester",))

    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    monkeypatch.setenv("RIPPLE_V2_BLOB_BUCKET", "test-bucket")

    config_path = tmp_path / "budgets.yaml"
    config_path.write_text(
        """
default_model_id: test-model
model_prices:
  test-model:
    input_usd_per_1k: "0.01"
    output_usd_per_1k: "0.01"
max_output_tokens: 2048
max_input_tokens_per_call: 8192
max_retries: 1
max_provider_calls_per_job: 1
per_job_cap_usd: "1.00"
total_cap_usd: "50.00"
fail_closed_on_missing_usage: true
latency_slo_p50_seconds: 5
latency_slo_p95_seconds: 20
enabled_roles: all
""".strip(), encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(config_path))

    def browser():
        client = TestClient(judge.app, base_url="https://judge.test")
        client.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
        return client

    return {"tenant": tenant, "project": project, "workspace": workspace,
           "admin": admin, "requester": requester, "credential": credential + "-req",
           "browser": browser, "store": store}


def login(client, account, credential):
    response = client.post("/v2/auth/login", json={"account_id": account, "credential": credential})
    assert response.status_code == 200, response.text
    return response


def drain_outbox(store, tenant, workspace) -> None:
    while True:
        claim = store.claim_outbox(tenant, workspace, "drain-worker", lease_seconds=5)
        if claim is None:
            return
        store.ack_outbox(tenant, workspace, claim.id, "drain-worker", claim.fencing_token)


def publish_with_chunk(store, tenant, workspace, *, artifact_id="policy-doc", text: str,
                       revision_id: str | None = None) -> str:
    """Publish one source revision with a single artifact whose bytes are
    `text`, PLUS a real, persisted observation ("text_chunk") whose
    `raw_value` is that same text -- the exact shape the ingest
    pipeline leaves behind, so `ExtractRulesExecutor`'s citation resolver has
    something real to check a model-returned quote against."""
    revision_id = revision_id or f"rev-{uuid4().hex}"
    digest = store.stage_blob(text.encode("utf-8"))
    store.publish_source(tenant, workspace, revision_id, {artifact_id: digest},
                        expected_version=store.head(tenant, workspace).version,
                        operation="source.import.commit", idempotency_key=uuid4().hex)
    store.record_source_observations(
        tenant, workspace, revision_id, artifact_id,
        [{"kind": "text_chunk", "locator": {"chunk": 0}, "raw_value": text,
         "fields": {}, "parser_version": "1.0.0"}])
    return revision_id
