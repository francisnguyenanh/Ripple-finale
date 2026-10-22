"""Code facts (AST branch outcomes, role mappings) and the recorded JOURNEY-V2-J1 output, where every code candidate came back without outcome and actor.
Pure: no database, no model. Helpers from test_business_sources."""
from __future__ import annotations

import json

from core.change_intelligence.v2 import business_sources as bs
from tests.unit.test_business_sources import CAPEX_LINE, PATHS, REC2, code_cand, recorded, values

ROUTE_SRC = """def route(request, config):
    if request.category == CategoryType.CAPEX and request.net_amount_jpy >= config.capex_finance_threshold_jpy:
        return RouteDecision(request.request_id, RouteType.NEEDS_FINANCE, "CAPEX_HIGH_VALUE")
    if sup.risk == SupplierRisk.HIGH:
        return RouteDecision(request.request_id, config.high_risk_route, "X")
    if isinstance(request.net_amount_jpy, bool):
        return RouteDecision(request.request_id, RouteType.REJECTED, "INVALID")
    return RouteDecision(request.request_id, RouteType.AUTO, "AUTO")
"""
ROLE_SRC = """def required_role(route):
    if route.route == RouteType.NEEDS_FINANCE:
        required_role = Role.FINANCE
    elif route.route == RouteType.NEEDS_DIRECTOR:
        required_role = Role.DIRECTOR
    return required_role
"""


def obs(artifact, qualname, start, end, text):
    return {"artifact_id": artifact, "locator": {"type": "code_span", "qualname": qualname, "line_start": start, "line_end": end}, "raw_value": text}


def test_recorded_v2_output_where_every_code_candidate_lacks_outcome_and_actor_still_yields_the_complete_capex_route():
    (rules, report), _ = recorded(REC2)
    assert all(c["outcome"] is None and c["actor"] is None for c in REC2["candidates"] if REC2["artifact_paths"][c["artifact_revision_id"]].endswith(".py"))
    capex = next(r for r in rules if "request.category" in json.dumps(r["condition"]) and REC2["artifact_paths"][r["source_bindings"][0]["artifact_id"]].endswith("routing.py"))
    assert capex["condition"] == {"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "value": 2000000, "unit": "JPY"}]}
    assert capex["outcome"]["kind"] == "RouteType.NEEDS_FINANCE" and capex["actor_ref"] == "role-finance"
    methods = [p["method"] for p in capex["provenance"] if p["kind"] == "source_link"]
    assert sorted(methods) == ["attribute_name", "branch_outcome", "enum_member", "outcome_actor"]
    high_risk = next(r for r in rules if r["condition"] == {"field": "sup.risk", "op": "eq", "value": "HIGH"}
                     and REC2["artifact_paths"][r["source_bindings"][0]["artifact_id"]].endswith("routing.py"))
    assert high_risk["outcome"]["kind"] == "NEEDS_DIRECTOR"                                 # `return ... config.high_risk_route ...` resolved to the configured value
    role_check = next(r for r in rules if r["condition"] == {"field": "route.route", "op": "eq", "value": "NEEDS_FINANCE"})
    assert role_check["actor_ref"] == "role-finance" and role_check["outcome"]["kind"] == "required_role = Role.FINANCE"
    assert report["uncovered_branches"] == [] and report["unresolved_actors"] == []
    assert not any(r["actor_ref"] and "needs" in r["actor_ref"] for r in rules)


def test_code_facts_read_branches_returns_assignments_and_role_mappings_and_skip_what_they_cannot_parse():
    observations = [obs("a", "route", 1, 8, ROUTE_SRC), obs("a", "required_role", 11, 16, ROLE_SRC), obs("a", "broken", 20, 21, "def x(:\n  pass")]
    f = bs.code_facts(observations, {"a": "app/r.py"})
    by = {b["line"]: b for b in f["branches"]}
    assert by["if request.category == CategoryType.CAPEX and request.net_amount_jpy >= config.capex_finance_threshold_jpy:"]["outcome"] == ("enum", "NEEDS_FINANCE", "RouteType.NEEDS_FINANCE")
    assert by["if sup.risk == SupplierRisk.HIGH:"]["outcome"] == ("ref", "config.high_risk_route")
    assert "if isinstance(request.net_amount_jpy, bool):" not in by                           # a call is not interpreted
    assert by["if route.route == RouteType.NEEDS_FINANCE:"]["outcome"] == ("assign", "required_role", "FINANCE", "Role.FINANCE")
    assert f["role_mappings"] == {"NEEDS_FINANCE": {"role-finance": ["app/r.py#required_role:13"]}, "NEEDS_DIRECTOR": {"role-director": ["app/r.py#required_role:15"]}}
    assert by["if sup.risk == SupplierRisk.HIGH:"]["line_no"] == 4 and by["if route.route == RouteType.NEEDS_FINANCE:"]["outcome_line"] == 13


def test_two_files_mapping_the_same_route_to_different_roles_leave_the_actor_unknown_and_a_renamed_function_or_key_still_links_by_content():
    route = code_cand("c1", CAPEX_LINE, outcome=None, rejected=None)
    observations = [
        obs("art-code", "route_any_name", 1, 3, "def route_any_name(request, config):\n    " + CAPEX_LINE + "\n        return RouteDecision(request.request_id, RouteType.NEEDS_FINANCE, \"X\")\n"),
        obs("art-roles", "who", 1, 3, "def who(route):\n    if route.route == RouteType.NEEDS_FINANCE:\n        required_role = Role.FINANCE\n"),
        obs("art-roles2", "who2", 1, 3, "def who2(route):\n    if route.route == RouteType.NEEDS_FINANCE:\n        approver_role = Role.CFO\n")]
    paths = dict(PATHS, **{"art-roles": "app/approval.py", "art-roles2": "app/legacy_approval.py"})
    vals = values("limits:\n  capex_finance_threshold_jpy: 2000000\n")
    rules, rep = bs.observed_rules([route], vals, paths, bs.code_facts(observations, paths))
    r = rules[0]
    assert r["outcome"]["kind"] == "RouteType.NEEDS_FINANCE" and r["condition"]["all"][1]["value"] == 2000000   # outcome from the branch, value by key name
    assert r["actor_ref"] is None and rep["unresolved_actors"] == [{"rule_id": r["rule_id"], "value": "NEEDS_FINANCE", "actors": ["role-cfo", "role-finance"]}]
    rules, rep = bs.observed_rules([route], vals, paths, bs.code_facts(observations[:2], paths))
    assert rules[0]["actor_ref"] == "role-finance" and next(p for p in rules[0]["provenance"] if p.get("method") == "outcome_actor")["to_ref"] == "app/approval.py#who:3"
    rules, rep = bs.observed_rules([route], vals, paths, bs.code_facts(observations[:1], paths))      # no role mapping anywhere: unknown, honestly
    assert rules[0]["actor_ref"] is None and rep["unresolved_actors"] == []
