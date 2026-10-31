"""SEC-01 (audit 2026-10-30): the exact before/after of a proposal (`GET /v2/proposals/{id}/diff`, lines of the source file) is served to the person
who approves it (`change.approve`, the DecisionBasis contract) and to evidence readers (`evidence.read`); a viewer or a requester of the same
workspace is refused before the job is read, with one answer whether or not the proposal exists; a revoked reader is refused on the next request;
a missing staged blob is a safe 409 for a reader and still 403 for the others. Real API, real PostgreSQL, real worker chain (stub provider)."""
from __future__ import annotations

import json

import psycopg2
import pytest

from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import DSN
from tests.pitch_brief.test_draft_to_proposal import _to_approval, chain_to_proposal
from tests.pitch_brief.test_role_model import member

REFUSED = {"error": "permission denied: evidence.read or change.approve"}


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _diff(client, proposal, ws):
    return client.get(f"/v2/proposals/{proposal}/diff", params={"workspace_id": ws})


def test_viewer_and_requester_are_refused_before_the_job_is_read(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    _to_approval(env, ch, c)
    for role in ("viewer", "requester"):
        m = member(env, role)
        r = _diff(m, c.proposal, env.ws)
        assert r.status_code == 403 and r.json() == REFUSED, role
        assert "lines" not in r.text and "logical_path" not in r.text
        missing = _diff(m, "job-does-not-exist", env.ws)
        assert missing.status_code == 403 and missing.json() == REFUSED, "the same answer whether or not the proposal exists"
        other_ws = _diff(m, c.proposal, "ws-not-a-member")
        assert other_ws.status_code in (401, 403, 404) and "lines" not in other_ws.text
        job = m.get(f"/v2/jobs/{c.proposal}", params={"workspace_id": env.ws})
        # the generic job read gives the typed edit (path and values, the change itself) with quotes redacted, never the file's lines
        res = job.json().get("execution_result") or {}
        assert job.status_code == 200 and "lines" not in res and "content" not in res


def test_approver_and_evidence_reader_get_the_exact_diff_and_matching_hashes(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, _review, _aj, _appr = _to_approval(env, ch, c)
    record = op.get(f"/v2/jobs/{c.proposal}", params={"workspace_id": env.ws}).json()["execution_result"]
    for client in (op, member(env, "reviewer"), member(env, "admin")):
        r = _diff(client, c.proposal, env.ws)
        assert r.status_code == 200
        body = r.json()
        assert body["before_blob_hash"] == record["before_blob_hash"] and body["after_blob_hash"] == record["after_blob_hash"]
        assert {"add", "remove"} <= {l["kind"] for l in body["lines"]} and body["logical_path"].endswith("approval.yaml")
    from infra.adapters.v2_policy import permissions_for
    assert "evidence.read" not in permissions_for(("change_approver",)) and "change.approve" in permissions_for(("change_approver",))
    assert "evidence.read" in permissions_for(("reviewer",)) and "change.approve" not in permissions_for(("viewer",)) and "evidence.read" not in permissions_for(("requester",))


def test_revoked_reader_is_refused_on_the_next_request(env, ch, monkeypatch):
    from infra.adapters.v2_identity import PostgresIdentityStore
    c = chain_to_proposal(env, ch, monkeypatch)
    _to_approval(env, ch, c)
    rv = member(env, "reviewer")
    assert _diff(rv, c.proposal, env.ws).status_code == 200
    PostgresIdentityStore(DSN).revoke_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, rv.account)
    r = _diff(rv, c.proposal, env.ws)
    assert r.status_code in (401, 403) and "lines" not in r.text


def test_missing_staged_blob_is_a_safe_409_for_a_reader_and_still_403_for_a_viewer(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, _review, _aj, _appr = _to_approval(env, ch, c)
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("""UPDATE v2_jobs SET execution_result = execution_result || '{"staged_blob_hash": "0000000000000000000000000000000000000000000000000000000000000000"}'::jsonb
                       WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""", (env.tenant, env.ws, c.proposal))
    r = _diff(op, c.proposal, env.ws)
    assert r.status_code == 409 and r.json() == {"error": "staged proposal bytes unavailable"}
    v = _diff(member(env, "viewer"), c.proposal, env.ws)
    assert v.status_code == 403 and v.json() == REFUSED


def test_an_approver_of_another_workspace_cannot_read_the_diff(env, ch, monkeypatch):
    """The exception is scoped to the proposal's own tenant and workspace: an account holding change.approve elsewhere gets no diff here."""
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from infra import judge
    from infra.adapters.v2_identity import PostgresIdentityStore
    c = chain_to_proposal(env, ch, monkeypatch)
    _to_approval(env, ch, c)
    ids = PostgresIdentityStore(DSN)
    other_ws = f"ws-other-{uuid4().hex[:8]}"
    env.store.create_workspace(env.tenant, env.tenant.replace("t-", "p-"), other_ws)
    acct, cred = f"ap-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}"
    ids.create_account(acct, cred)
    ids.grant_membership(env.tenant, env.tenant.replace("t-", "p-"), other_ws, acct, ("change_approver",))
    cl = TestClient(judge.app, base_url="https://judge.test")
    cl.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert cl.post("/v2/auth/login", json={"account_id": acct, "credential": cred}).status_code == 200
    assert _diff(cl, c.proposal, other_ws).status_code in (403, 404) and "lines" not in _diff(cl, c.proposal, other_ws).text, "the proposal is not in that workspace"
    r = _diff(cl, c.proposal, env.ws)
    assert r.status_code in (401, 403, 404) and "lines" not in r.text, "no membership in the proposal's workspace: refused"
