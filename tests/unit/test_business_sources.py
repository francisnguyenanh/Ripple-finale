"""business_sources: from extraction candidates to OBSERVED rules with traced links between files (journey finding JOURNEY-V1-J1, 2026-10-21).

Part 1 replays the RECORDED model output of that live run (fixture `recorded/journey_v1_j1_candidates.json`): it tests the mapping on real output, not the model.
Part 2 uses hand-written candidates for the variants: renamed or duplicated configuration key, changed threshold, CAPEX / non-CAPEX, missing actor, a role
mapping stated once, twice with different roles, or not at all. Pure: no database, no model."""
from __future__ import annotations

import copy
import json
import pathlib

import yaml

from core.change_intelligence.v2 import business_graph as bg
from core.change_intelligence.v2 import business_knowledge as bk
from core.change_intelligence.v2 import business_sources as bs

ROOT = pathlib.Path(__file__).resolve().parents[2]
REC = json.loads((ROOT / "tests/fixtures/final_knowledge_loop/recorded/journey_v1_j1_candidates.json").read_text(encoding="utf-8"))
REC2 = json.loads((ROOT / "tests/fixtures/final_knowledge_loop/recorded/journey_v2_j1_candidates.json").read_text(encoding="utf-8"))
YAML_PATH = "fixtures/harbor_public/config/approval.yaml"


def yaml_observations(text, artifact_id="art-yaml"):
    out = []

    def walk(node, prefix):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                walk(v, path)
            else:
                out.append({"artifact_id": artifact_id, "locator": {"type": "key_path", "key_path": path}, "raw_value": v})
    walk(yaml.safe_load(text), "")
    return out


def real_observations(paths):
    """The ingest parsers of the product over the real files of the journey (code blocks and YAML leaves), stamped with the recorded artifact ids."""
    from core.change_intelligence.v2.source_ingest_adapters import parse_python, parse_yaml
    out = []
    for aid, p in paths.items():
        parser = parse_python if p.endswith(".py") else parse_yaml if p.endswith(".yaml") else None
        if parser:
            out += [dict(o, artifact_id=aid) for o in parser(p, (ROOT / p).read_bytes()).observations]
    return out


def recorded(rec=REC, *, code=True):
    paths = dict(rec["artifact_paths"])
    yaml_art = next(a for a, p in paths.items() if p.endswith("approval.yaml"))
    obs = real_observations(paths)
    values = bs.structured_values(obs, paths)
    return bs.observed_rules(copy.deepcopy(rec["candidates"]), values, paths, bs.code_facts(obs, paths) if code else None), yaml_art


def by_quote(rules, text):
    return [r for r in rules if any(text in str(b.get("evidence_ref")) for b in r["source_bindings"]) or text in r["title"]]


def test_recorded_output_the_capex_route_gets_its_condition_from_the_code_line_and_its_value_from_the_configuration_with_a_traced_link():
    (rules, report), yaml_art = recorded()
    capex = [r for r in rules if r["condition"] and "request.category" in json.dumps(r["condition"]) and "NEEDS_FINANCE" in r["outcome"]["kind"]]
    assert len(capex) == 2                                                                  # routing.py and emergency.py: the branch outcome fills what the model left out
    r = next(x for x in capex if REC["artifact_paths"][x["source_bindings"][0]["artifact_id"]].endswith("routing.py"))
    assert r["condition"] == {"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"},
                                      {"field": "request.net_amount_jpy", "op": "gte", "value": 2000000, "unit": "JPY"}]}
    assert {"artifact_id": yaml_art, "revision_id": REC["source_revision_id"], "evidence_ref": YAML_PATH + "#routing_rules.capex_finance_threshold_jpy"} in r["source_bindings"]
    links = {p["method"]: p for p in r["provenance"] if p["kind"] == "source_link"}
    assert links["attribute_name"]["reference"] == "config.capex_finance_threshold_jpy" and links["attribute_name"]["value"] == 2000000
    assert links["attribute_name"]["unit_from"] == "key_name"                               # the key says _jpy; no unit is assumed otherwise
    assert links["enum_member"]["to_ref"] == "CategoryType.CAPEX" and links["enum_member"]["value"] == "CAPEX"
    assert r["actor_ref"] == "role-finance" and "actor_ref" not in r["unknown_fields"] and r["authority"] == "OBSERVED" and r["status"] in ("VALIDATED", "DRAFT")
    actor_link = next(p for p in r["provenance"] if p.get("method") == "outcome_actor")
    assert actor_link["to_ref"].endswith("fixtures/harbor_public/app/approval.py#approve_request:" + str(actor_link["to_ref"].rsplit(":", 1)[1]))   # the code's own role check
    bk.validate_provenance(r["provenance"], status=r["status"])                             # the link is provenance, never a confirmation
    (rules_no_code, _), _ = recorded(code=False)                                            # without the code facts the actor and the emergency outcome stay unknown
    assert next(x for x in rules_no_code if "request.category" in json.dumps(x["condition"]))["actor_ref"] is None


def test_recorded_output_configuration_constants_are_supporting_facts_and_statements_without_actor_or_outcome_are_listed_not_questioned():
    (rules, report), _ = recorded()
    assert report["supporting_facts_count"] == 8 and all(f["evidence_refs"][0].startswith(YAML_PATH + "#") for f in report["supporting_facts"])
    assert report["incomplete_count"] >= 5 and all(set(i["missing"]) == {"actor", "outcome"} for i in report["incomplete"])
    gaps = bg.derive_gaps(rules)
    assert len(rules) < 20 and len(gaps) < 20                                                # was 34 rules and 71 questions on the same output
    assert report["code_branches"] == 18 and [u["line"] for u in report["uncovered_branches"]] == [                 # branches the model gave no candidate for: disclosed
        "if route.route == RouteType.NEEDS_MANAGER:", "elif route.route == RouteType.NEEDS_FINANCE:", "elif route.route == RouteType.NEEDS_DIRECTOR:"]
    assert not any(r["title"].startswith(("routing_rules.", "matching_rules.", "currency")) for r in rules)
    assert report["unresolved_references"] == []                                            # both config references resolve exactly once


def test_recorded_output_the_policy_rules_keep_their_evidence_and_no_actor_is_invented_for_a_route():
    (rules, report), _ = recorded()
    fc = [r for r in rules if r["actor_ref"] == "role-finance-controller"]
    assert len(fc) == 1 and 5000000 in [l.get("value") for l in [fc[0]["condition"]]] and fc[0]["condition"]["op"] == "gte"
    assert not any(r["actor_ref"] == "role-needs-finance" for r in rules)                 # a route name is never an actor by itself; role-finance comes from approval.py
    assert report["unresolved_actors"] == []



# ---- variants (hand-written candidates) -----------------------------------------------------------------------------------------------------------------------

CODE_LOC = json.dumps({"type": "code_span", "qualname": "route", "line_start": 1, "line_end": 9})


def code_cand(cid, quote, outcome="RouteType.NEEDS_FINANCE", actor=None, condition=None, rejected="condition_unsupported_expression", raw=None):
    c = {"candidate_id": cid, "subject": "request.net_amount_jpy", "predicate": "routes", "value": None, "operator": ">=", "unit": "JPY", "modality": "observed",
         "scope": {"unresolved": True}, "exceptions": [], "outcome": outcome, "actor": actor, "condition": condition, "business_fields_rejected": rejected,
         "applicability": "RECOGNIZED", "source_revision_id": "src-1", "artifact_revision_id": "art-code", "model_run_id": "run-1",
         "evidence": [{"quote": quote, "locator": CODE_LOC, "parser_version": "1"}]}
    if raw is not None:
        c["condition_raw"] = raw
    return c


PATHS = {"art-code": "app/routing.py", "art-yaml": "config/approval.yaml", "art-yaml2": "config/other.yaml"}
CAPEX_LINE = "if request.category == CategoryType.CAPEX and request.net_amount_jpy >= config.capex_finance_threshold_jpy:"


def values(text, art="art-yaml"):
    return bs.structured_values(yaml_observations(text, art), PATHS)


def test_a_changed_threshold_follows_the_configuration_and_a_non_capex_branch_keeps_its_own_condition():
    rules, _ = bs.observed_rules([code_cand("c1", CAPEX_LINE)], values("routing_rules:\n  capex_finance_threshold_jpy: 2500000\n"), PATHS)
    assert rules[0]["condition"]["all"][1]["value"] == 2500000
    plain = "if request.net_amount_jpy >= config.standard_manager_threshold_jpy:"
    rules, _ = bs.observed_rules([code_cand("c2", plain, outcome="RouteType.NEEDS_MANAGER")], values("routing_rules:\n  standard_manager_threshold_jpy: 1000000\n"), PATHS)
    assert rules[0]["condition"] == {"field": "request.net_amount_jpy", "op": "gte", "value": 1000000, "unit": "JPY"}
    assert "category" not in json.dumps(rules[0]["condition"])


def test_a_renamed_or_duplicated_key_or_a_non_number_is_never_guessed_and_is_reported_for_a_question():
    for text, reason in (("routing_rules:\n  capex_threshold: 2000000\n", "not_found"), ("routing_rules:\n  capex_finance_threshold_jpy: two million\n", "not_a_number")):
        rules, report = bs.observed_rules([code_cand("c1", CAPEX_LINE)], values(text), PATHS)
        assert rules[0]["condition"] is None and report["unresolved_references"][0]["reason"] == reason
        assert report["unresolved_references"][0]["reference"] == "config.capex_finance_threshold_jpy"
    two = values("routing_rules:\n  capex_finance_threshold_jpy: 2000000\n") + values("limits:\n  capex_finance_threshold_jpy: 3000000\n", "art-yaml2")
    rules, report = bs.observed_rules([code_cand("c1", CAPEX_LINE)], two, PATHS)
    assert rules[0]["condition"] is None and report["unresolved_references"][0]["reason"] == "several_values" and len(report["unresolved_references"][0]["candidates"]) == 2


def test_a_call_or_negated_isinstance_line_is_not_interpreted_and_the_models_raw_condition_is_a_fallback_only_for_a_reference():
    assert bs.code_condition("if isinstance(request.net_amount_jpy, bool) or not isinstance(request.net_amount_jpy, int):") is None
    assert bs.code_condition("if request.currency != \"JPY\":") == {"field": "request.currency", "op": "ne", "operand": ("literal", "JPY")}
    assert bs.code_condition("if not request.net_amount_jpy >= 1_000_000:")["not"]["operand"] == ("literal", 1000000)
    raw = {"all": [{"field": "request.category", "op": "eq", "value": "CategoryType.CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "value": "config.capex_finance_threshold_jpy"}]}
    cand = code_cand("c1", "return RouteDecision(...)", raw=raw)                              # no `if` line in the evidence: the model's own tree is used
    rules, report = bs.observed_rules([cand], values("routing_rules:\n  capex_finance_threshold_jpy: 2000000\n"), PATHS)
    assert rules[0]["condition"]["all"][0]["value"] == "CAPEX" and rules[0]["condition"]["all"][1]["value"] == 2000000


def test_an_actor_comes_only_from_one_stated_mapping_of_the_same_outcome_value():
    route = code_cand("c1", CAPEX_LINE)
    role = code_cand("c2", "if route.route == RouteType.NEEDS_FINANCE:", outcome="required_role = Role.FINANCE", actor="role-finance",
                     condition={"field": "route.route", "op": "eq", "value": "RouteType.NEEDS_FINANCE"}, rejected=None)
    vals = values("routing_rules:\n  capex_finance_threshold_jpy: 2000000\n")
    rules, report = bs.observed_rules([route, role], vals, PATHS)
    capex = next(r for r in rules if r["candidate_ids"] == ["c1"])
    assert capex["actor_ref"] == "role-finance" and "actor_ref" not in capex["unknown_fields"]
    link = next(p for p in capex["provenance"] if p.get("method") == "outcome_actor")
    assert link["to_ref"] == next(r for r in rules if r["candidate_ids"] == ["c2"])["rule_id"]
    other = copy.deepcopy(role)
    other.update(candidate_id="c3", actor="role-cfo")
    rules, report = bs.observed_rules([route, role, other], vals, PATHS)
    assert next(r for r in rules if r["candidate_ids"] == ["c1"])["actor_ref"] is None and report["unresolved_actors"][0]["actors"] == ["role-cfo", "role-finance"]
    rules, _ = bs.observed_rules([route], vals, PATHS)
    assert rules[0]["actor_ref"] is None                                                    # no mapping: unknown stays unknown


def test_a_structured_rule_binding_is_written_as_path_and_key_so_the_refresh_read_back_and_the_draft_can_find_it():
    cand = {"candidate_id": "y1", "subject": "manager threshold", "predicate": "requires", "value": 1000000, "operator": ">=", "unit": "JPY", "modality": "must",
            "scope": {"process": "standard"}, "exceptions": [], "outcome": "manager approval", "actor": "role-manager", "condition": None, "business_fields_rejected": None,
            "applicability": "RECOGNIZED", "source_revision_id": "src-1", "artifact_revision_id": "art-yaml", "model_run_id": "run-1",
            "evidence": [{"quote": "routing_rules.standard_manager_threshold_jpy: 1000000", "locator": json.dumps({"type": "key_path", "key_path": "routing_rules.standard_manager_threshold_jpy"})}]}
    rules, _ = bs.observed_rules([cand], [], PATHS)
    assert rules[0]["source_bindings"][0]["evidence_ref"] == "config/approval.yaml#routing_rules.standard_manager_threshold_jpy"
    unbound = bg.unbound_values(rules, yaml_observations("routing_rules:\n  standard_manager_threshold_jpy: 1000000\n  other_jpy: 5\n"), PATHS)
    assert [v["location"] for v in unbound["values"]] == ["routing_rules.other_jpy"]       # the bound value is no longer listed as unused


def test_the_reading_summary_lists_what_was_not_read_as_a_rule_and_what_could_not_be_linked():
    from infra.adapters.v2_business import _sources_summary
    (rules, report), _ = recorded()
    s = _sources_summary({"sources": dict(report, carried_files=[])})
    assert s["supporting_facts"] == 8 and s["incomplete"] == report["incomplete_count"] and s["source_links"] == report["source_links_count"]
    assert len(s["incomplete_items"]) == min(50, report["incomplete_count"]) and all(set(i) == {"title", "file", "missing"} for i in s["incomplete_items"])
    assert s["unresolved_references"] == [] and _sources_summary({"origin": "analysis"}) is None              # seeded knowledge: nothing to summarise
    vals = values("routing_rules:\n  capex_threshold: 2000000\n")
    _, rep = bs.observed_rules([code_cand("c1", CAPEX_LINE)], vals, PATHS)
    assert _sources_summary({"sources": rep})["unresolved_references"] == [{"reference": "config.capex_finance_threshold_jpy", "reason": "not_found"}]


def test_a_configuration_value_whose_outcome_only_restates_the_value_is_a_supporting_fact_not_a_rule():
    """Closure C-10 (live run JOURNEY-V8): the model returned each approval.yaml key with `outcome` = the value itself (`1000000`, `JPY`, `1.0.0`); those became rules and the
    change request picked one. Such a candidate is a supporting fact; a real outcome or an actor still makes a rule."""
    loc = [{"locator": json.dumps({"type": "key_path", "key_path": "routing_rules.standard_manager_threshold_jpy"})}]
    value_only = {"subject": "routing_rules.standard_manager_threshold_jpy", "predicate": "is set to", "value": 1000000, "operator": "==", "actor": None, "outcome": "1000000", "evidence": loc}
    assert bs.classify(value_only)[0] == "supporting_fact"
    assert bs.classify(dict(value_only, value="1,000,000"))[0] == "supporting_fact"
    assert bs.classify(dict(value_only, value="1.0.0", outcome="1.0.0"))[0] == "supporting_fact"
    assert bs.classify(dict(value_only, outcome="RouteType.NEEDS_MANAGER"))[0] == "rule"                     # a real outcome
    assert bs.classify(dict(value_only, actor="role-manager"))[0] == "rule"                                  # an actor
    route = dict(value_only, subject="routing_rules.high_risk_route", value="NEEDS_DIRECTOR", outcome="NEEDS_DIRECTOR", actor="role-needs-director")
    assert bs.classify(route)[0] == "supporting_fact"                                                         # the value turned into a role name is not a stated actor
    prose = {"subject": "Purchase requests", "value": 1000000, "outcome": "approval from the requesting department's manager", "actor": None, "evidence": []}
    assert bs.classify(prose)[0] == "rule"


REC_V8 = json.loads((ROOT / "tests/fixtures/final_knowledge_loop/recorded/journey_v8_j1_candidates.json").read_text(encoding="utf-8"))


def test_recorded_v8_output_configuration_values_are_supporting_facts_and_the_approval_rules_and_the_traced_route_stay():
    """Closure C-10 on the recorded JOURNEY-V8-J1 output: the eight approval.yaml answers repeated their value as the "outcome"; seven become supporting facts with their
    `path#key` evidence (the eighth, `high_risk_route`, whose actor is only its value as a role name, too); the approval rules keep 1,000,000 with an
    order operator; the CAPEX route keeps its traced link to the configured 2,000,000."""
    (rules, report), _ = recorded(REC_V8)
    titles = [r["title"] for r in rules]
    assert not any(t.startswith(("routing_rules.", "matching_rules.", "currency", "version")) for t in titles), titles
    facts = {f["evidence_refs"][0] for f in report["supporting_facts"]}
    assert {YAML_PATH + "#routing_rules.standard_manager_threshold_jpy", YAML_PATH + "#routing_rules.capex_finance_threshold_jpy"} <= facts
    def leaves(c):
        return [c] if c and "field" in c else [x for k in ("all", "any") for y in (c or {}).get(k, []) for x in leaves(y)]
    approvals = [r for r in rules if any(l.get("value") == 1000000 and l.get("op") in ("gte", "gt") for l in leaves(r["condition"]))]
    assert len(approvals) >= 3
    capex = [r for r in rules if any(l.get("value") == 2000000 for l in leaves(r["condition"])) and any(l.get("value") == "CAPEX" for l in leaves(r["condition"]))]
    assert capex and all(any(p.get("method") == "attribute_name" and p.get("to_ref", "").endswith("capex_finance_threshold_jpy") for p in r["provenance"]) for r in capex)

