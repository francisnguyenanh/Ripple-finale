"""KNO-03: graph and gap queue through the real routes and store (throwaway PostgreSQL, no provider, no model call).

Flow: import -> source revision -> pending observed snapshot -> POST /v2/business/analyse -> GET /v2/business/gaps and /graph."""
from __future__ import annotations

import json

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


def test_numbers_in_the_structured_sources_that_no_rule_uses_are_disclosed_by_the_analysis(env, ws):
    """Extraction omission the server can see (2026-10-18): the Harbor config holds matching values (tolerance, payment term) that no rule of the fixture knowledge binds;
    the analysis lists them for a person (never a gap, never a rule); the values the rules use are not listed."""
    seed(env, ws)
    admin = env.admin_client()
    r = admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).json()
    refs = {v["evidence_ref"].rsplit("/", 1)[-1] for v in r["unbound_values"]["values"]}
    assert {"approval.yaml#matching_rules.price_tolerance_percent", "approval.yaml#matching_rules.payment_term_days"} <= refs
    assert "approval.yaml#routing_rules.standard_manager_threshold_jpy" not in refs and "approval.yaml#routing_rules.capex_finance_threshold_jpy" not in refs
    assert not any(ref.endswith("#version") for ref in refs)                                         # "1.0.0" is not a number
    again = admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).json()
    assert again["replayed"] is True and again["unbound_values"] == r["unbound_values"]


def test_unbound_values_is_pure_and_matches_bindings_by_file_name_and_label():
    rules = [{"source_bindings": [{"evidence_ref": "approval.yaml#routing_rules.limit"}], "provenance": [{"kind": "source_evidence", "evidence_ref": "rules.xlsx#Rates!B2"}]}]
    obs = [{"artifact_id": "a1", "locator": {"key_path": "routing_rules.limit"}, "raw_value": 100},
           {"artifact_id": "a1", "locator": {"key_path": "routing_rules.other"}, "raw_value": "2,500"},
           {"artifact_id": "a1", "locator": {"key_path": "flag"}, "raw_value": True},
           {"artifact_id": "a1", "locator": {"key_path": "name"}, "raw_value": "harbor"},
           {"artifact_id": "a2", "locator": {"sheet": "Rates", "cell": "B2"}, "raw_value": 3},
           {"artifact_id": "a2", "locator": {"sheet": "Rates", "cell": "B3"}, "raw_value": 4.5},
           {"artifact_id": "a2", "locator": {"sheet": "Rates", "cell": "B1"}, "raw_value": "Threshold_JPY"},
           {"artifact_id": "a2", "locator": {"sheet": "Rates", "cell": "A3"}, "raw_value": "MANAGER"}]
    out = bg.unbound_values(rules, obs, {"a1": "config/approval.yaml", "a2": "data/rules.xlsx"})
    assert out == {"count": 2, "truncated": False, "values": [
        {"evidence_ref": "config/approval.yaml#routing_rules.other", "value": "2,500", "file": "config/approval.yaml", "location": "routing_rules.other"},
        {"evidence_ref": "data/rules.xlsx#Rates!B3", "value": 4.5, "file": "data/rules.xlsx", "location": "Rates!B3", "sheet": "Rates", "cell": "B3",
         "header": "Threshold_JPY", "row_label": "MANAGER"}]}


# ---- Harbor workbook B2/B3 (owner instruction 2026-10-18): a candidate source conflict goes to conflict review only when concept and scope match -----------------
def _workbook_manager_rule(head, scope):
    """The observed rule an extraction of `approval_matrix.xlsx` RoutingRules row 3 would produce (MANAGER, 500,000, 'Standard / General limit'), bound to that cell.
    Seeded here as an extraction result on the throwaway DB only; no rule is created in any real workspace."""
    import copy
    base = copy.deepcopy(next(r for r in KNOWLEDGE["observed_rules"] if r["rule_id"] == "rule-standard-manager-review"))
    base.update(rule_id="rule-matrix-manager-limit", title="Approval matrix: manager limit 500,000 JPY (RoutingRules!B3, 'Operational guidance limit')",
                condition={"field": "request.net_amount_jpy", "op": "gte", "value": 500000, "unit": "JPY"}, scope=scope,
                provenance=[{"kind": "source_evidence", "artifact_id": "data-approval-matrix", "revision_id": head.source_revision_id, "evidence_ref": "approval_matrix.xlsx#RoutingRules!B3"}],
                source_bindings=[{"artifact_id": "data-approval-matrix", "revision_id": head.source_revision_id, "evidence_ref": "approval_matrix.xlsx#RoutingRules!B3"}],
                implementation_status="unverifiable")
    return base


def test_a_workbook_limit_with_the_same_concept_and_scope_enters_conflict_review_with_both_evidence_sets_and_no_winner(env, ws):
    head = env.store.head(env.tenant, env.ws)
    seed(env, head, rules=observed(head) + [_workbook_manager_rule(head, {"process": ["STANDARD"]})])
    admin = env.admin_client()
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).status_code == 200
    gaps = [g for g in admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"]
            if g["kind"] == "conflicting_facts" and set(g["affected_rule_ids"]) == {"rule-matrix-manager-limit", "rule-standard-manager-review"}]
    assert len(gaps) == 1 and gaps[0]["status"] == "OPEN"                                                     # a person decides; nothing chose a winner
    graph = admin.get("/v2/business/graph", params={"workspace_id": env.ws, "focus": "rule-matrix-manager-limit"}).json()
    assert any(e["type"] == "conflicts_with" and e["status"] == "PROPOSED" for e in graph["edges"])
    view = business_view(env.store, env.tenant, env.ws)
    rules = {r["rule_id"]: r for r in view["pending"]["rules"]}                                                # both evidence sets are kept on their rules
    assert [b["evidence_ref"] for b in rules["rule-matrix-manager-limit"]["source_bindings"]] == ["approval_matrix.xlsx#RoutingRules!B3"]
    assert {b["evidence_ref"] for b in rules["rule-standard-manager-review"]["source_bindings"]} >= {"approval.yaml#routing_rules.standard_manager_threshold_jpy", "procurement-policy.md#2.1"}


def test_a_workbook_limit_recorded_with_a_different_scope_is_a_scope_distinction_not_a_conflict(env, ws):
    head = env.store.head(env.tenant, env.ws)
    seed(env, head, rules=observed(head) + [_workbook_manager_rule(head, {"process": ["GUIDANCE"]})])
    admin = env.admin_client()
    assert admin.post("/v2/business/analyse", json={"workspace_id": env.ws}).status_code == 200
    assert not [g for g in admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"]
                if g["kind"] == "conflicting_facts" and "rule-matrix-manager-limit" in g["affected_rule_ids"]]


def test_the_unlinked_values_list_shows_values_only_to_readers_of_the_source_and_never_outside_the_workspace(env, ws):
    """Owner decision (2026-10-18), minimal list: grouped by file, sheet/cell/header/row label where they apply, labelled 'not linked to a rule'; values and
    locations only with evidence.read (they are source text); others see the file and the count; another workspace sees nothing; no gap or rule is created."""
    head = env.store.head(env.tenant, env.ws)
    seed(env, head)
    admin = env.admin_client()
    before = len(admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"])
    full = admin.get("/v2/business/unbound-values", params={"workspace_id": env.ws}).json()
    matrix = next(f for f in full["files"] if f["file"].endswith("approval_matrix.xlsx"))
    b3 = next(v for v in matrix["values"] if v["location"] == "RoutingRules!B3")
    assert full["values_withheld"] is False and (b3["value"], b3["header"], b3["row_label"]) == (500000, "Threshold_JPY", "MANAGER")
    assert "Not linked to any rule" in full["statement"] and "error" not in full["statement"].lower()
    req = env.req_client()                                                                                    # requester: no evidence.read
    low = req.get("/v2/business/unbound-values", params={"workspace_id": env.ws}).json()
    assert low["values_withheld"] is True and all(f["values"] == [] and f["count"] > 0 for f in low["files"])
    assert "500000" not in json.dumps(low) and "RoutingRules" not in json.dumps(low) and low["total"] == full["total"]
    other = f"other-{env.nonce}"
    env.store.create_workspace(env.tenant, f"p-{env.nonce}", other)
    assert req.get("/v2/business/unbound-values", params={"workspace_id": other}).status_code in (403, 404)
    assert len(admin.get("/v2/business/gaps", params={"workspace_id": env.ws}).json()["gaps"]) == before         # listing creates nothing
