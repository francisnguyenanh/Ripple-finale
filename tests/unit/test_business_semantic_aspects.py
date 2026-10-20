"""EUI-03 semantic aspects of a rule change (pure): a changed operator, actor, unit, exception or scope is reported as its own typed aspect even when the number is the
same; values are the stored ones (no rounding, no locale rewrite, no derived percentage)."""
from __future__ import annotations

from core.change_intelligence.v2.business_draft import rule_diff, semantic_aspects

LEAF = {"field": "amount", "op": "gt", "value": 500000, "unit": "JPY"}
RULE = {"actor_ref": "role-manager", "condition": {"all": [LEAF, {"field": "category", "op": "in", "value": ["capex"]}]}, "exceptions": [{"text": "Emergency repairs"}],
        "scope": {"region": ["JP"]}, "title": "Manager approval"}


def aspects(after):
    return semantic_aspects(rule_diff(RULE, after))


def test_an_operator_change_with_the_same_threshold_is_visible_and_no_value_aspect_is_invented():
    after = {**RULE, "condition": {"all": [{**LEAF, "op": "gte"}, RULE["condition"]["all"][1]]}}
    out = aspects(after)
    assert out == [{"aspect": "operator", "label": "Comparison (amount)", "before": "more than", "after": "at least"}]


def test_threshold_and_unit_keep_the_stored_values_exactly():
    out = aspects({**RULE, "condition": {"all": [{**LEAF, "value": 1234567.5}, RULE["condition"]["all"][1]]}})
    assert out == [{"aspect": "threshold", "label": "Value (amount)", "before": "500000", "after": "1234567.5", "unit": "JPY"}]
    mixed = aspects({**RULE, "condition": {"all": [{**LEAF, "value": 4000, "unit": "USD"}, RULE["condition"]["all"][1]]}})
    assert [a["aspect"] for a in mixed] == ["threshold", "unit"] and mixed[0]["unit"] is None                  # mixed currency: no shared unit is claimed
    assert not any("%" in str(v) for a in mixed for v in a.values())


def test_actor_exception_scope_and_structure_changes_are_typed():
    out = aspects({**RULE, "actor_ref": "role-finance-director", "exceptions": [{"text": "Safety stops"}], "scope": {"region": ["JP", "VN"]}})
    assert [a["aspect"] for a in out] == ["actor", "exception_removed", "exception_added", "scope"]
    assert out[0]["before"] == "manager" and out[0]["after"] == "finance director"
    assert out[3] == {"aspect": "scope", "label": "Where it applies (region)", "before": "JP", "after": "JP, VN"}
    restructured = aspects({**RULE, "condition": LEAF})
    assert [a["aspect"] for a in restructured] == ["condition"]                                                # a different shape is never paired leaf by leaf
