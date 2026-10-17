# -*- coding: utf-8 -*-
"""The six rows, and the two that matter most.

Every row asserts BOTH axes. A test that checked only `alignment` would
pass for a system that reported ALIGNED and then proposed four changes,
and that is precisely the confusion `ChangeDecision` exists to prevent.

    row 3  Scenario E -- only the data differs; touch no code
    row 4  Scenario H -- already true; do nothing at all

Those two are what separate a system that understands a problem from one
that always finds work to do.
"""

from __future__ import annotations

import pytest

from core.change_intelligence.intent_reconciliation import (
    BEHAVIOUR_KINDS,
    IntentOutcome,
    Position,
    reconcile_intent,
)
from core.change_intelligence.states import AlignmentState, ChangeDecision

TARGET = "approval_threshold"


def world(workflow, excel, sop, tests):
    """The four representations the reconciliation matrix names."""
    return [
        Position("workflow", "workflow/approval_routes.yaml", workflow, "L37"),
        Position("spreadsheet", "data/approval_matrix.xlsx", excel, "Thresholds!2"),
        Position("docs", "docs/procurement-sop.md", sop, "body!29"),
        Position("tests", "tests/test_approval_rules.py", tests, "L78"),
    ]


# --- the six rows -------------------------------------------------------------------


def test_row1_intent_matches_the_spreadsheet_only():
    """500k wanted; Excel already there; workflow, SOP and tests are not."""
    outcome = reconcile_intent(
        500000, world(1000000, 500000, 1000000, 1000000), target=TARGET)

    assert outcome.alignment is AlignmentState.DRIFT
    assert outcome.decision is ChangeDecision.CHANGE_REQUIRED
    changed = {a.artifact for a in outcome.actions}
    assert "data/approval_matrix.xlsx" not in changed
    assert len(outcome.actions) == 3


def test_row2_intent_is_new_and_the_whole_system_is_elsewhere():
    outcome = reconcile_intent(
        500000, world(1000000, 1000000, 1000000, 1000000), target=TARGET)

    assert outcome.alignment is AlignmentState.DRIFT
    assert outcome.decision is ChangeDecision.CHANGE_REQUIRED
    assert len(outcome.actions) == 4


def test_row3_only_the_spreadsheet_differs_is_a_data_change(   ):
    """SCENARIO E, and one of the two most important rows in the product.

    The request agrees with code, workflow, SOP and tests. Only the
    spreadsheet is out of step. The right answer is to correct the data and
    touch nothing that runs -- a system that "fixes" the code here has
    changed working behaviour to match a stale sheet.
    """
    outcome = reconcile_intent(
        1000000, world(1000000, 500000, 1000000, 1000000), target=TARGET)

    assert outcome.alignment is AlignmentState.DRIFT
    assert outcome.decision is ChangeDecision.CHANGE_REQUIRED
    assert len(outcome.actions) == 1
    assert outcome.actions[0].kind == "data"
    assert outcome.actions[0].artifact.endswith(".xlsx")

    assert outcome.is_knowledge_only
    assert not {a.kind for a in outcome.actions} & BEHAVIOUR_KINDS, (
        "a knowledge-only correction proposed a change to something that runs")


def test_row4_reality_already_matches_the_request():
    """SCENARIO H, and the other most important row.

    A model asked to be helpful will always find work to do. This row is
    the product saying the correct answer is sometimes nothing at all.
    """
    outcome = reconcile_intent(
        1000000, world(1000000, 1000000, 1000000, 1000000), target=TARGET)

    assert outcome.alignment is AlignmentState.ALIGNED
    assert outcome.decision is ChangeDecision.NO_CHANGE_REQUIRED
    assert outcome.actions == ()
    assert "already true" in outcome.why


def test_row5_three_values_is_a_conflict_for_a_person():
    """The artifacts disagree among themselves AND the request names a
    further position. Which value is being changed FROM has no evidential
    answer, and picking one is the thing this product exists not to do.
    """
    outcome = reconcile_intent(
        500000, world(1000000, 500000, 300000, 1000000), target=TARGET)

    assert outcome.alignment is AlignmentState.CONFLICT
    assert outcome.decision is ChangeDecision.HUMAN_REVIEW_REQUIRED
    assert outcome.actions == ()


def test_row6_an_unreadable_representation_is_unknown_with_a_gap():
    """I5. Not knowing what an artifact says is not the same as knowing it
    agrees."""
    outcome = reconcile_intent(
        1000000, world(1000000, None, 1000000, 1000000), target=TARGET)

    assert outcome.alignment is AlignmentState.UNKNOWN
    assert outcome.decision is ChangeDecision.HUMAN_REVIEW_REQUIRED
    assert outcome.gaps
    assert outcome.actions == ()


def test_row6b_an_undetermined_request_is_unknown_not_aligned():
    """The artifacts agreeing with each other does not answer a question
    nobody managed to ask."""
    outcome = reconcile_intent(
        None, world(1000000, 1000000, 1000000, 1000000), target=TARGET,
        intent_known=False)

    assert outcome.alignment is AlignmentState.UNKNOWN
    assert outcome.decision is ChangeDecision.HUMAN_REVIEW_REQUIRED
    assert outcome.gaps


# --- the axes stay apart ----------------------------------------------------------


def test_no_change_required_is_not_an_alignment_state():
    """Asserted here because this is the module most tempted to
    merge them: "everything agrees" and "therefore do nothing" are
    different claims."""
    assert "NO_CHANGE_REQUIRED" not in {s.name for s in AlignmentState}
    assert len(AlignmentState) == 4
    assert len(ChangeDecision) == 3


def test_every_row_answers_both_axes():
    """A system that reported ALIGNED and then proposed four changes would
    pass a test that checked only one field."""
    rows = [
        reconcile_intent(500000, world(1000000, 500000, 1000000, 1000000), target=TARGET),
        reconcile_intent(500000, world(1000000, 1000000, 1000000, 1000000), target=TARGET),
        reconcile_intent(1000000, world(1000000, 500000, 1000000, 1000000), target=TARGET),
        reconcile_intent(1000000, world(1000000, 1000000, 1000000, 1000000), target=TARGET),
        reconcile_intent(500000, world(1000000, 500000, 300000, 1000000), target=TARGET),
        reconcile_intent(1000000, world(1000000, None, 1000000, 1000000), target=TARGET),
    ]
    for outcome in rows:
        assert isinstance(outcome.alignment, AlignmentState)
        assert isinstance(outcome.decision, ChangeDecision)
        assert outcome.why
        if outcome.decision is ChangeDecision.NO_CHANGE_REQUIRED:
            assert outcome.actions == ()
        if outcome.decision is ChangeDecision.HUMAN_REVIEW_REQUIRED:
            assert outcome.actions == (), (
                "a decision that hands over to a person must not also carry "
                "a list of changes to make")


def test_all_four_alignment_states_are_reachable():
    """A matrix with an unreachable row is a row nobody thought about."""
    reached = {
        reconcile_intent(1000000, world(1000000, 1000000, 1000000, 1000000), target=TARGET).alignment,
        reconcile_intent(500000, world(1000000, 1000000, 1000000, 1000000), target=TARGET).alignment,
        reconcile_intent(500000, world(1000000, 500000, 300000, 1000000), target=TARGET).alignment,
        reconcile_intent(1000000, world(1000000, None, 1000000, 1000000), target=TARGET).alignment,
    }
    assert reached == set(AlignmentState)


# --- Scenario E, stated as its own guarantee -----------------------------------------


def test_knowledge_only_is_computed_from_the_actions_not_declared():
    """An outcome cannot claim to be knowledge-only while carrying a code
    change."""
    outcome = reconcile_intent(
        500000, world(1000000, 1000000, 1000000, 1000000), target=TARGET)
    assert not outcome.is_knowledge_only
    assert {a.kind for a in outcome.actions} & BEHAVIOUR_KINDS


def test_an_empty_action_list_is_not_knowledge_only():
    """Scenario H is not Scenario E. Doing nothing is not a doc change."""
    outcome = reconcile_intent(
        1000000, world(1000000, 1000000, 1000000, 1000000), target=TARGET)
    assert outcome.actions == ()
    assert outcome.is_knowledge_only is False


def test_no_branch_here_picks_a_winner():
    """I15, by attribute."""
    from core.change_intelligence import intent_reconciliation

    for forbidden in ("pick_newest", "prefer_source", "auto_resolve",
                      "choose_winner", "resolve_automatically"):
        assert not hasattr(intent_reconciliation, forbidden)


def test_every_action_says_where_it_came_from_and_where_it_goes():
    """A proposal a reviewer cannot check is a proposal they will rubber
    stamp."""
    outcome = reconcile_intent(
        500000, world(1000000, 1000000, 1000000, 1000000), target=TARGET)
    for action in outcome.actions:
        assert action.from_value is not None
        assert action.to_value == 500000
        assert action.artifact and action.why
