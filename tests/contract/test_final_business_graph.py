"""KNO-03 (pure part): relation proposals, gap detection and bounded traversal over business rules. Fixtures: tests/fixtures/final_knowledge_loop."""
import copy
import json
import pathlib

from core.change_intelligence.v2 import business_graph as bg
from core.change_intelligence.v2 import business_knowledge as bk

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "final_knowledge_loop"
KNOWLEDGE = json.loads((FIX / "expected_knowledge.json").read_text(encoding="utf-8"))
EXAMPLES = json.loads((FIX / "contract_examples.json").read_text(encoding="utf-8"))
RULES = {r["rule_id"]: r for r in KNOWLEDGE["observed_rules"]}


def gaps_of(rules):
    return bg.derive_gaps(rules)


def test_capex_threshold_conflict_becomes_one_critical_gap_with_both_citations():
    gaps = [g for g in gaps_of(RULES.values()) if g["kind"] == "conflicting_facts"]
    assert len(gaps) == 1
    g = gaps[0]
    expected = next(x for x in KNOWLEDGE["expected_gaps"] if x["kind"] == "conflicting_facts")
    assert g["priority"] == "critical" and g["affected_rule_ids"] == sorted(expected["affected_rule_ids"])
    assert set(expected["must_cite"]) <= set(g["evidence_refs"])
    assert {o["label"] for o in g["options"]} == {"2000000", "5000000"} and "Which one is the business rule?" in g["question"]
    bk.validate_gap(g)


def test_emergency_rule_without_actor_or_condition_is_asked_not_guessed():
    gaps = {(g["kind"], tuple(g["affected_rule_ids"])): g for g in gaps_of(RULES.values())}
    actor = gaps[("missing_actor", ("rule-emergency-routing",))]
    assert actor["priority"] == "normal"            # its outcome is unknown, so it is not yet known to need a review
    assert ("missing_condition", ("rule-emergency-routing",)) in gaps and ("missing_outcome", ("rule-emergency-routing",)) in gaps
    expected = next(x for x in KNOWLEDGE["expected_gaps"] if x["kind"] == "missing_actor")
    assert set(expected["must_cite"]) <= set(actor["evidence_refs"])
    assert not any(k[1] == ("rule-standard-manager-review",) for k in gaps)      # a complete rule raises nothing


def test_gap_ids_are_stable_and_rerun_does_not_duplicate():
    first, second = gaps_of(RULES.values()), gaps_of(reversed(list(RULES.values())))
    assert first == second and len({g["gap_id"] for g in first}) == len(first)
    for g in first:
        bk.validate_gap(g)


def test_same_number_in_a_different_rule_is_not_merged_or_flagged():
    other = copy.deepcopy(RULES["rule-standard-manager-review"])
    other.update(rule_id="rule-other-1m", concept_ids=["concept-freight"], actor_ref="role-logistics", title="Freight desk review from 1,000,000 JPY")
    rules = [*RULES.values(), other]
    assert not any("rule-other-1m" in e["from"] + e["to"] for e in bg.derive_relations(rules) if e["type"] == "conflicts_with")
    assert not any("rule-other-1m" in g["affected_rule_ids"] and g["kind"] == "conflicting_facts" for g in gaps_of(rules))
    same_actor = copy.deepcopy(other)
    same_actor.update(rule_id="rule-other-2m", actor_ref="role-manager", scope={"process": ["STANDARD"]})
    same_actor["condition"] = {"field": "request.net_amount_jpy", "op": "gte", "value": 2000000, "unit": "JPY"}
    same_actor["concept_ids"] = ["concept-purchase-approval"]
    assert any(g["kind"] == "conflicting_facts" and "rule-other-2m" in g["affected_rule_ids"] for g in gaps_of([*RULES.values(), same_actor]))   # same actor, concept, scope: a real conflict


def test_uncomparable_conditions_are_not_compared():
    a = copy.deepcopy(RULES["rule-capex-finance-config"])
    b = copy.deepcopy(RULES["rule-capex-finance-policy"])
    b["condition"] = {"any": [b["condition"]["all"][1]]}
    assert not any(g["kind"] == "conflicting_facts" for g in gaps_of([a, b]))


def test_relations_are_proposals_unless_a_human_decision_backs_them():
    edges = bg.derive_relations(RULES.values())
    assert {e["type"] for e in edges} == {"defines", "approves", "conflicts_with"}
    assert all(e["status"] == "PROPOSED" and e["provenance_refs"] for e in edges)
    for e in edges:
        bk.validate_relation(e)
    target = EXAMPLES["target_rule"]
    confirmed = [e for e in bg.derive_relations([target]) if e["type"] == "approves"]
    assert confirmed[0]["status"] == "CONFIRMED" and confirmed[0]["provenance_refs"] == ["decision-s1-capex"]
    bare = copy.deepcopy(RULES["rule-high-risk-director"])
    bare["source_bindings"], bare["provenance"] = [], [{"kind": "ai_inference", "model_run_id": "run-1"}]
    bare_edges = bg.derive_relations([bare])
    assert all(e["status"] == "PROPOSED" and e["provenance_refs"] == [] for e in bare_edges)      # no evidence, no decision: PROPOSED with an honest empty list


def test_multi_hop_trace_reaches_the_other_rule_and_discloses_limits():
    edges = bg.derive_relations(RULES.values())
    t = bg.traverse(edges, "role-finance", max_hops=2)
    assert {"rule-capex-finance-config", "rule-capex-finance-policy", "concept-capex"} <= set(t["nodes"])
    path = t["paths"]["concept-capex"]
    assert len(path) == 2 and all(any(e["edge_id"] == pid for e in edges) for pid in path)   # every hop is a stored edge, so it has provenance
    one = bg.traverse(edges, "role-finance", max_hops=1)
    assert "concept-capex" not in one["nodes"] and one["omitted"]["beyond_hops"] > 0
    capped = bg.traverse(edges, "role-finance", max_nodes=2)
    assert len(capped["nodes"]) == 2 and capped["omitted"]["nodes_over_cap"] > 0


def test_cycles_terminate():
    edges = [{"edge_id": f"edge-{i}", "from": a, "to": b, "type": "depends_on", "status": "PROPOSED", "provenance_refs": []}
             for i, (a, b) in enumerate([("n1", "n2"), ("n2", "n3"), ("n3", "n1")])]
    t = bg.traverse(edges, "n1", max_hops=10)
    assert sorted(t["nodes"]) == ["n1", "n2", "n3"]


def test_withdrawn_rules_raise_nothing_and_join_nothing():
    a, b = copy.deepcopy(RULES["rule-capex-finance-config"]), copy.deepcopy(RULES["rule-capex-finance-policy"])
    b["status"] = "SUPERSEDED"
    assert not any(g["kind"] == "conflicting_facts" for g in bg.derive_gaps([a, b]))
    assert not any(e["type"] == "conflicts_with" or b["rule_id"] in (e["from"], e["to"]) for e in bg.derive_relations([a, b]))
