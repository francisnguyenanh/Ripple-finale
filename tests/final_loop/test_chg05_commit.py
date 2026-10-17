"""CHG-05: committing an approved business change into the accepted knowledge, through the real routes, store, outbox and index worker. No model call of its own
(the understanding comes from the CHG-01 STUB). What this proves: one atomic accepted revision with TARGET rules, the observed rules untouched, replay/CAS/failure rollback,
the four completion axes, and that the next context pack (target view) carries the new meaning while the observed view keeps the old. Not human acceptance.
"""
from __future__ import annotations

import psycopg2
import pytest

from infra.adapters import v2_business_commit
from infra.adapters.v2_business import business_view
from infra.adapters.v2_postgres import PostgresV2Store
from tests.final_loop.test_chg01_change_request import NUM_REQ, OP_VALUE, model_obj
from tests.final_loop.test_chg02_impact import confirmed
from tests.final_loop.test_chg04_review import clarify, decide, prepared, review
from tests.final_loop.test_kno01_business_store import counts, workspace_row, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import drain

TARGET = "rule-standard-manager-review-target"


def commit(q, client, cid, binding):
    return client.post(f"/v2/business/changes/{cid}/commit", json={"workspace_id": q.env.ws, "binding": binding})


def approved(q):
    cid, approver = prepared(q)
    binding = review(q, approver, cid).json()["binding"]
    assert decide(q, approver, cid, binding).status_code == 200
    return cid, approver, binding


def rules_of(view):
    return {r["rule_id"]: r for r in view["accepted"]["rules"]}


def test_commit_needs_a_current_approval_then_writes_one_accepted_revision_with_target_rules_and_replays(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    binding = review(q, q.admin, cid).json()["binding"]
    r = commit(q, q.admin, cid, binding)
    assert r.status_code == 409 and r.json()["code"] == "not_approved"
    assert decide(q, approver, cid, binding).status_code == 200
    before = business_view(q.env.store, q.env.tenant, q.env.ws)
    r = commit(q, q.admin, cid, binding)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["business_revision"] == 2 and out["replayed"] is False and out["target_rule_ids"] == [TARGET] and out["completion"]["knowledge_result"] == "committed"
    assert out["completion"]["source_sync"] == "pending" and out["completion"]["code_sync"] == "not_required" and out["completion"]["behavioral_verification"] == "not_tested"
    fresh = PostgresV2Store(q.env.dsn, q.env.blobs)                                                    # a restarted process reads the same knowledge back
    view = business_view(fresh, q.env.tenant, q.env.ws)
    rules = rules_of(view)
    assert view["business_head"]["business_revision"] == 2 and view["accepted"]["snapshot"]["coverage"]["origin"] == "business_change"
    t = rules[TARGET]
    assert t["authority"] == "TARGET" and t["status"] == "CONFIRMED" and t["implementation_status"] == "pending" and t["condition"]["value"] == 1500000
    assert {"kind": "human_decision", "decision_id": out["decision_id"]} in t["provenance"] and t["source_bindings"] == rules["rule-standard-manager-review"]["source_bindings"]
    assert rules["rule-standard-manager-review"] == rules_of(before)["rule-standard-manager-review"]                                              # the observed rule is what the sources say
    assert any(d["kind"] == "business_change" and d["body"]["intent_id"] == cid for d in view["accepted"]["decisions"])
    rv = review(q, q.admin, cid, "technical").json()
    assert rv["commit"]["business_revision"] == 2 and rv["completion"]["knowledge_result"] == "committed" and rv["can"]["commit_knowledge"] is False and "already committed" in rv["can"]["reasons"]["commit_knowledge"]
    assert rv["commit"]["pending_implementation"]["counts"]["AI_EDIT_SUPPORTED"] == 1 and rv["commit"]["pending_implementation"]["items"]
    assert review(q, q.req, cid).json()["commit"]["pending_implementation"].get("items") is None                                                 # file details need evidence rights
    row = workspace_row(q.env)
    again = commit(q, q.admin, cid, binding)
    assert again.status_code == 200 and again.json()["replayed"] is True and again.json()["business_revision"] == 2 and workspace_row(q.env) == row  # no second version
    st = q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()
    assert st["documents"] == {"freshness": "not_requested", "jobs": 0} and st["index"]["pending_events"] >= 1
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND kind LIKE 'document%%'", (q.env.tenant,))
        assert cur.fetchone()[0] == 0
    drain(q.env)
    assert q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["index"]["pending_events"] == 0


def test_the_next_context_pack_carries_the_new_meaning_in_the_target_view_and_the_old_in_the_observed_view(qa_env):
    q = qa_env
    cid, approver, binding = approved(q)
    assert commit(q, q.admin, cid, binding).status_code == 200
    drain(q.env)
    def pack(view):
        r = q.admin.post("/v2/business/context", json={"workspace_id": q.env.ws, "query": "standard manager approval threshold purchases", "view": view, "semantic": False})
        assert r.status_code == 200, r.text
        return r.json()
    target = pack("target")
    texts = {i["item_id"]: i["text"] for i in target["items"]}
    assert TARGET in texts and "1500000" in texts[TARGET] and target["pin"]["business_revision"] == 2
    observed = pack("observed")
    assert all("1500000" not in i["text"] for i in observed["items"]) and any("1000000" in i["text"] for i in observed["items"])


def test_a_failure_inside_the_commit_changes_nothing_and_the_same_request_works_afterwards(qa_env, monkeypatch):
    q = qa_env
    cid, approver, binding = approved(q)
    before = (counts(q.env), workspace_row(q.env), business_view(q.env.store, q.env.tenant, q.env.ws)["business_head"])

    def boom(*a, **k):
        raise RuntimeError("injected commit/outbox failure")
    with monkeypatch.context() as m:
        m.setattr(PostgresV2Store, "_record_commit", boom)
        with pytest.raises(RuntimeError):
            v2_business_commit.commit_change(q.env.store, q.env.tenant, q.env.ws, cid, actor_id=q.env.admin, roles=("admin",), seen=binding)
    assert (counts(q.env), workspace_row(q.env), business_view(q.env.store, q.env.tenant, q.env.ws)["business_head"]) == before
    assert commit(q, q.admin, cid, binding).status_code == 200


def test_wrong_role_tampered_binding_stale_approval_and_moved_knowledge_refuse_with_zero_writes(qa_env):
    q = qa_env
    cid, approver, binding = approved(q)
    before = (counts(q.env), workspace_row(q.env))
    assert commit(q, q.req, cid, binding).status_code == 403 and commit(q, approver, cid, binding).status_code == 403                             # requester, approver: no knowledge.confirm
    assert commit(q, q.admin, cid, dict(binding, semantic_diff_hash="0" * 64)).json()["code"] == "diff_mismatch"
    assert commit(q, q.admin, cid, dict(binding, draft_hash="0" * 64)).json()["code"] == "draft_revised"
    assert commit(q, q.admin, cid, {}).status_code == 409 and commit(q, q.admin, cid, "x").status_code == 400
    first = review(q, q.admin, cid, "technical").json()
    ref = next(f["ref"] for f in first["draft"]["files"] if f["action"] == "UNRESOLVED")
    assert clarify(q, q.req, cid, binding, ref, "manual_later").status_code == 200                                                                # revised: the approval is stale
    stale = review(q, q.admin, cid).json()
    assert stale["can"]["commit_knowledge"] is False and "approval" in stale["can"]["reasons"]["commit_knowledge"]
    assert commit(q, q.admin, cid, stale["binding"]).json()["code"] == "not_approved"
    assert decide(q, approver, cid, stale["binding"], reason="Approved again.").status_code == 200
    with q.env.store._tx() as cur:
        cur.execute("SELECT decision_id FROM v2_business_decisions WHERE tenant_id=%s AND kind='answer_gap'", (q.env.tenant,))
        decision = cur.fetchone()[0]
    assert q.admin.post("/v2/business/decisions/revoke", json={"workspace_id": q.env.ws, "decision_id": decision, "reason": "changed"}).status_code == 200
    moved = review(q, q.admin, cid).json()
    r = commit(q, q.admin, cid, moved["binding"])
    assert r.status_code == 409 and "stale_snapshot" in r.text
    assert counts(q.env)["v2_business_snapshots"] == before[0]["v2_business_snapshots"] + 1                                                       # only the revoke wrote a snapshot
    assert v2_business_commit.committed_state(q.env.store, q.env.tenant, q.env.ws, cid) is None


def test_a_second_change_on_the_same_rule_asks_which_version_and_the_decided_version_is_then_updated_in_place(qa_env):
    """Review F-07 (a): after a first change decided the rule, a second request on the OBSERVED rule is not confirmable (the understanding asks which version); naming the
    decided rule runs the whole chain again and updates the target in place (no second target, no 409 at commit)."""
    q = qa_env
    cid, approver, binding = approved(q)
    assert commit(q, q.admin, cid, binding).status_code == 200
    from tests.final_loop.test_chg01_change_request import understand
    text = "Raise the standard manager approval threshold from 1,500,000 to 1,700,000 JPY."
    asked, _ = understand(q, text, model_obj(ops=[dict(OP_VALUE, desired={"value": 1700000, "unit": "JPY"}, user_quote=text.rstrip("."))]), mode="technical")
    u = asked["understanding"]
    assert u["status"] == "CLARIFY" and u["can_confirm"] is False and any("decided version" in qq for qq in u["questions"])
    assert u["technical"]["rejected"][0]["reason"] == "observed_rule_has_decided_version"
    text2 = "Raise the decided standard manager approval threshold from 1,500,000 to 1,700,000 JPY."
    cid2, _ = confirmed(q, text2, [dict(OP_VALUE, rule_id=TARGET, desired={"value": 1700000, "unit": "JPY"}, user_quote=text2.rstrip("."))])
    b2 = review(q, approver, cid2).json()["binding"]
    assert decide(q, approver, cid2, b2, reason="Second raise, on the decided rule.").json()["code"] == "open_blockers"            # changing a decided rule is HIGH: acknowledge it
    assert decide(q, approver, cid2, b2, reason="Second raise, on the decided rule.", acknowledged_flags=True).status_code == 200
    r = commit(q, q.admin, cid2, b2)
    assert r.status_code == 200, r.text
    assert r.json()["business_revision"] == 3 and r.json()["target_rule_ids"] == [TARGET]
    rules = rules_of(business_view(q.env.store, q.env.tenant, q.env.ws))
    assert rules[TARGET]["condition"]["value"] == 1700000 and rules["rule-standard-manager-review"]["condition"]["value"] == 1000000
    assert sum(1 for rid in rules if rid.startswith("rule-standard-manager-review")) == 2                 # observed + one decided version, updated in place
