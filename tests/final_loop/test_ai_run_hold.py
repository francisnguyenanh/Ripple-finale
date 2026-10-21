"""F-ACC-2 through the real routes on the throwaway PostgreSQL (2026-10-21): in a sandbox workspace every AI run needs a SECOND PERSON. A business question asked
by the requester waits; the read DTO says why, which roles may allow it, where, and whether THIS reader may; the requester cannot allow their own run; a member without
the permission cannot; a reviewer on the lease sees it in the awaiting list and allows it through POST /v2/jobs/{id}/approve; it then waits for a worker slot and runs.
Refusals: the lease released (409 sandbox_lease_required), the member's role revoked (refused). Stub model; nothing here is AI quality."""
from __future__ import annotations

import time
from uuid import uuid4

from fastapi.testclient import TestClient

from infra import judge
from infra.adapters.v2_identity import PostgresIdentityStore
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import ask, obj, qa_env, use  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import DSN, drain, sandbox_config


def member(env, role):
    ids = PostgresIdentityStore(DSN)
    acct, cred = f"{role[:3]}-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}"
    ids.create_account(acct, cred)
    ids.grant_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, acct, (role,))
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": acct, "credential": cred}).status_code == 200
    return c, acct


def sandbox(q, monkeypatch, participants):
    head = q.env.store.head(q.env.tenant, q.env.ws)
    monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(q.env.ws, head.source_revision_id))
    r = q.admin.post("/v2/sandbox/lease", json={"workspace_id": q.env.ws, "action": "acquire", "participants": participants})
    assert r.status_code == 200, r.text


def read(client, q, qid):
    r = client.get(f"/v2/business/questions/{qid}", params={"workspace_id": q.env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def awaiting(client, q):
    r = client.get("/v2/ai-runs/awaiting-approval", params={"workspace_id": q.env.ws})
    assert r.status_code == 200, r.text
    return [x["job_id"] for x in r.json()["runs"]]


def test_a_second_person_allows_a_waiting_run_and_the_reasons_are_stated_for_everyone_else(qa_env, monkeypatch):
    q = qa_env
    model = use(q, lambda p: obj("ABSTAINED"))
    rev, rev_acct = member(q.env, "reviewer")                                                 # holds ai.use, is not the person who asked
    view, _ = member(q.env, "viewer")                                                         # project.read only: no ai.use
    sandbox(q, monkeypatch, [q.env.req, rev_acct])
    r = ask(q, q.req, "Who approves a purchase of 1,200,000 JPY?")
    assert r.status_code == 200, r.text
    qid = r.json()["question_id"]
    drain(q.env)                                                                              # the stock worker never runs an unapproved run
    assert model.calls == 0
    own = read(q.req, q, qid)
    assert own["status"] == "waiting_for_approval"
    h = own["hold"]
    assert (h["state"], h["reason"], h["permission"], h["distinct_required"], h["requested_by_you"], h["you_may_allow"], h["why_not"]) == \
        ("waiting_for_approval", "sandbox_second_person", "ai.use", True, True, False, "requested_by_you")
    assert "reviewer" in h["allowed_roles"] and "viewer" not in h["allowed_roles"] and "AI runs waiting for a second person" in h["where"]
    assert q.req.post(f"/v2/jobs/{qid}/approve", json={"workspace_id": q.env.ws}).status_code == 403          # the route refuses self-approval
    assert awaiting(q.req, q) == []
    hv = read(view, q, qid)["hold"]
    assert hv["you_may_allow"] is False and hv["why_not"] == "no_permission" and awaiting(view, q) == []
    hr = read(rev, q, qid)["hold"]
    assert hr["you_may_allow"] is True and hr["why_not"] is None and awaiting(rev, q) == [qid]
    assert rev.post(f"/v2/jobs/{qid}/approve", json={"workspace_id": q.env.ws}).status_code == 200
    after = read(q.req, q, qid)
    assert after["status"] in ("waiting_for_slot", "running", "done") and awaiting(rev, q) == []
    if after["status"] == "waiting_for_slot":
        assert after["hold"] == {"state": "waiting_for_slot"}
    deadline = time.time() + 60                                                               # the held claim is deferred; the stock worker reclaims it
    while read(q.req, q, qid)["status"] != "done" and time.time() < deadline:
        drain(q.env)
        time.sleep(0.5)
    assert read(q.req, q, qid)["status"] == "done" and model.calls == 1


def test_a_released_lease_or_a_revoked_role_stops_the_allow_and_says_why(qa_env, monkeypatch):
    q = qa_env
    use(q, lambda p: obj("ABSTAINED"))
    rev, rev_acct = member(q.env, "reviewer")
    sandbox(q, monkeypatch, [q.env.req, rev_acct])
    qid = ask(q, q.req, "Who approves a purchase of 1,300,000 JPY?").json()["question_id"]
    assert q.admin.post("/v2/sandbox/lease", json={"workspace_id": q.env.ws, "action": "release"}).status_code == 200
    hr = read(rev, q, qid)["hold"]
    assert hr["you_may_allow"] is False and hr["why_not"] == "lease_required" and awaiting(rev, q) == []
    r = rev.post(f"/v2/jobs/{qid}/approve", json={"workspace_id": q.env.ws})
    assert r.status_code == 409 and r.json()["code"] == "sandbox_lease_required"
    assert read(q.req, q, qid)["status"] == "waiting_for_approval"                              # nothing was allowed
    sandbox(q, monkeypatch, [q.env.req, rev_acct])
    PostgresIdentityStore(DSN).revoke_membership(q.env.tenant, q.env.tenant.replace("t-", "p-"), q.env.ws, rev_acct)
    r = rev.post(f"/v2/jobs/{qid}/approve", json={"workspace_id": q.env.ws})
    assert r.status_code in (401, 403) and read(q.req, q, qid)["status"] == "waiting_for_approval"   # the route re-checks the membership at the action
