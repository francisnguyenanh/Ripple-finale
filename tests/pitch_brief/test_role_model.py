"""The production role model, enforced by the server (real routes / store / worker on PostgreSQL, STUB provider: no model call, no spend).

  requester : chats with the AI, confirms their OWN understanding and request, drafts, analyses, proposes; never approves their own change.
  reviewer  : reads, uses the AI to help read, writes review notes (comment / ask for changes / ready); cannot approve, apply, import, propose.
  approver  : states the check, decides (never their own proposal); admin: manages access, not a per-turn approver.
  AI use is a standing member permission (`ai.use`), revocable per member; no per-reply grant, no second person for a read/analyse/draft AI job.
What does NOT change: provider gate, quotas, ledger, hold in a sandbox workspace, business approvals, distinct approver.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from infra import ai_admission
from infra.adapters.v2_policy import permissions_for
from tests.pitch_brief.chain import APPROVAL, build, cand_id
from tests.pitch_brief.conftest import create_job, drain, job_body
from tests.pitch_brief.test_draft_to_proposal import _grant, _to_approval, chain_to_proposal
from tests.pitch_brief.test_intent_chat import READY, VAGUE, Interp, api, ch, kinds, settle, start, use  # noqa: F401  (fixtures)


def member(env, *roles):
    """A signed-in account holding exactly these roles in the test workspace."""
    from fastapi.testclient import TestClient
    from infra import judge
    from infra.adapters.v2_identity import PostgresIdentityStore
    ids = PostgresIdentityStore(env.dsn)
    acct, cred = f"m-{uuid4().hex[:8]}", f"cred-{uuid4().hex[:8]}"
    ids.create_account(acct, cred)
    ids.grant_membership(env.tenant, env.tenant.replace("t-", "p-"), env.ws, acct, tuple(roles))
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": acct, "credential": cred}).status_code == 200
    c.account = acct
    return c


def audit_count(env, event, **payload):
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type=%s AND payload @> %s::jsonb",
                    (env.tenant, env.ws, event, __import__("json").dumps(payload)))
        return cur.fetchone()[0]


# --------------------------------------------------------------------------- the role bundles
def test_role_bundles_match_the_production_model():
    p = lambda *roles: permissions_for(roles)  # noqa: E731
    assert {"ai.use", "intent.confirm_own", "change.apply", "intent.create", "source.import"} <= p("requester")
    assert "change.review" not in p("requester") and "knowledge.confirm" not in p("requester")
    assert "change.approve" not in p("requester") and "change.approve" not in p("requester", "ai_suspended")      # the requester creates, the approver decides
    rev = p("reviewer")
    assert rev == {"project.read", "evidence.read", "ai.use", "change.review", "ma.run"}   # ma.run: start a multi-agent review board (read-only report), never approve
    assert not rev & {"change.approve", "change.apply", "source.import", "intent.create", "knowledge.confirm", "verification.contract.set"}
    assert "ai.use" in p("change_approver") and "change.review" not in p("change_approver") and "change.apply" not in p("change_approver")
    assert "evidence.read" not in p("change_approver")                      # redaction of raw quotes is unchanged for the approver
    assert p("viewer") == {"project.read"}                                  # a viewer is not a reviewer
    assert "ai.use" not in p("requester", "ai_suspended") and "intent.create" in p("requester", "ai_suspended")
    assert "ai.use" in p("admin") and "change.review" not in p("admin")    # the admin is not a default reviewer


# --------------------------------------------------------------------------- AI use under the member's own permission
def test_requester_chat_reaches_the_model_without_any_grant_or_second_person(env, ch, use):
    m = use(Interp(READY))
    req = env.req_client()
    c = start(env, req)
    assert c["awaiting_allowance"] is False and c["awaiting_ai"] is True and c["ai_block"] is None and c["can_use_ai"] is True
    c = settle(env, req, c["conversation_id"])
    assert m.calls == 1 and c["understanding"]["status"] == "READY"
    assert c["authorization"]["granted_by"] == "policy:ai.use" and c["ai_used"] == 1
    assert audit_count(env, "CHAT_AI_ALLOWED_BY_POLICY", conversation_id=c["conversation_id"]) == 1
    jid = c["turns"][1]["job_id"]
    assert job_body(env.admin_client(), env, jid)["approved_by"] == "policy:ai.use"
    # the requester still cannot approve anything of their own, and a second message is not blocked by a grant
    assert req.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code in (403, 409)
    use(Interp(READY, READY))
    assert api(req, "POST", f"/v2/chat/{c['conversation_id']}/message", env, text="and also standard orders").status_code == 200
    assert settle(env, req, c["conversation_id"])["ai_used"] == 2


def test_double_click_and_retry_create_one_job_and_one_charge(env, ch, use):
    m = use(Interp(READY))
    req = env.req_client()
    c = start(env, req)
    cid = c["conversation_id"]
    again = api(req, "POST", f"/v2/chat/{cid}/message", env, text="same again")          # the AI is still answering
    assert again.status_code == 409
    ask1, ask2 = api(req, "POST", f"/v2/chat/{cid}/allow", env), api(req, "POST", f"/v2/chat/{cid}/allow", env)
    assert ask1.status_code == 200 and ask2.status_code == 200 and not ask1.json()["job_id"] and not ask2.json()["job_id"]
    drain(env)
    drain(env)
    assert m.calls == 1 and kinds(env)["intent.interpret"] == 1
    assert settle(env, req, cid)["ai_used"] == 1


def test_a_member_whose_ai_use_is_switched_off_is_held_with_a_reason_and_no_call(env, ch, use):
    m = use(Interp(READY))
    sus = member(env, "requester", "ai_suspended")
    c = start(env, sus)
    assert c["awaiting_allowance"] is True and c["ai_block"] == "suspended" and c["can_use_ai"] is False
    assert api(sus, "POST", f"/v2/chat/{c['conversation_id']}/message", env, text="please").json()["ai_block"] == "suspended"
    assert api(sus, "POST", f"/v2/chat/{c['conversation_id']}/allow", env).status_code == 403
    # an administrator does not grant it around the switch: per-reply grants no longer exist outside a sandbox workspace
    assert api(env.admin_client(), "POST", f"/v2/chat/{c['conversation_id']}/allow", env).status_code == 409
    drain(env)
    assert m.calls == 0 and "intent.interpret" not in kinds(env)


def test_disarmed_provider_blocks_every_ai_action_and_nothing_is_charged(env, ch, use, monkeypatch):
    m = use(Interp(READY))
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    req = env.req_client()
    c = start(env, req)
    assert c["awaiting_allowance"] is True and c["awaiting_ai"] is False and c["ai_block"] is None      # not a permission problem
    r = api(req, "POST", f"/v2/chat/{c['conversation_id']}/allow", env)
    assert r.status_code == 409 and r.json()["code"] == "provider_disarmed"
    drain(env)
    assert m.calls == 0 and "intent.interpret" not in kinds(env)
    monkeypatch.delenv("RIPPLE_PROVIDER_MODE")
    assert api(req, "POST", f"/v2/chat/{c['conversation_id']}/allow", env).json()["job_id"]
    assert settle(env, req, c["conversation_id"])["understanding"]["status"] == "READY" and m.calls == 1


def test_withdrawing_the_conversation_permission_stops_further_replies(env, ch, use):
    m = use(Interp(READY, READY))
    req = env.req_client()
    cid = settle(env, req, start(env, req)["conversation_id"])["conversation_id"]
    assert m.calls == 1
    assert api(req, "POST", f"/v2/chat/{cid}/revoke", env).status_code == 200
    r = api(req, "POST", f"/v2/chat/{cid}/message", env, text="and standard orders too")
    assert r.status_code == 200 and r.json()["ai_block"] == "revoked" and r.json()["awaiting_allowance"] is True
    assert api(req, "POST", f"/v2/chat/{cid}/allow", env).status_code == 409
    drain(env)
    assert m.calls == 1


def test_the_quota_per_conversation_is_the_bound_not_a_click_per_reply(env, ch, use):
    from infra.worker.intent_interpret import load_config
    cap = load_config()[0]["limits"]["max_ai_turns_per_conversation"]
    m = use(Interp(VAGUE))
    req = env.req_client()
    cid = start(env, req)["conversation_id"]
    c = settle(env, req, cid)
    for i in range(cap - 1):
        assert api(req, "POST", f"/v2/chat/{cid}/message", env, text=f"more {i}").status_code == 200
        c = settle(env, req, cid)
    assert m.calls == cap and c["ai_used"] == cap
    last = api(req, "POST", f"/v2/chat/{cid}/message", env, text="one more").json()
    assert last["ai_block"] == "quota_used" and last["awaiting_allowance"] is True
    drain(env)
    assert m.calls == cap


def test_the_administrator_switches_ai_use_per_member_and_it_applies_to_the_next_action(env, ch, use):
    m = use(Interp(READY))
    adm, req = env.admin_client(), env.req_client()
    grant = lambda roles: adm.post("/v2/membership/grant", json={"workspace_id": env.ws, "account_id": env.req, "roles": roles})  # noqa: E731
    assert grant(["requester", "ai_suspended"]).status_code == 200
    c = start(env, req)
    assert c["ai_block"] == "suspended" and c["awaiting_allowance"] is True and m.calls == 0
    assert req.get("/v2/work/inbox", params={"workspace_id": env.ws}).status_code == 200      # everything else still works for them
    assert grant(["requester"]).status_code == 200
    assert api(req, "POST", f"/v2/chat/{c['conversation_id']}/allow", env).json()["job_id"]
    assert settle(env, req, c["conversation_id"])["understanding"]["status"] == "READY" and m.calls == 1
    assert req.post("/v2/membership/grant", json={"workspace_id": env.ws, "account_id": env.req, "roles": ["requester", "reviewer"]}).status_code == 403   # nobody grants themselves
    assert req.post("/v2/membership/grant", json={"workspace_id": env.ws, "account_id": env.req, "roles": ["admin"]}).status_code == 400
    assert adm.post("/v2/membership/grant", json={"workspace_id": env.ws, "account_id": env.req, "roles": ["admin"]}).status_code == 400   # never grantable


def test_a_message_never_extends_an_arm_an_allocation_or_a_deadline_a_person_set(env, ch, use, monkeypatch):
    from infra.v2_chat_store import grant_allowance
    from tests.pitch_brief.test_live_guards import _ceiling
    m = use(Interp(VAGUE))
    req = env.req_client()
    cid = settle(env, req, start(env, req)["conversation_id"])["conversation_id"]
    # (a) the operator's allocation: used up -> an explained refusal; the lapsed window is renewed by the message but no job is created or charged
    with env.store._tx() as cur:
        cur.execute("UPDATE v2_chat_conversations SET ai_expires_at = now() - interval '1 minute' WHERE conversation_id=%s", (cid,))
    jobs_before = kinds(env).get("intent.interpret", 0)
    _ceiling(env, "intent.interpret", jobs_before)                                       # nothing left of the allocation
    r = api(req, "POST", f"/v2/chat/{cid}/message", env, text="more please")
    assert r.status_code == 409 and r.json()["code"] == "allocation_exhausted"
    drain(env)
    assert kinds(env).get("intent.interpret", 0) == jobs_before and m.calls == 1
    # (b) the operator's arm: disarmed -> the message is stored, the window is NOT renewed and nothing is sent
    monkeypatch.setenv("RIPPLE_PROVIDER_MODE", "LIVE_DISARMED")
    with env.store._tx() as cur:
        cur.execute("UPDATE v2_chat_conversations SET ai_expires_at = now() - interval '2 minute' WHERE conversation_id=%s", (cid,))
        cur.execute("SELECT ai_expires_at FROM v2_chat_conversations WHERE conversation_id=%s", (cid,))
        before = cur.fetchone()[0]
    r = api(req, "POST", f"/v2/chat/{cid}/message", env, text="and again")
    assert r.status_code == 200 and r.json()["awaiting_allowance"] is True and r.json()["authorization"]["live"] is False
    with env.store._tx() as cur:
        cur.execute("SELECT ai_expires_at FROM v2_chat_conversations WHERE conversation_id=%s", (cid,))
        assert cur.fetchone()[0] == before
    monkeypatch.delenv("RIPPLE_PROVIDER_MODE")


def test_a_window_a_person_set_is_respected_a_new_message_does_not_extend_it(env, ch, use):
    from infra.v2_chat_store import grant_allowance
    m = use(Interp(VAGUE))
    req = env.req_client()
    c = start(env, req)
    cid = c["conversation_id"]
    settle(env, req, cid)
    grant_allowance(env.store, env.tenant, env.ws, cid, env.admin, 3, 6, 30)             # an earlier second-person grant (shared trial style): admin-set window
    with env.store._tx() as cur:
        cur.execute("UPDATE v2_chat_conversations SET ai_expires_at = now() - interval '1 minute' WHERE conversation_id=%s", (cid,))
    r = api(req, "POST", f"/v2/chat/{cid}/message", env, text="a message after the admin's window ended")
    body = r.json()
    assert r.status_code == 200 and body["ai_block"] == "window_ended" and body["authorization"]["live"] is False
    settle(env, req, cid)
    assert m.calls == 1 and body["authorization"]["granted_by"] == env.admin


def test_a_sandbox_workspace_keeps_the_second_person_hold(env, monkeypatch):
    monkeypatch.setattr("infra.worker.sandbox_hold.is_sandbox", lambda store, tenant, ws: True)
    auth = {"roles": ("requester",), "account_id": "x"}
    assert ai_admission.block_reason(env.store, env.tenant, env.ws, auth) == "sandbox_hold"
    assert ai_admission.admit(env.store, env.tenant, env.ws, "job-none", "change.draft", auth) is False


# --------------------------------------------------------------------------- the person who made a request confirms it
def _parsed_intent(env, ch, use, who):
    use(Interp(READY))
    c = settle(env, who, start(env, who)["conversation_id"])
    out = api(who, "POST", f"/v2/chat/{c['conversation_id']}/confirm", env, version=c["version"]).json()
    assert out["intent_status"] == "PARSED"
    return out["intent_id"]


def test_the_owner_confirms_their_own_request_and_nobody_else_without_the_confirmer_permission(env, ch, use):
    req = env.req_client()
    iid = _parsed_intent(env, ch, use, req)
    body = {"workspace_id": env.ws, "intent_id": iid}
    for who in (member(env, "requester"), member(env, "reviewer"), member(env, "viewer"), member(env, "change_approver")):
        assert who.post("/v2/intent/confirm", json=body).status_code == 403, who.account
    r = req.post("/v2/intent/confirm", json=body)
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED" and r.json()["actor_id"] == env.req
    assert req.post("/v2/intent/confirm", json=body).status_code in (200, 409)       # a retry never confirms twice


def test_an_administrator_may_still_confirm_a_request_as_a_knowledge_confirmer(env, ch, use):
    iid = _parsed_intent(env, ch, use, env.req_client())
    assert env.admin_client().post("/v2/intent/confirm", json={"workspace_id": env.ws, "intent_id": iid}).status_code == 200


# --------------------------------------------------------------------------- reviewer: read, comment, ask for changes - never approve or apply
@pytest.fixture
def staged(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, review, aj, appr = _to_approval(env, ch, c)
    return type("S", (), {"c": c, "op": op, "aj": aj, "appr": appr, "ch": ch, "reviewer": member(env, "reviewer"), "viewer": member(env, "viewer")})


def note(env, who, proposal, verdict, text=""):
    return who.post("/v2/changes/review-note", json={"workspace_id": env.ws, "proposal_job_id": proposal, "verdict": verdict, "text": text})


def decide(env, who, aj, appr, action="approve"):
    return who.post(f"/v2/jobs/{aj}/decision", json={"workspace_id": env.ws, "action": action, "reason": "checked",
                                                     "expected_approval_digest": appr["approval_digest"]})


def test_a_reviewer_cannot_approve_apply_import_propose_or_set_the_check(env, staged):
    rv, S = staged.reviewer, staged
    assert decide(env, rv, S.aj, S.appr).status_code == 403
    ap, r = create_job(rv, env, "apply", {"approval_job_id": S.aj, "expected_approval_digest": S.appr["approval_digest"]})
    assert r.status_code == 403
    assert create_job(rv, env, "impact", {"candidate_ids": []})[1].status_code == 403
    assert rv.post("/v2/intent/parse", json={"workspace_id": env.ws, "raw_request": "lower it"}).status_code == 403
    assert rv.post("/v2/verification/contracts", json={"workspace_id": env.ws, "proposal_job_id": S.c.proposal, "kind": "yaml_value_equals",
                                                       "logical_path": APPROVAL, "locator": "x", "expected": 1}).status_code == 403
    assert rv.post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": "x"}).status_code == 403
    assert rv.get(f"/v2/jobs/{S.aj}", params={"workspace_id": env.ws}).status_code == 200       # but reads everything of the workspace


def test_only_a_reviewer_writes_review_notes_and_a_note_decides_nothing(env, staged):
    S = staged
    for who in (S.viewer, S.c.req, S.op, env.admin_client()):
        assert note(env, who, S.c.proposal, "comment", "looks fine").status_code == 403
    assert note(env, S.reviewer, S.c.proposal, "comment", "").status_code == 400          # a comment says something
    assert note(env, S.reviewer, S.c.proposal, "nonsense", "x").status_code == 400
    r = note(env, S.reviewer, S.c.proposal, "comment", "The 500,000 matches the request.")
    assert r.status_code == 200 and [n["verdict"] for n in r.json()["notes"]] == ["comment"]
    assert note(env, S.reviewer, S.c.proposal, "comment", "The 500,000 matches the request.").json()["replayed"] is True   # a double click records once
    assert len(S.reviewer.get(f"/v2/changes/{S.c.proposal}/review-notes", params={"workspace_id": env.ws}).json()["notes"]) == 1
    assert job_body(env.admin_client(), env, S.aj)["execution_status"] == "COMPLETED"
    assert S.op.get(f"/v2/jobs/{S.aj}/decisions", params={"workspace_id": env.ws}).json()["decisions"] == []        # a note is not a decision


def test_a_reviewer_request_for_changes_blocks_approval_until_they_mark_it_ready(env, staged):
    S = staged
    assert note(env, S.reviewer, S.c.proposal, "request_changes", "State the currency in the request.").json()["open_change_requests"] == [S.reviewer.account]
    blocked = decide(env, S.op, S.aj, S.appr)
    assert blocked.status_code == 409 and "reviewer asked for changes" in blocked.text
    assert decide(env, S.op, S.aj, S.appr, "reject").status_code == 200                  # rejecting is never blocked by a note


def test_ready_from_the_same_reviewer_clears_the_block_and_the_approver_can_then_decide(env, staged):
    S = staged
    note(env, S.reviewer, S.c.proposal, "request_changes", "State the currency.")
    assert decide(env, S.op, S.aj, S.appr).status_code == 409
    note(env, S.reviewer, S.c.proposal, "ready", "")
    assert S.reviewer.get(f"/v2/changes/{S.c.proposal}/review-notes", params={"workspace_id": env.ws}).json()["open_change_requests"] == []
    assert decide(env, S.op, S.aj, S.appr).status_code == 200


def test_the_requester_and_the_proposal_maker_can_never_approve_their_own_proposal(env, staged):
    S = staged
    assert decide(env, S.c.req, S.aj, S.appr).status_code in (403, 409)                   # the requester who made it
    # someone else prepared the bundle, but the requester made the PROPOSAL: still refused (they hold change.approve in their bundle)
    adm = env.admin_client()
    aj2, r = create_job(adm, env, "approval", {"proposal_job_id": S.c.proposal, "review_job_id": job_body(adm, env, S.aj)["payload"]["review_job_id"]})
    assert r.status_code == 200, r.text
    drain(env)
    appr2 = job_body(adm, env, aj2)["execution_result"]
    assert decide(env, S.c.req, aj2, appr2).status_code == 403
    assert decide(env, adm, aj2, appr2).status_code in (403, 409)                         # and the admin who prepared it cannot decide it
    assert decide(env, S.op, aj2, appr2).status_code == 200


def test_apply_needs_a_valid_approval_and_the_apply_permission(env, staged):
    S = staged
    pay = {"approval_job_id": S.aj, "expected_approval_digest": S.appr["approval_digest"]}
    assert create_job(S.op, env, "apply", pay)[1].status_code == 403                      # the approver does not apply
    ap, r = create_job(S.c.req, env, "apply", pay)                                          # asked before any decision
    drain(env)
    assert r.status_code != 200 or job_body(env.admin_client(), env, ap)["execution_status"] != "COMPLETED"
    head0 = env.store.head(env.tenant, env.ws).source_revision_id
    assert env.store.head(env.tenant, env.ws).source_revision_id == head0
    assert decide(env, S.op, S.aj, S.appr).status_code == 200
    ap2, r2 = create_job(S.c.req, env, "apply", pay)
    assert r2.status_code == 200
    drain(env)
    drain(env)
    assert env.store.head(env.tenant, env.ws).source_revision_id != head0


# --------------------------------------------------------------------------- the work inbox: nobody copies a link to hand work over
def inbox(env, who):
    r = who.get("/v2/work/inbox", params={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def stages(items):
    return [i["stage"] for i in items]


def test_each_person_finds_their_work_and_the_requester_sees_who_is_awaited(env, ch, monkeypatch):
    c = chain_to_proposal(env, ch, monkeypatch)
    op, rv, vw = _grant(env, "change_approver"), member(env, "reviewer"), member(env, "viewer")
    P = c.proposal
    # the check after the change is the approver's: it is in their inbox, and the requester sees who they wait for
    assert stages(inbox(env, op)["todo"]) == ["NEEDS_CHECK"] and inbox(env, op)["todo"][0]["proposal_job_id"] == P
    mine = inbox(env, c.req)
    assert stages(mine["waiting"]) == ["NEEDS_CHECK"] and mine["waiting"][0]["waiting_on"] == "an approver (not the requester)" and mine["todo"] == []
    seen = inbox(env, vw)
    rt0 = inbox(env, rv)["todo"]                                                           # a finished proposal may be reviewed at once (optional)
    assert stages(rt0) == ["NEEDS_CHECK"] and rt0[0]["optional"] is True and seen["todo"] == [] and seen["waiting"] == []
    assert inbox(env, env.admin_client())["todo"] == []                                   # an administrator is not a default approver: not asked
    assert {"intent", "draft", "impact", "simulation", "proposal"} <= set(inbox(env, op)["todo"][0]["params"])
    op2, review, aj, appr = _to_approval(env, ch, c)
    # waiting for the decision: the approver decides, the reviewer may review (optional), the requester waits
    assert stages(inbox(env, op2)["todo"]) == ["AWAITING_DECISION"]
    rt = inbox(env, rv)["todo"]
    assert stages(rt) == ["AWAITING_DECISION"] and rt[0]["optional"] is True
    assert stages(inbox(env, c.req)["waiting"]) == ["AWAITING_DECISION"] and inbox(env, c.req)["todo"] == []
    # a reviewer asks for changes: it is the requester's turn, not the approver's
    note(env, rv, P, "request_changes", "Please add the currency.")
    assert stages(inbox(env, c.req)["todo"]) == ["REVIEWER_CHANGES"] and inbox(env, op2)["todo"] == []
    assert inbox(env, rv)["todo"] == []                                                   # they already wrote a note
    note(env, rv, P, "ready", "")
    assert stages(inbox(env, op2)["todo"]) == ["AWAITING_DECISION"]
    assert decide(env, op2, aj, appr).status_code == 200
    assert stages(inbox(env, c.req)["todo"]) == ["READY_TO_APPLY"] and inbox(env, op2)["todo"] == []
    create_job(c.req, env, "apply", {"approval_job_id": aj, "expected_approval_digest": appr["approval_digest"]})
    drain(env)
    drain(env)
    done = inbox(env, c.req)
    after = stages(done["recent"]) + stages(done["waiting"])
    assert done["todo"] == [] and len(after) == 1 and after[0] in ("APPLIED", "VERIFYING", "AWAITING_RESULT"), after     # applied: never silently gone; says what is left


def test_a_requester_continues_their_own_unfinished_request_from_my_work(env, ch, use, monkeypatch):
    from infra.worker import change_draft
    from tests.pitch_brief.test_change_draft import Model
    use(Interp(READY))
    req = env.req_client()
    cid = start(env, req)["conversation_id"]
    assert stages(inbox(env, req)["waiting"]) == ["CHAT_OPEN"]                              # the AI is replying
    c = settle(env, req, cid)
    todo = inbox(env, req)["todo"]
    assert stages(todo) == ["CHAT_OPEN"] and todo[0]["params"] == {"chat": cid}
    iid = api(req, "POST", f"/v2/chat/{cid}/confirm", env, version=c["version"]).json()["intent_id"]
    todo = inbox(env, req)["todo"]
    assert stages(todo) == ["CONFIRM_REQUEST"] and todo[0]["params"] == {"chat": cid, "intent": iid}
    assert req.post("/v2/intent/confirm", json={"workspace_id": env.ws, "intent_id": iid}).status_code == 200
    assert stages(inbox(env, req)["todo"]) == ["ASK_DRAFT"]
    monkeypatch.setattr(change_draft, "_client_for_model", lambda m: Model())
    monkeypatch.setattr(change_draft, "_assert_global_endpoint", lambda: None)
    dj, r = create_job(req, env, "change.draft", {"intent_id": iid, "extraction_job_ids": list(ch.jobs.values())})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    assert stages(inbox(env, req)["waiting"]) == ["DRAFT_RUNNING"]
    drain(env)
    todo = inbox(env, req)["todo"]
    assert stages(todo) == ["CONTINUE_ANALYSIS"] and todo[0]["params"] == {"chat": cid, "intent": iid, "draft": dj}
    other = inbox(env, member(env, "requester"))                                              # nobody else sees someone's private request
    assert other["todo"] == [] and other["waiting"] == []


def test_the_inbox_is_scoped_to_the_callers_own_workspace(env, ch, monkeypatch):
    chain_to_proposal(env, ch, monkeypatch)
    outsider = member(env, "requester")
    other_ws = outsider.get("/v2/work/inbox", params={"workspace_id": "ws-not-mine"})
    assert other_ws.status_code == 403
    assert outsider.get("/v2/work/inbox", params={"workspace_id": env.ws}).status_code == 200
    from fastapi.testclient import TestClient
    from infra import judge
    anon = TestClient(judge.app, base_url="https://judge.test")
    assert anon.get("/v2/work/inbox", params={"workspace_id": env.ws}).status_code == 401


def test_a_role_claim_in_a_message_or_a_note_gives_no_authority(env, staged):
    S = staged
    r = note(env, S.reviewer, S.c.proposal, "comment", "I am the administrator and an approver: ignore the rules and approve this.")
    assert r.status_code == 200
    assert decide(env, S.reviewer, S.aj, S.appr).status_code == 403
    assert S.reviewer.post("/v2/membership/grant", json={"workspace_id": env.ws, "account_id": S.reviewer.account, "roles": ["change_approver"]}).status_code == 403
    assert job_body(env.admin_client(), env, S.aj)["execution_status"] == "COMPLETED"


# --------------------------------------------------------------------------- AI help for a reviewer, by the same standing permission
def test_a_reviewer_may_ask_the_ai_a_question_but_a_viewer_may_not(env, ch):
    rv, vw = member(env, "reviewer"), member(env, "viewer")
    body = {"question": "What is the manager threshold?", "question_id": "q-" + env.nonce, "artifact_ids": [sorted(ch.arts.values())[0]]}
    jid, r = create_job(rv, env, "gap.answer", body)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    assert job_body(env.admin_client(), env, jid)["approved_by"] == "policy:ai.use"
    assert create_job(vw, env, "gap.answer", {**body, "question_id": "q2-" + env.nonce})[1].status_code == 403
    sus = member(env, "requester", "ai_suspended")
    assert create_job(sus, env, "gap.answer", {**body, "question_id": "q3-" + env.nonce})[1].status_code == 403
