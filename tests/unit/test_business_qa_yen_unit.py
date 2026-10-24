"""A rule extracted from "1,000,000 yen" compares an amount-named field in unit "yen": the amount the question states is that field's value (no question asked
for it); a field in another currency or a difference / charge field is still not the case amount."""
from core.change_intelligence.v2 import business_qa as qa


def test_yen_written_units_stand_for_jpy():
    for unit in ("JPY", "yen", "Yen", "円", "¥"):
        assert qa.amount_field_stands_for_the_case("purchase_request.amount", unit), unit
    assert not qa.amount_field_stands_for_the_case("purchase_request.amount", "USD")
    assert not qa.amount_field_stands_for_the_case("purchase_request.amount", None)
    assert not qa.amount_field_stands_for_the_case("freight.charge_amount", "yen")


def test_case_facts_fill_a_yen_amount_field_from_the_stated_amount():
    rule = {"condition": {"field": "purchase_request.amount", "op": "gte", "value": 1000000, "unit": "yen"}}
    facts = qa.case_facts_for_rule(rule, {"request.net_amount_jpy": 1200000})
    assert facts["purchase_request.amount"] == 1200000
