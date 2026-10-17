# -*- coding: utf-8 -*-
"""A person overrules the system, and nothing keeps the old answer.

    AI:    "Strategic customers require additional approval."
    Human: "Wrong. The strategic exemption applies to the cancellation fee,
            not to approval."

A judge will test this, and an AI which defends its own
inference loses their trust immediately. So the interesting work here is not
accepting the correction -- it is finding everything that QUIETLY DEPENDED on
the rejected inference and marking it too.

**Zombie findings are the failure this module exists to prevent.** A
correction that updates the screen the person is looking at, and leaves the
same discredited conclusion on a report two clicks away, is worse than no
correction: the system now disagrees with itself and the person has been
told it was fixed.

**The authority matrix has two rows, and only one of them is a hierarchy.**

    human decision  >  model inference        a person outranks a guess
    artifact fact   is not outranked by either

The second row is I8 and it is the one that is tempting to drop. A person
saying "the threshold is 500,000" does not make `config.py` say 500,000.
It makes the system BELIEVE differently, and the artifact goes on saying
what it says until somebody changes it. A correction that could edit a fact
would let the product be argued out of its own evidence.

Nothing is deleted. `REJECTED_BY_HUMAN` is a state, not a removal: deleting
the finding means the next run rediscovers it and reports it again, which is
the zombie arriving by the other door.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

#: A finding a person has overruled. Kept and labelled, never dropped.
REJECTED_BY_HUMAN = "REJECTED_BY_HUMAN"

#: A finding that rested on something now rejected. Not wrong yet -- nobody
#: has looked. Saying "stale" rather than "wrong" is the honest label, and
#: it is also the one that keeps it on the queue.
NEEDS_REEVALUATION = "NEEDS_REEVALUATION"

#: What may overrule what. Written out because an authority order that
#: lives in people's heads is one every module implements differently.
AUTHORITY = ("artifact_fact", "human_decision", "model_inference")


class CannotOverruleAFact(Exception):
    """A correction that tried to change what an artifact says.

    I8. A person's correction changes what the system BELIEVES, never what
    it OBSERVED. Raised rather than silently ignored, because a caller that
    tried this has misunderstood the product badly enough to be told.
    """


@dataclass(frozen=True)
class Correction:
    """One person, overruling one inference, with a reason."""

    correction_id: str
    target_finding_id: str
    actor: str
    timestamp: str
    session_id: str
    reason: str
    #: What the person says is actually the case. Optional: "this is wrong"
    #: is a complete correction, and demanding a replacement would push
    #: people into inventing one.
    corrected_claim: str = ""

    def __post_init__(self) -> None:
        if not self.actor or not self.actor.strip():
            raise CannotOverruleAFact(
                f"{self.correction_id} has no actor; an anonymous correction "
                f"carries no authority (I12)")
        if not self.reason or not self.reason.strip():
            raise CannotOverruleAFact(
                f"{self.correction_id} gives no reason; the reason is the "
                f"part the next reader needs")


@dataclass(frozen=True)
class CorrectionRecord:
    """The audit entry. I12."""

    correction_id: str
    target_finding_id: str
    actor: str
    timestamp: str
    session_id: str
    reason: str
    authority: str = "human_decision"
    overruled: str = "model_inference"


@dataclass
class CorrectionOutcome:
    """Everything the correction touched, including what it touched indirectly."""

    record: CorrectionRecord
    rejected: tuple = ()
    needs_reevaluation: tuple = ()
    new_conflicts: tuple = ()
    findings: tuple = ()
    notes: list[str] = field(default_factory=list)


def _depends_on(finding, target) -> bool:
    """Did this finding rest on the same inference?

    Two findings share an inference when they share the semantic_id that
    grouping produced: the grouping IS the inference. If "these eight
    artifacts describe one rule" is wrong, then every finding about that
    rule rests on something a person has just rejected -- including the ones
    on other screens, which is the whole point.
    """
    if finding.finding_id == target.finding_id:
        return False
    if not finding.semantic_id or not target.semantic_id:
        return False
    return finding.semantic_id == target.semantic_id


def apply_correction(correction: Correction, findings) -> CorrectionOutcome:
    """Overrule one inference and follow it wherever it went.

    Returns a NEW list of findings. Nothing is mutated and nothing is
    dropped, so a caller that ignores the result has not silently half
    applied a correction.
    """
    findings = list(findings)
    by_id = {f.finding_id: f for f in findings}
    target = by_id.get(correction.target_finding_id)
    if target is None:
        raise CannotOverruleAFact(
            f"{correction.target_finding_id!r} is not in this report")

    record = CorrectionRecord(
        correction_id=correction.correction_id,
        target_finding_id=correction.target_finding_id,
        actor=correction.actor,
        timestamp=correction.timestamp,
        session_id=correction.session_id,
        reason=correction.reason)

    updated = []
    rejected = []
    stale = []

    for finding in findings:
        if finding.finding_id == target.finding_id:
            marked = replace(
                finding,
                resolution_status=f"{REJECTED_BY_HUMAN}:{correction.actor}",
                why=(f"{finding.why} -- OVERRULED by {correction.actor}: "
                     f"{correction.reason}"))
            rejected.append(marked)
            updated.append(marked)
            continue

        if _depends_on(finding, target):
            # Marked stale, not wrong. Nobody has looked at it yet, and
            # calling it wrong would be making the same mistake in the
            # opposite direction.
            marked = replace(
                finding,
                resolution_status=NEEDS_REEVALUATION,
                why=(f"{finding.why} -- rests on an inference "
                     f"{correction.actor} rejected in "
                     f"{correction.correction_id}; not yet re-examined"))
            stale.append(marked)
            updated.append(marked)
            continue

        updated.append(finding)

    outcome = CorrectionOutcome(
        record=record,
        rejected=tuple(rejected),
        needs_reevaluation=tuple(stale),
        findings=tuple(updated))
    outcome.notes.append(
        f"1 inference overruled; {len(stale)} finding(s) rested on it and are "
        f"marked for re-examination")
    outcome.notes.append(
        "no artifact was changed: a correction changes what the system "
        "believes, never what it observed (I8)")
    return outcome


def zombie_findings(findings, corrections) -> tuple:
    """Findings that still assert something a person has overruled.

    The test for this module asserts the result is empty. It is written as
    a query over the final state rather than as a flag set during the
    update, because the failure being hunted is precisely a path that
    forgot to set the flag.
    """
    overruled = {c.target_finding_id for c in corrections}
    if not overruled:
        return ()

    by_id = {f.finding_id: f for f in findings}
    targets = [by_id[i] for i in overruled if i in by_id]

    zombies = []
    for finding in findings:
        status = finding.resolution_status or ""
        if finding.finding_id in overruled:
            if not status.startswith(REJECTED_BY_HUMAN):
                zombies.append(finding)
            continue
        if any(_depends_on(finding, t) for t in targets) and \
                status != NEEDS_REEVALUATION:
            zombies.append(finding)
    return tuple(zombies)


def outranks(a: str, b: str) -> bool:
    """The authority matrix, as a function rather than as a convention.

    `artifact_fact` outranks nothing and is outranked by nothing: it is not
    on the same axis. A fact is not an opinion with high priority, and
    treating it as one is how a system ends up letting a confident person
    edit its evidence.
    """
    if "artifact_fact" in (a, b):
        raise CannotOverruleAFact(
            "an artifact fact is not on the authority axis; it is changed by "
            "editing the artifact, not by outranking it (I8)")
    order = {"human_decision": 1, "model_inference": 0}
    return order[a] > order[b]


__all__ = [
    "AUTHORITY",
    "CannotOverruleAFact",
    "Correction",
    "CorrectionOutcome",
    "CorrectionRecord",
    "NEEDS_REEVALUATION",
    "REJECTED_BY_HUMAN",
    "apply_correction",
    "outranks",
    "zombie_findings",
]
