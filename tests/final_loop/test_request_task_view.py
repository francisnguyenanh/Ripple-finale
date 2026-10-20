"""RUX-01 task view of one request (contract `ripple-request-task/1`): real routes, store and workflow on the throwaway PostgreSQL; stub models only where the fixtures need
them. Permission, assignment and obligation stay apart (requester, approver, administrator, reviewer, viewer); the business-change family keeps its own read rule; the view
agrees with the inbox on the stage; unknown, foreign and malformed ids get the same generic 404; the read writes nothing."""
from __future__ import annotations

import pytest

from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_syn01_code import login
from tests.pitch_brief.chain import S1, build, intent
from tests.pitch_brief.test_draft_to_proposal import _grant, _to_approval, chain_to_proposal


def tv(client, env, family, rid):
    return client.get(f"/v2/requests/{family}/{rid}/task-view", params={"workspace_id": env.ws})


def ob(view, code):
    return next(o for o in view["obligations"] if o["code"] == code)


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_intent_flow_obligations_separate_permission_assignment_and_the_asked_person(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    approver, viewer, reviewer = _grant(env, "change_approver"), _grant(env, "viewer"), _grant(env, "reviewer")
    req = tv(c.req, env, "intent_flow", c.intent).json()
    assert req["schema"] == "ripple-request-task/1" and req["request_ref"] == {"family": "intent_flow", "id": c.intent} and req["stage"]["code"] == "NEEDS_CHECK"
    o = ob(req, "needs_check")
    assert o["holder"] == "role_pool" and o["eligible_me"] is False and o["assigned_me"] is False and o["obligation_me"] is False and o["reason"]
    assert any(w["holder"] == "role_pool" for w in req["waiting_on"]) and [s["code"] for s in req["stages"]][:3] == ["conversation", "intent", "draft"]
    assert ob(tv(approver, env, "intent_flow", c.intent).json(), "needs_check")["obligation_me"] is True
    adm = ob(tv(ch.admin, env, "intent_flow", c.intent).json(), "needs_check")
    assert adm["eligible_me"] is True and adm["obligation_me"] is False and "not asked" in adm["reason"]           # admin may, is not asked by default
    v = tv(viewer, env, "intent_flow", c.intent).json()
    assert ob(v, "needs_check")["eligible_me"] is False and all(not a["allowed"] for a in v["permitted_actions"])
    _op, _review, aj, _appr = _to_approval(env, ch, c)
    a = tv(approver, env, "intent_flow", c.intent).json()
    assert a["stage"]["code"] == "AWAITING_DECISION" and ob(a, "awaiting_decision")["obligation_me"] is True
    r = tv(reviewer, env, "intent_flow", c.intent).json()
    opt = ob(r, "review_optional")
    assert opt["required"] is False and opt["obligation_me"] is True and ob(r, "awaiting_decision")["eligible_me"] is False
    note = reviewer.post("/v2/changes/review-note", json={"workspace_id": env.ws, "proposal_job_id": c.proposal, "verdict": "request_changes", "text": "The threshold needs the finance view."})
    assert note.status_code == 200, note.text
    q2 = tv(c.req, env, "intent_flow", c.intent).json()
    assert q2["stage"]["code"] == "REVIEWER_CHANGES" and any(b["code"] == "reviewer_changes" for b in q2["blockers"])
    o2 = ob(q2, "reviewer_changes")
    assert o2["holder"] == "requester" and o2["assigned_me"] is True and o2["obligation_me"] is True
    rv2 = tv(reviewer, env, "intent_flow", c.intent).json()
    assert all(o["code"] != "review_optional" for o in rv2["obligations"]) and ob(rv2, "reviewer_changes")["obligation_me"] is False   # back with the requester
    inbox = c.req.get("/v2/work/inbox", params={"workspace_id": env.ws}).json()
    item = next(i for i in inbox["todo"] + inbox["waiting"] if i["proposal_job_id"] == c.proposal)
    assert item["stage"] == q2["stage"]["code"]                                                                # the same stored facts give the same stage


def test_unknown_foreign_or_mismatched_ids_get_the_same_generic_404_and_anonymous_gets_401(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    for fam, rid in (("intent_flow", "intent-doesnotexist"), ("intent_flow", c.proposal), ("intent_flow", "chat-nope"), ("business_change", c.intent),
                     ("no_family", c.intent), ("business_change", "chg-missing")):
        r = tv(c.req, env, fam, rid)
        assert r.status_code == 404 and r.json().get("error") == "request not found", (fam, rid, r.text)
    assert c.req.get(f"/v2/requests/intent_flow/{c.intent}/task-view", params={"workspace_id": "ws-not-mine"}).status_code in (403, 404)
    from fastapi.testclient import TestClient
    from infra import judge
    anon = TestClient(judge.app, base_url="https://judge.test")
    r = anon.get(f"/v2/requests/intent_flow/{c.intent}/task-view", params={"workspace_id": env.ws})
    assert r.status_code == 401 and c.intent not in r.text


def test_business_change_keeps_its_read_rule_and_shows_the_decision_obligation(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    req = tv(q.req, q.env, "business_change", cid)
    assert req.status_code == 200, req.text
    rv = req.json()
    d = ob(rv, "decide_knowledge")
    assert rv["request_ref"] == {"family": "business_change", "id": cid} and d["holder"] == "role_pool" and d["eligible_me"] is False and d["obligation_me"] is False
    assert rv["completion_axes"]["knowledge_result"] == "not_committed" and "source_sync" in rv["completion_axes"]           # axes stay separate
    groups = {s["code"]: s.get("parallel_group") for s in rv["stages"]}
    assert groups["commit"] == groups["file_decision"] == "after_knowledge_decision" and groups["knowledge_decision"] is None   # two people, either order
    assert d["step"] == "knowledge_decision" and all(w.get("step") in {s["code"] for s in rv["stages"]} for w in rv["waiting_on"])
    a = tv(approver, q.env, "business_change", cid).json()
    assert ob(a, "decide_knowledge")["obligation_me"] is True and any(x["action"] == "approve_knowledge" and x["allowed"] for x in a["permitted_actions"])
    for role in ("viewer", "reviewer", "judge"):                                                                 # not the requester, no knowledge.confirm / change.approve
        other = login(q, f"{role}-tv-{q.env.nonce}", (role,))
        r = tv(other, q.env, "business_change", cid)
        assert r.status_code == 404 and r.json()["error"] == "request not found" and cid not in r.text, role
    before = counts(q.env)
    tv(approver, q.env, "business_change", cid)
    assert counts(q.env) == before                                                                                # a read writes nothing


def test_business_change_after_the_knowledge_moved_is_blocked_with_the_reason(qa_env):
    from tests.final_loop.test_kno05_accept import answer as answer_gap
    q = qa_env
    cid, approver = prepared(q)
    gap = next(g for g in q.admin.get("/v2/business/gaps", params={"workspace_id": q.env.ws}).json()["gaps"] if g["kind"] == "missing_actor")
    answer_gap(q.admin, q.env, gap, {"actor": "finance director"}, "Only the finance director approves these.")
    a = tv(approver, q.env, "business_change", cid).json()
    assert a["stage"]["code"] == "KNOWLEDGE_MOVED" and any(b["code"] == "knowledge_moved" for b in a["blockers"])
    d = ob(a, "decide_knowledge")
    assert d["state"] == "blocked" and d["eligible_me"] is False and "moved" in d["reason"]
    assert all(w["holder"] != "role_pool" for w in a["waiting_on"])                                       # never "waiting on an approver" while blocked
    assert any(w["holder"] == "requester" and w["code"] == "restate_request" for w in a["waiting_on"])
    assert ob(a, "restate_request")["obligation_me"] is False
    r = tv(q.req, q.env, "business_change", cid).json()
    rs = ob(r, "restate_request")
    assert rs["obligation_me"] is True and rs["assigned_me"] is True and rs["state"] == "current" and not r["waiting_on"]
    acts = {x["action"]: x for x in r["permitted_actions"]}
    assert acts["restate_request"]["allowed"] is True and acts["clarify"]["allowed"] is False and "moved" in acts["clarify"]["reason"]


def _grant_roles(env, roles):
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from infra import judge
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.pitch_brief.conftest import DSN
    ids = PostgresIdentityStore(DSN)
    acct, cred = f"mr-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}"
    ids.create_account(acct, cred)
    ids.grant_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, acct, tuple(roles))
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": acct, "credential": cred}).status_code == 200
    return c


def test_before_the_draft_the_step_is_named_with_its_action_and_eligibility_is_not_assignment(env, ch):
    req = env.req_client()
    iid = intent(env, req, S1)["intent_id"]                                                              # stated and confirmed by the requester, no draft yet
    mine = tv(req, env, "intent_flow", iid).json()
    assert mine["stage"]["code"] == "ASK_DRAFT"
    o = ob(mine, "ask_draft")
    assert o["label"] == "Ask for the AI draft of the change" and o["label"] != mine["stage"]["label"]   # the step, not the stage
    assert o["holder"] == "requester" and o["assigned_me"] is True and o["obligation_me"] is True and not mine["waiting_on"]
    assert [d["code"] for d in mine["expected_deliverables"]] == ["draft"]                                # what the step hands over
    assert [a for a in mine["permitted_actions"] if a["action"] == "change.prepare" and a["allowed"]]
    judge_c = _grant(env, "judge")                                                                       # may prepare a change (route predicate), is not the requester
    j = tv(judge_c, env, "intent_flow", iid).json()
    oj = ob(j, "ask_draft")
    assert oj["eligible_me"] is True and oj["assigned_me"] is False and oj["obligation_me"] is False and "requester is asked" in oj["reason"]
    assert any(w["holder"] == "requester" and w["code"] == "ask_draft" for w in j["waiting_on"])
    v = tv(_grant(env, "viewer"), env, "intent_flow", iid).json()
    assert ob(v, "ask_draft")["eligible_me"] is False and all(not a["allowed"] for a in v["permitted_actions"])
    sus = tv(_grant_roles(env, ("requester", "ai_suspended")), env, "intent_flow", iid).json()           # multi-role: AI use switched off removes the model step
    assert ob(sus, "ask_draft")["eligible_me"] is False and "switched off" in ob(sus, "ask_draft")["reason"]
    adm = ob(tv(ch.admin, env, "intent_flow", iid).json(), "ask_draft")
    assert adm["eligible_me"] is True and adm["obligation_me"] is False                                  # may, is not asked


def test_after_request_changes_the_requester_answers_first_and_the_approver_is_not_asked(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    binding = q.req.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws, "mode": "business"}).json()["binding"]
    d = approver.post(f"/v2/business/changes/{cid}/review/decide", json={"workspace_id": q.env.ws, "kind": "knowledge", "action": "request_changes",
                                                                       "reason": "Please state the scope.", "binding": binding})
    assert d.status_code == 200, d.text
    r = tv(q.req, q.env, "business_change", cid).json()
    assert r["stage"]["code"] == "CHANGES_REQUESTED" and not r["waiting_on"]
    resp = ob(r, "respond_to_changes")
    assert resp["obligation_me"] is True and resp["assigned_me"] is True and resp["state"] == "current"
    assert ob(r, "decide_knowledge")["state"] == "waiting"
    a = tv(approver, q.env, "business_change", cid).json()
    da = ob(a, "decide_knowledge")
    assert da["state"] == "waiting" and da["eligible_me"] is True and da["obligation_me"] is False and "requester answers first" in da["reason"]
    assert [w["code"] for w in a["waiting_on"]] == ["respond_to_changes"] and a["waiting_on"][0]["holder"] == "requester"
    assert ob(a, "respond_to_changes")["obligation_me"] is False
    assert {x["action"]: x["allowed"] for x in a["permitted_actions"]}["approve_knowledge"] is True     # the route still allows a decision: shown, not hidden
    s = {x["code"]: x["state"] for x in a["stages"]}
    assert s["knowledge_decision"] == "waiting"


def test_a_committed_change_is_not_reported_as_moved_by_its_own_commit(qa_env):
    from tests.final_loop.test_syn02_reconcile import committed_change
    q = qa_env
    cid = committed_change(q)
    v = tv(q.admin, q.env, "business_change", cid).json()
    assert v["stage"]["code"] != "KNOWLEDGE_MOVED" and all(b["code"] != "knowledge_moved" for b in v["blockers"])
    assert all(o["code"] != "restate_request" for o in v["obligations"]) and {x["code"]: x["state"] for x in v["stages"]}["commit"] == "done"


def _work(client, env):
    r = client.get("/v2/work/inbox", params={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def _sec(inbox, cid):
    return {k: [i for i in v if i["proposal_job_id"] == cid] for k, v in inbox["sections"].items() if any(i["proposal_job_id"] == cid for i in v)}


def test_the_work_list_places_business_changes_from_the_same_projection(qa_env):
    from tests.final_loop.test_kno05_accept import answer as answer_gap
    q = qa_env
    cid, approver = prepared(q)
    mine = _sec(_work(q.req, q.env), cid)
    assert list(mine) == ["waiting_human"] and mine["waiting_human"][0]["waiting_on"] == "an approver" and mine["waiting_human"][0]["page"] == "questions"
    assert mine["waiting_human"][0]["params"] == {"chg": cid} and mine["waiting_human"][0]["step"] == "knowledge_decision"
    a = _sec(_work(approver, q.env), cid)
    assert list(a) == ["mine"] and a["mine"][0]["task"] == "decide_knowledge"
    assert a["mine"][0]["stage"] == tv(approver, q.env, "business_change", cid).json()["stage"]["code"]          # the list and the task view agree
    assert _sec(_work(q.admin, q.env), cid) == {}                                                              # may decide, is not asked: not in the work list
    viewer = login(q, f"viewer-wl-{q.env.nonce}", ("viewer",))
    assert _sec(_work(viewer, q.env), cid) == {}                                                               # cannot read the change at all
    binding = q.req.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws, "mode": "business"}).json()["binding"]
    assert approver.post(f"/v2/business/changes/{cid}/review/decide", json={"workspace_id": q.env.ws, "kind": "knowledge", "action": "request_changes",
                                                                         "reason": "Please state the scope.", "binding": binding}).status_code == 200
    r2 = _sec(_work(q.req, q.env), cid)
    assert list(r2) == ["mine"] and r2["mine"][0]["task"] == "respond_to_changes"                             # back with the requester
    assert _sec(_work(approver, q.env), cid) == {}                                                             # not asked while the requester answers
    gap = next(g for g in q.admin.get("/v2/business/gaps", params={"workspace_id": q.env.ws}).json()["gaps"] if g["kind"] == "missing_actor")
    answer_gap(q.admin, q.env, gap, {"actor": "finance director"}, "Only the finance director approves these.")
    r3 = _sec(_work(q.req, q.env), cid)
    assert list(r3) == ["mine"] and r3["mine"][0]["task"] == "restate_request" and r3["mine"][0]["blocked"] is True and r3["mine"][0]["blockers"]
    assert _sec(_work(approver, q.env), cid) == {} and _work(q.req, q.env)["unavailable"] == 0
