"""The trial driver's judgement is a pure function of the stored result: a rule the MODEL did not emit and a rule the SERVER dropped are
reported apart, and nothing here calls a provider."""
from tools.judge_local import live_trial as T


def _c(cid, op, value, outcome):
    return {"candidate_id": cid, "operator": op, "value": value, "subject": "net_amount_jpy", "predicate": "amount", "outcome": outcome}


def test_both_branches_present_is_recognised():
    v = T.judge_emergency({"candidates": [_c("a", "<=", 0, "INVALID"), _c("b", ">=", 1000000, "NEEDS_MANAGER")], "rejected": [],
                           "merge_signature": T.MARKER})
    assert v["both_present"] and not v["server_dropped_a_rule"] and v["model_did_not_emit"] == [] and v["marker"] == T.MARKER


def test_a_rule_the_model_did_not_emit_is_not_blamed_on_the_server():
    v = T.judge_emergency({"candidates": [_c("a", "<=", 0, "INVALID")], "rejected": [], "merge_signature": T.MARKER})
    assert not v["both_present"] and v["model_did_not_emit"] == [">= 1000000"] and not v["server_dropped_a_rule"]


def test_a_rule_the_server_dropped_is_reported_as_such():
    v = T.judge_emergency({"candidates": [_c("a", "<=", 0, "INVALID")], "rejected": [{"index": 1, "reason": "operator_conflict_in_merge"}],
                           "dropped_for_conflict": [{"index": 1}]})
    assert v["server_dropped_a_rule"] and not v["both_present"]
