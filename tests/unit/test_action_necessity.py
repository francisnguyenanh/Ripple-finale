# -*- coding: utf-8 -*-
"""`required` is a claim, and it has to be earned.

The metric this protects is `unnecessary-change rate` at P9. A planner that
marks everything required scores badly there, but it also fails at something
simpler: it has told the reviewer nothing. If every line says "required", the
word carries no information and the reviewer reads none of them.
"""

from __future__ import annotations

import pytest

from core.change_intelligence.action_plan import (
    ACTION_KINDS,
    Action,
    NECESSITY,
    OPTIONAL,
    PERFORMER,
    RECOMMENDED,
    REQUIRED,
    UNSUPPORTED,
    UnjustifiedAction,
    communication_actions,
    plan,
    render,
)
from core.change_intelligence.impact import ImpactNode, ImpactResult

TARGET = "approval.threshold"


def _node(identifier, kind, tier, evidence="quote:1"):
    return ImpactNode(identifier=identifier, kind=kind, tier=tier,
                      reason=f"{identifier} is involved", evidence=evidence)


def _impact(*nodes):
    return ImpactResult(target=TARGET, nodes=tuple(nodes))


def _plan(*nodes, **kwargs):
    return plan(target=TARGET, impact=_impact(*nodes),
                current_value=1000, proposed_value=500, **kwargs)


# --- the four levels ---------------------------------------------------------


def test_there_are_exactly_four_levels_of_necessity():
    assert NECESSITY == ("required", "recommended", "optional", "unsupported")


def test_an_action_marked_required_with_no_evidence_is_refused():
    with pytest.raises(UnjustifiedAction, match="no impact evidence"):
        Action(kind="CODE", target="a.py", description="d",
               necessity=REQUIRED, performer="human", impact_evidence=(),
               reason="r")


def test_an_action_with_no_reason_is_refused():
    with pytest.raises(UnjustifiedAction, match="no reason"):
        Action(kind="CODE", target="a.py", description="d",
               necessity=OPTIONAL, performer="human", reason="")


def test_recommended_and_optional_do_not_need_evidence():
    """Only the strong claim is gated. The weak ones are already weak."""
    for necessity in (RECOMMENDED, OPTIONAL, UNSUPPORTED):
        Action(kind="DOC", target="d.md", description="d",
               necessity=necessity, performer="system", reason="r")


def test_a_kind_outside_the_seven_is_refused():
    with pytest.raises(UnjustifiedAction):
        Action(kind="REFACTOR", target="a.py", description="d",
               necessity=OPTIONAL, performer="human", reason="r")


def test_there_are_exactly_seven_kinds():
    assert len(ACTION_KINDS) == 7
    assert set(PERFORMER) == set(ACTION_KINDS)


# --- necessity follows the tier ----------------------------------------------


def test_a_direct_operative_artifact_is_required():
    plan_ = _plan(_node("workflow/routes.yaml", "workflow_config", "DIRECT"))
    assert plan_.actions[0].necessity == REQUIRED
    assert plan_.actions[0].kind == "WORKFLOW"


def test_a_document_that_describes_the_rule_is_recommended_not_required():
    """It must be updated. It is not where the change is made."""
    plan_ = _plan(_node("docs/policy.md", "document", "INDIRECT"))
    assert plan_.actions[0].necessity == RECOMMENDED
    assert plan_.actions[0].kind == "DOC"


def test_an_unknown_tier_becomes_unsupported_not_a_guess():
    plan_ = _plan(_node("docs/unclear.md", "document", "UNKNOWN"))
    assert plan_.actions[0].necessity == UNSUPPORTED


def test_an_unsupported_item_is_listed_rather_than_dropped():
    plan_ = _plan(_node("docs/unclear.md", "document", "UNKNOWN"))
    assert plan_.actions
    assert any("for a person to decide" in note for note in plan_.notes)


def test_a_direct_node_with_no_evidence_drops_a_step_rather_than_claiming():
    plan_ = _plan(_node("workflow/routes.yaml", "workflow_config", "DIRECT",
                        evidence=""))
    assert plan_.actions[0].necessity == RECOMMENDED, (
        "required was asserted without a quotable line behind it")


def test_every_required_action_carries_impact_evidence():
    plan_ = _plan(
        _node("workflow/routes.yaml", "workflow_config", "DIRECT"),
        _node("data/rules.xlsx", "spreadsheet", "DIRECT"),
        _node("docs/policy.md", "document", "INDIRECT"))
    assert plan_.of_necessity(REQUIRED)
    assert all(a.impact_evidence for a in plan_.of_necessity(REQUIRED))


# --- not everything is required ----------------------------------------------


def test_a_plan_is_not_all_required():
    plan_ = _plan(
        _node("workflow/routes.yaml", "workflow_config", "DIRECT"),
        _node("docs/policy.md", "document", "INDIRECT"),
        _node("docs/guide.md", "document", "INDIRECT"))
    levels = {a.necessity for a in plan_.actions}
    assert len(levels) > 1, (
        "every action came out at the same necessity; the field then "
        "carries no information and a reviewer reads none of them")


def test_the_planner_does_not_produce_all_seven_kinds_for_one_change():
    plan_ = _plan(_node("workflow/routes.yaml", "workflow_config", "DIRECT"))
    assert len(plan_.kinds) < 7


def test_an_impact_set_of_nothing_produces_no_actions():
    assert plan(target=TARGET, impact=_impact()).is_empty


def test_one_artifact_produces_one_action_not_one_per_representation():
    plan_ = _plan(
        _node("docs/policy.md", "document", "INDIRECT"),
        _node("docs/policy.md", "document", "INDIRECT"))
    assert len(plan_.of_kind("DOC")) == 1


# --- ordering and grouping ---------------------------------------------------


def test_actions_are_grouped_by_necessity_strongest_first():
    plan_ = _plan(
        _node("docs/unclear.md", "document", "UNKNOWN"),
        _node("docs/policy.md", "document", "INDIRECT"),
        _node("workflow/routes.yaml", "workflow_config", "DIRECT"))
    assert [necessity for necessity, _ in plan_.grouped] == [
        REQUIRED, RECOMMENDED, UNSUPPORTED]


def test_required_appears_before_recommended_in_the_rendering():
    plan_ = _plan(
        _node("docs/policy.md", "document", "INDIRECT"),
        _node("workflow/routes.yaml", "workflow_config", "DIRECT"))
    text = render(plan_)
    assert text.index("REQUIRED") < text.index("RECOMMENDED")


def test_the_rendering_is_deterministic():
    nodes = (_node("workflow/routes.yaml", "workflow_config", "DIRECT"),
             _node("docs/policy.md", "document", "INDIRECT"))
    assert render(_plan(*nodes)) == render(_plan(*nodes))


# --- who does the work -------------------------------------------------------


def test_a_code_action_is_marked_as_carried_out_by_a_person():
    plan_ = _plan(_node("app/handler.py", "software", "DIRECT"))
    action = plan_.of_kind("CODE")[0]
    assert action.is_proposal_only
    assert action.performer == "human"


def test_a_workflow_action_is_applied_by_the_system():
    plan_ = _plan(_node("workflow/routes.yaml", "workflow_config", "DIRECT"))
    assert plan_.of_kind("WORKFLOW")[0].performer == "system"


def test_the_rendering_marks_what_a_person_has_to_do():
    plan_ = _plan(_node("app/handler.py", "software", "DIRECT"))
    assert "[by a person]" in render(plan_)


def test_detecting_a_code_change_is_separate_from_generating_one():
    """Cutting the patch generator must not cut the detection."""
    plan_ = _plan(_node("app/handler.py", "software", "DIRECT"))
    action = plan_.of_kind("CODE")[0]
    assert action.necessity == REQUIRED
    assert not action.generated, (
        "no patch was generated here, and the action is still required; "
        "the product's claim is that it NOTICES the code change, not that "
        "it writes it")


# --- the kind a technical product forgets ------------------------------------


def test_a_role_that_acts_on_the_rule_produces_a_communication_action():
    plan_ = _plan(_node("Procurement Manager", "role", "INDIRECT"))
    assert communication_actions(plan_)


def test_the_communication_action_says_what_changes_for_them():
    plan_ = _plan(_node("Procurement Manager", "role", "INDIRECT"))
    description = communication_actions(plan_)[0].description
    assert "1000" in description and "500" in description


def test_a_notification_target_is_a_communication_action_too():
    plan_ = _plan(_node("notify:finance-team", "notification", "INDIRECT"))
    assert communication_actions(plan_)


# --- introduced vs inherited (P6 freeze, item 2) -----------------------------


class _Case:
    def __init__(self, handlers):
        self.shadow = [type("O", (), {"handler": h, "outcome": "x",
                                      "error": ""})() for h in handlers]


def test_an_introduced_contradiction_promotes_its_artifact_to_required():
    plan_ = _plan(
        _node("application/emergency_purchase.py", "software", "INDIRECT"),
        introduced=[_Case(["application.emergency_purchase.route_emergency"])])
    action = plan_.of_kind("CODE")[0]
    assert action.necessity == REQUIRED
    assert "without reading the changed value" in action.description


def test_an_inherited_contradiction_is_noted_and_not_blamed_on_the_change():
    plan_ = _plan(_node("workflow/routes.yaml", "workflow_config", "DIRECT"),
                  inherited=[_Case(["a.b"])])
    note = " ".join(plan_.notes)
    assert "already existed before this change" in note
    assert "NOT caused by it" in note


def test_an_inherited_contradiction_does_not_promote_anything():
    with_inherited = _plan(_node("docs/policy.md", "document", "INDIRECT"),
                           inherited=[_Case(["a.b"])])
    without = _plan(_node("docs/policy.md", "document", "INDIRECT"))
    assert ([a.necessity for a in with_inherited.actions]
            == [a.necessity for a in without.actions])
