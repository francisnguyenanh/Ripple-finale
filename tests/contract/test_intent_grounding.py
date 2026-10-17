"""Deterministic, boundary-aware grounding of E1 hints."""
import pytest

from core.change_intelligence.intent_candidate import ground_scope, ground_target

VOCAB = {"approval_threshold": ("approval", "sign-off", "manager", "approve"),
         "emergency_purchase_ceiling": ("emergency", "fast-track")}


@pytest.mark.parametrize("text,scope,expected", [
    ("For small orders lower approval threshold", "all", ""),
    ("For all orders lower approval threshold", "all orders", "all orders"),
    ("For ALL orders", "all orders", "all orders"),
    ("For overall orders", "all", ""),
    ("For all orders", "UNKNOWN", ""),
    ("For all orders", "  ", ""),
])
def test_scope_matches_whole_words_only(text, scope, expected):
    assert ground_scope(text, scope) == expected


@pytest.mark.parametrize("text,hint,target", [
    ("lower approval threshold to 500000", "approval_threshold", "approval_threshold"),
    ("managers sign-off above 500000", "approval_threshold", "approval_threshold"),
    ("lower shipping fee to 500000", "approval_threshold", ""),
    ("raise the emergency ceiling", "approval_threshold", ""),
    ("approval threshold and emergency ceiling", "approval_threshold", ""),
    ("lower approval threshold", "invented_policy", ""),
    ("disapproved items", "approval_threshold", ""),
])
def test_target_must_be_the_only_rule_the_text_names(text, hint, target):
    assert ground_target(text, hint, VOCAB)[0] == target


def test_refusal_says_which_rule_the_text_named():
    target, reasons = ground_target("raise the emergency ceiling", "approval_threshold", VOCAB)
    assert target == "" and "emergency_purchase_ceiling" in reasons[0]


@pytest.mark.parametrize("text,scope,expected", [
    ("For small orders, not all orders, lower approval threshold", "all orders", ""),
    ("For small orders, not all orders, lower approval threshold", "small orders", "small orders"),
    ("For all orders except small orders lower approval threshold", "all orders", ""),
    ("Lower approval threshold to JPY 500,000 for all orders", "all orders", "all orders"),
])
def test_scope_in_a_negated_clause_is_refused(text, scope, expected):
    assert ground_scope(text, scope) == expected


@pytest.mark.parametrize("text,hint,target", [
    ("For all orders, do not change approval threshold; lower shipping fee", "approval_threshold", ""),
    ("keep the approval threshold, raise the emergency ceiling", "approval_threshold", ""),
    ("lower approval threshold, not the emergency ceiling", "approval_threshold", "approval_threshold"),
    ("lower approval threshold but never the emergency ceiling", "approval_threshold", "approval_threshold"),
    ("lower approval threshold, but do not lower approval threshold", "approval_threshold", ""),
])
def test_negated_or_conflicting_target_is_refused(text, hint, target):
    assert ground_target(text, hint, VOCAB)[0] == target


@pytest.mark.parametrize("text,hint,target", [
    ("lower shipping fee instead of approval threshold", "approval_threshold", ""),
    ("lower shipping fee rather than approval threshold", "approval_threshold", ""),
    ("lower shipping fee apart from approval threshold", "approval_threshold", ""),
    ("approval threshold stays untouched; lower shipping fee", "approval_threshold", ""),
    ("leave the approval threshold alone, lower shipping fee", "approval_threshold", ""),
    ("lower approval threshold instead of the emergency ceiling", "approval_threshold", "approval_threshold"),
    ("lower approval threshold rather than the emergency ceiling", "approval_threshold", "approval_threshold"),
])
def test_exclusion_connectives_exclude_what_follows(text, hint, target):
    assert ground_target(text, hint, VOCAB)[0] == target


@pytest.mark.parametrize("text,scope,expected", [
    ("For small orders instead of all orders, lower approval threshold", "all orders", ""),
    ("For small orders rather than all orders, lower approval threshold", "all orders", ""),
    ("lower approval threshold for all orders except small orders", "all orders", ""),
    ("lower approval threshold for all orders other than small orders", "all orders", ""),
])
def test_scope_in_a_clause_with_any_exclusion_is_refused(text, scope, expected):
    assert ground_scope(text, scope) == expected


# The value anchor: the requested rule sits next to the value the human gives.
EXCLUDED = [
    "lower shipping fee to JPY 500000; approval threshold stays the same",
    "lower shipping fee, the approval threshold remains as it is",
    "maintain the approval threshold and lower shipping fee",
    "preserve approval threshold, lower shipping fee",
    "freeze the approval threshold while lowering shipping fee",
    "lower shipping fee but no change to approval threshold",
    "don't touch approval threshold, lower shipping fee",
    "lower shipping fee except for approval threshold",
    "lower shipping fee, approval threshold should not change",
    "retain approval threshold; cut shipping fee to 500000",
    "approval threshold must stay; lower shipping fee to 500000",
    "Lower shipping fee to 500000. Approval threshold: forget it.",
    "lower shipping fee to JPY 500000. The approval threshold is fine.",
]
REQUESTED = [
    "For all orders lower approval threshold to JPY 500000",
    "lower the manager sign-off threshold to 500000",
    "lower approval threshold to JPY 500,000 for all orders",
    "For the approval threshold, lower it to JPY 500000",
    "lower approval threshold to 500000, not the emergency ceiling",
    "lower approval threshold to 500000 instead of the emergency ceiling",
    "Approval threshold: lower it to 500000",
]


@pytest.mark.parametrize("text", EXCLUDED)
def test_excluded_or_unvalued_rule_is_refused(text):
    assert ground_target(text, "approval_threshold", VOCAB)[0] == ""


@pytest.mark.parametrize("text", REQUESTED)
def test_requested_rule_is_still_grounded(text):
    assert ground_target(text, "approval_threshold", VOCAB)[0] == "approval_threshold"


def test_scope_excluding_a_rule_is_not_a_narrowed_scope():
    text = "For all orders lower approval threshold to JPY 500000 instead of the emergency ceiling"
    assert ground_scope(text, "all orders", VOCAB) == "all orders"
    assert ground_scope("lower approval threshold for all orders except small orders", "all orders", VOCAB) == ""


# The new value must belong to the action on the requested rule.
VALUE_ON_ANOTHER_SUBJECT = [
    "For all orders lower shipping fee to JPY 500000 and review approval threshold later",
    "For all orders lower shipping fee to JPY 500000 and discuss approval threshold tomorrow",
    "For all orders lower shipping fee to JPY 500000, with approval threshold to be reviewed later",
    "lower shipping fee to 500000 while we look at the approval threshold",
    "lower shipping fee to 500000 then check approval threshold",
    "approval threshold needs a look; meanwhile set shipping fee to 500000",
    "lower shipping fee to 500000 and we should review approval threshold later",
    "lower shipping fee to 500000 so that the approval threshold can be discussed",
    "lower shipping fee to 500000 before revisiting approval threshold",
    "lower shipping fee to 500000 after approval threshold review",
    "lower shipping fee to JPY 500000 (approval threshold next week)",
    "lower shipping fee to 500000 plus review approval threshold",
    "lower the shipping fee, currently tied to the approval threshold, to 500000",
]
VALUE_ON_THE_RULE = [
    "lower the approval threshold and set it to 500000",
    "For the approval threshold, lower it to JPY 500000",
    "Approval threshold: lower it to 500000",
    "lower approval threshold for small and medium orders to 500000",
    "lower approval threshold to 500000 and review the emergency ceiling later",
    "For orders over 100000 lower approval threshold to 500000",
    "approval threshold 500000 for all orders",
]


@pytest.mark.parametrize("text", VALUE_ON_ANOTHER_SUBJECT)
def test_value_on_another_subject_is_refused(text):
    assert ground_target(text, "approval_threshold", VOCAB)[0] == ""


@pytest.mark.parametrize("text", VALUE_ON_THE_RULE + REQUESTED)
def test_value_on_the_requested_rule_is_grounded(text):
    assert ground_target(text, "approval_threshold", VOCAB)[0] == "approval_threshold"
