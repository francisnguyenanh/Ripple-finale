"""CHG-04: review and decisions on a confirmed business change, through the real routes, store and policy.

No model call is made by this task: the understanding and the optional guidance come from the CHG-01/CHG-03 STUB flows (synthetic answers). What this proves: the decision
ledger and its guards (binding to the draft seen, separate knowledge / file-write decisions, clarifications as revisions, distinct-approver policy, HIGH flags never
self-approvable, zero writes on refusal, Business view without ids). It does not prove that a person's decision is right.
"""
from __future__ import annotations

import json

import psycopg2
import pytest
from fastapi.testclient import TestClient

from infra import judge
from infra.adapters import v2_business_review
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import StaleAuthorityError
from tests.final_loop.test_chg01_change_request import ACTOR_REQ, NUM_REQ, OP_ACTOR, OP_VALUE, model_obj, understand
from tests.final_loop.test_chg02_impact import confirmed
from tests.final_loop.test_chg03_draft import ask_guidance, deps_first, gitem, gobj, settle_guidance
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401  (qa_env is a fixture)


def login(q, account, roles, cred="cv"):
    ids = PostgresIdentityStore(q.env.dsn)
    ids.create_account(account, cred)
    ids.grant_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, account, tuple(roles))
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": account, "credential": cred}).status_code == 200
    return c


def review(q, client, cid, mode="business"):
    return client.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws, "mode": mode})


def decide(q, client, cid, binding, action="approve", kind="knowledge", reason="Checked against the policy; the new threshold is right.", **extra):
    return client.post(f"/v2/business/changes/{cid}/review/decide", json={"workspace_id": q.env.ws, "kind": kind, "action": action, "reason": reason, "binding": binding, **extra})


def clarify(q, client, cid, binding, ref, disposition, answer=None, reason="Clarified."):
    return client.post(f"/v2/business/changes/{cid}/review/clarify", json={"workspace_id": q.env.ws, "ref": ref, "disposition": disposition, "answer": answer, "reason": reason, "binding": binding})


def ledger(q):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_business_change_decisions WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))
        n = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='BUSINESS_CHANGE_DECISION_RECORDED'", (q.env.tenant, q.env.ws))
        return n, cur.fetchone()[0]


def relax_policy(q, value=False):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("UPDATE v2_workspaces SET require_distinct_approver=%s WHERE tenant_id=%s AND workspace_id=%s", (value, q.env.tenant, q.env.ws))


def prepared(q, text=NUM_REQ, ops=(OP_VALUE,), client=None):
    cid, _ = confirmed(q, text, list(ops), client=client)
    approver = login(q, f"approver-{q.env.nonce}", ("change_approver", "reviewer"))        # an approver who may also read evidence (paths, instructions, questions)
    return cid, approver


def test_the_review_shows_meaning_impact_pending_implementation_and_what_this_person_may_do(qa_env):
    q = qa_env
    got, _ = understand(q, NUM_REQ, model_obj(ops=[OP_VALUE]))
    assert review(q, q.req, got["change_id"]).status_code == 409 and review(q, q.req, got["change_id"]).json()["code"] == "not_confirmed"
    cid, approver = prepared(q)
    r = review(q, q.req, cid)
    assert r.status_code == 200, r.text
    body = r.json()
    rc = body["draft"]["rule_changes"][0]
    assert body["changes"] == ["Purchases of 1,000,000 JPY or more need manager approval"] and rc["before"] != rc["after"] and rc["changes"][0].startswith("when it applies:")
    assert body["impact"]["affected_rules"][0]["link"] == "direct" and body["draft"]["file_action_counts"]["AI_EDIT_SUPPORTED"] == 1 and body["draft"]["file_action_counts"]["UNRESOLVED"] >= 1
    assert body["can"]["approve_knowledge"] is False and "cannot decide" in body["can"]["reasons"]["approve_knowledge"] and body["can"]["clarify"] is True
    assert body["binding"]["semantic_diff_hash"] and body["binding"]["draft_hash"] and body["binding"]["edits_hash"] and body["decisions"] == [] and body["knowledge_moved"] is False
    blob = json.dumps({k: v for k, v in body.items() if k != "binding"})
    assert "rule-" not in blob and ".yaml" not in blob and "routing_rules" not in blob and "technical" not in body["draft"]                   # Business mode: no ids, paths or locators
    a = review(q, approver, cid, "technical").json()
    assert a["can"]["approve_knowledge"] is True and a["can"]["approve_file_writes"] is True and a["can"]["clarify"] is False and a["can"]["blockers"] == []
    assert a["draft"]["technical"]["semantic_diff_hash"] == body["binding"]["semantic_diff_hash"] and "decisions_are_separate" in a
    assert review(q, q.req, "chg-nonexistent").status_code == 404 and review(q, q.req, cid, "x").status_code == 400
    assert login(q, f"viewer-r-{q.env.nonce}", ("viewer",)).get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws}).status_code == 404   # a stranger sees nothing


def test_a_knowledge_decision_binds_to_the_draft_seen_and_is_separate_from_the_file_write_decision(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    before_counts, head0 = counts(q.env), q.env.store.head(q.env.tenant, q.env.ws)
    before_knowledge = q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["accepted"]
    binding = review(q, approver, cid).json()["binding"]
    r = decide(q, approver, cid, binding)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "knowledge" and r.json()["self_approval"] is False and r.json()["draft_hash"] == binding["draft_hash"]
    after = review(q, approver, cid).json()
    assert after["can"]["knowledge_decision"]["action"] == "approve" and after["can"]["file_writes_decision"] is None                      # accepting the meaning approves no file write
    assert after["decisions"][0]["current"] is True and after["can"]["approve_knowledge"] is False and "already decided" in after["can"]["reasons"]["approve_knowledge"]
    again = decide(q, approver, cid, binding)
    assert again.status_code == 409 and again.json()["code"] == "already_decided"
    fw = decide(q, approver, cid, binding, kind="file_writes", reason="The YAML edit is the right one.")
    assert fw.status_code == 200 and fw.json()["kind"] == "file_writes"
    final = review(q, approver, cid, "technical").json()
    assert final["can"]["file_writes_decision"]["action"] == "approve" and final["decisions"][1]["binding"]["edits_hash"] == binding["edits_hash"] and final["decisions"][1]["binding"]["edit_refs"] == ["F001"]
    assert counts(q.env) == before_counts and q.env.store.head(q.env.tenant, q.env.ws) == head0 and ledger(q) == (2, 2)                   # nothing committed, no version or epoch moved
    assert q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["accepted"] == before_knowledge                    # the accepted knowledge is untouched


def test_tampered_outdated_or_moved_bindings_refuse_with_zero_writes(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    binding = review(q, approver, cid).json()["binding"]
    assert decide(q, approver, cid, dict(binding, semantic_diff_hash="0" * 64)).json()["code"] == "diff_mismatch"
    assert decide(q, approver, cid, dict(binding, draft_hash="0" * 64)).json()["code"] == "draft_revised"
    assert decide(q, approver, cid, dict(binding, edits_hash="0" * 64), kind="file_writes").json()["code"] == "edits_changed"
    assert decide(q, approver, cid, {}).status_code == 409 and decide(q, approver, cid, "x").status_code == 400
    assert decide(q, approver, cid, binding, reason="").status_code == 400 and decide(q, approver, cid, binding, reason="<b>x</b>").status_code == 400
    assert decide(q, approver, cid, binding, action="explode").status_code == 400 and decide(q, approver, cid, binding, kind="result").status_code == 400
    with q.env.store._tx() as cur:                                                                     # the knowledge moves after confirmation
        cur.execute("SELECT decision_id FROM v2_business_decisions WHERE tenant_id=%s AND kind='answer_gap'", (q.env.tenant,))
        decision = cur.fetchone()[0]
    assert q.admin.post("/v2/business/decisions/revoke", json={"workspace_id": q.env.ws, "decision_id": decision, "reason": "changed"}).status_code == 200
    moved = review(q, approver, cid).json()
    assert moved["knowledge_moved"] is True and moved["can"]["approve_knowledge"] is False and "moved" in moved["can"]["reasons"]["approve_knowledge"]
    r = decide(q, approver, cid, moved["binding"])
    assert r.status_code == 409 and "stale_snapshot" in r.text
    assert ledger(q) == (0, 0)


def test_a_clarification_revises_the_draft_labels_the_item_and_makes_the_earlier_approval_stale(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    first = review(q, approver, cid, "technical").json()
    assert decide(q, approver, cid, first["binding"]).status_code == 200
    ref = next(f["ref"] for f in first["draft"]["files"] if f["action"] == "UNRESOLVED")
    assert clarify(q, approver, cid, first["binding"], ref, "manual_later").status_code == 403                                             # an approver without knowledge.confirm cannot clarify
    r = clarify(q, q.req, cid, first["binding"], ref, "manual_later", reason="We will update the policy text after the change.")
    assert r.status_code == 200, r.text
    assert clarify(q, q.req, cid, first["binding"], "F999", "manual_later").status_code == 409                                             # a revised draft: the old binding is refused
    second = review(q, approver, cid, "technical").json()
    assert second["binding"]["draft_hash"] != first["binding"]["draft_hash"] and second["binding"]["semantic_diff_hash"] == first["binding"]["semantic_diff_hash"]
    item = next(f for f in second["draft"]["files"] if f["ref"] == ref)
    assert item["action"] == "UNRESOLVED" and item["clarification"]["disposition"] == "manual_later" and item["clarification"]["by"] == q.env.req     # labelled, not resolved
    k = next(d for d in second["decisions"] if d["kind"] == "knowledge")
    assert k["current"] is False and "revised" in k["stale_reason"] and second["can"]["knowledge_decision"] is None and second["can"]["approve_knowledge"] is True
    assert clarify(q, q.req, cid, second["binding"], "F999", "manual_later").status_code == 409                                            # not an open item
    assert clarify(q, q.req, cid, second["binding"], ref, "answered").status_code == 400                                                   # this disposition needs an answer
    assert decide(q, approver, cid, second["binding"], reason="Approved again for the revised draft.").status_code == 200
    third = review(q, approver, cid).json()
    assert third["can"]["knowledge_decision"]["action"] == "approve" and sum(1 for d in third["decisions"] if d["kind"] == "knowledge") == 2 and third["decisions"][1]["kind"] == "clarification"


def test_an_open_question_from_the_guidance_blocks_the_knowledge_decision_until_a_person_answers_it(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    deps_first(q, q.admin, cid)                                                                                                              # guidance needs the dependency step first
    use(q, lambda p: gobj(items=[gitem("F001", instruction="Change the key and run the tests.", hold=True)],                                  # the model holds F001 ...
                          questions=[{"question": "Does the new threshold include tax?", "needed": True, "ref": "F001"},                       # ... and needs this answered: blocks
                                     {"question": "Have you verified this with the latest financial control policy?", "needed": False, "ref": None},   # advice: shown, never a blocker
                                     "Is the change effective from the next quarter?"]))                                                    # a bare string: advice
    assert ask_guidance(q, q.admin, cid).status_code == 200
    settle_guidance(q, q.admin, cid)
    r = review(q, approver, cid).json()
    assert [(b["code"], b["about"]) for b in r["can"]["blockers"]] == [("open_question", "F001")] and r["can"]["approve_knowledge"] is False and r["draft"]["questions"][0]["ref"] == "Q001"
    assert len(r["draft"]["questions"]) == 1 and "include tax" in r["draft"]["questions"][0]["question"]                                       # one blocker only
    advice = r["draft"]["guidance"]["advice_for_human"]
    assert [x["text"][:24] for x in advice] == ["Have you verified this w", "Is the change effective "] and advice[0]["why_not_blocking"] == "not needed to define the change"
    refused = decide(q, approver, cid, r["binding"])
    assert refused.status_code == 409 and refused.json()["code"] == "open_blockers" and "include tax" in refused.json()["error"]
    assert clarify(q, q.req, cid, r["binding"], "Q001", "answered", answer="Yes, the threshold is the net amount including tax.").status_code == 200
    r2 = review(q, approver, cid).json()
    assert r2["can"]["blockers"] == [] and r2["draft"]["questions"][0]["clarification"]["disposition"] == "answered"
    assert decide(q, approver, cid, r2["binding"]).status_code == 200


def test_a_high_risk_change_needs_an_acknowledged_flag_and_is_never_self_approved(qa_env):
    q = qa_env
    cid, approver = prepared(q, ACTOR_REQ, (OP_ACTOR,))
    r = review(q, approver, cid).json()
    assert r["can"]["flags_to_acknowledge"] and "Changes who decides" in r["can"]["flags_to_acknowledge"][0] and any(b["code"] == "flag_not_acknowledged" for b in r["can"]["blockers"])
    assert decide(q, approver, cid, r["binding"]).json()["code"] == "open_blockers"
    ok = decide(q, approver, cid, r["binding"], acknowledged_flags=True)
    assert ok.status_code == 200 and review(q, approver, cid, "technical").json()["decisions"][0]["binding"]["acknowledged_flags"] is True
    both = login(q, f"both-{q.env.nonce}", ("requester", "change_approver"))
    own, _ = confirmed(q, "Raise the standard manager approval threshold from 1,000,000 to 1,700,000 JPY.",
                       [dict(OP_VALUE, desired={"value": 1700000, "unit": "JPY"}, user_quote="Raise the standard manager approval threshold from 1,000,000 to 1,700,000 JPY")], client=both)
    b = review(q, both, own).json()
    assert b["can"]["approve_knowledge"] is False and "different person" in b["can"]["reasons"]["approve_knowledge"]
    assert decide(q, both, own, b["binding"]).status_code == 403                                                                           # strict policy: no self-approval
    relax_policy(q)
    b = review(q, both, own).json()
    assert b["can"]["approve_knowledge"] is True
    r2 = decide(q, both, own, b["binding"], reason="Demo workspace: approving my own request.")
    assert r2.status_code == 200 and r2.json()["self_approval"] is True and review(q, both, own).json()["decisions"][0]["self_approval"] is True
    own_actor, _ = confirmed(q, "Purchases from high-risk suppliers should be approved by the Finance Controller instead of the Operations Director!", [OP_ACTOR], client=both)
    hb = review(q, both, own_actor).json()
    assert hb["can"]["approve_knowledge"] is False and "high-risk" in hb["can"]["reasons"]["approve_knowledge"]
    assert decide(q, both, own_actor, hb["binding"], acknowledged_flags=True).status_code == 403                                           # relaxed policy never covers a high-risk change
    relax_policy(q, True)


def test_roles_without_change_approve_cannot_decide_and_a_revoked_actor_is_caught_inside_the_transaction(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    binding = review(q, approver, cid).json()["binding"]
    assert decide(q, q.req, cid, binding).status_code == 403                                                                               # the requester holds no change.approve
    owner = login(q, f"owner-{q.env.nonce}", ("knowledge_owner",))
    assert decide(q, owner, cid, binding).status_code == 403 and review(q, owner, cid).status_code == 200                                 # a confirmer may read and clarify, not decide
    assert clarify(q, owner, cid, binding, "F002", "outside_this_request", reason="The policy text is maintained by Legal, not by this change.").status_code == 200
    anonymous = q.env.admin_client()
    anonymous.cookies.clear()
    assert anonymous.post(f"/v2/business/changes/{cid}/review/decide", json={"workspace_id": q.env.ws, "action": "approve", "reason": "x", "binding": binding}).status_code in (401, 403)
    binding = review(q, approver, cid).json()["binding"]
    PostgresIdentityStore(q.env.dsn).revoke_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, f"approver-{q.env.nonce}")
    with pytest.raises(StaleAuthorityError):                                                                                               # roles from an older session, membership gone now
        v2_business_review.record_decision(q.env.store, q.env.tenant, q.env.ws, cid, actor_id=f"approver-{q.env.nonce}", roles=("change_approver",), kind="knowledge", action="approve",
                                           reason="late", seen=binding)
    assert ledger(q) == (1, 1)


def test_idempotent_replay_and_request_changes(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    binding = review(q, approver, cid).json()["binding"]
    a = decide(q, approver, cid, binding, action="request_changes", reason="Please state whether framework contracts are included.", idempotency_key="k1")
    b = decide(q, approver, cid, binding, action="request_changes", reason="Please state whether framework contracts are included.", idempotency_key="k1")
    assert a.status_code == 200 and b.status_code == 200 and b.json()["replayed"] is True and a.json()["decision_id"] == b.json()["decision_id"] and ledger(q)[0] == 1
    c = decide(q, approver, cid, binding, action="reject", reason="Different decision, same key.", idempotency_key="k1")
    assert c.status_code == 409 and c.json()["code"] == "idempotency_conflict"
    r = review(q, approver, cid).json()
    assert r["can"]["knowledge_decision"]["action"] == "request_changes" and r["can"]["approve_knowledge"] is True                        # a request for changes is not terminal
    assert decide(q, approver, cid, binding, action="reject", reason="Not this quarter.").status_code == 200
    assert decide(q, approver, cid, binding).json()["code"] == "already_decided"
