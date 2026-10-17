# -*- coding: utf-8 -*-
"""One test per feedback type, asserting the behaviours DIFFER.

Gate G5 asks for at least four distinct downstream behaviours. The point is
not the number: it is that classification without consequence is theatre.
"""

from __future__ import annotations

import pytest

from core.change_intelligence.change_semantics import (
    SEMANTICS,
    Downstream,
    SemanticType,
    distinct_behaviours,
    downstream_of,
)


def test_all_seven_types_are_defined():
    assert len(SemanticType) == 7
    assert set(SEMANTICS) == set(SemanticType)


def test_gate_g5_at_least_four_distinct_downstream_behaviours():
    behaviours = distinct_behaviours()
    assert len(behaviours) >= 4, (
        f"only {len(behaviours)} distinct behaviours: {behaviours}. Seven labels "
        f"with one behaviour is a classifier that changes nothing"
    )


@pytest.mark.parametrize(
    "semantic_type,expected",
    [
        (SemanticType.OBSERVATION, Downstream.LEDGER_ONLY),
        (SemanticType.CORRECTION, Downstream.REEVALUATE_INFERENCE),
        (SemanticType.CLARIFICATION, Downstream.MAY_CLOSE_GAP),
        (SemanticType.INTENT, Downstream.OPEN_CHANGE_BRANCH),
        (SemanticType.EXCEPTION, Downstream.MARK_INTENTIONAL),
        (SemanticType.APPROVAL, Downstream.STATE_TRANSITION),
        (SemanticType.REJECTION, Downstream.STATE_TRANSITION),
    ],
)
def test_each_type_maps_to_its_declared_behaviour(semantic_type, expected):
    assert downstream_of(semantic_type) is expected
    assert downstream_of(semantic_type.value) is expected


def test_only_intent_opens_a_change_branch():
    """If anything else could open it, a passing remark would start a workflow."""
    openers = [t for t in SemanticType if SEMANTICS[t].creates_intent]
    assert openers == [SemanticType.INTENT]


def test_i8_no_feedback_type_touches_the_fact_layer():
    """A person's correction changes what the system BELIEVES, never what it OBSERVED.

    If a correction could rewrite a Fact, then every downstream claim to be
    evidence-backed would be circular: the evidence would have been edited
    to agree with the conclusion.
    """
    offenders = [t.value for t in SemanticType if SEMANTICS[t].touches_fact_layer]
    assert not offenders, offenders


def test_exception_and_correction_do_different_things():
    """The most expensive confusion in the seven.

    Treating an `exception` as a `correction` deletes a real operational
    truth ("Osaka is fast-tracked, deliberately, since 2020") and the next
    run reports the same finding again -- the zombie finding that correction handling exists
    to prevent.
    """
    assert downstream_of(SemanticType.EXCEPTION) is not downstream_of(SemanticType.CORRECTION)
    assert downstream_of(SemanticType.EXCEPTION) is Downstream.MARK_INTENTIONAL


def test_clarification_and_intent_do_different_things():
    """Past tense versus future tense, and only a model can tell them apart.

    "The threshold is actually 500,000" describes reality; "the threshold
    should be 500,000" requests a change. Same words, different act. This
    is one of the AI Essentialness cases.
    """
    assert downstream_of(SemanticType.CLARIFICATION) is not downstream_of(SemanticType.INTENT)


def test_approval_and_rejection_share_a_behaviour_on_purpose():
    """Not padding: they are one kind of act in two directions.

    Inventing an eighth Downstream so the count looks better would be
    meeting the metric instead of the requirement.
    """
    assert downstream_of(SemanticType.APPROVAL) is downstream_of(SemanticType.REJECTION)
    # The sharing has to be documented, or the next reader "fixes" it.
    assert SEMANTICS[SemanticType.REJECTION].note != SEMANTICS[SemanticType.APPROVAL].note


def test_an_unknown_semantic_type_is_refused():
    with pytest.raises(ValueError):
        downstream_of("suggestion")


def test_every_type_records_why_it_behaves_as_it_does():
    """A table of behaviours with no reasons is a table nobody can argue with."""
    for semantic_type, semantics in SEMANTICS.items():
        assert len(semantics.note) > 40, f"{semantic_type.value} has no real explanation"
