"""RQA-01 request visibility and safe context (no model, no network): real store, workflow and read services on the throwaway PostgreSQL. Hidden canaries (a private
creation chat, evidence-only paths) never reach a context pack; a hidden-only variation leaves a reader's pack unchanged; the business-change read rule holds for the pack;
the generic job read no longer bypasses the narrower read rules of business changes, interview readings, chat replies and evidence kinds; `ai_suspended` removes every AI
capability. Contract docs/contracts/REQUEST_TASKS_AND_ASSISTANT.md sections 2 and 4."""
from __future__ import annotations

import json

import pytest

from infra.adapters import v2_request_assistant as vra
from infra.adapters.v2_policy import AI_CAPABILITIES, permissions_for
from infra.adapters.v2_postgres import TenantScopeError
from tests.final_loop.test_chg04_review import login, prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.pitch_brief.chain import build
from tests.pitch_brief.test_draft_to_proposal import chain_to_proposal

CANARY_CHAT = "CANARY-PRIVATE-CHAT-5D21"
CANARY_CHAT2 = "CANARY-PRIVATE-CHAT-9E07"


def auth(env, account, roles):
    return {"tenant_id": env.tenant, "account_id": account, "roles": tuple(roles), "workspace_id": env.ws}


def private_chat(env, account, text, intent_id=None):
    from infra import v2_chat_store
    cid = v2_chat_store.create(env.store, env.tenant, env.ws, account, text, env.store.head(env.tenant, env.ws))
    v2_chat_store.add_turn(env.store, env.tenant, env.ws, cid, "user", text + " (follow-up)", account)
    if intent_id:
        v2_chat_store.set_status(env.store, env.tenant, env.ws, cid, "CONFIRMED", intent_id=intent_id)
    return cid


def blob(pack) -> str:
    return json.dumps(pack, ensure_ascii=False)


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def test_intent_flow_pack_never_carries_the_creation_chat_and_a_hidden_only_change_leaves_it_unchanged(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    private_chat(env, env.req, f"My manager is Taro Yamada; {CANARY_CHAT}", intent_id=c.intent)              # the requester's own creation chat, bound to the intent
    readers = {"viewer": auth(env, "viewer-x", ("viewer",)), "requester": auth(env, env.req, ("requester",)), "admin": auth(env, env.admin, ("admin",)),
               "approver": auth(env, "appr-x", ("change_approver",))}
    packs = {k: vra.build_context(env.store, env.tenant, env.ws, "intent_flow", c.intent, a) for k, a in readers.items()}
    for k, p in packs.items():
        assert CANARY_CHAT not in blob(p) and "Taro Yamada" not in blob(p), k                              # not even the creator's own chat (v1)
        assert p["facts"] and all(f["id"].startswith("F") for f in p["facts"]) and p["coverage"]["truncated"] is False
        assert env.req not in blob(p) and env.admin not in blob(p), k                                       # people appear as roles, never account ids
    assert any(f["kind"] == "obligation" and "held by you" in f["text"] for f in packs["approver"]["facts"])   # needs_check: an approver is asked
    assert not any("held by you" in f["text"] for f in packs["viewer"]["facts"])
    before = packs["viewer"]["manifest_hash"]
    private_chat(env, env.req, f"Another private note {CANARY_CHAT2}")                                       # hidden from the viewer: nothing of it may move the viewer's pack
    again = vra.build_context(env.store, env.tenant, env.ws, "intent_flow", c.intent, readers["viewer"])
    assert again["manifest_hash"] == before and CANARY_CHAT2 not in blob(again)
    cid = private_chat(env, env.req, f"Conversation only {CANARY_CHAT2}")                                    # a conversation-only request: its creator reads the task view, the pack has no chat text
    own = vra.build_context(env.store, env.tenant, env.ws, "intent_flow", cid, readers["requester"])
    assert CANARY_CHAT2 not in blob(own) and own["request_ref"]["id"] == cid
    with pytest.raises(TenantScopeError):
        vra.build_context(env.store, env.tenant, env.ws, "intent_flow", cid, readers["viewer"])            # the conversation rule: creator or knowledge confirmer only
    with pytest.raises(TenantScopeError):
        vra.build_context(env.store, env.tenant, "ws-other", "intent_flow", c.intent, readers["admin"])
    with pytest.raises(TenantScopeError):
        vra.build_context(env.store, "t-other", env.ws, "intent_flow", c.intent, dict(readers["admin"], tenant_id="t-other"))


def test_business_change_pack_follows_the_family_rule_and_the_evidence_right(qa_env):
    q = qa_env
    cid, _approver = prepared(q)
    env = q.env
    req_pack = vra.build_context(env.store, env.tenant, env.ws, "business_change", cid, auth(env, env.req, ("requester",)))
    text = blob(req_pack)
    assert ".yaml" not in text and "routing_rules" not in text and "rule-" not in text                      # Business shaping: no ids, paths or locators
    assert any(f["kind"] == "rule_change" for f in req_pack["facts"]) and any(f["kind"] == "obligation" for f in req_pack["facts"])
    appr = vra.build_context(env.store, env.tenant, env.ws, "business_change", cid, auth(env, "appr-b", ("change_approver", "reviewer")))
    assert appr["auth"]["evidence_read"] is True and appr["manifest_hash"] != req_pack["manifest_hash"]       # another reader is another context
    for roles in (("viewer",), ("reviewer",), ("judge",), ("restore_operator",)):
        with pytest.raises(TenantScopeError):
            vra.build_context(env.store, env.tenant, env.ws, "business_change", cid, auth(env, f"x-{roles[0]}", roles))


def test_the_generic_job_read_applies_the_narrower_read_rules(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    viewer = login(q, f"viewer-j-{q.env.nonce}", ("viewer",))
    reviewer = login(q, f"rev-j-{q.env.nonce}", ("reviewer",))
    get = lambda client, jid: client.get(f"/v2/jobs/{jid}", params={"workspace_id": q.env.ws})          # noqa: E731
    assert get(q.req, cid).status_code == 200 and get(approver, cid).status_code == 200                    # the requester and a person who may decide
    for who in (viewer, reviewer):
        r = get(who, cid)
        assert r.status_code == 404 and "request" not in r.text.lower() or r.status_code == 404, r.text    # before RQA-01: 200 with the request text
        assert (r.json().get("payload") or {}) == {}
    with q.env.store._tx() as cur:                                                                            # every job bound to that change follows the same rule
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND payload->>'change_id'=%s", (q.env.tenant, q.env.ws, cid))
        bound = [r[0] for r in cur.fetchall()]
    for jid in bound:
        assert get(viewer, jid).status_code == 404 and get(approver, jid).status_code == 200, jid
    with q.env.store._tx() as cur:
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='knowledge.qa' LIMIT 1", (q.env.tenant, q.env.ws))
        row = cur.fetchone()
    if row:
        assert get(viewer, row[0]).status_code == 200                                                         # a kind without a narrower rule keeps the generic rule


def test_interview_readings_and_chat_replies_are_not_readable_through_the_generic_job_read(qa_env):
    from tests.final_loop.test_kno07_answer_interpret import actor_gap, mobj, run
    from tests.final_loop.test_qry03_qa import use
    q = qa_env
    use(q, lambda p: mobj("actor", "finance director", quote="Only the finance director approves these."))
    _got, jid = run(q, q.admin, actor_gap(q)["gap_id"], "Only the finance director approves these.")
    reviewer = login(q, f"rev-i-{q.env.nonce}", ("reviewer",))
    assert q.admin.get(f"/v2/jobs/{jid}", params={"workspace_id": q.env.ws}).status_code == 200
    r = reviewer.get(f"/v2/jobs/{jid}", params={"workspace_id": q.env.ws})
    assert r.status_code == 404 and "finance director" not in r.text                                         # the KNO-07 route needs knowledge.confirm; so does the generic read now


def test_ai_suspended_removes_every_ai_capability():
    assert {"ai.use", "request.assistant.ask"} <= AI_CAPABILITIES
    for roles in (("requester", "ai_suspended"), ("admin", "ai_suspended"), ("reviewer", "change_approver", "ai_suspended")):
        assert not (permissions_for(roles) & AI_CAPABILITIES), roles
    assert "ai.use" in permissions_for(("requester",))


def test_generic_job_actions_follow_the_read_rule_and_revocation_cross_tenant_and_multi_role_hold(qa_env):
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.final_loop.test_kno07_answer_interpret import actor_gap, mobj, run
    from tests.final_loop.test_qry03_qa import use
    q = qa_env
    use(q, lambda p: mobj("actor", "finance director", quote="Only the finance director approves these."))
    _got, gai = run(q, q.admin, actor_gap(q)["gap_id"], "Only the finance director approves these.")         # a COMPLETED interview reading (knowledge.confirm to read)
    cid, approver = prepared(q)
    reviewer = login(q, f"rev-x-{q.env.nonce}", ("reviewer",))                                               # holds ai.use: the action permission of both kinds
    for path in ("cancel", "retry", "approve", "publish"):
        missing = reviewer.post(f"/v2/jobs/gai-does-not-exist/{path}", json={"workspace_id": q.env.ws})
        for jid in (gai, cid):
            r = reviewer.post(f"/v2/jobs/{jid}/{path}", json={"workspace_id": q.env.ws})
            assert (r.status_code, r.json()) == (missing.status_code, missing.json()) and "not found" in r.text, (path, jid, r.status_code, r.text)   # same as a missing job
    multi = login(q, f"multi-{q.env.nonce}", ("viewer", "reviewer"))                                        # a union of roles reads what one of them may read, no more
    assert multi.get(f"/v2/jobs/{cid}", params={"workspace_id": q.env.ws}).status_code == 404
    assert multi.get(f"/v2/jobs/{gai}", params={"workspace_id": q.env.ws}).status_code == 404
    owner = login(q, f"ko-x-{q.env.nonce}", ("knowledge_owner",))
    assert owner.get(f"/v2/jobs/{gai}", params={"workspace_id": q.env.ws}).status_code == 200 and owner.get(f"/v2/jobs/{cid}", params={"workspace_id": q.env.ws}).status_code == 200
    acct = f"appr-x-{q.env.nonce}"
    temp = login(q, acct, ("change_approver",))
    assert temp.get(f"/v2/jobs/{cid}", params={"workspace_id": q.env.ws}).status_code == 200
    PostgresIdentityStore(q.env.dsn).grant_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, acct, ("viewer",))   # roles replaced: the next read is checked again
    r = temp.get(f"/v2/jobs/{cid}", params={"workspace_id": q.env.ws})
    assert r.status_code == 404 and (r.json().get("payload") or {}) == {}
    other = q.admin.get(f"/v2/jobs/{cid}", params={"workspace_id": "ws-of-someone-else"})                   # another workspace / tenant: refused (existing 401 rule), nothing shown
    assert other.status_code in (401, 403, 404) and "payload" not in other.text and cid not in other.text
