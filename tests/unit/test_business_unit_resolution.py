"""A monetary amount the extraction left without a unit (live finding 2026-10-17: the freight threshold came back as 500000 with unit null) is recovered only from the evidence
quote that spells the currency next to that very number; otherwise the rule carries `unit:<field>` in `unknown_fields`, gets its own question, is excluded from numeric conflict
comparison and makes a typed decision INDETERMINATE. Pure: no provider, no DB."""
from core.change_intelligence.v2 import business_graph as bg
from core.change_intelligence.v2 import business_knowledge as bk
from core.change_intelligence.v2 import policy_decision as pd

QUOTE_WITH = "Purchases with a net goods amount of **500,000 JPY or greater** receive free freight delivery (0 JPY freight charge)."
QUOTE_WITHOUT = "Purchases with a net goods amount of 500,000 or greater receive free freight delivery."


def cand(quote, unit=None, condition=None, cid="cand-1"):
    return {"candidate_id": cid, "subject": "Purchases", "predicate": "receive free freight delivery", "operator": ">=", "value": 500000, "unit": unit, "scope": {"process": ["STANDARD"]},
            "modality": "must", "outcome": "free_freight", "exceptions": [], "applicability": "RECOGNIZED", "actor": "role-logistics", "condition": condition,
            "artifact_revision_id": "art-policy", "source_revision_id": "src-1", "model_run_id": "run-1",
            "evidence": [{"locator": "procurement-policy.md#4", "quote": quote, "artifact_revision_id": "art-policy"}]}


def test_the_unit_is_taken_from_the_quote_that_names_the_same_number_and_never_from_another_number():
    assert bk.unit_from_quotes(500000, [QUOTE_WITH]) == "JPY" and bk.unit_from_quotes("500,000", [QUOTE_WITH]) == "JPY"
    assert bk.unit_from_quotes(500000, [QUOTE_WITHOUT]) is None
    assert bk.unit_from_quotes(2000, [QUOTE_WITH]) is None                                           # 2,000 does not appear with a currency in this quote
    assert bk.unit_from_quotes(500000, ["ships at 500,000 yen"]) == "JPY" and bk.unit_from_quotes(500000, ["costs 500,000 USD"]) == "USD"


def test_a_rule_from_a_unitless_candidate_gets_the_unit_from_its_quote_or_a_visible_unknown_field():
    with_quote = bk.observed_rule_from_candidate(cand(QUOTE_WITH, condition={"field": "net_goods_value", "op": "gte", "value": 500000}))
    assert with_quote["condition"]["unit"] == "JPY" and with_quote["unknown_fields"] == []
    legacy = bk.observed_rule_from_candidate(cand(QUOTE_WITH))                                            # operator/value only: the legacy leaf is built, then the unit recovered
    assert legacy["condition"]["unit"] == "JPY" and legacy["unknown_fields"] == []
    without = bk.observed_rule_from_candidate(cand(QUOTE_WITHOUT, condition={"field": "net_goods_value", "op": "gte", "value": 500000}))
    assert "unit" not in without["condition"] and without["unknown_fields"] == ["unit:net_goods_value"]
    bk.validate_business_rule(without)                                                                  # still a valid rule: the gap is visible, the value is not normalised
    count = bk.observed_rule_from_candidate(cand(QUOTE_WITHOUT, condition={"field": "request.quantity", "op": "gt", "value": 0}))
    assert count["unknown_fields"] == []                                                                 # a count is not monetary: no unit expected
    big_legacy = bk.observed_rule_from_candidate(cand(QUOTE_WITHOUT))                                     # legacy leaf "purchases >= 500000" with no currency anywhere: flagged by size
    assert big_legacy["unknown_fields"] == ["unit:purchases"]


def test_an_unknown_unit_asks_its_own_question_is_not_compared_numerically_and_leaves_a_typed_decision_open():
    without = bk.observed_rule_from_candidate(cand(QUOTE_WITHOUT, condition={"field": "net_goods_value", "op": "gte", "value": 500000}))
    other = bk.observed_rule_from_candidate(cand("free freight from **400,000 JPY**", condition={"field": "net_goods_value", "op": "gte", "value": 400000}, cid="cand-2"))
    gaps = bg.derive_gaps([without, other])
    unit_gaps = [g for g in gaps if g["kind"] == "missing_condition" and "without a unit" in g["question"]]
    assert len(unit_gaps) == 1 and unit_gaps[0]["affected_rule_ids"] == [without["rule_id"]] and "net_goods_value" in unit_gaps[0]["question"]
    assert not [g for g in gaps if g["kind"] == "conflicting_facts"]                                      # 500000 (unit unknown) vs 400000 JPY is not a numeric conflict
    both_known = bk.observed_rule_from_candidate(cand(QUOTE_WITH, condition={"field": "net_goods_value", "op": "gte", "value": 500000}))
    assert [g for g in bg.derive_gaps([both_known, other]) if g["kind"] == "conflicting_facts"]            # with units on both sides the disagreement is detected as before
    decision = pd.decide([without], [], {"process": "STANDARD", "net_goods_value": 600000}, pin={})
    assert decision["decision"] == "INDETERMINATE" and any("rule_fields_unknown:unit:net_goods_value" in r for r in decision["indeterminate_reasons"])
