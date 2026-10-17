"""Real import path (prepare -> commit -> source_ingest on a LOCAL TEST Postgres) -> run context builder -> scorer.
Skipped unless PITCH_SCORER_TEST_DSN points at a throwaway Postgres owned by this test run. Own fixture, not the Pitch set.
Predictions here are produced by D0's public extract_from_store (read-only use); nothing in pitch/d0 is modified."""
from __future__ import annotations

import base64
import copy
import hashlib
import os
from uuid import uuid4

import pytest

DSN = os.environ.get("PITCH_SCORER_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="set PITCH_SCORER_TEST_DSN to a throwaway Postgres")

POLICY = b"approval:\n  region: JP\n  currency: JPY\n  max_amount: 500000\n"
PATH = "policy.yaml"


class MemoryBlobs:
    def __init__(self):
        self.data = {}

    def put_immutable(self, digest, content):
        self.data.setdefault(digest, content)

    def read(self, digest):
        return self.data[digest]


def make_env(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from infra import judge, v2_source_routes
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store

    nonce = uuid4().hex[:12]
    tenant, project, ws = f"t-{nonce}", f"p-{nonce}", f"w-{nonce}"
    blobs = MemoryBlobs()
    store = PostgresV2Store(DSN, blobs)
    store.migrate()
    store.create_workspace(tenant, project, ws)
    ids = PostgresIdentityStore(DSN)
    ids.migrate()
    acc, cred = f"a-{nonce}", f"cred-{nonce}"
    ids.bootstrap_admin(tenant, project, ws, acc, cred)
    cfg = tmp_path / f"budgets-{nonce}.yaml"
    cfg.write_text("default_model_id: m\nmodel_prices:\n  m:\n    input_usd_per_1k: '0.01'\n"
                   "    output_usd_per_1k: '0.01'\nmax_output_tokens: 2048\nmax_input_tokens_per_call: 8192\n"
                   "max_retries: 1\nmax_provider_calls_per_job: 1\nper_job_cap_usd: '1.00'\n"
                   "total_cap_usd: '50.00'\nfail_closed_on_missing_usage: true\nlatency_slo_p50_seconds: 5\n"
                   "latency_slo_p95_seconds: 20\n", encoding="utf-8")
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    monkeypatch.setenv("RIPPLE_V2_BLOB_BUCKET", "test-bucket")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(v2_source_routes, "_blob_port", lambda: blobs)
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": acc, "credential": cred}).status_code == 200
    env = {"tenant": tenant, "ws": ws, "store": store, "client": c}
    # real import API: prepare -> commit -> worker runs source_ingest
    from infra.worker.job_worker import claim_and_admit_one
    r = c.post("/v2/sources/imports", json={"workspace_id": ws, "idempotency_key": uuid4().hex,
                                            "files": [{"path": PATH, "content_b64": base64.b64encode(POLICY).decode()}]})
    assert r.status_code == 200, r.text
    imp = r.json()["import_id"]
    r = c.post(f"/v2/sources/imports/{imp}/commit", json={"workspace_id": ws, "expected_version": 0,
                                                          "acknowledged_skips": []})
    assert r.status_code == 200, r.text
    n = 0
    while claim_and_admit_one(store, tenant, ws, "ing-worker"):
        n += 1
        assert n < 10
    env["rev"] = store.head(tenant, ws).source_revision_id
    return env


def label():
    return {"artifact_id": "P1", "file": PATH, "format": "yaml", "primary_class": "C1", "traps": [],
            "soft_guidance": [], "observed_facts": [], "non_input_notes": [],
            "gold_rules": [{"id": "P1-1", "class": "C1", "subject": "approval", "predicate": "max amount",
                            "comparator": "==", "value": 500000, "unit": "JPY", "scope": {"region": "JP"},
                            "modality": "must", "exceptions": [], "quote": "", "status": "gold"}]}


def predictions(env):
    from pitch.d0 import extract_from_store
    view = env["store"].source_revision_view(env["tenant"], env["ws"], env["rev"])
    art = next(a["artifact_id"] for a in view["artifacts"] if a["logical_path"] == PATH)
    return extract_from_store(env["store"], env["tenant"], env["ws"], env["rev"], art, "normative")


def run_score(env, ctx, trusted, preds, run_id="run-A"):
    from pitch.eval.scorer import score
    out = score({"P1": label()}, {"P1": {"text": POLICY, "format": "yaml", "file": PATH}},
                {run_id: {"P1": preds}}, run_contexts={run_id: ctx} if ctx is not None else {},
                trusted_context_hashes={run_id: trusted} if trusted is not None else {})
    return out["runs"][run_id]


def test_real_import_context_scores_strict_and_hash_is_product_blob_hash(monkeypatch, tmp_path):
    from pitch.eval.run_context import build_run_context_from_import, trusted_hash_from_store
    env = make_env(monkeypatch, tmp_path)
    ctx, h = build_run_context_from_import(env["store"], env["tenant"], env["ws"], env["rev"], "run-A",
                                           {"P1": PATH}, imported_files={PATH: POLICY})
    assert ctx["artifact_content_hashes"]["P1"] == "sha256:" + hashlib.sha256(POLICY).hexdigest()
    assert h == trusted_hash_from_store(env["store"], env["tenant"], env["ws"], env["rev"], "run-A", {"P1": PATH})
    preds = predictions(env)
    ev = preds["candidates"][0]["evidence"][0]
    assert "tenant_id" not in ev  # product EvidenceRef has no tenant field; ownership comes from the context
    r = run_score(env, ctx, h, preds)
    assert r["context_state"] == "ok"
    assert r["overall"]["found"] == 1 and r["evidence"]["correct"] == 1
    # product result dict carries no content_hash: row reported unverifiable, not gating under the default policy
    assert r["evidence_hash_row"]["unverifiable"] == 1 and "content_hash" not in ev


def test_other_tenant_workspace_and_other_run_contexts_rejected(monkeypatch, tmp_path):
    from infra.adapters.v2_postgres import TenantScopeError  # noqa: F401  (raised by the real store)
    from pitch.eval.run_context import build_run_context_from_import
    a = make_env(monkeypatch, tmp_path)
    preds = predictions(a)
    b = make_env(monkeypatch, tmp_path)  # another tenant AND workspace with the same file content
    ctx_b, h_b = build_run_context_from_import(b["store"], b["tenant"], b["ws"], b["rev"], "run-A", {"P1": PATH})
    r = run_score(a, ctx_b, h_b, preds)  # tenant B's context used for tenant A's run
    assert r["context_state"] == "ok" and r["evidence"]["foreign_tenant_workspace"] == 1
    assert r["overall"]["found"] == 0 and r["overall"]["T"] == 1
    # builder refuses a revision foreign to the tenant / workspace
    with pytest.raises(Exception) as e1:
        build_run_context_from_import(a["store"], b["tenant"], a["ws"], a["rev"], "run-A", {"P1": PATH})
    with pytest.raises(Exception) as e2:
        build_run_context_from_import(a["store"], a["tenant"], b["ws"], a["rev"], "run-A", {"P1": PATH})
    assert type(e1.value).__name__ == "TenantScopeError" == type(e2.value).__name__
    # a context built for another run
    ctx_other, h_other = build_run_context_from_import(a["store"], a["tenant"], a["ws"], a["rev"], "run-B",
                                                       {"P1": PATH})
    r = run_score(a, ctx_other, h_other, preds, run_id="run-A")
    assert r["context_state"] == "wrong_run" and r["overall"]["found"] == 0


def test_tampered_context_and_wrong_hash_rejected(monkeypatch, tmp_path):
    from pitch.eval.run_context import build_run_context_from_import, trusted_hash_from_store
    from pitch.eval.scorer import make_run_context
    env = make_env(monkeypatch, tmp_path)
    preds = predictions(env)
    ctx, h = build_run_context_from_import(env["store"], env["tenant"], env["ws"], env["rev"], "run-A", {"P1": PATH})
    # edited and re-hashed context; the independent (store-recomputed) hash no longer matches
    forged = make_run_context(ctx["tenant_id"], ctx["workspace_id"], ctx["source_revision_ids"] + ["x"],
                              ctx["artifact_revision_ids"], run_id="run-A",
                              artifact_content_hashes=ctx["artifact_content_hashes"])
    independent = trusted_hash_from_store(env["store"], env["tenant"], env["ws"], env["rev"], "run-A", {"P1": PATH})
    r = run_score(env, forged, independent, preds)
    assert r["context_state"] == "tampered" and r["overall"]["found"] == 0
    # wrong EvidenceRef.content_hash in the prediction
    bad = copy.deepcopy(preds)
    bad["candidates"][0]["evidence"][0]["content_hash"] = "sha256:" + "0" * 64
    r = run_score(env, ctx, h, bad)
    assert r["evidence"]["hash_mismatch"] == 1 and r["overall"]["found"] == 0 and r["overall"]["partial"] == 1
