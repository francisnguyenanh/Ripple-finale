# -*- coding: utf-8 -*-
"""One version, one owner, one event that moves it.

D4 was recorded at P1 as *"lineage versioning is scaffolding -- code
declares that nothing increments it"*. This module closes it for the thing
Phase 8 actually needs, and states precisely what it does not close.

**What owns the version.** A session's `VerifiedLineage`. Not the artifact,
not the ChangeSet, not the Knowledge Model. The version describes how far
the session's VERIFIED state has advanced, and a counter owned by anything
smaller would have to be reconciled across things that move independently.

**What increments it.** Exactly one event: a verification that PASSED, on a
snapshot not already verified. Not apply, not approval, not a write.

    apply            -> new snapshot, version UNCHANGED
    verify, passed   -> version + 1
    verify, failed   -> version UNCHANGED, attempt recorded with its reason
    verify, again    -> version UNCHANGED (idempotent on snapshot_id)

Incrementing on apply would be the obvious choice and it is wrong: it would
mean the version advances for a change nobody has confirmed landed, which
is exactly the "declared rather than actual" defect this phase exists to
remove. A version that counts attempts is a version that lies about state.

**How parent and child are represented.** Each entry names the snapshot it
verified and that snapshot's parent. The chain is checked on the way in: an
entry whose parent is not the current verified head raises rather than
being appended, because a lineage with a gap in it cannot answer "what was
the verified state before this" -- which is the only question it exists to
answer.

**How verification refers to a snapshot.** By `snapshot_id`, which is
content-addressed and already deterministic (P1). Never by version number:
versions are a convenience for people reading a history, and resolving one
back to a state requires this table, whereas the snapshot id IS the state.

---

**What this does NOT close, and why.** `core/kernel/lineage.py` carries a
separate `DEFAULT_REF_VERSION` on document-pipeline artifact refs. It is
still `"1"`, still nothing increments it, and `test_no_consumer_reads_the_
ref_version` still holds that open. That is deliberate and it is not this
counter under another name -- the two describe different things:

    core/kernel/lineage.py   a REF to a document-pipeline artifact
    this module              how far a session's VERIFIED state has advanced

That module argues, correctly, that giving its version a value before
anything reads it would enlarge the problem rather than fix it, and that
the order is to give the version a reader first. Nothing here reads it, so
nothing here changes it. Two counters for two different things are not
competing counters; one counter forced to mean both would be.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The baseline is version 0 by definition: it is the reference every
#: session is compared against, and it was never "verified" because there
#: was nothing to verify it against.
BASELINE_VERSION = 0


class LineageMismatch(Exception):
    """An entry whose parent is not the current verified head.

    Raised rather than appended. A lineage with a gap cannot answer "what
    was the verified state before this", which is the only question it
    exists to answer.
    """


class NotDeterministic(Exception):
    """Something tried to put a non-deterministic value into the lineage."""


@dataclass(frozen=True)
class LineageEntry:
    """One verified state of a session, and how it got there."""

    version: int
    snapshot_id: str
    parent_snapshot_id: str | None
    changeset_id: str
    verified_by: str
    verified_at: str
    #: The artifacts whose content differs from the previous verified
    #: state. Recorded here rather than recomputed, so reading the history
    #: does not require the snapshots to still be reachable.
    artifacts: tuple = ()

    def __post_init__(self) -> None:
        missing = [name for name in ("snapshot_id", "verified_by", "verified_at")
                   if not getattr(self, name)]
        if missing:
            raise NotDeterministic(
                f"a lineage entry without {missing} cannot be attributed or "
                f"replayed (I12)")

    def rendered(self) -> str:
        return (f"v{self.version}  {self.snapshot_id[:12]}  "
                f"{self.changeset_id or '(no changeset)'}  "
                f"{len(self.artifacts)} artifact(s)  by {self.verified_by}")


@dataclass(frozen=True)
class FailedAttempt:
    """A verification that did not pass. Kept, because the reason is the point.

    Dropping these would make the lineage a list of successes, and "it has
    been tried three times and failed for the same reason" is exactly what
    a person needs to see before approving a fourth attempt.
    """

    snapshot_id: str
    changeset_id: str
    attempted_by: str
    attempted_at: str
    reason: str


@dataclass
class VerifiedLineage:
    """A session's verified history. The single owner of the version number."""

    session_id: str
    baseline_id: str
    entries: list = field(default_factory=list)
    failures: list = field(default_factory=list)

    # --- reading -------------------------------------------------------------

    @property
    def version(self) -> int:
        """How far the session's VERIFIED state has advanced."""
        return self.entries[-1].version if self.entries else BASELINE_VERSION

    @property
    def head_snapshot(self) -> str:
        """The snapshot the current version refers to, or "" at the baseline."""
        return self.entries[-1].snapshot_id if self.entries else ""

    def at(self, version: int) -> LineageEntry:
        for entry in self.entries:
            if entry.version == version:
                return entry
        raise KeyError(version)

    def entry_for(self, snapshot_id: str) -> LineageEntry | None:
        for entry in self.entries:
            if entry.snapshot_id == snapshot_id:
                return entry
        return None

    def is_verified(self, snapshot_id: str) -> bool:
        return self.entry_for(snapshot_id) is not None

    @property
    def chain(self) -> tuple:
        """`(version, snapshot_id)` oldest first, including the baseline."""
        return ((BASELINE_VERSION, ""),) + tuple(
            (e.version, e.snapshot_id) for e in self.entries)

    def failures_for(self, snapshot_id: str) -> tuple:
        return tuple(f for f in self.failures if f.snapshot_id == snapshot_id)

    # --- the one increment ---------------------------------------------------

    def record_verified(self, *, snapshot_id: str, parent_snapshot_id,
                        changeset_id: str, actor: str, timestamp: str,
                        artifacts=()) -> LineageEntry:
        """The ONLY thing in this codebase that advances the version.

        Idempotent on `snapshot_id`: verifying the same state again returns
        the existing entry and does not increment. A redelivered event, a
        retry, and a person clicking twice must not make the history claim
        two advances where one happened.
        """
        existing = self.entry_for(snapshot_id)
        if existing is not None:
            return existing

        head = self.head_snapshot
        if head and parent_snapshot_id != head:
            raise LineageMismatch(
                f"this verification's snapshot descends from "
                f"{parent_snapshot_id!r}, but the verified head is {head!r}; "
                f"appending it would leave a gap, and a lineage with a gap "
                f"cannot say what the verified state was before this")

        entry = LineageEntry(
            version=self.version + 1,
            snapshot_id=snapshot_id,
            parent_snapshot_id=parent_snapshot_id,
            changeset_id=changeset_id,
            verified_by=actor,
            verified_at=timestamp,
            artifacts=tuple(sorted(artifacts)))
        self.entries.append(entry)
        return entry

    def record_failure(self, *, snapshot_id: str, changeset_id: str,
                       actor: str, timestamp: str, reason: str) -> FailedAttempt:
        """A verification that did not pass. The version does not move."""
        if not reason:
            raise NotDeterministic(
                "a failed verification without a reason is indistinguishable "
                "from one nobody ran")
        attempt = FailedAttempt(
            snapshot_id=snapshot_id, changeset_id=changeset_id,
            attempted_by=actor, attempted_at=timestamp, reason=reason)
        self.failures.append(attempt)
        return attempt


def render(lineage: VerifiedLineage) -> str:
    """Deterministic from the same lineage."""
    out = [f"LINEAGE   {lineage.session_id}   v{lineage.version}",
           f"  v{BASELINE_VERSION}  (baseline {lineage.baseline_id[:12]})"]
    for entry in lineage.entries:
        out.append(f"  {entry.rendered()}")
    for failure in lineage.failures:
        out.append(f"  --  {failure.snapshot_id[:12]} NOT verified: "
                   f"{failure.reason}")
    return "\n".join(out)


__all__ = [
    "BASELINE_VERSION",
    "FailedAttempt",
    "LineageEntry",
    "LineageMismatch",
    "NotDeterministic",
    "VerifiedLineage",
    "render",
]
