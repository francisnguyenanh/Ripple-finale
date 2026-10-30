"""Request-assistant context/4 (R12): facts the server computes or states exactly, so the model does not have to infer them. Pure: the real `condition_change`,
`task_facts`, `position_fact`, `decision_status_fact` and `pack`; no DB, no model. Each class comes from a live A123 miss on the seen sets:
  * which cases a threshold change moves (H4-09: "orders of 500,000 or more would lose free delivery" for a 500,000 -> 600,000 change);
  * what the asking person owes (H4-03: an approver's decision given as the requester's deliverable);
  * which decisions are in effect and what follows the current step (H4-14: "the record does not state what happens after" while it did, or could).
Before context/4 none of these facts existed (each assertion on them fails on context/3). The real route / worker path is in test_request_assistant_jobs.py."""
from __future__ import annotations

import pytest

from core.change_intelligence.v2 import business_draft as bd
from core.change_intelligence.v2 import request_assistant as ra
from infra.adapters import v2_request_tasks as T

O = T._obligation


def leaf(op, value, field="order_net_goods_jpy", unit="JPY"):
    return {"field": field, "op": op, "value": value, "unit": unit}


def words(before, after):
    c = bd.condition_change(before, after)
    return None if c is None else bd.condition_change_text(c)


# ---- A. which cases a threshold change moves -----------------------------------------------------------------------------------------------------------------

def test_a_raised_lower_bound_moves_only_the_cases_between_the_old_and_the_new_value():
    t = words(leaf("gte", 500000), leaf("gte", 600000))
    assert t == ("by order net goods jpy, cases that would stop meeting the condition: at least 500000 and less than 600000 JPY; would start meeting it: none; "
                 "would still meet it: at least 600000 JPY; would still not meet it: less than 500000 JPY")    # nothing newly qualifies


@pytest.mark.parametrize("before, after, moved", [
    (leaf("gte", 1000000, "request_net_amount_jpy"), leaf("gte", 750000, "request_net_amount_jpy"),
     "would start meeting it: at least 750000 and less than 1000000 JPY"),                      # lowered (another domain, other numbers)
    (leaf("gte", 500000), leaf("gt", 500000), "stop meeting the condition: exactly 500000 JPY;"),  # the boundary itself
    (leaf("gt", 500000), leaf("gte", 500000), "would start meeting it: exactly 500000 JPY;"),
    (leaf("lt", 500000), leaf("lt", 600000), "would start meeting it: at least 500000 and less than 600000 JPY;"),       # upper bound, open
    (leaf("lte", 10, "days_open", None), leaf("lte", 7, "days_open", None), "by days open, cases that would stop meeting the condition: more than 7 and at most 10;"),
    (leaf("gte", 1.5, "ratio", None), leaf("gte", 2.25, "ratio", None), "stop meeting the condition: at least 1.5 and less than 2.25;"),
])
def test_lowered_raised_open_closed_and_boundary_changes_are_computed_exactly(before, after, moved):
    assert moved in words(before, after)


def test_a_combined_condition_is_computed_within_its_unchanged_parts_and_says_so():
    risk = {"field": "supplier_risk", "op": "eq", "value": "HIGH"}
    t = words({"all": [leaf("gte", 1000000, "amount"), risk]}, {"all": [leaf("gte", 750000, "amount"), risk]})
    assert "would start meeting it: at least 750000 and less than 1000000 JPY" in t and t.endswith("; only among cases that also meet: supplier risk is HIGH")


@pytest.mark.parametrize("before, after, reason", [
    ({"any": [leaf("gte", 1)]}, {"any": [leaf("gte", 2)]}, "either-or"),
    ({"not": leaf("gte", 1)}, {"not": leaf("gte", 2)}, "negated"),
    (leaf("gte", 5), leaf("lte", 5), "same side"),
    (leaf("gte", 5), leaf("gte", 5, unit="USD"), "unit changed"),
    (leaf("gte", 5), leaf("gte", 5, field="other"), "measured quantity"),
    (leaf("eq", 5), leaf("eq", 6), "one-sided bound"),
    (leaf("gte", "high"), leaf("gte", "higher"), "not a number"),
    ({"all": [leaf("gte", 1), leaf("lt", 9)]}, {"all": [leaf("gte", 2), leaf("lt", 8)]}, "more than one part"),
    (leaf("gte", 1), {"all": [leaf("gte", 1), leaf("lt", 9)]}, "changed in form"),
])
def test_a_change_that_cannot_be_computed_exactly_says_why_and_computes_nothing(before, after, reason):
    c = bd.condition_change(before, after)
    assert c["computed"] is False and reason in c["reason"]
    t = bd.condition_change_text(c)
    assert t.startswith("which cases the change moves is not computed (") and t.endswith("); this does not mean no case is affected")   # not computed != no effect


def test_the_longest_computed_text_still_fits_one_whole_fact():
    long_measure = "net_goods_amount_after_discounts_and_before_tax_jpy"
    t = words(leaf("gte", 123456789.25, long_measure), leaf("gt", 987654321.75, long_measure))
    change = bd.condition_change(leaf("gte", 123456789.25, long_measure), leaf("gt", 987654321.75, long_measure))
    full = ra.computed_range_text("x" * 120, change)                                              # a long rule title: named by its before / after fact instead
    assert len(full) <= ra.MAX_FACT_CHARS and full.startswith("Computed by the server for the rule of the Before / After fact just before this one, by ")
    assert full.endswith("would still not meet it: less than 123456789.25 JPY.") and t in full
    ordinary = ra.computed_range_text("Purchases of 500,000 JPY or more receive free freight delivery", bd.condition_change(leaf("gte", 500000), leaf("gte", 600000)))
    assert ordinary.startswith("Computed by the server for the rule 'Purchases of 500,000 JPY or more receive free freight delivery', by order net goods jpy")
    scoped = ra.computed_range_text("Purchases of 500,000 JPY or more receive free freight delivery", bd.condition_change(leaf("gte", 500000), leaf("gte", 600000)),
                                    "process: STANDARD, unchanged")
    assert scoped.startswith("Computed by the server for the rule 'Purchases of 500,000 JPY or more receive free freight delivery' (applies to: process: STANDARD, "
                             "unchanged), by order net goods jpy") and len(scoped) <= ra.MAX_FACT_CHARS          # context/6: the cases that move, with the scope
    long_scoped = ra.computed_range_text("x" * 120, change, "process: STANDARD, EMERGENCY, unchanged")
    assert len(long_scoped) <= ra.MAX_FACT_CHARS and "(applies to: process: STANDARD, EMERGENCY, unchanged)" in long_scoped   # the scope is kept; the title gives way
    risk = [{"field": f"other_condition_{i}", "op": "eq", "value": "SOMETHING_LONG_ENOUGH"} for i in range(6)]
    combined = bd.condition_change({"all": [leaf("gte", 1, "amount")] + risk}, {"all": [leaf("gte", 2, "amount")] + risk})
    text = ra.computed_range_text("A rule", combined)
    assert len(text) <= ra.MAX_FACT_CHARS and text.endswith("only among cases that also meet: the rule's other unchanged conditions.")


def test_an_unchanged_condition_gives_no_fact():
    assert bd.condition_change(leaf("gte", 5), leaf("gte", 5)) is None


# ---- B. what the asking person owes ---------------------------------------------------------------------------------------------------------------------------

def view(obligations=(), actions=(), states=None, family="business_change", spec=T.BUSINESS_STEPS):
    states = states or {"understand": "done", "draft": "done", "clarify": "done", "knowledge_decision": "current"}
    return T._dto(family, "x", None, "t", {"code": "C", "label": "L"}, list(obligations), [], [], list(actions), T._steps(spec, states), {}, [], [])


def facts(tv, kind=None, key=None):
    return [f for f in ra.task_facts(tv) if (kind is None or f["kind"] == kind) and (key is None or f["key"] == key)]


def test_a_reader_whose_steps_are_all_held_by_others_is_told_nothing_is_owed_and_another_holders_step_is_not_listed_as_theirs():
    approvers = O("decide_knowledge", "Decide the knowledge change", holder="role_pool", holder_label="an approver", eligible_me=False)
    tv = view([approvers], [{"action": "clarify", "label": "Answer an open question of the draft", "allowed": True, "serves": []}])
    [p] = facts(tv, "position")
    assert p["text"] == ("Asked of you on this request now: nothing (no current step of this request is yours; this covers this request now, not later steps). "
                         "You may also, though not asked now: Answer an open question of the draft. Decisions and actions are made only with this request's own "
                         "controls; the assistant never acts.")                                                    # scoped: this request, now; whole (<= 400)
    assert "Decide the knowledge change" not in p["text"] and p["data"] == {"asked": [], "optional": ["Answer an open question of the draft"]} and p["tier"] == "mandatory"


def test_a_reader_who_holds_the_current_step_is_told_that_step_and_a_future_step_of_theirs_is_not_asked_now():
    mine = O("decide_knowledge", "Decide the knowledge change", holder="approver", holder_label="you", eligible_me=True, assigned_me=True)
    later = O("commit_knowledge", "Commit the approved knowledge change", holder="knowledge_owner", holder_label="you", eligible_me=True, assigned_me=True, state="future")
    [p] = facts(view([mine, later]), "position")
    assert p["text"] == ("Asked of you on this request now: Decide the knowledge change. Decisions and actions are made only with this request's own controls; "
                         "the assistant never acts.") and p["data"]["asked"] == ["Decide the knowledge change"]
    many = [{"action": f"a{i}", "label": f"A long optional action label number {i} for this request", "allowed": True, "serves": []} for i in range(8)]
    [q] = facts(view([], many), "position")
    assert len(q["text"]) <= ra.MAX_FACT_CHARS and "You may also take 8 other action(s), not asked now (see the action facts)." in q["text"] and not q.get("truncated")


def test_the_steps_still_to_come_are_stated_in_order_and_none_when_none_remain():
    [s] = facts(view(), "step", "steps:later")
    assert s["text"].startswith("Steps expected after the current one if the request goes ahead, in the workflow's order (a decision can still end it or send it back): ")
    assert set(s["data"]["phases"].values()) == {"future"}                                          # expected and conditional, not a promise
    done = {c: "done" for c, _ in T.BUSINESS_STEPS}
    assert facts(view(states=done), "step", "steps:later") == []
    intent = view(states={"conversation": "done", "intent": "done", "draft": "current"}, family="intent_flow", spec=T.INTENT_STEPS)
    assert facts(intent, "step", "steps:later")[0]["text"].endswith("send it back): Proposal prepared; Check that must hold after the change; Review run; "
                                                                  "Approval bundle; Approver's decision; Applied; Verification and knowledge update; Result decided by a person.")


def test_a_later_step_does_not_count_as_running_or_stopped_for_the_status_check():
    p = ra.pack({"family": "business_change", "id": "x"}, "now", ra.task_facts(view()), auth={})
    assert not ra.status_problem("The request is not stalled; it waits for the knowledge decision.", p["facts"])
    assert ra.status_problem("The request is stalled.", p["facts"])


# ---- C. which decisions are in effect ------------------------------------------------------------------------------------------------------------------------

def test_a_superseded_decision_is_out_of_effect_with_its_reason_and_nothing_pending_and_a_current_one_is_in_effect():
    old = {"kind": "knowledge", "action": "request_changes", "current": False, "stale_reason": "the draft was revised after this decision", "reason": "State the scope."}
    now = {"kind": "knowledge", "action": "approve", "current": True, "reason": "Checked against the policy."}
    t = ra.decision_status_fact([old])["text"]
    assert t == ("Decisions in effect now: none. Earlier decisions no longer in effect: knowledge decision: changes requested (the draft was revised after this "
                 "decision); nothing is pending from it.")
    t2 = ra.decision_status_fact([now, old])["text"]
    assert t2.startswith("Decisions in effect now: knowledge decision: approved (reason given: Checked against the policy.).") and "changes requested" in t2
    assert ra.decision_status_fact([now])["text"].endswith("No earlier decision is out of effect.")
    assert ra.decision_status_fact([])["text"] == "No decision has been recorded on this request yet."
    assert ra.decision_status_fact([old])["kind"] not in ra.CAUSE_FACTS                       # a status summary is not a reason a cause may cite


def test_context_4_keeps_the_history_facts_and_names_its_version():
    assert ra.CONTEXT_VERSION == "request-assistant-context/14" and {"position", "decision_status"} <= set(ra.KINDS)


def test_the_rule_parts_are_labelled_so_who_acts_under_the_rule_is_not_read_as_its_scope():
    """R14 (A126 H3-09 / H4-09: "actor: logistics" became "for logistics orders"; the scope is the STANDARD process). Other actors, scopes and domains alike."""
    free = {"actor_ref": "role-logistics", "condition": leaf("gte", 500000), "exceptions": [], "scope": {"process": ["STANDARD"]}}
    assert bd.rule_words(free) == ("who acts under the rule: logistics | applies when: order net goods jpy at least 500000 JPY | exceptions: none | "
                                   "applies to (scope): process: STANDARD")
    supplier = {"actor_ref": "role-finance-controller", "condition": {"field": "supplier_risk", "op": "eq", "value": "HIGH"},
                "exceptions": [{"text": "framework contracts"}], "scope": {"process": ["STANDARD", "EMERGENCY"]}}
    assert bd.rule_words(supplier) == ("who acts under the rule: finance controller | applies when: supplier risk is HIGH | exceptions: framework contracts | "
                                       "applies to (scope): process: STANDARD, EMERGENCY")
    assert bd.rule_words({"actor_ref": None, "condition": None, "scope": {}}) == ("who acts under the rule: unknown | applies when: condition unknown | "
                                                                                    "exceptions: none | applies to (scope): no scope stated")    # nothing invented
