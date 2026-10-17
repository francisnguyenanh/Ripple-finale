# -*- coding: utf-8 -*-
"""Turning an ambiguous finding into a question somebody can answer.

Two questions are asked, in this order:

    "Is this intentional?"                    -> yes / no / unclear
    "Which source represents current intent?" -> one of the representations

The second is the one that matters. The first only labels a finding; the
second converts a CONFLICT into an `Intent` that P5 can act on, and it is
the only bridge between "the enterprise disagrees with itself" and "here is
what to change".

Three things this module refuses to do, and each refusal is the design:

**It never answers on anyone's behalf.** There is no default actor, no
generated timestamp, no inferred choice. `HumanDecision` already rejects a
record missing its attribution (I12), and every answer here becomes one.

**It offers a way out of its own question.** The options for question two
are the representations and nothing else -- a reader cannot pick a source
that is not in evidence -- but `NONE_OF_THESE` is always available. A
question whose options are all wrong, and which must be answered anyway,
manufactures a decision; what it produces is worse than no answer, because
it carries a person's name. Choosing it records a Gap (I5).

**A confirmed intent is a DECISION, not a fact.** `ConfirmedIntent` carries
who decided and when, and the product goes on reporting the conflict
underneath it. A person saying the spreadsheet is right does not make the
workflow agree with it; it records that somebody accountable said so, which
is a different and more useful thing to carry into P7.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.change_intelligence.alignment import Gap
from core.change_intelligence.discovery import (
    Finding,
    HumanDecision,
    UnresolvedWithoutHuman,
)

#: The escape hatch on question two. Not a refusal to engage -- it is the
#: answer "the current intent is not any of these", which is a real state of
#: the world and the one a forced choice destroys.
NONE_OF_THESE = "none_of_these"

INTENTIONAL = ("yes", "no", "unclear")

Q_INTENTIONAL = "is_this_intentional"
Q_CURRENT_INTENT = "which_source_represents_current_intent"


@dataclass(frozen=True)
class Option:
    """One answer a person can give, and what it points at."""

    value: str
    label: str
    artifact: str = ""
    locator: str = ""
    states: object = None


@dataclass(frozen=True)
class Question:
    question_id: str
    text: str
    options: tuple[Option, ...]

    def option(self, value: str) -> Option:
        for option in self.options:
            if option.value == value:
                return option
        raise UnansweredQuestion(
            f"{value!r} is not an option for {self.question_id}; "
            f"the options are {[o.value for o in self.options]}")


class UnansweredQuestion(Exception):
    """An answer that does not correspond to anything that was asked.

    Raised rather than coerced. A UI that can submit a value the question
    never offered is a UI whose audit trail records answers nobody gave.
    """


@dataclass(frozen=True)
class ConfirmedIntent:
    """What a person said the current intent is.

    Deliberately not called `Resolution`. Nothing about the artifacts
    changed when this was created -- the conflict is still there, still
    reported, still evidenced. This records an accountable human position
    on it, which is what P5 needs in order to propose anything at all.
    """

    finding_id: str
    semantic_id: str
    artifact: str
    locator: str
    value: object
    decided_by: str
    decided_at: str
    session_id: str
    rationale: str = ""


@dataclass
class ConfirmationOutcome:
    """Everything one round of answering produced.

    The finding comes back CHANGED rather than mutated, and the audit
    records come back alongside it, because a caller that forgets to persist
    them should not also be the caller that keeps the resolution.
    """

    finding: Finding
    intent: ConfirmedIntent | None = None
    decisions: tuple[HumanDecision, ...] = ()
    gaps: tuple[Gap, ...] = ()


# --- asking --------------------------------------------------------------------


def questions_for(finding: Finding) -> tuple[Question, Question]:
    """The two questions, with the second one's options taken from evidence.

    Options are built from the finding's own claims, so every choice a
    person can make is a source they can open and read. An option list that
    included anything else would be asking someone to endorse a claim the
    product cannot show them.
    """
    seen: set[tuple[str, str]] = set()
    options = []
    for claim in sorted(finding.claims, key=lambda c: (c.artifact, c.locator)):
        key = (claim.artifact, claim.locator)
        if key in seen:
            continue
        seen.add(key)
        stated = "states no value" if claim.value is None else f"states {claim.value}"
        options.append(Option(
            value=f"{claim.artifact}#{claim.locator}",
            label=f"{claim.artifact} ({claim.family}) {stated}",
            artifact=claim.artifact,
            locator=claim.locator,
            states=claim.value))

    options.append(Option(
        value=NONE_OF_THESE,
        label="none of these represents the current intent"))

    return (
        Question(
            question_id=Q_INTENTIONAL,
            text="Is this intentional?",
            options=tuple(Option(value=v, label=v) for v in INTENTIONAL)),
        Question(
            question_id=Q_CURRENT_INTENT,
            text="Which source represents current intent?",
            options=tuple(options)),
    )


# --- answering ------------------------------------------------------------------


def confirm(finding: Finding, *, actor: str, timestamp: str, session_id: str,
            intentional: str, current_intent: str = "",
            reason: str = "") -> ConfirmationOutcome:
    """Record one person's answers to both questions.

    `timestamp` is supplied, never generated here. A clock reading invented
    inside the core would make the audit record a statement about when this
    function ran rather than about when a person decided, and the two are
    not the same event.
    """
    _, intent_q = questions_for(finding)

    if intentional not in INTENTIONAL:
        raise UnansweredQuestion(
            f"{intentional!r} is not an answer to {Q_INTENTIONAL}; "
            f"the answers are {list(INTENTIONAL)}")

    decisions: list[HumanDecision] = []
    gaps: list[Gap] = []

    # Question 1. `unclear` is an answer, not a skip: it produces a Gap (I5)
    # and the finding stays open. Treating it as "no response" would let the
    # hardest cases quietly leave the queue.
    decisions.append(HumanDecision(
        actor=actor, timestamp=timestamp, finding_id=finding.finding_id,
        answer=f"{Q_INTENTIONAL}={intentional}",
        reason=reason, session_id=session_id))

    if intentional == "unclear":
        gaps.append(Gap(
            gap_id=f"GAP-{finding.finding_id}-INTENT",
            reason=(f"{actor} could not say whether this is intentional"
                    + (f": {reason}" if reason else ""))))

    intent = None
    if current_intent:
        option = intent_q.option(current_intent)
        decisions.append(HumanDecision(
            actor=actor, timestamp=timestamp, finding_id=finding.finding_id,
            answer=f"{Q_CURRENT_INTENT}={current_intent}",
            reason=reason, session_id=session_id))

        if option.value == NONE_OF_THESE:
            # The most informative answer available, and the one a forced
            # choice would have hidden: every representation the product
            # found is wrong, so the rule lives somewhere nothing has read.
            gaps.append(Gap(
                gap_id=f"GAP-{finding.finding_id}-SOURCE",
                reason=(f"{actor} says no represented source carries the "
                        f"current intent for {finding.semantic_id}"
                        + (f": {reason}" if reason else ""))))
        else:
            intent = ConfirmedIntent(
                finding_id=finding.finding_id,
                semantic_id=finding.semantic_id,
                artifact=option.artifact,
                locator=option.locator,
                value=option.states,
                decided_by=actor,
                decided_at=timestamp,
                session_id=session_id,
                rationale=reason)

    updated = replace(finding, gaps=finding.gaps + tuple(gaps))
    if intent is not None:
        # The status names the person. "RESOLVED" with no name is how a
        # decision becomes a property of the system rather than of whoever
        # made it, and six months later nobody can be asked why.
        updated = replace(updated, resolution_status=f"RESOLVED_BY:{actor}")

    return ConfirmationOutcome(finding=updated, intent=intent,
                               decisions=tuple(decisions), gaps=tuple(gaps))


def unanswered(findings) -> tuple[Finding, ...]:
    """Findings still waiting on a person.

    Exists so "how many are open" is computed from the findings themselves
    rather than tracked in a counter beside them, which is how the two stop
    agreeing.
    """
    return tuple(f for f in findings
                 if not f.resolution_status.startswith("RESOLVED_BY:"))


__all__ = [
    "ConfirmationOutcome",
    "ConfirmedIntent",
    "NONE_OF_THESE",
    "Option",
    "Q_CURRENT_INTENT",
    "Q_INTENTIONAL",
    "Question",
    "UnansweredQuestion",
    "UnresolvedWithoutHuman",
    "confirm",
    "questions_for",
    "unanswered",
]
