"""Pitch change.brief fixtures: real PostgreSQL (throwaway, own DSN), real import path (prepare + commit + source_ingest
job), real extraction job through the worker with a STUB provider (no model call), optional D0 executor arm.

Documents: the Harbor public bundle `input/` files that ripple/_testdata/generated/operator/IMPORT_MAPPING.md allows
(app/*.py, config/*.yaml, data/*.xlsx, docs/*.md), read-only. Nothing from evaluator_private/, authoring/, reports/.
DSN: env PITCH_BRIEF_TEST_DSN (default: loopback port 55831, user postgres, throwaway database).
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest

DSN = os.environ.get("PITCH_BRIEF_TEST_DSN",
                     "host=127.0.0.1 port=55831 dbname=pbrief user=postgres password=localtest")
REPO = pathlib.Path(__file__).resolve().parents[2]
_MAIN = REPO.parents[2] if REPO.parent.name == "worktrees" else REPO
HARBOR_INPUT = pathlib.Path(os.environ.get(
    "HARBOR_INPUT", str(_MAIN / "ripple" / "_testdata" / "generated" / "bundles" / "harbor_demo" / "input")))
ALLOWED = ("app/*.py", "config/*.yaml", "data/*.xlsx", "docs/*.md")
PREFIX = "harbor_demo"


class MemoryBlobs:
    def __init__(self):
        self.data: dict = {}

    def put_immutable(self, digest, content):
        self.data.setdefault(digest, content)

    def read(self, digest):
        return self.data[digest]


#: The REAL candidate budget config (not a copy) and the ONE sandbox limits file: tests run against what will be deployed.
REAL_BUDGET_PATH = REPO / "docs" / "runbooks" / "pitch_budget_config.yaml"
SANDBOX_LIMITS_PATH = REPO / "docs" / "runbooks" / "pitch_sandbox_limits.json"


def sandbox_config(workspace_id, seed_revision_id, **override):
    """RIPPLE_SANDBOX_CONFIG JSON for one workspace = the canonical limits file (+ explicit per-test overrides)."""
    limits = {k: v for k, v in json.loads(SANDBOX_LIMITS_PATH.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    limits.update(override, seed_revision_id=seed_revision_id)
    return json.dumps({"workspaces": {workspace_id: limits}})


#: Option A (user-approved): the Pitch workspace imports ONLY these six public Harbor files (originals untouched).
PITCH_SCOPE = ("config/approval.yaml", "config/workflow.yaml", "docs/procurement_policy.md",
               "docs/finance_desk_guide.md", "data/approval_matrix.xlsx", "app/calculations.py")


def harbor_files():
    out = []
    for rel in PITCH_SCOPE:
        p = HARBOR_INPUT / rel
        out.append((f"{PREFIX}/{rel}", p.read_bytes()))
    return out


EXTRACT_MODEL, GAP_MODEL = "gemini-3.8-flash", "gemini-3.8-flash"   # gap.answer is a pinned 3.8 role now
# Mirrors docs/runbooks/w1_prod_budget_config.yaml (the candidate config shape): real model ids and prices, pinned
# roles, ceilings 24000/12288, 1 call/job, 1 retry, per-job 0.30, concurrency 2. total cap = the Pitch cap 12.82.
BUDGET = """
default_model_id: gemini-3.5-flash-lite
role_model_ids:
  extraction: gemini-3.8-flash
  change.draft: gemini-3.8-flash
model_prices:
  gemini-3.5-flash-lite:
    input_usd_per_1k: "0.0003"
    output_usd_per_1k: "0.0025"
  gemini-3.8-flash:
    input_usd_per_1k: "0.0015"
    output_usd_per_1k: "0.0075"
price_snapshot_date: "2026-09-30"
price_snapshot_max_age_days: 30
request_timeout_seconds: 90
per_job_cap_usd: "0.30"
total_cap_usd: "12.82"
max_input_tokens_per_call: 24000
max_output_tokens: 12288
max_retries: 1
max_provider_calls_per_job: 1
max_concurrent_jobs_per_workspace: 2
fail_closed_on_missing_usage: true
latency_slo_p50_seconds: 30
latency_slo_p95_seconds: 90
"""


@pytest.fixture
def env(monkeypatch, tmp_path):
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("pitch_brief tests need a disposable PostgreSQL; set PITCH_BRIEF_TEST_DSN")
    if not HARBOR_INPUT.is_dir():
        pytest.skip("Harbor public input bundle not found")
    from fastapi.testclient import TestClient
    from infra import judge, v2_source_routes
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store

    nonce = uuid4().hex[:12]
    tenant, project, ws = f"t-{nonce}", f"p-{nonce}", f"ws-{nonce}"
    blobs = MemoryBlobs()
    store = PostgresV2Store(DSN, blobs)
    store.migrate()
    store.create_workspace(tenant, project, ws)
    store.set_project_model_policy(tenant, project, external_provider="allow")  # real guard; stub provider only
    ids = PostgresIdentityStore(DSN)
    ids.migrate()
    admin, req = f"a-{nonce}", f"r-{nonce}"
    ids.bootstrap_admin(tenant, project, ws, admin, f"cred-a-{nonce}")
    ids.create_account(req, f"cred-r-{nonce}")
    ids.grant_membership(tenant, project, ws, req, ("requester",))
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    monkeypatch.setenv("RIPPLE_V2_BLOB_BUCKET", "test-bucket")
    monkeypatch.setattr(v2_source_routes, "_blob_port", lambda: blobs)
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(REAL_BUDGET_PATH))

    def client(account, cred):
        c = TestClient(judge.app, base_url="https://judge.test")
        c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
        r = c.post("/v2/auth/login", json={"account_id": account, "credential": cred})
        assert r.status_code == 200, r.text
        return c

    return SimpleNamespace(tenant=tenant, ws=ws, store=store, blobs=blobs, dsn=DSN, admin=admin, req=req,
                           admin_client=lambda: client(admin, f"cred-a-{nonce}"),
                           req_client=lambda: client(req, f"cred-r-{nonce}"), nonce=nonce)


def import_harbor(env, client):
    """The REAL path: POST /v2/sources/imports, commit, then the worker runs the source_ingest job."""
    from infra.worker.job_worker import claim_and_admit_one
    files = harbor_files()
    body = {"workspace_id": env.ws, "idempotency_key": uuid4().hex,
            "files": [{"path": p, "content_b64": base64.b64encode(c).decode()} for p, c in files]}
    r = client.post("/v2/sources/imports", json=body)
    assert r.status_code == 200, r.text
    dto = r.json()
    r = client.post(f"/v2/sources/imports/{dto['import_id']}/commit",
                    json={"workspace_id": env.ws, "expected_version": 0, "acknowledged_skips": []})
    assert r.status_code == 200, r.text
    claim = claim_and_admit_one(env.store, env.tenant, env.ws, "ingest-worker")
    assert claim is not None
    head = env.store.head(env.tenant, env.ws)
    view = env.store.source_revision_view(env.tenant, env.ws, head.source_revision_id)
    return {a["logical_path"]: a["artifact_id"] for a in view["artifacts"]}, head.source_revision_id


def drain(env):
    from infra.worker.job_worker import claim_and_admit_one
    n = 0
    while claim_and_admit_one(env.store, env.tenant, env.ws, "w") is not None:
        n += 1
        if n > 60:
            break
    return n


def create_job(client, env, kind, payload):
    job_id = f"job-{kind.replace('.', '-')}-{uuid4().hex[:10]}"
    r = client.post("/v2/jobs", json={"workspace_id": env.ws, "kind": kind, "job_id": job_id, "payload": payload})
    return job_id, r


def job_body(client, env, job_id):
    return client.get(f"/v2/jobs/{job_id}", params={"workspace_id": env.ws}).json()


def checks_ready(env):
    """What the page does before it asks for a draft: the relation coverage and the conflict check of the CURRENT revision exist (deterministic, no model)."""
    r = env.req_client().post("/v2/change/coverage/ensure", json={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    drain(env)


class LiveWorker:
    """What the live server's own worker does: take queued jobs as they come (a thread; start() / stop())."""

    def __init__(self, env):
        import threading
        self.env, self._stop, self._t = env, threading.Event(), threading.Thread(target=self._run, daemon=True)

    def _run(self):
        import time
        from infra.worker.job_worker import claim_and_admit_one
        while not self._stop.is_set():
            try:
                if claim_and_admit_one(self.env.store, self.env.tenant, self.env.ws, "live-worker") is None:
                    self._stop.wait(0.3)
            except Exception:
                time.sleep(0.5)

    def start(self):
        self._t.start()
        return self

    @property
    def stop(self):
        """`worker.stop.set()` returns only once the loop has ended: a job still in flight would otherwise keep querying while the next test's
        `store.migrate()` takes its table locks (seen as `deadlock detected` and a fixture error in the next test)."""
        return _Stop(self._stop, self._t)


class _Stop:
    def __init__(self, event, thread):
        self._event, self._thread = event, thread

    def is_set(self):
        return self._event.is_set()

    def set(self, timeout=60):
        import threading
        self._event.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)
            assert not self._thread.is_alive(), f"live worker still running {timeout} s after stop"

