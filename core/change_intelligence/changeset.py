# -*- coding: utf-8 -*-
"""One object a person can approve a multi-artifact change from.

Eleven fields, and each one is there because a reviewer asks for it:

    trigger             what started this
    intent              what a person said they wanted, at which version
    base_snapshot       the world this was computed against
    affected_nodes      what moves, tiered
    affected_artifacts  the files
    evidence            the quotes behind every claim
    simulation_result   what running it showed
    actions             what would be done, grouped by necessity
    risk                the deterministic assessment
    approval            who decided, when, on which version
    verification        what was checked afterwards

`simulation_result` is the one that distinguishes this from a task list.
Without it a ChangeSet says "here are eleven files to edit" and the reviewer
has to take that on trust. With it, the reviewer sees what would actually
happen to the requests the business processes -- which is the thing they are
qualified to judge, and the list of files is not.

**The id is content-addressed over the proposal.** That is not a tidiness
choice, it is how stale approval is caught: an approval names the
`proposal_version` it was given, so revising the proposal after approval
produces a different version and the old approval no longer matches. The
alternative -- remembering to invalidate approvals on edit -- is a rule
somebody eventually forgets, and this is a fact nobody can forget.

**Replay.** From the id alone, the whole chain must come back:

    base_snapshot -> trigger -> intent -> impact -> simulation
      -> proposal -> approval -> execution -> verification

A ChangeSet whose chain has a hole cannot answer "why was this done", which
is the question an audit actually asks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace

from core.change_intelligence.states import ChangeSetState


class IncompleteChangeSet(Exception):
    """A ChangeSet missing what makes it reviewable or replayable."""


class BrokenChain(Exception):
    """Replay found a hole. The 'why' cannot be reconstructed."""


#: The eleven, in the order the review screen reads them. A test asserts
#: the dataclass carries exactly these.
FIELDS = (
    "trigger", "intent", "base_snapshot", "affected_nodes",
    "affected_artifacts", "evidence", "simulation_result", "actions",
    "risk", "approval", "verification",
)

#: The parts that define WHAT IS BEING PROPOSED. Approval is taken against
#: a hash of exactly these -- not of the whole object, because a ChangeSet
#: moving from REVIEW to APPROVED must not change the version it was
#: approved at.
_PROPOSAL_PARTS = ("trigger", "intent", "base_snapshot",
                   "affected_artifacts", "actions")


@dataclass
class ChangeSet:
    """A coordinated change across artifacts, waiting for a person."""

    trigger: str
    base_snapshot: str
    intent: object = None
    affected_nodes: tuple = ()
    affected_artifacts: tuple = ()
    evidence: tuple = ()
    simulation_result: object = None
    actions: object = None
    risk: object = None
    approval: object = None
    verification: object = None

    state: ChangeSetState = ChangeSetState.PROPOSED
    session_id: str = ""
    created_by: str = ""
    created_at: str = ""
    history: list = field(default_factory=list)

    def __post_init__(self) -> None:
        missing = [name for name in ("trigger", "base_snapshot")
                   if not getattr(self, name)]
        if missing:
            raise IncompleteChangeSet(
                f"a ChangeSet without {missing} cannot be replayed; a "
                f"proposal that does not name the world it was computed "
                f"against describes a change to nothing in particular (I17)")

    # --- identity ------------------------------------------------------------

    def proposal_payload(self) -> dict:
        """Exactly what a person is being asked to approve."""
        actions = getattr(self.actions, "actions", ()) or ()
        return {
            "trigger": self.trigger,
            "base_snapshot": self.base_snapshot,
            "intent": _intent_key(self.intent),
            "affected_artifacts": sorted(self.affected_artifacts),
            "actions": sorted(
                f"{a.kind}|{a.target}|{a.necessity}|"
                + "|".join(sorted(e.rendered() for e in a.edits))
                for a in actions),
        }

    @property
    def proposal_version(self) -> str:
        """Changes whenever the proposal changes. Never otherwise."""
        payload = json.dumps(self.proposal_payload(), sort_keys=True,
                             default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def changeset_id(self) -> str:
        return f"CHG-{self.proposal_version[:12]}"

    # --- reading -------------------------------------------------------------

    @property
    def required_actions(self) -> tuple:
        actions = getattr(self.actions, "actions", ()) or ()
        return tuple(a for a in actions if a.necessity == "required")

    @property
    def introduced_contradictions(self) -> tuple:
        if self.simulation_result is None:
            return ()
        return tuple(self.simulation_result.new_inconsistencies())

    @property
    def inherited_contradictions(self) -> tuple:
        """Reported, and never attributed to this change (P6 freeze, item 2)."""
        if self.simulation_result is None:
            return ()
        return tuple(self.simulation_result.inherited_inconsistencies())

    def system_edits(self) -> tuple:
        """Every structured edit the system would apply after approval.

        Actions a person carries out are deliberately excluded: they are
        listed on the screen, tracked, and verified, but the apply path
        must not pretend to have done them.
        """
        actions = getattr(self.actions, "actions", ()) or ()
        edits = []
        for action in actions:
            if action.performer == "system":
                edits.extend(action.edits)
        return tuple(edits)

    def with_state(self, state: ChangeSetState, *, actor: str,
                   timestamp: str, reason: str = "") -> "ChangeSet":
        """A NEW ChangeSet in the new state, with the move recorded. I12."""
        from core.change_intelligence.states import transition

        move = transition(self.state, state, actor=actor,
                          timestamp=timestamp, reason=reason)
        moved = replace(self, state=state, history=list(self.history) + [move])
        return moved


def _intent_key(intent) -> str:
    """Id AND version. An intent revised to v2 is a different request."""
    if intent is None:
        return ""
    identifier = getattr(intent, "intent_id", "") or getattr(intent, "id", "")
    version = getattr(intent, "version", 0)
    return f"{identifier}@v{version}" if identifier else ""


# --- replay -------------------------------------------------------------------

#: Every link the chain must have, and what supplies it. Named here rather
#: than checked inline so the test and the check read the same list.
CHAIN = (
    ("base_snapshot", "base_snapshot"),
    ("trigger", "trigger"),
    ("intent", "intent"),
    ("impact", "affected_nodes"),
    ("simulation", "simulation_result"),
    ("proposal", "actions"),
    ("approval", "approval"),
    ("verification", "verification"),
)


def replay(changeset: ChangeSet, *, through: str = "proposal") -> tuple:
    """Walk the chain, stopping where the ChangeSet has got to.

    `through` exists because a ChangeSet under review genuinely has no
    approval yet, and reporting that as a broken chain would make the check
    useless at the only moment somebody looks at it. What it must never do
    is let a hole EARLIER than `through` pass.
    """
    links = []
    for name, attribute in CHAIN:
        value = getattr(changeset, attribute, None)
        present = bool(value) if not isinstance(value, (int, float)) else True
        links.append((name, present))
        if name == through:
            break

    holes = [name for name, present in links if not present]
    if holes:
        raise BrokenChain(
            f"{changeset.changeset_id} cannot be replayed: {holes} missing. "
            f"A ChangeSet with a hole in its chain cannot answer 'why was "
            f"this done', which is the question an audit asks")
    return tuple(links)


def summary(changeset: ChangeSet) -> str:
    actions = getattr(changeset.actions, "actions", ()) or ()
    return (f"{changeset.changeset_id}  {changeset.state.value}  "
            f"{len(actions)} action(s), "
            f"{len(changeset.required_actions)} required, "
            f"risk {getattr(changeset.risk, 'level', 'UNKNOWN')}")


__all__ = [
    "BrokenChain",
    "CHAIN",
    "ChangeSet",
    "FIELDS",
    "IncompleteChangeSet",
    "replay",
    "summary",
]
