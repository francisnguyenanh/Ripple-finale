# -*- coding: utf-8 -*-
"""A comment stops being text and becomes something with a lifecycle.

Gate G5 asks that at least four feedback classes produce genuinely DIFFERENT
downstream behaviour. A classifier that sorts remarks into seven bins and
then does the same thing with all seven has understood nothing, so the test
for this module asserts the outcomes DIFFER, not that they succeed.

The contract is `change_semantics.SEMANTICS`, written up front so that
implementation follows a table rather than inventing one.

Two architectural rules, and they are the reason this module returns a
REQUEST rather than performing an update:

**Feedback never reaches the Knowledge Model directly** (I8/I9). There is no
path from a person's remark to a Fact. `apply` produces a `FeedbackOutcome`
describing what SHOULD happen, and a caller with the authority to do it
carries it out. A remark that could write to the KM would let anyone assert
a fact by typing it, and the product's whole claim is that facts come from
artifacts.

**Everything is recorded, including the feedback that does nothing.** An
`observation` produces a ledger entry and no action. Dropping it would mean
the one kind of feedback a person gives most often leaves no trace, and
"nobody told us" becomes unanswerable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence.change_semantics import (
    SEMANTICS,
    Downstream,
    SemanticType,
)
from core.change_intelligence.evidence import EvidenceRef
from core.change_intelligence.states import MissingActorError, transition


class UnattributedFeedback(Exception):
    """Feedback with no actor or no timestamp. I12.

    The same rule the state machine applies to transitions, applied one step
    earlier: a remark nobody made is not evidence that anybody said anything.
    """


class FeedbackNotApplicable(Exception):
    """Feedback aimed at something that is not there.

    Raised rather than ignored. An `exception` marking a finding that does
    not exist is either a stale UI or a typo, and both are worth surfacing
    the moment they happen instead of at the next report.
    """


@dataclass(frozen=True)
class Feedback:
    feedback_id: str
    semantic_type: SemanticType
    text: str
    actor: str
    timestamp: str
    session_id: str
    #: What the remark is about. A finding, a rule, or neither -- an
    #: observation about the enterprise in general is still feedback.
    target_finding_id: str = ""
    target_semantic_id: str = ""
    #: Evidence the person supplied. Decisive for `clarification`: context
    #: with evidence can close a Gap, context without it cannot.
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if not self.actor or not self.actor.strip():
            raise UnattributedFeedback(f"{self.feedback_id} has no actor (I12)")
        if not self.timestamp or not self.timestamp.strip():
            raise UnattributedFeedback(f"{self.feedback_id} has no timestamp (I12)")
        if not isinstance(self.semantic_type, SemanticType):
            raise FeedbackNotApplicable(
                f"{self.semantic_type!r} is not a SemanticType; feedback with "
                f"no class cannot be routed")


@dataclass(frozen=True)
class LedgerEntry:
    """What was said, by whom, about what. Append-only."""

    feedback_id: str
    semantic_type: str
    downstream: str
    actor: str
    timestamp: str
    session_id: str
    text: str
    target: str = ""


@dataclass(frozen=True)
class IntentRequest:
    """A request that the future be different, before anything parses it.

    Carries the raw sentence, not a parsed value. Extraction is done by intent_parser and
    happens through a provider; this module must not guess at numbers, or
    the one type that opens a change branch would be opening it on a guess.
    """

    feedback_id: str
    text: str
    actor: str
    timestamp: str
    session_id: str
    target_semantic_id: str = ""


@dataclass(frozen=True)
class InferenceRejection:
    """A person saying an inference is wrong. The correction module acts on it."""

    feedback_id: str
    target_finding_id: str
    reason: str
    actor: str
    timestamp: str


@dataclass(frozen=True)
class IntentionalMark:
    """The divergence is real AND deliberate.

    Marked, never deleted. Deleting the finding means the next run
    rediscovers it and reports it again -- the zombie.
    """

    feedback_id: str
    target_finding_id: str
    reason: str
    actor: str
    timestamp: str


@dataclass(frozen=True)
class GapResolution:
    """A gap a clarification touched, and whether it may close.

    `closes` is False far more often than a reader expects, and the `why`
    is the useful half: context that is not itself evidence-backed leaves
    the gap open and says so.
    """

    gap_id: str
    closes: bool
    why: str


@dataclass
class FeedbackOutcome:
    """What this feedback causes. Nothing here has happened yet.

    The caller applies it. Returning a description rather than performing
    the update is what keeps the path from a person's remark to a Fact
    closed (I8).
    """

    feedback_id: str
    downstream: Downstream
    ledger: LedgerEntry
    intent_request: IntentRequest | None = None
    inference_rejection: InferenceRejection | None = None
    intentional_mark: IntentionalMark | None = None
    gap_resolutions: tuple[GapResolution, ...] = ()
    transitions: tuple = ()
    notes: list[str] = field(default_factory=list)

    def touches_knowledge_model(self) -> bool:
        """Always False, and asserted by a test rather than trusted.

        Kept as a method so the guarantee is stated in the type a caller
        holds, not only in this docstring.
        """
        return False


def _ledger_entry(feedback: Feedback) -> LedgerEntry:
    return LedgerEntry(
        feedback_id=feedback.feedback_id,
        semantic_type=feedback.semantic_type.value,
        downstream=SEMANTICS[feedback.semantic_type].downstream.value,
        actor=feedback.actor,
        timestamp=feedback.timestamp,
        session_id=feedback.session_id,
        text=feedback.text,
        target=feedback.target_finding_id or feedback.target_semantic_id)


def apply(feedback: Feedback, *, findings=(), gaps=(),
          state=None, target_state=None) -> FeedbackOutcome:
    """Route one piece of feedback to its downstream effect.

    `findings` and `gaps` are read, never written. `state`/`target_state`
    are only meaningful for approval and rejection, which are the two types
    that move a state machine.
    """
    semantics = SEMANTICS[feedback.semantic_type]
    outcome = FeedbackOutcome(
        feedback_id=feedback.feedback_id,
        downstream=semantics.downstream,
        ledger=_ledger_entry(feedback))

    by_id = {getattr(f, "finding_id", ""): f for f in findings}

    if feedback.semantic_type is SemanticType.OBSERVATION:
        # Deliberately the shortest branch in the module. A system that acts
        # on every remark cannot be talked to.
        outcome.notes.append("recorded; an observation triggers nothing by design")
        return outcome

    if feedback.semantic_type is SemanticType.CORRECTION:
        finding = _require_target(feedback, by_id)
        outcome.inference_rejection = InferenceRejection(
            feedback_id=feedback.feedback_id,
            target_finding_id=finding.finding_id,
            reason=feedback.text,
            actor=feedback.actor,
            timestamp=feedback.timestamp)
        outcome.notes.append(
            "the inference is rejected and the decision passes to a person; "
            "the FACT it was drawn from is untouched (I8)")
        return outcome

    if feedback.semantic_type is SemanticType.CLARIFICATION:
        outcome.gap_resolutions = _resolve_gaps(feedback, gaps)
        closing = [g for g in outcome.gap_resolutions if g.closes]
        outcome.notes.append(
            f"{len(closing)} of {len(outcome.gap_resolutions)} touched gaps may close")
        return outcome

    if feedback.semantic_type is SemanticType.INTENT:
        outcome.intent_request = IntentRequest(
            feedback_id=feedback.feedback_id,
            text=feedback.text,
            actor=feedback.actor,
            timestamp=feedback.timestamp,
            session_id=feedback.session_id,
            target_semantic_id=feedback.target_semantic_id)
        outcome.notes.append(
            "opens intent -> impact -> shadow -> proposal; the sentence is "
            "carried unparsed, because extraction happens outside core (I11)")
        return outcome

    if feedback.semantic_type is SemanticType.EXCEPTION:
        finding = _require_target(feedback, by_id)
        outcome.intentional_mark = IntentionalMark(
            feedback_id=feedback.feedback_id,
            target_finding_id=finding.finding_id,
            reason=feedback.text,
            actor=feedback.actor,
            timestamp=feedback.timestamp)
        outcome.notes.append(
            "the finding is CORRECT and the divergence is deliberate; marked "
            "intentional and kept, because deleting it means the next run "
            "reports it again")
        return outcome

    # APPROVAL and REJECTION. They share a Downstream on purpose: they are
    # the same kind of act in opposite directions, and inventing a seventh
    # outcome to make Gate G5's count look better would be padding it.
    if state is None or target_state is None:
        raise FeedbackNotApplicable(
            f"{feedback.semantic_type.value} moves a state machine and no "
            f"state was supplied")
    try:
        moved = transition(state, target_state,
                           actor=feedback.actor, timestamp=feedback.timestamp,
                           reason=feedback.text)
    except MissingActorError:
        raise
    outcome.transitions = (moved,)
    outcome.notes.append(f"{state.name} -> {target_state.name} by {feedback.actor}")
    return outcome


def _require_target(feedback: Feedback, by_id: dict):
    finding = by_id.get(feedback.target_finding_id)
    if finding is None:
        raise FeedbackNotApplicable(
            f"{feedback.semantic_type.value} names finding "
            f"{feedback.target_finding_id!r}, which is not in this report")
    return finding


def _resolve_gaps(feedback: Feedback, gaps) -> tuple[GapResolution, ...]:
    """A clarification closes a gap only when it brings evidence.

    This is the branch most likely to be written the easy way. "The
    threshold is measured before tax" is useful context and, on its own, is
    one more person's assertion -- closing a Gap with it converts an opinion
    into a settled fact, which is exactly the move the product exists to
    catch other systems making.

    So: with evidence, the gap MAY close; without, it stays open and the
    clarification is attached to it as context.
    """
    touched = [g for g in gaps
               if not feedback.target_semantic_id
               or feedback.target_semantic_id in getattr(g, "gap_id", "")]
    has_evidence = bool(feedback.evidence)

    return tuple(GapResolution(
        gap_id=getattr(gap, "gap_id", ""),
        closes=has_evidence,
        why=("clarification cites evidence, so the gap may close"
             if has_evidence else
             "clarification cites no evidence; recorded as context and the "
             "gap stays open -- closing it would turn one person's statement "
             "into a settled fact"))
        for gap in touched)


def distinct_outcomes(outcomes) -> set:
    """How many genuinely different effects a batch produced.

    Gate G5 counts this. Compared on the EFFECT rather than on the label,
    because two outcomes that differ only in which enum they carry are the
    thing the gate exists to rule out.
    """
    signatures = set()
    for outcome in outcomes:
        signatures.add((
            outcome.downstream,
            outcome.intent_request is not None,
            outcome.inference_rejection is not None,
            outcome.intentional_mark is not None,
            bool(outcome.gap_resolutions),
            bool(outcome.transitions),
        ))
    return signatures


__all__ = [
    "Feedback",
    "FeedbackNotApplicable",
    "FeedbackOutcome",
    "GapResolution",
    "InferenceRejection",
    "IntentRequest",
    "IntentionalMark",
    "LedgerEntry",
    "UnattributedFeedback",
    "apply",
    "distinct_outcomes",
]
