# -*- coding: utf-8 -*-
"""The one door, and the signature required to open it.

Everything in this product exists to get to a moment where a named person
decides. This module is that moment, and the guarantee it makes is narrow
and absolute:

**There is exactly one function that writes.** `apply_changeset`. Not one
per artifact kind, not one convenience wrapper for tests. Several apply
paths means several places for the check to be missing from, and the one
that is missing it is the one that gets used. A test walks this package and
asserts no other function writes through an overlay.

**It writes to the session overlay, never to the baseline.** Apply produces
a new snapshot inside the session; the baseline hash is unchanged and reset
still works. This has a pleasant consequence: apply and reverse-drift use
the same write mechanism, and the ONLY difference between them is that apply
came through this gate. That difference is precisely what P8 looks for.

**Approval is a state transition with six fields, not a boolean.**

    actor  timestamp  changeset_id  proposal_version  decision  reason

`proposal_version` is the field that is easy to leave out and expensive to
have left out. Without it, "approved" is a flag on an object that can still
be edited, and the change that gets applied is not the change that was read.
With it, editing the proposal after approval changes the version, the
version no longer matches, and apply refuses. Nobody has to remember to
invalidate anything.

**Refusal raises.** Not a logged warning, not a False return that a caller
may ignore. A security boundary whose failure mode is a log line is a
boundary that has already been crossed by the time anybody reads the log.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence.changeset import ChangeSet, replay
from core.change_intelligence.states import ChangeSetState, transition
from core.change_intelligence.structured_diff import StaleDiff, apply_edits


class ApprovalRequired(Exception):
    """Something tried to apply a change nobody approved.

    FAILURE INJECTION target for Gate G7: remove the check that raises this
    and the suite must go red. A gate nobody has tested the absence of is
    an `if` statement, not a gate.
    """


class StaleApproval(Exception):
    """The proposal changed after it was approved."""


class IncompleteApproval(Exception):
    """An approval missing one of the six fields."""


class NotVerified(Exception):
    """Apply succeeded and nothing checked it. I14."""


#: The six. Absent any one, the approval is refused.
APPROVAL_FIELDS = ("actor", "timestamp", "changeset_id", "proposal_version",
                   "decision", "reason")

APPROVE = "approve"
REJECT = "reject"
REQUEST_CHANGES = "request_changes"
DECISIONS = (APPROVE, REJECT, REQUEST_CHANGES)


@dataclass(frozen=True)
class ApprovalRecord:
    """Who decided what, on which version of the proposal, and why."""

    actor: str
    timestamp: str
    changeset_id: str
    proposal_version: str
    decision: str
    reason: str

    def __post_init__(self) -> None:
        missing = [name for name in APPROVAL_FIELDS if not getattr(self, name)]
        if missing:
            raise IncompleteApproval(
                f"approval is missing {missing}; an approval without all six "
                f"cannot answer who authorised this, for which version, or "
                f"on what grounds (I12)")
        if self.decision not in DECISIONS:
            raise IncompleteApproval(
                f"{self.decision!r} is not one of {DECISIONS}")

    @property
    def is_approval(self) -> bool:
        return self.decision == APPROVE


def decide(changeset: ChangeSet, *, actor: str, timestamp: str,
           decision: str, reason: str) -> tuple:
    """Record a human decision and move the ChangeSet.

    Returns `(changeset, approval)`. The approval is bound to the version it
    was given: `changeset.proposal_version` is read HERE, at the moment of
    the decision, and never refreshed.
    """
    record = ApprovalRecord(
        actor=actor, timestamp=timestamp,
        changeset_id=changeset.changeset_id,
        proposal_version=changeset.proposal_version,
        decision=decision, reason=reason)

    target = (ChangeSetState.APPROVED if record.is_approval
              else ChangeSetState.REJECTED)
    moved = changeset.with_state(target, actor=actor, timestamp=timestamp,
                                 reason=reason)
    moved.approval = record
    return moved, record


# --- the checks, named so the failure injection has something to remove ------


def check_approved(changeset: ChangeSet) -> None:
    """The gate. Removing this call is the G7 mutation."""
    approval = changeset.approval
    if approval is None:
        raise ApprovalRequired(
            f"{changeset.changeset_id} is {changeset.state.value} and has no "
            f"approval; nothing may be applied without a named person "
            f"having decided")
    if not approval.is_approval:
        raise ApprovalRequired(
            f"{changeset.changeset_id} was not approved: {approval.actor} "
            f"chose {approval.decision!r} -- {approval.reason}")
    if changeset.state is not ChangeSetState.APPROVED:
        raise ApprovalRequired(
            f"{changeset.changeset_id} carries an approval but is in state "
            f"{changeset.state.value}; the record and the state disagree, "
            f"and a disagreement about authorisation is resolved by refusing")


def check_version(changeset: ChangeSet) -> None:
    """Approval of v1 does not authorise v2."""
    approval = changeset.approval
    if approval.proposal_version != changeset.proposal_version:
        raise StaleApproval(
            f"{approval.actor} approved version "
            f"{approval.proposal_version} of this proposal; it is now "
            f"{changeset.proposal_version}. The change that would be applied "
            f"is not the change that was read")


def check_snapshot(changeset: ChangeSet, *, current_snapshot: str) -> None:
    """The world must still be the one the proposal was computed against."""
    if current_snapshot and current_snapshot != changeset.base_snapshot:
        raise StaleDiff(
            f"this proposal was computed against {changeset.base_snapshot} "
            f"and the session is now at {current_snapshot}; the impact set "
            f"and the simulation describe a world that has moved")


@dataclass
class ApplyResult:
    changeset_id: str
    snapshot: object = None
    written: tuple = ()
    human_actions: tuple = ()
    events: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def requires_verification(self) -> bool:
        return "ChangeApplied" in self.events


# --- the only write in the package -------------------------------------------


def apply_changeset(changeset: ChangeSet, *, overlay, documents,
                    actor: str, timestamp: str,
                    serialise=None, validate=None) -> tuple:
    """The single apply path. Everything that writes goes through here.

    `documents` maps artifact path -> the parsed document to edit. Editing
    the parsed form rather than the bytes is what makes the change
    structured all the way down: the bytes are produced by `serialise` from
    a document that was validated after the edit, so an invalid file cannot
    be written even in principle.

    Returns `(changeset, ApplyResult)`. The ChangeSet comes back in state
    APPLIED, whose only outgoing edge is VERIFIED (I14) -- a successful
    apply that nothing checks afterwards cannot reach a finished state.
    """
    check_approved(changeset)
    check_version(changeset)
    check_snapshot(changeset, current_snapshot=getattr(
        overlay.head, "snapshot_id", ""))
    replay(changeset, through="approval")

    applying = changeset.with_state(
        ChangeSetState.APPLYING, actor=actor, timestamp=timestamp,
        reason="approved and being written to the session overlay")

    by_artifact: dict = {}
    for edit in changeset.system_edits():
        by_artifact.setdefault(edit.artifact, []).append(edit)

    # PHASE 1: validate + serialise EVERY artifact before anything is
    # staged. A missing artifact, a stale value, or a serialiser that
    # raises on the second (or Nth) artifact must not leave the first
    # already written -- SS8.4.1, review 14 F02.
    staged: dict = {}
    for artifact in sorted(by_artifact):
        document = documents.get(artifact)
        if document is None:
            raise StaleDiff(
                f"{artifact} is named by an approved edit but was not "
                f"supplied; applying part of an approved change is worse "
                f"than applying none of it")
        result = apply_edits(document, by_artifact[artifact],
                             validate=validate)
        raw = (serialise or _default_serialise)(artifact, result.after)
        staged[artifact] = raw

    # PHASE 2 + 3: every artifact validated and serialised. ONE call to
    # write_all mints exactly ONE new snapshot containing every staged
    # artifact -- not a loop of write() calls, which would advance the head
    # on the first artifact before a write-layer failure on a later one
    # could be caught.
    written = sorted(staged)
    snapshot = overlay.write_all(staged) if staged else overlay.head

    applied = applying.with_state(
        ChangeSetState.APPLIED, actor=actor, timestamp=timestamp,
        reason=f"{len(written)} artifact(s) written to the session overlay")
    applied.approval = changeset.approval

    actions = getattr(changeset.actions, "actions", ()) or ()
    human = tuple(a for a in actions if a.performer == "human")

    result = ApplyResult(
        changeset_id=changeset.changeset_id, snapshot=snapshot,
        written=tuple(written), human_actions=human,
        events=["ChangeApplied"])
    if human:
        result.notes.append(
            f"{len(human)} approved action(s) are carried out by a person "
            f"and were NOT written: "
            + ", ".join(f"{a.kind} {a.target}" for a in human))
    return applied, result


def _default_serialise(artifact: str, document) -> bytes:
    import json

    if artifact.endswith((".yaml", ".yml")):
        import yaml

        return yaml.safe_dump(document, sort_keys=False,
                              allow_unicode=True).encode("utf-8")
    return json.dumps(document, indent=2, sort_keys=True,
                      ensure_ascii=False).encode("utf-8")


def mark_verified(changeset: ChangeSet, *, actor: str, timestamp: str,
                  report) -> ChangeSet:
    """APPLIED -> VERIFIED, and only with a report. I14."""
    if not report:
        raise NotVerified(
            f"{changeset.changeset_id} was applied and nothing checked it; "
            f"APPLIED is deliberately not a terminal state")
    verified = changeset.with_state(
        ChangeSetState.VERIFIED, actor=actor, timestamp=timestamp,
        reason="verification completed")
    verified.approval = changeset.approval
    verified.verification = report
    return verified


__all__ = [
    "APPROVAL_FIELDS",
    "APPROVE",
    "ApplyResult",
    "ApprovalRecord",
    "ApprovalRequired",
    "DECISIONS",
    "IncompleteApproval",
    "NotVerified",
    "REJECT",
    "REQUEST_CHANGES",
    "StaleApproval",
    "apply_changeset",
    "check_approved",
    "check_snapshot",
    "check_version",
    "decide",
    "mark_verified",
]
