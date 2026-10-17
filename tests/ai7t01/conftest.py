"""Fixtures for the patch-generation ("proposal") tests.

Reuses `tests.ai4t01.conftest.setup`/`login`/`publish_with_chunk`/
`drain_outbox` (real PostgreSQL, real `infra.judge.app`, one "requester"
account); one shared resolution/fixture path, not reinvented. Adds the same real
"impact" and "simulation" job helpers used by the what-if preview tests,
since proposal generation takes "confirmed intent, impact, current
source/KM/epoch, simulation" as its real upstream inputs -- these must be
REAL, COMPLETED impact/simulation jobs, not invented shapes.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.worker import job_worker

from tests.ai4t01.conftest import DSN, drain_outbox, login, publish_with_chunk, setup  # noqa: F401


def submit_job(client, workspace, *, kind, payload=None, job_id=None):
    return client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": kind,
        "job_id": job_id or f"job-{kind}-{uuid4().hex}",
        "payload": payload or {}})


def create_real_impact_job(setup, client, workspace, *, candidate_ids=None,
                            existing_finding_ids=None):
    """Real, COMPLETED "impact" job -- reused as a
    real upstream report, not re-tested here."""
    tenant, store = setup["tenant"], setup["store"]
    job_id = f"job-impact-{uuid4().hex}"
    resp = submit_job(client, workspace, kind="impact", job_id=job_id,
                      payload={"candidate_ids": candidate_ids or [],
                              "existing_finding_ids": existing_finding_ids or []})
    assert resp.status_code == 200, resp.text
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "impact-worker")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    status = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["execution_status"] == "COMPLETED", body
    return job_id


def create_real_simulation_job(setup, client, workspace, *, payload=None):
    """Real, COMPLETED "simulation" job."""
    tenant, store = setup["tenant"], setup["store"]
    job_id = f"job-sim-{uuid4().hex}"
    resp = submit_job(client, workspace, kind="simulation", job_id=job_id, payload=payload or {})
    assert resp.status_code == 200, resp.text
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "sim-worker")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    status = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["execution_status"] == "COMPLETED", body
    return job_id


# --------------------------------------------------------------------------- proposal helpers
# Real product-path helpers to drive `kind="proposal"` end to end (submit ->
# real worker claim -> real persisted GET). Reused by every C1/C2/C3
# assertion instead of each test re-deriving its own claim/GET boilerplate.

def claim_proposal_job(setup, workspace, *, worker_id="proposal-worker"):
    store, tenant = setup["store"], setup["tenant"]
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, worker_id)
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    return claim


def get_job_status(client, workspace, job_id):
    resp = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert resp.status_code == 200, resp.text
    return resp.json()


def submit_claim_and_get(setup, client, workspace, *, kind, payload, job_id=None,
                         worker_id="proposal-worker"):
    """Submit a job through the real route, expect a real 200 (job created),
    claim it through the real worker, and return the real persisted
    `GET /v2/jobs/{id}` body. Callers that expect a REJECTED submission
    (400/409) must call `submit_job` directly instead -- this helper asserts
    200 on submit, matching every "the job IS created, the real
    reject/blocker/failure happens at claim time" row this round closes."""
    job_id = job_id or f"job-proposal-{uuid4().hex}"
    resp = submit_job(client, workspace, kind=kind, payload=payload, job_id=job_id)
    assert resp.status_code == 200, resp.text
    claim_proposal_job(setup, workspace, worker_id=worker_id)
    return get_job_status(client, workspace, job_id)


def create_confirmed_python_intent(setup, monkeypatch, owner, *, raw_value):
    """Real, end-to-end CONFIRMED Intent via the
    real `/v2/intent/parse` -> `/v2/intent/confirm` routes -- reuses
    `tests.ai6t01.test_intent_parse_confirm`'s own real helpers verbatim
    (one shared resolution path, not reinvented). Returns
    `(intent_id, candidate_id)`. NB: the real Intent schema stores `target` as the
    grounded `candidate_id` (an opaque id), never a human-readable string --
    `infra/worker/proposal.py::_classify_python_markdown_edit` anchors a
    contextual edit by finding `intent["target"]` as a literal substring of
    the CURRENT published text. For this mechanism to find a real anchor,
    the published source text must therefore literally contain the
    candidate_id string (e.g. as a machine-readable rule marker), which is
    what the fixtures below do -- a natural-language document that
    merely *describes* the same rule in prose would NOT be found by this
    literal-substring anchor. This is a real, non-crashing limitation
    (classification correctly falls back to "manual" rather than fabricating
    an edit when no anchor exists)."""
    from tests.ai6t01.test_intent_parse_confirm import _intent_confirm, _parse, _seed_current_km

    candidate_id = _seed_current_km(setup, monkeypatch, owner)
    requester = setup["browser"]()
    login(requester, setup["requester"], setup["credential"])
    parse_resp = _parse(requester, setup["workspace"],
                        raw_request=f"Raise the freight approval threshold to {raw_value} USD.",
                        target_hint=candidate_id)
    assert parse_resp.status_code == 200, parse_resp.text
    intent_id = parse_resp.json()["intent_id"]
    confirm_resp = _intent_confirm(owner["client"], setup["workspace"], intent_id)
    assert confirm_resp.status_code == 200, confirm_resp.text
    assert confirm_resp.json()["status"] == "CONFIRMED", confirm_resp.text
    return intent_id, candidate_id
