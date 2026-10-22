"""Candidate pairs for the gap-proposal model (business_proposals.candidate_pairs, 2026-10-22): rules that decide the same kind of case by an amount but state another
threshold or actor are listed for the model to examine; the list decides nothing. Found on live run JOURNEY-V3-J2: the 2,000,000 (code) and 5,000,000 (policy) CAPEX
rules use different field names and actors, so neither the structural check nor the model's instruction (which excluded numeric disagreements) reached them.
Pure: no database, no model."""
from __future__ import annotations

import json
import re
import pathlib

from core.change_intelligence.v2 import business_proposals as bp

ROOT = pathlib.Path(__file__).resolve().parents[2]
FROZEN = json.loads((ROOT / "config/knowledge_gap_proposals.json").read_text(encoding="utf-8"))


def rule(rid, cond, actor, title, scope=None, status="VALIDATED"):
    return {"rule_id": rid, "title": title, "condition": cond, "actor_ref": actor, "scope": scope or {}, "status": status, "authority": "OBSERVED",
            "outcome": {"kind": "x", "predicate": ""}, "exceptions": [], "provenance": [], "source_bindings": []}


POLICY_CAPEX = rule("rule-policy-capex", {"op": "gte", "unit": "JPY", "field": "capital_expenditure.amount", "value": 5000000}, "role-finance-controller",
                    "Capital expenditure - is referred", {"type": "Capital expenditure"})
CODE_CAPEX = rule("rule-code-capex", {"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "value": 2000000, "unit": "JPY"}]},
                  "role-finance", "request.net_amount_jpy - routes CAPEX", {"unresolved": True}, status="DRAFT")
POLICY_MANAGER = rule("rule-policy-manager", {"op": "gte", "unit": "JPY", "field": "purchase_request.amount", "value": 1000000}, "role-requesting-department-s-manager",
                      "Purchase requests - require approval", {"type": "Standard purchases"})
CODE_MANAGER = rule("rule-code-manager", {"field": "request.net_amount_jpy", "op": "gte", "value": 1000000, "unit": "JPY"}, "role-manager", "request.net_amount_jpy - routes to manager")
HIGH_RISK = rule("rule-high-risk", {"field": "supplier.risk", "op": "eq", "value": "HIGH"}, "role-operations-director", "Purchase - is referred")


def test_the_capex_threshold_pair_is_found_across_field_names_and_actors_and_a_rule_without_an_amount_is_never_paired():
    pairs = bp.candidate_pairs([POLICY_CAPEX, CODE_CAPEX, HIGH_RISK, POLICY_MANAGER, CODE_MANAGER])
    capex = next(p for p in pairs if set(p["rule_ids"]) == {"rule-code-capex", "rule-policy-capex"})
    assert capex["case"] == "CAPEX" and "thresholds 2000000 and 5000000" in capex["why"] and "actors role-finance and role-finance-controller" in capex["why"]
    assert not any("rule-high-risk" in p["rule_ids"] for p in pairs)
    assert not any({"rule-policy-capex", "rule-code-manager"} == set(p["rule_ids"]) for p in pairs)      # a CAPEX rule and a general one are different cases
    manager = next(p for p in pairs if set(p["rule_ids"]) == {"rule-policy-manager", "rule-code-manager"})
    assert "thresholds" not in manager["why"] and "actors" in manager["why"]                            # same number, differently named actors: listed, not judged


def test_a_withdrawn_rule_and_two_rules_that_agree_make_no_pair_and_the_prompt_lists_pairs_for_the_model_to_examine():
    assert bp.candidate_pairs([dict(CODE_CAPEX, status="SUPERSEDED"), POLICY_CAPEX]) == []
    assert bp.candidate_pairs([CODE_CAPEX, dict(CODE_CAPEX, rule_id="rule-code-capex-2")]) == []
    prompt = bp.build_prompt(FROZEN, [POLICY_CAPEX, CODE_CAPEX], [], [], can_read_evidence=False, max_chars=FROZEN["limits"]["max_prompt_chars"])
    assert "<pairs>\n- rule-code-capex and rule-policy-capex: both decide a CAPEX purchase by its amount but state different thresholds 2000000 and 5000000; actors role-finance and role-finance-controller (case read from: condition literal, scope)\n</pairs>" in prompt
    assert "whatever words or field names each source uses" in prompt and "would not catch" not in prompt
    empty = bp.build_prompt(FROZEN, [HIGH_RISK], [], [], can_read_evidence=False, max_chars=FROZEN["limits"]["max_prompt_chars"])
    assert "<pairs>\n(none)\n</pairs>" in empty


def test_the_whole_prompt_body_shares_one_budget_so_many_open_questions_and_relations_cannot_push_it_past_the_ceiling():
    """Live run JOURNEY-V6-J2 (2026-10-22): 34 rules at the cap plus 22 open questions and 52 relation lines made a 23,956-character prompt that the input ceiling refused
    before any call. The rules keep priority; pairs, questions and relations are cut with a note once the budget is used up."""
    frozen = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "knowledge_gap_proposals.json").read_text(encoding="utf-8"))
    rules = [{"rule_id": f"rule-{i:02d}", "title": "t" * 200, "status": "VALIDATED", "actor_ref": None, "condition": {"field": "x", "op": "gte", "value": i}, "exceptions": [],
              "scope": {}, "outcome": {"kind": "k"}, "source_bindings": [], "provenance": []} for i in range(34)]
    asked = [f"- missing_actor about rule-{i:02d}: Who acts on it?" * 3 for i in range(22)]
    edges = [f"- rule-{i:02d} depends_on rule-{(i + 1) % 34:02d}" * 4 for i in range(52)]
    max_chars = frozen["limits"]["max_prompt_chars"]
    prompt = bp.build_prompt(frozen, rules, asked, edges, can_read_evidence=True, max_chars=max_chars)
    template_len = len(frozen["prompt_template"]) - sum(len(k) for k in ("{rules}", "{asked}", "{edges}", "{pairs}"))
    assert len(prompt) <= template_len + max_chars + 4 * len("- (more not shown)") + 4 * len("(none)") + 200
    assert "(more not shown)" in prompt and prompt.count("- id=rule-") >= 1
    small = bp.build_prompt(frozen, rules[:3], asked[:2], edges[:2], can_read_evidence=True, max_chars=max_chars)
    assert "(more not shown)" not in small and all(a in small for a in asked[:2]) and all(e in small for e in edges[:2])


def test_a_pair_names_the_basis_of_its_case_and_a_cut_prompt_reports_its_coverage():
    """Closure C-02 / C-01 (2026-10-22). The v6 policy CAPEX rule carried the category only in its field name and title: the pair is still offered and says on what
    basis (a condition field segment before a title word; a condition literal first). The prompt's coverage counts what was shown of what the snapshot holds."""
    policy_field_only = dict(POLICY_CAPEX, title="Purchases above the threshold - are referred", scope={}, condition={"field": "capital_expenditure.amount", "op": "gte", "value": 5000000, "unit": "JPY"})
    pairs = bp.candidate_pairs([policy_field_only, CODE_CAPEX])
    assert len(pairs) == 1 and pairs[0]["case"] == "CAPEX" and pairs[0]["case_from"] == ["condition field", "condition literal"]
    title_only = dict(policy_field_only, condition={"field": "request.net_amount_jpy", "op": "gte", "value": 5000000, "unit": "JPY"}, title="Capital expenditure - is referred")
    assert bp.candidate_pairs([title_only, CODE_CAPEX])[0]["case_from"] == ["condition literal", "title word"]
    no_marker = dict(title_only, title="Large purchases - are referred")
    assert bp.candidate_pairs([no_marker, CODE_CAPEX]) == []                                        # no structured or titled category: not the same kind of case; nothing invented
    frozen = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "knowledge_gap_proposals.json").read_text(encoding="utf-8"))
    rules = [{"rule_id": f"rule-{i:02d}", "title": "t" * 200, "status": "VALIDATED", "actor_ref": None, "condition": {"field": "x", "op": "gte", "value": i}, "exceptions": [],
              "scope": {}, "outcome": {"kind": "k"}, "source_bindings": [], "provenance": []} for i in range(34)]
    asked = [f"- missing_actor about rule-{i:02d}: Who acts on it?" * 3 for i in range(22)]
    edges = [f"- rule-{i:02d} depends_on rule-{(i + 1) % 34:02d}" * 4 for i in range(52)]
    prompt, cov = bp.build_prompt_with_coverage(frozen, rules, asked, edges, can_read_evidence=True, max_chars=frozen["limits"]["max_prompt_chars"])
    assert cov["rules"]["total"] == 34 and cov["questions"]["total"] == 22 and cov["relations"]["total"] == 52 and cov["complete"] is False
    assert cov["questions"]["shown"] <= 22 and cov["relations"]["shown"] < 52 and cov["body_chars"] <= frozen["limits"]["max_prompt_chars"] and cov["prompt_chars"] == len(prompt)
    _, full = bp.build_prompt_with_coverage(frozen, rules[:3], asked[:2], edges[:2], can_read_evidence=True, max_chars=frozen["limits"]["max_prompt_chars"])
    assert full["complete"] is True and full["questions"] == {"shown": 2, "total": 2}


def _rules_and_frozen():
    frozen = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "knowledge_gap_proposals.json").read_text(encoding="utf-8"))
    return {r["rule_id"]: r for r in (POLICY_CAPEX, CODE_CAPEX)}, frozen


def _suggest(question, rules, frozen, *gaps):
    g = {"kind": "conflicting_facts", "rule_ids": [CODE_CAPEX["rule_id"], POLICY_CAPEX["rule_id"]], "question": question, "evidence_refs": [CODE_CAPEX["rule_id"]]}
    obj = {"status": "OK", "gaps": [g, *gaps], "relations": [], "notes": []}
    return bp.resolve_proposals(obj, rules, [], [], limits=frozen["limits"], can_read_evidence=False)


def test_an_over_long_question_keeps_its_decision_clause_names_both_rules_structurally_and_discloses_the_cut():
    """Closure C-06 (live run JOURNEY-V7-J2, 2026-10-22). A question made long by two 45-character rule ids is kept: the head and the LAST sentence (the one that asks)
    stay, the cut is disclosed with the full wording on the result, the two rules are named on the gap (`affected_rule_ids`, `options`) — nothing a person needs to
    decide is lost. Unsafe text has its own reason; empty text too; a second wording of the same disagreement in one proposal is a duplicate."""
    rules, frozen = _rules_and_frozen()
    limit = frozen["limits"]["max_question_chars"]
    decision = "Which threshold is the business rule for a capital purchase between those amounts, and who approves it?"
    long_q = ("Do these rules conflict regarding CAPEX purchases? Rule " + CODE_CAPEX["rule_id"] + " states a threshold of 2000000 JPY for the Finance team, whereas rule "
              + POLICY_CAPEX["rule_id"] + " states a threshold of 5000000 JPY for the Finance Controller, so a purchase of 3,000,000 JPY would be routed differently by each source. " + decision)
    assert len(long_q) > limit
    out = _suggest(long_q, rules, frozen)
    assert out["rejected"] == [] and len(out["gaps"]) == 1
    gap = out["gaps"][0]
    assert len(gap["question"]) <= limit and gap["question"].endswith(decision) and "… " in gap["question"] and gap["question"].startswith("Do these rules conflict")
    assert sorted(gap["affected_rule_ids"]) == sorted([CODE_CAPEX["rule_id"], POLICY_CAPEX["rule_id"]]) and [o["rule_id"] for o in gap["options"]] == gap["affected_rule_ids"]
    [tr] = out["questions_trimmed"]
    assert tr["reason"] == "question_over_length" and tr["full_question"] == long_q and tr["cut_chars"] == len(long_q) - len(gap["question"]) and tr["shown_chars"] == len(gap["question"])
    # the cut never changes the meaning: every number of the full question is either in the shown text or in the two rules named on the gap
    shown_numbers = set(re.findall(r"\d[\d,]*", gap["question"]))
    assert {"2000000", "5000000"} <= shown_numbers | {str(v) for r in (CODE_CAPEX, POLICY_CAPEX) for v in re.findall(r"\d+", json.dumps(r["condition"]))}
    unsafe = _suggest("See <b>here</b>: do they conflict?", rules, frozen)
    assert unsafe["gaps"] == [] and [r["reason"] for r in unsafe["rejected"]] == ["question_not_plain_text"]
    empty = _suggest("   ", rules, frozen)
    assert empty["gaps"] == [] and [r["reason"] for r in empty["rejected"]] == ["question_not_plain_text"]
    short = _suggest("Which CAPEX threshold is the business rule: 2,000,000 or 5,000,000 JPY?", rules, frozen)
    assert short["questions_trimmed"] == [] and short["gaps"][0]["question"].endswith("?")
    other_wording = {"kind": "conflicting_facts", "rule_ids": [POLICY_CAPEX["rule_id"], CODE_CAPEX["rule_id"]], "question": "Is it 2,000,000 or 5,000,000 JPY that sends a CAPEX request to Finance?", "evidence_refs": [POLICY_CAPEX["rule_id"]]}
    two = _suggest(long_q, rules, frozen, other_wording)
    assert len(two["gaps"]) == 1 and [r["reason"] for r in two["rejected"]] == ["duplicate"]                     # one disagreement, two wordings: one question
    only_last = "x" * (limit + 50) + ". Which one applies?"
    out3 = _suggest(only_last, rules, frozen)
    assert out3["gaps"][0]["question"].endswith("Which one applies?") and len(out3["gaps"][0]["question"]) <= limit


def test_references_copied_without_their_json_braces_are_recognised_as_the_offered_ones_and_stored_in_the_servers_form():
    """Live run JOURNEY-V7P-J2 (2026-10-22, raw object kept by C-07): the model named the right pair and copied the offered locators as
    `line_end: 44, line_start: 8, qualname: route_request, type: code_span` instead of the JSON string; the exact-string check refused the suggestion. The server now
    recognises the offered reference under that form and stores its own form; a reference that denotes nothing offered is still refused."""
    rules, frozen = _rules_and_frozen()
    code = dict(CODE_CAPEX, source_bindings=[{"artifact_id": "art-code", "revision_id": "1", "evidence_ref": json.dumps({"line_end": 44, "line_start": 8, "qualname": "route_request", "type": "code_span"})},
                                            {"artifact_id": "art-cfg", "revision_id": "1", "evidence_ref": "fixtures/harbor_public/config/approval.yaml#routing_rules.capex_finance_threshold_jpy"}])
    policy = dict(POLICY_CAPEX, source_bindings=[{"artifact_id": "art-policy", "revision_id": "1", "evidence_ref": json.dumps({"line_end": 29, "line_start": 28, "type": "line_span"})}])
    rules = {code["rule_id"]: code, policy["rule_id"]: policy}
    as_copied = ["fixtures/harbor_public/config/approval.yaml#routing_rules.capex_finance_threshold_jpy", "line_end: 44, line_start: 8, qualname: route_request, type: code_span", "line_end: 29, line_start: 28, type: line_span"]
    g = {"kind": "conflicting_facts", "rule_ids": [code["rule_id"], policy["rule_id"]], "question": "Do CAPEX purchases require referral to finance at 2,000,000 JPY or 5,000,000 JPY?", "evidence_refs": as_copied}
    out = bp.resolve_proposals({"status": "OK", "gaps": [g], "relations": [], "notes": []}, rules, [], [], limits=frozen["limits"], can_read_evidence=True)
    assert out["rejected"] == [] and len(out["gaps"]) == 1
    stored = set(out["gaps"][0]["evidence_refs"])
    assert stored <= bp.rule_refs(code) | bp.rule_refs(policy) and json.dumps({"line_end": 44, "line_start": 8, "qualname": "route_request", "type": "code_span"}) in stored   # the server's form, not the model's
    foreign = {**g, "evidence_refs": ["line_end: 99, line_start: 98, type: line_span"]}
    out2 = bp.resolve_proposals({"status": "OK", "gaps": [foreign], "relations": [], "notes": []}, rules, [], [], limits=frozen["limits"], can_read_evidence=True)
    assert out2["gaps"] == [] and [r["reason"] for r in out2["rejected"]] == ["evidence_ref_not_in_the_named_rules"]
    no_rights = bp.resolve_proposals({"status": "OK", "gaps": [g], "relations": [], "notes": []}, rules, [], [], limits=frozen["limits"], can_read_evidence=False)
    assert no_rights["gaps"] == [] and [r["reason"] for r in no_rights["rejected"]] == ["evidence_ref_not_in_the_named_rules"]                 # without evidence rights only rule ids may be cited


def test_the_asking_sentence_stays_whole_wherever_it_stands_replay_of_the_v7p_question():
    """The verbatim JOURNEY-V7P question (303 characters: the asking sentence FIRST, then a statement naming both rule ids). The first cut kept the last sentence and
    lost " JPY?" of the question; the asking sentence now stays whole wherever it stands and the statement is cut instead (2026-10-22, correction to C-06)."""
    rules, frozen = _rules_and_frozen()
    limit = frozen["limits"]["max_question_chars"]
    q = ("Do CAPEX purchases require referral to finance at 2,000,000 JPY or 5,000,000 JPY? Rule rule-cand-run-b963b2e9477e4eda836ccef3087e3caf-0006 sets the threshold at "
         "2,000,000 JPY for role-finance, while rule-cand-run-f68e03eadd6d410ba5801de1a90f6ac2-0002 sets it at 5,000,000 JPY for the Finance Controller.")
    assert len(q) == 303 and len(q) > limit
    shown, trimmed, reason = bp._question_text(q, limit)
    assert trimmed and reason == "question_over_length" and len(shown) <= limit
    assert shown.startswith("Do CAPEX purchases require referral to finance at 2,000,000 JPY or 5,000,000 JPY? Rule ") and shown.endswith("…")
    last = "Which one applies to a 3,000,000 JPY capital purchase?"
    q2 = "x" * 200 + ". " + "y" * 120 + ". " + last
    shown2, _, _ = bp._question_text(q2, limit)
    assert shown2.endswith(last) and len(shown2) <= limit and shown2.startswith("x")
    no_question = "a" * 200 + ". " + "b" * 200 + "."
    shown3, _, _ = bp._question_text(no_question, limit)
    assert shown3.endswith("b" * 200 + ".") and len(shown3) <= limit                                 # nothing asks: the last sentence is kept whole

