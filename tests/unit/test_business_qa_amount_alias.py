"""The case amount of a question stands in for an amount field a rule compares only on a basis: a `..._jpy` field, or an amount-named field the rule compares in JPY
(unit on the leaf, from the source). Found on live run JOURNEY-V4-J3 (A4a asked for an amount the question stated because the policy rule's field was
`purchase_request.amount`). Pure: no database, no model."""
from __future__ import annotations

from core.change_intelligence.v2 import business_qa as qa


def rule(field, unit=None, extra=None, scope=None):
    leaf = {"field": field, "op": "gte", "value": 1000000}
    if unit:
        leaf["unit"] = unit
    cond = {"all": [leaf, extra]} if extra else leaf
    return {"rule_id": "r", "kind": "rule", "condition": cond, "exceptions": [], "scope": scope or {}}


FACTS = {"request.net_amount_jpy": 1200000, "process": "STANDARD"}


def test_an_amount_named_field_compared_in_jpy_takes_the_case_amount_and_fields_that_are_not_the_case_amount_do_not():
    assert qa.case_facts_for_rule(rule("purchase_request.amount", "JPY"), FACTS)["purchase_request.amount"] == 1200000      # the policy's own words, in JPY
    assert qa.case_facts_for_rule(rule("order.net_goods_jpy"), FACTS)["order.net_goods_jpy"] == 1200000                    # a `_jpy` field
    for field, unit in (("purchase_request.amount", "USD"), ("purchase_request.amount", None), ("order.gross_amount", "JPY"), ("price_difference.amount", "JPY"),
                        ("freight.charge_jpy", None), ("supplier.risk", None)):
        assert field not in qa.case_facts_for_rule(rule(field, unit), FACTS), field                                        # another currency, no currency, gross, difference, charge
    assert qa.amount_field_stands_for_the_case("capital_expenditure.amount", "JPY") and not qa.amount_field_stands_for_the_case("capital_expenditure.amount", "jpy ") is False


def test_with_the_alias_the_manager_rule_applies_to_the_stated_amount_and_a_category_the_question_lacks_still_asks():
    universe = {}
    manager = rule("purchase_request.amount", "JPY")
    assert qa.rule_applicability(manager, FACTS, universe)["status"] == "applies"
    capex = rule("request.net_amount_jpy", "JPY", extra={"field": "request.category", "op": "eq", "value": "CAPEX"})
    out = qa.rule_applicability(capex, FACTS, universe)
    assert out["status"] == "conditional" and out["missing"] == ["request.category"]                                       # the category is not stated: a question, no guess
    usd = rule("purchase_request.amount", "USD")
    assert qa.rule_applicability(usd, FACTS, universe)["status"] == "conditional"                                          # another currency: never the case amount
    assert qa.rule_applicability(manager, FACTS, universe, alias_amount=False)["status"] == "conditional"                   # a rule nobody cited: only stated facts
