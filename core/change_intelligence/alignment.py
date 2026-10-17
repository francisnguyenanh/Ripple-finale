# -*- coding: utf-8 -*-
"""Alignment semantics. The deterministic half of the product.

Gate G1 asks for contracts "implementable without semantic ambiguity". This
is the file where ambiguity is most expensive, because DRIFT and CONFLICT
are the two words a judge hears in the first thirty seconds of the video.

    DRIFT     the representations differ AND we can say which one is older
    CONFLICT  the representations differ AND we cannot

The hero scenario is a CONFLICT, not a DRIFT: three artifacts state the same
rule, they disagree, and nothing in the evidence says which is authoritative.
Calling that "drift" would imply an answer the system does not have.

Nothing in this module calls a model. Classification is a predicate over
evidence; a model's job is upstream (recognising that three sentences are
about the same rule at all -- see identity), not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence.states import AlignmentState, ChangeDecision


@dataclass(frozen=True)
class Representation:
    """One artifact's claim about one rule, with where it was read from."""

    artifact: str
    semantic_id: str
    value: object
    locator: str
    #: Comparable recency marker (a commit date, a `last_reviewed` column, a
    #: version). None means "this artifact cannot tell us how old it is" --
    #: which is precisely what turns a DRIFT into a CONFLICT.
    observed_at: str | None = None
    snapshot_id: str = ""
    #: The guard under which this artifact applies the rule. Compared on its
    #: own axis: two artifacts can agree on every number and still disagree
    #: about who the rule is for.
    scope: str = ""
    #: Reads the value from another artifact instead of stating one.
    defers_to_config: bool = False
    scope_field: str = ""
    #: The field this artifact tests to decide the rule applies.
    field_name: str = ""


@dataclass(frozen=True)
class Gap:
    """An unresolved question. I5: UNKNOWN never travels without one."""

    gap_id: str
    reason: str


@dataclass(frozen=True)
class AlignmentOutcome:
    """The result of reconciliation: what is true, and what follows.

    Three fields rather than one, because "reality is inconsistent" and
    "somebody must act" are different claims and collapsing them is how
    `AlignmentState.NO_CHANGE_REQUIRED` gets invented.
    """

    alignment: AlignmentState
    decision: ChangeDecision
    actions: list = field(default_factory=list)
    representations: list[Representation] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    reason: str = ""

    def __post_init__(self) -> None:
        # The two hard rules of alignment, enforced where they cannot be skipped.
        if self.decision is ChangeDecision.NO_CHANGE_REQUIRED and self.actions:
            raise ValueError(
                "NO_CHANGE_REQUIRED with a non-empty action list: if nothing "
                "needs to change, proposing a change is the unnecessary-change "
                "rate the P9 metric measures"
            )
        if self.decision is ChangeDecision.CHANGE_REQUIRED and not self.actions:
            raise ValueError(
                "CHANGE_REQUIRED with no actions: telling someone a change is "
                "required without saying what to change is not a finding"
            )
        if self.alignment is AlignmentState.UNKNOWN and not self.gaps:
            raise ValueError(
                "UNKNOWN without a Gap: I5 requires a Gap ID and a reason. "
                "An unexplained UNKNOWN is indistinguishable from a bug"
            )
        if self.alignment is AlignmentState.CONFLICT and len(self.representations) < 2:
            raise ValueError(
                "CONFLICT needs at least two representations; a conflict with "
                "one side is a claim, not a conflict"
            )


#: Families whose documents may cover a rule without stating a value.
_PROSE_FAMILIES = ("docs/",)


def states_a_value(rep: "Representation") -> bool:
    """Does this representation take a position on the VALUE?

    Three things look alike and are not. A document that covers a rule in
    prose without a number, and a code site that reads the number from
    config, both hold no opinion about the value -- so neither can disagree
    about it. Only a representation that states one belongs in the
    comparison.

    Folding them in is how a rule every artifact agrees on comes back
    CONFLICT: the disagreement is between a number and an absence, which is
    not a disagreement at all.
    """
    if rep.value is not None:
        return True
    if rep.defers_to_config:
        return False
    return not rep.artifact.startswith(_PROSE_FAMILIES)


def comparable(representations: list["Representation"]) -> list["Representation"]:
    return [r for r in representations if states_a_value(r)]


# --- predicates -------------------------------------------------------------


def _values_agree(reps: list[Representation]) -> bool:
    return len({r.value for r in reps}) <= 1


def _has_time_ordering(reps: list[Representation]) -> bool:
    """Can we say which representation is newer?

    Requires every differing side to carry a recency marker AND the markers
    to be distinct. One missing marker is enough to lose the ordering: if we
    do not know when one side was written, "the other is newer" is a guess.
    """
    marks = [r.observed_at for r in reps]
    if any(m is None for m in marks):
        return False
    return len(set(marks)) == len(marks)


def classify(
    representations: list[Representation],
    *,
    gaps: list[Gap] | None = None,
    unresolved: list[str] | None = None,
    evidence_is_fresh: bool = True,
) -> AlignmentState:
    """The four-way classification, as a total function over the inputs."""
    gaps = gaps or []
    unresolved = unresolved or []

    if not representations or gaps:
        return AlignmentState.UNKNOWN

    if _values_agree(representations):
        # Stale evidence and unresolved items do NOT change what reality
        # looks like -- everything still agrees. They change what should be
        # DONE about it, which is `decide()`'s axis, not this one. Folding
        # them in here is how ALIGNED stops meaning "the artifacts agree".
        return AlignmentState.ALIGNED

    return AlignmentState.DRIFT if _has_time_ordering(representations) else AlignmentState.CONFLICT


#: The default mapping. Deterministic, and deliberately NOT how the axes are
#: related in general -- see `decide()` for the two cases that depart from it.
DEFAULT_DECISION: dict[AlignmentState, ChangeDecision] = {
    AlignmentState.ALIGNED: ChangeDecision.NO_CHANGE_REQUIRED,
    AlignmentState.DRIFT: ChangeDecision.CHANGE_REQUIRED,
    AlignmentState.CONFLICT: ChangeDecision.HUMAN_REVIEW_REQUIRED,
    AlignmentState.UNKNOWN: ChangeDecision.HUMAN_REVIEW_REQUIRED,
}


def decide(
    alignment: AlignmentState,
    *,
    evidence_is_fresh: bool = True,
    unresolved: list[str] | None = None,
    exceeds_risk_threshold: bool = False,
) -> ChangeDecision:
    """Map an alignment state to an action decision.

    Two departures from the default mapping, and they are the whole reason
    the axes are kept separate rather than folded into one enum:

        ALIGNED + CHANGE_REQUIRED        everything agrees, but the evidence
                                         is stale -- it needs re-grounding
        DRIFT   + HUMAN_REVIEW_REQUIRED  we know which side is newer, but the
                                         change is too risky to propose alone
    """
    unresolved = unresolved or []

    if alignment is AlignmentState.ALIGNED:
        if not evidence_is_fresh:
            return ChangeDecision.CHANGE_REQUIRED
        if unresolved:
            # I: ALIGNED with unresolved items must not read as "all clear".
            # Verification blocks on exactly this combination.
            return ChangeDecision.HUMAN_REVIEW_REQUIRED
        return ChangeDecision.NO_CHANGE_REQUIRED

    if alignment is AlignmentState.DRIFT and exceeds_risk_threshold:
        return ChangeDecision.HUMAN_REVIEW_REQUIRED

    return DEFAULT_DECISION[alignment]


def reconcile(
    representations: list[Representation],
    *,
    gaps: list[Gap] | None = None,
    unresolved: list[str] | None = None,
    evidence_is_fresh: bool = True,
    exceeds_risk_threshold: bool = False,
    actions: list | None = None,
) -> AlignmentOutcome:
    """Classify, decide, and refuse to emit an incoherent pair.

    I15 lives here by omission: there is no branch that picks a winning
    representation. A CONFLICT comes back with every side intact, and
    resolving it is a person's job (see confirmation).
    """
    gaps = list(gaps or [])
    unresolved = list(unresolved or [])
    actions = list(actions or [])

    alignment = classify(
        representations,
        gaps=gaps,
        unresolved=unresolved,
        evidence_is_fresh=evidence_is_fresh,
    )
    decision = decide(
        alignment,
        evidence_is_fresh=evidence_is_fresh,
        unresolved=unresolved,
        exceeds_risk_threshold=exceeds_risk_threshold,
    )

    if decision is ChangeDecision.NO_CHANGE_REQUIRED:
        actions = []

    return AlignmentOutcome(
        alignment=alignment,
        decision=decision,
        actions=actions,
        representations=list(representations),
        gaps=gaps,
        unresolved=unresolved,
        reason=_explain(alignment, representations, gaps),
    )


def _explain(alignment: AlignmentState, reps: list[Representation], gaps: list[Gap]) -> str:
    if alignment is AlignmentState.UNKNOWN:
        return "; ".join(f"{g.gap_id}: {g.reason}" for g in gaps) or "no representations found"
    if alignment is AlignmentState.ALIGNED:
        return f"{len(reps)} representation(s) agree"
    sides = ", ".join(f"{r.artifact}={r.value!r} ({r.locator})" for r in reps)
    if alignment is AlignmentState.DRIFT:
        newest = max(reps, key=lambda r: r.observed_at or "")
        return f"{sides}; newest is {newest.artifact} at {newest.observed_at}"
    return f"{sides}; no representation carries a recency marker that orders it against the others"
