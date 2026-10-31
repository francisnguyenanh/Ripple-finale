"""Staging finding 2026-10-31 (C21, production concurrency limit 1): after a committed source refresh the worker consumed the published event, queued the
refresh's invalidation jobs, then tried to create the re-extraction through the per-workspace concurrency ceiling, which the just-queued jobs had already
filled; the refusal propagated out of the claim loop, the worker service exited, the event stayed unacknowledged and every later execution died on the
redelivery: nothing ever drained. Two guards, both on the real routes, store and worker (stub provider):
  1. the system's re-extraction of a committed refresh is admitted regardless of the user concurrency ceiling (bounded by the refresh impact and by the project
     budget), so the follow-up the refresh exists for is scheduled on the first pass;
  2. a transient failure inside the refresh consumer never ends the worker loop: the claim is left for redelivery and the loop continues with the queued jobs."""
from __future__ import annotations

import os
import time

import psycopg2

import pytest
from types import SimpleNamespace

from infra.worker import job_worker, refresh_impact
from tests.final_loop.test_syn02_reconcile import refresh_yaml
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import drain


@pytest.fixture
def qa_env(env, monkeypatch):
    """The Harbor import with one real extraction job per artifact (stub provider), so a refresh of approval.yaml has a re-extraction to schedule."""
    ch = build(env, monkeypatch)
    return SimpleNamespace(env=env, admin=ch.admin, req=env.req_client(), monkeypatch=monkeypatch, ch=ch)


def _limit_one(q, tmp_path):
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read()
    assert "max_concurrent_jobs_per_workspace: 2" in cfg
    path = tmp_path / "budget_limit1.yaml"
    path.write_text(cfg.replace("max_concurrent_jobs_per_workspace: 2", "max_concurrent_jobs_per_workspace: 1"), encoding="utf-8")
    q.monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))


def _jobs(q, kind):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT job_id, execution_status FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind=%s ORDER BY job_id", (q.env.tenant, q.env.ws, kind))
        return cur.fetchall()


def _unacked(q, kind):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind=%s AND status IN ('PENDING','CLAIMED')", (q.env.tenant, q.env.ws, kind))
        return cur.fetchone()[0]


def test_a_refresh_under_a_concurrency_limit_of_one_still_schedules_its_re_extraction_and_the_loop_drains(qa_env, tmp_path):
    q = qa_env
    _limit_one(q, tmp_path)
    before = _jobs(q, "extraction")
    refresh_yaml(q, 1500000)                                                                       # commit + drain: the published event, the invalidations, the re-extraction
    after = _jobs(q, "extraction")
    assert len(after) == len(before) + 1, (before, after)                                           # guard 1: the follow-up was created on the first pass
    assert _unacked(q, "SOURCE_PUBLISHED") == 0                                                     # the refresh event is acknowledged, not left behind
    assert all(st in ("COMPLETED", "FAILED", "NOT_REQUIRED") for _, st in _jobs(q, "invalidation")) or _jobs(q, "invalidation") == []
    art_id = next(v for k, v in q.ch.arts.items() if str(k).endswith("approval.yaml"))                 # the chain maps logical path -> artifact id
    r = q.req.post("/v2/jobs", json={"workspace_id": q.env.ws, "kind": "extraction", "job_id": "job-user-after-refresh", "payload": {"artifact_id": art_id, "source_role": "normative"}})
    assert r.status_code == 200, r.text                                                             # a person's own submit is admitted once the refresh work drained
    drain(q.env)


def test_a_transient_failure_in_the_refresh_consumer_leaves_the_claim_for_redelivery_and_the_loop_goes_on(qa_env, tmp_path, monkeypatch):
    q = qa_env
    calls = {"n": 0}
    real = refresh_impact.consume_refresh_published

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected transient failure")
        return real(*a, **k)
    monkeypatch.setattr(refresh_impact, "consume_refresh_published", flaky)
    # a committed refresh without draining: the published event waits in the outbox
    import base64
    from uuid import uuid4
    from tests.pitch_brief.conftest import harbor_files
    files = [{"path": p, "content_b64": base64.b64encode(c.replace(b"standard_manager_threshold_jpy: 1000000", b"standard_manager_threshold_jpy: 1700000")).decode()} for p, c in harbor_files()]
    prep = q.admin.post("/v2/sources/refresh/prepare", json={"workspace_id": q.env.ws, "idempotency_key": uuid4().hex, "files": files}).json()
    assert q.admin.post("/v2/sources/refresh/commit", json={"workspace_id": q.env.ws, "refresh_id": prep["refresh_id"], "expected_version": prep["base"]["version"],
                                                           "expected_refresh_digest": prep["refresh_digest"], "acknowledged_deletions": []}).status_code == 200
    # the loop survives the injected failure (guard 2): the claim call returns, the event is still unacknowledged
    seen = []
    for _ in range(12):
        claim = job_worker.claim_and_admit_one(q.env.store, q.env.tenant, q.env.ws, "w-flaky", lease_seconds=1)
        if claim is None:
            break
        seen.append(claim.kind)
    assert calls["n"] >= 1 and "SOURCE_PUBLISHED" in seen
    assert _unacked(q, "SOURCE_PUBLISHED") == 1                                                     # left for redelivery, not acked, not lost
    time.sleep(1.5)                                                                                  # the short lease expires: the event is redelivered and consumed
    drain(q.env)
    assert calls["n"] >= 2 and _unacked(q, "SOURCE_PUBLISHED") == 0
