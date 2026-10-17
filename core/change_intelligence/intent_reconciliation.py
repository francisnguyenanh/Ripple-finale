# -*- coding: utf-8 -*-
"""What a person wants, against what is actually there.

Six rows, and rows three and four are the two most important
in the whole product:

    row 3  only the spreadsheet differs   -> a data change, and NOTHING in
                                             code or workflow (Scenario E)
    row 4  reality already matches        -> ALIGNED, NO_CHANGE_REQUIRED,
                                             zero actions (Scenario H)

Row 3 shows the system knows the right answer is sometimes *do not touch
the code*. Row 4 shows it knows the right answer is sometimes *do nothing
at all*. Both are what separates a system that understands a problem from
one that always finds work to do -- and a model asked to be helpful will
always find work to do, which is why none of this is asked of a model.

The two axes stay separate. `alignment` says what reality looks
like; `decision` says what follows. `NO_CHANGE_REQUIRED` is a ChangeDecision
and can never be an AlignmentState: "everything agrees" and "therefore do
nothing" are different claims, and collapsing them means a system that
cannot express "they disagree and I am not going to guess which is right".

I15 holds here as everywhere: where the evidence does not settle it, this
returns HUMAN_REVIEW_REQUIRED. There is no branch that picks a winner.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence.alignment import Gap
from core.change_intelligence.states import AlignmentState, ChangeDecision


@dataclass(frozen=True)
class Position:
    """What one artifact family holds, for this comparison.

    `value is None` means "states nothing", which is different from "states
    something we could not read" -- the second is a Gap and arrives as one.
    """

    family: str
    artifact: str
    value: object
    locator: str = ""


@dataclass(frozen=True)
class Action:
    """One thing that would have to change, and where.

    `kind` is what tests assert over: a knowledge-only correction must
    produce no action whose kind is `code` or `workflow`.
    """

    kind: str  # "doc" | "data" | "code" | "workflow" | "test"
    artifact: str
    from_value: object
    to_value: object
    why: str


#: family -> the kind of change touching it would be. The mapping is here
#: rather than inferred from a path, because "what kind of change is this"
#: is a product question and a path is an accident of layout.
KIND_OF = {
    "docs": "doc",
    "spreadsheet": "data",
    "software": "code",
    "workflow": "workflow",
    "tests": "test",
}

KNOWLEDGE_KINDS = frozenset({"doc", "data"})
BEHAVIOUR_KINDS = frozenset({"code", "workflow"})


@dataclass
class IntentOutcome:
    """The three fields Gate G5 requires, and never two of them merged."""

    alignment: AlignmentState
    decision: ChangeDecision
    actions: tuple[Action, ...] = ()
    gaps: tuple[Gap, ...] = ()
    why: str = ""

    @property
    def is_knowledge_only(self) -> bool:
        """True when nothing that RUNS would change.

        The property Scenario E is about. Computed from the actions rather
        than set by whoever built the outcome, so it cannot claim to be
        knowledge-only while carrying a code change.
        """
        return bool(self.actions) and all(
            a.kind in KNOWLEDGE_KINDS for a in self.actions)


def reconcile_intent(desired, positions, *, target: str = "",
                     intent_known: bool = True) -> IntentOutcome:
    """Compare one intent against every representation of the rule.

    `desired` is what the person asked for. `positions` is what the
    artifacts hold. Nothing here consults a model and nothing chooses a
    winner between two artifacts that disagree.
    """
    positions = tuple(positions)
    stated = [p for p in positions if p.value is not None]
    missing = [p for p in positions if p.value is None]

    gaps = tuple(Gap(gap_id=f"GAP-{target}-{p.family}",
                     reason=(f"{p.artifact} is expected to state {target} and "
                             f"states nothing readable"))
                 for p in missing)

    # Row 6: the intent itself is not known. Asked first, because an
    # unknown request cannot be compared with anything -- and answering
    # "aligned" because the artifacts agree with each other would be
    # answering a question nobody asked.
    if not intent_known or desired is None:
        return IntentOutcome(
            alignment=AlignmentState.UNKNOWN,
            decision=ChangeDecision.HUMAN_REVIEW_REQUIRED,
            gaps=gaps or (Gap(gap_id=f"GAP-{target}-intent",
                              reason="the request does not determine a value"),),
            why="the request does not determine what is wanted")

    distinct = {p.value for p in stated}

    # Row 6 again, from the other side: a representation the product could
    # not read. Not knowing what an artifact says is not the same as knowing
    # it agrees.
    if missing:
        return IntentOutcome(
            alignment=AlignmentState.UNKNOWN,
            decision=ChangeDecision.HUMAN_REVIEW_REQUIRED,
            gaps=gaps,
            why=(f"{len(missing)} representation(s) state nothing readable, so "
                 f"whether reality matches the request is not derivable"))

    # Row 5: the artifacts disagree AMONG THEMSELVES. The intent cannot be
    # applied to a rule whose current value is contested -- which of the
    # three is being changed FROM is a question with no evidential answer,
    # and picking one is the thing this product exists not to do (I15).
    if len(distinct) > 2 or (len(distinct) == 2 and desired not in distinct):
        return IntentOutcome(
            alignment=AlignmentState.CONFLICT,
            decision=ChangeDecision.HUMAN_REVIEW_REQUIRED,
            gaps=gaps,
            why=(f"the artifacts hold {len(distinct)} different values "
                 f"({', '.join(str(v) for v in sorted(distinct, key=str))}) and "
                 f"the request names a fourth position; which one is being "
                 f"changed from is not derivable"))

    # Row 4 -- Scenario H. Everything already agrees with the request.
    if distinct == {desired}:
        return IntentOutcome(
            alignment=AlignmentState.ALIGNED,
            decision=ChangeDecision.NO_CHANGE_REQUIRED,
            actions=(),
            gaps=gaps,
            why=(f"every representation already states {desired}; the request "
                 f"asks for what is already true"))

    # Rows 1-3. Reality does not match the request, and the artifacts are
    # ordered enough to say what would have to move.
    actions = tuple(
        Action(kind=KIND_OF.get(p.family, p.family),
               artifact=p.artifact,
               from_value=p.value,
               to_value=desired,
               why=f"{p.artifact} states {p.value}; the request is {desired}")
        for p in sorted(stated, key=lambda p: (p.family, p.artifact))
        if p.value != desired)

    kinds = {a.kind for a in actions}
    return IntentOutcome(
        alignment=AlignmentState.DRIFT,
        decision=ChangeDecision.CHANGE_REQUIRED,
        actions=actions,
        gaps=gaps,
        why=(f"{len(actions)} of {len(stated)} representations differ from the "
             f"request; the change touches {', '.join(sorted(kinds))}"))


def knowledge_only(outcome: IntentOutcome) -> bool:
    """Scenario E, asked as a question rather than asserted as a label."""
    return outcome.is_knowledge_only


__all__ = [
    "Action",
    "BEHAVIOUR_KINDS",
    "KIND_OF",
    "KNOWLEDGE_KINDS",
    "IntentOutcome",
    "Position",
    "knowledge_only",
    "reconcile_intent",
]
