"""REL-01 (R05, R07, R11): one end-to-end run of a NONNUMERIC business change through the real routes, store and worker with the STUB understanding and the STUB
answer model: actor change -> confirm -> approve -> commit -> the next question is answered from the new (decided) knowledge while the sources still say the old
actor -> a second request on the same rule asks which version -> no document job was created by any of it. What this does NOT prove: that a real model
understands or answers well (every model output here is a synthetic stub authored from the fixtures). Simulated roles; not human acceptance."""
from __future__ import annotations

import json

import psycopg2

from infra.adapters.v2_business import business_view
from tests.final_loop.test_chg01_change_request import ACTOR_REQ, OP_ACTOR, model_obj, understand
from tests.final_loop.test_chg04_review import decide, prepared, review
from tests.final_loop.test_chg05_commit import commit
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import claim, obj, qa_env, run, use  # noqa: F401  (qa_env is a fixture)
from tests.final_loop.test_syn04_journey import journey, states
from tests.pitch_brief.conftest import drain

RULE = "rule-high-risk-director"
TARGET = RULE + "-target"


def test_an_actor_change_is_decided_committed_answered_from_and_the_second_request_asks_which_version(qa_env):
    q = qa_env
    before = business_view(q.env.store, q.env.tenant, q.env.ws)
    observed_actor = {r["rule_id"]: r for r in before["accepted"]["rules"]}[RULE]["actor_ref"]
    cid, approver = prepared(q, ACTOR_REQ, (OP_ACTOR,))                                                     # R05: a nonnumeric request kept with its meaning
    rv = review(q, approver, cid).json()
    assert rv["draft"]["rule_changes"][0]["valid"] is True and "finance controller" in json.dumps(rv["draft"]["rule_changes"][0]).lower()
    assert rv["draft"]["file_action_counts"]["AI_EDIT_SUPPORTED"] == 0                                        # an actor named in prose: nothing is edited automatically
    assert decide(q, approver, cid, rv["binding"], reason="The Finance Controller owns high-risk suppliers now.").json()["code"] == "open_blockers"      # who decides changes: HIGH flag
    assert decide(q, approver, cid, rv["binding"], reason="The Finance Controller owns high-risk suppliers now.", acknowledged_flags=True).status_code == 200
    r = commit(q, q.admin, cid, rv["binding"])
    assert r.status_code == 200 and r.json()["target_rule_ids"] == [TARGET] and r.json()["completion"]["source_sync"] == "pending", r.text   # R07: atomic accepted snapshot
    drain(q.env)
    view = business_view(q.env.store, q.env.tenant, q.env.ws)
    rules = {r["rule_id"]: r for r in view["accepted"]["rules"]}
    assert view["business_head"]["business_revision"] == 2 and rules[TARGET]["authority"] == "TARGET" and rules[TARGET]["actor_ref"] != observed_actor
    assert rules[RULE]["actor_ref"] == observed_actor and rules[TARGET]["implementation_status"] == "pending"   # the sources still say the old actor; nothing hides it
    model = use(q, lambda p: obj("ANSWERED", "The Finance Controller approves purchases from high-risk suppliers.",
                                 [claim("High-risk supplier purchases are approved by the Finance Controller.", [TARGET], "target")]))
    got = run(q, q.admin, "Who approves a purchase from a high-risk supplier?")
    a = got["answer"]
    assert got["status"] == "done" and a["status"] in ("ANSWERED", "PARTIAL") and model.calls == 1
    assert a["claims"][0]["authority_labels"] == ["decided by a person"] and a["implementation_note"] and TARGET in a["implementation_note"]   # R07: answered from the new knowledge, divergence disclosed
    tech = q.admin.get(f"/v2/business/questions/{got['question_id']}", params={"workspace_id": q.env.ws, "mode": "technical"}).json()["answer"]
    assert tech["technical"]["pin"]["business_revision"] == 2
    j = journey(q, q.admin, cid)
    assert j["knowledge_only_complete"] is True and j["fully_implemented"] is False and states(j)["source_sync"] == "current" and states(j)["code_sync"] in ("waiting", "not_required")
    asked, _ = understand(q, "High-risk supplier purchases should be approved by the CFO instead.", model_obj(ops=[dict(OP_ACTOR, desired="CFO", user_quote="High-risk supplier purchases should be approved by the CFO instead")]), mode="technical")
    u = asked["understanding"]
    assert u["status"] == "CLARIFY" and u["can_confirm"] is False and any("decided version" in x for x in u["questions"])     # R07: the second request names the version first
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind LIKE 'document%%'", (q.env.tenant, q.env.ws))
        assert cur.fetchone()[0] == 0                                                                        # R11: no document work from any of this
    assert q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()["documents"]["freshness"] == "not_requested"
