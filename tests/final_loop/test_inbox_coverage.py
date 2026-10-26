"""SCN-12 work-list coverage (`GET /v2/work/inbox`, contract `ripple-request-task/1` §3): the response says which candidate sources it read up to their limit, whether
a source had more than the limit (the lists may then miss requests), how many recent entries were loaded vs shown, and `complete` only when nothing was cut and every
task view was readable. Real routes and store on the throwaway PostgreSQL; both request families; a failed read is never complete and never an empty "no work"."""
from __future__ import annotations

import psycopg2
import pytest

from infra import v2_work_routes
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_syn01_code import login
from tests.pitch_brief.chain import build
from tests.pitch_brief.test_draft_to_proposal import _grant, chain_to_proposal


def _work(client, env):
    r = client.get("/v2/work/inbox", params={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def _ids(inbox):
    return {i["proposal_job_id"] for v in inbox["sections"].values() for i in v}


def _rows(env):
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        out = {}
        for table in ("v2_jobs", "v2_audit"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id=%s AND workspace_id=%s", (env.tenant, env.ws))
            out[table] = cur.fetchone()[0]
        return out


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_business_change_coverage_complete_then_truncated_and_role_scoped(qa_env, monkeypatch):
    q = qa_env
    cid, approver = prepared(q)
    viewer = login(q, f"viewer-cov-{q.env.nonce}", ("viewer",))                          # account set-up writes its own audit row: before the baseline
    before = _rows(q.env)
    a = _work(approver, q.env)
    assert cid in _ids(a)
    cov = a["coverage"]
    assert cov["scope"] == "loaded" and cov["truncated"] == [] and cov["complete"] is True and a["unavailable"] == 0
    assert cov["candidate_limits"]["business_change"] == v2_work_routes.INBOX_LIMIT and cov["recent_shown"] <= 5 and cov["recent_shown"] <= cov["recent_loaded"]
    req = _work(q.req, q.env)["coverage"]                                                     # the requester reads its own sources too (intent.create)
    assert {"change_request_conversation", "own_intent"} <= set(req["candidate_limits"]) and req["complete"] is True
    v = _work(viewer, q.env)
    assert cid not in _ids(v) and v["unavailable"] == 0 and v["coverage"]["complete"] is True     # not readable: neither listed nor counted, and no hint
    assert "own_intent" not in v["coverage"]["candidate_limits"]
    monkeypatch.setattr(v2_work_routes, "INBOX_LIMIT", 0)                                         # a source with more candidates than its limit
    cut = _work(approver, q.env)
    assert cid not in _ids(cut) and "business_change" in cut["coverage"]["truncated"] and cut["coverage"]["complete"] is False
    hidden = _work(viewer, q.env)["coverage"]                                                    # changes this reader may not read never show up as a cut
    assert "business_change" not in hidden["truncated"]
    assert _rows(q.env) == before                                                                  # reads write nothing and queue no job


def test_intent_flow_truncation_and_own_conversation_limit(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    approver = _grant(env, "change_approver")
    full = _work(approver, env)
    assert c.proposal in _ids(full) and full["coverage"]["complete"] is True
    monkeypatch.setattr(v2_work_routes, "INBOX_LIMIT", 0)
    cut = _work(approver, env)
    assert c.proposal not in _ids(cut) and "proposal" in cut["coverage"]["truncated"] and cut["coverage"]["complete"] is False
    monkeypatch.setattr(v2_work_routes, "INBOX_LIMIT", 40)
    me = _work(c.req, env)["account_id"]
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:                           # two of the requester's own open conversations (store state the route reads)
        for n in (1, 2):
            cur.execute("INSERT INTO v2_chat_conversations (tenant_id, workspace_id, conversation_id, created_by, status, purpose) VALUES (%s,%s,%s,%s,'OPEN','change_request')",
                        (env.tenant, env.ws, f"chat-cov-{n}-{env.nonce}", me))
    monkeypatch.setattr(v2_work_routes, "OWN_CONVERSATION_LIMIT", 1)
    r = _work(c.req, env)["coverage"]
    assert "change_request_conversation" in r["truncated"] and r["candidate_limits"]["change_request_conversation"] == 1 and r["complete"] is False
    monkeypatch.setattr(v2_work_routes, "OWN_CONVERSATION_LIMIT", 2)
    r2 = _work(c.req, env)["coverage"]
    assert "change_request_conversation" not in r2["truncated"]                                   # exactly at the limit: nothing beyond it, not cut
    assert full["coverage"]["candidate_limits"]["proposal"] == 40


def test_a_failed_task_view_is_unavailable_and_never_complete(qa_env, monkeypatch):
    from infra.adapters import v2_request_tasks as rt
    q = qa_env
    cid, approver = prepared(q)

    def broken(*_a, **_k):
        raise RuntimeError("projection read failed")
    monkeypatch.setattr(rt, "task_view", broken)
    w = _work(approver, q.env)
    assert w["unavailable"] >= 1 and w["coverage"]["complete"] is False and cid not in _ids(w)     # failure is reported, not shown as no work
