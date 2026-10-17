# -*- coding: utf-8 -*-
"""The seven kinds of human feedback, and what each one does.

Defined at P1 so that P5 implements a contract rather than inventing one.
Gate G5 asks that at least four of the seven produce genuinely DIFFERENT
downstream behaviour -- a classifier that sorts feedback into seven bins and
then does the same thing with all seven has understood nothing.

I8 runs through every row of this table: no path here reaches the Fact
layer. A person's correction changes what the system BELIEVES, never what it
OBSERVED.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SemanticType(Enum):
    OBSERVATION = "observation"
    CORRECTION = "correction"
    CLARIFICATION = "clarification"
    INTENT = "intent"
    EXCEPTION = "exception"
    APPROVAL = "approval"
    REJECTION = "rejection"


class Downstream(Enum):
    """What a piece of feedback actually CAUSES. The unit Gate G5 counts."""

    LEDGER_ONLY = "ledger_only"
    REEVALUATE_INFERENCE = "reevaluate_inference"
    MAY_CLOSE_GAP = "may_close_gap"
    OPEN_CHANGE_BRANCH = "open_change_branch"
    MARK_INTENTIONAL = "mark_intentional"
    STATE_TRANSITION = "state_transition"


@dataclass(frozen=True)
class Semantics:
    downstream: Downstream
    touches_fact_layer: bool
    creates_intent: bool
    note: str


SEMANTICS: dict[SemanticType, Semantics] = {
    SemanticType.OBSERVATION: Semantics(
        Downstream.LEDGER_ONLY, False, False,
        "Recorded and nothing else. A system that acts on every remark cannot be talked to.",
    ),
    SemanticType.CORRECTION: Semantics(
        Downstream.REEVALUATE_INFERENCE, False, False,
        "The inference was wrong. Reject it, hand the decision to a person, re-evaluate. "
        "The FACT it was drawn from is untouched -- that is I8.",
    ),
    SemanticType.CLARIFICATION: Semantics(
        Downstream.MAY_CLOSE_GAP, False, False,
        "Context about what already exists. MAY close a gap -- only if the added "
        "context is itself evidence-backed, otherwise the gap stays open and says why.",
    ),
    SemanticType.INTENT: Semantics(
        Downstream.OPEN_CHANGE_BRANCH, False, True,
        "A request that the future be different. The only type that opens "
        "intent -> impact -> shadow -> proposal.",
    ),
    SemanticType.EXCEPTION: Semantics(
        Downstream.MARK_INTENTIONAL, False, False,
        "The finding is correct AND the divergence is deliberate. Marked intentional, "
        "never deleted: deleting it means the next run reports it again (zombie finding).",
    ),
    SemanticType.APPROVAL: Semantics(
        Downstream.STATE_TRANSITION, False, False,
        "REVIEW -> APPROVED, with actor and timestamp. I12.",
    ),
    SemanticType.REJECTION: Semantics(
        Downstream.STATE_TRANSITION, False, False,
        "-> REJECTED, with a reason. The reason is the part a person needs.",
    ),
}


def downstream_of(semantic_type: SemanticType | str) -> Downstream:
    if isinstance(semantic_type, str):
        semantic_type = SemanticType(semantic_type)
    return SEMANTICS[semantic_type].downstream


def distinct_behaviours() -> set[Downstream]:
    """How many genuinely different outcomes the seven types produce.

    Gate G5 wants at least four. Approval and rejection deliberately share
    STATE_TRANSITION: they are the same kind of act in opposite directions,
    and inventing a seventh Downstream to make the count look better would
    be padding the metric rather than meeting it.
    """
    return {s.downstream for s in SEMANTICS.values()}
