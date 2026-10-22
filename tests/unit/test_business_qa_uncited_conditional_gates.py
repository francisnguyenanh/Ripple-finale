"""An approval gate nobody cited that a fact the question does not state would decide is asked for, never answered (business_qa.uncited_conditional_gates, 2026-10-22,
JOURNEY-V4-J3 A4: a 4,000,000 JPY purchase whose category is not stated may also need Finance under the CAPEX rule). The decided side of a disagreement a person
settled is a settled rule; the losing side and both sides of an open disagreement are left to the conflict disclosure. Pure: no provider, no DB."""
from __future__ import annotations

import json
import pathlib

from core.change_intelligence.v2 import business_qa as qa

ROOT = pathlib.Path(__file__).resolve().parents[2]
FROZEN = json.loads((ROOT / "config" / "knowledge_qa.json").read_text(encoding="utf-8"))
RULES = {r["rule_id"]: r for r in json.loads((ROOT / "tests" / "fixtures" / "final_knowledge_loop" / "expected_knowledge.json").read_text(encoding="utf-8"))["observed_rules"]}
CATEGORY_Q = qa.FACT_QUESTIONS["request.category"]
MANAGER = {"text": "Purchases of 1,000,000 JPY or more need manager approval.", "refs": ["rule-standard-manager-review"], "basis": "observed"}
SUBSCRIPTION = "Who approves a software subscription of 4 million yen?"


def item(rule, **over):
    base = {"item_id": rule["rule_id"], "kind": "rule", "text": rule["title"], "authority": rule["authority"], "status": rule["status"], "title": rule["title"], "versions": [],
            "condition": rule["condition"], "scope": rule["scope"], "exceptions": rule["exceptions"], "outcome": rule["outcome"], "actor_ref": rule["actor_ref"], "implementation_status": "verified"}
    return {**base, **over}


def run(question, items, claims=(MANAGER,), counter=(), status="ANSWERED"):
    pack = {"query": question, "items": items, "counterevidence": list(counter), "observations": {"status": "OK", "results": []}, "pin": {"snapshot_id": "s", "snapshot_state": "ACCEPTED"},
            "retrieval": {}, "semantic": {}, "sufficiency": "unknown", "coverage": {}, "index": {}}
    model = {"status": status, "answer": " ".join(c["text"] for c in claims), "claims": list(claims), "uncertainty": "", "clarifying_questions": []}
    kept, dropped = qa.judge_claims(model["claims"], pack, question, FROZEN, can_read_evidence=True)
    return qa.finalize(model, kept, dropped, pack, FROZEN)


def test_a_gate_that_a_missing_category_would_decide_is_asked_for_and_the_answer_is_partial_not_a_claim():
    items = [item(RULES["rule-standard-manager-review"]), item(RULES["rule-capex-finance-config"])]
    out = run(SUBSCRIPTION, items)
    assert out["status"] == "PARTIAL" and "conditional_rule_not_cited" in out["reasons"] and CATEGORY_Q in out["clarifying_questions"]      # the process question is the pinned-default disclosure
    assert [g["rule_id"] for g in out["undecided_gates"]] == ["rule-capex-finance-config"] and out["undecided_gates"][0]["missing"] == ["request.category"]
    assert [c["refs"] for c in out["claims"]] == [["rule-standard-manager-review"]] and out["claims"][0]["applicability"] == "applies"            # the server asks; it does not answer
    assert "depends on a fact the question does not state" in out["answer"] and "1,000,000 JPY or more need manager approval" in out["answer"]
    below = run("Who has to sign off a 1.2 million yen standard purchase?", items)                                                                         # 1,200,000 never reaches 2,000,000
    assert below["status"] == "ANSWERED" and below["undecided_gates"] == [] and below["clarifying_questions"] == []
    stated = run("Who approves a CAPEX purchase of 4 million yen?", items)                                                                        # the category is stated: a server claim, no question
    assert stated["undecided_gates"] == [] and [c["refs"][0] for c in stated["claims"] if c.get("origin") == "server_applicability"] == ["rule-capex-finance-config"]


def test_the_decided_side_of_a_settled_disagreement_is_asked_about_the_losing_side_and_an_open_pair_are_not():
    policy, config = RULES["rule-capex-finance-policy"], RULES["rule-capex-finance-config"]
    decided = item(config, item_id="rule-capex-finance-config-target", authority="TARGET", status="CONFIRMED", decision_ids=["dec-1"], versions=[{"item_id": "rule-capex-finance-config"}])
    items = [item(RULES["rule-standard-manager-review"]), item(policy), decided]
    pair = {"kind": "conflicting_rule", "rule_id": "rule-capex-finance-policy", "with": "rule-capex-finance-config", "title": policy["title"], "text": "x", "authority": "OBSERVED"}
    settled = run(SUBSCRIPTION, items, counter=[dict(pair, from_question="gap-1", question_status="RESOLVED")])
    assert [g["rule_id"] for g in settled["undecided_gates"]] == ["rule-capex-finance-config-target"] and CATEGORY_Q in settled["clarifying_questions"]
    open_ = run(SUBSCRIPTION, items, counter=[dict(pair, from_question="gap-1", question_status="OPEN")])
    assert open_["undecided_gates"] == [] and "conditional_rule_not_cited" not in open_["reasons"]
    assert run(SUBSCRIPTION, items, counter=[pair])["undecided_gates"] == []                                                                      # a stored relation alone: undecided


def test_nothing_is_asked_without_a_kept_claim_for_an_amount_in_another_currency_or_for_a_fact_the_reader_cannot_read():
    items = [item(RULES["rule-standard-manager-review"]), item(RULES["rule-capex-finance-config"])]
    assert run(SUBSCRIPTION, items, claims=(), status="ABSTAINED")["undecided_gates"] == []
    usd = run("Does a purchase of 12,000 USD need manager approval?", items)
    assert usd["undecided_gates"] == [] and usd["status"] == "NEEDS_CLARIFICATION"
    odd = item(RULES["rule-capex-finance-config"], item_id="rule-odd", condition={"all": [{"field": "request.cost_centre", "op": "eq", "value": "X"}, {"field": "request.net_amount_jpy", "op": "gte", "value": 2000000, "unit": "JPY"}]})
    assert run(SUBSCRIPTION, [item(RULES["rule-standard-manager-review"]), odd])["undecided_gates"] == []
    assert run(SUBSCRIPTION, [item(RULES["rule-standard-manager-review"]), item(RULES["rule-high-risk-director"])])["undecided_gates"] == []        # no amount comparison: not about the stated case
    prose = item(RULES["rule-standard-manager-review"], item_id="rule-prose-manager", condition={"field": "purchase_request.amount", "op": "gte", "value": 1000000, "unit": "JPY"})
    out = run(SUBSCRIPTION, [prose, item(RULES["rule-capex-finance-config"])], claims=({**MANAGER, "refs": ["rule-prose-manager"]},))
    assert CATEGORY_Q in out["clarifying_questions"] and not any("amount" in x for x in out["clarifying_questions"])                                                                                          # never "what is the amount" for a stated amount
