# -*- coding: utf-8 -*-
"""The state vocabulary, and the only place it is defined.

Four machines and two flat state sets. Every module in P4-P8 imports from
here; no module is allowed to restate an enum member of its own, because a
second definition is how two halves of a system come to disagree about what
APPROVED means without anyone noticing.

The distinction this module exists to protect:

    AlignmentState   describes what REALITY looks like   (4 members)
    ChangeDecision   describes the CONSEQUENCE for action (3 members)

They are perpendicular axes. `NO_CHANGE_REQUIRED` is not, and can never be,
an AlignmentState -- see `alignment.py` and docs/domain/alignment_semantics.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TransitionError(Exception):
    """A state transition that the contract forbids.

    Carries both ends so a failure-injection test can assert WHICH edge was
    refused, not merely that something raised.
    """

    def __init__(self, machine: str, source: object, target: object, reason: str = "") -> None:
        self.machine = machine
        self.source = source
        self.target = target
        super().__init__(
            f"{machine}: {getattr(source, 'name', source)} -> "
            f"{getattr(target, 'name', target)} is not a permitted transition"
            + (f" ({reason})" if reason else "")
        )


class MissingActorError(Exception):
    """A transition without an actor or a timestamp. I12.

    An approval nobody signed is not an approval, so the audit fields are
    part of the transition itself rather than something written alongside it.
    """


# --- the machines -----------------------------------------------------------


class KnowledgeState(Enum):
    PROPOSED = "PROPOSED"
    VERIFIED = "VERIFIED"
    SUPERSEDED = "SUPERSEDED"


class IntentState(Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    SUPERSEDED = "SUPERSEDED"
    #: An intent that failed validation is still STORED, never discarded
    #: Losing the record of a rejected intent loses the reason it
    #: was rejected, which is the part a person needs.
    REJECTED = "REJECTED"


class ChangeSetState(Enum):
    PROPOSED = "PROPOSED"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


# --- the two flat axes ------------------------------------------------------


class AlignmentState(Enum):
    """What reality looks like. Exactly four members -- a test counts them."""

    ALIGNED = "ALIGNED"
    DRIFT = "DRIFT"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class ChangeDecision(Enum):
    """What follows for action. Exactly three members -- a test counts them."""

    NO_CHANGE_REQUIRED = "NO_CHANGE_REQUIRED"
    CHANGE_REQUIRED = "CHANGE_REQUIRED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


# --- permitted edges --------------------------------------------------------
#
# Written as the complete set of what IS allowed. Everything absent is
# forbidden, and the tests assert over the Cartesian product rather than over
# a hand-written list of bad edges -- a list of forbidden transitions is only
# ever as complete as the person who wrote it was imaginative.

KNOWLEDGE_TRANSITIONS: dict[KnowledgeState, frozenset[KnowledgeState]] = {
    KnowledgeState.PROPOSED: frozenset({KnowledgeState.VERIFIED, KnowledgeState.SUPERSEDED}),
    KnowledgeState.VERIFIED: frozenset({KnowledgeState.SUPERSEDED}),
    KnowledgeState.SUPERSEDED: frozenset(),
}

INTENT_TRANSITIONS: dict[IntentState, frozenset[IntentState]] = {
    IntentState.DRAFT: frozenset({IntentState.VALIDATED, IntentState.REJECTED}),
    #: VALIDATED -> SUPERSEDED exists because
    #: the gap showed up: a person revises a request far more often BEFORE it is
    #: approved than after. Without this edge the only way out of VALIDATED
    #: is REJECTED, so a revision would be recorded as a rejection -- and
    #: `status_reason` is the one field somebody reads to learn why an
    #: intent is no longer live. Mislabelling "they changed their mind" as
    #: "it failed validation" corrupts exactly that answer.
    IntentState.VALIDATED: frozenset({IntentState.APPROVED, IntentState.REJECTED,
                                      IntentState.SUPERSEDED}),
    IntentState.APPROVED: frozenset({IntentState.APPLIED, IntentState.SUPERSEDED}),
    IntentState.APPLIED: frozenset({IntentState.SUPERSEDED}),
    IntentState.REJECTED: frozenset({IntentState.SUPERSEDED}),
    IntentState.SUPERSEDED: frozenset(),
}

CHANGESET_TRANSITIONS: dict[ChangeSetState, frozenset[ChangeSetState]] = {
    ChangeSetState.PROPOSED: frozenset({ChangeSetState.REVIEW, ChangeSetState.REJECTED}),
    ChangeSetState.REVIEW: frozenset({ChangeSetState.APPROVED, ChangeSetState.REJECTED}),
    ChangeSetState.APPROVED: frozenset({ChangeSetState.APPLYING, ChangeSetState.REJECTED}),
    ChangeSetState.APPLYING: frozenset({ChangeSetState.APPLIED, ChangeSetState.REJECTED}),
    #: APPLIED -> VERIFIED is the ONLY way out of APPLIED. I14: every
    #: successful apply must trigger verification, so APPLIED is deliberately
    #: not a terminal state.
    ChangeSetState.APPLIED: frozenset({ChangeSetState.VERIFIED}),
    ChangeSetState.VERIFIED: frozenset(),
    ChangeSetState.REJECTED: frozenset(),
}

_MACHINES: dict[str, dict] = {
    "Knowledge": KNOWLEDGE_TRANSITIONS,
    "Intent": INTENT_TRANSITIONS,
    "ChangeSet": CHANGESET_TRANSITIONS,
}

#: Terminal states, stated rather than inferred, so "APPLIED has no outgoing
#: edge" can be distinguished from "APPLIED is finished".
TERMINAL_STATES = frozenset(
    {
        KnowledgeState.SUPERSEDED,
        IntentState.SUPERSEDED,
        ChangeSetState.VERIFIED,
        ChangeSetState.REJECTED,
    }
)


def _machine_of(state: Enum) -> tuple[str, dict]:
    for name, table in _MACHINES.items():
        if type(state) is type(next(iter(table))):
            return name, table
    raise TypeError(f"{state!r} does not belong to a state machine defined here")


def can_transition(source: Enum, target: Enum) -> bool:
    """True only for an edge that appears in the machine's table."""
    if type(source) is not type(target):
        return False
    _, table = _machine_of(source)
    return target in table[source]


@dataclass(frozen=True)
class Transition:
    """A state change with the audit record that makes it real. I12."""

    machine: str
    source: Enum
    target: Enum
    actor: str
    timestamp: str
    reason: str = ""


def transition(source: Enum, target: Enum, *, actor: str, timestamp: str, reason: str = "") -> Transition:
    """Perform a state transition, or refuse.

    Refuses on two separate grounds, which are worth keeping separate: the
    edge may be forbidden (TransitionError), or the edge may be permitted but
    unattributed (MissingActorError). A system that reports both as "invalid"
    cannot tell a design mistake from a missing signature.
    """
    machine, table = _machine_of(source)
    if type(source) is not type(target):
        raise TransitionError(machine, source, target, "different state machines")
    if target not in table[source]:
        raise TransitionError(machine, source, target)
    if not actor or not str(actor).strip():
        raise MissingActorError(
            f"{machine}: {source.name} -> {target.name} has no actor; a transition "
            f"nobody made is not an audit record (I12)"
        )
    if not timestamp or not str(timestamp).strip():
        raise MissingActorError(
            f"{machine}: {source.name} -> {target.name} has no timestamp (I12)"
        )
    return Transition(
        machine=machine,
        source=source,
        target=target,
        actor=actor,
        timestamp=timestamp,
        reason=reason,
    )
