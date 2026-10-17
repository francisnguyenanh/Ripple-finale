"""Q&A in the accepted-target view when an observed rule lost a decided conflict (pure). Invariant: the losing rule may be cited as history or as what the sources say
when the question asks about it, but never presented as the current, decided rule. Diagnostic finding H-QA-06 (2026-10-17, non-independent): the model cited the policy rule
as what the sources say next to the decided rule; the evaluation label of H-QA-06 is unchanged and awaits the owner. These tests protect the invariant, not the label."""
from core.change_intelligence.v2 import business_qa as qa
from tests.unit.test_business_qa_applicability import FROZEN, RULES, item

QUESTION = "The policy document says CAPEX goes to the Finance Controller only from 5,000,000 JPY. Is that still the rule for a 3,000,000 JPY CAPEX request?"
TARGET = dict(item(RULES["rule-capex-finance-config"]), item_id="rule-capex-finance-config-target", authority="TARGET", status="CONFIRMED",
              versions=[{"item_id": "rule-capex-finance-config", "authority": "OBSERVED", "status": "VALIDATED"},
                        {"item_id": "rule-capex-finance-config-target", "authority": "TARGET", "status": "CONFIRMED"}])


def pack():
    return {"query": QUESTION, "items": [TARGET, item(RULES["rule-capex-finance-policy"])],
            "counterevidence": [{"kind": "conflicting_rule", "rule_id": "rule-capex-finance-policy", "with": "rule-capex-finance-config", "title": RULES["rule-capex-finance-policy"]["title"],
                                 "authority": "OBSERVED", "text": RULES["rule-capex-finance-policy"]["title"]}],
            "observations": {"status": "OK", "results": []}, "pin": {"snapshot_id": "s", "snapshot_state": "ACCEPTED", "view": "target"}, "retrieval": {}, "semantic": {},
            "sufficiency": "unknown", "coverage": {}, "index": {}}


def judge(claims):
    return qa.judge_claims(claims, pack(), QUESTION, FROZEN, can_read_evidence=False)


def test_the_losing_rule_cited_as_what_the_sources_say_is_kept_with_the_observed_label_only():
    kept, dropped = judge([{"text": "The policy document states that CAPEX of 5,000,000 JPY or more goes to the Finance Controller.", "refs": ["rule-capex-finance-policy"], "basis": "observed"},
                           {"text": "CAPEX of 2,000,000 JPY or more goes to Finance.", "refs": ["rule-capex-finance-config-target"], "basis": "target"}])
    by = {c["refs"][0]: c for c in kept}
    assert dropped == [] and by["rule-capex-finance-policy"]["authority_labels"] == ["what the sources say"]
    assert by["rule-capex-finance-config-target"]["authority_labels"] == ["decided by a person"]


def test_the_losing_rule_can_never_be_presented_as_the_decided_rule():
    kept, dropped = judge([{"text": "CAPEX of 5,000,000 JPY or more goes to the Finance Controller.", "refs": ["rule-capex-finance-policy"], "basis": "target"},
                           {"text": "CAPEX of 5,000,000 JPY or more goes to the Finance Controller.", "refs": ["rule-capex-finance-policy"], "basis": "decision"}])
    assert kept == [] and {d["reason"] for d in dropped} == {"basis_does_not_match_cited_authority"}


def test_a_conflict_with_only_one_side_cited_is_disclosed_in_the_final_answer():
    kept, dropped = judge([{"text": "CAPEX of 2,000,000 JPY or more goes to Finance.", "refs": ["rule-capex-finance-config-target"], "basis": "target"}])
    final = qa.finalize({"status": "ANSWERED", "answer": "Finance from 2,000,000 JPY.", "claims": [], "uncertainty": "", "clarifying_questions": []}, kept, dropped, pack(), FROZEN)
    assert final["conflicts"] and final["conflicts"][0]["rules"] == ["rule-capex-finance-config", "rule-capex-finance-policy"]
