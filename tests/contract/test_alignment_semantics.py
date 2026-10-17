# -*- coding: utf-8 -*-
"""Alignment semantics.

Every pair of alignment states needs a fixture that distinguishes them.
The pair that matters most is DRIFT vs CONFLICT: they are the two words a
judge hears in the first thirty seconds, and the hero scenario is a
CONFLICT. Getting them backwards in the video would claim an answer the
system does not have.
"""

from __future__ import annotations

import itertools

import pytest

from core.change_intelligence.alignment import (
    AlignmentOutcome,
    Gap,
    Representation,
    classify,
    decide,
    reconcile,
)
from core.change_intelligence.states import AlignmentState, ChangeDecision


def rep(artifact, value, *, observed_at=None, locator="loc"):
    return Representation(
        artifact=artifact, semantic_id="approval_threshold", value=value,
        locator=locator, observed_at=observed_at,
    )


# --- the four states --------------------------------------------------------


def test_aligned_when_every_representation_agrees():
    outcome = reconcile([rep("a.yaml", 500_000), rep("b.csv", 500_000)])
    assert outcome.alignment is AlignmentState.ALIGNED
    assert outcome.decision is ChangeDecision.NO_CHANGE_REQUIRED
    assert outcome.actions == []


def test_drift_when_they_differ_and_one_is_demonstrably_newer():
    outcome = reconcile(
        [rep("a.yaml", 1_000_000, observed_at="2020-03-04"),
         rep("b.csv", 500_000, observed_at="2022-04-11")],
        actions=["align the workflow to the spreadsheet"],
    )
    assert outcome.alignment is AlignmentState.DRIFT
    assert "newest is b.csv" in outcome.reason


def test_conflict_when_they_differ_and_nothing_orders_them():
    """The hero scenario. Three artifacts, three claims, no authority."""
    outcome = reconcile([
        rep("approval_routes.yaml", 1_000_000, locator="rules[0].threshold"),
        rep("approval_matrix.xlsx", 500_000, locator="row 2, column threshold"),
        rep("SOP-Procurement.md", 1_000_000, locator="section 4.2"),
    ])
    assert outcome.alignment is AlignmentState.CONFLICT
    assert outcome.decision is ChangeDecision.HUMAN_REVIEW_REQUIRED


def test_unknown_requires_a_gap():
    outcome = reconcile([], gaps=[Gap("GAP-001", "no artifact states this threshold")])
    assert outcome.alignment is AlignmentState.UNKNOWN
    assert outcome.decision is ChangeDecision.HUMAN_REVIEW_REQUIRED
    assert outcome.gaps


def test_i5_unknown_without_a_gap_cannot_be_constructed():
    with pytest.raises(ValueError, match="Gap"):
        AlignmentOutcome(alignment=AlignmentState.UNKNOWN,
                         decision=ChangeDecision.HUMAN_REVIEW_REQUIRED)


# --- the pair that matters --------------------------------------------------


def test_drift_and_conflict_differ_only_in_whether_time_orders_them():
    """One input changes -- the recency markers -- and the verdict flips.

    This is the whole definition, written as an experiment rather than as
    prose, so it cannot drift away from the glossary.
    """
    without = [rep("a.yaml", 1_000_000), rep("b.csv", 500_000)]
    with_time = [rep("a.yaml", 1_000_000, observed_at="2020-03-04"),
                 rep("b.csv", 500_000, observed_at="2022-04-11")]

    assert classify(without) is AlignmentState.CONFLICT
    assert classify(with_time) is AlignmentState.DRIFT


def test_one_missing_recency_marker_is_enough_to_make_it_a_conflict():
    """Half an ordering is not an ordering.

    If we do not know when one side was written, "the other is newer" is a
    guess -- and a guess presented as DRIFT tells a person to go change the
    wrong artifact.
    """
    reps = [rep("a.yaml", 1_000_000, observed_at="2020-03-04"),
            rep("b.csv", 500_000, observed_at=None)]
    assert classify(reps) is AlignmentState.CONFLICT


def test_identical_timestamps_do_not_order_anything():
    reps = [rep("a.yaml", 1_000_000, observed_at="2022-04-11"),
            rep("b.csv", 500_000, observed_at="2022-04-11")]
    assert classify(reps) is AlignmentState.CONFLICT


def test_i15_a_conflict_keeps_every_side_and_names_no_winner():
    outcome = reconcile([rep("a.yaml", 1_000_000), rep("b.csv", 500_000)])
    assert {r.value for r in outcome.representations} == {1_000_000, 500_000}
    assert not hasattr(outcome, "winner")
    assert not hasattr(outcome, "resolved_value")


@pytest.mark.parametrize(
    "left,right", list(itertools.combinations(list(AlignmentState), 2)),
    ids=lambda s: getattr(s, "name", s),
)
def test_every_pair_of_states_is_reachable_by_some_fixture(left, right):
    """No state is unreachable, and no two states share a fixture."""
    produced = {
        AlignmentState.ALIGNED: classify([rep("a", 1), rep("b", 1)]),
        AlignmentState.DRIFT: classify([rep("a", 1, observed_at="2020"),
                                        rep("b", 2, observed_at="2022")]),
        AlignmentState.CONFLICT: classify([rep("a", 1), rep("b", 2)]),
        AlignmentState.UNKNOWN: classify([], gaps=[Gap("G", "why")]),
    }
    assert produced[left] is left
    assert produced[right] is right
    assert produced[left] is not produced[right]


# --- the second axis --------------------------------------------------------


def test_aligned_but_stale_evidence_still_requires_a_change():
    """The case that proves the axes are not the same axis.

    Everything agrees -- so reality is ALIGNED -- but the evidence was read
    from a snapshot that has since moved, so the grounding needs refreshing.
    A single merged enum has nowhere to put this.
    """
    assert decide(AlignmentState.ALIGNED, evidence_is_fresh=False) is ChangeDecision.CHANGE_REQUIRED


def test_drift_above_the_risk_threshold_goes_to_a_human():
    """The mirror case: we know what to do and still must not do it alone."""
    assert decide(AlignmentState.DRIFT, exceeds_risk_threshold=True) is (
        ChangeDecision.HUMAN_REVIEW_REQUIRED
    )


def test_aligned_with_unresolved_items_is_not_all_clear():
    """Verification blocks on exactly this. ALIGNED must not read as 'done'."""
    assert decide(AlignmentState.ALIGNED, unresolved=["GAP-002"]) is (
        ChangeDecision.HUMAN_REVIEW_REQUIRED
    )


def test_no_change_required_forbids_a_non_empty_action_list():
    with pytest.raises(ValueError, match="unnecessary-change"):
        AlignmentOutcome(alignment=AlignmentState.ALIGNED,
                         decision=ChangeDecision.NO_CHANGE_REQUIRED,
                         actions=["do something anyway"])


def test_change_required_forbids_an_empty_action_list():
    with pytest.raises(ValueError, match="not a finding"):
        AlignmentOutcome(alignment=AlignmentState.DRIFT,
                         decision=ChangeDecision.CHANGE_REQUIRED,
                         actions=[])


def test_reconcile_drops_actions_when_it_decides_nothing_is_needed():
    """Scenario H: the system is allowed to say 'nothing to do'.

    A caller that hands in a speculative action list must not be able to
    turn a no-op into a proposal just by having prepared one.
    """
    outcome = reconcile([rep("a", 1), rep("b", 1)], actions=["a speculative action"])
    assert outcome.decision is ChangeDecision.NO_CHANGE_REQUIRED
    assert outcome.actions == []


def test_a_conflict_with_one_side_is_not_a_conflict():
    with pytest.raises(ValueError, match="two representations"):
        AlignmentOutcome(alignment=AlignmentState.CONFLICT,
                         decision=ChangeDecision.HUMAN_REVIEW_REQUIRED,
                         representations=[rep("a", 1)])
