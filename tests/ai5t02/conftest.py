"""Decision ledger fixtures: append-only decisions / authority semantics.

Reuses `tests/ai4t01/conftest.py::setup` (real PostgreSQL, real
`infra.judge.app`, one "requester" account) and
`tests/ai4t02/conftest.py::seed_extraction_candidates` (real `extraction`
job -> real `RuleCandidate`s) exactly as `tests/ai4t03/conftest.py` already
does for the same upstream chain (one shared resolution path, not
reinvented). Adds a second "knowledge_owner" account (real
`knowledge.confirm` + `conflict.resolve` permissions per
`infra/adapters/v2_policy.py::ROLE_PERMISSIONS`) on top of `setup`, since
the findings fixture only creates a "requester" account and these tests need
both a proposer/answerer role and a confirming/resolving role, the same
A/B split `tests/ai5t01/conftest.py::two_sessions` established.

`seed_conflict_finding` drives a REAL "finding" job
(`infra.worker.findings.FindingsExecutor`) with two
candidates disagreeing on the same subject/scope -- SOP text says 500,000
VND, system/code config says 1,000,000 VND -- the exact SOP-vs-code numeric
conflict ("SOP=500k/code=1m"). Returns
the persisted job body so tests can read the real, persisted `CONFLICT`
finding's `severity`/`authority_required` fields instead of an invented
shape.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.worker import job_worker

from tests.ai4t01.conftest import DSN, login, setup  # noqa: F401
from tests.ai4t02.conftest import seed_extraction_candidates  # noqa: F401


def _candidate(subject, predicate, value, scope, quote, unit="VND", modality="must"):
    return {"subject": subject, "predicate": predicate, "value": value, "unit": unit,
           "modality": modality, "scope": scope, "outcome": "Finance Manager approval",
           "quotes": [quote]}


@pytest.fixture
def owner(setup):
    """Adds a second, "knowledge_owner" account (knowledge.confirm +
    conflict.resolve) on top of the real `setup` fixture's tenant/workspace,
    and returns a logged-in client for it plus the account id -- the
    confirming/resolving actor D1-D3 need, distinct from `setup["requester"]`
    (who only carries knowledge.answer)."""
    from infra.adapters.v2_identity import PostgresIdentityStore

    nonce = uuid4().hex
    account, credential = f"owner-{nonce}", f"cred-owner-{nonce}"
    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.create_account(account, credential)
    identities.grant_membership(setup["tenant"], setup["project"], setup["workspace"],
                                account, ("knowledge_owner",))
    client = setup["browser"]()
    login(client, account, credential)
    return {"account": account, "credential": credential, "client": client}


SOP_TEXT = ("Expense SOP. Approvals above 500,000 VND require Finance sign-off.")
CODE_TEXT = ("System configuration. approval_threshold_vnd = 1000000.")


def seed_conflict_finding(setup, monkeypatch, owner_client):
    """Real `finding` job over two SOP-vs-code disagreeing
    candidates. Returns (job_status_dict, [cand_sop_id, cand_code_id]).

    NB: job creation for kind "finding" is gated by "source.import"
    (`JOB_CREATE_PERMISSION`, infra/adapters/v2_policy.py:60-70), which
    "knowledge_owner" does NOT carry (by design -- confirming/resolving is a
    distinct permission from running analysis jobs). The finding job is
    therefore submitted with `setup["requester"]`'s own client (who does
    carry source.import via the "requester" role), matching the real
    permission boundary instead of granting `owner_client` a permission it
    should not have merely to make seeding convenient."""
    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    requester_client = setup["browser"]()
    login(requester_client, setup["requester"], setup["credential"])

    body_sop = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="expense-sop", text=SOP_TEXT,
        candidates_json=[_candidate("approval_threshold", "approval_threshold_vnd",
                                    500000, {"category": "expense", "source": "SOP"},
                                    SOP_TEXT.split(". ", 1)[1])])
    body_code = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="expense-code", text=CODE_TEXT,
        candidates_json=[_candidate("approval_threshold", "approval_threshold_vnd",
                                    1000000, {"category": "expense", "source": "code"},
                                    CODE_TEXT.split(". ", 1)[1])])
    cand_sop = body_sop["execution_result"]["candidates"][0]["candidate_id"]
    cand_code = body_code["execution_result"]["candidates"][0]["candidate_id"]

    job_id = f"job-finding-{uuid4().hex}"
    resp = requester_client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "finding", "job_id": job_id,
        "payload": {"candidate_ids": [cand_sop, cand_code]}})
    assert resp.status_code == 200, resp.text
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "finding-worker")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    status = requester_client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert status.status_code == 200, status.text
    return status.json(), [cand_sop, cand_code]
