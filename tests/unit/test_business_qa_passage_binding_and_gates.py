"""Two findings of live run JOURNEY-V5-J3 (2026-10-22), fixed after the run, never during it:
(a) a claim that cites only a source passage is read through the ONE rule bound to that passage (stored binding: same artifact, overlapping lines), so the case can
    be checked against it; two bound rules or none → nothing is attached (A4a: the model cited the policy passage instead of the rule read from it);
(b) an approval gate is recognised from the rule itself — the modelled kind, approval words in its outcome, or a code value with a known actor — because extracted
    rules never carry the fixture's `review_required` kind, so the uncited-gate checks were inert on real knowledge (A4b: no question about the category).
Pure: no provider, no DB."""
from __future__ import annotations

import json
import pathlib

from core.change_intelligence.v2 import business_index as bi
from core.change_intelligence.v2 import business_qa as qa

ROOT = pathlib.Path(__file__).resolve().parents[2]
FROZEN = json.loads((ROOT / "config" / "knowledge_qa.json").read_text(encoding="utf-8"))
ART = "art-policy"
SPAN = json.dumps({"type": "line_span", "line_start": 20, "line_end": 21})


def rule(rid, title, condition, outcome, actor="role-manager", bindings=(("art-policy", SPAN),), authority="OBSERVED", status="VALIDATED", **over):
    r = {"rule_id": rid, "title": title, "authority": authority, "status": status, "actor_ref": actor, "condition": condition, "outcome": outcome, "exceptions": [], "scope": {},
         "implementation_status": "not_required", "unknown_fields": [], "provenance": [], "concept_ids": [],
         "source_bindings": [{"artifact_id": a, "revision_id": "1", "evidence_ref": e} for a, e in bindings]}
    return {**r, **over}


MANAGER = rule("rule-policy-manager", "Purchase requests - require approval", {"field": "purchase_request.amount", "op": "gte", "value": 1000000, "unit": "JPY"},
               {"kind": "unspecified", "predicate": "", "stated_text": "Approval from the requesting department's manager before a purchase order may be raised"})
CAPEX = rule("rule-code-capex", "request.net_amount_jpy - must be greater than or equal to",
             {"all": [{"field": "request.category", "op": "eq", "value": "CAPEX"}, {"field": "request.net_amount_jpy", "op": "gte", "value": 2000000, "unit": "JPY"}]},
             {"kind": "NEEDS_FINANCE", "predicate": ""}, actor="role-finance", bindings=(("art-code", json.dumps({"type": "code_span", "function": "route_request"})),))
PASSAGE = {"observation_id": "obs-art-policy-1.0.0-000007", "artifact_id": ART, "locator": {"type": "line_span", "line_start": 19, "line_end": 22},
           "raw_value": "Purchase requests of JPY 1,000,000 or more require approval from the requesting department's manager before a purchase order may be raised."}


def pack(question, rules, observations=(PASSAGE,)):
    return {"query": question, "items": [bi.rule_item(r) for r in rules], "counterevidence": [], "observations": {"status": "READY", "results": list(observations)},
            "pin": {"snapshot_id": "s", "snapshot_state": "ACCEPTED"}, "retrieval": {}, "semantic": {}, "sufficiency": "unknown", "coverage": {}, "index": {}}


def run(question, rules, claims, observations=(PASSAGE,), evidence=True):
    p = pack(question, rules, observations)
    model = {"status": "ANSWERED", "answer": " ".join(c["text"] for c in claims), "claims": list(claims), "uncertainty": "", "clarifying_questions": []}
    kept, dropped = qa.judge_claims(model["claims"], p, question, FROZEN, can_read_evidence=evidence)
    return qa.finalize(model, kept, dropped, p, FROZEN), kept, dropped


PASSAGE_CLAIM = {"text": "Purchase requests of JPY 1,000,000 or more require approval from the requesting department's manager.", "refs": ["obs-art-policy-1.0.0-000007"], "basis": "observed"}


def test_a_claim_citing_only_the_passage_is_read_through_the_one_rule_bound_to_it_and_checked_against_the_case():
    final, kept, dropped = run("Who has to sign off a 1.2 million yen standard purchase?", [MANAGER, CAPEX], [PASSAGE_CLAIM])
    assert dropped == [] and kept[0]["refs"] == ["obs-art-policy-1.0.0-000007", "rule-policy-manager"] and kept[0]["refs_from_passage"] == ["rule-policy-manager"]
    assert kept[0]["applicability"] == "applies" and final["status"] == "ANSWERED" and final["clarifying_questions"] == []       # 1,200,000 reaches the manager rule only
    final, kept, _ = run("Who approves a software subscription of 4 million yen?", [MANAGER, CAPEX], [PASSAGE_CLAIM])
    assert kept[0]["applicability"] == "applies" and final["status"] == "PARTIAL" and "conditional_rule_not_cited" in final["reasons"]
    assert qa.FACT_QUESTIONS["request.category"] in final["clarifying_questions"] and [g["rule_id"] for g in final["undecided_gates"]] == ["rule-code-capex"]


def test_nothing_is_attached_without_evidence_rights_for_two_bound_rules_other_lines_or_a_claim_that_already_cites_a_rule():
    final, kept, _ = run("Who has to sign off a 1.2 million yen standard purchase?", [MANAGER, CAPEX], [PASSAGE_CLAIM], evidence=False)
    assert kept == [] and final["status"] == "ABSTAINED"                                                                        # the passage is not readable: dropped as before
    twin = rule("rule-policy-twin", "Requests - are approved", {"field": "purchase_request.amount", "op": "gte", "value": 1000000, "unit": "JPY"}, MANAGER["outcome"])
    _, kept, _ = run("Who has to sign off a 1.2 million yen standard purchase?", [MANAGER, twin], [PASSAGE_CLAIM])
    assert kept[0]["refs"] == ["obs-art-policy-1.0.0-000007"] and "refs_from_passage" not in kept[0] and kept[0]["applicability"] == "not_evaluated"   # two rules on those lines: nothing guessed
    far = dict(PASSAGE, observation_id="obs-art-policy-1.0.0-000012", locator={"type": "line_span", "line_start": 40, "line_end": 41})
    _, kept, _ = run("Who has to sign off a 1.2 million yen standard purchase?", [MANAGER], [dict(PASSAGE_CLAIM, refs=["obs-art-policy-1.0.0-000012"])], observations=(far,))
    assert "refs_from_passage" not in kept[0] and kept[0]["applicability"] == "not_evaluated"
    both = dict(PASSAGE_CLAIM, refs=["obs-art-policy-1.0.0-000007", "rule-policy-manager"])
    _, kept, _ = run("Who has to sign off a 1.2 million yen standard purchase?", [MANAGER, CAPEX], [both])
    assert kept[0]["refs"] == both["refs"] and "refs_from_passage" not in kept[0]


def test_an_approval_gate_is_read_from_the_rule_itself_never_from_a_route_name_alone():
    assert bi.rule_is_gate(MANAGER) and bi.rule_is_gate(CAPEX)                                                                  # approval words; code value + known actor
    assert bi.rule_is_gate(dict(MANAGER, outcome={"kind": "review_required", "predicate": ""}))
    assert not bi.rule_is_gate(dict(CAPEX, actor_ref=None))                                                                       # NEEDS_FINANCE with nobody named: not a gate
    assert bi.rule_is_gate(dict(CAPEX, outcome={"kind": "RouteType.NEEDS_FINANCE", "predicate": ""}))                            # as the code wrote it (live snapshot, JOURNEY-V5 replay)
    assert not bi.rule_is_gate(dict(CAPEX, outcome={"kind": "RouteType.STANDARD", "predicate": ""}))
    assert not bi.rule_is_gate(dict(MANAGER, outcome={"kind": "Referred to the Finance Controller regardless of the requesting department", "predicate": "is referred"}))   # a referral is not an approval word: a known limit
    assert not bi.rule_is_gate(dict(MANAGER, outcome={"kind": "unspecified", "predicate": "", "stated_text": "handled by the system"}))
    assert not bi.rule_is_gate(dict(MANAGER, outcome={"kind": "unspecified", "predicate": "", "stated_text": "reviewed by IT governance each quarter"}))   # a periodic review
    assert not bi.rule_is_gate(dict(MANAGER, outcome={"kind": "ROUTE_STANDARD", "predicate": ""}))                              # a route name alone
    item = bi.rule_item(MANAGER)
    assert item["gate"] is True and item["bindings"] == [{"artifact_id": ART, "evidence_ref": SPAN}]
