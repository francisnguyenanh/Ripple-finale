"""Contract v3 claim stance (sealed run FNL-EVAL-20261023): for a concrete case the model may say a cited rule does NOT apply; the server keeps that claim only when
its own applicability check agrees, drops a "no" on a rule that applies, and still drops a claim without a stance on a rule that does not apply. Pure."""
from tests.unit.test_business_qa_passage_binding_and_gates import CAPEX, MANAGER, run

BELOW = {"text": "A purchase of 650,000 JPY is below the 1,000,000 JPY manager threshold, so manager approval is not required.", "refs": ["rule-policy-manager"],
         "basis": "observed"}


def test_a_stated_non_application_the_check_confirms_is_kept_and_answers_the_case():
    final, kept, dropped = run("Does a 650,000 yen standard purchase need manager approval?", [MANAGER, CAPEX], [dict(BELOW, applies="no")])
    assert dropped == [] and kept[0]["applicability"] == "does_not_apply" and kept[0]["stance_verified"] is True
    assert final["status"] == "ANSWERED" and "no_valid_claim" not in final["reasons"]


def test_without_a_stance_the_non_applying_claim_is_dropped_as_before():
    final, kept, dropped = run("Does a 650,000 yen standard purchase need manager approval?", [MANAGER, CAPEX], [BELOW])
    assert kept == [] and dropped[0]["reason"] == "claim_not_applicable" and final["status"] == "ABSTAINED"


def test_a_no_on_a_rule_that_applies_contradicts_the_check_and_is_dropped():
    claim = dict(BELOW, text="Manager approval is not required for this purchase.", applies="no")
    final, kept, dropped = run("Does a 1,200,000 yen standard purchase need manager approval?", [MANAGER, CAPEX], [claim])
    assert kept == [] and dropped[0]["reason"] == "claim_contradicts_applicability"


def test_an_unknown_stance_value_is_a_shape_error():
    import pytest
    from core.change_intelligence.v2 import business_qa as qa
    from tests.unit.test_business_qa_passage_binding_and_gates import FROZEN
    bad = {"status": "ANSWERED", "answer": "x", "claims": [dict(BELOW, applies="maybe")], "uncertainty": "", "clarifying_questions": []}
    with pytest.raises(ValueError):
        qa.validate_model_object(bad, FROZEN)
