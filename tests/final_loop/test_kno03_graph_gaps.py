"""KNO-03: graph and gap queue through the real routes and store (throwaway PostgreSQL, no provider, no model call).

Flow: import -> source revision -> pending observed snapshot -> POST /v2/business/analyse -> GET /v2/business/gaps and /graph."""
from __future__ import annotations

import psycopg2
import pytest

from core.change_intelligence.v2 import business_graph as bg
from infra.adapters.v2_business import analyse_workspace, business_view, commit_snapshot, gap_queue, graph_view, BusinessKnowledgeRefused
from tests.final_loop.test_kno01_business_store import KNOWLEDGE, counts, observed, ws  # noqa: F401  (ws is a fixture)


def seed(env, head, rules=None, key="seed-1", snapshot_id="snap-seed", gaps=()):
    commit_snapshot(env.store, env.tenant, env.ws, snapshot_id=snapshot_id, actor_id="system", lifecycle_state="PENDING_HUMAN",
                    rules=rules if rules is not None else observed(head), gaps=gaps, expected_version=head.version, expected_business_revision=0,
                    source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id, decision_epoch=head.decision_epoch,
                    idempotency_key=key)
    return env.store.head(env.tenant, env.ws)


def test_gap_appears_in_the_real_queue_for_conflict_and_missing_actor(env, ws):
    seed(env, ws)
    admin = env.admin_client()
    r = admin.post("/v2/business/analyse", json={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    assert r.json()["replayed"] is False and r.json()["gaps"] >= 3
    q = admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()
    kinds = [(g["kind"], tuple(g["affected_rule_ids"])) for g in q["gaps"]]
    assert q["gaps"][0]["priority"] == "critical" and q["gaps"][0]["kind"] == "conflicting_facts"            # critical first
    assert ("conflicting_facts", ("rule-capex-finance-config", "rule-capex-finance-policy")) in kinds
    assert ("missing_actor", ("rule-emergency-routing",)) in kinds
    assert all(a["title"] for g in q["gaps"] for a in g["affected"])
    g = admin.get("/v2/business/graph", params={"workspace_id": env.ws, "focus": "role-finance"}).json()
    assert any(e["type"] == "conflicts_with" and e["status"] == "PROPOSED" for e in g["edges"]) and "rule-capex-finance-policy" in g["traversal"]["nodes"]
    assert {n["type"] for n in g["nodes"]} == {"rule", "concept", "role"}


def test_rerun_is_idempotent_and_never_duplicates_questions(env, ws):
    seed(env, ws)
    first = analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a1")
    before = counts(env)
    again = analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a2")
    assert again["replayed"] is True and again["snapshot_id"] == first["snapshot_id"] and counts(env) == before
    ids = [g["gap_id"] for g in gap_queue(env.store, env.tenant, env.ws)["gaps"]]
    assert len(ids) == len(set(ids))
    # the same answer to the same route twice
    admin = env.admin_client()
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).json()["replayed"] is True


def test_answered_gap_is_kept_and_not_asked_again(env, ws):
    rules = observed(ws)
    conflict = next(g for g in bg.derive_gaps(rules) if g["kind"] == "conflicting_facts")
    seed(env, ws, rules=rules, gaps=[dict(conflict, status="RESOLVED")])
    analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a1")
    queue = gap_queue(env.store, env.tenant, env.ws)
    assert conflict["gap_id"] not in {g["gap_id"] for g in queue["gaps"]}
    kept = next(g for g in business_view(env.store, env.tenant, env.ws)["pending"]["gaps"] if g["gap_id"] == conflict["gap_id"])
    assert kept["status"] == "RESOLVED"


def test_unrelated_same_value_rules_stay_separate_in_the_stored_graph(env, ws):
    rules = observed(ws)
    other = dict(rules[0], rule_id="rule-other-1m", concept_ids=["concept-freight"], actor_ref="role-logistics", title="Freight review from 1,000,000 JPY")
    seed(env, ws, rules=rules + [other])
    analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a1")
    edges = graph_view(env.store, env.tenant, env.ws)["edges"]
    assert not any(e["type"] == "conflicts_with" and "rule-other-1m" in (e["from"], e["to"]) for e in edges)
    assert all(e["status"] != "CONFIRMED" for e in edges)                                  # observed, undecided: proposals only


def test_analysis_refuses_without_a_snapshot_and_with_a_stale_base(env, ws):
    with pytest.raises(BusinessKnowledgeRefused) as exc:
        analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a0")
    assert exc.value.code == "no_snapshot"
    head = seed(env, ws)
    digest = env.store.stage_blob(b"newer source")
    env.store.publish_source(env.tenant, env.ws, "src-moved", {"artifact-new": digest}, expected_version=head.version,
                             operation="source.import.commit", idempotency_key="moved")
    # SYN-02 (contract section 13): a moved head no longer locks the analysis; the knowledge is first carried over to the current head as a PENDING_HUMAN
    # proposal (origin rebase) and analysed there. Stale observed rules never enter silently: the coverage says what was re-read and what was not.
    out = analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a1")
    assert out["derived"] and out["derived"].startswith("rebase-")
    from infra.adapters.v2_business import business_view
    pending = business_view(env.store, env.tenant, env.ws)["pending"]
    assert pending["snapshot"]["source_revision_id"] == "src-moved" and pending["snapshot"]["coverage"]["origin"] == "rebase"
    r = env.admin_client().post("/v2/business/analyse", json={"workspace_id": env.ws})
    assert r.status_code == 200 and r.json()["replayed"] is True


def test_permissions_and_tenant_scope(env, ws):
    seed(env, ws)
    req = env.req_client()
    r = req.post("/v2/business/analyse", json={"workspace_id": env.ws})
    assert r.status_code == 403                                                             # a requester has no knowledge.confirm
    assert req.get("/v2/business/gaps", params={"workspace_id": env.ws}).status_code == 200  # reads need project.read only
    assert counts(env)["v2_business_snapshots"] == 1                                         # the refused call wrote nothing
    env.store.create_workspace("t-other-" + env.nonce, f"p-{env.nonce}", env.ws)
    assert gap_queue(env.store, "t-other-" + env.nonce, env.ws) == {"snapshot_id": None, "gaps": []}
    assert graph_view(env.store, "t-other-" + env.nonce, env.ws)["edges"] == []
    anonymous = env.admin_client()
    anonymous.cookies.clear()
    assert anonymous.get("/v2/business/gaps", params={"workspace_id": env.ws}).status_code in (401, 403)
