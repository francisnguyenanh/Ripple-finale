# -*- coding: utf-8 -*-
"""State machine contract.

The forbidden transitions are not enumerated by hand. Every pair in the
Cartesian product that is absent from the table must raise, which is a
stronger claim than a list of six bad edges: a hand-written list is only as
complete as the person writing it, and the edge nobody thought of is exactly
the one an attacker or a refactor will take.
"""

from __future__ import annotations

import itertools

import pytest

from core.change_intelligence.states import (
    CHANGESET_TRANSITIONS,
    INTENT_TRANSITIONS,
    KNOWLEDGE_TRANSITIONS,
    AlignmentState,
    ChangeDecision,
    ChangeSetState,
    IntentState,
    KnowledgeState,
    MissingActorError,
    TransitionError,
    can_transition,
    transition,
)

ACTOR = {"actor": "ops.lead@example.com", "timestamp": "2026-09-19T12:00:00Z"}

MACHINES = [
    ("Knowledge", KnowledgeState, KNOWLEDGE_TRANSITIONS),
    ("Intent", IntentState, INTENT_TRANSITIONS),
    ("ChangeSet", ChangeSetState, CHANGESET_TRANSITIONS),
]


def _pairs():
    for machine, enum, table in MACHINES:
        for source, target in itertools.product(enum, enum):
            yield pytest.param(machine, source, target, table, id=f"{machine}:{source.name}->{target.name}")


@pytest.mark.parametrize("machine,source,target,table", list(_pairs()))
def test_every_pair_is_permitted_or_refused_exactly_as_the_table_says(machine, source, target, table):
    permitted = target in table[source]
    assert can_transition(source, target) is permitted

    if permitted:
        result = transition(source, target, **ACTOR)
        assert result.source is source and result.target is target
        assert result.machine == machine
    else:
        with pytest.raises(TransitionError) as exc:
            transition(source, target, **ACTOR)
        assert exc.value.source is source
        assert exc.value.target is target


# --- the six named forbidden transitions, called out individually -----------
#
# Already covered by the product test above. Named separately because each
# one is an invariant someone will be tempted to relax, and a failure here
# says WHICH promise broke rather than "some edge changed".


def test_i13_proposed_may_not_jump_straight_to_applying():
    """Skipping REVIEW and APPROVED is the bypass mutation 3 looks for."""
    with pytest.raises(TransitionError):
        transition(ChangeSetState.PROPOSED, ChangeSetState.APPLYING, **ACTOR)


def test_i13_review_may_not_skip_approval():
    with pytest.raises(TransitionError):
        transition(ChangeSetState.REVIEW, ChangeSetState.APPLYING, **ACTOR)


def test_approved_may_not_jump_to_applied():
    """Losing APPLYING loses the only state in which an apply is in flight.

    Without it there is no moment at which the system can say "this is
    running" -- a crash mid-apply would leave a ChangeSet that claims to be
    APPROVED with a half-written overlay behind it.
    """
    with pytest.raises(TransitionError):
        transition(ChangeSetState.APPROVED, ChangeSetState.APPLIED, **ACTOR)


def test_i14_applied_is_not_terminal_and_leads_only_to_verified():
    assert CHANGESET_TRANSITIONS[ChangeSetState.APPLIED] == frozenset({ChangeSetState.VERIFIED})


def test_i10_intent_draft_may_not_skip_validation():
    with pytest.raises(TransitionError):
        transition(IntentState.DRAFT, IntentState.APPROVED, **ACTOR)


def test_i10_a_rejected_intent_is_still_stored():
    """An invalid intent is kept with status REJECTED, not dropped.

    The reason it was rejected is the part a person needs; deleting the
    record deletes the reason.
    """
    assert IntentState.REJECTED in INTENT_TRANSITIONS[IntentState.DRAFT]
    assert IntentState.REJECTED in INTENT_TRANSITIONS[IntentState.VALIDATED]


# --- I12: attribution is part of the transition -----------------------------


@pytest.mark.parametrize("missing", ["actor", "timestamp"])
def test_i12_a_transition_without_attribution_is_refused(missing):
    kwargs = dict(ACTOR)
    kwargs[missing] = ""
    with pytest.raises(MissingActorError):
        transition(ChangeSetState.REVIEW, ChangeSetState.APPROVED, **kwargs)


def test_i12_a_blank_actor_is_not_an_actor():
    with pytest.raises(MissingActorError):
        transition(ChangeSetState.REVIEW, ChangeSetState.APPROVED, actor="   ",
                   timestamp="2026-09-19T12:00:00Z")


def test_a_forbidden_edge_and_a_missing_actor_raise_DIFFERENT_errors():
    """A design mistake and a missing signature are not the same failure.

    If both raised one error type, a caller could not tell "this transition
    is impossible" from "you forgot to say who did it", and would retry the
    wrong one.
    """
    with pytest.raises(TransitionError):
        transition(ChangeSetState.PROPOSED, ChangeSetState.APPLIED, **ACTOR)
    with pytest.raises(MissingActorError):
        transition(ChangeSetState.PROPOSED, ChangeSetState.REVIEW, actor="", timestamp="t")


def test_states_from_two_different_machines_never_connect():
    with pytest.raises(TransitionError):
        transition(IntentState.APPROVED, ChangeSetState.APPROVED, **ACTOR)
    assert not can_transition(IntentState.APPROVED, ChangeSetState.APPROVED)


# --- the two axes stay perpendicular --------------------------------


def test_alignment_state_has_exactly_four_members():
    assert len(AlignmentState) == 4
    assert {s.name for s in AlignmentState} == {"ALIGNED", "DRIFT", "CONFLICT", "UNKNOWN"}


def test_change_decision_has_exactly_three_members():
    assert len(ChangeDecision) == 3
    assert {d.name for d in ChangeDecision} == {
        "NO_CHANGE_REQUIRED", "CHANGE_REQUIRED", "HUMAN_REVIEW_REQUIRED"
    }


def test_no_change_required_is_not_an_alignment_state():
    assert not hasattr(AlignmentState, "NO_CHANGE_REQUIRED")
    with pytest.raises(KeyError):
        AlignmentState["NO_CHANGE_REQUIRED"]
