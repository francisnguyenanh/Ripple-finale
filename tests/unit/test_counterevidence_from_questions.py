"""A disagreement recorded as a question (by the analysis, or suggested by a model and answered by a person) is counter-evidence for an answer whether or not a
`conflicts_with` relation names the pair (business_index.counterevidence, 2026-10-22, JOURNEY-V4-J3 A19). A model suggestion nobody answered is not a stored
question and is never counted. Pure."""
from __future__ import annotations

from core.change_intelligence.v2 import business_index as bi


def rule(rid, title, status="VALIDATED"):
    return {"rule_id": rid, "title": title, "status": status, "authority": "OBSERVED", "condition": {"field": "x", "op": "gte", "value": 1}, "actor_ref": None,
            "outcome": {"kind": "y", "predicate": ""}, "exceptions": [], "scope": {}, "source_bindings": [], "provenance": [], "concept_ids": [], "implementation_status": "not_required", "unknown_fields": []}


RULES = [rule("rule-code-capex", "CAPEX route"), rule("rule-policy-capex", "Capital expenditure"), rule("rule-other", "Other")]


def gap(status, rids=("rule-code-capex", "rule-policy-capex"), gid="gap-m"):
    return {"gap_id": gid, "kind": "conflicting_facts", "status": status, "affected_rule_ids": list(rids), "question": "Which?", "priority": "critical", "evidence_refs": ["a", "b"]}


def test_a_resolved_or_open_disagreement_question_names_the_other_rule_as_counter_evidence_once():
    out = bi.counterevidence(["rule-code-capex"], RULES, [], [gap("RESOLVED")])
    conf = [c for c in out if c["kind"] == "conflicting_rule"]
    assert len(conf) == 1 and conf[0]["rule_id"] == "rule-policy-capex" and conf[0]["with"] == "rule-code-capex" and conf[0]["from_question"] == "gap-m" and conf[0]["question_status"] == "RESOLVED"
    out = bi.counterevidence(["rule-policy-capex"], RULES, [], [gap("OPEN")])
    assert [c["rule_id"] for c in out if c["kind"] == "conflicting_rule"] == ["rule-code-capex"] and any(c["kind"] == "open_question" for c in out)


def test_a_stored_relation_is_not_duplicated_a_withdrawn_rule_and_an_unrelated_question_are_left_out():
    edge = {"edge_id": "e1", "type": "conflicts_with", "from": "rule-code-capex", "to": "rule-policy-capex", "status": "PROPOSED", "provenance_refs": []}
    out = bi.counterevidence(["rule-code-capex"], RULES, [edge], [gap("RESOLVED")])
    assert [c["edge_id"] for c in out if c["kind"] == "conflicting_rule"] == ["e1"]
    withdrawn = [rule("rule-code-capex", "x"), rule("rule-policy-capex", "y", status="SUPERSEDED")]
    assert [c for c in bi.counterevidence(["rule-code-capex"], withdrawn, [], [gap("RESOLVED")]) if c["kind"] == "conflicting_rule"] == []
    assert [c for c in bi.counterevidence(["rule-other"], RULES, [], [gap("RESOLVED")]) if c["kind"] == "conflicting_rule"] == []
    assert [c for c in bi.counterevidence(["rule-code-capex"], RULES, [], [gap("ACCEPTED_UNKNOWN")]) if c["kind"] == "conflicting_rule"] == []   # left open on purpose: no disagreement claim


def test_a_decided_rule_stands_for_the_observed_rule_it_replaces_when_the_question_names_the_observed_pair():
    target = dict(rule("rule-code-capex-target", "CAPEX route (decided)"), authority="TARGET", status="CONFIRMED")
    out = bi.counterevidence(["rule-code-capex-target"], RULES + [target], [], [gap("RESOLVED")])        # the answer cited the decided rule; the question names the observed ids
    conf = [c for c in out if c["kind"] == "conflicting_rule"]
    assert len(conf) == 1 and conf[0]["rule_id"] == "rule-policy-capex" and conf[0]["with"] == "rule-code-capex"

