"""SCN-12 follow-up: `GET /v2/audit/{ws}` keeps each request family's read rule. Real routes and store on the throwaway PostgreSQL. A reader who may not read a
business change sees no event bound to it (its own job, its confirmation, its decisions, jobs whose payload names it); the person who asked and holders of
`knowledge.confirm` / `change.approve` still see them; a private request-assistant job is shown only to the person who asked it. Workspace events stay visible."""
from __future__ import annotations

import json

import psycopg2

from tests.final_loop.test_chg04_review import login, prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_browser import rq_ui  # noqa: F401  (fixture)
from tests.ai10ui.fixtures import pw_browser  # noqa: F401  (rq_ui's module needs it importable)


def _events(client, env):
    r = client.get(f"/v2/audit/{env.ws}")
    assert r.status_code == 200, r.text
    return r.json()["events"]


def _naming(events, ident):
    return [e for e in events if ident in json.dumps(e)]


def test_business_change_events_follow_the_change_read_rule(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    viewer = login(q, f"viewer-aud-{q.env.nonce}", ("viewer",))
    confirmer = login(q, f"owner-aud-{q.env.nonce}", ("knowledge_owner",))
    seen_by_asker = _naming(_events(q.req, q.env), cid)
    assert {e["event_type"] for e in seen_by_asker} >= {"JOB_QUEUED", "BUSINESS_CHANGE_INTENT_CONFIRMED"}       # the person who asked keeps the history
    assert len(_naming(_events(approver, q.env), cid)) == len(seen_by_asker)                                    # change.approve
    assert len(_naming(_events(confirmer, q.env), cid)) == len(seen_by_asker)                                   # knowledge.confirm
    v = _events(viewer, q.env)
    assert _naming(v, cid) == []                                                                                 # nothing about a change the viewer may not read
    assert any(e["event_type"] == "SOURCE_PUBLISHED" for e in v)                                                 # workspace events are still shown
    stored = [e for e in v if e.get("id") is not None]
    assert stored and all(isinstance(e.get("created_at"), str) and "T" in e["created_at"] for e in stored)      # each stored event carries its recorded time
    dep = q.req.post(f"/v2/business/changes/{cid}/impact/dependencies", json={"workspace_id": q.env.ws})        # a job bound to the change by its payload
    assert dep.status_code in (200, 202), dep.text                                                               # queued only: no worker runs, no model call
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind<>'change.understand' AND payload->>'intent_id'=%s",
                    (q.env.tenant, q.env.ws, cid))
        derived = [r[0] for r in cur.fetchall()]
    assert derived
    v = _events(viewer, q.env)
    for jid in derived:
        assert _naming(v, jid) == [] and _naming(_events(q.req, q.env), jid)
    other = login(q, f"req2-aud-{q.env.nonce}", ("requester",))                                                  # another requester: not the asker, no rights
    assert _naming(_events(other, q.env), cid) == []


def test_a_private_assistant_job_is_shown_only_to_its_asker(rq_ui):
    q, model, server, cid = rq_ui
    approver = login(q, f"approver-{q.env.nonce}x", ("change_approver", "reviewer"))
    r = approver.post(f"/v2/requests/business_change/{cid}/assistant/ask", json={"workspace_id": q.env.ws, "question": "What should I do next?", "idempotency_key": "audit-scope-0001"})
    assert r.status_code in (200, 202), r.text
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='request.qa'", (q.env.tenant, q.env.ws))
        jobs = [row[0] for row in cur.fetchall()]
    assert jobs
    assert all(_naming(_events(approver, q.env), j) for j in jobs)                                              # the asker sees their own question's job
    assert all(not _naming(_events(q.req, q.env), j) for j in jobs)                                             # the requester may read the change, not this thread
    assert all(not _naming(_events(q.admin, q.env), j) for j in jobs)                                           # nor an administrator


def test_membership_events_are_listed_once_with_their_id_and_time(qa_env):
    """PC-06 (2026-10-26): the route merged the identity store's membership list, which reads the same audit rows, so every grant and revocation was listed
    twice, the copy without id or recorded time (History hides grants but showed a revocation twice)."""
    from infra.adapters.v2_identity import PostgresIdentityStore
    q = qa_env
    viewer = login(q, f"viewer-mem-{q.env.nonce}", ("viewer",))
    gone = f"gone-mem-{q.env.nonce}"
    login(q, gone, ("requester",))
    PostgresIdentityStore(q.env.dsn).revoke_membership(q.env.tenant, q.env.tenant.replace("t-", "p-"), q.env.ws, gone)
    for client in (q.admin, viewer):
        events = _events(client, q.env)
        assert all(e.get("id") is not None and isinstance(e.get("created_at"), str) for e in events), [e for e in events if e.get("id") is None][:2]
        assert len({e["id"] for e in events}) == len(events)                                                     # no event listed twice
        mine = [e for e in events if e["event_type"].startswith("membership.") and (e.get("payload") or {}).get("account_id") == gone]
        assert sorted(e["event_type"] for e in mine) == ["membership.grant", "membership.revoke"]               # each exactly once
        assert not any("credential" in json.dumps(e) for e in events)
