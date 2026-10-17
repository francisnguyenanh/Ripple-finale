"""The demo roles and the three DIFFERENT approvals, enforced by the API (not by hidden buttons):
  model-call approval (job approve)  !=  knowledge confirmation (knowledge.confirm)  !=  change approval (change decision).
Also: the source revision does not move before apply; self-approval and wrong roles are refused. Stub provider only."""
from __future__ import annotations

import pytest

from tests.pitch_brief.chain import APPROVAL, build, cand_id
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_draft_to_proposal import _grant, _to_approval, chain_to_proposal


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_matrix_refusals_and_source_unchanged_until_apply(env, ch, monkeypatch):
    head0 = env.store.head(env.tenant, env.ws)
    c = chain_to_proposal(env, ch, monkeypatch)                      # draft, impact, simulation, proposal: all deterministic reads
    assert env.store.head(env.tenant, env.ws).source_revision_id == head0.source_revision_id
    op, review, aj, appr = _to_approval(env, ch, c)                  # contract by a different person, review, approval bundle
    assert env.store.head(env.tenant, env.ws).source_revision_id == head0.source_revision_id    # still the Harbor seed
    req = c.req
    dec = {"workspace_id": env.ws, "action": "approve", "reason": "x", "expected_approval_digest": appr["approval_digest"]}
    # (1) change approval: the requester cannot decide its own change; the approver can
    assert req.post(f"/v2/jobs/{aj}/decision", json=dec).status_code in (403, 409)
    # (2) knowledge confirmation: requester and change_approver have no knowledge.confirm
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    assert req.post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": tid}).status_code == 403
    assert op.post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": tid}).status_code == 403
    # (3) the approver cannot create work (no source.import): jobs are created by the requester/admin only
    _, r = create_job(op, env, "impact", {"candidate_ids": [tid]})
    assert r.status_code == 403
    # (4) model-call approval: a requester cannot approve its own provider job, the admin (another person) can
    jid, r = create_job(req, env, "extraction", {"artifact_id": ch.arts[APPROVAL], "source_role": "unknown"})
    assert r.status_code == 200, r.text
    assert req.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code in (403, 409)
    assert op.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 403   # approver is not a job approver
    assert ch.admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
    # (5) apply is refused before any approval decision; the source stays the seed
    ap, r = create_job(req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
    drain(env)
    body = job_body(ch.admin, env, ap) if r.status_code == 200 else None
    assert r.status_code != 200 or body["execution_status"] != "COMPLETED"
    assert env.store.head(env.tenant, env.ws).source_revision_id == head0.source_revision_id
    # (6) with the distinct approver's decision, apply publishes a NEW source revision (history keeps the seed)
    assert op.post(f"/v2/jobs/{aj}/decision", json=dec).status_code == 200
    ap2, r2 = create_job(req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
    assert r2.status_code == 200, r2.text
    drain(env)
    drain(env)
    assert env.store.head(env.tenant, env.ws).source_revision_id != head0.source_revision_id
    hist = env.admin_client().get(f"/v2/history/{env.ws}")
    assert hist.status_code == 200 and head0.source_revision_id in hist.text            # the seed revision is still in history
