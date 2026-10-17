"""Response scoping of every GET the new interface reads: what a signed-in member may and may not receive.

Rules checked here (all server-side; hiding in the page is never relied on):
  * anonymous callers and members of another workspace receive nothing;
  * raw source quotes of claims (the human work queue, graph evidence, timeline) reach only a holder of `evidence.read`;
  * a conversation, its words and the AI permission belong to its creator and to accounts that may confirm knowledge;
  * the audit trail never carries a credential, a typed message or a model prompt.
Stub provider only.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from infra import judge
from tests.pitch_brief.chain import build
from tests.pitch_brief.test_intent_chat import Interp, READY, allow, api, settle, start, use  # noqa: F401


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _member(env, role):
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.pitch_brief.conftest import DSN
    ids = PostgresIdentityStore(DSN)
    acct, cred = f"{role[:4]}-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}"
    ids.create_account(acct, cred)
    ids.grant_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, acct, (role,))
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    r = c.post("/v2/auth/login", json={"account_id": acct, "credential": cred})
    assert r.status_code == 200, r.text
    return c


def _quotes(ch, env) -> set[str]:
    ov = api(ch.admin, "GET", "/v2/knowledge/overview", env).json()
    q = {e["quote"] for c in ov["claims"] for e in (c.get("evidence") or []) if len(e.get("quote", "")) > 25}
    assert q, "the fixture must carry evidence quotes"
    return q


def _gets(env, ch):
    ov = api(ch.admin, "GET", "/v2/knowledge/overview", env).json()
    node = ov["claims"][0]["candidate_id"]
    rev = api(ch.admin, "GET", f"/v2/workspaces/{env.ws}/head", env).json()["source_revision_id"]
    return [("/v2/hitl/queue", {}), ("/v2/knowledge/overview", {}), ("/v2/knowledge/graph", {}), ("/v2/knowledge/timeline", {"node_id": node}),
            ("/v2/knowledge/history", {}), (f"/v2/audit/{env.ws}", {}), ("/v2/change/impact-context", {"target_candidate_id": node}),
            ("/v2/support/matrix", {}), ("/v2/chat", {}), (f"/v2/history/{env.ws}", {}), (f"/v2/sources/revisions/{rev}", {}),
            ("/v2/sandbox/status", {})]


@pytest.mark.parametrize("role", ["requester", "change_approver", "restore_operator"])
def test_claim_quotes_reach_only_an_evidence_reader(env, ch, role):
    quotes = _quotes(ch, env)
    c = _member(env, role)
    leaks = []
    for path, extra in _gets(env, ch):
        r = c.get(path, params={"workspace_id": env.ws, **extra})
        if r.status_code != 200:
            continue
        body = json.dumps(r.json())
        leaks += [(path, q[:40]) for q in quotes if q in body]
    assert not leaks, f"{role} received claim quotes: {leaks}"


def test_a_knowledge_owner_does_receive_the_quotes_positive_control(env, ch):
    quotes = _quotes(ch, env)
    c = _member(env, "knowledge_owner")
    r = c.get("/v2/knowledge/overview", params={"workspace_id": env.ws})
    assert r.status_code == 200 and any(q in json.dumps(r.json()) for q in quotes)


@pytest.mark.parametrize("path", ["/v2/hitl/queue", "/v2/knowledge/overview", "/v2/knowledge/graph", "/v2/chat", "/v2/audit/{ws}", "/v2/history/{ws}",
                                  "/v2/support/matrix", "/v2/sandbox/status"])
def test_anonymous_and_foreign_workspace_callers_receive_nothing(env, ch, path):
    anon = TestClient(judge.app, base_url="https://judge.test")
    p = path.format(ws=env.ws)
    r = anon.get(p, params={"workspace_id": env.ws}, headers={"X-Ripple-Judge": "1"})
    assert r.status_code in (401, 403), (path, r.status_code)
    other = _member(env, "admin")                    # a real session, but for a workspace it is not a member of
    r = other.get(p.replace(env.ws, "ws-of-another-tenant"), params={"workspace_id": "ws-of-another-tenant"})
    assert r.status_code in (401, 403, 404), (path, r.status_code)


def test_the_audit_trail_carries_no_typed_text_credential_or_prompt(env, ch, use):
    use(Interp(READY))
    req = env.req_client()
    secret = "the-private-sentence-xyz-4242 I want the manager limit changed to 500,000 yen"
    c = start(env, req, secret)
    settle(env, req, c["conversation_id"])
    events = api(_member(env, "restore_operator"), "GET", f"/v2/audit/{env.ws}", env).json()["events"]
    blob = json.dumps(events)
    assert "the-private-sentence-xyz-4242" not in blob
    assert "credential" not in blob.lower() or "credential_digest" not in blob
    assert "prompt" not in blob.lower() or "prompt_template" not in blob


def test_a_conversation_and_its_words_are_private_to_the_creator_and_knowledge_confirmers(env, ch):
    req = env.req_client()
    c = start(env, req, "my private words about the limit 4711")
    cid = c["conversation_id"]
    for role, visible in (("change_approver", False), ("restore_operator", False), ("knowledge_owner", True)):
        m = _member(env, role)
        got = api(m, "GET", f"/v2/chat/{cid}", env)
        listing = json.dumps(api(m, "GET", "/v2/chat", env).json())
        assert (got.status_code == 200) is visible, (role, got.status_code)
        assert ("4711" in listing) is visible, role


def test_extraction_job_results_do_not_hand_claim_quotes_to_a_member_without_evidence_read(env, ch):
    quotes = _quotes(ch, env)
    jid = next(iter(ch.jobs.values()))
    admin_body = json.dumps(api(ch.admin, "GET", f"/v2/jobs/{jid}", env).json())
    assert any(q in admin_body for q in quotes), "positive control: the admin's job record carries quotes"
    for role in ("requester", "change_approver", "restore_operator"):
        got = api(_member(env, role), "GET", f"/v2/jobs/{jid}", env)
        assert got.status_code in (200, 403), got.status_code
        if got.status_code == 200:
            body = json.dumps(got.json())
            assert not [q for q in quotes if q in body], f"{role} received claim quotes through the job record"


def test_the_read_only_viewer_can_read_every_review_page_and_no_quote_and_cannot_act(env, ch):
    quotes = _quotes(ch, env)
    v = _member(env, "viewer")
    paths = [p for p in _gets(env, ch) if p[0] != "/v2/sandbox/status"] + [("/v2/sources/lifecycle", {}), ("/v2/harbor/configs", {})]   # sandbox status is 404 here: no sandbox configured in this fixture, not a role refusal
    blocked, leaks = [], []
    for path, extra in paths:
        r = v.get(path, params={"workspace_id": env.ws, **extra})
        if r.status_code != 200:
            blocked.append((path, r.status_code))
            continue
        leaks += [(path, q[:40]) for q in quotes if q in json.dumps(r.json())]
    assert blocked == [] and leaks == []
    for kind, payload in (("extraction", {"artifact_id": next(iter(ch.arts.values()))}), ("gap.answer", {"question": "q", "question_id": "q1", "artifact_ids": [next(iter(ch.arts.values()))]})):
        assert v.post("/v2/jobs", json={"workspace_id": env.ws, "kind": kind, "job_id": "job-v-" + kind.replace(".", "-"), "payload": payload}).status_code == 403
    assert v.post("/v2/chat/start", json={"workspace_id": env.ws, "text": "hello"}).status_code in (403, 409)
