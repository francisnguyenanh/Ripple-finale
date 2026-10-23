"""Closure C-11 (live run SUR07-V1-20261022): on knowledge read from real sources, the ROI replay treated the scope marker `{"unresolved": true}` as a dataset field, so no
rule was evaluated and every record was reported ALLOW; and a code route outcome such as `RouteType.NEEDS_MANAGER` was not read as an approval. The marker is no field; a
NEEDS_ route value is an approval; a rule with an open question still makes a record undecided (unsupported), never "allowed". Pure."""
from __future__ import annotations

from core.change_intelligence.v2 import policy_decision as pd
from core.change_intelligence.v2 import roi_replay


def rule(rid, value, outcome="RouteType.NEEDS_MANAGER", scope=None, status="VALIDATED"):
    return {"rule_id": rid, "title": rid, "authority": "OBSERVED", "status": status, "actor_ref": "role-manager", "outcome": {"kind": outcome, "predicate": ""},
            "condition": {"field": "request.net_amount_jpy", "op": "gte", "value": value, "unit": "JPY"}, "exceptions": [], "scope": scope if scope is not None else {"unresolved": True},
            "implementation_status": "not_required", "unknown_fields": [], "provenance": [], "source_bindings": [], "concept_ids": []}


RECORDS = [{"id": f"r{i}", "time": "2026-03-01T09:00:00Z", "amount_jpy": a, "currency": "JPY", "category": "OFFICE", "process": "STANDARD", "supplier_risk": "LOW"}
           for i, a in enumerate([900_000, 1_200_000, 1_600_000])]


def test_the_unresolved_scope_marker_is_not_a_field_and_a_needs_route_value_is_an_approval():
    kept, out = roi_replay.evaluable_rules([rule("r-old", 1_000_000)])
    assert [r["rule_id"] for r in kept] == ["r-old"] and out == []
    assert pd.classify_outcome("RouteType.NEEDS_MANAGER") == "REQUIRES_APPROVAL" and pd.classify_outcome("RouteType.AUTO") == "UNCLASSIFIED"
    rep = roi_replay.replay(RECORDS, {"label": "b", "rules": [rule("r-old", 1_000_000)], "gaps": []}, {"label": "t", "rules": [rule("r-new", 1_500_000)], "gaps": []},
                            assumptions={"minutes_per_approval": 20})
    assert rep["changes"]["count"] == 1 and rep["baseline"]["requires_approval"] == 2 and rep["target"]["requires_approval"] == 1     # 1,200,000 leaves manager review


def test_an_open_question_on_an_applicable_rule_makes_the_record_unsupported_not_allowed():
    gap = {"gap_id": "g", "kind": "missing_actor", "status": "OPEN", "affected_rule_ids": ["r-old"], "question": "Who?", "priority": "normal", "evidence_refs": []}
    rep = roi_replay.replay(RECORDS, {"label": "b", "rules": [rule("r-old", 1_000_000)], "gaps": [gap]}, {"label": "t", "rules": [rule("r-old", 1_000_000)], "gaps": [gap]},
                            assumptions={"minutes_per_approval": 20})
    assert rep["baseline"]["allow"] == 0 and rep["denominators"]["unsupported"] == 3                                                   # the open question keeps every record of that scope undecided (existing contract)
