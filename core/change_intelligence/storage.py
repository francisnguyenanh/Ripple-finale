# -*- coding: utf-8 -*-
"""The six storage contracts, as protocols and nothing else.

Taken from ADR-003, which the spike proved by running it. Defined here so
that `core/change_intelligence/` never names a database or a filesystem: the
whole point of the two-tier architecture (ADR-001) is that tier 2 can be
served from Cloud Run without knowing what is behind these interfaces.

An architecture test greps this package for `sqlite3`, `open(` and `Path(`
and expects nothing. That test is why these are Protocols and not base
classes with a default implementation -- a default implementation would have
to pick a backend, and picking one here is the mistake being prevented.

    BaselineArtifactStore   READ-ONLY    immutable baseline          (tier 2)
    SessionArtifactOverlay  read-write   session-local changes       (tier 2)
    EvidenceStore           READ-ONLY    evidence snapshots          (tier 2)
    GraphStore              READ-ONLY    Enterprise Graph at runtime (tier 2)
    StateStore              read-write   intents/changesets/approvals(tier 2)
    SessionStore            read-write   judge session isolation     (tier 2)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class BaselineImmutableError(Exception):
    """Something tried to write through the baseline. I18.

    The baseline exposes a `write` that always raises rather than no write
    at all: a store with no write method is a promise, and a store whose
    write refuses is a testable fact.
    """


class SessionScopeError(Exception):
    """An overlay query without a session_id. I18.

    Every overlay read and write is scoped to one session. A query that
    forgets the scope does not fail loudly on its own -- it quietly returns
    another judge's data -- so the scope is checked rather than trusted.
    """


@runtime_checkable
class Snapshot(Protocol):
    """An immutable, addressable view of reality at one point in a session."""

    snapshot_id: str
    session_id: str
    baseline_id: str

    def read(self, path: str) -> bytes:
        """Resolve overlay first, then baseline. ADR-003 Q-B."""
        ...

    def list_changed(self) -> list[str]:
        """Artifacts that differ from the baseline in this snapshot."""
        ...

    def diff_from(self, other: "Snapshot") -> list[str]:
        """Artifact paths whose CONTENT differs between two snapshots.

        Content, not overlay bookkeeping: a write that restores the original
        bytes must produce no difference, or P8 receives a phantom
        `ArtifactChanged` every time a ChangeSet applies a no-op.
        """
        ...


@runtime_checkable
class BaselineArtifactStore(Protocol):
    """READ-ONLY. ADR-001 contract #1."""

    baseline_id: str

    def read(self, path: str) -> bytes: ...

    def names(self) -> list[str]: ...

    def manifest(self) -> dict[str, str]:
        """path -> sha256. The input to `baseline_id`."""
        ...

    def write(self, path: str, content: bytes) -> None:
        """MUST raise BaselineImmutableError. Never implemented otherwise."""
        ...


@runtime_checkable
class SessionArtifactOverlay(Protocol):
    """read-write. ADR-001 contract #2. The abstraction that carries P7, P8, P9.

    Three contract tests, one per claim, listed here because they are part
    of the contract rather than part of some implementation's test file:

        1. write() never alters the baseline    -- hash before and after
        2. read() resolves overlay then baseline
        3. session A never observes session B's overlay
    """

    session_id: str
    head: Snapshot

    def write(self, path: str, content: bytes) -> Snapshot:
        """Store bytes in this session and mint a NEW snapshot.

        Never UPDATEs a row and never overwrites an object: the overlay is
        append-only, which is what makes an old snapshot still readable and
        `diff_from` meaningful.

        The single write path (P7_FROZEN §7): this is `write_all` with one
        entry, not a second door into the overlay. `apply_gate` calls
        `write_all` directly so a multi-artifact apply mints exactly one
        snapshot; this method exists for single-artifact callers (e.g.
        `scenario.external_edit`, P9's CONTROLLED path) and single-artifact
        tests.
        """
        ...

    def write_all(self, contents: dict[str, bytes]) -> Snapshot:
        """Write every artifact in `contents` into exactly ONE new snapshot.

        A loop of `write()` calls advances the
        head on every iteration, so a failure on artifact N leaves artifacts
        1..N-1 already published even though the overall apply raised.
        `write_all` MUST NOT publish anything -- must not advance `head`,
        must not make any path readable at the new value -- unless every
        entry in `contents` is written. If any entry cannot be written, the
        overlay is left exactly as it was before the call.
        """
        ...

    def clear(self) -> Snapshot:
        """Reset to the pure baseline view (one-click reset)."""
        ...

    def read_at(self, snapshot: Snapshot, path: str) -> bytes: ...


@runtime_checkable
class EvidenceStore(Protocol):
    """READ-ONLY. Evidence is written at build time (tier 1) and only read at runtime."""

    def get(self, evidence_id: str) -> dict: ...

    def for_claim(self, claim_id: str) -> list[dict]: ...

    def quote_still_present(self, evidence: dict, snapshot: Snapshot) -> bool:
        """I4: does the quote actually exist at that location, in THIS snapshot?

        Takes a snapshot rather than reading the baseline, because after an
        apply the answer can change -- and evidence that silently refers to a
        superseded artifact is worse than no evidence.
        """
        ...


@runtime_checkable
class GraphStore(Protocol):
    """READ-ONLY at runtime. The graph is built in tier 1 (ADR-001)."""

    def node(self, node_id: str) -> dict: ...

    def edges_from(self, node_id: str, edge_types: list[str] | None = None) -> list[dict]: ...

    def impact_scope(self, node_ids: list[str], max_depth: int | None = None) -> dict:
        """DIRECT and INDIRECT are returned separately, never merged.

        Merging them is how precision dies: an indirect neighbour four hops
        out counts the same as the artifact that literally states the rule,
        and every impact set becomes the whole graph.
        """
        ...


@runtime_checkable
class StateStore(Protocol):
    """read-write -> Cloud SQL. Intents, changesets, approvals, verifications."""

    def put(self, kind: str, record_id: str, record: dict, *, session_id: str) -> None: ...

    def get(self, kind: str, record_id: str, *, session_id: str) -> dict | None: ...

    def list(self, kind: str, *, session_id: str) -> list[dict]: ...

    def append_event(self, event: dict, *, session_id: str) -> None:
        """Append-only. I12 and I14 are only auditable if history cannot be edited."""
        ...


@runtime_checkable
class SessionStore(Protocol):
    """read-write -> Cloud SQL. Judge session isolation (I18)."""

    def create(self, baseline_id: str) -> str: ...

    def get(self, session_id: str) -> dict | None: ...

    def reset(self, session_id: str) -> str:
        """Drop the overlay and return a fresh session on the same baseline."""
        ...


#: Every contract, and whether runtime may write through it. A
#: single contract-test suite runs against both the local and the cloud adapter, and
#: reads this table to know which of the two halves to run.
CONTRACTS: dict[str, str] = {
    "BaselineArtifactStore": "read-only",
    "SessionArtifactOverlay": "read-write",
    "EvidenceStore": "read-only",
    "GraphStore": "read-only",
    "StateStore": "read-write",
    "SessionStore": "read-write",
}

#: Physical placement, FROZEN in ADR-001/ADR-003. Restated here because a
#: decision that lives only in prose gets re-litigated in every phase.
PHYSICAL_STORAGE: dict[str, str] = {
    "artifact_bytes": "Cloud Storage (gs://<bucket>/...)",
    "metadata": "Cloud SQL (overlay_artifacts, snapshots, sessions, ...)",
    "binary_in_cloud_sql": "NEVER",
}
