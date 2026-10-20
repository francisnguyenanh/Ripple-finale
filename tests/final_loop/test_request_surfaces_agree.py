"""RUX-06 / RQA-05: the task view, My work and the request assistant's context say the same thing to the same reader, for both request families (real routes, store and
workflow on the throwaway PostgreSQL; no model call: the assistant context is built, never sent). Permission is not assignment: an administrator who may decide is not asked
on any surface. A request whose view cannot be read is counted as unavailable in My work, never shown as "no work"."""
from __future__ import annotations

from tests.final_loop.test_chg04_review import login, prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_task_view import _grant_roles, ch, tv  # noqa: F401  (ch is a fixture)
from tests.pitch_brief.chain import S1, intent
from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal


def _authority(env, account, roles):
    return {"tenant_id": env.tenant, "workspace_id": env.ws, "account_id": account, "roles": tuple(roles)}


def _agree(client, env, family, rid, account, roles):
    """Every surface for one reader: the projection, its place in My work and the facts the assistant would get."""
    from infra.adapters import v2_request_assistant as vra
    view = tv(client, env, family, rid).json()
    work = client.get("/v2/work/inbox", params={"workspace_id": env.ws}).json()
    pack = vra.build_context(env.store, env.tenant, env.ws, family, rid, _authority(env, account, roles))
    facts = pack["facts"]
    assert facts[0]["kind"] == "state" and view["stage"]["label"] in facts[0]["text"]                             # the same stage
    asked = {o["code"] for o in view["obligations"] if o["obligation_me"] and o["state"] == "current" and o["required"]}
    held_by_me = {f["text"].split(" - ")[0] for f in facts if f["kind"] == "obligation" and f["data"]["you"]}
    assert held_by_me == {o["label"] for o in view["obligations"] if o["obligation_me"]}                           # "held by you" only where the view says so
    placed = [(k, i) for k, items in work["sections"].items() for i in items if i["request_id"] == view["request_ref"]["id"]]
    assert len(placed) <= 1
    if asked:
        assert placed and placed[0][0] == "mine" and placed[0][1]["task"] in asked and placed[0][1]["stage"] == view["stage"]["code"]
    else:
        assert not placed or placed[0][0] != "mine"
    return view, placed


def test_business_change_surfaces_agree_for_requester_approver_admin(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    env = q.env
    _agree(q.req, env, "business_change", cid, env.req, ("requester",))
    view, placed = _agree(approver, env, "business_change", cid, f"approver-{env.nonce}", ("change_approver", "reviewer"))
    assert placed[0][0] == "mine"
    view, placed = _agree(q.admin, env, "business_change", cid, env.admin, ("admin",))
    d = next(o for o in view["obligations"] if o["code"] == "decide_knowledge")
    assert d["eligible_me"] is True and d["obligation_me"] is False and not placed                                # may decide, not asked anywhere
    binding = q.req.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": env.ws, "mode": "business"}).json()["binding"]
    assert approver.post(f"/v2/business/changes/{cid}/review/decide", json={"workspace_id": env.ws, "kind": "knowledge", "action": "request_changes",
                                                                         "reason": "Please state the scope.", "binding": binding}).status_code == 200
    view, placed = _agree(q.req, env, "business_change", cid, env.req, ("requester",))
    assert placed[0][1]["task"] == "respond_to_changes"
    _agree(approver, env, "business_change", cid, f"approver-{env.nonce}", ("change_approver", "reviewer"))


def test_intent_flow_surfaces_agree_before_the_draft_and_at_the_proposal(env, ch, monkeypatch):
    req = env.req_client()
    iid = intent(env, req, S1)["intent_id"]
    _agree(req, env, "intent_flow", iid, env.req, ("requester",))
    judge_c = _grant_roles(env, ("judge",))
    view = tv(judge_c, env, "intent_flow", iid).json()
    assert next(o for o in view["obligations"] if o["code"] == "ask_draft")["obligation_me"] is False             # eligible, not assigned
    c = chain_to_proposal(env, ch, monkeypatch)
    _agree(c.req, env, "intent_flow", c.intent, env.req, ("requester",))
    view, placed = _agree(env.admin_client(), env, "intent_flow", c.intent, env.admin, ("admin",))
    assert not placed or placed[0][0] != "mine"


def test_a_request_whose_view_fails_is_counted_unavailable_not_hidden(qa_env, monkeypatch):
    from infra.adapters import v2_request_tasks as rt
    q = qa_env
    cid, approver = prepared(q)
    real = rt.task_view

    def broken(store, tenant, ws_, family, rid, authority):
        out = real(store, tenant, ws_, family, rid, authority)                       # the read rule first: an unreadable request stays unreadable
        if rid == cid:
            raise RuntimeError("simulated read failure")
        return out

    monkeypatch.setattr(rt, "task_view", broken)
    work = approver.get("/v2/work/inbox", params={"workspace_id": q.env.ws}).json()
    assert work["unavailable"] == 1 and all(i["request_id"] != cid for items in work["sections"].values() for i in items)
    viewer = login(q, f"viewer-un-{q.env.nonce}", ("viewer",))
    assert viewer.get("/v2/work/inbox", params={"workspace_id": q.env.ws}).json()["unavailable"] == 0             # not readable: not counted
