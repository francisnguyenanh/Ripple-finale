"""Data rights per response of the new interface: the AI-extracted QUOTE layer (claims, queue, graph, timeline, job results) is returned
only to a role with `evidence.read`; the answers about files, states and counts need `project.read` only; member names need
`membership.manage`. Checked on the server's own responses, not on what a page chooses to show (hiding in CSS is not security).
Source file content itself is a workspace-shared document (`project.read`): see docs in UI_DATA_RIGHTS.md for that decision."""
from __future__ import annotations

import json

import pytest

from tests.pitch_brief.chain import APPROVAL, build, cand_id


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch, finguide_scope={"category": "all"})


def _quotes(ch):
    out = set()
    for cs in ch.cands.values():
        for c in cs:
            for e in c.get("evidence") or []:
                q = " ".join(str(e.get("quote") or "").split())
                if len(q) >= 20:
                    out.add(q[:24])
    assert out
    return out


def _routes(env, ch):
    cid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    job = ch.jobs[APPROVAL]
    w = {"workspace_id": env.ws}
    return {
        "GET /v2/knowledge/graph": ("/v2/knowledge/graph", w),
        "GET /v2/knowledge/overview": ("/v2/knowledge/overview", w),
        "GET /v2/hitl/queue": ("/v2/hitl/queue", w),
        "GET /v2/jobs/{extraction job}": (f"/v2/jobs/{job}", w),
        "GET /v2/knowledge/timeline": ("/v2/knowledge/timeline", {**w, "node_id": cid}),
        "GET /v2/knowledge/history": ("/v2/knowledge/history", w),
        "GET /v2/sources/lifecycle": ("/v2/sources/lifecycle", w),
        "GET /v2/workspace/profile": ("/v2/workspace/profile", w),
        "GET /v2/audit/{ws}": (f"/v2/audit/{env.ws}", {}),
    }


def test_a_role_without_evidence_read_receives_no_quote_in_any_knowledge_response(env, ch):
    quotes = _quotes(ch)
    req, adm = env.req_client(), env.admin_client()
    leaks, seen_by_admin = [], set()
    for name, (path, params) in _routes(env, ch).items():
        mine = req.get(path, params=params)
        theirs = adm.get(path, params=params)
        assert mine.status_code == 200 and theirs.status_code == 200, (name, mine.status_code, theirs.status_code)
        flat_req, flat_adm = " ".join(mine.text.split()), " ".join(theirs.text.split())
        flat_req = json.dumps(mine.json(), ensure_ascii=False)
        flat_adm = json.dumps(theirs.json(), ensure_ascii=False)
        for q in quotes:
            qj = json.dumps(q, ensure_ascii=False)[1:-1]
            if qj in flat_req:
                leaks.append((name, q))
            if qj in flat_adm:
                seen_by_admin.add(name)
    assert not leaks, f"quote text reached a role without evidence.read: {leaks}"
    assert {"GET /v2/knowledge/graph", "GET /v2/hitl/queue", "GET /v2/knowledge/overview"} <= seen_by_admin, (
        "the control failed: an evidence-reading role must still see the quotes", seen_by_admin)


def test_member_names_need_the_right_to_manage_access_and_counts_need_only_project_read(env, ch):
    req, adm = env.req_client(), env.admin_client()
    p_req = req.get("/v2/workspace/profile", params={"workspace_id": env.ws}).json()
    p_adm = adm.get("/v2/workspace/profile", params={"workspace_id": env.ws}).json()
    assert "members" not in p_req and "members" in p_adm
    assert env.admin not in json.dumps(p_req), "another person's account name is not shown to a role that cannot manage access"
    life = req.get("/v2/sources/lifecycle", params={"workspace_id": env.ws}).json()
    assert life["totals"]["imported"] == len(ch.arts) and life["totals"]["claims_total"] > 0, "counts are allowed for project.read"


def test_a_foreign_workspace_and_an_anonymous_caller_get_nothing_from_any_of_these_routes(env, ch):
    import fastapi.testclient as tc
    from infra.judge import app
    anon = tc.TestClient(app, base_url="https://judge.test")
    req = env.req_client()
    for name, (path, params) in _routes(env, ch).items():
        assert anon.get(path, params=params).status_code in (401, 403), name
        if "workspace_id" in params:
            r = req.get(path, params={**params, "workspace_id": "ws-not-mine"})
            assert r.status_code in (401, 403, 404), (name, r.status_code)      # the jobs route answers 401 for a workspace that is not yours: nothing leaks
            assert env.tenant not in r.text and env.ws not in r.text, name
